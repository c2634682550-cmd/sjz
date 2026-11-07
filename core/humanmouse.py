"""拟人鼠标轨迹（命名空间 `mouse.human.*`）——只规划，不碰真实光标。

分工
    这里只回答「从 A 走到 B 该怎么走」以及「这条路在当前通道下到底算不算数」；
    真正发事件（驱动注入 / SetCursorPos）由 `input.Mouse.move_path` 做。
    好处：离线可测（纯计算），也避免轨迹逻辑和通道逻辑缠在一起。

配置（契约 `_feature_contract.md` §2.1，只有三个键）
    mouse.human.traj     auto / windmouse / bezier / pack
    mouse.human.move_ms  20~800，整次移动的总时长
    mouse.human.curve    0~1，贝塞尔控制点偏移幅度（0 = 直线）

降级（全部显式，绝不假装生效）
    · 通道是 message：没有真实光标可动 → effective="degraded"
    · 通道降级到 legacy：仍能动真实光标，但每次点击变慢 → effective="legacy"
    · pack 但 data/traj 空/坏 → 按 auto 走，effective="pack_fallback_auto"
    · 盘上配置脏（越界/类型错）：不静默改值，problems 原样带上 → "invalid_config"

抖动（手抖）没有做成配置项：契约只给了三个键，这里按 curve 派生一个小幅
随机偏移（≤3px），末端收敛到 0，保证落点仍然精确。
"""

from __future__ import annotations

import glob
import json
import math
import os
import random

from .config import DATA_DIR

TRAJ_TYPES = ("auto", "windmouse", "bezier", "pack")
DEFAULT_MOVE_MS = 120
DEFAULT_CURVE = 0.35
MOVE_MS_LO, MOVE_MS_HI = 20, 800
CURVE_LO, CURVE_HI = 0.0, 1.0

PACK_DIR = os.path.join(DATA_DIR, "traj")
PACK_MIN_POINTS = 8
FIRE_CAP_MS = 30.0          # 开火期路径耗时硬上限（契约 §2.1）
FIRE_INTERVAL_SHARE = 0.25  # 且不超过点击间隔的 1/4

# 开火零位移：提前量窗口内先把光标走到按钮（预热），开火只做 down/up。
# 预热结果的有效期；超时或光标被用户挪走就按「瞬时直线落点」兜底。
PREHEAT_MAX_AGE_S = 10.0
TAIL_JUMP_BUDGET_MS = 1.0   # 目标晚到时允许的尾部微移上界（只做瞬时落点）

# 分段预热（提前量不足以走完整条曲线时）：预热点停在按钮前，开火前再用
# 一小段 8~20ms 的短距离移动补齐。目标是别在开火瞬间出现「几百像素瞬移」
# 这种物理鼠标做不到的特征，同时按下时刻不变。
TAIL_MIN_MS = 8.0           # 短移最短时长（再短就不像手了）
TAIL_MAX_MS = 20.0          # 短移最长时长（超过这个数就挤占点击间隔）
TAIL_MAX_PX = 120.0         # 短移能覆盖的最大剩余位移，超过就认输走瞬时落点
TAIL_SPEED_PX_S = 6000.0    # 短移速度上限（约 6px/ms）
SEGMENT_LEG_MIN_MS = 30.0   # 预热段本身至少 30ms，否则一样算瞬移
SEGMENT_RESERVE_MS = 20.0   # 每次开火预留的尾部窗口（从等待时间里扣）

# 加速段 / 减速段：契约没有给这两个配置键，按固定比例实现（非匀速直线）
ACCEL_SHARE = 0.28
DECEL_SHARE = 0.42
V_FLOOR = 0.12


