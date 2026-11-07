# 自进化优化闭环（EVO v1.0.0）

> 位置：`dafors/evo/`，入口 `python -m evo.cli`。
> 取数只认两个事实源：`data/records.jsonl` 与 `data/telemetry/telemetry.db`。
> 只读现有代码与数据，不写 `core/`、`web/`、`tools/`、`data/`，不接入真实购买链路。

---

## 1. 边界与不变量

| 约束 | 落地方式 |
|---|---|
| 独立目录 | 全部代码在 `evo/`，不改任何现有模块；参数改动只写 `evo/state/params_overlay.json`，不写 `core/config.json` |
| 两事实源 | 观测层只打开台账与遥测库；台账按字节偏移增量读，遥测库以只读 URI `mode=ro` 按 `seq` 水位读 |
| 不重复读 | 游标 `evo/state/cursors.json`：`ledger.offset/rows`、`telemetry.last_seq/events` |
| 不开窗口 / 不动鼠键 | 无 GUI 调用、无 `pyautogui` 类依赖；影子级只做文件读取 |
| 不调用真实购买 | `experiment.real_plan()` 固定 `status=blocked`，需人工注入 `EVO_REAL_EXECUTOR` 才能启用 |
| 不 commit / 不 push | 运行全程只做文件读写，无 git 操作 |

---

## 2. 目录与模块

```
evo/
├─ evo_config.json        阈值与开关（所有判据的唯一来源）
├─ config.py              配置深合并 + 环境变量覆盖
├─ common.py              原子写、jsonl 读写、Wilson/P95/均值/签名
├─ observe.py             观测层：增量读两源、合并去重、日聚合、replay 缓存、健康度
├─ funnel.py              漏斗层：五段计数、段间流失、滚动窗口、按月/模式分段
├─ attribute.py           归因层：瓶颈定位 + 7 类候选因子（带支撑数据）
├─ hypothesize.py         假设层：3 类模板、四要素校验、最小样本、冷静期
├─ guardrails.py          决策层护栏 G1~G5（硬否决）
├─ decide.py              Thompson Sampling + 增益排序 + 护栏过滤
├─ experiment.py          实验层：离线回放 → 影子模式 → 真机（默认禁用）
├─ registry.py            沉淀层：registry.jsonl 追加写 + 文件锁 + 签名索引
├─ rollback.py            回滚层：promote / 灰度递减 / 超阈值自动回退
├─ daemon.py              长任务守护：9 阶段、checkpoint、崩溃留痕、续跑
├─ selftest.py            确定性夹具自检（23 项断言）
├─ cli.py                 命令入口
├─ runs/<时间戳>/         每轮产物（00~08 + summary.md）
└─ state/                 游标、日聚合、replay、checkpoint、参数覆盖层
```

产物编号约定：`00_observation.json`、`01_funnel.json`、`02_attribution.json`、`03_hypotheses.json`、`04_decision.json` + `04_vetoes.jsonl`、`05_experiments.json`、`06_registry.json`、`07_rollback.json`、`08_summary.json` + `summary.md`。

---

## 3. 一轮的 9 个阶段

`PHASES = observe → funnel → attribute → hypothesize → decide → experiment → settle → rollback → report`

每完成一个阶段：写该阶段产物 → 追加到 `checkpoint.phases_done` → 原子落盘 `state/checkpoint.json`。续跑时跳过 `phases_done` 中已有的阶段，直接复用其产物 JSON。

---

## 4. 观测层

### 4.1 台账（`data/records.jsonl`）

- 按**字节偏移**读取：`offset` 之后的字节才消费；轮转（文件变小）自动把 offset 归零。
- 尾部半行不消费，offset 只推进到最后一个完整换行，避免把半截 JSON 记成非法行。
- 单批上限 `max_batch_rows=5000`，一轮不清空积压以外的新数据。
- 行级异常计数：`invalid_json`、`missing_ts`、`unknown_result`、`name_too_long`（name 长度 > 128 字符）、`future_ts`（时间戳超前 > 容差）。
- 正常化字段：`ts/day/name/tier/result/verified/elapsed/mode/skew_ms/skip_reason`。

