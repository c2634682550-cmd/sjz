"""鼠标输入。

直接调 SendInput，绕开 pyautogui：后者每次 click() 后有 0.1s 强制 PAUSE，
实测 9.6 次/秒；这里 0.69ms/次，1447 次/秒。
"""
import ctypes
import time
from ctypes import wintypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_MOVE = 0x0001
_LEFTDOWN = 0x0002
_LEFTUP = 0x0004
_ABSOLUTE = 0x8000

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_MK_LBUTTON = 0x0001

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


class MouseStolen(Exception):
    """有别的程序或用户在动鼠标，导致点击没落到目标上。"""


class WindowNotForeground(Exception):
    """目标窗口不在前台，点击会被系统投递给别的窗口。

    实测：目标窗口非前台时 SendInput 不报错、光标也停在目标坐标，
    但目标窗口收不到任何点击事件 —— 静默失败，必须显式检出。
    """


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        # 必须是 ULONG_PTR（指针大小），不能是 POINTER(c_ulong)：
        # 后者在 64 位下是 8 字节指针但 INPUT 的 union 需要按 8 字节对齐，
        # 实测用 POINTER 会让 sizeof(INPUT)=28 而不是 40，SendInput 直接返回 0
        # 并报 ERROR_INVALID_PARAMETER(87) —— 点击全部静默失效。
        ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
         else ctypes.c_ulong),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class Mouse:
    def __init__(self, screen, game_hwnd=None, mode="interception"):
        self._origin_left = screen.bounds["left"]
        self._origin_top = screen.bounds["top"]
        self._game_hwnd = game_hwnd
        # interception = 驱动注入；legacy = SetCursorPos+PostMessage。
        # 驱动不可用（没装/被杀软清了）时自动退 legacy：宁可慢，不要停。
        self._mode = mode
        if mode == "interception":
            from .interception import driver_ready
            ok, why = driver_ready()
            if not ok:
                self._mode = "legacy"
                self._drv_warn = why
            else:
                self._drv_warn = None
        else:
            self._drv_warn = None
        self._vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        self._vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        self._vw = max(1, _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) - 1)
        self._vh = max(1, _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) - 1)

    def _send(self, flags, dx=0, dy=0):
        inp = _INPUT(type=0)
        inp.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, 0)
        return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _abs(self, x, y):
        """把本屏像素坐标换算成虚拟屏 0-65535 绝对坐标。"""
        px = self._origin_left + x - self._vx
        py = self._origin_top + y - self._vy
        return (
            int(round(px * 65535 / self._vw)),
            int(round(py * 65535 / self._vh)),
        )

    @property
    def mode(self):
        """实际生效的输入通道（引擎启动时读它写日志）。"""
        return self._mode

    @property
    def driver_warning(self):
        """驱动不可用时的原因（mode 退成 legacy 才非空）。"""
        return self._drv_warn

    def move(self, x, y):
        dx, dy = self._abs(x, y)
        self._send(_MOVE | _ABSOLUTE, dx, dy)

    def click(self, x, y, verify=False, expect_foreground=None):
        """移动 + 按下 + 抬起，走 Interception 驱动（2026-10-30 起）。

        为什么换掉「SetCursorPos + PostMessage」组合（旧做法，实测有效但慢）：
        驱动注入把事件从 kbdclass/mouclass 层送进输入栈，游戏读到的是
        **真实硬件事件**（无 LLMHF_INJECTED 标志，Raw Input 收得到），
        不需要动真实光标、也不依赖前台。每次 click 从约 0.13s 降到亚毫秒级。

        expect_foreground 仍然要查：驱动点击与前台无关是**对游戏而言**，
        但游戏收不到点击时的表现和旧路子一样是静默失败 —— 站错窗口照样
        白点。安全语义保留，速度语义换掉。

        verify 默认改 False：光标已经不跟着点击走了，回读光标位置判
        MouseStolen 的前提不成立。防抢保护由 engine 的 failsafe
        （鼠标甩左上角）承担，那套不依赖光标位置。

        失败模式（都显式报错，不静默）：
        - 驱动没装/没重启 → InterceptionError，信息里带修复步骤
        - send 返回 0 → InterceptionError（驱动装了但被卸了/杀软清了）
        """
        if expect_foreground:
            fg = self.foreground()
            if fg != expect_foreground:
                raise WindowNotForeground(
                    f"目标窗口 {expect_foreground} 不在前台（当前前台 {fg}）。"
                    f"点击会被系统投递给前台窗口，目标收不到。")

        if self._mode == "legacy":
            return self._click_messages(x, y, verify=verify)

        from .interception import (InterceptionError, MOUSE_LEFT_DOWN,
                                   MOUSE_LEFT_UP, MOUSE_MOVE_ABSOLUTE,
                                   MOUSE_VIRTUAL_DESKTOP, shared)
        dx, dy = self._abs(x, y)
        flags = MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP
        drv = shared()
        try:
            drv.send_mouse(0, x=dx, y=dy, flags=flags)       # 光标到位
            drv.send_mouse(MOUSE_LEFT_DOWN, x=dx, y=dy, flags=flags)
            drv.send_mouse(MOUSE_LEFT_UP, x=dx, y=dy, flags=flags)
        except InterceptionError as e:
            raise MouseStolen(f"Interception 驱动点击失败：{e}")

    def _click_messages(self, x, y, verify=True):
        """旧的 PostMessage 点击路径。留着：驱动不可用时的兜底。

        游戏必须用「SetCursorPos + PostMessage」组合，不能只发消息：
        它按下时读「当前真实光标位置」（docs/auto-buy-design.md §4.8）。
        """
        sx, sy = self._origin_left + x, self._origin_top + y
        _user32.SetCursorPos(sx, sy)
        time.sleep(0.04)

        hwnd = self._game_hwnd or self.foreground()
        pt = wintypes.POINT(sx, sy)
        _user32.ScreenToClient(hwnd, ctypes.byref(pt))
        lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
        _user32.PostMessageW(hwnd, _WM_MOUSEMOVE, 0, lp)
        time.sleep(0.02)
        _user32.PostMessageW(hwnd, _WM_LBUTTONDOWN, _MK_LBUTTON, lp)
        time.sleep(0.06)
        _user32.PostMessageW(hwnd, _WM_LBUTTONUP, 0, lp)

        if verify:
            time.sleep(0.05)
            cx, cy = self.cursor()
            if abs(cx - sx) > 6 or abs(cy - sy) > 6:
                raise MouseStolen(
                    f"光标没停在目标上（想在 {sx},{sy}，实际 {cx},{cy}）——"
                    f"有别的程序或人在动鼠标")

    @staticmethod
    def ensure_foreground(hwnd, tries=3, gap=0.25, screen=None):
        """把目标窗口抢回前台，成功返回 True。

        `SetForegroundWindow` 受 Windows 前台锁限制：调用方不是前台进程时
        直接失败（返回 0，窗口不动），提权也绕不过去。`AttachThreadInput`
        把自己的输入队列挂到当前前台线程上，再调就生效 —— 真机上只有这个
        做法稳定（纯 ShowWindow + BringWindowToTop 实测无效）。

        抢不回来不要硬点：`click(expect_foreground=)` 会拦下，点击不会投递
        到别的窗口去。

        screen：给了就在**真的切过窗口**之后等画面换过来再返回。合成器要
        一两帧才把游戏画面贴上来，这期间抓到的是上一个窗口的内容（实测把
        游戏标题读成了窗口标题）。本来就在前台就不等 —— 否则每次白等 1 秒。
        """
        if not hwnd:
            return False
        before = None
        if screen is not None:
            try:
                before = screen.grab()      # 切窗前的画面，用来判断「换过来了没」
            except Exception:               # noqa: BLE001
                before = None
        switched = False
        for _ in range(max(1, tries)):
            if _user32.GetForegroundWindow() == hwnd:
                break
            fg = _user32.GetForegroundWindow()
            tid_fg = _user32.GetWindowThreadProcessId(fg, None)
            tid_me = _kernel32.GetCurrentThreadId()
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(_user32.AttachThreadInput(tid_me, tid_fg, True))
            try:
                _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
                _user32.BringWindowToTop(hwnd)
                _user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    _user32.AttachThreadInput(tid_me, tid_fg, False)
            switched = True
            time.sleep(gap)
            if _user32.GetForegroundWindow() == hwnd:
                break
        else:
            return False
        if _user32.GetForegroundWindow() != hwnd:
            return False
        if switched and screen is not None:
            # 真切过窗口才等画面：合成器贴新画面要一两帧，这期间抓到的
            # 是上一个窗口的内容。本来就在前台就不等，省 1 秒。
            try:
                from .screen import wait_frame_change
                wait_frame_change(before, screen.grab)
            except Exception:                   # noqa: BLE001
                pass
        return True

    @staticmethod
    def foreground():
        """当前前台窗口句柄。"""
        return _user32.GetForegroundWindow()

    @staticmethod
    def cursor():
        p = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    @staticmethod
    def release_cursor_lock():
        """清掉光标裁剪锁。

        游戏独占全屏时会 ClipCursor 把光标钉在一个点上。这时 SendInput 和
        SetCursorPos **全部静默失效**：函数照返回成功，GetCursorPos 照返回坐标
        （但那是被锁住的位置，不是我们设的），结果就是「程序说点了，游戏没反应」。

        实测（`docs/auto-buy-design.md` §4.8）：清掉之后 SetCursorPos 立刻恢复。

        返回是否真的清掉了 —— 有些程序会在下一帧把锁重新设上，
        调用方可以据此判断要不要重复清。
        """
        before = wintypes.RECT()
        had = bool(_user32.GetClipCursor(ctypes.byref(before)))
        ok = bool(_user32.ClipCursor(None))
        return ok, had

    @staticmethod
    def cursor_is_locked():
        """光标是否被裁剪锁限制在一个很小的区域里。

        注意：不能只看 GetClipCursor 的返回值 —— 实测游戏会把裁剪区设成
        整个屏幕，光标却依然被钉住。真正的判据是「设一个位置、看它有没有动」。
        """
        p = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(p))
        x0, y0 = p.x, p.y
        probe = (x0 + 40, y0 + 40) if x0 < 1800 else (x0 - 40, y0 - 40)
        _user32.SetCursorPos(*probe)
        _user32.GetCursorPos(ctypes.byref(p))
        moved = (p.x, p.y) == probe
        _user32.SetCursorPos(x0, y0)        # 放回去，别弄乱用户的鼠标
        return not moved


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYINPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


