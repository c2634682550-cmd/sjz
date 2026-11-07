"""旁路采集器：把现有产物榨成「一次机会一行」的 evo/collect/events.jsonl。

规则（与需求逐条对应）：
  · 只读 core/、web/、tools/ 的产物，不改任何现有文件；
  · 增量游标（字节偏移 + 最后一行 sha256），台账轮转自动回到 0；
  · 幂等：event_id 由内容派生，重复跑不重复产行；
  · 坏行只影响自身：解析失败记进 rejects.jsonl，继续读下一行；
  · 时间一律毫秒整数，time_src 标明来源；本机时区 = Asia/Shanghai；
  · --since/--until + --rebuild 可按时间窗口整份重跑（确定性输出）。

用法：
    .venv\\Scripts\\python.exe evo\\collect\\collector.py                # 增量
    .venv\\Scripts\\python.exe evo\\collect\\collector.py --rebuild      # 全量重建
    ... --since "2026-11-07 08:00:00" --until "2026-11-07 09:00:00" --rebuild
    ... --with-harness        # 把 _soak_data 台架产物也当机会（synthetic=true）
    ... --poll-url http://127.0.0.1:8080 --poll-seconds 30   # 实时旁路
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_ROOT = PROJECT_ROOT
sys.path.insert(0, PROJECT_ROOT)

from evo.collect import parse, schema   # noqa: E402

OUT_EVENTS = os.path.join(HERE, "events.jsonl")
OUT_STAGES = os.path.join(HERE, "stages.jsonl")
OUT_REJECTS = os.path.join(HERE, "rejects.jsonl")
STATE_DIR = os.path.join(HERE, ".state")
INBOX_DIR = os.path.join(HERE, "inbox")
CURSORS = os.path.join(STATE_DIR, "cursors.json")
SEEN = os.path.join(STATE_DIR, "seen.json")          # shots 这类无偏移源的去重集
SNAPSHOT = os.path.join(STATE_DIR, "snapshot.json")  # 盘点快照（模板目录/目录清单）


def _log(msg, quiet=False):
    if not quiet:
        print(f"[collect] {msg}", flush=True)


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, (dict, list)) else default
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- 增量读

def read_incremental(path, cursor):
    """返回 (完整行列表[(line, line_no)], 新游标)。只吃完整行。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], cursor
    offset = int(cursor.get("offset") or 0)
    if size < offset:                       # 轮转/清空
        offset = 0
    if cursor.get("path") and cursor.get("path") != path:
        offset = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            buf = f.read(max(0, size - offset))
    except OSError:
        return [], cursor
    cut = buf.rfind(b"\n")
    if cut < 0:
        return [], cursor
    chunk = buf[:cut + 1]
    base_line = int(cursor.get("line_no") or 0)
    rows = []
    all_lines = chunk.split(b"\n")[:-1]
    for i, raw in enumerate(all_lines):
        if raw.strip():
            rows.append((raw, base_line + i + 1))
    new_offset = offset + cut + 1
    last = all_lines[-1] if all_lines else b""
    return rows, {"path": path, "offset": new_offset, "line_no": base_line + len(all_lines),
                  "size": size,
                  "last_line_sha256": hashlib.sha256(last).hexdigest()}


def read_all(path):
    """全量读（--rebuild）：[(raw, line_no)]。"""
    rows = []
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f, 1):
                if raw.strip():
                    rows.append((raw, i))
    except OSError:
        pass
    return rows


# ---------------------------------------------------------------- 源清单

def ledger_paths(root):
    base = os.path.join(root, "data", "records.jsonl")
    out = [base] if os.path.isfile(base) else []
    out += sorted(glob.glob(base + ".*"), key=lambda p: os.path.getmtime(p)
                  if os.path.exists(p) else 0, reverse=True)
    return [p for p in out if os.path.isfile(p)]


def harness_cycle_paths(root):
    return sorted(glob.glob(os.path.join(root, "_soak_data", "*", "cycles.jsonl")))


def harness_ledger_paths(root):
    return sorted(glob.glob(os.path.join(root, "_soak_data", "*", "data", "records.jsonl")))


def harness_log_paths(root):
    return sorted(glob.glob(os.path.join(root, "_soak_data", "*", "soak.log")))


def shot_paths(root):
    d = os.path.join(root, "data", "shots")
    if not os.path.isdir(d):
        return []
    return sorted(glob.glob(os.path.join(d, "*.jpg")) + glob.glob(os.path.join(d, "*.jpeg"))
                  + glob.glob(os.path.join(d, "*.png")))


