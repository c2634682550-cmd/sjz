"""空闲漫游（feature:mouse.idle）。

倒计时等待期间，在游戏客户区内做少量「只移动 / 移动+滚轮」的拟人动作；
进入开点窗口前按 return_s 归位，全程不产生任何点击。

设计约束（与 _feature_contract.md 对齐）：
- 配置只读 mouse.idle.*，字段模式/类型/范围由 switches.py 的开关表负责。
- 只在倒计时路线生效；拿不到 zero_at / 客户区 / 前台时不漫游。
- 移动一律走 Mouse.move_path（拟人轨迹共用入口），不自己直接发事件。
- 本模块没有点击调用，也不持有点击能力。
"""

from __future__ import annotations

import ctypes
import math
import random
import threading
import time
from collections import deque

from . import switches

# engine.FIRE_LEAD_S 的镜像：开点窗口从倒计时剩 5s 开始。两边必须一致。
FIRE_LEAD_S = 5.0
INSET_PX = 8.0


def settings(cfg):
    """当前 mouse.idle.* 的有效值（脏值/缺失由 switches.get 兜底）。"""
    return {
        "mode": switches.get(cfg, "mouse.idle.mode"),
        "interval_ms": switches.get(cfg, "mouse.idle.interval_ms"),
        "return_s": switches.get(cfg, "mouse.idle.return_s"),
        "min_remain_s": switches.get(cfg, "mouse.idle.min_remain_s"),
        "wheel_notches": switches.get(cfg, "mouse.idle.wheel_notches"),
    }


def gate(cfg, workflow):
    """该路线是否允许漫游：返回 (allowed, reason)。"""
    s = settings(cfg)
    if s["mode"] == "off":
        return False, "空闲漫游已关闭"
    if workflow != "countdown":
        return False, "该路线没有可反推的解锁时刻，空闲漫游不启用"
    return True, ""


def _screen_origin(screen):
    b = getattr(screen, "bounds", None)
    if isinstance(b, dict):
        try:
            return float(b.get("left", 0)), float(b.get("top", 0))
        except (TypeError, ValueError):
            return 0.0, 0.0
    return 0.0, 0.0


def client_rect(hwnd):
    """窗口客户区的屏幕绝对坐标 (x0, y0, x1, y1)；读不到返回 None。"""
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        r = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(r)):
            return None
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        if r.right <= 0 or r.bottom <= 0:
            return None
        return (float(pt.x), float(pt.y),
                float(pt.x + r.right), float(pt.y + r.bottom))
    except Exception:
        return None


def cursor_pos():
    """当前光标的屏幕绝对坐标；读不到返回 None。"""
    try:
        from ctypes import wintypes
        pt = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return None
        return float(pt.x), float(pt.y)
    except Exception:
        return None


def _inside(pt, rect, inset=0.0):
    x, y = pt
    x0, y0, x1, y1 = rect
    return (x0 + inset <= x <= x1 - inset and
            y0 + inset <= y <= y1 - inset)


def _clamp(pt, rect, inset=INSET_PX):
    x, y = pt
    x0, y0, x1, y1 = rect
    x = min(max(float(x), x0 + inset), x1 - inset)
    y = min(max(float(y), y0 + inset), y1 - inset)
    return x, y


def _bezier_path(start, end, rng, curve=0.35):
    """内置拟人曲线：三次贝塞尔 + 轻微抖动，全部点限制在起点/终点附近。"""
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    bend = rng.uniform(-1.0, 1.0) * curve * length
    c1 = (sx + dx * 0.3 + nx * bend, sy + dy * 0.3 + ny * bend)
    c2 = (sx + dx * 0.7 + nx * bend * 0.6, sy + dy * 0.7 + ny * bend * 0.6)
    steps = rng.randint(10, 16)
    pts = []
    for i in range(1, steps + 1):
        t = i / float(steps)
        e = t * t * (3.0 - 2.0 * t)  # smoothstep，起停更自然
        u = 1.0 - e
        x = (u * u * u * sx + 3 * u * u * e * c1[0] +
             3 * u * e * e * c2[0] + e * e * e * ex)
        y = (u * u * u * sy + 3 * u * u * e * c1[1] +
             3 * u * e * e * c2[1] + e * e * e * ey)
        if i < steps:
            x += rng.uniform(-0.8, 0.8)
            y += rng.uniform(-0.8, 0.8)
        pts.append((x, y))
    pts.append((ex, ey))
    total_ms = max(60.0, min(260.0, length * 0.9 + rng.uniform(40, 80)))
    return pts, total_ms


