"""诊断推理器：硬信号 → 统计信号 → 相关性，最后给出「放弃并转人工」的判断。

推理顺序是有意的：
1. hard  —— 必现硬信号（错误码/异常类名/明确告警），一条就能定性；
2. stat  —— 统计信号（速率、阈值、斜率），要跟同窗口的安静基线比；
3. corr  —— 相关性（台账不增长但成交在增长、日志同时段恶化等），最弱，只做加分。

三档来源对应三个置信度上限，低档不允许给出高置信度结论。
"""
from __future__ import annotations

import math

from ..common import fmt_ts
from . import rules as R
from . import sources
from .model import CauseHit, Diagnosis, Evidence

MIN_WINDOW_S = 20.0     # 低于这个时长又无硬信号，统计量不可信
MIN_CONF = 0.35         # 结论成立的最低置信度
AMBIGUOUS_GAP = 0.06    # 前两名差多少算「分不开」


def _confidence(score: float) -> float:
    return max(0.0, min(0.97, 1.0 - math.exp(-1.5 * score)))


def _dedupe(evs: list[Evidence], limit: int = 6) -> list[Evidence]:
    seen, out = set(), []
    for e in sorted(evs, key=lambda x: (x.source, x.line or 0)):
        key = (e.source, e.line, e.text[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out[:limit]


def diagnose(bundle: sources.SourceBundle, book: R.RuleBook,
             start: float | None = None, end: float | None = None,
             artifacts: str | None = None, label: str | None = None) -> Diagnosis:
    diag = Diagnosis(
        window_start=start, window_end=end,
        window_label=label or (f"{fmt_ts(start)} ~ {fmt_ts(end)}" if start and end else "整段"),
        sources=dict(bundle.counts))
    diag.notes += bundle.notes

    scores: dict[str, float] = {}
    basis: dict[str, str] = {}
    evs: dict[str, list[Evidence]] = {}
    fired: dict[str, list[str]] = {}
    hard_hits: list[tuple[str, str, float]] = []
    missing: list[str] = []

    for rule in sorted(book.rules, key=lambda r: R.STAGE_ORDER[r["stage"]]):
        m = R.match_rule(rule, bundle, start, end, artifacts)
        missing += m.missing
        if not m.matched:
            continue
        for cause, w in rule["then"].items():
            scores[cause] = scores.get(cause, 0.0) + float(w) * m.strength * float(rule.get("weight", 1.0))
            prev = basis.get(cause)
            if prev is None or R.STAGE_ORDER[rule["stage"]] < R.STAGE_ORDER[prev]:
                basis[cause] = rule["stage"]
            evs.setdefault(cause, []).extend(m.evidence)
            fired.setdefault(cause, []).append(rule["id"])
        if rule["stage"] == "hard":
            hard_hits.append((rule["phenomenon"], rule["id"], m.strength))

    pid_of_cause: dict[str, tuple[str, dict]] = {}
    for pid, p in book.phenomena.items():
        for c in p.get("causes", []):
            pid_of_cause[c["id"]] = (pid, c)

    causes: list[CauseHit] = []
    for cid, sc in scores.items():
        if cid not in pid_of_cause:
            continue
        _pid, c = pid_of_cause[cid]
        conf = _confidence(sc)
        b = basis.get(cid, "corr")
        if b == "hard":
            conf = max(conf, 0.85)
        elif b == "stat":
            conf = min(conf, 0.75)
        else:
            conf = min(conf, 0.60)
        causes.append(CauseHit(cause=cid, label=c.get("label", cid), score=round(sc, 3),
                               confidence=round(min(conf, 0.97), 3),
                               scene=c.get("scene", ""), actions=list(c.get("actions", [])),
                               risk=c.get("risk", ""), basis=b,
                               evidence=_dedupe(evs.get(cid, [])), rules=fired.get(cid, [])))
    causes.sort(key=lambda c: c.score, reverse=True)
    diag.causes = causes[:5]
    diag.missing = _dedupe_text(missing)

    # ---------------- 放弃条件 ----------------
    dur = (end - start) if (start is not None and end is not None) else None
    hard_fire = [h for h in hard_hits if h[2] >= 0.7]
    top = diag.top

    if not causes:
        if dur is not None and dur < MIN_WINDOW_S:
            diag.verdict = "insufficient"
            diag.missing.append(f"窗口只有 {dur:.0f}s（< {MIN_WINDOW_S:.0f}s），且无硬信号")
        elif any(bundle.counts.get(k, 0) for k in ("log", "cycles", "metrics", "ledger")):
            diag.verdict = "normal"
            diag.notes.append("数据可读但未命中任何已知现象：按『未见已知异常』处理，不等于健康")
        else:
            diag.verdict = "insufficient"
            diag.missing.append("没有任何可读数据")
        return diag

    if len(hard_fire) >= 2:
        pids = sorted({h[0] for h in hard_fire})
        sets = [{c["id"] for c in book.phenomenon(x).get("causes", [])} for x in pids]
        disjoint = all(not (sets[i] & sets[j])
                       for i in range(len(sets)) for j in range(i + 1, len(sets)))
        if len(pids) >= 2 and disjoint:
            diag.verdict = "conflict"
            diag.notes.append("两个硬信号指向不同根因，且假设集合不相交：" + "、".join(pids))
            diag.missing.append("需要更窄的时间窗或当时的人工操作记录，才能二选一")
            return diag

    if not hard_fire and dur is not None and dur < MIN_WINDOW_S \
            and (top is None or top.score < 0.4):
        diag.verdict = "insufficient"
        diag.missing.append(f"窗口只有 {dur:.0f}s（< {MIN_WINDOW_S:.0f}s），统计量不足以定性")
        return diag

    if top.confidence < MIN_CONF:
        diag.verdict = "insufficient"
        diag.missing.append(f"最高分只有 {top.confidence:.2f}，低于结论线 {MIN_CONF}")
        return diag

    if len(causes) >= 2 and not hard_fire and \
            (causes[0].confidence - causes[1].confidence) < AMBIGUOUS_GAP:
        diag.verdict = "ambiguous"
        diag.missing.append(
            f"{causes[0].label} 与 {causes[1].label} 只差 "
            f"{causes[0].confidence - causes[1].confidence:.02f}：现有信号分不开，需要二次观测")
        return diag

    diag.verdict = "conclusive"
    return diag


def _dedupe_text(items: list[str]) -> list[str]:
    seen, out = set(), []
    for s in items:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out[:10]
