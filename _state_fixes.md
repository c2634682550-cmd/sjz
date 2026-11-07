# 状态机 / 调度接缝修复报告（`_state_fixes.md`）

范围：`_bug_r04_state.md`（B01–B14、M 矩阵）+ `_bug_r01_concurrency.md`（R01 第 1–10 条）
+ 返工两项（F4 / R3）+ 故障注入 S9（看门狗接线）。
改动文件：`core/engine.py`、`core/scheduler.py`、`core/scene_guard.py`、
`core/pagewatch.py`、`core/config.py`（仅 `_normalize` 的路线派生，R3 需要）。
`web/`、`server.py`、`countdown.py`、`input.py`、`license.py`、`timesync.py`、
`switches.py`、`market.py`、`ocr_chain.py` 与既有测试文件一行未动。
新增测试：`tests/test_state_hardening.py`（28 个用例，全部走真实线程 / 内存配置，离线）。

---

## 1. 三条致命

### B01 看门狗绕过「不自动重跑」→ 重复下单

- **根因**：`_drive_task` 先 `engine.stop()`，`stop()` 把 `_state` 落成 `ABORT`，之后
  再读 `st["state"]` 判 `in ("fire","confirm")` 永远为假 → 返回 `"stalled"` →
  `_run_task` 重开同一任务，等于把可能已经成交的那一件再买一次。
- **改法**
  1. `Engine` 新增 `_stage_before_stop` 和 `_mark_stage()`；`stop()` 在置 `ABORT`
     **之前**先记下 `FIRE/CONFIRM`，`_set_state(ABORT)` 同样记（异常退出路径）。
     `status()` 暴露 `stage_before_stop`。
  2. `_drive_task` 改为 `stage_before = st["stage_before_stop"] or 停止前的 state`。
  3. 只要 `stage_before in ("fire","confirm")`：任务落 `FAILED + needs_human=True`，
     `_set_halt(hard=True)` 锁住整条队列，返回 `"stalled_no_retry"`，**不再重开**。
  4. 守卫报 `halted/waiting_human` 且阶段是 fire/confirm 时同样 `needs_human` + 硬停。
- **证据**：`test_stop_records_fire_stage_before_abort`、
  `test_watchdog_stall_in_fire_is_not_redispatched`（断言 `start_calls == 1`、
  `needs_human is True`、`_halt_reason` 非空）。还原旧读法后该用例失败（rc=1）。

### B02 守卫硬停不落调度器、下一任务把它清掉

- **根因**：`Engine.start()` 无条件 `guard.user_resume()`；调度器既不查守卫，
  也不把 `HALTED/WAITING_HUMAN` 落到任务状态。守卫的「硬停」只是引擎内的瞬时态。
- **改法**
  1. `SceneGuard` 新增 `_halt_latch`（`halt_latched` 属性）：`hard_halt` 与
     `hard_level==LEVEL_HALT` 的场景置位；`_reset()` 才清（只有 `user_resume`/
     `auto_resume`/`succeed` 会走）。
  2. `Engine.start()` 不再清守卫；`halt_latched` 或 `STATE_HALTED/WAITING_HUMAN`
     直接拒绝启动并给出可读原因。
  3. `Scheduler` 新增 `_halt_reason/_halt_hard`（持久队列锁）：`start()` 在
     同一临界区内先查队列锁、再查守卫；`_run_step()` 在**派发任何任务之前**先查
     守卫，命中就锁队列。
  4. `_drive_task` 把 `halted/waiting_human` 落到任务：进过开火 → `needs_human`；
     未进开火的人工段 → 任务回 `PENDING`（点「继续」后自动接着跑）。
- **证据**：`test_guard_halt_survives_engine_start`、
  `test_scheduler_does_not_dispatch_when_guard_halted`。还原「无条件 resume」/
  「派发前不看守卫」后两例均失败（rc=1）。

### B03 `containment_pending` 永不清零

- **根因**：`containment_done()` 在生产路径没有调用者；`_cleanup.run()` 只在
  少数异常分支跑，跑完也不清标记 → 前端唯一按钮永远 disabled。
