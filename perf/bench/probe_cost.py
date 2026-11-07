# -*- coding: utf-8 -*-
"""静态硬件成本探针：把「一次抓屏 / 一次 OCR / 一次模板匹配」各花多少毫秒
在本机量出来，作为流程分解的单价表。

不碰游戏、不点鼠标、不开窗口：只做 mss 抓屏（被动）与本地图片上的 OCR/CV。

用法：
    .venv\\Scripts\\python.exe perf\\bench\\probe_cost.py --n 60

输出 perf/out/costs.json（单价 p50/p95）+ 控制台表格。
"""
import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))          # ...\dafors
BASE = os.path.join(PROJ, "perf", "base")              # 冻结快照
sys.path.insert(0, BASE)

from core import cards as cardmod                   # noqa: E402
from core import market, ocr                        # noqa: E402
from core import vision                             # noqa: E402
from core.nav_coords import (N_BALANCE, N_CLOCK, N_LIST,  # noqa: E402
                             N_POPUP_HINT, N_REWARD, N_SKIP)

OUT = os.path.join(PROJ, "perf", "out")
FIXTURE = os.path.join(PROJ, "tests", "fixtures", "market", "list_filtered.png")
TPL_BUY = os.path.join(PROJ, "templates", "buy.png")
TPL_CONFIRM = os.path.join(PROJ, "templates", "confirm.png")


