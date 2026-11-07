"""心跳与卡死检测。

为什么要有：抢购线程一旦卡在某个同步调用里（截图驱动卡住、OCR 服务端不收包、
注入 DLL 阻塞），`while not _stop.is_set()` 那套循环根本轮不到自己判断 ——
线程还「活着」，状态永远停在 WATCH，调度器看到 `running=True` 就一直等，
整条队列到天亮都没动静，而且没有任何人会发现。

这里只做两件事：
  · Heartbeat：干活的一方在关键循环里 beat()，监督方按「多久没跳」判卡死；
  · Watchdog：连续 N 次采样都超时就触发一次恢复回调（只在冷却期外触发）。

刻意不在这里做「杀线程」：Python 没有安全的杀线程手段，硬杀会把锁留在
半解锁状态。卡死只能靠**上层收掉资源 + 重开一轮**，所以回调由调度器给。
"""
import threading
import time


class Heartbeat:
    """一组命名心跳。时间源可注入（测试用假时钟）。"""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._beats = {}

    def beat(self, name="main", at=None):
        with self._lock:
            self._beats[name] = float(self._clock() if at is None else at)

    def age(self, name="main"):
        """距离上次心跳多少秒；从没跳过返回 None。"""
        with self._lock:
            last = self._beats.get(name)
            if last is None:
                return None
            return max(0.0, float(self._clock()) - last)

    def snapshot(self):
        with self._lock:
            now = float(self._clock())
            return {k: max(0.0, now - v) for k, v in self._beats.items()}

    def reset(self):
        with self._lock:
            self._beats.clear()


class Watchdog:
    """卡死判定：连续 confirm_times 次超时才认定卡死（防抖）。"""

    def __init__(self, heartbeat, limit_s=60.0, confirm_times=3,
                 cooldown_s=300.0, clock=None):
        self.hb = heartbeat
        self.limit_s = float(limit_s)
        self.confirm_times = max(1, int(confirm_times))
        self.cooldown_s = float(cooldown_s)
        self._clock = clock or time.monotonic
        self._strikes = 0
        self._last_fire = None
        self.fired = 0

    def poll(self, names=("main",), age_override=None):
        """返回 True 表示「确认卡死且允许触发」。

        没有心跳记录（还没开始 beat）不算卡死 —— 刚启动的几百毫秒里
        什么都没跳，不能因此重启一轮。
        """
        ages = []
        for n in names:
            a = age_override if age_override is not None else self.hb.age(n)
            if a is None:
                return False
            ages.append(a)
        if not ages or max(ages) < self.limit_s:
            self._strikes = 0
            return False
        self._strikes += 1
        if self._strikes < self.confirm_times:
            return False
        now = float(self._clock())
        if self._last_fire is not None and now - self._last_fire < self.cooldown_s:
            # 冷却期内不重复触发：卡死恢复本身要时间，连着重启只会把
            # 「已经抢到的收尾」反复打断。
            return False
        self._last_fire = now
        self.fired += 1
        self._strikes = 0
        return True

    def info(self):
        return {"limit_s": self.limit_s, "strikes": self._strikes,
                "fired": self.fired, "ages": self.hb.snapshot()}
