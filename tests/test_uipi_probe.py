"""UIPI 三态探测：可发送 / 被 UIPI 拦 / 窗口无响应。

旧实现用 `SendMessageTimeoutW(WM_NULL)` 判断 UIPI。`WM_NULL` 在 UIPI 的
**白名单**里 —— 高完整性窗口照样会应答，所以探测结果永远是「成功」，而
真正投递鼠标消息时才会 err=5。本组用例把这个假阳性钉死，并覆盖新实现的
三种结论：

  1. 高完整性目标（本机是普通权限 0x2000，造不出真的 High 窗口 → 桩）
  2. 普通窗口（真实隐藏窗口，同进程）
  3. 已销毁窗口（真实创建后 DestroyWindow）
  4. 无响应窗口（桩：线程不泵消息 → err=1460）
"""
import ctypes
import ctypes.wintypes as w

import pytest

from core import nocursor as N

WM_NULL = 0x0000

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

_user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
_user32.DefWindowProcW.restype = _LRESULT
_user32.EnumWindows.argtypes = [ctypes.c_void_p, w.LPARAM]
_user32.IsWindow.argtypes = [w.HWND]
_user32.IsWindowVisible.argtypes = [w.HWND]
_user32.SendMessageTimeoutW.argtypes = [
    w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
    ctypes.POINTER(ctypes.c_size_t)]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_k32.GetModuleHandleW.restype = w.HMODULE

_SEEN = []


def _on_message(h, m, wp, lp):
    _SEEN.append(int(m))
    return _user32.DefWindowProcW(h, m, wp, lp)


_PROC = _WNDPROC(_on_message)          # 必须持有引用，否则回调被回收


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


_CLASS_NAME = "UipiProbeHiddenWnd"
_class_ready = []


