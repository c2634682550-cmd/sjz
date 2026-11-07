#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合并闸门：把某个 worktree 的改动按文件/hunk 合回主树，拒绝覆盖冲突。

不是「把快照整棵拷回去」，而是三路合并：
    基线 = 快照创建时的内容（<worktree>/SNAPSHOT.files.json + _snapshot_base/）
    A    = 快照当前的改动（--from）
    B    = 主树当前内容（--to）

分类规则：
    - 只有 worktree 改了（主树 == 基线）      -> 直接合入
    - 两边都改了                             -> git merge-file 三路合并；
                                                有冲突则整文件拒绝、单独列出，绝不覆盖
    - 新增文件与主树同名且内容不同            -> 冲突
    - 删除：默认只提示不执行（--allow-delete 才生效）
    - 二进制两边都改                          -> 冲突

用法：
    python tools/merge_gate.py --from ..\\dafors-wt\\wt-a            # dry-run（默认）
    python tools/merge_gate.py --from ..\\dafors-wt\\wt-a --apply
    python tools/merge_gate.py --from ..\\dafors-wt\\wt-a --apply --operator alice --verbose

退出码：0=无冲突（dry-run 或已合入）；2=有冲突文件被拒；1=参数/环境错误。
合并记录追加到 <to>/_merge_log.jsonl（已加入 .gitignore）。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_digest as td  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_NAMES = {"SNAPSHOT.txt", "SNAPSHOT.files.json", "_tree_timeline.jsonl",
                 "_merge_log.jsonl", "desktop.log"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def is_binary(blob):
    return b"\x00" in blob[:8192]


def path_of(root, rel):
    return os.path.join(root, *rel.split("/"))


def collect_files(root):
    """与 tree_digest 相同排除口径；额外跳过快照元数据与运行产物。"""
    out = set()
    for rel, _path, _size in td.iter_files(root):
        if rel in RUNTIME_NAMES or rel.startswith("_snapshot_base/"):
            continue
        if rel.lower().endswith(".log"):
            continue
        out.add(rel)
    return out


def hunk_stats(base_blob, from_blob):
    """返回 (added, removed, hunk_count)：行级。"""
    a = base_blob.decode("utf-8", "replace").splitlines()
    b = from_blob.decode("utf-8", "replace").splitlines()
    added = removed = hunks = 0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        hunks += 1
        removed += i2 - i1
        added += j2 - j1
    return added, removed, hunks


def merge3(from_path, base_path, to_path):
    """git merge-file 三路合并，返回 (rc, merged_bytes)；rc>0 表示冲突区域数。"""
    cmd = ["git", "merge-file", "-p",
           "-L", "worktree", "-L", "base", "-L", "main",
           from_path, base_path, to_path]
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode, p.stdout


def load_snapshot(from_root):
    manifest_path = os.path.join(from_root, "SNAPSHOT.files.json")
    if not os.path.exists(manifest_path):
        raise SystemExit(
            "缺少 %s。请用 tools/make_worktree.ps1 创建快照（合并闸门需要基线哈希）。"
            % manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    base_dir = os.path.join(from_root, "_snapshot_base")
    return manifest, base_dir


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main(argv=None):
    ap = argparse.ArgumentParser(description="按 hunk 把 worktree 合回主树（冲突拒绝覆盖）")
    ap.add_argument("--from", dest="src", required=True, help="来源 worktree 目录")
    ap.add_argument("--to", dest="dst", default=REPO_ROOT, help="目标树（默认当前仓库主树）")
    ap.add_argument("--apply", action="store_true", help="真正写入；缺省只做 dry-run")
    ap.add_argument("--operator", default=os.environ.get("USERNAME") or os.environ.get("USER") or "unknown")
    ap.add_argument("--allow-delete", action="store_true", help="允许把 worktree 里删除的文件同步删除")
    ap.add_argument("--log", default=None, help="合并日志路径，默认 <to>/_merge_log.jsonl")
    ap.add_argument("--verbose", action="store_true", help="打印每个文件的 unified diff 摘要")
    ap.add_argument("--max-diff-lines", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.src)
    dst = os.path.abspath(args.dst)
    if not os.path.isdir(os.path.join(src, "core")):
        raise SystemExit("来源目录里没有 core/：%s" % src)
    if not os.path.isdir(os.path.join(dst, "core")):
        raise SystemExit("目标目录里没有 core/：%s" % dst)
    if os.path.normcase(src) == os.path.normcase(dst):
        raise SystemExit("--from 与 --to 是同一个目录")

    manifest, base_dir = load_snapshot(src)
    base_files = manifest.get("files", {})
    base_core = manifest.get("core_digest")

    src_files = collect_files(src)
    dst_files = collect_files(dst)
    candidates = sorted((set(base_files) | src_files))

    from_core, _ = td.compute_digest(src, "core")
    to_core_before, _ = td.compute_digest(dst, "core")
    from_all, _ = td.compute_digest(src, "all")
    to_all_before, _ = td.compute_digest(dst, "all")

    actions = []    # 可执行：dict(rel, kind=copy/add/merge/delete, blob, hunks, added, removed)
    conflicts = []  # dict(rel, reason, detail)
    skipped = []    # dict(rel, reason)

    for rel in candidates:
        sp = path_of(src, rel)
        dp = path_of(dst, rel)
        bp = path_of(base_dir, rel)
        in_src = rel in src_files
        in_dst = rel in dst_files
        base_h = (base_files.get(rel) or {}).get("sha256")
        src_h = sha256_file(sp) if in_src else None
        dst_h = sha256_file(dp) if in_dst else None

        # 删除
        if base_h is not None and not in_src:
            if not in_dst:
                continue  # 两边都不存在：早已同步删除，不算差异
            if dst_h != base_h:
                conflicts.append({"rel": rel, "reason": "worktree 删除，但主树已另行修改"})
                continue
            if not args.allow_delete:
                skipped.append({"rel": rel, "reason": "worktree 删除（默认不删主树，需 --allow-delete）"})
                continue
            actions.append({"rel": rel, "kind": "delete", "hunks": 0, "added": 0, "removed": 0})
            continue

        # 未改动
        if src_h is None or src_h == base_h:
            continue

        # 新增
        if base_h is None:
            if not in_dst:
                blob = read_bytes(sp)
                added, removed, hunks = hunk_stats(b"", blob)
                actions.append({"rel": rel, "kind": "add", "blob": blob,
                                "hunks": hunks, "added": added, "removed": removed})
                continue
            if dst_h == src_h:
                skipped.append({"rel": rel, "reason": "主树已有相同内容"})
                continue
            conflicts.append({"rel": rel, "reason": "新增文件与主树同名且内容不同"})
            continue

        if not in_dst:
            conflicts.append({"rel": rel, "reason": "主树已删除该文件，worktree 又改了它"})
            continue
        if dst_h == src_h:
            skipped.append({"rel": rel, "reason": "主树已包含相同内容"})
            continue

        src_blob = read_bytes(sp)

        # 主树未动 -> 直接采用 worktree 版本
        if dst_h == base_h:
            added, removed, hunks = hunk_stats(read_bytes(bp) if os.path.exists(bp) else b"",
                                               src_blob)
            actions.append({"rel": rel, "kind": "copy", "blob": src_blob,
                            "hunks": hunks, "added": added, "removed": removed})
            continue

        # 两边都改 -> 三路合并
        if is_binary(src_blob) or is_binary(read_bytes(dp)):
            conflicts.append({"rel": rel, "reason": "两边都改了二进制文件"})
            continue
        if not os.path.exists(bp):
            conflicts.append({"rel": rel, "reason": "两边都改了，且快照没有该文件的基线副本"})
            continue
        rc, merged = merge3(sp, bp, dp)
        if rc != 0:
            conflicts.append({"rel": rel,
                              "reason": "两边改了同一区域，git merge-file 报 %d 处冲突" % rc,
                              "detail": merged.decode("utf-8", "replace")})
            continue
        added, removed, hunks = hunk_stats(read_bytes(bp), src_blob)
        actions.append({"rel": rel, "kind": "merge", "blob": merged,
                        "hunks": hunks, "added": added, "removed": removed})

    # ------------------------------------------------------------ 输出
    total_hunks = sum(a["hunks"] for a in actions)
    out = []
    out.append("== merge gate ==")
    out.append("from      : %s" % src)
    out.append("to        : %s" % dst)
    out.append("operator  : %s   mode: %s" % (args.operator, "APPLY" if args.apply else "dry-run"))
    out.append("core_digest: base=%s  worktree=%s  main=%s" %
               (base_core, from_core, to_core_before))
    out.append("all_digest : worktree=%s  main=%s" % (from_all, to_all_before))
    out.append("")
    out.append("-- 改动文件 --")
    if not actions and not conflicts:
        out.append("（无差异：worktree 的改动已经在主树里）")
    for a in actions:
        out.append("  %-1s %-44s hunks=%-2d +%d/-%d" %
                   ({"copy": "M", "add": "A", "merge": "M", "delete": "D"}[a["kind"]],
                    a["rel"], a["hunks"], a["added"], a["removed"]))
        if args.verbose and "blob" in a:
            base_blob = b""
            bp = path_of(base_dir, a["rel"])
            if os.path.exists(bp):
                base_blob = read_bytes(bp)
            diff = list(difflib.unified_diff(
                base_blob.decode("utf-8", "replace").splitlines(),
                a["blob"].decode("utf-8", "replace").splitlines(),
                fromfile="base/" + a["rel"], tofile="worktree/" + a["rel"], lineterm="", n=2))
            for line in diff[:args.max_diff_lines]:
                out.append("      " + line)
            if len(diff) > args.max_diff_lines:
                out.append("      ... (%d 行省略)" % (len(diff) - args.max_diff_lines))

    if skipped:
        out.append("")
        out.append("-- 跳过 --")
        for s in skipped:
            out.append("  - %-44s %s" % (s["rel"], s["reason"]))

    if conflicts:
        out.append("")
        out.append("-- 冲突（拒绝自动合并，主树保持原样）--")
        for c in conflicts:
            out.append("  ! %-44s %s" % (c["rel"], c["reason"]))
        if args.verbose:
            for c in conflicts:
                if c.get("detail"):
                    for line in c["detail"].splitlines()[:args.max_diff_lines]:
                        out.append("      " + line)

    # ------------------------------------------------------------ 写入
    written = []
    if args.apply and actions:
        for a in actions:
            dp = path_of(dst, a["rel"])
            if a["kind"] == "delete":
                if os.path.exists(dp):
                    os.remove(dp)
                written.append(a["rel"])
                continue
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            with open(dp, "wb") as f:
                f.write(a["blob"])
            written.append(a["rel"])

    to_core_after, _ = td.compute_digest(dst, "core")
    to_all_after, _ = td.compute_digest(dst, "all")
    if args.apply:
        out.append("")
        out.append("-- 已写入 %d 个文件 --" % len(written))
        out.append("core_digest: %s -> %s" % (to_core_before, to_core_after))
        out.append("all_digest : %s -> %s" % (to_all_before, to_all_after))

    log_path = os.path.abspath(args.log or os.path.join(dst, "_merge_log.jsonl"))
    entry = {
        "ts": now_iso(),
        "epoch": round(time.time(), 3),
        "operator": args.operator,
        "mode": "apply" if args.apply else "dry-run",
        "source_worktree": src,
        "source_name": os.path.basename(src.rstrip("\\/")),
        "target": dst,
        "base_core_digest": base_core,
        "worktree_core_digest": from_core,
        "target_core_digest_before": to_core_before,
        "target_core_digest_after": to_core_after,
        "worktree_all_digest": from_all,
        "target_all_digest_before": to_all_before,
        "target_all_digest_after": to_all_after,
        "files": [a["rel"] for a in actions],
        "written": written,
        "hunks": total_hunks,
        "conflicts": [c["rel"] for c in conflicts],
        "skipped": [s["rel"] for s in skipped],
    }
    # dry-run 也记录：这是一次「将要合并」的可追溯读数。
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    if not args.json:
        out.append("")
        out.append("log: %s" % log_path)
        print("\n".join(out))
    else:
        print(json.dumps({"entry": entry, "actions": [
            {k: v for k, v in a.items() if k != "blob"} for a in actions],
            "conflicts": [{k: v for k, v in c.items() if k != "detail"} for c in conflicts],
            "skipped": skipped}, ensure_ascii=False, sort_keys=True))

    if conflicts:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
