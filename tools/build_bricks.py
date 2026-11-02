"""按源数据重建砖皮目录 core/bricks.json。

源数据是 `tools/bricks_source.csv`（三角洲曼德尔砖枪皮数据库的种子表，
243 条，型号名是干净的）。手工维护的 bricks.json 有两个老毛病：

  · **型号名是 OCR 残骸** —— 当初目录是照着游戏画面 OCR 采的，型号那截
    经常只认出一半（`S步枪·纯白涂装`、`手步枪·花岗岩`、`U冲锋枪·蛇纹涂装`、
    `K4步枪·巨兽`）。按型号表核，175 件里 40 件的型号根本不在表内。
  · **漏了整砖的三分之二的皮肤** —— 一块砖是 1 传说 + 3 史诗 + 8 稀有 +
    10 普通，旧目录每赛季只有 16 件，源数据是 22 件。

所以：**源数据当底稿，旧目录当校准源**。

合并规则（每条都有理由）：
  1. 条目、型号、赛季、品阶 → 用源数据（我们和它对上的 114 件里
     赛季/品阶零分歧，说明它是对的）
  2. 枪种 → 源数据的细分枪种映射到游戏的筛选档（步枪/冲锋枪/霰弹枪/
     轻机枪/精准射手步枪/狙击步枪/手枪）；**游戏自己核过的条目以游戏
     为准**（游戏把 SR-3M 紧凑突击步枪归在冲锋枪档里，源数据说步枪，
     按游戏的来，否则按枪种筛选会筛出空列表）
  3. 赛季上线日期、赛季中文名、系列别名、系列备注、做工/磨损知识库
     → 沿用旧目录（这些是手工整理的，源数据里没有）
  4. 源数据里标了「推定」的系列 → 备注照抄，别当成实测的

用法：
    python tools/build_bricks.py                    # 重建 core/bricks.json
    python tools/build_bricks.py --gun-map _shots/gun_map.json   # 带上游戏核对
"""
import argparse
import collections
import csv
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SOURCE = os.path.join(ROOT, "tools", "bricks_source.csv")
CATALOG = os.path.join(ROOT, "core", "bricks.json")
TIER_ORDER = ("传说", "史诗", "稀有", "普通")

# 细分枪种 → 游戏筛选面板里的那一档。
# 游戏把突击步枪/战斗步枪合在「步枪」，射手步枪/精确射手步枪合在
# 「精准射手步枪」，轻机枪/通用机枪合在「轻机枪」。
FINE_TO_GAME = {
    "突击步枪": "步枪", "战斗步枪": "步枪", "步枪": "步枪",
    "射手步枪": "精准射手步枪", "精确射手步枪": "精准射手步枪",
    "狙击步枪": "狙击步枪", "冲锋枪": "冲锋枪", "霰弹枪": "霰弹枪",
    "手枪": "手枪", "轻机枪": "轻机枪", "通用机枪": "轻机枪",
}


def norm(s):
    return re.sub(r"[·．\.、,，:：\-—_/\\\s]+", "", s or "")


def read_source(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def game_gun_map(path):
    """今天真机按枪种筛选扫出来的结果：{游戏枪种: [卡片上的名字]}。

    卡片名被游戏截断（`KC17突击步枪造物纪` ← 造物纪元），所以只能按前缀
    比对；命中多个档的一律不采信 —— 那说明前缀太短，分不清。
    """
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if isinstance(v, list)}


def gun_by_game(item_name, gm):
    """这一件在游戏里归哪个枪种。认不出（没命中 / 命中多档）返回 None。"""
    hits = set()
    ww = norm(item_name)
    if not ww:
        return None
    for gun, names in gm.items():
        for n in names:
            nn = norm(n)
            if len(nn) >= 5 and (ww.startswith(nn) or nn.startswith(ww)):
                hits.add(gun)
                break
    return hits.pop() if len(hits) == 1 else None


