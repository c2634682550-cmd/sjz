"""管理后台：登录 / TOTP / 发卡 / 版本 / 记录 / 概览 / 导出。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import time
from datetime import datetime, timedelta

import flask

from . import (cardcode, cards_service, db as dbmod, ratelimit, stats,
               tokens, totp, versions_service)
from .security import (admin_required, check_csrf, client_ip, current_admin,
                       hash_password, owner_required, safe_storage_path,
                       upload_error_message, user_agent, verify_password,
                       UploadError, safe_filename)

bp = flask.Blueprint("admin", __name__, url_prefix="/admin")

PAGE_SIZE = 50
_EXPORT_KINDS = ("cards", "downloads", "logins", "batches")


def _cfg():
    return flask.current_app.extensions["portal_config"]


def _signer():
    return flask.current_app.extensions["portal_signer"]


def _log():
    return flask.current_app.extensions["portal_log"]


def _uid():
    admin = current_admin() or {}
    return admin.get("id")


def _flash(kind: str, message: str):
    flask.session.setdefault("_flashes", []).append([kind, message])


def _take_flashes():
    return flask.session.pop("_flashes", [])


# ---------------------------------------------------------------- 登录

@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_admin():
        return flask.redirect(flask.url_for("admin.overview"))
    message = ""
    status = 200
    if flask.request.method == "POST":
        db = dbmod.get_db()
        config = _cfg()
        username = (flask.request.form.get("username") or "").strip()[:64]
        password = flask.request.form.get("password") or ""
        code = (flask.request.form.get("totp") or "").strip()
        ip = client_ip()
        ua = user_agent()
        row = db.execute("SELECT * FROM admins WHERE username=?",
                         (username,)).fetchone()
        now = int(time.time())
        if row and row["locked_until"] and int(row["locked_until"]) > now:
            wait = (int(row["locked_until"]) - now) // 60 + 1
            db.execute("INSERT INTO login_events(ts,username,ip,ua,ok,reason) "
                       "VALUES(?,?,?,?,0,?)", (now, username, ip, ua, "锁定中"))
            db.commit()
            return flask.render_template(
                "login.html", message=f"登录已锁定，请 {wait} 分钟后再试"), 429

        ok = bool(row) and verify_password(row["password_hash"], password)
        if ok and row["totp_enabled"] and row["totp_secret"]:
            ok = totp.verify(row["totp_secret"], code)
            if not ok:
                message = "动态验证码不正确"
        if not ok and not message:
            message = "用户名或口令不正确"

        if not ok:
            if row:
                fails = int(row["failed_count"]) + 1
                lock_until = None
                if fails >= int(config.get("admin_max_failures", 5)):
                    lock_until = now + int(config.get("admin_lock_seconds", 900))
                    fails = 0
                db.execute("UPDATE admins SET failed_count=?, locked_until=? "
                           "WHERE id=?", (fails, lock_until, row["id"]))
            db.execute("INSERT INTO login_events(ts,username,ip,ua,ok,reason) "
                       "VALUES(?,?,?,?,0,?)",
                       (now, username, ip, ua, "口令或验证码错误"))
            db.commit()
            _log().warning("后台登录失败 user=%s ip=%s", username, ip)
            return flask.render_template("login.html", message=message), 401

        db.execute("UPDATE admins SET failed_count=0, locked_until=NULL WHERE id=?",
                   (row["id"],))
        db.execute("INSERT INTO login_events(ts,username,ip,ua,ok,reason) "
                   "VALUES(?,?,?,?,1,?)", (now, username, ip, ua, ""))
        dbmod.audit(db, row["id"], "login", username, ip)
        flask.session.clear()
        flask.session.permanent = True
        flask.session["admin"] = {"id": row["id"], "username": row["username"],
                                  "role": row["role"], "login_at": now}
        flask.session["sid"] = secrets.token_hex(16)
        nxt = flask.request.args.get("next") or ""
        if not nxt.startswith("/admin"):
            nxt = flask.url_for("admin.overview")
        return flask.redirect(nxt)

    return flask.render_template("login.html", message=message), status


@bp.post("/logout")
@admin_required
def logout():
    admin = dict(flask.g.admin)
    flask.session.clear()
    _log().info("后台登出 user=%s", admin.get("username"))
    return flask.redirect(flask.url_for("admin.login"))


# ---------------------------------------------------------------- 概览

@bp.get("/")
@admin_required
def overview():
    db = dbmod.get_db()
    data = stats.overview(db, _cfg())
    recent = db.execute(
        "SELECT * FROM downloads ORDER BY id DESC LIMIT 8").fetchall()
    recent_cards = db.execute(
        "SELECT * FROM cards ORDER BY id DESC LIMIT 5").fetchall()
    return flask.render_template("overview.html", stats=data, recent=recent,
                                 recent_cards=recent_cards,
                                 flashes=_take_flashes())


@bp.get("/api/overview")
@admin_required
def api_overview():
    return flask.jsonify(stats.overview(dbmod.get_db(), _cfg()))


# ---------------------------------------------------------------- 发卡

def _card_filters():
    q = (flask.request.args.get("q") or "").strip()[:32]
    status = (flask.request.args.get("status") or "all").strip()
    where = []
    args = []
    if q:
        code = cardcode.normalize(q)
        if code:
            where.append("code_hash=?")
            args.append(cardcode.code_hash(code, _cfg().card_pepper))
        else:
            where.append("code_mask LIKE ?")
            args.append("%" + q.upper() + "%")
    now = int(time.time())
    if status == "active":
        where.append("revoked=0 AND (expires_at IS NULL OR expires_at>?)")
        args.append(now)
    elif status == "revoked":
        where.append("revoked=1")
    elif status == "expired":
        where.append("expires_at IS NOT NULL AND expires_at<=?")
        args.append(now)
    return (" WHERE " + " AND ".join(where) if where else ""), args, q, status


@bp.get("/cards")
@admin_required
def cards():
    db = dbmod.get_db()
    where, args, q, status = _card_filters()
    page_no = max(0, int(flask.request.args.get("page", 0) or 0))
    total = db.execute("SELECT COUNT(*) AS n FROM cards" + where,
                       args).fetchone()["n"]
    rows = db.execute("SELECT * FROM cards" + where + " ORDER BY id DESC "
                      "LIMIT ? OFFSET ?", (*args, PAGE_SIZE, page_no * PAGE_SIZE)
                      ).fetchall()
    batches = db.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 30"
                         ).fetchall()
    return flask.render_template(
        "cards.html", cards=rows, total=total, page=page_no,
        pages=(total + PAGE_SIZE - 1) // PAGE_SIZE, q=q, status=status,
        batches=batches, flashes=_take_flashes())


@bp.route("/cards/new", methods=["GET", "POST"])
@admin_required
def card_new():
    if flask.request.method == "POST":
        try:
            result = cards_service.create_batch(
                dbmod.get_db(), _cfg(), _signer(), count=1,
                days=_int_form("days", 365, 0, 3650),
                tier=(flask.request.form.get("tier") or "pro")[:32],
                note=(flask.request.form.get("note") or "")[:200],
                max_per_day=_int_form("max_per_day", 5, 1, 1000),
                bind_device=flask.request.form.get("bind_device") == "1",
                admin_id=_uid())
        except Exception as exc:               # noqa: BLE001
            _flash("error", f"发卡失败：{_safe_exc(exc)}")
            return flask.redirect(flask.url_for("admin.card_new"))
        dbmod.audit(dbmod.get_db(), _uid(), "issue_card",
                    result["codes"][0][:4] + "…", f"batch={result['batch_id']}")
        return flask.render_template(
            "batch_codes.html", codes=result["codes"], days=result["days"],
            tier=result["tier"], note=result["note"],
            batch_id=result["batch_id"])
    return flask.render_template("card_new.html", flashes=_take_flashes())


def _int_form(name, default, lo, hi):
    try:
        value = int(flask.request.form.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


@bp.post("/cards/batch")
@admin_required
def card_batch_create():
    count = _int_form("count", 10, 1, 200000)
    days = _int_form("days", 365, 0, 3650)
    tier = (flask.request.form.get("tier") or "pro")[:32]
    note = (flask.request.form.get("note") or "")[:200]
    max_per_day = _int_form("max_per_day", 5, 1, 1000)
    bind_device = flask.request.form.get("bind_device") == "1"
    try:
        result = cards_service.create_batch(
            dbmod.get_db(), _cfg(), _signer(), count=count, days=days,
            tier=tier, note=note, max_per_day=max_per_day,
            bind_device=bind_device, admin_id=_uid())
    except Exception as exc:                   # noqa: BLE001
        _flash("error", f"批量发卡失败：{_safe_exc(exc)}")
        return flask.redirect(flask.url_for("admin.cards"))
    dbmod.audit(dbmod.get_db(), _uid(), "issue_batch", f"batch={result['batch_id']}",
                f"count={count} days={days}")
    csv_token = _write_batch_csv(result)
    return flask.render_template(
        "batch_codes.html", codes=result["codes"], days=days, tier=tier,
        note=note, batch_id=result["batch_id"], csv_token=csv_token)


def _write_batch_csv(result) -> str:
    token = secrets.token_urlsafe(32)
    path = os.path.join(_cfg().export_dir, f"batch-{result['batch_id']}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["卡密", "档位", "有效期(天)", "备注"])
        for code in result["codes"]:
            writer.writerow([code, result["tier"], result["days"],
                             result["note"]])
    dbmod.get_db().execute(
        "INSERT INTO export_tokens(token_hash,batch_id,created_at,expires_at) "
        "VALUES(?,?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), result["batch_id"],
         int(time.time()), int(time.time()) + 3600))
    dbmod.get_db().commit()
    return token


@bp.get("/cards/batch/<int:batch_id>/csv")
@admin_required
def batch_csv(batch_id: int):
    token = flask.request.args.get("t", "")
    path = os.path.join(_cfg().export_dir, f"batch-{batch_id}.csv")
    db = dbmod.get_db()
    row = db.execute("SELECT * FROM export_tokens WHERE token_hash=?",
                     (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    now = int(time.time())
    if not row or row["batch_id"] != batch_id or row["consumed_at"] \
            or row["expires_at"] < now or not os.path.isfile(path):
        return "导出链接无效或已过期", 404
    db.execute("UPDATE export_tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
               (now, row["id"]))
    db.commit()
    return flask.send_file(path, as_attachment=True,
                           download_name=f"cards-batch-{batch_id}.csv",
                           mimetype="text/csv")


# ---------------------------------------------------------------- 单卡操作

def _card_or_404(card_id):
    card = cards_service.get_card(dbmod.get_db(), card_id)
    if card is None:
        flask.abort(404)
    return card


@bp.post("/cards/<int:card_id>/revoke")
@admin_required
def card_revoke(card_id):
    card = _card_or_404(card_id)
    cards_service.revoke(dbmod.get_db(), card,
                         (flask.request.form.get("reason") or "")[:200])
    dbmod.audit(dbmod.get_db(), _uid(), "revoke_card", card["code_mask"], "")
    _flash("ok", "已封禁该卡密")
    return flask.redirect(flask.request.referrer or flask.url_for("admin.cards"))


@bp.post("/cards/<int:card_id>/unrevoke")
@admin_required
def card_unrevoke(card_id):
    card = _card_or_404(card_id)
    cards_service.unrevoke(dbmod.get_db(), card)
    dbmod.audit(dbmod.get_db(), _uid(), "unrevoke_card", card["code_mask"], "")
    _flash("ok", "已解除封禁")
    return flask.redirect(flask.request.referrer or flask.url_for("admin.cards"))


@bp.post("/cards/<int:card_id>/renew")
@admin_required
def card_renew(card_id):
    card = _card_or_404(card_id)
    days = _int_form("days", 30, 0, 3650)
    try:
        new_exp = cards_service.renew(dbmod.get_db(), _signer(), card, days)
    except Exception as exc:                   # noqa: BLE001
        _flash("error", f"续期失败：{_safe_exc(exc)}")
    else:
        dbmod.audit(dbmod.get_db(), _uid(), "renew_card", card["code_mask"],
                    f"days={days} exp={new_exp}")
        _flash("ok", "续期完成" if days else "已改为永久有效")
    return flask.redirect(flask.request.referrer or flask.url_for("admin.cards"))


@bp.post("/cards/<int:card_id>/rebind")
@admin_required
def card_rebind(card_id):
    card = _card_or_404(card_id)
    device = "".join(ch for ch in (flask.request.form.get("device") or "")
                     if ch.isalnum() or ch in "-_")[:64]
    enable = flask.request.form.get("bind") == "1" if "bind" in flask.request.form \
        else bool(card["bind_device"])
    try:
        cards_service.rebind(dbmod.get_db(), _signer(), card,
                             device or None, enable=enable)
    except Exception as exc:                   # noqa: BLE001
        _flash("error", f"改绑失败：{_safe_exc(exc)}")
    else:
        dbmod.audit(dbmod.get_db(), _uid(), "rebind_card", card["code_mask"],
                    device or "(解绑)")
        _flash("ok", "设备绑定已更新")
    return flask.redirect(flask.request.referrer or flask.url_for("admin.cards"))


@bp.post("/cards/<int:card_id>/limits")
@admin_required
def card_limits(card_id):
    card = _card_or_404(card_id)
    try:
        cards_service.set_limits(dbmod.get_db(), _signer(), card,
                                 _int_form("max_per_day", 5, 1, 1000),
                                 flask.request.form.get("bind_device") == "1")
    except Exception as exc:                   # noqa: BLE001
        _flash("error", f"修改失败：{_safe_exc(exc)}")
    else:
        dbmod.audit(dbmod.get_db(), _uid(), "limits_card", card["code_mask"], "")
        _flash("ok", "限额与绑定开关已更新")
    return flask.redirect(flask.request.referrer or flask.url_for("admin.cards"))


# ---------------------------------------------------------------- 版本

@bp.get("/versions")
@admin_required
def versions():
    db = dbmod.get_db()
    rows = db.execute("SELECT * FROM versions ORDER BY uploaded_at DESC, id DESC"
                      ).fetchall()
    return flask.render_template("versions.html", versions=rows,
                                 signer=_signer(), flashes=_take_flashes())


@bp.post("/versions/upload")
@admin_required
def version_upload():
    if not _signer().ready:
        _flash("error", "签名器未就绪，暂不能上传版本（先修复私钥配置）")
        return flask.redirect(flask.url_for("admin.versions"))
    file = flask.request.files.get("package")
    if not file:
        _flash("error", "请选择 zip 文件")
        return flask.redirect(flask.url_for("admin.versions"))
    try:
        info = versions_service.save_upload(
            dbmod.get_db(), _cfg(), file,
            version=(flask.request.form.get("version") or "").strip(),
            notes=(flask.request.form.get("notes") or "")[:2000],
            force_update=flask.request.form.get("force_update") == "1")
    except UploadError as exc:
        _flash("error", f"上传被拒绝：{exc}")
        return flask.redirect(flask.url_for("admin.versions"))
    except Exception as exc:                   # noqa: BLE001
        _log().exception("版本上传异常：%s", flask.request.form.get("version"))
        _flash("error", f"上传失败：{_safe_exc(exc)}")
        return flask.redirect(flask.url_for("admin.versions"))
    dbmod.audit(dbmod.get_db(), _uid(), "upload_version", info["version"],
                f"sha256={info['sha256'][:12]} size={info['size']}")
    _flash("ok", f"版本 {info['version']} 已上传（SHA256 {info['sha256'][:16]}…）")
    return flask.redirect(flask.url_for("admin.versions"))


@bp.post("/versions/<int:version_id>/toggle")
@admin_required
def version_toggle(version_id):
    row = versions_service.get(dbmod.get_db(), version_id)
    if row is None:
        flask.abort(404)
    versions_service.set_enabled(dbmod.get_db(), row, not row["enabled"])
    dbmod.audit(dbmod.get_db(), _uid(), "toggle_version", row["version"],
                "on" if not row["enabled"] else "off")
    _flash("ok", "已上架" if not row["enabled"] else "已下架")
    return flask.redirect(flask.url_for("admin.versions"))


@bp.post("/versions/<int:version_id>/force")
@admin_required
def version_force(version_id):
    row = versions_service.get(dbmod.get_db(), version_id)
    if row is None:
        flask.abort(404)
    versions_service.set_force(dbmod.get_db(), row, not row["force_update"])
    dbmod.audit(dbmod.get_db(), _uid(), "force_version", row["version"], "")
    return flask.redirect(flask.url_for("admin.versions"))


@bp.get("/versions/<int:version_id>/file")
@admin_required
def version_file(version_id):
    row = versions_service.get(dbmod.get_db(), version_id)
    if row is None:
        flask.abort(404)
    try:
        path = safe_storage_path(_cfg().storage_dir, row["stored_path"])
    except ValueError:
        flask.abort(404)
    if not os.path.isfile(path) or \
            not versions_service.confirm_sha256(path, row["sha256"]):
        return "文件缺失或 SHA256 不匹配", 404
    return flask.send_file(path, as_attachment=True,
                           download_name=row["filename"],
                           mimetype="application/zip")


# ---------------------------------------------------------------- 记录

@bp.get("/logs")
@admin_required
def logs():
    db = dbmod.get_db()
    tab = (flask.request.args.get("tab") or "downloads").strip()
    if tab not in ("downloads", "issuance", "logins"):
        tab = "downloads"
    start, end = _date_range()
    q = (flask.request.args.get("q") or "").strip()[:32]
    page_no = max(0, int(flask.request.args.get("page", 0) or 0))
    rows, total = _log_query(db, tab, start, end, q, page_no)
    return flask.render_template(
        "logs.html", tab=tab, rows=rows, total=total, page=page_no,
        pages=(total + PAGE_SIZE - 1) // PAGE_SIZE,
        start=flask.request.args.get("start", ""),
        end=flask.request.args.get("end", ""), q=q,
        flashes=_take_flashes())


def _date_range():
    def parse(name):
        raw = (flask.request.args.get(name) or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=stats.tz(_cfg()))
            return int(dt.timestamp())
        except ValueError:
            return None
    start = parse("start")
    end = parse("end")
    if end is not None:
        end += 86400
    return start, end


def _log_query(db, tab, start, end, q, page_no):
    where, args = [], []
    if tab == "downloads":
        table, ts, mask = "downloads", "ts", "card_mask"
    elif tab == "logins":
        table, ts, mask = "login_events", "ts", None
    else:
        table, ts, mask = "audit", "ts", None
    if start is not None:
        where.append(f"{ts}>=?")
        args.append(start)
    if end is not None:
        where.append(f"{ts}<?")
        args.append(end)
    if q:
        if mask:
            code = cardcode.normalize(q)
            if code:
                where.append(f"{mask}=?")
                args.append(cardcode.mask_code(code))
            else:
                where.append(f"{mask} LIKE ?")
                args.append("%" + q.upper() + "%")
        elif tab == "logins":
            where.append("(username LIKE ? OR ip LIKE ?)")
            args.extend(["%" + q + "%", "%" + q + "%"])
        else:
            where.append("(action LIKE ? OR target LIKE ?)")
            args.extend(["%" + q + "%", "%" + q + "%"])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(f"SELECT COUNT(*) AS n FROM {table}" + clause,
                       args).fetchone()["n"]
    rows = db.execute(f"SELECT * FROM {table}" + clause + " ORDER BY id DESC "
                      "LIMIT ? OFFSET ?", (*args, PAGE_SIZE, page_no * PAGE_SIZE)
                      ).fetchall()
    return rows, total


@bp.get("/export/<kind>.csv")
@admin_required
def export_csv(kind: str):
    """导出参数只认白名单 kinds；不接受任何文件名/路径参数。"""
    if kind not in _EXPORT_KINDS:
        return "不支持的导出类型", 400
    db = dbmod.get_db()
    start, end = _date_range()
    q = (flask.request.args.get("q") or "").strip()[:32]
    stamp = datetime.now(stats.tz(_cfg())).strftime("%Y%m%d-%H%M%S")
    if kind == "cards":
        where, args, _q, _s = _card_filters()
        rows = db.execute("SELECT * FROM cards" + where + " ORDER BY id DESC",
                          args).fetchall()
        header = ["ID", "卡密掩码", "档位", "有效期到", "封禁", "绑定设备",
                  "每日上限", "总下载", "创建时间"]
        data = [[r["id"], r["code_mask"], r["tier"],
                 _fmt_ts(r["expires_at"]), "是" if r["revoked"] else "否",
                 r["bound_device"] or "", r["max_per_day"],
                 r["total_downloads"], _fmt_ts(r["created_at"])] for r in rows]
    elif kind == "downloads":
        where, args = _range_where(start, end)
        rows = db.execute("SELECT * FROM downloads" + where +
                          " ORDER BY id DESC", args).fetchall()
        header = ["时间", "卡密掩码", "IP", "UA", "版本"]
        data = [[_fmt_ts(r["ts"]), r["card_mask"], r["ip"], r["ua"],
                 r["version"]] for r in rows]
    elif kind == "logins":
        where, args = _range_where(start, end)
        rows = db.execute("SELECT * FROM login_events" + where +
                          " ORDER BY id DESC", args).fetchall()
        header = ["时间", "用户名", "IP", "UA", "成功", "原因"]
        data = [[_fmt_ts(r["ts"]), r["username"], r["ip"], r["ua"],
                 "是" if r["ok"] else "否", r["reason"]] for r in rows]
    else:
        rows = db.execute("SELECT * FROM audit ORDER BY id DESC").fetchall()
        header = ["时间", "管理员", "动作", "对象", "详情"]
        data = [[_fmt_ts(r["ts"]), r["admin_id"], r["action"], r["target"],
                 r["detail"]] for r in rows]

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield "\ufeff" + buf.getvalue()
        for row in data:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow(row)
            yield buf.getvalue()

    resp = flask.Response(flask.stream_with_context(generate()),
                          mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        f"attachment; filename={kind}-{stamp}.csv"
    resp.headers["Cache-Control"] = "no-store"
    dbmod.audit(db, _uid(), "export", kind, f"start={start} end={end}")
    return resp


def _range_where(start, end):
    where, args = [], []
    if start is not None:
        where.append("ts>=?")
        args.append(start)
    if end is not None:
        where.append("ts<?")
        args.append(end)
    return ((" WHERE " + " AND ".join(where)) if where else ""), args


def _fmt_ts(value):
    if value in (None, ""):
        return ""
    return datetime.fromtimestamp(int(value), stats.tz(_cfg())).strftime(
        "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 管理员 / TOTP

@bp.get("/admins")
@owner_required
def admins():
    db = dbmod.get_db()
    rows = db.execute("SELECT * FROM admins ORDER BY id").fetchall()
    return flask.render_template("admins.html", admins=rows,
                                 flashes=_take_flashes())


@bp.post("/admins/create")
@owner_required
def admin_create():
    username = (flask.request.form.get("username") or "").strip()
    password = flask.request.form.get("password") or ""
    role = "staff" if flask.request.form.get("role") != "owner" else "owner"
    if not (3 <= len(username) <= 32) or any(not c.isalnum() and c not in "-_" for c in username):
        _flash("error", "用户名只允许 3-32 位字母/数字/-_")
        return flask.redirect(flask.url_for("admin.admins"))
    if len(password) < 10:
        _flash("error", "口令至少 10 位（建议 14 位以上）")
        return flask.redirect(flask.url_for("admin.admins"))
    db = dbmod.get_db()
    try:
        db.execute("INSERT INTO admins(username,password_hash,role,created_at) "
                   "VALUES(?,?,?,?)",
                   (username, hash_password(password), role, int(time.time())))
        db.commit()
    except Exception:                          # noqa: BLE001
        _flash("error", "用户名已存在")
        return flask.redirect(flask.url_for("admin.admins"))
    dbmod.audit(db, _uid(), "create_admin", username, role)
    _flash("ok", f"已创建管理员 {username}（{role}）")
    return flask.redirect(flask.url_for("admin.admins"))


@bp.post("/admins/<int:admin_id>/delete")
@owner_required
def admin_delete(admin_id):
    if admin_id == _uid():
        _flash("error", "不能删除当前登录的账号")
        return flask.redirect(flask.url_for("admin.admins"))
    db = dbmod.get_db()
    db.execute("DELETE FROM admins WHERE id=?", (admin_id,))
    db.commit()
    dbmod.audit(db, _uid(), "delete_admin", str(admin_id), "")
    _flash("ok", "已删除")
    return flask.redirect(flask.url_for("admin.admins"))


@bp.route("/security", methods=["GET", "POST"])
@admin_required
def security_page():
    db = dbmod.get_db()
    row = db.execute("SELECT * FROM admins WHERE id=?",
                     (_uid(),)).fetchone()
    if flask.request.method == "POST":
        action = flask.request.form.get("action")
        if action == "start":
            secret = totp.new_secret()
            flask.session["totp_pending"] = secret
            return flask.render_template(
                "security.html", admin=row, pending=secret,
                otpauth=totp.otpauth_url(secret, row["username"]),
                flashes=_take_flashes())
        if action == "enable":
            pending = flask.session.get("totp_pending") or ""
            code = flask.request.form.get("code", "")
            if not pending or not totp.verify(pending, code):
                _flash("error", "验证码不正确，请用验证器里的 6 位数字")
            else:
                db.execute("UPDATE admins SET totp_secret=?, totp_enabled=1 "
                           "WHERE id=?", (pending, _uid()),)
                db.commit()
                flask.session.pop("totp_pending", None)
                dbmod.audit(db, _uid(), "enable_totp", row["username"], "")
                _flash("ok", "双重验证已启用")
        if action == "disable":
            code = flask.request.form.get("code", "")
            if row["totp_enabled"] and not totp.verify(row["totp_secret"], code):
                _flash("error", "关闭双重验证需要当前动态验证码")
            else:
                db.execute("UPDATE admins SET totp_secret=NULL, totp_enabled=0 "
                           "WHERE id=?", (_uid(),))
                db.commit()
                dbmod.audit(db, _uid(), "disable_totp", row["username"], "")
                _flash("ok", "双重验证已关闭")
        return flask.redirect(flask.url_for("admin.security_page"))
    row = db.execute("SELECT * FROM admins WHERE id=?", (_uid(),)).fetchone()
    return flask.render_template("security.html", admin=row,
                                 pending=flask.session.get("totp_pending"),
                                 otpauth=totp.otpauth_url(
                                     flask.session["totp_pending"],
                                     row["username"])
                                 if flask.session.get("totp_pending") else "",
                                 flashes=_take_flashes())


# ---------------------------------------------------------------- 预留对接点

def _safe_exc(exc) -> str:
    text = upload_error_message(exc)
    return text[:200]
