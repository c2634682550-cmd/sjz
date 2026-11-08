"""精确触发内核：在指定的 QPC 时刻执行一次回调。

存在意义：OCR 有延迟，等「读到价 → 判断 → 点击」串行跑完，点击必然晚于
倒计时归零。这个模块只解决「怎么在指定时刻精确触发一次动作」，不含任何
业务判断，也不含鼠标注入实现 —— 注入函数由调用方传进来。

时基是 `QueryPerformanceCounter`（下称 QPC）。QPC 是单调计数器，单位是
「tick」，tick 长度 = 1/频率（本机 10MHz，即 100ns）。所有目标时刻都用
tick 表示，和 `time.time()` / 系统时间无关，不受 NTP 校时、时区、改系统
时间影响。

四段式等待（这是精度的来源，也是精度的上限）：

    远段   剩余 > spin_s     -> time.sleep(分片)，把长时间等待交给内核
    近段   剩余 <= spin_s    -> 纯 QPC 自旋，不用任何 sleep
    触发   首次观测 >= deadline -> 立刻回调，只读一次 QPC
    收尾   恢复线程优先级 / 计时器精度

为什么近段必须自旋：Windows 的 Sleep 即使开了 timeBeginPeriod(1)，实测
仍会超时 0.45~0.6ms（p50~450us），而且超时量不可预测。把最后 3ms 交给
自旋，超时量从「几百微秒」压到「几百纳秒~几十微秒」（看 CPU 抢占）。

线程优先级：自旋线程提到 THREAD_PRIORITY_TIME_CRITICAL(15)。这会让它
在它占着的那个核上压过所有普通线程（包括渲染、输入、其他 Python 线程），
只在最后 spin_s 毫秒内生效，触发后立即恢复。副作用见模块末尾说明。

用法：

    from core.precision import fire_at, now_ticks, seconds_to_ticks

    target = now_ticks() + seconds_to_ticks(0.5)
    shot = fire_at(target, lambda: injector.click(x, y))   # 后台线程到点触发
    ...
    shot.cancel()          # 随时可取消，不会卡住线程
    shot.wait()            # 等它结束

    # 提前量：想在 target 前 8ms 就把 down/up 注入出去
    shot = fire_at(target, click, lead_s=0.008)
"""
from __future__ import annotations

import contextlib
import ctypes
import threading
import time
import traceback
from ctypes import wintypes

__all__ = [
    "PrecisionUnavailable",
    "Shot",
    "fire_at",
    "fire_in",
    "fire_sync",
    "now_ticks",
    "seconds_to_ticks",
    "ticks_to_seconds",
    "ticks_per_second",
    "timer_resolution",
    "current_timer_resolution_ms",
    "DEFAULT_SPIN_S",
    "DEFAULT_TIMER_MS",
    "THREAD_PRIORITY_TIME_CRITICAL",
    "THREAD_PRIORITY_HIGHEST",
]

# 线程优先级（winbase.h）。15 = TIME_CRITICAL，和进程优先级类别无关，
# 在任何非 REALTIME 类别里都表示「该类别下的最高」。
THREAD_PRIORITY_TIME_CRITICAL = 15
THREAD_PRIORITY_HIGHEST = 2

# 默认值来自实测（见 tests/test_precision.py 与 tools/bench_precision.py）：
# Sleep 超时 p95≈0.56ms，自旋阈值留 3ms 足够覆盖；阈值再大只会白白占核。
DEFAULT_SPIN_S = 0.003
DEFAULT_TIMER_MS = 1
# 远段一次最多睡 50ms：取消请求最迟这么久被看见。
DEFAULT_SLEEP_SLICE_S = 0.05


class PrecisionUnavailable(RuntimeError):
    """当前环境拿不到 QPC（理论上只会发生在非 Windows 上）。"""


# ---------------------------------------------------------------- Win32 绑定

_AVAILABLE = True
_LOAD_ERROR = ""
try:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _winmm = ctypes.WinDLL("winmm")
except OSError as exc:                      # pragma: no cover - 非 Windows
    _AVAILABLE = False
    _LOAD_ERROR = str(exc)
    _k32 = _winmm = None

