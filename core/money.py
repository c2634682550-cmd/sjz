"""金额文本解析的**唯一**实现（N11）。

为什么单独一个模块
------------------
这条规则以前散在三处（`ocr_chain.price_from_text`、`market.parse_row`、
`cards._price_token_in`），三份各自漂移：同一个 `15 252`，一处读 252、
一处读 15252；同一个 `¥12 345` 换行接 `678`，一处读 12345、一处读
12345678。价格错一个数量级，预算闸门就等于没有。所以语法只在这里写一份，
三处调用，谁都不许再自己拿正则取"最后一个数"。

语法（逐类）
------------
· 全角：先做 NFKC 归一。全角数字 `１２３` → `123`，全角逗号/句点/空格
  一起归一，因此全角写法与半角同规则，不再单独判。
· 货币符号：`¥` / `￥`（归一后同为 `¥`）。符号后到行尾（遇到非金额字符
  截止）整段当一个数解析；符号前有负号（`−¥250`）或段内有负号（`¥-5`）
  一律拒识。
· 没有货币符号：`parse_line` 要求**整行只印这一个数**；`scan_text` 在自
  由文本里找候选，但绝不把"多段数字里的最后一段"直接当数 —— 见下条。
· 空格分组：数字之间的空格只在"千分位分组"语义下合并 —— 首组 1~3 位，
  其后每组**正好 3 位**（`15 252`→15252、`1 234 567`→1234567、
  `12 34` 拒识）。不符合分组规则的一律拒识，不做启发式拼接。
· 逗号分组：必须严格 `\d{1,3}(,\d{3})+`（`9,9`、`1,00,000`、`1,000,`
  全部拒识）。逗号与空格**混用**（`1,234 567`）判为畸形，拒识。
· 小数点：金额小数最多 2 位（`1,234.5`→1234.5）。3 位小数默认判为
  "逗号被认成句点/没读全"→拒识；只有调用方显式 `dot3_as_thousands=True`
  （价格带/交易行，整数部分 >5）才按千分位还原（`9.000`→9000），
  否则 3 位小数是磨损值（0~5），绝不能当价格。
· 跨行：解析窗口**不跨行**。`scan_text` 逐行解析，`¥12 345` 换行 `678`
  只读第一行 → 12345，不会吞成 12345678。
· 前导 0：默认拒识（金币图标常被认成 0）。价格带/交易行显式
  `strip_leading_zeros=True` 才去掉（`025,000`→25000）。
· 位数：1~9 位（交易行上限 99,999,999），超长按"没读全"拒识。

对外接口
--------
    parse_amount(s, **kw)     -> (value|None, token)   单个金额串
    parse_line(line, **kw)    -> (value|None, token)   一行（可带货币符号）
    find_numbers(line, **kw)  -> [ {value, token, decimal, dot3} ]  一行里的数
    scan_text(text, **kw)     -> (value|None, token)   自由文本（跨行安全，生产路径已不用）
    scan_block(text, **kw)    -> (value|None, token)   价格块（块里只该有一个价）
    scan_balance(text, **kw)  -> (value|None, token)   余额栏（只认最右一组货币）
    split_lines(text)         -> 逐行（已归一）
"""
import re
import unicodedata

MIN_DIGITS = 1
MAX_DIGITS = 9              # 交易行价格上限 99,999,999；再长按「没读全」处理

# 价格块里行首/行尾的杂字（OCR 把面板描边读成 `|` `]` 之类）。只削两端，
# 中间夹着字的行不削 —— 那是「这行根本不是价」的信号。逗号/点是数字的
# 一部分，留在原地：`1,000,` 必须继续被千分位校验打掉，不能削成 `1,000`。
_EDGE_NOISE = re.compile(r"^[^\d,.¥￥\-−]+|[^\d,\-−]+$")

SYMBOLS = "\u00a5\uffe5"    # ¥ ￥（NFKC 后 ￥ 会变成 ¥）
SIGNS = "-\u2212"           # - −（U+2212 不会归一成 '-'，两个都认）

_GROUPED = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_AMOUNT_RUN = re.compile(r"[0-9,.\s]+")
_DIGITS = re.compile(r"\d+")
# 字段标记：余额/货币栏这类"数字 + 符号"并排的文本（`2,049 0+ 0 40+`）。
# 出现它说明空格是字段分隔，不是千分位。
FIELD_MARKS = "+%\u2030"


