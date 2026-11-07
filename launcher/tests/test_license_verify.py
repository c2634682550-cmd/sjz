"""验签 + 指纹判定：Python 签 → 启动器验，以及各类拒绝路径。"""
import base64
import json
import time

import pytest

import core.license as lic
import core.ed25519_ref as ed
from conftest import (payload_for, run_cli, sign, blob_text, write_blob, write_raw)


def der_wrap(sig64):
    """把 P1363 r||s 包成 DER SEQUENCE —— 用来证明启动器只认 P1363。"""
    def enc(b):
        b = b.lstrip(b"\x00") or b"\x00"
        return b"\x02" + bytes([len(b)]) + b
    body = enc(sig64[:32]) + enc(sig64[32:])
    return b"\x30" + bytes([len(body)]) + body


def test_python_sign_python_verify_roundtrip(fp):
    blob = sign(payload_for(fp["vec"]))
    payload = lic.verify_blob(blob)
    assert payload["lic_id"] == "TEST-0001"
    assert lic.verify_blob(json.loads(json.dumps(blob)))["v"] == 1


def test_python_verify_rejects_tampered_payload(fp):
    blob = sign(payload_for(fp["vec"]))
    blob["payload"]["lic_id"] = "EVIL-0001"
    with pytest.raises(lic.LicenseError) as e:
        lic.verify_blob(blob)
    assert e.value.code == "sig_invalid"


def test_python_sign_launcher_verify_ok(tmp_path, fp):
    path = write_blob(tmp_path / "ok.json", payload_for(fp["vec"]))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 0, raw
    assert data["status"] == "ok" and data["ok"] is True
    assert data["fp_score"] >= 0.999
    assert len(data["fp_hits"]) == 6


def test_launcher_rejects_tampered_payload(tmp_path, fp):
    blob = sign(payload_for(fp["vec"]))
    blob["payload"]["exp"] = int(time.time()) + 999999       # 改到期日
    path = write_raw(tmp_path / "t.json", json.dumps(blob, ensure_ascii=False))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "sig_invalid", raw
    assert "签名" in data["reason"]


def test_launcher_rejects_tampered_signature(tmp_path, fp):
    blob = sign(payload_for(fp["vec"]))
    raw_sig = bytearray(lic._decode_sig(blob["sig"]))
    raw_sig[5] ^= 0x01
    path = write_raw(tmp_path / "s.json",
                     json.dumps({"payload": blob["payload"],
                                 "sig": base64.b64encode(bytes(raw_sig)).decode()},
                                ensure_ascii=False))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "sig_invalid", raw


def test_launcher_rejects_other_curve_ed25519(tmp_path, fp):
    """Ed25519 签名也是 64 字节 —— 只换算法、不做曲线校验就会被放行。"""
    m = lic.canonical_bytes(payload_for(fp["vec"]))
    sk = bytes(range(32))
    sig = ed.signature(m, sk, ed.publickey(sk))
    path = write_raw(tmp_path / "e.json",
                     json.dumps({"payload": payload_for(fp["vec"]),
                                 "sig": base64.b64encode(sig).decode()},
                                ensure_ascii=False))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "sig_invalid", raw


def test_launcher_rejects_der_encoded_p256(tmp_path, fp):
    """同一把密钥、同一份 payload，只把编码换成 DER：必须拒（P1363 only）。"""
    p = payload_for(fp["vec"])
    blob = sign(p)
    sig_der = der_wrap(lic._decode_sig(blob["sig"]))
    path = write_raw(tmp_path / "der.json",
                     json.dumps({"payload": p,
                                 "sig": base64.b64encode(sig_der).decode()},
                                ensure_ascii=False))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "sig_invalid", raw
    assert "64" in data["reason"]


