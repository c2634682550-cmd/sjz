"""正式密钥落地：keygen 行为、仓库内无私钥、换钥不通、打包后无钩子。

这一组只碰临时目录与内存里的临时密钥；仓库里不落任何私钥。
"""
import base64
import inspect
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as L                           # noqa: E402
from tools import keygen                                 # noqa: E402


def _payload():
    return {
        "v": 1, "lic_id": "lic-keygen-1", "tier": "pro", "trial": False,
        "issued_at": 1_700_000_000, "exp": 1_800_000_000,
        "grace_h": 72, "lease_h": 168, "offline_grace_h": 72,
        "fp_vec": {}, "fp_weights": dict(L.WEIGHTS),
    }


def _read_seed(path):
    lines = [ln.strip() for ln in open(path, encoding="utf-8")
             if ln.strip() and not ln.startswith("#")]
    return lines[0]


# ---------------------------------------------------------------- keygen

def test_keygen_writes_pair_to_given_path(tmp_path, capsys):
    out = tmp_path / "keys" / "prod_p256.txt"
    assert keygen.main(["--out", str(out)]) == 0
    seed = _read_seed(out)
    pub = L.public_key_b64_from_priv(seed)
    printed = capsys.readouterr().out
    assert pub in printed, "dev 公钥必须打印出来"
    assert seed not in printed, "私钥不许出现在输出里"
    assert "dev" in printed and "只能用于开发环境" in printed, "必须明确标注 dev"
    # 新生成的这对密钥自洽：签→验成功，改一字节失败
    blob = L.sign_blob(_payload(), seed)
    assert L.p256_verify(pub, L.canonical_bytes(blob["payload"]),
                         base64.b64decode(blob["sig"]))


def test_keygen_refuses_inside_repo(tmp_path):
    inside = os.path.join(ROOT, "tools", "license_keys", "should_not_exist.txt")
    assert keygen.main(["--out", inside]) == 2
    assert not os.path.exists(inside)


def test_keygen_refuses_overwrite(tmp_path):
    out = tmp_path / "keep.txt"
    out.write_text("existing-private-material\n", encoding="utf-8")
    assert keygen.main(["--out", str(out)]) == 3
    assert out.read_text(encoding="utf-8") == "existing-private-material\n"


def test_default_signing_key_path_is_outside_repo():
    for path in (L.default_signing_key_path(), keygen.default_dev_key_path()):
        root = os.path.normcase(os.path.realpath(ROOT)) + os.sep
        assert not os.path.normcase(os.path.realpath(path)).startswith(root)
    assert os.path.basename(keygen.default_dev_key_path()) \
        == "dev_signing_key_p256.txt"
    assert keygen.default_dev_key_path() != L.default_signing_key_path()


def test_repo_has_no_private_key_files():
    keydir = os.path.join(ROOT, "tools", "license_keys")
    assert not os.path.isdir(keydir) or not os.listdir(keydir), (
        "仓库内仍有私钥文件：%r" % (os.listdir(keydir)
                                   if os.path.isdir(keydir) else []))


# ---------------------------------------------------------------- 换钥隔离

def test_two_keypairs_do_not_interop(monkeypatch):
    s1, s2 = L.new_private_key_hex(), L.new_private_key_hex()
    p1, p2 = (L.public_key_b64_from_priv(s1), L.public_key_b64_from_priv(s2))
    blob = L.sign_blob(_payload(), s1)

    monkeypatch.setattr(L, "PUBLIC_KEY_B64", p1)
    assert L.verify_blob(blob)["lic_id"] == "lic-keygen-1"

    monkeypatch.setattr(L, "PUBLIC_KEY_B64", p2)
    with pytest.raises(L.LicenseError) as e:
        L.verify_blob(blob)
    assert e.value.code == "sig_invalid" and "签名" in str(e.value)


# ---------------------------------------------------------------- 无测试钩子

def test_verify_blob_has_no_runtime_key_override(monkeypatch):
    """打包后（sys.frozen）任何环境变量都不能替换公钥。"""
    seed = L.new_private_key_hex()
    pub = L.public_key_b64_from_priv(seed)
    blob = L.sign_blob(_payload(), seed)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for name in ("PUBLIC_KEY_B64", "SJZ_PUBLIC_KEY", "SZJ_PUBLIC_KEY",
                 "SJZ_PUBKEY_B64", "SZJ_LICENSE_PUBKEY"):
        monkeypatch.setenv(name, pub)
    with pytest.raises(L.LicenseError) as e:
        L.verify_blob(blob)
    assert e.value.code == "sig_invalid"


def test_verify_blob_source_has_no_test_hooks():
    src = inspect.getsource(L.verify_blob)
    for forbidden in ("_test_hooks_allowed", "environ", "getattr", "frozen"):
        assert forbidden not in src, "verify_blob 里出现了钩子：%s" % forbidden
    assert list(inspect.signature(L.verify_blob).parameters) == ["blob"]
