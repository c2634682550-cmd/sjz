# -*- coding: utf-8 -*-
"""诊断：走真实 core.ocr 路径时，详情面板到底用了哪一档线程。"""
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import ocr

p = os.path.join(SNAP, "tests", "fixtures", "market", "detail_panel.png")
img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
print("shape", img.shape)

ocr.read_words(img)
b = getattr(ocr, "_backend", None)
chain = b
rap = getattr(chain, "rapid", None) if chain is not None else None
print("backend:", type(chain).__name__)
if rap is not None:
    print("rapid cfg:", {k: v for k, v in rap.cfg.items() if "thread" in k or "big" in k})
    print("engs:", getattr(getattr(rap, "_local", None), "engs", None))
xs = []
for _ in range(12):
    t0 = time.perf_counter()
    ocr.read_words(img)
    xs.append((time.perf_counter() - t0) * 1000.0)
xs.sort()
print(f"detail p50={statistics.median(xs):.1f} min={xs[0]:.1f}")
