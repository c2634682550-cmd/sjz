# R04 状态机与分支接缝专项审查

审查对象：`C:\Users\26346\Desktop\project\dafors`
范围：异常自愈/场景守卫 × 引擎主循环 × 调度器 × 输入通道 × 倒计时估计
方式：静态读码 + 状态组合矩阵推演 + 纯内存守卫探针 + 现有单测复跑
约束：未改业务代码、未 commit/push、未启动真机、未移动真实光标、未开可见窗口

---

## 0. 结论摘要

共发现 3 个致命、8 个高危、10 个中低危。最要命的三条：

1. **看门狗自己破坏了「FIRE/CONFIRM 卡死不重跑」保护**：`stop()` 先把状态改成 `abort`，看门狗再读状态就永远读不到 `fire/confirm`，于是自动重开同一任务 → 可能同一件买两次（B01）。
2. **guard 的「硬停/人工等待」不向调度器传播**：调度器只认引擎状态，硬停被记成普通失败；下一个任务 `Engine.start()` 无条件 `user_resume()` 清掉硬停，队列继续下单（B02）。
3. **硬停/误买后 `containment_pending` 永不清零**：前端异常弹层的唯一按钮被永久禁用（`正在安全收尾，收尾完成前不能关闭`），用户只能杀进程（B03）。

另有一个全局性事实：**五级处置 + 规划器 + 自动续跑在真实运行路径里全部未接线**。`feed()/tick()/RecoveryRunner/ManualWatcher/T0Estimator/Watchdog` 只被 `tests/` 使用；生产只有 `force()` 的 6 个场景，17 个场景中 11 个永不触发，反作弊检测实际不存在（B06）。

现有 253 个单测全部通过，但**没有任何一个测试同时驱动 Scheduler + Engine + SceneGuard**，所以这些接缝问题测不出来。

---

## 1. 谁有权驱动同一步操作（驱动力矩阵）

| 动作 / 状态写入 | 写入者 | 语义 | 问题 |
|---|---|---|---|
| `Engine._state = ABORT` | `Engine.stop()` | 「停止这一轮」 | 与看门狗判定共用，B01 |
| `Engine._state = ABORT` | `Engine.guard_stop()` | 「安全停止 + 收尾」 | 不 join 线程、不告诉调度器，B03/B07 |
| `Engine._state = ABORT` | `Engine._run()` 异常分支 | 「出错」 | 与人工等待混在同一个 `abort` |
| `Engine._stop.set()` | `Cleanup.freeze` / `guard_stop` / `stop` / 用户停止 | 「马上别点了」 | 三处来源，语义各异 |
| Scheduler 任务状态 → `PENDING` | `Scheduler.stop()` | 「暂停队列」 | F8 热键走这条 |
| Scheduler 任务状态 → `FAILED` | `_drive_task` | 把 guard 硬停也归到这里 | B02 |
| Scheduler 任务状态 → `DONE` | `_drive_task` | 成交 | 会被 `guard_stop` 覆盖成 `abort`，B12 |
| 重启引擎线程 | `_run_task` 看门狗 | 「卡死重开」 | B01 |
| guard 状态清零 | `Engine.start()` | 「用户新一轮」 | 同时清掉硬停/人工等待，B02 |
| guard 状态清零 | `guard_resume` | 「用户点继续」 | 不重启引擎，B04 |

结论：一次「停止」有 4 条不同路径、3 种最终状态，用户无法预期；同一步操作（点击）名义上由引擎独占，但看门狗重开、旧线程残留、未接线的恢复器都能成为第二个驱动者。

---

## 2. 状态组合矩阵（两两 / 三三叠加）

