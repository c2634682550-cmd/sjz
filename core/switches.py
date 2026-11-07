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
import math


class SwitchValueError(ValueError):
    """开关值不合法：带字段名和可读原因，调用方能原样回给界面。

    审查 R03-N03：以前 NaN / ±inf / 负数会被静默接受，然后在引擎里
    变成"永不识别""无限连点""线程崩"。这里把入口收死。
    """

    def __init__(self, key, reason):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}：{reason}")

# ---------------------------------------------------------------- 开关表
#
# 分组顺序就是界面上的显示顺序。改这里不用动前端。
SWITCHES = [
    # ====================== 运行模式 ======================
    {
        "key": "run.semi", "group": "运行模式", "label": "半自动",
        "kind": "bool", "path": "semi_mode", "default": False,
        "desc": "打开后只定位到购买按钮前停手，不点购买/确认；关闭则全自动",
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
        "kind": "bool", "path": "follow_before_buy", "default": False,
        "desc": "定位到目标后先点星标关注，再等倒计时/购买。**默认关**："
                "进购买界面多一次点击就多一次抢不到的机会，而且星标在"
                "详情面板顶部，点到别处会切走页面",
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
        "kind": "select", "path": "filter_defaults.craft", "default": "",
        "options": [["", "不限"], ["极品", "只要极品"], ["优品", "只要优品"]],
        "desc": "只买指定做工的挂单，其余整行跳过",
    },
    {
        "key": "filter.wear_max", "group": "价格与筛选", "label": "最大磨损",
        "kind": "float", "path": "filter_defaults.max_wear", "default": 0.0,
        "hint": "0=不限，如 0.399",
        "desc": "磨损值高于它的不买（成色 S 一般 ≤0.399）",
    },
    {
        "key": "filter.sort", "group": "价格与筛选", "label": "挑单顺序",
        "kind": "select", "path": "filter_defaults.sort", "default": "price",
        "options": [["price", "价格从低到高"], ["wear", "磨损从低到高"],
                    ["screen", "屏幕顺序"]],
        "desc": "一屏里同时有多件符合条件时，先试哪一件",
    },
    {
        "key": "countdown.min_lead", "group": "价格与筛选", "label": "最短倒计时",
        "kind": "int", "path": "countdown_min_lead_s", "default": 6,
        "hint": "秒，0=不限制",
        "desc": "解锁倒计时短于这个秒数的商品直接跳过 —— 来不及弹确认框，"
                "等到了也多半被别人先买走，白耗一次。已经解锁（显示 0 秒）"
                "的不受限制，那种不需要等，可以直接买",
    },
    {
        "key": "filter.pub", "group": "价格与筛选", "label": "公示期筛选",
        "kind": "select", "path": "filter_defaults.pub",
        "default": "只显示公示期",
        "options": [["不限公示期", "不限公示期"],
                    ["只显示公示期", "只显示公示期"],
                    ["不显示公示期", "不显示公示期"]],
        "desc": "在售页顶部那个下拉。倒计时抢购必须选「只显示公示期」—— "
                "只有公示期里才有「X分Y秒后解锁购买」可以掐",
    },
    {
        "key": "filter.ons_sort", "group": "价格与筛选", "label": "列表排序",
        "kind": "select", "path": "filter_defaults.ons_sort",
        "default": "默认排序",
        "options": [["默认排序", "默认排序"],
                    ["按价格升序", "按价格升序"],
                    ["按价格降序", "按价格降序"],
                    ["按成色升序", "按成色升序"],
                    ["按成色降序", "按成色降序"]],
        "desc": "在售页顶部那个排序下拉。捡漏建议「按价格升序」—— 默认排序"
                "里最便宜的那件可能排在后面几页，而抢购只看第一页",
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
        "desc": "至少连点几下。弹窗没消失就继续点（直到「确认兜底时长」），"
                "消失了立刻停 —— 不再盲点固定次数",
    },
    {
        "key": "fire.confirm_gap", "group": "抢购设置", "label": "确认间隔",
        "kind": "int", "path": "confirm_interval_ms", "default": 10, "hint": "毫秒",
        "desc": "两次确认之间隔多久",
    },
    {
        "key": "fire.confirm_timeout", "group": "抢购设置", "label": "确认兜底时长",
        "kind": "float", "path": "confirm_timeout_s", "default": 2.0,
        "hint": "秒",
        "desc": "弹窗还开着就一直点确认，最多点这么久。太短会在「按钮比"
                "倒计时晚亮」时白点一场",
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
    # ==== FEATURE-SWITCHES:stop.by.grade BEGIN ====
    # 品阶目标只在「校验成交」之后 +1；普通品阶不计数；读不出品阶的件不计入
    # 任何一档（也不会被猜成蓝）。0 = 不限。
    {
        "key": "stop.by_grade.enabled", "group": "抢购设置", "label": "按品阶停止",
        "kind": "bool", "strict": True, "path": "stop.by_grade.enabled",
        "default": False,
        "desc": "默认关。关掉时完全不参与判定：不计数、不影响停止原因、不写台账；"
                "三个目标件数原样保留，重新打开就按上次填的走。",
    },
    {
        "key": "stop.by_grade.legend", "group": "抢购设置", "label": "传说目标件数",
        "kind": "int", "integral": True, "path": "stop.by_grade.legend",
        "default": 0, "hint": "件，0~99",
        "desc": "传说到手达到这个件数就按「品阶达标后动作」处理；0 = 不限。",
    },
    {
        "key": "stop.by_grade.epic", "group": "抢购设置", "label": "紫(史诗)目标件数",
        "kind": "int", "integral": True, "path": "stop.by_grade.epic",
        "default": 0, "hint": "件，0~99",
        "desc": "紫色（史诗）到手达到这个件数就按「品阶达标后动作」处理；0 = 不限。",
    },
    {
        "key": "stop.by_grade.rare", "group": "抢购设置", "label": "蓝(稀有)目标件数",
        "kind": "int", "integral": True, "path": "stop.by_grade.rare",
        "default": 0, "hint": "件，0~99",
        "desc": "蓝色（稀有）到手达到这个件数就按「品阶达标后动作」处理；0 = 不限。",
    },
    {
        "key": "stop.by_grade.scope", "group": "抢购设置", "label": "品阶达标后动作",
        "kind": "select", "strict": True, "path": "stop.by_grade.scope",
        "default": "all",
        "options": [["all", "整轮停止"], ["grade", "只停该品阶"]],
        "desc": "整轮停止：任一品阶达标就停下这一轮；只停该品阶：跳过已达标的品阶，"
                "其余继续抢。普通不参与，读不出品阶的件不计数也不拦。",
    },
    # ==== FEATURE-SWITCHES:stop.by.grade END ====
    # ---- feature:scan.repeat BEGIN ----
    # 每件随机刷新次数：倒计时路线每「件」独立抽签；列表路线按页刷新，
    # 这两个值不生效（见 engine.status() 的 stats.refresh_route）。
    {
        "key": "scan.repeat.min", "group": "抢购设置", "label": "每件最少刷新",
        "kind": "int", "path": "scan.repeat.min", "default": 1,
        "integral": True, "hint": "次，0~10",
        "desc": "每件挂单独立抽刷新次数的下限（含）。刷新是重新排队，"
                "不是增加同一件的中签机会；列表路线按页刷新，本设置不生效",
    },
    {
        "key": "scan.repeat.max", "group": "抢购设置", "label": "每件最多刷新",
        "kind": "int", "path": "scan.repeat.max", "default": 1,
        "integral": True, "hint": "次，0~10",
        "desc": "每件挂单独立抽刷新次数的上限（含）。默认 1 = 维持原来的"
                "「没货只刷一次」；调大才启用随机区间。0/0 = 完全不刷新；"
                "次数用尽就换下一件，不无限刷",
    },
    # ---- feature:scan.repeat END ----
    # ==== FEATURE-SWITCHES:buy.followup BEGIN ====
    {
        "key": "buy.followup.enabled", "group": "抢购设置", "label": "假购买补刀",
        "kind": "bool", "strict": True, "path": "buy.followup.enabled",
        "default": False,
        "desc": "默认关；与连点互斥，连点开启时不生效；无确认弹窗的版式不补刀。"
                "开火超时没等到确认弹窗时，在同一购买点再补 1 次点击，"
                "再确认一次；仍然没弹窗就按没抢到记。"
                "抽签模型下补刀收益不确定，还可能双买，不确定就别开。",
    },
    # ==== FEATURE-SWITCHES:buy.followup END ====
    # ==== FEATURE-SWITCHES:click.burst BEGIN ====
    # 四个字段只在开火循环里生效。append = 保持 click_hz 原节奏（本组不改变
    # 开火频率）；burst = 开点瞬间按「每次按住 hold_ms、两次之间隔
    # interval_ms」反复点，window 用尽或弹窗出现立刻回到 click_hz。
    # 抽签模型下只缩短「没中之后再次进池」的等待，不提高单次中签概率。
    {
        "key": "click.burst.mode", "group": "抢购设置", "label": "连点模式",
        "kind": "select", "strict": True, "path": "click.burst.mode",
        "default": "append",
        "options": [["append", "追加点击"], ["burst", "连点"]],
        "desc": "追加点击：完全沿用现在的点击频率，不额外补点。"
                "连点：开点瞬间按「每次按住下面的时长、两次之间隔下面的"
                "间隔」反复点，窗口用完或弹出确认框就停。"
                "抽签机制下：连点只缩短再次进池的延迟，不提高单次中签概率。",
    },
    {
        "key": "click.burst.hold_ms", "group": "抢购设置", "label": "按住时长",
        "kind": "int", "integral": True, "path": "click.burst.hold_ms",
        "default": 100, "hint": "毫秒",
        "desc": "每一次按下按住多久（20~1000）。窗口内排不下一次完整按住"
                "就不再点，不会为了多凑次数缩短按住时间。",
    },
    {
        "key": "click.burst.interval_ms", "group": "抢购设置", "label": "点击间隔",
        "kind": "int", "integral": True, "path": "click.burst.interval_ms",
        "default": 5, "hint": "毫秒",
        "desc": "前一次抬起之后隔多久再按下（1~50）。间隔越小，没中之后"
                "再次进池越快。",
    },
    {
        "key": "click.burst.window", "group": "抢购设置", "label": "触发窗口",
        "kind": "select", "strict": True, "path": "click.burst.window",
        "default": "all",
        "options": [["all", "全部"], ["1s", "1 秒"], ["2s", "2 秒"]],
        "desc": "连点阶段最晚持续到开点后多久。全部 = 只受开火超时限制；"
                "窗口内能排下几次就点几次。",
    },
    # ==== FEATURE-SWITCHES:click.burst END ====
    # ==== FEATURE-SWITCHES:buy.skip_late BEGIN ====
    {
        "key": "buy.skip_late.threshold_s", "group": "抢购设置",
        "label": "来不及跳过",
        "kind": "int", "integral": True, "path": "buy.skip_late.threshold_s",
        "default": 2, "hint": "秒，0=关闭",
        "desc": "首次读到这件只剩不到这么多秒就直接跳过，不进场、不占一次"
                "尝试。剩余时间优先按反推的解锁时刻算，不再信被截断的显示秒；"
                "已经解锁（显示 0 秒）不跳过。0 = 关闭",
    },
    # ==== FEATURE-SWITCHES:buy.skip_late END ====

    # ==== FEATURE-SWITCHES:mouse.human BEGIN ====
    {
        "key": "mouse.human.traj", "group": "抢购设置", "label": "拟人轨迹",
        "kind": "select", "path": "mouse.human.traj", "default": "auto",
        "strict": True,
        "options": [["auto", "自动（开火外 WindMouse，开火短曲线）"],
                    ["windmouse", "WindMouse 重力模型"],
                    ["bezier", "三次贝塞尔"],
                    ["pack", "真人轨迹包（data/traj）"]],
        "desc": "点购买前的移动路径。只在会移动真实光标的通道（驱动/legacy）"
                "生效；纯消息通道不动光标，开了也不生效。轨迹包目录为空或"
                "格式坏时降级为贝塞尔并告警，不会假装在用轨迹包",
    },
    {
        "key": "mouse.human.move_ms", "group": "抢购设置", "label": "单次移动时长",
        "kind": "int", "path": "mouse.human.move_ms", "default": 120,
        "integral": True, "hint": "毫秒",
        "desc": "一次移动到落点的总时长（20~800 毫秒）。开火连点期另有硬上限："
                "不超过点击间隔的 1/4 且不超过 30 毫秒，绝不拖慢连点节奏",
    },
    {
        "key": "mouse.human.curve", "group": "抢购设置", "label": "轨迹弯曲度",
        "kind": "float", "path": "mouse.human.curve", "default": 0.35,
        "hint": "0~1",
        "desc": "贝塞尔控制点的随机偏移幅度，越大越弯；0 = 直线（机械特征"
                "最明显，不推荐）。0.3~0.5 接近手动的自然弧线",
    },
    # ==== FEATURE-SWITCHES:mouse.human END ====

    # ==== FEATURE-SWITCHES:mouse.idle BEGIN ====
    {
        "key": "mouse.idle.mode", "group": "抢购设置", "label": "空闲漫游",
        "kind": "select", "strict": True, "path": "mouse.idle.mode",
        "default": "off",
        "options": [["off", "关"], ["move", "只移动"],
                    ["move_wheel", "移动 + 滚轮"]],
        "desc": "倒计时等待期间在游戏客户区内轻移鼠标，绝不点击；开点窗口前"
                "自动归位。三档：关 / 只移动 / 移动+滚轮。",
    },
    {
        "key": "mouse.idle.interval_ms", "group": "抢购设置", "label": "漫游间隔",
        "kind": "int", "integral": True, "path": "mouse.idle.interval_ms",
        "default": 1200, "hint": "毫秒",
        "desc": "两次漫游动作之间隔多久（300~10000 毫秒）。越小动作越频繁。",
    },
    {
        "key": "mouse.idle.return_s", "group": "抢购设置", "label": "提前归位",
        "kind": "int", "integral": True, "path": "mouse.idle.return_s",
        "default": 2, "hint": "秒",
        "desc": "开点窗口前多少秒必须回到预置位置（1~30 秒），归位后不再移动。",
    },
    {
        "key": "mouse.idle.min_remain_s", "group": "抢购设置",
        "label": "开始漫游剩余",
        "kind": "int", "integral": True, "path": "mouse.idle.min_remain_s",
        "default": 8, "hint": "秒",
        "desc": "倒计时剩余大于该值才开始漫游（3~3600 秒）；接近开点就停下等待。",
    },
    {
        "key": "mouse.idle.wheel_notches", "group": "抢购设置", "label": "漫游滚轮格数",
        "kind": "int", "integral": True, "path": "mouse.idle.wheel_notches",
        "default": 3, "hint": "格",
        "desc": "移动+滚轮模式下每次滚几格（1~10）。纯消息通道滚轮无效，"
                "引擎会如实标成只移动。",
    },
    # ==== FEATURE-SWITCHES:mouse.idle END ====

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
        "key": "env.save_shots", "group": "环境与安全", "label": "成交截图",
        "kind": "bool", "path": "save_shots", "default": True,
        "desc": "成交后留一张截图，事后能回看抢到了什么、花了多少",
    },
    {
        "key": "shot.enabled", "group": "环境与安全", "label": "截图总开关",
        "kind": "bool", "path": "shot.enabled", "default": True,
        "desc": "成交那一刻把游戏客户区丢进后台队列存图；关掉完全不碰磁盘。"
                "「成交截图」也关着时同样不会存",
    },
    {
        "key": "shot.watermark", "group": "环境与安全", "label": "截图水印",
        "kind": "bool", "path": "shot.watermark", "default": True,
        "desc": "底部产品通栏 + 斜向平铺水印 + 左上角 UID；关掉只存无水印原图",
    },
    {
        "key": "shot.keep_raw", "group": "环境与安全", "label": "保留无水印原图",
        "kind": "bool", "path": "shot.keep_raw", "default": True,
        "desc": "打水印时另存一份 _raw 原图（文件名带 _raw），方便二次处理",
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
        "options": [["interception", "内核驱动（快，不动光标）"],
                    ["legacy", "消息投递+借光标（稳，慢）"],
                    ["message", "纯消息投递（零光标干扰，目标须按窗口消息响应）"]],
        "desc": ("驱动注入最快且不动光标；legacy 用 SetCursorPos+PostMessage 兜底；"
                 "message 全程不碰光标，但游戏按下时读真实光标，纯消息不会进游戏逻辑"),
    },
    {
        "key": "env.input_fallback", "group": "环境与安全", "label": "通道降级",
        "kind": "select", "path": "input_fallback", "default": "fail",
        "options": [["fail", "直接失败（默认，界面报错）"],
                    ["message", "降到纯消息投递（不动光标）"],
                    ["legacy", "降到 legacy（借系统光标，录屏可见）"]],
        "desc": ("配置的通道不可用时怎么办；每次降级都会留记录（时间/原因/从哪到哪）。"
                 "默认 fail：硬件通道没通就在界面上明报，不静默借用本机鼠标。"
                 "legacy 用 SetCursorPos 借系统光标，录屏里看得见光标跳动，慎选"),
        "hint": "选 legacy 还要把下面「允许借系统光标」打开，否则按 fail 处理",
    },
    {
        "key": "env.input_legacy_borrow", "group": "环境与安全",
        "label": "允许借系统光标（legacy）",
        "kind": "bool", "path": "input_legacy_borrow", "default": False,
        "desc": ("打开后，input_mode/input_fallback 才会真的允许 legacy 通道"
                 "（SetCursorPos 借光标）。默认关闭：录屏会看到光标跳到点击点，"
                 "而且会占用用户的鼠标"),
    },
    {
        "key": "env.input_message_strict", "group": "环境与安全",
        "label": "纯消息严格判定",
        "kind": "bool", "path": "input_message_strict", "default": True,
        "desc": ("目标是 UnrealWindow（按真实光标判定）时，严格模式按降级策略处理；"
                 "关掉只告警，仍逐次校验窗口是否受理消息"),
    },
]

