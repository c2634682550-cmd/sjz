"""屏幕捕获与 DPI 感知。

mss 底层是 GDI BitBlt：抓 400x150 的 ROI 实测 1.9ms，全屏 9.3ms，
比 pyautogui.screenshot（13ms 且无法指定 bbox 复用）快且稳。

注意 mss 实例不是线程安全的，每个用屏的线程各建一个。

坐标系约定（审查 R09-04，全项目只在这里换算一次）
------------------------------------------------
· 进程必须是 **per-monitor DPI aware**（V2）。这样 Win32 返回的窗口矩形、
  显示器边界、GetSystemMetrics 全是**物理像素**，和 mss 抓出来的图一一对应。
· 归一化区域 → 物理像素：只由 `Screen.norm_to_px()` 负责（唯一入口）。
· 物理像素 → 鼠标绝对坐标 0..65535：只由 `core/input.Mouse` 负责。
· 其它模块（engine/nav/vision）一律只用物理像素，不再做任何 DPI 换算。
  之前 `desktop.py` 在 `pywebview.start()` 之前调 `SetProcessDPIAware()`
  把进程锁成 system-aware，混合 DPI（笔记本 150% + 外接 100%）下截图与
  pywebview 的坐标口径不一致 —— 现在统一改成 per-monitor V2，见
  `enable_dpi_awareness()`。
"""
import ctypes
import time
from ctypes import wintypes

import mss
import numpy as np

from . import vision

# ---------------------------------------------------------------- DPI 感知


_DPI_AWARENESS_NAMES = {
    0: "unaware",
    1: "system-aware",
    2: "per-monitor-aware",
}
_PER_MONITOR_AWARE_V2 = -4
_MONITOR_DEFAULT_TO_PRIMARY = 1
_MONITOR_DEFAULT_TO_NEAREST = 2
_MDT_EFFECTIVE_DPI = 0
_GAME_WINDOW_CLASS = "UnrealWindow"