| # | 叠加组合 | 复现序列 | 推演结果 | 判定 |
|---|---|---|---|---|
| M-1 | 正常 + 异常（引擎内 force） | 引擎点确认 → 发现弹窗异常 → `force(CONFIRM_UNEXPECTED)` → `Aborted` → `_state=ABORT` | 引擎线程退出；guard 停在 `RECOVERING`，无 `tick` 驱动，永不复位 | 状态悬挂，B05 |
| M-2 | 正常 + 异常（余额不足） | 确认阶段 `_guard_insufficient` → `force(INSUFFICIENT)` → `Aborted` | guard=`WAITING_HUMAN`，引擎死；调度器置 FAILED；下一个任务 `start()` 清掉等待 | B02/B04 |
| M-3 | 恢复中 + 收到停止 | guard=`RECOVERING`（B05 的悬挂态）→ 用户点「停止」→ `guard_stop` | `hard_halt` + 同步收尾 + `ABORT`；但引擎已死/或仍在循环内，`guard_stop` 不 join，仍可能再点一次 | B07 |
| M-4 | 硬停 + 正常收尾同时 | 成交 → `_state=DONE` → 调度器还没轮询 → 用户点停止/自动 `guard_stop` | `_set_state(ABORT)` 覆盖 DONE；任务记 FAILED；`stop.after_hit` 的 `on_hit` 被跳过；队列继续 | B12 |
| M-5 | 任务切下一个 + 上一轮未清 | 任务 A 连败 3 次 → ABORT；任务 B 第一次没抢到 | `_fails_in_row=3` 残留 → 计数变 4 → 立即 `Aborted(连续 4 次)` | B08 |
| M-6 | 任务切下一个 + 模板残留 | A 有自己的 buy 模板 → `sync_template` 覆盖全局；B 没有自己的模板 | B 通过 preflight（全局文件在），实际用 A 的模板匹配 | B09 |
| M-7 | 卡死 + 已在 FIRE | 点购买后卡在驱动调用 → 心跳 `age>=STALL` → 看门狗 `engine.stop()` → 状态被改成 `abort` | 883 行 `where in (fire,confirm)` 恒为假 → 返回 `stalled` → 自动重开 | **B01（致命）** |
| M-8 | 停止进行中 + 立刻开始 | `stop()` join 未完成 → 用户点开始 | `start()` 里 `self.stop()` 幂等返回「正在停止」，`is_alive` 仍真 → 拒绝启动 | 安全，但提示含糊 |
| M-9 | 停止 join 超时 + 任务状态 | 引擎线程卡在不可中断调用 → `stop()` 3s join 超时 → 直接返回 | 任务停在 `RUNNING`；之后 `start()` 被 `_thread.is_alive()` 拒绝；进程内无复位路径 | 状态无法离开，B17 |
| M-10 | 调度线程异常 + 任务 RUNNING | `_drive_task` 抛异常 → `_run` 捕获继续 | 该任务永远 RUNNING，直到 `stop()` 或重启进程 | B17 |
| M-11 | 硬停 + 前端弹层 | 任何 HALT（误买/安全停止） | `containment_pending` 恒 true → `b.disabled=true`、`收尾完成前不能关闭` | **B03（致命）** |
| M-12 | 人工等待 + 前端继续 | guard=`WAITING_HUMAN`，引擎已死 → 点「继续」 | 只 `_reset()` guard；不重启引擎、任务已 FAILED；toast 说「正在重新核对画面」但什么都不发生 | B04 |
| M-13 | 恢复中 + 用户直接开始新一轮 | recovering 悬挂 + 队列点开始 | `Scheduler.start()` 新建 `Engine()`，旧 guard 直接丢弃；等价于静默清场 | B02 |
| M-14 | 人工等待 + 自动续跑（若接线） | guard waiting + `goal_met` 为真 + 连续正常帧 | `feed()` 只对 `RECOVERING` 走 `_ok_tick`，`WAITING_HUMAN` 永不自动复位（探针 A） | B04（实测） |
| M-15 | 抖动 + 恢复清场（若接线） | 异常帧/正常帧交替 + `force(CLICK_NO_EFFECT)` | 第 2 拍就清回 `ok`，且清场那一拍本身带异常（探针 B） | B14（实测） |
| M-16 | 重入 + 阶梯超时（若接线） | `force` 同一场景两次 → 再 `tick()` 到 44.5s | 总计时被重置为第二次进入时刻，`max_total_s=40` 不生效（探针 C） | B14（实测） |

---

## 3. 问题清单

### B01【致命】看门狗把 FIRE/CONFIRM 的「不自动重跑」保护自毁

- 文件行号：`core/scheduler.py:855-864`、`core/scheduler.py:879-892`、`core/scheduler.py:747-784`（重开循环）、`core/engine.py:270-281`
- 可复现状态序列：
  1. 引擎进入 `FIRE`/`CONFIRM`，点击后卡在同步驱动调用（心跳停滞 ≥ `WATCHDOG_STALL_S`）；
  2. 调度器 856-864 判定卡死，调用 `self._engine.stop()`；
  3. `Engine.stop()` 在 279-280 把 `_state` 从 `FIRE/CONFIRM` 改成 `ABORT`；
  4. 调度器 867 重新读状态，`where = "abort"`；
  5. 883 的 `where in ("fire","confirm")` 永远不成立 → 890 返回 `"stalled"`；
  6. `_run_task`（约 779 行）看到 `"stalled"` → 再 `engine.start()` 重开同一任务。
