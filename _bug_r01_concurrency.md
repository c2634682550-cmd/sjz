# daForS 并发与线程安全专项审查（R01）

- 审查对象：`C:\Users\26346\Desktop\project\dafors`（工作区当前内容，含未提交改动）
- 审查范围：仅并发/线程安全；不改代码、不 commit、不 push
- 约束遵守：全程使用桩件替换鼠标/屏幕/OCR/窗口，未移动真实光标、未打开可见窗口、未触碰真机游戏
- 探针脚本（不在仓库内）：`conc_probe.py`、`conc_probe_chain.py`、`conc_probe_desktop.py`

## 方法

1. 先枚举全部线程起止点、锁、共享可变状态（`core/`、`desktop.py`、`run.py`）。
2. 每个候选点写独立进程探针：用真实模块代码 + 只替换硬件层（`Mouse`/`Screen`/`ocr_chain`/`find_game_window`），
   并发起跑后统计线程数、活线程峰值、动作次数、缓存一致性。
3. 用 `threading.settrace` 记录线程在目标行上的交错（引擎实验），用 `sys.setswitchinterval(1e-6)`
   做强制抢占放大（records 实验），不修改被测代码逻辑。
4. 所有“窗口放大”都如实标注（真实窗口尺寸另附实测值）。

实测环境：Python 3.11 / Windows；`Config.load()` 磁盘读实测 p50=0.65ms、p90≈1.2ms。

## 结论速览

| # | 严重度 | 问题 | 位置 |
|---|--------|------|------|
| 1 | 致命 | `Scheduler.start()` 的“已在运行”判断在锁外，并发调用可起多条调度线程 | core/scheduler.py:419-428, 478-484 |
| 2 | 致命 | `Engine.start()` check-then-act 无锁，并发调用起 N 条引擎线程同时点鼠标 | core/engine.py:206-207, 265-267 |
| 3 | 严重 | `Engine.stop()` 只等 2s 就报“已停止”，线程仍在阻塞调用里、之后还会点 | core/engine.py:270-281 |
| 4 | 严重 | `desktop` 提权失败后回抢单实例闸的返回值被丢弃 → 双实例 | desktop.py:327-333 |
| 5 | 严重 | `_start_hotkeys()` 无互斥，并发注册泄漏热键线程，“停止热键”失效 | core/server.py:965-993 |
| 6 | 严重 | `_bye` 在 `scheduler.stop()` 可能没停干净时直接 `os._exit(0)` | core/server.py:704-715 |
| 7 | 严重 | `ocr._client()` 首建无锁，N 个线程各建一份模型 | core/ocr.py:86-105 |
| 8 | 严重 | `recovery.Cleanup.run()` 无互斥，重复执行冻结/收尸/落记录 | core/recovery.py:301-330 |
| 9 | 一般 | `PageWatcher` 并发 start 起多条轮询；`_loop` 吞掉所有异常；`stop()` 跨线程关屏 | core/pagewatch.py:60-73, 157-163 |
| 10 | 提示 | 热键 id 自增非原子、钩子回调异常无兜底、停止去抖无锁 | core/hotkey.py:291-292, 166-172 |

---

## 1. 致命：`Scheduler.start()` 并发可起多条调度线程，且 `self._engine` 被中途换掉

**位置**：`core/scheduler.py:417-485`（关键：419 的 `is_alive()` 判断在 `428` 的 `with self._lock` 之外；`478-484` 的 `_stop.clear()`、`self._engine = Engine()`、新线程启动也在锁外）

**复现步骤**（`conc_probe.py sched`）：

1. 把 `sch.Config` 换成内存桩（`load()` 内含 3ms 磁盘读延时，对齐实测 p90），队列放 1 条 pending 任务，`Engine` 换成可观测桩。
2. 6 条线程用 `Barrier` 同时调用 `sched.start()`。
3. 采样并存线程数，结束后判任务状态。

**实际观测**：

```
successful_starts=6, engines_constructed=7,
peak_concurrent_scheduler_threads=6,
before_stop: queue_status="running", scheduler_running_flag=false, active=null
```

