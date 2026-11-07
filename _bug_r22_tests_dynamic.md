# R22 测试体系有效性 —— 动态实测报告

> 审计对象：`C:\Users\26346\Desktop\project\dafors`（只读审查，未改工作区代码、未 commit/push）
> 方法：**变异测试 + 5 次全量复跑 + 2 次随机顺序 + 70 个测试文件逐个单跑**
> 所有变异实验在临时副本 `%TEMP%\r22_audit_B` 中执行，测试复跑在 `%TEMP%\r22_audit_A` 中执行；两副本均从 2026-11-07 06:20 左右的工作区快照复制。

---

## 1. 结论速览

| 模块 | 有效变异 | 存活 | 存活率 | 判定 |
|---|---:|---:|---:|---|
| 价格判定（cards/market） | 6 | 5 | **83.3%** | 基本形同虚设（只防“返回常量”这种低级错） |
| 倒计时估计（countdown） | 6 | 3 | **50.0%** | 核心路径有效，边界/速率校验缺失 |
| 输入通道选择（input） | 5 | 2 | **40.0%** | 主流程有效，兜底分支几乎无覆盖 |
| 异常分级（scene_guard） | 5 | 3 | **60.0%** | 硬停与滞回有效，升级上限/耗尽判据缺失 |
| 配置读写（config） | 5 | 0 | **0.0%** | **确实有效** |
| **合计** | **27** | **13** | **48.1%** | 近一半人为缺陷不会被发现 |

- 测试套件当前是**红灯**：全量 1313 用例稳定失败 10 个（8 个前端静态契约 + 1 个精度基准 + 1 个源码 BOM 编译），意味着“测试能拦 bug”这一前提本身已不成立。
- 9 个 flaky 用例，全部集中在时序/子进程/线程断言；其中 `test_two_hundred_shots_deviation_distribution` 全量 5/5 失败、单跑 3/3 通过，是**套件内污染**的直接证据。
- 随机顺序未发现新增的顺序依赖（除上述精度用例），说明问题主要是**时序门限过紧 + 用例间资源残留**，不是全局状态顺序耦合。
- 唯一可以称得上“测试确实有效”的模块：**配置读写**。

---

## 2. 基线（工作区，2026-11-07 约 06:10）

```
python -m pytest tests -q -p no:cacheprovider
1313 tests, 11 failed, 耗时 432s
```

稳定失败的 11 个（工作区快照）：

| 类别 | 用例 |
|---|---|
| 前端静态契约 ×8 | `test_condition_errors_are_rendered_inline`、`test_keyboard_accessibility_is_wired`、`test_poll_discards_stale_responses`、`test_start_button_shows_the_only_mode`、`test_start_is_mutually_exclusive`、`test_task_name_is_escaped_in_the_queue_list`、`test_tier_options_come_from_the_backend_contract`、`test_wear_tag_escapes_the_ocr_value` |
| 精度基准 | `test_two_hundred_shots_deviation_distribution`（“外部进程没看见这次触发”12 次） |
| 源码编译 | `test_every_source_file_compiles`（`tools/_r08_dbg.py:1` 带 UTF-8 BOM） |
| 速度门限 | `test_detection_is_fast_enough_for_the_buy_loop`（560ms > 300ms） |

> 前端 8 个用例断言的是旧 DOM 契约。例如 `test_start_is_mutually_exclusive` 断言 HTML 中存在字面量 `if(STATE.starting) return;`，而当前 `web/index.html` 已改用 `S.*` 状态对象，该字面量不存在。它们是**测试过期**，不是偶发。

---

## 3. 变异测试（27 个有效变异，逐次注入、逐次跑测）

方法：每个变异只改一处、语法合法；跑完立即还原。价格/倒计时/输入/分级/配置分别跑对应测试子集（价格：`test_cards` `test_market` `test_fix_c` `test_ocr_price`；倒计时：`test_countdown` `test_countdown_sim`；输入：`test_input_channels` `test_input_abi` `test_input_interception`；分级：`test_scene_guard`；配置：`test_config` `test_switches`）。判定“杀死”= 出现基线之外的新失败节点。

