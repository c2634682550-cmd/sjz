# 旁路数据补采：盘点 · 字段规格 · 采集器 · 覆盖率 · 冷启动

> 只读 `core/`、`web/`、`tools/` 与 `data/` 的现有产物；未改任何现有文件，未 commit/push，未开窗口、未动鼠标键盘。
> 产物只落在 `evo/collect/` 与本文。

## 0. 结论摘要

1. **真实数据几乎为空**：`data/records.jsonl` 只剩 1 行，且该行 `name` 是 1,048,576 个 `x`（压测污染行）；`data/telemetry/` 不存在；`data/shots/` 不存在。五段漏斗里只有「成交」这 1 条记录，前四段真实样本为 0。
2. **toast 文案不在任何已保存截图里**：`data/` 的 71 组裁剪图共 142 张，只有 **2 张唯一图**（币值 `13,888`、数量 `2`），是框选模板缓存，不含「已加入抢购池 / 随机点数 / 排队已满」。S3、S4 只能**运行时截取或让引擎落日志**。
3. **S1 半可解析**：列表首次看到时刻可靠（扫描周期 1.4–2.2 s），倒计时「剩余 X 分 Y 秒」可反推挂牌时刻，误差 = 扫描延迟 + 1 s 截断，只能当下界。
4. **采集器可用**：增量游标、内容哈希幂等、坏行隔离、毫秒+时区来源、窗口重跑全部通过自检；产物 `events.jsonl`（一次机会一行）与 `stages.jsonl`（单段观测）。
5. **必须改引擎的最小集合只有 3 项**：进池结果、抽签点数、挂牌时刻；其余字段旁路已能拿到或可派生。

---

## 1. 落点盘点

| 落点 | 现状 | 可提取 | 时间粒度 | 可靠度 |
|---|---|---|---|---|
| `data/records.jsonl` | 1 行；字段 `ts,t,name,price,result,elapsed,shot,mode,quota,verified` | name/price/result/elapsed(秒)/verified/mode/quota | `ts` 毫秒时间戳（本地 epoch） | **低**：唯一行是压测垃圾；且「跳过不入账」的机会天然不在这里 |
| `records.jsonl.*` 轮转 | 不存在 | — | — | — |
| `data/telemetry/`（sqlite+csv） | **目录不存在，0 行** | 按 `tools/telemetry_agent.py`：`event_id/occurred_at/tier/name/price/result/skew_ms/skip_reason` | 理论毫秒 | 无产物；设计上可补 `tier`+`skew_ms`，但需引擎侧 telemetry 服务与 `data/telemetry/telemetry.db` |
| `data/<task_id>/buy.png`+`confirm.png` | 71 个任务目录 / 142 张 / **仅 2 个唯一 md5** | 任务 id、校准写盘时刻（mtime） | 毫秒（mtime） | **低**：`buy.png`=币值 `13,888` 裁剪、`confirm.png`=数量 `2` 裁剪；无 toast 区域 |
| `data/shots/*.jpg` | **不存在**（成交截图未落盘） | 若存在：`YYYYMMDD_HHMMSS_名称.jpg` → 秒级成交证据，可对齐台账 `shot` | 秒（文件名） | 设计可用，当前 0 |
| `desktop.log` | 启动器日志，`[engine]` 行 = **0** | 启动/重启时刻 | 秒 | 对漏斗无用 |
| `_soak_data/*/soak.log` | 合成台架日志 | 发现（`第 N 件：…`）、点击（`开点（距归零还有 Xms）`）、成交（`成交…点了 N 下，用时 X s`） | 秒（文本） | 高（但 `synthetic=true`） |
| `_soak_data/*/cycles.jsonl` | 1074 次仿真机会 | `ts/duration_s/clicks/hit_roll/bought/drift_ms/seq` | 毫秒 | 合成 |
| `_soak_data/*/data/records.jsonl` | 655 行台账副本 | 同主台账 | 毫秒 | 合成 |
| `_rm_shots` / `_r08_shots` / `_ui_shots` / `_verify_bb` | 36 / 2 / 4 / 0 张 | 只有步骤号（`S0-8`、`S1-3`、`S4-6`），无时间戳命名 | mtime 毫秒，但间隔 40–70 ms 是写盘间隔 | 不可作帧序列 |
| `_video_analysis_*.md` | 屏幕语义文档 | S1 列表/倒计时、S3「已加入抢购池」、S4「随机点数 99 / 最大点数 125」、S5 结算弹窗 | 无数据 | 语义参照，可靠 |
| `_pool_entry_attribution.md` | 归因报告 | 137 成交 / 12.28 天 / 44 张卡 / p50 319.5 s；22 点 30 件、03 点 34 件 | 秒 | 高（本次规格的时间预算来源） |
| `GET /api/scheduler` | `engine.status()` → `log` 最后 **60 条** `{t,ts,level,msg}` | 发现/开点/成交文本，实时 | 毫秒（`ts` float） | **唯一实时旁路**；环形缓冲会滚动，需轮询 |

