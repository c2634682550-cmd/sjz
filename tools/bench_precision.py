"""精确触发内核的压测台。

两种用法：

    # 分布 + 不同负载/自旋阈值/优先级对比
    .venv\\Scripts\\python.exe tools\\bench_precision.py --shots 200

    # 外部进程独立观测（第二套时钟 + 第二个进程，最硬的一路交叉验证）
    .venv\\Scripts\\python.exe tools\\bench_precision.py --external --shots 120

测量通道：

    M1 触发线程内 QPC 读数（权威值）
    M2 同一瞬间 GetSystemTimePreciseAsFileTime（另一套 API 读数一致性）
    M4 独立子进程忙等共享内存，看见触发标志后用自己的 FILETIME 打点
       —— 它同时带来 ~1us 级的进程间可见延迟，这个延迟逐枪测量并单列

为什么要外部进程：Python 线程受 GIL 切换间隔限制，同进程的外部线程最精
只能到毫秒；换一个进程忙等，才能把观测粒度压到微秒级。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import precision as P       # noqa: E402

FT_PER_S = 10_000_000
CREATE_NO_WINDOW = 0x08000000

# 共享内存槽位（字节偏移），q = int64 小端
OFF_SEQ, OFF_DEADLINE, OFF_FIRED, OFF_SEEN, OFF_DETECTED, OFF_STOP, OFF_READY = (
    0, 8, 16, 24, 32, 40, 48)
OBS_SIZE = 64

OBSERVER_SCRIPT = r'''
import ctypes, mmap, struct, sys
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]

# 本机经常同时跑别的测试/审计任务；普通优先级忙等会被饿到几十毫秒，
# 直接表现为「外部进程没看见本次触发」。观测器本来就是要拿到最硬的
# 外部视角，必须用高优先级把调度延迟压住，否则测的是机器负载不是精度。
# 只提进程优先级、不提线程优先级：线程提到 TIME_CRITICAL 会在触发线程
# 「读 FILETIME -> 写 fired」之间抢占它，把 handoff 自己撑大。
# GetCurrentProcess 的默认 restype 是 c_int，返回 -1 这个伪句柄，直接传给
# kernel32 即可，不要改成 HANDLE 否则会溢出。
HIGH_PRIORITY_CLASS = 0x00000080
k32.SetPriorityClass(k32.GetCurrentProcess(), HIGH_PRIORITY_CLASS)

def ft_now():
    f = wintypes.FILETIME()
    k32.GetSystemTimePreciseAsFileTime(ctypes.byref(f))
    return (f.dwHighDateTime << 32) | f.dwLowDateTime

path = sys.argv[1]
fh = open(path, "r+b")
mm = mmap.mmap(fh.fileno(), 64)
pack, unpack = struct.pack_into, struct.unpack_from
pack("<q", mm, 48, 1)                       # ready
seen = 0
while True:
    if unpack("<q", mm, 40)[0]:             # stop
        break
    seq = unpack("<q", mm, 0)[0]
    if seq != seen and unpack("<q", mm, 16)[0]:   # 新序号且 fired=1
        pack("<q", mm, 32, ft_now())        # 先写打点
        pack("<q", mm, 24, seq)             # 再写 seen：parent 看到 seen 时打点必已就绪
        seen = seq
'''


class ExternalObserver:
    """独立子进程观测器。

    协议：arm(deadline_ft) 写入目标时刻 -> 触发线程把 fired 置 1 ->
    子进程看见后写入自己的 FILETIME 打点 -> 父进程 wait_detect 取回。
    """

    def __init__(self, python=None, timeout_s=5.0):
        self.dir = tempfile.mkdtemp(prefix="prec_obs_")
        self.path = os.path.join(self.dir, "obs.bin")
        with open(self.path, "wb") as f:
            f.write(b"\0" * OBS_SIZE)
            f.flush()
        script = os.path.join(self.dir, "obs.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(OBSERVER_SCRIPT)
        self._fh = open(self.path, "r+b")
        self._mm = mmap.mmap(self._fh.fileno(), OBS_SIZE)
        self._seq = 0
        self._proc = subprocess.Popen(
            [python or sys.executable, script, self.path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        t_end = time.perf_counter() + timeout_s
        while struct.unpack_from("<q", self._mm, OFF_READY)[0] == 0:
            if time.perf_counter() > t_end:
                self.close()
                raise RuntimeError("外部观测进程没起来")
            time.sleep(0.0002)

    # ---- 协议

    def arm(self, deadline_ft):
        """宣告下一枪的目标时刻（FILETIME 轴）。返回序号。"""
        self._seq += 1
        mm = self._mm
        struct.pack_into("<q", mm, OFF_FIRED, 0)
        struct.pack_into("<q", mm, OFF_DEADLINE, int(deadline_ft))
        struct.pack_into("<q", mm, OFF_SEQ, self._seq)
        return self._seq

    def fire(self):
        """触发线程在动作里调用：把 fired 置 1（子进程看到就打点）。"""
        struct.pack_into("<q", self._mm, OFF_FIRED, 1)

    def wait_detect(self, seq, timeout_s=0.25):
        """等观测进程看见本枪。

        50ms 在满负载机器上会误报「没看见」——观测进程优先级再高也可能
        被短时抢占；这里的超时只是活性上限，不是精度阈值，打点值仍按
        真实 FILETIME 计算。
        """
        mm = self._mm
        t_end = time.perf_counter() + timeout_s
        while True:
            if struct.unpack_from("<q", mm, OFF_SEEN)[0] == seq:
                return struct.unpack_from("<q", mm, OFF_DETECTED)[0]
            if time.perf_counter() > t_end:
                return None

    def close(self):
        try:
            struct.pack_into("<q", self._mm, OFF_STOP, 1)
            self._proc.wait(2.0)
        except Exception:                                     # noqa: BLE001
            self._proc.kill()
        finally:
            try:
                self._mm.close()
                self._fh.close()
            except Exception:                                 # noqa: BLE001
                pass


# ---------------------------------------------------------------- 统计

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]


def ft_now():
    f = wintypes.FILETIME()
    k32.GetSystemTimePreciseAsFileTime(ctypes.byref(f))
    return (f.dwHighDateTime << 32) | f.dwLowDateTime


def anchor_bracket():
    """QPC 括取锚点：q1 -> FILETIME -> q2，取中点，压掉单边读间隔。"""
    q1 = P.now_ticks()
    f = ft_now()
    q2 = P.now_ticks()
    return (q1 + q2) // 2, f


def ft_of_ticks(ticks, anchor_q, anchor_ft):
    return anchor_ft + (ticks - anchor_q) * FT_PER_S // P.ticks_per_second()


def stats(values):
    s = sorted(values)
    n = len(s)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "p50": statistics_median(s),
        "p95": s[min(n - 1, int(round(0.95 * (n - 1))))],
        "max": s[-1],
        "mean": sum(s) / n,
        "std": (sum((v - sum(s) / n) ** 2 for v in s) / n) ** 0.5,
        "early": sum(1 for v in s if v < 0),
    }


def statistics_median(sorted_values):
    n = len(sorted_values)
    mid = n // 2
    return sorted_values[mid] if n % 2 else (sorted_values[mid - 1] + sorted_values[mid]) / 2


def fmt(name, st):
    if not st.get("n"):
        return f"{name:<22} n=0"
    return (f"{name:<22} n={st['n']:<3} p50={st['p50']:>8.2f} p95={st['p95']:>9.2f} "
            f"max={st['max']:>10.2f} std={st['std']:>8.2f} 早={st['early']}")


def histogram(values, bins):
    out = []
    for lo, hi in bins:
        c = sum(1 for v in values if lo <= v < hi)
        out.append((lo, hi, c))
    return out


# ---------------------------------------------------------------- 负载

class Load:
    """用 Python 线程占 CPU，模拟别的进程抢占（线程优先级普通）。"""

    def __init__(self, threads=0):
        self.stop = threading.Event()
        self.threads = []
        for _ in range(threads):
            t = threading.Thread(target=self._burn, daemon=True)
            t.start()
            self.threads.append(t)

    def _burn(self):
        while not self.stop.is_set():
            pass

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(0.2)


class ProcLoad:
    """用子进程占 CPU：模拟真·别的进程抢占（不涉及 GIL）。

    统一走 tools/load_gen.py —— 降优先级（不影响用户前台）、硬超时、父进程
    消失即自杀。不再裸起 `python -c "while True: pass"`（那是一批残留满核
    进程的来源）。duration 只是兜底，正常结束靠 close() 杀掉父进程，
    子 worker 会自行退出。
    """

    def __init__(self, procs, duration_s=180.0):
        script = os.path.join(ROOT, "tools", "load_gen.py")
        self.proc = subprocess.Popen(
            [sys.executable, script, "--workers", str(max(1, procs)),
             "--duration", f"{duration_s:g}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        time.sleep(0.3)

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(3.0)
        except Exception:                                     # noqa: BLE001
            self.proc.kill()


# ---------------------------------------------------------------- 实验

def run_shots(shots, spin_s=P.DEFAULT_SPIN_S, priority=P.THREAD_PRIORITY_TIME_CRITICAL,
              delay=(0.010, 0.030), external=None, switch_interval=None):
    """跑 N 枪，返回各通道的偏差（单位 us）。

    M0 = 回调入口 - deadline（抖动，即「目标时刻 -> 回调被调用」）
    M0T= 回调入口 - target（字面口径，含提前量）
    M1 = trigger - deadline（QPC）
    M2 = 同一处「FILETIME 读数 - 由 QPC 锚点映射出来的 FILETIME」= 读数路径一致性
    M4 = 外部进程看见的时刻 - 目标时刻（含进程间可见延迟 handoff）
    """
    ticks_per_us = P.ticks_per_second() / 1e6
    m0, m0t, m1, m2, m4, handoff = [], [], [], [], [], []
    uns = []
    old_interval = None
    if switch_interval:
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(switch_interval)

    try:
        for i in range(shots):
            anchor_q, anchor_ft = anchor_bracket()
            target = P.now_ticks() + P.seconds_to_ticks(
                delay[0] + (delay[1] - delay[0]) * (i % 97) / 96.0)
            samples = []
            ft_at_fire = []
            seq = None
            if external is not None:
                seq = external.arm(ft_of_ticks(target, anchor_q, anchor_ft))

            def action():
                q = P.now_ticks()
                f = ft_now()
                samples.append((q, f))
                if external is not None:
                    ft_at_fire.append(f)
                    external.fire()

            shot = P.fire_at(target, action, spin_s=spin_s, priority=priority)
            if not shot.wait(1.0) or not shot.fired or shot.error:
                uns.append(i)
                continue
            q_act, f_act = samples[0]
            m0.append(shot.call_latency_us)
            m0t.append(shot.call_latency_from_target_us)
            m1.append(shot.deviation_us)
            # 两种 API 在同一瞬间的读数一致性：正常应在 0.2~1us（读间隔）
            m2.append((f_act - ft_of_ticks(q_act, anchor_q, anchor_ft)) / 10.0)
            if external is not None:
                detected = external.wait_detect(seq)
                if detected is None:
                    uns.append(i)
                    continue
                m4.append((detected - ft_of_ticks(target, anchor_q, anchor_ft)) / 10.0)
                handoff.append((detected - ft_at_fire[0]) / 10.0)
    finally:
        if old_interval is not None:
            sys.setswitchinterval(old_interval)
    return {"m0": m0, "m0t": m0t, "m1": m1, "m2": m2, "m4": m4,
            "handoff": handoff, "uns": uns}


def print_result(title, res, bins=None):
    print(f"\n=== {title} ===")
    print(fmt("M0 回调入口-deadline", stats(res["m0"])))
    print(fmt("M0T 回调入口-target", stats(res["m0t"])))
    print(fmt("M1 QPC 偏差(us)", stats(res["m1"])))
    if res["m2"]:
        # m2 的符号：正值表示 FILETIME 映射比 QPC 快（读在 QPC 之前）
        print(fmt("M2 同刻两钟差(us)", stats(res["m2"])))
    if res["m4"]:
        print(fmt("M4 外部进程偏差(us)", stats(res["m4"])))
        print(fmt("   进程间可见延迟(us)", stats(res["handoff"])))
        diff = [a - b for a, b in zip(res["m4"], res["handoff"])]
        print(fmt("   M4-延迟(us)", stats(diff)))
    if res["uns"]:
        print(f"   未完成：{len(res['uns'])} 枪")
    if bins:
        for tag, vals in (("M0", res["m0"]), ("M1", res["m1"])):
            print(f"   {tag} 直方图（us）：")
            for lo, hi, c in histogram(vals, bins):
                print(f"   [{lo:g},{hi:g}) us: {c}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="precision kernel bench")
    ap.add_argument("--shots", type=int, default=200)
    ap.add_argument("--external", action="store_true", help="启用外部进程观测")
    ap.add_argument("--spin-sweep", action="store_true")
    ap.add_argument("--priority-sweep", action="store_true")
    ap.add_argument("--load", choices=["none", "single", "all", "procs"], default="none")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    bins = [(0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 10), (10, 25), (25, 50),
            (50, 100), (100, 200), (200, 500), (500, 1000), (1000, 1e9)]
    out = {}

    load = None
    if args.load == "single":
        load = Load(1)
        print("[load] 1 个普通优先级 Python 忙等线程（会争 GIL）")
    elif args.load == "all":
        n = max(2, (os.cpu_count() or 4) // 2)
        load = Load(n)
        print(f"[load] {n} 个普通优先级 Python 忙等线程（会争 GIL）")
    elif args.load == "procs":
        n = max(1, os.cpu_count() or 4)
        load = ProcLoad(n)
        print(f"[load] {n} 个忙等子进程占满 CPU（不涉及 GIL）")
    obs = ExternalObserver() if args.external else None
    try:
        if not (args.spin_sweep or args.priority_sweep):
            res = run_shots(args.shots, external=obs)
            print_result(f"idle / spin={P.DEFAULT_SPIN_S * 1000:.0f}ms / "
                         f"prio={P.THREAD_PRIORITY_TIME_CRITICAL}", res, bins)
            out["base"] = {k: stats(v) if k != "uns" else len(v) for k, v in res.items()}

        if args.spin_sweep:
            for spin_ms in (0.5, 1, 2, 3, 5, 10):
                res = run_shots(args.shots, spin_s=spin_ms / 1000.0, external=obs)
                print_result(f"spin={spin_ms}ms", res)
                out[f"spin_{spin_ms}"] = stats(res["m1"])
        if args.priority_sweep:
            for prio_name, prio in (("normal", 0), ("highest", 2), ("time_critical", 15)):
                res = run_shots(args.shots, priority=prio, external=obs)
                print_result(f"priority={prio_name}({prio})", res)
                out[f"prio_{prio_name}"] = stats(res["m1"])
    finally:
        if obs is not None:
            obs.close()
        if load is not None:
            load.close()

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
