# -*- coding: utf-8 -*-
"""B3 详情面板就绪延迟采集：点击 -> 面板可读（能 OCR 到编号）的延迟。

当前口径里「进详情固定等待 610.43 ms」就是拿固定 sleep 盖住这段未知延迟；
本脚本先把这段延迟量出来，才能判断 flow/01（条件等待）能不能真正落地。

用法：
  python tools/collect_b3_detail_ready.py --events events.jsonl
  python tools/collect_b3_detail_ready.py --selftest

事件格式：
  {"ts": 1.000, "kind": "detail_click"}   # 或 click_detail / 点击详情
  {"ts": 1.486, "kind": "detail_ready"}   # 或 panel_ready / 面板可读
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

CLICK_KINDS = {"detail_click", "click_detail", "click_down", "click", "点击详情"}
READY_KINDS = {"detail_ready", "panel_ready", "ready", "面板可读", "详情就绪"}
EDGES_MS = [200, 400, 550, 700, 900, 1200]


def collect_lags(records, max_lag_ms=2000.0):
    events = []
    for rec in records:
        ts = event_ts(rec)
        if ts is None:
            continue
        kind = event_kind(rec)
        k = kind.lower() if kind.isascii() else kind
        if kind in CLICK_KINDS or k in CLICK_KINDS:
            events.append((ts, "c"))
        elif kind in READY_KINDS or k in READY_KINDS:
            events.append((ts, "r"))
    events.sort(key=lambda x: x[0])

    lags = []
    last_click = None
    dropped = 0
    for ts, tag in events:
        if tag == "c":
            last_click = ts
            continue
        if last_click is None:
            dropped += 1
            continue
        lag = (ts - last_click) * 1000.0
        if lag < 0 or lag > max_lag_ms:
            dropped += 1
        else:
            lags.append(lag)
            last_click = None
    return lags, {"dropped": dropped}


def build_report(lags, drop, args):
    st = summary(lags)
    fixed = args.current_sleep_ms
    over = None
    if st["p99"] is not None:
        over = round(st["p99"] - fixed, 2)
    return {
        "script": "B3_detail_ready",
        "objective": "点击 -> 详情面板可读的延迟（P99）",
        "sample": st,
        "p99_ms": st["p99"],
        "current_fixed_sleep_ms": fixed,
        "p99_minus_fixed_ms": over,
        "interpretation": (
            "P99 高于固定等待：现有 610 ms 会偶发读不到面板，需要加尾部重试"
            if over is not None and over > 0 else
            "P99 低于固定等待：存在可压缩空间，flow/01 条件等待可评估落地"),
        "histogram_ms": [{"lo": lo, "hi": hi, "n": c}
                         for lo, hi, c in hist(lags, EDGES_MS)],
        "dropped": drop,
        "min_sample": args.min_sample,
        "status": "OK" if st["n"] >= args.min_sample else "INSUFFICIENT",
        "source": args.events or "selftest",
    }


def render(rep):
    lines = ["B3 详情面板就绪（点击 -> 可读）",
             "样本量: %d" % rep["sample"]["n"]]
    for key in ("p50", "p95", "p99", "max", "mean"):
        lines.append("  %-5s %s ms" % (key, rep["sample"][key]))
    lines.append("对照: 现有固定等待 %.2f ms，P99 - 固定 = %s ms" %
                 (rep["current_fixed_sleep_ms"], rep["p99_minus_fixed_ms"]))
    lines.append("解读: %s" % rep["interpretation"])
    lines.append("直方图(ms):")
    for b in rep["histogram_ms"]:
        hi = "inf" if b["hi"] is None else str(b["hi"])
        lines.append("  [%5s, %5s)  %d" % (b["lo"], hi, b["n"]))
    if rep["status"] == "INSUFFICIENT":
        lines.append("注意: 样本不足 %d，不给结论。" % rep["min_sample"])
    return lines


def selftest():
    # 真实分布桩：p50≈420ms，10% 尾部 700-900ms
    rng = random.Random(7)
    recs = []
    expect = []
    t = 0.0
    for i in range(150):
        recs.append({"ts": t, "kind": "detail_click"})
        lag = (700.0 + rng.random() * 200) if i % 10 == 0 else (360.0 + rng.random() * 120)
        expect.append(lag)
        recs.append({"ts": t + lag / 1000.0, "kind": "detail_ready"})
        t += 2.5
    recs.append({"ts": 5.0, "kind": "detail_ready"})       # 无点击
    recs.append({"ts": t + 9.0, "kind": "detail_click"})   # 无就绪
    lags, drop = collect_lags(recs, max_lag_ms=2000.0)
    st = summary(lags)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))

    check("配对数", len(lags) == 150, "got=%d" % len(lags))
    check("孤立就绪被丢弃", drop["dropped"] == 1, str(drop))
    check("P50 在 360-480 区间", 350.0 <= st["p50"] <= 500.0, "p50=%.1f" % st["p50"])
    check("P99 在尾部区间", 650.0 <= st["p99"] <= 1000.0, "p99=%.1f" % st["p99"])
    check("P99 判定为高于固定 610", st["p99"] > 610.0, "p99=%.1f" % st["p99"])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B3 详情面板就绪延迟采集")
    ap.add_argument("--events")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--max-lag-ms", type=float, default=2000.0)
    ap.add_argument("--min-sample", type=int, default=20)
    ap.add_argument("--current-sleep-ms", type=float, default=610.43,
                    help="当前树进详情的固定等待，用于对照")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.events:
        ap.error("需要 --events 或 --selftest")

    recs = read_jsonl(args.events)
    lags, drop = collect_lags(recs, args.max_lag_ms)
    rep = build_report(lags, drop, args)
    rep["out_dir"] = args.out_dir or default_out_dir("b3_detail_ready")
    lines = render(rep)
    print("\n".join(lines))
    jp, tp = write_report(rep["out_dir"], "b3_detail_ready", rep, lines)
    print("输出: %s\n      %s" % (jp, tp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
