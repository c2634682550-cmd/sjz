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
        """挑要抓的那块屏。

        **优先主显示器**（用 GetSystemMetrics 问系统要它的尺寸），
        而不是死按下标取。

        为什么不能按下标取：拔过显示器、改过分辨率之后，mss 的显示器
        列表里会残留一块「幽灵屏」—— 设备已经不在了，但系统还没把它的
        条目清掉。按下标取就可能抓到它，而它**不再刷新**：抓回来的是
        一张冻结的旧画面，每一帧都一样。

        真机实测（2026-10-23）：抓到一块 1345×960 的幽灵屏，截图内容和
        9 个小时前一模一样（md5 相同）。引擎据此把画面里的「跳过」当成
        成交弹窗，凭空记了一笔「成交」—— 记录里那些没有商品名、耗时
        上万秒的假成交就是这么来的。
        """
        if want == 0:
            return 0                      # 虚拟屏：拼上所有显示器
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
        if 0 <= want < len(self.monitors):
            return want
        return 1 if len(self.monitors) > 1 else 0

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