### 3.1 存活变异明细（测试没拦住）

| ID | 严重度 | 模块 | 具体变异 | 为什么没被发现 | 建议补什么测试 |
|---|---|---|---|---|---|
| P3 | **高** | 价格判定 | `parse_row` 多候选价 `max(cand)` → `min(cand)` | `pick()` 用例直接构造行对象；`parse_row` 用例每行只有一个候选价，max/min 不可区分 | 同一行两个候选（如“饰品格 025000 1200”）断言取 25000；再加“低价促销 1200 与划线价 25000”断言取 25000 |
| P1 | 中 | 价格判定 | 价格下限 `val < _MIN_PRICE` → `<=`（把 100 当合法价） | 全部价格用例都在 8888/13000/25000 量级，没有 99/100/101 边界 | 参数化边界：99→无价、100→100、101→101（cards 与 market 两条路径都测） |
| P4 | 中 | 价格判定 | 千位点号 `int(ip) > 5` → `>= 5`（“5.000”被读成 5000） | 没有“磨损值恰好 5.000”的用例；现有 `9.000`/`0.120` 覆盖不到 5 | `_price_from_block("成色 5.000")` 断言无价；`("5.000 25000")` 断言 25000 |
| P5 | **高** | 价格判定 | 删掉 `1<=len(ip)<=3 and int(ip)>5` 两道校验（`elif len(fp)==3:` 直接放行） | 没有“三位小数磨损混在价格行”的用例；`0.583172` 因整数部分 6 位被另一条规则挡住 | `_price_token_in("QBZ95 成色 0.120 14000")` 断言 14000；`("0.120")` 断言无价；再补 `0.050`、`0.005` 反例 |
| P6 | 中 | 价格判定 | `_MIN_PRICE = 100` → `1000` | 测试里最低真实价 8888，100–999 的低价场景完全没有 | 100、999、1000 三档价格解析用例（低价捡漏是核心业务） |
| C2 | **高** | 倒计时估计 | 速率违约触发格数 `ds >= 2` → `ds >= 3` | 只有单拍跳变/时钟回拨用例；没有“连续两拍稳定但整段速率超标”的构造 | 构造 (T, T-2)、间隔 1.0s 的两拍稳定读数，断言 Estimate.trust=False；再补 ds=2/3/4 参数化 |
| C3 | **高** | 倒计时估计 | 删掉“读数不许倒退”校验（`ds < 0` 返回 False） | 倒退场景由 `observe()` 先重置缓冲，`_rate_violation` 的负 ds 分支实际不可达；无用例直接构造跨稳定段倒退 | 直接向估计器喂入两段稳定但末值更小的读数，断言不 trust / 回退到 bracket 不可信态 |
| C1 | 中 | 倒计时估计 | admissible 半宽阈值 `0.010` → `0.020` | `test_countdown` 只断言估计出的 `half_width` 小于 0.020/0.010，没有构造 `half_width=0.015` 并断言 admissible=False | 直接构造 `Estimate(trust=True, half_width=0.015, n_inliers=100, span=2)` 断言 admissible 为 False；0.009/0.010/0.011 边界参数化 |
| I5 | 中 | 输入通道选择 | 删掉“降级目标 == 请求通道”兜底（`and False`） | `mode=message + fallback=message + 窗口不支持消息` 的分支没有任何用例；现有用例只测 legacy→message | 断言该组合抛 `R_FALLBACK_DISABLED`，且不静默“降级到自己” |
| I4 | 低 | 输入通道选择 | 非法 fallback 的默认值由 `legacy` 改成 `message` | `test_config` 只校验配置层默认值，从不调用 `_resolve_fallback("bogus")`；运行时行为漂移无人发现 | 单测 `_resolve_fallback("bogus") == "legacy"`；并断言 Mouse(fallback="bogus") 的降级链走 legacy |
| S4 | **高** | 异常分级 | `spec.max_level >= LEVEL_GUIDE` → `>`（可耗尽场景不再转“找人”） | 没有 `max_level` 恰好等于 LEVEL_GUIDE 的场景用例；现有用例只覆盖“安全场景直接到 4/5 级”和单步升级 | 构造 max_level==4 的场景，连续失败耗尽自动手段，断言进入 `STATE_WAITING_HUMAN` 而不是 HALT |
| S2 | 中 | 异常分级 | 升级上限 `self._level < LEVEL_RELOCATE` → `<=` | 没有“已到 L3 再失败”的用例，升级越界路径无人验证 | 连续失败把 level 推到 3，再触发一次失败，断言不会越过 RELOCATE 直接跳级 |
| S5 | 中 | 异常分级 | `LEVEL_TEXT[LEVEL_GUIDE]` 由“需要你”改成“正常” | `test_scene_guard` 只断言文案非空/无术语，没有逐级精确文案 | 对 `LEVEL_TEXT` 全表做快照断言（1–5 级文案逐字校验） |

