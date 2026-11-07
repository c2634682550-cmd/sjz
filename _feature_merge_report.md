# 七路功能合并与全量验证报告

- 代码根：`C:\Users\26346\Desktop\project\dafors`
- 验证快照：`_frozen`（合并前，含七功能与测试隔离修复）、`_merged`（合并后，含七功能 + perf 补丁队列 Q1–Q5）
- 基线：`perf/base @ cf532c70c48dfa8c`（该提交不在本仓对象库中，补丁按 hunk 人工合并）
- 约束遵守：未 commit、未 push、未开可见窗口、未动真实光标键盘、未调用真实购买链路
- 结论：**合并成立**。产品代码因合并缺陷改动 0 行；七功能结构/互斥/三段联通全部通过；补丁队列 5 条全部落地；全量回归 0 failed。

---

## 1. 结构一致性核对

### 1.1 21 个配置键 × 三张表

任务书写「20 键」，实际统计为 **21 键**（`stop.by_grade.enabled` 不在任务书列表里，但确实存在于三表与前端）。逐键结果：

| # | 键 | DEFAULTS | SWITCHES | BOUNDS | 前端区块 | 三段联通 |
|---|---|---|---|---|---|---|
| 1 | mouse.human.traj | ✓ auto | ✓ select | — | blk-fire-human | OK |
| 2 | mouse.human.move_ms | ✓ 120 | ✓ int | ✓ 30–800 | blk-fire-human | OK |
| 3 | mouse.human.curve | ✓ 0.35 | ✓ float | ✓ 0–1 | blk-fire-human | OK |
| 4 | mouse.idle.mode | ✓ off | ✓ select | — | blk-fire-idle | OK |
| 5 | mouse.idle.interval_ms | ✓ 1200 | ✓ int | ✓ 300–5000 | blk-fire-idle | OK |
| 6 | mouse.idle.return_s | ✓ 2 | ✓ int | ✓ 1–10 | blk-fire-idle | OK |
| 7 | mouse.idle.min_remain_s | ✓ 8 | ✓ int | ✓ 1–30 | blk-fire-idle | OK |
| 8 | mouse.idle.wheel_notches | ✓ 3 | ✓ int | ✓ 0–10 | blk-fire-idle | OK |
| 9 | scan.repeat.min | ✓ 1 | ✓ int | ✓ 0–10 | blk-fire-repeat | OK |
| 10 | scan.repeat.max | ✓ 1 | ✓ int | ✓ 0–10 | blk-fire-repeat | OK |
| 11 | buy.skip_late.threshold_s | ✓ 2 | ✓ int | ✓ 0–10 | blk-fire-skip-late | OK |
| 12 | stop.by_grade.enabled | ✓ False | ✓ bool | — | blk-fire-grade | OK |
| 13 | stop.by_grade.legend | ✓ 0 | ✓ int | ✓ 0–99 | blk-fire-grade | OK |
| 14 | stop.by_grade.epic | ✓ 0 | ✓ int | ✓ 0–99 | blk-fire-grade | OK |
| 15 | stop.by_grade.rare | ✓ 0 | ✓ int | ✓ 0–99 | blk-fire-grade | OK |
| 16 | stop.by_grade.scope | ✓ all | ✓ select | — | blk-fire-grade | OK |
| 17 | click.burst.mode | ✓ append | ✓ select | — | blk-fire-burst | OK |
| 18 | click.burst.hold_ms | ✓ 100 | ✓ int | ✓ 20–500 | blk-fire-burst | OK |
| 19 | click.burst.interval_ms | ✓ 5 | ✓ int | ✓ 1–200 | blk-fire-burst | OK |
| 20 | click.burst.window | ✓ all | ✓ select | — | blk-fire-burst | OK |
| 21 | buy.followup.enabled | ✓ False | ✓ bool | — | blk-fire-followup | OK |

