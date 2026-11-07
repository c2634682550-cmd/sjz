# -*- coding: utf-8 -*-
"""回填真值后的评测：与 `_ocr_real_20261107/result.json` **同结构**。

读 `labels.csv`（`price_label_export.py` 产出的那份），对每一条价格样本
**重新跑一次生产入口**：
  * 列表页样本 → 整帧 `market.scan(frame)` 一次，按顺序对齐样本；
  * 面板/详情/结算样本 → `ocr.read_words(roi)` + `ocr_chain.price_from_words`，
    tier1 取同一批词里 `field=="price"` 的块拼串再 `price_from_text`。

统计口径与 `tools/ocr_independent_real_eval.py` 完全一致：
exact / rejected / wrong / 各类分母 / lower95（Clopper-Pearson 保守近似）。

`truth` 为空的行**不进分母**。想把链路先跑通、暂时没人填真值，可以加
`--accept-candidate`：把预填候选当临时真值，输出里会打 `"dry_run": true`，
这份结果**不能**用来对外声称 0.98，只用于验证工具链。

用法
----
    python tools/price_label_eval.py --labels _price_frames/labels.csv
    python tools/price_label_eval.py --labels _price_frames/labels.csv --accept-candidate
"""
import argparse
import csv
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2                       # noqa: E402
import numpy as np               # noqa: E402

from core import market, ocr, ocr_chain   # noqa: E402


