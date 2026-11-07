#!/usr/bin/env python
"""A/B 探针：在指定 root 下 import evo.funnel，跑一份冻结 corpus，输出 JSON。

用途：不改动被测代码，用 --root 切换 R0 快照 / 当前工作树，跑同一份 corpus，
只比同轮差值。每次进程独立，wall_ms 反映冷启动 + 构建耗时。
"""
import argparse
import json
import os
import sys
import time


def p95(xs):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, int(round(0.95 * (len(xs) - 1))))
    return xs[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--repeat", type=int, default=20)
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    sys.path.insert(0, root)

    from evo.config import load_config
    from evo import funnel

    cfg = load_config()
    with open(args.corpus, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    samples = []
    out = None
    for _ in range(max(1, args.repeat)):
        t0 = time.perf_counter()
        out = funnel.build(cfg, bundle)
        samples.append((time.perf_counter() - t0) * 1000.0)

    batch = out["batch"]
    win = out["window"]
    result = {
        "root": root,
        "funnel_module": os.path.abspath(funnel.__file__),
        "stages_n": len(out["window"]["stages"]),
        "stage_keys": [s["stage"] for s in out["window"]["stages"]],
        "counts": win["counts"],
        "metrics": {
            k: win["metrics"].get(k) for k in (
                "list_rate", "detail_rate", "click_rate", "pool_rate",
                "pool_rate_upper", "pool_unknown_rate", "pool_unknown_count",
                "phantom_pool_count", "hit_rate", "claimed_hit_rate",
                "settle_rate", "end_to_end_rate", "unverified_hit_rate",
                "won_conflict_count")
        },
        "batch_unknown": batch.get("pool_unknown"),
        "batch_conflicts": batch.get("won_conflicts"),
        "stages": win["stages"],
        "wall_ms": {
            "repeat": len(samples),
            "p50": sorted(samples)[len(samples) // 2],
            "p95": p95(samples),
            "min": min(samples),
            "max": max(samples),
            "samples": [round(x, 3) for x in samples],
        },
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
