# 抢购功能扩展契约（七个分支共用的唯一约定）

> 本文件只定义契约与验收标准，**不包含实现**。
> 七个功能各自独立分支实现；合并时只按本文件的追加约定拼装，不允许重排既有内容。
> 全程禁止：真实桌面操作、可见窗口、移动系统光标、commit、push。

---

## 0. 基线事实（改之前先认账）

当前仓库里这七项**一个都没有**。判定依据：

- `core/switches.py` 的 `SWITCHES` 里没有 `mouse.human.* / mouse.idle.* / scan.repeat.* / buy.skip_late.* / stop.by_grade.* / click.burst.* / buy.followup.*` 任何 key。
- `core/config.py` 的 `DEFAULTS` 里没有 `mouse / buy / stop / click` 这几个配置节（`scan` 也没有子节）。
- `config.json` 里没有对应字段。

已有、**不要重做**的能力（新功能只能复用，不能替换）：

| 能力 | 现有实现位置 | 复用方式 |
|---|---|---|
| 提前量 `FIRE_LEAD_S = 5.0` | `core/engine.py:66`，`countdown.Clock.due(t, lead)` | 漫游归位、来不及判定都以它为准 |
| 反推解锁时刻 | `core/countdown.py:Clock.zero_at()/remaining()/due()` | 来不及跳过、漫游窗口、品阶计数都读它 |
| 最短倒计时 `countdown.min_lead` | `core/switches.py` + `core/engine.py` 的 sweep | 与 `buy.skip_late.*` 叠加，不互相替代 |
| 扫描/刷新节奏 | `list_scan_interval`、`list_max_tries`、`QUEUE_IDLE_REFRESH_S` | 随机刷新只包在倒计时路线的“找这件”阶段 |
| 命中即停 / 总量 / 预算 | `stop_after_hit`、`console.quota`、`console.budget` | 品阶停止是叠加闸门，不改变这三者的语义 |
| 输入通道 | `core/input.py:Mouse.click`，`mode = interception / legacy / message` | 拟人轨迹必须按通道能力降级 |

---

## 1. 命名空间与配置落点

七个命名空间**既是开关 key 前缀，也是配置路径前缀**，三者必须逐字一致：

| # | 功能 | 命名空间 | 配置节（`config.json`） | 前端区块 id | 挂载位置 |
|---|---|---|---|---|---|
| 1 | 拟人鼠标轨迹 | `mouse.human.*` | `mouse.human` | `blk-fire-human` | `#swFire` |
| 2 | 空闲漫游 | `mouse.idle.*` | `mouse.idle` | `blk-fire-idle` | `#swFire` |
| 3 | 每件随机刷新次数 | `scan.repeat.*` | `scan.repeat` | `blk-fire-repeat` | `#swFire` |
| 4 | 来不及跳过 | `buy.skip_late.*` | `buy.skip_late` | `blk-fire-skip-late` | `#swFire` |
| 5 | 按品阶计数停止 | `stop.by_grade.*` | `stop.by_grade` | `blk-fire-grade` | `#swFire` |
| 6 | 连点参数 | `click.burst.*` | `click.burst` | `blk-fire-burst` | `#swFire` |
| 7 | 假购买补刀 | `buy.followup.*` | `buy.followup` | `blk-fire-followup` | `#swFire` |

规则：

- 开关 key = 配置路径（点路径字符串完全相同）。前端只认 key，引擎只认路径。
- 全部归入现有分组 `抢购设置`（`switches.GROUPS` 里已有，**不许新增分组、不许改分组顺序**）。
- 单位后缀强制：毫秒 `_ms`、秒 `_s`、像素 `_px`、比例无后缀（hint 写 `0~1`）。字段名后缀、`hint` 单位、前端显示单位三者必须一致。
- 布尔项没有范围，但 `kind` 必须是 `bool`；其余必须是 `int / float / select`。

### 1.1 完整配置 schema（默认值即最终落盘值）

