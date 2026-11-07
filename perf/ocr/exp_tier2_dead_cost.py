# -*- coding: utf-8 -*-
"""量化「二级在听但每次 500」的真实代价：只在二级**被触发**时才付钱。

exp_tier2_cost.py 已证明：21 张价格基准全部一级通过，tier2=0，
崩掉的中间层在基准集上零成本。这里补两件事：

  1. 单次 500 往返本身多贵（_pp_words 直接调用）；
  2. 强制二级被触发（一级置空）后连续 12 次读价的耗时曲线 ——
     验证熔断（fail_limit=3 → 长冷却 + 10s 半开探测）是否把代价压住。
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

imgs = [load(os.path.join(FIX, it["file"])) for it in items]

# 1) 单次 500 往返
lat = []
for img in imgs[:5]:
    t0 = time.perf_counter()
    got = b._pp_words(ocr_chain.upscale(img, 3), timeout=5.0)
    lat.append((time.perf_counter() - t0) * 1000.0)
    assert got is None
print(f"single 500 round-trip: n={len(lat)} p50={statistics.median(lat):.1f}ms "
      f"max={max(lat):.1f}ms")
print("after 5 fails:", b.name(), "|", b.stats_line())

# 2) 强制二级触发：一级永远读空 -> 走二级 -> 500 -> 三级字模
real = b.rapid.read_words
b.rapid.read_words = lambda img, timeout=None: []
rows = []
for k in range(12):
    img = imgs[k % len(imgs)]
    t0 = time.perf_counter()
    v = b.read_price(img)
    rows.append((time.perf_counter() - t0) * 1000.0)
    print(f"  #{k + 1:02d} {rows[-1]:7.1f}ms value={v} name={b.name()}")

b.rapid.read_words = real
print(f"forced-tier2 read_price: p50={statistics.median(rows):.1f}ms "
      f"max={max(rows):.1f}ms first3={[round(x, 1) for x in rows[:3]]}")
print("stats:", b.stats_line())
print("last_error:", b.last_error)
