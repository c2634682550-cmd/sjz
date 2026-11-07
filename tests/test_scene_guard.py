"""场景守卫的回归测试：矩阵完整、跳级正确、优先级合并、防抖与滞回。

这些断言直接对应的交付要求：
  · 16 类场景，每类五项（检测 / 级别 / 动作 / 升级 / 一句话）齐全；
  · 用户可见文案没有专业术语，每个异常只有一个主按钮；
  · 误买 / 反作弊直接硬停，余额不足 / 用户接管直接进人工等待；
  · 同时发生的异常有优先级和合并规则，不会两套恢复动作打架；
  · 不出现「卡在原地反复重试」。
"""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import recovery, scene_guard as sg          # noqa: E402
from tests import scene_sim as sim                     # noqa: E402

# 用户可见文案里绝不能出现的词（一个都不许有）
JARGON = [
    "OCR", "ocr", "DPI", "dpi", "锚点", "注入", "UIPI", "句柄", "钩子",
    "子进程", "坐标", "分辨率", "模板", "识别", "进程", "像素", "标定",
    "校验", "接口", "端口", "路径", "线程", "缓存", "算法", "阈值",
]


def _user_texts(spec: sg.SceneSpec):
    return [spec.name, spec.goal_text, spec.guide_message,
            spec.halt_message, spec.button_label]


def test_matrix_has_16_unique_scenes():
    assert len(sg.MATRIX_SCENES) == 16
    assert len(set(sg.MATRIX_SCENES)) == 16
    for sid in sg.MATRIX_SCENES:
        assert sid in sg.SCENES


@pytest.mark.parametrize("sid", list(sg.MATRIX_SCENES))
def test_scene_has_five_required_parts(sid):
    """五项：检测方式 / 级别 / 自动动作 / 升级次数 / 最终一句话。"""
    spec = sg.SCENES[sid]
    assert callable(spec.detect)                 # 怎么检测（可观测信号）
    assert spec.min_confirm >= 1                 # 确认次数（失败升级的起点）
    assert spec.goal_text                        # 恢复到哪个目标状态
    # 要么进人工（第 4 级），要么硬停（第 5 级），二者必有一句人话
    assert spec.guide_message or spec.halt_message
    assert spec.button_id and spec.button_label  # 唯一主按钮
    # 自动动作：每个场景在能力库里至少有一个可用原子动作
    lib = recovery.CapabilityLibrary(performer=lambda a, c: {"ok": True})
    acts = recovery.available_actions(sid, sg.LEVEL_RETRY, lib)
    assert acts, f"{sid} 一级没有任何可用原子动作"


@pytest.mark.parametrize("sid", list(sg.MATRIX_SCENES) + [sg.SC_CONFIG_FAULT])
def test_user_text_has_no_jargon(sid):
    spec = sg.SCENES[sid]
    for text in _user_texts(spec):
        for word in JARGON:
            assert word not in text, f"{sid} 的用户文案出现「{word}」：{text}"


@pytest.mark.parametrize("sid", list(sg.MATRIX_SCENES) + [sg.SC_CONFIG_FAULT])
def test_each_scene_has_exactly_one_primary_button(sid):
    """主按钮只能有一个，且点一下就知道会发生什么。"""
    spec = sg.SCENES[sid]
    assert spec.button_id in ("resume", "wait_more", "reconnect", "quit")
    assert spec.button_label.strip()


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4, 5])
def test_doing_text_present_for_every_level(level):
    text = sg._DOING[level]
    assert text and level in sg._DOING
    assert sg.LEVEL_TEXT[level]


def test_recovering_state_still_has_one_button():
    """自动处理中也给一个主按钮（停止），不是只转圈。"""
    now = 0.0
    g = sg.SceneGuard(sim.sim_config(), clock=lambda: now)
    rng = random.Random(7)
    s = sim.anomaly_signals(sg.SC_CLICK_NO_EFFECT, rng)
    g.feed(s, now=0)
    snap = g.feed(s, now=1)
    assert snap["state"] == sg.STATE_RECOVERING
    assert snap["button"] == {"id": "stop", "label": "停止"}
    assert snap["doing"]


@pytest.mark.parametrize("sid, level", [
    (sg.SC_MISBUY, sg.LEVEL_HALT),
    (sg.SC_ANTICHEAT, sg.LEVEL_HALT),
    (sg.SC_INSUFFICIENT, sg.LEVEL_GUIDE),
    (sg.SC_USER_TAKEOVER, sg.LEVEL_GUIDE),
])
def test_safety_scenes_jump_straight_to_final_level(sid, level):
    """误买 / 反作弊 / 余额不足 / 用户接管：不许从第 1 级慢慢试。"""
    now = 0.0
    g = sg.SceneGuard(sim.sim_config(), clock=lambda: now)
    snap = g.force(sid, now=0)
    assert snap["scene"] == sid
    assert snap["level"] == level
    # 历史里不能出现 level_up（说明没有先试 1→2→3）
    assert not [h for h in snap["history"] if h.get("action") == "level_up"]


def test_misbuy_enters_halted_with_one_sentence():
    now = 0.0
    g = sg.SceneGuard(sim.sim_config(), clock=lambda: now)
    snap = g.force(sg.SC_MISBUY, {"balance_before": 1000, "balance_after": 700,
                                  "expected_price": 100}, now=0)
    assert snap["state"] == sg.STATE_HALTED
    assert snap["button"]["id"] == "quit"
    # 自动止损还没做完，必须先走安全收尾再算完
    assert snap["containment_pending"] is True


