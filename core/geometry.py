"""几何定位：先找「面板 + 按钮色块」，再推按钮中心，模板只做最后确认。

为什么需要它：内置坐标是按某一种版式量出来的，真实客户端的同一类按钮
会整体平移（本机实测「购买」偏 +66/+31 px）。纯模板硬搜只能看到「偏了」，
看不到「为什么偏」；而按钮是面板里的一块高亮长条，位置可以由面板边缘
和自身比例推出来，模板只负责点头/摇头。

推导链（写进日志，可复核）：

    Sobel 竖边 → 面板左缘 x_panel
    底部亮块轮廓 → 按钮 bbox（长宽比 2.5–9、宽 0.06–0.25W）
    bbox → 中心 (cx, cy)
    比例：cx 相对面板宽度、cy 相对客户区高度
    模板在 bbox 外扩窗里确认（≥ 0.55）→ 用模板中心微调

只依赖灰度/BGR 帧，不碰屏幕、不碰鼠标，可在离线截图上复算。
"""
import cv2
import numpy as np

from . import vision

# 按钮亮块的灰度下限；面板背景通常 40–90，按钮 150+
BRIGHT_MIN = 110
# 按钮色块的长宽比与尺寸区间（相对客户区）
AR_MIN, AR_MAX = 2.5, 9.0
W_MIN_RATIO, W_MAX_RATIO = 0.06, 0.25
H_MIN_RATIO, H_MAX_RATIO = 0.025, 0.12
# 模板确认下限，与 autocalib.ABSENT_SCORE 对齐
CONFIRM_SCORE = 0.55


def _gray(frame):
    return frame if frame.ndim == 2 else vision.to_gray(frame)


def _panel_edge(gray, x_hint):
    """按钮左侧最近的强竖边（面板左缘）。找不到就回退到按钮左边界。"""
    h, w = gray.shape[:2]
    lo = max(0, int(x_hint - w * 0.30))
    hi = max(lo + 1, int(x_hint - w * 0.01))
    band = gray[int(h * 0.15):int(h * 0.92), lo:hi]
    if band.size == 0:
        return None, 0.0
    gx = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3))
    col = gx.mean(axis=0)
    # 面板边缘是一条持续竖线；用 90 分位抑制零散文字边缘
    col = np.percentile(gx, 90, axis=0)
    i = int(np.argmax(col))
    return lo + i, float(col[i])


