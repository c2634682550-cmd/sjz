# -*- coding: utf-8 -*-
"""价格 OCR 的七类验收（2026-11-06 落地 RapidOCR 主路径时补）。

夹具是 5 张真实 1920×1080 截图里人工框出的 21 条价格 ROI（含千分位），
来源与坐标见仓库根 `_ocr_bench_user_shots.md`；`u4_card_r1_r` 被筛选面板
遮挡，没框进来（标了「不可读」）。

七条对应报告里的验收项：
  1. 真实 ROI 全量（两轮，第二轮打乱）
  2. 两个尺度一致（1× / 3×）
  3. 闸门 2：千分位 / 位数格式
  4. 闸门 1：分数阈值
  5. 三级降级顺序（Rapid → PP-OCR → 字模）
  6. 缺模型 / 服务不在时的降级（不抛、不猜）
  7. 冷启动预算 + 本地模型优先
"""
import json
import os
import random
import time

import numpy as np
import pytest

from core import ocr, ocr_chain, ocr_rapid

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "price_roi")
MANIFEST = os.path.join(FIX, "manifest.json")


def _items():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)["items"]


def _load(name):
    import cv2
    p = os.path.join(FIX, name)
    buf = np.fromfile(p, dtype=np.uint8)          # 中文路径也能读
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


ITEMS = _items()
# 真实识别用例：RapidOCR 装不起来就整组跳过，并说明原因（别给假绿）
_needs_ocr = pytest.mark.skipif(
    not ocr.available(), reason=f"OCR 不可用：{ocr.backend_name()}")


# ---------------------------------------------------------------- 1. 真实 ROI

@_needs_ocr
@pytest.mark.parametrize("it", ITEMS, ids=[i["id"] for i in ITEMS])
def test_real_price_rois_round1(it):
    """21 条真实价格 ROI，第一轮按标注顺序。"""
    assert ocr.read_price(_load(it["file"])) == int(it["digits"]), it["id"]


@_needs_ocr
def test_real_price_rois_round2_shuffled():
    """第二轮打乱顺序：结果必须与第一轮一致（共 42 次判定）。"""
    order = list(ITEMS)
    random.Random(20261106).shuffle(order)
    bad = []
    for it in order:
        got = ocr.read_price(_load(it["file"]))
        if got != int(it["digits"]):
            bad.append((it["id"], it["display"], got))
    assert not bad, f"打乱后不一致：{bad}"


# ---------------------------------------------------------------- 2. 尺度

@_needs_ocr
@pytest.mark.parametrize("idx", [0, 4, 6, 8, 12, 18])
def test_two_scales_agree(idx):
    """同一条 ROI 在 1× 与 3× 下必须读出同一个数（42/42 的两个尺度）。"""
    it = ITEMS[idx]
    img = _load(it["file"])
    import cv2
    big = cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    assert ocr.read_price(img) == int(it["digits"])
    assert ocr.read_price(big) == int(it["digits"])


# ---------------------------------------------------------------- 3. 格式闸门

@pytest.mark.parametrize("text,want", [
    ("168,000", 168000),
    ("1,000,000", 1000000),
    ("9,999,999", 9999999),
    ("25000000", 25000000),
    ("0", 0),
    ("2,049 0+ 0 40+", 40),        # 余额行：取最后一个合格数字串
    ("9,9", None),                 # 半截串
    ("1,00,000", None),            # 分组错
    ("1,000,", None),              # 逗号悬空
    ("025,000", None),             # 图标粘出来的前导 0
    ("1234567890", None),          # 超 9 位（没读全）
    ("", None),
    ("暂无", None),
])
def test_thousand_separator_gate(text, want):
    assert ocr_chain.price_from_text(text)[0] == want
    assert ocr.last_number(text) == want      # 对外入口同一套规则


# ---------------------------------------------------------------- 4. 分数闸门

def test_low_score_word_is_rejected():
    low = [{"text": "150,000", "score": 0.42, "x": 0, "y": 0, "w": 80, "h": 20}]
    ok = [{"text": "150,000", "score": 0.93, "x": 0, "y": 0, "w": 80, "h": 20}]
    assert ocr_chain._accept(low)[0] is None
    assert ocr_chain._accept(ok)[0] == 150000


# ---------------------------------------------------------------- 5. 三级链

