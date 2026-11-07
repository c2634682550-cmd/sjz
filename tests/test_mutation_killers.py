# -*- coding: utf-8 -*-
"""本轮变异测试存活体（`_bug_r22_tests_dynamic.md`）的定点杀手用例。

原则：每个用例只在「原始实现为 A、变异实现为 B」时才会出现 A/B 不同的
可观察结果；不做覆盖率凑数，也不依赖墙钟时间。

原始数据（27 个变异体，存活 13）：
  价格判定 5/6 存活、倒计时 3/6、输入通道 2/5、异常分级 3/5。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cards, countdown, market  # noqa: E402
from core import input as input_mod  # noqa: E402
from core.input import (  # noqa: E402
    R_FALLBACK_DISABLED,
    R_UIPI_BLOCKED,
    InputChannelError,
    _decide_channel,
    _resolve_fallback,
)
from core import scene_guard as sg  # noqa: E402


# ============================================================ 价格判定

# P3：`max(cand)` -> `min(cand)`
@pytest.mark.parametrize("text", [
    "饰品格 025000 金币 1200",          # 整数候选：25000 与 1200
    "低价促销 1200 划线价 25000",       # 同一价格两个来源
])
def test_parse_row_picks_max_candidate_not_min(text):
    """多候选时必须取最高价；取最低价会把 2.5 万的货当成 1200 买。"""
    row = market.parse_row(text)
    assert row["price"] == 25000, f"多候选取值错了：{row}"


# P1：`val < _MIN_PRICE` -> `<=`
def test_price_floor_cards_keeps_exactly_100():
    assert cards._price_token_in("成色 99") == "", "99 应低于下限"
    assert cards._price_token_in("成色 100") == "100", "下限 100 必须保留"
    assert cards._price_token_in("成色 101") == "101"


# P6：`_MIN_PRICE = 100` -> `1000`
@pytest.mark.parametrize("text,want", [
    ("成色 A 0", None),
    ("成色 A 99", None),
    ("成色 A 100", 100),
    ("成色 A 999", 999),
    ("成色 A 1000", 1000),
    ("成色 A 1001", 1001),
])
def test_price_floor_market_boundary_points(text, want):
    """0 / 99 / 100 / 1000 / 1001 五个点：阈值一动就必须有断言掉下来。"""
    row = market.parse_row(text)
    assert row["price"] == want, f"{text!r} -> {row['price']}，期望 {want}"


def test_price_floors_agree_between_cards_and_market():
    assert cards._MIN_PRICE == market._MIN_PRICE == 100


# P4：`int(ip) > 5` -> `>= 5`（三位小数里的整数部分 5.000 被当成价格）
def test_five_thousand_wear_is_not_a_price():
    """整数段恰为 5 的「三位小数」仍按磨损处理，不许当 5000。

    注：P4 变异落在重构前的 `int(ip) > 5` 上；当前实现走 `money.find_numbers`
    的 decimal 分流，这里锁的是同一语义（5.000 不算价、6.000 才算）。
    """
    assert cards._price_token_in("成色 5.000") == "", "5.000 是磨损，不是 5000"
    assert cards._price_token_in("成色 6.000") == "6.000", "6.000 才允许当价格"


# P5：删掉 `1<=len(ip)<=3 and int(ip)>5` 两道校验
def test_three_decimal_wear_never_becomes_price():
    """0.x 的三位小数是磨损；混在价格行里时也绝不能顶掉真实价格。

    注：P5 变异点已被 N11 的 `money` 重构移除（`_price_token_in` 不再手写
    ip/fp 校验）。当前实现等价守卫在 `parse_row`：decimal 只进 floats、
    不进价格候选。这里按行为锁死，重构后再犯同样的错会立刻红灯。
    """
    assert cards._price_token_in("0.120") == "", "0.120 的整数部分是 0，不许放行"
    assert market.parse_row("0.120")["price"] is None
    # 磨损比真实价格“大”时最危险：放行会直接买错价
    assert market.parse_row("QBZ95 成色 0.120 14000")["price"] == 14000
    assert market.parse_row("QBZ95 成色 0.500 14000")["price"] == 14000


# ============================================================ 倒计时估计

# C2：速率违约阈值 `ds >= 2` -> `>= 3`
def _run(s, t0, count=5):
    return {"s": s, "t0": t0, "count": count}


def test_rate_violation_two_ticks_in_one_second():
    """1 秒掉 2 格：`ds>=2` 且 `dt<1.5*win` 必须判违约。"""
    assert countdown._rate_violation(
        [_run(10, 0.0), _run(8, 1.0)], win=1.0) is True


def test_rate_violation_two_ticks_is_allowed_after_enough_time():
    """同样掉 2 格，用了 1.6 秒（≥1.5*win）就不算违约。"""
    assert countdown._rate_violation(
        [_run(10, 0.0), _run(8, 1.6)], win=1.0) is False


def test_rate_violation_three_ticks_boundary():
    """掉 3 格：<2.5 秒违约，≥2.5 秒不违约。"""
    assert countdown._rate_violation(
        [_run(10, 0.0), _run(7, 2.4)], win=1.0) is True
    assert countdown._rate_violation(
        [_run(10, 0.0), _run(7, 2.6)], win=1.0) is False


def test_single_tick_drop_is_never_a_rate_violation():
    """单格下降不触发速率硬校验（1 格是正常倒计时）。"""
    assert countdown._rate_violation(
        [_run(10, 0.0), _run(9, 0.2)], win=1.0) is False


# C3：删掉 `ds < 0`（读数不许倒退）分支
def test_reading_going_up_is_a_rate_violation():
    """后一段读数比前一段大：必须按违约拒绝，不能当做正常。"""
    assert countdown._rate_violation(
        [_run(10, 0.0), _run(11, 0.5)], win=1.0) is True


def test_backward_branch_is_reachable_through_observe():
    """证明「读数倒退」不是死代码：+1 的跳变不会触发 reset。

    `reset_jump_s` 默认 20 秒，读数只涨 1 时缓冲区不清空，两段稳定读数
    相邻进入 `_runs_of`，ds = -1 直达倒退分支。
    """
    est = countdown.T0Estimator()
    for i in range(5):
        est.observe(10, 100.0 + i * 0.01)
    for i in range(5):
        est.observe(11, 101.0 + i * 0.01)

    runs = countdown._runs_of(list(est._buf))
    assert len(runs) == 2, f"缓冲区被提前重置了：{runs}"
    assert runs[0]["s"] == 10 and runs[1]["s"] == 11
    assert countdown._rate_violation(runs, est.window) is True

    got = est.estimate()
    assert got.trust is False
    # 拒绝原因可能是 wide / too_few；关键是速率分支已命中（上一行断言）


def test_rate_violation_two_ticks_through_observe_is_untrusted():
    est = countdown.T0Estimator()
    for i in range(10):
        est.observe(12, 100.0 + i * 0.05)
    for i in range(10):
        est.observe(10, 100.6 + i * 0.05)
    runs = countdown._runs_of(list(est._buf))
    assert countdown._rate_violation(runs, est.window) is True
    assert est.estimate().trust is False


# P2：`half_width <= 0.010` -> `<= 0.020`
@pytest.mark.parametrize("hw,ok", [
    (0.009, True),
    (0.010, True),
    (0.011, False),
    (0.020, False),
])
def test_gate_half_width_boundary_at_ten_ms(hw, ok):
    e = countdown.Estimate(trust=True, half_width=hw, n_inliers=120, span=1.2)
    got, _ = countdown.gate(e)
    assert got is ok, f"half_width={hw} 的闸门判定错了"

    e2 = countdown.Estimate(trust=True, half_width=hw, n_inliers=100, span=1.0)
    assert e2.admissible is ok, f"admissible 与 gate 口径不一致：{hw}"


# ============================================================ 输入通道选择

# I4：`_resolve_fallback` 非法值兜底 `legacy` -> `message`
def test_unknown_fallback_policy_falls_back_to_fail():
    """未知策略默认 fail —— 不再默认指向 legacy（借系统光标）。"""
    assert _resolve_fallback("bogus") == "fail"
    assert _resolve_fallback(" MESSAGE ") == "message"
    assert _resolve_fallback("fail") == "fail"


def test_unknown_config_fallback_falls_back_to_fail(monkeypatch):
    monkeypatch.setattr(input_mod, "_config_value",
                        lambda key, default: "bogus" if key == "input_fallback" else default)
    assert _resolve_fallback(None) == "fail"


# I1：降级策略被固定成 legacy
def test_fallback_policy_message_is_honoured(monkeypatch):
    monkeypatch.setattr(input_mod, "_driver_probe",
                        lambda: (False, "驱动没装", R_UIPI_BLOCKED))
    cap = {"usable": True, "functional": "unknown", "reason_code": None, "detail": ""}
    out = _decide_channel("interception", fallback="message", message_cap_fn=lambda: cap)
    assert out["effective"] == "message", "input_fallback=message 必须降级到 message"
    assert out["policy"] == "message"
    assert out["reason_code"] == R_UIPI_BLOCKED, "降级原因码必须是原通道的原因"


def test_fallback_priority_legacy_only_when_configured(monkeypatch):
    """legacy 只能显式打开：input_fallback=legacy 还要 input_legacy_borrow=true。"""
    monkeypatch.setattr(input_mod, "_driver_probe",
                        lambda: (False, "驱动没装", R_UIPI_BLOCKED))
    # 没开 input_legacy_borrow：拒绝借系统光标
    with pytest.raises(InputChannelError):
        _decide_channel("interception", fallback="legacy")
    # 显式打开后才可达
    monkeypatch.setattr(input_mod, "_legacy_allowed", lambda: True)
    out = _decide_channel("interception", fallback="legacy")
    assert out["effective"] == "legacy"


# I3：降级目标不可用时把原因码吃掉（统一变成驱动不可用）
def test_degrade_to_unusable_message_keeps_message_reason(monkeypatch):
    monkeypatch.setattr(input_mod, "_driver_probe",
                        lambda: (False, "驱动没装", R_UIPI_BLOCKED))
    cap = {"usable": False, "reason_code": R_UIPI_BLOCKED,
           "detail": "UIPI 拦截", "functional": "unknown"}
    with pytest.raises(InputChannelError) as ei:
        _decide_channel("interception", fallback="message", message_cap_fn=lambda: cap)
    assert ei.value.reason_code == R_UIPI_BLOCKED, "必须回传 message 通道自己的原因码"


# I5：删掉「降级目标 == 请求通道」的短路
def test_degrade_to_same_channel_is_disabled_not_silently_kept(monkeypatch):
    monkeypatch.setattr(input_mod, "_config_value",
                        lambda key, default: True if key == "input_message_strict" else default)
    cap = {"usable": True, "functional": "unsupported",
           "reason_code": None, "detail": "UnrealWindow 读真实光标"}
    with pytest.raises(InputChannelError) as ei:
        _decide_channel("message", fallback="message", message_cap_fn=lambda: cap)
    assert ei.value.reason_code == R_FALLBACK_DISABLED


# ============================================================ 异常分级

def _guard(scene, level=0):
    g = sg.SceneGuard()
    g._owner = scene
    g._level = level
    g._state = sg.STATE_RECOVERING
    g._fails = 0
    g._late = False
    return g


# S2：`_level < LEVEL_RELOCATE` -> `<=`
def test_escalate_from_relocate_goes_to_human_not_next_level():
    """已经到「回到安全页面」这一档，再升级必须转人工，不许再自动升一级。"""
    g = _guard(sg.SC_LIST_STALL, level=sg.LEVEL_RELOCATE)
    g._escalate(1.0)
    assert g.state == sg.STATE_WAITING_HUMAN, g.state
    assert g.level == sg.LEVEL_GUIDE


# S4：`max_level >= LEVEL_GUIDE` -> `>`
def test_guide_scene_with_max_level_guide_waits_for_human():
    """max_level 正好是 4 的场景必须停等人工，不是硬停。"""
    g = _guard(sg.SC_LIST_STALL, level=sg.LEVEL_RELOCATE)
    g._to_guide_or_halt(1.0, "自动手段用完")
    assert g.state == sg.STATE_WAITING_HUMAN, g.state
    assert g.level == sg.LEVEL_GUIDE
    assert g._halt_latch is False, "max_level=4 不该触发硬停"


def test_halt_on_exhausted_scene_still_hard_halts():
    """对照：halt_on_exhausted=True 的场景仍走硬停。"""
    scene = next(k for k, v in sg.SCENES.items() if v.halt_on_exhausted)
    g = _guard(scene, level=sg.LEVEL_RELOCATE)
    g._to_guide_or_halt(1.0, "自动手段用完")
    assert g.state == sg.STATE_HALTED
    assert g._halt_latch is True


# S5：`LEVEL_TEXT` 文案篡改
def test_level_text_is_exact():
    assert sg.LEVEL_TEXT == {
        sg.LEVEL_NORMAL: "正常",
        sg.LEVEL_RETRY: "再试一次",
        sg.LEVEL_FIX: "换个方式",
        sg.LEVEL_RELOCATE: "回到安全页面",
        sg.LEVEL_GUIDE: "需要你",
        sg.LEVEL_HALT: "已停止",
    }
