"""按页面特征认定「枪皮（典藏外观）」与「挂单列表」，不按坐标、不按页签名。

真机取证（050404 帧）：
  · 典藏外观页正文 = 武器名列表（`K437突击步枪-王者之剑` 等）+ 单价 + 购买键
  · 该页**不写磨损**；磨损只出现在挂单列表 / 在售详情里
所以分两层判：

  weapon_list  —— 枪皮列表：正文有「型号+品类-皮肤名」的武器名，且正文
                  不出现挂饰/近战/表情/喷漆/名片/动作
  listing_list —— 挂单列表：在 weapon_list 基础上再要求「磨损字样 + 可解析单价」

顶部标签栏（y<110）的文字不参与拒绝判定：`典藏挂饰` 这个页签名字本身
就含「挂饰」，按整屏文本判会把枪皮页误杀（050404 实测踩过）。
"""
import re

# 磨损等价字段：枪皮挂单页不写「磨损」，写的是「成色 S/A/B/C」（真机 050506 帧）。
# 两者统一按 WEAR_WORDS 处理，代码和日志不再按「磨损」两个字单独找。
WEAR_WORDS = ("磨损", "磨损度", "成色", "崭新出厂", "略有磨损", "久经沙场",
              "破损不堪")
GRADE_RE = re.compile(r"成色\s*([SABC])")
WEAPON_KINDS = ("突击步枪", "冲锋枪", "狙击步枪", "霰弹枪", "轻机枪",
                "精确射手步枪", "手枪", "步枪", "机枪")
# 非目标页签：命中即回退。挂饰/近战/表情/喷漆/名片/动作六类都进来。
REJECT_WORDS = ("挂饰", "近战", "表情", "喷漆", "名片", "动作")
REJECT_PAGES = (
    ("charm", ("挂饰",)),
    ("melee", ("近战",)),
    ("emote", ("表情",)),
    ("spray", ("喷漆",)),
    ("card", ("名片",)),
    ("action", ("动作",)),
)
TAB_BAND = 110                      # 顶部标签栏高度（1080p）
PRICE_RE = re.compile(r"\d[\d,]{3,}")
NAME_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{2,14}-[\u4e00-\u9fff]{2,12}")


def _split(words):
    tab = [w for w in (words or []) if int(w.get("y", 0)) < TAB_BAND]
    body = [w for w in (words or []) if int(w.get("y", 0)) >= TAB_BAND]
    return tab, body


def _segments(text=None, words=None):
    """按词切段：整屏文本直接拼接会把「王者之剑」和下一行「MK4」粘成一个名字，
    也会把 18,200 和 9,999 粘成 182009999。有词表就按词，没有才按空格切。"""
    if words is not None:
        return [(w.get("text") or "") for w in _split(words)[1]]
    return [s for s in (text or "").replace("|", " ").split() if s]


def _body_text(text=None, words=None):
    if words is not None:
        return "".join((w.get("text") or "") for w in _split(words)[1])
    return text or ""


def wear_hits(text=None, words=None):
    """磨损等价字段命中：`磨损/磨损度/崭新出厂…` 或 `成色 S/A/B/C`。

    枪皮挂单页只有「成色 C」这种，没有「磨损」二字；统一在这里归一，
    调用方不要再自己去找「磨损」。
    """
    t = _body_text(text, words)
    hits = [w for w in WEAR_WORDS if w in t and w != "成色"]
    grades = ["成色 %s" % g for g in GRADE_RE.findall(t)]
    if "成色" in t and not grades:
        hits.append("成色")
    return hits + grades


def page_kind(text=None, words=None):
    """页面身份：weapon_list / listing / charm / melee / emote / spray /
    card / action / unknown。命中拒绝页时同时给出是哪一类。"""
    r = listing_list(text, words)
    if r.get("reject"):
        for kind, keys in REJECT_PAGES:
            if any(k in r["reject"] for k in keys):
                return kind
        return "reject"
    if r["ok"]:
        return "listing"
    if weapon_list(text, words)["ok"]:
        return "weapon_list"
    return "unknown"


def weapon_list(text=None, words=None):
    """枪皮列表判定：正文武器名 + 无拒绝页字样。"""
    segs = _segments(text, words)
    t = _body_text(text, words)
    names = []
    for s in segs:
        for m in NAME_RE.findall(s):
            if any(k in m for k in WEAPON_KINDS) and m not in names:
                names.append(m)
    reject = [w for w in REJECT_WORDS if w in t]
    prices = []
    for s in segs:
        for n in PRICE_RE.findall(s):
            v = int(n.replace(",", ""))
            if v >= 1000 and v not in prices:
                prices.append(v)
    wear = wear_hits(text, words)
    why = []
    if reject:
        why.append("命中拒绝页字样：" + "/".join(reject))
    if not names:
        why.append("正文没有武器名（型号-皮肤名）")
    return {"ok": not why, "why": why, "names": names, "prices": prices,
            "wear": wear, "reject": reject, "len": len(t)}


def listing_list(text=None, words=None):
    """挂单列表判定：枪皮列表 + 磨损等价字段（磨损/成色）+ 可解析单价。"""
    base = weapon_list(text, words)
    why = list(base["why"])
    if not base["wear"]:
        why.append("没有磨损/成色字段")
    if not base["prices"]:
        why.append("没有可解析单价")
    return dict(base, ok=not why, why=why)


# 固化导航路径：交易行主页 → 枪皮列表。找到后用这条，不再现场试错。
WEAPON_ROUTE = (
    {"step": "market_home", "action": "recover", "expect_page": "market"},
    {"step": "tab_collection", "action": "click_ocr:典藏外观",
     "expect_tab": "collection", "expect_page": "market"},
    {"step": "weapon_list", "action": "verify", "expect": "weapon_list"},
)
