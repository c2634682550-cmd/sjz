# -*- coding: utf-8 -*-
"""生产路径 CPU 占用：无状态 market.scan vs Scanner（串行/并行），含每商品 CPU 毫秒。

注意：cmd_cpu 用的是无状态的 market.scan（每帧新建、不用差分），
生产用的是 market.Scanner；两者 CPU 形态不同，必须分开量。
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
from core import cards, market

FIX = os.path.join(SNAP, "tests", "fixtures", "market")
IMG = cv2.imdecode(np.fromfile(os.path.join(FIX, "list_unfiltered.png"),
                               dtype=np.uint8), cv2.IMREAD_COLOR)
RECTS = cards.locate_cards(IMG)
assert len(RECTS) == 6, len(RECTS)


def frame(k):
    img = IMG.copy()
    for x0, y0, x1, y1 in RECTS:
        cy = (y0 + y1) // 2
        img[cy - 12:cy + 12, x1 - 30:x1 - 6] = (255, 255, 255) if k % 2 else (0, 0, 0)
    return img


def measure(label, fn, rounds=6):
    fn(0)                                        # 预热/建池
    walls, cpus, rows = [], [], None
    for i in range(rounds):
        c0, t0 = time.process_time(), time.perf_counter()
        rows = fn(i + 1)
        walls.append((time.perf_counter() - t0) * 1000)
        cpus.append((time.process_time() - c0) * 1000)
    w = statistics.median(walls)
    c = statistics.median(cpus)
    n = len(rows)
    print(f"{label:26s} wall={w:7.1f}ms cpu={c:8.1f}ms "
          f"cpu%1core={c / w * 100:6.1f}% cores={c / w:5.2f} "
          f"cpu_ms/prod={c / n:6.1f} prod/s={n / (w / 1000):6.3f}")
    return w, c, n


measure("stateless market.scan", lambda k: market.scan(frame(k)))

for par in (1, 3, 4):
    sc = market.Scanner(parallel=par)
    measure(f"Scanner(parallel={par})", lambda k, s=sc: s.scan(frame(k)))
