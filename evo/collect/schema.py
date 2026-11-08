"""五段漏斗字段规格（机器可读）+ 轻量校验。

只描述「旁路采集」能落地的字段。所有缺失一律写 null，不写 0、不写 ""、
不写猜测值；`trust` 标记这一行的证据强度。
"""
from __future__ import annotations

STAGES = ("discover", "click", "pool", "draw", "deal")

TRUST = ("verified", "claimed", "synthetic", "inferred", "unknown")

RESULT_NORM = ("hit", "miss", "skip", "unknown")

GRADE = ("legend", "epic", "rare", "other", "unknown")

# 字段名 -> (类型, 取值域/单位, 缺失填法)
FIELDS = {
    # ---- 身份 ----
    "event_id": ("str", "稳定哈希，重跑不变", "必填"),
    "kind": ("str", "opportunity|stage", "必填"),
    "source": ("str", "records|shots|soak_cycle|soak_ledger|soak_log|live_api", "必填"),
    "source_path": ("str", "项目根相对路径", "必填"),
    "source_line": ("int|null", "行号（jsonl/log/csv）", "null"),
    "synthetic": ("bool", "true=仿真台架产物，非真实机会", "必填"),
    "trust": ("str", ";".join(TRUST), "必填"),
    # ---- 商品 ----
    "name": ("str|null", "商品名（台账原样）", "null"),
    "tier": ("str|null", "传说|史诗|稀有|普通|极品|优品|未标注", "null"),
    "grade": ("str|null", ";".join(GRADE), "null"),
    "grade_counts": ("dict|null", "各档累计计数", "null"),
    "price": ("float|null", "成交/标价，元", "null"),
    "quota": ("int|null", "购买数量", "null"),
    "mode": ("str|null", "detail|list|…", "null"),
    "task_id": ("str|null", "data/<task_id> 目录名或任务 id", "null"),
    # ---- 结果 ----
    "result_raw": ("str|null", "台账原值（hit/miss/no_money/over_budget/aborted）", "null"),
    "result_norm": ("str", ";".join(RESULT_NORM), "unknown"),
    "win": ("bool|null", "抽签是否中签（与成交独立）", "null"),
    "verified": ("bool|null", "是否校验过结算弹窗", "null"),
    "verified_by": ("str|null", "结算弹窗|余额下降|未校验|…", "null"),
    # ---- 时间（毫秒，绝对 epoch；缺失 null） ----
    "ts_ms": ("int|null", "机会锚点时间（台账 ts 优先）", "null"),
    "attempt_id": ("str|null", "机会唯一键：列表行 id/hash + 点击时刻", "null"),
    "first_seen_ms": ("int|null", "第一次看到该商品的时刻", "null"),
    "listed_at_ms": ("int|null", "挂单挂牌时刻（反推=首次看到−剩余秒；精确需引擎写）", "null"),
    "publicity_s": ("float|null", "公示时长（=unlock_at−listed_at），秒", "null"),
    "unlock_at_ms": ("int|null", "理论解锁零点（倒计时反推）", "null"),
    "click_at_ms": ("int|null", "首次开点时刻", "null"),
    "pool_joined_at_ms": ("int|null", "进入抢购池时刻（屏幕出现『已加入抢购池』）", "null"),
    "roll_at_ms": ("int|null", "抽签结果出现时刻（toast）", "null"),
    "deal_at_ms": ("int|null", "结算/成交确认时刻", "null"),
    "tz": ("str", "一律 Asia/Shanghai（本机时区）", "必填"),
    "time_src": ("dict", "{字段: 来源}（record_epoch|record_t_local|log_text_local|mtime_local|api_epoch|ocr_epoch|absent）", "必填"),
    # ---- 时序量化 ----
    "lead_err_ms": ("int|null", "点击相对理论零点偏差，负=早（台账 offset_ms/skew_ms/drift_ms）", "null"),
    "lead_ms": ("int|null", "日志『距归零还有 N ms』", "null"),
    "forward_remaining_s": ("float|null", "看到该件时倒计时显示的剩余秒（『X分Y秒』原值）", "null"),
    "elapsed_ms": ("int|null", "台账 elapsed（秒→毫秒）或日志『用时 X s』", "null"),
    "pool_wait_ms": ("int|null", "进池→抽签结果间隔", "null"),
    "queue_wait_ms": ("int|null", "点击→进池间隔", "null"),
    # ---- 队位 / 号码 / 抽签点数 ----
    "join_result": ("str|null", "joined|rejected_pool|queue_full|no_response|unknown", "null"),
    "join_raw": ("str|null", "进池 toast 原文（OCR/日志原样）", "null"),
    "pool_entered": ("bool|null", "是否确认进入抢购池", "null"),
    "ticket_no": ("str|null", "排队号码（屏幕/接口）", "null"),
    "queue_pos": ("int|null", "队列位置", "null"),
    "queue_capacity": ("int|null", "队列上限（如 1/7 的分母）", "null"),
    "roll_self": ("int|null", "『您的随机点数为 N』", "null"),
    "roll_max": ("int|null", "『本轮抢购最大点数为 M』", "null"),
    "won": ("bool|null", "抽签结论（独立于成交校验）", "null"),
    "toast_source": ("str|null", "joined|queue_full|roll|ocr|log|api|screenshot", "null"),
    # ---- 点击/重试计数 ----
    "click_count": ("int|null", "本轮总点击数", "null"),
    "burst_clicks": ("int|null", "连点阶段点击数", "null"),
    "confirm_clicks": ("int|null", "确认阶段点击数", "null"),
    "refresh_used": ("int|null", "本轮补刷次数", "null"),
    "late_skips": ("int|null", "本轮因来不及而跳过的件数", "null"),
    "followup_used": ("bool|null", "成交前是否补过刀", "null"),
    # ---- 证据 / 收尾 ----
    "shot": ("str|null", "成交截图文件名", "null"),
    "stop_reason": ("str|null", "budget|grade|…", "null"),
    "error": ("str|null", "本轮错误文本", "null"),
    "stage_reached": ("int", "0..5（0=无证据；1 发现 2 点击 3 进池 4 中签 5 成交）", "必填"),
    "stage_evidence": ("list", "每段的原始证据串", "[]"),
}


