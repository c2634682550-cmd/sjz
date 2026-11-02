"""引擎在购买后遇到「三角币不足」时的行为。

用假的 Screen/Mouse 跑真实的 `_guard_insufficient`，不点鼠标、不抓屏。
断言的是三件会花真钱或让人误判的事：

1. 认出来就必须停（换下一条商品也是余额不足，继续试只会拖到结束时间）
2. 停下之前必须点「取消」（弹窗是模态的，不关掉后面点击会落在弹窗上）
3. 点了「取消」就绝不能碰「充值」
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod
from core import popup
from core.vision import imread

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "market")
CROP = (640, 330)


class FakeScreen:
    """只回放固定的一帧。"""

    def __init__(self, frame):
        self._frame = frame
        self.grabs = 0

    def grab(self, box=None):
        self.grabs += 1
        return self._frame

    def size(self):
        return 1920, 1080


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None):
        self.clicks.append((x, y))


def _load(name):
    img = imread(os.path.join(FIXTURES, name))
    assert img is not None, f"{name} 读不出来"
    return img


@pytest.fixture
def eng():
    e = engine_mod.Engine()
    e._dry_run = False
    e._log = []
    e._say = lambda msg, level="info": e._log.append((level, msg))
    return e


def test_insufficient_popup_stops_the_run(eng):
    screen = FakeScreen(_load("insufficient.png"))
    mouse = FakeMouse()
    with pytest.raises(engine_mod.Aborted) as exc:
        eng._guard_insufficient(screen, mouse)
    assert "余额不足" in str(exc.value)


def test_insufficient_popup_clicks_cancel_and_never_top_up(eng):
    screen = FakeScreen(_load("insufficient.png"))
    mouse = FakeMouse()
    with pytest.raises(engine_mod.Aborted):
        eng._guard_insufficient(screen, mouse)

    assert len(mouse.clicks) == 1, f"点了 {len(mouse.clicks)} 次，只该点一次「取消」"
    x, y = mouse.clicks[0]
    full_x, full_y = x + CROP[0], y + CROP[1]
    # 录屏实测：取消 x 782-819 / y 665-683，充值 x 1100-1137
    assert 782 <= full_x <= 819, f"点到了 x={full_x}，不是「取消」"
    assert 665 <= full_y <= 683, f"点到了 y={full_y}，不是「取消」"
    assert full_x < 900, f"点到了 x={full_x}，已越过「取消」进入「充值」区域"


def test_popup_without_located_cancel_does_not_guess_a_position(eng):
    """认出弹窗但没定位到「取消」时：一次都不能点。

    这是最危险的路径 —— 如果代码退回「弹窗中心」之类的默认坐标，
    那个点很可能落在两个按钮之间或直接落在「充值」上。
    """
    frame = _load("insufficient.png")

    real_scan = popup.scan
    popup.scan = lambda img: {
        "found": True, "kind": "insufficient", "shortfall": None,
        "recommend": 328, "buttons": None, "text": "三角币不足",
    }
    try:
        mouse = FakeMouse()
        with pytest.raises(engine_mod.Aborted):
            eng._guard_insufficient(FakeScreen(frame), mouse)
        assert mouse.clicks == [], f"没定位到取消却点了 {mouse.clicks}"
    finally:
        popup.scan = real_scan


def test_normal_list_page_lets_the_run_continue(eng):
    """列表页（没有弹窗）不能触发中止，否则每次购买后都会被误判成余额不足。"""
    screen = FakeScreen(_load("list_unfiltered.png"))
    mouse = FakeMouse()
    eng._guard_insufficient(screen, mouse)   # 不该抛
    assert mouse.clicks == []


def test_dry_run_reports_but_does_not_click(eng):
    eng._dry_run = True
    screen = FakeScreen(_load("insufficient.png"))
    mouse = FakeMouse()
    eng._guard_insufficient(screen, mouse)   # 演练不抛，正常收尾
    assert mouse.clicks == []
    assert any("余额不足" in m for _, m in eng._log)


# ---------------- 点购买那一轮就得认出来 ----------------

class RectScreen(FakeScreen):
    """带 norm_to_px 的假屏幕 —— `_insufficient_showing` 要裁一块再 OCR。"""

    def norm_to_px(self, box):
        return (0, 0, 1920, 1080)


class FakeCfg(dict):
    def __init__(self):
        super().__init__({
            "threshold": 0.8, "failsafe": False, "click_hz": 50,
            "fire_timeout_s": 1.0, "confirm_clicks": 1,
            "confirm_interval_ms": 0,
            "regions": {"confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.1}},
        })

    def region(self, name):
        return self["regions"].get(name)


def test_insufficient_hint_sees_the_dialog(eng):
    assert eng._insufficient_showing(RectScreen(_load("insufficient.png"))) is True


def test_insufficient_hint_ignores_a_normal_page(eng):
    assert eng._insufficient_showing(RectScreen(_load("list_unfiltered.png"))) is False


def test_fire_stops_on_the_first_click_when_money_is_short(eng, monkeypatch):
    """钱不够时点购买不会弹确认框，而是直接弹余额不足。

    真机实测：点满 fire_timeout 之前弹窗就盖在上面了，后面每一下点击落到
    哪都不受控（弹窗右边就是「充值」）。所以第一次点完就要查。
    """
    monkeypatch.setattr(eng, "_insufficient_showing", lambda scr: True)
    guard_calls = []

    def guard(screen, mouse, tries=3, gap=0.35):
        guard_calls.append(1)
        raise engine_mod.Aborted("余额不足，已取消")

    monkeypatch.setattr(eng, "_guard_insufficient", guard)
    monkeypatch.setattr(engine_mod, "match", lambda a, b: (0.0, None))
    clicks = []
    monkeypatch.setattr(eng, "_click", lambda m, x, y: clicks.append((x, y)))

    with pytest.raises(engine_mod.Aborted):
        eng._fire_and_confirm(FakeCfg(), RectScreen(_load("insufficient.png")),
                              FakeMouse(), _load("insufficient.png"),
                              (100, 200), 0.0)
    assert guard_calls == [1]
    assert len(clicks) == 1, f"点了 {len(clicks)} 次购买，认出余额不足前只该点一次"


def test_fire_keeps_clicking_when_money_is_fine(eng, monkeypatch):
    """钱够的时候这道检查不能挡路：不弹窗、不中止，正常走完。"""
    monkeypatch.setattr(eng, "_insufficient_showing", lambda scr: False)
    # match 永远返回「弹窗在」：确认阶段会一直点到兜底时长
    monkeypatch.setattr(engine_mod, "match", lambda a, b: (0.99, None))
    # 成交校验和收奖励弹窗都替掉 —— 这条测的是余额检查，不是那两件事
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    monkeypatch.setattr(eng, "_close_reward", lambda *a, **k: None)
    clicks = []
    monkeypatch.setattr(eng, "_click", lambda m, x, y: clicks.append((x, y)))

    assert eng._fire_and_confirm(FakeCfg(), RectScreen(_load("list_unfiltered.png")),
                                 FakeMouse(), _load("insufficient.png"),
                                 (100, 200), 0.0) is True

    # 只点了购买和确认，没有点「取消」（取消只在认出余额不足时才点）
    assert (100, 200) in clicks, "没点购买按钮"
    confirm = [c for c in clicks if c != (100, 200)]
    assert confirm, "没点确认"
    assert len(set(confirm)) == 1, f"确认应该点在同一处：{set(confirm)}"
    # 确认阶段是闭环（弹窗不消失就一直点），所以断言「有界」而不是精确值：
    # 精确值由 confirm_timeout_s / confirm_interval_ms 决定，属于配置。
    assert len(confirm) <= 120, f"确认点了 {len(confirm)} 下，兜底时长没生效"