### 4.2 遥测（`data/telemetry/telemetry.db`）

- `sqlite3.connect("file:<path>?mode=ro", uri=True)`，只读打开，绝不写库。
- `SELECT ... FROM events WHERE seq > ? ORDER BY seq LIMIT ?`，水位存 `last_seq`。
- 库缺失 → `telemetry=absent`；表缺失 → `missing_schema`；两种情况都不致命。
- 台账缺失时，用遥测的 `result` 字段回退构造 attempts（保证单源也能出漏斗）。

### 4.3 合并与去重

- `discover = 台账 attempts + 遥测中的 skip 事件`。
- 遥测 skip 与台账行按 `(name, 秒级时间戳)` 去重，避免同一机会被记两次。
- 每条明细追加进 `state/replay.jsonl`（上限 2000），供离线回放使用；离线回放只读这个自有缓存，不再回读事实源。

### 4.4 日聚合与健康度

- 按天累积到 `state/rollup.json`：`counts / blocked / skip / unverified_hits / elapsed / skew / by_mode / skip_reasons`。
- `health = {ledger, telemetry, rows_read, invalid_rows, suspicious_rows, health_ratio, anomaly_ratio, anomalies, replay_rows}`。
- `health_ratio = 有效行 / 总行`；`anomaly_ratio = 可疑行 / max(1, 总行)`。`ledger_health` 因子取两者中较大的失效度。

---

## 5. 漏斗口径（五段）

| 段 | key | 判定条件（与台账/遥测字段一一对应） |
|---|---|---|
| 发现 | `discover` | 台账行数 + 遥测中独立的 skip 事件；`aborted` 只记发现、不计点击 |
| 点击 | `click` | `result ∈ {hit, miss, no_money, over_budget}`（真的发起了抢） |
| 进池 | `pool` | `result ∈ {hit, miss}`（没被额度/预算挡在门外） |
| 中签 | `hit` | `result = hit` |
| 成交 | `deal` | `result = hit` 且 `verified = True` |

每段输出：`count`、`rate_of_discover`、`step_rate`（本段→下一段）、`loss_count = max(0, n - n_next)`、`loss_rate = 1 - step_rate`、`wilson_low`（Wilson 95% 下界，用于判断样本是否够）。
统计窗口默认最近 7 天（`funnel.window_days`），同时输出本轮增量漏斗（`batch`）与窗口漏斗（`window`），以及按 `mode`、按天的分段。

---

## 6. KPI 定义

| KPI | 公式 | 含义 |
|---|---|---|
| `click_rate` | click / discover | 发现后真正发起抢的比例 |
| `pool_rate` | pool / click | 点击后进入候选池（未被额度/预算拦截） |
| `hit_rate` | hit / pool | 进池后中签 |
| `settle_rate` | deal / hit | 中签后完成校验结算 |
| `end_to_end_rate` | deal / discover | 端到端成交率（核心目标） |
| `block_rate` | blocked / click | 额度/预算拦截占比 |
| `refresh_skip_rate` | skip / discover | 刷新预算耗尽（未解锁/来不及）占比 |
| `unverified_hit_rate` | unverified_hits / hit | 中签但未校验结算占比（记账风险） |
| `elapsed_p50/p95_s` | 耗时分位 | 队列/接口延迟 |
| `skew_p95_ms` | 卡点误差分位 | 定时偏差 |

---

## 7. 归因层

**瓶颈选择**：在 `discover→click→pool→hit→deal` 四段中，取 `lost = n - n_next > 0` 且 `(lost, loss_rate)` 字典序最大的一段。

**候选因子**（每轮输出 top_k=6，按 `score = effect × min(n,50)/50` 排序）：

