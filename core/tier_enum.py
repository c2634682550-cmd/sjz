"""品阶枚举：品阶由「筛选上下文」决定，不从卡片 OCR 猜。

为什么要这个模块（2026-11-08 用户要求）：
  卡片上只有成色和价格，没有品阶文字 —— 逐卡推断品阶必然靠猜。
  正确做法是把品阶筛成「普通」：这一页列出来的**全部是普通品阶**，
  页面上下文本身就是证明。所以每条记录的品阶字段写成
  `普通（上下文继承）`，并附上枚举当时的**面板回读快照**。

两条硬约束：
  1. 面板回读（赛季下拉文字 + 四个品阶勾选框）与预期不符 →
     整个枚举作废并抛 `FilterContextError`，不继续跑。
     回读错了说明筛的不是想要的那一页，后面数出来的条数全是废数据。
  2. 两次独立枚举条数必须相同；不同就返回差异条目清单，
     不许取其一当结论（用户原话）。
"""
import time
from collections import Counter

from .nav_coords import NavError

# 筛选面板品阶四列的顺序（页面从左到右，真机实测 2026-11-08）
GRADE_COLUMNS = ("传说", "史诗", "稀有", "普通")

# 品阶字段的写法：不是读出来的，是由筛选上下文继承的
INHERITED_SUFFIX = "（上下文继承）"


class FilterContextError(RuntimeError):
    """筛选面板回读与预期不符 —— 整个枚举作废，不许降级继续。"""


def grade_field(grade):
    """品阶字段的标准写法，例如 `普通（上下文继承）`。"""
    return f"{grade}{INHERITED_SUFFIX}"


def season_readback_ok(season, combo):
    """赛季下拉回读的严格校验。

    `nav._season_match` 是「目标里一半的字出现在读到的串里」，对
    `棱镜攻势` / `棱镜攻势S2` 这种前缀相同的赛季会误判通过 —— 实测
    2026-11-08 15:24：目标棱镜攻势、下拉框实际是棱镜攻势S2，回读
    被判 True，整季枚举拿的是 S2 的列表。所以这里加一条硬约束：
    读到的串**不能比目标长**（多出来的 S2 就是铁证），容许 OCR 等长换字。
    """
    tgt = "".join(ch for ch in (season or "") if not ch.isspace()).upper()
    got = "".join(ch for ch in (combo or "") if not ch.isspace()).upper()
    if not tgt or not got:
        return False
    hit = sum(1 for ch in tgt if ch in got)
    return hit * 2 >= len(tgt) and len(got) <= len(tgt) + 1


def read_context(nav, season=None):
    """读筛选面板的当前状态（面板必须开着）。

    返回 dict:
      season_readback —— 下拉框收起态读到的文字（OCR 原文）
      season_ok       —— 与 season 是否严格匹配（没传 season 时为 None）
      tier            —— 四个勾选框 [传说, 史诗, 稀有, 普通] 的 bool
      owned / not_owned
    """
    img = nav._snap()
    boxes = nav._read_boxes(img)
    combo = nav._season_now() or ""
    season_ok = None
    if season is not None:
        season_ok = season_readback_ok(season, combo)
    return {
        "season_readback": combo,
        "season_ok": season_ok,
        "tier": list(boxes["tier"]),
        "owned": bool(boxes["owned"]),
        "not_owned": bool(boxes["not_owned"]),
    }


def verify_context(ctx, season, grade):
    """校验回读快照与预期一致；不一致抛 `FilterContextError`。

    校验通过后返回要落盘的上下文快照（带 source 标记）。
    """
    if grade not in GRADE_COLUMNS:
        raise FilterContextError(f"没有这个品阶：{grade}")
    idx = GRADE_COLUMNS.index(grade)
    want_tier = [i == idx for i in range(len(GRADE_COLUMNS))]
    if ctx["tier"] != want_tier:
        raise FilterContextError(
            f"品阶回读不符：期望只勾「{grade}」{want_tier}，实际 {ctx['tier']}"
            f"（赛季回读「{ctx['season_readback']}」）—— 整个枚举作废")
    if not ctx.get("season_ok"):
        raise FilterContextError(
            f"赛季回读不符：期望「{season}」，下拉框实际显示"
            f"「{ctx['season_readback']}」—— 整个枚举作废")
    return {
        "season": season,
        "grade": grade,
        "source": "panel_readback",
        "season_readback": ctx["season_readback"],
        "tier_readback": ctx["tier"],
        "owned": ctx["owned"],
        "not_owned": ctx["not_owned"],
    }


def _confirm_panel(nav, tries=3):
    """关筛选面板，最多 tries 轮（每轮 nav 自己还会补一次双击）。

    真机实测（2026-11-08 15:11）：`确认` 偶发连点两次都不响应，面板停在
    filter_collection，nav 抛「停止，不重放」。这里再补 2 轮 —— 仍关不掉
    就按 ESC 收干净并作废整个枚举，绝不带着「筛了但没确认」的列表去数条数。
    """
    last = None
    for _ in range(max(1, tries)):
        try:
            nav.col_filter_confirm()
        except NavError as e:
            last = e
        time.sleep(1.0)
        if not nav._panel_open():
            return True
    try:
        nav.close_filter_panel()
    except Exception:                                        # noqa: BLE001
        pass
    raise FilterContextError(
        f"筛选「确认」{tries} 轮都没关上面板（{last}）—— 枚举作废")


