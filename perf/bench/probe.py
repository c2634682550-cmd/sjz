# -*- coding: utf-8 -*-
"""探针：确认夹具尺寸、卡片定位数量、OCR 可用性。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "base"))

import cv2  # noqa: E402
from core import cards, ocr, ocr_chain, ocr_rapid  # noqa: E402

FIX = os.path.join(BASE, "base", "tests", "fixtures")

for name in ("market/list_unfiltered.png", "market/list_filtered.png",
             "market/detail_panel.png", "market/catalog.png",
             "market/filter_panel.png", "market/insufficient.png"):
    p = os.path.join(FIX, name)
    img = cv2.imdecode(cv2.imread(p, cv2.IMREAD_UNCHANGED), cv2.IMREAD_COLOR) if False else cv2.imread(p)
    print(name, None if img is None else img.shape)

img = cv2.imread(os.path.join(FIX, "market/list_unfiltered.png"))
print("cards(unfiltered):", len(cards.locate_cards(img)))
img2 = cv2.imread(os.path.join(FIX, "market/list_filtered.png"))
print("cards(filtered):", len(cards.locate_cards(img2)))

print("rapid available:", ocr_rapid.make().available())
print("chain available:", ocr.available())
print("backend:", ocr.backend_name())
b = ocr._client()
print("pp available:", b.pp.available(), "url:", b.url)

# 一次真实调用，确认统计字段
w = ocr.read_words(cv2.imread(os.path.join(FIX, "price_roi/u3_top_2049.png")))
print("words:", [(x["text"], round(x["score"], 3)) for x in w])