| 因子 | 段 | effect 计算 | 支撑数据 |
|---|---|---|---|
| `refresh_exhaust` 刷新预算耗尽 | discover→click | skip / discover | 跳过数/发现数 + 原因分布 |
| `quota_block` 额度/预算拦截 | click→pool | blocked / click | 拦截数/点击数 |
| `ui_detect` 界面判定失败 | pool→hit | 1 - hit / pool | 进池数/未中签数 |
| `queue_delay` 队列延迟 | pool→hit | clamp((P95 - 3s) / 3s) | 耗时 P95 / 基线 / 样本数 |
| `timing_skew` 时序偏差 | pool→hit | clamp((\|skew P95\| - 120ms) / 120ms) | 卡点误差 P95 / 基线 |
| `settle_verify` 结算确认缺失 | hit→deal | unverified / hit | 中签中未校验条数 |
| `ledger_health` 台账健康度 | data | max(1-health_ratio, anomaly_ratio) | 非法行 / 可疑行 / 异常码样例 |

置信度按证据量分档：`n ≥ 30 → high`，`n ≥ 10 → medium`，其余 `low`。
若瓶颈段样本 < 10（`funnel.min_stage_n`），`evidence_sufficient=false` 并在 `note` 标注"样本不足，归因仅作方向性提示"。

---

## 8. 假设层

**三类模板**（factor → 参数改动）：

| 类别 | factor | 参数 | 指标 | 最小可测收益 |
|---|---|---|---|---|
| 策略级 | `refresh_exhaust` | `list_max_tries + 8` | deal_rate | +0.04 |
| 策略级 / 参数级 | `quota_block` | `evo.policy.requota False→True` / `stop_after_fails 0→2` | deal_rate / block_rate | +0.05 / -0.03 |
| 策略级 / 参数级 | `ui_detect` | `evo.policy.double_check False→True` / `click.burst.duration_ms 100→140` | hit_rate | +0.05 / +0.04 |
| 时序级 | `queue_delay` | `countdown_min_lead_s 6→7` | hit_rate | +0.04 |
| 时序级 | `timing_skew` | `fire_delay_ms 0→30` | skew_p95_ms | -0.05 |
| 策略级 | `settle_verify` | `evo.policy.settle_gate False→True` | deal_rate | +0.10 |

**四要素（缺一项即丢弃）**：
1. `expected_direction`：可证伪方向 + `min_delta`；
2. `min_sample`：两比例检验近似（双侧 α=0.05、power=0.8），下限 30；
3. `stop_condition`：`max_rounds=3`、`max_seconds=86400`、`futility_bound.min_effect=δ/4`、`abort_on_guardrail_veto=true`；
4. `expected_gain`：`abs_delta`、`per_100_opportunities`、`basis`（引用归因支撑原文）。

**过滤顺序**：`effect ≤ 0.005` 跳过 → 模板字段完整性 → `REQUIRED` 字段非空 → 类别合法性 → `min_sample ≥ 30` → 签名冷静期 → 收益为正 → 按收益降序取前 5。
**签名** `sig16 = sha1(class, param, from, to, metric, direction)[:16]`，用于"同类失败不重复尝试"。

---

## 9. 实验层：三级递进

| 级别 | 做法 | 成本 | 通过判据（全过才晋级） |
|---|---|---|---|
| 离线回放 | 用 `state/replay.jsonl` 历史行与日聚合重跑 | 0 | `replay_rows ≥ 30`、`n ≥ 计划样本`、`direction` 一致、`delta ≥ min_delta`、`CI95 下界 > 0`、护栏全过 |
| 影子模式 | 只读 `data/<8位hex>/` 会话帧（`buy.png`、`confirm.png`），只读不点 | 0 | 可用会话 > 0、决策覆盖率 ≥ 90%、读取延迟 P95 ≤ 350ms、副作用 0 次 |
| 小样本真机 | 只产计划，硬禁用 | — | 需人工注入 `EVO_REAL_EXECUTOR`；`max_runs ≤ 5`、`abort_after_s=180`、禁止抢前台、禁止购买链路 |

任一闸门失败 → 后续级别置 `skipped`，本轮结论 `inconclusive`，并写 `failure_reason`。
影子级实际读的是真实目录，但只做 `open().read(4096)`，不产生点击、不写业务状态。

---

## 10. 决策层

