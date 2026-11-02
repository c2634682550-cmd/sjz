"""任务队列 + 调度器。

一条任务 = 一个要抢的商品：
    {id, name, keywords, mode, regions, fire_at, status, note, result,
     min_price, max_price, max_wear, craft, wear, season, sort}

fire_at 是"开卖时刻"（校时后的 Unix 时间戳，0 表示随时可抢/由按钮触发）。

mode 决定工作流：
    detail  详情页模式——停在某件商品页上，盯购买按钮变色（原来的做法）
    list    列表模式——盯交易行搜索结果，挑第一条符合价格/做工/成色条件的
            挂单买。捡漏靠这个：东西什么时候上架、叫什么名字都不知道，
            只能等它出现在列表里。

调度器一个线程，按 fire_at 排序，到点前 ARM_LEAD 秒把引擎切到该任务
的模板，到点触发。多任务串行执行——同一时刻只有一个鼠标，并行没有意义。

状态流转：
    pending → armed → running → done / failed / skipped
"""
import os
import shutil
import threading
import time
import traceback
import uuid
from collections import deque

from . import timesync
from .config import Config
from .engine import Engine

PENDING, ARMED, RUNNING, DONE, FAILED, SKIPPED = (
    "pending", "armed", "running", "done", "failed", "skipped")

# 抢购路线的中文名（日志里给用户看的）
WORKFLOW_CN = {"countdown": "公示期倒计时", "list": "在售列表", "detail": "商品页按钮"}

ARM_LEAD = 20.0      # 提前 20 秒装载模板、进入布防
POLL_IDLE = 0.25


