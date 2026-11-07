"""按品阶分别计数停止（feature: stop.by.grade）。

契约 `_feature_contract.md` §2.5：
  总开关 `enabled`（默认 false）：关掉时整个功能不参与判定——不计数、
  不影响停止原因、不写台账；三个目标件数原样保留。
  legend = 传说；epic = 紫（目录里的「史诗」）；rare = 蓝（目录里的「稀有」）。
  普通不参与计数，也不设目标；0 = 不限。
  计数只加在「校验成交」之后；未成交 / 未校验不计数。
  品阶读不出（未点名皮肤、卡片不写品阶）→ 不参与闸门、不计数，绝不猜。
  scope = all   → 任一目标达成就整轮停；
  scope = grade → 只跳过已达标的品阶，其余品阶继续抢。

判阶能力沿用项目自己的皮肤目录（core/bricks.find_skin），不另造分类表。
"""
from . import bricks, switches

LEGEND, EPIC, RARE = "legend", "epic", "rare"
GRADES = (LEGEND, EPIC, RARE)
CN = {LEGEND: "传说", EPIC: "紫", RARE: "蓝"}
# 目录 tier → 配置键。普通不参与计数，单列成 other 只用于复盘标注。
TIER_TO_GRADE = {"传说": LEGEND, "史诗": EPIC, "稀有": RARE, "普通": "other"}
UNKNOWN, OTHER = "unknown", "other"


def settings(cfg):
    """读本功能的开关（脏值由 switches.get 兜底成默认）。"""
    return {
        "enabled": bool(switches.get(cfg, "stop.by_grade.enabled")),
        LEGEND: switches.get(cfg, "stop.by_grade.legend"),
        EPIC: switches.get(cfg, "stop.by_grade.epic"),
        RARE: switches.get(cfg, "stop.by_grade.rare"),
        "scope": switches.get(cfg, "stop.by_grade.scope"),
    }


def enabled(s):
    """总开关是否打开（默认关）。关掉时本功能整个不参与判定。"""
    return bool(s.get("enabled"))


def armed(s):
    """开关打开且至少一档目标不为 0 —— 停止闸门真正生效的条件。

    全 0 = 不限：只计数、不停；关掉 = 连计数都不做。
    """
    return enabled(s) and any((s.get(g) or 0) > 0 for g in GRADES)


def grade_key(text):
    """卡面文本 → legend/epic/rare/other/unknown。

    只认目录里查得到的品阶：find_skin 返回 None（只给了型号、OCR 太脏）
    一律 unknown，不按颜色/价格猜。
    """
    text = (text or "").strip()
    if not text:
        return UNKNOWN
    try:
        hit = bricks.find_skin(text)
    except Exception:                     # noqa: BLE001 - 目录坏了也不许打断抢购
        hit = None
    tier = (hit or {}).get("tier") or ""
    return TIER_TO_GRADE.get(tier, UNKNOWN)


class Counter:
    """一次引擎运行内的品阶计数（传说 / 紫 / 蓝）。"""

    def __init__(self):
        self.counts = {g: 0 for g in GRADES}

    def record(self, grade):
        if grade in self.counts:
            self.counts[grade] += 1
        return self.snapshot()

    def snapshot(self):
        return dict(self.counts)


def reached(s, counts):
    """已经达标的品阶（开关打开、目标 > 0 且计数 >= 目标）。"""
    if not enabled(s):
        return []
    return [g for g in GRADES
            if (s.get(g) or 0) > 0 and (counts.get(g) or 0) >= s[g]]


def detail(s, counts, hits):
    """「传说 1/1、紫 2/2」这种可读的达标说明。"""
    return "、".join(f"{CN[g]} {counts.get(g, 0)}/{s[g]}" for g in hits)
