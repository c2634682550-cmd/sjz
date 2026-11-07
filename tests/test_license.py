"""许可模块测试：正常 / 过期 / 篡改 / 换机 / 回拨 / 离线宽限 / 吊销 / 试用。

两种独立复核：
  · ECDSA P-256 用 RFC 6979 §A.2.5 的官方向量做已知答案校验（不是自己签自己验）；
  · 指纹用测试里手写的第二套实现（逐组件 sha256 + 加权）和 fingerprint() 对比。

R10 回归用例的命名都带 `r10_`，与 `_bug_r10_license.md` 的编号对应；
差分证明见 `tools/r10_regression_proof.py`（同一批断言在旧代码上必须失败）。
"""
import base64
import hashlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as L                           # noqa: E402

DAY = 86400
# 每轮测试现生成一把临时标量：不落盘、不进夹具、跑完即随进程销毁。
SEED_HEX = L.new_private_key_hex()
PUB_B64 = L.public_key_b64_from_priv(SEED_HEX)

COMPS = {"bios_uuid": "bios-aaaa", "machine_guid": "guid-bbbb",
         "board_serial": "board-cccc", "cpu_id": "cpu-dddd",
         "volume_serial": "vol-eeee", "mac": "mac-ffff"}

T0 = 1_700_000_000

# 在 autouse fixture 改常量之前抓一份源码里的发布公钥
RELEASE_PUB = L.PUBLIC_KEY_B64


@pytest.fixture(autouse=True)
def _reset_enforcement():
    """门禁标志是进程级全局；测试之间必须复位，否则会污染 server 测试。"""
    L._ENFORCED = None
    yield
    L._ENFORCED = None


@pytest.fixture(autouse=True)
def _pin_test_public_key(monkeypatch):
    """把「编译期公钥」换成测试公钥。

    这是模块常量，不是函数参数 —— 生产代码里没有任何从外部替换它的入口
    （R10-L09 修的正是这一点）。
    """
    monkeypatch.setattr(L, "PUBLIC_KEY_B64", PUB_B64)


def _fp(comps=None):
    return L.fingerprint(COMPS if comps is None else comps)


def make_blob(comps=None, *, now=T0, days=365, trial=False, perpetual=False,
              lease_h=168, grace_h=72, offline_grace_h=72, **over):
    fp = _fp(comps)
    payload = {
        "v": 1, "lic_id": "lic-test-1", "tier": "pro", "issued_at": now,
        "exp": None if perpetual else now + days * DAY,
        "grace_h": grace_h, "lease_h": lease_h,
        "offline_grace_h": offline_grace_h,
        "fp": fp["hash"], "fp_vec": fp["vec"], "fp_weights": L.WEIGHTS,
        "trial": trial, "perpetual": perpetual, "features": ["ocr"],
    }
    payload.update(over)
    return L.sign_blob(payload, SEED_HEX)


def online_state(now=T0, mono=1000, used=0, rollbacks=0, lic_id="lic-test-1"):
    return {"last_check_wall": now, "last_check_mono": mono,
            "offline_used_s": used, "last_server_epoch": now,
            "rollback_events": rollbacks, "lic_id": lic_id,
            "revoke_gen": 0, "rev_seen": False}


def unproven_state(born=T0, used=0, now=T0, mono=1000, lic_id="lic-test-1"):
    """首次离线激活建立的锚点账本（锚点类型 = unproven）。"""
    return {"anchor_kind": "unproven", "anchor_epoch": born,
            "anchor_born_wall": born, "anchor_seen_wall": now,
            "last_check_wall": now, "last_check_mono": mono,
            "offline_used_s": used, "last_server_epoch": None,
            "rollback_events": 0, "lic_id": lic_id,
            "revoke_gen": 0, "rev_seen": False}


def ev(blob, *, now=T0, mono=1000, comps=COMPS, state="default", rev=None,
       trusted=True, mirror=None):
    if state == "default":
        state = online_state()
    return L.evaluate(blob, revocations=rev, state=state, now=now, mono=mono,
                      components=comps, state_trusted=trusted,
                      anchor_mirror=mirror)


# ---------------------------------------------------------------- 加密

