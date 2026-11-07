# Evo Round 1 — 入池漏斗首轮闭环

- 轮次：R1（框架上线后第一次真正跑完 observe→funnel→attribute→hypothesize→experiment→decide→registry）
- 日期：2026-11-07
- 基线：R0 `core_digest=389bcc0e8bbab821`（`_baseline_r0.md`）；本轮实测工作树 `core_digest=3479365394c5865e`（core/ 有 49 个并行分支脏文件，本轮未触碰 core）
- 冻结快照：`%TEMP%\dafors_rounds\r1\base_snap`（改动前的 `evo/*.py` + `evo_config.json`）
- 结论：**accepted（观测级）**。改进对象是「漏斗口径与证据接入」，不是点击策略；未产生也未声称任何游戏侧行为增益。

---

## 1. 数据源审计：真实台账在哪，哪些不能用

| 数据源 | 路径 | 实测状态 | 能否用于本轮漏斗 |
|---|---|---|---|
| tuning 快照 | `tuning_recommendation.json`（2026-11-07 08:03:49） | 137 行 / 128 hit + 9 miss；128 个精确 hit 时间戳；9 个 miss 只有小时桶；字段只有 `name/price/result/ts` | **是**，唯一可聚合的真实台账快照 |
| 当前台账 | `data/records.jsonl` | 1 行 / 1,048,742 字节 / name 长度 1,048,576（写盘测试残留） | 否，与快照生成时的 137 行不一致 |
| 遥测库 | `data/telemetry/telemetry.db` | 目录不存在 | 否（无数据） |
| 特性遥测 | `data/feature_telemetry/` | 不存在 | 否（无数据） |
| toast 旁路 | `evo/collect/inbox/20261107.jsonl` | 1 条真实 toast：`draw`，点数 99 / 125，won=false，ts=1794039547000 | 可读，但**挂不上任何 attempt**（无 attempt_id、无同名邻近记录） |
| collect 派生 | `evo/collect/events.jsonl` | 1 行，由上面那条 1 行残留 records 派生 | 否（非真实新增） |

审计结论：`tuning_recommendation.json` 是唯一可用真实数据；其余落点要么不存在、要么是测试残留。本轮任何环节缺数据一律写「无数据」，没有用估算补齐。

