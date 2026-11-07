"""自动标定：开抢前自检 + 离线预设验证。

买家不再手拖 9 个框。程序在开抢前用当前**游戏客户区**尺寸把内置坐标
换算成像素，再在画面上找几个锚点（购买/确认按钮走模板匹配，价格/倒计时
走 OCR）确认坐标没跑偏：

  1. 全中且偏差 ≤ `TOL_PX`  → status=ok，静默继续；
  2. 有锚点但整体偏移       → 按偏移量微调一次再验，status=adjusted；
  3. 画面不是 16:9 / 客户区异常 → 按比例内缩居中后再验；
  4. 调完仍对不上、或关键锚点缺失 → status=fallback，直接回到**内置
                              坐标**（不带任何偏移），界面必须明确提示，
                              不能带着错坐标开抢；
  5. 一个锚点都没看到（不在交易行页面）→ status=skipped，**拦启动**：
                              没核对过按钮位置就用内置坐标点，1280×800 偏
                              29px、3440×1440 偏 307px；抓屏/读图异常单独
                              记 error，两者同权。唯一放行：必需区域已被
                              用户手框过。

锚点规则（2026-11-08 改：确认弹窗不再参与启动标定）：
  · 启动只要求 `buy` + 面板几何锚点；`confirm` 只在购买弹窗第一次出现时
    现场标定并缓存（弹窗不在画面里时拿它做启动校验必然 0.2x 误杀）；
  · `buy` 优先走几何推导（面板边缘 → 按钮色块 → 中心 → 模板确认），
    模板硬搜只作兜底；因此不再依赖「整体偏移量 ≤ 固定 24px」这类误杀；
  · 偏移上限改成按模板短边比例算（`TOL_RATIO × 模板短边`），并把偏移量
    写进日志/`calibration.message`；
  · 只看到 OCR 文字、没有任何像素锚点 → skipped，不得判 ok；
  · 模板中心必须用**缩放后**模板尺寸算：`vision.match()` 内部按目标比例
    重采样，继续用原图 th/tw 会产生 `tpl_w*(1-r)/2` 的系统性偏移，并被当成
    真实偏差写进坐标（1280×720 下 +53px、3840×2160 下 −152px）；
  · 尺度候选优先用信箱比例 `min(宽比, 高比)`，全部没命中时穷举
    1.00–2.00（21:9 的真实比例 1.3333 与几何均值 1.5456 差很远）。

离线可跑：`calibrate_image()` 只依赖传入的 BGR 图和尺寸，不碰屏幕、不碰
真实鼠标；`tests/test_autocalib.py` 用它做 12 组预设与 5 张真实截图验证。
"""
import re
import time

import cv2

from . import geometry
from . import ocr as _ocr
from . import region_backup as _backup
from . import region_table, regions, vision

# 锚点偏差容忍度（物理像素）。小于这个数就不折腾配置了。
TOL_PX = 6
# 偏移可信上限 = 模板短边 × 该比例（实测「购买」模板 298×58，短边 58，
# 上限 87px；本机真实偏移 +66/+31 因此放行，而不是被 24px 写死值误杀）。
TOL_RATIO = 1.5
# 关键锚点：模板匹配的像素锚点。
PIXEL_ANCHORS = ("buy", "confirm")
# 每条路线**必需**的锚点：启动只校验「购买按钮」。
# `confirm` 不参与启动标定——确认弹窗只在点过购买之后才出现，启动阶段
# 一定读不到（实测最高 0.223），拿它当必需等于每轮必 fallback。
# 第一次弹窗出现时由 `calibrate_confirm()` 现场标定并缓存。
REQUIRED_BY_WORKFLOW = {
    # 倒计时路线靠时钟 OCR 触发，没有 buy 模板也不拦（P0-D）。
    "countdown": (),
    "list": ("buy",),
    "detail": ("buy",),
}
# 模板匹配得分下限；低于它视为「画面里没有这个按钮」。
ABSENT_SCORE = 0.55
# 没有模板尺寸可参考时的退路（按 1080p 基准，随客户区宽度等比放大）。
MAX_ADJUST_REF = 24.0
# 确认弹窗的搜索区（占客户区比例 x/y/w/h）：弹窗总出现在中部偏下，
# 不需要全屏找；全屏找会把列表里相似的价格条当成按钮（实测误命中 0.719）。
CONFIRM_SEARCH = (0.12, 0.26, 0.76, 0.62)
# 确认按钮的形状约束（长宽比 / 占屏宽比 / 占屏高比）。
CONFIRM_AR = (2.5, 9.0)
CONFIRM_W = (0.03, 0.40)
CONFIRM_H = (0.010, 0.200)
# 自动/几何都失败时的兜底提示：手框一次，存的是 ROI 矩形，不是像素模板。
MANUAL_HINT = ("；兜底：到「高级设置 → 区域微调」手框一次（只存 ROI 矩形，"
               "下次直接读）")


def _max_adjust(size, anchors=None):
    """偏移可信上限：按模板短边比例算，模板未知才退回旧口径。

    取所有锚点里**最短**的模板短边：确认按钮的模板比购买按钮大，
    用最大值会把购买按钮的容忍度一起放宽（58→87 变成 70→105），
    小锚点被大模板的错误平移带着跑。
    """
    shorts = [min(a["tpl"]) for a in (anchors or [])
              if a.get("tpl") and len(a["tpl"]) == 2]
    if shorts:
        return max(float(TOL_PX + 2), TOL_RATIO * float(min(shorts)))
    return MAX_ADJUST_REF * float(size[0]) / 1920.0

_LOGICAL = (("1080p", 1920, 1080), ("1440p", 2560, 1440), ("2160p", 3840, 2160))
_SCALES = (100, 125, 150, 200)


def presets():
    """3 档逻辑分辨率 × 4 档系统缩放 = 12 组物理客户区尺寸。

    物理像素 = 逻辑分辨率 × 缩放。窗口化时客户区可能小于它，但换算公式
    与尺寸无关；这 12 组是验收用的确定值。
    """
    out = []
    for label, w, h in _LOGICAL:
        for pct in _SCALES:
            out.append({
                "name": f"{label}@{pct}%",
                "logical": [w, h],
                "scale": pct,
                "width": int(round(w * pct / 100.0)),
                "height": int(round(h * pct / 100.0)),
            })
    return tuple(out)


def boxes_for(size, norm=None):
    """按客户区尺寸算出 9 个区域的像素框 `{name: (x,y,w,h)}`。"""
    table = norm or region_table.BUILTIN
    return {name: regions.px_box(table[name], size) for name in region_table.ORDER}


def _aspect_fix(boxes, size):
    """非 16:9 客户区：把 1080p 基准等比缩放后居中（游戏黑边/信箱模式）。

    返回 `(boxes, meta)`；已经是 16:9 时原样返回、meta["changed"]=False。
    """
    w, h = float(size[0]), float(size[1])
    if w <= 0 or h <= 0:
        return boxes, {"changed": False, "reason": "尺寸无效"}
    base_ratio = region_table.BASE_W / region_table.BASE_H
    ratio = w / h
    if abs(ratio - base_ratio) / base_ratio <= 0.01:
        return boxes, {"changed": False}
    s = min(w / region_table.BASE_W, h / region_table.BASE_H)
    dx = (w - region_table.BASE_W * s) / 2.0
    dy = (h - region_table.BASE_H * s) / 2.0
    # boxes 已经按 width/height 拉伸算过像素；这里换成「等比 + 居中」：
    # 先除以拉伸系数，再乘等比系数 s，最后加黑边偏移。
    kx, ky = s * region_table.BASE_W / w, s * region_table.BASE_H / h
    fixed = {}
    for name, (x, y, bw, bh) in boxes.items():
        fixed[name] = (int(round(x * kx + dx)), int(round(y * ky + dy)),
                       max(1, int(round(bw * kx))), max(1, int(round(bh * ky))))
    return fixed, {"changed": True, "scale": round(s, 4),
                   "dx": round(dx, 2), "dy": round(dy, 2),
                   "reason": f"客户区 {int(w)}×{int(h)} 不是 16:9，已按比例居中"}


