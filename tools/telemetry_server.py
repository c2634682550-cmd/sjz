"""抢购遥测收集服务（作者自建后台的后端）。

三件事：
  1. `POST /api/telemetry/batch` —— 客户端上报（HMAC 验签 + 许可有效性 +
     限流 + schema 白名单 + 单批上限 + event_id 幂等入库）；
  2. `GET /api/admin/*` —— 总览 / 用户列表 / 单用户明细 / 趋势 / CSV，
     全部要管理员令牌（启动时从 `--admin-token` 或 `TELEMETRY_ADMIN_TOKEN` 取，
     代码里没有默认值）；
  3. `serve` 同时把 `tools/telemetry_dashboard.html` 原样吐出来（同源，无外链）。

许可数据**只读** `tools/license.db`（`licenses` 表），不参与签发；
另一路正在把签发改成 P-256，这里只按 `lic_id / fp / fp_vec / tier / exp /
revoked` 做判定，因此不受签名算法迁移影响。

启动：
    .venv\\Scripts\\python.exe tools\\telemetry_server.py serve --port 8091 ^
        --admin-token <TOKEN>
或：
    $env:TELEMETRY_ADMIN_TOKEN = "<TOKEN>"
    .venv\\Scripts\\python.exe tools\\telemetry_server.py serve

维护命令：
    ... export --scope users  --out exports/users.csv
    ... export --scope overview --out exports/overview.csv
    ... export --scope user --id <lic_hash> --out exports/user.csv
    ... purge --days 180 --archive archive/old.ndjson
    ... rebuild-agg
导出/归档路径只能落在 `data/telemetry/` 目录里，`..` 和绝对路径都会被拒。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as lic                       # noqa: E402  只读：派生上报密钥

DATA_ROOT = os.path.join(ROOT, "data", "telemetry")
DEFAULT_DB = os.path.join(DATA_ROOT, "telemetry.db")
DEFAULT_LICENSE_DB = os.path.join(ROOT, "tools", "license.db")
DEFAULT_DASHBOARD = os.path.join(ROOT, "tools", "telemetry_dashboard.html")

MAX_BATCH_EVENTS = 200            # 单批条数上限
MAX_BODY_BYTES = 512 * 1024       # 单批请求体上限
RATE_LIMIT_PER_MIN = 20           # 每个 license 每分钟最多几批
ONLINE_WINDOW_S = 600             # 10 分钟内有上报 = 在线
DEFAULT_RETENTION_DAYS = 180
SKEW_LIMIT = 5000                 # 卡点误差合理区间（毫秒）

TOP_KEYS = ("license_id", "machine", "client_version", "sent_at", "events")
EVENT_KEYS = ("event_id", "occurred_at", "tier", "name", "price", "result",
              "skew_ms", "skip_reason")
REQUIRED_EVENT_KEYS = ("event_id", "occurred_at", "tier", "name", "result")
RESULTS = ("hit", "miss", "skip")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  lic_hash TEXT NOT NULL,
  machine TEXT,
  occurred_at REAL NOT NULL,
  day TEXT NOT NULL,
  tier TEXT,
  name TEXT,
  price REAL,
  result TEXT NOT NULL,
  skew_ms INTEGER,
  skip_reason TEXT,
  client_version TEXT,
  received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_lic ON events(lic_hash, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE TABLE IF NOT EXISTS daily_agg(
  day TEXT NOT NULL, lic_hash TEXT NOT NULL, tier TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0,
  fails INTEGER NOT NULL DEFAULT 0,
  skips INTEGER NOT NULL DEFAULT 0,
  spent REAL NOT NULL DEFAULT 0,
  skew_sum REAL NOT NULL DEFAULT 0,
  skew_n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, lic_hash, tier)
);
"""

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 工具

def _log(msg):
    try:
        sys.stderr.write(f"[telemetry-server] {time.strftime('%H:%M:%S')} {msg}\n")
        sys.stderr.flush()
    except OSError:
        pass


def _day_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _mask(code):
    code = str(code or "")
    if len(code) <= 8:
        return (code[:2] + "…") if code else "—"
    return code[:4] + "…" + code[-4:]


def _lic_hash(lic_id):
    return hashlib.sha256(("lic:" + str(lic_id)).encode("utf-8")).hexdigest()[:32]