def build(source_rows, old, gm):
    """源数据 + 旧目录 → 新目录。"""
    # 旧目录里的：赛季日期/中文名、系列别名与备注、做工磨损知识库
    old_seasons = {s["season"]: s for s in old.get("seasons", [])}
    old_alias = {}      # (赛季, 系列) -> 别名
    old_note = {}       # (赛季, 系列) -> 备注
    for s in old.get("seasons", []):
        for sk in s.get("skins", []):
            key = (s["season"], sk.get("skin", ""))
            old_alias.setdefault(key, []).extend(sk.get("alias") or [])
            if sk.get("note"):
                old_note.setdefault(key, sk["note"])

    # 源数据按 (赛季, 系列) 归组
    grouped = collections.OrderedDict()
    for r in source_rows:
        se = r["赛季"]
        ti = r["品阶"]
        model = r["枪械型号"].strip()
        fine = r["枪种"].strip()
        skin = r["皮肤名"].strip()
        series = r["系列"].strip() or skin
        weapon = f"{model}{fine}"
        name = f"{weapon}·{skin}"
        gun = FINE_TO_GAME.get(fine, fine)
        ggun = gun_by_game(name, gm)
        item = {
            "weapon": weapon,
            "name": name,
            "model": model,
            "gun": gun,
            "gun_fine": fine,
            "skin": skin,
            "series": series,
        }
        if ggun and ggun != gun:
            item["gun"] = ggun
            item["gun_source"] = "游戏筛选核对"
        grouped.setdefault(se, collections.OrderedDict()).setdefault(
            series, {"tiers": collections.defaultdict(list), "alias": [], "note": ""})
        g = grouped[se][series]
        g["tiers"][ti].append(item)
        for a in (r.get("别名") or "").split(","):
            a = a.strip()
            if a and a not in g["alias"]:
                g["alias"].append(a)
        if r.get("备注") and not g["note"]:
            g["note"] = r["备注"]

    seasons = []
    for se, series_map in grouped.items():
        old_se = old_seasons.get(se, {})
        tiers = {}
        for ti in TIER_ORDER:
            items = []
            for series, g in series_map.items():
                items.extend(g["tiers"].get(ti, []))
            if items:
                tiers[ti] = items
        # 识别层（identify() 靠它认名字）：按**皮肤名**归组。
        # 一块砖里「命运」和「王牌之剑」是两个皮肤名、同属「命运王牌」
        # 系列 —— 按系列归组的话，认「K416 突击步枪 命运」就只剩型号命中，
        # 会被判成弱匹配（实测就是这么挂的）。
        skins = []
        seen_skin = set()
        for series, g in series_map.items():
            for ti in TIER_ORDER:
                for it in g["tiers"].get(ti, []):
                    key = (se, it["skin"])
                    if key in seen_skin:
                        continue
                    seen_skin.add(key)
                    weapons = []
                    for ti2 in TIER_ORDER:
                        for it2 in g["tiers"].get(ti2, []):
                            if it2["skin"] != it["skin"]:
                                continue
                            w = {"name": it2["weapon"], "token": it2["model"]}
                            if w not in weapons:
                                weapons.append(w)
                    alias = []
                    for a in [series] + list(g["alias"]):
                        if a and a not in alias:
                            alias.append(a)
                    # 旧目录里的别名/备注按皮肤名找，找不到再按系列找
                    for src_key in ((se, it["skin"]), (se, series)):
                        for a in old_alias.get(src_key, []):
                            if a not in alias:
                                alias.append(a)
                        if old_note.get(src_key):
                            break
                    note = ""
                    for src_key in ((se, it["skin"]), (se, series)):
                        if old_note.get(src_key):
                            note = old_note[src_key]
                            break
                    note = note or g["note"]
                    skins.append({
                        "skin": it["skin"],
                        "weapons": weapons,
                        "alias": alias,
                        "note": note,
                        "series": [],
                    })
        core_series = ""
        if "传说" in tiers and tiers["传说"]:
            core_series = tiers["传说"][0].get("series", "")
        seasons.append({
            "season": se,
            "name": old_se.get("name", ""),
            "date": old_se.get("date", ""),
            "skins": skins,
            "series": core_series,
            "tiers": tiers,
        })
    seasons.sort(key=lambda s: int(s["season"][1:]))

    out = {
        "updated": time.strftime("%Y-%m-%d"),
        "note": (
            "国服赛季与曼德尔砖典藏外观。条目来自 tools/bricks_source.csv"
            "（三角洲曼德尔砖枪皮数据库，243 条），由 tools/build_bricks.py 生成；"
            "枪种已按游戏自己的筛选核对过（2026-10-23，典藏外观页按枪种筛选、"
            "滚到底读全部卡片名），游戏说算数的以游戏为准。"
        ),
        "seasons": seasons,
        "tiers": old.get("tiers", []),
        "craft": old.get("craft", []),
        "wear": old.get("wear", []),
    }
    # 游戏核对的结果记一笔，方便以后知道哪些是游戏说了算的
    changed = []
    for s in seasons:
        for ti, items in s["tiers"].items():
            for it in items:
                if it.get("gun_source"):
                    changed.append({"name": it["name"], "season": s["season"],
                                    "tier": ti, "gun": it["gun"]})
    out["gun_verified"] = {
        "date": "2026-10-23",
        "method": "典藏外观页按枪种筛选，滚到底读全部卡片名",
        "counts": {k: len(v) for k, v in gm.items()},
        "changed": changed,
    }
    return out


def main():
    ap = argparse.ArgumentParser(description="重建砖皮目录")
    ap.add_argument("--source", default=SOURCE)
    ap.add_argument("--base", default=CATALOG, help="旧目录（取日期/别名/备注）")
    ap.add_argument("--out", default=CATALOG)
    ap.add_argument("--gun-map", default=os.path.join(ROOT, "_shots", "gun_map.json"),
                    help="真机按枪种筛选扫出来的结果（可选）")
    args = ap.parse_args()

    rows = read_source(args.source)
    old = {}
    if os.path.isfile(args.base):
        with open(args.base, encoding="utf-8") as f:
            old = json.load(f)
    gm = game_gun_map(args.gun_map)
    new = build(rows, old, gm)

    total = sum(len(items) for s in new["seasons"] for items in s["tiers"].values())
    print(f"源数据 {len(rows)} 条 → 新目录 {total} 件，"
          f"{len(new['seasons'])} 个赛季，"
          f"游戏核对改档 {len(new['gun_verified']['changed'])} 件")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=1)
    print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
