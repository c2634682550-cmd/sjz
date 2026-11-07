"""漏斗层：把每次机会拆成 候选出现→进入列表→点进详情→入池→中签→成交 六段。

口径（与台账 22 列 / 遥测一一对应，写进报告）：
  discover 候选出现 = 台账行数 + 独立 skip 事件（下界；候选数本身通常无独立记录）
  listed   进入列表 = 有 listed_at / listed_at_ms 证据的行；无证据则标「无数据」
  click    点进详情 = result ∈ {hit, miss, no_money, over_budget}（真的发起了抢）
  pool     入池     = join_result=joined / pool_entered=true / 点数或中签记录 / result=hit
                      老台账没有上述证据的 miss 单列 pool_unknown，不默认算入池
  hit      中签     = result = hit（won=true 与 result=hit 冲突时只记 conflict，不改判）
  deal     成交     = hit 且 verified = True（校验过结算弹窗/余额）

按段输出 unknown / measured / claimed，缺数据的环节不给估算值。
"""
from .common import percentile, safe_ratio, wilson_lower
from . import pool_contract
from core import settle_verify

STAGES = [
    ("discover", "候选出现"),
    ("listed", "进入列表"),
    ("click", "点进详情"),
    ("pool", "入池"),
    ("hit", "中签"),
    ("deal", "成交"),
]

STAGE_KEYS = [k for k, _ in STAGES]


def _empty_counts():
    return {k: 0 for k in STAGE_KEYS}


def _default_measured(counts):
    return {
        "discover": True,
        "listed": bool(counts.get("listed", 0)),
        "click": True,
        "pool": True,
        "hit": True,
        "deal": True,
    }


def _stage_status(stage, meas, unk, settle_ev):
    """单段数据状态：measured / partial / no_evidence。

    deal 段特别处理：没有任何一单拿到结算证据时是 no_evidence，
    不能把「不知道」显示成「0% 成交」。
    """
    if stage == "deal":
        if settle_ev <= 0:
            return "no_evidence"
        return "partial" if unk else "measured"
    if not meas:
        return "no_evidence"
    if stage == "pool" and unk:
        return "partial"
    return "measured"


def _stage_rows(counts, measured=None, unknown=None, claimed=None, settle=None):
    measured = dict(measured or _default_measured(counts))
    unknown = dict(unknown or {})
    claimed = dict(claimed or {})
    settle = settle or {}
    ev_n = int(settle.get("evidence_n", 0) or 0)
    settle_unk = int(settle.get("unknown_n", 0) or 0)
    settle_lost = int(settle.get("lost_n", 0) or 0)
    labels = dict(STAGES)
    rows = []
    statuses = {}
    for k in STAGE_KEYS:
        unk = settle_unk if k == "deal" else int(unknown.get(k, 0) or 0)
        statuses[k] = _stage_status(k, bool(measured.get(k, True)), unk, ev_n)
    for i, k in enumerate(STAGE_KEYS):
        n = int(counts.get(k, 0) or 0)
        unk = settle_unk if k == "deal" else int(unknown.get(k, 0) or 0)
        eff = n + unk
        meas = bool(measured.get(k, True))
        status = statuses[k]
        nxt = int(counts.get(STAGE_KEYS[i + 1], 0) or 0) if i + 1 < len(STAGE_KEYS) else None
        step = loss = lost_n = None
        step_status = "no_evidence"
        if i + 1 < len(STAGE_KEYS):
            nk = STAGE_KEYS[i + 1]
            if (meas and measured.get(nk, True) and eff > 0
                    and status != "no_evidence" and statuses.get(nk) != "no_evidence"):
                step = safe_ratio(nxt, eff)
                loss = 1.0 - step
                lost_n = max(0, eff - nxt)
                step_status = "measured"
        row = {
            "stage": k,
            "label": labels[k],
            "count": n,
            "unknown": unk,
            "effective": eff,
            "measured": meas,
            "status": status,
            "no_evidence": status == "no_evidence",
            "claimed": int(claimed.get(k, 0) or 0),
            "rate_of_discover": (safe_ratio(eff, counts.get("discover", 0))
                                 if meas and status != "no_evidence" else None),
            "step_rate": step,
            "step_status": step_status,
            "loss_count": lost_n,
            "loss_rate": loss,
            "wilson_low": (wilson_lower(eff, counts.get("discover", 0))
                           if meas and status != "no_evidence" else None),
        }
        if k == "deal":
            row["evidence_n"] = ev_n
            row["settle_breakdown"] = {"won": n, "lost": settle_lost,
                                       "unknown": settle_unk}
        rows.append(row)
    return rows


