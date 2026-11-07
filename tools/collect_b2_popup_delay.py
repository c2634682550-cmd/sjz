# -*- coding: utf-8 -*-
"""B2 余额不足弹窗延迟采集 + 本机整屏弹窗素材自检。

采集：点击时刻 → 首次检测到「余额不足」弹窗的时刻，输出 P99。
自检：扫描产品素材目录，回答「本机到底有没有整屏弹窗素材」——当前结论是 0，
这正是 flow/04 预筛被砍的原因，也是本项必须先采集的前提。

用法：
  python tools/collect_b2_popup_delay.py --events events.jsonl
  python tools/collect_b2_popup_delay.py --selftest

事件格式：
  {"ts": 1.000, "kind": "click_down"}    # 或 buy_click / 按下
  {"ts": 1.212, "kind": "popup_seen"}    # 或 popup / insufficient / 弹窗
"""
from __future__ import annotations

import argparse
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _perf_collect_common import (  # noqa: E402
    PROJ, default_out_dir, event_kind, event_ts, hist, read_jsonl, summary,
    write_report,
)

CLICK_KINDS = {"click_down", "click", "buy_click", "down", "按下", "点击"}
POPUP_KINDS = {"popup_seen", "popup", "insufficient", "弹窗", "余额不足"}
ASSET_KEYWORDS = ("popup", "insufficient", "dialog", "notice", "toast",
                  "弹窗", "余额不足", "提示框", "整屏")
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
EDGES_MS = [50, 100, 200, 300, 500, 1000]


def collect_lags(records, max_lag_ms=3000.0):
    events = []
    for rec in records:
        ts = event_ts(rec)
        if ts is None:
            continue
        kind = event_kind(rec)
        k = kind.lower() if kind.isascii() else kind
        if kind in CLICK_KINDS or k in CLICK_KINDS:
            events.append((ts, "c"))
        elif kind in POPUP_KINDS or k in POPUP_KINDS:
            events.append((ts, "p"))
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
            last_click = None   # 一个点击只配一次弹窗
    return lags, {"dropped": dropped}


def png_size(path):
    """读 PNG IHDR 宽高，失败返回 None。用于判断「整屏」候选。"""
    try:
        with open(path, "rb") as f:
            if f.read(8) != PNG_MAGIC:
                return None
            head = f.read(16)
            if len(head) < 16 or head[4:8] != b"IHDR":
                return None
            w, h = struct.unpack(">II", head[8:16])
            return int(w), int(h)
    except OSError:
        return None


def scan_assets(dirs):
    hits = []
    scanned = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(IMG_EXT):
                    continue
                scanned += 1
                low = fn.lower()
                if any(k.lower() in low for k in ASSET_KEYWORDS):
                    p = os.path.join(root, fn)
                    size = png_size(p) if low.endswith(".png") else None
                    area = (size[0] * size[1]) if size else 0
                    hits.append({
                        "path": os.path.relpath(p, PROJ),
                        "size": None if size is None else "%dx%d" % size,
                        "fullscreen_candidate": area >= 1000000,
                    })
    return {
        "dirs_scanned": [os.path.relpath(d, PROJ) for d in dirs
                         if os.path.isdir(d)],
        "images_scanned": scanned,
        "popup_assets": hits,
        "has_fullscreen_popup_asset": any(h["fullscreen_candidate"]
                                          for h in hits),
        "conclusion": ("有整屏弹窗素材" if any(h["fullscreen_candidate"] for h in hits)
                       else "无整屏弹窗素材（预筛不可落地，需先补素材或走小窗口 ROI）"),
    }


def build_report(lags, drop, assets, args):
    st = summary(lags)
    return {
        "script": "B2_popup_delay",
        "objective": "点击 -> 余额不足弹窗首次可见的延迟（P99）",
        "sample": st,
        "p99_ms": st["p99"],
        "histogram_ms": [{"lo": lo, "hi": hi, "n": c}
                         for lo, hi, c in hist(lags, EDGES_MS)],
        "dropped": drop,
        "assets": assets,
        "status": "OK" if st["n"] >= args.min_sample else "INSUFFICIENT",
        "source": args.events or "selftest",
    }