**结论**：历史产物只能证明「成交过」，不能证明「进过池/抽过签」；历史漏斗 0 覆盖是结构性的，不是解析没写好。

---

## 2. toast 是否在已保存截图里？

- `data/` 71 组裁剪图：142 张，`unique_images=2`，`per_dir_counts={2:71}`。逐像素看内容只有「币值 13,888」与「数量 2」两块，**没有 toast 条**。
- `_rm_shots` 里两组 12 帧目录（`20261107_084431`、`20261107_084610`）命名是 `S0-8/S0-10/S1-1…S4-6`，是**验证步骤截图**，不是点击前后的连续帧；间隔 40–70 ms 只是写盘速度。
- 因此：**S3「已加入抢购池 / 排队已满」与 S4「随机点数 N / 最大点数 M」必须运行时截取**。旁路可行的路径是把 toast 文本写进 `evo/collect/inbox/*.jsonl`（解析器已就绪，见 §4）；引擎自身落日志则更稳。

---

## 3. 字段规格（五段漏斗，缺失一律 `null`）

| 段 | 字段 | 类型 | 取值域 | 缺失填法 |
|---|---|---|---|---|
| 通用 | `event_id` | str | `e` + sha256 前 20 位，内容派生 | 必填 |
| 通用 | `attempt_id` | str | 列表行 id/hash + 点击时刻 | `null` |
| 通用 | `source` | str | `records\|shots\|soak_cycle\|soak_ledger\|soak_log\|live_api\|toast_ocr` | 必填 |
| 通用 | `synthetic` | bool | true=台架/仿真 | 必填 |
| 通用 | `trust` | enum | `verified\|claimed\|synthetic\|inferred\|unknown` | 必填 |
| 通用 | `ts_ms` / `tz` / `time_src` | int/str/dict | 绝对毫秒；`Asia/Shanghai`；每字段来源 | `tz` 必填，`time_src` 必填 |
| **S1 发现** | `first_seen_ms` | int ms | 首次看到该件 | `null` |
| | `forward_remaining_s` | float s | 倒计时显示原值（`X 分 Y 秒`） | `null` |
| | `listed_at_ms` | int ms | 挂牌时刻；旁路=`first_seen − remaining` | `null`（不填估算值进引擎侧表，只写派生字段） |
| | `publicity_s` | float s | `unlock_at − listed_at` | `null`（公示常量未知） |
| | `name` / `tier` / `grade` | str | 带「极品/优品」前缀则取之；`legend\|epic\|rare\|other\|unknown` | `null`（禁止猜档） |
| **S2 点击** | `click_at_ms` | int ms | 首次开点时刻 | `null` |
| | `lead_err_ms` | int ms | 点击−理论零点，负=早（台账 `offset_ms/skew_ms/drift_ms`） | `null`（禁止填 0） |
| | `lead_ms` | int ms | 日志「距归零还有 N ms」 | `null` |
| | `click_count` / `confirm_clicks` | int | 本轮/确认阶段点击数 | `null` |
| **S3 进池** | `pool_joined_at_ms` | int ms | toast 首次出现时刻 | `null` |
| | `join_result` | enum | `joined\|rejected_pool\|queue_full\|no_response\|unknown` | `null` |
| | `join_raw` | str | toast 原文 | `null` |
| | `pool_entered` | bool | 仅 `join_result` 明确时填 | `null` |
| | `queue_pos` / `queue_capacity` | int | 「队列 1/7」分子分母 | `null` |
| | `ticket_no` | str | 排队号码 | `null` |
| **S4 中签** | `roll_at_ms` | int ms | 结果 toast 时刻 | `null` |
| | `roll_self` / `roll_max` | int | 「随机点数 99 / 最大点数 125」 | `null` |
| | `won` | bool | `roll_self ≥ roll_max`（仅失败分支已验证） | `null`（成功分支文案未知前不写） |
| | `toast_source` | str | `ocr\|log\|api` | `null` |
| **S5 成交** | `deal_at_ms` | int ms | 结算弹窗/台账写入 | `null` |
| | `verified` / `verified_by` | bool/str | 结算校验结果与方式 | `null`（=`claimed` 不算 verified） |
| | `price` / `quota` / `shot` / `elapsed_ms` | float/int/str/int | 台账原值 | `null` |
| 汇总 | `stage_reached` | int 0–5 | 0=无证据；1 发现…5 成交 | 必填 |
| | `stage_evidence` | list | 每段原始证据串 | `[]` |