# RFC 6979 §A.2.5：P-256 + SHA-256，message = "sample"
RFC6979 = {
    "d": "c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721",
    "msg": b"sample",
    "r": "efd48b2aacb6a8fd1140dd9cd45e81d69d2c877b56aaf991c34d0ea84eaf3716",
    "s": "f7cb1c942d657c41d436c7a1b6e29f65f3e900dbb9aff4064dc4ab2f843acda8",
}


def test_p256_rfc6979_known_answer():
    """官方向量：r 必须逐字节一致，s 是本实现 low-S 归一化后的结果。"""
    sig = L.p256_sign(RFC6979["d"], RFC6979["msg"])
    assert len(sig) == 64
    assert sig[:32].hex() == RFC6979["r"]
    assert int.from_bytes(sig[32:], "big") == \
        L._P256_N - int(RFC6979["s"], 16)          # 低半区归一化
    assert L.p256_verify(L.public_key_b64_from_priv(RFC6979["d"]),
                         RFC6979["msg"], sig)


def test_p256_sign_is_deterministic():
    a = L.p256_sign(SEED_HEX, b"run-twice")
    b = L.p256_sign(SEED_HEX, b"run-twice")
    assert a == b


def test_p256_verify_rejects_garbage():
    assert not L.p256_verify(PUB_B64, b"m", b"\x00" * 64)          # 点不在曲线上
    assert not L.p256_verify(PUB_B64, b"m", b"\x00" * 63)          # 截断签名
    assert not L.p256_verify(PUB_B64, b"m", b"\x00" * 65)          # 超长签名
    assert not L.p256_verify(PUB_B64, b"m", b"")                   # 空签名
    short = base64.b64encode(b"\x00" * 32).decode()                # 旧 32 字节公钥
    assert not L.p256_verify(short, b"m", b"\x00" * 64)
    assert not L.p256_verify(PUB_B64, b"other", L.p256_sign(SEED_HEX, b"m"))


def test_old_ed25519_license_is_rejected_readably():
    """迁移说明里的「旧卡密作废」：旧算法签名即使格式完整也必须拒，原因可读。"""
    from core import ed25519_ref as E
    blob = make_blob()
    old_seed = bytes(range(32))
    sig = E.signature(L.canonical_bytes(blob["payload"]), old_seed,
                      E.publickey(old_seed))
    blob["sig"] = base64.b64encode(sig).decode()
    with pytest.raises(L.LicenseError) as e:
        L.verify_blob(blob)
    assert e.value.code == "sig_invalid"
    assert "签名" in str(e.value)


def test_release_key_is_separate_from_test_key():
    """发布公钥必须是正式钥；仓库里不允许出现任何私钥文件。

    正式私钥只放仓库外（%LOCALAPPDATA%\\SJZ-License-Keys 或 SJZ_SIGNING_KEY），
    仓库里只有 64 字节 X||Y 公钥；`tools/license_keys` 必须为空/不存在。
    """
    assert RELEASE_PUB != PUB_B64, "发布公钥不能等于测试公钥"
    keydir = os.path.join(ROOT, "tools", "license_keys")
    leftovers = sorted(os.listdir(keydir)) if os.path.isdir(keydir) else []
    assert leftovers == [], (
        "仓库里仍有私钥文件 %r：请移到仓库外（见 tools/keygen.py）" % leftovers)
    # 公钥编码必须是 P-256 的 64 字节 X||Y，且真的在曲线上
    assert len(base64.b64decode(RELEASE_PUB, validate=True)) == 64
    L._decode_pubkey(RELEASE_PUB)


