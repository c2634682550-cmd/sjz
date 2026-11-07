# -*- coding: utf-8 -*-
"""价格识别独立重测：一帧一读，覆盖四类状态，分开统计两条路径。

只改 tools/ 下的测量脚本；core/ 只读。产出全部落 `_measure/`。

用法（一律隐藏窗口跑）：
    python tools/ocr_price_remeasure.py --build-only
    python tools/ocr_price_remeasure.py --run

设计要点
--------
1. 帧互不重复（像素 SHA256 去重断言），一帧只送一次 `ocr.read_words` /
   `cards.read_card`，不做「同一帧喂 N 轮」。
2. 四类：list_thousands / detail / low_nosep / edge_hug。
3. 路径：面板拼条 vs 整幅回退。面板帧先走生产 `ocr.read_words`（内部
   面板→回退），同时用包装器记录拼条那一次的原始 words，用来判回退原因，
   不额外再推理一次。
4. 列表帧走生产 `cards.read_card`（整卡一次 OCR）。
5. 静默错价（返回了非真值数值）逐帧列出裁片路径、OCR 原文、最终值。
"""
import argparse
import collections
import hashlib
import json
import os
import random
import sys
import threading
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import ocr_chain  # noqa: E402  只读引用（ROI/解析口径）

FIX = os.path.join(ROOT, "tests", "fixtures")
PRICE_DIR = os.path.join(FIX, "price_roi")
MARKET_DIR = os.path.join(FIX, "market")
OUT = os.path.join(ROOT, "_measure")
CORPUS = os.path.join(OUT, "corpus")
FRAMES_JSON = os.path.join(OUT, "frames.json")
RESULT_JSON = os.path.join(OUT, "result_price_remeasure.json")
LOG_PATH = os.path.join(OUT, "run.log")

DIGIT_ALPHA = "01245689"          # 现实裁片里能切全的字模（缺 3/7，见结论）
COIN_DROP = 26                    # 兼容旧裁片的左侧图标留白（探针用）

# 真实卡片网格由 core.cards.locate_cards 现算（两列三行，行优先）
CARD_TRUTH = {
    "list_unfiltered.png": [[25000, 17000], [28888, 16000], [9000, 9500]],
    "list_filtered.png": [[2050, 2050], [2050, 2050], [2100, 2500]],
}

PANEL_SIZE = (858, 590)           # h, w


# ---------------------------------------------------------------- logging
_log_f = None


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if _log_f:
        _log_f.write(line + "\n")
        _log_f.flush()


# ---------------------------------------------------------------- 帧/素材工具
def sha(img):
    return hashlib.sha256(img.tobytes()).hexdigest()


def ink_mask(img, kind):
    b, g, r = cv2.split(img)
    if kind == "red":
        m = ((r.astype(np.int16) - g.astype(np.int16)) >= 45) & \
            ((r.astype(np.int16) - b.astype(np.int16)) >= 45) & (r >= 130)
    else:
        lo = np.minimum(np.minimum(b, g), r)
        m = lo >= 120
    return m.astype(np.uint8)


def ink_mask_trim(img, kind, margin=3):
    """裁片常带 1-2px 浅色描边框，不裁掉会被当成整幅墨迹。"""
    m = ink_mask(img, kind)
    if margin > 0:
        m[:margin, :] = 0
        m[-margin:, :] = 0
        m[:, :margin] = 0
        m[:, -margin:] = 0
    return m


def column_groups(m):
    cols = (m.sum(axis=0) > 0).astype(np.uint8)
    groups, s = [], None
    for x, v in enumerate(cols):
        if v and s is None:
            s = x
        elif not v and s is not None:
            groups.append((s, x))
            s = None
    if s is not None:
        groups.append((s, len(cols)))
    merged = []
    for a, b in groups:
        if merged and a - merged[-1][1] < 2:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    out = []
    for a, b in merged:
        x1 = b - 1
        sub = m[:, a:x1 + 1]
        rr = np.where(sub.sum(axis=1) > 0)[0]
        if not rr.size:
            continue
        out.append({"x0": a, "x1": x1, "y0": int(rr[0]), "y1": int(rr[-1]) + 1,
                    "w": x1 - a + 1, "h": int(rr[-1] - rr[0] + 1)})
    return out


