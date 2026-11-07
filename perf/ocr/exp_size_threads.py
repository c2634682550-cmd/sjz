# -*- coding: utf-8 -*-
"""按尺寸定线程：同一批图在不同 intra_op 线程数下的单帧延迟。

只看一件事：大图（详情面板 590×860）到底几线程最快，小 ROI（价格条/卡片）
几线程最快，两者拐点是否真的不同。数据驱动阈值，不拍脑袋。
"""
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
# 卡片里的一行（名称/成色/价格），取列表图左上角一张卡的条带
card = listimg[10:110, 20:320]

IMGS = [("detail", detail), ("card300x100", card), ("price", price)]

REPS = 10
WARM = 2


def bench(n):
    b = ocr_rapid.make({"threads": n})
    if not b.available():
        return None
    for _, img in IMGS:
        for _ in range(WARM):
            b.read_words(img)
    out = {}
    for name, img in IMGS:
        xs = []
        for _ in range(REPS):
            t0 = time.perf_counter()
            b.read_words(img)
            xs.append((time.perf_counter() - t0) * 1000.0)
        xs.sort()
        out[name] = (statistics.median(xs), xs[0], xs[-1])
    return out


print("shape detail=%s card=%s price=%s" % (detail.shape, card.shape, price.shape))
print(f"{'threads':>7} | " + " | ".join(f"{n:>26}" for n, _ in IMGS))
print(f"{'':>7} | " + " | ".join(f"{'p50':>8}{'min':>10}{'max':>8}" for _ in IMGS))
rows = {}
for n in (1, 2, 4, 6, 8, 12, 16, 24, 32):
    t0 = time.perf_counter()
    r = bench(n)
    rows[n] = r
    if r is None:
        print(f"{n:>7} | 不可用")
        continue
    cells = " | ".join(f"{r[k][0]:8.1f}{r[k][1]:10.1f}{r[k][2]:8.1f}" for k, _ in IMGS)
    print(f"{n:>7} | {cells}   ({time.perf_counter() - t0:.0f}s)")