def inbox_paths():
    """运行时 toast 文本旁路：evo/collect/inbox/*.jsonl（本项目自有目录）。"""
    return sorted(glob.glob(os.path.join(INBOX_DIR, "*.jsonl")))


# ---------------------------------------------------------------- 解析装配

def _rel(root, p):
    try:
        return os.path.relpath(p, root).replace("\\", "/")
    except ValueError:
        return p


def collect_rows(root, cursors, seen, rebuild, since_ms, until_ms, with_harness,
                 rejects, quiet=False):
    """读所有源 → (events, stages)。增量或全量由 rebuild 决定。"""
    events, stages = [], []

    def in_window(ts):
        if since_ms is not None and (ts is None or ts < since_ms):
            return False
        if until_ms is not None and (ts is None or ts > until_ms):
            return False
        return True

    def feed(path, parser, source_kind):
        rel = _rel(root, path)
        if rebuild:
            rows, cur = read_all(path), None
        else:
            rows, cur = read_incremental(path, cursors.get(path, {}))
            cursors[path] = cur
        for raw, line_no in rows:
            try:
                ev = parser(raw, rel, line_no)
            except Exception as e:                        # noqa: BLE001 坏行只影响自身
                rejects.append({"source_path": rel, "line": line_no,
                                "error": f"{type(e).__name__}: {e}",
                                "raw": raw[:300].decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)[:300],
                                "at": round(time.time(), 3)})
                continue
            if ev is None:
                continue
            if not in_window(ev.get("ts_ms")):
                continue
            if ev.get("kind") == "stage":
                stages.append(ev)
            else:
                events.append(ev)

    for p in ledger_paths(root):
        feed(p, lambda raw, rel, ln: parse.parse_ledger_line(raw, rel, ln), "records")
    for p in shot_paths(root):
        if not rebuild:
            if p in seen:
                continue
            seen[p] = True
        ev = parse.parse_shot_file(p, _rel(root, p))
        if ev and in_window(ev.get("ts_ms")):
            events.append(ev)
    if with_harness:
        for p in harness_cycle_paths(root):
            feed(p, lambda raw, rel, ln: parse.parse_cycle_line(raw, rel, ln), "soak_cycle")
        for p in harness_ledger_paths(root):
            feed(p, lambda raw, rel, ln: parse.parse_ledger_line(
                raw, rel, ln, synthetic=True, source="soak_ledger"), "soak_ledger")
        for p in harness_log_paths(root):
            feed(p, lambda raw, rel, ln: parse.parse_log_line(
                raw.decode("utf-8", "replace"), rel, ln)[1], "soak_log")
    # 运行时 toast 文本（OCR/日志转写）→ 段观测，S3/S4 唯一旁路入口
    for p in inbox_paths():
        feed(p, lambda raw, rel, ln: parse.parse_toast_line(raw, rel, ln), "toast_ocr")
    return events, stages


# ---------------------------------------------------------------- 挂段

def _stage_time_field(stage):
    return {"discover": "first_seen_ms", "click": "click_at_ms",
            "pool": "pool_joined_at_ms", "draw": "roll_at_ms",
            "deal": "deal_at_ms"}.get(stage)


def attach_stages(events, stages):
    """把单段观测挂到最近的机会上（同名优先，其次 0~180s 时序邻近）。

    只填空字段，不覆盖台账已有值；挂不上就留在 stages.jsonl 里，不伪造机会。
    """
    by_name = {}
    for ev in events:
        if ev.get("name"):
            by_name.setdefault(ev["name"], []).append(ev)
    for st in stages:
        fld = _stage_time_field(st["stage"])
        if not fld:
            continue
        cands = by_name.get(st.get("name") or "", []) or events
        best, best_d = None, None
        for ev in cands:
            base = ev.get("ts_ms")
            if base is None:
                continue
            dt = st["ts_ms"] - base
            if not (-5000 <= dt <= 180000):
                continue
            d = abs(dt)
            if best is None or d < best_d:
                best, best_d = ev, d
        if best is None:
            continue
        if best.get("name") is None and st.get("name"):
            best["name"] = st["name"]
        if best.get(fld) is None:
            best[fld] = st["ts_ms"]
            best.setdefault("time_src", {})[fld] = st.get("time_src") or "log_text_local"
        for k in ("lead_ms", "lead_err_ms", "roll_self", "roll_max", "won",
                  "join_result", "join_raw", "toast_source", "confirm_clicks",
                  "pool_entered", "queue_pos", "queue_capacity",
                  "forward_remaining_s"):
            if st.get(k) is not None and best.get(k) is None:
                best[k] = st[k]
                best.setdefault("time_src", {})[k] = st.get("time_src") or "log_text_local"
        if st.get("attempt_id") and not best.get("attempt_id"):
            best["attempt_id"] = st["attempt_id"]
        best["stage_evidence"] = list(best.get("stage_evidence") or []) + [
            f"{st.get('source')}:{st['stage']}"]
    for ev in events:
        derive_event(ev)