def _safe_path(base, rel):
    """把 rel 解析到 base 之下；绝对路径 / `..` / 软链逃逸一律 ValueError。"""
    base = os.path.realpath(base)
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("路径不能为空")
    if os.path.isabs(rel):
        raise ValueError("只接受相对路径（导出/归档统一放在数据目录里）")
    if ".." in rel.replace("\\", "/").split("/"):
        raise ValueError("路径里不允许出现 ..")
    target = os.path.realpath(os.path.join(base, rel))
    if os.path.commonpath([target, base]) != base:
        raise ValueError("路径越出了数据目录")
    return target


# ---------------------------------------------------------------- 许可（只读库）

class LicenseStore:
    """只读 tools/license.db，缓存 15 秒。

    另一路在改签发逻辑，这边只读 `licenses` 表，字段是稳定的。
    """

    TTL = 15.0

    def __init__(self, path):
        self.path = path
        self._rows = {}
        self._at = 0.0

    def _load(self):
        if not os.path.isfile(self.path):
            raise FileNotFoundError(f"许可库不存在：{self.path}")
        uri = "file:" + urllib.parse.quote(os.path.abspath(self.path)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT lic_id,code,fp,fp_vec,tier,exp,revoked FROM licenses"
            ).fetchall()
        finally:
            conn.close()
        out = {}
        for lic_id, code, fp, vec_raw, tier, exp, revoked in rows:
            try:
                vec = json.loads(vec_raw or "{}")
            except ValueError:
                vec = {}
            if not isinstance(vec, dict):
                vec = {}
            key = lic._ledger_key({"fp_vec": vec, "fp": fp or ""})
            out[_lic_hash(lic_id)] = {
                "lic_id": lic_id, "code": code or "", "fp": fp or "",
                "vec": vec, "tier": tier or "", "exp": exp,
                "revoked": int(revoked or 0), "key": key,
            }
        return out

    def get(self, lic_hash):
        now = time.time()
        if now - self._at > self.TTL:
            self._rows = self._load()
            self._at = now
        return self._rows.get(lic_hash)

    def all(self):
        if time.time() - self._at > self.TTL:
            self._rows = self._load()
            self._at = time.time()
        return dict(self._rows)


# ---------------------------------------------------------------- 存储

