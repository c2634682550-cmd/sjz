"""规则加载与匹配。

规则全部写在 `rules/phenomena.json` 和 `rules/rules.json` 里，这里只做三件事：
1. 加载并自检（引用的动作/场景在 core 里真实存在，不新造）；
2. 把一条规则的若干匹配器逐个求值；
3. 返回「命中强度 + 证据行 + 缺什么」。

匹配器是声明式的，加规则不需要改代码；只有新增匹配器种类才需要动这里。
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import sources
from .model import Evidence

STAGE_ORDER = {"hard": 0, "stat": 1, "corr": 2}

_METRIC_LEAK = ("faults_active", "faults_total", "faults_recovered")


@dataclass
class MatchResult:
    matched: bool
    strength: float
    evidence: list[Evidence] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class RuleBook:
    phenomena: dict[str, dict]
    rules: list[dict]
    actions: set[str]
    scenes: set[str]
    issues: list[str] = field(default_factory=list)
    dir: str = ""

    def phenomenon(self, pid: str) -> dict:
        return self.phenomena.get(pid) or {}

    def causes_for(self, pid: str) -> dict[str, dict]:
        p = self.phenomenon(pid)
        return {c["id"]: c for c in p.get("causes", [])}


def _extract(pattern: str, path: str) -> set[str]:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return set()
    return set(re.findall(pattern, text))


def load_rules(rule_dir: str, repo_root: str) -> RuleBook:
    phenomena = json.load(open(os.path.join(rule_dir, "phenomena.json"), encoding="utf-8"))
    rules = json.load(open(os.path.join(rule_dir, "rules.json"), encoding="utf-8"))
    actions = _extract(r'ActionSpec\("([a-z_]+)"', os.path.join(repo_root, "core", "recovery.py"))
    scenes = _extract(r'SC_[A-Z_]+\s*=\s*"([a-z_]+)"', os.path.join(repo_root, "core", "scene_guard.py"))
    book = RuleBook(phenomena={p["id"]: p for p in phenomena["phenomena"]}, rules=rules["rules"],
                    actions=actions, scenes=scenes, dir=rule_dir)
    book.issues = validate(book)
    return book


def validate(book: RuleBook) -> list[str]:
    """启动即自检：动作/场景写错立刻报，不等到运行期才失效。"""
    issues: list[str] = []
    global_causes: set[str] = set()
    for pid, p in book.phenomena.items():
        for c in p.get("causes", []):
            global_causes.add(c["id"])
            if c.get("scene") and c["scene"] not in book.scenes:
                issues.append(f"{pid}/{c['id']}: 场景 {c['scene']} 不存在")
            for a in c.get("actions", []):
                if a not in book.actions:
                    issues.append(f"{pid}/{c['id']}: 动作 {a} 不存在")
    for r in book.rules:
        if r["phenomenon"] not in book.phenomena:
            issues.append(f"{r['id']}: 现象 {r['phenomenon']} 未定义")
        for cause in r.get("then", {}):
            # 跨层根因是允许的（例如『倒计时停跳』要同时给出 OCR 与页面两个假设）
            if cause not in global_causes:
                issues.append(f"{r['id']}: 根因 {cause} 未在任何现象里定义")
        if r.get("stage") not in STAGE_ORDER:
            issues.append(f"{r['id']}: stage {r.get('stage')} 非法")
    return issues


def _num(r: sources.Row, field_: str) -> float | None:
    v = r.data.get(field_)
    try:
        return float(v) if v not in (None, "", "nan") else None
    except (TypeError, ValueError):
        return None


def _cmp(v: float, op: str, target: float) -> bool:
    return {">=": v >= target, ">": v > target, "<=": v <= target,
            "<": v < target, "==": v == target, "!=": v != target}.get(op, False)


def _strength_from_excess(v: float, target: float, op: str) -> float:
    if target == 0:
        return 1.0
    excess = (v - target) / abs(target) if op in (">=", ">") else (target - v) / abs(target)
    return max(0.55, min(1.0, 0.55 + 0.45 * min(1.0, max(0.0, excess)) * 3))


def _lines(bundle: sources.SourceBundle, start: float | None, end: float | None) -> list[sources.LogLine]:
    return [ln for ln in bundle.logs if sources.in_window(ln.ts, start, end)]


def match_rule(rule: dict, bundle: sources.SourceBundle, start: float | None,
               end: float | None, artifacts: str | None) -> MatchResult:
    """一条规则里的多个匹配器取交集：全部命中才算这条规则命中，强度取最小。"""
    results = [_MATCHERS[m["kind"]](m, bundle, start, end, artifacts)
               for m in rule.get("match", [])]
    if not results:
        return MatchResult(False, 0.0)
    hit = all(r.matched for r in results)
    strength = min(r.strength for r in results) if hit else 0.0
    ev: list[Evidence] = []
    miss: list[str] = []
    for r in results:
        ev += r.evidence
        miss += r.missing
    return MatchResult(hit, round(strength, 3), ev, miss)


def _m_log(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    pats = [m["pattern"]] if "pattern" in m else list(m.get("patterns", []))
    if not pats:
        return MatchResult(False, 0.0)
    regs = [re.compile(p, re.I if m.get("ignore_case") else 0) for p in pats]
    lines = _lines(b, start, end)
    hits = [ln for ln in lines if any(r.search(ln.text) for r in regs)]
    if m.get("require_all", False):
        hits = [ln for ln in hits if all(r.search(ln.text) for r in regs)]
    need = int(m.get("min_count", 1))
    if len(hits) < need:
        return MatchResult(False, 0.0)
    ev = [Evidence(ln.source, ln.line, ln.ts, ln.text.strip()[:160], note=m.get("label", ""))
          for ln in hits[:6]]
    return MatchResult(True, min(1.0, 0.6 + 0.4 * len(hits) / need), ev)


def _m_cycles(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    if not b.cycles:
        return MatchResult(False, 0.0, missing=["缺 cycles.jsonl：统计类规则无法求值"])
    rows = [r for r in b.cycles if sources.in_window(r.ts, start, end)]
    if not rows:
        return MatchResult(False, 0.0, missing=[f"窗口内没有 cycles（{start}~{end}）"])
    where = m.get("where") or {}
    sub = [r for r in rows if all(r.data.get(k) == v for k, v in where.items())]
    if not sub:
        return MatchResult(False, 0.0, missing=[f"cycles 中没有满足 {where} 的行"])
    field_ = m["field"]
    op, target = m["op"], float(m["value"])
    ok = [r for r in sub if _num(r, field_) is not None and _cmp(_num(r, field_), op, target)]
    rate = len(ok) / len(sub)
    rate_min = float(m.get("rate_min", 0.3))
    if rate < rate_min:
        return MatchResult(False, 0.0)
    ev = [Evidence(r.source, r.line, r.ts,
                   f"{field_}={r.data.get(field_)} state={r.data.get('state')} "
                   f"clicks={r.data.get('clicks')} error={str(r.data.get('error'))[:40]}")
          for r in ok[:4]]
    if m.get("min_count") is not None and len(ok) < int(m["min_count"]):
        return MatchResult(False, 0.0)
    strength = 0.55 + 0.45 * min(1.0, (rate - rate_min) / max(rate_min, 0.01))
    return MatchResult(True, round(min(strength, 1.0), 3), ev)


def _m_metric(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    if not b.metrics:
        return MatchResult(False, 0.0, missing=["缺 metrics.csv：资源/时序类规则无法求值"])
    rows = [r for r in b.metrics if sources.in_window(r.ts, start, end)]
    if not rows:
        return MatchResult(False, 0.0, missing=["窗口内没有 metrics 采样"])
    field_ = m["field"]
    vals = [v for v in (_num(r, field_) for r in rows) if v is not None]
    if not vals:
        return MatchResult(False, 0.0, missing=[f"metrics.{field_} 全为空"])
    agg = m.get("agg", "max")
    if agg == "max":
        v = max(vals)
    elif agg == "min":
        v = min(vals)
    elif agg == "mean":
        v = sum(vals) / len(vals)
    elif agg == "delta":
        v = vals[-1] - vals[0]
    else:
        return MatchResult(False, 0.0, missing=[f"未知 agg {agg}"])
    op, target = m["op"], float(m["value"])
    if not _cmp(v, op, target):
        return MatchResult(False, 0.0)
    src = rows[0].source
    ev = [Evidence(src, rows[0].line, rows[0].ts, f"{field_} agg={agg} {op} {target} 实际={v:.2f}",
                   note=m.get("label", ""))]
    return MatchResult(True, _strength_from_excess(v, target, op), ev)


def _m_slope(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    if not b.metrics:
        return MatchResult(False, 0.0, missing=["缺 metrics.csv：趋势类规则无法求值"])
    rows = [r for r in b.metrics if sources.in_window(r.ts, start, end) and _num(r, m["field"]) is not None]
    if len(rows) < 3:
        return MatchResult(False, 0.0, missing=["采样少于 3 个点，算不出趋势"])
    span = (rows[-1].ts - rows[0].ts) if rows[0].ts and rows[-1].ts else None
    if not span or span <= 0:
        return MatchResult(False, 0.0, missing=["采样时间跨度无效"])
    min_span = float(m.get("min_span", 60))
    if span < min_span:
        return MatchResult(False, 0.0,
                          missing=[f"窗口跨度 {span:.0f}s < {min_span:.0f}s：趋势类结论不可信"])
    delta = _num(rows[-1], m["field"]) - _num(rows[0], m["field"])
    min_delta = float(m.get("min_delta", 0))
    if abs(delta) < min_delta:
        return MatchResult(False, 0.0)
    per = 60.0 if m.get("unit", "s") == "min" else 1.0
    slope = delta / span * per
    op, target = m["op"], float(m["value"])
    if not _cmp(slope, op, target):
        return MatchResult(False, 0.0)
    ev = [Evidence(rows[0].source, rows[0].line, rows[0].ts,
                   f"{m['field']} 斜率 {slope:.4f}/{m.get('unit','s')}（{span:.0f}s 内 {_num(rows[0], m['field'])}→{_num(rows[-1], m['field'])}）")]
    return MatchResult(True, _strength_from_excess(slope, target, op), ev)


def _m_ledger(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    if not b.ledger and not b.bad_ledger:
        return MatchResult(False, 0.0, missing=["缺台账 records.jsonl：数据层规则无法求值"])
    stat = m["stat"]
    rows = [r for r in b.ledger if sources.in_window(r.ts, start, end)]
    bad = [x for x in b.bad_ledger if sources.in_window(x.ts, start, end)]
    if stat == "bad_rows":
        v = float(len(bad))
    elif stat == "rows":
        v = float(len(rows))
    elif stat == "max_line_bytes":
        v = float(max([len(json.dumps(r.data, ensure_ascii=False)) for r in rows] or [0]))
    elif stat == "hit_rate":
        v = (sum(1 for r in rows if r.data.get("result") == "hit") / len(rows)) if rows else 0.0
    elif stat == "miss_rate":
        v = (sum(1 for r in rows if r.data.get("result") == "miss") / len(rows)) if rows else 0.0
    elif stat == "unverified_rate":
        v = (sum(1 for r in rows if r.data.get("verified") is False) / len(rows)) if rows else 0.0
    else:
        return MatchResult(False, 0.0, missing=[f"未知台账统计 {stat}"])
    op, target = m["op"], float(m["value"])
    if not _cmp(v, op, target):
        return MatchResult(False, 0.0)
    if bad:
        ev = [Evidence(x.source, x.line, x.ts, f"坏行: {x.text[:120]}") for x in bad[:3]]
    elif rows:
        ev = [Evidence(rows[0].source, rows[0].line, rows[0].ts,
                       f"台账 {stat}={v:.3f}（阈值 {op} {target}）")]
    else:
        ev = []
    return MatchResult(True, _strength_from_excess(v, target, op), ev)


def _m_artifact(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    """离线目录证据（例如 config.json.broken-*.bad 落盘残留）。

    必须按文件 mtime 落进窗口，否则整目录的残留会在每个窗口都命中，变成全局误报。
    """
    if not artifacts:
        return MatchResult(False, 0.0, missing=["未提供 --artifact-dir，目录类证据不可用"])
    hits = sorted(glob.glob(os.path.join(artifacts, m["pattern"])))
    if start is not None and end is not None:
        hits = [h for h in hits if start <= os.path.getmtime(h) <= end]
    if len(hits) < int(m.get("min_count", 1)):
        return MatchResult(False, 0.0)
    ev = [Evidence(os.path.basename(h), None, os.path.getmtime(h),
                   f"残留文件 {os.path.basename(h)}") for h in hits[:4]]
    return MatchResult(True, 1.0, ev)


def _m_absent(m: dict, b: sources.SourceBundle, start, end, artifacts) -> MatchResult:
    key = m["source"]
    n = b.counts.get(key, 0)
    if n == 0:
        return MatchResult(False, 0.0, missing=[m.get("note") or f"缺数据源 {key}"])
    return MatchResult(True, 1.0)


_MATCHERS = {
    "log": _m_log,
    "cycles": _m_cycles,
    "metric": _m_metric,
    "slope": _m_slope,
    "ledger": _m_ledger,
    "artifact": _m_artifact,
    "absent": _m_absent,
}
