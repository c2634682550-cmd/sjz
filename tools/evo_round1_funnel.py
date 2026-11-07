#!/usr/bin/env python
"""R1 真实漏斗生成器：只用已有台账/遥测/旁路，缺数据的环节标「无数据」。

真实来源（按可信度排序）：
  1) tuning_recommendation.json —— 2026-11-07 08:03 对当时 137 行台账的聚合快照；
     现 data/records.jsonl 已被写盘测试残留覆盖（1 行 / name 2160 字节），不可用。
  2) evo/collect/inbox/*.jsonl —— 引擎 pooltrace 的 toast 旁路（真实，1 条 draw，
     roll 99/125，won=false），无 attempt 可挂。
  3) data/telemetry/ —— 不存在（feature/ledger 遥测库都没落盘）。

本工具把 1) 还原成 attempts 并调用 evo.funnel.build（与主循环同一套口径），
输出 JSON + Markdown 表；同时导出 A/B 用的冻结 corpus。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evo.common import day_str, ensure_dir  # noqa: E402
from evo.collect.parse import parse_toast_line  # noqa: E402
from evo import funnel  # noqa: E402


def _read_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(p, obj):
    ensure_dir(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_attempts(metrics):
    hits = [float(t) for t in (metrics.get("hit_ts") or [])]
    attempts = []
    for i, ts in enumerate(hits, 1):
        attempts.append({
            "ts": ts,
            "day": day_str(ts),
            "name": "",
            "name_len": 0,
            "price": None,
            "result": "hit",
            "mode": "unknown",
            "quota": None,
            "verified": None,
            "verified_by": "",
            "elapsed": None,
            "shot": "",
            "tier": "unknown",
            "source": "legacy_aggregate",
            "attempt_id": f"legacy-hit-{i:04d}",
            "ts_quality": "exact",
        })
    n_miss = int(metrics.get("n_attempts", 0)) - len(hits)
    for i in range(max(0, n_miss)):
        attempts.append({
            "ts": None,
            "day": "unknown",
            "name": "",
            "name_len": 0,
            "price": None,
            "result": "miss",
            "mode": "unknown",
            "quota": None,
            "verified": None,
            "verified_by": "",
            "elapsed": None,
            "shot": "",
            "tier": "unknown",
            "source": "legacy_aggregate",
            "attempt_id": f"legacy-miss-{i:04d}",
            "ts_quality": "hour_bucket_only",
        })
    return attempts, n_miss


def audit_sources(tuning):
    audit = {}

    p = os.path.join(ROOT, "data", "records.jsonl")
    rec = {"path": "data/records.jsonl", "present": os.path.exists(p)}
    if rec["present"]:
        rec["size_bytes"] = os.path.getsize(p)
        with open(p, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        rec["lines"] = 1 if line else 0
        try:
            row = json.loads(line)
            rec["row"] = {k: (v if k != "name" else f"<len {len(str(v))}>")
                          for k, v in row.items()}
            rec["name_len"] = len(str(row.get("name") or ""))
        except (ValueError, TypeError) as e:
            rec["error"] = str(e)
        rec["usable"] = False
        rec["reason"] = ("现文件只有 1 行且 name 超长（写盘测试残留）；"
                         "与 tuning 快照生成时（2026-11-07 08:03，137 行）不一致")
    audit["records"] = rec

    p = os.path.join(ROOT, "evo", "collect", "events.jsonl")
    ev = {"path": "evo/collect/events.jsonl", "present": os.path.exists(p)}
    if ev["present"]:
        with open(p, "r", encoding="utf-8") as f:
            lines = [x for x in f if x.strip()]
        ev["lines"] = len(lines)
        ev["usable"] = False
        ev["reason"] = "由当前 1 行残留 records 派生，非真实新增事件"
    audit["events"] = ev

    inbox = os.path.join(ROOT, "evo", "collect", "inbox")
    items = []
    for fn in sorted(os.listdir(inbox)) if os.path.isdir(inbox) else []:
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(inbox, fn), "r", encoding="utf-8") as f:
            for no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    st = parse_toast_line(line.strip(), fn, no)
                except (ValueError, TypeError):
                    continue
                if not st:
                    continue
                items.append({
                    "file": f"evo/collect/inbox/{fn}",
                    "stage": st["stage"],
                    "ts": st.get("occurred_at"),
                    "name": st.get("name") or "",
                    "roll_self": st.get("roll_self"),
                    "roll_max": st.get("roll_max"),
                    "won": st.get("won"),
                    "won_source": st.get("won_source"),
                    "join_result": st.get("join_result"),
                    "text": (st.get("text") or "")[:80],
                })
    audit["pool_inbox"] = {"present": os.path.isdir(inbox), "rows": len(items),
                           "usable": bool(items), "items": items}

    for name in ("telemetry", "feature_telemetry"):
        p = os.path.join(ROOT, "data", name)
        audit[f"data_{name}"] = {
            "path": f"data/{name}",
            "present": os.path.exists(p),
            "rows": (len(os.listdir(p)) if os.path.isdir(p) else 0),
        }
    if not os.path.exists(os.path.join(ROOT, "data", "telemetry")):
        audit["telemetry_status"] = "无数据（data/telemetry 不存在）"

    met = (tuning.get("metrics") or {})
    q = met.get("quality") or {}
    audit["tuning_snapshot"] = {
        "path": "tuning_recommendation.json",
        "generated_at": tuning.get("generated_at"),
        "n_rows": met.get("n_rows"),
        "n_attempts": met.get("n_attempts"),
        "n_hits": met.get("n_hits"),
        "n_misses": int(met.get("n_attempts", 0)) - int(met.get("n_hits", 0)),
        "hit_ts": len(met.get("hit_ts") or []),
        "ts_min": q.get("ts_min"),
        "ts_max": q.get("ts_max"),
        "fields_seen": ((tuning.get("inputs") or {}).get("records")
                        or {}).get("fields_seen"),
        "result_breakdown": met.get("result_breakdown"),
    }
    return audit


def table_md(funnel_out):
    counts = funnel_out["window"]["counts"]
    m = funnel_out["window"]["metrics"]
    rows = funnel_out["window"]["stages"]

    def pct(v):
        return "无数据" if v is None else f"{v * 100:.2f}%"

    lines = [
        "| 环节 | 样本（可证） | 未知 | 段间转化（到下一环节） | 流失数 | 数据状态 |",
        "|---|---|---|---|---|---|",
    ]
    for st in rows:
        if st["stage"] == "discover":
            n, status = f"{st['count']}（下界）", "无独立记录"
        elif st["stage"] == "listed":
            n, status = "无数据", "无数据"
        elif st["stage"] == "click":
            n, status = str(st["count"]), "台账 attempt"
        elif st["stage"] == "pool":
            n = f"{st['count']} 可证" if st["count"] else "无数据"
            status = (f"hit 反推；{st['unknown']} 条未知计入分母"
                      if st["unknown"] else "台账证据")
        elif st["stage"] == "hit":
            n, status = str(st["count"]), "result=hit（claimed）"
        else:
            n = f"{st['count']} 已校验"
            status = f"{st['claimed']} 条 claimed 未校验"
        lines.append(f"| {st['label']} | {n} | {st['unknown'] or '-'} | "
                     f"{pct(st['step_rate'])} | "
                     f"{st['loss_count'] if st['loss_count'] is not None else '-'} | "
                     f"{status} |")
    lines.append("")
    counts = funnel_out["window"]["counts"]
    on_proven_hit = (counts.get("hit", 0) / counts.get("pool", 1)
                     if counts.get("pool") else None)
    lines.append(
        f"- 端到端（已校验成交 / 候选）：{pct(m.get('end_to_end_rate'))}")
    lines.append(
        f"- 详情→可证入池：{pct(m.get('pool_rate'))}；入池未知 "
        f"{m.get('pool_unknown_count', 0)} 单（{pct(m.get('pool_unknown_rate'))} of 点击）")
    lines.append(
        f"- 中签 / 可证入池（仅 {counts.get('pool', 0)} 条）：{pct(on_proven_hit)}；"
        f"中签 / 发起（含未知）：{pct(m.get('hit_rate'))}")
    lines.append(
        f"- 中签→已校验成交：{pct(m.get('settle_rate'))}；"
        f"未校验中签率 {pct(m.get('unverified_hit_rate'))}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tuning", default=os.path.join(ROOT, "tuning_recommendation.json"))
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "evo", "runs", "round1_20261107"))
    ap.add_argument("--dump-corpus", default="")
    args = ap.parse_args()

    from evo.config import load_config
    cfg = load_config()

    tuning = _read_json(args.tuning)
    met = tuning.get("metrics") or {}
    audit = audit_sources(tuning)
    attempts, n_miss = build_attempts(met)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    health = {
        "ledger": "legacy_snapshot",
        "telemetry": "absent",
        "rows_read": len(attempts),
        "invalid_rows": 0,
        "suspicious_rows": 0,
        "health_ratio": 1.0,
        "anomaly_ratio": 0.0,
        "source": "tuning_recommendation.json",
        "source_generated_at": tuning.get("generated_at"),
        "current_records_usable": False,
        "pool_inbox": audit.get("pool_inbox", {}),
    }
    bundle = {
        "attempts": attempts,
        "skips": [],
        "telemetry_events": [],
        "rollup": {},
        "health": health,
        "discover_measured": False,
        "stage_measured": {"listed": False},
        "source": "legacy_aggregate",
        "built_at": stamp,
    }
    out = funnel.build(cfg, bundle)

    ensure_dir(args.out_dir)
    _write_json(os.path.join(args.out_dir, "corpus_real.json"), bundle)
    _write_json(os.path.join(args.out_dir, "source_audit.json"), audit)
    _write_json(os.path.join(args.out_dir, "funnel_real.json"), {
        "built_at": stamp,
        "source": "tuning_recommendation.json",
        "source_generated_at": tuning.get("generated_at"),
        "missing_ts_rows": n_miss,
        "funnel": out,
        "table_md": table_md(out),
    })

    if args.dump_corpus:
        _write_json(args.dump_corpus, bundle)

    print(table_md(out))
    print("")
    print(f"attempts={len(attempts)} hits={len(attempts) - n_miss} "
          f"misses={n_miss} miss_ts=hour-bucket-only")
    print(f"source_generated_at={tuning.get('generated_at')} "
          f"build={stamp} out={args.out_dir}")


if __name__ == "__main__":
    main()
