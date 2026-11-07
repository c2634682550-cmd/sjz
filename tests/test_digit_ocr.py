"""`core.digit_ocr`：千分位解析、图标剔除、切分、字模匹配、灰度/二值。

这个模块是「毫秒级数字 OCR」的兜底路径，价格/倒计时全靠它。以前零覆盖：
认错单位（把 2049 读成 204）、把金币当 0、把 `2,04` 当 204 都会直接
影响出价 —— 所以这里对**错误读数**敏感，而不是只跑一遍 happy path。
"""
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import digit_ocr as d  # noqa: E402


# ---------------------------------------------------------------- 数字解析

@pytest.mark.parametrize("token, want", [
    ("2049", 2049),
    ("2,049", 2049),
    ("2.049", 2049),          # 句点也当千分位
    ("12,345,678", 12345678),
    ("0", 0),
    (" 2049 ", 2049),
    ("2,04", None),           # 分组不是 3 位：宁可读不出也不猜
    ("2,0499", None),
    ("1234567890", None),     # 10 位超过价格上限
    ("", None),
    ("   ", None),
    ("abc", None),
    ("2,049,", None),
])
def test_parse_token(token, want):
    assert d._parse_token(token) == want


# ---------------------------------------------------------------- 图标剔除

def test_drop_icons_removes_circles_and_border_strips():
    binary = np.zeros((120, 100), np.uint8)
    boxes = [
        (5, 5, 10, 20),       # 数字：保留
        (40, 40, 30, 30),     # 1:1 圆（金币）
        (0, 0, 99, 111),      # 贴边整条边框
    ]
    assert d._drop_icons(boxes, binary) == [(5, 5, 10, 20)]


def test_drop_icons_removes_wide_icons_but_keeps_digits():
    binary = np.zeros((120, 100), np.uint8)
    boxes = [(5, 5, 10, 20), (20, 5, 10, 20), (35, 5, 10, 20), (50, 5, 60, 18)]
    assert d._drop_icons(boxes, binary) == [(5, 5, 10, 20), (20, 5, 10, 20), (35, 5, 10, 20)]


def test_drop_icons_empty():
    assert d._drop_icons([], np.zeros((10, 10), np.uint8)) == []


# ---------------------------------------------------------------- 切分

def test_segment_finds_two_glyphs_and_sorts_by_x():
    binary = np.zeros((100, 60), np.uint8)
    binary[10:40, 30:40] = 255      # 右边的数字（x 更大）
    binary[10:40, 10:20] = 255      # 左边的数字
    boxes = d.segment(binary)
    assert boxes == [(10, 10, 10, 30), (30, 10, 10, 30)]


def test_segment_drops_speckles():
    binary = np.zeros((100, 60), np.uint8)
    binary[10:40, 10:20] = 255
    binary[90:91, 50:51] = 255      # 1px 噪点
    assert d.segment(binary) == [(10, 10, 10, 30)]


# ---------------------------------------------------------------- 匹配

def _templates():
    out = []
    for ch in "0123456789,":
        img = d._render_char(ch, 1)
        h, w = img.shape[:2]
        out.append((ch, d._canon(img), float(w) / float(h)))
    return out


def test_match_char_detail_matches_the_same_glyph():
    tpl = _templates()
    seven = d._render_char("7", 1)
    ch, score, _margin = d.match_char_detail(seven, tpl)
    assert ch == "7"
    assert score > 0.9


def test_match_char_detail_does_not_confuse_0_and_8():
    tpl = _templates()
    assert d.match_char_detail(d._render_char("8", 1), tpl)[0] == "8"
    assert d.match_char_detail(d._render_char("0", 1), tpl)[0] == "0"


def test_blank_patch_matches_nothing():
    tpl = _templates()
    assert d.match_char_detail(np.zeros((20, 10), np.uint8), tpl) == ("", 0.0, 0.0)


# ---------------------------------------------------------------- 灰度 / 二值

def test_to_gray_accepts_all_layouts():
    gray = np.zeros((4, 5), np.uint8)
    assert d.to_gray(gray) is gray
    assert d.to_gray(np.zeros((4, 5, 3), np.uint8)).shape == (4, 5)
    assert d.to_gray(np.zeros((4, 5, 4), np.uint8)).shape == (4, 5)


def test_to_gray_rejects_none():
    with pytest.raises(d.DigitOcrError):
        d.to_gray(None)


def test_binarize_makes_text_white_on_black():
    gray = np.full((60, 140), 255, np.uint8)
    cv2.putText(gray, "2049", (8, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 2, cv2.LINE_AA)
    binary = d.binarize(gray)
    assert binary[0, :].sum() == 0, "没有补 2px 边框"
    nz = int(np.count_nonzero(binary))
    assert nz > 0.005 * binary.size, "文字极性反了（文字应该是 255）"
    assert nz < 0.5 * binary.size, "背景被当成了前景"


def test_binarize_blank_image_stays_blank():
    assert np.count_nonzero(d.binarize(np.zeros((20, 20), np.uint8))) == 0


# ---------------------------------------------------------------- read_detail

def test_read_detail_rejects_degenerate_box():
    img = np.zeros((50, 50), np.uint8)
    assert d.read_detail(img, box=(20, 20, 20, 30)) == (None, "", 0.0)
    assert d.read_detail(img, box=(100, 100, 120, 120)) == (None, "", 0.0)


def _compose_number(text, thickness=2, gap=6, pad=4):
    """把字模按字形拼成一整行数字（白字黑底），模拟真实的截取条。"""
    glyphs = [d._render_char(c, thickness) for c in text]
    h = max(g.shape[0] for g in glyphs)
    w = sum(g.shape[1] for g in glyphs) + gap * (len(glyphs) - 1)
    canvas = np.zeros((h + 2 * pad, w + 2 * pad), np.uint8)
    x = pad
    for g in glyphs:
        gh, gw = g.shape[:2]
        y = pad + (h - gh)
        canvas[y:y + gh, x:x + gw] = g
        x += gw + gap
    return canvas


def test_read_detail_reads_a_rendered_number():
    """真跑一遍：字形灰度 → 二值 → 切分 → 匹配 → 解析。"""
    # 用粗一档的字模（thickness=2），模板是 thickness=1 —— 形状相近但不是同一个图，
    # 能测出「匹配」而不是「同图异或」。
    canvas = _compose_number("207")
    value, token, score = d.read_detail(canvas, templates=_templates())
    assert value == 207, f"读数错误：value={value} token={token!r} score={score:.2f}"
    assert token == "207"


def test_read_detail_returns_fallback_when_rejected():
    """置信度门槛拉满 → 宁可返回 None，也不能硬猜一个数。"""
    value, _token, _score = d.read_detail(_compose_number("207"),
                                          templates=_templates(), min_conf=0.999)
    assert value is None


def test_digit_reader_instance_is_a_singleton():
    assert d.DigitReader.instance() is d.DigitReader.instance()