```json
{
  "mouse": {
    "human": {
      "traj": "auto",
      "move_ms": 120,
      "curve": 0.35
    },
    "idle": {
      "mode": "off",
      "interval_ms": 1200,
      "return_s": 2,
      "min_remain_s": 8,
      "wheel_notches": 3
    }
  },
  "scan": {
    "repeat": {
      "min": 1,
      "max": 3
    }
  },
  "buy": {
    "skip_late": {
      "threshold_s": 2
    },
    "followup": {
      "enabled": false
    }
  },
  "stop": {
    "by_grade": {
      "legend": 0,
      "epic": 0,
      "rare": 0,
      "scope": "all"
    }
  },
  "click": {
    "burst": {
      "mode": "append",
      "duration_ms": 100,
      "gap_ms": 5,
      "window": "all"
    }
  }
}
```

---

## 2. 字段级契约

### 2.1 拟人鼠标轨迹 `mouse.human.*`

| key / path | kind | 默认 | 合法范围 / 枚举 | 非法时 |
|---|---|---|---|---|
| `mouse.human.traj` | select（strict） | `auto` | `auto` / `windmouse` / `bezier` / `pack` | 拒绝：`不在可选值 [auto, windmouse, bezier, pack]` |
| `mouse.human.move_ms` | int（integral） | `120` | 20–800 | 拒绝：非整数 / 小于下限 20 / 大于上限 800 |
| `mouse.human.curve` | float | `0.35` | 0.0–1.0 | 拒绝：非有限数 / 越界 |

语义：

- `auto`：非开火移动走 WindMouse；开火连点走“短曲线 + 2–3 个采样点”，不做长路径。
- `windmouse`：WindMouse 重力/风力模型，带加速—减速曲线。
- `bezier`：三次贝塞尔，控制点随机偏移，`curve` 控制偏移幅度。
- `pack`：读 `data/traj/*.json` 真人轨迹包；按实际起止点缩放、按 `move_ms` 重采样。
- 开火阶段的路径耗时硬上限：`min(move_ms, 0.25 × 当前点击间隔, 30ms)`，绝不允许拟人轨迹把连点频率拖慢到设置值以下。
- 降级（必须显式，不许假装生效）：
  - `input_mode = message`：该通道不移动真实光标，`effective = "degraded"`，日志与 `engine.status()["stats"]["trajectory"]` 都要写明“纯消息通道无真实移动，拟人轨迹不生效”。
  - 驱动不可用并已降级到 `legacy`：轨迹仍然可走真实光标，但每次点击耗时上升，状态里标 `effective = "legacy"`。
  - `pack` 但轨迹包目录为空/格式坏：自动降为 `auto`，日志一条 `warn`，状态 `effective = "pack_fallback_auto"`，**不得静默当 pack 用**。

轨迹包格式（新增目录 `data/traj/`）：

```json
{
  "name": "sample-01",
  "points": [[0, 0, 0], [3, 5, 18], [12, 14, 42], [40, 30, 120]],
  "meta": {"screen": [1920, 1080], "source": "manual"}
}
```

要求：至少 8 个点；首点 `[0,0,0]`；`dt` 单调递增且总时长 > 0；最后一点即终点偏移。格式不合格 → 整包忽略并告警，不半用。

### 2.2 空闲漫游 `mouse.idle.*`

| key / path | kind | 默认 | 合法范围 / 枚举 | 非法时 |
|---|---|---|---|---|
| `mouse.idle.mode` | select（strict） | `off` | `off` / `move` / `move_wheel` | 拒绝：`不在可选值 [off, move, move_wheel]` |
| `mouse.idle.interval_ms` | int（integral） | `1200` | 300–10000 | 拒绝：越界 / 非整数 |
| `mouse.idle.return_s` | int（integral） | `2` | 1–30 | 拒绝：越界 / 非整数 |
| `mouse.idle.min_remain_s` | int（integral） | `8` | 3–3600 | 拒绝：越界 / 非整数 |
| `mouse.idle.wheel_notches` | int（integral） | `3` | 1–10 | 拒绝：越界 / 非整数 |