def test_launcher_rejects_empty_and_truncated(tmp_path, fp):
    empty = write_raw(tmp_path / "empty.json", "")
    code, data, _ = run_cli(["--verify", str(empty)])
    assert code == 1 and data["status"] == "sig_invalid"

    good = json.dumps(sign(payload_for(fp["vec"])), ensure_ascii=False)
    cut = write_raw(tmp_path / "cut.json", good[: len(good) // 2])
    code2, data2, _ = run_cli(["--verify", str(cut)])
    assert code2 == 1 and data2["status"] == "sig_invalid"
    assert "JSON" in data2["reason"]


def test_launcher_missing_license_file(tmp_path, fp):
    code, data, _ = run_cli(["--verify", str(tmp_path / "nope.json")])
    assert code == 1 and data["status"] == "none"
    assert "没有找到卡密" in data["reason"]


# ---------------------------------------------------------------- 指纹

def test_fp_single_component_missing_still_passes(tmp_path, fp):
    vec = dict(fp["vec"])
    vec.pop("mac")                                   # 权重 5 → 95%
    path = write_blob(tmp_path / "m.json", payload_for(vec))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 0 and data["ok"] is True, raw
    assert data["fp_score"] == pytest.approx(0.95)
    assert "mac" not in data["fp_hits"]


def test_fp_below_threshold_fails(tmp_path, fp):
    vec = dict(fp["vec"])
    vec.pop("bios_uuid")                             # -30
    vec.pop("machine_guid")                          # -25 → 45%
    path = write_blob(tmp_path / "f.json", payload_for(vec))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "fp_mismatch", raw
    assert data["fp_score"] == pytest.approx(0.45)
    assert data["machine_code"]


def test_fp_too_few_components_fails(tmp_path, fp):
    keep = ("bios_uuid", "machine_guid", "board_serial")     # 3 个 = 70%
    vec = {k: fp["vec"][k] for k in keep}
    path = write_blob(tmp_path / "c.json", payload_for(vec))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "fp_mismatch", raw
    assert "低于下限 4" in data["reason"]
    assert data["fp_score"] == pytest.approx(0.70)


def test_fp_anchor_missing_fails(tmp_path, fp):
    """锚点（bios_uuid / machine_guid）必须命中一个，哪怕分数够也不行。"""
    vec = {k: v for k, v in fp["vec"].items() if k in
           ("board_serial", "cpu_id", "volume_serial", "mac")}   # 45%
    path = write_blob(tmp_path / "a.json", payload_for(vec))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "fp_mismatch", raw


def test_fp_other_machine(tmp_path, fp):
    vec = {k: f"{i:032x}" for i, k in enumerate(sorted(fp["vec"]))}
    path = write_blob(tmp_path / "o.json", payload_for(vec))
    code, data, raw = run_cli(["--verify", str(path)])
    assert code == 1 and data["status"] == "fp_mismatch", raw
    assert data["fp_score"] == 0
    # 机器码稳定：同一台机器两次输出一致
    code2, data2, _ = run_cli(["--verify", str(path)])
    assert data2["machine_code"] == data["machine_code"]


# ---------------------------------------------------------------- 过期

def test_expired_beyond_grace(tmp_path, fp):
    p = payload_for(fp["vec"], exp=int(time.time()) - 73 * 3600, grace_h=72)
    code, data, raw = run_cli(["--verify", str(write_blob(tmp_path / "x.json", p))])
    assert code == 1 and data["status"] == "expired", raw
    assert "已过期" in data["reason"]


def test_expired_within_grace_allows(tmp_path, fp):
    p = payload_for(fp["vec"], exp=int(time.time()) - 3600, grace_h=72)
    code, data, raw = run_cli(["--verify", str(write_blob(tmp_path / "g.json", p))])
    assert code == 0 and data["status"] == "ok_grace", raw
    assert data["ok"] is True


def test_perpetual_license(tmp_path, fp):
    p = payload_for(fp["vec"])
    p.pop("exp")
    p["perpetual"] = True
    code, data, raw = run_cli(["--verify", str(write_blob(tmp_path / "p.json", p))])
    assert code == 0 and data["status"] == "ok", raw
    assert "长期" in data["reason"]
