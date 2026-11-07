# -*- coding: utf-8 -*-
"""同一进程里先有小图 session（4 线程），再测大图不同线程档 —— 复现流程组现场。"""
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


def load(rel):
    p = os.path.join(SNAP, "tests", "fixtures", rel)
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


detail = load("market/detail_panel.png")
card = load("market/list_unfiltered.png")[10:110, 20:320]

small = ocr_rapid.make({"threads": 4})
for _ in range(5):
    small.read_words(card)

for n in (4, 8, 12, 16, 20, 24, 28, 32):
    b = ocr_rapid.make({"threads": 4, "big_threads": n})
    b.read_words(card)
    b.read_words(detail)
    b.read_words(detail)
    xs = []
    for _ in range(10):
        t0 = time.perf_counter()
        b.read_words(detail)
        xs.append((time.perf_counter() - t0) * 1000.0)
    xs.sort()
    print(f"big_threads={n:>2}  detail p50={statistics.median(xs):6.1f}  "
          f"min={xs[0]:6.1f}")
