# 分支 B：卡点调度内核 `core/precision.py`

> 只回答一个问题：**给定一个精确目标时刻（QPC tick），怎样在那个时刻触发一次回调。**
> 不碰业务逻辑，不含任何注入实现，不移动真实光标，不创建任何可见窗口。

测量机：Windows（16 逻辑核），`QueryPerformanceFrequency = 10 000 000`（1 tick = 100 ns），
系统计时器分辨率 1.00 ms。全部实验用高精度时钟打点，**没有一次真实鼠标移动**。

---

## 1. 接口

```python
from core import precision as P

shot = P.fire_at(target_ticks, action,
                 lead_s=0.0, lead_ticks=0,        # 提前量：在 target - lead 触发
                 spin_s=0.003,                    # 自旋阈值（秒）
                 timer_ms=1,                      # timeBeginPeriod(1)，None = 不碰
                 priority=THREAD_PRIORITY_TIME_CRITICAL,  # None = 不改优先级
                 cancel=None)                     # Event 或返回 bool 的可调用对象
shot.wait()          # 或 shot.wait(1.0)
```

| 入口 | 用途 |
|---|---|
| `fire_at(target_ticks, action, **kw)` | 后台线程排队，返回 `Shot`。最常用 |
| `fire_in(delay_s, action, **kw)` | 相对现在 + delay 触发 |
| `fire_sync(target_ticks, action, **kw)` | 在当前线程阻塞到点触发（用于独占进程） |
| `now_ticks()` / `seconds_to_ticks(s)` / `ticks_to_seconds(t)` | QPC 工具 |
| `timer_resolution(ms)` | 上下文管理器，引用计数，结束还原 |

`Shot` 关键字段（全部是实测值，测试与报告直接引用）：

| 字段 / 属性 | 含义 |
|---|---|
| `target_ticks` / `lead_ticks` / `deadline_ticks` | 目标 / 提前量 / 实际等待的点 = target − lead |
| `trigger_ticks` | 自旋判据命中那一刻的 QPC 读数 |
| `action_start_ticks` | **回调入口**的 QPC 读数 |
| `deviation_us` | `trigger − deadline`（内核自身误差） |
| `call_latency_us` | `action_start − deadline`：**「目标时刻 → 回调被调用」这段抖动** |
| `call_latency_from_target_us` | `action_start − target`（字面口径，带提前量时约等于 −lead） |
| `action_s` | 回调本身耗时 |
| `fired` / `cancelled` / `error_text` | 结果；回调异常被捕获，不会抛到调用线程 |
| `priority_before / during / after` | 优先级提升与还原的实证（测试断言 during==15、after==0） |
| `spin_loops` / `sleep_chunks` | 自旋圈数、远段 sleep 次数 |

**触发时刻与注入彻底解耦**：`action` 是外部传入的无参回调。上层想用哪种注入
（外部进程注入、驱动、虚拟设备、SendInput……）都只改回调，内核一行不动。

执行模型：远段 `time.sleep`（每片 ≤ 50 ms，保证取消能在 120 ms 内生效）→
剩余 ≤ `spin_s` 转纯 QPC 自旋（循环里只有「读 QPC / 比较 / 查取消」三件事）→
命中后立刻读一次 QPC 并调用回调 → `finally` 还原线程优先级与计时器精度。

---

## 2. 「目标时刻 → 回调被调用」的抖动分布（核心指标）

这是整条卡点链里**本模块唯一能控制的一段**：判据命中到回调真正进入，中间只隔
一次 QPC 读 + 一次函数调用。等待的剩余时间不参与统计。

### 2.1 空闲机，200 枪（`bench_precision.py --external --shots 200`，spin=3ms，TIME_CRITICAL）

| 通道 | n | p50 | p95 | max | std | 早/晚 |
|---|---|---|---|---|---|---|
| **M0 回调入口 − deadline** | 200 | **2.10 us** | **3.40 us** | 25.00 us | 1.78 us | 0 / 200 |
| M1 判据命中 − deadline | 200 | 0.20 us | 0.50 us | 22.40 us | 1.62 us | 0 / 200 |
| M2 同刻两钟差 | 200 | 1.90 us | 3.40 us | 5.20 us | 0.97 us | — |
| M4 独立进程看见 − deadline | 200 | 15.60 us | 27.10 us | 85.70 us | 6.98 us | 0 / 200 |
| 其中：进程间可见延迟 handoff | 200 | 10.45 us | 17.90 us | 75.20 us | 5.75 us | — |
| M4 − handoff（纯观测开销） | 200 | 5.20 us | 8.90 us | 29.00 us | 2.33 us | — |