def _gap_s(ms_list):
    """单调时钟毫秒差 -> 秒；无样本返回 None（不是 0）。"""
    p50 = percentile(ms_list, 0.5)
    p95 = percentile(ms_list, 0.95)
    return {
        "p50_s": None if p50 is None else round(p50 / 1000.0, 3),
        "p95_s": None if p95 is None else round(p95 / 1000.0, 3),
        "samples": len(ms_list),
        "status": "measured" if ms_list else "no_evidence",
    }


def _metrics(counts, blocked, skip, unverified, elapsed, skew, unknown=0,
             measured=None, conflicts=0, settle=None, gaps=None):
    measured = dict(measured or _default_measured(counts))
    settle = settle or {}
    if "evidence_n" in settle:
        ev_n = int(settle.get("evidence_n") or 0)
    else:
        ev_n = 1 if counts.get("deal", 0) else 0
    settle_unk = int(settle.get("unknown_n") or 0)
    settle_lost = int(settle.get("lost_n") or 0)
    has_settle = ev_n > 0
    eff_pool = int(counts.get("pool", 0)) + int(unknown)
    gaps = gaps or {}
    dl = _gap_s(gaps.get("discover_listed") or [])
    lc = _gap_s(gaps.get("listed_click") or [])
    return {
        "list_rate": (safe_ratio(counts.get("listed", 0), counts.get("discover", 0))
                      if measured.get("listed") else None),
        "detail_rate": (safe_ratio(counts.get("click", 0), counts.get("listed", 0))
                        if measured.get("listed") else None),
        "click_rate": safe_ratio(counts["click"], counts["discover"]),
        "pool_rate": safe_ratio(counts["pool"], counts["click"]),
        "pool_rate_upper": safe_ratio(eff_pool, counts["click"]),
        "pool_unknown_rate": safe_ratio(unknown, counts["click"]),
        "pool_unknown_count": int(unknown),
        "phantom_pool_count": int(unknown),
        "hit_rate": safe_ratio(counts["hit"], eff_pool),
        "claimed_hit_rate": safe_ratio(counts["hit"], counts["click"]),
        # 结算段：没有证据就是 None（不是 0.00%）。
        "settle_rate": (safe_ratio(counts["deal"], counts["hit"])
                        if has_settle else None),
        "end_to_end_rate": (safe_ratio(counts["deal"], counts["discover"])
                            if has_settle else None),
        "settle_state": "measured" if has_settle else "no_evidence",
        "settle_evidence_n": ev_n,
        "settle_unknown_n": settle_unk,
        "settle_lost_n": settle_lost,
        "settle_unknown_rate": safe_ratio(settle_unk, counts["hit"]),
        "block_rate": safe_ratio(blocked, counts["click"]),
        "refresh_skip_rate": safe_ratio(skip, counts["discover"]),
        "unverified_hit_rate": safe_ratio(unverified, counts["hit"]),
        "won_conflict_count": int(conflicts),
        "elapsed_p50_s": percentile(elapsed, 0.5),
        "elapsed_p95_s": percentile(elapsed, 0.95),
        "skew_p95_ms": percentile(skew, 0.95),
        "skew_samples": len(skew),
        "elapsed_samples": len(elapsed),
        # 两个时间戳补齐的两段时长
        "discover_to_listed": dl,
        "listed_to_click": lc,
        "discover_to_listed_p50_s": dl["p50_s"],
        "discover_to_listed_p95_s": dl["p95_s"],
        "discover_to_listed_samples": dl["samples"],
        "listed_to_click_p50_s": lc["p50_s"],
        "listed_to_click_p95_s": lc["p95_s"],
        "listed_to_click_samples": lc["samples"],
    }