def test_release_pubkey_rejects_non_p1363_shapes(monkeypatch):
    """截断签名 / DER 签名 / 32 字节公钥 / 128 字节公钥都必须拒，原因可读。"""
    good = make_blob()

    # 32 字节公钥（Ed25519 长度）
    monkeypatch.setattr(L, "PUBLIC_KEY_B64",
                        base64.b64encode(b"\x33" * 32).decode())
    with pytest.raises(L.LicenseError) as e32:
        L.verify_blob(good)
    assert e32.value.code == "sig_invalid" and "64 字节" in str(e32.value)

    # 128 字节公钥（P-384 / 双点拼接之类）
    monkeypatch.setattr(L, "PUBLIC_KEY_B64",
                        base64.b64encode(b"\x44" * 128).decode())
    with pytest.raises(L.LicenseError) as e128:
        L.verify_blob(good)
    assert e128.value.code == "sig_invalid" and "64 字节" in str(e128.value)

    monkeypatch.setattr(L, "PUBLIC_KEY_B64", PUB_B64)

    # 截断签名：r||s 只剩 32 字节
    blob = json.loads(json.dumps(good))
    raw = base64.b64decode(blob["sig"])
    blob["sig"] = base64.b64encode(raw[:32]).decode()
    with pytest.raises(L.LicenseError) as etrunc:
        L.verify_blob(blob)
    assert etrunc.value.code == "sig_invalid" and "64 字节" in str(etrunc.value)

    # DER 编码签名：SEQUENCE(INTEGER r, INTEGER s)，长度合法但不是 P1363
    def _der_int(v: bytes) -> bytes:
        v = v.lstrip(b"\x00") or b"\x00"
        if v[0] & 0x80:
            v = b"\x00" + v
        return b"\x02" + bytes([len(v)]) + v

    body = _der_int(raw[:32]) + _der_int(raw[32:])
    der = b"\x30" + bytes([len(body)]) + body
    blob["sig"] = base64.b64encode(der).decode()
    with pytest.raises(L.LicenseError) as eder:
        L.verify_blob(blob)
    assert eder.value.code == "sig_invalid" and "64 字节" in str(eder.value)


def test_blob_rejects_ed25519_era_pubkey_and_truncated_sig(monkeypatch):
    """长度校验是防降级的一部分：32 字节公钥 / 截断签名都必须被拒。"""
    good = make_blob()
    short_pub = base64.b64encode(b"\x33" * 32).decode()      # Ed25519 长度
    monkeypatch.setattr(L, "PUBLIC_KEY_B64", short_pub)
    with pytest.raises(L.LicenseError) as e:
        L.verify_blob(good)
    assert e.value.code == "sig_invalid"
    assert "64 字节" in str(e.value)

    monkeypatch.setattr(L, "PUBLIC_KEY_B64", PUB_B64)
    blob = json.loads(json.dumps(good))
    blob["sig"] = base64.b64encode(base64.b64decode(blob["sig"])[:32]).decode()
    with pytest.raises(L.LicenseError) as e2:
        L.verify_blob(blob)
    assert e2.value.code == "sig_invalid"
    assert "64 字节" in str(e2.value)


# ---------------------------------------------------------------- 指纹

def test_fingerprint_second_implementation():
    """手写第二套实现：逐组件 sha256[:32]，再按序拼接 sha256。"""
    vec = {}
    for k in L.WEIGHTS:
        v = COMPS.get(k)
        if not v:
            continue
        vec[k] = hashlib.sha256(
            f"sjz-fp:{k}:{' '.join(str(v).strip().lower().split())}".encode()
        ).hexdigest()[:32]
    order = sorted(vec)
    digest = hashlib.sha256(
        "|".join(f"{k}={vec[k]}" for k in order).encode()).hexdigest()
    fp = _fp()
    assert fp["vec"] == vec
    assert fp["hash"] == digest
    assert fp["hash"] == _fp()["hash"]          # 稳定


def test_fingerprint_tolerates_nic_and_memory_change():
    blob = make_blob()
    assert ev(blob, comps=dict(COMPS, mac="mac-9999")).status == "ok"   # 换网卡
    assert ev(blob, comps=dict(COMPS)).fp_score == 1.0                  # 加内存不影响
    assert ev(blob, comps=dict(COMPS, cpu_id="cpu-new")).status == "ok" # 换 CPU
    # 换主板 + 换系统盘：bios/board/volume 全变，只剩 guid/cpu/mac
    other = dict(COMPS, bios_uuid="x", board_serial="y", volume_serial="z")
    assert ev(blob, comps=other).status == "fp_mismatch"


def test_r10_fp_single_component_license_rejected():
    """旧实现分母只算许可里存的组件 → 只绑 MAC 也 100% 通过（可爆破）。
    新实现分母固定 100 且要求 ≥4 个组件。"""
    only_mac = {"mac": COMPS["mac"]}
    blob = make_blob(comps=only_mac)
    d = ev(blob, comps=COMPS)
    assert d.status == "fp_mismatch" and not d.ok
    assert d.fp_score <= 0.05


def test_r10_fp_machine_guid_only_change_rejected():
    """只换 MachineGuid 在旧实现是 75% > 0.7 阈值 → 通过；新阈值下必须拒绝。"""
    blob = make_blob()
    moved = dict(COMPS, machine_guid="guid-moved")
    d = ev(blob, comps=moved)
    assert d.status == "fp_mismatch", d
    assert d.fp_score < L.FP_MIN_RATIO