M0 直方图（单位 us，bin 左闭右开）：

```
[0,0.5)   0
[0.5,1)   0
[1,2)    75
[2,5)   121
[5,10)    3
[25,50)   1        <- 一次系统级抖动
其余      0
```

M1 直方图：`[0,0.5)=186  [0.5,1)=12  [5,10)=1  [10,25)=1`，其余 0。

**读法**：M0 比 M1 多出的 ~1.9 us 就是「判据命中 → 回调入口」的固定成本
（一次 ctypes QPC 往返 + Python 帧），抖动本身只有 ±0.3 us。**这段可以忽略不计，
真正的误差全在「什么时候拿到 CPU」上。**

### 2.2 回归测试里的 200 枪（`pytest tests/test_precision.py -s`，延迟 8–20 ms 随机）

| 通道 | n | p50 | p95 | max | std |
|---|---|---|---|---|---|
| **M0 回调入口 − deadline** | 200 | **1.90 us** | **2.90 us** | 49.40 us | 3.70 us |
| M1 判据命中 − deadline | 200 | 0.10 us | 0.40 us | 47.40 us | 3.64 us |
| M4 独立进程看见 | 200 | 10.50 us | 24.10 us | 106.30 us | 11.33 us |
| handoff | 200 | 6.95 us | 13.80 us | 102.80 us | 10.74 us |
| M4 − M1 | 200 | 10.30 us | 20.00 us | 106.20 us | 10.83 us |

M0 直方图：`[1,2)=108  [2,5)=90  [10,25)=1  [25,50)=1`
M1 直方图：`[0,0.5)=194  [0.5,1)=4  [10,25)=1  [25,50)=1`

### 2.3 双路交叉验证（两套独立测量，不是自证）

| 路 | 做法 | 为什么算独立 |
|---|---|---|
| **M1 / M0** | 触发线程内直接读 QPC | 权威值（kernel 侧） |
| **M4** | **另一个进程**忙等共享内存，看见 fired 标志后用自己的 `GetSystemTimePreciseAsFileTime` 打点 | 另一个时钟 API + 另一个进程，绕开 GIL |

关键点：`FILETIME` 底层由 QPC 换算，**同进程内的「第二套时钟」是伪独立**（实测两者
轴间还有 ~1 µs/s 的缓慢漂移），所以第二路必须换进程。逐枪用括取锚点
（q1 → FILETIME → q2 取中点）消除单边读偏置。

**差异 > 15% 的单独说明**：p50 相对差 99%（0.10 vs 10.50 us）、p95 98%、std 68%、
max 55%。原因不是两路吵架，而是 M4 天然晚一个**固定传递量**：写标志 → 缓存可见 →
子进程忙等到那一轮 → 读自己的钟。这段 handoff 逐枪测到 p50 6.95–10.45 us。
证据是 `M4 − M1` 的 p50=10.30 us、std=10.83 us —— 两路测的是同一个事件，差的是
固定开销，时钟轴没有分歧。**微秒级结论只用 M0/M1；M4 只作「确实在这个时刻发生了」
的独立见证。**

---

## 3. 不同负载下的表现

同一台机、spin=3 ms、priority=TIME_CRITICAL，每档 60–200 枪：

| 负载 | M0 p50 | M0 p95 | M0 max | M0 std | 结论 |
|---|---|---|---|---|---|
| 空闲 | **2.1 us** | 3.4 us | 25 us | 1.8 us | 达标 |
| 1 个 Python 忙等线程（争 GIL） | 2 830 us | 11 501 us | 15 141 us | 3 520 us | 崩到毫秒 |
| 8 个 Python 忙等线程（争 GIL） | 111 592 us | 266 374 us | 404 808 us | 72 584 us | 崩到百毫秒 |
| 16 个忙等子进程（占满 CPU，无 GIL） | 10 581 us | 42 221 us | 61 112 us | 13 903 us | 尾部崩 |
| 16 个忙等子进程 + **内核跑在独立进程** | **0.2 us** | 19.6 us | 69.2 us | 11.1 us | 中位数完好 |

