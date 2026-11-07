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


@pytest.mark.parametrize("text,want", [
    ("2,049 0+ 0 40+", 40),        # 三组货币，最右一组三角币
    ("2,049 0+ 22+", 22),
    ("2,049 0+ 26 +", 26),         # `+` 与数字之间有空格
    ("2,049\n0+\n0\n40+", 40),     # read_text 逐词换行，并成一行
    ("2,049 0+ 0 40+ 限购5", 40),  # 末尾带标签干扰数字：只能是 40 或 None
    ("2,049 0+ 0 40+ 满5+", 40),   # 带 `+` 的批量折扣提示，有标签就不是货币组
    ("2,049 0+ 0 40+ 剩余5次", 40),
    ("2,049 0+ 0 40+ 5", None),    # 裸数字：不知道属于谁，拒识
    ("2,049 0+ 0 40,", None),      # 最右一组被截断（千分位坏）→ 不许退到中间组
    ("2,049 0+ 0 1,000,00|+", None),
    ("2,537K 2,075 + à", None),    # 最右一组没读出来：不许退到中间组
    ("2,049", None),               # 没有 `+`：结构不明，不猜
    ("暂无", None),
    ("", None),
])
def test_balance_entry_never_borrows_another_group(text, want):
    """余额栏锁死：要么读出最右一组货币，要么 None，绝不拿别的数字顶替。"""
    assert ocr_chain.balance_from_text(text)[0] == want
    assert ocr.last_number(text) == want


def test_balance_trailing_limit_number_never_wins():
    """用户点名的锁死用例：`限购5` 在末尾，余额必须仍是 40（或 None）。

    旧实现 `rest[-1]` 会把 5 当余额 —— 读低 8 倍，成交判定与止损就全偏了。
    """
    text = "2,049 0+ 0 40+ 限购5"
    got = ocr.last_number(text)
    assert got in (40, None)
    assert got != 5


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
    def __init__(self, words=None, boom=False, crash=False):
        self.words, self.boom, self.crash = words or [], boom, crash
        self.cfg = {"model": "PP-OCRv6 tiny"}
        self.last_error = ""

    def available(self):
        # crash=True 模拟「端口在听、每次推理 500」：探活成功，调用必炸。
        return not self.boom

    def read_words(self, img, timeout=None):
        if self.crash:
            raise RuntimeError("500 ConvertPirAttribute2RuntimeAttribute not support")
        if self.boom:
            raise RuntimeError("服务不在")
        return self.words

    def stats_line(self):
        return "pp stub"

    def selfcheck(self, timeout=None, probe_timeout=0.3):
        if self.crash:
            return False, "RuntimeError: 500 ConvertPirAttribute2RuntimeAttribute"
        return True, ""


def _chain(rapid_words=None, pp_words=None, tpl=None, rap_boom=False,
           pp_boom=False, pp_crash=False):
    b = ocr_chain.Backend.__new__(ocr_chain.Backend)
    b.rapid = _Rapid(rapid_words, rap_boom)
    b.pp = _PP(pp_words, pp_boom, pp_crash)
    b.tpl = tpl or type("T", (), {"read_detail": staticmethod(lambda img: (None, "", 0.0))})()
    b.stats = {"calls": 0, "empty": 0, "tier2": 0, "tier2_fail": 0,
               "tier3": 0, "ms": 0.0, "fails": 0}
    b._pp_err = ""
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


def test_tier2_listening_but_500_degrades_counts_and_keeps_error():
    """二级端口在听、每次推理都 500（本机 Paddle+oneDNN 的实际形态）。

    价格链必须：计数 + 记错误 + 继续落三级；不许把异常冒到上层，也不许
    把它当成「这一块没字」。
    """
    tpl_hit = type("T", (), {"read_detail": staticmethod(
        lambda img: (250000, "250,000", 0.72))})()
    b = _chain([{"text": "150,000", "score": 0.42, "x": 0, "y": 0, "w": 80, "h": 20}],
               tpl=tpl_hit, pp_crash=True)
    assert b.read_price(np.zeros((32, 120, 3), np.uint8)) == 250000
    assert b.stats["tier2"] == 1 and b.stats["tier2_fail"] == 1
    assert "PirAttribute" in b.last_error
    assert "二级推理失败" in b.name()


def test_tier2_500_on_text_path_does_not_raise_or_lose_tier1():
    """普通文本路径同样只降级不抛异常；一级读到的字不许被二级的死拖累。"""
    b = _chain([], pp_crash=True)
    img = np.zeros((40, 160, 3), np.uint8)
    assert b.read_words(img) == []                 # 空结果，不是异常
    assert b.stats["tier2"] == 1 and b.stats["tier2_fail"] == 1

    words = [{"text": "稀有度极品", "score": 0.9, "x": 0, "y": 0, "w": 60, "h": 20}]
    b2 = _chain(words, pp_crash=True)
    assert b2.read_words(img) == words             # 一级有字就不该再去碰二级
    assert b2.stats["tier2"] == 0


def test_tier1_and_tier2_both_dead_still_raises_for_strict_callers():
    """一级崩 + 二级 500：再次往上抛，交给 ocr.read_text(raise_on_error=True)
    转成 OCRError —— 不能让「两层都瞎」被当成「没字」。"""
    b = _chain(rap_boom=True, pp_crash=True)
    try:
        b.read_words(np.zeros((40, 160, 3), np.uint8))
    except RuntimeError as e:
        assert "rapid" in str(e)
    else:
        raise AssertionError("两层都不可用时必须抛")
    assert b.stats["tier2_fail"] == 1


def test_selfcheck_pp_reports_configured_but_crashing_tier2():
    """启动自检要能区分「关掉」「没起」「在听但在崩」。"""
    b = _chain([{"text": "1", "score": 1.0, "x": 0, "y": 0, "w": 1, "h": 1}])
    ok, err = b.selfcheck_pp()
    assert ok is True and err == ""                # stub 没有 selfcheck → 视为可用

    b2 = _chain([], pp_crash=True)
    ok2, err2 = b2.selfcheck_pp()
    assert ok2 is False and err2
    assert "二级不可用" in b2.name()         # 自检阶段还没真实调用，不写「失败 0 次」


def test_name_shows_breaker_and_off_instead_of_missing_service():
    """available() 为假有三种原因：没起 / 关闭 / 熔断。状态栏不许一律写「未起」。

    本机 Paddle+oneDNN 的情形是第三种：端口在听、每次推理 500，连续三次后
    熔断。写成「服务未起」会让人去查进程，查了也查不出问题。
    """
    b = _chain([])
    b.pp.available = lambda: False
    b.pp.last_error = ("HTTPError: HTTP Error 500: Internal Server Error"
                       "（连续 3 次失败，本会话长时间停用 PP-OCR）")
    n = b.name()
    assert "二级已熔断" in n and "未起" not in n

    b2 = _chain([])
    b2.pp.available = lambda: False
    b2.pp.mode = "off"
    b2.pp.last_error = ""
    assert "二级已关闭" in b2.name()


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
