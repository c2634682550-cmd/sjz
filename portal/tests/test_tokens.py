"""下载令牌：一次性、60 秒、绑会话、不可预测、重放拒绝。"""
from __future__ import annotations

import threading
import time

from portal import tokens as token_mod

from .conftest import create_admin, issue, login, submit, token_from, upload_version


def _issue_token(app, client, **kw):
    create_admin(app)
    login(client)
    upload_version(client)
    result = issue(app, count=1, **kw)
    resp = submit(client, result["codes"][0])
    return token_from(resp)


def test_token_is_single_use(app):
    client = app.test_client()
    token = _issue_token(app, client)
    assert client.get(f"/dl/{token}").status_code == 200
    replay = client.get(f"/dl/{token}")
    assert replay.status_code == 410
    assert "已经被使用" in replay.get_data(as_text=True)


def test_token_expires_after_ttl(app_factory):
    """用真实 1 秒 TTL 等待过期：签名里的 exp 未被篡改，过期判定才可信。"""
    app = app_factory(token_ttl_seconds=1)
    client = app.test_client()
    token = _issue_token(app, client)
    time.sleep(1.3)
    resp = client.get(f"/dl/{token}")
    assert resp.status_code == 410
    assert "超时" in resp.get_data(as_text=True)


def test_token_ttl_is_60_seconds(app, db):
    client = app.test_client()
    _issue_token(app, client)
    row = db.execute("SELECT * FROM tokens ORDER BY id DESC LIMIT 1").fetchone()
    assert row["expires_at"] - row["created_at"] == 60


def test_token_bound_to_session(app):
    owner = app.test_client()
    token = _issue_token(app, owner)
    other = app.test_client()
    other.get("/")
    resp = other.get(f"/dl/{token}")
    assert resp.status_code == 403
    assert "会话不匹配" in resp.get_data(as_text=True)


def test_garbage_token_rejected(app):
    client = app.test_client()
    assert client.get("/dl/abc.def").status_code == 404


def test_token_not_predictable(app, db):
    client = app.test_client()
    t1 = _issue_token(app, client)
    resp = submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD")
    assert resp.status_code == 404  # 不新增令牌，只校验已有令牌的形态
    row = db.execute("SELECT token_hash FROM tokens ORDER BY id DESC LIMIT 1"
                     ).fetchone()
    assert len(row["token_hash"]) == 64
    assert t1.count(".") == 1
    nonce, mac = t1.split(".")
    assert len(nonce) >= 24 and len(mac) == 40


def test_concurrent_redeem_only_one_wins(app):
    client = app.test_client()
    token = _issue_token(app, client)
    cookie_name = "portal_session"
    value = client.get_cookie(cookie_name).value

    clients = []
    for _ in range(4):
        c = app.test_client()
        c.set_cookie(cookie_name, value)
        clients.append(c)
    barrier = threading.Barrier(len(clients))
    results = []

    def worker(c):
        barrier.wait()
        results.append(c.get(f"/dl/{token}").status_code)

    threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1, results
    assert results.count(410) >= 1, results


def test_expired_tokens_sweep(db):
    db.execute("DELETE FROM tokens")
    db.commit()
    assert token_mod.sweep_expired(db) == 0
