# -*- coding: utf-8 -*-
"""检测参数扫描：limit_side_len / unclip_ratio 对准确率与耗时的影响。"""
import copy
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


pitems = json.load(open(os.path.join(FIX, "price_roi", "manifest.json"), encoding="utf-8"))["items"]
gitems = json.load(open(os.path.join(FIX, "grade_roi", "manifest.json"), encoding="utf-8"))["items"]

P = [(it, load(os.path.join(FIX, "price_roi", it["file"]))) for it in pitems]
G = [(it, load(os.path.join(FIX, "grade_roi", it["file"]))) for it in gitems]


def eng(**over):
    det, rec = ocr_rapid._model_paths()
    p = {**ocr_rapid._PARAMS, **over}
    if det: p["Det.model_path"] = det
    if rec: p["Rec.model_path"] = rec
    return RapidOCR(params=p)


def words(e, im):
    r = e(im)
    t = getattr(r, "txts", None) or ()
    return [{"text": x, "score": 1.0, "x": 0, "y": 0, "w": 1, "h": 1} for x in t if x]


def evaluate(label, **over):
    e = eng(**over)
    e(load(os.path.join(FIX, "price_roi", pitems[0]["file"])))
    okp, okg, tp, tg = 0, 0, [], []
    for f in (1, 3):
        for it, im in P:
            big = scale(im, f)
            t0 = time.perf_counter(); ws = words(e, big); tp.append((time.perf_counter() - t0) * 1000)
            if ocr_chain._accept(ws)[0] == int(it["digits"]):
                okp += 1
        for it, im in G:
            big = scale(im, f)
            t0 = time.perf_counter(); ws = words(e, big); tg.append((time.perf_counter() - t0) * 1000)
            txt = " ".join(w["text"] for w in ws)
            key = it.get("craft") or it.get("wear") or ""
            if key and key in txt:
                okg += 1
    print(f"{label}: price {okp}/42 grade {okg}/58 | all p50={statistics.median(tp):.1f}ms "
          f"x1={statistics.median(tp[:len(P)]):.1f} x3={statistics.median(tp[len(P):]):.1f}")


evaluate("baseline 64/1.6", )
evaluate("limit 48", **{"Det.limit_side_len": 48})
evaluate("limit 32", **{"Det.limit_side_len": 32})
evaluate("limit 96", **{"Det.limit_side_len": 96})
evaluate("unclip 1.2", **{"Det.unclip_ratio": 1.2})
evaluate("unclip 1.0", **{"Det.unclip_ratio": 1.0})
evaluate("thresh .2", **{"Det.thresh": 0.2})
