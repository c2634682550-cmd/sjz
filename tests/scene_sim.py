"""场景守卫 / 恢复规划器的模拟环境。

不碰真机：用假遥测 + 独立的「故障世界」模拟「哪几层坏了、每个动作成功率多少」，
统计每个场景在 200 组随机变体下的三档结果：
  · 自动解决
  · 人工介入后自动继续
  · 彻底失败

和旧版的区别（旧版是「自己证自己」）
----------------------------------
旧版 performer 只要动作返回 ok，就**直接**把遥测改成目标状态，于是
`goal_met` 必然为真、200 组必然 0 失败 —— 判据和执行器是同一个人。

现在分成三个互不重叠的角色：

  · `World`：独立保存「哪些故障层还坏着」。只有动作**真的修好了某一层**，
    状态才会变；动作返回值（`ok`）和真实状态（`changed`）是两个字段。
  · `SceneGuard/RecoveryRunner`：被测实现，只能通过 `probe()` 看到 World 的
    真实状态，靠 `goal_met` 判断有没有恢复。
  · `oracle`：断言侧独立写一份「什么算恢复」（tests/test_recovery.py 里），
    用来抓「守卫宣布成功但其实没恢复」。

performer 也能返回失败：`p_action` / `p_fix` / `p_safety` 都可以注入，
用来证明这套模拟真的能报出失败（见 test_recovery.py 的负向对照）。

它也是 tests/test_scene_guard.py 的数据来源。
"""
from __future__ import annotations

import copy
import os
import random
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import recovery, scene_guard as sg


def sim_config() -> sg.GuardConfig:
    return sg.GuardConfig(
        l1_fail_n=3, l2_fail_n=2, l3_fail_n=2,
        l1_budget_s=6.0, l2_budget_s=8.0, l3_budget_s=10.0,
        max_total_s=40.0, max_actions=20, max_plans=8, max_depth=3,
        clear_ticks=3, clear_window_s=2.0, min_interval_s=0.0,
        manual_timeout_s=60.0, hard_timeout_s=120.0,
    )


def normal_signals(rng: random.Random) -> sg.Signals:
    off = rng.uniform(-140, 140)
    prev = off + rng.uniform(-150, 150)
    return sg.Signals(
        page="onsale",
        expected_pages=("market", "onsale", "detail"),
        anchor_hits=rng.randint(2, 9), anchor_total=9,
        anchor_pages_sampled=rng.randint(0, 3),
        click_effect_age_s=rng.uniform(0.05, 0.34),
        frame_changed=True,
        frame_dt_ms=rng.uniform(14, 70),
        frame_dt_p95_ms=rng.uniform(16, 230),
        confirm_found=False,
        budget=0.0,
        balance_before=None, balance_after=None, expected_price=None,
        refresh_rounds=rng.randint(0, 7),
        hits=rng.randint(0, 3),
        stall_s=rng.uniform(0, 590),
        list_fresh=True,
        countdown_conf=rng.uniform(0.8, 1.0),
        countdown_jitter_ms=rng.uniform(0, 100),
        ocr_fail_streak=rng.randint(0, 4),
        ocr_value_jump=rng.uniform(0, 0.25),
        clock_offset_ms=off, prev_clock_offset_ms=prev,
        hwnd=1, hwnd_alive=True, minimized=False, foreground=True,
        game_alive=True, login_visible=False,
        screen_size=(1920, 1080), baseline_size=(1920, 1080),
        scale=1.0, baseline_scale=1.0,
        balance=1000.0 + rng.uniform(0, 500), price=100.0,
        quota_left=rng.randint(1, 5),
        anticheat=False,
        user_input_age_s=rng.uniform(5, 120), own_input_age_s=0.1,
        in_fire=False,
        driver_ok=True, worker_alive=True, config_ok=True,
    )


