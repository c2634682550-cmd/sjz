"""私钥闸门：scan_secrets 的识别能力与「不误报」边界。

测试全部在 tmp_path 里造文件；唯一碰真仓库的用例只读 `tools/hooks/pre-commit`。
不写任何真实密钥，也不改任何 git 配置（临时仓库用 `-c user.name/email` 传参）。
"""
import hashlib
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import scan_secrets as S                        # noqa: E402


PUBLIC_KEY_B64 = (
    "VtWMhwsKS9K8VVHWI7ITr9O3jzWpPkcojeCvU8JGeGnRfVFE/FsxMn8OBxUuNZjAEPokQD+"
    "Iyd/YiPax/yZjWw==")


def _fake_seed(tag="seed") -> str:
    """一个 64 位 hex 假种子（合法 sha256 输出，不是任何真实密钥）。"""
    return hashlib.sha256(("sjz-fake-secret:" + tag).encode()).hexdigest()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T",
         "-c", "user.email=t@example.com", *args],
        capture_output=True, text=True)


def _rules(hits):
    return {h.rule for h in hits}


# ---------------------------------------------------------------- 识别能力

def test_detects_pem_private_key(tmp_path):
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + "A" * 20
    text = "-----BEGIN EC PRIVATE KEY-----\n%s\n" % body
    hits = S.scan_text("key.pem", text)
    assert S.RULE_PEM in _rules(hits)
    assert hits[0].line == 1


def test_detects_pem_embedded_in_source(tmp_path):
    body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB" + "B" * 16
    text = 'KEY = """-----BEGIN OPENSSH PRIVATE KEY-----\n%s\n"""\n' % body
    hits = S.scan_text("core/some_module.py", text)
    assert S.RULE_PEM in _rules(hits)


def test_pem_word_in_source_is_not_flagged():
    """扫描器自己/其它源码里提到 PEM 字样（正则、注释、测试说明）不算私钥。"""
    code = '_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")\n'
    assert S.scan_text("tools/scan_secrets.py", code) == []
    assert S.scan_text("docs/notes.md", "支持 PEM 头：-----BEGIN PRIVATE KEY-----\n") == []


def test_detects_hex_seed_in_key_text(tmp_path):
    seed = _fake_seed()
    text = "# dev signing key\n%s\n" % seed
    hits = S.scan_text("tools/license_keys/dev_signing_key_p256.txt", text)
    assert S.RULE_SEED in _rules(hits)
    seeded = [h for h in hits if h.rule == S.RULE_SEED][0]
    assert seeded.line == 2
    assert seed not in seeded.detail, "命中说明里不许回显种子"


def test_detects_seed_in_bom_utf8_file(tmp_path):
    """记事本写出的 UTF-8 BOM 不能让第一行的裸 hex 漏掉。"""
    seed = _fake_seed("bom")
    raw = ("\ufeff" + seed + "\n").encode("utf-8")
    hits = S.scan_bytes("ops/notes.txt", raw)
    assert S.RULE_SEED in _rules(hits)
    assert [h for h in hits if h.rule == S.RULE_SEED][0].line == 1


def test_detects_known_hash_in_source_file(monkeypatch, tmp_path):
    seed = _fake_seed("known")
    monkeypatch.setattr(S, "KNOWN_SEED_SHA256",
                        frozenset({hashlib.sha256(seed.encode()).hexdigest()}))
    text = 'TOKEN = "%s"\n' % seed          # 变量名不带 key 字样
    hits = S.scan_text("core/config.py", text)
    assert S.RULE_KNOWN in _rules(hits)
    assert seed not in hits[0].detail


def test_detects_known_file_hash(monkeypatch, tmp_path):
    raw = b"# SJZ prod\n" + _fake_seed("file").encode() + b"\n"
    digest = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    monkeypatch.setattr(S, "KNOWN_KEY_FILE_SHA256", frozenset({digest}))
    hits = S.scan_bytes("perf/snapshots/x/tools/license_keys/copy.txt", raw)
    assert S.RULE_KNOWN in _rules(hits)
    assert [h for h in hits if h.rule == S.RULE_KNOWN][0].line == 1


def test_allowlisted_test_seed_is_skipped(monkeypatch):
    """已知测试夹具种子（旧快照的 SEED_HEX）按 sha256 豁免，不算私钥。"""
    seed = _fake_seed("allowlisted")
    monkeypatch.setattr(
        S, "KNOWN_TEST_SEED_SHA256",
        frozenset({hashlib.sha256(seed.encode()).hexdigest()}))
    assert S.scan_text("tests/test_license.py",
                       'SEED_HEX = "%s"\n' % seed) == []


def test_detects_suspicious_filenames(tmp_path):
    assert S.filename_hit("signing_key.txt") is not None
    assert S.filename_hit("tools/license_keys/notes.md") is not None
    assert S.filename_hit("ops/my_key.txt") is not None
    assert S.filename_hit("keys/dev_signing_key_p256.txt") is not None


# ---------------------------------------------------------------- 不误报

def test_public_key_is_not_flagged():
    text = 'PUBLIC_KEY_B64 = "%s"\n' % PUBLIC_KEY_B64
    assert S.scan_text("core/license.py", text) == []


