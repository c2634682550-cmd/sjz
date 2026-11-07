# -*- coding: utf-8 -*-
"""配对实验：det+rec（现状） vs rec-only（单行引擎），同进程交替计时。

结论看三点：准确率是否掉、文本是否一致、耗时比值。
"""
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "base"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from rapidocr import RapidOCR

from core import cards, ocr, ocr_chain, ocr_rapid, vision

FIX = os.path.join(SNAP, "tests", "fixtures")


def load(name):
    return cv2.imdecode(np.fromfile(os.path.join(FIX, name), dtype=np.uint8), cv2.IMREAD_COLOR)


def scale(img, f):
    return img if f == 1 else cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def txt_of(res):
    t = getattr(res, "txts", None) or ()
    return " ".join(x for x in t if x)


det, rec = ocr_rapid._model_paths()
std = RapidOCR(params={**ocr_rapid._PARAMS, **({"Det.model_path": det} if det else {}),
                       **({"Rec.model_path": rec} if rec else {})})
line = RapidOCR(params={**ocr_rapid._PARAMS, "Global.use_det": False,
                        **({"Rec.model_path": rec} if rec else {})})

std(load("price_roi/" + json.load(open(os.path.join(FIX, "price_roi", "manifest.json"), encoding="utf-8"))["items"][0]["file"]))
line(load("price_roi/" + json.load(open(os.path.join(FIX, "price_roi", "manifest.json"), encoding="utf-8"))["items"][0]["file"]))


def paired(images, rounds=5, label=""):
    a, b = [], []
    for _ in range(rounds):
        for im in images:
            t0 = time.perf_counter(); ra = std(im); a.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter(); rb = line(im); b.append((time.perf_counter() - t0) * 1000)
    med_a, med_b = statistics.median(a), statistics.median(b)
    print(f"{label}: det+rec p50={med_a:.2f}ms rec-only p50={med_b:.2f}ms "
          f"ratio={med_b/med_a:.2f} max={max(b):.1f}")
    return med_a, med_b


def accept(text):
    words = [{"text": text, "score": 1.0, "x": 0, "y": 0, "w": 1, "h": 1}] if text else []
    return ocr_chain._accept(words)[0]


# ---------------- 价格 ROI ----------------
pitems = json.load(open(os.path.join(FIX, "price_roi", "manifest.json"), encoding="utf-8"))["items"]
for f in (1, 3):
    imgs = [scale(load("price_roi/" + it["file"]), f) for it in pitems]
    bad = [(it["file"], it["digits"], txt_of(line(im))) for it, im in zip(pitems, imgs)
           if accept(txt_of(line(im))) != int(it["digits"])]
    same = sum(1 for im in imgs if txt_of(std(im)) == txt_of(line(im)))
    print(f"price x{f}: rec-only acc {len(pitems)-len(bad)}/{len(pitems)} text-same {same}/{len(pitems)} bad={bad[:3]}")
    paired(imgs, label=f"price x{f}")

# ---------------- 品阶 ROI ----------------
gitems = json.load(open(os.path.join(FIX, "grade_roi", "manifest.json"), encoding="utf-8"))["items"]
for f in (1, 3):
    imgs = [scale(load("grade_roi/" + it["file"]), f) for it in gitems]
    ok, same, miss = 0, 0, []
    for it, im in zip(gitems, imgs):
        t = txt_of(line(im))
        same += (t == txt_of(std(im)))
        key = (it.get("craft") or it.get("wear") or "")
        if key and (key in t):
            ok += 1
        elif key:
            miss.append((it["file"], key, t))
    print(f"grade x{f}: rec-only hit {ok}/{len(gitems)} text-same {same}/{len(gitems)} miss={miss[:3]}")
    paired(imgs, label=f"grade x{f}")

# ---------------- 卡片底部复核（生产最贵的一次）----------------
img = load("market/list_unfiltered.png")
rects = cards.locate_cards(img)
pieces = []
for r in rects:
    x0, y0, x1, y1 = r
    sub = img[max(0, y0 - 2):min(img.shape[0], y1 + 12), x0:x1]
    h = sub.shape[0]
    piece = vision.upscale(cards._pad_aspect(sub[int(h * 0.62):h, :]), 3)
    pieces.append(piece)
same, texts = 0, []
for p in pieces:
    a, b = txt_of(std(p)), txt_of(line(p))
    texts.append((a, b))
    same += (a == b)
print(f"card-bottom x3: text-same {same}/{len(pieces)}")
for a, b in texts:
    if a != b:
        print("  DIFF", repr(a), "|", repr(b))
paired(pieces, label="card-bottom x3")

# ---------------- 详情页四块 ----------------
img = load("market/detail_panel.png")
boxes = {"price": (180, 748, 425, 794), "grade": (110, 395, 505, 458),
         "count": (110, 458, 505, 492), "button": (160, 698, 435, 744)}
imgs = {k: (img[b[1]:b[3], b[0]:b[2]] if k == "price" else scale(img[b[1]:b[3], b[0]:b[2]], 2))
        for k, b in boxes.items()}
for k, im in imgs.items():
    a, b = txt_of(std(im)), txt_of(line(im))
    print(f"detail {k}: same={a == b} std={a!r} line={b!r}")
    paired([im], rounds=7, label=f"detail {k}")

# ---------------- ORT 线程数（交替）----------------
im = load("price_roi/" + pitems[0]["file"])
engs = {}
for th in (16, 8, 4, 2, 1):
    engs[th] = RapidOCR(params={**ocr_rapid._PARAMS,
                                "EngineConfig.onnxruntime.intra_op_num_threads": th,
                                **({"Det.model_path": det} if det else {}),
                                **({"Rec.model_path": rec} if rec else {})})
for e in engs.values():
    e(im)
res = {th: [] for th in engs}
for _ in range(25):
    for th, e in engs.items():
        t0 = time.perf_counter(); e(im); res[th].append((time.perf_counter() - t0) * 1000)
print("threads:", {th: round(statistics.median(v), 2) for th, v in res.items()})