生效闸门（必须同时满足，缺一不漫游）：

1. `mode != off`；
2. 当前是 `workflow = countdown` 且 `Clock.zero_at()` 有值；
3. `left = Clock.remaining(now)`，且 `left > max(min_remain_s, FIRE_LEAD_S + return_s + 1.0)`；
4. 未进入开点窗口：一旦 `left <= FIRE_LEAD_S + return_s`，**停止漫游并归位**，之后不再移动。

硬约束：

- 漫游只允许 `move`（移动）与 `move_wheel`（移动 + 滚轮），**绝不点击**。
- 所有落点必须钳制在游戏客户区矩形内（内缩 8 px）；客户区读不到就本轮不漫游。
- 每次漫游前校验前台；前台丢失时不发事件，直接退出漫游。
- 归位目标：漫游开始前的光标点（若在客户区内），否则客户区中心点；归位后记 `settled_at`。
- 非倒计时路线（`list` / `detail`）：完全不生效，启动时一条 `info` 说明“该路线没有可反推的解锁时刻，空闲漫游不启用”。

### 2.3 每件随机刷新次数 `scan.repeat.*`

| key / path | kind | 默认 | 合法范围 | 非法时 |
|---|---|---|---|---|
| `scan.repeat.min` | int（integral） | `1` | 0–10 | 拒绝：越界 / 非整数 |
| `scan.repeat.max` | int（integral） | `3` | 0–10 | 拒绝：越界 / 非整数 |

跨字段：

- `min <= max`，否则整批拒绝，原因 `scan.repeat：最少 4 大于最多 3`。
- `0 / 0` = 完全不刷新，合法。
- 单字段保存时也要与盘上另一字段比较，不允许先写 `min=4` 再写 `max=3` 把配置写成非法态。

行为：

- 每“件”（倒计时路线的每次 `_countdown_prepare` 目标）独立取 `randint(min, max)`，不复用同一轮的随机值。
- 刷新只发生在“找这件”阶段：挑不到候选时按预算刷新，预算用尽即退出，不无限刷。
- 实际使用次数必须记录：
  - 记录行 `extra.refresh_budget`、`extra.refresh_used`；
  - `engine.status()["stats"]["refresh_counts"]` 保留最近 30 条 `{item, budget, used}`。
- 列表路线按“页”刷新，不按件；列表分支只需保证不把 `scan.repeat.*` 误当成页刷新预算，并在状态里注明 `route=list` 时该设置不生效。

### 2.4 来不及跳过 `buy.skip_late.*`

| key / path | kind | 默认 | 合法范围 | 非法时 |
|---|---|---|---|---|
| `buy.skip_late.threshold_s` | int（integral） | `2` | 0–3600 | 拒绝：越界 / 非整数；0 = 关闭 |

判定：

- “首次被读到”= 该候选第一次拿到可用时间读数的那一帧。
- 优先用反推：`Clock.remaining(now)`；不可用时退回显示值 `sec`（显示值因 `DISPLAY_TRUNC_S = 1.0` 只是下界）。
- 若 `0 < remaining < threshold_s` → 直接跳过该件：不点购买、不计入失败次数、记录 `skip_reason = "late"`、`skip_source = "inferred" / "display"`。
- `remaining <= 0`（已解锁）不跳过；显示 `0秒` 也不跳过。
- 与 `countdown.min_lead` 的关系：sweep 阶段继续用 `min_lead` 拦“显示秒数太短”；首次反推读数再叠 `threshold_s` 这道闸。两者取更严格的那个，不合并成一个字段。

### 2.5 按品阶计数停止 `stop.by_grade.*`

