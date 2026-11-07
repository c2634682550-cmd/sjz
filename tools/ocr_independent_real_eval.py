# -*- coding: utf-8 -*-
"""price_exact 真实独立帧集重测（扩帧版：只统计真实素材，不掺派生帧）。

独立性口径：
  1. 每个裁片按**像素 sha256** 去重，重复像素只保留一份；
  2. 每份裁片在整套帧集里**只出现一次**，帧间不复用；
  3. 每帧只调用一次生产入口（面板走 `ocr.read_words`，列表走 `market.scan`）。

真实素材清单（全部来自真实截图，无合成价格）：
  * tests/fixtures/price_roi 去重后的 20 个裁片（14 价格 + 6 货币）
  * tests/fixtures/market/detail_panel.png  1 张真实详情面板
  * tests/fixtures/market/list_unfiltered.png 1 张真实列表页（6 个价格）
  * tests/fixtures/market/list_filtered.png   1 张真实列表页（滚动后 4 个价格）

分类：list_thousands / detail_price / low_nosep / right_edge，外加 currency（非价格单列）。
真实面板帧与裁片帧分开统计，不混算。
"""
import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import cv2
import numpy as np

from core import market, ocr, ocr_chain

PANEL = os.path.join(PROJ, "tests", "fixtures", "market", "detail_panel.png")
LIST_U = os.path.join(PROJ, "tests", "fixtures", "market", "list_unfiltered.png")
LIST_F = os.path.join(PROJ, "tests", "fixtures", "market", "list_filtered.png")
PRICE_DIR = os.path.join(PROJ, "tests", "fixtures", "price_roi")

BAND_PRICE = (262, 760, 380, 806)
PRICE_CX, PRICE_CY = 316, 782

# 真值 = 原图肉眼复核 + tests/test_market_video.py 的 UNFILTERED/FILTERED 常量
LIST_TRUTH = {
    "list_unfiltered.png": [25000, 17000, 28888, 16000, 9000, 9500],
    "list_filtered.png": [2050, 2050, 2050, 2050, 2100, 2500],
}

CURRENCY_IDS = {"u2_top_2049", "u2_top_0", "u2_top_8",
                "u3_top_2049", "u3_top_0", "u3_top_8"}
DETAIL_IDS = {"u3_detail_price"}


def imread(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def build_plate(panel):
    img = panel.copy()
    mask = np.zeros(img.shape[:2], np.uint8)
    x0, y0, x1, y1 = BAND_PRICE
    mask[y0:y1, x0:x1] = 255
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    return cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)


def paste(img, crop, x, y):
    h, w = img.shape[:2]
    ch, cw = crop.shape[:2]
    x1, y1 = min(w, x + cw), min(h, y + ch)
    if x1 <= x or y1 <= y:
        return img
    img[y:y1, x:x1] = crop[:y1 - y, :x1 - x]
    return img


def resize(img, scale):
    if scale == 1.0:
        return img
    h, w = img.shape[:2]
    return cv2.resize(img, (int(round(w * scale)), int(round(h * scale))),
                      interpolation=cv2.INTER_AREA)


def unique_items():
    with open(os.path.join(PRICE_DIR, "manifest.json"), encoding="utf-8") as f:
        items = json.load(f)["items"]
    seen, keep, dropped = {}, [], []
    for it in sorted(items, key=lambda x: x["id"]):
        p = os.path.join(PRICE_DIR, it["file"])
        h = sha(open(p, "rb").read())
        if h in seen:
            dropped.append({"id": it["id"], "same_as": seen[h]})
            continue
        seen[h] = it["id"]
        keep.append(it)
    return keep, dropped


def cpu_pct():
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
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % name, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000,
            startupinfo=si, timeout=15).stdout
        total += sum(1 for ln in out.splitlines() if ".exe" in ln.lower())
    return total


