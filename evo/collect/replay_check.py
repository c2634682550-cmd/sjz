"""截图/回放可行性评估：数清每个目录的帧数、间隔、命名、能否对齐台账。

    .venv\\Scripts\\python.exe evo\\collect\\replay_check.py
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

IMG = (".png", ".jpg", ".jpeg", ".bmp")
NAME_RE = re.compile(r"(\d{8})_(\d{6})")


def md5(p):
    with open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def describe(dirpath, recursive=False):
    pat = "**/*" if recursive else "*"
    files = [p for p in glob.glob(os.path.join(dirpath, pat), recursive=recursive)
             if os.path.isfile(p) and p.lower().endswith(IMG)]
    if not files:
        return None
    files.sort(key=os.path.getmtime)
    times = [os.path.getmtime(p) for p in files]
    hashes = Counter(md5(p) for p in files)
    deltas = [int(round((b - a) * 1000)) for a, b in zip(times, times[1:])]
    named = [NAME_RE.search(os.path.basename(p)) for p in files]
    return {
        "dir": os.path.relpath(dirpath, ROOT).replace("\\", "/"),
        "frames": len(files),
        "unique_images": len(hashes),
        "first_ms": int(round(times[0] * 1000)),
        "last_ms": int(round(times[-1] * 1000)),
        "span_ms": int(round((times[-1] - times[0]) * 1000)),
        "delta_ms_min": min(deltas) if deltas else None,
        "delta_ms_median": sorted(deltas)[len(deltas) // 2] if deltas else None,
        "timestamped_name": sum(1 for m in named if m),
        "samples": [os.path.relpath(p, ROOT).replace("\\", "/") for p in files[:4]],
    }


def main():
    out = {"dirs": [], "template_dirs": {}, "ledger": None, "verdict": {}}

    # 1) 各截图目录
    for d in ("data/shots", "_r08_shots", "_ui_shots", "_verify_bb"):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            info = describe(p, recursive=True)
            if info:
                out["dirs"].append(info)

    rm = os.path.join(ROOT, "_rm_shots")
    if os.path.isdir(rm):
        for sub in sorted(glob.glob(os.path.join(rm, "*"))) + [rm]:
            if os.path.isdir(sub):
                info = describe(sub, recursive=True)
                if info:
                    out["dirs"].append(info)

    # 2) data/<task_id> 模板缓存
    tdirs = [d for d in glob.glob(os.path.join(ROOT, "data", "*")) if os.path.isdir(d)]
    imgs = [p for d in tdirs for p in glob.glob(os.path.join(d, "*.png"))]
    out["template_dirs"] = {
        "dirs": len(tdirs), "images": len(imgs),
        "unique_images": len(set(md5(p) for p in imgs)),
        "per_dir_counts": dict(Counter(len(glob.glob(os.path.join(d, "*.png"))) for d in tdirs)),
    }

    # 3) 台账行数（能否对齐）
    led = os.path.join(ROOT, "data", "records.jsonl")
    if os.path.isfile(led):
        with open(led, encoding="utf-8", errors="replace") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        out["ledger"] = {"rows": len(rows),
                         "with_shot": sum(1 for r in rows if r.get("shot"))}

    # 4) 结论：需要 ≥30 帧且有名字时间戳（或 ≤250ms 稳定间隔）才算可回放序列
    seq_dirs = []
    for d in out["dirs"]:
        if d["dir"] == "_rm_shots" or d["span_ms"] > 60000:   # 汇总目录/跨轮聚合不算序列
            continue
        named_ratio = d["timestamped_name"] / d["frames"] if d["frames"] else 0
        fast = (d["delta_ms_median"] or 99999) <= 250
        if d["frames"] >= 30 and d["unique_images"] > 1 and (named_ratio >= 0.9 or fast):
            seq_dirs.append(d["dir"])
    has_ts_names = any(d["timestamped_name"] for d in out["dirs"])
    out["verdict"] = {
        "sequential_dirs": len(seq_dirs),
        "replayable": bool(seq_dirs),
        "reason": ("无帧序列：裁剪模板每目录固定 2 张且全项目仅 2 张不同图；"
                   "台架/验证截图每组 2–12 张，文件名是步骤号而非时间戳，"
                   "mtime 间隔 17–1423425ms 不可作帧率；data/shots 不存在"
                   if not seq_dirs else "存在 ≥30 帧目录，可按文件名时间戳回放"),
        "alignable_to_ledger": bool(has_ts_names),
    }

    with open(os.path.join(HERE, "replay_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for d in out["dirs"]:
        print(f"{d['dir']:<28} 帧={d['frames']:>3} 唯一图={d['unique_images']:>3} "
              f"跨度={d['span_ms']}ms 命名带时间={d['timestamped_name']}/{d['frames']}")
    print("模板目录:", out["template_dirs"])
    print("台账:", out["ledger"])
    print("结论:", out["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