class _Rapid:
    def __init__(self, words=None, boom=False):
        self.words, self.boom = words or [], boom
        self.stats = {"calls": 0, "hits": 0, "ms": 0.0, "fails": 0, "lines": 0}

    def available(self):
        return not self.boom

    def read_words(self, img, timeout=None):
        if self.boom:
            raise RuntimeError("rapid 崩了")
        return self.words

    def recent_ms(self):
        return 0.0

    def stats_line(self):
        return "stub"


class _PP:
    def __init__(self, words=None, boom=False):
        self.words, self.boom, self.cfg = words or [], boom, {"model": "PP-OCRv6 tiny"}

    def available(self):
        return not self.boom

    def read_words(self, img, timeout=None):
        if self.boom:
            raise RuntimeError("服务不在")
        return self.words


def _chain(rapid_words=None, pp_words=None, tpl=None, rap_boom=False, pp_boom=False):
    b = ocr_chain.Backend.__new__(ocr_chain.Backend)
    b.rapid = _Rapid(rapid_words, rap_boom)
    b.pp = _PP(pp_words, pp_boom)
    b.tpl = tpl or type("T", (), {"read_detail": staticmethod(lambda img: (None, "", 0.0))})()
    b.stats = {"calls": 0, "empty": 0, "tier2": 0, "tier3": 0, "ms": 0.0, "fails": 0}
    b.url = ""
    return b


def test_tier1_wins_when_confident():
    b = _chain([{"text": "150,000", "score": 0.95, "x": 0, "y": 0, "w": 80, "h": 20}])
    assert b.read_price(np.zeros((32, 120, 3), np.uint8)) == 150000
    assert b.stats["tier2"] == 0 and b.stats["tier3"] == 0


def test_tier2_runs_when_tier1_is_unsure():
    """一级分数不够 → 二级（3× 送服务）；二级合格就用它。"""
    b = _chain([{"text": "150,000", "score": 0.42, "x": 0, "y": 0, "w": 80, "h": 20}],
               [{"text": "150,000", "score": 0.91, "x": 0, "y": 0, "w": 240, "h": 60}])
    assert b.read_price(np.zeros((32, 120, 3), np.uint8)) == 150000
    assert b.stats["tier2"] == 1


def test_tier3_is_the_last_resort():
    """两条管线都不合格 → 字模；字模也给 None 就返回 None（不猜）。"""
    tpl_hit = type("T", (), {"read_detail": staticmethod(
        lambda img: (250000, "250,000", 0.72))})()
    b = _chain([], [], tpl=tpl_hit, pp_boom=True)
    assert b.read_price(np.zeros((32, 120, 3), np.uint8)) == 250000
    b2 = _chain([], [], pp_boom=True)
    assert b2.read_price(np.zeros((32, 120, 3), np.uint8)) is None


# ---------------------------------------------------------------- 6. 缺模型

def test_no_engine_at_all_degrades_quietly(monkeypatch):
    """Rapid 被关 + 服务地址是黑洞：available 为假、read_price 返回 None、
    read_text 返回空串 —— 不许抛异常，也不许编一个数。"""
    s = {"rapid": {"enabled": False},
         "ppocr": {"mode": "http", "url": "http://127.0.0.1:9", "timeout_s": 0.3,
                   "cache": 4, "min_score": 0.5, "fail_limit": 1,
                   "cooldown_s": 0.1, "long_cooldown_s": 1.0}}
    monkeypatch.setattr(ocr, "_settings", s, raising=False)
    monkeypatch.setattr(ocr, "_backend", None, raising=False)
    monkeypatch.setattr(ocr, "_last_err", "", raising=False)
    monkeypatch.setattr(ocr, "_broken", False, raising=False)
    b = ocr._client()
    assert b.available() is False
    img = np.zeros((32, 120, 3), np.uint8)
    assert b.read_price(img) is None
    assert ocr.read_text(img) == ""


# ---------------------------------------------------------------- 7. 冷启动

def test_local_models_are_preferred_and_cold_start_is_small():
    """模型必须优先从 core/assets/rapid 读（离线可跑），冷启动 < 5s。

    1.2s 是这台机器实测值；给 5s 余量是因为 CI/低配机会慢，但这道闸门
    能拦住「悄悄去联网下模型」导致的几十秒冷启动。
    """
    det, rec = ocr_rapid._model_paths()
    assert det and rec, "core/assets/rapid 里缺 onnx 模型，离线装机就废了"
    assert os.path.dirname(det) == ocr_rapid.MODEL_DIR
    t0 = time.perf_counter()
    b = ocr_rapid.make()
    assert b.available()
    assert (time.perf_counter() - t0) < 5.0
