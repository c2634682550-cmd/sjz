"""剩余时间：解析 + 相位锁定，用来掐准归零那一刻。

为什么不直接等「0分0秒」
------------------------
倒计时一秒一跳。显示「0分0秒」的那一整秒里，真实剩余可能是 0.99 秒，也
可能是 0.01 秒 —— 拿它当触发时钟，最坏会晚整整一秒。抢购窗口按毫秒算，
这一秒足够被别人买走。

真正能用的信息是**跳秒的时刻**：显示值从 7 变 6 的那一瞬间，剩余时间是一个
确定的整数秒。所以每看到一次「值变成 S」，就得到一个归零时刻的估计：

    归零时刻 ≈ 观测到 S 的时刻 + S

取所有观测里最小的那个，误差收敛到一次轮询间隔（实测 10~20ms）。

一个单帧看不出来的地方：显示值是 floor 还是 ceil（已实测，答案是 floor）
--------------------------------------------------------------------------
单看一张截图，分不清「显示 6」意味着剩余在 [6,7) 还是 (5,6]。两种可能：

    ceil ：跳进 S 的那一刻剩余正好 S      → 归零 = t + S
    floor：跳进 S 的那一刻剩余是 S+1      → 归零 = t + S + 1

2026-10-21 在真机上用 `scratch/calib_live.py` 标定了一次，178Hz 采样、采满
80 次跳秒、一直跟到归零。结果是 **floor**，证据是两条互相独立的：

1. 显示从 1 跳到 0 之后 **0.999 秒**，购买按钮才从「不可点」变成「可点」；
   显示从 2 跳到 1 之后 2.000 秒才是那一刻。
2. 起始时显示 80，若按 ceil 则归零落在 (79,80]，若按 floor 则落在 [80,81]。
   按钮实际变化在 80.653 秒 —— 只有 floor 装得下。

所以 `DISPLAY_TRUNC_S = 1.0`，归零 = t + S + 1。显示「0分0秒」的那一整秒里
东西**还没开卖**，等它出现「0」再点，正好晚一秒。

（采样本身很干净：80 次跳秒的间隔 min 0.996s / max 1.005s，没有漏秒。）

开点比归零早一小段
------------------
`due(t, lead)` 让引擎在归零前 lead 秒就开始点。引擎传的提前量是 5 秒
（`FIRE_LEAD_S`，2026-10-21 用户实测那时按钮已经能点）：等归零是跟所有
人同一刻抢，早 5 秒反而稳。早了只是空点几下
（`_fire_and_confirm` 会一直点到弹窗出现），晚了就是被别人买走。

两种倒计时，归零后发生的事完全相反
--------------------------------
真机实测（2026-10-21）交易行里有两行长得像、含义相反的时间：

    剩余：2分31秒          <- 「在售」页，上架剩余时间。归零 = 商品下架
    2分31秒后解锁购买      <- 「公示期」，归零 = 解除限制，可以买了

抢购要盯的是后者。前者归零只会让商品变成「已过期」、按钮变灰，掐它等于
白等一场 —— 引擎读到「剩余」会直接报错中止，而不是闷头空点。判据见
`kind()`。

解析要能吃的 OCR 噪声
---------------------
真机实测（fx=1）：`剩余：0分6秒` 会被读成 `0剩余：0分6秒`（开头那个 0 是
时钟图标），fx=3 读成 `0剩余．0分6秒`，fx=6 读成 `．剩余：0分6秒`。
所以解析不能按固定格式切，要用正则在整个字符串里找「…分…秒」。
"""
import bisect
import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass

# OCR 可能吐出全角数字
_FW = str.maketrans("０１２３４５６７８９", "0123456789")

# 小时/分都可选，秒必须有。这样「0分6秒」「6秒」「1小时2分3秒」都能吃。
_PAT = re.compile(r"(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分)?\s*(\d+)\s*秒")

# 「…后解锁购买」是公示期倒计时，归零即可购买；「剩余：…」是上架剩余
# 时间，归零商品下架。两种必须分开，否则会对着下架时刻掐点。
_UNLOCK_MARKS = ("解锁", "开售", "可购买")
_REMAIN_MARKS = ("剩余",)

# 多久没看到新的跳秒就认为倒计时停更（被买走了 / 界面切走了）
STALE_S = 2.5

# 显示值相对真实剩余时间的滞后，单位秒。真机实测是 1.0（见模块 docstring），
# 意思是「显示 0」的那一整秒里东西还没开卖。
DISPLAY_TRUNC_S = 1.0


def _parse_text(text):
    """从整串文本里抠出剩余秒数。解析不出返回 None。

    只认「…秒」结尾的形式 —— 界面上别的数字（价格、数量）不带「秒」，
    不会误吃。这是对**词序列**解析的补充：词序列凑不出分秒结构时
    （比如只写了「12秒」），用这一条兜底。
    """
    if not text:
        return None
    t = text.translate(_FW)
    m = _PAT.search(t)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


def parse_words(words):
    """词序列解析：取单位词里、或它左边最近的数字。

    真机教训（2026-10-23）：倒计时左边的锁图标被 OCR 读成「6」，和真正的
    「0」挨在一起时，整串文本解析会把「0分5秒」当成「60分5秒」—— 傻等
    一小时，抢购窗口早过去了。按词切开、只认紧贴单位的数字，图标噪声被
    自然隔离：`6 | 0 | 分 | 5 | 秒` 取到的是 0 分 5 秒。

    单位词自己带数字（OCR 把「0分」粘成一个词）时取单位前的那段；
    否则向左找最近的数字词。词序列里凑不出分秒结构时退回整串文本解析
    （「12秒」这种只写秒数的写法）。
    """
    if not words:
        return None
    texts = [str(w.get("text", "") or "").strip()
             for w in sorted(words, key=lambda w: w.get("x", 0))]
    mi = next((i for i, t in enumerate(texts) if "分" in t), None)
    si = next((i for i, t in enumerate(texts) if "秒" in t), None)
    if mi is None or si is None:
        return _parse_text("".join(texts))

    def num_for(idx, unit):
        nums = re.findall(r"\d+", texts[idx].split(unit, 1)[0])
        if nums:
            return int(nums[-1])
        for j in range(idx - 1, -1, -1):
            nums = re.findall(r"\d+", texts[j])
            if nums:
                return int(nums[-1])
        return 0

    hi = next((i for i, t in enumerate(texts) if "时" in t), None)
    h = num_for(hi, "时") if hi is not None else 0
    return h * 3600 + num_for(mi, "分") * 60 + num_for(si, "秒")


def kind_words(words):
    """这条倒计时是哪一种。解析不出时返回 None。

    "unlock" —— 公示期：「X分Y秒后解锁购买」，归零 = 可以买了。
    "remain" —— 在售：「剩余：X分Y秒」，归零 = 商品下架。

    不要求分秒结构存在：「剩余：2天23小时」（已解锁）没有「秒」也能判成
    remain —— 引擎要对准那种状态报「你盯的是上架剩余时间」，而不是等
    6 秒超时再说框选区域不对。
    """
    if not words:
        return None
    joined = "".join(str(w.get("text", "") or "") for w in words)
    for mark in _UNLOCK_MARKS:
        if mark in joined:
            return "unlock"
    for mark in _REMAIN_MARKS:
        if mark in joined:
            return "remain"
    return None


