"""dry-run 工装：把「本该注入的点击」记下来，一个发送函数都不碰。

用途：先验证识别/决策链路，再单独验证点击通道。这里替换掉 `Mouse` 之后，
`Engine` 的正常代码路径照旧走（扫描、OCR、判定、坐标计算全部真实），
只有最后 `mouse.click()` 这一下变成「记录 + 返回成功」。

设计约束：
  · 不构造真 `Mouse`，因此连 interception 驱动都不会装载；
  · 不调用 `SetCursorPos` / `SendInput` / `PostMessage`，系统光标零位移；
  · `ensure_foreground()` 直接返回 True —— dry-run 不抢焦点；
  · 默认不启用，只有 `DAFORS_DRY_RUN=1` 或显式 `dry_run=True` 才生效。
"""
import ctypes
import time
from ctypes import wintypes

NONE = "none"

_EMPTY_LOCK = None


def _get_cursor():
    """只读当前系统光标位置（不动它）。"""
    try:
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except (AttributeError, OSError):
        pass
    return None


def point_report(screen, x, y, bbox=None):
    """判断一个落点是否在客户区矩形 / 按钮 bbox 内。

    `screen` 可以是 `core.screen.Screen`，也可以是任何带 `base_rect` 的对象；
    没传时两项都返回 None（不知道就不猜）。
    """
    base = getattr(screen, "base_rect", None)
    in_client = None
    client = None
    if base:
        client = [base["left"], base["top"],
                  base["left"] + base["width"], base["top"] + base["height"]]
        in_client = (client[0] <= x < client[2] and client[1] <= y < client[3])
    in_bbox = None
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        in_bbox = (x0 <= x <= x1 and y0 <= y <= y1)
    return {
        "x": int(x), "y": int(y),
        "client_rect": client,
        "in_client": in_client,
        "bbox": list(bbox) if bbox is not None else None,
        "in_bbox": in_bbox,
    }


class DryRunMouse:
    """`Mouse` 的只读替身：接口够 Engine/Navigator 用，但永不注入。"""

    mode = "dry-run"
    channel_label = "dry-run（只记录落点，不注入）"
    driver_warning = None
    tail_reserve_s = 0.0
    _fire_hint = False

    def __init__(self, screen=None, sink=None):
        self.screen = screen
        self.events = []            # 每条记录 = 一次「本该注入」的动作
        self.injections = 0         # 恒为 0；断言零注入用
        self.sink = sink            # 外部回调：Engine 接进来写日志/事件
        self._attached = None

    # ---------------------------------------------------------- 注入接口

    def click(self, x, y, expect_foreground=None, hold=None, verify=True,
              **_kw):
        self._note("click", x, y, hold=hold)
        return True

    def wheel(self, x, y, notches):
        self._note("wheel", x, y, notches=notches)
        return True

    def move(self, x, y, **_kw):
        self._note("move", x, y)
        return True

    def ensure_foreground(self, hwnd=None, tries=0, gap=0.0, screen=None):
        # 不抢焦点：dry-run 期间前台状态由用户/其它会话决定。
        return True

    def preheat(self, *_a, **_k):
        return None

    def attach_trajectory(self, traj):
        self._attached = traj

    def channel_status(self):
        return {"mode": self.mode, "clicks": len(self.events),
                "injections": self.injections, "last_click": None}

    @staticmethod
    def cursor():
        return _get_cursor()

    @staticmethod
    def foreground():
        try:
            return int(ctypes.windll.user32.GetForegroundWindow())
        except (AttributeError, OSError):
            return 0

    # ---------------------------------------------------------- 记录

    def _note(self, kind, x, y, **extra):
        event = {"t": round(time.time(), 3), "kind": kind,
                 "x": int(x), "y": int(y)}
        event.update(extra)
        event.update(point_report(self.screen, x, y))
        self.events.append(event)
        if len(self.events) > 20000:          # 影子长跑也别把内存吃满
            del self.events[:-10000]
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception:                 # noqa: BLE001 记录不能反过来炸引擎
                pass
        return True


def dry_enabled(value=None):
    """是否启用 dry-run。显式参数 > 环境变量 > 默认关闭。"""
    if value is not None:
        return bool(value)
    import os
    return os.getenv("DAFORS_DRY_RUN", "") == "1"