- 实际现象：注释写着「卡死在点购买/确认阶段绝不自动重跑」，代码却必然重跑；同一件商品可能被买两次，且第二次走完整购买流程。这是本审查里唯一直接造成资金损失的逻辑。

### B02【致命】guard 硬停/人工等待不传播到调度器；下一任务启动即清除

- 文件行号：`core/engine.py:252-253`（`start()` 无条件 `user_resume()`）、`core/engine.py:381-392`（`guard_stop`）、`core/scheduler.py:846-906`（`abort → FAILED`）、`core/scheduler.py:476-482`（新一轮新建 `Engine()`）、`core/scheduler.py:562-574`
- 可复现状态序列：
  1. 任务 A 触发 `SC_MISBUY`（`core/engine.py:2184`）或被 `guard_stop`；
  2. guard=`HALTED`，引擎 `ABORT`，调度器 900-906 把 A 记成 `FAILED`（不是「需要人工」）；
  3. 调度器循环继续，取下一个 `PENDING` 任务 B；
  4. `_drive_task` → `engine.start()` → `core/engine.py:253` `self._guard.user_resume()`；
  5. guard 回到 `OK`，`containment_pending` 被 `_reset()` 静默清掉，B 开始真实点击。
- 实际现象：用户点「安全停止」或发生误买硬停后，队列里的下一个任务在秒级内继续下单，硬停形同虚设；`Scheduler.start()` 每次新建 `Engine`，连跨轮保留都没有。

### B03【致命】`containment_pending` 永不清零 → 硬停后前端唯一按钮永久禁用

- 文件行号：`core/scene_guard.py:728-741`（`hard_halt` 置 pending）、`core/scene_guard.py:743-749`（`containment_done` 存在但没人调）、`core/engine.py:381-388`（`guard_stop` 只跑 `cleanup.run`）、`core/engine.py:539-548`（`_run` finally 同样只跑 cleanup）、`web/index.html:2623-2663`（`pending` → 按钮 disabled + 「收尾完成前不能关闭」）
- 可复现状态序列：
  1. 误买或点「安全停止」→ `hard_halt` → `_containment_pending=True`；
  2. 收尾适配器执行完，但全工程没有任何生产者调用 `containment_done()`；
  3. 前端轮询得到 `guard.containment_pending=true`；
  4. 异常弹层的唯一按钮变为 disabled，提示「正在安全收尾，收尾完成前不能关闭」。
- 实际现象：硬停后软件永远停在「收尾中」，队列页的开始按钮也无法启动新一轮（modal 不消失）；用户只能任务管理器杀进程。`guard.containment_ok` 也永远是 `None`。
- 备注：`_reset()`（`scene_guard.py:862-875`）会把 pending 静默清掉，但这要求先成功 `user_resume()`，而唯一按钮被禁用，形成闭环死锁。

### B04【高】`WAITING_HUMAN` 没有离开边：自动续跑不存在，手动「继续」也不重启引擎

- 文件行号：`core/scene_guard.py:590-647`（`feed` 无 waiting 分支）、`core/scene_guard.py:714-726`（`user_resume`/`auto_resume` 只 reset）、`core/scene_guard.py:851-860`（`_ok_tick` 仅 `RECOVERING`）、`core/engine.py:375-379`、`core/scheduler.py:562-567`
- 可复现状态序列：
  1. `force(SC_INSUFFICIENT)` / `SC_USER_TAKEOVER` → `WAITING_HUMAN`，引擎抛 `Aborted` 退出；
  2. 用户补足金币/停止操作，画面恢复正常；
  3. `feed()` 连续 12 帧无异常（探针 A），`goal_met= True`；
  4. guard 仍停在 `WAITING_HUMAN`，`level=4`；
  5. 用户点弹层「继续」→ `/api/guard/resume` → 只 `_reset()`；引擎线程早已死亡，任务已 FAILED，调度器已前进。
