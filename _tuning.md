# 调优层：分析 → 建议 → 受控自适应

目标：用抢购台账 + 后台看板 + 真机报告，让参数依据数据收敛，而不是靠人手工试。
**不 commit、不 push；不动 `core/engine.py` / `core/config.py` / `core/switches.py` / `web/index.html`。**

```
抢购数据 ──> core/autotune.py:summarize() ──> 指标快照
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
tools/tune_from_data.py     AutoTuner.step()
（只读，出报告与建议）       （6 条护栏 + shadow + 审计/回滚）
        │                        │
tuning_recommendation.json   data/autotune/state.json
_tuning_report.md            data/autotune/audit.jsonl
```

## 1. 分析层：`tools/tune_from_data.py`

```powershell
.\.venv\Scripts\python.exe tools\tune_from_data.py
.\.venv\Scripts\python.exe tools\tune_from_data.py --backend exports\board.csv --realmachine _realmachine_result.json
.\.venv\Scripts\python.exe tools\tune_from_data.py --backtest
```

- 数据源：本地台账 `data/records.jsonl`（容错坏行）、后台 CSV/JSON（`list` 或
  `rows/data/records/items/list` 包装）、真机 `_realmachine_result.json`
  （`runs/cases/results/records/rows` 或单对象）。缺哪个跳过并在报告第 0 节写明。
- 字段别名归一化在 `core/autotune.py:ALIASES`：时间、卡密哈希、商品名、品阶、
  成交价、结果、卡点误差（毫秒/秒自动换算）、刷新次数、连点配置、再进池延迟、
  客户端版本、跳过证据。缺字段留 `None`，不猜。
- 合并顺序：后台 + 真机 + 台账，按 `ts` 排序；乱序检查**逐源**做，避免跨源交错误判。
  重复事件按 `(ts, 卡密哈希, 商品名, 品阶, 结果, 误差)` 去重。

### 指标定义与算法

1. **各品阶成交率**
   `attempts` = 结果属于 `hit/miss/skip/no_money/over_budget/aborted` 的行；
   `hit_rate = hits / attempts`，95% 区间用 Wilson 分数（小样本不塌到 0/1）。
   品阶映射 传说/史诗/稀有/普通，未知归 `unknown`。
2. **卡点误差分布**
   误差 = 实际开点 − `挂单时刻 + 公示时长` 反推的解锁时刻（负=点早、正=点晚）。
   只统计真的点了的 `hit/miss`（跳过没有点击时序）；绝对值 >60s 视为脏数据丢弃。
   p50/p90/p95 用线性插值；超窗 = `|误差| > 33ms`（一帧 33.3ms，
   与 `_lottery_fsm.CONFIRM_WINDOW_MS` 同源）。另给按小时（本机时区）分段。
3. **跳过命中率**
   分母只含**有事后证据**的跳过；`unknown` 单独计数，**绝不算成「跳对了」**。
   确认来不及 / 确认本来来得及 从 `skip_evidence` 文本或布尔值解析。
4. **刷新收益**
   按 `refresh_count` 分桶，桶内成交率；桶样本最小 <30 或只有一个取值 → 不下结论。
   合格时用加权最小二乘斜率 + 95%CI（权重=桶样本量）。这是**观测相关性**：
   热门皮肤本身难中且常被刷更多，因果解释需要分品阶/分商品控制，报告已注明。
5. **连点收益**
   按 `(连点模式, 间隔, 持续时长)` 分组，给再进池延迟中位数与 p90；n<30 标 `insufficient`。
   只描述「再进池更快」，不声称提高单次中签率（抽签模型）。
6. **分时段 / 分客户端**：同一 `_group_stats` 口径，样本 <30 的组只列数字、不下结论。
7. **不能下的结论**：写进第 7 节，并同时进 `conclusions_withheld` 字段。

真实台账当前结果（2026-11-07）：137 行 / 137 次尝试，成交 128、未中 9，
成交率 **93.4%**（Wilson 95%CI 88.0%~96.5%）；台账没有品阶、卡点误差、刷新、
连点字段，三条建议均为「不改」。

## 2. 自适应层：`core/autotune.py:AutoTuner`

只写**已有**开关，全部走 `switches.set()`（单值边界 + min>max 组合校验仍然生效）：

| 开关 | 软边界 | 单步 | 规则 |
|---|---|---|---|
| `fire.delay` | 0~1200ms | ≥10ms 或 20% | R1 开点对齐 |
| `buy.skip_late.threshold_s` | 0~10s | ≥1s 或 20% | R2 跳过阈值 |
| `scan.interval` | 0.02~0.5s | 0.005s 或 20% | R3 刷新收益 |
| `scan.repeat.max` | 0~10 次 | 1 或 20% | R4 刷新收益撞界后接手 |
| `scan.repeat.min` | 0~10 次 | 1 或 20% | R4 同上 |

