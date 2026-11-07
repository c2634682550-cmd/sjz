"""句柄增长定位探针。

长跑 72h 数据显示句柄 218 -> 382（唯一 FAIL 项）。这个探针把长跑工作负载
逐段拆开，各自跑几千次，看哪一段的 `GetProcessHandleCount` 会单调上涨：

    python -m tools.stability.probe_handles [--seg all|cycle|records|shot|warm]

`warm` 是基线：空转 4000 次循环 + 采样，确认「采样器自己不漏」。
"""
import argparse
import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.stability import clock as clk, fakes, metrics, workload  # noqa: E402


def _h():
    gc.collect()
    return metrics.sample()["handles"]


def _spin_warm(n):
    for _ in range(n):
        sum(range(1000))
    return _h()


def _spin_cycle(n):
    clock = clk.VirtualClock()
    import core.countdown, core.engine, core.pagewatch, core.records, core.scheduler, core.watchdog
    mods = [core.engine, core.countdown, core.records, core.pagewatch,
            core.scheduler, core.watchdog]
    with clk.install(mods, clock):
        for i in range(n):
            workload.run_cycle(clock)
            workload.idle_tick(clock)
    return _h()


def _spin_records(n):
    import core.records as R
    for i in range(n):
        R.add(name="p%d" % i, price=100 + i, result="hit", elapsed=0.5)
        if i % 50 == 0:
            R.load(200)
            R.stats()
    return _h()


def _spin_shot(n):
    import numpy as np
    from core import retention
    from core.vision import imwrite
    ws = fakes.TempWorkspace("probe_shot")
    d = ws.shots
    os.makedirs(d, exist_ok=True)
    img = np.zeros((65, 240, 3), np.uint8)
    for i in range(n):
        imwrite(os.path.join(d, "s%d.png" % (i % 400)), img)
        retention.prune_dir(d, retention.SHOTS_MAX_FILES, retention.SHOTS_MAX_BYTES)
    return _h()


SEGS = {"warm": _spin_warm, "records": _spin_records,
        "shot": _spin_shot, "cycle": _spin_cycle}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg", default="all")
    ap.add_argument("--n", type=int, default=3000)
    a = ap.parse_args()
    segs = list(SEGS) if a.seg == "all" else [a.seg]
    if a.seg == "all":
        segs = ["warm", "records", "shot", "cycle"]
    # 工作区必须在 cycle / records 之前装好
    ws = fakes.TempWorkspace("probe_h")
    workload.apply_workspace(ws)
    metrics.track_files()
    for name in segs:
        base = _h()
        end = SEGS[name](a.n)
        print("[probe] %-8s x%-5d handles %d -> %d  delta %+d"
              % (name, a.n, base, end, end - base), flush=True)


if __name__ == "__main__":
    main()
