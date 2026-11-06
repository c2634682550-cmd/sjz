"""抢购引擎。

触发时钟不是倒计时读秒，而是「按钮状态跃迁」这个像素级事件。
界面显示"1秒"时真实剩余可能是 1.0~1.9 秒，拿它当时钟，误差比整个抢购窗口
还大。按钮变色是确定事件：ROI 抓取 + 灰度匹配实测 3.7ms 一轮，能 270Hz 盯着。

两种触发模式：
  vanish  盯"公示中"这类占位按钮，等它消失。开卖前就能布好，最稳。
  appear  盯"¥价格"这类可购买按钮，等它出现。需要先截到该状态的图。

两种工作流：
  detail  详情页模式：停在某件商品的页面上，盯购买按钮。
  list    列表模式：盯交易行搜索结果列表，谁先符合条件就买谁。捡漏靠这个——
          你不知道目标什么时候出现、叫什么名字，只能等它出现在列表里。

状态机：IDLE → WATCH → FIRE → CONFIRM → DONE / ABORT
列表模式：IDLE → SCAN → WATCH → FIRE → CONFIRM → DONE / ABORT
"""
import os
import random
import re
import threading
import time
import traceback
from collections import deque

import cv2
import numpy as np

from . import countdown, market, ocr, popup, records, switches, timesync
from .config import Config, DATA_DIR
from .input import (Keyboard, Mouse, MouseStolen, WindowNotForeground,
                    find_game_window)
from .nav import NavError, Navigator
# ROI 的唯一定义处（绝对像素在 nav_coords，引擎用归一化别名）
from .nav_coords import (CONTINUE_POINT as CONT_CLICK,
                         N_BALANCE as BALANCE_N, N_BAR as BAR_N,
                         N_CLOCK as CLOCK_N, N_LIST as LIST_N,
                         N_POPUP_HINT as POPUP_HINT_N,
                         N_REWARD as REWARD_N, N_SERIAL as SERIAL_N,
                         N_SKIP as SKIP_N)
from .screen import Screen
from .vision import expand, imread, imwrite, match, to_gray, upscale

IDLE, SCAN, WATCH, FIRE, CONFIRM, DONE, ABORT = (
    "idle", "scan", "watch", "fire", "confirm", "done", "abort")

# 三条抢购路线。任务级选择（scheduler 传进来），全局配置只当默认值。
#   countdown  盯公示期倒计时，归零就买
#   list       盯在售列表，谁先符合条件就买谁
#   detail     盯某个商品页上的购买按钮，按钮变了就买
WORKFLOWS = ("countdown", "list", "detail")

# 倒计时区域连续这么久一次都没读出秒数就中止。框错区域时像素差恒为 0，
# OCR 一次都不会跑，光靠「停更」判不出来 —— 没有这个兜底会无限空转。
CLOCK_READ_TIMEOUT_S = 6.0

# 比归零早这么多就开始点。用户实测（2026-10-21）：公示期按钮在归零前
# 5 秒就已经能点 —— 等归零是跟所有人同一刻抢，早 5 秒反而稳。相位估计
# 在 floor 模型下最多偏晚 1 秒，5 秒提前量实际落在 4~5.5 秒之间，仍远早
# 于归零。早了只是空点几下（点不动的按钮没反应，_fire_and_confirm 会
# 一直点到弹窗出现），晚了就是被别人买走。
FIRE_LEAD_S = 5.0

# 引擎抓的 ROI 全部来自 core/nav_coords.py（单一来源，import 在文件顶部）。
# 以前这里散着 9 个 `{"x": 60 / 1920, ...}`，同一块界面在导航层和引擎层
# 各写一遍：游戏改版要改两个地方，实测就漏改过一处（列表下沿）。
# 顶部 import 里只保留别名，调用点继续写 LIST_N / BAR_N 这种短名字。

# 抢不到就换下一件，最多连试几件。公示期一解锁常常一堆人同时抢，第一件
# 抢不到是常态，不该就此收工。
COUNTDOWN_ATTEMPTS = 3

# 列表里一件符合条件的都没有时，隔多久点一次刷新。新挂单随时可能冒出来，
# 光等是等不到的；刷太快又没意义（刷新本身要 2.5s）。
QUEUE_IDLE_REFRESH_S = 12.0

# 挑目标时只扫列表前几张卡。真机实测（2026-10-23）：列表默认排序就是按
# 解锁时间升序，前两张卡永远等得最短；扫完整屏只是把时间烧在长倒计时上。
COUNTDOWN_SCAN_CARDS = 2

# 运行中重读配置的间隔（秒）。用户改价格/做工/限量，最多这么久后生效。
LIVE_RELOAD_S = 2.0


def choose_countdown_target(cands, tried=()):
    """从候选里挑「最早解锁」的那件。没有合格的就返回 None。

    cands 每项：{"serial", "sec", "kind"}。只认 kind=="unlock"（「X分Y秒后
    解锁购买」）；kind=="remain" 是上架剩余时间，归零即下架，抢它没意义。

    `tried` 里除了编号，还可以放 ("pos", x, y)：编号读不出来的卡片只能用
    位置去重，否则同一张卡会被反复选中、反复点。
    """
    skip = set(tried)

    def _already(c):
        pos = ("pos", int(c.get("x") or 0), int(c.get("y") or 0))
        return pos in skip or c.get("serial") in skip

    live = [c for c in cands
            if c.get("kind") == "unlock" and c.get("sec") is not None
            and not _already(c)]
    if not live:
        return None
    return min(live, key=lambda c: c["sec"])


class Aborted(Exception):
    """用户主动停止或触发紧急停止。"""