- **改法**：把「跑收尾」和「清标记」绑在一起：`_contain_guard(reason)` 统一执行
  `cleanup.run(...)`，把 `containment_ok / containment_failed` 写进守卫证据，
  **之后**调 `containment_done()`。三个生产入口都走它：`guard_stop()`、
  `guard_resume()`（若仍 pending 先补跑收尾）、`_run` 的 `HALTED` 收尾、
  `force_release_input()`；另加超时/下一次启动**不是**清零路径。
  `guard_stop()` 返回收尾**之后**的快照，前端拿到的就是已清零的状态。
- **证据**：`test_guard_containment_cleared_only_after_cleanup`
  断言 `containment_pending is False` 且 `containment_ok is True`。
  删掉 `containment_done()` 调用后该用例失败（rc=1）。

---

## 2. 高危 B04–B11

| 编号 | 根因 | 改法 | 回归证据 |
|---|---|---|---|
| B04 | `WAITING_HUMAN` 无出边；点「继续」只 reset 守卫，不重启队列 | `_ok_tick` 在人工段用 `goal_met(owner, s)` 判定，满足 `clear_ticks` **且** `t-last_detect >= clear_window_s` 才 `auto_resume`；`Scheduler.guard_resume()` 清锁后若无线程且有 PENDING/ARMED 则 `start()` 重启队列 | `test_waiting_human_auto_resumes_when_goal_met`、`test_scheduler_resume_restarts_the_queue`；还原 `if False:` 后失败 |
| B05 | `RECOVERING` 没有出口，引擎退出后永久悬挂 | `SceneGuard.abandon_recovery()`：RECOVERING 时按场景 `halt_on_exhausted / max_level` 结算成 WAITING_HUMAN 或 HALTED；`_run` 的 `finally` 在本轮结束时调用 | `test_recovering_is_settled_when_round_ends`；删掉该方法后 AttributeError 失败 |
| B06 | 关键子系统未接线（RecoveryRunner / PageWatcher / OCR 联动） | **未在本文件改**，见第 6 节待接线清单 | — |
| B07 | `guard_stop` 不 join；先还原光标、后等线程 → 仍可能补点一次 | `guard_stop` 顺序改为「置停 + 记阶段 + `hard_halt` + `join(2s)` + 收尾 + 才置 `ABORT`」，并在 `_state==DONE` 时不覆盖 | `test_guard_containment_cleared_only_after_cleanup`、`test_guard_stop_does_not_overwrite_done` |
| B08 | `_fails_in_row` / `_rows` 跨任务残留 | `_start_locked` 每轮清零 `_fails_in_row=0`、`_rows=[]`（与价格筛选一起清） | `test_engine_start_resets_task_residue`；还原为 `pass` 后失败 |
| B09 | 任务模板只进不出，下一条任务拿上一条的模板过 preflight | `_borrow_config` 快照 `buy/confirm` 原字节（不存在记 `None`）；`_return_config` 有则写回原字节、原本不存在则删掉 | `test_template_borrow_is_returned`；删掉还原循环后失败 |
| B10 | `stop()` join 超时仍宣称已停止；配置还原早于线程退出 | `Engine.stop()` 只有线程真的不在才返回成功；`_run_task` 用 `wait_stop(3.0)` 判定，没停干净就**不还原**配置并锁队列 | `test_engine_stop_is_honest_when_thread_will_not_quit`、`test_scheduler_stop_keeps_task_when_engine_will_not_quit` |
| B11 | 停止/继续/启动三者竞态，HALTED 被「继续」清掉却不重启 | 队列锁与守卫门闩分离：队列锁只由 `guard_resume()`/用户显式动作清；`start()` 与 `stop()` 同在 `_stop_guard` 临界区；`guard_resume()` 负责把队列拉起来 | `test_scheduler_resume_restarts_the_queue`、`test_scheduler_start_is_atomic_under_barrier` |

中危顺带修掉：

- **B12**：`guard_stop` 在 `DONE` 时不再写 `ABORT`（`test_guard_stop_does_not_overwrite_done`）。
- **B13**：三种停止语义统一为「置停 → join → 收尾 → 才落终态 → 返回真实结果」。
- **B14**：见下节两条实测项。

---

## 3. 已实证项（B14）

