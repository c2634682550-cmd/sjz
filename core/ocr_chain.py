"""OCR 主链：RapidOCR → PP-OCRv6（服务，3× 放大）→ 自建字模。

为什么要三级
------------
阅读价格这件事有硬要求：**读不到就拒识，不许猜**。单引擎再准，也有
「模型没装 / 服务挂了 / 偶发漏检」的时候，这时候必须有的退，而不是把
半截串当价格点下去。

  1. RapidOCR（core/ocr_rapid.py）：进程内、离线、12~70ms。两块基准集
     全对（36/36、42/42），是默认路径。
  2. PP-OCRv6（core/ocr_ppocr.py，HTTP 服务）：**只在 Rapid 分数 < 0.5
     或千分位格式没过时**才二次确认，且固定 3× 放大 —— 单字货币 `0`/`8`
     在 1× 下会漏（实测 8 次），3× 全对。这层是「Rapid 拿不准时再问一遍
     另一条管线」，不是每帧都跑，否则单帧 4.3s 冷启动 + 70ms 稳态白烧。
  3. 自建字模（core/digit_ocr.py）：0.08MB、4ms，纯数字快路径，噪声块会
     编数字（负样本只拒 4/8），所以只当最后一层，且必须过 margin 闸门。
"""
from . import digit_ocr, money, ocr_ppocr, ocr_rapid

SCORE_MIN = 0.5

# 详情面板「必要四块」ROI：(字段, x, y, w, h)，相对面板宽高的比例。
#
# 为什么这么定：面板 590×860 实测帧上逐词量出来的包围盒，按比例写死，
# 换分辨率等比缩放即可用。四块分别对应 名称 / 品阶+成色 / 价格 / 数量。
# 面板里的武器大图、背景、装饰、说明文字都不进 det —— 它们只是显示，
# 判价与判品阶用不到（整屏读一次 det 要覆盖 50 万像素，这里只覆盖 1 万级）。
#
# 成色这一块把「稀有度(品阶) + 成色」两行并成一块：判 craft/wear 两个字段
# 都要，拆成两次调用反而多付一次 det + 一次固定开销（实测 61ms vs 2×18ms
# 只差几毫秒，但并块少一次 python/ONNX 调用开销，整块读数还更稳）。
PANEL_ROIS = (
    ("name", 0.2339, 0.0291, 0.2847, 0.0453),
    ("grade", 0.5322, 0.4895, 0.2237, 0.0442),
    # 价格：590 宽面板下 245..390 / 758..808。旧值右缘 360，真实 7 位价
    # （`1,000,000`）墨迹右缘到 376，末位被切 → `1,000,00` 千分位不过 →
    # 旧 scan_text 拿数量列的 63 顶替（静默错价）。左右各留 ≥12px 余量：
    # 8 位价（`99,999,999`）居中在 x≈316 时墨迹 257..375。
    ("price", 0.415254, 0.881395, 0.245763, 0.058140),
    ("count", 0.6881, 0.5442, 0.0661, 0.0395),
)
# 稀有度单行（品阶「极品」在成色行上方，与成色行不同 y），并进拼条一起读
PANEL_EXTRA = (("rarity", 0.6712, 0.4465, 0.0847, 0.0360),)

# 拼条必须读到的块：名称 / 成色 / 价格 / 数量。
#
# 稀有度是**可选**补充块：卡片版式里品阶和成色在同一行（读在 grade 块里），
# 「无做工」面板干脆没有品阶行。把它设成必需会让这两类整帧回退（实测 444
# 帧里有 290 帧是卡片版式），而回退整幅反而更差 —— 整幅读不到 0 价时会
# 把倒计时 `剩余:9分37秒` 的 37 当价格。必需块缺一个就回退，这个颗粒度
# 足够挡住「丢一条还照常下单」。
PANEL_REQUIRED = ("name", "grade", "price", "count")
PANEL_FIELDS = PANEL_REQUIRED + ("rarity",)

_PANEL_PAD = 4       # 拼条四周留白（像素）
_PANEL_GAP_RATIO = 0.014   # 行间留白 = 面板高 × 该比例（860px → 12px）