**选样**：Thompson Sampling。每个签名维护 Beta 后验 `(α, β)`，`α = 1 + 成功数`、`β = 1 + 失败数`；采样值 × 预期增益 = 打分，按分数降序取 `batch_size=2`。

**硬护栏（任一破即否决，理由写 `04_vetoes.jsonl`）**：

| 码 | 约束 | 触发条件 |
|---|---|---|
| `G1_NO_PURCHASE` | 不误买 | 实验动作含 `submit_order / buy / confirm_purchase / real_click`，且 `allow_real_purchase=false` |
| `G2_BUDGET_QUOTA` | 不越预算与额度 | 参数为 `budget/quota/console.*` 且目标值 > `budget_cap`(0) / `quota_cap`(1) |
| `G3_INPUT_IDENTITY` | 输入身份不降级 | 参数含 `input` 且目标值为 `virtual/message/legacy/软件模拟`，或打开 `input_allow_virtual` |
| `G4_SKEW_P95` | 卡点误差 P95 不变差 | 投影 `skew_p95_ms > 基线 + 0.5ms` |
| `G5_FOREGROUND` | 不抢前台 | 动作/风险标记含 `foreground / focus_window` |

否决的实验不进入实验层，但仍会写登记册（结论 `rejected` + 否决码），形成"失败不重复"的依据。

---

## 11. 沉淀层：`evo/registry.jsonl`

追加写 + 文件锁（`state/registry.lock`，30s 僵尸锁自动回收）。每条字段：

| 字段 | 说明 |
|---|---|
| `schema` | `evo-registry/1` |
| `ts` / `run_id` / `round` | 时间、轮次、运行标识 |
| `experiment_id` | `EXP-<signature>-<run 尾部 8 位>` |
| `hypothesis` | id / class / title / factor / metric / expected_direction / expected_gain |
| `params` | name / from / to / unit |
| `sample_size` | `planned` + `observed`（如 `{"n": 1}`） |
| `metric_before` / `metric_after` | 指标名 + 实测值（实测优先，模型值只作参考） |
| `conclusion` | `accepted / canary / rejected / rolled_back / inconclusive` |
| `failure_reason` | 闸门失败码或否决码 |
| `level` | `offline / shadow / real_pending / real` |
| `signature` | 16 位签名 |

**同类失败不重复尝试**（`registry.blocked`）：
- 结论 `accepted` / `canary` → 永久拦截；
- 结论 `rejected` / `rolled_back` 且未过 `failure_cooldown_days=7` → 拦截；
- 结论 `inconclusive` 且本轮样本 < 上次样本 1.5 倍 → 拦截（证据没有实质增长，不重复消耗轮次）。

---

## 12. 回滚层

1. **晋级**：实验 `final.status = canary_candidate` → `promote()` 写 `state/params_overlay.json`，记录 `current`（新值/旧值）、`baseline`（指标、方向、样本、时间）、`canary.cycles_left = 2`。
2. **灰度观察**：每轮 `observe()` 递减 `cycles_left`；未到 0 时只落盘不退。
3. **自动回退**：到 0 时比较当前窗口指标与基线，`degrade_rel` 超 `0.05` 且样本 `n ≥ 10` → 参数恢复为 `prev`，写 `history[{event:"rollback", baseline, current, degrade_rel, threshold, n}]`，并登记 `rolled_back`。
4. 未劣化 → 写 `history[{event:"canary_ok"}]`，清空灰度标记，参数转为正式值。

---

## 13. 长任务守护与断点续跑

- 间隔 `daemon.cycle_interval_s = 86400`（24h），单轮上限 1 次（`max_rounds_per_cycle`）。
- 每轮开始：新建 `runs/<YYYYMMDDTHHMMSS>/`，写 `checkpoint.status=running`、`phases_done=[]`、`cycle_index`。
- 每阶段完成：`_done()` 追加阶段名并原子写 checkpoint。
- 崩溃：捕获任何异常 → `status=crashed`、`crash_count+1`、`crash_error` 写 checkpoint，同时写 `runs/<ts>/crash.json`。
- 续跑：下次 `run_round(resume=True)` 读 checkpoint，`status ∈ {running, crashed}` 且 `run_dir` 存在 → 复用同一 run 目录与 `run_id`，跳过已完成阶段，从断点继续；产物文件名不变，`resumed=true`。
- 并发保护：`state/daemon.lock` + `lock_timeout_s=5`。
- 轮次调度状态：`state/daemon.json`（`cycle / last_run_id / last_round_at / next_round_at`）。

