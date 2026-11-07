# -*- coding: utf-8 -*-
"""流程级基线测量：跑**真实控制流**（引擎里的原方法，一行不改），
但硬件动静全部换成本机实测单价 —— 抓屏、OCR、模板匹配、点击、sleep。

为什么这样测：
  没有游戏窗口时，唯一诚实的做法是把「一次抓屏 / 一次 OCR / 一次匹配」
  在本机量准（probe_cost.py / probe_cost2.py），然后在同一个控制流上把
  这些单价累加。控制流是真的（循环次数、判定顺序、早退条件都是引擎代码），
  单价是真的（本机实测），只有像素内容是脚本化的。

被测对象：perf/base/core（冻结快照）；同一份脚本对 perf/_ref 跑一次 = 基线。

用法：
    .venv\\Scripts\\python.exe perf\\bench\\flow_bench.py --root perf/base --run 200
"""
import argparse
import importlib
import json
import math
import os
import random
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))


# ---------------------------------------------------------------- 单价表
# 来自 perf/out/costs.json / costs_detail.json（本机实测，2026-11-07）
UNIT = {
    # kind: (p50_ms, p95_ms)
    "grab_1340x730": (9.30, 10.9),
    "grab_300x80": (1.85, 2.3),
    "grab_400x60": (1.90, 2.4),
    "grab_540x260": (3.40, 4.1),
    "grab_100x55": (1.70, 2.1),
    "grab_390x32": (1.70, 2.1),
    "grab_230x97": (1.75, 2.1),
    "grab_full": (19.1, 21.6),
    "grab_340x30": (1.70, 2.1),
    "ocr_clock": (13.4, 16.9),        # 无字：13ms 起；有字实测均值 ~77ms
    "ocr_clock_text": (58.0, 96.0),
    "ocr_balance": (101.0, 146.0),
    "ocr_reward": (335.0, 470.0),
    "ocr_skip": (30.0, 44.0),
    "ocr_hint": (25.0, 36.0),
    "ocr_cont": (60.0, 92.0),
    # 编号：抓 340×30 + 3× 放大 + 一级推理。旧值 (25, 36) 是**假设表**，
    # 比现实乐观一倍。当前树真机实测（perf/bench/real_detail_wait.py，
    # perf/out/p1b_real_merged.json）：p50 53.99 / p95 63.96 / max 64.71ms；
    # 守门那台是 47.46 / 55.06 / 56.29ms。两台都取到，这里用**更差的那台**
    # （53.99 / 63.96），台架宁可比现实悲观，不许更乐观。
    # 改这一项的影响：基线流程 0 次编号读（不受影响）；01b 的命中路径
    # 从「约 27ms/拍」变成「约 54ms/拍」，收益随之缩水（见 _perf_flow.md §9）。
    # 复算历史数字：设 PERF_OCR_SERIAL=25,36 即回到旧假设表。
    "ocr_serial": (53.99, 63.96),
    "ocr_fullframe": (1350.0, 2100.0),   # 全屏 read_words（实测 450~1366，取中位）
    "match": (1.88, 2.3),
    "click": (0.53, 0.84),
    "locate_cards": (11.0, 12.6),
    "changed_cards": (3.8, 4.4),
    "popup_scan": (450.0, 853.0),
    "imwrite": (4.4, 6.1),
    "records_add": (5.0, 7.0),
    # 列表发现三段，单价取 _baseline_r0.md §1.1（真实时序、带载上限）：
    # 抓列表区 12.19 / 14.42、稳态缓存扫描 0.21 / 0.28、挑行 0.04 / 0.05。
    "grab_list": (12.19, 14.42),
    "scan_cached": (0.21, 0.28),
    "pick_row": (0.04, 0.05),
}

