"""悬浮窗定位与展开：窗口必须始终落在显示器工作区内，且按缩放换算物理像素。

覆盖 bd22375 之后的两处回归：
  * 展开只改高度、左上角不动，窗口拖到靠近底边时下半截会探出屏幕；
  * 默认位置用逻辑像素 OVERLAY_W 算右上角，缩放 150% 时右缘探出屏幕。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop  # noqa: E402

WORK = (0, 0, 1920, 1040)           # 1080p，减去 40px 任务栏


class _FakeU32:
    def __init__(self):
        self.calls = []

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        self.calls.append({"x": x, "y": y, "w": w, "h": h, "flags": flags})
        return 1


class _FakeWin:
    def __init__(self):
        self.moves = []
        self.resizes = []

    def move(self, x, y):
        self.moves.append((x, y))

    def resize(self, w, h):
        self.resizes.append((w, h))


@pytest.fixture
def overlay(monkeypatch):
    """装一个假悬浮窗：窗口矩形、显示器工作区、user32 都可控。"""
    state = {"rect": {"x": 1600, "y": 16, "w": 304, "h": 137}, "work": WORK}
    win = _FakeWin()
    u32 = _FakeU32()
    monkeypatch.setattr(desktop, "_OVERLAY_HANDLE", {
        "win": win, "hwnd": 4242, "placed": False, "pos": None, "watch": None})
    monkeypatch.setattr(desktop, "_window_rect", lambda hwnd: dict(state["rect"]))
    monkeypatch.setattr(desktop, "_monitor_work_area", lambda hwnd: state["work"])
    monkeypatch.setattr(desktop, "_work_area", lambda: state["work"])
    monkeypatch.setattr(desktop, "_u32", lambda: u32)
    monkeypatch.setattr(desktop, "_load_overlay_pos", lambda: None)
    monkeypatch.setattr(desktop, "_log", lambda *a, **k: None)
    return state, win, u32


# ---------------------------------------------------------------- 纯函数
@pytest.mark.parametrize("x,y,w,h,expect", [
    (100, 100, 300, 200, (100, 100)),         # 本来就在里面：不动
    (1800, 100, 300, 200, (1620, 100)),       # 右缘探出：左移
    (100, 1000, 300, 260, (100, 780)),        # 底缘探出：上移
    (-50, -20, 300, 200, (0, 0)),             # 左上探出：贴边
    (0, 0, 3000, 2000, (0, 0)),               # 比工作区还大：左上贴住
])
def test_clamp_into_work_area(x, y, w, h, expect):
    assert desktop.clamp_into_work_area(x, y, w, h, WORK) == expect


def test_clamp_respects_work_area_origin():
    """副屏工作区原点不是 (0,0)：夹的是它自己的边界。"""
    work = (1920, 0, 1920, 1080)
    assert desktop.clamp_into_work_area(1900, 1000, 304, 260, work) == (1920, 820)


# ---------------------------------------------------------------- 展开
def test_expand_near_bottom_moves_up_and_keeps_x(overlay):
    state, win, u32 = overlay
    state["rect"] = {"x": 1500, "y": 960, "w": 304, "h": 137}
    assert desktop._resize_overlay(True) is True
    call = u32.calls[-1]
    assert call["x"] == 1500
    assert call["y"] + call["h"] <= WORK[1] + WORK[3]
    assert call["h"] == desktop.OVERLAY_H_EXP and call["w"] == desktop.OVERLAY_W


def test_expand_with_room_does_not_move(overlay):
    state, win, u32 = overlay
    assert desktop._resize_overlay(True) is True
    call = u32.calls[-1]
    assert (call["x"], call["y"]) == (1600, 16)


def test_expand_uses_scaled_pixels_at_150_percent(overlay):
    state, win, u32 = overlay
    state["rect"] = {"x": 1400, "y": 800, "w": 456, "h": 205}     # 304*1.5, 137*1.5
    assert desktop._resize_overlay(True) is True
    call = u32.calls[-1]
    assert (call["w"], call["h"]) == (456, 390)
    assert call["y"] + call["h"] <= WORK[1] + WORK[3]


def test_expand_never_activates_or_reorders(overlay):
    state, win, u32 = overlay
    assert desktop._resize_overlay(True) is True
    flags = u32.calls[-1]["flags"]
    assert flags & desktop._SWP_NOACTIVATE and flags & desktop._SWP_NOZORDER


# ---------------------------------------------------------------- 默认位置
def test_default_position_100_percent(overlay):
    state, win, u32 = overlay
    assert desktop._place_overlay(win) is True
    assert win.moves[-1] == (1920 - 304 - 16, 16)


def test_default_position_150_percent_stays_inside_screen(overlay):
    """缩放 150%：窗口物理宽 456，右缘必须仍在工作区内。"""
    state, win, u32 = overlay
    state["rect"] = {"x": 0, "y": 0, "w": 456, "h": 205}
    assert desktop._place_overlay(win) is True
    x, y = win.moves[-1]
    assert x + 456 <= WORK[2]
    assert x == 1920 - 456 - 16


def test_saved_position_outside_work_area_is_clamped(overlay, monkeypatch):
    state, win, u32 = overlay
    monkeypatch.setattr(desktop, "_load_overlay_pos", lambda: (1900, 1030))
    assert desktop._place_overlay(win) is True
    x, y = win.moves[-1]
    assert x + 304 <= WORK[2] and y + 137 <= WORK[1] + WORK[3]


# ---------------------------------------------------------------- 线程
def test_watch_thread_not_duplicated_on_recreate(monkeypatch):
    """重建悬浮窗时只留一条位置监视线程。"""
    import threading
    started = []

    class _T:
        def __init__(self, *a, **k):
            self._alive = False

        def start(self):
            self._alive = True
            started.append(self)

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(desktop, "_OVERLAY_HANDLE", {
        "win": None, "hwnd": None, "placed": False, "pos": None, "watch": None})
    monkeypatch.setattr(threading, "Thread", _T)
    results = [desktop._start_overlay_watch() for _ in range(3)]
    assert results == [True, False, False]
    assert len(started) == 1