def refine_group(m, g):
    """把一个列组里「只占下半截」的逗号子段拆出来（数字之间的 1px 缝不拆）。"""
    x0, x1, y0, y1 = g["x0"], g["x1"], g["y0"], g["y1"]
    h = y1 - y0
    if h <= 1:
        return [g]
    tops = []
    for x in range(x0, x1 + 1):
        col = m[y0:y1, x]
        idx = np.where(col > 0)[0]
        tops.append((int(idx[0]), int(idx[-1])) if idx.size else (h, -1))
    bt = [(t >= 0.5 * h and (b - t + 1) <= 0.55 * h and b >= 0) for t, b in tops]
    runs, s = [], None
    for i, v in enumerate(bt):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i - 1))
            s = None
    if s is not None:
        runs.append((s, len(bt) - 1))
    if len(runs) != 1:
        return [g]
    a, b = runs[0]
    if a == 0 or b == len(bt) - 1 or (b - a + 1) > 7:
        return [g]

    def sub(xa, xb):
        sm = m[y0:y1, x0 + xa:x0 + xb + 1]
        rr = np.where(sm.sum(axis=1) > 0)[0]
        if not rr.size:
            return None
        return {"x0": x0 + xa, "x1": x0 + xb, "y0": y0 + int(rr[0]),
                "y1": y0 + int(rr[-1]) + 1, "w": xb - xa + 1,
                "h": int(rr[-1] - rr[0] + 1)}
    parts = [sub(0, a - 1), sub(a, b), sub(b + 1, len(bt) - 1)]
    return [p for p in parts if p]


def split_glyphs(img, display, kind):
    """按已知读数把裁片切成 数字/逗号 字模。切不齐返回 None（不猜）。"""
    m = ink_mask_trim(img, kind)
    gs = column_groups(m)
    if not gs:
        return None
    gs = [x for g in gs for x in refine_group(m, g)]
    # 首组若是左侧金币图标：与第二组的间距远大于字间距（字间只有 1px）
    if len(gs) >= 2 and gs[1]["x0"] - gs[0]["x1"] >= 6:
        gs = gs[1:]
    med = float(np.median([g["h"] for g in gs]))
    gs = [g for g in gs if not (g["h"] > med * 1.45 or g["h"] <= 1)]
    digits = [g for g in gs if g["h"] >= max(6, med * 0.55)]
    comma_h = max(6, med * 0.55)
    commas = [g for g in gs if g["h"] < comma_h and 2 <= g["h"] and g["w"] <= 5]
    if len(digits) + len(commas) != len(display):
        return None
    for c in commas:
        if not (any(d["x0"] < c["x0"] for d in digits) and
                any(d["x0"] > c["x0"] for d in digits)):
            return None
    items = sorted(digits + commas, key=lambda g: g["x0"])
    chars = list(display)
    glyphs = []
    for g, ch in zip(items, chars):
        if ch == "," and not (g["h"] < comma_h and g["w"] <= 5):
            return None
        if ch != "," and g["h"] < comma_h:
            return None
        patch = img[g["y0"]:g["y1"], g["x0"]:g["x1"] + 1].copy()
        if patch.size == 0:
            return None
        glyphs.append({"ch": ch, "rgb": patch, "alpha": ink_alpha(patch, kind),
                       "w": g["w"], "h": g["h"], "base_h": med})
    return {"glyphs": glyphs, "kind": kind, "base_h": med}


def ink_alpha(patch, kind):
    b, g, r = cv2.split(patch)
    lo = np.minimum(np.minimum(b, g), r).astype(np.float32)
    if kind == "red":
        val = r.astype(np.float32) - np.maximum(g, b).astype(np.float32)
        peak = max(30.0, float(np.percentile(val, 99)))
        return np.clip(val / peak, 0, 1)
    peak = max(60.0, float(np.percentile(lo, 99)))
    return np.clip((lo - 45.0) / (peak - 45.0), 0, 1)


