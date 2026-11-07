# -*- coding: utf-8 -*-
"""复测：默认线程 vs 各档位，两轮交替，看 16 线程的异常是否稳定复现。"""
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
listimg = load("market/list_unfiltered.png")
price = load("price_roi/u3_card_r1_l.png")
card = listimg[10:110, 20:320]
IMGS = [("detail", detail), ("card", card), ("price", price)]

print("cpu_count:", os.cpu_count())
COUNTS = [None, 6, 8, 10, 12, 16, 24, 32]
settings = {}
for n in COUNTS:
    b = ocr_rapid.make({} if n is None else {"threads": n})
    assert b.available()
    for _, img in IMGS:
        b.read_words(img)
        b.read_words(img)
    settings[n] = b

for rnd in (1, 2):
    print(f"\n--- round {rnd} ---")
    for n, b in settings.items():
        cells = []
        for name, img in IMGS:
            xs = []
            for _ in range(8):
                t0 = time.perf_counter()
                b.read_words(img)
                xs.append((time.perf_counter() - t0) * 1000.0)
            xs.sort()
            cells.append(f"{name}={statistics.median(xs):6.1f}")
        print(f"threads={str(n):>4}  " + "  ".join(cells))
