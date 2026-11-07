# 结算观测补齐：三态落盘 · 两个时间戳 · 六段漏斗

日期：2026-11-07　范围：`core/settle_verify.py`（新增）、`core/records.py`、`evo/`、`tests/test_settle_observability.py`（新增）、`evo/replay_settle.py`（新增）
约束遵守：`core/pooltrace.py` **一字未改**；`tests/conftest.py` 未改；未 commit、未 push。

---

## 1. 三态判定规则（`core/settle_verify.py`）

`verified` 只允许三个值：`won` / `lost` / `unknown`。

| 判定 | 证据（任一满足） | 说明 |
|---|---|---|
| `won` | `won_screen=True` 看到中签界面<br>`item_received=True` 背包出现物品<br>`reward_claimable=True` 奖励可领取<br>`toast_won=True` 且来源 `text` | 中签侧任一直接证据成立 |
| `lost` | `lost_text=True` 明确「未中签/未抢购成功」文案<br>`toast_won=False` 且来源 `text`<br>`publicity_over=True` **且** `row_gone_from_list=True` **且**（`reward_absent=True` 或 `reward_claimable=False`） | 文案证据，或公示结束+列表消失+**无奖励**三条齐全 |
| `unknown` | 以上都没有；或证据自相矛盾；或只有点数判负 | **缺证据的默认值，且是唯一的默认值** |

硬约束（写进模块 docstring + 测试）：

1. 没有任何证据 → `unknown`，**绝不**落 `lost`，也绝不落 `won`。
2. 公示结束 + 列表消失，但「有没有可领奖励」不可证 → 仍是 `unknown`（不拿“没看见奖励”当“没有奖励”）。
3. `toast_won_source=points`（点数判负）不算结算落败证据，只标 `unknown` + `reason=points_only`。
4. 中签证据与落败证据同时出现 → `unknown` + `reason=conflicting_evidence`，不擅自裁断。

旧台账兼容（读侧 `normalize()`）：

| 旧值 | 折算 | 原因 |
|---|---|---|
| `True` | `won` | 旧代码含义「校验过结算弹窗」 |
| `False` | `unknown` | 旧代码含义「只点了确认」，不是“没中” |
| `None` / 脏值 | `unknown` | 从没写过结算证据 |

写侧 `records.add()`：字符串三态原样保留；历史 bool 保留 bool（引擎仍在用 True/False 表示校验开关，不强行改写语义）；`None` 一律落成 `"unknown"`。合并阶段只需把 `settle_verify.decide(...)` 的返回值传给 `records.add(verified=...)` 即可。

**与 pooltrace 的接线契约（本分支不调用、不修改 pooltrace）**：由合并阶段把已解析结果塞进下列字段，`settle_verify` 直接消费：

```python
{"toast_won": bool|None,          # parse_toast 的 won
 "toast_won_source": "text"|"points"|"",
 "won_screen": bool|None, "item_received": bool|None,
 "reward_claimable": bool|None, "lost_text": bool|None,
 "publicity_over": bool|None, "row_gone_from_list": bool|None,
 "reward_absent": bool|None}
```

---

## 2. 两个时间戳

| 字段 | 含义 | 时钟 | 写入方 |
|---|---|---|---|
| `first_seen_ms` | 该候选**首次**出现在列表扫描结果里的时刻 | 单调时钟毫秒（`time.monotonic()`） | 列表扫描首次命中时 `settle_verify.mark_first_seen(row)` |
| `listed_at_ms` | 该单挂单完成、**进入公示**的时刻 | 单调时钟毫秒（同一时钟） | 挂单成功时 `settle_verify.mark_listed(row)` |

规则：

- 两个字段只在**同一进程、同一单调时钟**内相减有效；跨进程重启不做时长推断。
- 无法观测 → 字段写 `None`，同时行内 `no_evidence` 列出缺失字段名（如 `["first_seen_ms","listed_at_ms"]`）。
- `norm_ms()` 拒收 `0`、`-1`、`True`、字符串 —— 不用哨兵值冒充观测。
- 墙钟副本 `listed_at`（epoch 秒，toast 推出）继续保留，只用于跨会话排序，**不参与**段间时长。
- 两段时长：`discover→listed = listed_at_ms - first_seen_ms`；`listed→click = click_at_ms - listed_at_ms`（`click_at_ms` 为既有字段，行内没有就是 `no_evidence`，不猜）。