def enable_dpi_awareness():
    """把进程设成 per-monitor DPI aware V2，返回实际生效的 awareness 值。

    顺序：SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2) →
    shcore.SetProcessDpiAwareness(2) → SetProcessDPIAware()。
    一旦进程已经设过（Manifest 或更早的代码），后面的调用会失败但不影响
    结果 —— 这里不覆盖旧状态，只读回真实值。
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            return dpi_awareness()
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return dpi_awareness()
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass
    return dpi_awareness()


def dpi_awareness():
    """读回进程 DPI 感知级别：0=unaware，1=system，2=per-monitor。

    per-monitor V2 在 GetProcessDpiAwareness 里同样报 2；它和 V1 的差别
    在窗口跨屏时的自动重绘，对抓屏/坐标换算而言两者都是物理像素口径。
    """
    try:
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        return int(value.value)
    except (AttributeError, OSError):
        try:
            return 1 if ctypes.windll.user32.IsProcessDPIAware() else 0
        except (AttributeError, OSError):
            return 0


def awareness_name(code):
    return _DPI_AWARENESS_NAMES.get(code, f"unknown({code})")


# ---------------------------------------------------------------- 游戏窗口


def _virtual_screen_rect():
    try:
        u = ctypes.windll.user32
        return (
            u.GetSystemMetrics(76),             # SM_XVIRTUALSCREEN
            u.GetSystemMetrics(77),             # SM_YVIRTUALSCREEN
            u.GetSystemMetrics(78),             # SM_CXVIRTUALSCREEN
            u.GetSystemMetrics(79),             # SM_CYVIRTUALSCREEN
        )
    except (AttributeError, OSError):
        return None


def _is_plausible_window_rect(r):
    """最小化窗口 GetWindowRect 会给出 -32000 之类的坐标，过滤掉。"""
    if r["width"] <= 0 or r["height"] <= 0:
        return False
    vr = _virtual_screen_rect()
    if not vr:
        return True
    vx, vy, vw, vh = vr
    # 至少和虚拟屏有一块交集，否则是隐藏/最小化窗口
    return (r["left"] < vx + vw and r["left"] + r["width"] > vx
            and r["top"] < vy + vh and r["top"] + r["height"] > vy)


def _game_windows(cls=_GAME_WINDOW_CLASS):
    """枚举可见的游戏顶层窗口，返回 [{hwnd,left,top,width,height}, ...]。"""
    found = []
    try:
        user32 = ctypes.windll.user32
        buf = ctypes.create_unicode_buffer(256)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetClassNameW(hwnd, buf, 256)
            if n and buf.value == cls:
                r = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    rect = {
                        "hwnd": int(hwnd),
                        "left": int(r.left), "top": int(r.top),
                        "width": int(r.right - r.left),
                        "height": int(r.bottom - r.top),
                    }
                    if _is_plausible_window_rect(rect):
                        found.append(rect)
            return True

        user32.EnumWindows(_enum, 0)
    except (AttributeError, OSError):
        return []
    return found


def find_game_window_rect(cls=_GAME_WINDOW_CLASS):
    """找游戏窗口的物理像素矩形；找不到返回 None。

    Unreal 引擎的顶层窗口类是 `UnrealWindow`。多个同名窗口取面积最大的
    那个；最小化/隐藏的窗口不会进入候选（否则会拿到 -32000 的无效矩形）。
    """
    found = _game_windows(cls)
    if not found:
        return None
    best = max(found, key=lambda r: r["width"] * r["height"])
    return {k: best[k] for k in ("left", "top", "width", "height")}


def find_game_client_rect(cls=_GAME_WINDOW_CLASS):
    """找游戏窗口的**客户区**矩形（物理像素，屏幕绝对坐标）。

    为什么要客户区而不是整屏/显示器：
      · 窗口化时窗口周围有标题栏和边框，游戏画面只画在客户区里；
      · 无边框窗口化和全屏时客户区≈窗口矩形，同一套换算无需分支；
      · 归一化坐标必须相对「游戏画面」换算，否则窗口一挪，
        所有区域就整体偏掉一个边框的距离。

    找不到窗口 / 取不到客户区时返回 None，调用方回落到显示器矩形。
    """
    found = _game_windows(cls)
    if not found:
        return None
    best = max(found, key=lambda r: r["width"] * r["height"])
    try:
        user32 = ctypes.windll.user32
        cr = wintypes.RECT()
        if not user32.GetClientRect(best["hwnd"], ctypes.byref(cr)):
            return None
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(best["hwnd"], ctypes.byref(pt)):
            return None
        frame = {
            "left": int(pt.x), "top": int(pt.y),
            "width": int(cr.right - cr.left), "height": int(cr.bottom - cr.top),
        }
    except (AttributeError, OSError):
        return None
    if not _is_plausible_window_rect(frame):
        return None
    return frame


def _intersection_area(a, b):
    x0 = max(a["left"], b["left"])
    y0 = max(a["top"], b["top"])
    x1 = min(a["left"] + a["width"], b["left"] + b["width"])
    y1 = min(a["top"] + a["height"], b["top"] + b["height"])
    if x1 <= x0 or y1 <= y0:
        return 0
    return (x1 - x0) * (y1 - y0)


def _center(b):
    return b["left"] + b["width"] / 2.0, b["top"] + b["height"] / 2.0


def _primary_monitor_rect():
    """主显示器的物理像素矩形（不是"最大的那块"）。"""

    class _MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    try:
        user32 = ctypes.windll.user32
        hmon = user32.MonitorFromWindow(user32.GetDesktopWindow(),
                                        _MONITOR_DEFAULT_TO_PRIMARY)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return {
            "left": int(r.left), "top": int(r.top),
            "width": int(r.right - r.left), "height": int(r.bottom - r.top),
        }
    except (AttributeError, OSError, IndexError):
        return None


def _dpi_for_monitor(bounds):
    """某块显示器的有效 DPI（MDT_EFFECTIVE_DPI）；失败返回 96。"""
    try:
        user32 = ctypes.windll.user32
        # 用矩形左上角向内 1px 的点，保证命中这块屏
        pt = wintypes.POINT(
            int(bounds["left"] + max(1, bounds["width"] // 2)),
            int(bounds["top"] + max(1, bounds["height"] // 2)),
        )
        hmon = user32.MonitorFromPoint(pt, _MONITOR_DEFAULT_TO_NEAREST)
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(
            hmon, _MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy))
        return int(dx.value) or 96
    except (AttributeError, OSError):
        return 96


def wait_frame_change(before, grab, timeout=1.2, interval=0.06, tol=1.0):
    """等画面**换掉**（相对 before 变了），返回等到的耗时秒数；超时返回 None。

    为什么需要：抢回前台之后，合成器还要一两帧才把游戏画面贴上来。这期间
    抓到的还是上一个窗口的内容 —— 实测把游戏标题读成了窗口标题「on.exe」。
    固定 sleep 0.6s 能用，但每次白等；这里改成「等它真的变了就走」，
    实测 100~200ms 就够。

    注意判据是「和切窗前的画面不一样」，不是「连续两帧一样」：游戏画面
    一直在动（模型在转、粒子在飘），要求两帧相同会永远等不到。
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        time.sleep(interval)
        now = grab()
        if now is None or getattr(now, "size", 0) == 0:
            continue
        if before is None or now.shape != before.shape:
            return time.monotonic() - t0
        diff = float(np.abs(now.astype("int16") - before.astype("int16")).mean())
        if diff > tol:
            return time.monotonic() - t0
    return None


