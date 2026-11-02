"""卡片定位：先找到卡片，再逐块认字。

为什么不是「整屏 OCR 再切词」：
  交易行一屏六张卡片，卡内「名字在左上、价格在右下」的距离（300+ px）
  比两张卡片之间的空隙（10~40 px）还大。按词距聚类会把两张卡并成一条、
  把名字和价格拆到不同记录里 —— 这是早期版本踩过的坑，不是假想。

所以顺序反过来：
  1. OpenCV 在列表区域里找卡片的边框线，拼出网格 → 得到每张卡的矩形
  2. 按卡片切图，再按比例切出 图标 / 名称 / 价格 三块
  3. 名称和价格先放大再交给 WinRT OCR（13px 的字不放大读不出千位逗号）
  4. 相邻两帧逐卡比对，没变的卡不重复 OCR

定位失败（换分辨率、卡片半透明糊成一片）时由调用方退回固定网格，
不在这里硬猜 —— 猜错一次就是买错一件。
"""
import re

import cv2
import numpy as np

from . import vision

# 卡片内部的分块比例（相对卡片矩形，0~1）。
# 实测 1920x1080 在售页：图标占左 70%，名字在顶部一行，成色左下，价格右下。
ICON_BOX = (0.04, 0.05, 0.74, 0.72)
NAME_BOX = (0.02, 0.02, 0.98, 0.28)
GRADE_BOX = (0.02, 0.70, 0.56, 0.98)
PRICE_BOX = (0.54, 0.70, 0.99, 0.98)

# 相邻帧「变了」的判据：卡片平均灰度差超过这个值才算变。
# 3 以下基本都是游戏自己的光照闪烁和粒子特效，不是换货。
CHANGE_DELTA = 6.0


def _peaks(profile, ratio=0.35, min_gap=40):
    """在一维边缘强度里找「边框线」的位置。

    profile: 每行/每列的边缘强度之和
    返回: 合并后、按位置升序的中心坐标
    """
    p = np.asarray(profile, dtype=np.float32)
    if p.size == 0:
        return []
    thr = max(float(p.max()) * ratio, float(p.mean()) * 1.5)
    idx = np.where(p >= thr)[0]
    if idx.size == 0:
        return []
    groups, cur = [], [int(idx[0])]
    for i in idx[1:]:
        if int(i) - cur[-1] <= 6:          # 相邻几行算同一条线（线有 2~3px 粗）
            cur.append(int(i))
        else:
            groups.append(cur)
            cur = [int(i)]
    groups.append(cur)

    out = []
    for g in groups:
        c = int(round(float(np.mean(g))))
        if out and c - out[-1] < min_gap:  # 太近的两条线是同一个边框的内外沿
            continue
        out.append(c)
    return sorted(out)


def _edges(img):
    """灰度 + Sobel，返回 (行强度, 列强度)。"""
    if img is None or getattr(img, "size", 0) == 0:
        return None, None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    sy = np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)).sum(axis=1)
    sx = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)).sum(axis=0)
    return sy, sx


def _long_lines(img, axis, frac=0.25, need_frac=0.5, edges=None):
    """找「够长的直线」，返回它们在 axis 方向上的坐标。

    axis=1 找竖线（卡片左右边框），axis=0 找横线（行与行之间的分隔）。
    frac：一条线至少要有这么长（占画面该方向的比例）才算边框 ——
    卡片里的文字也是一条条边，但都不够长，靠这个把它们筛掉。

    edges：可传入已经算好的 Canny 边缘图，省一次 Canny（列表模式 20 次/秒）。
    """
    h, w = img.shape[:2]
    if edges is None:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 30, 100)
    if axis == 1:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(h * frac))))
    else:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w * frac)), 1))
    lines = cv2.morphologyEx(edges, cv2.MORPH_OPEN, k)
    prof = lines.sum(axis=0 if axis == 1 else 1) / 255.0
    limit = w if axis == 1 else h
    need = (h if axis == 1 else w) * frac * need_frac
    return [int(i) for i in np.where(prof >= need)[0]], limit


def _row_pitch(edges, h, min_h):
    """行距：纵向边缘的自相关周期。返回 None 表示算不出来。

    为什么不用「横线间距」：卡片之间的分隔线常被卡片内容压断，断一条就把
    行距算成两倍。实测 1920 在售页（`_shots/drv_onsale.png`）只剩
    8 / 214 / 705 三条横线，按中位数算出 197，而真实行高是 206 ——
    三行累计偏 27px，每块都压到相邻卡片上，于是「900,000」串进别人家的
    行、两张卡粘成一条、价格读成 250。

    卡片是等高的，整幅图的边缘在纵向严格以行高为周期重复，自相关对
    断线、缺线都不敏感（同一张图自相关给的就是 206）。
    """
    return _pitch(edges, axis=1, limit=h, min_size=min_h)