def _crop(gray, rect):
    x, y, w, h = [int(v) for v in rect]
    hgt, wid = gray.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(wid, x + w), min(hgt, y + h)
    if x1 <= x0 or y1 <= y0:
        return None, (x0, y0)
    return gray[y0:y1, x0:x1], (x0, y0)


def _search_rect(rect, size, pad=0.35, floor=40):
    """锚点搜索窗：在区域外扩一圈（至少 floor 像素）。

    模板只在区域里时得分恒为 1，外扩才能看出「偏了多少」。pad 太小会
    让 ±20px 的偏移直接跑出窗口，自检就永远只能报"没看到"。
    """
    x, y, w, h = rect
    px, py = max(int(w * pad), floor), max(int(h * pad), floor)
    x0 = max(0, x - px)
    y0 = max(0, y - py)
    x1 = min(size[0], x + w + px)
    y1 = min(size[1], y + h + py)
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def _tpl_gray(cfg, name):
    if cfg is None or not hasattr(cfg, "template_path"):
        return None
    path = cfg.template_path(name)
    try:
        img = vision.imread(path, cv2.IMREAD_GRAYSCALE)
    except Exception:
        return None
    return img if img is not None else None


def _match_template(gray, rect, tpl, size):
    """在 rect 外扩窗口里找模板，返回 `(score, center_xy, found_rect)`。

    中心与命中框都用**缩放后**的模板尺寸：`vision.match()` 内部按当前客户区
    比例重采样模板，`loc` 对应缩放后的左上角；继续拿原模板 th/tw 去加半个
    模板宽，会多出 `tpl_w*(1-r)/2` 的系统性偏差（1280×720 约 +53px）。
    """
    search = _search_rect(rect, size)
    crop, (ox, oy) = _crop(gray, search)
    if crop is None or tpl is None:
        return 0.0, None, None
    try:
        # deep=True：元数据推算的比例全都没命中时穷举一遍。
        # 21:9（3440×1440）几何均值给 1.5456、真实信箱比例 1.3333，
        # 不穷举就只能判「没找到」。
        score, loc, scale = vision.match_scaled(crop, tpl, deep=True)
        used = vision.scaled(tpl, scale)
    except Exception:
        return 0.0, None, None
    if not loc or used is None:
        return float(score or 0.0), None, None
    th, tw = used.shape[:2]
    fx, fy = ox + int(loc[0]), oy + int(loc[1])
    return float(score or 0.0), (fx + tw / 2.0, fy + th / 2.0), (fx, fy, tw, th)


def _ocr_text(gray, rect):
    if not _ocr.available():
        return ""
    crop, _ = _crop(gray, rect)
    if crop is None:
        return ""
    try:
        return _ocr.read_text(crop) or ""
    except Exception:
        return ""


def _anchor(name, label, expected, found_center, score=0.0, status="absent",
            text="", method="", tpl_size=None, found_rect=None, geo=None):
    ex = expected[0] + expected[2] / 2.0 if expected else None
    ey = expected[1] + expected[3] / 2.0 if expected else None
    off = None
    if found_center is not None and ex is not None:
        off = [round(found_center[0] - ex, 2), round(found_center[1] - ey, 2)]
    a = {
        "name": name, "label": label,
        "expected": ([int(v) for v in expected] if expected else None),
        "found_center": ([round(found_center[0], 2), round(found_center[1], 2)]
                         if found_center is not None else None),
        "offset": off,
        "score": round(float(score or 0.0), 3),
        "status": status,
        "text": text,
        "method": method,
        "tpl": ([int(tpl_size[0]), int(tpl_size[1])] if tpl_size else None),
        "found_rect": ([int(v) for v in found_rect] if found_rect else None),
    }
    if geo:
        a["geo"] = geo
    return a


BUY_TEXT = "购买"


def _buy_text_hit(img, size):
    """整帧找「购买」二字（第一优先锚点）。返回 (bbox, hits)。"""
    if img is None:
        return None, []
    try:
        ws = _ocr.read_words(img) or []
    except Exception:                                # noqa: BLE001
        return None, []
    hits = [w for w in ws if BUY_TEXT in (w.get("text") or "")]
    if not hits:
        return None, []
    exact = [w for w in hits if (w.get("text") or "").strip() == BUY_TEXT]
    pool = exact or hits
    low = [w for w in pool if w["y"] > 0.45 * float(size[1])] or pool
    w = max(low, key=lambda r: (r["x"] + r["y"]))     # 按钮在右下
    return (int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"])), hits


def _has_buy_text(img, rect, pad=0.2):
    """矩形（外扩 pad）里有没有「购买」二字。True/False；认不出字返回 None。"""
    if img is None or not rect:
        return None
    x, y, w, h = rect
    px, py = int(w * pad), int(h * pad)
    crop = img[max(0, y - py):y + h + py, max(0, x - px):x + w + px]
    if crop.size == 0:
        return None
    try:
        ws = _ocr.read_words(crop) or []
    except Exception:                                # noqa: BLE001
        return None
    if not ws:
        return None                                  # OCR 没读出字，不判
    return any(BUY_TEXT in (t.get("text") or "") for t in ws)


def _text_button_rect(tb, size):
    """文字 bbox → 按钮矩形（中心取文字中心，纵向略向下补 0.35 行高）。"""
    tx, ty, tw, th = tb
    cx = tx + tw / 2.0
    cy = ty + th / 2.0 + th * 0.35
    bw = max(200.0, tw * 3.0)
    bh = max(52.0, th * 2.0)
    x = min(max(0.0, cx - bw / 2.0), max(0.0, size[0] - bw))
    y = min(max(0.0, cy - bh / 2.0), max(0.0, size[1] - bh))
    return (int(round(x)), int(round(y)), int(round(bw)), int(round(bh))), (cx, cy)


# ---------------------------------------------------------------------------
# 倒计时锚点：按钮上没有「购买」二字，靠「剩余：X小时Y分」这一行定按钮
#
# 实测量（1920×1080，挂单页右下面板）：
#   倒计时行 bbox=(1554,874,156,22)  中心 x=1632  底部 y=896
#   按钮矩形 x 1481..1785  y 902..961 → 304×59，中心 (1633, 931)
#   ⇒ offset_x=0（按钮与倒计时行同中心）、offset_y=+6（紧贴行底）、304×59
# 关系存 `calibration.buy_anchor`，坐标跟着倒计时行跑，不再存绝对坐标。
# ---------------------------------------------------------------------------
COUNTDOWN_KEYS = ("剩余", "倒计时", "公示期剩余")
_COUNTDOWN_TIME = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}(\s*[:：]\s*\d{2})?")
_COUNTDOWN_SKIP = ("开放时间", "次日")

BUY_ANCHOR_DEFAULT = {
    "offset_x": 0.0,      # 按钮中心相对倒计时行中心的横向偏移
    "offset_y": 6.0,      # 按钮上边相对倒计时行底部的纵向偏移
    "w": 304.0,           # 按钮宽（1920 基准）
    "h": 59.0,            # 按钮高（1920 基准）
    "ref": [1920, 1080],
}


