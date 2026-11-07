# -*- coding: utf-8 -*-
"""OCR 性能基准：三级耗时 / 单件分项 / 吞吐 / CPU 占用。

所有测量在 perf/base 冻结快照里跑（sys.path 指向快照），脚本只读夹具。
用法（cwd 任意）：
  python perf/bench/bench_ocr.py tiers --rounds 5 --label baseline
  python perf/bench/bench_ocr.py item  --rounds 5 --label baseline
  python perf/bench/bench_ocr.py scan  --rounds 5 --label baseline
  python perf/bench/bench_ocr.py cpu   --scan 10 --label baseline
"""
import argparse
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# perf/base 被并行会话改写过，OCR 专项独立冻结在 perf/ocr/base（附 SNAPSHOT.sha256）
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "base"))
sys.path.insert(0, SNAP)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from core import cards, digit_ocr, market, ocr, ocr_chain, ocr_ppocr, ocr_rapid  # noqa: E402

FIX = os.path.join(SNAP, "tests", "fixtures")
OUT = os.path.join(BASE, "ocr", "out")
os.makedirs(OUT, exist_ok=True)


# ---------------------------------------------------------------- 工具

def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (q / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return float(s[lo] + (s[hi] - s[lo]) * (k - lo))


def stats(vals):
    return {
        "n": len(vals),
        "p50": round(pct(vals, 50), 2),
        "p95": round(pct(vals, 95), 2),
        "max": round(max(vals), 2) if vals else 0.0,
        "mean": round(statistics.fmean(vals), 2) if vals else 0.0,
        "min": round(min(vals), 2) if vals else 0.0,
    }


def best_round(round_lists):
    """每轮先取中位数，再取各轮里最好的一轮 —— 抵消并行会话的 CPU 抖动。"""
    meds = [statistics.median(r) for r in round_lists if r]
    return round(min(meds), 2) if meds else 0.0


def load(name):
    p = os.path.join(FIX, name)
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def price_items():
    with open(os.path.join(FIX, "price_roi", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)["items"]


def grade_items():
    with open(os.path.join(FIX, "grade_roi", "manifest.json"), encoding="utf-8") as f:
        return json.load(f)["items"]


def _scale(img, factor):
    if factor == 1:
        return img
    return cv2.resize(img, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)


class CallCounter:
    """包住 ocr 的三个入口，数调用次数（不侵入快照代码）。"""

    def __init__(self):
        self.n = {"words": 0, "text": 0, "price": 0}
        self.orig = {}

    def __enter__(self):
        for name, key in (("read_words", "words"), ("read_text", "text"),
                          ("read_price", "price")):
            fn = getattr(ocr, name)
            setattr(ocr, name, self._wrap(fn, key))
            self.orig[name] = fn
        return self

    def _wrap(self, fn, key):
        def inner(*a, **kw):
            self.n[key] += 1
            return fn(*a, **kw)
        return inner

    def __exit__(self, *exc):
        for k, v in self.orig.items():
            setattr(ocr, k, v)
        return False


# ---------------------------------------------------------------- 三级

def cmd_tiers(args):
    items = price_items()
    imgs = [load(os.path.join("price_roi", it["file"])) for it in items]
    want = [int(it["digits"]) for it in items]
    rounds = args.rounds

    rapid = ocr_rapid.make({"enabled": True})

    def warm(engine, ims):
        for _ in range(2):
            for im in ims:
                engine.read_words(im)

    # ---- 一级：主引擎，原图 ----
    warm(rapid, imgs)
    t1, ok1, r1 = [], 0, []
    for r in range(rounds):
        row = []
        for im, w in zip(imgs, want):
            t0 = time.perf_counter()
            words = rapid.read_words(im)
            dt = (time.perf_counter() - t0) * 1000
            t1.append(dt); row.append(dt)
            value = ocr_chain._accept(words)[0]
            if r == 0 and value == w:
                ok1 += 1
        r1.append(row)
    # ---- 二级：3× 放大 + PP-OCR 服务（禁用客户端缓存，否则第 2 轮起全是缓存命中）----
    pp = ocr_ppocr.make({"mode": "http",
                         "url": ocr.current_url() or "http://127.0.0.1:8081",
                         "cache": 0, "timeout_s": 10.0})
    tier2 = {"available": bool(pp.available())}
    up_ms, pp_ms, t2, ok2 = [], [], [], 0
    if tier2["available"]:
        pp.read_words(_scale(imgs[0], 3))            # 预热（含服务首次推理）
        for r in range(rounds):
            for im, w in zip(imgs, want):
                t0 = time.perf_counter()
                big = _scale(im, 3)
                up_ms.append((time.perf_counter() - t0) * 1000)
                t1_ = time.perf_counter()
                words = pp.read_words(big)
                pp_ms.append((time.perf_counter() - t1_) * 1000)
                t2.append((time.perf_counter() - t0) * 1000)
                value = ocr_chain._accept(words)[0]
                if r == 0 and value == w:
                    ok2 += 1
    # ---- 三级：自建字模 ----
    tpl = digit_ocr
    tpl.read_detail(imgs[0])
    t3, ok3, r3 = [], 0, []
    for r in range(rounds):
        row = []
        for im, w in zip(imgs, want):
            t0 = time.perf_counter()
            value, _tok, _m = tpl.read_detail(im)
            dt = (time.perf_counter() - t0) * 1000
            t3.append(dt); row.append(dt)
            if r == 0 and value == w:
                ok3 += 1
        r3.append(row)
    res = {
        "n_items": len(items),
        "rounds": rounds,
        "tier1_rapid_1x": {**stats(t1), "p50_best_round": best_round(r1),
                           "acc": f"{ok1}/{len(items)}"},
        "tier2_pp_3x": {**stats(t2), "acc": f"{ok2}/{len(items)}",
                        "available": tier2["available"],
                        "upscale": stats(up_ms), "pp_only": stats(pp_ms)},
        "tier3_glyph": {**stats(t3), "p50_best_round": best_round(r3),
                        "acc": f"{ok3}/{len(items)}"},
    }
    save(args.label, "tiers", res)
    return res


# ---------------------------------------------------------------- 单件分项

# detail_panel.png 是 590×860 的详情弹窗夹具，按像素裁四块
DETAIL = {
    "price": (180, 748, 425, 794),      # 底部按钮价格，1× 直读
    "grade": (110, 395, 505, 458),      # 稀有度 + 成色两行
    "count": (110, 458, 505, 492),      # 淘汰计数
    "button": (160, 698, 435, 744),     # 「剩余：9分37秒」
}


def _crop(img, box):
    x0, y0, x1, y1 = box
    return img[y0:y1, x0:x1]


def cmd_item(args):
    img = load(os.path.join("market", "detail_panel.png"))
    rounds = args.rounds
    price_img = _crop(img, DETAIL["price"])
    grade_img = _scale(_crop(img, DETAIL["grade"]), 2)
    count_img = _scale(_crop(img, DETAIL["count"]), 2)
    button_img = _scale(_crop(img, DETAIL["button"]), 2)

    # 先确认每块到底读出什么，读数不对就不值得计时
    probe = {
        "price": ocr.read_price(price_img),
        "grade": ocr.read_text(grade_img),
        "count": ocr.read_text(count_img),
        "button": ocr.read_text(button_img),
    }

    times = {"price": [], "grade": [], "count": [], "button": []}
    rtimes = {"price": [], "grade": [], "count": [], "button": []}
    for _ in range(rounds):
        for key, fn, im in (
                ("price", ocr.read_price, price_img),
                ("grade", ocr.read_text, grade_img),
                ("count", ocr.read_text, count_img),
                ("button", ocr.read_text, button_img)):
            t0 = time.perf_counter()
            fn(im)
            dt = (time.perf_counter() - t0) * 1000
            times[key].append(dt); rtimes[key].append(dt)

    total = [sum(x) for x in zip(*times.values())]
    res = {
        "fixture": "market/detail_panel.png",
        "probe": probe,
        "categories": {k: {**stats(v), "best": round(min(v), 2)}
                       for k, v in times.items()},
        "item_total": stats(total),
        "item_total_best": round(min(total), 2),
        "item_ocr_calls": 4,
    }
    save(args.label, "item", res)
    return res


# ---------------------------------------------------------------- 吞吐

def cmd_scan(args):
    imgs = [("list_unfiltered", load(os.path.join("market", "list_unfiltered.png"))),
            ("list_filtered", load(os.path.join("market", "list_filtered.png")))]
    rounds = args.rounds
    out = {}
    for name, img in imgs:
        # 预热一次（模型/缓存/ORT 分配）
        market.scan(img)
        vals, rows_n, calls = [], [], []
        for _ in range(rounds):
            with CallCounter() as c:
                t0 = time.perf_counter()
                rows = market.scan(img)
                dt = (time.perf_counter() - t0) * 1000
            vals.append(dt)
            rows_n.append(len(rows))
            calls.append(dict(c.n))
        ms = statistics.fmean(vals)
        per_scan_calls = statistics.fmean(sum(c.values()) for c in calls)
        out[name] = {
            "ms": stats(vals),
            "rows": rows_n,
            "products_per_sec": round(statistics.fmean(rows_n) / (ms / 1000.0), 3),
            "ocr_calls_per_scan": round(per_scan_calls, 2),
            "ocr_calls_per_product": round(per_scan_calls / max(1, statistics.fmean(rows_n)), 2),
        }
    # 帧间差分：同一帧连跑两次，第二次应当只认变化的卡
    img = imgs[0][1]
    sc = market.Scanner()
    diff = {}
    try:
        with CallCounter() as c:
            sc.scan(img)
            diff["frame1_calls"] = dict(c.n)
        with CallCounter() as c:
            sc.scan(img.copy())
            diff["frame2_calls"] = dict(c.n)
    except Exception as exc:                       # 基线里这条路径是坏的，要如实记录
        diff["error"] = f"{type(exc).__name__}: {exc}"
    diff["cached_products"] = len(sc.cache)

    # 生产路径：Scanner 带差分缓存，六张卡每帧都变（价格刷新时的最坏情况）。
    # 同帧连跑会被缓存全部跳过（0 次 OCR），必须每帧在卡片右侧留白处点块。
    rects = cards.locate_cards(img)

    def perturb(k):
        out = img.copy()
        for x0, y0, x1, y1 in rects:
            cy = (y0 + y1) // 2
            out[cy - 12:cy + 12, x1 - 30:x1 - 6] = (255, 255, 255) if k % 2 else (0, 0, 0)
        return out

    sc2 = market.Scanner()
    sc2.scan(perturb(0))
    sc2.scan(perturb(1))
    vals2, calls2 = [], []
    for i in range(rounds):
        with CallCounter() as c:
            t0 = time.perf_counter()
            rows = sc2.scan(perturb(i + 2))
            vals2.append((time.perf_counter() - t0) * 1000)
            calls2.append(sum(c.n.values()))
    res = {"images": out, "diff_skip": diff,
           "scanner_parallel": {
               "parallel": getattr(sc2, "parallel", 1),
               "ms": stats(vals2),
               "rows": len(rows),
               "ocr_calls_per_frame": calls2,
               "products_per_sec": round(len(rows) / (statistics.fmean(vals2) / 1000.0), 3),
           }}
    save(args.label, "scan", res)
    return res


# ---------------------------------------------------------------- CPU

def cmd_cpu(args):
    img = load(os.path.join("market", "list_unfiltered.png"))
    market.scan(img)                                  # 预热
    n = args.scan
    wall0, cpu0 = time.perf_counter(), time.process_time()
    for _ in range(n):
        market.scan(img)
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    res = {
        "scans": n,
        "wall_s": round(wall, 3),
        "cpu_s": round(cpu, 3),
        "cpu_percent_of_one_core": round(cpu / wall * 100.0, 1),
        "effective_cores": round(cpu / wall, 2),
        "cpu_ms_per_scan": round(cpu / n * 1000.0, 1),
        "wall_ms_per_scan": round(wall / n * 1000.0, 1),
        "logical_cpus": os.cpu_count(),
    }
    save(args.label, "cpu", res)
    return res


# ---------------------------------------------------------------- 汇总

def save(label, section, data):
    path = os.path.join(OUT, f"{label}.json")
    blob = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
        except ValueError:
            blob = {}
    blob[section] = data
    blob.setdefault("meta", {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "snapshot": SNAP,
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(blob, f, ensure_ascii=False, indent=1)
    print(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"[saved] {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tiers", "item", "scan", "cpu", "all"])
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--scan", type=int, default=10)
    ap.add_argument("--label", default="baseline")
    a = ap.parse_args()
    if a.cmd in ("tiers", "all"):
        print("== tiers =="); cmd_tiers(a)
    if a.cmd in ("item", "all"):
        print("== item =="); cmd_item(a)
    if a.cmd in ("scan", "all"):
        print("== scan =="); cmd_scan(a)
    if a.cmd in ("cpu", "all"):
        print("== cpu =="); cmd_cpu(a)


if __name__ == "__main__":
    main()
