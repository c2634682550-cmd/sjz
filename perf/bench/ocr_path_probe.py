"""主链路 OCR 分段计时探针（离线、真夹具）。

目的：把「F4 → 交易行 → 典藏外观 → 挂单列表读到价格」这条链上每次 OCR
的**单次成本**量出来，作为 `docs/perf_ocr_path.md` 的数字来源。

用法（在仓库根）：
    .\.venv\Scripts\python.exe perf\bench\ocr_path_probe.py --rep 5

口径：
  · 每段先跑 1 次热身（模型已常驻，热身只为让 ORT 线程池热起来）；
  · 报 p50 / p95 / min / max，单位 ms；
  · 所有图片来自 tests/fixtures/market/（真机截图，1920×1080）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402

from perf.bench.common import Summary  # noqa: E402


def load(name):
    p = os.path.join(ROOT, "tests", "fixtures", "market", name)
    img = cv2.imread(p)
    if img is None:
        raise SystemExit(f"找不到夹具：{p}")
    return img


def crop(img, box):
    x, y, w, h = box
    return img[y:y + h, x:x + w]


def timed(fn, rep):
    s = Summary(warmup=1)
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        s.add((time.perf_counter() - t0) * 1000.0)
    return s.summary()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rep", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    rep = max(2, args.rep)

    frames = {n: load(n) for n in
              ("catalog.png", "detail_panel.png", "filter_panel.png",
               "list_filtered.png", "list_unfiltered.png")}

    from core import market, ocr, page_check, vision

    out = {"meta": {"rep": rep, "git": _git_rev()}, "shapes": {},
           "segments": {}}

    for n, im in frames.items():
        out["shapes"][n] = list(im.shape)

    # ---- 1. 整帧 OCR（where()/_read_text 的真实成本）----
    for n in ("catalog.png", "list_unfiltered.png", "list_filtered.png",
              "detail_panel.png", "filter_panel.png"):
        im = frames[n]
        out["segments"][f"ocr_full:{n}"] = timed(
            lambda im=im: ocr.read_words(im), rep)

    # ---- 2. 顶部页签栏 ROI（0..110 高 / x 100..700）----
    tab = crop(frames["catalog.png"], (100, 0, 600, 110))
    out["segments"]["ocr_tab_roi_600x110"] = timed(
        lambda: ocr.read_words(tab), rep)
    tab2 = crop(frames["catalog.png"], (100, 0, 600, 110))
    tab2 = vision.upscale(tab2, 2)
    out["segments"]["ocr_tab_roi_600x110_x2"] = timed(
        lambda: ocr.read_words(tab2), rep)

    # ---- 3. 购买按钮 ROI（nav_col._buy_button_visible 原样：180x65 ×2）----
    buy = crop(frames["catalog.png"], (1600, 850, 180, 65))
    buy2 = vision.upscale(buy, 2)
    out["segments"]["ocr_buy_btn_180x65_x2"] = timed(
        lambda: ocr.read_words(buy2), rep)

    # ---- 4. 挂单列表价格列（右列，x≈1200..1500 半屏）----
    price_col = crop(frames["list_unfiltered.png"], (1180, 300, 380, 480))
    out["segments"]["ocr_price_col_380x480"] = timed(
        lambda: ocr.read_words(price_col), rep)
    price_col2 = vision.upscale(price_col, 2)
    out["segments"]["ocr_price_col_380x480_x2"] = timed(
        lambda: ocr.read_words(price_col2), rep)

    # ---- 5. 列表扫描（locate_cards + 逐卡 OCR，带 memo 的稳态与实际）----
    for n in ("list_unfiltered.png", "list_filtered.png"):
        im = frames[n]

        def _scan(im=im):
            sc = market.Scanner(upscale=1, read_text=None)
            return sc.scan(im)
        out["segments"][f"market_scan_cold:{n}"] = timed(_scan, 3)

    # 同一帧第二次（memo 命中）
    sc = market.Scanner(upscale=1)
    im = frames["list_unfiltered.png"]
    sc.scan(im)
    out["segments"]["market_scan_memo"] = timed(lambda: sc.scan(im), rep)

    # ---- 6. 页面判定（纯文本，不含 OCR）----
    words = ocr.read_words(frames["list_unfiltered.png"])
    text = "".join(w.get("text", "") for w in words)
    out["segments"]["classify_python"] = timed(
        lambda: page_check.page_kind(words=words), rep)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)


def _git_rev():
    try:
        import subprocess
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=False).stdout.strip()
    except Exception:                            # noqa: BLE001
        return ""


if __name__ == "__main__":
    main()