if _AVAILABLE:
    _k32.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(ctypes.c_int64)]
    _k32.QueryPerformanceFrequency.restype = wintypes.BOOL
    _k32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_int64)]
    _k32.QueryPerformanceCounter.restype = wintypes.BOOL

    # 必须显式声明：默认 restype=c_int 会在 64 位下把句柄截断。
    _k32.GetCurrentThread.restype = wintypes.HANDLE
    _k32.GetCurrentThread.argtypes = []
    _k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _k32.SetThreadPriority.restype = wintypes.BOOL
    _k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
    _k32.GetThreadPriority.restype = ctypes.c_int

    _winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
    _winmm.timeBeginPeriod.restype = ctypes.c_uint
    _winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
    _winmm.timeEndPeriod.restype = ctypes.c_uint

    _freq_buf = ctypes.c_int64(0)
    if not _k32.QueryPerformanceFrequency(ctypes.byref(_freq_buf)) or _freq_buf.value <= 0:
        _AVAILABLE = False
        _LOAD_ERROR = "QueryPerformanceFrequency 失败"
    _FREQ = int(_freq_buf.value) if _AVAILABLE else 0
else:                                       # pragma: no cover
    _FREQ = 0


def _require():
    if not _AVAILABLE:
        raise PrecisionUnavailable(f"QPC 不可用：{_LOAD_ERROR}")


def ticks_per_second():
    """QPC 频率（每秒 tick 数）。本机实测 10 000 000，即 1 tick = 100ns。"""
    _require()
    return _FREQ


def seconds_to_ticks(seconds):
    """秒 -> tick。四舍五入，避免固定补偿量被截断成整 tick 时丢精度。"""
    _require()
    return int(round(float(seconds) * _FREQ))


def ticks_to_seconds(ticks):
    """tick -> 秒。"""
    _require()
    return float(ticks) / _FREQ


_tls = threading.local()


def now_ticks():
    """读一次 QPC。热路径，复用本线程的缓冲区（省一次分配）。"""
    _require()
    buf = getattr(_tls, "buf", None)
    if buf is None:
        buf = _tls.buf = ctypes.c_int64(0)
    _k32.QueryPerformanceCounter(ctypes.byref(buf))
    return buf.value


# ---------------------------------------------------------------- 计时器精度

_res_lock = threading.Lock()
_res_periods = []          # 正在生效的 timeBeginPeriod(ms) 值，按调用顺序


def _resolution_current_ms():
    """当前系统计时器分辨率（毫秒），读不到返回 None。

    NtQueryTimerResolution 三个 out 参数顺序是 (最大, 最小, 当前)，
    单位 100ns —— 和名字反直觉，照 ntdll 原型填。
    """
    try:
        ntdll = ctypes.WinDLL("ntdll")
        mx = wintypes.ULONG(0)
        mn = wintypes.ULONG(0)
        cur = wintypes.ULONG(0)
        if ntdll.NtQueryTimerResolution(ctypes.byref(mx), ctypes.byref(mn),
                                        ctypes.byref(cur)) != 0:
            return None
        return cur.value / 10000.0
    except OSError:                        # pragma: no cover
        return None


current_timer_resolution_ms = _resolution_current_ms


@contextlib.contextmanager
def timer_resolution(ms=DEFAULT_TIMER_MS):
    """把系统计时器精度提到 `ms` 毫秒（进程级），退出时还原。

    引用计数：多个 Shot 并行时只有第一个调用真正 timeBeginPeriod，
    最后一个退出才 timeEndPeriod —— 否则先结束的那个会把别人的精度撤掉。
    每次 begin 必须配一次同值的 end（Windows 按 (进程, 值) 计数）。
    """
    _require()
    with _res_lock:
        if _winmm.timeBeginPeriod(int(ms)) == 0:      # TIMERR_NOERROR
            _res_periods.append(int(ms))
            began = True
        else:                                          # pragma: no cover
            began = False
    try:
        yield
    finally:
        if began:
            with _res_lock:
                if int(ms) in _res_periods:
                    _res_periods.remove(int(ms))
                    _winmm.timeEndPeriod(int(ms))