class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _q(self, sql, *args):
        with _LOCK:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # -- 写入
    def ingest(self, lic, events, machine, client_version):
        """幂等入库：event_id 已存在则跳过，同时只对真正插入的行累加日聚合。"""
        inserted = 0
        now = time.time()
        with _LOCK:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for ev in events:
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO events(event_id,lic_hash,machine,"
                        "occurred_at,day,tier,name,price,result,skew_ms,"
                        "skip_reason,client_version,received_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ev["event_id"], lic, machine, ev["occurred_at"],
                         _day_of(ev["occurred_at"]), ev["tier"], ev["name"],
                         ev["price"], ev["result"], ev["skew_ms"],
                         ev["skip_reason"], client_version, now))
                    if cur.rowcount:
                        inserted += 1
                        self._agg(ev, lic)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return inserted

    def _agg(self, ev, lic_hash):
        day = _day_of(ev["occurred_at"])
        hit = 1 if ev["result"] == "hit" else 0
        fail = 1 if ev["result"] == "miss" else 0
        skip = 1 if ev["result"] == "skip" else 0
        spent = (ev["price"] or 0.0) if ev["result"] == "hit" else 0.0
        skew = ev["skew_ms"]
        has_skew = 1 if (ev["result"] == "hit" and skew is not None) else 0
        self.conn.execute(
            "INSERT INTO daily_agg(day,lic_hash,tier,hits,fails,skips,spent,"
            "skew_sum,skew_n) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day,lic_hash,tier) DO UPDATE SET "
            "hits=hits+excluded.hits, fails=fails+excluded.fails, "
            "skips=skips+excluded.skips, spent=spent+excluded.spent, "
            "skew_sum=skew_sum+excluded.skew_sum, "
            "skew_n=skew_n+excluded.skew_n",
            (day, lic_hash, ev["tier"] or "未标注", hit, fail, skip,
             round(spent, 2), float(skew or 0), has_skew))

    def rebuild_agg(self):
        with _LOCK:
            self.conn.execute("DELETE FROM daily_agg")
            rows = self.conn.execute(
                "SELECT lic_hash,occurred_at,tier,result,price,skew_ms "
                "FROM events").fetchall()
            for lic_hash, ts, tier, result, price, skew in rows:
                ev = {"occurred_at": ts, "tier": tier, "result": result,
                      "price": price, "skew_ms": skew}
                self._agg(ev, lic_hash)
            self.conn.commit()
            return len(rows)

    # -- 读
    def overview(self):
        now = time.time()
        today = _day_of(now)
        online = self._q(
            "SELECT COUNT(DISTINCT lic_hash) FROM events WHERE received_at>=?",
            now - ONLINE_WINDOW_S).fetchone()[0]
        row = self._q(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='hit' THEN COALESCE(price,0) ELSE 0 END),"
            " AVG(CASE WHEN result='hit' AND skew_ms IS NOT NULL "
            "     THEN skew_ms END) "
            "FROM events WHERE day=?", today).fetchone()
        tiers = self._q(
            "SELECT tier,"
            " SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='miss' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='skip' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='hit' THEN COALESCE(price,0) ELSE 0 END) "
            "FROM events WHERE day=? GROUP BY tier ORDER BY 2 DESC", today
        ).fetchall()
        return {
            "server_time": round(now, 1),
            "today": today,
            "online": int(online or 0),
            "today_events": int(row[0] or 0),
            "today_hits": int(row[1] or 0),
            "today_spent": round(float(row[2] or 0), 2),
            "today_skew_avg_ms": (round(float(row[3]), 1)
                                  if row[3] is not None else None),
            "tiers": [{"tier": t or "未标注", "hits": int(h or 0),
                       "fails": int(f or 0), "skips": int(s or 0),
                       "spent": round(float(sp or 0), 2)}
                      for t, h, f, s, sp in tiers],
        }

    def trend(self, bucket="hour", points=24):
        now = time.time()
        if bucket == "hour":
            # 末尾一定落在「当前这个小时」，否则最近一小时的数据会落在窗外。
            now_hour = int(time.mktime(time.strptime(
                time.strftime("%Y-%m-%d %H:00:00"), "%Y-%m-%d %H:00:00")))
            start = now_hour - (points - 1) * 3600
            rows = self._q(
                "SELECT strftime('%m-%d %H', datetime(occurred_at,"
                "'unixepoch','localtime')) AS b,"
                " SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN result='miss' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN result='skip' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN result='hit' THEN COALESCE(price,0) ELSE 0 END) "
                "FROM events WHERE occurred_at>=? GROUP BY b ORDER BY b", start
            ).fetchall()
            slots = []
            for i in range(points):
                t = start + i * 3600
                slots.append(time.strftime("%m-%d %H", time.localtime(t)))
            index = {s: i for i, s in enumerate(slots)}
            out = [{"label": s, "hits": 0, "fails": 0, "skips": 0,
                    "spent": 0.0} for s in slots]
            for b, h, f, s2, sp in rows:
                i = index.get(b)
                if i is None:
                    continue
                out[i].update(hits=int(h or 0), fails=int(f or 0),
                              skips=int(s2 or 0), spent=round(float(sp or 0), 2))
            return {"bucket": "hour", "points": out}

        since_day = _day_of(now - points * 86400)
        rows = self._q(
            "SELECT day,"
            " SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='miss' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='skip' THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN result='hit' THEN COALESCE(price,0) ELSE 0 END) "
            "FROM events WHERE day>=? GROUP BY day ORDER BY day", since_day
        ).fetchall()
        out, cur = [], time.time() - (points - 1) * 86400
        by_day = {d: (h, f, s2, sp) for d, h, f, s2, sp in rows}
        for i in range(points):
            d = _day_of(cur + i * 86400)
            h, f, s2, sp = by_day.get(d, (0, 0, 0, 0))
            out.append({"label": d, "hits": int(h or 0), "fails": int(f or 0),
                        "skips": int(s2 or 0), "spent": round(float(sp or 0), 2)})
        return {"bucket": "day", "points": out}

    def users(self, since=None, until=None):
        sql = ("SELECT lic_hash,"
               " COUNT(*),"
               " SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END),"
               " SUM(CASE WHEN result='miss' THEN 1 ELSE 0 END),"
               " SUM(CASE WHEN result='skip' THEN 1 ELSE 0 END),"
               " SUM(CASE WHEN result='hit' THEN COALESCE(price,0) ELSE 0 END),"
               " MAX(occurred_at), MAX(received_at),"
               " AVG(CASE WHEN result='hit' AND skew_ms IS NOT NULL "
               "     THEN skew_ms END) "
               "FROM events")
        where, args = [], []
        if since is not None:
            where.append("occurred_at>=?")
            args.append(since)
        if until is not None:
            where.append("occurred_at<=?")
            args.append(until)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY lic_hash"
        rows = self._q(sql, *args).fetchall()

        tier_rows = self._q(
            "SELECT lic_hash,tier,SUM(CASE WHEN result='hit' THEN 1 ELSE 0 END) "
            "FROM events GROUP BY lic_hash,tier").fetchall()
        tier_map = {}
        for lic_hash, tier, hits in tier_rows:
            tier_map.setdefault(lic_hash, {})[(tier or "未标注")] = int(hits or 0)

        meta = self._q(
            "SELECT lic_hash,machine,client_version FROM events "
            "WHERE seq IN (SELECT MAX(seq) FROM events GROUP BY lic_hash)"
        ).fetchall()
        meta_map = {r[0]: (r[1] or "", r[2] or "") for r in meta}

        lic_rows = LICENSE.all()
        out = []
        for (lic_hash, n, hits, fails, skips, spent, last_ev,
             last_seen, skew_avg) in rows:
            m = lic_rows.get(lic_hash) or {}
            machine, version = meta_map.get(lic_hash, ("", ""))
            out.append({
                "id": lic_hash,
                "license_mask": _mask(m.get("code")),
                "license_tier": m.get("tier") or "",
                "machine": machine,
                "events": int(n or 0),
                "hits": int(hits or 0),
                "fails": int(fails or 0),
                "skips": int(skips or 0),
                "spent": round(float(spent or 0), 2),
                "skew_avg_ms": (round(float(skew_avg), 1)
                                if skew_avg is not None else None),
                "last_event": round(float(last_ev or 0), 1),
                "last_seen": round(float(last_seen or 0), 1),
                "online": (time.time() - float(last_seen or 0)) <= ONLINE_WINDOW_S,
                "client_version": version,
                "tier_hits": tier_map.get(lic_hash, {}),
            })
        return out

    def user_events(self, lic_hash, limit=50):
        rows = self._q(
            "SELECT occurred_at,tier,name,price,result,skew_ms,skip_reason,"
            "client_version FROM events WHERE lic_hash=? "
            "ORDER BY occurred_at DESC LIMIT ?", lic_hash, limit).fetchall()
        return [{"occurred_at": round(ts, 3), "tier": tier or "未标注",
                 "name": name or "", "price": price, "result": result,
                 "skew_ms": skew, "skip_reason": reason or "",
                 "client_version": version or ""}
                for ts, tier, name, price, result, skew, reason, version in rows]

    def user_daily(self, lic_hash, days=30):
        since = _day_of(time.time() - (days - 1) * 86400)
        rows = self._q(
            "SELECT day, SUM(hits), SUM(fails), SUM(skips), SUM(spent),"
            " SUM(skew_sum), SUM(skew_n) FROM daily_agg "
            "WHERE lic_hash=? AND day>=? GROUP BY day ORDER BY day",
            lic_hash, since).fetchall()
        out = []
        for day, h, f, s, sp, ss, sn in rows:
            out.append({"day": day, "hits": int(h or 0), "fails": int(f or 0),
                        "skips": int(s or 0), "spent": round(float(sp or 0), 2),
                        "skew_avg_ms": round(float(ss or 0) / float(sn), 1)
                        if sn else None})
        return out

    def purge(self, days, archive_path=None):
        """删掉 days 天前的事件 + 聚合。给了 archive 就先写一份 NDJSON 备份。"""
        cutoff = time.time() - days * 86400
        rows = self._q(
            "SELECT event_id,lic_hash,occurred_at,day,tier,name,price,result,"
            "skew_ms,skip_reason,client_version FROM events "
            "WHERE occurred_at<? ORDER BY occurred_at", cutoff).fetchall()
        if archive_path and rows:
            os.makedirs(os.path.dirname(archive_path), exist_ok=True)
            with open(archive_path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps({
                        "event_id": r[0], "lic_hash": r[1],
                        "occurred_at": r[2], "day": r[3], "tier": r[4],
                        "name": r[5], "price": r[6], "result": r[7],
                        "skew_ms": r[8], "skip_reason": r[9],
                        "client_version": r[10]}, ensure_ascii=False) + "\n")
        with _LOCK:
            cur = self.conn.execute("DELETE FROM events WHERE occurred_at<?",
                                    (cutoff,))
            self.conn.execute("DELETE FROM daily_agg WHERE day<?", (_day_of(cutoff),))
            self.conn.commit()
        return cur.rowcount, len(rows)


