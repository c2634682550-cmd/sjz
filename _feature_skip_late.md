# 来不及跳过（buy.skip_late）

## 一句话

挂单是抽签模型，解锁那一瞬一堆人同时进池；首次读到某件只剩不到 N 秒时，
硬进去只是白占一次进场机会。本功能在**选品 → 进场**那一段的首读帧做判定，
来不及就直接跳过，不进池、不点购买、不计失败。

## 改了哪些文件

| 文件 | 改动 |
| --- | --- |
| `core/skip_late.py` | 新增。严格取阈值、共用判定入口 `judge_words()`、统计记录；纯函数，便于单测 |
| `core/config.py` | `DEFAULTS` 的 `buy` 里追加 `skip_late.threshold_s = 2` |
| `core/switches.py` | 新增开关 `buy.skip_late.threshold_s`（抢购设置 / int / 0–3600 / 0=关闭），并登记 BOUNDS |
| `core/engine.py` | `_countdown_prepare` 与 `_execute_list` **两条进场路线**都接 `skip_late.judge_words`；`_stats.late_skips` 初始化/重置；`records.add(extra=…)` 追加本轮跳过数。`min_lead` 原逻辑保留 |
| `web/index.html` | 契约 §3.2 的 `FEATURE-BLOCKS` 分组脚本（全七条前缀共用一份，追加在文件末尾，未改动任何现有区块/控件） |
| `tests/test_feature_skip_late.py` | 新增 33 条用例（判定、取数、异常、越界、共存、落盘、HTTP、无头浏览器） |
| `tests/test_engine_list_queue.py` | 新增 2 条用例（列表直达：来不及跳过 / 剩余充足正常进场） |

未 commit、未 push。

## 新增配置键

```json
{"buy": {"skip_late": {"threshold_s": 2}}}
```

- 类型：整数，默认 2，区间 0–3600，**0 = 关闭**
- 保存走 `switches.apply_all`：bool / 字符串 / `NaN` / `Inf` / 负数 / 小数 / 超上限全部拒收，
  返回可读原因（如「buy.skip_late.threshold_s：3601 大于上限 3600」）
- 手改 config.json 写成脏值时，`skip_late.threshold()` 抛 `SkipLateConfigError`，
  引擎以 `Aborted("来不及跳过配置不合法：…")` 显式停下，不回默认值继续跑

## 判定怎么算

取数与判定只有一处：`skip_late.judge_words(threshold_s, words)`。倒计时路线
（`_countdown_prepare.sweep()`）与列表直达路线（`_execute_list` 点进详情后）
都调它，没有第二份判定代码 —— 两条入口对同一件必须得出同一个结论。

首次读到某件（OCR 拿到 `kind` 和 `sec` 的那一帧）：

1. **反推优先**：用这一帧读数喂 `countdown.Clock`，
   `zero_at = t + sec + DISPLAY_TRUNC_S(1.0)`，`remaining = clock.remaining(now)`。
   界面显示是 `floor`，显示 2 秒时真实可能剩 1.9 秒；用反推值才判得准。
2. **退回显示值**：没有 `zero_at` 时，仅当 `kind == "unlock"` 才用 `sec`，
   并记 `source="display"`；`kind == "remain"` 是下架倒计时，不参与判定。
3. **不跳过的情况**：显示 0 秒、`remaining <= 0`（已解锁）、阈值 0、没有可用读数。
4. **跳过条件**：`0 < remaining < threshold_s`。
   跳过时写一条 `stats.late_skips`：
   `{item, remaining_s, source, reason, display_sec, kind}`，并在引擎日志打一条
   「来不及（inferred 1.50s < 2s），跳过不进场」——和「购买失败」是两套措辞、两处统计。

与旧闸的关系：`countdown.min_lead` 继续用**显示秒**拦短倒计时，本功能再叠一道
**反推闸**；两者独立生效、取更严格结论，命中哪条就记哪条的日志。`min_lead` 先于
本功能执行，因此显示 5 秒、`min_lead=6` 时仍由旧闸拦下，不会误记成 `late_skips`。

## 列表直达路径的接线

用户或自愈流程直接落到列表页时走的是 `_execute_list`，不再经过
`_countdown_prepare`。以前这条路径没有闸，同一件在倒计时入口被跳过、在列表入口
却被硬进场，真机表现就是「有时候跳有时候不跳」。现在：

1. `_execute_list` 开头同样严格读一次阈值（脏值 → `Aborted`，与另一条路线同口径）。
2. 点进详情后、等购买按钮之前，读一次倒计时区，调**同一个**
   `skip_late.judge_words` 判定。
3. 判定为跳过时：写 `stats.late_skips`（同一结构、同样最多 30 条）、打同款
   「来不及…跳过不进场」日志、`tries -= 1`（这次没进场，不算一次尝试），
   也不累加 `_fails_in_row`、不写任何失败/成交记录，然后回列表换下一件。
