"""卡点进程结构验证（分支 B 结论的代码层复核）。

分支 B 实测：主进程 16 个进程满载时，同一 `fire_at` 在主进程里 p50 变成
12.6ms，放进独立进程仍 0.2µs；根因是 GIL，不是算力。这里用更温和的
负载（主进程 3 个 Python 线程吃 GIL）复核同一个结构性结论：

- `tools/fire_worker.py`：独立进程，只做自旋，不 import 项目模块；
- 主进程内自旋：受 GIL 调度影响；
- 两者同 target 对比，给出偏差分布。

全程不碰鼠标 / 窗口 / 前台，只吃 CPU。
"""

import json
import os
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "tools", "fire_worker.py")


class FireWorker:
    """长连接卡点进程：发 target 行，读回实际触发时刻。"""

    def __init__(self, exe=None):
        self.p = subprocess.Popen(
            [exe or sys.executable, WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)

    def fire_at(self, target):
        self.p.stdin.write(f"{target:.9f}\n")
        self.p.stdin.flush()
        return float(self.p.stdout.readline())

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
            self.p.wait(timeout=3)
        except Exception:                   # noqa: BLE001
            pass


class Load:
    """主进程内的 GIL 竞争者：纯 Python 忙等。"""

    def __init__(self, n=3):
        self.stop = False
        self.ths = [threading.Thread(target=self._run, daemon=True)
                    for _ in range(n)]
        for t in self.ths:
            t.start()

    def _run(self):
        while not self.stop:
            pass

    def close(self):
        self.stop = True
        for t in self.ths:
            t.join(timeout=1)


def _pct(xs, q):
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _summary(errs):
    a = [abs(x) for x in errs]
    return {"p50": round(_pct(a, .50), 4), "p95": round(_pct(a, .95), 4),
            "max": round(max(a), 4), "n": len(a)}


def measure_worker(n=100, lead=0.010, load_n=3):
    w = FireWorker()
    load = Load(load_n)
    errs = []
    try:
        for _ in range(n):
            target = time.perf_counter() + lead
            fired = w.fire_at(target)
            errs.append((fired - target) * 1000.0)
    finally:
        load.close()
        w.close()
    return _summary(errs)


def measure_inprocess(n=100, lead=0.010, load_n=3):
    load = Load(load_n)
    errs = []
    try:
        for _ in range(n):
            target = time.perf_counter() + lead
            while time.perf_counter() < target:
                pass
            errs.append((time.perf_counter() - target) * 1000.0)
    finally:
        load.close()
    return _summary(errs)


def test_worker_stays_accurate_under_main_process_load():
    s = measure_worker(100, load_n=3)
    assert s["p95"] < 5.0, s
    assert s["p50"] < 2.0, s


def test_in_process_spin_degrades_under_main_process_load():
    s = measure_inprocess(100, load_n=3)
    assert s["p50"] > 2.0, f"主进程内自旋没有被 GIL 干扰？{s}"


def test_worker_beats_in_process_spin():
    w = measure_worker(100, load_n=3)
    p = measure_inprocess(100, load_n=3)
    assert w["p50"] < p["p50"], f"worker={w} inproc={p}"


if __name__ == "__main__":
    out = {
        "worker_under_load": measure_worker(200, load_n=3),
        "inprocess_under_load": measure_inprocess(200, load_n=3),
        "worker_idle": measure_worker(200, load_n=1),
        "inprocess_idle": measure_inprocess(200, load_n=1),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