# ---------------------------------------------------------------- 线程优先级

def _thread_priority():
    """当前线程优先级；失败返回 None。"""
    v = _k32.GetThreadPriority(_k32.GetCurrentThread())
    # THREAD_PRIORITY_ERROR_RETURN = 0x7FFFFFFF
    return None if v == 0x7FFFFFFF else int(v)


def _set_thread_priority(value):
    """设置当前线程优先级，返回是否成功。"""
    return bool(_k32.SetThreadPriority(_k32.GetCurrentThread(), int(value)))


# ---------------------------------------------------------------- 一次触发

class Shot:
    """一次排定的精确触发。

    生命周期：构造（算 deadline）-> start()（进线程）-> 等待 -> 回调一次
    -> 结束。取消是幂等的，触发后取消无效（会返回 False）。

    全部时间量单位都是 QPC tick，另给秒的换算属性。
    """

    def __init__(self, target_ticks, action, *, lead_s=0.0, lead_ticks=0,
                 spin_s=DEFAULT_SPIN_S, timer_ms=DEFAULT_TIMER_MS,
                 sleep_slice_s=DEFAULT_SLEEP_SLICE_S,
                 priority=THREAD_PRIORITY_TIME_CRITICAL,
                 cancel=None, name="precision-shot", daemon=True):
        """
        target_ticks : 目标时刻（QPC tick）。到点（或 target-lead）触发一次。
        action       : 无参回调。实际注入鼠标 down/up 的代码由外部传入。
        lead_s       : 提前量（秒）。在 target - lead 触发，用于补偿注入/排队延迟。
        lead_ticks   : 提前量（tick），和 lead_s 叠加。
        spin_s       : 自旋阈值（秒）。剩余时间小于它就转入纯自旋。
        timer_ms     : timeBeginPeriod 的值。传 None 表示不碰系统计时器精度。
        priority     : 自旋期间临时提的线程优先级；None 表示不动。
        cancel       : 可选取消源：threading.Event 或返回 bool 的可调用对象。
        """
        _require()
        if not callable(action):
            raise TypeError("action 必须是可调用对象")
        self.target_ticks = int(target_ticks)
        self.lead_ticks = int(lead_ticks) + seconds_to_ticks(lead_s)
        if self.lead_ticks < 0:
            raise ValueError("提前量不能为负")
        self.deadline_ticks = self.target_ticks - self.lead_ticks

        self.spin_s = max(0.0, float(spin_s))
        self.spin_ticks = max(1, seconds_to_ticks(self.spin_s))
        self.timer_ms = None if timer_ms is None else int(timer_ms)
        self.sleep_slice_s = max(0.001, float(sleep_slice_s))
        self.priority = None if priority is None else int(priority)
        self.action = action
        self.name = name
        self.daemon = daemon

        self._cancel_evt = threading.Event()
        if cancel is None:
            self._cancel_fn = None
        elif callable(cancel):
            self._cancel_fn = cancel
        elif hasattr(cancel, "is_set"):
            self._cancel_fn = cancel.is_set
        else:
            raise TypeError("cancel 必须是 Event 或返回 bool 的可调用对象")

        # ---- 结果/统计（供测试与报告用）
        self.fired = False
        self.cancelled = False
        self.error = None
        self.error_text = ""
        self.trigger_ticks = None        # 实际触发的 QPC 读数（回调前最后一次读）
        self.action_start_ticks = None   # 回调入口处的 QPC 读数（t_call）
        self.setup_ticks = None
        self.finished_ticks = None
        self.action_ticks = None         # 回调本身的耗时
        self.spin_loops = 0
        self.sleep_chunks = 0
        self.priority_before = None
        self.priority_during = None
        self.priority_after = None
        self.priority_error = ""

        self._thread = None
        self._done = threading.Event()
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------ 属性

    @property
    def deviation_ticks(self):
        """实际触发 - deadline（tick）。内核自身误差，恒 >= 0。"""
        if self.trigger_ticks is None:
            return None
        return self.trigger_ticks - self.deadline_ticks

    @property
    def deviation_s(self):
        """实际触发 - deadline（秒）。"""
        d = self.deviation_ticks
        return None if d is None else d / _FREQ

    @property
    def offset_s(self):
        """实际触发 - target（秒）。带提前量时约等于 -lead_s，用于校核补偿。"""
        if self.trigger_ticks is None:
            return None
        return (self.trigger_ticks - self.target_ticks) / _FREQ

    @property
    def deviation_us(self):
        """实际触发 - deadline（微秒）。报告和测试里最常用的单位。"""
        d = self.deviation_ticks
        return None if d is None else d / _FREQ * 1e6

    @property
    def call_latency_ticks(self):
        """回调入口 - deadline（tick）。

        整条卡点链路里唯一由本模块决定的抖动：判据命中那一刻到回调真正
        进入之间只隔着一次 QPC 读 + 一次函数调用。等待剩余时间不参与统计。
        """
        if self.action_start_ticks is None:
            return None
        return self.action_start_ticks - self.deadline_ticks

    @property
    def call_latency_us(self):
        """回调入口 - deadline（微秒）。"""
        d = self.call_latency_ticks
        return None if d is None else d / _FREQ * 1e6

    @property
    def call_latency_from_target_ticks(self):
        """回调入口 - target（tick）。字面口径：从「目标时刻」到回调被调用。

        带提前量时该值约等于 -lead；不带提前量时与 call_latency_ticks 相同。
        """
        if self.action_start_ticks is None:
            return None
        return self.action_start_ticks - self.target_ticks

    @property
    def call_latency_from_target_us(self):
        """回调入口 - target（微秒）。"""
        d = self.call_latency_from_target_ticks
        return None if d is None else d / _FREQ * 1e6

    def done(self):
        return self._done.is_set()

    def wait(self, timeout=None):
        """阻塞到结束，返回是否已结束。"""
        return self._done.wait(timeout)

    def join(self, timeout=None):
        t = self._thread
        if t is not None:
            t.join(timeout)

    def is_alive(self):
        t = self._thread
        return bool(t and t.is_alive())

    # ------------------------------------------------------------ 取消

    def cancel(self):
        """请求取消。已触发或已结束则返回 False（不能撤回已经点出去的那一下）。"""
        with self._state_lock:
            if self.fired or self._done.is_set():
                return False
            self._cancel_evt.set()
            return True

    def _cancelled(self):
        if self._cancel_evt.is_set():
            return True
        fn = self._cancel_fn
        return bool(fn()) if fn is not None else False

    # ------------------------------------------------------------ 启动

    def start(self):
        t = threading.Thread(target=self._run, name=self.name, daemon=self.daemon)
        self._thread = t
        t.start()
        return self

    # ------------------------------------------------------------ 等待逻辑

    def _spin(self, deadline):
        """纯自旋。返回首次 >= deadline 的读数；被取消返回 None。

        循环里只做三件事：读 QPC、比较、看取消。任何多余操作都会直接变成
        触发延迟。绑定局部变量，避免每轮做属性查找。
        """
        q = _k32.QueryPerformanceCounter
        buf = self._tls_buf()
        byref = ctypes.byref
        evt_is_set = self._cancel_evt.is_set
        cfn = self._cancel_fn
        i = 0
        while True:
            q(byref(buf))
            now = buf.value
            if now >= deadline:
                self.spin_loops = i
                return now
            i += 1
            if evt_is_set():
                return None
            if cfn is not None and cfn():
                return None

    def _tls_buf(self):
        buf = getattr(_tls, "buf", None)
        if buf is None:
            buf = _tls.buf = ctypes.c_int64(0)
        return buf

    def _wait_until(self, deadline):
        """远段 sleep + 近段自旋。返回触发读数，取消返回 None。"""
        spin_ticks = self.spin_ticks
        sleep_ticks = seconds_to_ticks(self.sleep_slice_s)
        while True:
            now = now_ticks()
            if now >= deadline:
                return now
            if self._cancelled():
                return None
            remaining = deadline - now
            if remaining <= spin_ticks:
                return self._spin(deadline)
            # 远段：留出 spin_ticks，且一次最多睡 sleep_slice_s
            chunk = min((remaining - spin_ticks) / _FREQ,
                        self.sleep_slice_s)
            time.sleep(chunk)
            self.sleep_chunks += 1

    # ------------------------------------------------------------ 线程主体

    def _run(self):
        prev_prio = None
        try:
            self.setup_ticks = now_ticks()
            prio_ctx = (timer_resolution(self.timer_ms) if self.timer_ms is not None
                        else contextlib.nullcontext())
            with prio_ctx:
                prev_prio = _thread_priority()
                self.priority_before = prev_prio
                if self.priority is not None and prev_prio is not None:
                    if not _set_thread_priority(self.priority):
                        self.priority_error = (
                            f"SetThreadPriority({self.priority}) 失败，"
                            f"err={ctypes.get_last_error()}")
                    else:
                        self.priority_during = _thread_priority()
                try:
                    trigger = self._wait_until(self.deadline_ticks)
                    if trigger is None:
                        self.cancelled = True
                        return
                    # 触发点：这里之后到 action() 之间不能再有任何分支/记日志，
                    # 否则那些时间都会算进偏差里。
                    self.trigger_ticks = trigger
                    self.fired = True
                    t_call = now_ticks()
                    self.action_start_ticks = t_call
                    try:
                        self.action()
                    finally:
                        self.finished_ticks = now_ticks()
                        self.action_ticks = self.finished_ticks - t_call
                finally:
                    # 无论触发、取消还是回调抛异常，都必须把优先级还回去
                    if prev_prio is not None:
                        _set_thread_priority(prev_prio)
                    self.priority_after = _thread_priority()
        except BaseException as exc:                 # noqa: BLE001
            self.error = exc
            self.error_text = traceback.format_exc()
            if prev_prio is not None:
                _set_thread_priority(prev_prio)
                self.priority_after = _thread_priority()
        finally:
            if self.finished_ticks is None:
                self.finished_ticks = now_ticks()
            self._done.set()


