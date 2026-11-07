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
import ctypes
import re
import threading
import time
import traceback
import weakref
from collections import deque

import cv2
import numpy as np

from . import (click_burst, countdown, idle_roam, market, ocr, popup, records,
               repeat, retention, recovery, scene_guard as sg, skip_late,
               stop_grade, switches, timesync, watchdog)
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

# 边界调用连续失败多久才放弃这一轮。截图/OCR 是外部依赖（驱动、显存、本机
# 服务），偶发失败很常见；0.1 秒的抖动不该把一件正在倒计时的货甩掉。但
# 「一直失败」必须中止 —— 否则线程会一声不响地空转到天亮（挂机稳定性）。
OCR_ERROR_ABORT_S = 6.0
GRAB_ERROR_ABORT_S = 10.0


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
        # 「检查存活 → 建线程」的不可分割闸门（审查 R01 #2）：并发 start() 必须
        # 直接拒绝，不能排队 —— 排队会让后到线程在第一个建完线程之后再过一次
        # 存活检查，8 个请求照样起来 8 条引擎线程同时点同一个鼠标。
        self._lifecycle = threading.RLock()
        # 停止前的真实阶段。stop()/guard_stop() 会把状态改成 abort，不先记
        # 下来的话「是不是已经进过开火/确认」就永久丢失，调度器只能看到
        # abort 而误判成「还没下单、可以自动重跑」（审查 B01）。
        self._stage_before_stop = None
        # 本轮安全收尾是否已经跑过：guard_stop 与 _run 的 finally 都会收尾，
        # 不收口的话同一次事故会冻结/还原/写记录两遍（审查 R01 #8）。
        self._contained = False
        # 卡死检测：监督方（调度器 / 长跑验证工具）按「多久没跳」判活。
        # 引擎自己不做重启 —— 收掉资源、重开一轮是调度器的决定。
        self._hb = watchdog.Heartbeat()
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
        # 场景守卫：只负责「看清情况 / 决定级别 / 给一句话」，具体恢复动作由
        # recovery 能力库和调度器执行。默认配置来自代码，用户配置里有 guard
        # 段才覆盖 —— 这里只读不写。
        try:
            guard_cfg = sg.GuardConfig.from_mapping(Config.load().get("guard"))
        except Exception:                    # noqa: BLE001  读配置失败不影响启动
            guard_cfg = sg.GuardConfig()
        self._guard = sg.SceneGuard(guard_cfg)
        self._cursor_home = None
        # Cleanup 的回调要「不持有 self」。直接用 bound method 会形成
        # Engine -> Cleanup -> bound method -> Engine 的引用环：Engine 只能等
        # gc 回收，它持有的 threading.Event（_stop）也跟着拖住一个内核 Event
        # 句柄。真机只建一个 Engine 所以看不见，压测里每轮新建就会句柄堆积。
        # 这里传弱引用代理，环断掉，引用计数归零即释放。
        self_ref = weakref.ref(self)

        def _freeze():
            eng = self_ref()
            return eng._stop.set() if eng else True

        def _reset_cursor():
            eng = self_ref()
            return eng._restore_cursor() if eng else True

        def _record(ctx=None):
            eng = self_ref()
            return eng._record_incident(ctx) if eng else True

        self._cleanup = recovery.Cleanup(recovery.ResourceAdapters(
            freeze=_freeze,
            release_input=lambda: True,   # 点击是即按即放，没有持续按下态
            reset_cursor=_reset_cursor,
            record=_record,
        ))
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
            # ---- feature:scan.repeat BEGIN ----
            # 最近 30 次的「每件刷新」用量（界面上直接可查）；路线见 refresh_route，
            # list 路线按页刷新，scan.repeat 不生效。
            "refresh_counts": [],
            "refresh_route": "",
            # ---- feature:scan.repeat END ----
            # ==== FEATURE-SKIP-LATE:STATS BEGIN ====
            # 本轮因「来不及」跳过的件：{item, remaining_s, source, reason}
            "late_skips": [],
            # ==== FEATURE-SKIP-LATE:STATS END ====
        }

    # ---------------- 对外 ----------------

    def start(self, deadline=None, mode="detail", workflow=None):
        """启动一轮。

        workflow 决定走哪条抢购路线（countdown / list / detail）。
        它是**任务级**的：以前工作流由全局 trigger_mode 决定，任务自己说了
        也不算 —— 界面上写「公示期倒计时归零就买」，配置里一改就悄悄换成
        另一条完全不同的路线。现在任务说了算，全局配置只当默认值。
        """
        if not self._lifecycle.acquire(blocking=False):
            return False, "引擎正在启动或停止，稍后再试"
        try:
            return self._start_locked(deadline, mode, workflow)
        finally:
            self._lifecycle.release()

    def _start_locked(self, deadline=None, mode="detail", workflow=None):
        if self._thread and self._thread.is_alive():
            return False, "已经在运行了"
        # 硬停/人工处理是**持久状态**：下一轮启动不许自行清掉它（审查 B02）。
        # 以前这里无条件 user_resume()，队列在几百毫秒内就会接着下单。
        gw = self._guard
        if gw.halt_latched or gw.state in (sg.STATE_HALTED,
                                          sg.STATE_WAITING_HUMAN):
            snap = gw.snapshot()
            return False, (snap.get("message")
                           or "当前处于安全停止状态，请先点「继续」再开始")
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
        self._hb.reset()
        self._hb.beat()
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
        # 任务切换不能带着上一轮的残留（审查 B08）：连续失败计数会提前触发
        # stop_after_fails，`_rows` 会让新任务的清单页显示旧任务的挂单。
        self._fails_in_row = 0
        self._rows = []
        # ==== FEATURE-GRADE:INIT BEGIN ====
        # 品阶计数每轮重来（和 quota/budget 一样是「本轮」语义）
        self._grade = None
        self._grade_last = {}
        # ==== FEATURE-GRADE:INIT END ====
        # 新一轮开始：上一轮「停止前阶段 / 已收尾」的标记作废
        self._stage_before_stop = None
        self._contained = False
        self._state = SCAN if mode == "list" else WATCH
        # 把当前前台窗口记成目标，之后每次点击都要求它仍在前台，
        # 否则点击会被投递给别的窗口。
        # 目标窗口 = **游戏窗口**，按窗口类名（UnrealWindow）找，不是「当前前台」。
        # 用户点「开始抢购」的那一刻，前台是控制台自己 —— 拿它当目标的话，
        # 引擎会把整场鼠标点到控制台界面上（`ensure_foreground` 还会殷勤地
        # 把控制台切到前台）。找不到游戏窗口才退回当前前台。
        self._target_hwnd = find_game_window() or Mouse.foreground()
        # 记录事故前的光标位置：硬停/误买收尾时尝试还原，不让光标停在危险区域
        try:
            self._cursor_home = Mouse.cursor()
        except Exception:                    # noqa: BLE001
            self._cursor_home = None
        # 用户主动开新的一轮 = 清掉上一次的暂停/硬停状态
        # （已删除：硬停是持久状态，只有用户显式点「继续」才清 —— 见 B02）
        # 游戏独占全屏会把光标锁住，导致后面所有点击静默失效（§4.8）。
        ok, had = Mouse.release_cursor_lock()
        if had:
            self._say(f"清掉光标裁剪锁（之前{'有' if had else '没有'}锁）"
                      f" -> {'成功' if ok else '失败'}", "warn")
        # 不再做「挪 40px 再挪回」的锁探测：那会真实移动用户鼠标。
        # 若光标真被钉住，点击失败会从 click() 的报错链直接暴露出来。
        self._stats.update(hz=0.0, score=0.0, clicks=0, elapsed=0.0,
                           started_at=time.time(),
                           trigger_mode=cfg["trigger_mode"],
                           mode=mode, workflow=workflow, scans=0, rows=0,
                           # ==== FEATURE-SKIP-LATE:STATS BEGIN ====
                           # 每轮重新记，复盘只看本轮
                           late_skips=[],
                           # ==== FEATURE-SKIP-LATE:STATS END ====
                           # ==== FEATURE-GRADE:STATS BEGIN ====
                           # 契约 §4.3：stats.grade_counts 本轮各品阶成交件数
                           grade_counts={"legend": 0, "epic": 0, "rare": 0},
                           # 停止原因：quota / budget / grade（空 = 正常收尾）
                           stop_reason="", stop_detail="",
                           # ==== FEATURE-GRADE:STATS END ====
                           )
        try:
            self._thread = threading.Thread(target=self._run,
                                            args=(cfg, mode, workflow),
                                            daemon=True)
            self._thread.start()
        except Exception as e:                    # noqa: BLE001
            # 启动失败必须回滚：线程没起来却把状态留在 WATCH，调度器会一直等
            self._thread = None
            self._set_state(IDLE)
            return False, f"引擎线程启动失败：{type(e).__name__}: {e}"
        return True, "已启动"

    def stop(self, timeout=2.0):
        """停下引擎。**只有线程真的退出才返回成功**（审查 R01 #3）。

        以前 join 2s 超时也返回「已停止」，可线程还卡在 OCR/HTTP 这类不可
        打断的调用里，返回后 0.79s 还会补点一次；上层看到「已停止」就把任务
        放回 PENDING、再开一轮，于是两个引擎同时点同一个鼠标。
        现在：停不下来就返回 False + 明说原因，让调度器把队列锁住。
        """
        with self._lifecycle:
            self._stop.set()
            self._mark_stage()
            t = self._thread
            # 引擎线程自己调 stop()（on_hit 回调）时绝不能 join 自己：
            # join 当前线程直接 RuntimeError("cannot join current thread")。
            if (t and t.is_alive() and t is not threading.current_thread()):
                t.join(timeout=timeout)
            alive = bool(t and t.is_alive()
                         and t is not threading.current_thread())
            if alive:
                return False, "停止请求已发出，但引擎线程还在收尾（可能仍有一步点击）"
            with self._lock:
                if self._state in (SCAN, WATCH, FIRE, CONFIRM):
                    self._state = ABORT
            return True, "已停止"

    def wait_stop(self, timeout=3.0):
        """等引擎线程真正退出；返回 True = 已停干净。"""
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            try:
                t.join(timeout=timeout)
            except RuntimeError:
                pass
        return not (t and t.is_alive())

    def _mark_stage(self):
        """把「停止瞬间真正在做什么」记下来（B01 的唯一可信来源）。"""
        with self._lock:
            if self._state in (FIRE, CONFIRM):
                self._stage_before_stop = self._state

    def status(self):
        with self._lock:
            st = dict(self._stats)
            state = self._state
            error = self._error
        running = bool(self._thread and self._thread.is_alive())
        # ---- feature:mouse.human BEGIN ----
        # 轨迹的生效/降级必须能在状态里读到（契约 §2.1），没开跑也要给原因
        traj = getattr(self, "_traj", None)
        st["trajectory"] = (traj.status() if traj is not None else
                            {"effective": "idle", "active": False,
                             "reason": "尚未开跑：轨迹决策器在 _run 里才构造"})
        # ---- feature:mouse.human END ----
        # ---- feature:mouse.idle BEGIN ----
        # 漫游的生效/降级/归位时刻都要能从 status 读到；未开跑给稳定形状。
        roam_sess = getattr(self, "_roam", None)
        st["roam"] = (roam_sess.snapshot() if roam_sess is not None
                      else idle_roam.empty_stats())
        # ---- feature:mouse.idle END ----
        # ---- feature:buy.followup BEGIN ----
        # 补刀的开关/是否生效/是否用掉，没开跑也要有稳定形状给前端与用例。
        st["followup"] = dict(st.get("followup") or
                              {"enabled": False, "effective": False, "used": False})
        # ---- feature:buy.followup END ----
        # 跑完了就把耗时定住。一直按「现在 - 开始时间」算的话，
        # 停了之后界面上的运行时间还会一秒一秒往上跳。
        if st["started_at"] and running:
            st["elapsed"] = round(time.time() - st["started_at"], 1)
        return {
            "state": state,
            # 停止前真实阶段（fire/confirm 或者 None）：调度器据此判断
            # 「这轮是不是已经进过开火/确认」，决定能不能自动重跑。
            "stage_before_stop": self._stage_before_stop,
            "running": running,
            # 心跳有多久没跳。None = 还没开始干活（刚构造/刚启动）。
            "beat_age_s": self._hb.age(),
            "mode": self._mode,
            "stats": st,
            "error": error,
            "rows": list(self._rows),
            "log": list(self._log)[-60:],
            # 前端只认这两个：当前处置级别 + 一句人话 + 唯一主按钮
            "guard": self._guard.snapshot(),
            "doing": self._guard.doing_text(self._doing_text(state)),
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

    @staticmethod
    def _doing_text(state):
        """「现在在做什么」：给完全不懂技术的用户看，不显示转圈。"""
        return {
            IDLE: "闲着，等你开始",
            SCAN: "正在看交易行的列表",
            WATCH: "正在盯着倒计时",
            FIRE: "正在点购买",
            CONFIRM: "正在确认购买",
            DONE: "这一轮已经完成",
            ABORT: "已经停下",
        }.get(state, "正在处理")

    def guard_snapshot(self):
        return self._guard.snapshot()

    def guard_resume(self):
        """用户点了唯一的「继续」：清掉暂停/硬停记录，允许下一轮继续。"""
        # 若还挂着「未收尾」标记，先把真实收尾跑完再清 —— 标记只能由
        # 「收尾真的做完」触发清零，不能靠点一下按钮抹掉（审查 B03）。
        try:
            if self._guard.snapshot().get("containment_pending"):
                self._contain_guard("resume")
        except Exception:                    # noqa: BLE001
            pass
        snap = self._guard.user_resume()
        self._say("用户点了继续，恢复盯盘", "info")
        return snap

    def _contain_guard(self, reason):
        """跑一次安全收尾，并把结果写进守卫证据后清 pending。"""
        snap = self._guard.snapshot()
        res = self._cleanup.run({"scene": snap.get("scene"), "reason": reason})
        self._contained = True
        self._guard.add_evidence(containment_ok=not res["failed"],
                                 containment_failed=list(res["failed"]))
        self._guard.containment_done()
        return res, snap

    def guard_stop(self):
        """用户要求立刻安全停止：清状态、还原、写记录、退出。"""
        # 顺序很重要（审查 B07）：先冻结、再等线程退出，最后才还原光标 ——
        # 反过来做，线程在「按下→抬起」之间被还原光标，仍会补点一次。
        self._stop.set()
        self._mark_stage()
        snap = self._guard.hard_halt("用户请求停止")
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            try:
                t.join(timeout=2.0)
            except RuntimeError:
                pass
        res, snap = self._contain_guard("user_stop")
        self._say(f"安全停止：还原 {len(res['done'])} 项，"
                  f"失败 {len(res['failed'])} 项", "warn")
        with self._lock:
            # 已经完成的任务不许被「停止」改成 abort（M-4）
            if self._state != DONE:
                self._state = ABORT
            self._error = snap.get("message")
        # 返回收尾**之后**的快照：hard_halt 那份的 containment_pending 还是 True
        return self._guard.snapshot()

    def force_release_input(self):
        """退出进程前的最后一道保险：把强制收尾能做的都做一遍。

        `_bye` 会在 stop() 之后直接 os._exit(0)，不给守护线程任何机会；
        只要 stop() 没停干净，按键状态就可能停在 DOWN。这里同步跑一次
        真实收尾（冻结 + 释放输入 + 还原光标），返回是否全部成功。
        """
        self._stop.set()
        try:
            res, _ = self._contain_guard("shutdown")
        except Exception as e:               # noqa: BLE001
            return False, f"收尾失败：{type(e).__name__}: {e}"
        return (not res["failed"]), f"还原 {len(res['done'])} 项，失败 {len(res['failed'])} 项"

    def _force_scene(self, scene_id, evidence=None):
        """把一条确定发生的异常上报给守卫；返回值是给前端的状态快照。"""
        snap = self._guard.force(scene_id, evidence or {})
        if snap.get("state") in (sg.STATE_HALTED, sg.STATE_WAITING_HUMAN):
            msg = snap.get("message") or ""
            if msg:
                self._say(msg, "error" if snap["state"] == sg.STATE_HALTED else "warn")
                with self._lock:
                    if self._error is None:
                        self._error = msg
        return snap

    def _restore_cursor(self):
        """把光标放回启动前的位置；失败只返回 False，不影响其它收尾。"""
        if not self._cursor_home:
            return True
        try:
            ok = ctypes.windll.user32.SetCursorPos(int(self._cursor_home[0]),
                                                  int(self._cursor_home[1]))
            # SetCursorPos 返回 0 表示被别的窗口/裁剪锁挡下来了，不能当成功
            return bool(ok)
        except Exception:                    # noqa: BLE001
            return False

    def _record_incident(self, ctx=None):
        """事故记录：写一条可追溯的日志（截图由 _record 在成交路径负责）。"""
        ctx = ctx or {}
        self._say(f"事故记录：场景={ctx.get('scene') or 'unknown'} "
                  f"级别={ctx.get('level')} 时间={time.strftime('%H:%M:%S')}",
                  "error")
        return True

    def _click(self, mouse, x, y, hold=None):
        """带前台校验的点击。目标窗口不在前台就抛 WindowNotForeground。

        `hold`（秒）只在连点参数里用到：按下→抬起之间真的按住这么久，
        None 时各通道沿用原行为。
        """
        mouse.click(x, y, expect_foreground=self._target_hwnd, hold=hold)

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
            # F6：monitor 是手改配置的常见目标（写成负数/字符串/超大值会让
            # Screen 抛 ValueError，线程直接死）。读时校验，越界回退到自动选屏。
            monitor = switches.read(cfg, "monitor", 1, "int", 0, 16, key="monitor")
            screen = Screen(monitor)
            self._input_mode = cfg.get("input_mode") or "interception"
            mouse = Mouse(screen, game_hwnd=self._target_hwnd,
                          mode=self._input_mode)
            # getattr 兜底：测试里的 FakeMouse 没有这两个属性时不炸
            warn = getattr(mouse, "driver_warning", None)
            eff = getattr(mouse, "mode", self._input_mode)
            # review F8：`_input_mode` 必须回写**实际生效**通道（请求 message
            # 却降级到 legacy 时，后面的日志/键盘都要按真实通道走）。
            self._input_mode = eff
            label = getattr(mouse, "channel_label", None) \
                or ("驱动注入(interception)" if eff == "interception"
                    else "PostMessage(legacy)")
            if warn:
                self._say(f"输入通道：{label}（已降级：{warn}）", "warn")
            else:
                self._say(f"输入通道：{label}")
            # ---- feature:mouse.human BEGIN ----
            self._traj = None
            try:
                from . import humanmouse
                self._traj = humanmouse.Trajectory(cfg, mode=eff)
                attach = getattr(mouse, "attach_trajectory", None)
                if attach:                     # FakeMouse 没有这个方法，测试不受影响
                    attach(self._traj)
                text, level = self._traj.banner()
                self._say(text, level)
            except Exception as e:                          # noqa: BLE001
                self._traj = None
                self._say("拟人轨迹：初始化失败，本次点击不做拟人移动"
                          f"（{type(e).__name__}: {e}）", "warn")
            # ---- feature:mouse.human END ----
            workflow = workflow or cfg.get("workflow") or "countdown"
            # ---- feature:mouse.idle BEGIN ----
            # 只在倒计时路线生效；list/detail 没有可反推的解锁时刻，
            # 构造时就把「不启用」说明打出来，之后 tick 也不会产生动作。
            self._roam = idle_roam.RoamSession(
                cfg, screen=screen, mouse=mouse, hwnd=self._target_hwnd,
                workflow=workflow, input_mode=self._input_mode,
                traj=getattr(self, "_traj", None), say=self._say)
            note = self._roam.startup_notice()
            if note:
                self._say(note, "info")
            # ---- feature:mouse.idle END ----
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
            msg = str(e)
            if "鼠标甩到左上角" in msg:
                self._force_scene(sg.SC_USER_TAKEOVER,
                                  {"user_input_age_s": 0.0})
            elif "抢不到前台" in msg:
                self._force_scene(sg.SC_WINDOW_LOST,
                                  {"foreground": False})
            self._set_state(ABORT)
        except MouseStolen as e:
            self._say(f"鼠标被抢：{e}", "error")
            self._say("停止执行，避免点击落到别处", "error")
            self._force_scene(sg.SC_SELF_FAULT,
                              {"driver_ok": False, "why": str(e)})
            self._set_state(ABORT)
            with self._lock:
                self._error = f"鼠标被抢：{e}"
        except WindowNotForeground as e:
            self._say(f"目标窗口不在前台：{e}", "error")
            self._say("把游戏窗口切到前台再开始，"
                      "否则点击会被系统投递给别的窗口", "error")
            self._force_scene(sg.SC_WINDOW_LOST,
                              {"foreground": False, "why": str(e)})
            self._set_state(ABORT)
            with self._lock:
                self._error = f"目标窗口不在前台：{e}"
        except Exception as e:  # 引擎不能静默死掉
            self._say(f"{type(e).__name__}: {e}", "error")
            for line in traceback.format_exc().splitlines()[-6:]:
                self._say(line, "error")
            self._force_scene(sg.SC_SELF_FAULT,
                              {"driver_ok": False, "why": str(e)})
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
            # 硬停收尾：冻结、释放、还原光标、卸钩子、收子进程、写记录。
            # 幂等，重复跑也不会报错；有一项失败只记录，不阻断其它收尾。
            try:
                # RECOVERING 不能悬着（B05）：自动恢复动作还没接线，本轮退出
                # 时把它结算成「需要人处理」或硬停，别让引擎状态永远停在恢复中。
                if self._guard.state == sg.STATE_RECOVERING:
                    self._guard.abandon_recovery("本轮结束：没有可执行的自动恢复")
                    self._say("自动恢复没有可执行动作，转人工/硬停", "warn")
                if self._guard.state == sg.STATE_HALTED and not self._contained:
                    res = self._cleanup.run({
                        "scene": self._guard.owner,
                        "level": sg.LEVEL_HALT,
                        "reason": self._error or "",
                    })
                    self._contained = True
                    # 收尾真的跑完才清 containment_pending（B03）：以前没有
                    # 任何生产路径调 containment_done()，前端唯一按钮永远灰着。
                    self._guard.add_evidence(
                        containment_ok=not res["failed"],
                        containment_failed=list(res["failed"]))
                    self._guard.containment_done()
                    self._say(f"安全收尾：完成 {len(res['done'])} 项，"
                              f"失败 {len(res['failed'])} 项，"
                              f"跳过 {len(res['skipped'])} 项", "error")
                elif self._guard.state == sg.STATE_WAITING_HUMAN:
                    with self._lock:
                        if self._error is None:
                            self._error = self._guard.snapshot().get("message")
                elif self._state == DONE:
                    self._guard.succeed()
            except Exception:                    # noqa: BLE001
                pass
            if screen is not None:
                screen.close()

    def _set_state(self, state):
        with self._lock:
            # 任何落到 abort 的路径都先记下真实阶段：stop()、守卫硬停、
            # 异常退出三者都要留下「是否已进过开火/确认」这一事实（B01）。
            if state == ABORT and self._state in (FIRE, CONFIRM):
                self._stage_before_stop = self._state
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
        threshold = switches.get(cfg, "fire.threshold")
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
        # F6：同样走带校验的读取入口 —— 手改成 inf 会让 sleep 挂死线程。
        arm_grace = switches.read(cfg, "arm_grace_s", 1.5, "float", 0.0, 30.0,
                                  key="arm_grace_s")
        t_overdue = None

        # ---- 阶段一：监视按钮状态 ----
        while not self._stop.is_set():
            self._hb.beat()
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
            # ==== FEATURE-GRADE:DETAIL AFTER-HIT BEGIN ====
            self._grade_after_hit(cfg)
            # ==== FEATURE-GRADE:DETAIL AFTER-HIT END ====
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
        # F6：手改成 "abc"/inf 时旧写法 int() 直接抛异常打挂这一轮。
        attempts = max(1, int(switches.read(
            cfg, "countdown_attempts", COUNTDOWN_ATTEMPTS, "int", 0, 100,
            key="countdown_attempts") or COUNTDOWN_ATTEMPTS))
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
                                      buy_click, t_start,
                                      item=target.get("name") or ""):
                # 倒计时路线成交也要累加「已抢 N 件」：以前只有列表路线写
                # bought，面板上倒计时抢到永远显示 0（审查 S16）。
                self._set(bought=int(self._stats.get("bought") or 0) + 1)
                # ==== FEATURE-GRADE:COUNTDOWN AFTER-HIT BEGIN ====
                # 成交后按品阶收口；scope=all 抛 Aborted，scope=grade 也到此为止
                # （倒计时路线一件成交就是这一轮结束）。
                self._grade_after_hit(cfg)
                # ==== FEATURE-GRADE:COUNTDOWN AFTER-HIT END ====
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

        # ---- feature:scan.repeat BEGIN ----
        # 每件独立抽「还刷不刷、刷几次」：倒计时路线一次 _countdown_prepare
        # 就是一件。0/0 = 一次都不刷；抽到几次就最多刷几次，用尽即退出。
        # 语义：刷新是重新排队/重新抽样，不是给同一件多一次中签机会。
        self._set(refresh_route="countdown")
        self._repeat_ctx = None
        rep_used = 0
        rep_last = [0.0]
        try:
            _rep_lo, _rep_hi = repeat.limits(cfg)
            rep_budget = repeat.draw(_rep_lo, _rep_hi)
        except repeat.RepeatConfigError as e:
            rep_budget = 0
            self._say(f"scan.repeat 区间倒挂（{e}）—— 本轮按 0 次刷新", "warn")
        # 间隔走既有的「列表扫描间隔」，再压上硬下限，避免刷新点成连发
        rep_gap = repeat.gap_floor(switches.get(cfg, "scan.interval"))

        def repeat_refresh(why=""):
            """按这一件的预算刷一次列表；预算用尽返回 False。

            次数在**点刷新之前**扣：刷新抛错也算一次，不能白刷。两次刷新
            之间至少隔 rep_gap 秒（硬下限）。间隔未到就等，等到用户按停为止。
            """
            nonlocal rep_used
            # 买到即停（quota/stop_after_hit/用户按停）已经把 _stop 置位：
            # 一次都不许再刷，预算和停止标志都不能绕过。
            if self._stop.is_set() or rep_used >= rep_budget:
                return False
            if rep_used > 0:
                wait = rep_gap - (time.perf_counter() - rep_last[0])
                if wait > 0 and self._stop.wait(wait):
                    return False
            rep_used += 1
            try:
                nav.ons_refresh()
            except Exception as e:                # noqa: BLE001
                self._say(f"{why}刷新失败（第 {rep_used}/{rep_budget} 次，"
                          f"仍计入次数）：{e}", "warn")
            else:
                self._say(f"{why}刷新（第 {rep_used}/{rep_budget} 次）")
            rep_last[0] = time.perf_counter()
            return True

        def repeat_settle(text=""):
            """记这一件的刷新用量：写进 status 的最近 30 条 + 留给成交台账。"""
            self._repeat_ctx = {"refresh_budget": rep_budget,
                                "refresh_used": rep_used}
            with self._lock:
                rc = self._stats.setdefault("refresh_counts", [])
                rc.append({"item": str(text or "")[:80],
                           "budget": int(rep_budget), "used": int(rep_used)})
                del rc[:-30]
            if rep_used:
                self._say(f"这件刷新 {rep_used}/{rep_budget} 次"
                          + ("（用尽，换下一件）" if rep_used >= rep_budget
                             else ""))
        # ---- feature:scan.repeat END ----

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
                repeat_refresh("换件前")
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
        # 来不及跳过的阈值（秒）：与 min_lead 并存，取更严格的那个。
        # 手改配置写成脏值时这里显式失败，不静默套默认值继续跑。
        try:
            late_s = skip_late.threshold(cfg)
        except skip_late.SkipLateConfigError as e:
            raise Aborted(f"来不及跳过配置不合法：{e}")

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
                # ==== FEATURE-GRADE:COUNTDOWN GATE BEGIN ====
                # 点进详情页之前就按品阶闸门过一遍：已达标的档位连点都不点。
                verdict, gk = self._grade_gate(cfg, row.get("text") or "")
                if verdict == "abort":
                    s = stop_grade.settings(cfg)
                    counts = self._grade_counter().snapshot()
                    text_g = stop_grade.detail(s, counts, [gk])
                    self._mark_stop(cfg, "grade", text_g + "，整轮停止")
                    raise Aborted(f"品阶达标：{text_g}，按设置整轮停止")
                if verdict == "skip":
                    skips.append(f"{stop_grade.CN[gk]} 已达目标件数")
                    continue
                # ==== FEATURE-GRADE:COUNTDOWN GATE END ====
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
                # ---- 来不及跳过：首读这一帧就判，来不及的直接不进场 ----
                # 取数与判定都走 skip_late.judge_words()，列表直达路线共用
                # 同一份实现，避免两条路线判得不一样。
                verdict = skip_late.judge_words(late_s, words)
                sec, kind = verdict["display_sec"], verdict["kind"]
                if verdict["skip"]:
                    entry = skip_late.stats_entry(
                        text or f"row@{y}", verdict, sec, kind)
                    with self._lock:
                        lst = self._stats.setdefault("late_skips", [])
                        lst.append(entry)
                        del lst[:-30]
                    skips.append(f"来不及：{verdict['reason']}")
                    self._say(
                        f"  价 {self._fmt(price)} · {text!r} —— 来不及"
                        f"（{verdict['source']} {verdict['remaining_s']:.2f}s"
                        f" < {late_s}s），跳过不进场", "warn")
                    continue
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
                              "x": x, "y": y, "price": price,
                              # ==== FEATURE-GRADE:NAME BEGIN ====
                              # 卡面原文带上，成交后 _record 才能按品阶计数
                              "name": row.get("text") or "",
                              # ==== FEATURE-GRADE:NAME END ====
                              })
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
        # ---- feature:scan.repeat BEGIN ----
        while (best is None and not self._stop.is_set()
               and repeat_refresh("第一页没有「X分Y秒后解锁购买」，")):
            # 每件独立预算：抽到几次就最多刷几次；0/0 一次都不刷。
            cands, skips = sweep()
            best = choose_countdown_target(cands, tried)
        # ---- feature:scan.repeat END ----
        if skips:
            # 一件都不合格时必须说清是价格读不出还是真超区间 —— 否则用户只
            # 看到「没有一件」，无从判断该改条件还是该修 OCR。
            self._say(f"跳过 {len(skips)} 行：" + "；".join(skips[:4])
                      + ("…" if len(skips) > 4 else ""))
        if best is None:
            # ---- feature:scan.repeat BEGIN ----
            # 预算用尽就明确进下一步（返回 None，由上层换任务/收工），不无限刷。
            repeat_settle("")
            if rep_budget and rep_used >= rep_budget:
                self._say(f"每件刷新预算用尽（{rep_used}/{rep_budget}）"
                          f"—— 不再刷，进入下一步", "warn")
            # ---- feature:scan.repeat END ----
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
                # ---- feature:scan.repeat BEGIN ----
                repeat_settle("")
                self._say("剩下的都已经试过了", "warn")
                # ---- feature:scan.repeat END ----
                return None
        self._say(f"选中 {best['serial'] or '(编号未读出)'}，"
                  f"{best['sec']} 秒后解锁", "hit")
        # ---- feature:scan.repeat BEGIN ----
        repeat_settle(best.get("text") or "")
        # ---- feature:scan.repeat END ----
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
        # ==== FEATURE:telemetry BEGIN ====
        # 本轮没有可信零点时，台账 offset_ms 必须写 null（0 会被当成完美卡点）。
        self._zero_perf = None
        self._zero_primed = False
        # ==== FEATURE:telemetry END ====
        # ---- feature:mouse.idle BEGIN ----
        roam = getattr(self, "_roam", None)
        if roam is not None:
            roam.begin_attempt()
        # ---- feature:mouse.idle END ----
        t_beat = time.perf_counter()
        t_boot = t_beat
        # 第一次认出这是哪一种倒计时后就不再改判：解锁瞬间文字会变成
        # 「剩余：2天23小时」，那时必须继续用公示期的估计开点。
        target_kind = None
        # 边界调用（截图 / OCR）连续失败从什么时候开始。偶发失败不放弃，
        # 持续失败才中止 —— 见 OCR_ERROR_ABORT_S 的说明。
        grab_err_since = None
        ocr_err_since = None

        while not self._stop.is_set():
            self._hb.beat()
            self._failsafe(bool(cfg["failsafe"]))
            # **等倒计时的几十秒里也要盯着前台。** 这段时间用户切出去看别
            # 的东西、或者别的窗口抢了焦点，截图抓到的就是那个窗口 ——
            # 倒计时读数一直停在同一个值，2.5 秒后就被判成「停更」而放弃
            # 一件明明能买的货（实测 63 秒卡住 2.5 秒就退出了）。
            if self._target_hwnd and Mouse.foreground() != self._target_hwnd:
                if not Mouse.ensure_foreground(self._target_hwnd, screen=screen):
                    raise Aborted("游戏窗口抢不到前台 —— 别切走，或者手动点一下游戏")
            t_frame = time.perf_counter()
            try:
                crop = screen.grab(px)
            except Exception as e:                    # noqa: BLE001
                # 截图驱动/显示器拔掉/显存耗尽都会在这里抛。抛出去会把整轮
                # 任务炸掉；偶发失败先当作空帧继续盯，持续失败才放弃。
                crop = None
                if grab_err_since is None:
                    grab_err_since = t_frame
                    self._say(f"截图失败：{type(e).__name__}: {e}", "warn")
                elif t_frame - grab_err_since >= GRAB_ERROR_ABORT_S:
                    raise Aborted(f"连续 {GRAB_ERROR_ABORT_S:.0f} 秒抓不到画面"
                                  "（截图驱动异常），这一轮放弃")
                time.sleep(0.05)
                continue
            else:
                if grab_err_since is not None:
                    self._say("截图恢复", "info")
                    grab_err_since = None
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
            try:
                words = ocr.read_words(big)
            except Exception as e:                    # noqa: BLE001
                words = []
                if ocr_err_since is None:
                    ocr_err_since = t_frame
                    self._say(f"OCR 失败：{type(e).__name__}: {e}", "warn")
                elif t_frame - ocr_err_since >= OCR_ERROR_ABORT_S:
                    raise Aborted(f"连续 {OCR_ERROR_ABORT_S:.0f} 秒 OCR 都失败，"
                                  "这一轮放弃（服务/驱动可能挂了）")
            else:
                if ocr_err_since is not None:
                    self._say("OCR 恢复", "info")
                    ocr_err_since = None
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
            # ---- feature:mouse.idle BEGIN ----
            # 放在 due 之前：一旦 remaining <= FIRE_LEAD_S + return_s，
            # 这一帧先归位，再决定开点；归位后 _settled 会挡住后续移动。
            if roam is not None:
                roam.tick(now, cd)
            # ---- feature:mouse.idle END ----
            if cd.due(now, FIRE_LEAD_S):
                left = cd.remaining(now) or 0.0
                # ==== FEATURE:telemetry BEGIN ====
                z = cd.zero_at()
                if z is not None:
                    self._zero_perf = z
                    self._zero_primed = True
                # ==== FEATURE:telemetry END ====
                self._say(f"开点（距归零还有 {left * 1000:.0f}ms），"
                          f"接着连点到弹窗出现", "hit")
                return
            # OCR 自己连续失败时，读数不动不是「界面切走」，别报那句
            # 「倒计时停了，界面可能已经切走」的误导文案 —— 交给上面
            # 的 OCR 熔断说清原因（6 秒都失败 = 服务/驱动挂了）。
            if ocr_err_since is None and cd.is_stale(now):
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
        # ---- feature:scan.repeat BEGIN ----
        # 列表路线按**页**刷新（一页试完刷一次），不是按件。scan.repeat 只在
        # 倒计时路线生效；这里把路线写进状态并在日志里说一句，避免用户以为
        # 改了控件没反应。
        self._set(refresh_route="list")
        self._repeat_ctx = None
        self._say("列表路线按页刷新：每件随机刷新次数（scan.repeat）不生效")
        # ---- feature:scan.repeat END ----
        # F6：这些数值以前直接 float()/int() 读配置，手改成 NaN/Inf/负数
        # 会让 sleep 抛错或 max_tries 变成负数。统一走带校验的读取入口。
        threshold = switches.get(cfg, "fire.threshold")
        scan_interval = switches.get(cfg, "scan.interval")
        scan_timeout = switches.get(cfg, "scan.timeout")
        detail_timeout = switches.get(cfg, "scan.detail_timeout")
        max_tries = switches.get(cfg, "scan.max_tries")
        failsafe = bool(cfg["failsafe"])

        # 来不及跳过：列表直达路线（用户/自愈流程直接落到列表页）也要生效。
        # 判定与倒计时路线共用 skip_late.judge_words，不另写一份。
        try:
            late_s = skip_late.threshold(cfg)
        except skip_late.SkipLateConfigError as e:
            raise Aborted(f"来不及跳过配置不合法：{e}")
        # 没有倒计时区域就没有「离解锁还剩多久」可读，这条路线不判定
        # （硬判会把在售剩余时间当解锁时间，反而乱跳）。
        clock_region = cfg.region("clock")
        clock_px = (screen.norm_to_px(expand(clock_region, 0.1))
                    if clock_region else None)

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
            self._hb.beat()
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
                    # ==== FEATURE-GRADE:BUDGET BEGIN ====
                    detail = f"预算用尽（已花 {spent:.0f} / 预算 {budget:.0f}）"
                    self._mark_stop(cfg, "budget", detail, say=False)
                    try:
                        if switches.get(cfg, "env.record"):
                            budget_extra = {"stop_reason": "budget",
                                            "stop_detail": detail}
                            # 总开关关掉时台账里不带品阶字段（本功能完全不参与）
                            if stop_grade.enabled(stop_grade.settings(cfg)):
                                budget_extra["grade_counts"] = \
                                    self._grade_counter().snapshot()
                            records.add(
                                name=task.get("name") or "预算停止", price=None,
                                result="over_budget",
                                mode=(task.get("mode") or getattr(self, "_mode", "")
                                      or task.get("workflow") or ""),
                                quota=quota, extra=budget_extra)
                    except Exception:      # noqa: BLE001 - 记不下也不能挡住收尾
                        pass
                    # ==== FEATURE-GRADE:BUDGET END ====
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

            # ==== FEATURE-GRADE:GATE BEGIN ====
            # 每次尝试前按当前计数过闸门：scope=all 直接整轮停，scope=grade 只跳这一档。
            verdict, gk = self._grade_gate(cfg, row.get("text") or "")
            if verdict == "abort":
                s = stop_grade.settings(cfg)
                counts = self._grade_counter().snapshot()
                text = stop_grade.detail(s, counts, [gk])
                self._mark_stop(cfg, "grade", text + "，再次开抢前整轮停止")
                raise Aborted(f"品阶达标：{text}，按设置整轮停止")
            if verdict == "skip":
                tried.add((round(row["x0"]), round(row["y0"])))
                self._say(f"{stop_grade.CN[gk]} 已达目标件数，跳过「"
                          f"{(row.get('text') or '')[:30]}」，其余品阶继续", "info")
                continue
            # ==== FEATURE-GRADE:GATE END ====
            tries += 1
            self._say(f"候选：「{row['text'][:50]}」 价格 {self._fmt(row['price'])} · {why}", "hit")
            tried.add((round(row["x0"]), round(row["y0"])))

            # scan 的 x0/cy 是浮点；SetCursorPos 只收整数，浮点会当场
            # ctypes.ArgumentError（真机踩过）
            target_x = int(list_px[0] + max(row["x0"], 0) + 40)
            target_y = int(list_px[1] + int(row["cy"]))

            self._set_state(WATCH)
            self._click(mouse, target_x, target_y)

            # 来不及跳过：点进详情后首读这一帧就判。走列表直达路线时以前没有
            # 这道闸，同一件明明来不及也会硬进场 —— 两条路径一个跳一个不跳，
            # 真机上就是「有时候跳有时候不跳」。
            if clock_px is not None and late_s > 0:
                # 先让详情面板渲染出来，读到的才是它的钟，不是列表残留。
                time.sleep(min(0.55, detail_timeout))
                words = self._ocr_clock(screen, clock_px)
                reading = " ".join(w["text"] for w in words)
                verdict = skip_late.judge_words(late_s, words)
                if verdict["skip"]:
                    sec, kind = verdict["display_sec"], verdict["kind"]
                    entry = skip_late.stats_entry(
                        row.get("text") or reading, verdict, sec, kind)
                    with self._lock:
                        lst = self._stats.setdefault("late_skips", [])
                        lst.append(entry)
                        del lst[:-30]
                    # 不入账：这次根本没进场，不算一次尝试，也不记失败。
                    tries -= 1
                    self._say(
                        f"  「{(row.get('text') or '')[:30]}」 —— 来不及"
                        f"（{verdict['source']} {verdict['remaining_s']:.2f}s"
                        f" < {late_s}s），跳过不进场", "warn")
                    self._set_state(SCAN)
                    continue

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
                # ==== FEATURE-GRADE:AFTER-HIT BEGIN ====
                # 成交入账后按品阶收口：scope=all 抛 Aborted，scope=grade 只放掉该档。
                self._grade_after_hit(cfg)
                # ==== FEATURE-GRADE:AFTER-HIT END ====
                if bought >= quota:
                    # ==== FEATURE-GRADE:QUOTA BEGIN ====
                    self._mark_stop(cfg, "quota", f"总量达标（已抢 {bought}/{quota} 件）")
                    # ==== FEATURE-GRADE:QUOTA END ====
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
            limit = switches.get(cfg, "stop.on_fail")
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
        threshold = switches.get(cfg, "fire.threshold")
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
        interval = 1.0 / max(1, switches.get(cfg, "fire.hz"))
        fire_timeout = switches.get(cfg, "fire.timeout")
        confirm_clicks = max(1, switches.get(cfg, "fire.confirm_clicks"))
        confirm_gap = max(0.0, switches.get(cfg, "fire.confirm_gap") / 1000.0)
        failsafe = bool(cfg["failsafe"])

        # ---- 开点前的那点延迟 ----
        # 「解锁了」和「按钮画出来了」不是同一时刻。差几十毫秒，点下去就是空点。
        # fire_delay_ms 是固定的那一段；fire_jitter_ms 是额外随机量（避免每轮
        # 节奏完全一样）；fire_adaptive 按上一轮实测的「点下去→弹窗出现」
        # 耗时反推该提前还是推后（最多只吃掉一半，防止来回震荡）。
        delay = max(0.0, switches.get(cfg, "fire.delay") / 1000.0)
        adaptive = bool(switches.get(cfg, "fire.adaptive"))
        if adaptive and self._fire_lag > 0:
            delay = max(0.0, delay - self._fire_lag * 0.5)
        jitter = max(0.0, switches.get(cfg, "fire.jitter") / 1000.0)
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
        # ==== FEATURE:telemetry BEGIN ====
        # 本 attempt 的「第一下点击」时刻：没点过就保持 None，台账写 null。
        # 零点只认紧挨着这次开火、由 _watch_countdown 反推出来的那个；
        # 其它路线（详情/列表）没有倒计时零点，一律清空，绝不拿上一轮的零点。
        if not getattr(self, "_zero_primed", False):
            self._zero_perf = None
        self._zero_primed = False
        self._click_perf = None

        def _click_buy(hold=None):
            if self._click_perf is None:
                self._click_perf = time.perf_counter()
            # 连点阶段会把「按住多久」传进来；普通开火不传，保持原调用形态
            if hold is None:
                return self._click(mouse, *buy_click)
            return self._click(mouse, *buy_click, hold=hold)
        # ==== FEATURE:telemetry END ====
        # ==== FEATURE:click.burst BEGIN ====
        # 连点参数（click.burst.*）：抽签模型下只缩短「没中之后再次进池」的
        # 等待，不提高单次中签概率。append = 完全保持下面的 click_hz 节奏，
        # 本组参数一下都不多点；burst = 开点瞬间按「每次按住 hold_ms、两次
        # 之间隔 interval_ms」反复点，window 用尽、或确认弹窗出现，立刻回到
        # 原来的 click_hz 节奏。
        # 点击只走 self._click → Mouse.click（三通道 / 零位移通道），
        # 不另起注入，也不绕过游戏自己的排队。
        burst_plan = click_burst.begin(cfg, say=self._say)
        if burst_plan and not got_confirm:
            rep = click_burst.run(
                burst_plan, t_fire=t_fire, fire_timeout=fire_timeout,
                click=_click_buy,
                dialog=lambda: match(to_gray(screen.grab(confirm_px)),
                                     confirm_tpl)[0] >= threshold,
                stop=self._stop.is_set,
                guard=lambda: self._failsafe(failsafe),
                beat=self._hb.beat,
            )
            clicks += int(rep.get("clicks") or 0)
            self._set(clicks=clicks, burst=rep)
            if rep.get("stopped_by") == "dialog":
                self._fire_lag = time.perf_counter() - t_fire
                self._say(f"连点阶段确认弹窗出现，共 {clicks} 次 / "
                          f"{self._fire_lag:.2f}s", "hit")
                got_confirm = True
        # ==== FEATURE:click.burst END ====
        while not got_confirm and time.perf_counter() - t_fire < fire_timeout:
            self._hb.beat()
            t_iter = time.perf_counter()
            if self._stop.is_set():
                raise Aborted("用户停止")
            self._failsafe(failsafe)
            _click_buy()
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
            # ==== FEATURE:buy.followup BEGIN ====
            # 假购买补刀：开火超时没等到确认弹窗，界面可能只是卡住/被遮挡/
            # 点空了。判定为「疑似没点成」时，在同一购买点再补 1 次点击，
            # 补完再看一眼确认弹窗；还是没有就按 miss 记（不会无限重试）。
            # 生效条件：开关开 + 连点没开 + 有确认区域（无弹窗版式在上面的
            # _buy_without_dialog 已经分流） + 非半自动（半自动更早就 Abort 了）。
            followup_enabled = bool(switches.get(cfg, "buy.followup.enabled"))
            # 互斥判定复用第 6 路（连点参数）的 begin() 结果：只有它真的
            # 进 burst 阶段才算「连点开着」，append（含没写/被拒回落）都放行。
            followup_effective = followup_enabled and not burst_plan
            self._followup_used = False
            self._set(followup={"enabled": followup_enabled,
                                "effective": followup_effective, "used": False})
            if followup_enabled and burst_plan:
                # 前端在连点区块下方也能看到同一句（/api/switches 的 warnings），
                # 这里留 info 是为了让「为什么这次没补刀」在日志里也查得到。
                self._say(click_burst.MUTEX_REASON, "info")
            if followup_effective:
                self._say("疑似没点成：在同一购买点补 1 次点击", "warn")
                _click_buy()
                self._followup_used = True
                # 补刀后最多看 0.25s：弹窗出现就接着走确认流程，仍没有就认输。
                t_fu = time.perf_counter()
                while time.perf_counter() - t_fu < 0.25:
                    if self._stop.is_set():
                        raise Aborted("用户停止")
                    cscore, _ = match(to_gray(screen.grab(confirm_px)), confirm_tpl)
                    if cscore >= threshold:
                        got_confirm = True
                        self._say("补刀后确认弹窗出现", "hit")
                        break
                    time.sleep(0.004)
                self._set(followup={"enabled": True, "effective": True, "used": True})
            # ==== FEATURE:buy.followup END ====
            if not got_confirm:
                self._say(f"{fire_timeout}s 内没等到确认弹窗，中止", "warn")
                self._record(cfg, screen, item=item, price=price,
                             elapsed=time.perf_counter() - t_start, result="miss")
                return False

        # ---- 确认框价格超出任务预算：不点确认，先取消再停手 ----
        # 筛选层已经拦过一轮，这里是最后一道闸：真出现价不对的确认框，
        # 宁可少买一件，也不把预算点穿。
        task = cfg.get("active_task", {}) or {}
        try:
            budget = float(task.get("budget") or 0)
        except (TypeError, ValueError):
            budget = 0.0
        if price is not None and budget > 0 and float(price) > budget:
            try:
                res = popup.scan(screen.grab())
                pt = popup.cancel_point(res)
                if pt is not None:
                    self._click(mouse, *pt)
                    self._say("确认框价格超出预算，已点「取消」", "warn")
            except Exception:                # noqa: BLE001  取消失败也不能点确认
                pass
            self._force_scene(sg.SC_CONFIRM_UNEXPECTED,
                              {"confirm_price": float(price),
                               "budget": budget, "expected_item": item})
            self._record(cfg, screen, item=item, price=price,
                         elapsed=time.perf_counter() - t_start, result="miss")
            raise Aborted("确认价格超出预算，没有点确认")

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
        confirm_timeout = max(0.2, switches.read(
            cfg, "confirm_timeout_s", 2.0, "float", 0.05, 600.0,
            key="confirm_timeout_s"))
        t_conf = time.perf_counter()
        confirm_done = False
        conf_clicks = 0
        while not self._stop.is_set():
            self._hb.beat()
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
            retries = max(0, switches.get(cfg, "follow.retry"))
            wait = max(0.05, switches.get(cfg, "follow.wait_ms") / 1000.0)
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
        # ==== FEATURE:telemetry BEGIN ====
        # 这一版没有确认弹窗，也记「第一下点击」的时刻给 offset_ms 用。
        # 零点沿用 _fire_and_confirm 交接过来的那个（倒计时路线可能有）。
        self._click_perf = None
        # ==== FEATURE:telemetry END ====
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
            # ==== FEATURE:telemetry BEGIN ====
            if self._click_perf is None:
                self._click_perf = time.perf_counter()
            # ==== FEATURE:telemetry END ====
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
            # ---- 误买止损：余额掉得比目标价多，立刻停手并记事故 ----
            # 正常成交只应该掉「一件的价」；掉超过 1.2 倍说明数量或价格不对。
            # 这里不做任何补偿操作，只冻结、还原、记录，然后硬停。
            try:
                after = self._balance(screen)
                if (before is not None and after is not None and price
                        and (before - after) > float(price) * 1.2 + 0.01):
                    self._force_scene(sg.SC_MISBUY, {
                        "balance_before": before,
                        "balance_after": after,
                        "expected_price": float(price),
                        "bought_delta": 1,
                    })
                    self._say(f"余额 {before} → {after}，比目标价 {price} 多，"
                              "按误买处理", "error")
                    self._record(cfg, screen, item=item, price=price,
                                 elapsed=elapsed, result="hit")
                    raise Aborted("买到的价格或数量和设置不一致，已停手")
            except Aborted:
                raise
            except Exception:                # noqa: BLE001  核对失败不影响正常记账
                pass
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

    # ==== FEATURE-GRADE:HELPERS BEGIN ====
    def _grade_counter(self):
        if getattr(self, "_grade", None) is None:
            self._grade = stop_grade.Counter()
        return self._grade

    def _grade_quote(self, cfg):
        """(设置, 计数快照, 已达标品阶) —— 每次现读，界面改完 2 秒内生效。"""
        s = stop_grade.settings(cfg)
        counts = self._grade_counter().snapshot()
        return s, counts, stop_grade.reached(s, counts)

    def _grade_on_hit(self, cfg, text):
        """校验成交之后才调用：计一档 + 写 stats + 生成台账 extra。

        总开关关掉（默认）时整个功能不参与：不计数、不写品阶字段，
        只保留「总量达标」这种与品阶无关的台账信息。
        """
        s = stop_grade.settings(cfg)
        extra = {}
        if stop_grade.enabled(s):
            gk = stop_grade.grade_key(text)
            if gk == stop_grade.UNKNOWN:
                # 契约 §2.5：读不出品阶的件不参与闸门、不计数，日志一条 info，不许猜
                self._say("这件读不出品阶：不参与品阶闸门，也不计入任何目标", "info")
            counts = self._grade_counter().record(gk)
            self._set(grade_counts=counts)
            _s, counts, hits = self._grade_quote(cfg)
            extra = {"grade": gk, "grade_counts": counts}
            if hits and s["scope"] == "all":
                extra["stop_reason"] = "grade"
                extra["stop_detail"] = stop_grade.detail(s, counts, hits) + "，整轮停止"
            elif hits:
                extra["grade_reached"] = stop_grade.detail(s, counts, hits)
        # 台账里也要能看出「这一件抢完总量就达标了」（品阶已停时仍然记一条）
        if "stop_reason" not in extra:
            task = cfg.get("active_task") or {}
            quota = max(1, int(task.get("quota") or 1))
            after = int(self._stats.get("bought") or 0) + 1
            if after >= quota:
                extra["stop_reason"] = "quota"
                extra["stop_detail"] = f"总量达标（已抢 {after}/{quota} 件）"
        return extra

    def _grade_gate(self, cfg, text):
        """每次尝试前的品阶闸门：返回 (verdict, 品阶)；verdict ∈ ok/skip/abort。"""
        s = stop_grade.settings(cfg)
        if not stop_grade.armed(s):
            return "ok", None
        gk = stop_grade.grade_key(text)
        if gk == stop_grade.UNKNOWN:
            return "ok", gk
        _s, _c, hits = self._grade_quote(cfg)
        if gk in hits:
            return ("abort" if s["scope"] == "all" else "skip"), gk
        return "ok", gk

    def _mark_stop(self, cfg, reason, detail, say=True):
        """把停止原因落到 stats（status 可见），并留一条日志。"""
        self._set(stop_reason=reason, stop_detail=detail)
        if say:
            self._say(f"停止原因：{detail}", "warn")

    def _grade_after_hit(self, cfg):
        """成交入账后按 scope 决定：整轮停（Aborted）或只停该品阶。"""
        s, counts, hits = self._grade_quote(cfg)
        if not stop_grade.armed(s) or not hits:
            return
        text = stop_grade.detail(s, counts, hits)
        if s["scope"] == "all":
            self._mark_stop(cfg, "grade", text + "，整轮停止")
            raise Aborted(f"品阶达标：{text}，按设置整轮停止")
        self._say(f"品阶达标：{text}；只停该品阶，其余继续抢", "warn")
    # ==== FEATURE-GRADE:HELPERS END ====

    def _record(self, cfg, screen, item="", price=None, elapsed=None, result="hit"):
        """留图 + 记账。两件事都不许影响抢购：失败了只记一条警告。

        只有成交才留图 —— 开关叫「成交后存截图」，没抢到也存就是在骗人。
        """
        # ==== FEATURE:telemetry:EXTRA BEGIN ====
        # 台账补两列（遥测那边已按这两个名字读，只追加、不改既有字段）：
        #   offset_ms = 第一下点击时刻 − 反推的理论零点（归零=解锁），毫秒整数，
        #               可正可负；没有可用零点必须写 null —— 0 会被统计成
        #               「完美卡点」，绝不拿 0 充数。
        #   tier      = 项目既有判阶结果（bricks.find_skin 的 tier）；
        #               判不出来写「未标注」，不默认成任何一档。
        offset_ms = None
        _click_at = getattr(self, "_click_perf", None)
        _zero_at = getattr(self, "_zero_perf", None)
        if _click_at is not None and _zero_at is not None:
            try:
                offset_ms = int(round((float(_click_at) - float(_zero_at)) * 1000.0))
            except (TypeError, ValueError, OverflowError):
                offset_ms = None
        # 消费过就清掉，别串到下一次购买
        self._click_perf = None
        self._zero_perf = None
        self._zero_primed = False
        tier = "未标注"
        try:
            from . import bricks
            _name = ((item if isinstance(item, str) else "")
                     or ((cfg.get("active_task", {}) or {}).get("name") or ""))
            _hit = bricks.find_skin(_name)
            tier = str((_hit or {}).get("tier") or "未标注")
        except Exception:                   # noqa: BLE001 判阶失败不能影响记账
            tier = "未标注"
        # ==== FEATURE:telemetry:EXTRA END ====
        shot = self._save_shot(cfg, screen, tag=item) if result == "hit" else None
        # ==== FEATURE-GRADE:COUNT BEGIN ====
        # 只有校验成交通过才会以 result="hit" 走到这里；miss/未校验不计数。
        grade_extra = {}
        if result == "hit":
            grade_extra = self._grade_on_hit(
                cfg, item or (cfg.get("active_task", {}) or {}).get("name") or "")
        # ==== FEATURE-GRADE:COUNT END ====
        if not switches.get(cfg, "env.record"):
            return shot
        task = cfg.get("active_task", {}) or {}
        # `mode` 以前取 task.get("mode") —— active_task 里根本没这个键，
        # 记录里 mode 永远空。依次回退：任务自带 / 引擎启动时的 mode / 工作流。
        mode = (task.get("mode") or getattr(self, "_mode", "")
                or task.get("workflow") or "")
        verified_by = getattr(self, "_verified_by", "")
        # ---- feature:scan.repeat BEGIN ----
        _rc = getattr(self, "_repeat_ctx", None) or {}
        # ---- feature:scan.repeat END ----
        try:
            records.add(name=item or task.get("name") or "", price=price,
                        result=result, elapsed=elapsed,
                        shot=os.path.basename(shot) if shot else "",
                        mode=mode, quota=task.get("quota"),
                        # verified 仍是「这轮开着校验」：miss 也是核对过才记的。
                        # 具体靠什么判定成交（结算弹窗 / 余额下降 / 关了校验）
                        # 放进 extra，否则两种可信度完全不同的成交分不开（D2）。
                        verified=bool(switches.get(cfg, "fire.verify")),
                        extra={"verified_by": verified_by,
                               # 这次成交/失败前有没有补过刀（只记事实，不影响金额）
                               "followup_used": bool(getattr(self, "_followup_used", False)),
                               # ---- feature:scan.repeat BEGIN ----
                               # 这一件刷新用了几次 / 本轮给它抽了几次（0 = 没刷）
                               "refresh_budget": int(_rc.get("refresh_budget") or 0),
                               "refresh_used": int(_rc.get("refresh_used") or 0),
                               # ---- feature:scan.repeat END ----
                               # ==== FEATURE-SKIP-LATE:EXTRA BEGIN ====
                               # 本轮因「来不及」跳过的件数（契约 §4.3 追加字段）
                               "late_skips": len(self._stats.get("late_skips") or []),
                               # ==== FEATURE-SKIP-LATE:EXTRA END ====
                               # ==== FEATURE-BURST:EXTRA BEGIN ====
                               # 连点阶段实际点了几下（只记事实，不代表中签概率）
                               "burst_clicks": int((self._stats.get("burst") or {}).get("clicks") or 0),
                               # ==== FEATURE-BURST:EXTRA END ====
                               # ==== FEATURE-GRADE:EXTRA BEGIN ====
                               # 本件的品阶 / 本轮各档计数 / 停止原因（契约 §4.3）
                               **grade_extra,
                               # ==== FEATURE-GRADE:EXTRA END ====
                               # ==== FEATURE:telemetry:EXTRA BEGIN ====
                               # 台账两列：点击相对理论零点的毫秒偏差 / 商品品阶
                               "offset_ms": offset_ms,
                               "tier": tier,
                               # ==== FEATURE:telemetry:EXTRA END ====
                               })
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
            # 截图是「证据」，但也是唯一会无限增长的大文件。超上限就删最旧的。
            retention.prune_dir(d, retention.SHOTS_MAX_FILES,
                                retention.SHOTS_MAX_BYTES)
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
            # 账户级问题：不重试购买，直接进第 4 级（取消 + 一句话 + 唯一按钮）
            self._force_scene(sg.SC_INSUFFICIENT,
                              {"purchase_blocked": True, "why": msg})
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