def price_from_text(text):
    """闸门 2：千分位 / 位数格式校验。返回 (value, token)。

    语法只有一份，见 `core/money.py`（N11：这里以前是第二份实现，和
    `market.parse_row` / `cards._price_token_in` 各自漂移，`15 252` 在这
    里读 252、在那里读 15252）。三条硬规则：
      · 没有货币符号时，多段数字按千分位分组整体合并，分不了组就拒识
        （`15 252`→15252、`1 234 567`→1234567、`12 34`→None）
      · 空格只在"后组正好 3 位"时是千分位；逗号与空格混用拒识
      · 跨行不拼接（`¥12 345` 换行 `678` → 12345，不是 12345678）
    小数最多 2 位，三位小数按磨损/没读全拒识；负号、前导 0 拒识。
    """
    return money.scan_text(text)


def balance_from_text(text):
    """余额栏入口：只认**最右一组货币**，读不出就是 None。返回 (value|None, token)。

    `money.scan_text` 的 `rest[-1]` 是「最后一个数字」，不是「最后一组
    货币」：最右一组读成 `à`/空时它会静默退到中间那组青铜币（实测
    `2,537K 2,075 + à` → 2075）。余额是成交判定与误买止损的依据，读错
    比读不到更糟，所以单独走 `money.scan_balance`（带 `+` 标记才算货币
    组，右侧出现裸数字/残字一律拒识）。
    """
    return money.scan_balance(text)


def _join(words):
    """按行拼词：同一行的词用空格连，不同行用换行连。

    以前是无条件 `" ".join`：`¥12 345` 和下一行的 `678` 被拼成一行，
    价格解析就会跨行吞数字（N11 的 12345678）。行信息在 read_words 的
    每个词上都带着，不能在这一步丢掉。
    """
    if not words:
        return ""
    if not all(isinstance(w.get("y"), (int, float)) for w in words):
        return " ".join(str(w.get("text") or "") for w in words)
    ws = sorted(words, key=lambda w: (w["y"], w.get("x", 0)))
    lines, cur, cy = [], [], None
    for w in ws:
        c = w["y"] + (w.get("h") or 0) / 2.0
        if cy is not None and abs(c - cy) <= max(4.0, (w.get("h") or 0) * 0.6):
            cur.append(w)
            cy = (cy * (len(cur) - 1) + c) / len(cur)
        else:
            if cur:
                lines.append(cur)
            cur, cy = [w], c
    if cur:
        lines.append(cur)
    return "\n".join(" ".join(str(x.get("text") or "") for x in row)
                     for row in lines)


def _block_of(words, field):
    return [w for w in (words or []) if w.get("field") == field]


def price_from_words(words):
    """价格**只**从 `field=="price"` 的块取。返回 (value|None, token)。

    为什么必须有这一层：以前把整帧词拼成一串文本再交给 `money.scan_text`，
    价格末位被 ROI 切掉时它的 `rest[-1]` 会从**数量列**抓一个数（
    `1,000,00` 解析不过 → 拿同帧的 `63` 当价格下单，静默错价）。这里块内
    自洽，取不到就是 None，由调用方回退整幅 —— 绝不拿别的字段顶替。

    三条拒识：
      · 没有一个词带 field → 来源不明确，None
      · 没有 price 块 → None
      · price 块内有两个不同值 / 与数量块同值 → None（来源不可信）
    """
    tagged = [w for w in (words or []) if w.get("field")]
    if not tagged:
        return None, ""                 # 来源不明确：不许在这里猜
    band = _block_of(words, "price")
    if not band:
        return None, ""
    value, tok = money.scan_block(_join(band))
    if value is None:
        return None, tok
    # 静默错价复发守卫：价格与同帧数量同值 → 判为不可信。
    # 价格条只有它自己一个数，读到和数量一样只可能是「读串了」。
    for w in _block_of(words, "count"):
        cnt, _ = money.parse_line(str(w.get("text") or ""))
        if isinstance(cnt, int) and cnt == value:
            return None, "价格与数量同值（来源不可信）"
    return value, tok


