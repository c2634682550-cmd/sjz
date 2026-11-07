# -*- coding: utf-8 -*-
"""并行认卡 A/B：线程数 × 并发度，看墙钟、CPU、识别结果是否一致。"""
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import market, ocr, ocr_chain, ocr_rapid

IMG = cv2.imdecode(np.fromfile(os.path.join(SNAP, "tests", "fixtures", "market",
                                            "list_unfiltered.png"), dtype=np.uint8), 1)


def sig(rows):
    return [(r.get("name"), r.get("price"), r.get("wear")) for r in rows]


def run(threads, parallel, rounds=4, warm=True):
    ocr._settings = {"rapid": {"threads": threads}}
    ocr._backend = None
    ocr._broken = False
    b = ocr._client()
    sc = market.Scanner(parallel=parallel)
    rows = sc.scan(IMG)                       # 首帧：建池 + 建 session
    if warm:
        sc.scan(IMG)
    ts, cpus = [], []
    for _ in range(rounds):
        c0, t0 = time.process_time(), time.perf_counter()
        rows = sc.scan(IMG)
        ts.append((time.perf_counter() - t0) * 1000)
        cpus.append((time.process_time() - c0) * 1000)
    sc.reset()
    ts2 = []
    for _ in range(rounds):
        t0 = time.perf_counter(); rows = sc.scan(IMG); ts2.append((time.perf_counter() - t0) * 1000)
    return {
        "sig": sig(rows),
        "p50_cached": round(statistics.median(ts), 1),
        "p50_fresh": round(statistics.median(ts2), 1),
        "cpu_ms": round(statistics.median(cpus), 1),
    }


gold = None
for threads, parallel in ((4, 1), (4, 2), (2, 2), (2, 3), (4, 4), (2, 1)):
    r = run(threads, parallel)
    ok = "same" if gold is None or r["sig"] == gold else "DIFF"
    gold = gold or r["sig"]
    print(f"threads={threads} parallel={parallel}: fresh={r['p50_fresh']}ms "
          f"cached={r['p50_cached']}ms cpu={r['cpu_ms']}ms rows={ok}")
