"""卡片定位与逐卡认字：`core/cards.py` + `market.Scanner` 的帧间缓存。

这些测试盯的是**真机会踩的坑**，不是「函数能跑」：

- 卡片贴着画面底边时，文字块上下留的边不能靠裁（裁掉等于没留，
  WinRT 对紧贴文字的一条直接返回空串）；
- 2000x70 这种极扁的价格行要补高，否则 OCR 空串；
- 相邻帧只有一张卡变了，就不能把六张卡全重认一遍。
"""

import cv2
import numpy as np
import pytest

from core import cards, market, vision

FIXTURE = "tests/fixtures/market/list_unfiltered.png"


@pytest.fixture(scope="module")
def shot():
    img = vision.imread(FIXTURE)
    if img is None:
        pytest.skip("缺少列表截图夹具")
    return img


def fake_ocr(tall_means_name=True):
    """照抄真机行为：高的条是名称行，扁的条是成色+价格行。"""
    calls = []

    def read(piece, **_kw):
        calls.append(piece.shape[:2])
        h, w = piece.shape[:2]
        if h * 3 >= w:
            return "QBZ95 · 1 突击步枪 · 王牌之剑"
        return "极品 | 成色 A 12,000"

    read.calls = calls
    return read


def test_locates_six_cards_in_a_two_by_three_grid(shot):
    found = cards.locate_cards(shot)
    assert len(found) == 6, f"应当六张卡，实际 {len(found)}：{found}"
    xs = sorted({x0 for x0, _, _, _ in found})
    assert len(xs) == 2, f"两列，实际列起点 {xs}"


def test_every_card_crop_stays_inside_the_picture(shot):
    h, w = shot.shape[:2]
    for card in cards.locate_cards(shot):
        reg = cards.card_regions(shot, card, upscale=1)
        for key, box in reg["boxes"].items():
            x0, y0, x1, y1 = box
            assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h, f"{key} 越界 {box}"


def test_last_row_reaches_the_picture_bottom(shot):
    """最后一行的卡片矩形必须补到画面底边。

    真机踩过：行距是按上一行的间距推的，最后一行会短二十来像素，正好把
    底行的「极品 | 成色 C  9,000」切掉一半 → 这张卡永远读不出价格。
    """
    h = shot.shape[0]
    found = cards.locate_cards(shot)
    assert found, "夹具里应当能找到卡片"
    assert max(y1 for _, _, _, y1 in found) == h,         f"最后一行没补到画面底边（图高 {h}）：{found}"


def test_bottom_band_reaches_past_the_card_edge(shot):
    """底行要切到卡片底边、还要再往下看一点。

    真机上「成色 S / 2,3xx」正好压在行分隔线上，只切到卡片底边会把字的下半截
    切掉，OCR 直接返回空串（实测整条底行认不出来）。
    """
    for card in cards.locate_cards(shot):
        box = cards.card_regions(shot, card, upscale=1)["boxes"]["bottom"]
        assert box[3] >= card[3], "底行没切到卡片底边"
        assert box[3] <= card[3] + 14, f"底行往下看得太多：{box} vs 卡片 {card}"


def test_very_wide_strips_get_padded_before_ocr():
    strip = np.full((40, 2000, 3), 40, np.uint8)
    out = cards._pad_aspect(strip, min_ratio=10)
    assert out.shape[0] >= 200, f"2000x40 的条应当补到 200 高，实际 {out.shape}"


def test_bottom_crop_is_never_a_hairline(shot):
    """底行切出来不能是「1200x96」这种极扁的条 —— WinRT 会直接给空串。

    夹具上的卡正好够高，所以另造一张很宽的卡来逼出补边逻辑。
    """
    for card in cards.locate_cards(shot):
        piece = cards.card_regions(shot, card, upscale=3)["bottom"]
        h, w = piece.shape[:2]
        assert w / h <= 10.5, f"底行 {w}x{h} 太扁"

    wide = np.full((300, 1200, 3), 30, np.uint8)
    cv2.putText(wide, "极品 | 成色 A  12,000", (40, 270),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (235, 235, 235), 1)
    piece = cards.card_regions(wide, (0, 0, 1200, 300), upscale=3)["bottom"]
    h, w = piece.shape[:2]
    assert w / h <= 10.5, f"宽卡片的底行 {w}x{h} 没补边"