- 6 次调用全部返回“调度器已启动”；共构造 7 个 Engine（1 个来自 `__init__`，6 个来自 `start()`）。
- 6 条 `_run` 同时活着；最后的 `self._engine` 指向第 6 个，前 5 个已不可达。
- 所有 start 返回后，`scheduler.status()` 的 `running` 仍为 `false`、`active` 为 `null`，任务却停在 `running`。
- 对照：把 `load()` 的 3ms 去掉（`SLOW=0`）仍 6/6 成功——真实磁盘读+p0-4 模板 `isfile` 检查已足够宽。

**影响范围**：

- 多个 `_run` 共享同一个 `self._engine` 属性：`_run_task`/`_drive_task`（`scheduler.py:772-846`）每次循环都现读 `self._engine`，中途被换就会对着新引擎 `status()`/`stop()`，真正在点的旧引擎没人停 → 鼠标不可控。
- `_borrow_config`/`_return_config`（`scheduler.py:786-846`）不在 `self._lock` 内：两条调度线程同时借用/归还会互相覆盖 `regions`、并把对方的备份写回 config → 配置被写坏。
- `_run` 的 `finally` 任意线程退出都会把 `_active_id` 清空，状态页显示与实际不符。

---

## 2. 致命：`Engine.start()` 并发 → N 条引擎线程同时点同一个鼠标

**位置**：`core/engine.py:206-207`（`if self._thread and self._thread.is_alive(): return False`）与 `265-267`（`self._thread = Thread(...)` + `start()`），中间隔着 `Config.load()` 与 `find_game_window()`，无任何锁

**复现步骤**（`conc_probe.py engine`）：

1. 桩替换 `Config`（`load()` 2ms）、`Mouse`、`Screen`、`find_game_window`、`_validate`、`_load`。
2. `_execute_countdown` 换成“进入即记线程 id，循环到 `_stop` 每 2ms 计一次点击”。
3. 8 条线程 `Barrier` 同时 `eng.start(deadline=None, ...)`；同时 `threading.settrace` 只记录 206/221/265/267 行。

**实际观测**：

```
start_results: 8 × ["True","已启动"],  engine_threads_entered=8,  clicks_in_300ms=2276
trace: 8 个不同线程 id 先全部命中 206，随后各自命中 221/265/267
```

`settrace` 原始片段（线程 id, 行号）：

```
(37060,206) (22132,206) (32168,206) (4864,206) (32460,206) (28836,206) (15560,206) (15728,206)
(28836,221) (28836,265) (28836,267) (15560,221) (15560,265) (15560,267) ...
```

即：8 条线程都在“通过存活检查”之后才走到赋值行——check-then-act 竞态成立。

**影响范围**：同一 `Mouse` 被多线程并发 `click()`，点速翻 N 倍、可能重复下单；`start()` 里的 `self._log.clear()`、`_stats.update()` 会被后到的线程清掉前一条线程的运行日志/统计；`self._stop.clear()` 会把先到线程刚收到的停止信号抹掉。

---

## 3. 严重：`stop()` 说“已停止”时线程还活着，并且之后仍会点一下

**位置**：`core/engine.py:270-281`（`t.join(timeout=2.0)` 后无条件 `return True, "已停止"`）

**复现步骤**（`conc_probe.py latestop`）：

1. `_execute_countdown` 换成“先睡 3s（模拟不可中断的 OCR/HTTP），再记一次点击”。
2. `start()` → 0.2s 后 `stop()`；记录 stop 阻塞时长与返回瞬间的 `status()["running"]`。
3. 1.4s 后再起第二个 Engine，看能否同时活。

**实际观测**：

```
stop: ["True","已停止"],  stop_blocked_ms=2001,
engine_still_running_when_reported_stopped=true,
clicks_after_stop=[("click", 2796ms 相对 stop 调用)],   // 比“已停止”晚 0.79s 才点
second_engine_start_while_first_alive=["True","已启动"]
```

**影响范围**：

- 用户看到“已停止”，鼠标还会再点一次（阻塞中的那一步无法被打断：OCR 超时 3s > join 2s）。
- 串到调度器就变成“停不下来还能再开”：`Scheduler.stop()`（`scheduler.py:487-505`）只 join 调度线程、不校验引擎线程死活；`_drive_task` 一看到 `_stop` 就把任务回 PENDING（`scheduler.py:898`）并退出。用与实测同款的引擎桩串联复现（`conc_probe_chain.py`）：