def _dig(cfg, path):
    """Config 或普通 dict 都收，按点路径取值。"""
    cur = getattr(cfg, "data", None)
    if not isinstance(cur, dict):
        cur = cfg if isinstance(cfg, dict) else None
    if cur is None:
        return None
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def read_spec(cfg=None):
    """读三项配置并校验；脏值记进 problems（带上可读原因），不静默改值。"""
    problems = []

    traj = _dig(cfg, "mouse.human.traj")
    if traj is None:
        traj = "auto"
    elif not isinstance(traj, str) or traj not in TRAJ_TYPES:
        problems.append("mouse.human.traj 不在可选值 "
                        f"[{', '.join(TRAJ_TYPES)}]：{traj!r}")
        traj = "auto"

    move_ms = _dig(cfg, "mouse.human.move_ms")
    if move_ms is None:
        move_ms = DEFAULT_MOVE_MS
    elif not _is_int(move_ms):
        problems.append("mouse.human.move_ms 必须是整数：%r" % (move_ms,))
        move_ms = DEFAULT_MOVE_MS
    elif not (MOVE_MS_LO <= move_ms <= MOVE_MS_HI):
        problems.append("mouse.human.move_ms %d 越界（%d~%d）"
                        % (move_ms, MOVE_MS_LO, MOVE_MS_HI))
        move_ms = DEFAULT_MOVE_MS

    curve = _dig(cfg, "mouse.human.curve")
    if curve is None:
        curve = DEFAULT_CURVE
    else:
        try:
            curve = float(curve)
        except (TypeError, ValueError):
            problems.append("mouse.human.curve 不是数字：%r" % (curve,))
            curve = DEFAULT_CURVE
        else:
            if not math.isfinite(curve) or not (CURVE_LO <= curve <= CURVE_HI):
                problems.append("mouse.human.curve %r 越界（%g~%g）"
                                % (curve, CURVE_LO, CURVE_HI))
                curve = DEFAULT_CURVE

    return {"traj": traj, "move_ms": move_ms, "curve": float(curve),
            "problems": problems}


# ---------------------------------------------------------------- 轨迹包
def load_packs(directory=None):
    """读真人轨迹包。返回 (packs, warnings)；坏包整包忽略，不半用。"""
    d = directory or PACK_DIR
    packs, warns = [], []
    if not os.path.isdir(d):
        warns.append(f"轨迹包目录不存在：{d}")
        return packs, warns
    for path in sorted(glob.glob(os.path.join(d, "*.json"))):
        name = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:                               # noqa: BLE001
            warns.append(f"{name} 读不了：{type(e).__name__}: {e}")
            continue
        pts = raw.get("points") if isinstance(raw, dict) else raw
        why = _pack_problem(pts)
        if why:
            warns.append(f"{name} 格式不合格（{why}），整包忽略")
            continue
        packs.append({"name": raw.get("name") or os.path.splitext(name)[0],
                      "points": [(float(p[0]), float(p[1])) for p in pts],
                      "file": name})
    if not packs:
        warns.append("没有合格的轨迹包（至少 8 个点、首点 [0,0,0]、dt 递增且总时长 > 0）")
    return packs, warns


def _pack_problem(pts):
    if not isinstance(pts, list) or len(pts) < PACK_MIN_POINTS:
        return "点数不足 %d" % PACK_MIN_POINTS
    last_t = None
    for p in pts:
        if not isinstance(p, (list, tuple)) or len(p) < 3:
            return "点必须是 [x, y, dt]"
        try:
            x, y, t = float(p[0]), float(p[1]), float(p[2])
        except (TypeError, ValueError):
            return "点里有非数字"
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(t)):
            return "点里有 NaN/Infinity"
        if last_t is not None and not (t > last_t):
            return "dt 必须单调递增"
        last_t = t
    first = pts[0]
    if abs(float(first[0])) > 1e-9 or abs(float(first[1])) > 1e-9 \
            or abs(float(first[2])) > 1e-9:
        return "首点必须是 [0,0,0]"
    if last_t is None or last_t <= 0:
        return "总时长必须 > 0"
    return None


