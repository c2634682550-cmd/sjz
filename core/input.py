"""鼠标输入。

直接调 SendInput，绕开 pyautogui：后者每次 click() 后有 0.1s 强制 PAUSE，
实测 9.6 次/秒；这里 0.69ms/次，1447 次/秒。
"""
import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32

_MOVE = 0x0001
_LEFTDOWN = 0x0002
_LEFTUP = 0x0004
_ABSOLUTE = 0x8000

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
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class Mouse:
    def __init__(self, screen):
        self._origin_left = screen.bounds["left"]
        self._origin_top = screen.bounds["top"]
        self._vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        self._vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        self._vw = max(1, _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) - 1)
        self._vh = max(1, _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) - 1)

    def _send(self, flags, dx=0, dy=0):
        inp = _INPUT(type=0)
        inp.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, None)
        _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _abs(self, x, y):
        """把本屏像素坐标换算成虚拟屏 0-65535 绝对坐标。"""
        px = self._origin_left + x - self._vx
        py = self._origin_top + y - self._vy
        return (
            int(round(px * 65535 / self._vw)),
            int(round(py * 65535 / self._vh)),
        )

    def move(self, x, y):
        dx, dy = self._abs(x, y)
        self._send(_MOVE | _ABSOLUTE, dx, dy)

    def click(self, x, y, verify=True, expect_foreground=None):
        """移动 + 按下 + 抬起。

        三个事件都带坐标，防止中间被别的程序移走光标。verify=True 时在抬起后
        回读光标位置：如果不在目标上，说明有别的程序（或用户的手）在抢鼠标，
        这一下点击落到别处去了 —— 必须报错，不能静默继续点。

        expect_foreground: 期望接收这一下点击的窗口句柄。Windows 把点击投递给
        前台窗口，目标不在前台时点击静默丢失（SendInput 照成功、光标照移动、
        目标窗口收不到任何事件），所以在点击前显式检查。传 None 则跳过检查。
        """
        if expect_foreground:
            fg = self.foreground()
            if fg != expect_foreground:
                raise WindowNotForeground(
                    f"目标窗口 {expect_foreground} 不在前台（当前前台 {fg}）。"
                    f"点击会被系统投递给前台窗口，目标收不到。")
        dx, dy = self._abs(x, y)
        self._send(_MOVE | _ABSOLUTE, dx, dy)
        self._send(_LEFTDOWN | _ABSOLUTE, dx, dy)
        self._send(_LEFTUP | _ABSOLUTE, dx, dy)
        if verify:
            cx, cy = self.cursor()
            if abs(cx - (self._origin_left + x)) > 6 or abs(cy - (self._origin_top + y)) > 6:
                raise MouseStolen(
                    f"光标没停在目标上（想在 {x},{y}，实际 {cx},{cy}）——"
                    f"有别的程序或人在动鼠标")

    @staticmethod
    def foreground():
        """当前前台窗口句柄。"""
        return _user32.GetForegroundWindow()

    @staticmethod
    def cursor():
        p = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y
