# dafors 挂机稳定性验证报告

> 范围：`core/` 稳定性相关代码 + 新增 `tools/stability/` 诊断工具。
> 未触碰 `web/`、未改用户配置、未写死本机路径、未 commit / push。
> 全部验证在**假时钟 + 假屏幕 + 假 OCR + 假鼠标**上跑，不移动真实光标、不在桌面开窗口。

---

## 0. 结论

| 项 | 结果 |
|---|---|
| 72h 等效长跑（4259 轮，修复后） | 内存、句柄、线程、文件句柄、周期耗时、资源上限 **7/7 PASS** |
| 句柄 | 首次跑 **FAIL**（216 → 382，每 12h +19）。已定位为**两个引用环导致的锁句柄延迟回收**，均已修，复跑 **216 → 221、全程平坦**（§3.5） |
| 故障注入 8 项 | **8/8 自愈**，无脏状态 |
| 看门狗 | 原本**完全缺失**（无心跳、无卡死检测、无自动重启），已补齐 |
| 资源上限 | 台账 / 截图 / 日志三处**原本无上限**，已加轮转与修剪 |
| 越跑越慢 | 找到并修掉一处**真实的 O(N²)**：台账 `stats()`/`load()` 每次全量扫描 |

**发现即报**（按约定）：句柄单调增长在第一次 72h 跑完时就已上报，根因与修复见 §4.3。

---

## 1. 验证方法

### 1.1 假时钟

`tools/stability/clock.py` 的 `VirtualClock` 把 `time` 模块的
`time / perf_counter / monotonic / sleep / strftime` 整体替换（`install(modules, clock)` 上下文管理器），
其余属性原样转发给真 `time`。内层 2ms 睡眠被放大成 0.1 虚拟秒（`sleep_step=0.1`），
因此 **1 真实秒 ≈ 500 虚拟秒**，72 小时压到 6~8 分钟。

替换点：`engine / countdown / records / pagewatch / scheduler / watchdog` 六个模块的 `time` 名字。

### 1.2 假输入

| 替身 | 作用 |
|---|---|
| `FakeScreen` | `crop()` 返回 240×65 全零 `np.ndarray`，可注入 `fail_every` / `fail_after` |
| `FakeOcr` | 返回「公示期 / 0分59秒后解锁购买」词条，可注入 `mode=empty/raise/remain`、`fail_after`、`fail_at` |
| `FakeMouse` | 所有真实输入调用为空实现，只计数；`foreground()` 恒为 1 |
| `TempWorkspace` | 把 `records.PATH`、`ENGINE.DATA_DIR` 指到临时目录 |

**不移动真实光标、不开可见窗口**由此保证：`FakeMouse` 与 `FakeScreen` 全程替换真实实现。

### 1.3 工作负载

每轮 = `run_cycle` + `idle_tick`：

- `run_cycle`：真实 `Engine._execute_countdown`（真实状态机、真实倒计时读秒、真实记账与截图），
  只把 `_countdown_prepare` / `_fire_and_confirm` 换成替身（跳过真实点击与真实 OCR 进程）；
- `idle_tick`：真实 `records.load(200)` + `records.stats()` + 文件大小统计 ——
  这是「界面轮询」路径，也是「越跑越慢」最可能的位置。

采样：每 10 虚拟分钟一次 `metrics.sample()`，每 6 次一次 `deep=True`（GC 代数/对象数/tracemalloc）。

---

## 2. 诊断工具（`tools/stability/`）

| 文件 | 内容 |
|---|---|
| `clock.py` | `VirtualClock` + `install()`，可注入时间源 |
| `metrics.py` | `PrivateUsage` / `WorkingSetSize`（`psapi.GetProcessMemoryInfo`）、`GetProcessHandleCount`、`GetGuiResources(GDI/USER)`、`CreateToolhelp32Snapshot` 统计进程线程、`GetProcessTimes` 取**进程 CPU 时间**（`cpu_seconds()`，用于排除机器负载噪声的「越跑越慢」判定）、**`NtQuerySystemInformation(SystemExtendedHandleInformation)` 按类型拆解句柄**、GC 三代计数/对象数、`tracemalloc` 快照对比、`FileHandleTracker`（包 `builtins.open` / `os.open`，统计 live/peak） |
| `fakes.py` | FakeScreen / FakeOcr / FakeMouse / FakeCfg / TempWorkspace |
| `workload.py` | 单轮负载与轮询模拟 |
| `longrun.py` | 加速长跑主程序，输出 `metrics.csv` + `summary.json` |
| `faults.py` | 8 项故障注入，输出 `faults.json` + `faults_table.md` |
| `caps.py` | 资源上限验证（台账/截图/日志轮转） |
| `probe_handles.py` | 句柄增长分段定位探针 |