### 3.2 被测试杀死的变异（说明这些地方测试确实有牙）

| ID | 模块 | 变异 | 抓到它的测试（示例） |
|---|---|---|---|
| P2 | 价格判定 | 价格函数返回常量空串 | `test_price_in_the_grade_band_must_look_like_a_price` 等 5 个 |
| C4 | 倒计时 | 跳变成对判据 `==1` → `==2` | `test_jump_pairs_lock_to_milliseconds[1/3/11]` 等 6 个 |
| C5 | 倒计时 | 跳秒种类不足时不判冻结 | `test_frozen_display_is_never_trusted[start-0.99/1.05]` |
| C6 | 倒计时 | 显示 floor 语义 1.0s → 0.0s | 倒计时重启/相位锁定等 8 个 |
| I1 | 输入 | 忽略驱动探测失败 | `test_driver_fallback_is_recorded_and_queryable` 等 4 个 |
| I2 | 输入 | UnrealWindow 纯消息无效判据失效 | `test_message_on_unreal_window_degrades_with_reason` 等 6 个 |
| I3 | 输入 | UIPI `>` → `>=` | `test_legacy_*` 等 8 个 |
| S1 | 分级 | 硬停级按“需要你”进入 | `test_misbuy_enters_halted_with_one_sentence`、`test_safety_scenes_jump_straight_to_final_level` |
| S3 | 分级 | 滞回清零 `>=` → `>` | `test_hysteresis_requires_clear_ticks` |
| K1 | 配置 | 白名单删 `regions.bar` | `test_the_price_bar_region_survives_a_round_trip` |
| K2 | 配置 | 默认 workflow 改 list | `test_a_fresh_config_gets_the_documented_defaults`、`test_every_default_matches_config_defaults` |
| K3 | 配置 | regions 不按白名单过滤 | `test_unknown_region_keys_are_dropped` |
| K4 | 配置 | 首次启动不落盘 | `test_a_fresh_config_gets_the_documented_defaults` |
| K5 | 配置 | 默认阈值 0.85 → 0.5 | 同上两个默认值契约用例 |

---

## 4. 重复跑 5 遍：flaky 清单

命令：`python -m pytest tests -q --tb=no -p no:cacheprovider`，每次 1313 用例。

| 轮次 | 失败数 | 耗时 |
|---|---:|---:|
| R1 | 18 | 580.6s |
| R2 | 16 | 512.7s |
| R3 | 10 | 418.4s |
| R4 | 10 | 404.1s |
| R5 | 10 | 383.2s |

固定失败 10 个（8 前端静态 + `test_two_hundred_shots_deviation_distribution` + `test_every_source_file_compiles`），波动失败 8 个：