def _ensure_class():
    """注册一个只在本测试里用的窗口类（不显示窗口，不碰桌面）。"""
    if _class_ready:
        return _class_ready[0]
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
    _user32.RegisterClassW.restype = w.ATOM
    _user32.CreateWindowExW.argtypes = [
        w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
    _user32.CreateWindowExW.restype = w.HWND
    _user32.DestroyWindow.argtypes = [w.HWND]

    wc = _WNDCLASS()
    wc.lpfnWndProc = _PROC
    wc.hInstance = _k32.GetModuleHandleW(None)
    wc.lpszClassName = _CLASS_NAME
    _user32.RegisterClassW(ctypes.byref(wc))
    _class_ready.append(wc.hInstance)
    return wc.hInstance


def _create_hidden_window():
    """0x00CF0000 = WS_OVERLAPPEDWINDOW，**不含 WS_VISIBLE** → 用户看不到。"""
    inst = _ensure_class()
    hwnd = _user32.CreateWindowExW(
        0, _CLASS_NAME, "uipi-probe", 0x00CF0000,
        0, 0, 200, 120, None, None, inst, None)
    assert hwnd, f"CreateWindowExW 失败 err={ctypes.get_last_error()}"
    return hwnd


@pytest.fixture
def hidden_window():
    hwnd = _create_hidden_window()
    yield hwnd
    _user32.DestroyWindow(hwnd)


class _StubUser32:
    """只实现 probe_window / probe_message 用到的三个 API。"""

    def __init__(self, alive=True, send_ok=True, err=0, class_name="TestWindow"):
        self.alive = alive
        self.send_ok = send_ok
        self.err = err
        self.class_name = class_name
        self.sends = []

    def IsWindow(self, hwnd):
        return 1 if self.alive else 0

    def GetClassNameW(self, hwnd, buf, size):
        for i, ch in enumerate(self.class_name[: size - 1]):
            buf[i] = ch
        return len(self.class_name)

    def SendMessageTimeoutW(self, hwnd, msg, wp, lp, flags, timeout, out):
        self.sends.append(int(msg))
        if not self.send_ok:
            ctypes.set_last_error(self.err)
            return 0
        return 1


def _stub_integrity(monkeypatch, ours, target):
    monkeypatch.setattr(N, "_integrity_levels", lambda hwnd: (ours, target))


# ------------------------------------------------ 1. 高完整性目标（桩）
def test_high_integrity_target_is_uipi_blocked_not_success(monkeypatch):
    """目标 High(12288) vs 我们 Medium(8192) → 必须报 UIPI，不许报成功。

    真实高完整性窗口需要一个 High 进程来建窗口；本机控制台是普通权限
    （0x2000），构造不出来，所以这里把完整性级别桩成 8192/12288。
    """
    stub = _StubUser32()                  # 桩如果被问，也会回答「收到」
    monkeypatch.setattr(N, "_user32", stub)
    _stub_integrity(monkeypatch, 8192, 12288)

    r = N.probe_window(1)

    assert r["state"] == N.PROBE_UIPI_BLOCKED
    assert r["ok"] is False
    assert r["reason_code"] == N.R_UIPI_BLOCKED
    assert r["integrity"] == {"ours": 8192, "target": 12288, "checked": True}
    assert "12288" in r["detail"] and "8192" in r["detail"]
    assert stub.sends == [], "UIPI 判定不该依赖消息往返（WM_NULL 恒被放行）"


def test_wm_null_round_trip_alone_says_sendable(monkeypatch):
    """回归点：只看 WM_NULL 往返会得到「可发送」—— 这正是旧实现的判据。

    桩对 WM_NULL 照常应答，而它与完整性级别的比较无关，所以单靠往返
    永远问不出 UIPI 拦截。
    """
    stub = _StubUser32(send_ok=True)
    monkeypatch.setattr(N, "_user32", stub)
    _stub_integrity(monkeypatch, None, None)   # 模拟「级别读不到」

    ok, code, detail = N.window_responsive(1, timeout_ms=50)

    assert ok is True and code is None
    assert WM_NULL in stub.sends


def test_high_integrity_through_window_responsive(monkeypatch):
    """旧签名也要给出 False + UIPI 原因码（调用方已经在用这个三元组）。"""
    monkeypatch.setattr(N, "_user32", _StubUser32())
    _stub_integrity(monkeypatch, 8192, 12288)

    ok, code, detail = N.window_responsive(1)

    assert ok is False
    assert code == N.R_UIPI_BLOCKED
    assert "完整性" in detail


# ------------------------------------------------ 2. 普通窗口（真实）
def test_real_hidden_window_is_sendable(hidden_window):
    """真实窗口（同进程、普通完整性）：三态里的「可发送」。"""
    _SEEN.clear()

    r = N.probe_window(hidden_window)

    assert r["state"] == N.PROBE_SENDABLE
    assert r["ok"] is True
    assert r["reason_code"] is None
    assert r["integrity"]["checked"] is True
    assert r["integrity"]["ours"] == r["integrity"]["target"]
    assert _SEEN == [WM_NULL], "存活探测只发一条 WM_NULL"


# ------------------------------------------------ 3. 已销毁窗口（真实）
def test_real_destroyed_window_is_gone(hidden_window):
    _user32.DestroyWindow(hidden_window)
    assert not _user32.IsWindow(hidden_window)

    r = N.probe_window(hidden_window)

    assert r["state"] == N.PROBE_GONE
    assert r["ok"] is False
    assert r["reason_code"] == N.R_WINDOW_GONE


# ------------------------------------------------ 4. 无响应（桩）
def test_no_response_is_distinct_from_uipi(monkeypatch):
    """线程不泵消息（err=1460）≠ UIPI（err=5），各报各的码。"""
    stub = _StubUser32(send_ok=False, err=1460)
    monkeypatch.setattr(N, "_user32", stub)
    _stub_integrity(monkeypatch, 8192, 8192)

    r = N.probe_window(1, timeout_ms=50)

    assert r["state"] == N.PROBE_NO_RESPONSE
    assert r["reason_code"] == N.R_TARGET_HUNG
    assert r["ok"] is False
    assert "无应答" in r["detail"]


def test_three_states_never_share_an_answer(monkeypatch):
    """同一套桩，三种输入必须给出三个不同的 state（防止退化成同一个「成功」）。"""
    _stub_integrity(monkeypatch, 8192, 12288)
    monkeypatch.setattr(N, "_user32", _StubUser32())
    s_uipi = N.probe_window(1)["state"]

    _stub_integrity(monkeypatch, 8192, 8192)
    monkeypatch.setattr(N, "_user32", _StubUser32(send_ok=False, err=1460))
    s_hung = N.probe_window(1)["state"]

    monkeypatch.setattr(N, "_user32", _StubUser32(send_ok=True))
    s_ok = N.probe_window(1)["state"]

    assert {s_uipi, s_hung, s_ok} == {
        N.PROBE_UIPI_BLOCKED, N.PROBE_NO_RESPONSE, N.PROBE_SENDABLE}


# ------------------------------------------------ 5. 读数失败时的兜底
def test_unreadable_integrity_falls_back_to_message_probe(monkeypatch):
    """完整性级别读不到时退回消息往返：err=5 仍归 UIPI，err=1460 仍归无响应。"""
    _stub_integrity(monkeypatch, None, None)
    monkeypatch.setattr(N, "_user32", _StubUser32(send_ok=False, err=5))
    r = N.probe_window(1)
    assert r["state"] == N.PROBE_UIPI_BLOCKED
    assert r["integrity"]["checked"] is False

    monkeypatch.setattr(N, "_user32", _StubUser32(send_ok=False, err=1460))
    r = N.probe_window(1)
    assert r["state"] == N.PROBE_NO_RESPONSE


# ------------------------------------------------ 6. probe_message 契约
def test_probe_message_exposes_state(monkeypatch):
    monkeypatch.setattr(N, "_user32", _StubUser32(class_name="UnrealWindow"))
    _stub_integrity(monkeypatch, 8192, 12288)

    cap = N.probe_message(1)

    assert cap["usable"] is False
    assert cap["state"] == N.PROBE_UIPI_BLOCKED
    assert cap["reason_code"] == N.R_UIPI_BLOCKED
    assert cap["functional"] == "unsupported"


# --------------------------------- 7. 真机：找本机现成的高完整性窗口
def _find_high_integrity_window():
    """本机现有可见窗口里完整性级别高于自身的一个，返回
    `(hwnd, target_rid, ours)`；没有就 `(None, None, ours)`。

    构造一个真的 High 窗口需要以 High 完整性起进程（会弹 UAC），本机
    控制台是普通权限（0x2000），所以只能借机器上已有的高权限窗口做实测；
    连一个都没有时用例 skip，不假装跑过。
    """
    from core.input import process_integrity

    ours = process_integrity()
    if ours is None:
        return None, None, None
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
    def _cb(h, _):
        if _user32.IsWindowVisible(h):
            rid = process_integrity(h)
            if rid is not None and rid > ours:
                found.append((h, rid))
                return False
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:                        # noqa: BLE001
        pass
    if not found:
        return None, None, ours
    return found[0][0], found[0][1], ours


def test_real_high_integrity_window_is_blocked_while_wm_null_answers():
    """真机验证：高完整性窗口对 WM_NULL 照常应答，但新探测按 UIPI 拦。

    这条用例就是旧 bug 的现场复现：`window_responsive` 以前返回 True。
    """
    hwnd, target, ours = _find_high_integrity_window()
    if hwnd is None:
        pytest.skip("本机没有高于自身完整性的可见窗口（造 High 窗口需要提权）")

    ctypes.set_last_error(0)
    out = ctypes.c_size_t(0)
    ok = _user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, 2, 50,
                                     ctypes.byref(out))
    assert ok == 1, "WM_NULL 在白名单里，高完整性窗口应该照样应答"

    r = N.probe_window(hwnd)

    assert r["state"] == N.PROBE_UIPI_BLOCKED
    assert r["integrity"] == {"ours": ours, "target": target, "checked": True}
    assert r["ok"] is False
