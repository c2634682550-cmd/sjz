"""受控自适应调优层：数据 -> 指标 -> 建议 -> 有限度的自动改值。

设计边界（和任务书对齐，别越线）：

* 只写**已有**开关：``fire.delay`` / ``buy.skip_late.threshold_s`` /
  ``scan.interval`` / ``scan.repeat.min`` / ``scan.repeat.max``。
  不新增配置字段，不碰 ``core/config.py``、``core/switches.py`` 的边界表；
  写值一律走 ``switches.set()``，所以单值边界和 min>max 的组合校验仍然生效。
* 默认 **shadow**：只算、只记、只写审计，运行参数一个字节都不改。
  要真改必须显式 ``shadow=False``（工具层对应 ``--apply``）。
* 每次调整必须同时过 6 条护栏（见 ``GUARDS``），任何一条不过就只出建议。
* 每条调整都带一句人话解释，审计里同时落「旧值 / 新值 / 依据指标 / 样本量 /
  触发规则」，可回滚到任意上一版。

为什么提前量用 ``fire.delay``：引擎里的 ``FIRE_LEAD_S`` 是常量，不在开关表里。
``fire.delay`` 只能把开点**往后推**，所以：

* 卡点误差为负（点早了）→ 增加 ``fire.delay`` 是真正可执行的修正；
* 卡点误差为正（点晚了）→ ``fire.delay`` 只能减到 0，再往前需要改常量，
  本层不碰 ``core/engine.py``，只出建议并标注 ``blocked="constant"``。

统计口径集中在 ``summarize()``：所有指标都从它出，工具层只做 IO 和排版，
自适应层只吃同一份 summary，避免两边算法漂移。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime

from . import config as config_mod
from . import switches

# ---------------------------------------------------------------- 阈值常数

WINDOW_MS = 33.0          # 可点窗口：一帧 33.3ms，取整 33；超窗 = |误差| > 33ms
MIN_SAMPLE = 30           # 同品阶 / 同配置桶的最少尝试次数
MIN_HITS_BETWEEN = 10     # 两次调整之间至少间隔的成交次数
ALIGN_BAND_MS = 16.0      # 偏差小于半窗口不折腾
MAX_LEAD_ERR_MS = 60000.0 # 卡点误差超过 60s 视为脏数据，不参与分位数
MAX_CHANGES_PER_STEP = 1  # 一个批次最多落地一次变更，避免多变量同时动

KILL_MIN_ATTEMPTS = 30    # kill switch 观察窗口
KILL_SPAM_SHARE = 0.80    # 单一商品占窗口比例 >= 80% 判刷屏
KILL_DISORDER_RATIO = 0.10  # 升/降序对占比的最小值 >= 10% 判乱序
KILL_DISORDER_MIN = 3
KILL_TS_SUSPECT_RATIO = 0.20

GRADES = ("legend", "epic", "rare", "common", "unknown")
GRADE_LABEL = {
    "legend": "传说", "epic": "史诗", "rare": "稀有",
    "common": "普通", "unknown": "未知",
}

ATTEMPT_RESULTS = ("hit", "miss", "skip", "no_money", "over_budget", "aborted")

# ---------------------------------------------------------------- 调优对象

# soft_bounds 是自适应层自己的更窄边界：即使开关表允许，也不一次放飞。
KNOBS = {
    "fire.delay": {
        "unit": "ms", "kind": "int", "soft": (0, 1200), "min_step": 10,
        "priority": 1, "label": "开点延迟（提前量的可执行部分）",
    },
    "buy.skip_late.threshold_s": {
        "unit": "s", "kind": "int", "soft": (0, 10), "min_step": 1,
        "priority": 2, "label": "来不及跳过阈值",
    },
    "scan.interval": {
        "unit": "s", "kind": "float", "soft": (0.02, 0.5), "min_step": 0.005,
        "priority": 3, "label": "列表扫描间隔",
    },
    "scan.repeat.max": {
        "unit": "次", "kind": "int", "soft": (0, 10), "min_step": 1,
        "priority": 4, "label": "刷新次数上限",
    },
    "scan.repeat.min": {
        "unit": "次", "kind": "int", "soft": (0, 10), "min_step": 1,
        "priority": 5, "label": "刷新次数下限",
    },
}

GUARDS = (
    "G1 最小样本：同品阶 / 同配置桶 >= 30 次尝试，否则只建议不改值",
    "G2 单次幅度 <= 20%（整数/零值用开关最小可表示步长，见 _step_size）",
    "G3 硬上下界：开关表 BOUNDS 与本层 soft 边界的交集，撞界即停并记录",
    "G4 调整间隔：两次变更之间至少 MIN_HITS_BETWEEN 次成交",
    "G5 审计：每次变更写 jsonl（时间/旧值/新值/依据指标/样本量/规则）",
    "G6 kill switch：全失败/全成功/时间戳乱序/单一商品刷屏 -> 冻结",
)

# ---------------------------------------------------------------- 字段归一化

ALIASES = {
    "ts": ("ts", "time", "timestamp", "时间", "时间戳", "记录时间",
           "成交时间", "created_at"),
    "card_hash": ("card_hash", "卡密哈希", "卡密", "card", "hash",
                  "card_no", "卡号"),
    "name": ("name", "商品名", "商品", "item", "skin", "名称", "商品名称"),
    "grade": ("grade", "品阶", "品质", "quality", "tier", "稀有度"),
    "price": ("price", "成交价", "价格", "成交价格", "amount", "售价"),
    "result": ("result", "结果", "outcome", "status", "状态"),
    "lead_err_ms": ("lead_err_ms", "卡点误差毫秒", "卡点误差", "lead_error_ms",
                    "error_ms", "timing_error_ms", "误差毫秒",
                    "卡点误差秒", "lead_error_s", "error_s"),
    "refresh_count": ("refresh_count", "刷新次数", "refreshes", "scans",
                      "refresh"),
    "burst_mode": ("burst_mode", "连点模式", "burst"),
    "burst_gap_ms": ("burst_gap_ms", "点击间隔毫秒", "gap_ms",
                     "连点间隔", "点击间隔"),
    "burst_duration_ms": ("burst_duration_ms", "连点持续毫秒", "duration_ms",
                          "连点时长"),
    "reentry_delay_ms": ("reentry_delay_ms", "再进池延迟", "再进池延迟毫秒",
                         "pool_reentry_ms", "reentry_ms"),
    "client": ("client", "客户端版本", "客户端", "version",
               "client_version", "游戏版本"),
    "skip_evidence": ("skip_evidence", "跳过证据", "事后结果", "post_outcome",
                      "confirmed", "skip_verdict"),
    "ttl_s": ("ttl_s", "公示时长", "公示秒数", "publicity_s"),
    "unlock_at": ("unlock_at", "解锁时刻", "解锁时间", "zero_at", "归零时刻"),
    "listed_at": ("listed_at", "挂单时刻", "挂单时间", "list_time"),
}

_GRADE_MAP = {
    "legend": "legend", "传说": "legend", "橙": "legend", "橙色": "legend",
    "mythic": "legend",
    "epic": "epic", "史诗": "epic", "紫": "epic", "紫色": "epic",
    "purple": "epic",
    "rare": "rare", "稀有": "rare", "蓝": "rare", "蓝色": "rare",
    "blue": "rare",
    "common": "common", "普通": "common", "白": "common", "白色": "common",
    "normal": "common", "white": "common",
}

_RESULT_MAP = {
    "hit": "hit", "success": "hit", "win": "hit", "成交": "hit",
    "成功": "hit", "抢到": "hit", "抢购成功": "hit", "中签": "hit",
    "miss": "miss", "fail": "miss", "failure": "miss", "lose": "miss",
    "失败": "miss", "未中": "miss", "没抢到": "miss", "未成交": "miss",
    "未抢购成功": "miss",
    "skip": "skip", "skipped": "skip", "跳过": "skip", "放弃": "skip",
    "no_money": "no_money", "nomoney": "no_money", "余额不足": "no_money",
    "over_budget": "over_budget", "超预算": "over_budget",
    "预算超限": "over_budget",
    "aborted": "aborted", "abort": "aborted", "中止": "aborted",
    "停止": "aborted",
}

_SKIP_LATE_WORDS = ("late", "too_late", "miss", "来不及", "确认真来不及",
                    "confirmed_late", "本来也来不及", "跳对")
_SKIP_WRONG_WORDS = ("would_hit", "wouldhit", "本来来得及", "hit",
                     "跳错", "confirmed_wrong")


def _norm_key(key):
    return re.sub(r"[ _\-\t]", "", str(key).strip().lower())


_NORM_ALIASES = {}
for _field, _names in ALIASES.items():
    for _name in _names:
        _NORM_ALIASES[_norm_key(_name)] = _field


def _to_float(value):
    """字符串/数字 -> 有限 float；其余返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            num = float(text)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(num):
        return None
    return num