class Clock:
    """把一串「显示值 + 观测时刻」拟合成精确的归零时刻。

    用法：

        c = Clock()
        for 每次 OCR:
            sec = parse(text)
            if sec is not None:
                c.observe(sec, now)
            if c.zero_at() is not None and now >= c.zero_at():
                fire()

    **只在值变化时记录**。同一个值连续看到很多帧时，t + S 会越算越大，
    那些观测全是松上界，记进去只会污染 min。
    """

    def __init__(self, poll_interval=0.01):
        self.poll_interval = poll_interval
        self.reset()

    def reset(self):
        self._last_value = None
        self._last_tick = None      # 显示值最后一次「变化」的时刻
        self._estimates = []      # 每次跳秒给出的归零时刻上界
        self._seen_zero = False
        self._first_seen = None

    def observe(self, seconds, t=None):
        """喂一次观测。返回这次是否构成一个新的跳秒（值变了）。"""
        if seconds is None or seconds < 0:
            return False
        t = time.perf_counter() if t is None else t
        if self._last_value is not None and seconds > self._last_value:
            # 倒计时变长了 —— 换了目标或者重新开始，旧的观测全部作废，
            # 否则 min() 会被上一个目标的归零时刻钉死。
            self.reset()
        if self._first_seen is None:
            self._first_seen = t
        if seconds == self._last_value:
            return False        # 同一个值：不记新上界，t+S 只会越算越松
        self._last_value = seconds
        self._last_tick = t
        if seconds == 0:
            self._seen_zero = True
        # 实测（2026-10-21 真机，178Hz 采满 80 次跳秒）：显示值是 floor，
        # 显示 0 的那一整秒里真实还剩 0~1 秒。所以跳进 S 的那一刻真实剩余
        # 是 S+1，归零时刻 = t + S + 1。证据见模块 docstring。
        self._estimates.append(t + seconds + DISPLAY_TRUNC_S)
        return True

    def zero_at(self):
        """归零时刻的最紧上界（perf_counter 时间轴）。没有观测返回 None。"""
        if not self._estimates:
            return None
        return min(self._estimates)

    def remaining(self, t=None):
        """按拟合结果算剩余秒数。没有观测返回 None。"""
        z = self.zero_at()
        if z is None:
            return None
        t = time.perf_counter() if t is None else t
        return z - t

    def due(self, t=None, lead=0.0):
        """到归零时刻了吗。没有观测时永远 False —— 不猜。

        `lead` 是提前量：传 5.0 表示「归零前 5 秒就算到点」（引擎的
        `FIRE_LEAD_S`，用户实测那时按钮已可点）。开点比归零早一点点
        没坏处（按钮还没亮，点了也白点），晚了就是被别人买走。
        """
        z = self.zero_at()
        if z is None:
            return False
        t = time.perf_counter() if t is None else t
        return t + lead >= z

    def is_stale(self, t=None, stale_s=None):
        """倒计时是不是停更了（跳秒没了、或者界面切走）。

        判据是「**显示值**多久没变」，不是「多久没读到」—— 后者会被偶发的
        OCR 失败误伤。真机上就是这么栽的：一帧没读出来就少一次刷新，
        连丢两帧就误报停更，把好好的倒计时掐了。

        从来没读到过不算停更，那种情况是配置问题，由调用方另行报错。
        """
        if self._last_tick is None:
            return False
        t = time.perf_counter() if t is None else t
        return (t - self._last_tick) > (STALE_S if stale_s is None else stale_s)

    def info(self):
        """给界面/日志看的快照。"""
        return {
            "last": self._last_value,
            "zero_at": self.zero_at(),
            "ticks": len(self._estimates),
            "seen_zero": self._seen_zero,
        }


# ---------------------------------------------------------------- T0 反推
#
# 记号：T0 = 归零时刻（与输入时间戳同一时间轴）。读数 s 在帧时刻 t 满足
#
#     s = floor(T0 - t + lag)        （lag = 显示链路滞后，默认 0）
#
# 于是 T0 ∈ [t + s - lag, t + s + 1 - lag)，记 v = t + s。正确读数的 v
# 全落在同一个宽度 1 的窗里（v = T0 - frac(剩余)），取交集：
#
#     T0 ∈ [max v, min v + 1)       宽度 = 1 - (max v - min v)
#
# 点估计取交集中点，误差 ≤ 宽度 / 2，不依赖采样相位分布。**跳变前最后一帧
# + 跳变后第一帧**天然把区间收到一个帧间隔 —— 只要每帧都喂，这条成对信息
# 就在缓冲里；只喂跳变沿会把「跳变前一帧」丢掉，交集退化成 1 秒宽（实测
# 中点偏 -495ms）。是喂法丢信息，不是信息不存在（见报告 §6.3）。
#
# 错读（±1/±2）三层剔除：
#   1. 尖峰过滤：短命 run（<0.5 秒）且前后读数相同 → 整段判外点；
#   2. 共识窗 + 交集倒挂剪枝（RANSAC 式）；
#   3. 速率模型：读数只能减不能增；掉 k 格至少需要 k-0.5 秒。违背即不可信。
#
# 可信不是「区间窄」的同义词：界面冻结、一次跳变都没有、模型违背，都会
# 让交集看起来又窄又准，必须单独否决。不可信时 `Estimate.zero_at` 为 None，
# 原始中点只放在 `zero_at_raw` 里供日志/复核，调用方拿不到能误用的数。

# 一个读数的可行区间宽度（秒）。显示值是 floor，所以固定为 1。
EST_WINDOW_S = 1.0

# 估计器保留的读数上限。320 条 @125Hz ≈ 2.56 秒：既能覆盖 300 条预算，
# 又不至于让 estimate() 的排序变慢（256 会把 300 条截成 256，报告已修）。
EST_MAXLEN = 320

# 少于这个有效读数，任何精度承诺都不可信。
EST_MIN_TRUST_N = 8

# 有效读数的采样跨度下限。
EST_MIN_SPAN_S = 0.2

# 读数比上一次突然增大这么多 —— 不是同一场倒计时了（换了目标/重新开始）。
EST_RESET_JUMP_S = 20.0

# 默认容差。可行区间半宽超过它就算不可信（上层要求 ±20ms）。
EST_DEFAULT_TOL_S = 0.020

