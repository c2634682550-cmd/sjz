"""光标锁检测与解除。

游戏独占全屏时会把光标钉在一个点上（ClipCursor）。这时 SendInput 和
SetCursorPos **全部静默失效** —— 函数照返回成功，GetCursorPos 照返回坐标
（但那是被锁住的位置），表现是「程序说点了，游戏没反应」。

实测（docs/auto-buy-design.md §4.8）：手动 ClipCursor(None) 之后
SetCursorPos 立刻恢复正常。

这些测试跑在真实桌面上，会短暂移动光标（几十毫秒内放回原位）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.input import Mouse  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_cursor():
    """测试结束后把光标放回原处。"""
    before = Mouse.cursor()
    yield
    try:
        import ctypes
        ctypes.windll.user32.SetCursorPos(*before)
    except Exception:
        pass


def test_release_cursor_lock_returns_a_pair():
    """返回值是 (是否成功, 之前有没有锁)，两个都要是 bool。

    调用方靠第二个值决定要不要在日志里提一句「清掉了锁」。
    """
    ok, had = Mouse.release_cursor_lock()
    assert isinstance(ok, bool)
    assert isinstance(had, bool)


def test_cursor_is_locked_returns_a_bool():
    assert isinstance(Mouse.cursor_is_locked(), bool)


def test_lock_detection_survives_an_already_unlocked_cursor(monkeypatch):
    """已经解锁时检测不能报「锁着」。

    误报的后果：引擎启动时无端给用户弹一条「点击可能无效」的错误。

    这里用可控的假光标而不是真实桌面：真机上有游戏在跑时，它会不断把
    ClipCursor 重新设上，用真实光标测这条会时好时坏。
    """
    import ctypes
    import ctypes.wintypes as wt

    state = {"x": 500, "y": 500}

    def fake_set(x, y):
        state["x"], state["y"] = x, y
        return True

    def fake_get(ref):
        ref._obj.x, ref._obj.y = state["x"], state["y"]
        return True

    monkeypatch.setattr(ctypes.windll.user32, "SetCursorPos", fake_set)
    monkeypatch.setattr(ctypes.windll.user32, "GetCursorPos", fake_get)

    assert Mouse.cursor_is_locked() is False, "光标能动就不该报「锁着」"


def test_lock_detection_actually_probes_by_moving():
    """检测必须靠「设一个位置、看它有没有动」，不能只看 GetClipCursor。

    实测游戏会把裁剪区设成**整个屏幕**，GetClipCursor 看起来完全正常，
    光标却纹丝不动。所以只看返回值的实现会漏判 —— 这条用例用猴子补丁
    模拟「GetClipCursor 正常但 SetCursorPos 无效」，要求检测仍然报 True。
    """
    import ctypes
    import ctypes.wintypes as wt

    real_set = ctypes.windll.user32.SetCursorPos
    real_get = ctypes.windll.user32.GetCursorPos

    frozen = wt.POINT()
    real_get(ctypes.byref(frozen))

    def fake_set(x, y):
        return True              # 假装成功，但不真的动

    def fake_get(ref):
        # 永远返回那个冻结位置 —— 模拟被锁住的光标
        ref._obj.x = frozen.x
        ref._obj.y = frozen.y
        return True

    ctypes.windll.user32.SetCursorPos = fake_set
    ctypes.windll.user32.GetCursorPos = fake_get
    try:
        assert Mouse.cursor_is_locked() is True, (
            "GetCursorPos 永远返回同一位置时，应当判为「锁着」"
        )
    finally:
        ctypes.windll.user32.SetCursorPos = real_set
        ctypes.windll.user32.GetCursorPos = real_get


def test_lock_detection_puts_the_cursor_back():
    """探测完必须把光标放回原位，不能把用户的鼠标留在别处。"""
    Mouse.release_cursor_lock()
    before = Mouse.cursor()
    Mouse.cursor_is_locked()
    after = Mouse.cursor()
    assert after == before, f"光标没归位：{before} -> {after}"