def build_library():
    """从 9 个去重后的真实价格裁片切字模（白/红两套）。"""
    manifest = json.load(open(os.path.join(PRICE_DIR, "manifest.json"), encoding="utf-8"))
    seen, uniq = set(), []
    for it in manifest["items"]:
        if it.get("cls") != "price":
            continue
        p = os.path.join(PRICE_DIR, it["file"])
        img = cv2.imread(p)
        if img is None:
            continue
        h = sha(img)
        if h in seen:
            continue
        seen.add(h)
        uniq.append({"id": it["id"], "file": p, "display": it["display"],
                     "digits": it["digits"], "img": img})
    log("价格裁片去重：%d 张（manifest %d 条 price）" %
        (len(uniq), sum(1 for x in manifest["items"] if x.get("cls") == "price")))
    # 8 只出现在余额裁片里，字模同字体，只取字形不当价格样本
    for extra in ("u2_top_8.png", "u3_top_8.png"):
        p = os.path.join(PRICE_DIR, extra)
        if os.path.isfile(p):
            img = cv2.imread(p)
            if split_glyphs(img, "8", "white"):
                uniq.append({"id": "glyph_" + extra, "file": p, "display": "8",
                             "digits": "8", "img": img, "glyph_only": True})
                break

    white, red = {}, {}
    for s in uniq:
        kind = "red" if ("detail" in s["id"] or "range" in s["id"]) else "white"
        if s["id"].startswith("u4_range"):
            kind = "white"       # range 裁片是浅底白字，探针已确认
        lib = split_glyphs(s["img"], s["display"], kind)
        if not lib:
            log("  字模切分失败，跳过：%s" % s["id"])
            continue
        s["kind"] = kind
        s["lib"] = lib
        for g in lib["glyphs"]:
            if not g["ch"].isdigit():
                continue
            tgt = (red if kind == "red" else white)
            cur = tgt.get(g["ch"])
            if cur is None or abs(g["h"] - lib["base_h"]) < abs(cur["h"] - cur["base_h"]):
                tgt[g["ch"]] = g
    log("字模库：白=%s 红=%s" % ("".join(sorted(white)), "".join(sorted(red))))
    if len(white) < 6:
        raise SystemExit("白字模不足（%d），停下不猜" % len(white))
    # 红字只切到 0/1：用白字模 + 面板红字颜色合成，字形仍来自真实裁片
    red_lib = dict(white)
    for ch, g in red.items():
        red_lib[ch] = g
    comma = None
    for s in uniq:
        for g in s["lib"]["glyphs"]:
            if g["ch"] == ",":
                comma = g
                break
        if comma:
            break
    return uniq, {"white": white, "red": red_lib, "comma": comma}


def compose(text, library, color_kind, height):
    """按真实字模拼一串数字，返回 (rgb_patch, alpha_patch)。"""
    fam = library["red"] if color_kind == "red" else library["white"]
    comma = library["comma"]
    base_h = 13.0
    scale = height / base_h
    # 先用所有字形求宽高，再画布
    items = []
    for ch in text:
        g = comma if ch == "," else fam.get(ch)
        if g is None:
            raise ValueError("字模缺字符 %r" % ch)
        items.append((ch, g))
    gap = max(1, int(round(1.4 * scale)))
    widths = [max(1, int(round(g["w"] * scale))) for _c, g in items]
    # 逗号与数字同间距：贴紧会被 OCR 读成小数点或错位（实测 25449,241）
    advances = [w + gap for w in widths]
    total_w = max(1, sum(advances) - gap)
    total_h = int(round(max(g["h"] for _c, g in items) * scale)) + 2
    canvas = np.zeros((total_h, total_w, 4), np.float32)
    x = 0
    for (ch, g), w, adv in zip(items, widths, advances):
        patch = g["rgb"]
        alpha = g["alpha"]
        hh = max(1, int(round(g["h"] * scale)))
        p2 = cv2.resize(patch, (w, hh), interpolation=cv2.INTER_AREA)
        a2 = np.clip(cv2.resize(alpha, (w, hh), interpolation=cv2.INTER_AREA), 0, 1)
        y = total_h - 1 - hh
        if color_kind == "red":
            # 面板价格是红字，字形沿用白字模，颜色取真实红字采样
            rgb = np.zeros_like(p2, dtype=np.float32)
            rgb[:, :] = (67.0, 124.0, 221.0)
        else:
            # 白字只用真实裁片的 alpha，颜色统一亮白（避免贴边暗底把字染灰）
            rgb = np.zeros_like(p2, dtype=np.float32)
            rgb[:, :] = (232.0, 232.0, 232.0)
        ww = min(w, canvas.shape[1] - x)
        if ww <= 0:
            break
        region = canvas[y:y + hh, x:x + ww]
        a = a2[:, :ww, None]
        region[..., :3] = rgb[:, :ww] * a + region[..., :3] * (1 - a)
        region[..., 3:4] = np.maximum(region[..., 3:4], a)
        x += adv
    return canvas[..., :3], canvas[..., 3]


