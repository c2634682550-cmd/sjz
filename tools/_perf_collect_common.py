# -*- coding: utf-8 -*-
"""B1–B5 采集脚本共用工具：分位数、JSONL 事件读取、临时输出目录。

约定：
- 事件时间戳统一按**秒**（monotonic 或 epoch 均可，只要同一条流内一致）。
  接受 ts / t / time / timestamp；带 `_ms` 后缀的字段自动 /1000。
- 所有输出默认写系统临时目录，**不写仓库 data/**。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_out_dir(tag):
    """临时输出目录：%TEMP%/dafors_perf/<tag>-<时间戳>/。"""
    root = os.environ.get("DAFORS_PERF_OUT") or tempfile.gettempdir()
    d = os.path.join(root, "dafors_perf",
                     "%s-%s" % (tag, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(d, exist_ok=True)
    return d


def percentile(values, q):
    """线性插值分位数；values 为有限数值序列，q ∈ [0,100]。"""
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(v))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * (float(q) / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def summary(values):
    xs = [float(v) for v in values if v is not None and math.isfinite(v)]
    if not xs:
        return {"n": 0, "p50": None, "p95": None, "p99": None,
                "max": None, "mean": None}
    return {
        "n": len(xs),
        "p50": round(percentile(xs, 50), 3),
        "p95": round(percentile(xs, 95), 3),
        "p99": round(percentile(xs, 99), 3),
        "max": round(max(xs), 3),
        "mean": round(sum(xs) / len(xs), 3),
    }


def read_jsonl(path):
    """读 JSONL / JSON 数组 / 单个 JSON 对象，容错空行与 BOM。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read().strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for key in ("events", "records", "items", "data"):
                if isinstance(obj.get(key), list):
                    return obj[key]
            return [obj]
    except ValueError:
        pass
    out = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


_TS_KEYS = ("ts", "t", "time", "timestamp", "at", "ts_ms", "t_ms")
_KIND_KEYS = ("kind", "event", "type", "name", "tag")


def event_ts(rec):
    """取事件时间戳（秒）。支持 *_ms 自动换算。缺失返回 None。"""
    for key in _TS_KEYS:
        if key in rec:
            try:
                v = float(rec[key])
            except (TypeError, ValueError):
                continue
            return v / 1000.0 if key.endswith("_ms") else v
    return None


def event_kind(rec):
    for key in _KIND_KEYS:
        if key in rec and rec[key] is not None:
            return str(rec[key]).strip()
    return ""


def write_report(out_dir, tag, payload, lines):
    """同时写 <tag>.json 与 <tag>.txt，返回两个路径。"""
    os.makedirs(out_dir, exist_ok=True)
    jp = os.path.join(out_dir, tag + ".json")
    tp = os.path.join(out_dir, tag + ".txt")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(tp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return jp, tp


def hist(values, edges_ms):
    """按毫秒边界做计数直方图，返回 [(lo,hi,count)]。"""
    counts = [0] * max(1, len(edges_ms) + 1)
    for v in values:
        for i, edge in enumerate(edges_ms):
            if v < edge:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    out = []
    lo = 0
    for i, edge in enumerate(edges_ms):
        out.append((lo, edge, counts[i]))
        lo = edge
    out.append((lo, None, counts[-1]))
    return out


def print_kv(title, data, indent=2):
    print(title)
    pad = " " * indent
    for k, v in data.items():
        print("%s%-18s %s" % (pad, k, v))
