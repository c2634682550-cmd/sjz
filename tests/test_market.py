"""market.py 的回归测试。

这些断言全部来自实测截图和 Windows OCR 的真实输出现象，不是构造的示例：
每一条都对应一个曾经让程序买错东西（或筛不出东西）的 bug。

跑法：uv run pytest
"""
import os
import sys

import cv2
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import market  # noqa: E402

# 真实截图（列表 + 详情，1920x1080）。缺了就跳过依赖图片的用例。
SHOT = (r"D:\backup-mirror\WX\xwechat_files\wxid_li981uy64bcc22_c299"
        r"\temp\RWTemp\2026-10\69c47abfd01c41dd2978455fbd6e24a2"
        r"\cdbf41309910276bb486f2c9aeac020f.png")
RECT = (90, 229, 1399, 839)   # 列表 ROI：engine.py 里的 list_px

# 截图里肉眼可见的六件商品，从上到下、从左到右
REAL_PRICES = [15000, 15252, 13333, 32888, 10000, 8888]
REAL_WEARS = ["A", "A", "S", "S", "B", "B"]


def _roi():
    img = cv2.imread(SHOT)
    if img is None:
        pytest.skip("测试截图不存在")
    x0, y0, x1, y1 = RECT
    return img[y0:y1, x0:x1]


# ---------------------------------------------------------------- 价格解析

@pytest.mark.parametrize("text,want", [
    ("15,000", 15000),            # 逗号当千位分隔符，不是小数点
    ("9,999,999", 9999999),       # 多个千位逗号
    ("2,050", 2050),
    ("¥15,000", 15000),           # 带货币符号
    ("15 000", 15000),            # 逗号被 OCR 换成空格
    ("13 333", 13333),
    ("15 囤 000", 15000),         # 逗号被 OCR 认成汉字
    ("015 252", 15252),           # 放大后常见的 6 位无逗号形式
])
def test_price_with_thousands_separator(text, want):
    """千位逗号必须去掉。

    本地回归：早期版本 `tok.replace(",", ".")` 把 `15,000` 读成 15.0，
    所有价格错 1000 倍；`check` 的 price <= max_price 于是永远成立。
    """
    assert market.parse_row(text)["price"] == want


def test_wear_value_is_not_a_price():
    """小数是磨损，不能当价格。截图详情页的 `0.583172`。"""
    row = market.parse_row("成色 A( 0.583172 )")
    assert row["price"] is None
    assert row["wear_value"] == pytest.approx(0.583172)


def test_price_absent_stays_none():
    """认不出必须是 None，不能默认成 0 —— 0 会让所有价格上限检查通过。"""
    row = market.parse_row("Vector 冲锋枪美杜莎")
    assert row["price"] is None


# ---------------------------------------------------------------- 成色字母

@pytest.mark.parametrize("text,want", [
    ("极品丨成色 A 15000", "A"),    # 实际渲染带竖线
    ("极品成色 S 013333", "S"),
    ("Vector 冲锋枪美杜莎极品 | 成色 B 8888", "B"),
    ("优品 - C", "C"),
])
def test_wear_letter(text, want):
    """做工和成色字母之间隔着 `丨 成色 ` 这类字，正则必须容许。

    本地回归：早期正则要求两者紧邻，实测 6/6 张卡片 wear=None，
    于是任何按成色的过滤都筛不出东西。
    """
    assert market.parse_row(text)["wear"] == want


def test_wear_letter_does_not_eat_prices():
    """字母不能从无关文本里误抓，价格里的数字也不能当字母。"""
    assert market.parse_row("15000")["wear"] is None


# ---------------------------------------------------------------- 卡片切分

@pytest.mark.skipif(not os.path.exists(SHOT), reason="测试截图不存在")
def test_scan_splits_six_cards():
    """一张截图 = 六张卡片，不是两行文字块。

    本地回归：卡片是「名字行 + 属性行」的上下两段布局，卡内中心距 334px
    反而大于跨卡距离 76px，任何按间隙聚类的方法都会把两行并成一张卡片、
    把名字和价格拆到不同记录里。改用固定网格（206px 卡高 + 两列）。
    """
    rows = market.scan(_roi())
    assert len(rows) == 6, [r["text"][:40] for r in rows]