class Screen:
    def __init__(self, monitor=1):
        enable_dpi_awareness()
        self.awareness = dpi_awareness()
        self._sct = mss.mss()
        self.monitors = self._sct.monitors
        self.game_rect = None
        self.pick_reason = ""
        try:
            self.index = self._pick_monitor(monitor)
        except Exception:
            # mss 构造时已经占了 1 个 DC + 1 个 Bitmap；显示器号非法或配置
            # 错误时构造失败，不关就会每次失败泄漏 2 个 GDI 对象（R02-L01）。
            try:
                self._sct.close()
            except Exception:                   # noqa: BLE001
                pass
            raise
        self.bounds = self.monitors[self.index]
        self.dpi = _dpi_for_monitor(self.bounds)
        # 游戏**客户区**（物理像素，屏幕绝对坐标）。归一化坐标、抓屏 ROI、
        # 鼠标落点都以它为基准；找不到窗口/取不到客户区时退回显示器矩形
        # （全屏和单屏场景下两者等价，行为与旧版一致）。
        self.frame = None
        if self.game_rect is None:
            self.game_rect = find_game_window_rect()
        client = find_game_client_rect()
        self.client_rect = client
        # 窗口跨屏/有一部分拖出屏幕：客户区不能整块抓到（越界部分是黑的），
        # 按显示器矩形归一化又会整体偏几十像素。这里只记录，由自动标定
        # 明确拒绝（见 autocalib.run_for_engine），不静默用错的基准。
        self.client_partial = bool(client and not self._frame_inside(client, self.bounds))
        if client and not self.client_partial:
            self.frame = client
        # 系统缩放（125% → 1.25）。只用于日志/诊断；模板尺度对齐用的是
        # 「当前屏尺寸 / 模板采集屏尺寸」，见 core/vision.py。
        self.scale = round(self.dpi / 96.0, 3)
        # 登记当前物理像素环境，模板匹配按它重采样
        vision.set_target_screen(self.size, self.dpi)

    @staticmethod
    def _frame_inside(frame, bounds, tol=2):
        """客户区必须基本落在选中的显示器里，否则退回显示器矩形。"""
        return (frame["left"] >= bounds["left"] - tol
                and frame["top"] >= bounds["top"] - tol
                and frame["left"] + frame["width"] <=
                bounds["left"] + bounds["width"] + tol
                and frame["top"] + frame["height"] <=
                bounds["top"] + bounds["height"] + tol)

    @property
    def base_rect(self):
        """当前实际使用的像素基准矩形：游戏客户区优先，否则显示器。"""
        return self.frame or self.bounds

    @property
    def origin(self):
        """像素坐标 (0,0) 在屏幕上的绝对位置（鼠标换算用）。"""
        base = self.base_rect
        return base["left"], base["top"]

    # ------------------------------------------------------------ 选屏

    def _pick_monitor(self, want):
        """挑要抓的那块屏。**传进来的屏号必须被尊重**。

        · want=0  → 虚拟屏（拼上所有显示器）。
        · want=None/1 → 自动：优先「游戏窗口实际所在的那块屏」。两块同
          分辨率的屏以前只能按尺寸匹配，永远抓回 1 号屏（审查 R09-03），
          现在按窗口矩形与各屏的交集面积选，同分辨率也能区分。
          找不到游戏窗口时退化为按主显示器的**位置+尺寸**匹配（而不是
          只按尺寸），仍能避开拔掉后残留的幽灵屏。
        · want>=2 → 显式指定，越界/无效抛 ValueError，不悄悄回落。
        """
        if want == 0:
            self.pick_reason = "虚拟屏（所有显示器）"
            return 0
        if want is None or want == 1 or want == "auto":
            self.game_rect = find_game_window_rect()
            if self.game_rect:
                idx = self._monitor_for_rect(self.game_rect)
                if idx is not None:
                    self.pick_reason = (
                        f"游戏窗口 {self.game_rect['width']}×{self.game_rect['height']}"
                        f" @({self.game_rect['left']},{self.game_rect['top']}) 所在显示器")
                    return idx
                self.pick_reason = "游戏窗口不在任何显示器内（被最小化？）"
            else:
                self.pick_reason = "未发现游戏窗口，按主显示器"
            idx = self._primary_index()
            self.pick_reason += f"（显示器 {idx}）"
            return idx

        try:
            want = int(want)
        except (TypeError, ValueError):
            raise ValueError(f"显示器编号必须是整数：{want!r}") from None
        if not (0 <= want < len(self.monitors)):
            raise ValueError(
                f"指定的显示器 {want} 不存在（本机可用 0..{len(self.monitors) - 1}）")
        m = self.monitors[want]
        if m.get("width", 0) <= 0 or m.get("height", 0) <= 0:
            raise ValueError(f"显示器 {want} 尺寸无效：{m}")
        self.pick_reason = f"显式指定显示器 {want}"
        return want

    def _monitor_for_rect(self, rect):
        """窗口矩形 → 交集面积最大的显示器下标；完全相同则比中心距离。"""
        best, best_key = None, None
        for i, m in enumerate(self.monitors):
            if i == 0:
                continue
            area = _intersection_area(rect, m)
            if area <= 0:
                continue
            # 交集面积为主键；同面积用「显示器中心到窗口中心」的距离做次键，
            # 保证窗口跨屏时也能选到真正承载它的那块。
            cx, cy = _center(m)
            wx, wy = _center(rect)
            dist = (cx - wx) ** 2 + (cy - wy) ** 2
            key = (area, -dist)
            if best_key is None or key > best_key:
                best, best_key = i, key
        return best

    def _primary_index(self):
        primary = _primary_monitor_rect()
        if primary:
            for i, m in enumerate(self.monitors):
                if i == 0:
                    continue
                if (m.get("left") == primary["left"]
                        and m.get("top") == primary["top"]
                        and m.get("width") == primary["width"]
                        and m.get("height") == primary["height"]):
                    return i
            for i, m in enumerate(self.monitors):
                if i == 0:
                    continue
                if (m.get("width"), m.get("height")) == (
                        primary["width"], primary["height"]):
                    return i
        # 最后一次兜底：选面积最大的真实屏（通常就是主屏）；单屏则 0。
        real = [(i, m.get("width", 0) * m.get("height", 0))
                for i, m in enumerate(self.monitors) if i > 0]
        if real:
            return max(real, key=lambda t: t[1])[0]
        return 0

    # ------------------------------------------------------------ 抓屏

    @property
    def size(self):
        base = self.base_rect
        return base["width"], base["height"]

    def grab(self, box=None):
        """box 为 (x, y, w, h) 物理像素坐标（相对**客户区**）；None 抓整个客户区。"""
        base = self.base_rect
        if box is None:
            rect = base
        else:
            x, y, w, h = box
            rect = {
                "left": base["left"] + int(x),
                "top": base["top"] + int(y),
                "width": max(1, int(w)),
                "height": max(1, int(h)),
            }
        shot = self._sct.grab(rect)
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        # 前三通道正好是 BGR，省一次 cvtColor
        return arr[:, :, :3].copy()

    def norm_to_px(self, box):
        """归一化区域 → 物理像素矩形（**全项目唯一**的归一化→像素换算点）。

        审查 R03-N12/N18：region 里的 NaN/Inf 以前会在这里变成 OverflowError
        或一堆无意义坐标，截出来的图是黑的，后面所有识别都失败却看不出原因。
        这里只做"必须是有限数"的兜底校验；0~1 与 x+w<=1 的完整校验属于配置
        入口（core/config.py），不在本文件权限内（见 _portability_fixes.md
        待接线）。
        像素统一四舍五入到整数（审查 R03-N18）；per-monitor aware 下
        `self.size` 是物理像素，这里不需要也不应该再乘缩放。
        """
        import math

        for k in ("x", "y", "w", "h"):
            try:
                v = float(box[k])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"区域缺少有效字段 {k!r}：{box!r}") from None
            if not math.isfinite(v):
                raise ValueError(f"区域字段 {k!r} 不是有限数：{v!r}")
        w, h = self.size
        return (
            int(round(box["x"] * w)),
            int(round(box["y"] * h)),
            max(1, int(round(box["w"] * w))),
            max(1, int(round(box["h"] * h))),
        )

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass
