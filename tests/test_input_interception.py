"""input_mode 切换与 legacy 兜底。

驱动注入换了点击通道后，input.py 的行为分叉点必须钉住：
- mode=interception 且驱动可用 → click 走驱动（光标跟着动，因为有
  绝对移动 stroke）
- 驱动不可用 → 自动退 legacy，不抛异常不让整场抢购停摆
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
from core import interception as X  # noqa: E402
from core import input as I  # noqa: E402
from core.input import Keyboard, Mouse  # noqa: E402
from core.screen import Screen  # noqa: E402


@pytest.fixture
def screen():
    s = Screen(1)
    yield s
    s.close()


def test_config_default_is_interception():
    cfg = cfgmod.Config()
    assert cfg["input_mode"] == "interception"


def test_mouse_mode_interception_when_driver_ready(screen):
    drv_ok = X.driver_ready()[0]
    m = Mouse(screen, mode="interception")
    assert m.mode == ("interception" if drv_ok else "legacy")


def test_mouse_mode_legacy_forces_legacy(screen):
    m = Mouse(screen, mode="legacy")
    assert m.mode == "legacy"
    assert m.driver_warning is None


def test_mouse_falls_back_when_dll_missing(screen, monkeypatch):
    """DLL 找不到时 create 失败 → mode 自动退 legacy，不抛异常。"""
    monkeypatch.setattr(X, "driver_ready", lambda: (False, "dll 没了"))
    m = Mouse(screen, mode="interception")
    assert m.mode == "legacy"
    assert m.driver_warning == "dll 没了"


@pytest.mark.skipif(not X.driver_ready()[0], reason="驱动未装")
def test_real_click_smoke(screen):
    """真点一下屏幕右下角空地：走完整 click() 路径，亚毫秒级。

    这是行为级冒烟，不只是单测 —— 它证明「绑定→send→驱动收」全链路通。
    """
    import time
    w, h = screen.size
    m = Mouse(screen, mode="interception")
    assert m.mode == "interception"
    t0 = time.perf_counter()
    m.click(w - 30, h - 30)     # expect_foreground=None，桌面点击
    dt = time.perf_counter() - t0
    assert dt < 0.05, f"驱动点击应亚毫秒级，实际 {dt*1000:.1f}ms"


@pytest.mark.skipif(not X.driver_ready()[0], reason="驱动未装")
def test_tap_smoke():
    """F12 无副作用：完整 tap() 路径（down → hold → up）不抛异常。"""
    k = Keyboard(None, mode="interception")
    k.tap("F12", hold=0.02)


def test_scan_table_has_f4_and_esc():
    """F4/ESC 是导航的命根子（开市场/复位），表里丢了导航全瘫。"""
    assert I.SCAN["F4"] == 0x3E
    assert I.SCAN["ESC"] == 0x01


def test_extended_keys_marked():
    """方向键是扩展键（E0），普通字母不是。漏标的表现：驱动注入后
    游戏把「上」认成小键盘 8。"""
    assert I._EXTENDED.get("UP") is True
    assert I._EXTENDED.get("A", False) is False