# ---------------------------------------------------------------- schema 校验

class BadRequest(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _need_text(value, name, maxlen, minimum=0):
    if not isinstance(value, str):
        raise BadRequest(f"字段 {name} 必须是字符串")
    v = value.strip()
    if len(v) < minimum:
        raise BadRequest(f"字段 {name} 不能为空")
    if len(v) > maxlen:
        raise BadRequest(f"字段 {name} 超长（上限 {maxlen} 字符）")
    return v


def _need_num(value, name, lo, hi, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRequest(f"字段 {name} 必须是数字")
    v = float(value)
    if not (lo <= v <= hi):
        raise BadRequest(f"字段 {name} 超出范围 [{lo}, {hi}]")
    return v


def clean_event(raw):
    if not isinstance(raw, dict):
        raise BadRequest("events 里每一项都必须是对象")
    unknown = set(raw) - set(EVENT_KEYS)
    if unknown:
        raise BadRequest("未知字段：" + "、".join(sorted(unknown)))
    missing = [k for k in REQUIRED_EVENT_KEYS if raw.get(k) in (None, "")]
    if missing:
        raise BadRequest("缺字段：" + "、".join(missing))
    eid = _need_text(raw["event_id"], "event_id", 64, 8)
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", eid):
        raise BadRequest("字段 event_id 只能是十六进制")
    ev = {
        "event_id": eid.lower(),
        "occurred_at": round(_need_num(raw["occurred_at"], "occurred_at",
                                      1, 1e11), 3),
        "tier": _need_text(raw["tier"], "tier", 16, 1),
        "name": _need_text(raw["name"], "name", 64),
        "price": _need_num(raw.get("price"), "price", 0, 1e7, allow_none=True),
        "result": _need_text(raw["result"], "result", 8, 1),
        "skew_ms": None,
        "skip_reason": "",
    }
    if ev["result"] not in RESULTS:
        raise BadRequest("字段 result 只能是 hit / miss / skip")
    if raw.get("skew_ms") is not None:
        s = _need_num(raw["skew_ms"], "skew_ms", -SKEW_LIMIT, SKEW_LIMIT)
        if s != int(s):
            raise BadRequest("字段 skew_ms 必须是整数毫秒")
        ev["skew_ms"] = int(s)
    if raw.get("skip_reason"):
        if ev["result"] != "skip":
            raise BadRequest("只有 result=skip 时才允许 skip_reason")
        ev["skip_reason"] = _need_text(raw["skip_reason"], "skip_reason", 24, 1)
    return ev


def clean_batch(body):
    if not isinstance(body, dict):
        raise BadRequest("请求体必须是 JSON 对象")
    unknown = set(body) - set(TOP_KEYS)
    if unknown:
        raise BadRequest("未知字段：" + "、".join(sorted(unknown)))
    lid = _need_text(body.get("license_id"), "license_id", 64, 32)
    if not re.fullmatch(r"[0-9a-f]{32}", lid):
        raise BadRequest("license_id 必须是 32 位十六进制哈希")
    machine = _need_text(body.get("machine"), "machine", 32, 8)
    if not re.fullmatch(r"[0-9a-f]{8,32}", machine):
        raise BadRequest("machine 必须是十六进制短哈希")
    version = _need_text(body.get("client_version"), "client_version", 20, 1)
    sent_at = _need_num(body.get("sent_at"), "sent_at", 1, 1e11)
    events = body.get("events")
    if not isinstance(events, list) or not events:
        raise BadRequest("events 不能为空")
    if len(events) > MAX_BATCH_EVENTS:
        raise BadRequest(f"单批最多 {MAX_BATCH_EVENTS} 条，当前 {len(events)} 条")
    return lid, machine, version, sent_at, [clean_event(e) for e in events]


# ---------------------------------------------------------------- 限流

class RateLimiter:
    def __init__(self, limit=RATE_LIMIT_PER_MIN, window=60.0):
        self.limit = limit
        self.window = window
        self._d = {}
        self._lock = threading.Lock()

    def hit(self, key):
        """返回 None = 放行；否则返回建议重试秒数。"""
        now = time.time()
        with self._lock:
            q = self._d.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])))
            q.append(now)
            if len(self._d) > 4096:
                for k in [k for k, v in self._d.items() if not v]:
                    self._d.pop(k, None)
        return None