### 3.1 决定性对照：同进程 vs 独立进程（16 进程满载，60 枪）

```
同进程  fire_at  （priority_during=15） p50=12 644 us  p95=29 561 us  max=49 377 us
独立进程 fire_sync（priority_during=15） p50=    0.2 us p95=19 600 us max=    69 us
```

两边优先级都确认提到了 15，差的只是「进程里有没有别的 Python 线程」。
**满载下 12.6 ms 的误差不是 CPU 不够，是 GIL 抢不到。** 这也解释了为什么
`THREAD_PRIORITY_TIME_CRITICAL` 在满载下只把 p50 从 17 ms 拉到 11 ms ——
OS 优先级管不了 Python 解释器锁。

### 3.2 优先级扫描（16 进程满载，60 枪）

| 优先级 | p50 | p95 | max | std |
|---|---|---|---|---|
| normal (0) | 17 180 us | 38 883 us | 57 820 us | 12 638 us |
| highest (2) | 17 316 us | 34 487 us | 38 592 us | 10 850 us |
| time_critical (15) | **11 147 us** | 41 071 us | 45 458 us | 11 866 us |

优先级有效（p50 降 35%、max 降 21%），但不能救 GIL。

### 3.3 满载 + 加大自旋窗口（60 枪，TIME_CRITICAL）

| spin | p50 | p95 | max |
|---|---|---|---|
| 0.5 ms | 13 623 us | 32 509 us | 55 910 us |
| 3 ms | 14 160 us | 34 236 us | 37 781 us |
| 10 ms | 9 801 us | 24 801 us | 32 650 us |
| 20 ms | 13 139 us | 26 925 us | 35 417 us |
| 40 ms | 13 732 us | 31 208 us | 43 172 us |

**加大自旋窗口对满载无效。** 误差不是「sleep 醒得晚」，是「醒了也拿不到 GIL」。
别再往这条路加码。

### 3.4 自旋阈值扫描（空闲，每档 100 枪）

| spin | p50 | p95 | max | std |
|---|---|---|---|---|
| 0.5 ms | 0.20 us | 27.20 us | 38.20 us | 8.47 us |
| 1 ms | 0.20 us | 0.40 us | 4.30 us | 0.45 us |
| 2 ms | 0.10 us | 0.50 us | 33.20 us | 3.33 us |
| 3 ms | 0.10 us | 0.60 us | 21.90 us | 3.13 us |
| 5 ms | 0.10 us | 0.40 us | 53.90 us | 5.35 us |
| 10 ms | 0.10 us | 0.30 us | 0.40 us | 0.11 us |

p50 在 ≥1 ms 后全部落在 0.1–0.2 us。0.5 ms 档 p95 明显劣化（27 us）：最后一段
sleep 受 1 ms 计时器粒度影响，醒来时已经越过 spin 入口。最大值差异是系统中断/DPC
的随机性，与 spin 长短不呈单调关系。

---

## 4. 精度下界

| 口径 | 实测下界 | 说明 |
|---|---|---|
| 判据命中（M1） | **p50 0.1–0.2 us，p95 0.4–0.6 us** | 理论地板 = QPC 分辨率 100 ns + 一次 QPC 读（约 0.1–0.3 us） |
| **回调被调用（M0，本次交付指标）** | **p50 ≈ 2 us，p95 ≈ 3 us** | 多一次 QPC 读 + Python 帧开销；抖动本身 < 1 us |
| 独立进程外部见证（M4） | p50 10–16 us | 含 7–10 us 进程间可见延迟，属于测量开销，不是内核误差 |
| 空闲机最大值 | 20–50 us | 单次系统级抖动（中断、DPC、其它进程），非确定性 |

**结论：本内核在空闲机上把「目标 → 回调」压到 p50 2 µs / p95 3 µs，比 1 ms 计时器
粒度小三个数量级。** 对「倒计时 0 秒点击」这个量级（人类与游戏处理链都在毫秒以上），
这已经是噪声底。

### 影响因素（按破坏力排序）