规则：

- **`null` 不是 0**：点数/号码/队列位置未知就 `null`；`won` 未知就 `null`，不默认 `false`。
- `trust`：`verified`（结算校验）/`claimed`（台账自称成交）/`synthetic`（台架）/`inferred`（推断，如「有台账行所以点过」）。
- 每个时间字段在 `time_src` 里标明来源：`record_epoch`、`log_text_local`、`api_epoch`、`ocr_epoch`、`mtime_local`、`derived`、`absent`。

---

## 4. 采集器 `evo/collect/`

| 文件 | 作用 |
|---|---|
| `schema.py` | 字段规格（机器可读）+ 类型/枚举校验 |
| `parse.py` | 台账/截图/台架 cycles/日志/API/toast 文本 的按行解析与时间归一 |
| `collector.py` | 游标、去重、窗口、挂段、原子落盘、实时轮询、watch |
| `coverage.py` | 覆盖率和漏斗断点报告 |
| `replay_check.py` | 截图帧序列/回放可行性评估 |
| `selftest.py` | 坏行隔离 / 幂等 / 增量 / 窗口 四项自检 |
| `events.jsonl` | **一次机会一行**（当前仅真实行 1 条） |
| `events_harness.jsonl` | 台架样本快照（1730 行，供对照，不混入真实表） |
| `stages.jsonl` | 单段观测（toast/日志/API），可挂到机会上 |
| `rejects.jsonl` | 坏行（只跳过自身） |
| `coverage.json` / `coverage_harness.json` | 覆盖率结果 |
| `inbox/*.jsonl` | **运行时 toast 文本入口**（冷启动用） |
| `.state/` | 游标 `cursors.json`、去重集 `seen.json`、盘点快照 |

保证（均已自检通过）：

1. **增量游标**：字节偏移 + 行号 + 最后一行 sha256；文件被截断/轮转自动回到 0。
2. **幂等**：`event_id` 由内容派生；连跑两次 `events.jsonl` 字节一致（`4638ac04b8cc` → 同值）。
3. **坏行隔离**：`{bad json`/`[]`/`ts:"x"`/`ts:null` 都只记 `rejects.jsonl`，后续行继续解析。
4. **时间统一**：全部整数毫秒 + `time_src`；`tz=Asia/Shanghai`。
5. **窗口重跑**：`--rebuild --since ... --until ...` 输出确定性（窗口内 1 条，全部在窗口内）。

命令：

