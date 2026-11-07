"""纯 Python Ed25519（RFC 8032）—— 许可签名验签用的最小实现。

为什么不用 cryptography / PyNaCl：
  · 许可校验每次启动只跑一次，纯 python 的 30~60ms 完全可以接受；
  · 少一个二进制依赖，打包体积和杀软画像都干净一点；
  · 算法照 RFC 8032 参考实现（原始版本 public domain），
    并且 tests/test_license.py 用 RFC 8032 §7.1 的官方测试向量做已知答案校验，
    不是「看起来能跑」就算过。

签名只给 `tools/license_admin.py`（服务端/离线签发机）用；客户端只需要 verify。
"""
import base64
import binascii
import hashlib

b = 256
q = 2 ** 252 + 27742317777372353535851937790883648493
p = 2 ** 255 - 19


def H(m):
    """SHA-512 摘要。"""
    return hashlib.sha512(m).digest()


def inv(x):
    """模 p 求逆。"""
    return pow(x, p - 2, p)


d = -121665 * inv(121666) % p
I = pow(2, (p - 1) // 4, p)


def xrecover(y):
    xx = (y * y - 1) * inv(d * y * y + 1)
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p != 0:
        x = (x * I) % p
    if x % 2 != 0:
        x = p - x
    return x


By = 4 * inv(5)
Bx = xrecover(By)
B = [Bx % p, By % p]


def edwards(P, Q):
    x1, y1 = P[0], P[1]
    x2, y2 = Q[0], Q[1]
    x3 = (x1 * y2 + x2 * y1) * inv(1 + d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * inv(1 - d * x1 * x2 * y1 * y2)
    return [x3 % p, y3 % p]


def scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = scalarmult(P, e // 2)
    Q = edwards(Q, Q)
    if e & 1:
        Q = edwards(Q, P)
    return Q


def encodeint(y):
    return bytes((y >> (8 * i)) & 0xff for i in range(b // 8))


def encodepoint(P):
    x, y = P[0], P[1]
    bits = [(y >> i) & 1 for i in range(b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8))
                 for i in range(b // 8))


def bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def publickey(seed):
    """32 字节私钥种子 → 32 字节公钥。"""
    h = H(seed)
    a = 2 ** (b - 2) + sum(2 ** i * bit(h, i) for i in range(3, b - 2))
    return encodepoint(scalarmult(B, a))


def Hint(m):
    h = H(m)
    return sum(2 ** i * bit(h, i) for i in range(2 * b))


def signature(m, sk, pk):
    """SHA-512(seed) 派生的 32 字节私钥 sk + 公钥 pk → 64 字节签名。"""
    h = H(sk)
    a = 2 ** (b - 2) + sum(2 ** i * bit(h, i) for i in range(3, b - 2))
    r = Hint(bytes(h[i] for i in range(b // 8, b // 4)) + m)
    R = scalarmult(B, r)
    S = (r + Hint(encodepoint(R) + pk + m) * a) % q
    return encodepoint(R) + encodeint(S)


def decodeint(s):
    return sum(2 ** i * bit(s, i) for i in range(0, b))


def decodepoint(s):
    y = sum(2 ** i * bit(s, i) for i in range(0, b - 1))
    x = xrecover(y)
    if x & 1 != bit(s, b - 1):
        x = p - x
    P = [x, y]
    if not isoncurve(P):
        raise ValueError("点不在曲线上")
    return P


def isoncurve(P):
    x, y = P[0], P[1]
    return (-x * x + y * y - 1 - d * x * x * y * y) % p == 0


def b64encode_sig(sig) -> str:
    return base64.b64encode(sig).decode("ascii")


def b64decode_sig(text) -> bytes:
    """base64 签名 → 64 字节；任何格式问题都转成 ValueError。"""
    try:
        raw = base64.b64decode(str(text).strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"不是合法 base64：{e}") from e
    if len(raw) != 64:
        raise ValueError(f"签名长度应为 64 字节，实际 {len(raw)}")
    return raw


def decode_pubkey_b64(text) -> bytes:
    """base64 公钥 → 32 字节。"""
    try:
        raw = base64.b64decode(str(text).strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"公钥不是合法 base64：{e}") from e
    if len(raw) != 32:
        raise ValueError(f"公钥长度应为 32 字节，实际 {len(raw)}")
    return raw


def verify(pk, m, signature_bytes):
    """验签。任何格式错误 / 非曲线点 / 长度不对都返回 False，不抛异常。"""
    try:
        if not pk or len(pk) != 32:
            return False
        if not signature_bytes or len(signature_bytes) != 64:
            return False
        P = decodepoint(pk)
        Q = decodepoint(signature_bytes[:b // 8])
        S = decodeint(signature_bytes[b // 8:])
        if S >= q:
            return False
        h = Hint(encodepoint(Q) + pk + m)
        # [S]B == R + [h]Q
        if scalarmult(B, S) != edwards(Q, scalarmult(P, h)):
            return False
        return True
    except (ValueError, TypeError, IndexError, OSError):
        return False
