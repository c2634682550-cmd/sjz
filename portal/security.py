"""安全基础设施：口令哈希、CSRF、后台守卫、上传检查、日志脱敏。"""
from __future__ import annotations

import hmac
import logging
import logging.handlers
import os
import re
import secrets
import stat
import zipfile
from functools import wraps

import flask
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbmod

# ---------------------------------------------------------------- 口令

def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method="scrypt")


def verify_password(stored: str, raw: str) -> bool:
    try:
        return check_password_hash(stored, raw)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- CSRF

_CSRF_KEY = "_csrf"


def csrf_token() -> str:
    token = flask.session.get(_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        flask.session[_CSRF_KEY] = token
    return token


def check_csrf() -> bool:
    sent = (flask.request.form.get("csrf_token")
            or flask.request.headers.get("X-CSRF-Token") or "")
    expected = flask.session.get(_CSRF_KEY) or ""
    return bool(expected) and hmac.compare_digest(str(sent), str(expected))


def _csrf_guard():
    if flask.request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if flask.request.path.startswith("/admin") and not check_csrf():
            if flask.request.path.startswith("/admin/api/"):
                return flask.jsonify({"ok": False, "error": "CSRF 校验失败"}), 400
            return "CSRF 校验失败", 400
    return None


# ---------------------------------------------------------------- 后台守卫

def wants_json() -> bool:
    path = flask.request.path
    if path.startswith("/admin/api/"):
        return True
    accept = flask.request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def current_admin():
    return flask.session.get("admin")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin = current_admin()
        if not admin:
            if wants_json():
                return flask.jsonify({"ok": False, "error": "未登录"}), 401
            return flask.redirect(flask.url_for("admin.login",
                                                next=flask.request.path))
        flask.g.admin = admin
        return fn(*args, **kwargs)
    return wrapper


def owner_required(fn):
    @wraps(fn)
    @admin_required
    def wrapper(*args, **kwargs):
        if flask.g.admin.get("role") != "owner":
            if wants_json():
                return flask.jsonify({"ok": False, "error": "越权访问"}), 403
            return "403 越权访问：该操作仅限 owner 角色", 403
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- 客户端信息

def client_ip() -> str:
    cfg = flask.current_app.config
    if cfg.get("PORTAL_TRUST_PROXY"):
        forwarded = flask.request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (flask.request.remote_addr or "0.0.0.0")[:64]


def user_agent() -> str:
    return (flask.request.headers.get("User-Agent") or "")[:200]


# ---------------------------------------------------------------- 上传 / 路径

class UploadError(Exception):
    """上传不合法；消息是给管理员看的中文原因。"""


_MAX_NAME = 160


def safe_filename(name: str) -> str:
    from werkzeug.utils import secure_filename
    base = secure_filename(name or "")[:_MAX_NAME]
    return base or "file.zip"


def inspect_zip(path: str, config) -> dict:
    """校验 zip：条目数、路径穿越、符号链接、压缩比、总解压体积。"""
    max_entries = int(config.get("max_zip_entries", 5000))
    max_total = int(config.get("max_zip_uncompressed_bytes", 2 << 30))
    max_ratio = int(config.get("max_zip_ratio", 100))
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise UploadError(f"压缩包条目过多（>{max_entries}）")
            total = 0
            for info in infos:
                name = info.filename
                if "\x00" in name:
                    raise UploadError("压缩包包含非法文件名")
                if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
                    raise UploadError(f"压缩包包含绝对路径条目：{name}")
                parts = [p for p in re.split(r"[\\/]", name) if p not in ("", ".")]
                if ".." in parts:
                    raise UploadError(f"压缩包包含路径穿越条目：{name}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise UploadError(f"压缩包包含符号链接：{name}")
                if info.flag_bits & 0x1:
                    raise UploadError("压缩包包含加密条目")
                if info.is_dir():
                    continue
                total += info.file_size
                if total > max_total:
                    raise UploadError("压缩包解压后体积超过限制")
                if info.file_size > 0 and info.compress_size == 0:
                    raise UploadError("压缩包包含异常条目（压缩比为 0）")
                if info.compress_size > 0 and \
                        info.file_size / info.compress_size > max_ratio:
                    raise UploadError(f"压缩包压缩比异常（>{max_ratio}:1）")
            return {"entries": len(infos), "uncompressed": total}
    except zipfile.BadZipFile as exc:
        raise UploadError("不是有效的 zip 文件") from exc


def safe_storage_path(storage_dir: str, relative: str) -> str:
    """把数据库里的相对路径解析到 storage 根下；越界一律拒绝。"""
    root = os.path.realpath(storage_dir)
    target = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath([root, target]) != root:
        raise ValueError("路径越界")
    return target


def upload_error_message(exc) -> str:
    """把异常转成可读中文；不把内部栈/路径抛给界面。"""
    text = str(exc).strip()
    return text if text else exc.__class__.__name__


# ---------------------------------------------------------------- 日志脱敏

_CARD_RE = re.compile(r"SJZ-[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){3}")
_HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_LONG_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")


def redact(text: str) -> str:
    if not isinstance(text, str):
        return text
    from .cardcode import mask_code
    text = _CARD_RE.sub(lambda m: mask_code(m.group(0)), text)
    text = _HEX64_RE.sub("[KEY]", text)
    text = _LONG_RE.sub("[TOKEN]", text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:                      # noqa: BLE001
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return True


def init_logging(config) -> logging.Logger:
    logger = logging.getLogger("portal")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    path = os.path.join(config.log_dir, "portal.log")
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------- 响应头

def security_headers(response):
    cfg = flask.current_app.config
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy",
                                "geolocation=(), microphone=(), camera=()")
    if cfg.get("PORTAL_HTTPS_ONLY"):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if flask.request.path.startswith(("/admin", "/dl/")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def init_app(app):
    app.before_request(_csrf_guard)
    app.after_request(security_headers)
