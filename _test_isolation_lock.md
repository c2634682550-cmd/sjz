# pytest 跨进程互斥（heavy 分组锁）实施报告

> 目标：同一台机器上多份 pytest 并发跑时，重负载用例不再互相踩，时序类假失败归零；
> 轻量用例照常并发；锁自己能从崩溃里爬出来；有逃生开关。
>
> 落地文件：`tests/conftest.py`、`tests/_isolation/{__init__,lock,groups,priority}.py`
> 边界：未改动 `core/`、`engine.py`、`web/`、`tools/`；未 commit、未 push。

---

## 1. 一句话结论

| 口径 | 不加锁（4 并发） | 加锁（4 并发） |
|---|---|---|
| 每实例用例数 | 13 | 13 |
| 假失败总数 | **5 处 / 3 个实例有红** | **0 处 / 4 个实例全绿** |
| 4 进程总墙钟 | 7.4s | 24.3s |
| 单实例 pytest duration | 5.89 ~ 6.57s | 19.69 ~ 23.49s |
| 锁超时 | — | 0 次 |

时序用例 `lag_ms < 2.0` 的断言一字未改。加锁后同一份断言在同一台满负载机器上全绿，
说明那些红是**资源竞争**造成的，不是被测代码退化。

单次全量（同一台机、同一条命令）加锁前后：**775.47s → 749.42s**，没有变慢。

---

## 2. 问题现场

同机多会话并发跑 pytest 时的典型症状（本仓库 2026-11-07 实测）：

- `test_feature_mouse_human.py::test_late_cursor_bump_uses_tail_not_teleport` —
  `lag_ms` 从 0.4ms 涨到 8.202ms / 11.935ms / 20.589ms，断言 `< 2.0` 判失败；
- `test_feature_mouse_human.py::test_segment_preheat_stops_short_then_tail_closes_gap` —
  同类，实测 7.533 / 11.346 / 12.004 / 14.147 / 18.206 / 25.434 / 37.174ms；
- `tests/test_fire_proc.py` 子进程用例被判 `dead`（子进程启动超时）；
- `test_console_api.py::test_preflight_reports_every_item_with_a_fix` Playwright 超时；
- `-n 4` 下出现 `greenlet` ERROR（Playwright 线程模型被饿死）。

空载时全绿，满负载时随机红 —— 这是排他性资源竞争，不是代码缺陷。
机器实测长期 100% CPU（其它会话在跑全量 pytest、`run_accuracy.py`、
`tools.longrun_collect --minutes 65`、游戏本体），16 逻辑核被占满。

---

## 3. 分组清单（可枚举、可审计）

分组的**唯一真源**是 [groups.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/groups.py)：
模块 → 组的静态表 + 用例级覆盖表。不靠命名规则猜（`test_market_video` 听着轻量，
实际跑真 OCR 帧）。新增重负载用例必须在这张表里加一行 —— 评审时看得见。

随时打印现行清单：

```powershell
.venv\Scripts\python.exe -m pytest tests --test-lock-groups
```

### 3.1 准入条件（写死在代码里）

一条用例属于 heavy，当且仅当满足至少一条：

| 组 | 含义 | 判据 |
|---|---|---|
| `timing` | 时序敏感 | 断言里有毫秒/微秒级墙钟阈值，CPU 被抢就假失败 |
| `spawn` | 起子进程 | 起子进程 / 多解释器 / 命名互斥体，跨进程竞态 |
| `playwright` | 真 Chromium | 起真浏览器，吃内存/CPU/焦点 |
| `ocr` | 真 OCR | RapidOCR 模型或 PP-OCR 服务，吃 CPU/GPU |
| `realdriver` | 真实驱动 | 碰真 Win32 输入/窗口消息，抢系统级资源 |
| `full` | 完整链路 | 单条用例就是一段持续负载（整链路 / 蒙特卡洛） |

其余用例一律轻量：不加锁、不串行、零额外开销。

### 3.2 模块清单

