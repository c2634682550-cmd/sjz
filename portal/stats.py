"""概览统计：总卡数 / 有效卡 / 今日下载 / 近 7 天趋势。

切天按时区配置（默认 Asia/Shanghai），SQLite 用 '+8 hours'；
迁 Postgres 时换成 `AT TIME ZONE`，SQL 只在 _portal.md 里列一处。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def tz(config):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    except Exception:                          # noqa: BLE001
        return timezone(timedelta(hours=8))


def today_key(config) -> str:
    return datetime.now(tz(config)).strftime("%Y-%m-%d")


def overview(db, config) -> dict:
    now = int(time.time())
    total = db.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    revoked = db.execute("SELECT COUNT(*) AS n FROM cards WHERE revoked=1"
                         ).fetchone()["n"]
    expired = db.execute(
        "SELECT COUNT(*) AS n FROM cards WHERE revoked=0 AND expires_at IS NOT NULL "
        "AND expires_at<=?", (now,)).fetchone()["n"]
    today = today_key(config)
    downloads_today = db.execute(
        "SELECT COUNT(*) AS n FROM downloads WHERE ts>=?",
        (_day_start_ts(config, today),)).fetchone()["n"]
    versions = db.execute("SELECT COUNT(*) AS n FROM versions").fetchone()["n"]
    online = db.execute("SELECT COUNT(*) AS n FROM versions WHERE enabled=1"
                        ).fetchone()["n"]
    return {"total_cards": total, "active_cards": total - revoked - expired,
            "revoked_cards": revoked, "expired_cards": expired,
            "downloads_today": downloads_today, "versions": versions,
            "versions_online": online,
            "trend": trend_7d(db, config)}


def _day_start_ts(config, day: str) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz(config))
    return int(dt.timestamp())


def trend_7d(db, config) -> list:
    zone = tz(config)
    today = datetime.now(zone).date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(6, -1, -1)]
    start = _day_start_ts(config, days[0])
    rows = db.execute("SELECT ts FROM downloads WHERE ts>=?", (start,)).fetchall()
    counts = {d: 0 for d in days}
    for row in rows:
        key = datetime.fromtimestamp(int(row["ts"]), zone).strftime("%Y-%m-%d")
        if key in counts:
            counts[key] += 1
    peak = max(counts.values()) or 1
    return [{"day": d, "count": counts[d], "pct": round(counts[d] / peak * 100)}
            for d in days]