def normalize(text):
    """全角 → 半角；`，`/`．`/`　` 跟着 NFKC 一起归一到半角。"""
    return unicodedata.normalize("NFKC", text or "")


def split_lines(text):
    """归一后按行切开。解析窗口不跨行，跨行一律在调用方先切开。"""
    return normalize(text).splitlines()


# ---------------------------------------------------------------- 单个数

def _int_digits(s):
    """整数段 → 纯数字串（分隔符合法时）；不合法返回 None。

    只认三种分隔风格：无、全逗号、全空格。混用直接 None。
    """
    s = (s or "").strip()
    if not s:
        return None
    has_comma = "," in s
    has_space = bool(re.search(r"\s", s))
    if has_comma and has_space:
        return None                     # 逗号 + 空格混用：畸形
    if has_comma:
        return s.replace(",", "") if _GROUPED.match(s) else None
    if has_space:
        groups = s.split()
        if not all(g.isdigit() for g in groups):
            return None
        if len(groups) == 1:
            return groups[0]
        if not 1 <= len(groups[0]) <= 3:
            return None
        if any(len(g) != 3 for g in groups[1:]):
            return None                 # `12 34`：不是千分位分组
        return "".join(groups)
    return s if s.isdigit() else None


def _finish(digits, *, power=0, strip_leading_zeros=False):
    """位数/前导 0 校验 → int 或 None。"""
    if digits is None:
        return None
    if len(digits) > 1 and digits[0] == "0":
        if not strip_leading_zeros:
            return None                 # 金币图标粘出来的 0
        digits = digits.lstrip("0")
        if not digits:
            return None
    if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        return None
    return int(digits) * (10 ** power)


def parse_amount(s, *, strip_leading_zeros=False, dot3_as_thousands=False,
                 max_frac=2):
    """解析「一个金额串」。返回 (value|None, token)。

    调用方传进来的必须是单个金额（不带货币符号、不带其它文字）；
    带了其它字符一律 None —— 这是"拒识"而不是"猜"。

    max_frac：允许的小数位数。金额 2 位；磨损值 4~6 位（0.583172）由
    交易行解析显式放开到 6，价格路径保持 2 —— 价格不可能带小数尾。
    """
    tok = normalize(s).strip()
    if not tok:
        return None, ""
    if any(ch in SIGNS for ch in tok):
        return None, tok                # 负金额：价格语义反了
    if any(ch not in "0123456789,. \t" for ch in tok):
        return None, tok                # 混进了文字/符号：拒识
    if tok.count(".") > 1:
        return None, tok
    if "." in tok:
        int_s, _, frac = tok.partition(".")
        frac = frac.strip()
        if not frac.isdigit():
            return None, tok
        if len(frac) == 3:
            # 三位小数：默认是磨损值 / 没读全；只有显式兼容才当"句点=千分位"
            if not dot3_as_thousands:
                return None, tok
            if "," in int_s or re.search(r"\s", int_s.strip()):
                return None, tok
            ip = int_s.strip()
            if not ip.isdigit() or not 1 <= len(ip) <= 3:
                return None, tok
            if int(ip) <= 5:
                # 整数段 0~5：这是磨损值（2.500 → 2.5），价格不可能 6 块以下
                return float(f"{int(ip)}.{frac}"), tok
            digits = _finish(ip + frac, strip_leading_zeros=strip_leading_zeros)
            return digits, tok
        if not 1 <= len(frac) <= max_frac:
            return None, tok
        digits = _int_digits(int_s)
        whole = _finish(digits, strip_leading_zeros=strip_leading_zeros)
        if whole is None:
            return None, tok
        return float(f"{whole}.{frac}"), tok
    digits = _int_digits(tok)
    value = _finish(digits, strip_leading_zeros=strip_leading_zeros)
    return value, tok


# ---------------------------------------------------------------- 一行

def _symbol_pos(line):
    for i, ch in enumerate(line):
        if ch in SYMBOLS:
            return i
    return -1