### 时钟基准守卫

`core/pooltrace.py`（另一条线在改，本分支未触碰）与 `evo/collect/schema.py` 的历史实现把时间字段当 epoch ms 用。为兼容两种来源，`gap_ms()` 加了一道基准守卫：

- 写入方（`mark_first_seen` / `mark_listed`）用单调时钟，保证同进程内差值单调。
- 读取方允许两端都是 epoch ms —— 只要同基准，差值同样可信。
- 差值 `> MAX_GAP_MS`（12h）视为跨基准（单调 vs epoch 会差几十亿毫秒），直接返回 `None`，该段标 `no_evidence`。例行公示窗只有分钟级，12h 不会误伤真实样本。
- 反向/倒序差值同样返回 `None`：时钟回拨或写序错乱时不产出负时长。

---

## 3. 六段漏斗

`discover → listed → click → pool → hit → deal`（候选出现 → 进入列表 → 点进详情 → 入池 → 中签 → 成交）。

每段 stage row 都带：

| 字段 | 含义 |
|---|---|
| `count` | 有正向证据的样本数 |
| `unknown` | 该段未知样本数（如 pool 的 9 条无入池证据） |
| `effective` | `count + unknown` |
| `rate_of_discover` | 本段有效样本 / discover |
| `step_rate` / `loss_count` / `loss_rate` | 到下一段转化 / 流失；**证据不足时全部为 `null`** |
| `status` | `measured` / `partial` / `no_evidence` |
| `no_evidence` | 布尔，`status == "no_evidence"` |

结算段专用：`deal.status` 在没有任何一单拿到 `won/lost` 证据时为 `no_evidence`；`metrics.settle_rate`、`end_to_end_rate` 相应为 `None`，**不再输出 0.00%**。`settle_breakdown = {won, lost, unknown}` 单独列出三态，unknown 不并进 lost。

时间维度另有 `metrics.discover_to_listed` / `listed_to_click`：`{p50_s, p95_s, samples, status}`；没有样本时 `status=no_evidence`、数值为 `None`。

归因层：某对相邻段的 `status` 为 `no_evidence` 时，缺口标 `reason=settle_no_evidence` 并在 `bottleneck.no_evidence` 上打标；报告文案写「证据缺失，不计为已确认流失」。

---

## 4. 回放结果（137 行冻结语料）

命令：`python -m evo.replay_settle`
产物：`evo/runs/round1_20261107/settle_replay/ledger.jsonl`、`report.json`

```
语料 137 行（hit 128 / miss 9）
写盘 137 行，三态分布 {'won': 0, 'lost': 0, 'unknown': 137}
第一条 unknown：legacy-hit-0001 day=2026-10-23
  verified='unknown' no_evidence=['first_seen_ms','listed_at_ms']
```

真实语料六段：

| 段 | count | unknown | 段间转化 | status |
|---|---|---|---|---|
| 候选出现 | 137 | 0 | - | `no_evidence`（语料未独立记录候选数，仅下界） |
| 进入列表 | 0 | 0 | - | `no_evidence`（无 `listed_at_ms`/`listed_at` 证据） |
| 点进详情 | 137 | 0 | 93.43% | `measured` |
| 入池 | 128 | 9 | 93.43% | `partial` |
| 中签 | 128 | 0 | -（成交段无证据） | `measured` |
| 成交 | 0 | 128 | - | `no_evidence` |

- `settle_rate = null`，`settle_state = no_evidence`，`settle = {evidence_n: 0, unknown_n: 128, lost_n: 0}`
- `no_evidence` 段：`["discover","listed","deal"]`
- 候选→挂单 / 挂单→点进：`samples=0, status=no_evidence`（语料确实没有这两个时刻，不补假数）

构造样本（证明“有证据时六段都能出数”）：