- `SWITCHES total: 67, dups: []`；`BOUNDS entries: 36, raw dups: []`；七组键全部落在「抢购设置」分组，组内 21 项。
- 每个键的默认值与磁盘值一致（`config.json` 覆盖读回同值），无被其他块覆盖的键。
- 机关：数值键 14/14 在 BOUNDS；bool/select 7 个不进 BOUNDS（由 `_coerce` 按类型收敛），与 `switches.spec()` 输出一致。

### 1.2 前端七区块

- `web/index.html` `FEATURE-BLOCKS` MAP 顺序：`blk-fire-human` → `blk-fire-idle` → `blk-fire-repeat` → `blk-fire-skip-late` → `blk-fire-grade` → `blk-fire-burst` → `blk-fire-followup`，与契约一致；`blk-fire-grade` 为静态锚点，其余由渲染器重建。
- 无头 Edge 实测：七个容器全部存在于 DOM、顺序正确；21 个 `[data-sw]` 控件全部落在前缀对应的区块，`STRAY []`（零越界）；每秒重绘（`paintStats`）后归属不变。
- 互斥提示条 `#clickBurstWarn` 真实可见（`display:block`），文案：「连点与补刀互斥，当前按连点执行：假购买补刀本次不生效。」

### 1.3 引擎七处扩展点

| 扩展点 | 位置 | 承载功能 | 覆盖检查 |
|---|---|---|---|
| `_run` 启动块 | engine.py:618 | OCR 二级异步自检（Q1） | 与七功能无重叠 |
| `_countdown_prepare` | engine.py:1074 | scan.repeat / skip_late | 两功能共用，顺序为「先抽刷新次数，再判来不及」 |
| `_watch_countdown` | engine.py:1498 | idle tick + human 预热 | 预热在开火前收尾，idiom 无二次改写 |
| `_execute_list` | engine.py:1864 | 品阶闸门/额度 | 单一入口，无重复实现 |
| `_fire_and_confirm` | engine.py:2263 | click.burst + followup gate | 互斥判定在此，唯一 |
| 成交校验循环 | engine.py 约 2500 | Q3 前紧后松 | 独立 hunk |
| `_close_reward` | engine.py:2808 | Q4 条件等待 | 独立函数 |

- AST 检查：`dup_defs` 为空，无同名函数被两个功能各定义一遍。
- 与冻结快照逐行 diff：合并后差异只落在补丁 hunk（engine +93/−13、market +33/−11），七功能的代码块零改动。

### 1.4 六个新模块均被真实调用

| 模块 | 调用点（`.` 级引用计数） |
|---|---|
| humanmouse | engine 1、input 1、config 1 |
| idle_roam | engine 2 |
| repeat | engine 4、autotune 19、switches 8 |
| skip_late | engine 10、autotune 5、switches 3 |
| click_burst | engine 3、server 1（互斥 warning） |
| stop_grade | engine 23 |

无「写了文件没人调」的模块。

---

## 2. 七组跨功能互斥 / 共存实测

26 条专项用例（`mutex_merged.log`）：**26 passed in 8.65s**；合并前同一批 `26 passed in 8.85s`。

| 组 | 结论 | 关键证据 |
|---|---|---|
| 连点 × 补刀 | 同时开启 → 补刀被拒、日志有记录、界面有提示 | `followup_gate` 返回 `allowed=False`；reason「连点与补刀互斥，当前按连点执行：同一次进场只由连点出手，假购买补刀本次不生效」；POST `/api/switches` 返回 warning；`#clickBurstWarn` 可见 |
| 空闲漫游 × 开火 | 漫游在开火前归位，开火瞬间零位移 | `test_d5_fire_window_preempts_and_then_stays_quiet` / `..._list_route_has_zero_events_and_one_notice` |
| 拟人轨迹 × 开火时序 | 轨迹移动时间不计入卡点误差；做不到时有开关且文案说明代价 | 预热走完整 `move_ms`，开火 `fire=True` 零位移；`test_preheat_budget_too_small_refuses_to_move` 文案含「提前量不足（已计入 stats…」 |
| 来不及跳过 × 最小提前量 | 两阈值取更严 | `test_min_lead_and_skip_late_both_apply_stricter` |
| 随机刷新 × 预算/额度 | 随机值不突破预算，停即断刷 | `test_d5_draw_value_is_the_hard_cap` / `..._stop_stops_refresh_immediately` / `..._budget_is_bounded_and_exits_when_used_up` |
| 品阶计数 × quota/budget | 品阶闸门不绕 quota/budget | `test_grade_stop_wins_over_quota_when_both_hit` / quota / budget 三条 |
| 拟人轨迹 × 消息通道 | 纯消息通道不动真光标，轨迹标不生效 | `test_d5_message_channel_reports_degraded_and_moves_nothing` / idle 的 wheel 降级 |

