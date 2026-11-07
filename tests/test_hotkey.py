"""`core.hotkey` 的单元测试：规格解析、Win32 注册/消息循环、去抖。

以前 `Hotkey` 类零覆盖 —— 现有 test_hotkey_start.py 测的是 server 里的
「按 F7 开抢」编排，不是热键实现本身。这里用假的 `_user32/_kernel32`
（不注册真热键、不碰键盘），验证：

  · 注册失败必须把错误码带回给用户（「被别的程序占用(1409)」）
  · 只有 wParam == 自己的 id 的 WM_HOTKEY 才回调
  · 退出消息会走到 UnregisterHotKey
  · 低级钩子的 `_fire` 按住不放时 0.4s 去抖
"""
import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import hotkey  # noqa: E402


# ---------------------------------------------------------------- 假 Win32

class FakeHotkeyApi:
    """`RegisterHotKey` + 线程消息队列的最小实现。"""

    def __init__(self, *, register_ok=True, register_error=1409):
        self.register_ok = register_ok
        self.register_error = register_error
        self.register_calls = []
        self.unregister_calls = []
        self.getmessage_calls = 0
        self.cond = threading.Condition()
        self.queue = []
        self.tid = None

    def RegisterHotKey(self, hwnd, hid, mods, vk):
        self.register_calls.append((hwnd, hid, mods, vk))
        if not self.register_ok:
            ctypes.set_last_error(self.register_error)
        return 1 if self.register_ok else 0

    def UnregisterHotKey(self, hwnd, hid):
        self.unregister_calls.append((hwnd, hid))
        return 1

    def GetMessageW(self, ref, hwnd, lo, hi):
        self.getmessage_calls += 1
        with self.cond:
            while not self.queue:
                self.cond.wait(timeout=0.2)
            _tid, message, wparam = self.queue.pop(0)
        msg = ref._obj
        msg.message = message
        msg.wParam = wparam
        return 1

    def PostThreadMessageW(self, tid, message, wparam, _lparam):
        with self.cond:
            self.queue.append((tid, message, wparam))
            self.cond.notify_all()
        return 1

    def TranslateMessage(self, _ref):
        return 1

    def DispatchMessageW(self, _ref):
        return 1


class FakeKernel32:
    """GetCurrentThreadId + 命名互斥体（按名字在字典里占位）。"""

    def __init__(self, tid=0x1234):
        self.tid = tid
        self.calls = 0
        self.mutexes = {}          # name -> handle
        self._next_handle = 0x200
        self.create_calls = []
        self.close_calls = []

    def GetCurrentThreadId(self):
        self.calls += 1
        return self.tid

    def CreateMutexW(self, _attrs, _initial_owner, name):
        self.create_calls.append(name)
        self._next_handle += 1
        handle = self._next_handle
        if name in self.mutexes:
            ctypes.set_last_error(hotkey._ERROR_ALREADY_EXISTS)
            return handle            # 探测句柄：acquire 会立刻 CloseHandle
        ctypes.set_last_error(0)
        self.mutexes[name] = handle
        return handle

    def CloseHandle(self, handle):
        self.close_calls.append(handle)
        for name, h in list(self.mutexes.items()):
            if h == handle:
                del self.mutexes[name]
        return 1


def _install(monkeypatch, api=None):
    api = api or FakeHotkeyApi()
    kernel = FakeKernel32()
    monkeypatch.setattr(hotkey, "_user32", api)
    monkeypatch.setattr(hotkey, "_kernel32", kernel)
    return api, kernel


