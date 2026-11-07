"""限流与爆破计数：同 IP / 同卡密双维度，全部落在 attempts 表。"""
from __future__ import annotations

import time


def record_attempt(db, ip: str, card_hash: str, ok: bool, reason: str = "",
                   ua: str = "") -> None:
    db.execute("INSERT INTO attempts(ts, ip, card_hash, ok, reason, ua) "
               "VALUES(?,?,?,?,?,?)",
               (int(time.time()), (ip or "")[:64], (card_hash or "")[:64],
                1 if ok else 0, (reason or "")[:80], (ua or "")[:200]))
    db.commit()


def _count(db, where: str, args, seconds: int) -> int:
    since = int(time.time()) - seconds
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM attempts WHERE {where} AND ts>=?",
        (*args, since)).fetchone()
    return int(row["n"])


def ip_request_count(db, ip: str, seconds: int) -> int:
    return _count(db, "ip=?", (ip,), seconds)


def ip_fail_count(db, ip: str, seconds: int) -> int:
    return _count(db, "ip=? AND ok=0", (ip,), seconds)


def card_fail_count(db, card_hash: str, seconds: int) -> int:
    return _count(db, "card_hash=? AND ok=0", (card_hash,), seconds)


def card_locked(db, card_hash: str, limit: int, window: int,
                lock_seconds: int) -> bool:
    if not card_hash:
        return False
    fails = card_fail_count(db, card_hash, max(window, lock_seconds))
    if fails < limit:
        return False
    row = db.execute(
        "SELECT MAX(ts) AS last FROM attempts WHERE card_hash=? AND ok=0",
        (card_hash,)).fetchone()
    last = int(row["last"] or 0)
    return (int(time.time()) - last) < lock_seconds


# ---------------------------------------------------------------- 验证码
_CAPTCHA_KEY = "portal_captcha"


def needs_captcha(db, session, ip: str, config) -> bool:
    threshold = int(config.get("captcha_after_failures", 5))
    window = int(config.get("fail_window_seconds", 600))
    return ip_fail_count(db, ip, window) >= threshold


def make_captcha(session) -> str:
    """生成一道算术题，答案存 session（HttpOnly 签名 cookie）。"""
    import secrets
    a = secrets.randbelow(8) + 2
    b = secrets.randbelow(8) + 2
    session[_CAPTCHA_KEY] = {"a": a, "b": b}
    return f"{a} + {b} = ?"


def verify_captcha(session, answer: str) -> bool:
    item = session.get(_CAPTCHA_KEY)
    if not item:
        return False
    session.pop(_CAPTCHA_KEY, None)          # 一次性
    try:
        return int(str(answer).strip()) == int(item["a"]) + int(item["b"])
    except (TypeError, ValueError):
        return False