def test_changed_cards_only_reports_the_one_that_moved():
    a = np.zeros((100, 100, 3), np.uint8)
    b = a.copy()
    rects = [(0, 0, 100, 100)]
    assert cards.changed_cards(None, a, rects) == [0], "第一帧全都算变了"
    assert cards.changed_cards(a, b, rects) == [], "同一张图不该算变"
    b2 = b.copy()
    b2[10:20, 10:20] = 255
    assert cards.changed_cards(a, b2, rects) == [0]


def test_scanner_reads_a_card_once_until_it_changes(shot):
    read = fake_ocr()
    sc = market.Scanner(read_text=read)
    first = sc.scan(shot)
    assert len(first) == 6
    assert all(r["price"] == 12000 for r in first), [r["price"] for r in first]
    first_calls = len(read.calls)

    # 同一帧再来一次：一张都没变，不该再花一次 OCR
    second = sc.scan(shot.copy())
    assert len(second) == 6
    assert len(read.calls) == first_calls, "没变的卡不该重认"
    assert [r["price"] for r in second] == [r["price"] for r in first]


def test_scanner_refreshes_only_the_changed_card(shot):
    read = fake_ocr()
    sc = market.Scanner(read_text=read)
    sc.scan(shot)
    before = len(read.calls)

    moved = shot.copy()
    cards_ = cards.locate_cards(shot)
    x0, y0, x1, y1 = cards_[0]
    moved[y0 + 40:y0 + 90, x0 + 20:x1 - 20] = 255      # 第一张卡换货了
    sc.scan(moved)

    extra = len(read.calls) - before
    assert extra <= 2, f"只该重认第一张卡（名称+底行两次），实际多认了 {extra} 次"


def test_scanner_forgets_everything_after_reset(shot):
    read = fake_ocr()
    sc = market.Scanner(read_text=read)
    sc.scan(shot)
    sc.reset()
    before = len(read.calls)
    sc.scan(shot)
    assert len(read.calls) - before >= 6, "刷新列表后必须重认，不能拿旧缓存"


def _fake_bands(monkeypatch, texts):
    """把 card_bands 换掉，只测「认出来的文字怎么分到名称/成色/价格」。"""
    pieces = [{"box": (0, 0, 10, 10), "piece": k} for k in texts]
    monkeypatch.setattr(cards, "card_bands", lambda *a, **k: pieces)
    return lambda piece: texts[piece]


def test_price_never_comes_from_the_name_band(monkeypatch):
    """名称里的型号数字（M4A1 → 41）绝不能当价格。

    真机踩过：成色那条带里没认出数字时，退而去「有数字的那条」，
    结果把名称的 M4A1 读成 41，六张卡全是 41。
    """
    read = _fake_bands(monkeypatch, {"a": "M4A1 突击步枪 · 雪地迷彩", "b": "成色 A"})
    got = cards.read_card(np.zeros((4, 4, 3), np.uint8), (0, 0, 4, 4), read_text=read)
    assert got["price"] == "", f"把名称当价格了：{got['price']!r}"
    assert got["grade"] == "成色 A"


def test_price_in_the_grade_band_must_look_like_a_price(monkeypatch):
    """成色带里的**残数字**（「05」）不能当价格；带里的真价格可以。

    回归（录屏实测）：卡片含「成色 S 05」时整条带被当价格 → price=5 →
    pick() 以为这条最便宜，点进去买的反而是贵的。05 是成色带里的残数字
    （真·两个三角币的挂单价格会单独成一条带，走 _price_from_block）。
    """
    read = _fake_bands(monkeypatch, {"a": "成色 S 05", "b": "M4A1 突击步枪 · 雪地迷彩"})
    got = cards.read_card(np.zeros((4, 4, 3), np.uint8), (0, 0, 4, 4), read_text=read)
    assert got["price"] == "", f"把成色带里的残数字当价格了：{got['price']!r}"
    assert market._price_from_block(got["price"]) is None

    read2 = _fake_bands(monkeypatch, {"a": "极品 | 成色 S 14,000",
                                      "b": "M4A1 突击步枪 · 雪地迷彩"})
    got2 = cards.read_card(np.zeros((4, 4, 3), np.uint8), (0, 0, 4, 4),
                           read_text=read2)
    assert got2["price"] == "14,000"
    assert market._price_from_block(got2["price"]) == 14000


def test_short_prices_are_real_prices():
    """真机上有 2 个三角币的挂单（用户确认），不能因为位数少就当读错。"""
    assert market._price_from_block("成色 S 05") == 5
    assert market._price_from_block("025,000") == 25000
    assert market._price_from_block("9 ， 500") == 9500
    assert market._price_from_block("成色 A") is None
    assert market._price_from_block("0") is None
