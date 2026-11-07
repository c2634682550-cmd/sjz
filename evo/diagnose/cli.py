"""命令行入口。

示例（仓库根目录下运行）：

  python -m evo.diagnose.cli ^
      --log _soak_data/baseline/soak.log ^
      --cycles _soak_data/baseline/cycles.jsonl ^
      --metrics _soak_data/baseline/metrics.csv ^
      --ledger _soak_data/baseline/data/records.jsonl ^
      --since "2026-11-07 05:44:25" --until "2026-11-07 05:45:05"

也可以只喂一段文本日志（RESULT 台架输出没有时间戳，按整段诊断）：

  python -m evo.diagnose.cli --log path/to/persist.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import engine, report, rules as R
from . import sources

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_RULES = os.path.join(ROOT, "evo", "diagnose", "rules")


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S"):
        try:
            t = time.strptime(s, fmt)
            if fmt == "%H:%M:%S":
                t = time.struct_time((time.localtime().tm_year, time.localtime().tm_mon,
                                      time.localtime().tm_mday, t.tm_hour, t.tm_min, t.tm_sec,
                                      t.tm_wday, t.tm_yday, t.tm_isdst))
            return time.mktime(t)
        except ValueError:
            continue
    raise SystemExit(f"看不懂的时间: {s}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线自我诊断：日志 + 遥测 + 台账 → 根因排序")
    ap.add_argument("--log", action="append", default=[], help="文本日志，可重复")
    ap.add_argument("--cycles", help="soak cycles.jsonl")
    ap.add_argument("--metrics", help="metrics.csv")
    ap.add_argument("--ledger", help="台账 records.jsonl")
    ap.add_argument("--since", help="窗口起点（epoch 或 'YYYY-MM-DD HH:MM:SS'）")
    ap.add_argument("--until", help="窗口终点")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="规则目录")
    ap.add_argument("--artifact-dir", help="离线目录证据（如 soak 目录里的 .bad 残留）")
    ap.add_argument("--json", dest="json_path", help="把结果写成 JSON")
    ap.add_argument("--quiet", action="store_true", help="只打印结论行")
    args = ap.parse_args(argv)

    book = R.load_rules(args.rules, ROOT)
    if book.issues:
        print("规则自检未通过：", file=sys.stderr)
        for x in book.issues:
            print("  - " + x, file=sys.stderr)
        return 2

    bundle = sources.load_bundle(logs=args.log, cycles=args.cycles, metrics=args.metrics,
                                 ledger=args.ledger, root=ROOT)
    if not any((args.log, args.cycles, args.metrics, args.ledger)):
        print("至少给一个数据源", file=sys.stderr)
        return 2

    start, end = parse_time(args.since), parse_time(args.until)
    d = engine.diagnose(bundle, book, start, end, artifacts=args.artifact_dir)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(d.as_dict(), f, ensure_ascii=False, indent=2)

    if args.quiet:
        top = d.top
        print(f"{d.verdict}\t{top.label if top else '-'}\t{top.confidence if top else 0:.2f}")
    else:
        print(report.render(d))
    return 0 if d.verdict != "insufficient" else 1


if __name__ == "__main__":
    raise SystemExit(main())
