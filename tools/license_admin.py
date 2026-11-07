"""卡密签发 / 密钥管理 CLI（只在你的签发机上跑，不要随产品分发）。

用法（在仓库根）：
  .venv\\Scripts\\python.exe tools\\license_admin.py gen-key
  .venv\\Scripts\\python.exe tools\\license_admin.py gen-codes --db tools\\license.db --count 20 --days 365
  .venv\\Scripts\\python.exe tools\\license_admin.py list --db tools\\license.db
  .venv\\Scripts\\python.exe tools\\license_admin.py revoke --db tools\\license.db --lic-id LIC_ID
  .venv\\Scripts\\python.exe tools\\license_admin.py verify --license license.json

卡密兑换、许可续期、吊销生效都在 tools\\license_server.py（HTTP + SQLite）。
"""
import argparse
import os
import sqlite3
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as lic                       # noqa: E402

# 签发私钥只放仓库外（%LOCALAPPDATA%\SJZ-License-Keys 或 SJZ_SIGNING_KEY）。
DEFAULT_KEY = lic.default_signing_key_path()
DEFAULT_DB = os.path.join(ROOT, "tools", "license.db")

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"        # Crockford base32（无 I/L/O/U）
SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
  code TEXT PRIMARY KEY, tier TEXT NOT NULL, days INTEGER NOT NULL,
  created_at INTEGER NOT NULL, used_by TEXT, used_at INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS licenses (
  lic_id TEXT PRIMARY KEY, code TEXT, fp TEXT, fp_vec TEXT, tier TEXT,
  issued_at INTEGER, exp INTEGER, trial INTEGER DEFAULT 0,
  revoked INTEGER DEFAULT 0, revoked_at INTEGER, last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS trials (
  fp TEXT PRIMARY KEY, lic_id TEXT, first_at INTEGER
);
"""


def db_open(path):
    # check_same_thread=False：许可服务器用 ThreadingHTTPServer，多个请求
    # 会跨线程用同一个连接（R10-L02 的并发缺陷）。调用方必须自己加锁。
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    return conn


def load_seed(key_file) -> str:
    """从密钥文件读出 64 位十六进制种子。**只给签发机用**。"""
    with open(key_file, encoding="utf-8") as f:
        seed = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")][0]
    return seed.strip().lower()


def sign(payload, seed_hex):
    """用十六进制种子签名。

    历史缺陷：这里以前把 `seed` 当**文件路径** `open()`，而调用方传的是
    十六进制串 → 全部签发端点 500，且 FileNotFoundError 的消息把种子
    原样回显（R10-L02 / L-02B）。现在显式断言输入类型。
    """
    return lic.sign_blob(payload, seed_hex)


def gen_code() -> str:
    raw = uuid.uuid4().hex
    body = "".join(ALPHABET[int(raw[i:i + 2], 16) % 32] for i in range(0, 32, 2))
    return "SJZ-" + "-".join(body[i:i + 4] for i in range(0, 16, 4))


def make_payload(lic_id, fp, fp_vec, tier="pro", days=365, trial=False,
                 now=None, fp_weights=None, perpetual=False):
    now = int(now if now is not None else time.time())
    return {
        "v": lic.VERSION, "lic_id": lic_id, "tier": tier,
        "issued_at": now,
        "exp": None if perpetual else now + int(days) * 86400,
        "grace_h": lic.DEFAULT_GRACE_H,
        "lease_h": 24 if trial else lic.DEFAULT_LEASE_H,
        "offline_grace_h": lic.DEFAULT_OFFLINE_GRACE_H,
        "fp": fp, "fp_vec": fp_vec or {},
        "fp_weights": fp_weights or lic.WEIGHTS,
        "trial": bool(trial), "perpetual": bool(perpetual),
        "features": ["auto_buy", "ocr", "scheduler", "records"],
        "limits": {"max_tasks": 1} if trial else {},
    }


def cmd_gen_key(args):
    # 委托给 tools/keygen.py：P-256、仓库外落盘、拒绝覆盖。
    from tools import keygen
    argv = ["--out", args.out] if args.out else []
    raise SystemExit(keygen.main(argv))


def cmd_gen_codes(args):
    conn = db_open(args.db)
    now = int(time.time())
    codes = []
    for _ in range(args.count):
        code = gen_code()
        conn.execute("INSERT INTO keys(code,tier,days,created_at,note) VALUES(?,?,?,?,?)",
                     (code, args.tier, args.days, now, args.note))
        codes.append(code)
    conn.commit()
    conn.close()
    print("\n".join(codes))


def cmd_list(args):
    conn = db_open(args.db)
    rows = conn.execute(
        "SELECT code,tier,days,used_by IS NOT NULL,created_at FROM keys ORDER BY created_at DESC LIMIT ?",
        (args.limit,)).fetchall()
    print(f"{'卡密':<22} {'档位':<6} {'天数':<6} 已用")
    for code, tier, days, used, _ in rows:
        print(f"{code:<22} {tier:<6} {days:<6} {'是' if used else '否'}")
    n_lic = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
    n_rev = conn.execute("SELECT COUNT(*) FROM licenses WHERE revoked=1").fetchone()[0]
    print(f"\n共 {len(rows)} 条（最多显示 {args.limit}）；已签发许可 {n_lic}，已吊销 {n_rev}")
    conn.close()


def cmd_revoke(args):
    conn = db_open(args.db)
    cur = conn.execute("UPDATE licenses SET revoked=1, revoked_at=? WHERE lic_id=?",
                       (int(time.time()), args.lic_id))
    conn.commit()
    conn.close()
    print("已吊销" if cur.rowcount else "没找到这个 lic_id")


def cmd_issue(args):
    vec = {}
    if args.fp_vec:
        import json
        vec = json.loads(args.fp_vec)
    payload = make_payload("lic-" + uuid.uuid4().hex[:16], args.fp or "",
                           vec, tier=args.tier, days=args.days,
                           perpetual=args.days == 0)
    blob = sign(payload, load_seed(args.key_file))
    import json
    out = args.out or os.path.join(os.getcwd(), "license.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False, indent=2)
    print(f"已写入 {out}（tier={args.tier} days={args.days}）")


def cmd_verify(args):
    import json
    with open(args.license, encoding="utf-8") as f:
        blob = json.load(f)
    payload = lic.verify_blob(blob)
    print("签名有效：", json.dumps(payload, ensure_ascii=False, indent=2)[:500])


def main():
    ap = argparse.ArgumentParser(description="SJZ 卡密签发工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gen-key", help="生成一对新的 P-256 签发密钥（私钥只写仓库外）")
    p.add_argument("--out", help="私钥路径；默认 %LOCALAPPDATA%\\SJZ-License-Keys\\")
    p.set_defaults(fn=cmd_gen_key)

    p = sub.add_parser("gen-codes", help="批量生成卡密")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--days", type=int, default=365, help="0 = 永久")
    p.add_argument("--tier", default="pro")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_gen_codes)

    p = sub.add_parser("list", help="查看卡密/许可")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("revoke", help="吊销一个许可")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--lic-id", required=True)
    p.set_defaults(fn=cmd_revoke)

    p = sub.add_parser("issue", help="离线签发一个许可（已知指纹时用）")
    p.add_argument("--key-file", default=DEFAULT_KEY)
    p.add_argument("--fp", default="")
    p.add_argument("--fp-vec", default="")
    p.add_argument("--tier", default="pro")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_issue)

    p = sub.add_parser("verify", help="校验一个许可文件")
    p.add_argument("--license", required=True)
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
