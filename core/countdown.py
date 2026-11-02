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
import re
import time

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
