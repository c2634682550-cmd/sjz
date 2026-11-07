# -*- coding: utf-8 -*-
"""第三轮候选：ROI 收紧 + 前置预处理开关 + 线程数口径修正后的检测参数扫描。

Det 是 limit_type=min / limit_side_len=64：ROI 变窄不会让送进检测网络的图
变小（短边被固定抬到 64），所以「收紧 ROI」理论上省不到时间；而
`Global.use_preprocess_img` 现在是 False，打开只会多一步。
"""
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from rapidocr import RapidOCR
from core import ocr_chain, ocr_rapid

FIX = os.path.join(SNAP, "tests", "fixtures")


def load(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def scale(im, f):
    return im if f == 1 else cv2.resize(im, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def tight(im, pct=0.08):
    h, w = im.shape[:2]
    dx, dy = int(w * pct), int(h * pct)
    return im[dy:h - dy, dx:w - dx]


P = [(it, load(os.path.join(FIX, "price_roi", it["file"])))
     for it in json.load(open(os.path.join(FIX, "price_roi", "manifest.json"),
                              encoding="utf-8"))["items"]]
G = [(it, load(os.path.join(FIX, "grade_roi", it["file"])))
     for it in json.load(open(os.path.join(FIX, "grade_roi", "manifest.json"),
                              encoding="utf-8"))["items"]]


def eng(**over):
    det, rec = ocr_rapid._model_paths()
    p = {**ocr_rapid._PARAMS, **over}
    p["EngineConfig.onnxruntime.intra_op_num_threads"] = 4
    p["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
    if det: p["Det.model_path"] = det
    if rec: p["Rec.model_path"] = rec
    return RapidOCR(params=p)


def words(e, im):
    r = e(im)
    t = getattr(r, "txts", None) or ()
    return [{"text": x, "score": 1.0, "x": 0, "y": 0, "w": 1, "h": 1} for x in t if x]


def evaluate(label, crop=None, **over):
    e = eng(**over)
    e(P[0][1])
    okp, okg, tp, tg = 0, 0, [], []
    for f in (1, 3):
        for it, im in P:
            big = scale(crop(im) if crop else im, f)
            t0 = time.perf_counter(); ws = words(e, big); tp.append((time.perf_counter() - t0) * 1000)
            if ocr_chain._accept(ws)[0] == int(it["digits"]):
                okp += 1
        for it, im in G:
            big = scale(crop(im) if crop else im, f)
            t0 = time.perf_counter(); ws = words(e, big); tg.append((time.perf_counter() - t0) * 1000)
            txt = " ".join(w["text"] for w in ws)
            key = it.get("craft") or it.get("wear") or ""
            if key and key in txt:
                okg += 1
    print(f"{label:26s} price {okp}/42 grade {okg}/58 | all p50={statistics.median(tp):.1f}ms "
          f"x1={statistics.median(tp[:len(P)]):.1f} x3={statistics.median(tp[len(P):]):.1f}")


print(f"[snap] {SNAP}  price={len(P)} grade={len(G)}")
# 交替跑两轮，压掉「第一次建 session / arena 增长」造成的顺序偏差
for i in (1, 2):
    evaluate(f"baseline #{i}")
    evaluate(f"preprocess_img=True #{i}", **{"Global.use_preprocess_img": True})
evaluate("roi tight 8%", crop=tight)