1. **抖动未确认帧被当正常**：`feed()` 在「有检测、未到 `min_confirm`」分支不再走
   `_ok_tick`，改为 `_last_detect = t; _ok_ticks = 0; return`。
   证据：`test_unconfirmed_jitter_ticks_are_not_treated_as_normal`（4 拍全保持
   RECOVERING、`ok_ticks == 0`）；还原 `_ok_tick` 调用后 2 拍即被清场 → 失败。
2. **重入重置总计时**：`_enter` 新增 `same`（同场景且已在 RECOVERING/WAITING_HUMAN），
   `same` 时不动 `_total_since/_level_since/_fails/_attempts/_level`。
   证据：`test_reentry_does_not_reset_the_total_timer`（0s 进入 → 10s 升级 → 20s
   重入 → 65s 触发 total_timeout → WAITING_HUMAN）；还原 `same = False` 后失败。

---

## 4. 组合矩阵

| 矩阵 | 同时发生的两类 | 现在的行为 | 证据 |
|---|---|---|---|
| M-1 | 正常 → 引擎内 force → 异常退出 | `_run` finally 把 `RECOVERING` 用 `abandon_recovery` 结算，不再悬挂 | `test_recovering_is_settled_when_round_ends` |
| M-3 | 恢复中 + 用户停止 | `guard_stop` 先冻结、join、再收尾，收尾完成后才清 `containment_pending` | `test_guard_containment_cleared_only_after_cleanup` |
| M-4 | 硬停 + 成交（DONE）同时 | `DONE` 不被 `ABORT` 覆盖，`on_hit` 路径保留 | `test_guard_stop_does_not_overwrite_done` |
| M-5 | 切下一任务 + 上一轮连败残留 | `_start_locked` 清 `_fails_in_row/_rows` | `test_engine_start_resets_task_residue` |
| M-7 | 卡死 + 已在 FIRE | 记 `stage_before_stop`，`stalled_no_retry` + `needs_human` + 锁队列 | `test_watchdog_stall_in_fire_is_not_redispatched` |
| M-8 | 停止未完成 + 立刻开始 | 同一临界区拒绝；`stop()` 不假成功，任务不回 PENDING | `test_scheduler_start_is_atomic_under_barrier`、`test_scheduler_stop_keeps_task_when_engine_will_not_quit` |
| M-12 | 人工等待 + 前端「继续」 | `guard_resume` 清锁并重启队列 | `test_scheduler_resume_restarts_the_queue` |

---

## 5. 并发 R01 第 1–5 条

| 编号 | 根因 | 改法 | 证据 |
|---|---|---|---|
| #1 | `Scheduler.start()` 的 `is_alive()` 在锁外 | 存活判断 / 守卫检查 / `_engine` 替换 / 建线程全在同一 `_stop_guard` 临界区；线程启动异常回滚 `_thread=None` | `test_scheduler_start_is_atomic_under_barrier`（6 线程 Barrier，成功=1、构造 Engine=2） |
| #2 | `Engine.start()` check-then-act | `_lifecycle` 非阻塞闸门 `acquire(blocking=False)`，抢不到直接拒绝并给原因；线程创建失败回滚状态 | `test_engine_start_is_atomic_under_barrier`（8 线程，成功=1） |
| #3 | `stop()` 只等 2s 就报「已停止」 | 只有在 `join` 后线程确实不在才返回 True；否则返回 False + 原因，且**不改** `ABORT`、不把任务回 PENDING | `test_engine_stop_is_honest_when_thread_will_not_quit`、`test_scheduler_stop_keeps_task_when_engine_will_not_quit` |
| #4 | `_bye` 不看 `stop()` 直接 `os._exit(0)` | `Scheduler.stop()` 同步调 `force_release_input()`（冻结 + 释放输入 + 还原光标），失败显式返回 False；`Engine._restore_cursor` 检查 `SetCursorPos` 返回值 | `test_scheduler_stop_reports_input_release_failure` |
| #5 | `PageWatcher` 并发 start；`_loop` 吞异常 | `_life` 锁内 `is_alive` 判断（幂等）；`_note_error` 计数 + 第 1/每 20 次写 stderr；`snapshot()` 暴露 `errors/error/age_s`；`stop()` 先 join 再关屏 | `test_pagewatcher_start_is_idempotent_under_barrier`、`test_pagewatch_records_loop_errors` |