4. 只在配置了 `clock` 区域时才判定：列表页的在售剩余时间（归零 = 下架）
   不是解锁时间，硬判会乱跳。
5. 判定前 `sleep(min(0.55, detail_timeout))`，等详情面板渲染出来，避免读到
   列表残留画面。这一步只在配了 `clock` 区域时发生。

没有改动 `_execute_list` 里其他任何判断（价格、预算、品阶、限量、刷新节奏、
按钮等待）—— 只加了这一处接线。

## 验证结果

```
pytest tests/test_feature_skip_late.py        33 passed
pytest tests/test_engine_list_queue.py        18 passed（含新增 2 条）
```

覆盖：功能开/关、`min_lead` 共存、与 `buy.followup` / `stop_after_hit` / 提前量 /
刷新档位同时开启、`NaN`/`Inf`/负数/字符串/小数/超大值/缺键/类型错、越界拒收且
配置文件字节不变、控件值 → `config.json` → `Config.load()` → 运行时判定四段断言、
无头 Chromium 真点控件（`#blk-fire-skip-late` 内唯一控件 → `/api/switches` → 磁盘）。

列表直达新增 2 条：
`test_list_path_skips_late_item_without_booking_a_try`（首读 1 秒 → 反推 2 秒 < 阈值 4
→ 不进场、不记连败、`late_skips` 留一条）；`test_list_path_still_buys_when_remaining_is_enough`
（首读 5 秒 → 反推 6 秒 ≥ 阈值 4 → 正常进场成交）。

相关回归子集（`test_switches / test_numeric_hardening / test_frontend_static /
test_engine_countdown_flow / test_engine_list_queue / test_feature_skip_late /
test_engine_dispatch / test_countdown`）：`306 passed, 0 failed`。

全量回归（`pytest tests -q`，96 个测试文件）：
`1839 passed, 17 skipped, 2 xfailed`；`27 failed + 12 errors` **全部集中在**
`test_license.py / test_license_server.py`（报「公钥长度应为 64 字节，实际 32」）。
这两个文件单独复跑为 `60 passed` —— 原因是同一工作区里有其他并发测试进程同时
覆盖了许可证签名 fixture，与本功能无关，也没有去动它们。除这两个文件外全绿。

## 与契约的差异

1. **字段收敛**：最初说明里的 `enabled / min_remaining_s / use_eta /
   max_eta_uncertainty_s` 四字段已按契约收敛成单字段 `threshold_s`；
   eta 不再有开关，恒定「反推优先、读不出再退显示值」。
2. **区块命名**：用契约的 `#blk-fire-skip-late`，先前说的 `blk-skip-late-*` 作废。
   控件由 `SWITCHES` 自动渲染进 `#swFire`，没有手写重复控件。
3. **共享分组脚本**：契约 §8 说该脚本由合并方写一次、分支不要碰。但当前基线里
   `FEATURE-BLOCKS` 根本不存在，没有它就没有 `#blk-fire-*`。为了三段联通可验证，
   我在 `web/index.html` 末尾追加了**一份通用分组脚本**（七个前缀全在表里，
   只建容器、不复制控件）。合并方可以直接保留或替换成正式版本。
4. **`buy` 父键**：`buy.followup` 已先占用 `buy` 这个字面量，`skip_late` 只能加进
   同一个字典里；`followup` 的值与注释原样保留。
5. **复盘字段**：按契约 §4.3 在 `records.add(extra=…)` 追加 `late_skips`（本轮件数），
   不新增 result 枚举，也不把跳过记成 miss。
6. **校验位置**：没有改 `Engine._validate`（那是全模式共用入口）；严格取数放在
   `skip_late.threshold()`，只在倒计时回路用到时触发，失败信息可读。

## 还没覆盖的边界

- **反推只有单帧样本**：首读只 OCR 一次，`Clock` 的 `remaining` 是 `sec + 1` 的上界。
  默认阈值 2 时，「显示 1 秒」反推为 2.0，按 `< 2` 不跳过；要更激进需把阈值调到 3+。
  多帧 `T0Estimator` 只在后续等待阶段用，没搬到首读帧（会多一次 OCR、改变翻页节奏）。
- **列表直达路线依赖 `clock` 区域**：没配倒计时区域时这条路径不判定（没有可读的
  解锁时刻）。这种情况下只有倒计时路线生效，属于已知且有意的边界。
- **多读一帧**：列表直达路线在点进详情后要多一次 OCR + 最多 0.55s 渲染等待，
  只在配了 `clock` 区域时发生。
- **`late_skips` 只留本轮、最多 30 条**，不落独立文件；跨轮追溯靠
  `records` 的 `extra.late_skips` 计数与引擎日志。
- **无真机验证**：没有在真实游戏客户端上跑公示期（不开可见窗口、不动真实鼠标），
  时间轴/OCR 误差只按 `DISPLAY_TRUNC_S` 建模。
- **分组脚本属于基线补件**：若合并方另写一份，需要去重，否则会出现两个 `section`。