def imread(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def sha_file(p):
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def lower95(n, exact):
    """与测真实帧那份脚本同一条公式（0 错时 ≈ 1-3/n，有错时 Clopper-Pearson）。"""
    if n <= 0:
        return None
    p = exact / float(n)
    z = 1.96
    den = 1 + z * z / n
    cen = p + z * z / (2 * n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return round(max(0.0, (cen - half) / den), 4)


def cpu_pct():
    """整机 CPU 占用（1s 采样）。与旧评测脚本同口径，便于两份报告对比。"""
    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]

    def snap():
        i, k, u = FT(), FT(), FT()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
        f = lambda v: (v.hi << 32) | v.lo          # noqa: E731
        return f(i), f(k), f(u)

    a = snap()
    time.sleep(1.0)
    b = snap()
    di = b[0] - a[0]
    dt = (b[1] - a[1]) + (b[2] - a[2])
    return 0.0 if dt <= 0 else round(100.0 * (1.0 - di / float(dt)), 1)


def python_procs():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    total = 0
    for name in ("python.exe", "pythonw.exe"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq %s" % name, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, creationflags=0x08000000,
                startupinfo=si, timeout=15).stdout
        except Exception:                    # noqa: BLE001
            continue
        total += sum(1 for ln in out.splitlines() if ".exe" in ln.lower())
    return total


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def _to_int(v):
    s = str(v or "").strip().replace(",", "").replace("，", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def cls_of(val, text, page):
    if page == "list":
        if val is not None and val < 1000 and "," not in (text or ""):
            return "low_nosep"
        return "list_thousands"
    if page == "settle":
        return "settle_price"
    return "detail_price"


def main():
    ap = argparse.ArgumentParser(description="回填真值评测")
    ap.add_argument("--labels", required=True)
    ap.add_argument("--out", default="", help="result.json 路径（默认与 labels 同目录）")
    ap.add_argument("--accept-candidate", action="store_true",
                    help="truth 为空时把候选当临时真值（dry_run，不能对外用）")
    ap.add_argument("--max-words", type=int, default=0,
                    help="rows 里 words 最多保留几帧（0=全留，列表页建议 0）")
    args = ap.parse_args()

    labels = os.path.abspath(args.labels)
    base_dir = os.path.dirname(labels)
    frames_meta = {}
    for fr in read_jsonl(os.path.join(base_dir, "frames.jsonl")):
        if fr.get("frame"):
            frames_meta[fr["frame"].replace("\\", "/")] = fr

    with open(labels, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("!! labels 里没有行")
        return 2

    grouped = {}
    for r in rows:
        grouped.setdefault((r.get("frame") or "").replace("\\", "/"), []).append(r)

    ocr.reload()
    procs0 = python_procs()
    cpu0 = cpu_pct()
    out_rows = []
    t_all0 = time.perf_counter()
    for frame_rel, grp in grouped.items():
        frame_path = os.path.join(base_dir, frame_rel)
        meta = frames_meta.get(frame_rel) or {}
        page = (meta.get("page") or grp[0].get("page") or "").strip()
        img = imread(frame_path) if frame_rel else None
        ms = 0.0
        got_list = []
        if img is not None and (page == "list" or any(
                (g.get("cls") or "") in ("list_thousands", "low_nosep") for g in grp)):
            t0 = time.perf_counter()
            try:
                scanned = market.scan(img)
            except Exception:                 # noqa: BLE001
                scanned = []
            ms = (time.perf_counter() - t0) * 1000.0
            scanned = sorted(scanned, key=lambda r: (r.get("y0", 0), r.get("x0", 0)))
            got_list = [int(r["price"]) if r.get("price") is not None else None
                        for r in scanned]
        for i, g in enumerate(grp):
            truth = _to_int(g.get("truth"))
            truth_src = "manual"
            if truth is None and args.accept_candidate:
                truth = _to_int(g.get("candidate"))
                truth_src = "candidate"
            if truth is None:
                continue                     # 没填真值：不进分母
            got = None
            t1 = None
            words = []
            if got_list:
                got = got_list[i] if i < len(got_list) else None
                text = _digits(got) if got is not None else ""
            else:
                roi = os.path.join(base_dir, (g.get("roi") or "").replace("\\", "/"))
                crop = imread(roi) if g.get("roi") else img
                if crop is not None and getattr(crop, "size", 0):
                    t0 = time.perf_counter()
                    words = ocr.read_words(crop)
                    ms = (time.perf_counter() - t0) * 1000.0
                    got, tok = ocr_chain.price_from_words(words)
                    t1w = [w for w in words if w.get("field") == "price"]
                    t1, _ = ocr_chain.price_from_text("\n".join(w["text"] for w in t1w))
                    if got is None:
                        got, tok = ocr_chain.price_from_text(
                            "\n".join(w["text"] for w in words))
                    text = tok or "\n".join(w["text"] for w in words)
                else:
                    text = g.get("ocr_text") or ""
            if args.max_words and len(out_rows) >= args.max_words:
                words = []
            out_rows.append({
                "id": g.get("sample_id") or "",
                "src": frame_rel,
                "roi": g.get("roi") or "",
                "truth": truth,
                "truth_source": truth_src,
                "display": "{:,}".format(truth),
                "cls": cls_of(got, text, page),
                "page": page,
                "frame_kind": meta.get("frame_kind") or
                              ("real_panel" if page in ("list", "settle") else "real_crop"),
                "scale": meta.get("scale"),
                "right_margin": None,
                "got": got, "tier1": t1,
                "readable": got is not None,
                "rejected": got is None,
                "wrong": got is not None and got != truth,
                "silent_wrong": got is not None and got != truth,
                "fields": sorted({w.get("field") for w in words if w.get("field")}),
                "words": [{"t": w.get("text"), "f": w.get("field"),
                           "s": round(w.get("score", 0), 3)} for w in words][:60],
                "ms": round(ms, 1),
                "route": "scanner" if got_list else "panel",
            })
    eval_ms = round((time.perf_counter() - t_all0) * 1000.0, 1)

    if not out_rows:
        print("!! 没有一行填了真值（truth 全空）；想先跑通链路请加 --accept-candidate")
        return 2

    def summary(sel):
        n = len(sel)
        readable = sum(1 for r in sel if r["readable"])
        exact = sum(1 for r in sel if r["got"] == r["truth"])
        rejected = n - readable
        wrong = sum(1 for r in sel if r["wrong"])
        t1n = sum(1 for r in sel if r["tier1"] is not None)
        t1e = sum(1 for r in sel if r["tier1"] == r["truth"])
        return {"n": n, "n_truth": n, "readable": readable, "unreadable": rejected,
                "exact": exact,
                "price_exact": round(exact / n, 4) if n else None,
                "price_exact_readable": round(exact / readable, 4) if readable else None,
                "tier1_exact": t1e, "tier1_n": t1n,
                "tier1_price_exact": round(t1e / t1n, 4) if t1n else None,
                "silent_wrong_frames": wrong,
                "lower95": lower95(n, exact)}

    by_class = {}
    for r in out_rows:
        by_class.setdefault(r["cls"], []).append(r)
    by_kind = {}
    for r in out_rows:
        by_kind.setdefault(r["frame_kind"], []).append(r)

    res = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frame_set": base_dir,
        "labels": labels,
        "dry_run": bool(args.accept_candidate and any(
            r["truth_source"] == "candidate" for r in out_rows)),
        "unique_price_crops": len({r["roi"] for r in out_rows if r.get("roi")}),
        "dropped_duplicate_crops": [],
        "frames_total": len(grouped),
        "real_panel_frames": sum(1 for r in out_rows if r["frame_kind"] == "real_panel"),
        "real_crop_frames": sum(1 for r in out_rows if r["frame_kind"] == "real_crop"),
        "price_items_total": len(out_rows),
        "core_ocr_sha256": sha_file(os.path.join(ROOT, "core", "ocr.py")),
        "core_ocr_chain_sha256": sha_file(os.path.join(ROOT, "core", "ocr_chain.py")),
        "market_sha256": sha_file(os.path.join(ROOT, "core", "market.py")),
        "python_procs_before": procs0, "cpu_pct_before": cpu0,
        "python_procs_after": python_procs(), "cpu_pct_after": cpu_pct(),
        "eval_ms": eval_ms,
        "summary_all_price": summary(out_rows),
        "summary_by_class": {c: summary(v) for c, v in sorted(by_class.items())},
        "summary_by_frame_kind": {k: summary(v) for k, v in sorted(by_kind.items())},
        "summary_panel_route": summary([r for r in out_rows if r["route"] == "panel"]),
        "summary_scanner_route": summary([r for r in out_rows if r["route"] == "scanner"]),
        "summary_currency": summary([r for r in out_rows if r["cls"] == "currency"]),
        "rows": out_rows,
    }

    out_path = os.path.abspath(args.out or os.path.join(base_dir, "result.json"))
    if os.path.isdir(out_path):
        out_path = os.path.join(out_path, "result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    s = res["summary_all_price"]
    print("frames=%d price_items=%d exact=%d/%d price_exact=%s lower95=%s wrong=%d%s"
          % (res["frames_total"], s["n"], s["exact"], s["n_truth"],
             s["price_exact"], s["lower95"], s["silent_wrong_frames"],
             "  [dry_run]" if res["dry_run"] else ""))
    for c, v in res["summary_by_class"].items():
        print("  %-16s n=%-3d exact=%-3d rate=%-6s lower95=%s"
              % (c, v["n"], v["exact"], v["price_exact"], v["lower95"]))
    for r in out_rows:
        if r["wrong"]:
            print("  MISS %-16s truth=%-9s got=%-9s t1=%-9s roi=%s"
                  % (r["id"], r["truth"], r["got"], r["tier1"], r["roi"]))
    print("结果:", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