**运维命令**

```bash
# 一轮 dry 跑（真实台账，默认从 checkpoint 续）
python -m evo.cli dryrun

# 强制新一轮，忽略旧 checkpoint
python -m evo.cli dryrun --fresh

# 守护单轮 / 崩溃后自愈单轮 / 24h 循环（最多 2 轮便于观察）
python -m evo.cli cycle --once
python -m evo.cli cycle --once --fault attribute --auto-heal
python -m evo.cli cycle --loop --max-cycles 2

# 查看游标 / checkpoint / 登记册摘要
python -m evo.cli status

# 夹具自检（确定性数据，验证漏斗数学、护栏、回滚、续跑）
python -m evo.cli selftest
```

环境变量覆盖：`EVO_LEDGER_PATH`、`EVO_TELEMETRY_DB`、`EVO_STATE_DIR`、`EVO_RUNS_DIR`、`EVO_REGISTRY`（自检用它隔离到 `evo/fixtures/`）。

---

## 14. 关键阈值（`evo/evo_config.json`）

| 组 | 键 | 值 |
|---|---|---|
| observation | `max_name_bytes` / `max_batch_rows` | 128 / 5000 |
| funnel | `window_days` / `min_stage_n` | 7 / 10 |
| attribution | `top_k` / `min_evidence_n` | 6 / 10 |
| attribution | `skew_p95_baseline_ms` / `queue_p95_baseline_s` | 120 / 3.0 |
| hypothesis | `max_per_round` / `min_sample_floor` / `failure_cooldown_days` | 5 / 30 / 7 |
| hypothesis | `offline_min_rows` | 30 |
| hypothesis | `shadow_min_coverage` / `shadow_max_p95_ms` / `shadow_max_sessions` | 0.9 / 350 / 60 |
| guardrails | `budget_cap` / `quota_cap` / `skew_p95_max_ms` | 0 / 1 / 120 |
| guardrails | `allow_foreground` / `allow_real_purchase` / `allow_input_downgrade` | false / false / false |
| decision | `batch_size` / `exploration` | 2 / 1.0 |
| rollback | `degrade_rel_threshold` / `min_obs` / `canary_cycles` | 0.05 / 10 / 2 |
| daemon | `cycle_interval_s` / `replay_buffer_max` / `sample_cap` | 86400 / 2000 / 2000 |

---

## 15. Dry 跑取证

### 15.1 真实台账轮 1（`run_id = 20261107T084412`）

事实源状态：`data/records.jsonl` 仅 1 行；`data/telemetry/telemetry.db` 不存在（`telemetry=absent`）。
游标：`ledger.offset=1048742`、`rows=1`、`telemetry.last_seq=0`。

**观测健康度**

```
ledger=ok, telemetry=absent, rows_read=1, invalid_rows=0, suspicious_rows=1,
health_ratio=1.0, anomaly_ratio=1.0,
anomalies=[{"code":"name_too_long","detail":"1048576 chars > 128"}]
```

**五段漏斗（滚动 7 天窗口，原样输出）**

| 段 | 样本量 | 占发现比 | 段间转化 | 流失数 | 流失率 |
|---|---|---|---|---|---|
| 发现 | 1 | 100.00% | 100.00% | 0 | 0.00% |
| 点击 | 1 | 100.00% | 100.00% | 0 | 0.00% |
| 进池 | 1 | 100.00% | 100.00% | 0 | 0.00% |
| 中签 | 1 | 100.00% | 0.00% | 1 | 100.00% |
| 成交 | 0 | 0.00% | - | - | - |

