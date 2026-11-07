# -*- coding: utf-8 -*-
"""实机验证二级：端口在听、推理 500 时，链路的计数/错误/降级是否成立。

不跑基准，只回答四个问题的证据部分。BENCH_SNAP 指向 work 快照。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import ocr

FIX = os.path.join(SNAP, "tests", "fixtures", "price_roi")


def load(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


print("[1] metrics(启动前) =", json.dumps(ocr.metrics(), ensure_ascii=False))
print("[2] selfcheck_pp     =", ocr.selfcheck_pp())
print("[3] backend_name     =", ocr.backend_name())

items = json.load(open(os.path.join(FIX, "manifest.json"), encoding="utf-8"))["items"]
b = ocr._client()
got = []
for it in items[:5]:
    img = load(os.path.join(FIX, it["file"]))
    got.append(b.read_price(img))
print("[4] read_price 5 例   =", got, "期望 =", [int(i["digits"]) for i in items[:5]])
print("[5] stats            =", b.stats_line())
print("[6] last_error       =", (b.last_error or "")[:160])
print("[7] backend_name(后) =", ocr.backend_name())
print("[8] metrics(后)      =", json.dumps(ocr.metrics(), ensure_ascii=False))