# ------------------------------------------------------------ 当前口径（现状）
# 「七功能 + Q1–Q5」合并树的分项（hit，p50），来源：
#   _feature_merge_report.md §5.1 / perf/out/flow_merged*.json（run=40）。
#
# 边界（重要，别再把两段混成一个数）：
#   hit 单件 = entry（进详情等待 610.43）+ fire_all（购买键→收奖励 3743.90）
#   3743.90 不含 entry；全链路 = 610.43 + 3743.90 ≈ 4354.33（p50 相加，仅定位）
#   列表发现（抓帧/扫列表/挑行 ≈ 12.44）台架 hit 模式不测，主指标单列。
#
# 各分项独立分位数采样，所以严格的主指标必须逐件相加后再取分位；
# 台架每次 run 落两档样本：
#   item_e2e      = entry + fire_all（旧口径，缺 list_prefix）
#   item_e2e_full = list_prefix + entry + fire_all（§10.4 唯一主指标）
# 4354.33 是「各段 p50 相加」的定位常量，不是分位口径，别当实测值引用。
CURRENT_BREAKDOWN = {
    "source": "_feature_merge_report.md §5.1 / perf/out/flow_merged*.json",
    "tree": "七功能 + Q1–Q5 合并树（2916 口径的 flow/01 + flow/04 已退回/砍掉）",
    "core_digest": "22823fc89c3fbfa4",
    "fire_all_ms": 3743.90,
    "entry_ms": 610.43,
    "list_prefix_ms": 12.44,
    "end_to_end_ms": 4354.33,       # = entry + fire_all（p50 相加，仅定位）
    "end_to_end_kind": "sum_of_p50_not_quantile",
    "items": [
        {"key": "sub_guard_insufficient", "name": "余额不足守卫",
         "ms": 2092.75, "pct_fire": 55.9, "pct_full": 48.1,
         "note": "flow/04 预筛已砍，回到 3 次整屏 popup.scan + 350 ms 间隔"},
        {"key": "sub_wait_dialog_unlock", "name": "等弹窗解锁",
         "ms": 1092.0, "pct_fire": 29.2, "pct_full": 25.1,
         "note": "相位保护 1 s + 倒计时轮询，语义红线"},
        {"key": "entry", "name": "进详情等待（段外）",
         "ms": 610.43, "pct_fire": None, "pct_full": 14.0,
         "note": "flow/01 已退回，仍是 sleep(min(0.55, timeout)) + 读钟"},
        {"key": "sub_close_reward", "name": "收奖励",
         "ms": 142.37, "pct_fire": 3.8, "pct_full": 3.3,
         "note": "Q4 条件等待生效"},
        {"key": "sub_bought_dialog_hit", "name": "成交校验（hit）",
         "ms": 31.0, "pct_fire": 0.8, "pct_full": 0.7,
         "note": "Q2 先读「跳过」"},
        {"key": "sub_bought_dialog_lost", "name": "成交校验（lost）",
         "ms": 1550.0, "pct_fire": None, "pct_full": None,
         "note": "Q3 六拍前紧后松；1550 是 6 拍合计，台架只出单拍 p50，不比 delta"},
        {"key": "sub_record", "name": "记账 + 存图",
         "ms": 23.7, "pct_fire": 0.6, "pct_full": 0.5,
         "note": "同步执行，异步化已否决"},
    ],
    "others_ms": 362.08,
    "others_pct_fire": 9.7,
    "others_pct_full": 8.3,
    "sum_note": ("fire_all 3743.90 已含守卫/解锁/收奖励/记账；entry 610.43 在 fire 段之外，"
                 "所以 2092.75+1092+610.43 的 1.4% 巧差没有校验意义。"
                 "全链路 = entry + fire_all ≈ 4354.33（p50 相加仅定位，严格值看 item_e2e）"),
}

# 复算历史收益用：PERF_OCR_SERIAL="25,36" 回到旧假设表，默认用上面的实测值。
_env_serial = os.environ.get("PERF_OCR_SERIAL", "").strip()
if _env_serial:
    _p50, _p95 = (float(x) for x in _env_serial.split(","))
    UNIT["ocr_serial"] = (_p50, _p95)


def _lognorm_params(p50, p95):
    m = math.log(p50)
    s = (math.log(p95) - m) / 1.645
    return m, max(1e-3, s)


class Sampler(object):
    def __init__(self, rng):
        self.rng = rng

    def take(self, kind):
        p50, p95 = UNIT[kind]
        m, s = _lognorm_params(p50, p95)
        return math.exp(self.rng.gauss(m, s))


# ---------------------------------------------------------------- 虚拟时钟
class VClock(object):
    """引擎里的 time 全换成它：sleep 只推进虚拟时间，但按实测加上
    Windows 定时器溢出（1ms 睡成 1.55ms、5ms 睡成 5.4ms）。"""

    def __init__(self, sampler):
        self.t = 0.0
        self.sampler = sampler
        self.sleeps = []
        self.ops = []

    def perf_counter(self):
        return self.t

    def monotonic(self):
        return self.t

    def time(self):
        return 1.631e9 + self.t

    def sleep(self, d):
        d = max(0.0, float(d))
        self.sleeps.append(d)
        self.t += d + (0.00055 if d > 0 else 0.0)

    def charge(self, kind, dur=None):
        d = self.sampler.take(kind) if dur is None else dur
        self.ops.append((self.t, d, kind))
        self.t += d / 1000.0
        return d

    def strftime(self, fmt, *a):
        import time as _t
        return _t.strftime(fmt, *_t.localtime(*a) if a else _t.localtime())

    def localtime(self, *a):
        import time as _t
        return _t.localtime(*a)


# ---------------------------------------------------------------- 假帧 / 假硬件
class FakeFrame(object):
    __slots__ = ("kind", "h", "w")

    def __init__(self, kind, w=100, h=40):
        self.kind = kind
        self.w, self.h = w, h

    @property
    def shape(self):
        return (self.h, self.w, 3)

    @property
    def size(self):
        return self.w * self.h * 3


AREA_KIND = {
    # px 面积 -> 抓屏单价
    "list": "grab_1340x730",
    "clock": "grab_300x80",
    "balance": "grab_400x60",
    "buy": "grab_300x80",
    "confirm": "grab_230x97",
    "reward": "grab_540x260",
    "skip": "grab_100x55",
    "hint": "grab_390x32",
    "cont": "grab_230x97",
    "serial": "grab_340x30",
    "full": "grab_full",
}