def parse_line(line, *, strip_leading_zeros=False, dot3_as_thousands=False,
               max_frac=2):
    """解析一行文字。返回 (value|None, token)。

    有货币符号：符号后整段（到非金额字符为止）按一个数解析；符号前有负号
    直接拒识，段内解析失败也拒识（**不再回落到"随便抓一个数字"**）。
    没有货币符号：整行必须就是这个数（`0.583`、`14,000`、`15 252`）。
    """
    tok_line = normalize(line).strip()
    if not tok_line:
        return None, ""
    pos = _symbol_pos(tok_line)
    if pos < 0:
        return parse_amount(tok_line, strip_leading_zeros=strip_leading_zeros,
                            dot3_as_thousands=dot3_as_thousands,
                            max_frac=max_frac)
    if sum(tok_line.count(ch) for ch in set(SYMBOLS)) != 1:
        return None, tok_line             # 一行里两个金额符号：读不准
    if any(ch in SIGNS for ch in tok_line[:pos]):
        return None, tok_line             # `−¥250`
    rest = tok_line[pos + 1:].lstrip()
    m = _AMOUNT_RUN.match(rest)
    if not m:
        return None, tok_line
    region = m.group(0).rstrip(" .")
    value, _ = parse_amount(region, strip_leading_zeros=strip_leading_zeros,
                            dot3_as_thousands=dot3_as_thousands,
                            max_frac=max_frac)
    return value, region


# ---------------------------------------------------------------- 一行里的数

def _run_numbers(run, *, strip_leading_zeros, dot3_as_thousands, max_frac=2,
                 spaces_are_fields=False):
    """一个"数字段"（只含数字/逗号/句点/空格）→ 候选数列表。

    小数 + 空格 + 整数（`0.120 9,500`）是「磨损 + 价格」两个字段：
       · 小数段单独成为候选（小数），不对它做任何拼接
       · 整数段只有本身"像价格"（≥4 位或带逗号）才成为候选 —— 否则整段作废
         （`0.583 172` 的 172 是数量/残片，不是价格）
    其余情况整段按一个数解析；解析失败就**一个候选都不产出**，不回落到
    逐段取末尾（这正是 N11 的错因）。

    spaces_are_fields=True（该行有 `+`/`%` 这类字段标记，如余额行
    `2,049 0+ 0 40+`）：这里的空格是**字段分隔符**，各段独立成数，
    不做千分位合并；价格条没有这类标记，空格仍然是千分位候选。
    """
    run = run.strip().rstrip(" .")
    if not run:
        return []
    if spaces_are_fields and re.search(r"\s", run.strip()):
        out = []
        for p in run.split():
            out.extend(_run_numbers(p, strip_leading_zeros=strip_leading_zeros,
                                    dot3_as_thousands=dot3_as_thousands,
                                    max_frac=max_frac))
        return out
    if "." in run and re.search(r"\s", run.strip()):
        parts = run.split()
        decs, ints = [], []
        for p in parts:
            if "." in p:
                v, tok = parse_amount(p, strip_leading_zeros=strip_leading_zeros,
                                     dot3_as_thousands=dot3_as_thousands,
                                     max_frac=max_frac)
                if v is not None and isinstance(v, float):
                    decs.append({"value": v, "token": tok,
                                 "decimal": True, "dot3": False})
            elif re.search(r"\d", p):
                plain = p.replace(",", "")
                if not plain.isdigit():
                    return decs       # 残片：只保留小数（磨损），不猜价格
                if len(plain) >= 4 or "," in p:
                    v, tok = parse_amount(p, strip_leading_zeros=strip_leading_zeros,
                                          max_frac=max_frac)
                    if v is not None:
                        ints.append({"value": v, "token": tok,
                                     "decimal": False, "dot3": False})
                else:
                    return decs
        return decs + ints
    value, tok = parse_amount(run, strip_leading_zeros=strip_leading_zeros,
                              dot3_as_thousands=dot3_as_thousands,
                              max_frac=max_frac)
    if value is None:
        return []
    dot3 = "." in tok and not isinstance(value, float)
    return [{"value": value, "token": tok,
             "decimal": isinstance(value, float) and not dot3, "dot3": dot3}]


