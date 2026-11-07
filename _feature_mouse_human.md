# 拟人鼠标轨迹（mouse.human）

**一句话**：点购买前的移动不再瞬移，按 WindMouse / 三次贝塞尔 / 真人轨迹包之一走一条带加速—减速的曲线；只在真会移动光标的通道算数。

| 文件 | 改动 |
| --- | --- |
| `core/humanmouse.py` | 新增（411 行）：`read_spec` 严格取三键、`plan` 纯计算出点序列、`Trajectory` 通道/轨迹包两级降级 + 开火期封顶 |
| `core/config.py` | FEATURE-CONFIG 块内追加 `mouse.human.traj/move_ms/curve`（与 `mouse.idle` 同放一个 `"mouse"` dict，合并提示已写在块内） |
| `core/switches.py` | 三条目（抢购设置，紧跟 `fire.verify` 后）+ BOUNDS `move_ms(20,800)` `curve(0,1)`；`select` 的 `strict` 拒收分支只加在 `_coerce` 里，`integral` 复用已有实现 |
| `core/input.py` | 追加 `trajectory_capability()`、`Mouse.attach_trajectory()`、`Mouse.move_path()`、`Mouse.preheat()`、`Mouse._prepared_hit()`、`Mouse._prepared_tail()`、`Mouse.close_tail()`、`click(..., path=None, fire=None)`；`path` 缺省且未挂载 → 与改动前逐字节一致；开火点击读 `_fire_hint`，有预热落点零位移、移位 ≤120px 走 8–20ms 收尾短移、没有窗口才瞬时落点 |
| `core/engine.py` | §4.5 扩展点内：`_run` 挂轨迹并打降级日志；`status()["stats"]["trajectory"]` 回读；`_watch_countdown` 在 `due` 前预热、`_fire_and_confirm` 按 `delay` 预算预热、`_click_fire()` 给开火点击打标记（不改 `_click` 签名） |
| `tests/test_feature_mouse_human.py` | 新增 48 条：D1 点序列/速度曲线、D2 HTTP+无头浏览器三段、D3/D4 拒收文案、D5 共存与降级、开火精度（预热/零位移/分段预热/降级计数/实测分布）；`web/index.html` 未改（复用已存在的 FEATURE-BLOCKS 分组脚本，`mouse.human.` → `#blk-fire-human`） |

**降级两级，都在 `stats.trajectory.effective` 里**：① 通道（`message` → `degraded`，文案「纯消息通道无真实移动，拟人轨迹不生效」；`input_fallback` 落到 legacy → `legacy`）；② 轨迹包（`pack` 但 `data/traj` 空/坏 → `pack_fallback_auto`，实际生成器走贝塞尔并在 `last.type` 标注）。盘上配置脏 → `invalid_config` + `problems` 原文，且 `active=False`，不静默套默认。
**通道取舍（「无条件生效 vs 仅 legacy」我选前者）**：驱动注入虽会临时移动系统光标（`_click_driver` 点击后还原），轨迹在拦截通道看得见也有效，所以 `interception`/`legacy` 都生效，只排除 message——按「会不会动真光标」而不是按通道名字来判，才不会在默认通道下谎称生效。
**与派发说明的差异**：派发里的 `enabled/trajectory/accel_ratio/decel_ratio/max_speed/min_duration_ms/jitter_px` 全部收敛到契约三键，不再并存：加速/减速段固定 0.28/0.42，手抖幅度由 `curve` 派生（≤3px，末端归零），最大速度不再单独可调（实测 500px/120ms 峰值 6158px/s）；开火期封顶 `min(move_ms, 0.25×点击间隔, 30ms)`。另因 `_click` 不在扩展点内，额外加了 `attach_trajectory()` 这一处接线（无 hook 时 `path=` 无人可传）。「当前通道下此功能不生效」写在开关 desc 里（不改 `index.html`，就没做实时通道徽标）。
**证据**：`python -m pytest tests/test_feature_mouse_human.py -q` → 36 passed；实测 400,300 起点(0,0)：10 点、120ms、速度 `1815→6158→884 px/s`（峰/谷 6.96×）、离弦最大 11.43px。未 commit、未 push、未动真实光标（message 分支用 `Mouse.__new__` 探语义，前端用无头 Chromium）。