class FakeScreen(object):
    def __init__(self, vc, scene, regions):
        self.vc = vc
        self.scene = scene
        self.last = None
        # 归一化区域 → 像素框，用「最近的框」认这次 grab 是哪个区域
        self.boxes = {}
        for name, norm in regions.items():
            self.boxes[name] = self.norm_to_px(norm)

    def resolution(self):
        return (1920, 1080)

    def norm_to_px(self, norm):
        if norm is None:
            return None
        W, H = 1920, 1080
        x, y = int(norm["x"] * W), int(norm["y"] * H)
        w, h = max(1, int(norm["w"] * W)), max(1, int(norm["h"] * H))
        return (x, y, w, h)

    def kind_of(self, px):
        if px is None:
            return "full"
        x, y, w, h = px
        best, bestd = "full", 1e18
        for name, box in self.boxes.items():
            bx, by, bw, bh = box
            d = (abs(x - bx) + abs(y - by) + abs(w - bw) + abs(h - bh))
            if d < bestd:
                best, bestd = name, d
        return best

    def grab(self, px=None):
        kind = self.kind_of(px)
        self.vc.charge(AREA_KIND.get(kind, "grab_full"))
        self.last = kind
        self.scene.on_grab(kind)
        return FakeFrame(kind)

    def close(self):
        pass


class FakeMouse(object):
    def __init__(self, vc, scene):
        self.vc = vc
        self.scene = scene
        self.clicks = 0

    def ensure_foreground(self, hwnd, screen=None, **kw):
        return True

    def click(self, x, y, hold=None, **kw):
        self.vc.charge("click")
        self.clicks += 1
        self.scene.on_click(x, y)

    def move(self, x, y, **kw):
        self.vc.charge("click", 0.05)


class Scene(object):
    """脚本化场面：谁在什么时候出现。

    mode:
      miss —— 点 8 秒没有确认弹窗，余额不变，校验六拍全空
      hit  —— 第 1 下点出确认弹窗，解锁后确认，结算弹窗立刻出来
    """

    def __init__(self, vc, mode):
        self.vc = vc
        self.mode = mode
        self.buy_ready = 0          # 第几次 grab(buy) 起按钮可见
        self.buy_grab = 0
        self.confirm_at_click = 1 if mode == "hit" else None
        self.confirm_visible = False
        self.unlock_polls = 2
        self.unlock_poll = 0
        self.reward = False
        self.showcase = False
        self.skip_visible = False
        self.balance = 1204.0
        self.balance_after = 1204.0
        self.serial_ready = 0
        self.serial_grab = 0
        self.cont_iter = 0
        self.clicks_fire = 0
        self.clock_grab = 0
        self.clock_zero = (mode == "hit")   # hit = 开点时已经解锁；miss = 一直没抢到
        self.t_first_click = None
        self.back_list = False
        self.phase = "entry"
        self.confirm_delay = 0.20     # 解锁后 200ms 才弹出「已买到」
        self.confirm_open = False     # 确认弹窗在不在（点「确认」后关掉）
        self.confirmed = False        # 「确认」已经点过了 —— 之后弹窗不再回来
        self.lose = (mode == "lost")  # 点了确认但什么都没拿到（别人先买走）
        self.in_close = False         # 正在走 _close_reward（区分「购买点」与「跳过」）
        self.t_confirm_click = None

    def kind_of(self, px):
        if px is None:
            return "full"
        x, y, w, h = px
        if w >= 900:
            return "list"
        if w >= 400 and h >= 150:
            return "reward"
        if w >= 300 and h <= 70:
            return "balance" if y < 120 else "serial"
        if w >= 300:
            return "clock"
        if w >= 180 and h >= 90:
            return "cont" if y > 900 else "confirm"
        if w >= 90 and h <= 60:
            return "skip" if y > 800 else "clock"
        if h <= 40:
            return "hint"
        return "buy"

    def on_grab(self, kind):
        if kind == "buy":
            self.buy_grab += 1
        if kind == "serial":
            self.serial_grab += 1
        if kind == "clock":
            self.clock_grab += 1

    def on_click(self, x, y):
        if self.in_close:
            if y > 900 and x > 1400:                # 「跳过」：奖励弹窗关掉，展示页还在
                self.reward = False
            elif 900 < x < 1020 and y > 960:        # 「继续」：展示页收掉，回到列表
                self.showcase = False
                self.back_list = True
                self.cont_iter += 1
        elif self.phase == "fire":
            if self.t_first_click is None:
                self.t_first_click = self.vc.t
            if y < 800:                             # 点的是确认框里的「确认」
                self.confirm_open = False           # 弹窗立刻消失
                self.confirmed = True               # 之后不再出现
                self.reward = not self.lose         # 换成奖励弹窗
                self.showcase = not self.lose       # 展示页同时在
                self.t_confirm_click = self.vc.t
        elif self.phase == "close":
            if y > 900 and x > 1400:
                self.reward = False
            elif 900 < x < 1020 and y > 960:
                self.showcase = False
                self.back_list = True
                self.cont_iter += 1
        self.clicks_fire += 1


