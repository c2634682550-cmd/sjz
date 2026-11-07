# 连点参数（click.burst.*）实现报告

## 1. 范围

只做「连点参数」这一项。共享文件里只加了 `FEATURE-CONFIG` / `FEATURE-SWITCHES:click.burst` /
`FEATURE:click.burst` 三个起止块，以及 `web/index.html` 末尾独立的
`CLICK-BURST-NOTICE` 块；没改其它功能的键、区块或共享 FEATURE-BLOCKS 脚本。

语义（界面说明与代码注释同口径）：抢皮肤是**抽签模型**（点进去拿号 → 摇号 → 点数比大小），
连点只缩短「没中之后再次进池」的延迟，**不提高**单次中签概率。补点是正常点击，
重复进场走游戏自己的排队，不绕过单次进场上限。

## 2. 改动文件

| 文件 | 类型 | 内容 |
|---|---|---|
| `core/click_burst.py` | 新增 | 四字段严格校验、计划读取、连点执行器（可注入假时钟）、互斥闸门 `followup_gate()`、共用文案 `MUTEX_WARNING/MUTEX_REASON` |
| `core/config.py` | 追加 | `click.burst` 默认值：`mode=append / hold_ms=100 / interval_ms=5 / window=all` |
| `core/switches.py` | 追加 | 4 个开关项（`strict`/`integral`、中文选项、毫秒 hint、抽签说明）+ BOUNDS 两条 |
| `core/input.py` | 追加 | `Mouse.click(..., hold=None)` 把按住时长透传到 driver / legacy / message 三通道；`hold=None` 时保持原行为 |
| `core/engine.py` | 追加 | import `click_burst`；`_click(..., hold=)` 透传；`_fire_and_confirm` 连点区块；`burst_clicks` 记录；补刀互斥 info 日志改用共用文案 |
| `core/server.py` | 追加 | `/api/switches` 保存成功时返回 `warnings` |
| `web/index.html` | 追加 | 末尾 `CLICK-BURST-NOTICE` 块：在 `#blk-fire-burst` 下方显示互斥提示，并直接展示保存回包的 `warnings` |
| `tests/test_feature_click_burst.py` | 新增 | D1–D5 与默认值/核验直断共 41 个用例 |
| `tests/_fe_click_burst_dom.js` | 新增 | 无头 DOM 垫片，执行真实共享分组脚本 |
| `tests/_fe_click_burst_notice.js` | 新增 | 无头 DOM 垫片，执行真实的 CLICK-BURST-NOTICE 脚本（含假 fetch 回包） |

## 3. 配置键（只有这四个）

| 键 | 类型 | 默认 | 区间 / 可选值 | 中文字面 |
|---|---|---|---|---|
| `click.burst.mode` | select(strict) | `append` | `append` / `burst` | 追加点击 / 连点 |
| `click.burst.hold_ms` | int(integral) | `100` | 20–1000 | 按住时长（毫秒） |
| `click.burst.interval_ms` | int(integral) | `5` | 1–50 | 点击间隔（毫秒） |
| `click.burst.window` | select(strict) | `all` | `all` / `1s` / `2s` | 全部 / 1 秒 / 2 秒 |

`BOUNDS` 同步为 `click.burst.hold_ms: (20, 1000)`、`click.burst.interval_ms: (1, 50)`；
`test_numeric_bounds_cover_every_numeric_switch` 因此通过。

拒收：非数字、`NaN`/`Infinity`、布尔、小数、负数、越界、非法枚举 → `SwitchValueError`
（带键名 + 可读原因），批量整批回滚不落盘；手改 `config.json` 写坏 → `read()` 收集原因，
`begin()` 用 `_say(..., "warn")` 打出，不静默变默认；缺键（老配置）才走默认。

## 4. 时长与间隔的实际生效路径

```
控件 (data-sw=click.burst.*) → POST /api/switches
  → switches.apply_all → _coerce（strict/integral + BOUNDS）
  → cfg.data["click"]["burst"] → Config.save()
  → 下一轮 Config.load() → click_burst.read(cfg) → plan
  → click_burst.run(plan, t_fire, fire_timeout, click=..., dialog=...)
        stage = min(window 秒数 或 fire_timeout, fire_timeout)
        end   = t_fire + stage
        循环：剩余 < hold_ms → 停手（不压缩 hold）
              click(hold_ms/1000)  ← 真正的按住时长
              弹窗出现 → 停手（一下都不多点）
              抬起后按 interval_ms 等（4ms 采样，弹窗/停止立刻可见）
  → click = _click_buy(hold) → Engine._click(hold=) → Mouse.click(x, y, hold=)
        driver：DOWN → time.sleep(hold) → UP
        legacy：LBUTTONDOWN → sleep(hold) → LBUTTONUP（None 时仍为原 60ms）
        message：nocursor.Clicker.click(hold=)（零位移）
```

