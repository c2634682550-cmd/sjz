"""生成 P-256 签发密钥（替代 tools/license_admin.py gen-key 的 Ed25519 版本）。

用法（在项目根）：
    .venv\\Scripts\\python.exe launcher\\tools\\genkey_p256.py --out tools\\license_keys\\prod_p256.txt

输出文件是「注释头 + 十六进制标量」，与 tools/license_admin.py load_seed 兼容。
生成后把 public_key_b64 写进 core/license.py 的 PUBLIC_KEY_B64（发布版），
并且该私钥文件必须 gitignore、离线保管。
"""
import argparse
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core import license as lic  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    while True:
        d = secrets.randbelow(lic._P256_N - 1) + 1
        h = "%064x" % d
        if len(h) == 64:
            break
    pub = lic.public_key_b64_from_priv(h)

    text = (
        "# SJZ 卡密签发密钥（ECDSA P-256 / SHA-256）\n"
        "# 这个文件必须 gitignore，绝不打进发布包。\n"
        "# 对应公钥（写进 core/license.py 的 PUBLIC_KEY_B64）：\n"
        "#   %s\n" % pub
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + h + "\n")
        print("已写入 %s" % args.out)
    else:
        print(text + h)
    print("公钥 base64: %s" % pub)
    print("算法: %s / P1363 r||s（各 32 字节）" % lic.SIG_ALG)


if __name__ == "__main__":
    main()
