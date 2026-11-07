# -*- coding: utf-8 -*-
"""B1 卡点 P95 采集：列表刷新时刻 → 按下时刻。

这是所有点击优化的**准入门禁**：没有这条分布，任何「再快 N ms」的说法都没有依据。
当前该数据为 0 条。

用法：
  # 真机：喂事件流（JSONL，ts 单位为秒）
  python tools/collect_b1_gate_p95.py --events events.jsonl
  # 自检：合成数据，不需要游戏窗口
  python tools/collect_b1_gate_p95.py --selftest

事件格式（一行一个 JSON）：
  {"ts": 12.345, "kind": "list_refresh"}   # 列表刷新/可按下时刻，别名: refresh / 列表刷新
  {"ts": 12.371, "kind": "click_down"}     # 物理按下时刻，别名: down / 按下 / click

输出：p50 / p95 / max / n + 直方图；同时给门禁判定（p95 ≤ --gate-ms）。
"""
from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _perf_collect_common import (  # noqa: E402
    default_out_dir, event_kind, event_ts, hist, read_jsonl, summary,
    write_report,
)

REFRESH_KINDS = {"list_refresh", "refresh", "list_scan", "列表刷新", "刷新"}
CLICK_KINDS = {"click_down", "down", "press", "click", "按下", "点击"}
EDGES_MS = [20, 50, 100, 200, 500, 1000]


def collect_lags(records, max_lag_ms=2000.0):
    """返回 (lags_ms, stats)。每个 click 配对它之前最近的一次 refresh。"""
    events = []
    dropped_no_refresh = 0
    dropped_stale = 0
    for rec in records:
        ts = event_ts(rec)
        if ts is None:
            continue
        kind = event_kind(rec)
        k = kind.lower() if kind.isascii() else kind
        if kind in REFRESH_KINDS or k in REFRESH_KINDS:
            events.append((ts, "r"))
        elif kind in CLICK_KINDS or k in CLICK_KINDS:
            events.append((ts, "c"))
    events.sort(key=lambda x: x[0])

    lags = []
    last_refresh = None
    for ts, tag in events:
        if tag == "r":
            last_refresh = ts
            continue
        if last_refresh is None:
            dropped_no_refresh += 1
            continue
        lag = (ts - last_refresh) * 1000.0
        if lag < 0:
            dropped_no_refresh += 1
        elif lag > max_lag_ms:
            dropped_stale += 1
        else:
            lags.append(lag)
    return lags, {"dropped_no_refresh": dropped_no_refresh,
                  "dropped_stale": dropped_stale}


def build_report(lags, drop, args):
    st = summary(lags)
    h = hist(lags, EDGES_MS)
    gate_ok = st["p95"] is not None and st["p95"] <= args.gate_ms
    return {
        "script": "B1_gate_p95",
        "objective": "列表刷新时刻 -> 按下时刻的卡点分布（点击优化准入门禁）",
        "gate_ms": args.gate_ms,
        "status": ("PASS" if gate_ok else
                   ("INSUFFICIENT" if st["n"] < args.min_sample else "FAIL")),
        "sample": st,
        "min_sample": args.min_sample,
        "histogram_ms": [{"lo": lo, "hi": hi, "n": c} for lo, hi, c in h],
        "dropped": drop,
        "source": args.events or "selftest",
    }


def render(rep):
    lines = []
    lines.append("B1 卡点 P95（列表刷新 -> 按下）")
    lines.append("样本量: %d  (最少 %d)" % (rep["sample"]["n"], rep["min_sample"]))
    for key in ("p50", "p95", "p99", "max", "mean"):
        lines.append("  %-5s %s ms" % (key, rep["sample"][key]))
    lines.append("门禁阈值: P95 <= %.1f ms -> %s" % (rep["gate_ms"], rep["status"]))
    lines.append("直方图(ms):")
    for b in rep["histogram_ms"]:
        hi = "inf" if b["hi"] is None else str(b["hi"])
        lines.append("  [%5s, %5s)  %d" % (b["lo"], hi, b["n"]))
    lines.append("丢弃: 无前序刷新 %d / 超过 max-lag %d" %
                 (rep["dropped"]["dropped_no_refresh"],
                  rep["dropped"]["dropped_stale"]))
    if rep["status"] == "INSUFFICIENT":
        lines.append("注意: 样本不足，不给结论；先补到 %d 条。" % rep["min_sample"])
    return lines


# ------------------------------------------------------------------ selftest
def selftest():
    rng = random.Random(20261107)
    recs = []
    expect = []
    t = 0.0
    for i in range(200):
        recs.append({"ts": t, "kind": "list_refresh"})
        if i % 20 == 0:
            lag = 480.0 + rng.random() * 40   # 5% 慢样本
        elif i % 7 == 0:
            lag = 110.0 + rng.random() * 35
        else:
            lag = 18.0 + rng.random() * 40
        expect.append(lag)
        recs.append({"ts": t + lag / 1000.0, "kind": "click_down"})
        t += 0.6
    # 干扰项：首帧刷新之前就点击（无前序）、超 max-lag、无法识别的事件
    recs.append({"ts": -5.0, "kind": "click_down"})
    recs.append({"ts": t + 5.0, "kind": "click_down"})
    recs.append({"ts": t + 5.1, "kind": "noise"})

    lags, drop = collect_lags(recs, max_lag_ms=2000.0)
    st = summary(lags)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))

    check("样本配对数", len(lags) == len(expect),
          "got=%d want=%d" % (len(lags), len(expect)))
    check("无前序刷新被丢弃", drop["dropped_no_refresh"] == 1, str(drop))
    check("超 max-lag 被丢弃", drop["dropped_stale"] == 1, str(drop))
    import statistics
    med = statistics.median(expect)
    check("p50 与期望中位数一致", abs(st["p50"] - med) <= 3.0,
          "p50=%.2f median=%.2f" % (st["p50"], med))
    check("p95 落在慢样本区", 90.0 <= st["p95"] <= 520.0, "p95=%.2f" % st["p95"])
    check("max 不超过 max-lag", st["max"] <= 2000.0, "max=%.2f" % st["max"])
    check("直方图计数总和", sum(b[2] for b in hist(lags, EDGES_MS)) == len(lags))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B1 卡点 P95 采集")
    ap.add_argument("--events", help="事件 JSONL/JSON 路径")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--max-lag-ms", type=float, default=2000.0,
                    help="超过此间隔视为配对失效（默认 2000）")
    ap.add_argument("--gate-ms", type=float, default=100.0,
                    help="P95 门禁（默认 100 ms）")
    ap.add_argument("--min-sample", type=int, default=30)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.events:
        ap.error("需要 --events 或 --selftest")

    recs = read_jsonl(args.events)
    lags, drop = collect_lags(recs, args.max_lag_ms)
    rep = build_report(lags, drop, args)
    rep["out_dir"] = args.out_dir or default_out_dir("b1_gate_p95")
    lines = render(rep)
    print("\n".join(lines))
    jp, tp = write_report(rep["out_dir"], "b1_gate_p95", rep, lines)
    print("输出: %s\n      %s" % (jp, tp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