| 段 | count | unknown | 段间转化 | status |
|---|---|---|---|---|
| 候选出现 | 6 | 0 | 100.00% | `measured` |
| 进入列表 | 6 | 0 | 100.00% | `measured` |
| 点进详情 | 6 | 0 | 66.67% | `measured` |
| 入池 | 4 | 1 | 60.00% | `partial` |
| 中签 | 3 | 0 | 33.33% | `measured` |
| 成交 | 1 | 1 | - | `partial` |

- `settle_rate = 33.33%`，`settle_breakdown = {won:1, lost:1, unknown:1}`
- 候选→挂单 P50 = 2.0s（6 样本）、挂单→点进 P50 = 0.5s（6 样本）

旧行照读：137 行原始语料（`verified=None`、无新字段）先直接进 `funnel.build()`，无异常；再由 `records.add` 写盘、`records.load()` 读回，字段与三态均正常（`report.json` 的 `legacy_stages` / `written_rows`）。

---

## 5. 无证据行为的证明

新增测试 `tests/test_settle_observability.py`（12 条，全通过）：

| 测试 | 断言 |
|---|---|
| `test_no_evidence_is_unknown_never_lost` | 完全无证据的 hit 行 → `unknown`，且 `!= lost` / `!= won` |
| `test_empty_and_false_like_inputs_are_unknown` | `{}` / `None` / `False` / `"false"` / `0` / 垃圾值 → 全 `unknown` |
| `test_lost_requires_explicit_evidence` | 公示结束+列表消失但奖励未知 → `unknown`；三条齐全才 `lost` |
| `test_points_only_and_conflict_are_unknown` | 点数判负 → `unknown`；won+lost 冲突 → `unknown` |
| `test_time_marks_reject_zero_and_negative` | `0/-1/True/"1000"` 全拒收，`no_evidence` 正确列出 |
| `test_records_write_tristate_and_marks` | 写盘 `verified` 为三态字符串；缺时间戳 → `None + no_evidence` |
| `test_funnel_six_stage_no_evidence_statuses` | 六段顺序正确；discover/listed/deal 为 `no_evidence`，`settle_rate=None` |
| `test_funnel_six_stage_with_full_evidence` | 六段全部有数、`settle_rate=1/3`、两段 P50 = 2.0s / 0.5s |
| `test_records_legacy_row_reads_without_error` | 旧行（`verified=None`、无新键）照读并进漏斗不报错 |
| `test_gap_rejects_mixed_clock_bases` | 单调+epoch 混用 → `gap_ms` 为 `None`；同基准仍可算；>12h 判无证据 |

真实回放侧证据：`report.json` 中 `verdicts.unknown = 137`、`first_unknown.verified = "unknown"`、`funnel_batch_settle_rate = null`、`funnel_batch_no_evidence` 含 `deal`。

---

## 6. 数据清理

| 项 | 结果 |
|---|---|
| 原路径 | `data/records.jsonl`（1 行，1MB 测试残留） |
| 留档 | `C:\Users\26346\AppData\Local\Temp\dafors-records-residue-20261107.jsonl` |
| 大小 / SHA256 | 1,048,742 bytes / `6764C3DF68C225401A9923034F96D69FA9822140AA8CAB7765B02556001DFB48` |
| 工作树 | 已删除（`Test-Path data\records.jsonl` → False）；未触碰其他 `data/` 文件 |

---

## 7. 验证汇总

- `python -m pytest tests/test_settle_observability.py -q` → 13 passed
- `python -m pytest tests/test_records.py tests/test_pool_fields.py tests/test_evo_funnel_round1.py tests/test_console_api.py tests/test_fix_a.py tests/test_switches.py tests/test_retention.py tests/test_persist_hardening.py tests/test_selfcheck.py` → 仅 6 条环境失败（本机缺 RapidOCR/标定模型）与 1 条并发下的性能抖动；性能用例单独重跑通过
- `python -c "from evo.selftest import run_selftest; run_selftest()"` → 24/24 通过（含六段漏斗、瓶颈、回滚）
- `python -m evo.replay_settle` → 137 行三态落盘、六段状态、无证据证明全部产出
