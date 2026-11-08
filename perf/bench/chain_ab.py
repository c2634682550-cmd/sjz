"""A/B 交替跑主链路，抵消机器负载漂移。

同一个进程里交替 base / reuse / fast / both，每组 2 条链路、重复 3 轮，
取每组的 p50。比「四个独立进程各跑一次」可靠得多。
"""
from __future__ import annotations

import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "perf", "bench"))

import chain_bench  # noqa: E402

CELLS = {
    "base": (False, False),
    "reuse": (True, False),
    "fast": (False, True),
    "both": (True, True),
}


def main():
    rounds = 3
    res = {k: [] for k in CELLS}
    detail = {k: [] for k in CELLS}
    for _ in range(rounds):
        for name, (reuse, fast) in CELLS.items():
            out = chain_bench.run(rep=2, reuse=reuse, fast=fast, quiet=True)
            res[name].extend(out["end_to_end_ms"])
            detail[name].append(out)
    print(json.dumps({
        "e2e": {k: {"p50": round(statistics.median(v), 1),
                    "min": round(min(v), 1), "n": len(v)}
                for k, v in res.items()},
        "reuse_hits": {k: [d["ocr"].get("reuse", 0) for d in v]
                       for k, v in detail.items()},
        "phase_avg": {k: {p: round(sum(d["phases"][p]["avg_ms"] * d["phases"][p]["n"]
                                       for d in v) / max(1, sum(d["phases"][p]["n"]
                                                                for d in v)), 1)
                          for p in v[0]["phases"]}
                      for k, v in detail.items()},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