# ---- P0-1 冻结 / 模型硬不变式（阈值由 tools/bench 冻结矩阵实测确定） ----
# 同一读数持续 ≥ 0.96s 后交集宽度会掉到几十毫秒并谎报可信；真实倒计时
# 同一读数最多持续 1.0s，所以按 0.90s + 占比 0.85 卡，同时要求至少出现
# 两次不同读数（n_ticks ≥ 2）。
EST_MIN_TICKS = 2
EST_FREEZE_RUN_S = 0.90
EST_FREEZE_SHARE = 0.85
# 「显示值多久没变 = 卡死」的逐次判据。一次正常读数持续 1.0s，归零前那一刻
# 距上次跳变恰好 ≈1s，所以这个门槛必须比 1s 宽；1.20s 不会误伤正常跳秒。
EST_STUCK_S = 1.20

# 首尾各 5 帧中位数与经过时间的偏差上限（单位：win）。1.1 能挡住 1.1s+
# 的停更，同时不误伤正常 2.4s 缓冲。
EST_MODEL_TOL_S = 1.1

# 时钟跳变：时间戳倒退超过 0.5s（NTP 后跳 / 手工改表）或前跳超过 8s
# （长时间没喂）都按「换时间轴」清空重来，不让两段轴混在同一个缓冲里。
EST_CLOCK_BACK_S = 0.5
EST_CLOCK_GAP_S = 8.0

# 跳变成对夹逼的最大帧间隔：再大就不是「跳变前最后一帧」了。
EST_BRACKET_GAP_S = 0.35

# 只喂「跳变前一帧 + 跳变后第一帧」时，每段约 2 条读数：观测/段数 ≈1.4~1.6；
# 只喂跳变沿（不喂前一帧）时恒为 1.0。用这个比值判定调用方到底给了哪种数据，
# 比值不够就明确不可信，绝不拿假成对硬凑。
EST_PAIR_RATIO_MIN = 1.15

# 成对夹逼至少要几对同时在同一个点重叠才算可信。实测 3 对仍会以 1%~4%
# 的概率被 ±1 错读造出的假簇骗过；4 对在偏读率 ≤10% 时假可信为 0。
EST_BRACKET_MIN_PAIRS = 4

# 真跳变在时间上严格相隔整数秒（同一读数持续 1.0s），成对区间中点也应落在
# 整数格点上。假成对（±1 错读造成）出现在 tick 中间，与真成对的时间差是
# 小数 —— 这一条是独立于「重叠计数」的第二道校验。
EST_BRACKET_LATTICE_TOL_S = 0.050

# 密集流的双算法复核：交集（精细但怕假窄点）vs 最大共识窗（粗但抗孤立
# 错读）。两条独立管线算出的中点必须对齐，分歧超过这个值就判不可信 ——
# 单条 ±1 错读把区间钉到偏 60~130ms 的假窄点时，就靠这一条挡住。
EST_AGREE_TOL_S = 0.040

# 窄区间（被某个跳变夹住的区间）两端各自由哪个稳定段撑住：那两段至少要有
# 这么多帧。实测：一个 tick 内两条相邻 -1 错读会在缓冲尾部伪造一次「跳变」，
# 形成 11ms 宽、偏 344ms 的假可信区间，两端各有 2 帧 / 27 帧 —— 只靠交集、
# 共识、格点都分不开（数学上它是自洽的另一解），但假跳变后的新读数只有
# 2 帧，真跳变的后续段在 125Hz 下通常有几十帧。真跳变正好落在缓冲最后
# 1~2 帧时会被判不可信 —— 这是有意的保守取舍（宁可多等一秒）。
EST_MIN_BOUND_FRAMES = 3
EST_MIN_BOUND_SPAN_S = 0.100



def _rise_outliers(obs, win=EST_WINDOW_S):
    """读数在时间上只能不变或变小。返回「违反这条约束」的下标集合。

    这是独立于共识计数的硬约束，专挡一种假可信：整段只有一个 tick 时，任何
    一条 `+1` 错读都会把 v=t+s 抬到 s+1 的位置，把交集从「整秒宽」钉成
    8ms 宽的假区间，中点偏 +0.5s，且计数完全正常（27/30 内点）。
    实测（tools/bench）：30 条单 tick 数据里 1 条 +1 错读即可复现 +541ms
    的假可信；剔完后交集回到正确的 0.6s 宽 → wide → 不可信。

    判据用物理先验而不是「数帧」：真读数每个值持续一整个 tick（约 1.0s），
    所以**同一读数的连续段不可能短**；短段只能是 ±1/±2 错读（含成串错读）
    造出来的伪平台。

    为什么不用「内部短段一律剔除」或按时间顺序走段：
      * 「内部短段一律剔除」：±1 错读会把一个真平台**打碎**成十几段，每段
        都 < 0.5s，实测 120 条数据被剔掉 89~118 条（可用性直接归零）；
      * 「以首个长段为基准、向上回跳一律剔、短下跳段也剔」：基准一旦取到
        窗口后半段（后半段更长很常见），前半段真读数会被整片当成「向上
        回跳」剔掉，实测矩阵可用性从 0.94 掉到 0.21，且可信误差最大到 660ms。

    最终用**最长非增子序列（LNDS）**：在「s 只能持平或下降」这条物理约束
    下保留尽可能多的帧，其余帧判为错读。逐帧全局最优，不会整片误删；±1/±2
    的孤立错读不论落在平台中间、交界还是尾部，都只剔它自己，不会像「相邻
    比较 + 平台长度」那种启发式一样把错读之后的**恢复帧**一起误杀。
    LNDS 的唯一已知盲点（成串 +1 错读恰好压在真跳变前，计数上比正确链长）
    由 estimate() 里的「端点支撑复核」兜底：界定区间的帧必须来自持续
    ≥ EST_MIN_BOUND_SPAN_S 的同值段。

    缓冲开头的短段（< EST_MIN_BOUND_FRAMES 帧）且读数比后面高，额外单独
    剔除：LNDS 在「7,6,6,6...」这种开头会把 7 保留下来（长度 4 > 3），在
    「s0, s0+1, s0+1, s0, s0, ...」这种两帧 +1 错读里甚至会偏爱错读
    （2+27 > 1+27）。但真跳变不会「先高后低只持续 1~2 帧」，所以这一段只能
    是错读。保守剔除 —— 代价是这一对约束作废（交集变宽 → 不可信），不会
    产生假可信。
    """
    n = len(obs)
    if n == 0:
        return set()
    tails = []          # 各长度最优链末尾的 -s 值（非降）
    tail_idx = []       # 对应下标
    prev = [-1] * n
    for i, (_t, s) in enumerate(obs):
        x = -s
        j = bisect.bisect_right(tails, x)
        prev[i] = tail_idx[j - 1] if j > 0 else -1
        if j == len(tails):
            tails.append(x)
            tail_idx.append(i)
        else:
            tails[j] = x
            tail_idx[j] = i
    keep = set()
    k = tail_idx[-1]
    while k != -1:
        keep.add(k)
        k = prev[k]
    out = set(range(n)) - keep

    j = 0
    while j < n and obs[j][1] == obs[0][1]:
        j += 1
    if 0 < j < EST_MIN_BOUND_FRAMES and j < n and obs[0][1] > obs[j][1]:
        out.update(range(j))
    # 头部「正确帧 + 短 +1 抬高段 + 回落到正确值」：LNDS 会偏爱抬高段
    # （2+27 > 1+27），必须显式剔掉抬高段本身。
    if 0 < j < EST_MIN_BOUND_FRAMES and j < n:
        k = j
        while k < n and obs[k][1] == obs[j][1]:
            k += 1
        if (j < k < j + EST_MIN_BOUND_FRAMES
                and obs[j][1] == obs[0][1] + 1
                and k < n and obs[k][1] == obs[0][1]):
            out.update(range(j, k))
    return out



