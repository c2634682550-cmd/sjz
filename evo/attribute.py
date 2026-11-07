"""归因层：每轮定位当前流失最大的一段，并给出可归因因素候选（附支撑数据）。

两条纪律：
1) 只有两端都有证据的相邻段才参与「最大流失」评选；
2) 流失里落在「入池未知」区间的部分不算已确认流失，单列 unresolved。
"""
from .common import clamp, safe_ratio

STAGE_ORDER = ["discover", "listed", "click", "pool", "hit", "deal"]
STAGE_LABEL = {"discover": "候选出现", "listed": "进入列表", "click": "点进详情",
               "pool": "入池", "hit": "中签", "deal": "成交"}

FACTOR_LABEL = {
    "refresh_exhaust": "刷新预算耗尽",
    "quota_block": "额度/预算拦截",
    "ui_detect": "界面判定失败",
    "queue_delay": "队列延迟",
    "timing_skew": "时序偏差",
    "settle_verify": "结算确认缺失",
    "pool_evidence_gap": "入池证据缺失",
    "ledger_health": "台账数据健康度",
}


def _confidence(n, cfg):
    if n >= 30:
        return "high"
    if n >= int(cfg["attribution"]["min_evidence_n"]):
        return "medium"
    return "low"


def _bottleneck(funnel):
    stages = funnel["window"].get("stages") or []
    best = nonev_best = None
    gaps, unresolved = [], []
    for i, row in enumerate(stages[:-1]):
        nxt = stages[i + 1]
        frm, to = row["stage"], nxt["stage"]
        if not row.get("measured") or not nxt.get("measured"):
            gaps.append({"from": frm, "to": to, "reason": "no_evidence",
                         "unknown": int(row.get("unknown", 0) or 0)})
            continue
        eff = int(row.get("effective", row.get("count", 0)) or 0)
        nxt_n = int(nxt.get("count", 0) or 0)
        lost = max(0, eff - nxt_n)
        if lost <= 0:
            continue
        unk = int(row.get("unknown", 0) or 0)
        if unk and lost <= unk:
            unresolved.append({"from": frm, "to": to, "lost": lost,
                               "unknown": unk, "reason": "loss_all_unknown"})
            continue
        rate = (lost / eff) if eff else 0.0
        # 结算段没有证据时，缺口要标 no_evidence，不能当成已确认流失。
        noev = (row.get("status") == "no_evidence"
                or nxt.get("status") == "no_evidence")
        cand = {"from": frm, "to": to, "lost": lost,
                "loss_rate": rate, "n": eff, "unknown": unk,
                "no_evidence": noev,
                "label": f"{STAGE_LABEL[frm]}→{STAGE_LABEL[to]}"}
        if noev:
            gaps.append({"from": frm, "to": to, "reason": "settle_no_evidence",
                         "lost": lost, "unknown": unk})
            if nonev_best is None or (lost, rate) > (nonev_best["lost"],
                                                    nonev_best["loss_rate"]):
                nonev_best = cand
            continue
        if best is None or (lost, rate) > (best["lost"], best["loss_rate"]):
            best = cand
    return (best or nonev_best), gaps, unresolved