| key / path | kind | 默认 | 合法范围 / 枚举 | 非法时 |
|---|---|---|---|---|
| `stop.by_grade.legend` | int（integral） | `0` | 0–99；0 = 不限 | 拒绝：越界 / 非整数 |
| `stop.by_grade.epic` | int（integral） | `0` | 0–99；0 = 不限 | 拒绝：越界 / 非整数 |
| `stop.by_grade.rare` | int（integral） | `0` | 0–99；0 = 不限 | 拒绝：越界 / 非整数 |
| `stop.by_grade.scope` | select（strict） | `all` | `all` / `grade` | 拒绝：`不在可选值 [all, grade]` |

映射（界面文案 / 内部品阶）：

- `legend` = 传说；
- `epic` = 紫（游戏目录里的“史诗”）；
- `rare` = 蓝（游戏目录里的“稀有”）；
- 普通不参与计数，也不设目标。

行为：

- 每次尝试前检查当前件的品阶：已达目标且 `scope = all` → 整轮停止（`Aborted`，原因写明“X 品阶已达 N 件”）；
- `scope = grade` → 只跳过该品阶，继续找其他品阶；
- 计数**只加在“校验成交”之后**，未成交/未校验不计数；
- 品阶读不出（例如未点名皮肤、列表卡片不写品阶）：该件不参与闸门、不计数，日志一条 `info`，不许猜品阶；
- 与 `console.quota`、`console.budget`、`stop_after_hit` 并存：三者照旧生效，品阶闸门只是额外一层。

### 2.6 连点参数 `click.burst.*`

| key / path | kind | 默认 | 合法范围 / 枚举 | 非法时 |
|---|---|---|---|---|
| `click.burst.mode` | select（strict） | `append` | `append` / `burst` | 拒绝：`不在可选值 [append, burst]` |
| `click.burst.duration_ms` | int（integral） | `100` | 20–1000 | 拒绝：越界 / 非整数 |
| `click.burst.gap_ms` | int（integral） | `5` | 1–50 | 拒绝：越界 / 非整数 |
| `click.burst.window` | select（strict） | `all` | `all` / `1s` / `2s` | 拒绝：`不在可选值 [all, 1s, 2s]` |

语义（必须原样写进 `desc`）：

- `append`：保持现有节奏（`click_hz` + 弹窗出现即停），本组参数不改变开火频率。
- `burst`：从 `t_fire` 起，在触发窗口内用 `gap_ms` 作为点击间隔，持续 `duration_ms`；`duration_ms` 用尽或弹窗出现立即回到 `append` 节奏并停止本次连点。
- `window`：`all` = 整个开火期；`1s` / `2s` = 只在开点后 1/2 秒内连点，超出后回归 `click_hz`。
- 绝不点击确认按钮、绝不越过 `fire_timeout_s`、绝不把点击落在弹窗出现之后的界面上。
- **抽签模型说明（界面必须展示这句）**：“连点只缩短再次进池的延迟，不提高单次中签概率。”

### 2.7 假购买补刀 `buy.followup.*`

| key / path | kind | 默认 | 合法范围 | 非法时 |
|---|---|---|---|---|
| `buy.followup.enabled` | bool（strict） | `false` | `true/false`（接受 `1/0/on/off/是/否`） | 拒绝：`不是布尔值` |

行为：

- 只在“一次开火尝试结束且没等到确认弹窗”后执行，最多 1 次补刀点击（同一购买点），补刀后再读一次确认弹窗；仍没有就按 miss 记。
- **生效条件**：`enabled = true` **且** `click.burst.mode = "append"` **且** 存在 `confirm` 区域（无弹窗版式不补刀，避免多点买成两件）。
- 与连点互斥：`click.burst.mode = "burst"` 时补刀不生效，日志一条 `info`“连点开启，假购买补刀本次不生效”；不拒绝保存、不篡改配置值。
- 半自动模式永不补刀。
- 记录 `extra.followup_used = true/false`；`stats.followup = {enabled, effective, used}`。
- `desc` 必须注明：“默认关；与连点互斥，连点开启时不生效；无确认弹窗的版式不补刀。”