def test_priority_lag_suppresses_ocr():
    """卡顿和识别失败同时发生：卡顿优先，先不启动识别的恢复动作。"""
    rng = random.Random(11)
    s = sim.anomaly_signals(sg.SC_LAG, rng)
    s.ocr_fail_streak = 12
    detected = sg.detect_all(s)
    assert sg.SC_LAG in detected and sg.SC_OCR_FAIL in detected
    assert sg.resolve(detected, None) == sg.SC_LAG


def test_priority_anticheat_beats_lower_scenes():
    """反作弊和误买同时出现：两者都要停，但只允许一个 owner —— 优先级高的先停。"""
    rng = random.Random(12)
    s = sim.anomaly_signals(sg.SC_MISBUY, rng)
    s.anticheat = True
    s.ocr_fail_streak = 9
    detected = sg.detect_all(s)
    assert sg.SC_MISBUY in detected and sg.SC_ANTICHEAT in detected
    assert sg.resolve(detected, None) == sg.SC_ANTICHEAT


def test_priority_misbuy_beats_soft_scenes():
    rng = random.Random(12)
    s = sim.anomaly_signals(sg.SC_MISBUY, rng)
    s.ocr_fail_streak = 9
    s.click_effect_age_s = 2.0
    s.frame_changed = False
    detected = sg.detect_all(s)
    assert sg.SC_OCR_FAIL in detected and sg.SC_CLICK_NO_EFFECT in detected
    assert sg.resolve(detected, None) == sg.SC_MISBUY


def test_merge_keeps_single_owner_while_recovering():
    """两套异常同时来，守卫同一时刻只允许一个 owner，另一套不启动动作。"""
    rng = random.Random(13)
    now = 0.0
    g = sg.SceneGuard(sim.sim_config(), clock=lambda: now)
    lag = sim.anomaly_signals(sg.SC_LAG, rng)
    lag.ocr_fail_streak = 8
    for i in range(3):
        snap = g.feed(lag, now=i)
    assert g.owner == sg.SC_LAG
    assert snap["scene"] == sg.SC_LAG
    # 识别失败没有成为 owner，历史里也没有它的恢复动作
    assert not [h for h in snap["history"] if h.get("scene") == sg.SC_OCR_FAIL]


def test_window_loss_suppresses_unexpected_page():
    rng = random.Random(14)
    s = sim.anomaly_signals(sg.SC_WINDOW_LOST, rng)   # 页面同时是 unknown
    detected = sg.detect_all(s)
    assert sg.SC_UNEXPECTED_PAGE in detected
    assert sg.resolve(detected, None) == sg.SC_WINDOW_LOST


@pytest.mark.parametrize("sid", list(sg.MATRIX_SCENES) + [sg.SC_CONFIG_FAULT])
def test_goal_met_after_recovery(sid):
    """异常信号能被检测，修好后必须判为「已到达目标」。"""
    rng = random.Random(21)
    s = sim.anomaly_signals(sid, rng)
    spec = sg.SCENES[sid]
    assert spec.detect(s), f"{sid} 的异常信号没有被检测器认出"
    assert not sg.goal_met(sid, s), f"{sid} 异常状态不应算已恢复"
    sim.fix_goal(sid, s)
    assert sg.goal_met(sid, s), f"{sid} 修好后没有判为恢复"


def test_hysteresis_requires_clear_ticks():
    """一两拍正常不足以清零，避免抖动里反复进出。"""
    rng = random.Random(31)
    now = 0.0
    g = sg.SceneGuard(sim.sim_config(), clock=lambda: now)
    bad = sim.anomaly_signals(sg.SC_CLICK_NO_EFFECT, rng)
    g.feed(bad, now=0)
    g.feed(bad, now=1)
    assert g.state == sg.STATE_RECOVERING
    good = sim.normal_signals(rng)
    g.feed(good, now=2)
    g.feed(good, now=3)
    assert g.state == sg.STATE_RECOVERING      # clear_ticks=3，还差一拍
    g.feed(good, now=4)
    assert g.state == sg.STATE_OK


@pytest.mark.parametrize("sid", list(sg.MATRIX_SCENES) + [sg.SC_CONFIG_FAULT])
def test_never_stuck_in_endless_retry(sid):
    """全自动手段全部失效时，也必须落到人工/硬停，不能原地死循环。"""
    env = sim.SimEnv(sid, 5)
    env.manual_only = True                     # 所有自动动作都失败
    cfg = sim.sim_config()
    guard = sg.SceneGuard(cfg, clock=lambda: env.now)
    runner = recovery.RecoveryRunner(
        guard, env.library, env.probe, cfg,
        clock=lambda: env.now, cleanup=env.cleanup)
    for tick in range(40):
        env.now += 1.0
        snap = runner.step(env.now)
        if guard.state in (sg.STATE_WAITING_HUMAN, sg.STATE_HALTED):
            break
    assert guard.state in (sg.STATE_WAITING_HUMAN, sg.STATE_HALTED), \
        f"{sid} 40 拍后仍在 recovering，属于原地反复重试"
    # 动作数有上限：每个级别 max_actions + 一次安全收尾
    assert env.actions <= cfg.max_actions * 3 + 7
    assert tick < 40