**实测数字**（独立于单测的第二种复核，真实线程 + Barrier）：

```
[A] 6 线程 Barrier 同时 Scheduler.start(): 成功=1 构造Engine=2（1 基线 + 1 实起）
    拒绝消息=['调度器已在运行', ...]   status()['running']=True  调度线程alive=True
[B] 8 线程 Barrier 同时 Engine.start(): 成功=1 引擎线程创建=1
    拒绝消息=['引擎正在启动或停止，稍后再试', ...]
[C] stop() -> ok=True '已停止'
    返回瞬间 running=False state=abort
    点击计数: 停止前=146 返回时=146 之后1s=146 差值=0
[D] 线程卡在不可打断调用 stop(timeout=0.05) -> ok=False
    msg='停止请求已发出，但引擎线程还在收尾（可能仍有一步点击）'
    running=True state=scan（不假成功、不改终态）
```

---

## 6. 「修之前失败」反向验证

在 `%TEMP%\dafors_revert\` 的副本里逐条还原旧代码（原工作区全程只读），每条单独跑
对应用例，跑完换下一个干净副本：

```
baseline 单点                         rc=0  1 passed
B01a 停止前记录阶段                    rc=1  失败 ✓
B01b 开火阶段被打断不重跑              rc=1  失败 ✓
B02a 启动不许清硬停                    rc=1  失败 ✓
B02b 派发前看守卫                      rc=1  失败 ✓
B03  收尾做完才清 pending              rc=1  失败 ✓
B04  人工段自动续跑                    rc=1  失败 ✓
B05  恢复态结算出口                    rc=1  失败 ✓
B14a 未确认抖动不算正常                rc=1  失败 ✓
B14b 重入不重置总计时                  rc=1  失败 ✓
B08  切任务清残留                      rc=1  失败 ✓
B09  模板借用归还                      rc=1  失败 ✓
R01#1 调度器启动原子性                 rc=1  失败 ✓
R01#2 引擎启动闸门                     rc=1  失败 ✓
R01#3 停止不许假成功                   rc=1  失败 ✓
R01#4 退出前释放输入                   rc=1  失败 ✓
R01#5a 页面轮询幂等                    rc=1  失败 ✓
R01#5b 轮询异常不静默                  rc=1  失败 ✓
RESULT: OK（17/17 旧行为下均报错）
```

并发两条额外在副本里放大 check-then-act 窗口（`time.sleep(0.03~0.05)`），复现
「多线程同时过检查」，不是靠运气。

---

## 7. 回归

- `python -m compileall -q core tests` → **OK**。
- 相关既有测试 + 新测试：
  `test_state_hardening / test_fix_d / test_fix_e / test_scheduler_queue /
  test_scheduler_add / test_scene_guard / test_recovery / test_engine_list_queue /
  test_engine_dispatch / test_console_api / test_resume / test_process_lifecycle`
  → **403 passed, 1 xfailed**。
- 全量跑时 `tests/test_countdown.py::test_estimator_survives_wrong_digits` 仍失败，
  单独复跑同样失败；`core/countdown.py` 不在本次可改范围，工作区有大量他人未提交
  改动，**非本次引入**。
- `tests/test_recovery.py` 在整目录全量跑时曾偶发跨用例状态污染，单文件跑通过；
  未改该文件。

顺带清理：`scheduler.py` 里同名 `guard_status()` 定义重复（后者覆盖前者），已删掉
多余的那份；对外行为不变。

---

## 8. 待接线清单（本次不越界改）

1. **`core/server.py::_start_hotkeys()` 无互斥** —— 6 线程会造 12 个热键对象且全部存活，
   「停止热键」失效。需要一把模块级锁 + 保存真实句柄。（文件别人在改）
2. **`core/ocr_chain.py` / `ocr._client()` 首建无锁** —— 多线程同时首建会加载多份模型。
   需要 `threading.Lock` 双重检查，失败要能回滚到 `None`。
3. **`core/cleanup.py::Cleanup.run()` 名义幂等实则无锁** —— 并发会重复执行
   `release_input/reap`。需要内部锁 + `_done` 标记；引擎侧已串行化，但别的调用方没有。
4. **`core/server.py::_bye` 仍直接 `os._exit(0)`** —— 现在由 `Scheduler.stop()` 里的
   `force_release_input()` 兜底；建议 server 侧显式检查 `stop()` 返回值，False 时再补一次
   释放/记录后再退出。
5. **`RecoveryRunner` 未接线（B06）** —— 五级处置目前只有 `force + 提示`；`_plan` 生成器
   与执行器需要接进引擎的 `_refresh_task` 循环。
6. **`PageWatcher` 与引擎状态显示（B19）** —— `stale=true` / `age_s` 已可用，但前端仍可能
   同时显示「页面=详情」和「引擎在开火」。需要 server 把 `stale` 一起下发并在前端降级显示。
7. **`web/index.html` 的按钮态** —— `containment_pending` 现已能在收尾后清零；建议按钮
   `disabled` 同时参考 `halt_latched`，避免硬停未收尾时被误点。
8. **`core/countdown.py`** 的既有失败与本次无关，留给该文件负责人。
9. **R3 的「旧路线」提示只到 API** —— `tasks()/tasks_status()` 已带
   `route_stale/route_hint`，但 `web/index.html` 尚未渲染该标记（不在本次可改范围）。
10. **`watchdog` 段阈值没有前端入口** —— 默认 `step_s=25 / round_s=90 / confirm=3 /
    poll_s=1`，需要时手工写进 `config.json`；若要暴露到控制台，由 `server.py` 负责人接。

---

## 9. 约束遵守

- 未 commit、未 push。
- 未碰真机：所有测试用内存配置 / 假 Engine / 假鼠标；不抓屏、不注入、不联网、
  不开可见窗口、不起长跑或基准脚本。
- 未修改：`web/index.html`、`core/server.py`、`core/countdown.py`、`core/input.py`、
  `core/license.py`、`core/timesync.py`、`core/switches.py`、`core/market.py`、
  `core/ocr_chain.py`、`tests/` 下既有文件。

---

## 10. 返工 F4：借调配置失败必须收场

- **根因**：`_run_task` 把 `_set_status(RUNNING)` 放在 `try` 里，`_borrow_config()`
  （内部 `Config.transaction()` 退出时写盘）留在 `try` 之外。盘满/只读/权限异常从
  调用点直接冒出去，`_run_task` 中途返回，没有人把任务改回 FAILED 或 PENDING ——
  它永久停在 `running`，界面看着在跑，队列再也不会碰它。
- **改法**（`scheduler.py::_run_task`）
  1. `_set_status(RUNNING)`、清 `needs_human`、`_borrow_config()` **全部收进同一个
     `try`**：借调失败与状态置位失败走同一条收场路径。
  2. 失败分支统一 `_settle_task(tid, reason)`：`_set_status(FAILED, reason)`，
     `result` 写「借用配置失败：异常类型: 消息」，清 `_active_id`，日志同步。
  3. 连 FAILED 都写不进去（盘彻底不可写）：`_set_halt(reason, hard=True)` 锁队列，
     并明确写入「重启后该任务会自动回滚为等待」—— 靠既有
     `_recover_orphans()` 把 `running` 回滚成 `pending`（可重试）。
  4. `_run_step` 里 `_drive_task` 抛异常的分支同样改用 `_settle_task`，写不进去就硬停。
- **证据（真实触发一次落盘失败）**

注入点在 `Config.transaction()` 的提交边界（`save()` 抛
`OSError("模拟磁盘只读（WinError 5）")`），事务异常时把内存回滚成写前快照 ——
和真实 `config.py` 的事务语义一致：

```
[F4-a] status=failed result=借用配置失败：OSError: 模拟磁盘只读（WinError 5）
       active=None halt=''