def _fit_pack(pack, sx, sy, tx, ty):
    """把轨迹包按实际起止点缩放 + 旋转（保持笔画形状，末点精确落到终点）。"""
    pts = pack["points"]
    ox, oy = pts[-1]
    src_len = math.hypot(ox, oy)
    dst_len = math.hypot(tx - sx, ty - sy)
    if src_len < 1e-9:
        return None
    k = dst_len / src_len
    ang = math.atan2(ty - sy, tx - sx) - math.atan2(oy, ox)
    ca, sa = math.cos(ang), math.sin(ang)
    out = []
    for (px, py) in pts:
        rx, ry = px * ca - py * sa, px * sa + py * ca
        out.append((sx + rx * k, sy + ry * k))
    out.append((float(tx), float(ty)))
    return out


# ---------------------------------------------------------------- 形状
def _windmouse(sx, sy, tx, ty, rng):
    """WindMouse：重力 + 风力 + 惯性，天然带加速—减速，不是直线匀速。"""
    sqrt3, sqrt5 = math.sqrt(3.0), math.sqrt(5.0)
    gravity, wind, max_step, target_area = 6.0, 4.0, 12.0, 12.0
    x, y = float(sx), float(sy)
    vx = vy = wx = wy = 0.0
    pts = [(x, y)]
    while len(pts) < 400:
        d = math.hypot(tx - x, ty - y)
        if d < 1.0:
            break
        w = min(wind, d)
        if d >= target_area:
            wx = wx / sqrt3 + (rng.random() * 2 * w - w) / sqrt5
            wy = wy / sqrt3 + (rng.random() * 2 * w - w) / sqrt5
        else:
            wx /= sqrt3
            wy /= sqrt3
            if max_step < 3:
                max_step = rng.random() * 3 + 3
            else:
                max_step /= sqrt5
        vx += wx + gravity * (tx - x) / d
        vy += wy + gravity * (ty - y) / d
        vm = math.hypot(vx, vy)
        if vm >= max_step and vm > 0:
            vx, vy = vx / vm * max_step, vy / vm * max_step
        x += vx
        y += vy
        pts.append((x, y))
    pts.append((float(tx), float(ty)))
    return pts


def _bezier(sx, sy, tx, ty, curve, rng):
    dx, dy = tx - sx, ty - sy
    length = max(1e-6, math.hypot(dx, dy))
    nx, ny = -dy / length, dx / length
    side = 1.0 if rng.random() < 0.5 else -1.0
    # 控制点法向偏移 = 距离 × curve × 0.25：curve=0.35 时约 9% 弧高，
    # 像手动甩过去；curve=1 才明显夸张。0 = 直线。
    amp = length * curve * 0.25 * rng.uniform(0.75, 1.0) * side
    c1 = (sx + dx * 0.30 + nx * amp * rng.uniform(0.6, 1.0)
          + rng.uniform(-0.02, 0.02) * length * curve,
          sy + dy * 0.30 + ny * amp * rng.uniform(0.6, 1.0)
          + rng.uniform(-0.02, 0.02) * length * curve)
    c2 = (sx + dx * 0.68 + nx * amp * rng.uniform(0.5, 0.9)
          + rng.uniform(-0.02, 0.02) * length * curve,
          sy + dy * 0.68 + ny * amp * rng.uniform(0.5, 0.9)
          + rng.uniform(-0.02, 0.02) * length * curve)
    out = []
    steps = 40
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = (mt ** 3) * sx + 3 * (mt ** 2) * t * c1[0] + 3 * mt * (t ** 2) * c2[0] + (t ** 3) * tx
        y = (mt ** 3) * sy + 3 * (mt ** 2) * t * c1[1] + 3 * mt * (t ** 2) * c2[1] + (t ** 3) * ty
        out.append((x, y))
    return out


def _cum_len(points):
    acc = [0.0]
    for i in range(1, len(points)):
        acc.append(acc[-1] + math.hypot(points[i][0] - points[i - 1][0],
                                        points[i][1] - points[i - 1][1]))
    return acc


