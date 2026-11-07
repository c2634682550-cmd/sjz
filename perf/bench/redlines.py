"""红线旁证采集：卡点 P95、误买防护、输入身份。

这三项不在基准套件里造数，由各自专项/测试给出证据；本脚本只做汇总，
缺失就是缺失 —— 守门按 fail-closed 处理，不猜。

用法：
  python perf/bench/redlines.py --snapshot . --out out/redlines.json
  python perf/bench/redlines.py --point-p95 path/to/point_p95.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if BENCH_DIR not in sys.path:
    sys.path.insert(0, BENCH_DIR)

import common  # noqa: E402

GUARD_TESTS = (
    "tests/test_precision.py",
    "tests/test_engine_insufficient.py",
    "tests/test_numeric_hardening.py",
)
IDENTITY_TESTS = ("tests/test_input_channels.py",)


def _pytest(root, files, timeout=600):
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"] + list(files),
            cwd=root, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e), "exit": None}
    tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    return {"ok": proc.returncode == 0, "exit": proc.returncode, "detail": tail[0]}


def _point_p95(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        try:
            doc = json.loads(txt)
        except json.JSONDecodeError:
            return float(txt)
        for key in ("point_p95_ms", "p95_ms", "trusted_p95_abs_ms", "p95_abs_ms"):
            if isinstance(doc, dict) and doc.get(key) is not None:
                return float(doc[key])
        if isinstance(doc, (int, float)):
            return float(doc)
        return None
    except Exception:  # noqa: BLE001
        return None


def collect(root, mods, point_p95_path="", run_tests=True):
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot": root,
    }

    # 输入身份：通道不能降级
    cfg_live = {}
    live_path = os.path.join(root, "config.json")
    if os.path.exists(live_path):
        try:
            cfg_live = common.load_json(live_path)
        except Exception:  # noqa: BLE001
            cfg_live = {}
    mode = str(cfg_live.get("input_mode") or mods["config"].DEFAULTS.get("input_mode") or "")
    try:
        ready, why = mods["interception"].driver_ready(probe=False)
    except Exception as e:  # noqa: BLE001
        ready, why = False, str(e)
    identity = "interception" if (mode == "interception" and ready) else (mode or "legacy")
    out["input_identity"] = identity
    out["input_driver_ready"] = bool(ready)
    out["input_detail"] = why

    # 卡点 P95：由速度线 A 产出
    out["point_p95_ms"] = _point_p95(point_p95_path)

    # 误买防护：红线测试必须全绿
    if run_tests:
        guard = _pytest(root, GUARD_TESTS)
        ident = _pytest(root, IDENTITY_TESTS)
        out["misbuy_guard"] = bool(guard["ok"])
        out["input_identity_guard"] = bool(ident["ok"])
        out["tests"] = {"misbuy": guard, "input": ident}
    else:
        out["misbuy_guard"] = None
        out["input_identity_guard"] = None
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="红线旁证采集")
    p.add_argument("--snapshot", default=".")
    p.add_argument("--out", default=os.path.join(BENCH_DIR, "out", "redlines.json"))
    p.add_argument("--point-p95", default="", help="速度线 A 的卡点 P95 结果文件")
    p.add_argument("--skip-tests", action="store_true")
    args = p.parse_args(argv)

    root = common.load_snapshot(args.snapshot)
    mods = common.import_core(root)
    doc = collect(root, mods, args.point_p95, run_tests=not args.skip_tests)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    print(f"\n[redlines] 写入 {args.out}")
    return 0 if doc.get("misbuy_guard") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