**KPI**：点击 100.00% / 进池 100.00% / 中签 100.00% / 结算 0.00% / 端到端 0.00%；额度拦截 0.00% / 刷新跳过 0.00% / 未校验中签 100.00%。

**瓶颈**：`hit → deal`，流失 1（流失率 100.00%，样本 1）。

| 候选因素 | 段 | 效应 | 证据样本 | 置信 | 支撑数据 |
|---|---|---|---|---|---|
| 结算确认缺失 | hit→deal | 1.0000 | 1 | low | 中签 1 中 1 条未校验结算（verified 缺失/False） |
| 台账数据健康度 | data | 1.0000 | 1 | low | 非法行 0，可疑行 1，异常样例 ['name_too_long'] |
| 刷新预算耗尽 | discover→click | 0.0000 | 1 | low | 跳过 0/1，原因分布 {} |
| 额度/预算拦截 | click→pool | 0.0000 | 1 | low | 额度/预算拦截 0/1 |
| 界面判定失败 | pool→hit | 0.0000 | 1 | low | 进池 1 中未中签 0（界面判定/弹窗未出现） |

**假设（1 条，通过四要素校验）**

```
[策略级] 成交记账前强制结算校验，杜绝未验证成交
参数：evo.policy.settle_gate: False -> True
预期：deal_rate up >= 0.10
最小样本：88
停止条件：max_rounds=3, max_seconds=86400, futility.min_effect=0.025, 护栏否决即中止
预期收益：+10.0 / 100 次机会
签名：7a245c81fd6a982e
```

**实验：离线回放失败（结论 `inconclusive`）**

| 闸门 | 结果 | 明细 |
|---|---|---|
| `replay_rows` | 失败 | 历史行 1 / 要求 ≥ 30 |
| `power_n` | 失败 | 1 / 计划样本 88 |
| `direction` | 通过 | 估计增益 +0.1640（up） |
| `min_delta` | 通过 | 0.1640 / 要求 ≥ 0.1 |
| `ci_low_positive` | 失败 | 95% 下界 -0.6042 |
| `guardrails` | 通过 | 护栏全过 |

`failure_reason = "gate_failed:replay_rows,power_n,ci_low_positive"`；影子级 `skipped`（offline_not_passed），真机级 `skipped`（shadow_not_passed）。

**登记册**（实际写入 `evo/registry.jsonl`，1 条）

```json
{"schema":"evo-registry/1","ts":1794012252.817,"run_id":"20261107T084412","round":1,
 "experiment_id":"EXP-7a245c81fd6a982e-7T084412",
 "hypothesis":{"id":"H-7a245c81fd6a982e-1","class":"策略级",
   "title":"成交记账前强制结算校验，杜绝未验证成交","factor":"settle_verify",
   "metric":"deal_rate",
   "expected_direction":{"metric":"deal_rate","direction":"up","min_delta":0.1},
   "expected_gain":{"metric":"deal_rate","abs_delta":0.1,
     "per_100_opportunities":10.0,"basis":"中签 1 中 1 条未校验结算（verified 缺失/False）"}},
 "params":{"name":"evo.policy.settle_gate","from":false,"to":true,"unit":"bool"},
 "sample_size":{"planned":88,"observed":{"n":1}},
 "metric_before":{"metric":"deal_rate","value":0.0},
 "metric_after":{"metric":"deal_rate","value":0.174},
 "conclusion":"inconclusive",
 "failure_reason":"gate_failed:replay_rows,power_n,ci_low_positive",
 "level":"offline","signature":"7a245c81fd6a982e"}
```

三条闭环证据成立：**漏斗能算**（五段齐、流失率齐）、**假设能生成**（四要素齐、最小样本由功效公式给出）、**登记册能写**（契约字段齐、失败原因落盘）。

### 15.2 增量轮 2（`run_id = 20261107T084417`）

- 台账游标未变（offset 仍为 1048742，无新增行），遥测仍为 `absent` → 本轮增量 0。
- 归因与轮 1 相同；假设层命中同签名 `7a245c81fd6a982e` 的冷静期拦截：

