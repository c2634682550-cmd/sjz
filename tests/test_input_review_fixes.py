"""独立复核四项返工的回归测试：F2 / F7 / F8 / F5。

F2（阻塞）message 通道必须先于前台校验，且永不抢前台；
F7     降级要有日志，`channel_status()` 必须有真实调用点；
F8     `_input_mode` 回写实际通道，日志按实际通道出文案；
F5     `/api/config` 收 message / input_fallback / input_message_strict，
       非法值给可读原因。
"""
import ctypes
import logging
import types

import pytest

from core import input as I
from core import nocursor as N


class _FakeUser32:
    """message/legacy 通道够用的桩：光标写操作全部可计数。"""

    def __init__(self, class_name="TestWindow", post_ok=True,
                 timeout_results=None, timeout_err=0):
        self.class_name = class_name
        self.post_ok = post_ok
        self.results = list(timeout_results or [1])
        self.timeout_err = timeout_err
        self.posts = []
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
    return I.Mouse(None, game_hwnd=99, mode="message", fallback="legacy")


# ---------------------------------------------------------------- F2（阻塞）
def test_message_click_skips_foreground_guard(monkeypatch):
    """前台不匹配时 message 通道也不抢前台、不抛错，只把状态记下来。"""
    fake = _FakeUser32()
    m = _message_mouse(monkeypatch, fake)

    grabbed = []
    monkeypatch.setattr(I.Mouse, "ensure_foreground",
                        staticmethod(lambda hwnd, *a, **kw:
                                     grabbed.append(hwnd) or True))
    monkeypatch.setattr(I.Mouse, "foreground", staticmethod(lambda: 4242))

    # 目标 99 与当前前台 4242 不匹配 —— 旧实现会先 ensure_foreground 再分发
    ok = m.click(100, 100, expect_foreground=99)
    assert ok is False                        # I2：没有「已处理」证据就不报成功
    assert grabbed == []                      # 一次都没抢前台
    assert fake.set_calls == 0                # 一次都没碰真实光标
    last = m.channel_status()["last_click"]
    assert last["channel"] == "message"
    assert last["accepted"] is True           # 投递成功
    assert last["processed"] is None          # 但目标有没有处理 = unknown
    assert last["retry_suggested"] is True
    assert last["foreground_ok"] is False     # 状态可观测，不是静默


def test_message_click_reports_foreground_ok_when_matching(monkeypatch):
    fake = _FakeUser32()
    m = _message_mouse(monkeypatch, fake)
    monkeypatch.setattr(I.Mouse, "foreground", staticmethod(lambda: 99))
    assert m.click(100, 100, expect_foreground=99) is False
    last = m.channel_status()["last_click"]
    assert last["foreground_ok"] is True
    assert last["accepted"] is True
    assert last["processed"] is None


def test_legacy_click_still_guards_foreground(monkeypatch):
    """legacy 需要光标真的落在目标窗口上，前台校验必须保留。"""
    fake = _FakeUser32()
    m = _message_mouse(monkeypatch, fake)
    m._mode = "legacy"                        # 只为走分支，不重跑降级决策
    monkeypatch.setattr(I.Mouse, "foreground", staticmethod(lambda: 4242))
    calls = []
    monkeypatch.setattr(I.Mouse, "ensure_foreground",
                        staticmethod(lambda hwnd, *a, **kw:
                                     calls.append(hwnd) or False))
    with pytest.raises(I.WindowNotForeground):
        m.click(100, 100, expect_foreground=99)
    assert calls == [99]                      # 抢过一次（抢不回来才报错）


# ------------------------------------------------------------------------ F7
def test_degrade_is_logged_and_status_is_queried(monkeypatch, caplog):
    monkeypatch.setattr(I, "_driver_probe", lambda: (False, "dll 没了"))
    # legacy 会借系统光标，默认禁用；这里显式打开 input_legacy_borrow。
    monkeypatch.setattr(
        I, "_config_value",
        lambda key, default=None: True if key == "input_legacy_borrow" else default)
    with caplog.at_level(logging.WARNING, logger=I.__name__):
        m = I.Mouse(None, game_hwnd=1, mode="interception", fallback="legacy")
    assert m.mode == "legacy"
    st = m.channel_status()
    assert st["degraded"] is True
    assert st["reason_code"] == I.R_DRIVER_UNAVAILABLE
    assert isinstance(m._init_status, dict)   # 构造时就调过 channel_status()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "输入通道降级" in text             # 降级有日志
    assert "dll 没了" in text                 # 带原因，不是只记「降级了」


# ------------------------------------------------------------------------ F8
def test_input_mode_label_follows_effective_channel(monkeypatch):
    fake = _FakeUser32()
    m = _message_mouse(monkeypatch, fake)
    assert m.channel_label == "message(纯消息)"

    m._mode = "legacy"
    assert m.channel_label == "legacy(借光标)"


# ------------------------------------------------------------------------ F5
def test_config_api_accepts_message_and_new_keys():
    from core import server as S
    assert S._as_input_mode("message") == "message"
    assert S._as_input_mode("INTERCEPTION") == "interception"
    assert S._as_input_mode("telepathy") is None
    assert S._as_input_fallback("fail") == "fail"
    assert S._as_input_fallback("message") == "message"
    assert S._as_input_fallback("bogus") is None
    assert S._as_strict_bool("maybe") is None
    assert S._as_strict_bool("否") is False
    for key in ("input_mode", "input_fallback", "input_message_strict"):
        assert key in S._CONFIG_COERCERS


def test_config_api_rejects_bad_values_with_readable_reason():
    from core import server as S

    fake = types.SimpleNamespace(
        _json=lambda payload, code=200: (payload, code))

    payload, code = S.Handler._save_config(fake, {"input_mode": "telepathy"})
    assert code == 400
    assert "input_mode" in payload["msg"] and "不合法" in payload["msg"]

    payload, code = S.Handler._save_config(fake, {"input_fallback": "bogus"})
    assert code == 400
    assert "input_fallback" in payload["msg"]

    payload, code = S.Handler._save_config(
        fake, {"input_message_strict": "maybe"})
    assert code == 400
    assert "input_message_strict" in payload["msg"]
