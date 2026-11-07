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
from core import nocursor as N  # noqa: E402
from core.input import Keyboard, Mouse  # noqa: E402
from core.screen import Screen  # noqa: E402


def _allow_legacy(monkeypatch):
    """显式打开 input_legacy_borrow（legacy 默认禁用，会借系统光标）。"""
    monkeypatch.setattr(
        I, "_config_value",
        lambda key, default=None: True if key == "input_legacy_borrow" else default)


@pytest.fixture
def screen():
    s = Screen(1)
    yield s
    s.close()


def test_config_default_is_interception():
    cfg = cfgmod.Config()
    assert cfg["input_mode"] == "interception"


def test_mouse_mode_interception_when_driver_ready(screen, monkeypatch):
    # 不去碰真实驱动（另一条分支正在装/卸驱动，driver_ready 会等服服务）
    monkeypatch.setattr(X, "driver_ready", lambda: (True, ""))
    m = Mouse(screen, mode="interception")
    assert m.mode == "interception"

    # 驱动不可用：2026-11-07 起默认 input_fallback=fail —— 直接失败，
    # 不再悄悄降级到会借系统光标的 legacy。配置来源钉在代码默认值上，
    # 免得读到磁盘上别的进程正在改的那一份。
    monkeypatch.setattr(I, "_config_value",
                        lambda key, default=None: default)
    monkeypatch.setattr(X, "driver_ready", lambda: (False, "没装"))
    monkeypatch.setattr(X, "driver_status", lambda **_kw: {
        "ok": False, "detail": "没装", "reason_code": "DRIVER_UNAVAILABLE",
    })
    with pytest.raises(I.InputChannelError) as ei:
        Mouse(screen, mode="interception")
    assert ei.value.reason_code == N.R_DRIVER_UNAVAILABLE
    assert ei.value.channel == "interception"


def test_mouse_mode_legacy_forces_legacy(screen, monkeypatch):
    _allow_legacy(monkeypatch)          # legacy 需要显式打开借光标闸
    m = Mouse(screen, mode="legacy")
    assert m.mode == "legacy"
    assert m.driver_warning is None


def test_mouse_falls_back_when_dll_missing(screen, monkeypatch):
    """显式配 input_fallback=legacy + input_legacy_borrow=1 时才降级。

    默认策略是 fail：DLL 找不到时直接抛 InputChannelError（见上一条用例）。
    """
    # driver_ready() 决定「能不能用」，driver_status() 只负责细分原因码
    monkeypatch.setattr(X, "driver_ready", lambda: (False, "dll 没了"))
    monkeypatch.setattr(X, "driver_status", lambda **_kw: {
        "ok": False, "detail": "dll 没了", "reason_code": "DRIVER_UNAVAILABLE",
    })
    _allow_legacy(monkeypatch)
    m = Mouse(screen, mode="interception", fallback="legacy")
    assert m.mode == "legacy"
    assert m.driver_warning == "dll 没了"


# 导入期绝不探测真实驱动（driver_ready 可能在等驱动服务，导致整份测试卡住）
_LIVE = bool(os.environ.get("SJZ_LIVE"))


def test_filter_state_never_reads_in_process_by_default(monkeypatch):
    """默认只记期望值，不在本进程调 get_filter。

    回归点：`interception_get_filter` 会让解释器 finalize 在 native 层
    无界等待（实测 atexit→退出 57s，且 taskkill /F 收不掉）。每个调
    /api/preflight 的进程都读一遍，就等于每个进程都可能变成僵尸。
    """
    called = []

    def _must_not_be_called(*a, **k):
        called.append((a, k))
        return {"ok": True, "values": {d: 0 for d in X.FILTER_SLOTS}, "ms": 1.0}

    monkeypatch.delenv(X.FILTER_READBACK_ENV, raising=False)
    monkeypatch.setattr(X, "filter_audit_isolated", _must_not_be_called)
    st = X._filter_state()
    assert st == {"expected": "all-zero", "verified": False, "values": None}
    assert called == [], "默认路径不许回读 filter"


def test_filter_state_readback_when_explicitly_enabled(monkeypatch):
    """显式打开（参数或环境变量）时，回读走一次性子进程并如实标注。"""
    values = {d: 0 for d in X.FILTER_SLOTS}
    monkeypatch.setattr(X, "filter_audit_isolated",
                        lambda *a, **k: {"ok": True, "values": values, "ms": 12.5})
    st = X._filter_state(verify=True)
    assert st["verified"] is True and st["all_zero"] is True
    assert st["values"] == values and st["expected"] == "all-zero"

    monkeypatch.setenv(X.FILTER_READBACK_ENV, "1")
    st = X._filter_state()
    assert st["verified"] is True, "环境变量 SJZ_FILTER_READBACK=1 也要生效"

    monkeypatch.setattr(X, "filter_audit_isolated",
                        lambda *a, **k: {"ok": False, "values": None,
                                         "error": "超时", "ms": 5000.0})
    st = X._filter_state(verify=True)
    assert st["verified"] is False and st["all_zero"] is False
    assert st["error"] == "超时"


