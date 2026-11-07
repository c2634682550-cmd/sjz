"""P-256 签名适配层：直接复用 core/license.py 的曲线实现与公钥。

依赖说明
--------
另一条线正在把许可体系从 Ed25519 迁到 ECDSA P-256（secp256r1 / SHA-256 /
IEEE P1363 r||s）。本模块**不实现第二套密码学**，只做三件事：

1. 公钥直接读 `core.license.PUBLIC_KEY_B64`（现有许可数据，编译期常量）；
2. 验签用 `core.license.p256_verify` + `core.license.canonical_bytes`，
   与 `verify_blob` 完全同一套规范化/编码；
3. 签名用 `core.license.sign_blob`，密钥文件默认在仓库外
   （`core.license.default_signing_key_path()`，或环境变量 SJZ_SIGNING_KEY）。

卡密签名文档有两类：
  dl-batch  {v,kind,batch_id,count,exp,tier,max_per_day,bind_device,
             manifest_sha256,created_at}
            签名覆盖整个批次清单文件（每行一个 code_hash），验签后再做
            哈希成员判定 —— 等价于“每张卡都被 P-256 背书”，但签发 10 万
            张只需要一次签名。
  dl-card   {v,kind,card_hash,batch_id,exp,max_per_day,bind_device,gen,changed_at}
            续期 / 改绑 / 改每日上限时追加，覆盖数据库里可能被篡改的字段。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import sys
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import license as core_license      # noqa: E402

PUBLIC_KEY_B64 = core_license.PUBLIC_KEY_B64
SIG_ALG = core_license.SIG_ALG


class SignatureError(Exception):
    """签名/文档格式不合法。对外只显示固定的中文原因。"""


def _decode_sig(text) -> bytes:
    try:
        raw = base64.b64decode(str(text).strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("签名不是合法 base64") from exc
    if len(raw) != 64:
        raise SignatureError("签名长度必须是 64 字节（r||s）")
    return raw


def verify_payload(payload: dict, sig: str) -> bool:
    """公钥验证 payload 的 P-256 签名。任何异常都吞成 False。"""
    if not isinstance(payload, dict) or not isinstance(sig, str):
        return False
    try:
        raw = _decode_sig(sig)
        return core_license.p256_verify(
            PUBLIC_KEY_B64, core_license.canonical_bytes(payload), raw)
    except (SignatureError, TypeError, ValueError):
        return False


def sign_payload(payload: dict, seed_hex: str) -> dict:
    """返回 {"payload":..., "sig": base64}。seed 由调用方保证不出日志。"""
    sig = core_license.p256_sign(seed_hex, core_license.canonical_bytes(payload))
    return {"payload": payload,
            "sig": base64.b64encode(sig).decode("ascii")}


def load_seed(path: str) -> str | None:
    """读私钥文件第一行非注释内容；不存在返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f
                 if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return None
    seed = lines[0].lower()
    if len(seed) != 64 or any(c not in "0123456789abcdef" for c in seed):
        return None
    return seed


