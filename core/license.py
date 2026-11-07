"""许可（卡密）客户端：验签、机器指纹、离线账本、吊销、试用。

签名算法：**ECDSA P-256（secp256r1）+ SHA-256，签名编码 IEEE P1363（r||s）**。
选它的唯一原因是启动器必须是纯 .NET Framework 4.8，而 BCL 没有 Ed25519；
P-256 三处（本模块 / tools/license_server.py / launcher）共用同一曲线与编码，
公钥是 base64(X||Y)、签名是 base64(r||s 各 32 字节)，不做 DER 解析、不做双算法兼容。
旧 Ed25519 卡密一律作废重发（见 _launcher_net_fx.md 的迁移章节）。

设计前提：**判定在服务端，本地只保存签名许可**。
  · 许可文件里只有「服务端签发时的 payload + ECDSA 签名」；
  · 公钥编译进程序（`PUBLIC_KEY_B64`），运行期**不接受**任何参数 /
    配置 / 环境变量替换（R10-L09）；
  · 本地能做的只有「验签 + 指纹 + 时间账本」——这三样全都能被改，
    但改完之后签名对不上，或者账本完整性对不上，就会被判失效。

文件（默认在 `%LOCALAPPDATA%\\SJZ-Control\\`）：
  license.json      服务端签发的许可（payload + sig）
  state.json        离线时间账本 + 已见吊销列表代数（HMAC 签名，防记事本改）
  revocations.json  服务端签发的吊销列表（必须验签，缺失不等于放行）
  trial.json        本地试用记录（HMAC 签名 + 注册表 / ProgramData 多副本）
  anchor.json       锚点副本（ProgramData；锚点出生时间/累计秒数，防删账本重置）

锚点两种来源（`Decision.anchor`）：
  online     见过服务端：以 server_now 为基点，联网成功即清零离线累计并续租期
  unproven   本机首次激活没联网：签名合法的许可 + 指纹通过即可建立，
             以本机墙钟为出生点，按单调时钟累计，给满「租期 + 缓冲」，
             累计耗尽后必须联网校验才能续期（永久许可也不例外）

状态码（`Decision.status`）：
  ok           正常
  ok_grace     过了到期日但在宽限期内 / 离线超过租期但在离线宽限内
  need_online  离线超出「租期 + 离线宽限」/ 未联网锚点租期耗尽 / 账本与许可不匹配
  expired      过了「到期 + 宽限」
  revoked      许可被吊销 / 试用被拉黑
  fp_mismatch  指纹匹配度不够（换机器 / 许可里组件太少）
  sig_invalid  签名或格式不对（被改过）
  clock_tamper 连续两次检测到回拨
  none         没有许可（由调用方决定是否进试用）

接入口：
  · desktop.main() 在开窗前：`d = license.ensure(server=...); if not d.ok: 激活提示`
  · server.py 写操作路由：`license.server_gate_ok()`
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

VERSION = 1

# 发布公钥：编译期固定。私钥不在仓库、不在安装包，只在签发服务器。
# 编码 = base64(X || Y)，X/Y 各 32 字节大端；曲线固定 P-256，没有算法协商。
# 换钥用 `tools/keygen.py` 生成新的一对，把公钥填到这里，并同步
# `launcher/Crypto.cs` 的 PublicKeyB64 后重编启动器（仓库里只放公钥）。
# 运行期任何 `public_key_b64=` 形式的参数旁路都已移除（R10-L09）。
PUBLIC_KEY_B64 = "VtWMhwsKS9K8VVHWI7ITr9O3jzWpPkcojeCvU8JGeGnRfVFE/FsxMn8OBxUuNZjAEPokQD+Iyd/YiPax/yZjWw=="

APP_DIR_NAME = "SJZ-Control"

# 指纹权重（总和 100）。缺失组件按「未命中」计（分母固定 100），
# 不再让「许可里存了几个组件」决定阈值（R10-L07）。
WEIGHTS = {
    "bios_uuid": 30,       # SMBIOS Type 1 UUID，最稳（重装系统不变）
    "machine_guid": 25,    # HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid
    "board_serial": 15,    # SMBIOS Type 2 主板序列号
    "cpu_id": 15,          # ProcessorNameString + Identifier
    "volume_serial": 10,   # 系统盘卷序列号（格式化会变）
    "mac": 5,              # 物理网卡 MAC（换网卡会变，权重最低）
}
FP_MIN_RATIO = 0.85          # 高于单组件最大权重，单项变动不再放行（R10-L08）
FP_MIN_COMPONENTS = 4        # 许可里少于 4 个组件 => 拒绝（低熵可爆破，R10-L07）
FP_ANCHOR_KEYS = ("bios_uuid", "machine_guid")   # 至少命中其中一个

DEFAULT_GRACE_H = 72        # 到期后的宽限
DEFAULT_LEASE_H = 168       # 离线租期（7 天必须联网一次，吊销才能生效）
DEFAULT_OFFLINE_GRACE_H = 72  # 离线租期后的宽限
TRIAL_HOURS = 72            # 本地试用固定 72h，**不从本地记录读**（R10-L04）
MAX_LICENSE_DAYS = 3650     # 签发 / 续期的绝对上限（10 年，R10-L11）

ROLLBACK_TOLERANCE_S = 300  # 系统时间倒退超过 5 分钟才算「回拨」
ROLLBACK_EVENTS_MAX = 2     # 同一许可累计两次回拨 → 必须联网
MAX_OFFLINE_USED_S = 10 * 365 * 86400    # 账本累计离线秒数上限（10 年）
MAX_ROLLBACK_EVENTS = 1000
MAX_EPOCH = 1e13            # 约公元 2286 年，超过视为脏数据

# 锚点：online = 见过服务端；unproven = 首次离线激活（签名合法但未联网校验）
ANCHOR_ONLINE = "online"
ANCHOR_UNPROVEN = "unproven"
ANCHOR_MIRROR_FILE = "anchor.json"
ANCHOR_REG_PATH = r"Software\SJZ-Control"
ANCHOR_REG_VALUE = "anchor"
ANCHOR_REG_SIG = "anchor_sig"

# 账本 / 试用记录的完整性密钥（与指纹一起派生 HMAC key）。
# 说明：它编译进程序，能挡住「记事本改 JSON」，挡不住反编译者重算 HMAC；
# 真正的权威判定仍在服务端（联网租期 + 吊销）。R10-L05 的目标是堵死
# 「删文件 / 改数字 / 改 hours」这三条零成本路径。
_LEDGER_SECRET_HEX = (
    "d158f48460f93c940fcd80ad15de9a98c6d70841d52c5cb830cb6e56fe01bcd6")

# 账本绑定用的稳定组件子集：网卡 / 系统盘变化不应让账本失效。
_LEDGER_STABLE = ("bios_uuid", "machine_guid", "board_serial")

_ENFORCED = None            # None = 本进程没启用门禁（源码 / 测试）


class LicenseError(Exception):
    """许可格式 / 签名 / 网络错误。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 基础工具