@pytest.mark.skipif(not os.path.exists(SHOT), reason="测试截图不存在")
def test_scan_prices_and_wears_match_the_screenshot():
    """每张卡片的字段要和肉眼所见一致，且同一条记录里既有名字又有价格。

    名字和价格必须同记录：check() 在 price is None 时直接拒绝，
    被拆开的卡片会让整个筛选失效。
    """
    rows = market.scan(_roi())
    assert [int(r["price"]) for r in rows] == REAL_PRICES
    assert [r["wear"] for r in rows] == REAL_WEARS
    for r in rows:
        assert "Vector" in r["text"] and "美杜莎" in r["text"]


@pytest.mark.skipif(not os.path.exists(SHOT), reason="测试截图不存在")
def test_scan_coordinates_land_inside_the_card():
    """点击目标必须落在卡片矩形内 —— engine.py:363 用 cy 和 x0 定位。"""
    rows = market.scan(_roi())
    for r in rows:
        cx, cy = r["x0"] + 40, r["cy"]
        assert r["x0"] <= cx <= r["x1"]
        assert r["y0"] <= cy <= r["y1"]


# ---------------------------------------------------------------- 筛选

@pytest.mark.skipif(not os.path.exists(SHOT), reason="测试截图不存在")
def test_pick_respects_the_price_cap():
    """价格上限内最便宜的一件被选中：六件里 <=13000 的只有 10000 和 8888。"""
    rows = market.scan(_roi())
    task = {"name": "Vector 冲锋枪-美杜莎", "keywords": ["美杜莎"],
            "max_price": 13000, "sort": "price"}
    row, why, diag = market.pick(rows, task)
    assert row is not None, why
    assert row["price"] == 8888
    assert sum(1 for d in diag if d["ok"]) == 2


@pytest.mark.skipif(not os.path.exists(SHOT), reason="测试截图不存在")
def test_pick_rejects_a_grade_above_the_cap():
    """要 S 成色、价格 <=13000：两件便宜的都不是 S，必须一件不选。"""
    rows = market.scan(_roi())
    task = {"name": "Vector 冲锋枪-美杜莎", "keywords": ["美杜莎"],
            "max_price": 13000, "wear": "S"}
    row, why, diag = market.pick(rows, task)
    assert row is None
    assert any("成色" in d["why"] for d in diag)


@pytest.mark.skipif(not os.path.exists(SHOT), reason="测试截图不存在")
def test_pick_never_selects_an_over_budget_item():
    """上限低于所有价格时一件都不能选 —— 选错就是买贵了。"""
    rows = market.scan(_roi())
    task = {"name": "Vector 冲锋枪-美杜莎", "keywords": ["美杜莎"],
            "max_price": 5000}
    row, why, _ = market.pick(rows, task)
    assert row is None


# ---------------------------------------------------------------- OCR 后端自检

def test_backend_name_reports_the_real_engine():
    """backend_name() 是打包后排查 OCR 的唯一手段，必须真的建起引擎。

    打包成 exe 后最隐蔽的故障是惰性 winrt 没被打进去：模板匹配主路径照常
    工作，只有 OCR 悄悄失败。所以这个自检不能只报「平台是 win32」。
    """
    from core import ocr
    name = ocr.backend_name()
    assert isinstance(name, str) and name
    if sys.platform == "win32":
        assert "WinRT" in name, f"OCR 后端没起来: {name}"
        assert "zh-Hans" in name or "语言" in name, name


def test_backend_name_matches_a_real_recognition():
    """自检说可用，就必须真的能识别出东西 —— 否则自检在骗人。"""
    from core import ocr
    if "WinRT" not in ocr.backend_name():
        pytest.skip("本机没有可用的 WinRT OCR")
    if not os.path.exists(SHOT):
        pytest.skip("测试截图不存在")
    # 截图右上角有商品名，随便一块有字的区域即可
    img = _roi()[0:120, 0:700]
    text = ocr.read_text(img)
    assert text.strip(), "自检报告可用，但识别结果是空的"
