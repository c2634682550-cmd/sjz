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

from . import bricks, money, vision

# 相邻帧「变了」的判据：卡片平均灰度差超过这个值才算变。
# 3 以下基本都是游戏自己的光照闪烁和粒子特效，不是换货。
CHANGE_DELTA = 6.0

# 型号 token（QBZ951 / M250）：整段摘掉，免得型号里的数字被当价格
_MODEL_LIKE = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*\d[A-Za-z0-9.\-]*")
# 价格下限：和 market._MIN_PRICE 一个口径（两位数是残数字，不是价格）
_MIN_PRICE = 100


def _price_token_in(text):
    """从一段混合文字里切出「像价格」的那个数，切不出来返回 ""。

    数字/千分位/小数的语法只有一份，在 `core/money.py`（N11）。这里只留
    「哪一段算价格」的策略：
      · 整条带只有一个数（`05` / `9.000` / `17 000`）：整条就是价格。
        真机上确实有两个三角币的挂单，这种两位数字必须认；`0.x` 是磨损，
        整数段 ≥ 1 才收。
      · 混在成色带里（`极品 | 成色 S 14,000`）：只认三位以上整数、带千位
        分隔符、或「句点当千位」（`9.000`，整数段 > 5）的数。
      · `05` 这种残数字、`0.120` / `0.1209` 磨损值一律不认 —— 05 会让
        pick() 以为这条最便宜，点进去买的反而是贵的。
      · 解析窗口不跨行（`¥12 345` 换行 `678` 只读第一行）。
    """
    for line in money.split_lines(text or ""):
        s = line.strip()
        if not s:
            continue
        if any(ch in money.SYMBOLS for ch in s):
            # 带货币符号的行：整行只信这一个金额。解析失败就拒识 ——
            # `−¥250` 不能回落到行内数字读成 250（负号被吃掉价格语义就反了）。
            value, tok = money.parse_line(s, strip_leading_zeros=True,
                                          dot3_as_thousands=True)
            if value is not None and not isinstance(value, float) and value >= 1:
                return tok
            continue

        value, tok = money.parse_amount(s, strip_leading_zeros=True,
                                        dot3_as_thousands=True)
        if value is not None and not isinstance(value, float):
            ip = re.sub(r"\D", "", tok.split(".")[0])
            if ip.lstrip("0") and int(ip) >= 1:
                return tok

        rest = _MODEL_LIKE.sub(" ", s)
        best_tok, best_val = "", None
        for item in money.find_numbers(rest, strip_leading_zeros=True,
                                       dot3_as_thousands=True):
            if item["decimal"]:
                continue                 # 0.120 / 0.1209 是磨损，不是价格
            digits = re.sub(r"\D", "", item["token"])
            if "." not in item["token"] and len(digits) < 3:
                continue                 # 05 / 2 这种残数字
            if item["value"] < _MIN_PRICE:
                continue
            if best_val is None or item["value"] > best_val:
                best_tok, best_val = item["token"], item["value"]
        if best_tok:
            return best_tok
    return ""


def _long_lines(img, axis, frac=0.25, need_frac=0.5, edges=None, ref_h=None):
    """找「够长的直线」，返回它们在 axis 方向上的坐标。

    axis=1 找竖线（卡片左右边框），axis=0 找横线（行与行之间的分隔）。
    frac：一条线至少要有这么长（占画面该方向的比例）才算边框 ——
    卡片里的文字也是一条条边，但都不够长，靠这个把它们筛掉。

    edges：可传入已经算好的 Canny 边缘图，省一次 Canny（列表模式 20 次/秒）。
    ref_h：只跑下半屏时传整幅图的高度 —— 门槛按整幅图算，才和全屏扫描同口径
    （线本身还是有一整张卡那么长，用半屏高度算会把门槛砍掉一半，假线全进来了）。
    """
    h, w = img.shape[:2]
    if edges is None:
        gray = vision.to_gray(img)
        edges = cv2.Canny(gray, 30, 100)
    hh = ref_h or h
    if axis == 1:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(hh * frac))))
    else:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w * frac)), 1))
    lines = cv2.morphologyEx(edges, cv2.MORPH_OPEN, k)
    prof = lines.sum(axis=0 if axis == 1 else 1) / 255.0
    limit = w if axis == 1 else h
    need = (hh if axis == 1 else w) * frac * need_frac
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