def _finite(value, lo, hi):
    """把任意输入收敛成 [lo, hi] 内的有限浮点；非法返回 None。

    所有数值入口先过这里：NaN / inf / 负数 / 非数字一律判非法，
    这是 R10-L05「NaN 绕过租期」与 L-12「grace_h=null 崩溃」的统一闸。
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < lo or f > hi:
        return None
    return f


def _safe_int(value, lo=0, hi=2 ** 31):
    f = _finite(value, lo, hi)
    return int(f) if f is not None else None


def _test_hooks_allowed() -> bool:
    """`SZJ_LICENSE_DIR` / `SZJ_NO_REGISTRY` 只在源码运行（非 frozen）时生效。

    打包后一律忽略 —— 否则被测用户只要知道变量名就能无限重开试用（R10-L04）。
    """
    return not getattr(sys, "frozen", False)


# ---------------------------------------------------------------- 路径

def app_dir() -> str:
    """许可数据的目录。不写死本机路径；测试用 SZJ_LICENSE_DIR 指到临时目录。"""
    override = os.environ.get("SZJ_LICENSE_DIR") if _test_hooks_allowed() else None
    if override:
        d = override
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        d = os.path.join(base, APP_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def paths() -> dict:
    d = app_dir()
    return {
        "license": os.path.join(d, "license.json"),
        "state": os.path.join(d, "state.json"),
        "revocations": os.path.join(d, "revocations.json"),
        "trial": os.path.join(d, "trial.json"),
    }


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    """原子写：先写 .tmp 再 os.replace，断电不会留下半个文件。"""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 签名
#
# ECDSA P-256 + SHA-256，签名 = IEEE P1363 的 r||s（各 32 字节），base64 传输。
# 纯 Python 实现（只依赖 hashlib/hmac）：和 ed25519_ref 一样，许可校验只在启动时
# 跑一次，几十毫秒可接受，换来「不增加二进制依赖」。.NET 侧用 BCL 的 ECDsaCng
# 校验同一份 r||s —— 两边都不做 DER，避免编码差异。

_P256_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_P256_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_P256_A = _P256_P - 3
_P256_B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_P256_G = (
    0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
    0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5,
)
SIG_ALG = "ECDSA-P256-SHA256"


def _p256_inv(x, m):
    return pow(x % m, -1, m)


def _p256_add(p1, p2):
    """仿射点加。None = 无穷远点。"""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P256_P == 0:
            return None
        lam = (3 * x1 * x1 + _P256_A) * _p256_inv(2 * y1, _P256_P) % _P256_P
    else:
        lam = (y2 - y1) * _p256_inv(x2 - x1, _P256_P) % _P256_P
    x3 = (lam * lam - x1 - x2) % _P256_P
    y3 = (lam * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_mul(k, point):
    """标量乘（双倍-加），k 取模 n。"""
    k %= _P256_N
    out = None
    addend = point
    while k:
        if k & 1:
            out = _p256_add(out, addend)
        addend = _p256_add(addend, addend)
        k >>= 1
    return out


def _p256_on_curve(point) -> bool:
    if point is None:
        return False
    x, y = point
    if not (0 <= x < _P256_P and 0 <= y < _P256_P):
        return False
    return (y * y - (x * x * x + _P256_A * x + _P256_B)) % _P256_P == 0


def public_key_b64_from_priv(priv_hex: str) -> str:
    """私钥（64 位十六进制标量）→ base64(X||Y)。签发机 / 测试用。"""
    d = _parse_priv(priv_hex)
    x, y = _p256_mul(d, _P256_G)
    return base64.b64encode(x.to_bytes(32, "big") + y.to_bytes(32, "big")).decode()


def new_private_key_hex() -> str:
    """生成一个合法 P-256 标量（64 位十六进制）。只给签发机 / 测试用。"""
    import secrets
    return "%064x" % (secrets.randbelow(_P256_N - 1) + 1)


# 签发侧私钥的位置：**不在仓库里**。优先读环境变量 `SJZ_SIGNING_KEY`，
# 否则落在用户级受控目录（%LOCALAPPDATA%\\SJZ-License-Keys\\）。
# portal 后台与 tools/license_server.py 都从这里取默认值，保证两处同一套。
SIGNING_KEY_ENV = "SJZ_SIGNING_KEY"


def default_signing_key_path() -> str:
    """签发私钥的默认路径（仓库外）。仓库里只放公钥。"""
    override = os.environ.get(SIGNING_KEY_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "SJZ-License-Keys", "prod_signing_key_p256.txt")


def _parse_priv(priv_hex) -> int:
    if not isinstance(priv_hex, str):
        raise LicenseError("bad_seed", "签名私钥必须是十六进制字符串")
    s = priv_hex.strip().lower()
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise LicenseError("bad_seed", "签名私钥必须是 64 位十六进制")
    d = int(s, 16)
    if not 1 <= d < _P256_N:
        raise LicenseError("bad_seed", "签名私钥超出 P-256 标量范围")
    return d


def _decode_pubkey(text) -> tuple:
    """base64(X||Y) → (x, y)。长度、曲线、无穷远点都查。"""
    try:
        raw = base64.b64decode(str(text).strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"公钥不是合法 base64：{e}") from e
    if len(raw) != 64:
        raise ValueError(f"公钥长度应为 64 字节（X||Y），实际 {len(raw)}")
    point = (int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    if not _p256_on_curve(point):
        raise ValueError("公钥不在 P-256 曲线上")
    return point


def _decode_sig(text) -> bytes:
    """base64(r||s) → 64 字节；格式问题统一转 ValueError。"""
    try:
        raw = base64.b64decode(str(text).strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"签名不是合法 base64：{e}") from e
    if len(raw) != 64:
        raise ValueError(f"签名长度应为 64 字节（r||s），实际 {len(raw)}")
    return raw


def _rfc6979_nonce(d, digest) -> int:
    """RFC 6979 确定性 nonce（HMAC-SHA256）。

    确定性签名让「同一 payload 两次签发字节一致」，测试和比对都更容易；
    也避免 RNG 复用 k 这种致命事故。
    """
    x = d.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + digest, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < _P256_N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def p256_sign(priv_hex, message: bytes) -> bytes:
    """ECDSA P-256 签名 → 64 字节 r||s。"""
    d = _parse_priv(priv_hex)
    digest = hashlib.sha256(message).digest()
    z = int.from_bytes(digest, "big")
    while True:
        k = _rfc6979_nonce(d, digest)
        r = _p256_mul(k, _P256_G)[0] % _P256_N
        if r == 0:
            continue
        s = (_p256_inv(k, _P256_N) * (z + r * d)) % _P256_N
        if s == 0:
            continue
        if s > _P256_N // 2:                 # low-S：签名唯一、少一条边信道入口
            s = _P256_N - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def p256_verify(pub_b64, message: bytes, sig64: bytes) -> bool:
    """ECDSA P-256 验签。任何格式/范围错误都返回 False，不抛异常。"""
    try:
        point = _decode_pubkey(pub_b64)
        if not sig64 or len(sig64) != 64:
            return False
        r = int.from_bytes(sig64[:32], "big")
        s = int.from_bytes(sig64[32:], "big")
        if not (1 <= r < _P256_N and 1 <= s < _P256_N):
            return False
        z = int.from_bytes(hashlib.sha256(message).digest(), "big")
        w = _p256_inv(s, _P256_N)
        u1 = (z * w) % _P256_N
        u2 = (r * w) % _P256_N
        pt = _p256_add(_p256_mul(u1, _P256_G), _p256_mul(u2, point))
        if pt is None:
            return False
        return pt[0] % _P256_N == r
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return False


def canonical_bytes(payload) -> bytes:
    """签名的规范化字节串。服务端必须用同一套参数，否则签名对不上。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _validate_payload(payload: dict) -> dict:
    """验签之后统一做 schema 校验：不合法 -> sig_invalid（fail closed）。

    以前 `grace_h=null` 会在 evaluate 里抛 TypeError，接到启动路径就是闪退
    （R10-L12）。现在错误一律变成可显示的判定结果。
    """
    # 吊销列表是一类独立的签名文档（只有 gen / lic_ids / fp_ids）
    if "lic_ids" in payload or "fp_ids" in payload:
        if _safe_int(payload.get("gen"), 0, 2 ** 31) is None:
            raise LicenseError("sig_invalid", "吊销列表 gen 非法")
        for key in ("lic_ids", "fp_ids"):
            value = payload.get(key) or []
            if not isinstance(value, list) or not all(
                    isinstance(x, str) for x in value):
                raise LicenseError("sig_invalid", f"吊销列表 {key} 非法")
        return payload

    lic_id = payload.get("lic_id")
    if not isinstance(lic_id, str) or not lic_id or len(lic_id) > 128:
        raise LicenseError("sig_invalid", "lic_id 非法")

    exp = payload.get("exp")
    if exp is not None and _finite(exp, 0.0, MAX_EPOCH) is None:
        raise LicenseError("sig_invalid", "exp 非法")

    for key, default, hi in (("grace_h", DEFAULT_GRACE_H, 8760.0),
                             ("lease_h", DEFAULT_LEASE_H, 8760.0),
                             ("offline_grace_h", DEFAULT_OFFLINE_GRACE_H, 8760.0)):
        if _finite(payload.get(key, default), 0.0, hi) is None:
            raise LicenseError("sig_invalid", f"{key} 非法")

    vec = payload.get("fp_vec") or {}
    if not isinstance(vec, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in vec.items()):
        raise LicenseError("sig_invalid", "fp_vec 非法")
    if "fp" in payload and payload["fp"] is not None \
            and not isinstance(payload["fp"], str):
        raise LicenseError("sig_invalid", "fp 非法")
    weights = payload.get("fp_weights") or {}
    if not isinstance(weights, dict) or not all(
            isinstance(k, str) and _finite(v, 0.0, 100.0) is not None
            for k, v in weights.items()):
        raise LicenseError("sig_invalid", "fp_weights 非法")
    if not isinstance(payload.get("trial", False), bool):
        raise LicenseError("sig_invalid", "trial 非法")
    return payload


