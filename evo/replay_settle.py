"""用冻结语料回放「结算三态 + 两个时间戳 + 六段漏斗」。

只跑离线层：不加载引擎、不碰游戏、不写用户真实 data/。
台账写到 ``evo/runs/round1_20261107/settle_replay/``，进程内临时改
``records.PATH``，和 tests/conftest.py 的隔离手法一致。

用法：
    python -m evo.replay_settle
"""
from __future__ import annotations

import json
import os
import sys

from core import records, settle_verify
from evo import funnel, pool_contract
from evo.common import atomic_write_json, ensure_dir
from evo.config import load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "evo", "runs", "round1_20261107", "corpus_real.json")
OUT_DIR = os.path.join(ROOT, "evo", "runs", "round1_20261107", "settle_replay")

# 语料行 -> attempts 行。ts=None 的 9 行原样保留（不补墙钟，不猜）。
ATTEMPT_KEYS = (
    "ts", "day", "name", "price", "result", "mode", "quota", "verified",
    "verified_by", "elapsed", "shot", "tier", "attempt_id", "join_result",
    "join_raw", "pool_entered", "roll_self", "roll_max", "won",
    "listed_at_ms", "listed_at", "first_seen_ms", "click_at_ms", "no_evidence",
)


def _attempt(row):
    a = {k: row.get(k) for k in ATTEMPT_KEYS}
    a["day"] = str(row.get("day") or "unknown")
    ts = row.get("ts")
    a["ts"] = float(ts) if isinstance(ts, (int, float)) else None
    return a


def _ledger_write(rows, path):
    """走 records.add 真正落盘（不是手写 json），证明写路径带三态。

    返回读回来的行（load 必须在 PATH 还指着回放台账时做）。
    """
    old = records.PATH
    records.PATH = path
    try:
        for r in rows:
            verdict = settle_verify.from_row(r)
            records.add(
                name=str(r.get("name") or ""),
                price=r.get("price"),
                result=str(r.get("result") or "hit"),
                elapsed=r.get("elapsed"),
                shot=str(r.get("shot") or ""),
                mode=str(r.get("mode") or "unknown"),
                quota=r.get("quota"),
                verified=verdict,
                extra={
                    "ts": r.get("ts"),
                    "day": r.get("day"),
                    "tier": r.get("tier"),
                    "attempt_id": r.get("attempt_id"),
                    "source": "settle_replay",
                    "verified_by": "settle_verify",
                    "first_seen_ms": r.get("first_seen_ms"),
                    "listed_at_ms": r.get("listed_at_ms"),
                    "click_at_ms": r.get("click_at_ms"),
                },
            )
        return list(reversed(records.load(limit=0)))
    finally:
        records.PATH = old


def _demo_bundle():
    """六段全有证据的构造样本，用来证明新观测能力真的能出数。"""
    base = {"ts": 1794000000.0, "day": "2026-11-07", "mode": "normal",
            "first_seen_ms": 1000, "listed_at_ms": 3000, "click_at_ms": 3500}
    rows = [
        dict(base, name="演示-中签", result="hit", join_result="joined",
             verified="won", attempt_id="demo-won"),
        dict(base, name="演示-未中签", result="hit", join_result="joined",
             verified="lost", attempt_id="demo-lost"),
        dict(base, name="演示-无证据", result="hit", join_result="joined",
             verified="unknown", attempt_id="demo-unknown"),
        dict(base, name="演示-队列满", result="miss", join_result="queue_full",
             attempt_id="demo-full"),
        dict(base, name="演示-已入池", result="miss", join_result="joined",
             verified="unknown", attempt_id="demo-pooled"),
        dict(base, name="演示-入池未知", result="miss",
             attempt_id="demo-pool-unknown"),
    ]
    return {"attempts": rows, "skips": [], "rollup": {"days": {}},
            "health": {}, "telemetry_events": [], "discover_measured": True,
            "stage_measured": {}}


def _funnel_rows(stages):
    return [{"stage": s["stage"], "label": s["label"], "count": s["count"],
             "unknown": s["unknown"], "effective": s["effective"],
             "status": s["status"], "rate_of_discover": s["rate_of_discover"],
             "step_rate": s["step_rate"], "loss_count": s["loss_count"],
             "loss_rate": s["loss_rate"]} for s in stages]