def test_driver_status_filters_shape_is_stable(monkeypatch):
    """`filters` 字段名与「期望全 0」的语义不变，界面不会少一栏。"""
    monkeypatch.delenv(X.FILTER_READBACK_ENV, raising=False)
    status = X.driver_status(probe=False)
    assert isinstance(status["filters"], dict)
    assert status["filters"]["expected"] == "all-zero"
    assert status["filters"]["verified"] is False
    for key in ("ok", "reason_code", "detail", "identity", "filters"):
        assert key in status



@pytest.mark.skipif(not _LIVE, reason="真机冒烟：需 SJZ_LIVE=1 且驱动已装")
def test_real_click_smoke(screen):
    """真点一下屏幕右下角空地：走完整 click() 路径，亚毫秒级。

    这是行为级冒烟，不只是单测 —— 它证明「绑定→send→驱动收」全链路通。
    **会移动真实光标**，所以默认跳过，只在显式 SJZ_LIVE=1 时跑。
    """
    import time
    w, h = screen.size
    m = Mouse(screen, mode="interception")
    assert m.mode == "interception"
    t0 = time.perf_counter()
    m.click(w - 30, h - 30)     # expect_foreground=None，桌面点击
    dt = time.perf_counter() - t0
    assert dt < 0.05, f"驱动点击应亚毫秒级，实际 {dt*1000:.1f}ms"


@pytest.mark.skipif(not _LIVE, reason="真机冒烟：需 SJZ_LIVE=1 且驱动已装")
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


def _watch(events=(), since=0.0):
    """离线 DeliveryWatch：不建 Raw Input 窗口（那会真注册设备）。"""
    import threading

    w = object.__new__(X.DeliveryWatch)
    w._cv = threading.Condition()
    w._lock = threading.RLock()
    w._events = list(events)
    w._reset_ticks = since
    return w


def test_wait_buttons_all_zero_wants_is_legal():
    """want_down=0 是合法输入：不要求任何事件就立刻返回已满足。

    以前 `downs[0]` 在空序列上直接 IndexError —— 接口复用必踩。
    """
    r = _watch(since=X._qpc()).wait_buttons(want_down=0, want_up=0,
                                            want_moves=0, timeout_ms=0)
    assert r["ok"] is True
    assert (r["down"], r["up"], r["moves"]) == (0, 0, 0)
    assert r["t_down"] is None and r["t_up"] is None
    assert r["elapsed_ms"] == 0.0


def test_wait_buttons_zero_down_with_up_event():
    """只要 up、不要 down（want_down=0）：拿到 up 就返回，不碰 downs[0]。"""
    ev = {"t": X._qpc(), "kind": "up", "hdev": 1, "name": ""}
    r = _watch([ev]).wait_buttons(want_down=0, want_up=1, want_moves=0,
                                  timeout_ms=1)
    assert r["ok"] is True
    assert (r["down"], r["up"]) == (0, 1)
    assert r["t_down"] is None and r["t_up"] == ev["t"]
    assert r["elapsed_ms"] >= 0.0


class _WrappedDrv:
    """被包装过的驱动：方法都在，但没声明 supports_batch 能力位。"""

    gen = 1

    def __init__(self):
        self.calls = []

    def mouse_device(self, force=False):
        return {"device": 14, "vid": 0x1234, "pid": 0x5678,
                "virtual": False, "hardware_id": r"HID\VID_1234",
                "source": "auto"}

    def send_mouse(self, state, x=0, y=0, flags=0, device=None, **kw):
        self.calls.append(state)

    def send_mouse_batch(self, strokes, device=None):      # noqa: ARG002
        raise AssertionError("没有能力位就不该走批路径")


class _ClickU32:
    def GetCursorPos(self, ref):
        ref._obj.x, ref._obj.y = 300, 200
        return 1

    def SetCursorPos(self, x, y):
        return 1

    def GetSystemMetrics(self, index):
        return {76: 0, 77: 0, 78: 1919, 79: 1079}.get(index, 0)

    def GetClipCursor(self, ref):
        return 0


def test_wrapped_driver_degrades_with_one_warning(monkeypatch, caplog):
    """包装驱动（无 supports_batch）不得静默降级：逐条 send 能点，且告警一次。"""
    import logging

    from core import input as IM

    drv = _WrappedDrv()

    class _Scr:
        rect = {"left": 0, "top": 0}

        @property
        def base_rect(self):
            return self.rect

    monkeypatch.setattr(X, "shared", lambda: drv)
    monkeypatch.setattr(X, "delivery_watch", lambda: None)
    monkeypatch.setattr(IM, "_user32", _ClickU32())
    monkeypatch.setattr(IM, "_cursor_pos", lambda: (300, 200))
    caplog.set_level(logging.WARNING, logger="core.input")

    m = object.__new__(Mouse)
    m._init_click_perf()
    m._mode = "interception"
    m._screen = _Scr()
    m._vw, m._vh = 1919, 1079
    m._occ = []
    m._verify = False

    assert m._click_driver(10, 10, verify=False) is True
    assert m._click_driver(10, 11, verify=False) is True
    assert drv.calls == [0, 1, 2, 0, 1, 2], \
        f"应按逐条 send 发送 move/down/up，实际 {drv.calls}"
    warns = [r for r in caplog.records if "退回逐条 send" in r.getMessage()]
    assert len(warns) == 1, f"降级原因只应告警一次，实际 {len(warns)} 次"