def _sample_at(points, fractions):
    """按弧长比例取样（0~1），末点保持精确终点。"""
    if len(points) < 2:
        return [points[-1]] * max(1, len(fractions))
    acc = _cum_len(points)
    total = acc[-1]
    if total < 1e-9:
        return [points[-1]] * max(2, len(fractions))
    out, j = [], 0
    for frac in fractions:
        target = total * max(0.0, min(1.0, frac))
        while j < len(acc) - 2 and acc[j + 1] < target:
            j += 1
        span = acc[j + 1] - acc[j]
        r = 0.0 if span < 1e-12 else (target - acc[j]) / span
        out.append((points[j][0] + (points[j + 1][0] - points[j][0]) * r,
                    points[j][1] + (points[j + 1][1] - points[j][1]) * r))
    out[-1] = (float(points[-1][0]), float(points[-1][1]))
    return out


def _progress_table(accel=ACCEL_SHARE, decel=DECEL_SHARE, n=257):
    """速度形状 v(u)（梯形：加速—匀速—减速）→ 位置进度表。

    峰值归一化为 1，末速不为 0（手不会在终点前完全停住），所以不是匀速。
    """
    cruise = max(0.0, 1.0 - accel - decel)
    vs = []
    for i in range(n):
        u = i / (n - 1)
        if u < accel and accel > 0:
            v = V_FLOOR + (1.0 - V_FLOOR) * (u / accel)
        elif u <= accel + cruise:
            v = 1.0
        elif decel > 0:
            v = 1.0 - (1.0 - V_FLOOR) * ((u - accel - cruise) / decel)
        else:
            v = 1.0
        vs.append(max(V_FLOOR, v))
    total = 0.0
    cum = [0.0]
    for i in range(1, n):
        total += (vs[i] + vs[i - 1]) * 0.5 / (n - 1)
        cum.append(total)
    return [c / total for c in cum]


_PROGRESS = _progress_table()


def _progress(u):
    table = _PROGRESS
    pos = u * (len(table) - 1)
    i = min(len(table) - 2, int(pos))
    r = pos - i
    return table[i] + (table[i + 1] - table[i]) * r


def _jitter_offsets(count, curve, rng):
    """小幅手抖：随机游走 + 末端收敛，幅度 ≤3px，落点偏移为 0。

    curve=0 承诺「直线」，此时不抖；弯曲度越大越像手。
    """
    amp = 0.0 if curve <= 0 else min(3.0, 1.0 + curve)
    out, drift = [], 0.0
    for i in range(count):
        u = i / max(1, count - 1)
        drift = drift * 0.86 + rng.uniform(-amp, amp) * 0.35
        env = math.sin(math.pi * u) if 0 < u < 1 else 0.0
        out.append(drift * env)
    out[-1] = 0.0
    return out