三个时间量：`window` 决定阶段时长，`hold + interval` 决定每次形态。
窗口内能排下几次就点几次：`n = floor(stage / (hold + interval))`，第 n+1 次只剩
不足一次完整按住时不再开始，绝不为了凑次数压 hold。

实测（假时钟）：`hold=100 / interval=5 / window=1s` → 9 次，每次按住 100ms，
相邻按下起点间距 105ms，阶段耗时 ≤1004.5ms；`window` 三档 stage = 500 / 1000 / 2000ms；
`fire_timeout=50ms` 时 2 次、≤54.5ms；`fire_timeout=120ms` 且 hold=100ms 时只点 1 次
（剩余 15ms 排不下，停手）。

## 5. 共存 / 互斥

| 组合 | 规则 | 证据 |
|---|---|---|
| 连点 + 买到即停 | 确认弹窗一出现，连点立即归零（`stopped_by="dialog"`），随后才做成交/停手判定 | `test_stop_after_hit_wins_over_burst`、`test_engine_burst_stops_clicking_once_dialog_appears` |
| 连点(burst) + 假购买补刀 | **可保存**，运行时只按连点执行：`followup_effective=False`、`used=False`、`_followup_used=False`，并打 info 日志 `MUTEX_REASON` | `test_followup_never_fires_when_burst_is_on` |
| 保存回包 | 冲突时 `{"ok": true, ..., "warnings": ["连点与补刀互斥，当前按连点执行"]}`；两个值都落盘 | `test_switches_save_returns_warning_but_keeps_both_values` |
| 前端可见 | `#blk-fire-burst` 下方常驻提示条，冲突时显示；保存回包的 warnings 直接展示，不依赖日志 | `test_mutex_notice_visible_headless` |
| 连点(append) + 假购买补刀 | 追加点击不改变开火频率，补刀按原逻辑生效，`warnings` 为空 | 同上 |

## 6. 验证结果（数字）

`tests/test_feature_click_burst.py`：**41 passed**（D1–D5 37 条 + `hold=None`
三通道直断 1 条 + `verify=True` 有界轮询 3 条）。

- D1：hold/interval 形态（9 次 + 每次 100ms 容差）、弹窗后零点击、`window` 三档
  （参数化 3 条）、fire_timeout 截断、不压缩 hold、append 不点。
- D2：`snapshot` 四键（kind/default/options/hint）→ `apply_all` 落盘 → `Config.load()`
  读回 → 引擎侧按 `hold+interval` 改变点击形态，三段各有断言。
- D3：8 个参数化坏值 + 配置层布尔/负数 + 整批回滚 + 手改配置报原因。
- D4：hold 19/1001、interval 0/51 四条越界 + 拒收后文件字节不变。
- D5：`followup_gate` 原因可读、保存成功且返回 warnings、warnings 为空的反例、
  引擎里补刀 0 次、引擎弹窗第 3 下后停手、买到即停优先。
- 前端：markers / MAP 行 / 抽签原句 / 4 控件来自 SWITCHES / `data-sw`·`data-kind`
  渲染契约 / node 无头分组 / node 无头互斥提示。

`pytest tests/test_switches.py tests/test_numeric_hardening.py`：**93 passed、1 failed**。
失败为跨分支遗留：`test_fire_and_confirm_survives_hand_edited_out_of_range_config`
把 `_click` 桩写成三参数 `lambda m, x, y`，而共享 `engine._click_buy()` 现在带
`fire=True` 关键字（telemetry 分支加的），与本功能无关；
`test_numeric_bounds_cover_every_numeric_switch` 单独复跑 **1 passed**。

`pytest tests/test_feature_click_burst.py tests/test_input_channels.py tests/test_fix_e.py`：
**80 passed**（41 + 20 + 19），0 failed。

## 7. 与先前口径的差异（已按最新拍板改写）

1. **`duration_ms` 作废**。旧语义「连点阶段总时长」已删除；阶段时长一律由 `window`
   （或 `fire_timeout`）决定。单次形态由 `hold_ms`（按住）+ `interval_ms`（间隔）决定，
   三量物理自洽，排不下就不点，不压缩 hold。
2. **互斥位置**：保存不拒绝（两个值都落盘），`/api/switches` 回包带 `warnings`，
   前端在连点区块下方显示；运行时 `followup_gate()` 硬拒绝补刀并留 info 日志，
   同一句文案服务端与前端共用。
3. `max_clicks` / `enabled` 未保留；不存在第二套命名。
4. 前端控件仍由 SWITCHES 渲染、共享脚本归组；本次只追加了独立的提示块，
   没改共享 FEATURE-BLOCKS 脚本。

## 8. 未覆盖边界

- 真实通道抖动：`Mouse.click` 的 IPC 自身耗时会计入抬起→下次按下之间，真机间隔
  ≥ `interval_ms`；`interval_ms=1` 约等于通道下限。