```json
{"hypotheses":[],"dropped":[],
 "duplicates":[{"signature":"7a245c81fd6a982e",
   "title":"成交记账前强制结算校验，杜绝未验证成交",
   "reason":"同类失败冷静期内"}]}
```

- 登记册仍为 1 条、签名 1 个 → 证明"同类失败不重复尝试"生效。

### 15.3 崩溃自恢复（`run_id = 20261107T084426`）

命令：`python -m evo.cli cycle --once --fault attribute`（第一轮注入崩溃）→ 再跑 `cycle --once`（续跑）。

```
checkpoint（崩溃时）: status=crashed, crash_count=1,
  crash_error="InjectedFault: injected_fault_at_attribute",
  phases_done=["observe","funnel"]
checkpoint（续跑后）: status=done,
  phases_done=["observe","funnel","attribute","hypothesize","decide",
               "experiment","settle","rollback","report"]
```

`summary.md` 头部：`断点续跑：是；崩溃次数：1`。续跑复用同一 `runs/20261107T084426/` 目录，产物编号连续无缺号。

### 15.4 夹具自检（确定性数据，23/23 PASS）

自检数据：3 天 × 600 attempts（1800 行台账）+ 每天 300 条 skip 遥测（共 900），确定性构造。

**漏斗（`runs-main-*/summary.md` 原样）**

| 段 | 样本量 | 占发现比 | 段间转化 | 流失数 | 流失率 |
|---|---|---|---|---|---|
| 发现 | 2700 | 100.00% | 66.67% | 900 | 33.33% |
| 点击 | 1800 | 66.67% | 94.00% | 108 | 6.00% |
| 进池 | 1692 | 62.67% | 16.61% | 1411 | 83.39% |
| 中签 | 281 | 10.41% | 100.00% | 0 | 0.00% |
| 成交 | 281 | 10.41% | - | - | - |

KPI：点击 66.67% / 进池 94.00% / 中签 16.61% / 结算 100.00% / 端到端 10.41%；拦截 6.00% / 刷新跳过 33.33% / 未校验中签 0.00%。瓶颈 `pool → hit`（流失 1411，83.39%）。

**归因候选（top 4）**

| 候选 | 段 | 效应 | 证据样本 | 置信 |
|---|---|---|---|---|
| 队列延迟 | pool→hit | 1.0000 | 1692 | high |
| 界面判定失败 | pool→hit | 0.8339 | 1692 | high |
| 时序偏差 | pool→hit | 0.4000 | 1800 | high |
| 刷新预算耗尽 | discover→click | 0.3333 | 2700 | high |

**5 条假设（三类齐备，均为上限内）**

| 类别 | 标题 | 参数改动 | 指标方向 | 最小样本 | 签名 |
|---|---|---|---|---|---|
| 策略级 | 场景守卫二次校验，压掉「弹窗没出现」 | `evo.policy.double_check False→True` | hit_rate up ≥0.05 | 970 | `09f8a7e02fc6b0ad` |
| 时序级 | 按卡点误差中位数做相位补偿 | `fire_delay_ms 0→30` | skew_p95_ms down ≥0.05 | 30 | `d742f9c8f4be9e5c` |
| 策略级 | 开抢前复核额度与余额，避免无效点击 | `evo.policy.requota False→True` | deal_rate up ≥0.05 | 705 | `4c9cc429f2342925` |
| 时序级 | 提前进入倒计时等待，摊薄队列延迟 | `countdown_min_lead_s 6→7` | hit_rate up ≥0.04 | 1485 | `0616cec5f36a961a` |
| 参数级 | 点击 burst 时长略增，覆盖渲染抖动 | `click.burst.duration_ms 100→140` | hit_rate up ≥0.04 | 1485 | `2ca3888397af6e5f` |

**三级闸门**

- `H-d742f9c8f4be9e5c-1`：离线 passed（n=2000，Δ=0.0798）→ 影子 passed → 结论 `canary_candidate`
- `H-0616cec5f36a961a-1`：离线 passed（n=2000，Δ=0.0566）→ 影子 passed → 结论 `canary_candidate`
- 其余三条离线未过闸，影子/真机 `skipped`；两条晋级的真机级均为 `blocked`（`real_machine_disabled`）。

