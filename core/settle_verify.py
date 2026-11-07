"""结算判定（三态）+ 两个观测时间戳的落盘口径。

**硬约束（测试里逐条断言，改代码前先读这一段）**：
  拿不到任何证据时，判定必须是 ``unknown``。
  绝不允许把「缺证据」默认成 ``lost``，也绝不允许默认成 ``won``。
  `unknown` 不是失败态，是「这一单结算结果没有证据」的显式状态 —— 归因层
  只能标证据缺失，不能标结算失败。

本模块只吃「**已经解析好的字段**」，不 import ``core.pooltrace``、不读屏幕、
不读 toast：toast 解析那条线正在独立修 5 个缺陷，合并阶段由上层把
`toast_won` / `toast_won_source` 等字段塞进来即可，避免两条线互相改同一文件。

证据字段（全部可选，缺省即 None）：

  中签侧（任一为 True 即 ``won``）
    won_screen          bool  看到中签界面
    item_received       bool  背包/仓库里出现该物品
    reward_claimable    bool  奖励可领取（红点/领取按钮）
    toast_won           bool  toast 解析结论（已由 pooltrace 解析好）
    toast_won_source    str   toast 结论来源：text / points

  落败侧（满足一条即 ``lost``）
    lost_text           bool  明确「未中签 / 未抢购成功」文案
    publicity_over      bool  公示已结束
    row_gone_from_list  bool  公示结束后列表里该单已消失
    reward_absent       bool  明确确认「没有可领取奖励」
      —— 公示结束 + 列表消失 还必须同时拿到「无奖励」证据才算 lost；
         奖励可领取性未知时，仍是 unknown。

  旧口径 ``verified``：
    True  等价 won（历史含义：校验过结算弹窗）
    False 等价 unknown（历史含义：只点了确认，没有任何结算证据）
    None  / 其它脏值 → unknown

两个时间戳（**都是单调时钟毫秒**，同一进程内相减才有意义）：

  first_seen_ms  候选首次出现在列表扫描结果里的时刻
  listed_at_ms   该单完成挂单、进入公示的时刻

  无法观测 -> 字段写 None，并在同一行的 ``no_evidence`` 里列出该字段名；
  不许用 0 / -1 冒充。``listed_at``（epoch 秒）是 toast 推出的墙钟副本，
  不参与段间时长计算，只用于跨会话排序。
"""
from __future__ import annotations

import math
import time

WON = "won"
LOST = "lost"
UNKNOWN = "unknown"
VERDICTS = (WON, LOST, UNKNOWN)

TIME_FIELDS = ("first_seen_ms", "listed_at_ms")

# 两段时长只在同一时钟基准下相减。写入方用单调时钟；回放/旧采集若是
# epoch ms，只要两端同基准也能算。差异超过 12h 视为跨基准（例行公示窗
# 只有分钟级），判 no_evidence，不猜。
MAX_GAP_MS = 12 * 3600 * 1000

# 中签侧证据
WON_KEYS = ("won_screen", "item_received", "reward_claimable")
# 落败侧：文案 / 公示结束 + 列表消失 + 无奖励
LOST_TEXT_KEYS = ("lost_text",)
PUBLICITY_KEYS = ("publicity_over", "row_gone_from_list")
NO_REWARD_KEYS = ("reward_absent",)

REASON = {
    "no_evidence": "没有任何结算证据",
    "won_screen": "看到中签界面",
    "item_received": "收到物品",
    "reward_claimable": "奖励可领取",
    "lost_text": "明确未中签文案",
    "publicity_closed": "公示结束 + 列表消失 + 无奖励",
    "conflicting_evidence": "中签证据与落败证据同时出现，无法裁断",
    "points_only": "只有点数判负，没有结算文案/公示证据",
    "reward_unconfirmed": "公示结束且列表消失，但无奖励状态不可证",
}


def now_ms(clock=None):
    """单调时钟毫秒。``clock`` 仅供测试注入（单位：秒）。"""
    base = time.monotonic() if clock is None else clock
    return int(round(float(base) * 1000))


def _tri(value):
    """证据归一：只认真正的 bool；其余（含 "true"/1/None）一律 None。"""
    return value if isinstance(value, bool) else None


