# -*- coding: utf-8 -*-
"""详情面板 ROI 路由：几何判定、四块读数、开关与回退。

真模型跑（同 test_skin_ocr_real），一组约 5 秒；只覆盖「面板读数对不对」，
耗时性能归 perf/bench 管。
"""
import os

import cv2
import numpy as np
import pytest

from core import bricks, ocr, ocr_chain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "tests", "fixtures", "market", "detail_panel.png")
LIST = os.path.join(ROOT, "tests", "fixtures", "market", "list_unfiltered.png")


def _imread(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


@pytest.fixture(scope="module")
def panel():
    return _imread(PANEL)


@pytest.mark.skipif(not os.path.exists(PANEL), reason="缺详情面板素材")
def test_panel_like_only_vertical_panel(panel):
    assert ocr._panel_like(panel)
    if os.path.exists(LIST):
        assert not ocr._panel_like(_imread(LIST))
    assert not ocr._panel_like(np.zeros((1080, 1920, 3), np.uint8))


@pytest.mark.skipif(not os.path.exists(PANEL), reason="缺详情面板素材")
def test_panel_rois_read_price_craft_wear(panel):
    """四块拼条读出来的价格 / 品阶 / 成色必须与整幅读一致。"""
    words = ocr._client().read_panel(panel)
    text = "\n".join(w["text"] for w in words)
    assert ocr_chain.price_from_text(text)[0] == 25000
    assert bricks.parse_craft(text) == "极品"
    assert bricks.parse_wear(text) == "A"
    # 拼条坐标要还原回面板坐标系（名称在顶部、价格在底部）
    fields = {w.get("field"): w for w in words}
    assert fields["name"]["y"] < 100
    assert fields["price"]["y"] > 700


@pytest.mark.skipif(not os.path.exists(PANEL), reason="缺详情面板素材")
def test_read_words_routes_panel_and_switch_off(panel):
    """默认走四块；ocr.detail_panel_roi=false 时退回整幅（行数明显更多）。"""
    words = ocr.read_words(panel)
    assert len(words) <= 6

    ocr.reload()
    ocr._cfg()["detail_panel_roi"] = False
    try:
        whole = ocr.read_words(panel)
        assert len(whole) > 10
    finally:
        ocr.reload()


@pytest.mark.skipif(not os.path.exists(PANEL), reason="缺详情面板素材")
def test_panel_blank_frame_falls_back(panel):
    """竖版但没字的帧：拼条读不出四条 → 退回整幅，不能丢成「面板失灵」。"""
    blank = np.zeros_like(panel)
    assert ocr.read_words(blank) == []


# ---------------------------------------------------------------- 静默错价

PRICE_CROP = os.path.join(ROOT, "tests", "fixtures", "price_roi", "u3_detail_price.png")


def _word(text, field, y=0):
    return {"text": text, "field": field, "x": 0, "y": y, "w": 80, "h": 20,
            "score": 0.99}


def test_price_block_never_borrows_count_block():
    """价格块被切坏时，绝不能拿同帧数量块的数顶替（静默错价）。"""
    from core import money
    # 块内口径：切掉末位后千分位不过 → 拒识，不回落到别的数字
    assert money.scan_block("1,000,00")[0] is None
    assert money.scan_block("1,000,000")[0] == 1000000

    words = [_word("1,000,00", "price", 780), _word("63", "count", 470)]
    assert ocr_chain.price_from_words(words)[0] is None      # 不跨块顶替
    # 没有 field 标记（来源不明确）→ 拒识，不许猜
    assert ocr_chain.price_from_words([_word("1,000,00", None, 780),
                                       _word("63", None, 470)])[0] is None


def test_price_equal_count_is_untrusted():
    """价格与同帧数量同值且来源不明确 → 判不可信，回退。"""
    words = [_word("63", "price", 780), _word("63", "count", 470)]
    assert ocr_chain.price_from_words(words)[0] is None


def test_price_block_edge_noise_trimmed_but_truncation_not():
    """面板描边被读成 `]` `|` 只是行尾杂字；真截断（少一位）必须继续拒识。"""
    from core import money
    assert money.scan_block("1,000,000]")[0] == 1000000
    assert money.scan_block("1,000,000|")[0] == 1000000
    assert money.scan_block("1,000,00|")[0] is None     # 少一位 → 不猜
    assert money.scan_block("1,000,")[0] is None        # 尾逗号不削成 1,000


def test_panel_read_ok_requires_core_blocks():
    """名称 / 成色 / 价格 / 数量缺任一 → 回退；稀有度是可选补充块。"""
    ok = [_word("武器名", "name", 30), _word("极品", "rarity", 390),
          _word("A( 0.80)", "grade", 440), _word("63", "count", 470),
          _word("1,000,000", "price", 780)]
    assert ocr._panel_read_ok(ok)
    assert ocr._panel_read_ok([w for w in ok if w["field"] != "rarity"])  # 卡片版式
    assert not ocr._panel_read_ok([w for w in ok if w["field"] != "name"])
    assert not ocr._panel_read_ok([w for w in ok if w["field"] != "grade"])
    assert not ocr._panel_read_ok([w for w in ok if w["field"] != "count"])
    broken = [w for w in ok if w["field"] != "rarity"] + [_word("1,000,00", "price", 780)]
    assert not ocr._panel_read_ok(broken[:4] + broken[-1:])  # 价格读不出 → 回退


@pytest.mark.skipif(not (os.path.exists(PANEL) and os.path.exists(PRICE_CROP)),
                    reason="缺详情面板 / 价格裁片素材")
def test_one_million_never_reads_as_count(panel):
    """1,000,000 必须读成 1,000,000 或 None，绝不能读成 63 / 3,999,999。"""
    crop = _imread(PRICE_CROP)
    img = panel.copy()
    # 先盖掉原价签（25000）的墨迹，再按真机位置（价签中心 316,782）贴 1,000,000
    img[758:808, 245:390] = img[758:768, 245:390].mean(axis=0)
    ch, cw = crop.shape[:2]
    x, y = 316 - cw // 2, 782 - ch // 2
    img[y:y + ch, x:x + cw] = crop

    words = ocr.read_words(img)
    value, _tok = ocr_chain.price_from_words(words)
    legacy = ocr_chain.price_from_text("\n".join(w["text"] for w in words))[0]
    assert value in (None, 1000000), f"分块取价读成了 {value}"
    assert legacy in (None, 1000000), f"整串取价读成了 {legacy}"
