"""生成 **dev** 签发密钥对（ECDSA P-256 / SHA-256，签名编码 IEEE P1363 r||s）。

定位：这是开发/联调用钥，不产出、也不替换生产公钥。协议不变：
  · 公钥 = base64(X || Y)，X/Y 各 32 字节大端；
  · 签名 = base64(r || s)，各 32 字节。

用法（在仓库根）：
    .venv\\Scripts\\python.exe tools\\keygen.py
    .venv\\Scripts\\python.exe tools\\keygen.py --out D:\\tmp\\dev_p256.txt
    .venv\\Scripts\\python.exe tools\\keygen.py --print-only   # 只看公钥，不落盘

约定：
  · 私钥默认写到仓库外的受控目录（%LOCALAPPDATA%\\SJZ-License-Keys\\dev_...），
    或环境变量 SJZ_SIGNING_KEY 指向的位置；
  · **拒绝**写到仓库内，也**拒绝**覆盖任何已存在的私钥文件；
  · 私钥内容只写文件，不打印、不进日志；屏幕上只有公钥、指纹与 dev 提示。
  · 生产私钥不走这个脚本的默认值；生产由运维单独生成/保管，
    并把公钥同步进 core/license.py 与 launcher/Crypto.cs。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import license as lic      # noqa: E402


def default_dev_key_path() -> str:
    """dev 私钥的默认位置：仓库外、与生产私钥同目录但文件名明确是 dev_。"""
    return os.path.join(os.path.dirname(lic.default_signing_key_path()),
                        "dev_signing_key_p256.txt")


def _inside_repo(path: str) -> bool:
    """路径是否落在仓库目录内（软链接按真实路径判断）。"""
    real = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    root = os.path.normcase(os.path.realpath(ROOT))
    return real == root or real.startswith(root + os.sep)


def _write_new(path: str, seed_hex: str, pub_b64: str) -> None:
    """原子地新建私钥文件；已存在就抛 FileExistsError（绝不覆盖）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("# SJZ **dev** 卡密签发私钥（ECDSA P-256 / SHA-256，P1363 r||s）\n")
        f.write("# 只用于开发/联调；生产客户端不信任它签出的卡。\n")
        f.write("# 对应公钥（公开）：%s\n" % pub_b64)
        f.write(seed_hex + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="生成 dev P-256 签发密钥（私钥只写仓库外，不碰生产公钥）")
    ap.add_argument("--out", default=default_dev_key_path(),
                    help="私钥输出路径；默认仓库外的 dev_signing_key_p256.txt")
    ap.add_argument("--print-only", action="store_true",
                    help="只生成并打印公钥，不写任何文件")
    args = ap.parse_args(argv)

    out = os.path.abspath(os.path.expanduser(args.out))
    if not args.print_only and _inside_repo(out):
        print("拒绝：私钥不能写进仓库（%s）。请用 --out 指到仓库外。" % out,
              file=sys.stderr)
        return 2

    seed = lic.new_private_key_hex()
    pub = lic.public_key_b64_from_priv(seed)

    if not args.print_only:
        try:
            _write_new(out, seed, pub)
        except FileExistsError:
            print("拒绝：%s 已存在，不覆盖已存在的私钥。"
                  "换一个 --out，或先确认旧钥可以作废再手工移走。" % out,
                  file=sys.stderr)
            return 3

    fingerprint = hashlib.sha256(pub.encode("utf-8")).hexdigest()[:16]
    print("dev 公钥（仅供开发联调）：")
    print("  %s" % pub)
    print("公钥指纹 sha256[:16]：%s" % fingerprint)
    print("算法：%s / P1363 r||s（各 32 字节）" % lic.SIG_ALG)
    if pub == lic.PUBLIC_KEY_B64:
        print("注意：它与 core/license.py 的 PUBLIC_KEY_B64 巧合相同，"
              "请确认这不是在生产环境里跑。")
    else:
        print("当前使用 dev 密钥：与编译期 PUBLIC_KEY_B64 不配对，"
              "它签出的卡只能用于开发环境。")
    if args.print_only:
        print("本次只打印公钥，未写私钥文件。")
    else:
        print("dev 私钥已写入 %s（仓库外，权限 600）" % out)
        print("签服务器可用 --key-file 指向它，或设 %s。" % lic.SIGNING_KEY_ENV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
