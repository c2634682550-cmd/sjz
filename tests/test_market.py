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

# 真实截图走仓库里的 tests/fixtures（以前绑的是 D:ackup-mirror 下的一张
# 微信临时图，本机早没了 —— 那 7 条用例天天 skip，等于没有）。
# 整屏扫描/价格这类「要看图」的断言在 test_market_video.py 里用
# _shots/drv_onsale.png（1920×1080 真机截图）覆盖，这里只留解析与挑选。
SHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "market", "list_unfiltered.png")
RECT = (0, 0, 0, 0)      # 这张 fixture 本身就是列表 ROI，不用再裁


def _roi():
    img = cv2.imread(SHOT)
    if img is None:
        pytest.skip("测试截图不存在")
    return img


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


def test_dot_thousand_separator_is_a_price():
    """千位分隔符被认成句点：`9,000` → `9.000`。

    2026-11-01 换 PP-OCRv5 之后真机出现（Windows OCR 是丢逗号或认成汉字）。
    不处理的话 `_to_number` 把它当小数，9000 变 9，pick() 立刻挑中它去买。
    """
    assert market.parse_row("9.000极品|成色B")["price"] == 9000


def test_dot_separator_does_not_swallow_wear():
    """但磨损值（0~5 的小数）不能被当成价格 —— 判据是整数部分 > 5。"""
    row = market.parse_row("极品|成色B 2.500")
    assert row["price"] is None
    assert row["wear_value"] == pytest.approx(2.5)


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
#
# 「一张截图切出六张卡、价格成色全对、坐标落在卡内」这三条原来挂在这张
# 微信临时截图上，图没了就一直在 skip。同样的断言已经搬到
# test_market_video.py，用仓库里的 1920×1080 真机截图（drv_onsale.png）
# 跑，还多覆盖了「滚过一行之后仍然对」。这里不再重复。

# ---------------------------------------------------------------- 筛选

def _rows():
    """六条合成挂单，价格/成色和以前那张截图一致，但不依赖图片。

    以前这三条要从截图里扫出 rows 才能跑；图一没就全 skip。挑选逻辑
    （market.pick）本来只吃 row 字典，没必要为它准备一张截图。
    """
    data = [(15000, "A"), (15252, "A"), (13333, "S"),
            (32888, "S"), (10000, "B"), (8888, "B")]
    return [{"text": f"Vector 冲锋枪-美杜莎 {p}", "price": p, "wear": w,
             "wear_value": None, "craft": "极品", "x0": 10, "y0": 10 + i * 100,
             "cy": 40 + i * 100}
            for i, (p, w) in enumerate(data)]


def test_pick_respects_the_price_cap():
    """价格上限内最便宜的一件被选中：六件里 <=13000 的只有 10000 和 8888。"""
    task = {"name": "Vector 冲锋枪-美杜莎", "keywords": ["美杜莎"],
            "max_price": 13000, "sort": "price"}
    row, why, diag = market.pick(_rows(), task)
    assert row is not None, why
    assert row["price"] == 8888
    assert sum(1 for d in diag if d["ok"]) == 2


def test_pick_rejects_a_grade_above_the_cap():
    """要 S 成色、价格 <=13000：两件便宜的都不是 S，必须一件不选。"""
    task = {"name": "Vector 冲锋枪-美杜莎", "keywords": ["美杜莎"],
            "max_price": 13000, "wear": "S"}
    row, why, diag = market.pick(_rows(), task)
    assert row is None
    assert any("成色" in d["why"] for d in diag)


def test_pick_never_selects_an_over_budget_item():
    """上限低于所有价格时一件都不能选 —— 选错就是买贵了。"""
    task = {"name": "Vector 冲锋枪-美杜莎", "keywords": ["美杜莎"],
            "max_price": 5000}
    row, why, _ = market.pick(_rows(), task)
    assert row is None


# ---------------------------------------------------------------- OCR 后端自检

def test_backend_name_reports_the_real_engine():
    """backend_name() 是打包后排查 OCR 的唯一手段，必须说清是谁在干活。

    2026-11-01 起只剩 PP-OCRv6 一个后端（走本机服务），所以这里钉两件事：
    名字里必须写明是 PP-OCR，且不可用时必须**明说**不可用 —— 不许出现
    「名字看着正常、其实一直读空」的情况。
    """
    from core import ocr
    name = ocr.backend_name()
    assert isinstance(name, str) and name
    if "不可用" in name:
        return          # 服务没起是合法状态，只要它老老实实报不可用
    assert "PP-OCR" in name, name
    assert "127.0.0.1" in name or "http" in name, name


def test_backend_name_matches_a_real_recognition():
    """自检说可用，就必须真的能识别出东西 —— 否则自检在骗人。"""
    from core import ocr
    if "不可用" in ocr.backend_name():
        pytest.skip(f"OCR 后端不可用：{ocr.backend_name()}")
    if not os.path.exists(SHOT):
        pytest.skip("测试截图不存在")
    # 截图右上角有商品名，随便一块有字的区域即可
    img = _roi()[0:120, 0:700]
    text = ocr.read_text(img)
    assert text.strip(), "自检报告可用，但识别结果是空的"