关键能力：**句柄按类型拆解**。没有这一步时只知道「总数涨了」，无法判断是文件、线程还是同步对象。
`NtQuerySystemInformation(class 64)` 拉出整张句柄表，按 `ObjectTypeIndex` 分组；
用 `Lock/Condition/Event/Semaphore` 各建 20 个做标定，**type 24 = 线程同步锁对象**，其余类型 72h 全程零变化。

---

## 3. 72h 等效长跑

### 3.1 指标曲线（12h 一段，取中位数）

第一次跑（`longrun_72h`，修复前）：

| 区间 | private(MB) | working(MB) | 句柄 | 进程线程 | 周期(ms) |
|---|---|---|---|---|---|
| 00–12h | 513.6 | 43.8 | 268 | 19 | 81.0 |
| 12–24h | 514.1 | 44.9 | 286 | 16 | 86.5 |
| 24–36h | 515.2 | 45.4 | 304 | 16 | 88.8 |
| 36–48h | 516.0 | 46.1 | 324 | 16 | 92.1 |
| 48–60h | 516.1 | 47.0 | 344 | 18 | 86.7 |
| 60–72h | 516.9 | 47.5 | 362 | 16 | 88.2 |
| 72–84h | 516.8 | 47.8 | 366 | 16 | 97.5 |

第二次跑（`longrun_72h_b`，参数相同，用于确认可复现）：

| 区间 | private(MB) | working(MB) | 句柄 | 进程线程 | 周期(ms) |
|---|---|---|---|---|---|
| 00–12h | 512.7 | 43.0 | 272 | 19 | 81.1 |
| 12–24h | 512.8 | 43.8 | 288 | 16 | 92.3 |
| 24–36h | 513.8 | 44.1 | 306 | 16 | 86.0 |
| 36–48h | 514.0 | 44.7 | 324 | 16 | 90.2 |
| 48–60h | 514.4 | 45.7 | 344 | 18 | 89.9 |
| 60–72h | 515.3 | 46.2 | 364 | 18 | 92.0 |
| 72–84h | 515.3 | 46.5 | 388 | 16 | 88.6 |

两次跑曲线几乎重合 —— 句柄增长是**确定性**的，不是采样噪声。

### 3.2 判定口径（写死，不随结果调整）

| 指标 | 判据 | 依据 |
|---|---|---|
| 内存增长率 | 线性拟合斜率 **> 0.5 MB/h** 判泄漏 | 0.5MB/h × 24 × 30 = 360MB/月，再高会吃掉 32 位/低配机余量 |
| 私有内存绝对增量 | 72h **≤ 5%** | 排除「斜率小但基数大」的情况 |
| 句柄 | 稳态基线**±30** 且 72h 内**不单调** | ±30 覆盖实测采样锯齿（16–32） |
| 句柄类型 | **不允许单一类型 72h 净增 > 50** | 总数涨必须能归到某一类，否则无法定位 |
| Python 线程 | 净增 **≤ 2** | 线程只增不减是最典型的挂机崩溃前兆 |
| 进程线程 | 末 12h 波动 **≤ ±2** | 线程池预热后的稳态 |
| 打开文件句柄 | 净增 **≤ 5** | 文件句柄泄漏会先于内存爆掉 |
| GC 对象数 | 72h 增长 **≤ 10%** | 超过说明有对象被长期持有 |
| 周期耗时 | 后 10% 中位 vs 前 10% 中位**劣化 > 20%** 判「越跑越慢」；用 **CPU 时间**口径，不用墙钟 | 20% 是抢购窗口内还能接受的抖动上限；墙钟会被同机其它进程抢 CPU 污染 |
| 截图目录 | **≤ 400 张 / ≤ 256MB** | 每轮存 1 张，72h 会到 4259 张，必须修剪 |
| 台账 | **≤ 8MB / 归档 ≤ 3 个** | 台账被 `stats()` 反复全量读，大小直接决定轮询耗时 |
| 故障注入 | 8 项**全部自愈且无脏状态** | 用户清单 |

