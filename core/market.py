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

from . import bricks, cards as cardmod, ocr
from . import vision

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

# 数字之间的空格（可能连着 OCR 认错的单字，如 `15 囤 00`）
_DIGIT_GAP = re.compile(r"(?<=\d)\s*[\u4e00-\u9fff]?\s*(?=\d)")
# 「小数 + 间隔 + 一笔价格」的数字串：小数段要先保护起来，见 _squeeze_digits。
# 条件是后面那笔数至少 4 位（或带千位逗号），否则它更像是磨损值被 OCR 拆开
# 了（`0.583 172` 应该合成 `0.583172`，不能挤出一个 172 的价格）。
_DECIMAL_TAIL = re.compile(
    r"(\d[\d,]*\.\d+)(\s*[\u4e00-\u9fff]?\s*)(?=\d[\d,]{3,}|\d+,\d)")

# 型号 token：字母开头、内部含数字（QBZ95-1 / M250 / MP7 / KC17 / S2）。
# 这些数字**不是价格**，必须先从文本里摘掉再取数。
#
# 本地回归（录屏实测）：卡片文字 `QBZ951 突击步枪王牌之剑` 里 95-1 被 OCR 粘成 951，
# 早期实现「取最大整数当价格」于是 price=951.0。而 pick() 按价格升序挑最便宜的，
# 这条读不出真价格的行就永远排在真价格（25000）前面被选中 —— 点进去买的是别的挂单。
# `M250` 得 250、`M7` 得 7、`QBZ95-1` 得 95，同一个 bug 的其它写法。
#
# 只认「字母开头」：数字开头的型号（93R）靠下面的 _MIN_PRICE 兜底，
# 否则 `15000S` 这种价格和字母粘在一起的写法会被整段吃掉。
_MODEL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*\d[A-Za-z0-9.\-]*")

# 价格至少三位数。一两位的整数在这些卡片里全部来自型号残片或认错的 `S2` 尾巴，
# 当成价格只会让 pick() 选中垃圾行。宁可判「读不出价」（该行被跳过），
# 也不能让它冒充最便宜的一条。
_MIN_PRICE = 100


def _squeeze_digits(text):
    """把数字串中间的空格和 OCR 误认的单字挤掉。

    `15 000` -> `15000`；`15 囤 00` -> `15000`；`015 252` -> `015252`。
    只处理「空格/单个汉字夹在两个数字之间」的情况，不动正常文字。

    **小数不能被挤。** `0.120 9,500`（磨损 + 价格）里的空格左边属于磨损值
    `0.120`，挤掉就变成 `0.1209,500`，切出 `0.1209` 和 `500` —— 价格读成
    500，pick() 立刻挑中它去买贵的。所以先把小数整段保护起来，挤完再还原。
    """
    if not text:
        return text
    held = []

    def _hold(m):
        held.append(m.group(1))
        # 只把小数段换成占位符，中间的空格原样留着 —— 挤空格时占位符
        # 左边不是数字，`(?<=\d)` 就不会跨过小数和小数后面那笔价格。
        return f"\x00{len(held) - 1}\x00" + m.group(2)

    tmp = _DECIMAL_TAIL.sub(_hold, text)
    prev = None
    cur = tmp
    # 反复挤，直到稳定（`15 囤 000` 这类可能要两轮）
    while cur != prev:
        prev = cur
        cur = _DIGIT_GAP.sub("", cur)
    for i, tok in enumerate(held):
        cur = cur.replace(f"\x00{i}\x00", tok)
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