def run():
    corpus = json.load(open(CORPUS, encoding="utf-8"))
    rows = corpus["attempts"]
    ensure_dir(OUT_DIR)
    ledger = os.path.join(OUT_DIR, "ledger.jsonl")
    if os.path.exists(ledger):
        os.remove(ledger)

    # 1) 旧行照读：原始语料行（verified=None、没有新字段）直接进 funnel
    legacy_attempts = [_attempt(r) for r in rows]
    legacy_bundle = {"attempts": legacy_attempts,
                     "skips": corpus.get("skips") or [],
                     "rollup": {"days": {}},
                     "health": corpus.get("health") or {},
                     "telemetry_events": corpus.get("telemetry_events") or [],
                     "discover_measured": corpus.get("discover_measured", True),
                     "stage_measured": corpus.get("stage_measured") or {}}
    legacy_funnel = funnel.build(load_config(), legacy_bundle)

    # 2) 三态写盘（records.add）再读回来
    written = _ledger_write(rows, ledger)
    verdicts = [settle_verify.from_row(r) for r in written]
    first_unknown = next(
        ({"attempt_id": r.get("attempt_id"), "ts": r.get("ts"),
          "day": r.get("day"), "verified": r.get("verified"),
          "no_evidence": r.get("no_evidence")}
         for r, v in zip(written, verdicts) if v == settle_verify.UNKNOWN), None)

    # 3) 读回来的台账 -> 漏斗
    attempts = [_attempt(r) for r in written]
    bundle = dict(legacy_bundle, attempts=attempts)
    f = funnel.build(load_config(), bundle)

    # 4) 构造样本：六段全有证据
    demo = funnel.build(load_config(), _demo_bundle(), today="2026-11-07")

    report = {
        "corpus": str(CORPUS),
        "rows": len(rows),
        "hit": sum(1 for r in rows if r.get("result") == "hit"),
        "miss": sum(1 for r in rows if r.get("result") == "miss"),
        "legacy_read_ok": True,
        "written_rows": len(written),
        "verdicts": {
            settle_verify.WON: verdicts.count(settle_verify.WON),
            settle_verify.LOST: verdicts.count(settle_verify.LOST),
            settle_verify.UNKNOWN: verdicts.count(settle_verify.UNKNOWN),
        },
        "first_unknown": first_unknown,
        "sample_rows": [
            {"attempt_id": r.get("attempt_id"), "verified": r.get("verified"),
             "first_seen_ms": r.get("first_seen_ms"),
             "listed_at_ms": r.get("listed_at_ms"),
             "no_evidence": r.get("no_evidence")}
            for r in written[:3]
        ],
        "funnel_batch_counts": f["batch"]["counts"],
        "funnel_batch_stages": _funnel_rows(f["batch"]["stages"]),
        "funnel_batch_no_evidence": f["batch"]["no_evidence"],
        "funnel_batch_settle": f["batch"]["settle"],
        "funnel_batch_time_marks": f["batch"]["time_marks"],
        "funnel_batch_timing": {
            "discover_to_listed": f["batch"]["metrics"]["discover_to_listed"],
            "listed_to_click": f["batch"]["metrics"]["listed_to_click"],
        },
        "funnel_batch_settle_rate": f["batch"]["metrics"]["settle_rate"],
        "funnel_batch_settle_state": f["batch"]["metrics"]["settle_state"],
        "legacy_stages": _funnel_rows(legacy_funnel["batch"]["stages"]),
        "demo": {
            "counts": demo["batch"]["counts"],
            "stages": _funnel_rows(demo["batch"]["stages"]),
            "no_evidence": demo["batch"]["no_evidence"],
            "settle": demo["batch"]["settle"],
            "timing": {
                "discover_to_listed": demo["batch"]["metrics"]["discover_to_listed"],
                "listed_to_click": demo["batch"]["metrics"]["listed_to_click"],
            },
            "settle_rate": demo["batch"]["metrics"]["settle_rate"],
        },
    }
    atomic_write_json(os.path.join(OUT_DIR, "report.json"), report)
    return report


def _p2(v):
    return "-" if v is None else f"{v:.2%}"


def _print(report):
    print(f"语料 {report['rows']} 行（hit {report['hit']} / miss {report['miss']}）")
    print(f"写盘 {report['written_rows']} 行，三态分布 {report['verdicts']}")
    u = report["first_unknown"]
    if u:
        print(f"第一条 unknown：{u['attempt_id']} day={u['day']} "
              f"verified={u['verified']!r} no_evidence={u['no_evidence']}")
    print("\n回放六段（真实语料）：")
    for s in report["funnel_batch_stages"]:
        print(f"  {s['label']:<6} n={s['count']:<4} 未知={s['unknown']:<3} "
              f"段间={_p2(s['step_rate'])} 状态={s['status']}")
    print(f"  no_evidence 段：{report['funnel_batch_no_evidence']}")
    print(f"  结算率：{report['funnel_batch_settle_rate']}"
          f"（{report['funnel_batch_settle_state']}）")
    dl = report["funnel_batch_timing"]["discover_to_listed"]
    lc = report["funnel_batch_timing"]["listed_to_click"]
    print(f"  候选→挂单：samples={dl['samples']} status={dl['status']}")
    print(f"  挂单→点进：samples={lc['samples']} status={lc['status']}")
    print("\n构造样本（六段全有证据）：")
    for s in report["demo"]["stages"]:
        print(f"  {s['label']:<6} n={s['count']:<4} "
              f"段间={_p2(s['step_rate'])} 状态={s['status']}")
    print(f"  结算率 {_p2(report['demo']['settle_rate'])} / 时长 "
          f"{report['demo']['timing']['discover_to_listed']['p50_s']}s、"
          f"{report['demo']['timing']['listed_to_click']['p50_s']}s")


if __name__ == "__main__":
    _print(run())