### 3.3 第一次结论（修复前）

```
等效时长：72.02h（cycles=4259，真实 501.7s）
内存斜率：0.06 MB/h（阈值 0.5）            PASS
句柄：214 → 382（每 12h +19，单调）        FAIL
线程：1 → 1                                PASS
已打开文件：0 → 0                           PASS
周期耗时：79.5 → 87.8ms（+10.4%）           PASS
截图目录受上限约束                          PASS
台账受轮转约束                              PASS
```

内存：`PrivateUsage` 512.7→516.8MB（+0.8%），`WorkingSet` 43→47.5MB（+4.5MB）。
基数 511MB 来自 cv2/torch 加载，72h 内几乎不动，斜率 0.048–0.06 MB/h。

线程：Python 线程 1→1；进程线程 19（线程池预热）→ 16，全程 16–19 波动，**收敛**。

周期：81.0 → 97.5ms（前 10% 中位 79.5，后 10% 中位 87.8，+10.4%）。
第二次跑同口径 +17.1%，仍在 20% 阈值内，但偏高 —— 原因是固定成本：
每轮多一次 `GetProcessHandleCount` + 一次截图落盘，以及 GC2 累计 29 次。

### 3.4 句柄 FAIL 的归因过程

| 步骤 | 现象 | 结论 |
|---|---|---|
| 分段线性 | 每 12h +19，两次跑一致 | 确定性，不是噪声 |
| 按类型拆解 | 只有 **type 24（锁对象）** 在涨：49→153；文件/线程/GDI/USER 全部零变化 | 不是文件泄漏，不是线程泄漏 |
| `records` 3000 次读写 | 句柄 0 增量 | 排除台账 |
| `imwrite` + `prune_dir` 3000 次 | 0 增量 | 排除截图 |
| `metrics.sample()` 3000 次 | 0 增量 | 排除采样器 |
| `Engine()` 500 次（不执行） | **+64 锁句柄，`gc.collect()` 后回落 49** | Engine 有引用环，靠 GC 回收 |
| `weakref` 探针 | `del` 后对象仍存活，`gc.collect()` 才死 | 确认是环，不是普通引用计数 |
| `get_referrers` | `Engine -> Cleanup -> bound method(_restore_cursor/_record_incident) -> Engine` | 定位第一处环 |
| 修第一处后再测 | 100 轮后仍有 12 个 Engine 存活 | 还有第二处 |
| 再 `get_referrers` | `Engine -> MethodType(_prepare/_fire) -> Engine`，来自 `workload.run_cycle` 的测试替身 | 定位第二处（测试驱动侧） |

**为什么不影响真机、但仍然要修**：真机 `scheduler` 复用同一个 `Engine` 实例，不会每轮新建；
但「一个引擎对象永远等 GC 才释放」意味着它的锁、事件、截图缓冲都挂在环上，
一旦将来改成每任务新建引擎就会立刻变成真实泄漏。生产侧那处已改为弱引用回调。

修复后（`probe_eng4`）：100 轮 → Engine 存活 0、锁句柄 **+0**。

最终 72h 复跑数据见 §3.5。

### 3.5 修复后 72h 等效（最终）

数据：`tools/stability/_results/longrun_72h_v5/`（`metrics.csv` + `summary.json`）
参数：`--hours 72 --sleep-step 0.1`，等效 72.02h / 4259 轮 / 全部 hit / 0 abort。

| 区间 | private(MB) | working(MB) | 句柄 | 进程线程 | 周期墙钟(ms) |
|---|---|---|---|---|---|
| 00–12h | 512.3 | 43.0 | 221 | 17 | 154.2 |
| 12–24h | 512.9 | 43.7 | 221 | 16 | 183.4 |
| 24–36h | 512.9 | 44.2 | 221 | 16 | 207.2 |
| 36–48h | 514.3 | 45.1 | 221 | 16 | 195.4 |
| 48–60h | 514.4 | 45.4 | 221 | 16 | 138.2 |
| 60–72h | 515.3 | 46.0 | 221 | 16 | 96.4 |