```powershell
# 增量（默认只读真实产物）
.\.venv\Scripts\python.exe evo\collect\collector.py

# 全量重建 / 指定窗口重跑
.\.venv\Scripts\python.exe evo\collect\collector.py --rebuild
.\.venv\Scripts\python.exe evo\collect\collector.py --rebuild --since "2026-11-07 08:00:00" --until "2026-11-07 12:00:00"

# 含台架样本（synthetic=true）
.\.venv\Scripts\python.exe evo\collect\collector.py --rebuild --with-harness

# 实时旁路 + 常驻（冷启动）
.\.venv\Scripts\python.exe evo\collect\collector.py --watch --watch-interval 2 --poll-url http://127.0.0.1:8080 --poll-interval 0.3

# 覆盖率 / 回放评估 / 自检
.\.venv\Scripts\python.exe evo\collect\coverage.py --split real
.\.venv\Scripts\python.exe evo\collect\replay_check.py
.\.venv\Scripts\python.exe evo\collect\selftest.py
```

`inbox` 契约（运行时 toast 转写，一行一个事件）：

```json
{"ts": 1794010400.120, "text": "已加入抢购池", "name": "某商品", "attempt_id": "a1", "source": "toast_ocr"}
{"ts": 1794010402.350, "text": "抢购池已满，排队人数过多", "name": "某商品", "attempt_id": "a2"}
{"ts": 1794010405.980, "text": "未抢购成功，您的随机点数为 99，本轮最大点数为 125", "name": "某商品", "attempt_id": "a1"}
```

已实测解析结果：`join_result=joined / pool_entered=true`、`join_result=queue_full / pool_entered=false`、`roll_self=99 / roll_max=125 / won=false`，`attempt_id` 透传。

---

## 5. 覆盖率（真实数据 vs 台架对照）

### 5.1 真实（`events.jsonl`，1 行）

| 字段 | 填充 | 说明 |
|---|---|---|
| `name`,`price`,`result_raw`,`result_norm`,`ts_ms`,`deal_at_ms`,`win` | 1/1 | 唯一行为 `result=hit`、`ts=2026-11-07 08:11:59.179 +08:00` |
| `first_seen_ms`,`click_at_ms`,`pool_joined_at_ms`,`roll_at_ms` | **0/1** | 前四段 0 证据 |
| `tier`,`lead_err_ms`,`join_result`,`roll_self`,`roll_max`,`won` | **0/1** | 该行无 `offset_ms`/`tier` 扩展键 |

漏斗：**发现 0 → 点击 0 → 进池 0 → 中签 0 → 成交 1**。段位分布 `{5: 1}`；**前四段全黑盒**。
该行 `name` 长度 1,048,576（全 `x`），`verified` 缺失 → `trust=claimed`，不是 `verified`。

### 5.2 台架对照（`events_harness.jsonl`，1730 行：真实 1 + 仿真 1729）

| 字段 | 填充率 |
|---|---|
| `first_seen_ms` | 91.7% |
| `click_at_ms` / `lead_ms` / `lead_err_ms` | 39.1% |
| `deal_at_ms` | 31.4% |
| `elapsed_ms` | 99.9% |
| `click_count` | 62.1% |
| `confirm_clicks` | 30.1% |
| `win` | 75.9% |
| `verified` / `shot` | 37.9% |
| **`pool_joined_at_ms`** | **0%** |
| **`roll_at_ms` / `roll_self` / `roll_max` / `won`** | **0%** |
| `tier` / `price(真实业务价)` | 0% / 0.06% |

漏斗：发现 1586 → 点击 676 → **进池 0 → 中签 0** → 成交 543；到达过第 3 段的机会 1181 条，但**没有一条有时间戳**——说明台架也只写结果，不写进池/抽签。

**区分**：

- 旁路可提取：`first_seen`（S1）、`click_at_ms`/`lead_err`/`click_count`（S2，来自台账扩展或 `/api/scheduler` 日志）、`deal_at_ms`/`verified`/`price`/`shot`（S5）。
- 必须引擎：S3 进池、S4 抽签、S1 的 `listed_at/publicity`。
- 结构性缺口：`result=skip` 的机会不入账，台账不是机会全集（137 成交是幸存者）。

---

