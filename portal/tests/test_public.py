"""用户侧下载链路：正确/错误/过期/封禁/设备/限额/直链。"""
from __future__ import annotations

import time
import threading

import pytest

from portal import cardcode

from .conftest import (create_admin, issue, login, submit, token_from,
                       upload_version)


def _setup(app, client, **issue_kw):
    create_admin(app)
    login(client)
    upload_version(client)
    return issue(app, count=1, **issue_kw)


def test_correct_card_downloads(app):
    client = app.test_client()
    result = _setup(app, client)
    code = result["codes"][0]
    resp = submit(client, code)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "校验通过" in html
    token = token_from(resp)
    assert token, html
    dl = client.get(f"/dl/{token}")
    assert dl.status_code == 200
    assert dl.mimetype == "application/zip"
    assert "attachment" in dl.headers["Content-Disposition"]


def test_wrong_card_rejected_with_reason(app):
    client = app.test_client()
    _setup(app, client)
    resp = submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD")
    assert resp.status_code == 404
    assert "卡密不存在" in resp.get_data(as_text=True)


def test_bad_format_rejected(app):
    client = app.test_client()
    resp = submit(client, "not-a-card")
    assert resp.status_code == 400
    assert "卡密格式不正确" in resp.get_data(as_text=True)


def test_expired_card_rejected(app, db):
    client = app.test_client()
    result = _setup(app, client)
    code = result["codes"][0]
    h = cardcode.code_hash(code, app.extensions["portal_config"].card_pepper)
    db.execute("UPDATE cards SET expires_at=? WHERE code_hash=?",
               (int(time.time()) - 10, h))
    db.commit()
    resp = submit(client, code)
    assert resp.status_code == 403
    assert "卡密已过期" in resp.get_data(as_text=True)


def test_revoked_card_rejected(app, db):
    client = app.test_client()
    result = _setup(app, client)
    code = result["codes"][0]
    h = cardcode.code_hash(code, app.extensions["portal_config"].card_pepper)
    db.execute("UPDATE cards SET revoked=1 WHERE code_hash=?", (h,))
    db.commit()
    resp = submit(client, code)
    assert resp.status_code == 403
    assert "卡密已被封禁" in resp.get_data(as_text=True)


def test_device_mismatch_rejected(app_factory):
    app = app_factory(device_binding_enabled=True)
    client = app.test_client()
    result = _setup(app, client, bind_device=True)
    code = result["codes"][0]
    first = submit(client, code, device="device-A")
    assert first.status_code == 200
    second = submit(client, code, device="device-B", ip="203.0.113.9")
    assert second.status_code == 403
    assert "设备不匹配" in second.get_data(as_text=True)


def test_no_version_gives_clear_reason(app):
    client = app.test_client()
    create_admin(app)
    login(client)
    result = issue(app, count=1)
    resp = submit(client, result["codes"][0])
    assert resp.status_code == 503
    assert "暂无可下载版本" in resp.get_data(as_text=True)


def test_direct_file_path_is_not_reachable(app):
    client = app.test_client()
    _setup(app, client)
    conn = app.extensions["portal_config"]
    import sqlite3
    db = sqlite3.connect(conn.db_path)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM versions LIMIT 1").fetchone()
    stored = row["stored_path"]
    db.close()
    for path in (f"/{stored}", f"/storage/{stored}",
                 f"/static/../var/storage/{stored}", f"/var/storage/{stored}"):
        resp = client.get(path)
        assert resp.status_code in (403, 404), (path, resp.status_code)


def test_daily_limit(app_factory):
    app = app_factory()
    client = app.test_client()
    result = _setup(app, client, max_per_day=2)
    code = result["codes"][0]
    assert submit(client, code).status_code == 200
    assert submit(client, code).status_code == 200
    third = submit(client, code)
    assert third.status_code == 429
    assert "下载太频繁" in third.get_data(as_text=True)


def test_ip_rate_limit(app_factory):
    app = app_factory(ip_limit=3)
    client = app.test_client()
    for i in range(3):
        resp = submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD", ip="192.0.2.5")
        assert resp.status_code == 404
    resp = submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD", ip="192.0.2.5")
    assert resp.status_code == 429
    assert "请求过于频繁" in resp.get_data(as_text=True)


def test_captcha_after_failures(app_factory):
    app = app_factory(captcha_after_failures=5, card_fail_limit=99,
                      ip_limit=1000)
    client = app.test_client()
    _setup(app, client)
    for _ in range(5):
        submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD", ip="192.0.2.6")
    blocked = submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD", ip="192.0.2.6")
    assert blocked.status_code == 429
    assert "人机验证" in blocked.get_data(as_text=True)
    with client.session_transaction() as sess:
        item = sess.get("portal_captcha")
    assert item
    good = submit(client, "SJZ-AAAA-BBBB-CCCC-DDDD", ip="192.0.2.6",
                  captcha=item["a"] + item["b"])
    assert good.status_code == 404  # 验证码过了，继续走后面的“卡密不存在”


def test_card_failure_lock(app_factory):
    app = app_factory(captcha_after_failures=999, card_fail_limit=3)
    client = app.test_client()
    result = _setup(app, client)
    code = result["codes"][0]
    from portal import db as dbmod
    conn = dbmod.connect(app.extensions["portal_config"].db_path)
    h = cardcode.code_hash(code, app.extensions["portal_config"].card_pepper)
    conn.execute("UPDATE cards SET expires_at=? WHERE code_hash=?",
                 (int(time.time()) - 10, h))
    conn.commit()
    conn.close()
    for _ in range(3):
        assert submit(client, code).status_code == 403
    locked = submit(client, code)
    assert locked.status_code == 429
    assert "尝试过于频繁" in locked.get_data(as_text=True)


def test_concurrent_downloads_only_quota_wins(app_factory):
    app = app_factory(ip_limit=100000, captcha_after_failures=999)
    setup_client = app.test_client()
    result = _setup(app, setup_client, max_per_day=1)
    code = result["codes"][0]
    barrier = threading.Barrier(8)
    results = []
    lock = threading.Lock()

    def worker():
        client = app.test_client()
        client.get("/")
        barrier.wait()
        resp = client.post("/", data={"code": code},
                           environ_overrides={"REMOTE_ADDR": "203.0.113.77"})
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(200) == 1, results
    assert results.count(429) >= 1
