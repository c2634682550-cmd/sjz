"""滚轮和赛季下拉的三个真机 bug 的回归测试。

钉住的三个坑（都是 2026-10-23 真机踩的）：

  1. **滚轮必须用真实鼠标事件**（mouse_event），不能发 WM_MOUSEWHEEL
     消息 —— 游戏收消息式的滚轮直接无视：典藏外观的左侧列表滚 12 屏
     纹丝不动，每屏都是同样 6 件。
  2. **滚动的方向符号**：底层 `Keyboard.wheel` 用 Windows 惯例
     （正数向上），上层 `skin_scroll` 是「正数向下」—— 不翻符号就
     滚反方向，在列表顶部滚 12 屏全在原地。
  3. **赛季名按共有字符认**：OCR 会吞掉灰字小字的中间字（下拉里
     「全部赛季」读成「全季」、「气象感应」读成「气感应」、
     「命运王牌」读成「命运三牌」），要求全等就永远找不到「全部赛季」。
"""
import numpy as np
import pytest

from core import interception as X
from core.input import Keyboard
from core.nav import Navigator


class FakeScreen:
    def __init__(self):
        self._img = np.zeros((1080, 1920, 3), np.uint8)
        self.size = (1920, 1080)

    def grab(self, box=None):
        return self._img


class FakeMouse:
    def click(self, x, y, verify=True, expect_foreground=None):
        return True


# ------------------------------------------------------------ 1. 滚轮事件

class _FakeUser32:
    """记录 _user32 上被调用的方法。"""

    def __init__(self):
        self.calls = []

    def SetCursorPos(self, x, y):
        self.calls.append(("SetCursorPos", x, y))
        return True

    def mouse_event(self, flags, dx, dy, data, extra):
        self.calls.append(("mouse_event", flags, data))
        return None

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.calls.append(("PostMessageW", msg))
        return True


def test_wheel_uses_real_mouse_event(monkeypatch):
    """滚轮必须是真实事件 —— PostMessage 版的滚轮游戏不理。

    默认走 Interception 驱动注入（真事件）；这条钉的是 legacy 兜底
    路径：驱动不可用时自动退回来，仍必须 mouse_event 而不是消息。
    """
    import core.input as inp
    fake = _FakeUser32()
    monkeypatch.setattr(inp, "_user32", fake)

    Keyboard(1234, mode="legacy").wheel(600, 400, -1)

    kinds = [c[0] for c in fake.calls]
    assert "mouse_event" in kinds, "滚轮没走 mouse_event，游戏会无视"
    assert "PostMessageW" not in kinds, "滚轮还在发 WM_MOUSEWHEEL 消息"
    # 滚轮事件标志 MOUSEEVENTF_WHEEL = 0x0800
    ev = [c for c in fake.calls if c[0] == "mouse_event"]
    assert ev[0][1] == 0x0800
    # 负数 = 向下（Windows 惯例）
    assert ev[0][2] == -120


@pytest.mark.skipif(not X.driver_ready()[0], reason="Interception 驱动未安装")
def test_wheel_default_goes_through_driver(monkeypatch):
    """默认模式（interception）：滚轮走驱动 send，不发 user32 事件。"""
    import core.input as inp
    fake = _FakeUser32()
    monkeypatch.setattr(inp, "_user32", fake)

    sent = []
    class _Spy:
        def send_mouse(self, state, **kw):
            sent.append((state, kw))
    monkeypatch.setattr(X, "shared", lambda: _Spy())

    Keyboard(1234, mode="interception").wheel(600, 400, -1)

    assert sent, "默认模式滚轮必须走驱动"
    state, kw = sent[0]
    assert state == X.MOUSE_WHEEL
    assert kw["rolling"] == -120
    # 驱动滚轮也不该再调 mouse_event（真事件已由驱动注入）
    kinds = [c[0] for c in fake.calls]
    assert "mouse_event" not in kinds, "驱动滚轮不该再发 user32 事件"
    assert "SetCursorPos" in kinds, "光标还是要挪过去 —— 游戏按光标位置分流滚动"