class Engine:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._log = deque(maxlen=300)
        self._state = IDLE
        self._error = None
        self._deadline = None
        self._rows = []            # 最近一屏扫到的挂单（给界面看）
        # 上一轮「开始点购买 → 确认弹窗出现」的实测耗时。延迟自适应拿它调
        # 下一轮的 fire_delay_ms（见 _fire_and_confirm）。
        self._fire_lag = 0.0
        self._fails_in_row = 0     # 连续没抢到几件（stop_after_fails 用）
        # 抢到之后要通知谁（调度器挂进来，用来实现「抢到就停整条队列」）。
        # 引擎不认识调度器，靠这个回调解耦。
        self.on_hit = None
        self._mode = "detail"
        # 一次性状态：每轮 start() 会重新清零（见 start），这里给个初始值，
        # 免得单独调用内部方法时 getattr 兜底散落各处。
        self._price_filter_tried = False
        self._sort_tried = False
        self._verified_by = ""
        self._trigger_mode = None
        self._target_hwnd = None      # 启动时游戏窗口；点击前校验它在前台
        self._stats = {
            "hz": 0.0,
            "score": 0.0,
            "clicks": 0,
            "elapsed": 0.0,
            "started_at": None,
            "trigger_mode": "vanish",
            "mode": "detail",
            "scans": 0,
            "rows": 0,
        }

    # ---------------- 对外 ----------------

    def start(self, deadline=None, mode="detail", workflow=None):
        """启动一轮。

        workflow 决定走哪条抢购路线（countdown / list / detail）。
        它是**任务级**的：以前工作流由全局 trigger_mode 决定，任务自己说了
        也不算 —— 界面上写「公示期倒计时归零就买」，配置里一改就悄悄换成
        另一条完全不同的路线。现在任务说了算，全局配置只当默认值。
        """
        if self._thread and self._thread.is_alive():
            return False, "已经在运行了"
        cfg = Config.load()
        workflow = workflow or cfg.get("workflow") or "countdown"
        if workflow not in WORKFLOWS:
            return False, f"不认识的抢购方式：{workflow}"
        problem = self._validate(cfg, mode, workflow)
        if problem:
            return False, problem
        if not find_game_window():
            # 找不到游戏窗口就只剩「当前前台」，而用户点「开始抢购」的那一刻
            # 前台是控制台/浏览器自己 —— 整场鼠标会点到控制台上，还可能被
            # ensure_foreground 殷勤地切到前台。宁可拒绝启动，也不盲点（I9）。
            return False, ("没找到游戏窗口（窗口类名 UnrealWindow）—— 先把游戏"
                           "切成无边框窗口化并开着，再点开始")
        self._stop.clear()
        self._mode = mode
        self._workflow = workflow
        # deadline 是校时后的 Unix 时间戳，换算成本机单调时钟，避免中途系统改时间
        self._deadline = (time.perf_counter() + max(0.0, deadline - timesync.true_now())
                          if deadline else None)
        self._error = None
        self._log.clear()
        # 一次性状态（价格区间筛选/排序筛选）每轮清零。这两个标记是 Engine
        # 实例属性，而整个调度只建一个 Engine：不清的话第 2 个任务起会沿用
        # 上一个任务「筛选已经做过了」，换了价格区间也一件不筛，预算判断
        # 全按错的候选集合走（审查 S2）。
        self._price_filter_tried = False
        self._sort_tried = False
        self._verified_by = ""
        self._trigger_mode = None
        self._state = SCAN if mode == "list" else WATCH
        # 把当前前台窗口记成目标，之后每次点击都要求它仍在前台，
        # 否则点击会被投递给别的窗口。
        # 目标窗口 = **游戏窗口**，按窗口类名（UnrealWindow）找，不是「当前前台」。
        # 用户点「开始抢购」的那一刻，前台是控制台自己 —— 拿它当目标的话，
        # 引擎会把整场鼠标点到控制台界面上（`ensure_foreground` 还会殷勤地
        # 把控制台切到前台）。找不到游戏窗口才退回当前前台。
        self._target_hwnd = find_game_window() or Mouse.foreground()
        # 游戏独占全屏会把光标锁住，导致后面所有点击静默失效（§4.8）。
        ok, had = Mouse.release_cursor_lock()
        if had:
            self._say(f"清掉光标裁剪锁（之前{'有' if had else '没有'}锁）"
                      f" -> {'成功' if ok else '失败'}", "warn")
        if Mouse.cursor_is_locked():
            self._say("光标仍被锁住：点击可能无效。"
                      "如果游戏是独占全屏，请改成「无边框窗口化」再试", "error")
        self._stats.update(hz=0.0, score=0.0, clicks=0, elapsed=0.0,
                           started_at=time.time(),
                           trigger_mode=cfg["trigger_mode"],
                           mode=mode, workflow=workflow, scans=0, rows=0)
        self._thread = threading.Thread(target=self._run, args=(cfg, mode, workflow),
                                        daemon=True)
        self._thread.start()
        return True, "已启动"

    def stop(self):
        self._stop.set()
        t = self._thread
        # 引擎线程自己调 stop()（比如 on_hit 回调里「抢到就停」）时绝不能 join
        # 自己：join 当前线程直接 RuntimeError("cannot join current thread")，
        # 异常从回调里抛出去会打断抢到的收尾。让线程自己看到 _stop 退出即可。
        if (t and t.is_alive() and t is not threading.current_thread()):
            t.join(timeout=2.0)
        with self._lock:
            if self._state in (SCAN, WATCH, FIRE, CONFIRM):
                self._state = ABORT
        return True, "已停止"

    def status(self):
        with self._lock:
            st = dict(self._stats)
            state = self._state
            error = self._error
        running = bool(self._thread and self._thread.is_alive())
        # 跑完了就把耗时定住。一直按「现在 - 开始时间」算的话，
        # 停了之后界面上的运行时间还会一秒一秒往上跳。
        if st["started_at"] and running:
            st["elapsed"] = round(time.time() - st["started_at"], 1)
        return {
            "state": state,
            "running": running,
            "mode": self._mode,
            "stats": st,
            "error": error,
            "rows": list(self._rows),
            "log": list(self._log)[-60:],
        }

    # ---------------- 内部 ----------------

    @staticmethod
    def _validate(cfg, mode="detail", workflow=None):
        """开工前的自检：环境不对就别开跑（错一条就白跑一轮）。

        **权限不在这里查** —— 引擎的校验必须只依赖入参（测试要能稳定复现）。
        权限是环境事实，由启动前检查（preflight）和启动接口负责。
        """
        workflow = workflow or cfg.get("workflow") or "countdown"
        if workflow == "list":
            if not cfg.region("list"):
                return "列表模式要先框选交易行列表区域"
            # 列表模式也要「购买」按钮：点进详情页后靠模板判断按钮可不可点。
            if not cfg.region("buy"):
                return "还没框选购买按钮区域"
            if not cfg.has_template("buy"):
                return "还没截取购买按钮模板"
            # 列表模式是**无弹窗版式**：点购买直接成交，没有确认框，所以
            # 统一口径下这里不要求确认模板/确认区域（此前要求了，用户照着
            # 框了一堆用不上的区域，还挡住没框的人开跑）。
            return None
        if workflow == "countdown":
            # 倒计时模式不看购买按钮长什么样，只要它在哪 —— 归零那刻直接点
            # 它的中心。所以不要求购买按钮模板，但确认按钮模板仍要（点完要
            # 靠它判断弹窗有没有出来）。
            if not cfg.region("clock"):
                return "倒计时模式要先框选「倒计时」区域"
            if not cfg.region("buy"):
                return "还没框选购买按钮区域"
            if not cfg.region("confirm"):
                return "还没框选确认按钮区域"
            if not cfg.has_template("confirm"):
                return "还没截取确认按钮模板"
            return None
        if not cfg.region("buy"):
            return "还没框选购买按钮区域"
        if not cfg.region("confirm"):
            return "还没框选确认按钮区域"
        if not cfg.has_template("buy"):
            return "还没截取购买按钮模板"
        if not cfg.has_template("confirm"):
            return "还没截取确认按钮模板"
        return None

    def _say(self, msg, level="info"):
        # ts 是浮点时间戳：同一秒里的多行要按真实先后排，光看 "HH:MM:SS"
        # 会把这一秒的顺序打乱（抢购时一秒内好几行很常见）。
        self._log.append({"t": time.strftime("%H:%M:%S"), "ts": time.time(),
                          "level": level, "msg": msg})

    def _click(self, mouse, x, y):
        """带前台校验的点击。目标窗口不在前台就抛 WindowNotForeground。"""
        mouse.click(x, y, expect_foreground=self._target_hwnd)

    def _set(self, **kw):
        with self._lock:
            self._stats.update(kw)

    def _run(self, cfg, mode="detail", workflow=None):
        # **资源构造也必须在 try 里**：Screen/Mouse 一抛异常（mss 初始化、
        # 显示器序号、interception 驱动装载），线程直接死掉、`_error` 永远
        # 是 None，调度器把这轮记成「未抢到」，真因只剩一条没人看的 stderr
        # 栈。构造失败必须落到 except，写成线程级 error（审查 S9）。
        screen = None
        try:
            screen = Screen(cfg["monitor"])
            self._input_mode = cfg.get("input_mode") or "interception"
            mouse = Mouse(screen, game_hwnd=self._target_hwnd,
                          mode=self._input_mode)
            # getattr 兜底：测试里的 FakeMouse 没有这两个属性时不炸
            warn = getattr(mouse, "driver_warning", None)
            eff = getattr(mouse, "mode", self._input_mode)
            if warn:
                self._say(f"Interception 驱动不可用，退回旧输入方式：{warn}", "warn")
            else:
                self._say(f"输入通道：{'驱动注入(interception)' if eff == 'interception' else 'PostMessage(legacy)'}")
            workflow = workflow or cfg.get("workflow") or "countdown"
            try:
                confirm_tpl = self._load(cfg, "confirm")
            except RuntimeError:
                if workflow != "list":
                    raise
                # 列表模式按统一口径不要求确认模板/区域：模板缺了就按
                # 「无弹窗版式」走 _buy_without_dialog，别把整任务炸掉。
                confirm_tpl = None
            w, h = screen.size
            self._say(f"屏幕 {w}x{h} · 实战模式")
            if workflow == "countdown":
                self._say("触发：等公示期倒计时归零（按跳秒节奏反推精确时刻，"
                          "不是干等「0分0秒」）")
                self._execute_countdown(cfg, screen, mouse, confirm_tpl)
            elif workflow == "list":
                self._say("工作流：列表模式——盯交易行列表，谁先符合条件就买谁")
                nav = self._navigate(screen, mouse, cfg)
                # 导航只走一遍：_navigate 已经把页面/价格筛选做完了，把 nav
                # 交给 _execute_list 复用，避免 _ons_ready 再走一次 F4 + 18 秒
                # 的价格筛选（审查「导航跑两遍」）。
                self._execute_list(cfg, screen, mouse, confirm_tpl, nav)
            else:
                # 详情页工作流盯的是按钮本身怎么变：trigger_mode 决定等
                # 「占位按钮消失」还是等「可购买按钮出现」。
                trig = cfg.get("trigger_mode")
                if trig not in ("vanish", "appear"):
                    trig = "vanish"
                # 规范化结果要**一处生效**：以前这里只改局部变量 trig 打日志，
                # `_execute` 又按原始 cfg["trigger_mode"] 判分支，非法值时日志
                # 说「等占位按钮消失」、实际按「等按钮出现」跑，宽限期也没了。
                self._trigger_mode = trig
                buy_tpl = self._load(cfg, "buy")
                self._say(f"触发：{'等占位按钮消失' if trig == 'vanish' else '等可购买按钮出现'}"
                          f" · 阈值 {cfg['threshold']}")
                self._execute(cfg, screen, mouse, buy_tpl, confirm_tpl)
        except Aborted as e:
            self._say(str(e), "warn")
            self._set_state(ABORT)
        except MouseStolen as e:
            self._say(f"鼠标被抢：{e}", "error")
            self._say("停止执行，避免点击落到别处", "error")
            self._set_state(ABORT)
            with self._lock:
                self._error = f"鼠标被抢：{e}"
        except WindowNotForeground as e:
            self._say(f"目标窗口不在前台：{e}", "error")
            self._say("把游戏窗口切到前台再开始，"
                      "否则点击会被系统投递给别的窗口", "error")
            self._set_state(ABORT)
            with self._lock:
                self._error = f"目标窗口不在前台：{e}"
        except Exception as e:  # 引擎不能静默死掉
            self._say(f"{type(e).__name__}: {e}", "error")
            for line in traceback.format_exc().splitlines()[-6:]:
                self._say(line, "error")
            self._set_state(ABORT)
            with self._lock:
                self._error = f"{type(e).__name__}: {e}"
        finally:
            # 「抢到就停」：这一轮成功结束（DONE）时通知调度器停掉整条队列，
            # 而不是接着跑下一个任务 —— 有的人只想要那一件。
            try:
                if (self._state == DONE and switches.get(cfg, "stop.after_hit")
                        and callable(self.on_hit)):
                    self._say("已抢到，按设置停掉整条队列", "hit")
                    self.on_hit()
            except Exception:                    # noqa: BLE001
                pass
            with self._lock:
                started = self._stats.get("started_at")
                if started:
                    self._stats["elapsed"] = round(time.time() - started, 1)
            if screen is not None:
                screen.close()

    def _set_state(self, state):
        with self._lock:
            self._state = state

    def _refresh_task(self, cfg):
        """把盘上的配置合并进 cfg，返回最新的任务条件。

        引擎线程启动时拿到的 cfg 是一份快照。运行中想让用户改的筛选条件
        生效，只能主动重读 —— 见 _execute_list 里的定时调用。

        两个兼容（config.py / scheduler.py 不归本文件改）：

        - `Config.reload()` 读盘失败时会整份换成默认配置，**regions 会被
          清空**。那份默认值顶上去，confirm_region=None 就会走进盲点购买。
          这里先拍快照，丢了的框选一律按快照补回来。
        - 调度器的 `update()` 只写 queue、不写 active_task，于是这里读到的
          永远是进任务时的那份死快照，LIVE_RELOAD 等于死代码。按任务名回填
          一次（名字唯一命中才回填），让改价格/限量真能生效。
        """
        snap_regions = dict(cfg.get("regions") or {})
        snap_task = dict(cfg.get("active_task") or {})
        try:
            cfg.reload()
        except Exception:                    # noqa: BLE001
            pass
        else:
            lost = [k for k, v in snap_regions.items()
                    if v and not (cfg.get("regions") or {}).get(k)]
            if lost:
                for k in lost:
                    cfg.set_region(k, snap_regions[k])
                self._say(f"重读配置丢了 {len(lost)} 个框选区域"
                          f"（{', '.join(lost)}），继续用启动时那份", "warn")
        task = dict(cfg.get("active_task") or snap_task)
        name = (task.get("name") or "").strip()
        if name:
            hits = [t for t in (cfg.get("queue") or [])
                    if (t.get("name") or "").strip() == name]
            if len(hits) == 1:
                entry = hits[0]
                # 只回填用户真正改过的非空值：空值(0 / "" / []) 是界面上的
                # 「不限」，回填进来会把全局兜底下限撑开，宁可保持原值。
                for k in ("min_price", "max_price", "max_wear", "min_wear",
                          "craft", "proficiency", "quota", "budget",
                          "keywords", "materials", "colors", "categories",
                          "sort", "season"):
                    v = entry.get(k)
                    if v not in (None, "", 0, [], {}):
                        task[k] = v
        return task

    @staticmethod
    def _load(cfg, name):
        img = imread(cfg.template_path(name), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"模板读不出来：{name}.png")
        return to_gray(img)

    def _execute(self, cfg, screen, mouse, buy_tpl, confirm_tpl):
        threshold = float(cfg["threshold"])
        # 入口已经规范化过就沿用那个值（见 _run）；单独调进来时也要兜住，
        # 非法值按 vanish：跟日志里报的「等占位按钮消失」保持一致。
        mode = getattr(self, "_trigger_mode", None) or cfg["trigger_mode"]
        if mode not in ("vanish", "appear"):
            mode = "vanish"
        buy_region = cfg.region("buy")
        buy_px = screen.norm_to_px(expand(buy_region, 0.15))
        buy_click = self._center(screen, buy_region)
        failsafe = bool(cfg["failsafe"])

        frames = 0
        t_report = time.perf_counter()
        t_start = t_report
        t_beat = t_report
        last_score = 0.0
        armed = mode != "vanish"   # vanish 模式必须先看见占位按钮，才能"等它消失"
        t_arm_warn = t_start
        arm_grace = float(cfg.get("arm_grace_s", 1.5))
        t_overdue = None

        # ---- 阶段一：监视按钮状态 ----
        while not self._stop.is_set():
            self._failsafe(failsafe)
            last_score, _ = match(to_gray(screen.grab(buy_px)), buy_tpl)
            frames += 1
            now = time.perf_counter()
            if now - t_report >= 0.25:
                self._set(hz=round(frames / (now - t_report), 1), score=round(last_score, 3))
                frames, t_report = 0, now

            if mode == "vanish":
                if last_score >= threshold:
                    if not armed:
                        armed = True
                        self._say(f"已布防（占位按钮相关度 {last_score:.3f}），等它消失")
                    hit = False
                else:
                    # 没布防就消失 = 一开始就不在这个界面，不能盲点
                    hit = armed
            else:
                hit = last_score >= threshold

            # 到点了却一直没看到占位按钮：说明进来的时候已经开卖（占位按钮早没了）。
            # 再等下去只会错过整场，宽限一会儿后强行进入点击阶段；点不到弹窗就中止，
            # 不会无限点下去。
            if mode == "vanish" and not armed and self._deadline:
                if now > self._deadline and t_overdue is None:
                    t_overdue = now
                    self._say("到点了但没看到占位按钮，可能已经开卖", "warn")
                if t_overdue and now - t_overdue >= arm_grace:
                    armed = True
                    hit = True
                    self._say(f"宽限 {arm_grace}s 仍未见占位按钮，直接进入点击阶段", "warn")

            if now - t_beat >= 15.0:
                t_beat = now
                state = "已布防，等按钮消失" if armed else "未布防，等占位按钮出现"
                self._say(f"监视中 {now - t_start:.0f}s · {state} · 相关度 {last_score:.3f}")

            if mode == "vanish" and not armed and now - t_arm_warn >= 5.0 and not self._deadline:
                t_arm_warn = now
                self._say("5 秒内没看到占位按钮。开卖可能已经开始——"
                          "这种情况改用「等按钮出现」模式，或先把界面停在公示中状态", "warn")

            if hit:
                self._say(f"触发（相关度 {last_score:.3f} / 阈值 {threshold}）", "hit")
                break
            time.sleep(0.001)
        else:
            raise Aborted("用户停止")

        if self._fire_and_confirm(cfg, screen, mouse, confirm_tpl, buy_click, t_start):
            self._set_state(DONE)
        else:
            # 没抢到也得落到终态。状态停在 FIRE，调度器那边会一直等
            # 「done/abort」，整条队列就卡在这儿了。
            self._say("这一轮没抢到", "warn")
            self._set_state(ABORT)

    # ---------------- 倒计时模式 ----------------

    def _execute_countdown(self, cfg, screen, mouse, confirm_tpl):
        """盯公示期的「X分Y秒后解锁购买」，到解锁那一刻开点；抢不到换下一件。

        和另外两种触发模式的取舍：vanish/appear 盯按钮像素，要求按钮在
        开卖瞬间有可辨识的变化；有些界面按钮一直长一个样（只是从不可点
        变成可点），那时只能靠时间。倒计时就是为这种场景。

        注意别框成在售页那行「剩余：X分Y秒」—— 那是上架剩余时间，归零时
        商品直接下架，掐它等于白等。`_watch_countdown` 读到那种会当场报错。

        **不是**读到「0分0秒」就点 —— 那一整秒里真实剩余可能是 0.99 秒，
        最坏晚一整秒。这里按跳秒节奏反推精确时刻，见 core/countdown.py。

        为什么外面还要套一层循环：公示期一解锁是一堆人同时抢，第一件抢不到
        是常态。抢不到就刷新列表、挑下一件、重新掐点，最多 COUNTDOWN_ATTEMPTS
        件。每件抢过就记进 `tried`，不重复抢同一件。
        """
        buy_region = cfg.region("buy")
        if not buy_region:
            raise RuntimeError("倒计时模式需要先框选「购买」按钮区域")
        buy_click = self._center(screen, buy_region)
        tried = []
        attempts = max(1, int(cfg.get("countdown_attempts") or COUNTDOWN_ATTEMPTS))
        for attempt in range(1, attempts + 1):
            self._set_state(SCAN)
            target = self._countdown_prepare(cfg, screen, mouse, tried,
                                             refresh=attempt > 1)
            if target is None:
                raise Aborted("公示期里没有符合条件、还在倒计时的商品"
                              + (f"（已经试过 {len(tried)} 件）" if tried else ""))
            self._say(f"第 {attempt} 件：{target['serial'] or '(编号未读出)'}"
                      f" · {target['text']!r}", "hit")
            t_start = time.perf_counter()
            # 列表上写着「0分0秒后解锁购买」= **其实已经解锁了**。点进去
            # 读到的会是「剩余：X分Y秒」（上架剩余），不是解锁倒计时 ——
            # 这时候还去等倒计时会当场报错、把一件已经能买的货白白丢掉
            # （真机日志：「选中 xxx，0 秒后解锁」→「这件没等到解锁就没了」）。
            already = target.get("sec") == 0
            if already:
                self._say("这件已经解锁了，直接买", "hit")
            try:
                self._watch_countdown(cfg, screen, already_unlocked=already)
            except Aborted as e:
                if self._stop.is_set():
                    raise
                # 没等到解锁这件就没了（被别人买走 / 界面切走），换下一件
                self._say(f"这件没等到解锁就没了：{e}", "warn")
                tried.append(self._tried_mark(target))
                continue
            if self._fire_and_confirm(cfg, screen, mouse, confirm_tpl,
                                      buy_click, t_start):
                # 倒计时路线成交也要累加「已抢 N 件」：以前只有列表路线写
                # bought，面板上倒计时抢到永远显示 0（审查 S16）。
                self._set(bought=int(self._stats.get("bought") or 0) + 1)
                self._set_state(DONE)
                return
            tried.append(self._tried_mark(target))
            self._say(f"「{target['serial'] or '(编号未读出)'}」没抢到，"
                      f"刷新换下一件", "warn")
        raise Aborted(f"连续 {attempts} 件都没抢到，停")

    @staticmethod
    def _tried_mark(target):
        """已试过那件的去重键：有编号用编号，没编号用坐标。

        以前一律 append(target["serial"])，读不出编号时往 tried 里塞的是空串，
        choose_countdown_target 拿到 "" 等于没去重 —— 同一张卡被反复抢（S18）。
        """
        if target.get("serial"):
            return target["serial"]
        return ("pos", int(target.get("x") or 0), int(target.get("y") or 0))

    @staticmethod
    def _target_skin(cfg):
        """任务点名的那一砖（系列 + 品阶 + 名字）。没点名就返回 None。

        知识库（core/bricks.json 的品阶目录）里有每个赛季 × 四个品阶的
        全部皮肤，能把「我要抢 AK-12突击步枪·水墨迷彩」翻译成
        「棱镜攻势 · 普通」—— 那是游戏筛选面板里的两个下拉/复选框。
        """
        task = cfg.get("active_task") or {}
        name = (task.get("name") or "").strip()
        if not name:
            return None
        try:
            from . import bricks
            hit = bricks.find_skin(name)
        except Exception:
            return None
        if not hit:
            return None
        # series 给的是**砖名**（游戏赛季下拉里的那一项），不是子系列
        return {"name": hit["name"], "series": hit.get("brick") or hit.get("series", ""),
                "tier": hit.get("tier", ""), "season": hit.get("season", ""),
                "gun": hit.get("gun", ""), "weapon": hit.get("weapon", "")}

    def _countdown_prepare(self, cfg, screen, mouse, tried=(), refresh=False):
        """把界面带到「只显示公示期」，扫第一页挑出最早解锁的一件并选中它。

        以前倒计时模式默认「用户已经选好了那件」。可公示期里同时挂着一堆
        商品，人眼挑不出哪件最先解锁 —— 于是这里自己扫列表：按任务条件
        过滤，逐件点开读详情面板的倒计时，取最早的那个。

        三条用户定的规矩（都有真机理由）：

        - **只看第一页，一页都不翻。** 翻一页要 1.5 秒，公示期随时可能在
          翻页途中解锁，等翻回来那件已经被别人买走。第一页就能抢。
        - **第一轮不刷新，没抢到才刷新。** 每轮都刷会一直闪，而且刷新期间
          那件可能正好解锁，刷完就错过了。所以 refresh=False 时一件都没挑到
          才补刷一次；refresh=True 进来的（没抢到换下一件）一轮只刷那一次。
        - **扫之前先翻回第 1 页。** 上一轮试完会停在后面的页，不翻回去就是
          从第 3 页开扫，而第 1 页有货（真机踩过：报「没有符合条件的商品」，
          其实第 1 页有）。

        编号只给挑中的那件读 —— 每件都读等于每件多点一轮详情面板。防重复
        靠选中后复核：读到的编号要是已经试过，就换下一个候选。

        列表价格读不出（公示期版式的价签会漂，真机整页 6 行全读不出过）→
        点进去从弹窗价格条补读，超区间照样跳过。

        返回 {"serial","sec","text","x","y","price"}，没有合格目标返回 None。
        """
        clock_region = cfg.region("clock")
        if not clock_region:
            raise RuntimeError("倒计时模式需要先框选「倒计时」区域")
        clock_px = screen.norm_to_px(expand(clock_region, 0.1))

        if not mouse.ensure_foreground(self._target_hwnd, screen=screen):
            raise Aborted("游戏窗口抢不到前台 —— 先手动点一下游戏窗口再开始")
        nav = Navigator(screen, mouse, Keyboard(self._target_hwnd, mode=getattr(self, "_input_mode", "interception")),
                        snap=screen.grab, log=self._say)
        page, _ = nav.where()
        target = self._target_skin(cfg)
        # 这一轮是不是已经「重新定位过」了。定位过一次就别再来一次 ——
        # 下面扫不到目标时还会兜底定位，重复跑等于把筛选流程走两遍。
        located = [False]

        def locate():
            """把界面筛到目标皮肤那一档，并点开它。

            筛不到会逐级放宽（枪种/品阶/赛季），最后一档全库扫 —— 只要
            这件皮肤在库里就一定翻得到。这是「程序自己找到皮肤」的正主：
            用户不用先手动选皮肤、也不用先把界面停在正确的页上。
            """
            located[0] = True
            self._say(f"目标：{target['name']}"
                      f"（{target['season']} {target['series']} · "
                      f"{target['tier']} · {target['gun']}）")
            from .skinfind import SkinFinder
            got = SkinFinder(nav, say=self._say).find(
                target["name"],
                season=target.get("brick") or target.get("series"),
                tier=target.get("tier"), gun=target.get("gun"))
            if not got.get("ok"):
                raise Aborted(f"找不到「{target['name']}」："
                              f"{got.get('msg') or '翻遍列表没有'}")
            nav.buy()               # 典藏外观页的「购买」= 进挂单列表，不花钱
            # 这里**不要再 sleep**：nav.buy() 自己已经等过 1.5 秒并用
            # require() 确认落到 detail/onsale 了。以前外面又 sleep(1.5)，
            # 等于每件白等 1.5 秒 —— 抢购里 1.5 秒足够别人把货买走。
            self._say(f"已进到「{nav.where()[0]}」")
            if switches.get(cfg, "follow.before_buy"):
                self._follow(nav, cfg, True)

        if page != "onsale":
            self._say(f"当前在「{page}」，先走到在售页")
            try:
                if target:
                    locate()
                else:
                    nav.go_to_onsale()
            except NavError as e:
                raise Aborted(f"界面导航失败：{e}")
        elif target:
            # **已经在在售页，但可能是上一件皮肤的列表。**
            # 换皮肤之后直接点「开始抢购」，界面上停的还是旧那件：右侧
            # 详情面板写着旧皮肤名，列表里也全是旧皮肤的挂单。这时候
            # 直接扫列表，扫到天亮也扫不到新目标（真机就是这么白跑一轮）。
            # 所以先读一眼右侧标题，不是目标就**退回上一级重新筛选**。
            try:
                cur = nav.skin_title()
            except Exception:                    # noqa: BLE001
                cur = ""
            # 只有读到「像皮肤名」的文字才敢下结论：控制台压在游戏上、
            # 或者面板还没渲染出来时，OCR 会读出「默认排序」这种无关文字，
            # 拿它去比对会误判成「换了皮肤」，白白重筛一遍。
            gun_words = ("冲锋枪", "步枪", "手枪", "霰弹枪", "轻机枪",
                         "狙击", "射手", "机枪")
            if cur and any(w in cur for w in gun_words) \
                    and ocr.similarity(cur, target["name"]) < 0.72:
                self._say(f"当前选中的是「{cur}」，不是「{target['name']}」"
                          f"—— 退回上一级重新筛选", "warn")
                try:
                    locate()                     # 内部第一步就是 goto_collection
                except NavError as e:
                    raise Aborted(f"界面导航失败：{e}")

        def scan_first_page():
            """切公示期筛选 + 排序 + 翻回第 1 页 + 扫一页。

            公示期筛选走界面上的开关（不限 / 只显示 / 不显示）：倒计时模式
            必须「只显示公示期」—— 切成「不显示」的话列表里全是已解锁的挂单，
            根本没有「X分Y秒后解锁购买」可以等（真机踩过，白跑一整轮）。
            排序同理：默认排序里最便宜的那件可能排在后面几页，而抢购只看
            第一页，「按价格升序」才是捡漏该用的顺序。

            上一轮试完会停在后面的页；不先翻回去，这一轮就是从中间页开扫。
            页码读不出（None）或点了一步没变小就停手 —— 盲翻只会更错。
            """
            nav.pub_filter(switches.get(cfg, "filter.pub") or "只显示公示期")
            # **价格/成色区间也要真设进游戏**。以前只有 list 模式的
            # `_ons_ready` 会调这个，倒计时模式从来没设过 —— 任务里写的
            # 「价 1000~999999」只出现在日志的 `_describe` 里，游戏那边
            # 一件没筛，全靠扫到之后逐行比对，列表一多就白扫。
            # 设不进去只记警告：那是锦上添花，逐行比对还能兜住；
            # AttributeError 是测试替身 FakeNav 没有这些方法。
            # **一轮任务只做一次**：这个输入框逐位敲、还要失焦才显示，
            # 实测一次要 18 秒，每件都来一遍纯属烧时间（区间又不会变）。
            if not getattr(self, "_price_filter_tried", False):
                try:
                    self._apply_price_filter(nav, cfg)
                    # 「已尝试」只在**真正设进去之后**才置真：设失败（
                    # 输入没进去、区域变了）下一件还要再试一次，否则整轮
                    # 任务都在按没筛过的列表挑行（审查 S2 的同一口径）。
                    self._price_filter_tried = True
                except Aborted:
                    # 价格区间是空集（「最低价 > 最高价」）时 _apply_price_filter
                    # 会抛 Aborted。那是任务配置写错了、不是整个任务该死，
                    # 不在中途放弃 —— 丢掉筛选，继续靠逐行比对（审查 S10）。
                    if self._stop.is_set():
                        raise
                    self._say("价格区间是空集，这轮不设筛选（靠逐行比对）", "warn")
                except (NavError, AttributeError) as e:
                    self._say(f"价格筛选没设成（继续，靠逐行比对）：{e}", "warn")
            # 排序切不动不拦：顺序不同而已，不该为它整场罢工。
            # **一轮任务只试一次**：切不进去的时候每次都重试，每件白烧
            # 一次 OCR + 一条警告日志，列表刷得越勤越亏。同样只在成功
            # 之后才置真（失败允许重试）。
            # getattr 兜底：测试里的 FakeNav 没有这个方法。
            if not getattr(self, "_sort_tried", False):
                sort_by = getattr(nav, "sort_by", None)
                if sort_by is not None:
                    try:
                        sort_by(switches.get(cfg, "filter.ons_sort") or "默认排序")
                        self._sort_tried = True
                    except NavError as e:
                        self._say(f"排序没切成（不影响抢购）：{e}", "warn")
            if refresh:
                nav.ons_refresh()
            page_no = nav.ons_page()
            while page_no is not None and page_no > 1:
                nav.ons_prev_page()
                now_page = nav.ons_page()
                if now_page is None or now_page >= page_no:
                    break
                page_no = now_page
            return sweep()

        list_px = screen.norm_to_px(cfg.region("list") or LIST_N)
        task = dict(cfg.get("active_task") or {})
        # 最短倒计时（秒）：比它短的直接跳过，0 = 不限制
        min_lead = float(switches.get(cfg, "countdown.min_lead") or 0)

        def sweep():
            """扫当前页，逐件点开读倒计时。返回 (候选, 跳过原因)。

            只扫前 COUNTDOWN_SCAN_CARDS 张卡（按阅读顺序：从上到下、从左到
            右）。列表默认按解锁时间升序，前两张就是等得最短的；扫完整屏
            只会把时间烧在长倒计时上，还可能正好错过前排的开点。
            """
            rows = market.scan(screen.grab(list_px))
            rows = sorted(rows, key=lambda r: (int(r["cy"]), int(r["x0"])))
            rows = rows[:COUNTDOWN_SCAN_CARDS]
            self._say(f"{len(rows)} 行 · 条件：{self._describe(task)}")
            cands, skips = [], []
            for row in rows:
                ok, why = market.check(row, task)
                if not ok and why != "价格没读出来":
                    skips.append(why)
                    continue
                # scan 给的 x0/cy 是浮点，SetCursorPos 只收整数，必须转
                x = int(list_px[0] + max(row["x0"], 0) + 40)
                y = int(list_px[1] + int(row["cy"]))
                self._click(mouse, x, y)      # 只是选中，不买
                time.sleep(0.55)
                price = row["price"]
                if price is None:
                    # 公示期版式的价签会漂：点进去从弹窗价格条补读
                    price = self._bar_price(cfg, screen)
                    if price is None:
                        skips.append("弹窗价格也没读出")
                        continue
                    lo = float(task.get("min_price") or 0)
                    hi = float(task.get("max_price") or 0)
                    if (lo and price < lo) or (hi and price > hi):
                        skips.append(f"弹窗价格 {price:.0f} 不在区间")
                        continue
                words = self._ocr_clock(screen, clock_px)
                text = " ".join(w["text"] for w in words)
                sec = countdown.parse_words(words)
                kind = countdown.kind_words(words)
                # **倒计时太短的别买**：来不及把确认框弹出来，等到了也
                # 多半被别人先买走，白耗一次尝试。0 秒是「已经解锁」，
                # 不需要等，照买不误（那种经常 4 秒就成交）。
                if kind == "unlock" and sec is not None and 0 < sec < min_lead:
                    skips.append(f"只剩 {sec} 秒，来不及")
                    self._say(f"  价 {self._fmt(price)} · {text!r}"
                              f" —— 只剩 {sec} 秒，跳过", "warn")
                    continue
                cands.append({"serial": None, "sec": sec,
                              "kind": kind, "text": text,
                              "x": x, "y": y, "price": price})
                self._say(f"  价 {self._fmt(price)} · {text!r} [{kind}]")
            return cands, skips

        cands, skips = scan_first_page()
        best = choose_countdown_target(cands, tried)
        if best is None and target and page == "onsale" and not located[0]:
            # 第一页里没有目标 → 用知识库把筛选切到它那一档，再扫一遍。
            # 「自己找到皮肤」不能只靠第一页碰运气：目标排在后面的页时，
            # 不切筛选就永远扫不到它。只在「本来就停在在售页」时补这一步
            # —— 别的页面进来时上面已经定位过了，别重复折腾界面。
            self._say("第一页没有目标皮肤，先筛到它那一档再扫", "warn")
            try:
                locate()
            except NavError as e:
                raise Aborted(f"界面导航失败：{e}")
            cands, skips = scan_first_page()
            best = choose_countdown_target(cands, tried)
        if best is None and not refresh:
            # 一件都没挑到才补刷一次。用户定的「没抢到再去点刷新」——
            # 每轮都刷会一直闪，刷新期间那件还可能正好解锁，刷完就错过了。
            self._say("第一页没有「X分Y秒后解锁购买」，刷新再看一次", "warn")
            nav.ons_refresh()
            cands, skips = sweep()
            best = choose_countdown_target(cands, tried)
        if skips:
            # 一件都不合格时必须说清是价格读不出还是真超区间 —— 否则用户只
            # 看到「没有一件」，无从判断该改条件还是该修 OCR。
            self._say(f"跳过 {len(skips)} 行：" + "；".join(skips[:4])
                      + ("…" if len(skips) > 4 else ""))
        if best is None:
            self._say("没有一件是「X分Y秒后解锁购买」", "warn")
            return None

        # 选中挑中的那件，这时才读编号。刷新后同一件可能还在列表里：读到
        # 的编号已经试过就换下一个候选，不把尝试次数浪费在同一件上。
        exclude = set(tried)
        while True:
            self._click(mouse, best["x"], best["y"])
            time.sleep(0.4)
            best["serial"] = self._read_serial(screen)
            # 编号读不出来时只靠位置去重：以前空串直接 break，同一张卡会被
            # 反复选中反复点（tried 里也是 ""，去重形同虚设，审查 S18）。
            mark = best["serial"] or ("pos", int(best["x"]), int(best["y"]))
            if mark not in exclude:
                break
            self._say(f"{best['serial'] or '这张卡'} 已经试过，换下一件", "warn")
            exclude.add(mark)
            best = choose_countdown_target(cands, exclude)
            if best is None:
                self._say("剩下的都已经试过了", "warn")
                return None
        self._say(f"选中 {best['serial'] or '(编号未读出)'}，"
                  f"{best['sec']} 秒后解锁", "hit")
        return best

    def _bar_price(self, cfg, screen):
        """详情弹窗价格条：购买按钮上印的就是价格。

        公示期版式的价签会漂，列表 scan 经常整行 price=None（真机踩过：6 行
        全读不出，一件也抢不了）。购买按钮区在 (1482..1780, 936..984)，点
        进去直接读它。按钮上要是印着倒计时（没到解锁时刻的版式），读出来
        的数字不可信，返回 None。

        这里走 `ocr.read_price`（RapidOCR → PP-OCRv6 3× 复核 → 字模），
        不是 read_text + last_number：三级降级和「分数 + 千分位」两道闸门
        都在那一层做，放大也由它按级别决定（一级用原图，别把 3× 图喂给
        RapidOCR，那 30ms 白花）。
        """
        crop = screen.grab(screen.norm_to_px(cfg.region("bar") or BAR_N))
        if crop is None or getattr(crop, "size", 0) == 0:
            return None
        return ocr.read_price(crop, ban=("解锁", "秒"))

    @staticmethod
    def _ocr_clock(screen, px):
        """抓一次倒计时区并 OCR，返回**词列表**（不是整串文本）。

        2x 放大：14px 小字原尺寸会被整行跳过。返回词而不返回文本，是为了
        让调用方走 countdown.parse_words 的词序列解析 —— 整串文本会把锁
        图标读成的「6」和真正的「0」粘成「60」，详见 parse_words 的说明。
        """
        crop = screen.grab(px)
        if getattr(crop, "size", 0) == 0:
            return []
        big = upscale(crop, 2)
        return ocr.read_words(big)

    @staticmethod
    def _read_serial(screen):
        """读详情面板的「编号 001738261」。读不出返回空串。"""
        crop = screen.grab(screen.norm_to_px(SERIAL_N))
        if getattr(crop, "size", 0) == 0:
            return ""
        big = upscale(crop, 3)
        m = re.search(r"\d{6,}", ocr.read_text(big))
        return m.group(0) if m else ""

    def _watch_countdown(self, cfg, screen, already_unlocked=False):
        """循环读倒计时，归零即返回；界面切走或停更则中止。

        `already_unlocked`：进弹窗时列表上就写着「0分0秒后解锁购买」，
        也就是**已经解锁了**。这种情况下弹窗里显示的是「剩余：X分Y秒」
        （上架剩余时间），不是解锁倒计时 —— 那是正常的，直接放行去点购买，
        不能按「框错了区域」报错（那样会白丢一件已经能买的货）。

        **每一帧都 OCR，不做像素差预筛。** 曾经用「画面变了才 OCR」省开销，
        真机上栽了：单个数字变化只改十几个像素，`mean(absdiff)` 在 0.017~0.7
        之间抖，而这块区域本身有 0.756 的噪声 —— 两个区间重叠，20 次真跳秒
        里漏掉 7 次，漏两次就触发 2.5 秒的「停更」误判。实测这块 240x65 的
        区域 OCR 只要 3.8ms（265Hz 上限），省它毫无意义。
        """
        region = cfg.region("clock")
        if not region:
            raise RuntimeError("倒计时模式需要先在界面上框选「倒计时」区域")
        px = screen.norm_to_px(expand(region, 0.1))
        cd = countdown.Clock()
        t_beat = time.perf_counter()
        t_boot = t_beat
        # 第一次认出这是哪一种倒计时后就不再改判：解锁瞬间文字会变成
        # 「剩余：2天23小时」，那时必须继续用公示期的估计开点。
        target_kind = None

        while not self._stop.is_set():
            self._failsafe(bool(cfg["failsafe"]))
            # **等倒计时的几十秒里也要盯着前台。** 这段时间用户切出去看别
            # 的东西、或者别的窗口抢了焦点，截图抓到的就是那个窗口 ——
            # 倒计时读数一直停在同一个值，2.5 秒后就被判成「停更」而放弃
            # 一件明明能买的货（实测 63 秒卡住 2.5 秒就退出了）。
            if self._target_hwnd and Mouse.foreground() != self._target_hwnd:
                if not Mouse.ensure_foreground(self._target_hwnd, screen=screen):
                    raise Aborted("游戏窗口抢不到前台 —— 别切走，或者手动点一下游戏")
            crop = screen.grab(px)
            if getattr(crop, "size", 0) == 0:
                time.sleep(0.005)
                continue
            now = time.perf_counter()
            # 框错区域时一次都读不出秒数 —— 没有这个兜底会一声不响地空转。
            if cd.info()["last"] is None and now - t_boot >= CLOCK_READ_TIMEOUT_S:
                raise Aborted(f"{CLOCK_READ_TIMEOUT_S:.0f} 秒内一次都没读出倒计时"
                              "秒数 —— 检查框选区域是否只框住了"
                              "「剩余：X分Y秒」这行字")
            big = upscale(crop, 2)
            words = ocr.read_words(big)
            sec = countdown.parse_words(words)
            k = countdown.kind_words(words)
            if target_kind is None and k is not None:
                target_kind = k
                if k == "remain":
                    if already_unlocked:
                        self._say("已经解锁了（弹窗里显示的是上架剩余时间），"
                                  "直接买", "hit")
                        return
                    raise Aborted(
                        "读到的是「剩余：X分Y秒」—— 上架剩余时间，归零时"
                        "商品下架、按钮变灰，不是开卖倒计时。要抢购请把顶部"
                        "公示期筛选切成「只显示公示期」，盯「X分Y秒后解锁"
                        "购买」那一行")
                self._say("倒计时类型：公示期解锁（归零即可购买）")
            # 解锁之后这一行会换成「剩余：2天23小时」。那一刻不该重置
            # 相位估计，否则 zero_at 被推到两天后，永远等不到开点。
            if target_kind == "unlock" and k != "unlock":
                sec = None
            if sec is not None and cd.observe(sec, now):
                left = cd.remaining(now)
                self._set(countdown=sec,
                          zero_in=None if left is None else round(left, 2))
            if now - t_beat >= 10.0:
                t_beat = now
                left = cd.remaining(now)
                self._say(f"倒计时 {cd.info()['last']} 秒 · "
                          + ("预计归零 --" if left is None
                             else f"预计 {left:.2f}s 后归零"))
            if cd.due(now, FIRE_LEAD_S):
                left = cd.remaining(now) or 0.0
                self._say(f"开点（距归零还有 {left * 1000:.0f}ms），"
                          f"接着连点到弹窗出现", "hit")
                return
            if cd.is_stale(now):
                raise Aborted(f"显示值 {cd.info()['last']} 秒已经 "
                              f"{countdown.STALE_S:.1f} 秒没再跳 —— "
                              "倒计时停了，界面可能已经切走或被买走")
            time.sleep(0.002)
        raise Aborted("用户停止")

    # ---------------- 列表模式 ----------------

    def _navigate(self, screen, mouse, cfg):
        """把界面带到「在售」页，并按任务条件设好价格筛选。

        为什么必须校验页面：`engine` 后面的逻辑假设「已经站在在售页」。
        如果不确认，实际停在曼德尔砖页时会对着错误的列表 OCR，按错误
        的坐标点 —— 买错东西。

        价格筛选（2026-10-21 新增）：以前只导航不筛选，导致在售页是
        全档位混排，任务里的 min_price/max_price 只用于事后挑行，白跑
        大量无关卡片。现在导航到位后直接设区间，让游戏自己过滤。
        """
        nav = Navigator(screen, mouse, Keyboard(self._target_hwnd, mode=getattr(self, "_input_mode", "interception")),
                        snap=screen.grab, log=self._say)
        # **先把游戏抢到前台**。list 模式以前少了这一步：用户点完「开始抢购」
        # 控制台自己在前台，F4 就发给了控制台（日志「F4 打开市场 变化 5.4%
        # （无响应）」），接着 OCR 抓到的全是控制台画面，页面一直 unknown，
        # 整个任务以「复位失败」告终。倒计时那条路有这一步，列表这条漏了。
        # getattr 兜底：测试里的 FakeMouse 没有这个方法。
        ensure_fg = getattr(mouse, "ensure_foreground", None)
        if ensure_fg is not None and not ensure_fg(self._target_hwnd, screen=screen):
            raise Aborted("游戏窗口抢不到前台 —— 先手动点一下游戏窗口再开始")
        # 任务点名了具体皮肤时，**先自己定位过去**（筛选 → 点开 → 进在售页）。
        # 以前 list 模式只做 go_to_onsale，不碰左侧筛选：换皮肤之后界面还停在
        # 上一件的列表上，扫到天亮也扫不到目标，甚至会按「不限名」把别的
        # 赛季的货买回来。
        target = self._target_skin(cfg)
        try:
            if target:
                from .skinfind import SkinFinder
                got = SkinFinder(nav, say=self._say).find(
                    target["name"],
                    season=target.get("brick") or target.get("series"),
                    tier=target.get("tier"), gun=target.get("gun"))
                if not got.get("ok"):
                    raise Aborted(f"找不到「{target['name']}」："
                                  f"{got.get('msg') or '翻遍列表没有'}")
                nav.buy()            # 典藏外观页的「购买」= 进挂单列表，不花钱
                page = nav.where()[0]
            else:
                page = nav.go_to_onsale()
        except NavError as e:
            raise Aborted(f"界面导航失败：{e}")
        self._say(f"已到在售页（{page}）", "hit")
        self._apply_price_filter(nav, cfg)
        # 把 nav 交回调用方复用：页面和价格筛选（实测 18 秒）都已经做完了，
        # 再让 _ons_ready 新建一个 Navigator 重做一遍就是白烧一轮（审查 S3）。
        return nav

    def _apply_price_filter(self, nav, cfg):
        """按当前任务设筛选条件。没配就跳过，配了但设不进去就中止。

        为什么设失败要中止而不是继续：筛选条件是「不买错」的硬条件。
        设不进去还继续盯列表，会按 min_price/max_price 挑行 —— 但列表
        是全档位的，OCR 出来的价格和筛选框里的值对不上，容易挑错。

        一次开面板设完所有条件再点「确定」，不反复开关面板：
        每次开关都要 1.5s 以上，而且面板会记住上次状态，反复开关容易乱。
        """
        task = dict(cfg.get("active_task") or {})
        lo = float(task.get("min_price") or 0)
        hi = float(task.get("max_price") or 0)
        mats = list(task.get("materials") or [])
        cols = list(task.get("colors") or [])
        cats = list(task.get("categories") or [])
        if not lo and not hi and not mats and not cols and not cats:
            self._say("任务没设筛选条件，跳过")
            return

        self._say(f"筛选条件：价格 {lo:.0f}~{hi:.0f} "
                  f"材质 {mats} 颜色 {cols} 分类 {cats}")
        nav.ons_filter()
        if lo or hi:
            # 价格区间交给游戏筛，筛完读回比对：读回值和要的不一样就是没设
            # 进去（真机踩过：点错位置不聚焦、按键没抬起丢位数，要 3000
            # 读回 2）。这时候绝不能继续 —— 筛错档位等于没筛，会买错货。
            got = nav.ons_price(int(lo) or None, int(hi) or None)
            self._check_price_box(got, int(lo), int(hi))
        for name, want in ((mats, "材质"), (cols, "颜色"), (cats, "分类")):
            if not name:
                continue
            done = nav.ons_checks(name)
            self._say(f"  {want}已勾选：{done}")
        nav.ons_filter_confirm()
        self._say("筛选已生效", "hit")

    def _check_price_box(self, got, lo, hi):
        """核对价格框读回值：必须**不宽于**要的区间，且区间非空。

        为什么不要求完全相等：游戏会把下限抬到它自己的最低价（真机实测：
        要 1000，框里变成 2050）。抬上去只会筛得更窄，是安全的。
        反过来放宽（下限更低 / 上限更高）等于没筛，必须中止 —— 那会买到
        档位外的货。读回 (1000, 2) 这种空区间也是坏的：上限 2 什么货都没有。
        """
        g_lo, g_hi = got
        # **读不回不再中止。** 价格框是深色底 + 细字，OCR 经常整个读空
        # （真机日志：「价格框读回 (None, None)」），一中止整单就废了。
        # 而 `market.check` 后面还会按 min/max 逐行再过滤一遍 —— 最坏情况
        # 只是「游戏内没筛窄」，不会买到区间外的货。
        # 只有「区间本身是空的」才真没救，那种才中止。
        if lo and g_lo is None:
            self._say(f"最低价没读回来（要 {lo}）—— 按未设处理，"
                      "后面逐行比对兜底", "warn")
        elif lo and g_lo < lo:
            self._say(f"最低价被放宽了：要 {lo}，框里是 {g_lo} —— "
                      "逐行比对会拦住，继续", "warn")
        if hi and g_hi is None:
            self._say(f"最高价没读回来（要 {hi}）—— 按未设处理，"
                      "后面逐行比对兜底", "warn")
        elif hi and g_hi > hi:
            self._say(f"最高价被放宽了：要 {hi}，框里是 {g_hi} —— "
                      "逐行比对会拦住，继续", "warn")
        if g_lo is not None and g_hi is not None and g_lo > g_hi:
            raise Aborted(f"价格区间是空的：框里 {g_lo}~{g_hi}"
                          " —— 输入没进去，先手动确认筛选面板能正常输入")
        self._say(f"  价格区间：{g_lo or ''}~{g_hi or ''}", "hit")

    def _ons_ready(self, cfg, screen, mouse, nav=None):
        """把在售列表带到「不显示公示期 + 任务的价格/材质筛选」，并刷新一次。

        为什么排除公示期：公示期那批商品到解锁那一刻基本已经被抢光（真机
        实测：归零后按钮确实变可点，但货早没了），盯着它们纯属浪费时间。
        只抢已经能买的。

        切完筛选必须刷新 —— 改筛选不会重排已经加载的卡片，旧卡片还带着旧
        状态。

        `nav`：`_navigate` 已经导航+筛过时传进来（同一个 Navigator 只能用
        一次就重建）。那种情况下只补「不显示公示期」和刷新，**不再重设
        价格筛选** —— 那一步逐位敲输入框，实测 18 秒。
        """
        if nav is not None:
            self._say("列表模式只买已解锁的货（公示期那批解锁时基本已被抢光）；"
                      "要抢公示期解锁，把「抢购路线」改成「公示期倒计时」", "info")
            nav.pub_filter("不显示公示期")
            nav.ons_refresh()
            return nav
        if not mouse.ensure_foreground(self._target_hwnd, screen=screen):
            raise Aborted("游戏窗口抢不到前台 —— 先手动点一下游戏窗口再开始")
        nav = Navigator(screen, mouse, Keyboard(self._target_hwnd, mode=getattr(self, "_input_mode", "interception")),
                        snap=screen.grab, log=self._say)
        page = nav.where()[0]
        if page != "onsale":
            # 「推荐」页顶部也有在售/推荐标签，判据分得开，但站错页时先切回来
            self._say(f"当前是 {page} 页，切到「在售」")
            if not nav.to_onsale():
                raise Aborted("切不到「在售」页 —— 先手动切过去再开始")
        # 列表模式**只买已经解锁的**，所以这里固定切「不显示公示期」——
        # 这是模式语义，不是漏读开关（真机实测：公示期那批到解锁那一刻基本
        # 已经被抢光，盯着它们纯属浪费时间）。
        # 界面上那个「公示期筛选」开关只对**倒计时路线**生效；想盯公示期
        # 解锁购买，请把「抢购路线」改成「公示期倒计时」。
        # 这里必须说一句，否则用户会以为开关没生效（真机上就是这么误解的）。
        self._say("列表模式只买已解锁的货（公示期那批解锁时基本已被抢光）；"
                  "要抢公示期解锁，把「抢购路线」改成「公示期倒计时」", "info")
        nav.pub_filter("不显示公示期")
        self._apply_price_filter(nav, cfg)     # 价格/材质/颜色/分类
        nav.ons_refresh()
        return nav

    def _execute_list(self, cfg, screen, mouse, confirm_tpl, nav=None):
        """盯在售列表，按屏幕顺序一件件抢；这一页都试过就刷新重来。

        顺序是用户定的：越靠后的越难等到（剩余时间更长），所以只从第一件
        往下试。抢失败（被别人买走 / 按钮没出来）就换下一件；一页都试完了
        点刷新，再从第一件开始，直到抢到或用户按停。

        OCR 一次约 0.3s，不能每轮都做：用「缩略图指纹」判断列表变没变，
        没变就复用上次的扫描结果 —— 但**挑选每轮都做**，否则上一件失败后
        列表一动不动，就再也换不到下一件。
        """
        list_px = screen.norm_to_px(cfg.region("list"))
        threshold = float(cfg["threshold"])
        scan_interval = float(cfg.get("list_scan_interval", 0.05))
        scan_timeout = float(cfg.get("list_scan_timeout", 0))
        detail_timeout = float(cfg.get("list_detail_timeout", 3.0))
        max_tries = int(cfg.get("list_max_tries", 20))
        failsafe = bool(cfg["failsafe"])

        t_start = time.perf_counter()
        t_report = t_start
        t_beat = t_start
        t_refresh = t_start
        t_cfg = t_start
        rows = []
        tried = set()         # 这一轮试过的卡片 (x0, y0)，避免反复点同一张
        rounds = 0            # 刷新重来了几轮
        tries = 0
        scans = 0
        frames = 0
        last_score = 0.0
        task = dict(cfg.get("active_task") or {})
        buy_tpl = None
        # 本轮限量 / 预算上限：抢够几件就停、最多花多少金币。
        # 参考「抢够 2 件即跳下一个任务」和「预算 3200」—— 抢购不是抢一件就完事，
        # 而是一个目标可能要多件，且总花费要封顶。
        quota = max(1, int(task.get("quota") or 1))
        budget = float(task.get("budget") or 0)
        bought, spent = 0, 0.0

        nav = self._ons_ready(cfg, screen, mouse, nav)
        scanner = market.Scanner()
        self._say(f"列表区域 {list_px[2]}x{list_px[3]} · 条件：{self._describe(task)}")

        while not self._stop.is_set():
            self._failsafe(failsafe)
            # **每一轮都确认游戏还在前台**。截图抓的是整块屏幕，游戏一旦被
            # 别的窗口盖住（用户切出去看控制台/浏览器，或者别的程序抢焦点），
            # OCR 读到的就是那个窗口的内容 —— 实测读到「WorkBuddy AI 新建」
            # 这种文字，然后拿它当挂单去点，当然点不中，还会一直空转到超时。
            if self._target_hwnd and Mouse.foreground() != self._target_hwnd:
                if not mouse.ensure_foreground(self._target_hwnd, screen=screen):
                    raise Aborted("游戏窗口抢不到前台 —— 别切走，或者手动点一下游戏")
                self._say("前台被别的窗口抢走了，已切回游戏", "warn")
            now = time.perf_counter()
            frames += 1
            # 每隔几秒把盘上的配置合并进来：用户在界面上改价格区间 / 做工 /
            # 限量，最多 2 秒后生效，不用等这一轮跑完。
            # 以前 cfg 是 start() 那一刻的快照，改了永远不生效 —— 用户看到
            # 的是「我明明把上限改成 5000 了，它还在按 3000 抢」。
            if now - t_cfg >= LIVE_RELOAD_S:
                t_cfg = now
                fresh = self._refresh_task(cfg)
                if fresh and fresh != task:
                    task.clear()
                    task.update(fresh)
                    quota = max(1, int(task.get("quota") or 1))
                    budget = float(task.get("budget") or 0)
                    self._say(f"条件已更新：{self._describe(task)}")
            if now - t_report >= 0.25:
                self._set(hz=round(frames / (now - t_report), 1))
                frames, t_report = 0, now

            if scan_timeout and now - t_start > scan_timeout:
                self._say(f"扫描 {scan_timeout}s 没抢到，结束", "warn")
                break

            # 定时任务：到点前先别抢
            if self._deadline and now < self._deadline:
                if now - t_beat >= 5.0:
                    t_beat = now
                    self._say(f"等开卖，还有 {self._deadline - now:.1f}s")
                self._stop.wait(min(self._deadline - now, 0.2))
                continue

            frame = screen.grab(list_px)
            if frame.size == 0:
                time.sleep(scan_interval)
                continue

            # 逐卡比帧：只有真的变了的那几张卡才重新认字，没变的直接用上一帧。
            # 一屏六张卡，20 次/秒的循环里通常只有一张在变 —— 每帧全认一遍
            # 等于把 OCR 的钱全花在没变的卡上。挑选每轮都做（列表不动也要换下一件）。
            rows = scanner.scan(frame)
            scans += 1
            self._set(scans=scans, rows=len(rows))
            # 界面要实时看「现在这一屏有什么」，所以留一份最近扫到的挂单。
            # 只留前 30 条：列表一屏最多 8 张，30 条够翻好几屏了。
            self._rows = [{"name": (r.get("text") or "").strip(),
                           "price": r.get("price"),
                           "craft": r.get("craft") or "",
                           "wear": r.get("wear") or "",
                           "cy": round(float(r.get("cy") or 0), 1)}
                          for r in rows[:30]]

            fresh = [r for r in rows
                     if (round(r["x0"]), round(r["y0"])) not in tried]
            if budget:
                # 买不起的整行剔掉。不剔的话「超预算」会被当成「没抢到」，
                # 白白点一遍购买、还可能真的成交。
                # **价格读不出的行也剔掉**：它的价按 0 算，永远挤进候选，
                # 预算等于形同虚设（审查 D3）。
                left = [r for r in fresh
                        if r.get("price") is not None
                        and spent + r["price"] <= budget]
                if fresh and not left:
                    self._say(f"剩下的都超预算（已花 {spent:.0f} / 预算 {budget:.0f}），"
                              f"结束", "warn")
                    break
                fresh = left
            row, why, diag = market.pick(fresh, task)
            if row is None:
                if tried:
                    # 这一页符合条件的都试过了：刷新，从第一件重来
                    rounds += 1
                    self._say(f"这一页试了 {len(tried)} 件都没成，"
                              f"刷新重来（第 {rounds} 轮）", "warn")
                    tried.clear()
                    nav.ons_refresh()
                    scanner.reset()
                    continue
                # 一件都不符合时也要定期刷新：新挂单随时可能冒出来，
                # 光等是等不到的。
                if now - t_refresh >= QUEUE_IDLE_REFRESH_S:
                    t_refresh = now
                    self._say(f"{len(rows)} 行里没有符合条件的，刷新再看"
                              f"（已扫 {scans} 轮）")
                    nav.ons_refresh()
                    scanner.reset()
                    continue
                if now - t_beat >= 5.0:
                    t_beat = now
                    self._say(f"扫了 {scans} 轮 · {len(rows)} 行 · {why}")
                    for d in diag[:6]:
                        self._say(f"    「{d['text'][:40]}」{d['why']}", "info")
                time.sleep(scan_interval)
                continue

            tries += 1
            self._say(f"候选：「{row['text'][:50]}」 价格 {self._fmt(row['price'])} · {why}", "hit")
            tried.add((round(row["x0"]), round(row["y0"])))

            # scan 的 x0/cy 是浮点；SetCursorPos 只收整数，浮点会当场
            # ctypes.ArgumentError（真机踩过）
            target_x = int(list_px[0] + max(row["x0"], 0) + 40)
            target_y = int(list_px[1] + int(row["cy"]))

            self._set_state(WATCH)
            self._click(mouse, target_x, target_y)

            # 点进详情页后购买按钮应当立刻可点（东西本来就在卖）。
            # 给它 detail_timeout 秒；没等到就退回列表换下一条。
            if buy_tpl is None:
                buy_tpl = self._load(cfg, "buy")
            got = False
            t_det = time.perf_counter()
            # 区域换算提到循环外：点进详情页之后区域不会变，每轮重算
            # norm_to_px/expand 只是白花时间。
            buy_region = cfg.region("buy")
            buy_px_det = screen.norm_to_px(expand(buy_region, 0.15)) if buy_region else None
            while time.perf_counter() - t_det < detail_timeout:
                t_iter = time.perf_counter()
                if self._stop.is_set():
                    raise Aborted("用户停止")
                self._failsafe(failsafe)
                if buy_px_det is None:
                    break
                last_score, _ = match(to_gray(screen.grab(buy_px_det)), buy_tpl)
                if last_score >= threshold:
                    got = True
                    break
                # 一次「截屏+匹配」约 2ms；以前固定睡 10ms，等于每 12ms 才
                # 看一眼，按钮亮起到被发现平均晚 6ms。改成按 5ms 的采样
                # 节奏补睡，粒度降到 ~5ms。
                time.sleep(max(0.0, 0.005 - (time.perf_counter() - t_iter)))

            if not got:
                self._say(f"详情页 {detail_timeout}s 没等到购买按钮"
                          f"（相关度 {last_score:.3f}），退回列表", "warn")
                self._set_state(SCAN)
                if tries >= max_tries:
                    self._say(f"试了 {tries} 条都不行，结束", "warn")
                    break
                continue

            self._say(f"购买按钮出现（相关度 {last_score:.3f}），开点", "hit")
            # 开点前先关注：星标在详情面板顶部，此刻面板正开着，位置最准。
            # 关掉这个开关就纯粹抢购，不碰「我的关注」。
            if switches.get(cfg, "follow.before_buy"):
                self._follow(nav, cfg, True)
            buy_click = self._center(screen, cfg.region("buy"))
            if self._fire_and_confirm(cfg, screen, mouse, confirm_tpl, buy_click, t_start,
                                      item=row.get("text") or "", price=row.get("price")):
                self._fails_in_row = 0
                bought += 1
                spent += float(row["price"] or 0)
                self._set(bought=bought, spent=round(spent))
                self._say(f"抢到第 {bought}/{quota} 件 · 累计 {spent:.0f}"
                          + (f" / 预算 {budget:.0f}" if budget else ""), "hit")
                if switches.get(cfg, "follow.after_hit"):
                    # 已经买到的没必要还留在「我的关注」里，下次扫列表还得
                    # 再看它一遍
                    self._follow(nav, cfg, False)
                if bought >= quota:
                    self._set_state(DONE)
                    return
                # 没抢够：回列表从头再来（列表已经变了，重新扫）
                tried.clear()
                scanner.reset()
                nav.ons_refresh()
                continue
            self._set_state(SCAN)
            # 连续失败计数：界面改版/被风控/挂单全是别人的，都会表现为
            # 「一直没抢到」。连败到阈值就停，别让它空转到天亮。
            self._fails_in_row += 1
            limit = int(cfg.get("stop_after_fails") or 0)
            if limit and self._fails_in_row >= limit:
                raise Aborted(f"连续 {self._fails_in_row} 次没抢到，"
                              f"按设置停下（stop_after_fails={limit}）")
            if tries >= max_tries:
                self._say(f"试了 {tries} 条都不行，结束", "warn")
                break

        if self._stop.is_set():
            raise Aborted("用户停止")
        self._set(elapsed=round(time.perf_counter() - t_start, 2))
        self._set_state(ABORT)

    @staticmethod
    def _signature(frame):
        """缩略图指纹：32x32 灰度均值。列表变没变，比这个就够了。"""
        small = cv2.resize(to_gray(frame), (32, 32), interpolation=cv2.INTER_AREA)
        return float(np.mean(small)) / 255.0

    @staticmethod
    def _fmt(v):
        return "--" if v is None else f"{v:.0f}"

    @staticmethod
    def _describe(task):
        bits = []
        lo = float(task.get("min_price") or 0)
        hi = float(task.get("max_price") or 0)
        if lo or hi:
            bits.append(f"价 {lo:.0f}~{hi:.0f}" if hi else f"价 ≥{lo:.0f}")
        if task.get("craft"):
            bits.append(task["craft"])
        if task.get("wear"):
            bits.append(task["wear"] + " 成色")
        if task.get("max_wear"):
            bits.append(f"磨损≤{task['max_wear']}")
        if task.get("season"):
            bits.append(task["season"])
        if int(task.get("quota") or 1) > 1:
            bits.append(f"抢 {int(task['quota'])} 件")
        if float(task.get("budget") or 0):
            bits.append(f"预算 {float(task['budget']):.0f}")
        return " · ".join(bits) or "不限（见任务名）"

    # ---------------- 点击购买 + 确认 ----------------

    def _balance(self, screen, img=None):
        """右上角那排货币里最后一个是三角币，读它。

        这一版界面点价格按钮**直接成交**（实测点三下成交三件、余额 40→34），
        根本没有确认弹窗。没有弹窗可等，就只能用余额变化判断成没成 ——
        比「点完就当买到了」靠谱得多。
        """
        try:
            crop = img if img is not None else screen.grab(screen.norm_to_px(BALANCE_N))
            if crop is None or getattr(crop, "size", 0) == 0:
                return None
            big = upscale(crop, 3)
            text = ocr.read_text(big) or ""
        except Exception:
            return None
        return ocr.last_number(text)

    def _insufficient_showing(self, screen):
        """屏幕上是不是「三角币不足」弹窗 —— 只 OCR 那一行，几毫秒。

        为什么要在「点购买」的循环里查：钱不够时点购买**不会**弹确认框，
        而是直接弹余额不足（实测）。不查就会一直点购买点到 fire_timeout
        为止，而模态弹窗盖在上面，点击落到哪都不受控 —— 弹窗右边就是
        「充值」。查到就交给 `_guard_insufficient`（全屏识别 + 只点「取消」）。
        """
        px = screen.norm_to_px(POPUP_HINT_N)
        crop = screen.grab(px)
        if crop is None or getattr(crop, "size", 0) == 0:
            return False
        big = upscale(crop, 2)
        return "不足" in ocr.read_text(big)

    def _fire_and_confirm(self, cfg, screen, mouse, confirm_tpl, buy_click, t_start,
                          item="", price=None):
        """点购买直到弹窗出现，再点确认。返回是否成功。"""
        threshold = float(cfg["threshold"])
        confirm_region = cfg.region("confirm")

        # 半自动：定位、筛选、进详情都做了，就是不点最后那一下 —— 留给用户
        # 自己按（有人就是想在最后一刻确认一眼）。走到这里说明「该点了」。
        if switches.get(cfg, "run.semi"):
            self._say("半自动：已停在购买按钮前 —— 请在游戏里自己点这一下",
                      "warn")
            raise Aborted("半自动模式，已就位，未点击")

        if confirm_region is None:
            # 没有框「确认」= 这版界面没有确认弹窗，点价格按钮直接成交。
            # 千万不能进下面那个循环：那个循环按 fire_timeout 连点（50Hz 下
            # 8 秒 400 下），没有弹窗拦着就是买 400 件。
            # 没有确认区域时也必须走这里（不能往下走 expand(None) → TypeError，以前
            # 一旦没框确认区域就直接把任务炸了，审查 D4）。
            return self._buy_without_dialog(screen, mouse, buy_click, t_start,
                                            item=item, price=price)

        confirm_px = screen.norm_to_px(expand(confirm_region, 0.25))
        confirm_click = self._center(screen, confirm_region)
        interval = 1.0 / max(1, int(cfg["click_hz"]))
        fire_timeout = float(cfg["fire_timeout_s"])
        confirm_clicks = max(1, int(cfg["confirm_clicks"]))
        confirm_gap = max(0.0, float(cfg["confirm_interval_ms"]) / 1000.0)
        failsafe = bool(cfg["failsafe"])

        # ---- 开点前的那点延迟 ----
        # 「解锁了」和「按钮画出来了」不是同一时刻。差几十毫秒，点下去就是空点。
        # fire_delay_ms 是固定的那一段；fire_jitter_ms 是额外随机量（避免每轮
        # 节奏完全一样）；fire_adaptive 按上一轮实测的「点下去→弹窗出现」
        # 耗时反推该提前还是推后（最多只吃掉一半，防止来回震荡）。
        delay = max(0.0, float(cfg.get("fire_delay_ms", 0)) / 1000.0)
        adaptive = bool(switches.get(cfg, "fire.adaptive"))
        if adaptive and self._fire_lag > 0:
            delay = max(0.0, delay - self._fire_lag * 0.5)
        jitter = max(0.0, float(cfg.get("fire_jitter_ms", 0)) / 1000.0)
        if jitter:
            delay += random.uniform(0, jitter)
        if delay > 0.0005:
            self._say(f"开点前等 {delay * 1000:.0f}ms"
                      + ("（含自适应）" if adaptive and self._fire_lag else ""), "info")
            time.sleep(delay)

        # ---- 点购买，边点边等弹窗 ----
        self._set_state(FIRE)
        clicks = 0
        t_fire = time.perf_counter()
        # 开点前抢一次前台：等待期间焦点可能被别的窗口抢走，第一下点空
        # 就整场白等。抢不回来就别点（点击会投递给前台窗口）。
        if switches.get(cfg, "env.foreground") and not mouse.ensure_foreground(
                self._target_hwnd, screen=screen):
            raise Aborted("游戏窗口抢不到前台，没法开点")
        got_confirm = False
        while time.perf_counter() - t_fire < fire_timeout:
            t_iter = time.perf_counter()
            if self._stop.is_set():
                raise Aborted("用户停止")
            self._failsafe(failsafe)
            self._click(mouse, *buy_click)
            clicks += 1
            self._set(clicks=clicks)
            # 钱不够时弹的是「三角币不足」，右边那个按钮是「充值」。
            # 每 5 次点查一遍（OCR 几毫秒，50Hz 下占 7%）。
            if clicks % 5 == 1 and self._insufficient_showing(screen):
                self._guard_insufficient(screen, mouse)
            # 每点一次就查一次弹窗：弹窗一出现立刻停手，避免后续点击穿透到弹窗里的别处
            cscore, _ = match(to_gray(screen.grab(confirm_px)), confirm_tpl)
            if cscore >= threshold:
                self._fire_lag = time.perf_counter() - t_fire
                self._say(f"确认弹窗出现（相关度 {cscore:.3f}），"
                          f"点了 {clicks} 次 / {self._fire_lag:.2f}s", "hit")
                got_confirm = True
                break

            # ---- 空档里继续采样，别睡死 ----
            # 两次点击之间要等 interval（50Hz = 20ms）。以前这段是纯
            # sleep：弹窗在这 20ms 里出现，也要等睡醒才看得见 —— 平均
            # 白等 10ms、最坏 20ms。抢购里 20ms 就是几十个人的差距。
            # 现在按 4ms 一个节拍采样（一次截屏+匹配约 2ms），把「弹窗
            # 出现 → 被发现」压到 ~4ms，同时不改变点击频率。
            next_click_at = t_iter + interval
            while time.perf_counter() < next_click_at:
                left = next_click_at - time.perf_counter()
                time.sleep(min(0.004, max(0.0, left)))
                cscore, _ = match(to_gray(screen.grab(confirm_px)), confirm_tpl)
                if cscore >= threshold:
                    self._fire_lag = time.perf_counter() - t_fire
                    self._say(f"确认弹窗出现（相关度 {cscore:.3f}），"
                              f"点了 {clicks} 次 / {self._fire_lag:.2f}s", "hit")
                    got_confirm = True
                    break
            if got_confirm:
                break

        if not got_confirm:
            self._say(f"{fire_timeout}s 内没等到确认弹窗，中止", "warn")
            self._record(cfg, screen, item=item, price=price,
                         elapsed=time.perf_counter() - t_start, result="miss")
            return False

        # ---- 等弹窗里的共享倒计时归零，再点确认 ----
        # 真机流程（2026-10-23）：T-5s 点购买弹出确认页，倒计时和列表共享，
        # 归零前点购买没有反应。只能等文本归零，详见 _wait_dialog_unlock。
        # 传模板进去：文本「提前消失」可能就是弹窗已经没了（被别人买走），
        # 那种情况不能再盲点确认（以前直接 return True 说解锁了）。
        unlocked = self._wait_dialog_unlock(
            screen, confirm_tpl=confirm_tpl, confirm_px=confirm_px,
            threshold=threshold)
        if unlocked == "gone":
            self._say("确认弹窗已经消失（多半被人先买走），这一件按没抢到记",
                      "warn")
            self._record(cfg, screen, item=item, price=price,
                         elapsed=time.perf_counter() - t_start, result="miss")
            return False

        # ---- 点确认：闭环，弹窗一消失就停 ----
        self._set_state(CONFIRM)
        # 记一次余额：奖励弹窗读不到时，余额减少是第二判据
        before_balance = self._balance(screen)
        # 以前是**盲点** confirm_clicks 下（默认 20 × 10ms = 200ms）：
        # 第一下就成功的话，剩下 19 下全打在奖励弹窗上（那上面有
        # 「跳过」，甚至可能点到别的东西），而且白等 190ms。
        # 现在每点一下就看一眼确认弹窗还在不在：没了立刻收手。
        # 还开着就继续点，最多点到 confirm_timeout_s —— 覆盖「按钮比
        # 倒计时晚亮」的情况（这正是原来那个固定 sleep(0.6) 想解决的）。
        confirm_timeout = max(0.2, float(cfg.get("confirm_timeout_s", 2.0)))
        t_conf = time.perf_counter()
        confirm_done = False
        conf_clicks = 0
        while not self._stop.is_set():
            # 连点次数用完、兜底时间也过了，才真的收手
            if conf_clicks >= confirm_clicks and time.perf_counter() - t_conf >= confirm_timeout:
                break
            self._click(mouse, *confirm_click)
            conf_clicks += 1
            # 连点次数用完之后降速：还在说明按钮没亮（或认错了），
            # 这时每 50ms 补一下足够，没必要 500Hz 猛砸 —— 真要是弹窗
            # 已经换成别的东西，砸得越猛越容易点到不该点的。
            gap = confirm_gap if conf_clicks < confirm_clicks else max(0.05, confirm_gap)
            # 2ms 下限：confirm_gap 配成 0 时闭环会刷到几千 Hz，
            # 把输入队列灌满反而丢事件。一次匹配本身就要 2ms。
            time.sleep(max(0.002, gap))
            cscore, _ = match(to_gray(screen.grab(confirm_px)), confirm_tpl)
            if cscore < threshold:
                confirm_done = True
                break
        if self._stop.is_set():
            raise Aborted("用户停止")
        self._say(f"确认点了 {conf_clicks} 下"
                  f"（{time.perf_counter() - t_conf:.2f}s）"
                  + ("" if confirm_done else "，弹窗仍在"), "info")

        # ---- 查余额不足 ----
        # 点完确认的第一个可能结果是「钱不够」。这一步必须在报告"完成"之前，
        # 否则用户以为买到了，实际上什么都没买。
        self._guard_insufficient(screen, mouse)

        # ---- 校验「真的买到了」 ----
        # 点完确认 ≠ 买到了：别人先买走、余额不足、弹窗没响应，都会出现
        # 「点了但什么都没发生」。2026-11-02 之前这里直接记 hit，结果 data/
        # 里堆了 78 条「成交」，配的截图全是交易行列表 —— 一条真的都没有。
        # 只认奖励弹窗（_bought_dialog）；读不到弹窗时，余额减少算第二判据。
        bought = False
        by = ""
        if not switches.get(cfg, "fire.verify"):
            # 关掉校验：按「点了就算」处理。这条开关默认开，关掉是为了在
            # 结算弹窗识别不了的界面版本上还能跑；记录里会标 verified=False，
            # 事后能分清真成交和「可能成交」。
            bought = True
            by = "off"
            self._say("成交校验已关闭，按「点了就算」记（记录里标 verified=false）",
                      "warn")
        else:
            for _ in range(6):
                if self._stop.is_set():
                    break
                if self._bought_dialog(screen):
                    bought = True
                    by = "dialog"
                    break
                got = self._balance(screen)
                if (before_balance is not None and got is not None
                        and got < before_balance):
                    bought = True
                    by = "balance"
                    self._say(f"没读到结算弹窗，但余额 {before_balance} → {got}，"
                              "按成交记", "info")
                    break
                time.sleep(0.25)
        # 成交判定方式要落到记录里：以前只写 verified=开关状态，
        # 「弹窗确认」和「余额下降」两种可信度完全不同的成交混在一起（D2）。
        self._verified_by = by if bought else ""

        elapsed = time.perf_counter() - t_start
        self._set(elapsed=round(elapsed, 2))
        if bought:
            self._say(f"成交，总用时 {elapsed:.2f}s", "hit")
        else:
            self._say(f"点了 {confirm_clicks} 下确认，但没看到结算弹窗 —— "
                      "按没抢到记（多半是被人先买走了）", "warn")
            # **把残留的提示收掉**。没抢到的时候屏幕上通常留着一个
            # 「该商品已被购买」之类的弹窗，它挡住整个界面 —— 下一个任务
            # 连页面都切不过去（实测连报三次「点 collection 标签 0.0%
            # 无响应」，三个任务连环失败）。按 Esc 收掉，再继续。
            try:
                Keyboard(self._target_hwnd,
                         mode=getattr(self, "_input_mode", "interception")
                         ).tap("ESC")
                time.sleep(0.4)
            except Exception as e:            # noqa: BLE001
                self._say(f"收残留弹窗失败：{type(e).__name__}", "warn")
        # 只有真成交才留图；没抢到也记一条 miss，好复盘
        self._record(cfg, screen, item=item, price=price,
                     elapsed=elapsed, result="hit" if bought else "miss")
        if bought:
            # 奖励弹窗 + 全屏展示页收掉，回到在售列表，下一件才能接着抢
            self._close_reward(screen, mouse)
        return bought

    def _follow(self, nav, cfg, want):
        """关注 / 取消关注当前详情面板里的这一件。

        失败只记一条警告，不中断抢购 —— 关注是「下次还在我的关注里」的
        便利，不是成交的必要条件。点不动就点不动，别为此停下。
        """
        try:
            retries = max(0, int(cfg.get("follow_retry", 2)))
            wait = max(0.05, float(cfg.get("follow_wait_ms", 450)) / 1000.0)
            skip_done = bool(switches.get(cfg, "follow.skip_done"))
            for i in range(retries + 1):
                got = nav.set_follow(want, wait=wait, skip_done=skip_done)
                if got == want:
                    return got
                if i < retries:
                    time.sleep(0.25)
            self._say(f"关注没点动（试了 {retries + 1} 次），继续抢", "warn")
        except Exception as e:                   # noqa: BLE001
            self._say(f"关注失败（不影响抢购）：{type(e).__name__}: {e}", "warn")
        return None

    def _bought_dialog(self, screen):
        """屏幕上是不是成交后的奖励弹窗 —— 这是唯一可靠的成交信号。

        2026-10-23 真机复核：新界面的奖励弹窗左侧写「获得枪械外观 /
        拥有：N」(60..600, 140..400)，底部一个「跳过」(1665,942)；旧版那行
        「购买成功，已存入仓库」已经不存在了。两个地方任意一个命中就算。

        为什么不用余额和「拥有：N」的位置记忆：弹窗开着时右上角货币栏
        整块不渲染，余额永远读空（实测）；「拥有」在两个弹窗里位置不一样，
        死区域会漏读。所以只认弹窗本身的文字。
        """
        for region, marks in ((REWARD_N, ("获得", "拥有")),
                              (SKIP_N, ("跳过",))):
            try:
                crop = screen.grab(screen.norm_to_px(region))
                if crop is None or not getattr(crop, "size", 0):
                    continue
                big = upscale(crop, 2)
                text = ocr.read_text(big) or ""
            except Exception:
                continue
            if any(m in text for m in marks):
                return True
        return False

    def _wait_dialog_unlock(self, screen, timeout=30.0, confirm_tpl=None,
                            confirm_px=None, threshold=0.85):
        """等确认弹窗里的共享倒计时归零，再让调用方去点购买。

        真机实测（2026-10-23）：T-5s 点购买弹出的确认页和列表共享同一个
        倒计时，归零前点购买毫无反应。**只信「分秒文本归零/消失」这一个
        信号** —— 弹窗的边框装饰也是绿的，像素检测会把还没解锁的按钮误判
        成可点，早点了就是白点还被游戏忽略（用户当场抓过一次「你点早了」）。

        必须先见到过分秒结构、才允许把「文本消失」当解锁：弹窗刚弹出那
        一两帧 OCR 可能什么都没读到，那种「消失」是没读到，不是归零。
        开场 2 秒内从没出现过倒计时，说明点进来时本来就已解锁，直接放行。

        返回值：True = 可以点；False = 等到超时（照原样试一下）；字符串
        "gone" = 弹窗已经不在屏幕上了（文本在离零点还远的时候就消失，且
        传入的确认模板也匹配不上）。以前这种情况返回 True，调用方对着已经
        不存在的弹窗盲点确认，还可能点穿到别的按钮上（审查 S13）。用字符串
        而不是 None：None 和「没返回」的桩函数撞车。只有传 confirm_tpl 才
        做这层复核，不传就保持老行为（直接调它的测试不抓屏）。
        """
        px = screen.norm_to_px(CLOCK_N)
        t0 = time.perf_counter()
        seen = False
        # **用跳秒相位估计判归零，不能看「读到 0」。**
        # 显示值是 floor（见 countdown.observe 的注释）：读到「0分0秒」的
        # 那一刻真实剩余还有 0~1 秒，这一下点确认游戏直接忽略 —— 连续三次
        # 没抢到、用户一眼看出「点太早了」就是这么来的。上次只加了 150ms
        # 复核，远不够那 1 秒，所以还是早。
        # 估计公式：零点 = 读到 S 的时刻 + S + 1。
        cd = countdown.Clock()
        # 轮询间隔按实测的读表成本自适应：读一次 240x65 的倒计时约 25ms
        # （截屏 2ms + 放大 + 一次 OCR）。以前写死 250ms，等于「解锁了还要
        # 再等最多 250ms 才发现」—— 抢购里 250ms 是能决定成败的量级。
        poll = 0.025
        while time.perf_counter() - t0 < timeout:
            if self._stop.is_set():
                raise Aborted("用户停止")
            t_read = time.perf_counter()
            now = t_read
            words = self._ocr_clock(screen, px)
            sec = countdown.parse_words(words)
            if sec is not None:
                seen = True
                cd.observe(sec, now)
                if cd.due(now, 0.0):
                    self._say("弹窗倒计时真归零了，开点", "hit")
                    return True
            elif seen:
                # 文本消失：有相位估计就等估计的零点，没有就只能信它
                z = cd.zero_at()
                if z is None:
                    self._say("弹窗倒计时文本已消失，视为解锁", "hit")
                    return True
                left = z - time.perf_counter()
                if left <= 0:
                    self._say("弹窗倒计时文本消失且已到零点，开点", "hit")
                    return True
                if left > 0.3:
                    # 离零点还远文本就没了 —— 不是归零，是界面变了
                    # （被别人买走、切页、弹窗被关）。干等下去没意义。
                    self._say(f"弹窗倒计时提前消失（距零点还有 {left:.1f}s），"
                              "按界面变化处理", "warn")
                    if confirm_tpl is not None and confirm_px is not None:
                        # 再核一眼确认按钮还在不在：模板也没了就说明弹窗
                        # 真的没了，别让调用方对着空屏幕盲点确认。
                        try:
                            cscore, _ = match(to_gray(screen.grab(confirm_px)),
                                              confirm_tpl)
                        except Exception:        # noqa: BLE001
                            cscore = 0.0
                        if cscore < threshold:
                            self._say(f"确认按钮也不在了（相关度 {cscore:.3f}），"
                                      "判定弹窗已消失", "warn")
                            return "gone"
                    return True
                # 0 < left <= 0.3：马上就归零了，等它
            elif time.perf_counter() - t0 >= 2.0:
                # 开场 2 秒从没见过分秒结构：点进来时本来就已经解锁了
                self._say("弹窗里一直没有倒计时，按已解锁处理", "hit")
                return True
            # 睡「间隔 - 本次读表耗时」，保证轮询频率而不是被 IO 拖慢
            cost = time.perf_counter() - t_read
            time.sleep(max(0.005, poll - cost))
        self._say("等弹窗解锁超时，照着点一次试试", "warn")
        return False

    def _close_reward(self, screen, mouse):
        """成交后收掉奖励流程：「跳过」→ 全屏展示页「继续」→ 回在售列表。

        真机实测（2026-10-23）：奖励弹窗点掉「跳过」(1665,942) 还有一层
        全屏「获得外观」展示页，底部中央是「继续」(960,1000)，点完才回到
        列表。不收掉这层，下一轮抢购的筛选检查会挂在展示页上白跑。

        屏幕上没有奖励痕迹就一下都不点 —— 没成交也来收尾就是乱点。
        """
        if not self._bought_dialog(screen):
            return
        if not mouse.ensure_foreground(self._target_hwnd, screen=screen):
            self._say("游戏窗口抢不到前台，奖励弹窗收不掉", "warn")
            return
        self._click(mouse, *self._center(screen, SKIP_N))
        time.sleep(2.0)
        words = ocr.read_words(screen.grab())
        joined = "".join(w["text"] for w in words)
        if "获得" in joined or "继续" in joined:
            self._click(mouse, *CONT_CLICK)
            time.sleep(2.5)
            words = ocr.read_words(screen.grab())
            joined = "".join(w["text"] for w in words)
        back = "只显示公示期" in joined
        self._say("奖励流程已收掉，" + ("已回在售列表" if back else "未确认回到列表"),
                  "info" if back else "warn")

    def _chip_ready(self, screen):
        """价格按钮现在能不能点 —— 认完屏幕再决定动不动手。

        公示期没结束时，价格那一行写的是「X分Y秒后解锁购买」/「订单尚未开放
        购买」，这时候点下去游戏直接忽略。必须等这句话消失（倒计时归零），
        点了才有意义。所以开点前先读这一行，读不懂就当作还不能点。
        """
        try:
            crop = screen.grab(screen.norm_to_px(CLOCK_N))
            if crop is None or not getattr(crop, "size", 0):
                return False
            big = upscale(crop, 2)
            text = (ocr.read_text(big) or "").strip()
        except Exception:
            return False
        # 一个字都没认出来 = 没认屏幕，这时候点等于瞎点
        if not text:
            return False
        return not (("解锁" in text) or ("尚未开放" in text))

    def _buy_without_dialog(self, screen, mouse, buy_click, t_start, item="", price=None):
        """归零后点价格按钮，用结算弹窗判断成没成。返回是否成交。

        一下一下点，不是每秒十下：用户手动那一遍是归零后点一下就成了
        （录屏见 §4.24），而从 5 秒前开始狂点会把点击落在还没开放的按钮上，
        既白点也可能把界面点到别的状态。
        """
        cfg = Config.load()
        self._set_state(FIRE)
        if not mouse.ensure_foreground(self._target_hwnd, screen=screen):
            raise Aborted("游戏窗口抢不到前台，没法开点")
        before = self._balance(screen)
        ok = False
        by = ""
        clicks = 0
        for attempt in range(3):
            if self._stop.is_set():
                raise Aborted("用户停止")
            if not self._chip_ready(screen):
                # 价格行还写着倒计时/尚未开放（一般是跳回了另一件），停手别乱点
                self._say("价格行还不能点，这一波先停手", "warn")
                break
            # 两次落点错开：正中 + 略偏，用户明确要求「两次不在一个位置」
            px, py = buy_click if attempt == 0 else (buy_click[0] + 35, buy_click[1] + 12)
            self._click(mouse, px, py)
            clicks += 1
            self._set(clicks=clicks)
            # 点完立刻认屏幕：结算弹窗出来得比余额变化快得多
            for _ in range(4):
                time.sleep(0.18)
                if self._stop.is_set():
                    raise Aborted("用户停止")
                self._guard_insufficient(screen, mouse)
                if self._bought_dialog(screen):
                    ok = True
                    by = "dialog"
                    break
                got = self._balance(screen)
                if before is not None and got is not None and got < before:
                    ok = True
                    by = "balance"
                    break
            if ok:
                break
        elapsed = time.perf_counter() - t_start
        self._set(elapsed=round(elapsed, 2))
        if ok:
            self._say(f"成交（结算弹窗已出现），点了 {clicks} 下，"
                      f"用时 {elapsed:.2f}s", "hit")
            # 这次的成交是靠「结算弹窗 / 余额下降」判出来的，记下来 ——
            # 只写 verified=True 分不清是哪一种（审查 D2）。
            self._verified_by = by or "unknown"
            self._record(cfg, screen, item=item, price=price,
                         elapsed=elapsed, result="hit")
        else:
            self._say(f"点了 {clicks} 下都没看到结算弹窗，按没抢到记", "warn")
            self._record(cfg, screen, item=item, price=price,
                         elapsed=elapsed, result="miss")
        return ok

    def _record(self, cfg, screen, item="", price=None, elapsed=None, result="hit"):
        """留图 + 记账。两件事都不许影响抢购：失败了只记一条警告。

        只有成交才留图 —— 开关叫「成交后存截图」，没抢到也存就是在骗人。
        """
        shot = self._save_shot(cfg, screen, tag=item) if result == "hit" else None
        if not switches.get(cfg, "env.record"):
            return shot
        task = cfg.get("active_task", {}) or {}
        # `mode` 以前取 task.get("mode") —— active_task 里根本没这个键，
        # 记录里 mode 永远空。依次回退：任务自带 / 引擎启动时的 mode / 工作流。
        mode = (task.get("mode") or getattr(self, "_mode", "")
                or task.get("workflow") or "")
        verified_by = getattr(self, "_verified_by", "")
        try:
            records.add(name=item or task.get("name") or "", price=price,
                        result=result, elapsed=elapsed,
                        shot=os.path.basename(shot) if shot else "",
                        mode=mode, quota=task.get("quota"),
                        # verified 仍是「这轮开着校验」：miss 也是核对过才记的。
                        # 具体靠什么判定成交（结算弹窗 / 余额下降 / 关了校验）
                        # 放进 extra，否则两种可信度完全不同的成交分不开（D2）。
                        verified=bool(switches.get(cfg, "fire.verify")),
                        extra={"verified_by": verified_by})
        except Exception as e:
            self._say(f"记录没写进去：{type(e).__name__}", "warn")
        return shot

    def _save_shot(self, cfg, screen, tag=""):
        """成交后存一张整屏截图。存不下来不能影响抢购，只记一条警告。"""
        if not cfg.get("save_shots", True):
            return None
        try:
            frame = screen.grab()
            if frame is None or not getattr(frame, "size", 0):
                return None
            name = cfg.get("active_task", {}).get("name") or tag or "成交"
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name))[:40] or "成交"
            # 截图写在 exe 同级目录（冻结时 __file__ 指向临时解包目录，
            # 写那儿退出即丢）。
            d = os.path.join(DATA_DIR, "shots")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"{time.strftime('%Y%m%d_%H%M%S')}_{safe}.jpg")
            if not imwrite(path, frame):
                self._say("成交截图没存下（写文件失败）", "warn")
                return None
            self._say(f"成交截图：{os.path.basename(path)}", "hit")
            return path
        except Exception as e:
            self._say(f"成交截图没存下：{type(e).__name__}", "warn")
            return None

    def _guard_insufficient(self, screen, mouse, tries=3, gap=0.35):
        """点完确认后查「三角币不足」弹窗。命中就点「取消」并中止整场。

        为什么命中要中止而不是换下一条：余额不足是账户级问题，换哪件商品
        都还是不足，继续试只会反复弹窗、拖到结束时间。

        为什么点「取消」而不是直接停：弹窗是模态的，不关掉它，后面所有
        点击都会落在弹窗上 —— 包括可能落在「充值」上。
        """
        for i in range(tries):
            if self._stop.is_set():
                raise Aborted("用户停止")
            if i:
                time.sleep(gap)
            result = popup.scan(screen.grab())
            if not result["found"]:
                continue

            msg = popup.describe(result)
            self._say(f"余额不足：{msg}", "error")
            point = popup.cancel_point(result)
            if point is None:
                # 认出了弹窗但没定位到「取消」。此时绝不能猜坐标：猜错就是「充值」。
                self._say("认出了余额不足弹窗，但没定位到「取消」按钮 —— "
                          "请手动点「取消」，程序不再继续点击", "error")
                raise Aborted("余额不足，且没能定位取消按钮")
            self._click(mouse, *point)
            self._say(f"已点「取消」{point}", "warn")
            raise Aborted("余额不足，已取消")

    @staticmethod
    def _center(screen, box):
        x, y, w, h = screen.norm_to_px(box)
        return x + w // 2, y + h // 2

    @staticmethod
    def _failsafe(enabled):
        if not enabled:
            return
        x, y = Mouse.cursor()
        if x <= 2 and y <= 2:
            raise Aborted("鼠标甩到左上角，紧急停止")