- 实际现象：前端提示「你停下来后它会自己接着跑」，实际什么都不跑；界面上「继续」是纯安慰按钮。要么用户手动点「开始」（会把所有 FAILED 复活重跑），要么一直卡着。
- 与设计文档冲突：`_scene_guard.md:435-461` 要求「监听到达成 → `on_resume` → 守卫清零 → 调度器从当前任务继续，保留已花预算/已买数量」，代码没有恢复点，也没有 `on_resume`。

### B05【高】`RECOVERING` 成为引擎退出后的永久悬挂态，且规划器从未接线

- 文件行号：`core/engine.py:539-556`（finally 只处理 `HALTED/WAITING_HUMAN/DONE`）、`core/engine.py:491-495`/`500-502`/`509-511`/`518-520`（多个异常路径 `force` 后立即 `ABORT`）、`core/scene_guard.py:684-703`（`tick` 只被 `RecoveryRunner.step` 调用）、`core/recovery.py:335-408`（`RecoveryRunner` 全工程无生产引用）
- 可复现状态序列：
  1. 点击时鼠标被抢 / 抢不到前台 / 引擎内部异常 → `force(SC_SELF_FAULT|SC_WINDOW_LOST)`（`hard_level=0`）；
  2. `_set_state(ABORT)`，线程退出；
  3. 没有任何 `tick()` 调用，`plan_failed`/`escalate` 永不推进；
  4. guard 永久 `RECOVERING`（`level=1`，`doing=正在再试一次`）。
- 实际现象：日志/API 显示「自动恢复中」，实际零动作；因前端 `paintException` 只在 `level>=4` 弹层，用户看不到任何提示，只觉得「任务失败了」。`SC_CONFIRM_UNEXPECTED`（`engine.py:1824`）同样悬挂。

### B06【高】关键子系统全部未接线：五级处置只剩「force + 提示」

- 文件行号：`core/engine.py:157`（唯一 `SceneGuard` 实例）、`core/engine.py:490-520`/`1824`/`2184`/`2288`（全部 `force` 调用点，无一处 `feed`）、`core/scene_guard.py:590`（`feed` 无生产调用）、`core/recovery.py:335`（`RecoveryRunner` 只在 tests）、`core/countdown.py:565`（`T0Estimator` 只在 tests）、`core/watchdog.py`（整文件未被引用）
- 可复现状态序列：
  1. 运行中发生「点了没生效 / 不认识界面 / 列表刷新买不到 / 卡顿 / 掉线 / UI 变了 / 分辨率变了 / 识别失败 / 时钟漂移 / 设置坏了 / 反作弊提示」；
  2. 引擎不采集这些信号，guard 不 `feed`，因此 `RECOVERING` 与 `HALTED` 永远不会被这些场景驱动；
  3. 只有 6 个场景能通过 `force` 进入：`CONFIRM_UNEXPECTED/MISBUY/INSUFFICIENT/USER_TAKEOVER/WINDOW_LOST/SELF_FAULT`。
- 实际现象：17 个场景里 11 个是死代码；`SC_ANTICHEAT`（`scene_guard.py:395-401`，`hard_level=HALT`）永不触发，反作弊保护等于不存在；`_scene_guard.md` 的「五级处置」「单一驱动」「规划器」在当前运行路径里全不生效。
- 若在其他分支已完成接线，请按「待接线」而非 bug 处理（见 §7）。

### B07【高】`guard_stop` 不 join、不同步：安全停止后仍可能再点一次，光标还原被覆盖

- 文件行号：`core/engine.py:381-392`（只 `hard_halt` + 同步 cleanup + `_stop.set()` + `ABORT`，不调用 `stop()`/不 join）、`core/engine.py:270-281`（`stop()` 才 join 2s）、`core/engine.py:1747`/`1860-1868`（`time.sleep(fire_delay)` 与确认循环的 `_stop` 检查点）
- 可复现状态序列：
  1. 引擎在 `_fire_and_confirm` 的 `time.sleep(delay)`（最长 `fire_delay+jitter`）或两次 `_stop` 检查之间；
  2. HTTP 线程调用 `guard_stop`：`_stop.set()` + `_restore_cursor()` 立即执行；
  3. 引擎线程被唤醒后继续当前迭代，`_click` 在循环顶部检查之前已经发出；
  4. 光标被引擎再次移动，刚刚的还原失效。