def _tile_from(w, pitch, min_w):
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


def locate_cards(img, min_w=220, min_h=110, max_cards=16, gray=None,
                 bottom_half=False):
    """在列表截图里找商品卡片，返回 [(x0, y0, x1, y1), ...]，先上后下、先左后右。

    列：卡片左右边框是贯穿整张卡的竖线（_long_lines axis=1）。
    行：行与行之间有一条横贯的分隔线；只找到一条时，用它的高度当行距往上/下铺
        —— 这一屏永远是等高的行，比逐行找边更稳。

    一条线索都没有就返回空列表，由调用方退回固定网格，这里不硬猜。

    gray：调用方已经算好的灰度图，省一次 to_gray（列表模式 20 次/秒）。
    bottom_half：只在**下半屏**上找线。新品从底部插进来，滚动后上半屏的
        分割线常被内容压断，下半屏反而更干净；像素少一半，开运算也快一半。
        网格仍然按行距铺满整幅图 —— 只省「找线」的像素，不省卡片覆盖。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return []
    h, w = img.shape[:2]
    gray = vision.to_gray(img) if gray is None else gray
    edges = cv2.Canny(gray, 30, 100)      # 只算一次，横竖线共用

    # 下半屏：从 h//2 起算，横线坐标最后加回偏移；门槛用整幅图高度（ref_h）。
    off, work = 0, edges
    if bottom_half and h >= 4 * min_h:
        off = h // 2
        work = edges[off:]

    # 竖线只跨一张卡片的高度（约 1/3 屏），门槛得比横线松
    # 竖线只跨一张卡高（约 0.22 屏高），而且常被图标压断成几截；
    # 0.12 的开运算核会把断口连不起来，实测中间那条列分隔线就是这么丢的。
    vx, _ = _long_lines(img, axis=1, frac=0.08, need_frac=0.5, edges=work,
                        ref_h=h)
    cols = _spans(_merge_close(vx), w, min_w)
    if not _uniform(cols):
        # 竖线断了、或者混进了卡片内容造成的假竖线（滚动后常见）：
        # 实测滚半屏后得到 (28,670)(670,912)(912,1340) 三列，中间那列
        # 只有 242px 宽 —— 商品卡片是等宽网格，宽度不齐就是误检。
        pw = _col_pitch(work, w, min_w)
        alt = _tile_from(w, pw, min_w) if pw else []
        if _uniform(alt):
            cols = alt
    if not cols:
        return []

    hy, _ = _long_lines(img, axis=0, frac=0.25, need_frac=0.5, edges=work,
                        ref_h=h)
    # gap 从 6 放到 12：卡片顶边常常被检测成紧挨着的两条线（实测 8 和 17），
    # 不合并的话这两条会造出一个 9px 的假间距，把行距中位数带偏。
    hy = [y + off for y in _merge_close(hy, gap=12)]
    if not hy:
        return []

    # 行距优先用自相关（对断线免疫），算不出来才退回「横线间距中位数」
    pitch = _row_pitch(work, h, min_h)
    if pitch is None:
        if len(hy) >= 2:
            diffs = sorted(b - a for a, b in zip(hy, hy[1:]))
            pitch = diffs[len(diffs) // 2]
        else:
            pitch = hy[0]                  # 只有一条分隔线：它的高度就是行距
    if pitch < min_h:
        return []

    # 以第一条横线为基准，按行距铺满整幅图。
    # hy[0] 有两种含义：
    #   · 列表从画面顶部就开始 → hy[0] 是「第一行的上边界」（值很小）
    #   · 列表上方还有表头 / 基准线落在画面中间（下半屏扫描）→ 它只是某条
    #     分隔线，真实的第一行上边界要往前退到 [0, pitch) 里。
    # 早期用 `base - (base // pitch) * pitch` 反推，要求 base 正好是行距的
    # 整数倍；自相关给出的 206 和实测的 203 差 3px，这个式子就把第一行
    # 整个丢掉了（1380×630 那张 fixture：6 张卡变 4 张 —— 203 % 206 = 203，
    # 直接退成了「第一行从 203 开始」）。
    base = hy[0]
    first = base
    while first >= pitch * 0.8:        # 退到「够不到一个行距」为止
        first -= pitch
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


def card_bands(img, card, upscale=2, max_bands=6):
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
    gray = vision.to_gray(sub)
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
        # 上限放宽到 6：真机上常见「名称 / 成色 / 价格 / 数量 / 锁图标」五条，
        # 按 4 条按高度截断时，最薄的那条（价格）最先被丢掉 —— 整张卡就读
        # 不出价，check() 会把这条挂单跳过。宁可多认一条带。
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
    gray = vision.to_gray(sub)
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


def _card_sub(img, card):
    """卡片在整图里实际要认的那块（上 -2、下 +12），返回 (子图, 子图上沿 y)。"""
    x0, y0, x1, y1 = card
    sy0 = max(0, y0 - 2)
    sy1 = min(img.shape[0], y1 + 12)
    return img[sy0:sy1, x0:x1], sy0


def _rows_from_words(words, dx=0, dy=0, min_gap=6.0):
    """按行中心把词聚成行。返回 [(text, box)]，box 已加上 (dx, dy) 偏移。

    min_gap 是同一行的容差：`_whole_rows` 用 6（重排后的整卡词），
    `_join`/`read_bottom` 那条路用 4（原来 ocr_chain._join 的口径）。
    """
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["y"] + w["h"] / 2.0, w["x"]))
    rows, cur, cy = [], [], None
    for w in words:
        c = w["y"] + w["h"] / 2.0
        if cy is not None and abs(c - cy) <= max(min_gap, w["h"] * 0.6):
            cur.append(w)
            cy = (cy * (len(cur) - 1) + c) / len(cur)
        else:
            if cur:
                rows.append(_row_of(cur, dx, dy))
            cur, cy = [w], c
    if cur:
        rows.append(_row_of(cur, dx, dy))
    return rows


def _whole_rows(img, card):
    """整卡只做一次 OCR，再按行中心把词聚成行。返回 [(text, box), ...]。

    为什么不再逐带放大：3× 逐带是给 WinRT / PP-OCR 时代的补丁 —— 那时
    13px 的字不放大就读不出千位逗号。换成进程内 RapidOCR 之后，原图
    （短边走 64 的 det 限制）已经能读对，而一次整卡推理比 4~5 次条带推理
    快 5~7 倍：实测 u3 15 张卡 6948ms → 1055ms，字段反而更全（品阶和
    价格不容易被条带切掉半截）。详见 `_ocr_engine_selection.md` 第七节。
    """
    from . import ocr

    sub, sy0 = _card_sub(img, card)
    if sub is None or sub.size == 0:
        return []
    words = ocr.read_words(sub)
    if not words:
        return []
    return _rows_from_words(words, card[0], sy0)


def _row_of(words, dx, dy):
    """一组同行的词 → (拼好的文本, 在整图里的矩形)。"""
    x0 = min(w["x"] for w in words) + dx
    y0 = min(w["y"] for w in words) + dy
    x1 = max(w["x"] + w["w"] for w in words) + dx
    y1 = max(w["y"] + w["h"] for w in words) + dy
    return (" ".join(w["text"] for w in words), (x0, y0, x1, y1))


def _canon_grade(text):
    """成色块 → 规范形 `做工|成色`（两段都认出时），认不全保留原文。

    批量 rec 会把同一批文字块补齐到同宽，极窄的「|」块被补成一片空白、
    读成空串 —— 同一张卡走单卡路径与批量路径，grade 文本就差这一个分隔符。
    业务字段（craft / wear）不受影响，但字节级不一致以后会被依赖分隔符的
    解析或展示静默踩到。这里统一出规范形：`极品|S`；认不全不猜，保留原文。
    """
    craft = bricks.parse_craft(text)
    wear = bricks.parse_wear(text)
    if craft and wear:
        return f"{craft}|{wear}"
    return text or ""


def _card_result(card, texts, bands=()):
    """行文本 → 卡片字段。单卡 `read_card` 与拼图 `read_cards` 共用这一套。"""
    # 名称先定：最长的、带中文的那一条（成色那条往往很短）。
    # 顺序很重要 —— 下面挑价格时要排除名称那条，名称里的 M4A1 会被读成 41。
    cjk = [(len(re.findall(r"[一-鿿]", t)), t) for t, _ in texts
           if re.search(r"[一-鿿]", t)]
    name = max(cjk)[1] if cjk else ""

    grade = price = ""
    for t, _box in texts:
        if "成色" in t:
            grade = grade or t
            # 成色和价格常常挤在同一条带里（「极品 | 成色 S 14,000」），
            # 这里只切出「像价格」的那个数，整条带原样当价格是不行的。
            price = _price_token_in(t)
            if price:
                break
    if not price:
        for t, _box in texts:
            if t != name and t != grade and len(t) <= 24:
                price = _price_token_in(t)
                if price:
                    break

    return {
        "card": card,
        "name": name,
        "grade": _canon_grade(grade) or (price if "成色" in price else ""),
        "price": price,
        "bottom": "\n".join(t for t, _ in texts),
        "text": "\n".join(t for t, _ in texts),
        "bands": [b["box"] for b in bands],
    }


def read_card(img, card, upscale=2, read_text=None):
    """认一张卡片：整卡一次 OCR，再按内容分到 名称/成色/价格。

    价格**只认「像价格」的数**：成色带里常混着价格（`极品 | 成色 S 14,000`），
    也常混着残数字（`成色 S 05`）和磨损小数（`成色 S 0.1209`）。后两者
    一律不能当价格 —— 05 会让 pick() 以为这条最便宜，点进去买的反而是贵的。

    read_text: 可注入（测试用假 OCR），默认走 ocr.read_words 整卡一次。
    注入时仍走逐带放大路径 —— 假 OCR 通常只接受「一条带」的输入，
    也方便单测精确控制每次调用的文本。
    """
    if read_text is None:
        texts = _whole_rows(img, card)
        bands = []
    else:
        bands = card_bands(img, card, upscale=upscale)
        texts = []
        for b in bands:
            t = (read_text(b["piece"]) or "").strip()
            if t:
                texts.append((t, b["box"]))
    return _card_result(card, texts, bands)


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


def read_card_bottom(img, card, upscale=3, read_text=None):
    """只认卡片底部那条带（成色 + 价格），给品阶/价格补认用。

    原来补认是「整卡 5× 再读一遍」：u3 一张卡 5× 要 1.4s，15 张卡直接
    20s（实测 market.scan 17.5s 的主因之一）。这里只裁底部 38% 再放大，
    既保留小字放大，又把面积砍掉一大半。
    """
    if read_text is None:
        from . import ocr
        read_text = ocr.read_text

    x0, y0, x1, y1 = card
    sy0 = max(0, y0 - 2)
    sy1 = min(img.shape[0], y1 + 12)
    sub = img[sy0:sy1, x0:x1]
    if sub is None or sub.size == 0:
        return {"grade": "", "price": "", "text": ""}
    h = sub.shape[0]
    piece = sub[int(h * 0.62):h, :]
    if piece is None or piece.size == 0:
        return {"grade": "", "price": "", "text": ""}
    piece = vision.upscale(_pad_aspect(piece), max(1, int(upscale or 1)))
    t = (read_text(piece) or "").strip()
    return {"grade": _canon_grade(t), "price": t, "text": t}


def _engine():
    """拿到底层 RapidOCR 引擎；换引擎/拿不到时返回 None（调用方退回逐块整读）。

    只用它做「逐块 det + 一次批量 rec」。为什么 det 不能拼：把六张卡拼成
    一张大图，det 的框会挪 1~2px、个别字直接读错（catalog 上实测
    「电玩高手」→「电玩高丢」，竖排/2 列/3 列 3 种拼法都复现）。
    为什么 rec 可以批量：每个文字块是独立前向，批量只影响吞吐 ——
    30 个文字块逐块 vs 批量，结果逐字相同（实测）。
    """
    try:
        from . import ocr
        client = ocr._client()
    except Exception:                      # noqa: BLE001
        return None
    rapid = getattr(client, "rapid", None)
    if rapid is None or not getattr(rapid, "enabled", True):
        return None
    try:
        eng = rapid._engine()
    except Exception:                      # noqa: BLE001
        return None
    if not (hasattr(eng, "detect_and_crop") and hasattr(eng, "recognize_txt")):
        return None
    return eng


def _det_words(subs):
    """逐块 det + 一次批量 rec，返回每块 words（拿不到引擎返回 None）。

    rec 输出的顺序与 det 的框一一对应，按每块的框数切回去即可 ——
    和逐块 `ocr.read_words` 的词表逐字段相同。
    """
    eng = _engine()
    if eng is None:
        return None
    chunks, dets = [], []
    for sub in subs:
        try:
            crops, det = eng.detect_and_crop(sub, {})
        except Exception:                     # noqa: BLE001
            # 空帧 / 纯色帧 det 没有结果，RapidOCR 直接抛 RapidOCRError。
            # 这种帧本来就「没有卡」，按 0 个文字块返回，不能让扫描链路崩。
            crops, det = [], None
        chunks.extend(crops)
        dets.append(det)
    if not chunks:
        return [[] for _ in subs]
    res = eng.recognize_txt(chunks)
    txts = getattr(res, "txts", None)
    scores = getattr(res, "scores", None)
    txts = [txts] if isinstance(txts, str) else list(txts) if txts is not None else []
    if isinstance(scores, (float, int)):
        scores = [float(scores)]
    else:
        scores = list(scores) if scores is not None else []
    # rec 批量会把同一批的块补到同宽：极窄的块（比如「|」分隔符）会被补成
    # 一片空白，直接读成空串。空串/低分的块补一次单块前向 —— 块很少，
    # 不影响批量收益，但能保住原来读得出的那几个字。
    for i, chunk in enumerate(chunks):
        if not (txts[i] if i < len(txts) else "") or (
                i < len(scores) and float(scores[i]) < 0.5):
            one = eng.recognize_txt([chunk])
            t = getattr(one, "txts", None)
            t = t[0] if isinstance(t, (list, tuple)) and t else (t or "")
            s = getattr(one, "scores", None)
            s = s[0] if isinstance(s, (list, tuple)) and s else (s or 1.0)
            if i < len(txts):
                txts[i] = t
                if i < len(scores):
                    scores[i] = s
            elif t:
                txts.append(t)
                scores.append(s)
    out, k = [], 0
    for det in dets:
        boxes = getattr(det, "boxes", None)
        boxes = [] if boxes is None else list(boxes)
        words = []
        for i, box in enumerate(boxes):
            j = k + i
            t = ((txts[j] if j < len(txts) else "") or "").strip()
            if not t:
                continue
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            words.append({"x": min(xs), "y": min(ys),
                          "w": max(xs) - min(xs), "h": max(ys) - min(ys),
                          "text": t,
                          "score": float(scores[j]) if j < len(scores) else 1.0})
        out.append(words)
        k += len(boxes)
    return out


def read_cards(img, recs, upscale=2, read_text=None):
    """批量认卡：det 逐张照旧，rec 合成一次前向，再按卡分回文字。

    六张卡逐张整读 = 六次 det + 六次 rec，每次都要重启一遍预处理/后处理；
    这里 det 仍是逐张（保证裁切与单卡完全一致，结果才不会变），
    rec 只跑一次（每个文字块独立前向，批量不影响结果）。实测 6 卡
    457ms → 260ms，字段逐一对齐。

    read_text 注入时退回逐张路径 —— 假 OCR 只接受单卡输入。

    返回字段：批量路径给 `text_batch`（不是 `text`），且 `text_reliable=False`。
    批量 rec 会把同一批文字块补齐到同宽，极窄的「|」块可能被补成空白读成
    空串 —— `text_batch` 与单卡路径的原始词流**不是逐字一致的**，只能用于
    debug/日志，**不可用于解析或断言**；结构化字段（name/price/grade）才是
    两条路径逐字节一致的那一份。单卡路径的 `text` 才是可靠词流。
    """
    recs = [tuple(int(v) for v in r) for r in recs]
    if not recs:
        return []
    if read_text is not None or len(recs) == 1:
        # 一张卡没有批量收益（实测 86ms → 96ms），直接走原来的整卡路径
        return [read_card(img, r, upscale=upscale, read_text=read_text)
                for r in recs]
    from . import ocr

    subs = [_card_sub(img, r) for r in recs]
    words_all = _det_words([s for s, _ in subs if s is not None and s.size])
    if words_all is None:
        # 引擎内部 API 变了 / 退到别的后端：逐张整读，行为与原来完全一致
        return [read_card(img, r, upscale=upscale) for r in recs]

    out, j = [], 0
    for r, (sub, sy0) in zip(recs, subs):
        if sub is None or sub.size == 0:
            out.append([r, sy0, []])
            continue
        words = words_all[j]
        j += 1
        if not words:
            # 一块字都没读到：走 ocr.read_words（它会按原逻辑降级到 PP-OCR 复核）
            out.append([r, sy0, ocr.read_words(sub)])
            continue
        out.append([r, sy0, words])
    out = [_card_result(r, _rows_from_words(w, r[0], sy0))
           for r, sy0, w in out]
    for d in out:
        # 批量词流与单卡路径可能差一个「|」，不能顶着 text 这个名字冒充可靠原文
        d["text_batch"] = d.pop("text", "")
        d["text_reliable"] = False
    return out


def read_bottoms(img, recs, upscale=3, read_text=None):
    """批量认卡片底带（成色 + 价格）：det 逐张，rec 一次，口径同 read_card_bottom。

    逐张补认时每张卡都是一次完整推理；缺价格/成色的卡一多，光补认就能吃掉
    一整个扫描周期。底带的裁切/补边/放大与单张完全一致，只是把 rec 合并。

    同 `read_cards`：批量路径给 `text_batch` + `text_reliable=False`（仅供
    debug，不可用于解析或断言）；单张路径的 `text` 才可靠。
    """
    recs = [tuple(int(v) for v in r) for r in recs]
    if not recs:
        return []
    if read_text is not None or len(recs) == 1:
        return [read_card_bottom(img, r, upscale=upscale, read_text=read_text)
                for r in recs]
    from . import ocr

    pieces = []
    for r in recs:
        sub, _sy0 = _card_sub(img, r)
        piece = None
        if sub is not None and sub.size:
            h = sub.shape[0]
            piece = sub[int(h * 0.62):h, :]
        if piece is not None and piece.size:
            piece = vision.upscale(_pad_aspect(piece),
                                   max(1, int(upscale or 1)))
        pieces.append(piece if (piece is not None and piece.size) else None)
    valid = [p for p in pieces if p is not None]
    if not valid:
        return [{"grade": "", "price": "", "text_batch": "",
                 "text_reliable": False} for _ in recs]

    words_all = _det_words(valid)
    if words_all is None:
        return [read_card_bottom(img, r, upscale=upscale) for r in recs]

    out, j = [], 0
    for piece in pieces:
        if piece is None:
            out.append({"grade": "", "price": "", "text": ""})
            continue
        words = words_all[j]
        j += 1
        if not words:
            words = ocr.read_words(piece)
        # 和 ocr_chain._join 一个口径：行内空格、行间换行（min_gap=4）。
        t = "\n".join(x for x, _ in _rows_from_words(words, min_gap=4.0))
        out.append({"grade": _canon_grade(t), "price": t,
                    "text_batch": t, "text_reliable": False})
    return out


_WARMED = False


def warm():
    """首帧预热：先跑一次很小的 det+rec，把会话/线程池/内存池建起来。

    冷启动那一次推理要 1.1~1.3 秒（模型加载 + 首次前向）。它落在第一帧上，
    就是「第一次扫列表要 1.2 秒」，而那正是新品刚出来的时候。挪到
    Scanner 构造时做（那时还在等页面刷出来），扫描本身就不背这笔开销。
    只做一次；OCR 引擎重建（ocr.reload）后第一次扫描仍会慢一点。
    """
    global _WARMED
    if _WARMED:
        return
    from . import ocr
    probe = np.full((64, 256, 3), 30, np.uint8)
    cv2.putText(probe, "1,234", (8, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                (235, 235, 235), 2)
    try:
        ocr.read_words(probe)
    except Exception:                      # noqa: BLE001
        return                             # OCR 不可用时预热失败不算错
    _WARMED = True


def changed_cards(prev, cur, cards, delta=CHANGE_DELTA, prev_cards=None):
    """逐卡比较两帧，返回变了的卡片下标集合。

    卡片数不一致（列表翻页/刷新）时全部算变 —— 宁可贵一点，也不漏新货。
    只要调用方知道上一帧的卡片列表（prev_cards），就必须传进来：两帧的
    卡片**数量**变了但图片尺寸没变（翻页换了一批、高度一样的卡）时，下标
    按位置比较会拿旧卡的缓存当新卡用 —— 价格和名字全是上一批的。
    """
    if prev is None or cur is None or prev.shape != cur.shape:
        return list(range(len(cards)))
    if prev_cards is not None and len(prev_cards) != len(cards):
        return list(range(len(cards)))
    if not cards:
        return []
    a = vision.to_gray(prev).astype(np.float32)
    b = vision.to_gray(cur).astype(np.float32)
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