---

## 3. 21 键前后端三段联通（无头浏览器，不弹窗）

- 工具：Playwright + 系统 Edge（`channel="msedge"`, headless），进程内起 `ThreadingHTTPServer`，临时 `CONFIG_PATH`，`server_gate_ok` 打桩。
- 逐键三段：① 控件存在且可交互 → ② POST `/api/switches` 后配置文件落盘值正确 → ③ 引擎读到的行为参数随配置改变（`switches.get` / 各模块 `read_*` 探针）。
- 结果：**KEYS 21 / ALL3_PASS True / bad: []**，`ORDER_OK True`，`STRAY []`，`CONSOLE_ERRORS []`。
- 关键点：数字控件用页面内 `evaluate` 设值并派发 `change`（因每秒重绘会替换节点）；bool 用 DOM `click`；先关闭 `#guideDlg` 避免拦截。
- 输出文件：`_merge_cfg/three_stage.json`（逐键 `stage1/2/3` 与 disk/api 值）。

---

## 4. 全量回归

### 4.1 合并前（`_frozen`，七功能 + 测试隔离修复）

| 运行 | 命令 | 结果 | 耗时 |
|---|---|---|---|
| F1 单进程 | `pytest tests -q` | 2005 passed / 19 skipped / 2 xfailed | 509.68s |
| F2 并行 | `pytest tests -q -n 4` | 2005 passed / 19 / 2 | 207.94s |
| F3 随机序 | `--random-order --random-order-seed=20261107` | 2005 passed / 19 / 2 | 479.69s |
| F4 随机序 | `--random-order --random-order-seed=424242` | 2005 passed / 19 / 2 | 478.13s |

四次均 **0 failed**。历史抖动与定位见 §6。

### 4.2 合并后（`dafors` 工作树，七功能 + Q1–Q5）

| 运行 | 命令 | 结果 | 耗时 |
|---|---|---|---|
| P1 | `pytest tests -q` | 1 failed（前端 30s 等待超时，负载抖动）→ 单跑该用例 **1 passed 3.83s** | 500.20s |
| P1b | `pytest tests -q`（复跑） | **2011 passed / 19 skipped / 2 xfailed，0 failed** | 469.53s |
| P2 | `pytest tests -q -n 4` | **2011 passed / 19 skipped / 2 xfailed，0 failed** | 200.75s |
| 互斥专项 | 26 节点 | 26 passed | 8.65s |
| 三段联通 | 无头 Edge | 21/21 | — |

- 2011 = 2005 + 6 条新增 OCR 测试（Q1 自带 5 条 + 1 条熔断文案不复读）。
- 跳过项均为已知环境依赖（缺真机截图素材 / rapidocr 未安装），未用 skip 掩盖任何 failed。

---

## 5. 待应用补丁队列（Q1–Q5）合并明细