def _col_pitch(edges, w, min_w):
    """列宽：横向边缘的自相关周期。"""
    return _pitch(edges, axis=0, limit=w, min_size=min_w)


def _pitch(edges, axis, limit, min_size):
    """在某个方向上的重复周期。axis=1 竖着数（行距），axis=0 横着数（列宽）。"""
    prof = edges.sum(axis=axis) / 255.0
    prof = prof - prof.mean()
    if not np.any(prof):
        return None
    ac = np.correlate(prof, prof, mode="full")[len(prof) - 1:]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    lo = max(int(min_size), 60)
    hi = min(len(ac), max(lo + 1, limit // 2))
    if hi <= lo:
        return None
    best = max(range(lo, hi), key=lambda p: ac[p])
    return best if ac[best] >= 0.15 else None


def _uniform(spans, tol=0.15):
    """这些列是不是等宽的。商品卡片是等宽网格，宽度差太多的分组是误检。"""
    if len(spans) < 2:
        return False
    widths = [b - a for a, b in spans]
    med = float(np.median(widths))
    return med > 0 and all(abs(x - med) <= med * tol for x in widths)


def _tile_from(spans, w, pitch, min_w):
    """按列宽从**右边界**往回铺。

    为什么从右边铺：列表 ROI 的右边界就是面板的右边框，位置是准的；
    而左边第一条竖线经常检测不到（滚动后尤其），从左边铺会整体偏十几像素
    —— 实测偏 21px 时，价格最后一位正好被切掉（`13,000` 读成 `13,00`）。
    """
    pts = [w]
    x = w
    while x - pitch > 0:
        x -= pitch
        pts.append(x)
    pts.append(0)
    pts = sorted(set(pts))
    return [(a, b) for a, b in zip(pts, pts[1:]) if b - a >= min_w]


def _merge_close(vals, gap=8):
    if not vals:
        return []
    out, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= gap:
            cur.append(v)
        else:
            out.append(int(round(float(np.mean(cur)))))
            cur = [v]
    out.append(int(round(float(np.mean(cur)))))
    return sorted(out)


def _spans(lines, limit, min_size):
    """线 → 区间：首尾补图像边界，太窄的丢掉。"""
    pts = [0] + sorted(set(int(v) for v in lines if 0 < v < limit)) + [limit]
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)
            if pts[i + 1] - pts[i] >= min_size]