def anomaly_signals(scene: str, rng: random.Random) -> sg.Signals:
    s = normal_signals(rng)
    if scene == sg.SC_CLICK_NO_EFFECT:
        s.page, s.frame_changed = "onsale", False
        s.click_effect_age_s = rng.uniform(0.4, 2.0)
    elif scene == sg.SC_UNEXPECTED_PAGE:
        s.page = "unknown"
        s.click_effect_age_s = rng.uniform(0.2, 1.0)
    elif scene == sg.SC_CONFIRM_UNEXPECTED:
        s.page = "detail"
        s.confirm_found = True
        s.confirm_price = rng.uniform(150, 800)
        s.budget = rng.uniform(50, 120)
        s.confirm_item = "other"
        s.expected_item = "target"
    elif scene == sg.SC_MISBUY:
        price = rng.uniform(50, 300)
        s.expected_price = price
        s.balance_before = rng.uniform(900, 2000)
        s.balance_after = s.balance_before - price * rng.uniform(1.3, 2.5)
        s.bought_delta = rng.choice((1, 2))
    elif scene == sg.SC_LIST_STALL:
        s.hits = 0
        s.refresh_rounds = rng.randint(8, 40)
        s.stall_s = rng.uniform(600, 1800)
        s.list_fresh = False
    elif scene == sg.SC_LAG:
        s.frame_dt_p95_ms = rng.uniform(260, 700)
        s.frame_dt_ms = rng.uniform(120, 400)
        s.countdown_conf = rng.uniform(0.1, 0.55)
        s.countdown_jitter_ms = rng.uniform(130, 400)
        s.in_fire = True
    elif scene == sg.SC_WINDOW_LOST:
        s.foreground = False
        s.minimized = True
        s.page = "unknown"
    elif scene == sg.SC_GAME_DOWN:
        s.game_alive = False
        s.login_visible = True
        s.page = "login"
    elif scene == sg.SC_UI_CHANGED:
        s.anchor_hits = 0
        s.anchor_pages_sampled = rng.randint(2, 5)
    elif scene == sg.SC_RESOLUTION_CHANGED:
        s.screen_size = rng.choice(((1600, 900), (2560, 1440), (1280, 720)))
        s.scale = rng.choice((0.8, 1.25, 1.5))
    elif scene == sg.SC_OCR_FAIL:
        s.ocr_fail_streak = rng.randint(5, 20)
        s.ocr_value_jump = rng.uniform(0.3, 0.9)
    elif scene == sg.SC_CLOCK_DRIFT:
        s.clock_offset_ms = rng.choice((-1, 1)) * rng.uniform(160, 900)
        s.prev_clock_offset_ms = s.clock_offset_ms + rng.uniform(-300, 300)
    elif scene == sg.SC_INSUFFICIENT:
        s.price = rng.uniform(150, 400)
        s.balance = s.price - rng.uniform(1, 100)
    elif scene == sg.SC_ANTICHEAT:
        s.anticheat = True
    elif scene == sg.SC_USER_TAKEOVER:
        s.user_input_age_s = rng.uniform(0.0, 0.4)
    elif scene == sg.SC_SELF_FAULT:
        if rng.random() < 0.5:
            s.driver_ok = False
        else:
            s.worker_alive = False
    elif scene == sg.SC_CONFIG_FAULT:
        s.config_ok = False
    return s


