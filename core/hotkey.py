"""全局热键（停止用）。

鼠标甩左上角的急停有个问题：抢购时手在键盘上，鼠标可能根本不在你手边；
而且游戏全屏时鼠标位置不受控。热键更可靠，按一下就停。

实现用 RegisterHotKey + 独立线程的消息循环。不装任何依赖。
注意：热键是全局独占的，别的程序已经占了同一个键会注册失败 —— 这时要
老实告诉用户换一个键，不能假装注册成功。
"""
import ctypes
import threading
from ctypes import wintypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008

_VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "ESC": 0x1B, "PAUSE": 0x13, "SCROLLLOCK": 0x91, "INSERT": 0x2D,
}


def parse(spec):
    """把 "F8" / "Ctrl+F8" / "Alt+Shift+F9" 解析成 (mods, vk)。认不出返回 None。"""
    if not spec:
        return None
    parts = [p.strip().upper() for p in str(spec).replace("＋", "+").split("+") if p.strip()]
    if not parts:
        return None
    key = parts[-1]
    vk = _VK.get(key)
    if vk is None:
        return None
    mods = 0
    for p in parts[:-1]:
        if p in ("CTRL", "CONTROL"):
            mods |= _MOD_CONTROL
        elif p == "ALT":
            mods |= _MOD_ALT
        elif p == "SHIFT":
            mods |= _MOD_SHIFT
        elif p in ("WIN", "META", "SUPER"):
            mods |= _MOD_WIN
        else:
            return None
    return mods, vk


# ---------------- 低级键盘钩子（首选） ----------------
# 为什么不再靠 RegisterHotKey：游戏全屏独占键盘时，系统热键经常收不到 ——
# 注册是成功的（日志写着「F7 已就绪」），按下去却毫无反应，用户反复踩到。
# 低级钩子在输入栈更底层，游戏吃键盘也照样能看见。
_WH_KEYBOARD_LL = 13
_WM_KEYDOWN = 0x0100
_WM_SYSKEYDOWN = 0x0104
_WM_KEYUP = 0x0101


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)

# **必须显式声明参数类型。** 不声明时 ctypes 按 c_int 传，64 位的
# lParam 直接溢出（实测报 "OverflowError: int too long to convert"），
# CallNextHookEx 一抛异常，钩子链就断了 —— 后面所有按键都可能卡住。
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC,
                                      ctypes.c_void_p, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                                wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int     # -1 = 出错，不能用 BOOL


class HookHotkey:
    """低级键盘钩子版热键。**只监听不拦截**（CallNextHookEx 原样放行）。

    和 `Hotkey`（RegisterHotKey）接口一致：start() 返回 (ok, msg)。
    组合键（Ctrl+F8）在这里不处理 —— 只比 vkCode，按住修饰键时也会触发，
    对「停止」这种用途反而是好事。
    """

    def __init__(self, spec="F8", callback=None, log=None):
        self.spec = spec
        self.callback = callback
        self.log = log
        self._thread = None
        self._tid = None
        self._proc = None       # **必须保住引用**，被 GC 掉会直接崩进程
        self._hook = None
        self._ok = False
        self._msg = "未启动"
        self._last_fire = 0.0

    def _fire(self):
        # 按住不放时系统会连续发 KEYDOWN，去重 0.4 秒
        import time as _t
        now = _t.perf_counter()
        if now - self._last_fire < 0.4:
            return
        self._last_fire = now
        try:
            if self.log:
                self.log(f"钩子收到 {self.spec}")
        except Exception:                     # noqa: BLE001
            pass
        try:
            if self.callback:
                self.callback()
        except Exception:                     # noqa: BLE001
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return True, "已经在监听"
        parsed = parse(self.spec)
        if not parsed:
            return False, f"认不出热键「{self.spec}」，用 F8 / F7 这种写法"
        _mods, vk = parsed
        ready = threading.Event()

        def loop():
            self._tid = _kernel32.GetCurrentThreadId()

            def handler(n_code, w_param, l_param):
                if n_code == 0 and w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    if kb.vkCode == vk:
                        self._fire()
                return _user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._proc = _HOOKPROC(handler)
            self._hook = _user32.SetWindowsHookExW(
                _WH_KEYBOARD_LL, self._proc, None, 0)
            self._ok = bool(self._hook)
            if self._ok:
                self._msg = f"热键 {self.spec} 已就绪（低级钩子）"
            else:
                err = ctypes.get_last_error()
                self._msg = f"热键 {self.spec} 钩子安装失败（错误码 {err}）"
            ready.set()
            if not self._ok:
                return
            msg = wintypes.MSG()
            while True:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:                    # WM_QUIT
                    break
                if ret == -1:                   # 出错：别静默退出
                    self._ok = False
                    self._msg = (f"热键 {self.spec} 消息循环出错"
                                 f"（{ctypes.get_last_error()}）")
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
            if self._hook:
                _user32.UnhookWindowsHookEx(self._hook)
                self._hook = None

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        ready.wait(timeout=2.0)
        return self._ok, self._msg

    def stop(self):
        if self._tid:
            _user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        self._tid = None
        self._ok = False
        self._msg = "已停止"
        return True

    def status(self):
        return {"spec": self.spec,
                "active": bool(self._thread and self._thread.is_alive() and self._ok),
                "msg": self._msg}


class Hotkey:
    """一个全局热键。start() 返回 (ok, msg)，失败时 msg 说明原因。"""

    _next_id = 0x5A00

    def __init__(self, spec="F8", callback=None, log=None):
        self.spec = spec
        self.callback = callback
        self.log = log
        self._thread = None
        self._tid = None
        self._id = 0
        self._ok = False
        self._msg = "未启动"

    def start(self):
        if self._thread and self._thread.is_alive():
            return True, "已经在监听"
        parsed = parse(self.spec)
        if not parsed:
            return False, f"认不出热键「{self.spec}」，用 F8 / Ctrl+F8 这种写法"
        mods, vk = parsed

        Hotkey._next_id += 1
        self._id = Hotkey._next_id
        ready = threading.Event()

        def loop():
            self._tid = _kernel32.GetCurrentThreadId()
            ok = _user32.RegisterHotKey(None, self._id, mods, vk)
            self._ok = bool(ok)
            if not ok:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                self._msg = (f"热键 {self.spec} 注册失败（错误码 {err}）——"
                             f"多半是别的程序已经占用了，换一个键")
            else:
                self._msg = f"热键 {self.spec} 已就绪"
            ready.set()
            if not ok:
                return
            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam == self._id:
                    try:
                        if self.callback:
                            self.callback()
                    except Exception:
                        pass
                elif msg.message == _WM_QUIT:
                    break
            _user32.UnregisterHotKey(None, self._id)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        ready.wait(timeout=2.0)
        return self._ok, self._msg

    def stop(self):
        if self._tid:
            _user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        self._tid = None
        self._ok = False
        self._msg = "已停止"
        return True

    def status(self):
        return {"spec": self.spec, "active": bool(self._thread and self._thread.is_alive() and self._ok),
                "msg": self._msg}