- 实际现象：界面已显示「已停止 / 安全停止：还原 N 项」，游戏里仍落下一次点击；光标位置与 UI 声称的不一致。

### B08【高】连续失败计数 `_fails_in_row` 跨任务残留

- 文件行号：`core/engine.py:138`（初始化）、`core/engine.py:1602`（只在成功时清零）、`core/engine.py:1621-1627`（阈值判定）、`core/engine.py:198-268`（`start()` 未重置）
- 可复现状态序列：
  1. 任务 A（list 模式）连败 3 次，`stop_after_fails=3` → `Aborted`；
  2. 调度器同一 `Engine` 实例启动任务 B（`scheduler.py:816-824` 复用 `self._engine`）；
  3. B 第一次没抢到 → `_fails_in_row=4` → 立刻 `Aborted("连续 4 次没抢到")`。
- 实际现象：第二个任务几乎立刻失败，日志数字与实际不符；用户会以为 B 的条件有问题。`_rows` 也未清空，状态接口在 B 首轮扫描前仍返回 A 的挂单列表。

### B09【高】任务级模板「只进不出」：`sync_template` 覆盖全局，切任务不还原

- 文件行号：`core/scheduler.py:806-814`（`_borrow_config` 只快照 `regions`/`active_task`）、`core/scheduler.py:828-835`（`sync_template` 把任务模板复制到全局路径）、`core/scheduler.py:783-784` + `_return_config`（不还原模板）
- 可复现状态序列：
  1. 任务 A 用自定义 buy/confirm 模板；运行时 `sync_template` 写入全局模板路径；
  2. A 结束，`_return_config` 只还原区域；
  3. 任务 B 没有自己的模板，`preflight`（`engine` 的模板检查 + `scheduler.py:471-476`）因为全局文件存在而通过；
  4. B 以 A 的按钮长相/坐标做匹配。
- 实际现象：B 轻则每个商品都匹配失败，重则把 A 的购买按钮模板匹配到 B 界面的错误位置并点击（模板内含相对坐标）。

### B10【高】`Engine.stop()` join 超时仍宣称「已停止」，区域还原先于线程真正退出

- 文件行号：`core/engine.py:270-281`（`join(timeout=2.0)` 后不校验 `is_alive()`，无条件 `return True`）、`core/scheduler.py:867-870`（`stop()` 后立刻继续）、`core/scheduler.py:783-784`（`_return_config`）
- 可复现状态序列：
  1. 引擎线程卡在不可中断驱动调用；
  2. `_drive_task` 判定卡死 → `engine.stop()` 2s 后返回「已停止」但线程仍活；
  3. `_return_config` 把 `regions`/`active_task` 写回上一任务；
  4. 旧线程恢复运行，继续用已被改写/属于上一任务的区域点击。
- 实际现象：点击坐标错位；配合 B01 可在「卡死重开」时让两套区域交替生效。

### B11【高】停止/继续/启动三者竞态，HALTED 可被「继续」清掉而不重启

- 文件行号：`core/scene_guard.py:714-722`（`user_resume` 无 `containment_pending`/state 校验）、`core/engine.py:375-379`、`core/scheduler.py:417-427`（start 与 stop 串行化）、`core/scheduler.py:487-524`（stop 的 join/事务）
- 可复现状态序列：
  1. `/api/guard/stop` 与 `/api/guard/resume` 被快速连点（或前端轮询后自动重发）；
  2. `hard_halt` 之后 `user_resume` 立即把 `HALTED` 置 `OK`，`_containment_pending=False`；
  3. `_stop` 仍为 set，引擎线程退出，任务 FAILED；
  4. guard 快照显示一切正常，收尾义务消失。
- 实际现象：用户看到「已停止 → 已恢复」，实际这一轮已废；下一次点击「开始」把所有 FAILED 复活重跑（`scheduler.py:446-453`），相当于停止被抹掉。另：`start()` 与 `stop()` 的事务交错时，旧 stop 可能把新一轮刚写的 `RUNNING` 改回 `PENDING`（`scheduler.py:517-521` vs `419-427`），出现「引擎在跑但列表显示等待」。

### B12【中】`guard_stop` 覆盖 `DONE`：成交被记成失败、`on_hit` 被跳过

