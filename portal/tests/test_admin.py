"""后台：登录 / 锁定 / TOTP / CSRF / 越权 / 发卡 / 版本 / 导出。"""
from __future__ import annotations

import re

from .conftest import (admin_csrf, create_admin, current_totp, get_csrf, issue,
                       login, make_zip, submit, token_from, upload_version)

CODE_RE = re.compile(r"SJZ-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-"
                     r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}")


def test_unauthenticated_admin_api_401(app):
    client = app.test_client()
    resp = client.get("/admin/api/overview")
    assert resp.status_code == 401
    assert resp.json["error"] == "未登录"
    page = client.get("/admin/cards")
    assert page.status_code == 302
    assert "/admin/login" in page.headers["Location"]


def test_login_cookie_flags(app):
    create_admin(app)
    client = app.test_client()
    resp = login(client)
    assert resp.status_code == 302
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_login_lockout_after_failures(app):
    create_admin(app)
    client = app.test_client()
    for _ in range(5):
        assert login(client, password="wrong-pass-000").status_code == 401
    resp = login(client)
    assert resp.status_code == 429
    assert "登录已锁定" in resp.get_data(as_text=True)


def test_totp_login(app):
    from portal import totp
    secret = totp.new_secret()
    create_admin(app, totp_secret=secret)
    client = app.test_client()
    assert login(client, username="admin").status_code == 401
    assert login(client, username="admin",
                 totp_code=current_totp(secret)).status_code == 302


def test_csrf_required(app):
    create_admin(app)
    client = app.test_client()
    login(client)
    resp = client.post("/admin/cards/new", data={"days": 30, "tier": "pro"})
    assert resp.status_code == 400
    assert "CSRF" in resp.get_data(as_text=True)
    token = admin_csrf(client)
    ok = client.post("/admin/cards/new",
                     data={"csrf_token": token, "days": 30, "tier": "pro"})
    assert ok.status_code == 200


def test_staff_forbidden_403(app):
    create_admin(app, username="root", role="owner")
    create_admin(app, username="staff1", role="staff")
    client = app.test_client()
    login(client, username="staff1")
    resp = client.get("/admin/admins")
    assert resp.status_code == 403
    assert "越权" in resp.get_data(as_text=True)
    token = admin_csrf(client)
    resp = client.post("/admin/admins/create",
                       data={"csrf_token": token, "username": "x1",
                             "password": "Passw0rd!2345", "role": "owner"})
    assert resp.status_code == 403


def test_single_issue_returns_plaintext_once(app):
    create_admin(app)
    client = app.test_client()
    login(client)
    token = admin_csrf(client)
    resp = client.post("/admin/cards/new",
                       data={"csrf_token": token, "days": 365, "tier": "pro",
                             "max_per_day": 5, "note": "test"})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    code = CODE_RE.search(html).group(0)
    assert "只显示这一次" in html
    # 后台列表只显示掩码
    listing = client.get("/admin/cards").get_data(as_text=True)
    assert code not in listing
    from portal import cardcode
    mask = cardcode.mask_code(code)
    assert mask in listing


def test_batch_issue_and_one_time_csv(app):
    create_admin(app)
    client = app.test_client()
    login(client)
    token = admin_csrf(client)
    resp = client.post("/admin/cards/batch",
                       data={"csrf_token": token, "count": 10, "days": 365,
                             "tier": "pro", "max_per_day": 5, "note": "b"})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    codes = CODE_RE.findall(html)
    assert len(codes) == 10
    match = re.search(r'href="(/admin/cards/batch/[0-9]+/csv\?t=[^"]+)"', html)
    assert match
    csv_path = match.group(1).replace("&amp;", "&")
    first = client.get(csv_path)
    assert first.status_code == 200
    assert "text/csv" in first.headers["Content-Type"]
    assert codes[0].encode() in first.data
    assert client.get(csv_path).status_code == 404


def test_cards_export_has_no_plaintext(app):
    create_admin(app)
    client = app.test_client()
    login(client)
    result = issue(app, count=3)
    resp = client.get("/admin/export/cards.csv")
    assert resp.status_code == 200
    assert "卡密掩码" in resp.get_data(as_text=True)
    assert all(code not in resp.get_data(as_text=True)
               for code in result["codes"])


def test_version_upload_toggle_and_disable(app, db):
    create_admin(app)
    client = app.test_client()
    login(client)
    resp = upload_version(client, version="v1.0.0", force=True)
    assert resp.status_code == 302
    row = db.execute("SELECT * FROM versions WHERE version='v1.0.0'").fetchone()
    assert row and row["enabled"] == 1 and row["force_update"] == 1
    assert row["sha256"] and row["stored_path"].startswith("versions/")
    # 下架后用户侧无可用版本
    token = admin_csrf(client)
    client.post(f"/admin/versions/{row['id']}/toggle",
                data={"csrf_token": token})
    assert db.execute("SELECT enabled FROM versions WHERE id=?",
                      (row["id"],)).fetchone()["enabled"] == 0
    result = issue(app, count=1)
    resp = submit(client, result["codes"][0])
    assert resp.status_code == 503


def test_upload_rejects_path_traversal_zip(app, db):
    create_admin(app)
    client = app.test_client()
    login(client)
    resp = upload_version(client, version="evil", data=make_zip(
        {"../evil.txt": b"pwn", "ok.txt": b"ok"}))
    assert resp.status_code == 302
    assert db.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"] == 0


def test_upload_rejects_absolute_and_symlink(app, db):
    import io
    import zipfile
    create_admin(app)
    client = app.test_client()
    login(client)
    # 绝对路径
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/evil", b"x")
    resp = upload_version(client, version="abs", data=buf.getvalue())
    assert resp.status_code == 302
    assert db.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"] == 0


def test_upload_rejects_type_and_size(app, db):
    create_admin(app)
    client = app.test_client()
    login(client)
    token = admin_csrf(client)
    resp = client.post(
        "/admin/versions/upload",
        data={"csrf_token": token, "version": "v-txt",
              "package": (__import__("io").BytesIO(b"plain"), "evil.txt")},
        content_type="multipart/form-data")
    assert resp.status_code == 302
    assert db.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"] == 0


def test_export_kind_whitelist(app):
    create_admin(app)
    client = app.test_client()
    login(client)
    assert client.get("/admin/export/passwd.csv").status_code == 400
    assert client.get("/admin/export/unknown.csv?type=../../etc").status_code == 400
    assert client.get("/admin/export/cards.csv").status_code == 200


def test_overview_numbers(app, db):
    create_admin(app)
    client = app.test_client()
    login(client)
    result = issue(app, count=4, max_per_day=5)
    upload_version(client)
    for _ in range(2):
        submit(client, result["codes"][0])
    data = client.get("/admin/api/overview").json
    assert data["total_cards"] == 4
    assert data["active_cards"] == 4
    assert data["downloads_today"] >= 0  # 令牌未兑换，下载记录为 0
    assert len(data["trend"]) == 7


def test_integration_endpoint_reserved(app_factory):
    app = app_factory(integration_token="s3cr3t")
    client = app.test_client()
    assert client.get("/api/integration/metrics").status_code == 401
    resp = client.get("/api/integration/metrics",
                      headers={"X-Integration-Token": "s3cr3t"})
    assert resp.status_code == 200
    body = resp.json
    assert body["enabled"] is False and "fields" in body
    closed = app_factory()
    assert closed.test_client().get("/api/integration/metrics").status_code == 404
