"""各数据落点的按行解析器。纯标准库；只读文件，不碰引擎。

每个解析器产出 (events, stages)：
  events —— 一次「机会」一行（五段漏斗的容器）
  stages —— 单段观测（来自日志/接口的文本信号），可后挂到某条 event 上
"""
from __future__ import annotations

import hashlib
import os
import re
import time

from . import schema

TZ = "Asia/Shanghai"

# 台账 result → 归一三态（与 tools/telemetry_agent.py 同一口径）
RESULT_MAP = {
    "hit": "hit", "ok": "hit", "success": "hit",
    "miss": "miss", "no_money": "miss", "over_budget": "miss",
    "aborted": "miss", "fail": "miss", "failed": "miss",
    "skip": "skip", "skipped": "skip", "late_skip": "skip",
}

_CRAFT_RE = re.compile("(极品|优品)")
_TIER_HINT = re.compile("(传说|史诗|稀有|普通)")


# ---------------------------------------------------------------- 时间

def epoch_ms(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not (0 < f < 1e11):
        return None
    return int(round(f * 1000))


def text_ms(s, ref_year=None):
    """'YYYY-MM-DD HH:MM:SS' / 'MM-DD HH:MM:SS' → 本地毫秒。

    年份缺失时用台账锚点年份（跨年按「未来不超过 1 天」回退一年）。
    """
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m-%d %H:%M:%S"):
        try:
            t = time.strptime(s, fmt)
        except ValueError:
            continue
        year = t.tm_year if fmt.startswith("%Y") else (ref_year or time.localtime().tm_year)
        ts = time.mktime((year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min,
                          t.tm_sec, 0, 0, -1))
        if fmt.startswith("%m") and ref_year and ts > time.time() + 86400:
            ts = time.mktime((year - 1, t.tm_mon, t.tm_mday, t.tm_hour,
                              t.tm_min, t.tm_sec, 0, 0, -1))
        return int(round(ts * 1000))
    return None


def parse_window_ms(s):
    """'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' / epoch 秒 或毫秒 → 毫秒。"""
    if s is None or str(s).strip() == "":
        return None
    s = str(s).strip()
    try:
        f = float(s)
        return int(round(f * 1000)) if f < 1e11 else int(round(f))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            t = time.strptime(s, fmt)
        except ValueError:
            continue
        return int(round(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour,
                                      t.tm_min, t.tm_sec, 0, 0, -1)) * 1000))
    return None


def mtime_ms(path):
    try:
        return int(round(os.path.getmtime(path) * 1000))
    except OSError:
        return None


def stable_id(*parts):
    raw = "|".join("" if p is None else str(p) for p in parts)
    return "e" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------- 台账

