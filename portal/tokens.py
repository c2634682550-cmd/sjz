"""一次性下载令牌：签名 + 60 秒 + 单次 + 绑定会话。

令牌 = nonce.<hmac>，服务端只存 sha256(令牌)。即使数据库泄露也拿不到
可用令牌；兑换用一条原子 UPDATE 完成，并发下只有一个请求能赢。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

TTL_FALLBACK = 60


def _mac_key(config) -> bytes:
    return hashlib.sha256(
        ("portal-download-token:" + config.secret_key).encode("utf-8")).digest()


def _mac(config, nonce: str, card_id: int, version_id: int, exp: int,
         session_id: str) -> str:
    msg = f"{nonce}|{card_id}|{version_id}|{exp}|{session_id}".encode("utf-8")
    return hmac.new(_mac_key(config), msg, hashlib.sha256).hexdigest()


def issue(db, config, card_id: int, version_id: int, session_id: str,
          ip: str = "") -> tuple[str, int]:
    now = int(time.time())
    ttl = int(config.get("token_ttl_seconds", TTL_FALLBACK))
    exp = now + ttl
    nonce = secrets.token_urlsafe(24)
    token = f"{nonce}.{_mac(config, nonce, card_id, version_id, exp, session_id)[:40]}"
    db.execute(
        "INSERT INTO tokens(token_hash, card_id, version_id, session_id, ip, "
        "created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), card_id, version_id,
         session_id, ip, now, exp))
    db.commit()
    return token, exp


def lookup(db, token: str):
    if not isinstance(token, str) or len(token) > 200:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    return db.execute("SELECT * FROM tokens WHERE token_hash=?",
                      (digest,)).fetchone()


def verify_signature(config, row, token: str) -> bool:
    """校验 URL 里自带的 HMAC 签名（数据库只存哈希，签名靠行数据重算）。"""
    if not isinstance(token, str) or "." not in token:
        return False
    nonce, mac = token.rsplit(".", 1)
    expected = _mac(config, nonce, row["card_id"], row["version_id"],
                    row["expires_at"], row["session_id"])[:40]
    return hmac.compare_digest(mac, expected)


def check(db, config, token: str, session_id: str) -> tuple[int, str, object]:
    """只检查不消费。返回 (HTTP 状态码, 原因, row)。"""
    row = lookup(db, token)
    if row is None:
        return 404, "下载令牌不存在或格式不正确", None
    if not verify_signature(config, row, token):
        return 403, "下载令牌签名无效", row
    if row["consumed_at"] is not None:
        return 410, "下载令牌已经被使用过（一次性）", row
    if row["expires_at"] <= int(time.time()):
        return 410, "下载令牌已超时（60 秒有效）", row
    if not hmac.compare_digest(row["session_id"], session_id or ""):
        return 403, "下载令牌与当前会话不匹配", row
    return 200, "", row


def consume(db, row) -> bool:
    """原子兑换：只有未消费且未过期的行能更新成功。"""
    cur = db.execute(
        "UPDATE tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL "
        "AND expires_at>?",
        (int(time.time()), row["id"], int(time.time())))
    db.commit()
    return cur.rowcount == 1


def sweep_expired(db, older_than_seconds: int = 3600) -> int:
    cur = db.execute("DELETE FROM tokens WHERE expires_at < ?",
                     (int(time.time()) - older_than_seconds,))
    db.commit()
    return cur.rowcount
