"""抓屏选屏：不能抓到「幽灵屏」。

幽灵屏 = 设备已经拔掉或改过分辨率，但系统还没从显示器列表里清掉的条目。
抓到它会拿到一张**不再刷新**的冻结画面 —— 每帧都一样。

真机上的后果（2026-10-23）：抓到 1345×960 的幽灵屏，截图和 9 小时前
md5 相同；引擎把画面里的「跳过」当成成交弹窗，凭空记了一笔「成交」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import screen as scr  # noqa: E402


class FakeMss:
    """假装是 mss，显示器列表里塞一块幽灵屏。"""

    def __init__(self, monitors):
        self.monitors = monitors

    def grab(self, rect):        # pragma: no cover - 选屏测试用不到
        raise AssertionError("不该真的抓屏")


def _monitors():
    return [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},   # 0 虚拟屏
        {"left": 0, "top": 0, "width": 1345, "height": 960},     # 1 幽灵屏
        {"left": 0, "top": 0, "width": 1920, "height": 1080},    # 2 真正的主屏
    ]


def test_primary_display_wins_over_a_ghost(monkeypatch):
    monkeypatch.setattr(scr.mss, "mss", lambda: FakeMss(_monitors()))
    s = scr.Screen(1)
    assert s.size == (1920, 1080), f"抓到了 {s.size} —— 那是幽灵屏"


def test_virtual_screen_is_still_selectable(monkeypatch):
    monkeypatch.setattr(scr.mss, "mss", lambda: FakeMss(_monitors()))
    s = scr.Screen(0)
    assert s.size == (1920, 1080)


def test_single_display_falls_back_cleanly(monkeypatch):
    only = [{"left": 0, "top": 0, "width": 1280, "height": 720}]
    monkeypatch.setattr(scr.mss, "mss", lambda: FakeMss(only))
    s = scr.Screen(1)
    assert s.size == (1280, 720)


def test_failed_construction_releases_mss(monkeypatch):
    """显示器号非法导致构造失败时，mss 已经占的 DC/Bitmap 必须关掉。

    每次失败漏 2 个 GDI 对象，用户反复点「定位」几小时就能把进程 GDI
    配额吃满（R02-L01）。
    """
    holder = {}

    class Closing(FakeMss):
        def __init__(self, monitors):
            super().__init__(monitors)
            self.closed = False

        def close(self):
            self.closed = True

    def make():
        holder["m"] = Closing(_monitors())
        return holder["m"]

    monkeypatch.setattr(scr.mss, "mss", make)
    with pytest.raises(ValueError):
        scr.Screen(99)
    assert holder["m"].closed, "构造失败后 mss 没关，GDI 对象泄漏"


# ---------------------------------------------------------------- 等画面换过来

def test_wait_frame_change_returns_as_soon_as_the_screen_changes():
    """抢回前台之后，画面一换就返回 —— 不要固定傻等。

    合成器贴新画面要一两帧，这期间抓到的是上一个窗口的内容（实测把游戏
    标题读成了窗口标题「on.exe」）。固定 sleep 0.6s 能绕过去，但每次都白等。
    """
    import numpy as np

    from core.screen import wait_frame_change

    before = np.zeros((20, 30, 3), np.uint8)
    after = np.full((20, 30, 3), 200, np.uint8)
    frames = iter([before.copy(), before.copy(), after])
    t = wait_frame_change(before, lambda: next(frames), timeout=2.0, interval=0.01)
    assert t is not None
    assert t < 1.0, f"等了 {t:.2f}s，说明没有及时返回"


def test_wait_frame_change_gives_up_when_nothing_changes():
    """画面一直没变就超时返回 None —— 由调用方决定要不要继续。"""
    import numpy as np

    from core.screen import wait_frame_change

    before = np.zeros((20, 30, 3), np.uint8)
    t = wait_frame_change(before, lambda: before.copy(), timeout=0.2, interval=0.01)
    assert t is None