def empty_stats():
    """没有引擎实例时给 /api/scheduler 的稳定形状。"""
    return {
        "mode": "off", "workflow": None, "effective": "off", "phase": "off",
        "moves": 0, "wheel": 0, "clicks": 0, "settled": False,
        "settled_at": None, "settled_left_s": None, "last_left_s": None,
        "events": [], "trace": [], "path": [], "home": None,
        "client": None, "reason": "空闲漫游未启动",
    }


class RoamSession:
    """一次开点尝试的漫游会话；Engine 持有，_watch_countdown 每帧 tick。"""

    def __init__(self, cfg, screen=None, mouse=None, hwnd=None,
                 workflow="countdown", input_mode="interception", traj=None,
                 say=None, rect=None, cursor_pos_fn=None, wheel_fn=None,
                 rng=None, sleep=time.sleep):
        self._cfg = cfg
        self._screen = screen
        self._mouse = mouse
        self._hwnd = hwnd
        self._workflow = workflow
        self._input_mode = input_mode
        self._traj = traj
        self._say = say
        self._rect_in = tuple(rect) if rect is not None else None
        self._cursor_in = tuple(cursor_pos_fn) if cursor_pos_fn is not None else None
        self._wheel_fn = wheel_fn
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._ox, self._oy = _screen_origin(screen)

        self._lock = threading.Lock()
        self._events = deque(maxlen=80)
        self._trace = deque(maxlen=200)
        self._phase = "waiting"
        self._effective = "off"
        self._reason = ""
        self._moves = 0
        self._wheel = 0
        self._settled = False
        self._settled_at = None
        self._settled_left = None
        self._last_left = None
        self._last_move_t = -1e9
        self._exited = False
        self._geom_warned = False
        self._wheel_warned = False
        self._rect = None
        self._home = None
        self._pos = None
        self._path = []
        self._kb = None

    # ---------------------------------------------------------------- lifecycle
    def begin_attempt(self):
        with self._lock:
            self._events.clear()
            self._trace.clear()
            self._phase = "waiting"
            self._reason = ""
            self._moves = 0
            self._wheel = 0
            self._settled = False
            self._settled_at = None
            self._settled_left = None
            self._last_left = None
            self._last_move_t = -1e9
            self._exited = False
            self._geom_warned = False
            self._wheel_warned = False
            self._rect = None
            self._home = None
            self._pos = None
            self._path = []
            self._effective = "off"

    def startup_notice(self):
        s = settings(self._cfg)
        if s["mode"] == "off":
            return None
        allowed, reason = gate(self._cfg, self._workflow)
        if not allowed:
            return reason
        if self._input_mode == "message":
            return "空闲漫游：纯消息通道无真实光标移动，漫游与滚轮都不生效"
        if s["mode"] == "move_wheel":
            return ("空闲漫游：已开启（移动+滚轮），开点前 %ds 归位，全程零点击"
                    % s["return_s"])
        return ("空闲漫游：已开启（只移动），开点前 %ds 归位，全程零点击"
                % s["return_s"])

    def _event(self, kind, **kw):
        item = {"type": kind, "at": round(time.time(), 3)}
        item.update(kw)
        self._events.append(item)

    def _warn(self, text):
        if callable(self._say):
            try:
                self._say(text, "warn")
            except Exception:
                pass

    # ---------------------------------------------------------------- geometry
    def _read_rect(self):
        if self._rect_in is not None:
            return self._rect_in
        if not self._hwnd:
            return None
        raw = client_rect(self._hwnd)
        if raw is None:
            return None
        return (raw[0] - self._ox, raw[1] - self._oy,
                raw[2] - self._ox, raw[3] - self._oy)

    def _read_cursor(self):
        if self._cursor_in is not None:
            return self._cursor_in
        raw = cursor_pos()
        if raw is None:
            return None
        return raw[0] - self._ox, raw[1] - self._oy

    def _ensure_geom(self):
        if self._rect is not None:
            return self._rect
        rect = self._read_rect()
        if rect is None or rect[2] - rect[0] <= INSET_PX * 2 or rect[3] - rect[1] <= INSET_PX * 2:
            self._effective = "no_client_area"
            self._phase = "no_client_area"
            self._reason = "读不到游戏客户区，本轮不漫游"
            if not self._geom_warned:
                self._geom_warned = True
                self._event("no_client_area", reason=self._reason)
                self._warn("空闲漫游：" + self._reason)
            return None
        self._rect = rect
        # 归位点：漫游开始前的光标点（在客户区内），否则客户区中心。
        cur = self._read_cursor()
        if cur is not None and _inside(cur, rect, INSET_PX):
            home = _clamp(cur, rect)
        else:
            home = ((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0)
        self._home = home
        self._pos = home
        return rect

    def _foreground_ok(self):
        fg = getattr(self._mouse, "foreground", None)
        if not callable(fg) or not self._hwnd:
            return True
        try:
            return fg() == self._hwnd
        except Exception:
            return False

    # ---------------------------------------------------------------- path
    def _plan(self, target):
        traj = self._traj
        if traj is not None and getattr(traj, "active", False):
            try:
                got = traj.path_for(target[0], target[1], start=self._pos)
                if got:
                    points, total_ms = got
                    return list(points), float(total_ms)
            except Exception:
                pass
        return _bezier_path(self._pos, target, self._rng)

    def _emit(self, target, mode, notches):
        points, total_ms = self._plan(target)
        rect = self._rect
        points = [_clamp((float(p[0]), float(p[1]),), rect) for p in points]
        if len(points) < 2:
            return False
        mover = getattr(self._mouse, "move_path", None)
        ok = False
        if callable(mover):
            try:
                ok = bool(mover(points, total_ms))
            except Exception:
                ok = False
        if not ok:
            self._event("move_failed", target=[round(target[0], 1), round(target[1], 1)])
            self._reason = "移动通道不可用，本轮不漫游"
            return False
        self._pos = points[-1]
        self._moves += 1
        if self._moves == 1 and callable(self._say):
            try:
                self._say("空闲漫游：开始在客户区内轻移（零点击），"
                          "开点前 %ds 归位" % settings(self._cfg)["return_s"],
                          "info")
            except Exception:
                pass
        self._path = [[round(p[0], 1), round(p[1], 1)] for p in points]
        inside = all(_inside(p, rect, 0.0) for p in points)
        self._trace.append({
            "type": "move", "target": [round(target[0], 1), round(target[1], 1)],
            "points": len(points), "inside": inside,
        })
        self._event("move", target=self._trace[-1]["target"],
                    points=len(points), inside=inside, left_s=self._last_left)
        if mode == "move_wheel":
            self._do_wheel(target, notches)
        return True

    def _do_wheel(self, target, notches):
        if self._input_mode == "message":
            self._effective = "message_no_wheel"
            if not self._wheel_warned:
                self._wheel_warned = True
                self._reason = "纯消息通道滚轮无效（游戏按真实光标判定），只移动不滚轮"
                self._event("wheel_skipped", reason=self._reason)
            return
        fn = self._wheel_fn
        if fn is None:
            fn = self._wheel_default
        try:
            fn(target[0], target[1], int(notches))
        except Exception:
            self._event("wheel_failed", target=[target[0], target[1]])
            return
        self._wheel += 1
        self._event("wheel", notches=int(notches),
                    target=[round(target[0], 1), round(target[1], 1)])

    def _wheel_default(self, x, y, notches):
        if self._kb is None:
            from .input import Keyboard
            self._kb = Keyboard(self._hwnd, mode=self._input_mode)
        return self._kb.wheel(x + self._ox, y + self._oy, notches)

    # ---------------------------------------------------------------- stop
    def _settle(self, now, left):
        rect = self._ensure_geom()
        if rect is None:
            self._settled = True
            self._settled_at = time.time()
            self._settled_left = left
            return
        if not self._foreground_ok():
            self._phase = "settle_pending"
            self._reason = "前台丢失，暂不能归位"
            return
        home = self._home
        dist = math.hypot(self._pos[0] - home[0], self._pos[1] - home[1]) if self._pos else 0.0
        moved = 0
        if dist > 2.0:
            points, total_ms = self._plan(home)
            points = [_clamp((float(p[0]), float(p[1])), rect) for p in points]
            mover = getattr(self._mouse, "move_path", None)
            ok = False
            if callable(mover) and len(points) >= 2:
                try:
                    ok = bool(mover(points, total_ms))
                except Exception:
                    ok = False
            if not ok:
                # 移动通道没把光标带回去：停止漫游，但如实记「未归位」。
                self._settled = True
                self._settled_at = time.time()
                self._settled_left = left
                self._phase = "settled"
                self._reason = "移动通道不可用，未能归位（已停止漫游）"
                self._event("settle_failed", left_s=left,
                            home=[round(home[0], 1), round(home[1], 1)])
                return
            moved = len(points)
            self._pos = points[-1]
        self._settled = True
        self._settled_at = time.time()
        self._settled_left = left
        self._phase = "settled"
        self._reason = "已归位，开点窗口前不再移动"
        self._event("settle", left_s=left, home=[round(home[0], 1), round(home[1], 1)],
                    points=moved)
        if callable(self._say):
            try:
                self._say("空闲漫游：已归位并停止移动（剩余 %.1fs，零点击）"
                          % left, "info")
            except Exception:
                pass

    # ---------------------------------------------------------------- tick
    def tick(self, now, clock=None, left=None):
        """引擎每帧调用。返回本帧是否产生了漫游移动（便于测试）。"""
        with self._lock:
            s = settings(self._cfg)
            mode = s["mode"]
            if mode == "off":
                self._phase = "off"
                self._effective = "off"
                return False
            if self._workflow != "countdown":
                self._phase = "inactive_route"
                self._effective = "inactive_route"
                self._reason = "该路线没有可反推的解锁时刻，空闲漫游不启用"
                return False
            if left is None and clock is not None:
                za = getattr(clock, "zero_at", None)
                if callable(za):
                    try:
                        za = za()
                    except Exception:
                        za = None
                if za is not None:
                    try:
                        left = max(0.0, float(za) - float(now))
                    except (TypeError, ValueError):
                        left = None
            if left is None:
                self._phase = "no_clock"
                self._effective = "no_clock"
                self._reason = "倒计时尚未反推出解锁时刻"
                return False
            self._last_left = float(left)
            stop_at = FIRE_LEAD_S + float(s["return_s"])
            start_at = max(float(s["min_remain_s"]), stop_at + 1.0)
            if stop_at > start_at:  # 配置被手改成倒挂时也不漫游
                self._phase = "config_invalid"
                self._reason = "return_s 与 min_remain_s 倒挂，漫游窗口为空"
                return False
            if left <= stop_at:
                if not self._settled:
                    self._settle(now, left)
                return False
            if self._settled or self._exited:
                return False
            if left <= start_at:
                self._phase = "waiting"
                self._effective = ("message_no_wheel" if mode == "move_wheel"
                                   and self._input_mode == "message" else mode)
                self._reason = "等待进入漫游窗口"
                return False
            if not self._foreground_ok():
                self._exited = True
                self._phase = "foreground_lost"
                self._reason = "前台丢失，退出漫游"
                return False
            interval = max(0.0, float(s["interval_ms"]) / 1000.0)
            if now - self._last_move_t < interval:
                self._phase = "roaming"
                return False
            rect = self._ensure_geom()
            if rect is None:
                return False
            pad = INSET_PX
            target = (self._rng.uniform(rect[0] + pad, rect[2] - pad),
                      self._rng.uniform(rect[1] + pad, rect[3] - pad))
            self._last_move_t = now
            self._phase = "roaming"
            self._effective = ("message_no_wheel" if mode == "move_wheel"
                               and self._input_mode == "message" else mode)
            self._reason = ""
            if self._input_mode == "message":
                self._effective = ("message_no_wheel" if mode == "move_wheel"
                                   else "message_no_move")
                self._reason = ("纯消息通道滚轮无效、也不会移动真实光标，"
                                "本模式不漫游")
                if not self._wheel_warned:
                    self._wheel_warned = True
                    self._event("move_skipped", reason=self._reason)
                return False
            return self._emit(target, mode, s["wheel_notches"])

    # ---------------------------------------------------------------- stats
    def snapshot(self):
        with self._lock:
            return {
                "mode": settings(self._cfg)["mode"],
                "workflow": self._workflow,
                "effective": self._effective,
                "phase": self._phase,
                "moves": self._moves,
                "wheel": self._wheel,
                "clicks": 0,
                "settled": bool(self._settled),
                "settled_at": self._settled_at,
                "settled_left_s": self._settled_left,
                "last_left_s": self._last_left,
                "events": list(self._events)[-20:],
                "trace": list(self._trace)[-40:],
                "path": list(self._path),
                "home": ([round(self._home[0], 1), round(self._home[1], 1)]
                         if self._home else None),
                "client": ([round(v, 1) for v in self._rect] if self._rect else None),
                "reason": self._reason,
            }