def find_numbers(line, *, strip_leading_zeros=False, dot3_as_thousands=False,
                 max_frac=2):
    """一行文本里的数（按出现顺序）。返回 [{value, token, decimal, dot3}]。

    紧贴负号的段整段跳过（`-15000` 不能丢号变成 +15000）。
    """
    t = normalize(line)
    spaces_are_fields = any(ch in FIELD_MARKS for ch in t)
    out = []
    i, n = 0, len(t)
    while i < n:
        if not t[i].isdigit():
            i += 1
            continue
        j = i
        while j < n and (t[j].isdigit() or t[j] in ",. \t"):
            j += 1
        neg = i > 0 and t[i - 1] in SIGNS
        if not neg:
            out.extend(_run_numbers(t[i:j],
                                    strip_leading_zeros=strip_leading_zeros,
                                    dot3_as_thousands=dot3_as_thousands,
                                    max_frac=max_frac,
                                    spaces_are_fields=spaces_are_fields))
        i = j
    return out


# ---------------------------------------------------------------- 自由文本

def scan_text(text, *, strip_leading_zeros=False, dot3_as_thousands=False,
              max_frac=2):
    """自由文本里找那个价格。返回 (value|None, token)。

    **生产路径已不再使用**：价格走 `scan_block`（块内自洽），余额走
    `scan_balance`（只认最右一组货币）。保留这个函数只为兼容外部脚本与
    既有测试；新代码不要接它 —— 它的 `rest[-1]` 是「最后一个数字」，
    既不是「价格块里的唯一价」，也不是「最右一组货币」。

    · 逐行解析，**不跨行**：`¥12 345` / `678` 两行 → 12345
    · 带货币符号的行：只信整段解析的结果，失败就这一行作废（不回落到
      行内其它数字 —— `−¥250` 不能读成 250）
    · 没有符号的行：整行是单个数就用它，否则取行内**最后一个合格候选**
      （价格条只印一个数，货币栏三角币在最右）
    · 多行都带符号且值不一致 → 读不准，拒识
    """
    sym, rest = [], []
    for line in split_lines(text):
        if not line.strip():
            continue
        has_sym = any(ch in SYMBOLS for ch in line)
        value, tok = parse_line(line, strip_leading_zeros=strip_leading_zeros,
                                dot3_as_thousands=dot3_as_thousands,
                                max_frac=max_frac)
        if has_sym:
            if value is not None:
                sym.append((value, tok))
            continue                    # 带符号的行解析失败 → 该行拒识
        if value is not None:
            rest.append((value, tok))
            continue
        for item in find_numbers(line, strip_leading_zeros=strip_leading_zeros,
                                 dot3_as_thousands=dot3_as_thousands,
                                 max_frac=max_frac):
            rest.append((item["value"], item["token"]))
    if sym:
        if len({v for v, _ in sym}) > 1:
            return None, "多段金额不一致"
        return sym[-1]
    if rest:
        return rest[-1]
    return None, ""


def scan_block(text, *, strip_leading_zeros=False, dot3_as_thousands=False,
               max_frac=2):
    """**价格块**专用：块里只该有一个价。返回 (value|None, token)。

    与 `scan_text` 的区别只有一条：**不做跨行/跨字段兜底**。`scan_text` 的
    `rest[-1]` 是服务余额栏的（`2,049 0+ 0 40+` 空格是字段分隔，取最后一
    段）。价格块正相反 —— 块里出现第二个数，说明这块根本不是价格，或者
    价格被切成了两截，两种都只能拒识。

    这就是「静默错价」的封口：`1,000,00` 千分位过不了 → 块内没有合格值
    → None，绝不拿同帧数量列的 `63` 顶替。
    """
    vals = []
    for line in split_lines(text):
        if not line.strip():
            continue
        # 价格带右缘挨着面板描边，OCR 常把最后一个 「0」 带出一截边线（
        # `1,000,000]` / `1,000,00|`）。只切**两端**的非数字杂字，不动
        # 行中间的（中间混着字才说明这行不是价）。
        line = _EDGE_NOISE.sub("", line.strip()).strip()
        if not line:
            continue
        value, tok = parse_line(line, strip_leading_zeros=strip_leading_zeros,
                                dot3_as_thousands=dot3_as_thousands,
                                max_frac=max_frac)
        if value is None:
            continue                    # 这一行作废，不回落行内其它数字
        vals.append((value, tok))
    if not vals:
        return None, ""
    if len({v for v, _ in vals}) > 1:
        return None, "价格块多值不一致"
    return vals[-1]


# ---------------------------------------------------------------- 余额栏

