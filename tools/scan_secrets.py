"""私钥闸门：扫描暂存区 / 工作树里的私钥材料（R10-L09 的自动化兜底）。

背景：`tools/license_keys/` 历史上放过签名私钥明文，而 `.gitignore` 里只有
`/tools/license_keys/` 时匹配不到 `perf/snapshots/*/tools/license_keys/`。
2026-11-07 在快照目录里扫出 34 份明文副本（其中 17 份与生产钥同哈希），
全部未跟踪，但一次 `git add -A` 就会把它们带进提交。

四类信号（任一命中即算「有私钥」）：

  1. `pem`        PEM / OpenSSH / PuTTY 私钥头；
  2. `seed_hex`   32 字节 hex 私钥种子（P-256 与 Ed25519 都是 64 位十六进制）；
  3. `known_hash` 已知生产私钥的精确哈希（种子规范化后的 sha256 / 整文件 sha256），
                  这一条不看文件类型、不看上下文，命中即确定；
  4. `filename`   可疑文件名（`signing_key*`、`*_key.txt`、`tools/license_keys/` 等）。

例外：旧快照里硬编码的**测试专用**假种子按 sha256 记在 `KNOWN_TEST_SEED_SHA256`
里豁免（见常量注释）；除这几份已知良性材料外，任何种子都照旧命中。

用法（在仓库根）：

    python tools/scan_secrets.py --staged    # pre-commit 钩子：只看暂存区
    python tools/scan_secrets.py --all       # 打包前 / CI：看整个工作树

退出码：0 干净；1 命中；2 用法或 git 出错。

**输出只给路径、行号和规则名，绝不回显密钥内容**（只给 sha256 前 8 位指纹）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass

# 仓库根：本文件在 tools/ 下
ROOT_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 已知私钥哈希
#
# 只存哈希，不存密钥本体。种子先规范化（去空白、转小写）再求 sha256；
# 整文件哈希按 LF 归一化后求 sha256（Windows 的 CRLF 副本一样能识别）。
#
# 生产钥：%LOCALAPPDATA%\SJZ-License-Keys\prod_signing_key_p256.txt
#   种子 sha256 = fcac1235 555fe50c ... 5924b09a9b
#   文件 sha256 = 5afca5bc d71b14df ... 0e0b6f3bee（655 字节）
#   （见 _license_keys.md §6.2；换钥后必须同步更新这两行）
KNOWN_SEED_SHA256 = frozenset({
    "fcac123555fe50cc7ccbea9dde4a6263aaa2c3f873ffeb0f8387785924b09a9b",
})
KNOWN_KEY_FILE_SHA256 = frozenset({
    "5afca5bcd71b14dfcf73c34b6e94d65a9e50ad58e7dbbc34dfac760e0b6f3bee",
})

# 已知的**测试夹具**种子：旧快照 `perf/**/tests/test_license*.py` 里的
# `SEED_HEX = "a3b1c8d5…"`，源码注释写明「测试专用 P-256 标量，与发布私钥无关」。
# 只按 sha256 跳过这一处已知良性材料；其它任何种子照旧命中。
KNOWN_TEST_SEED_SHA256 = frozenset({
    "f94cf43e3e1a5ed06566787a7bab5ecdf2d93e06f8039b2efbe7788bf1f0a566",
})

# ---------------------------------------------------------------- 规则常量

RULE_PEM = "pem"
RULE_SEED = "seed_hex"
RULE_KNOWN = "known_hash"
RULE_FILENAME = "filename"

# 这些目录纯属本机环境 / 打包产物，不必逐文件扫（文件名规则仍然生效）。
SKIP_DIRS = frozenset({
    ".git", ".venv", ".venv-vl", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".omp",
    "build", "dist", "dist-installer", "dist-onefile",
})

# 单文件内容上限：超过只做文件名规则（私钥都是小文本，不扫大文件省时间）
MAX_SCAN_BYTES = 4 * 1024 * 1024

# 裸 hex 行只在「数据/配置文件」里判定。源码与文档里 64 位 hex 太常见
# （sha256 常量、摘要、示例），在这类文件里必须靠已知哈希或 key 字样上下文。
_BARE_HEX_ALLOWED_EXT = frozenset({
    "", ".txt", ".key", ".pem", ".hex", ".seed", ".priv", ".dat", ".bin",
    ".json", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".env",
    ".log", ".csv", ".tsv", ".xml", ".asc", ".properties", ".p256",
})

_PEM_PRIVATE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
    r"|-----BEGIN OPENSSH PRIVATE KEY-----"
    r"|PuTTY-User-Key-File-\d+:"
)

_HEX64 = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")

# PEM 正文：真实私钥的 base64 行都很长，用它把「源码里提到 PEM 字样」区分开
_PEM_BODY = re.compile(r"^[A-Za-z0-9+/=]{32,}$")

# 整行只有一个 64 位 hex（允许引号、行尾逗号与注释）
_BARE_HEX_LINE = re.compile(
    r"""^\s*["']?([0-9a-fA-F]{64})["']?\s*[,;]?\s*(?:\#.*)?$"""
)

