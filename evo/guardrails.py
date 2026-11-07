"""决策护栏：硬约束，任一破即否决，理由落盘到本轮 vetoes.jsonl。

G1 不误买          —— 实验动作里出现真实下单/支付 → 否决
G2 不越预算与额度  —— 参数目标值超过预算/额度上限 → 否决
G3 输入身份不降级  —— 把真实设备输入降级成虚拟/消息输入 → 否决
G4 卡点误差不变差  —— 投影 P95 高于基线 → 否决
G5 不抢前台        —— 需要抢占前台的动作 → 否决
"""
import time

PURCHASE_ACTIONS = ("submit_order", "buy", "confirm_purchase", "real_click")
RISKY_INPUT = ("virtual", "message", "legacy", "软件模拟")


def evaluate(cfg, hyp, ctx):
    g = cfg["guardrails"]
    vetoes = []
    actions = list(hyp.get("actions") or [])
    param = hyp.get("param") or {}
    name = str(param.get("name") or "")
    to = param.get("to")

    if not g.get("allow_real_purchase"):
        bad = [a for a in actions if a in PURCHASE_ACTIONS]
        if bad:
            vetoes.append(_v("G1_NO_PURCHASE", "实验动作包含真实购买链路",
                             {"actions": bad}))

    caps = {
        "console.budget": g.get("budget_cap"),
        "budget": g.get("budget_cap"),
        "console.quota": g.get("quota_cap"),
        "quota": g.get("quota_cap"),
    }
    if name in caps and isinstance(to, (int, float)):
        cap = caps[name]
        if cap is not None and to > cap:
            vetoes.append(_v("G2_BUDGET_QUOTA", "参数目标值越过预算/额度上限",
                             {"param": name, "to": to, "cap": cap}))

    if not g.get("allow_input_downgrade"):
        if "input" in name.lower() and isinstance(to, str) and to in RISKY_INPUT:
            vetoes.append(_v("G3_INPUT_IDENTITY",
                             "输入身份从真实设备降级为虚拟/消息输入",
                             {"param": name, "to": to}))
        if "input_allow_virtual" in name and to is True:
            vetoes.append(_v("G3_INPUT_IDENTITY", "打开虚拟输入开关",
                             {"param": name, "to": to}))

    proj = ctx.get("projected_skew_p95_ms")
    base = ctx.get("baseline_skew_p95_ms")
    if isinstance(proj, (int, float)) and isinstance(base, (int, float)):
        if proj > base + 0.5:
            vetoes.append(_v("G4_SKEW_P95", "卡点误差 P95 变差",
                             {"projected_p95": proj, "baseline_p95": base}))

    flags = set(hyp.get("risk_flags") or []) | set(actions)
    if not g.get("allow_foreground"):
        if "foreground" in flags or "focus_window" in flags:
            vetoes.append(_v("G5_FOREGROUND", "实验需要抢占前台窗口",
                             {"flags": sorted(flags)}))

    return {"vetoed": bool(vetoes), "vetoes": vetoes,
            "checked_at": round(time.time(), 3)}


def _v(code, reason, evidence):
    return {"code": code, "reason": reason, "evidence": evidence}