def test_wheel_moves_cursor_first(monkeypatch):
    """先把真实光标挪到目标位置，游戏才知道该把这一滚交给谁。"""
    import core.input as inp
    fake = _FakeUser32()
    monkeypatch.setattr(inp, "_user32", fake)

    Keyboard(1234).wheel(613, 380, 1)

    assert fake.calls[0] == ("SetCursorPos", 613, 380), \
        "滚轮前没有挪光标，这一滚会落在别的地方"


# ------------------------------------------------------- 2. 滚动方向符号

class FakeKeyboard:
    def __init__(self):
        self.wheels = []

    def tap(self, key, **kw):
        return True

    def wheel(self, x, y, notches, **kw):
        self.wheels.append((x, y, notches))
        return True


def _nav():
    return Navigator(FakeScreen(), FakeMouse(), FakeKeyboard(),
                     snap=lambda: np.zeros((1080, 1920, 3), np.uint8),
                     log=lambda *a, **k: None)


def test_skin_scroll_down_is_negative_notch():
    """skin_scroll(1) = 向下 = 底层拿到负数（Windows 惯例正数向上）。"""
    nav = _nav()
    nav.skin_scroll(1)
    assert nav.keyboard.wheels, "skin_scroll 没发滚轮"
    for _x, _y, notches in nav.keyboard.wheels:
        assert notches < 0, f"向下滚却发了正数滚轮（{notches}），滚反方向了"


def test_skin_scroll_top_is_positive_notch():
    nav = _nav()
    nav.skin_scroll_top(max_notches=2)
    for _x, _y, notches in nav.keyboard.wheels:
        assert notches > 0, f"往上滚却发了负数滚轮（{notches}）"


# ------------------------------------------------------- 3. 赛季名识别

Y0, BY = 280, 334


def _chars(text, y):
    """造一组「单字」OCR 词（真机 OCR 对中文就是逐字返回的）。"""
    return [{"text": ch, "x": 600 + i * 20, "y": y, "w": 18, "h": 16}
            for i, ch in enumerate(text)]


def _spans(text, y=400):
    return Navigator._series_spans(_chars(text, y), Y0, BY)


@pytest.mark.parametrize("text,want", [
    ("全部赛季", "全部赛季"),
    ("全季", "全部赛季"),        # OCR 吞掉「部」
    ("气感应", "气象感应"),      # 吞掉「象」
    ("命运三牌", "命运王牌"),    # 「王」认成「三」
    ("棱镜攻势", "棱镜攻势"),
    ("群星", "群星"),
    ("难星", "群星"),            # 「群」认成「难」
    ("疾光赃影", "疾光魅影"),    # 「魅」认成「赃」
    ("棱镜攻势S2", "棱镜攻势S2"),
    ("电玩高手S2", "电玩高手S2"),
])
def test_series_spans_tolerates_ocr_noise(text, want):
    """OCR 认错字、漏字也要认得出赛季名。"""
    spans = _spans(text)
    assert want in spans, f"{text!r} 认成了 {list(spans)}"


def test_series_spans_distinguishes_s2_variants():
    """「棱镜攻势」和「棱镜攻势S2」是两项，不能混。"""
    assert "棱镜攻势" in _spans("棱镜攻势")
    assert "棱镜攻势S2" in _spans("棱镜攻势S2")
    assert "电玩高手" in _spans("电玩高手")
    assert "电玩高手S2" in _spans("电玩高手S2")


@pytest.mark.parametrize("text", ["已拥有", "未拥有", "传说品阶", "史诗品阶", "",
                                  "普通品阶", "确认", "筛选", "典藏"])
def test_series_spans_rejects_non_season_text(text):
    assert _spans(text) == {}