| 顺序 | 补丁 | 落地文件 | 合并方式 | 复核 |
|---|---|---|---|---|
| Q1 | `ocr/01_tier2_never_dies_silently.patch` | core/ocr_chain.py、core/ocr.py、core/ocr_ppocr.py、core/engine.py、tests/test_ocr_price.py、tests/test_ocr_backend.py | 人工逐 hunk | 自检线程 daemon；探活 0.3s；三态文案；`tier2_fail` 计数；熔断文案不套娃 |
| Q2 | `flow/02-bought-dialog-skip-first.patch` | core/engine.py `_bought_dialog` | 人工 | 「跳过」先读，判定集合不变 |
| Q3 | `flow/03-verify-frontload-gaps.patch` | core/engine.py 成交校验 | 人工 | 6 拍、gaps 0/0.1/0.1/0.2/0.35/0.75，总窗仍 1.5s |
| Q4 | `flow/05-close-reward-conditional.patch` | core/engine.py `_close_reward` + `_skip_shown`/`_list_back` | 人工 | 2.0/2.5s 仅为上界；窄带 60ms + 整屏兜底；无奖励痕迹零点击 |
| Q5 | `flow/06-scan-frame-memo.patch` | core/market.py | 人工 | 逐字节判等；兜底静止帧复用；`reset()` 同时清 memo |

- 未碰：`ocr/` 三条被否决线程/会话池补丁、`flow/04` 守卫预筛、`flow/01`（上下文过期）。
- `git diff --no-index` 逐块核对：合并后 engine/market 仅多出补丁 hunk，七功能段落无反向抹除；无「虚增 908 行」现象。
- **FakeMouse 既有缺陷**：主仓 `tests/test_engine_insufficient.py` 的桩已由并行会话补成 `**kw`；本次另补 5 处仍缺 `hold` 的 `FakeMouse.click`（`test_engine_nav`、`test_fix_b`、`test_nav_filter` ×2、`test_nav`、`test_nav_scroll`），统一加 `**kw`，产品代码零改动。

### 5.1 单件端到端耗时（`perf/bench/flow_bench.py`）

`flow_merged.json`（`--root _merged --run 40 --seed 20261107`）：

| 指标 | 补丁基线（`flow_base_final.json`） | 合并后 | 判定 |
|---|---|---|---|
| hit `fire_all` p50 | 2878.39 ms | **3743.90 ms** | < 5000 ms，补丁未失效 |
| `entry` p50 | 80.46 ms | 610.43 ms | +530 ms — 对应 flow/01（已退回） |
| `sub_guard_insufficient` p50 | 1239.91 ms | 2092.75 ms | +853 ms — 对应 flow/04（已砍） |
| `sub_close_reward` p50 | 152.54 ms | 142.37 ms | −10 ms，Q4 生效 |
| hit 固定 sleep | 0.827 s | 1.392 s | 差额即上述两条未入队补丁 |

结论：**12168 → 2916 的口径包含 flow/01 与 flow/04**（`_perf_flow.md` 原文「①②③⑤ + ④兜底」），而这两条按指示已退回；只带 Q1–Q5 的合并体理论下限即约 3.7s。合并后 3743.9 ms，未掉回 5000 ms 以上，Q4/Q5/Q2/Q3 均可从分阶段数据看到收益。

---

## 6. 修复明细

| # | 现象 | 根因 | 改动 | 验证 |
|---|---|---|---|---|
| 1 | 随机序下 `test_partial_line_...` 断言计数=1 得 2 | `core.records._recovery_state` 模块级累计，跨用例污染 | `tests/test_persist_hardening.py`：用例前 `monkeypatch.setattr(recmod, "_recovery_state", {"count":0,"last":None})` | 同种子复跑 2005 passed |
| 2 | 前端 toast 用例 10s 超时 | 并发/高负载下 POST 往返 >10s | 两处等待 `timeout=10000→30000`，断言不变 | 同种子复跑全绿 |
| 3 | `test_close_reward_clicks_skip_then_continue` 报「未确认回到列表」 | Q4 改为认卡片线判断回列表，旧桩的纯文字帧没有卡片线 | 测试内打桩 `engine_mod.cards.locate_cards` 返回 2 条线（断言未改） | 定向 3 文件复跑通过 |
| 4 | 5 个 `FakeMouse.click` 不接 `hold/fire` 关键字 | 测试桩未随 `_click` 新参数更新 | 统一加 `**kw` | 定向 nav/engine 用例通过；全量 2011 passed |
| 5 | `-n 4` 偶发 `test_bad_worker_path_marks_dead` | `FireProcess` 启动握手固定 50ms 后 poll，xdist 高负载下 3s 内未标 dead；合并前已存在 | **未改产品代码**，列入待决策 | 合并后低负载 `-n 4` 2011 passed |
| 6 | 首轮全量 1 failed（前端 30s 等待） | 机器上有并行会话跑基准，30s 内页面未就绪 | 未改代码 | 单跑 3.83s 通过；复跑全量 0 failed |