def _button_candidates(gray, expected_center=None):
    h, w = gray.shape[:2]
    y0 = int(h * 0.55)
    x0 = int(w * 0.40)
    band = gray[y0:h, x0:w]
    mask = cv2.inRange(band, BRIGHT_MIN, 255)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((7, 21), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw <= 0 or bh <= 0:
            continue
        ar = bw / float(bh)
        if not (AR_MIN <= ar <= AR_MAX):
            continue
        if not (W_MIN_RATIO <= bw / float(w) <= W_MAX_RATIO):
            continue
        if not (H_MIN_RATIO <= bh / float(h) <= H_MAX_RATIO):
            continue
        # 注意：不能按「块内平均亮度」过滤。真实按钮是深底 + 亮边框/文字，
        # 二值化后由亮边拼成长条，块内均值只有 37（实测），按均值筛会全灭。
        cx, cy = x + x0 + bw / 2.0, y + y0 + bh / 2.0
        dist = None
        if expected_center:
            dist = ((cx - expected_center[0]) ** 2
                    + (cy - expected_center[1]) ** 2) ** 0.5
        out.append({"bbox": [x + x0, y + y0, bw, bh],
                    "center": [cx, cy], "area": bw * bh, "dist": dist})
    return out


def _pick(cands, expected_center=None):
    if not cands:
        return None
    # 有预期中心：先按距离取近的（±25% 屏宽以内），再按面积；
    # 没有预期：直接取面积最大的（详情页右下角只有一个购买长条）。
    if expected_center:
        near = [c for c in cands if c["dist"] is not None
                and c["dist"] <= 0.35 * max(1.0, expected_center[0])]
        if near:
            near.sort(key=lambda c: (round(c["dist"] / 40.0), -c["area"]))
            return near[0]
    cands.sort(key=lambda c: -c["area"])
    return cands[0]


def locate(gray_or_bgr, tpl=None, expected_center=None):
    """几何推导按钮中心。返回 dict，`ok` 表示推导链完整。

    字段：panel / button / center / ratio / score / tpl / chain / why
    """
    gray = _gray(gray_or_bgr)
    h, w = gray.shape[:2]
    chain = []
    cand = _pick(_button_candidates(gray, expected_center), expected_center)
    if cand is None:
        return {"ok": False, "why": "底部没找到按钮色块",
                "chain": chain + ["亮块轮廓：无符合长宽比的候选"], "score": 0.0}
    bbox = cand["bbox"]
    chain.append("按钮亮块 bbox=%s（长宽比 %.2f）"
                 % (tuple(bbox), bbox[2] / float(bbox[3])))
    px, ps = _panel_edge(gray, bbox[0])
    if px is None:
        panel = [0, 0, w, h]
        chain.append("面板竖边：没找到，按整屏算")
    else:
        panel = [int(px), 0, int(w - px), h]
        chain.append("面板左缘 x=%d（Sobel 90 分位 %.1f）" % (px, ps))
    cx, cy = cand["center"]
    ratio = {
        "cx": round((cx - panel[0]) / float(panel[2]), 4),
        "cy": round(cy / float(h), 4),
        "w": round(bbox[2] / float(panel[2]), 4),
        "h": round(bbox[3] / float(h), 4),
    }
    chain.append("中心=(%d,%d)｜面板内比例 cx=%.3f cy=%.3f"
                 % (cx, cy, ratio["cx"], ratio["cy"]))

    score, tpl_size, center = 0.0, None, [cx, cy]
    if tpl is not None:
        pad_x = max(12, int(bbox[2] * 0.55))
        pad_y = max(8, int(bbox[3] * 0.9))
        x0 = max(0, bbox[0] - pad_x)
        y0 = max(0, bbox[1] - pad_y)
        x1 = min(w, bbox[0] + bbox[2] + pad_x)
        y1 = min(h, bbox[1] + bbox[3] + pad_y)
        crop = gray[y0:y1, x0:x1]
        try:
            score, loc, scale = vision.match_scaled(crop, tpl, deep=False)
            used = vision.scaled(tpl, scale)
        except Exception:                       # noqa: BLE001
            score, loc, used = 0.0, None, None
        if loc and used is not None:
            tw, th = used.shape[1], used.shape[0]
            center = [x0 + loc[0] + tw / 2.0, y0 + loc[1] + th / 2.0]
            tpl_size = [int(tw), int(th)]
            chain.append("模板确认 score=%.3f，模板框 %d×%d，中心=(%.0f,%.0f)"
                         % (score, tw, th, center[0], center[1]))
        else:
            chain.append("模板确认：窗口内没匹配上（score=%.3f）" % score)
    else:
        chain.append("模板确认：没有购买模板，跳过")

    ok = tpl is None or score >= CONFIRM_SCORE
    if not ok:
        chain.append("模板分 %.3f < %.2f，几何结果不采信" % (score, CONFIRM_SCORE))
    return {"ok": bool(ok), "panel": panel, "button": bbox,
            "center": [round(center[0], 1), round(center[1], 1)],
            "ratio": ratio, "score": round(float(score), 3),
            "tpl": tpl_size, "chain": chain,
            "why": "" if ok else "模板确认没过"}


def button_box(geo, size=None):
    """把几何结果换成像素框 (x,y,w,h)；模板框优先，其次亮块。"""
    if not geo or not geo.get("ok"):
        return None
    tpl = geo.get("tpl")
    cx, cy = geo["center"]
    if tpl:
        tw, th = tpl
        box = (cx - tw / 2.0, cy - th / 2.0, tw, th)
    else:
        box = tuple(geo["button"])
    x, y, bw, bh = [int(round(v)) for v in box]
    if size:
        x = max(0, min(int(size[0]) - bw, x))
        y = max(0, min(int(size[1]) - bh, y))
    return (x, y, max(1, bw), max(1, bh))