# `private_key = "<hex>"` / `seed: <hex>` 这类带 key 字样的赋值
_KEYED_HEX_LINE = re.compile(
    r"""(?i)\b(?:private[_ -]?key|priv[_ -]?key|signing[_ -]?key|secret[_ -]?key"""
    r"""|private[_ -]?seed|seed)"""
    r"""\w*\s*[:=]\s*["']?([0-9a-fA-F]{64})["']?"""
)

# 可疑文件名（匹配 stem，扩展名另判）
_KEY_EXTS = frozenset({
    "", ".txt", ".pem", ".key", ".hex", ".seed", ".priv", ".dat", ".json",
    ".bin", ".asc",
})
_KEY_NAME_PATTERNS = tuple(re.compile(p) for p in (
    r"^signing[_ -]?key",
    r"^private[_ -]?key",
    r"^(?:id_rsa|id_ecdsa|id_ed25519|id_dsa)$",
    r"_key(?:$|[._\-])",
))
_KEY_DIRS = frozenset({"license_keys", "signing_keys", "private_keys"})


@dataclass(frozen=True)
class Hit:
    """一处命中：路径（相对仓库根）、行号、规则名、脱敏说明。"""

    path: str
    line: int
    rule: str
    detail: str


# ---------------------------------------------------------------- 单文件判定

def _is_public_name(stem: str) -> bool:
    """`public_key.txt` / `pubkey.pem` 这类名字是公钥，不按私钥文件名判定。"""
    return "public" in stem or stem.startswith("pub")