def parse_ts(value):
    """时间 -> epoch 秒。接受 epoch 秒/毫秒、ISO、常见中文日期格式。"""
    if value is None or isinstance(value, bool):
        return None
    num = _to_float(value)
    if num is not None:
        if num <= 0:
            return None
        # 1e11 以上按毫秒：1.7e12 是 2023 年的毫秒值，1.7e9 才是秒。
        if num > 1e11:
            num = num / 1000.0
        return num
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%m-%d %H:%M:%S", "%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=datetime.now().year)
        return dt.timestamp()
    return None


def norm_grade(value):
    if value is None:
        return "unknown"
    return _GRADE_MAP.get(_norm_key(value), "unknown")


def norm_result(value):
    if value is None:
        return None
    key = _norm_key(value)
    if key in ("", "unknown", "未知", "none", "null"):
        return None
    return _RESULT_MAP.get(key)


def _lead_scale(original_key):
    """卡点误差字段的单位：毫秒优先，秒 -> *1000。"""
    key = _norm_key(original_key)
    if "毫秒" in key or "ms" in key:
        return 1.0
    if "秒" in key or key.endswith("s"):
        return 1000.0
    return 1.0


def normalize_row(raw, source=""):
    """任意一行（jsonl / csv / 后台导出）-> 统一字段。缺字段留 None。"""
    if not isinstance(raw, dict):
        return None
    found = {}
    for key, value in raw.items():
        field = _NORM_ALIASES.get(_norm_key(key))
        if field and field not in found:
            found[field] = (key, value)

    def pick(field):
        return found.get(field, (None, None))

    present = sorted(found)

    ts = parse_ts(pick("ts")[1])
    result = norm_result(pick("result")[1])
    grade = norm_grade(pick("grade")[1])
    name = pick("name")[1]
    name = str(name).strip() if name not in (None, "") else None
    card = pick("card_hash")[1]
    card = str(card).strip() if card not in (None, "") else None

    price = _to_float(pick("price")[1])
    if price is not None and abs(price) > 1e9:
        price = None

    err_key, err_raw = pick("lead_err_ms")
    err = _to_float(err_raw)
    if err is not None:
        err *= _lead_scale(err_key or "")
        if abs(err) > MAX_LEAD_ERR_MS:
            err = None

    refresh = _to_float(pick("refresh_count")[1])
    if refresh is not None:
        refresh = int(refresh) if abs(refresh) < 1e6 else None

    reentry = _to_float(pick("reentry_delay_ms")[1])
    gap = _to_float(pick("burst_gap_ms")[1])
    duration = _to_float(pick("burst_duration_ms")[1])
    mode = pick("burst_mode")[1]
    client = pick("client")[1]
    evidence = pick("skip_evidence")[1]

    return {
        "ts": ts,
        "card_hash": card,
        "name": name,
        "grade": grade,
        "price": price,
        "result": result,
        "lead_err_ms": err,
        "refresh_count": refresh,
        "burst_mode": str(mode).strip() if mode not in (None, "") else None,
        "burst_gap_ms": gap,
        "burst_duration_ms": duration,
        "reentry_delay_ms": reentry,
        "client": str(client).strip() if client not in (None, "") else None,
        "skip_evidence": evidence,
        "source": source,
        "_present": present,
    }


# ---------------------------------------------------------------- 基础统计


def quantile(values, q):
    """线性插值分位数；空序列返回 None。"""
    if not values:
        return None
    data = sorted(float(v) for v in values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def median(values):
    return quantile(values, 0.5)


def wilson(hits, n, z=1.96):
    """二项比例的 Wilson 95% 区间。"""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _dist(values, window_ms=WINDOW_MS):
    if not values:
        return {
            "n": 0, "mean": None, "p50": None, "p90": None, "p95": None,
            "out_of_window": 0, "out_of_window_ratio": None,
            "late_off": 0, "early_off": 0,
        }
    vals = [float(v) for v in values]
    out = sum(1 for v in vals if abs(v) > window_ms)
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 2),
        "p50": round(quantile(vals, 0.50), 2),
        "p90": round(quantile(vals, 0.90), 2),
        "p95": round(quantile(vals, 0.95), 2),
        "out_of_window": out,
        "out_of_window_ratio": round(out / len(vals), 4),
        "late_off": sum(1 for v in vals if v > window_ms),
        "early_off": sum(1 for v in vals if v < -window_ms),
    }


def _hour_of(ts):
    if ts is None:
        return None
    try:
        return time.localtime(ts).tm_hour
    except (OSError, OverflowError, ValueError):
        return None


def _dedupe(rows):
    seen = set()
    kept, duplicates = [], 0
    for row in rows:
        if row["ts"] is None:
            kept.append(row)
            continue
        sig = (
            round(row["ts"], 3), row["card_hash"], row["name"], row["grade"],
            row["result"], None if row["lead_err_ms"] is None
            else round(row["lead_err_ms"], 1),
        )
        if sig in seen:
            duplicates += 1
            continue
        seen.add(sig)
        kept.append(row)
    return kept, duplicates


def _attempt(row):
    return row["result"] in ATTEMPT_RESULTS


