"""OCR 组：三级识别耗时、单件识别总耗时、每秒可评估商品数。

口径：
- 三级分别单独计时，走生产入口（RapidOCR / PP-OCR 服务 / 自建字模）；
- 单件识别 = 一张列表卡的三字段（名称/成色/价格）识别，走 market 的生产路径；
- 每秒可评估商品数 = 一屏冷扫描读出的行数 / 冷扫描耗时（不把缓存命中当吞吐）。
"""

from __future__ import annotations

import os

import numpy as np

from common import (Summary, load_json, load_manifest, materials, now, percentile,
                    process_cpu_ms)

CURRENCY = "currency"


def _exact(value, truth):
    return 1.0 if value is not None and int(value) == int(truth) else 0.0


def _rate(hits, n):
    return {"value": round(hits / n, 4) if n else None, "n": n}


def run(mods, root, args, cfg):
    ocr = mods["ocr"]
    rapid = mods["ocr_rapid"]
    pp = mods["ocr_ppocr"]
    chain = mods["ocr_chain"]
    digit = mods["digit_ocr"]
    vision = mods["vision"]
    market = mods["market"]
    cards = mods["cards"]
    bricks = mods["bricks"]

    mats = materials(root)
    reps = int(cfg["reps"]["ocr_price"])
    item_reps = int(cfg["reps"]["ocr_item"])

    price_items = load_manifest(mats["price_roi"])["items"]
    grade_items = load_manifest(mats["grade_roi"])["items"]

    price_imgs, price_truth = [], []
    for it in price_items:
        path = os.path.join(mats["price_roi"], it["file"])
        img = vision.imread(path)
        if img is None or getattr(img, "size", 0) == 0:
            raise RuntimeError(f"素材读不出来：{path}")
        price_imgs.append(img)
        price_truth.append(it)
    grade_imgs = []
    for it in grade_items:
        path = os.path.join(mats["grade_roi"], it["file"])
        img = vision.imread(path)
        if img is None or getattr(img, "size", 0) == 0:
            raise RuntimeError(f"素材读不出来：{path}")
        grade_imgs.append(img)

    notes = {}
    # ---------- 冷启动 ----------
    t0 = now()
    fresh = rapid.make({})
    construct_ms = (now() - t0) * 1000.0
    t0 = now()
    fresh.read_words(price_imgs[0])
    first_infer_ms = (now() - t0) * 1000.0
    cold = {"construct_ms": round(construct_ms, 2),
            "first_infer_ms": round(first_infer_ms, 2)}

    # ---------- 三级耗时 ----------
    s_tier1 = Summary(warmup=3)
    s_up = Summary(warmup=3)
    s_tier2 = Summary(warmup=3)
    s_tier3 = Summary(warmup=3)
    s_chain = Summary(warmup=3)

    pp_ok = False
    pp_probe_ms = None
    try:
        t0 = now()
        pp_ok = bool(pp.available())
        pp_probe_ms = round((now() - t0) * 1000.0, 2)
    except Exception as e:  # noqa: BLE001
        notes["tier2"] = f"PP-OCR 探活失败：{e}"
    if not pp_ok:
        notes["tier2"] = "PP-OCR 服务未起（二级只在 Rapid 失败时才走，基线里计 0 次）"

    tier1_hit = tier1_n = 0
    tier3_hit = tier3_n = 0
    chain_hit = chain_n = 0

    for _ in range(reps):
        for img, it in zip(price_imgs, price_truth):
            t0 = now()
            words1 = fresh.read_words(img)
            s_tier1.add((now() - t0) * 1000.0)
            tok = "\n".join(w["text"] for w in words1)
            v1, _t = chain.price_from_text(tok)
            tier1_n += 1
            if v1 is not None and int(v1) == int(it["digits"]):
                tier1_hit += 1

            t0 = now()
            big = chain.upscale(img, 3)
            s_up.add((now() - t0) * 1000.0)
            if pp_ok:
                t0 = now()
                pp.read_words(big)
                s_tier2.add((now() - t0) * 1000.0)

            t0 = now()
            v3, _raw3, _s3 = digit.read_detail(img)
            s_tier3.add((now() - t0) * 1000.0)
            tier3_n += 1
            if v3 is not None and int(v3) == int(it["digits"]):
                tier3_hit += 1

            t0 = now()
            v = ocr.read_price(img)
            s_chain.add((now() - t0) * 1000.0)
            chain_n += 1
            if v is not None and int(v) == int(it["digits"]):
                chain_hit += 1

    # ---------- 下界探针：空图也走一次完整前向 ----------
    # det 模型按输入固定缩放（长边 ~960），ROI 再小也省不掉一次前向；
    # 这张 16×16 空图的耗时就是「单帧识别的物理下界」实测值。
    s_floor = Summary(warmup=3)
    tiny = np.zeros((16, 16, 3), dtype=np.uint8)
    for _ in range(reps):
        t0 = now()
        fresh.read_words(tiny)
        s_floor.add((now() - t0) * 1000.0)

    # ---------- 成色/品阶识别 ----------
    s_grade = Summary(warmup=3)
    grade_hit = grade_n = 0
    for _ in range(reps):
        for img, it in zip(grade_imgs, grade_imgs and grade_items):
            t0 = now()
            text = ocr.read_text(img)
            s_grade.add((now() - t0) * 1000.0)
            want_craft = it.get("craft")
            want_wear = it.get("wear")
            ok = True
            if want_craft:
                ok = ok and (bricks.parse_craft(text) == want_craft)
            if want_wear:
                ok = ok and (bricks.parse_wear(text) == want_wear)
            grade_n += 1
            grade_hit += 1 if ok else 0

    # ---------- 单件识别（一张卡三字段） ----------
    list_img = vision.imread(os.path.join(mats["market"], "list_unfiltered.png"))
    if list_img is None or getattr(list_img, "size", 0) == 0:
        raise RuntimeError("缺 tests/fixtures/market/list_unfiltered.png")
    found = cards.locate_cards(list_img)
    if not found:
        raise RuntimeError("locate_cards 在 list_unfiltered.png 上一张卡都没找到")
    s_item = Summary(warmup=3)
    s_cpu_item = Summary(warmup=3)
    card_count = 0
    for _ in range(item_reps):
        for rect in found[:6]:
            cpu0 = process_cpu_ms()
            t0 = now()
            rows = market._scan_cards(list_img, [rect])
            s_item.add((now() - t0) * 1000.0)
            if cpu0 is not None:
                s_cpu_item.add(process_cpu_ms() - cpu0)
            card_count += 1
            if not rows:
                notes.setdefault("item_scan", "有卡片三字段一个都没认出来（计入耗时，不计入行数）")

    # ---------- 列表吞吐 ----------
    s_list = Summary(warmup=1)
    s_list_cached = Summary(warmup=1)
    s_cpu_screen = Summary(warmup=1)
    rows_cold = []
    for _ in range(5):
        scanner = market.Scanner(upscale=1)
        cpu0 = process_cpu_ms()
        t0 = now()
        rows_cold = scanner.scan(list_img)
        s_list.add((now() - t0) * 1000.0)
        if cpu0 is not None:
            s_cpu_screen.add(process_cpu_ms() - cpu0)
        t0 = now()
        scanner.scan(list_img)          # 同帧再扫一次：命中帧间缓存
        s_list_cached.add((now() - t0) * 1000.0)
    list_ms = s_list.summary()["p50"]
    items_per_s = (len(rows_cold) / (list_ms / 1000.0)) if list_ms else None

    metrics = {
        "tier1_ms": s_tier1.summary(),
        "tier1_floor_ms": s_floor.summary(),
        "tier2_upscale_ms": s_up.summary(),
        "tier3_ms": s_tier3.summary(),
        "chain_price_ms": s_chain.summary(),
        "grade_read_ms": s_grade.summary(),
        "item_recog_ms": s_item.summary(),
        "item_cpu_ms": s_cpu_item.summary(),
        "list_cold_scan_ms": s_list.summary(),
        "list_cached_scan_ms": s_list_cached.summary(),
        "list_scan_cpu_ms": s_cpu_screen.summary(),
    }
    if pp_ok:
        metrics["tier2_total_ms"] = s_tier2.summary()

    accuracy = {
        "price_exact_rate": _rate(chain_hit, chain_n),
        "tier1_price_exact_rate": _rate(tier1_hit, tier1_n),
        "tier3_price_exact_rate": _rate(tier3_hit, tier3_n),
        "grade_hit_rate": _rate(grade_hit, grade_n),
    }
    extra = {
        "items_per_s": round(items_per_s, 2) if items_per_s else None,
        "cpu_ms_per_item": round(s_cpu_item.summary()["p50"], 1) if s_cpu_item.raw else None,
        "cpu_ms_per_screen": round(s_cpu_screen.summary()["p50"], 1) if s_cpu_screen.raw else None,
        "cards_per_screen": len(found),
        "rows_per_cold_scan": len(rows_cold),
        "card_scan_samples": card_count,
        "tier2_available": pp_ok,
        "tier2_probe_ms": pp_probe_ms,
        "cold_start": cold,
    }
    notes["口径"] = ("三级分别单独计时；chain_price_ms 是生产入口（一级失败自动降二级/三级）；"
                    "item_recog_ms 为一张卡三字段的生产路径；items_per_s 用冷扫描行数/冷扫描 p50，"
                    "不把缓存命中算成吞吐。")
    notes["素材"] = (f"price_roi {len(price_imgs)} 例 × {reps} 轮；grade_roi {len(grade_imgs)} 例 × "
                     f"{reps} 轮；列表卡 {len(found)} 张 × {item_reps} 轮")
    return {"metrics": metrics, "accuracy": accuracy, "extra": extra, "notes": notes}