def _countdown_hit(img, size):
    """整帧找「剩余 / 倒计时」那一行（或 HH:MM:SS）。返回 (bbox, text, hits)。"""
    if img is None:
        return None, "", []
    try:
        ws = _ocr.read_words(img) or []
    except Exception:                            # noqa: BLE001
        return None, "", []
    hits = []
    for w in ws:
        t = (w.get("text") or "").strip()
        if not t or any(k in t for k in _COUNTDOWN_SKIP):
            continue
        if any(k in t for k in COUNTDOWN_KEYS) or _COUNTDOWN_TIME.search(t):
            hits.append(w)
    if not hits:
        return None, "", []
    # 按钮在右下面板：优先画面右下角的倒计时行
    low = [w for w in hits if w["x"] > 0.5 * size[0] and w["y"] > 0.35 * size[1]]
    w = max(low or hits, key=lambda r: (r["y"], r["x"]))
    return (int(w["x"]), int(w["y"]), int(w["w"]), int(w["h"])), w["text"], hits


def buy_anchor_rel(cfg):
    """读 `calibration.buy_anchor`，缺项用实测量补。"""
    rel = dict(BUY_ANCHOR_DEFAULT)
    if cfg is not None and hasattr(cfg, "get"):
        try:
            cal = cfg.get("calibration") or {}
            saved = cal.get("buy_anchor") if isinstance(cal, dict) else None
            if isinstance(saved, dict):
                for k in ("offset_x", "offset_y", "w", "h"):
                    if isinstance(saved.get(k), (int, float)):
                        rel[k] = float(saved[k])
                if isinstance(saved.get("ref"), (list, tuple)) and len(saved["ref"]) == 2:
                    rel["ref"] = [float(saved["ref"][0]), float(saved["ref"][1])]
        except Exception:                        # noqa: BLE001
            pass
    return rel


def _buy_anchor_rect(rel, cd, size):
    """倒计时行 bbox → 按钮矩形（按画面尺寸缩放基准量）。"""
    ref_w, ref_h = float(rel["ref"][0]), float(rel["ref"][1])
    sx, sy = float(size[0]) / ref_w, float(size[1]) / ref_h
    cd_cx = cd[0] + cd[2] / 2.0
    cd_bottom = cd[1] + cd[3]
    bw = float(rel["w"]) * sx
    bh = float(rel["h"]) * sy
    cx = cd_cx + float(rel["offset_x"]) * sx
    cy = cd_bottom + float(rel["offset_y"]) * sy + bh / 2.0
    x = min(max(0.0, cx - bw / 2.0), max(0.0, size[0] - bw))
    y = min(max(0.0, cy - bh / 2.0), max(0.0, size[1] - bh))
    return ((int(round(x)), int(round(y)), int(round(bw)), int(round(bh))),
            (cx, cy), (bw, bh))


def check_buy_region(img, rect, pad=6):
    """按钮区校验：非空 + 与上方背景有明显对比；顺手记下框内 OCR 文本。

    按钮里可能只有价格/图标（没有「购买」二字），所以判定标准是
    「这块和周围背景不一样」，不是「有字」。
    """
    if img is None or not rect:
        return {"ok": False, "why": "没有画面或矩形", "text": "",
                "mean": 0.0, "std": 0.0, "delta": 0.0}
    x, y, w, h = [int(v) for v in rect]
    H, W = img.shape[:2]
    crop = img[max(0, y):min(H, y + h), max(0, x):min(W, x + w)]
    if crop.size == 0:
        return {"ok": False, "why": "矩形不在画面内", "text": "",
                "mean": 0.0, "std": 0.0, "delta": 0.0}
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    bg = img[max(0, y - 70):max(1, y - 12), max(0, x):min(W, x + w)]
    bg_g = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY) if bg.ndim == 3 else bg
    mean, std = float(gray.mean()), float(gray.std())
    delta = abs(mean - float(bg_g.mean())) if bg_g.size else 0.0
    text = ""
    try:
        ws = _ocr.read_words(crop) or []
        text = " ".join((t.get("text") or "").strip() for t in ws).strip()
    except Exception:                            # noqa: BLE001
        pass
    ok = bool(delta >= 6.0 or std >= 8.0)
    why = ("底色差 %.1f / 纹理 std %.1f（通过）" % (delta, std) if ok
           else "与背景几乎同色（差 %.1f，std %.1f），不像按钮" % (delta, std))
    return {"ok": ok, "why": why, "text": text,
            "mean": round(mean, 1), "std": round(std, 1),
            "delta": round(delta, 1)}


def find_buy_by_countdown(img, size, cfg=None):
    """倒计时行 → 按钮矩形（第一优先锚点）。返回 dict 或 None。"""
    cd, text, hits = _countdown_hit(img, size)
    if not cd:
        return None
    rel = buy_anchor_rel(cfg)
    rect, center, bwbh = _buy_anchor_rect(rel, cd, size)
    chk = check_buy_region(img, rect)
    return {"ok": bool(chk["ok"]), "cd": cd, "text": text, "rect": rect,
            "center": center, "size": bwbh, "check": chk, "rel": rel,
            "hits": [(w["text"], w["x"], w["y"]) for w in hits]}