def attribute(cfg, funnel, health=None):
    counts = funnel["window"]["counts"]
    m = funnel["window"]["metrics"]
    acfg = cfg["attribution"]
    candidates = []

    discover = int(counts.get("discover", 0) or 0)
    click = int(counts.get("click", 0) or 0)
    pool = int(counts.get("pool", 0) or 0)
    hit = int(counts.get("hit", 0) or 0)
    unknown = int(funnel["window"].get("pool_unknown", 0) or 0)
    eff_pool = pool + unknown

    skip_rate = safe_ratio(funnel["window"].get("skip", 0), discover)
    if discover:
        candidates.append(_cand(
            "refresh_exhaust", "discover→listed", skip_rate, discover,
            f"跳过 {funnel['window'].get('skip', 0)}/{discover}，"
            f"原因分布 {funnel['window'].get('skip_reasons') or {}}",
            acfg))

    block_rate = safe_ratio(funnel["window"].get("blocked", 0), click)
    if click:
        candidates.append(_cand(
            "quota_block", "click→pool", block_rate, click,
            f"额度/预算拦截 {funnel['window'].get('blocked', 0)}/{click}",
            acfg))

    miss_rate = 1.0 - safe_ratio(hit, eff_pool) if eff_pool else 0.0
    if eff_pool:
        candidates.append(_cand(
            "ui_detect", "pool→hit", miss_rate, eff_pool,
            f"入池证据 {pool} + 未知 {unknown}，其中未中签 "
            f"{max(0, eff_pool - hit)}（界面判定/弹窗未出现）",
            acfg))

    p95 = m.get("elapsed_p95_s")
    base_q = float(acfg["queue_p95_baseline_s"])
    if p95 is not None and eff_pool:
        eff = clamp((p95 - base_q) / base_q, 0.0, 1.0)
        candidates.append(_cand(
            "queue_delay", "pool→hit", eff, eff_pool,
            f"耗时 P95={p95:.2f}s，基线 {base_q:.2f}s，样本 "
            f"{m.get('elapsed_samples', 0)}", acfg))

    skew_p95 = m.get("skew_p95_ms")
    base_s = float(acfg["skew_p95_baseline_ms"])
    if skew_p95 is not None:
        eff = clamp((abs(skew_p95) - base_s) / base_s, 0.0, 1.0)
        candidates.append(_cand(
            "timing_skew", "pool→hit", eff, int(m.get("skew_samples", 0) or 0),
            f"卡点误差 P95={skew_p95:.1f}ms，基线 {base_s:.1f}ms", acfg))

    unverified = int(funnel["window"].get("unverified_hits", 0) or 0)
    if hit:
        candidates.append(_cand(
            "settle_verify", "hit→deal", safe_ratio(unverified, hit), hit,
            f"中签 {hit} 中 {unverified} 条未校验结算（verified 缺失/False）",
            acfg))

    if unknown and click:
        candidates.append(_cand(
            "pool_evidence_gap", "click→pool", safe_ratio(unknown, click), click,
            f"台账/旁路里 {unknown}/{click} 条无入池证据，"
            f"老口径会把 miss 一律算入池；新口径单列未知", acfg))

    hr = (health or {}).get("health_ratio")
    ar = (health or {}).get("anomaly_ratio")
    if hr is not None and (int((health or {}).get("invalid_rows", 0) or 0) > 0
                            or float(ar or 0) > 0):
        eff = max(1.0 - float(hr), float(ar or 0))
        candidates.append(_cand(
            "ledger_health", "data", eff,
            int(health.get("rows_read", 0) or 0),
            f"非法行 {health.get('invalid_rows')}，可疑行 "
            f"{health.get('suspicious_rows', 0)}，异常样例 "
            f"{[a.get('code') for a in (health.get('anomalies') or [])][:4]}", acfg))

    candidates.sort(key=lambda c: (c["score"], c["evidence_n"]), reverse=True)
    candidates = candidates[:int(acfg["top_k"])]
    bn, gaps, unresolved = _bottleneck(funnel)
    return {
        "bottleneck": bn,
        "data_gaps": gaps,
        "unresolved": unresolved,
        "candidates": candidates,
        "window_counts": counts,
        "metrics": m,
        "evidence_sufficient": bool(bn and bn["n"] >= int(cfg["funnel"]["min_stage_n"])),
        "note": ("样本不足，归因仅作方向性提示"
                 if bn and bn["n"] < int(cfg["funnel"]["min_stage_n"]) else ""),
    }


def _cand(factor, stage, effect, n, support, acfg):
    return {
        "factor": factor,
        "label": FACTOR_LABEL.get(factor, factor),
        "stage": stage,
        "effect": round(float(effect), 4),
        "evidence_n": int(n),
        "support": support,
        "confidence": _confidence(int(n), {"attribution": acfg}),
        "score": round(float(effect) * min(int(n), 50) / 50.0, 4),
    }