def test_r10_fp_requires_anchor_component():
    """bios_uuid / machine_guid 都不命中时，即使比例够也不能放行。"""
    comps_no_anchor = {k: v for k, v in COMPS.items()
                       if k not in ("bios_uuid", "machine_guid")}
    blob = make_blob()                     # 许可里存了 6 项
    moved = dict(comps_no_anchor, cpu_id="other", board_serial="other",
                 volume_serial="other", mac="other")
    d = ev(blob, comps=moved)
    assert d.status == "fp_mismatch"


# ---------------------------------------------------------------- 正常 / 过期

def test_valid_license():
    d = ev(make_blob())
    assert d.status == "ok" and d.ok
    assert d.lic_id == "lic-test-1"
    assert d.remaining_s and d.remaining_s > 300 * DAY


def test_expired_and_grace():
    blob = make_blob(days=1)
    assert ev(blob, now=T0 + DAY + 3600).status == "ok_grace"
    assert ev(blob, now=T0 + DAY + 100 * 3600).status == "expired"


def test_perpetual_never_expires():
    assert ev(make_blob(perpetual=True), now=T0 + 3650 * DAY).status == "ok"


# ---------------------------------------------------------------- 篡改

def test_tampered_payload_rejected():
    blob = make_blob()
    forged = {"payload": dict(blob["payload"], exp=blob["payload"]["exp"] + 999 * DAY),
              "sig": blob["sig"]}
    assert ev(forged).status == "sig_invalid"


def test_tampered_signature_rejected():
    blob = make_blob()
    raw = bytearray(base64.b64decode(blob["sig"]))
    raw[3] ^= 0xFF
    bad = {"payload": blob["payload"], "sig": base64.b64encode(bytes(raw)).decode()}
    assert ev(bad).status == "sig_invalid"


def test_garbage_license_file():
    assert ev({"payload": {"v": 1}, "sig": "not-base64"}).status == "sig_invalid"
    assert ev({"payload": {"v": 1}, "sig": "AAAA"}).status == "sig_invalid"
    assert ev({}).status == "none"


def test_r10_nan_and_inf_payload_fields_rejected():
    """exp / grace_h 收到 NaN、Infinity、null 一律 sig_invalid（不再闪退/永久有效）。"""
    for bad in (float("nan"), float("inf"), None, "abc", -1):
        assert ev({"payload": dict(make_blob()["payload"], grace_h=bad),
                   "sig": make_blob()["sig"]}).status == "sig_invalid"
    nan_exp = {"payload": dict(make_blob()["payload"], exp=float("nan")),
               "sig": make_blob()["sig"]}
    assert ev(nan_exp).status == "sig_invalid"


# ---------------------------------------------------------------- 离线 / 时钟

def test_offline_within_lease():
    blob = make_blob(lease_h=168, offline_grace_h=72)
    st = online_state()
    assert ev(blob, state=st, now=T0 + 3 * DAY, mono=1000 + 3 * DAY).status == "ok"
    assert ev(blob, state=st, now=T0 + 8 * DAY,
              mono=1000 + 8 * DAY).status == "ok_grace"
    assert ev(blob, state=st, now=T0 + 11 * DAY,
              mono=1000 + 11 * DAY).status == "need_online"


def test_wall_clock_rollback_ignored_while_same_boot():
    """同一开机内只看单调时钟：把系统时间往回拨不影响判定。"""
    blob = make_blob()
    st = online_state()
    d = ev(blob, state=st, now=T0 - 30 * DAY, mono=1000 + DAY)
    assert d.status == "ok"
    assert d.offline_used_s == DAY       # 用的是 mono 差值，不是墙钟


def test_reboot_rollback_counted_then_blocks():
    blob = make_blob()
    st = online_state(rollbacks=1)
    d = ev(blob, state=st, now=T0 - DAY, mono=10)
    assert d.rollback_events == 2
    assert d.status == "clock_tamper" and not d.ok


def test_reboot_with_correct_clock_accumulates_offline():
    blob = make_blob()
    st = online_state()
    assert ev(blob, state=st, now=T0 + 2 * DAY, mono=5).status == "ok"
    d = ev(blob, state=st, now=T0 + 2 * DAY, mono=5)
    assert d.offline_used_s >= 2 * DAY


