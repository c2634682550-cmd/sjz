"""来不及跳过（`buy.skip_late.threshold_s`）的判定与取数。

真机背景：皮肤挂单走抽签模型，点进去只是「拿号」。公示期一解锁是一堆人
同时进池，倒计时只剩一两秒才读到的那件，等确认框弹出来往往已经结束 ——
硬进去只是白占一次进场机会。这里在**首次读到可用时间读数**的那一帧做判定，
来不及就直接跳过。

两条约定（契约 §2.4 / §6）：

* 优先用反推的解锁时刻 `countdown.Clock.remaining(now)`。界面显示值是
  `floor`（`DISPLAY_TRUNC_S = 1.0`），显示「1 秒」时真实可能还剩 1.9 秒，
  「0 秒」时也可能还剩 0.99 秒 —— 拿显示值当剩余时间会把边界判偏。
* 反推算不出来（没有 `zero_at`）才退回显示值 `sec`，并标 `source="display"`，
  复盘时能分清这条结论的数据质量。

与 `countdown.min_lead` 的关系：`min_lead` 继续用显示秒拦「显示太短」，
这里再叠一道反推闸；两者取更严格的结论，不合并成一个字段。

阈值 0 = 关闭。已解锁（`remaining <= 0`）和显示 0 秒都不跳过 —— 那种不需要
等，反而常常 4 秒就成交。
"""

from __future__ import annotations

import json
import math
import time

from . import countdown

KEY = "buy.skip_late.threshold_s"
DEFAULT_THRESHOLD_S = 2
BOUNDS = (0, 3600)


class SkipLateConfigError(ValueError):
    """配置里的阈值不合法：带字段名和可读原因，调用方原样报给界面/日志。"""


def _root(cfg):
    data = getattr(cfg, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(cfg, dict):
        return cfg
    return {}


def threshold(cfg):
    """严格读阈值。缺键走默认；脏值抛 `SkipLateConfigError`，不回默认。

    保存入口由 `switches.apply_all` 全量校验；这里兜的是「手改 config.json」：
    读到 NaN / 小数 / 负数 / 字符串时，引擎应当拿到可读原因，而不是静默
    套上 2 秒继续跑（那样用户根本不知道自己改的东西没生效）。
    """
    root = _root(cfg)
    buy = root.get("buy")
    if buy is not None and not isinstance(buy, dict):
        raise SkipLateConfigError(f"{KEY}：buy 必须是对象，实际是 {type(buy).__name__}")
    node = (buy or {}).get("skip_late")
    if node is not None and not isinstance(node, dict):
        raise SkipLateConfigError(
            f"{KEY}：buy.skip_late 必须是对象，实际是 {type(node).__name__}")
    if not isinstance(node, dict) or "threshold_s" not in node:
        return DEFAULT_THRESHOLD_S
    raw = node["threshold_s"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SkipLateConfigError(f"{KEY}：不是数字：{raw!r}")
    num = float(raw)
    if not math.isfinite(num):
        raise SkipLateConfigError(
            f"{KEY}：必须是有限数字（NaN/Infinity 不接受）：{raw!r}")
    if num != int(num):
        raise SkipLateConfigError(f"{KEY}：必须是整数：{raw!r}")
    num = int(num)
    lo, hi = BOUNDS
    if num < lo:
        raise SkipLateConfigError(f"{KEY}：{num} 小于下限 {lo}")
    if num > hi:
        raise SkipLateConfigError(f"{KEY}：{num} 大于上限 {hi}")
    return num


def judge(threshold_s, clock=None, now=None, display_sec=None, kind=None):
    """对一件刚读到的挂单做判定。

    参数
        threshold_s   阈值（秒），<= 0 表示关闭
        clock         `countdown.Clock` 或带 `zero_at()/remaining()` 的同构对象
        now           与 clock 同一时间轴（默认 perf_counter）
        display_sec   OCR 读到的显示秒
        kind          `countdown.kind_words()` 的结果；只有 `unlock` 的读数
                      才是「离解锁还有多久」，`remain` 是下架倒计时，不能拿来判

    返回 dict：
        skip         bool
        remaining_s  判定用的剩余秒数；无可用读数时 None
        source       "inferred" / "display" / None
        reason       人话，直接进日志和统计
    """
    out = {"skip": False, "remaining_s": None, "source": None, "reason": ""}
    if threshold_s <= 0:
        out["reason"] = "开关关闭（阈值 0）"
        return out

    remaining = None
    source = None
    if clock is not None and clock.zero_at() is not None:
        remaining = clock.remaining(now)
        source = "inferred"
    elif kind == "unlock" and display_sec is not None:
        remaining = float(display_sec)
        source = "display"

    if remaining is None:
        out["reason"] = "没有可用时间读数"
        return out
    out["remaining_s"] = remaining
    out["source"] = source

    # 契约：显示 0 秒 = 已经解锁，不跳过；反推 remaining<=0 同理。
    if display_sec == 0:
        out["reason"] = "显示 0 秒，已解锁"
        return out
    if remaining <= 0:
        out["reason"] = "已解锁"
        return out

    if remaining < threshold_s:
        out["skip"] = True
        out["reason"] = f"剩余 {remaining:.2f} 秒，不足 {threshold_s} 秒"
    else:
        out["reason"] = f"剩余 {remaining:.2f} 秒，够 {threshold_s} 秒"
    return out


def judge_words(threshold_s, words, now=None):
    """从一次 OCR 词序列直接判定，**两条进场路线共用这一份实现**。

    `_countdown_prepare` 的首读帧和 `_execute_list` 的详情页首读帧都调这里：
    解析（`parse_words` / `kind_words`）、反推（`Clock`）和判定都只有一处，
    免得两条路线各写一套、真机上出现「这条路径跳、那条路径不跳」。

    返回同 `judge()` 的 dict，另外带上 `display_sec` / `kind`，调用方不用再
    解析一遍（重复解析还可能因为入参不同得出不同结论）。
    """
    now = time.perf_counter() if now is None else now
    sec = countdown.parse_words(words)
    kind = countdown.kind_words(words)
    clock = countdown.Clock()
    # 只有「X分Y秒后解锁购买」这类读数才是离解锁还剩多久；在售剩余时间
    # （归零 = 下架）不能拿来判。
    if kind == "unlock" and sec is not None:
        clock.observe(sec, now)
    out = judge(threshold_s, clock, now, sec, kind)
    out["display_sec"] = sec
    out["kind"] = kind
    return out


def stats_entry(item, verdict, display_sec=None, kind=None):
    """给 `engine.status()["stats"]["late_skips"]` 的一条可追溯记录。"""
    return {
        "item": item or "",
        "remaining_s": (round(verdict["remaining_s"], 3)
                        if verdict.get("remaining_s") is not None else None),
        "source": verdict.get("source"),
        "reason": verdict.get("reason") or "",
        "display_sec": display_sec,
        "kind": kind,
    }


def dumps(entry):
    """日志里打一行紧凑 JSON，排查时能直接看全字段。"""
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
