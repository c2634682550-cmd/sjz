"""入池 / 抽签证据：从游戏 toast 文案解析，落到台账行尾的 8 列事实 + 2 列辅助。

为什么单独一个模块：解析规则、缺失口径、时间反推公式都是「事实层」的东西，
放在引擎里会被点击/校验逻辑淹掉。引擎只做两件事：
  · 把顶部 toast 的 OCR 文本交给 `observe_toast()`；
  · 把第一次读到倒计时的时刻交给 `observe_clock()`（带二次读数）。
剩下的规范化、容错、null 口径全在这里，测试也只测这里。

四条硬口径（复核要求，别改）：
  1. 识别不到一律 `null` / `unknown`，**绝不写 0 冒充**；单边点数也不许推结论；
     `self` 与 `max` **字段级失效**：各自能读到可信数值就保留；两边都读到但
     `self > max` 或 max 超上界只是互相矛盾 —— 保留两个原值、`join_conflict=true`、
     `won`/`won_source` 置 null，绝不把可信的 `self` 一起抹掉；
  2. `listed_at` 只在读到 clock 倒计时且剩余秒 > 0 时才反推，并且必须做一次
     二次读数交叉校验（倒计时路线与列表直达路线都要；残影 / 跳变一律 null，
     不静默按正常值记账）；
  3. `matched` 只决定要不要发 evo 旁路，**不拦证据写入**；
  4. 记账开销 < 5ms/件：这里只有几次正则，没有 I/O。探针（OCR）另计：
     本地 RapidOCR 中位 215.5ms（181–247），PP-OCR 服务在线 805–1218ms，
     后端冷启动 3229ms；探针耗时由引擎从 elapsed 扣掉，但单次判定仍可能
     多出最多约 1.2s 的墙钟时间。
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata

JOINED = "joined"
QUEUE_FULL = "queue_full"
UNKNOWN = "unknown"

# evo 采集器的现成旁路通道：它已经在读 `evo/collect/inbox/*.jsonl`，
# 引擎把同一段 toast 规范形态追加进去，消费方不用改一行代码就能拿到入池/抽签事实。
INBOX_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "evo", "collect", "inbox")

# 台账/事件里允许出现的证据键（8 个事实 + 误差 + 矛盾标记）。
EVIDENCE_KEYS = (
    "pool_joined_at", "join_result", "roll_self", "roll_max", "won",
    "listed_at", "publicity_s",
    "listed_at_err_s", "join_conflict", "won_source",
)

# ---------------------------------------------------------------- 规范化
#
# 真机 OCR 会混进：全角数字（９９）、全角冒号/逗号、零宽字符（ZWSP/ZWNJ/ZWJ/
# WORD JOINER/BOM）、换行、千分位（1,250）。
#
# 千分位只在原始文本上、且只认 ASCII 逗号剥（NFKC 会把全角逗号也折成 ASCII，
# 之后再剥会把「随机点数 99，125」粘成 99125）。全角逗号一律按语义分隔处理。
_ZW = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u180e]")
_WS = re.compile(r"[ \t\r\n\u00a0\u3000]+")
# 只认**原始文本**里的 ASCII 逗号：NFKC 会把全角逗号（，）也折成 ASCII 逗号，
# 之后再剥就会把「随机点数 99，125」粘成 99125（复核第二轮第 1 条）。
# 前后都必须是数字、逗号后正好三位且后面不再接数字。
_RE_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def normalize(text):
    """把 toast 文本折成便于正则匹配的一行；空输入返回空串。"""
    raw = _ZW.sub("", text or "")
    raw = _RE_THOUSANDS.sub("", raw)          # 必须在 NFKC 之前、只认 ASCII 逗号
    s = unicodedata.normalize("NFKC", raw)
    return _WS.sub(" ", s).strip()


# ---------------------------------------------------------------- 数字读取
#
# OCR 会把一个数拆成多段（「9 9」/「1 2 5」/「12 5」/「1 25」）。只在「点数」
# 这类关键词后面做拼接：把紧邻的、空白分隔的数字段接起来，但
#   · 整串（≥2 段）后面跟「人/名/位/参与」——那是参人数，整串一个都不接；
#   · 自己的点数出现 ≥3 段且无「人」这类锚点时按歧义写 null（复核第 5 条）；
#   · 总位数超过 7 位 → 视为 OCR 噪声。
# 最大/最高点数一侧允许三段（「本轮最大点数 1 2 5」是复核 PASS 用例）。

_MAX_VALUE = 10_000_000        # 超过这个数当 OCR 噪声，写 null
_MAX_ROLL = 1_000_000          # max 上界（点数不可能超过这个量级）
_MAX_DIGITS = 7

# 关键词与数字之间的连接词：达到 / 为 / 是 / 共 / 有 / 即 / 等于 …
_LEAD = re.compile(r"^[\s:：,，;；\-—=＝为是共达到有即等于记作]*")


def _sane(v):
    return v if 0 < v <= _MAX_VALUE else None


def _number_at(s, pos, window=64, allow_many=False):
    """从 pos 起读一个数；允许前导连接词，允许空白分隔的拆字段拼接。

    allow_many=False 时，「自己的点数」若由 ≥3 段拼成，判为歧义返回 None
    （「随机点数 1 2 3」可能是 123 / 1 23 / 12 3，不猜）。
    """
    tail = _LEAD.sub("", s[pos:pos + window])
    m = re.match(r"\d+", tail)
    if not m:
        return None
    segs = [m.group(0)]
    end = m.end()
    while True:
        m2 = re.match(r"[ \t]+(\d{1,3})(?![0-9])", tail[end:])
        if not m2:
            break
        segs.append(m2.group(1))
        end += m2.end()
        if len(segs) > 6:
            break
    # 复核第三轮 D5：整串之后出现「人/名/位/参与」，这串数字全是参与人数，
    # 一个都不能并进点数（`1 2 3 人参与` 不是 12，`1 2 5 人参与` 不是 12）。
    # 单段（`1,250 人`）除外：那是千分位还原后的一个完整数字。
    if len(segs) >= 2 and re.match(r"\s*(?:人|名|位|参与)", tail[end:]):
        return None
    if len(segs) > 1 and not allow_many and len(segs) >= 3:
        return None
    digits = "".join(segs)
    if len(digits) > _MAX_DIGITS:
        return None
    try:
        return _sane(int(digits))
    except (TypeError, ValueError):
        return None



_RE_JOIN = re.compile(
    r"已加入抢购池|已进入抢购池|加入抢购池成功|已加入抢购")
_RE_FULL = re.compile(
    r"排队已满|排队人数已满|队列已满|抢购池已满|抢购池已满员|抢购池满|人数过多")
# 「最大点数 / 最高点数」整段先挖掉，再找自己的点数 —— 否则
# 「本轮最大\n点数 125」会被 (?<!最大) 之外的空白绕过，误读成 self=125。
_RE_MAX_PHRASE = re.compile(
    r"(?:本轮|本次|当前|这轮)?\s*(?:最大|最高)\s*点数")
# 变体「本轮最大值 125」（复核第三轮 D3）：必须带轮次前缀，避免把泛化的
# 「最大值 125」这类非点数语境当成本轮最大点数。
_RE_MAX_VALUE = re.compile(
    r"(?:本轮|本次|当前|这轮)\s*(?:最大|最高)\s*值")
_RE_MAX_SHORT = re.compile(
    r"(?:本轮|本次)\s*(?:最大|最高)\s*(?:为|是)?")
# 从「自己的点数」里整段挖掉的最大点数表达（三种变体，挖成字段边界）。
_RE_MAX_ALL = re.compile(
    r"(?:本轮|本次|当前|这轮)?\s*(?:最大|最高)\s*点数"
    r"|(?:本轮|本次|当前|这轮)\s*(?:最大|最高)\s*值"
    r"|(?:本轮|本次)\s*(?:最大|最高)\s*(?:为|是)?")
_RE_SELF_PHRASE = re.compile(r"(?:随机)?点数")
_RE_NEG_WIN = re.compile(
    r"(?:没能|没有|并未|未能|未|没|不曾|并未能)\s*(?:抢购|抽签|中签|购买)?\s*成功")
_RE_LOSE = re.compile(
    r"未抢购成功|未能抢购成功|没能抢购成功|抢购失败|抽签失败|未中签|没有中签"
    r"|并未中签|没能中签|未能中签|未抢到|没能抢到|没有抢到|很遗憾|运气不佳")
# 名词短语「抢购成功率 12%」不是结果句：成功后面不许接率/概率/次数/记录。
_RE_WIN = re.compile(
    r"(?:抢购|抽签|中签)\s*成功(?!率|概率|次数|记录|的)"
    r"|恭喜(?:您|你)?(?:中签|抢到|抢购成功)|已中签|中签了")


def _blank():
    return {
        "join_result": UNKNOWN,
        "roll_self": None,
        "roll_max": None,
        "won": None,
        "won_source": None,
        "join_conflict": False,
        "matched": False,
    }


def parse_toast(text):
    """一段 toast → 入池/抽签事实；识别不到的分项写 None / unknown。

    返回 dict（不写库，纯函数）：
      join_result   joined / queue_full / unknown
      roll_self     本次点数（int / None）
      roll_max      本轮最大点数（int / None）
      won           True / False / None（等值、单边、未命中、点数互相矛盾一律 None）
      won_source    text / points / None（结论来源；won 为 None 时必为 None）
      join_conflict 文案结论与点数矛盾 / 只有单边点数 / self>max / max 超上界 → True
      matched       是否值得发 evo 旁路（只影响旁路，不拦证据写入）
    """
    s = normalize(text)
    out = _blank()
    if not s:
        return out

    join_found = False
    if _RE_FULL.search(s):
        out["join_result"] = QUEUE_FULL
        join_found = True
    elif _RE_JOIN.search(s):
        out["join_result"] = JOINED
        join_found = True

    # ---- 自己的点数：先把「最大/最高点数 / 本轮最大值」整段挖成字段边界 ----
    # 挖成 `|` 而不是空格：`随机点数 99本轮最大点数 125` 这种无标点粘连换成
    # 空格后，`_number_at` 会把 125 当成 99 的下一段拼成 99125（复核 D4）。
    s_self = _RE_MAX_ALL.sub("|", s)
    m_self = _RE_SELF_PHRASE.search(s_self)
    self_ = _number_at(s_self, m_self.end()) if m_self else None

    # ---- 本轮最大点数：最大 / 最高等价；也认「本轮最大值 125」变体 ----
    m_max = _RE_MAX_PHRASE.search(s) or _RE_MAX_VALUE.search(s)
    maximum = _number_at(s, m_max.end(), allow_many=True) if m_max else None
    if maximum is None:
        m_bare = _RE_MAX_SHORT.search(s)
        if m_bare:
            maximum = _number_at(s, m_bare.end(), allow_many=True)

    # ---- 自洽校验（复核第三轮 D1/D2）：字段级失效，不做整组清零 ----
    # self 与 max 各自独立赋值：某一边读到可信数值就保留。两边都读到但
    # self>max 或 max 超上界，只说明这两条事实互相矛盾 —— 保留原值、标
    # conflict、结论置 null；可信的 self 绝不能被 max 的问题连带抹掉
    # （下游算入池率时缺 self 比缺 max 更致命）。
    inconsistent = False
    if maximum is not None and maximum > _MAX_ROLL:
        inconsistent = True
    if self_ is not None and maximum is not None and self_ > maximum:
        inconsistent = True

    out["roll_self"] = self_
    out["roll_max"] = maximum

    # ---- 结论：失败先于成功；否定词先摘掉再找成功 ----
    text_won = None
    if _RE_NEG_WIN.search(s) or _RE_LOSE.search(s):
        text_won = False
    else:
        if _RE_WIN.search(_RE_NEG_WIN.sub(" ", s)):
            text_won = True

    # ---- 点数推结论：只有双方都在、且没被标成互相矛盾时才推；等值/单边一律 None ----
    point_won = None
    if not inconsistent and self_ is not None and maximum is not None:
        if self_ > maximum:
            point_won = True
        elif self_ < maximum:
            point_won = False

    # 矛盾组合/超上界时不落结论：点数已经不可信，文案也不再兜底（D1 口径）。
    if inconsistent:
        out["won"] = None
        out["won_source"] = None
    else:
        out["won"] = text_won if text_won is not None else point_won
        # 结果来源（复核第 6 条）：下游要能分清「文案说的」和「点数推的」。
        if text_won is not None:
            out["won_source"] = "text"
        elif point_won is not None:
            out["won_source"] = "points"
        else:
            out["won_source"] = None

    # ---- 矛盾标记：文案与点数打架、只有单边点数、或自洽校验不通过 ----
    conflict = inconsistent
    if text_won is not None and point_won is not None and text_won != point_won:
        conflict = True
    if (self_ is None) != (maximum is None):
        conflict = True
    out["join_conflict"] = conflict

    # 旁路只在「有入池事实 / 有结论 / 能由双方点数推出结论」时发；
    # 单边点数照旧落台账，但不往 inbox 灌无结论的半截话。
    out["matched"] = bool(join_found or text_won is not None or point_won is not None)
    return out


# ---------------------------------------------------------------- 时间反推
#
# 「挂牌时刻」用「首次见到该商品的时刻 − 显示剩余秒」反推（与
# `_bypass_collect.md` §3 的旁路口径一致）。三个误差源：
#   · 帧抓取 → OCR 完成之间的扫描延迟；
#   · 倒计时显示向下取整（每次读数最多 1 秒偏差）；
#   · 二次读数之间的实测偏差（残影 / 跳变，超过容差直接判无效）。
# 所以明确写 ±秒，不把推算值当成精确值。

_GHOST_TOL_S = 1.5             # 两次读数允许的最大偏差（1s 截断 + 抖动）


def derive_listed(first_seen_ms, remaining_s, scan_lag_s=0.0, second=None):
    """(listed_at, listed_at_err_s)；输入不全/剩余<=0/残影 时返回 (None, None)。

    `second` = (第二次读到的毫秒, 第二次剩余秒, 第二次 OCR 延迟)。
    给了就做交叉校验：两次之间剩余秒的下降应该≈墙钟差；偏差超过
    `_GHOST_TOL_S` 说明第一帧可能是残影，宁可写 null 也不按正常值记账。
    """
    if first_seen_ms is None or remaining_s is None:
        return None, None
    try:
        seen = float(first_seen_ms)
        rem = float(remaining_s)
        lag = float(scan_lag_s or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None, None
    if rem <= 0:
        # 「已解锁 / 剩余：X天」这类读数不是解锁倒计时，反推没有意义。
        return None, None

    err = max(0.0, lag) + 1.0

    if second:
        try:
            seen2, rem2, lag2 = second
            seen2 = float(seen2)
            rem2 = float(rem2)
            lag2 = float(lag2 or 0.0)
        except (TypeError, ValueError, OverflowError):
            seen2 = rem2 = None
        if rem2 is not None and rem2 >= 0:
            dt = (seen2 - seen) / 1000.0
            if dt < 0:
                return None, None
            dev = abs((rem - rem2) - dt)
            if dev > _GHOST_TOL_S:
                return None, None          # 残影 / 跳变：不落正常值
            err = max(max(0.0, lag), max(0.0, lag2)) + 1.0 + round(dev, 3)

    listed = int(round(seen - rem * 1000.0))
    return listed, round(err, 3)


# ---------------------------------------------------------------- 证据容器

class PoolTrace:
    """一次「尝试」的入池/抽签证据。一次尝试 = 一件商品从进场到记账。

    线程安全不在这里做：引擎本来就是单线程抢购，`records.add` 自己带锁。
    """

    def __init__(self, inbox_dir=None):
        self._inbox_dir = inbox_dir
        self.begin()

    @staticmethod
    def _blank():
        return {
            "pool_joined_at": None,
            "join_result": UNKNOWN,
            "roll_self": None,
            "roll_max": None,
            "won": None,
            "listed_at": None,
            "publicity_s": None,
            "listed_at_err_s": None,
            "join_conflict": False,
            "won_source": None,
        }

    def begin(self):
        """开始新的一次尝试：清空上一件的证据，缺失口径回到 null/unknown。"""
        self._ev = self._blank()
        self._clock_done = False
        self._toast_seen = False

    def observe_clock(self, first_seen_ms, remaining_s, scan_lag_s=0.0, second=None):
        """第一次读到该商品的解锁倒计时时调用；重复调用只认第一次。"""
        if self._clock_done:
            return
        listed, err = derive_listed(first_seen_ms, remaining_s, scan_lag_s, second)
        if listed is None:
            return
        self._ev["listed_at"] = listed
        self._ev["listed_at_err_s"] = err
        # 公示总时长不可见（只有「剩余」这一侧），没有稳定常量就不硬猜。
        self._ev["publicity_s"] = None
        self._clock_done = True

    def has_toast(self):
        """本次尝试是否已经读到有效 toast（引擎据此停掉后续重复探针）。"""
        return self._toast_seen

    def observe_toast(self, text, at_ms=None, name="", source="toast_ocr"):
        """收一段 toast OCR 文本；返回解析结果（便于日志/自测）。

        `matched` 只决定要不要发旁路：解析出的非空事实一律落账，
        单边点数、未命中文案都不会被丢掉。
        """
        parsed = parse_toast(text)
        fact = (parsed["join_result"] != UNKNOWN
                or parsed["roll_self"] is not None
                or parsed["roll_max"] is not None
                or parsed["won"] is not None)

        if parsed["join_result"] == QUEUE_FULL:
            # 已进池之后不会再收到「排队已满」；以先到的为准，不把 joined 降级。
            if self._ev["join_result"] != JOINED:
                self._ev["join_result"] = QUEUE_FULL
        elif parsed["join_result"] == JOINED:
            self._ev["join_result"] = JOINED
            if self._ev["pool_joined_at"] is None and at_ms is not None:
                try:
                    self._ev["pool_joined_at"] = int(round(float(at_ms)))
                except (TypeError, ValueError, OverflowError):
                    self._ev["pool_joined_at"] = None

        if parsed["roll_self"] is not None:
            self._ev["roll_self"] = parsed["roll_self"]
        if parsed["roll_max"] is not None:
            self._ev["roll_max"] = parsed["roll_max"]
        if parsed["won"] is not None:
            self._ev["won"] = bool(parsed["won"])
        if parsed.get("won_source"):
            self._ev["won_source"] = parsed["won_source"]
        if parsed["join_conflict"]:
            self._ev["join_conflict"] = True

        if not fact:
            return parsed

        self._toast_seen = True
        if parsed["matched"]:
            self._emit(text, parsed, at_ms=at_ms, name=name, source=source)
        return parsed

    def evidence(self):
        """台账行尾要追加的 10 个键（8 个事实 + 误差 + 矛盾标记 + 结果来源）。"""
        ev = {k: self._ev.get(k) for k in EVIDENCE_KEYS}
        # 只落了单边点数时，即使解析那一步没标矛盾，这里也补上。
        if (ev["roll_self"] is not None) != (ev["roll_max"] is not None):
            ev["join_conflict"] = True
        ev["join_conflict"] = bool(ev["join_conflict"])
        return ev

    # ------------------------------------------------------------ 旁路
    def _emit(self, text, parsed, at_ms=None, name="", source="toast_ocr"):
        """把识别到的 toast 追加到 evo 现成的 inbox 通道。

        消费方（evo/collect/collector.py → parse_toast_line）已经在读这个目录，
        **不改它的代码**就能拿到 roll/won。写失败一律吞掉：旁路不是记账，
        不允许因为它打断抢购。
        """
        base = self._inbox_dir or INBOX_DIR
        if not base:
            return
        try:
            os.makedirs(base, exist_ok=True)
            now = float(at_ms) / 1000.0 if at_ms else time.time()
            # 给 evo 的文本用它的正则认得的规范形态（全角已折半角）；
            # 原文完整留在 raw 里，排障时还能看到 OCR 到底读成了什么。
            row = {
                "ts": round(now, 3),
                "t": time.strftime("%m-%d %H:%M:%S", time.localtime(now)),
                "text": self._canonical(parsed, text),
                "raw": str(text or ""),
                "name": str(name or ""),
                "source": source,
            }
            path = os.path.join(base, time.strftime("%Y%m%d", time.localtime(now)) + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
        except Exception:                       # noqa: BLE001 旁路失败不影响抢购
            pass

    @staticmethod
    def _canonical(parsed, text):
        """规范成 evo 现有正则认得的句子（不行才退回 normalize 原文）。"""
        parts = []
        if parsed["join_result"] == JOINED:
            parts.append("已加入抢购池")
        elif parsed["join_result"] == QUEUE_FULL:
            parts.append("排队已满")
        if parsed["roll_self"] is not None:
            parts.append(f"随机点数 {parsed['roll_self']}")
        if parsed["roll_max"] is not None:
            parts.append(f"本轮最大点数 {parsed['roll_max']}")
        if parsed["won"] is True:
            parts.append("抢购成功")
        elif parsed["won"] is False:
            parts.append("未抢购成功")
        if parts:
            return "，".join(parts)
        return normalize(text)
