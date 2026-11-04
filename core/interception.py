"""Interception 驱动绑定 —— 内核级鼠标/键盘注入。

为什么换掉 SendInput/PostMessage 组合（2026-10-30 决定）：
游戏读 Raw Input，SendInput 注入的事件带 LLMHF_INJECTED 标志被它过滤；
PostMessage 又依赖真实光标位置。SetCursorPos+PostMessage 组合能用，但每次
click 要 0.13s 左右（挪光标 40ms + 消息 60ms + 校验 50ms），火力点击太慢。

Interception（oblitum/Interception，Oblita 官方 v1.00，家用免费）挂在
kbdclass/mouclass 之上：程序把事件发给驱动，驱动在内核注回输入栈 ——
游戏收到的是**真实硬件事件**，没有注入标志，也不需要动真实光标。

DLL 来源：C:\\Users\\26346\\Downloads\\AxonSkin+鼠标驱动\\drivers\\drivers\\
interception.dll（SHA256 ab88164c…，官方原版）。驱动必须先装好：
    install-interception.exe install    （管理员 + 重启一次）

API 备忘（照 library/interception.h 抄，不能凭感觉填）：
- 设备编号 1..10 = 键盘，11..20 = 鼠标（INTERCEPTION_KEYBOARD/MOUSE 宏）
- stroke 大小 = sizeof(InterceptionMouseStroke) = 20 字节（i16×3 + i32×2
  + u32，按 4 对齐）。InterceptionStroke 就是这个 20 字节缓冲，键盘的
  8 字节 KeyStroke 装进它的前 8 字节。
- interception_send 返回发送的 stroke 数；驱动没装时 create_context
  返回 NULL 或 send 返回 0 —— 两种失败都要当「驱动不可用」。
- 绝对坐标：flag INTERCEPTION_MOUSE_MOVE_ABSOLUTE + 坐标 0..65535，
  加 VIRTUAL_DESKTOP 跨多显示器时按虚拟屏算。
"""
import ctypes
import os
import sys
import threading

# ---------------- 常量（interception.h 原文） ----------------

MAX_KEYBOARD = 10
MAX_MOUSE = 10

KEYBOARD = lambda index: index + 1                    # noqa: E731
MOUSE = lambda index: MAX_KEYBOARD + index + 1        # noqa: E731

# 键盘 stroke 的 state 位
KEY_DOWN = 0x00
KEY_UP = 0x01
KEY_E0 = 0x02

# 鼠标 stroke 的 state
MOUSE_LEFT_DOWN = 0x001
MOUSE_LEFT_UP = 0x002
MOUSE_WHEEL = 0x400

# 鼠标 stroke 的 flag
MOUSE_MOVE_RELATIVE = 0x000
MOUSE_MOVE_ABSOLUTE = 0x001
MOUSE_VIRTUAL_DESKTOP = 0x002


class MouseStroke(ctypes.Structure):
    """InterceptionMouseStroke，20 字节。

    布局是 u16 state / u16 flags / i16 rolling / i32 x / i32 y / u32 information；
    rolling 后面有 2 字节对齐填充，所以不是 2+2+2+4+4+4=18，而是 20。
    send 时按这个大小传，尺寸算错驱动会直接收 0（点击静默失效）。
    """
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_short),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("information", ctypes.c_uint),
    ]


class KeyStroke(ctypes.Structure):
    """InterceptionKeyStroke，8 字节。"""
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    ]


def _dll_candidates():
    """DLL 的查找顺序。

    打包 exe 时 DLL 跟着 _MEIPASS 走（spec 里 datas 带上）；
    源码运行时放 core/assets/（只读随仓库）；都没有再去 AxonSkin 那份。
    """
    names = ["interception.dll"]
    out = []
    if getattr(sys, "frozen", False):
        out.append(os.path.join(sys._MEIPASS, "interception.dll"))  # noqa: SLF001
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "assets", "interception.dll"))
    out.append(os.path.join(here, "interception.dll"))
    out.append(r"C:\Users\26346\Downloads\AxonSkin+鼠标驱动"
               r"\drivers\drivers\interception.dll")
    return out


class InterceptionError(RuntimeError):
    """驱动不可用（没装 / 没重启 / DLL 缺失）。"""