def fix_goal(scene: str, s: sg.Signals) -> None:
    """目标状态（人工完成 / 全部故障层修好后的样子）。"""
    if scene == sg.SC_CLICK_NO_EFFECT:
        s.page, s.frame_changed = "onsale", True
    elif scene in (sg.SC_UNEXPECTED_PAGE, sg.SC_UI_CHANGED):
        s.page, s.anchor_hits = "market", 9
    elif scene == sg.SC_CONFIRM_UNEXPECTED:
        s.page, s.confirm_found = "onsale", False
    elif scene in (sg.SC_MISBUY, sg.SC_ANTICHEAT):
        s.clicks_frozen = s.cursor_restored = True
        s.hooks_unloaded = s.record_written = True
    elif scene == sg.SC_LIST_STALL:
        s.page, s.list_fresh = "onsale", True
    elif scene == sg.SC_LAG:
        s.frame_dt_p95_ms, s.countdown_conf = 18.0, 0.95
        s.frame_dt_ms, s.countdown_jitter_ms = 16.0, 20.0
    elif scene == sg.SC_WINDOW_LOST:
        s.hwnd_alive, s.minimized, s.foreground = True, False, True
        s.page = "onsale"
    elif scene == sg.SC_GAME_DOWN:
        s.game_alive, s.login_visible, s.page = True, False, "hall"
    elif scene == sg.SC_RESOLUTION_CHANGED:
        s.screen_size, s.scale, s.recalibrated = (1920, 1080), 1.0, True
    elif scene == sg.SC_OCR_FAIL:
        s.ocr_fail_streak, s.ocr_value_jump, s.page = 0, 0.0, "onsale"
    elif scene == sg.SC_CLOCK_DRIFT:
        s.clock_offset_ms, s.prev_clock_offset_ms = 20.0, 20.0
    elif scene == sg.SC_INSUFFICIENT:
        s.confirm_found, s.purchase_blocked = False, True
        s.page = "onsale"
    elif scene == sg.SC_USER_TAKEOVER:
        s.user_input_age_s, s.page = 30.0, "onsale"
    elif scene == sg.SC_SELF_FAULT:
        s.driver_ok = s.worker_alive = True
    elif scene == sg.SC_CONFIG_FAULT:
        s.config_ok = True


# 每个场景「哪几层」坏了：只有对应组的动作能把那一层真正修好。
REQUIRED_GROUPS = {
    sg.SC_CLICK_NO_EFFECT: ("page", "read"),
    sg.SC_UNEXPECTED_PAGE: ("page", "read"),
    sg.SC_CONFIRM_UNEXPECTED: ("page",),
    sg.SC_MISBUY: (),
    sg.SC_LIST_STALL: ("page",),
    sg.SC_LAG: ("system", "time"),
    sg.SC_WINDOW_LOST: ("window", "page"),
    sg.SC_GAME_DOWN: ("window", "system", "time"),
    sg.SC_UI_CHANGED: ("read", "page"),
    sg.SC_RESOLUTION_CHANGED: ("window", "page"),
    sg.SC_OCR_FAIL: ("read", "system"),
    sg.SC_CLOCK_DRIFT: ("time",),
    sg.SC_INSUFFICIENT: (),
    sg.SC_ANTICHEAT: (),
    sg.SC_USER_TAKEOVER: (),
    sg.SC_SELF_FAULT: ("system",),
    sg.SC_CONFIG_FAULT: ("system",),
}

# 每一层修好后，遥测里哪几个字段会变。全部分层补丁叠加 == fix_goal 的效果。
# 这里的拆分是对外契约：某个层的动作只能修好这一层，不能顺手把别的层也改好。
LAYER_FIX = {
    sg.SC_CLICK_NO_EFFECT: {
        "page": {"page": "onsale"},
        "read": {"frame_changed": True},
    },
    sg.SC_UNEXPECTED_PAGE: {
        "page": {"page": "market", "anchor_hits": 9},
        "read": {},
    },
    sg.SC_CONFIRM_UNEXPECTED: {
        "page": {"page": "onsale", "confirm_found": False},
    },
    sg.SC_LIST_STALL: {
        "page": {"page": "onsale", "list_fresh": True},
    },
    sg.SC_LAG: {
        "system": {"frame_dt_p95_ms": 18.0, "frame_dt_ms": 16.0},
        "time": {"countdown_conf": 0.95, "countdown_jitter_ms": 20.0},
    },
    sg.SC_WINDOW_LOST: {
        "window": {"hwnd_alive": True, "minimized": False, "foreground": True},
        "page": {"page": "onsale"},
    },
    sg.SC_GAME_DOWN: {
        "window": {"game_alive": True, "login_visible": False, "page": "hall"},
        "system": {},
        "time": {},
    },
    sg.SC_UI_CHANGED: {
        "read": {"anchor_hits": 9},
        "page": {"page": "market"},
    },
    sg.SC_RESOLUTION_CHANGED: {
        "window": {"screen_size": (1920, 1080), "scale": 1.0, "recalibrated": True},
        "page": {},
    },
    sg.SC_OCR_FAIL: {
        "read": {"ocr_fail_streak": 0, "ocr_value_jump": 0.0},
        "system": {"page": "onsale"},
    },
    sg.SC_CLOCK_DRIFT: {
        "time": {"clock_offset_ms": 20.0, "prev_clock_offset_ms": 20.0},
    },
    sg.SC_SELF_FAULT: {
        "system": {"driver_ok": True, "worker_alive": True},
    },
    sg.SC_CONFIG_FAULT: {
        "system": {"config_ok": True},
    },
}