def stat(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return {"n": 0}
    def q(p):
        return round(xs[min(n - 1, int(round(p * (n - 1))) )], 3)
    return {"n": n, "p50": q(.5), "p95": q(.95), "mean": round(sum(xs) / n, 3),
            "max": round(xs[-1], 3)}


def timeit(fn, n):
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000.0)
    return stat(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    args = ap.parse_args()
    n = args.n
    os.makedirs(OUT, exist_ok=True)

    res = {}
    frame = cv2.imread(FIXTURE)
    if frame is None:
        raise SystemExit(f"缺少真实列表帧：{FIXTURE}")
    H, W = frame.shape[:2]

    def crop_norm(nbox):
        """从真实帧上按归一化框取一块（帧只有列表区域，用整帧做底）。"""
        x = int(nbox["x"] * W); y = int(nbox["y"] * H)
        w = max(1, int(nbox["w"] * W)); h = max(1, int(nbox["h"] * H))
        x, y = min(x, W - 2), min(y, H - 2)
        w, h = min(w, W - x), min(h, H - y)
        c = frame[y:y + h, x:x + w]
        return c if c.size else frame[:h, :w]

    # ---------- 1. mss 抓屏（本机真实） ----------
    from core.screen import Screen
    scr = Screen(monitor=1)
    res["grab_full_1920x1080"] = timeit(lambda: scr.grab(None), 20)
    res["grab_list_1340x730"] = timeit(lambda: scr.grab((60, 210, 1340, 730)), n)
    res["grab_clock_300x80"] = timeit(lambda: scr.grab((1100, 600, 300, 80)), n)
    res["grab_balance_400x60"] = timeit(lambda: scr.grab((1500, 0, 400, 60)), n)
    res["grab_buy_315x67"] = timeit(lambda: scr.grab((1500, 900, 315, 67)), n)
    scr.close()

    # ---------- 2. OCR 单次成本（真实模型、真实裁剪） ----------
    c_clock = crop_norm(N_CLOCK)
    c_bal = crop_norm(N_BALANCE)
    c_hint = crop_norm(N_POPUP_HINT)
    c_rew = crop_norm(N_REWARD)
    c_skip = crop_norm(N_SKIP)
    c_name = frame[0:60, 0:300]

    res["ocr_clock_300x80_x2"] = timeit(
        lambda: ocr.read_text(vision.upscale(c_clock, 2)), n)
    res["ocr_hint_390x32_x2"] = timeit(
        lambda: ocr.read_text(vision.upscale(c_hint, 2)), n)
    res["ocr_balance_400x60_x3"] = timeit(
        lambda: ocr.read_text(vision.upscale(c_bal, 3)), n)
    res["ocr_reward_540x260_x2"] = timeit(
        lambda: ocr.read_text(vision.upscale(c_rew, 2)), 20)
    res["ocr_skip_100x55_x2"] = timeit(
        lambda: ocr.read_text(vision.upscale(c_skip, 2)), 20)
    res["ocr_namecard_300x60"] = timeit(lambda: ocr.read_text(c_name), n)
    res["ocr_words_full_1380x630"] = timeit(lambda: ocr.read_words(frame), 10)
    res["ocr_available"] = ocr.available()
    res["ocr_backend"] = ocr.backend_name()

    # ---------- 3. CV：卡片定位 / 增量检测 / 模板匹配 ----------
    found = cardmod.locate_cards(frame)
    res["locate_cards_n"] = len(found)
    res["locate_cards"] = timeit(lambda: cardmod.locate_cards(frame), n)

    if found:
        res["changed_cards"] = timeit(
            lambda: cardmod.changed_cards(frame, frame, found, prev_cards=found), n)
        r = found[0]
        res["read_card_one"] = timeit(
            lambda: cardmod.read_card(frame, r, upscale=2), n)

    tpl = cv2.imread(TPL_BUY)
    M = cv2.imread(TPL_CONFIRM)
    if tpl is not None and M is not None:
        tpl = vision.to_gray(tpl)
        M = vision.to_gray(M)
        buy_c = frame[0:tpl.shape[0], 0:tpl.shape[1]]
        conf_c = frame[0:M.shape[0], 0:M.shape[1]]
        res["match_buy"] = timeit(lambda: vision.match(vision.to_gray(buy_c), tpl)[0], n)
        res["match_confirm"] = timeit(lambda: vision.match(vision.to_gray(conf_c), M)[0], n)
        # 真实 ROI 尺寸上的匹配（expand 0.15 之后：1920x1080 下的 387x75）
        full = cv2.resize(frame, (1920, 1080))
        bx, by = 1450, 890                      # expand 后大致范围（左侧留余量）
        roi = full[by:by + 80, bx:bx + 400]
        res["match_buy_roi"] = timeit(
            lambda: vision.match(vision.to_gray(roi), tpl)[0], n)

    # ---------- 4. 列表整帧扫描：冷/热 ----------
    sc = market.Scanner()
    cold = []
    warm = []
    for i in range(15):
        sc.reset()
        t = time.perf_counter(); rows = sc.scan(frame); cold.append((time.perf_counter() - t) * 1000)
    sc.reset(); sc.scan(frame)
    for i in range(40):
        t = time.perf_counter(); rows2 = sc.scan(frame); warm.append((time.perf_counter() - t) * 1000)
    res["scan_cold_frame"] = stat(cold)
    res["scan_warm_frame"] = stat(warm)
    res["scan_rows"] = len(rows)
    res["scan_ocr_calls_warm"] = sc.ocr_calls

    # ---------- 5. 纯 Python 开销 ----------
    res["perf_counter"] = timeit(lambda: time.perf_counter(), 200)
    res["sleep_1ms_overhead"] = None  # 由测量得到：见下
    xs = []
    for _ in range(60):
        t = time.perf_counter(); time.sleep(0.001); xs.append((time.perf_counter() - t) * 1000)
    res["sleep_1ms_actual"] = stat(xs)
    xs = []
    for _ in range(60):
        t = time.perf_counter(); time.sleep(0.005); xs.append((time.perf_counter() - t) * 1000)
    res["sleep_5ms_actual"] = stat(xs)

    with open(os.path.join(OUT, "costs.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    print(f"{'指标':38s} {'p50':>8s} {'p95':>8s} {'mean':>8s}  n")
    for k, v in res.items():
        if isinstance(v, dict) and "p50" in v:
            print(f"{k:38s} {v['p50']:8.3f} {v['p95']:8.3f} {v['mean']:8.3f}  {v['n']}")
    print("OCR:", res.get("ocr_backend"), "| 卡片定位:", res["locate_cards_n"],
          "| 行:", res.get("scan_rows"))
    print("输出:", os.path.join(OUT, "costs.json"))


if __name__ == "__main__":
    main()