产品代码因「合并缺陷」改动：**0 行**。第 3、4 条只动测试桩；第 1、2 条属测试隔离；第 5 条为既存产品时序问题。

---

## 7. 待决策（语义冲突，未自行拍板）

1. **键数口径**：任务书写 20 键，实际 21（`stop.by_grade.enabled` 契约外但三表/前端俱全）。报告按 21 验收。
2. **click.burst 键名**：实现为 `hold_ms` / `interval_ms`，契约写 `duration_ms` / `gap_ms`；改键名会破坏已保存配置，需产品确认是否兼容别名。
3. **scan.repeat 默认**：实现 1..1（等同关闭），契约写 1..3。默认值影响「默认即随机」的预期。
4. **stop.by_grade.enabled**：契约未定义该总开关，实现有。保留则契约需补一条。
5. **config.py 标记嵌套**：`# feature:mouse.idle` 块嵌在 `# feature:mouse.human` 块内部，纯注释结构问题，不影响运行。
6. **flow/01 是否重写**：入口固定 `time.sleep 0.55s` 仍在（+530 ms）。若不重写，单件端到端稳定在 ~3.7s 而非 2.9s。
7. **flow/04 是否重开**：守卫 3 次整屏 OCR（+853 ms）。原判「真实弹窗命中 0/1」不适合作为放行依据，需要新素材再评。
8. **FireProcess 启动握手**：是否把 `sleep(0.05)+poll` 改为事件/管道握手，或把测试等待放宽。前者动产品时序，后者可能掩盖真实启动失败，需产品决策。
9. **OCR 二级自检的环境依赖**：`test_cooldown_lets_go_once_the_service_recovers` 在 rapidocr 可用/不可用时结论不同，CI 需固定后端开关。

---

## 8. 合并后仍存在的风险

- **并发会话改写**：验证期间其他会话持续改 `core/engine.py`、`core/input.py`，因此回归以冻结快照为准；交付前需再取一次工作树 hash。
- **`perf/base` 不是「只含已批准补丁」的快照**：它含 flow/01、flow/04，直接拿它做 A/B 会高估合并体收益（本报告已按分阶段拆开）。
- **入口 550ms 固定睡眠与守卫 3 次整屏 OCR** 仍在，合计约 1.38s；这是「退回补丁」的直接代价，不是合并缺陷。
- **前端 30s 等待**在 4 路并行高负载下仍可能偶发超时（本次 1 次，单跑 3.8s）；属测试基础设施，不是产品。
- **xdist 下 FireProcess 启动竞态**未修，`-n 4` 在重负载机器上仍可能偶发 1 条失败。
- **OCR 二级**：真实后端缺失时只能验证降级路径；`selfcheck_pp` 的 0.3s 探活预算在极端丢包网络下可能误报「未起」。
- **补丁基线缺失**：`cf532c70c48dfa8c` 不在本仓对象库，后续若有人基于旧副本再发补丁，仍可能反向抹掉并行改动；合并前务必逐 hunk diff。

---

## 9. 复现命令

