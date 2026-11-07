"""安全硬要求逐条验证：哈希入库、日志脱敏、响应头、篡改检测、路径穿越。"""
from __future__ import annotations

import json
import os
import re

import pytest

from core import license as core_license
from portal import cardcode, db as dbmod, signing
from portal.security import redact, safe_storage_path

from .conftest import (
    create_admin, issue, login, make_zip, submit, upload_version,
)


def _cfg(app):
    return app.extensions["portal_config"]


def _read_log(app) -> str:
    path = os.path.join(_cfg(app).log_dir, "portal.log")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 卡密不明文

def test_card_plaintext_never_stored(app, db):
    result = issue(app, count=3)
    codes = result["codes"]
    conn = dbmod.connect(_cfg(app).db_path)
    rows = conn.execute("SELECT * FROM cards").fetchall()
    conn.close()
    joined = " ".join(str(v) for row in rows for v in tuple(row))
    for code in codes:
        assert code not in joined
        h = cardcode.code_hash(code, _cfg(app).card_pepper)
        row = next(r for r in rows if r["code_hash"] == h)
        assert len(row["code_hash"]) == 64
        assert re.fullmatch(r"SJZ-[0-9A-Z]{4}-\*\*\*\*-\*\*\*\*-[0-9A-Z]{4}",
                            row["code_mask"])
        body = code.replace("SJZ-", "").replace("-", "")
        assert row["code_mask"].endswith(body[-4:])
        assert body[:4] in row["code_mask"]

    # 连数据库文件的原始字节里都不应出现明文卡密（含 WAL）
    for suffix in ("", "-wal"):
        path = _cfg(app).db_path + suffix
        if os.path.isfile(path):
            with open(path, "rb") as f:
                blob = f.read()
            for code in codes:
                assert code.encode() not in blob


def test_admin_page_shows_mask_only(app):
    result = issue(app, count=1)
    code = result["codes"][0]
    client = app.test_client()
    create_admin(app)
    login(client)
    html = client.get("/admin/cards").get_data(as_text=True)
    assert code not in html
    assert "****" in html


# ---------------------------------------------------------------- 日志脱敏

def test_redact_function_masks_card_and_key():
    code = "SJZ-ABCD-EFGH-JKMN-PQRS"
    text = f"card={code} key={'a' * 64} token={'B' * 48}"
    out = redact(text)
    assert code not in out
    assert "SJZ-ABCD-****-****-PQRS" in out
    assert "a" * 64 not in out
    assert "B" * 48 not in out


def test_logs_do_not_leak_plaintext_card(app):
    result = issue(app, count=1)
    code = result["codes"][0]
    client = app.test_client()
    submit(client, code)
    submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD")
    log = _read_log(app)
    assert log
    assert code not in log
    assert "SJZ-AAAA-BBBB-CCCC-DDDD" not in log
    assert "SJZ-AAAA-****-****-DDDD" in log
    assert _cfg(app).card_pepper not in log


# ---------------------------------------------------------------- 响应头 / Cookie

def test_security_headers_on_public_page(app):
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert "Strict-Transport-Security" not in resp.headers


def test_session_cookie_flags(app):
    create_admin(app)
    client = app.test_client()
    resp = login(client)
    cookie = resp.headers.get("Set-Cookie", "")
    assert "portal_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_https_mode_cookie_and_hsts(app_factory):
    app = app_factory(https_only=True)
    create_admin(app)
    client = app.test_client()
    resp = login(client)
    cookie = resp.headers.get("Set-Cookie", "")
    assert "Secure" in cookie
    page = client.get("/")
    assert page.headers["Strict-Transport-Security"].startswith(
        "max-age=31536000")


# ---------------------------------------------------------------- 篡改检测

def test_tampered_batch_manifest_rejected(app, db):
    result = issue(app, count=1)
    code = result["codes"][0]
    cfg = _cfg(app)
    row = db.execute("SELECT manifest_file FROM batches WHERE id=?",
                     (result["batch_id"],)).fetchone()
    path = os.path.join(cfg.storage_dir, row["manifest_file"])
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    lines[0] = ("0" if lines[0][0] != "0" else "1") + lines[0][1:]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    resp = submit(app.test_client(), code)
    assert resp.status_code == 403
    assert "签名无效" in resp.get_data(as_text=True)