**登记册（夹具，2 条）**

```json
{"experiment_id":"EXP-d742f9c8f4be9e5c-7T084406","params":{"name":"fire_delay_ms","from":0,"to":30},
 "metric_before":{"metric":"skew_p95_ms","value":168.0},
 "metric_after":{"metric":"skew_p95_ms","value":154.6},
 "conclusion":"inconclusive","failure_reason":"real_stage_disabled:awaiting_operator",
 "level":"real_pending","signature":"d742f9c8f4be9e5c"}
{"experiment_id":"EXP-0616cec5f36a961a-7T084406","params":{"name":"countdown_min_lead_s","from":6,"to":7},
 "metric_before":{"metric":"hit_rate","value":0.1661},
 "metric_after":{"metric":"hit_rate","value":0.2227},
 "conclusion":"inconclusive","failure_reason":"real_stage_disabled:awaiting_operator",
 "level":"real_pending","signature":"0616cec5f36a961a"}
```

**23 项断言全部 PASS**（覆盖：一轮跑通、五段单调不增、发现样本 > 300、每段有流失率、瓶颈已定位；假设 ≤ 5、四要素齐全、类别合法；离线已执行、通过后才进影子、真机未执行；登记册已写入且字段齐全、冷静期拦截；G1/G2/G5 否决；注入崩溃留痕、checkpoint 阶段正确、续跑成功且复用原目录；劣化超阈值自动回滚并恢复上一版参数）。

回滚断言的实测值：基线 `hit_rate=0.5` → 劣化后 `0.1`，`degrade_rel=0.8 > 0.05`，`n=100 ≥ 10`，灰度 2 轮后自动回退，`fire_delay_ms` 恢复为 `0`。

---

## 16. 数据健康与当前限制

1. `data/records.jsonl` 只有 1 行，且 `name` 字段为 1,048,576 字符的 `x` 填充 → 触发 `name_too_long`，`suspicious_rows=1`。该行 `result=hit`、`verified` 缺失，造成 `hit→deal` 100% 流失与 `unverified_hit_rate=100%`。
2. `data/telemetry/telemetry.db` 不存在 → `telemetry=absent`，本轮回退到单源（台账）口径，遥测侧 skip 与 `skew_ms` 均为 0 样本。
3. 因此真实 dry 跑是 `degraded` 模式：漏斗数学成立、归因方向成立，但样本量 1 使离线闸门必然失败，结论按规则落为 `inconclusive`——这是正确行为，不是框架缺陷。
4. 夹具自检在同一套代码上给出 2700/1800/1692/281/281 的完整链路，证明机器本身可用。

---

## 17. 不在本闭环取数口径内的目录

`evo/collect/`、`evo/diagnose/` 是并行功能分支的产物，读取 `_soak_data` 日志，超出"只有两个事实源"的口径，**未接入**本闭环（`observe.py` 不引用它们）。它们与本框架共用 `evo/` 顶层目录，但不共享状态与登记册；本框架不写、不读它们的文件。

---

## 18. 下一轮怎么跑

1. 补数据：让台账持续追加，或把遥测库落到 `data/telemetry/telemetry.db`（表 `events` 需含 `seq, occurred_at, name, tier, price, result, skew_ms, skip_reason, client_version`）。
2. 清掉 1MB `name` 的可疑行来源（属于 `core/` 侧问题，本框架只标记不改动）。
3. 跑到窗口内 `discover ≥ 30`：离线闸门自动可过，`settle_verify` 假设会重新进入实验层。
4. 常驻：`python -m evo.cli cycle --loop`（24h 一轮，崩溃自动从 checkpoint 续跑）；每轮看 `evo/runs/<时间戳>/summary.md`。
5. 晋级后盯灰度：`state/params_overlay.json` 的 `canary.cycles_left` 到 0 时自动判定，劣化超 5% 回退并在 `07_rollback.json` 留痕。
