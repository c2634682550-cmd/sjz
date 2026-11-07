"""基准套件入口。

用法（在任意目录执行，指定快照即可）：

  python perf/bench/run.py --snapshot . 
  python perf/bench/run.py --snapshot D:\\snapshots\\v2 --groups ocr,click
  python perf/bench/run.py --snapshot . --freeze-baseline

输出：perf/bench/out/<时间戳>/perf.json + perf.md
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
import traceback

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if BENCH_DIR not in sys.path:
    sys.path.insert(0, BENCH_DIR)

import click_group  # noqa: E402
import common  # noqa: E402
import flow_group  # noqa: E402
import ocr_group  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="dafors 性能基准与守门套件")
    p.add_argument("--snapshot", default=".", help="代码快照目录（含 core/）")
    p.add_argument("--out", default=os.path.join(BENCH_DIR, "out"), help="结果输出根目录")
    p.add_argument("--groups", default="ocr,click,flow", help="要跑的组，逗号分隔")
    p.add_argument("--tag", default="", help="结果目录后缀，便于区分")
    p.add_argument("--seed", type=int, default=20261107, help="随机种子（写进 meta）")
    p.add_argument("--allow-inject", action="store_true",
                   help="允许发零位移相对事件测物理层往返（不动光标）；默认关闭")
    p.add_argument("--freeze-baseline", action="store_true",
                   help="把本次结果同时冻结为 perf/bench/baseline.json")
    p.add_argument("--with-redlines", action="store_true",
                   help="同时采集红线旁证（误买防护测试 / 输入身份 / 卡点 P95 文件）")
    p.add_argument("--point-p95", default="", help="速度线 A 的卡点 P95 结果文件")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = common.load_json(os.path.join(BENCH_DIR, "bench_config.json"))
    random.seed(args.seed)

    root = common.load_snapshot(args.snapshot)
    mods = common.import_core(root)
    cfg_path, cfg_digest = common.pin_config(
        mods["config"], mods["ocr"], cfg.get("config_overrides"))

    bench_sha = common.bench_digest(BENCH_DIR)
    meta = common.env_fingerprint(root, cfg_digest, BENCH_DIR)
    meta["command"] = " ".join([os.path.basename(sys.executable)] + sys.argv)
    meta["argv"] = sys.argv[1:]
    meta["seed"] = args.seed
    meta["groups"] = args.groups
    meta["pinned_config_file"] = cfg_path
    meta["cpu_load_start"] = common.cpu_load()
    print(f"[bench] 快照 {root}")
    print(f"[bench] git {meta['git_head']} / 套件 {bench_sha} / 配置 {cfg_digest[:12]}")

    wanted = [g.strip() for g in args.groups.split(",") if g.strip()]
    groups = {}
    extras = {}
    notes = {}
    accuracy = {}

    runners = {"ocr": ocr_group, "click": click_group, "flow": flow_group}

    for name in wanted:
        runner = runners.get(name)
        if runner is None:
            groups[name] = {"error": f"未知组：{name}"}
            continue
        t0 = common.now()
        try:
            out = runner.run(mods, root, args, cfg)
            groups[name] = out["metrics"]
            extras[name] = out.get("extra") or {}
            notes.update({f"{name}.{k}": v for k, v in (out.get("notes") or {}).items()})
            if out.get("accuracy"):
                accuracy.update(out["accuracy"])
            print(f"[bench] {name} 完成 { (common.now() - t0) * 1000:.0f} ms")
        except Exception as e:  # noqa: BLE001
            groups[name] = {"error": f"{type(e).__name__}: {e}"}
            notes[f"{name}.error"] = traceback.format_exc(limit=3)
            print(f"[bench] {name} 失败：{e}")

    meta["cpu_load_end"] = common.cpu_load()
    meta["core_digest_end"] = common.core_digest(root)
    if meta["core_digest_end"] != meta["core_digest"]:
        meta["invalid"] = "bench 期间 core/ 被改动，本轮结果作废"
        print("[bench] 警告：跑的过程中 core/ 变了，结果不可比，需在静止快照上重跑")
    result = {
        "meta": meta,
        "groups": groups,
        "extra": extras,
        "accuracy": accuracy,
        "notes": notes,
    }

    if args.with_redlines:
        import redlines as redlines_mod
        try:
            result["redlines"] = redlines_mod.collect(
                root, mods, args.point_p95, run_tests=True)
            print("[bench] 红线旁证已采集")
        except Exception as e:  # noqa: BLE001
            result["redlines"] = {"error": f"{type(e).__name__}: {e}"}
            print(f"[bench] 红线旁证采集失败：{e}")
    stem = "perf" + (f"_{args.tag}" if args.tag else "")
    outdir = os.path.join(args.out, time.strftime("%Y%m%d_%H%M%S"))
    json_path, md_path = common.write_outputs(result, outdir, stem)

    if args.freeze_baseline:
        frozen = os.path.join(BENCH_DIR, "baseline.json")
        with open(frozen, "w", encoding="utf-8") as f:
            import json
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[bench] 基线已冻结 {frozen}")

    for name, group in groups.items():
        print(f"\n== {name} ==")
        for metric, value in group.items():
            if isinstance(value, dict) and value.get("p50") is not None:
                p95 = value.get("p95")
                maxv = value.get("max")
                p99 = value.get("p99")
                tail = f"p95={p95:9.2f}  max={maxv:9.2f}" if p95 is not None and maxv is not None else ""
                if p95 is not None:
                    tail = f"p95={p95:8.2f} p99={p99 if p99 is not None else float('nan'):8.2f} max={maxv:8.2f}"
                print(f"  {metric:22s} n={value['n']:>3}  p50={value['p50']:9.2f}  "
                      f"{tail} {value['unit']}".rstrip())
            elif isinstance(value, dict) and "error" in value:
                print(f"  {name} 错误：{value['error']}")
    if accuracy:
        print("\n== 准确率 ==")
        for k, v in accuracy.items():
            if isinstance(v, dict):
                print(f"  {k:24s} {v['value']}  (n={v['n']})")
    print(f"\n[bench] 结果：{json_path}\n[bench] 表格：{md_path}")
    return 0


if __name__ == "__main__":
    code = main()
    # ORT 的线程池/atexit 在部分版本里会挂住进程（已实测：结果写完、CPU 不再
    # 增长，进程却永远不退出）。基准是纯读操作，写完就硬退，别把整条队列拖死。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
