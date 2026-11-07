"""启动到首个商品可处理：把「冷启动」放在明处。

分段计时（全新进程）：
  import_ms   —— 导入 core + 钉配置
  warmup_ms   —— ocr.warmup()（补丁 02 才有；没有就记 0）
  first_item_ms —— 预热后的第一件商品识别（一卡三字段）
  total_ms    —— 三者之和 = 从进程启动到首件可处理

用法：
  python perf/bench/startup.py --snapshot <快照> --out out/startup.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if BENCH_DIR not in sys.path:
    sys.path.insert(0, BENCH_DIR)

import common  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="启动到首件可处理总时长")
    p.add_argument("--snapshot", default=".")
    p.add_argument("--out", default=os.path.join(BENCH_DIR, "out", "startup.json"))
    p.add_argument("--runs", type=int, default=1)
    args = p.parse_args(argv)

    root = common.load_snapshot(args.snapshot)
    results = []
    for _ in range(max(1, args.runs)):
        t0 = common.now()
        mods = common.import_core(root)
        cfg = common.load_json(os.path.join(BENCH_DIR, "bench_config.json"))
        cfg_path, cfg_digest = common.pin_config(
            mods["config"], mods["ocr"], cfg.get("config_overrides"))
        import_ms = (common.now() - t0) * 1000.0

        ocr = mods["ocr"]
        warm = getattr(ocr, "warmup", None)
        warmup_ms = None
        if callable(warm):
            t = common.now()
            try:
                warm()
            except Exception as e:  # noqa: BLE001
                warmup_ms = None
                print(f"[startup] warmup 失败：{e}")
            else:
                warmup_ms = (common.now() - t) * 1000.0

        # 补丁 04 的二级自检在引擎启动里跑一次真推理。它同样是「首件之前」的
        # 开销，必须单独计时并计入 total，否则等于把它藏到看不见的地方。
        sc = getattr(ocr, "selfcheck_pp", None)
        selfcheck_ms = None
        selfcheck_detail = ""
        if callable(sc):
            t = common.now()
            try:
                ok, err = sc()
                selfcheck_detail = "ok" if ok else str(err)[:80]
            except Exception as e:  # noqa: BLE001
                selfcheck_detail = f"{type(e).__name__}: {e}"[:80]
            selfcheck_ms = (common.now() - t) * 1000.0

        mats = common.materials(root)
        list_img = mods["vision"].imread(os.path.join(mats["market"], "list_unfiltered.png"))
        found = mods["cards"].locate_cards(list_img)
        rect = found[0]
        t = common.now()
        rows = mods["market"]._scan_cards(list_img, [rect])
        first_item_ms = (common.now() - t) * 1000.0

        total = import_ms + (warmup_ms or 0.0) + (selfcheck_ms or 0.0) + first_item_ms
        results.append({
            "import_ms": round(import_ms, 1),
            "warmup_ms": round(warmup_ms, 1) if warmup_ms is not None else None,
            "has_warmup": callable(warm),
            "selfcheck_ms": round(selfcheck_ms, 1) if selfcheck_ms is not None else None,
            "has_selfcheck": callable(sc),
            "selfcheck_detail": selfcheck_detail,
            "first_item_ms": round(first_item_ms, 1),
            "total_ms": round(total, 1),
            "rows": len(rows),
            "core_digest": common.core_digest(root),
            "config_sha256": cfg_digest[:16],
            "packages": common.env_packages(),
            "python_exe": sys.executable,
        })

    doc = {"snapshot": root,
           "cpu_load": common.cpu_load(),
           "runs": results}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
