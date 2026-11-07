"""HookHotkey 的跨实例互斥（命名互斥体）：单元 + 真子进程两层证据。

缺陷：低级钩子没有独占语义，开两个实例时同一个热键会在两个进程里都触发。
修复靠 `CreateMutexW` + `ERROR_ALREADY_EXISTS`，名字由 parse 后的
(修饰键, 键码) 决定。这里钉住五个场景：

  1. 同进程内两个对象注册同一热键 —— 第二个必须失败，并给出可读原因
  2. 两个**真实子进程**同时注册 —— 第二个失败，第一个仍在监听
  3. 第一个进程被强杀 —— 名字随句柄消失，第二个立刻能注册
  4. 正常 stop() / 子进程正常退出 —— 名字释放，可以重新注册
  5. RegisterHotKey 那条路共用同一个名字，且 server 不会在冲突时回退

单元用例把 `_kernel32/_user32` 换成假实现（按名字占位的字典），不装真钩子；
跨进程用例子进程里跑真的 HookHotkey（真互斥体、真低级钩子），父进程只读写
JSON 和 kill。
"""
import ctypes
import contextlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import hotkey, server  # noqa: E402

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_hotkey_child.py")

# 用产品默认值之外的键：F12 给跨进程用例，F11 给同进程真互斥用例。
# 这样即使本机正开着产品（F7/F8），测试也不会撞上它。
SPEC_PROC = "F12"
SPEC_LOCAL = "F11"


# ---------------------------------------------------------------- 假 Win32

class FakeKernel32:
    """命名互斥体：名字在字典里就算被占；CloseHandle 删掉名字。"""

    def __init__(self, tid=0x1234):
        self.tid = tid
        self._next_handle = 0x300
        self.names = {}
        self.create_calls = []
        self.closed = []

    def GetCurrentThreadId(self):
        return self.tid

    def CreateMutexW(self, _attrs, _initial_owner, name):
        self.create_calls.append(name)
        self._next_handle += 1
        handle = self._next_handle
        if name in self.names:
            ctypes.set_last_error(hotkey._ERROR_ALREADY_EXISTS)
            return handle
        ctypes.set_last_error(0)
        self.names[name] = handle
        return handle

    def CloseHandle(self, handle):
        self.closed.append(handle)
        for name, h in list(self.names.items()):
            if h == handle:
                del self.names[name]
        return 1


class FakeUser32:
    """钩子 + 热键 + 线程消息队列的最小假实现。"""

    def __init__(self, hook_ok=True, register_ok=True, register_error=1409):
        self.hook_ok = hook_ok
        self.register_ok = register_ok
        self.register_error = register_error
        self.hook_calls = 0
        self.register_calls = []
        self.unregister_calls = []
        self.pressed = set()             # 当前按下的键（GetAsyncKeyState）
        self.cond = threading.Condition()
        self.queue = []

    def GetAsyncKeyState(self, vk):
        return -32768 if vk in self.pressed else 0

    def SetWindowsHookExW(self, _kind, _proc, _mod, _tid):
        self.hook_calls += 1
        ctypes.set_last_error(0 if self.hook_ok else 1428)
        return 0x9001 if self.hook_ok else 0

    def UnhookWindowsHookEx(self, _hook):
        return 1

    def CallNextHookEx(self, _hook, _code, _wp, _lp):
        return 0

    def GetMessageW(self, ref, _hwnd, _lo, _hi):
        with self.cond:
            while not self.queue:
                self.cond.wait(timeout=0.1)
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

    def RegisterHotKey(self, hwnd, hid, mods, vk):
        self.register_calls.append((hwnd, hid, mods, vk))
        ctypes.set_last_error(0 if self.register_ok else self.register_error)
        return 1 if self.register_ok else 0

    def UnregisterHotKey(self, hwnd, hid):
        self.unregister_calls.append((hwnd, hid))
        return 1


def _install(monkeypatch, *, user32=None):
    k32 = FakeKernel32()
    u32 = user32 or FakeUser32()
    monkeypatch.setattr(hotkey, "_kernel32", k32)
    monkeypatch.setattr(hotkey, "_user32", u32)
    return k32, u32


# ---------------------------------------------------------------- 场景 1：同进程

