"""假交易行 + RecordingDriver：整条「看 → 判 → 点」链路的无头验证。

不碰真屏幕、不注入输入：抓屏换成 FakeScreen，鼠标换成 MarketMouse（只记账）。
识别（market.scan）与决策（market.pick）用真实实现。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fake_market import (BUY_BTN, CONFIRM_BTN, LIST_ORIGIN, FakeMarket,  # noqa: E402
                         FakeScreen, MarketMouse)


def _center(box):
    x0, y0, x1, y1 = box
    return (x0 + x1) // 2, (y0 + y1) // 2


def _walk_to_confirm(market, mouse, card=0):
    x0, y0, x1, y1 = market.card_box(card)
    mouse.click(x0 + 40, (y0 + y1) // 2)
    mouse.click(*_center(BUY_BTN))


# ------------------------------------------------------------ 状态机（无需 OCR）

def test_full_purchase_flow_and_zero_injection():
    m = FakeMarket()
    mouse = MarketMouse(m)
    _walk_to_confirm(m, mouse, card=1)
    assert m.page == "confirm"
    mouse.click(*_center(CONFIRM_BTN))
    assert m.page == "success"
    assert [c.price for c in m.purchases] == [2050]
    assert m.balance == 100000 - 2050
    assert mouse.injections == 0            # RecordingDriver：一次真实注入都没有
    assert len(mouse.events) == 3 and all(e["in_client"] for e in mouse.events)


@pytest.mark.parametrize("outcome", ["sold_out", "insufficient"])
def test_failure_outcomes_buy_nothing(outcome):
    m = FakeMarket(outcome=outcome)
    mouse = MarketMouse(m)
    _walk_to_confirm(m, mouse)
    mouse.click(*_center(CONFIRM_BTN))
    assert m.page == outcome
    assert m.purchases == [] and m.balance == 100000


def test_click_outside_hotspots_does_nothing():
    m = FakeMarket()
    mouse = MarketMouse(m)
    mouse.click(5, 5)                                       # 列表页空白处
    assert m.page == "list" and m.clicks[-1]["hit"] is None
    _walk_to_confirm(m, mouse)
    mouse.click(10, 10)                                     # 确认框外
    assert m.page == "confirm" and m.purchases == []


def test_buy_button_off_by_a_few_pixels_misses():
    m = FakeMarket()
    mouse = MarketMouse(m)
    x0, y0, x1, y1 = m.card_box(0)
    mouse.click(x0 + 40, (y0 + y1) // 2)
    mouse.click(BUY_BTN[0] - 3, BUY_BTN[1] + 20)            # 差 3 像素，没点中
    assert m.page == "detail"


def test_fake_screen_frame_shape_and_crop():
    m = FakeMarket()
    s = FakeScreen(m)
    frame = s.grab()
    assert frame.shape == (1080, 1920, 3)
    assert s.grab((LIST_ORIGIN[0], LIST_ORIGIN[1], 100, 50)).shape == (50, 100, 3)


# ------------------------------------------------------------ 识别 + 决策（需要 OCR）

def _ocr_ready():
    try:
        from core import market
        m = FakeMarket()
        rows = market.scan(FakeScreen(m).grab((LIST_ORIGIN[0], LIST_ORIGIN[1], 1340, 520)))
        return bool(rows)
    except Exception:
        return False


needs_ocr = pytest.mark.skipif(not _ocr_ready(), reason="OCR 服务不可用或渲染帧读不出来")

TASK_OK = {"id": "ok", "name": "QBZ95-1突击步枪-王牌之剑", "keywords": ["王牌之剑"],
           "max_price": 3000}
TASK_TOO_CHEAP = {"id": "cheap", "name": "QBZ95-1突击步枪-王牌之剑", "keywords": ["王牌之剑"],
                  "max_price": 1000}


@needs_ocr
def test_scan_reads_prices_from_rendered_frame():
    from core import market
    m = FakeMarket()
    rows = market.scan(FakeScreen(m).grab((LIST_ORIGIN[0], LIST_ORIGIN[1], 1340, 520)))
    assert sorted(r["price"] for r in rows) == [2050, 2050, 2100, 2500]


@needs_ocr
def test_decide_then_click_buys_the_cheap_card():
    from core import market
    m = FakeMarket()
    screen, mouse = FakeScreen(m), MarketMouse(m)
    rows = market.scan(screen.grab((LIST_ORIGIN[0], LIST_ORIGIN[1], 1340, 520)))
    best, _why, _diag = market.pick(rows, TASK_OK)
    assert best is not None
    mouse.click(LIST_ORIGIN[0] + int(best["x0"]) + 40, LIST_ORIGIN[1] + int(best["cy"]))
    assert m.page == "detail" and m.cards[m.selected].price == 2050
    mouse.click(*_center(BUY_BTN))
    mouse.click(*_center(CONFIRM_BTN))
    assert m.page == "success" and m.purchases[0].price == 2050


@needs_ocr
def test_over_budget_task_never_clicks():
    from core import market
    m = FakeMarket()
    mouse = MarketMouse(m)
    rows = market.scan(FakeScreen(m).grab((LIST_ORIGIN[0], LIST_ORIGIN[1], 1340, 520)))
    best, _why, _diag = market.pick(rows, TASK_TOO_CHEAP)
    assert best is None
    assert mouse.events == [] and m.clicks == [] and m.purchases == []
