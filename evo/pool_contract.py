"""入池/抽签证据口径：只判定，缺失即 unknown，不拿 result 猜。

依据 _pool_fields.md 的台账列契约：
- join_result / pool_entered 是直接入池证据；
- roll_self / roll_max / won 是池内抽签证据（能抽签说明已入池）；
- result = hit 是成交级证据（中签必然入池），作为老台账缺失时的唯一回退。

老台账（无上述列）里的 miss 不再默认算入池，改判 pool_unknown 单独计数，
避免把「没进池」和「进池后点数落败」混成一件事。
"""
from __future__ import annotations

from core import settle_verify

# 从台账/遥测行原样透传到 attempts 的字段（observe 用）
CARRY_FIELDS = (
    "attempt_id", "join_result", "join_raw", "pool_entered",
    "pool_joined_at_ms", "queue_pos", "queue_capacity",
    "roll_self", "roll_max", "won", "roll_at_ms",
    "listed_at_ms", "first_seen_ms", "click_at_ms", "unlock_at_ms",
    "forward_remaining_s", "toast_source", "stage_evidence", "no_evidence",
)

POOL_JOINED = ("joined",)
POOL_REJECTED = ("queue_full", "rejected_pool")

CLICK_RESULTS = ("hit", "miss", "no_money", "over_budget")
BLOCK_RESULTS = ("no_money", "over_budget")

# 进入列表（挂单完成、进入公示）的直接证据；first_seen_ms 不算 —— 那是
# 「候选首次出现在扫描结果里」，属于 discover 段，不能拿来冒充 listed。
LISTED_FIELDS = ("listed_at_ms", "listed_at")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def pool_state(row):
    """joined / not_joined / unknown。只认证据，不按 result 猜。"""
    jr = row.get("join_result")
    if jr in POOL_JOINED:
        return "joined"
    if jr in POOL_REJECTED:
        return "not_joined"
    pe = row.get("pool_entered")
    if isinstance(pe, bool):
        return "joined" if pe else "not_joined"
    # 池内才可能产生点数/中签记录
    if _num(row.get("roll_self")) or isinstance(row.get("won"), bool):
        return "joined"
    # 中签必然入池：老台账唯一可用的直接证据
    if row.get("result") == "hit":
        return "joined"
    return "unknown"


def won_state(row):
    """True / False / None。点数可算时按 roll_self >= roll_max 判定。"""
    w = row.get("won")
    if isinstance(w, bool):
        return w
    rs, rm = row.get("roll_self"), row.get("roll_max")
    if _num(rs) and _num(rm):
        return float(rs) >= float(rm)
    if row.get("result") == "hit":
        return True
    return None


def deal_state(row):
    """成交需结算三态为 won；claimed 与 verified 必须分开。

    旧台账 bool True（校验过结算弹窗）折算为 won，bool False / None
    折算为 unknown —— 缺证据不是 lost。
    """
    return settle_verify.normalize(row.get("verified")) == settle_verify.WON


def listed_state(row):
    """进入公示的直接证据是否存在（缺证据返回 False，不猜）。"""
    return any(_num(row.get(k)) and float(row.get(k)) > 0 for k in LISTED_FIELDS)


def classify(row):
    """把一行拆成漏斗判定 + 冲突标记。"""
    res = row.get("result")
    click = res in CLICK_RESULTS
    blocked = res in BLOCK_RESULTS
    ps = pool_state(row)
    hit = res == "hit"
    won = won_state(row)
    deal = deal_state(row)

    conflict = None
    if hit and won is False:
        conflict = "ledger_hit_toast_lose"
    elif (not hit) and won is True and res in ("miss", "", None):
        conflict = "toast_win_ledger_miss"

    unknown = bool(click and not blocked and ps == "unknown")

    if row.get("join_result") in POOL_JOINED or row.get("join_result") in POOL_REJECTED:
        evidence = "ledger:join_result"
    elif isinstance(row.get("pool_entered"), bool):
        evidence = "ledger:pool_entered"
    elif _num(row.get("roll_self")) or isinstance(row.get("won"), bool):
        evidence = "ledger:roll"
    elif hit:
        evidence = "ledger:hit"
    elif click:
        evidence = "ledger:legacy"
    else:
        evidence = None

    return {
        "click": click,
        "blocked": blocked,
        "pool": ps,
        "hit": hit,
        "won": won,
        "deal": deal,
        "unknown": unknown,
        "conflict": conflict,
        "evidence": evidence,
    }