---

## 开火精度：轨迹不吃卡点

### 1. 开火零位移（预热在提前量窗口内完成）

- `_watch_countdown`：剩余时间进入 `FIRE_LEAD_S + max(0.30, move_s + 0.12)` 时调 `Mouse.preheat(buy_click)`，把整条曲线（完整 `move_ms`，**不受**开火期 30ms 封顶）在解锁前走完，落点缓存进 `_prepared`。
- `_fire_and_confirm`：jitter 算完、`sleep(delay)` 之前再预热一次（幂等，已命中直接返回），**并把预热实际耗时从 `delay` 里扣掉**（`delay = max(0, delay - spent)`），所以开火的绝对时刻与改动前完全一致——预热挤占的是等待时间，不是卡点时间。
- 开火点击走 `_click_fire()` → `mouse._fire_hint=True` → `Mouse.click(..., fire=True)`：`path` 恒为 `None`，`_click_driver` 的 `if path: _emit_plan()` 不执行，只发一次 target 上的 move + down + up。`_click_messages` 在 `prepared` 命中时跳过 40ms 等光标与 20ms move 等待。
- 真机 843ms 提前量 + 120ms 曲线：实际等待从 843ms 变成约 723ms，按下时刻不变。

### 2. 目标未知 / 提前量不足

不硬走曲线，直接**瞬时直线落点**：`preheat` 发现 `budget_s + 2ms < move_ms` 时不移动光标，记 `tail_jump=True`、`tail_budget_ms=1.0`；开火点击仅靠 down 前那一次 move 事件把光标放到按钮上。

- 尾部微移时间上界：**1ms**（`TAIL_JUMP_BUDGET_MS`），只有一次 `send_mouse` 注入，不跑插值循环。
- 缓存有效性：目标坐标误差 ≤1px、光标实际位置误差 ≤2px、年龄 ≤10s（`PREHEAT_MAX_AGE_S`）任一不满足即视为未预热，走瞬时落点。列表刷新导致按钮移动 → 旧 `_prepared` 失效 → 瞬时落点，不会抓着错误坐标空点。

### 3. 实测数字

假 user32 + 假 Interception 驱动（每次注入 busy-wait 0.12ms，接近真机 p95 0.843ms 的量级），预热 120ms / 10 点，连续 50 次开火：

| 指标 | p50 | p95 | max |
| --- | --- | --- | --- |
| 开火按下→松开（`press_ms`） | 0.242ms | 0.293ms | 0.569ms |
| 最后一次移动结束→按下（`gap_ms`） | 106.771ms | 211.3ms | 219.602ms |
| 单次点击占用（`occupancy`） | 1.023ms | 1.531ms | 2.891ms |

- `fire_moves_per_click = 1.2`：50 次开火只有 60 个 move 事件 = 预热 10 点 + 每次开火 1 个落点 move，开火路径**没有额外曲线事件**。
- 真机数字从 `status().trajectory.fire` 读：`{n, press_ms{p50,p95,max}, gap_ms{...}, last, tail_jump_budget_ms}`，样本上限 200。gap 是「预热结束到按下」的真实间隔，不是基准脚本的循环间隔。

### 4. 为什么没有第 4 条要求的新开关

零位移已达成（预热覆盖整条曲线，开火只发 down/up），不需要「开火退回直线」的降级开关。真正的降级条件由运行时预算判定，自动发生并在日志里写明文案：

- 预热成功：`拟人轨迹：光标已预热到购买按钮，开火瞬间零位移`
- 预算不足：`拟人轨迹：提前量不足，本次开火按瞬时直线落点（不做曲线移动，不占用卡点时间）`

非开火动作（`_plan_path` 覆盖的移动、滚轮、漫游点击）仍走完整曲线，`trajectory_capability()` 的 `effective` 也如实反映通道是否生效。

### 与契约的差异