def blank_event():
    """一条「全 null」的机会壳；调用方只填有证据的字段。

    `str`/`int`/`bool` 类字段默认也是 None —— 规范要求缺失写 null，
    只有 `stage_reached`（0=无证据）和少数容器字段例外。
    """
    ev = {}
    for k, (typ, _dom, _null) in FIELDS.items():
        base = typ.replace("|null", "")
        if base == "str":
            ev[k] = None
        elif base == "int":
            ev[k] = 0 if k == "stage_reached" else None
        elif base == "bool":
            ev[k] = None
        elif base == "float":
            ev[k] = None
        elif base == "dict":
            ev[k] = {}
        elif base == "list":
            ev[k] = []
        else:
            ev[k] = None
    return ev


def validate(ev):
    """返回问题列表（空 = 通过）。只做类型/枚举检查，不猜值。"""
    out = []
    for k, (typ, _dom, _null) in FIELDS.items():
        if k not in ev:
            out.append(f"缺字段 {k}")
    for k in ("event_id", "kind", "source", "trust", "result_norm", "tz"):
        if not ev.get(k):
            out.append(f"{k} 为空")
    if ev.get("result_norm") not in RESULT_NORM:
        out.append(f"result_norm 非法: {ev.get('result_norm')!r}")
    if ev.get("kind") not in ("opportunity", "stage"):
        out.append(f"kind 非法: {ev.get('kind')!r}")
    sr = ev.get("stage_reached")
    if not isinstance(sr, int) or not (0 <= sr <= 5):
        out.append(f"stage_reached 非法: {sr!r}")
    for k in ("ts_ms", "first_seen_ms", "click_at_ms", "pool_joined_at_ms",
              "roll_at_ms", "deal_at_ms", "lead_err_ms", "lead_ms",
              "elapsed_ms", "pool_wait_ms", "queue_wait_ms"):
        v = ev.get(k)
        if v is not None and not isinstance(v, int):
            out.append(f"{k} 不是整数毫秒: {v!r}")
    if not isinstance(ev.get("time_src"), dict):
        out.append("time_src 不是对象")
    return out