def test_second_hook_instance_in_same_process_is_rejected(monkeypatch):
    k32, _ = _install(monkeypatch)
    a = hotkey.HookHotkey(SPEC_LOCAL)
    assert a.start()[0] is True

    b = hotkey.HookHotkey(SPEC_LOCAL)
    ok, msg = b.start()

    assert ok is False
    assert "重复注册" in msg and "错误码 183" in msg
    assert "结束其它实例" in msg
    assert b._thread is None, "冲突时还起了钩子线程"
    assert b._conflict is True
    assert b.status()["active"] is False
    assert msg in b.status()["msg"]
    a.stop()

    # 名字确实只被创建过两次（一次成功、一次探测失败）
    name = hotkey._mutex_name(0, hotkey._VK["F11"])
    assert k32.create_calls.count(name) == 2
    assert name not in k32.names, "stop() 之后名字没放开"


def test_registerhotkey_path_shares_the_same_name(monkeypatch):
    """两条实现必须互斥：一个实例走钩子、另一个回退 RegisterHotKey 也不行。"""
    _install(monkeypatch)
    hook = hotkey.HookHotkey(SPEC_LOCAL)
    assert hook.start()[0] is True

    reg = hotkey.Hotkey(SPEC_LOCAL)
    ok, msg = reg.start()
    assert ok is False and "重复注册" in msg and "错误码 183" in msg
    assert reg._thread is None
    hook.stop()

    # 放开之后 RegisterHotKey 那条路也能正常注册
    reg2 = hotkey.Hotkey(SPEC_LOCAL)
    assert reg2.start()[0] is True
    reg2.stop()


def test_modifier_matching_is_exact_not_vk_only(monkeypatch):
    """F8 与 Ctrl+F8 必须各自独立：以前只比 vkCode，一次 F8 两边都触发。"""
    _, u32 = _install(monkeypatch)
    f8 = hotkey._VK["F8"]

    u32.pressed = set()
    assert hotkey._matches(0, f8, f8) is True
    assert hotkey._matches(hotkey._MOD_CONTROL, f8, f8) is False

    u32.pressed = {hotkey._VK_CONTROL}
    assert hotkey._matches(hotkey._MOD_CONTROL, f8, f8) is True
    assert hotkey._matches(0, f8, f8) is False            # 单键不认组合键
    assert hotkey._matches(hotkey._MOD_ALT, f8, f8) is False   # Ctrl != Alt

    u32.pressed = {hotkey._VK_CONTROL, hotkey._VK_SHIFT}
    assert hotkey._matches(hotkey._MOD_CONTROL, f8, f8) is False  # 多按一个也不行
    assert hotkey._matches(hotkey._MOD_CONTROL | hotkey._MOD_SHIFT, f8, f8) is True


def test_release_allows_reregistration_in_same_process(monkeypatch):
    _install(monkeypatch)
    a = hotkey.HookHotkey(SPEC_LOCAL)
    assert a.start()[0] is True

    # 同一个对象：stop 后重注册（可重入）
    assert a.stop() is True
    assert a.start()[0] is True
    assert a.stop() is True

    # 另一个对象接管这个名字
    b = hotkey.HookHotkey(SPEC_LOCAL)
    assert b.start()[0] is True
    b.stop()


def test_hook_install_failure_releases_the_name(monkeypatch):
    """钩子装不上时不能把名字攥着 —— 否则回退 RegisterHotKey 一定失败。"""
    k32, _ = _install(monkeypatch, user32=FakeUser32(hook_ok=False))
    h = hotkey.HookHotkey(SPEC_LOCAL)
    ok, msg = h.start()

    assert ok is False and "钩子安装失败" in msg
    assert h._conflict is False, "把安装失败误判成跨实例占用"
    name = hotkey._mutex_name(0, hotkey._VK["F11"])
    assert name not in k32.names, "安装失败后名字没放开，回退路径会被自己挡住"

    probe = hotkey._HotkeyMutex(name)
    assert probe.acquire()[0] is True
    probe.release()


def test_mutex_name_uses_parsed_key_not_raw_text():
    assert hotkey._mutex_name(*hotkey.parse("f8")) == \
        hotkey._mutex_name(*hotkey.parse(" F8 "))
    assert hotkey._mutex_name(*hotkey.parse("Ctrl+F8")) != \
        hotkey._mutex_name(*hotkey.parse("F8"))
    assert hotkey._mutex_name(0, 0x7B).startswith("Local\\dafors.hotkey.")


