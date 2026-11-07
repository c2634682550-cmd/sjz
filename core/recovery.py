"""原子恢复能力库 + 组合规划器 + 安全收尾。

设计原则：
  · 每个原子动作独立执行、独立判断成败、可重复执行；
  · 规划器给定「目标状态」，按代价从低到高组合动作，每步验证，失败换组合；
  · 用总时长 / 总动作数上限做保护，到点先安全收尾再停；
  · 能力库里没有任何「确认购买 / 充值 / 支付」动作，弹窗只允许取消或关闭。

本模块不直接碰鼠标键盘；所有真实操作通过 `performer(action_id, ctx)` 注入，
测试用假执行器，引擎用真实适配器。代码阶段默认是 `not_available`。
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import scene_guard as sg

# ---------------- 原子动作 ----------------

@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    cost: int
    group: str                  # read / page / window / time / system / safety
    min_level: int = 1
    max_level: int = 5
    scenes: tuple[str, ...] | None = None   # None = 所有场景
    safe: bool = True
    enabled: bool = True

    def for_scene(self, scene_id: str) -> bool:
        return self.scenes is None or scene_id in self.scenes


ACTIONS: dict[str, ActionSpec] = {}


def _reg(spec: ActionSpec) -> ActionSpec:
    ACTIONS[spec.id] = spec
    return spec


# 识别组 ------------------------------------------------------
_reg(ActionSpec("reread", "重新识别当前画面", 1, "read"))
_reg(ActionSpec("reread_scale", "换尺度重识别", 2, "read"))
_reg(ActionSpec("reread_crop", "换裁剪区域重识别", 2, "read"))
_reg(ActionSpec("alt_template", "多模板匹配", 2, "read"))
_reg(ActionSpec("alt_ocr", "换备用识别通道", 3, "read"))

# 页面组 ------------------------------------------------------
_reg(ActionSpec("esc", "返回上一级", 2, "page", min_level=1, max_level=3))
_reg(ActionSpec("cancel_confirm", "取消当前确认框", 2, "page", min_level=1, max_level=3,
                scenes=(sg.SC_CONFIRM_UNEXPECTED, sg.SC_INSUFFICIENT)))
_reg(ActionSpec("close_popup", "按弹窗类型关闭", 2, "page", min_level=1, max_level=3,
                scenes=(sg.SC_CONFIRM_UNEXPECTED, sg.SC_INSUFFICIENT, sg.SC_UNEXPECTED_PAGE)))
_reg(ActionSpec("back_button", "点界面返回按钮", 3, "page", min_level=2, max_level=3))
_reg(ActionSpec("scroll_top", "列表滚到顶部", 3, "page", min_level=2, max_level=3,
                scenes=(sg.SC_LIST_STALL, sg.SC_CLICK_NO_EFFECT)))
_reg(ActionSpec("scroll_bottom", "列表滚到底部", 3, "page", min_level=2, max_level=3,
                scenes=(sg.SC_LIST_STALL,)))
_reg(ActionSpec("refresh_list", "刷新列表", 4, "page", min_level=2, max_level=3,
                scenes=(sg.SC_LIST_STALL, sg.SC_CLICK_NO_EFFECT)))
_reg(ActionSpec("sort_switch", "换排序或换页", 4, "page", min_level=2, max_level=3,
                scenes=(sg.SC_LIST_STALL,)))
_reg(ActionSpec("tab_reenter", "切标签页重进", 5, "page", min_level=2, max_level=3,
                scenes=(sg.SC_UNEXPECTED_PAGE, sg.SC_UI_CHANGED,
                        sg.SC_RESOLUTION_CHANGED)))
_reg(ActionSpec("nav_home", "回交易行首页", 6, "page", min_level=3, max_level=3,
                scenes=(sg.SC_UNEXPECTED_PAGE, sg.SC_CONFIRM_UNEXPECTED,
                        sg.SC_CLICK_NO_EFFECT, sg.SC_LIST_STALL,
                        sg.SC_UI_CHANGED, sg.SC_RESOLUTION_CHANGED,
                        sg.SC_INSUFFICIENT, sg.SC_WINDOW_LOST)))
_reg(ActionSpec("nav_main_menu", "逐级退回主菜单", 7, "page", min_level=3, max_level=3,
                scenes=(sg.SC_UNEXPECTED_PAGE, sg.SC_UI_CHANGED)))

# 窗口组 ------------------------------------------------------
_reg(ActionSpec("ensure_foreground", "抢回窗口焦点", 5, "window", min_level=2, max_level=3,
                scenes=(sg.SC_WINDOW_LOST, sg.SC_GAME_DOWN, sg.SC_LAG,
                        sg.SC_UNEXPECTED_PAGE, sg.SC_UI_CHANGED)))
_reg(ActionSpec("restore_window", "最小化还原并前台", 8, "window", min_level=3, max_level=3,
                scenes=(sg.SC_WINDOW_LOST, sg.SC_GAME_DOWN)))
_reg(ActionSpec("rebind_window", "按进程重新绑定窗口", 8, "window", min_level=3, max_level=3,
                scenes=(sg.SC_WINDOW_LOST, sg.SC_GAME_DOWN)))
_reg(ActionSpec("reset_cursor", "还原光标位置", 3, "window", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("release_cursor_lock", "解除光标裁剪锁", 2, "window", min_level=1, max_level=3,
                scenes=(sg.SC_WINDOW_LOST, sg.SC_USER_TAKEOVER)))
_reg(ActionSpec("recalibrate", "重新标定坐标", 6, "window", min_level=2, max_level=3,
                scenes=(sg.SC_RESOLUTION_CHANGED, sg.SC_UI_CHANGED,
                        sg.SC_WINDOW_LOST)))

# 时间组 ------------------------------------------------------
_reg(ActionSpec("resync_clock", "重新对时", 3, "time", min_level=2, max_level=3,
                scenes=(sg.SC_CLOCK_DRIFT,)))
_reg(ActionSpec("wait_retry", "等待并重试", 2, "time", min_level=1, max_level=3,
                scenes=None))

# 系统组 ------------------------------------------------------
_reg(ActionSpec("restart_ocr", "重启识别服务", 6, "system", min_level=3, max_level=3,
                scenes=(sg.SC_OCR_FAIL, sg.SC_UI_CHANGED)))
_reg(ActionSpec("reconnect_input", "重新连接输入通道", 3, "system",
                min_level=1, max_level=2, scenes=(sg.SC_SELF_FAULT,)))
_reg(ActionSpec("restart_worker", "重启卡点子进程", 10, "system", min_level=2, max_level=3,
                scenes=(sg.SC_SELF_FAULT,)))
_reg(ActionSpec("restart_round", "重开一轮", 12, "system", min_level=3, max_level=3,
                scenes=None))
_reg(ActionSpec("restart_game", "重启游戏进程", 100, "system", min_level=3, max_level=3,
                scenes=(sg.SC_GAME_DOWN,), enabled=False))  # 代码阶段默认不启用
_reg(ActionSpec("reduce_load", "降低截图和预览负载", 2, "system", min_level=2, max_level=3,
                scenes=(sg.SC_LAG,)))
_reg(ActionSpec("reload_config", "重新读取设置文件", 3, "system", min_level=1, max_level=3,
                scenes=(sg.SC_CONFIG_FAULT,)))
_reg(ActionSpec("restore_config", "用上一份好设置恢复", 4, "system", min_level=2, max_level=3,
                scenes=(sg.SC_CONFIG_FAULT,)))

# 安全 / 收尾组 -----------------------------------------------
_reg(ActionSpec("freeze", "冻结点击", 0, "safety", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("release_input", "释放按下态", 1, "safety", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("record", "写事故记录", 0, "safety", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("unhook", "卸掉输入挂接", 0, "safety", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("reap", "收掉卡点子进程", 0, "safety", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("close_capture", "关闭截图资源", 0, "safety", min_level=1, max_level=5,
                scenes=None))
_reg(ActionSpec("manual_handoff", "停在人工操作准确位置", 0, "safety",
                min_level=4, max_level=4, scenes=None))
_reg(ActionSpec("halt_cleanup", "硬停清理", 0, "safety", min_level=5, max_level=5,
                scenes=None))

# 每个场景的「目标状态」由 scene_guard.goal_met 判定，规划器不重复定义。


def available_actions(scene_id: str, level: int,
                      library: "CapabilityLibrary | None" = None) -> list[ActionSpec]:
    out = []
    for spec in ACTIONS.values():
        if not spec.enabled:
            continue
        if spec.for_scene(scene_id) and spec.min_level <= level <= spec.max_level:
            if library is not None and not library.has(spec.id):
                continue
            out.append(spec)
    out.sort(key=lambda x: (x.cost, x.id))
    return out


@dataclass
class ActionResult:
    ok: bool
    changed: bool = False
    evidence: dict = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_mapping(cls, data: Any) -> "ActionResult":
        if isinstance(data, ActionResult):
            return data
        if isinstance(data, bool):
            return cls(ok=data)
        if isinstance(data, Mapping):
            return cls(ok=bool(data.get("ok")),
                       changed=bool(data.get("changed")),
                       evidence=dict(data.get("evidence") or {}),
                       error=str(data.get("error") or ""))
        return cls(ok=False, error="bad_result")


class CapabilityLibrary:
    """能力库：注册原子动作的真实实现；没注册的动作返回 not_available。"""

    def __init__(self, performer: Callable[[str, Mapping[str, Any]], Any] | None = None):
        self._impl: dict[str, Callable] = {}
        self._performer = performer

    def register(self, action_id: str, fn: Callable[[Mapping[str, Any]], Any]) -> None:
        if action_id not in ACTIONS:
            raise KeyError(action_id)
        self._impl[action_id] = fn

    def has(self, action_id: str) -> bool:
        return self._performer is not None or action_id in self._impl

    def run(self, action_id: str, ctx: Mapping[str, Any]) -> ActionResult:
        try:
            if self._performer is not None:
                return ActionResult.from_mapping(self._performer(action_id, ctx))
            fn = self._impl.get(action_id)
            if fn is None:
                return ActionResult(False, error="not_available")
            return ActionResult.from_mapping(fn(ctx))
        except Exception as e:                    # noqa: BLE001  单个动作不能把恢复带崩
            return ActionResult(False, error=f"{type(e).__name__}: {e}")

    def missing(self, scene_id: str, level: int) -> list[str]:
        return [s.id for s in available_actions(scene_id, level)
                if not self.has(s.id)]


@dataclass(frozen=True)
class Plan:
    actions: tuple[str, ...]
    cost: int

    def key(self) -> tuple[str, ...]:
        return tuple(sorted(self.actions))


class Planner:
    """按代价从低到高生成组合；去重、限深、限数量。"""

    def __init__(self, library: CapabilityLibrary,
                 max_depth: int = 3, max_plans: int = 8):
        self.library = library
        self.max_depth = max(1, int(max_depth))
        self.max_plans = max(1, int(max_plans))

    def plans(self, scene_id: str, level: int) -> list[Plan]:
        acts = available_actions(scene_id, level, self.library)
        if not acts:
            return []
        singles = [Plan((a.id,), a.cost) for a in acts]
        out: list[Plan] = []
        seen: set[tuple[str, ...]] = set()

        def push(plan: Plan) -> None:
            key = plan.key()
            if key in seen:
                return
            seen.add(key)
            out.append(plan)

        for p in singles:
            push(p)
        if self.max_depth >= 2:
            groups: dict[str, list[ActionSpec]] = {}
            for a in acts:
                groups.setdefault(a.group, []).append(a)
            gnames = sorted(groups)
            for i, g1 in enumerate(gnames):
                for g2 in gnames[i + 1:]:
                    for a1 in groups[g1][:6]:
                        for a2 in groups[g2][:6]:
                            push(Plan((a1.id, a2.id), a1.cost + a2.cost))
        if self.max_depth >= 3:
            # 只在两段组合足够少时才生成三段，避免组合爆炸
            struct = [a for a in acts if a.group in ("page", "window", "system")]
            for a1, a2, a3 in itertools.islice(
                    itertools.permutations(struct, 3), 0, 120):
                if len({a1.group, a2.group, a3.group}) < 3:
                    continue
                push(Plan((a1.id, a2.id, a3.id),
                          a1.cost + a2.cost + a3.cost))
        out.sort(key=lambda p: (p.cost, len(p.actions), p.actions))
        # 先保证每个动作组至少有一条候选，再按代价补满 —— 否则最便宜的一堆
        # 同类动作会把名额占光（例如只排「重识别」，永远排不到「取消弹窗」）。
        picked: list[Plan] = []
        used_groups: set[str] = set()
        picked_keys: set[tuple[str, ...]] = set()
        for p in out:
            g = ACTIONS[p.actions[0]].group
            if g in used_groups:
                continue
            used_groups.add(g)
            picked.append(p)
            picked_keys.add(p.key())
            if len(picked) >= self.max_plans:
                break
        for p in out:
            if len(picked) >= self.max_plans:
                break
            if p.key() in picked_keys:
                continue
            picked.append(p)
            picked_keys.add(p.key())
        return picked[:self.max_plans]


# ---------------- 安全收尾 ----------------

@dataclass
class ResourceAdapters:
    """真实还原入口；代码阶段默认全部未接，测试注入假实现。"""

    freeze: Callable[[], Any] | None = None
    release_input: Callable[[], Any] | None = None
    reset_cursor: Callable[[], Any] | None = None
    unhook: Callable[[], Any] | None = None
    reap: Callable[[], Any] | None = None
    close_capture: Callable[[], Any] | None = None
    record: Callable[[Mapping[str, Any]], Any] | None = None


class Cleanup:
    """幂等安全收尾：冻结点击 → 释放输入 → 还原光标 → 卸钩子 → 收子进程 → 关截图 → 写记录。"""

    ORDER = ("freeze", "release_input", "reset_cursor", "unhook",
             "reap", "close_capture", "record")

    def __init__(self, adapters: ResourceAdapters | None = None):
        self.adapters = adapters or ResourceAdapters()

    def run(self, context: Mapping[str, Any] | None = None) -> dict:
        done: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        for name in self.ORDER:
            fn = getattr(self.adapters, name, None)
            if fn is None:
                skipped.append(name)
                continue
            try:
                res = fn(context or {}) if name == "record" else fn()
                if res is None or res is True:
                    done.append(name)
                elif isinstance(res, Mapping) and res.get("ok") is False:
                    failed.append(name)
                else:
                    done.append(name)
            except Exception as e:                # noqa: BLE001  收尾必须继续
                failed.append(f"{name}:{type(e).__name__}")
        ok = not failed
        return {"ok": ok, "done": done, "failed": failed, "skipped": skipped}


# ---------------- 规划执行器 ----------------

class RecoveryRunner:
    """把守卫 + 规划器 + 能力库接起来：检测 → 组合作战 → 验目标 → 升级。"""

    def __init__(self, guard: sg.SceneGuard, library: CapabilityLibrary,
                 probe: Callable[[], sg.Signals | Mapping[str, Any]],
                 cfg: sg.GuardConfig | None = None,
                 clock: Callable[[], float] | None = None,
                 cleanup: Cleanup | None = None,
                 on_event: Callable[[dict], None] | None = None):
        self.guard = guard
        self.cfg = cfg or guard.cfg
        self.library = library
        self._probe = probe
        self._clock = clock or time.monotonic
        self.cleanup = cleanup or Cleanup()
        self.planner = Planner(library, self.cfg.max_depth, self.cfg.max_plans)
        self._tried_level: tuple[str | None, int] | None = None
        self._tried: set[tuple[str, ...]] = set()
        self._actions = 0
        self._started = self._clock()
        self._on_event = on_event
        self.stats = {"plans": 0, "actions": 0, "contained": 0, "manual": 0}

    def probe(self) -> sg.Signals:
        raw = self._probe()
        return raw if isinstance(raw, sg.Signals) else sg.Signals.from_mapping(raw)

    def step(self, now: float | None = None) -> dict:
        t = self._clock() if now is None else float(now)
        snap = self.guard.feed(self.probe(), now=t)
        self._emit(snap)

        if snap["state"] == sg.STATE_HALTED:
            return self._contain(snap, t)

        if snap["state"] == sg.STATE_WAITING_HUMAN:
            self.stats["manual"] += 1
            return snap

        if snap["state"] != sg.STATE_RECOVERING:
            return snap

        if self._elapsed(t) >= self._total_budget():
            self.guard.tick(t)
            return self._finish(self.guard.snapshot(), t)

        scene = snap["scene"] or ""
        level = int(snap["level"])
        key = (scene, level)
        if self._tried_level != key:
            self._tried_level = key
            self._tried.clear()
            self._actions = 0

        for plan in self.planner.plans(scene, level):
            if plan.key() in self._tried:
                continue
            self._tried.add(plan.key())
            self.stats["plans"] += 1
            self.guard.set_plan(list(plan.actions))
            if self._execute(plan, scene, t):
                self.guard.succeed(t)
                self._emit(self.guard.snapshot())
                return self.guard.snapshot()
            # 单条组合失败只记录；一轮所有组合都失败才算这一级失败。
            self.guard.record_action("plan", False, t)
            if self.guard.state != sg.STATE_RECOVERING:
                return self._finish(self.guard.snapshot(), t)
            if self._actions >= self.cfg.max_actions:
                break

        # 这一轮所有组合都没到目标：升级一级（或进人工/硬停）
        self.guard.plan_failed("本轮组合用尽", t)
        return self._finish(self.guard.snapshot(), t)

    def _finish(self, snap: dict, t: float) -> dict:
        """状态落定后立刻处理：硬停就在同一拍把收尾做完，不留给下一轮。"""
        if snap["state"] == sg.STATE_HALTED:
            return self._contain(snap, t)
        self._emit(snap)
        return snap

    def _execute(self, plan: Plan, scene: str, t: float) -> bool:
        ctx_base = {"scene": scene, "level": self.guard.level,
                    "evidence": dict(self.guard.snapshot().get("evidence") or {}),
                    "now": t}
        for action_id in plan.actions:
            if self._actions >= self.cfg.max_actions:
                return False
            if self._elapsed(t) >= self._total_budget():
                return False
            self._actions += 1
            self.stats["actions"] += 1
            ctx = dict(ctx_base)
            ctx["signals"] = self.probe()
            res = self.library.run(action_id, ctx)
            self.guard.record_action(action_id, res.ok, t)
            if not res.ok:
                return False
            s = self.probe()
            self.guard.add_evidence(**res.evidence)
            if sg.goal_met(scene, s):
                return True
        return False

    def _contain(self, snap: dict, t: float) -> dict:
        if not snap.get("containment_pending"):
            return snap
        ctx = {"scene": snap.get("scene"), "level": sg.LEVEL_HALT,
               "evidence": dict(snap.get("evidence") or {}), "now": t}
        ok_all = True
        for name in ("freeze", "release_input", "reset_cursor",
                     "unhook", "reap", "close_capture", "record"):
            res = self.library.run(name, ctx)
            self.guard.record_action(name, res.ok, t)
            ok_all = ok_all and res.ok
        cleanup = self.cleanup.run(ctx)
        ok_all = ok_all and cleanup["ok"]
        self.guard.add_evidence(containment_ok=ok_all)
        self.guard.containment_done(t)
        self.stats["contained"] += 1
        snap = self.guard.snapshot()
        snap["containment_ok"] = ok_all
        self._emit(snap)
        return snap

    def _elapsed(self, t: float) -> float:
        since = float(self.guard.snapshot().get("since") or self._started)
        return max(0.0, t - since)

    def _total_budget(self) -> float:
        snap = self.guard.snapshot()
        spec = sg.SCENES.get(snap.get("scene") or "")
        return float(spec.total_s if spec else self.cfg.max_total_s)

    def _emit(self, snap: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(snap)
            except Exception:                 # noqa: BLE001
                pass


class ManualWatcher:
    """人工插入段：停在准确位置，持续监听；检测到已通过就自动续跑。"""

    def __init__(self, guard: sg.SceneGuard,
                 probe: Callable[[], sg.Signals | Mapping[str, Any]],
                 on_resume: Callable[[], None] | None = None,
                 position: Callable[[dict], Any] | None = None,
                 clock: Callable[[], float] | None = None):
        self.guard = guard
        self._probe = probe
        self._on_resume = on_resume
        self._position = position
        self._clock = clock or time.monotonic
        self._positioned_for: str | None = None
        self.polls = 0

    def poll(self, now: float | None = None) -> dict:
        t = self._clock() if now is None else float(now)
        snap = self.guard.snapshot()
        if snap["state"] != sg.STATE_WAITING_HUMAN:
            return {"resumed": False, "state": snap["state"]}
        scene = snap.get("scene") or ""
        if self._positioned_for != scene:
            self._positioned_for = scene
            if self._position is not None:
                try:
                    self._position(snap)
                except Exception:             # noqa: BLE001
                    pass
        s = self._probe()
        s = s if isinstance(s, sg.Signals) else sg.Signals.from_mapping(s)
        self.polls += 1
        if sg.goal_met(scene, s):
            self.guard.add_evidence(manual_ready=True)
            self.guard.auto_resume(t)
            self._positioned_for = None
            if self._on_resume is not None:
                try:
                    self._on_resume()
                except Exception:             # noqa: BLE001
                    pass
            return {"resumed": True, "state": sg.STATE_OK, "scene": scene}
        # 超时只记录，不改变界面；真正的硬停由上层按 hard_timeout 决定
        waited = max(0.0, t - float(snap.get("since") or t))
        return {"resumed": False, "state": sg.STATE_WAITING_HUMAN,
                "scene": scene, "waited_s": round(waited, 1),
                "over_timeout": waited >= self.guard.cfg.manual_timeout_s}