```
stop_returned_at_s=0.333, task_status_after_stop="pending",
second_start_after_stop=["True","调度器已启动"],
engines_still_working_after_restart=2,
clicks_after_stop_returned=[("click", 旧引擎id, 3.0s), ...]
```

  即“停止成功 + 任务回 PENDING + 再开一轮成功 + 旧引擎仍在点”。

---

## 4. 严重：提权失败后不检查回抢单实例闸的结果 → 两个实例同时跑

**位置**：`desktop.py:327-333`（327 `_release_mutex()` 放闸 → 328 `elevate_self()` 失败 → 332 `_single_instance()` **丢弃返回值** → 333 `return False`，`main()` 在 367 行继续起服务）

**复现步骤**（`conc_probe_desktop.py`，两个真实进程 + 真实命名互斥）：

1. 本进程 `acquire()` 拿到闸。
2. 子进程循环抢闸；本进程在 `_elevate_if_needed()` 里 `release()`，`elevate_self()` 桩为“120ms 后返回 False”（不弹 UAC）。
3. 看本进程的 `_elevate_if_needed()` 返回值、以及本进程是否持有闸。

**实际观测**：

```
elevate_returned=false, we_hold_mutex=false, child="CHILD_HELD True",
main_would_continue=true
```

**影响范围**：闸已被子进程（或用户再点开的另一个实例）拿走，本进程仍会继续 `serve()` 起 HTTP 服务与调度器 → 双实例、双调度线程、两套引擎抢同一鼠标。`run.py` 对同一返回值有判断，`desktop.py` 没有，两条入口行为不一致。

---

## 5. 严重：`_start_hotkeys()` 并发注册，泄漏热键线程且“停止热键”失效

**位置**：`core/server.py:965-993`（`global` 三件套 + 先 stop 再重建，全程无锁；`_hotkey` 只有在 `h.start()` 返回后才赋值）

**复现步骤**（`conc_probe.py hotkey`）：

1. `HookHotkey`/`Hotkey` 换成可计数桩，`start()` 里睡 30ms（真实 `HookHotkey.start()` 要等钩子线程 ready，最长 `ready.wait(2.0)`，窗口更大）。
2. 6 条线程 `Barrier` 同时调 `_start_hotkeys({...})`（模拟两个 `/api/hotkey` 写请求并发，或启动期与请求期重叠）。

**实际观测**：

```
created=12, still_running(not stopped)=12, unescaped_after_last_call=12
```

12 个热键对象全部存活，没有任何一个被后一次的 stop 循环收掉——因为每一条线程进入时读到的 `_hotkey` 还是 `None`，等它们返回时全局已被别人覆盖。

**影响范围**：`RegisterHotKey` 线程只增不减；F8/F7 一次按键会触发多条回调；用户点“关闭热键”后仍有残留热键生效（按 F8 还能停、按 F7 还能开）。`_hotkey_stop_at`（`server.py:869-874`）也是无锁普通全局，多线程下 300ms 去抖可能同时通过。

---

## 6. 严重：`_bye` 在引擎可能没停干净时直接 `os._exit(0)`

**位置**：`core/server.py:704-715`（`_scheduler.stop()` 返回值不看，随后 `os._exit(0)`）

**复现步骤**（`conc_probe.py quit`）：子进程里起一条“按下鼠标—睡 5s—抬起”的工作线程（不碰真实鼠标，只写状态文件），0.2s 后执行与 `_bye` 相同的顺序（`stop()` 超时 → `os._exit(0)`）。

**实际观测**：

```
button_state_at_exit="DOWN"
```

**影响范围**：`os._exit` 不做任何清理、不等 daemon 线程：引擎线程若正处在“按下→抬起”之间，进程没了、抬起永远不会发生——真实设备上就是鼠标键卡在按下状态（系统级影响，不只是本程序）；同时 `Cleanup`（还原光标、解除钩子、写事故记录）与 records 落盘都可能没执行。触发前提正是第 3 条（`stop()` 2s 超时即可复现）。

---

## 7. 严重：`ocr._client()` 首建竞态，多份模型同时加载

**位置**：`core/ocr.py:86-105`（`if _backend is None:` 判断与赋值都在 `_lock` 之外，只有内部“失败置 `_broken`”用了锁）