def paste_ink(dst, wrap, x, y):
    """把合成好的 (rgb, alpha) 贴到 dst 上（只贴墨迹，不贴底色）。"""
    h, w = wrap[1].shape[:2]
    H, W = dst.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sx0, sy0 = x0 - x, y0 - y
    a = wrap[1][sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)][..., None]
    rgb = wrap[0][sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
    reg = dst[y0:y1, x0:x1].astype(np.float32)
    dst[y0:y1, x0:x1] = np.clip(rgb * a + reg * (1 - a), 0, 255).astype(np.uint8)


def bbox_of_mask(m):
    ys, xs = np.where(m > 0)
    if not ys.size:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def text_bbox(img, kind):
    """数字墨迹的包围盒：首组若与第二组相距 ≥6px 判为金币图标，剔除。"""
    m = ink_mask_trim(img, kind)
    gs = column_groups(m)
    if not gs:
        return None
    if len(gs) >= 2 and gs[1]["x0"] - gs[0]["x1"] >= 6:
        gs = gs[1:]
    return (min(g["x0"] for g in gs), min(g["y0"] for g in gs),
            max(g["x1"] for g in gs) + 1, max(g["y1"] for g in gs) + 1)


def inpaint_region(img, mask, radius=4):
    out = cv2.inpaint(img, (mask > 0).astype(np.uint8) * 255, radius, cv2.INPAINT_TELEA)
    return out


# ---------------------------------------------------------------- 模板
def load_templates():
    panel_path = os.path.join(MARKET_DIR, "detail_panel.png")
    panel = cv2.imread(panel_path)
    if panel is None:
        raise SystemExit("缺 %s" % panel_path)
    h, w = panel.shape[:2]
    px = [r for r in ocr_chain.PANEL_ROIS if r[0] == "price"][0]
    rx0 = int(round(px[1] * w))
    ry0 = int(round(px[2] * h))
    rx1 = int(round((px[1] + px[3]) * w))
    ry1 = int(round((px[2] + px[4]) * h))
    band = (max(0, rx0 - 6), max(0, ry0 - 8), min(w, rx1 + 6), min(h, ry1 + 8))
    bx0, by0, bx1, by1 = band
    sub = panel[by0:by1, bx0:bx1]
    m = ink_mask(sub, "red")
    # 金币与红字是一整组：整条内框一起抹掉再补，宽度变化时价格才能真居中
    mm = np.zeros(sub.shape[:2], np.uint8)
    mm[3:-3, 3:-3] = 255
    blank = panel.copy()
    blank[by0:by1, bx0:bx1] = inpaint_region(sub, mm, 6)
    orig_bbox = bbox_of_mask(cv2.dilate(m, np.ones((2, 2), np.uint8), 1))
    return {"panel": panel, "blank": blank, "band": band, "roi": (rx0, ry0, rx1, ry1),
            "orig_text_bbox": (bx0 + orig_bbox[0], by0 + orig_bbox[1],
                               bx0 + orig_bbox[2], by0 + orig_bbox[3]) if orig_bbox else None}


def load_cards():
    from core import cards
    out = []
    for fn, truth in CARD_TRUTH.items():
        img = cv2.imread(os.path.join(MARKET_DIR, fn))
        if img is None:
            raise SystemExit("缺 %s" % fn)
        rects = cards.locate_cards(img)
        for i, (x0, y0, x1, y1) in enumerate(rects):
            r, c = i // 2, i % 2
            card = img[y0:y1, x0:x1].copy()
            h, w = card.shape[:2]
            # 价格区：整卡右下角；只抹这一块（成色行在左侧，不受影响）
            rx0, ry0 = int(w * 0.68), int(h * 0.62)
            m = ink_mask(card[ry0:, rx0:], "white")
            mm = np.zeros(card[ry0:, rx0:].shape[:2], np.uint8)
            mm[2:-2, 2:-2] = 255
            blank = card.copy()
            blank[ry0:, rx0:] = inpaint_region(card[ry0:, rx0:], mm, 5)
            bb = bbox_of_mask(cv2.dilate(m, np.ones((2, 2), np.uint8), 1))
            out.append({"file": fn, "row": r, "col": c, "img": card, "blank": blank,
                        "truth": truth[r][c], "w": w, "h": h,
                        "price_box": (rx0 + bb[0], ry0 + bb[1], rx0 + bb[2], ry0 + bb[3])
                        if bb else None})
    return out


# ---------------------------------------------------------------- 生成帧
def next_value(rng, lo_digits, hi_digits, sep):
    nd = rng.randint(lo_digits, hi_digits)
    s = str(rng.choice([int(ch) for ch in DIGIT_ALPHA if ch != "0"]))
    s += "".join(rng.choice(DIGIT_ALPHA) for _ in range(nd - 1))
    if sep and nd > 3:
        parts = []
        while len(s) > 3:
            parts.insert(0, s[-3:])
            s = s[:-3]
        parts.insert(0, s)
        return ",".join(parts), int("".join(parts))
    return s, int(s)


def build_frames(rng):
    library_uniq, lib = build_library()
    tpl = load_templates()
    cardsrc = load_cards()
    frames = []
    seen_sha = set()

    def add(img, cls, truth, route_hint, src, note=""):
        hh = sha(img)
        if hh in seen_sha:
            log("跳过重复帧：%s %s" % (cls, note))
            return False
        seen_sha.add(hh)
        fid = "%04d_%s" % (len(frames), cls)
        p = os.path.join(CORPUS, fid + ".png")
        cv2.imwrite(p, img)
        frames.append({"id": fid, "class": cls, "truth": int(truth),
                       "route_hint": route_hint, "source": src, "note": note,
                       "path": p})
        return True

    panel, blank = tpl["panel"], tpl["blank"]
    prx0, pry0, prx1, pry1 = tpl["roi"]

    def panel_paste(text, kind, x_mode="center", right_edge=None):
        img = blank.copy()
        wrap = compose(text, lib, kind, 15)
        ww = wrap[1].shape[1]
        if x_mode == "center":
            x = 316 - ww // 2
        else:
            x = right_edge - ww
        y = 797 - wrap[1].shape[0]
        paste_ink(img, wrap, x, y)
        return img

    def card_paste(card, text, kind):
        img = card["blank"].copy()
        wrap = compose(text, lib, kind, 13)
        ww = wrap[1].shape[1]
        box = card["price_box"]
        right = box[2] if box else card["w"] - 10
        bottom = box[3] if box else card["h"] - 10
        paste_ink(img, wrap, right - ww, bottom - wrap[1].shape[0])
        return img

    def scale(img, f):
        if f == 1.0:
            return img
        return cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)

    # ---- 真实裁片：贴到面板/卡片（墨迹级合成，保留真实字形与底色）
    real_sources = [s for s in library_uniq if "lib" in s and not s.get("glyph_only")]
    for s in real_sources:
        kind = s["kind"]
        bb = text_bbox(s["img"], kind)
        if not bb:
            continue
        crop = s["img"][bb[1]:bb[3], bb[0]:bb[2]]
        alpha = ink_alpha(crop, kind)
        wrap = (crop.astype(np.float32), alpha)
        hh = wrap[1].shape[0]
        # B 详情页：居中 + 3 个缩放
        for f in (1.0, 1.15, 1.3):
            img = blank.copy()
            paste_ink(img, wrap, 316 - wrap[1].shape[1] // 2, 797 - hh)
            add(scale(img, f), "detail", int(s["digits"]), "panel_strip",
                s["id"], "real_center@%.2f" % f)
        # A 列表页：三种卡片底 × 右对齐
        for i in range(3):
            card = cardsrc[(real_sources.index(s) + i) % len(cardsrc)]
            img = card["blank"].copy()
            box = card["price_box"]
            right = box[2] if box else card["w"] - 10
            bottom = box[3] if box else card["h"] - 10
            paste_ink(img, wrap, right - wrap[1].shape[1], bottom - hh)
            add(img, "list_thousands", int(s["digits"]), "whole_card",
                s["id"], "real_on_card_%d" % card["row"] + str(card["col"]))
        # D 右缘贴边：墨迹右缘 392 / 390
        for edge in (392, 390):
            for f in (1.0, 1.2):
                img = blank.copy()
                paste_ink(img, wrap, edge - wrap[1].shape[1], 797 - hh)
                add(scale(img, f), "edge_hug", int(s["digits"]), "panel_strip",
                    s["id"], "real_edge@%d/%.2f" % (edge, f))

    # ---- 真实卡片整帧（原图不动，真值人工核对过）
    for card in cardsrc:
        add(card["img"].copy(), "list_thousands", card["truth"], "whole_card",
            "%s_r%dc%d" % (card["file"], card["row"], card["col"]), "real_card_raw")

    # ---- 真实详情页整帧（原图不动，面板真值 25,000）
    add(tpl["panel"].copy(), "detail", 25000, "panel_strip",
        "detail_panel.png", "real_screenshot")

    # ---- 去逗号变体：把逗号墨迹抹掉，得到「无千分位」中间价
    for s in real_sources:
        if "," not in s["display"]:
            continue
        img0 = s["img"]
        bb = text_bbox(img0, s["kind"])
        if not bb:
            continue
        bx0, by0, bx1, by1 = bb
        m = ink_mask_trim(img0, s["kind"])
        gs = [g for g in column_groups(m) if g["x0"] >= bx0 and g["x1"] < bx1]
        med = np.median([g["h"] for g in gs]) or 1
        comma_g = [g for g in gs if g["h"] <= max(5, med * 0.4) and 2 <= g["w"] <= 8]
        if not comma_g:
            continue
        cleared = img0.copy()
        for g in comma_g:
            pad = 3
            xa, xb = max(0, g["x0"] - pad), min(img0.shape[1], g["x1"] + pad)
            ya, yb = max(0, g["y0"] - pad), min(img0.shape[0], g["y1"] + pad)
            mm = np.zeros(img0.shape[:2], np.uint8)
            mm[ya:yb, xa:xb] = ((m[ya:yb, xa:xb] > 0) * 255).astype(np.uint8)
            cleared = inpaint_region(cleared, mm, 6)
        truth = int(s["display"].replace(",", ""))
        # 面板底，2 个缩放
        for f in (1.0, 1.2):
            img = blank.copy()
            c2 = cleared[by0:by1, bx0:bx1]
            a2 = ink_alpha(c2, s["kind"])
            paste_ink(img, (c2.astype(np.float32), a2),
                      316 - a2.shape[1] // 2, 797 - a2.shape[0])
            add(scale(img, f), "low_nosep", truth, "panel_strip",
                s["id"] + "|nocomma", "nocomma_panel@%.1f" % f)

    # ---- 合成帧（数字串互不相同，字形仍来自真实裁片）
    # A 列表千分位 60 帧
    used_vals = set()
    for i in range(60):
        while True:
            text, val = next_value(rng, 5, 7, True)
            if val not in used_vals:
                used_vals.add(val)
                break
        card = cardsrc[i % len(cardsrc)]
        img = card_paste(card, text, "white")
        add(img, "list_thousands", val, "whole_card", "composed",
            "value=" + text)
    # B 详情页 60 帧
    used_vals_b = set()
    for i in range(60):
        while True:
            text, val = next_value(rng, 6, 8, True)
            if val not in used_vals_b:
                used_vals_b.add(val)
                break
        add(panel_paste(text, "red"), "detail", val, "panel_strip",
            "composed", "value=" + text)
    # C 低价无千分位 40 帧（20 面板 + 20 列表）
    used_vals_c = set()
    for i in range(40):
        while True:
            text, val = next_value(rng, 1, 4, False)
            if val not in used_vals_c and val != 63:
                used_vals_c.add(val)
                break
        if i % 2 == 0:
            add(panel_paste(text, "red"), "low_nosep", val, "panel_strip",
                "composed", "value=" + text)
        else:
            card = cardsrc[(i * 7) % len(cardsrc)]
            add(card_paste(card, text, "white"), "low_nosep", val, "whole_card",
                "composed", "value=" + text)
    # D 右缘贴边 60 帧（20 个值 × 3 个贴边量）
    used_vals_d = set()
    for i in range(20):
        while True:
            text, val = next_value(rng, 6, 8, True)
            if val not in used_vals_d:
                used_vals_d.add(val)
                break
        for edge in (392, 390, 386):
            add(panel_paste(text, "red", x_mode="edge", right_edge=edge),
                "edge_hug", val, "panel_strip", "composed",
                "value=%s@%d" % (text, edge))

    json.dump(frames, open(FRAMES_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("生成帧：%d（互不重复，SHA256 断言通过）" % len(frames))
    by_cls = collections.Counter(f["class"] for f in frames)
    log("分类：" + " ".join("%s=%d" % (k, v) for k, v in sorted(by_cls.items())))
    return frames


# ---------------------------------------------------------------- 测量
def phrase_text(words):
    out = []
    for w in words or []:
        out.append("%s(%s,x=%d,y=%d,s=%.2f)" % (
            w.get("text", ""), w.get("field") or "-",
            int(w.get("x") or 0), int(w.get("y") or 0), float(w.get("score") or 0)))
    return " | ".join(out)


def measure(frames, timeout_s=1800):
    from core import ocr, ocr_chain, cards, market, money

    def watchdog():
        time.sleep(timeout_s)
        log("硬超时 %ds，强制退出" % timeout_s)
        os._exit(2)

    threading.Thread(target=watchdog, daemon=True).start()

    ocr.reload()
    if not ocr.available():
        raise SystemExit("OCR 不可用：%s" % (ocr.rapid_error() or "未知"))
    log("后端：%s" % ocr.backend_name())

    b = ocr._client()
    cap = {"panel": None}
    orig_panel = b.read_panel

    def wrapped_panel(img, timeout=None, rois=None):
        words = orig_panel(img, timeout=timeout, rois=rois)
        cap["panel"] = words
        cap["price_words"] = [w for w in (words or []) if w.get("field") == "price"]
        return words

    b.read_panel = wrapped_panel

    rows = []
    t_all = time.perf_counter()
    for i, f in enumerate(frames):
        img = cv2.imread(f["path"])
        truth = int(f["truth"])
        t0 = time.perf_counter()
        rec = {"id": f["id"], "class": f["class"], "truth": truth,
               "path": f["path"], "source": f["source"], "note": f["note"]}
        if f["route_hint"] == "whole_card":
            res = cards.read_card(img, (0, 0, img.shape[1], img.shape[0]))
            tok = res.get("price") or ""
            val = money.scan_text(tok)[0] if tok else None
            rec.update({"route": "whole_card", "strip_fields": [], "strip_price": None,
                        "raw": tok, "words": res.get("bottom", "")[:400],
                        "value": val})
        else:
            cap["panel"] = None
            cap["price_words"] = None
            words = ocr.read_words(img)
            tags = [w.get("field") for w in words if w.get("field")]
            fields = sorted(set(tags))
            strip = cap["panel"]
            strip_fields = sorted({w.get("field") for w in (strip or []) if w.get("field")})
            strip_price, strip_tok = ocr_chain.price_from_words(strip or [])
            if "price" in fields:
                val, tok = ocr_chain.price_from_words(words)
                route = "panel"
            else:
                txt = ocr_chain._join(words)
                val = market.parse_row(txt, whole_frame=True).get("price")
                tok = ""
                route = "fallback" if strip is not None else "whole_plain"
            missing = [x for x in ocr_chain.PANEL_REQUIRED if x not in strip_fields]
            rec.update({"route": route, "strip_fields": strip_fields,
                        "strip_price": strip_price, "strip_token": strip_tok,
                        "strip_price_words": phrase_text(cap.get("price_words")),
                        "strip_missing": missing,
                        "raw": phrase_text(words), "value": val})
        ms = (time.perf_counter() - t0) * 1000.0
        rec["ms"] = round(ms, 1)
        if rec["value"] is None:
            rec["outcome"] = "rejected"
        elif int(rec["value"]) == truth:
            rec["outcome"] = "exact"
        else:
            rec["outcome"] = "wrong"
        rows.append(rec)
        if (i + 1) % 20 == 0:
            log("进度 %d/%d  %.1fs" % (i + 1, len(frames), time.perf_counter() - t_all))
    total_s = time.perf_counter() - t_all
    return rows, round(total_s, 1), ocr.backend_name()


def summarize(rows, total_s, backend, frames_meta):
    block = {}
    for key, items in (("class", rows),
                       ("route", rows)):
        for name in sorted({r[key] for r in items}):
            sub = [r for r in items if r[key] == name]
            n = len(sub)
            exact = sum(1 for r in sub if r["outcome"] == "exact")
            rej = sum(1 for r in sub if r["outcome"] == "rejected")
            wrong = n - exact - rej
            block.setdefault(key, {})[name] = {
                "n": n, "exact": exact, "rejected": rej, "wrong": wrong,
                "exact_rate": round(exact / n, 4) if n else 0.0,
                "reject_rate": round(rej / n, 4) if n else 0.0,
                "wrong_rate": round(wrong / n, 4) if n else 0.0}

    panel_rows = [r for r in rows if r["route"] in ("panel", "fallback")]
    fb = [r for r in panel_rows if r["route"] == "fallback"]
    fb_reasons = collections.Counter()
    fb_class = collections.Counter(r["class"] for r in fb)
    for r in fb:
        if r["strip_missing"]:
            fb_reasons["缺块:" + ",".join(r["strip_missing"])] += 1
        elif r["strip_price"] is None:
            fb_reasons["拼条价格拒识"] += 1
        else:
            fb_reasons["其他"] += 1

    # 同一素材只取第一次读，做「素材级独立」对照
    first = {}
    for r in rows:
        first.setdefault(r["source"], r)
    frows = list(first.values())
    fx = sum(1 for r in frows if r["outcome"] == "exact")
    fr = sum(1 for r in frows if r["outcome"] == "rejected")
    fw = len(frows) - fx - fr

    silent = [r for r in rows if r["outcome"] == "wrong"]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": backend,
        "frames_total": len(rows),
        "frames_unique_content_sources": len({r["source"] for r in rows}),
        "elapsed_s": total_s,
        "ms_per_frame_avg": round(sum(r["ms"] for r in rows) / len(rows), 1),
        "by_class": block.get("class", {}),
        "by_route": block.get("route", {}),
        "panel_like_frames": len(panel_rows),
        "fallback_frames": len(fb),
        "fallback_rate": round(len(fb) / len(panel_rows), 4) if panel_rows else 0.0,
        "fallback_by_class": dict(fb_class),
        "fallback_reasons": dict(fb_reasons),
        "source_first_read_only": {"n": len(frows), "exact": fx, "rejected": fr,
                                   "wrong": fw,
                                   "exact_rate": round(fx / len(frows), 4) if frows else 0.0},
        "silent_wrong_count": len(silent),
        "silent_wrong": [{
            "frame": r["id"], "class": r["class"], "truth": r["truth"],
            "value": r["value"], "route": r["route"], "source": r["source"],
            "crop_path": r["path"], "raw": r["raw"], "note": r["note"]}
            for r in silent],
        "frames_meta_path": FRAMES_JSON,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()
    os.makedirs(CORPUS, exist_ok=True)
    global _log_f
    _log_f = open(LOG_PATH, "w", encoding="utf-8")
    if args.build_only:
        for f in os.listdir(CORPUS):
            if f.endswith(".png"):
                os.remove(os.path.join(CORPUS, f))
    if args.build_only or not os.path.isfile(FRAMES_JSON):
        frames = build_frames(random.Random(20261107))
        if args.build_only:
            log("只生成，不测量")
            return
    else:
        frames = json.load(open(FRAMES_JSON, encoding="utf-8"))
        log("复用已生成帧：%d" % len(frames))
    if args.run or True:
        rows, total_s, backend = measure(frames, timeout_s=args.timeout)
        summary = summarize(rows, total_s, backend, frames)
        out = {"summary": summary, "frames": rows}
        json.dump(out, open(RESULT_JSON, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        log("结果写入 %s" % RESULT_JSON)


if __name__ == "__main__":
    main()
