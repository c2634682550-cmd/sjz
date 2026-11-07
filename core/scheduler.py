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
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from collections import deque

from . import timesync
from . import watchdog as wd_mod
from .config import (Config, ConfigLoadError, drop_task_dir, mode_for_workflow,
                     normalize_workflow, task_workflow)
from .validate import check_conditions
from .engine import Engine

PENDING, ARMED, RUNNING, DONE, FAILED, SKIPPED = (
    "pending", "armed", "running", "done", "failed", "skipped")

# 抢购路线的中文名（日志里给用户看的）
WORKFLOW_CN = {"countdown": "公示期倒计时", "list": "在售列表", "detail": "商品页按钮"}

ARM_LEAD = 20.0      # 提前 20 秒装载模板、进入布防

# 看门狗：引擎心跳超过这么久没跳就认定卡死（截图驱动/OCR 服务把线程堵住）。
# 60 秒足够宽 —— 正常倒计时循环每几毫秒 beat 一次，真卡住才能到 60。
WATCHDOG_STALL_S = 60.0
# 卡死在「还在盯」阶段时自动重开一轮；最多这么多次，之后交给人。
WATCHDOG_MAX_RESTARTS = 2

# S9：调度级看门狗（core/watchdog.py 的 Heartbeat / Watchdog）。
# 以前这个模块根本没被调度器引用，等于摆设：调度线程卡在 borrow_config /
# engine.start 这类同步调用里时，引擎心跳没人读，整条队列到天亮没动静。
#   · step_s  引擎心跳（某一步）超过这么久 = 某一步卡住／死循环；
#   · round_s 调度线程自己的心跳（整轮）超过这么久 = 整轮卡住；
#   · confirm 连续确认次数，防抖（慢但还在推进不算卡死）；
#   · poll_s  监督线程采样间隔。
# 可在 config.json 的 "watchdog" 段覆盖（测试用极小值）。
WD_STEP_S = 25.0
WD_ROUND_S = 90.0
WD_CONFIRM = 3
WD_POLL_S = 1.0


