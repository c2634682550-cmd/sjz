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
import threading
import time
import traceback
from collections import deque

import cv2
import numpy as np

from . import market, timesync
from .config import Config
from .input import Mouse, MouseStolen, WindowNotForeground
from .screen import Screen
from .vision import expand, match, to_gray

IDLE, SCAN, WATCH, FIRE, CONFIRM, DONE, ABORT = (
    "idle", "scan", "watch", "fire", "confirm", "done", "abort")


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
        self._dry_run = True
        self._deadline = None
        self._mode = "detail"
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

    def start(self, dry_run=True, deadline=None, mode="detail"):
        if self._thread and self._thread.is_alive():
            return False, "已经在运行了"
        cfg = Config.load()
        problem = self._validate(cfg, mode)
        if problem:
            return False, problem
        self._stop.clear()
        self._dry_run = bool(dry_run)
        self._mode = mode
        # deadline 是校时后的 Unix 时间戳，换算成本机单调时钟，避免中途系统改时间
        self._deadline = (time.perf_counter() + max(0.0, deadline - timesync.true_now())
                          if deadline else None)
        self._error = None
        self._log.clear()
        self._state = SCAN if mode == "list" else WATCH
        # 演练不点鼠标，也就无所谓前台；实战时把当前前台窗口记成目标，
        # 之后每次点击都要求它仍在前台，否则点击会被投递给别的窗口。
        self._target_hwnd = None if self._dry_run else Mouse.foreground()
        self._stats.update(hz=0.0, score=0.0, clicks=0, elapsed=0.0,
                           started_at=time.time(),
                           trigger_mode=cfg["trigger_mode"],
                           mode=mode, scans=0, rows=0)
        self._thread = threading.Thread(target=self._run, args=(cfg, mode), daemon=True)
        self._thread.start()
        return True, "已启动"

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._lock:
            if self._state in (SCAN, WATCH, FIRE, CONFIRM):
                self._state = ABORT
        return True, "已停止"

    def status(self):
        with self._lock:
            st = dict(self._stats)
            state = self._state
            error = self._error
        if st["started_at"]:
            st["elapsed"] = round(time.time() - st["started_at"], 1)
        return {
            "state": state,
            "running": bool(self._thread and self._thread.is_alive()),
            "dry_run": self._dry_run,
            "mode": self._mode,
            "stats": st,
            "error": error,
            "log": list(self._log)[-60:],
        }

    # ---------------- 内部 ----------------

    @staticmethod
    def _validate(cfg, mode="detail"):
        if mode == "list":
            if not cfg.region("list"):
                return "列表模式要先框选交易行列表区域"
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
        self._log.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

    def _click(self, mouse, x, y):
        """带前台校验的点击。目标窗口不在前台就抛 WindowNotForeground。"""
        mouse.click(x, y, expect_foreground=self._target_hwnd)

    def _set(self, **kw):
        with self._lock:
            self._stats.update(kw)

    def _run(self, cfg, mode="detail"):
        screen = Screen(cfg["monitor"])
        mouse = Mouse(screen)
        try:
            confirm_tpl = self._load(cfg, "confirm")
            w, h = screen.size
            self._say(f"屏幕 {w}x{h} · "
                      f"{'演练模式，只识别不点击' if self._dry_run else '实战模式'}")
            if mode == "list":
                self._say("工作流：列表模式——盯交易行列表，谁先符合条件就买谁")
                self._execute_list(cfg, screen, mouse, confirm_tpl)
            else:
                buy_tpl = self._load(cfg, "buy")
                self._say(f"触发：{'等占位按钮消失' if cfg['trigger_mode'] == 'vanish' else '等可购买按钮出现'}"
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
            screen.close()

    def _set_state(self, state):
        with self._lock:
            self._state = state

    @staticmethod
    def _load(cfg, name):
        img = cv2.imread(cfg.template_path(name), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"模板读不出来：{name}.png")
        return to_gray(img)

    def _execute(self, cfg, screen, mouse, buy_tpl, confirm_tpl):
        threshold = float(cfg["threshold"])
        mode = cfg["trigger_mode"]
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

    # ---------------- 列表模式 ----------------

    def _execute_list(self, cfg, screen, mouse, confirm_tpl):
        """盯交易行列表，挑第一条符合任务条件的行，点进去买。

        OCR 一次约 0.3s，不能每轮都做。先用「缩略图指纹」判断列表有没有变：
        没变就跳过 OCR，循环跑得飞快；变了才付 OCR 的代价。
        列表静止时几乎零开销，一有新挂单立刻能发现。
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
        last_sig = None
        scanned_sig = None
        rows = []
        clicked = []          # 已点过的行的 y 中心，避免反复点同一条
        tries = 0
        scans = 0
        frames = 0
        last_score = 0.0
        task = dict(cfg.get("active_task") or {})
        buy_tpl = None

        self._say(f"列表区域 {list_px[2]}x{list_px[3]} · 条件：{self._describe(task)}")

        while not self._stop.is_set():
            self._failsafe(failsafe)
            now = time.perf_counter()
            frames += 1
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

            sig = self._signature(frame)
            if last_sig is None or abs(sig - last_sig) > 0.004:
                # 画面变了（或第一次）：值得付 OCR 的代价
                if sig != scanned_sig:
                    rows = market.scan(frame)
                    scanned_sig = sig
                    scans += 1
                    self._set(scans=scans, rows=len(rows))
                last_sig = sig
            else:
                time.sleep(scan_interval)
                continue

            row, why, diag = market.pick(rows, task, exclude=clicked)
            if row is None:
                if now - t_beat >= 5.0:
                    t_beat = now
                    self._say(f"扫了 {scans} 轮 · {len(rows)} 行 · {why}")
                    for d in diag[:6]:
                        self._say(f"    「{d['text'][:40]}」{d['why']}", "info")
                time.sleep(scan_interval)
                continue

            tries += 1
            self._say(f"候选：「{row['text'][:50]}」 价格 {self._fmt(row['price'])} · {why}", "hit")
            clicked.append(row["cy"])

            target_x = list_px[0] + max(row["x0"], 0) + 40
            target_y = list_px[1] + int(row["cy"])

            self._set_state(WATCH)
            if self._dry_run:
                # 演练的价值是让人看清「本来会买什么」。只报坐标不够 —— 要报
                # 商品、价格、磨损、点击目标，以及这一屏里所有候选。
                self._say(f"[演练] 会点这一行 → 屏幕 ({target_x}, {target_y})", "warn")
                self._say(f"[演练] 商品：「{row['text'][:60]}」", "warn")
                self._say(f"[演练] 价格：{self._fmt(row['price'])}"
                          f" · 做工：{row['craft'] or '未读出'}"
                          f" · 成色：{row['wear'] or '未读出'}"
                          f" · 磨损：{row['wear_value'] if row['wear_value'] is not None else '未读出'}",
                          "warn")
                self._say(f"[演练] 整屏候选（{len(rows)} 条，按条件过滤后 "
                          f"{sum(1 for d in diag if d['ok'])} 条符合）：", "warn")
                for d in diag[:8]:
                    mark = "✓" if d["ok"] else "×"
                    self._say(f"[演练]   {mark} 「{d['text'][:44]}」{d['why']}", "warn")
                self._set(elapsed=round(time.perf_counter() - t_start, 2))
                self._say("演练结束：没有点击任何东西", "hit")
                self._set_state(DONE)
                return

            self._click(mouse, target_x, target_y)

            # 点进详情页后购买按钮应当立刻可点（东西本来就在卖）。
            # 给它 detail_timeout 秒；没等到就退回列表换下一条。
            if buy_tpl is None:
                buy_tpl = self._load(cfg, "buy")
            got = False
            t_det = time.perf_counter()
            while time.perf_counter() - t_det < detail_timeout:
                if self._stop.is_set():
                    raise Aborted("用户停止")
                self._failsafe(failsafe)
                buy_region = cfg.region("buy")
                if not buy_region:
                    break
                last_score, _ = match(
                    to_gray(screen.grab(screen.norm_to_px(expand(buy_region, 0.15)))),
                    buy_tpl)
                if last_score >= threshold:
                    got = True
                    break
                time.sleep(0.01)

            if not got:
                self._say(f"详情页 {detail_timeout}s 没等到购买按钮"
                          f"（相关度 {last_score:.3f}），退回列表", "warn")
                self._set_state(SCAN)
                if tries >= max_tries:
                    self._say(f"试了 {tries} 条都不行，结束", "warn")
                    break
                continue

            self._say(f"购买按钮出现（相关度 {last_score:.3f}），开点", "hit")
            buy_click = self._center(screen, cfg.region("buy"))
            if self._fire_and_confirm(cfg, screen, mouse, confirm_tpl, buy_click, t_start):
                self._set_state(DONE)
                return
            self._set_state(SCAN)
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
        return " · ".join(bits) or "不限（见任务名）"

    # ---------------- 点击购买 + 确认 ----------------

    def _fire_and_confirm(self, cfg, screen, mouse, confirm_tpl, buy_click, t_start):
        """点购买直到弹窗出现，再点确认。返回是否成功。"""
        threshold = float(cfg["threshold"])
        confirm_region = cfg.region("confirm")
        confirm_px = screen.norm_to_px(expand(confirm_region, 0.25))
        confirm_click = self._center(screen, confirm_region)
        interval = 1.0 / max(1, int(cfg["click_hz"]))
        fire_timeout = float(cfg["fire_timeout_s"])
        confirm_clicks = max(1, int(cfg["confirm_clicks"]))
        confirm_gap = max(0.0, float(cfg["confirm_interval_ms"]) / 1000.0)
        failsafe = bool(cfg["failsafe"])

        # ---- 点购买，边点边等弹窗 ----
        self._set_state(FIRE)
        clicks = 0
        t_fire = time.perf_counter()
        if self._dry_run:
            # 演练不点鼠标，弹窗永远不会出现，所以不能等它——否则要么空转到
            # 超时误报"没等到弹窗"，要么把屏幕上的旧画面当成弹窗。直接模拟。
            self._say(f"[演练] 本该点击购买按钮 {buy_click}，"
                      f"最多连点 {fire_timeout}s", "warn")
            got_confirm = True
        else:
            got_confirm = False
            while time.perf_counter() - t_fire < fire_timeout:
                if self._stop.is_set():
                    raise Aborted("用户停止")
                self._failsafe(failsafe)
                self._click(mouse, *buy_click)
                clicks += 1
                self._set(clicks=clicks)
                # 每点一次就查一次弹窗：弹窗一出现立刻停手，避免后续点击穿透到弹窗里的别处
                cscore, _ = match(to_gray(screen.grab(confirm_px)), confirm_tpl)
                if cscore >= threshold:
                    self._say(f"确认弹窗出现（相关度 {cscore:.3f}），"
                              f"点了 {clicks} 次 / {time.perf_counter() - t_fire:.2f}s", "hit")
                    got_confirm = True
                    break
                time.sleep(interval)

        if not got_confirm:
            self._say(f"{fire_timeout}s 内没等到确认弹窗，中止", "warn")
            return False

        # ---- 点确认 ----
        self._set_state(CONFIRM)
        if self._dry_run:
            self._say(f"[演练] 本该点确认 {confirm_clicks} 次 {confirm_click}", "warn")
        else:
            for _ in range(confirm_clicks):
                if self._stop.is_set():
                    raise Aborted("用户停止")
                self._click(mouse, *confirm_click)
                time.sleep(confirm_gap)

        self._set(elapsed=round(time.perf_counter() - t_start, 2))
        self._say(f"完成，总用时 {time.perf_counter() - t_start:.2f}s", "hit")
        return True

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
