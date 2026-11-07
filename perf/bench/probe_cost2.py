# -*- coding: utf-8 -*-
"""关键点上再做几次更细的测量：
1) 同一张 240x65 倒计时图反复 OCR 的分布（决定开点/解锁检测的粒度，P95 红线所在）
2) 弹窗识别 popup.scan / 全屏帧 popup 成本
3) 成交截图 imwrite + records.add 的磁盘成本
4) 「用卡片定位判断回到列表」这条廉价信号在真实列表帧上是否成立
"""
import json
import os
import sys
import tempfile
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PROJ, "perf", "base"))

from core import cards as cardmod          # noqa: E402
from core import ocr, popup, records, vision  # noqa: E402

FIX = os.path.join(PROJ, "tests", "fixtures", "market", "list_filtered.png")
OUT = os.path.join(PROJ, "perf", "out")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))) )]


frame = cv2.imread(FIX)
H, W = frame.shape[:2]
res = {}

# 1) 倒计时区：真实尺寸 300x80（含字），连续 60 次
clock = frame[300:380, 700:1000]
res["clock_shape"] = list(clock.shape)
big = vision.upscale(clock, 2)
xs = []
for _ in range(60):
    t = time.perf_counter(); w = ocr.read_words(big); xs.append((time.perf_counter() - t) * 1000)
res["clock_words_60"] = {"p10": round(pct(xs, .1), 2), "p50": round(pct(xs, .5), 2),
                         "p90": round(pct(xs, .9), 2), "p95": round(pct(xs, .95), 2),
                         "max": round(max(xs), 2), "n_words": len(w)}

# 1b) 同一块只有 1 行数字（更像倒计时）：从序列号样式处裁
num = frame[0:40, 380:620]
bign = vision.upscale(num, 2)
xs = []
for _ in range(40):
    t = time.perf_counter(); w2 = ocr.read_words(bign); xs.append((time.perf_counter() - t) * 1000)
res["number_words_40"] = {"p50": round(pct(xs, .5), 2), "p95": round(pct(xs, .95), 2),
                          "max": round(max(xs), 2), "text": "".join(x["text"] for x in w2)[:40]}

# 2) popup.scan 成本（全屏帧）
full = cv2.resize(frame, (1920, 1080))
xs = []
for _ in range(20):
    t = time.perf_counter(); r = popup.scan(full); xs.append((time.perf_counter() - t) * 1000)
res["popup_scan_full"] = {"p50": round(pct(xs, .5), 2), "p95": round(pct(xs, .95), 2),
                          "found": r["found"]}

# 3) imwrite JPEG + records.add
tmp = tempfile.mkdtemp()
xs = []
for _ in range(12):
    t = time.perf_counter(); cv2.imwrite(os.path.join(tmp, "a.jpg"), full, [cv2.IMWRITE_JPEG_QUALITY, 80])
    xs.append((time.perf_counter() - t) * 1000)
res["imwrite_full_jpg"] = {"p50": round(pct(xs, .5), 2), "p95": round(pct(xs, .95), 2)}
# records 指向临时文件，纯写盘成本
xs = []
for _ in range(20):
    t = time.perf_counter();
    records.add(name="bench", price=2050.0, result="miss", elapsed=1.2, extra={})
    xs.append((time.perf_counter() - t) * 1000)
res["records_add"] = {"p50": round(pct(xs, .5), 2), "p95": round(pct(xs, .95), 2)}

# 4) 列表帧上 locate_cards 的稳定性（用作「回到列表」廉价信号）
n_ok = 0
for _ in range(20):
    if len(cardmod.locate_cards(frame)) >= 2:
        n_ok += 1
res["locate_cards_ge2_ratio"] = f"{n_ok}/20"
res["locate_cards_n"] = len(cardmod.locate_cards(frame))

# 5) 缩略抓屏（用一半尺寸抓列表区）是否仍能定位卡片
from core.screen import Screen
scr = Screen(monitor=1)
px = (60, 210, 1340, 730)
xs = []
for _ in range(20):
    t = time.perf_counter(); half = scr.grab((60, 210, 670, 365)); xs.append((time.perf_counter() - t) * 1000)
res["grab_half_list"] = {"p50": round(pct(xs, .5), 2), "p95": round(pct(xs, .95), 2)}
res["half_mean_absdiff_self"] = None
a = scr.grab((60, 210, 670, 365))
b = scr.grab((60, 210, 670, 365))
if a is not None and b is not None and a.shape == b.shape:
    res["half_mean_absdiff_self"] = round(float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()), 3)
scr.close()

with open(os.path.join(OUT, "costs_detail.json"), "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print(json.dumps(res, ensure_ascii=False, indent=1))
