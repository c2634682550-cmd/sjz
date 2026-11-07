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

    2026-11-06 起主路径是进程内 RapidOCR（PP-OCRv6 服务退成价格二级复核、
    字模做三级兜底）。钉两件事：名字里要么写 RapidOCR、要么写 PP-OCR（并
    带上服务地址）；真不可用时必须**明说**不可用 —— 不许出现「名字看着
    正常、其实一直读空」。
    """
    from core import ocr
    name = ocr.backend_name()
    assert isinstance(name, str) and name
    if "不可用" in name:
        return          # 进程内引擎也起不来是合法状态，只要它老老实实报不可用
    assert "RapidOCR" in name or "PP-OCR" in name, name
    if "PP-OCR" in name:
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


# ---------------------------------------------------------------- 批量路径的决策来源

def _batch_scan(monkeypatch, got_items):
    """用假的批量认卡结果跑 _scan_cards：不碰真 OCR，只看字段来源。"""
    import numpy as np

    from core import cards as cardmod

    rects = [(10, 20, 310, 210), (320, 20, 620, 210)][:len(got_items)]
    monkeypatch.setattr(cardmod, "read_cards",
                        lambda img, recs, upscale=2: list(got_items))
    monkeypatch.setattr(cardmod, "read_bottoms",
                        lambda img, recs, upscale=3: [])
    return market._scan_cards(np.zeros((400, 700, 3), np.uint8), rects)


def test_batch_text_cannot_move_the_click(monkeypatch):
    """批量路径的 text_batch 被污染（商品名改一个字），点击决策必须不受影响。

    真实事故形状：批量 rec 与单卡 rec 有 15 处差一个 `|`。哪天差的不是
    分隔符而是名字里的一个字，拿整行 text 去匹配就会点错商品。所以名字、
    价格、成色只认结构化块，text_batch 只许出现在展示/日志里。
    """
    good = "QBZ95-1突击步枪-王牌之剑"
    poison = "QBZ95-1突击步枪-王牌之刃"
    polluted = f"{poison}\n极品|B 28,888"
    items = [{"name": good, "grade": "极品|B", "price": "28,888",
              "text_batch": polluted, "text_reliable": False} for _ in range(2)]

    rows = _batch_scan(monkeypatch, items)
    assert len(rows) == 2
    for row in rows:
        assert row["name"] == good
        assert row["name_source"] == "structured"
        # 匹配串必须逐字等于结构化名字：污染的那个字一次都不能出现
        assert market._match_text(row) == good
        assert poison not in market._match_text(row)
        # 展示串也由结构化字段拼成，污染的名字根本进不来
        assert poison not in row["text"]

    task = {"name": good, "max_price": 40000}
    ok, why = market.check(rows[0], task)
    assert ok, why

    row, why, diag = market.pick(rows, task)
    assert row is not None, why
    assert row["name"] == good
    assert (row["x0"], row["y0"]) == (10, 20)
    assert diag[0]["name_source"] == "structured"


def test_poisoned_text_cannot_steal_the_click(monkeypatch):
    """批量文本被换成**另一件商品**的名字：不能因此抢走本该点另一张卡的决策。

    这是「点错商品」的具体形状：左边那张卡的结构化名字是 A，批量文本却被
    污染成 B；右边那张才是 B。按结构化名字匹配，任务 B 只能选中右边；
    若拿整行 text 匹配，两张都会被判成 B，`pick` 会按屏幕顺序点中左边。
    """
    name_a = "QBZ95-1突击步枪-王牌之剑"
    name_b = "Vector冲锋枪-美杜莎"
    items = [
        {"name": name_a, "grade": "极品|B", "price": "28,888",
         "text_batch": f"{name_b}\n极品|B 28,888", "text_reliable": False},
        {"name": name_b, "grade": "极品|B", "price": "28,888",
         "text_batch": f"{name_b}\n极品|B 28,888", "text_reliable": False},
    ]
    rows = _batch_scan(monkeypatch, items)
    task = {"name": name_b, "max_price": 40000}

    assert market.check(rows[0], task)[0] is False
    assert market.check(rows[1], task)[0] is True

    row, why, diag = market.pick(rows, task)
    assert row is not None, why
    assert row["name"] == name_b
    assert (row["x0"], row["y0"]) == (320, 20)   # 右边的卡，不是被污染的左边


def test_scanner_batch_rows_use_structured_name(monkeypatch):
    """生产路径 Scanner().scan 也必须走结构化字段（批量 text_batch 只展示）。

    引擎列表模式走的是 Scanner，不是 `_scan_cards` —— 同一类风险有两条路，
    这条钉住第二条：行的名字只来自结构化 name，text_batch 里的污染不参与。
    """
    import numpy as np

    from core import cards as cardmod

    good = "QBZ95-1突击步枪-王牌之剑"
    poison = "Vector冲锋枪-美杜莎"
    rects = [(10, 20, 310, 210), (320, 20, 620, 210)]
    monkeypatch.setattr(cardmod, "locate_cards",
                        lambda img, gray=None, bottom_half=False: list(rects))
    monkeypatch.setattr(cardmod, "read_cards", lambda img, recs, upscale=2: [
        {"name": good, "grade": "极品|B", "price": "28,888",
         "text_batch": f"{poison}\n极品|B 28,888", "text_reliable": False}
        for _ in recs])
    monkeypatch.setattr(cardmod, "read_bottoms",
                        lambda img, recs, upscale=3: [])

    sc = market.Scanner()
    rows = sc.scan(np.zeros((400, 700, 3), np.uint8))
    assert len(rows) == 2
    for row in rows:
        assert row["name"] == good
        assert row["name_source"] == "structured"
        assert poison not in row["text"]
    assert market.check(rows[0], {"name": poison})[0] is False


def test_batch_result_without_text_never_keyerrors(monkeypatch):
    """全仓守护：批量结果**只有 text_batch、没有 text**，任何公开入口都不许 KeyError。

    第三轮把 `text` 改名成 `text_batch` 时漏掉了 `Scanner.scan` 的
    `got["text"]`，生产列表模式会直接崩。这条把「批量结果形状」钉死：
    字段再改名时，下面这些入口会立刻红。
    """
    import numpy as np

    from core import cards as cardmod

    good = "QBZ95-1突击步枪-王牌之剑"
    rects = [(10, 20, 310, 210), (320, 20, 620, 210)]
    batch = [{"name": good, "grade": "极品|B", "price": "28,888",
              "text_batch": f"{good}\n极品|B 28,888", "text_reliable": False}
             for _ in rects]
    for d in batch:
        assert "text" not in d

    monkeypatch.setattr(cardmod, "locate_cards",
                        lambda img, gray=None, bottom_half=False: list(rects))
    monkeypatch.setattr(cardmod, "read_cards",
                        lambda img, recs, upscale=2: list(batch))
    monkeypatch.setattr(cardmod, "read_bottoms",
                        lambda img, recs, upscale=3: [])

    frame = np.zeros((400, 700, 3), np.uint8)

    # 扫描入口：模块级批量认卡 + 生产 Scanner
    rows = market._scan_cards(frame, rects)
    assert [r["name"] for r in rows] == [good, good]
    rows2 = market.Scanner().scan(frame)
    assert [r["name"] for r in rows2] == [good, good]

    # 行级入口：拿一条没有 text 的行（只可能是批量结果形状）
    bare = dict(batch[0])
    bare.update(price=28888.0, x0=0, y0=0, x1=0, y1=0, cy=0)
    assert "text" not in bare
    assert market._match_text(bare) == good
    assert market.check(bare, {"name": good})[0] is True
    picked, why, diag = market.pick([bare], {"name": good})
    assert picked is not None, why
    assert diag[0]["text"] == ""


def test_text_only_row_is_flagged_and_never_bought(monkeypatch):
    """结构化名字块空了、只剩 text_batch：打降级标记，只展示不购买。

    降级标记字段是 `name_source`（值 text_batch / none），`check()` 直接
    拒绝、`pick()` 的台账里也带着它，出问题能查出来是哪一行降级了。
    """
    good = "QBZ95-1突击步枪-王牌之剑"
    items = [{"name": "", "grade": "极品|B", "price": "28,888",
              "text_batch": f"{good}\n极品|B 28,888", "text_reliable": False}]

    rows = _batch_scan(monkeypatch, items)
    assert rows[0]["name_source"] == "text_batch"
    assert market._match_text(rows[0]) == ""

    ok, why = market.check(rows[0], {"name": good})
    assert not ok
    assert "降级" in why and "name_source=text_batch" in why

    row, why, diag = market.pick(rows, {"name": good})
    assert row is None
    assert diag[0]["name_source"] == "text_batch"
    assert "降级" in diag[0]["why"]


# ============================================================ 整幅兜底的保守判据
# 回归来源：385 帧重测里，面板拼条价格拒识后走整幅兜底，`编号 000104429`
# 被「取最大 ≥100 的整数」当成价格，9,999,999 输出成 104,429（静默错价）。
# 抢购场景下这种错价会直接下成高价单，比拒识危险得多。

def test_whole_frame_never_reads_id_as_price():
    """编号行不得当价格：000104429 是货号。"""
    text = "煙灭の時幻羽\n编号 000104429\n9.999.999"
    row = market.parse_row(text, whole_frame=True)
    assert row["price"] is None
    assert row["price_note"] == market.PRICE_UNREAD
    # 纯编号行同样拒识（单卡行路径也不认）
    assert market.parse_row("编号 000104429", whole_frame=True)["price"] is None
    assert market.parse_row("编号 000104429")["price"] is None


def test_whole_frame_accepts_labeled_price():
    """带标签的行能确认价格语义，照常取值。"""
    text = "售价 9,999,999\n编号 000104429"
    assert market.parse_row(text, whole_frame=True)["price"] == 9999999


def test_whole_frame_only_accepts_ref_matching_number():
    """无标签时只认与面板拼条一致的数；无参照就拒识，不猜。"""
    assert market.parse_row("129.999", whole_frame=True,
                            ref_price="129,999")["price"] == 129999
    assert market.parse_row("129.999", whole_frame=True)["price"] is None
    assert market.parse_row("17,000\n104,429", whole_frame=True,
                            ref_price="17,000")["price"] == 17000


def test_whole_frame_rejects_overlong_and_zero_padded_tokens():
    """位数异常/多前导 0 的数字串按编号排除，不参与价格判定。"""
    assert market.parse_row("饰品 025000 金币 1200", whole_frame=True)["price"] is None
    assert market.parse_row("1044290000", whole_frame=True)["price"] is None