[F4-b] 写盘失败当场 status=running
       halt='借用配置失败：OSError: 模拟磁盘只读（WinError 5）；配置写不进去，
              重启后该任务会自动回滚为等待'
       hard=True
       → 重启回收后 status=pending
```

对应用例：`test_borrow_config_failure_settles_task_not_running`（可恢复的写失败 →
FAILED 收场）、`test_borrow_config_write_failure_halts_and_rolls_back_on_restart`
（盘彻底不可写 → 锁队列 + 重启回滚为 pending）。把 `_borrow_config` 挪回 `try`
外面，第一条用例立刻失败（任务停在 running）。

## 11. 返工 R3：全局路线切换的语义

- **根因**：`config.py::_normalize` 对每条任务无条件 `t["workflow"] = wf`
  （`wf` = 全局路线）或无条件沿用任务自带的 `workflow`，两种写法都有洞：
  前者把「钉住」的任务也改掉，后者让已排队任务完全不跟全局；界面看不出差异。
- **定下的语义**（三态，落盘可见）
  | `workflow_source` | 含义 | 全局切换时 |
  |---|---|---|
  | `global` | 用户从没给这条任务选过路线 | **跟随**：重新派生为新全局路线 |
  | `task` | 用户显式选过（`workflow_explicit=True`） | **不跟随**：保留原路线 |
  | `frozen` | 已经开跑（`status` 非 pending/armed） | **不跟随**：半路不换图，避免瞄准的按钮变样 |
  另落 `workflow_effective`（当前实际用的路线）与 `route_stale = (effective != 全局)`。
  `Scheduler.tasks()/tasks_status()` 对 `route_stale` 的任务补
  `route_hint = "该任务使用旧路线：<中文路线名>"` —— 不静默不一致。
- **证据（真实落盘、两次 `Config.load()`）**

```
[R3-a] 切全局 detail→countdown：
       a（未选过路线）=global/countdown/stale=False
       b（显式 list）  =task/list/stale=True  hint='该任务使用旧路线：在售列表'