def verify_blob(blob) -> dict:
    """校验 `{"payload": {...}, "sig": "..."}`，返回 payload；失败抛 LicenseError。

    公钥固定为模块常量 `PUBLIC_KEY_B64`，**没有覆盖参数**（R10-L09）。
    曲线固定 P-256；签名只认 P1363（r||s）。别的曲线/编码一律判 sig_invalid，
    不做算法回退 —— 那等于给自己开一条降级攻击面。
    """
    if not isinstance(blob, dict):
        raise LicenseError("sig_invalid", "许可不是一个 JSON 对象")
    payload = blob.get("payload")
    sig = blob.get("sig")
    if not isinstance(payload, dict) or not isinstance(sig, str):
        raise LicenseError("sig_invalid", "许可缺少 payload/sig")
    if payload.get("v") != VERSION:
        raise LicenseError("sig_invalid", f"许可版本不支持：{payload.get('v')!r}")
    try:
        raw_sig = _decode_sig(sig)
        _decode_pubkey(PUBLIC_KEY_B64)        # 公钥坏了也归到 sig_invalid
    except ValueError as e:
        raise LicenseError("sig_invalid", f"公钥/签名格式错误：{e}") from e
    if not p256_verify(PUBLIC_KEY_B64, canonical_bytes(payload), raw_sig):
        raise LicenseError("sig_invalid", "签名校验失败（许可被改过或公钥不匹配）")
    return _validate_payload(payload)


def sign_blob(payload: dict, seed_hex: str) -> dict:
    """供签发工具使用（客户端不用）。seed_hex = 私钥标量（32 字节十六进制）。

    `seed_hex` 显式做类型与格式断言 —— R10-L02 的根因就是「把 hex 当文件
    路径传」，这里不让非十六进制串进来。
    """
    raw = p256_sign(seed_hex, canonical_bytes(payload))
    return {"payload": payload, "sig": base64.b64encode(raw).decode("ascii")}


# ---------------------------------------------------------------- 指纹

def _norm(value) -> str:
    return " ".join(str(value).strip().lower().split())


def _smbios_table() -> bytes:
    """GetSystemFirmwareTable('RSMB') → 原始 SMBIOS 表。读不到返回空。"""
    if not sys.platform.startswith("win"):
        return b""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.GetSystemFirmwareTable.argtypes = [
            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        k32.GetSystemFirmwareTable.restype = wintypes.DWORD
        sig = 0x52534D42                     # 'RSMB'
        size = k32.GetSystemFirmwareTable(sig, 0, None, 0)
        if size < 64:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = k32.GetSystemFirmwareTable(sig, 0, buf, size)
        if got < 64:
            return b""
        raw = buf.raw[:got]
        length = int.from_bytes(raw[4:8], "little")
        return raw[8:8 + length]
    except Exception:                        # noqa: BLE001
        return b""


def _smbios_structures(table: bytes):
    """逐条产出 (type, 格式化段 bytes, 字符串列表)。"""
    off = 0
    n = len(table)
    while off + 4 <= n:
        stype = table[off]
        slen = table[off + 1]
        if slen < 4 or off + slen > n:
            break
        fmt = table[off:off + slen]
        p = off + slen
        strings = []
        if p < n and table[p:p + 1] == b"\x00":
            nxt = p + 2
        else:
            while p < n:
                end = table.find(b"\x00", p)
                if end < 0:
                    end = n
                strings.append(table[p:end].decode("latin-1", "ignore"))
                p = end + 1
                if p < n and table[p:p + 1] == b"\x00":
                    p += 1
                    break
            nxt = p
        yield stype, fmt, strings
        if nxt <= off:
            break
        off = nxt


def _smbios_ids() -> dict:
    """Type 1 UUID + Type 2 主板序列号。全 0/全 F 视为未写入，返回空。"""
    out = {}
    for stype, fmt, strings in _smbios_structures(_smbios_table()):
        if stype == 1 and len(fmt) >= 0x18:
            raw = fmt[0x08:0x18]
            if any(b not in (0x00, 0xFF) for b in raw):
                out["bios_uuid"] = raw.hex()
        elif stype == 2 and len(fmt) >= 0x08:
            idx = fmt[0x07]
            if 0 < idx <= len(strings):
                s = strings[idx - 1].strip()
                if s and s.lower() not in ("to be filled by o.e.m.",
                                           "default string", "none", "empty"):
                    out["board_serial"] = s
    return out


def _reg_value(root, path, name):
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg
        with winreg.OpenKey(root, path) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    except Exception:                        # noqa: BLE001
        return None


def _machine_guid():
    import winreg
    return _reg_value(winreg.HKEY_LOCAL_MACHINE,
                      r"SOFTWARE\Microsoft\Cryptography", "MachineGuid")


def _cpu_id():
    import winreg
    base = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    name = _reg_value(winreg.HKEY_LOCAL_MACHINE, base, "ProcessorNameString")
    ident = _reg_value(winreg.HKEY_LOCAL_MACHINE, base, "Identifier")
    if name or ident:
        return f"{name or ''}|{ident or ''}"
    return None


def _volume_serial():
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        root = (os.environ.get("SystemDrive") or "C:") + "\\"
        serial = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial),
            None, None, None, 0)
        if not ok:
            return None
        return f"{serial.value:08x}"
    except Exception:                        # noqa: BLE001
        return None


def _mac():
    """uuid.getnode() 的 MAC；多播位（随机/无网卡）视为不可用。"""
    try:
        import uuid as _uuid
        node = _uuid.getnode()
        if (node >> 40) & 1:
            return None
        return f"{node:012x}"
    except Exception:                        # noqa: BLE001
        return None


def collect_components() -> dict:
    """读硬件特征。任何一项读不到就不放进返回表。"""
    out = {}
    try:
        out.update(_smbios_ids())
    except Exception:                        # noqa: BLE001
        pass
    for key, fn in (("machine_guid", _machine_guid),
                    ("cpu_id", _cpu_id),
                    ("volume_serial", _volume_serial),
                    ("mac", _mac)):
        try:
            v = fn()
        except Exception:                    # noqa: BLE001
            v = None
        if v:
            out[key] = v
    return out


