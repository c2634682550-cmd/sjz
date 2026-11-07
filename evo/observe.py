"""观测层：只读两个事实源，按游标增量读取，不重读历史。

事实源 A：data/records.jsonl（成交台账，追加写、可轮转）
事实源 B：data/telemetry/telemetry.db（遥测库，events 表按 seq 递增）

游标落 state/cursors.json；日聚合落 state/rollup.json；
最近若干条明细落 state/replay.jsonl（供离线回放用，读也只读这个自有缓存）。
"""
import json
import os
import sqlite3
import time
from urllib.parse import quote

from .common import (abs_path, atomic_write_json, day_str, ensure_dir,
                     load_json, safe_ratio)
from .config import state_dir
from . import pool_contract, pool_inbox
from core import settle_verify

LEDGER_RESULTS = ("hit", "miss", "no_money", "over_budget", "aborted")
CLICK_RESULTS = ("hit", "miss", "no_money", "over_budget")
POOL_RESULTS = ("hit", "miss")
BLOCK_RESULTS = ("no_money", "over_budget")


def _tier(name, keywords):
    n = str(name or "")
    for kw in keywords:
        if kw and kw in n:
            return kw
    if "｜" in n or "|" in n:
        head = n.replace("|", "｜").split("｜")[0].strip()
        if head:
            return head
    return "未标注"


def _norm_ledger(row, cfg, now):
    ts = row.get("ts")
    name = str(row.get("name") or "")
    result = str(row.get("result") or "")
    out = {
        "ts": float(ts),
        "day": day_str(ts),
        "name": name,
        "name_len": len(name),
        "price": row.get("price") if isinstance(row.get("price"), (int, float)) else None,
        "result": result,
        "mode": str(row.get("mode") or ""),
        "quota": row.get("quota"),
        "verified": row.get("verified"),
        "verified_by": str(row.get("verified_by") or ""),
        "elapsed": row.get("elapsed") if isinstance(row.get("elapsed"), (int, float)) else None,
        "shot": str(row.get("shot") or ""),
        "tier": _tier(name, cfg["funnel"]["tier_keywords"]),
        "source": "ledger",
        "future_ts": bool(ts and ts > now + 86400),
    }
    for k in pool_contract.CARRY_FIELDS:
        out[k] = row.get(k)
    return out


def _read_ledger(cfg, cursors, now):
    spec = cfg["observation"]
    p = abs_path(spec["ledger_path"])
    cur = cursors.setdefault("ledger", {})
    if cur.get("path") != p:
        cur.clear()
        cur.update({"path": p, "offset": 0, "rows": 0})
    meta = {"path": p, "status": "absent", "new_rows": 0, "invalid_rows": 0,
            "rotation_reset": False, "partial_tail": False,
            "bytes_read": 0, "size": 0, "suspicious_rows": 0}
    if not os.path.exists(p):
        return [], meta, cur, []

    size = os.path.getsize(p)
    meta["size"] = size
    off = int(cur.get("offset") or 0)
    if size < off:
        off = 0
        meta["rotation_reset"] = True
    if size == off:
        meta["status"] = "ok"
        return [], meta, cur, []

    with open(p, "rb") as f:
        f.seek(off)
        blob = f.read(int(spec["max_batch_rows"]) * 4096 or size)

    rows, anomalies = [], []
    consumed = 0
    end_nl = blob.endswith(b"\n")
    parts = blob.split(b"\n")
    for i, part in enumerate(parts):
        last = (i == len(parts) - 1)
        if last and not end_nl:
            meta["partial_tail"] = True
            break
        consumed += len(part) + (0 if last else 1)
        txt = part.decode("utf-8", "replace").strip()
        if not txt:
            continue
        try:
            row = json.loads(txt)
        except ValueError:
            meta["invalid_rows"] += 1
            anomalies.append({"code": "invalid_json", "detail": txt[:80]})
            continue
        if not isinstance(row, dict) or not isinstance(row.get("ts"), (int, float)):
            meta["invalid_rows"] += 1
            anomalies.append({"code": "missing_ts", "detail": txt[:80]})
            continue
        if row.get("result") not in LEDGER_RESULTS:
            anomalies.append({"code": "unknown_result",
                              "detail": str(row.get("result"))[:24]})
        nm = str(row.get("name") or "")
        if len(nm) > int(spec["max_name_bytes"]):
            anomalies.append({"code": "name_too_long",
                              "detail": f"{len(nm)} chars > {spec['max_name_bytes']}"})
            meta["suspicious_rows"] += 1
        if row.get("result") not in LEDGER_RESULTS:
            meta["suspicious_rows"] += 1
        if schema_ts_future(row.get("ts"), now):
            anomalies.append({"code": "future_ts", "detail": str(row.get("ts"))})
            meta["suspicious_rows"] += 1
            continue
        rows.append(row)
        if len(rows) >= int(spec["max_batch_rows"]):
            break

    meta["status"] = "ok"
    meta["new_rows"] = len(rows)
    meta["invalid_rows"] = meta["invalid_rows"]
    meta["bytes_read"] = consumed
    cur["offset"] = off + consumed
    cur["rows"] = int(cur.get("rows") or 0) + len(rows)
    cur["last_read_at"] = time.time()
    return rows, meta, cur, anomalies