| 用例 | 严重度 | R1–R5 | 实测证据 |
|---|---|---|---|
| `test_precision::test_two_hundred_shots_deviation_distribution` | **高** | FFFFF（但乱序第二种子通过；单跑 3/3 通过） | 全量时“外部进程没看见这次触发”12 次；单独跑 50 个时序用例 6.8s 全绿。典型的套件内资源/进程残留污染 |
| `test_precision::test_fire_in_uses_relative_delay` | **高** | FF... | `实际等了 150.6ms`，断言 `<= 60ms`；负载下调度抖动直接击穿 60ms 门限 |
| `test_precision::test_fire_at_returns_immediately` | 高 | FF... | 同上的“立即返回”墙钟断言 |
| `test_precision::test_external_thread_observes_each_firing` | 高 | FF... | 外部线程漏看触发，缺少对“看不到就重试/放宽”的容错 |
| `test_precision::test_cancel_is_seen_inside_the_spin_loop` | 中 | FF... | 自旋循环内取消可见性依赖线程调度 |
| `test_precision::test_external_cancel_source_is_honoured` | 中 | F....（仅 R1） | 同上，偶发 |
| `test_popup::test_detection_is_fast_enough_for_the_buy_loop` | 高 | FF... | `识别耗时 560ms`，断言 `<300ms`；与工作区基线同样失败 |
| `test_fire_proc::test_bad_worker_path_marks_dead` | 高 | FF... | 断言 `.dead` 必须“立刻”为 True，而子进程退出是异步的；负载下状态更新滞后 |
| `test_engine_list_queue::test_idle_refresh_when_nothing_matches_for_a_while` | 中 | F....（仅 R1） | 窗口内只发生 1 次周期刷新（断言 ≥2），刷新周期与窗口卡得太紧 |

结论：flaky 全部是**墙钟/异步状态断言**，不是业务逻辑不确定；正确修法是注入可控时钟/事件，而非简单放宽阈值（放宽会掩盖真实性能退化）。

---

## 5. 顺序打乱（2 个种子）

| 种子 | 失败数 | 与 R5 固定集差异 |
|---|---:|---|
| 20261107 | 10 | 完全一致（含精度基准） |
| 424242 | 9 | 少 1 个：`test_two_hundred_shots_deviation_distribution` 通过 |

- 除精度基准外，**没有发现新的顺序依赖**：8 个前端静态与 BOM 用例在任何顺序下都失败，说明是契约过期而非顺序问题。
- 精度基准在乱序第二种子下通过、第一种子下失败、全量 5/5 失败、单跑 3/3 通过 → 它的问题是**同进程内前序用例残留（线程/子进程/CPU 占用）**，而不是固定顺序。

---

## 6. 隔离性：单文件单跑（70 个文件）

| 现象 | 用例 |
|---|---|
| 全量失败、单跑通过（**污染/资源依赖**） | `test_two_hundred_shots_deviation_distribution` |
| 单跑失败、全量通过 | 无 |
| 单跑也稳定失败（**测试本身过期/源码问题**） | 8 个 `test_frontend_static::*`、`test_every_source_file_compiles` |

补充实测：把 `test_precision.py test_popup.py test_fire_proc.py test_engine_list_queue.py` 一起单独跑 3 遍，每遍 **50 passed，6.7–6.9s**。这证明时序用例本身在干净进程里是稳定的，放进全量套件后因前序用例残留而崩。

---

## 7. 超时与资源

单文件单跑耗时 Top：

| 文件 | 耗时 |
|---|---:|
| `test_nav_filter.py` | 86.6s |
| `test_nav.py` | 40.6s |
| `test_soak.py` | 27.3s |
| `test_nav_onsale.py` | 26.3s |
| `test_ocr_service.py` | 25.1s |
| `test_engine_shot.py` | 21.8s |

单用例 Top（全量 R5）：