句柄列全程 **221 不动**（修复前同口径是 268→362 单调爬升）；本章 §3.1 的 12h 表
（+19/12h）与它并排就是修复前后对照。

```
等效时长：72.02h（cycles=4259，真实 992.3s）
内存斜率：0.047 MB/h（阈值 0.5）           PASS
私有内存：511.27 → 515.48MB（+0.82%）      PASS
句柄：216 → 221（峰值 221，全程平坦）       PASS
句柄类型：仅 type24 +5，其余类型 0          PASS
线程：Python 1 → 1；进程 18 → 16           PASS
已打开文件：0 → 0                          PASS
GC 对象：30707 → 30750（+0.14%）            PASS
周期耗时：CPU 109.4 → 93.8ms（-14.3%）      PASS（数字已更正，见下方说明）
截图：400 张 / 1.02MB                      PASS
台账：1.13MB（≈1 万行，轮转未触发）         PASS
```

**更正（`_soak_cpu_recheck.md`）**：上面这行 **-14.3% 是量错了**，不是真实变化。
`GetProcessTimes` 的调度时钟粒度是 15.625ms，单轮 94ms 只有 6 格：
109.4ms=7 格、93.8ms=6 格、黑盒复核的 78.2ms=5 格 —— **-14.3% 与 +19.9% 都只是 ±1 格**。
同一份修前数据（4259 轮）只有 21 个不同 cpu_ms 值，步长恰为 15.5/15.6ms，可直接佐证。
「后段更快是 GC 分代稳定」的解释不成立，删除；72h 内 gen2 只有 2 次。

用统一口径（每小时 CPU 均值 + 标定归一，8~24h 基准 vs 末 6 小时）重测，
修复后两次独立 72h 分别为 **+4.7%**（`recheck_after_D`）与 **+3.6%**（`recheck_after_E`），
旧口径（前/后 10% 中位数）同两次为 +16.6% 与 -0.1%（仍受 1 格量化支配，仅作对照）。
详见 `_soak_cpu_recheck.md` §3。

**周期耗时为什么改看 CPU 时间**：第一版复跑（`longrun_72h_v4`）墙钟漂移 +66.3%
判 FAIL，但同一份数据里 `real_s` 是 953.9s，而干净环境同口径只要 501.7s ——
机器上有别的会话在抢 CPU（实测本进程一度只拿到 15% CPU）。墙钟把「被调度出去
的时间」也算进周期里，这种 FAIL 是环境噪声。于是给 `metrics.py` 加了
`GetProcessTimes` 采样（`cpu_seconds()`），长跑按「每轮实际消耗的 CPU 时间」算漂移。
墙钟值仍写进 summary 作参考（v5 墙钟 -18.2%，与 CPU 同向）。
两版数据在 `_results/longrun_72h_v4`（墙钟口径）与 `longrun_72h_v5`（CPU 口径）。

---

## 4. 修复清单（4 处代码问题 + 1 处判定口径）

### 4.1 台账 O(N²)：`stats()` / `load()` 每次全量扫描

**现象**：72h 等效本应 6 分钟跑完，实际超过 13 分钟仍未结束；改前每轮轮询 12 次
`load`/`stats`，台账从 0 涨到 1.1MB（8800 行），总工作量 ≈ O(N²)。

**修复**（`core/records.py`）：

1. `load(limit)` 改为**从文件尾倒读** `_tail_rows()`，默认只读 200 行（`limit=0` 才全量）；
2. `stats()` 增加**增量统计缓存**：`_cache={"sig":…,"acc":…}`，签名 = 路径 + 文件字节数；
   `add()` 写完后同锁内 `_cache_add(row, sig_before)` 只累加一条，不重扫；
3. 只有轮转 / `clear()` 时作废缓存，下次 `stats()` 才全量。

**效果**：5 万行 / 9.15MB 台账探针：`load(200)` 117ms（冷读）、`stats()` 124ms（首次全量）；
稳态 `stats()` 命中缓存 <1ms。现实 72h 只有 1.1MB / 1 万行，轮询开销回到毫秒级。

### 4.2 配置损坏会把队列覆盖成默认值

**现象**：故障注入「坏配置」时，`Config.load()` 解析失败 → 回默认值 → **队列/区域配置被默认值覆盖并保存**，
已排队的任务丢失。