def parse_ledger_line(raw, source_path, line_no, synthetic=False, source="records"):
    """一行台账 → event。坏行抛 ValueError（由调用方只跳过这一行）。"""
    import json
    row = json.loads(raw)
    if not isinstance(row, dict):
        raise ValueError("不是对象")
    ts_ms = epoch_ms(row.get("ts"))
    if ts_ms is None:
        raise ValueError("ts 非法")

    ev = schema.blank_event()
    ev.update(kind="opportunity", source=source, source_path=source_path,
              source_line=line_no, synthetic=synthetic, tz=TZ,
              trust="synthetic" if synthetic else "claimed")

    name = str(row.get("name") or "") or None
    ev["name"] = name
    ev["price"] = _float_or_none(row.get("price"))
    ev["quota"] = _int_or_none(row.get("quota"))
    ev["mode"] = str(row.get("mode") or "") or None
    ev["shot"] = str(row.get("shot") or "") or None
    ev["verified"] = row.get("verified") if isinstance(row.get("verified"), bool) else None
    ev["verified_by"] = str(row.get("verified_by") or "") or None
    ev["followup_used"] = row.get("followup_used") if isinstance(row.get("followup_used"), bool) else None
    ev["stop_reason"] = str(row.get("stop_reason") or "") or None
    ev["grade"] = str(row.get("grade") or "") or None
    ev["tier"] = str(row.get("tier") or "") or (_CRAFT_RE.search(name).group(1)
                                                if name and _CRAFT_RE.search(name) else None)
    ev["grade_counts"] = row.get("grade_counts") if isinstance(row.get("grade_counts"), dict) else None
    ev["burst_clicks"] = _int_or_none(row.get("burst_clicks"))
    ev["refresh_used"] = _int_or_none(row.get("refresh_used"))
    ev["late_skips"] = _int_or_none(row.get("late_skips"))
    ev["lead_err_ms"] = _skew(row)
    ev["elapsed_ms"] = _elapsed_ms(row.get("elapsed"))
    ev["ts_ms"] = ts_ms

    raw_result = str(row.get("result") or "") or None
    ev["result_raw"] = raw_result
    ev["result_norm"] = RESULT_MAP.get((raw_result or "").lower(), "unknown")

    src = {"ts_ms": "record_epoch"}
    if ev["elapsed_ms"] is not None:
        src["elapsed_ms"] = "record_elapsed_s"
    if ev["lead_err_ms"] is not None:
        src["lead_err_ms"] = "record_extra_offset_ms"

    if ev["result_norm"] == "hit":
        ev["deal_at_ms"] = ts_ms
        src["deal_at_ms"] = "record_epoch"
        ev["stage_reached"] = 5
        ev["win"] = True
        ev["trust"] = ("verified" if ev["verified"] is True
                       else ("synthetic" if synthetic else "claimed"))
        ev["stage_evidence"] = ["ledger:hit"]
    elif ev["result_norm"] == "skip":
        ev["stage_reached"] = 2
        ev["stage_evidence"] = ["ledger:skip"]
        ev["trust"] = "inferred"
    else:
        # miss / no_money / over_budget / aborted：点过、但没拿到结算证据
        ev["stage_reached"] = 2
        ev["stage_evidence"] = [f"ledger:{raw_result}"]
        ev["trust"] = "inferred"
    ev["time_src"] = src
    ev["event_id"] = stable_id(source_path, ts_ms, name, ev["price"],
                               ev["result_raw"], ev["mode"], line_no)
    return ev


def _float_or_none(v):
    if v in (None, "", 0):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _elapsed_ms(v):
    f = _float_or_none(v)
    return None if f is None else int(round(f * 1000))


def _skew(row):
    """台账里卡点误差的四种常见写法；超区间一律当缺失（不编造）。"""
    for key in ("offset_ms", "skew_ms", "delta_ms", "error_ms", "drift_ms"):
        v = row.get(key)
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if -5000 <= f <= 5000:
            return int(round(f))
    return None


# ---------------------------------------------------------------- 成交截图

_SHOT_RE = re.compile(r"^(\d{8})_(\d{6})_(.+?)\.(jpg|jpeg|png)$", re.I)


def parse_shot_file(path, rel, synthetic=False):
    """data/shots/YYYYmmdd_HHMMSS_名称.jpg → 只带「成交」一段的机会。"""
    m = _SHOT_RE.match(os.path.basename(path))
    if not m:
        return None
    d, tm, name, _ext = m.groups()
    ts_ms = text_ms(f"{d[:4]}-{d[4:6]}-{d[6:]} {tm[:2]}:{tm[2:4]}:{tm[4:]}")
    if ts_ms is None:
        return None
    ev = schema.blank_event()
    ev.update(kind="opportunity", source="shots", source_path=rel,
              source_line=None, synthetic=synthetic, tz=TZ, trust="claimed",
              name=name, shot=os.path.basename(path), ts_ms=ts_ms,
              deal_at_ms=ts_ms, stage_reached=5,
              stage_evidence=["shot:filename"], time_src={"ts_ms": "mtime_or_name_local",
                                                          "deal_at_ms": "filename_local"})
    ev["event_id"] = stable_id(rel, ts_ms, name)
    return ev


# ---------------------------------------------------------------- 台架 cycles

