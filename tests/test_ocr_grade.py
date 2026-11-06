# -*- coding: utf-8 -*-
"""中文品阶 / 成色的 OCR 基准（2026-11-06 补）。

为什么单独一份：价格那块有 21 条真实 ROI 守数字，但中文品阶一直只有
「服务在跑时补问一遍」的兜底，没有数字级的基准。RapidOCR 换上来之后
必须证明它读中文不比旧管线差，否则列表 scan 会把「极品 A」读成
「极品 4」，整行被筛掉。

夹具（`tests/fixtures/grade_roi/`）从 5 张真实 1920×1080 截图上裁出，
坐标与来源见 manifest.json：

  · 4 条品阶词：传说 / 史诗 / 稀有 / 普通（筛选面板顶部）
  · 21 条「极品丨成色 X」卡片块（A/B/C/S，含 2 条价格+品阶被合成一行）
  · 4 条纯标签：「稀有度」「成色」「所有稀有度」「所有成色」
  · 1 条「典藏」标签

基准：29/29，准确率 100%，负样本 0 误识（1× 原图，RapidOCR）。
3× 放大再跑一遍，防止「_scan_cards 放大后反而变差」这种回退。
"""
import json
import os

import numpy as np
import pytest

from core import bricks, ocr

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "grade_roi")
MANIFEST = os.path.join(FIX, "manifest.json")


def _items():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)["items"]


def _load(name):
    import cv2
    p = os.path.join(FIX, name)
    buf = np.fromfile(p, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _scale(img, factor):
    if factor == 1:
        return img
    import cv2
    return cv2.resize(img, None, fx=factor, fy=factor,
                      interpolation=cv2.INTER_CUBIC)


def _check(it, img):
    text = ocr.read_text(img) or ""
    if it["kind"] == "tier":
        return it["craft"] in text
    craft = bricks.parse_craft(text)
    wear = bricks.parse_wear(text)
    if it["kind"] == "card":
        return craft == it["craft"] and wear == it["wear"]
    return craft is None and wear is None        # neg：一个都不许编出来


ITEMS = _items()
_needs_ocr = pytest.mark.skipif(
    not ocr.available(), reason=f"OCR 不可用：{ocr.backend_name()}")


@_needs_ocr
@pytest.mark.parametrize("it", ITEMS, ids=[i["id"] for i in ITEMS])
def test_grade_rois_round1(it):
    """29 条真实品阶/成色 ROI，1× 原图。"""
    assert _check(it, _load(it["file"])), f"{it['id']} {it['note']}"


@_needs_ocr
@pytest.mark.parametrize("it", ITEMS, ids=[i["id"] for i in ITEMS])
def test_grade_rois_at_3x(it):
    """同一批 ROI 放大 3× 再读 —— _scan_cards 走的就是 3×。"""
    assert _check(it, _scale(_load(it["file"]), 3)), f"{it['id']} {it['note']}"


@_needs_ocr
def test_grade_baseline_is_complete():
    """基准集本身别缩水：4 品阶 + 20 卡片 + 5 负样本。"""
    kinds = [i["kind"] for i in ITEMS]
    assert kinds.count("tier") == 4
    assert kinds.count("card") == 20
    assert kinds.count("neg") == 5
    assert len(ITEMS) == 29


def test_wear_parser_rejects_lookalike_names():
    """名称里的型号字母不许被当成成色（M4A1 / SCAR-H / S2）。"""
    assert bricks.parse_wear("M4A1突击步枪-群星", strict=True) is None
    assert bricks.parse_wear("SCAR-H突击步枪", strict=True) is None
    assert bricks.parse_wear("MP7冲锋枪-电玩高手S2", strict=True) is None
    # 但带「成色」字样的写法必须认出来
    assert bricks.parse_wear("极品丨成色 A", strict=True) == "A"
