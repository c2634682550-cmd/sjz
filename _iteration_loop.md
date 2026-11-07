# 迭代执行手册（每轮固定动作）

> 适用范围：`dafors` 主树的多轮持续优化。第 0 轮只固化基线，不改产品代码。
> 硬约束：不 commit / 不 push；不写仓库 `data/`；测量只在快照副本与系统临时目录；
> 不开可见窗口；不动真实鼠标；每轮开始前先冻结快照。

---

## 0. 一轮的定义与不变量

一轮 = 一个假设 → 一次最小代码改动 → 同台架复测 → 登记或回滚。

- **快照先行**：本轮所有测量、改动、回归都发生在冻结副本里，主树只做只读取数。
- **指纹**：每轮记录 `git_head` / `dirty 文件数` / `core_digest` / `bench_digest` /
  `config_digest` / `seed`。`core_digest` 前后不一致 = 本轮作废。
  快照里不含 `.git`，`git_head` 与 dirty 数从源树在冻结前另记一行。
- **对照**：只比同一台架、同一快照基准、同一载重下的**相对增益**；不跨机比绝对值。

冻结快照（PowerShell，仓库外）：

```powershell
$r="r07"; $snap="$env:TEMP\dafors_rounds\$r\snap"
robocopy . $snap /E /XD .git .venv build dist data __pycache__ .pytest_cache /XF *.pyc | Out-Null
.venv\Scripts\python.exe perf\bench\run.py --snapshot $snap --groups ocr,click,flow `
  --out "$env:TEMP\dafors_rounds\$r\bench" --tag $r
```

---

## 1. 每轮六个固定动作

### A. 取数选瓶颈

```powershell
.venv\Scripts\python.exe tools\loop_metrics.py --json `
  --engine "<snap>\core\engine.py" > "$env:TEMP\dafors_rounds\r07\shape.json"
.venv\Scripts\python.exe perf\bench\run.py --snapshot <snap> --groups ocr,click,flow `
  --out <本轮目录>\bench --tag r07
