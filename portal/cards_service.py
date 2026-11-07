"""发卡、续期、改绑、作废 —— 所有写操作都在这里，路由层只做参数收口。"""
from __future__ import annotations

import json
import os
import secrets
import time

from . import cardcode, db as dbmod

MAX_BATCH = 200000


def _manifest_dir(config) -> str:
    path = os.path.join(config.storage_dir, "batches")
    os.makedirs(path, exist_ok=True)
    return path


def create_batch(db, config, signer, *, count: int, days: int, tier: str,
                 note: str = "", max_per_day: int = 5, bind_device: bool = False,
                 admin_id=None) -> dict:
    """生成一批卡密：写清单 → P-256 签批次 → 落库。明文只返回这一次。"""
    count = max(1, min(int(count), MAX_BATCH))
    days = max(0, int(days))
    max_per_day = max(1, min(int(max_per_day), 1000))
    now = int(time.time())
    expires = None if days == 0 else now + days * 86400

    codes = [cardcode.generate_code() for _ in range(count)]
    canon = []
    seen = set()
    for code in codes:                      # 极端情况下的重复直接重抽
        while True:
            h = cardcode.code_hash(code, config.card_pepper)
            if h not in seen:
                seen.add(h)
                canon.append((code, h))
                break
            code = cardcode.generate_code()
    hashes = sorted(h for _c, h in canon)

    cur = db.execute(
        "INSERT INTO batches(created_at,count,days,tier,note,max_per_day,"
        "bind_device,manifest_file,manifest_sha256,sig_blob,created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (now, count, days, tier, note, max_per_day, 1 if bind_device else 0,
         "", "", "", admin_id))
    db.commit()
    batch_id = cur.lastrowid

    manifest_rel = f"batches/{batch_id}.txt"
    _manifest_dir(config)
    manifest_abs = os.path.join(config.storage_dir, manifest_rel)
    with open(manifest_abs, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(hashes) + "\n")
    import hashlib
    with open(manifest_abs, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()

    payload = {"v": 1, "kind": "dl-batch", "batch_id": batch_id,
               "count": count, "exp": expires, "tier": tier,
               "max_per_day": max_per_day,
               "bind_device": 1 if bind_device else 0,
               "manifest_sha256": digest, "created_at": now}
    blob = signer.sign(payload)
    db.execute("UPDATE batches SET manifest_file=?,manifest_sha256=?,sig_blob=? "
               "WHERE id=?", (manifest_rel, digest,
                              json.dumps(blob, ensure_ascii=False), batch_id))

    rows = []
    for code, h in canon:
        rows.append((h, cardcode.mask_code(code), batch_id, tier, days, note,
                     now, expires, max_per_day, 1 if bind_device else 0))
    db.executemany(
        "INSERT INTO cards(code_hash,code_mask,batch_id,tier,days,note,"
        "created_at,expires_at,max_per_day,bind_device) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return {"batch_id": batch_id, "codes": [c for c, _h in canon],
            "count": count, "days": days, "tier": tier, "expires_at": expires,
            "note": note}


def get_batch(db, batch_id: int):
    return db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def get_card(db, card_id: int):
    return db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()


def find_card_by_code(db, config, code: str):
    h = cardcode.code_hash(code, config.card_pepper)
    return db.execute("SELECT * FROM cards WHERE code_hash=?", (h,)).fetchone()


def _amend(db, signer, card, *, expires_at, max_per_day, bind_device,
           bound_device, changed_at) -> None:
    gen = int(card["amend_gen"]) + 1
    payload = {"v": 1, "kind": "dl-card", "card_hash": card["code_hash"],
               "batch_id": card["batch_id"], "exp": expires_at,
               "max_per_day": int(max_per_day),
               "bind_device": 1 if bind_device else 0,
               "gen": gen, "changed_at": changed_at}
    blob = signer.sign(payload)
    db.execute(
        "UPDATE cards SET expires_at=?, max_per_day=?, bind_device=?, "
        "bound_device=?, amend_gen=?, amend_blob=?, changed_at=? WHERE id=?",
        (expires_at, int(max_per_day), 1 if bind_device else 0,
         bound_device, gen, json.dumps(blob, ensure_ascii=False), changed_at,
         card["id"]))
    db.commit()


def renew(db, signer, card, days: int) -> int | None:
    """从 max(现在, 原到期) 往后延长 days 天；days<=0 视为永久。"""
    now = int(time.time())
    if int(days) <= 0:
        expires = None
    else:
        base = max(now, int(card["expires_at"] or now))
        expires = base + int(days) * 86400
    _amend(db, signer, card, expires_at=expires,
           max_per_day=card["max_per_day"], bind_device=bool(card["bind_device"]),
           bound_device=card["bound_device"], changed_at=now)
    return expires


def rebind(db, signer, card, device: str | None,
           enable: bool | None = None) -> None:
    now = int(time.time())
    bind = bool(card["bind_device"]) if enable is None else bool(enable)
    _amend(db, signer, card, expires_at=card["expires_at"],
           max_per_day=card["max_per_day"], bind_device=bind,
           bound_device=(device or None), changed_at=now)


def set_limits(db, signer, card, max_per_day: int,
               bind_device: bool) -> None:
    now = int(time.time())
    _amend(db, signer, card, expires_at=card["expires_at"],
           max_per_day=max(1, min(int(max_per_day), 1000)),
           bind_device=bind_device,
           bound_device=card["bound_device"], changed_at=now)


def revoke(db, card, reason: str = "") -> bool:
    if card["revoked"]:
        return False
    db.execute("UPDATE cards SET revoked=1, revoked_at=?, "
               "revoke_reason=? WHERE id=?",
               (int(time.time()), reason[:200], card["id"]))
    db.commit()
    return True


def unrevoke(db, card) -> bool:
    if not card["revoked"]:
        return False
    db.execute("UPDATE cards SET revoked=0, revoked_at=NULL "
               "WHERE id=?", (card["id"],))
    db.commit()
    return True


# ---------------------------------------------------------------- 下载判定

REASON_FORMAT = "卡密格式不正确"
REASON_NOT_FOUND = "卡密不存在"
REASON_SIG = "卡密签名无效（数据被篡改，请联系管理员）"
REASON_REVOKED = "卡密已被封禁"
REASON_EXPIRED = "卡密已过期"
REASON_DEVICE = "设备不匹配（该卡已绑定其他设备）"
REASON_LIMIT = "下载太频繁（该卡今日下载次数已用完）"
REASON_NONE_VERSION = "暂无可下载版本"


def evaluate_card(db, config, signer, code: str, device: str | None):
    """按需求顺序做全部校验。返回 (card_row, reason)；reason 为空表示通过。

    签名校验放在有效期/封禁之前：批次清单与单卡修订都必须先证明是本系统
    签发的，再去谈状态。封禁/日限额是服务端权威状态，不依赖签名。
    """
    metrics = {}
    card_hash = cardcode.code_hash(code, config.card_pepper)
    card = db.execute("SELECT * FROM cards WHERE code_hash=?",
                      (card_hash,)).fetchone()
    if card is None:
        return None, REASON_NOT_FOUND
    batch = get_batch(db, card["batch_id"])
    try:
        leaves = signer.verify_batch(db, batch, config.storage_dir)
    except Exception:                          # noqa: BLE001
        return card, REASON_SIG
    if card["code_hash"] not in leaves:
        return card, REASON_SIG
    try:
        amend = signer.verify_amend(card)
    except Exception:                          # noqa: BLE001
        return card, REASON_SIG
    metrics["amend"] = bool(amend)
    if card["revoked"]:
        return card, REASON_REVOKED
    exp = card["expires_at"]
    if exp is not None and int(exp) <= int(time.time()):
        return card, REASON_EXPIRED
    if device and int(card["bind_device"]) and card["bound_device"] \
            and card["bound_device"] != device:
        return card, REASON_DEVICE
    return card, ""


def consume_download_quota(db, card, device: str | None, today: str,
                           bind_now: bool) -> bool:
    """原子地：按天重置计数 + 增加下载次数 + 必要时绑定设备。

    返回 False 表示并发/限额/绑定冲突 —— 调用方重新读一次卡状态给出具体原因。
    """
    now = int(time.time())
    cur = db.execute(
        "UPDATE cards SET "
        "day_key=CASE WHEN day_key=? THEN day_key ELSE ? END, "
        "day_count=CASE WHEN day_key=? THEN day_count+1 ELSE 1 END, "
        "total_downloads=total_downloads+1, "
        "first_used_at=COALESCE(first_used_at, ?), last_used_at=?, "
        "bound_device=CASE WHEN ? THEN COALESCE(bound_device, ?) "
        "                  ELSE bound_device END "
        "WHERE id=? AND revoked=0 "
        "AND (expires_at IS NULL OR expires_at>?) "
        "AND (CASE WHEN day_key=? THEN day_count ELSE 0 END) < max_per_day "
        "AND (?=0 OR bound_device IS NULL OR bound_device=?)",
        (today, today, today, now, now, 1 if bind_now else 0, device,
         card["id"], now, today, 1 if bind_now else 0, device))
    db.commit()
    return cur.rowcount == 1
