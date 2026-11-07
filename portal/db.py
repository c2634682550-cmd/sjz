"""SQLite 连接与 schema 初始化。

迁 Postgres 的路径写在 _portal.md：本文件是唯一持有 SQL 方言的地方，
把 `?` 占位符换成 `%s`、`AUTOINCREMENT` 换成 `BIGSERIAL`、
`INSERT ... ON CONFLICT` 已经是两边通用写法。
"""
from __future__ import annotations

import os
import sqlite3
import time

import flask

SCHEMA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def now() -> int:
    return int(time.time())


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def get_db() -> sqlite3.Connection:
    """每个请求一个连接（Flask g 生命周期内复用）。"""
    if "db" not in flask.g:
        flask.g.db = connect(flask.current_app.config["PORTAL_DB_PATH"])
    return flask.g.db


def close_db(_exc=None) -> None:
    db = flask.g.pop("db", None)
    if db is not None:
        db.close()


def audit(db: sqlite3.Connection, admin_id, action: str, target="",
          detail="") -> None:
    db.execute("INSERT INTO audit(ts, admin_id, action, target, detail) "
               "VALUES(?,?,?,?,?)", (now(), admin_id, action, str(target),
                                     str(detail)))
    db.commit()
