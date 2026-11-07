"""输入通道三缺陷的回归测试（纯逻辑层：不投真实输入、不动真实光标）。

覆盖：
  1. message 成为正式的第三通道，且“窗口受理 ≠ 游戏逻辑生效”可判定/可上报
  2. 降级不再静默：实际通道可查、降级有账本、可配置成 fail 直接失败
  3. UIPI 主动判定 + 每次点击后的真实性校验（稳定原因码）
"""
import ctypes
import ctypes.wintypes as wintypes

import pytest

from core import config as C
from core import input as I
from core import nocursor as N


@pytest.fixture(autouse=True)
def _clean_ledger():
    I.LEDGER.clear()
    yield
    I.LEDGER.clear()


def _allow_legacy(monkeypatch):
    """显式打开 input_legacy_borrow。

    legacy 通道会 SetCursorPos 借系统光标（录屏可见、占用户鼠标），2026-11-07
    起默认禁用；任何要验证 legacy 路径的用例都必须显式打开这道闸。
    """
    monkeypatch.setattr(
        I, "_config_value",
        lambda key, default=None: True if key == "input_legacy_borrow" else default)


# ---------------------------------------------------------------- 缺陷 1：配置
def test_config_exposes_three_modes_with_defaults():
    assert C.DEFAULTS["input_mode"] == "interception"
    # 2026-11-07 起默认 fail（不再悄悄降级到借光标的 legacy）；legacy 必须
    # 显式配 input_fallback=legacy + input_legacy_borrow=true 才可达。
    assert C.DEFAULTS["input_fallback"] == "fail"
    assert C.DEFAULTS["input_legacy_borrow"] is False
    assert C.DEFAULTS["input_message_strict"] is True
    assert set(C.INPUT_MODES) == {"interception", "legacy", "message"}


def test_config_normalizes_message_and_fallback():
    cfg = C.Config({"input_mode": "MESSAGE", "input_fallback": "FAIL",
                    "input_message_strict": False})
    assert cfg["input_mode"] == "message"
    assert cfg["input_fallback"] == "fail"
    assert cfg["input_message_strict"] is False


def test_old_config_without_new_keys_still_reads():
    cfg = C.Config({"input_mode": "legacy"})
    assert cfg["input_mode"] == "legacy"
    assert cfg["input_fallback"] == "fail"
    assert cfg["input_message_strict"] is True


def test_invalid_values_fall_back_to_defaults():
    cfg = C.Config({"input_mode": "bogus", "input_fallback": "bogus",
                    "input_message_strict": "yes"})
    assert cfg["input_mode"] == "interception"
    assert cfg["input_fallback"] == "fail"
    assert cfg["input_message_strict"] is True


# ------------------------------------------------------- 缺陷 2：静默降级
def test_driver_fallback_is_recorded_and_queryable(monkeypatch):
    monkeypatch.setattr(I, "_driver_probe", lambda: (False, "dll 没了"))
    _allow_legacy(monkeypatch)
    m = I.Mouse(None, mode="interception", fallback="legacy")

    st = m.channel_status()
    assert m.mode == "legacy"
    assert st["requested"] == "interception"
    assert st["degraded"] is True
    assert st["reason_code"] == N.R_DRIVER_UNAVAILABLE
    assert st["fallback_policy"] == "legacy"
    assert m.driver_warning == "dll 没了"
    ev = [e for e in st["events"] if e["from"] == "interception"]
    assert ev and ev[-1]["to"] == "legacy" and ev[-1]["ok"] is True


def test_fallback_fail_raises_instead_of_silently_degrading(monkeypatch):
    monkeypatch.setattr(I, "_driver_probe", lambda: (False, "驱动服务没起"))
    with pytest.raises(I.InputChannelError) as ei:
        I.Mouse(None, mode="interception", fallback="fail")

    assert ei.value.reason_code == N.R_DRIVER_UNAVAILABLE
    assert ei.value.channel == "interception"
    ev = I.channel_events()
    assert ev and ev[-1]["ok"] is False
    assert ev[-1]["from"] == "interception" and ev[-1]["to"] is None


def test_no_event_when_driver_is_ready(monkeypatch):
    monkeypatch.setattr(I, "_driver_probe", lambda: (True, ""))
    # 用默认策略 fail：legacy 被禁用时即使驱动就绪也会留一条 warn，
    # 这条用例断的是「驱动就绪 → 没有任何通道事件」。
    m = I.Mouse(None, mode="interception", fallback="fail")
    assert m.mode == "interception"
    assert m.channel_status()["degraded"] is False
    assert m.driver_warning is None
    assert I.channel_events() == []


def test_keyboard_uses_same_policy(monkeypatch):
    monkeypatch.setattr(I, "_driver_probe", lambda: (False, "dll 没了"))
    # Keyboard 不传 fallback 时会去读**当前配置**（_config_value），而
    # config.json 是活的（别的进程一直在改）。这里把「策略来源」钉死在
    # 代码默认值上，否则这条用例断的是磁盘上那一刻的配置，不是在断
    # 「键盘与鼠标共用同一套策略」。legacy 额外要 input_legacy_borrow。
    policy = {"input_fallback": "legacy", "input_legacy_borrow": True}
    monkeypatch.setattr(I, "_config_value",
                        lambda key, default=None: policy.get(key, default))
    kb = I.Keyboard(1234, mode="interception")
    assert kb.mode == "legacy"
    st = kb.channel_status()
    assert st["degraded"] is True
    assert st["reason_code"] == N.R_DRIVER_UNAVAILABLE