def _sum_window(rollup, days):
    counts = _empty_counts()
    blocked = skip = unverified = unknown = conflicts = 0
    deal_evidence = settle_unknown = settle_lost = 0
    listed_measured = False
    first_seen_measured = False
    elapsed, skew = [], []
    gap_dl, gap_lc = [], []
    by_mode = {}
    skip_reasons = {}
    per_day = {}
    for d in days:
        day = rollup.get("days", {}).get(d) or {}
        dc = day.get("counts") or {}
        for k in counts:
            counts[k] += int(dc.get(k, 0) or 0)
        blocked += int(day.get("blocked", 0) or 0)
        skip += int(day.get("skip", 0) or 0)
        unverified += int(day.get("unverified_hits", 0) or 0)
        unknown += int(day.get("pool_unknown", 0) or 0)
        conflicts += int(day.get("won_conflicts", 0) or 0)
        deal_evidence += int(day.get("deal_evidence", 0) or 0)
        settle_unknown += int(day.get("settle_unknown_hits", 0) or 0)
        settle_lost += int(day.get("settle_lost_hits", 0) or 0)
        listed_measured = listed_measured or bool(day.get("listed_measured"))
        first_seen_measured = (first_seen_measured
                               or bool(day.get("first_seen_measured")))
        elapsed.extend(day.get("elapsed") or [])
        skew.extend(day.get("skew") or [])
        gap_dl.extend(day.get("gap_discover_listed") or [])
        gap_lc.extend(day.get("gap_listed_click") or [])
        for reason, n in (day.get("skip_reasons") or {}).items():
            skip_reasons[reason] = skip_reasons.get(reason, 0) + int(n or 0)
        for mode, md in (day.get("by_mode") or {}).items():
            mc = md.get("counts") or {}
            slot = by_mode.setdefault(mode, _empty_counts())
            for k in counts:
                slot[k] += int(mc.get(k, 0) or 0)
        per_day[d] = {"counts": dict(dc), "blocked": day.get("blocked", 0),
                      "skip": day.get("skip", 0)}
    measured = {"discover": True, "listed": listed_measured or bool(counts["listed"]),
                "click": True, "pool": True, "hit": True, "deal": True}
    settle = {"evidence_n": deal_evidence, "unknown_n": settle_unknown,
              "lost_n": settle_lost}
    gaps = {"discover_listed": gap_dl, "listed_click": gap_lc}
    return {
        "counts": counts,
        "blocked": blocked,
        "skip": skip,
        "unverified_hits": unverified,
        "pool_unknown": unknown,
        "won_conflicts": conflicts,
        "settle": settle,
        "time_marks": {
            "first_seen_measured": first_seen_measured,
            "listed_measured": listed_measured,
        },
        "by_mode": by_mode,
        "skip_reasons": skip_reasons,
        "per_day": per_day,
        "measured": measured,
        "metrics": _metrics(counts, blocked, skip, unverified, elapsed, skew,
                            unknown=unknown, measured=measured, conflicts=conflicts,
                            settle=settle, gaps=gaps),
    }


def window_stats(cfg, rollup, today=None, days=None):
    n = days or int(cfg["funnel"]["window_days"])
    import datetime
    today = today or max(list(rollup.get("days", {}).keys()) or ["1970-01-01"])
    t = datetime.date.fromisoformat(today)
    picked = [(t - datetime.timedelta(days=i)).isoformat() for i in range(n)]
    return _sum_window(rollup, picked)


