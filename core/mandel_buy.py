"""曼德尔砖「指定价格购买」下单层的字段读取。

**这一页不是在售列表**，没有挂单卡：整屏只有
  「成交均价 / 挂单数 / 指定价格购买 / 购买数量 / 预期总花费 / 挂单有效期」
以及底部一排价格档位（真机帧 2026-11-08：`mandel_3_buy.png`）。

口径（必须写死在这里，不要在外面重新解释）：
  * 底部那一排 y≈739 的数字 = **价格档位**，每一档都是三角币单价；
    最小档 = 该砖的「最低可买价」。
  * 「成交均价 170」是**行情参考**，不是可买价 —— 成交均价可以低于
    最低档（也可能高），拿它判断能不能买会得到错误结论。
  * 「预期总花费」= 当前档位 × 购买数量，是这一单要扣的三角币。

读不出的一律 None，不猜；`min_price` 读不出就返回 None，由上层报
「价格未识别」，绝不写 0。
"""
import re

from . import ocr

# 千分位优先，避免把 `133,733` 拆成 133 和 733（probe_scan3 踩过的坑）。
NUM = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")

# 以下比例来自真机 1920x1080 帧，按画幅归一化，换分辨率不用改常量。
_LADDER_Y = 0.684        # 739 / 1080：价位档位那一排
_LADDER_TOL = 0.022      # ±24px
_LADDER_X_MAX = 0.60     # 只要左侧柱状图下面那排；右侧绿色按钮里的价格不算档位
_CHART_X = (0.19, 0.56)  # 柱状图横坐标范围
_CHART_Y = (0.28, 0.66)  # 柱顶标签的纵坐标范围
_ROW_TOL = 0.045         # 同一行的纵向容差（≈49px）

_PAGE_TYPE = "mandel_buy"

# 档位那一排字很小（真机 3 位数只有 40px 宽），整屏 OCR 会把 175 读成 174、
# 180 读成 178、190 读成 194 —— 首轮真机就是这样。档位行单独裁出来放大 3×
# 再 OCR，值才稳定；放大后仍读不出 ≥4 个档位就退回整屏结果。
_LEVEL_BOX = (0.16, 0.655, 0.60, 0.715)   # x0, y0, x1, y1（归一化）
_LEVEL_SCALE = 3


def _levels_from_image(img):
    """档位行放大后 OCR，返回 [(价格, x中心_全图坐标), ...]；失败返回 []。"""
    try:
        import cv2

        h, w_full = int(img.shape[0]), int(img.shape[1])
        x0, y0, x1, y1 = _LEVEL_BOX
        crop = img[int(y0 * h):int(y1 * h), int(x0 * w_full):int(x1 * w_full)]
        if crop is None or crop.size == 0:
            return []
        big = cv2.resize(crop, None, fx=_LEVEL_SCALE, fy=_LEVEL_SCALE,
                         interpolation=cv2.INTER_CUBIC)
        words = ocr.read_words(big) or []
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for w in words:
        v = _num(w.get("text"))
        if v is None:
            continue
        out.append((v, int(x0 * w_full) + _cx(w) / _LEVEL_SCALE))
    out.sort(key=lambda it: it[1])
    return out


def _num(text):
    """词框文本 → 整数；不是数字返回 None。"""
    m = NUM.fullmatch((text or "").strip())
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


def _cx(w):
    return float(w.get("x") or 0) + float(w.get("w") or 0) / 2.0


def _cy(w):
    return float(w.get("y") or 0) + float(w.get("h") or 0) / 2.0


def _right_value(words, label, height, numeric=True):
    """找 `label` 这一行右侧的值。numeric=False 时返回原文（如 `24小时`）。"""
    h_tol = height * _ROW_TOL
    labs = [w for w in words if label in str(w.get("text") or "")]
    if not labs:
        return None
    lab = labs[0]
    lx = float(lab.get("x") or 0) + float(lab.get("w") or 0)
    ly = _cy(lab)
    right = [w for w in words
             if _cy(w) is not None and abs(_cy(w) - ly) <= h_tol
             and float(w.get("x") or 0) >= lx]
    # 同一行带里可能混进相邻字段（「购买数量」行上方 48px 就是指定价格栏的
    # 170）：先按纵向距离排，再按 x 排，取最近的那个，别按纯 x 顺序取。
    right.sort(key=lambda w: (abs(_cy(w) - ly), float(w.get("x") or 0)))
    for w in right:
        if numeric:
            v = _num(w.get("text"))
            if v is not None:
                return v
        else:
            t = str(w.get("text") or "").strip()
            if t:
                return t
    return None