- 文件行号：`core/engine.py:389`（无条件 `_set_state(ABORT)`）、`core/engine.py:278-281`（`stop()` 才排除 DONE）、`core/engine.py:526-532`（`on_hit` 依赖 `_state == DONE`）、`core/scheduler.py:893-906`
- 可复现状态序列：
  1. 成交 → `_execute` 设 `DONE`；
  2. 调度器尚未轮询到 `done`，用户点「安全停止」（或前端自动 `guard_stop`）；
  3. `_state` 被改成 `ABORT`；
  4. `_drive_task` 走 900-906 → 任务 `FAILED`；`stop.after_hit` 不触发，队列继续。
- 实际现象：「已抢 N 件」与「失败：用户请求停止」同时出现；开了「抢到就停」也不停。

### B13【中】三种「停止」语义互不一致

- 文件行号：`core/server.py` F8 走 `_hotkey_stop → scheduler.stop()`、`/api/guard/stop` 走 `engine.guard_stop()`、`_drive_task` 卡死走 `engine.stop()+重开`、`core/scheduler.py:487-521`（暂停：ARMED/RUNNING→PENDING）
- 状态序列：F8 → 队列 PENDING（可续、保留已买）；弹层「安全停止」→ 引擎 ABORT + 收尾，队列继续下一个；看门狗 → 停 + 重开。
- 实际现象：用户按「停止」得到的后果取决于从哪个入口按；「安全停止」不停止队列，F8 不做收尾，看门狗会重开。需要统一成一个 `HALT_QUEUE` 状态。

### B14【中／接线后生效】`feed` 抖动清场 + `_enter` 重置总计时（实测）

- 文件行号：`core/scene_guard.py:604-609`（异常帧计 streak）、`core/scene_guard.py:644-646`（**只要 `winner is None` 就进清场分支，未确认异常也被当成「无异常」计 ok tick**）、`core/scene_guard.py:789-798`（`_enter` 重置 `_total_since/_level_since`）、`core/scene_guard.py:862-875`
- 探针输出（`SceneGuard` 纯内存，未接引擎）：
  ```
  B force(click_no_effect) -> recovering 1
  B cycle0: state=recovering level=1 owner=click_no_effect
  B cycle1: state=ok level=0 owner=None   ← 清场那一拍本身带异常帧
  C wall=44.5s(自首次进入), since= 5.0 -> recovering 2 (max_total_s=40 本应到顶)
  ```
- 实际现象（接线后）：OCR 抖动会让恢复反复进出，`clear_window_s` 完全没用（代码只用 `clear_ticks`）；重复 `force` 会把阶梯总计时清零，`max_total_s/hard_timeout` 永不触发。文档 `_scene_guard.md:430-431` 要求「第 4/5 级锁 + 冷却窗口」，均未实现。

### B15【中／接线后生效】`RecoveryRunner` 无停止检查、同步执行最多 20 个动作、收尾双跑

- 文件行号：`core/recovery.py:362-408`（`step` 不读任何外部 stop 事件；`_execute` 在一个 `step` 内同步执行）、`core/recovery.py:440-459`（`library.run(7 项)` + `cleanup.run()` 两次执行同一批收尾）、`core/recovery.py:121-136`（`freeze` 代价 0、任何 level 可用，会被规划器优先选中）、`core/recovery.py:240-283`（规划器无「1 秒内同一动作只执行一次」）
- 状态序列（接线后）：用户点停止 → 引擎 `_stop` 置位 → 恢复器继续导航/点击直至本轮 plan 用尽；`freeze` 本身又会把引擎 `_stop` 置位，导致「先冻结、再恢复」自相矛盾。
- 实际现象：停止后仍有恢复动作落到游戏；事故记录/光标还原写两遍；`_scene_guard.md:429` 的去重规则未实现。

### B16【中】收尾失败被当成成功（回滚不彻底 + 假成功）

- 文件行号：`core/recovery.py:319-326`（适配器返回 `False` 时落入 `else: done.append`）、`core/engine.py:406-415`（`_restore_cursor` 忽略 `SetCursorPos` 返回值、异常才 `False`）、`core/scene_guard.py:743-749`
- 状态序列：光标被 UIPI 拦截/坐标越界 → `SetCursorPos` 静默失败 → 适配器返回 `True` → `Cleanup` 报 `done` → `containment_ok=True`。
- 实际现象：界面显示「安全收尾 7 项全部完成」，光标其实没有回位，危险区域仍在鼠标下。

### B17【中】`RUNNING` 可在进程内永久卡住；`stop()` join 超时后无复位路径