[R3-b] 在跑任务切全局 detail→list：
       frozen/detail/stale=True  hint='该任务使用旧路线：详情页加购'
```

对应用例：`test_global_route_switch_rederives_inherited_tasks`、
`test_running_task_does_not_follow_global_switch`（后者还断言在跑任务不被换路线）。

## 12. S9：看门狗真正接进调度

- **根因**：`core/watchdog.py` 的 `Heartbeat/Watchdog` 行为正确，但
  `scheduler.py` 从未引用它；`_drive_task` 里只有一条硬编码 60s 的
  `beat_age_s` 判断，且调度线程自己卡在同步调用里（写盘、`engine.start`）时，
  连那条判断都轮不到执行 —— 整条队列到天亮没动静。
- **改法**
  1. 心跳打点（调度线程）：`_run()` 每轮开头 `beat("tick")`；`_idle()`（所有
     等待点统一的 0.2s 可中断等待）持续打点；`_drive_task` 盯守循环每 50ms 打点。
     语义：**「在等」不等于「卡死」**。
  2. 监督线程 `dafors-watchdog`（daemon，只读心跳、只处置，不参与正常调度），
     采样间隔 `poll_s`。两级判定，**整轮优先**：
     - 整轮：调度心跳 `tick` 超 `round_s` → 调度线程自己不动了，引擎心跳再新也没人
       推进队列；
     - 单步：引擎 `status().beat_age_s` 超 `step_s` 且 `running=True` → 卡在某个同步
       调用里；
     - 两者都要连续 `confirm` 次超时才触发（防抖：慢但还在推进不误杀），触发后进
       冷却期，不连续重开。
  3. 处置分级（**保持原约束**）：
     | 判定 | 阶段 | 动作 |
     |---|---|---|
     | 整轮 | 未知（无法确认停在哪） | `_set_halt(hard=True)` → `guard_stop()`（失败兜底 `stop(2.0)`）→ 当前任务 `needs_human` → 安全停机，**绝不自动重跑** |
     | 单步 | `fire` / `confirm` | `needs_human` + 硬停锁队列 + 停引擎，**绝不自动重跑** |
     | 单步 | 开火之前 | 停本轮 → 既有 `_run_task` 重开策略（最多 `WATCHDOG_MAX_RESTARTS=2` 次），超限落 FAILED |
  4. 接线细节：监督线程按下的处置用 `_wd_kill = {stage, retry, age}` 标记本轮，
     `_drive_task` 据此返回 `"stalled"` / `"stalled_no_retry"`（否则会被当成普通
     「未抢到」，重开策略永远走不到）；`_wd_kill` 每轮开头清空，避免陈旧标记。
  5. 阈值可在 `config.json` 的 `watchdog` 段覆盖：
     `step_s=25.0`、`round_s=90.0`、`confirm=3`、`poll_s=1.0`（生产默认）。
  6. `stop()`/`_run` 的 `finally` 都会收掉监督线程；`status()["watchdog"]` 暴露
     `step_s/round_s/confirm/alive/beat_age_s/stalls(最近 5 条)`。
- **实测触发时延**（测试用极小阈值，真实时钟；不是模拟时钟）

```
[S9-step ] step_s=0.2 confirm=2 poll_s=0.02，引擎心跳已 1.0s
           触发时延 0.041s；整轮恢复（重开+跑完）总耗时 0.05s；start=2；终态 done
