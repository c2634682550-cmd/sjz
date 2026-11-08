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
import collections
import json
import math
import os
import re

import cv2
import numpy as np

from . import bricks, cards as cardmod, money, ocr
from . import vision

# 价格/磨损的文本语法只有一份，在 `core/money.py`（N11）。这里只保留
# 「价格判定」的策略：下限、型号 token 排除、磨损与价格怎么分工。
#
# 以前这里还自带一套正则（挤空格、逗号当千位、句点当千位），和
# `ocr_chain.price_from_text` / `cards._price_token_in` 各写一份，同一个
# `15 252` 三处读出三个值（252 / 15252 / 252），`¥12 345` 换行 `678`
# 又被拼成 12345678。语法收敛后本模块只做两步：按行切开（跨行不拼），
# 再把 money 给出的候选按下面的策略挑一个价格。

# OCR 把千位逗号认成汉字：`15 囤 000`。只在**行内**把「数字-单个汉字-数字」
# 的空隙换成空格，交给 money 按千分位分组校验；分不了组的照样拒识。
_GAP_GLYPH = re.compile(r"(?<=\d)[ \t]*[\u4e00-\u9fff][ \t]*(?=\d)")

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

# ---- 整幅兜底的保守判据（静默错价比拒识危险得多）----
# 背景：早期整幅兜底「取最大 ≥ _MIN_PRICE 的整数」。列表/详情整帧里同时存在
# `编号 000104429` 这类字段，编号比价格大时兜底就把编号当价格，抢购场景会直接
# 下成错价单（实测 9,999,999 被读成 104,429）。下面这套判据只接受**能确认是价格
# 语义**的数，认不出来就返回 None。
_PRICE_LABEL_WORDS = ("售价", "价格")           # 行内标签：命中即认价格语义
_ID_LINE = re.compile(r"编号|序号|(?:^|[^A-Za-z])ID(?:[^A-Za-z]|$)")
_MAX_PRICE_DIGITS = 8      # 价格本体最多 8 位；更长的纯数字串按编号处理
_ID_LEADING_ZEROS = 2      # 连续 ≥2 个前导 0 按编号处理（单个 0 是 OCR 补位）
_PRICE_REF_TOL = 0.01      # 与结构化价格比对时的相对容差（1%）
PRICE_UNREAD = "价格未识别"


def _is_id_token(tok):
    """token 是不是编号类：位数超长，或带 ≥2 个前导 0。"""
    digits = re.sub(r"\D", "", tok or "")
    if not digits:
        return True
    if len(digits) > _MAX_PRICE_DIGITS:
        return True
    return len(digits) - len(digits.lstrip("0")) >= _ID_LEADING_ZEROS


def _ref_value(ref_price):
    """把面板拼条/结构化块给的价格转成数值；转不动就当没有参照。"""
    if ref_price is None or isinstance(ref_price, bool):
        return None
    if isinstance(ref_price, (int, float)):
        return float(ref_price) or None
    tok = str(ref_price).strip().replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return None
    try:
        return float(tok) or None
    except ValueError:
        return None


# ---- 帧 memo 埋点（只统计、不改判据）----
# 灰度差小于它当「没变」。这是**统计**口径，不参与任何复用决策：
# 要不要复用上一帧仍然只看「整帧逐字节相同」。
MEMO_NOISE = 12
MEMO_WINDOW = 24      # 学「时间列」用的帧窗口
MEMO_FLUSH = 200      # 每多少帧往台账写一行累计比例


def _squeeze_digits(text):
    """只做一件事：把「数字-汉字-数字」的间隔换成空格（`15 囤 000`）。

    这里**不再**挤掉数字之间的空格本身：那一格是不是千分位由 money 的
    分组规则判（`15 252` 合法 → 15252，`12 34` 非法 → 拒识）。以前那条
    启发式会把 `12 34` 直接拼成 1234，读出一个"看起来合理"的错值。
    """
    return _GAP_GLYPH.sub(" ", text or "")


