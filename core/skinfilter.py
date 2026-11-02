"""皮肤 → 筛选面板怎么点。后端和找皮肤逻辑都从这张表读。

数据来源
--------
`core/skins_raw.csv`（官方概率公示整理出来的 243 件，字段：皮肤名/枪械型号/
枪种/品阶/赛季/系列/别名/来源URL/备注）。这是**唯一的事实来源** ——
bricks.json 里的目录也是从它生成的，两边应该完全一致（见 verify()）。

为什么需要这张表
----------------
筛选面板上有三组东西要点：

    赛季下拉   12 项（全部赛季 + 11 个砖名）
    品阶       4 个勾选框：传说 / 史诗 / 稀有 / 普通
    枪种       7 个勾选框：步枪 / 冲锋枪 / 霰弹枪 / 轻机枪
                          / 精准射手步枪 / 狙击步枪 / 手枪

但官方数据里的枪种有 **8 种**，比面板多一个：「突击步枪」和「战斗步枪」
在面板里都归到「步枪」这一个框。这层映射要是不做，找「战斗步枪」的皮肤
时会去点一个不存在的框，或者错点成「冲锋枪」。
另外「赛季」下拉里列的是**砖名**（棱镜攻势 / 美杜莎 / 群星…），不是数据表
里的「系列」（雪地迷彩 / 水墨迷彩…）—— 一个砖名管一整个赛季的六个系列。

所以每件皮肤都要算出「点哪里」：
    赛季下拉 → 选哪个砖名
    品阶     → 第几个框（0..3）
    枪种     → 第几行第几个框
"""
import csv
import io
import os

# 官方枪种 → 面板上的勾选框。8 种并成 7 个：突击步枪和战斗步枪都归「步枪」。
GUN_SLOTS = {
    "突击步枪": "步枪",
    "战斗步枪": "步枪",
    "冲锋枪": "冲锋枪",
    "霰弹枪": "霰弹枪",
    "轻机枪": "轻机枪",
    "射手步枪": "精准射手步枪",
    "狙击步枪": "狙击步枪",
    "手枪": "手枪",
}

# 面板上品阶从左到右的顺序（col_filter_states 也是按这个顺序返回）
TIERS = ("传说", "史诗", "稀有", "普通")

# 赛季下拉的 11 个砖名（第一项是「全部赛季」，不算）
BRICKS = ("棱镜攻势", "美杜莎", "电玩高手", "命运王牌", "气象感应",
          "棱镜攻势S2", "悬赏令", "电玩高手S2", "造物纪元", "群星", "疾光魅影")

# 赛季 → 下拉里对应的砖名。一个赛季只有一个砖名代表它。
SEASON_BRICK = {
    "S1": "棱镜攻势", "S2": "美杜莎", "S3": "电玩高手", "S4": "命运王牌",
    "S5": "气象感应", "S6": "棱镜攻势S2", "S7": "悬赏令", "S8": "电玩高手S2",
    "S9": "造物纪元", "S10": "群星", "S11": "疾光魅影",
}

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skins_raw.csv")

_cache = None


def _norm_model(s):
    """型号归一：数据表写 ASh-12，知识库写 ASH-12，比对时统一成大写。"""
    return (s or "").strip().upper().replace(" ", "")