def build(cfg, bundle, today=None):
    """返回本轮增量漏斗 + 滚动窗口漏斗 + 分段。"""
    attempts = bundle["attempts"]
    counts = _empty_counts()
    counts["discover"] = len(attempts) + len(bundle["skips"])
    blocked = skip = unverified = unknown = conflicts = 0
    deal_evidence = settle_unknown = settle_lost = 0
    elapsed, skew = [], []
    gap_dl, gap_lc = [], []
    by_mode, by_day, skip_reasons = {}, {}, {}
    conflict_samples = []
    listed_measured = False
    first_seen_measured = False
    discovery_measured = bool(bundle.get("discover_measured", True))

    def _slot(counts_slot, cls, listed):
        counts_slot["discover"] += 1
        if listed:
            counts_slot["listed"] += 1
        if cls["click"]:
            counts_slot["click"] += 1
        if cls["pool"] == "joined":
            counts_slot["pool"] += 1
        if cls["hit"]:
            counts_slot["hit"] += 1
            if cls["deal"]:
                counts_slot["deal"] += 1

    for a in attempts:
        cls = pool_contract.classify(a)
        listed = pool_contract.listed_state(a)
        if listed:
            counts["listed"] += 1
            listed_measured = True
        fs = settle_verify.norm_ms(a.get("first_seen_ms"))
        la = settle_verify.norm_ms(a.get("listed_at_ms"))
        ca = settle_verify.norm_ms(a.get("click_at_ms"))
        if fs is not None:
            first_seen_measured = True
        gap = settle_verify.gap_ms(fs, la)
        if gap is not None:
            gap_dl.append(float(gap))
        gap = settle_verify.gap_ms(la, ca)
        if gap is not None:
            gap_lc.append(float(gap))
        if cls["click"]:
            counts["click"] += 1
        if cls["pool"] == "joined":
            counts["pool"] += 1
        if cls["unknown"]:
            unknown += 1
        if cls["hit"]:
            counts["hit"] += 1
            state = settle_verify.normalize(a.get("verified"))
            if cls["deal"]:
                counts["deal"] += 1
                deal_evidence += 1
            else:
                unverified += 1
                if state == settle_verify.LOST:
                    deal_evidence += 1
                    settle_lost += 1
                else:
                    settle_unknown += 1
        if cls["blocked"]:
            blocked += 1
        if cls["conflict"]:
            conflicts += 1
            if len(conflict_samples) < 12:
                conflict_samples.append({"name": a.get("name"),
                                          "ts": a.get("ts"),
                                          "kind": cls["conflict"]})
        if isinstance(a.get("elapsed"), (int, float)):
            elapsed.append(float(a["elapsed"]))
        mode = a.get("mode") or "unknown"
        _slot(by_mode.setdefault(mode, _empty_counts()), cls, listed)
        _slot(by_day.setdefault(a["day"], _empty_counts()), cls, listed)

    for s in bundle["skips"]:
        skip += 1
        reason = str(s.get("skip_reason") or "unlabeled")
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        dslot = by_day.setdefault(s["day"], _empty_counts())
        dslot["discover"] += 1

    for ev in bundle.get("telemetry_events") or []:
        if isinstance(ev.get("skew_ms"), (int, float)):
            skew.append(float(ev["skew_ms"]))

    measured = {
        "discover": discovery_measured,
        "listed": listed_measured,
        "click": True,
        "pool": True,
        "hit": True,
        "deal": True,
    }
    override = bundle.get("stage_measured") or {}
    for k, v in override.items():
        if k in measured:
            measured[k] = bool(v)

    settle_info = {"evidence_n": deal_evidence, "unknown_n": settle_unknown,
                   "lost_n": settle_lost}
    gaps = {"discover_listed": gap_dl, "listed_click": gap_lc}
    batch_metrics = _metrics(counts, blocked, skip, unverified, elapsed, skew,
                             unknown=unknown, measured=measured,
                             conflicts=conflicts, settle=settle_info, gaps=gaps)
    batch_stages = _stage_rows(counts, measured, {"pool": unknown},
                               {"deal": unverified}, settle=settle_info)

    rollup = bundle["rollup"]
    today = today or (max(rollup.get("days", {}).keys()) if rollup.get("days") else None)
    if today:
        win = window_stats(cfg, rollup, today=today)
    else:
        win = {
            "counts": counts, "metrics": batch_metrics, "blocked": blocked,
            "skip": skip, "unverified_hits": unverified, "pool_unknown": unknown,
            "won_conflicts": conflicts, "by_mode": by_mode, "measured": measured,
            "skip_reasons": skip_reasons, "per_day": by_day,
            "settle": settle_info,
            "time_marks": {"first_seen_measured": first_seen_measured,
                           "listed_measured": listed_measured},
        }
    win_settle = win.get("settle") or {"evidence_n": 0, "unknown_n": 0,
                                       "lost_n": 0}
    win_stages = _stage_rows(win["counts"], win.get("measured"),
                             {"pool": win.get("pool_unknown", 0)},
                             {"deal": win.get("unverified_hits", 0)},
                             settle=win_settle)
    win["stages"] = win_stages
    win["no_evidence"] = [r["stage"] for r in win_stages if r["no_evidence"]]

    segments = {
        "by_mode": {m: {"counts": c, "stages": _stage_rows(c)} for m, c in by_mode.items()},
        "by_day": {d: {"counts": c, "stages": _stage_rows(c)} for d, c in by_day.items()},
    }

    return {
        "batch": {
            "counts": counts, "blocked": blocked, "skip": skip,
            "unverified_hits": unverified, "pool_unknown": unknown,
            "won_conflicts": conflicts, "skip_reasons": skip_reasons,
            "measured": measured, "settle": settle_info,
            "time_marks": {"first_seen_measured": first_seen_measured,
                           "listed_measured": listed_measured},
            "metrics": batch_metrics,
            "stages": batch_stages,
            "no_evidence": [r["stage"] for r in batch_stages if r["no_evidence"]],
            "n": counts["discover"],
            "conflict_samples": conflict_samples,
        },
        "window": win,
        "segments": segments,
        "health": bundle.get("health") or {},
        "window_days": int(cfg["funnel"]["window_days"]),
        "window_end": today,
    }