def parse_cycle_line(raw, rel, line_no):
    """_soak_data/*/cycles.jsonl → 一次仿真机会。"""
    import json
    row = json.loads(raw)
    if not isinstance(row, dict):
        raise ValueError("不是对象")
    ts_ms = epoch_ms(row.get("ts"))
    if ts_ms is None:
        raise ValueError("ts 非法")
    ev = schema.blank_event()
    clicks = _int_or_none(row.get("clicks")) or 0
    bought = row.get("bought") if isinstance(row.get("bought"), bool) else None
    roll = row.get("hit_roll") if isinstance(row.get("hit_roll"), bool) else None
    state = str(row.get("state") or "") or None
    dur_ms = None
    try:
        dur_ms = int(round(float(row.get("duration_s") or 0) * 1000)) or None
    except (TypeError, ValueError):
        pass
    ev.update(kind="opportunity", source="soak_cycle", source_path=rel,
              source_line=line_no, synthetic=True, tz=TZ, trust="synthetic",
              ts_ms=ts_ms, first_seen_ms=ts_ms, click_count=clicks,
              lead_err_ms=_skew(row), elapsed_ms=dur_ms,
              result_raw=state, result_norm=("hit" if bought else "miss"),
              win=roll, error=str(row.get("error") or "") or None)
    ev["time_src"] = {"ts_ms": "cycle_epoch", "first_seen_ms": "cycle_epoch",
                      "elapsed_ms": "cycle_duration_s"}
    src_ev = ["cycle:start"]
    stage = 1
    if clicks > 0:
        stage = 2
        src_ev.append("cycle:clicks>0")
    if roll:
        stage = 4
        src_ev.append("cycle:hit_roll")
    if bought:
        stage = 5
        src_ev.append("cycle:bought")
    ev["stage_reached"] = stage
    ev["stage_evidence"] = src_ev
    seq = row.get("seq")
    ev["event_id"] = stable_id(rel, ts_ms, seq, state, clicks)
    return ev


# ---------------------------------------------------------------- 日志

_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(\w+)\]\s+(\w+):\s*(.*)$")

_RE_DISCOVER = re.compile(r"第\s*(\d+)\s*件：[·\s]*(.*?)\s*·")
_RE_LEAD = re.compile(r"开点（距归零还有\s*(-?\d+)\s*ms）")
_RE_DEAL = re.compile(r"成交（.*?），点了\s*(\d+)\s*下，用时\s*([\d.]+)\s*s")
_RE_JOIN = re.compile(r"已加入抢购池|加入抢购池|已进入抢购池")
_RE_FULL = re.compile(r"抢购池已满|抢购池满|排队已满|人数过多|排队人数已满")
_RE_QUEUE = re.compile(r"队列\s*(\d+)\s*/\s*(\d+)")
_RE_DRAW = re.compile(r"随机点数[^\d]{0,6}(\d{1,4})")
_RE_DRAWMAX = re.compile(r"(?:本轮|最大)点数[^\d]{0,4}(\d{1,4})|最大点数为\s*(\d+)")


def parse_log_line(line, rel, line_no, ref_year=None):
    """引擎日志文本 → (event|null, stage|null)。

    只有能落到具体段的行才产出 stage；其余返回 (None, None)。
    """
    m = _LOG_RE.match(line.rstrip())
    if not m:
        return None, None
    t_text, _chan, _level, msg = m.groups()
    ts_ms = text_ms(t_text, ref_year=ref_year)
    if ts_ms is None:
        return None, None

    stage = None
    fields = {}
    if _RE_DISCOVER.search(msg):
        dm = _RE_DISCOVER.search(msg)
        stage = "discover"
        fields = {"name": (dm.group(2) or "").strip() or None}
    elif _RE_LEAD.search(msg):
        stage = "click"
        fields = {"lead_ms": int(_RE_LEAD.search(msg).group(1))}
    elif _RE_DEAL.search(msg):
        dm = _RE_DEAL.search(msg)
        stage = "deal"
        fields = {"confirm_clicks": int(dm.group(1)),
                  "elapsed_ms": int(round(float(dm.group(2)) * 1000))}
    elif _RE_FULL.search(msg):
        stage = "pool"
        fields = {"join_result": "queue_full", "pool_entered": False,
                  "join_raw": msg[:120], "toast_source": "log"}
        qm = _RE_QUEUE.search(msg)
        if qm and qm.lastindex:
            fields.update(queue_pos=int(qm.group(1)),
                          queue_capacity=int(qm.group(2)))
    elif _RE_JOIN.search(msg):
        stage = "pool"
        fields = {"join_result": "joined", "pool_entered": True,
                  "join_raw": msg[:120], "toast_source": "log"}
    elif _RE_DRAW.search(msg):
        stage = "draw"
        fields = {"roll_self": int(_RE_DRAW.search(msg).group(1)),
                  "toast_source": "log"}
        mm = _RE_DRAWMAX.search(msg)
        if mm:
            roll_max = int(mm.group(1) or mm.group(2))
            fields["roll_max"] = roll_max
            # 「未抢购成功…随机点数 99 / 本轮最大点数 125」= 点数不足，未中签
            fields["won"] = fields["roll_self"] >= roll_max
    if stage is None:
        return None, None

    st = {"kind": "stage", "stage": stage, "ts_ms": ts_ms, "source": "soak_log",
          "source_path": rel, "source_line": line_no, "name": fields.get("name"),
          "msg": msg[:200], "tz": TZ, "time_src": "log_text_local"}
    st.update(fields)
    st["stage_id"] = stable_id(rel, ts_ms, stage, msg)
    return None, st


