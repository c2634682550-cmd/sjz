"""决策层：Thompson 采样选下一批实验，先过硬约束护栏，否决理由落盘。"""
import random

from . import guardrails
from .common import clamp


def _posterior(idx, hyp):
    slot = idx.get(hyp["signature"]) or {}
    alpha = 1 + int(slot.get("successes", 0))
    beta = 1 + int(slot.get("failures", 0))
    return alpha, beta


def select(cfg, hyps, idx, funnel, run_id, veto_log=None):
    rng = random.Random(abs(hash(run_id)) % (2 ** 31))
    m = funnel["window"]["metrics"]
    base_skew = m.get("skew_p95_ms") or 0.0
    scored, vetoed = [], []
    for h in hyps:
        ctx = {
            "baseline_skew_p95_ms": base_skew,
            "projected_skew_p95_ms": (base_skew * 0.9
                                      if h["metric"] == "skew_p95_ms"
                                      else base_skew),
            "budget_cap": cfg["guardrails"]["budget_cap"],
            "quota_cap": cfg["guardrails"]["quota_cap"],
        }
        g = guardrails.evaluate(cfg, h, ctx)
        if g["vetoed"]:
            vetoed.append({"hypothesis_id": h["id"], "title": h["title"],
                           "signature": h["signature"], "vetoes": g["vetoes"]})
            continue
        alpha, beta = _posterior(idx, h)
        theta = rng.betavariate(alpha, beta)
        gain = clamp(float(h["expected_gain"]["abs_delta"]) * 20.0, 0.01, 5.0)
        score = theta * gain
        scored.append({"hypothesis": h, "theta": round(theta, 5),
                       "alpha": alpha, "beta": beta,
                       "score": round(score, 5),
                       "guardrails": g})

    scored.sort(key=lambda x: x["score"], reverse=True)
    batch = scored[:int(cfg["decision"]["batch_size"])]
    if veto_log is not None:
        veto_log.extend(vetoed)
    return {
        "selected": [s["hypothesis"] for s in batch],
        "batch_detail": [{k: v for k, v in s.items() if k != "hypothesis"}
                         for s in batch],
        "vetoed": vetoed,
        "ranked": [s["hypothesis"]["id"] for s in scored],
        "method": "thompson_sampling + hard_guardrails",
        "batch_size": int(cfg["decision"]["batch_size"]),
    }
