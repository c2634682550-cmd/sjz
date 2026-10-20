"""交易行列表扫描。

和「详情页盯按钮」是两条路：
  详情页模式 —— 你已经停在某件商品的页面上，抢的是「按钮变可点」那一瞬。
  列表模式   —— 你不知道目标什么时候出现、叫什么名字，只能盯搜索结果列表，
                谁先符合条件就买谁。捡漏靠这个。

流程：OCR 整个列表区域 → 按行聚词 → 每行解析 名称/价格/做工/成色/磨损 →
按任务条件过滤 → 挑最合适的 → 点它进详情页 → 交给原来的购买流程。

解析故意做得宽松：认不出的字段留 None，由过滤决定「跳过」还是「放行」，
绝不猜。宁可漏一件，不可买错一件——买错一件的损失比漏十件大。
"""
import re

import cv2

from . import bricks, ocr

# 交易行里价格是带千位逗号的整数（15,000 / 9,999,999），磨损是 0~10 的小数
# （0.583）。所以逗号必须当千位分隔符去掉，不能当小数点 —— 早期版本把
# `15,000` 解析成 15.0，价格整体错 1000 倍。
#
# Windows OCR 对千位逗号有三种破坏方式（都在实测截图里出现过）：
#   1. 整个丢掉、分成两个词   15,000 -> `15` + `000`
#   2. 认成汉字               15,000 -> `15` + `囤` + `00`
#   3. 换成一个空格           15,000 -> `15 000`（放大后常见）
# 所以下面先把数字串里的空格挤掉，再按逗号当千位分隔符解析。
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CURRENCY = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")
_CRAFT = re.compile(r"(极品|优品)")
# 成色字母：实际渲染是 `极品丨成色 A` / `极品成色 S`。解析出来的文本里「做工」
# 和字母之间可能隔着 `丨 成色 ` 这类字符，所以允许中间有少量任意非字母字符。
# 早期版本只允许一个连字符，于是所有成色都读不出来（实测 6/6 张 wear=None）。
_WEAR_LETTER = re.compile(r"(?:极品|优品)[^SABC]{0,8}?([SABC])(?![A-Za-z])")

# 数字之间的空格（可能连着 OCR 认错的单字，如 `15 囤 00`）
_DIGIT_GAP = re.compile(r"(?<=\d)\s*[\u4e00-\u9fff]?\s*(?=\d)")


def _squeeze_digits(text):
    """把数字串中间的空格和 OCR 误认的单字挤掉。

    `15 000` -> `15000`；`15 囤 00` -> `15000`；`015 252` -> `015252`。
    只处理「空格/单个汉字夹在两个数字之间」的情况，不动正常文字。
    """
    if not text:
        return text
    prev = None
    cur = text
    # 反复挤，直到稳定（`15 囤 000` 这类可能要两轮）
    while cur != prev:
        prev = cur
        cur = _DIGIT_GAP.sub("", cur)
    return cur


def _to_number(tok):
    """把 OCR 出来的数字 token 转成 float。

    千位逗号去掉：`15,000` -> 15000。小数点保留：`0.583` -> 0.583。
    两者都出现时（`1,234.5`）逗号仍是千位分隔符。
    """
    if tok is None:
        return None
    cleaned = tok.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _is_decimal_token(tok):
    """这个 token 是小数（磨损）还是整数（价格）。

    判据是「有没有小数点」，不是「有没有逗号」——逗号是千位分隔符。
    """
    return "." in tok