def parse_api_log(msg_obj, rel="live:/api/scheduler"):
    """engine.status()['log'] 里的一条 {t, ts, level, msg} → stage。"""
    if not isinstance(msg_obj, dict):
        return None
    msg = str(msg_obj.get("msg") or "")
    ts_ms = epoch_ms(msg_obj.get("ts"))
    if ts_ms is None:
        return None
    _ev, st = parse_log_line(
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_ms / 1000))} "
        f"[engine] info: {msg}", rel, 0)
    if st is None:
        return None
    st["ts_ms"] = ts_ms
    st["source"] = "live_api"
    st["time_src"] = "api_epoch"
    st["stage_id"] = stable_id(rel, ts_ms, st["stage"], msg)
    return st


def _mkline(ts_ms, msg):
    return (f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_ms / 1000))} "
            f"[toast] info: {msg}")


def parse_toast_text(text, ts_ms, source="toast_ocr", rel="inbox", line_no=0,
                     name=None, attempt_id=None):
    """运行时 toast 文本（OCR 或日志）→ stage 行。

    冷启动唯一能把 S3/S4 补上的旁路输入：屏幕文案原样喂进来即可。
    """
    text = (text or "").strip()
    if not text or ts_ms is None:
        return None
    _ev, st = parse_log_line(_mkline(ts_ms, text), rel, line_no)
    if st is None:
        return None
    st["source"] = source
    st["time_src"] = "ocr_epoch" if source == "toast_ocr" else "log_text_local"
    st["name"] = name or st.get("name")
    st["attempt_id"] = attempt_id
    st["msg"] = text[:200]
    st["stage_id"] = stable_id(rel, ts_ms, st["stage"], text, attempt_id)
    return st


def parse_toast_line(raw, rel, line_no):
    """evo/collect/inbox/*.jsonl 一行：{ts, text, name?, attempt_id?, source?}。"""
    import json
    row = json.loads(raw)
    if not isinstance(row, dict):
        raise ValueError("不是对象")
    text = str(row.get("text") or "")
    ts_ms = epoch_ms(row.get("ts")) or text_ms(row.get("t"))
    if not text.strip():
        raise ValueError("text 为空")
    if ts_ms is None:
        raise ValueError("ts 非法")
    st = parse_toast_text(text, ts_ms, source=str(row.get("source") or "toast_ocr"),
                          rel=rel, line_no=line_no, name=row.get("name"),
                          attempt_id=row.get("attempt_id"))
    if st is None:
        return None
    return st


# ---------------------------------------------------------------- 模板目录（仅盘点用）

def scan_template_dirs(root):
    """data/<id>/buy.png|confirm.png —— 框选模板缓存，不是会话帧。"""
    out = []
    data_dir = os.path.join(root, "data")
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for n in names:
        d = os.path.join(data_dir, n)
        if not os.path.isdir(d):
            continue
        files = [f for f in ("buy.png", "confirm.png")
                 if os.path.isfile(os.path.join(d, f))]
        if not files:
            continue
        out.append({"task_id": n, "files": files,
                    "mtime_ms": mtime_ms(os.path.join(d, files[0]))})
    return out