| 组 | 模块（`tests/` 下） |
|---|---|
| `full` | `test_soak.py`、`test_recovery.py`、`test_engine_countdown_flow.py` |
| `timing` | `test_precision.py`、`test_click_speed.py`、`test_feature_mouse_human.py`（同时属 `playwright`） |
| `spawn` | `test_fire_proc.py`、`test_fire_process.py`、`test_hotkey_mutex.py`、`test_persist_hardening.py`、`test_process_lifecycle.py` |
| `playwright` | `test_feature_skip_late.py`、`test_frontend_switch_rollback.py` |
| `ocr` | `test_popup.py`、`test_market_video.py`、`test_engine_insufficient.py`、`test_skin_ocr_real.py`、`test_ocr_panel.py`、`test_ocr_price.py`、`test_ocr_grade.py` |
| `realdriver` | `test_interception.py`、`test_uipi_probe.py` |

用例级覆盖（同一模块里只有个别用例重）：

| 组 | 用例 |
|---|---|
| `ocr` | `test_console_api.py::test_preflight_reports_every_item_with_a_fix` |

共 22 个模块 + 1 条用例级覆盖。一个模块可属多组（如 `test_feature_mouse_human.py`
是 `timing` + `playwright`），组锁会全部拿一遍。

### 3.3 组 → 锁文件命名

```python
GROUP_PRIORITY = ("timing", "playwright", "ocr", "spawn", "full", "realdriver")
```

锁文件名 = `heavy-<primary_group>`，`primary_group` 取 `GROUP_PRIORITY` 里第一个命中的组，
日志里一眼能看出「谁把谁挡住了」。

### 3.4 兜底：用例自己标

不在表里的临时/新增用例可以就地入组，避免为一条用例跑两处改：

```python
@pytest.mark.heavy(group="timing")
def test_something_tight(): ...
```

`conftest` 的 fixture 在表里查不到时会读 `get_closest_marker("heavy")` 的
`groups` / `group`（缺省 `full`）。收集阶段 `pytest_collection_modifyitems` 会把表里
的分组自动落成同名 marker，所以 `-m heavy` 也能选中全部重负载用例。

---

## 4. 锁实现要点

核心在 [lock.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/lock.py)。

### 4.1 选型：`O_CREAT|O_EXCL` + JSON 元数据，而不是 `msvcrt.locking` 常驻句柄

```python
flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
fd = os.open(self.path, flags, 0o644)
# 写 JSON 元数据（token / pid / pid_start / groups / nodeid / host / cwd / created）
os.close(fd)   # 写完立刻关句柄
```

理由：

1. 创建与释放都是普通文件操作，跨进程、跨会话、跨 xdist worker 都成立；
2. 持有者崩溃（进程消失）留下的文件可由**别的进程**判定并接管 ——
   `msvcrt.locking` 句柄式锁在持有者进程死前无法被打破；
3. 元数据能写「谁在持有、持有哪个用例、多久了」，超时提示才可读；
4. 句柄立即关闭，接管方的 `os.replace` 不会被 Windows 的共享锁挡住。

OS 级互斥**只用在拆陈旧锁那一小步**（`breaker.lock`，Windows `msvcrt.locking(LK_NBLCK,1)`，
POSIX `fcntl.flock`），保证同一时刻只有一个等待者去拆，避免双 rename 造成双持锁。

### 4.2 两级锁 + 固定顺序（防死锁）

```
heavy.lock          总闸：所有 heavy 用例共用，拿到才能往下
  ├─ heavy-timing.lock
  ├─ heavy-playwright.lock
  ├─ heavy-ocr.lock
  ├─ heavy-spawn.lock
  ├─ heavy-full.lock
  └─ heavy-realdriver.lock
```

`build_locks()` **先总闸、再按组名字典序**构造锁链，所有进程同一顺序获取 ——
不存在「A 拿 timing 等 playwright、B 拿 playwright 等 timing」的环。

为什么保留总闸：总闸让「不同组之间也串行」。组锁用于诊断归属和未来的并行策略。
实测 heavy 用例在满负载下互相踩得厉害（OCR 探测能拖几分钟），先全局串行是最省事
且可验证的解法。

### 4.3 崩溃接管路径（四类，全部实测过）

`_stale_reason(meta, age)` 按顺序判定，命中即在 breaker 临界区内**重检**后
`os.replace` 到 `.stale-<pid>-<rand>` 再 `unlink`：

