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
from .config import (Config, ConfigLoadError, drop_task_dir, mode_for_workflow,
                     normalize_workflow, task_workflow)
from .engine import Engine

PENDING, ARMED, RUNNING, DONE, FAILED, SKIPPED = (
    "pending", "armed", "running", "done", "failed", "skipped")

# 抢购路线的中文名（日志里给用户看的）
WORKFLOW_CN = {"countdown": "公示期倒计时", "list": "在售列表", "detail": "商品页按钮"}

ARM_LEAD = 20.0      # 提前 20 秒装载模板、进入布防
POLL_IDLE = 0.25


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
        stale = [t for t in Config.load()["queue"]
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

    def tasks(self):
        with self._lock:
            return [dict(t) for t in Config.load()["queue"]]

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
        # **workflow 是唯一真源，mode 由它派生**（审查 S7）。以前两个字段
        # 各自为政：add 里按 mode 判区域、引擎按 workflow 跑，同一个任务
        # 检查的地方和实际跑的地方不是一回事。
        if workflow not in (None, ""):
            wf = normalize_workflow(workflow, "")
            if not wf:
                return None, f"不认识的抢购方式：{workflow}"
        else:
            # 调用方没指定时：全局默认；再退一步才看旧 mode 参数
            wf = normalize_workflow(Config.load().get("workflow"),
                                    "list" if mode == "list" else "countdown")
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
            # 事务：读盘 + 追加 + 落盘 在同一把锁里。不能 cfg.save()
            # 了事 —— 那份 cfg 是上面 load 出来的快照，期间服务端可能刚
            # 存过设置，整份写回会把人家刚存的盖掉。
            with Config.transaction() as cur:
                cur["queue"].append(task)
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
        """
        with self._lock, Config.transaction() as cfg:
            before = len(cfg["queue"])
            cfg["queue"] = [t for t in cfg["queue"] if t.get("id") != task_id]
            removed = before - len(cfg["queue"])
        if removed:
            self._drop_task_dir(task_id)
        return removed

    @staticmethod
    def _drop_task_dir(task_id):
        """删任务时把它的模板目录一起删掉。

        不删的话 data/ 只涨不落：实测堆了 799 个目录、53MB，而队列里
        通常只有 1-2 条。
        """
        drop_task_dir(task_id)

    def clear_finished(self):
        with self._lock, Config.transaction() as cfg:
            gone = [t.get("id") for t in cfg["queue"]
                    if t["status"] in (DONE, FAILED, SKIPPED)]
            cfg["queue"] = [t for t in cfg["queue"]
                            if t["status"] not in (DONE, FAILED, SKIPPED)]
            left = len(cfg["queue"])
        for tid in gone:
            self._drop_task_dir(tid)
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
        with self._lock, Config.transaction() as cfg:
            for t in cfg["queue"]:
                if t["id"] == task_id:
                    t.update(fields)
                    if task_id == self._active_id:
                        cfg["active_task"] = self._engine_task(t)
                    return True
            return False

    # ---------------- 运行 ----------------

    def start(self, dry_run=True):
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return False, "调度器已在运行"
            # 上一轮 stop() 的 join 超时了，旧线程可能还在点鼠标。共享一个
            # 鼠标，绝不能让两轮并行 —— 先再收一次，收不干净就拒绝（审查 S14）。
            # 以前这里直接放行新一轮，旧线程还在点，点击就叠在一起了。
            self.stop()
            if self._thread and self._thread.is_alive():
                return False, "上一轮还在收尾，稍等两秒再开始"
        with self._lock:
            cfg = Config.load()
            if not cfg["queue"]:
                return False, "队列是空的"
            # 开跑前先把「模板在不在」查一遍。缺模板的话任务会在后台线程里
            # 立刻失败，而接口已经回了「已启动」—— 用户看到的就是点了没反应。
            first = [t for t in cfg["queue"]
                     if t.get("status") in (PENDING, ARMED) and t.get("enabled", True)]
            if not first:
                # 队列里只剩「完成 / 失败 / 跳过」的：用户又点了一次开始，
                # 意思就是「再跑一遍」。不重置的话下面 _run 会立刻说
                # 「队列跑完了」，界面上还是没反应（实测这台机器上就是这条：
                # 上一次因为 SameFileError 失败，之后每次点开始都毫无动静）。
                with Config.transaction() as cur:
                    for t in cur["queue"]:
                        if (t.get("enabled", True)
                                and t.get("status") in (DONE, FAILED, SKIPPED)):
                            t["status"] = PENDING
                            t["result"] = None
                cfg = Config.load()
                first = [t for t in cfg["queue"]
                         if t.get("status") in (PENDING, ARMED)
                         and t.get("enabled", True)]
            if first:
                task = first[0]
                # 模板要求按 **workflow** 判，和引擎 preflight 完全一致（审查 P0-4）：
                #   list      → 只要 buy 模板（无弹窗版式，不要求确认）
                #   countdown → 只要 confirm 模板（归零直接点购买，不认按钮长相）
                #   detail    → buy + confirm 都要
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
        # 执行模式是全局开关：dry 一定不点，semi 走到购买键前停手，
        # auto 才真点。界面上的「演练模式」也是这条。
        from . import switches
        mode = switches.get(cfg, "run.mode")
        if mode == "dry":
            dry_run = True
        self._stop.clear()
        self._engine = Engine()
        # 「抢到就停」靠这个回调通知调度器自己停 —— 引擎不认识调度器。
        # 必须投递到新线程：回调发生在引擎线程里，直接 join 自己会炸（审查 S11）。
        self._engine.on_hit = self._hit_stop
        self._thread = threading.Thread(target=self._run, args=(dry_run,), daemon=True)
        self._thread.start()
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
            try:
                self._engine.stop()
            except Exception:                # noqa: BLE001
                traceback.print_exc()
            t = self._thread
            if t and t.is_alive() and t is not threading.current_thread():
                try:
                    t.join(timeout=3.0)
                except RuntimeError:
                    pass
            if t and t.is_alive():
                # join 没等到：线程可能还在点鼠标。这时候**不能**把状态改回
                # PENDING，否则下一轮 start() 会以为队列空闲、和旧线程并行
                # 抢同一个鼠标（审查 S14）。
                self._say("停止请求已发出，上一轮还在收尾", "warn")
                return True, "正在停止（上一轮还在收尾）"
            with self._lock, Config.transaction() as cfg:
                for t in cfg["queue"]:
                    if t["status"] in (ARMED, RUNNING):
                        t["status"] = PENDING
            return True, "已停止"
        finally:
            with self._stop_guard:
                self._stopping = False

    def _hit_stop(self):
        """引擎「抢到就停」回调。

        on_hit 是在**引擎线程**的 finally 里调用的，直接 self.stop() 会让
        引擎 join 自己（RuntimeError），异常一抛，调度器那半段状态复位就
        永远不执行。投递到新线程，两边的 join 都不会撞上自己（审查 S11）。
        """
        threading.Thread(target=self.stop, daemon=True).start()

    def status(self):
        with self._lock:
            tasks = [dict(t) for t in Config.load()["queue"]]
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
        with self._lock, Config.transaction() as cfg:
            for t in cfg["queue"]:
                if t["id"] == task_id:
                    t["status"] = status
                    if result is not None:
                        t["result"] = result
                    break

    def _run(self, dry_run):
        try:
            while not self._stop.is_set():
                try:
                    if not self._run_step(dry_run):
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
            with self._lock:
                self._active_id = None

    def _run_step(self, dry_run):
        """挑一条任务、布防、执行。返回 False = 队列空了，可以收工。"""
        pending = [t for t in self.tasks()
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
                    self._stop.wait(wait)
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
            self._run_task(task, dry_run)
        except Exception as e:                   # noqa: BLE001
            self._set_status(task["id"], FAILED,
                             f"{type(e).__name__}: {e}")
            self._say(f"{self._label(task)}：执行出错 "
                      f"{type(e).__name__}: {e}，跳过它继续", "error")
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

    def _run_task(self, task, dry_run):
        tid = task["id"]
        # workflow 是真源，mode 由它派生（审查 S7）：老任务只有 mode 时按它迁移
        workflow = task_workflow(task, "countdown")
        mode = mode_for_workflow(workflow)
        self._active_id = tid
        self._set_status(tid, RUNNING)
        self._say(f"开始执行：{self._label(task)}"
                  f"{'（' + WORKFLOW_CN.get(workflow, workflow) + '）'}", "hit")

        # 借全局配置：把任务的区域和筛选条件套进去，引擎只认配置。
        # 这是**临时借用**，跑完必须还（见 _return_config）—— 以前不还，
        # 用户的全局框选会被某一条任务永久改写，下一个任务和手动定位
        # 都按错的区域去点。
        saved = self._borrow_config(task)
        try:
            self._drive_task(task, dry_run, mode, workflow)
        finally:
            self._return_config(saved)

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
        except Exception:                    # noqa: BLE001
            pass

    def _drive_task(self, task, dry_run, mode, workflow):
        tid = task["id"]
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
        if workflow == "list":
            # 列表模式点进详情页后才用购买模板：有就带上，没有也不拦
            sync_template(cfg, "buy", tid)

        ok, msg = self._engine.start(dry_run=dry_run, deadline=_fire_at(task) or None,
                                     mode=mode, workflow=workflow)
        if not ok:
            self._set_status(tid, FAILED, msg)
            self._say(f"{self._label(task)}：{msg}", "error")
            return

        t0 = time.time()
        while not self._stop.is_set():
            st = self._engine.status() or {}
            if st.get("state") in ("done", "abort"):
                break
            if not st.get("running"):
                # 引擎线程已经结束，状态却不是终态（引擎内部出错、或者状态
                # 没落到 done/abort）。这时再等下去就是死等 —— 整条队列卡死，
                # 后面的任务永远排不上。
                break
            time.sleep(0.05)

        st = self._engine.status() or {}
        self._engine.stop()
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
        elif self._stop.is_set():
            self._set_status(tid, PENDING, "被停止")
            self._say(f"{self._label(task)}：被停止", "warn")
        else:
            # 失败原因要能看懂。以前只写「未抢到」，可真正的原因（比如
            # 「公示期里没有符合条件、还在倒计时的商品」「界面导航失败」）
            # 只在引擎日志里，用户盯着清单页的「失败」两字完全不知道为什么。
            res = st.get("error") or self._engine_reason(st) or "未抢到"
            if dry_run:
                # 演练跑出来的「失败」不是真失败：没点鼠标，本来就不会成交。
                # 不标出来，用户会以为程序坏了。
                res = f"演练：{res}"
            self._set_status(tid, FAILED, res)
            self._say(f"{self._label(task)}：{res}", "warn")