审计产物：[source_audit.json](file:///C:/Users/26346/Desktop/project/dafors/evo/runs/round1_20261107/source_audit.json)

---

## 2. observe + funnel：真实六环漏斗

语料：[corpus_real.json](file:///C:/Users/26346/Desktop/project/dafors/evo/runs/round1_20261107/corpus_real.json)（137 行，128 条带真实 ts，9 条 ts 缺失、day 记为 `unknown`）。
漏斗产物：[funnel_real.json](file:///C:/Users/26346/Desktop/project/dafors/evo/runs/round1_20261107/funnel_real.json)

### 2.1 六环数据表

| 环节 | 证据口径 | 样本（可证） | 未知 | 到下一环转化 | 流失数 | 数据状态 |
|---|---|---|---|---|---|---|
| 候选出现 | 台账行数（已进抢购流程的候选） | 137（**下界**） | - | 无数据 | - | 无独立记录 |
| 进入列表 | `listed_at_ms / listed_at` | **无数据** | - | 无数据 | - | 无数据（台账从不写） |
| 点进详情 | `result ∈ {hit,miss,no_money,over_budget}` | 137 | - | **93.43%** → 入池 | 9 | 台账 attempt |
| 入池 | `join_result=joined / pool_entered=true / 有点数或中签` | 128 可证 | **9** | **100.00%** → 中签（分母只算可证入池） | 0 | hit 反推；9 条未知单列，不并入池 |
| 中签 | `result=hit` | 128 | - | **0.00%** → 成交 | 128 | claimed，全部未校验 |
| 成交 | `verified=true` | 0 已校验 | - | - | - | 128 条 claimed 未校验 |

### 2.2 口径说明（为什么不能报单一大数）

- **候选出现 137 是下界**：台账只记已进抢购流程的机会，列表页刷出但没点进去的候选没有任何落点。真实候选数 ≥137，无法量化。
- **进入列表 / 点进详情 都是无数据**：两段没有任何证据字段，段间转化率不能给。按 `_iteration_loop.md` 口径，这两段记 `data_gaps: no_evidence`，不估算。
- **入池**：128 条 `hit` 是「中签必然入池」的直接证据；9 条 miss 无任何入池证据，按新口径进 `pool_unknown`（6.57% of 点击），**不默认算入池**。旧口径会把 137 全算入池，那是 9 条幻影入池。
- **中签 / 可证入池 = 100.00%** 只在 128 条可证样本上成立，存在选择偏差：能确认入池的样本本身就是 hit 反推来的。含未知分母时该值为 93.43%。
- **成交 0.00%** 是「已校验成交 / 中签」，不是「用户没收到货」。台账从不写 `verified`（128/128 为 None），所以只能报「0 已校验 + 128 claimed」，未校验率 100.00%。

### 2.3 分日（真实时间戳，9 条无 ts 单列）

| 日期 | 候选 | 点进 | 可证入池 | 中签 | 成交 |
|---|---|---|---|---|---|
| 2026-10-23 | 14 | 14 | 14 | 14 | 0 |
| 2026-10-24 | 12 | 12 | 12 | 12 | 0 |
| 2026-10-30 | 2 | 2 | 2 | 2 | 0 |
| 2026-10-31 | 10 | 10 | 10 | 10 | 0 |
| 2026-11-01 | 11 | 11 | 11 | 11 | 0 |
| 2026-11-02 | 47 | 47 | 47 | 47 | 0 |
| 2026-11-03 | 32 | 32 | 32 | 32 | 0 |
| unknown（无 ts） | 9 | 9 | 0 | 0 | 0 |

---

## 3. attribute：损失最大的一环

归因产物：[attribution_real.json](file:///C:/Users/26346/Desktop/project/dafors/evo/runs/round1_20261107/attribution_real.json)

| 项 | 结果 |
|---|---|
| 确认瓶颈 | **中签→成交**，lost=128，loss_rate=1.00，n=128 |
| 无证据段 | 候选出现→进入列表、进入列表→点进详情（`no_evidence`） |
| 未决（unresolved） | 入池→中签 lost=9，其中 unknown=9，整段落差落在未知区间，**不算确认流失** |
| 因子排序 | `settle_verify` 1.0（n=128） > `ui_detect` 0.0657 = `pool_evidence_gap` 0.0657 > `refresh_exhaust` 0.0 > `quota_block` 0.0 |

必须说清楚：`settle_verify=1.0` 是**证据缺失**导致的段间落差，不能读成「128 单中签全部失败」。台账里根本没有结算校验字段，所以 `hit→deal` 恒为 0。这条归因指向的是「缺埋点」，不是「结算失败」。

真正有行为解释空间的是 `pool→hit` 的 9 单（全部落在 unknown），以及「候选→列表→详情」两段完全无数据——后者才是长期最贵的盲区：如果列表环节有流失，现有台账连看都看不到。

---

## 4. hypothesize：四个可执行假设

| ID | 针对环节 | 改什么 | 预期指标 | 怎么证伪 |
|---|---|---|---|---|
| **H1** | 中签→成交 | `records` 写入时补 `verified/verified_by`（结算弹窗或余额读到才算） | `deal_rate` 从 0 变正；`unverified_hit_rate` 从 1.00 下降 | 接入后跑一轮，`unverified_hit_rate` 仍 1.00 → 埋点没生效或字段没落盘 |
| **H2** | 点击→入池（证据接入） | `observe` 透传 22 列入池字段 + 接 `evo/collect/inbox`；`funnel` 扩六段；miss 无证据记 unknown | `phantom_pool_count` 9→0；`stages_n` 5→6；`pool_unknown_count` 0→9 | 接入后 `pool` 仍为 137 或 `pool_unknown=0` → 接错了数据源 |
| **H3** | 候选→列表→详情 | 列表扫描处写 `first_seen_ms` / `listed_at_ms` / `unlock_at_ms` | `list_rate`、`detail_rate` 从 `None` 变数值，两段 data_gap 消失 | 埋点上线后 `listed` 仍 0 → 要么候选从未进列表，要么埋点未执行 |
| **H4** | 点击→入池（池满） | 把 `join_result=queue_full` 单独计数，并从入池分母剔除 | `pool` 段流失可解释为「池满」而不是「界面判定失败」 | 分桶后 `queue_full` 为 0 → 该假设不成立，9 单另有原因 |

选择落地 **H2**，理由：用户要求「不依赖游戏、本地可验证」；H1/H3 必须接游戏进程才能产出新数据，H4 需要新台账才有 queue_full 样本。H2 是框架接线修复，用已有 137 行真实语料就能闭环验证。

---

## 5. experiment：H2 落地与 A/B/AB/BA 原始数据

### 5.1 设计

- 臂：`base` = 冻结快照（五段、老口径 pool=137）；`cand` = 当前工作树（六段、证据口径）。
- 交替顺序 `[base, cand, base, cand]`，同一份冻结语料（137 行），只比同轮差值。
- 每臂 2 轮 × 每轮 20 次重复（`repeat=20`），只统计 `funnel.build` 墙钟 p95。
- `core_digest` 冻结：跑前跑后均 `3479365394c5865e`，`stable=True`。
- 台架：[tools/evo_round1_ab.py](file:///C:/Users/26346/Desktop/project/dafors/tools/evo_round1_ab.py)；原始数据：[ab_runs.json](file:///C:/Users/26346/Desktop/project/dafors/evo/runs/round1_20261107/ab_runs.json)

### 5.2 四轮原始值

| 轮 | 臂 | discover | listed | click | pool | hit | deal | pool_rate | funnel_build p95 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | base | 137 | - | 137 | 137 | 128 | 0 | 1.000000 | 0.475600 ms |
| 2 | cand | 137 | 0 | 137 | 128 | 128 | 0 | 0.934307 | 0.893200 ms |
| 3 | base | 137 | - | 137 | 137 | 128 | 0 | 1.000000 | 0.411400 ms |
| 4 | cand | 137 | 0 | 137 | 128 | 128 | 0 | 0.934307 | 0.828500 ms |

### 5.3 同轮差值

| 指标 | 差值 |
|---|---|
| `pool_count` | **−9** |
| `pool_rate` | **−6.5693 pp**（口径纠正，非退化） |
| `pool_unknown_count` | **+9** |
| `phantom_pool_removed` | **9** |
| `stages_n` | **+1**（5→6） |
| `hit_count` / `deal_count` | 0 / 0（本层不改判，符合设计） |
| `funnel_build_p95` | **+0.4173 ms**（相对 +94.1%） |
| `core_digest` | 前后一致，`stable=True` |

### 5.4 停止条件核对

手册阈值是**端到端行为指标**（1% 增益 / 点击 5% / 识别 2% / 流程 1 步），本轮改动落在离线分析层，不改变抢购行为，因此：

- 不能按「pool_rate 下降 6.57pp」判失败——那是把 9 条幻影入池纠正成未知，方向正确。
- 也不能声称行为增益：`hit`、`deal` 差值为 0，真实入池率是否变化需要新台账才能观测。
- 唯一可量化的代价是分析层 p95 `+0.417 ms/轮`，绝对值小于 1 ms，且不在热路径；已写入 registry `side_effects`。
- 「连续 3 轮低于阈值就停」的规则本轮不适用：这是单次口径/接线修复，不是参数寻优；R2 起才进入行为参数的多轮比较。

---

## 6. decide + registry：保留与回滚

判定：**accepted（level=offline）**。依据是证据正确性（9 条幻影入池清零、六段完整、无证据段被显式标注），不是速度或中签率。

登记条目：[registry.jsonl](file:///C:/Users/26346/Desktop/project/dafors/evo/registry.jsonl)（第 2 条，`schema=evo-registry/2`）

| 字段 | 值 |
|---|---|
| `experiment_id` | `EXP-b41c9e07d2a53f18-7T195502` |
| `signature` | `b41c9e07d2a53f18` |
| `hypothesis.factor` | `pool_evidence_gap` |
| `metric_before` | `phantom_pool_count=9, pool_count=137, pool_rate=1.0, stages_n=5` |
| `metric_after` | `phantom_pool_count=0, pool_count=128, pool_rate=0.9343, stages_n=6, pool_unknown_count=9` |
| `sample_size` | planned 137 / observed n=137（可证入池 128，未知 9） |
| `changes` | 7 个文件（见下） |
| `side_effects` | pool_rate −6.57pp（口径纠正）；funnel_build_p95 +0.417ms（离线层）；stages_n +1 |
| `rollback` | `patch-reverse`，ref=`evo/runs/round1_20261107/impl_round1.patch` |

回滚步骤（已验证可反向应用，`git apply --check -R` rc=0）：

```powershell
cd C:\Users\26346\Desktop\project\dafors
git apply -R evo\runs\round1_20261107\impl_round1.patch
# 复跑台架应回到 pool=137 / stages_n=5 / pool_unknown_count=None
.\.venv\Scripts\python.exe tools\evo_round1_ab.py --base-snap "$env:TEMP\dafors_rounds\r1\base_snap" --corpus evo\runs\round1_20261107\corpus_real.json --repeat 20
```

改动清单（全部在 `evo/`、`tools/`、`tests/`，未触碰并行分支在改的 `core/*`）：

| 文件 | 范围 | 内容 |
|---|---|---|
| `evo/pool_contract.py` | 新增 1-111 | 证据分级：`join_result` → `pool_entered` → `roll/won` → `hit` 回退；缺失即 unknown |
| `evo/pool_inbox.py` | 新增 1-111 | inbox toast 挂接：先 `attempt_id` 精确，再同名 0~180s 邻接 |
| `evo/funnel.py` | 重写 1-284 | 五段扩六段；每段输出 count/unknown/effective/measured/claimed；`pool_rate` 分母只算可证 |
| `evo/attribute.py` | 重写 1-168 | 跳过无证据段；未知区间记 `unresolved`；新增 `pool_evidence_gap` |
| `evo/observe.py` | 18, 59, 223, 232-301, 308-337, 366-391 | 透传 22 列入池字段；接 inbox；rollup 按 `classify` 计数 |
| `evo/daemon.py` | 382-420 | 日报改六段，加「未知/数据」列与可证入池率 |
| `evo/selftest.py` | 26-75, 152-170 | 夹具补入池/抽签字段，断言六段与有证据段单调 |
| `tools/evo_round1_probe.py` / `evo_round1_funnel.py` / `evo_round1_ab.py` / `evo_round1_register.py` | 新增 | 探针 / 真实漏斗 / 交替台架 / 登记 |
| `tests/test_evo_funnel_round1.py` | 新增 | 5 条回归：六段顺序、legacy miss→unknown、证据判定、inbox 挂接、归因 |

---

## 7. 测试结果

```
python -m pytest tests/test_evo_funnel_round1.py -q
5 passed in 20.33s

python -m evo.cli selftest
自检结束：24/24 通过
（含「六段漏斗存在」「有证据段单调不增 [2700,0,1800,1692,281,281]」「瓶颈已定位 pool」）

python -m evo.collect.selftest
自检 通过（幂等 4638ac04b8cc，增量无新增 1730→1730，窗口重跑 1 条全在窗口内）
```

`git apply --check -R impl_round1.patch` → `rc=0`（回滚路径可用）。

---

## 8. 下一轮建议（R2）

1. **先补数据缺口，再谈优化**：H3（`first_seen_ms/listed_at_ms` 埋点）能让「候选→列表→详情」两段从「无数据」变成可测；当前 137 行里有 2 段完全盲区，是最贵的未知。
2. **H1 需要真机一轮**：`verified` 写入必须靠结算弹窗/余额读取，本地无法验证；建议 R2 与 H3 一起上，跑一次真实抢购（哪怕 0 单成交也要有 `verified=false` 的显式落盘），否则 `hit→deal=100%` 的落差永远无法解释。
3. **H4 等样本**：`queue_full` 需要新台账才出现；一旦出现，应把「池满」与「界面判定失败」分开计，否则 9 条 unknown 永远只能标 unresolved。
4. **语料卫生**：`data/records.jsonl` 目前是 1 行 1MB 的写盘残留，与本轮快照不一致；建议在干净分支上清理并重放（本轮按约束未动）。
5. **不要在离线层追速度**：`+0.417ms/轮` 已记录为副作用；若 R2 继续扩字段导致 p95 明显上涨，应改用增量 rollup 而不是全量重算。

---

## 9. 本轮未证与边界

- 没有验证任何「入池率提升」，只验证了「入池证据被正确记账」。抽签制下真正决定成败的是入池率，而这个指标目前仍只能以 128 条 hit 反推（93.43% 可证/137 发起），不是全量真值。
- 9 条 miss 的 ts 缺失（只有小时桶），所以分日表里单列 `unknown`，未把它们塞进任一天。
- `pool_rate=100%`（R0 口径）在 128 条 hit 的语料里是循环论证；本轮已改为 93.43%（可证）并显式标注未知。
- 无 commit、无 push；`core/` 下 49 个并行分支脏文件未被本轮读取修改或还原。