# ---------------------------------------------------------------- 规划
def plan(start, end, spec, rng=None, fire=False, interval_ms=20.0, pack_dir=None):
    """生成一条轨迹。返回 dict；纯计算，不发任何输入事件。

    start / end 是屏幕坐标 (x, y)；spec 是 read_spec() 的结果。
    """
    rng = rng or random.Random()
    sx, sy = float(start[0]), float(start[1])
    tx, ty = float(end[0]), float(end[1])
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy)

    move_ms = float(spec["move_ms"])
    if fire:
        move_ms = min(move_ms, FIRE_INTERVAL_SHARE * interval_ms, FIRE_CAP_MS)
    move_ms = max(0.0, move_ms)

    traj = spec["traj"]
    notes = []
    used = traj

    if dist < 1.0:
        return {"points": [[sx, sy, 0.0], [tx, ty, move_ms]], "type": "direct",
                "ms": move_ms, "fire": bool(fire), "notes": ["距离不足 1px"]}

    pack = None
    if traj == "pack":
        packs, warns = load_packs(pack_dir)
        if packs:
            pack = rng.choice(packs)
            notes.append(f"轨迹包：{pack['name']}")
        else:
            used = "bezier"
            notes.append("轨迹包不可用，已降级为贝塞尔：" + "；".join(warns[:2]))
    elif traj == "auto":
        if dist >= 240.0:
            used = "windmouse"
        else:
            used = "bezier"

    if pack is not None:
        path = _fit_pack(pack, sx, sy, tx, ty) or _bezier(sx, sy, tx, ty, spec["curve"], rng)
        used = "pack"
    elif used == "windmouse":
        path = _windmouse(sx, sy, tx, ty, rng)
    else:
        used = "bezier"
        path = _bezier(sx, sy, tx, ty, spec["curve"], rng)

    count = 3 if fire else max(3, min(40, int(round(move_ms / 12.0))))
    # 时间等分（t_i = T·i/(n-1)），位置按速度曲线的进度取样 ——
    # 这样才有「起步加速 → 中段最快 → 收尾减速」，而不是匀速直线。
    fracs = [_progress(i / (count - 1)) for i in range(count)]
    path = _sample_at(path, fracs)

    # 手抖：沿法线方向加小幅偏移（末端为 0）
    if dist > 1e-6:
        nx, ny = -dy / dist, dx / dist
        offs = _jitter_offsets(count, spec["curve"], rng)
        path = [(p[0] + nx * o, p[1] + ny * o) for p, o in zip(path, offs)]
        path[-1] = (tx, ty)

    points = []
    for i, (px, py) in enumerate(path):
        u = i / (count - 1)
        points.append([px, py, move_ms * u])

    return {"points": points, "type": used, "ms": move_ms,
            "fire": bool(fire), "notes": notes}


