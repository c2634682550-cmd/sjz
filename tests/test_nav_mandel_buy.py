"""曼德尔砖下单层（mandel_buy）的页型与字段契约。

真机事故（2026-11-08）：曼德尔砖页签点「购买」后落到「指定价格购买」下单层，
`classify()` 没有这个页型的锚点 → 整页 unknown → 枚举读成 0 条 →
报告「进不去挂单列表」。看起来像没货，实际上是页型没识别。

下面的词框与拼串全部抄自真机帧（`mandel_3_buy.png`，1920x1080），
改 classify / 改字段解析会立刻被这些用例拦住。
"""
import numpy as np
import pytest

from core import market, mandel_buy, nav

# ---------------------------------------------------------------- 真机原文

# 帧③ 整屏拼串（unknown 的那一帧）
MANDEL_BUY_TEXT = (
    "CN UID:186668917734233179499_202611081432购买成交均价170挂单数"
    "曼德尔砖-王者之剑133,733指定价格购买81,477170十购买数量1十40,574"
    "25,689预期总花费17021,257挂单有效期24小时1701751801850190197170"
    "Esc返回Esc返回")


def W(text, x, y, w=40, h=30):
    return {"text": text, "x": x, "y": y, "w": w, "h": h}


# 帧③ 的词框（x/y/w 抄 OCR 输出，h 取同类字高）
MANDEL_BUY_WORDS = [
    W("购买", 357, 225, 123),
    W("曼德尔砖-王者之剑", 1110, 316, 146),
    W("成交均价", 458, 324, 72), W("170", 555, 323, 44),
    W("挂单数", 1010, 327, 58), W("133,733", 581, 375, 78),
    W("指定价格购买", 1141, 465, 106), W("170", 1289, 509, 44),
    W("购买数量", 1103, 557, 72), W("1", 1303, 594, 17),
    W("40,574", 829, 607, 70), W("25,689", 952, 645, 68),
    W("预期总花费", 1104, 643, 87), W("170", 1474, 642, 44),
    W("挂单有效期", 1103, 680, 88), W("24小时", 1455, 680, 64),
    W("81,477", 463, 504, 70), W("21,257", 708, 656, 67),
    # 底部档位
    W("170", 436, 739, 43), W("175", 517, 738, 63), W("180", 656, 738, 46),
    W("185", 778, 738, 46), W("190", 903, 739, 42), W("197", 1006, 738, 62),
    # 右下绿色按钮里的价格（不是档位，必须被 x 上限挡掉）
    W("170", 1303, 740, 42),
    W("Esc", 923, 934, 29), W("返回", 956, 932, 31),
]


class FakeImg:
    shape = (1080, 1920, 3)


# ---------------------------------------------------------------- 页型

def test_mandel_buy_classified():
    assert nav.classify(MANDEL_BUY_TEXT) == "mandel_buy"


def test_without_anchor_falls_to_unknown():
    """反证：删掉 mandel_buy 两条锚点 → 整页 unknown → 枚举 0 条。

    真机现象：mandel 页签报「进不去挂单列表」，后续条目全部跳过。
    """
    saved = nav._PAGE_ANCHORS
    try:
        nav._PAGE_ANCHORS = [a for a in saved if a[0] != "mandel_buy"]
        assert nav.classify(MANDEL_BUY_TEXT) == "unknown"
    finally:
        nav._PAGE_ANCHORS = saved


def test_market_pages_includes_mandel_buy():
    """停在 mandel_buy 时 enter_market 不该再按一次 F4。"""
    assert "mandel_buy" in nav.MARKET_PAGES


# ---------------------------------------------------------------- 页签映射

def test_interface_map():
    assert nav.TAB_INTERFACE == {"collection": "onsale", "charm": "onsale",
                                 "mandel": "mandel_buy"}
    assert nav.assert_interface("mandel", "mandel_buy") == "mandel_buy"
    assert nav.assert_interface("collection", "onsale") == "onsale"


def test_interface_mismatch_raises():
    """已知页签 + 错界面 = 硬错，绝不静默降级成「没货」。"""
    with pytest.raises(nav.InterfaceMismatch):
        nav.assert_interface("mandel", "onsale")
    with pytest.raises(nav.InterfaceMismatch):
        nav.assert_interface("collection", "mandel_buy")
    with pytest.raises(nav.InterfaceMismatch):
        nav.assert_interface("charm", "unknown")
    with pytest.raises(nav.InterfaceMismatch):
        nav.assert_interface("nope", "onsale")


# ---------------------------------------------------------------- 字段

def test_mandel_buy_fields(monkeypatch):
    monkeypatch.setattr(mandel_buy.ocr, "read_words", lambda img: MANDEL_BUY_WORDS)
    out = mandel_buy.read(FakeImg())
    assert out["page_type"] == "mandel_buy"
    assert out["levels"] and [p for p, _x in out["levels"]] == [170, 175, 180, 185, 190, 197]
    assert out["min_price"] == 170
    assert out["expected_total"] == 170
    assert out["qty"] == 1
    assert out["validity"] == "24小时"
    assert out["avg_price"] == 170          # 参考值，不是可买价
    assert out["title"] == "曼德尔砖-王者之剑"
    # 柱顶标签五个（第六根柱真机没读到标签）
    assert sorted(v for v, _x in out["counts_raw"]) == [21257, 25689, 40574, 81477, 133733]
    assert all(price in (170, 175, 180, 185, 190, 197) for price, _v in out["counts"])


def test_mandel_buy_drops_ocr_noise(monkeypatch):
    """反证：档位前的三角币图标被读成 `0` 时，最低价不许变 0。

    真机首轮就是 `[170, 175, 0, 180, 185, 190, 196]` → min=0。剔噪后 0 必须
    被丢掉，最低价仍是 170。
    """
    noisy = MANDEL_BUY_WORDS + [W("0", 600, 739, 18)]
    monkeypatch.setattr(mandel_buy.ocr, "read_words", lambda img: noisy)
    out = mandel_buy.read(FakeImg())
    assert out["min_price"] == 170
    assert 0 not in [p for p, _x in out["levels"]]


def test_mandel_buy_no_ladder_returns_none(monkeypatch):
    """读不出档位 → min_price None，绝不写 0 冒充最低价。"""
    words = [w for w in MANDEL_BUY_WORDS
             if not (730 <= w["y"] <= 748 and w["x"] < 1100)]
    monkeypatch.setattr(mandel_buy.ocr, "read_words", lambda img: words)
    out = mandel_buy.read(FakeImg())
    assert out["levels"] == []
    assert out["min_price"] is None


# ---------------------------------------------------------------- 分派

def test_scan_tab_dispatch(monkeypatch):
    monkeypatch.setattr(mandel_buy.ocr, "read_words", lambda img: MANDEL_BUY_WORDS)
    out = market.scan_tab("mandel", "mandel_buy", FakeImg())
    assert out["page_type"] == "mandel_buy" and out["min_price"] == 170

    monkeypatch.setattr(market, "scan", lambda img, **kw: [{"price": 10}])
    out = market.scan_tab("collection", "onsale", np.zeros((2, 2, 3), np.uint8))
    assert out["page_type"] == "onsale" and out["rows"] == [{"price": 10}]


def test_scan_tab_rejects_mismatch():
    with pytest.raises(nav.InterfaceMismatch):
        market.scan_tab("mandel", "onsale", FakeImg())
    with pytest.raises(nav.InterfaceMismatch):
        market.scan_tab("collection", "mandel_buy", FakeImg())
