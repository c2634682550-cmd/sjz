"""参考许可服务器的端到端测试（真 HTTP，127.0.0.1:随机端口）。

覆盖 R10 的 P0/P1：
  · L-02  签发端点能正常签出可验签的许可（旧代码恒 500）；
  · L-02B 私钥种子不进响应体、不进日志；
  · L-03  试用去重/限速在服务端做，不盲信客户端 fp；
  · L-11  refresh 不延长绝对到期日；
  · 并发请求不触发 sqlite 跨线程错误。
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as L                           # noqa: E402

import base64

# 每轮现生成：不落盘、不进夹具，跑完随进程销毁。
SEED_HEX = L.new_private_key_hex()
PUB_B64 = L.public_key_b64_from_priv(SEED_HEX)

COMPS = {"bios_uuid": "bios-aaaa", "machine_guid": "guid-bbbb",
         "board_serial": "board-cccc", "cpu_id": "cpu-dddd",
         "volume_serial": "vol-eeee", "mac": "mac-ffff"}


def fp_of(comps=COMPS):
    return L.fingerprint(comps)


def post(base, path, body, timeout=10):
    raw = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=raw, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "PUBLIC_KEY_B64", PUB_B64)
    import tools.license_server as S

    store = S.Store(str(tmp_path / "lic.db"), SEED_HEX)
    monkeypatch.setattr(S, "S", store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        yield base, store, S
    finally:
        httpd.shutdown()
        httpd.server_close()


def add_key(store, code="CARD-0001", tier="pro", days=365):
    store._q("INSERT INTO keys(code,tier,days,created_at) VALUES(?,?,?,?)",
             code, tier, days, int(time.time()))


# ---------------------------------------------------------------- 密钥来源

def test_server_selftest_uses_loaded_key_not_fixed_pub(tmp_path):
    """dev 私钥也能启动：自检按本次私钥派生公钥做往返，不与编译期公钥硬比。"""
    import tools.license_server as S

    dev_seed = L.new_private_key_hex()        # 与编译期 PUBLIC_KEY_B64 不配对
    assert L.public_key_b64_from_priv(dev_seed) != L.PUBLIC_KEY_B64
    store = S.Store(str(tmp_path / "dev.db"), dev_seed)
    assert store.dev_key is True
    assert store.public_key_b64 == L.public_key_b64_from_priv(dev_seed)

    # dev 签出的卡，客户端（只看编译期公钥）必须拒绝
    probe = S._make_license("f" * 64, {}, "pro", 365)
    blob = L.sign_blob(probe, dev_seed)
    with pytest.raises(L.LicenseError) as e:
        L.verify_blob(blob)
    assert e.value.code == "sig_invalid"


def test_explicit_key_file_is_the_loaded_secret(tmp_path):
    """--key-file / SJZ_SIGNING_KEY 走的就是这条加载路径。"""
    import tools.license_server as S
    from tools import license_admin as A

    seed = L.new_private_key_hex()
    path = tmp_path / "dev_signing_key_p256.txt"
    path.write_text("# dev\n" + seed + "\n", encoding="utf-8")
    assert A.load_seed(str(path)) == seed
    store = S.Store(str(tmp_path / "explicit.db"), A.load_seed(str(path)))
    assert store.dev_key is True


def test_default_signing_key_is_not_in_repo():
    import tools.license_server as S
    root = os.path.normcase(os.path.realpath(ROOT)) + os.sep
    default = os.path.normcase(os.path.realpath(S.DEFAULT_KEY_EFFECTIVE))
    assert not default.startswith(root)


def test_health_reports_prod_key_mode(server):
    base, _store, _S = server
    with urllib.request.urlopen(base + "/api/license/health", timeout=5) as r:
        body = json.loads(r.read().decode())
    assert body["key_mode"] == "prod"


# ---------------------------------------------------------------- 签发

def test_activate_signs_verifiable_license(server):
    """L-02：旧实现把 hex 私钥当文件路径 → 恒 500；现在必须签得出来。"""
    base, store, _S = server
    add_key(store)
    fp = fp_of()
    code, body = post(base, "/api/license/activate",
                      {"key": "CARD-0001", "fp": fp["hash"], "fp_vec": fp["vec"]})
    assert code == 200 and body.get("license")
    payload = L.verify_blob(body["license"])
    assert payload["lic_id"].startswith("lic-")
    assert payload["fp"] == fp["hash"]
    assert body["server_now"] and body["revocations"]


def test_trial_signs_and_binds(server):
    base, _store, _S = server
    fp = fp_of()
    code, body = post(base, "/api/license/trial",
                      {"fp": fp["hash"], "fp_vec": fp["vec"]})
    assert code == 200
    payload = L.verify_blob(body["license"])
    assert payload["trial"] is True
    assert payload["exp"] - payload["issued_at"] == L.TRIAL_HOURS * 3600


def test_refresh_signs_and_keeps_expiry(server):
    """L-11：续期只续租约，不把到期日推到 now+原天数。"""
    base, store, _S = server
    add_key(store)
    fp = fp_of()
    code, body = post(base, "/api/license/activate",
                      {"key": "CARD-0001", "fp": fp["hash"], "fp_vec": fp["vec"]})
    assert code == 200
    first = body["license"]
    exp0 = first["payload"]["exp"]
    for _ in range(3):
        code, body = post(base, "/api/license/refresh",
                          {"license": first, "fp": fp["hash"], "fp_vec": fp["vec"]})
        assert code == 200
        assert body["license"]["payload"]["exp"] == exp0, "续期延长了到期日"
        assert body["license"]["payload"]["lic_id"] == first["payload"]["lic_id"]
    # 20 年后也不能超过签发时的绝对上限
    assert exp0 <= first["payload"]["issued_at"] + L.MAX_LICENSE_DAYS * 86400


def test_activate_key_cannot_be_reused(server):
    base, store, _S = server
    add_key(store)
    fp = fp_of()
    assert post(base, "/api/license/activate",
                {"key": "CARD-0001", "fp": fp["hash"],
                 "fp_vec": fp["vec"]})[0] == 200
    other = fp_of(dict(COMPS, bios_uuid="x"))
    assert post(base, "/api/license/activate",
                {"key": "CARD-0001", "fp": other["hash"],
                 "fp_vec": other["vec"]})[0] == 409


# ---------------------------------------------------------------- 私钥不回显

def test_r10_error_response_never_contains_seed(server, monkeypatch, capsys):
    """L-02B：任何异常路径都不许把种子写进响应或日志。"""
    base, store, S = server

    def boom(payload, seed_hex):
        raise RuntimeError(f"sign failed for seed {SEED_HEX}")

    monkeypatch.setattr(S, "sign", boom)
    add_key(store)
    fp = fp_of()
    code, body = post(base, "/api/license/activate",
                      {"key": "CARD-0001", "fp": fp["hash"], "fp_vec": fp["vec"]})
    assert code == 500
    text = json.dumps(body, ensure_ascii=False)
    assert SEED_HEX not in text
    assert "RuntimeError" not in text and "sign failed" not in text
    assert body == {"error": "internal error"}

    err = capsys.readouterr().err
    assert SEED_HEX not in err, "日志里出现了私钥种子"
    assert "[REDACTED]" in err


def test_r10_store_rejects_bad_seed_without_echo(tmp_path):
    """种子格式不对直接拒绝，异常消息里不携带种子内容。"""
    import tools.license_server as S
    bad = "not-a-hex-seed-material"
    with pytest.raises(ValueError) as ei:
        S.Store(str(tmp_path / "x.db"), bad)
    assert bad not in str(ei.value)


def test_old_ed25519_license_rejected_not_500(server):
    """迁移后旧算法卡密发到 refresh：400 + 可读中文原因，不是 500。"""
    import time
    from core import ed25519_ref as E
    base, _store, _S = server
    fp = fp_of()
    payload = {"v": 1, "lic_id": "OLD-ED25519-1", "tier": "pro", "trial": False,
               "issued_at": int(time.time()) - 60, "exp": int(time.time()) + 86400,
               "grace_h": 72, "fp": fp["hash"], "fp_vec": fp["vec"],
               "fp_weights": L.WEIGHTS, "features": ["ocr"]}
    seed = bytes(range(32))
    sig = E.signature(L.canonical_bytes(payload), seed, E.publickey(seed))
    code, body = post(base, "/api/license/refresh",
                      {"license": {"payload": payload,
                                   "sig": base64.b64encode(sig).decode()},
                       "fp": fp["hash"], "fp_vec": fp["vec"]})
    assert code == 400, body
    assert "许可无效" in body["error"] and "签名" in body["error"]
    text = json.dumps(body)
    assert "Traceback" not in text and "500" not in text


def test_r10_health_has_no_key_material(server):
    """健康检查要报算法，但绝不能报密钥材料。"""
    base, _store, _S = server
    with urllib.request.urlopen(base + "/api/license/health", timeout=5) as r:
        body = json.loads(r.read().decode())
    assert body["ok"] is True and body["sign"] is True
    assert body["alg"] == L.SIG_ALG
    assert body["curve"] == "P-256"
    assert body["sig_format"] == "P1363"
    text = json.dumps(body)
    assert L.PUBLIC_KEY_B64 not in text and PUB_B64 not in text
    assert SEED_HEX not in text


# ---------------------------------------------------------------- 试用去重

def test_r10_trial_dedupe_is_server_side(server):
    """L-03：换上报的 fp 字符串没用 —— 服务端按组件自己算指纹。"""
    base, _store, _S = server
    fp = fp_of()
    assert post(base, "/api/license/trial",
                {"fp": fp["hash"], "fp_vec": fp["vec"]})[0] == 200
    # 第二次干脆不上报 fp：服务端按组件自己算，照样认出是同一台
    code, body = post(base, "/api/license/trial", {"fp_vec": fp["vec"]})
    assert code == 409 and "用过" in body["error"]
    # 上报一个假的 fp：组件与 fp 不一致，直接 400
    assert post(base, "/api/license/trial",
                {"fp": "ab" * 32, "fp_vec": fp["vec"]})[0] == 400


def test_r10_trial_rejects_fp_vec_mismatch(server):
    base, _store, _S = server
    fp = fp_of()
    code, _body = post(base, "/api/license/trial",
                       {"fp": "0" * 64, "fp_vec": fp["vec"]})
    assert code == 400


def test_r10_trial_rejects_too_few_components(server):
    """只报 MAC 一个组件就想领试用 → 400（低熵可爆破）。"""
    base, _store, _S = server
    fp = fp_of({"mac": COMPS["mac"]})
    code, _body = post(base, "/api/license/trial",
                       {"fp": fp["hash"], "fp_vec": fp["vec"]})
    assert code == 400


def test_r10_trial_rate_limited_per_ip(server):
    base, _store, _S = server
    codes = []
    for i in range(4):
        comps = dict(COMPS, bios_uuid=f"bios-{i}")
        fp = fp_of(comps)
        codes.append(post(base, "/api/license/trial",
                          {"fp": fp["hash"], "fp_vec": fp["vec"]})[0])
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


# ---------------------------------------------------------------- 吊销 / 并发

def test_r10_revocations_gen_is_monotonic(server):
    base, _store, _S = server
    g1 = L.verify_blob(post(base, "/api/license/revocations", {})[1]["revocations"])["gen"]
    g2 = L.verify_blob(post(base, "/api/license/revocations", {})[1]["revocations"])["gen"]
    assert g2 > g1


def test_r10_concurrent_requests_do_not_hit_sqlite_thread_error(server):
    """L-02 的并发缺陷：sqlite 连接跨线程 → 之前会 ProgrammingError。"""
    base, store, _S = server
    results = []

    def worker(i):
        add_key(store, code=f"CARD-{i}", tier="pro", days=30)
        fp = fp_of(dict(COMPS, bios_uuid=f"b-{i}"))
        results.append(post(base, "/api/license/activate",
                            {"key": f"CARD-{i}", "fp": fp["hash"],
                             "fp_vec": fp["vec"]})[0])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 5