# ---------------------------------------------------------------- 运行期
class Trajectory:
    """一个任务周期内的轨迹决策器（引擎只在 `_run` 里构造一次）。"""

    def __init__(self, cfg=None, mode="interception", pack_dir=None):
        self.spec = read_spec(cfg)
        self.mode = mode
        self.pack_dir = pack_dir
        self.moves = 0
        self.last = {}
        self.fire = []                  # 开火阶段实测样本（press_ms / gap_ms）
        self._last_at = None
        self.packs, self.pack_warns = load_packs(pack_dir)
        self.interval_ms = self._interval(cfg)
        self.effective, self.reason = self._resolve()

    @staticmethod
    def _interval(cfg):
        hz = _dig(cfg, "click_hz")
        try:
            hz = float(hz)
        except (TypeError, ValueError):
            hz = 50.0
        if not math.isfinite(hz) or hz <= 0:
            hz = 50.0
        return max(1.0, 1000.0 / hz)

    def _resolve(self):
        if self.spec["problems"]:
            return "invalid_config", "配置被拒收：" + "；".join(self.spec["problems"])
        if self.mode == "message":
            return ("degraded",
                    "纯消息通道无真实移动，拟人轨迹不生效")
        if self.mode not in ("interception", "legacy"):
            return "degraded", f"未知输入通道 {self.mode!r}，拟人轨迹不生效"
        if self.spec["traj"] == "pack" and not self.packs:
            return ("pack_fallback_auto",
                    "轨迹包不可用（" + "；".join(self.pack_warns[:2])
                    + "），已降级为自动策略")
        return self.mode, ""

    @property
    def active(self):
        return self.effective in ("interception", "legacy", "pack_fallback_auto")

    def path_for(self, x, y, start=None, now=None):
        """给一次点击算 path。返回 (points, total_ms) 或 None。"""
        if not self.active or start is None:
            return None
        now = now if now is not None else _clock()
        fire = (self._last_at is not None
                and (now - self._last_at) <= max(0.05, 2.5 * self.interval_ms / 1000.0))
        self._last_at = now
        try:
            tr = plan(start, (x, y), self.spec, fire=fire,
                      interval_ms=self.interval_ms, pack_dir=self.pack_dir)
        except Exception as e:                               # noqa: BLE001
            self.last = {"used": False, "reason": f"轨迹生成失败：{type(e).__name__}: {e}"}
            return None
        self.moves += 1
        self.last = {"used": True, "type": tr["type"], "points": len(tr["points"]),
                     "ms": round(tr["ms"], 2), "fire": tr["fire"],
                     "notes": list(tr["notes"])}
        return (tr["points"], tr["ms"])

    def prewarm_path(self, x, y, start=None, stop=None, ms=None):
        """提前量窗口内的预热移动：走完整 move_ms 曲线，不占开火时长。

        与 path_for 的区别只有一点：强制 fire=False，因此不会被 30ms 上限
        截断。预热结束后开火点击直接命中缓存，不再产生任何曲线位移。
        stop 给定时只走到该点（分段预热：停在按钮前，开火前再补一段短移）；
        ms 给定时用指定时长（预算不足时把预热段压进剩余提前量里）。
        返回 (points, total_ms) 或 None。
        """
        if not self.active or start is None:
            return None
        spec = self.spec
        if ms is not None:
            spec = dict(spec, move_ms=max(1.0, float(ms)))
        end = (x, y) if stop is None else stop
        try:
            tr = plan(start, end, spec, fire=False,
                      interval_ms=self.interval_ms, pack_dir=self.pack_dir)
        except Exception as e:                               # noqa: BLE001
            self.last = {"used": False, "preheat": True,
                         "reason": f"预热轨迹生成失败：{type(e).__name__}: {e}"}
            return None
        self.moves += 1
        self.last = {"used": True, "preheat": True, "type": tr["type"],
                     "points": len(tr["points"]), "ms": round(tr["ms"], 2),
                     "fire": False, "notes": list(tr["notes"])}
        if stop is not None:
            self.last["segment"] = True
        return (tr["points"], tr["ms"])

    def tail_path(self, x, y, start=None, ms=None):
        """分段预热的收尾短移：8~20ms、2~3 个点，把安全区到按钮的余量走完。

        它不是开火路径的时间预算的一部分：调用方把短移窗口从「等解锁」里
        提前扣掉，短移正好在按下时刻收尾。
        """
        if not self.active or start is None:
            return None
        try:
            tail_ms = float(ms if ms is not None else TAIL_MIN_MS)
        except (TypeError, ValueError):
            tail_ms = TAIL_MIN_MS
        tail_ms = max(TAIL_MIN_MS, min(TAIL_MAX_MS, tail_ms))
        spec = dict(self.spec, move_ms=tail_ms)
        try:
            tr = plan(start, (x, y), spec, fire=False,
                      interval_ms=self.interval_ms, pack_dir=self.pack_dir)
        except Exception as e:                               # noqa: BLE001
            self.last = {"used": False, "tail": True,
                         "reason": f"收尾短移生成失败：{type(e).__name__}: {e}"}
            return None
        self.moves += 1
        self.last = {"used": True, "tail": True, "type": tr["type"],
                     "points": len(tr["points"]), "ms": round(tr["ms"], 2),
                     "fire": False, "notes": list(tr["notes"])}
        return (tr["points"], tr["ms"])

    def record_fire(self, press_ms, gap_ms=None, preheated=False, jumped=False,
                    mode=None, first=False, lag_ms=None):
        """记录一次开火实测：按下→松开耗时、最后一次预热结束→按下的间隔。

        mode：zero=预热命中零位移，segment=收尾短移，straight=瞬时落点。
        first：是否开火阶段的第一次点击（卡点本身；后续连点单列）。
        lag_ms：实际按下时刻相对计划按下时刻的偏差（正=晚了）。
        """
        try:
            press = float(press_ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(press):
            return
        if mode is None:
            mode = "straight" if jumped else ("zero" if preheated else "straight")
        sample = {"press_ms": round(press, 3), "preheated": bool(preheated),
                  "jumped": bool(jumped), "mode": str(mode)[:12],
                  "first": bool(first)}
        if gap_ms is not None:
            try:
                gap = float(gap_ms)
            except (TypeError, ValueError):
                gap = None
            if gap is not None and math.isfinite(gap):
                sample["gap_ms"] = round(gap, 3)
        if lag_ms is not None:
            try:
                lag = float(lag_ms)
            except (TypeError, ValueError):
                lag = None
            if lag is not None and math.isfinite(lag):
                sample["lag_ms"] = round(lag, 3)
        self.fire.append(sample)
        if len(self.fire) > 200:
            del self.fire[:-200]

    @staticmethod
    def _fire_counts(rows):
        out = {"n": len(rows), "zero": 0, "segment": 0, "straight": 0}
        for s in rows:
            m = s.get("mode")
            if m in ("zero", "segment", "straight"):
                out[m] += 1
        total = out["n"]
        out["straight_rate"] = (round(out["straight"] / total, 4)
                                if total else None)
        out["segment_rate"] = (round(out["segment"] / total, 4)
                               if total else None)
        return out

    def fire_stats(self):
        """开火阶段分布：p50 / p95 / max + 降级计数。样本为空时全部为 None。

        降级计数分两档：first 是开火阶段第一次点击（卡点本身），retry 是
        之后等弹窗的连点。两档拆开才看得出「卡点有没有被轨迹拖累」。
        """
        def _dist(vals):
            vals = sorted(v for v in vals if v is not None)
            if not vals:
                return {"n": 0, "p50": None, "p95": None, "max": None}
            def _pick(p):
                idx = min(len(vals) - 1, max(0, int(math.ceil(p * len(vals)) - 1)))
                return round(vals[idx], 3)
            return {"n": len(vals), "p50": _pick(0.50),
                    "p95": _pick(0.95), "max": round(vals[-1], 3)}
        first = [s for s in self.fire if s.get("first")]
        retry = [s for s in self.fire if not s.get("first")]
        return {
            "n": len(self.fire),
            "press_ms": _dist([s.get("press_ms") for s in self.fire]),
            "gap_ms": _dist([s.get("gap_ms") for s in self.fire]),
            "press_lag_ms": _dist([s.get("lag_ms") for s in self.fire]),
            "last": dict(self.fire[-1]) if self.fire else {},
            "tail_jump_budget_ms": TAIL_JUMP_BUDGET_MS,
            "degrade": {
                "all": self._fire_counts(self.fire),
                "first": self._fire_counts(first),
                "retry": self._fire_counts(retry),
                "segment_reserve_ms": SEGMENT_RESERVE_MS,
                "tail_window_ms": [TAIL_MIN_MS, TAIL_MAX_MS],
                "tail_max_px": TAIL_MAX_PX,
            },
        }

    def status(self):
        return {
            "traj": self.spec["traj"],
            "move_ms": self.spec["move_ms"],
            "curve": self.spec["curve"],
            "effective": self.effective,
            "reason": self.reason,
            "active": self.active,
            "channel": self.mode,
            "moves": self.moves,
            "last": dict(self.last),
            "packs": {"count": len(self.packs),
                      "names": [p["name"] for p in self.packs],
                      "warnings": list(self.pack_warns[:3])},
            "problems": list(self.spec["problems"]),
            "fire": self.fire_stats(),
        }

    def banner(self):
        """启动时的一句话 + 日志级别（引擎直接 _say）。"""
        if self.effective == "degraded":
            return f"拟人轨迹：{self.reason}", "warn"
        if self.effective == "pack_fallback_auto":
            return f"拟人轨迹：{self.reason}", "warn"
        if self.effective == "invalid_config":
            return f"拟人轨迹：{self.reason}", "error"
        if self.effective == "legacy":
            return ("拟人轨迹：走 legacy 借光标，轨迹可生效但每次点击更慢"
                    f"（{self.spec['move_ms']}ms 上限；开火前预热，开火瞬间零位移）",
                    "info")
        return (f"拟人轨迹：{self.spec['traj']}（{self.spec['move_ms']}ms，"
                f"curve={self.spec['curve']:g}；开火前预热，开火瞬间零位移）", "info")


def _clock():
    import time
    return time.perf_counter()