- 未新增配置键：仍是 `mouse.human.traj/move_ms/curve` 三键；预热预算、尾部上界是内部常量。
- `_click` 签名未改（沿用扩展点），开火标记通过 `mouse._fire_hint` 传递，避免打断既有 monkeypatch 测试。
- `status().trajectory` 多一个 `fire` 字段（只读观测，不影响既有键）。
- legacy 通道的按下固定含 60ms hold，所以 legacy 下 `press_ms ≈ 60ms` 是既有设计；杀手级路径是 interception（实测上表）。

### 测试

- `python -m pytest tests/test_feature_mouse_human.py -q` → **48 passed**
- `python -m pytest tests/test_feature_mouse_human.py tests/test_click_speed.py -q` → **53 passed**
- 开火相关 10 个文件（input_interception / input_channels / input_review_fixes / engine_shot / engine_countdown_flow / engine_no_dialog / feature_click_burst / fire_proc / fire_process / feature_mouse_idle）→ **142 passed, 2 skipped**
- 前端静态段用无头 Chromium，仅注入 `localStorage.dafors.guided=1` 规避共享首页引导弹窗，`web/index.html` 未改。
- `blk-fire-grade` 静态断言失败属另一分支区块，本路未触碰。

---

## 降级路径：瞬移特征怎么收，代价多少

### 1. 先量化「开火瞬移」的触发面

按代码结构，开火点击的移动有三档，全部写进 `stats.trajectory.fire.last.mode`：

| mode | 含义 | 触发条件 |
| --- | --- | --- |
| `zero` | 预热落点命中，开火零位移 | 预热后光标仍在按钮 ±2px、坐标 ±1px、年龄 ≤10s |
| `segment` | 分段预热 / 收尾短移，3 点、8–20ms、位移 ≤120px | 预热时预算不足；或预热后按钮移位 ≤120px |
| `straight` | **瞬时直线落点（瞬移特征）** | 预热完全没窗口、移位 >120px、或收尾来不及 |

真机典型场景的触发面（结构性估计，最终以现场 `stats` 为准）：

| 场景 | 改造前 | 改造后 |
| --- | --- | --- |
| 倒计时稳定点击 | zero | zero |
| 列表刷新导致按钮移位（前 0.4s 内，≤120px） | **straight 100%**：倒计时预热的 `_tail_s=0`，没有收尾窗口 | `segment`：`close_tail` 在 due 前补回 |
| 首次进详情页（预热时坐标还没定位） | straight | straight（无预热窗口，属真实未知） |
| 窗口切换后（光标被挪走 >120px） | straight | straight（超短移上限，不再硬凑） |
| 连点阶段（每次点击后还原光标） | straight | straight（既有行为，不计入卡点） |

- 结论：**卡点首击的降级不再是「只要移位就瞬移」**。改造前倒计时路线只要在预热后发生移位就是 100% 瞬移；改造后 ≤120px 且早于 due 前 ~40ms 的移位全部转成 `segment`。
- 剩下的首击瞬移只剩三类：预热时目标未定位（首次进详情）、移位 >120px（窗口切换/换页）、移位发生在 due 前最后一个 OCR 帧（~20–40ms 窗口）。稳态倒计时下估 **<2%**；首次进详情/窗口切换是**每会话个位数次数**，但单次必瞬移。
- **不做拍脑袋的百分比**：真机数字只能现场读 `stats.trajectory.fire.degrade.all.straight_rate`。这个字段就是为这件事加的：全量、首击、连点三档分别计数，不写日志也能事后统计。

### 2. `stats.trajectory.fire` 的降级计数字段

`status()["stats"]["trajectory"]["fire"]` 新增（只读，不影响既有键）：

```
fire: {
  n, press_ms{p50,p95,max}, gap_ms{...},
  press_lag_ms{p50,p95,max},        # 按下时刻 − 计划开火时刻，正=偏晚
  last: {mode, jumped, preheated, first, lag_ms, gap_ms},
  degrade: {
    all:   {n, zero, segment, straight, straight_rate, segment_rate},
    first: {...},                   # 卡点第一击
    retry: {...},                   # 等弹窗的连点
    segment_reserve_ms: 20,
    tail_window_ms: [8, 20],
    tail_max_px: 120
  },
  tail_jump_budget_ms: 1
}
```