# 扫描码表：游戏读的是硬件扫描码，不是虚拟键码
SCAN = {
    "ESC": 0x01, "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E,
    "F5": 0x3F, "F6": 0x40, "F7": 0x41, "F8": 0x42, "F9": 0x43,
    "F10": 0x44, "F11": 0x57, "F12": 0x58,
    "TAB": 0x0F, "ENTER": 0x1C, "SPACE": 0x39, "BACKSPACE": 0x0E,
    "UP": 0x48, "DOWN": 0x50, "LEFT": 0x4B, "RIGHT": 0x4D,
    "HOME": 0x47, "END": 0x4F, "PAGEUP": 0x49, "PAGEDOWN": 0x51,
    "INSERT": 0x52, "DELETE": 0x53,
    "A": 0x1E, "B": 0x30, "C": 0x2E, "D": 0x20, "E": 0x12, "F": 0x21,
    "G": 0x22, "H": 0x23, "I": 0x17, "J": 0x24, "K": 0x25, "L": 0x26,
    "M": 0x32, "N": 0x31, "O": 0x18, "P": 0x19, "Q": 0x10, "R": 0x13,
    "S": 0x1F, "T": 0x14, "U": 0x16, "V": 0x2F, "W": 0x11, "X": 0x2D,
    "Y": 0x15, "Z": 0x2C,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
}

