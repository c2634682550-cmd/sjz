"""把 evo/collect/inbox/*.jsonl 的 toast 旁路挂到主循环的 attempts 上。

背景：evo/collect/ 是独立采集分支，只写自己的 events/stages.jsonl；
七层主循环的 observe/funnel 从未读过它，入池与抽签证据因此一直不可判定。
本模块只做「挂接」：按 attempt_id 精确匹配，其次同名 + 0~180s 时序邻近；
只填空字段，挂不上就如实计数，不伪造机会。
"""
from __future__ import annotations

import glob
import os

from .common import abs_path
from .collect.collector import attach_stages, derive_event
from .collect.parse import parse_toast_line

EVIDENCE_KEYS = ("join_result", "join_raw", "pool_entered", "pool_joined_at_ms",
                 "queue_pos", "queue_capacity", "roll_self", "roll_max",
                 "won", "roll_at_ms", "toast_source", "listed_at_ms",
                 "first_seen_ms", "forward_remaining_s")


def inbox_dir(cfg):
    rel = (cfg.get("observation") or {}).get("pool_inbox_dir") or "evo/collect/inbox"
    return abs_path(rel)


def load_stages(cfg):
    """只读 inbox，不碰 collect 的游标/落盘文件。"""
    stats = {"dir": inbox_dir(cfg), "files": 0, "lines": 0, "parsed": 0,
             "invalid": 0, "kinds": {}}
    stages = []
    for p in sorted(glob.glob(os.path.join(stats["dir"], "*.jsonl"))):
        stats["files"] += 1
        try:
            with open(p, "r", encoding="utf-8") as f:
                for no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    stats["lines"] += 1
                    try:
                        st = parse_toast_line(line, os.path.basename(p), no)
                    except (ValueError, TypeError):
                        stats["invalid"] += 1
                        continue
                    if not st:
                        stats["invalid"] += 1
                        continue
                    stages.append(st)
                    stats["parsed"] += 1
                    stats["kinds"][st["stage"]] = stats["kinds"].get(st["stage"], 0) + 1
        except OSError:
            continue
    return stages, stats


def _fill(ev, st):
    if ev.get("name") is None and st.get("name"):
        ev["name"] = st["name"]
    tsrc = ev.get("time_src")
    if not isinstance(tsrc, dict):
        tsrc = {}
        ev["time_src"] = tsrc
    for k in EVIDENCE_KEYS:
        if st.get(k) is not None and ev.get(k) is None:
            ev[k] = st[k]
            tsrc[k] = st.get("time_src") or "toast"
    if st.get("attempt_id") and not ev.get("attempt_id"):
        ev["attempt_id"] = st["attempt_id"]
    ev["stage_evidence"] = list(ev.get("stage_evidence") or []) + [
        f"{st.get('source')}:{st['stage']}"]


def attach(attempts, cfg):
    """返回挂接统计；attached 数按「有一条 stage 落到某 attempt」计。"""
    stages, stats = load_stages(cfg)
    stats.update({"attached": 0, "unattached": 0})
    if not stages or not attempts:
        stats["unattached"] = len(stages)
        return stats

    for a in attempts:
        ts = a.get("ts")
        if a.get("ts_ms") is None:
            a["ts_ms"] = int(round(float(ts) * 1000)) if isinstance(ts, (int, float)) else None
        if not isinstance(a.get("time_src"), dict):
            a["time_src"] = {}

    rest = []
    by_id = {a.get("attempt_id"): a for a in attempts if a.get("attempt_id")}
    for st in stages:
        target = by_id.get(st.get("attempt_id")) if st.get("attempt_id") else None
        if target is None:
            rest.append(st)
            continue
        _fill(target, st)
        stats["attached"] += 1

    if rest:
        before = {id(a): len(a.get("stage_evidence") or []) for a in attempts}
        attach_stages(attempts, rest)
        gained = sum(1 for a in attempts
                     if len(a.get("stage_evidence") or []) > before[id(a)])
        stats["attached"] += gained
        stats["unattached"] = max(0, len(rest) - gained)
    for a in attempts:
        if not isinstance(a.get("time_src"), dict):
            a["time_src"] = {}
        derive_event(a)
    return stats