def _same_file(a, b):
    """两个路径是不是同一个文件。Windows 大小写不敏感，samefile 还可能抛。"""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _fire_at(task):
    """任务的 fire_at 当数字用；缺失/脏数据一律当 0（= 手动触发）。"""
    try:
        return float((task or {}).get("fire_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def sync_template(cfg, name, tid):
    """把任务自己的模板同步到全局位置；没有就跳过（引擎直接读全局那份）。

    返回 False = 任务和全局都没有这个模板，调用方该判失败。

    以前这里无条件 `shutil.copyfile(src, dst)`：任务目录里没有模板时会退回
    全局路径，src 和 dst 就成了同一个文件 → SameFileError → 任务启动即死。
    而 /api/scheduler/start 早就回了 ok，界面上看起来就是「点了没反应」。
    实测这台机器上就是这么死的（队列里那条任务的 result 就是 SameFileError）。
    """
    dst = cfg.template_path(name)
    src = cfg.template_path(name, tid)
    if os.path.isfile(src) and not _same_file(src, dst):
        shutil.copyfile(src, dst)
        return True
    return os.path.isfile(dst)


GUARD_BLOCKING = ("halted", "waiting_human")


class Scheduler:
    """任务队列 + 调度器。

    **不缓存配置**：每次读写都从盘上拿（Config.load / Config.transaction）。
    以前这里存了一份 self._cfg 快照，所有改动都在这份快照上做再整文件写回
    —— 服务端保存设置走的是另一份，后写的把先写的整份盖掉，用户看到的就是
    「设置改完几秒后自己变回去」。配置就几百字节，读一次不到 1ms，不值得
    为了省这点开销去冒丢更新的风险。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._stop_guard = threading.RLock()
        self._stopping = False
        self._thread = None
        self._engine = Engine()
        self._log = deque(maxlen=400)
        self._active_id = None
        # 最后一次**读成功的**队列快照。config.json 读不出来时用它顶住 ——
        # 以前读失败会拿到默认空队列，_run_step 立刻判「跑完了」收工。
        self._queue_cache = []
        self._cfg_error = None
        self._cfg_error_said = None
        # 队列硬停锁（审查 B02）：非空 = 已安全停止/需要人处理，下一次派发
        # 前必须先看守卫；只有用户显式「继续」或守卫自行恢复到 ok 才清。
        self._halt_reason = ""
        self._halt_hard = False
        self._halt_said = ""
        # S9：调度级看门狗。心跳由调度线程打点，监督线程只读心跳 + 处置，
        # 这样「调度线程自己卡死」也能被外面看见。
        self._hb = wd_mod.Heartbeat()
        self._wd_step = None
        self._wd_round = None
        self._wd_cfg = {"step_s": WD_STEP_S, "round_s": WD_ROUND_S,
                        "confirm": WD_CONFIRM, "poll_s": WD_POLL_S}
        self._wd_thread = None
        self._wd_cancel = threading.Event()
        self._stall_records = deque(maxlen=20)
        # 监督线程判定「本轮某一步卡死」后按下的标记：告知 `_drive_task`
        # 这一轮是被看门狗打断的，不是普通失败 —— 否则它会当成「未抢到」
        # 收场，重开策略永远走不到。
        self._wd_kill = None
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
        # 先只读判断：没有卡住的任务就**不写盘**。以前这里无条件 save，
        # 结果光 import core.server（构造 Scheduler）就会重写一遍 config.json。
        cfg = Config.load()
        if not getattr(cfg, "load_ok", True):
            # 配置读不出来时手上是默认空队列：既不能据此判定「没有卡住的
            # 任务」，更不能进事务（会覆盖原文件）。跳过本次恢复。
            self._say(f"队列恢复跳过：{getattr(cfg, 'last_error', None) or '配置读不出来'}"
                      "（不写盘，下次启动再试）", "warn")
            return
        stale = [t for t in cfg["queue"]
                 if t.get("status") in (ARMED, RUNNING)]
        if not stale:
            return
        ids = {t.get("id") for t in stale}
        try:
            with self._lock, Config.transaction() as cfg:
                for t in cfg["queue"]:
                    if t.get("id") in ids:
                        t["status"] = PENDING
                        t["note"] = (t.get("note") or "") + "（上次没跑完，已放回队列）"
        except ConfigLoadError as e:
            # 配置这会儿读不出来：宁可不恢复，也不能拿默认值整份覆盖（审查 S1）
            self._say(f"队列恢复跳过：{e}", "warn")
            return
        for t in stale:
            self._say(f"{self._label(t)}：上次没跑完，已放回队列", "warn")

    def gc_task_dirs(self):
        """清掉队列里已经不存在的任务目录。

        只在程序启动时调（server.serve），不放在 __init__ 里：测试会
        临时改 CONFIG_PATH，那时队列是空的，一 init 就会把真实 data/
        目录当成孤儿全删掉。
        """
        try:
            dropped = Config.load().gc_task_dirs(keep=[t.get("id") for t in self.tasks()])
        except Exception:                    # noqa: BLE001
            return 0
        if dropped:
            self._say(f"清掉 {dropped} 个已删除任务留下的目录", "info")
        return dropped

    # ---------------- 队列管理 ----------------

    def _read_queue(self, load=None):
        """读队列，成功时更新快照。返回 `(队列, 读盘是否成功, 错误说明)`。

        读失败时返回**上一次成功读到的**队列（不是空列表）：空列表会被
        `_run_step` 当成「跑完了」，整轮直接收工，而用户的任务其实还排着
        （review R1）。
        """
        try:
            cfg = (load or Config.load)()
        except Exception as e:               # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            self._cfg_error = err
            return [dict(t) for t in self._queue_cache], False, err
        if getattr(cfg, "load_ok", True):
            q = [dict(t) for t in (cfg["queue"] or [])]
            self._queue_cache = q
            self._cfg_error = None
            return q, True, None
        err = getattr(cfg, "last_error", None) or "config.json 读不出来"
        self._cfg_error = err
        return [dict(t) for t in self._queue_cache], False, err

    def _poll_queue(self):
        """界面轮询用的队列：和 `_read_queue` 同契约，但配置走内存快照。

        `Config.transaction()` 落盘时要 fsync，忙盘时能持 `_save_lock`
        130ms+；界面 1s 一次的轮询以前就压在它上面（长时报告里的 185ms
        顿点）。跑单逻辑仍然用 `_read_queue()` —— 那里必须看到最新队列。
        """
        load = getattr(Config, "load_cached", Config.load)
        return self._read_queue(load)

    def tasks(self):
        with self._lock:
            queue, _ok, _err = self._poll_queue()
            return [self._with_route_hint(t) for t in queue]

    @staticmethod
    def _with_route_hint(task):
        """给界面一个「这条任务用的是旧路线」的提示（审查 R3）。

        全局路线切换后，任务自己钉住的路线不会跟着变；静默不一致正是这次
        返工要消掉的东西，所以 API 里显式给一句话，前端直接显示即可。
        """
        if task.get("route_stale"):
            wf = task.get("workflow_effective") or task.get("workflow") or ""
            task["route_hint"] = (f"该任务使用旧路线："
                                  f"{WORKFLOW_CN.get(wf, wf or '未知')}")
        else:
            task.pop("route_hint", None)
        return task

    def tasks_status(self):
        """队列 + 读盘状态。给 /api/tasks 用：界面能看出「配置读失败」。

        只有 `config_ok=False` 时 `tasks` 才是上一次的快照，可能比盘上旧。
        """
        with self._lock:
            queue, ok, err = self._poll_queue()
            return {"tasks": [self._with_route_hint(t) for t in queue],
                    "config_ok": ok, "config_error": err}

    def add(self, name, keywords=None, fire_at=0, note="", regions=None, template_from=None,
            mode=None, min_price=0, max_price=0, max_wear=0,
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
        # 数字独立兜底：0 与空串合法（= 不限），但 Infinity / 负数 / 小数
        # 一律拒绝，绝不静默折成默认值（1e309 在 JSON 里会变成 null，
        # 到了引擎就按 0 = 不限价处理）。
        clean, errs = check_conditions({
            "min_price": min_price, "max_price": max_price,
            "quota": quota, "budget": budget, "max_wear": max_wear,
            "craft": craft})
        if errs:
            return None, "条件不合法：" + "；".join(errs)
        min_price, max_price = clean["min_price"], clean["max_price"]
        quota, budget = clean["quota"], clean["budget"]
        max_wear = clean["max_wear"]
        # **workflow 是唯一真源，mode 由它派生**（审查 S7）。以前两个字段
        # 各自为政：add 里按 mode 判区域、引擎按 workflow 跑，同一个任务
        # 检查的地方和实际跑的地方不是一回事。
        if workflow not in (None, ""):
            wf = normalize_workflow(workflow, "")
            if not wf:
                return None, f"不认识的抢购方式：{workflow}"
        else:
            # 调用方没指定路线时，**唯一真源**是全局默认 workflow；
            # 只有老调用方还在传 mode 时才按它迁移（task_workflow 的 legacy 分支）。
            # 以前这里的入参默认值是 "detail"，和全局默认 workflow 是两个
            # 真源，调用方漏传时结论全看哪一边先说话（review R2）。
            default_wf = normalize_workflow(Config.load().get("workflow"), "countdown")
            wf = task_workflow({"mode": mode}, default_wf)
        mode = mode_for_workflow(wf)
        with self._lock:
            cfg = Config.load()
            regions = regions or {k: v for k, v in cfg["regions"].items() if v}
            if mode == "list":
                # 列表模式必须知道盯哪块屏幕，以及点进去后确认按钮在哪
                if not regions.get("list"):
                    return None, "列表模式要先框选交易行列表区域"
            # 「确认」模板只在**有确认弹窗**的界面才需要。这一版界面点价格
            # 按钮直接成交（实测），没有弹窗可截 —— 硬要求会让任务永远加不上。
            # 引擎那边没有确认区域时改走「点一次 + 看余额」的判定。
            # 入队只拦「区域」，模板与倒计时区域留给 start()/引擎去判：
            # 入队允许先占位，启动时才真正校验（口径见 start）。
            missing = [k for k in ("buy",) if not regions.get(k)]
            if missing:
                return None, "先把购买按钮框选好再添加任务"
            tid = uuid.uuid4().hex[:8]
            task = {
                "id": tid,
                "name": name.strip(),
                "keywords": [k for k in (keywords or []) if k],
                "mode": mode,
                # 走哪条抢购路线（countdown / list / detail）；mode 是它的派生
                "workflow": wf,
                # 路线来源（审查 R3）：没显式选过就跟着全局走，切全局时重新派生
                "workflow_explicit": bool(workflow not in (None, "")),
                "workflow_source": ("task" if workflow not in (None, "") else "global"),
                "workflow_effective": wf,
                "route_stale": False,
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
                "quota": quota,
                "budget": budget,
                # 启用开关：停用的任务留在队列里但不跑（改条件时先停掉）
                "enabled": True,
                # 已抢几件 / 已花多少：抢购是累计的，列表要看得见进度
                "bought": 0,
                "spent": 0.0,
            }
            # 事务：读盘 + 追加 + 落盘 在同一把锁里。不能 cfg.save()
            # 了事 —— 那份 cfg 是上面 load 出来的快照，期间服务端可能刚
            # 存过设置，整份写回会把人家刚存的盖掉。
            with Config.transaction() as cur:
                # 同名 + 同路线只保留一条：update 而不是追加。老版本换目标
                # 时会在队列里堆一串同名旧任务，取消勾选再开始就按旧目标
                # 重跑。
                prior = next((t for t in cur["queue"]
                              if (t.get("name") or "").strip() == task["name"]
                              and t.get("workflow") == wf), None)
                if prior:
                    task["id"] = prior.get("id", task["id"])
                    task["created"] = prior.get("created", task["created"])
                    # 不复活已结束的任务：状态跟着旧任务走，重跑必须走
                    # update 里的显式 pending。
                    task["status"] = prior.get("status", task["status"])
                    for k in ("bought", "spent"):
                        if k in prior:
                            task[k] = prior[k]
                    cur["queue"][cur["queue"].index(prior)] = task
                else:
                    cur["queue"].append(task)
            tid = task["id"]
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
        """删掉一条任务（连同它的模板目录）。

        条件改了就该把旧任务删掉：留着它，调度器会按老条件再跑一遍 ——
        老条件可能是「不限价」，那就是真金白银的事故。

        模板目录一起删：不删的话 data/ 只涨不落（实测堆了 799 个目录、53MB，
        而队列里通常只有 1-2 条）。
        """
        with self._lock, Config.transaction() as cfg:
            before = len(cfg["queue"])
            cfg["queue"] = [t for t in cfg["queue"] if t.get("id") != task_id]
            removed = before - len(cfg["queue"])
        if removed:
            drop_task_dir(task_id)
        return removed

    def clear_finished(self):
        with self._lock, Config.transaction() as cfg:
            gone = [t.get("id") for t in cfg["queue"]
                    if t["status"] in (DONE, FAILED, SKIPPED)]
            cfg["queue"] = [t for t in cfg["queue"]
                            if t["status"] not in (DONE, FAILED, SKIPPED)]
            left = len(cfg["queue"])
        for tid in gone:
            drop_task_dir(tid)
        return left

    def update(self, task_id, **fields):
        """改一条任务的字段。

        正在跑的那条（active_task）必须同步写进 `cfg["active_task"]`：引擎每轮
        reload 都从那儿读条件，只改 `queue` 的话「跑着改价」永远不生效（审查 S4）。
        之前 `_refresh_task` 读到的是旧快照，用户以为勾了、实际按老条件抢。
        """
        if "workflow" in fields:
            wf = normalize_workflow(fields.get("workflow"), "")
            if wf:
                fields["workflow"] = wf
                fields["mode"] = mode_for_workflow(wf)
                # 用户在这条任务上显式改路线 = 钉住，不再跟随全局（审查 R3）
                fields["workflow_explicit"] = True
                fields["workflow_source"] = "task"
                fields["workflow_effective"] = wf
                fields["route_stale"] = False
        with self._lock, Config.transaction() as cfg:
            for t in cfg["queue"]:
                if t["id"] == task_id:
                    t.update(fields)
                    if task_id == self._active_id:
                        cfg["active_task"] = self._engine_task(t)
                    return True
            return False

    # ---------------- 运行 ----------------

    def start(self):
        """启动一轮（只有实战一种路径）。

        存活判断、守卫检查、`self._engine` 替换、建线程**全在同一临界区**
        （审查 R01 #1）：以前 is_alive() 在锁外，6 条线程同时调用会起 6 条
        调度线程、构造 7 个 Engine，只有最后一个可达，旧引擎仍在点鼠标。
        """
        with self._stop_guard:
            if self._thread and self._thread.is_alive():
                if not self._stop.is_set():
                    return False, "调度器已在运行"
                # 上一轮 stop() 的 join 超时了，旧线程可能还在点鼠标。共享一个
                # 鼠标，绝不能让两轮并行 —— 先再收一次，收不干净就拒绝（S14）。
                self.stop()
                if self._thread and self._thread.is_alive():
                    return False, "上一轮还在收尾，稍等两秒再开始"
            # 硬停是持久状态：没有用户显式「继续」，**任何**启动都不能绕过（B02）
            if self._halt_reason:
                return False, (f"{self._halt_reason} —— 先在异常弹层点「继续」，"
                               "再开始")
            try:
                g = self.guard_status().get("guard") or {}
            except Exception:                # noqa: BLE001
                g = {}
            if g.get("state") in GUARD_BLOCKING or g.get("halt_latched"):
                self._set_halt(g.get("message") or "当前处于安全停止状态",
                               hard=bool(g.get("state") == "halted"
                                         or g.get("halt_latched")))
                return False, (g.get("message")
                               or "当前处于安全停止状态，请先点「继续」")
            # 上一轮线程/引擎异常退出会留下 RUNNING 的任务；两边都确认死了
            # 才能复位，否则就是把「还在点的任务」再排一遍（S17）。
            self._recover_orphans()
            with self._lock:
                cfg = Config.load()
                if not getattr(cfg, "load_ok", True):
                    # 读失败时这份 cfg 是默认值：queue 是空的、区域也没了。
                    # 直接报错比「按默认配置点鼠标」安全，也不写盘。
                    return False, (f"{getattr(cfg, 'last_error', None) or '配置读不出来'}"
                                   "，先别开抢（关掉占着 config.json 的程序后重试）")
                if not cfg["queue"]:
                    return False, "队列是空的"
                # 开跑前先把「模板在不在」查一遍。缺模板的话任务会在后台线程里
                # 立刻失败，而接口已经回了「已启动」—— 用户看到的就是点了没反应。
                first = [t for t in cfg["queue"]
                         if t.get("status") in (PENDING, ARMED) and t.get("enabled", True)]
                if not first:
                    # 失败/跳过的任务，再点一次开始就是「重跑」，自动复位；
                    # **已完成（DONE）的绝不能在这里复活**。需要人处理的
                # （needs_human，比如开火阶段被打断）也不许自动重排，否则
                    # 就是重复下单。
                    with Config.transaction() as cur:
                        revived = False
                        for t in cur["queue"]:
                            if (t.get("enabled", True)
                                    and not t.get("needs_human")
                                    and t.get("status") in (FAILED, SKIPPED)):
                                t["status"] = PENDING
                                t["result"] = None
                                revived = True
                    if not revived:
                        return False, ("队列里只剩已完成或需要人工处理的任务 —— "
                                       "要重跑请先在队列页点「重跑」并确认")
                    cfg = Config.load()
                    first = [t for t in cfg["queue"]
                             if t.get("status") in (PENDING, ARMED)
                             and t.get("enabled", True)]
                if first:
                    task = first[0]
                    # 模板要求按 **workflow** 判，和引擎 preflight 完全一致（P0-4）
                    wf = task_workflow(task, cfg.get("workflow") or "countdown")
                    need = {"list": ("buy",),
                            "countdown": ("confirm",),
                            "detail": ("buy", "confirm")}[wf]
                    missing = [n for n in need
                               if not (os.path.isfile(cfg.template_path(n, task.get("id")))
                                       or os.path.isfile(cfg.template_path(n)))]
                    if missing:
                        return False, (f"缺少模板：{'、'.join(missing)}"
                                       "（先在游戏里重新截一次再开始）")
            self._say("执行模式：实战（会真实点击）")
            self._stop.clear()
            self._engine = Engine()
            # 「抢到就停」靠这个回调通知调度器自己停 —— 引擎不认识调度器。
            # 必须投递到新线程：回调发生在引擎线程里，直接 join 自己会炸（S11）。
            self._engine.on_hit = self._hit_stop
            # S9：把看门狗真正接进来。心跳/阈值按这份配置装；监督线程
            # 与调度线程同生共死 —— 调度线程卡住时，只有它还能动。
            self._arm_watchdog(cfg)
            try:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                self._start_watchdog()
            except Exception as e:               # noqa: BLE001
                # 启动失败要回滚：线程没起来却把 _thread 指过去，下一次 start
                # 会永远看到「已在运行」。看门狗起不来同样不能留半套。
                self._stop.set()
                t = self._thread
                if t and t.is_alive() and t is not threading.current_thread():
                    t.join(timeout=1.0)
                self._thread = None
                return False, f"调度线程启动失败：{type(e).__name__}: {e}"
            return True, "调度器已启动"

    def stop(self):
        """停下调度器。**幂等**：连按两下停止不会把状态搞成半截（审查 S26）。

        以前这里没有任何保护：
          · 第二下进来又 join 一次，与第一下抢同一把文件锁；
          · `self._engine.stop()` 在引擎线程里调（on_hit 回调）时会 join 自己，
            RuntimeError 一抛，后面的状态复位整段跳过（审查 S11）。
        """
        with self._stop_guard:
            if self._stopping:
                return True, "正在停止"
            self._stopping = True
        try:
            self._stop.set()
            eng_ok, eng_msg = True, ""
            try:
                res = self._engine.stop()
                if isinstance(res, tuple) and len(res) >= 2:
                    eng_ok, eng_msg = bool(res[0]), str(res[1])
                else:
                    eng_ok = not self._engine_alive()
            except Exception:                # noqa: BLE001
                traceback.print_exc()
                eng_ok, eng_msg = False, "引擎停止时报错"
            t = self._thread
            if t and t.is_alive() and t is not threading.current_thread():
                try:
                    t.join(timeout=3.0)
                except RuntimeError:
                    pass
            # **只有真停了才算停**（审查 R01 #3）：引擎线程还在不可打断的
            # OCR/HTTP 调用里时，返回「已停止」会让上层把任务放回 PENDING、
            # 再开一轮，于是两个引擎同时点同一个鼠标。
            # 退出前必须确认输入已释放（审查 R01 #4）：`_bye` 在 stop() 之后
            # 立刻 os._exit(0)，不给任何守护线程收尾的机会。这里同步做一遍
            # 强制收尾（冻结 + 释放输入 + 还原光标），释放不掉要把结果说清楚。
            rel_ok, rel_msg = True, ""
            try:
                res = self._engine.force_release_input()
                if isinstance(res, tuple) and len(res) >= 2:
                    rel_ok, rel_msg = bool(res[0]), str(res[1])
            except AttributeError:
                rel_ok = True
            except Exception as e:           # noqa: BLE001
                rel_ok, rel_msg = False, f"强制释放输入失败：{type(e).__name__}: {e}"
            if not rel_ok:
                self._set_halt(rel_msg or "输入没能确认释放", hard=True)
                self._say(f"停止：{rel_msg or '输入没能确认释放'}", "error")
                return False, (rel_msg or "输入没能确认释放")
            if not eng_ok or self._engine_alive():
                self._set_halt(eng_msg or "停止请求已发出，但引擎还没退出", hard=True)
                self._say("停止请求已发出，引擎还在收尾（队列已锁住）", "warn")
                return False, "正在停止（引擎还没退出）"
            if t and t.is_alive():
                # join 没等到：线程可能还在点鼠标。不能把状态改回 PENDING。
                self._set_halt("上一轮调度线程还在收尾", hard=True)
                self._say("停止请求已发出，上一轮还在收尾", "warn")
                return False, "正在停止（上一轮还在收尾）"
            with self._lock, Config.transaction() as cfg:
                for t in cfg["queue"]:
                    if t["status"] in (ARMED, RUNNING):
                        t["status"] = PENDING
            return True, "已停止"
        finally:
            self._stop_watchdog()
            with self._stop_guard:
                self._stopping = False

    def _recover_orphans(self):
        """把异常退出留下的 RUNNING 放回队列（两边都死了才敢动）。"""
        if self._thread and self._thread.is_alive():
            return 0
        if self._engine_alive():
            return 0
        try:
            with self._lock, Config.transaction() as cfg:
                fixed = 0
                for t in cfg["queue"]:
                    if t.get("status") == RUNNING:
                        t["status"] = PENDING
                        t["result"] = "上一轮没跑完，已放回队列"
                        fixed += 1
        except Exception:                    # noqa: BLE001
            # 配置读不出来时由 start() 的 load_ok 分支统一报错，这里不抢答
            return 0
        if fixed:
            self._say(f"复位 {fixed} 条上一轮遗留的「运行中」任务", "warn")
        return fixed

    def _engine_alive(self):
        try:
            return bool(self._engine.status().get("running"))
        except Exception:                    # noqa: BLE001
            return False

    def _set_halt(self, reason, hard=False):
        """锁住队列：非空期间 _run_step 不派发任何任务（B02）。"""
        with self._lock:
            self._halt_reason = str(reason or "已安全停止")
            self._halt_hard = bool(hard or self._halt_hard)

    def _clear_halt(self):
        with self._lock:
            self._halt_reason = ""
            self._halt_hard = False
            self._halt_said = ""

    def _hit_stop(self):
        """引擎「抢到就停」回调。

        on_hit 是在**引擎线程**的 finally 里调用的，直接 self.stop() 会让
        引擎 join 自己（RuntimeError），异常一抛，调度器那半段状态复位就
        永远不执行。投递到新线程，两边的 join 都不会撞上自己（审查 S11）。
        """
        threading.Thread(target=self.stop, daemon=True).start()

    def status(self):
        with self._lock:
            tasks, cfg_ok, cfg_err = self._poll_queue()
            active = self._active_id
            log = list(self._log)[-80:]
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "active": active,
            "tasks": tasks,
            # 读盘状态：`config_ok=False` 时界面/接口要显示「配置读不出来」，
            # 别把这份快照当成用户真的把任务删光了（review R1）。
            "config_ok": cfg_ok,
            "config_error": cfg_err,
            "log": log,
            "engine": self._engine.status(),
            "watchdog": {
                "step_s": self._wd_cfg.get("step_s"),
                "round_s": self._wd_cfg.get("round_s"),
                "confirm": self._wd_cfg.get("confirm"),
                "alive": bool(self._wd_thread and self._wd_thread.is_alive()),
                "beat_age_s": self._hb.age("tick"),
                "stalls": list(self._stall_records)[-5:],
            },
            "clock": timesync.info(),
        }

    def guard_status(self):
        """场景守卫的对外快照：级别、一句人话、唯一主按钮、现在在做什么。"""
        try:
            st = self._engine.status()
            return {"guard": st.get("guard"), "doing": st.get("doing"),
                    "engine_state": st.get("state")}
        except Exception as e:                   # noqa: BLE001
            return {"guard": None, "doing": "", "error": str(e)}

    def guard_resume(self):
        """人工段处理完 / 用户点「继续」：清暂停，必要时把队列接着跑起来。

        以前这里只清守卫，调度器若已退出就永远不动 —— 用户点了「继续」，
        界面显示正常，可队列一条都不再派发（审查 B04/M-12）。
        """
        try:
            snap = self._engine.guard_resume()
        except Exception:                        # noqa: BLE001
            snap = {}
        self._clear_halt()
        started = False
        if not (self._thread and self._thread.is_alive()):
            try:
                pending = [t for t in self.tasks()
                           if t.get("status") in (PENDING, ARMED)
                           and t.get("enabled", True)]
            except Exception:                    # noqa: BLE001
                pending = []
            if pending:
                ok, _msg = self.start()
                if ok:
                    started = True
                    self._say("继续：调度器接着跑", "hit")
        out = dict(snap or {})
        out["resumed"] = True
        out["queue_running"] = started
        return out

    def guard_stop(self):
        """安全停止：冻结、还原光标、写记录、退出，并且锁住整条队列。"""
        self._set_halt("已安全停止", hard=True)
        try:
            snap = self._engine.guard_stop()
        except Exception:                        # noqa: BLE001
            snap = {}
        self._set_halt((snap or {}).get("message") or "已安全停止", hard=True)
        self._say("安全停止：队列已锁住，等你核对后再点「继续」", "warn")
        return snap

    def busy(self):
        """现在是不是正在跑一轮抢购。

        OCR 服务管家靠它决定「现在能不能重启」—— 蓝绿虽然不断流，但新实例
        加载模型要占显存和带宽，正在抢的那一轮里没必要折腾。
        """
        return self._engine_alive()

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
        with self._lock, Config.transaction() as cfg:
            for t in cfg["queue"]:
                if t["id"] == task_id:
                    t["status"] = status
                    if result is not None:
                        t["result"] = result
                    break

    def _settle_task(self, task_id, reason):
        """把一条没跑成的任务落成 FAILED，返回是否写盘成功（审查 F4）。

        写不进去时不能假装成功：调用方会据此锁队列，并在下一次启动时
        由 `_recover_orphans` 把 RUNNING 回滚成可重试的 PENDING。
        """
        try:
            self._set_status(task_id, FAILED, reason)
            return True
        except Exception as e:                   # noqa: BLE001
            self._say(f"任务收场写盘失败：{type(e).__name__}: {e}", "error")
            return False

    # ---------------------------------------------------------------- S9 看门狗

    def _arm_watchdog(self, cfg):
        """按配置装心跳阈值（测试会传极小的值来实测触发时延）。"""
        try:
            w = cfg.get("watchdog") or {}
        except Exception:                        # noqa: BLE001
            w = {}

        def num(key, default):
            try:
                return max(0.02, float(w.get(key, default)))
            except (TypeError, ValueError):
                return default

        step_s = num("step_s", WD_STEP_S)
        round_s = num("round_s", WD_ROUND_S)
        confirm = max(1, int(num("confirm", WD_CONFIRM)))
        self._wd_cfg = {"step_s": step_s, "round_s": round_s,
                        "confirm": confirm, "poll_s": num("poll_s", WD_POLL_S)}
        self._hb.reset()
        self._wd_step = wd_mod.Watchdog(self._hb, limit_s=step_s,
                                        confirm_times=confirm,
                                        cooldown_s=max(step_s, 1.0))
        self._wd_round = wd_mod.Watchdog(self._hb, limit_s=round_s,
                                         confirm_times=confirm,
                                         cooldown_s=max(round_s, 1.0))

    def _start_watchdog(self):
        self._wd_cancel.clear()
        t = threading.Thread(target=self._watchdog_loop, daemon=True,
                             name="dafors-watchdog")
        t.start()
        self._wd_thread = t

    def _stop_watchdog(self):
        self._wd_cancel.set()
        t = self._wd_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._wd_thread = None

    def _watchdog_loop(self):
        """监督线程：只读心跳、只处置，不参与正常调度。

        这样即使调度线程自己卡在一次同步调用里（写盘、engine.start），
        它也有机会发现并安全停机。
        """
        while not self._wd_cancel.is_set() and not self._stop.is_set():
            try:
                self._watchdog_poll_once()
            except Exception as e:               # noqa: BLE001
                self._say(f"看门狗自身异常：{type(e).__name__}: {e}", "error")
            self._wd_cancel.wait(self._wd_cfg.get("poll_s", WD_POLL_S))

    def _watchdog_poll_once(self):
        if self._halt_reason or not self._wd_round:
            return None
        # 整轮卡住优先：调度线程自己不动了，引擎心跳再新也没人推进队列。
        if self._wd_round.poll(("tick",)):
            return self._on_round_stall()
        st = {}
        try:
            st = self._engine.status() or {}
        except Exception:                        # noqa: BLE001
            return None
        if not st.get("running"):
            return None
        age = st.get("beat_age_s")
        if age is None:
            return None
        if self._wd_step.poll(("tick",), age_override=age):
            return self._on_step_stall(st, float(age))
        return None

    def _record_stall(self, kind, age, stage, action):
        self._stall_records.append({"at": time.time(), "kind": kind,
                                    "age_s": round(float(age), 3),
                                    "stage": stage, "action": action,
                                    "task": self._active_id})

    def _on_round_stall(self):
        """整轮卡住：调度线程没有推进 —— 一律安全停机，绝不自动重跑。

        无法确认它卡在哪一步，也就无法排除「已经点过确认」，所以保守处理：
        锁队列 + 当前任务标人工。
        """
        age = self._hb.age("tick")
        reason = (f"整轮卡住：调度心跳 {age:.1f}s 没有推进"
                  f"（阈值 {self._wd_cfg['round_s']:.1f}s），"
                  "已安全停机")
        self._set_halt(reason, hard=True)
        try:
            self._engine.guard_stop()
        except Exception:                    # noqa: BLE001
            try:
                self._engine.stop(timeout=2.0)
            except Exception:                # noqa: BLE001
                pass
        if self._active_id:
            try:
                self._needs_human(self._active_id, reason)
            except Exception:                # noqa: BLE001
                pass
        self._say(reason, "error")
        self._record_stall("round", age or 0.0, "unknown", "halt")
        return "halt"

    def _on_step_stall(self, st, age):
        """某一步卡死：先看阶段，fire/confirm 之后一律不自动重跑。"""
        stage = st.get("stage_before_stop") or st.get("state") or "unknown"
        if stage in ("fire", "confirm"):
            reason = (f"看门狗：某一步卡死 {age:.1f}s 且已在 {stage} 阶段，"
                      "不自动重跑")
            # 告诉 `_drive_task`：这轮是看门狗按下的，而且不许重开。
            self._wd_kill = {"stage": stage, "retry": False, "age": age}
            if self._active_id:
                try:
                    self._needs_human(self._active_id, reason)
                except Exception:            # noqa: BLE001
                    pass
            self._set_halt(reason, hard=True)
            try:
                self._engine.stop(timeout=2.0)
            except Exception:                # noqa: BLE001
                pass
            self._say(reason, "error")
            self._record_stall("step", age, stage, "no_retry")
            return "no_retry"
        # 开火之前：停掉这一轮，交给 `_drive_task` 既有的「重开一轮」策略，
        # 不在这里改写任务状态，避免与调度线程抢同一份配置。
        self._say(f"看门狗：某一步卡死 {age:.1f}s（{stage} 阶段），"
                  "停掉本轮交给既有重开策略", "warn")
        self._wd_kill = {"stage": stage, "retry": True, "age": age}
        ok, msg = True, ""
        try:
            res = self._engine.stop(timeout=2.0)
            if isinstance(res, tuple):
                ok, msg = bool(res[0]), str(res[1])
        except Exception as e:               # noqa: BLE001
            ok, msg = False, f"{type(e).__name__}: {e}"
        if not ok:
            reason = (f"看门狗：某一步卡死且引擎停不下来（{msg}），已锁队列")
            self._wd_kill["retry"] = False
            self._set_halt(reason, hard=True)
            self._say(reason, "error")
            self._record_stall("step", age, stage, "halt")
            return "halt"
        self._record_stall("step", age, stage, "retry")
        return "retry"

    def _idle(self, seconds):
        """调度线程的等待也要打心跳 —— 「在等」不等于「卡死」。"""
        end = time.monotonic() + float(seconds)
        while not self._stop.is_set():
            self._hb.beat("tick")
            left = end - time.monotonic()
            if left <= 0:
                break
            self._stop.wait(min(left, 0.2))

    def _run(self):
        try:
            while not self._stop.is_set():
                # S9：调度线程每轮打一次心跳；卡在同步调用里心跳就停，
                # 监督线程据此判定「整轮卡住」。
                self._hb.beat("tick")
                try:
                    if not self._run_step():
                        break
                except Exception as e:               # noqa: BLE001
                    # 调度器**自身**的异常（配置读失败、排序、校时）以前会一路
                    # 冒出 while、线程直接结束，剩下的任务永远停在「等待」。
                    # 现在记一笔、歇 0.5s 继续（审查 S25）。
                    self._say(f"调度器异常：{type(e).__name__}: {e}", "error")
                    for line in traceback.format_exc().splitlines()[-4:]:
                        self._say(line, "error")
                    if self._stop.wait(0.5):
                        break
        finally:
            self._stop_watchdog()
            with self._lock:
                # 只清本线程那一轮的 active：上一轮残留线程不能在下一轮已开始
                # 之后把 _active_id 抹掉，否则新任务的「跑着改价」全部失效（R01）。
                if threading.current_thread() is self._thread:
                    self._active_id = None

    def _run_step(self):
        """挑一条任务、布防、执行。返回 False = 队列空了，可以收工。"""
        # 派发任何任务之前先看守卫（审查 B02）：硬停/人工等待必须锁住整条
        # 队列。守卫自行回到 ok（目标达成后 auto_resume）时自动续跑；硬停
        # 只能由用户点「继续」解除。
        try:
            g = self.guard_status().get("guard") or {}
        except Exception:                    # noqa: BLE001
            g = {}
        if (g.get("state") in GUARD_BLOCKING or g.get("halt_latched")) \
                and not self._halt_reason:
            self._set_halt(g.get("message") or "当前处于安全停止状态",
                           hard=bool(g.get("state") == "halted"
                                     or g.get("halt_latched")))
        if self._halt_reason:
            if not self._halt_hard and g.get("state") not in GUARD_BLOCKING:
                self._clear_halt()
                self._say("守卫已自行恢复（目标已达成），队列自动续跑", "hit")
            else:
                if self._halt_reason != self._halt_said:
                    self._say(f"队列已暂停：{self._halt_reason}", "warn")
                    self._halt_said = self._halt_reason
                self._idle(0.2)
                return True
        # 先看读盘状态：配置读不出来时**不能**推进状态机 ——
        # 以前这里直接拿默认空队列判「队列跑完了」，盘上的任务还在排着，
        # 调度线程却收工了（review R1）。保持队列、记录 error、等下一轮。
        queue, cfg_ok, cfg_err = self._read_queue()
        if not cfg_ok:
            if cfg_err != self._cfg_error_said:
                self._say(f"配置读不出来：{cfg_err}；本轮不推进，"
                          f"保持队列 {len(queue)} 条", "error")
                self._cfg_error_said = cfg_err
            self._idle(0.5)
            return True
        self._cfg_error_said = None
        pending = [t for t in queue
                   if t.get("status") in (PENDING, ARMED) and t.get("enabled", True)]
        if not pending:
            self._say("队列跑完了")
            return False
        # 手动触发的（fire_at=0）就是"现在要抢"，排在所有定时任务前面；
        # 否则排一条一小时后开卖的任务，手动那条会白等一小时。
        # 排序用 `_fire_at()`：老任务可能根本没有 fire_at 字段，以前直接
        # `t["fire_at"]` 是 KeyError，一条脏数据就把调度线程打死（审查 S25）。
        pending.sort(key=lambda t: (1, _fire_at(t)) if _fire_at(t) else (0, 0))
        task = pending[0]
        fire_at = _fire_at(task)

        if fire_at:
            now = timesync.true_now()
            if task.get("status") != ARMED:
                if now < fire_at - ARM_LEAD:
                    wait = min(fire_at - ARM_LEAD - now, 30.0)
                    self._say(f"下一条：{self._label(task)}，"
                              f"{fire_at - now:.0f}s 后进入布防")
                    self._idle(wait)
                    return True
                self._set_status(task["id"], ARMED)
                self._say(f"进入布防：{self._label(task)}，"
                          f"{fire_at - now:.1f}s 后开卖", "hit")

        # 布防即开跑：引擎一启动就在盯按钮，先确认占位按钮在（布防），
        # 再等它变化。不能等开卖那一刻才启动——那时按钮已经变了，
        # 引擎永远看不到"变化"这个事件。
        #
        # 一条任务炸了不能带走整条队列：以前异常会一路冒到 _run 的
        # except，线程直接结束，剩下的任务永远停在「等待」，界面上
        # 只看到调度器停了、日志里一段 traceback。现在这条标失败，
        # 接着跑下一条。
        try:
            self._run_task(task)
        except Exception as e:                   # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
            if not self._settle_task(task["id"], reason):
                self._set_halt(f"任务失败且状态写不进去（{reason}）；"
                               "重启后会自动回滚为等待", hard=True)
            self._say(f"{self._label(task)}：执行出错 "
                      f"{reason}，跳过它继续", "error")
            for line in traceback.format_exc().splitlines()[-4:]:
                self._say(line, "error")
        return True

    @staticmethod
    def _engine_reason(st):
        """引擎日志里最后一条 warn/error，当作失败原因。

        引擎 abort 时只把 Aborted 的文案写进自己的日志（`_error` 只有
        鼠标被抢/窗口不在前台这类才填），所以调度器这边要主动去捞，
        否则清单页永远只显示「未抢到」。
        """
        for e in reversed(st.get("log") or []):
            if e.get("level") in ("warn", "error") and e.get("msg"):
                return str(e["msg"])[:120]
        return ""

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

    def _run_task(self, task):
        tid = task["id"]
        # workflow 是真源，mode 由它派生（审查 S7）：老任务只有 mode 时按它迁移
        workflow = task_workflow(task, "countdown")
        mode = mode_for_workflow(workflow)
        self._active_id = tid
        # 状态置 RUNNING、清人工标记、借用配置**全在同一个保护段里**（审查 F4）：
        # 以前借调配置在 try 之外，落盘一失败异常直接冒出去，任务永远停在
        # RUNNING；而补救的 `_set_status(FAILED)` 同样要写盘，写不进去就没人
        # 收场。现在借调失败一定走到收场分支，绝不留在 RUNNING。
        try:
            self._set_status(tid, RUNNING)
            if task.get("needs_human"):
                # 用户显式把这条重排进队列了：人工确认已经发生，清掉标记
                self.update(tid, needs_human=False)
            self._say(f"开始执行：{self._label(task)}"
                      f"{'（' + WORKFLOW_CN.get(workflow, workflow) + '）'}", "hit")

        # 借全局配置：把任务的区域和筛选条件套进去，引擎只认配置。
        # 这是**临时借用**，跑完必须还（见 _return_config）—— 以前不还，
        # 用户的全局框选会被某一条任务永久改写，下一个任务和手动定位
        # 都按错的区域去点。
            saved = self._borrow_config(task)
        except Exception as e:                   # noqa: BLE001
            reason = f"借用配置失败：{type(e).__name__}: {e}"
            self._say(f"{self._label(task)}：{reason}，任务未开始执行", "error")
            if not self._settle_task(tid, reason):
                # 连状态都写不进去（盘满/只读）：锁队列，重启时
                # `_recover_orphans` 会把 RUNNING 回滚成可重试的 PENDING。
                self._set_halt(reason + "；配置写不进去，重启后该任务会"
                                       "自动回滚为等待", hard=True)
            with self._lock:
                if self._active_id == tid:
                    self._active_id = None
            return
        try:
            for attempt in range(0, WATCHDOG_MAX_RESTARTS + 1):
                outcome = self._drive_task(task, mode, workflow)
                if outcome != "stalled" or self._stop.is_set():
                    break
                if attempt >= WATCHDOG_MAX_RESTARTS:
                    # 重开次数用完：停在失败，不再自动重试。
                    self._set_status(tid, FAILED,
                                     f"看门狗连续 {WATCHDOG_MAX_RESTARTS} 次判定卡死，"
                                     "停止重试（请看日志）")
                    self._say(f"{self._label(task)}：连续卡死，停止重试", "error")
                    break
                # 只有「还在盯、还没点」的卡死才自动重开。重开前把状态
                # 复位成 RUNNING，界面上看到的是同一任务在重试而不是新任务。
                # 真正拉起引擎由下一轮 `_drive_task` 完成 —— 这里再调一次
                # `engine.start()` 会和它撞车（第二次必然「已经在运行了」）。
                self._say(f"{self._label(task)}：第 {attempt + 1} 次自动重开"
                          f"（看门狗判定卡死）", "warn")
                self._set_status(tid, RUNNING)
        finally:
            # 引擎线程没退出就**不还原**区域（审查 B10）：旧线程还会读全局
            # 区域去点按钮，还原得越早，它越会按上一条任务/手动定位的坐标点。
            alive = True
            try:
                alive = bool(self._engine.wait_stop(3.0))
            except Exception:                    # noqa: BLE001
                alive = True
            if alive:
                self._return_config(saved)
            else:
                self._set_halt("引擎线程还没退出，本轮配置先不还原", hard=True)
                self._say(f"{self._label(task)}：引擎线程仍在退出中，"
                          "配置不还原，队列已锁住", "error")

    def _borrow_config(self, task):
        """把任务的区域 / 筛选条件临时写进全局配置，返回还原用的快照。

        只记**真正被覆盖的那几个键**：任务没动的区域（比如用户跑到一半
        重新框选的 confirm）不该被还原操作抹掉。
        """
        with Config.transaction() as cfg:
            borrowed = {}
            for k, v in (task.get("regions") or {}).items():
                if k in cfg["regions"]:
                    borrowed[k] = cfg["regions"].get(k)
                    cfg.set_region(k, v)
            saved = {"regions": borrowed,
                     "active_task": json.loads(json.dumps(cfg.get("active_task") or {}))}
            # 模板也要记快照：`_drive_task` 会把任务模板拷到全局位置，跑完
            # 不还原就会永久留在盘上 —— 下一条没有模板的任务照样过 preflight、
            # 拿**上一条的模板**去点（审查 B09「模板只进不出」）。
            templates = {}
            for name in ("buy", "confirm"):
                path = cfg.template_path(name)
                try:
                    if os.path.isfile(path):
                        with open(path, "rb") as f:
                            templates[name] = f.read()
                    else:
                        templates[name] = None
                except OSError:
                    pass
            saved["templates"] = templates
            cfg["active_task"] = self._engine_task(task)
        return saved

    def _return_config(self, saved):
        """把 _borrow_config 借走的东西还回去。"""
        if not saved:
            return
        try:
            with Config.transaction() as cfg:
                for k, v in (saved.get("regions") or {}).items():
                    if k in cfg["regions"]:
                        cfg.set_region(k, v)
                cfg["active_task"] = saved.get("active_task") or {}
                # 模板：原本有就写回原字节，原本没有就删掉任务刚拷进去的那份
                for name, data in (saved.get("templates") or {}).items():
                    path = cfg.template_path(name)
                    try:
                        if data is None:
                            if os.path.isfile(path):
                                os.remove(path)
                        else:
                            cur = None
                            if os.path.isfile(path):
                                with open(path, "rb") as f:
                                    cur = f.read()
                            if cur != data:
                                with open(path, "wb") as f:
                                    f.write(data)
                    except OSError:
                        pass
        except Exception:                    # noqa: BLE001
            pass

    def _drive_task(self, task, mode, workflow):
        tid = task["id"]
        # 每一轮开始前清掉上一轮的看门狗标记（S9）：否则上一轮的 kill
        # 会被这一轮当成「自己又卡死了」，重开次数白烧。
        self._wd_kill = None
        cfg = Config.load()

        # 任务自己的模板同步到全局位置，引擎从那儿读。
        # 口径与引擎 `_validate` / preflight 一致（审查 P0-4）：
        #   list      → 只要 buy（无弹窗版式，不要求确认）
        #   countdown → 只要 confirm（归零直接点购买中心，不认按钮长相）
        #   detail    → buy + confirm
        need = {"list": ("buy",),
                "countdown": ("confirm",),
                "detail": ("buy", "confirm")}.get(workflow, ("buy", "confirm"))
        for name in need:
            if not sync_template(cfg, name, tid):
                self._set_status(tid, FAILED, f"缺少 {name} 模板")
                self._say(f"{self._label(task)}：缺少 {name} 模板，重新截一次", "error")
                return

        ok, msg = self._engine.start(deadline=_fire_at(task) or None,
                                     mode=mode, workflow=workflow)
        if not ok:
            self._set_status(tid, FAILED, msg)
            self._say(f"{self._label(task)}：{msg}", "error")
            return

        t0 = time.time()
        stalled = False
        while not self._stop.is_set():
            st = self._engine.status() or {}
            if st.get("state") in ("done", "abort"):
                break
            if not st.get("running"):
                # 引擎线程已经结束，状态却不是终态（引擎内部出错、或者状态
                # 没落到 done/abort）。这时再等下去就是死等 —— 整条队列卡死，
                # 后面的任务永远排不上。
                break
            age = st.get("beat_age_s")
            if age is not None and age >= WATCHDOG_STALL_S:
                # 线程还在（running=True），但心跳停了 —— 这就是「卡在某个
                # 同步调用里」的典型样子。stop() 只能尽力收尾，真正恢复靠
                # 上层重开一轮。
                self._say(f"看门狗：{self._label(task)} 已 {age:.0f}s "
                          f"没有心跳，判定卡死", "error")
                stalled = True
                self._engine.stop()
                break
            # 盯的这一轮也要打调度心跳：引擎忙、调度线程活着 = 单步问题；
            # 两边都不跳才是整轮卡住（S9）。
            self._hb.beat("tick")
            time.sleep(0.05)

        st = self._engine.status() or {}
        pre_state = st.get("state")
        try:
            self._engine.stop()
        except Exception:                    # noqa: BLE001
            pass
        st = self._engine.status() or {}
        # B01 的根因：stop() 会把 state 改成 abort，之后再读永远看不到
        # fire/confirm。真实阶段必须在停止**之前**记下来 —— 引擎现在把
        # 它写在 stage_before_stop 里，这里优先用它。
        stage_before = st.get("stage_before_stop") or pre_state
        kill = self._wd_kill
        if kill:
            # 监督线程判定的阶段比 stop() 之后读到的更准（它是在卡死当时读的）
            stage_before = kill.get("stage") or stage_before
        elapsed = round(time.time() - t0, 2)

        # 这一轮抢到几件、花了多少，累计回任务上 —— 列表的「已抢」就靠它
        stats = st.get("stats") or {}
        got = int(stats.get("bought") or 0)
        spent = float(stats.get("spent") or 0)
        if got or spent:
            self.update(tid, bought=int(task.get("bought") or 0) + got,
                        spent=round(float(task.get("spent") or 0) + spent, 1))

        if st.get("state") == "done":
            res = f"完成，{int(stats.get('clicks') or 0)} 次点击，{elapsed}s"
            self._set_status(tid, DONE, res)
            self._say(f"{self._label(task)}：{res}", "hit")
            return "ok"

        # —— 守卫硬停/人工等待：落到任务状态，并且锁住整条队列（B02）
        g = {}
        try:
            g = self._engine.guard_snapshot() or {}
        except Exception:                    # noqa: BLE001
            g = st.get("guard") or {}
        gstate = g.get("state")
        if gstate in ("halted", "waiting_human"):
            if stage_before in ("fire", "confirm"):
                msg = (f"停在{stage_before}阶段（可能已下单），需要人工核对，"
                       "不自动重跑")
                self._needs_human(tid, msg)
                self._set_halt(msg, hard=True)
            elif gstate == "halted":
                msg = g.get("message") or "已安全停止，需要人工处理"
                self._needs_human(tid, msg)
                self._set_halt(msg, hard=True)
            else:
                # 人工等待还没进开火：任务放回等待，用户点「继续」后继续跑
                self._set_status(tid, PENDING,
                                 "需要你处理：处理完点「继续」自动接着跑")
                self._set_halt(g.get("message") or "等待人工处理", hard=False)
            self._say(f"{self._label(task)}：{self._halt_reason}", "warn")
            return "halted"

        if stalled or kill:
            # 卡死在「已经在点购买/确认」阶段时**绝不自动重跑**：可能已经
            # 下过单，重跑会把同一件再买一次。宁可停在失败等人看一眼。
            no_retry = ((kill and not kill.get("retry"))
                        or stage_before in ("fire", "confirm"))
            if no_retry:
                msg = (f"看门狗：卡死在{stage_before or '未知'}阶段"
                       f"（{float((kill or {}).get('age') or 0.0):.1f}s 无心跳），"
                       "可能已下单，不自动重跑")
                self._needs_human(tid, msg)
                self._set_halt(msg, hard=True)
                self._say(f"{self._label(task)}：{msg}", "error")
                return "stalled_no_retry"
            self._set_status(tid, FAILED, "看门狗：心跳停了（卡死），准备重开一轮")
            self._say(f"{self._label(task)}：卡死，收尾后重开一轮", "warn")
            return "stalled"
        if self._stop.is_set():
            self._set_status(tid, PENDING, "被停止")
            self._say(f"{self._label(task)}：被停止", "warn")
        else:
            # 失败原因要能看懂。以前只写「未抢到」，可真正的原因（比如
            # 「公示期里没有符合条件、还在倒计时的商品」「界面导航失败」）
            # 只在引擎日志里，用户盯着清单页的「失败」两字完全不知道为什么。
            res = st.get("error") or self._engine_reason(st) or "未抢到"
            self._set_status(tid, FAILED, res)
            self._say(f"{self._label(task)}：{res}", "warn")
        return "ok"

    def _needs_human(self, task_id, msg):
        """落成「需要人处理」：FAILED 可见，并且带 needs_human 不许自动重排。"""
        self._set_status(task_id, FAILED, msg)
        try:
            self.update(task_id, needs_human=True)
        except Exception:                    # noqa: BLE001
            pass