class Signer:
    """应用级签名器：启动自检 + 批次清单验签缓存。

    缓存的是 (批次签名, 清单哈希, 哈希集合)；签名或清单变化即失效。
    """

    def __init__(self, config):
        self.seed = load_seed(config.signing_key_file)
        self.ready = False
        self.dev_key = False
        self.public_key_b64 = ""
        self.error = "未配置签名私钥"
        self._cache: dict[int, tuple] = {}
        self._lock = threading.Lock()
        if self.seed:
            self._selftest()

    def _selftest(self):
        """用**本次实际加载的私钥**派生公钥做往返，不拿固定公钥硬比。

        与编译期公钥不配对时仍可启动（dev 联调），但会标 dev_key 并告警：
        生产客户端只认编译期公钥，dev 签出的卡会被拒。
        """
        probe = {"v": 1, "kind": "portal-selftest", "ts": 0}
        try:
            self.public_key_b64 = core_license.public_key_b64_from_priv(self.seed)
            blob = sign_payload(probe, self.seed)
            raw = base64.b64decode(blob["sig"])
            ok = core_license.p256_verify(self.public_key_b64,
                                          core_license.canonical_bytes(probe), raw)
        except Exception:                     # noqa: BLE001
            self.error = "签名私钥格式错误"
            return
        if not ok:
            self.error = "签名私钥自检失败（私钥与派生公钥往返不一致）"
            return
        self.ready = True
        self.dev_key = self.public_key_b64 != PUBLIC_KEY_B64
        if self.dev_key:
            self.error = "当前使用 dev 密钥：签出的卡只能用于开发环境"
            logging.getLogger(__name__).warning(
                "%s（派生公钥指纹 %s）", self.error,
                hashlib.sha256(self.public_key_b64.encode()).hexdigest()[:16])
        else:
            self.error = ""

    def _verify_own(self, payload: dict, sig) -> bool:
        """用本签名器自己的公钥验（prod 时等于编译期公钥，dev 时可自洽）。"""
        try:
            raw = _decode_sig(sig)
            return core_license.p256_verify(
                self.public_key_b64 or PUBLIC_KEY_B64,
                core_license.canonical_bytes(payload), raw)
        except (SignatureError, TypeError, ValueError):
            return False

    def sign(self, payload: dict) -> dict:
        if not self.ready:
            raise SignatureError(self.error or "签名器不可用")
        return sign_payload(payload, self.seed)

    # ------------------------------------------------------------ 批次验证
    def verify_batch(self, db, batch_row, storage_dir: str) -> set[str]:
        """验批次签名 + 清单完整性，返回 code_hash 集合。失败抛 SignatureError。"""
        if batch_row is None:
            raise SignatureError("批次不存在")
        batch_id = batch_row["id"]
        manifest_path = os.path.join(storage_dir, batch_row["manifest_file"])
        try:
            with open(manifest_path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            raise SignatureError("批次清单缺失") from exc
        digest = hashlib.sha256(raw).hexdigest()

        key = (batch_id, batch_row["sig_blob"], digest)
        with self._lock:
            hit = self._cache.get(batch_id)
        if hit and hit[0] == key:
            return hit[1]

        try:
            blob = json.loads(batch_row["sig_blob"])
        except (TypeError, ValueError) as exc:
            raise SignatureError("批次签名文档损坏") from exc
        payload = blob.get("payload") if isinstance(blob, dict) else None
        if not isinstance(payload, dict) or payload.get("kind") != "dl-batch":
            raise SignatureError("批次签名文档类型不对")
        if payload.get("batch_id") != batch_id:
            raise SignatureError("批次签名与批次号不匹配")
        if payload.get("manifest_sha256") != digest:
            raise SignatureError("批次清单被改动（哈希不一致）")
        if payload.get("count") != batch_row["count"]:
            raise SignatureError("批次签名数量不一致")
        if not self._verify_own(payload, blob.get("sig")):
            raise SignatureError("批次签名校验失败")

        leaves = set()
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if len(line) == 64:
                leaves.add(line)
        with self._lock:
            if len(self._cache) >= 4:
                self._cache.pop(next(iter(self._cache)))
            self._cache[batch_id] = (key, leaves)
        return leaves

    # ------------------------------------------------------------ 单卡修订
    def verify_amend(self, card_row) -> dict | None:
        """校验续期/改绑文档；没有文档返回 None，有但非法抛异常。"""
        blob_text = card_row["amend_blob"]
        if not blob_text:
            return None
        try:
            blob = json.loads(blob_text)
        except (TypeError, ValueError) as exc:
            raise SignatureError("卡密修订文档损坏") from exc
        payload = blob.get("payload") if isinstance(blob, dict) else None
        if not isinstance(payload, dict) or payload.get("kind") != "dl-card":
            raise SignatureError("卡密修订文档类型不对")
        if payload.get("card_hash") != card_row["code_hash"]:
            raise SignatureError("卡密修订文档与卡密不匹配")
        if payload.get("gen") != card_row["amend_gen"]:
            raise SignatureError("卡密修订代数不匹配")
        if not self._verify_own(payload, blob.get("sig")):
            raise SignatureError("卡密修订签名校验失败")
        return payload
