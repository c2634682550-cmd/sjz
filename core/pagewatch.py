"""界面 watcher：随时知道游戏停在哪个页面。

为什么要有：用户问「脚本知不知道我现在停在哪个界面」—— 要能答，就得一直
盯着，而不是只在启动前检查时查一次。

怎么盯得便宜：整屏 OCR 一次 0.5~1 秒，每秒都做会把一个核吃光。
所以先用 32x32 缩略图算**指纹**（几毫秒），**画面变了才去 OCR**：
静止时几乎不花钱，用户一动鼠标/切页面就立刻更新。

页面判定复用 core.nav 的分类器（hall / market / onsale / detail /
filter_onsale / …），和导航用的是同一套判据，不会出现「看的是一页、
认的是另一页」。
"""
import os
import threading
import time

import cv2
import numpy as np

# 控制台窗口标题里会出现这些标记（desktop.py 的 WINDOW_TITLE 前缀）。
# 前台窗口标题命中就认为「控制台挡在游戏前面」，不去认页面。
_CONSOLE_TITLES = ("SJZ 抢购控制台", "SJZ 控制台")


class PageWatcher:
    """后台盯着当前页面。start() 之后 snapshot() 随时能拿到。"""

    # 「画面变了多少才重新认页面」的门槛（32x32 缩略图的平均像素差）。
    #
    # 实测（2026-10-23 真机）：游戏停在同一页时，待机动画造成的差异是
    # **3.3~4.7**；翻页是几十。所以门槛取 8：待机动画不会触发重新认页
    # （整屏 OCR 0.5~1 秒，每 2 秒做一次会把一个核吃光），换页一定会触发。
    CHANGE_THRESHOLD = 8.0

    def __init__(self, monitor=1, interval=1.5, nav_factory=None):
        self.interval = interval
        # 用户选的那块屏（配置里的 monitor）。以前构造函数收了参数却
        # 从来不存，`_make_nav` 硬编码 `Screen(1)` —— 选 2 号屏的用户
        # 永远认的是 1 号屏（审查 S19）。
        self.monitor = monitor
        self._nav_factory = nav_factory    # 可注入，便于测试
        self._lock = threading.Lock()
        self._page = "unknown"
        self._text = ""
        self._at = 0.0
        self._sig = None
        self._stale = False
        self._stop = threading.Event()
        self._thread = None
        self._nav = None

    # ---------------- 对外 ----------------

    def snapshot(self):
        """当前页面名、识别时间，以及是不是「旧」的（控制台挡着游戏时）。"""
        with self._lock:
            return {"page": self._page, "at": self._at, "stale": self._stale}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._nav is not None:
                self._nav.screen.close()
        except Exception:
            pass

    # ---------------- 内部 ----------------

    def _make_nav(self):
        """第一次用时才建（建 mss 抓屏 + 导航器要几十毫秒）。"""
        if self._nav is not None:
            return self._nav
        if self._nav_factory is not None:
            self._nav = self._nav_factory()
            return self._nav
        from .input import Mouse
        from .nav import Navigator
        from .screen import Screen
        screen = Screen(self.monitor)
        self._nav = Navigator(screen, Mouse(screen), snap=screen.grab,
                              dry_run=False)
        return self._nav

    @staticmethod
    def _signature(img):
        """32x32 灰度缩略图 —— 画面变没变一眼就能看出来。"""
        if img is None or getattr(img, "size", 0) == 0:
            return None
        small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (32, 32),
                            interpolation=cv2.INTER_AREA)
        return small.astype("int16")

    def _refresh_once(self):
        """看一眼画面：变了才去 OCR。

        整屏 OCR 0.5~1 秒，画面静止时每轮都做会把一个核吃光。

        **自己挡着的时候不认**：控制台窗口压在游戏上时，抓整屏读到的是
        控制台自己的字（认出来的页面当然是「认不出」）。这时保留上一次
        的结果并标成「旧」，不去浪费一次 OCR 也不给出错的答案。
        """
        nav = self._make_nav()
        img = nav._snap()
        if self._own_window_in_front():
            with self._lock:
                self._stale = True
            return
        sig = self._signature(img)
        if sig is None:
            return
        if self._sig is not None and float(np.abs(sig - self._sig).mean()) <= self.CHANGE_THRESHOLD:
            return
        self._sig = sig
        page, text = nav.where()
        with self._lock:
            self._page = page
            self._text = text
            self._at = time.time()
            self._stale = False

    @staticmethod
    def _own_window_in_front():
        """前台窗口是不是「我们自己」（控制台窗口）。

        只比 PID 是不够的：桌面版的控制台可能是 WebView2 / 浏览器宿主 /
        打包后的 exe，**开的进程和我们并不相同**，PID 永远对不上，
        于是控制台压着游戏时照样去认页面，抓到的全是控制台自己的字（审查 S12）。
        所以：本进程的窗口算我们的；标题是这个控制台标题的窗口也算我们的。
        """
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            fg = u.GetForegroundWindow()
            if not fg:
                return False
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            if pid.value == os.getpid():
                return True
            # 控制台是独立宿主进程时按标题认（desktop.py 里那个窗口标题）
            u.GetWindowTextW.restype = ctypes.c_int
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(fg, buf, 256)
            title = buf.value or ""
            return any(mark in title for mark in _CONSOLE_TITLES)
        except Exception:
            return False

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._refresh_once()
            except Exception:
                pass
            self._stop.wait(self.interval)
