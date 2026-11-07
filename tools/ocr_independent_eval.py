# -*- coding: utf-8 -*-
"""price_exact 独立帧集重测（ROI 修复后）。

独立性口径（三条，缺一条本次结果作废）：
  1. 每个价格裁片按**像素 sha256 去重**，重复内容只保留一份（u4/u5 的
     `*_card_r1_l.png` 像素完全相同，只算一个）；
  2. 每个保留裁片在整套帧集里**只出现一次**，帧之间不复用同一张裁片；
  3. 每帧只调用一次生产入口 `ocr.read_words`，不重复读、不挑最好的一次。

帧集构造：真实详情面板底板，只 inpaint 擦掉价格条，名称/成色/品阶/数量
保留原帧；把唯一的真实价格裁片按游戏原生坐标（中心 x≈316, y≈782）回贴，
再按 100/125/150% 三档缩放轮转。另加 1 帧未改动真实面板。

tier1 指标不另起一次推理：直接取同一帧单次读出的 `field=="price"` 词块，
等价于旧的「价格 ROI 窄带单独读 Rapid」，但不多读一遍。

用法：
    .venv\\Scripts\\pythonw.exe tools\\ocr_independent_eval.py --out <证据目录>
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

from core import ocr, ocr_chain

PANEL = os.path.join(PROJ, "tests", "fixtures", "market", "detail_panel.png")
PRICE_DIR = os.path.join(PROJ, "tests", "fixtures", "price_roi")

BAND_PRICE = (262, 760, 380, 806)      # 面板原生价格条
PRICE_CX, PRICE_CY = 316, 782


def imread(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def build_plate(panel):
    """只擦价格条，其余真实内容原样保留。"""
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


def unique_price_items():
    """按像素 sha256 去重；返回 (保留项, 被去重项)。"""
    with open(os.path.join(PRICE_DIR, "manifest.json"), encoding="utf-8") as f:
        items = json.load(f)["items"]
    seen, keep, dropped = {}, [], []
    for it in sorted(items, key=lambda x: x["id"]):
        p = os.path.join(PRICE_DIR, it["file"])
        h = sha_bytes(open(p, "rb").read())
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"C:\Users\26346\Documents\Trae-CN\temp_20261107_117\_ocr_indep_20261107")
    args = ap.parse_args()
    out_dir = os.path.abspath(args.out)
    corpus = os.path.join(out_dir, "corpus")
    os.makedirs(corpus, exist_ok=True)

    procs0 = python_procs()
    cpu0 = cpu_pct()

    keep, dropped = unique_price_items()
    panel = imread(PANEL)
    plate = build_plate(panel)

    frames = []
    scales = (1.0, 1.25, 1.5)
    for i, it in enumerate(keep):
        crop = imread(os.path.join(PRICE_DIR, it["file"]))
        img = paste(plate.copy(), crop, PRICE_CX - crop.shape[1] // 2,
                    PRICE_CY - crop.shape[0] // 2)
        sc = scales[i % len(scales)]
        img = resize(img, sc)
        fn = os.path.join(corpus, "%02d_%s_s%d.png" % (i, it["id"], int(sc * 100)))
        cv2.imencode(".png", img)[1].tofile(fn)
        frames.append({"id": it["id"], "file": fn, "truth": int(it["digits"]),
                       "display": it["display"], "cls": it.get("cls"),
                       "scale": sc, "src": it["file"],
                       "frame_sha": sha_bytes(open(fn, "rb").read())})
    # 1 帧未改动真实面板
    fn = os.path.join(corpus, "REALPANEL.png")
    cv2.imencode(".png", panel)[1].tofile(fn)
    frames.append({"id": "REALPANEL", "file": fn, "truth": 25000,
                   "display": "25,000", "cls": "price", "scale": 1.0,
                   "src": "detail_panel.png",
                   "frame_sha": sha_bytes(open(fn, "rb").read())})

    # 独立性自检：帧内容必须两两不同；每个源裁片只能出现一次
    hs = [f["frame_sha"] for f in frames]
    srcs = [f["src"] for f in frames]
    assert len(set(hs)) == len(hs), "帧集出现重复像素"
    assert len([s for s in srcs if s != "detail_panel.png"]) == \
        len(set(s for s in srcs if s != "detail_panel.png")), "裁片被复用"

    ocr.reload()
    # 预热：空白灰图读一次，不进样本
    gray = np.full((860, 590, 3), 32, np.uint8)
    ocr.read_words(gray)

    rows = []
    for f in frames:
        img = imread(f["file"])
        t0 = time.perf_counter()
        words = ocr.read_words(img)              # 每帧只读这一次
        ms = (time.perf_counter() - t0) * 1000.0
        val, _tok = ocr_chain.price_from_words(words)
        t1words = [w for w in words if w.get("field") == "price"]
        t1, _ = ocr_chain.price_from_text("\n".join(w["text"] for w in t1words))
        rows.append({
            "id": f["id"], "src": f["src"], "truth": f["truth"], "cls": f["cls"],
            "scale": f["scale"], "got": val, "tier1": t1,
            "readable": val is not None,
            "silent_wrong": val is not None and val != f["truth"],
            "fields": sorted({w.get("field") for w in words if w.get("field")}),
            "words": [{"t": w["text"], "f": w.get("field"), "s": round(w.get("score", 0), 3)}
                      for w in words],
            "ms": round(ms, 1),
        })

    def summary(sel):
        n = len(sel)
        readable = sum(1 for r in sel if r["readable"])
        exact = sum(1 for r in sel if r["got"] == r["truth"])
        t1 = sum(1 for r in sel if r["tier1"] == r["truth"])
        silent = sum(1 for r in sel if r["silent_wrong"])
        return {"n": n, "readable": readable, "unreadable": n - readable,
                "price_exact": round(exact / n, 4) if n else None,
                "price_exact_readable": round(exact / readable, 4) if readable else None,
                "tier1_price_exact": round(t1 / n, 4) if n else None,
                "silent_wrong_frames": silent}

    res = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frame_set": corpus,
        "unique_price_crops": len(keep),
        "dropped_duplicate_crops": dropped,
        "frames_total": len(frames),
        "price_cls_frames": sum(1 for r in rows if r["truth"] != 25000),
        "core_ocr_sha256": sha_bytes(open(os.path.join(PROJ, "core", "ocr.py"), "rb").read()),
        "core_ocr_chain_sha256": sha_bytes(open(os.path.join(PROJ, "core", "ocr_chain.py"), "rb").read()),
        "python_procs_before": procs0, "cpu_pct_before": cpu0,
        "python_procs_after": python_procs(), "cpu_pct_after": cpu_pct(),
        "summary_all": summary(rows),
        "summary_item_price": summary([r for r in rows if r["cls"] == "price"]),
        "summary_currency": summary([r for r in rows if r["cls"] == "currency"]),
        "rows": rows,
    }
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    s = res["summary_all"]
    print("frames=%d readable=%d price_exact=%s (%d/%d) tier1=%s silent_wrong=%d" % (
        s["n"], s["readable"], s["price_exact_readable"],
        sum(1 for r in rows if r["got"] == r["truth"]), s["n"],
        s["tier1_price_exact"], s["silent_wrong_frames"]))
    for r in rows:
        flag = "OK" if r["got"] == r["truth"] else ("NONE" if r["got"] is None else "MISS")
        print("  %-6s %-22s truth=%-9s got=%-9s t1=%-9s %s" %
              (flag, r["id"], r["truth"], r["got"], r["tier1"], r["fields"]))
    print("结果:", os.path.join(out_dir, "result.json"))


if __name__ == "__main__":
    main()
