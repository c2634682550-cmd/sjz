"""把 `tools/hooks/pre-commit` 装进本仓库的 `.git/hooks/`。

用法（在仓库根）：

    python tools/install_hooks.py
    python tools/install_hooks.py --root <另一个 clone>

行为：
  · 只写 `.git/hooks/pre-commit`，**不改任何 git 配置**（core.hooksPath 只读检查）；
  · 已有钩子与本版本逐字节相同 → 直接报「已是最新」；
  · 已有其它钩子 → 先备份成 `pre-commit.bak.<时间戳>`，再覆盖（不静默丢别人的钩子）；
  · 文件按 LF 写入并置可执行位（Git for Windows 的 sh 才能直接跑）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

ROOT_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_REL = os.path.join("tools", "hooks", "pre-commit")


def _git(root: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", root, *args],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("git %s 失败：%s"
                           % (" ".join(args), out.stderr.strip() or out.returncode))
    return out.stdout.strip()


def install(root: str) -> int:
    root = os.path.abspath(root)
    src = os.path.join(root, SRC_REL)
    if not os.path.isfile(src):
        print("找不到钩子模板：%s" % SRC_REL, file=sys.stderr)
        return 2

    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    dst = os.path.join(hooks_dir, "pre-commit")

    with open(src, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")

    if os.path.isfile(dst):
        with open(dst, "rb") as f:
            old = f.read().replace(b"\r\n", b"\n")
        if old == data:
            print("已是最新：%s" % dst)
            return _report(root, dst)

    if os.path.isfile(dst):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = dst + ".bak." + stamp
        shutil.copy2(dst, backup)
        print("已有钩子已备份：%s" % backup)

    with open(dst, "wb") as f:
        f.write(data)
    try:
        os.chmod(dst, 0o755)
    except OSError:
        pass
    print("已安装：%s" % dst)
    return _report(root, dst)


def _report(root: str, dst: str) -> int:
    try:
        hooks_path = _git(root, "config", "--get", "core.hooksPath")
    except RuntimeError:
        hooks_path = ""
    if hooks_path:
        print("注意：本仓库设了 core.hooksPath=%s，git 会忽略 .git/hooks/，"
              "这个钩子不会生效。请把钩子装到那个目录，或让维护者移除该项。"
              % hooks_path, file=sys.stderr)
    print("自检：python tools/scan_secrets.py --staged")
    print("验证：git add <一份假私钥> 后 git commit，应被阻断。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="安装私钥闸门 pre-commit 钩子")
    ap.add_argument("--root", default=ROOT_DEFAULT, help="仓库根")
    args = ap.parse_args(argv)
    try:
        return install(args.root)
    except (RuntimeError, OSError) as e:
        print("[install_hooks] %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
