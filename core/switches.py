"""抢购开关表：界面上那一片开关，每一个都对应引擎真读的一个配置。

为什么要有这张表
    界面上摆一排开关很容易，难的是「拨了真的有用」。以前的做法是前端写死
    一堆 checkbox，后端根本没读 —— 看着能拨，实际什么都不影响。这里把
    **开关 → 配置路径 → 消费点** 三样写在一处，谁没接线一眼能看出来。

每个开关的字段
    key      稳定标识（前端/接口用它）
    group    分组（界面上按组折叠）
    label    名字
    kind     bool / int / float / select
    path     写进 config 的点路径，如 "click_hz"、"console.min_price"
    default  默认值（也写进 config.DEFAULTS）
    desc     一句话说明它到底改变什么
    hint     （可选）取值范围提示

约定
    · `path` 必须是引擎/导航真的读的地方，否则这个开关就是装饰品。
    · 读写统一走 get()/set()，不要直接摸 config 字典。
    · 值一律按 kind 归一化，前端传字符串也能吃。
"""

from __future__ import annotations

import copy

# ---------------------------------------------------------------- 开关表
#
# 分组顺序就是界面上的显示顺序。改这里不用动前端。
SWITCHES = [
    # ====================== 运行模式 ======================
    {
        "key": "run.mode", "group": "运行模式", "label": "执行模式",
        "kind": "select", "path": "run_mode", "default": "auto",
        "options": [["auto", "全自动"], ["semi", "半自动"], ["dry", "演练"]],
        "desc": "全自动：筛选→关注→盯列表→卡点购买；半自动：只定位不点购买；"
                "演练：走完全流程但不发点击",
    },
    {
        "key": "run.workflow", "group": "运行模式", "label": "抢购路线",
        "kind": "select", "path": "workflow", "default": "countdown",
        "options": [["countdown", "公示期倒计时"], ["list", "在售列表"],
                    ["detail", "详情页按钮"]],
        "desc": "倒计时：等公示期归零那一刻点；列表：谁先符合条件买谁",
    },

    # ====================== 收藏设置 ======================
    {
        "key": "follow.before_buy", "group": "收藏设置", "label": "抢前先关注",
        "kind": "bool", "path": "follow_before_buy", "default": True,
        "desc": "定位到目标后先点星标关注，再等倒计时/购买。抢不到下次也还在"
                "「我的关注」里",
    },
    {
        "key": "follow.after_hit", "group": "收藏设置", "label": "抢到后取消关注",
        "kind": "bool", "path": "follow_after_hit", "default": False,
        "desc": "成交后把星标点回灰白，避免「我的关注」被已买到的塞满",
    },
    {
        "key": "follow.retry", "group": "收藏设置", "label": "关注重试次数",
        "kind": "int", "path": "follow_retry", "default": 2, "hint": "0~5",
        "desc": "星标点下去颜色没变时重试几次（游戏偶尔吃点击）",
    },
    {
        "key": "follow.wait_ms", "group": "收藏设置", "label": "关注后等待",
        "kind": "int", "path": "follow_wait_ms", "default": 450, "hint": "毫秒",
        "desc": "点完星标等界面刷新多久，再读颜色校验",
    },
    {
        "key": "follow.skip_done", "group": "收藏设置", "label": "已关注不重复点",
        "kind": "bool", "path": "follow_skip_done", "default": True,
        "desc": "读一眼星标颜色，已经是金色就不再点 —— 再点等于取消关注",
    },

    # ====================== 价格与筛选 ======================
    {
        "key": "price.min", "group": "价格与筛选", "label": "最低价",
        "kind": "int", "path": "console.min_price", "default": 0, "hint": "0=不限",
        "desc": "低于这个价的不买（捡漏的下限，防止买到假货/标错价）",
    },
    {
        "key": "price.max", "group": "价格与筛选", "label": "最高价",
        "kind": "int", "path": "console.max_price", "default": 0, "hint": "0=不限",
        "desc": "高于这个价的不买",
    },
    {
        "key": "price.quota", "group": "价格与筛选", "label": "每轮限量",
        "kind": "int", "path": "console.quota", "default": 1, "hint": "件",
        "desc": "一个任务最多抢几件，抢够就停",
    },
    {
        "key": "price.budget", "group": "价格与筛选", "label": "总预算",
        "kind": "int", "path": "console.budget", "default": 0, "hint": "0=不限",
        "desc": "累计花费上限，超了立刻停手",
    },
    {
        "key": "filter.craft", "group": "价格与筛选", "label": "做工",
        "kind": "select", "path": "active_task.craft", "default": "",
        "options": [["", "不限"], ["极品", "只要极品"], ["优品", "只要优品"]],
        "desc": "只买指定做工的挂单，其余整行跳过",
    },
    {
        "key": "filter.wear_max", "group": "价格与筛选", "label": "最大磨损",
        "kind": "float", "path": "active_task.max_wear", "default": 0.0,
        "hint": "0=不限，如 0.399",
        "desc": "磨损值高于它的不买（成色 S 一般 ≤0.399）",
    },
    {
        "key": "filter.sort", "group": "价格与筛选", "label": "挑单顺序",
        "kind": "select", "path": "active_task.sort", "default": "price",
        "options": [["price", "价格从低到高"], ["wear", "磨损从低到高"],
                    ["screen", "屏幕顺序"]],
        "desc": "一屏里同时有多件符合条件时，先试哪一件",
    },

    # ====================== 抢购设置 ======================
    {
        "key": "fire.hz", "group": "抢购设置", "label": "点击频率",
        "kind": "int", "path": "click_hz", "default": 50, "hint": "次/秒",
        "desc": "购买按钮连点频率。太高会被游戏限流，50 是实测稳的上限",
    },
    {
        "key": "fire.threshold", "group": "抢购设置", "label": "按钮识别阈值",
        "kind": "float", "path": "threshold", "default": 0.85, "hint": "0~1",
        "desc": "模板匹配相关度达到多少算「购买按钮出现了」。低了会点空，"
                "高了会错过",
    },
    {
        "key": "fire.timeout", "group": "抢购设置", "label": "连点超时",
        "kind": "float", "path": "fire_timeout_s", "default": 8.0, "hint": "秒",
        "desc": "点购买最多点这么久，等不到确认弹窗就放弃",
    },
    {
        "key": "fire.confirm_clicks", "group": "抢购设置", "label": "确认连点",
        "kind": "int", "path": "confirm_clicks", "default": 20, "hint": "次",
        "desc": "确认弹窗出现后点几下确认（多点几下防丢帧）",
    },
    {
        "key": "fire.confirm_gap", "group": "抢购设置", "label": "确认间隔",
        "kind": "int", "path": "confirm_interval_ms", "default": 10, "hint": "毫秒",
        "desc": "两次确认之间隔多久",
    },
    {
        "key": "fire.delay", "group": "抢购设置", "label": "购买延迟",
        "kind": "int", "path": "fire_delay_ms", "default": 0, "hint": "毫秒",
        "desc": "解锁瞬间先等这么久再开点。界面还没刷出按钮时点也是白点",
    },
    {
        "key": "fire.jitter", "group": "抢购设置", "label": "随机抖动",
        "kind": "int", "path": "fire_jitter_ms", "default": 0, "hint": "毫秒",
        "desc": "在延迟上再加 0~N 毫秒随机量，避免每次节奏完全一样",
    },
    {
        "key": "fire.adaptive", "group": "抢购设置", "label": "延迟自适应",
        "kind": "bool", "path": "fire_adaptive", "default": True,
        "desc": "按上一轮「点下去→弹窗出现」的实测耗时自动调延迟，"
                "卡得比固定值准",
    },
    {
        "key": "fire.verify", "group": "抢购设置", "label": "成交校验",
        "kind": "bool", "path": "fire_verify", "default": True,
        "desc": "点完确认后必须看到结算弹窗（或余额减少）才算抢到。"
                "关掉会重演「点了就算成交」的假记录，不建议",
    },

    # ====================== 自动与停止 ======================
    {
        "key": "scan.interval", "group": "自动与停止", "label": "列表扫描间隔",
        "kind": "float", "path": "list_scan_interval", "default": 0.05,
        "hint": "秒",
        "desc": "列表没变化时多久看一次屏幕",
    },
    {
        "key": "scan.timeout", "group": "自动与停止", "label": "扫描总时长",
        "kind": "float", "path": "list_scan_timeout", "default": 0.0,
        "hint": "0=一直扫",
        "desc": "扫这么久还没抢到就结束",
    },
    {
        "key": "scan.max_tries", "group": "自动与停止", "label": "最多试几件",
        "kind": "int", "path": "list_max_tries", "default": 20, "hint": "件",
        "desc": "一页里最多试多少条挂单，试完刷新重来",
    },
    {
        "key": "scan.detail_timeout", "group": "自动与停止", "label": "详情页等待",
        "kind": "float", "path": "list_detail_timeout", "default": 3.0,
        "hint": "秒",
        "desc": "点进详情后等购买按钮出现多久，超时就换下一件",
    },
    {
        "key": "auto.retry", "group": "自动与停止", "label": "没抢到换下一件",
        "kind": "int", "path": "countdown_attempts", "default": 3, "hint": "件",
        "desc": "倒计时模式下这件没抢到，最多再换几件继续",
    },
    {
        "key": "stop.after_hit", "group": "自动与停止", "label": "抢到就停",
        "kind": "bool", "path": "stop_after_hit", "default": False,
        "desc": "抢到一件就停掉整条队列，而不是继续下一个任务",
    },
    {
        "key": "stop.on_fail", "group": "自动与停止", "label": "连续失败停",
        "kind": "int", "path": "stop_after_fails", "default": 0, "hint": "0=不停",
        "desc": "连续这么多次没抢到就停，避免界面变了还在空转",
    },
    {
        "key": "stop.hotkey", "group": "自动与停止", "label": "停止热键",
        "kind": "bool", "path": "hotkey.enabled", "default": True,
        "desc": "按 F8 立刻停止（游戏全屏时鼠标甩左上角不一定灵）",
    },
    {
        "key": "start.hotkey", "group": "自动与停止", "label": "开抢热键",
        "kind": "bool", "path": "hotkey.start_enabled", "default": True,
        "desc": "按 F7 直接开抢（跳过界面但不跳过启动前检查）",
    },

    # ====================== 截前（休市） ======================
    {
        "key": "premarket.enabled", "group": "截前", "label": "休市截前",
        "kind": "bool", "path": "premarket.enabled", "default": False,
        "desc": "05:00-08:00 休市时，把鼠标移到购买键上按热键直接进市场排队",
    },
    {
        "key": "premarket.hotkey", "group": "截前", "label": "截前热键",
        "kind": "select", "path": "premarket.hotkey", "default": "F10",
        "options": [["F9", "F9"], ["F10", "F10"], ["F11", "F11"]],
        "desc": "触发截前的键（和停止/开抢键不能重复）",
    },

    # ====================== 环境与安全 ======================
    {
        "key": "env.foreground", "group": "环境与安全", "label": "开点前抢前台",
        "kind": "bool", "path": "force_foreground", "default": True,
        "desc": "每次开点前把游戏切到前台并等画面刷新。关掉能省 100~200ms，"
                "但焦点被别人抢走时会点空",
    },
    {
        "key": "env.preflight", "group": "环境与安全", "label": "启动前检查",
        "kind": "bool", "path": "preflight_required", "default": True,
        "desc": "开抢前检查窗口/OCR/校时，不过就不许开始",
    },
    {
        "key": "env.dry_run", "group": "环境与安全", "label": "演练模式",
        "kind": "bool", "path": "dry_run", "default": True,
        "desc": "走完全流程但不发任何点击，只报告「本来会买什么」",
    },
    {
        "key": "env.save_shots", "group": "环境与安全", "label": "成交截图",
        "kind": "bool", "path": "save_shots", "default": True,
        "desc": "成交后留一张截图，事后能回看抢到了什么、花了多少",
    },
    {
        "key": "env.record", "group": "环境与安全", "label": "写成交记录",
        "kind": "bool", "path": "record_enabled", "default": True,
        "desc": "把每次结果写进 data/records.jsonl，界面的「记录」页读它",
    },
    {
        "key": "env.failsafe", "group": "环境与安全", "label": "防误触",
        "kind": "bool", "path": "failsafe", "default": True,
        "desc": "鼠标甩到屏幕左上角立刻停（游戏全屏时的保命招）",
    },
    {
        "key": "env.input_mode", "group": "环境与安全", "label": "输入通道",
        "kind": "select", "path": "input_mode", "default": "interception",
        "options": [["interception", "内核驱动（快）"], ["legacy", "消息投递（稳）"]],
        "desc": "驱动注入点击更快；驱动装不上时用消息投递，慢但不需要管理员",
    },
]