def test_status_releases_the_name_after_the_hook_thread_died(monkeypatch):
    """钩子线程被系统回收后，不能留一个空壳继续挡着别的实例。"""
    k32, _ = _install(monkeypatch)
    a = hotkey.HookHotkey(SPEC_LOCAL)
    assert a.start()[0] is True

    a._thread = None                     # 模拟线程消失
    st = a.status()
    assert st["active"] is False
    assert "钩子线程已退出" in st["msg"], f"状态文案自相矛盾：{st}"
    assert "已就绪" not in st["msg"]
    assert a.status()["msg"] == "热键 F11 钩子线程已退出（已释放占用）"
    name = hotkey._mutex_name(0, hotkey._VK["F11"])
    assert name not in k32.names, "钩子没了但名字还攥着"

    b = hotkey.HookHotkey(SPEC_LOCAL)
    assert b.start()[0] is True
    b.stop()
    a.stop()


def test_restart_after_dead_thread_does_not_conflict_with_itself(monkeypatch):
    """线程没了但没走 stop/status 时，start() 得先放开自己上次的名字。"""
    _install(monkeypatch)
    a = hotkey.HookHotkey(SPEC_LOCAL)
    assert a.start()[0] is True

    a._thread = None                     # 模拟线程已死、名字还攥在本进程手里
    ok, msg = a.start()
    assert ok is True, msg
    a.stop()


# ---------------------------------------------------------------- 场景 2/3/4：真子进程

def _spawn(spec):
    return subprocess.Popen(
        [sys.executable, CHILD, spec],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=os.path.dirname(CHILD))


def _hello(proc):
    line = proc.stdout.readline()
    assert line, f"子进程没有输出（已退出？）：{proc.stderr.read()}"
    return json.loads(line)


def _shutdown(proc):
    if proc.poll() is not None:
        return
    try:
        proc.stdin.write("stop\n")
        proc.stdin.flush()
        proc.wait(timeout=3)
    except Exception:                       # noqa: BLE001
        proc.kill()
        proc.wait(timeout=3)


def test_two_real_processes_second_registration_fails():
    a = _spawn(SPEC_PROC)
    b = None
    try:
        ha = _hello(a)
        assert ha["ok"] is True, ha
        assert ha["mutex"].endswith(".7b"), "名字没有按 vk 归一"

        b = _spawn(SPEC_PROC)
        hb = _hello(b)
        assert hb["ok"] is False, f"第二个进程也注册成功了：{hb}"
        assert "已被占用" in hb["msg"] and "183" in hb["msg"]
        assert a.poll() is None, "第一个进程的监听被顶掉了"
    finally:
        if b is not None:
            _shutdown(b)
        _shutdown(a)


def test_killed_first_process_leaves_no_permanent_placeholder():
    a = _spawn(SPEC_PROC)
    b = None
    try:
        assert _hello(a)["ok"] is True

        a.kill()                            # TerminateProcess，不给清理机会
        a.wait(timeout=3)

        b = _spawn(SPEC_PROC)
        hb = _hello(b)
        assert hb["ok"] is True, f"强杀后名字没释放：{hb}"
    finally:
        if b is not None:
            _shutdown(b)
        if a.poll() is None:
            a.kill()


def test_graceful_stop_lets_the_next_process_register():
    a = _spawn(SPEC_PROC)
    b = None
    try:
        assert _hello(a)["ok"] is True

        a.stdin.write("stop\n")
        a.stdin.flush()
        stopped = json.loads(a.stdout.readline())
        assert stopped["stopped"] is True
        a.wait(timeout=3)

        b = _spawn(SPEC_PROC)
        hb = _hello(b)
        assert hb["ok"] is True, f"正常退出后名字没释放：{hb}"
    finally:
        if b is not None:
            _shutdown(b)
        if a.poll() is None:
            a.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="命名互斥体是 Windows 机制")
def test_real_mutex_blocks_a_second_hook_in_this_process():
    """不靠假实现：真的 CreateMutexW + 真的低级钩子。"""
    a = hotkey.HookHotkey(SPEC_LOCAL)
    ok, msg = a.start()
    assert ok is True, msg

    b = hotkey.HookHotkey(SPEC_LOCAL)
    try:
        ok2, msg2 = b.start()
        assert ok2 is False
        assert "重复注册" in msg2 and "183" in msg2
    finally:
        b.stop()
        a.stop()

    c = hotkey.HookHotkey(SPEC_LOCAL)
    try:
        assert c.start()[0] is True, "释放后仍然注册不上"
    finally:
        c.stop()


# ---------------------------------------------------------------- 场景 5：上层呈现