- `straight_rate` 就是「瞬移特征占比」；`first` / `retry` 拆开，避免连点把卡点数字冲淡。
- 开火瞬间退回瞬移时，`_last_path.reason` 明确写「**本次开火存在瞬移特征**：提前量不足/无提前量窗口，已计入 stats.trajectory.fire.degrade」，引擎同步打 warn 文案；这是**已知取舍**，不是无声降级。

### 3. 分段预热：按下时刻误差与占用增量

预热点停在按钮前的安全区（`stop` 点），开火前用 `tail_path` 走 8–20ms、≤120px 的短移收尾；短移窗口从等待时间里预留，按下时刻由绝对截止时刻 `_fire_deadline` 对齐（短移先走完，再 `_wait_fire_deadline(0)` 睡眠对齐，不靠 sleep 累积）。

实测（假 user32 + 假 Interception 驱动，空闲机器，每档 80 次）：

| 场景 | mode | 按下误差 p50 | p95 | max | 尾部预留 |
| --- | --- | --- | --- | --- | --- |
| 移位 60px / 预算 50ms | segment ×80 | 0.003ms | 0.005ms | 0.107ms | 10ms |
| 移位 120px / 预算 50ms | segment ×80 | 0.004ms | 0.005ms | 0.006ms | 20ms |
| 移位 200px（尾部 80px）/ 50ms | segment ×80 | 0.004ms | 0.006ms | 0.056ms | 20ms |
| 边界 20px / 预算 38ms（最短段） | segment ×80 | 0.607ms | 0.792ms | 0.866ms | 8ms |
| 移位 40px / 预算 30ms | straight ×80 | — | — | — | 0 |

- **按下误差 p95 ≤0.8ms、max 1.04ms，全部 <2ms 阈值 → 方案划算**。边界档（预热段只有 30ms、尾部 8ms）最差，仍在 1.04ms。
- **占用增量**：短移 8–20ms 完全从提前量窗口里扣（`sleep_for = delay − tail_s`），down→up 仍是 1 次注入、约 1ms；卡点时刻不变。相对 843ms 提前量，占用增量 0.95–2.37%，且只发生在移位场景。
- **代价边界**：预热段最短 30ms（`SEGMENT_LEG_MIN_MS`）+ 尾部 8–20ms ⇒ 要 `segment` 至少得有 **38–50ms** 的预热预算；低于这个窗口才退回 `straight`。预热点停得越远，收尾越接近速度上限（6000px/s），所以尾部硬性封顶 120px。

### 4. 倒计时路线为什么改成 due 前的 `close_tail`

倒计时的零点估计每秒都可能修订，**不能**像列表路线那样用一个绝对 `_fire_deadline` 对时（估计一变，旧截止时刻会把按下拖后几百毫秒）。因此：

- 预热（无预算）仍然预留 20ms 收尾窗口（`_tail_s`），但**不设截止时刻**；
- `_watch_countdown` 在 `remaining ≤ FIRE_LEAD_S + reserve + 20ms` 时调 `Mouse.close_tail()`，把移位在 due 之前走完；
- 开火点击命中 `_prepared` → `zero`，按下时刻不变；
- 若最后一个 OCR 帧没赶上 `close_tail`，`click` 里**禁止**再补短移（会拖后 8–20ms），直接按 `straight` 记账——宁可有 1 次可统计的瞬移，也不破坏卡点。

### 5. 测试

- `python -m pytest tests/test_feature_mouse_human.py -q` → **48 passed**
- 开火相关 10 个文件 → **142 passed, 2 skipped**
- 全量 `tests` → **1887 passed, 134 skipped**；唯一失败 `test_ocr_price.py::test_local_models_are_preferred_and_cold_start_is_small` 与本路无关（本机 `ocr_rapid` 模型不可用，`Backend.available()==False`）。
- 未 commit、未 push、未动真实光标、未改他人区块。