def probe(gray, boxes, cfg=None, ocr_mod=None, img=None):
    """在当前画面上找锚点。

    buy：先用几何（面板边缘 + 按钮色块 → 中心）推，模板只做确认；
         几何没推出来才退回模板硬搜（记录 `method` 便于日志归因）。
    confirm：模板匹配；启动阶段读不到属于正常（弹窗未出现），标记为
             "deferred"，不参与闸门。
    clock / price：OCR 关键字/数字（OCR 不可用时视为"没有"）。
    返回 `(anchors, visible)`；visible=False 表示一个锚点都没看到。
    """
    size = (gray.shape[1], gray.shape[0])
    anchors = []
    visible = False

    # --- buy：倒计时锚点 → 「购买」文字 → 手框缓存 → 几何 → 模板 ---
    tpl = _tpl_gray(cfg, "buy")
    b = boxes["buy"]
    exp_center = (b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)
    view = img if img is not None else gray
    cd_hit = None
    if img is not None:
        try:
            cd_hit = find_buy_by_countdown(img, size, cfg)
        except Exception:                        # noqa: BLE001
            cd_hit = None
    tb, text_hits = _buy_text_hit(view, size)
    text_rect, text_center = (None, None)
    if tb:
        text_rect, text_center = _text_button_rect(tb, size)

    manual_box = None
    manual_reject = ""
    if cfg is not None and hasattr(cfg, "region_raw"):
        try:
            raw = cfg.region_raw("buy")
            applied = ((cfg.get("calibration") or {}).get("applied") or {})
            if raw and regions.is_manual(cfg, "buy", applied.get("buy")):
                cand = regions.px_box(raw, size)
                # 手框必须真的框住「购买」二字，否则就是价格框（这次踩过的坑）。
                if _has_buy_text(view, cand) is True:
                    manual_box = cand
                else:
                    manual_reject = "手框缓存作废：框内 OCR 不到「购买」二字"
        except Exception:                        # noqa: BLE001
            manual_box = None

    geo = {"ok": False, "chain": ["未参与"], "score": 0.0}
    if not text_rect and not manual_box:
        try:
            geo = geometry.locate(gray, tpl=tpl, expected_center=exp_center)
        except Exception as e:                       # noqa: BLE001
            geo = {"ok": False, "why": f"几何推导异常：{type(e).__name__}: {e}",
                   "chain": [], "score": 0.0}

    if cd_hit and cd_hit.get("ok"):
        # 第一优先：按钮上没有「购买」二字（只有价格/图标），靠倒计时行定位。
        visible = True
        chk = cd_hit.get("check") or {}
        a = _anchor(
            "buy", "购买按钮", b, cd_hit["center"], 1.0, "found",
            text="倒计时锚点「%s」bbox=%s → 按钮 %s（%s；框内 OCR：%s）"
                 % (cd_hit["text"], tuple(cd_hit["cd"]), tuple(cd_hit["rect"]),
                    chk.get("why", ""), chk.get("text") or "无"),
            method="countdown", tpl_size=(cd_hit["rect"][2], cd_hit["rect"][3]),
            found_rect=cd_hit["rect"])
        a["cd"] = [int(v) for v in cd_hit["cd"]]
        a["cd_text"] = cd_hit["text"]
        a["check"] = chk
        a["anchor_rel"] = cd_hit.get("rel")
        anchors.append(a)
        geo = {"ok": False, "chain": ["倒计时锚点：%s" % cd_hit["text"]],
               "score": 0.0}
    elif text_rect:
        # 第一优先：文字在哪，按钮中心就在哪；不靠模板分数证明自己。
        visible = True
        anchors.append(_anchor(
            "buy", "购买按钮", b, text_center, 1.0, "found",
            text="OCR「购买」bbox=%s" % (tb,), method="text",
            tpl_size=(text_rect[2], text_rect[3]), found_rect=text_rect))
        geo = {"ok": False, "chain": ["文字锚点：OCR「购买」%s" % (tb,)], "score": 0.0}
    elif manual_box and _has_buy_text(view, manual_box) is not False:
        visible = True
        mc = (manual_box[0] + manual_box[2] / 2.0,
              manual_box[1] + manual_box[3] / 2.0)
        anchors.append(_anchor(
            "buy", "购买按钮", b, mc, 1.0, "found", method="manual",
            text="手框缓存（框内有「购买」文字）",
            tpl_size=(manual_box[2], manual_box[3]), found_rect=manual_box))
        geo = {"ok": False, "chain": ["手框缓存：框内已 OCR 到「购买」"], "score": 0.0}
    elif geo.get("ok"):
        gbox = geometry.button_box(geo, size)
        # 一致性检查只在「整帧本来就有『购买』文字」时生效：按钮上只印价格
        # 的页面（挂单页右下面板）整帧没有这两个字，不能据此否决几何/模板。
        if text_hits and _has_buy_text(view, gbox) is False:
            anchors.append(_anchor(
                "buy", "购买按钮", b, None, float(geo.get("score") or 0.0),
                "absent", text="几何框内没有「购买」文字（疑似价格框）；" + manual_reject,
                method="geometry-rejected", geo=geo))
        else:
            visible = True
            anchors.append(_anchor(
                "buy", "购买按钮", b, geo["center"], geo.get("score", 0.0),
                "found", method="geometry",
                tpl_size=geo.get("tpl"), found_rect=gbox, geo=geo))
    else:
        score, center, rect = _match_template(gray, b, tpl, size)
        if tpl is not None and score >= ABSENT_SCORE and center is not None \
                and not (text_hits and _has_buy_text(view, rect) is False):
            visible = True
            anchors.append(_anchor(
                "buy", "购买按钮", b, center, score, "found",
                method="template", tpl_size=(rect[2], rect[3]), found_rect=rect))
        else:
            why = geo.get("why") or ""
            if tpl is not None and score >= ABSENT_SCORE:
                why = "模板命中 %.3f 但框内无「购买」文字（疑似价格框）" % score
            anchors.append(_anchor(
                "buy", "购买按钮", b, None,
                max(float(score or 0.0), float(geo.get("score") or 0.0)),
                "absent", text="；".join(x for x in (why, manual_reject) if x),
                method="none", geo=geo))

    # 购买文字的独立记录：收市判定要用（整帧有没有这两个字）
    anchors.append({
        "name": "buy_text", "label": "购买文字", "expected": None,
        "found_center": None, "offset": None,
        "score": 1.0 if tb else 0.0,
        "status": "found" if tb else "absent",
        "text": ("OCR bbox=%s（%d 处）" % (tb, len(text_hits))) if tb
                else "整帧 0 处「购买」文字（收市期间按钮不出现）",
        "method": "ocr", "tpl": None, "found_rect": None, "bbox": tb,
    })

    # 面板锚点：只做旁证，写进日志（不拦启动）
    if geo.get("panel"):
        anchors.append({
            "name": "panel", "label": "详情面板", "expected": None,
            "found_center": None, "offset": None,
            "score": 1.0, "status": "found", "text": "",
            "method": "geometry", "tpl": None, "found_rect": None,
            "panel": geo.get("panel"), "ratio": geo.get("ratio"),
        })
    else:
        anchors.append({
            "name": "panel", "label": "详情面板", "expected": None,
            "found_center": None, "offset": None, "score": 0.0,
            "status": "absent", "text": "", "method": "geometry",
            "tpl": None, "found_rect": None,
        })

    # --- confirm：启动阶段只记录，不要求 ---
    ctpl = _tpl_gray(cfg, "confirm")
    cscore, ccenter, crect = _match_template(gray, boxes["confirm"], ctpl, size)
    if ctpl is not None and cscore >= ABSENT_SCORE and ccenter is not None:
        visible = True
        anchors.append(_anchor("confirm", "确认按钮", boxes["confirm"],
                               ccenter, cscore, "found", method="template",
                               tpl_size=(crect[2], crect[3]), found_rect=crect))
    else:
        anchors.append(_anchor("confirm", "确认按钮", boxes["confirm"], None,
                               cscore, "absent", method="deferred",
                               text="启动不校验；首次弹窗出现时现场标定"))

    # clock：只看框里有没有「分/秒/剩余/解锁」这类字，不要求 OCR 一定装好
    clock_text = _ocr_text(gray, boxes["clock"])
    if clock_text and any(k in clock_text for k in ("分", "秒", "剩余", "解锁", "公示")):
        visible = True
        anchors.append(_anchor("clock", "公示期倒计时", boxes["clock"], None,
                               1.0, "text", clock_text))
    else:
        anchors.append(_anchor("clock", "公示期倒计时", boxes["clock"], None,
                               0.0, "absent", clock_text))

    price_text = _ocr_text(gray, boxes["price"])
    digits = "".join(ch for ch in price_text if ch.isdigit())
    if len(digits) >= 2:
        visible = True
        anchors.append(_anchor("price", "价格文字", boxes["price"], None,
                               1.0, "text", price_text))
    else:
        anchors.append(_anchor("price", "价格文字", boxes["price"], None,
                               0.0, "absent", price_text))

    return anchors, visible