def schema_ts_future(ts, now):
    return bool(ts and ts > now + 86400)


def _read_telemetry(cfg, cursors):
    spec = cfg["observation"]
    p = abs_path(spec["telemetry_db"])
    cur = cursors.setdefault("telemetry", {})
    if cur.get("path") != p:
        cur.clear()
        cur.update({"path": p, "last_seq": 0, "events": 0})
    meta = {"path": p, "status": "absent", "new_events": 0}
    if not os.path.exists(p):
        return [], meta, cur

    uri = "file:" + quote(p.replace("\\", "/"), safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    except sqlite3.Error as e:
        meta["status"] = "unreadable"
        meta["error"] = f"{type(e).__name__}: {e}"
        return [], meta, cur

    try:
        conn.execute("SELECT 1 FROM events LIMIT 1")
    except sqlite3.Error as e:
        meta["status"] = "missing_schema"
        meta["error"] = f"{type(e).__name__}: {e}"
        conn.close()
        return [], meta, cur

    last_seq = int(cur.get("last_seq") or 0)
    limit = int(spec["max_batch_events"])
    try:
        fetched = conn.execute(
            "SELECT seq,event_id,lic_hash,machine,occurred_at,day,tier,name,"
            "price,result,skew_ms,skip_reason,client_version "
            "FROM events WHERE seq>? ORDER BY seq LIMIT ?", (last_seq, limit)
        ).fetchall()
    except sqlite3.Error as e:
        meta["status"] = "query_error"
        meta["error"] = f"{type(e).__name__}: {e}"
        conn.close()
        return [], meta, cur
    conn.close()

    cols = ["seq", "event_id", "lic_hash", "machine", "occurred_at", "day",
            "tier", "name", "price", "result", "skew_ms", "skip_reason",
            "client_version"]
    events = [dict(zip(cols, r)) for r in fetched]
    if events:
        cur["last_seq"] = max(int(e["seq"]) for e in events)
        cur["events"] = int(cur.get("events") or 0) + len(events)
    meta["status"] = "ok"
    meta["new_events"] = len(events)
    return events, meta, cur


def _event_attempt(ev, cfg):
    ts = ev.get("occurred_at")
    name = str(ev.get("name") or "")
    out = {
        "ts": float(ts),
        "day": ev.get("day") or day_str(ts),
        "name": name,
        "name_len": len(name),
        "price": ev.get("price"),
        "result": ev.get("result") or "miss",
        "mode": "telemetry",
        "quota": None,
        "verified": None,
        "verified_by": "",
        "elapsed": None,
        "shot": "",
        "tier": ev.get("tier") or _tier(name, cfg["funnel"]["tier_keywords"]),
        "skew_ms": ev.get("skew_ms"),
        "skip_reason": ev.get("skip_reason"),
        "source": "telemetry",
        "event_id": ev.get("event_id"),
        "future_ts": False,
    }
    for k in pool_contract.CARRY_FIELDS:
        out[k] = ev.get(k)
    return out


def _skip_dedupe_key(name, ts):
    return (str(name or "")[:64], int(round(float(ts or 0))))


def _merge_rollup(cfg, rollup, attempts, skips, events, now):
    cap = int(cfg["daemon"]["sample_cap"])
    days = rollup.setdefault("days", {})
    counts_by_day = {}
    for a in attempts:
        d = days.setdefault(a["day"], _empty_day())
        c = d.setdefault("counts", {})
        cls = pool_contract.classify(a)
        c["discover"] = c.get("discover", 0) + 1
        listed = pool_contract.listed_state(a)
        if listed:
            c["listed"] = c.get("listed", 0) + 1
            d["listed_measured"] = True
        # 两个观测时间戳：first_seen_ms 只用于 discover→listed 时长，
        # listed_at_ms 同时用于 listed 段计数和 listed→click 时长。
        fs = settle_verify.norm_ms(a.get("first_seen_ms"))
        la = settle_verify.norm_ms(a.get("listed_at_ms"))
        ca = settle_verify.norm_ms(a.get("click_at_ms"))
        if fs is not None:
            d["first_seen_measured"] = True
        gap_dl = settle_verify.gap_ms(fs, la)
        if gap_dl is not None:
            d.setdefault("gap_discover_listed", []).append(float(gap_dl))
            d["gap_discover_listed"] = d["gap_discover_listed"][-cap:]
        gap_lc = settle_verify.gap_ms(la, ca)
        if gap_lc is not None:
            d.setdefault("gap_listed_click", []).append(float(gap_lc))
            d["gap_listed_click"] = d["gap_listed_click"][-cap:]
        if cls["click"]:
            c["click"] = c.get("click", 0) + 1
        if cls["pool"] == "joined":
            c["pool"] = c.get("pool", 0) + 1
        if cls["unknown"]:
            d["pool_unknown"] = d.get("pool_unknown", 0) + 1
        if cls["hit"]:
            c["hit"] = c.get("hit", 0) + 1
            if cls["deal"]:
                c["deal"] = c.get("deal", 0) + 1
            else:
                d["unverified_hits"] = d.get("unverified_hits", 0) + 1
            # 结算证据分账：won/lost 是「有证据」，unknown 单列，绝不并进 lost。
            state = settle_verify.normalize(a.get("verified"))
            if state == settle_verify.WON:
                d["deal_evidence"] = d.get("deal_evidence", 0) + 1
            elif state == settle_verify.LOST:
                d["deal_evidence"] = d.get("deal_evidence", 0) + 1
                d["settle_lost_hits"] = d.get("settle_lost_hits", 0) + 1
            else:
                d["settle_unknown_hits"] = d.get("settle_unknown_hits", 0) + 1
        if cls["blocked"]:
            d["blocked"] = d.get("blocked", 0) + 1
        if a["elapsed"] is not None:
            d.setdefault("elapsed", []).append(float(a["elapsed"]))
            d["elapsed"] = d["elapsed"][-cap:]
        mode = a["mode"] or "unknown"
        mday = d.setdefault("by_mode", {}).setdefault(mode, _empty_day())
        mc = mday.setdefault("counts", {})
        mc["discover"] = mc.get("discover", 0) + 1
        if listed:
            mc["listed"] = mc.get("listed", 0) + 1
        if cls["click"]:
            mc["click"] = mc.get("click", 0) + 1
        if cls["pool"] == "joined":
            mc["pool"] = mc.get("pool", 0) + 1
        if cls["hit"]:
            mc["hit"] = mc.get("hit", 0) + 1
            if cls["deal"]:
                mc["deal"] = mc.get("deal", 0) + 1
        counts_by_day[a["day"]] = counts_by_day.get(a["day"], 0) + 1

    for s in skips:
        d = days.setdefault(s["day"], _empty_day())
        c = d.setdefault("counts", {})
        c["discover"] = c.get("discover", 0) + 1
        d["skip"] = d.get("skip", 0) + 1
        reason = str(s.get("skip_reason") or "unlabeled")
        d.setdefault("skip_reasons", {})
        d["skip_reasons"][reason] = d["skip_reasons"].get(reason, 0) + 1

    for ev in events:
        ts = ev.get("occurred_at")
        if not isinstance(ts, (int, float)):
            continue
        tday = ev.get("day") or day_str(ts)
        d = days.setdefault(tday, _empty_day())
        if isinstance(ev.get("skew_ms"), (int, float)):
            d.setdefault("skew", []).append(float(ev["skew_ms"]))
            d["skew"] = d["skew"][-cap:]

    rollup["version"] = 1
    rollup["updated_at"] = now
    return rollup


def _empty_day():
    return {"counts": {}, "blocked": 0, "skip": 0, "unverified_hits": 0,
            "pool_unknown": 0, "listed_measured": False,
            "first_seen_measured": False,
            "deal_evidence": 0, "settle_unknown_hits": 0,
            "settle_lost_hits": 0,
            "gap_discover_listed": [], "gap_listed_click": [],
            "elapsed": [], "skew": [], "by_mode": {}, "skip_reasons": {}}


REPLAY_KEYS = ("ts", "day", "name", "result", "mode", "quota", "verified",
               "verified_by", "elapsed", "tier", "skew_ms", "skip_reason",
               "attempt_id", "join_result", "pool_entered", "roll_self",
               "roll_max", "won", "listed_at_ms", "first_seen_ms",
               "click_at_ms", "no_evidence")


def _append_replay(cfg, attempts):
    sp = os.path.join(state_dir(cfg), "replay.jsonl")
    ensure_dir(os.path.dirname(sp))
    cap = int(cfg["daemon"]["replay_buffer_max"])
    keep = []
    if os.path.exists(sp):
        keep = []
        with open(sp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    keep.append(line)
    with open(sp, "a", encoding="utf-8") as f:
        for a in attempts:
            slim = {k: a.get(k) for k in REPLAY_KEYS}
            f.write(json.dumps(slim, ensure_ascii=False) + "\n")
    if len(keep) + len(attempts) > cap:
        all_lines = keep + [json.dumps({k: a.get(k) for k in REPLAY_KEYS},
                                       ensure_ascii=False) for a in attempts]
        with open(sp, "w", encoding="utf-8") as f:
            f.write("\n".join(all_lines[-cap:]) + "\n")


def observe(cfg):
    st = state_dir(cfg)
    ensure_dir(st)
    now = time.time()
    cursors = load_json(os.path.join(st, "cursors.json"), {})
    rollup = load_json(os.path.join(st, "rollup.json"), {"version": 1, "days": {}})

    ledger_rows, lmeta, lcur, anomalies = _read_ledger(cfg, cursors, now)
    events, tmeta, tcur = _read_telemetry(cfg, cursors)

    attempts = [_norm_ledger(r, cfg, now) for r in ledger_rows]
    telemetry_events = events

    if lmeta["status"] == "absent" and events:
        attempts = [_event_attempt(e, cfg) for e in events
                    if e.get("result") in CLICK_RESULTS]
        skips = [_event_attempt(e, cfg) for e in events
                 if e.get("result") == "skip"]
    else:
        seen = {_skip_dedupe_key(a["name"], a["ts"]) for a in attempts}
        skips = []
        for e in events:
            if e.get("result") != "skip":
                continue
            key = _skip_dedupe_key(e.get("name"), e.get("occurred_at"))
            if key in seen:
                continue
            skips.append(_event_attempt(e, cfg))

    inbox = pool_inbox.attach(attempts, cfg)
    rollup = _merge_rollup(cfg, rollup, attempts, skips, telemetry_events, now)
    atomic_write_json(os.path.join(st, "rollup.json"), rollup)
    atomic_write_json(os.path.join(st, "cursors.json"), cursors)
    if attempts or skips:
        _append_replay(cfg, attempts + skips)

    invalid = lmeta["invalid_rows"]
    suspicious = lmeta.get("suspicious_rows", 0)
    total_seen = len(ledger_rows) + invalid
    health = {
        "ledger": lmeta["status"],
        "telemetry": tmeta["status"],
        "rows_read": len(ledger_rows),
        "invalid_rows": invalid,
        "suspicious_rows": suspicious,
        "health_ratio": safe_ratio(len(ledger_rows), total_seen),
        "anomaly_ratio": safe_ratio(suspicious, max(1, total_seen)),
        "anomalies": anomalies[:12],
        "anomaly_count": len(anomalies),
        "telemetry_error": tmeta.get("error"),
        "replay_rows": len(attempts),
        "pool_inbox": inbox,
        "pool_evidence_rows": sum(
            1 for a in attempts
            if pool_contract.pool_state(a) != "unknown"
            and a.get("result") in ("hit", "miss")),
        "settle_won_rows": sum(
            1 for a in attempts if settle_verify.normalize(a.get("verified")) == settle_verify.WON),
        "settle_lost_rows": sum(
            1 for a in attempts if settle_verify.normalize(a.get("verified")) == settle_verify.LOST),
        "settle_unknown_rows": sum(
            1 for a in attempts if settle_verify.normalize(a.get("verified")) == settle_verify.UNKNOWN),
        "time_marks_missing": {
            k: sum(1 for a in attempts if settle_verify.norm_ms(a.get(k)) is None)
            for k in settle_verify.TIME_FIELDS},
    }
    return {
        "attempts": attempts,
        "skips": skips,
        "telemetry_events": telemetry_events,
        "health": health,
        "meta": {"ledger": lmeta, "telemetry": tmeta},
        "rollup": rollup,
        "observed_at": now,
    }