def _pick_season(nav, season):
    """强制选中目标赛季。

    `nav.col_pick_season` 的「已经选中就跳过」用半字匹配，`棱镜` 与
    `棱镜S2` 会互相冒充 —— 实测 2026-11-08 15:24：目标是棱镜攻势、
    下拉框停在棱镜攻势S2，被判定「已经是目标」直接跳过，整季拿错列表。
    所以严格回读不是目标时，先退到中性的「全部赛季」，再选目标：
    这样「跳过」逻辑必然不成立，一定走「滚到顶 → 点第一命中」。
    """
    nav.col_filter()
    time.sleep(0.3)
    if not season_readback_ok(season, nav._season_now() or ""):
        nav.col_pick_season("全部赛季")
        time.sleep(0.5)
    nav.col_pick_season(season)


def apply_filter(nav, season, grade="普通"):
    """把筛选调成「赛季=season + 品阶只剩 grade」，并校验回读。

    返回校验通过的上下文快照；回读不符直接抛错，面板停在原处，
    调用方能看出「筛错了」而不是拿到一堆假条数。
    """
    _pick_season(nav, season)
    idx = GRADE_COLUMNS.index(grade) if grade in GRADE_COLUMNS else -1
    if idx < 0:
        raise FilterContextError(f"没有这个品阶：{grade}")
    nav.col_apply(tier=[i == idx for i in range(len(GRADE_COLUMNS))])
    ctx = verify_context(read_context(nav, season), season, grade)
    _confirm_panel(nav)
    time.sleep(0.8)
    return ctx


def scan_list(nav, step=6, max_screens=60, pause=0.0):
    """把左侧列表从头滚到底，收集卡片名（按出现顺序去重）。

    一格滚轮 = 一张卡，一屏 7 张 → 滚 6 格重叠 1 张（`skinfind` 的实测）。
    一屏全是见过的名字 = 到底了。名字被游戏截断，按名字去重会把
    「同型号不同皮肤」截断成同一个串而少算，所以同时记录屏数供核对。
    """
    nav.skin_scroll_top()
    names = []
    seen = set()
    screens = 0
    empty_streak = 0
    for _ in range(max_screens):
        items = nav.skin_items()
        screens += 1
        if not items:
            empty_streak += 1
            if empty_streak >= 2:
                break
            nav.skin_scroll(step)
            continue
        empty_streak = 0
        fresh = [t for t, _y in items if t not in seen]
        for t in fresh:
            seen.add(t)
            names.append(t)
        if screens > 1 and not fresh:
            break
        nav.skin_scroll(step)
        if pause:
            time.sleep(pause)
    return {"names": names, "screens": screens, "count": len(names)}


def _diff(a, b):
    """两份枚举的名字多重集差异 → [(名字, 第一次条数, 第二次条数)]。"""
    ca, cb = Counter(a), Counter(b)
    out = []
    for name in sorted(set(ca) | set(cb)):
        if ca[name] != cb[name]:
            out.append((name, ca[name], cb[name]))
    return out


def enumerate_season(nav, season, grade="普通", passes=2, step=6,
                     max_screens=60, pause=0.0):
    """单个赛季 + 单档品阶的枚举，跑 passes 次独立枚举做一致性核对。

    每次都是「重新筛 → 回读校验 → 从头扫到底」，不是复用上一遍的列表。
    返回:
      season / grade / grade_field / context（最后一次回读快照）
      passes: [{"names": [...], "screens": n, "count": n}, ...]
      count: 第一次的条数（一致时就是最终条数；不一致时为 None）
      consistent: 两次条数是否相同
      diff: 不一致时的差异条目清单
      items: 校验通过后按上下文继承品阶的条目
    """
    runs = []
    ctx = None
    for _ in range(max(1, int(passes))):
        ctx = apply_filter(nav, season, grade)
        runs.append(scan_list(nav, step=step, max_screens=max_screens,
                              pause=pause))
    counts = [r["count"] for r in runs]
    consistent = len(set(counts)) == 1
    diff = [] if consistent else _diff(runs[0]["names"],
                                       runs[1]["names"]) if len(runs) > 1 else []
    first = runs[0]
    items = [{"name": t, "grade": grade_field(grade),
              "grade_source": "filter_context"} for t in first["names"]]
    return {
        "season": season,
        "grade": grade,
        "grade_field": grade_field(grade),
        "context": ctx,
        "passes": runs,
        "count": first["count"] if consistent else None,
        "counts": counts,
        "consistent": consistent,
        "diff": diff,
        "screens": [r["screens"] for r in runs],
        "items": items,
    }


def enumerate_seasons(nav, seasons, grade="普通", passes=2, step=6,
                      max_screens=60, pause=0.0, on_item=None):
    """逐个赛季跑 enumerate_season，汇总条数。

    on_item —— 可选回调，每跑完一个赛季调用一次（打印/落盘用）。
    返回 {"grade", "grade_field", "seasons": [...], "total", "consistent"}
    任一年级作废（FilterContextError）会向外抛，由调用方报错停止。
    """
    rows = []
    for season in seasons:
        row = enumerate_season(nav, season, grade=grade, passes=passes,
                               step=step, max_screens=max_screens,
                               pause=pause)
        rows.append(row)
        if on_item:
            on_item(row)
    total = sum(r["count"] or 0 for r in rows)
    return {
        "grade": grade,
        "grade_field": grade_field(grade),
        "seasons": rows,
        "total": total,
        "consistent": all(r["consistent"] for r in rows),
    }