def fingerprint(components=None) -> dict:
    """组件 → {hash, vec, weights}。vec 存的是每个组件的截断 sha256。"""
    comps = collect_components() if components is None else components
    vec = {}
    for key, weight in WEIGHTS.items():
        value = comps.get(key)
        if not value:
            continue
        vec[key] = hashlib.sha256(
            f"sjz-fp:{key}:{_norm(value)}".encode("utf-8")).hexdigest()[:32]
    order = sorted(vec)
    digest = hashlib.sha256(
        "|".join(f"{k}={vec[k]}" for k in order).encode("utf-8")).hexdigest()
    return {"hash": digest, "vec": vec,
            "weights": {k: WEIGHTS[k] for k in order},
            "components_present": order}


def match_score(stored_vec, current_vec, weights=None) -> tuple:
    """返回 (匹配比例, 命中的组件列表)。

    分母**固定**为 `sum(WEIGHTS.values()) = 100`：许可里没存的组件按未命中计。
    以前分母只累加 stored_vec 里有的组件，于是「只存 MAC」的许可 100% 通过
    （R10-L07）。`weights` 只允许在本模块常量范围内覆盖，不接受放大。
    """
    w = dict(WEIGHTS)
    for k, v in (weights or {}).items():
        if k in WEIGHTS:
            f = _finite(v, 0.0, float(WEIGHTS[k]))
            if f is not None:
                w[k] = f
    denom = float(sum(w.values()))
    matched = 0.0
    hits = []
    for key, digest in (stored_vec or {}).items():
        if key not in w or not isinstance(digest, str):
            continue
        if (current_vec or {}).get(key) == digest:
            matched += float(w[key])
            hits.append(key)
    if denom <= 0:
        return 0.0, []
    return matched / denom, hits


def _fp_passes(stored_vec, hits, ratio) -> bool:
    """多组件联合判定：数量下限 + 必须命中锚点 + 比例下限。"""
    if len(stored_vec or {}) < FP_MIN_COMPONENTS:
        return False
    if not any(k in hits for k in FP_ANCHOR_KEYS):
        return False
    return ratio >= FP_MIN_RATIO


# ---------------------------------------------------------------- 账本

def _state_skeleton() -> dict:
    return {"last_check_wall": None, "last_check_mono": None,
            "offline_used_s": 0.0, "last_server_epoch": None,
            "rollback_events": 0, "revoke_gen": 0, "rev_seen": False,
            "lic_id": None, "anchor_kind": None, "anchor_epoch": None,
            "anchor_born_wall": None, "anchor_born_mono": None,
            "anchor_seen_wall": None}


def _sanitize_state(state) -> dict:
    """把读进来的账本字段逐个收敛到合法范围，非法值直接丢弃。"""
    st = dict(_state_skeleton())
    if not isinstance(state, dict):
        return st
    for key in ("last_check_wall", "last_check_mono", "last_server_epoch",
                "anchor_epoch", "anchor_born_wall", "anchor_born_mono",
                "anchor_seen_wall"):
        f = _finite(state.get(key), 0.0, MAX_EPOCH)
        st[key] = f
    f = _finite(state.get("offline_used_s"), 0.0, MAX_OFFLINE_USED_S)
    st["offline_used_s"] = f if f is not None else 0.0
    i = _safe_int(state.get("rollback_events"), 0, MAX_ROLLBACK_EVENTS)
    st["rollback_events"] = i if i is not None else 0
    i = _safe_int(state.get("revoke_gen"), 0, 2 ** 31)
    st["revoke_gen"] = i if i is not None else 0
    st["rev_seen"] = bool(state.get("rev_seen"))
    lid = state.get("lic_id")
    st["lic_id"] = lid if isinstance(lid, str) else None
    kind = state.get("anchor_kind")
    st["anchor_kind"] = kind if kind in (ANCHOR_ONLINE, ANCHOR_UNPROVEN) else None
    return st


def _ledger_key(payload) -> bytes | None:
    """账本 HMAC key：嵌入密钥 + 许可里的稳定组件子集。"""
    if not isinstance(payload, dict):
        return None
    vec = payload.get("fp_vec") or {}
    if not isinstance(vec, dict):
        return None
    stable = "|".join(f"{k}={vec[k]}" for k in sorted(vec)
                      if k in _LEDGER_STABLE and isinstance(vec.get(k), str))
    if not stable:
        fp = payload.get("fp")
        stable = f"fp={fp}" if isinstance(fp, str) and fp else ""
    if not stable:
        return None
    return hmac.new(bytes.fromhex(_LEDGER_SECRET_HEX), stable.encode("utf-8"),
                    hashlib.sha256).digest()


def _sign_state(payload, key) -> str:
    return hmac.new(key, canonical_bytes(payload), hashlib.sha256).hexdigest()


