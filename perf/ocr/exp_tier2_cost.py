# -*- coding: utf-8 -*-
"""二级「在听但在崩」时，价格链的真实代价：多一次 HTTP 往返 + 落三级。

对比口径：服务在听（探活 True、推理 500）vs 服务不在（探活 False，直接跳）。
"""
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import ocr, ocr_chain

FIX = os.path.join(SNAP, "tests", "fixtures", "price_roi")


def load(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


items = json.load(open(os.path.join(FIX, "manifest.json"), encoding="utf-8"))["items"]
b = ocr_chain.make((ocr._cfg().get("ocr") or ocr._cfg()))
print("probe:", b.pp.available(), "| name:", b.name())

ms, wrong = [], []
for it in items:
    img = load(os.path.join(FIX, it["file"]))
    t0 = time.perf_counter()
    v = b.read_price(img)
    ms.append((time.perf_counter() - t0) * 1000)
    want = int(it["digits"])
    if v is None or v != want:
        wrong.append((it["file"], want, v))

print(f"n={len(items)} p50={statistics.median(ms):.1f}ms max={max(ms):.1f}ms")
print("stats:", b.stats_line())
print("last_error:", (b.last_error or "")[:120])
print(f"不中是: {len(wrong)} -> {wrong[:5]}")
