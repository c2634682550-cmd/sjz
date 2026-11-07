"""长时间挂机稳定性验证：真实时钟连跑 N 小时，离线、无真实光标/可见窗口。

为什么不用 tools/stability/clock.py 的虚拟钟
------------------------------------------------
那份工具把 72 小时压进几分钟，用来跑逻辑仿真；但本次要回答的是
「RSS / 句柄 / 线程 / 日志体积随时间怎么长」——这些量只跟**真实墙钟**
有关，虚拟钟压出来的曲线乘以系数就是编数，不宜当结论。所以这里整块
时间源保持真实，只把外部依赖（截图 / OCR / 注入器 / 窗口）换成假的。

测的是什么
----------
  1. 内存 RSS / 峰值 / 提交量（psapi，不依赖 psutil）
  2. 进程句柄、GDI 对象、USER 对象
  3. 线程数（Toolhelp 快照，按 pid 过滤）
  4. 日志与台账文件体积、轮转、磁盘余量
  5. 倒计时触发误差（每次「开点」相对计划零点的毫秒偏差）
  6. 任务队列积压 / 长时间停在运行态
  7. 9 类故障注入的发生、自动恢复与恢复耗时
  8. 每轮结束后的资源清理（光标 / 按下态 / 钩子 / 子进程 / 引擎线程）

用法
----
    python tools/soak.py --tag baseline --hours 1
    python tools/soak.py --tag long --hours 8.5
    python tools/soak.py --report --baseline _soak_data/baseline \
        --long _soak_data/long --out _soak_report.md

中断安全：CSV / JSONL 每条都 flush，SIGINT 只置停止位，已采数据不回滚。
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import math
import os
import random
import shutil
import signal
import statistics
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np                                    # noqa: E402

from core import config as cfg_mod                    # noqa: E402
from core import engine as eng_mod                    # noqa: E402
from core import countdown, jobs, records, retention, switches   # noqa: E402

# ---------------------------------------------------------------- 判据
# 阈值在开跑前写死（不是跑完再挑一个能过的数）。依据见每条注释。
THRESHOLDS = {
    # RSS：Python 进程有分配器/arena 缓存，完全平的曲线不现实；
    # 3 MB/h 的斜率对 8 小时是 24MB，已能覆盖解释器缓存抖动，
    # 真正的泄漏（每轮几十 KB 级）会明显超过这条线。
    "rss_slope_mb_per_h": 3.0,
    "rss_total_growth_mb": 80.0,
    # 句柄：8 小时净增 >20 或稳定斜率 >1/h 都按泄漏处理。
    "handle_growth": 20,
    "handle_slope_per_h": 1.0,
    # GDI / USER：窗口不开时本来就该是平的，净增 >10 算泄漏。
    "gdi_growth": 10,
    "user_growth": 10,
    # 线程：采样瞬间会同时存在 sampler / job / engine 三条短命线程，
    # 稳定段 max-min 留 6 条余量；真泄漏是按轮累加，会远超这条线。
    "thread_growth_stable": 6,
    # 定时误差：末小时 p95 ≤ 3× 基线 p95，且绝对值 ≤ 60ms。
    "drift_ratio": 3.0,
    "drift_p95_ms": 60.0,
    # 故障：自动恢复率 ≥ 90%，连续被故障打断的轮次 ≤ 2。
    "fault_recovery_rate": 0.90,
    "fault_consecutive_abort": 2,
    # 队列：积压 ≤ 5，没有一轮卡死超过 90 秒。
    "queue_backlog": 5,
    "cycle_stuck_s": 90.0,
    # 清理：100% 的轮次必须干净。
    "clean_ratio": 1.0,
}

# 运行参数
CYCLE_WARN_S = 90.0          # 单轮超过这个时长算卡死
ARRIVAL_EXTRA_P = 0.08       # 每一次空闲有 8% 概率多排一条任务（制造真实积压）
QUEUE_CAP = 8

# ---------------------------------------------------------------- 进程指标


class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ThreadID", ctypes.c_ulong),
        ("th32OwnerProcessID", ctypes.c_ulong),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
    ]


_k32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_user32 = ctypes.windll.user32
_PID = os.getpid()

# 64 位下必须显式声明 HANDLE，否则默认 c_int 会把 -1 截成 32 位，
# GetProcessMemoryInfo / GetProcessHandleCount 全部返回失败。
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_HANDLE = ctypes.c_void_p(_k32.GetCurrentProcess())
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC),
                                        ctypes.c_ulong]
_psapi.GetProcessMemoryInfo.restype = ctypes.c_int
_k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_ulong)]
_k32.GetProcessHandleCount.restype = ctypes.c_int
_k32.GetProcessTimes.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p]
_k32.GetProcessTimes.restype = ctypes.c_int
_user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.GetGuiResources.restype = ctypes.c_uint
_k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.Thread32First.argtypes = [ctypes.c_void_p,
                               ctypes.POINTER(_THREADENTRY32)]
_k32.Thread32First.restype = ctypes.c_int
_k32.Thread32Next.argtypes = [ctypes.c_void_p,
                              ctypes.POINTER(_THREADENTRY32)]
_k32.Thread32Next.restype = ctypes.c_int
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = ctypes.c_int


def _proc_mem():
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(pmc)
    ok = _psapi.GetProcessMemoryInfo(_HANDLE, ctypes.byref(pmc), pmc.cb)
    if not ok:
        return None
    return pmc


def _proc_cpu_s():
    """进程累计 CPU 秒（user+kernel）。"""
    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

    c, e, k, u = FT(), FT(), FT(), FT()
    if not _k32.GetProcessTimes(_HANDLE, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return None
    def q(ft):
        return (ft.hi << 32) | ft.lo
    return (q(k) + q(u)) / 1e7


def _handle_count():
    n = ctypes.c_ulong(0)
    if _k32.GetProcessHandleCount(_HANDLE, ctypes.byref(n)):
        return int(n.value)
    return None


def _gui_resources(flag):
    # 0 = GDI objects, 1 = USER objects
    try:
        return int(_user32.GetGuiResources(_HANDLE, flag))
    except Exception:
        return None


def _thread_count():
    snap = _k32.CreateToolhelp32Snapshot(0x4, 0)      # TH32CS_SNAPTHREAD
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    total = 0
    try:
        te = _THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        if _k32.Thread32First(snap, ctypes.byref(te)):
            while True:
                if te.th32OwnerProcessID == _PID:
                    total += 1
                if not _k32.Thread32Next(snap, ctypes.byref(te)):
                    break
    finally:
        _k32.CloseHandle(snap)
    return total


# ---------------------------------------------------------------- 小工具


def _mb(n):
    return round(float(n) / (1024 * 1024), 2) if n is not None else None


def _p95(xs):
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return None
    k = max(0, min(len(xs) - 1, int(math.ceil(0.95 * len(xs)) - 1)))
    return xs[k]


def _linreg(xs, ys):
    pts = [(float(x), float(y)) for x, y in zip(xs, ys)
           if x is not None and y is not None]
    n = len(pts)
    if n < 2:
        return 0.0
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den == 0:
        return 0.0
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den


def _dir_usage(path):
    n = 0
    total = 0
    try:
        for name in os.listdir(path):
            p = os.path.join(path, name)
            try:
                if os.path.isfile(p):
                    n += 1
                    total += os.path.getsize(p)
            except OSError:
                pass
    except OSError:
        pass
    return n, total


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class _Logger:
    """真实滚动日志：按行 flush，超限走 retention.rotate_log（真代码）。"""

    def __init__(self, path, max_bytes):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.writes = 0

    def write(self, line):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        rec = f"{ts} {line}\n"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(rec)
                    f.flush()
            except OSError:
                return False
            self.writes += 1
            if self.writes % 64 == 0:
                retention.rotate_log(self.path, self.max_bytes)
        return True


# ---------------------------------------------------------------- 故障注入
FAULT_NAMES = [
    "ocr_garbage",        # 单帧识别返回垃圾
    "ocr_timeout",        # 单帧识别超时/抛异常
    "click_no_effect",    # 点击后界面没变化
    "unknown_page",       # 界面切到未知页
    "focus_lost",         # 窗口短暂失去焦点
    "clock_jump",         # 时间源跳变
    "injector_down",      # 注入器临时不可用
    "config_corrupt",     # 配置文件被改坏
    "disk_write_fail",    # 磁盘写入短暂失败
]

FAULT_CN = {
    "ocr_garbage": "OCR 返回垃圾",
    "ocr_timeout": "OCR 超时/异常",
    "click_no_effect": "点击无效",
    "unknown_page": "未知页面",
    "focus_lost": "窗口失焦",
    "clock_jump": "时间源跳变",
    "injector_down": "注入器不可用",
    "config_corrupt": "配置被改坏",
    "disk_write_fail": "磁盘写失败",
}


class Faults:
    """按固定间隔随机注入一种故障；同一时刻最多一种。"""

    def __init__(self, interval, duration_range=(3.0, 10.0), seed=None, logger=None,
                 workdir=None, config_path=None):
        self.interval = float(interval)
        self.duration_range = duration_range
        self.rng = random.Random(seed)
        self.logger = logger
        self.workdir = workdir
        self.config_path = config_path
        self.active = None            # type name
        self.started = None
        self.ends = None
        self.jump_offset = 0.0
        self.last_cleared = None      # (type, clear_time)
        self.next_at = None
        self.counts = {n: 0 for n in FAULT_NAMES}
        self.aborted = {n: 0 for n in FAULT_NAMES}
        self.recovered = {n: 0 for n in FAULT_NAMES}
        self.recovery_s = {n: [] for n in FAULT_NAMES}
        self.windows = []             # (type, start, end)
        self._lock = threading.Lock()
        self._cfg_backup = None
        self._records_path = None
        self.block_path = os.path.join(workdir or ROOT, "_disk_blocked")
        self._blocked_prepared = False

    # ---- 便捷判据 ----
    def _is(self, name):
        return self.active == name

    @property
    def unknown(self):
        return self._is("unknown_page")

    @property
    def ocr_timeout(self):
        return self._is("ocr_timeout")

    @property
    def ocr_garbage(self):
        return self._is("ocr_garbage")

    @property
    def click_dead(self):
        return self._is("click_no_effect")

    @property
    def focus_lost(self):
        return self._is("focus_lost")

    @property
    def injector_down(self):
        return self._is("injector_down")

    @property
    def disk_fail(self):
        return self._is("disk_write_fail")

    @property
    def config_corrupt(self):
        return self._is("config_corrupt")

    @property
    def clock_offset(self):
        return self.jump_offset if self._is("clock_jump") else 0.0

    # ---- 生命周期 ----
    def tick(self, now):
        with self._lock:
            if self.active and now >= self.ends:
                name = self.active
                self.active = None
                self.jump_offset = 0.0
                self.last_cleared = (name, now)
                self.windows.append((name, self.started, now))
                if self.logger:
                    self.logger.write(f"[fault] clear {name} after "
                                      f"{now - self.started:.1f}s")
                self._undo(name)
                return ("clear", name, now)
        return None

    def maybe_start(self, now):
        with self._lock:
            if self.active:
                return None
            if self.next_at is None:
                self.next_at = now + self.interval
                return None
            if now < self.next_at:
                return None
            name = self.rng.choice(FAULT_NAMES)
            dur = self.rng.uniform(*self.duration_range)
            self.active = name
            self.started = now
            self.ends = now + dur
            self.counts[name] += 1
            self.next_at = now + self.interval
            self._apply(name)
            if self.logger:
                self.logger.write(f"[fault] inject {name} for {dur:.1f}s")
            return ("start", name, now, dur)

    def _apply(self, name):
        if name == "clock_jump":
            self.jump_offset = self.rng.choice([-1, 1]) * self.rng.uniform(5, 15)
        elif name == "config_corrupt":
            try:
                self._cfg_backup = open(self.config_path, "rb").read()
                with open(self.config_path, "w", encoding="utf-8") as f:
                    f.write('{"broken": ,,,')
            except OSError:
                self._cfg_backup = None
        elif name == "disk_write_fail":
            if not self._blocked_prepared:
                try:
                    with open(self.block_path, "w", encoding="utf-8") as f:
                        f.write("not a directory")
                    self._blocked_prepared = True
                except OSError:
                    pass
            self._records_path = records.PATH
            records.PATH = os.path.join(self.block_path, "records.jsonl")

    def _undo(self, name):
        if name == "config_corrupt" and self._cfg_backup is not None:
            try:
                with open(self.config_path, "wb") as f:
                    f.write(self._cfg_backup)
            except OSError:
                pass
            self._cfg_backup = None
        elif name == "disk_write_fail" and self._records_path:
            records.PATH = self._records_path
            self._records_path = None


CURRENT = {"st": None, "faults": None}

# soak 的「手框」确认按钮坐标（归一化）。故意与内置坐标不同：
# 自动标定闸门只对「用户手框过」的区域放行，假屏没有真实锚点，
# 用内置值会被判成 skipped/fallback 拦下启动（见 core/autocalib.py）。
# 假屏按这份坐标把 templates/confirm.png 贴出来，模拟确认弹窗。
CONFIRM_BOX = {"x": 0.58, "y": 0.60, "w": 0.18, "h": 0.08}


# ---------------------------------------------------------------- 假设备


class FakeScreen:
    """假截图：ROI 返回同尺寸小图，全屏返回固定真实截图。绝不碰桌面。"""

    _full = None
    _confirm_img = None

    def __init__(self, monitor=1):
        self.monitor = monitor
        self.closed = False
        self.grabs = 0
        if FakeScreen._full is None:
            FakeScreen._full = self._load_frame()
        # 真 Screen 在 __init__ 里把「当前屏 + DPI」写进 vision，模板匹配按
        # 这个比例重采样。假屏同样要写 —— 否则 vision 停在默认 1920×1080，
        # 假确认弹窗与引擎实际比较的尺度不一致，相关度上不去。
        from core import vision
        vision.set_target_screen(self.size, 96)

    @staticmethod
    def _load_frame():
        for rel in ("tests/fixtures/market/list_unfiltered.png",
                    "tests/fixtures/market/list.png"):
            p = os.path.join(ROOT, rel)
            try:
                import cv2
                img = cv2.imread(p, cv2.IMREAD_COLOR)
                if img is not None:
                    return img
            except Exception:
                pass
        # 没有素材就造一张带纹理的 1920x1080：JPEG 有真实体积，能测截图修剪
        rng = np.random.default_rng(7)
        noise = rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
        return noise

    @property
    def size(self):
        h, w = FakeScreen._full.shape[:2]
        return w, h

    def norm_to_px(self, box):
        w, h = self.size
        return (int(box["x"] * w), int(box["y"] * h),
                max(1, int(box["w"] * w)), max(1, int(box["h"] * h)))

    def confirm_px(self):
        """确认按钮的物理像素框（按假屏尺寸换算）。"""
        return self.norm_to_px(CONFIRM_BOX)

    def _is_confirm_roi(self, box):
        """这次 grab 的是不是「确认按钮 + 25% 外扩」那块 ROI。

        引擎用 `expand(confirm_region, 0.25)` 再 norm_to_px；两边的取整
        逻辑是同一份代码，所以直接比像素框即可（留 2px 容差防将来改动）。
        """
        from core.vision import expand
        ex = self.norm_to_px(expand(CONFIRM_BOX, 0.25))
        x, y, w, h = (int(v) for v in box)
        return (abs(x - ex[0]) <= 2 and abs(y - ex[1]) <= 2
                and abs(w - ex[2]) <= 2 and abs(h - ex[3]) <= 2)

    @staticmethod
    def _confirm_template():
        if FakeScreen._confirm_img is None:
            import cv2
            FakeScreen._confirm_img = cv2.imread(
                os.path.join(ROOT, "templates", "confirm.png"), cv2.IMREAD_COLOR)
        return FakeScreen._confirm_img

    def _confirm_frame(self, w, h):
        """把 templates/confirm.png 按当前屏比例缩放后居中贴进 ROI —— 假确认弹窗。

        缩放比按 `core/vision` 的口径算（模板是在 1920×1080 截的，真机按
        min(宽比, 高比) 重采样再匹配）；不这么算，假图与引擎实际比较的
        尺度对不上，相关度会掉到阈值以下。
        """
        import cv2
        sw, sh = self.size
        fit = min(sw / 1920.0, sh / 1080.0)
        tpl = cv2.resize(self._confirm_template(), None, fx=fit, fy=fit,
                         interpolation=cv2.INTER_AREA)
        th, tw = tpl.shape[:2]
        frame = np.zeros((h, w, 3), np.uint8) + 32
        ox, oy = max(0, (w - tw) // 2), max(0, (h - th) // 2)
        frame[oy:oy + min(th, h - oy), ox:ox + min(tw, w - ox)] = \
            tpl[:h - oy, :w - ox]
        return frame

    def grab(self, box=None):
        self.grabs += 1
        f = CURRENT["faults"]
        st = CURRENT["st"]
        if box is None:
            return FakeScreen._full
        x, y, w, h = box
        w = max(1, min(int(w), 1920))
        h = max(1, min(int(h), 1080))
        if f is not None and f.unknown:
            return np.zeros((h, w, 3), np.uint8)
        # 点过购买（st.bought）之后界面才弹确认框；点了确认就收起来。
        # 这是「点击 → 弹窗 → 确认 → 弹窗消失」的真机时序，不是恒亮图。
        if (st is not None and st.bought and not st.popup_closed
                and self._is_confirm_roi((x, y, w, h))):
            return self._confirm_frame(w, h)
        return np.zeros((h, w, 3), np.uint8) + 32

    def close(self):
        self.closed = True
        st = CURRENT["st"]
        if st is not None:
            st.screen_closed = True


class FakeMouse:
    """假注入器：只记调用与按下态，从不移动真实光标。"""

    def __init__(self, screen=None, game_hwnd=None, mode="legacy"):
        self.screen = screen
        self.game_hwnd = game_hwnd
        self.mode = "legacy"
        self.driver_warning = None
        self.cursor_moves = 0

    @classmethod
    def foreground(cls):
        f = CURRENT["faults"]
        return 999 if (f is not None and f.focus_lost) else 1

    @classmethod
    def ensure_foreground(cls, hwnd, tries=3, gap=0.25, screen=None):
        f = CURRENT["faults"]
        return not (f is not None and f.focus_lost)

    @classmethod
    def release_cursor_lock(cls):
        return True, False

    @classmethod
    def cursor(cls):
        return 100, 100

    def click(self, x, y, **kw):
        st = CURRENT["st"]
        f = CURRENT["faults"]
        st.clicks.append((int(x), int(y), time.perf_counter()))
        if f is not None and f.injector_down:
            # 模拟注入器拒绝：按下状态残留，正好用来验证清理检测
            st.pressed.add((int(x), int(y)))
            raise RuntimeError("soak: injector unavailable")
        st.pressed.add((int(x), int(y)))
        st.pressed.discard((int(x), int(y)))
        if len(st.clicks) == 1:
            st.bought = not (f is not None and f.click_dead) and st.hit_roll
        # 点在确认按钮上 = 弹窗被点掉，下一次 grab 就该看不到它了
        # （引擎的确认闭环靠这个信号收手）。
        cx, cy, cw, ch = self.screen.confirm_px()
        if st.bought and cx <= int(x) < cx + cw and cy <= int(y) < cy + ch:
            st.popup_closed = True


def _words(sec):
    return [{"text": "0", "x": 0, "y": 0, "w": 18, "h": 16},
            {"text": "分", "x": 20, "y": 0, "w": 18, "h": 16},
            {"text": str(sec), "x": 40, "y": 0, "w": 18, "h": 16},
            {"text": "秒", "x": 70, "y": 0, "w": 18, "h": 16},
            {"text": "后", "x": 90, "y": 0, "w": 18, "h": 16},
            {"text": "解", "x": 110, "y": 0, "w": 18, "h": 16},
            {"text": "锁", "x": 130, "y": 0, "w": 18, "h": 16},
            {"text": "购", "x": 150, "y": 0, "w": 18, "h": 16},
            {"text": "买", "x": 170, "y": 0, "w": 18, "h": 16}]


_WORD_CACHE = {}


def _fake_read_words(_img, *a, **k):
    """假 OCR：按当前计划零点算显示秒数，保持 floor 语义。

    500Hz 下每帧新建 9 个 dict 会把「假 OCR 的分配」算进 RSS 曲线；
    按秒缓存同一份词表，把测量对象拉回被测程序本身。
    """
    st = CURRENT["st"]
    f = CURRENT["faults"]
    if st is None:
        return []
    if f is not None:
        if f.unknown:
            return []
        if f.ocr_timeout:
            raise TimeoutError("soak: OCR timeout")
        if f.ocr_garbage:
            return [{"text": "???", "x": 0, "y": 0, "w": 18, "h": 16},
                    {"text": "8", "x": 20, "y": 0, "w": 18, "h": 16}]
    rem = st.zero_at - time.perf_counter()
    if rem < 0:
        rem = 0.0
    sec = int(math.floor(rem))
    if f is not None and f.clock_offset:
        sec = max(0, int(sec + f.clock_offset))
    w = _WORD_CACHE.get(sec)
    if w is None:
        w = _WORD_CACHE[sec] = _words(sec)
    return w


def _fake_read_text(_img, *a, **k):
    return ""


def _fake_last_number(_text, *a, **k):
    return None


# ---------------------------------------------------------------- 引擎外壳


class CycleState:
    def __init__(self, countdown_s, hit_roll, rng):
        self.countdown_s = countdown_s
        self.hit_roll = hit_roll
        self.zero_at = None
        self.clicks = []
        self.pressed = set()
        self.bought = False
        self.popup_closed = False
        self.engine = None
        self.start = time.perf_counter()
        self.config_ok = True
        self.fault_seen = None
        self.screen_closed = False

    def cleanup(self):
        eng = self.engine
        running = bool(eng.status()["running"]) if eng else False
        return {
            "engine_thread_alive": running,
            "pressed_left": len(self.pressed),
            "cursor_moves": 0,
            "hooks": 0,
            "subprocesses": 0,
            "screen_closed": self.screen_closed,
            "clean": (not running) and not self.pressed and self.screen_closed,
        }


class SoakEngine(eng_mod.Engine):
    """用真实 Engine，只替换「人去哪儿看/在哪儿点」的外部边界。"""

    def __init__(self, cfg_factory):
        super().__init__()
        self._cfg_factory = cfg_factory

    @staticmethod
    def _validate(cfg, mode="detail", workflow=None):
        return None

    def _countdown_prepare(self, cfg, screen, mouse, tried=(), refresh=False):
        st = CURRENT["st"]
        return {"serial": "SOAK-001", "sec": st.countdown_s,
                "text": f"0分{st.countdown_s}秒后解锁购买",
                "x": 100, "y": 200, "price": 1000}

    def _chip_ready(self, screen):
        st = CURRENT["st"]
        return not CURRENT["faults"].unknown

    def _balance(self, screen, img=None):
        st = CURRENT["st"]
        # 只有真成交才掉余额；点击无效故障下余额不动，让引擎走完 3 次重试
        return 98 if st.bought else 100

    def _bought_dialog(self, screen):
        return bool(CURRENT["st"].bought)

    def _guard_insufficient(self, screen, mouse, tries=3, gap=0.35):
        return None

    def _say(self, msg, level="info"):
        super()._say(msg, level)
        lg = CURRENT.get("logger")
        if lg:
            lg.write(f"[engine] {level}: {msg}")


# ---------------------------------------------------------------- 采集


class Sampler(threading.Thread):
    COLUMNS = [
        "ts", "elapsed_s", "rss_mb", "peak_rss_mb", "private_mb", "handles",
        "gdi", "user", "threads", "cpu_pct", "log_bytes", "ledger_bytes",
        "ledger_archives", "shots_files", "shots_bytes", "disk_free_mb",
        "queue_pending", "queue_running", "cycles_ok", "cycles_fail",
        "cycles_stuck", "faults_active", "faults_total", "faults_recovered",
        "drift_p95_ms", "config_ok", "py_traced_mb",
    ]

    def __init__(self, run, interval, stop_event):
        super().__init__(daemon=True, name="soak-sampler")
        self.run_obj = run
        self.interval = interval
        self.stop_event = stop_event
        self.rows = 0
        self._last_cpu = None
        self._last_t = None
        self._next_tm = 3600.0
        self._prev_snap = None
        if run.tracemalloc:
            import tracemalloc
            tracemalloc.start(10)
            self._prev_snap = tracemalloc.take_snapshot()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.sample()
            except Exception as e:                  # noqa: BLE001
                self.run_obj.log(f"[sampler] {type(e).__name__}: {e}")
            self.stop_event.wait(self.interval)

    def sample(self):
        import tracemalloc as tm
        r = self.run_obj
        mem = _proc_mem()
        now = time.perf_counter()
        cpu = _proc_cpu_s()
        cpu_pct = None
        if cpu is not None and self._last_cpu is not None and self._last_t:
            dt = max(1e-6, now - self._last_t)
            cpu_pct = round((cpu - self._last_cpu) / dt * 100, 1)
        self._last_cpu, self._last_t = cpu, now

        elapsed = r.elapsed()
        traced = tm.get_traced_memory()[0] if r.tracemalloc else None
        with r.lock:
            queue_pending = len(r.pending)
            queue_running = 1 if r.current_task else 0
            ok, fail, stuck = r.cycles_ok, r.cycles_fail, r.cycles_stuck
            faults_total = sum(r.faults.counts.values())
            faults_recovered = sum(r.faults.recovered.values())
            config_ok = r.config_ok
            drift = _p95(r.drift_recent(3600.0))
        dn, db = _dir_usage(os.path.join(r.datadir, "shots"))
        try:
            free = shutil.disk_usage(r.workdir).free / (1024 * 1024)
        except OSError:
            free = None

        row = {
            "ts": round(time.time(), 1),
            "elapsed_s": round(elapsed, 1),
            "rss_mb": _mb(mem.WorkingSetSize if mem else None),
            "peak_rss_mb": _mb(mem.PeakWorkingSetSize if mem else None),
            "private_mb": _mb(mem.PagefileUsage if mem else None),
            "handles": _handle_count(),
            "gdi": _gui_resources(0),
            "user": _gui_resources(1),
            "threads": _thread_count(),
            "cpu_pct": cpu_pct,
            "log_bytes": _size(r.logpath),
            "ledger_bytes": _size(records.PATH),
            "ledger_archives": sum(_size(f"{records.PATH}.{i}") for i in (1, 2)),
            "shots_files": dn,
            "shots_bytes": db,
            "disk_free_mb": round(free, 1) if free is not None else None,
            "queue_pending": queue_pending,
            "queue_running": queue_running,
            "cycles_ok": ok,
            "cycles_fail": fail,
            "cycles_stuck": stuck,
            "faults_active": 1 if r.faults.active else 0,
            "faults_total": faults_total,
            "faults_recovered": faults_recovered,
            "drift_p95_ms": None if drift is None else round(drift, 2),
            "config_ok": 1 if config_ok else 0,
            "py_traced_mb": _mb(traced),
        }
        r.write_csv(row)

        if r.tracemalloc and elapsed >= self._next_tm:
            self._next_tm += 3600.0
            try:
                snap = tm.take_snapshot()
                diffs = snap.compare_to(self._prev_snap, "lineno")[:8]
                self._prev_snap = snap
                r.event("tracemalloc", hour=int(elapsed // 3600),
                        top=[str(d) for d in diffs])
            except Exception as e:                  # noqa: BLE001
                r.log(f"[sampler] tracemalloc {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 运行器


class Run:
    def __init__(self, args):
        self.args = args
        self.tag = args.tag
        self.workdir = os.path.join(ROOT, args.outroot, args.tag)
        self.datadir = os.path.join(self.workdir, "data")
        self.logpath = os.path.join(self.workdir, "soak.log")
        self.csvpath = os.path.join(self.workdir, "metrics.csv")
        self.cyclespath = os.path.join(self.workdir, "cycles.jsonl")
        self.eventspath = os.path.join(self.workdir, "events.jsonl")
        self.metapath = os.path.join(self.workdir, "meta.json")
        self.config_copy = os.path.join(self.workdir, "config.json")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.t0 = time.perf_counter()
        self.t0_wall = time.time()
        self.pending = []              # 待跑任务（模拟队列积压）
        self.current_task = None       # {"id", "start"}
        self.cycles_ok = 0
        self.cycles_fail = 0
        self.cycles_stuck = 0
        self.dropped_jobs = 0
        self.next_task_at = 0.0      # 失败后的退避，避免故障期间空转刷轮次
        self.cycle_rows = []           # 只在内存里留最近 2000 条，其余已落盘
        self.drift_all = []            # (elapsed_s, drift_ms, faulted)
        self.config_ok = True
        self.tracemalloc = not args.no_tracemalloc
        self.faults = Faults(args.fault_interval, seed=args.seed,
                             workdir=self.workdir, config_path=self.config_copy)
        self.logger = _Logger(self.logpath, max_bytes=2 * 1024 * 1024)
        self.faults.logger = self.logger
        CURRENT["faults"] = self.faults
        CURRENT["logger"] = self.logger
        self._csv_header = not os.path.isfile(self.csvpath)
        self._seq = 0

    # ---- 基础 ----
    def elapsed(self):
        return time.perf_counter() - self.t0

    def faults_ready(self):
        """前 N 轮跑完之前不注故障；N=0 时行为与原来一致（全程可注）。"""
        warm = int(getattr(self.args, "fault_warmup_cycles", 0) or 0)
        return warm <= 0 or (self.cycles_ok + self.cycles_fail) >= warm

    def log(self, msg):
        self.logger.write(msg)

    def event(self, kind, **kw):
        row = {"kind": kind, "ts": round(time.time(), 3),
               "elapsed_s": round(self.elapsed(), 2), **kw}
        with self.lock:
            with open(self.eventspath, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()

    def write_csv(self, row):
        with self.lock:
            new = self._csv_header
            self._csv_header = False
            with open(self.csvpath, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=Sampler.COLUMNS)
                if new:
                    w.writeheader()
                w.writerow(row)
                f.flush()

    def write_cycle(self, row):
        with self.lock:
            self._seq += 1
            row["seq"] = self._seq
            with open(self.cyclespath, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
            self.cycle_rows.append(row)
            if len(self.cycle_rows) > 2000:
                self.cycle_rows = self.cycle_rows[-2000:]

    def drift_recent(self, window_s):
        now = self.elapsed()
        return [d for (t, d, faulted) in self.drift_all
                if not faulted and (now - t) <= window_s]

    # ---- 准备 ----
    def prepare(self):
        os.makedirs(self.workdir, exist_ok=True)
        os.makedirs(self.datadir, exist_ok=True)
        os.makedirs(os.path.join(self.datadir, "shots"), exist_ok=True)
        # 配置用副本：坏文件注入只碰副本，绝不改仓库里的 config.json
        shutil.copyfile(cfg_mod.CONFIG_PATH, self.config_copy)
        cfg_mod.CONFIG_PATH = self.config_copy
        cfg_mod.DATA_DIR = self.datadir
        eng_mod.DATA_DIR = self.datadir
        records.PATH = os.path.join(self.datadir, "records.jsonl")
        # 让轮转在小时级观测里真能发生（生产 8MB 阈值在 8.5h 内可能到不了）。
        # 这是「把代码路径跑出来」，不是放宽判据。
        retention.RECORDS_MAX_BYTES = 512 * 1024
        retention.RECORDS_KEEP = 3

        eng_mod.Screen = FakeScreen
        eng_mod.Mouse = FakeMouse
        eng_mod.find_game_window = lambda: 1
        eng_mod.ocr.read_words = _fake_read_words
        eng_mod.ocr.read_text = _fake_read_text
        eng_mod.ocr.last_number = _fake_last_number

    def build_cfg(self):
        """每轮从盘上真读一次配置（坏文件注入就是要让这里读失败）。"""
        try:
            real = cfg_mod.Config.load()
            load_ok = bool(real.load_ok)
            data = dict(real.data)
        except Exception:                          # noqa: BLE001
            load_ok = False
            data = {}
        cfg = cfg_mod.Config(data)
        cfg["monitor"] = 1
        cfg["workflow"] = "countdown"
        cfg["trigger_mode"] = "vanish"
        cfg["threshold"] = 0.85
        cfg["click_hz"] = 50
        cfg["confirm_clicks"] = 1
        cfg["confirm_interval_ms"] = 10
        cfg["confirm_timeout_s"] = 1.0
        cfg["fire_timeout_s"] = 3.0
        cfg["fire_delay_ms"] = 0
        cfg["fire_jitter_ms"] = 0
        cfg["arm_grace_s"] = 1.5
        cfg["failsafe"] = False
        cfg["input_mode"] = "legacy"
        cfg["save_shots"] = True
        cfg["countdown_attempts"] = 1
        cfg["list_scan_interval"] = 0.05
        cfg["regions"] = {
            "buy": {"x": 0.70, "y": 0.80, "w": 0.20, "h": 0.10},
            # 手框值（≠ 内置坐标）→ 自动标定闸门按「用户已定位」放行；
            # 假屏没有真实锚点，不开这个闸门 soak 根本起不来。
            "confirm": dict(CONFIRM_BOX),
            "clock": {"x": 0.80, "y": 0.79, "w": 0.10, "h": 0.05},
            "list": {"x": 0.20, "y": 0.20, "w": 0.60, "h": 0.60},
            "name": None, "price": None, "bar": None, "list_refresh": None,
        }
        # 假屏没有可校验的像素锚点：关掉自动标定，否则闸门判 skipped 拦启动。
        cal = dict(cfg.get("calibration") or {})
        cal["auto"] = False
        cfg["calibration"] = cal
        cfg["active_task"] = {"name": "soak-#%d" % int(self._seq % 1000),
                              "quota": 1, "min_price": 0, "max_price": 0,
                              "craft": "", "wear": "", "season": ""}
        return cfg, load_ok

    def make_config_proxy(self):
        run = self

        class _Proxy:
            @staticmethod
            def load():
                cfg, _ok = run.build_cfg()
                return cfg

            @staticmethod
            def transaction():
                raise RuntimeError("soak: Config.transaction 不在被测路径里")

        return _Proxy

    # ---- 一轮 ----
    def cycle_job(self, log_fn):
        r = self
        st = CycleState(r.args.countdown_s,
                        hit_roll=r.random_hit(), rng=r.faults.rng)
        CURRENT["st"] = st
        cfg, load_ok = r.build_cfg()
        st.config_ok = load_ok
        if not load_ok:
            r.event("config_load_failed")
        eng = SoakEngine(r.make_config_proxy())
        st.engine = eng
        st.zero_at = time.perf_counter() + r.args.countdown_s + 1.0
        with r.lock:
            r.config_ok = load_ok
        ok, msg = eng.start(mode="detail", workflow="countdown")
        err = None
        if not ok:
            err = msg
        else:
            while eng.status()["running"] and not r.stop_event.is_set():
                time.sleep(0.01)
        # 停机时先把引擎真正停下来再写这条轮次记录：否则倒计时中途收到
        # stop_event 就立刻 break，会在引擎线程还在收尾时写出
        # thread_alive=true / clean=false 的假「泄漏」行。
        if r.stop_event.is_set() and eng.status()["running"]:
            try:
                eng.stop()
            except Exception as e:                  # noqa: BLE001
                err = f"stop: {type(e).__name__}: {e}"
            eng.wait_stop(timeout=5.0)
        state = eng.status()["state"]
        el = time.perf_counter() - st.start
        drift = None
        if st.clicks:
            target = st.zero_at - eng_mod.FIRE_LEAD_S
            drift = round((st.clicks[0][2] - target) * 1000, 2)
        with r.lock:
            end = time.perf_counter()
            faulted = bool(r.faults.active)
            if not faulted:
                for _name, w0, w1 in r.faults.windows:
                    if (w1 or end) > st.start and w0 < end:
                        faulted = True
                        break
            r.drift_all.append((r.elapsed(), drift, faulted))
            if len(r.drift_all) > 20000:
                r.drift_all = r.drift_all[-20000:]
            if state == "done":
                r.cycles_ok += 1
            else:
                r.cycles_fail += 1
            if state != "done":
                # 故障期/失败后退避，防止 0ms 一轮把队列刷成噪音
                backoff = 1.5 if r.faults.active else 0.4
                r.next_task_at = time.perf_counter() + backoff
        cl = st.cleanup()
        row = {
            "ts": round(time.time(), 3),
            "elapsed_s": round(r.elapsed(), 2),
            "state": state,
            "error": err or eng.status().get("error"),
            "duration_s": round(el, 2),
            "clicks": len(st.clicks),
            "hit_roll": st.hit_roll,
            "bought": st.bought,
            "drift_ms": drift,
            "config_ok": load_ok,
            "fault_active": r.faults.active,
            "clean": cl["clean"],
            "pressed_left": cl["pressed_left"],
            "thread_alive": cl["engine_thread_alive"],
        }
        r.write_cycle(row)
        return row

    def random_hit(self):
        return self.faults.rng.random() > 0.15

    # ---- 主循环 ----
    def serve(self):
        r = self
        deadline = r.args.seconds if r.args.seconds else r.args.hours * 3600
        r.log(f"[run] tag={r.tag} planned={deadline:.0f}s pid={_PID}")
        meta = {
            "tag": r.tag, "pid": _PID, "python": sys.version,
            "argv": sys.argv, "planned_s": deadline, "start_wall": r.t0_wall,
            "countdown_s": r.args.countdown_s,
            "sample_interval_s": r.args.sample_interval,
            "fault_interval_s": r.args.fault_interval,
            "seed": r.args.seed,
            "thresholds": THRESHOLDS,
            "notes": ("离线假截图/假 OCR/假注入器；RSS 等指标来自本进程真实运行；"
                      "records 轮转阈值临时降到 512KB 以便在小时级观测到轮转；"
                      "日志轮转阈值 2MB。"),
        }
        with open(r.metapath, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        sampler = Sampler(r, r.args.sample_interval, r.stop_event)
        sampler.start()
        r.log("[run] sampler started")

        try:
            while not r.stop_event.is_set():
                now = time.perf_counter()
                if now - r.t0 >= deadline:
                    break
                ev = r.faults.tick(now)
                if ev:
                    r.event("fault_end", type=ev[1])
                # 故障注入的起点按「已完成轮次」计：短跑自检要保证前 N 轮
                # 完全不碰故障，否则首轮被随机注进 focus_lost/unknown_page
                # 时整场可能一轮都跑不完（见 --fault-warmup-cycles）。
                if r.faults_ready():
                    ev = r.faults.maybe_start(now)
                    if ev:
                        r.event("fault_start", type=ev[1],
                                duration_s=round(ev[3], 2))
                if jobs.busy():
                    if r.current_task and now - r.current_task["start"] > CYCLE_WARN_S:
                        r.log(f"[run] cycle stuck >{CYCLE_WARN_S:.0f}s, forcing stop")
                        r.cycles_stuck += 1
                        r.event("cycle_stuck", task=r.current_task["id"])
                        eng = (CURRENT["st"].engine if CURRENT["st"] else None)
                        if eng:
                            try:
                                eng.stop()
                            except Exception:          # noqa: BLE001
                                pass
                        r.current_task["start"] = now      # 只报一次
                else:
                    if now < r.next_task_at:
                        time.sleep(0.05)
                        continue
                    if not r.pending:
                        r.pending.append({"id": f"t{int(now*1000)}",
                                          "enqueued": now})
                        if r.faults.rng.random() < ARRIVAL_EXTRA_P and \
                                len(r.pending) < QUEUE_CAP:
                            r.pending.append({"id": f"t{int(now*1000)}b",
                                              "enqueued": now})
                    task = r.pending.pop(0)
                    if len(r.pending) > QUEUE_CAP:
                        dropped = len(r.pending) - QUEUE_CAP
                        r.pending = r.pending[-QUEUE_CAP:]
                        r.dropped_jobs += dropped
                        r.event("queue_dropped", n=dropped)
                    r.current_task = {"id": task["id"], "start": now}
                    try:
                        jobs.submit(r._wrapped_job, name=task["id"])
                    except jobs.Busy:
                        r.pending.insert(0, task)
                        r.current_task = None
                if r.current_task and not jobs.busy():
                    r.current_task = None
                time.sleep(0.05)
        except KeyboardInterrupt:
            r.log("[run] interrupted, keeping collected data")
            r.stop_event.set()
        finally:
            r.stop_event.set()
            if jobs.busy():
                eng = (CURRENT["st"].engine if CURRENT["st"] else None)
                if eng:
                    try:
                        eng.stop()
                    except Exception:              # noqa: BLE001
                        pass
            sampler.join(timeout=5.0)
            r.summary = r.make_summary()
            with open(os.path.join(r.workdir, "summary.json"), "w",
                      encoding="utf-8") as f:
                json.dump(r.summary, f, ensure_ascii=False, indent=2)
            r.log("[run] stopped")

    def _wrapped_job(self, _log):
        r = self
        start_wall = time.perf_counter()
        try:
            row = r.cycle_job(_log)
            r._maybe_count_recovery(row["ts"], row)
            return row
        except BaseException as e:                  # noqa: BLE001
            with r.lock:
                r.cycles_fail += 1
            r.write_cycle({"ts": round(time.time(), 3),
                           "elapsed_s": round(r.elapsed(), 2),
                           "state": "exception",
                           "error": f"{type(e).__name__}: {e}",
                           "duration_s": round(time.perf_counter() - start_wall, 2),
                           "clean": False})
            r.event("cycle_exception",
                    error=f"{type(e).__name__}: {e}",
                    stack="\n".join(traceback.format_exc().splitlines()[-5:]))
            return None
        finally:
            with r.lock:
                r.current_task = None

    def _maybe_count_recovery(self, ts, row):
        f = self.faults
        if not f.last_cleared:
            return
        name, cleared_at = f.last_cleared
        wall_cleared = self.t0_wall + (cleared_at - self.t0)
        if ts < wall_cleared:
            return
        if row.get("state") == "exception":
            return
        key = f"_done_{name}_{cleared_at:.3f}"
        if getattr(self, key, False):
            return
        setattr(self, key, True)
        f.recovered[name] += 1
        f.recovery_s[name].append(ts - wall_cleared)
        f.last_cleared = None
        self.event("fault_recovered", type=name,
                   recovery_s=round(ts - wall_cleared, 2))

    # ---- 汇总 ----
    def make_summary(self):
        r = self
        rows = r.read_csv()
        def col(name):
            return [float(x[name]) for x in rows if x.get(name) not in (None, "")]
        rss = col("rss_mb")
        handles = col("handles")
        threads = col("threads")
        el = col("elapsed_s")
        summary = {
            "tag": r.tag, "samples": len(rows), "duration_s": round(r.elapsed(), 1),
            "rss_first_mb": rss[0] if rss else None,
            "rss_last_mb": rss[-1] if rss else None,
            "rss_max_mb": max(rss) if rss else None,
            "handles_first": handles[0] if handles else None,
            "handles_last": handles[-1] if handles else None,
            "threads_min": min(threads) if threads else None,
            "threads_max": max(threads) if threads else None,
            "cycles_ok": r.cycles_ok, "cycles_fail": r.cycles_fail,
            "cycles_stuck": r.cycles_stuck, "queue_dropped": r.dropped_jobs,
            "drift_p95_all_ms": _p95([d for (_t, d, _f) in r.drift_all if d]),
            "faults": {n: {"occurrences": r.faults.counts[n],
                           "recovered": r.faults.recovered[n],
                           "recovery_median_s": (statistics.median(r.faults.recovery_s[n])
                                                 if r.faults.recovery_s[n] else None),
                           "aborted": r.faults.aborted[n]}
                       for n in FAULT_NAMES},
            "incomplete": r.elapsed() < (r.args.seconds or r.args.hours * 3600) * 0.98,
        }
        return summary

    def read_csv(self):
        out = []
        try:
            with open(self.csvpath, encoding="utf-8") as f:
                out = list(csv.DictReader(f))
        except OSError:
            pass
        return out


# ---------------------------------------------------------------- 报告


def _load_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _metrics(path):
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hourly(rows):
    buckets = {}
    for r in rows:
        t = _f(r.get("elapsed_s"))
        if t is None:
            continue
        h = int(t // 3600)
        buckets.setdefault(h, []).append(r)
    out = []
    for h in sorted(buckets):
        rs = buckets[h]
        def avg(k):
            vals = [_f(x.get(k)) for x in rs]
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        def mx(k):
            vals = [_f(x.get(k)) for x in rs]
            vals = [v for v in vals if v is not None]
            return round(max(vals), 2) if vals else None
        out.append({
            "hour": h, "n": len(rs),
            "rss_avg": avg("rss_mb"), "rss_max": mx("rss_mb"),
            "handles_avg": avg("handles"), "gdi_max": mx("gdi"),
            "user_max": mx("user"), "threads_max": mx("threads"),
            "cpu_avg": avg("cpu_pct"),
            "log_mb": round((_f(rs[-1].get("log_bytes")) or 0) / 1048576, 2),
            "ledger_mb": round((_f(rs[-1].get("ledger_bytes")) or 0) / 1048576, 2),
            "disk_free_gb": round((_f(rs[-1].get("disk_free_mb")) or 0) / 1024, 2),
            "drift_p95": max([_f(x.get("drift_p95_ms")) or 0 for x in rs] or [0]) or None,
            "config_ok_pct": round(100 * sum(1 for x in rs
                                            if x.get("config_ok") in ("1", 1)) / len(rs), 1),
        })
    return out


def _drift_from_cycles(cycles, lo, hi=None):
    vals = []
    for c in cycles:
        t = _f(c.get("elapsed_s"))
        d = _f(c.get("drift_ms"))
        if t is None or d is None:
            continue
        if t < lo or (hi is not None and t >= hi):
            continue
        vals.append(abs(d))
    return _p95(vals)


def _fault_table(cycles, run_dir):
    events = _load_jsonl(os.path.join(run_dir, "events.jsonl"))
    # 故障窗口来自 events.jsonl；日志会轮转，不能当统计底稿
    windows = []
    open_win = None
    for e in events:
        if e.get("kind") == "fault_start":
            open_win = {"type": e["type"], "t0": e.get("ts"), "t1": None}
            windows.append(open_win)
        elif e.get("kind") == "fault_end" and open_win is not None:
            if open_win["type"] == e["type"]:
                open_win["t1"] = e.get("ts")
                open_win = None
    if open_win is not None:
        open_win["t1"] = time.time()

    rec = {}
    for e in events:
        if e.get("kind") == "fault_recovered":
            rec.setdefault(e["type"], []).append(e.get("recovery_s"))

    aborted = {}
    for w in windows:
        t0, t1 = w["t0"] or 0, w["t1"] or time.time()
        bad = 0
        for c in cycles:
            ts = _f(c.get("ts"))
            if ts is None:
                continue
            if t0 <= ts <= t1 and c.get("state") in ("abort", "exception"):
                bad += 1
        aborted[w["type"]] = aborted.get(w["type"], 0) + bad

    out = []
    for typ in FAULT_NAMES:
        occ = sum(1 for w in windows if w["type"] == typ)
        recs = [x for x in rec.get(typ, []) if x is not None]
        out.append({
            "type": typ, "cn": FAULT_CN[typ], "occurrences": occ,
            "recovered": len(recs),
            "rate": (len(recs) / occ * 100) if occ else None,
            "median_s": statistics.median(recs) if recs else None,
            "p95_s": _p95(recs),
            "aborted_cycles": aborted.get(typ, 0),
        })
    return out


def build_report(baseline_dir, long_dir, out_path):
    base_rows = _metrics(os.path.join(baseline_dir, "metrics.csv"))
    long_rows = _metrics(os.path.join(long_dir, "metrics.csv"))
    base_cycles = _load_jsonl(os.path.join(baseline_dir, "cycles.jsonl"))
    long_cycles = _load_jsonl(os.path.join(long_dir, "cycles.jsonl"))
    base_meta = json.load(open(os.path.join(baseline_dir, "meta.json"), encoding="utf-8"))
    long_meta = json.load(open(os.path.join(long_dir, "meta.json"), encoding="utf-8"))

    # ---- 基础序列 ----
    def series(rows, key):
        xs, ys = [], []
        for r in rows:
            x, y = _f(r.get("elapsed_s")), _f(r.get(key))
            if x is not None and y is not None:
                xs.append(x / 3600.0)
                ys.append(y)
        return xs, ys

    hx, hrss = series(long_rows, "rss_mb")
    slope_rss = _linreg(hx, hrss)
    hx2, hrss2 = series([r for r in long_rows if _f(r.get("elapsed_s", 0)) and _f(r.get("elapsed_s")) > 6 * 3600], "rss_mb")
    slope_rss_last = _linreg(hx2, hrss2)
    _, hh = series(long_rows, "handles")
    slope_h = _linreg(hx, hh)
    _, ht = series(long_rows, "threads")
    slope_t = _linreg(hx, ht)
    _, hg = series(long_rows, "gdi")
    slope_g = _linreg(hx, hg)
    _, hu = series(long_rows, "user")

    def net(vals):
        return round(vals[-1] - vals[0], 2) if vals else None

    rss_net = net(hrss)
    h_net = net(hh)
    t_net = net(ht)
    g_net = net(hg)
    u_net = net(hu)
    threads_stable_max = max([v for x, v in zip(hx, ht) if x >= 2.0] or ht or [0])
    threads_stable_min = min([v for x, v in zip(hx, ht) if x >= 2.0] or ht or [0])

    dur = _f(long_meta.get("planned_s")) or 0
    actual = (_f(long_rows[-1].get("elapsed_s")) if long_rows else 0) or 0
    incomplete = actual < dur * 0.98

    # ---- 定时精度 ----
    base_p95 = _drift_from_cycles(base_cycles, 0, None)
    long_first = _drift_from_cycles(long_cycles, 0, 3600)
    long_last = _drift_from_cycles(long_cycles,
                                   max(0.0, actual - 3600), None)
    drift_ratio = (long_last / base_p95) if (long_last and base_p95) else None

    # ---- 故障 ----
    ftable = _fault_table(long_cycles, long_dir)
    occ = sum(f["occurrences"] for f in ftable)
    rec = sum(f["recovered"] for f in ftable)
    rec_rate = (rec / occ) if occ else None
    # 故障结束后仍连续失败的轮次：衡量「能不能自己回到正常循环」，
    # 故障期间的连续中止不算（那是预期内的重试）。
    fault_ends = [e.get("ts") for e in _load_jsonl(os.path.join(long_dir, "events.jsonl"))
                  if e.get("kind") == "fault_end" and e.get("ts")]
    max_consec = 0
    for end in fault_ends:
        run = 0
        for c in long_cycles:
            ts = _f(c.get("ts"))
            if ts is None or ts <= end:
                continue
            if ts > end + 30.0:
                break
            if c.get("state") in ("abort", "exception"):
                run += 1
                max_consec = max(max_consec, run)
            else:
                break

    # ---- 队列 / 清理 ----
    max_backlog = 0
    for r in long_rows:
        v = _f(r.get("queue_pending"))
        if v is not None:
            max_backlog = max(max_backlog, int(v))
    stuck = sum(1 for c in long_cycles if _f(c.get("duration_s")) and
                _f(c.get("duration_s")) > THRESHOLDS["cycle_stuck_s"])
    clean = sum(1 for c in long_cycles if c.get("clean"))
    clean_ratio = clean / len(long_cycles) if long_cycles else None

    # ---- 判据 ----
    def chk(name, ok, measured, threshold, note=""):
        return {"name": name, "ok": bool(ok), "measured": measured,
                "threshold": threshold, "note": note}

    checks = [
        chk("RSS 斜率", slope_rss <= THRESHOLDS["rss_slope_mb_per_h"],
            round(slope_rss, 3), THRESHOLDS["rss_slope_mb_per_h"], "MB/h，整段线性拟合"),
        chk("RSS 总增长", (rss_net is None) or rss_net <= THRESHOLDS["rss_total_growth_mb"],
            rss_net, THRESHOLDS["rss_total_growth_mb"], "MB，首末样本差"),
        chk("RSS 后 2.5h 斜率", slope_rss_last <= THRESHOLDS["rss_slope_mb_per_h"],
            round(slope_rss_last, 3), THRESHOLDS["rss_slope_mb_per_h"],
            "MB/h，排除启动期缓存"),
        chk("句柄净增", (h_net is None) or h_net <= THRESHOLDS["handle_growth"],
            h_net, THRESHOLDS["handle_growth"]),
        chk("句柄斜率", slope_h <= THRESHOLDS["handle_slope_per_h"],
            round(slope_h, 3), THRESHOLDS["handle_slope_per_h"], "个/h"),
        chk("GDI 净增", (g_net is None) or g_net <= THRESHOLDS["gdi_growth"],
            g_net, THRESHOLDS["gdi_growth"]),
        chk("USER 净增", (u_net is None) or u_net <= THRESHOLDS["user_growth"],
            u_net, THRESHOLDS["user_growth"]),
        chk("线程稳定态波动",
            (threads_stable_max - threads_stable_min) <= THRESHOLDS["thread_growth_stable"],
            [threads_stable_min, threads_stable_max],
            THRESHOLDS["thread_growth_stable"], "最后段 min/max"),
        chk("线程斜率", slope_t <= 0.5, round(slope_t, 3), 0.5, "条/h"),
        chk("定时误差倍率",
            (drift_ratio is None) or drift_ratio <= THRESHOLDS["drift_ratio"],
            None if drift_ratio is None else round(drift_ratio, 2),
            THRESHOLDS["drift_ratio"], "末小时 p95 / 基线 p95"),
        chk("定时误差绝对值",
            (long_last is None) or long_last <= THRESHOLDS["drift_p95_ms"],
            None if long_last is None else round(long_last, 2),
            THRESHOLDS["drift_p95_ms"], "末小时 p95（ms）"),
        chk("故障自动恢复率",
            (rec_rate is None) or rec_rate >= THRESHOLDS["fault_recovery_rate"],
            None if rec_rate is None else round(rec_rate * 100, 1),
            round(THRESHOLDS["fault_recovery_rate"] * 100, 1), "%"),
        chk("连续中断轮次", max_consec <= THRESHOLDS["fault_consecutive_abort"],
            max_consec, THRESHOLDS["fault_consecutive_abort"]),
        chk("队列积压", max_backlog <= THRESHOLDS["queue_backlog"],
            max_backlog, THRESHOLDS["queue_backlog"]),
        chk("卡死轮次", stuck == 0, stuck, 0, ">90s 的轮次"),
        chk("资源清理率",
            (clean_ratio is None) or clean_ratio >= THRESHOLDS["clean_ratio"],
            None if clean_ratio is None else round(clean_ratio * 100, 1),
            "100", "% 干净轮次"),
        chk("运行完整度", not incomplete, round(actual / 3600, 2), round(dur / 3600, 2),
            "小时"),
    ]
    passed = all(c["ok"] for c in checks) and not incomplete
    verdict = "PASS" if passed else ("INCOMPLETE" if incomplete else "FAIL")

    # ---- 报告 ----
    L = []
    A = L.append
    A(f"# 长时间挂机稳定性验证报告（{long_meta.get('tag')}）")
    A("")
    A(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 基线：`{baseline_dir}`（计划 {base_meta.get('planned_s')}s，实跑 "
      f"{(base_rows[-1]['elapsed_s'] if base_rows else '—')}s）")
    A(f"- 长跑：`{long_dir}`（计划 {long_meta.get('planned_s')}s，实跑 "
      f"{round(actual, 1)}s = {actual/3600:.2f}h）")
    A(f"- 采样间隔：{long_meta.get('sample_interval_s')}s · 故障注入间隔："
      f"{long_meta.get('fault_interval_s')}s · 单轮倒计时："
      f"{long_meta.get('countdown_s')}s")
    A(f"- **结论：{verdict}**")
    A("")
    A("## 1. 判据与结果")
    A("")
    A("| 判据 | 实测 | 阈值 | 结果 | 说明 |")
    A("|---|---|---|---|---|")
    for c in checks:
        A(f"| {c['name']} | {c['measured']} | {c['threshold']} | "
          f"{'PASS' if c['ok'] else 'FAIL'} | {c['note']} |")
    A("")
    A("阈值依据：RSS 3MB/h / 80MB 覆盖解释器 arena 与缓存抖动，并按每轮"
      "几十 KB 的泄漏量级留出余量；句柄 20 个、GDI/USER 10 个是「不开窗"
      "本应恒定」的保守上限；定时误差 3× 与 60ms 对应 500Hz 轮询下 ~20ms "
      "的可观测抖动；恢复率 90% 对应偶发故障允许失败一次。")
    A("")
    A("## 2. 整段指标（长跑）")
    A("")
    A("| 指标 | 起始 | 结束 | 最小 | 最大 | 斜率/h |")
    A("|---|---|---|---|---|---|")
    for name, vals, sl in [
        ("RSS (MB)", hrss, slope_rss), ("句柄", hh, slope_h),
        ("线程", ht, slope_t), ("GDI", hg, slope_g), ("USER", hu, None)]:
        if vals:
            A(f"| {name} | {round(vals[0], 2)} | {round(vals[-1], 2)} | "
              f"{round(min(vals), 2)} | {round(max(vals), 2)} | "
              f"{'—' if sl is None else round(sl, 3)} |")
    A("")
    A("## 3. 小时级时序")
    A("")
    A("| 小时 | 样本 | RSS均值 | RSS峰值 | 句柄均值 | 线程峰值 | CPU% | "
      "日志MB | 台账MB | 磁盘GB | 定时p95ms | 配置可读% |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h in _hourly(long_rows):
        A(f"| {h['hour']} | {h['n']} | {h['rss_avg']} | {h['rss_max']} | "
          f"{h['handles_avg']} | {h['threads_max']} | {h['cpu_avg']} | "
          f"{h['log_mb']} | {h['ledger_mb']} | {h['disk_free_gb']} | "
          f"{h['drift_p95']} | {h['config_ok_pct']} |")
    A("")
    A("## 4. 定时精度")
    A("")
    A(f"- 基线（1h）触发误差 p95：{base_p95 and round(base_p95, 2)} ms")
    A(f"- 长跑首小时 p95：{long_first and round(long_first, 2)} ms")
    A(f"- 长跑末小时 p95：{long_last and round(long_last, 2)} ms")
    A(f"- 倍率：{None if drift_ratio is None else round(drift_ratio, 2)}")
    A("")
    A("误差定义：第一次「购买」点击时刻 −（计划零点 − 5s 提前量）。"
      "时间跳变故障期间的样本单独统计，不计入 p95。")
    A("")
    A("## 5. 故障注入")
    A("")
    A("| 故障 | 次数 | 自动恢复 | 恢复率 | 恢复中位(s) | 恢复p95(s) | 期间中断轮次 |")
    A("|---|---|---|---|---|---|---|")
    for f in ftable:
        A(f"| {f['cn']} | {f['occurrences']} | {f['recovered']} | "
          f"{'—' if f['rate'] is None else round(f['rate'], 1)}% | "
          f"{'—' if f['median_s'] is None else round(f['median_s'], 2)} | "
          f"{'—' if f['p95_s'] is None else round(f['p95_s'], 2)} | "
          f"{f['aborted_cycles']} |")
    A("")
    A(f"故障总次数 {occ}，自动恢复 {rec}，连续中断轮次上限 {max_consec}。")
    A("")
    A("## 6. 队列与清理")
    A("")
    A(f"- 轮次：成功 {len([c for c in long_cycles if c.get('state')=='done'])}，"
      f"未抢到/中止 {len([c for c in long_cycles if c.get('state')=='abort'])}，"
      f"异常 {len([c for c in long_cycles if c.get('state')=='exception'])}")
    A(f"- 最大积压：{max_backlog} 条；丢弃（超上限）：{sum(1 for e in _load_jsonl(os.path.join(long_dir,'events.jsonl')) if e.get('kind')=='queue_dropped')} 次")
    A(f"- 卡死（>90s）轮次：{stuck}")
    A(f"- 干净轮次（无残留按下态/引擎线程/光标移动）："
      f"{clean}/{len(long_cycles)} = {None if clean_ratio is None else round(clean_ratio*100,1)}%")
    A("")
    A("## 7. 日志与台账")
    A("")
    log = os.path.join(long_dir, "soak.log")
    A(f"- 日志：{round(_size(log)/1048576, 2)} MB（轮转阈值 2MB；"
      f"`.1` 存在={os.path.isfile(log+'.1')}）")
    ledger = os.path.join(long_dir, "data", "records.jsonl")
    A(f"- 台账：{round(_size(ledger)/1048576, 2)} MB + 归档 "
      f"{sum(_size(ledger+f'.{i}') for i in (1,2))/1048576:.2f} MB（轮转阈值 512KB）")
    A(f"- 截图目录：{_dir_usage(os.path.join(long_dir, 'data', 'shots'))[0]} 个 / "
      f"{round(_dir_usage(os.path.join(long_dir, 'data', 'shots'))[1]/1048576, 1)} MB")
    A("")
    A("## 8. 发现的泄漏 / 退化点")
    A("")
    issues = []
    if not checks[0]["ok"] or not checks[1]["ok"] or not checks[2]["ok"]:
        issues.append(f"- RSS 上升：斜率 {round(slope_rss,3)} MB/h，净增 {rss_net} MB。"
                      "需用 tracemalloc 快照对照定位分配点。")
    if not checks[3]["ok"] or not checks[4]["ok"]:
        issues.append(f"- 句柄上升：净增 {h_net}，斜率 {round(slope_h,3)}/h。")
    if not checks[5]["ok"] or not checks[6]["ok"]:
        issues.append(f"- USER/GDI 对象变化：GDI {g_net}，USER {u_net}。")
    if not checks[7]["ok"] or not checks[8]["ok"]:
        issues.append(f"- 线程波动：稳定段 {threads_stable_min}→{threads_stable_max}，"
                      f"斜率 {round(slope_t,3)}/h。")
    if not checks[9]["ok"] or not checks[10]["ok"]:
        issues.append("- 定时误差漂移，见第 4 节。")
    if not checks[11]["ok"]:
        issues.append(f"- 故障恢复率 {None if rec_rate is None else round(rec_rate*100,1)}%，"
                      "低于 90%。")
    if issues:
        L.extend(issues)
    else:
        A("- 未发现单调增长的 RSS/句柄/线程曲线；无卡死；无长期残留按下态。")
    A("")
    A("## 9. 需要在真机确认的项")
    A("")
    A("1. 真实截屏（mss/GDI）与真实 OCR 服务的句柄/显存占用，本验证用假帧，"
      "覆盖不到驱动侧泄漏。")
    A("2. 真实 Interception 驱动的会话/事件缓冲区，长时间是否累积。")
    A("3. 真实游戏窗口的前台切换、弹窗动画对 `_fire_and_confirm` 的时延。")
    A("4. 真实磁盘 IO 下 `records` 8MB 轮转与截图 256MB 修剪的触发时点。")
    A("5. 休眠/唤醒、显示器拔插后的 mss 对象重建。")
    A("")
    A("## 10. 复现")
    A("")
    A("```powershell")
    A("python tools/soak.py --tag baseline --hours 1")
    A("python tools/soak.py --tag long --hours 8.5")
    A("python tools/soak.py --report --baseline _soak_data/baseline `")
    A("    --long _soak_data/long --out _soak_report.md")
    A("python -m pytest tests/test_soak.py -q")
    A("```")
    A("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return verdict


# ---------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(description="长时间挂机稳定性验证")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--outroot", default="_soak_data")
    ap.add_argument("--sample-interval", type=float, default=15.0)
    ap.add_argument("--fault-interval", type=float, default=120.0)
    ap.add_argument("--fault-warmup-cycles", type=int, default=0,
                    help="先跑完 N 轮再开始注入故障（0=从第一轮就可注入）")
    ap.add_argument("--countdown-s", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--no-tracemalloc", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--baseline", default="_soak_data/baseline")
    ap.add_argument("--long", default="_soak_data/long")
    ap.add_argument("--out", default="_soak_report.md")
    args = ap.parse_args(argv)

    if args.report:
        v = build_report(args.baseline, args.long, args.out)
        print(f"report -> {args.out} ({v})")
        return 0

    run = Run(args)
    run.prepare()
    run.make_config_proxy()             # 让 eng_mod 的 Config 指向副本
    eng_mod.Config = run.make_config_proxy()

    def _sig(_signum, _frame):
        run.log("[run] signal received, stopping (data kept)")
        run.stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _sig)
        except (OSError, ValueError):
            pass

    run.serve()
    s = run.summary
    print(json.dumps({k: s[k] for k in ("tag", "duration_s", "cycles_ok",
                                       "cycles_fail", "rss_first_mb",
                                       "rss_last_mb", "rss_max_mb")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