def derive_event(ev):
    """由已填的时间/结果推派生量与「到第几段」。全 null 不补 0。"""
    t = ev.get("click_at_ms"), ev.get("pool_joined_at_ms"), ev.get("roll_at_ms"), ev.get("deal_at_ms")
    stage = int(ev.get("stage_reached") or 0)
    if ev.get("first_seen_ms") is not None:
        stage = max(stage, 1)
    if t[0] is not None:
        stage = max(stage, 2)
    if t[1] is not None:
        stage = max(stage, 3)
    if t[2] is not None:
        stage = max(stage, 4)
    if t[3] is not None:
        stage = max(stage, 5)
    ev["stage_reached"] = stage
    if t[1] is not None and t[0] is not None:
        ev["queue_wait_ms"] = t[1] - t[0]
        ev.setdefault("time_src", {})["queue_wait_ms"] = "derived"
    if t[2] is not None and t[1] is not None:
        ev["pool_wait_ms"] = t[2] - t[1]
        ev.setdefault("time_src", {})["pool_wait_ms"] = "derived"
    # 抽签点数不足 = 未中签（与「成交」独立，成交仍需 verified）
    if ev.get("roll_self") is not None and ev.get("roll_max") is not None:
        ev["won"] = ev["roll_self"] >= ev["roll_max"]
    # 进池结果与 pool_entered 保持一致（joined→True，queue_full→False）
    if ev.get("join_result") == "joined":
        ev["pool_entered"] = True
    elif ev.get("join_result") in ("queue_full", "rejected_pool"):
        ev["pool_entered"] = False
    # S1 半可解析：用「首次看到 + 显示剩余秒」反推挂牌时刻
    # 误差 = 扫描延迟 1.4–2.2 s + 显示截断 1 s，只能当下界用
    if ev.get("listed_at_ms") is None and ev.get("first_seen_ms") is not None \
            and ev.get("forward_remaining_s") is not None:
        ev["listed_at_ms"] = int(ev["first_seen_ms"]
                                 - float(ev["forward_remaining_s"]) * 1000)
        ev.setdefault("time_src", {})["listed_at_ms"] = "derived_countdown"
    if ev.get("listed_at_ms") is not None and ev.get("unlock_at_ms") is not None:
        ev["publicity_s"] = round((ev["unlock_at_ms"] - ev["listed_at_ms"]) / 1000, 3)
        ev.setdefault("time_src", {})["publicity_s"] = "derived"
    # 台账 ts 存在时不再标 absent（覆盖上面的默认）
    if ev.get("ts_ms") is not None:
        ev.setdefault("time_src", {})["ts_ms"] = ev["time_src"].get("ts_ms") or "record_epoch"


# ---------------------------------------------------------------- 落盘

def _ids(path, key="event_id"):
    out = set()
    try:
        with open(path, "rb") as f:
            for raw in f:
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get(key):
                    out.add(o[key])
    except OSError:
        pass
    return out


def _dedupe(rows, key):
    """按 id 去重；后出现的（刚解析的）覆盖先出现的（旧文件）。"""
    out = {}
    for r in rows:
        if r.get(key):
            out[r[key]] = r
    return list(out.values())


