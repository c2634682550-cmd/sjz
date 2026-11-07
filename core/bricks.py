"""砖皮知识库。

数据在 bricks.json：11 个赛季 → 每季砖皮 → 武器 → 别名。
作用是把游戏里 OCR 出来的一行字（"M7战斗步枪-棱镜攻势S2"）对回
"这是 S6 烈火冲天的砖皮"，顺带判断是不是当期、成色几何。

为什么要这个：抢购时真正决定"值不值得抢"的不是名字，而是
「当期 / 往期稀有掉落」「极品 / 优品」「S / A / B / C 成色」这三层。
名字只用来定位是哪一件。
"""
import json
import os
import re
import time

from . import ocr

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bricks.json")

_cache = {"mtime": 0, "data": None}


def normalize(s):
    """汉字 + 字母数字，去掉空格和标点。

    OCR 出来的「AS Val」「QBZ95-1」都要能对上。和 OCR 模块共用同一份实现
    （以前这里自己写了一份正则，两边规则一漂就对不上）。
    """
    return ocr.normalize(s)


def load_problem():
    """资源缺失/损坏时返回一句人话，正常返回 None。

    以前 bricks.json 缺失会静默变成空库，界面仍按「11 赛季」在跑，
    用户以为数据在、其实一条都匹配不上。
    """
    if not os.path.isfile(_PATH):
        return f"皮肤砖块表缺失：{_PATH}"
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:                       # noqa: BLE001
        return f"皮肤砖块表损坏（{type(e).__name__}: {e}）：{_PATH}"
    if not (data.get("seasons") or data.get("tiers")):
        return f"皮肤砖块表是空壳（没有赛季数据）：{_PATH}"
    return None


def _load():
    try:
        mtime = os.path.getmtime(_PATH)
    except OSError:
        return {"seasons": [], "tiers": [], "craft": [], "wear": []}
    if _cache["data"] is not None and _cache["mtime"] == mtime:
        return _cache["data"]
    with open(_PATH, encoding="utf-8") as f:
        data = json.load(f)
    _cache.update(mtime=mtime, data=data)
    return data


def reset_cache():
    """丢掉模块级砖皮表缓存（测试隔离用，生产不调用）。

    `_cache` 按 mtime 命中：测试若在临时目录里铺一份 bricks.json 并把
    `_PATH` 指过去，用例结束后缓存仍留着那份数据；下一个用例换成别的
    目录、但 mtime 恰好相同时会拿到上一份表 —— 随机顺序下就是
    「按品阶停止」用例偶发读错砖皮。测试侧由
    `tests/order_isolation.py` 的 autouse 夹具调用。
    """
    _cache.update(mtime=0, data=None)


def _flatten(data=None):
    """摊平成「一条 = 一个赛季 × 一个皮肤 × 一把武器」。

    副系列（S2 暗鸦、S10 海韵之星、S11 黑银先锋…）也各自摊一条，否则
    「黑银先锋 AS Val」这种写法只能靠武器型号去猜，会被 S7 的悬赏令抢走。
    """
    data = data or _load()
    out = []
    for s in data.get("seasons", []):
        for sk in s.get("skins", []):
            for w in sk.get("weapons", []):
                out.append({
                    "season": s["season"],
                    "season_name": s["name"],
                    "date": s.get("date", ""),
                    "skin": sk["skin"],
                    "weapon": w["name"],
                    "token": w["token"],
                    "alias": sk.get("alias") or [],
                    "note": sk.get("note", ""),
                    "series": sk.get("series") or [],
                    "series_of": None,
                })
            for ser in sk.get("series") or []:
                for token in ser.get("weapons", []):
                    out.append({
                        "season": s["season"],
                        "season_name": s["name"],
                        "date": s.get("date", ""),
                        "skin": ser["name"],
                        "weapon": token,
                        "token": token,
                        "alias": [sk["skin"]] + list(sk.get("alias") or []),
                        "note": f"{sk['skin']} 副系列",
                        "series": [],
                        "series_of": sk["skin"],
                    })
    return out


def records():
    return _flatten()


def seasons():
    return _load().get("seasons", [])


def current_season(today=None):
    """按上线日期算当期赛季（最后一条 date <= 今天的）。"""
    today = today or time.strftime("%Y-%m-%d")
    cur = None
    for s in seasons():
        if s.get("date", "") <= today:
            cur = s
    return cur