**复现步骤**（`conc_probe.py ocr`）：把 `ocr_chain.make` 换成“构造耗时 50ms 并计数”的桩，10 条线程 `Barrier` 同时调 `ocr.available()`。

**实际观测**：

```
backends_built=10
```

**影响范围**：冷启动时（`serve()` 预热 + 首个请求 + 页面识别线程撞在一起）会重复加载 N 份检测/识别模型：内存与启动时间成倍增长；`_backend` 被反复替换，正在进行中的 OCR 调用可能拿到一半初始化的后端。同类窗口在 `set_url`/`set_lang` 换后端时也存在。

---

## 8. 严重：`Cleanup.run()` 并发重复执行清理动作

**位置**：`core/recovery.py:301-330`（`Cleanup.run()` 无锁；注释称“幂等”，实际每次调用都会把 `ORDER` 里的动作全跑一遍）

**复现步骤**（`conc_probe.py cleanup`）：用 7 个计数动作装成 `ResourceAdapters`，4 条线程 `Barrier` 同时 `Cleanup(ad).run({})`。

**实际观测**：

```
actions_run=28, expected_once=7,
per_action={unhook:4, reap:4, freeze:4, close_capture:4, reset_cursor:4, release_input:4, record:4}
```

**影响范围**：真实调用点有两条并发路径——`guard_stop()`（HTTP 线程，`engine.py:369-390` 附近）与 `_run` 的 `finally`（引擎线程，`engine.py:536-545`）：前者硬停、后者发现 `guard.state == HALTED` 也跑一遍。结果是同一次事故写 2 条事故记录、光标被还原两次、子进程被收两次；若 `record` 走 records 落盘，还会叠出重复行。

---

## 9. 一般：`PageWatcher` 并发 start 起多条轮询；`_loop` 吞掉所有异常；`stop()` 跨线程关屏

**位置**：`core/pagewatch.py:60-66`（`start()` 判断与建线程无锁）、`157-163`（`except Exception: pass`）、`67-73`（`stop()` 在调用方线程 `self._nav.screen.close()`）

**复现步骤**（`conc_probe.py page`）：`nav_factory` 换成桩导航（`_snap` 返回 40×40 假图），`_loop` 包一层计数，6 条线程同时 `start()`；观察 0.4s 后再 `stop()`。

**实际观测**：

```
before_stop: peak_loops=6, nav_objects=1, snapshot={"page":"market","at":...,"stale":false}
after_stop:  screen_closes=1, snapshot(不变), loops_alive=0
```

**影响范围**：

- 6 条轮询同时每 20ms 抓整屏 + 截图变化检测，OCR 负载成倍；`_sig`/`_nav` 被多线程无锁读写（本次因 GIL 只建了 1 个 nav，但 `_make_nav` 无锁，存在建两份 mss `Screen` 的窗口）。
- `_loop` 把所有异常吞掉且不置 `stale`：`stop()` 关掉 screen 后、或显示器热插拔/抓屏失败后，循环会一直失败但页面时间戳冻结、`stale=false`，界面把旧页面当“当前页面”显示，且日志里一个字都不会有（真正的静默降级）。
- 可达性说明：`_watcher.start()` 只在 `serve()` 里调一次（`server.py:1105`），所以“并发 start”在这条入口上目前不可达；本条按“防御性缺陷 + 静默降级”记。

---

## 10. 提示：热键层的三处无锁共享

**位置**：`core/hotkey.py:291-292`（`Hotkey._next_id += 1`）、`core/hotkey.py:166-172`（钩子回调里 `self._fire()` 无 try/except）；`core/server.py:869-874`（`_stop_hotkey_at`）

**复现步骤/观测**：未单独造并发实验（这三处都是“窗口极窄 + 后果有限”）。静态确认：`_next_id += 1` 是读-改-写，多线程可分配同 id；`_dispatch` 里 `t.start()` 抛 `RuntimeError: can't start new thread` 时会从 ctypes 回调里逃逸，钩子链不做 `CallNextHookEx` → 该次按键被吞（极端情况下钩子失效）。

**影响范围**：同 id 导致 `WM_HOTKEY` 误路由（两个热键互相触发）；回调异常吞键；去抖失效导致连发 stop 线程。

---

## 中间修改态标注（按要求不当 bug 报）

