"""Interception 绑定层：ABI 与加载逻辑。

钉的都是换内核注入时真会踩的坑：
- stroke 大小错一个字节，驱动 send 直接拒（返回 0），表现是
  「程序说点了、游戏没反应」—— 和当年 SendInput ABI 那个坑一模一样。
- 设备编号映射错（键盘 1-10、鼠标 11-20），点击会发给键盘设备，
  驱动不报错但事件落空。
"""
import ctypes
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import interception as X  # noqa: E402


def test_mouse_stroke_size_matches_c_abi():
    """sizeof(InterceptionMouseStroke) = 20：u16×2 + i16 + i32×2 + u32，按 4 对齐。

    官方头文件 InterceptionStroke 就按这个大小开缓冲。写错（比如当成
    16）send 一样返回 1 但驱动读的内存越界/错位 —— 比返回 0 更隐蔽。
    """
    assert ctypes.sizeof(X.MouseStroke) == 20


def test_key_stroke_layout():
    """KeyStroke 8 字节，code/state 在前 4 字节，information 在 4-8。"""
    assert ctypes.sizeof(X.KeyStroke) == 8
    assert X.KeyStroke.code.offset == 0
    assert X.KeyStroke.state.offset == 2
    assert X.KeyStroke.information.offset == 4


def test_device_numbering_matches_header():
    """键盘 1..10、鼠标 11..20 —— INTERCEPTION_KEYBOARD/MOUSE 宏。

    弄反的表现：send 到键盘设备发鼠标 state，驱动收了但什么都不发生。
    """
    assert X.KEYBOARD(0) == 1
    assert X.KEYBOARD(9) == 10
    assert X.MOUSE(0) == 11
    assert X.MOUSE(9) == 20


def test_mouse_state_constants():
    """state 位值必须和 interception.h 一致（写错点的是别的键）。"""
    assert X.MOUSE_LEFT_DOWN == 0x001
    assert X.MOUSE_LEFT_UP == 0x002
    assert X.MOUSE_WHEEL == 0x400
    assert X.MOUSE_MOVE_ABSOLUTE == 0x001
    assert X.MOUSE_VIRTUAL_DESKTOP == 0x002


def test_key_state_constants():
    assert X.KEY_DOWN == 0x00
    assert X.KEY_UP == 0x01
    assert X.KEY_E0 == 0x02


def test_dll_candidate_paths_cover_frozen_and_source():
    """DLL 查找顺序：冻结环境(_MEIPASS) → core/assets → 下载目录兜底。"""
    paths = X._dll_candidates()
    assert paths, "至少要有一个候选路径"
    # 第一个候选和运行方式相关，但 core/assets 那个永远在列表里
    here = os.path.dirname(os.path.abspath(X.__file__))
    assert os.path.join(here, "assets", "interception.dll") in paths


def test_dll_present_in_repo_assets():
    """仓库里必须带 DLL —— 没它整套驱动注入静默降级成 legacy。"""
    here = os.path.dirname(os.path.abspath(X.__file__))
    assert os.path.isfile(os.path.join(here, "assets", "interception.dll"))


@pytest.mark.skipif(X._LIB.load() is None, reason="interception.dll 不可用")
def test_stroke_structs_sendable_size():
    """发给驱动前把 8 字节 KeyStroke 装进 20 字节缓冲的逻辑成立。"""
    ks = X.KeyStroke(code=0x3E, state=X.KEY_DOWN, information=0)
    buf = X.MouseStroke()
    ctypes.memmove(ctypes.byref(buf), ctypes.byref(ks), ctypes.sizeof(ks))
    # 前 4 字节（code+state）必须原样落在缓冲开头
    assert buf.state == 0x3E
    assert buf.flags == X.KEY_DOWN


@pytest.mark.skipif(not X.driver_ready()[0], reason="Interception 驱动未安装")
@pytest.mark.skipif(not os.environ.get("SJZ_LIVE"),
                    reason="实机冒烟：会动真鼠标，设 SJZ_LIVE=1 才跑")
def test_no_op_send_accepted():
    """相对移动 (0,0)：驱动不报错，且不会把光标甩走。

    这条为什么默认不跑：它断言的是**全局光标**的位置，而这是一台活机器 ——
    鼠标随时可能被人或别的程序碰，驱动走相对移动、指针加速也会让 0 位移
    抖几个像素。按「完全相等」写会随机红（今天连红两次，差 1px 和 3px），
    随机红的用例比没有用例更糟：真出问题时没人再看它。

    `send_mouse` 本身不返回驱动结果（None），所以「驱动接受了」只能靠
    「不抛异常 + 光标没飞走」间接观察。要跑就设 SJZ_LIVE=1。
    """
    drv = X.shared()
    before = _cursor()
    drv.send_mouse(0, x=0, y=0, flags=X.MOUSE_MOVE_RELATIVE)   # 不抛异常即可
    import time
    time.sleep(0.05)
    after = _cursor()
    dx, dy = abs(after[0] - before[0]), abs(after[1] - before[1])
    assert dx <= 20 and dy <= 20, f"no-op 移动把光标甩走了：{before} → {after}"


def _cursor():
    from ctypes import wintypes
    p = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)