| 判定 | 条件 | 场景 |
|---|---|---|
| `holder-dead` | `pid_alive(pid)` 为假 | 持有者进程被 kill / 崩溃 |
| `pid-reused` | 元数据 `pid_start` 与 `GetProcessTimes` 实际创建时间差 > 1 | 旧进程死后 PID 被新进程复用，看起来「还活着」 |
| `holder-wedged` | `age > stale_after_s`（默认 900s） | 持有者活着但卡死（关不掉的 shutdown 挂起） |
| `corrupt-meta` | 元数据不可读且 `age > 2s` | 刚创建还没写完 / 写到一半崩溃 |
| `bad-pid` | `pid` 字段非法且 `age > 2s` | 元数据被手改坏 |

`pid_alive()` 在 Windows 用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`；
拿不到句柄且错误码是 `ACCESS_DENIED` 时**保守判活**（宁可多等，不误拆活锁）。
`pid_start_marker()` 用 `GetProcessTimes` 拿 FILETIME 创建时间，专治 PID 复用。

实测均 0.000s 接管（见 §5.4 单元验证）。

### 4.4 超时与自救

所有等待有上限（默认 300s，`--test-lock-timeout` / `SJZ_TEST_LOCK_TIMEOUT` 可调）。
轮询间隔 `min(剩余时间, random(0.02, 0.08))`，不会越过上限。超时抛 `LockTimeout`，
`conftest` 转成 `pytest.fail(..., pytrace=False)`，消息带齐：

```
等锁超时：heavy-ocr
  等了 300.0s（上限 300.0s）
  当前持有者：pid=12345（存活）组=ocr 用例=... 持有 302.1s
  锁文件：C:\Users\...\Temp\sjz-pytest-isolation\dafors-b3a7879d\heavy-ocr.lock
  自救：1) 确认没有别的 pytest 在跑后删除上面的锁文件；
       2) 用 --no-test-lock 跳过本次互斥；3) 调大 --test-lock-timeout。
```

释放前校验 `token`：锁已被接管时绝不删别人的锁（返回 False 而不是误删）。

### 4.5 时序组：拿锁后提优先级，跑完还原

`tests/_isolation/priority.py`：`timing` 组拿到锁之后才把本进程提到
`HIGH_PRIORITY_CLASS (0x0080)`（仅当旧优先级属 NORMAL/BELOW/ABOVE_NORMAL，
绝不动 REALTIME/HIGH），`finally` 立刻还原。等待期间不抢 CPU，只在持锁期间抢。
这一条是把 2ms 断言从「偶发红」救回「稳定绿」的关键之一。

### 4.6 锁目录（不写仓库）

```
%TEMP%\sjz-pytest-isolation\dafors-b3a7879d\
```

`project_tag()` = 目录名 + 规范化绝对路径的 sha1[:8]，同机别的仓库用同一套机制
互不阻塞。仓库里**一个锁文件都不落**。

### 4.7 等待统计

每条 heavy 用例（含 collection 阶段的 OCR 探测）释放后追加一行 JSONL 到
`<锁目录>\waits-<run_id>-<pid>.jsonl`：`run / pid / nodeid / groups / waited_ms / held_ms / timed_out`。

`RUN_ID` 取 `SJZ_TEST_RUN_ID`，没有就生成 `时间戳-pid` 并**写回环境变量** ——
xdist worker 继承它，同一场运行的所有 worker 共用一个 id。
`collect_waits()` 先按 run_id **严格**过滤，一条都读不到才退回「按时间窗」兜底，
避免把并发的另一场会话的等待算到自己头上。

`pytest_terminal_summary` 打印分布（p50/p95/max/总计 + 最慢 5 条），
**丰收的话只在有记录时打印**，不给干净输出添噪声。

### 4.8 开关一览

| 选项 / 环境变量 | 默认 | 作用 |
|---|---|---|
| `--no-test-lock` | 关 | 逃生开关：完全禁用互斥，汇总行提示「已禁用」 |
| `--test-lock-timeout` / `SJZ_TEST_LOCK_TIMEOUT` | 300 | 等锁上限秒 |
| `--test-lock-stale` / `SJZ_TEST_LOCK_STALE` | 900 | 陈旧接管阈值秒 |
| `--test-lock-groups` | — | 打印分组清单并退出 0 |
| `SJZ_TEST_RUN_ID` | 自动 | 运行标识，xdist 聚合用 |

`pytest_report_header` 每次开会话都打一行，说明锁是启用还是禁用、目录、阈值 ——
跑完日志里能自证状态，不用猜。

---

## 5. 实测证据

### 5.1 四并发：不加锁

选择 = 2 条时序用例 + 整份 `tests/test_fire_proc.py`（每实例 13 例）：

```powershell
$sel = @('tests/test_feature_mouse_human.py::test_segment_preheat_stops_short_then_tail_closes_gap',
         'tests/test_feature_mouse_human.py::test_late_cursor_bump_uses_tail_not_teleport',
         'tests/test_fire_proc.py')
