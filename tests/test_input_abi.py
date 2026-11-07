"""SendInput 结构体必须符合 ABI —— 错一个字节就全部点击失效。

这里钉的是一个真实踩过的坑：`_MOUSEINPUT.dwExtraInfo` 原本写成
`ctypes.POINTER(ctypes.c_ulong)`，在 64 位下让 sizeof(INPUT) = 28 而不是 40，
`SendInput` 直接返回 0 并报 ERROR_INVALID_PARAMETER(87)。表现是
「程序说点了、光标也动了，游戏一点反应没有」—— 完全静默，极难排查。

所以这些测试检查的是**内存布局**，不是源码文本：改坏结构定义就会失败。
"""
import ctypes
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import input as I  # noqa: E402

IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8


def test_input_struct_size_matches_abi():
    """sizeof(INPUT) 必须是 40（64 位）/ 28（32 位）。

    这是 SendInput 唯一接受的尺寸。差一个字节，系统就认为参数非法，
    一个事件都不投递，而且**不报错给调用方**（除非去查 GetLastError）。
    """
    expected = 40 if IS_64 else 28
    assert ctypes.sizeof(I._INPUT) == expected, (
        f"sizeof(INPUT)={ctypes.sizeof(I._INPUT)}，应为 {expected}。"
        f"dwExtraInfo 必须是 ULONG_PTR（指针大小），不能是 POINTER(...)"
    )


def test_extra_info_is_pointer_sized():
    """dwExtraInfo 占一个指针大小，且位于结构末尾。"""
    ptr = ctypes.sizeof(ctypes.c_void_p)
    size = ctypes.sizeof(I._MOUSEINPUT) - I._MOUSEINPUT.dwExtraInfo.offset
    assert size == ptr, (
        f"dwExtraInfo 占 {size} 字节，应为 {ptr}"
    )
    assert I._MOUSEINPUT.dwExtraInfo.offset == 24, (
        f"dwExtraInfo 偏移 {I._MOUSEINPUT.dwExtraInfo.offset}，应为 24"
    )


def test_mouse_input_size():
    """sizeof(MOUSEINPUT) 在 64 位下是 32（含尾部对齐填充）。"""
    assert ctypes.sizeof(I._MOUSEINPUT) == (32 if IS_64 else 28)


def test_input_type_field_is_first():
    """type 必须在偏移 0 —— SendInput 靠它区分鼠标/键盘。"""
    assert I._INPUT.type.offset == 0


def test_input_union_starts_at_pointer_boundary():
    """union 必须从偏移 8 开始（type 是 DWORD，后面按指针对齐）。"""
    assert I._INPUT.u.offset == (8 if IS_64 else 4)


@pytest.mark.skipif(not IS_64, reason="只验证 64 位布局")
def test_no_pointer_wrapper_on_extra_info():
    """dwExtraInfo 不能是 POINTER 类型。

    用 POINTER(c_ulong) 时 ctypes 认为它是 8 字节，看起来没问题，
    但整个结构的总大小会算成 28 —— 因为它把 dwExtraInfo 当普通指针
    而不是 ULONG_PTR，尾部对齐填充丢失。这条用例直接检查类型。
    """
    t = I._MOUSEINPUT._fields_[[f[0] for f in I._MOUSEINPUT._fields_].index("dwExtraInfo")][1]
    assert t in (ctypes.c_ulonglong, ctypes.c_ulong), (
        f"dwExtraInfo 的类型是 {t}，应为 c_ulonglong/c_ulong"
    )


class _FakeScreen:
    bounds = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    index = 0
    monitors = []


class _FakeUser32:
    """只记 PostMessage 的 lParam，并按「窗口在 -8,-31」换算客户区坐标。"""

    WIN_ORIGIN = (-8, -31)

    def __init__(self):
        self.posts = []
        self.cursor = (0, 0)

    def GetSystemMetrics(self, idx):
        return 0

    def SetCursorPos(self, x, y):
        self.cursor = (x, y)
        return 1

    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = self.cursor
        return 1

    def ScreenToClient(self, hwnd, ref):
        ref._obj.x -= self.WIN_ORIGIN[0]
        ref._obj.y -= self.WIN_ORIGIN[1]
        return 1

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        self.posts.append((msg, lparam))
        return 1

    def GetForegroundWindow(self):
        return 4242


def test_click_posts_client_coordinates_not_screen_coordinates(monkeypatch):
    """PostMessage 的 lParam 必须是**客户区**坐标（legacy 兜底路径）。

    真机踩过：窗口在 (-8,-31)，直接把屏幕坐标塞进 lParam，每一下都整体偏
    (8,31) 像素 —— 大的按钮还能点到，交易行那个 280x60 的价格按钮就点到边上，
    表现是「消息发成功了，游戏没反应」。

    2026-10-30 起默认走 Interception 驱动注入；这条仍要过 —— 兜底路径
    （驱动不可用时自动退 legacy）继续承载同样语义。
    """
    fake = _FakeUser32()
    monkeypatch.setattr(I, "_user32", fake)
    # legacy 会借系统光标，2026-11-07 起默认禁用，这里显式打开闸门
    monkeypatch.setattr(I, "_config_value",
                        lambda key, default=None:
                        True if key == "input_legacy_borrow" else default)

    m = I.Mouse(_FakeScreen(), game_hwnd=4242, mode="legacy")
    m.click(1700, 885, verify=False)

    downs = [lp for msg, lp in fake.posts if msg == I._WM_LBUTTONDOWN]
    assert downs, "没有投递按下消息"
    lp = downs[0]
    cx, cy = lp & 0xFFFF, (lp >> 16) & 0xFFFF
    assert (cx, cy) == (1700 - (-8), 885 - (-31)), (
        f"lParam 是 ({cx},{cy})，应当是客户区坐标 (1708,916)；"
        f"发屏幕坐标会让每次点击偏 (8,31) 像素"
    )