def parse_row(text):
    """把一行文字解析成可过滤的字段。

    返回 {text, price, craft, wear, wear_value, numbers}
    认不出的就是 None —— 调用方要按「未知」处理，不能当 0。

    解析只用文本：放大后的 OCR 会把 `15,000` 当成一个词（内部带空格），
    按词框拼接反而拼不出来，所以统一在文本层用 _squeeze_digits 处理。
    """
    if not text:
        return {"text": "", "price": None, "craft": None, "wear": None,
                "wear_value": None, "numbers": []}

    text = _squeeze_digits(text)

    price = None
    m = _CURRENCY.search(text)
    if m:
        price = _to_number(m.group(1))

    # 先把型号 token 整段摘掉，剩下的数字才是价格/磨损的候选。
    # 不摘的话 `QBZ95-1` 的 951、`M250` 的 250、`M7` 的 7 都会被当成价格，
    # 而 pick() 按价格升序挑，这些假低价会挤掉真挂单。
    rest = _MODEL_TOKEN.sub(" ", text)

    ints, floats, float_toks = [], [], []
    for tok in _NUM.findall(rest):
        v = _to_number(tok)
        if v is None:
            continue
        if _is_decimal_token(tok):
            floats.append(v)
            float_toks.append(tok)
        else:
            ints.append(v)

    # 价格没带 ¥ 时取最大的整数：列表里通常只有价格和数量两种整数，
    # 价格远大于数量。低于 _MIN_PRICE 的不算候选（见该常量的注释）。
    if price is None and ints:
        cand = [v for v in ints if v >= _MIN_PRICE]
        if cand:
            price = max(cand)

    # 千位分隔符被认成句点：`9,000` -> `9.000`。
    # 2026-11-01 换 PP-OCRv5 之后才出现（Windows OCR 是丢逗号/认成汉字），
    # 而 `_to_number` 会把它当小数 —— 9000 变 9，pick() 立刻挑中它去买。
    # 只在这个前提下才当分隔符：
    #   · 整数部分 > 5（磨损值是 0~5 的小数，价格不可能 6 块以下）
    #   · 小数部分正好三位
    # 两条都满足才转换，宁可少认一个价，也不能把磨损当成价格。
    if price is None:
        for tok in float_toks:
            ip, _, fp = tok.replace(",", "").partition(".")
            if len(fp) == 3 and ip.isdigit() and 1 <= len(ip) <= 3 and int(ip) > 5:
                v = int(ip + fp)
                if v >= _MIN_PRICE and (price is None or v > price):
                    price = v

    # 磨损值：优先取 0~5 之间的小数，排除掉价格本身
    wear_value = None
    for v in sorted(floats):
        if 0.0 <= v <= 5.0 and (price is None or abs(v - price) > 1e-9):
            wear_value = v
            break

    craft = bricks.parse_craft(text)
    wear = bricks.parse_wear(text)

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