def test_server_does_not_fall_back_when_another_instance_holds_the_key(monkeypatch):
    made = []

    class BusyHook:
        _conflict = True

        def __init__(self, spec, callback=None, log=None):
            self.spec = spec

        def start(self):
            return False, "热键 F12 已被占用（错误码 183）——另一个实例正在用它，先关掉或换一个键"

        def stop(self):
            pass

    class NeverRegisters:
        def __init__(self, spec, callback=None):
            made.append(spec)

        def start(self):
            return True, "就绪"

    monkeypatch.setattr(server, "HookHotkey", BusyHook)
    monkeypatch.setattr(server, "Hotkey", NeverRegisters)

    _h, ok, msg = server._make_hotkey("F12", lambda: None)
    assert ok is False
    assert "已被占用" in msg
    assert made == [], "跨实例占用还回退 RegisterHotKey，会变成钩子+热键双响应"


def test_server_still_falls_back_when_the_hook_cannot_install(monkeypatch):
    made = []

    class BrokenHook:
        _conflict = False

        def __init__(self, spec, callback=None, log=None):
            self.spec = spec

        def start(self):
            return False, "热键 F12 钩子安装失败（错误码 1428）"

        def stop(self):
            pass

    class Reg:
        def __init__(self, spec, callback=None):
            made.append(spec)

        def start(self):
            return True, "热键 F12 已就绪"

    monkeypatch.setattr(server, "HookHotkey", BrokenHook)
    monkeypatch.setattr(server, "Hotkey", Reg)

    _h, ok, msg = server._make_hotkey("F12", lambda: None)
    assert ok is True and made == ["F12"]
    assert "改用 RegisterHotKey" in msg


def test_status_carries_the_conflict_reason_to_the_ui(monkeypatch):
    reason = "热键 F12 已被占用（错误码 183）——另一个实例正在用它，先关掉或换一个键"

    class Stub:
        def status(self):
            return {"spec": "F12", "active": False, "msg": reason}

    cfg = {"hotkey": {"spec": "F12", "enabled": True,
                      "start_spec": "F7", "start_enabled": False}}

    class Cfg:
        @classmethod
        def load(cls):
            return cfg

    monkeypatch.setattr(server, "Config", Cfg)
    monkeypatch.setattr(server, "_hotkey", Stub())
    monkeypatch.setattr(server, "_hotkey_start", None)

    st = server.Handler._hotkey_status(None)   # 方法体不碰 self，只读模块全局
    assert st["stop"]["active"] is False
    assert st["stop"]["msg"] == reason


def test_set_hotkey_rolls_back_and_reregisters_old_key(monkeypatch):
    """新键没注册上时，旧热键必须被写回配置并重新注册 —— 不能保存一次就丢键。"""
    store = {"hotkey": {"enabled": True, "spec": "F8",
                        "start_enabled": False, "start_spec": "F7"}}
    calls = []

    def fake_start(hk=None):
        calls.append(dict(hk))
        if hk.get("spec") == "F12":
            return {"停止": (False, "热键 F12 已被占用（错误码 183）"
                             "——另一个实例正在用它，先关掉或换一个键"),
                    "开抢": (True, "开抢热键已关闭"),
                    "截前": (True, "截前热键已关闭")}
        return {"停止": (True, "热键 F8 已就绪（低级钩子）"),
                "开抢": (True, "开抢热键已关闭"),
                "截前": (True, "截前热键已关闭")}

    class Cfg:
        @classmethod
        def load(cls):
            return store

        @classmethod
        @contextlib.contextmanager
        def transaction(cls):
            yield store

    class Stub:
        def _json(self, payload):
            return payload

        def _hotkey_status(self):
            return server.Handler._hotkey_status(None)

    monkeypatch.setattr(server, "Config", Cfg)
    monkeypatch.setattr(server, "_start_hotkeys", fake_start)
    monkeypatch.setattr(server, "_hotkey", None)
    monkeypatch.setattr(server, "_hotkey_start", None)

    out = server.Handler._set_hotkey(
        Stub(), {"spec": "F12", "enabled": True,
                 "start_spec": "F7", "start_enabled": False})

    assert out["ok"] is False
    assert "已被占用" in out["msg"] and "已恢复原热键（F8）" in out["msg"]
    assert store["hotkey"]["spec"] == "F8", "配置没有回滚"
    assert [c["spec"] for c in calls] == ["F12", "F8"], "旧热键没有重新注册"