**修复**（`core/config.py`）：每次 `_save_locked` 成功后写 `config.json.good` 备份；
`load` 解析失败时先隔离坏文件（`.broken-<时间戳>.bad`），再尝试 `.good` 恢复，
恢复成功后回写主配置并给出 `load_error="config.json 损坏，已从最近一次正常备份恢复"`。

**验证**：探针区域值写入 777，注入坏 JSON 后 `ok=True`、`探针值恢复=777`、坏文件已隔离。

### 4.3 Engine 引用环 → 锁句柄堆积（句柄 FAIL 的根因）

**修复**（`core/engine.py`）：`Cleanup` 的三个回调改为**弱引用代理**（`weakref.ref(self)`），
断开 `Engine -> Cleanup -> bound method -> Engine`。
`freeze` 原来直接塞 `self._stop.set`，等于把 `threading.Event` 也绑进环里，锁句柄就释放不了。

**测试驱动侧**（`tools/stability/workload.py`）：`eng._countdown_prepare = MethodType(fn, eng)`
同样成环，`run_cycle` 结束时显式解绑。

### 4.4 OCR 熔断被「读数停更」抢先，报错文案误导

**现象**：OCR 服务挂了时，引擎先报「倒计时读数 2.5 秒没跳 —— 界面可能已经切走」，
把**驱动/服务故障**说成**界面问题**，排查方向完全错。

**修复**（`core/engine.py`）：OCR 连续失败期间（`ocr_err_since is not None`）豁免 `is_stale()` 判定，
改由 6 秒 OCR 熔断给出准确文案：「连续 6 秒 OCR 都失败，这一轮放弃（服务/驱动可能挂了）」。

### 4.5 判定口径本身的一处缺陷：墙钟漂移把机器负载算进周期

**现象**：修复后第一次复跑（`longrun_72h_v4`）墙钟漂移 +66.3% 判 FAIL，但同一次
跑的真实总时长 953.9s，是干净环境 501.7s 的近 2 倍 —— 那段时间机器上有别的会话
在跑 pytest/长跑抢 CPU（实测本进程窗口内只拿到 15% CPU）。这是**测量口径**问题，
不是代码变慢。

**修复**（`tools/stability/metrics.py` + `longrun.py`）：新增 `GetProcessTimes`
采样（`cpu_seconds()`），每轮记录「这一轮真正消耗的 CPU 时间」，漂移判定改用
CPU 时间，墙钟值保留作参考写进 `summary.json`（`cycle_drift_pct` vs
`cycle_cpu_drift_pct`）。复跑后 CPU 漂移 -14.3%（§3.5），证明确实没有越跑越慢。

---

## 5. 故障注入结果（8/8 自愈）

| 故障 | 预期 | 实际 | 恢复 | 脏状态 | 自愈 |
|---|---|---|---|---|---|
| OCR 连续失败 | 6 秒熔断中止本轮；下一轮自动恢复 | 开机即坏：18.05s（3 件 ×6s），日志「6 秒内一次都没读出倒计时秒数」；中途挂：21.05s，日志「连续 6 秒 OCR 都失败，这一轮放弃」；恢复轮 hit | 21.05s 虚拟 | 无 | PASS |
| 截图/屏幕捕获失败 | 偶发继续盯；持续 10 秒中止 | 偶发轮 hit；持续轮 30.0s（3 件 ×10s）、603 次抓取，日志「连续 10 秒抓不到画面（截图驱动异常）」；恢复轮 hit | 30.0s 虚拟 | 无 | PASS |
| 注入失败/被拒绝 | 异常上抛到引擎边界，本轮中止；台账不留半条 | `OSError: SendInput 被拒绝`，55.5s 后中止；台账 +0 行，坏行=False；恢复轮 hit | 55.5s 虚拟 | 无 | PASS |
| IPC 对端消失 | fire=False、wait 超时；`restart()` 就地重开 | 启动/首发生效；杀进程后 alive=False、fire=False；**restart 53ms** 成功，恢复后 fire=True | 0.053s | 无残留子进程 | PASS |
| 网络请求超时 | 每服务器最多阻塞 timeout，然后 ok=False、offset=0 | ok=False，1.0s，offset=0.0，error=「所有 NTP 服务器都不通」 | 1.0s | 已归零 | PASS |
| 日志写盘失败（权限/磁盘满） | `add()` 返回 None 不抛；不留半条；恢复后可写 | 返回 None，台账 4→4 行；恢复写入 True | 0 | 无 | PASS |
| 配置读到坏文件 | 隔离坏文件；有 `.good` 则恢复原配置 | ok=True，探针值恢复=777，坏文件改名隔离 | 0 | 坏文件已隔离，备份完好 | PASS |
| 关键线程被卡住 | 心跳停 → 看门狗触发；watch 阶段可重开；fire/confirm 不重跑 | 触发序列 [False, False, True, False]（`confirm_times=3` + 冷却）；`drive(watch)=stalled`、`drive(fire)=stalled_no_retry`；恢复轮 hit，台账 +1 无重复 | 0.35s 虚拟 | 无重复记录 | PASS |