# 分组顺序（界面按这个排）
GROUPS = ["运行模式", "收藏设置", "价格与筛选", "抢购设置",
          "自动与停止", "截前", "环境与安全"]

_BY_KEY = {s["key"]: s for s in SWITCHES}


def spec(key):
    """取一个开关的定义。不认识的 key 抛 KeyError（宁可炸，不要静默）。"""
    return _BY_KEY[key]


def _root(d):
    """Config 对象或普通 dict 都收。引擎那边传的是 Config。"""
    data = getattr(d, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(d, dict):
        return d
    return {}


def _dig(d, path):
    cur = _root(d)
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _plant(d, path, value):
    """把 value 写到 d 的点路径上，中间缺的层自动建。"""
    parts = path.split(".")
    cur = _root(d)
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _coerce(s, value):
    """按 kind 归一化。前端传字符串（表单）也能吃。"""
    kind = s["kind"]
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if kind == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return int(s["default"])
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(s["default"])
    # select：必须在选项里，否则退回默认
    val = "" if value is None else str(value)
    allowed = [str(o[0]) for o in s.get("options") or []]
    if allowed and val not in allowed:
        return s["default"]
    return val


def get(cfg, key):
    """读一个开关当前的值（配置里没有就用默认）。"""
    s = spec(key)
    raw = _dig(cfg, s["path"])
    if raw is None:
        return copy.deepcopy(s["default"])
    return _coerce(s, raw)


def set(cfg, key, value):
    """写一个开关。返回归一化后的值。"""
    s = spec(key)
    val = _coerce(s, value)
    _plant(cfg, s["path"], val)
    return val


def apply_all(cfg, values):
    """批量写。values 是 {key: value}。返回实际生效的 {key: value}。

    不认识的 key 直接忽略 —— 前端多传一个字段不该让整次保存失败。
    """
    out = {}
    for key, val in (values or {}).items():
        if key not in _BY_KEY:
            continue
        out[key] = set(cfg, key, val)
    return out


def snapshot(cfg):
    """给界面用的完整表：定义 + 当前值，按分组排好。"""
    groups = []
    for g in GROUPS:
        items = []
        for s in SWITCHES:
            if s["group"] != g:
                continue
            item = {k: v for k, v in s.items() if k != "group"}
            item["value"] = get(cfg, s["key"])
            items.append(item)
        if items:
            groups.append({"name": g, "items": items})
    return groups


def defaults():
    """默认值字典（点路径 → 值），给 config.DEFAULTS 用。"""
    out = {}
    for s in SWITCHES:
        _plant(out, s["path"], s["default"])
    return out