---

## 3. 前端区块与控件绑定

### 3.1 挂载与渲染

- 物理挂载点只有一个：既有 `#swFire`（“抢购设置”标签页，`web/index.html:1331`）。
- 现有渲染器 `renderSettingsSwitches()` 按 `S.switches.groups` 生成行；行内控件统一带：
  - `data-sw="<switch key>"`
  - `data-kind="bool|int|float|select"`
- 绑定规则：`data-sw` 必须逐字等于 `SWITCHES` 的 key；`data-kind` 必须等于 `kind`。前端不额外写业务字段、不做单位换算、不在客户端钳制数值。
- 保存链路固定：控件 `change` → `POST /api/switches {values:{<key>: <value>}}` → `switches.apply_all`（全量校验、全有或全无）→ `Config.transaction()` 落盘 → 返回 `groups` 重新渲染。
- 读链路固定：`GET /api/switches` 返回 `groups`（`switches.snapshot`），不新增路由。

### 3.2 区块 id（DOM 契约）

七个区块必须可在 DOM 里定位，且不改变既有行内容：

| 区块 id | `data-feature` | 选择器（验收用） |
|---|---|---|
| `blk-fire-human` | `mouse.human` | `#swFire #blk-fire-human [data-sw^="mouse.human."]` |
| `blk-fire-idle` | `mouse.idle` | `#swFire #blk-fire-idle [data-sw^="mouse.idle."]` |
| `blk-fire-repeat` | `scan.repeat` | `#swFire #blk-fire-repeat [data-sw^="scan.repeat."]` |
| `blk-fire-skip-late` | `buy.skip_late` | `#swFire #blk-fire-skip-late [data-sw^="buy.skip_late."]` |
| `blk-fire-grade` | `stop.by_grade` | `#swFire #blk-fire-grade [data-sw^="stop.by_grade."]` |
| `blk-fire-burst` | `click.burst` | `#swFire #blk-fire-burst [data-sw^="click.burst."]` |
| `blk-fire-followup` | `buy.followup` | `#swFire #blk-fire-followup [data-sw^="buy.followup."]` |

实现方式（append-only）：在 `web/index.html` 末尾追加一个共享脚本区，用 `MutationObserver` 监听 `#swFire`，在每次重渲染后按 key 前缀把 `.setrow` 归入对应 `<section id="blk-fire-…" data-feature="…">`。脚本只加分组容器，不复制、不改写控件。该区域由合并负责人一次写入，七个分支**不许各写一份**。

- 每个区块必须有且仅有一次“说明文案”：放在 `desc`，渲染器已经会显示在名称下方；禁止另起 HTML 说明块。
- 例外：`click.burst` 的“抽签语义”与 `buy.followup` 的“互斥”必须**同时**出现在 `desc` 字符串里。

---

## 4. 共享文件的追加约定

只许追加，不许重排、不许改名、不许删除既有条目。所有插入区必须有起止注释，且同一命名空间只出现一次。

### 4.1 `core/config.py`

在 `DEFAULTS` 字典末尾、既有最后一个键之后追加：

```python
    # ==== FEATURE-CONFIG:BEGIN ====
    # 每个分支只在自己的 <ns> 起止块内追加，合并时按块拼接
    # ---- feature:mouse.human BEGIN ----
    # ---- feature:mouse.human END ----
    # ...其余六个同理...
    # ==== FEATURE-CONFIG:END ====
```

- 默认值必须与 `SWITCHES` 的 `default` 逐字一致（`tests/test_switches.py::test_every_default_matches_config_defaults` 会拦）。
- 嵌套节必须与第 1.1 节 schema 完全一致；`Config._merge` 是深合并，缺子键不会报错，但契约要求写全。

### 4.2 `core/switches.py`

三处追加，均带起止标记：

1. `SWITCHES` 列表末尾、`fire.verify` 条目之后、`# ===== 自动与停止 =====` 之前：