def test_r10_nan_state_cannot_buy_perpetual_offline():
    """旧实现 NaN/Large 值让 used 变成 NaN，比较永远为 False → 永久有效。"""
    blob = make_blob()
    for bad in (float("nan"), float("inf"), -1, 1e300, "abc"):
        d = ev(blob, state=dict(online_state(), offline_used_s=bad),
               now=T0 + 400 * DAY, mono=1000 + 400 * DAY)
        assert not d.ok, (bad, d.status)
    # 负数回拨计数不能把检测关掉
    d = ev(blob, state=dict(online_state(rollbacks=-99)), now=T0 - DAY, mono=10)
    # 负数被收敛到 0，本次回拨记成 1（不是靠负数把检测关掉）
    assert d.rollback_events >= 1


def test_r10_state_without_anchor_builds_unproven():
    """全新机器（无账本）→ 建立未联网锚点，能进；但租期照常消耗。"""
    blob = make_blob(days=365)
    d = ev(blob, state=None, now=T0 - 400 * DAY, mono=1000)
    assert d.status == "ok" and d.ok
    assert d.anchor == "unproven"
    assert d.state_patch["anchor_kind"] == "unproven"
    assert d.state_patch["anchor_epoch"] == float(T0 - 400 * DAY)
    assert d.state_patch["anchor_born_wall"] == float(T0 - 400 * DAY)
    assert d.notes.count("未联网锚点") >= 1
    # issued_at（T0）不能当锚点：11 天后同一锚点必须联网
    d2 = ev(blob, state=d.state_patch, now=T0 - 400 * DAY + 11 * DAY,
            mono=1000 + 11 * DAY)
    assert d2.status == "need_online" and not d2.ok


def test_r10_untrusted_state_becomes_unproven_but_capped_by_mirror():
    """账本被改 → 丢弃内容，按未联网锚点重建；副本在时累计秒数不会被清零。"""
    blob = make_blob()
    d = ev(blob, state=dict(online_state(), offline_used_s=-5), trusted=False)
    assert d.status == "ok" and d.anchor == "unproven"
    assert d.offline_used_s == 0
    mirror = {"born_wall": T0, "used_s": 10 * DAY, "seen_wall": T0 + 10 * DAY}
    d2 = ev(blob, state={}, trusted=False, mirror=mirror,
            now=T0 + 10 * DAY, mono=1000)
    assert d2.status == "ok_grace" and d2.offline_used_s >= 10 * DAY
    d3 = ev(blob, state={}, trusted=False, mirror=mirror,
            now=T0 + 11 * DAY, mono=1000)
    assert d3.status == "need_online" and not d3.ok


def test_r10_state_for_other_license_needs_online():
    d = ev(make_blob(), state=online_state(lic_id="lic-other"))
    assert d.status == "need_online"


# ---------------------------------------------------------------- 吊销

def rev_doc(lic_ids=("lic-test-1",), gen=100, fp_ids=()):
    return L.sign_blob({"v": 1, "gen": gen, "lic_ids": list(lic_ids),
                        "fp_ids": list(fp_ids)}, SEED_HEX)


def test_revocation_blocks_license():
    d = ev(make_blob(), rev=rev_doc())
    assert d.status == "revoked" and not d.ok


def test_stale_revocation_list_ignored():
    """服务端列表代数比本地小 → 忽略（防止用旧列表洗白）。"""
    st = dict(online_state(), revoke_gen=200)
    d = ev(make_blob(), rev=rev_doc(gen=100), state=st)
    assert d.status == "ok"


def test_revoked_trial_machine():
    blob = make_blob(trial=True, days=3)
    d = ev(blob, rev=rev_doc(lic_ids=(), gen=9, fp_ids=(_fp()["hash"],)))
    assert d.status == "revoked"


def test_r10_unsigned_revocation_list_not_trusted():
    """旧实现直接读 JSON 的 lic_ids；现在必须验签，伪造列表不生效。"""
    forged = {"payload": {"v": 1, "gen": 999, "lic_ids": ["lic-test-1"],
                          "fp_ids": []}, "sig": "AAAA"}
    d = ev(make_blob(), rev=forged)
    assert d.status != "revoked"
    # 且因为「曾同步过」而被要求联网
    d2 = ev(make_blob(), rev=forged,
            state=dict(online_state(), rev_seen=True))
    assert d2.status == "need_online"