```powershell
# 结构审计（三表 / 模块引用）
$env:MERGE_ROOT='C:\Users\26346\Documents\Trae-CN\temp_20261107_76\_merged'
.\.venv\Scripts\python.exe C:\Users\26346\Documents\Trae-CN\temp_20261107_76\_merge_audit.py

# AST / 前端静态审计
.\.venv\Scripts\python.exe C:\Users\26346\Documents\Trae-CN\temp_20261107_76\_merge_ast_audit.py

# 21 键三段联通（无头 Edge）
$env:MERGE_ROOT='...\_merged'
.\.venv\Scripts\python.exe C:\Users\26346\Documents\Trae-CN\temp_20261107_76\_merge_three_stage.py

# 互斥专项
.\.venv\Scripts\python.exe -m pytest <26 个节点> -q

# 全量
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest tests -q -n 4
.\.venv\Scripts\python.exe -m pytest tests -q --random-order --random-order-seed=20261107
.\.venv\Scripts\python.exe -m pytest tests -q --random-order --random-order-seed=424242

# 单件端到端（虚拟时钟台架）
.\.venv\Scripts\python.exe perf\bench\flow_bench.py --root "...\_merged" --run 40 --seed 20261107 --out flow_merged.json
```

---

## 10. 点击性能线收口（click/0001 + click/0002）

### 10.1 状态与本因

- 两条补丁在 `perf/base` 快照里**已应用**（`core/input.py` 有 `_opt_click/_ident/_abs_cache/_home_pending`，`core/interception.py` 有 `send_mouse_batch/gen/want_moves`），但主树 `core/input.py`、`core/interception.py` 里一个符号都没有——`perf/base` 与主树已分叉，原 patch 的上下文（`_perf_trace/_stage/mark()`）主树也不存在，逐行 `git apply` 不可能成功。
- 处理：以 `perf/base` 的两个文件为语义真源，把两条补丁的 hunk **逐段移植**进主树；`perf/base` 基线提交 `cf532c70c48dfa8c` 不在本仓对象库，无法用三方合并，因此以「落地前合并树快照 ↔ 落地后」的逐 hunk diff 作为核对依据。

### 10.2 diff 摘要（相对落地前的合并树快照）

| 文件 | 变更 | 关键内容 |
|---|---|---|
| `core/input.py` | +177 / −26 | `_init_click_perf()`（`_opt_click` 八项开关、`_ident/_ident_gen`、`_abs_cache`、`_watch`、`_home/_home_target/_home_pending`、`_perf_trace/_stage/_click_t0`）；`click()` 入口时间戳；`_click_driver` 改为：设备句柄代际缓存 → 坐标缓存 → 原点瞬态校正 → 整批提交（drop_move / down / up / 还原 stroke 同批）→ 观测 move+down+up → 还原只回读一次并按 `_home_pending` 交给下一击；`last_click` 增 `origin / restore_queued / stages` |
| `core/interception.py` | +55 / −3 | `self.gen`（设备代际，重枚举即 +1）；`self._batch_buf`（`MouseStroke*8` 复用）；`send_mouse_batch(strokes, device)` 一次 `interception_send` 提交整批；`wait_buttons(..., want_moves=0)` 追加 move 事件与 `moves/t_move/move_elapsed_ms` |

逐段核对结论：

- 删除行共 26+3 条，全部是「逐条 `send_mouse` + 定时 `SetCursorPos` 还原 + 旧 `wait_buttons` 签名」，**没有一条**属于七功能或 Q1–Q5。
- `feature:mouse.human` 的 **11 对** BEGIN/END 全部保持原位（此前后文写成「10 组」，实测锚点计数是 11 对，位置未动、只是数字写错）；`_emit_plan(path)` 与 `_wait_fire_deadline(0.0)` 仍在原点读取之后、提交之前，开火时序语义未变；非 `hold` 路径才走整批，连点 `hold_ms` 仍在 down→up 之间真实 `sleep`。
- 兼容点（非产品语义变更）：驱动无 `send_mouse_batch` 时自动退回逐条发送（测试桩/替代驱动），`drv.gen` 缺失时按 0 处理，`Mouse.__new__` 构造路径由 `_init_click_perf()` 懒补字段。