# 分组顺序（界面按这个排）
GROUPS = ["运行模式", "收藏设置", "价格与筛选", "抢购设置",
          "自动与停止", "截前", "环境与安全"]

_BY_KEY = {s["key"]: s for s in SWITCHES}

# 数值开关的硬边界（审查 R03-N03）：(下限, 上限)，None 表示该侧不设限。
# 放在表外单独列，是因为边界是"入口校验"的职责，不是界面提示的一部分；
# 但漏掉一个边界就等于给引擎留了一条静默失效的路，所以每个数值开关都必须
# 在这里出现（tests/test_numeric_hardening.py 会检查）。
BOUNDS = {
    "follow.retry": (0, 5),
    "follow.wait_ms": (0, 10000),
    "price.min": (0, 99999999),
    "price.max": (0, 99999999),
    "price.quota": (1, 99),
    "price.budget": (0, 999999999),
    "filter.wear_max": (0.0, 10.0),
    "countdown.min_lead": (0, 3600),
    "fire.hz": (1, 200),
    "fire.threshold": (0.0, 1.0),
    "fire.timeout": (0.1, 600.0),
    "fire.confirm_clicks": (0, 1000),
    "fire.confirm_gap": (0, 1000),
    "fire.confirm_timeout": (0.05, 600.0),
    "fire.delay": (0, 5000),
    "fire.jitter": (0, 5000),
    "scan.interval": (0.001, 60.0),
    "scan.timeout": (0.0, 86400.0),
    "scan.max_tries": (1, 1000),
    "scan.detail_timeout": (0.05, 600.0),
    "auto.retry": (0, 100),
    "stop.on_fail": (0, 1000),
    # ---- feature:scan.repeat BEGIN ----
    "scan.repeat.min": (0, 10),
    "scan.repeat.max": (0, 10),
    # ---- feature:scan.repeat END ----
    # ==== FEATURE-SWITCHES:buy.skip_late BEGIN ====
    "buy.skip_late.threshold_s": (0, 3600),
    # ==== FEATURE-SWITCHES:buy.skip_late END ====
    # ==== FEATURE-SWITCHES:mouse.human BEGIN ====
    "mouse.human.move_ms": (20, 800),
    "mouse.human.curve": (0.0, 1.0),
    # ==== FEATURE-SWITCHES:mouse.human END ====
    # ==== FEATURE-SWITCHES:click.burst BEGIN ====
    "click.burst.hold_ms": (20, 1000),
    "click.burst.interval_ms": (1, 50),
    # ==== FEATURE-SWITCHES:click.burst END ====
    # ==== FEATURE-SWITCHES:stop.by.grade BEGIN ====
    "stop.by_grade.legend": (0, 99),
    "stop.by_grade.epic": (0, 99),
    "stop.by_grade.rare": (0, 99),
    # ==== FEATURE-SWITCHES:stop.by.grade END ====
    # ==== FEATURE-SWITCHES:mouse.idle BEGIN ====
    "mouse.idle.interval_ms": (300, 10000),
    "mouse.idle.return_s": (1, 30),
    "mouse.idle.min_remain_s": (3, 3600),
    "mouse.idle.wheel_notches": (1, 10),
    # ==== FEATURE-SWITCHES:mouse.idle END ====
}


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