def test_r10_missing_revocations_after_sync_needs_online():
    """evaluate 里缺失列表按未知处理；check() 里「曾同步过」也要联网。"""
    st = dict(online_state(), rev_seen=True, revoke_gen=500)
    d = ev(make_blob(), rev=None, state=st)
    assert d.status == "need_online", d


def test_r10_revocation_gen_written_into_signed_state():
    """check() 把列表代数写进签名账本 → 本地写死 gen 绕过失效。"""
    st = dict(online_state(), revoke_gen=0)
    d = ev(make_blob(), rev=rev_doc(lic_ids=(), gen=10), state=st)
    assert d.status == "ok"
    assert d.state_patch["revoke_gen"] == 10
    assert d.state_patch["rev_seen"] is True


# ---------------------------------------------------------------- 试用

def test_trial_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    rec = L.trial_begin(components=COMPS, now=T0, mono=100)
    assert L.trial_evaluate(rec, components=COMPS, now=T0, mono=100).status == "trial_ok"
    assert L.trial_evaluate(rec, components=COMPS,
                            now=T0 + 2 * DAY, mono=100 + 2 * DAY).status == "trial_ok"
    assert L.trial_evaluate(rec, components=COMPS,
                            now=T0 + 4 * DAY, mono=100 + 4 * DAY).status == "expired"


def test_trial_begin_does_not_reset_when_record_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    first = L.trial_begin(components=COMPS, now=T0, mono=100)
    again = L.trial_begin(components=COMPS, now=T0 + DAY, mono=100 + DAY)
    assert again["started_wall"] == first["started_wall"]
    assert again["lic_id"] == first["lic_id"]


def test_trial_clock_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    rec = L.trial_begin(components=COMPS, now=T0, mono=100)
    # 重启后（mono 归零）墙钟比开始时间还早 → 判定回拨
    d = L.trial_evaluate(rec, components=COMPS, now=T0 - DAY, mono=10)
    assert d.status == "clock_tamper"
    # 同一次开机内改墙钟：单调时钟为准，不误报
    assert L.trial_evaluate(rec, components=COMPS,
                            now=T0 - DAY, mono=100).status == "trial_ok"


def test_r10_trial_hours_field_ignored(tmp_path, monkeypatch):
    """旧实现读记录里的 hours；把 72 改成 720 就能无限试用。"""
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    rec = L.trial_begin(components=COMPS, now=T0, mono=100)
    rec["hours"] = 720
    d = L.trial_evaluate(rec, components=COMPS, now=T0 + 5 * DAY, mono=100 + 5 * DAY)
    assert d.status == "expired"
    assert L.TRIAL_HOURS == 72


def test_r10_trial_record_is_signed_and_fp_bound(tmp_path, monkeypatch):
    """明文 / 别机器的试用记录都不能用。"""
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    plain = {"lic_id": "t", "started_wall": T0, "hours": 72, "fp_vec": _fp()["vec"]}
    with open(tmp_path / "trial.json", "w", encoding="utf-8") as f:
        json.dump(plain, f)
    assert L._read_trial(COMPS) is None          # 无签名 → 不认

    L.trial_begin(components=COMPS, now=T0, mono=100)
    other = dict(COMPS, bios_uuid="other", machine_guid="other")
    d = L.trial_check(components=other, now=T0 + 3600)
    # 记录用本机指纹派生 HMAC key：换机器连签都验不过 → 认不出这条记录
    assert not d.ok and d.status in ("none", "fp_mismatch")


def test_r10_trial_survives_single_file_delete(tmp_path, monkeypatch):
    """删掉用户目录那份，ProgramData 副本还在 → 不能重开试用。"""
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    L.trial_begin(components=COMPS, now=T0, mono=100)
    os.remove(L.trial_record_path())
    again = L.trial_begin(components=COMPS, now=T0 + DAY, mono=100 + DAY)
    assert again["started_wall"] == T0


def test_r10_trial_env_override_ignored_when_frozen(tmp_path, monkeypatch):
    """打包后 SZJ_LICENSE_DIR 必须无效，否则换目录就能重开试用。"""
    la, other = tmp_path / "la", tmp_path / "other"
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(other))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert L.app_dir().startswith(str(la))
    assert L.app_dir() != str(other)