def _median_offset(anchors):
    """模板锚点的整体偏移中位数；只看真的找到了的。"""
    offs = [a["offset"] for a in anchors
            if a["status"] == "found" and a["offset"]]
    if not offs:
        return None
    xs = sorted(o[0] for o in offs)
    ys = sorted(o[1] for o in offs)
    return xs[len(xs) // 2], ys[len(ys) // 2]


def _shift(boxes, dx, dy, size):
    out = {}
    for name, (x, y, w, h) in boxes.items():
        out[name] = (max(0, min(int(size[0]) - w, int(round(x + dx)))),
                     max(0, min(int(size[1]) - h, int(round(y + dy)))),
                     w, h)
    return out


def _norm(boxes, size):
    w, h = float(size[0]), float(size[1])
    return {name: {"x": round(x / w, 6), "y": round(y / h, 6),
                   "w": round(bw / w, 6), "h": round(bh / h, 6)}
            for name, (x, y, bw, bh) in boxes.items()}


def _required(workflow):
    """当前路线必需的像素锚点。未知路线按最严的来。"""
    return REQUIRED_BY_WORKFLOW.get(workflow or "", PIXEL_ANCHORS)


def _missing_critical(anchors, required=PIXEL_ANCHORS):
    """没找到的**必需**锚点。"""
    return [a for a in anchors
            if a["name"] in required and a["status"] != "found"]


def _missing_optional(anchors, required=PIXEL_ANCHORS):
    """没找到但本路线不要求的像素锚点（降级用，不拦启动）。

    `method="deferred"` 的（确认按钮：等弹窗出现才标）不算「缺失」——
    启动阶段它本来就不该在画面里，写进提示只会让人以为标定漏了东西。
    """
    return [a for a in anchors
            if a["name"] in PIXEL_ANCHORS and a["name"] not in required
            and a["status"] != "found" and a.get("method") != "deferred"]


def _found_critical(anchors):
    return [a for a in anchors
            if a["name"] in PIXEL_ANCHORS and a["status"] == "found"]


def _anchor_detail(a):
    """给用户看的一句话：哪个锚点、差多少、往哪边。"""
    if a["status"] == "found" and a.get("offset"):
        dx, dy = a["offset"]
        return f"{a['label']} {dx:+.1f}/{dy:+.1f} px"
    if a["status"] == "text":
        return f"{a['label']} 仅文字"
    return f"{a['label']} 没找到（最高分 {a['score']:.3f}）"


def _result(status, ok, size, boxes, anchors, attempts, meta, message,
            offset=None):
    return {
        "ok": bool(ok), "status": status,
        "client": [int(size[0]), int(size[1])],
        "attempts": int(attempts),
        "offset": ([round(offset[0], 2), round(offset[1], 2)]
                   if offset else None),
        "message": message,
        "anchors": anchors,
        "boxes": boxes,
        "norm": _norm(boxes, size),
        "aspect": meta,
        # 路径：geometry / template / builtin（回落内置）/ none（没看到锚点）
        "path": ("none" if status in ("skipped",) else
                 "builtin" if status in ("fallback", "error") else "template"),
        "residual": [a["name"] for a in anchors if a["status"] == "found"
                     and a.get("offset")
                     and (abs(a["offset"][0]) > TOL_PX
                          or abs(a["offset"][1]) > TOL_PX)],
    }


def calibrate_image(image, size=None, cfg=None, ocr_mod=None, workflow=None):
    """纯函数：给定一帧画面，算出自检结果。不读写配置、不碰屏幕。

    `image` 可以是 BGR ndarray 或图片路径；`size` 缺省取画面尺寸。
    `workflow` 决定必需锚点集（countdown 不要求 buy）。
    fallback 一律回落到内置坐标（含非 16:9 的等比居中），不保留任何
    偏移或内缩——偏移本身不可信时，唯一可信的就是内置表。
    """
    if isinstance(image, str):
        image = cv2.imread(image)
    if image is None:
        return {"ok": False, "status": "error", "message": "读不到画面",
                "anchors": [], "attempts": 0, "client": list(size or (0, 0)),
                "boxes": {}, "norm": {}, "residual": []}
    size = size or (image.shape[1], image.shape[0])
    gray = vision.to_gray(image)
    vision.set_target_screen(size, 96)

    base, meta = _aspect_fix(boxes_for(size), size)
    attempts = 1 if meta.get("changed") else 0
    required = _required(workflow)

    anchors, visible = probe(gray, base, cfg, ocr_mod, image)
    missing = _missing_critical(anchors, required)
    found = _found_critical(anchors)

    if not visible:
        return _result(
            "skipped", False, size, base, anchors, attempts, meta,
            "画面里没找到可校验锚点（可能不在交易行页面），按钮位置未经"
            "校对；本次不启动，请先打开交易行，或到「高级设置 → 区域微调」"
            "手动框一次")

    # 只有 OCR 文字、没有任何像素锚点：无法核对「点哪里」→ skipped（拦）。
    if missing and not found:
        return _result(
            "skipped", False, size, base, anchors, attempts, meta,
            "只看到文字锚点，%s；按钮位置未经校对，本次不启动"
            % "、".join(_anchor_detail(a) for a in missing))

    # 任一**必需**锚点缺失：直接回落内置，闸门会拦下本次启动。
    if missing:
        return _result(
            "fallback", False, size, base, anchors, attempts, meta,
            "缺少本路线必需的锚点：%s；已回到内置坐标，不采用任何偏移%s"
            % ("、".join(_anchor_detail(a) for a in missing), MANUAL_HINT))

    buy = next((a for a in anchors if a["name"] == "buy"), None)
    cd_mode = bool(buy and buy.get("method") == "countdown" and buy.get("found_rect"))
    text_mode = bool(buy and buy.get("method") == "text" and buy.get("found_rect"))
    manual_mode = bool(buy and buy.get("method") == "manual"
                       and buy.get("found_rect"))
    geo_mode = bool(buy and buy.get("method") == "geometry"
                    and buy.get("found_rect"))
    cap = _max_adjust(size, anchors)
    off = _median_offset(anchors)
    boxes = base
    adjusted = bool(meta.get("changed"))

    if cd_mode:
        # 倒计时行锚点：按钮矩形按「行中心 + 行底 + 固定尺寸」推，不搜索。
        boxes = dict(base)
        boxes["buy"] = tuple(buy["found_rect"])
        adjusted = True
        residual = []
    elif text_mode:
        boxes = dict(base)
        boxes["buy"] = tuple(buy["found_rect"])
        adjusted = True
        residual = []
    elif manual_mode:
        # 手框缓存：用户框过的矩形就是唯一真值，不判偏移、不搜索。
        boxes = dict(base)
        boxes["buy"] = tuple(buy["found_rect"])
        adjusted = True
        residual = []
    elif geo_mode:
        # 几何推导命中的框已经过模板确认：直接把「购买」换成推导框，
        # 其余区域保持内置。不做整体平移——按钮平移不代表整块 UI 平移，
        # 平移错了还会把列表扫描框带偏（实测 +66/31 会把右缘推到 1466）。
        boxes = dict(base)
        boxes["buy"] = tuple(buy["found_rect"])
        boff = buy.get("offset") or (0.0, 0.0)
        adjusted = adjusted or abs(boff[0]) > TOL_PX or abs(boff[1]) > TOL_PX
        if abs(boff[0]) > cap or abs(boff[1]) > cap:
            return _result(
                "fallback", False, size, base, anchors, attempts, meta,
                "几何推导的购买按钮偏移 %+.1f/%+.1f px 超出可信范围"
                "（上限 %.1f px ＝ 模板短边×%.1f）；已回到内置坐标%s"
                % (boff[0], boff[1], cap, TOL_RATIO, MANUAL_HINT))
        residual = []
    else:
        if off and (abs(off[0]) > TOL_PX or abs(off[1]) > TOL_PX):
            if abs(off[0]) > cap or abs(off[1]) > cap:
                return _result(
                    "fallback", False, size, base, anchors, attempts, meta,
                    "整体偏移 %+.1f/%+.1f px 超出可信范围（上限 %.1f px ＝ 模板"
                    "短边×%.1f）；已回到内置坐标，不采用偏移%s"
                    % (off[0], off[1], cap, TOL_RATIO, MANUAL_HINT))
            moved = _shift(base, off[0], off[1], size)
            anchors, visible = probe(gray, moved, cfg, ocr_mod)
            attempts += 1
            adjusted = True
            missing = _missing_critical(anchors, required)
            if not visible or missing:
                why = ("、".join(_anchor_detail(a) for a in missing)
                       if missing else "移动后锚点全部消失")
                return _result(
                    "fallback", False, size, base, anchors, attempts, meta,
                    f"按偏移 {off[0]:+.1f}/{off[1]:+.1f} px 微调后对不上（{why}）；"
                    f"已回到内置坐标，不采用偏移")
            off2 = _median_offset(anchors)
            if off2 and (abs(off2[0]) > TOL_PX or abs(off2[1]) > TOL_PX):
                return _result(
                    "fallback", False, size, base, anchors, attempts, meta,
                    "微调后仍偏移 %+.1f/%+.1f px（%s）；已回到内置坐标，不采用偏移"
                    % (off2[0], off2[1],
                       "、".join(_anchor_detail(a) for a in anchors
                                if a["status"] == "found" and a.get("offset"))))
            boxes = moved

        residual = [a for a in anchors
                    if a["status"] == "found" and a.get("offset")
                    and (abs(a["offset"][0]) > TOL_PX
                         or abs(a["offset"][1]) > TOL_PX)]
        if residual:
            # 各锚点方向不一致（中位数为 0 但个别仍偏），微调无解 → 内置坐标
            return _result(
                "fallback", False, size, base, anchors, attempts, meta,
                "锚点互相矛盾：%s；已回到内置坐标，不采用偏移"
                % "、".join(_anchor_detail(a) for a in residual))

    status = "adjusted" if adjusted else "ok"
    optional = _missing_optional(anchors, required)
    if optional and status == "ok":
        # 非必需锚点缺失：不拦，但绝不能报「与内置坐标一致」。
        status = "adjusted"
    if cd_mode:
        bc = buy.get("found_center") or [0, 0]
        chk = buy.get("check") or {}
        msg = ("自动标定通过：标定来源=倒计时锚点｜「%s」bbox %s → 按钮 %s"
               "｜中心 (%.0f, %.0f)｜%s｜框内 OCR：%s（坐标跟着倒计时行，不搜索）"
               % (buy.get("cd_text") or buy.get("text", ""), tuple(buy.get("cd") or ()),
                  tuple(buy["found_rect"]), bc[0], bc[1],
                  chk.get("why", ""), chk.get("text") or "无"))
    elif text_mode:
        bc = buy.get("found_center") or [0, 0]
        msg = ("自动标定通过：标定来源=OCR「购买」｜%s｜按钮中心 (%.0f, %.0f)"
               "｜落点框 %s（文字锚点优先，不比模板）"
               % (buy.get("text", ""), bc[0], bc[1], tuple(buy["found_rect"])))
    elif manual_mode:
        bc = buy.get("found_center") or [0, 0]
        msg = ("自动标定通过：标定来源=手框缓存｜buy %.3f 中心 (%.0f, %.0f)｜"
               "落点框 %s（不搜索、不比模板）"
               % (buy["score"], bc[0], bc[1], tuple(buy["found_rect"])))
    elif geo_mode:
        boff = buy.get("offset") or (0.0, 0.0)
        g = buy.get("geo") or {}
        msg = ("自动标定通过：路径=几何推导｜buy %.3f 偏移 %+.1f/%+.1f px"
               "（上限 %.1f）｜几何中心 (%.0f, %.0f)｜落点框 %s"
               "｜面板 x=%s"
               % (buy["score"], boff[0], boff[1], cap,
                  buy["found_center"][0], buy["found_center"][1],
                  tuple(buy["found_rect"]),
                  (g.get("panel") or ["?"])[0]))
    elif status == "ok":
        msg = "自动标定通过：购买按钮落在内置坐标上（confirm 启动不校验）"
    elif off and (abs(off[0]) > TOL_PX or abs(off[1]) > TOL_PX):
        msg = (f"自动标定已微调：整体偏移 {off[0]:+.1f}/{off[1]:+.1f} px"
               f"（上限 {cap:.1f}）")
    elif meta.get("changed"):
        msg = f"自动标定已按比例居中：{meta.get('reason')}"
    else:
        msg = "自动标定已校验：锚点与内置坐标一致（本路线不要求全部锚点）"
    if optional:
        msg += ("；本路线不要求 %s，只用 %s 校验"
                % ("、".join(a["label"] for a in optional),
                   "、".join(a["label"] for a in anchors
                            if a["status"] == "found")))
    if geo_mode:
        # 推导链要能一条日志读完：面板 bbox → 按钮 bbox 比例 → 中心点 → 模板分。
        chain = (buy.get("geo") or {}).get("chain") or []
        if chain:
            msg += "｜推导：" + " → ".join(chain)
    res = _result(status, True, size, boxes, anchors, attempts, meta, msg,
                  offset=(buy.get("offset") if (cd_mode or text_mode or geo_mode
                                                 or manual_mode) else off))
    res["path"] = ("countdown" if cd_mode else
                   "text" if text_mode else
                   "manual" if manual_mode else
                   "geometry" if geo_mode else "template")
    res["limit_px"] = round(float(cap), 1)
    if cd_mode:
        # 关系落盘：这些量是「相对倒计时行」的，不是绝对坐标。
        res["buy_anchor"] = {"cd_bbox": buy.get("cd"),
                             "cd_text": buy.get("cd_text"),
                             "offset_x": (buy.get("anchor_rel") or {}).get("offset_x"),
                             "offset_y": (buy.get("anchor_rel") or {}).get("offset_y"),
                             "w": (buy.get("anchor_rel") or {}).get("w"),
                             "h": (buy.get("anchor_rel") or {}).get("h"),
                             "ref": (buy.get("anchor_rel") or {}).get("ref"),
                             "button": buy.get("found_rect"),
                             "check": buy.get("check"),
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return res


def confirm_search_rect(size):
    """确认弹窗的搜索区像素框。"""
    w, h = float(size[0]), float(size[1])
    x, y, bw, bh = CONFIRM_SEARCH
    return (int(round(w * x)), int(round(h * y)),
            max(1, int(round(w * bw))), max(1, int(round(h * bh))))


def calibrate_confirm(cfg, image, size=None, save=True):
    """确认弹窗**第一次出现时**现场标定，命中就把 ROI 缓存进配置。

    启动阶段不碰它：弹窗不在画面里时模板分只有 0.2x，拿它当启动锚点等于
    每轮必 fallback。这里只在弹窗区（`CONFIRM_SEARCH`）里找，命中后用
    **缩放后**模板尺寸算框，写进 `regions["confirm"]`（归一化 ROI，不是
    像素模板）+ `calibration.confirm_*`；手框过的确认区域一律不动。

    返回 `{ok, score, center, norm, msg, manual}`。
    """
    if cfg is None or not hasattr(cfg, "set_region"):
        return {"ok": False, "manual": False, "msg": "没有可写的配置对象"}
    if isinstance(image, str):
        image = cv2.imread(image)
    if image is None:
        return {"ok": False, "manual": False, "msg": "读不到画面"}
    size = size or (image.shape[1], image.shape[0])
    cal = cfg.get("calibration")
    if not isinstance(cal, dict):
        cal = {}
        cfg.data["calibration"] = cal
    applied = cal.get("applied") if isinstance(cal.get("applied"), dict) else {}
    if regions.is_manual(cfg, "confirm", applied.get("confirm")):
        return {"ok": False, "manual": True,
                "msg": "确认区域是手框值，保持不动（不再用模板覆盖）"}
    tpl = _tpl_gray(cfg, "confirm")
    if tpl is None:
        return {"ok": False, "manual": False,
                "msg": "缺少 templates/confirm.png，无法现场标定"}
    gray = vision.to_gray(image)
    vision.set_target_screen(size, 96)
    crop, (ox, oy) = _crop(gray, confirm_search_rect(size))
    if crop is None:
        return {"ok": False, "manual": False, "msg": "弹窗搜索区不在画面内"}
    try:
        score, loc, sc = vision.match_scaled(crop, tpl, deep=False)
        if not loc or float(score or 0.0) < ABSENT_SCORE:
            # 比例元数据对不上（换过分辨率/窗口）才穷举；只在没命中时付代价。
            score, loc, sc = vision.match_scaled(crop, tpl, deep=True)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "manual": False,
                "msg": f"模板匹配异常：{type(e).__name__}: {e}"}
    score = float(score or 0.0)
    if not loc or score < ABSENT_SCORE:
        return {"ok": False, "manual": False, "score": round(score, 3),
                "msg": "弹窗区里没找到确认按钮（最高分 %.3f < %.2f）"
                       % (score, ABSENT_SCORE)}
    used = vision.scaled(tpl, sc)
    th, tw = used.shape[:2]
    fx, fy = ox + int(loc[0]), oy + int(loc[1])
    ar = tw / float(max(1, th))
    if not (CONFIRM_AR[0] <= ar <= CONFIRM_AR[1]
            and CONFIRM_W[0] <= tw / float(size[0]) <= CONFIRM_W[1]
            and CONFIRM_H[0] <= th / float(size[1]) <= CONFIRM_H[1]):
        return {"ok": False, "manual": False, "score": round(score, 3),
                "msg": ("命中块形状不像确认按钮（%d×%d，长宽比 %.2f，"
                        "占屏 %.1f%%×%.1f%%）"
                        % (tw, th, ar, 100.0 * tw / size[0],
                           100.0 * th / size[1]))}
    center = (fx + tw / 2.0, fy + th / 2.0)
    norm = {"x": round(fx / float(size[0]), 6),
            "y": round(fy / float(size[1]), 6),
            "w": round(tw / float(size[0]), 6),
            "h": round(th / float(size[1]), 6)}
    msg = ("确认区现场标定：score %.3f · 中心 (%.0f, %.0f) · ROI %s"
           % (score, center[0], center[1],
              (norm["x"], norm["y"], norm["w"], norm["h"])))
    if save:
        cfg.set_region("confirm", dict(norm))
        cal["confirm_roi"] = dict(norm)
        cal["confirm_center"] = [round(center[0], 1), round(center[1], 1)]
        cal["confirm_score"] = round(score, 3)
        cal["confirm_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        cfg._dirty.add("calibration")
        try:
            cfg.save()
        except Exception as e:                   # noqa: BLE001
            msg += f"；写盘失败（{type(e).__name__}: {e}），本次只在内存生效"
    return {"ok": True, "manual": False, "score": round(score, 3),
            "center": [round(center[0], 1), round(center[1], 1)],
            "norm": norm, "rect": [fx, fy, tw, th], "msg": msg}


def _manual_ready(cfg, required):
    """本路线必需的区域是否都已被用户手框过（或被用户确认过）。

    跨屏、遮挡这类场景没法自动核对，但用户手框一次就是明确的人工定位；
    这时闸门要放行，否则「请手动框一次」的提示是句空话。
    """
    if cfg is None or not hasattr(cfg, "region_raw"):
        return False
    cal = cfg.get("calibration") if isinstance(cfg.get("calibration"), dict) else {}
    applied = cal.get("applied") if isinstance(cal.get("applied"), dict) else {}
    for name in required:
        if name not in region_table.BUILTIN:
            continue
        if not regions.is_manual(cfg, name, applied.get(name)):
            return False
    return True


def gate_reason(result, cfg=None, workflow=None):
    """当次闸门：返回拦截原因，`None` 表示放行。

    · `fallback/error/skipped` 一律拦——`skipped` 不是绿灯：没核对过
      按钮位置就用拉伸内置坐标点，1280×800 偏 29px、3440×1440 偏 307px；
    · `adjusted` 也不能无条件放行：微调量超过可信上限（旧版写下的
      +53/−152px）当次就拦，不显示成「绿 + 警告」；
    · 唯一例外：本路线必需的区域都已被用户手框（或明确确认）过，
      此时用配置坐标就是用户自己定的，放行。
    """
    res = result or {}
    st = res.get("status")
    required = _required(workflow)
    if st in ("fallback", "error", "skipped"):
        if _manual_ready(cfg, required):
            return None
        return res.get("message") or "自动标定未通过"
    if st == "adjusted":
        off = res.get("offset") or (0.0, 0.0)
        size = res.get("client") or (1920, 1080)
        # 上限必须和标定时算的同一口径（模板短边 × TOL_RATIO）；
        # 这里再按 anchors 里的模板尺寸重算一次，避免历史结果没有
        # `limit_px` 时退回写死的 24px 把几何推导误杀。
        lim = res.get("limit_px") or _max_adjust(size, res.get("anchors"))
        if abs(off[0]) > lim or abs(off[1]) > lim:
            return ("微调量 %+.1f/%+.1f px 超出可信上限 %.1f px，判为不可信"
                    "偏移，本次不采用" % (off[0], off[1], lim))
    return None


def _write_boxes(cfg, boxes, cal):
    """把一批归一化框写入 regions；手框区域跳过。返回 (applied, skipped)。"""
    prev = cal.get("applied") if isinstance(cal.get("applied"), dict) else {}
    applied, skipped = {}, []
    for name, box in (boxes or {}).items():
        if name not in region_table.BUILTIN:
            continue
        if regions.is_manual(cfg, name, prev.get(name)):
            skipped.append(name)
            continue
        cfg.set_region(name, box)
        applied[name] = dict(box)
    if applied:
        prev.update(applied)
        cal["applied"] = prev
    return applied, skipped


def apply_proposal(cfg):
    """用户点「应用提议」：把 `calibration.proposed` 写进 regions。

    手动框过的区域永远跳过；返回实际写入/跳过的清单，界面据此说明。
    写入前先备份整段 regions，写后读回校验；不一致就回滚并报错。
    """
    cal = cfg.get("calibration")
    if not isinstance(cal, dict):
        cal = {}
        cfg.data["calibration"] = cal
    prop = cal.get("proposed") if isinstance(cal.get("proposed"), dict) else {}
    if not prop:
        return {"ok": False, "msg": "没有待应用的标定提议",
                "applied": [], "skipped": []}
    backup = _backup.save_snapshot(cfg, "apply-proposal")
    if not backup:
        return {"ok": False, "applied": [], "skipped": [],
                "msg": "无法备份当前区域坐标，本次不应用提议"}
    applied, skipped = _write_boxes(cfg, prop, cal)
    cal["apply_skipped_manual"] = skipped
    cfg._dirty.add("calibration")
    try:
        cfg.save()
    except Exception as e:                       # noqa: BLE001
        _backup.restore(cfg, backup)
        return {"ok": False, "applied": [], "skipped": skipped,
                "msg": f"写盘失败（{type(e).__name__}: {e}），已回滚到之前的坐标"}
    bad = _backup.verify_applied(applied)
    if bad:
        _backup.restore(cfg, backup)
        return {"ok": False, "applied": [], "skipped": skipped,
                "rolled_back": backup,
                "msg": ("%s 写入后读回不一致（可能被外部改动），已回滚到"
                        "之前的坐标" % region_table.LABELS.get(bad, bad))}
    if not applied:
        return {"ok": False,
                "msg": "提议涉及的区域都是手动框过的，未覆盖",
                "applied": [], "skipped": skipped}
    return {"ok": True, "applied": sorted(applied), "skipped": skipped,
            "backup": backup,
            "msg": ("已应用标定提议 %d 个区域" % len(applied)
                    + (("；%d 个手框区域保持不变" % len(skipped))
                       if skipped else ""))}


def _coords_mismatch(cfg, result, workflow):
    """闸门要校验的是**引擎真正会点的坐标**（配置里的 regions）。

    提议没落盘时配置还是旧值，闸门却校验提议，两套坐标并存 →
    4K 上限 48px 的 40/47/48px 全放行但实际真偏 40/47/48px。
    这里检查「必需区域」的配置值是否等于本次标定结果；不等且不是
    手框（手框是用户自己定的，放行）就判不一致。
    """
    norm = result.get("norm") or {}
    cal = cfg.get("calibration") if isinstance(cfg.get("calibration"), dict) else {}
    applied = cal.get("applied") if isinstance(cal.get("applied"), dict) else {}
    for name in _required(workflow):
        want = norm.get(name)
        if not want or name not in region_table.BUILTIN:
            continue
        raw = cfg.region_raw(name) if hasattr(cfg, "region_raw") else None
        if raw is None or regions.same_box(raw, want):
            continue
        if regions.is_manual(cfg, name, applied.get(name)):
            continue
        return name
    return None


def record(cfg, result, save=True, apply=None, workflow=None):
    """把结果写进 `calibration` 节，并按需写入坐标。

    规则（独立复核后定稿）：

      · 默认 `apply=None` 跟随 `calibration.apply_auto`（默认 True）：
        标定结果**直接写进 regions**，引擎点到哪、闸门就校验到哪，只有
        一套坐标。留提案不落盘会让两套坐标并存——闸门校验提案、引擎点
        配置，4K 下 40/47/48px 全部「放行」但真实偏移一模一样。
      · 写前先把整段 `regions` + `calibration.applied` 快照到用户数据目录
        （`core/region_backup.py`，不在仓库里）；备份失败就不写。
      · 写后**读回校验**：重新读盘确认写入值等于提案值；不一致或写盘
        异常就回滚快照、本次判拦截。用户原来的坐标不会丢。
      · 手动框过的区域**永不覆盖**（与内置值不同、又不是上次自动写入的
        值，就算手动）；`calibration.auto=True` 也压不过它。
      · `fallback` 的 boxes 已经是内置值，写入等于恢复默认，不带偏移。
      · `skipped` 不写：没核对过的坐标不应该被当成「已确认」。
    """
    status = result.get("status", "never")
    proposal = result.get("norm") if status in ("adjusted", "fallback") else {}
    cal = cfg.get("calibration")
    if not isinstance(cal, dict):
        cal = {}
        cfg.data["calibration"] = cal
    if apply is None:
        apply = bool(cal.get("apply_auto", True))

    applied, skipped, backup, write_error = {}, [], "", ""
    if apply and proposal and save:
        backup = _backup.save_snapshot(cfg, "auto-calibration")
        if backup:
            applied, skipped = _write_boxes(cfg, proposal, cal)
        else:
            write_error = ("无法把当前区域坐标备份到用户数据目录"
                           f"（{_backup.backup_dir()}），本次未写入坐标；"
                           "请检查该目录是否可写")

    block = gate_reason(result, cfg, workflow) or ""
    if not block and applied:
        bad = _coords_mismatch(cfg, result, workflow)
        if bad:
            block = ("%s 的配置坐标与本次标定结果不一致，可能被旧值或"
                     "外部改动覆盖；本次不启动" % region_table.LABELS.get(bad, bad))
    if write_error and not block:
        block = write_error

    # 最终生效的路径：手框缓存放行时单独标出来（验证/日志要看这一条）。
    path = result.get("path", "")
    manual_lift = (not block
                   and status in ("fallback", "error", "skipped")
                   and _manual_ready(cfg, _required(workflow)))
    if manual_lift:
        path = "manual"

    cal.update({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "attempts": int(result.get("attempts") or 0),
        "message": result.get("message", ""),
        "block": block,
        "manual_ready": bool(_manual_ready(cfg, _required(workflow))),
        "applied_now": sorted(applied),
        "client": list(result.get("client") or [0, 0]),
        "offset": result.get("offset"),
        "offsets": [a.get("offset") for a in result.get("anchors", [])
                    if a.get("offset")],
        "anchors": [{"name": a.get("name"), "label": a.get("label"),
                     "status": a.get("status"), "offset": a.get("offset"),
                     "score": a.get("score"), "method": a.get("method"),
                     "tpl": a.get("tpl"),
                     "found_center": a.get("found_center"),
                     "found_rect": a.get("found_rect")}
                    for a in result.get("anchors", [])],
        "path": path,
        "limit_px": result.get("limit_px"),
        "buy_anchor": (result.get("buy_anchor") or cal.get("buy_anchor") or {}),
        "proposed": proposal or {},
        "apply_skipped_manual": skipped,
        "backup": backup,
        "rolled_back": "",
    })
    cfg._dirty.add("calibration")

    if save:
        err = None
        try:
            cfg.save()
        except Exception as e:                   # noqa: BLE001
            err = e
        bad = None
        if applied and err is None:
            bad = _backup.verify_applied(applied)
        if applied and (err is not None or bad):
            why = (f"写盘失败：{type(err).__name__}: {err}" if err is not None
                   else "%s 写入后读回值不一致（可能被外部改动）"
                        % region_table.LABELS.get(bad, bad))
            rb = _backup.restore(cfg, backup)
            block = (f"自动标定坐标没有可靠落盘（{why}）；"
                     "已回滚到写入前的坐标，本次不启动")
            cal["block"] = block
            cal["rolled_back"] = rb
            cal["applied_now"] = []
            result["gate"] = block
            try:
                cfg.save()
            except Exception:                    # noqa: BLE001
                pass

    result["gate"] = block
    result["backup"] = backup
    result["rolled_back"] = cal.get("rolled_back", "")
    return result


def run_for_engine(cfg, workflow=None):
    """开抢前跑一次：抓当前客户区 → 校验 → 写配置。

    结果分两用：`status` 给界面看，`gate` 给闸门用（record 算好）。抓帧/
    异常返回 `error`，与「没锚点」的 `skipped` 同权——两者都拦，唯一放行
    条件是必需区域已被用户手框。
    """
    if workflow is None:
        run = cfg.get("run") if isinstance(cfg.get("run"), dict) else {}
        workflow = run.get("workflow") or cfg.get("workflow")
    try:
        cal = cfg.get("calibration")
        # 只有「不是 Config」的映射（旧接口 / 引擎的最小 dict 替身）才按
        # 「未配置」放行：自动标定对它不适用，也不能把 dict 当 Config 去写
        # `.data`。真 Config 的空节/坏节一律按「自动标定关闭」处理，仍然
        # 走闸门（P0-B），不能靠清空配置绕过。
        if not isinstance(cal, dict) or not cal:
            if not isinstance(getattr(cfg, "data", None), dict):
                return {"ok": True, "status": "skipped", "gate": "",
                        "attempts": 0, "anchors": [],
                        "message": ("配置对象没有标定节（旧接口），"
                                    "本次不做自动标定，按现有坐标启动")}
            cal = {}
        if not bool(cal.get("auto", False)):
            res = {"ok": True, "status": "skipped", "attempts": 0,
                   "message": ("自动标定已关闭，按钮位置未经校对；请到"
                               "「高级设置 → 区域微调」手动框一次再启动"),
                   "anchors": []}
            record(cfg, res, save=True, apply=False, workflow=workflow)
            return res
        sc = regions.open_screen(cfg)
        try:
            if getattr(sc, "client_partial", False):
                c = getattr(sc, "client_rect", None) or {}
                b = getattr(sc, "bounds", None) or {}
                res = {
                    "ok": False, "status": "error", "attempts": 0,
                    "client": [c.get("width", 0), c.get("height", 0)],
                    "message": (
                        "游戏窗口有一部分在屏幕外（客户区 %s,%s 起，%s×%s；"
                        "显示器 %s,%s 起，%s×%s），越界部分抓不到，自动标定"
                        "拒绝执行。请把窗口完整移回屏幕内，或打开"
                        "「高级设置 → 区域微调」手动框一次。"
                        % (c.get("left", "?"), c.get("top", "?"),
                           c.get("width", "?"), c.get("height", "?"),
                           b.get("left", "?"), b.get("top", "?"),
                           b.get("width", "?"), b.get("height", "?"))),
                    "anchors": [],
                }
                record(cfg, res, save=True, apply=False, workflow=workflow)
                return res
            frame = sc.grab()
            size = sc.size
        finally:
            sc.close()
        res = calibrate_image(frame, size=size, cfg=cfg, workflow=workflow)
        # 默认跟随 calibration.apply_auto（默认 True）：坐标直接落盘，
        # 闸门校验的就是引擎会点的那套值，不存在两套坐标。
        record(cfg, res, save=True, workflow=workflow)
        return res
    except Exception as e:                      # 抓帧/屏幕异常按 error 拦
        res = {"ok": False, "status": "error", "attempts": 0,
               "message": f"自动标定没跑起来：{type(e).__name__}: {e}",
               "anchors": []}
        try:
            record(cfg, res, save=False, workflow=workflow)
        except Exception:
            pass
        return res
