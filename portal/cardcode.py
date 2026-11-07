"""卡密字符串：生成 / 规范化 / 哈希 / 掩码。

明文卡密只在“生成的那一刻”出现在响应里，之后数据库里只有 HMAC 哈希。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# 与 tools/license_admin.py 同一套 Crockford base32（去掉 I/L/O/U）
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BODY_LEN = 16
PREFIX = "SJZ-"

# 只接受大写字母/数字 + 固定分段；服务端在哈希前统一规范化
_CANON_RE = re.compile(r"^SJZ-[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}$")


def generate_code() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LEN))
    return PREFIX + "-".join(body[i:i + 4] for i in range(0, BODY_LEN, 4))


def normalize(raw: str | None) -> str | None:
    """用户输入 → 规范形态；格式不对返回 None。

    容忍小写、空格、全角空格、缺失或多余的连字符；其余一律判格式错误。
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().upper().replace("　", "").replace(" ", "")
    compact = s.replace("-", "")
    if len(compact) != 3 + BODY_LEN or not compact.startswith("SJZ"):
        return None
    body = compact[3:]
    if any(ch not in ALPHABET for ch in body):
        return None
    canon = PREFIX + "-".join(body[i:i + 4] for i in range(0, BODY_LEN, 4))
    return canon if _CANON_RE.match(canon) else None


def code_hash(code: str, pepper: str) -> str:
    """HMAC-SHA256(pepper, canonical_code)。数据库里只出现这个值。"""
    return hmac.new(pepper.encode("utf-8"), code.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def mask_code(code: str) -> str:
    """显示用：保留卡体前 4 后 4，中间全掩码。"""
    body = code.replace(PREFIX, "").replace("-", "")
    return f"{PREFIX}{body[:4]}-****-****-{body[-4:]}"


def mask_hash(code_hash_value: str) -> str:
    return "hash:" + code_hash_value[:8] + "…"


def looks_like_code(raw: str) -> bool:
    return normalize(raw) is not None
