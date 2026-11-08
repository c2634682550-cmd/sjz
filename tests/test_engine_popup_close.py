"""关「三角币不足」弹窗：取消点不动必须 ESC 兜底，并回读确认消失。

反证（这条是本轮事故的现场，2026-11-08）：
    取消按钮点击落在 (800,675)，但弹窗没消失；代码「发一次取消就继续」，
    于是后面 4 条挂单全部点在弹窗上，页型全部读成 unknown —— 整批作废。
    把 ESC 兜底拿掉，用例 1 就会退化成 False（关不掉），上层只能停手；
    没有这道兜底，就是上面那 4 条 unknown。
"""
import os
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod


class FakeMouse:
    def __init__(self, cancel_works=False):
        self.cancel_works = cancel_works
        self.clicks = []

    def click(self, x, y, **kw):
        self.clicks.append((x, y))


class FakeKeyboard:
    taps = []
    on_tap = None

    def __init__(self, *a, **k):
        pass

    def tap(self, key):
        FakeKeyboard.taps.append(key)
        if FakeKeyboard.on_tap:
            FakeKeyboard.on_tap(key)


class FakeScreen:
    def __init__(self, state):
        self.state = state          # {"open": True}

    def grab(self, box=None):
        return np.zeros((100, 100, 3), np.uint8)


class Stub:
    """只带 `_close_insufficient_popup` 依赖的最小 self。"""

    _target_hwnd = None
    _input_mode = "interception"
    _click = engine_mod.Engine._click
    _popup_present = engine_mod.Engine._popup_present
    _tap_esc = engine_mod.Engine._tap_esc
    _close_insufficient_popup = engine_mod.Engine._close_insufficient_popup

    def __init__(self):
        self._stop = threading.Event()
        self.said = []

    def _say(self, *a, **k):
        self.said.append(" ".join(str(x) for x in a))


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    FakeKeyboard.taps = []
    FakeKeyboard.on_tap = None
    monkeypatch.setattr(engine_mod, "Keyboard", FakeKeyboard)


def _wire(monkeypatch, screen, mouse, cancel_works=False):
    def on_tap(_key):
        if screen.state["open"]:
            screen.state["open"] = False

    def _click_hook(x, y):
        mouse.clicks.append((x, y))
        if cancel_works:
            screen.state["open"] = False

    monkeypatch.setattr(engine_mod.popup, "scan", lambda img: {
        "found": screen.state["open"], "kind": "insufficient" if screen.state["open"] else None,
        "buttons": ({"cancel": {"cx": 800, "cy": 675}} if screen.state["open"] else None),
        "text": ""})
    monkeypatch.setattr(engine_mod.popup, "cancel_point",
                        lambda res: (800, 675) if res.get("found") else None)
    FakeKeyboard.on_tap = on_tap
    obj = Stub()
    obj._click = lambda m, x, y, hold=None: _click_hook(x, y)
    return obj


def test_cancel_noop_falls_back_to_esc(monkeypatch):
    screen = FakeScreen({"open": True})
    mouse = FakeMouse(cancel_works=False)
    stub = _wire(monkeypatch, screen, mouse, cancel_works=False)
    assert stub._close_insufficient_popup(screen, mouse) is True
    assert screen.state["open"] is False
    assert FakeKeyboard.taps == ["ESC"]      # 取消点不动 → ESC 兜底一次
    assert mouse.clicks == [(800, 675)]      # 仍然先礼后兵，点到「取消」而不是「充值」


def test_cancel_works_no_esc(monkeypatch):
    screen = FakeScreen({"open": True})
    mouse = FakeMouse(cancel_works=True)
    stub = _wire(monkeypatch, screen, mouse, cancel_works=True)
    assert stub._close_insufficient_popup(screen, mouse) is True
    assert FakeKeyboard.taps == []


def test_no_popup_returns_true_without_click(monkeypatch):
    screen = FakeScreen({"open": False})
    mouse = FakeMouse()
    stub = _wire(monkeypatch, screen, mouse)
    assert stub._close_insufficient_popup(screen, mouse) is True
    assert mouse.clicks == [] and FakeKeyboard.taps == []


def test_unclosable_returns_false_so_caller_stops(monkeypatch):
    """取消点不动 + ESC 也无效 → 必须 False（上层停手），绝不当作已关闭。"""
    screen = FakeScreen({"open": True})
    mouse = FakeMouse()
    stub = _wire(monkeypatch, screen, mouse)
    FakeKeyboard.on_tap = lambda _k: None      # ESC 也关不掉
    assert stub._close_insufficient_popup(screen, mouse) is False
    assert screen.state["open"] is True


def test_esc_send_failure_returns_false(monkeypatch):
    screen = FakeScreen({"open": True})
    mouse = FakeMouse()
    stub = _wire(monkeypatch, screen, mouse)

    def boom(_self):
        raise RuntimeError("no keyboard")
    monkeypatch.setattr(engine_mod, "Keyboard", boom)
    assert stub._close_insufficient_popup(screen, mouse) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