> `FIRE_LEAD_S` 是常量、不在开关表里，所以「提前量」只能通过 `fire.delay` 往后推：
> 误差为负（点早）可直接修正；误差为正（点晚）只能减到 0，再往前需要改常量，
> 本层只出建议并标 `blocked="constant"`。

**规则产出一定是人话**，例如：

> 因为 p50 卡点误差 -40.0ms（点早），p95 从 41.2ms 到 43.8ms（超窗 38%），
> 所以把 fire.delay 从 100 调到 120（R1-lead-early，样本 n=30）

每条建议含 `knob / old / new / rule / sample / bounds / metric / text / blocked`，
`blocked` 非空即「只建议不改值」。

### 六条护栏的实现

| 护栏 | 实现 | 阈值理由 |
|---|---|---|
| G1 最小样本 | `sample < min_sample(30)` → `blocked="sample"`；R1 取「最大品阶尝试数」与误差条数的较小值 | 30 次时比例 95%CI 半宽约 ±0.18；再少分位数和成交率都不可信 |
| G2 单次幅度 ≤20% | `_step_size = old*0.2`（整数 round，至少 1）；`old=0` 用开关最小步长（整数最小可表示单位 1，属必要例外） | 一次 20% 是「可回滚的实验步」，避免一把调飞 |
| G3 硬上下界 | `BOUNDS ∩ KNOBS.soft`；`_clamp` 后撞界即停，状态记 `stop_side/stop_reason`，同方向后续直接 `blocked="stopped"` | 开关表是全局契约，本层只收窄不放宽；撞界后不再反复试探 |
| G4 调整间隔 | 距上次变更的成交数（`hit_ts` 中 `ts > changed_ts` 的条数）`< 10` → 拦；首次调整没有「上一次」，直接放行 | 10 次成交保证新参数已产生可观测样本，也避免同批多变量叠加 |
| G5 审计 | 每次 step 追加 `data/autotune/audit.jsonl`：时间、mode、knob、旧值、新值、规则、指标、样本量、边界、版本、解释句 | 追加写 + 版本号，可逐条复盘；shadow 也照记 |
| G6 kill switch | 最近 30 次有胜负尝试全失败/全成功；升/降序混杂 ≥10% 且 ≥3 对；单一商品占比 ≥80% 且尝试 ≥30；时间戳落在 2001 前或未来 1 天外的比例 ≥20%；无任何尝试 → 冻结 | 这些形态更像数据管线坏了（重复灌库、时钟错、只刷一件），继续调会把噪声学成参数 |

冻结后 `step()` 不再产生建议，只写 `freeze` 审计；人工复核后可
`AutoTuner.unfreeze("原因")` 解除，历史原因写入 `freeze_history`。

### shadow / 审计 / 回滚

- `AutoTuner(cfg, shadow=True)` 是默认：算指标、出建议、写审计，**不调用
  `switches.set()`**，运行参数逐字节不变（测试 `test_shadow_changes_nothing_but_audits`
  用深比较断言）。
- 真要落地必须显式 `shadow=False`（注意：分析工具本身永远是只读，不提供 apply 开关）。

```python
from core.autotune import AutoTuner, AutotuneStore
tuner = AutoTuner({}, shadow=False)          # 显式打开
out = tuner.step(summary)                    # 一进一出，最多 1 个变更
tuner.rollback(steps=3)                      # 回到第一次调整前的值
```

- 每次变更前把全量开关快照写进 `state.history`（`version/values/at`），
  `rollback(steps=N)` 恢复 `history[-N]` 并截断历史与版本号；shadow 下只写
  `rollback_shadow` 审计，不动值。
- 回滚条目也进 `audit.jsonl`，含 `from_version/to_version/values/errors`。
- 一次 step 最多落地 1 个变更（`MAX_CHANGES_PER_STEP=1`），其余标 `deferred`，
  避免多变量同时动导致归因不清。

## 3. 验证结果

### 3.1 合成数据收敛（真值 bias=-30ms → 目标 `fire.delay=30`）

20 批 × 30 条，noise σ=38ms，抽签中签率 0.8，seed=20261107：

```
delay: 10 10 12 14 14 14 14 17 17 20 20 20 20 20 20 20 20 20 20 20
最终误差 10ms（死区内即停，ALIGN_BAND_MS=16）
```

受 G2（20% 步长）与 G4（10 次成交间隔）限制，收敛不是一步到位，而是分批逼近；
停在 ±16ms 死区是刻意设计，避免为 10ms 级别噪声来回抖。