1. **同进程其它 Python 线程 / GIL**：破坏力最大，能把 p50 从 2 us 抬到 10 ms–100 ms。
   `TIME_CRITICAL` 对 GIL 无效。**必须让卡点线程独占进程。**
2. **CPU 饱和（其它进程占满核）**：独立进程下中位数仍 0.2 us，但 p95 到 20 us、
   max 到 70 us，因为线程可能被调度器暂时移出。
3. **电源计划 / CPU 频率与 C-state**：QPC 频率恒定、不受调频影响，但从深 C-state
   唤醒的延迟会直接变成尾部误差。高性能/卓越性能计划、关闭核心停泊更稳。
4. **计时器分辨率与 timer coalescing**：`timeBeginPeriod(1)` 只影响远段 sleep；
   自旋段不受它约束。省电模式下的定时器合并会把唤醒推迟到数个 ms。
5. **系统中断 / DPC / SMT 兄弟核**：造成 20–100 us 级别的偶发尾部，无法在用户态消除。
6. **QPC 分辨率**：10 MHz = 100 ns，已是硬地板；个别平台 QPC 可能不单调，
   本模块用「≥ deadline 就触发」的单向判据，不做回退。
7. **注入链路（不在本模块内）**：注入排队、游戏帧边界、前台进程完整性级别等
   延迟**未验证**，必须由上层用 `lead_s` 实测标定。

---

## 5. 推荐配置

| 项 | 推荐值 | 依据 |
|---|---|---|
| `spin_s` | **0.003 s（3 ms）** | ≥1 ms 后 p95 已 sub-us；3 ms 兼顾取消响应（自旋段 <20 ms 退出）与对同核渲染/输入的干扰；10 ms 收益极小却明显增加卡顿风险 |
| `priority` | **`THREAD_PRIORITY_TIME_CRITICAL`** | 只在最后 spin 窗口生效，触发后 `finally` 立即还原（测试断言 before/after 均为 0，during 为 15） |
| `timer_ms` | **1** | 远段 sleep 分片需要 1 ms 粒度；引用计数配对，最后一个 Shot 结束才还原 |
| `sleep_slice_s` | 0.05 | 保证取消最迟 ~120 ms 生效（实测 <120 ms） |
| 架构 | **卡点进程独立** | 满载对照：同进程 p50 12.6 ms，独立进程 p50 0.2 us。OCR/业务逻辑留在主进程，只把 `target_ticks` 通过 IPC 送过来（QPC 全系统同轴，可直接跨进程传） |
| `lead_s` | 由上层标定 | 本模块只负责「到点触发」；注入固定延迟必须实测后填进提前量 |

线程优先级对系统的影响：`TIME_CRITICAL(15)` 在自旋的数毫秒内优先于同核普通线程，
DWM/输入/渲染可能被压住数百微秒到数毫秒；只在极短窗口使用，且必须还原。
不会、也不需要提成 REALTIME 进程类。

---

## 6. 验证状态与约束

- **已验证**：接口（单次触发、提前量、取消、阈值/优先级可配）、18 项测试全绿
  （`18 passed`）、200 枪偏差分布（含直方图）、双路交叉验证、四类负载对照、
  跨进程对照、优先级/自旋扫描。
- **未验证**：真实注入链路的端到端延迟（注入实现不在本分支）、游戏前台/
  完整性级别对注入的影响、不同电源计划下的尾部数据（只测了当前机器默认计划）。
- **约束遵守**：全程无真实光标移动、无可见窗口（观测子进程用 `CREATE_NO_WINDOW`，
  已用 `EnumWindows` 前后对比确认为零新窗口）、无 git commit / push、
  未改动 `core/input.py`、`core/interception.py`、`core/engine.py`、`core/scheduler.py`、
  `core/ocr*.py`、`config.yaml`。

复现命令：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_precision.py -q -s
.venv\Scripts\python.exe tools\bench_precision.py --external --shots 200
.venv\Scripts\python.exe tools\bench_precision.py --spin-sweep --shots 100
.venv\Scripts\python.exe tools\bench_precision.py --priority-sweep --load procs --shots 60
.venv\Scripts\python.exe tools\bench_precision.py --load single --shots 120
.venv\Scripts\python.exe tools\bench_precision.py --load procs --shots 120
```