- `core/records.py`：工作区有未提交改动，且 `stats()`/`_cache` 段（约 255-330 行）正落在改动 hunk 内。我在 `stats()` 里看到“两次读 `_cache["acc"]`、命中缓存与全量扫描都无锁、并发扫描互相覆盖”的线索；强制抢占（`setswitchinterval=1e-6`，6 读者 + 4 写者，4s）与 16 轮定向插桩均未复现异常 → 记为**中间态线索，不计入 bug**，等该文件落地后再验。
- `core/scheduler.py`、`core/engine.py`、`core/server.py`、`desktop.py` 均有未提交改动，但本文引用的行段（`start()`/`stop()`/`_bye`/`_start_hotkeys`/`_elevate_if_needed`）**不在**改动 hunk 内（已逐段 `git diff --unified=0` 核对），属 HEAD 代码，按 bug 报。
- `core/ocr.py`、`core/pagewatch.py`、`core/recovery.py`、`core/hotkey.py`、`core/jobs.py`、`core/config.py`：工作区干净。

## 确认没问题的项（同样跑并发对照）

| 项 | 位置 | 做法 | 结果 |
|----|------|------|------|
| 配置事务与原子写 | core/config.py | 12 线程 × 8 次 `Config.transaction()` 并发自增 | 96/96 无丢失，落盘 JSON 完好；tmp 名带 `pid + 线程 ident`，跨线程不撞名 |
| 台账写 + 读并发 | core/records.py `add/load` | 4 写 × 1200 + 3 读 × 1200（强制抢占） | 0 异常、0 坏行 |
| jobs 提交与状态轮询 | core/jobs.py | 30 并发提交 + 4 线程轮询 `busy()/status()` | 无结构性异常；1 次 `Busy` 属设计上的串行约束，非竞态 |
| 校时读 | core/timesync.py | 8 线程 × 300 次 `info()/offset()` | 无异常，锁覆盖完整 |
| 引擎自 join | core/engine.py:270-281 | 在引擎线程内部调 `stop()` | 1ms 返回，无 `cannot join current thread`、无死锁 |
| 事故闸 | core/scene_guard.py | 全 RLock 封闭 | 未发现无锁读写窗口 |
| 单实例互斥本身 | core/single_instance.py | 真实命名互斥跨进程抢闸 | `acquire/release/activate` 行为正确；问题只在 desktop 的调用顺序（见第 4 条） |

## 无法验证 / 未复现

1. **跨进程共享内存可见性**：运行时无实现（`core/`、`desktop.py`、`run.py` 均无 `mmap`/`SharedMemory`/`CreateFileMapping`）。仅 `tools/bench_precision.py` 用 `mmap`，属离线工具，不计入运行时并发面。
2. **线程池/定时器回调访问 UI 或配置**：`core/` 无 `ThreadPoolExecutor`、无 `threading.Timer`。`desktop.py` 的 pywebview 回调只写 `shown/closed` 两个 bool 字典，主线程读，GIL 下无结构风险。
3. **真实鼠标/键盘硬件层交错**（`input.py` 的 `SendInput`/interception、`precision.py` 的 spin 循环）：按约束未动真光标，全部以桩替代；这层只能证明“逻辑层会并发”，无法证明驱动层的实际交错时序。
4. **`records.load` 偶发 `RecordReadError`**：全套连跑时出现过 1 次（3 个读线程 × 200 次 `load(limit=0)` 期间），随后 3 次单独复跑与 4 写 × 1200 高压复跑均未再现；异常信息未取到。注意 `records.py` 的 `load()`（105-243 行）本身就在未提交改动区内，不能排除是新 `load` 实现的问题，需该文件落地后带日志再验。
5. **`FireProcess.restart()` 与 `fire()` 的 `self.p` 交换**：`restart()` 不在 `_lock` 内替换 `self.p`/`self._q`，`fire()` 只护写 stdin。未构造实验（该模块在本次审查的入口链路里未被调用），暂列未验证。
6. **`Config.load()` 与 `Config.transaction()` 的 `_save_lock` 争用**：`Scheduler.status()` 在 `self._lock` 内调 `Config.load()`（含磁盘 I/O），属锁粒度过粗（会让所有 API 串行化），本次未造成死锁（未发现反向加锁顺序），只记提示，不单列 bug。
