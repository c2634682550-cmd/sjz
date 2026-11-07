"""版本包：接收、校验、落盘、上下架。文件永远在 web 根之外。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid

from .security import UploadError, inspect_zip, safe_filename

VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+\-]{0,39}$")
ALLOWED_EXT = {".zip"}


def validate_version(version: str) -> str:
    version = (version or "").strip()
    if not VERSION_RE.match(version) or ".." in version:
        raise UploadError("版本号只允许字母/数字/._+-，长度 1-40")
    return version


def latest_enabled(db):
    return db.execute(
        "SELECT * FROM versions WHERE enabled=1 ORDER BY uploaded_at DESC, id DESC "
        "LIMIT 1").fetchone()


def save_upload(db, config, file_storage, *, version: str, notes: str,
                force_update: bool) -> dict:
    version = validate_version(version)
    filename = safe_filename(getattr(file_storage, "filename", "") or "pkg.zip")
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise UploadError("只接受 .zip 文件")

    limit = int(config.get("max_upload_bytes", 200 * 1024 * 1024))
    tmp_name = f"upload-{uuid.uuid4().hex}.zip"
    tmp_path = os.path.join(config.tmp_dir, tmp_name)
    written = 0
    digest = hashlib.sha256()
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise UploadError(
                        f"文件超过大小限制（{limit // (1024 * 1024)} MB）")
                digest.update(chunk)
                out.write(chunk)
        if written == 0:
            raise UploadError("文件为空")
        info = inspect_zip(tmp_path, config)

        rel_dir = f"versions/{uuid.uuid4().hex}"
        abs_dir = os.path.join(config.storage_dir, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        stored_rel = f"{rel_dir}/{filename}"
        shutil.move(tmp_path, os.path.join(abs_dir, filename))
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    now = int(time.time())
    try:
        cur = db.execute(
            "INSERT INTO versions(version,notes,force_update,enabled,sha256,size,"
            "filename,stored_path,uploaded_at) VALUES(?,?,?,1,?,?,?,?,?)",
            (version, notes, 1 if force_update else 0, digest.hexdigest(),
             written, filename, stored_rel, now))
        db.commit()
    except Exception as exc:                   # sqlite3.IntegrityError
        shutil.rmtree(abs_dir, ignore_errors=True)
        if "UNIQUE" in str(exc).upper():
            raise UploadError(f"版本号 {version} 已存在") from exc
        raise
    return {"id": cur.lastrowid, "version": version, "sha256": digest.hexdigest(),
            "size": written, "entries": info["entries"],
            "uncompressed": info["uncompressed"]}


def set_enabled(db, version_row, enabled: bool) -> None:
    db.execute("UPDATE versions SET enabled=? WHERE id=?",
               (1 if enabled else 0, version_row["id"]))
    db.commit()


def set_force(db, version_row, force: bool) -> None:
    db.execute("UPDATE versions SET force_update=? WHERE id=?",
               (1 if force else 0, version_row["id"]))
    db.commit()


def get(db, version_id: int):
    return db.execute("SELECT * FROM versions WHERE id=?",
                      (version_id,)).fetchone()


def confirm_sha256(path: str, expected: str) -> bool:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest() == expected
