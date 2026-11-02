"""录屏实测的回归测试：交易行列表页的解析。

数据来自用户提供的 45 秒录屏（1920x1080），逐帧 OCR 后取了两张有代表性的
列表截图存成 `tests/fixtures/market/`。每一条断言都对应录屏里肉眼可见的事实，
不是构造的示例。

这里钉住的两个缺陷都会导致**买错东西**：

1. 型号里的数字被当成价格（`QBZ95-1` → 951、`M250` → 250、`M7` → 7）。
   `pick()` 按价格升序挑最便宜的，于是读不出真价格的行永远排在真挂单前面。
2. 卡片切分错位（网格顶没对齐名字行）→ 价格和名字落到不同记录里，
   `check()` 因 price is None 拒绝一切，或者把相邻两卡的数字混在一起。

截图里的六件商品（价格 / 做工 / 成色），肉眼逐条核对过。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import market  # noqa: E402
from core.vision import imread  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "market")

# 未筛选：QBZ95-1 王牌之剑，六件挂单（价格 / 做工 / 成色），与截图逐条核对
UNFILTERED = [
    (25000, "极品", "A"),
    (17000, "极品", "B"),
    (28888, "极品", "B"),
    (16000, "极品", "S"),
    (9000, "极品", "C"),
    (9500, "极品", "B"),
]

FILTERED = [
    (2050, "优品", "C"),
    (2050, "优品", "B"),
    (2050, "优品", "B"),
    (2050, "优品", "B"),
    (2100, "优品", "B"),
    (2500, "优品", "B"),
]


def _load(name):
    path = os.path.join(FIXTURES, name + ".png")
    if not os.path.isfile(path):
        pytest.skip(f"缺少录屏截图 {name}.png")
    img = imread(path)
    if img is None:
        pytest.skip(f"截图读不出来 {name}.png")
    return img


def test_unfiltered_list_prices_and_wears():
    """六件挂单的价格和成色要和截图一致。

    这张图专门钉「型号数字被当成价格」：`QBZ951` 里的 951 曾经让
    price=951，比所有真挂单都低，于是被当成最便宜的一条选中。
    """
    rows = market.scan(_load("list_unfiltered"))
    assert len(rows) == 6, [r["text"][:40] for r in rows]

    got = [(int(r["price"]), r["craft"], r["wear"]) for r in rows]
    assert got == UNFILTERED

    # 价格必须全部读出来：有一条 None 就意味着它永远进不了候选，
    # 而 pick() 只按价格排序 —— 读不出价的行会被当成「最便宜」以外的另一类错误。
    assert all(r["price"] is not None for r in rows)


def test_model_numbers_are_never_prices():
    """型号里的数字不能当价格。这是上面那条的单元级版本。"""
    for text in ("QBZ951 突击步枪王牌之剑",
                 "QBZ95-1 突击步枪-王牌之剑",
                 "M250 通用机枪 电玩高手S2",
                 "M7 战斗步枪 棱镜攻势S2",
                 "MP7 冲锋枪 电玩高手S2",
                 "KC17 突击步枪 造物纪元"):
        assert market.parse_row(text)["price"] is None, text


def test_price_still_read_when_model_number_present():
    """摘掉型号之后，真价格必须还能读出来。"""
    row = market.parse_row("极品丨成色 B QBZ951 突击步枪王牌之剑 017000")
    assert row["price"] == 17000
    assert row["craft"] == "极品"
    assert row["wear"] == "B"


def test_filtered_list_prices_and_wears():
    """筛选到 2,050 上限后的六件：全部优品，价格 2050~2500。

    这张图钉的是「筛选生效后列表确实变了」—— 与上一张的极品/9000~28888
    完全不同，如果筛选没生效而程序照样扫，会拿旧列表去挑。
    """
    rows = market.scan(_load("list_filtered"))
    assert len(rows) == 6, [r["text"][:40] for r in rows]
    assert [(int(r["price"]), r["craft"], r["wear"]) for r in rows] == FILTERED


def test_pick_chooses_the_cheapest_within_budget():
    """上限 10000 时，9000 那件（极品 C）最便宜，必须选它。

    修复前 `QBZ951` 的 951 会被当成价格，于是选中一条读不出真价的行 ——
    点进去买的是别人挂的任意一件。
    """
    rows = market.scan(_load("list_unfiltered"))
    task = {"name": "QBZ95-1 突击步枪-王牌之剑", "keywords": ["王牌之剑"],
            "max_price": 10000, "sort": "price"}
    row, why, diag = market.pick(rows, task)
    assert row is not None, why
    assert row["price"] == 9000
    assert row["wear"] == "C"


def test_pick_refuses_when_everything_is_over_budget():
    """上限 8000：这批最便宜 9000，一件都不能选。"""
    rows = market.scan(_load("list_unfiltered"))
    task = {"name": "QBZ95-1 突击步枪-王牌之剑", "keywords": ["王牌之剑"],
            "max_price": 8000}
    row, why, _ = market.pick(rows, task)
    assert row is None


def test_pick_prefers_the_requested_grade():
    """要 S 成色：只有 16000 那件，价格上限放开就得选它。"""
    rows = market.scan(_load("list_unfiltered"))
    task = {"name": "QBZ95-1 突击步枪-王牌之剑", "keywords": ["王牌之剑"],
            "wear": "S", "sort": "price"}
    row, why, diag = market.pick(rows, task)
    assert row is not None, why
    assert row["wear"] == "S"
    assert row["price"] == 16000


def test_click_target_lands_inside_its_card():
    """点击坐标必须落在卡片内 —— engine 用 x0+40 和 cy 定位。"""
    rows = market.scan(_load("list_unfiltered"))
    for r in rows:
        cx = r["x0"] + 40
        assert r["x0"] <= cx <= r["x1"]
        assert r["y0"] <= r["cy"] <= r["y1"]


# ---------------------------------------------------------------- 1920 在售页
#
# 2026-11-02 补：上面那两张 fixture 是 1380×630 的裁切图，而真机是 1920×1080。
# 在真实分辨率上，卡片定位曾经是错的（行距算成 197，实际 206），症状是
# 「900,000」串进别人家的行、两张卡粘成一条、价格读成 250。

SHOT_1920 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "_shots", "drv_onsale.png")
LIST_1920 = (60, 210, 1340, 730)      # engine.LIST_N 的像素值


def _onsale_1920():
    img = imread(SHOT_1920)
    if img is None:
        pytest.skip("缺截图 drv_onsale.png")
    x, y, w, h = LIST_1920
    return img[y:y + h, x:x + w]


def test_1920_onsale_reads_all_six_cards():
    """1920 真机截图：6 张卡、6 个价格全对、成色对。

    真值（肉眼看原图）：38,888 / 42,888 / 13,000 / 25,000 / 20,000 / 14,000，
    最后一张是成色 B，其余 C。
    """
    from core import market
    rows = market.scan(_onsale_1920())
    assert len(rows) == 6, f"应扫到 6 张卡，实际 {len(rows)}"
    assert [int(r["price"]) for r in rows] == [38888, 42888, 13000, 25000, 20000, 14000]
    assert [r["wear"] for r in rows] == ["C", "C", "C", "C", "C", "B"]
    assert all(r["craft"] == "极品" for r in rows)
    assert all("MK4冲锋枪-疾光魅影" in r["text"] for r in rows)


def test_1920_card_rects_are_evenly_spaced():
    """卡片矩形的行距要均匀 —— 这是「不串行」的根。

    早期行距按横线间距的中位数算，被一条断线带偏成 197（实际 206），
    三行下来累计偏 27px，每块都压到相邻卡片上。
    """
    from core import cards
    rects = cards.locate_cards(_onsale_1920())
    assert len(rects) == 6
    rows = sorted({(r[1], r[3]) for r in rects})
    heights = [b - a for a, b in rows]
    assert len(set(heights)) == 1, f"行高不一致：{heights}"
    assert all(200 <= x <= 215 for x in heights), f"行高不对：{heights}"


def test_1920_onsale_survives_scrolling_one_row():
    """列表往上滚过一行之后，剩下的四张仍要读对。

    滚动是这一页的常态：滚完之后第一条竖线经常检测不到，早期会退化成
    「1 列」或「3 列」，价格还会被右边界切掉最后一位（13,000 → 13,00）。
    """
    from core import market
    frame = _onsale_1920()[206:]          # 正好滚过一行
    rows = market.scan(frame)
    assert len(rows) == 4, f"滚动后应剩 4 张，实际 {len(rows)}"
    assert [int(r["price"]) for r in rows] == [13000, 25000, 20000, 14000]