## 6. 最小引擎接口需求（只写需求，不改代码）

形式：每次机会按段追加一行 JSON 到 `data/funnel.jsonl`（或现有 `_log` 增加一条结构化记录），字段 `attempt_id, stage, ts_ms, name, 值`。

| # | 字段 | 从哪个 UI 元素读 | 解析规则 | 允许缺失时填法 | 旁路能否替代 |
|---|---|---|---|---|---|
| 1 | `pool_joined_at_ms` + `join_result`（+`join_raw`） | 屏幕中下部 toast：成功「已加入抢购池」；失败「抢购池已满，排队人数过多 / 排队已满」 | `已加入抢购池\|加入抢购池\|已进入抢购池` → `joined`；`抢购池已满\|抢购池满\|排队已满\|人数过多` → `queue_full`；时刻取 toast **首次出现帧**；同时给 `attempt_id` 才能与 S2 配对 | `null`（不写 0、不写 `pool_entered=false`） | 仅当运行时把 toast 文本写入 `inbox` 时可替代；否则必须运行时截取 |
| 2 | `roll_self` + `roll_max` + `won` | 结果 toast：`未抢购成功，您的随机点数为 99，本轮最大点数为 125` | `随机点数[^\d]{0,6}(\d{1,4})` 取自己；`(本轮\|最大)点数…(\d{1,4})` 取上限；`won = self ≥ max`（仅失败分支已证实）；**成功分支文案未知，先按 `won=false` 分支建规则** | 三个都 `null`；`won` 未知时 `null`，不默认 false | 同 #1，需运行时文本 |
| 3 | `listed_at_ms` + `publicity_s` | 列表行倒计时：`剩余 X 分 Y 秒` + 卡片 id；挂单瞬间的列表首现 | 半可解析：`listed_at = first_seen − remaining`，误差 = 扫描周期 1.4–2.2 s + 显示截断 1 s（只能当下界）；`publicity_s` 需要引擎在挂单与解锁各写一次才能精确，不同卡是否同值未验证 | `null`；不要用反推值冒充精确值（本采集器写 `time_src=derived_countdown`） | 部分可替代（S1 首次看到 + 倒计时），精度不足 → 仍需引擎写 `listed_at/unlock_at` |

> 最小集合就是这 3 项（共 5 个值 + 1 个 `attempt_id`）。其余字段（品阶、卡点误差、成交、余额校验）都由台账扩展字段或 `/api/scheduler` 覆盖。

---

## 7. 截图回放评估

`replay_check.json` 结论：**不可回放**（`replayable=false`，`sequential_dirs=0`）。

| 目录 | 帧 | 唯一图 | 跨度 | 带时间戳命名 |
|---|---|---|---|---|
| `_r08_shots` | 2 | 2 | 17 ms | 0/2 |
| `_ui_shots` | 4 | 4 | 3017 ms | 0/4 |
| `_rm_shots/*` 7 组 | 2 每组 | 2 | 44–86 ms | 0 |
| `_rm_shots/084431`,`/084610` | 12 每组 | 12 | 481/536 ms | 0（`S0-8`…`S4-6` 步骤号） |
| `data/<task_id>/*.png` | 142 | **2** | — | 0 |
| `data/shots` | 不存在 | — | — | — |

原因：没有「连续帧 + 时间戳」的产物。要支持离线回放，最低条件是：**同一机会 ≥30 帧、帧名含 `YYYYmmdd_HHMMSS_mmm` 或 `attempt_id + 帧号`、采样 ≥5 fps**，而现有截图是「校准模板 + 验证步骤快照」。

---

## 8. 冷启动方案（当前无任何历史漏斗数据）

### 8.1 时间预算（来自归因报告）

- 137 成交 / 12.28 天 ≈ **11.2 件/天**；相邻机会 p50 **319.5 s**；44 张不同卡。
- 集中档位：22 点档 12 天共 30 件，03 点档共 34 件 ≈ **2.5 件/夜/档**。
- 因此：**30 件可用样本 ≈ 3 个常规日**，或 **2 个「夜档」约 20–25 件**（21:30–01:00 + 02:30–05:00）。