# ---------------------------------------------------------------- 全局

DB = None
LICENSE = None
ADMIN_TOKEN = ""
LIMITER = RateLimiter()
START_AT = time.time()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "SJZTelemetry/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 只记状态行，不记请求体（事件内容不进日志）
        _log("%s %s" % (self.command, fmt % args))

    # -- 输出
    def _send(self, code, obj=None, raw=None, ctype="application/json; charset=utf-8",
              extra_headers=None):
        if raw is None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _err(self, code, msg, extra_headers=None):
        self._send(code, {"ok": False, "error": msg}, extra_headers=extra_headers)

    def _admin_ok(self):
        token = (self.headers.get("X-Admin-Token")
                 or (self.headers.get("Authorization") or "").replace("Bearer ", "")
                 or "").strip()
        if not ADMIN_TOKEN or not token:
            return False
        return hmac.compare_digest(token, ADMIN_TOKEN)

    def _admin_guard(self):
        if self._admin_ok():
            return True
        self._err(401, "需要管理员令牌（X-Admin-Token 或 Authorization: Bearer）")
        return False

    # -- 路由
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/dashboard", "/dashboard.html"):
            return self._serve_dashboard()
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/health":
            return self._send(200, {"ok": True, "uptime_s":
                                    int(time.time() - START_AT)})
        if not path.startswith("/api/admin/"):
            return self._err(404, "not found")
        if not self._admin_guard():
            return
        try:
            return self._admin_get(path, q)
        except BadRequest as e:
            return self._err(e.code, str(e))
        except Exception as e:                        # noqa: BLE001
            _log(f"管理接口出错：{type(e).__name__}: {e}")
            return self._err(500, "服务器内部错误")

    def _admin_get(self, path, q):
        if path == "/api/admin/overview":
            return self._send(200, DB.overview())
        if path == "/api/admin/trend":
            bucket = (q.get("bucket", ["hour"])[0] or "hour").lower()
            if bucket not in ("hour", "day"):
                raise BadRequest("bucket 只能是 hour 或 day")
            points = 24 if bucket == "hour" else 30
            return self._send(200, DB.trend(bucket, points))
        if path == "/api/admin/users":
            since, until = _range_of(q)
            users = DB.users(since, until)
            term = (q.get("q", [""])[0] or "").strip().lower()
            if term:
                users = [u for u in users
                         if term in u["license_mask"].lower()
                         or term in (u["license_tier"] or "").lower()
                         or term in u["machine"]]
            sort = (q.get("sort", ["hits"])[0] or "hits")
            if sort not in ("hits", "spent", "last_seen", "skips", "fails"):
                raise BadRequest("sort 只能是 hits / spent / last_seen")
            order = (q.get("order", ["desc"])[0] or "desc").lower()
            users.sort(key=lambda u: u.get(sort) or 0,
                       reverse=(order != "asc"))
            return self._send(200, {"generated_at": round(time.time(), 1),
                                    "count": len(users), "users": users})
        if path.startswith("/api/admin/user/"):
            lic_hash = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[0-9a-f]{32}", lic_hash):
                raise BadRequest("id 必须是 32 位十六进制")
            users = DB.users()
            meta = next((u for u in users if u["id"] == lic_hash), None)
            if not meta:
                return self._err(404, "没有这个用户的遥测数据")
            return self._send(200, {"user": meta,
                                    "events": DB.user_events(lic_hash, 50),
                                    "daily": DB.user_daily(lic_hash, 30)})
        if path == "/api/admin/reveal":
            return self._reveal(q)
        if path == "/api/admin/export":
            return self._export(q)
        return self._err(404, "not found")

    def _reveal(self, q):
        if (self.headers.get("X-Confirm") or "").strip().lower() not in ("yes", "1"):
            return self._err(428, "二次确认缺失：需要 X-Confirm: yes")
        lic_hash = (q.get("id", [""])[0] or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", lic_hash):
            raise BadRequest("id 必须是 32 位十六进制")
        m = LICENSE.all().get(lic_hash)
        if not m:
            return self._err(404, "许可库里没有这个用户")
        _log(f"管理员查看了 {lic_hash} 的完整卡密")
        return self._send(200, {"id": lic_hash, "code": m["code"],
                                "tier": m["tier"]})

    def _export(self, q):
        scope = (q.get("scope", ["users"])[0] or "users")
        lic_hash = (q.get("id", [""])[0] or "").strip()
        if scope in ("overview", "users"):
            text = export_csv(DB, scope)
        elif scope == "user":
            if not re.fullmatch(r"[0-9a-f]{32}", lic_hash):
                raise BadRequest("导出单用户需要合法的 id")
            text = export_csv(DB, scope, lic_hash)
        else:
            raise BadRequest("scope 只能是 overview / users / user")
        name = f"{scope}_{time.strftime('%Y%m%d_%H%M')}.csv"
        self._send(200, raw=("\ufeff" + text).encode("utf-8"),
                   ctype="text/csv; charset=utf-8",
                   extra_headers={"Content-Disposition":
                                  f'attachment; filename="{name}"',
                                  "X-Export-Scope": scope})

    def _serve_dashboard(self):
        try:
            with open(DEFAULT_DASHBOARD, "rb") as f:
                raw = f.read()
        except OSError:
            return self._err(500, "看板文件缺失：tools/telemetry_dashboard.html")
        self._send(200, raw=raw, ctype="text/html; charset=utf-8")

    # -- 上报
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/telemetry/batch":
            return self._err(404, "not found")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "Content-Length 非法")
        if length <= 0:
            return self._err(400, "请求体为空")
        if length > MAX_BODY_BYTES:
            return self._err(413, f"请求体过大（上限 {MAX_BODY_BYTES // 1024}KB）")
        raw = self.rfile.read(length)
        sig = (self.headers.get("X-Telemetry-Signature") or "").strip().lower()
        head_lid = (self.headers.get("X-Telemetry-License") or "").strip().lower()

        if not sig:
            return self._err(401, "缺少签名（X-Telemetry-Signature）")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._err(400, "请求体不是 UTF-8 JSON")
        try:
            lid, machine, version, _sent_at, events = clean_batch(body)
        except BadRequest as e:
            return self._err(e.code, str(e))
        if head_lid and head_lid != lid:
            return self._err(403, "请求头里的 license 与请求体不一致")

        lic_row = LICENSE.get(lid)
        if not lic_row:
            return self._err(403, "许可未登记（可能来自旧服务器或伪造）")
        if lic_row["revoked"]:
            return self._err(403, "许可已吊销")
        exp = lic_row["exp"]
        if exp is not None and float(exp) < time.time() - 3600:
            return self._err(403, "许可已到期")
        key = lic_row["key"]
        if not key:
            return self._err(403, "许可缺少指纹组件，无法验签")
        want = hmac.new(key, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, sig):
            return self._err(403, "签名不匹配（报文被改过或密钥不对）")

        wait = LIMITER.hit(lid)
        if wait:
            return self._err(429, f"上报过于频繁：每 license 每分钟最多 "
                                  f"{RATE_LIMIT_PER_MIN} 批，请 {wait}s 后再试",
                             extra_headers={"Retry-After": str(wait)})
        inserted = DB.ingest(lid, events, machine, version)
        return self._send(200, {"ok": True, "received": len(events),
                                "inserted": inserted,
                                "duplicates": len(events) - inserted})