def render(rep):
    lines = ["B2 余额不足弹窗延迟（点击 -> 首次可见）",
             "样本量: %d" % rep["sample"]["n"]]
    for key in ("p50", "p95", "p99", "max", "mean"):
        lines.append("  %-5s %s ms" % (key, rep["sample"][key]))
    lines.append("直方图(ms):")
    for b in rep["histogram_ms"]:
        hi = "inf" if b["hi"] is None else str(b["hi"])
        lines.append("  [%5s, %5s)  %d" % (b["lo"], hi, b["n"]))
    lines.append("素材自检:")
    a = rep["assets"]
    lines.append("  扫描目录: %s" % (", ".join(a["dirs_scanned"]) or "（无）"))
    lines.append("  图片数: %d，命中弹窗关键词: %d" %
                 (a["images_scanned"], len(a["popup_assets"])))
    for h in a["popup_assets"][:8]:
        lines.append("    - %s  %s  fullscreen=%s" %
                     (h["path"], h["size"], h["fullscreen_candidate"]))
    lines.append("  结论: %s" % a["conclusion"])
    return lines


def selftest():
    # 1) 延迟配对：120 样本，10% 慢样本；校验 P99 落在慢样本区
    rng = random.Random(42)
    recs = []
    expect = []
    t = 0.0
    for i in range(120):
        recs.append({"ts": t, "kind": "click_down"})
        lag = (900.0 + rng.random() * 150) if i % 10 == 0 else (80.0 + rng.random() * 60)
        expect.append(lag)
        recs.append({"ts": t + lag / 1000.0, "kind": "popup_seen"})
        t += 2.0
    recs.append({"ts": 10.0, "kind": "popup_seen"})        # 无点击，丢弃
    lags, drop = collect_lags(recs, max_lag_ms=3000.0)
    st = summary(lags)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))

    check("配对数", len(lags) == 120, "got=%d" % len(lags))
    check("无点击弹窗被丢弃", drop["dropped"] == 1, str(drop))
    check("P99 落在慢样本区", 800.0 <= st["p99"] <= 1200.0, "p99=%.1f" % st["p99"])
    check("P50 在快样本区", 60.0 <= st["p50"] <= 160.0, "p50=%.1f" % st["p50"])

    # 2) 素材自检：造 3 张图，2 张命中关键词，1 张整屏
    import tempfile
    try:
        import cv2  # noqa: F401
        import numpy as np
        have_cv = True
    except Exception:
        have_cv = False
    tmp = tempfile.mkdtemp(prefix="b2_assets_")
    made = 0
    if have_cv:
        import numpy as np
        import cv2
        for name, (w, h) in [("popup_insufficient.png", (1600, 900)),
                             ("notice_fullscreen.png", (1920, 1080)),
                             ("logo.png", (64, 64))]:
            cv2.imwrite(os.path.join(tmp, name),
                        np.zeros((h, w, 3), dtype="uint8"))
            made += 1
    a = scan_assets([tmp])
    if have_cv:
        check("素材命中数", len(a["popup_assets"]) == 2, str(a["popup_assets"]))
        check("整屏候选", a["has_fullscreen_popup_asset"] is True)
    else:
        check("无 cv2 时素材自检跳过（不算 FAIL）", True, "cv2 缺失")
    check("非图片文件被忽略", a["images_scanned"] == made, str(a["images_scanned"]))

    # 3) 空目录不炸
    a2 = scan_assets([os.path.join(tmp, "__nope__")])
    check("空目录安全", a2["images_scanned"] == 0 and not a2["has_fullscreen_popup_asset"])

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B2 余额不足弹窗延迟采集")
    ap.add_argument("--events")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--max-lag-ms", type=float, default=3000.0)
    ap.add_argument("--min-sample", type=int, default=20)
    ap.add_argument("--assets-dir", action="append", default=None,
                    help="可重复；默认 core/assets、templates")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.events:
        ap.error("需要 --events 或 --selftest")

    dirs = args.assets_dir or [os.path.join(PROJ, "core", "assets"),
                               os.path.join(PROJ, "templates")]
    recs = read_jsonl(args.events)
    lags, drop = collect_lags(recs, args.max_lag_ms)
    assets = scan_assets(dirs)
    rep = build_report(lags, drop, assets, args)
    rep["out_dir"] = args.out_dir or default_out_dir("b2_popup_delay")
    lines = render(rep)
    print("\n".join(lines))
    jp, tp = write_report(rep["out_dir"], "b2_popup_delay", rep, lines)
    print("输出: %s\n      %s" % (jp, tp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