```python
    # ==== FEATURE-SWITCHES:mouse.human BEGIN ====
    # ==== FEATURE-SWITCHES:mouse.human END ====
    # ...其余六个同理...
```

2. `BOUNDS` 字典末尾追加所有新数值项（`tests/test_numeric_hardening.py::test_numeric_bounds_cover_every_numeric_switch` 会拦）。
3. 新增 `strict` / `integral` 能力的实现只加在 `_coerce/_number` 的分支里，不改既有分支行为：
   - `s.get("strict")`：select 非法值抛 `SwitchValueError(key, "不在可选值 [...]")`，不再回落默认。
   - `s.get("integral")`：int 字段收到小数抛 `SwitchValueError(key, "必须是整数：...")`，不再向零截断。
   - 跨字段校验放 `apply_all` 与 `set`：先算出“候选最终值”（盘上现值 + 本次改动），再验证 `min <= max`。

### 4.3 API 路由

- 不新增路由。`GET/POST /api/switches` 已覆盖读写。
- 新增状态只追加到 `engine.status()`：
  - `stats.trajectory`、`stats.roam`、`stats.refresh_counts`、`stats.late_skips`、`stats.grade_counts`、`stats.burst`、`stats.followup`。
- 新增复盘字段只追加到 `records.add(extra={...})`，不改既有字段名。

### 4.4 `web/index.html`

- 默认零改动：控件由 `SWITCHES` 自动生成。
- 唯一允许的追加：文件末尾 `<!-- FEATURE-BLOCKS BEGIN --> … <!-- FEATURE-BLOCKS END -->`（共享 DOM 分组脚本，合并时写一次）。
- 不许改 `renderSettingsSwitches` 的 `map`、不许改 `numHtml/selectHtml/switchHtml`、不许改事件委托。

### 4.5 引擎 `core/engine.py`（允许改，但限定插入点）

新增代码只允许落在以下既有函数的**扩展点**，不许重排既有语句：

| 扩展点 | 允许做什么 |
|---|---|
| `_run` 构造 `Mouse` 之后 | 初始化轨迹/漫游对象，打印降级说明 |
| `_countdown_prepare` 的 `sweep()` | 随机刷新预算、`skip_late` 首读判定、记录 used |
| `_watch_countdown` 循环内 | 每帧喂漫游 tick；归位闸门 |
| `_fire_and_confirm` 开火循环 | burst 节奏切换；miss 后的 followup |
| `_fire_and_confirm` 成交校验之后 | 品阶计数 + 下一件闸门 |
| `_record` 的 `extra` | 追加本功能的复盘子字段 |
| `status()` 的 `stats` | 追加本功能状态块 |

输入层 `core/input.py` 只允许追加：`Mouse.click(..., path=None)` 的新关键字参数、`Mouse.move_path(points, total_ms)`、`trajectory_capability()`；既有 `click` 行为在不传 `path` 时必须与现在逐字节一致。

---

## 5. 七条互斥 / 依赖关系（必须逐条有测试）

1. **连点 ↔ 假购买补刀**：`click.burst.mode = "burst"` 时 `buy.followup` 不产生任何额外点击；`append` 时才可能补刀。允许同时保存，不拒绝、不偷偷改值。
2. **拟人轨迹 ↔ 输入通道 / 轨迹包**：只有 `interception`、`legacy` 有真实光标移动；`message` 一律降级并显式说明；`pack` 不可用降级 `auto` 并告警。任何降级都必须在 `stats.trajectory.effective` 里有值。
3. **空闲漫游 ↔ 开点窗口**：`left <= FIRE_LEAD_S + return_s` 必须已归位且不再发移动/滚轮；漫游期间零点击；落点必须在客户区内。
4. **空闲漫游 ↔ 倒计时模型**：只有 `workflow = countdown` 且 `zero_at` 有值才生效；`list/detail` 完全不生效并有说明。
5. **随机刷新 ↔ 逐件跳过**：先给这件抽样刷新预算，再在首次读数上做 `skip_late` 判定；刷新次数用尽不等于“来不及”，两者在记录里分开（`refresh_used` vs `skip_reason`）。
6. **来不及跳过 ↔ 最短倒计时**：`min_lead` 用显示秒（下界），`skip_late` 用反推剩余；两者都命中就取更严格的结论，记录里标 `skip_source`。
7. **品阶停止 ↔ 总量/预算/抢到就停**：品阶闸门不绕过 quota/budget；已达目标按 `scope` 决定整轮停或只跳该品阶；品阶未知不计数、不拦；`stop_after_hit` 仍在成交后优先触发。

