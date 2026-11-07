"""假设层：每轮最多 5 条可执行假设，分参数级 / 策略级 / 时序级。

硬性要求：可证伪的预期方向、最小样本量、停止条件、预期收益 —— 缺一项即丢弃。
同签名（同类改动 + 同指标方向）在冷静期内不重复尝试（由登记册提供拦截）。
"""
import math

from .common import abs_path, clamp, load_json, safe_ratio, sig16

VALID_CLASSES = ("参数级", "策略级", "时序级")
REQUIRED = ("class", "factor", "title", "param", "metric", "metric_stage",
            "expected_direction", "min_sample", "stop_condition",
            "expected_gain", "signature")


def _z(alpha=0.05, power=0.8):
    return 1.959964, 0.841621


def two_prop_n(p1, p2, floor=30):
    """两比例检验的近似最小样本量（双侧 alpha=0.05, power=0.8）。"""
    p1 = clamp(float(p1 or 0.0), 0.01, 0.95)
    p2 = clamp(float(p2 or 0.0), 0.01, 0.99)
    if p2 <= p1:
        return int(floor)
    z_a, z_b = _z()
    p = (p1 + p2) / 2.0
    num = (z_a * math.sqrt(2 * p * (1 - p))
           + z_b * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    n = num / ((p2 - p1) ** 2)
    return max(int(floor), int(math.ceil(n)))


def _current_params():
    cfg = load_json(abs_path("config.json"), {}) or {}

    def get(dotted, default=None):
        cur = cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur
    return get


def _templates():
    """factor -> 模板列表。effect_scale 决定回放里可实现的改进上限。"""
    return {
        "refresh_exhaust": [
            {"class": "策略级", "title": "提高列表刷新预算，减少「来不及」流失",
             "param": "list_max_tries", "to_of": None, "to": None,
             "to_expr": "plus", "to_value": 8, "unit": "次",
             "metric": "deal_rate", "metric_stage": "discover→click",
             "delta": 0.04, "effect_scale": 0.45, "param_level": False},
        ],
        "quota_block": [
            {"class": "策略级", "title": "开抢前复核额度与余额，避免无效点击",
             "param": "evo.policy.requota", "from": False, "to": True,
             "unit": "bool", "metric": "deal_rate", "metric_stage": "click→pool",
             "delta": 0.05, "effect_scale": 0.50, "param_level": False},
            {"class": "参数级", "title": "止损失败次数收紧，额度异常时快速退出",
             "param": "stop_after_fails", "from": 0, "to": 2, "unit": "次",
             "metric": "block_rate", "metric_stage": "click→pool",
             "delta": 0.03, "direction": "down", "effect_scale": 0.3,
             "param_level": True},
        ],
        "ui_detect": [
            {"class": "策略级", "title": "场景守卫二次校验，压掉「弹窗没出现」",
             "param": "evo.policy.double_check", "from": False, "to": True,
             "unit": "bool", "metric": "hit_rate", "metric_stage": "pool→hit",
             "delta": 0.05, "effect_scale": 0.50, "param_level": False},
            {"class": "参数级", "title": "点击 burst 时长略增，覆盖渲染抖动",
             "param": "click.burst.duration_ms", "from": 100, "to": 140,
             "unit": "ms", "metric": "hit_rate", "metric_stage": "pool→hit",
             "delta": 0.04, "effect_scale": 0.40, "param_level": True},
        ],
        "queue_delay": [
            {"class": "时序级", "title": "提前进入倒计时等待，摊薄队列延迟",
             "param": "countdown_min_lead_s", "from": 6, "to": 7, "unit": "s",
             "metric": "hit_rate", "metric_stage": "pool→hit",
             "delta": 0.04, "effect_scale": 0.35, "param_level": False},
        ],
        "timing_skew": [
            {"class": "时序级", "title": "按卡点误差中位数做相位补偿",
             "param": "fire_delay_ms", "from": 0, "to": 30, "unit": "ms",
             "metric": "skew_p95_ms", "metric_stage": "pool→hit",
             "delta": 0.05, "direction": "down", "effect_scale": 0.55,
             "param_level": False},
        ],
        "settle_verify": [
            {"class": "策略级", "title": "成交记账前强制结算校验，杜绝未验证成交",
             "param": "evo.policy.settle_gate", "from": False, "to": True,
             "unit": "bool", "metric": "deal_rate", "metric_stage": "hit→deal",
             "delta": 0.10, "effect_scale": 0.60, "param_level": False},
        ],
    }


def generate(cfg, attribution, funnel, blocked_signature=None, round_no=1,
             registry_index=None):
    """返回 (hypotheses, dropped, skipped_duplicates)。"""
    get = _current_params()
    hyps, dropped, dups = [], [], []
    candidates = attribution.get("candidates") or []
    m = funnel["window"]["metrics"]
    baseline_metrics = {
        "hit_rate": m.get("hit_rate") or 0.0,
        "deal_rate": m.get("end_to_end_rate") or 0.0,
        "block_rate": m.get("block_rate") or 0.0,
        "skew_p95_ms": m.get("skew_p95_ms") or 0.0,
    }
    floor = int(cfg["hypothesis"]["min_sample_floor"])
    tmpl_map = _templates()

    for cand in candidates:
        if float(cand.get("effect") or 0) <= 0.005:
            continue
        for t in tmpl_map.get(cand["factor"], []):
            h = _materialize(t, cand, baseline_metrics, get, floor, round_no)
            if h is None:
                dropped.append({"factor": cand["factor"],
                                "reason": "模板字段不完整",
                                "template": t.get("title")})
                continue
            missing = [k for k in REQUIRED if h.get(k) in (None, "", {},
                                                             [])]
            if missing:
                dropped.append({"factor": cand["factor"],
                                "reason": "缺少必填项: " + ",".join(missing),
                                "hypothesis": h.get("title")})
                continue
            if h["class"] not in VALID_CLASSES:
                dropped.append({"factor": cand["factor"],
                                "reason": "未知类别",
                                "hypothesis": h.get("title")})
                continue
            if h["min_sample"] < floor:
                dropped.append({"factor": cand["factor"],
                                "reason": "最小样本量低于下限",
                                "hypothesis": h.get("title")})
                continue
            if blocked_signature and blocked_signature(h["signature"]):
                dups.append({"signature": h["signature"],
                             "title": h["title"], "reason": "同类失败冷静期内"})
                continue
            if h["expected_gain"].get("abs_delta", 0) <= 0:
                dropped.append({"factor": cand["factor"],
                                "reason": "预期收益非正",
                                "hypothesis": h.get("title")})
                continue
            hyps.append(h)

    hyps.sort(key=lambda x: x["expected_gain"]["abs_delta"], reverse=True)
    hyps = hyps[:int(cfg["hypothesis"]["max_per_round"])]
    return {"hypotheses": hyps, "dropped": dropped, "duplicates": dups,
            "generated_at": None}


def _materialize(t, cand, base, get, floor, round_no):
    direction = t.get("direction", "up")
    if t.get("to_expr") == "plus":
        cur = get(t["param"], 0) or 0
        to = cur + t.get("to_value", 0)
        frm = cur
    else:
        frm = t.get("from", get(t["param"], None))
        to = t.get("to", None)
    if to is None:
        return None

    metric = t["metric"]
    p1 = base.get(metric) or 0.0
    if metric == "skew_p95_ms":
        p1c = 0.5
    elif metric == "block_rate":
        p1c = clamp(p1, 0.01, 0.9)
    else:
        p1c = clamp(p1, 0.01, 0.9)
    delta = float(t["delta"])
    p2c = clamp(p1c + (delta if direction == "up" else -delta), 0.01, 0.98)
    n = two_prop_n(p1c, p2c, floor)

    sig = sig16(t["class"], t["param"], frm, to, metric, direction)
    return {
        "id": f"H-{sig}-{round_no}",
        "round": round_no,
        "class": t["class"],
        "param_level": bool(t.get("param_level")),
        "factor": cand["factor"],
        "title": t["title"],
        "param": {"name": t["param"], "from": frm, "to": to,
                  "unit": t.get("unit", "")},
        "change": f"{t['param']}: {frm} -> {to}",
        "metric": metric,
        "metric_stage": t.get("metric_stage", ""),
        "expected_direction": {"metric": metric, "direction": direction,
                               "min_delta": delta},
        "min_sample": n,
        "stop_condition": {
            "max_rounds": 3,
            "max_seconds": 86400,
            "futility_bound": {"min_effect": max(0.005, delta / 4.0)},
            "abort_on_guardrail_veto": True,
        },
        "expected_gain": {
            "metric": metric,
            "abs_delta": delta,
            "per_100_opportunities": round(delta * 100.0, 2),
            "basis": cand.get("support", ""),
        },
        "evidence": {"factor": cand["factor"], "value": cand["effect"],
                     "n": cand["evidence_n"],
                     "confidence": cand.get("confidence")},
        "effect_scale": float(t.get("effect_scale", 0.4)),
        "actions": ["offline_replay", "shadow_read_only"],
        "risk_flags": [],
        "signature": sig,
        "status": "proposed",
    }