# 余额栏不是自由文本，是固定结构：右上角三组货币（左→右：纸币 / 青铜币 /
# 三角币），每组数字后面跟一个 `+`（充值按钮）。真实截图（`_bug_r06_vision.md`
# R06-02）只见过这几种：
#   `2,049 0+ 22+` / `2,049 0+ 26 +` / `2,049 0+ 0 40+`  → 三角币 22 / 26 / 40
#   `2,537K 2,075 + à`  → 最右一组读成 `à`，必须拒识，不许退到中间组 2,075
# 所以可验证的规则不是「最后一个数字」（那是 `rest[-1]`，最右一组读丢时
# 会静默退到中间组），而是「最后一个带 `+` 的数字」。`K` 是金额显示的
# 千位缩写（`2,537K` = 2,537,000），按确定规则展开；其它字母残字一律拒识。
_BAL_TAIL_FIELD = re.compile(r"[\u4e00-\u9fff]+[0-9,.%+\s]*")
_BAL_TAIL_NOISE = re.compile(r"[\s,、。：:；;·\-—/|()（）\[\]【】]+")
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d{1,2})?(?:\s*[Kk](?![A-Za-z]))?")


def _balance_labeled(line, start):
    """数字紧贴汉字/字母（`限购5`）：带标签的别的字段，不是货币组。"""
    head = line[:start].rstrip()
    if not head:
        return False
    ch = head[-1]
    if ch in "Kk" and len(head) >= 2 and (head[-2].isdigit() or head[-2] == ","):
        return False                    # `2,537K`：K 是倍率后缀，不是标签
    return ("\u4e00" <= ch <= "\u9fff") or (ch.isascii() and ch.isalpha())


def _balance_value(raw):
    """货币组数字 → int；`K` 千位缩写展开，位数/格式不合返回 None。"""
    s = (raw or "").strip()
    mult = 1
    if s and s[-1] in "Kk":
        s, mult = s[:-1].strip(), 1000
    value, _ = parse_amount(s)
    if not isinstance(value, int):      # 小数不是货币读数
        return None
    value *= mult
    return value if len(str(value)) <= MAX_DIGITS else None


def _balance_tail_ok(tail):
    """候选货币组之后只允许空格/标点/带汉字的标签字段（`限购5`）。"""
    s = _BAL_TAIL_NOISE.sub("", tail or "")
    s = _BAL_TAIL_FIELD.sub("", s)
    return not s


def _balance_from_line(line):
    toks = []
    for m in _NUM_TOKEN.finditer(line):
        if _balance_labeled(line, m.start()):
            continue
        value = _balance_value(m.group(0))
        if value is None:
            continue
        tail = line[m.end():]
        pad = len(tail) - len(tail.lstrip())
        mark = tail[pad:pad + 1]
        marked = bool(mark) and mark in FIELD_MARKS
        end = m.end() + pad + (1 if marked else 0)
        toks.append((value, m.group(0), end, marked))
    marked = [t for t in toks if t[3]]
    if not marked:
        return None, line               # `+` 全丢：结构不明，不猜
    value, tok, end, _ = marked[-1]
    if not _balance_tail_ok(line[end:]):
        return None, line               # 右侧还有裸数字/读不出的残字
    return value, tok


def scan_balance(text):
    """余额栏（右上角货币条）专用：只认**最右一组货币**。返回 (value|None, token)。

    与 `scan_text` 的 `rest[-1]` 的区别：`rest[-1]` 取「最后一个数字」，
    余额要的是「最后一组货币」。最右一组读成 `à`/空时，`rest[-1]` 会静默
    退到中间那组青铜币（实测 `2,537K 2,075 + à` → 2075）；本函数返回
    None，让上层按「余额未知」处理。

    OCR 的 `read_text` 是逐词换行拼出来的，货币条本身是一行，所以先把
    换行并成空格再解析；换行若来自两条不同的 UI 行，会被当成一行 ——
    但余额 ROI 只覆盖右上角这一条，不存在第二行。
    """
    line = " ".join(normalize(text).split())
    if not line:
        return None, ""
    return _balance_from_line(line)


def digits_first_line(text):
    """文本里**第一行**带数字的行 → 纯数字串（去前导 0）。跨行不拼。

    价格块（「金币图标 + 价格」单独成带）专用：块里只有价格，但仍按行取，
    避免 `12 345` 换行 `678` 拼成 12345678。
    """
    for line in split_lines(text):
        d = "".join(_DIGITS.findall(line)).lstrip("0")
        if d:
            return d
    return ""