def make_shims(vc, scene, screen):
    """engine 命名空间里的 upscale / match / to_gray / ocr / popup。"""

    def upscale(img, k):
        vc.charge("match", 0.05 * k)
        return img

    def to_gray(img):
        vc.charge("match", 0.05)
        return img

    def match(hay, needle, **kw):
        kind_h = getattr(hay, "kind", "")
        vc.charge("match")
        # 弹窗在「第一下点购买」之后 confirm_delay 才画出来 —— 早了看不见，
        # 这就是抽签模型里「点早了等于白点」的那一段。
        if (scene.mode in ("hit", "lost") and not scene.confirmed
                and scene.t_first_click is not None
                and vc.t - scene.t_first_click >= scene.confirm_delay):
            scene.confirm_open = True
        if kind_h == "confirm" or (isinstance(needle, str) and "confirm" in needle):
            return [1.0 if scene.confirm_open else 0.0, (10, 10)]
        if kind_h == "buy":
            return [1.0 if scene.buy_grab > scene.buy_ready else 0.0, (10, 10)]
        return [0.0, (0, 0)]

    class FakeOCR(object):
        def __init__(self):
            self.calls = []

        def available(self):
            return True

        def backend_name(self):
            return "bench-scripted"

        def read_words(self, img, **kw):
            kind = getattr(img, "kind", "full")
            self.calls.append(kind)
            if kind in ("list", "full"):
                vc.charge("ocr_fullframe")
                if scene.showcase:
                    return [{"text": "获得", "x": 0}, {"text": "继续", "x": 900}]
                if scene.back_list:
                    return [{"text": "只显示公示期", "x": 0}]
                return []
            if kind == "clock":
                vc.charge("ocr_clock_text")
                head = "00" if scene.clock_zero else "05"
                return [{"text": head, "x": 0}, {"text": "秒后解锁购买", "x": 40}]
            if kind == "reward":
                vc.charge("ocr_reward")
                if not scene.reward:
                    return []
                return [{"text": "获得 枪械外观", "x": 0}, {"text": "拥有：1", "x": 300}]
            if kind == "skip":
                vc.charge("ocr_skip")
                return [{"text": "跳过", "x": 0}] if scene.reward else []
            if kind == "balance":
                vc.charge("ocr_balance")
                return [{"text": "1,204", "x": 0}]
            if kind == "serial":
                vc.charge("ocr_serial")
                return [{"text": "编号 001738261", "x": 0}]
            if kind == "hint":
                vc.charge("ocr_hint")
                return []
            if kind == "cont":
                vc.charge("ocr_cont")
                return [{"text": "继续", "x": 0}] if scene.showcase else []
            vc.charge("ocr_clock")
            return []

        def read_text(self, img, **kw):
            w = self.read_words(img, **kw)
            return "".join(x["text"] for x in w)

        def read_price(self, img, **kw):
            return None

        # _balance 里会回读 ocr.last_number()（上一次数字识别的原始读数）
        def last_number(self, *a, **k):
            return "1204"

    SCENE_TEXT = {
        "clock": [{"text": "05", "x": 0}, {"text": "秒后解锁购买", "x": 40}],
        "balance": [{"text": "1,204", "x": 0}],
        "serial": [{"text": "编号 001738261", "x": 0}],
        "hint": [{"text": "三角币不足", "x": 0}] if scene.reward is False and False else [],
        "cont": [{"text": "继续", "x": 0}],
        "skip": [{"text": "跳过", "x": 0}],
        "reward": [{"text": "获得 枪械外观", "x": 0}, {"text": "拥有：1", "x": 300}],
    }

    class FakePopup(object):
        def scan(self, img, **kw):
            vc.charge("popup_scan")
            return {"found": False, "cancel": None, "text": ""}

    return upscale, to_gray, match, FakeOCR(), FakePopup(), SCENE_TEXT


# ---------------------------------------------------------------- 场景驱动
class _Zero(object):
    """台架专用的空占位：只用来补齐 Engine.__init__ 才会建的那些属性
    （_fire_lag / _hb / _repeat_ctx …）。它不会被写进被测逻辑，只是让
    「真实控制流」能在没有 GUI/线程的环境里跑起来。"""

    def __bool__(self):
        return False

    def __int__(self):
        return 0

    def __float__(self):
        return 0.0

    def __index__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __lt__(self, o):
        return False

    def __gt__(self, o):
        return False

    def __le__(self, o):
        return False

    def __ge__(self, o):
        return False

    def __add__(self, o):
        return 0

    def __radd__(self, o):
        return 0

    def __sub__(self, o):
        return 0

    def __rsub__(self, o):
        return 0

    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_bench_engine(eng_mod):
    class BenchEngine(eng_mod.Engine):
        def __init__(self):
            pass

        def __getattr__(self, name):
            if name.startswith("_"):
                return _Zero()
            raise AttributeError(name)

    return BenchEngine


