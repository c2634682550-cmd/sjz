# -*- coding: utf-8 -*-
"""并行认卡 A/B（2）：用带微扰的帧强制重认，比较墙钟/CPU/结果一致性。

同一帧连跑会被 Scanner 的差分缓存全部跳过（0 次 OCR），必须每帧在卡片
右侧留白处点一个 6×6 白块，让 changed_cards 判定「这几张卡变了」。
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
from core import cards, market, ocr

FIX = os.path.join(SNAP, "tests", "fixtures", "market")
BASE_IMG = cv2.imdecode(np.fromfile(os.path.join(FIX, "list_unfiltered.png"),
                                    dtype=np.uint8), cv2.IMREAD_COLOR)
RECTS = cards.locate_cards(BASE_IMG)


def frame(k):
    img = BASE_IMG.copy()
    for r in RECTS:
        x0, y0, x1, y1 = r
        cy = (y0 + y1) // 2
        img[cy - 12:cy + 12, x1 - 30:x1 - 6] = (255, 255, 255) if k % 2 else (0, 0, 0)
    return img


def sig(rows):
    return [(r.get("name"), r.get("price"), r.get("wear")) for r in rows]


def run(threads, parallel, rounds=5):
    ocr._settings = {"rapid": {"threads": threads}}
    ocr._backend = None
    ocr._broken = False
    sc = market.Scanner(parallel=parallel)
    sc.scan(frame(1))                          # 建池/建 session
    sc.scan(frame(2))
    ts, cpus, calls = [], [], []
    rows = None
    for i in range(rounds):
        c0, t0 = time.process_time(), time.perf_counter()
        n0 = sc.ocr_calls
        rows = sc.scan(frame(i + 3))
        ts.append((time.perf_counter() - t0) * 1000)
        cpus.append((time.process_time() - c0) * 1000)
        calls.append(sc.ocr_calls - n0)
    return sig(rows), round(statistics.median(ts), 1), round(statistics.median(cpus), 1), calls


gold = None
for th, par in ((4, 1), (4, 3), (4, 4), (4, 5)):
    s, wall, cpu, calls = run(th, par)
    ok = "same" if gold is None or s == gold else "DIFF"
    gold = gold or s
    print(f"threads={th} parallel={par}: wall={wall}ms cpu={cpu}ms calls={calls} rows={ok}")