def _write_signed(path, payload, key):
    """签名记录落盘：建目录 + 原子写；写不进去就算了（下次还能重写）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_json(path, {"payload": payload, "sig": _sign_state(payload, key)})
    except OSError:
        pass


def _load_state(key) -> tuple:
    """读账本 → (payload, trusted)。明文 / 缺字段 / HMAC 不对一律不可信。"""
    raw = _read_json(paths()["state"])
    if not isinstance(raw, dict) or not key:
        return {}, False
    payload, sig = raw.get("payload"), raw.get("sig")
    if not isinstance(payload, dict) or not isinstance(sig, str):
        return {}, False                            # 旧版明文账本
    if not hmac.compare_digest(sig, _sign_state(payload, key)):
        return {}, False                            # 被改过
    return _sanitize_state(payload), True


def _write_state(payload, key):
    if not key:
        return
    _write_json(paths()["state"],
                {"payload": _sanitize_state(payload),
                 "sig": _sign_state(payload, key)})


# ---------------------------------------------------------------- 锚点副本
#
# 账本（state.json）可以被删、被改、被回滚成旧副本。只靠它的话，
# 「删掉账本 + 把墙钟往回拨」就能重新拿满租期。这里把「锚点出生时间 +
# 累计离线秒数 + 见过的最新时间」另存到 ProgramData 与 HKCU 两个副本，
# 判定时取最早出生 / 最大累计 —— 删单个文件不能让租期重新起算。

def _anchor_mirror_paths() -> list:
    """副本落盘位置。测试可用 SZJ_ANCHOR_DIR 指到临时目录。"""
    override = (os.environ.get("SZJ_ANCHOR_DIR")
                if _test_hooks_allowed() else None)
    if override:
        return [os.path.join(override, ANCHOR_MIRROR_FILE)]
    if _test_hooks_allowed() and os.environ.get("SZJ_LICENSE_DIR"):
        return []          # 隔离测试：不碰真实 ProgramData
    pd = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return [os.path.join(pd, APP_DIR_NAME, ANCHOR_MIRROR_FILE)]


def _anchor_reg_allowed() -> bool:
    if not sys.platform.startswith("win"):
        return False
    if _test_hooks_allowed():
        if os.environ.get("SZJ_NO_REGISTRY"):
            return False
        if os.environ.get("SZJ_LICENSE_DIR") and not os.environ.get(
                "SZJ_ANCHOR_DIR"):
            return False       # 隔离测试：不写真实 HKCU
    return True


def _anchor_record(st) -> dict:
    """账本里的锚点字段 → 可签名的小记录。"""
    st = _sanitize_state(st)
    return {"v": 1, "lic_id": st.get("lic_id"),
            "kind": st.get("anchor_kind"),
            "born_wall": st.get("anchor_born_wall"),
            "born_mono": st.get("anchor_born_mono"),
            "used_s": st.get("offline_used_s"),
            "seen_wall": st.get("anchor_seen_wall") or st.get("last_check_wall"),
            "revoke_gen": st.get("revoke_gen"),
            "rev_seen": bool(st.get("rev_seen"))}


def _load_anchor_store(path, key):
    raw = _read_json(path)
    if not isinstance(raw, dict) or not key:
        return None
    payload, sig = raw.get("payload"), raw.get("sig")
    if not isinstance(payload, dict) or not isinstance(sig, str):
        return None
    if not hmac.compare_digest(sig, _sign_state(payload, key)):
        return None
    if _finite(payload.get("born_wall"), 0.0, MAX_EPOCH) is None:
        return None
    return payload


def _anchor_reg_read(key):
    if not _anchor_reg_allowed() or not key:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ANCHOR_REG_PATH) as k:
            text = winreg.QueryValueEx(k, ANCHOR_REG_VALUE)[0]
            sig = winreg.QueryValueEx(k, ANCHOR_REG_SIG)[0]
        rec = json.loads(text)
        if not isinstance(rec, dict):
            return None
        if not hmac.compare_digest(str(sig), _sign_state(rec, key)):
            return None
        return rec
    except Exception:                        # noqa: BLE001
        return None


def _anchor_reg_write(record, key):
    if not _anchor_reg_allowed() or not key:
        return
    try:
        import winreg
        text = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ANCHOR_REG_PATH) as k:
            winreg.SetValueEx(k, ANCHOR_REG_VALUE, 0, winreg.REG_SZ, text)
            winreg.SetValueEx(k, ANCHOR_REG_SIG, 0, winreg.REG_SZ,
                              _sign_state(record, key))
    except Exception:                        # noqa: BLE001
        pass


def _merge_anchor(records) -> dict:
    """多副本合并：取最早出生、最大累计、最晚见过时间。"""
    out = {}
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        born = _finite(rec.get("born_wall"), 0.0, MAX_EPOCH)
        if born is not None:
            out["born_wall"] = min(out.get("born_wall", born), born)
        used = _finite(rec.get("used_s"), 0.0, MAX_OFFLINE_USED_S)
        if used is not None:
            out["used_s"] = max(out.get("used_s", used), used)
        seen = _finite(rec.get("seen_wall"), 0.0, MAX_EPOCH)
        if seen is not None:
            out["seen_wall"] = max(out.get("seen_wall", seen), seen)
        gen = _safe_int(rec.get("revoke_gen"), 0, 2 ** 31) or 0
        out["revoke_gen"] = max(out.get("revoke_gen", 0), gen)
        out["rev_seen"] = bool(out.get("rev_seen")) or bool(rec.get("rev_seen"))
    return out


def _load_anchor_mirrors(key) -> dict:
    recs = []
    for path in _anchor_mirror_paths():
        rec = _load_anchor_store(path, key)
        if rec is not None:
            recs.append(rec)
    rec = _anchor_reg_read(key)
    if rec is not None:
        recs.append(rec)
    return _merge_anchor(recs)


def _write_anchor_mirrors(state, key):
    if not key:
        return
    rec = _anchor_record(state)
    if _finite(rec.get("born_wall"), 0.0, MAX_EPOCH) is None:
        return                                  # 还没建立锚点，不写空副本
    for path in _anchor_mirror_paths():
        _write_signed(path, rec, key)
    _anchor_reg_write(rec, key)


def _advance_offline(state, now, mono):
    """把账本推进到「现在」，返回新的账本副本（已 sanitize）。

    同一台开机内：用 time.monotonic() 的差值 —— 改系统时间不影响它。
    重启后（monotonic 归零）：退回用墙钟差值；墙钟倒退超过 5 分钟记一次回拨。
    """
    st = _sanitize_state(state)
    last_mono = st["last_check_mono"]
    last_wall = st["last_check_wall"]
    used = st["offline_used_s"]
    rollbacks = st["rollback_events"]
    note = ""

    if last_mono is None or last_wall is None:
        delta = 0.0
    elif mono >= last_mono:
        delta = max(0.0, mono - last_mono)
    else:
        delta = now - last_wall
        if delta < -ROLLBACK_TOLERANCE_S:
            note = "检测到系统时间回拨（重启后墙钟比上次早）"
            rollbacks += 1
            delta = 0.0
        else:
            delta = max(0.0, delta)
    st["offline_used_s"] = min(MAX_OFFLINE_USED_S, used + delta)
    st["rollback_events"] = min(MAX_ROLLBACK_EVENTS, rollbacks)
    st["last_check_wall"] = now
    st["last_check_mono"] = mono
    return st, note


def mark_online(state, server_epoch=None, now=None, mono=None) -> dict:
    """联网成功：清零离线账本，锚定服务端时间（锚点升级为 online）。"""
    st = _sanitize_state(state)
    st["offline_used_s"] = 0.0
    st["rollback_events"] = 0
    ep = _finite(server_epoch, 0.0, MAX_EPOCH)
    wall = float(now) if now is not None else time.time()
    if ep is None:
        ep = wall
    st["last_server_epoch"] = ep
    st["last_check_wall"] = wall
    st["last_check_mono"] = float(mono) if mono is not None else time.monotonic()
    st["anchor_kind"] = ANCHOR_ONLINE
    st["anchor_epoch"] = ep
    # 出生时间只保留最早的一份（unproven 期间已经起算的天数不能被联网洗掉）
    if _finite(st.get("anchor_born_wall"), 0.0, MAX_EPOCH) is None:
        st["anchor_born_wall"] = ep
    seen = _finite(st.get("anchor_seen_wall"), 0.0, MAX_EPOCH) or 0.0
    st["anchor_seen_wall"] = max(wall, seen)
    return st


# ---------------------------------------------------------------- 判定

class Decision:
    """一次许可判定的结果。`as_dict()` 可直接给 HTTP 接口。"""

    def __init__(self, status, ok, reason="", **fields):
        self.status = status
        self.ok = bool(ok)
        self.reason = reason
        self.lic_id = fields.get("lic_id")
        self.tier = fields.get("tier")
        self.trial = bool(fields.get("trial"))
        self.exp = fields.get("exp")
        self.remaining_s = fields.get("remaining_s")
        self.offline_used_s = fields.get("offline_used_s")
        self.lease_remaining_s = fields.get("lease_remaining_s")
        self.fp_score = fields.get("fp_score")
        self.fp_hits = fields.get("fp_hits") or []
        self.rollback_events = fields.get("rollback_events") or 0
        self.state_patch = fields.get("state_patch") or {}
        self.notes = fields.get("notes") or ""
        self.anchor = fields.get("anchor")

    def as_dict(self) -> dict:
        return {"status": self.status, "ok": self.ok, "reason": self.reason,
                "lic_id": self.lic_id, "tier": self.tier, "trial": self.trial,
                "exp": self.exp, "remaining_s": self.remaining_s,
                "offline_used_s": self.offline_used_s,
                "lease_remaining_s": self.lease_remaining_s,
                "fp_score": self.fp_score, "fp_hits": self.fp_hits,
                "rollback_events": self.rollback_events,
                "anchor": self.anchor, "notes": self.notes}

    def __repr__(self):
        return (f"<Decision {self.status} ok={self.ok} "
                f"lic={self.lic_id} remaining={self.remaining_s}>")


def _revoked_in(payload, revocations, state=None) -> tuple:
    """(是否吊销, 原因, 列表是否可信, 列表代数)。

    吊销列表**必须验签**（R10-L06）；验签失败按「不可信」处理，不静默使用。
    列表代数比账本里记的小 = 旧列表（可能被回滚洗白），直接忽略。
    """
    if not revocations:
        return False, "", False, None
    try:
        rp = verify_blob(revocations)
    except LicenseError as e:
        return False, f"吊销列表验签失败：{e}", False, None
    gen = _safe_int(rp.get("gen"), 0, 2 ** 31)
    if gen is None:
        return False, "吊销列表 gen 非法", False, None
    local_gen = _safe_int((state or {}).get("revoke_gen"), 0, 2 ** 31) or 0
    if gen < local_gen:
        return False, "", True, gen
    lic_ids = rp.get("lic_ids") or []
    fp_ids = rp.get("fp_ids") or []
    lic_id = payload.get("lic_id")
    if lic_id and lic_id in set(lic_ids):
        return True, f"许可 {lic_id} 已被吊销", True, gen
    if payload.get("trial") and payload.get("fp") in set(fp_ids):
        return True, "该机器已被禁止试用", True, gen
    return False, "", True, gen


def evaluate(blob, *, revocations=None, state=None, state_trusted=True,
             now=None, mono=None, components=None,
             anchor_mirror=None) -> Decision:
    """纯判定：不改文件、不联网。测试都打这里。

    state 由调用方保证**已经通过完整性校验**（`check()` 负责）；
    `state_trusted=False` 表示账本不可信（被删/被改），锚点副本
    (`anchor_mirror`) 仍可作为出生时间与累计秒数的依据。
    """
    now = time.time() if now is None else float(now)
    mono = time.monotonic() if mono is None else float(mono)
    state = dict(state or {}) if isinstance(state, dict) else {}
    if not blob:
        return Decision("none", False, "没有许可文件", state_patch=state)

    try:
        payload = verify_blob(blob)
    except LicenseError as e:
        return Decision("sig_invalid", False, str(e), state_patch=state)

    lic_id = payload.get("lic_id") or ""
    common = {"lic_id": lic_id, "tier": payload.get("tier"),
              "trial": bool(payload.get("trial")), "exp": payload.get("exp")}

    mirror = _merge_anchor([anchor_mirror] if anchor_mirror else {})
    rev_state = dict(state if state_trusted else {})
    mirror_gen = _safe_int(mirror.get("revoke_gen"), 0, 2 ** 31)
    if mirror_gen is not None:
        rev_state["revoke_gen"] = max(int(rev_state.get("revoke_gen") or 0),
                                      mirror_gen)
    revoked, why, rev_trusted, rev_gen = _revoked_in(
        payload, revocations, rev_state)
    if revoked:
        return Decision("revoked", False, why, **common, state_patch=state)

    fp = fingerprint(components)
    stored = payload.get("fp_vec") or {}
    ratio, hits = match_score(stored, fp["vec"], payload.get("fp_weights"))
    if not _fp_passes(stored, hits, ratio):
        extra = ""
        if len(stored) < FP_MIN_COMPONENTS:
            extra = "（许可只绑定了 %d 个组件，低于下限 %d）" % (
                len(stored), FP_MIN_COMPONENTS)
        return Decision("fp_mismatch", False,
                        f"硬件指纹匹配 {ratio:.0%}（阈值 {FP_MIN_RATIO:.0%}）{extra}",
                        fp_score=ratio, fp_hits=hits, **common, state_patch=state)

    prev_id = state.get("lic_id") if state_trusted else None
    st, note = _advance_offline(state if state_trusted else {}, now, mono)
    st["lic_id"] = lic_id
    used = float(st["offline_used_s"])

    # ---- 锚点来源：online（见过服务端）/ unproven（首次离线激活）
    kind = st.get("anchor_kind")
    if kind not in (ANCHOR_ONLINE, ANCHOR_UNPROVEN):
        kind = (ANCHOR_ONLINE if _finite(st.get("last_server_epoch"), 0.0,
                                         MAX_EPOCH) is not None else None)
    server_epoch = _finite(st.get("last_server_epoch"), 0.0, MAX_EPOCH)
    if kind == ANCHOR_ONLINE and server_epoch is None:
        kind = None

    mirror_born = _finite(mirror.get("born_wall"), 0.0, MAX_EPOCH)
    mirror_used = _finite(mirror.get("used_s"), 0.0, MAX_OFFLINE_USED_S) or 0.0
    mirror_seen = _finite(mirror.get("seen_wall"), 0.0, MAX_EPOCH)

    if kind is None:
        # 签名合法 + 指纹通过：允许建立未联网锚点（首次离线激活不再拒绝）
        born = mirror_born if mirror_born is not None else float(now)
        kind = ANCHOR_UNPROVEN
        st["anchor_kind"] = kind
        st["anchor_epoch"] = born
        st["anchor_born_wall"] = born
        st["anchor_born_mono"] = _finite(st.get("last_check_mono"), 0.0,
                                         MAX_EPOCH)
        note = (note + "；" if note else "") + \
            "本机首次离线激活：已建立未联网锚点（unproven）"

    if kind == ANCHOR_ONLINE:
        base = float(server_epoch)
        st["anchor_kind"] = ANCHOR_ONLINE
        st["anchor_epoch"] = base
        if _finite(st.get("anchor_born_wall"), 0.0, MAX_EPOCH) is None:
            st["anchor_born_wall"] = base
        check_now = max(now, base + used)
        span = used
    else:
        base = _finite(st.get("anchor_epoch"), 0.0, MAX_EPOCH)
        if base is None:
            base = mirror_born if mirror_born is not None else float(now)
            st["anchor_epoch"] = base
        # 删账本不能把累计秒数清零：取账本与副本的最大值
        used = max(used, mirror_used)
        st["offline_used_s"] = used
        seen = max(float(now),
                   _finite(st.get("anchor_seen_wall"), 0.0, MAX_EPOCH) or 0.0,
                   mirror_seen or 0.0)
        st["anchor_seen_wall"] = seen
        check_now = max(now, base + used, seen)
        span = max(used, check_now - base)
        # 累计秒数跟着「实际经过时间」走，删账本后副本里记的也是这个值
        used = span
        st["offline_used_s"] = span

    extra = dict(common, fp_score=ratio, fp_hits=hits,
                 offline_used_s=round(used, 1),
                 rollback_events=st["rollback_events"], notes=note,
                 state_patch=st, anchor=kind)

    if prev_id and prev_id != lic_id:
        return Decision("need_online", False,
                        "时间账本与当前许可不匹配，请联网校验",
                        lease_remaining_s=0.0, **extra)
    rev_seen = bool(st.get("rev_seen") or mirror.get("rev_seen"))
    st["rev_seen"] = rev_seen
    st["revoke_gen"] = max(int(st.get("revoke_gen") or 0),
                           int(mirror.get("revoke_gen") or 0))
    if not rev_trusted and rev_seen:
        return Decision("need_online", False,
                        "吊销列表缺失或验签失败（本地曾成功同步过），请联网重新拉取",
                        lease_remaining_s=0.0, **extra)
    if not rev_trusted and why:
        extra["notes"] = (extra["notes"] + "；" + why).strip("；")
    if rev_trusted and rev_gen is not None:
        # 列表代数记进签名账本：以后本地写死旧 gen 反而会被判成回滚
        st["revoke_gen"] = max(int(st.get("revoke_gen") or 0), rev_gen)
        st["rev_seen"] = True

    # 到期判定同时看墙钟和「锚点 + 累计秒数」的最大值：
    # 只把系统时间往回拨，不能让已到期的许可复活。
    grace_s = float(payload.get("grace_h", DEFAULT_GRACE_H)) * 3600.0
    lease_s = float(payload.get("lease_h", DEFAULT_LEASE_H)) * 3600.0
    offline_grace_s = float(
        payload.get("offline_grace_h", DEFAULT_OFFLINE_GRACE_H)) * 3600.0

    exp = payload.get("exp")
    if exp is not None:
        exp = float(exp)
        if check_now > exp + grace_s:
            return Decision("expired", False,
                            f"许可已过期 {(check_now - exp - grace_s) / 86400:.1f} 天",
                            remaining_s=0.0, **extra)
        if check_now > exp:
            return Decision("ok_grace", True,
                            "已过到期日，宽限期内（请尽快续费）",
                            remaining_s=round(exp + grace_s - check_now, 1),
                            **extra)
        extra["remaining_s"] = round(exp - check_now, 1)

    if kind == ANCHOR_ONLINE and (payload.get("perpetual")
                                  or payload.get("offline_ok")):
        extra["lease_remaining_s"] = None
        return Decision("ok", True, "永久许可", **extra)
    # 未联网锚点没有「永久」：本地签出来的许可也必须按时联网，否则可无限续期

    if span > lease_s + offline_grace_s:
        if kind == ANCHOR_UNPROVEN:
            reason = ("未联网锚点已累计 %.1f 天，超过租期 + 缓冲（%.0f + %.0f 天），"
                      "必须联网校验才能续期" % (
                          span / 86400, lease_s / 86400,
                          offline_grace_s / 86400))
        else:
            reason = ("已离线 %.1f 天，超过租期 + 宽限，需要联网校验"
                      % (used / 86400))
        return Decision("need_online", False, reason,
                        lease_remaining_s=0.0, **extra)
    if span > lease_s:
        if kind == ANCHOR_UNPROVEN:
            reason = ("未联网锚点已累计 %.1f 天，超过租期（%.0f 天），"
                      "缓冲期内请联网校验" % (span / 86400, lease_s / 86400))
        else:
            reason = "离线超过租期，宽限期内（请联网校验）"
        return Decision("ok_grace", True, reason,
                        lease_remaining_s=round(
                            lease_s + offline_grace_s - span, 1),
                        **extra)
    if st["rollback_events"] >= ROLLBACK_EVENTS_MAX:
        return Decision("clock_tamper", False,
                        "多次检测到系统时间回拨，需要联网校验", **extra)
    extra["lease_remaining_s"] = round(lease_s - span, 1)
    if kind == ANCHOR_UNPROVEN:
        return Decision("ok", True,
                        "未联网锚点有效（租期内，到期必须联网校验）", **extra)
    return Decision("ok", True, "许可有效", **extra)


# ---------------------------------------------------------------- 本地入口

def _peek_payload():
    """读许可文件并验签；无效返回 None（不抛异常）。"""
    blob = _read_json(paths()["license"])
    if not isinstance(blob, dict):
        return None
    try:
        return verify_blob(blob)
    except LicenseError:
        return None


def check(*, components=None, now=None, mono=None, save_state=True) -> Decision:
    """读本地文件 → 判定 → 落时间账本（含机器级锚点副本）。"""
    blob = _read_json(paths()["license"])
    rev = _read_json(paths()["revocations"])
    payload = _peek_payload()
    key = _ledger_key(payload) if payload else None
    state, trusted = _load_state(key) if key else ({}, False)
    mirror = _load_anchor_mirrors(key) if key else {}
    d = evaluate(blob, revocations=rev, state=state, state_trusted=trusted,
                 now=now, mono=mono, components=components,
                 anchor_mirror=mirror)
    patch = d.state_patch or {}
    if save_state and patch and key:
        try:
            _write_state(patch, key)
        except OSError:
            pass
        _write_anchor_mirrors(patch, key)
    return d


def summary() -> dict:
    """--doctor / 界面用的紧凑状态。"""
    try:
        d = check(save_state=False)
    except Exception as e:                    # noqa: BLE001
        return {"status": "error", "ok": False,
                "reason": f"{type(e).__name__}: {e}"}
    out = d.as_dict()
    out["path"] = paths()["license"]
    out["present"] = os.path.isfile(paths()["license"])
    out["enforced"] = _ENFORCED
    return out


# ---------------------------------------------------------------- 联网

def _post_json(url, body, timeout=6.0) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "SJZ-Control/license"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:                     # noqa: BLE001
            pass
        raise LicenseError("http", f"服务端返回 {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise LicenseError("network", f"连不上许可服务器：{e}") from e
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise LicenseError("http", f"服务端返回不是 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise LicenseError("http", "服务端返回不是 JSON 对象")
    return obj


def activate(server: str, key: str, *, components=None, timeout=6.0) -> Decision:
    """卡密兑换：POST /api/license/activate → 保存许可 → 返回判定。"""
    fp = fingerprint(components)
    resp = _post_json(server.rstrip("/") + "/api/license/activate",
                      {"v": VERSION, "key": key.strip(), "app": APP_DIR_NAME,
                       "fp": fp["hash"], "fp_vec": fp["vec"]}, timeout=timeout)
    save_license(resp.get("license"), revocations=resp.get("revocations"),
                 server_now=resp.get("server_now"))
    return check(components=components)


def refresh(server: str, *, components=None, timeout=6.0) -> Decision:
    """联网校验 / 拉吊销列表：POST /api/license/refresh。"""
    blob = _read_json(paths()["license"])
    if not blob:
        raise LicenseError("none", "没有可校验的许可文件")
    fp = fingerprint(components)
    resp = _post_json(server.rstrip("/") + "/api/license/refresh",
                      {"v": VERSION, "fp": fp["hash"], "fp_vec": fp["vec"],
                       "license": blob}, timeout=timeout)
    save_license(resp.get("license"), revocations=resp.get("revocations"),
                 server_now=resp.get("server_now"))
    return check(components=components)


def pull_revocations(server: str, *, components=None, timeout=4.0) -> Decision:
    """拉吊销列表（客户端真实调用方，R10-L06）。

    成功即证明连上了服务端 → 顺带把离线账本清零并锚定 server_now。
    """
    fp = fingerprint(components)
    resp = _post_json(server.rstrip("/") + "/api/license/revocations",
                      {"v": VERSION, "app": APP_DIR_NAME,
                       "fp": fp["hash"], "fp_vec": fp["vec"]}, timeout=timeout)
    payload = _peek_payload()
    key = _ledger_key(payload) if payload else None
    rev = resp.get("revocations")
    rp = None
    if rev:
        try:
            rp = verify_blob(rev)
        except LicenseError as e:
            raise LicenseError("sig_invalid", f"吊销列表验签失败：{e}") from e
        _write_json(paths()["revocations"], rev)
    if key:
        st, _trusted = _load_state(key)
        st = dict(st)
        if rp is not None:
            gen = _safe_int(rp.get("gen"), 0, 2 ** 31)
            if gen is not None:
                st["revoke_gen"] = max(int(st.get("revoke_gen") or 0), gen)
            st["rev_seen"] = True
        st = mark_online(st, server_epoch=resp.get("server_now"))
        st["lic_id"] = (payload or {}).get("lic_id")
        _write_state(st, key)
        _write_anchor_mirrors(st, key)
    return check(components=components)


def save_license(blob, *, revocations=None, server_now=None,
                 now=None, mono=None) -> dict:
    """验签后落盘；同时重置离线账本（这是「见过服务端」的唯一入口）。"""
    payload = verify_blob(blob)
    key = _ledger_key(payload)
    p = paths()
    _write_json(p["license"], blob)
    st, _trusted = _load_state(key) if key else ({}, False)
    st = dict(st)
    if revocations:
        try:
            rp = verify_blob(revocations)
        except LicenseError:
            rp = None                       # 坏的吊销列表不覆盖本地旧列表
        if rp is not None:
            _write_json(p["revocations"], revocations)
            gen = _safe_int(rp.get("gen"), 0, 2 ** 31)
            if gen is not None:
                st["revoke_gen"] = max(int(st.get("revoke_gen") or 0), gen)
            st["rev_seen"] = True
    st = mark_online(st, server_epoch=server_now, now=now, mono=mono)
    st["lic_id"] = payload.get("lic_id")
    _write_state(st, key)
    _write_anchor_mirrors(st, key)
    return payload


# ---------------------------------------------------------------- 门禁

def enforcement_state():
    """None = 本进程未启用门禁；True/False = 门禁结果。"""
    return _ENFORCED


def server_gate_ok() -> bool:
    """服务端写操作是否放行。未启用门禁（源码 / 测试）时不拦。"""
    return _ENFORCED is None or bool(_ENFORCED)


def ensure(*, server=None, components=None, allow_trial=True,
           sync=None) -> Decision:
    """启动门禁：没有有效许可就不得进入主功能。

    顺序：本地判定 → 无许可则开/续本地试用 → 有服务器则拉吊销。
    返回值一定带 `lic_id`（试用也有），`ok=True` 才能继续启动。
    """
    global _ENFORCED
    d = check(components=components)
    if not d.ok and d.status == "none" and allow_trial:
        rec = _read_trial(components)
        if rec is None:
            rec = trial_begin(components=components)
        d = trial_evaluate(rec, components=components)
    if server and (sync if sync is not None else True):
        try:
            pull_revocations(server, components=components)
            d = check(components=components)
            if not d.ok and d.status == "none" and allow_trial:
                rec = _read_trial(components)
                if rec is not None:
                    d = trial_evaluate(rec, components=components)
        except LicenseError:
            pass                            # 离线 / 服务器不可达：沿用本地判定
    _ENFORCED = bool(d.ok)
    return d


# ---------------------------------------------------------------- 试用

def trial_record_path() -> str:
    return paths()["trial"]


def _trial_key(fp_hash) -> bytes:
    return hmac.new(bytes.fromhex(_LEDGER_SECRET_HEX),
                    f"trial:{fp_hash}".encode("utf-8"),
                    hashlib.sha256).digest()


def _trial_mirrors() -> list:
    """试用记录的多副本：用户目录 + ProgramData（删一个不重置）。"""
    out = [trial_record_path()]
    pd = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    out.append(os.path.join(pd, APP_DIR_NAME, "trial.json"))
    return out


def _read_trial_store(path, key):
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return None
    payload, sig = raw.get("payload"), raw.get("sig")
    if not isinstance(payload, dict) or not isinstance(sig, str):
        return None                          # 旧明文记录：不可信
    if not hmac.compare_digest(sig, _sign_state(payload, key)):
        return None
    if _finite(payload.get("started_wall"), 0.0, MAX_EPOCH) is None:
        return None
    return payload


def _trial_reg(value=None, write=False):
    """注册表镜像：删文件不能重置试用。测试用 SZJ_NO_REGISTRY=1 关掉。"""
    if (os.environ.get("SZJ_NO_REGISTRY") and _test_hooks_allowed()) \
            or not sys.platform.startswith("win"):
        return None
    try:
        import winreg
        key_path = r"Software\SJZ-Control"
        if write:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                winreg.SetValueEx(k, "trial_start", 0, winreg.REG_DWORD,
                                  int(value))
            return value
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            return int(winreg.QueryValueEx(k, "trial_start")[0])
    except Exception:                        # noqa: BLE001
        return None


def _read_trial(components=None):
    """读最早的一份可信试用记录。返回 payload 或 None。"""
    fp = fingerprint(components)
    key = _trial_key(fp["hash"])
    starts = []
    for path in _trial_mirrors():
        rec = _read_trial_store(path, key)
        if rec is not None:
            starts.append(rec)
    reg = _trial_reg()
    if reg is not None:
        reg = _safe_int(reg, 0, MAX_EPOCH)
    if not starts and reg is None:
        return None
    earliest = min([r["started_wall"] for r in starts] + ([reg] if reg else []))
    base = next((r for r in starts if r["started_wall"] == earliest), None)
    if base is None:
        base = starts[0] if starts else {"lic_id": f"trial-{fp['hash'][:12]}",
                                         "fp": fp["hash"], "fp_vec": fp["vec"],
                                         "started_mono": None}
    rec = dict(base)
    rec["started_wall"] = earliest
    return rec


def trial_begin(*, components=None, now=None, mono=None, hours=None,
                force=False) -> dict:
    """开试用（本地兜底路径；正式产品的试用应由服务端签发）。

    · 时长固定 `TRIAL_HOURS`，**不读本地 hours**；
    · 记录 HMAC 签名，并同时写用户目录 / ProgramData / HKCU；
    · 任一副本存在就不再重置（删单个文件无效）。
    """
    now = time.time() if now is None else float(now)
    mono = time.monotonic() if mono is None else float(mono)
    fp = fingerprint(components)
    key = _trial_key(fp["hash"])

    existing = _read_trial(components)
    started = now if (force or existing is None) else existing["started_wall"]
    started_mono = mono if started == now else (
        existing or {}).get("started_mono")
    rec = {"lic_id": (existing or {}).get("lic_id") or f"trial-{fp['hash'][:12]}",
           "fp": fp["hash"], "fp_vec": fp["vec"],
           "started_wall": float(started), "started_mono": started_mono,
           "hours": TRIAL_HOURS, "trial": True}
    for path in _trial_mirrors():
        _write_signed(path, rec, key)
    if _trial_reg() is None:
        _trial_reg(int(started), write=True)
    return rec


def trial_evaluate(rec, *, now=None, mono=None, components=None) -> Decision:
    """本地试用的判定（固定时长 + 指纹绑定 + 时钟回拨保护）。"""
    now = time.time() if now is None else float(now)
    mono = time.monotonic() if mono is None else float(mono)
    if not rec:
        return Decision("none", False, "没有试用记录", trial=True)

    started = _finite(rec.get("started_wall"), 0.0, MAX_EPOCH)
    if started is None:
        return Decision("sig_invalid", False, "试用记录时间非法", trial=True)
    hours = float(TRIAL_HOURS)                # 固定，忽略记录里的 hours

    fp = fingerprint(components)
    stored = rec.get("fp_vec") or {}
    ratio, hits = match_score(stored, fp["vec"])
    if not _fp_passes(stored, hits, ratio):
        return Decision("fp_mismatch", False,
                        "试用记录绑定的不是这台机器", trial=True,
                        fp_score=ratio, fp_hits=hits)

    started_mono = _finite(rec.get("started_mono"), 0.0, MAX_EPOCH)
    same_boot = started_mono is not None and mono >= started_mono
    elapsed = (mono - started_mono) if same_boot else (now - started)
    if elapsed < -ROLLBACK_TOLERANCE_S:
        return Decision("clock_tamper", False, "试用期内检测到系统时间回拨",
                        trial=True, rollback_events=1)
    elapsed = max(0.0, elapsed)
    left = hours * 3600.0 - elapsed
    d = Decision("trial_ok", True, "试用中", trial=True,
                 lic_id=rec.get("lic_id"), tier="trial",
                 remaining_s=round(left, 1), fp_score=ratio, fp_hits=hits)
    if left <= 0:
        return Decision("expired", False, "试用已结束", trial=True,
                        lic_id=rec.get("lic_id"), tier="trial", remaining_s=0.0)
    return d


def trial_check(*, components=None, now=None, mono=None) -> Decision:
    rec = _read_trial(components)
    if not rec:
        return Decision("none", False, "没有试用记录", trial=True)
    return trial_evaluate(rec, now=now, mono=mono, components=components)