def run_fire(eng, cfg, screen, mouse, scene, ocr_fake, sampler):
    """一次完整的购买尝试：开点 → 解锁 → 确认 → 余额不足守卫 → 成交校验。"""
    scene.phase = "fire"
    t_begin = eng.time.perf_counter()
    try:
        ok = eng._fire_and_confirm(cfg, screen, mouse, "confirm_tpl", (1600, 940), 0.0,
                                   item="枪械外观", price=2050.0)
    except Exception as e:                     # 半自动等分支不该在这里触发
        if os.environ.get("FB_TRACE"):
            raise
        ok = "EXC:%s:%s" % (type(e).__name__, e)
    return ok, eng.time.perf_counter() - t_begin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="perf/base")
    ap.add_argument("--run", type=int, default=200, help="每场景 Monte-Carlo 次数")
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = os.path.join(PROJ, args.root)
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import common
        core_digest = common.core_digest(root)
    except Exception as _e:                 # 指纹算不出来也不能挡住出数
        core_digest = None
        print("警告: core_digest 计算失败:", _e)
    print("台架 root=%s  core_digest=%s" % (root, core_digest))
    sys.path.insert(0, root)
    eng_mod = importlib.import_module("core.engine")
    market_mod = importlib.import_module("core.market")
    popup_mod = importlib.import_module("core.popup")
    cards_mod = importlib.import_module("core.cards")
    from core import config as cfgmod

    # 真 cfg：regions 用 nav_coords 的默认框（用户自己的 config 这里是空的）
    from core.nav_coords import (N_BALANCE, N_CLOCK, N_LIST, N_POPUP_HINT,
                                 N_REWARD, N_SERIAL, N_SKIP)
    regions = {
        "list": dict(N_LIST), "clock": dict(N_CLOCK), "balance": dict(N_BALANCE),
        "reward": dict(N_REWARD), "skip": dict(N_SKIP), "hint": dict(N_POPUP_HINT),
        "serial": dict(N_SERIAL),
        "buy": {"x": 0.772, "y": 0.836, "w": 0.155, "h": 0.054},
        "confirm": {"x": 0.47, "y": 0.60, "w": 0.06, "h": 0.03},
        "cont": dict(getattr(eng_mod, "CONT_STRIP",
                            {"x": 0.40, "y": 0.890, "w": 0.20, "h": 0.100})),
    }

    class Cfg(object):
        def __init__(self):
            self.d = {"regions": regions, "failsafe": True,
                      "active_task": {"name": "枪械外观", "workflow": "list"},
                      "budget": 3000, "max_price": 3000, "switches": {}}

        def __getitem__(self, k):
            return self.d[k]

        def get(self, k, dv=None):
            return self.d.get(k, dv)

        def region(self, name):
            return self.d["regions"].get(name)

    cfg = Cfg()

    # switches：用注册表默认值，永不抛
    SW = {}
    sw_mod = importlib.import_module("core.switches")
    for spec in sw_mod.SWITCHES:
        SW[spec["key"]] = spec.get("default")
    eng_mod.switches.get = lambda c, key: SW.get(key)

    results = {}
    BenchEngineCls = _make_bench_engine(eng_mod)

    # 列表发现阶段用**真实列表帧**（tests/fixtures/market，1340×730），两张交替取用；
    # 扫描器先预热一次把稳态缓存建好（预热成本不进样本窗口）。
    import cv2
    list_frames, list_scanners = [], []
    for _name in ("list_unfiltered.png", "list_filtered.png"):
        _f = cv2.imread(os.path.join(PROJ, "tests", "fixtures", "market", _name))
        if _f is None:
            raise SystemExit("缺少真实列表帧: %s" % _name)
        _sc = market_mod.Scanner()
        _sc.prev = _f.copy()
        _sc.cards = [{"x0": 0, "y0": 0, "x1": 10, "y1": 10}] * 6
        _sc.cache = {i: {"name": "枪械外观", "price": 2050.0,
                         "x0": 0, "y0": 0, "x1": 10, "y1": 10, "cy": 5}
                     for i in range(6)}
        _sc.scan(_f)                              # 预热，不进任何样本
        list_frames.append(_f)
        list_scanners.append(_sc)

    for mode in ("idle", "miss", "hit", "lost"):
        stage_samples = {}
        cat_samples = {}
        rng = random.Random(args.seed)
        for si in range(args.run if mode != "idle" else 20):
            sampler = Sampler(rng)
            vc = VClock(sampler)
            scene = Scene(vc, mode)
            screen = FakeScreen(vc, scene, regions)
            mouse = FakeMouse(vc, scene)
            up, tg, mt, ocrf, popf, SCENE_TEXT = make_shims(vc, scene, screen)
            eng_mod.time = vc
            eng_mod.upscale = up
            eng_mod.to_gray = tg
            eng_mod.match = mt
            eng_mod.ocr = ocrf
            popup_mod.ocr = ocrf
            eng_mod.popup = popf

            def _locate(img, *a, **k):
                vc.charge("locate_cards")
                return [(0, 0, 10, 10)] * 6

            def _changed(a, b, cards, *ar, **kw):
                vc.charge("changed_cards")
                return []                            # 稳态：整帧没变，没有卡需要重读

            def _read_card(frame, card, *a, **k):
                vc.charge("ocr_reward")
                return {"text": "枪械外观 2050", "price": "2050",
                        "grade": "崭新出厂", "name": "枪械外观"}

            cards_mod.locate_cards = _locate
            cards_mod.changed_cards = _changed
            cards_mod.read_card = _read_card
            eng_mod.records.add = lambda **kw: vc.charge("records_add")

            eng = BenchEngineCls()
            eng._stop = threading.Event()
            eng._target_hwnd = 123
            eng._say = lambda *a, **k: None
            eng._stats = {"hz": 0.0, "score": 0.0, "clicks": 0}
            eng._lock = threading.Lock()
            eng.time = vc

            # 把几个关键子步骤包一层，拿到买入路径内部的拆解
            for _nm in ("_wait_dialog_unlock", "_guard_insufficient",
                        "_bought_dialog", "_balance", "_wait_detail_ready",
                        "_record", "_insufficient_showing", "_ocr_clock",
                        "_read_serial", "_close_reward"):
                if not hasattr(eng_mod.Engine, _nm):
                    continue                    # 基线快照里没有这个方法
                _orig = getattr(eng, _nm, None)
                if _orig is None:
                    continue

                def _wrap(orig=_orig, nm=_nm):
                    def f(*a, **k):
                        _t = vc.t
                        if nm == "_close_reward":
                            scene.in_close = True
                        r = orig(*a, **k)
                        stage_samples.setdefault("sub" + nm, []).append(
                            (vc.t - _t) * 1000)
                        if nm == "_close_reward":
                            scene.in_close = False
                        return r
                    return f

                setattr(eng, _nm, _wrap())

            # ---- 阶段 0：列表发现（抓帧 → 稳态扫描 → 挑行） ----
            t_list = vc.t
            if mode != "idle":
                fi = si % len(list_frames)
                _ts = time.perf_counter()
                list_scanners[fi].scan(list_frames[fi])   # 真实帧、真实调用
                scan_real = (time.perf_counter() - _ts) * 1000.0
                vc.charge("grab_list")                     # 抓 1340×730 列表区
                vc.charge("scan_cached", dur=max(0.0, scan_real))
                vc.charge("pick_row")                      # 选目标行
                stage_samples.setdefault("list_prefix", []).append(
                    (vc.t - t_list) * 1000)
                stage_samples.setdefault("list_scan_real", []).append(scan_real)
            list_prefix_ms = (vc.t - t_list) * 1000

            # ---- 阶段 1：进详情前的等待（读编号 / 读钟） ----
            t0 = vc.t
            if mode != "idle":
                cap = min(0.55, 2.0)
                # 快照里到底有没有条件等待 —— 不能靠 hasattr：BenchEngine 的
                # __getattr__ 会给任何 _xxx 名字返回空占位。
                if "_wait_detail_ready" in vars(eng_mod.Engine):
                    eng._wait_detail_ready(screen, cap)     # 改后的条件等待
                else:
                    vc.sleep(cap)                           # 基线：固定 sleep
                try:
                    eng._ocr_clock(screen, screen.norm_to_px(N_CLOCK))
                except Exception:
                    pass
            entry_ms = (vc.t - t0) * 1000
            stage_samples.setdefault("entry", []).append(entry_ms)

            # ---- 阶段 2：点购买 + 确认 + 校验 ----
            t1 = vc.t
            if mode == "idle":
                import cv2
                frame = cv2.imread(os.path.join(PROJ, "tests", "fixtures",
                                                "market", "list_filtered.png"))
                sc = market_mod.Scanner()
                sc.prev = frame.copy()
                sc.cards = [{"x0": 0, "y0": 0, "x1": 10, "y1": 10}] * 6
                sc.cache = {i: {"name": "枪械外观", "price": 2050.0,
                                "x0": 0, "y0": 0, "x1": 10, "y1": 10, "cy": 5}
                            for i in range(6)}
                for _ in range(args.run):
                    vc.charge("grab_1340x730")
                    a = vc.t
                    sc.scan(frame)
                    stage_samples.setdefault("scan_warm", []).append((vc.t - a) * 1000)
                    vc.sleep(0.05)
                stage_samples.setdefault("idle_iter", []).append((vc.t - t1) * 1000)
            else:
                ok, dur = run_fire(eng, cfg, screen, mouse, scene, ocrf, sampler)
                stage_samples.setdefault("fire_all", []).append(dur * 1000)
                # 主指标两档：旧口径（entry + fire）与 §10.4 全量（列表 + entry + fire），
                # 都是**同一件内逐段相加后再对样本取分位**，不是各段 p50 相加。
                stage_samples.setdefault("item_e2e", []).append(
                    entry_ms + dur * 1000)
                stage_samples.setdefault("item_e2e_full", []).append(
                    list_prefix_ms + entry_ms + dur * 1000)
                stage_samples.setdefault("fire_ok", []).append(0 if ok is True else 1)
                if isinstance(ok, str):
                    stage_samples.setdefault("fire_err", []).append(ok)

            # ---- 分类拆解 ----
            for (t_ev, d, kind) in vc.ops:
                cat_samples.setdefault(kind, []).append(d)
            cat_samples.setdefault("SLEEP", []).extend([s * 1000 for s in vc.sleeps])
            results.setdefault(mode, {"stages": {}, "cats": {}, "counts": {},
                                      "sleep_s": 0.0})
            for st, xs in stage_samples.items():
                results[mode]["stages"][st] = _stat(xs)
            for k, xs in cat_samples.items():
                results[mode]["cats"][k] = _stat(xs)
                results[mode]["counts"][k] = len(xs)
            results[mode]["sleep_s"] = sum(vc.sleeps)

    cb = _build_current_breakdown(results)
    _print_current_breakdown(cb)

    print(json.dumps(results, ensure_ascii=False, indent=1))
    outp = args.out or os.path.join(PROJ, "perf", "out", "flow_%s.json" %
                                    os.path.basename(root))
    with open(outp, "w", encoding="utf-8") as f:
        json.dump({"root": root, "core_digest": core_digest, "unit": UNIT,
                   "current_breakdown": cb, "res": results}, f,
                  ensure_ascii=False, indent=1)
    print("输出:", outp)