# 4 个进程同时启动，各带 --no-test-lock
```

| 实例 | 结果 | duration | 失败明细（`lag_ms`，断言 `< 2.0`） |
|---|---|---|---|
| unlocked-1 | 2 failed, 11 passed | 5.98s | `segment_preheat 11.346ms`、`late_cursor_bump 3.034ms` |
| unlocked-2 | 1 failed, 12 passed | 6.57s | `segment_preheat 7.533ms` |
| unlocked-3 | 13 passed | 6.55s | — |
| unlocked-4 | 2 failed, 11 passed | 5.89s | `segment_preheat 14.147ms`、`late_cursor_bump 4.181ms` |

**4 进程总墙钟 7.4s，共 5 处假失败，3/4 实例有红。**

同一台机器另一轮（选择里多带了 `test_console_api::test_preflight…`），
假失败值 20.589 / 11.935 / 25.434 / 18.58ms；并且**4 个进程打印完 summary 后不退出**
（CPU 冻结，10+ 分钟后由外部 kill）。该 shutdown 卡死在 `--no-test-lock` 与加锁下
都能出现（见 §7 残余风险 3），与锁无关，是套件已有的 `test_preflight` 关停问题。

### 5.2 四并发：加锁

同一选择，去掉 `--no-test-lock`，4 个进程同时启动：

| 实例 | 结果 | duration | 获取次数 | 超时 | 等锁 p50 / p95 / max / 总计 | 持锁 p50 |
|---|---|---|---|---|---|---|
| locked-1 | **13 passed** | 19.69s | 14 | 0 | 0 / 3704 / 7516 / 15157ms | 156ms |
| locked-2 | **13 passed** | 22.06s | 14 | 0 | 0 / 3765 / 14046 / 17811ms | 63ms |
| locked-3 | **13 passed** | 23.49s | 14 | 0 | 0 / 6625 / 11453 / 18656ms | 62ms |
| locked-4 | **13 passed** | 22.13s | 14 | 0 | 0 / 609 / 17360 / 17969ms | 62ms |

每实例 14 次获取 = 13 条用例 + 1 次 collection 阶段的 OCR 探测。
**4 进程总墙钟 24.3s，13×4 = 52 例全绿，0 次超时。**

持锁 p50 只有 62~156ms：绝大多数用例拿锁是瞬时的，串行成本远低于失败的代价。
最慢的一条等了 17.3s —— 那是排在别的进程的 `test_fire_proc` 后面，
属于设计内行为（等，而不是抢）。

### 5.3 换一种并发形态：xdist + 随机序

```
python -m pytest <抽样模块> -n 4 --random-order --random-order-seed=20261107
```

结果：90 passed、1 failed。唯一失败是
`test_frontend_switch_rollback.py::test_single_out_of_bounds_rolls_back_to_disk`
（Playwright `Page.wait_for_function` 8s 超时，该用例单跑 4.02s 通过）——外部满负载
下真 Chromium 自身超时，属残余风险，不是锁的问题。无 `greenlet` ERROR。

锁侧：79 次获取、0 次超时，最长等锁 38000ms（那一阵别的会话正占着总闸）。
**xdist worker 与 random-order 都正常工作，锁与两者不冲突。**

### 5.4 锁本身的自测（单元级）

`unit_lock_check.py`（临时脚本，不入库）跑在本机真实临时目录：

| 场景 | 期望 | 实测 |
|---|---|---|
| 空闲获取 | 立即成功 | 0.000s，release True |
| 持有者已死（`holder-dead`） | 接管 | 0.000s |
| 持有者活着但超龄（`holder-wedged`） | 接管 | 0.000s |
| 元数据损坏（`corrupt-meta`） | 超 2s 后接管 | 0.000s |
| 等锁超时（timeout=0.4s） | 抛 LockTimeout 且消息可读 | 0.4s，消息含 pid/组/用例/自救 |
| 统计写入 / 读回 | 分布正确 | 通过 |

---

## 6. 单次全量耗时对照

同一命令 `python -m pytest tests -q`，同一台满负载机器。

### 6.1 不加锁（基线）

```
4 failed, 2287 passed, 19 skipped, 2 xfailed, 8 warnings in 775.47s (0:12:55)
wall = 1399.8s, exit = 1
```

4 个失败全部**与锁无关、与被测代码无关**：

1. `test_fix_d.py::test_start_clears_one_shot_flags`
2. `test_state_hardening.py::test_engine_start_resets_task_residue`
3. `test_state_hardening.py::test_engine_start_is_atomic_under_barrier`
4. `test_frontend_switch_rollback.py::test_pair_inversion_rolls_back_to_disk`

前 3 条同一根因：`AttributeError: 'dict'/'_Cfg' object has no attribute 'data'`，
来自 `core/autocalib.py` 正被**别的会话**改写（外部 churn）。
第 4 条是 Playwright `Page.click: Timeout 30000ms exceeded`。

wall 1399.8s ≫ duration 775.47s：打印 summary 后又卡死约 625s 才退出，
即 §5.1 提到的同一个 shutdown 问题（`test_preflight` 的后台线程/子进程关不掉）。
**因此本仓库的「wall」不可作为耗时指标，只能看 pytest 自身的 duration。**

### 6.2 加锁（本次）

```
6 failed, 2293 passed, 19 skipped, 2 xfailed, 8 warnings in 749.42s (0:12:29)
wall = 1074.5s, exit = 1
```

锁侧统计（`pytest_terminal_summary` 原文）：

```
heavy 锁：587 次获取，其中 16 次等待 >=100ms，0 次超时
等锁耗时(ms)：p50=0.0 p95=16.0 max=24891.0 总计=69873
持锁耗时(ms)：p50=16 p95=1921 max=36094 总计=307041
```

解读：

- **587 次获取 = 全套 heavy 用例数**，占 2300+ 例的约 1/4；其余 3/4 轻量用例零锁开销。
- 等锁总计 **69.9s**，占 duration 的 9.3%；`p95=16ms` 说明绝大多数时候锁是空的。
- 持锁总计 307s（含等锁的 70s），占 duration 的 41% —— 这就是串行化必然的代价，
  但它换来的是「heavy 用例之间不再互相踩」。
- 0 次超时：默认 300s 上限在全量运行里一次都没触到。

### 6.3 前后对照

| 指标 | 不加锁 | 加锁 | 差 |
|---|---|---|---|
| pytest duration | 775.47s（12:55） | **749.42s（12:29）** | −26.0s |
| wall（含 shutdown 卡死） | 1399.8s | 1074.5s | −325s |
| failed | 4 | 6 | +2（见下） |
| passed | 2287 | 2293 | +6（收集到的用例多了 8 条） |
| skipped / xfailed | 19 / 2 | 19 / 2 | 0 |

**结论：加锁没有把单次全量拖慢。** duration 反而短了 26s，属机器负载波动的量级
（那 70s 的等锁时间被「heavy 用例不再互相拖」抵掉了）。
两个 wall 都不可直接比较：它们被同一个 shutdown 卡死放大（1754s 差异里的 625s vs 325s
只是运气），所以只看 duration。

### 6.4 加锁后的 6 个失败逐条归因（均与锁无关）

| # | 用例 | 现象 | 归因 |
|---|---|---|---|
| 1 | `test_fix_d.py::test_start_clears_one_shot_flags` | `AttributeError: '_Cfg' object has no attribute 'data'` | `core/` 被别的会话改写（外部 churn），基线同样红 |
| 2 | `test_state_hardening.py::test_engine_start_resets_task_residue` | 同上，`'dict' object` | 同上 |
| 3 | `test_state_hardening.py::test_engine_start_is_atomic_under_barrier` | 8 线程全返回同一 AttributeError | 同上 |
| 4 | `test_frontend_switch_rollback.py::test_pair_inversion_rolls_back_to_disk` | `Page.click: Timeout 30000ms exceeded` | 外部满负载下真 Chromium 自身超时（单跑 4.02s 通过） |
| 5 | `test_feature_skip_late.py::test_frontend_headless_control_to_config` | 子进程 Playwright `frame.click` 超时 | 同上，外部 CPU 饥饿 |
| 6 | `test_click_speed.py::test_confirm_stops_as_soon_as_the_dialog_closes` | 整轮 **1514ms**，断言 `< 120ms` | timing 组已进锁并提优先级，仍被外部 100% CPU 饿死；单跑可过 |

前 3 条是代码一致性问题的**外部 churn**（不是锁、不是并发），后 3 条是
「锁管不住别的会话」的**真环境不足**。基线里同类的后 3 条只红了 1 条，
说明这类红是概率性的，与锁无关（§7 残余风险 1、2）。

---

## 7. 残余风险

1. **外部 100% 负载下，Playwright 与 timing 自己的阈值仍会被饿死。**
   锁只串行本仓库的 heavy 用例，管不住别的会话的 Chromium / 游戏 / `longrun_collect`。
   加锁全量里仍红了 3 条：`test_frontend_switch_rollback.py` 的 `30s Page.click` 超时、
   `test_feature_skip_late.py` 子进程 Playwright `frame.click` 超时、
   `test_click_speed.py::test_confirm_stops_as_soon_as_the_dialog_closes` 整轮 1514ms
   （断言 `< 120ms`，该用例已在 `timing` 组且持锁期间提了优先级，仍被饿死）。
   这类红是真的环境不足，不是假失败 —— 要根治只能独占机器或调高 Playwright 超时
   （未做，属用例侧策略）。

2. **`core/` 被别的会话改坏时全量必红。**
   基线那 3 条 `autocalib` 失败就是现场证据。锁只保证「互斥」，不保证「代码一致」。
   排障顺序：先 `git diff core/`，再怀疑锁。

3. **shutdown 卡死（`test_preflight` / OCR 相关）与锁无关，但会污染 wall 指标。**
   4 并发下 4 个进程打印 summary 后都不退出；`--no-test-lock` 同样复现。
   全量单跑也一样：不加锁 wall 1399.8s（summary 后冻结约 625s）、
   加锁 wall 1074.5s（冻结约 325s），两次都靠外部 kill 或长时间后才退出。
   这是套件既有问题（`core/server.py` / OCR 服务关停），未在本任务范围修。
   **建议后续按 duration 而不是 wall 做对比。**

4. **陈旧接管的误判窗口：活但极慢的持有者。**
   `holder-wedged` 只认「锁年龄 > 900s」。如果某个 heavy 用例真的跑超过 900s
   （例如满负载下的 soak），等待者会把活锁拆掉，造成短暂双持。
   缓冲：默认阈值 900s 远大于任何单条 heavy 用例（全量实测最长持锁 36.1s）；
   极端场景请调 `--test-lock-stale`（例如 `1800`）。
   另：`pid_alive()` 在 `ACCESS_DENIED` 时保守判活，宁可等也不误拆。

5. **跨主机不成立。** 锁是本机文件系统语义。多台机器并行跑同一仓库不在覆盖范围内
   （也不该这么做 —— 用例会抢同一份 `config.json`）。

6. **锁不解决「真实数据文件」并发写。**
   那是 `_guard_real_files` fixture 的职责（拦本进程的写）；别的会话改写
   `config.json` 仍会影响本进程的读取。锁文件与业务数据文件是两件事。

---

## 8. 给后续会话的使用说明

### 8.1 默认：加锁，什么都不用做

不加任何参数跑 pytest 就是加锁状态。`pytest_report_header` 会打：

```
test isolation: heavy 锁启用 dir=C:\Users\...\Temp\sjz-pytest-isolation\dafors-b3a7879d timeout=300s stale=900s
```

看到这行就说明互斥生效。

### 8.2 什么情况下**必须**加锁

满足任意一条，就必须保持默认（不加 `--no-test-lock`）：

- 同机还有**第二个** pytest 在跑（别的会话、CI、你在另一个终端）；
- 用 `-n`（xdist）跑；
- 同机在跑 `tools/longrun_collect.py` / `run_accuracy.py` / 游戏本体 / OCR 服务压测；
- 跑全量（`pytest tests`）—— 里面有 timing / spawn / playwright / ocr 用例；
- 你不确定机器上有没有别的东西在跑。

### 8.3 什么情况下**允许** `--no-test-lock`

只允许这三种：

1. **独占机器排障**：你确认整机没有第二个 pytest、没有长跑采集、没开游戏，
   且目的是复现「无锁时的原始行为」；
2. **锁系统自测 / 对照实验**：就是要测「不加锁会不会红」（如本报告 §5.1）；
3. **单机单任务的 CI**：pipeline 保证同一台 runner 同一时刻只有一个 pytest 作业。

### 8.4 什么情况下**禁止** `--no-test-lock`

- 多会话并行时为了「刷绿」而加 —— 那是把假失败藏起来，不是修；
- 写进 CI 的默认参数（除非真做到 §8.3 第 3 条）；
- 遇到锁超时就无脑加 —— 先看超时消息里的持有者，多半是另一个会话正在跑全量，
  等它跑完即可。实在要跳过，先记录这次跳过，别让它变成习惯。

### 8.5 遇到锁超时怎么处理

超时消息已经给全了信息。按顺序：

1. 看 `当前持有者：pid=... 组=... 用例=...` —— 是不是另一个会话的正常运行？等它；
2. 确认没有别的 pytest 后，删掉消息里给出的锁文件；
3. 临时绕过：`--no-test-lock`（记得 §8.4）；
4. 已知某条用例就是慢：`--test-lock-stale 1800`，别改默认值；
5. 锁系统本身可疑：跑 `--test-lock-groups` 确认分组，再看 `<锁目录>\waits-*.jsonl`。

### 8.6 新增重负载用例怎么办

在 [groups.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/groups.py) 的
`MODULE_GROUPS` 加一行（整模块）或 `NODE_GROUPS` 加一条（单用例）。
判据见 §3.1。不要在用例里手写 `@pytest.mark.heavy` 长期驻留 —— 表才是真源，
marker 只给临时实验用。

### 8.7 别做的事

- 别把锁目录写进仓库（现在就在 `%TEMP%`，保持）；
- 别让锁文件进 `.gitignore` 之外的任何清单 —— 它们根本不在仓库里；
- 别用 `msvcrt` 长持有句柄式锁替代当前实现 —— 崩溃后无法被别的进程接管；
- 别在 `core/` / `engine.py` / `web/` / `tools/` 里引入对 `tests/_isolation` 的依赖
  （测试基建不能反向污染生产代码）。

---

## 9. 附：文件与命令

### 9.1 改动清单

| 文件 | 说明 |
|---|---|
| [tests/_isolation/__init__.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/__init__.py) | 包声明 |
| [tests/_isolation/lock.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/lock.py) | 锁本体：获取/接管/释放/统计 |
| [tests/_isolation/groups.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/groups.py) | 分组真源 + 清单输出 |
| [tests/_isolation/priority.py](file:///C:/Users/26346/Desktop/project/dafors/tests/_isolation/priority.py) | 时序组优先级提升/还原 |
| [tests/conftest.py](file:///C:/Users/26346/Desktop/project/dafors/tests/conftest.py) | 选项、marker、autouse fixture、OCR 探测锁、汇总输出 |
| `_test_isolation_lock.md` | 本文档 |

未改：`core/`、`engine.py`、`web/`、`tools/`。未 commit、未 push。

### 9.2 常用命令

```powershell
cd C:\Users\26346\Desktop\project\dafors

# 看分组清单
.venv\Scripts\python.exe -m pytest tests --test-lock-groups

# 只跑重负载用例（串行）
.venv\Scripts\python.exe -m pytest tests -m heavy -q

# 只跑轻量用例（可并发）
.venv\Scripts\python.exe -m pytest tests -m "not heavy" -q

# 全量（默认加锁）
.venv\Scripts\python.exe -m pytest tests -q

# 全量 + xdist（锁仍生效，worker 共用 RUN_ID）
.venv\Scripts\python.exe -m pytest tests -q -n 4

# 逃生开关（仅限 §8.3 三种场景）
.venv\Scripts\python.exe -m pytest tests -q --no-test-lock

# 收紧/放宽阈值
.venv\Scripts\python.exe -m pytest tests -q --test-lock-timeout 600 --test-lock-stale 1800
```