# 真正过不去的比例：游戏要重登 / 界面要重新设置 / 账户要充值 / 用户接管。
MANUAL_PROB = {
    sg.SC_GAME_DOWN: 0.60,
    sg.SC_UI_CHANGED: 0.55,
    sg.SC_RESOLUTION_CHANGED: 0.30,
    sg.SC_INSUFFICIENT: 1.0,
    sg.SC_USER_TAKEOVER: 1.0,
    sg.SC_CONFIG_FAULT: 0.25,
    sg.SC_MISBUY: 0.0,
    sg.SC_ANTICHEAT: 0.0,
}
DEFAULT_MANUAL_PROB = 0.08

# 收尾类动作：不修任何故障层，只写自己的「已还原」标记。
SAFETY_ACTIONS = frozenset({
    "freeze", "release_input", "reset_cursor", "unhook",
    "reap", "close_capture", "record",
})

FLAG_ACTIONS = {
    "freeze": "clicks_frozen",
    "reset_cursor": "cursor_restored",
    "unhook": "hooks_unloaded",
    "record": "record_written",
}


class World:
    """独立的故障世界。只认「某一层真的被修好」，不认动作返回值。"""

    def __init__(self, scene: str, rng: random.Random):
        self.scene = scene
        self.broken = set(REQUIRED_GROUPS.get(scene, ()))
        self.flags: set[str] = set()
        self.base = anomaly_signals(scene, rng)

    @property
    def required(self) -> tuple[str, ...]:
        return REQUIRED_GROUPS.get(self.scene, ())

    def signals(self) -> sg.Signals:
        """按当前真实故障状态拼出遥测：坏着的层不应用修复补丁。"""
        s = copy.deepcopy(self.base)
        patch = LAYER_FIX.get(self.scene, {})
        for layer in self.required:
            if layer in self.broken:
                continue
            for key, value in patch.get(layer, {}).items():
                setattr(s, key, value)
        for flag in self.flags:
            setattr(s, flag, True)
        return s

    def repair(self, layer: str) -> bool:
        if layer not in self.broken:
            return False
        self.broken.discard(layer)
        return True

    def set_flag(self, flag: str) -> None:
        self.flags.add(flag)

    def manual_fix(self) -> None:
        """人工把现场收拾好（登录 / 充值 / 用户停手 / 重新设置）。"""
        fix_goal(self.scene, self.base)
        self.broken.clear()
        self.flags.update(FLAG_ACTIONS.values())