def normalize(value):
    """把任意历史值折成三态字符串。

    旧台账 ``verified`` 是 bool：True = 校验过结算弹窗（won），
    False = 只点了确认（**不是 lost**，只是没有证据 -> unknown）。
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in VERDICTS:
            return v
    if value is True:
        return WON
    return UNKNOWN


def evidence(row):
    """从行/字典里抽出结算证据（不做任何默认推断）。"""
    out = {k: _tri((row or {}).get(k)) for k in
           WON_KEYS + LOST_TEXT_KEYS + PUBLICITY_KEYS + NO_REWARD_KEYS}
    out["toast_won"] = _tri((row or {}).get("toast_won"))
    src = (row or {}).get("toast_won_source")
    out["toast_won_source"] = str(src).strip().lower() if src else ""
    return out


def _won_hits(ev):
    hits = [k for k in WON_KEYS if ev.get(k) is True]
    if ev.get("toast_won") is True and ev.get("toast_won_source") in ("", "text"):
        hits.append("toast_won")
    return hits


def _lost_hits(ev):
    hits = [k for k in LOST_TEXT_KEYS if ev.get(k) is True]
    if ev.get("toast_won") is False and ev.get("toast_won_source") in ("", "text"):
        hits.append("toast_won")
    publicity = all(ev.get(k) is True for k in PUBLICITY_KEYS)
    if publicity:
        no_reward = (ev.get("reward_absent") is True
                     or ev.get("reward_claimable") is False)
        if no_reward:
            hits.append("publicity_closed")
    return hits


def explain(row):
    """返回判定 + 依据；调用方可以直接把它写进报告/日志。"""
    ev = evidence(row)
    won_hits = _won_hits(ev)
    lost_hits = _lost_hits(ev)
    if won_hits and lost_hits:
        verdict, reason = UNKNOWN, "conflicting_evidence"
    elif won_hits:
        verdict, reason = WON, won_hits[0]
    elif lost_hits:
        verdict, reason = LOST, lost_hits[0]
    elif ev.get("toast_won") is False and ev.get("toast_won_source") == "points":
        verdict, reason = UNKNOWN, "points_only"
    elif all(ev.get(k) is True for k in PUBLICITY_KEYS):
        verdict, reason = UNKNOWN, "reward_unconfirmed"
    else:
        verdict, reason = UNKNOWN, "no_evidence"
    return {
        "verified": verdict,
        "reason": reason,
        "reason_text": REASON.get(reason, reason),
        "won_evidence": won_hits,
        "lost_evidence": lost_hits,
        "conflict": bool(won_hits and lost_hits),
    }


def decide(row):
    """判定一单的最终结算结果：won / lost / unknown（缺证据必为 unknown）。"""
    return explain(row)["verified"]


def from_row(row):
    """台账行 -> 三态。

    已经有显式 ``verified``（字符串三态或历史 bool）时以它为准；
    没有时才看行内携带的已解析证据字段。两者都没有 -> unknown。
    """
    raw = (row or {}).get("verified")
    if isinstance(raw, str) or isinstance(raw, bool):
        return normalize(raw)
    return decide(row)


def norm_ms(value):
    """单调时钟毫秒字段归一。非正/非数值/布尔 -> None（不认 0 和 -1）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or float(value) <= 0:
        return None
    return int(round(float(value)))


def missing_marks(row):
    """缺证据的时间字段名列表（写进台账 ``no_evidence``）。"""
    return [k for k in TIME_FIELDS if norm_ms((row or {}).get(k)) is None]


def marks(row):
    """归一后的时间字段 + 缺证据标记。"""
    return {
        "first_seen_ms": norm_ms((row or {}).get("first_seen_ms")),
        "listed_at_ms": norm_ms((row or {}).get("listed_at_ms")),
        "no_evidence": missing_marks(row),
    }


def mark_first_seen(row, clock=None):
    """列表扫描首次命中该候选时调用；已写过则不覆盖。"""
    if norm_ms(row.get("first_seen_ms")) is None:
        row["first_seen_ms"] = now_ms(clock)
    return row["first_seen_ms"]


def mark_listed(row, clock=None):
    """挂单完成、进入公示时调用；已写过则不覆盖。"""
    if norm_ms(row.get("listed_at_ms")) is None:
        row["listed_at_ms"] = now_ms(clock)
    return row["listed_at_ms"]


def gap_ms(a, b):
    """两个时钟标记的差；任一缺失/倒序/跨基准 -> None（不猜）。

    跨基准：一端是单调时钟（开机毫秒），另一端是 epoch 毫秒时，差值会
    是几十亿量级，属于假时长。超过 MAX_GAP_MS 一律判 no_evidence。
    """
    x, y = norm_ms(a), norm_ms(b)
    if x is None or y is None or y < x:
        return None
    delta = y - x
    if delta > MAX_GAP_MS:
        return None
    return delta
