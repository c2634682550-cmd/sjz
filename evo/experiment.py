"""实验层：离线回放 -> 影子模式 -> 小样本真机，三级递进，逐级过闸。

离线：用 state/replay.jsonl 的历史行 + 日聚合重跑，零成本；给出的是模型
      估计而非实测，通过只代表「方向可验证」。
影子：只读真实帧目录（data/<8位hex>/buy.png、confirm.png），不点击、
      不占前台；测决策覆盖率与读取延迟。
真机：只产出执行计划并硬禁用，必须人工注入执行器且显式开关；
      本框架默认不接入真实购买链路。
"""
import math
import os
import re
import time

from .common import abs_path, clamp, percentile, safe_ratio

CAPS = {
    "timing_skew": 0.10,
    "ui_detect": 0.10,
    "queue_delay": 0.08,
    "quota_block": 0.12,
    "refresh_exhaust": 0.10,
    "settle_verify": 0.20,
}


def _p1(cfg, hyp, funnel):
    m = funnel["window"]["metrics"]
    metric = hyp["metric"]
    if metric == "skew_p95_ms":
        return clamp((m.get("skew_p95_ms") or 0.0) / 5000.0, 0.01, 0.9)
    if metric == "block_rate":
        return clamp(m.get("block_rate") or 0.0, 0.01, 0.9)
    if metric == "hit_rate":
        return clamp(m.get("hit_rate") or 0.0, 0.01, 0.9)
    return clamp(m.get("end_to_end_rate") or 0.0, 0.01, 0.9)


def _observed(cfg, hyp, funnel):
    m = funnel["window"]["metrics"]
    metric = hyp["metric"]
    if metric == "skew_p95_ms":
        return m.get("skew_p95_ms")
    if metric == "block_rate":
        return m.get("block_rate")
    if metric == "hit_rate":
        return m.get("hit_rate")
    return m.get("end_to_end_rate")


def offline_replay(cfg, hyp, funnel, replay_rows, guard_ctx):
    n = len(replay_rows)
    p1 = _p1(cfg, hyp, funnel)
    cap = CAPS.get(hyp["factor"], 0.05)
    delta = round(min(cap, cap * (0.55 + 0.45 * float(
        hyp.get("effect_scale") or 0.4))), 4)
    direction = hyp["expected_direction"]["direction"]
    min_n = int(cfg["hypothesis"]["offline_min_rows"])
    vetoed = bool(guard_ctx.get("vetoed"))

    if hyp["metric"] == "skew_p95_ms":
        base_ms = funnel["window"]["metrics"].get("skew_p95_ms") or 0.0
        proj_ms = round(base_ms * (1.0 - delta), 1)
        gates = [
            _gate("replay_rows", n >= min_n, f"历史行 {n} / 要求 >= {min_n}"),
            _gate("power_n", n >= int(hyp["min_sample"]),
                  f"{n} / 计划样本 {hyp['min_sample']}"),
            _gate("direction", direction == "down", f"方向 {direction}"),
            _gate("min_delta",
                  delta >= float(hyp["expected_direction"]["min_delta"]),
                  f"{delta:.4f} / 要求 >= "
                  f"{hyp['expected_direction']['min_delta']}"),
            _gate("guardrails", not vetoed,
                  "护栏全过" if not vetoed else "存在否决项"),
        ]
        ok = all(g["ok"] for g in gates)
        return {
            "level": "offline", "status": "passed" if ok else "failed",
            "sample": {"n": n}, "metric": "skew_p95_ms",
            "before": round(base_ms, 1), "after": proj_ms,
            "observed_before": round(base_ms, 1), "delta": delta,
            "ci95": [None, None], "gates": gates,
            "failure_reason": "" if ok else "gate_failed:" + ",".join(
                g["name"] for g in gates if not g["ok"]),
            "projected_skew_p95_ms": proj_ms, "cost": 0,
        }

    p2 = clamp(p1 + (delta if direction == "up" else -delta), 0.005, 0.995)
    se = math.sqrt(p1 * (1 - p1) / max(1, n) + p2 * (1 - p2) / max(1, n))
    ci_low = delta - 1.96 * se
    ci_high = delta + 1.96 * se

    gates = []
    gates.append(_gate("replay_rows", n >= min_n,
                       f"历史行 {n} / 要求 >= {min_n}"))
    gates.append(_gate("power_n", n >= int(hyp["min_sample"]),
                       f"{n} / 计划样本 {hyp['min_sample']}"))
    gates.append(_gate("direction", delta > 0,
                       f"估计增益 {delta:+.4f}（{direction}）"))
    gates.append(_gate("min_delta",
                       delta >= float(hyp["expected_direction"]["min_delta"]),
                       f"{delta:.4f} / 要求 >= "
                       f"{hyp['expected_direction']['min_delta']}"))
    gates.append(_gate("ci_low_positive", ci_low > 0,
                       f"95% 下界 {ci_low:+.4f}"))
    gates.append(_gate("guardrails", not vetoed,
                       "护栏全过" if not vetoed else "存在否决项"))

    status = "passed" if all(g["ok"] for g in gates) else "failed"
    reason = ""
    if not all(g["ok"] for g in gates):
        bad = [g["name"] for g in gates if not g["ok"]]
        reason = "gate_failed:" + ",".join(bad)
    return {
        "level": "offline",
        "status": status,
        "sample": {"n": n},
        "metric": hyp["metric"],
        "before": round(p1, 4),
        "observed_before": round(float(_observed(cfg, hyp, funnel) or 0.0), 4),
        "after": round(p2, 4),
        "delta": delta,
        "ci95": [round(ci_low, 4), round(ci_high, 4)],
        "gates": gates,
        "failure_reason": reason,
        "projected_skew_p95_ms": (
            funnel["window"]["metrics"].get("skew_p95_ms") or 0.0),
        "cost": 0,
    }