class SimEnv:
    def __init__(self, scene: str, seed: int, *,
                 p_fix: float | None = None,
                 p_action: float | None = None,
                 p_safety: float | None = None,
                 manual_only: bool | None = None,
                 repair_enabled: bool = True):
        self.scene = scene
        self.rng = random.Random(seed)
        self.now = 0.0
        self.world = World(scene, self.rng)
        self.manual_only = (self.rng.random() < MANUAL_PROB.get(scene, DEFAULT_MANUAL_PROB)
                            if manual_only is None else manual_only)
        self.p_fix = self.rng.uniform(0.55, 0.92) if p_fix is None else p_fix
        self.p_action = self.rng.uniform(0.75, 0.95) if p_action is None else p_action
        self.p_safety = 1.0 if p_safety is None else p_safety
        self.repair_enabled = repair_enabled
        self.actions = 0
        self.cleanup_calls: list[str] = []
        self.cleanup = recovery.Cleanup(recovery.ResourceAdapters(
            freeze=self._flag_adapter("freeze", "clicks_frozen"),
            release_input=self._ok_adapter("release_input"),
            reset_cursor=self._flag_adapter("reset_cursor", "cursor_restored"),
            unhook=self._flag_adapter("unhook", "hooks_unloaded"),
            reap=self._ok_adapter("reap"),
            close_capture=self._ok_adapter("close_capture"),
            record=self._flag_adapter("record", "record_written"),
        ))
        self.library = recovery.CapabilityLibrary(performer=self._perform)
        self.manual_delay = self.rng.randint(2, 12)
        self.manual_since: int | None = None

    def _ok_adapter(self, name):
        def fn(*_a):
            self.cleanup_calls.append(name)
            return True
        return fn

    def _flag_adapter(self, name, flag):
        def fn(*_a):
            self.cleanup_calls.append(name)
            self.world.set_flag(flag)
            return True
        return fn

    def probe(self) -> sg.Signals:
        return self.world.signals()

    def _perform(self, action_id: str, ctx) -> dict:
        self.actions += 1
        self.cleanup_calls.append(f"action:{action_id}")
        spec = recovery.ACTIONS[action_id]

        # 收尾类动作：机械成功率单独注入；成功才写「已还原」标记。
        if action_id in SAFETY_ACTIONS:
            ok = self.rng.random() <= self.p_safety
            if ok and action_id in FLAG_ACTIONS:
                self.world.set_flag(FLAG_ACTIONS[action_id])
            return {"ok": ok}

        # 环境要求人工时，自动动作物理上做不成（但返回值依旧单独计算）
        if self.manual_only:
            return {"ok": False, "changed": False}

        ok = self.rng.random() <= self.p_action
        changed = False
        if ok and self.repair_enabled and spec.group in self.world.broken:
            if self.rng.random() <= self.p_fix:
                changed = self.world.repair(spec.group)
            # ok=True 但 changed=False 是允许的：动作发出去了、状态没变。
        return {"ok": ok, "changed": changed}


def simulate_env(env: SimEnv, max_ticks: int = 80) -> dict:
    cfg = sim_config()
    guard = sg.SceneGuard(cfg, clock=lambda: env.now)
    runner = recovery.RecoveryRunner(
        guard, env.library, env.probe, cfg,
        clock=lambda: env.now, cleanup=env.cleanup)
    watcher = recovery.ManualWatcher(
        guard, env.probe, on_resume=lambda: None,
        clock=lambda: env.now)

    outcome = "fail"
    halted = False
    started_manual = False
    entered = False
    halt_ok: bool | None = None
    tick = 0
    for tick in range(max_ticks):
        env.now += 1.0
        snap = runner.step(env.now)
        state = guard.state
        if guard.entries > 0:
            entered = True
        if state == sg.STATE_OK:
            if not entered:
                continue          # 还在确认阶段（防抖未满），不算恢复成功
            outcome = "manual_continue" if started_manual else "auto"
            break
        if state == sg.STATE_HALTED:
            if halt_ok is None:
                halt_ok = bool(snap.get("containment_ok"))
                halted = halt_ok
            if not halt_ok:
                # 安全收尾自己失败了 —— 这是彻底失败，不能靠「停下来」蒙混过关
                outcome = "fail"
                break
            # 光「停下来」不算恢复；只有真到目标状态才算自动解决。
            if sg.goal_met(env.scene, env.probe()):
                outcome = "auto"
                break
            # 硬停后由用户处理（改配置 / 重开游戏），处理完才算「人工后继续」
            started_manual = True
        if state in (sg.STATE_WAITING_HUMAN, sg.STATE_HALTED):
            if env.manual_since is None:
                env.manual_since = tick
            if tick - env.manual_since >= env.manual_delay:
                env.world.manual_fix()
                if sg.goal_met(env.scene, env.probe()):
                    outcome = "manual_continue"
                    break
    return {
        "scene": env.scene,
        "outcome": outcome,
        "halted": halted,
        "ticks": tick + 1,
        "actions": env.actions,
        "levels": guard.level,
        "cleanup": list(env.cleanup_calls),
        "signals": env.probe(),
        "broken": tuple(sorted(env.world.broken)),
    }