---

## 6. 抽签模型下的语义变化（不许照搬竞品原样）

| 功能 | 旧模型（读价 + 抢按钮）语义 | 抽签模型下必须怎么解释 | 结果 |
|---|---|---|---|
| 连点 | 多点几次提高“买到”的概率 | 每次点击只是重新进池；连点只缩短“再次进池”的等待，不提高单次中签率 | 保留参数，文案改写，默认 `append` |
| 假购买补刀 | 多点一次制造“有人要买”的假象或刷新库存 | 抽签模型没有库存可刷；补刀只可能多触发一次进池，收益不确定且可能双买 | 默认关，且与连点互斥 |
| 每件随机刷新 | 刷新列表能刷出更多货 | 刷新是重新抽样/重新排队，不是“同一件多一次机会” | 按件独立预算，记录实际次数，便于复盘 |
| 来不及跳过 | 按界面倒计时秒数判断 | 显示秒被 `DISPLAY_TRUNC_S=1.0` 截断，“0 秒”仍可能剩 0.99 秒；必须用反推 `zero_at` | 首读用 `remaining()`，回退显示值时标 `display` |
| 品阶停止 | 抢到就算一件 | 抽签结果只有“中/不中”，计数必须只在成交校验通过后 +1 | 未校验不计数 |
| 拟人轨迹 | 降低被检测的机械特征 | 轨迹只影响注入观感，不影响中签；开火期不能拖慢节奏 | 开火路径限时，超限自动缩短 |
| 空闲漫游 | 挂机时保持“活人”状态 | 纯观感，零收益；必须给开点让路 | 开点前 `return_s` 强制归位 |

---

## 7. 验收标准（五维，逐功能可执行）

统一前提：所有测试离线；用 `tests/` 里的 Fake `Mouse/Screen`；不构造真实 `Mouse`、不开窗口、不动光标。命令统一为 `uv run pytest <file> -q`。

### D1 功能可用

| 功能 | 判据（可观察） | 测试文件 |
|---|---|---|
| mouse.human | 路径点数 > 2、点间不共线、总耗时落在 `move_ms ± 30%`；`pack` 能加载合格包；开火路径 ≤ 上限 | `tests/test_feature_mouse_human.py` |
| mouse.idle | 一次漫游产生 ≥1 次移动；`move_wheel` 产生滚轮；`off` 零事件 | `tests/test_feature_mouse_idle.py` |
| scan.repeat | 同一轮多件各自预算在 `[min,max]`；`0/0` 零刷新 | `tests/test_feature_scan_repeat.py` |
| buy.skip_late | 剩余 1.5s → 跳过且未点购买；剩余 3s → 正常进入 | `tests/test_feature_skip_late.py` |
| stop.by_grade | 传说目标 1，成交 1 件后 `scope=all` 整轮停；`scope=grade` 只跳传说继续 | `tests/test_feature_grade_stop.py` |
| click.burst | `gap_ms=5` 时点击间隔均值 ≤ 12ms；`duration_ms` 后回到 `click_hz`；弹窗出现后零点击 | `tests/test_feature_click_burst.py` |
| buy.followup | 默认关零补刀；开启 + append + miss → 恰好 1 次补刀；burst 开启 → 0 次 | `tests/test_feature_followup.py` |

### D2 前后端联通

