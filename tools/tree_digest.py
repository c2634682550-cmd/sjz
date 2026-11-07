#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工作树指纹工具：core_digest / 时间线 / 两次指纹之间的差异解释。

算法与 perf/bench/common.py::core_digest 完全一致（不要改口径）：
    sha256( for name in sorted(core/*.py): name.encode() + 文件字节 ).hexdigest()[:16]

用法：
    python tools/tree_digest.py                     # 打印当前 core_digest
    python tools/tree_digest.py --files             # 附每个 core/*.py 的大小/哈希/mtime
    python tools/tree_digest.py --record            # 记一条时间线（改动前打点）
    python tools/tree_digest.py --watch --interval 5
    python tools/tree_digest.py --explain OLD NEW   # 两次指纹之间哪些文件变了、变了几字节
    python tools/tree_digest.py --scope all --root <树>   # 同一算法扩到源码全集（验证合并是否收敛）

时间线默认写到 <root>/_tree_timeline.jsonl（已加入 .gitignore）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 复制/合并/时间线共用的排除口径：这些目录不是源码，且体积大或在被别的会话写。
EXCLUDE_DIR_NAMES = {
    ".git", ".venv", ".venv-vl", "__pycache__", ".pytest_cache",
    "build", "dist", "dist-installer", "dist-onefile", "node_modules",
}
EXCLUDE_REL_DIRS = {
    "data", "_bmad", "_bmad-output", "_reverse", "_rm_backup", "_rm_shots",
    "_r08_shots", "_ui_shots", "_calib_thumbs", "_verify_bb", "_fe_old_backup",
    "_timing_lab", "_soak_data",
    "launcher/.tmp",
    "perf/snapshots", "perf/ocr", "perf/base", "perf/_ref", "perf/merged",
    "perf/merged_pre", "perf/out", "perf/bench/out", "evo/runs",
}
# 可做 3-way 文本合并、并且值得留基线副本的扩展名。
MERGE_EXTS = {
    ".py", ".ps1", ".bat", ".cmd", ".spec", ".toml", ".yaml", ".yml",
    ".json", ".md", ".html", ".css", ".js", ".txt", ".ini", ".cfg", ".xml",
}
BASE_MAX_BYTES = 512 * 1024
MANIFEST_MAX_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------- 基础工具

def _is_excluded_rel(rel_posix):
    if rel_posix in EXCLUDE_REL_DIRS:
        return True
    head = rel_posix.split("/", 1)[0]
    return head in EXCLUDE_DIR_NAMES


def iter_files(root, exts=None, max_bytes=None):
    """按统一排除口径遍历文件，产出 (相对 posix 路径, 绝对路径, 大小)。"""
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        kept = []
        for d in dirnames:
            rel = (rel_dir + "/" + d) if rel_dir else d
            if d in EXCLUDE_DIR_NAMES or _is_excluded_rel(rel):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            rel = (rel_dir + "/" + name) if rel_dir else name
            if _is_excluded_rel(rel):
                continue
            if exts is not None and os.path.splitext(name)[1].lower() not in exts:
                continue
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if max_bytes is not None and size > max_bytes:
                continue
            yield rel, path, size


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def core_files(root):
    """与 perf/bench/common.py::core_digest 相同的取文件方式：core/ 顶层 *.py，按名排序。"""
    core_dir = os.path.join(root, "core")
    if not os.path.isdir(core_dir):
        raise SystemExit("目录里没有 core/：%s" % root)
    return sorted(n for n in os.listdir(core_dir) if n.endswith(".py"))


def compute_digest(root, scope="core"):
    """返回 (digest, {rel: {sha256,size,mtime_ns}})。core 口径与台架逐字节一致。"""
    root = os.path.abspath(root)
    h = hashlib.sha256()
    files = {}
    if scope == "core":
        core_dir = os.path.join(root, "core")
        for name in core_files(root):
            path = os.path.join(core_dir, name)
            st = os.stat(path)
            with open(path, "rb") as f:
                blob = f.read()
            h.update(name.encode())
            h.update(blob)
            files["core/" + name] = {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
            }
    else:
        skip_meta = {"SNAPSHOT.txt", "SNAPSHOT.files.json",
                     "_tree_timeline.jsonl", "_merge_log.jsonl"}
        for rel, path, size in sorted(iter_files(root, max_bytes=MANIFEST_MAX_BYTES)):
            # 快照元数据/运行日志不是源码，不能进 all 口径，否则 worktree 与主树永远对不齐。
            if rel in skip_meta or rel.startswith("_snapshot_base/"):
                continue
            if rel.lower().endswith((".log", ".pyc")):
                continue
            try:
                with open(path, "rb") as f:
                    blob = f.read()
                st = os.stat(path)
            except OSError:
                continue
            h.update(rel.encode())
            h.update(blob)
            files[rel] = {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
            }
    return h.hexdigest()[:16], files


def git_meta(root):
    def run(args):
        try:
            out = subprocess.run(["git", "-C", root] + args, capture_output=True,
                                 text=True, timeout=8)
            return out.stdout.strip()
        except Exception:
            return ""
    head = run(["rev-parse", "--short", "HEAD"])
    dirty = run(["status", "--porcelain", "--", "core"])
    return head or None, len([x for x in dirty.splitlines() if x.strip()])


def now_iso(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch).astimezone().isoformat(
        timespec="seconds")


def build_record(root, scope, event):
    digest, files = compute_digest(root, scope)
    head, dirty = git_meta(root)
    ts = time.time()
    return {
        "ts": now_iso(ts),
        "epoch": round(ts, 3),
        "event": event,
        "scope": scope,
        "root": os.path.abspath(root),
        "git_head": head,
        "core_dirty": dirty,
        "digest": digest,
        "file_count": len(files),
        "files": files,
    }


def append_record(path, rec):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------- 时间线 / 解释

def load_timeline(path, scope):
    recs = []
    if not os.path.exists(path):
        return recs
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("scope", "core") != scope:
                continue
            recs.append(rec)
    return recs


def explain(timeline_path, scope, old, new, out):
    recs = load_timeline(timeline_path, scope)
    if not recs:
        raise SystemExit("时间线为空或不含 scope=%s 的记录：%s" % (scope, timeline_path))
    old_rec = None
    for rec in recs:
        if rec.get("digest") == old:
            old_rec = rec  # 取最后一次处于 OLD 的记录 = 变化前一刻
    if old_rec is None:
        raise SystemExit("时间线里找不到 OLD=%s；先用 --record 在改动前打点" % old)
    new_rec = None
    for rec in recs:
        if rec.get("digest") == new and rec.get("epoch", 0) >= old_rec.get("epoch", 0):
            new_rec = rec
            break
    if new_rec is None:
        for rec in recs:
            if rec.get("digest") == new:
                new_rec = rec
                break
    if new_rec is None:
        raise SystemExit("时间线里找不到 NEW=%s" % new)

    of = old_rec.get("files", {})
    nf = new_rec.get("files", {})
    rows = []
    for rel in sorted(set(of) | set(nf)):
        a = of.get(rel)
        b = nf.get(rel)
        if a is None:
            rows.append((rel, "added", 0, b["size"], b["size"], b.get("mtime_ns", 0)))
        elif b is None:
            rows.append((rel, "removed", a["size"], 0, -a["size"], a.get("mtime_ns", 0)))
        elif a["sha256"] != b["sha256"]:
            rows.append((rel, "content", a["size"], b["size"], b["size"] - a["size"],
                         b.get("mtime_ns", 0)))
        elif a.get("mtime_ns") != b.get("mtime_ns"):
            rows.append((rel, "mtime", a["size"], b["size"], 0, b.get("mtime_ns", 0)))
    rows.sort(key=lambda r: (abs(r[4]), r[0]), reverse=True)

    out.append("OLD %s  %s  (event=%s, git=%s)" %
               (old, old_rec.get("ts"), old_rec.get("event"), old_rec.get("git_head")))
    out.append("NEW %s  %s  (event=%s, git=%s)" %
               (new, new_rec.get("ts"), new_rec.get("event"), new_rec.get("git_head")))
    if not rows:
        out.append("两次指纹之间没有任何文件差异（scope=%s）。" % scope)
        return 0
    out.append("")
    out.append("%-42s %-8s %10s %10s %9s" % ("file", "status", "old_B", "new_B", "delta_B"))
    for rel, status, sa, sb, delta, _m in rows:
        out.append("%-42s %-8s %10d %10d %+9d" % (rel, status, sa, sb, delta))
    out.append("")
    out.append("共 %d 个文件、净变化 %+d 字节。" % (len(rows), sum(r[4] for r in rows)))
    return 0


# ---------------------------------------------------------------- 快照清单

def emit_manifest(root, manifest_path, base_dir):
    """从 root（已复制好的快照）导出逐文件基线哈希；并为可文本合并的文件留基线副本。"""
    root = os.path.abspath(root)
    manifest_path = os.path.abspath(manifest_path)
    files = {}
    for rel, path, size in iter_files(root, max_bytes=MANIFEST_MAX_BYTES):
        if rel in ("SNAPSHOT.txt", "SNAPSHOT.files.json", "_tree_timeline.jsonl",
                   "_merge_log.jsonl") or rel.startswith("_snapshot_base/"):
            continue
        try:
            files[rel] = {"sha256": sha256_file(path), "size": size}
        except OSError:
            continue

    base_count = 0
    if base_dir:
        base_dir = os.path.abspath(base_dir)
        os.makedirs(base_dir, exist_ok=True)
        for rel, path, size in iter_files(root, exts=MERGE_EXTS, max_bytes=BASE_MAX_BYTES):
            if rel.startswith("_snapshot_base/"):
                continue
            dst = os.path.join(base_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(path, "rb") as f:
                blob = f.read()
            if b"\x00" in blob[:8192]:
                continue  # 二进制不做行级合并
            with open(dst, "wb") as f:
                f.write(blob)
            base_count += 1

    digest, _ = compute_digest(root, "core")
    head, dirty = git_meta(root)
    manifest = {
        "schema": 1,
        "created_at": now_iso(),
        "root": root,
        "git_head": head,
        "core_dirty": dirty,
        "core_digest": digest,
        "merge_exts": sorted(MERGE_EXTS),
        "base_max_bytes": BASE_MAX_BYTES,
        "base_blob_count": base_count,
        "file_count": len(files),
        "files": files,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, sort_keys=True, indent=1)
    return manifest


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="工作树指纹：core_digest / 时间线 / 差异解释")
    ap.add_argument("--root", default=REPO_ROOT, help="要计算指纹的树（默认当前仓库）")
    ap.add_argument("--scope", choices=("core", "all"), default="core",
                    help="core=与台架一致的口径；all=同一算法扩到全部源码（用于合并收敛验证）")
    ap.add_argument("--timeline", default=None, help="时间线路径，默认 <root>/_tree_timeline.jsonl")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--files", action="store_true", help="列出每个文件的哈希/大小/mtime")
    ap.add_argument("--record", action="store_true", help="向时间线追加一条记录")
    ap.add_argument("--watch", action="store_true", help="按间隔轮询并持续追加时间线")
    ap.add_argument("--interval", type=float, default=5.0, help="--watch 轮询间隔秒")
    ap.add_argument("--duration", type=float, default=0.0, help="--watch 总时长秒，0=一直跑")
    ap.add_argument("--only-changes", action="store_true", help="--watch 只在指纹变化时落盘")
    ap.add_argument("--explain", nargs=2, metavar=("OLD", "NEW"),
                    help="解释两次指纹之间哪些文件变了")
    ap.add_argument("--emit-manifest", default=None, help="把逐文件基线写入该 JSON（make_worktree 用）")
    ap.add_argument("--emit-base", default=None, help="把可合并文件的基线副本写到该目录")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    timeline = os.path.abspath(args.timeline or os.path.join(root, "_tree_timeline.jsonl"))

    if args.explain:
        out = []
        rc = explain(timeline, args.scope, args.explain[0], args.explain[1], out)
        print("\n".join(out))
        return rc

    if args.emit_manifest:
        m = emit_manifest(root, args.emit_manifest, args.emit_base)
        if not args.quiet:
            print("manifest=%s files=%d base_blobs=%d core_digest=%s" %
                  (os.path.abspath(args.emit_manifest), m["file_count"],
                   m["base_blob_count"], m["core_digest"]))
        return 0

    if args.watch:
        if not args.quiet:
            print("watch root=%s scope=%s interval=%.1fs timeline=%s" %
                  (root, args.scope, args.interval, timeline))
        last_digest = None
        started = time.time()
        try:
            while True:
                rec = build_record(root, args.scope, "watch")
                changed = rec["digest"] != last_digest
                if changed:
                    rec["event"] = "change" if last_digest else "watch-start"
                if changed or not args.only_changes:
                    append_record(timeline, rec)
                if not args.quiet:
                    print("%s  %s  files=%d%s" %
                          (rec["ts"], rec["digest"], rec["file_count"],
                           "  <- changed" if changed else ""))
                last_digest = rec["digest"]
                if args.duration and (time.time() - started) >= args.duration:
                    break
                time.sleep(max(0.2, args.interval))
        except KeyboardInterrupt:
            if not args.quiet:
                print("stopped")
        return 0

    digest, files = compute_digest(root, args.scope)
    head, dirty = git_meta(root)
    if args.record:
        rec = build_record(root, args.scope, "manual")
        append_record(timeline, rec)
        if not args.quiet:
            print("recorded %s digest=%s -> %s" % (rec["ts"], digest, timeline))
        if not args.files and not args.json:
            return 0

    if args.json:
        print(json.dumps({
            "root": root, "scope": args.scope, "digest": digest,
            "git_head": head, "core_dirty": dirty,
            "file_count": len(files),
            "files": files if args.files else None,
        }, ensure_ascii=False, sort_keys=True))
        return 0

    print("root=%s" % root)
    print("scope=%s  digest=%s  files=%d  git=%s  core_dirty=%s" %
          (args.scope, digest, len(files), head, dirty))
    if args.files:
        for rel in sorted(files):
            f = files[rel]
            print("  %-40s %8d B  %s  mtime_ns=%d" %
                  (rel, f["size"], f["sha256"][:16], f["mtime_ns"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