# ------------------------------------------------------------ 缺陷 3：UIPI
def test_integrity_mismatch_fails_before_any_click(monkeypatch):
    monkeypatch.setattr(
        I, "process_integrity",
        lambda hwnd=None: 0x2000 if hwnd is None else 0x3000)
    _allow_legacy(monkeypatch)          # legacy 默认禁用，显式打开这道闸
    with pytest.raises(I.InputChannelError) as ei:
        I.Mouse(None, game_hwnd=1234, mode="legacy")

    assert ei.value.reason_code == N.R_UIPI_BLOCKED
    assert "管理员" in str(ei.value) or "UIPI" in str(ei.value)


class _FakeClickUser32:
    """只实现 legacy 路径要用的 API；任何光标写入都可断言。"""

    def __init__(self, set_ok=True, move_cursor=True, post_ok=True):
        self.cursor = (0, 0)
        self.set_calls = []
        self.posts = []
        self.set_ok = set_ok
        self.move_cursor = move_cursor
        self.post_ok = post_ok

    def GetSystemMetrics(self, idx):
        return 0

    def SetCursorPos(self, x, y):
        self.set_calls.append((int(x), int(y)))
        if self.set_ok and self.move_cursor:
            self.cursor = (int(x), int(y))
        return 1 if self.set_ok else 0

    def GetCursorPos(self, pref):
        pref._obj.x, pref._obj.y = self.cursor
        return 1

    def ScreenToClient(self, hwnd, point):
        return 1

    def PostMessageW(self, hwnd, msg, wp, lp):
        self.posts.append(msg)
        return 1 if self.post_ok else 0

    def MakeIntResource(self, *a):
        return 0


def _legacy_mouse(monkeypatch, fake, **kw):
    monkeypatch.setattr(I, "_user32", fake)
    monkeypatch.setattr(I, "process_integrity", lambda hwnd=None: 0x2000)
    _allow_legacy(monkeypatch)          # legacy 默认禁用，显式打开这道闸
    return I.Mouse(None, game_hwnd=555, mode="legacy", **kw)


def test_legacy_setcursorpos_zero_reports_reason(monkeypatch):
    fake = _FakeClickUser32(set_ok=False)
    m = _legacy_mouse(monkeypatch, fake)
    with pytest.raises(I.InputChannelError) as ei:
        m.click(300, 200, verify=False)

    assert ei.value.reason_code == N.R_CURSOR_NOT_MOVED
    assert fake.posts == []                 # 光标都没到位，不许再投消息


def test_legacy_cursor_silently_stuck_is_detected(monkeypatch):
    # SetCursorPos 返回成功但光标纹丝不动 = UIPI/ClipCursor 的典型静默失败
    fake = _FakeClickUser32(set_ok=True, move_cursor=False)
    m = _legacy_mouse(monkeypatch, fake)
    with pytest.raises(I.InputChannelError) as ei:
        m.click(300, 200, verify=False)

    assert ei.value.reason_code == N.R_CURSOR_NOT_AT_TARGET
    assert fake.posts == []


def test_legacy_success_posts_client_coords_and_restores_cursor(monkeypatch):
    fake = _FakeClickUser32()
    m = _legacy_mouse(monkeypatch, fake)
    assert m.click(300, 200, verify=False) is True

    assert N.WM_MOUSEMOVE in fake.posts
    assert N.WM_LBUTTONDOWN in fake.posts
    assert N.WM_LBUTTONUP in fake.posts
    assert fake.set_calls[-1] == (0, 0)      # 光标还回去了
    assert m.channel_status()["last_click"]["channel"] == "legacy"


# ------------------------------------------------------ 缺陷 1：message 通道
class _FakeMessageUser32:
    """纯消息通道的桩：不实现光标 API 的写操作也能跑。"""

    def __init__(self, class_name="TestWindow", post_ok=True,
                 timeout_results=None, timeout_err=0):
        self.class_name = class_name
        self.post_ok = post_ok
        self.results = list(timeout_results or [1])
        self.timeout_err = timeout_err
        self.posts = []
        self.sends = []
        self.set_calls = 0
        self.get_calls = 0

    def IsWindow(self, hwnd):
        return 1

    def GetClassNameW(self, hwnd, buf, size):
        for i, ch in enumerate(self.class_name):
            buf[i] = ch
        return len(self.class_name)

    def PostMessageW(self, hwnd, msg, wp, lp):
        self.posts.append(msg)
        return 1 if self.post_ok else 0

    def SendMessageTimeoutW(self, hwnd, msg, wp, lp, flags, timeout, out):
        self.sends.append(msg)
        res = self.results.pop(0) if self.results else 1
        if res:
            return 1
        ctypes.set_last_error(self.timeout_err)
        return 0

    def ScreenToClient(self, hwnd, point):
        return 1

    def SetCursorPos(self, x, y):
        self.set_calls += 1
        return 1

    def GetCursorPos(self, pref):
        self.get_calls += 1
        pref._obj.x = pref._obj.y = 0
        return 1