def _finite(x):
    """有限实数（bool 不算）。"""
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


_QPC_FREQ = None
_QPC_PROBED = False


def qpc_frequency():
    """QPC 每秒计数（Windows）；非 Windows 或取不到返回 None。"""
    global _QPC_FREQ, _QPC_PROBED
    if not _QPC_PROBED:
        if os.name != "nt":
            _QPC_PROBED = True
            return None
        try:
            import ctypes
            c = ctypes.c_int64()
            if ctypes.windll.kernel32.QueryPerformanceFrequency(
                    ctypes.byref(c)) and c.value > 0:
                _QPC_FREQ = int(c.value)
                _QPC_PROBED = True
        except Exception:                   # 取不到：下次调用再试
            _QPC_FREQ = None
    return _QPC_FREQ


def qpc_now():
    """当前 QPC 计数。跟 time.perf_counter() 是同一条时间轴（Windows）。"""
    if qpc_frequency() is None:
        return None
    try:
        import ctypes
        c = ctypes.c_int64()
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(c))
        return int(c.value)
    except Exception:                       # noqa: BLE001
        return None


@dataclass(frozen=True)
class Estimate:
    """一次 T0 反推的结果。

    `zero_at` 只在 `trust=True` 时才有值；`zero_at_raw` 永远有值（诊断用，
    不许拿去掐点）。`admissible` 是 gate() 基础档的同口径判断。
    """

    zero_at: float | None = None        # 归零时刻（perf_counter 秒），不可信为 None
    zero_at_raw: float | None = None    # 未经过滤的中点，仅诊断
    zero_at_qpc: int | None = None      # 同一时刻换算成 QPC 计数，不可信为 None
    zero_at_ls: float | None = None     # 纯最小二乘对照值（诊断 / 复核）
    width: float | None = None          # 可行区间宽度，误差 ≤ width / 2
    half_width: float | None = None
    slope: float | None = None          # 拟合斜率，正常应为 -1（读数/秒）
    resid_rms: float | None = None      # 内点相对拟合线的残差 RMS
    n: int = 0                          # 收到的读数
    n_inliers: int = 0
    n_outliers: int = 0
    n_ticks: int = 0                    # 内点里出现过几个不同的读数（跳秒次数）
    span: float = 0.0                   # 内点的时间跨度
    first_t: float | None = None
    last_t: float | None = None
    mask: tuple = ()                    # 每条读数是否被判为内点（按喂入顺序）
    trust: bool = False
    frozen: bool = False
    strategy: str = ""
    reason: str = ""
    reason_code: str = ""

    @property
    def admissible(self):
        """与 gate() 基础档同口径：可信 + 半宽 ≤10ms + 稠密流 ≥100 条/≥1s；
        成对喂法（每次跳变 2 条）用 ≥12 条 + 跨度 ≥5s 作为等价强度。"""
        if not (self.trust and self.half_width is not None
                and self.half_width <= 0.010):
            return False
        if self.n_inliers >= 100 and (self.span or 0.0) >= 1.0:
            return True
        return self.n_inliers >= 12 and (self.span or 0.0) >= 5.0


def _runs_of(obs):
    """把 (t, s) 序列切成「同一读数的连续段」。时间乱序的帧各算新段。"""
    runs = []
    for i, (t, s) in enumerate(obs):
        if (runs and runs[-1]["s"] == s
                and t >= runs[-1]["t1"] - 1e-9):
            r = runs[-1]
            r["t1"] = t
            r["i1"] = i
            r["count"] += 1
        else:
            runs.append({"s": s, "t0": t, "t1": t,
                         "i0": i, "i1": i, "count": 1})
    for r in runs:
        r["dur"] = r["t1"] - r["t0"]
    return runs


def _clusters(obs, max_gap=0.15):
    """按读数分组，返回 {s: [(t0, t1)]}；组内相邻帧间隔 ≤ max_gap 连成簇。

    正常数据里同一个读数会持续近 1s，但 ±1 错读会把它打成碎片，所以不能
    只看「连续段」。这里把同读数帧按时间排序后，间隔 ≤ 0.15s 的并成一簇，
    用来判断「这个读数是否曾经稳定地出现过」以及「回跳段有多长」。
    """
    by = {}
    for t, s in obs:
        by.setdefault(s, []).append(t)
    out = {}
    for s, ts in by.items():
        ts.sort()
        cl = [[ts[0], ts[0]]]
        for t in ts[1:]:
            if t - cl[-1][1] <= max_gap:
                cl[-1][1] = t
            else:
                cl.append([t, t])
        out[s] = cl
    return out


def _rate_violation(runs, win, min_count=5):
    """读数速率硬校验：不许变大；掉 k 格至少需要 k-0.5 秒。

    只看「稳定段」（段内 ≥ min_count 帧）：1~3 帧的抖动段不参与，否则正常
    数据会被单帧 -1 错读误判成「掉 2 格 / 8ms」。这一条专门挡「≤1.5 秒内
    整体 -2 格」这类时间轴不连续：200 帧正常读数，第 100 帧起整体 -2，
    交集能算出一个又窄又偏 2 秒的 T0，必须先否决。
    """
    stable = [r for r in runs if r["count"] >= min_count]
    for a, b in zip(stable, stable[1:]):
        ds = a["s"] - b["s"]          # 正 = 读数下降的格数
        dt = b["t0"] - a["t0"]
        if ds < 0:                    # 倒计时不会往回走
            return True
        if ds >= 2 and dt < (ds - 0.5) * win:
            return True
    return False


def _pair_brackets(obs, win):
    """跳变成对夹逼：跳变前最后一帧 `(t_a, S+1)` + 跳变后第一帧 `(t_b, S)`。

    每条成对约束给出 `T0 ∈ [t_a + S + 1, t_b + S + 1)`，宽度 ≈ 两帧间隔
    （8ms 采样下 5~15ms）。6 个跳变共 6 对 12 条读数，交集能收到 1~3ms。
    只喂跳变沿（没有前一帧）时相邻跳变相隔 1s，超出 `EST_BRACKET_GAP_S`，
    这里自然一对都拿不到 —— 那种数据确实无法锁死 T0。
    """
    out = []
    for i in range(len(obs) - 1):
        (ta, sa), (tb, sb) = obs[i], obs[i + 1]
        if sa - sb != 1:
            continue
        gap = tb - ta
        if gap < 0.0 or gap > EST_BRACKET_GAP_S:
            continue
        lo = ta + sa
        hi = tb + sb + win
        if hi > lo:
            out.append((lo, hi, (i, i + 1)))
    return out