class Scheduler:
    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._engine = Engine()
        self._log = deque(maxlen=400)
        self._active_id = None
        self._cfg = Config.load()
        self._recover_stale_tasks()

    def _recover_stale_tasks(self):
        """把上次没跑完的任务放回队列。

        为什么必须做：任务状态是存在 config.json 里的，进程被强杀（任务
        管理器结束、机器断电）时，状态会停在 `running`。调度器只挑
        `pending`/`armed` 的任务跑 —— 卡在 `running` 的那条就**永远不再
        跑**，队列看着在那儿、其实已经死了。

        这直接影响「停掉再启动能不能接着干」：不恢复的话，重启后那条任务
        悄悄消失，用户以为还在跑。
        """
        with self._lock:
            stale = [t for t in self._cfg["queue"]
                     if t.get("status") in (ARMED, RUNNING)]
            if not stale:
                return
            for t in stale:
                t["status"] = PENDING
                t["note"] = (t.get("note") or "") + "（上次没跑完，已放回队列）"
            self._cfg.save()
        for t in stale:
            self._say(f"{self._label(t)}：上次没跑完，已放回队列", "warn")

    # ---------------- 队列管理 ----------------

    def tasks(self):
        with self._lock:
            return [dict(t) for t in self._cfg["queue"]]

    def add(self, name, keywords=None, fire_at=0, note="", regions=None, template_from=None,
            mode="detail", min_price=0, max_price=0, max_wear=0,
            craft="", wear="", season="", sort="price", quota=1, budget=0,
            workflow=None, materials=None, colors=None, categories=None):
        """加一条任务。

        regions 不传就沿用当前框选；template_from 是另一个任务的 id，把它的
        模板复制过来——批量加同一类商品时不用每个都重截一遍。

        mode=list 的任务不需要 buy 模板也能加（列表模式靠 OCR 找行，不靠
        模板匹配），但确认弹窗的模板还是要的。

        workflow 决定走哪条抢购路线（countdown / list / detail），不传就用
        全局配置的默认值。**任务说了算** —— 以前工作流由全局 trigger_mode
        决定，界面上写的和实际跑的会对不上。
        """
        # 空名 = 不按名称过滤：抢所有皮肤（引擎 `_matches_name` 见空名全放行）。
        # 控制台只维护这一条任务，顾客填价格条件就行，不用先认出皮肤叫什么。
        if name is None:
            return None, "商品名不能为空"
        mode = mode if mode in ("detail", "list") else "detail"
        workflow = workflow or Config.load().get("workflow") or "countdown"
        if workflow not in ("countdown", "list", "detail"):
            return None, f"不认识的抢购方式：{workflow}"
        with self._lock:
            cfg = Config.load()
            regions = regions or {k: v for k, v in cfg["regions"].items() if v}
            # 「确认」模板只在**有确认弹窗**的界面才需要。这一版界面点价格
            # 按钮直接成交（实测），没有弹窗可截 —— 硬要求会让任务永远加不上。
            # 引擎那边没有确认区域时改走「点一次 + 看余额」的判定。
            need = () if mode == "list" else ("buy",)
            if mode == "list":
                # 列表模式必须知道盯哪块屏幕，以及点进去后确认按钮在哪
                if not regions.get("list"):
                    return None, "列表模式要先框选交易行列表区域"
            missing = [k for k in need if not regions.get(k)]
            if missing:
                return None, "先把购买按钮框选好再添加任务"
            tid = uuid.uuid4().hex[:8]
            task = {
                "id": tid,
                "name": name.strip(),
                "keywords": [k for k in (keywords or []) if k],
                "mode": mode,
                # 走哪条抢购路线（countdown / list / detail）
                "workflow": workflow,
                "regions": regions,
                "fire_at": float(fire_at or 0),
                "status": PENDING,
                "note": note or "",
                "created": time.time(),
                "result": None,
                # 列表模式的筛选条件。0/空 = 不限
                "min_price": float(min_price or 0),
                "max_price": float(max_price or 0),
                "max_wear": float(max_wear or 0),
                "craft": (craft or "").strip(),
                "wear": (wear or "").strip().upper(),
                "season": (season or "").strip().upper(),
                "sort": (sort or "price").strip(),
                # 游戏筛选面板里的三组勾选（列表工作流用得上）：
                # 材质 / 颜色 / 更多分类
                "materials": [str(m) for m in (materials or []) if str(m).strip()],
                "colors": [str(c) for c in (colors or []) if str(c).strip()],
                "categories": [str(c) for c in (categories or []) if str(c).strip()],
                # 本轮限量：这一条任务抢够几件就停（默认 1）。
                # 预算上限：这一条任务最多花多少金币，0 = 不限。
                "quota": max(1, int(quota or 1)),
                "budget": max(0.0, float(budget or 0)),
                # 启用开关：停用的任务留在队列里但不跑（改条件时先停掉）
                "enabled": True,
                # 已抢几件 / 已花多少：抢购是累计的，列表要看得见进度
                "bought": 0,
                "spent": 0.0,
            }
            cfg["queue"].append(task)
            cfg.save()
            self._cfg = cfg
            self._say(f"已加入队列：{task['name'] or '所有皮肤'}"
                      + (f"（{time.strftime('%H:%M:%S', time.localtime(fire_at))} 开卖）"
                         if fire_at else "（手动触发）"))
        if template_from:
            self._copy_templates(template_from, tid)
        else:
            self._snapshot_global_templates(tid)
        return task, None

    def _snapshot_global_templates(self, tid):
        """把当前全局模板存一份给这条任务。

        模板是按商品截的（占位按钮长什么样、确认弹窗长什么样），不同商品
        可能不一样。加任务时快照一份，之后重新框选/重截全局模板也不会把
        已排队的任务改坏。
        """
        cfg = Config.load()
        for name in ("buy", "confirm"):
            src = cfg.template_path(name)
            if os.path.isfile(src):
                shutil.copyfile(src, cfg.template_path(name, tid))

    def _copy_templates(self, src_id, dst_id):
        """把 src 任务的模板复制给 dst，缺模板时返回缺失的名字。"""
        if src_id == dst_id:
            return []
        cfg = Config.load()
        missing = []
        for name in ("buy", "confirm"):
            src = cfg.template_path(name, src_id)
            if not os.path.isfile(src):
                missing.append(name)
                continue
            shutil.copyfile(src, cfg.template_path(name, dst_id))
        return missing

    def remove(self, task_id):
        """删掉一条任务。

        条件改了就该把旧任务删掉：留着它，调度器会按老条件再跑一遍 ——
        老条件可能是「不限价」，那就是真金白银的事故。
        """
        with self._lock:
            cfg = Config.load()
            before = len(cfg["queue"])
            cfg["queue"] = [t for t in cfg["queue"] if t.get("id") != task_id]
            cfg.save()
            self._cfg = cfg
            return before - len(cfg["queue"])

    def clear_finished(self):
        with self._lock:
            cfg = Config.load()
            cfg["queue"] = [t for t in cfg["queue"]
                            if t["status"] not in (DONE, FAILED, SKIPPED)]
            cfg.save()
            self._cfg = cfg
            return len(cfg["queue"])

    def update(self, task_id, **fields):
        with self._lock:
            cfg = Config.load()
            for t in cfg["queue"]:
                if t["id"] == task_id:
                    t.update(fields)
                    cfg.save()
                    self._cfg = cfg
                    return True
            return False

    # ---------------- 运行 ----------------

    def start(self, dry_run=True):
        if self._thread and self._thread.is_alive():
            return False, "调度器已在运行"
        with self._lock:
            cfg = Config.load()
            if not cfg["queue"]:
                return False, "队列是空的"
            self._cfg = cfg
        # 执行模式是全局开关：dry 一定不点，semi 走到购买键前停手，
        # auto 才真点。界面上的「演练模式」也是这条。
        from . import switches
        mode = switches.get(cfg, "run.mode")
        if mode == "dry":
            dry_run = True
        self._stop.clear()
        self._engine = Engine()
        # 「抢到就停」靠这个回调通知调度器自己停 —— 引擎不认识调度器
        self._engine.on_hit = self.stop
        self._thread = threading.Thread(target=self._run, args=(dry_run,), daemon=True)
        self._thread.start()
        return True, "调度器已启动"

    def stop(self):
        self._stop.set()
        self._engine.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        with self._lock:
            for t in self._cfg["queue"]:
                if t["status"] in (ARMED, RUNNING):
                    t["status"] = PENDING
            self._cfg.save()
        return True, "已停止"

    def status(self):
        with self._lock:
            tasks = [dict(t) for t in self._cfg["queue"]]
            active = self._active_id
            log = list(self._log)[-80:]
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "active": active,
            "tasks": tasks,
            "log": log,
            "engine": self._engine.status(),
            "clock": timesync.info(),
        }

    def busy(self):
        """现在是不是正在跑一轮抢购。

        OCR 服务管家靠它决定「现在能不能重启」—— 蓝绿虽然不断流，但新实例
        加载模型要占显存和带宽，正在抢的那一轮里没必要折腾。
        """
        try:
            return bool(self._engine.status().get("running"))
        except Exception:                        # noqa: BLE001
            return False

    def note(self, msg, level="info"):
        """外部往日志里写一行（热键、启动前检查这些不是任务自己在说的话）。"""
        self._say(msg, level)

    @staticmethod
    def _label(task):
        """日志里任务怎么称呼。空名任务是「抢所有皮肤」那条。"""
        return (task.get("name") or "").strip() or "所有皮肤"

    def _say(self, msg, level="info"):
        # ts 是浮点时间戳：同一秒里的多行要按真实先后排，光看 "HH:MM:SS"
        # 会把这一秒的顺序打乱（抢购时一秒内好几行很常见）。
        self._log.append({"t": time.strftime("%H:%M:%S"), "ts": time.time(),
                          "level": level, "msg": msg})

    def _set_status(self, task_id, status, result=None):
        with self._lock:
            for t in self._cfg["queue"]:
                if t["id"] == task_id:
                    t["status"] = status
                    if result is not None:
                        t["result"] = result
                    self._cfg.save()
                    break

    def _run(self, dry_run):
        try:
            while not self._stop.is_set():
                pending = [t for t in self.tasks()
                           if t["status"] in (PENDING, ARMED) and t.get("enabled", True)]
                if not pending:
                    self._say("队列跑完了")
                    break
                # 手动触发的（fire_at=0）就是"现在要抢"，排在所有定时任务前面；
                # 否则排一条一小时后开卖的任务，手动那条会白等一小时。
                pending.sort(key=lambda t: (1, t["fire_at"]) if t["fire_at"] else (0, 0))
                task = pending[0]
                fire_at = task["fire_at"]

                if fire_at:
                    now = timesync.true_now()
                    if task["status"] != ARMED:
                        if now < fire_at - ARM_LEAD:
                            wait = min(fire_at - ARM_LEAD - now, 30.0)
                            self._say(f"下一条：{self._label(task)}，"
                                      f"{fire_at - now:.0f}s 后进入布防")
                            self._stop.wait(wait)
                            continue
                        self._set_status(task["id"], ARMED)
                        self._say(f"进入布防：{self._label(task)}，"
                                  f"{fire_at - now:.1f}s 后开卖", "hit")

                # 布防即开跑：引擎一启动就在盯按钮，先确认占位按钮在（布防），
                # 再等它变化。不能等开卖那一刻才启动——那时按钮已经变了，
                # 引擎永远看不到"变化"这个事件。
                self._run_task(task, dry_run)
                if self._stop.is_set():
                    break
        except Exception as e:
            self._say(f"调度器异常：{type(e).__name__}: {e}", "error")
            for line in traceback.format_exc().splitlines()[-6:]:
                self._say(line, "error")
        finally:
            with self._lock:
                self._active_id = None

    @staticmethod
    def _engine_task(task):
        """任务 → 引擎读的 active_task。

        字段漏一个，界面上设的那一项就不生效（之前 quota/budget 就是这么丢的）。
        加字段时这里、`Engine._execute_list` 和前端三处都要跟着改。

        **任务没写的字段，退回全局开关的值。** 这一条很关键：
        设置页上「最低价 / 最高价 / 每轮限量 / 总预算 / 做工 / 最大磨损 /
        挑单顺序」七个开关写的是全局配置，而任务对象里通常没有这些字段。
        不做回退的话，开关拨了、任务一跑就被这里的 `or 0` / `or ""` 盖掉 ——
        界面上能拨，实际不生效，正是「假开关」。
        """
        from . import switches

        g = Config.load()
        console = g.get("console") or {}

        def num(task_val, global_val, zero_means_unset=True):
            """任务给了就用任务的；没给（None/空/0）就用全局。"""
            if task_val in (None, ""):
                return global_val
            if zero_means_unset and not task_val:
                return global_val
            return task_val

        return {
            "name": task.get("name"),
            "keywords": task.get("keywords") or [],
            "min_price": num(task.get("min_price"), console.get("min_price") or 0),
            "max_price": num(task.get("max_price"), console.get("max_price") or 0),
            "max_wear": num(task.get("max_wear"),
                            switches.get(g, "filter.wear_max") or 0),
            "craft": task.get("craft") or switches.get(g, "filter.craft") or "",
            "wear": task.get("wear") or "",
            "season": task.get("season") or "",
            "sort": task.get("sort") or switches.get(g, "filter.sort") or "price",
            "quota": num(task.get("quota"), console.get("quota") or 1,
                         zero_means_unset=False) or 1,
            "budget": num(task.get("budget"), console.get("budget") or 0),
            "materials": task.get("materials") or [],
            "colors": task.get("colors") or [],
            "categories": task.get("categories") or [],
        }

    def _run_task(self, task, dry_run):
        tid = task["id"]
        mode = task.get("mode") or "detail"
        workflow = task.get("workflow") or "countdown"
        self._active_id = tid
        self._set_status(tid, RUNNING)
        self._say(f"开始执行：{self._label(task)}"
                  f"{'（' + WORKFLOW_CN.get(workflow, workflow) + '）'}", "hit")

        cfg = Config.load()
        # 把任务的区域和模板套到配置上，引擎只认配置
        for k, v in (task["regions"] or {}).items():
            if k in cfg["regions"]:
                cfg.set_region(k, v)
        # 列表模式的筛选条件也交给引擎：引擎读 active_task 决定买哪一条
        cfg["active_task"] = self._engine_task(task)
        cfg.save()

        # 任务自己的模板复制到全局位置，引擎从那儿读。任务没模板就退回全局的
        # ——加任务时是快照的，正常都有；全局的也缺才是真缺。
        # 列表模式只强依赖确认模板；购买模板只在点进详情页后用到，缺了也能扫。
        need = ("confirm",) if mode == "list" else ("buy", "confirm")
        for name in need:
            src = cfg.template_path(name, tid)
            if not os.path.isfile(src):
                src = cfg.template_path(name)
            if not os.path.isfile(src):
                self._set_status(tid, FAILED, f"缺少 {name} 模板")
                self._say(f"{self._label(task)}：缺少 {name} 模板，重新截一次", "error")
                return
            shutil.copyfile(src, cfg.template_path(name))
        if mode == "list":
            # 有就带上，没有就不带——引擎在点进详情页时才读它
            src = cfg.template_path("buy", tid)
            if not os.path.isfile(src):
                src = cfg.template_path("buy")
            if os.path.isfile(src):
                shutil.copyfile(src, cfg.template_path("buy"))

        ok, msg = self._engine.start(dry_run=dry_run, deadline=task["fire_at"] or None,
                                     mode=mode, workflow=workflow)
        if not ok:
            self._set_status(tid, FAILED, msg)
            self._say(f"{self._label(task)}：{msg}", "error")
            return

        t0 = time.time()
        while not self._stop.is_set():
            st = self._engine.status()
            if st["state"] in ("done", "abort"):
                break
            if not st.get("running"):
                # 引擎线程已经结束，状态却不是终态（引擎内部出错、或者状态
                # 没落到 done/abort）。这时再等下去就是死等 —— 整条队列卡死，
                # 后面的任务永远排不上。
                break
            time.sleep(0.05)

        st = self._engine.status()
        self._engine.stop()
        elapsed = round(time.time() - t0, 2)

        # 这一轮抢到几件、花了多少，累计回任务上 —— 列表的「已抢」就靠它
        got = int(st["stats"].get("bought") or 0)
        spent = float(st["stats"].get("spent") or 0)
        if got or spent:
            self.update(tid, bought=int(task.get("bought") or 0) + got,
                        spent=round(float(task.get("spent") or 0) + spent, 1))

        if st["state"] == "done":
            res = f"完成，{st['stats']['clicks']} 次点击，{elapsed}s"
            self._set_status(tid, DONE, res)
            self._say(f"{self._label(task)}：{res}", "hit")
        elif self._stop.is_set():
            self._set_status(tid, PENDING, "被停止")
            self._say(f"{self._label(task)}：被停止", "warn")
        else:
            res = st.get("error") or "未抢到"
            self._set_status(tid, FAILED, res)
            self._say(f"{self._label(task)}：{res}", "warn")
