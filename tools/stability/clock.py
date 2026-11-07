"""可注入的假时钟。

为什么不能直接 `time.sleep` 加速：引擎里 `time.time()` / `time.perf_counter()`
`time.sleep()` 混着用，只把 sleep 变短会让倒计时「看起来还剩 60 秒，实际
只过了 0.06 秒」，逻辑立刻错乱。所以整块时间源一起换掉：

    clock = VirtualClock(sleep_step=0.1)
    with install([engine, countdown, records, pagewatch], clock):
        ...跑仿真...

`install` 把模块里的 `time` 名字换成一个代理对象：`time()` / `perf_counter()`
`monotonic()` / `sleep()` / `strftime()` 走虚拟钟，其余属性（`time.clock_gettime`
之类）转发给真 time。退出时原样还原。

`sleep_step` 是压缩比：每次 `sleep(dt)` 至少推进 `max(dt, sleep_step)` 虚拟秒。
引擎内层循环 sleep 0.002，step=0.1 时每个循环推进 0.1 虚拟秒 —— 60 秒
倒计时 600 次迭代而不是 3 万次，72 小时等效时长才跑得完。
"""
import threading
import time as _real_time


class VirtualClock:
    def __init__(self, start=1_700_000_000.0, sleep_step=0.1):
        self._lock = threading.RLock()
        self._t = float(start)          # 墙钟（epoch 秒）—— records/日志用
        self._pc = 0.0                  # 单调钟（秒）—— countdown/引擎用
        self.sleep_step = float(sleep_step)
        self.sleep_calls = 0
        self.advanced = 0.0

    # ---- 时间读取 ----
    def time(self):
        with self._lock:
            return self._t

    def perf_counter(self):
        with self._lock:
            return self._pc

    def monotonic(self):
        with self._lock:
            return self._pc

    def strftime(self, fmt, t=None):
        with self._lock:
            base = self._t if t is None else t
        return _real_time.strftime(fmt, _real_time.localtime(base))

    # ---- 推进 ----
    def advance(self, dt):
        dt = float(dt)
        if dt <= 0:
            return
        with self._lock:
            self._t += dt
            self._pc += dt
            self.advanced += dt

    def sleep(self, seconds):
        """不真睡，只推进虚拟时间。"""
        seconds = float(seconds or 0)
        with self._lock:
            self.sleep_calls += 1
        self.advance(max(seconds, self.sleep_step))

    def elapsed(self):
        return self._pc


class _ClockProxy:
    """给模块用的 `time` 替身：认识的走虚拟钟，其余转发真 time。"""

    def __init__(self, clock):
        self._c = clock

    def time(self):
        return self._c.time()

    def perf_counter(self):
        return self._c.perf_counter()

    def monotonic(self):
        return self._c.monotonic()

    def sleep(self, seconds):
        self._c.sleep(seconds)

    def strftime(self, fmt, t=None):
        return self._c.strftime(fmt, t)

    def localtime(self, t=None):
        base = self._c.time() if t is None else t
        return _real_time.localtime(base)

    def __getattr__(self, name):
        return getattr(_real_time, name)


class install:
    """上下文管理器：把一组模块的 `time` 换成虚拟钟，退出还原。"""

    def __init__(self, modules, clock):
        self.clock = clock
        self.modules = [m for m in modules if m is not None]
        self._saved = []

    def __enter__(self):
        proxy = _ClockProxy(self.clock)
        for m in self.modules:
            self._saved.append((m, getattr(m, "time", None)))
            m.time = proxy
        return proxy

    def __exit__(self, *exc):
        for m, old in self._saved:
            m.time = old
        self._saved = []
        return False