- 文件行号：`core/scheduler.py:611-628`（`_run` 捕获异常继续，但不复位当前任务）、`core/scheduler.py:487-516`（join 超时直接 return，不改状态）、`core/scheduler.py:44-48`（`_recover_stale_tasks` 只在构造时跑）
- 状态序列：`_drive_task` 抛异常 / 引擎线程卡死 → 任务 `RUNNING` → `stop()` 因线程仍活返回「正在停止」→ `start()` 被 `419-427` 拒绝 → 任务既不在跑也不可启动。
- 实际现象：队列永久停在「运行中」，只有重启软件（触发 `_recover_stale_tasks`）才能恢复。

### B18【低】`SKIPPED` 不可达；`DONE` 只能靠前端显式改状态离开

- 文件行号：`core/scheduler.py:36-37`（定义）、`386`/`450`（只读过滤）、全工程无写入者；`core/scheduler.py:441-456`（DONE 不复活，设计如此）
- 实际现象：状态机存在永远到不了的状态；`add()` 对同名同 workflow 复用旧 `DONE` 状态（`scheduler.py:202-240`），用户改条件后点「开始」得到「队列里只剩已完成的任务」，看起来像点了没反应（设计如此，但易踩）。

### B19【低】`PageWatcher` 的 `page` 与引擎 `state` 并行展示，最多 2.5s 自相矛盾

- 文件行号：`core/pagewatch.py:55-58`（1.5s 轮询 + OCR）、`core/pagewatch.py:100-126`（控制台在前台时标记 stale 但保留旧 page）、`core/server.py:234`（`st["page"]` 直接来自 watcher）
- 状态序列：引擎已 `WATCH` 进入详情并 `FIRE`；watcher 上一帧仍是 `onsale`；`/api/scheduler` 同时返回 `state=fire`、`page=onsale`。
- 实际现象：界面「现在在哪」滞后/错误；不是驱动者，但会误导用户判断引擎状态。

### B20【低】`guard_stop` + finally 的 HALTED 分支重复收尾

- 文件行号：`core/engine.py:381-388` 与 `539-548`
- 状态序列：用户 `guard_stop` → 同步 `cleanup.run`；引擎线程随后进入 finally，guard 仍 `HALTED` → 再跑一次 `cleanup.run`。
- 实际现象：事故记录写两遍、`_say` 两套「安全收尾」日志、光标还原两次；不致命但会污染审计与用户判断。

---

## 4. 状态机完整性小结（对应「无法到达 / 无法离开」）

| 状态 | 可达？ | 可离开？ | 说明 |
|---|---|---|---|
| Engine `IDLE` | 是 | 是（start） | 正常 |
| Engine `SCAN/WATCH/FIRE/CONFIRM` | 是 | 是（正常/stop/异常） | 正常，但 `FIRE` 被看门狗覆盖成 ABORT（B01） |
| Engine `DONE` | 是 | 否（设计） | `guard_stop` 会把它改成 ABORT（B12） |
| Engine `ABORT` | 是 | 是（start） | 与「人工等待/出错/用户停止」共用，信息丢失 |
| Guard `OK` | 是 | 是 | 正常 |
| Guard `RECOVERING` | 是（force） | **否**（无 tick） | B05 |
| Guard `WAITING_HUMAN` | 是（force） | 否（无自动续跑；手动继续只清 guard） | B04 |
| Guard `HALTED` | 是（force/guard_stop） | 是，但收尾义务永远清不掉 | B03 |
| Scheduler `PENDING` | 是 | 是 | 正常 |
| Scheduler `ARMED` | 是（有 fire_at） | 是（stop→PENDING） | 正常 |
| Scheduler `RUNNING` | 是 | **可能否**（join 超时/线程异常） | B17 |
| Scheduler `DONE` | 是 | 否（设计） | 正常 |
| Scheduler `FAILED` | 是（含 guard 硬停） | 是（start 自动复活） | 与 B02 组合＝停止后重跑 |
| Scheduler `SKIPPED` | **否** | — | B18 |

---

## 5. 已确认没有问题的项

1. **Engine 主状态写点齐全**：`IDLE→(WATCH|SCAN)→FIRE→CONFIRM→DONE/ABORT` 的写入点覆盖正常、停止、异常三条路径，`_set_state` 加锁（`engine.py:560-562`）；`_run` 的 `finally` 必关 `screen`（`557-558`）。
2. **调度器自身串行性**：`_stop_guard` + `_thread.is_alive()` + `stop()` 的 join 超时拒绝启动（`scheduler.py:419-427`、`487-516`）确实阻止了「两轮并行抢同一个鼠标」；`_hit_stop` 用新线程避免自我 join（`526-532`）。
3. **队列基本状态迁移**：`PENDING/ARMED/RUNNING/DONE/FAILED` 的写入与 `_recover_stale_tasks` 启动恢复逻辑自洽；`stop()` 只在 join 成功后才把 ARMED/RUNNING 置 PENDING，这个顺序是正确的。
4. **guard 的纯状态逻辑**：`resolve` 优先级、`_SUPPRESS` 因果抑制、`force` 的「同级/低优先级不抢占」、`LEVEL` 文本与按钮映射，在内存探针与 `tests/test_scene_guard.py` 下均符合设计。
5. **`RecoveryRunner`/`Planner`/`Cleanup` 在隔离环境**：`tests/test_recovery.py` 全过；`goal_met` 判定、`max_plans/max_depth/actions` 上限逻辑正确（未接生产）。
6. **倒计时 `Clock`**：跳变重构、`due()` 只在大跳/Step 触发，未发现状态卡死；`_wait_dialog_unlock` 每 ~25ms 检查 `_stop`，不会拖住停止。
7. **输入通道单点约束**：`Mouse.click` 带前台校验与禁点区校验；虽然 `input.py` 无跨引擎锁，但调度器的串行约束（见第 2 项）保证同一时刻只有一个引擎线程在点。
8. **现有测试**：`python -m pytest tests/test_scene_guard.py tests/test_recovery.py tests/test_scheduler_queue.py tests/test_engine_list_queue.py -q` → **253 passed in 24.11s**。

---

## 6. 无法验证 / 本轮未覆盖

1. **真机时序窗口**：B01/B07/B10 的竞态具体毫秒窗口，受「不动真鼠标、不开可见窗口」约束无法实测，只能静态+桩推演。建议后续用假驱动注入延迟做确定性复现。
2. **配置阈值**：`max_total_s`、`WATCHDOG_STALL_S`、`fire_delay_ms` 取自工作区当前 `config.json`（且此文件正被其他分支改动），这些阈值下的具体秒数未逐一实测。
3. **外部看门狗/托盘**：`core/watchdog.py` 未被引用；`desktop.py`（47KB）可能有独立的进程重启/托盘逻辑，是否会绕过 `HALTED` 未审。
4. **独立开火进程**：`core/fire_proc.py`、`tools/fire_worker.py` 当前未被 `server/scheduler` 引用；若某分支启用，「谁在点鼠标」会再多一个驱动者，需并入驱动力矩阵重审。
5. **`T0Estimator`**（`countdown.py:565`）：只在 tests 使用；生产用的是简单 `Clock`。无法判断这是刻意保留还是漏接。
6. **未覆盖的模块**：`core/jobs.py`、`core/switches.py`、`core/nocursor.py`、`core/precision.py`、`desktop.py` 与本专项无直接状态重叠，未逐行审。

---

## 7. 工作区提示（不要当 bug 的部分）

- 当前 `git status` 有 **87 项**未提交改动；`core/engine.py`、`core/scheduler.py`、`core/server.py`、`web/index.html`、`core/countdown.py` 等均为 **M**（相对 HEAD 已改），`core/scene_guard.py`、`core/recovery.py`、`core/pagewatch.py`、`core/fire_proc.py`、`core/watchdog.py`、`tests/test_scene_guard.py` 等为 **未跟踪新文件**。
- 本报告结论针对**工作区当前快照**。若以下内容在其他分支已合入，请按「待接线/已修」处理，不计为 bug：
  - `feed()/RecoveryRunner/ManualWatcher` 的接线（对应 B04/B05/B06/B14/B15）；
  - `containment_done()` 的调用点（对应 B03）；
  - `guard_stop` 后同步 `scheduler.stop()` 或队列 HALT 态（对应 B02/B13）；
  - 看门狗 `where` 判定改为在 `stop()` 之前取状态（对应 B01）。
- 若 `engine.py` 的 `start()` 里 `user_resume()`（253 行）是「用户主动开新一轮」的有意设计，也必须配合「HALTED 不可被新一轮清除」的门闩；否则 B02 依然成立。