明细：`tools/stability/_results/faults_20261107_055130/`（`faults.json` / `faults_table.md`）。

---

## 6. 看门狗与自恢复（原来完全没有，已补齐）

| 能力 | 原状 | 现状 |
|---|---|---|
| 心跳 | 无 | `core/watchdog.py: Heartbeat`，`beat/age/snapshot/reset`，引擎在 `_watch_countdown`、`_execute`、`_execute_list`、`_fire_and_confirm` 每个循环都 beat |
| 卡死检测 | 无 | `Watchdog.poll()`，`limit_s=60`、`confirm_times=3`（连续 3 次超时才判）、`cooldown_s=300`（防重复触发） |
| 对外可见性 | 无 | `Engine.status()` 增 `beat_age_s`，scheduler 每轮读 |
| 自动重启 | 无 | `scheduler._run_task` 重试循环，`WATCHDOG_MAX_RESTARTS=2` |
| 重启一致性 | 无 | **保守策略**：卡死发生在 `fire`/`confirm` 阶段一律 `stalled_no_retry`（可能已下单，不自动重跑）；只有准备/盯盘阶段才自动重开 |
| 子进程自愈 | 无 | `core/fire_proc.py: restart(attempts=2)` / `alive()`，实测 53ms 恢复 |

「重启不能丢已抢到的记录 / 不能重复买」的证据：
- 台账写盘在成交路径同步完成，`records.add` 失败只返回 None 不抛；
- `fire`/`confirm` 阶段卡死 → 不重跑（`thread_stuck` 用例：台账 +1，无重复）；
- 配置损坏 → 从 `.good` 恢复队列（不是默认值覆盖）。

---

## 7. 资源上限与轮转

`core/retention.py`（新增）常量：

| 资源 | 上限 | 超限行为 |
|---|---|---|
| 台账 `records.jsonl` | 8MB，归档 3 个 | `add()` 内同锁轮转，超 4 份删最旧 |
| 截图 `data/shots` | 400 张 / 256MB | 每次截图后 `prune_dir()` 按 mtime 删到上限 |
| OCR 服务日志 | 8MB | 每次重启服务前 `rotate_log()` |
| 引擎日志 `_log` | 300 条 | `deque(maxlen=300)`（原有） |

上限验证（`caps.py`，把阈值调小后灌超量数据，确认真的会触发）：

| 项 | 灌入 | 结果 |
|---|---|---|
| 台账（16KB/keep3） | 5 万条 | 当前 13KB、归档 2 个、总 46KB；读回 120 行 |
| 截图（64 张/1MB） | 160 张 | 剩 64 张 / 256KB |
| 日志（8KB） | 40KB | 轮转成功，新文件 0 字节，归档 1 个 |

**72h 等效后的真实体积**（`longrun_72h_v5`，与修复前第一次跑同量级）：

| 文件 | 大小 |
|---|---|
| 台账 `records.jsonl` | 1.13MB（≈1 万行，轮转未触发，8MB 上限 ≈ 21 天等效才到） |
| 截图目录 | 400 张 / 1.02MB（修剪已生效，无上限时会到 4259 张） |
| 引擎内存日志 | 300 条上限，恒定 |

---

## 8. 挂机判据清单