def _range_of(q):
    """把 from/to（`YYYY-MM-DD` 或 epoch 秒）解析成 [since, until]。"""
    def one(key, end=False):
        v = (q.get(key, [""])[0] or "").strip()
        if not v:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            t = time.mktime(time.strptime(v, "%Y-%m-%d"))
            return t + (86399 if end else 0)
        if re.fullmatch(r"\d{9,12}(\.\d+)?", v):
            return float(v)
        raise BadRequest(f"{key} 只能是 YYYY-MM-DD 或 epoch 秒")
    return one("from"), one("to", end=True)


# ---------------------------------------------------------------- CSV

def _csv_bytes_str(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def export_csv(store, scope, lic_hash=None):
    if scope == "overview":
        ov = store.overview()
        rows = [["在线台数（10 分钟）", ov["online"]],
                ["统计日期", ov["today"]],
                ["今日事件数", ov["today_events"]],
                ["今日成交件数", ov["today_hits"]],
                ["今日总花费（元）", f"{ov['today_spent']:.2f}"],
                ["平均卡点误差（毫秒）",
                 "" if ov["today_skew_avg_ms"] is None else ov["today_skew_avg_ms"]]]
        for t in ov["tiers"]:
            rows.append([f"品阶分布·{t['tier']}·成交", t["hits"]])
            rows.append([f"品阶分布·{t['tier']}·花费（元）", f"{t['spent']:.2f}"])
        return _csv_bytes_str(["指标", "数值"], rows)
    if scope == "users":
        users = sorted(store.users(), key=lambda u: u["hits"], reverse=True)
        header = ["卡密（掩码）", "机器码（短哈希）", "许可档位", "事件数",
                  "成交件数", "失败", "跳过", "总花费（元）",
                  "平均卡点误差（毫秒）", "最后在线", "客户端版本", "各品阶成交数"]
        rows = []
        for u in users:
            rows.append([
                u["license_mask"], u["machine"], u["license_tier"],
                u["events"], u["hits"], u["fails"], u["skips"],
                f"{u['spent']:.2f}",
                "" if u["skew_avg_ms"] is None else u["skew_avg_ms"],
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u["last_seen"]))
                if u["last_seen"] else "",
                u["client_version"],
                " / ".join(f"{k}:{v}" for k, v in sorted(u["tier_hits"].items())),
            ])
        return _csv_bytes_str(header, rows)
    if scope == "user":
        evs = store.user_events(lic_hash, 500)
        daily = store.user_daily(lic_hash, 90)
        header = ["发生时间", "品阶", "商品名", "成交价（元）", "结果",
                  "卡点误差（毫秒）", "跳过原因"]
        rows = []
        for e in evs:
            rows.append([time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(e["occurred_at"])),
                         e["tier"], e["name"],
                         "" if e["price"] is None else f"{e['price']:.2f}",
                         {"hit": "成功", "miss": "失败", "skip": "跳过"}
                         .get(e["result"], e["result"]),
                         "" if e["skew_ms"] is None else e["skew_ms"],
                         e["skip_reason"]])
        text = _csv_bytes_str(header, rows)
        if daily:
            text += "\r\n日均统计\r\n"
            text += _csv_bytes_str(
                ["日期", "成交件数", "失败", "跳过", "花费（元）", "平均卡点误差（毫秒）"],
                [[d["day"], d["hits"], d["fails"], d["skips"],
                  f"{d['spent']:.2f}",
                  "" if d["skew_avg_ms"] is None else d["skew_avg_ms"]]
                 for d in daily])
        return text
    raise BadRequest("scope 只能是 overview / users / user")