# 虚拟键码：PostMessage 的 wParam 用这个（_click_messages 兜底路径仍在用）
VK = {
    "ESC": 0x1B, "TAB": 0x09, "ENTER": 0x0D, "SPACE": 0x20, "BACKSPACE": 0x08,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "INSERT": 0x2D, "DELETE": 0x2E,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
}

# 扩展键（键盘右侧小键盘区/方向键区）：Interception 驱动注入要带 KEY_E0
# 位，PostMessage 兜底路径要 bit24 扩展标志。表里没有的键都不是扩展键。
_EXTENDED = {
    "UP": True, "DOWN": True, "LEFT": True, "RIGHT": True,
    "HOME": True, "END": True, "PAGEUP": True, "PAGEDOWN": True,
    "INSERT": True, "DELETE": True,
}

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_WM_RBUTTONDOWN, _WM_RBUTTONUP = 0x0204, 0x0205
_WM_MOUSEWHEEL = 0x020A
MK_LBUTTON = 0x0001


class Keyboard:
    """给游戏窗口发按键，走 Interception 驱动（2026-10-30 起）。

    旧路子踩过两个坑，都实测过（docs/auto-buy-design.md §4.8）：

    1. **SendInput 对游戏无效。** 游戏读 Raw Input，只认真实硬件事件；
       SendInput 照返回成功、系统光标照动，游戏一点反应没有。
    2. **PostMessage 必须带扫描码**，wParam 用 VK —— 有效但只是
       「窗口消息」层面的有效，跟驱动注入比是两个层次。

    驱动注入直接从 kbdclass 层发**真实硬件事件**（扫描码原样送达），
    上面两个坑都不存在了：不需要窗口句柄、不依赖前台、没有注入标志。
    """

    def __init__(self, hwnd=None, mode="interception"):
        # hwnd 参数保留：调用方（engine/nav）都传，但驱动注入用不到它。
        self.hwnd = hwnd
        self._mode = mode

    def _stroke(self, scan, up, e0):
        from .interception import shared
        shared().send_key(scan, up=up, e0=e0)

    def _tap_messages(self, key, hold):
        """旧 PostMessage 路径（legacy 模式）。"""
        scan = SCAN.get(key.upper()) if isinstance(key, str) else key
        vk = VK.get(key.upper()) if isinstance(key, str) else scan
        ext = _EXTENDED.get(key.upper(), False) if isinstance(key, str) else False
        lp = self._lparam(scan, up=False, extended=ext)
        ok1 = bool(_user32.PostMessageW(self.hwnd, WM_KEYDOWN, vk, lp))
        time.sleep(hold)
        lp = self._lparam(scan, up=True, extended=ext)
        ok2 = bool(_user32.PostMessageW(self.hwnd, WM_KEYUP, vk, lp))
        return ok1 and ok2

    def _lparam(self, scan, up=False, extended=False):
        """WM_KEYDOWN/WM_KEYUP 的 lParam 位布局（legacy 路径用）。

        bits 0-15  重复次数 = 1
        bits 16-23 扫描码
        bit  24    扩展键标志
        bits 30-31 WM_KEYUP 时置 1（先前状态 / 转换状态）
        """
        v = 1 | (scan << 16)
        if extended:
            v |= 1 << 24
        if up:
            v |= (1 << 30) | (1 << 31)
        return v

    def tap(self, key, hold=0.05):
        """按一下。key 可以是 "F4" 或扫描码整数。

        hold 是按下到抬起之间的间隔。驱动注入即时生效，这个间隔
        只防游戏把过快的 down/up 当成抖动（保持旧节奏，不冒进）。
        """
        scan = SCAN.get(key.upper()) if isinstance(key, str) else key
        if scan is None:
            raise KeyError(f"不认识的键：{key}")
        if self._mode == "legacy":
            return self._tap_messages(key, hold)
        e0 = _EXTENDED.get(key.upper()) if isinstance(key, str) else False
        self._stroke(scan, up=False, e0=bool(e0))
        time.sleep(hold)
        self._stroke(scan, up=True, e0=bool(e0))
        return True

    def click(self, x, y, hold=0.06):
        """键盘类上的「点击」是旧接口遗留（nav 曾把鼠标点击放这里）。

        现在驱动注入后 Mouse 自己就能点，这个方法按**鼠标左键**处理 ——
        保持旧签名，避免调用方跟着改。位置参数 (x, y) 用绝对坐标。
        """
        from .input import Mouse  # 本模块内的自引用，避免循环导入
        m = getattr(self, "_mouse_for_click", None)
        if m is None:
            m = Mouse.__new__(Mouse)
            m._origin_left = m._origin_top = 0
            m._game_hwnd = self.hwnd
            m._vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            m._vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            m._vw = max(1, _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) - 1)
            m._vh = max(1, _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) - 1)
            self._mouse_for_click = m
        m.click(x, y)

    def wheel(self, x, y, notches):
        """在屏幕坐标 (x,y) 滚轮。正数向上，负数向下。

        旧实现必须 SetCursorPos + mouse_event —— 游戏无视消息式滚轮
        （实测典藏外观列表滚 12 屏纹丝不动）。驱动注入的滚轮是真实
        事件，**但仍要先动光标**：游戏按「当前光标位置」决定把滚动
        交给哪个区域。所以这里保留 SetCursorPos，滚动本身走驱动。
        """
        _user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        step = 120
        if self._mode == "legacy":
            for _ in range(abs(int(notches))):
                delta = step if int(notches) > 0 else -step
                _user32.mouse_event(0x0800, 0, 0, delta, 0)  # MOUSEEVENTF_WHEEL
                time.sleep(0.12)
            time.sleep(0.2)
            return True
        from .interception import MOUSE_WHEEL, shared
        for _ in range(abs(int(notches))):
            delta = step if int(notches) > 0 else -step
            # rolling 是有符号 i16：120 = 向上一格
            shared().send_mouse(MOUSE_WHEEL, rolling=delta)
            time.sleep(0.12)
        time.sleep(0.2)
        return True