```

按顺序读，命中即停（一次只打一个瓶颈）：

| 观察 | 归因 | 首要候选 |
|---|---|---|
| `flow.item_total_ms.p95` 高，且 `detail_ocr_ms` 占比最大 | 详情识别 | 详情帧 ROI / 跳帧 / 解析路径 |
| `enter_click_ms` / `confirm_click_ms` 高，或 `click.period_ms` 漂移 | 输入节奏 | 按住与间隔、连点帧数 |
| `ocr.chain_price_ms.p50` 高，`tier1_ms` 未变 | 价格降级链 | 一级命中率、ROI、缓存 |
| `scan_list_ms` 稳态高或 `list_cold_scan_ms` 高 | 列表扫描 | 帧缓存、跳行、预筛 |
| `fixed_wait_ms / item_total_ms > 25%` | 固定等待 | 自适应等待、事件驱动 |
| 自愈仿真 `sim_manual_continue` 占比升高 | 自愈规则 | 场景补规则 / 动作编排 |
| `loop_metrics` 步骤数或分支数上升 | 流程形状 | 合并阶段、删分支 |

### B. 立假设（先写后做）

先在台账草稿里写死：`round`、`hypothesis`（可证伪的一句话）、`metric`、
`expected_direction`、`expected_gain`、`min_sample`、`changes` 计划（文件+行）、
`rollback` 计划。假设没写清不许开工。

### C. 三路并行（文件边界互不重叠）

| 路 | 职责 | 可写 | 产物 |
|---|---|---|---|
| 实现路 | 最小改动 + 生成补丁 | 本轮声明的代码文件 | `impl/rNN.patch`、`impl/notes.md` |
| 对抗复核路 | 只读挑错：反例、红线、失败模式 | 仅 `red/` | `red/review.md`（必须含“不通过理由”） |
| 影响面回归路 | 只读跑测试与相邻台架 | 仅 `reg/` | `reg/pytest.xml`、`reg/bench_*.json` |

**边界规则（硬）**：

1. 每路开工前写 `writes.txt`（绝对路径清单）；三份清单两两交集必须为空。
2. 同一文件只能由一路写；需要同时改同一文件 → 合并成一路，或排到下一轮。
3. 只读文件可共享；但只读方不许 `touch`、不许改 mtime。
4. 合并只认 `impl/rNN.patch`；`red/`、`reg/` 的改动一律不进补丁。
5. 禁写：`data/**`、`config.json`、`apps.json`、`cu.json`、`evo/state/**`（登记除外）。
6. 冲突检测：开工前记 `sha256` 清单，收工再记一次；非本路 `writes.txt` 内文件哈希变化 =
   越界，本路作废。

### D. 复核判过才合并

合并前四条全过，缺一退回：

1. `criteria.json` 五条红线全过（价格全等率 1.0、成色命中 ≥0.86、卡点 P95 不退化、
   误买防护、输入身份不降级）。
2. 主指标增益 ≥ 本节阈值（见 §2）。
3. 副作用：其余纳入指标退化 ≤3%，且无新增 >3% 项。
4. 回归全绿（失败数不高于基线，跳过原因不变）。

复核签字写在 `red/review.md` 末尾：`PASS` / `REJECT + 理由 + 反例`。没有签字不合并。

### E. 同台架重测

与 A 完全相同的命令、种子、快照基准。开跑前后各记一次 `cpu_load`：

- 前后差 >10 个百分点，或任一时刻 >50% → 本轮数字作废，等空载重跑。
- 同轮内至少 2 次独立跑，主指标 p50 差异 ≤2% 才认；否则加跑到 3 次取中位。

**负载漂移的根治办法：基线与候选交替跑（A/B/AB/BA）。**

```powershell
foreach ($s in @("base","cand","base","cand")) {
  .venv\Scripts\python.exe perf\bench\run.py --snapshot "$env:TEMP\dafors_rounds\r07\$s" `
    --groups ocr,click,flow --out "$env:TEMP\dafors_rounds\r07\bench_ab" --tag "r07_$s"
}
```

判决只认同一轮内 `cand` 与 `base` 的差值中位数，不认跨轮绝对值。
基线快照 `base` 固定为 R0 冻结副本；候选快照 `cand` = base + 本轮补丁。

### F. 登记或回滚

- 采纳：`conclusion="accepted"`，写 `metric_before/metric_after`、`changes`、
  `side_effects`、`rollback`。
- 退回：`conclusion="rejected"`，写 `failure_reason` 与 `rollback.ref`（补丁路径），
  反向应用补丁并同台架复测回基线。
- 灰度回滚：参数类改动走 `evo/rollback.py`，劣化超阈值自动回滚并登记
  `conclusion="rolled_back"`。

登记入口（代码轮次）：

```python
from evo import registry
from evo.config import load_config

cfg = load_config()
entry = registry.make_entry(
    hyp, run_id, round_no, level="offline",
    sample={"planned": 30, "observed": 30},
    before={"metric": "flow.item_total_ms.p50", "value": 678.89},
    after={"metric": "flow.item_total_ms.p50", "value": 641.2},
    conclusion="accepted",
    changes=[{"file": "core/market.py", "lines": "412-430", "summary": "复用上一帧缓存"}],
    side_effects=[{"metric": "ocr.chain_price_ms.p50", "delta": "+0.8%", "note": "噪声内"}],
    rollback={"method": "patch-reverse", "ref": "impl/r07.patch",
              "verify": "同台架复测 item_total p50 回到 678.89±2%"},
)
registry.append_entries(cfg, [entry])
```

---

## 2. 阈值与增益算法

| 指标 | 字段 | 增益算法 | 采纳阈值 | 备注 |
|---|---|---|---|---|
| 端到端 | `flow.item_total_ms.p95`（判据，= `criteria.objectives.primary`）；`p50` 仅诊断 | `(before-after)/before` | **1%** | p95 判据；p50 用于噪声检查 |
| 点击占用 | `click.period_ms.p50` + `click.hold_overhead_us.p95` | 同上 | **5%** | **hold 是命中率前提，不得低于基线**；靠 interval/overhead 降周期，不靠缩 hold |
| 识别 | `ocr.chain_price_ms.p50`（小图）/ `ocr.item_recog_ms.p50`（详情） | 同上 | **2%** | 准确率红线优先；p95 退化 >3% 退回 |
| 流程步骤 | `tools/loop_metrics.py` 的 `stages_n` 与每件动作数 | 步数差 | **1 步** | 减 1 步才算收益；分支只做旁证 |
| 副作用 | 其余纳入指标 | 同上 | 退化 ≤3% | 超限即退回 |

**停止条件**：连续 **3 轮**「所有候选增益都低于各自阈值」→ 停，输出汇总：
轮次表、累计增益、已退候选及原因、剩余瓶颈清单、是否具备切在线口径的条件。

> `criteria.json` 里旧的 `convergence`（连续 2 轮 <3%）是参数实验的收敛判据；
> 代码轮次按本节的 3 轮/分指标阈值执行，且不放宽任何红线。

---

## 3. 台账结构（evo/registry.jsonl）

现状核对（2026-11-07）：

| 要求字段 | schema/1 原状 | 处置 |
|---|---|---|
| 轮次号 | `round` | 已有 |
| 假设 | `hypothesis{}` | 已有 |
| 改动清单（文件+行） | 无 | **补** `changes[]`：`{file, lines, summary}` |
| 指标前后 | `metric_before/after` | 已有 |
| 副作用 | 无 | **补** `side_effects[]`：`{metric, delta, note}` |
| 判定 | `conclusion` | 已有（accepted/rejected/inconclusive/rolled_back） |
| 回滚方式 | 无 | **补** `rollback{}`：`{method, ref, verify}` |

已落地：`evo/registry.py::make_entry` 增加三个可选参数并升到 `schema/2`；
`evo/daemon.py` 的自动回滚条目同步补字段。旧条目按空列表/空对象读，不做迁移。

`method` 允许值：`patch-reverse` / `git-restore` / `param-overlay`。
`ref` 必须是本轮目录下的真实路径；`verify` 必须写成可执行的一句话（含指标与容差）。

---

## 4. 指标口径切换：离线 → 入池率/成交率

在线口径直接用 `evo/funnel.py` 的五段漏斗（字段名即代码里的名字）：

`discover` 发现 → `click` 点击 → `pool` 进池 → `hit` 中签 → `deal` 成交（verified）。
产出指标：`click_rate`、`pool_rate`（入池率）、`hit_rate`、`settle_rate`（成交率 = deal/hit）、
`end_to_end_rate`（deal/discover）、`unverified_hit_rate`、`elapsed_p50_s/p95_s`、`skew_p95_ms`。

### 4.1 什么时候切（三条全满足）

1. 离线主指标（`flow.item_total_ms.p95`、识别 p50、点击占用）**连续 3 轮**无 ≥ 阈值增益。
2. 漏斗日志齐全且能按轮次归因：每行 `discover/click/pool/hit/deal` 带时间戳、商品 ID、
   轮次号、脚本版本指纹；`deal` 必须 `verified=true`，否则只算 `unverified_hit`。
3. 样本量达到下表门槛（方向性读数）；要作**主判据**则成交 ≥30 笔。

| 阶段 | 字段 | 方向性最小样本 | 备注 |
|---|---|---|---|
| 发现 | `discover` | 200 | 低于 200 连方向都看不出 |
| 点击 | `click` | 50 | 用来算 `click_rate` |
| 进池 | `pool` | 20 | 用来算 `pool_rate`（入池率） |
| 中签 | `hit` | 10 | 用来算 `hit_rate` |
| 成交 | `deal` | 10 → **30** | 10 只看方向；30 才可作主判据 |

### 4.2 需要多少真实成交样本

- **≥10 笔成交**：只能看方向，不能宣布胜负。
- **≥30 笔成交**：可作主判据（与 `evo` 的 `hypothesis.min_sample_floor=30` 一致）。
- 分辨率的硬账用仓库现成公式 `evo.hypothesize.two_prop_n(p1, p2)`（α=0.05，power=0.8）：

| 要分辨的差 | 每组所需样本（`discover`） |
|---|---|
| 5% → 6%（+1pp） | **8,158** |
| 5% → 10%（+5pp） | **435** |
| 10% → 11%（+1pp） | 14,751 |
| 30% → 33%（+3pp） | 3,763 |

结论：1pp 级差异不要靠成交率（样本要上万），靠上游率（`pool_rate`、`click_rate`）
与离线指标；成交率只认 5pp 以上的变化。切换时主指标用 `end_to_end_rate`
（配 Wilson 下界），再用 `pool_rate` / `settle_rate` 做归因分解。

### 4.3 切换后继续盯的旧指标（任一破线立即回滚，不看在线指标）

- `accuracy.price_exact_rate` = 1.0
- `accuracy.grade_hit_rate` ≥ 0.86
- 误买防护、输入身份不降级（`interception` 不得降级）
- `redlines.point_p95_ms` 不退化
- `flow.item_total_ms.p95`、`ocr.chain_price_ms.p50` / `item_recog_ms.p50`
- `click.hold_ms.p50` **不得低于基线**（命中率前提），只允许压 `period_ms` 与 overhead
- 自愈：`sim_fail=0`、`sim_manual_continue` 占比不升、误报成功不升
- 在线侧：`unverified_hit_rate`（中签但未校验结算的占比）、`block_rate`（被额度/预算挡）、
  `refresh_skip_rate`（刷新跳过）、错价/误买回滚次数

### 4.4 回切条件

`deal` 样本 <30、归因链断、或漏斗日志缺轮次指纹 → 回离线口径。
红线任一破 → 立即回滚到上一版，不等在线样本。

---

## 5. 本轮产物布局（仓库外）

```
%TEMP%\dafors_rounds\r07\
  snap\            # 冻结快照（bench --snapshot 指向这里）
  bench\           # perf_*.json / perf_*.md
  shape.json       # tools/loop_metrics.py 输出
  impl\r07.patch   # 唯一可合并产物
  red\review.md    # 对抗复核签字
  reg\             # 回归与相邻台架结果
  ledger.json      # 本轮登记条目（追加到 evo/registry.jsonl 前留档）
```

一键复现（本轮基线，替换 `<snap>` 与输出目录即可）：

```powershell
.venv\Scripts\python.exe perf\bench\run.py --snapshot <snap> --groups ocr,click,flow --out <out> --tag r07
.venv\Scripts\python.exe tools\loop_metrics.py --json
.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider `
  --basetemp="$env:TEMP\dafors_rounds\r07\pt" --junitxml="<out>\pytest.xml"
```
