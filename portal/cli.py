"""命令行：初始化、管理员、批量发卡、备份/恢复。

用法示例：
  .venv\\Scripts\\python.exe -m portal.cli init-db
  .venv\\Scripts\\python.exe -m portal.cli init-admin --username admin --password "***" --totp
  .venv\\Scripts\\python.exe -m portal.cli issue-batch --count 100 --days 365 --out cards.csv
  .venv\\Scripts\\python.exe -m portal.cli backup --out backup.zip
"""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal import cards_service, db as dbmod, stats, totp          # noqa: E402
from portal.config import Config                                      # noqa: E402
from portal.security import hash_password                             # noqa: E402
from portal.signing import Signer                                     # noqa: E402


def _cfg(args):
    return Config(getattr(args, "config", None) or {})


def cmd_init_db(args):
    cfg = _cfg(args)
    dbmod.init_db(cfg.db_path)
    print(f"数据库已初始化：{cfg.db_path}")


def cmd_init_admin(args):
    cfg = _cfg(args)
    dbmod.init_db(cfg.db_path)
    password = args.password or getpass.getpass("新管理员口令（≥10 位）：")
    if len(password) < 10:
        raise SystemExit("口令至少 10 位")
    secret = totp.new_secret() if args.totp else None
    db = dbmod.connect(cfg.db_path)
    try:
        db.execute("INSERT INTO admins(username,password_hash,role,totp_secret,"
                   "totp_enabled,created_at) VALUES(?,?,?,?,?,?)",
                   (args.username, hash_password(password), args.role,
                    secret, 1 if secret else 0, int(time.time())))
        db.commit()
    except sqlite3.IntegrityError:
        raise SystemExit(f"管理员 {args.username} 已存在") from None
    print(f"已创建管理员 {args.username}（{args.role}）")
    if secret:
        print("TOTP 密钥（只显示一次）：", secret)
        print("otpauth：", totp.otpauth_url(secret, args.username))


def cmd_gen_totp(args):
    cfg = _cfg(args)
    db = dbmod.connect(cfg.db_path)
    row = db.execute("SELECT * FROM admins WHERE username=?",
                     (args.username,)).fetchone()
    if row is None:
        raise SystemExit("管理员不存在")
    secret = totp.new_secret()
    db.execute("UPDATE admins SET totp_secret=?, totp_enabled=1 WHERE id=?",
               (secret, row["id"]))
    db.commit()
    print("TOTP 密钥（只显示一次）：", secret)
    print("otpauth：", totp.otpauth_url(secret, args.username))


def cmd_issue_batch(args):
    cfg = _cfg(args)
    dbmod.init_db(cfg.db_path)
    signer = Signer(cfg)
    if not signer.ready:
        raise SystemExit(f"签名器不可用：{signer.error}")
    if signer.dev_key:
        print("警告：当前使用 dev 密钥，签出的卡只能用于开发环境。")
    db = dbmod.connect(cfg.db_path)
    result = cards_service.create_batch(
        db, cfg, signer, count=args.count, days=args.days, tier=args.tier,
        note=args.note, max_per_day=args.max_per_day,
        bind_device=args.bind_device, admin_id=None)
    print(f"批次 #{result['batch_id']} 生成 {result['count']} 张，"
          f"{result['days']} 天，档位 {result['tier']}")
    if args.out:
        import csv
        with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["卡密", "档位", "有效期(天)", "备注"])
            for code in result["codes"]:
                writer.writerow([code, result["tier"], result["days"],
                                 result["note"]])
        print(f"已写入 {args.out}（明文只在这里出现一次）")
    else:
        for code in result["codes"]:
            print(code)


def cmd_stats(args):
    cfg = _cfg(args)
    db = dbmod.connect(cfg.db_path)
    data = stats.overview(db, cfg)
    print("总卡数", data["total_cards"], "有效", data["active_cards"],
          "今日下载", data["downloads_today"])
    for item in data["trend"]:
        print(item["day"], item["count"])


def cmd_backup(args):
    cfg = _cfg(args)
    out = args.out or f"portal-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    tmp_db = cfg.db_path + ".backup"
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(tmp_db)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp_db, "portal.db")
        zf.write(os.path.join(cfg.var_dir, "secret_key"), "secret_key")
        zf.write(os.path.join(cfg.var_dir, "card_pepper"), "card_pepper")
        for root, _dirs, files in os.walk(cfg.storage_dir):
            for name in files:
                path = os.path.join(root, name)
                rel = os.path.relpath(path, cfg.storage_dir)
                zf.write(path, os.path.join("storage", rel))
    os.remove(tmp_db)
    print(f"备份完成：{os.path.abspath(out)}")


def cmd_restore(args):
    cfg = _cfg(args)
    if not os.path.isfile(args.archive):
        raise SystemExit("备份文件不存在")
    with zipfile.ZipFile(args.archive) as zf:
        names = set(zf.namelist())
        if "portal.db" not in names:
            raise SystemExit("备份里没有 portal.db")
        for name in zf.namelist():
            if name.startswith(("../", "/")) or ".." in name.split("/"):
                raise SystemExit("备份包含越界路径，拒绝恢复")
        os.makedirs(cfg.var_dir, exist_ok=True)
        zf.extract("portal.db", cfg.var_dir)
        os.replace(os.path.join(cfg.var_dir, "portal.db"), cfg.db_path)
        for secret in ("secret_key", "card_pepper"):
            if secret in names:
                zf.extract(secret, cfg.var_dir)
        os.makedirs(cfg.storage_dir, exist_ok=True)
        for name in names:
            if name.startswith("storage/"):
                zf.extract(name, cfg.var_dir)
    print(f"恢复完成：{cfg.db_path}（如果服务在跑，记得重启）")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="portal.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-db")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("init-admin")
    p.add_argument("--username", required=True)
    p.add_argument("--password", default="")
    p.add_argument("--role", default="owner", choices=["owner", "staff"])
    p.add_argument("--totp", action="store_true")
    p.set_defaults(func=cmd_init_admin)

    p = sub.add_parser("gen-totp")
    p.add_argument("--username", required=True)
    p.set_defaults(func=cmd_gen_totp)

    p = sub.add_parser("issue-batch")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--tier", default="pro")
    p.add_argument("--note", default="")
    p.add_argument("--max-per-day", type=int, default=5)
    p.add_argument("--bind-device", action="store_true")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_issue_batch)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("backup")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore")
    p.add_argument("--archive", required=True)
    p.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