def test_tampered_amend_rejected(app, db):
    from portal import cards_service
    result = issue(app, count=1)
    code = result["codes"][0]
    cfg = _cfg(app)
    signer = app.extensions["portal_signer"]
    conn = dbmod.connect(cfg.db_path)
    card = conn.execute("SELECT * FROM cards WHERE batch_id=?",
                        (result["batch_id"],)).fetchone()
    cards_service.renew(conn, signer, card, 30)
    conn.close()

    card_now = db.execute("SELECT * FROM cards WHERE batch_id=?",
                          (result["batch_id"],)).fetchone()
    blob = json.loads(card_now["amend_blob"])
    payload = dict(blob["payload"])
    payload["exp"] = None                       # 想把自己改成永久卡
    forged = json.dumps({"payload": payload, "sig": blob["sig"]})
    db.execute("UPDATE cards SET amend_blob=? WHERE id=?",
               (forged, card_now["id"]))
    db.commit()

    resp = submit(app.test_client(), code)
    assert resp.status_code == 403
    assert "签名无效" in resp.get_data(as_text=True)


def test_tokens_are_unique_and_unpredictable(app, db):
    result = issue(app, count=1, max_per_day=5)
    code = result["codes"][0]
    client = app.test_client()
    create_admin(app)
    login(client)
    upload_version(client)

    tokens = []
    for _ in range(3):
        resp = submit(client, code)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        match = re.search(r"/dl/([A-Za-z0-9_.\-]+)", body)
        tokens.append(match.group(1))
    assert len(set(tokens)) == 3
    rows = db.execute("SELECT token_hash FROM tokens ORDER BY id").fetchall()
    assert all(len(r["token_hash"]) == 64 for r in rows)
    assert len({r["token_hash"] for r in rows}) == len(rows)
    for token in tokens:
        nonce, mac = token.split(".")
        assert len(nonce) >= 24 and len(mac) == 40


# ---------------------------------------------------------------- 路径穿越

def test_safe_storage_path_blocks_escape(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_storage_path(str(root), "../../etc/passwd")
    with pytest.raises(ValueError):
        safe_storage_path(str(root), "/etc/passwd")
    inside = root / "versions" / "a.zip"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert safe_storage_path(str(root), "versions/a.zip") == str(inside)


def test_storage_dir_not_web_reachable(app, db):
    create_admin(app)
    client = app.test_client()
    login(client)
    upload_version(client)
    row = db.execute("SELECT stored_path, filename FROM versions "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    stored = row["stored_path"]
    for guess in (
        f"/storage/{stored}",
        f"/var/storage/{stored}",
        f"/portal/var/storage/{stored}",
        f"/static/{stored}",
        f"/{stored}",
        f"/downloads/{row['filename']}",
    ):
        resp = client.get(guess)
        assert resp.status_code in (403, 404), (guess, resp.status_code)


def test_inspect_zip_rejects_traversal_entries(app):
    from portal import security as sec
    cfg = _cfg(app)
    path = os.path.join(cfg.tmp_dir, "evil.zip")
    with open(path, "wb") as f:
        f.write(make_zip({"../evil.txt": b"x"}))
    with pytest.raises(sec.UploadError) as exc:
        sec.inspect_zip(path, cfg)
    assert "路径穿越" in str(exc.value)

    abs_path = os.path.join(cfg.tmp_dir, "abs.zip")
    with open(abs_path, "wb") as f:
        f.write(make_zip({"/etc/passwd": b"x"}))
    with pytest.raises(sec.UploadError) as exc2:
        sec.inspect_zip(abs_path, cfg)
    assert "绝对路径" in str(exc2.value)


def test_p256_verify_rejects_other_message(monkeypatch):
    payload = {"v": 1, "kind": "dl-batch", "batch_id": 1}
    # 每轮随机临时私钥：不落盘、不进夹具，跑完随进程销毁。
    seed = core_license.new_private_key_hex()
    monkeypatch.setattr(signing, "PUBLIC_KEY_B64",
                        core_license.public_key_b64_from_priv(seed))
    blob = signing.sign_payload(payload, seed)
    assert signing.verify_payload(payload, blob["sig"])
    assert not signing.verify_payload({**payload, "batch_id": 2}, blob["sig"])


def test_signer_dev_mode_verifies_its_own_output(tmp_path):
    """dev 私钥（与编译期公钥不配对）也能自洽：标记 dev，但自己的签名自己认。"""
    from types import SimpleNamespace

    seed = core_license.new_private_key_hex()
    key_file = tmp_path / "dev_signing_key_p256.txt"
    key_file.write_text(seed + "\n", encoding="utf-8")
    signer = signing.Signer(SimpleNamespace(signing_key_file=str(key_file)))
    assert signer.ready is True
    assert signer.dev_key is True
    blob = signer.sign({"v": 1, "kind": "dl-card", "card_hash": "a" * 64,
                        "gen": 1})
    assert signer._verify_own(blob["payload"], blob["sig"])
    tampered = {**blob["payload"], "gen": 2}
    assert not signer._verify_own(tampered, blob["sig"])