def write_outputs(events, stages, rebuild, quiet=False, new_e=0, new_s=0):
    """确定性整份原子重写（排序固定），重复跑字节级一致。"""
    events.sort(key=lambda e: (e.get("ts_ms") is None, e.get("ts_ms") or 0, e["event_id"]))
    stages.sort(key=lambda s: (s.get("ts_ms") is None, s.get("ts_ms") or 0, s["stage_id"]))
    _atomic_write(OUT_EVENTS, b"".join(
        (json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for e in events))
    _atomic_write(OUT_STAGES, b"".join(
        (json.dumps(s, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        for s in stages))
    tag = "重建" if rebuild else "增量"
    _log(f"{tag}完成：共 {len(events)} 条机会（新增 {new_e}）/ "
         f"{len(stages)} 条段观测（新增 {new_s}）", quiet)


def append_rejects(rejects):
    if not rejects:
        return
    with open(OUT_REJECTS, "a", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 实时旁路

def poll_api(url, seconds, interval, quiet=False):
    """只读 GET /api/scheduler，把 engine.log 抓成时间精确的段观测。"""
    stages, seen = [], _ids(OUT_STAGES)
    deadline = time.time() + max(0, seconds)
    while True:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/scheduler",
                                        timeout=2.0) as resp:
                doc = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception as e:                        # noqa: BLE001
            _log(f"接口不可用（{type(e).__name__}），本轮跳过", quiet)
            doc = {}
        eng = (doc.get("engine") or {}) if isinstance(doc, dict) else {}
        for item in (eng.get("log") or []):
            st = parse.parse_api_log(item)
            if st and st["stage_id"] not in seen:
                seen.add(st["stage_id"])
                stages.append(st)
        if time.time() >= deadline:
            break
        time.sleep(max(0.05, interval))
    return stages


# ---------------------------------------------------------------- 主

def run(argv=None):
    ap = argparse.ArgumentParser(description="旁路数据补采")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--rebuild", action="store_true", help="全量重算（可用 --since/--until 开窗）")
    ap.add_argument("--since", default=None, help="窗口起点 'YYYY-MM-DD HH:MM:SS' 或 epoch")
    ap.add_argument("--until", default=None)
    ap.add_argument("--with-harness", action="store_true",
                    help="纳入 _soak_data 仿真台架（synthetic=true）")
    ap.add_argument("--poll-url", default=None, help="如 http://127.0.0.1:8080")
    ap.add_argument("--poll-seconds", type=float, default=0)
    ap.add_argument("--poll-interval", type=float, default=0.3)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--watch", action="store_true",
                    help="冷启动常驻：每 N 秒增量采一次，Ctrl-C 结束")
    ap.add_argument("--watch-interval", type=float, default=2.0)
    args = ap.parse_args(argv)

    if not args.watch:
        return _run_once(args)
    _log("watch 模式：增量采集 + 接口轮询，Ctrl-C 结束", args.quiet)
    first = True
    while True:
        step = argparse.Namespace(**vars(args))
        step.rebuild = args.rebuild and first
        poll = step.poll_seconds
        step.poll_seconds = poll if poll > 0 else step.watch_interval
        _run_once(step)
        first = False
        time.sleep(max(0.5, args.watch_interval))
    return 0


def _run_once(args):
    root = os.path.abspath(args.root)
    os.makedirs(STATE_DIR, exist_ok=True)
    cursors = _load_json(CURSORS, {})
    seen = _load_json(SEEN, {})
    since_ms = parse.parse_window_ms(args.since)
    until_ms = parse.parse_window_ms(args.until)
    if args.rebuild and since_ms is None and until_ms is None:
        _log("全量重建（不加窗口）", args.quiet)

    rejects = []
    events, stages = collect_rows(root, cursors, seen, args.rebuild,
                                  since_ms, until_ms, args.with_harness,
                                  rejects, args.quiet)

    if args.poll_url:
        stages += poll_api(args.poll_url, args.poll_seconds, args.poll_interval,
                           args.quiet)

    # 段观测与机会合并去重后整份原子重写（读是增量的，写是确定性的）
    stages_all = _dedupe(stages + ([] if args.rebuild else _read_jsonl(OUT_STAGES)), "stage_id")
    events_all = _dedupe(events + ([] if args.rebuild else _read_jsonl(OUT_EVENTS)), "event_id")

    attach_stages(events_all, stages_all)
    prev_e, prev_s = len(_read_jsonl(OUT_EVENTS)), len(_read_jsonl(OUT_STAGES))
    write_outputs(events_all, stages_all, args.rebuild, args.quiet,
                  new_e=len(events_all) - prev_e, new_s=len(stages_all) - prev_s)
    append_rejects(rejects)

    # 游标/去重集只在成功落盘后推进（先写数据再推游标，崩了只会重读）
    _atomic_write(CURSORS, json.dumps(cursors, ensure_ascii=False).encode("utf-8"))
    _atomic_write(SEEN, json.dumps(seen, ensure_ascii=False).encode("utf-8"))
    snapshot = {"template_dirs": parse.scan_template_dirs(root),
                "shots_dir_exists": os.path.isdir(os.path.join(root, "data", "shots")),
                "telemetry_dir_exists": os.path.isdir(os.path.join(root, "data", "telemetry")),
                "at": round(time.time(), 3)}
    _atomic_write(SNAPSHOT, json.dumps(snapshot, ensure_ascii=False, indent=1).encode("utf-8"))
    if rejects:
        _log(f"{len(rejects)} 行坏数据只跳过自身，已记 rejects.jsonl", args.quiet)
    return 0


def _read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        pass
    return out


if __name__ == "__main__":
    sys.exit(run())