def load(path=None):
    """读表，返回 [{name, model, gun_raw, gun, tier, tier_index, season,
    season_item, series, alias}]，按 (赛季, 品阶, 型号) 排好序。"""
    global _cache
    if _cache is not None and path is None:
        return _cache
    rows = []
    with io.open(path or CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            gun_raw = (r.get("枪种") or "").strip()
            tier = (r.get("品阶") or "").strip()
            season = (r.get("赛季") or "").strip()
            if not tier or not season:
                continue
            rows.append({
                "name": (r.get("皮肤名") or "").strip(),
                "model": (r.get("枪械型号") or "").strip(),
                "gun_raw": gun_raw,
                "gun": GUN_SLOTS.get(gun_raw),
                "tier": tier,
                "tier_index": TIERS.index(tier) if tier in TIERS else None,
                "season": season,
                "season_item": SEASON_BRICK.get(season),
                "series": (r.get("系列") or "").strip(),
                "alias": (r.get("别名") or "").strip(),
            })
    rows.sort(key=lambda x: (x["season"], x["tier_index"] if x["tier_index"] is not None else 9,
                             x["model"]))
    if path is None:
        _cache = rows
    return rows


def plan(name):
    """这件皮肤在筛选里怎么点。找不到返回 None。

    输入可以是全名（`M4A1突击步枪-棱镜攻势`），也可以是 OCR 读出来的、
    带错字的那种 —— 交给知识库的模糊匹配先定到具体一件，再回表里取那一行。

    返回：
      season_item  赛季下拉里选哪一项（砖名）
      tier_index   品阶第几个框（0=传说 1=史诗 2=稀有 3=普通）
      gun          枪种框的名字（面板上的叫法，如「步枪」）
      gun_raw      官方数据里的枪种（突击步枪 / 战斗步枪 …）
    """
    if not name:
        return None
    from . import bricks
    hit = bricks.find_skin(name)
    if hit:
        for row in load():
            if (row["season"] == hit["season"]
                    and _norm_model(row["model"]) == _norm_model(hit["model"])
                    and row["name"] == hit["skin"]):
                return dict(row, match="bricks")

    # 兜底：表里按「型号 + 皮肤名」直接拼着比一次（知识库没收录时也能用）
    target = _norm_model(name)
    for row in load():
        if _norm_model(row["model"] + row["name"]) == target:
            return dict(row, match="exact")
    return None


def combos():
    """每个「赛季 + 品阶 + 枪种」组合里有多少件。找皮肤用它判断能不能筛。

    为什么需要：筛选面板在**筛出空列表**时会从贴边变成屏幕中央的浮层，
    坐标全变，复位逻辑就点不动了（详见 core/skinfind.py）。所以动手之前
    先查表：这个组合是 0 件的话，根本别去点它。
    """
    out = {}
    for row in load():
        if not row["season_item"] or not row["gun"] or row["tier_index"] is None:
            continue
        key = (row["season_item"], row["tier"], row["gun"])
        out[key] = out.get(key, 0) + 1
    return out


def count(season_item, tier, gun):
    """这个组合表里有多少件（0 = 别筛，会筛空）。"""
    return combos().get((season_item, tier, gun), 0)


def verify(bricks_catalog=None):
    """自检：每一件都能算出「点哪里」，且和知识库对得上。

    返回 {"ok": bool, "problems": [...]}。problems 为空才算过。
    """
    problems = []
    rows = load()
    if len(rows) != 243:
        problems.append(f"表里应有 243 件，实际 {len(rows)}")

    for row in rows:
        if row["gun"] is None:
            problems.append(f"{row['model']}-{row['name']}：枪种「{row['gun_raw']}」"
                            "在面板上没有对应的框")
        if row["tier_index"] is None:
            problems.append(f"{row['model']}-{row['name']}：品阶「{row['tier']}」不认识")
        if row["season_item"] is None:
            problems.append(f"{row['season']}：这个赛季没有对应的砖名")
        elif row["season_item"] not in BRICKS:
            problems.append(f"{row['season_item']}：砖名不在下拉列表里")

    if bricks_catalog is not None:
        a = {(r["season"], _norm_model(r["model"]), r["name"]) for r in rows}
        b = {(c["season"], _norm_model(c["model"]), c["skin"]) for c in bricks_catalog}
        for k in sorted(a - b)[:5]:
            problems.append(f"表里有、知识库没有：{k}")
        for k in sorted(b - a)[:5]:
            problems.append(f"知识库有、表里没有：{k}")
    return {"ok": not problems, "problems": problems, "count": len(rows)}


def describe(name):
    """给人看的一行：这件皮肤在筛选里怎么点。"""
    p = plan(name)
    if not p:
        return f"「{name}」不在表里"
    return (f"{p['model']}-{p['name']}：赛季下拉选「{p['season_item']}」，"
            f"品阶第 {p['tier_index']} 个框（{p['tier']}），"
            f"枪种选「{p['gun']}」（原表写的是「{p['gun_raw']}」）")