def parse_row(text, words=None):
    """把一行文字解析成可过滤的字段。

    返回 {text, price, craft, wear, wear_value, numbers}
    认不出的就是 None —— 调用方要按「未知」处理，不能当 0。

    words 参数保留是为了兼容旧调用点，当前解析只用文本：放大后的 OCR 会把
    `15,000` 当成一个词（内部带空格），按词框拼接反而拼不出来，所以统一在
    文本层用 _squeeze_digits 处理。
    """
    if not text:
        return {"text": "", "price": None, "craft": None, "wear": None,
                "wear_value": None, "numbers": []}

    text = _squeeze_digits(text)

    price = None
    m = _CURRENCY.search(text)
    if m:
        price = _to_number(m.group(1))

    ints, floats = [], []
    for tok in _NUM.findall(text):
        v = _to_number(tok)
        if v is None:
            continue
        (floats if _is_decimal_token(tok) else ints).append(v)

    # 价格没带 ¥ 时取最大的整数：列表里通常只有价格和数量两种整数，
    # 价格远大于数量
    if price is None and ints:
        price = max(ints)

    # 磨损值：优先取 0~5 之间的小数，排除掉价格本身
    wear_value = None
    for v in sorted(floats):
        if 0.0 <= v <= 5.0 and (price is None or abs(v - price) > 1e-9):
            wear_value = v
            break

    craft = None
    mc = _CRAFT.search(text)
    if mc:
        craft = mc.group(1)

    wear = None
    mw = _WEAR_LETTER.search(text)
    if mw:
        wear = mw.group(1).upper()

    return {"text": text, "price": price, "craft": craft, "wear": wear,
            "wear_value": wear_value, "numbers": ints + floats}


def _grid_cards(words, width, card_height, rows, cols, col_split):
    """按固定网格切卡片。

    为什么不用「找间隙聚类」：实测（放大 2 倍后）同一张卡片内「名字行→属性行」
    中心距 334px，而跨卡片「属性行→下一张名字行」只有 76px —— 卡内距离**大于**
    跨卡距离（卡片是上下两段布局）。任何「找大间隙」的方法都会分错：把属性行
    和下一张的名字行并到一起，把名字行和自己的属性行拆开。

    所以用固定网格：卡片高 206px（原尺寸实测）、两列、列边界在两列中间。
    网格参数走 scan 的入参，调用方可用 config 的 regions 重配。

    card_height 是**单张卡片**的高，不是整张图的高。
    """
    cells = []
    for r in range(rows):
        y0, y1 = r * card_height, (r + 1) * card_height
        for c in range(cols):
            if cols == 1:
                x0, x1 = 0, width
            else:
                x0 = 0 if c == 0 else col_split
                x1 = col_split if c == 0 else width
            cell = [w for w in words
                    if y0 <= w["y"] + w["h"] / 2.0 < y1 and x0 <= w["x"] < x1]
            if cell:
                cells.append(cell)
    return cells


def _rect_of(ws):
    return {
        "x0": min(w["x"] for w in ws),
        "y0": min(w["y"] for w in ws),
        "x1": max(w["x"] + w["w"] for w in ws),
        "y1": max(w["y"] + w["h"] for w in ws),
    }