def _group_stats(rows, window_ms):
    attempts = [r for r in rows if _attempt(r)]
    hits = sum(1 for r in attempts if r["result"] == "hit")
    misses = sum(1 for r in attempts if r["result"] == "miss")
    skips = sum(1 for r in attempts if r["result"] == "skip")
    others = len(attempts) - hits - misses - skips
    n = len(attempts)
    # 卡点误差只统计真的点了的尝试：跳过/余额不足没有点击时序。
    lead = [r["lead_err_ms"] for r in attempts
            if r["result"] in ("hit", "miss") and r["lead_err_ms"] is not None]
    ci = wilson(hits, n)
    return {
        "attempts": n,
        "hits": hits,
        "misses": misses,
        "skips": skips,
        "others": others,
        "hit_rate": round(hits / n, 4) if n else None,
        "miss_rate": round(misses / n, 4) if n else None,
        "skip_rate": round(skips / n, 4) if n else None,
        "hit_rate_ci95": [round(x, 4) for x in ci] if ci else None,
        "lead": _dist(lead, window_ms),
        "insufficient": n < MIN_SAMPLE,
    }


def _time_order(rows):
    ts = [r["ts"] for r in rows if r["ts"] is not None]
    if len(ts) < 3:
        return {"direction": "unknown", "pairs": 0, "mixed_ratio": 0.0,
                "disordered": False}
    asc = desc = 0
    for prev, cur in zip(ts, ts[1:]):
        if cur > prev:
            asc += 1
        elif cur < prev:
            desc += 1
    pairs = asc + desc
    mixed = (min(asc, desc) / pairs) if pairs else 0.0
    if asc > desc * 3:
        direction = "asc"
    elif desc > asc * 3:
        direction = "desc"
    else:
        direction = "mixed"
    disordered = (pairs >= KILL_DISORDER_MIN
                  and mixed >= KILL_DISORDER_RATIO)
    return {"direction": direction, "pairs": pairs,
            "mixed_ratio": round(mixed, 4), "disordered": disordered}


def time_order_report(rows):
    """公开的乱序检查：工具层在合并多源之前逐源调用。"""
    return _time_order(rows)


def _skip_accuracy(rows):
    skipped = [r for r in rows if r["result"] == "skip"]
    late = wrong = unknown = 0
    for row in skipped:
        ev = row["skip_evidence"]
        if isinstance(ev, bool):
            late += 1 if ev else 0
            wrong += 0 if ev else 1
            continue
        text = _norm_key(ev) if ev is not None else ""
        if not text:
            unknown += 1
        elif any(w in text for w in _SKIP_LATE_WORDS):
            late += 1
        elif any(w in text for w in _SKIP_WRONG_WORDS):
            wrong += 1
        else:
            unknown += 1
    confirmed = late + wrong
    ci = wilson(late, confirmed)
    return {
        "n_skipped": len(skipped),
        "confirmed_n": confirmed,
        "confirmed_late": late,
        "confirmed_wrong": wrong,
        "unknown": len(skipped) - confirmed,
        "hit_accuracy": round(late / confirmed, 4) if confirmed else None,
        "hit_accuracy_ci95": [round(x, 4) for x in ci] if ci else None,
        "wrong_rate": round(wrong / confirmed, 4) if confirmed else None,
        "verdict": ("unknown" if confirmed == 0 else
                    "insufficient" if confirmed < MIN_SAMPLE else "ok"),
    }


def _refresh_benefit(rows):
    pairs = [(r["refresh_count"], 1 if r["result"] == "hit" else 0)
             for r in rows
             if r["result"] in ("hit", "miss")
             and r["refresh_count"] is not None]
    buckets = {}
    for x, y in pairs:
        b = buckets.setdefault(x, {"n": 0, "hits": 0})
        b["n"] += 1
        b["hits"] += y
    table = []
    for x in sorted(buckets):
        b = buckets[x]
        ci = wilson(b["hits"], b["n"])
        table.append({
            "refresh": x, "attempts": b["n"],
            "hit_rate": round(b["hits"] / b["n"], 4),
            "hit_rate_ci95": [round(c, 4) for c in ci] if ci else None,
        })
    out = {"status": "insufficient", "reason": "", "buckets": table,
           "total_n": len(pairs), "slope_per_refresh": None,
           "slope_ci95": None, "min_bucket_n": None}
    if len(buckets) < 2:
        out["reason"] = "刷新次数只有一个取值，无法估计边际收益"
        return out
    small = min(b["n"] for b in buckets.values())
    out["min_bucket_n"] = small
    if small < MIN_SAMPLE:
        out["reason"] = f"最小配置桶 n={small} < {MIN_SAMPLE}，不下结论"
        return out

    xs = [x for x in sorted(buckets)]
    ys = [buckets[x]["hits"] / buckets[x]["n"] for x in xs]
    ws = [buckets[x]["n"] for x in xs]
    slope, se = _weighted_slope(xs, ys, ws)
    if slope is None:
        out["reason"] = "刷新次数无方差（所有样本同一取值）"
        return out
    half = 1.96 * se
    out.update({
        "status": "ok",
        "slope_per_refresh": round(slope, 4),
        "slope_ci95": [round(slope - half, 4), round(slope + half, 4)],
        "reason": "正收益" if slope - half > 0 else
                  "负收益" if slope + half < 0 else "区间跨 0，不显著",
    })
    return out


def _weighted_slope(xs, ys, ws):
    total = sum(ws)
    if total <= 0 or len(xs) < 2:
        return None, None
    mean_x = sum(w * x for x, w in zip(xs, ws)) / total
    mean_y = sum(w * y for y, w in zip(ys, ws)) / total
    sxx = sum(w * (x - mean_x) ** 2 for x, w in zip(xs, ws))
    if sxx <= 0:
        return None, None
    sxy = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in zip(xs, ys, ws))
    slope = sxy / sxx
    # 残差按桶均值算，自由度用桶数减 2
    if len(xs) <= 2:
        return slope, 0.0
    resid = sum(w * (y - (mean_y + slope * (x - mean_x))) ** 2
                for x, y, w in zip(xs, ys, ws))
    dof = len(xs) - 2
    se = math.sqrt(resid / dof / sxx) if resid > 0 and dof > 0 else 0.0
    return slope, se


def _burst_benefit(rows):
    groups = {}
    for row in rows:
        if (row["result"] not in ("hit", "miss")
                or row["reentry_delay_ms"] is None):
            continue
        gap = row["burst_gap_ms"]
        dur = row["burst_duration_ms"]
        sig = (row["burst_mode"] or "unknown",
               None if gap is None else round(gap, 1),
               None if dur is None else round(dur, 1))
        groups.setdefault(sig, []).append(row["reentry_delay_ms"])
    table = []
    for (mode, gap, dur), vals in sorted(
            groups.items(), key=lambda kv: str(kv[0])):
        table.append({
            "burst_mode": mode, "gap_ms": gap, "duration_ms": dur,
            "n": len(vals),
            "median_reentry_ms": round(median(vals), 2),
            "p90_reentry_ms": round(quantile(vals, 0.90), 2),
            "insufficient": len(vals) < MIN_SAMPLE,
        })
    return {
        "status": "ok" if table else "insufficient",
        "reason": "" if table else "没有连点配置 / 再进池延迟字段",
        "groups": table,
    }