- 弹窗识别延迟取决于 `screen.grab` + 模板匹配，未在真机验证（按纪律不开可见窗口）。
- `window=1s/2s` 是上限；`fire_timeout` 更短时由 `fire_timeout` 先收。
- 多任务并发：连点只在单次 `_fire_and_confirm` 内生效，重复进场仍由既有 scheduler 控制。
- 抽签上限与摇号结果不可测；补点不影响服务端进场上限。

## 9. legacy 通道睡眠序列：改动前 / 改动后对照

对照基线：`git show HEAD:core/input.py` 的 `Mouse._click_messages`（HEAD 版还是
`(self, x, y, verify=True)`，没有 `hold`）。当前实现逐行对齐如下（`hold=None`）：

| 阶段 | HEAD（改动前） | 现在（hold=None） | 是否一致 |
|---|---|---|---|
| `SetCursorPos` 之后 | `time.sleep(0.04)` | `if prepared is None: sleep(0.04)`（本用例 prepared=None） | 一致 |
| `_WM_MOUSEMOVE` 之后 | `sleep(0.02)` | `move_wait = 0.02`（prepared=None） | 一致 |
| `_WM_LBUTTONDOWN` 之后 | `sleep(0.06)` | `down_hold = 0.06 if hold is None` | 一致 |
| `_WM_LBUTTONUP` 之后 | 不睡（`0.0` 只占位） | 不睡（`0.0` 只占位） | 一致 |
| `verify=True` 复核前 | `sleep(0.05)` 后只读一次 | 无固定睡：`_verify_cursor_at` 有界轮询（1ms 步长、50ms 上界） | **被替代**，不是删除 |

因此 `verify=False` 路径改动前就是 **`[0.04, 0.02, 0.06]`**，与现在完全一致。
先前口头说的「`0.02 / 0.06 / 0.0` 三步」只描述了 `steps` 元组本身，漏掉了
`SetCursorPos` 之后的 0.04；更早输入链路报告里的「`0.04 / 0.06 / 0.05`」是
`verify=True` 路径（不含 move 的 0.02）。三处说法统一到上表。

0.05 的消失发生在 `mouse.human` 那次改动（源码注释写明「避免 legacy 每次点击
额外多 50ms」），不是连点参数引入的；连点参数只把 `down_hold` 从固定 0.06
改成 `0.06 if hold is None else hold`，`hold=None` 时数值与 HEAD 完全相同。

### 9.1 0.05 的去向：有界轮询（本次修正）

「立刻回读」有竞态：`SetCursorPos` / 合成输入落地不是瞬时的（要过消息泵，实机
还有 DPI 缩放与多屏换算），早一帧读到旧坐标会被判成 `CURSOR_NOT_AT_TARGET`
→ 上层按点击失败重试或中止。为了省 50ms 制造假失败不划算，改成两头都避开：

| 项 | 值 |
|---|---|
| 轮询步长 `_CURSOR_POLL_S` | `0.001`（1ms） |
| 到位上界 `_CURSOR_VERIFY_BUDGET_S` | `0.050`（50ms） |
| 快路径 | 光标已在目标点 → 第 1 次回读即返回，等待 ≈0ms（比原来快 50ms） |
| 慢路径 | 每 1ms 回读一次，到位立刻放行 |
| 超时 | 到 50ms 仍不到位才抛 `CURSOR_NOT_AT_TARGET`，原因里带实际等待毫秒数与回读次数 |
| 读不到光标 | 与原行为一致：`ok=None` 放行，不判失败 |

实测（假 `GetCursorPos` 按「距本次开始多少毫秒」切换旧坐标/目标点，模拟落地
延迟；300 次采样：60% 立即到位、30% 0.3–3ms、10% 3–15ms；全程不碰真实光标）：

| 指标 | 值 |
|---|---|
| 到位耗时 P50 | 0.002ms（第 1 次回读就到位） |
| 到位耗时 P95 | 7.556ms |
| 到位耗时 max | 15.284ms |
| 回读次数 max | 13 |
| 超时率 | 0.000000（300/300 成功） |

即：以前每次固定付 50ms；现在到位就立刻走（P50 近 0），最坏 15.3ms（受模拟
延迟上限约束），上界 50ms 只在真异常时兜底。

钉住的用例（均在 `tests/test_feature_click_burst.py`）：

- `test_verify_poll_accepts_arrival_after_one_stale_read`：假 user32 第 1 次回读
  旧坐标、第 2 次目标点 → 判成功（`ok=True`、`polls==2`），只等了 1 个步长。
- `test_verify_poll_timeout_reports_waited_ms`：一直不到位 → 抛
  `CURSOR_NOT_AT_TARGET`，原因含「等待 X ms」「上界 6ms」。
- `test_verify_true_sequence_keeps_three_sleeps_and_no_fixed_50ms`：`verify=True`
  完整序列 = `[0.04, 0.02, 0.06]`，没有 0.05 固定睡；同时断言本轮确实经过
  轮询（`ok=True`、`waited_ms < 5`）。