def locate_cards(img, min_w=220, min_h=110, max_cards=16):
    """在列表截图里找商品卡片，返回 [(x0, y0, x1, y1), ...]，先上后下、先左后右。

    列：卡片左右边框是贯穿整张卡的竖线（_long_lines axis=1）。
    行：行与行之间有一条横贯的分隔线；只找到一条时，用它的高度当行距往上/下铺
        —— 这一屏永远是等高的行，比逐行找边更稳。

    一条线索都没有就返回空列表，由调用方退回固定网格，这里不硬猜。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return []
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 30, 100)      # 只算一次，横竖线共用

    # 竖线只跨一张卡片的高度（约 1/3 屏），门槛得比横线松
    # 竖线只跨一张卡高（约 0.22 屏高），而且常被图标压断成几截；
    # 0.12 的开运算核会把断口连不起来，实测中间那条列分隔线就是这么丢的。
    vx, _ = _long_lines(img, axis=1, frac=0.08, need_frac=0.5, edges=edges)
    cols = _spans(_merge_close(vx), w, min_w)
    if not _uniform(cols):
        # 竖线断了、或者混进了卡片内容造成的假竖线（滚动后常见）：
        # 实测滚半屏后得到 (28,670)(670,912)(912,1340) 三列，中间那列
        # 只有 242px 宽 —— 商品卡片是等宽网格，宽度不齐就是误检。
        pw = _col_pitch(edges, w, min_w)
        alt = _tile_from(cols, w, pw, min_w) if pw else []
        if _uniform(alt):
            cols = alt
    if not cols:
        return []

    hy, _ = _long_lines(img, axis=0, frac=0.25, need_frac=0.5, edges=edges)
    # gap 从 6 放到 12：卡片顶边常常被检测成紧挨着的两条线（实测 8 和 17），
    # 不合并的话这两条会造出一个 9px 的假间距，把行距中位数带偏。
    hy = _merge_close(hy, gap=12)
    if not hy:
        return []

    # 行距优先用自相关（对断线免疫），算不出来才退回「横线间距中位数」
    pitch = _row_pitch(edges, h, min_h)
    if pitch is None:
        if len(hy) >= 2:
            diffs = sorted(b - a for a, b in zip(hy, hy[1:]))
            pitch = diffs[len(diffs) // 2]
        else:
            pitch = hy[0]                  # 只有一条分隔线：它的高度就是行距
    if pitch < min_h:
        return []

    # 以第一条横线为基准，向上/下按行距铺满整幅图。
    # hy[0] 有两种含义，得分开处理：
    #   · 列表从画面顶部就开始 → hy[0] 是「第一行的上边界」（值很小）
    #   · 列表上方还有表头 → hy[0] 是「第一行的下边界」（≈ 一个行高）
    # 早期用 `base - (base // pitch) * pitch` 反推，要求 base 正好是行距的
    # 整数倍；自相关给出的 206 和实测的 203 差 3px，这个式子就把第一行
    # 整个丢掉了（1380×630 那张 fixture：6 张卡变 4 张）。
    base = hy[0]
    first = base - pitch if base >= pitch * 0.8 else base
    first = max(0, first)
    rows = []
    y = first
    while y < h - min_h:
        rows.append((max(0, y), min(h, y + pitch)))
        y += pitch
    # 最后一屏常常露出半行：高度不到八成的当残行丢掉，别拿它去认价。
    if len(rows) > 1 and rows[-1][1] >= h - 4:
        y0, y1 = rows[-1]
        if y1 - y0 < (y1 - y0 + (y0 - rows[-2][0])) * 0.8 - 0 and (y1 - y0) < (rows[-2][1] - rows[-2][0]) * 0.8:
            rows.pop()
    if rows and rows[-1][1] < h - 4 and (h - rows[-1][1]) < 0.35 * pitch:
        rows[-1] = (rows[-1][0], h)      # 差一点点就到画面底边：补满

    cards = []
    for y0, y1 in rows:
        if y1 - y0 < min_h:
            continue
        for x0, x1 in cols:
            cards.append((int(x0), int(y0), int(x1), int(y1)))
    cards.sort(key=lambda c: (c[1], c[0]))
    return cards[:max_cards]


def _crop(img, card, box):
    """按卡片内比例切一块，返回 (子图, 在整图里的偏移)。"""
    x0, y0, x1, y1 = card
    w, h = x1 - x0, y1 - y0
    fx0, fy0, fx1, fy1 = box
    ax0 = int(x0 + w * fx0)
    ay0 = int(y0 + h * fy0)
    ax1 = int(x0 + w * fx1)
    ay1 = int(y0 + h * fy1)
    ax0, ay0 = max(0, ax0), max(0, ay0)
    ax1, ay1 = min(img.shape[1], ax1), min(img.shape[0], ay1)
    if ax1 <= ax0 or ay1 <= ay0:
        return None, (ax0, ay0)
    return img[ay0:ay1, ax0:ax1], (ax0, ay0)


# 文字块上下必须留边。实测 WinRT：紧贴字切出来的一条（高 14px）返回空串，
# 上下各留 6px 就能读对。这是「明明有字却认不出来」的典型原因。
PAD = 8


def _text_bands(gray, min_frac=0.012, min_h=5, thr=110):
    """卡片内部「有字的那几行」的 y 区间。

    字是亮的，图标也有亮区，所以这里只用来定位**行的位置**，
    真正的名字/价格靠上一条、下一条的左右切分来区分。
    """
    bright = (gray > thr).mean(axis=1)
    bands, start = [], None
    for y, v in enumerate(bright):
        if v >= min_frac and start is None:
            start = y
        elif v < min_frac and start is not None:
            if y - start >= min_h:
                bands.append((start, y))
            start = None
    if start is not None and len(bright) - start >= min_h:
        bands.append((start, len(bright)))
    return bands


def card_bands(img, card, upscale=2, max_bands=4):
    """把卡片切成「一条条有字的横带」，不管哪条在上面。

    真机上同一张卡会有两种版式：
      在售可买   成色+价格 在上、名称在下
      公示期     名称在上、成色+价格在下（右上角一把锁）
    按位置写死切法必然把其中一种切错，所以这里只找「哪几行有字」，
    由内容去认：带「成色」的是成色，带四位数字的是价格，其余是名称。
    中间那块枪的图案很高（占卡片四成以上），靠高度把它筛掉。
    """
    x0, y0, x1, y1 = card
    sy0 = max(0, y0 - 2)
    sy1 = min(img.shape[0], y1 + 12)
    sub = img[sy0:sy1, x0:x1]
    if sub is None or sub.size == 0:
        return []
    h, w = sub.shape[:2]
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    up = max(1, int(upscale or 1))
    # 0.30 而不是 0.22：卡片底部的「极品|成色B 14,000」常和卡片下边框连成
    # 一条 57px 的带子（卡高 220 的 26%），按 0.22 会被当成「图」丢掉 ——
    # 那一张卡就只剩名字、读不出价格。枪的图案本身占 100px 以上（>45%），
    # 0.30 仍然把它挡在外面。
    raw = [b for b in _text_bands(gray) if (b[1] - b[0]) <= max(6, int(h * 0.30))]
    # 挨得太近的两条并成一条（「成色 S」和它上面的小图标常被切两半）
    bands, cur = [], None
    for b0, b1 in raw:
        if cur and b0 - cur[1] <= 4:
            cur = (cur[0], b1)
        else:
            if cur:
                bands.append(cur)
            cur = (b0, b1)
    if cur:
        bands.append(cur)
    if len(bands) > max_bands:
        bands.sort(key=lambda b: b[1] - b[0], reverse=True)
        bands = sorted(bands[:max_bands])

    out = []
    for b0, b1 in bands:
        a0 = max(0, b0 - PAD)
        a1 = min(h, b1 + PAD)
        if a1 - a0 < 4:
            continue
        piece = sub[a0:a1, :]
        piece = vision.upscale(_pad_aspect(piece), up)
        out.append({"box": (x0, sy0 + a0, x1, sy0 + a1), "piece": piece})
    return out


def card_regions(img, card, upscale=2):
    """切出卡片里的 名称 / 底行 / 图标，文字块已放大。

    底行**整条一起切**，不按比例左右分开：成色和价格在同一行，位置随
    文字长短漂移（`极品 | 成色 C` 和 `极品 | 成色 S` 宽度就不一样），
    按 55% 硬切会把价格切掉一半。整条认出来再分字段更稳，还少一次 OCR。

    返回 dict：name / bottom / icon（放大图）+ boxes（在整图里的矩形）
    """
    x0, y0, x1, y1 = card
    # 往下多看 12px：真机上底行的「成色 S / 2,3xx」正好压在行分隔线上，
    # 只切到 y1 会把字的下半截切掉 —— OCR 对这种半截字返回空串。
    sy0 = max(0, y0 - 2)
    sy1 = min(img.shape[0], y1 + 12)
    sub = img[sy0:sy1, x0:x1]
    if sub is None or sub.size == 0:
        return {"name": None, "bottom": None, "icon": None, "boxes": {}}
    h, w = sub.shape[:2]
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    up = max(1, int(upscale or 1))

    bands = _text_bands(gray)
    name_top = bands[0][0] if bands else int(h * 0.04)
    name_bot = bands[0][1] if bands else int(h * 0.20)
    if name_bot - name_top < 8:
        name_top, name_bot = int(h * 0.03), int(h * 0.20)

    # 底行一直取到卡片底边：最后一行卡片的文字常被行距算得差几个像素切掉，
    # 切掉的那几像素正好是字的下半截，OCR 就直接空串了。
    bottom_top = int(h * 0.68)
    bottom_bot = h

    plan = {
        "name": (0, name_top - PAD, w, name_bot + PAD),
        "bottom": (0, bottom_top - PAD, w, bottom_bot + PAD),
    }
    icon_top = min(name_bot + PAD, h)
    icon_bot = max(bottom_top - PAD, icon_top)
    plan["icon"] = (0, icon_top, w, icon_bot)

    out = {"boxes": {}}
    for key, (ax0, ay0, ax1, ay1) in plan.items():
        # 上下留边不能靠裁：卡片贴着画面底边时，裁掉边距等于没留，
        # WinRT 对紧贴文字的一条会直接返回空串。超出就整体平移回来。
        need_h = ay1 - ay0
        if ay0 < 0:
            ay1 -= ay0
            ay0 = 0
        if ay1 > h:
            ay0 = max(0, ay0 - (ay1 - h))
            ay1 = h
        if ay1 - ay0 < need_h and ay0 == 0 and ay1 < h:
            ay1 = min(h, ay0 + need_h)
        ax0, ay0 = max(0, ax0), max(0, ay0)
        ax1, ay1 = min(w, ax1), min(h, ay1)
        if ax1 - ax0 < 4 or ay1 - ay0 < 4:
            out[key] = None
            continue
        out["boxes"][key] = (x0 + ax0, sy0 + ay0, x0 + ax1, sy0 + ay1)
        piece = sub[ay0:ay1, ax0:ax1]
        if key == "icon" or up == 1:
            out[key] = piece
        else:
            out[key] = vision.upscale(_pad_aspect(piece), up)
    return out


def read_card(img, card, upscale=2, read_text=None):
    """认一张卡片：每条有字的带都认一遍，再按内容分到 名称/成色/价格。

    read_text: 可注入（测试用假 OCR），默认 core.ocr.read_text。
    """
    if read_text is None:
        from . import ocr
        read_text = ocr.read_text

    bands = card_bands(img, card, upscale=upscale)
    texts = []
    for b in bands:
        t = (read_text(b["piece"]) or "").strip()
        if t:
            texts.append((t, b["box"]))

    # 名称先定：最长的、带中文的那一条（成色那条往往很短）。
    # 顺序很重要 —— 下面挑价格时要排除名称那条，名称里的 M4A1 会被读成 41。
    cjk = [(len(re.findall(r"[一-鿿]", t)), t) for t, _ in texts
           if re.search(r"[一-鿿]", t)]
    name = max(cjk)[1] if cjk else ""

    grade = price = ""
    for t, _box in texts:
        if "成色" in t:
            grade = grade or t
            # 成色和价格常常挤在同一条带里（「成色 S  05」），
            # 带里还有数字就一起当价格用，别再去别的带里找。
            if re.search(r"\d", t):
                price = t
                break
    if not price:
        for t, _box in texts:
            if t != name and t != grade and re.search(r"\d", t) and len(t) <= 24:
                price = t
                break

    return {
        "card": card,
        "name": name,
        "grade": grade or (price if "成色" in price else ""),
        "price": price,
        "bottom": " ".join(t for t, _ in texts),
        "text": " ".join(t for t, _ in texts),
        "bands": [b["box"] for b in bands],
    }


def _pad_aspect(piece, min_ratio=10):
    """把太扁的条补高一点。

    WinRT 对「宽高比超过 ~20:1 的一条」经常直接返回空串（2000x70 的价格行
    就是典型）。这里按上下边缘的颜色各补几行，把比例压到 10:1 以内 ——
    补的是卡片底色，不是黑边，免得引擎把黑边当成「画面结束了」。
    """
    if piece is None or piece.size == 0:
        return piece
    h, w = piece.shape[:2]
    need = int(w / max(2, min_ratio)) - h
    if need <= 0:
        return piece
    top = int(need // 2)
    bot = need - top
    top_row = piece[0:1, :, :]
    bot_row = piece[-1:, :, :]
    up = np.repeat(top_row, top, axis=0) if top else None
    down = np.repeat(bot_row, bot, axis=0) if bot else None
    parts = [x for x in (up, piece, down) if x is not None]
    return np.vstack(parts)


def changed_cards(prev, cur, cards, delta=CHANGE_DELTA):
    """逐卡比较两帧，返回变了的卡片下标集合。

    卡片数不一致（列表翻页/刷新）时全部算变 —— 宁可贵一点，也不漏新货。
    """
    if prev is None or cur is None or prev.shape != cur.shape:
        return list(range(len(cards)))
    if not cards:
        return []
    a = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY).astype(np.float32)
    out = set()
    for i, (x0, y0, x1, y1) in enumerate(cards):
        pa = a[y0:y1, x0:x1]
        pb = b[y0:y1, x0:x1]
        if pa.size == 0 or pb.size == 0:
            out.add(i)
            continue
        # 比的是「局部最明显的差」而不是整卡平均：价格从 12,000 变成
        # 12,500 只动几个像素，整卡一平均就被稀释到看不出来，缓存会把
        # 旧价当成新价用。按小块取最大差，噪声照样压得住。
        d = np.abs(pa - pb)
        bh = max(4, d.shape[0] // 6)
        bw = max(4, d.shape[1] // 6)
        hh, ww = (d.shape[0] // bh) * bh, (d.shape[1] // bw) * bw
        blocks = d[:hh, :ww].reshape(hh // bh, bh, ww // bw, bw).mean(axis=(1, 3))
        if float(blocks.max()) > delta:
            out.add(i)
    return sorted(out)