def _result_breakdown(rows):
    counts = {}
    for row in rows:
        key = row["result"] or "unknown"
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    return {
        "total": total,
        "counts": counts,
        "rates": {k: round(v / total, 4) for k, v in counts.items()}
        if total else {},
    }


def _quality(rows, attempts, duplicates, now, order_override=None):
    max_ts = max((r["ts"] for r in rows if r["ts"] is not None), default=None)
    min_ts = min((r["ts"] for r in rows if r["ts"] is not None), default=None)

    # kill switch 的「全失败/全成功」只看有明确胜负的尝试；
    # 全是 skip / 余额不足 不算异常，也不该冻结。
    decided = [r for r in attempts if r["result"] in ("hit", "miss")]
    recent = decided[-KILL_MIN_ATTEMPTS:]
    if max_ts is not None:
        recent = sorted(decided,
                        key=lambda r: r["ts"] if r["ts"] is not None else -1)
        recent = recent[-KILL_MIN_ATTEMPTS:]
    hits_recent = sum(1 for r in recent if r["result"] == "hit")
    all_fail = len(recent) >= KILL_MIN_ATTEMPTS and hits_recent == 0
    all_success = len(recent) >= KILL_MIN_ATTEMPTS and hits_recent == len(recent)

    order = order_override or _time_order(rows)

    identities = [r["card_hash"] or r["name"] for r in attempts
                  if (r["card_hash"] or r["name"])]
    top_share, spam = None, False
    if identities:
        counts = {}
        for ident in identities:
            counts[ident] = counts.get(ident, 0) + 1
        top_share = max(counts.values()) / len(identities)
        spam = (len(identities) >= KILL_MIN_ATTEMPTS
                and top_share >= KILL_SPAM_SHARE)

    suspect = 0
    for r in rows:
        if r["ts"] is None:
            continue
        if r["ts"] < 978307200 or r["ts"] > now + 86400:  # 2001-01-01
            suspect += 1
    ts_total = sum(1 for r in rows if r["ts"] is not None)
    ts_suspect_ratio = (suspect / ts_total) if ts_total else 0.0

    reasons = []
    if all_fail:
        reasons.append(f"最近 {len(recent)} 次尝试全部失败（成交 0）")
    if all_success:
        reasons.append(f"最近 {len(recent)} 次尝试全部成交（成交 {len(recent)}）")
    if order["disordered"]:
        reasons.append(
            f"时间戳乱序：升/降序混杂 {order['mixed_ratio']:.0%}"
            f"（{order['pairs']} 对相邻样本）")
    if spam:
        reasons.append(f"单一商品刷屏：占比 {top_share:.0%}")
    if ts_total and ts_suspect_ratio >= KILL_TS_SUSPECT_RATIO:
        reasons.append(f"时间戳不可信：{ts_suspect_ratio:.0%} 落在合理区间外")
    if not attempts:
        reasons.append("没有任何可识别的尝试记录")

    return {
        "n_rows": len(rows),
        "n_attempts": len(attempts),
        "duplicates_removed": duplicates,
        "result_unknown": sum(1 for r in rows if r["result"] is None),
        "ts_min": min_ts,
        "ts_max": max_ts,
        "time_order": order,
        "ts_suspect": suspect,
        "ts_suspect_ratio": round(ts_suspect_ratio, 4),
        "recent_window": len(recent),
        "recent_hits": hits_recent,
        "all_fail": all_fail,
        "all_success": all_success,
        "single_item_share": round(top_share, 4) if top_share is not None else None,
        "single_item_spam": spam,
        "freeze_reasons": reasons,
        "frozen": bool(reasons),
    }


def summarize(rows, window_ms=WINDOW_MS, min_sample=MIN_SAMPLE, now=None,
              order_override=None):
    """把归一化后的行汇总成一份可复用的指标快照。

    ``order_override`` 用于多源合并场景：先按每个源自己的原始顺序做乱序检查，
    再合并排序。工具层负责传入合并后的结论，避免「跨源交错」被误判成乱序。
    """
    now = time.time() if now is None else now
    rows = [r for r in rows if r]
    rows, duplicates = _dedupe(rows)
    attempts = [r for r in rows if _attempt(r)]

    by_grade = {}
    for grade in GRADES:
        subset = [r for r in rows if r["grade"] == grade]
        if subset:
            by_grade[grade] = _group_stats(subset, window_ms)

    by_hour = {}
    for row in rows:
        hour = _hour_of(row["ts"])
        if hour is None:
            continue
        by_hour.setdefault(str(hour), []).append(row)
    by_hour_stats = {h: _group_stats(v, window_ms)
                     for h, v in sorted(by_hour.items(), key=lambda kv: int(kv[0]))}

    by_client = {}
    for row in rows:
        by_client.setdefault(row["client"] or "unknown", []).append(row)
    by_client_stats = {c: _group_stats(v, window_ms)
                       for c, v in sorted(by_client.items())}

    lead_all = _dist([r["lead_err_ms"] for r in attempts
                      if r["lead_err_ms"] is not None], window_ms)
    lead_by_grade = {g: _group_stats([r for r in rows if r["grade"] == g],
                                     window_ms)["lead"]
                     for g in by_grade}

    missing = {}
    for field in ALIASES:
        present = sum(1 for r in rows if field in r["_present"])
        missing[field] = len(rows) - present

    return {
        "n_rows": len(rows),
        "n_attempts": len(attempts),
        "n_hits": sum(1 for r in attempts if r["result"] == "hit"),
        "hit_ts": sorted({round(r["ts"], 3) for r in attempts
                          if r["result"] == "hit" and r["ts"] is not None}),
        "window_ms": window_ms,
        "min_sample": min_sample,
        "quality": _quality(rows, attempts, duplicates, now, order_override),
        "by_grade": by_grade,
        "lead_error": {
            "all": lead_all,
            "by_grade": lead_by_grade,
            "by_hour": {h: s["lead"] for h, s in by_hour_stats.items()},
            "by_client": {c: s["lead"] for c, s in by_client_stats.items()},
        },
        "result_breakdown": _result_breakdown(rows),
        "skip_accuracy": _skip_accuracy(rows),
        "refresh_benefit": _refresh_benefit(rows),
        "burst_benefit": _burst_benefit(rows),
        "by_hour": by_hour_stats,
        "by_client": by_client_stats,
        "missing_fields": missing,
    }


# ---------------------------------------------------------------- 自适应层


