"""屏幕捕获。

mss 底层是 GDI BitBlt：抓 400x150 的 ROI 实测 1.9ms，全屏 9.3ms，
比 pyautogui.screenshot（13ms 且无法指定 bbox 复用）快且稳。

注意 mss 实例不是线程安全的，每个用屏的线程各建一个。
"""
import ctypes
import time

import mss
import numpy as np


def enable_dpi_awareness():
    """按物理像素工作。不设的话 Windows 缩放下坐标和截图会对不上。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def wait_frame_change(before, grab, timeout=1.2, interval=0.06, tol=1.0):
    """等画面**换掉**（相对 before 变了），返回等到的耗时秒数；超时返回 None。

    为什么需要：抢回前台之后，合成器还要一两帧才把游戏画面贴上来。这期间
    抓到的还是上一个窗口的内容 —— 实测把游戏标题读成了窗口标题「on.exe」。
    固定 sleep 0.6s 能用，但每次白等；这里改成「等它真的变了就走」，
    实测 100~200ms 就够。

    注意判据是「和切窗前的画面不一样」，不是「连续两帧一样」：游戏画面
    一直在动（模型在转、粒子在飘），要求两帧相同会永远等不到。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(interval)
        now = grab()
        if now is None or getattr(now, "size", 0) == 0:
            continue
        if before is None or now.shape != before.shape:
            return time.time() - t0
        diff = float(np.abs(now.astype("int16") - before.astype("int16")).mean())
        if diff > tol:
            return time.time() - t0
    return None


class Screen:
    def __init__(self, monitor=1):
        enable_dpi_awareness()
        self._sct = mss.mss()
        self.monitors = self._sct.monitors
        self.index = self._pick_monitor(monitor)
        self.bounds = self.monitors[self.index]

    def _pick_monitor(self, want):
        """挑要抓的那块屏。**传进来的屏号必须被尊重**。

        want=0 是虚拟屏（拼上所有显示器）。want=None 时才去猜「主显示器」
        （用 GetSystemMetrics 问系统要它的尺寸）—— 以前是**无条件**先按
        主屏尺寸匹配，配置里写 monitor=2 也会被抓回 1 号屏，用户以为在抓
        第二块屏，实际一直抓第一块（review V05）。

        want 明确但不存在（拔过显示器、改过分辨率后列表残留幽灵屏）就抛
        ValueError，让上层拿到明确失败；不再悄悄回落到别的屏。
        """
        if want == 0:
            return 0                      # 虚拟屏：拼上所有显示器
        if want is None or want == 1:
            # 1 是配置默认值，历史上当作「自动/主屏」。主屏用
            # GetSystemMetrics 问系统要尺寸再匹配：拔过显示器、改过分辨率
            # 后 mss 列表里会残留一块不再刷新的「幽灵屏」，按下标取可能
            # 抓到它（真机实测 1345x960 的冻结画面，引擎据此记了假成交）。
            try:
                primary = (ctypes.windll.user32.GetSystemMetrics(0),
                           ctypes.windll.user32.GetSystemMetrics(1))
            except Exception:
                primary = None
            if primary:
                for i, m in enumerate(self.monitors):
                    if i == 0:
                        continue
                    if (m["width"], m["height"]) == primary:
                        return i
            return 1 if len(self.monitors) > 1 else 0

        # 明确指定 2 号及以上的屏，就**用这块屏**：以前不管 want 写几，
        # 都先按主屏尺寸匹配，配置 monitor=2 也被抓回 1 号屏（review V05）。
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
        return want

    @property
    def size(self):
        return self.bounds["width"], self.bounds["height"]

    def grab(self, box=None):
        """box 为 (x, y, w, h) 像素坐标（相对本屏）；None 抓全屏。返回 BGR ndarray。"""
        if box is None:
            rect = self.bounds
        else:
            x, y, w, h = box
            rect = {
                "left": self.bounds["left"] + int(x),
                "top": self.bounds["top"] + int(y),
                "width": max(1, int(w)),
                "height": max(1, int(h)),
            }
        shot = self._sct.grab(rect)
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        # 前三通道正好是 BGR，省一次 cvtColor
        return arr[:, :, :3].copy()

    def grab_norm(self, box):
        """box 为归一化 {x,y,w,h}。"""
        return self.grab(self.norm_to_px(box))

    def norm_to_px(self, box):
        w, h = self.size
        return (
            int(box["x"] * w),
            int(box["y"] * h),
            max(1, int(box["w"] * w)),
            max(1, int(box["h"] * h)),
        )

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass
