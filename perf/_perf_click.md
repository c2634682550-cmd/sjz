# 点击速度优化专项（perf/click）

> 目标：把「端到端点击延迟」与「光标占用时长」压到该链路下的最优点。
> 红线：真实设备身份、不吞物理输入（filter 全 0）、`SetForegroundWindow` 0 次、
> 不取消落地确认、还原失败率不上升。
> 本文所有数字来自 `perf/bench/click_perf.py`（Raw Input 旁观 + 真实 HID 设备 dev14，
> VID_3554&PID_F50D），原始结果在 `perf/out/click_*.json`。

> ## ⚠ 口径更正（2026-11-07）：本文的「占用」不再是验收指标
>
> 本文 §0 的 `0.553 → 0.320 ms` 是**占用时间**口径（含等 Raw Input 落地、观察器唤醒、
> GIL 交接、还原回读）。四轮 n=120 复测证明：同一份代码 p50 在 0.28–0.73 ms
> 之间摆 2.6 倍，摆幅与并发负载同步——它不能用来判定代码优劣。
>
> 点击项的**唯一主指标**改为 `click_core_us` = stroke 组装 + `send_mouse_batch`
> DLL 调用（实测 DLL p50 7.8–8.1 µs / p95 9–10 µs，与负载无关）。
> 定义、分档与对照表见 `_perf_flow.md` §11。
>
> `0.250 ms` 与 `≤0.320 ms` 一律标注为「安静机器单次采样 / 无分辨力的旧阈值」，
> 禁止再用于验收或优劣判定（p50 0.25→0.73 命中率只差 ≤0.03pp）。

---

## 0. 结论摘要

| 指标 | 基线（off 组，同窗口复测） | 采纳后（单点 w1 / 连点 b6） | 变化 |
| --- | --- | --- | --- |
| 占用（移动→落地→还原）p50 | **0.553 ms** | **0.320 ms / 0.265 ms** | −42% / −52% |
| 占用 p95 | 1.264 ms | 0.993 ms / 0.412 ms | −21% / −67% |
| 占用 max | 2.565 ms（安静窗口基线） | 1.979 ms / 1.406 ms | −23% / −45% |
| 端到端 p50 | 587 µs | **366 µs** | −38% |
| down/up 内核落地 p50 | 0.160 ms | **0.113 ms** | −29% |
| 还原延时确认成功率（+1ms 回读） | 2.5% | **100%** | 红线不退化，反而确定 |
| 连点归位（每击 +1ms 探针命中本击还原目标） | — | **1.00（8/8）** | — |
| 连点原点稳定（`origin_distinct`） | — | **1（无漂移）** | — |
| Raw Input 收齐率（gap 0/0.05/0.1/0.2ms） | 100% | **100%** | 不退化 |
| 连点 gap p50 | 0.29 ms | 0.306 ms | 距 0.1ms 安全线 0.206 ms |

判定（历史，仅指占用）：**结构项已收敛**。后续三轮还原策略尝试（0.457 / 0.562 / 0.611 ms）
全部劣于当时的 0.320 ms。**该 0.320 不是稳定基线，也不能当验收阈值**（见顶部口径更正）。

---

## 1. 工作方式与快照

- 主仓 `core/` 正在被七路分支合并，**未改动**。冻结快照在 `perf/base/`，
  全部改造与测量都在快照内完成；补丁产出到 `perf/patches/click/`。
- 快照期间发生过一次外部回滚（`perf/base/core/input.py` 被并行合并流程覆盖回 08:36 的版本，
  本轮改造一度全部丢失）。恢复后每次测量前后都用 `Get-FileHash` 校验落盘，
  并在本文记录该环境事实：**`perf/base` 不是安全的独占工作区**，
  并行跑着 `perf/bench/run.py --snapshot p2|p3 --groups ocr,click,flow` 的合并流水线，
  它们同样会注入点击，属于本专项的测量噪声来源之一（见 §7）。

---

## 2. 基线分解（单次点击，p50，单位 µs）

测量点：`click` 入口 → `_click_driver` 入口 → 身份 → 坐标 → 读光标 → 轨迹 →
观测器 → 三条 stroke 提交 → 内核落地 → 落点回读 → 还原 → 上层记账。

| 阶段 | 基线 | 采纳后 | 说明 |
| --- | --- | --- | --- |
| 入口（轨迹规划 + 前台校验） | 8.2 | 7.0 | bench 传 `expect_foreground=None`，生产会多 1 次 `GetForegroundWindow` |
| 设备身份获取 | 2.1 | **0.8** | 原为每击 `mouse_device()`（枚举 + dict 拷贝） |
| 坐标换算（屏→绝对） | 3.0 | **0.4** | 原为每击重算 |
| 读取光标原点 `GetCursorPos` | 6.1 | 6.1 | 必须保留（还原基准 + 漂移检测） |
| 拟人轨迹发送 | 0.5 | 0.3 | 无路径时不再调用 `_emit_plan` |
| 落地观测器取用 | 6.2 | **4.0** | 单例常驻，不再每击取用/建窗 |
| 构造+提交 MOVE | 12.0 | **0（去掉）** | 目标点定位由 DOWN 自带绝对坐标承担 |
| 构造+提交 DOWN | 4.4 | 4.6 | 三条合并进一次 `send_mouse_batch` |
| 构造+提交 UP | 3.8 | 3.8（含在上项锁内） | — |
| 内核落地等待（down+up WM_INPUT 到齐） | 156 | **113** | 少一条 stroke 排队 |
| 落点回读 | 28 | **0.7** | 回原点 stroke 会覆盖目标点，回读目标点无意义且必然空转 |
| `SetCursorPos` 还原 | 23 | **0（去掉）** | 改为同批第 4 条 stroke（MOVE 回原点） |
| 还原确认（轮询光标回原点） | 318 | **0（不再阻塞）** | 见 §6「为什么必须改为延迟确认」 |
| 占用记账 | 0.6 | 0.4 | — |
| `last_click` 记账 | 5.4 | 4.6 | 每击建 dict + `hardware_id[:140]` 切片 |

**审计结论（改造前）**：每击 3 次 `_open`、3 次 `interception_send`、
1 次 `mouse_device()` 枚举、4 次 `GetCursorPos`、1 次 `SetCursorPos`；
进程 IO delta 全 0、句柄 0 增长；无每击 `GetForegroundWindow`（bench 路径）。
即：**没有同步文件/网络 I/O，没有每击重开设备，没有每击重扫设备列表，
没有每击重新计算屏幕映射**——慢的部分全在「每击重复计算」与「固定等待」上。

---

## 3. 每击调用次数（改造后）

| 调用 | 改造前 | 改造后（`audit`，300 击实测） |
| --- | --- | --- |
| `interception_send`（DLL，含 `_open`） | 3 | **1.0** |
| 设备枚举 `mouse_device()` | 1 | **0.003**（按 `drv.gen` 代际缓存，热插拔才失效） |
| `GetCursorPos` | 4 | **2.0**（原点 + 还原回读） |
| `SetCursorPos` | 1 | **0.0** |
| 观测器 `delivery_watch()` | 1（懒建） | **0.0**（实例缓存） |
| 进程 IO（read/write/other ops） | 0 | **0 / 0 / 0** |
| 句柄增长 | 0 | **0** |
| `SetForegroundWindow` | 0（bench 不传前台） | **0** |

---

## 4. 逐项尝试：收益与代价

| # | 尝试 | occ p50 | 结论 | 代价 / 风险 |
| --- | --- | --- | --- | --- |
| A | `cache`：设备句柄、坐标换算、观测器常驻 | 0.553→0.528 | 采纳 | 需 `gen` 代际失效；热插拔后第一次点击会多一次枚举 |
| B | `batch`：三条 stroke 合成一次 DLL 调用 | 同上（省 2 次加锁，≈10µs） | 采纳 | 失败重试只能整批重发，异常路径保留单发补 UP |
| C | `plan_skip`：无路径时不调 `_emit_plan` | ≈0.3µs | 采纳 | 挂载轨迹且有路径时仍必须走，特性不退化 |
| D | `drop_move`：去掉目标点 MOVE | 落地 0.160→0.113 | 采纳 | 目标定位改由 DOWN 的绝对坐标承担（HID 语义允许） |
| E | `restore_drv`：第 4 条 stroke 回原点 | 0.553→0.420 | 采纳 | 必须同批提交（FIFO 保证最后生效）；超出 8 条需扩容缓冲 |
| F | `restore_wait_move`：等回原点 stroke 的 Raw Input move | 0.457→0.320 | 采纳 | 依赖观测器 `want_moves`；观测器不可用时退化为盲等 |
| G | 去掉固定 2ms 还原轮询（`restore_early`） | 0.611→0.320 | 采纳 | 还原确认改为「一次回读 + 下一击复核」，见 §6 |
| H | `btn_noabs`：down/up 走相对坐标 0,0 | 0.647 | **否决** | 落地拖到 0.52ms（UP 被延后），且光标不再随 stroke 定位 |
| I | 还原当场验证（0.6ms 封顶自旋） | 0.562 | **否决** | 桌面位置更新比 Raw Input 晚 0.3–0.6ms，封顶必然大量失败；拉高占用还不如基线 |
| J | 连发 6 击合并成一批提交 | 未测（评估否决） | **否决** | 批量会让中间点击的「落地确认」粒度变粗，违反红线；且 0.1ms 安全间隔已由单发满足（Raw 收齐 100%） |

对照序列（同窗口、顺序执行、每组 200 击）：

```
w1  批量+缓存+drop_move+drv还原+等move+不阻塞   occ 0.320  e2e 366  delayed_ok 1.00
w3  同上但不等 move 事件                        occ 0.457  e2e 502  delayed_ok 1.00
c6  同上但还原当场自旋 0.6ms                    occ 0.562  e2e 603  delayed_ok 1.00
w2  同上但还原用原 2ms 轮询                     occ 0.611  e2e 647  delayed_ok 1.00
off 原始实现                                    occ 0.553  e2e 587  delayed_ok 0.025
prior 仅缓存/批处理（无还原改动）                occ 0.528  e2e 553  delayed_ok 0.000
```

> `delayed_ok` = 点击返回 1ms 后再回读光标是否在原点。`prior`/`off` 命中率低不是"没还原"，
> 而是**注入的位置更新晚于 `SetCursorPos`**，把还原覆盖掉（这正是原实现 2.5% 失败率的来源）。

---

## 5. 采纳的补丁

`perf/patches/click/`

1. `0001-click-input-fastpath.patch`（`core/input.py`）
   - `Mouse.__init__`：`_opt_click` 开关表、`_ident/_ident_gen`、`_abs_cache`、
     `_watch`、`_home/_home_target/_home_pending`。
   - `_click_driver`：身份按代际缓存、坐标按目标点缓存、原点瞬态识别（修连点漂移）、
     无路径跳过规划、观测器缓存、批量提交（含 `drop_move` 与第 4 条回原点 stroke）、
     等待 Button+Move 事件、纯自旋替换 `time.sleep(0)`、还原改为
     「回读一次 → 未命中记 `None` 并交给下一击复核」、`last_click` 增加 `origin/restore_queued`。
2. `0002-click-interception-batch.patch`（`core/interception.py`）
   - `Interception.gen` 代际 + `_batch_buf` 复用缓冲；
     `send_mouse_batch()` 一次加锁、一次 `interception_send`；
     `DeliveryWatch.wait_buttons(..., want_moves=N)` 支持等回原点 stroke 落地。

回归口径：`python perf/bench/click_perf.py --mode {baseline,burst,audit,drift}`，
默认配置即采纳配置（`_opt_click` 全开，`btn_noabs=False`）。

---

## 6. 为什么还原确认必须改成「延迟确认」

原实现：`SetCursorPos(origin)` 后轮询最多 2ms 等光标回位，实测占 **283–318µs**，
且仍有 2.5% 的概率被"晚到的位置更新"覆盖 → 既贵又不可靠。

根因不是 `SetCursorPos` 慢（它 23µs 且同步），而是：
`MOUSE_MOVE_ABSOLUTE` 携带的坐标在驱动队列里**先出 Raw Input 事件、后应用桌面位置**，
两者相差 **0.3–0.6ms（p95 ≈1.2ms）**，中间没有任何事件可供等待（位置应用不进 Raw Input 栈）。

因此只有两种可能：
- 当场验证 → 必须等到位置应用完成，最坏 ≈1ms，占用反而高于基线（实测 0.562–0.611ms，否决）；
- 改成「同批第 4 条 stroke 回原点 + FIFO 顺序保证」→ 回原点动作一定最后生效，
  当场只做一次回读；未命中就记 `None`（已排队、未确认），
  由**下一击首段的原点读取**复核（`_home_pending`），并且绝不把瞬态目标点当成新原点。

连点实测证明这条路是确定的：
`origin_home_rate=1.00`、`origin_distinct=1`、`cursor_home_after=True`、
`landing_ok_rate=1.00`、1ms 后回读 100% 在原点。**还原失败率不升反降（2.5% → 0%）**。

---

## 7. 连点、0.1ms 安全间隔、长跑漂移

连点（200 击，同目标，不按住）：

| 指标 | 值 |
| --- | --- |
| gap p50 / p95 / max | **0.306 / 0.471 / 1.460 ms**（已剔除探针相邻跳） |
| 与 0.1ms 安全间隔的距离 | **+0.206 ms** |
| 占用 p50 / p95 / max | 0.265 / 0.412 / 1.406 ms |
| 落地 p50 / p95 | 0.126 / 0.343 ms（b4，无探针；b6 含探针 0.128 / 0.355） |
| `restore_target_rate`（每击 +1ms，光标是否在本击自己的还原目标上） | **1.00（8/8）** |
| `origin_distinct` | **1** |
| `cursor_at_last_origin` | **True** |
| Raw Input 收齐率（gap 0 / 0.05 / 0.1 / 0.2ms，6 击×3 stroke） | **1.00 / 1.00 / 1.00 / 1.00** |

### 7.1 `origin_home_rate` 的口径（这条要看清）

代码在 [click_perf.py](file:///C:/Users/26346/Desktop/project/dafors/perf/bench/click_perf.py#L348-L401)：

```python
origins.append(tuple(last.get("origin") or ()))
"origin_home_rate": sum(1 for o in origins if o == home) / len(origins)
```

- 它统计的是「**每击开始时**读到的原点（经 `_home_pending` 解析后）是否等于 burst 起始的 `home`」，
  样本点在**点击之前**，等价于「上一击有没有把光标带回起始点」。
- **不是**「每击之后光标是否回到原点」。它天然接近 1.00：`home` 常常就是上一击的还原点；
  更极端地，若 `find_safe_point` 把光标留在目标点上（`home == 目标点`），
  这一击根本不移动光标，「回到 home」恒成立。**所以它只能当"有没有空间漂移"的粗指标，
  不能单独当归位证明。**
- 对应的干净指标是新加的 `restore_target_rate`：每击之后隔 1ms（每 25 击取一次），
  看光标是否落在**这一击自己读到的还原目标**上；探针会人为拉长相邻 gap，
  统计 `gap_ms` 时已把探针前后两跳剔除。
- 实测对照：
  - `b5`（`home` 与真实原点不一致的窗口）：`origin_home_rate=0.05`、`restore_probe_rate=0.00`
    —— 这正是旧口径会误判的场景（光标回到了自己的原点，但不是 `home`）。
  - `b6`（`home` 与原点一致）：`origin_home_rate=1.00`、**`restore_target_rate=1.00`**。
  - 结论：归位能力本身没问题，旧口径在特定 `home` 下会失真；以 `restore_target_rate` 为准。

### 7.2 长跑漂移（`--mode drift --n 10000`，停靠模式 down/up，每 1000 击一块）

| 块 | 落地 p50 (ms) | p95 (ms) | 块墙钟 (s) |
| --- | --- | --- | --- |
| 1 | 0.159 | 0.389 | 2.63 |
| 2 | 0.170 | 0.353 | 2.63 |
| 3 | 0.178 | 0.372 | 2.63 |
| 4 | 0.180 | 0.443 | 2.65 |
| 5 | 0.184 | 0.452 | 2.68 |
| 6 | 0.171 | 0.378 | 2.64 |
| 7 | 0.181 | 0.430 | 2.65 |
| 8 | 0.183 | 0.383 | 2.64 |
| 9 | 0.171 | 0.409 | 2.64 |
| 10 | 0.184 | 0.361 | 2.62 |
| **1 万次合计** | **0.176**（mean 0.211，p95 0.406，max 4.269） | | |

- IO delta `read/write/other = 0/0/0`，句柄 0 增长，`gc_delta=-172`（无分配增长）→ **没有结构性退化**。
- 块墙钟稳定在 2.62–2.68s（每块 1000 击），说明节奏没有变慢。
- `drift_pct`（末块 vs 首块）= 15.7%，但首块是全窗口最低点（0.159），
  第 2–10 块在 0.170–0.184 之间来回，属于并行点击/OCR 任务的抖动而不是单调漂移；
  第 4 块出现的 0.184 在第 10 块重现，可判定为平台期。
- 结论：**1 万次后无漂移性增长**，设备队列延迟仍在 0.17–0.18ms 平台。

### 7.3 连点节奏下限

本链路的节奏下限 ≈ 一次点击的真实占用（≈0.27–0.32ms），因为占用窗口内不能并发下一击
（否则两次注入的 stroke 会交错，落地确认失效）。连点 gap 0.306ms 与 0.1ms 安全间隔之间
差的 0.202ms 不是优化空间，是「必须等 down/up 落地」这条红线的价格。

> 环境噪声说明：本轮测量期间，同一台机器上有另一路 OCR 性能任务
> （20+ python 进程）与 `perf/bench/run.py --snapshot p2|p3 --groups ocr,click,flow`
> 的合并流水线在跑，它们同样注入点击。因此 p50 可信、**p95/max 被显著抬高**
> （同一配置在安静窗口的 p95 曾到 0.457ms）。所有 A/B 都在同一窗口内顺序执行，
> 相对比较仍然有效。

---

## 8. 停止理由

1. **连续两轮无 3% 以上改善**：
   采纳配置 0.320ms；随后把还原确认的三条替代路径（不等 move 0.457、当场自旋 0.562、
   原 2ms 轮询 0.611）逐一实测，全部劣化；再无可降低占用的结构项。
2. **逼近估算下界**：
   可分解的下界 = 提交锁+构造（≈9µs）+ down/up 两条 stroke 的 Raw 落地（≈113µs）
   + 观测器/记账/读光标（≈12µs）+ 调度抖动 ≈ **0.15–0.20ms**；
   实测 0.267–0.320ms，其中 0.10–0.15ms 是负载下的调度与队列抖动（非代码可控）。
3. 剩余可动项只有 Python 固定开销 ~24µs（占 7%），且其中 `GetCursorPos`（原点）
   与落地确认不可删；再压需要动红线（取消落地确认 / 退回 SendInput 或虚拟设备），不做。

---

## 9. 剩余空间估算与「为什么剩下的时间不可再压」

| 组成 | 量级 | 能否压 |
| --- | --- | --- |
| 一次 DLL 批量提交（3–4 stroke） | 8–15µs | 否（进内核必须过一次 `interception_send`） |
| 驱动队列 → Raw Input 落地（down、up 各一条 stroke） | 110µs（p50） | 否：HID 按钮语义要求两个独立状态转换，不能合成一条 |
| 桌面光标位置更新（晚于 Raw 事件） | 300–600µs | **不在占用窗口内**（见 §6）；要当场验证就必须付，付了就比基线还慢 |
| Python 固定开销（入口/身份/坐标/观测器/记账） | ≈24µs | 边际（已从 44µs 降到 24µs） |
| 调度抖动（高负载窗口） | 100–150µs | 否（机器上有并行合并/OCR 任务） |

物理解释（一句话版）：
**一次点击至少要两次设备状态转换（按下、抬起），每次转换都要在真实 HID 设备的
mouclass 队列里排一次队（≈100µs 级），而"确认这次点击真的发生了"只能等这两条
Raw Input 事件到齐——这段等待既是安全语义，也是链路的物理地板。**
光标还原之所以看起来"还有 0.3ms 可省"，是因为它被排在同一条队列的末尾；
它的位置应用还要再滞后 0.3–0.6ms，且无事件可等——所以任何"当场确认还原"的实现
都会把这段滞后计入占用，比"把确认推迟到下一击/下一次观测"更慢。
在不取消落地确认、不退回虚拟设备/SendInput 的前提下，0.27–0.32ms 已是该链路的最优点。

---

## 10. 边界：如果「下一击」永远不来

问题：还原确认被推迟到下一击首段的 `_home_pending` 复核；若用户停脚本、或脚本在两次点击之间退出，
**最后一击的目标点还原有没有兜底？**

结论：**不需要兜底——还原本来就在同一批 stroke 里提交了，只是"复核"推迟，不是"还原"推迟。**
不存在"退出时统一归位"这条路径，也不需要它。

代码位置（[input.py](file:///C:/Users/26346/Desktop/project/dafors/perf/base/core/input.py#L943-L959)）：
`use_batch` 分支把 4 条 stroke 一次性交给 `drv.send_mouse_batch(...)`：

```python
strokes = [] if opt.get("drop_move") else [(0, dx, dy, flags)]   # 目标点 MOVE（可省）
strokes.append((MOUSE_LEFT_DOWN, dx, dy, flags))
strokes.append((MOUSE_LEFT_UP,   dx, dy, flags))
if use_drv_restore:
    strokes.append((0, ox, oy, flags))     # ← 回原点，同批、FIFO 最后生效
drv.send_mouse_batch(strokes, device=ident["device"])
```

还原块（[input.py](file:///C:/Users/26346/Desktop/project/dafors/perf/base/core/input.py#L1029-L1040)）只做一件事：
回读一次光标，命中即 `restored=True`；没命中记 `None`（已排队、未确认），把
`_home/_home_target/_home_pending` 留给下一击——**这是"记账"，不是"动作"**。

实测证据（`perf/out/_exit_child2.log`，进程在 10ms 后 `os._exit(0)`，全程没有任何第二次点击）：

```
A home (300, 300)
B mouse ready
C click returned, cursor_now (500, 500)   ← 返回瞬间光标还在目标点（位置更新仍在队列里）
D after 10ms no next click (300, 300)     ← 没有下一击，光标也已回到原点
```

即 `C → D` 这段窗口里没有发生任何新注入，光标仍回到 `(300,300)`，说明回原点是由本击第 4 条 stroke
完成的。反过来若还原依赖下一击，`D` 必然停在 `(500,500)`。

另外核实：`core/input.py` 中**没有** `def close` / `atexit` / `__del__` / `shutdown` 任何退出钩子
（全文只有 legacy 通道行 1168 与滚轮行 1541 的两处 `SetCursorPos`，与点击路径无关）。
所以"退出时统一归位"这一设计在代码里根本不存在，也不该加：进程被杀时钩子不一定跑，
而"同批提交"在任何死法下都已经生效。

残余风险（唯一一条，且已知）：若进程在 `send_mouse_batch` 返回后、第 4 条 stroke 被驱动处理前
被强杀，光标会停在目标点。这个窗口是**微秒级**（提交已返回，队列在驱动侧），且与"还原失败率"
无冲突——它属于进程被杀，不属于还原逻辑。要覆盖它只能把"归位"放到进程外，与本次优化目标无关。

---

## 11. 残留进程（收尾核查）

核查时间 2026-11-07 11:1x。本轮曾出现的 4 个 `click_perf` / 测试进程：

| PID | 启动时间 | 是什么 | 现状 |
| --- | --- | --- | --- |
| 14440 | 10:58:13 | `--mode drift --n 10000`（d2） | 已自行退出 |
| 35188 | 11:08:13 | `perf/out/_exit_click.py`（边界实测） | 已自行退出 |
| 36172 | 11:09:34 | `--mode burst`（b5） | 已自行退出 |
| **33004** | **11:10:26** | `--mode burst`（b6） | **仍在，杀不掉** |

PID 33004（`python.exe -u perf\bench\click_perf.py --mode burst`）：1 线程、CPU 0.5s（静止）、
WS 30MB。结果 JSON 早已落盘，**不再注入任何输入**。

终止尝试（当前 shell 为 Medium 完整性级别）：

```
Stop-Process -Id 33004 -Force        → 不报错但不生效（进程仍在）
Invoke-CimMethod Win32_Process Terminate → rc=2
OpenProcess(PROCESS_TERMINATE)       → 句柄有效，err=0
TerminateProcess                     → rc=0，err=5 (ERROR_ACCESS_DENIED)
```

即目标进程处于更高完整性级别，需管理员执行：

```powershell
Stop-Process -Id 33004 -Force      # 管理员 PowerShell
```

**占用与影响（已实测）**：
- 只持有**它自己**的 Interception 客户端上下文（`interception_create_context` 是每客户端独立的，
  多客户端可共存）+ 一个 `RIDEV_INPUTSINK` 隐藏窗口（只旁观、不吞输入，filter 全槽仍为 0）。
- 实测在它存活期间新建上下文并枚举设备**成功**（dev10 列表正常返回 device 14 / VID_3554&PID_50D），
  证明**驱动上下文不独占**；队列已排空、光标已归位（见 §10 同类实测）。
- 对主程序**无影响**：不抢设备、不抢前台（`SetForegroundWindow` 0 次）、不改 filter。
- 唯一副作用：多一个常驻 30MB 的 python 进程，长期留着会让后续真机验证的"进程列表"不干净。
  建议按上面一条命令清掉；清不掉也不影响功能与测量结论。