def identify(text):
    """把一行文字对回砖皮。返回 {ok, options[], ...}，options 按分数降序。

    打分：皮肤名命中算重，武器型号命中再加权；别名命中按长度算。
    皮肤名互相包含（棱镜攻势 / 棱镜攻势S2）时，长的自然分高。
    """
    t = normalize(text)
    if not t:
        return {"ok": False, "options": [], "reason": "没有文字"}

    scored = []
    for rec in records():
        score, hits = 0.0, []
        name_hit = False
        for label in [rec["skin"]] + list(rec["alias"]):
            n = normalize(label)
            if n and n in t:
                score += len(n) * 2.0
                hits.append(label)
                name_hit = True
        tk = normalize(rec["token"])
        if tk and tk in t:
            score += len(tk) * 3.0
            hits.append(rec["token"])
        if score > 0:
            scored.append((score, hits, rec, name_hit))

    if not scored:
        return {"ok": False, "options": [], "reason": "对不上任何赛季砖皮"}

    top = max(s for s, _, _, _ in scored)
    best = [x for x in scored if x[0] == top]
    best.sort(key=lambda x: (x[2]["date"], x[2]["season"]), reverse=True)

    cur = current_season()
    cur_key = (cur or {}).get("season")
    options = []
    for score, hits, rec, name_hit in best:
        # 满分 = 皮肤名 + 武器型号都命中，多出来的别名命中不再加分
        full = len(normalize(rec["skin"])) * 2 + len(normalize(rec["token"])) * 3
        options.append({
            "season": rec["season"],
            "season_name": rec["season_name"],
            "date": rec["date"],
            "skin": rec["skin"],
            "weapon": rec["weapon"],
            "note": rec["note"],
            "series_of": rec.get("series_of"),
            "hits": hits,
            "current": rec["season"] == cur_key,
            # 只认出枪型号、没认出砖皮名 → 标弱匹配，别让人以为认准了
            "weak": not name_hit,
            "confidence": round(min(1.0, score / full) if full else 0.0, 3),
        })
    return {
        "ok": True,
        "weak": all(o["weak"] for o in options),
        "ambiguous": len(options) > 1,
        "options": options,
        "grade": parse_grade(text),
    }


def candidates():
    """给 OCR 模糊匹配用的候选名（皮肤名 + 别名 + 武器全名）。"""
    out = []
    for rec in records():
        out.append(f"{rec['token']} {rec['skin']}")
        out.append(f"{rec['weapon']} {rec['skin']}")
        out.extend(rec["alias"])
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


_WEAR_ONLY = re.compile(r"成色\s*[-－:：]?\s*([SABC])", re.I)
# 行文本里的成色字母：渲染是 `极品丨成色 A`，做工和字母之间常隔着杂字符，
# 所以允许中间有少量任意非字母字符（早期只允许一个连字符，实测 6/6 张卡片
# 成色都读不出来）。
_WEAR_ROW = re.compile(r"(?:极品|优品)[^SABC]{0,8}?([SABC])(?![A-Za-z])", re.I)
_CRAFT = re.compile(r"(极品|优品)")


def parse_craft(text):
    """做工：极品 / 优品。认不出返回 None。"""
    m = _CRAFT.search(text or "")
    return m.group(1) if m else None


def parse_wear(text, strict=False):
    """成色字母 S/A/B/C。

    认两种写法：「成色 A」和「极品丨成色 A」（做工和字母中间隔着杂字符）。
    **不猜**没有「成色」字样也没有做工前缀的裸字母 —— 名称里的 M4A1、
    SCAR-H 也能匹配出字母。

    strict=True 时只认「成色 X」这一种写法：用在那块**商品名称**上 ——
    名称里本来就有 M4A1 / SCAR-H 这种字母。
    """
    if not text:
        return None
    m = _WEAR_ONLY.search(text)
    if m:
        return m.group(1).upper()
    if strict:
        return None
    m = _WEAR_ROW.search(text)
    if m:
        return m.group(1).upper()
    return None


def parse_wear_loose(text):
    """整块图里的成色字母：取最后一个独立的 S/A/B/C。

    只用在「成色块」上 —— 那块图里就只有「极品 | 成色 A」，不用绕。
    """
    got = re.findall(r"[SABC](?![A-Za-z])", text or "")
    return got[-1] if got else None


def parse_grade(text):
    """从「…-极品S」「…优品A」里抠做工和成色。"""
    if not text:
        return {"craft": None, "wear": None}
    return {"craft": parse_craft(text), "wear": parse_wear(text)}