### 10.3 三项指标实测（合并树，真实驱动，基准 `perf/bench/click_perf.py --mode baseline --n 120`）

| 指标 | 补丁前（`perf/out/click_base_final.json`，n=300） | 合并树实测（n=120） | 判定 |
|---|---|---|---|
| 点击占用 p50 | 0.562 ms | **0.250 ms**（p95 0.458 / max 2.275）<br><sub>安静机器单次采样，非稳定基线；带并发负载时同一代码 p50 可达 0.28–0.73 ms，摆动 2.6 倍且与负载同步（证据见 `_click_cost_20261107.md`）</sub> | 达标（目标 ≤0.320） |
| 端到端 p50 | 601 µs | **283.1 µs**（p95 613.1 / mean 344.0） | 达标（目标 ≤366） |
| 还原失败 | 1/300（0.33%） | **0/120（0%）**；`click_errors` 0 | 达标 |
| 还原确认 | — | 5/120 立即回读命中，115 条 `restored=None`＝回原点 stroke 已排队、未回读（`restore_queued=True`，下一击 `_home_pending` 复核） | 按设计 |

> 说明：`restored=None` 是 `restore_early` 的既定语义（位置更新比 Raw Input 事件晚 0.3–0.6ms），不是失败；定义上的「还原失败」为 `restored is False`，本机 0 例。基准按设计只点桌面安全点并在结束时还原光标。

> **口径更正（2026-11-07）**：本表的 `0.250 ms` 与「目标 ≤0.320」都是**占用时间**口径，
> 已作废（安静机器单次采样 / 无分辨力阈值）。点击项主指标改为 `click_core_us`
> （组装 + `send_mouse_batch` DLL 调用，p50 7.8–8.1 µs、与负载无关），
> 见 `_perf_flow.md` §11。本表数值保留作历史记录，不得用于判定优劣。

### 10.4 全量回归（落地后）

| 轮次 | 命令 | 结果 |
|---|---|---|
| 单进程 | `pytest tests -q`（冻结快照） | 2091 passed / 19 skipped / 2 xfailed / **2 failed**，530.55s；两条失败均非产品：① `test_release_pipeline::test_version_single_source`（快照未含 `build/version_info.txt`，属我排除 `build` 所致）；② `test_feature_mouse_human::test_frontend_control_reaches_backend`（Playwright 30s 超时，并行会话重负载）。补齐文件并单跑 → **2 passed in 4.68s** |
| `-n 4` | `pytest tests -q -n 4`（同一冻结快照，已含 `version_info.txt`） | **2093 passed / 19 skipped / 2 xfailed / 0 failed**，180.01s |

### 10.5 `.gitignore` 新增（只忽略，未删任何文件）

```
# 本地测试/基准产物：会持续堆积，只忽略不删除（证据留在工作树里）
/_rm_backup/
/perf/out/
/_soak_data/
/perf/bench/out/
/evo/runs/
```

`git check-ignore -v` 五条全部命中（`.gitignore:76-80`）；五个目录 `git ls-files` 均为 0，说明此前确未跟踪，加规则不影响任何已跟踪文件。

### 10.6 `evo/` 依赖确认

- 运行链路：`desktop.py`、启动脚本、`pyproject.toml` 中**没有任何** `import evo` / `from evo`；全仓 `from|import evo` 仅出现在 `evo/` 内部（`evo/collect/*`）。
- 打包：`sjz.spec` 与 `sjz-onefile.spec` 通过 `_evo_datas()` 把 `evo/` 源码与 `evo_config.json` 作为 **datas** 附带（为 `<runtime> -m evo.cli` 用），不进 import 依赖图。
- 结论：符合「没进依赖树」；未新增任何依赖，保持原样。

