#!/usr/bin/env python
"""R1 A/B/AB/BA 台架：同一份冻结 corpus，交替跑 R0 快照 与 当前工作树。

- 只比同轮差值：base/cand/base/cand 四轮，逐轮记录 counts/metrics/耗时；
- core_digest 前后各算一次（本轮只改 evo/，core 应保持不变；若并行分支改了 core，
  记录漂移，但本轮结论不依赖 core）；
- 顺带生成 rollback patch（difflib 统一 diff，可用 git apply -R 反向）。
"""
import argparse
import difflib
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "tools", "evo_round1_probe.py")
FILES = [
    "evo/pool_contract.py", "evo/pool_inbox.py", "evo/funnel.py",
    "evo/attribute.py", "evo/observe.py", "evo/daemon.py", "evo/selftest.py",
]


def core_digest(root):
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(root, "core", "**", "*.py"),
                              recursive=True)):
        h.update(os.path.relpath(p, root).replace("\\", "/").encode())
        with open(p, "rb") as f:
            h.update(f.read())
    d = h.hexdigest()
    return {"sha256": d, "short16": d[:16]}


def run_probe(root, corpus, repeat):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cp = subprocess.run(
        [sys.executable, PROBE, "--root", root, "--corpus", corpus,
         "--repeat", str(repeat)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)
    if cp.returncode != 0:
        raise RuntimeError(f"probe failed root={root}: {cp.stderr[-300:]}")
    return json.loads(cp.stdout)


def make_patch(base_snap, out_path):
    chunks = []
    for rel in FILES:
        b = os.path.join(base_snap, rel)
        c = os.path.join(ROOT, rel)
        old = ""
        new = ""
        if os.path.exists(b):
            with open(b, "r", encoding="utf-8") as f:
                old = f.read()
        if os.path.exists(c):
            with open(c, "r", encoding="utf-8") as f:
                new = f.read()
        if old == new:
            continue
        header = []
        if not os.path.exists(b):
            header = [f"diff --git a/{rel} b/{rel}\n", "new file mode 100644\n",
                      "--- /dev/null\n", f"+++ b/{rel}\n"]
        else:
            header = [f"diff --git a/{rel} b/{rel}\n", f"--- a/{rel}\n",
                      f"+++ b/{rel}\n"]
        diff = list(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=rel, tofile=rel, n=3))
        hunks = diff[2:] if diff else []
        if hunks and hunks[0].startswith("@@"):
            chunks.extend(header)
            chunks.extend(hunks)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    text = "".join(chunks)
    if text and not text.endswith("\n"):
        text += "\n"
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-snap", required=True)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out-dir",
                    default=os.path.join(ROOT, "evo", "runs", "round1_20261107"))
    ap.add_argument("--repeat", type=int, default=20)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    before = core_digest(ROOT)
    git_core = subprocess.run(["git", "status", "--porcelain", "--", "core/"],
                              cwd=ROOT, capture_output=True, text=True)
    core_dirty = [x for x in git_core.stdout.splitlines() if x.strip()]

    order = ["base", "cand", "base", "cand"]
    roots = {"base": os.path.abspath(args.base_snap), "cand": ROOT}
    runs = []
    for i, who in enumerate(order, 1):
        t0 = time.time()
        out = run_probe(roots[who], os.path.abspath(args.corpus), args.repeat)
        runs.append({"round": i, "arm": who, "started": round(t0, 3),
                     "result": out})
        print(f"[{i}/4] {who}: counts={out['counts']} "
              f"pool_rate={out['metrics']['pool_rate']} "
              f"p95={out['wall_ms']['p95']:.3f}ms", flush=True)

    after = core_digest(ROOT)
    by_arm = {}
    for who in ("base", "cand"):
        xs = [r["result"] for r in runs if r["arm"] == who]
        by_arm[who] = {
            "counts": xs[0]["counts"],
            "metrics": xs[0]["metrics"],
            "stages_n": xs[0]["stages_n"],
            "wall_p95_ms": sum(x["wall_ms"]["p95"] for x in xs) / len(xs),
            "runs": [{"counts": x["counts"], "pool_rate": x["metrics"]["pool_rate"],
                      "wall_p95_ms": x["wall_ms"]["p95"]} for x in xs],
        }
    b, c = by_arm["base"], by_arm["cand"]
    phantom_removed = int(b["counts"].get("pool", 0)) - int(c["counts"].get("pool", 0))
    delta = {
        "pool_count": int(c["counts"].get("pool", 0)) - int(b["counts"].get("pool", 0)),
        "pool_rate_pp": (c["metrics"]["pool_rate"] - b["metrics"]["pool_rate"]) * 100,
        "pool_unknown_count": int(c["metrics"].get("pool_unknown_count") or 0),
        "phantom_pool_removed": phantom_removed,
        "stages_n": c["stages_n"] - b["stages_n"],
        "hit_count": int(c["counts"].get("hit", 0)) - int(b["counts"].get("hit", 0)),
        "deal_count": int(c["counts"].get("deal", 0)) - int(b["counts"].get("deal", 0)),
        "wall_p95_ms": c["wall_p95_ms"] - b["wall_p95_ms"],
        "wall_p95_rel": (c["wall_p95_ms"] / b["wall_p95_ms"] - 1.0)
                        if b["wall_p95_ms"] else None,
    }

    patch = make_patch(os.path.abspath(args.base_snap),
                       os.path.join(args.out_dir, "impl_round1.patch"))
    check = subprocess.run(["git", "apply", "--check", "-R", patch],
                           cwd=ROOT, capture_output=True, text=True)

    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "corpus": os.path.abspath(args.corpus),
        "base_snap": os.path.abspath(args.base_snap),
        "repeat_per_run": args.repeat,
        "order": order,
        "core_digest_before": before,
        "core_digest_after": after,
        "core_digest_stable": before == after,
        "core_dirty_files_seen": core_dirty,
        "by_arm": by_arm,
        "delta": delta,
        "runs": runs,
        "patch": patch,
        "patch_reverse_check": {
            "returncode": check.returncode,
            "stdout": check.stdout.strip(),
            "stderr": check.stderr.strip(),
        },
    }
    with open(os.path.join(args.out_dir, "ab_runs.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("\n=== 汇总（同轮差值） ===")
    for k, v in delta.items():
        print(f"{k}: {v}")
    print(f"core_digest before={before['short16']} after={after['short16']} "
          f"stable={before == after} core_dirty={len(core_dirty)}")
    print(f"patch={patch} reverse_check_rc={check.returncode}")


if __name__ == "__main__":
    main()