# ---------------- 品阶目录（每个赛季 × 四个品阶） ----------------
#
# 砖皮知识库原来只有每个赛季的**典藏**档砖皮。游戏里每个系列（赛季）
# 都有 普通/稀有/史诗/传说 四个品阶的皮肤，典藏外观页的筛选面板就是按
# 「系列 + 品阶」过滤的 —— 要「找到某一件」，就得知道它在哪个系列、哪一档。
#
# 数据是 2026-10-23 真机采集的（每个系列 × 每个品阶逐张卡片点开读标题）。
# 结构：seasons[].tiers = {品阶: [{weapon, name}]}

TIER_ORDER = ("传说", "史诗", "稀有", "普通")


def catalog():
    """整个品阶目录：[(赛季, 系列, 品阶, 枪种, 皮肤)]。

    **系列看条目自己的**，不看赛季那一层的 —— 一块砖有 1 传说 + 3 史诗 +
    8 稀有 + 10 普通，跨十几个系列（同一赛季里有「棱镜攻势」也有
    「水墨迷彩」）。按赛季的系列列会把二十来件全写成同一个系列。
    """
    data = _load()
    out = []
    for s in data.get("seasons", []):
        for tier in TIER_ORDER:
            for item in (s.get("tiers") or {}).get(tier, []):
                out.append({
                    "season": s.get("season", ""),
                    "season_name": s.get("name", ""),
                    # series 是这一件自己的系列（「水墨迷彩」「青纹」…），
                    # brick 是这一砖的名（「棱镜攻势」「美杜莎」…）——
                    # 游戏「赛季下拉」里列的是**砖名**，定位时要用它。
                    "series": item.get("series") or s.get("series") or "",
                    "brick": s.get("series") or "",
                    "tier": tier,
                    "weapon": item.get("weapon", ""),
                    "model": item.get("model", ""),
                    "gun": item.get("gun", ""),
                    "gun_fine": item.get("gun_fine", ""),
                    "skin": item.get("skin", ""),
                    "name": item.get("name", ""),
                })
    return out


def skins_of(season=None, tier=None, series=None):
    """查目录：按赛季 / 系列 / 品阶过滤。返回 [{weapon, name, ...}]。"""
    out = []
    for row in catalog():
        if season and row["season"] != season:
            continue
        if series and row["series"] != series:
            continue
        if tier and row["tier"] != tier:
            continue
        out.append(row)
    return out


_NAME_SEPS = ("·", "・", "－", "-", "_")


def _name_part(s):
    """取「皮肤名」那一段：最后一个分隔符之后的部分，没有分隔符就整串。

    `M4A1突击步枪-纯白涂装` → `纯白涂装`；`QBZ95-1突击步枪·王牌之剑` →
    `王牌之剑`；只给型号 `M4A1突击步枪` → 整串（= 没指定皮肤）。
    """
    for sep in _NAME_SEPS:
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s


def find_skin(name, tier=None, season=None, floor=0.72):
    """按名字找皮肤（模糊匹配），返回目录里最像的那条。

    为什么要模糊：游戏用的是花体字，OCR 认不准的字很多
    （「彩」→「采/未/米」、「白」→「自」、「QBZ95-1」→「OBZ．1」）。
    归一化后比相似度，比字符串相等稳得多。

    **只给型号时必须返回 None。** `AS Val突击步枪` 和目录里每一条同型号
    皮肤的相似度都在 0.8 上下（差的那一截正好是皮肤名），以前取第一个最高
    分就直接拿去筛了 —— 用户要「夜穹」，脚本把「蝴蝶」摆到第一个格子买走。
    所以命中后还要比对「皮肤名那一段」：对不上就当没解析出来，让调用方
    报「没指定皮肤名」，别猜。
    """
    if not name:
        return None
    best, best_score = None, 0.0
    for row in catalog():
        if tier and row["tier"] != tier:
            continue
        if season and row["season"] != season:
            continue
        score = ocr.similarity(name, row["name"])
        if score > best_score:
            best, best_score = row, score
    if best is None or best_score < floor:
        return None
    # 只给了型号：和目录里那一行的「型号」几乎一模一样 → 没指定皮肤。
    # 这种情况不能猜（同型号有四五款皮肤，猜错就是买错东西）。
    if ocr.similarity(name, best.get("model", "")) >= 0.98:
        return None
    # 给了皮肤名：分隔符后面那截也要对得上，否则宁可当没解析出来
    if any(sep in name for sep in _NAME_SEPS) and \
            ocr.similarity(_name_part(name), _name_part(best["name"])) < 0.45:
        return None
    out = dict(best)
    out["score"] = round(best_score, 3)
    return out
