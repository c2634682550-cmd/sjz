"""Flask 应用工厂。WSGI 入口：`portal.app:create_app()` 或 `portal/run.py`。"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta

import flask

from . import db as dbmod
from . import security, stats
from .config import Config
from .signing import Signer


def create_app(overrides: dict | None = None) -> flask.Flask:
    cfg = Config(overrides)
    app = flask.Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        MAX_CONTENT_LENGTH=int(cfg.get("max_upload_bytes")) + 64 * 1024,
        SESSION_COOKIE_NAME="portal_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(cfg.get("https_only")
                                   or cfg.get("session_cookie_secure")),
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=int(cfg.get("session_lifetime_seconds"))),
        PORTAL_DB_PATH=cfg.db_path,
        PORTAL_TRUST_PROXY=bool(cfg.get("trust_proxy")),
        PORTAL_HTTPS_ONLY=bool(cfg.get("https_only")),
        PORTAL_CONFIG_OBJ=cfg,
    )
    dbmod.init_db(cfg.db_path)
    app.extensions["portal_config"] = cfg
    app.extensions["portal_signer"] = Signer(cfg)
    app.extensions["portal_log"] = security.init_logging(cfg)

    security.init_app(app)
    app.teardown_appcontext(dbmod.close_db)

    from .routes_public import bp as public_bp
    from .routes_admin import bp as admin_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    @app.before_request
    def _ensure_session_id():
        if not flask.session.get("sid"):
            flask.session["sid"] = secrets.token_hex(16)

    @app.context_processor
    def _template_globals():
        return {"csrf_token": security.csrf_token,
                "site_name": cfg.get("site_name"),
                "now_ts": int(time.time()),
                "current_admin": security.current_admin()}

    @app.template_filter("ts")
    def _ts(value):
        if value in (None, ""):
            return "永久"
        return datetime.fromtimestamp(int(value),
                                      stats.tz(cfg)).strftime("%Y-%m-%d %H:%M")

    # 预留：抢购/数据看板对接位。字段协议未定，先给稳定的外壳和鉴权方式。
    @app.get("/api/integration/metrics")
    def integration_metrics():
        token = cfg.get("integration_token") or ""
        if not token:
            return flask.jsonify({"ok": False, "error": "integration disabled"}), 404
        sent = flask.request.headers.get("X-Integration-Token", "")
        if not secrets.compare_digest(sent, token):
            return flask.jsonify({"ok": False, "error": "unauthorized"}), 401
        return flask.jsonify({
            "ok": True, "enabled": False,
            "note": "字段协议未定；对接时在这里替换 data 内容",
            "overview": stats.overview(dbmod.get_db(), cfg),
            "fields": {"pending_orders": None, "flash_sale_slots": None},
        })

    @app.errorhandler(413)
    def _too_large(_exc):
        limit_mb = int(cfg.get("max_upload_bytes")) // (1024 * 1024)
        return flask.render_template("error.html", code=413,
                                     message=f"上传内容超过限制（{limit_mb} MB）"), 413

    @app.errorhandler(404)
    def _not_found(_exc):
        if security.wants_json():
            return flask.jsonify({"ok": False, "error": "not found"}), 404
        return flask.render_template("error.html", code=404,
                                     message="页面不存在"), 404

    @app.errorhandler(500)
    def _server_error(exc):                   # noqa: ARG001
        app.extensions["portal_log"].exception("未处理异常：%s",
                                               flask.request.path)
        return flask.render_template("error.html", code=500,
                                     message="服务器内部错误"), 500

    return app


def get_config(app=None) -> Config:
    app = app or flask.current_app
    return app.extensions["portal_config"]