### 3.2 参数漂移跟踪（真值从 -30 挪到 -60，目标变 60）

```
delay: 20 24 24 29 29 35 35 35 42 42 42 50
滞后 9 批进入 |误差| ≤20ms，末批误差 10ms
```

滞后来自同一条 G4：每次变更后要再攒 10 次成交才允许下一步，属于预期代价。

### 3.3 极端输入

| 用例 | 行数 | 冻结 | 变更 | 说明 |
|---|---|---|---|---|
| 只有 3 条 | 3 | 否 | 0 | 全部建议被 G1 拦 |
| 全失败 | 40 | 是 | 0 | 最近 30 次成交 0 |
| 全成功 | 40 | 是 | 0 | 最近 30 次全成交 |
| 时间倒序 | 40 | 是 | 0 | 倒序本身判 `desc`、不因顺序冻结；该组被冻结是同批结果全为成交 |
| 重复事件 | 12→6 | 否 | 0 | 去重 6 条，样本不足不调 |
| 字段缺失 | 4 | 是 | 0 | 无可识别尝试 |
| 超大数值 | 40 | 是 | 0 | 误差 >60s、价格 >1e9 被丢弃；全成交触发冻结 |

所有分支的值都落在 `BOUNDS ∩ soft` 内，没有异常抛出。
乱序冻结另由 `test_kill_switch_time_disorder`（打乱顺序、升降混杂）覆盖。

### 3.4 护栏 / 回滚 / shadow 测试

`tests/test_autotune.py`（22 + 5 契约项）与 `tests/test_tune_from_data.py`（11 项），
共 **37 passed**：

- 20 次样本 → 不动值；30 次 → 100→120（严格 20%）
- 第二次调整前只有 8 次成交 → 被 G4 拦，值保持 120
- 1190ms 上调到 1200 后撞界，再同方向 → `stop_side="hi"` 且不改
- 三次变更 `[100,120,144,173]`，`rollback(steps=3)` 回到 100、version 回 0
- shadow 三次 step 后 `cfg` 深比较完全一致，审计全是 `mode=shadow`
- 跳过阈值为 0 时，`old=0` 走最小步长 1，不会出现「0 的 20% = 0」死锁
- 返回形状契约：`summarize` 在空/样本不足/冻结/正常四种分支键集合完全一致；
  `_dedupe` 恒定二元组；三个 reader 恒定三元组。

## 4. 现在还不能下的结论

1. **开点对齐（R1）没有真机数据**：台账无 `lead_err_ms`，`_realmachine_result.json`
   也不存在。现在的 p50/p95 只在合成回测里成立，不能代表线上时序。
2. **品阶维度为空**：台账 137 行全部 `grade=unknown`；后台看板未接入。分品阶成交率、
   跳过阈值都还不能按品阶分别定。
3. **跳过命中率是 unknown**：没有任何事后证据，不能声称「跳得对」或「跳得狠」。
4. **刷新收益不可估**：台账 `refresh_count` 只有一个取值；而且即使有数据，
   它也是相关性，需分品阶/分商品控制混杂后才谈因果。
5. **连点收益不可估**：没有连点配置与再进池延迟字段，只能说「缺数据」。
6. **客户端版本不可比**：全部 `unknown`，无法判断版本间时序差异。
7. **成交率 93.4% 不能当基线**：台账缺品阶且样本集中在少数时段（多数小时 <30 次），
   不代表整体中签率；价格同皮肤同价，也不构成竞争维度。
8. **后台字段未定稿**：CSV 读写已按通用字段做过兼容，但字段最终定名后需要重跑一次，
   确认别名表覆盖（缺列会体现在 `missing_fields` 与报告第 0 节）。

## 5. 回归状态

- `pytest tests/test_autotune.py tests/test_tune_from_data.py -q` → **37 passed**
  （自适应层 27：原护栏用例 + 返回形状契约；分析层 10：IO 容错、报告、回测）。
- 全量 `pytest tests -q --continue-on-collection-errors` →
  **1965 passed / 2 failed / 17 skipped / 2 xfailed**，耗时 499s。
  2 个失败都是 `tests/test_fix_b.py` 读取真机截图 `_shots/drv_onsale.png`
  缺失（`cv2.imread` 返回 None），与调优层无关。
- 形状契约：`summarize()` 在空样本 / 样本不足 / 冻结 / 正常四种分支键集合完全一致；
  `_dedupe()` 恒定 `(list, int)` 二元组；三个 reader 恒定 `(rows|None, int, str)`
  三元组。下游不做 `try/except` 兜底，结构违约会直接测试失败。
- shadow 复验：三次 step 后配置深比较完全一致，真实运行参数零改动，审计全为
  `mode=shadow`。