def lower95(n, exact):
    """0 错时的 95% 下界近似 1-3/n；有错时用 Clopper-Pearson 近似（保守）。"""
    if n <= 0:
        return None
    p = exact / float(n)
    z = 1.96
    den = 1 + z * z / n
    cen = p + z * z / (2 * n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return round(max(0.0, (cen - half) / den), 4)


def ink_right_margin(crop):
    """价格裁片里最右墨迹到右边界还剩几像素。<=2 视为右缘贴边。"""
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    cols = np.where((g > 140).sum(axis=0) > 0)[0]
    if len(cols) == 0:
        return None
    return int(crop.shape[1] - 1 - cols.max())


def cls_of(item, margin):
    i = item["id"]
    if i in CURRENCY_IDS:
        return "currency"
    if i in DETAIL_IDS:
        return "detail_price"
    if margin is not None and margin <= 2:
        return "right_edge"           # 末位贴边，静默错价的高危类
    if int(item["digits"]) < 1000 and "," not in item["display"]:
        return "low_nosep"
    return "list_thousands"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"C:\Users\26346\Documents\Trae-CN\temp_20261107_117\_ocr_real_20261107")
    args = ap.parse_args()
    out_dir = os.path.abspath(args.out)
    corpus = os.path.join(out_dir, "corpus")
    os.makedirs(corpus, exist_ok=True)

    procs0 = python_procs()
    cpu0 = cpu_pct()

    keep, dropped = unique_items()
    panel = imread(PANEL)
    plate = build_plate(panel)

    frames = []      # 面板路线：每帧只读一次
    for i, it in enumerate(keep):
        crop = imread(os.path.join(PRICE_DIR, it["file"]))
        margin = ink_right_margin(crop)
        img = paste(plate.copy(), crop, PRICE_CX - crop.shape[1] // 2,
                    PRICE_CY - crop.shape[0] // 2)
        sc = (1.0, 1.25, 1.5)[i % 3]
        img = resize(img, sc)
        fn = os.path.join(corpus, "%02d_%s_s%d.png" % (i, it["id"], int(sc * 100)))
        cv2.imencode(".png", img)[1].tofile(fn)
        frames.append({"id": it["id"], "file": fn, "truth": int(it["digits"]),
                       "display": it["display"], "cls": cls_of(it, margin),
                       "right_margin": margin,
                       "frame_kind": "real_crop", "scale": sc,
                       "src": os.path.join("tests", "fixtures", "price_roi", it["file"]),
                       "frame_sha": sha(open(fn, "rb").read()), "route": "panel"})

    # 真实详情面板（原图不改）
    fn = os.path.join(corpus, "REALPANEL.png")
    cv2.imencode(".png", panel)[1].tofile(fn)
    frames.append({"id": "REALPANEL", "file": fn, "truth": 25000,
                   "display": "25,000", "cls": "detail_price",
                   "frame_kind": "real_panel", "scale": 1.0,
                   "src": os.path.join("tests", "fixtures", "market", "detail_panel.png"),
                   "frame_sha": sha(open(fn, "rb").read()), "route": "panel"})

    # 列表页两帧：整帧只扫一次，每个价格是一条样本
    list_frames = []
    for name in ("list_unfiltered.png", "list_filtered.png"):
        p = os.path.join(PROJ, "tests", "fixtures", "market", name)
        h = sha(open(p, "rb").read())
        list_frames.append({"name": name, "file": p, "sha": h,
                            "truth": LIST_TRUTH[name]})

    hs = [f["frame_sha"] for f in frames] + [lf["sha"] for lf in list_frames]
    assert len(set(hs)) == len(hs), "帧集出现重复像素"
    assert len(frames) - 1 == len(set(f["src"] for f in frames if f["frame_kind"] == "real_crop")), "裁片被复用"
    for lf in list_frames:
        assert lf["sha"] not in [f["frame_sha"] for f in frames], "列表帧与其它帧重复"

    ocr.reload()
    gray = np.full((860, 590, 3), 32, np.uint8)
    ocr.read_words(gray)          # 预热，不计入样本

    rows = []
    for f in frames:
        img = imread(f["file"])
        t0 = time.perf_counter()
        words = ocr.read_words(img)              # 每帧只读一次
        ms = (time.perf_counter() - t0) * 1000.0
        val, _tok = ocr_chain.price_from_words(words)
        t1w = [w for w in words if w.get("field") == "price"]
        t1, _ = ocr_chain.price_from_text("\n".join(w["text"] for w in t1w))
        rows.append({
            "id": f["id"], "src": f["src"], "truth": f["truth"], "display": f["display"],
            "cls": f["cls"], "frame_kind": f["frame_kind"], "scale": f["scale"],
            "right_margin": f.get("right_margin"),
            "got": val, "tier1": t1, "readable": val is not None,
            "silent_wrong": val is not None and val != f["truth"],
            "fields": sorted({w.get("field") for w in words if w.get("field")}),
            "words": [{"t": w["text"], "f": w.get("field"), "s": round(w.get("score", 0), 3)}
                      for w in words],
            "ms": round(ms, 1), "route": "panel",
        })

    for lf in list_frames:
        img = imread(lf["file"])
        t0 = time.perf_counter()
        got_rows = market.scan(img)              # 整帧只扫一次
        ms = (time.perf_counter() - t0) * 1000.0
        got_rows = sorted(got_rows, key=lambda r: (r.get("y0", 0), r.get("x0", 0)))
        got = [int(r["price"]) if r.get("price") is not None else None for r in got_rows]
        truth = lf["truth"]
        n = max(len(truth), len(got))
        for i in range(n):
            tr = truth[i] if i < len(truth) else None
            gv = got[i] if i < len(got) else None
            rows.append({
                "id": "%s#%d" % (lf["name"], i), "src": os.path.join("tests", "fixtures", "market", lf["name"]),
                "truth": tr, "display": ("{:,}".format(tr) if tr is not None else None),
                "cls": "list_thousands", "frame_kind": "real_panel", "scale": 1.0,
                "got": gv, "tier1": None, "readable": gv is not None,
                "silent_wrong": gv is not None and tr is not None and gv != tr,
                "fields": ["price"], "words": [{"t": r.get("text"), "f": "price"} for r in got_rows],
                "ms": round(ms, 1), "route": "scanner",
            })

    def summary(sel):
        n = len(sel)
        readable = sum(1 for r in sel if r["readable"])
        with_truth = [r for r in sel if r["truth"] is not None]
        exact = sum(1 for r in with_truth if r["got"] == r["truth"])
        t1 = sum(1 for r in with_truth if r["tier1"] == r["truth"])
        t1_n = sum(1 for r in with_truth if r["tier1"] is not None)
        silent = sum(1 for r in with_truth if r["silent_wrong"])
        nt = len(with_truth)
        return {"n": n, "n_truth": nt, "readable": readable, "unreadable": nt - readable,
                "exact": exact,
                "price_exact": round(exact / nt, 4) if nt else None,
                "price_exact_readable": round(exact / readable, 4) if readable else None,
                "tier1_exact": t1, "tier1_n": t1_n,
                "tier1_price_exact": round(t1 / t1_n, 4) if t1_n else None,
                "silent_wrong_frames": silent,
                "lower95": lower95(nt, exact)}

    price_rows = [r for r in rows if r["cls"] != "currency"]
    res = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frame_set": corpus,
        "unique_price_crops": len([f for f in frames if f["frame_kind"] == "real_crop"]),
        "dropped_duplicate_crops": dropped,
        "frames_total": len(frames) + len(list_frames),
        "real_panel_frames": len([f for f in frames if f["frame_kind"] == "real_panel"]) + len(list_frames),
        "real_crop_frames": len([f for f in frames if f["frame_kind"] == "real_crop"]),
        "price_items_total": len(price_rows),
        "core_ocr_sha256": sha(open(os.path.join(PROJ, "core", "ocr.py"), "rb").read()),
        "core_ocr_chain_sha256": sha(open(os.path.join(PROJ, "core", "ocr_chain.py"), "rb").read()),
        "market_sha256": sha(open(os.path.join(PROJ, "core", "market.py"), "rb").read()),
        "python_procs_before": procs0, "cpu_pct_before": cpu0,
        "python_procs_after": python_procs(), "cpu_pct_after": cpu_pct(),
        "summary_all_price": summary(price_rows),
        "summary_by_class": {c: summary([r for r in price_rows if r["cls"] == c])
                             for c in ("list_thousands", "detail_price", "low_nosep", "right_edge")},
        "summary_by_frame_kind": {k: summary([r for r in price_rows if r["frame_kind"] == k])
                                  for k in ("real_panel", "real_crop")},
        "summary_panel_route": summary([r for r in price_rows if r["route"] == "panel"]),
        "summary_scanner_route": summary([r for r in price_rows if r["route"] == "scanner"]),
        "summary_currency": summary([r for r in rows if r["cls"] == "currency"]),
        "rows": rows,
    }
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    s = res["summary_all_price"]
    print("frames=%d price_items=%d exact=%d/%d price_exact=%s lower95=%s silent_wrong=%d" % (
        res["frames_total"], s["n"], s["exact"], s["n_truth"], s["price_exact"],
        s["lower95"], s["silent_wrong_frames"]))
    for c, v in res["summary_by_class"].items():
        print("  %-16s n=%-3d exact=%-3d rate=%-6s lower95=%s" % (c, v["n"], v["exact"], v["price_exact"], v["lower95"]))
    for k, v in res["summary_by_frame_kind"].items():
        print("  %-12s n=%-3d exact=%-3d rate=%-6s" % (k, v["n"], v["exact"], v["price_exact"]))
    for r in rows:
        if r["truth"] is not None and r["got"] != r["truth"]:
            print("  MISS %-24s truth=%-9s got=%-9s t1=%-9s src=%s" %
                  (r["id"], r["truth"], r["got"], r["tier1"], r["src"]))
    print("结果:", os.path.join(out_dir, "result.json"))


if __name__ == "__main__":
    main()