def _clean_levels(levels):
    """按 x 升序剔噪：档位价格是单调递增的一排（真机 170/175/180/185/190/197）。

    放大 OCR 会把档位前的三角币图标读成 `0`（真机帧里就混进一个 0，最低价
    当场变成 0）。这里用单调递增 + 步长上限（相邻档差不到 35%）剔除这类
    噪声：0 小于前一档被丢；乱码数字大于前一档 1.35 倍也被丢。
    """
    out, last = [], None
    for v, x in sorted(levels, key=lambda it: it[1]):
        if v < 20:                     # 单价不可能低于 20 三角币；图标噪声常是 0/1
            continue
        if last is not None and not (last * 0.98 <= v <= last * 1.35):
            continue
        out.append((v, x))
        last = v
    return out


def read(img, words=None):
    """读下单层字段。返回 dict；`min_price` 读不出为 None。

    键：
      title           砖名（「曼德尔砖-王者之剑」）
      levels          [(价格, x中心), ...] 底部档位，按 x 升序
      min_price       最小档（= 最低可买价），读不出 None
      avg_price       成交均价（**参考值，不是可买价**）
      counts_raw      [(挂单数, x中心), ...] 柱顶标签原文数值
      counts          [(价格, 挂单数), ...] 按最近档位归档（可能有 ±1 档歧义）
      expected_total  预期总花费（三角币）
      qty             购买数量
      validity        挂单有效期原文（如 `24小时`）
      page_type       固定 "mandel_buy"
    """
    if words is None:
        words = ocr.read_words(img) or []
    words = [w for w in words if isinstance(w, dict) and str(w.get("text") or "").strip()]
    h = float(getattr(img, "shape", (1080, 1920, 3))[0])
    w_px = float(getattr(img, "shape", (1080, 1920, 3))[1])

    ladder_y = _LADDER_Y * h
    # 先试档位行放大 3× 的专门 OCR；读不到 ≥4 个档位再退回整屏词框。
    levels = _clean_levels(
        [it for it in _levels_from_image(img) if it[1] / w_px < _LADDER_X_MAX])
    if len(levels) < 4:
        levels = []
        for w in words:
            v = _num(w.get("text"))
            if v is None:
                continue
            if abs(_cy(w) - ladder_y) > _LADDER_TOL * h:
                continue
            if float(w.get("x") or 0) / w_px >= _LADDER_X_MAX:
                continue
            levels.append((v, _cx(w)))
        levels.sort(key=lambda it: it[1])
    # 两条路径的档位都过一遍剔噪（图标噪声 / 重读同价）。
    levels = _clean_levels(levels)

    counts_raw = []
    for w in words:
        v = _num(w.get("text"))
        if v is None or v < 1000:          # 柱值都是四位以上；档位数字已被上面吃掉
            continue
        xr = float(w.get("x") or 0) / w_px
        if not (_CHART_X[0] <= xr <= _CHART_X[1]):
            continue
        if not (_CHART_Y[0] * h <= _cy(w) <= _CHART_Y[1] * h):
            continue
        counts_raw.append((v, _cx(w)))
    counts_raw.sort(key=lambda it: it[1])

    counts = []
    if levels:
        for v, x in counts_raw:
            nearest = min(levels, key=lambda lv: abs(lv[1] - x))
            counts.append((nearest[0], v))

    title = None
    for w in words:
        t = str(w.get("text") or "").strip()
        if "曼德尔砖-" in t and float(w.get("x") or 0) / w_px >= 0.5:
            title = t
            break

    return {
        "page_type": _PAGE_TYPE,
        "title": title,
        "levels": levels,
        "min_price": min(v for v, _x in levels) if levels else None,
        "avg_price": _right_value(words, "成交均价", h),
        "counts_raw": counts_raw,
        "counts": counts,
        "expected_total": _right_value(words, "预期总花费", h),
        "qty": _right_value(words, "购买数量", h),
        "validity": _right_value(words, "挂单有效期", h, numeric=False),
    }