# ---------------- 完整性级别（UIPI） ----------------
#
# Windows 的 UIPI：低完整性进程**不能**向高完整性窗口发输入消息
# （WM_KEYDOWN / WM_LBUTTONDOWN 这类低于 WM_USER 的消息一律被拦），
# SendInput 也一样。被拦时**没有任何报错** —— 表现就是「点击、F4 全都没
# 反应」，看起来像代码坏了。
#
# 真机实测（2026-10-23）：游戏以管理员（High）运行、控制台以普通权限
# （Medium）运行 —— 点击和键盘全部无效；把控制台提权后，同一套代码
# 立刻全部生效（点交易行 54%、F4 进市场 21%、价格框读回准确）。

INTEGRITY_CN = {0x1000: "低", 0x2000: "普通", 0x3000: "管理员", 0x4000: "系统"}


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def process_integrity(hwnd=None):
    """进程完整性级别（RID）。取不到返回 None。

    hwnd 给了就查那个窗口所属的进程，否则查当前进程。
    """
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    token_query = 0x0008
    token_integrity_level = 25

    # 必须显式声明参数类型：句柄是 64 位指针，不声明的话 ctypes 按 32 位
    # 整数传，OpenProcessToken 会拿着截断的句柄去查 —— 静默失败，
    # 读出来是「权限未知」。
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]

    if hwnd:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return None
    else:
        h = kernel32.GetCurrentProcess()

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(h, token_query, ctypes.byref(token)):
        return None
    try:
        n = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_integrity_level,
                                    None, 0, ctypes.byref(n))
        if not n.value:
            return None
        buf = ctypes.create_string_buffer(n.value)
        advapi32.GetTokenInformation(token, token_integrity_level,
                                    buf, n.value, ctypes.byref(n))
        label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
        advapi32.ConvertSidToStringSidW.restype = ctypes.c_void_p
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                     ctypes.POINTER(ctypes.c_void_p)]
        p = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(label.Label.Sid, ctypes.byref(p)):
            return None
        try:
            rid = int(ctypes.wstring_at(p).split("-")[-1])
        finally:
            kernel32.LocalFree(p)
        return rid
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(token)