# ---------------------------------------------------------------- 对外接口

def fire_at(target_ticks, action, **kwargs):
    """在 `target_ticks` 那一刻（扣掉 lead）在后台线程触发一次 `action`。

    返回 Shot；`Shot.fired` / `deviation_s` / `error` 给出结果。异常不会
    抛到调用线程 —— 回调里的异常记在 `shot.error_text`，需要的话自己检查。
    """
    return Shot(target_ticks, action, **kwargs).start()


def fire_in(delay_s, action, **kwargs):
    """在「现在 + delay_s」那一刻触发。delay_s 是相对触发器的时间，不含提前量。"""
    target = now_ticks() + seconds_to_ticks(delay_s)
    return fire_at(target, action, **kwargs)


def fire_sync(target_ticks, action, **kwargs):
    """在当前线程阻塞到点并触发。会临时改动当前线程优先级，结束即还原。

    适合本来就要独占线程的场景；一般用 fire_at 起后台线程更省事。
    """
    shot = Shot(target_ticks, action, **kwargs)
    shot._run()                                # noqa: SLF001 - 本模块内部接口
    return shot


# ---------------------------------------------------------------- 副作用说明
#
# 1) THREAD_PRIORITY_TIME_CRITICAL：只在这一个线程、只在最后 spin_s 毫秒
#    生效。它在该核上优先于普通线程 —— 同一核上的渲染/输入/DWM/其他 Python
#    线程会被压住 spin_s 毫秒。spin_s 越大，卡顿风险越大；3ms 是实测够用且
#    几乎无感的量。触发后立即恢复原优先级，异常路径也恢复。
#
# 2) timeBeginPeriod(1)：进程级提升系统计时器精度，会影响整机的 Sleep 粒度
#    和定时器功耗（Win10 2004 之后是进程级，不再拖累全局，但仍提高唤醒频率）。
#    引用计数配对，最后一个 Shot 结束才还原。
#
# 3) 自旋期间持有 GIL：同进程其他 Python 线程在这 3ms 内拿不到 GIL。
#    这是 Python 的固有限制，不是 GIL 释放点；所以 spin_s 不宜调大。
#
# 4) 本模块不注入任何输入，也不修改光标；action 里做什么完全由调用方决定。