_issues = {}


def _number(s, value, bounds=None):
    """kind=int/float 的统一入口：有限性 + 符号/上下界。

    审查 R03-N03 修的就是这里：以前 `int(float(value))` 遇到 inf 抛
    OverflowError、遇到 nan 直接收下、负数原样放过 —— 引擎侧再拿去做
    比较/`sleep`，表现是"静默不工作"或线程崩。现在越界一律抛
    SwitchValueError，消息里带字段名和边界值。

    `bounds` 显式传入时用调用方给的 (下限, 上限)；否则查 BOUNDS 表。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise SwitchValueError(s["key"], f"不是数字：{value!r}") from None
    except OverflowError:
        # 10**400 这类整数转 float 直接溢出（审查 N9/N12）。以前这个异常
        # 漏出 get()/snapshot() 变成 500；现在当成越界值，get 退回默认值、
        # 原因进 issues()，前端能看见。
        raise SwitchValueError(s["key"], f"数字超出可表示范围：{value!r}") from None
    if not math.isfinite(num):
        raise SwitchValueError(
            s["key"], f"必须是有限数字（NaN/Infinity 不接受）：{value!r}")
    # ---- feature:scan.repeat BEGIN ----
    # 次数类字段要整数：1.5 既不能截断成 1（用户没说清）也不能四舍五入，
    # 一律拒收（契约 §3 integral）。
    if s.get("integral") and num != int(num):
        raise SwitchValueError(s["key"], f"必须是整数：{value!r}")
    # ---- feature:scan.repeat END ----
    if bounds is not None:
        lo, hi = bounds
    else:
        lo, hi = BOUNDS.get(s["key"], (None, None))
    if lo is not None and num < lo:
        raise SwitchValueError(s["key"], f"{num:g} 小于下限 {lo:g}")
    if hi is not None and num > hi:
        raise SwitchValueError(s["key"], f"{num:g} 大于上限 {hi:g}")
    if s["kind"] == "int":
        return int(num)                 # 向零截断，保持原有行为
    return num


def _coerce(s, value):
    """按 kind 归一化。前端传字符串（表单）也能吃。非法值抛 SwitchValueError。"""
    kind = s["kind"]
    if kind == "bool":
        if s.get("strict"):
            # 契约要求：非布尔值一律拒收（2、"maybe"、NaN 都不能被 bool() 吞掉），
            # 但接受契约列出的 1/0/on/off/是/否 写法。
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "yes", "on", "是"):
                    return True
                if v in ("0", "false", "no", "off", "否"):
                    return False
            raise SwitchValueError(s["key"], f"不是布尔值：{value!r}")
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)
    if kind == "int":
        return _number(s, value)
    if kind == "float":
        return _number(s, value)
    # select：必须在选项里，否则退回默认
    val = "" if value is None else str(value)
    allowed = [str(o[0]) for o in s.get("options") or []]
    if allowed and val not in allowed:
        # ---- feature:mouse.human BEGIN ----
        # 契约 §4.2：strict 的 select 非法值直接拒收，不再静默回落默认。
        if s.get("strict"):
            raise SwitchValueError(
                s["key"], f"不在可选值 [{', '.join(allowed)}]：{value!r}")
        # ---- feature:mouse.human END ----
        return s["default"]
    return val


def get(cfg, key):
    """读一个开关当前的值（配置里没有就用默认）。

    配置里是脏值（手改 config.json 写成 NaN / -999）时不把脏值交给引擎：
    退回默认值，并记进 `issues()` 供界面/排查使用（审查 R03-N03）。
    """
    s = spec(key)
    raw = _dig(cfg, s["path"])
    if raw is None:
        return copy.deepcopy(s["default"])
    try:
        return _coerce(s, raw)
    except SwitchValueError as e:
        _issues[key] = e.reason
        return copy.deepcopy(s["default"])


def set(cfg, key, value):
    """写一个开关。返回归一化后的值。非法值抛 SwitchValueError，不落盘。"""
    s = spec(key)
    val = _coerce(s, value)
    # ---- feature:scan.repeat BEGIN ----
    # 单字段保存也要过跨字段检查：不能靠「先写 min=4、再改 max」绕过 4>3。
    pe = pair_errors(cfg, {key: val})
    if pe:
        raise pe[0]
    # ---- feature:scan.repeat END ----
    _plant(cfg, s["path"], val)
    _issues.pop(key, None)
    return val


def issues():
    """最近一次读取被拒的配置项：{key: 原因}。"""
    return dict(_issues)


def reset_issues():
    """清空被拒配置台账（测试隔离用，生产不调用）。

    `_issues` 是模块级 dict，写它的是 `get()/read()`。测试里只要有一个
    用例故意喂了坏值（例如断言「scan.interval=0 要报错」），这条原因就
    会留到后面的用例：下一个用例读 `snapshot()` 时看到一条本不该存在的
    issue，随机顺序下表现成「连点参数 / 按品阶停止」相关断言偶发失败。
    测试侧由 `tests/order_isolation.py` 的 autouse 夹具在每个用例前调用。
    """
    _issues.clear()


def read(cfg, path, default, kind="int", lo=None, hi=None, key=None):
    """**引擎直读配置的带校验入口**（审查 F6）。

    引擎里有几处不经过 switches API、直接 `float(cfg["threshold"])` /
    `int(cfg["click_hz"])`。手改 config.json 写成 NaN/Inf/负数时，那些地方
    要么拿 NaN 去做比较（恒为假），要么 `int(inf)` 抛 OverflowError，直接把
    引擎线程打挂。

    这个入口读时校验：非有限/越界/类型不对 → 返回 `default`，并把原因记进
    `issues()`（`{key: reason}`），不抛异常 —— 引擎不该因为一个手改字段停摆。
    只用于**不属于开关表**的配置项（开关请用 `get`）；`path` 支持点号嵌套。
    """
    label = key or path
    s = {"key": label, "kind": kind if kind in ("int", "float") else "int"}
    raw = _dig(cfg, path)
    if raw is None:
        return copy.deepcopy(default)
    try:
        return _number(s, raw, bounds=(lo, hi))
    except SwitchValueError as e:
        _issues[label] = e.reason
        return copy.deepcopy(default)


# ---- feature:scan.repeat BEGIN ----
# 跨字段约束：单字段越界由 BOUNDS 拦，组合倒挂（min > max）只能在这里拦。
# 缺的字段从当前配置读 —— 所以「只改 max」也要跟盘上的 min 比一遍。
_PAIR_RULES = [
    ("scan.repeat.min", "scan.repeat.max", "scan.repeat"),
]


def pair_errors(cfg, values):
    """检查成对字段的大小关系，返回 [SwitchValueError]。values 是本次要写的值。"""
    errors = []
    for lo_key, hi_key, label in _PAIR_RULES:
        if lo_key not in _BY_KEY or hi_key not in _BY_KEY:
            continue
        lo = values[lo_key] if lo_key in values else get(cfg, lo_key)
        hi = values[hi_key] if hi_key in values else get(cfg, hi_key)
        if lo > hi:
            errors.append(SwitchValueError(label, f"最少 {lo} 大于最多 {hi}"))
    return errors
# ---- feature:scan.repeat END ----


def apply_all(cfg, values):
    """批量写。values 是 {key: value}。返回实际生效的 {key: value}。

    不认识的 key 直接忽略 —— 前端多传一个字段不该让整次保存失败。
    分两趟：先把所有值校验完，全合法才落盘；只要有一个不合法，整批拒绝并
    列出全部原因。这样即使调用方忘了套 Config.transaction()，也不会出现
    "合法字段被写进去、非法字段被静默丢弃"的半截状态（审查 R03-N03）。
    """
    picked = {}
    errors = []
    for key, val in (values or {}).items():
        if key not in _BY_KEY:
            continue
        try:
            picked[key] = _coerce(_BY_KEY[key], val)
        except SwitchValueError as e:
            errors.append(str(e))
    if errors:
        raise SwitchValueError("values", "；".join(errors))
    # ---- feature:scan.repeat BEGIN ----
    # 跨字段倒挂：整批拒收，磁盘一个字节都不动。
    pair = pair_errors(cfg, picked)
    if pair:
        raise SwitchValueError("values", "；".join(str(e) for e in pair))
    # ---- feature:scan.repeat END ----
    for key, val in picked.items():
        _plant(cfg, _BY_KEY[key]["path"], val)
        _issues.pop(key, None)
    return picked


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