def _build_current_breakdown(res):
    """把 CURRENT_BREAKDOWN 的现状基准与本次 run 的 measured 合成一张表。

    measured 全部取 hit 各阶段 p50；lost 的成交校验在文档里是 6 拍合计，
    台架没有同口径的总量，measured 留空、只保留 reference。
    主指标 measured 优先用 `item_e2e`（同一件内 entry + fire 相加后取分位），
    老结果文件没有这个阶段时退回 fire + entry 的 p50 相加。
    """
    hit = (res.get("hit") or {}).get("stages") or {}

    def p50(stages, name):
        d = stages.get(name) or {}
        v = d.get("p50")
        return None if v is None else float(v)

    fire_meas = p50(hit, "fire_all")
    entry_meas = p50(hit, "entry")
    list_meas = p50(hit, "list_prefix")
    e2e_legacy = p50(hit, "item_e2e")
    if e2e_legacy is None and fire_meas is not None and entry_meas is not None:
        e2e_legacy = fire_meas + entry_meas
    e2e_meas = p50(hit, "item_e2e_full")
    if e2e_meas is None and e2e_legacy is not None:
        e2e_meas = e2e_legacy + (list_meas or 0.0)

    rows = []
    others_meas = None
    for it in CURRENT_BREAKDOWN["items"]:
        key = it["key"]
        if key == "sub_bought_dialog_hit":
            meas = p50(hit, "sub_bought_dialog")
        elif key == "sub_bought_dialog_lost":
            # 1550 是 lost 场景 6 拍合计；stages 只有单拍 p50，口径不同，不硬比。
            meas = None
        else:
            meas = p50(hit, key)
        delta = None if (meas is None) else meas - it["ms"]
        pct_fire = None
        pct_full = None
        if meas is not None and fire_meas:
            pct_fire = round(100.0 * meas / fire_meas, 1)
        if meas is not None and e2e_meas:
            pct_full = round(100.0 * meas / e2e_meas, 1)
        rows.append({"key": key, "name": it["name"], "reference_ms": it["ms"],
                     "reference_pct_fire": it["pct_fire"],
                     "reference_pct_full": it["pct_full"],
                     "measured_ms": meas,
                     "measured_pct_fire": pct_fire,
                     "measured_pct_full": pct_full,
                     "delta_ms": None if delta is None else round(delta, 2),
                     "note": it["note"]})

    if fire_meas is not None:
        inside = [r["measured_ms"] for r in rows
                  if r["measured_ms"] is not None and r["key"] != "entry"]
        others_meas = round(fire_meas - sum(inside), 2)

    off = None
    if e2e_meas is not None:
        off = 100.0 * (e2e_meas - CURRENT_BREAKDOWN["end_to_end_ms"]) / \
            CURRENT_BREAKDOWN["end_to_end_ms"]
    return {
        "tree": CURRENT_BREAKDOWN["tree"],
        "source": CURRENT_BREAKDOWN["source"],
        "reference_core_digest": CURRENT_BREAKDOWN["core_digest"],
        "reference_fire_all_ms": CURRENT_BREAKDOWN["fire_all_ms"],
        "reference_entry_ms": CURRENT_BREAKDOWN["entry_ms"],
        "reference_list_prefix_ms": CURRENT_BREAKDOWN["list_prefix_ms"],
        "reference_end_to_end_ms": CURRENT_BREAKDOWN["end_to_end_ms"],
        "reference_end_to_end_kind": CURRENT_BREAKDOWN["end_to_end_kind"],
        "measured_fire_all_ms": fire_meas,
        "measured_entry_ms": entry_meas,
        "measured_list_prefix_ms": list_meas,
        "measured_item_e2e_legacy_ms": e2e_legacy,
        "measured_end_to_end_ms": e2e_meas,
        "measured_item_e2e_full": (hit.get("item_e2e_full") or {}),
        "measured_item_e2e_legacy": (hit.get("item_e2e") or {}),
        "end_to_end_delta_pct": None if off is None else round(off, 2),
        "measured_others_ms": others_meas,
        "items": rows,
        "others_ms": CURRENT_BREAKDOWN["others_ms"],
        "others_pct_fire": CURRENT_BREAKDOWN["others_pct_fire"],
        "others_pct_full": CURRENT_BREAKDOWN["others_pct_full"],
        "sum_note": CURRENT_BREAKDOWN["sum_note"],
    }