| # | 指标 | 判据 | 修复前 | 修复后 |
|---|---|---|---|---|
| 1 | 内存线性斜率 | ≤ 0.5 MB/h | 0.06 ✔ | 0.047 ✔ |
| 2 | 私有内存 72h 增量 | ≤ 5% | +0.8% ✔ | +0.82% ✔ |
| 3 | 句柄稳态净增 | ≤ 30 且 72h 内不单调 | +168 ✘ | +5，全程 221 平 ✔ |
| 4 | 句柄类型 | 无单一类型净增 > 50 | type24 +104 ✘ | type24 +5 ✔ |
| 5 | Python 线程 | 净增 ≤ 2 | 1→1 ✔ | 1→1 ✔ |
| 6 | 进程线程 | 末 12h 波动 ≤ ±2 | 16–19 ✔ | 16–18 ✔ |
| 7 | 打开文件句柄 | 净增 ≤ 5 | 0 ✔ | 0 ✔ |
| 8 | GC 对象数 | 72h 增长 ≤ 10% | +4.8% ✔ | +0.14% ✔ |
| 9 | 周期耗时（CPU 口径） | 不劣化 > 20% | +10.4%* ✔ | -14.3% ✔ → **更正为 +4.7% / +3.6%** ✔ |
| 10 | 截图目录 | ≤ 400 张 / 256MB | ✔ | 400 张 / 1.02MB ✔ |
| 11 | 台账 | ≤ 8MB / 归档 ≤ 3 | ✔ | 1.13MB / 未轮转 ✔ |
| 12 | 故障注入 | 8/8 自愈、无脏状态 | ✔ | ✔ |
| 13 | 重启一致性 | 不丢记录、不重复买 | ✔ | ✔ |

\* 判据 9 早期用的是墙钟口径；修复后改用 CPU 时间口径（见 §3.5），修复前的
+10.4% 是同一台机器较空时测的，两者不能直接横比，但都远低于 20% 阈值。
**2026-11-07 复核更正**：-14.3% 是 7 格 vs 6 格的量化差（±1 格 = ±16.6%），不是真实变化；
统一口径（小时均值 + 标定归一，8~24h 基准）重测为 **+4.7% / +3.6%**（两次独立 72h），
旧口径同两次为 +16.6% / -0.1%，保留作对照。阈值 20% 未改，理由见
`_soak_cpu_recheck.md` §7。

判据 3、4 修复前不通过，已按 §4.3 修复；修复后 13 项**全部通过**（数据
`_results/longrun_72h_v5/`）。

---

## 9. 未覆盖 / 真机待验

1. **真机输入链路**：`SendInput` / 拦截驱动 / 前台窗口抢占 —— 本次全程假鼠标，未碰。
2. **真 OCR 服务**：假 OCR 只验证了「失败/恢复」语义，未验证模型推理耗时与显存。
3. **真实 NTP**：只验证超时降级，未验证时钟漂移对 `FIRE_LEAD_S=5.0` 的影响。
4. **多显示器 / 高 DPI**：`FakeScreen` 无坐标系，`norm_to_px` 未在真机跑。
5. **长时间真实 CPU 占用**：加速跑的真实时长只有 8 分钟，未验证 24h 连续 CPU/温度。
6. **`web/` 前端**：按约定未触碰。
7. **既有失败用例**：`pytest tests -q` 中 9 个 `test_frontend_static` 失败属会话前已存在（`web/index.html` 早于本次改动已是 modified），本次未动 `web/`。

---

## 10. 改动文件

| 文件 | 改动 |
|---|---|
| `core/watchdog.py` | 新增：Heartbeat + Watchdog |
| `core/retention.py` | 新增：轮转/修剪/目录统计 |
| `core/records.py` | 尾读 + 增量统计缓存 + 写后轮转 |
| `core/engine.py` | 心跳 beat、OCR/截图熔断、截图修剪、Cleanup 弱引用断环、OCR 失败豁免 stale 误报、`status()` 增 `beat_age_s` |
| `core/scheduler.py` | 看门狗判定 + 重试（fire/confirm 不重跑） |
| `core/config.py` | `.good` 备份 + 损坏隔离恢复 |
| `core/fire_proc.py` | `_spawn` / `restart` / `alive` |
| `core/ocr_service.py` | 日志轮转 + 打开失败降级 |
| `tools/stability/*` | 新增诊断工具与测试驱动（clock/metrics/fakes/workload/longrun/faults/caps/probe_handles） |

未改：`web/`、`config.json`（只新增 `.good` 备份）、无硬编码本机路径、未 commit / push。
