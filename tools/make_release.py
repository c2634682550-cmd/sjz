"""生成发布物：更新 zip + version.json（version/url/sha256/notes）。

用法：
    python tools/make_release.py --dist dist\\SJZ-Control \
        --base-url https://dl.example.com/sjz \
        --notes-file RELEASE_NOTES.md \
        [--key path\\to\\signing.seed] [--out dist-release]

输出：
    dist-release\\SJZ-Control-<version>.zip
    dist-release\\version.json          （有私钥时是签名格式，否则明文）
    dist-release\\release_manifest.json （本地留档：大小/哈希/条目数）

签名复用 core/license.py 的 P-256 实现与规范化，和启动器 Json.Canonical
逐字节一致，不做第二套密码学。
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.version import APP_VERSION  # noqa: E402


def zip_dir(src, dst, root_name="."):
    entries = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache")]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                p = os.path.join(base, f)
                rel = os.path.relpath(p, src)
                arc = rel if root_name == "." else os.path.join(root_name, rel)
                z.write(p, arc.replace(os.sep, "/"))
                entries += 1
    return entries


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sign(payload):
    """有私钥就签，返回 {"payload":…, "sig":…}；否则返回 None。"""
    from core import license as core_license
    from portal import signing

    path = os.environ.get("SJZ_SIGNING_KEY") or core_license.default_signing_key_path()
    seed = signing.load_seed(path)
    if not seed:
        return None
    blob = signing.sign_payload(payload, seed)
    if not signing.verify_payload(payload, blob["sig"]):
        raise SystemExit("签名自检失败：私钥与 core/license.PUBLIC_KEY_B64 不配对")
    return blob


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist", "SJZ-Control"))
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--notes", default="")
    ap.add_argument("--notes-file")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist-release"))
    args = ap.parse_args(argv)

    if not os.path.isdir(args.dist):
        raise SystemExit(f"dist 不存在：{args.dist}（先跑 pyinstaller sjz.spec）")

    os.makedirs(args.out, exist_ok=True)
    name = f"SJZ-Control-{APP_VERSION}.zip"
    zip_path = os.path.join(args.out, name)
    entries = zip_dir(args.dist, zip_path)
    digest = sha256_file(zip_path)

    notes = args.notes
    if args.notes_file and os.path.isfile(args.notes_file):
        notes = open(args.notes_file, encoding="utf-8").read().strip()

    url = args.base_url.rstrip("/") + "/" + name
    payload = {"version": APP_VERSION, "url": url, "sha256": digest, "notes": notes}
    blob = sign(payload)

    vj_path = os.path.join(args.out, "version.json")
    with open(vj_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(blob or payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    manifest = {
        "version": APP_VERSION,
        "zip": name,
        "size": os.path.getsize(zip_path),
        "sha256": digest,
        "entries": entries,
        "signed": blob is not None,
        "version_json": "version.json",
    }
    with open(os.path.join(args.out, "release_manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if blob is None:
        print("警告：没有可用私钥，version.json 是明文格式；"
              "客户端需 update_require_sig=false 才会接受。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
