"""自检：坏行隔离 / 幂等 / 窗口重跑。只读现有产物，不写项目文件。

    .venv\\Scripts\\python.exe evo\\collect\\selftest.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from evo.collect import collector, parse  # noqa: E402


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest() if os.path.exists(path) else ""


def main():
    ok = True

    # 1) 坏行只影响自身
    cases = [b'{bad json', b'[]', b'{"ts":"x"}', b'{"ts":null}',
             b'{"ts":1794010319.179,"name":"","price":null,"result":"hit"}']
    for raw in cases:
        try:
            ev = parse.parse_ledger_line(raw, "data/records.jsonl", 99)
            assert ev["event_id"], "缺 event_id"
            print(f"  解析通过: {raw[:40]!r} -> {ev['result_norm']}/{ev['stage_reached']}")
        except Exception as e:                       # noqa: BLE001
            if raw.startswith(b'{"ts":1794010319'):
                ok = False
                print(f"  合法行被误判: {e}")
            else:
                print(f"  坏行已隔离: {raw[:30]!r} -> {type(e).__name__}")

    # 2) 幂等：连跑两次，events.jsonl 字节一致
    collector.run(["--rebuild", "--with-harness", "--quiet"])
    d1 = digest(collector.OUT_EVENTS)
    collector.run(["--rebuild", "--with-harness", "--quiet"])
    d2 = digest(collector.OUT_EVENTS)
    print(f"  幂等: {'一致' if d1 == d2 else '不一致'} ({d1[:12]} -> {d2[:12]})")
    ok &= d1 == d2

    # 3) 增量：跑一次不新增（游标已到文件尾）
    n1 = len(collector._read_jsonl(collector.OUT_EVENTS))
    collector.run(["--with-harness", "--quiet"])
    n2 = len(collector._read_jsonl(collector.OUT_EVENTS))
    print(f"  增量无新增: {n1} -> {n2}")
    ok &= n1 == n2

    # 4) 窗口：只保留 records 那一行所在秒
    one = json.loads(open(collector.OUT_EVENTS, encoding="utf-8").readline())
    ts = one["ts_ms"]
    collector.run(["--rebuild", "--with-harness", "--quiet",
                   "--since", str(ts), "--until", str(ts + 1)])
    rows = collector._read_jsonl(collector.OUT_EVENTS)
    inside = all(r["ts_ms"] is None or ts <= r["ts_ms"] <= ts + 1 for r in rows)
    print(f"  窗口重跑: {len(rows)} 条，全部在窗口内={inside}")
    ok &= inside

    collector.run(["--rebuild", "--with-harness", "--quiet"])   # 复位全量
    print("自检", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
