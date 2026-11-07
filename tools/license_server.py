"""许可服务器（参考实现）：卡密兑换 / 续期 / 吊销 / 试用。

判定全在这里，客户端只验签。SQLite 做账，HTTP 只暴露下面这些端点。

    .venv\\Scripts\\python.exe tools\\license_server.py --db tools\\license.db --port 8090

端点（全部 POST，JSON）：
  /api/license/activate      {key, fp, fp_vec}                 → {license, revocations, server_now}
  /api/license/refresh       {license:{payload,sig}, fp, fp_vec} → 同上
  /api/license/trial         {fp, fp_vec}                      → 同上（每台机器一次）
  /api/license/revocations   {}                                → {revocations, server_now}
  /api/license/health        GET                              → {ok, sign}

启动前先 `tools/license_admin.py gen-codes --db tools/license.db` 生成卡密。

安全约定（本轮修复）：
  · 私钥只在签发机，`--key-file`（或 SJZ_SIGNING_KEY）指向的文件不进仓库 / 安装包；
  · 启动自检用本次加载的私钥派生公钥做往返；与内置公钥不配对时标 dev 并告警；
  · 任何异常都**不回显 str(e)**，日志里的种子串会被脱敏（R10-L02B）；
  · 试用去重与限速在服务端做，不盲信客户端上报的 fp（R10-L03）；
  · 续期不延长绝对到期日（R10-L11）。
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as lic                              # noqa: E402
from tools.license_admin import DEFAULT_DB, db_open, load_seed, sign  # noqa: E402

# 签发私钥的默认位置：与 portal 后台同一个解析函数（仓库外受控目录 /
# SJZ_SIGNING_KEY），**不指向仓库内的 dev 私钥**。可用 `--key-file` 显式指定。
# 自检用「本次实际加载的私钥」派生公钥做往返，不与编译期公钥硬比；
# 若与编译期公钥不配对，会标成 dev 模式并告警（生产客户端不认它签的卡）。
DEFAULT_KEY_EFFECTIVE = lic.default_signing_key_path()

_lock = threading.RLock()       # 允许同一请求内嵌套调用（activate → revocations_doc）
_SECRETS: list = []             # 需要从日志里脱敏的串（签名种子）
S = None                        # 当前 Store，由 main() 或测试注入

VEC_KEY_RE = re.compile(r"^[0-9a-f]{16,64}$")
TRIAL_MAX_PER_IP = 3            # 同一 IP 24h 内最多领 3 次试用
TRIAL_IP_WINDOW_S = 86400
STABLE_KEYS = ("bios_uuid", "machine_guid", "board_serial")


# ---------------------------------------------------------------- 脱敏

def _redact(text: str) -> str:
    """把私有种子串从任何输出里抹掉（含大小写、以及 base64 形态）。"""
    if not isinstance(text, str):
        return text
    for secret in _SECRETS:
        if not secret or len(secret) < 16:
            continue
        for form in (secret, secret.upper(), secret.lower()):
            text = text.replace(form, "[REDACTED]")
    return text


def _log_exception(path, exc):
    """异常只进服务端 stderr，且先脱敏；响应体永远拿不到细节。"""
    tb = _redact("".join(traceback.format_exception(type(exc), exc,
                                                    exc.__traceback__)))
    sys.stderr.write("[license-server] %s 处理失败：%s\n" % (path, tb.strip()))


# ---------------------------------------------------------------- 校验

def _clean_vec(raw):
    """客户端上报的组件表：键必须在权重表里，值必须是十六进制摘要。"""
    if not isinstance(raw, dict):
        return None
    vec = {}
    for key, value in raw.items():
        if key not in lic.WEIGHTS:
            continue
        if not isinstance(value, str) or not VEC_KEY_RE.match(value):
            return None
        vec[key] = value.lower()
    if len(vec) < lic.FP_MIN_COMPONENTS:
        return None
    return vec


def _derive_fp(vec) -> str:
    """服务端自己算指纹（与 core/license.fingerprint() 同一算法）。"""
    order = sorted(vec)
    return hashlib.sha256(
        "|".join(f"{k}={vec[k]}" for k in order).encode("utf-8")).hexdigest()


def _dedupe_key(vec) -> str:
    """试用去重键：优先用稳定组件子集，避免客户端换 MAC/换盘就重领。"""
    stable = "|".join(f"{k}={vec[k]}" for k in sorted(vec) if k in STABLE_KEYS)
    basis = stable or "|".join(f"{k}={vec[k]}" for k in sorted(vec))
    return hashlib.sha256(("trial-dedupe:" + basis).encode("utf-8")).hexdigest()


def _require_seed_hex(value) -> str:
    """签名种子必须是 64 位十六进制；不是就报错，**不把内容带进异常**。"""
    if not isinstance(value, str):
        raise ValueError("签名种子必须是十六进制字符串")
    s = value.strip().lower()
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("签名种子必须是 64 位十六进制")
    return s


# ---------------------------------------------------------------- 存储

class Store:
    """SQLite 账本。一个连接一把可重入锁 —— 参考实现不追求并发性能。"""

    def __init__(self, path, seed):
        self.seed = _require_seed_hex(seed)
        _SECRETS.append(self.seed)
        self.conn = db_open(path)
        self._migrate()
        self.selftest()

    def _migrate(self):
        with _lock:
            self.conn.executescript(
                "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER);"
                "CREATE TABLE IF NOT EXISTS trials_v2("
                " dedupe TEXT PRIMARY KEY, fp TEXT, ip TEXT, lic_id TEXT,"
                " first_at INTEGER);")
            self.conn.commit()

    def selftest(self):
        """启动自检：用**本次实际加载的私钥**派生公钥，做签→验往返。

        不拿编译期 PUBLIC_KEY_B64 当验签钥匙，所以 dev 环境用 dev 私钥也能起来；
        但会标记 dev_key 并告警 —— 生产客户端只认编译期公钥，dev 签的卡会被拒。
        """
        probe = {"v": lic.VERSION, "lic_id": "selftest", "gen": 0,
                 "lic_ids": [], "fp_ids": []}
        try:
            self.public_key_b64 = lic.public_key_b64_from_priv(self.seed)
            blob = lic.sign_blob(probe, self.seed)
            ok = lic.p256_verify(self.public_key_b64,
                                 lic.canonical_bytes(probe),
                                 base64.b64decode(blob["sig"]))
        except Exception as e:                # noqa: BLE001
            raise SystemExit(_redact(
                "许可服务器自检失败（私钥无法完成 P-256 往返）：%s" % e)) from None
        if not ok:
            raise SystemExit("许可服务器自检失败：私钥与派生公钥往返不一致")
        self.dev_key = self.public_key_b64 != lic.PUBLIC_KEY_B64
        if self.dev_key:
            sys.stderr.write(
                "[license-server] 警告：当前使用 dev 密钥（指纹 %s），"
                "与内置公钥不配对，签出的卡只能用于开发环境。\n"
                % hashlib.sha256(self.public_key_b64.encode()).hexdigest()[:16])

    def _q(self, sql, *args):
        with _lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def key(self, code):
        with _lock:
            return self.conn.execute(
                "SELECT code,tier,days,used_by FROM keys WHERE code=?",
                (code,)).fetchone()

    def bind_key(self, code, fp):
        self._q("UPDATE keys SET used_by=?, used_at=? WHERE code=?",
                fp, int(time.time()), code)

    def license_row(self, lic_id):
        with _lock:
            return self.conn.execute(
                "SELECT lic_id,code,fp,fp_vec,tier,issued_at,exp,trial,revoked "
                "FROM licenses WHERE lic_id=?", (lic_id,)).fetchone()

    def add_license(self, payload, code=""):
        self._q("INSERT INTO licenses(lic_id,code,fp,fp_vec,tier,issued_at,exp,"
                "trial,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                payload["lic_id"], code, payload["fp"],
                json.dumps(payload["fp_vec"], ensure_ascii=False),
                payload["tier"], payload["issued_at"], payload["exp"] or 0,
                1 if payload["trial"] else 0, int(time.time()))

    def touch(self, lic_id):
        self._q("UPDATE licenses SET last_seen=? WHERE lic_id=?",
                int(time.time()), lic_id)

    def trial_seen(self, dedupe):
        with _lock:
            return self.conn.execute(
                "SELECT first_at FROM trials_v2 WHERE dedupe=?", (dedupe,)).fetchone()

    def trial_count_from_ip(self, ip):
        with _lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM trials_v2 WHERE ip=? AND first_at>=?",
                (ip, int(time.time()) - TRIAL_IP_WINDOW_S)).fetchone()[0]

    def add_trial(self, dedupe, fp, ip, lic_id):
        self._q("INSERT INTO trials_v2(dedupe,fp,ip,lic_id,first_at) "
                "VALUES(?,?,?,?,?)", dedupe, fp, ip, lic_id, int(time.time()))

    def _next_rev_gen(self):
        """服务端单调计数器：吊销列表代数只增不减（R10-L06）。"""
        with _lock:
            row = self.conn.execute(
                "SELECT v FROM meta WHERE k='rev_gen'").fetchone()
            gen = int(row[0]) + 1 if row else 1
            self.conn.execute(
                "INSERT INTO meta(k,v) VALUES('rev_gen',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (gen,))
            self.conn.commit()
            return gen

    def revocations_doc(self):
        with _lock:
            rows = self.conn.execute(
                "SELECT lic_id,trial,fp FROM licenses WHERE revoked=1").fetchall()
        gen = self._next_rev_gen()
        payload = {"v": lic.VERSION, "gen": gen,
                   "lic_ids": [r[0] for r in rows],
                   "fp_ids": [r[2] for r in rows if r[1] and r[2]]}
        return lic.sign_blob(payload, self.seed)


# ---------------------------------------------------------------- 业务

def _make_license(fp, vec, tier, days, trial=False):
    now = int(time.time())
    days = max(0, min(int(days), lic.MAX_LICENSE_DAYS))   # 绝对上限
    payload = {
        "v": lic.VERSION, "lic_id": "lic-" + uuid.uuid4().hex[:16],
        "tier": tier, "issued_at": now,
        "exp": now + days * 86400,
        "grace_h": lic.DEFAULT_GRACE_H,
        "lease_h": 24 if trial else lic.DEFAULT_LEASE_H,
        "offline_grace_h": lic.DEFAULT_OFFLINE_GRACE_H,
        "fp": fp, "fp_vec": vec, "fp_weights": lic.WEIGHTS,
        "trial": bool(trial), "perpetual": days == 0,
        "features": ["auto_buy", "ocr", "scheduler", "records"],
        "limits": {"max_tasks": 1} if trial else {},
    }
    if payload["perpetual"]:
        payload["exp"] = None
    return payload


def _pack(payload):
    return {"license": sign(payload, S.seed),
            "revocations": S.revocations_doc(),
            "server_now": int(time.time())}


def _vec_from_body(body):
    """返回 (vec, fp)；客户端给错就返回 (None, 错误信息)。"""
    vec = _clean_vec(body.get("fp_vec"))
    if vec is None:
        return None, "机器指纹组件非法或数量不足（至少 %d 项）" % lic.FP_MIN_COMPONENTS
    derived = _derive_fp(vec)
    claimed = body.get("fp")
    if claimed and claimed != derived:
        return None, "上报的指纹与组件不一致"
    return vec, derived


def activate(body, _ip="127.0.0.1"):
    code = (body.get("key") or "").strip()
    if not code:
        return 400, {"error": "缺少卡密"}
    vec, fp = _vec_from_body(body)
    if vec is None:
        return 400, {"error": fp}
    row = S.key(code)
    if not row:
        return 404, {"error": "卡密不存在"}
    if row[3]:
        return 409, {"error": "卡密已被使用"}
    payload = _make_license(fp, vec, row[1], int(row[2]))
    S.bind_key(code, fp)
    S.add_license(payload, code)
    return 200, _pack(payload)


def refresh(body, _ip="127.0.0.1"):
    try:
        old = lic.verify_blob(body.get("license") or {})
    except lic.LicenseError as e:
        return 400, {"error": _redact("许可无效：%s" % e)}
    row = S.license_row(old["lic_id"])
    if not row:
        return 404, {"error": "许可未登记（可能来自旧服务器）"}
    if row[8]:
        return 403, {"error": "许可已吊销"}
    vec, fp = _vec_from_body(body)
    if vec is None:
        return 400, {"error": fp}
    if fp != row[2]:
        return 403, {"error": "机器指纹与签发时不一致"}
    ratio, _hits = lic.match_score(json.loads(row[3] or "{}"), vec,
                                   old.get("fp_weights"))
    if ratio < lic.FP_MIN_RATIO:
        return 403, {"error": "机器指纹匹配不足（%.0f%%）" % (ratio * 100)}

    # 续期：**不延长到期日**。exp 是签发时写死的绝对上限，刷新只续租约。
    new = dict(old)
    new["refreshed_at"] = int(time.time())
    row_exp = row[6] or None
    if old.get("exp") is not None and row_exp:
        new["exp"] = min(float(old["exp"]), float(row_exp))
    S.touch(old["lic_id"])
    return 200, _pack(new)


def trial(body, ip="127.0.0.1"):
    vec, fp = _vec_from_body(body)
    if vec is None:
        return 400, {"error": fp}
    dedupe = _dedupe_key(vec)
    if S.trial_seen(dedupe):
        return 409, {"error": "该机器已经用过试用"}
    if S.trial_count_from_ip(ip) >= TRIAL_MAX_PER_IP:
        return 429, {"error": "该来源今日试用次数过多"}
    payload = _make_license(fp, vec, "trial", lic.TRIAL_HOURS // 24, trial=True)
    S.add_trial(dedupe, fp, ip, payload["lic_id"])
    S.add_license(payload)
    return 200, _pack(payload)


ROUTES = {"/api/license/activate": activate,
          "/api/license/refresh": refresh,
          "/api/license/trial": trial}


class Handler(BaseHTTPRequestHandler):
    server_version = "SJZLicense/1.0"

    def log_message(self, fmt, *args):
        # 只记方法/路径/状态行；请求体与响应体不进日志。
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"),
                                      _redact(fmt % args)))

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/license/health":
            # alg 显式暴露，便于核对「签发 / 验签 / 启动器」三处用的是同一套曲线。
            return self._send(200, {"ok": True, "sign": True,
                                    "alg": lic.SIG_ALG, "curve": "P-256",
                                    "sig_format": "P1363",
                                    "key_mode": ("dev" if getattr(S, "dev_key", True)
                                                 else "prod")})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        ip = self.client_address[0] if self.client_address else "0.0.0.0"
        if path == "/api/license/revocations":
            return self._send(200, {"revocations": S.revocations_doc(),
                                    "server_now": int(time.time())})
        fn = ROUTES.get(path)
        if fn is None:
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, OSError):
            return self._send(400, {"error": "请求体不是 JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "请求体必须是 JSON 对象"})
        try:
            code, obj = fn(body, ip)
        except Exception as e:                # noqa: BLE001
            # 只在服务端日志里记录（且脱敏）；响应体是固定的、不含任何材料。
            _log_exception(path, e)
            return self._send(500, {"error": "internal error"})
        self._send(code, obj)


def main():
    global S
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--db", default=os.environ.get("SJZ_LICENSE_DB") or DEFAULT_DB)
    ap.add_argument("--key", "--key-file", dest="key", default=DEFAULT_KEY_EFFECTIVE,
                    help="签发私钥路径（不传则读 %s 或仓库外默认位置）"
                         % lic.SIGNING_KEY_ENV)
    args = ap.parse_args()
    try:
        seed = load_seed(args.key)
    except FileNotFoundError:
        raise SystemExit("找不到签发私钥：%s\n用 --key-file 指定，或设 %s。"
                         % (args.key, lic.SIGNING_KEY_ENV)) from None
    S = Store(args.db, seed)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    pub = lic.PUBLIC_KEY_B64
    print(f"许可服务已启动 http://{args.host}:{args.port}  db={args.db}")
    print(f"签名算法 {lic.SIG_ALG}（P-256 / r||s），私钥 {args.key}")
    print(f"本次私钥派生公钥指纹 {hashlib.sha256(S.public_key_b64.encode()).hexdigest()[:16]}"
          f"；内置公钥指纹 {hashlib.sha256(pub.encode()).hexdigest()[:16]}")
    print("模式：%s" % ("dev（签出的卡只能用于开发环境）" if S.dev_key
                       else "prod（与内置公钥配对）"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