def _lattice_ok(sel, obs):
    """成对时刻必须落在整数秒格点上（±1 错读造的假成对落在 tick 中间）。

    用跳变后第一帧的时刻 `t_b` 作跳变时刻的代理：真跳变严格每 1.0s 一次，
    `t_b` 最多比真跳变晚一帧；而「跳变前一帧」可能因为丢帧离跳变点很远，
    成对区间中点会被拉偏几十毫秒 —— 那个偏移不影响区间本身，不能拿中点判。
    """
    tms = sorted(obs[b][0] for _lo, _hi, (_a, b) in sel)
    base = tms[0]
    for x in tms[1:]:
        d = x - base
        r = round(d)
        if r < 1 or abs(d - r) > EST_BRACKET_LATTICE_TOL_S:
            return False
    return True


def _pair_interval(obs, win):
    """在跳变成对里取最大共识区间（RANSAC），再过整数格点校验。

    单独一对可能被 ±1 错读带偏；真成对全部聚在 T0 附近，假成对散在 ±1 秒
    各处，所以先找被最多成对覆盖的点，再要求这些成对同时满足格点校验。
    """
    br = _pair_brackets(obs, win)
    if len(br) < EST_BRACKET_MIN_PAIRS:
        return None, None, []
    events = []
    for lo, hi, _idx in br:
        events.append((lo, 1))
        events.append((hi, -1))
    events.sort()
    cur = best = 0
    point = None
    for x, d in events:
        cur += d
        if cur > best:
            best, point = cur, x
    if point is None or best < EST_BRACKET_MIN_PAIRS:
        return None, None, []
    sel = [b for b in br if b[0] <= point <= b[1]]
    if len(sel) < EST_BRACKET_MIN_PAIRS or not _lattice_ok(sel, obs):
        return None, None, []
    lo = max(b[0] for b in sel)
    hi = min(b[1] for b in sel)
    if hi <= lo:
        return None, None, []
    keep = sorted({i for b in sel for i in b[2]})
    return lo, hi, keep