def integrity_name(rid):
    """完整性级别的中文名。"""
    if rid is None:
        return "未知"
    return INTEGRITY_CN.get(rid, hex(rid))


def foreground_integrity():
    """前台窗口所属进程的完整性级别。"""
    return process_integrity(_user32.GetForegroundWindow())


def find_game_window():
    """找出游戏的窗口句柄。

    为什么不能直接用前台窗口：前台可能是控制台自己、浏览器、IDE ——
    拿它当「游戏」会把点击发到错的窗口上（实测定位功能一直报
    「点标签没切过去」）。按**窗口类**找：Unreal 的渲染窗口类名是
    `UnrealWindow`，别的程序不会有这个名字。
    """
    from ctypes import wintypes

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _):
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(h, buf, 256)
        if buf.value == "UnrealWindow" and _user32.IsWindowVisible(h):
            found.append(h)
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        return None
    return found[0] if found else None


def visible_max_integrity(exclude_pid=None):
    """所有**可见**顶层窗口里，最高的进程完整性级别。

    为什么不用前台窗口：前台可能是控制台自己、IDE、浏览器 —— 游戏窗口
    这时被盖着，前台查不到它的权限，权限检查就漏了。而游戏窗口一直
    是个可见的顶层窗口，扫一遍就抓得到。

    自己的进程跳过（它当然跟自己一样高）。
    """
    if exclude_pid is None:
        exclude_pid = _kernel32.GetCurrentProcessId()
    best = None
    handles = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _):
        handles.append(h)
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        return None

    for h in handles:
        if not _user32.IsWindowVisible(h):
            continue
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == exclude_pid:
            continue
        rid = process_integrity(h)
        if rid is not None and (best is None or rid > best):
            best = rid
    return best



def needs_elevation():
    """有可见窗口比我们权限高吗？高的话我们点不动它（比如管理员的游戏）。"""
    try:
        ours = process_integrity()
        game = visible_max_integrity()
    except Exception:
        return False
    return ours is not None and game is not None and game > ours


def elevate_self():
    """用管理员身份把自己重开一遍。返回 True = 已经重开，调用方应退出。

    为什么必须有这一步：UIPI 规定低完整性进程不能向高完整性窗口发输入
    消息（鼠标、按键全被拦），而且**一声不吭** —— 症状是「点击没反应、
    F4 没反应」，看起来像代码坏了。

    真机实测（2026-10-23）：游戏以管理员（High）运行、控制台以普通权限
    （Medium）运行时，PostMessage / SendInput 四种注入方式全部无效；
    把控制台提权后，**同一套代码**立刻全部生效（点交易行 54%、F4 进市场
    21%、价格框读回准确）。
    """
    import os
    import subprocess
    import sys

    if not needs_elevation():
        return False

    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        exe = sys.executable
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    try:
        # ShellExecuteW 的 "runas" 会弹 UAC；返回 > 32 表示成功启动
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params,
                                                 None, 1)
    except Exception:
        return False
    return rc > 32