def simulate(scene: str, seed: int, max_ticks: int = 80, **env_kw) -> dict:
    return simulate_env(SimEnv(scene, seed, **env_kw), max_ticks=max_ticks)


def run_all(n: int = 200, oracle=None) -> dict:
    """跑全部场景。

    `oracle(scene, signals) -> bool` 由断言方传入（独立于 `goal_met` 的第二份判据）。
    凡是「守卫宣布恢复 / 人工续跑」但 oracle 说没恢复的，计入 false_success ——
    这个数必须为 0，否则就是「自己证自己」的回潮。
    """
    stats: dict[str, dict] = {}
    for scene in sg.MATRIX_SCENES + (sg.SC_CONFIG_FAULT,):
        counts = {"auto": 0, "manual_continue": 0, "fail": 0}
        auto_halt = 0
        details = []
        false_success = 0
        for seed in range(n):
            r = simulate(scene, seed)
            counts[r["outcome"]] += 1
            if r["halted"]:
                auto_halt += 1
            if oracle is not None and r["outcome"] in ("auto", "manual_continue"):
                if not oracle(scene, r["signals"]):
                    false_success += 1
            details.append(r)
        stats[scene] = {
            "scene_text": sg.SCENES[scene].name,
            "counts": counts,
            "rate": {k: round(v / n, 4) for k, v in counts.items()},
            "auto_halt": auto_halt,
            "false_success": false_success,
            "max_actions": max(d["actions"] for d in details),
            "max_ticks": max(d["ticks"] for d in details),
            "auto_min_actions": min((d["actions"] for d in details
                                     if d["outcome"] == "auto"), default=0),
        }
    return stats


def false_positive_rate(scene: str, n: int = 200, ticks: int = 6) -> float:
    """正常信号抖动连续喂 6 次，守卫被判成异常的比例。"""
    bad = 0
    for seed in range(1000, 1000 + n):
        rng = random.Random(seed)
        now = 0.0
        guard = sg.SceneGuard(sim_config(), clock=lambda: now)
        flagged = False
        for step in range(ticks):
            now = float(step)
            s = normal_signals(rng)
            snap = guard.feed(s, now=now)
            if snap["state"] != sg.STATE_OK or snap["level"] > 0:
                flagged = True
                break
        if flagged:
            bad += 1
    return round(bad / n, 4)


if __name__ == "__main__":
    stats = run_all(200)
    print("scene,auto,manual,fail,auto_halt,auto_rate,manual_rate,fail_rate,max_actions")
    for sid, row in stats.items():
        c = row["counts"]
        print(f"{sid},{c['auto']},{c['manual_continue']},{c['fail']},"
              f"{row['auto_halt']},"
              f"{row['rate']['auto']:.3f},{row['rate']['manual_continue']:.3f},"
              f"{row['rate']['fail']:.3f},{row['max_actions']}")
    print("false_positive:")
    for sid in sg.MATRIX_SCENES:
        print(f"{sid},{false_positive_rate(sid, 200)}")