def test_normal_sources_with_hashes_are_clean():
    sha = hashlib.sha256(b"anything").hexdigest()
    ledger = ('_LEDGER_SECRET_HEX = (\n'
              '    "%s")\n' % sha)
    digest_doc = "指纹：sha256(公钥)[:16] = %s\n" % sha[:16]
    assert S.scan_text("core/license.py", ledger) == []
    assert S.scan_text("_license_keys.md", digest_doc) == []


def test_license_keys_in_docs_and_tests_not_flagged():
    assert S.filename_hit("_license_keys.md") is None
    assert S.filename_hit("tests/test_license_keys.py") is None
    assert S.filename_hit("tools/keygen.py") is None
    assert S.filename_hit("core/hotkey.py") is None
    assert S.filename_hit("tests/fixtures/public_key.txt") is None


def test_random_in_memory_key_is_not_written_or_flagged(tmp_path):
    seed = _fake_seed("memory-only")          # 只在内存里，不落盘
    (tmp_path / "notes.txt").write_text("public material only\n",
                                       encoding="utf-8")
    files = [p for p in S.iter_worktree(str(tmp_path))]
    hits = []
    for path in files:
        hits.extend(S.scan_path(path, str(tmp_path)))
    assert hits == []
    assert seed not in "".join(p.read_text(encoding="utf-8")
                               for p in tmp_path.iterdir())


# ---------------------------------------------------------------- --staged

@pytest.fixture
def temp_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / "app.py").write_text("KEY = 'public'\n", encoding="utf-8")
    assert _git(repo, "add", "app.py").returncode == 0
    return repo


def test_staged_mode_blocks_key_then_passes(temp_repo, capsys):
    assert S.main(["--staged", "--root", str(temp_repo)]) == 0

    (temp_repo / "signing_key.txt").write_text(
        _fake_seed("staged") + "\n", encoding="utf-8")
    assert _git(temp_repo, "add", "signing_key.txt").returncode == 0

    assert S.main(["--staged", "--root", str(temp_repo)]) == 1
    out = capsys.readouterr().out
    assert "signing_key.txt" in out
    assert "[seed_hex]" in out or "[filename]" in out

    assert _git(temp_repo, "rm", "--cached", "-q",
                "signing_key.txt").returncode == 0
    (temp_repo / "signing_key.txt").unlink()
    assert S.main(["--staged", "--root", str(temp_repo)]) == 0


def test_staged_mode_ignores_untracked_files(temp_repo, capsys):
    (temp_repo / "leak.txt").write_text(_fake_seed("untracked") + "\n",
                                       encoding="utf-8")
    assert S.main(["--staged", "--root", str(temp_repo)]) == 0


def test_all_mode_sees_untracked_files(temp_repo, capsys):
    (temp_repo / "leak.txt").write_text(_fake_seed("untracked") + "\n",
                                       encoding="utf-8")
    assert S.main(["--all", "--root", str(temp_repo)]) == 1
    assert "leak.txt" in capsys.readouterr().out


# ---------------------------------------------------------------- 钩子与安装

def test_hook_template_runs_staged_scan():
    hook = os.path.join(ROOT, "tools", "hooks", "pre-commit")
    text = open(hook, encoding="utf-8").read()
    assert text.startswith("#!/bin/sh")
    assert "tools/scan_secrets.py" in text
    assert "--staged" in text
    assert "--no-verify" in text, "绕过方式必须写在钩子提示里"
    assert "\r" not in text, "钩子必须是 LF，否则 Git for Windows 的 sh 跑不了"


def test_install_hooks_installs_and_is_idempotent(temp_repo):
    from tools import install_hooks

    src = os.path.join(ROOT, "tools", "hooks", "pre-commit")
    dst_dir = temp_repo / "tools" / "hooks"
    dst_dir.mkdir(parents=True)
    (dst_dir / "pre-commit").write_bytes(open(src, "rb").read())

    assert install_hooks.main(["--root", str(temp_repo)]) == 0
    installed = temp_repo / ".git" / "hooks" / "pre-commit"
    assert installed.is_file()
    assert "scan_secrets.py" in installed.read_text(encoding="utf-8")
    assert b"\r" not in installed.read_bytes()

    assert install_hooks.main(["--root", str(temp_repo)]) == 0
    assert not list((temp_repo / ".git" / "hooks").glob("pre-commit.bak.*"))


def test_install_hooks_backs_up_foreign_hook(temp_repo):
    from tools import install_hooks

    src = os.path.join(ROOT, "tools", "hooks", "pre-commit")
    dst_dir = temp_repo / "tools" / "hooks"
    dst_dir.mkdir(parents=True)
    (dst_dir / "pre-commit").write_bytes(open(src, "rb").read())

    hooks = temp_repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n",
                                      encoding="utf-8", newline="\n")

    assert install_hooks.main(["--root", str(temp_repo)]) == 0
    backups = list(hooks.glob("pre-commit.bak.*"))
    assert len(backups) == 1
    assert "echo mine" in backups[0].read_text(encoding="utf-8")
