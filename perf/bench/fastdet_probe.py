"""原型验证：整帧 OCR 用「降采样 det + 原图 rec」能省多少、丢多少字。

做法：
  1. 用 tests/fixtures/market 里的真机 ROI 拼一张 1920×1080 的合成页面
     （黑底 + 原像素粘贴，内容是真的，尺寸是整屏）；
  2. baseline = RapidOCR 原样整帧；
  3. fast     = det 在 scale 倍图上跑，框映射回原图，按框裁原像素再 rec；
  4. 对比：耗时、词数、拼串、关键锚点（推荐/第N页/相似皮/编号/典藏外观）。

这不是产品代码，只用来定方案；确定后再落进 core/ocr_rapid.py。
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from perf.bench.common import Summary  # noqa: E402

ANCHORS = ("推荐", "第", "相似皮", "编号", "典藏外观", "典藏工坊", "市场流通量",
           "在售", "筛选", "价格")


def load(name):
    return cv2.imread(os.path.join(ROOT, "tests", "fixtures", "market", name))


def canvas():
    c = np.zeros((1080, 1920, 3), np.uint8)
    lst = load("list_unfiltered.png")          # 1380×630
    cat = load("catalog.png")                  # 410×810
    c[240:240 + lst.shape[0], 40:40 + lst.shape[1]] = lst
    c[150:150 + cat.shape[0], 1460:1460 + cat.shape[1]] = cat
    return c


def fast_words(eng, img, scale):
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    det = eng.text_det(small)
    boxes = getattr(det, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []
    crops, rects = [], []
    h, w = img.shape[:2]
    for pts in boxes:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        x0, y0 = int(min(xs) / scale), int(min(ys) / scale)
        x1, y1 = int(max(xs) / scale), int(max(ys) / scale)
        pad = 2
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        rects.append((x0, y0, x1, y1))
        crops.append(img[y0:y1, x0:x1])
    if not crops:
        return []
    rec = eng.text_rec(TextRecInput(img=crops))
    txts = getattr(rec, "txts", ()) or ()
    scores = getattr(rec, "scores", ()) or ()
    out = []
    for i, t in enumerate(txts):
        t = (t or "").strip()
        if not t or i >= len(rects):
            continue
        x0, y0, x1, y1 = rects[i]
        out.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                    "text": t, "score": float(scores[i]) if i < len(scores) else 1.0})
    return out


def timed(fn, rep=3):
    s = Summary(warmup=1)
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        s.add((time.perf_counter() - t0) * 1000.0)
    return s.summary()


def main():
    from core import ocr_rapid
    be = ocr_rapid.make({})
    eng = be._engine()
    img = canvas()

    base_words = be.read_words(img)
    base_text = "".join(w["text"] for w in base_words)

    out = {"shape": list(img.shape), "baseline": {
        "p50": timed(lambda: be.read_words(img))["p50"],
        "words": len(base_words), "chars": len(base_text),
        "anchors": [a for a in ANCHORS if a in base_text]},
        "by_scale": {}}

    for scale in (0.75, 0.6, 0.5, 0.4, 0.3):
        words = fast_words(eng, img, scale)
        text = "".join(w["text"] for w in words)
        out["by_scale"][f"{scale:g}"] = {
            "p50": timed(lambda s=scale: fast_words(eng, img, s))["p50"],
            "words": len(words), "chars": len(text),
            "anchors": [a for a in ANCHORS if a in text],
            "lost_anchors": [a for a in ANCHORS if a in base_text and a not in text],
            "text_sample": text[:60],
        }

    # 页码 ROI：6× 放大的真实成本
    x, y, w, h = 470, 915, 540, 60
    crop = img[y:y + h, x:x + w]
    big = cv2.resize(crop, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
    out["page_no_roi_x6"] = {
        "shape": list(big.shape),
        "p50": timed(lambda: be.read_words(big))["p50"],
        "text": "".join(w["text"] for w in be.read_words(big)),
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