### 8.2 三阶段开局

| 阶段 | 时长 | 动作 | 产出/验收 |
|---|---|---|---|
| T-0 校准（不抢） | 10 分钟 | 确认 `GET /api/scheduler` 可达；抓 3 条 `engine.log` 验证 `log_text_local` 解析；若做 toast 截取，先固定 toast 条裁剪框，用已知文案回灌 `inbox` 验证 3 条规则全中（`joined/queue_full/roll`） | 3/3 文案可解析；`attempt_id` 能配对 |
| 第 1 夜 | 21:30–01:00 | `--watch` 常驻（2 s 增量 + 0.3 s 轮询）；每出现 1 次机会，人工核对 1 次 toast 判读（前 5 件逐件核） | ≥8 件机会，S3 填充 ≥80%，S2 时间戳 ≥80% |
| 第 2 夜 | 02:30–05:00 | 同左，停止人工核对；核对 `won` 是否只在失败分支出现 | 累计 20–25 件；`join_result` 分布可算进池率 |
| 冻结 | 30 分钟 | `--rebuild` 全量重算，跑 `coverage.py`；把成功分支文案补进 `parse.py` 的规则（需 ≥5 例中签成功样本） | 规格冻结，字段填充率达标 |

### 8.3 第一天的最小运行脚本

```powershell
# 1) 冷启动前：确认引擎在跑
curl.exe -s http://127.0.0.1:8080/api/scheduler | Select-Object -First 1

# 2) 常驻采集（只读，不碰鼠标）
.\.venv\Scripts\python.exe evo\collect\collector.py --watch --watch-interval 2 `
    --poll-url http://127.0.0.1:8080 --poll-interval 0.3

# 3) 每 30 分钟看一次进度
.\.venv\Scripts\python.exe evo\collect\coverage.py --split real
```

### 8.4 冷启动期预期覆盖

| 字段 | 第 1 夜 | 第 2 夜后 |
|---|---|---|
| S1 `first_seen_ms` | 100%（列表扫描即有） | 100% |
| S2 `click_at_ms`/`lead_err_ms` | ~80%（依赖台账扩展或日志） | ~90% |
| S3 `join_result`/`pool_joined_at_ms` | ≥80%（toast 捕获成功时） | ≥95% |
| S4 `roll_self`/`roll_max` | 失败分支 ~100%，成功分支 0 | 失败 ~100%，成功待样本 |
| S5 `deal_at_ms` | 100%（台账） | 100% |

### 8.5 风险与对策

1. **toast 遮罩/Ocr 抖动**：以「首次出现帧」为准，同一 toast 多帧去重取最早；每帧写 `join_raw` 便于回溯。
2. **环形缓冲丢行**：`engine.log` 只留 60 条，轮询 0.3 s；若一次点击产生 >60 行会丢，扩容只能改引擎（列入 §6 之外的观察项）。
3. **成功分支文案未知**：`won` 仅在 `roll_self ≥ roll_max` 时判 `true`，且要求 `join_result=joined` 且 `deal` 有 `verified`；否则保持 `null`，宁可缺字段不写错。
4. **跳过不入账**：`result=skip` 的机会不会出现在台账，冷启动期进池率会被高估；必须靠 `join_result` 分母（每次点击一条）纠正。

---

## 9. 文件清单与边界

- 新增：`evo/`（`__init__.py`、`collect/` 全部）、`_bypass_collect.md`。
- 未修改：`core/`、`web/`、`tools/`、`data/` 任何文件；未运行 GUI；未 commit/push。
- 当前产物：`events.jsonl` 1 行（真实）；`events_harness.jsonl` 1730 行（对照）；`stages.jsonl` 0 行（真实产物无日志可采，跑 `--with-harness` 或写入 `inbox` 后再生）。
- 未实现（受约束）：运行时截图/OCR 抓 toast（需要真实窗口）；只给了 `inbox` 契约与解析器，运行时抓取由操作者跑或列入引擎接口需求。