def _clean_lines(text):
    """逐行 clean，行尾保留换行。`ocr.clean` 会把 `\\n` 压成空格 ——
    价格解析的窗口不能跨行，压平了就等于把两行拼成一个数（N11）。"""
    return "\n".join(ln for ln in (ocr.clean(x) for x in money.split_lines(text)) if ln)


def parse_row(text, ref_price=None, whole_frame=False):
    """把一行文字解析成可过滤的字段。

    返回 {text, price, craft, wear, wear_value, numbers, price_note}
    认不出的 price 就是 None，并把 price_note 标成「价格未识别」——
    调用方要按「未知」处理，不能当 0，更不能拿一个看着合理的错值去下单。

    整幅兜底只在**能确认价格语义**的行上取值：
      1. 行内带 `售价` / `价格` 标签，或带 ¥ / ￥ 货币符；
      2. 行内数值与调用方给的 ref_price（面板拼条/结构化块）在 1% 容差内一致。
    其余一律拒识。`编号` / `序号` / `ID` 行、位数超长或带 ≥2 个前导 0 的数字串
    显式排除——它们是编号，不是价格。整幅兜底必须传 whole_frame=True：
    整帧里同时有编号、倒计时、数量，取最大整数就是静默错价（实测
    `编号 000104429` 把 9,999,999 顶成 104,429）。单卡行文本仍保留
    「多候选取最大」的历史行为，那条路径的候选只在同一张卡的字段里。

    跨行不拼：文本按行切开，每行各自解析。`¥12 345` 换行 `678` 只读
    第一行的 12345，不会变成 12345678（N11）。
    """
    if not text:
        return {"text": "", "price": None, "craft": None, "wear": None,
                "wear_value": None, "numbers": [], "price_note": PRICE_UNREAD}

    lines = [_squeeze_digits(ln) for ln in money.split_lines(text)]

    price = None
    labeled_ints = []
    ints, floats = [], []
    for line in lines:
        if not line.strip():
            continue
        # 编号类整行不作价格候选（`编号 000104429` 是货号，不是价签）
        if _ID_LINE.search(line):
            continue
        # 带货币符号的行走整段解析：失败就整行作废，不回落去抓行内数字
        # （`−¥250` 不能读成 250）。金额分支仍要求整数、≥ 下限。
        if any(ch in money.SYMBOLS for ch in line):
            v, tok = money.parse_line(line, strip_leading_zeros=True)
            if v is None or "." in tok:
                continue
            if _is_id_token(tok):
                continue
            if v >= _MIN_PRICE and price is None:
                price = v
            continue

        # 先把型号 token 整段摘掉，剩下的数字才是价格/磨损的候选。
        labeled = any(w in line for w in _PRICE_LABEL_WORDS)
        rest = _MODEL_TOKEN.sub(" ", line)
        for item in money.find_numbers(rest, strip_leading_zeros=True,
                                       dot3_as_thousands=True, max_frac=6):
            if item["decimal"]:
                floats.append(item["value"])
                continue
            if _is_id_token(item["token"]):
                continue
            ints.append(item["value"])
            if labeled and item["value"] >= _MIN_PRICE:
                labeled_ints.append(item["value"])

    # 整幅兜底：只认标签行 / 与 ref_price 一致的数，绝不取最大整数。
    # 单卡行文本保留历史行为（候选都在同一张卡的字段里，取最大不会撞编号）。
    ref = _ref_value(ref_price)
    if price is None and ref is not None and ints:
        tol = max(1.0, ref * _PRICE_REF_TOL)
        hit = [v for v in ints if abs(v - ref) <= tol]
        if hit:
            price = max(hit)
    if price is None and not whole_frame and ints:
        cand = [v for v in ints if v >= _MIN_PRICE]
        if cand:
            price = max(cand)
    if price is None and labeled_ints:
        price = labeled_ints[0]

    # 磨损值：优先取 0~5 之间的小数，排除掉价格本身
    wear_value = None
    for v in sorted(floats):
        if 0.0 <= v <= 5.0 and (price is None or abs(v - price) > 1e-9):
            wear_value = v
            break

    craft = bricks.parse_craft(text)
    wear = bricks.parse_wear(text)

    return {"text": text, "price": price, "craft": craft, "wear": wear,
            "wear_value": wear_value, "numbers": ints + floats,
            "price_note": None if price is not None else PRICE_UNREAD}


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
        return _scan_cards(img, found, upscale=max(1, upscale or 1),
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

    def __init__(self, upscale=1, read_text=None, delta=None, bottom_half=False):
        self.upscale = upscale
        self.read_text = read_text
        self.delta = delta
        self.bottom_half = bottom_half   # 只在下半屏找线（网格照样铺满全屏）
        self.prev = None
        self.cards = []
        self.cache = {}
        self.ocr_calls = 0        # 测试用：数一共认了几张卡
        self._grid_prev = None    # 整屏 OCR 兜底：上一帧的图与结果
        self._grid_rows = []
        # PERF:flow 整帧完全相同时的结果复用（见 scan 开头）
        self._memo_img = None
        self._memo_gray = None
        self._memo_rows = []
        # PERF:memo 埋点（只统计、不改判据）
        self.memo_stat = {"total": 0, "same": 0, "time_only": 0,
                          "diff": 0, "noise": 0, "first": 0, "warming": 0}
        self._memo_hist = collections.deque(maxlen=MEMO_WINDOW)
        self._memo_band = None
        # 首帧预热：把 OCR 冷启动（模型加载 + 首次前向，约 1.2s）挪出扫描
        if read_text is None:
            cardmod.warm()

    def reset(self):
        """列表刷新/翻页后调用：上一帧的缓存全作废。"""
        self.prev = None
        self.cards = []
        self.cache.clear()
        self._grid_prev = None
        self._grid_rows = []
        self._memo_img = None
        self._memo_gray = None
        self._memo_rows = []
        self._memo_hist.clear()
        self._memo_band = None

    def memo_stats(self):
        """当前累计的帧分类计数（same/time_only/diff/noise/first/total）。"""
        return dict(self.memo_stat, band=self._memo_band)

    def scan(self, img):
        if img is None or getattr(img, "size", 0) == 0:
            return []
        self.memo_stat["total"] += 1
        # PERF:flow 整帧逐字节相同 → 直接回上一帧结果（约 0.6ms），省掉
        # locate_cards + changed_cards。判据是「完全相同」而不是阈值：
        # 阈值会漏掉单个数字变化，而那正是抢购要抓的东西。
        # 抓屏本身没变、列表也没变（列表只在倒计时跳秒时变）时命中率很高。
        if (self._memo_img is not None
                and getattr(img, "shape", None) == self._memo_img.shape
                and cv2.norm(img, self._memo_img, cv2.NORM_L1) == 0.0):
            self.memo_stat["same"] += 1
            self._memo_flush()
            return [dict(r) for r in self._memo_rows]
        # 灰度图只在未命中时才转：整帧相同时连这 0.3ms 都不花
        gray = vision.to_gray(img) if img.ndim == 3 else img
        self._memo_note(gray)
        # 灰度图若就是原图本身（已是单通道），留一份拷贝，防止调用方原地改写
        memo_gray = gray if gray is not img else gray.copy()
        found = cardmod.locate_cards(img, gray=gray,
                                     bottom_half=self.bottom_half)
        if not found:
            # 找不到卡片线，退回整屏 OCR（2×，1× 会漏小字）。
            # 整屏一帧 1.4~2.5s，静止帧直接复用上一帧结果 —— 兜底路径大多
            # 停在筛选/详情页，画面根本不动，没必要每帧重认一遍。
            # 判「动没动」用 1/4 缩略图上「明显变化」的像素数，而不是均值：
            # 均值会被大面积的静止背景稀释，一个价格数字变了也当没变。
            sig = cv2.resize(img, (480, 270), interpolation=cv2.INTER_AREA)
            if (self._grid_prev is not None
                    and int((cv2.absdiff(self._grid_prev, sig) > 20).sum()) < 120):
                return [dict(r) for r in self._grid_rows]
            kw = {"upscale": 2}
            if self.read_text is not None:
                kw["read_text"] = self.read_text
            rows = scan(img, **kw)
            self._grid_prev = sig
            self._grid_rows = [dict(r) for r in rows]
            self._memo_img = img.copy()
            self._memo_gray = memo_gray
            self._memo_rows = [dict(r) for r in rows]
            self._memo_flush()
            return rows

        kw = {} if self.delta is None else {"delta": self.delta}
        # prev_cards 必须传：两帧卡片数不一样时（翻页/刷新）下标不能按位置
        # 对应，否则会把上一批的缓存（名字、价格）当成新卡用。
        changed = cardmod.changed_cards(self.prev, img, found,
                                        prev_cards=self.cards, **kw)
        fresh = self._read_fresh(img, found, changed)

        rows = []
        for i, rect in enumerate(found):
            if i not in fresh and i in self.cache:
                row = dict(self.cache[i])
                x0, y0, x1, y1 = rect
                row.update(x0=x0, y0=y0, x1=x1, y1=y1, cy=round((y0 + y1) / 2.0, 1))
            else:
                got = fresh.get(i) or {}
                # 和 _scan_cards 同一口径：结构化块优先，批量 text_batch 只展示。
                name = (got.get("name") or "").strip()
                grade = got.get("grade") or ""
                price_tok = got.get("price") or ""
                if got.get("text_reliable", True) is not False:
                    text = _clean_lines(got.get("text") or "")
                    # 结构化块有价就把它当参照：整幅文字里与它一致的数才认价
                    row = parse_row(text, ref_price=price_tok)
                    row["name"] = name
                    row["name_source"] = ("structured" if name
                                          else ("text" if text else "none"))
                else:
                    row = _row_from_blocks(name, grade, price_tok,
                                           _clean_lines(got.get("text_batch") or ""))
                if not (row["text"] or name or price_tok):
                    self.cache.pop(i, None)
                    continue
                if self.read_text is None:
                    # 批量补认已经把底带的价格写回 got["price"]；这里不再
                    # 逐卡触发 read_card_bottom（那会把批量收益吃掉）。
                    price = _price_from_block(got.get("price"))
                else:
                    price = _price_from_block(got.get("price"), img, rect,
                                              self.read_text)
                if price is not None:
                    row["price"] = float(price)
                wear = bricks.parse_wear_loose(grade) or row.get("wear")
                if wear:
                    row["wear"] = wear
                x0, y0, x1, y1 = rect
                row.update(x0=x0, y0=y0, x1=x1, y1=y1, cy=round((y0 + y1) / 2.0, 1))
                self.cache[i] = row
            rows.append(row)

        self.prev = img.copy()
        self._memo_img = self.prev          # 只读共用，少一次整帧拷贝
        self._memo_gray = memo_gray
        self.cards = found
        rows.sort(key=lambda r: (r["y0"], r["x0"]))
        self._memo_rows = [dict(r) for r in rows]
        self._memo_flush()
        return rows

    def _read_fresh(self, img, found, changed):
        """认出「这一帧需要重认」的卡，返回 {卡片下标: read_card 结果}。

        生产路径（read_text=None）把全部待认卡拼成一张图走**一次** OCR；
        注入假 OCR 的测试路径逐张调用（测试要数调用次数）。
        """
        if not changed:
            return {}
        if self.read_text is not None:
            out = {}
            for i in changed:
                out[i] = cardmod.read_card(img, found[i], upscale=self.upscale,
                                           read_text=self.read_text)
                self.ocr_calls += 1
            return out

        idx = list(changed)
        recs = [found[i] for i in idx]
        got = cardmod.read_cards(img, recs, upscale=self.upscale)
        self.ocr_calls += len(idx)
        # 价格块没读出数 → 底带补认。缺几张就补几张，但只走一次拼图；
        # 逐张补认在「六张卡全换货」时等于多扫一整轮。
        need = [k for k, g in enumerate(got)
                if _price_from_block(g["price"]) is None]
        if need:
            bots = cardmod.read_bottoms(img, [recs[k] for k in need])
            for k, bot in zip(need, bots):
                got[k]["price"] = bot.get("price") or ""
        return {i: g for i, g in zip(idx, got)}

    # ---------- 帧 memo 埋点：只统计，不参与任何判定 ----------

    def _memo_note(self, gray):
        """把「这一帧和上一帧差在哪」记到 memo_stat 的一张账上。

        三类：整帧相同（same，含只有噪声级差异）、只有时间列变化
        （time_only）、完全不同（diff）。另有 first（还没有上一帧）、
        noise（差异全部低于 MEMO_NOISE）、warming（时间列还没学出来，
        前 8~12 帧归不了类）三个辅助桶。

        **判据没动**：要不要复用上一帧的结果仍然只看「整帧逐字节相同」。
        这里只是记账 —— 阈值化会把「价格少了一位」也当成没变，那正是
        抢购要抓的东西，所以这条必须留给人决策。
        """
        st = self.memo_stat
        if self._memo_gray is None or self._memo_gray.shape != gray.shape:
            st["first"] += 1
            return
        d = cv2.absdiff(gray, self._memo_gray)
        mask = d >= MEMO_NOISE
        anyc = mask.any(axis=0)
        cols = np.flatnonzero(anyc)
        if not cols.size:
            st["noise"] += 1
            return
        self._memo_hist.append(anyc)
        if len(self._memo_hist) >= 8 and st["total"] % 4 == 0:
            self._memo_learn_band()
        band = self._memo_band
        if band is None:
            # 时间列还没学出来（开跑前 8~12 帧），此时归不了类。
            # 记成 warming 而不是硬算 diff，否则会把「前面几帧」算成翻页噪声。
            st["warming"] += 1
        elif int(cols.min()) >= band[0] and int(cols.max()) <= band[1]:
            st["time_only"] += 1
        else:
            st["diff"] += 1

    def _memo_learn_band(self):
        """从最近 MEMO_WINDOW 帧的变化列里学「时间列」的 x 区间（纯统计）。

        倒计时那一列几乎每帧都在动，价格列只在换货时才动 —— 取「最近 24 帧
        里 60% 以上都动过」的最长连续列段，作为「时间列」。宽度限制在
        4px~25% 屏宽，否则宁可不算（学歪了只会让比例失真，不影响抢购）。
        """
        counts = np.sum(np.array(self._memo_hist, dtype=np.int16), axis=0)
        thr = max(3, int(0.6 * len(self._memo_hist)))
        xs = np.flatnonzero(counts >= thr)
        if not xs.size:
            self._memo_band = None
            return
        best, start, prev = (0, 0), xs[0], xs[0]
        for x in xs[1:]:
            if x - prev > 2:
                if prev - start > best[1] - best[0]:
                    best = (start, prev)
                start = x
            prev = x
        if prev - start > best[1] - best[0]:
            best = (start, prev)
        w = self._memo_gray.shape[1]
        wide, narrow = int(best[1] - best[0]), w * 0.25
        # 转成 python int：band 会进 JSON 台账，numpy int64 不能直接序列化
        self._memo_band = ((max(0, int(best[0]) - 2), int(best[1]) + 2)
                           if 4 <= wide <= narrow else None)

    def _memo_flush(self):
        """每 MEMO_FLUSH 次扫描往台账追加一行累计比例（不改判据）。

        **默认不落盘**：只有显式设置 `DAFORS_MEMO_STATS=<路径>` 才写文件。
        缺省去写仓库 `data/` 会污染工作树（混进 git status），而这份台账
        只是统计，不该成为运行时的副作用；不设环境变量时计数只留在内存里，
        由调用方 `memo_stats()` 取。
        """
        st = self.memo_stat
        if st["total"] % MEMO_FLUSH:
            return
        path = os.environ.get("DAFORS_MEMO_STATS")
        if not path:
            return
        n = st["total"]
        rec = dict(st, band=self._memo_band,
                   same_ratio=round(st["same"] / n, 3),
                   time_only_ratio=round(st["time_only"] / n, 3),
                   diff_ratio=round(st["diff"] / n, 3),
                   noise_ratio=round(st["noise"] / n, 3),
                   warming_ratio=round(st["warming"] / n, 3))
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            return        # 台账写不进去不影响扫描


def _row_from_blocks(name, grade, price_tok, debug_text=""):
    """批量路径的行：**只**由结构化字段拼出来。

    批量 rec 的词流（`text_batch`）与单卡路径不是逐字一致的（窄块被补宽后
    分隔符会丢），所以这里不拿它解析任何字段：
      - `name` 来自名称块，匹配与点击只认它；
      - 价格 / 做工 / 成色由调用方用价格块、成色块覆盖；
      - 磨损小数从**结构化 grade 块**里取（不是从拼好的整行里刮）；
      - `text` 只是给界面/日志看的展示串，不参与判据；
      - `name_source` 记名字出处（structured / text_batch / none），
        `check()` 据此拦下降级行，不让它参与自动购买。
    """
    meta = parse_row(grade or "")
    if not name and debug_text:
        # 结构化名称空了、只剩批量文本：降级，只展示不决策
        return {"text": debug_text, "price": None, "craft": None, "wear": None,
                "wear_value": meta["wear_value"], "numbers": meta["numbers"],
                "name": "", "name_source": "text_batch"}
    display = "\n".join(x for x in (name, grade, price_tok) if x)
    return {"text": display, "price": None, "craft": None, "wear": None,
            "wear_value": meta["wear_value"], "numbers": meta["numbers"],
            "name": name, "name_source": "structured" if name else "none"}


def _scan_cards(img, found, upscale=1, read_text=None):
    """批量认卡，拼成和 _scan_grid 一样的行结构。

    生产路径（read_text=None）把这一帧所有卡拼成一张图走一次 OCR，
    缺价格/成色的卡再拼一张底带图补认一次 —— 不再「一张卡一次推理」。
    upscale 只对注入 read_text 的测试路径有意义。

    行字段的取值顺序：**结构化块（name / price / grade）优先**；
    只有单卡路径那份可靠词流（`text`）才允许参与 parse_row 兜底。
    批量路径的 `text_batch` 绝不进决策链路（见 `_row_from_blocks`）。
    """
    found = [tuple(int(v) for v in r) for r in found]
    if not found:
        return []
    if read_text is None:
        got_all = cardmod.read_cards(img, found, upscale=upscale)
    else:
        got_all = [cardmod.read_card(img, rect, upscale=upscale,
                                    read_text=read_text) for rect in found]

    # 先解析一遍：缺价格/成色的卡记下来，后面一次拼图补齐
    parsed, miss = [], []
    for k, (rect, got) in enumerate(zip(found, got_all)):
        # 名字/成色/价格一律取结构化块；批量路径的 text_batch 只当展示串。
        name = (got.get("name") or "").strip()
        grade = got.get("grade") or ""
        price_tok = got.get("price") or ""
        reliable = got.get("text_reliable", True) is not False
        if reliable:
            # 单卡 / 注入 read_text 的路径：text 是可靠词流，可以兜底解析
            text = _clean_lines(got.get("text") or "")
            row = parse_row(text, ref_price=price_tok)
            row["name"] = name
            row["name_source"] = ("structured" if name
                                  else ("text" if text else "none"))
        else:
            row = _row_from_blocks(name, grade, price_tok,
                                   _clean_lines(got.get("text_batch") or ""))
        if not (row["text"] or name or price_tok):
            parsed.append(None)
            continue
        # 价格是单独一块认出来的，直接用它，别在拼起来的整段文字里猜 ——
        # 名字里的型号数字（QBZ95 / M250）会被当成价格。
        price = _price_from_block(got["price"])
        craft, wear = _grade_from_block(got["grade"])
        parsed.append([row, price, craft, wear, got])
        # 缺字段才补认，而且一卡最多补一次：价格和成色挤在同一条底带上，
        # 分开补等于把同一块图放大读两遍（u3 15 张卡，光补认就多吃 1.4s）。
        if (price is None or not wear) and img is not None:
            miss.append(k)
    bots = {}
    if miss:
        if read_text is not None:
            bots = {k: cardmod.read_card_bottom(img, found[k], upscale=3,
                                                read_text=read_text)
                    for k in miss}
        else:
            bots = dict(zip(miss, cardmod.read_bottoms(img, [found[k] for k in miss],
                                                       upscale=3)))

    out = []
    for k, (rect, got) in enumerate(zip(found, got_all)):
        item = parsed[k]
        if item is None:
            continue
        row, price, craft, wear, _got = item
        bottom = bots.get(k)
        if bottom is not None:
            if price is None:
                price = _price_from_block(bottom.get("price"))
            craft = craft or bricks.parse_craft(bottom.get("grade"))
            wear = wear or bricks.parse_wear(bottom.get("grade"))
        # 价格只信「价格块」里认出来的那一个：认不出来就是 None，
        # 不能让 parse_row 在整段文字里猜出来的数留着（会把 9,500 读成 500）。
        row["price"] = float(price) if price is not None else None
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
    big = cardmod.read_card_bottom(img, rect, upscale=3, read_text=read_text)
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
        return money.digits_first_line(t)

    d = digits(text)
    if d and len(d) <= 8:
        return int(d)
    if img is not None and rect is not None and read_text is not None:
        big = cardmod.read_card_bottom(img, rect, upscale=3, read_text=read_text)
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
        # 按行拼：同一行用空格、跨行用换行。`" ".join` 会把两行的数字
        # 拼成一行，解析时跨行吞位（N11）。
        text = _clean_lines("\n".join(r["text"] for r in ocr.rows(card)))
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


def _match_text(row):
    """匹配商品名用的文本：只认结构化名称（或单卡路径的可靠词流）。

    批量路径的 `text_batch` 与单卡路径的词流不是逐字一致的（窄块被补宽后
    分隔符会丢），用它匹配就可能匹配到错的名字、点错商品 —— 这里明确排除：
    `name_source` 为 text_batch / none 的行直接给空串（`check()` 也先一步
    拦掉了，这里是第二道闸）。`_scan_grid` 的退路行没有 `name_source`，
    行为保持原样（整行文字本来就是那条路径唯一的来源）。
    """
    src = row.get("name_source")
    if src in ("text_batch", "none"):
        return ""
    name = (row.get("name") or "").strip()
    if name:
        return name
    return row.get("text") or ""


def _matches_name(row, task):
    """名称匹配：任务名或任一别名，模糊比对。"""
    names = [task.get("name") or ""] + list(task.get("keywords") or [])
    names = [n for n in names if n]
    if not names:
        return True, 1.0
    hit, score = ocr.best_match(_match_text(row), names, floor=0.55)
    return hit is not None, round(score, 3)


def _matches_brick(row, task):
    """赛季 / 做工 / 成色 过滤。

    赛季筛选用砖皮知识库反查：行里的文字认得出是哪一季才放行。
    任务没设赛季就不管。
    """
    season = (task.get("season") or "").strip()
    if not season:
        return True, "不限赛季"
    info = bricks.identify(_match_text(row))
    if not info.get("ok"):
        return False, "认不出赛季"
    seasons = {o["season"] for o in info["options"]}
    if season.upper() in seasons:
        return True, f"{season} 命中"
    return False, f"不是 {season}（认成 {'/'.join(sorted(seasons))}）"


def _task_number(task, key):
    """任务里的数值参数：有限且非负才算数。

    审查 R03-N14：`float("nan")` 会让 `lo and price < lo` 恒为假，等于把
    上下限静默关掉；`-100` 的下限同理。脏值按「未设置」处理。
    """
    try:
        v = float(task.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v) or v < 0:
        return 0.0
    return v


def check(row, task):
    """一行是否符合任务条件。返回 (ok, 原因)。

    降级行（`name_source` 为 `text_batch` / `none`：结构化名字块没认出来，
    只剩批量调试文本或什么都没有）**一律不参与自动购买**，只展示。
    批量词流与单卡词流不是逐字一致的，拿它匹配商品名就可能点错货。
    """
    src = row.get("name_source")
    if src in ("text_batch", "none"):
        return False, f"降级：name_source={src}，仅展示不购买"

    ok, score = _matches_name(row, task)
    if not ok:
        return False, f"名称不符（{score}）"

    lo = _task_number(task, "min_price")
    hi = _task_number(task, "max_price")
    if row["price"] is None:
        # **价格读不出来就默认拦掉。** 只有显式配置 allow_unknown_price 才放行。
        # 以前只在配了上下限时才拦：没配限价（最常见）的行价一律放行，OCR 读
        # 残的那条（价格 None）总被当成「最便宜」，pick() 永远第一个挑它，
        # 点进去买的是条没核对过价格的挂单。
        if not task.get("allow_unknown_price"):
            return False, "价格没读出来"
    else:
        price = row["price"]
        if not math.isfinite(price):
            return False, "价格不是有限数字"
        # 0 元/负价只可能是解析残片，绝不能因为「最便宜」被挑中（审查 R03-N07）
        if price <= 0:
            return False, f"价格 {price:g} 不可信"
        if lo and price < lo:
            return False, f"价格 {price:.0f} 低于下限 {lo:.0f}"
        if hi and price > hi:
            return False, f"价格 {price:.0f} 高于上限 {hi:.0f}"

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

    max_wear = _task_number(task, "max_wear")
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
            diag.append({"text": row.get("text") or "", "ok": False,
                         "why": "已经点过",
                         "name_source": row.get("name_source")})
            continue
        ok, why = check(row, task)
        diag.append({"text": row.get("text") or "", "ok": ok, "why": why,
                     "name_source": row.get("name_source")})
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


def scan_tab(tab, page, img):
    """按「页签 → 界面类型」分派解析器。**唯一的枚举入口**。

    三个页签是三套界面，绝不能拿一份解析通吃：
      collection / charm → onsale    → 6 张挂单卡（scan）
      mandel            → mandel_buy → 下单层字段（mandel_buy.read）

    `page` 与页签映射对不上时**抛 InterfaceMismatch**（nav.assert_interface），
    不返回空结果 —— 空列表会被上层当成「没货」，真机事故就是这么发生的
    （mandel 判 unknown → 报「进不去挂单列表」）。

    返回统一结构：`{"page_type": ..., ...}`，落盘时用它区分 onsale / mandel_buy。
    """
    from . import mandel_buy as _mb
    from . import nav as _nav

    want = _nav.assert_interface(tab, page)
    if want == "mandel_buy":
        out = _mb.read(img)
        out["tab"] = tab
        return out
    return {"page_type": "onsale", "tab": tab, "rows": scan(img) or []}
