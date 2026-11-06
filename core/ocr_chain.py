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
import re

from . import digit_ocr, ocr_ppocr, ocr_rapid

SCORE_MIN = 0.5
MIN_DIGITS = 1
MAX_DIGITS = 9          # 交易行价格上限 99,999,999；再长按「没读全」处理

_PRICE_TOKEN = re.compile(r"\d+(?:,\d+)*")
_GROUPED = re.compile(r"^\d{1,3}(?:,\d{3})+$")


def price_from_text(text):
    """闸门 2：千分位 / 位数格式校验。

    返回 (value, token)；不合格返回 (None, token)，token 只用于日志。
    规则（与 `_ocr_engine_selection.md` 第五节一致）：

      · 取**最后一个**数字串（价格条印一个数，货币栏三角币在最右）
      · 带逗号：必须严格 3 位一组（`9,9`、`1,00,000`、`1,000,` 全部拒）
      · 不带逗号：1~9 位
      · 多位数字不许前导 0（`025` 多是金币图标/碎块被粘进价格）
    """
    t = (text or "").replace("，", ",")
    matches = list(_PRICE_TOKEN.finditer(t))
    if not matches:
        return None, ""
    m = matches[-1]
    tok = m.group(0)
    # 逗号悬空（`1,000,` 被检测框截断）—— 后面的逗号说明这串没印完
    if m.end() < len(t) and t[m.end()] == ",":
        return None, tok
    if "," in tok:
        if not _GROUPED.match(tok):
            return None, tok
        digits = tok.replace(",", "")
    else:
        digits = tok
    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        return None, tok
    if len(digits) > 1 and digits[0] == "0":
        return None, tok
    return int(digits), tok


def _join(words):
    return " ".join(str(w.get("text") or "") for w in words)


def _accept(words, min_score=SCORE_MIN):
    """(value, token, score)；score 取「参与数字的那些词」的最低分。"""
    if not words:
        return None, "", 0.0
    txt = _join(words)
    value, tok = price_from_text(txt)
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


class Backend:
    """对外与单引擎同形：available / name / read_words / read_text / read_price。"""

    supports_words = True

    def __init__(self, settings=None):
        cfg = dict(settings or {})
        self.rapid = ocr_rapid.make(cfg.get("rapid") or {})
        self.pp = ocr_ppocr.make(cfg.get("ppocr") or {})
        self.tpl = digit_ocr
        self.stats = {"calls": 0, "empty": 0, "tier2": 0, "tier3": 0,
                      "ms": 0.0, "fails": 0}
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

    def name(self):
        bits = []
        bits.append("RapidOCR" if self.rapid.available()
                    else "RapidOCR 不可用")
        if self.pp.available():
            bits.append(str(self.pp.cfg.get("model") or "PP-OCRv6"))
        else:
            bits.append("二级服务未起（价格二次确认自动跳过）")
        bits.append("字模兜底")
        return " → ".join(bits)

    @property
    def last_error(self):
        """PP-OCR 客户端最后一次错误（熔断/长时间停用的原因就写在这）。"""
        return getattr(self.pp, "last_error", "")

    def recent_ms(self):
        return self.rapid.recent_ms() if hasattr(self.rapid, "recent_ms") else 0.0

    def stats_line(self):
        s = self.stats
        return (f"chain calls={s['calls']} empty={s['empty']} "
                f"tier2={s['tier2']} tier3={s['tier3']} | "
                f"{self.rapid.stats_line()}")

    # ---------- 普通文本 ----------

    def read_words(self, img, timeout=None):
        """Rapid 先读；报错或**一个字都没读到**才落到 PP-OCR 服务。

        为什么空结果也降级：中文品阶/成色没有做过准确率基准，Rapid 在中文
        小字上漏读的风险高于数字。服务在跑时补问一次（60ms 级），服务没起
        就直接返回空，不为了「可能没有字」去等一次 5 秒超时。
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
                if self.pp.available():
                    words = self.pp.read_words(img, timeout=timeout) or []
                else:
                    raise
            if not words and self.pp.available():
                words = self.pp.read_words(img, timeout=timeout) or []
            return words
        finally:
            self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
            if "words" in locals() and words:
                self.stats["calls"] += 1
            else:
                self.stats["empty"] += 1

    def read_text(self, img, timeout=None):
        return "\n".join(w["text"] for w in self.read_words(img, timeout=timeout))

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
            self.stats["tier2"] += 1
            try:
                big = upscale(img, 3)
                words2 = self.pp.read_words(big, timeout=timeout)
                txt2 = _join(words2)
                if ban and any(b in txt2 for b in ban):
                    return None
                value2, _tok2, _s2 = _accept(words2)
            except Exception:                   # noqa: BLE001
                value2 = None
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