def _wait(predicate, timeout=1.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------- parse

def test_parse_bare_function_key():
    assert hotkey.parse("F8") == (0, 0x77)
    assert hotkey.parse(" f10 ") == (0, 0x79)


def test_parse_modifier_combinations():
    assert hotkey.parse("Ctrl+F8") == (hotkey._MOD_CONTROL, 0x77)
    assert hotkey.parse("alt+shift+F9") == (hotkey._MOD_ALT | hotkey._MOD_SHIFT, 0x78)
    assert hotkey.parse("Win+F10") == (hotkey._MOD_WIN, 0x79)
    # 全角＋号是界面输入法常见的写法
    assert hotkey.parse("Shift＋F8") == (hotkey._MOD_SHIFT, 0x77)


def test_parse_rejects_what_it_cannot_register():
    assert hotkey.parse("") is None
    assert hotkey.parse("   ") is None
    assert hotkey.parse("F13") is None          # 不在表里
    assert hotkey.parse("Ctrl+G") is None       # 代码只支持功能键/少数几个键
    assert hotkey.parse("Foo+F8") is None       # 不认识的修饰键


# ---------------------------------------------------------------- Hotkey

def test_register_failure_reports_the_real_error_code(monkeypatch):
    api, _ = _install(monkeypatch, FakeHotkeyApi(register_ok=False, register_error=1409))
    hk = hotkey.Hotkey("F8")
    ok, msg = hk.start()

    assert ok is False
    assert "1409" in msg, f"注册失败没带错误码，用户查不到原因：{msg}"
    assert "已被别的程序注册" in msg and "换一个键" in msg
    assert api.register_calls == [(None, hk._id, 0, 0x77)]
    hk.stop()


def test_start_registers_the_parsed_modifiers_and_key(monkeypatch):
    api, _ = _install(monkeypatch)
    hk = hotkey.Hotkey("Ctrl+F8")
    ok, msg = hk.start()

    assert ok is True
    assert "F8" in msg and "就绪" in msg
    _hwnd, hid, mods, vk = api.register_calls[0]
    assert hid == hk._id
    assert mods == hotkey._MOD_CONTROL
    assert vk == 0x77
    hk.stop()


def test_unparseable_spec_never_touches_win32(monkeypatch):
    api, kernel = _install(monkeypatch)
    hk = hotkey.Hotkey("F13")
    ok, msg = hk.start()

    assert ok is False and "认不出" in msg
    assert hk._thread is None, "认不出规格还起了线程"
    assert api.register_calls == []
    assert kernel.calls == 0


def test_second_start_does_not_register_again(monkeypatch):
    api, _ = _install(monkeypatch)
    hk = hotkey.Hotkey("F8")
    assert hk.start()[0] is True
    ok, msg = hk.start()

    assert ok is True and msg == "已经在监听"
    assert len(api.register_calls) == 1, "第二次 start 又注册了一遍热键"
    hk.stop()


def test_only_the_matching_hotkey_id_fires_the_callback(monkeypatch):
    api, _ = _install(monkeypatch)
    fired = []
    dispatched = []

    def cb():
        fired.append(1)

    hk = hotkey.Hotkey("F8", callback=cb)
    real_dispatch = hk._dispatch
    hk._dispatch = lambda fn: dispatched.append(fn)      # 同步记录，避免竞态
    assert hk.start()[0] is True

    # 别人的热键（wParam 不是我们的 id）
    api.PostThreadMessageW(hk._tid, hotkey._WM_HOTKEY, hk._id + 1, 0)
    api.PostThreadMessageW(hk._tid, hotkey._WM_HOTKEY, 0, 0)
    assert _wait(lambda: api.getmessage_calls >= 2), "消息循环没消费到两条消息"
    time.sleep(0.05)
    assert dispatched == [], "wParam 不匹配的 WM_HOTKEY 也回调了"

    # 我们的热键
    api.PostThreadMessageW(hk._tid, hotkey._WM_HOTKEY, hk._id, 0)
    assert _wait(lambda: len(dispatched) == 1), "命中的 WM_HOTKEY 没有派发回调"
    hk._dispatch = real_dispatch
    hk.stop()


def test_stop_posts_quit_and_unregisters_once(monkeypatch):
    api, _ = _install(monkeypatch)
    hk = hotkey.Hotkey("F8")
    assert hk.start()[0] is True
    hid = hk._id

    assert hk.stop() is True
    assert api.unregister_calls == [(None, hid)], (
        f"退出没走 UnregisterHotKey：{api.unregister_calls}"
    )
    st = hk.status()
    assert st["active"] is False
    assert st["msg"] == "已停止"

    # 第二次 stop 不该再发一遍 WM_QUIT / 再注销一次
    hk.stop()
    assert api.unregister_calls == [(None, hid)]


def test_status_is_inactive_when_the_thread_is_gone(monkeypatch):
    api, _ = _install(monkeypatch)
    hk = hotkey.Hotkey("F8")
    assert hk.start()[0] is True
    hk.stop()
    assert hk._thread is not None and not hk._thread.is_alive()
    assert hk.status()["active"] is False


# ---------------------------------------------------------------- HookHotkey

def test_hook_hotkey_fire_is_debounced_for_0_4s(monkeypatch):
    # 初始 _last_fire = 0.0；从 0 开始计时会被「距今 0 秒 < 0.4」挡掉，
    # 那是实现里的启动保护。用大一点的基准时间模拟「程序已经跑了一会儿」。
    clock = {"t": 1000.0}
    monkeypatch.setattr(hotkey.time, "perf_counter", lambda: clock["t"])
    fired = []

    ev = threading.Event()

    def cb():
        fired.append(1)
        ev.set()

    hk = hotkey.HookHotkey("F8", callback=cb)
    hk._fire()
    assert ev.wait(1.0), "钩子回调没有派发"

    ev.clear()
    clock["t"] = 1000.1
    hk._fire()
    time.sleep(0.1)          # 真的睡：perf_counter 已被替换成假钟
    assert len(fired) == 1, "0.4s 内按住不放被重复触发"

    clock["t"] = 1000.45
    hk._fire()
    assert ev.wait(1.0), "过了去抖窗口仍然不触发"
    assert len(fired) == 2


def test_hook_hotkey_rejects_bad_spec_without_spawning(monkeypatch):
    api, kernel = _install(monkeypatch)
    hk = hotkey.HookHotkey("F13")
    ok, msg = hk.start()

    assert ok is False and "认不出" in msg
    assert hk._thread is None
    assert kernel.calls == 0
    assert getattr(api, "register_calls", []) == []
