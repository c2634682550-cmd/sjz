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
    def click(self, x, y, verify=True, expect_foreground=None, **kw):
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


def test_wheel_legacy_is_refused_without_borrow(monkeypatch):
    """legacy 通道不再提供滚轮：不再借系统光标、不再发 mouse_event。

    旧实现会自动退回 user32 的 mouse_event —— 那会把真实滚轮事件发给
    用户当前光标下的窗口。现在只有在显式 input_legacy_borrow=true 时
    才允许 legacy，而滚轮本身只走驱动。
    """
    import core.input as inp
    fake = _FakeUser32()
    monkeypatch.setattr(inp, "_user32", fake)

    with pytest.raises(inp.InputChannelError):
        Keyboard(1234, mode="legacy").wheel(600, 400, -1)
    assert fake.calls == [], "拒绝之后不许往 user32 上发任何东西"


@pytest.mark.skipif(not X.driver_ready()[0], reason="Interception 驱动未安装")
def test_wheel_default_goes_through_driver(monkeypatch):
    """默认模式（interception）：滚轮走驱动 send，不发 user32 事件。"""
    import core.input as inp
    fake = _FakeUser32()
    monkeypatch.setattr(inp, "_user32", fake)

    sent = []

    class _Spy:
        def mouse_device(self):
            return {"device": 11}

        def send_mouse(self, state, **kw):
            sent.append((state, kw))

    monkeypatch.setattr(X, "shared", lambda: _Spy())

    Keyboard(1234, mode="interception").wheel(600, 400, -1)

    states = [s for s, _kw in sent]
    assert X.MOUSE_WHEEL in states, "默认模式滚轮必须走驱动"
    wheels = [kw for s, kw in sent if s == X.MOUSE_WHEEL]
    assert wheels[0]["rolling"] == -120, "负数 = 向下一格（Windows 惯例）"
    assert wheels[0]["device"] == 11
    kinds = [c[0] for c in fake.calls]
    assert "mouse_event" not in kinds, "驱动滚轮不该再发 user32 事件"
    assert "SetCursorPos" not in kinds, "回原位也走驱动事件，不再借 user32 挪光标"


def test_wheel_moves_cursor_first(monkeypatch):
    """先把光标（用驱动绝对坐标事件）挪到目标位置，再滚。

    驱动 send 用 spy 替掉，避免真往系统注入滚轮事件。
    """
    import core.input as inp
    fake = _FakeUser32()
    monkeypatch.setattr(inp, "_user32", fake)

    sent = []

    class _Spy:
        def mouse_device(self):
            return {"device": 11}

        def send_mouse(self, state, **kw):
            sent.append((state, kw))

    monkeypatch.setattr(X, "shared", lambda: _Spy())

    Keyboard(1234).wheel(613, 380, 1)

    assert sent, "滚轮没走驱动"
    state, kw = sent[0]
    assert state == 0, "第一条必须是移动事件"
    assert kw["flags"] & X.MOUSE_MOVE_ABSOLUTE, "移动要用绝对坐标，游戏按坐标路由滚动"
    assert (kw["x"], kw["y"]) != (0, 0), "滚轮前没有把位置挪过去"
    assert fake.calls == [], "移动/滚动/回原位全程走驱动，不碰 user32"


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

