"""RFC 6238 TOTP（标准库实现，避免多装 pyotp）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

DIGITS = 6
PERIOD = 30
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def new_secret(nbytes: int = 20) -> str:
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def _code(secret: str, counter: int) -> str:
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret.upper() + pad)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret: str, code: str, when: int | None = None,
           window: int = 1) -> bool:
    if not secret or not isinstance(code, str):
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return False
    now = int(when if when is not None else time.time())
    counter = now // PERIOD
    for delta in range(-window, window + 1):
        if hmac.compare_digest(_code(secret, counter + delta), code):
            return True
    return False


def otpauth_url(secret: str, username: str, issuer: str = "SJZ Portal") -> str:
    from urllib.parse import quote
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")