class _Lib:
    """惰性加载 + API 签名。加载一次，失败记住原因。"""

    def __init__(self):
        self._lib = None
        self._err = None

    def load(self):
        if self._lib is not None or self._err is not None:
            return self._lib
        for path in _dll_candidates():
            if not os.path.isfile(path):
                continue
            try:
                lib = ctypes.WinDLL(path)
            except OSError as e:
                self._err = f"{path}: {e}"
                continue
            try:
                # 签名照 interception.h：context 是 void*，send 收 (ctx, dev,
                # stroke*, n) 返回 nstroke 数
                lib.interception_create_context.restype = ctypes.c_void_p
                lib.interception_create_context.argtypes = []
                lib.interception_destroy_context.argtypes = [ctypes.c_void_p]
                lib.interception_send.restype = ctypes.c_uint
                lib.interception_send.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
                lib.interception_is_keyboard.restype = ctypes.c_int
                lib.interception_is_keyboard.argtypes = [ctypes.c_int]
                lib.interception_is_mouse.restype = ctypes.c_int
                lib.interception_is_mouse.argtypes = [ctypes.c_int]
            except AttributeError as e:
                self._err = f"{path}: 缺函数 {e}"
                continue
            self._lib = lib
            self.path = path
            return self._lib
        if self._err is None:
            self._err = "interception.dll 找不到（应放在 core/assets/ 下）"
        return None

    def error(self):
        if self._lib is None and self._err is None:
            self.load()
        return self._err


_LIB = _Lib()


class Interception:
    """一个进程一个 context。发鼠标 11 号设备、键盘 1 号设备。

    为什么不做成静态方法：context 是有资源的（内核句柄），要跟着
    destroy。Mouse/Keyboard 各持一个太浪费 —— 共享一个。
    """

    def __init__(self):
        self._ctx = None
        self._lock_err = None
        # 多线程（引擎线程 + 热键/扫描线程）同时第一次点击时会各建一个
        # context：后建的那个覆盖 self._ctx，先建的那个内核句柄就永久泄漏，
        # 而且两个 context 互相抢设备。创建/销毁必须串行化。
        self._lock = threading.RLock()

    def _open(self):
        with self._lock:
            if self._ctx is not None:
                return self._ctx
            lib = _LIB.load()
            if lib is None:
                raise InterceptionError(_LIB.error())
            ctx = lib.interception_create_context()
            # 驱动没装时 create_context 可能返回 NULL，也可能返回一个
            # send 全失败的 context —— 两种都等第一次 send 才暴露。
            if not ctx:
                raise InterceptionError(
                    "Interception 驱动不可用：先以管理员运行 "
                    "install-interception.exe install 并重启")
            self._ctx = ctx
            return self._ctx

    def close(self):
        with self._lock:
            if self._ctx is not None:
                lib = _LIB.load()
                if lib is not None:
                    lib.interception_destroy_context(self._ctx)
                self._ctx = None

    def send_mouse(self, state, x=0, y=0, rolling=0, flags=MOUSE_MOVE_RELATIVE):
        with self._lock:
            ctx = self._open()
            lib = _LIB.load()
            stroke = MouseStroke(state=state, flags=flags, rolling=rolling,
                                 x=x, y=y, information=0)
            n = lib.interception_send(ctx, MOUSE(0), ctypes.byref(stroke), 1)
            if n != 1:
                raise InterceptionError(
                    "Interception 驱动没有接收点击（send 返回 0）。"
                    "常见原因：装完驱动没重启；或驱动被卸载/被杀软清掉。"
                    "重跑 install-interception.exe install 后重启即可。")

    def send_key(self, scan, up=False, e0=False):
        with self._lock:
            ctx = self._open()
            lib = _LIB.load()
            state = (KEY_UP if up else KEY_DOWN) | (KEY_E0 if e0 else 0)
            stroke = KeyStroke(code=scan, state=state, information=0)
            # KeyStroke 8 字节，但 API 要求 20 字节 stroke —— 官方样例传
            # InterceptionStroke（20B char 数组）装它。这里照做：先填
            # MouseStroke 缓冲的前 8 字节。
            buf = MouseStroke()
            ctypes.memmove(ctypes.byref(buf), ctypes.byref(stroke),
                           ctypes.sizeof(stroke))
            n = lib.interception_send(ctx, KEYBOARD(0), ctypes.byref(buf), 1)
            if n != 1:
                raise InterceptionError("Interception 驱动没有接收按键（send 返回 0）")


_SHARED = Interception()


def shared():
    """进程级共享 context。"""
    return _SHARED


def driver_ready():
    """驱动是否已装并可用（能 create_context 且 send 通）。

    **不发测试事件**：create_context 成功 + DLL 能加载就算就绪。
    真正的「send 返回 0」到点的时候 engine 会报出明确的修复指引。
    """
    try:
        _SHARED._open()
        return True, ""
    except InterceptionError as e:
        return False, str(e)
