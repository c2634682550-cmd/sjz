"""覆盖率报告：events.jsonl 每个字段的填充率 + 漏斗断点。

    .venv\\Scripts\\python.exe evo\\collect\\coverage.py
    ... --split real|synthetic|all
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from evo.collect import schema  # noqa: E402
EVENTS = os.path.join(HERE, "events.jsonl")
STAGES = os.path.join(HERE, "stages.jsonl")
OUT = os.path.join(HERE, "coverage.json")

STAGE_LABEL = OrderedDict([(1, "发现"), (2, "点击"), (3, "进池"),
                           (4, "中签"), (5, "成交")])
STAGE_FIELD = {1: "first_seen_ms", 2: "click_at_ms", 3: "pool_joined_at_ms",
               4: "roll_at_ms", 5: "deal_at_ms"}


def load(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(o, dict):
                    rows.append(o)
    except OSError:
        pass
    return rows


def fill_rate(rows, field):
    if not rows:
        return {"filled": 0, "rate": 0.0}
    n = sum(1 for r in rows if r.get(field) is not None)
    return {"filled": n, "rate": round(n / len(rows), 4)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=("real", "synthetic", "all"), default="real")
    ap.add_argument("--events", default=EVENTS)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    events = load(args.events)
    stages = load(STAGES)
    real = [e for e in events if not e.get("synthetic")]
    synth = [e for e in events if e.get("synthetic")]
    rows = {"real": real, "synthetic": synth, "all": events}[args.split]

    report = {"split": args.split, "events_total": len(events),
              "real": len(real), "synthetic": len(synth),
              "stages_observations": len(stages), "fields": {}}

    for name, (_t, dom, null) in schema.FIELDS.items():
        if name in ("event_id", "kind", "source_path", "tz"):
            continue
        fr = fill_rate(rows, name)
        report["fields"][name] = {"filled": fr["filled"], "rate": fr["rate"],
                                  "domain": dom, "null_policy": null}

    # 漏斗：段位分布 + 每段有时间戳的比例
    dist = Counter(int(e.get("stage_reached") or 0) for e in rows)
    report["stage_reached_dist"] = {str(k): dist.get(k, 0) for k in range(0, 6)}
    per_stage = {}
    for i, label in STAGE_LABEL.items():
        fld = STAGE_FIELD[i]
        per_stage[label] = {
            "timestamped": sum(1 for e in rows if e.get(fld) is not None),
            "reached_ge": sum(1 for e in rows if int(e.get("stage_reached") or 0) >= i),
        }
    report["stages"] = per_stage
    report["blackbox_stages"] = [k for k, v in per_stage.items() if v["timestamped"] == 0]

    # 来源分布 / trust 分布 / schema 校验
    report["by_source"] = dict(Counter(e.get("source") for e in rows))
    report["by_trust"] = dict(Counter(e.get("trust") for e in rows))
    bad = []
    for e in rows:
        issues = schema.validate(e)
        if issues:
            bad.append({"event_id": e.get("event_id"), "issues": issues})
    report["invalid_rows"] = len(bad)
    report["invalid_examples"] = bad[:5]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(f"split={args.split}  机会={len(rows)}（真实 {len(real)} / 仿真 {len(synth)}）"
          f"  段观测={len(stages)}  非法行={report['invalid_rows']}")
    print(f"{'字段':<20}{'填充':>6}{'填充率':>9}")
    for name, v in report["fields"].items():
        if v["filled"] == 0 and v["rate"] == 0:
            continue
        print(f"{name:<20}{v['filled']:>6}{v['rate']*100:>8.1f}%")
    print("\n漏斗：", " → ".join(f"{k}({report['stages'][k]['timestamped']})"
                                for k in report["stages"]))
    print("段位分布：", report["stage_reached_dist"])
    print("全黑盒段：", report["blackbox_stages"] or "无")
    print("来源：", report["by_source"], " trust：", report["by_trust"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