| 用例 | 耗时 |
|---|---:|
| `test_soak::test_short_run_end_to_end` | 25.4s |
| `test_nav_filter::test_pick_season_reads_short_items_by_upscaling` | 21.4s |
| `test_nav_filter::test_col_filter_switches_back_to_the_trade_tab_when_the_panel_will_not_open` | 14.9s |
| `test_nav_filter::test_pick_season_scrolls_to_find_later_items` | 11.3s |
| `test_engine_shot::test_write_failure_only_warns` | 7.6s |

资源异常点：

- `test_precision::test_two_hundred_shots_deviation_distribution` 在进程内启动外部观察进程并跑 200 发，是全套件最重的资源点，也是污染源（单跑 6.8s 内 50 个用例全过，全量时它自己失败）。
- `test_soak` 起完整子进程链路（27s），`test_ocr_service` 起 OCR 服务（25s），`test_nav*` 用真实滚动模拟（累计 150s+）。这三类是 CI 时长与抖动的主要来源。
- 无 pytest-timeout：`pytest --timeout` 不可用（插件未安装），挂死用例无法被框架兜底，只能靠外层进程超时。

---

## 8. 固定红灯（与本次变异无关，但直接决定测试可信度）

| 严重度 | 模块 | 问题 | 原因 | 建议 |
|---|---|---|---|---|
| **高** | 前端静态契约 | 8 个 `test_frontend_static` 稳定失败 | 断言写死旧 DOM 字面量（如 `if(STATE.starting) return;`），当前 `web/index.html` 已改为 `S.*` 状态对象；契约测试未随重构更新 | 改用 `data-testid` 或 DOM 结构断言；把“互斥/转义/轮询丢弃”改为行为级断言，避免字符串快照 |
| 中 | 源码卫生 | `tools/_r08_dbg.py` 带 UTF-8 BOM，`test_every_source_file_compiles` 失败 | 调试脚本被 `tools/` 全量扫描纳入编译检查 | 去掉 BOM；或把 `_dbg` 类临时脚本排除出扫描 |
| 中 | 精度基准 | 200 发偏差分布在套件内必失败 | 前序用例残留 + 外部进程观察窗口过窄 | 用例内隔离进程/线程；给观察进程加就绪握手与最大等待；失败信息带上缺失触发编号 |

---

## 9. 测试确实有效的模块（收尾）

- **配置读写（config）**：5 个变异全部被杀死（默认值、白名单、未知 region 过滤、首次落盘、阈值），且这些用例单跑与全量都稳定。**这是本套件唯一可以完全信任的模块。**
- 其余模块只能算“核心路径有效、边界失守”：
  - 倒计时：冻结识别、跳变成对、floor 语义有效；速率违约与倒退校验无效。
  - 异常分级：硬停与滞回有效；升级上限、耗尽转人工、文案表无效。
  - 输入通道：驱动探测、UnrealWindow 判据、UIPI 有效；非法策略与同通道兜底无效。
  - 价格判定：只防“返回常量”，解析器的边界、多候选、磨损混排全部不设防。

---

## 10. 方法与限制

- 临时副本 A/B 均复制自 06:20 工作区快照；变异逐条注入、逐条还原，工作区代码零改动。
- 变异测试按模块跑相关测试文件子集（未对 27 个变异各跑一次 7 分钟全量，成本不可行）；子集覆盖该模块全部测试文件，结论对该范围成立。
- 复跑期间机器上同时有其他审计任务（soak/长跑/UI 探针），R1–R2 的墙钟失败明显偏多；因此本报告把“固定失败”“波动失败”“单跑通过”三类分列，避免把外部负载误判成业务 bug。R3–R5 在负载回落后稳定在 10 个固定失败。
- 复现命令（临时副本内）：
  - 变异：`python _mutrun.py` / `python _mutrun2.py`
  - 复跑+乱序+隔离：`python _dynrun.py`（`SHUFFLE_SEED` 控制种子）
  - 单跑复核：`python -m pytest tests/test_precision.py tests/test_popup.py tests/test_fire_proc.py tests/test_engine_list_queue.py -q`
