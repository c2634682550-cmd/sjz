# -*- coding: utf-8 -*-
"""找大图（详情面板 590×860）的线程拐点：20~40 逐档、两轮。"""
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import ocr_rapid

p = os.path.join(SNAP, "tests", "fixtures", "market", "detail_panel.png")
img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)

COUNTS = [20, 22, 24, 26, 28, 30, 32, 36, 40]
engs = {}
for n in COUNTS:
    b = ocr_rapid.make({"threads": n})
    b.read_words(img)
    b.read_words(img)
    engs[n] = b

for rnd in (1, 2):
    print(f"--- round {rnd} ---")
    for n, b in engs.items():
        xs = []
        for _ in range(10):
            t0 = time.perf_counter()
            b.read_words(img)
            xs.append((time.perf_counter() - t0) * 1000.0)
        xs.sort()
        print(f"threads={n:>3}  p50={statistics.median(xs):6.1f}  "
              f"min={xs[0]:6.1f}  p90={xs[8]:6.1f}")