def scan(img, min_row_h=8, upscale=2, card_h=206, cols=2, col_split=None):
    """OCR 一张列表截图，返回卡片列表（带像素坐标，相对这张图）。

    每张卡片在 parse_row 的基础上加 x0/y0/x1/y1/cy，供点击定位。

    upscale: 放大倍数，默认 2。实测决定性：13px 高的价格数字直接 OCR 会把
    `15,000` 读成 `1500`、`8,888` 读成 `888`（千位逗号和首位数字被吞），
    放大 2 倍后 6 个价格全部读对，而且更快（174ms vs 325ms）。返回的坐标
    会缩回原尺寸。

    card_h: 卡片高度（原尺寸像素），默认 206（1920x1080 截图实测）。
    cols: 列表列数，默认 2。
    col_split: 两列的 x 分界（原尺寸像素），默认取宽度的中点。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return []
    h, w = img.shape[:2]
    src = img
    up = upscale or 1
    if up != 1:
        src = cv2.resize(img, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)

    words = [w for w in ocr.read_words(src) if w["h"] >= min_row_h * up]
    if not words:
        return []

    ch = max(1, int(card_h * up))
    rows = max(1, (src.shape[0] + ch - 1) // ch)
    split = int(col_split * up) if col_split else src.shape[1] // 2

    out = []
    for card in _grid_cards(words, src.shape[1], ch, rows, cols, split):
        text = ocr.clean(" ".join(w["text"] for w in card))
        if not text:
            continue
        rect = _rect_of(card)
        row = parse_row(text)
        row.update(x0=rect["x0"], y0=rect["y0"], x1=rect["x1"], y1=rect["y1"],
                   cy=round((rect["y0"] + rect["y1"]) / 2.0, 1))
        out.append(row)

    if up != 1:
        # 词框坐标是在放大图上量的，缩回原尺寸，调用方才能直接用于点击
        for row in out:
            for k in ("x0", "y0", "x1", "y1", "cy"):
                row[k] = row[k] / up
    out.sort(key=lambda r: (r["y0"], r["x0"]))
    return out


def _matches_name(row, task):
    """名称匹配：任务名或任一别名，模糊比对。"""
    names = [task.get("name") or ""] + list(task.get("keywords") or [])
    names = [n for n in names if n]
    if not names:
        return True, 1.0
    best = 0.0
    for n in names:
        s = ocr.similarity(row["text"], n)
        best = max(best, s)
    return best >= 0.55, round(best, 3)


def _matches_brick(row, task):
    """赛季 / 做工 / 成色 过滤。

    赛季筛选用砖皮知识库反查：行里的文字认得出是哪一季才放行。
    任务没设赛季就不管。
    """
    season = (task.get("season") or "").strip()
    if not season:
        return True, "不限赛季"
    info = bricks.identify(row["text"])
    if not info.get("ok"):
        return False, "认不出赛季"
    seasons = {o["season"] for o in info["options"]}
    if season.upper() in seasons:
        return True, f"{season} 命中"
    return False, f"不是 {season}（认成 {'/'.join(sorted(seasons))}）"


def check(row, task):
    """一行是否符合任务条件。返回 (ok, 原因)。"""
    ok, score = _matches_name(row, task)
    if not ok:
        return False, f"名称不符（{score}）"

    lo = float(task.get("min_price") or 0)
    hi = float(task.get("max_price") or 0)
    if lo or hi:
        if row["price"] is None:
            return False, "价格没读出来"
        if lo and row["price"] < lo:
            return False, f"价格 {row['price']:.0f} 低于下限 {lo:.0f}"
        if hi and row["price"] > hi:
            return False, f"价格 {row['price']:.0f} 高于上限 {hi:.0f}"

    craft = (task.get("craft") or "").strip()
    if craft:
        if row["craft"] is None:
            return False, "做工没读出来"
        if row["craft"] != craft:
            return False, f"做工是 {row['craft']}，要 {craft}"

    wear = (task.get("wear") or "").strip().upper()
    if wear:
        if row["wear"] is None:
            return False, "成色没读出来"
        if row["wear"] != wear:
            return False, f"成色是 {row['wear']}，要 {wear}"

    max_wear = float(task.get("max_wear") or 0)
    if max_wear:
        if row["wear_value"] is None:
            return False, "磨损值没读出来"
        if row["wear_value"] > max_wear:
            return False, f"磨损 {row['wear_value']} 高于 {max_wear}"

    ok, why = _matches_brick(row, task)
    if not ok:
        return False, why

    return True, f"符合（{score}）"


def pick(rows, task, exclude=None):
    """从行里挑一条。返回 (row, 原因, 全部诊断)。

    exclude 是已经点过的行的 y 坐标，避免同一行反复点。
    排序按任务的 sort 字段：price（默认，捡漏就是找最便宜的）。
    """
    exclude = exclude or []
    diag = []
    hits = []
    for row in rows:
        if any(abs(row["cy"] - y) <= 6 for y in exclude):
            diag.append({"text": row["text"], "ok": False, "why": "已经点过"})
            continue
        ok, why = check(row, task)
        diag.append({"text": row["text"], "ok": ok, "why": why})
        if ok:
            hits.append(row)

    if not hits:
        return None, "没有符合条件的行", diag

    sort = (task.get("sort") or "price").strip()
    if sort == "price":
        # 没读到价格的排后面：读不出价就没法判断便不便宜
        hits.sort(key=lambda r: (r["price"] is None, r["price"] or 0))
    elif sort == "wear":
        hits.sort(key=lambda r: (r["wear_value"] is None, r["wear_value"] or 0))
    else:  # 列表顺序
        hits.sort(key=lambda r: r["y0"])

    best = hits[0]
    return best, f"选中第 {len(diag)} 行里的 1 条", diag
