"""用户侧：卡密兑换 + 一次性令牌 + 文件下发。"""
from __future__ import annotations

import os
import time

import flask

from . import cardcode, cards_service, ratelimit, stats, tokens, versions_service
from . import db as dbmod
from .security import client_ip, safe_storage_path, user_agent

bp = flask.Blueprint("public", __name__)

DEVICE_COOKIE = "portal_device"


def _cfg():
    return flask.current_app.extensions["portal_config"]


def _log():
    return flask.current_app.extensions["portal_log"]


def _reject(ip, code, reason, status, captcha=None):
    _log().info("校验未通过 ip=%s card=%s reason=%s", ip,
                cardcode.mask_code(code) if code else "(无)", reason)
    return _render(reason, captcha=captcha, status=status)


def _device_id() -> str:
    raw = (flask.request.headers.get("X-Device-Id")
           or flask.request.cookies.get(DEVICE_COOKIE) or "")
    raw = "".join(ch for ch in raw if ch.isalnum() or ch in "-_")[:64]
    return raw or "dev-" + flask.session.get("sid", "")[:16]


def get_device_id() -> str:
    return _device_id()


def _set_device_cookie(response, device: str):
    if flask.request.cookies.get(DEVICE_COOKIE) != device:
        response.set_cookie(DEVICE_COOKIE, device, httponly=True,
                            samesite="Lax", max_age=365 * 86400,
                            secure=bool(_cfg().get("https_only")))
    return response


def _render(message="", ok=False, token=None, exp=None, remain=None,
            captcha=None, status=200):
    version = versions_service.latest_enabled(dbmod.get_db())
    resp = flask.make_response(flask.render_template(
        "index.html", message=message, ok=ok, token=token, exp=exp,
        remain=remain, captcha=captcha, version=version,
        ttl=int(_cfg().get("token_ttl_seconds", 60))))
    if status != 200:
        resp.status_code = status
    return _set_device_cookie(resp, _device_id())


@bp.get("/")
def index():
    return _render()


@bp.get("/healthz")
def healthz():
    signer = flask.current_app.extensions["portal_signer"]
    return flask.jsonify({"ok": True, "signer_ready": signer.ready,
                          "signing_key_mode": "dev" if signer.dev_key else "prod"})


