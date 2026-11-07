# -*- coding: utf-8 -*-
"""扫描吞吐 A/B：ORT 线程数。每档用全新 Backend，交替跑，比较墙钟与识别文本一致性。"""
import copy
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "base"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import market, ocr, ocr_rapid

FIX = os.path.join(SNAP, "tests", "fixtures")
KEY = "EngineConfig.onnxruntime.intra_op_num_threads"
IMG = cv2.imdecode(np.fromfile(os.path.join(FIX, "market", "list_unfiltered.png"),
                               dtype=np.uint8), cv2.IMREAD_COLOR)
BASE_PARAMS = copy.deepcopy(ocr_rapid._PARAMS)


def rows_sig(rows):
    return tuple((str(r.get("name")), r.get("price")) for r in rows)


def run_with(th):
    ocr_rapid._PARAMS = {**BASE_PARAMS, KEY: th,
                         "EngineConfig.onnxruntime.inter_op_num_threads": 1}
    ocr._settings = None
    ocr._backend = None
    ocr._broken = False
    t0 = time.perf_counter()
    b = ocr._client()
    b.rapid.available()                    # 建 session
    cold = (time.perf_counter() - t0) * 1000
    market.scan(IMG)                       # 预热
    ts = []
    sig = None
    calls = []
    for _ in range(3):
        t0 = time.perf_counter()
        rows = market.scan(IMG)
        ts.append((time.perf_counter() - t0) * 1000)
        sig = sig or rows_sig(rows)
        calls.append((len(rows), len(b.rapid.recent)))
    return cold, statistics.median(ts), sig, len(rows)


gold = None
out = {}
for th in (16, 8, 4, 2, 1):
    cold, med, sig, n = run_with(th)
    gold = gold or sig
    same = "same" if sig == gold else "DIFF"
    out[th] = (cold, med)
    print(f"threads={th}: init={cold:.0f}ms scan_p50={med:.1f}ms rows={n} text={same} "
          f"products/s={n/(med/1000):.2f}")

# 交替复测，排除顺序偏置
rep = {th: [] for th in out}
for _ in range(3):
    for th in (2, 4, 1, 8):
        ocr_rapid._PARAMS = {**BASE_PARAMS, KEY: th,
                             "EngineConfig.onnxruntime.inter_op_num_threads": 1}
        ocr._settings = None; ocr._backend = None; ocr._broken = False
        ocr._client().rapid.available()
        t0 = time.perf_counter(); market.scan(IMG); rep[th].append((time.perf_counter() - t0) * 1000)
print("交替复测 scan p50:", {th: round(statistics.median(v), 1) for th, v in rep.items()})
