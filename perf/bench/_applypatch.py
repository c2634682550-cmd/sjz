"""把补丁打到 perf/snapshots/<快照> 里。

为什么需要：快照目录在项目 git 仓里，带 `diff --git` 头的补丁会被
`git apply` 判成 "Skipped patch"（它按仓库根解析路径），退出码却是 0。
这里去掉 `diff --git` 头、用不带 BOM 的临时文件，再按纯 unified diff 打。

用法：python perf/bench/_applypatch.py <patch> <target-dir> [--check]
"""
import os
import subprocess
import sys
import tempfile


def main(argv):
    patch, target = argv[1], argv[2]
    check = "--check" in argv
    with open(patch, "rb") as f:
        raw = f.read()
    lines = [ln for ln in raw.splitlines(keepends=True)
             if not ln.startswith(b"diff --git ")]
    fd, tmp = tempfile.mkstemp(suffix=".patch")
    with os.fdopen(fd, "wb") as f:
        f.write(b"".join(lines))
    cmd = ["git", "-C", target, "apply", "-p1", "--verbose"]
    if check:
        cmd.append("--check")
    cmd.append(tmp)
    p = subprocess.run(cmd, capture_output=True, text=True)
    print((p.stdout or "").strip())
    print((p.stderr or "").strip(), file=sys.stderr)
    os.unlink(tmp)
    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