[S9-round] round_s=0.1 confirm=2 poll_s=0.02，调度心跳停跳
           触发时延 0.142s；原因='整轮卡住：调度心跳 0.1s 没有推进（阈值 0.1s），已安全停机'
           guard_stop=1，halt_hard=True，stalls=1
[S9-slow ] step_s=0.1，心跳始终 0.01s（慢但推进），跑 0.6s（6 个阈值）
           误杀 0 次：stall_records=[]，stop_calls=0，halt=''
[S9-fire ] step_s=0.2，卡死在 fire 阶段
           start=1（未自动重跑），needs_human=True，result 含「不自动重跑」，halt_hard=True
```

- **一次「卡死后自动恢复」完整记录**（`_run_task` 全程、真实监督线程）

```
[S9] 单步卡死触发时延 0.041s，整轮恢复耗时 0.05s
[S9] 卡死后自动恢复记录：
  07:53:45 [warn] 看门狗：某一步卡死 1.0s（watch 阶段），停掉本轮交给既有重开策略
  07:53:45 [warn] 甲：卡死，收尾后重开一轮
  07:53:45 [warn] 甲：第 1 次自动重开（看门狗判定卡死）
→ 第二轮 start=2，状态 done，halt 为空（没有误锁队列）
```

对应用例：`test_watchdog_step_stall_fires_within_threshold_and_recovers`、
`test_watchdog_slow_step_is_not_killed`、
`test_watchdog_round_stall_safe_stops_with_readable_reason`、
`test_watchdog_step_stall_in_fire_never_retries`。

顺带修掉的重开路径缺陷：原 `_run_task` 重开分支在 `_drive_task` 之前又调了一次
`engine.start()`，下一轮 `_drive_task` 再调必然「已经在运行了」，重开实际从未成功。
现在只由 `_drive_task` 拉起引擎（`test_..._recovers` 断言 `start=2` 覆盖此点）。

## 13. 本轮回归

- `python -m compileall -q core tests` → **OK**。
- `python -m pytest tests/test_state_hardening.py -q` → **28 passed**。
- 相关既有测试
  （`test_scheduler_queue / test_scheduler_add / test_watchdog / test_recovery /
  test_config / test_persist_hardening / test_robustness / test_resume /
  test_retention`）→ **259 passed, 1 xfailed**。
- 整目录全量跑时出现两处**跨用例状态污染**，单文件复跑均通过：
  `test_engine_countdown_flow::test_empty_list_triggers_exactly_one_refresh_then_gives_up`
  （全量跑刷新 3 次 / 单文件 19 passed）、`test_fix_e` 3 例
  （`core/input.py:673` 缺 `mouse_device`，属他人未提交改动）。
  两者均与本次改动无关（本轮未动 `engine.py` 的导航/刷新与 `input.py`）。
- 引擎/守卫/校验/控制台批量（17 个文件）→ **386 passed, 8 skipped, 2 failed**；
  两处失败都在 `test_fix_b`，原因缺真实截图资产
  `_shots\drv_onsale.png`（`assert None is not None`），与环境有关，非代码回归。
- S9 用例的「修之前失败」：新用例依赖 `_arm_watchdog/_start_watchdog`（修复前
  不存在），且修复前 `_drive_task` 只认硬编码 60s 阈值 —— 用例把引擎心跳设成
  1.0s，旧代码永不触发、任务会一直停在 running（测试只能超时），因此修复后
  `start=2 / done` 的结果本身就证明接线生效。