@bp.post("/")
def submit():
    config = _cfg()
    db = dbmod.get_db()
    signer = flask.current_app.extensions["portal_signer"]
    ip = client_ip()
    ua = user_agent()
    device = _device_id()
    raw = flask.request.form.get("code", "")

    # 1) 单 IP 频次（窗口内所有提交）
    window = int(config.get("ip_window_seconds", 60))
    limit = int(config.get("ip_limit", 10))
    if ratelimit.ip_request_count(db, ip, window) >= limit:
        ratelimit.record_attempt(db, ip, "", False, "IP 限流", ua)
        return _reject(ip, "", "请求过于频繁，请稍后再试", 429,
                       _maybe_captcha(db, ip))

    # 2) 失败次数达到阈值 → 验证码（人机验证，防爆破）
    if ratelimit.needs_captcha(db, flask.session, ip, config):
        if not ratelimit.verify_captcha(flask.session,
                                        flask.request.form.get("captcha", "")):
            ratelimit.record_attempt(db, ip, "", False, "验证码错误", ua)
            return _reject(ip, "", "请先完成人机验证（答案不正确）", 429,
                           ratelimit.make_captcha(flask.session))

    # 3) 格式
    code = cardcode.normalize(raw)
    if not code:
        ratelimit.record_attempt(db, ip, "", False, "格式错误", ua)
        return _reject(ip, "", "卡密格式不正确（示例：SJZ-XXXX-XXXX-XXXX-XXXX）", 400)

    code_hash = cardcode.code_hash(code, config.card_pepper)

    # 4) 同卡密失败锁定
    if ratelimit.card_locked(db, code_hash,
                             int(config.get("card_fail_limit", 5)),
                             int(config.get("card_fail_window_seconds", 600)),
                             int(config.get("card_fail_lock_seconds", 600))):
        ratelimit.record_attempt(db, ip, code_hash, False, "卡密尝试锁定", ua)
        return _reject(ip, code, "该卡密尝试过于频繁，请 10 分钟后再试", 429)

    # 5) 签名 → 封禁 → 有效期 → 设备
    card, reason = cards_service.evaluate_card(db, config, signer, code, device)
    if not card:
        ratelimit.record_attempt(db, ip, code_hash, False, reason, ua)
        return _reject(ip, code, reason, 404)
    if reason:
        ratelimit.record_attempt(db, ip, code_hash, False, reason, ua)
        status = 429 if reason == cards_service.REASON_LIMIT else 403
        return _reject(ip, code, reason, status)

    # 6) 版本
    version = versions_service.latest_enabled(db)
    if version is None:
        ratelimit.record_attempt(db, ip, code_hash, False, "无可用版本", ua)
        return _reject(ip, code, cards_service.REASON_NONE_VERSION, 503)

    # 7) 原子扣减当天下载次数（并发下只有限额内的人能过）
    bind_now = bool(config.get("device_binding_enabled")) \
        and bool(card["bind_device"]) and card["bound_device"] is None
    today = stats.today_key(config)
    if not cards_service.consume_download_quota(db, card, device, today, bind_now):
        fresh = cards_service.get_card(db, card["id"])
        if fresh and fresh["bound_device"] \
                and fresh["bound_device"] != device and fresh["bind_device"]:
            reason = cards_service.REASON_DEVICE
            status = 403
        else:
            reason = cards_service.REASON_LIMIT
            status = 429
        ratelimit.record_attempt(db, ip, code_hash, False, reason, ua)
        return _reject(ip, code, reason, status)

    # 8) 一次性令牌（60 秒、绑定会话）
    token, exp = tokens.issue(db, config, card["id"], version["id"],
                              flask.session.get("sid", ""), ip)
    ratelimit.record_attempt(db, ip, code_hash, True, "", ua)
    flask.current_app.extensions["portal_log"].info(
        "下载准入 card=%s ip=%s version=%s",
        cardcode.mask_code(code), ip, version["version"])
    remain = max(0, int(card["max_per_day"])
                 - (int(card["day_count"]) + 1
                    if card["day_key"] == today else 1))
    return _render("校验通过，请在 60 秒内点击下载（令牌一次性）",
                   ok=True, token=token, exp=exp, remain=remain)


def _maybe_captcha(db, ip):
    config = _cfg()
    if ratelimit.needs_captcha(db, flask.session, ip, config):
        return ratelimit.make_captcha(flask.session)
    return None


@bp.get("/dl/<token>")
def download(token):
    config = _cfg()
    db = dbmod.get_db()
    sid = flask.session.get("sid", "")
    code, reason, row = tokens.check(db, config, token, sid)
    if code != 200:
        return flask.render_template("error.html", code=code, message=reason), code

    if not tokens.consume(db, row):
        return flask.render_template("error.html", code=410,
                                     message="下载令牌已被使用或已超时"), 410

    version = versions_service.get(db, row["version_id"])
    card = cards_service.get_card(db, row["card_id"])
    if version is None:
        return flask.render_template("error.html", code=404,
                                     message="版本不存在"), 404
    try:
        path = safe_storage_path(config.storage_dir, version["stored_path"])
        if not os.path.isfile(path):
            raise ValueError("文件缺失")
    except ValueError:
        flask.current_app.extensions["portal_log"].error(
            "版本文件路径非法 version=%s", version["id"])
        return flask.render_template("error.html", code=404,
                                     message="文件不存在"), 404

    db.execute("INSERT INTO downloads(ts,card_id,card_mask,ip,ua,version_id,version)"
               " VALUES(?,?,?,?,?,?,?)",
               (int(time.time()), row["card_id"],
                card["code_mask"] if card else "", client_ip(), user_agent(),
                version["id"], version["version"]))
    db.execute("UPDATE versions SET downloads=downloads+1 WHERE id=?",
               (version["id"],))
    db.commit()
    resp = flask.send_file(path, as_attachment=True,
                           download_name=version["filename"],
                           mimetype="application/zip", conditional=False)
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["X-Download-Version"] = version["version"]
    return resp