def shadow_run(cfg, hyp, funnel):
    root = abs_path(cfg.get("_shadow_dir") or "data")
    sessions = []
    try:
        for name in os.listdir(root):
            if re.fullmatch(r"[0-9a-f]{8}", name):
                d = os.path.join(root, name)
                if os.path.isdir(d):
                    sessions.append((os.path.getmtime(d), d))
    except OSError:
        sessions = []
    sessions.sort(reverse=True)
    sessions = sessions[:int(cfg["hypothesis"]["shadow_max_sessions"])]

    lat, decidable, side_effects = [], 0, 0
    for _, d in sessions:
        t0 = time.perf_counter()
        try:
            frames = [f for f in os.listdir(d)
                      if os.path.splitext(f)[1].lower() in (".png", ".jpg")]
            for f in frames[:2]:
                with open(os.path.join(d, f), "rb") as fh:
                    fh.read(4096)
            if any(f in frames for f in ("buy.png", "confirm.png")):
                decidable += 1
        except OSError:
            pass
        lat.append((time.perf_counter() - t0) * 1000.0)

    coverage = safe_ratio(decidable, len(sessions))
    p95 = percentile(lat, 0.95) or 0.0
    gates = [
        _gate("sessions", len(sessions) > 0, f"可用会话 {len(sessions)}"),
        _gate("coverage", coverage >= float(cfg["hypothesis"]["shadow_min_coverage"]),
              f"决策覆盖率 {coverage:.2%}"),
        _gate("latency_p95", p95 <= float(cfg["hypothesis"]["shadow_max_p95_ms"]),
              f"读取延迟 P95 {p95:.1f}ms"),
        _gate("side_effects", side_effects == 0, "点击/写入副作用 0 次"),
    ]
    ok = all(g["ok"] for g in gates)
    return {
        "level": "shadow",
        "status": "passed" if ok else "failed",
        "sample": {"sessions": len(sessions), "decidable": decidable},
        "coverage": round(coverage, 4),
        "latency_p95_ms": round(p95, 2),
        "side_effects": side_effects,
        "gates": gates,
        "failure_reason": "" if ok else "gate_failed:" + ",".join(
            g["name"] for g in gates if not g["ok"]),
        "started_at": round(time.time(), 3),
    }


def real_plan(cfg, hyp):
    """真机级只给计划，默认禁用；执行器必须由操作者显式注入。"""
    return {
        "level": "real",
        "status": "blocked",
        "reason": "real_machine_disabled",
        "plan": {
            "max_runs": min(5, int(hyp["min_sample"])),
            "abort_after_s": 180,
            "require_manual_enable": True,
            "forbid_foreground_steal": True,
            "forbid_purchase_chain": True,
            "success_gate": "n>=min_sample 且方向一致且护栏全过",
        },
        "executor_required": "EVO_REAL_EXECUTOR",
        "note": "未注入执行器；框架不会调用真实购买链路。",
    }


def run(cfg, hyp, funnel, replay_rows, guard_ctx):
    off = offline_replay(cfg, hyp, funnel, replay_rows, guard_ctx)
    out = {"offline": off}
    if off["status"] != "passed":
        out["shadow"] = {"level": "shadow", "status": "skipped",
                         "failure_reason": "offline_not_passed",
                         "gates": []}
        out["real"] = {"level": "real", "status": "skipped",
                       "failure_reason": "shadow_not_passed", "gates": []}
        out["final"] = {"status": "inconclusive",
                        "failure_reason": off["failure_reason"] or "offline_failed",
                        "level": "offline"}
        return out

    sh = shadow_run(cfg, hyp, funnel)
    out["shadow"] = sh
    if sh["status"] != "passed":
        out["real"] = {"level": "real", "status": "skipped",
                       "failure_reason": "shadow_not_passed", "gates": []}
        out["final"] = {"status": "inconclusive",
                        "failure_reason": sh["failure_reason"], "level": "shadow"}
        return out

    out["real"] = real_plan(cfg, hyp)
    out["final"] = {"status": "canary_candidate",
                    "failure_reason": "",
                    "level": "real_pending",
                    "note": "真机级未执行，等待人工注入执行器。"}
    return out


def _gate(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}
