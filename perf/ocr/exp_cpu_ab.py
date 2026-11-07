# -*- coding: utf-8 -*-
"""CPU 对照：同一脚本分别跑 base / work 快照，量「CPU 毫秒」而不是墙钟。

墙钟会被同机上的七路合并分支抢核，CPU 时间（process_time）更稳，用来做
base→work 的可比口径。用法：BENCH_SNAP=<快照> python exp_cpu_ab.py
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


def frame(k):
    img = IMG.copy()
    for x0, y0, x1, y1 in RECTS:
        cy = (y0 + y1) // 2
        img[cy - 12:cy + 12, x1 - 30:x1 - 6] = (255, 255, 255) if k % 2 else (0, 0, 0)
    return img


def measure(label, fn, rounds=6):
    fn(0)
    walls, cpus = [], []
    for i in range(rounds):
        c0, t0 = time.process_time(), time.perf_counter()
        rows = fn(i + 1)
        walls.append((time.perf_counter() - t0) * 1000)
        cpus.append((time.process_time() - c0) * 1000)
    w = statistics.median(walls)
    c = statistics.median(cpus)
    # 用 CPU 毫秒算每商品成本（6 张卡 → 行数，通常 6）
    n = max(1, len(rows))
    print(f"{label:22s} wall={w:7.1f} cpu={c:8.1f} cpu%1core={c / w * 100:6.1f}% "
          f"cores={c / w:5.2f} cpu_ms/prod={c / n:6.1f} rows={len(rows)}")


print(f"[snap] {SNAP}")
measure("stateless scan", lambda k: market.scan(frame(k)))
sc = market.Scanner()          # 复用同一个实例：缓存/线程池只建一次
measure("Scanner", lambda k: sc.scan(frame(k)))