class T0Estimator:
    """用「读数 + 帧时刻」反推归零时刻 T0。

    用法（每个 OCR 帧都喂，不要只在值变化时喂 —— 每帧都是一个约束）::

        est = T0Estimator()
        for 帧:
            sec = parse_words(...)
            if sec is not None:
                est.observe(sec, 帧捕获时刻)      # 不是 OCR 完成时刻
            e = est.estimate()
            if e.trust:                           # 无值语义：不可信时 zero_at 是 None
                # e.zero_at / e.half_width / e.zero_at_qpc 交给调度器
                ...

    `t` 必须是**该帧被捕获时**的 time.perf_counter()。用 OCR 完成时刻会
    把识别耗时算进读数与时刻的对齐里，整条线平移同样的量（可用 `lag_s`
    校掉，但不如一开始就传对）。

    `lag_s`：显示链路滞后。正值表示画面比真实剩余时间「晚」 lag 秒，估计
    会相应前移。默认 0，需要真机标定。

    `strategy`：`"auto"`（默认：每帧都喂 → 交集；只喂跳变沿 → 只用跳变
    成对夹逼，没有成对就明确不可信）、`"consensus"`（强制交集，A/B 用）、
    `"sparse_safe"`（稀疏数据一律不可信，A/B 用）。
    """

    def __init__(self, maxlen=EST_MAXLEN, window_s=EST_WINDOW_S,
                 reset_jump_s=EST_RESET_JUMP_S, min_trust_n=EST_MIN_TRUST_N,
                 lag_s=0.0, strategy="auto",
                 freeze_run_s=EST_FREEZE_RUN_S, model_tol_s=EST_MODEL_TOL_S):
        self.window = float(window_s)
        self.reset_jump_s = float(reset_jump_s)
        self.min_trust_n = int(min_trust_n)
        self.lag = float(lag_s)
        self.maxlen = int(maxlen)
        self.freeze_run_s = float(freeze_run_s)
        self.model_tol_s = float(model_tol_s)
        self.strategy = strategy if strategy in (
            "auto", "consensus", "sparse_safe") else "auto"
        self._buf = deque(maxlen=self.maxlen)
        self.reset()

    def reset(self):
        self._buf.clear()
        self._last_reading = None
        self._last_t = None
        self._last_change_t = None
        self._note = ""

    def observe(self, reading, t=None):
        """喂一次读数。返回是否接收。

        所有数值入口先做有限性校验：inf / NaN / 非整数读数一律拒绝，不抛。
        读数突然比上一次大 EST_RESET_JUMP_S 以上按「换目标」清空；时间戳
        倒退 >0.5s（NTP 后跳 / 手工改表）或前跳 >8s（长时间断喂）按「换
        时间轴」清空 —— 两段轴混在同一个缓冲里会让交集毫无意义。
        """
        if reading is None or isinstance(reading, bool):
            return False                   # bool 是 int 的子类，不能当读数
        try:
            f = float(reading)
        except (TypeError, ValueError, OverflowError):
            return False
        if (not math.isfinite(f)) or f < 0 or f > 1e9:
            return False                   # inf/NaN/离谱大数一律拒绝
        if f != int(f):
            return False                   # OCR 只会给整数；不静默截断 6.7→6
        s = int(f)
        if t is None:
            t = time.perf_counter()
        else:
            try:
                t = float(t)
            except (TypeError, ValueError, OverflowError):
                return False
        if not math.isfinite(t) or abs(t) > 1e9:
            return False                   # NaN/inf/离谱时间戳一律拒绝

        if self._last_t is not None:
            dt = t - self._last_t
            if dt < -EST_CLOCK_BACK_S:
                self.reset()
                self._note = "clock_backward"
            elif dt > EST_CLOCK_GAP_S:
                self.reset()
                self._note = "clock_gap"
        if (self._last_reading is not None
                and s > self._last_reading + self.reset_jump_s):
            self.reset()
            self._note = "target_switch"

        if s != self._last_reading:
            self._last_change_t = t
        self._buf.append((t, s))
        self._last_t = t
        self._last_reading = s
        return True

    # ------------------------------------------------------------ 估计
    def estimate(self, tolerance_s=EST_DEFAULT_TOL_S, want_qpc=True):
        raw = list(self._buf)
        n = len(raw)
        strat = self.strategy
        if n == 0:
            return Estimate(reason="还没有读数", reason_code="no_data",
                            strategy=strat)

        # 缓冲里理论上都是 observe 校验过的值；再兜一层，防外部直接塞 deque
        obs = [(float(t), int(s)) for t, s in raw
               if _finite(t) and _finite(s)]
        if not obs:
            return Estimate(reason="读数全部无效", reason_code="bad_input",
                            strategy=strat)
        win = self.window

        # 时间轴乱序（缓冲里出现倒退）→ 不去猜，直接不可信
        bad_time = any(obs[i + 1][0] < obs[i][0] - EST_CLOCK_BACK_S
                       for i in range(len(obs) - 1))

        runs = _runs_of(obs)
        n_runs = len(runs)
        # 每段平均帧数：整帧都喂 → >>2；只喂「跳变前一帧 + 跳变后第一帧」→
        # 1.4~1.6；只喂跳变沿（没有前一帧）→ 恒为 1.0。前者走交集，中者走
        # 跳变成对夹逼，后者明确不可信 —— 不拿 ±495ms 的数去糊弄上层。
        pair_ratio = (len(obs) / n_runs) if n_runs else 0.0
        sparse = pair_ratio < 2.0
        has_pairs = pair_ratio >= EST_PAIR_RATIO_MIN

        # ---- P0-1 冻结：同一读数持续太久 / 一次跳变都没有 ----
        n_ticks_all = len({s for _t, s in obs})
        longest = max(runs, key=lambda r: r["dur"]) if runs else None
        frozen = False
        if n_ticks_all < EST_MIN_TICKS:
            frozen = True
        elif not sparse and longest is not None:
            # 段长判据只对「整帧喂入」成立：成对喂法里同一个读数本来就要
            # 横跨约 1.0 秒（跳变后第一帧 → 下一次跳变前一帧），段长恒
            # ≈win，拿它判会把正常成对数据全判成冻结。冻结在成对流里表现
            # 为「没有新的跳变」→ 由 is_stale / due() 的停更判据负责。
            if longest["dur"] >= win + 1e-6:
                frozen = True
            elif (longest["dur"] > self.freeze_run_s
                  and longest["count"] / max(1, len(obs)) >= EST_FREEZE_SHARE):
                frozen = True

        # ---- 速率硬校验：只看稳定段（≥5 帧）；1~3 帧的抖动段不参与，
        # 否则正常数据会被单帧 -1 错读误判成「-2 格/8ms」。----
        rate_bad = _rate_violation(runs, win)

        # ---- 交集 ----
        lo = hi = None
        keep = []
        consensus_mid = None
        thin_edge = False
        if sparse:
            if len(obs) == 1:
                # 只有一个读数：给个 ±0.5s 的粗估（只进 zero_at_raw），
                # 可信判定照旧会被 too_few / short_span 拒掉
                lo = obs[0][0] + obs[0][1]
                hi = lo + win
                keep = [0]
            elif strat == "auto" and has_pairs:
                lo, hi, keep = _pair_interval(obs, win)
            # 只喂跳变沿（无前一帧）或 sparse_safe：明确不可信（A/B 用）
        elif len(obs) >= 2:
            v_of = [t + s for t, s in obs]
            # 读数变大 = 物理上不可能，先整条剔掉，再进共识/交集。
            # 不做成硬否决：OCR 单帧 +1 很常见，剔掉即可，不必整窗报废。
            rise_bad = _rise_outliers(obs, win)
            idx_all = [i for i in range(len(obs)) if i not in rise_bad]

            def _consensus(idx):
                if not idx:
                    return []
                sv = sorted((v_of[i], i) for i in idx)
                best_k, best_c = 0, 0
                j = 0
                mm = len(sv)
                for k in range(mm):
                    if j < k:
                        j = k
                    while j < mm and sv[j][0] < sv[k][0] + win - 1e-12:
                        j += 1
                    if j - k > best_c:
                        best_c, best_k = j - k, k
                return [i for _v, i in sv[best_k:best_k + best_c]]

            def _intersect(idx):
                if not idx:
                    return None, None, []
                vv = [(v_of[i], i) for i in idx]
                return max(v for v, _ in vv), min(v for v, _ in vv) + win, vv

            def _prune(idx):
                for _ in range(6):
                    lo_, hi_, vv = _intersect(idx)
                    if lo_ is None or hi_ >= lo_ or len(idx) <= 2:
                        break
                    med = sorted(v for v, _ in vv)[len(vv) // 2]
                    drop = max(vv, key=lambda p: abs(p[0] - med))[1]
                    idx = [i for i in idx if i != drop]
                return idx

            core = _prune(_consensus(idx_all))
            # 独立复核基线：最大共识窗自己的交集中点。
            # 交集管线精细但会被「两条相邻 +1 错读」钉成假窄点；共识窗
            # 对孤立错读不敏感，两者的中点必须对齐。
            _c_lo, _c_hi, _ = _intersect(_consensus(idx_all))
            if _c_lo is not None and _c_hi > _c_lo:
                consensus_mid = (_c_lo + _c_hi) / 2.0 - self.lag
            lo, hi, _vv = _intersect(core)
            if lo is not None:
                z = (lo + hi) / 2.0
                for margin in (0.60, 0.15, 0.05):
                    cand = [i for i in idx_all
                            if -margin <= z - v_of[i] <= win + margin]
                    if len(cand) < max(4, len(core) // 2):
                        break
                    c2 = _prune(_consensus(cand))
                    l2, h2, _vv2 = _intersect(c2)
                    if l2 is not None and h2 > l2:
                        z = (l2 + h2) / 2.0
                    core = c2

                keep = _prune(core)
                lo, hi, vv = _intersect(keep)

                # 边界孤立点防护：单条 +1 错读恰好落在相位 1-u_max 处会把
                # 区间钉成假窄点。只有孤立边缘才换次极值（孤立 = 邻距 >
                # max(5×中位间隔, 10ms)）。
                if keep and len(vv) >= 12:
                    sv = sorted(v for v, _ in vv)
                    gaps = [sv[i + 1] - sv[i] for i in range(len(sv) - 1)]
                    med_gap = sorted(gaps)[len(gaps) // 2]
                    gate = max(5.0 * med_gap, 0.010)
                    if sv[-1] - sv[-2] > gate:
                        lo = sv[-2]
                    if sv[1] - sv[0] > gate:
                        hi = sv[1] + win

                # 窄区间支撑校验：只有当区间被「两个不同读数的交界」夹住时
                # 才查。真跳变意味着新区间会在缓冲里持续很多帧，假跳变只有
                # 1~2 帧。用「同读数的内点条数」而不是「连续段长度」——错读
                # 会把一段正常读数打成碎片，段长不可靠，同值计数可靠。
                if (keep and lo is not None and hi is not None
                        and hi < lo + win):
                    imax = max(keep, key=lambda i: v_of[i])
                    imin = min(keep, key=lambda i: v_of[i])
                    if obs[imax][1] != obs[imin][1]:
                        ca = sum(1 for i in keep if obs[i][1] == obs[imax][1])
                        cb = sum(1 for i in keep if obs[i][1] == obs[imin][1])
                        thin_edge = min(ca, cb) < EST_MIN_BOUND_FRAMES
                        # 回跳簇复核：高读数一旦已经被一个「稳定出现」
                        # （簇跨度 ≥ EST_MIN_BOUND_SPAN_S）的更小读数否定过，
                        # 之后同一高读数的回跳段只能是一小撮错读。实测
                        # poisson 模型 3 条连续 +1 错读压在真跳变前，LNDS 会
                        # 偏爱错读（计数更长），把区间钉成 21ms 宽、偏 87.9ms
                        # 的假可信；它们的回跳簇只有 40ms，真平台则有几百 ms。
                        if not thin_edge:
                            cl = _clusters(obs)
                            s_hi = obs[imax][1]
                            t_hi = obs[imax][0]
                            neg = [c[0] for v, cls in cl.items() if v < s_hi
                                   for c in cls
                                   if c[1] - c[0] >= EST_MIN_BOUND_SPAN_S
                                   and c[0] <= t_hi]
                            if neg:
                                t_neg = min(neg)
                                seg = [(a, b) for a, b in cl[s_hi]
                                       if b >= t_neg]
                                if seg:
                                    span = (max(b for _a, b in seg)
                                            - max(t_neg,
                                                  min(a for a, _b in seg)))
                                else:
                                    span = 0.0
                                if span < EST_MIN_BOUND_SPAN_S:
                                    thin_edge = True
        width = (hi - lo) if (lo is not None and hi is not None
                              and hi > lo) else None
        mid = ((lo + hi) / 2.0 - self.lag) if width is not None else None
        # 双算法复核：交集中点与共识窗中点分歧过大 = 交集中了假窄点
        split_bad = (consensus_mid is not None and mid is not None
                     and abs(mid - consensus_mid) > EST_AGREE_TOL_S)

        # ---- 线性拟合（内点）：斜率应 -1，残差复核剔除是否干净 ----
        pts = [(obs[j][0], obs[j][1]) for j in keep]
        slope = resid_rms = ls_zero = None
        if len(pts) >= 3:
            mt = sum(t for t, _ in pts) / len(pts)
            ms = sum(s for _, s in pts) / len(pts)
            var = sum((t - mt) ** 2 for t, _ in pts)
            if var > 1e-12:
                cov = sum((t - mt) * (s - ms) for t, s in pts)
                slope = cov / var
                b = ms - slope * mt
                resid_rms = math.sqrt(
                    sum((s - (b + slope * t)) ** 2 for t, s in pts) / len(pts))
                ls_zero = b + win / 2.0 - self.lag

        ts = [t for t, _ in pts]
        span = (max(ts) - min(ts)) if len(ts) >= 2 else 0.0
        n_ticks = len({obs[j][1] for j in keep})

        # ---- 首尾中位数模型校验（停更 / 时间轴单位不符）----
        # 只对整帧喂入的数据做：稀疏成对数据里首尾各 5 条覆盖 2~3 个 tick，
        # 中位数相位天然差 ±1 格，拿它判会误杀；稀疏流的节奏由成对计数 +
        # 整数格点校验负责。
        model_bad = False
        if len(obs) >= 6 and not sparse:
            head = sorted(s for _t, s in obs[:5])[2]
            tail = sorted(s for _t, s in obs[-5:])[2]
            elapsed = obs[-1][0] - obs[0][0]
            if elapsed >= win and abs((head - tail) - elapsed) > self.model_tol_s * win:
                model_bad = True

        # ---- 可信判定 + 机器可读原因码 ----
        reasons = []
        code = "ok"

        def _deny(c, txt):
            nonlocal code
            reasons.append(txt)
            if code == "ok":
                code = c

        if bad_time:
            _deny("bad_time", "时间戳在缓冲里倒退（时钟跳变）")
        if split_bad:
            _deny("split", "交集与共识窗分歧超过 40ms（交集中了假窄点）")
        if thin_edge:
            _deny("thin_edge", "窄区间两端来自 1~2 帧的抖动段（疑似假跳变）")
        if sparse and lo is None:
            if not has_pairs:
                _deny("sparse", "只喂了跳变沿且没有成对的「跳变前一帧」，交集退化")
            else:
                _deny("sparse", f"有效跳变成对不足 {EST_BRACKET_MIN_PAIRS} 对"
                      "（或成对时刻不在整数秒格点上）")
        if len(keep) < self.min_trust_n:
            _deny("too_few", f"有效读数 {len(keep)} 个（需要 ≥ {self.min_trust_n}）")
        if span < EST_MIN_SPAN_S:
            _deny("short_span", f"采样跨度只有 {span * 1000:.0f}ms（需要 ≥ "
                  f"{EST_MIN_SPAN_S * 1000:.0f}ms）")
        if width is None:
            _deny("inconsistent", "可行区间已倒挂（读数互相矛盾）")
        elif width > 2.0 * tolerance_s:
            _deny("wide", f"可行区间 ±{width / 2 * 1000:.1f}ms 超出容差 "
                  f"±{tolerance_s * 1000:.0f}ms")
        if frozen:
            if n_ticks_all < EST_MIN_TICKS:
                _deny("frozen", "整段读数没有出现跳变（界面冻结 / 停更）")
            else:
                dur = longest["dur"] if longest else 0.0
                _deny("frozen", f"同一读数持续 {dur * 1000:.0f}ms（≥ "
                      f"{self.freeze_run_s * 1000:.0f}ms）")
        if rate_bad:
            _deny("model_violation", "读数跳变间隔与 1 格/秒不符（时间轴不连续？）")
        if model_bad:
            _deny("model_violation", f"首尾读数差与经过 {elapsed:.1f}s 不符"
                  "（应约 1 格/秒）")

        reason = "；".join(reasons)
        trust = not reasons
        zero_at = mid if trust else None

        qpc = None
        freq = qpc_frequency()
        if want_qpc and trust and mid is not None and freq:
            p0 = time.perf_counter()
            q = qpc_now()
            p1 = time.perf_counter()
            if q is not None:
                qpc = int(round(q + (mid - (p0 + p1) / 2.0) * freq))

        keep_raw = set(keep)
        mask = tuple(i in keep_raw for i in range(len(obs)))
        return Estimate(
            zero_at=zero_at, zero_at_raw=mid, zero_at_qpc=qpc, zero_at_ls=ls_zero,
            width=width,
            half_width=None if width is None else width / 2.0,
            slope=slope, resid_rms=resid_rms,
            n=len(obs), n_inliers=len(keep), n_outliers=len(obs) - len(keep),
            n_ticks=n_ticks, span=span,
            first_t=min(ts) if ts else None, last_t=max(ts) if ts else None,
            mask=mask, trust=trust, frozen=frozen, strategy=strat,
            reason=reason, reason_code=code,
        )

    def zero_at(self, tolerance_s=EST_DEFAULT_TOL_S):
        """归零时刻（perf_counter 秒）。不可信 / 没有读数 → None，不返回猜值。"""
        return self.estimate(tolerance_s=tolerance_s, want_qpc=False).zero_at

    def remaining(self, t=None, tolerance_s=EST_DEFAULT_TOL_S):
        """剩余秒数。不可信时 None；T0 已过期会返回负数（上层自己判断）。"""
        z = self.zero_at(tolerance_s=tolerance_s)
        if z is None:
            return None
        if t is None:
            t = time.perf_counter()
        else:
            try:
                t = float(t)
            except (TypeError, ValueError, OverflowError):
                return None
        if not math.isfinite(t):
            return None
        return z - t

    def due(self, t=None, lead=0.0, tolerance_s=EST_DEFAULT_TOL_S):
        """到点了吗。**估计不可信时永远 False** —— due() 自己挡住冻结 /
        无跳变 / 模型违背 / 区间过宽，不依赖调用方记得先调 gate()。"""
        e = self.estimate(tolerance_s=tolerance_s, want_qpc=False)
        if not (e.trust and e.zero_at is not None):
            return False
        if t is None:
            t = time.perf_counter()
        else:
            try:
                t = float(t)
            except (TypeError, ValueError, OverflowError):
                return False
        if not math.isfinite(t):
            return False
        # 双保险：冻结 / 停更时即使缓存了旧估计也不点。
        # 注意不能用「距上次跳变 > 0.9s」这种逐次判据：正常读数本来就要
        # 持续 1.0s，归零前那一刻距上次跳变恰好 ≈1s，会把正常到点全挡掉。
        # 冻结由 estimate 的整窗分析判定（frozen 已在 trust 里），这里只补
        # 长停更（> STALE_S）这一条。
        if e.frozen or self.is_stale(t):
            return False
        return t + lead >= e.zero_at

    def trusted(self, tolerance_s=EST_DEFAULT_TOL_S):
        return self.estimate(tolerance_s=tolerance_s, want_qpc=False).trust

    def is_stale(self, t=None, stale_s=None):
        """显示值多久没变（界面切走 / 被买走）。与 `Clock.is_stale` 同口径。

        注意：偶发 OCR 失败不会误报（看的是「值」而不是「帧」）。
        """
        if self._last_change_t is None:
            return False
        if t is None:
            t = time.perf_counter()
        else:
            try:
                t = float(t)
            except (TypeError, ValueError, OverflowError):
                return False
        if not math.isfinite(t):
            return False
        return (t - self._last_change_t) > (STALE_S if stale_s is None else stale_s)

    def is_frozen(self, t=None, run_s=None):
        """显示值卡死了多久（界面冻结 / 被买走 / 切页）。

        默认门槛 1.20s：一次正常读数持续 1.0s，归零前那一刻距上次跳变恰好
        ≈1s，门槛低于 1s 会把正常倒计时判成冻结。0.96~1.15s 的短暂冻结由
        `estimate()` 的整窗分析负责（会被判 wide / inconsistent / frozen）。
        """
        if self._last_change_t is None:
            return False
        if t is None:
            t = time.perf_counter()
        else:
            try:
                t = float(t)
            except (TypeError, ValueError, OverflowError):
                return False
        if not math.isfinite(t):
            return False
        gate = EST_STUCK_S if run_s is None else float(run_s)
        return (t - self._last_change_t) > gate

    def info(self):
        """给界面/日志看的快照。"""
        e = self.estimate(want_qpc=False)
        return {
            "last": self._last_reading,
            "zero_at": e.zero_at,
            "zero_at_raw": e.zero_at_raw,
            "zero_at_qpc": e.zero_at_qpc,
            "n": e.n,
            "inliers": e.n_inliers,
            "outliers": e.n_outliers,
            "ticks": e.n_ticks,
            "half_width_ms": None if e.half_width is None else round(e.half_width * 1000, 1),
            "trust": e.trust,
            "admissible": e.admissible,
            "frozen": e.frozen,
            "reason": e.reason,
            "reason_code": e.reason_code,
            "note": self._note,
        }


# ---------------------------------------------------------------- 到点卡点
#
# 这两个函数只管「等」，不碰倒计时逻辑 —— 时钟和 sleep 都是参数，所以能在
# 纯模拟环境里跑（假时钟推进即可，不需要真实等待）。

def gate(e, need_10ms=False, lag_calibrated=False):
    """点击前的硬门槛（分支 C 定的数，上层必须照这个卡）。

    返回 `(ok, reason)`。不满足就不许点 —— 宁可放弃一次，也不要在不确定的
    时刻乱点。

    基础门槛：`trust`、`half_width <= 10ms`、`n_inliers >= 100`、`span >= 1.0s`。
    成对喂法（每次跳变只交「前一帧 + 后一帧」，整批 10~40 条）改判
    `n_inliers >= 12` 且 `span >= 5s` —— 12 条 = 6 次跳变，和 100 条整帧流
    提供同样多的整数秒格点约束。
    掐 ±10ms 时再加：`n_inliers >= 160`、`span >= 1.3s`、且 `lag_s` 已标定；
    成对档对应 `n_inliers >= 24` 且 `span >= 10s`。
    """
    if not e.trust:
        return False, e.reason or "estimate_untrusted"
    if e.half_width is None or e.half_width > 0.010:
        hw = "None" if e.half_width is None else f"{e.half_width * 1000:.1f}ms"
        return False, f"half_width {hw} > 10ms"
    if e.n_inliers < 100:
        # 稠密流门槛；成对喂法每条读数就是一次跳变的半边，数量天然少一个量级
        if not (e.n_inliers >= 12 and (e.span or 0.0) >= 5.0):
            return False, (f"n_inliers {e.n_inliers} < 100"
                           "（成对档要求 ≥12 条且跨度 ≥5s）")
    if (e.span or 0.0) < 1.0:
        return False, f"span {e.span:.3f}s < 1.0s"
    if need_10ms:
        if e.n_inliers < 160:
            if not (e.n_inliers >= 24 and (e.span or 0.0) >= 10.0):
                return False, (f"n_inliers {e.n_inliers} < 160（±10ms 档；"
                               "成对档要求 ≥24 条且跨度 ≥10s）")
        if (e.span or 0.0) < 1.3:
            return False, f"span {e.span:.3f}s < 1.3s（±10ms 档）"
        if not lag_calibrated:
            return False, "lag_s 未标定（±10ms 档）"
    return True, ""


def wait_until(now, target, mode="hybrid", spin_window=0.0015,
               sleep_fn=time.sleep):
    """卡到 `target`，返回实际返回时刻（由 `now()` 读出）。

    `mode="spin"`   全程忙等，最准，吃满一核；
    `mode="hybrid"` 先睡到 `target - spin_window`，再自旋收尾。

    不能靠 `time.sleep` 收尾：Windows 上最小粒度 1~15.6ms，抖动远大于
    目标精度。`spin_window` 取 1.5~3ms（分支 B 推荐 3ms），自旋段内应把
    线程提到 TIME_CRITICAL 并在 finally 还原 —— 这部分在真机阶段做。
    """
    if mode == "hybrid":
        while True:
            left = target - now()
            if left <= spin_window:
                break
            sleep_fn(max(0.0, left - spin_window))
    while True:
        t = now()
        if t >= target:
            return t