# ---------------------------------------------------------------- 启动

def _connect_license_db(path):
    store = LicenseStore(path)
    store.all()                     # 启动即读一次：库缺失/表缺失直接暴露
    return store


def _serve(args):
    global DB, LICENSE, ADMIN_TOKEN
    token = args.admin_token or os.environ.get("TELEMETRY_ADMIN_TOKEN") or ""
    if not token.strip():
        raise SystemExit(
            "必须提供管理员令牌：--admin-token <TOKEN> 或环境变量 "
            "TELEMETRY_ADMIN_TOKEN（不写死在任何文件里）")
    ADMIN_TOKEN = token.strip()
    LICENSE = _connect_license_db(args.license_db)
    DB = Store(args.db)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"遥测收集服务已启动 http://{args.host}:{args.port}")
    print(f"  库        {args.db}")
    print(f"  许可库    {args.license_db}（只读，{len(LICENSE.all())} 条许可）")
    print(f"  看板      http://{args.host}:{args.port}/")
    print(f"  管理令牌  已从{'参数' if args.admin_token else '环境变量'}读取")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("收到 Ctrl+C，退出")


def main(argv=None):
    ap = argparse.ArgumentParser(description="抢购遥测收集服务")
    sub = ap.add_subparsers(dest="mode")

    p = sub.add_parser("serve", help="启动 HTTP 服务 + 看板")
    _common(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8091)
    p.add_argument("--admin-token", default="")

    p = sub.add_parser("export", help="导出 CSV（只能写进 data/telemetry/）")
    _common(p)
    p.add_argument("--scope", default="users",
                   choices=("overview", "users", "user"))
    p.add_argument("--id", default="")
    p.add_argument("--out", required=True)

    p = sub.add_parser("purge", help="清理超期数据（默认 180 天）")
    _common(p)
    p.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    p.add_argument("--archive", default="")

    p = sub.add_parser("rebuild-agg", help="按原始事件重建每日聚合表")
    _common(p)

    args = ap.parse_args(argv)
    if not args.mode:
        args.mode = "serve"
        args.host = "127.0.0.1"
        args.port = 8091
        args.admin_token = ""
        args.db, args.license_db = DEFAULT_DB, DEFAULT_LICENSE_DB
    if args.mode == "serve":
        return _serve(args)

    global DB, LICENSE
    DB = Store(args.db)
    LICENSE = _connect_license_db(args.license_db)

    if args.mode == "export":
        try:
            out = _safe_path(DATA_ROOT, args.out)
        except ValueError as e:
            raise SystemExit(f"导出路径被拒：{e}")
        text = export_csv(DB, args.scope, args.id)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
        print(f"已导出 {args.scope} → {out}")
        return 0
    if args.mode == "purge":
        archive = ""
        if args.archive:
            try:
                archive = _safe_path(DATA_ROOT, args.archive)
            except ValueError as e:
                raise SystemExit(f"归档路径被拒：{e}")
        n, before = DB.purge(args.days, archive or None)
        print(f"已清理 {n} 条（{args.days} 天前，命中 {before} 条）"
              + (f"，归档 {archive}" if archive else ""))
        return 0
    if args.mode == "rebuild-agg":
        n = DB.rebuild_agg()
        print(f"聚合表已按 {n} 条原始事件重建")
        return 0
    raise SystemExit("未知子命令")


def _common(p):
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--license-db", default=DEFAULT_LICENSE_DB)


if __name__ == "__main__":
    sys.exit(main())
