"""I2：消息通道必须区分「投递成功(accepted)」与「目标已处理(processed)」。

故障注入场景（reviewer 原话）：窗口受理消息但不处理，旧实现返回 True，
上层看到 `click=True accepted=True functional=unknown` 当成点击成功 —— 假成交。

本组用例覆盖四种结果：
  1. 目标不处理       → processed=None / success=False / MESSAGE_UNPROVEN
  2. 目标正常处理     → processed=True / success=True（有目标侧状态变化）
  3. 目标销毁         → 抛 WINDOW_GONE
  4. 目标挂起         → 抛 TARGET_HUNG
  5. UIPI 拦截        → 抛 UIPI_BLOCKED
"""
import ctypes

import pytest

from core import input as I
from core import nocursor as N

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202


class _FakeTarget:
    """会「受理消息」的窗口桩；`handle` 决定它处不处理鼠标消息。"""

    def __init__(self, handle=True, behavior="ok"):
        self.class_name = "TestWindow"
        self.handle = handle
        self.behavior = behavior          # ok / hung / uipi / destroyed / reject
        self.alive = True
        self.title = "idle"
        self.sends = []
        self.posts = []

    # ---- 窗口存在性 / 类名 / 标题（window_signature 取证用） ----
    def IsWindow(self, hwnd):
        return 1 if self.alive else 0

    def GetClassNameW(self, hwnd, buf, size):
        for i, ch in enumerate(self.class_name[: size - 1]):
            buf[i] = ch
        return len(self.class_name)

    def GetWindowTextW(self, hwnd, buf, size):
        text = self.title
        for i, ch in enumerate(text[: size - 1]):
            buf[i] = ch
        return len(text)

    # ---- 投递 ----
    def SendMessageTimeoutW(self, hwnd, msg, wp, lp, flags, timeout, out):
        self.sends.append(msg)
        if self.behavior == "destroyed":
            self.alive = False
            ctypes.set_last_error(1400)       # ERROR_INVALID_WINDOW_HANDLE
            return 0
        if self.behavior == "hung":
            ctypes.set_last_error(1460)       # ERROR_TIMEOUT
            return 0
        if self.behavior == "uipi":
            ctypes.set_last_error(5)          # ERROR_ACCESS_DENIED
            return 0
        if self.behavior == "reject":
            ctypes.set_last_error(87)         # ERROR_INVALID_PARAMETER
            return 0
        if self.handle and msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN):
            # 目标「处理了」的可观测痕迹：窗口标题跟着按下变了
            self.title = "clicked"
        try:
            out._obj.value = 0
        except Exception:                     # noqa: BLE001
            pass
        return 1

    def PostMessageW(self, hwnd, msg, wp, lp):
        self.posts.append(msg)
        return 1

    def ScreenToClient(self, hwnd, point):
        return 1


def _bind(monkeypatch, fake):
    monkeypatch.setattr(N, "_user32", fake)
    monkeypatch.setattr(I, "process_integrity", lambda hwnd=None: 0x2000)


def _mouse(monkeypatch, fake):
    _bind(monkeypatch, fake)
    return I.Mouse(None, game_hwnd=99, mode="message", fallback="legacy")


def test_message_accepted_but_not_processed_is_not_success(monkeypatch):
    """核心故障复现：窗口受理（投递成功）但根本不处理 → 不许报成功。"""
    fake = _FakeTarget(handle=False)
    m = _mouse(monkeypatch, fake)

    assert WM_LBUTTONDOWN not in fake.sends   # 点击前没投过任何鼠标消息
    ok = m.click(50, 60)
    assert ok is False                   # 不再是「投递成功 = 成功」
    assert WM_LBUTTONDOWN in fake.sends  # 消息同步投递到了窗口过程
    last = m.channel_status()["last_click"]
    assert last["accepted"] is True      # 投递：成功
    assert last["processed"] is None     # 处理：unknown（拿不到证据）
    assert last["success"] is False
    assert last["reason_code"] == N.R_MESSAGE_UNPROVEN
    assert last["retry_suggested"] is True


def test_message_processed_with_observable_effect_is_success(monkeypatch):
    """目标处理了并留下可观测状态变化 → 才算成功。"""
    fake = _FakeTarget(handle=True)
    m = _mouse(monkeypatch, fake)

    assert m.click(50, 60) is True
    last = m.channel_status()["last_click"]
    assert last["accepted"] is True
    assert last["processed"] is True
    assert last["success"] is True
    assert last["retry_suggested"] is False
    assert "title" in (last["changed"] or [])   # 证据来源可追溯
    assert last["evidence"] == "state:title"


def test_message_effect_probe_can_supply_processed_evidence(monkeypatch):
    """没有窗口状态变化时，上层可注入自己的「生效」探针。"""
    from core.nocursor import Clicker

    fake = _FakeTarget(handle=False)
    _bind(monkeypatch, fake)
    seen = []

    def probe():
        seen.append(1)
        return True

    c = Clicker(99, "message", effect_probe=probe)
    assert c.click(50, 60) is True
    assert seen == [1]
    assert c.last_result["evidence"] == "caller_probe"
    assert c.last_result["processed"] is True


def test_message_destroyed_target_has_distinct_code(monkeypatch):
    fake = _FakeTarget()
    m = _mouse(monkeypatch, fake)
    fake.behavior = "destroyed"          # 构造后销毁（模拟真实时序）

    with pytest.raises(I.InputChannelError) as ei:
        m.click(50, 60)
    assert ei.value.reason_code == N.R_WINDOW_GONE
    assert ei.value.channel == "message"
    assert m.channel_status()["last_click"]["success"] is False


def test_message_hung_target_has_distinct_code(monkeypatch):
    fake = _FakeTarget()
    m = _mouse(monkeypatch, fake)
    fake.behavior = "hung"

    with pytest.raises(I.InputChannelError) as ei:
        m.click(50, 60)
    assert ei.value.reason_code == N.R_TARGET_HUNG
    assert ei.value.channel == "message"
    assert m.channel_status()["last_click"]["processed"] is False
    assert m.channel_status()["last_click"]["success"] is False


def test_message_uipi_block_has_distinct_code(monkeypatch):
    fake = _FakeTarget()
    m = _mouse(monkeypatch, fake)
    fake.behavior = "uipi"

    with pytest.raises(I.InputChannelError) as ei:
        m.click(50, 60)
    assert ei.value.reason_code == N.R_UIPI_BLOCKED
    assert ei.value.channel == "message"
    assert m.channel_status()["last_click"]["success"] is False