# ---------------------------------------------------------------- 文件 / 路径

def test_check_reads_written_license(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    blob = make_blob()
    L.save_license(blob, server_now=T0, now=T0, mono=1000)
    d = L.check(components=COMPS, now=T0, mono=1000)
    assert d.status == "ok"
    assert os.path.isfile(tmp_path / "license.json")
    assert os.path.isfile(tmp_path / "state.json")


def test_check_without_license(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    d = L.check(components=COMPS)
    assert d.status == "none" and not d.ok


def test_r10_check_rejects_plaintext_state(tmp_path, monkeypatch):
    """旧格式明文账本不可信：内容丢弃，按未联网锚点重建（不采信里面的秒数）。"""
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    L.save_license(make_blob(), server_now=T0, now=T0, mono=1000)
    with open(tmp_path / "state.json", encoding="utf-8") as f:
        raw = json.load(f)
    plain = dict(raw["payload"], offline_used_s=40 * DAY)
    with open(tmp_path / "state.json", "w", encoding="utf-8") as f:
        json.dump(plain, f)                     # 去掉签名 + 伪造累计 40 天
    d = L.check(components=COMPS, now=T0, mono=1000, save_state=False)
    assert d.status == "ok" and d.anchor == "unproven"
    assert d.offline_used_s == 0                # 伪造的 40 天没有被采信


def test_r10_check_detects_state_tamper_value(tmp_path, monkeypatch):
    """改账本里的 offline_used_s（哪怕格式对）→ HMAC 对不上 → 账本不可信。"""
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    L.save_license(make_blob(), server_now=T0, now=T0, mono=1000)
    with open(tmp_path / "state.json", encoding="utf-8") as f:
        raw = json.load(f)
    raw["payload"]["offline_used_s"] = -1
    with open(tmp_path / "state.json", "w", encoding="utf-8") as f:
        json.dump(raw, f)
    d = L.check(components=COMPS, now=T0, mono=1000, save_state=False)
    assert d.status == "ok" and d.anchor == "unproven"
    assert d.offline_used_s == 0


def test_paths_do_not_hardcode_machine_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    p = L.paths()
    assert p["license"].startswith(str(tmp_path))
    src = open(L.__file__, encoding="utf-8").read()
    assert "C:\\Users" not in src and "Desktop\\project" not in src


# ---------------------------------------------------------------- 结构

def test_blob_is_canonical_json_serialisable():
    blob = make_blob()
    text = json.dumps(blob, ensure_ascii=False)
    assert len(text) < 4000
    payload = L.verify_blob(json.loads(text))
    assert payload["lic_id"] == "lic-test-1"
    reordered = dict(reversed(list(payload.items())))
    assert L.verify_blob({"payload": reordered, "sig": blob["sig"]})


def test_r10_public_key_cannot_be_overridden_by_parameter():
    """verify_blob / evaluate / save_license / check 都不得接受公钥参数。"""
    import inspect
    for fn in (L.verify_blob, L.evaluate, L.save_license, L.check):
        params = inspect.signature(fn).parameters
        assert not any("public_key" in p or p == "pk" for p in params), (
            f"{fn.__name__} 仍接受公钥参数：{list(params)}")


# ---------------------------------------------------------------- 门禁

def test_r10_ensure_grants_trial_when_no_license(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    d = L.ensure(components=COMPS)
    assert d.ok and d.status == "trial_ok"
    assert L.enforcement_state() is True


def test_r10_ensure_blocks_when_trial_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(tmp_path))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))
    L.trial_begin(components=COMPS, now=T0, mono=100)
    monkeypatch.setattr(L.time, "time", lambda: T0 + 10 * DAY)
    monkeypatch.setattr(L.time, "monotonic", lambda: 50)   # 模拟重启（mono 归零）
    d = L.ensure(components=COMPS, allow_trial=True)
    assert not d.ok and d.status == "expired"
    assert L.enforcement_state() is False


# ---------------------------------------------------------------- 未联网锚点（unproven）

def _write_license_only(dirpath, blob):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "license.json"), "w", encoding="utf-8") as f:
        json.dump(blob, f)