def _print_current_breakdown(cb):
    """终端表格：reference | measured | delta，跑完立刻能读。"""
    ref_e2e = cb["reference_end_to_end_ms"]
    mea_e2e = cb["measured_end_to_end_ms"]
    line = "=" * 104
    print("\n" + line)
    print("当前口径（hit p50）  fire段 reference %.2f / entry %.2f / 全链路 %.2f" %
          (cb["reference_fire_all_ms"], cb["reference_entry_ms"], ref_e2e))
    print("快照指纹 reference=%s（本次 run 的指纹见结果 JSON core_digest）" %
          cb["reference_core_digest"])
    print(line)
    print("%-20s %10s %8s %8s %10s %8s %8s %10s" %
          ("分项", "ref(ms)", "占fire", "占全链路", "meas(ms)", "占fire",
           "占全链路", "delta"))
    for r in cb["items"]:
        mea = "-" if r["measured_ms"] is None else "%.2f" % r["measured_ms"]
        rf = "-" if r["reference_pct_fire"] is None else "%.1f%%" % r["reference_pct_fire"]
        rl = "-" if r["reference_pct_full"] is None else "%.1f%%" % r["reference_pct_full"]
        mf = "-" if r["measured_pct_fire"] is None else "%.1f%%" % r["measured_pct_fire"]
        ml = "-" if r["measured_pct_full"] is None else "%.1f%%" % r["measured_pct_full"]
        dl = "-" if r["delta_ms"] is None else "%+.2f" % r["delta_ms"]
        print("%-20s %10.2f %8s %8s %10s %8s %8s %10s" %
              (r["name"], r["reference_ms"], rf, rl, mea, mf, ml, dl))
    print("%-20s %10.2f %8s %8s %10s %8s %8s %10s" %
          ("其他（点击/杂项）", cb["others_ms"],
           "%.1f%%" % cb["others_pct_fire"], "%.1f%%" % cb["others_pct_full"],
           "-" if cb["measured_others_ms"] is None else "%.2f" % cb["measured_others_ms"],
           "-", "-", "-"))
    print("-" * 104)
    print("fire 段 reference %.2f ms / measured %s" %
          (cb["reference_fire_all_ms"],
           "n/a" if cb["measured_fire_all_ms"] is None
           else "%.2f ms" % cb["measured_fire_all_ms"]))
    print("全链路 reference %.2f ms（%s，仅定位）/ measured item_e2e_full %s" %
          (ref_e2e, cb["reference_end_to_end_kind"],
           "n/a" if mea_e2e is None else "%.2f ms" % mea_e2e))
    if cb["measured_list_prefix_ms"] is not None:
        print("  其中 list_prefix measured %.2f ms；旧口径 item_e2e（缺 list_prefix）%s" %
              (cb["measured_list_prefix_ms"],
               "n/a" if cb["measured_item_e2e_legacy_ms"] is None
               else "%.2f ms" % cb["measured_item_e2e_legacy_ms"]))
    full = cb.get("measured_item_e2e_full") or {}
    if full:
        print("  item_e2e_full 分位：n=%s p50=%.2f p95=%.2f p99=%.2f max=%.2f" %
              (full.get("n"), full.get("p50", 0), full.get("p95", 0),
               full.get("p99", 0), full.get("max", 0)))
    if cb["end_to_end_delta_pct"] is not None:
        print("全链路偏差 %+.2f%%（>5%% 说明本次 root 可能不是合并树）" %
              cb["end_to_end_delta_pct"])
    print("口径说明：%s" % cb["sum_note"])
    print(line + "\n")


def _stat(xs):
    if not xs:
        return {}
    if isinstance(xs[0], str):
        return {"n": len(xs), "sample": sorted(set(xs))[:3]}
    xs = sorted(xs)
    n = len(xs)
    return {"n": n, "p50": round(xs[n // 2], 2),
            "p95": round(xs[min(n - 1, int(.95 * (n - 1)))], 2),
            "p99": round(xs[min(n - 1, int(.99 * (n - 1)))], 2),
            "max": round(xs[-1], 2),
            "mean": round(sum(xs) / n, 2)}


if __name__ == "__main__":
    main()
    # OCR 依赖会拉起非守护线程/子进程；结果已落盘，直接硬退出，
    # 避免解释器 shutdown 挂住变成僵尸（Job/超时壳之外的第二道保险）。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