def filename_hit(rel: str) -> Hit | None:
    """可疑文件名 / 可疑目录。只看名字，不看内容。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    for comp in parts[:-1]:
        if comp.lower() in _KEY_DIRS:
            return Hit(rel, 1, RULE_FILENAME,
                       "路径含私钥目录 `%s/`" % comp)
    if not parts:
        return None
    stem, ext = os.path.splitext(parts[-1])
    if ext.lower() not in _KEY_EXTS:
        return None
    low = stem.lower()
    if _is_public_name(low):
        return None
    for pat in _KEY_NAME_PATTERNS:
        if pat.search(low):
            return Hit(rel, 1, RULE_FILENAME,
                       "文件名形如私钥文件（`%s`）" % parts[-1])
    return None


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pem_is_real(lines, idx, start) -> bool:
    """PEM 头是不是真的私钥块，而不是源码/文档里提到「PRIVATE KEY」字样。

    两种情况算真：头在行首（允许前面只有引号）；或者头嵌在代码字符串里，
    但下一行非空内容是一段 base64 正文（≥32 字符）。后者用来抓「整段 PEM
    被塞进 .py 字符串」的情况，同时避免扫描器自己的正则字面量自报。
    """
    prefix = lines[idx][:start].strip()
    if all(c in "\"'`" for c in prefix):
        return True
    nxt = idx + 1
    while nxt < len(lines) and not lines[nxt].strip():
        nxt += 1
    return nxt < len(lines) and bool(_PEM_BODY.match(lines[nxt].strip()))


def _line_hits(rel: str, text: str) -> list[Hit]:
    ext = os.path.splitext(rel)[1].lower()
    lines = text.splitlines()
    hits: list[Hit] = []
    for idx, line in enumerate(lines):
        lineno = idx + 1
        pem = _PEM_PRIVATE.search(line)
        if pem and _pem_is_real(lines, idx, pem.start()):
            hits.append(Hit(rel, lineno, RULE_PEM, "PEM/OpenSSH 私钥头"))
            continue                      # 同一行不再往下判
        bare = _BARE_HEX_LINE.match(line)
        keyed = _KEYED_HEX_LINE.search(line)
        if keyed or (bare and ext in _BARE_HEX_ALLOWED_EXT):
            for token in _HEX64.findall(line):
                digest = _sha256_hex(token.lower())
                if digest in KNOWN_SEED_SHA256:
                    hits.append(Hit(rel, lineno, RULE_KNOWN,
                                    "与已知生产私钥同哈希（sha256 %s…）"
                                    % digest[:8]))
                    break
            else:
                candidate = keyed.group(1) if keyed else bare.group(1)
                digest = _sha256_hex(candidate.lower())
                if digest not in KNOWN_TEST_SEED_SHA256:
                    hits.append(Hit(rel, lineno, RULE_SEED,
                                    "64 位 hex 私钥种子（sha256 %s…）"
                                    % digest[:8]))
            continue
        # 不是 key 行也没裸 hex：仍然要查「已知哈希」，防种子塞进源码
        for token in _HEX64.findall(line):
            digest = _sha256_hex(token.lower())
            if digest in KNOWN_SEED_SHA256:
                hits.append(Hit(rel, lineno, RULE_KNOWN,
                                "与已知生产私钥同哈希（sha256 %s…）"
                                % digest[:8]))
                break
    return hits


def scan_text(rel: str, text: str) -> list[Hit]:
    """扫一段文本（相对路径只用于文件名规则与扩展名判断）。"""
    hits: list[Hit] = []
    name = filename_hit(rel)
    if name:
        hits.append(name)
    hits.extend(_line_hits(rel, text))
    return hits


def scan_bytes(rel: str, raw: bytes) -> list[Hit]:
    """扫一份文件的内容；整文件命中已知钥时不再重复报种子行。"""
    hits: list[Hit] = []
    name = filename_hit(rel)
    if name:
        hits.append(name)
    if len(raw) > MAX_SCAN_BYTES:
        return hits
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    digest = hashlib.sha256(normalized).hexdigest()
    if digest in KNOWN_KEY_FILE_SHA256:
        hits.append(Hit(rel, 1, RULE_KNOWN,
                        "整文件与已知生产私钥相同（sha256 %s…）" % digest[:8]))
        return hits
    try:
        text = normalized.decode("utf-8-sig")      # 容忍 Windows 记事本写出的 BOM
    except UnicodeDecodeError:
        text = normalized.decode("utf-8", "replace")
    hits.extend(_line_hits(rel, text))
    return hits


def scan_path(path: str, root: str) -> list[Hit]:
    rel = os.path.relpath(path, root)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        print("[secrets] 读不了 %s：%s" % (rel, e), file=sys.stderr)
        return []
    return scan_bytes(rel, raw)


# ---------------------------------------------------------------- 两种模式

def iter_worktree(root: str):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".git"))
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def staged_paths(root: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", root, "diff", "--cached", "--name-only",
         "--diff-filter=ACMR", "-z"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("git diff --cached 失败：%s"
                           % (out.stderr.strip() or out.returncode))
    return [p for p in out.stdout.split("\0") if p]


def staged_text(root: str, rel: str) -> str | None:
    """从暂存区（index）取内容；取不到返回 None。"""
    out = subprocess.run(["git", "-C", root, "show", ":" + rel],
                         capture_output=True)
    if out.returncode != 0:
        return None
    return out.stdout


def _resolve_root(explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return ROOT_DEFAULT


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="扫描私钥材料：--staged 只看暂存区，--all 看整个工作树")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true",
                      help="只扫 git 暂存区（pre-commit 钩子用）")
    mode.add_argument("--all", action="store_true",
                      help="扫整个工作树（打包前 / CI 用）")
    ap.add_argument("--root", default=None,
                    help="仓库根；默认用 git 顶层目录")
    args = ap.parse_args(argv)

    root = _resolve_root(args.root)
    hits: list[Hit] = []
    scanned = 0

    if args.staged:
        try:
            paths = staged_paths(root)
        except (RuntimeError, OSError) as e:
            print("[secrets] %s" % e, file=sys.stderr)
            return 2
        for rel in paths:
            raw = staged_text(root, rel)
            if raw is None:
                continue
            hits.extend(scan_bytes(rel, raw))
            scanned += 1
        mode_text = "--staged（暂存区）"
    else:
        for path in iter_worktree(root):
            hits.extend(scan_path(path, root))
            scanned += 1
        mode_text = "--all（工作树）"

    print("[secrets] 模式 %s，仓库 %s，扫描 %d 个文件"
          % (mode_text, root, scanned))
    if not hits:
        print("[secrets] 未发现私钥材料。")
        return 0

    # 去重（文件名命中与内容命中可能来自同一文件，但行号/规则不同，保留全部）
    uniq = sorted(set(hits), key=lambda h: (h.path, h.line, h.rule))
    print("[secrets] 命中 %d 项：" % len(uniq))
    for h in uniq:
        print("  %s:%d  [%s]  %s" % (h.path, h.line, h.rule, h.detail))
    print("[secrets] 处理方式：把密钥移出仓库（%LOCALAPPDATA%\\SJZ-License-Keys\\），"
          "并从暂存区撤下；确认误报才用 --no-verify 提交。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