def _message_mouse(monkeypatch, fake):
    monkeypatch.setattr(N, "_user32", fake)
    monkeypatch.setattr(I, "process_integrity", lambda hwnd=None: 0x2000)
    _allow_legacy(monkeypatch)          # 降级目标 legacy 需要显式允许借光标
    return I.Mouse(None, game_hwnd=99, mode="message", fallback="legacy")


def test_message_mode_is_available_and_never_touches_cursor(monkeypatch):
    fake = _FakeMessageUser32()
    m = _message_mouse(monkeypatch, fake)

    assert m.mode == "message"
    cap = m.channel_status()["message_capability"]
    assert cap["usable"] is True
    assert cap["functional"] == "unknown"     # 结构可用 ≠ 游戏逻辑已验证

    # I2：没有任何目标侧可观测变化 → 只能报 unknown，返回值不是成功
    assert m.click(100, 100) is False
    assert fake.set_calls == 0                # 一次都没碰真实光标
    assert fake.get_calls == 0
    assert N.WM_LBUTTONDOWN in fake.sends     # 消息是同步投递（窗口过程被执行）
    last = m.channel_status()["last_click"]
    assert last["channel"] == "message"
    assert last["accepted"] is True           # 投递成功
    assert last["processed"] is None          # 处理与否 unknown，不当成功
    assert last["retry_suggested"] is True
    assert last["functional"] == "unknown"


def test_message_send_rejected_reports_stable_reason(monkeypatch):
    # 构造探测成功；点击时同步投递被拒（err=87，既不是 UIPI/超时/失效）
    fake = _FakeMessageUser32(timeout_results=[1, 1, 0], timeout_err=87)
    m = _message_mouse(monkeypatch, fake)

    with pytest.raises(I.InputChannelError) as ei:
        m.click(10, 10)
    assert ei.value.reason_code == N.R_MESSAGE_NOT_ACCEPTED
    assert ei.value.channel == "message"


def test_message_verify_catches_uipi_after_post(monkeypatch):
    # 初始探测成功，点击后的受理校验被 UIPI 拦下（err=5）
    fake = _FakeMessageUser32(timeout_results=[1, 0], timeout_err=5)
    m = _message_mouse(monkeypatch, fake)

    with pytest.raises(I.InputChannelError) as ei:
        m.click(10, 10)
    assert ei.value.reason_code == N.R_UIPI_BLOCKED


def test_message_on_unreal_window_degrades_with_reason(monkeypatch):
    fake = _FakeMessageUser32(class_name="UnrealWindow")
    m = _message_mouse(monkeypatch, fake)

    assert m.mode == "legacy"                 # 游戏读真实光标 → 纯消息不被信任
    st = m.channel_status()
    assert st["reason_code"] == N.R_MESSAGE_RAW_CURSOR
    assert st["message_capability"]["functional"] == "unsupported"
    assert "UnrealWindow" in st["reason"]


def test_message_on_unreal_window_can_fail_hard(monkeypatch):
    fake = _FakeMessageUser32(class_name="UnrealWindow")
    monkeypatch.setattr(N, "_user32", fake)
    monkeypatch.setattr(I, "process_integrity", lambda hwnd=None: 0x2000)
    with pytest.raises(I.InputChannelError) as ei:
        I.Mouse(None, game_hwnd=99, mode="message", fallback="fail")

    assert ei.value.reason_code == N.R_MESSAGE_RAW_CURSOR


def test_message_strict_off_is_warning_only(monkeypatch):
    fake = _FakeMessageUser32(class_name="UnrealWindow")
    monkeypatch.setattr(N, "_user32", fake)
    monkeypatch.setattr(I, "process_integrity", lambda hwnd=None: 0x2000)
    monkeypatch.setattr(
        I, "_config_value",
        lambda key, default: False if key == "input_message_strict" else default)

    m = I.Mouse(None, game_hwnd=99, mode="message", fallback="legacy")
    assert m.mode == "message"
    assert m.channel_status()["reason_code"] == N.R_MESSAGE_RAW_CURSOR
    warn = [e for e in I.channel_events()
            if e.get("severity") == "warn"]
    assert warn and warn[-1]["reason_code"] == N.R_MESSAGE_RAW_CURSOR


def test_message_without_hwnd_reports_no_target(monkeypatch):
    monkeypatch.setattr(I, "process_integrity", lambda hwnd=None: 0x2000)
    with pytest.raises(I.InputChannelError) as ei:
        I.Mouse(None, game_hwnd=None, mode="message", fallback="fail")
    assert ei.value.reason_code == N.R_NO_TARGET_HWND


def test_message_capability_without_hwnd():
    cap = I.message_capability(None)
    assert cap["usable"] is False
    assert cap["reason_code"] == N.R_NO_TARGET_HWND
