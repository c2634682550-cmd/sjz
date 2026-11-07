"""测试公共设施：每个用例独立 var 目录，绝不触碰真实数据。"""
from __future__ import annotations

import io
import os
import re
import sys
import time
import zipfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import license as core_license                           # noqa: E402
from portal import cards_service, db as dbmod, signing, totp      # noqa: E402
from portal.app import create_app                                  # noqa: E402
from portal.security import hash_password                          # noqa: E402

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
TOKEN_RE = re.compile(r"/dl/([A-Za-z0-9_.\-]+)")

BASE_OVERRIDES = {
    "ip_limit": 1000,
    "ip_window_seconds": 60,
    "captcha_after_failures": 5,
    "card_fail_limit": 5,
    "card_fail_window_seconds": 600,
    "card_fail_lock_seconds": 600,
    "max_upload_bytes": 4 * 1024 * 1024,
    "token_ttl_seconds": 60,
    "admin_max_failures": 5,
    "admin_lock_seconds": 900,
}


@pytest.fixture(autouse=True)
def _temp_signing_key(tmp_path, monkeypatch):
    """每个用例现生成一把临时签发密钥（临时目录 + 进程内存），用完销毁。

    绝不让测试读到仓库外那把正式私钥，也不把任何私钥写进仓库。
    """
    seed = core_license.new_private_key_hex()
    pub = core_license.public_key_b64_from_priv(seed)
    path = tmp_path / "signing_key_p256.txt"
    path.write_text(seed + "\n", encoding="utf-8")
    monkeypatch.setattr(signing, "PUBLIC_KEY_B64", pub)
    monkeypatch.setitem(BASE_OVERRIDES, "signing_key_file", str(path))


@pytest.fixture()
def app_factory(tmp_path):
    counter = {"n": 0}

    def make(**extra):
        counter["n"] += 1
        overrides = dict(BASE_OVERRIDES)
        overrides["var_dir"] = str(tmp_path / f"var{counter['n']}")
        overrides.update(extra)
        return create_app(overrides)

    return make


@pytest.fixture()
def app(app_factory):
    return app_factory()


@pytest.fixture()
def cfg(app):
    return app.extensions["portal_config"]


@pytest.fixture()
def db(app):
    conn = dbmod.connect(app.extensions["portal_config"].db_path)
    yield conn
    conn.close()


def create_admin(app, username="admin", password="Passw0rd!2345",
                  role="owner", totp_secret=None):
    conn = dbmod.connect(app.extensions["portal_config"].db_path)
    conn.execute(
        "INSERT INTO admins(username,password_hash,role,totp_secret,"
        "totp_enabled,created_at) VALUES(?,?,?,?,?,?)",
        (username, hash_password(password), role, totp_secret,
         1 if totp_secret else 0, int(time.time())))
    conn.commit()
    conn.close()


def get_csrf(client, path="/admin/login"):
    html = client.get(path).get_data(as_text=True)
    match = CSRF_RE.search(html)
    return match.group(1) if match else ""


def login(client, username="admin", password="Passw0rd!2345",
          totp_code=None):
    token = get_csrf(client)
    payload = {"csrf_token": token, "username": username, "password": password}
    if totp_code:
        payload["totp"] = totp_code
    return client.post("/admin/login", data=payload)


def admin_csrf(client):
    return get_csrf(client, "/admin/")


def issue(app, count=1, days=365, tier="pro", note="", max_per_day=5,
          bind_device=False):
    cfg = app.extensions["portal_config"]
    signer = app.extensions["portal_signer"]
    conn = dbmod.connect(cfg.db_path)
    result = cards_service.create_batch(
        conn, cfg, signer, count=count, days=days, tier=tier, note=note,
        max_per_day=max_per_day, bind_device=bind_device)
    conn.close()
    return result


def make_zip(entries: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (entries or {"hello.txt": b"hello world"}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def upload_version(client, version="v1.0.0", data=None, notes="", force=False):
    csrf = admin_csrf(client)
    return client.post(
        "/admin/versions/upload",
        data={"csrf_token": csrf, "version": version, "notes": notes,
              "force_update": "1" if force else "",
              "package": (io.BytesIO(data if data is not None else make_zip()),
                          "pkg.zip")},
        content_type="multipart/form-data")


def submit(client, code, ip="198.51.100.7", device=None, captcha=None):
    headers = {}
    if device:
        headers["X-Device-Id"] = device
    data = {"code": code}
    if captcha is not None:
        data["captcha"] = str(captcha)
    return client.post("/", data=data, headers=headers,
                       environ_overrides={"REMOTE_ADDR": ip})


def token_from(resp) -> str:
    match = TOKEN_RE.search(resp.get_data(as_text=True))
    return match.group(1) if match else ""


def current_totp(secret: str) -> str:
    return totp._code(secret, int(time.time()) // 30)