def scan(img, min_row_h=8, upscale=2, card_h=206, cols=2, col_split=None, read_text=None):
    """扫描一张列表截图，返回卡片列表（带像素坐标，相对这张图）。

    走的是「先定位、再认字」：OpenCV 找卡片矩形（cards.locate_cards），
    再按卡片切出 名称/成色/价格 三块、各自放大后交给 OCR。
    整屏 OCR 再按词距聚类的老路子留着当退路（cards 找不到线时用），
    因为卡片内「名字到价格」的距离比卡片之间的缝隙还大，聚类会拆错卡。

    upscale: 文字块放大倍数，默认 2。13px 的价格不放大读不出千位逗号。
    card_h / cols / col_split: 只在退回固定网格时用。
    read_text: 可注入的 OCR（测试用），默认 core.ocr.read_text。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return []

    found = cardmod.locate_cards(img)
    if found:
        return _scan_cards(img, found, upscale=max(3, upscale or 1),
                           read_text=read_text)
    return _scan_grid(img, min_row_h=min_row_h, upscale=upscale,
                      card_h=card_h, cols=cols, col_split=col_split)


class Scanner:
    """带帧间缓存的扫描器：没变的卡片不重复认字。

    列表模式的循环是 20 次/秒，但一屏六张卡里通常只有一张在变。
    每帧都把六张全认一遍，等于把 OCR 的时间全花在没变的卡上，
    抢购最贵的就是那几百毫秒。

    用法：scanner = Scanner(); rows = scanner.scan(frame)
    """

    def __init__(self, upscale=3, read_text=None, delta=None):
        self.upscale = upscale
        self.read_text = read_text
        self.delta = delta
        self.prev = None
        self.cards = []
        self.cache = {}
        self.ocr_calls = 0        # 测试用：数一共认了几张卡

    def reset(self):
        """列表刷新/翻页后调用：上一帧的缓存全作废。"""
        self.prev = None
        self.cards = []
        self.cache.clear()

    def scan(self, img):
        if img is None or getattr(img, "size", 0) == 0:
            return []
        found = cardmod.locate_cards(img)
        if not found:
            # 找不到卡片线，退回整屏 OCR；这条路上没有缓存可谈。
            # 只用默认值时按原样调用，测试替换掉 scan() 也还能接得上。
            kw = {}
            if self.upscale != 3:
                kw["upscale"] = self.upscale
            if self.read_text is not None:
                kw["read_text"] = self.read_text
            return scan(img, **kw)

        kw = {} if self.delta is None else {"delta": self.delta}
        # prev_cards 必须传：两帧卡片数不一样时（翻页/刷新）下标不能按位置
        # 对应，否则会把上一批的缓存（名字、价格）当成新卡用。
        changed = cardmod.changed_cards(self.prev, img, found,
                                        prev_cards=self.cards, **kw)
        read = self.read_text or ocr.read_text

        rows = []
        for i, rect in enumerate(found):
            if i not in changed and i in self.cache:
                row = dict(self.cache[i])
                x0, y0, x1, y1 = rect
                row.update(x0=x0, y0=y0, x1=x1, y1=y1, cy=round((y0 + y1) / 2.0, 1))
            else:
                got = cardmod.read_card(img, rect, upscale=self.upscale, read_text=read)
                self.ocr_calls += 1
                text = ocr.clean(got["text"])
                if not text:
                    self.cache.pop(i, None)
                    continue
                row = parse_row(text)
                price = _price_from_block(got["price"], img, rect, read)
                if price is not None:
                    row["price"] = float(price)
                wear = bricks.parse_wear_loose(got["grade"]) or row.get("wear")
                if wear:
                    row["wear"] = wear
                x0, y0, x1, y1 = rect
                row.update(x0=x0, y0=y0, x1=x1, y1=y1, cy=round((y0 + y1) / 2.0, 1))
                self.cache[i] = row
            rows.append(row)

        self.prev = img.copy()
        self.cards = found
        rows.sort(key=lambda r: (r["y0"], r["x0"]))
        return rows


def _scan_cards(img, found, upscale=3, read_text=None):
    """逐张卡片认字，拼成和 _scan_grid 一样的行结构。"""
    if read_text is None:
        read_text = ocr.read_text
    out = []
    for rect in found:
        got = cardmod.read_card(img, rect, upscale=upscale, read_text=read_text)
        text = ocr.clean(got["text"])
        if not text:
            continue
        row = parse_row(text)
        # 价格是单独一块认出来的，直接用它，别让 parse_row 在拼起来的
        # 整段文字里猜 —— 名字里的型号数字（QBZ95 / M250）会被当成价格。
        price = _price_from_block(got["price"], img, rect, read_text)
        # 价格只信「价格块」里认出来的那一个：认不出来就是 None，
        # 不能让 parse_row 在整段文字里猜出来的数留着（会把 9,500 读成 500）。
        row["price"] = float(price) if price is not None else None
        craft, wear = _grade_from_block(got["grade"], img, rect, read_text)
        if not wear:
            # 在售页的成色印在卡片**左上角**（名称上方），不在底行；
            # 但只认「成色 X」这种写法 —— 名称里的 M4A1 / SCAR-H 也是字母。
            craft2, wear2 = _grade_from_block(got["name"], strict=True)
            craft, wear = craft or craft2, wear2
        row["craft"] = craft or row.get("craft")
        row["wear"] = wear or row.get("wear")
        x0, y0, x1, y1 = rect
        row.update(x0=x0, y0=y0, x1=x1, y1=y1, cy=round((y0 + y1) / 2.0, 1))
        out.append(row)
    out.sort(key=lambda r: (r["y0"], r["x0"]))
    return out


def _grade_from_block(text, img=None, rect=None, read_text=None, strict=False):
    """成色块 → (做工, 成色字母)。认不全就放大再认一次。

    `极品 | 成色 A` 这一块里两个信息都有；放大到 5 倍基本都能认全，
    认不出就返回 (None, None)，由调用方保留整段文字的解析结果。

    strict=True 用在**名称**块上：名称里本来就有 M4A1 / SCAR-H 这种字母，
    只认「成色 X」这一种写法。
    """
    craft = bricks.parse_craft(text)
    wear = bricks.parse_wear(text, strict=strict)
    if (craft and wear) or (img is None or rect is None or read_text is None):
        return craft, wear
    big = cardmod.read_card(img, rect, upscale=5, read_text=read_text)
    return (bricks.parse_craft(big.get("grade")) or craft,
            bricks.parse_wear(big.get("grade"), strict=strict) or wear)


def _price_from_block(text, img=None, rect=None, read_text=None):
    """价格块单独认出来的数字 → 价格。

    只留数字：`025,000` 里的前导 0 是金币图标被认成 0，`15,000` 的逗号是
    千位分隔符 —— 两种都得去掉，否则 25000 会变成 25。

    **不设位数下限**：真机上真的有 2 个三角币的挂单（用户确认过），
    按「至少四位」过滤会把它们全丢掉。这块图里只有「金币图标 + 价格」，
    没有型号数字，所以短数字是可信的；读不出来（空 / 0 / 超过八位）才返回 None。
    退路 `_scan_grid` 那条路上仍然按 _MIN_PRICE 兜底，因为那里是从整段文字里猜。
    """
    def digits(t):
        return ocr.digits_only(t, strip_zeros=True)

    d = digits(text)
    if d and len(d) <= 8:
        return int(d)
    if img is not None and rect is not None and read_text is not None:
        big = cardmod.read_card(img, rect, upscale=5, read_text=read_text)
        d2 = digits(big.get("price"))
        if d2 and len(d2) <= 8:
            return int(d2)
    return None


def _scan_grid(img, min_row_h=8, upscale=2, card_h=206, cols=2, col_split=None):
    """退路：整屏 OCR，再按固定网格把词分到卡片里。

    只有在 locate_cards 一条线都找不到时才会走到这里（比如换了个分辨率、
    或者列表是半透明浮在场景上的）。
    """
    src = img
    up = upscale or 1
    if up != 1:
        src = vision.upscale(img, up)

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
    hit, score = ocr.best_match(row["text"], names, floor=0.55)
    return hit is not None, round(score, 3)


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
    if row["price"] is None:
        # **价格读不出来就默认拦掉。** 只有显式配置 allow_unknown_price 才放行。
        # 以前只在配了上下限时才拦：没配限价（最常见）的行价一律放行，OCR 读
        # 残的那条（价格 None）总被当成「最便宜」，pick() 永远第一个挑它，
        # 点进去买的是条没核对过价格的挂单。
        if not task.get("allow_unknown_price"):
            return False, "价格没读出来"
    else:
        if lo and row["price"] < lo:
            return False, f"价格 {row['price']:.0f} 低于下限 {lo:.0f}"
        if hi and row["price"] > hi:
            return False, f"价格 {row['price']:.0f} 高于上限 {hi:.0f}"

    craft = (task.get("craft") or "").strip()
    if craft:
        if row["craft"] is None:
            # 读不出就**别拦**。在售页的卡片上只写「成色A/B/S」，不写品阶
            # （传说/史诗/稀有/普通）—— 这一项在列表里根本读不到。而筛选
            # 面板早把品阶筛成目标那一档了（SkinFinder 干的），所以读不到
            # 不代表不符合。拦下来的后果是**每一行都被跳过、一件都抢不到**
            # （真机实测：2 行全被判「做工没读出来」，日志里连倒计时都没读到）。
            pass
        elif row["craft"] != craft:
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


def _same_spot(row, spot):
    """exclude 里的一个元素是不是就是指这一行。

    老调用方传的是一行的 y（浮点）；新调用方传 (x0, cy) 二元组。**只按 y
    比会误伤**：交易行列表是网格排布，同一横排的几张卡 y 完全相同，按 y
    排除会把整排剩下的卡一起判成「已经点过」—— 那一排永远不再点。
    """
    if isinstance(spot, (tuple, list)):
        if len(spot) < 2:
            return abs(row["cy"] - float(spot[0])) <= 6
        return (abs(row["cy"] - float(spot[1])) <= 6 and
                abs(row["x0"] - float(spot[0])) <= 8)
    return abs(row["cy"] - float(spot)) <= 6


def pick(rows, task, exclude=None):
    """从行里挑一条。返回 (row, 原因, 全部诊断)。

    exclude 是已经点过的位置：老的传 y，新的传 (x0, cy)，两种都认。
    排序按任务的 sort 字段：price（默认，捡漏就是找最便宜的）。
    """
    exclude = exclude or []
    diag = []
    hits = []
    for row in rows:
        if any(_same_spot(row, spot) for spot in exclude):
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
    else:
        # 屏幕上的阅读顺序：先上后下，同一排先左后右。
        # 只按 y0 排是不够的 —— 列表是两列，同一排两张卡的 y0 相同，
        # 排出来的先后取决于 OCR 返回的顺序（不稳定）。
        hits.sort(key=lambda r: (r["y0"], r["x0"]))

    best = hits[0]
    return best, f"选中第 {len(diag)} 行里的 1 条", diag