@pytest.fixture
def fresh_machine(tmp_path, monkeypatch):
    """全新机器：只有 license.json；没有账本，也没有锚点副本。"""
    data = tmp_path / "data"
    anchor = tmp_path / "anchor"
    monkeypatch.setenv("SZJ_LICENSE_DIR", str(data))
    monkeypatch.setenv("SZJ_ANCHOR_DIR", str(anchor))
    monkeypatch.setenv("SZJ_NO_REGISTRY", "1")
    _write_license_only(data, make_blob(days=365))
    return data, anchor


def test_scn1_fresh_machine_offline_activation_ok(fresh_machine):
    """场景一：全新机器 + 合法卡 + 无网络 → 能进（锚点 unproven）。"""
    data, anchor = fresh_machine
    d = L.check(components=COMPS, now=T0, mono=1000)
    assert d.ok and d.status == "ok"
    assert d.anchor == "unproven"
    assert d.lease_remaining_s == 168 * 3600
    assert os.path.isfile(os.path.join(data, "state.json"))
    assert os.path.isfile(os.path.join(anchor, "anchor.json"))


def test_scn2_wall_clock_rollback_does_not_extend(fresh_machine):
    """场景二：同机回拨墙钟 → 累计只增不减，不延长可用时间。"""
    data, anchor = fresh_machine
    L.check(components=COMPS, now=T0, mono=1000)         # 首次激活，锚点出生
    d0 = L.check(components=COMPS, now=T0 + 6 * DAY, mono=1000 + 6 * DAY)
    assert d0.offline_used_s == 6 * DAY
    # 墙钟往回拨 5 天，但开机没重启（monotonic 继续走）
    d1 = L.check(components=COMPS, now=T0 + 1 * DAY, mono=1000 + 7 * DAY)
    assert d1.offline_used_s == 7 * DAY          # 不是被回拨成 1 天
    assert d1.status == "ok"
    d2 = L.check(components=COMPS, now=T0, mono=1000 + 10 * DAY)
    assert d2.offline_used_s == 10 * DAY
    assert d2.status == "ok_grace"
    d3 = L.check(components=COMPS, now=T0 - 10 * DAY, mono=1000 + 11 * DAY)
    assert d3.status == "need_online" and not d3.ok


def test_scn3_deleted_ledger_recovers_lease_from_mirror(fresh_machine):
    """场景三：删账本 → 回到 unproven，但出生点/累计从副本恢复；不能无限续。"""
    data, anchor = fresh_machine
    d = L.check(components=COMPS, now=T0, mono=1000)
    assert d.anchor == "unproven"
    os.remove(os.path.join(data, "state.json"))          # 删账本
    d2 = L.check(components=COMPS, now=T0 + 6 * DAY, mono=1000 + 6 * DAY)
    assert d2.ok and d2.anchor == "unproven"
    assert d2.state_patch["anchor_epoch"] == float(T0)   # 出生点没被重置
    assert d2.offline_used_s == 6 * DAY
    os.remove(os.path.join(data, "state.json"))          # 再删一次
    d3 = L.check(components=COMPS, now=T0 + 11 * DAY, mono=1000 + 11 * DAY)
    assert d3.status == "need_online" and not d3.ok      # 累计过 7+3 天


def test_scn4_offline_beyond_lease_refused_with_reason(fresh_machine):
    """场景四：无网络超过租期 → 拒绝，且原因可读。"""
    data, anchor = fresh_machine
    L.check(components=COMPS, now=T0, mono=1000)
    d = L.check(components=COMPS, now=T0 + 11 * DAY, mono=1000 + 11 * DAY)
    assert not d.ok and d.status == "need_online"
    assert "未联网锚点" in d.reason
    assert "7 + 3" in d.reason and "必须联网校验" in d.reason


def test_unproven_anchor_caps_perpetual_license():
    """本地签的永久卡也受租期封顶：unproven 不能变相无限续期。"""
    blob = make_blob(perpetual=True)
    d = ev(blob, state=unproven_state(born=T0), now=T0, mono=1000)
    assert d.ok and d.anchor == "unproven"
    assert d.lease_remaining_s == 168 * 3600     # 不是 None（不是「永久」）
    st = unproven_state(born=T0, used=10 * DAY, now=T0 + 10 * DAY,
                        mono=1000 + 10 * DAY)
    d2 = ev(blob, state=st, now=T0 + 11 * DAY, mono=1000 + 11 * DAY)
    assert d2.status == "need_online" and not d2.ok