def _iso(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def _default_state():
    return {
        "version": 0,
        "frozen": False,
        "freeze": None,
        "freeze_history": [],
        "history": [],
        "knobs": {},
        "last_step_ts": None,
    }


class AutotuneStore:
    """审计 jsonl + 状态 json 的持久化。directory=None 时落在 data/autotune。"""

    def __init__(self, directory=None):
        self.directory = directory or os.path.join(config_mod.DATA_DIR,
                                                   "autotune")
        self.state_path = os.path.join(self.directory, "state.json")
        self.audit_path = os.path.join(self.directory, "audit.jsonl")
        self.state = _default_state()
        self.loaded = False

    def load(self):
        if self.loaded:
            return self.state
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                merged = _default_state()
                merged.update(data)
                self.state = merged
        except (OSError, ValueError):
            self.state = _default_state()
        self.loaded = True
        return self.state

    def save(self):
        os.makedirs(self.directory, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def audit(self, entry):
        os.makedirs(self.directory, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def audit_entries(self, limit=200):
        try:
            with open(self.audit_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _effective_bounds(knob):
    spec = KNOBS[knob]
    hard_lo, hard_hi = switches.BOUNDS.get(knob, (None, None))
    soft_lo, soft_hi = spec["soft"]
    lo = max(x for x in (hard_lo, soft_lo) if x is not None)
    hi = min(x for x in (hard_hi, soft_hi) if x is not None)
    return lo, hi


def _step_size(knob, old):
    """单次步长：严格 <= 当前值的 20%；0 值用开关最小可表示步长起步。

    整数开关向下取整后至少 1（1 是整数的最小可表示单位，属于规则的
    必要例外；非 0 值不会出现「1 步 > 20%」以外的放大）。
    """
    spec = KNOBS[knob]
    if old > 0:
        raw = abs(old) * 0.2
    else:
        raw = spec["min_step"]
    if spec["kind"] == "int":
        return float(max(1, int(round(raw))))
    return float(raw)


def _clamp(value, lo, hi, kind):
    value = max(lo, min(hi, value))
    if kind == "int":
        return int(round(value))
    return round(value, 4)


class AutoTuner:
    """受控自适应：喂 summary（或原始行），产出建议并按护栏落地。"""

    def __init__(self, cfg=None, *, shadow=True, store=None,
                 window_ms=WINDOW_MS, min_sample=MIN_SAMPLE,
                 min_hits_between=MIN_HITS_BETWEEN,
                 now_fn=None, max_changes=MAX_CHANGES_PER_STEP):
        self.cfg = cfg if cfg is not None else {}
        self.shadow = shadow
        self.store = store if store is not None else AutotuneStore()
        self.store.load()
        self.window_ms = window_ms
        self.min_sample = min_sample
        self.min_hits_between = min_hits_between
        self.now_fn = now_fn or time.time
        self.max_changes = max_changes

    # ------------------------------------------------------------ 公共 API

    @property
    def state(self):
        return self.store.state

    def step(self, data, cfg=None, now=None):
        """喂一个批次。cfg 缺省用构造时给的（普通 dict 也收）。

        返回：``{shadow, frozen, decisions, applied, withheld, audit, version}``
        """
        cfg = self.cfg if cfg is None else cfg
        now = self.now_fn() if now is None else now
        summary = data if isinstance(data, dict) else summarize(
            data, self.window_ms, self.min_sample, now)

        reasons = summary["quality"].get("freeze_reasons") or []
        if reasons:
            self._freeze(reasons, now)
        else:
            self.state["last_step_ts"] = now

        proposals = [] if self.state["frozen"] else self._evaluate(summary, cfg, now)

        applied, withheld, audit = [], [], []
        changes = 0
        for proposal in sorted(proposals, key=lambda p: p["priority"]):
            entry = self._audit_base(proposal, "would_change", now, summary)
            blocked = proposal.get("blocked")
            if blocked:
                withheld.append(proposal)
                entry["action"] = "withheld"
                entry["note"] = blocked
                audit.append(entry)
                continue
            if changes >= self.max_changes:
                withheld.append(proposal)
                entry["action"] = "deferred"
                entry["note"] = "同批次只允许一次变更，等待下一次成交样本"
                audit.append(entry)
                continue
            interval_ok, hits_since = self._interval_ok(proposal["knob"], summary)
            if not interval_ok:
                withheld.append(proposal)
                entry["action"] = "withheld"
                entry["note"] = (f"距上次调整仅 {hits_since} 次成交，"
                                 f"不足 {self.min_hits_between}")
                entry["hits_since_change"] = hits_since
                audit.append(entry)
                continue
            if self.shadow:
                withheld.append(proposal)
                entry["action"] = "shadow"
                entry["note"] = "shadow 模式：只记录，不改运行参数"
                audit.append(entry)
                continue
            applied.append(self._commit(proposal, cfg, now, summary))
            entry.update(applied[-1]["audit"])
            entry["action"] = "change"
            audit.append(entry)
            changes += 1

        for entry in audit:
            self.store.audit(entry)
        if not self.shadow and changes:
            self.store.save()

        return {
            "shadow": self.shadow,
            "frozen": self.state["frozen"],
            "freeze_reasons": reasons,
            "decisions": proposals,
            "applied": applied,
            "withheld": withheld,
            "audit": audit,
            "version": self.state["version"],
        }

    def rollback(self, cfg=None, steps=1, now=None):
        """回滚到上一个版本（steps=3 即回到第一次调整前的值）。"""
        cfg = self.cfg if cfg is None else cfg
        now = self.now_fn() if now is None else now
        st = self.state
        history = st.get("history") or []
        if not history:
            entry = {
                "ts": _iso(now), "action": "rollback_noop", "mode":
                "shadow" if self.shadow else "apply",
                "note": "没有可回滚的历史版本",
            }
            self.store.audit(entry)
            return {"rolled_back": False, "version": st["version"],
                    "values": self.snapshot(cfg), "audit": entry}
        steps = max(1, min(int(steps), len(history)))
        snapshot = history[-steps]
        restored, errors = {}, []
        for knob, value in snapshot["values"].items():
            try:
                restored[knob] = switches.set(cfg, knob, value)
            except switches.SwitchValueError as exc:
                errors.append(str(exc))
        if self.shadow:
            entry = {
                "ts": _iso(now), "action": "rollback_shadow",
                "mode": "shadow", "from_version": st["version"],
                "to_version": snapshot["version"],
                "knob": None, "old": None, "new": None,
                "values": restored,
                "note": "shadow 模式没有真实改过值，回滚只做记录",
            }
            self.store.audit(entry)
            return {"rolled_back": False, "version": st["version"],
                    "values": self.snapshot(cfg), "audit": entry}
        st["history"] = history[:-steps]
        st["version"] = snapshot["version"]
        st["knobs"] = {}
        for knob in restored:
            st["knobs"][knob] = {"stopped": False, "changed_ts": None}
        self.store.save()
        entry = {
            "ts": _iso(now), "action": "rollback", "mode": "apply",
            "from_version": snapshot["version"] + steps,
            "to_version": snapshot["version"],
            "knob": None, "old": None, "new": None,
            "values": restored,
            "errors": errors,
            "note": f"回滚 {steps} 个版本",
        }
        self.store.audit(entry)
        return {"rolled_back": True, "version": st["version"],
                "values": self.snapshot(cfg), "audit": entry}

    def unfreeze(self, reason="", now=None):
        now = self.now_fn() if now is None else now
        st = self.state
        if not st["frozen"]:
            return False
        st["freeze_history"].append({"lifted_at": _iso(now),
                                     "reason": reason,
                                     "was": st.get("freeze")})
        st["frozen"] = False
        st["freeze"] = None
        self.store.save()
        self.store.audit({"ts": _iso(now), "action": "unfreeze",
                          "mode": "shadow" if self.shadow else "apply",
                          "note": reason or "人工解除冻结"})
        return True

    def propose(self, summary, cfg=None):
        """只出建议不改值，供分析工具塞进 tuning_recommendation.json。"""
        cfg = self.cfg if cfg is None else cfg
        proposals = self._evaluate(summary, cfg, self.now_fn())
        return proposals

    def snapshot(self, cfg=None):
        cfg = self.cfg if cfg is None else cfg
        return {knob: switches.get(cfg, knob) for knob in KNOBS}

    # ------------------------------------------------------------ 内部

    def _freeze(self, reasons, now):
        st = self.state
        if st["frozen"] and (st.get("freeze") or {}).get("reasons") == reasons:
            return
        st["frozen"] = True
        st["freeze"] = {"at": _iso(now), "reasons": list(reasons)}
        self.store.save()
        self.store.audit({"ts": _iso(now), "action": "freeze",
                          "mode": "shadow" if self.shadow else "apply",
                          "note": "；".join(reasons), "reasons": reasons})

    def _hits_after(self, summary, since_ts):
        if since_ts is None:
            return summary["n_hits"]
        hit_ts = summary.get("hit_ts") or []
        # ts 升序，找到严格大于 since_ts 的成交条数
        lo, hi = 0, len(hit_ts)
        while lo < hi:
            mid = (lo + hi) // 2
            if hit_ts[mid] <= since_ts:
                lo = mid + 1
            else:
                hi = mid
        return len(hit_ts) - lo

    def _interval_ok(self, knob, summary):
        info = self.state["knobs"].get(knob) or {}
        # 第一次调整没有「上一次」，不适用间隔护栏；之后必须攒够 N 次成交。
        if not info.get("last_change_at"):
            return True, summary["n_hits"]
        since = info.get("changed_ts")
        hits = self._hits_after(summary, since)
        return hits >= self.min_hits_between, hits

    def _audit_base(self, proposal, action, now, summary):
        return {
            "ts": _iso(now),
            "action": action,
            "mode": "shadow" if self.shadow else "apply",
            "knob": proposal["knob"],
            "old": proposal["old"],
            "new": proposal["new"],
            "rule": proposal["rule"],
            "metric": proposal["metric"],
            "sample": proposal["sample"],
            "sample_required": self.min_sample,
            "bounds": proposal["bounds"],
            "version": self.state["version"],
            "quality": {
                "n_attempts": summary["n_attempts"],
                "duplicates_removed": summary["quality"]["duplicates_removed"],
            },
            "text": proposal["text"],
        }

    def _commit(self, proposal, cfg, now, summary):
        st = self.state
        knob = proposal["knob"]
        before = self.snapshot(cfg)
        snapshot = {"version": st["version"], "values": before,
                    "at": _iso(now)}
        value = switches.set(cfg, knob, proposal["new"])
        st["history"].append(snapshot)
        st["version"] += 1
        max_ts = summary["quality"].get("ts_max")
        info = st["knobs"].setdefault(knob, {})
        info["changed_ts"] = max_ts if max_ts is not None else None
        info["last_change_at"] = _iso(now)
        info["last_value"] = value
        info["last_metrics"] = proposal["metric"]
        info["stopped"] = bool(proposal.get("at_bound"))
        if info["stopped"]:
            info["stop_side"] = "hi" if proposal["new"] == proposal["bounds"][1] else "lo"
            info["stop_reason"] = f"撞界 {proposal['bounds']}"
        return {"knob": knob, "old": proposal["old"], "new": value,
                "audit": {"version": st["version"]}}

    def _evaluate(self, summary, cfg, now):
        proposals = []
        proposals += self._propose_lead(summary, cfg, now)
        proposals += self._propose_skip(summary, cfg, now)
        proposals += self._propose_refresh(summary, cfg, now)
        return proposals

    # ---- 规则 R1：开点对齐（fire.delay）

    def _propose_lead(self, summary, cfg, now):
        knob = "fire.delay"
        old = switches.get(cfg, knob)
        dist = summary["lead_error"]["all"]
        n = dist["n"]
        grade_n = max((g["attempts"] for g in summary["by_grade"].values()),
                      default=0)
        sample = min(n, grade_n) if grade_n else n
        lo, hi = _effective_bounds(knob)
        if sample < self.min_sample:
            return [self._blocked(
                knob, old, old, "R1-lead",
                f"样本量不足：最大品阶尝试 {grade_n} 次，卡点误差 {n} 条，"
                f"< {self.min_sample}", summary, "sample", metric=dist)]
        if dist["p50"] is None:
            return [self._blocked(knob, old, old, "R1-lead",
                                  "没有可用的卡点误差字段", summary, "no_metric",
                                  metric=dist)]

        prior = (self.state["knobs"].get(knob) or {}).get("last_metrics") or {}
        stop_side = (self.state["knobs"].get(knob) or {}).get("stop_side")
        p50 = dist["p50"]
        p95 = dist["p95"]
        ratio = dist["out_of_window_ratio"]

        if p50 <= -ALIGN_BAND_MS:          # 点早了 -> 往后推
            direction = 1
            new = _clamp(old + _step_size(knob, old), lo, hi, "int")
            rule = "R1-lead-early"
            why = f"p50 卡点误差 {p50:.1f}ms（点早）"
        elif p50 >= ALIGN_BAND_MS:         # 点晚了 -> 只能减延迟
            if old <= lo:
                return [self._blocked(
                    knob, old, old, "R1-lead-late",
                    "fire.delay 已在下界；再往前需要常量提前量（不在本层范围）",
                    summary, "constant", metric=dist)]
            direction = -1
            new = _clamp(old - _step_size(knob, old), lo, hi, "int")
            rule = "R1-lead-late"
            why = f"p50 卡点误差 {p50:+.1f}ms（点晚）"
        else:
            return [self._blocked(knob, old, old, "R1-lead",
                                  f"p50={p50:+.1f}ms 在 ±{ALIGN_BAND_MS:.0f}ms "
                                  "死区内，不折腾", summary, "deadband",
                                  metric=dist)]

        if (direction > 0 and stop_side == "hi") or \
                (direction < 0 and stop_side == "lo"):
            return [self._blocked(
                knob, old, old, rule,
                f"该方向已撞界并停止（{stop_side} 侧，见审计 stop_reason）",
                summary, "stopped", metric=dist)]

        if prior.get("p95") is not None:
            why += (f"，p95 从 {prior['p95']:.1f}ms 到 {p95:.1f}ms"
                    f"（超窗 {ratio:.0%}）")
        else:
            why += f"，p95={p95:.1f}ms，超窗 {ratio:.0%}"
        text = (f"因为 {why}，所以把 fire.delay 从 {old} 调到 {new} "
                f"（{rule}，样本 n={sample}）")
        at_bound = (new == hi and direction > 0) or (new == lo and direction < 0)
        if new == old:
            return [self._blocked(knob, old, old, rule,
                                  f"已到边界 [{lo}, {hi}]，无法继续调整",
                                  summary, "bound", metric=dist)]
        return [{
            "knob": knob, "old": old, "new": new, "rule": rule,
            "priority": KNOBS[knob]["priority"],
            "sample": sample, "bounds": [lo, hi], "at_bound": at_bound,
            "metric": {"name": "p50_lead_err_ms", "value": p50, "p95": p95,
                       "out_of_window_ratio": ratio, "previous_p95":
                       prior.get("p95")},
            "text": text, "blocked": None,
        }]

    # ---- 规则 R2：跳过阈值（buy.skip_late.threshold_s）

    def _propose_skip(self, summary, cfg, now):
        knob = "buy.skip_late.threshold_s"
        old = switches.get(cfg, knob)
        skip = summary["skip_accuracy"]
        n = skip["confirmed_n"]
        lo, hi = _effective_bounds(knob)
        if n < self.min_sample:
            return [self._blocked(
                knob, old, old, "R2-skip",
                f"跳过命中率只有 {n} 条事后证据（unknown {skip['unknown']}），"
                f"< {self.min_sample}，不下结论也不改值", summary, "sample",
                metric={"name": "skip_wrong_rate", "value": skip["wrong_rate"]})]
        wrong = skip["wrong_rate"]
        ratio = summary["lead_error"]["all"]["out_of_window_ratio"] or 0.0
        if wrong >= 0.30:                  # 跳错太多 -> 放宽阈值（调小）
            direction = -1
            new = _clamp(old - _step_size(knob, old), lo, hi, "int")
            rule = "R2-skip-too-aggressive"
            why = f"跳过的事后证据里「本来来得及」占 {wrong:.0%}（跳得太狠）"
        elif wrong <= 0.20 and ratio >= 0.20:   # 太保守且确实常超窗 -> 收紧
            direction = 1
            new = _clamp(old + _step_size(knob, old), lo, hi, "int")
            rule = "R2-skip-too-loose"
            why = f"跳错仅 {wrong:.0%}，但超窗比例 {ratio:.0%}"
        else:
            return [self._blocked(
                knob, old, old, "R2-skip",
                f"跳错 {wrong:.0%}、超窗 {ratio:.0%}，两个方向证据都不够",
                summary, "no_signal",
                metric={"name": "skip_wrong_rate", "value": wrong})]
        stop_side = (self.state["knobs"].get(knob) or {}).get("stop_side")
        if (direction > 0 and stop_side == "hi") or \
                (direction < 0 and stop_side == "lo"):
            return [self._blocked(
                knob, old, old, rule,
                f"该方向已撞界并停止（{stop_side} 侧）", summary, "stopped",
                metric={"name": "skip_wrong_rate", "value": wrong})]
        if new == old:
            return [self._blocked(knob, old, old, rule,
                                  f"已到边界 [{lo}, {hi}]，无法继续调整",
                                  summary, "bound")]
        at_bound = (new == hi and direction > 0) or (new == lo and direction < 0)
        text = (f"因为 {why}（事后证据 n={n}，unknown 不计入），所以把 "
                f"buy.skip_late.threshold_s 从 {old} 调到 {new}（{rule}）")
        return [{
            "knob": knob, "old": old, "new": new, "rule": rule,
            "priority": KNOBS[knob]["priority"],
            "sample": n, "bounds": [lo, hi], "at_bound": at_bound,
            "metric": {"name": "skip_wrong_rate", "value": wrong,
                       "out_of_window_ratio": ratio},
            "text": text, "blocked": None,
        }]

    # ---- 规则 R3/R4：刷新收益（scan.interval / repeat.max / repeat.min）

    def _propose_refresh(self, summary, cfg, now):
        refresh = summary["refresh_benefit"]
        lo_i, hi_i = _effective_bounds("scan.interval")
        old_i = switches.get(cfg, "scan.interval")
        if refresh["status"] != "ok":
            return [self._blocked(
                "scan.interval", old_i, old_i, "R3-refresh",
                refresh.get("reason") or "刷新收益数据不足", summary, "sample",
                metric={"name": "refresh_slope", "value": None})]
        ci_low, ci_high = refresh["slope_ci95"]
        n = refresh["min_bucket_n"]
        out = []
        new_i = old_i
        if ci_low > 0:
            new_i = _clamp(old_i - _step_size("scan.interval", old_i),
                           lo_i, hi_i, "float")
            rule = "R3-refresh-positive"
            text = (f"因为每多刷一次成交率提升 {refresh['slope_per_refresh']:.1%}"
                    f"（95%CI [{ci_low:.1%}, {ci_high:.1%}]，每桶 n>={n}），"
                    f"所以把 scan.interval 从 {old_i} 调到 {new_i}（加密扫描）")
        elif ci_high < 0:
            new_i = _clamp(old_i + _step_size("scan.interval", old_i),
                           lo_i, hi_i, "float")
            rule = "R3-refresh-negative"
            text = (f"因为每多刷一次成交率变化 {refresh['slope_per_refresh']:.1%}"
                    f"（95%CI [{ci_low:.1%}, {ci_high:.1%}]，明显为负），"
                    f"所以把 scan.interval 从 {old_i} 调到 {new_i}（放慢扫描）")
        else:
            out.append(self._blocked(
                "scan.interval", old_i, old_i, "R3-refresh",
                "刷新收益 95%CI 跨 0，保持原值", summary, "no_signal",
                metric={"name": "refresh_slope_per_1",
                        "value": refresh["slope_per_refresh"]}))

        if new_i != old_i:
            out.append(self._mk(
                "scan.interval", old_i, new_i, rule, text, n, (lo_i, hi_i),
                {"name": "refresh_slope_per_1",
                 "value": refresh["slope_per_refresh"],
                 "ci95": refresh["slope_ci95"]}))
        elif ci_low <= 0 and ci_high >= 0:
            pass  # 已经在上面记了 no_signal
        elif new_i == lo_i:
            out.append(self._blocked(
                "scan.interval", old_i, old_i, "R3-refresh",
                f"扫描间隔已到下限 {lo_i}s，无法再加密", summary, "bound",
                metric={"name": "refresh_slope_per_1",
                        "value": refresh["slope_per_refresh"]}))
        elif new_i == hi_i:
            out.append(self._blocked(
                "scan.interval", old_i, old_i, "R3-refresh",
                f"扫描间隔已到上限 {hi_i}s，无法再放慢", summary, "bound",
                metric={"name": "refresh_slope_per_1",
                        "value": refresh["slope_per_refresh"]}))

        # 刷新次数上下限：只在 interval 已撞界时接手，避免一次动两个
        old_max = switches.get(cfg, "scan.repeat.max")
        old_min = switches.get(cfg, "scan.repeat.min")
        lo_r, hi_r = _effective_bounds("scan.repeat.max")
        at_low = new_i == lo_i and old_i == lo_i
        at_high = new_i == hi_i and old_i == hi_i
        if ci_low > 0 and at_low:
            if old_max < hi_r:
                new_max = _clamp(old_max + 1, old_min, hi_r, "int")
                out.append(self._mk(
                    "scan.repeat.max", old_max, new_max, "R4-repeat-positive",
                    f"扫描间隔已到下限 {lo_i}s，且刷新正收益显著"
                    f"（{refresh['slope_per_refresh']:.1%}/次，n>={n}），"
                    f"所以把 scan.repeat.max 从 {old_max} 调到 {new_max}"
                    "（允许每件多刷几次）",
                    n, (old_min, hi_r),
                    {"name": "refresh_slope_per_1",
                     "value": refresh["slope_per_refresh"]}))
            elif old_min < old_max:
                new_min = _clamp(old_min + 1, lo_r, old_max, "int")
                out.append(self._mk(
                    "scan.repeat.min", old_min, new_min, "R4-repeat-positive",
                    f"scan.repeat.max 已到上限 {hi_r}，但刷新正收益显著"
                    f"（{refresh['slope_per_refresh']:.1%}/次，n>={n}），"
                    f"所以把 scan.repeat.min 从 {old_min} 调到 {new_min}"
                    "（保证每件至少多刷一次）",
                    n, (lo_r, old_max),
                    {"name": "refresh_slope_per_1",
                     "value": refresh["slope_per_refresh"]}))
        elif ci_high < 0 and at_high:
            if old_max > old_min:
                new_max = _clamp(old_max - 1, old_min, hi_r, "int")
                out.append(self._mk(
                    "scan.repeat.max", old_max, new_max, "R4-repeat-negative",
                    f"扫描已放到上限 {hi_i}s，但刷新次数越多成交率越低"
                    f"（{refresh['slope_per_refresh']:.1%}/次，n>={n}），"
                    f"所以把 scan.repeat.max 从 {old_max} 调到 {new_max}",
                    n, (old_min, hi_r),
                    {"name": "refresh_slope_per_1",
                     "value": refresh["slope_per_refresh"]}))
            elif old_min > 0:
                new_min = _clamp(old_min - 1, lo_r, old_max, "int")
                out.append(self._mk(
                    "scan.repeat.min", old_min, new_min, "R4-repeat-negative",
                    f"scan.repeat.max 已到下限，且刷新负收益显著"
                    f"（{refresh['slope_per_refresh']:.1%}/次，n>={n}），"
                    f"所以把 scan.repeat.min 从 {old_min} 调到 {new_min}",
                    n, (lo_r, old_max),
                    {"name": "refresh_slope_per_1",
                     "value": refresh["slope_per_refresh"]}))
        return out

    # ---- 小工具

    def _mk(self, knob, old, new, rule, text, sample, bounds, metric):
        spec = KNOBS[knob]
        return {
            "knob": knob, "old": old, "new": new, "rule": rule,
            "priority": spec["priority"], "sample": sample,
            "bounds": [bounds[0], bounds[1]],
            "at_bound": new == bounds[0] or new == bounds[1],
            "metric": metric, "text": text, "blocked": None,
        }

    def _blocked(self, knob, old, new, rule, reason, summary, kind,
                 metric=None):
        text = f"不改值：{reason}"
        return {
            "knob": knob, "old": old, "new": new, "rule": rule,
            "priority": KNOBS[knob]["priority"]
            if knob in KNOBS else 99,
            "sample": summary.get("n_attempts", 0),
            "bounds": list(_effective_bounds(knob)) if knob in KNOBS else [],
            "at_bound": False, "metric": metric or {},
            "text": text, "blocked": f"{kind}: {reason}",
        }


# ---------------------------------------------------------------- 回测工具


def synthetic_rows(rng, *, bias_ms, delay_ms, n, grade="legend", ts0=0.0,
                   start_ts=None, noise_ms=38.0,
                   names=("风暴之怒", "龙啸九天", "星界游侠", "暗影猎手",
                          "霜火之刃"),
                   window_ms=WINDOW_MS, win_prob=0.8):
    """生成一批合成观测：卡点误差 = 真实偏差 + 当前延迟 + 噪声。

    ``bias_ms`` 是「引擎常量提前量造成的固有偏差」，负数表示点早。
    最优 ``fire.delay`` = -bias_ms。

    ``win_prob`` 是抽签模型的中签概率：即使卡点在窗口内也不保证成交。
    回测取 0.8 是为了让每批成交样本足够触发「间隔 N 次成交」护栏。
    """
    rows = []
    t0 = start_ts if start_ts is not None else ts0
    for i in range(n):
        err = bias_ms + delay_ms + rng.gauss(0.0, noise_ms)
        in_window = abs(err) <= window_ms
        hit = in_window and rng.random() < win_prob
        result = "hit" if hit else "miss"
        ts = t0 + i * 1.0
        rows.append({
            "ts": ts,
            "card_hash": None,
            "name": names[i % len(names)],
            "grade": grade,
            "price": 1000.0,
            "result": result,
            "lead_err_ms": err,
            "refresh_count": None,
            "burst_mode": None,
            "burst_gap_ms": None,
            "burst_duration_ms": None,
            "reentry_delay_ms": None,
            "client": "synthetic",
            "skip_evidence": None,
            "source": "synthetic",
            "_present": ["ts", "name", "grade", "result", "lead_err_ms"],
        })
    return rows


def run_convergence(true_bias_ms=-30.0, batches=20, batch_size=30,
                    seed=20261107, drift_to=None, drift_batches=12,
                    noise_ms=38.0):
    """确定性回测：给定真实参数，看自适应能否收敛 / 跟踪漂移。

    返回收敛曲线（每批一个点）与漂移滞后批次数。
    """
    import random
    import shutil
    import tempfile

    rng = random.Random(seed)
    cfg = {}
    tmpdir = tempfile.mkdtemp(prefix="autotune_bt_")
    tuner = AutoTuner(cfg, shadow=False, store=AutotuneStore(directory=tmpdir),
                      min_hits_between=MIN_HITS_BETWEEN)
    initial_target = -true_bias_ms
    target = initial_target
    curve = []
    t = 1_700_000_000.0
    delay = switches.get(cfg, "fire.delay")
    for b in range(batches):
        rows = synthetic_rows(rng, bias_ms=true_bias_ms, delay_ms=delay,
                              n=batch_size, start_ts=t, noise_ms=noise_ms)
        t += batch_size + 1
        summary = summarize(rows, now=t)
        out = tuner.step(summary, cfg, now=t)
        delay = switches.get(cfg, "fire.delay")
        curve.append({
            "batch": b + 1, "delay_ms": delay,
            "abs_error_ms": round(abs(delay - target), 1),
            "hits": summary["n_hits"],
            "changed": bool(out["applied"]),
            "rule": out["applied"][0]["knob"] if out["applied"] else "",
        })

    drift = None
    if drift_to is not None:
        true_bias_ms = drift_to
        target = -true_bias_ms
        lag = None
        trail = []
        for b in range(drift_batches):
            rows = synthetic_rows(rng, bias_ms=true_bias_ms, delay_ms=delay,
                                  n=batch_size, start_ts=t, noise_ms=noise_ms)
            t += batch_size + 1
            summary = summarize(rows, now=t)
            tuner.step(summary, cfg, now=t)
            delay = switches.get(cfg, "fire.delay")
            err = abs(delay - target)
            if lag is None and err <= 20.0:
                lag = b + 1
            trail.append({"batch": b + 1, "delay_ms": delay,
                          "abs_error_ms": round(err, 1)})
        drift = {"new_target_ms": target, "lag_batches": lag, "trail": trail}
    shutil.rmtree(tmpdir, ignore_errors=True)
    return {"target_ms": initial_target,
            "final_error_ms": curve[-1]["abs_error_ms"],
            "curve": curve, "drift": drift}