每个功能三条断言，缺一不可：

1. `GET /api/switches` 的 `groups` 里存在该 key，`kind/default/options/hint` 与契约一致。
2. `POST /api/switches {values:{key: new}}` 返回 `applied[key] == new`，且 `Config.load()` 读回同值（真落盘）。
3. 引擎用落盘后的配置跑对应分支，行为按 D1 改变；不许出现“控件能改、引擎不读”。

测试方式：`switches.apply_all` + `Config.transaction` 单元验证；引擎侧用 `cfg.data` 直接注入同一值。合并后再补一条端到端：启动 `core.server` 的测试实例（不打开浏览器）、POST、GET、断言快照。

### D3 异常输入

- 数字项：`"abc"` → `SwitchValueError`，消息含 key 与“不是数字”；`None` → 同样拒绝。
- `NaN` / `Infinity` → 消息含“有限数字”。
- int 项收到 `1.5` → 消息含“必须是整数”。
- select 项收到 `"bogus"` → 消息含“不在可选值”。
- bool 项收到 `"maybe"` / `2` → 消息含“不是布尔值”。
- 批量里混一个坏值 → `apply_all` 整批拒绝，其他合法字段**一个都不落盘**。

### D4 越界拒收

- 每个数值项各测“低于下限 / 高于上限”两条，消息含字段名与具体边界。
- 跨字段：`scan.repeat.min > max` → 拒绝，消息含两个值。
- 拒绝后：`config.json` 字节不变（比较写入前后文件内容或 `Config._dirty` 不含该 key），界面拿到 `ok:false` + 可读 `msg`。
- 不许出现“钳到边界值再保存”或“静默回默认”。

### D5 互斥 / 共存

按第 5 节七条逐条建用例：

1. burst + followup：`followup_used == 0`。
2. message + human：`trajectory.effective == "degraded"`，且无路径事件。
3. idle 在 `left = FIRE_LEAD_S + return_s - 0.5`：已归位，后续零事件。
4. idle 在 `workflow=list`：零事件 + 一条说明。
5. repeat 预算耗尽 + `skip_late` 命中：记录里 `refresh_used` 与 `skip_reason` 同时存在且不混淆。
6. `min_lead=6`、`skip_late=2`、剩余显示 5s：sweep 阶段即跳过（`min_lead` 生效）。
7. 品阶达标 + `quota=3`：`scope=all` 整轮停，`scope=grade` 继续但不再买该品阶；预算/总量仍照常拦。

### 合并门禁（七条都过才允许合并）

1. 七个分支各自 `tests/test_feature_*.py` 全绿。
2. 全量 `uv run pytest -q` 全绿，含既有 `test_switches.py`、`test_numeric_hardening.py`、`test_engine_countdown_flow.py`、`test_engine_list_queue.py`、`test_click_speed.py`。
3. `SWITCHES` 自洽：key 唯一、分组连续、默认值与 `DEFAULTS` 一致、每个数值项在 `BOUNDS`、每个 key 在 `core/` 有真实消费点。
4. 前端快照：七个 `data-sw` 前缀各有控件，且 `#blk-fire-*` 七个区块都能定位；每个控件改一次能落盘。
5. 回归：无真实输入、无可见窗口、无光标移动；`git status` 只包含预期文件。
6. 不 commit、不 push。

---

## 8. 分支交付物清单（每个分支只交这些）

1. 本功能块：`core/config.py` 的 feature 块 + `core/switches.py` 的 feature 块与 BOUNDS。
2. 引擎接线：只动第 4.5 节列出的扩展点；`input.py` 只做追加式参数。
3. 一个测试文件 `tests/test_feature_<ns>.py`，覆盖 D1–D5 中与本功能相关的条目。
4. 一份不超过 20 行的分支说明：改动的函数/插入点、状态字段、已知降级。
5. **不碰**其它六个命名空间的块、`web/index.html` 的共享脚本区、`renderSettingsSwitches`。