def _accept(words, min_score=SCORE_MIN):
    """(value, token, score)；score 取「参与数字的那些词」的最低分。

    文本闸门走 `scan_block` 而不是 `scan_text`：价格条/价格块里只该有一个
    价，出现第二个数就只能拒识。`scan_text` 的 `rest[-1]` 会把同帧的数量、
    余额等数字当成价格（`1,000,00` 被切时抓数量列的 `63`），那条兜底现在
    只留给兼容用途，生产价格路径不许再经过它。
    """
    if not words:
        return None, "", 0.0
    txt = _join(words)
    value, tok = money.scan_block(txt)
    scored = [float(w.get("score", 1.0)) for w in words
              if any(ch.isdigit() for ch in (w.get("text") or ""))]
    score = min(scored) if scored else 0.0
    if value is None:
        return None, tok, score
    if score < min_score:
        return None, tok, score          # 闸门 1：分数不够，交下一层
    return value, tok, score


def upscale(img, factor):
    import cv2
    return cv2.resize(img, None, fx=factor, fy=factor,
                      interpolation=cv2.INTER_CUBIC)


def _brief(err, limit=48):
    """异常压成一行短句：自检/状态栏只有一行，原样塞进去会把别的信息挤掉。"""
    s = " ".join(str(err or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


class Backend:
    """对外与单引擎同形：available / name / read_words / read_text / read_price。"""

    supports_words = True

    def __init__(self, settings=None):
        cfg = dict(settings or {})
        self.rapid = ocr_rapid.make(cfg.get("rapid") or {})
        self.pp = ocr_ppocr.make(cfg.get("ppocr") or {})
        self.tpl = digit_ocr
        self.stats = {"calls": 0, "empty": 0, "tier2": 0, "tier2_fail": 0,
                      "tier3": 0, "ms": 0.0, "fails": 0}
        self._pp_err = ""
        self.url = getattr(self.pp, "url", "") or ""

    # ---------- 自检 ----------

    def available(self):
        """Rapid 能用就算能用；Rapid 崩了还有 PP-OCR 服务。

        自建字模**不**让 available() 变真：它只认数字，读不了品阶/倒计时/
        弹窗，拿它通过启动自检等于带着半只眼睛去抢。
        """
        if self.rapid.available():
            return True
        return bool(self.pp.available())

    def selfcheck_pp(self, probe_timeout=0.3):
        """启动自检：二级到底能不能推理。结论写进 _pp_err，自检/状态立即可见。

        返回 (ok, error)。不做决定、不关级：是否降级由每次调用的计数和熔断
        负责，这里只负责「让界面先说真话」。

        不再先走 `pp.available()`（那是 1.5s 探活）；探活预算直接压到
        probe_timeout（默认 0.3s），引擎侧再把整段自检放到后台线程，
        启动不再替二级等这 1.5 秒。
        """
        fn = getattr(self.pp, "selfcheck", None)
        if not callable(fn):
            ok = self.pp.available()
            self._pp_err = "" if ok else "二级服务未起"
            return ok, self._pp_err
        try:
            ok, err = fn(probe_timeout=probe_timeout)
        except Exception as e:                       # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        self._pp_err = "" if ok else (err or "二级推理失败")
        return ok, self._pp_err

    def name(self):
        bits = []
        bits.append("RapidOCR" if self.rapid.available()
                    else "RapidOCR 不可用")
        if self.pp.available():
            fails = int(self.stats.get("tier2_fail") or 0)
            if self._pp_err and fails:
                # 服务在听、但每次推理都 500（本机 Paddle+oneDNN 就是这样）：
                # 只说模型名会让自检看起来一切正常，中间这层其实是死的。
                bits.append(f"二级推理失败 {fails} 次（{_brief(self._pp_err)}）")
            elif self._pp_err:
                # 启动自检失败，还没产生真实调用：别写成「失败 0 次」。
                bits.append(f"二级不可用（{_brief(self._pp_err)}）")
            else:
                model = str(self.pp.cfg.get("model") or "PP-OCRv6")
                bits.append(f"{model}（HTTP {self.url}/ocr）" if self.url else model)
        else:
            # available() 为假有三种原因，别都写成「未起」：服务确实没起、
            # 配置关了、或者服务在听但连续 500 被熔断（本机 Paddle+oneDNN）。
            pe = getattr(self.pp, "last_error", "") or ""
            mode = str(getattr(self.pp, "mode", "") or "")
            if mode in ("off", "none"):
                bits.append("二级已关闭（ocr.ppocr.mode=off）")
            elif "长时间停用" in pe:
                bits.append(f"二级已熔断（{_brief(pe)}）")
            else:
                bits.append("二级服务未起（价格二次确认自动跳过）")
        bits.append("字模兜底")
        return " → ".join(bits)

    @property
    def last_error(self):
        """PP-OCR 客户端最后一次错误（熔断/长时间停用的原因就写在这）。

        熔断信息优先：它解释「为什么这一级整段不可用」，比最后一次 500 的
        堆栈首行更值得放给用户看。
        """
        pe = getattr(self.pp, "last_error", "") or ""
        if "长时间停用" in pe:
            return pe
        return self._pp_err or pe

    def recent_ms(self):
        return self.rapid.recent_ms() if hasattr(self.rapid, "recent_ms") else 0.0

    def stats_line(self):
        s = self.stats
        pp = ""
        fn = getattr(self.pp, "stats_line", None)
        if callable(fn):
            try:
                pp = f" | {fn()}"
            except Exception:                        # noqa: BLE001
                pp = ""
        return (f"chain calls={s['calls']} empty={s['empty']} "
                f"tier2={s['tier2']} tier2_fail={s.get('tier2_fail', 0)} "
                f"tier3={s['tier3']} | "
                f"{self.rapid.stats_line()}{pp}")

    def _pp_words(self, img, timeout=None):
        """二级服务的唯一入口：失败只计数 + 记错误，返回 None 表示「继续降级」。

        探活只看 TCP 端口通不通，分辨不出「服务在听、每次推理都 500」——
        本机 Paddle 3.x + oneDNN 的 CPU 分支就是这样。以前普通文本路径把
        `pp.read_words` 直接写在 try 外，异常一路冒到 ocr.read_words 的边界
        被吞成空串：结果是「二级死了」和「图上真的没字」在日志里长得一样。
        """
        self.stats["tier2"] += 1
        try:
            words = self.pp.read_words(img, timeout=timeout) or []
        except Exception as e:                       # noqa: BLE001
            self.stats["tier2_fail"] = self.stats.get("tier2_fail", 0) + 1
            self.stats["fails"] += 1
            self._pp_err = f"{type(e).__name__}: {e}"
            return None
        self._pp_err = ""
        return words

    # ---------- 普通文本 ----------

    def read_words(self, img, timeout=None):
        """Rapid 先读；报错或**一个字都没读到**才落到 PP-OCR 服务。

        为什么空结果也降级：中文品阶/成色没有做过准确率基准，Rapid 在中文
        小字上漏读的风险高于数字。服务在跑时补问一次（60ms 级），服务没起
        就直接返回空，不为了「可能没有字」去等一次 5 秒超时。

        二级失败时**保留一级结果**（Rapid 报错才继续往上抛）：二级是复核，
        不是唯一来源，它死了不该把一级读到的字一起丢掉。
        """
        if img is None or getattr(img, "size", 0) == 0:
            return []
        import time
        t0 = time.perf_counter()
        try:
            try:
                words = self.rapid.read_words(img, timeout=timeout)
            except Exception:                   # noqa: BLE001
                self.stats["fails"] += 1
                if not self.pp.available():
                    raise
                got = self._pp_words(img, timeout=timeout)
                if got is None:
                    raise                       # 一级一级都瞎了，交给上层报 OCRError
                words = got
            if not words and self.pp.available():
                got = self._pp_words(img, timeout=timeout)
                if got is not None:
                    words = got
            return words
        finally:
            self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
            if "words" in locals() and words:
                self.stats["calls"] += 1
            else:
                self.stats["empty"] += 1

    def read_text(self, img, timeout=None):
        return "\n".join(w["text"] for w in self.read_words(img, timeout=timeout))

    # ---------- 详情面板：只读必要四块 ----------

    def read_panel(self, img, timeout=None, rois=None):
        """详情面板整帧 → 四块必要 ROI 拼条一次读完，返回面板坐标的 words。

        为什么拼条而不是分四次读：RapidOCR 每次调用的固定开销（预处理 +
        session 启动）在 20ms 级，四块分开就是四份；拼成一条竖条只付一次
        det，rec 仍是同样的几个文本行。实测 590×860 面板 222.8ms →
        89.6ms（p50），读数与原整屏一致（名称/品阶/成色/价格/数量）。

        坐标还原：拼条不缩放，只平移；每个 box 按中心落进哪一条 crop 就加
        回那条的 (x0, y0)。这样上层拿到的坐标仍是面板像素坐标，与
        `read_words(整帧)` 同口径。
        """
        import numpy as np

        if img is None or getattr(img, "size", 0) == 0:
            return []
        h, w = img.shape[:2]
        table = tuple(rois or ()) or (PANEL_ROIS + PANEL_EXTRA)
        bands = []
        for name, nx, ny, nw, nh in table:
            x0 = max(0, int(round(nx * w)))
            y0 = max(0, int(round(ny * h)))
            x1 = min(w, int(round((nx + nw) * w)))
            y1 = min(h, int(round((ny + nh) * h)))
            if x1 <= x0 or y1 <= y0:
                continue
            bands.append((name, x0, y0, x1, y1))
        if not bands:
            return []

        # 拼条：宽度取最宽一块，逐行向下堆，行间留 gap 防止 det 把两行粘成一行
        gap = max(6, int(round(h * _PANEL_GAP_RATIO)))
        pad = _PANEL_PAD
        cw = max(b[3] - b[1] for b in bands)
        ch = sum(b[4] - b[2] for b in bands) + gap * (len(bands) - 1) + pad * 2
        canvas = np.zeros((ch, cw + pad * 2, 3), np.uint8)
        placed = []
        y = pad
        for name, x0, y0, x1, y1 in bands:
            crop = img[y0:y1, x0:x1]
            canvas[y:y + crop.shape[0], pad:pad + crop.shape[1]] = crop
            placed.append((name, x0, y0, pad, y))
            y += crop.shape[0] + gap

        words = self.read_words(canvas, timeout=timeout)
        out = []
        for wd in words:
            cy = wd["y"] + wd["h"] / 2.0
            # 落在哪一条 crop 的纵向区间里（拼条里每条 = [top, top+高)）
            for i, (name, x0, y0, px, py) in enumerate(placed):
                top = py
                bot = py + (bands[i][4] - bands[i][2])
                if top <= cy < bot:
                    out.append({**wd,
                                "x": int(wd["x"] - px + x0),
                                "y": int(wd["y"] - py + y0),
                                "field": name})
                    break
        out.sort(key=lambda t: (t["y"], t["x"]))
        return out

    # ---------- 价格（两道闸门 + 三级降级） ----------

    def read_price(self, img, ban=None, timeout=None):
        """读价格。读不到 / 不合格一律返回 None，绝不返回猜的值。

        ban：出现这些词就认为这块不是价格（详情按钮上印倒计时）。
        """
        if img is None or getattr(img, "size", 0) == 0:
            return None
        import time
        t0 = time.perf_counter()
        self.stats["calls"] += 1
        try:
            value = self._price_tiers(img, ban, timeout)
        finally:
            self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
        if value is None:
            self.stats["empty"] += 1
        return value

    def _price_tiers(self, img, ban, timeout):
        # 一级：RapidOCR，原分辨率
        words = []
        try:
            words = self.rapid.read_words(img, timeout=timeout)
        except Exception:                       # noqa: BLE001
            self.stats["fails"] += 1
        txt = _join(words)
        if ban and any(b in txt for b in ban):
            return None                         # 详情按钮上印的是倒计时，不是价格
        value, _tok, _score = _accept(words)
        if value is not None:
            return value

        # 二级：Rapid 分数不足 / 格式没过 → 换一条管线，固定 3× 放大
        if self.pp.available():
            words2 = self._pp_words(upscale(img, 3), timeout=timeout)
            if words2:
                txt2 = _join(words2)
                if ban and any(b in txt2 for b in ban):
                    return None
                value2, _tok2, _s2 = _accept(words2)
                if value2 is not None:
                    return value2

        # 三级：自建字模（纯数字、4ms），margin 闸门不过就拒识
        self.stats["tier3"] += 1
        try:
            value3, _tok3, _s3 = self.tpl.read_detail(img)
        except Exception:                       # noqa: BLE001
            value3 = None
        return value3 if value3 is not None else None


def make(settings=None):
    return Backend(settings)
