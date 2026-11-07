"""流程组：单件全流程耗时分解、固定等待占比、重复动作次数。

离线合成：截图走**真实 mss 抓屏**（同机、同分辨率），识别/挑选/匹配走
生产函数 + 固定素材，点击走真实 sleep 占用（假 sink，不发输入）。不驱动
引擎主循环，避免碰到真实窗口和鼠标；阶段划分与引擎列表模式一一对应。
"""

from __future__ import annotations

import os
import time

import numpy as np

from common import Summary, materials, now, process_cpu_ms


def _pick_template(mats):
    for name in ("confirm.png", "buy.png"):
        path = os.path.join(mats["templates"], name)
        if os.path.exists(path):
            return path, name
    raise RuntimeError("templates/ 下既没有 confirm.png 也没有 buy.png")


def _grab_gray(sct, np_mod, vision, bbox):
    shot = sct.grab(bbox)
    return vision.to_gray(np.asarray(shot))


def run(mods, root, args, cfg):
    import mss

    vision = mods["vision"]
    ocr = mods["ocr"]
    market = mods["market"]
    cards = mods["cards"]
    bricks = mods["bricks"]
    burst = mods["click_burst"]
    threshold = float(mods["config"].DEFAULTS.get("threshold") or 0.85)

    mats = materials(root)
    list_path = os.path.join(mats["market"], "list_unfiltered.png")
    detail_path = os.path.join(mats["market"], "detail_panel.png")
    list_img = vision.imread(list_path)
    detail_img = vision.imread(detail_path)
    tpl_path, tpl_name = _pick_template(mats)
    tpl_gray = vision.to_gray(vision.imread(tpl_path))
    if list_img is None or detail_img is None or tpl_gray is None:
        raise RuntimeError("流程组素材缺失（list_unfiltered / detail_panel / 模板）")

    reps = int(cfg["reps"]["flow"])
    hold_s = float(cfg["click"]["hold_ms"]) / 1000.0
    fire_frames = max(1, int(cfg["flow"]["fire_frames"]))
    fire_delay_s = float(mods["config"].DEFAULTS.get("fire_delay_ms") or 0) / 1000.0
    confirm_gap_s = float(mods["config"].DEFAULTS.get("confirm_interval_ms") or 0) / 1000.0

    task = {"max_price": 10 ** 9, "sort": "price"}

    sct = mss.mss()
    mon = sct.monitors[1]
    bbox_full = {"left": mon["left"], "top": mon["top"],
                 "width": mon["width"], "height": mon["height"]}
    bbox_roi = {"left": mon["left"], "top": mon["top"],
                "width": min(400, mon["width"]), "height": min(150, mon["height"])}

    def click(hold):
        time.sleep(hold)

    s_cap = Summary(warmup=2)
    s_scan = Summary(warmup=2)
    s_pick = Summary(warmup=2)
    s_enter = Summary(warmup=2)
    s_detail = Summary(warmup=2)
    s_fire = Summary(warmup=2)
    s_confirm = Summary(warmup=2)
    s_total = Summary(warmup=2)
    s_cpu_item = Summary(warmup=2)

    ratios, ocr_calls, fire_iters, clicks = [], [], [], []
    scanner = market.Scanner(upscale=1)
    canvas = np.zeros((max(bbox_roi["height"], tpl_gray.shape[0] + 20),
                       max(bbox_roi["width"], tpl_gray.shape[1] + 20)), dtype=np.uint8)
    hit_canvas = canvas.copy()
    h, w = tpl_gray.shape[:2]
    hit_canvas[10:10 + h, 10:10 + w] = tpl_gray

    for _ in range(reps):
        t_item = now()
        cpu_item0 = process_cpu_ms()

        t0 = now()
        _grab_gray(sct, np, vision, bbox_full)
        cap_ms = (now() - t0) * 1000.0
        s_cap.add(cap_ms)

        t0 = now()
        calls_before = scanner.ocr_calls
        rows = scanner.scan(list_img)
        s_scan.add((now() - t0) * 1000.0)

        t0 = now()
        row, why, _diag = market.pick(rows, task)
        s_pick.add((now() - t0) * 1000.0)

        t0 = now()
        click(hold_s)
        if fire_delay_s:
            time.sleep(fire_delay_s)
        s_enter.add((now() - t0) * 1000.0)

        t0 = now()
        words = ocr.read_words(detail_img)
        text = "\n".join(x["text"] for x in words)
        price, _tok = mods["ocr_chain"].price_from_text(text)
        craft = bricks.parse_craft(text)
        wear = bricks.parse_wear(text)
        s_detail.add((now() - t0) * 1000.0)

        t0 = now()
        frames = 0
        score = 0.0
        while frames < max(fire_frames, 2):
            frame = hit_canvas if frames >= 1 else canvas
            _grab_gray(sct, np, vision, bbox_roi)
            score, _loc = vision.match(frame, tpl_gray)
            frames += 1
            if score >= threshold:
                break
        s_fire.add((now() - t0) * 1000.0)

        t0 = now()
        click(hold_s)
        if confirm_gap_s:
            time.sleep(confirm_gap_s)
        s_confirm.add((now() - t0) * 1000.0)

        total_ms = (now() - t_item) * 1000.0
        s_total.add(total_ms)
        if cpu_item0 is not None:
            s_cpu_item.add(process_cpu_ms() - cpu_item0)
        fixed_ms = hold_s * 2 * 1000.0 + fire_delay_s * 1000.0 + confirm_gap_s * 1000.0
        ratios.append(fixed_ms / total_ms if total_ms else None)
        ocr_calls.append(scanner.ocr_calls - calls_before + 1)
        fire_iters.append(frames)
        clicks.append(2)

    def med(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        vals = sorted(vals)
        m = len(vals) // 2
        return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0

    metrics = {
        "capture_list_ms": s_cap.summary(),
        "scan_list_ms": s_scan.summary(),
        "pick_ms": s_pick.summary(),
        "enter_click_ms": s_enter.summary(),
        "detail_ocr_ms": s_detail.summary(),
        "fire_frame_ms": s_fire.summary(),
        "confirm_click_ms": s_confirm.summary(),
        "item_total_ms": s_total.summary(),
        "item_cpu_ms": s_cpu_item.summary(),
    }
    extra = {
        "fixed_wait_ratio": round(med(ratios), 4) if med(ratios) else None,
        "fixed_wait_ms": round(hold_s * 2 * 1000.0 + fire_delay_s * 1000.0 + confirm_gap_s * 1000.0, 1),
        "ocr_calls_per_item": med(ocr_calls),
        "grabs_per_item": 1 + int(med(fire_iters) or 0),
        "clicks_per_item": med(clicks),
        "fire_frames_p50": med(fire_iters),
        "confirm_template": tpl_name,
        "monitor": [mon["width"], mon["height"]],
    }
    notes = {
        "口径": ("单件=列表扫一遍→挑一行→点进详情→读详情→连点到弹窗→确认；"
                "截图是真实 mss 抓屏，识别/挑选/模板匹配是生产函数 + 固定素材，"
                "点击是真实 sleep 占用（假 sink）。"),
        "缓存": ("列表扫描器跨件复用，scan_list_ms 反映稳态（帧间缓存命中）；"
                "冷态在 cold_p50 列。"),
        "弹窗": f"确认模板 {tpl_name}；第 2 帧起视为弹窗出现，阈值 {threshold:g}。",
    }
    sct.close()
    return {"metrics": metrics, "accuracy": {}, "extra": extra, "notes": notes}
