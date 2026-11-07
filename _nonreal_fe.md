# 第一路：前端与开关联通的全量无头验证（`_nonreal_fe.md`）

- 仓库：`C:\Users\26346\Desktop\project\dafors`
- 时间：2026-11-07；Playwright 1.64.1 + 无头 Chromium（`ms-playwright/chromium-1243`），`headless=True`，无可见窗口
- 服务：进程内 `core.server`，监听 `http://127.0.0.1:50961`；`CONFIG_PATH` 指向临时副本 `C:\Users\26346\AppData\Local\Temp\fe_nonreal_5l6agsxn\config.json`
- 隔离：未启动游戏、未移动真实鼠标/键盘、未开窗口；仓库 `config.json` sha256 前后一致（`ed9869fc6a4f722f`…），未 commit / 未 push
- 判定口径：**界面控件 → `POST /api/switches` → 临时 `config.json` → 重载回显**；行为层用静态消费点（引擎实际读取处）核对

## 结论

| 指标 | 结果 |
| --- | --- |
| 检查项合计 | 80 |
| 通过 / 失败 | 80 / 0 |
| 开关逐项（界面/类型/初值/POST/磁盘/重载） | 67 项全通过 |
| 七区块顺序/归属/重绘幂等 | 通过 |
| 假按钮（界面有、后端不动） | 0 个 |
| 占位控件（“待接入/敬请期待”等） | 0 处 |
| 控制台 error / pageerror / HTTP≥400 | 0 / 0 / 0 |

**总判定：全部通过**

检查项构成：67 个开关逐项 + 7 个功能区块 + 6 个汇总项（互斥提示、时间条、两种分辨率布局、占位扫描、控制台），合计 80。

## 一、测试面

| 面 | 数量 | 说明 |
| --- | --- | --- |
| 开关键 | 67（常规 46 + 七路 21） | `SWITCHES` 全量，含下拉/布尔/整数/小数 |
| 功能区块 | 7 | `blk-fire-human/idle/repeat/skip-late/grade/burst/followup` |
| 设置分栏 | 7 | general、hotkey、vision、fire、lab、console、about |
| 页面 | 6 | run、lib、rec、ana、journal、set |
| 分辨率 | 2 | 1366×768、1920×1080 |
| 可见按钮扫描 | 1013 | 无 `data-*` 处理器且无绑定 id 的按钮 0 个 |

## 二、开关逐项总表

列说明：初值=界面值（与 `/api/switches` 快照逐一比对）；改后=通过真实控件操作写入的值；磁盘=临时 `config.json` 读回；重载=整页重载后控件回显；消费点=引擎/导航里首个按该键或该 path 读取的位置。下表 67 项的 `initial_ui` 与 `initial_snap` 全部相等，`type_ok` / `post_ok` / `disk_ok` / `reload_ok` 全为 true，原始字段见 `_nonreal_fe_raw.json`。

### 2.1 常规开关（46）

| 键 | 分组 | 类型 | 初值 | 改后 | 磁盘 | 重载 | 消费点 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `run.semi` | 运行模式 | 布尔 | 关 | 开 | 开 | 开 | `core/engine.py:2313` | 通过 |
| `run.workflow` | 运行模式 | 下拉(3选/3中) | countdown | list | list | list | `core/engine.py:254` | 通过 |
| `follow.before_buy` | 收藏设置 | 布尔 | 关 | 开 | 开 | 开 | `core/engine.py:1214` | 通过 |
| `follow.after_hit` | 收藏设置 | 布尔 | 关 | 开 | 开 | 开 | `core/engine.py:2204` | 通过 |
| `follow.retry` | 收藏设置 | 整数 | 2 | 1 | 1 | 1 | `core/engine.py:2705` | 通过 |
| `follow.wait_ms` | 收藏设置 | 整数 | 450 | 5000 | 5000 | 5000 | `core/engine.py:2706` | 通过 |
| `follow.skip_done` | 收藏设置 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:2707` | 通过 |
| `price.min` | 价格与筛选 | 整数 | 0 | 49999999 | 49999999 | 49999999 | `core/engine.py:861` | 通过 |
| `price.max` | 价格与筛选 | 整数 | 0 | 49999999 | 49999999 | 49999999 | `core/engine.py:861` | 通过 |
| `price.quota` | 价格与筛选 | 整数 | 1 | 50 | 50 | 50 | `core/engine.py:862` | 通过 |
| `price.budget` | 价格与筛选 | 整数 | 0 | 499999999 | 499999999 | 499999999 | `core/engine.py:862` | 通过 |
| `filter.craft` | 价格与筛选 | 下拉(3选/3中) |  | 极品 | 极品 | 极品 | `core/scheduler.py:1140` | 通过 |
| `filter.wear_max` | 价格与筛选 | 小数 | 0 | 5.0 | 5.0 | 5 | `core/scheduler.py:1139` | 通过 |
| `filter.sort` | 价格与筛选 | 下拉(3选/3中) | price | wear | wear | wear | `core/scheduler.py:1143` | 通过 |
| `countdown.min_lead` | 价格与筛选 | 整数 | 6 | 1800 | 1800 | 1800 | `core/engine.py:1314` | 通过 |
| `filter.pub` | 价格与筛选 | 下拉(3选/3中) | 只显示公示期 | 不限公示期 | 不限公示期 | 不限公示期 | `core/engine.py:1262` | 通过 |
| `filter.ons_sort` | 价格与筛选 | 下拉(5选/5中) | 默认排序 | 按价格升序 | 按价格升序 | 按价格升序 | `core/engine.py:1296` | 通过 |
| `fire.hz` | 抢购设置 | 整数 | 50 | 100 | 100 | 100 | `core/engine.py:2329` | 通过 |
| `fire.threshold` | 抢购设置 | 小数 | 0.85 | 0.5 | 0.5 | 0.5 | `core/engine.py:878` | 通过 |
| `fire.timeout` | 抢购设置 | 小数 | 8 | 300.05 | 300.05 | 300.05 | `core/engine.py:2330` | 通过 |
| `fire.confirm_clicks` | 抢购设置 | 整数 | 20 | 500 | 500 | 500 | `core/engine.py:2331` | 通过 |
| `fire.confirm_gap` | 抢购设置 | 整数 | 10 | 500 | 500 | 500 | `core/engine.py:2332` | 通过 |
| `fire.confirm_timeout` | 抢购设置 | 小数 | 2 | 300.025 | 300.025 | 300.025 | `core/engine.py:2594` | 通过 |
| `fire.delay` | 抢购设置 | 整数 | 0 | 2500 | 2500 | 2500 | `core/autotune.py:72` | 通过 |
| `fire.jitter` | 抢购设置 | 整数 | 0 | 2500 | 2500 | 2500 | `core/engine.py:2344` | 通过 |
| `fire.adaptive` | 抢购设置 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:2341` | 通过 |
| `fire.verify` | 抢购设置 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:2635` | 通过 |
| `scan.interval` | 自动与停止 | 小数 | 0.05 | 30.0005 | 30.0005 | 30.0005 | `core/autotune.py:80` | 通过 |
| `scan.timeout` | 自动与停止 | 小数 | 0 | 43200.0 | 43200.0 | 43200 | `core/engine.py:1921` | 通过 |
| `scan.max_tries` | 自动与停止 | 整数 | 20 | 500 | 500 | 500 | `core/engine.py:1923` | 通过 |
| `scan.detail_timeout` | 自动与停止 | 小数 | 3 | 300.025 | 300.025 | 300.025 | `core/engine.py:1922` | 通过 |
| `auto.retry` | 自动与停止 | 整数 | 3 | 50 | 50 | 50 | `core/engine.py:1000` | 通过 |
| `stop.after_hit` | 自动与停止 | 布尔 | 关 | 开 | 开 | 开 | `core/engine.py:770` | 通过 |
| `stop.on_fail` | 自动与停止 | 整数 | 0 | 500 | 500 | 500 | `core/engine.py:2227` | 通过 |
| `stop.hotkey` | 自动与停止 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:409` | 通过 |
| `start.hotkey` | 自动与停止 | 布尔 | 开 | 关 | 关 | 关 | `core/server.py:1152` | 通过 |
| `premarket.enabled` | 截前 | 布尔 | 关 | 开 | 开 | 开 | `core/server.py:1326` | 通过 |
| `premarket.hotkey` | 截前 | 下拉(3选/0中) | F10 | F9 | F9 | F9 | `core/server.py:1142` | 通过 |
| `env.foreground` | 环境与安全 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:2406` | 通过 |
| `env.preflight` | 环境与安全 | 布尔 | 开 | 关 | 关 | 关 | `core/server.py:1249` | 通过 |
| `env.save_shots` | 环境与安全 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:3219` | 通过 |
| `env.record` | 环境与安全 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:2040` | 通过 |
| `env.failsafe` | 环境与安全 | 布尔 | 开 | 关 | 关 | 关 | `core/engine.py:887` | 通过 |
| `env.input_mode` | 环境与安全 | 下拉(3选/3中) | interception | legacy | legacy | legacy | `core/actions.py:52` | 通过 |
| `env.input_fallback` | 环境与安全 | 下拉(3选/3中) | legacy | message | message | message | `core/input.py:175` | 通过 |
| `env.input_message_strict` | 环境与安全 | 布尔 | 开 | 关 | 关 | 关 | `core/input.py:335` | 通过 |

### 2.2 七路新增（21）

| 键 | 分组 | 类型 | 初值 | 改后 | 磁盘 | 重载 | 消费点 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `stop.by_grade.enabled` | 抢购设置 | 布尔 | 关 | 开 | 开 | 开 | `core/stop_grade.py:28` | 通过 |
| `stop.by_grade.legend` | 抢购设置 | 整数 | 0 | 49 | 49 | 49 | `core/stop_grade.py:29` | 通过 |
| `stop.by_grade.epic` | 抢购设置 | 整数 | 0 | 49 | 49 | 49 | `core/stop_grade.py:30` | 通过 |
| `stop.by_grade.rare` | 抢购设置 | 整数 | 0 | 49 | 49 | 49 | `core/stop_grade.py:31` | 通过 |
| `stop.by_grade.scope` | 抢购设置 | 下拉(2选/2中) | all | grade | grade | grade | `core/stop_grade.py:32` | 通过 |
| `scan.repeat.min` | 抢购设置 | 整数 | 1 | 5 | 5 | 5 | `core/autotune.py:88` | 通过 |
| `scan.repeat.max` | 抢购设置 | 整数 | 1 | 5 | 5 | 5 | `core/autotune.py:84` | 通过 |
| `buy.followup.enabled` | 抢购设置 | 布尔 | 关 | 开 | 开 | 开 | `core/engine.py:2510` | 通过 |
| `click.burst.mode` | 抢购设置 | 下拉(2选/2中) | append | burst | burst | burst | `core/server.py:949` | 通过 |
| `click.burst.hold_ms` | 抢购设置 | 整数 | 100 | 510 | 510 | 510 | `core/click_burst.py:47` | 通过 |
| `click.burst.interval_ms` | 抢购设置 | 整数 | 5 | 25 | 25 | 25 | `core/click_burst.py:48` | 通过 |
| `click.burst.window` | 抢购设置 | 下拉(3选/3中) | all | 1s | 1s | 1s | `core/click_burst.py:49` | 通过 |
| `buy.skip_late.threshold_s` | 抢购设置 | 整数 | 2 | 1800 | 1800 | 1800 | `core/autotune.py:76` | 通过 |
| `mouse.human.traj` | 抢购设置 | 下拉(4选/4中) | auto | windmouse | windmouse | windmouse | `core/humanmouse.py:87` | 通过 |
| `mouse.human.move_ms` | 抢购设置 | 整数 | 120 | 410 | 410 | 410 | `core/humanmouse.py:95` | 通过 |
| `mouse.human.curve` | 抢购设置 | 小数 | 0.35 | 0.5 | 0.5 | 0.5 | `core/humanmouse.py:106` | 通过 |
| `mouse.idle.mode` | 抢购设置 | 下拉(3选/3中) | off | move | move | move | `core/idle_roam.py:32` | 通过 |
| `mouse.idle.interval_ms` | 抢购设置 | 整数 | 1200 | 5150 | 5150 | 5150 | `core/idle_roam.py:33` | 通过 |
| `mouse.idle.return_s` | 抢购设置 | 整数 | 2 | 15 | 15 | 15 | `core/idle_roam.py:34` | 通过 |
| `mouse.idle.min_remain_s` | 抢购设置 | 整数 | 8 | 1801 | 1801 | 1801 | `core/idle_roam.py:35` | 通过 |
| `mouse.idle.wheel_notches` | 抢购设置 | 整数 | 3 | 5 | 5 | 5 | `core/idle_roam.py:36` | 通过 |

### 2.3 下拉选项完整性

| 键 | 选项数 | 含中文 | 判定 |
| --- | --- | --- | --- |
| `run.workflow` | 3 | 3 | 通过 |
| `filter.craft` | 3 | 3 | 通过 |
| `filter.sort` | 3 | 3 | 通过 |
| `filter.pub` | 3 | 3 | 通过 |
| `filter.ons_sort` | 5 | 5 | 通过 |
| `stop.by_grade.scope` | 2 | 2 | 通过 |
| `click.burst.mode` | 2 | 2 | 通过 |
| `click.burst.window` | 3 | 3 | 通过 |
| `mouse.human.traj` | 4 | 4 | 通过 |
| `mouse.idle.mode` | 3 | 3 | 通过 |
| `premarket.hotkey` | 3 | 0 | 通过（热键键位 F9/F10/F11，非中文属预期） |
| `env.input_mode` | 3 | 3 | 通过 |
| `env.input_fallback` | 3 | 3 | 通过 |

## 三、七个功能区块

| 区块 | 顺序位 | 键数 | 容器 | 键名 | 判定 |
| --- | --- | --- | --- | --- | --- |
| `blk-fire-human` | 1 | 3 | `#swFire` | human.traj、human.move_ms、human.curve | 通过 |
| `blk-fire-idle` | 2 | 5 | `#swFire` | idle.mode、idle.interval_ms、idle.return_s、idle.min_remain_s、idle.wheel_notches | 通过 |
| `blk-fire-repeat` | 3 | 2 | `#swFire` | repeat.min、repeat.max | 通过 |
| `blk-fire-skip-late` | 4 | 1 | `#swFire` | skip_late.threshold_s | 通过 |
| `blk-fire-grade` | 5 | 5 | `#swFire` | by_grade.enabled、by_grade.legend、by_grade.epic、by_grade.rare、by_grade.scope | 通过 |
| `blk-fire-burst` | 6 | 4 | `#swFire` | burst.mode、burst.hold_ms、burst.interval_ms、burst.window | 通过 |
| `blk-fire-followup` | 7 | 1 | `#swFire` | followup.enabled | 通过 |

结构断言（3 次渲染分别采样）：

| 断言 | 实测 | 判定 |
| --- | --- | --- |
| 顺序 | blk-fire-human → blk-fire-idle → blk-fire-repeat → blk-fire-skip-late → blk-fire-grade → blk-fire-burst → blk-fire-followup | 通过 |
| 归属（section 的父节点为 `#swFire` 且在 `#pane-fire`） | True | 通过 |
| 键位（每区块只含对应前缀的键） | True | 通过 |
| 重绘不丢控件 | `#swFire` 内 31 个控件，期望 31 | 通过 |
| 重绘不重复渲染 | uniq=31，重复键 `[]`，section 数=7 | 通过 |
| 无控件流落在外 | 容器外键 `[]` | 通过 |

- 三次采样：首次渲染、重绘第 1 次、重绘第 2 次；结果完全一致，说明 `renderSettingsSwitches()` 重绘后分组脚本能稳定重建 7 个 section。
- 整页重载后再次采样：顺序 `blk-fire-human → blk-fire-idle → blk-fire-repeat → blk-fire-skip-late → blk-fire-grade → blk-fire-burst → blk-fire-followup`，父节点正确 `True`，`#swFire` 控件数 31。
- 全页 `data-sw` 控件共 74 个 = 设置行 67 + 运行页“节奏”面板快捷镜像 7 个；镜像键为 `fire.hz、fire.delay、fire.adaptive、fire.confirm_clicks、fire.confirm_gap、fire.jitter、fire.timeout`，与设置行同键、同样走 `applySwitch → POST /api/switches`，不是重复渲染。

按品阶停止的联动：

| 状态 | 总开关 | 三个目标件数输入框 | 判定 |
| --- | --- | --- | --- |
| 初始 | 关 | legend=禁用/0、epic=禁用/0、rare=禁用/0 | 通过 |
| 打开总开关后 | 开 | legend=可用/49、epic=可用/49、rare=可用/49 | 通过 |

## 四、互斥提示与时间同步条

| 项 | 判定 | 证据 |
| --- | --- | --- |
| 连点+补刀同开 → POST 回包警告 | 通过 | `warnings=["连点与补刀互斥，当前按连点执行"]`（服务端 `core/click_burst.py`） |
| 界面常驻提示可见 | 通过 | `#clickBurstWarn` display=`block`，parent=`#blk-fire-burst`，role=`status` |
| 文案与服务端一致 | 通过 | 界面全文「连点与补刀互斥，当前按连点执行：假购买补刀本次不生效。」，包含服务端原句 |
| 非互斥组合不显示 | 通过 | 补刀开、模式 `append` 时 display=`none`；切回后 display=`none` |
| 时间条·降级可见 | 通过 | 「时间同步：降级校时（仅 1 个来源，偏差 123.4 ms）」，title=`只收到 1 个来源，按单源校时` |
| 时间条·严格隐藏 | 通过 | display=`none` |
| 时间条触发链路 | 通过 | 未调用内部函数，`page.route` 拦截 `/api/scheduler` 后由页面自身 5s 轮询刷新 |

## 五、布局与无用控件

| 分辨率 | 页面 | `scrollWidth` / `innerWidth` | 横向溢出 | 越界元素 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 1366x768 | run | 1366 / 1366 | 无 | 0 | 通过 |
| 1366x768 | 设置/常规 | 1366 / 1366 | 无 | 0 | 通过 |
| 1366x768 | 设置/抢购 | 1366 / 1366 | 无 | 0 | 通过 |
| 1920x1080 | run | 1920 / 1920 | 无 | 0 | 通过 |
| 1920x1080 | 设置/常规 | 1920 / 1920 | 无 | 0 | 通过 |
| 1920x1080 | 设置/抢购 | 1920 / 1920 | 无 | 0 | 通过 |

- 控制台：`console.error` 0 条、`pageerror` 0 条、HTTP≥400 0 条（含两个布局页）。
- 占位词扫描（run、lib、rec、ana、journal、set；设置分栏 general、hotkey、vision、fire、lab、console、about）：命中 `无`。
- 按钮扫描 1013 个可见可用按钮：既无 `data-act/data-api/data-page/data-setpane/...` 也无绑定 id 的 0 个。

## 六、假按钮排查结论与后端接口

- 方法：对每个 `data-sw` 控件做真实交互（布尔点击 / 数字输入+change / 下拉选择），等 `POST /api/switches` 回包 `ok=true`，再读临时 `config.json` 对应路径；任一步不动即记为假按钮。
- 结果：**0 个假按钮**。67 个键全部满足 `界面 → 接口 → 磁盘 → 重载` 一致；反向也无“后端有、界面缺”，`data-sw` 集合与快照键集合完全对齐。
- 链路位置：
  - 开关表：`core/switches.py:45`（`SWITCHES`）；边界表 `core/switches.py:528`（`BOUNDS`）
  - 取值：`core/switches.py:700`（`get`）；批量写入：`core/switches.py:783`（`apply_all`）
  - 读接口：`core/server.py:476`（`GET /api/switches` → `switches.snapshot`）
  - 写接口：`core/server.py:933`（`POST /api/switches` → `Config.transaction` + `apply_all`，`server.py:943` 落盘，`server.py:949` 生成互斥 `warnings`）
  - 前端提交：`web/index.html:2244`（`applySwitch` → `api('/api/switches')`，失败走 `toast(humanize(e))`，`index.html:2256`）
  - 七区块容器脚本：`web/index.html:4109`（`FEATURE-BLOCKS`）；时间条 `index.html:4159`；品阶联动 `index.html:4199`；互斥常驻提示 `index.html:4254`

## 七、观察（非失败项，供真机路参考）

1. **成对输入倒挂时不回滚显示**：max=1 时把 min 输入 5，服务端按契约返回 `400`「最少 5 大于最多 1」，界面弹错误 toast，但输入框仍显示 `5`，磁盘仍为 `1`，重载后才回 `1`。临时实测：`ui_after=5`、`disk=1`、`ui_reload=1`。不属假按钮（后端明确拒收），但重载前界面与磁盘会短暂不一致。
2. **“节奏”面板有 7 个同键快捷镜像**（`fire.hz/delay/adaptive/confirm_clicks/confirm_gap/jitter/timeout`），与设置行同键同接口，属设计内的快捷入口，不构成重复渲染。
3. 本轮只做界面/接口/磁盘层；“行为”列用静态消费点核对（67/67 都能在引擎或导航里找到读取处），未进入游戏内实际点击验证。

## 八、复现步骤

### 8.1 一键复跑

```powershell
cd "C:\Users\26346\Desktop\project\dafors"
$env:PYTHONIOENCODING="utf-8"
\.venv\Scripts\python.exe "C:\Users\26346\Documents\Trae-CN\temp_20261107_82\_nonreal_fe_run.py"
```

脚本自带隔离：复制 `config.json` 到临时目录并改写 `CONFIG_PATH`，仓库文件只读；跑完校验 sha256 不变。原始证据落在 `_nonreal_fe_raw.json`。

### 8.2 单项手工复现（以 `fire.hz` 为例）

1. 打开 `http://127.0.0.1:<PORT>/`（端口见脚本首行 URL），点「设置 → 抢购设置」。
2. 页面执行 `document.querySelector('#swFire [data-sw="fire.hz"]').value`，与 `GET /api/switches` 里 `fire.hz` 的 `value` 比对（应相等）。
3. 输入 `100` 并派发 `change`；抓 `POST /api/switches` 回包（应 `ok=true`）。
4. 读临时 `config.json` 的 `click_hz`（应 `100`）。
5. 整页重载 → 抢购分栏，`[data-sw="fire.hz"]` 的值应为 `100`。

### 8.3 失败项复现

本轮 67 个开关、7 个区块、互斥提示、时间条、两种分辨率、占位与按钮扫描全部通过，无失败项。

---
附：原始证据 `_nonreal_fe_raw.json`（逐键 UI/快照/磁盘/重载值、区块三次采样、布局测量、控制台与 HTTP 记录）。

## 九、修复补充：非法值提交后被 400 拒收时的界面回滚（2026-11-07 追加）

本节对应第七节观察 1 的修复闭环。缺陷：成对输入倒挂（`scan.repeat.min` >
`scan.repeat.max`）提交时服务端按契约返回 400，界面只弹 toast，输入框停在
非法值（实测 `ui=5 / disk=1 / reload=1`）。修复前/后各跑一遍同一套真实界面
操作，作为对照组证据。

### 9.1 修复口径

| 项 | 处理 |
| --- | --- |
| 后端 400 契约 | **不动**：越界/倒挂继续整批拒收，`core/server.py:954` 原样回 `msg/key/reason` |
| 前端纠正 | **不做**：提交体仍是用户输入的原值（`{ [key]: value }`），不 clamp、不取整 |
| 被拒后控件 | 回填服务端实际落盘值：先按上一次成功快照兜底，再拉 `GET /api/switches` 覆盖 |
| 提示 | 错误 toast 保留，文案仍是服务端原因（如「`values：scan.repeat：最少 5 大于最多 1`」） |
| 状态条 | 修复前会一直停在「正在保存 …」；修复后改为「设置没有保存 / 未生效」 |

### 9.2 改动位置

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `web/index.html` | `2269-2308` | `readSel` / `assignKeepCaret`：可读选区按原样还原；number 输入用 `CARET` 推断插入点，赋值前临时切 `type=text` 定位再切回；越界就不碰光标 |
| `web/index.html` | `2310-2352` | `restoreSwitchControls` / `mergeSwitchValue` / `rollbackSwitch`：按 `data-sw` 回填所有同键控件；快照只合并被拒那一项；`seq` + `swVer` 双重门禁 |
| `web/index.html` | `2354-2379` | `applySwitch()`：序号登记、`timeout: 2000`、成功/失败都写 `saveNotice`，catch 里说明「本次未生效，已恢复上次保存的值」并回滚 |
| `tests/test_frontend_switch_rollback.py` | `87-155` | 7 条静态契约：不前端纠正、catch 走回滚、快照只合并不覆盖、seq/swVer 门禁、api 超时、回填覆盖数字与布尔、插入点不到末尾 |
| `tests/test_frontend_switch_rollback.py` | `285-378` | 3 条真界面用例：成对倒挂（两个方向）、单值越界、全部有界数字开关逐项 |
| `tests/test_frontend_switch_rollback.py` | `440-595` | 5 条竞态真界面用例：延迟 400、挂起超时、旧 GET 乱序、在途输入、插入点恢复 |

### 9.3 自动化测试

```powershell
cd "C:\Users\26346\Desktop\project\dafors"
\.venv\Scripts\python.exe -m pytest tests\test_frontend_switch_rollback.py -q
```

结果：`15 passed`（7 静态 + 8 无头真界面，用时约 29s）。第二轮新增的 5 条竞态/超时/插入点用例见 10.3。

覆盖点：

1. `test_pair_inversion_rolls_back_to_disk`：`min=5`（盘上 max=1）与 `max=0`（盘上 min=1）两个方向都 400 → 控件回到 `1`。
2. `test_single_out_of_bounds_rolls_back_to_disk`：`fire.hz=201`（上限 200）→ 控件回到磁盘值，toast 含「上限」。
3. `test_every_bounded_numeric_switch_rolls_back`：`core/switches.BOUNDS` 里全部 36 个 int/float 开关逐个提交「上限+1」，断言界面值 == 磁盘值。

### 9.4 修复前 / 修复后对照（38 次拒收）

列说明：基线=提交前界面与磁盘一致的值；修复前 UI=400 后输入框显示；刷新后=整页重载后回显；
判定按数值等价比较（`8` 与 `8.0` 视为相同）。修复前页面由当前文件把 `applySwitch` 的 catch
分支还原成旧实现（逐字旧代码）后由同一服务端下发，其余代码完全相同。

| # | 场景 | 键 | 非法值 | 基线 | 修复前 UI | 修复前磁盘 | 修复前·刷新后 | 修复后 UI | 修复后磁盘 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 成对倒挂 | `scan.repeat.min` | 5 | 1 | 5 | 1 | 1 | 1 | 1 | 已回滚 |
| 2 | 成对倒挂 | `scan.repeat.max` | 0 | 1 | 0 | 1 | — | 1 | 1 | 已回滚 |
| 3 | 单值越界 | `follow.retry` | 6 | 2 | 6 | 2 | — | 2 | 2 | 已回滚 |
| 4 | 单值越界 | `follow.wait_ms` | 10001 | 450 | 10001 | 450 | — | 450 | 450 | 已回滚 |
| 5 | 单值越界 | `price.min` | 100000000 | 0 | 100000000 | 0 | — | 0 | 0 | 已回滚 |
| 6 | 单值越界 | `price.max` | 100000000 | 0 | 100000000 | 0 | — | 0 | 0 | 已回滚 |
| 7 | 单值越界 | `price.quota` | 100 | 1 | 100 | 1 | — | 1 | 1 | 已回滚 |
| 8 | 单值越界 | `price.budget` | 1000000000 | 0 | 1000000000 | 0 | — | 0 | 0 | 已回滚 |
| 9 | 单值越界 | `filter.wear_max` | 11 | 0 | 11.0 | 0 | — | 0 | 0 | 已回滚 |
| 10 | 单值越界 | `countdown.min_lead` | 3601 | 6 | 3601 | 6 | — | 6 | 6 | 已回滚 |
| 11 | 单值越界 | `fire.hz` | 201 | 50 | 201 | 50 | — | 50 | 50 | 已回滚 |
| 12 | 单值越界 | `fire.threshold` | 2 | 0.85 | 2.0 | 0.85 | — | 0.85 | 0.85 | 已回滚 |
| 13 | 单值越界 | `fire.timeout` | 601 | 8 | 601.0 | 8 | — | 8 | 8 | 已回滚 |
| 14 | 单值越界 | `fire.confirm_clicks` | 1001 | 20 | 1001 | 20 | — | 20 | 20 | 已回滚 |
| 15 | 单值越界 | `fire.confirm_gap` | 1001 | 10 | 1001 | 10 | — | 10 | 10 | 已回滚 |
| 16 | 单值越界 | `fire.confirm_timeout` | 601 | 2 | 601.0 | 2 | — | 2 | 2 | 已回滚 |
| 17 | 单值越界 | `fire.delay` | 5001 | 0 | 5001 | 0 | — | 0 | 0 | 已回滚 |
| 18 | 单值越界 | `fire.jitter` | 5001 | 0 | 5001 | 0 | — | 0 | 0 | 已回滚 |
| 19 | 单值越界 | `stop.by_grade.legend` | 100 | 0 | 100 | 0 | — | 0 | 0 | 已回滚 |
| 20 | 单值越界 | `stop.by_grade.epic` | 100 | 0 | 100 | 0 | — | 0 | 0 | 已回滚 |
| 21 | 单值越界 | `stop.by_grade.rare` | 100 | 0 | 100 | 0 | — | 0 | 0 | 已回滚 |
| 22 | 单值越界 | `scan.repeat.min` | 11 | 1 | 11 | 1 | — | 1 | 1 | 已回滚 |
| 23 | 单值越界 | `scan.repeat.max` | 11 | 1 | 11 | 1 | — | 1 | 1 | 已回滚 |
| 24 | 单值越界 | `click.burst.hold_ms` | 1001 | 100 | 1001 | 100 | — | 100 | 100 | 已回滚 |
| 25 | 单值越界 | `click.burst.interval_ms` | 51 | 5 | 51 | 5 | — | 5 | 5 | 已回滚 |
| 26 | 单值越界 | `buy.skip_late.threshold_s` | 3601 | 2 | 3601 | 2 | — | 2 | 2 | 已回滚 |
| 27 | 单值越界 | `mouse.human.move_ms` | 801 | 120 | 801 | 120 | — | 120 | 120 | 已回滚 |
| 28 | 单值越界 | `mouse.human.curve` | 2 | 0.35 | 2.0 | 0.35 | — | 0.35 | 0.35 | 已回滚 |
| 29 | 单值越界 | `mouse.idle.interval_ms` | 10001 | 1200 | 10001 | 1200 | — | 1200 | 1200 | 已回滚 |
| 30 | 单值越界 | `mouse.idle.return_s` | 31 | 2 | 31 | 2 | — | 2 | 2 | 已回滚 |
| 31 | 单值越界 | `mouse.idle.min_remain_s` | 3601 | 8 | 3601 | 8 | — | 8 | 8 | 已回滚 |
| 32 | 单值越界 | `mouse.idle.wheel_notches` | 11 | 3 | 11 | 3 | — | 3 | 3 | 已回滚 |
| 33 | 单值越界 | `scan.interval` | 61 | 0.05 | 61.0 | 0.05 | — | 0.05 | 0.05 | 已回滚 |
| 34 | 单值越界 | `scan.timeout` | 86401 | 0 | 86401.0 | 0 | — | 0 | 0 | 已回滚 |
| 35 | 单值越界 | `scan.max_tries` | 1001 | 20 | 1001 | 20 | — | 20 | 20 | 已回滚 |
| 36 | 单值越界 | `scan.detail_timeout` | 601 | 3 | 601.0 | 3 | — | 3 | 3 | 已回滚 |
| 37 | 单值越界 | `auto.retry` | 101 | 3 | 101 | 3 | — | 3 | 3 | 已回滚 |
| 38 | 单值越界 | `stop.on_fail` | 1001 | 0 | 1001 | 0 | — | 0 | 0 | 已回滚 |

汇总：

| 阶段 | 拒收官次数 | 界面 == 磁盘 | 仍停在非法值 | pageerror |
| --- | --- | --- | --- | --- |
| 修复前 | 38 | 0 | 38 | 0 |
| 修复后 | 38 | 38 | 0 | 0 |

### 9.5 观察

1. **失焦会重复提交（修复前）**：非法值留在框里时，切页/失焦会再次触发 `change`，同一非法值被反复 400。
   实测请求数：修复前 `POST /api/switches` 75 次（38 次显式提交 + 重复触发），修复后 38 次；回滚本身新增 `GET /api/switches`（本次复跑共 41 次，含 3 次页面加载的开关表拉取）。
2. **状态条不再卡在「保存中」**：修复前 `#gText` 停在「正在保存 每件最少刷新」，修复后为「设置没有保存」。
3. **toast 文案两侧完全一致**（同一组 400 `msg`），说明只补了回滚，没有改动契约或错误提示。
4. `stop.by_grade.legend/epic/rare` 在总开关关掉时是置灰的，用例先打开总开关再逐项验证；值按设计保留、不清空。

### 9.6 隔离与复现

```powershell
$env:PYTHONIOENCODING="utf-8"
C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe "C:\Users\26346\Documents\Trae-CN\temp_20261107_89\_work\_rollback_verify.py"
```

- 无头 Chromium（`ms-playwright/chromium-1243`，`headless=True`），无可见窗口；未起游戏、未移动真实鼠标键盘。
- 服务端进程内 `core.server`，`CONFIG_PATH` 指向临时副本（每次运行新建 temp 目录）；
  仓库 `config.json` sha256 前后一致（本次 `d0f916ee85257a37…`），未 commit、未 push。
- 修复前页面在临时目录（`C:\Users\26346\AppData\Local\Temp\fe_before_web_3zowxl_7\index.html`），不改仓库文件。
- 原始证据：`_rollback_evidence.json`（38 行逐项 UI/磁盘/刷新/reset/HTTP/toast/状态条 + 请求计数）。

## 十、第二轮复核修复：竞态 / 超时 / 插入点（2026-11-07 追加）

独立复核的 4 项未通过逐项处理，前两项（回滚 GET 竞态、POST 无超时）为硬要求。

### 10.1 修复口径

| 复核项 | 根因 | 修复 |
| --- | --- | --- |
| 一、回滚 GET 竞态 | `rollbackSwitch` 的 `seq` 只守状态条，回填本身不判新旧；`GET /api/switches` 结果整体写回 `S.switches`，污染本地快照 | `S.swSeq`（尝试）/`S.swLastOkSeq`（最新成功）/`S.swVer`（服务端版本）三重门禁；`seq < swLastOkSeq` 时回包整包丢弃；回填与快照更新都过门禁；快照只用 `mergeSwitchValue` 校正被拒那一项（必须晚于当前 `swVer`）；成功回包也判 `seq < swLastOkSeq`；`loadSwitches` bump `swVer` 让旧异步回包作废 |
| 二、POST 无超时 | `api()` 裸 `fetch`，请求挂起后永远不落地 | `AbortController` 超时，开关 POST 上限 2000ms；超时不记断线，用 `err.timeout` 标记；按「本次未生效」toast 并回滚到落盘值；`S.saveNotice` 让「保存中 / 保存超时」保持 9s，周期刷新不覆盖 |
| 三、插入点被重置 | `el.value = v` 后 number 输入光标跳到末尾，中间插入变末尾追加（590 → 509） | 能读选区的控件按原 `selectionStart/selectionEnd` 还原；number 用输入 diff + 方向键维护 `CARET`，赋值前临时切 `type=text` 定位再切回；读数越界或取不到就不碰光标，不强行拉到末尾 |
| 四、在途输入被抹 | 回滚覆盖用户正在输入的半截值 | 保持现行为；toast 固定说明「本次未生效，已恢复上次保存的值」，不让用户以为输入丢了 |

### 10.2 改动位置

| 文件 | 位置 | 内容 |
| --- | --- | --- |
| `web/index.html` | `1622-1667` | `api()`：`AbortController` + 超时清理 + `timeout: true` 标记，超时不走 `noteFail` |
| `web/index.html` | `2269-2308` | `readSel` / `assignKeepCaret`：保插入点回填 |
| `web/index.html` | `2323-2328` | `mergeSwitchValue`：只改快照里的被拒项 |
| `web/index.html` | `2330-2352` | `rollbackSwitch`：`seq`/`swVer` 门禁守「写回填 + 更新快照」 |
| `web/index.html` | `2354-2379` | `applySwitch`：序号登记、2s 超时、`saveNotice` 全程保持、catch 回滚与说明 |
| `web/index.html` | `2381-2416` | `setSaveNotice` + `CARET/LASTVAL` 的 `focusin`/`input`/`keydown` 监听 |
| `web/index.html` | `2453-2459` | `loadSwitches` 重新读到服务端状态时 bump `swVer` |
| `web/index.html` | `2783-2789` | `paintStatusBand`：保存结果优先，9s 内不被周期刷新盖掉 |

### 10.3 新增自动化测试

`tests/test_frontend_switch_rollback.py` 现为 `15 passed`（7 静态 + 8 无头真界面，约 29s）。

| 用例 | 模拟 | 断言 |
| --- | --- | --- |
| `test_delayed_400_after_later_success` | `fire.hz=201` 的 400 延迟 1.6s，120ms 后保存 `60` 成功 | 最终 `ui==60==disk`；旧 400 不弹「已恢复」 |
| `test_post_timeout_rolls_back` | 201 的 POST 永不返回，`fetch` 在 abort 时 reject | 2.5s 后 `ui==50`、状态条「保存超时」；3.7s 跨周期刷新仍为「保存超时」；toast 含「保存超时 + 已恢复」；`__apiLog` 有 `timeout:true` |
| `test_stale_rollback_get_does_not_pollute_snapshot` | 第一次 `GET /api/switches` 立刻取磁盘(50) 但延迟 1.6s 返回 | 保存 60 后 1.7s 界面仍 60；UI 采样里保存 60 之后 0 帧回落 50；再拒一次仍回滚到 60（快照未污染） |
| `test_inflight_input_wiped_with_explanation` | 400 未回时继续键入 `8`（2018） | 回滚抹掉半截输入，但 toast 含「本次未生效 + 已恢复上次保存的值」 |
| `test_caret_restore_keeps_insert_point` | 201 → 光标左移两格 → change 400 → 回滚 | 焦点保留；再键入 `9` 得 `590`（非 `509`） |

### 10.4 修复前 / 修复后对照

**A. 回滚 GET 竞态**（复核复现路径：201 被拒 → 120ms 后保存 60（磁盘 60）→ 1.7s 旧 GET 到达）

| 版本 | 1.7s 界面 | 磁盘 | 保存 60 后出现 50 的采样帧 | 再拒一次界面 | 再拒后磁盘 |
| --- | --- | --- | --- | --- | --- |
| 修复前（无回滚） | 60 | 60 | 0 | 201（缺陷） | 60 |
| 第一轮修复（无门禁） | **50** | 60 | **37 帧（约 370ms）** | 60（伴随闪回 50） | 60 |
| 第二轮修复 | **60** | 60 | **0** | 60 | 60 |

**B. 延迟回包**（旧 400 晚于下一次成功保存返回）

| 版本 | 最终界面 | 磁盘 | 最后一条 toast |
| --- | --- | --- | --- |
| 修复前 | 60 | 60 | `values：fire.hz：201 大于上限 200`（旧错误盖在成功之后） |
| 第二轮 | 60 | 60 | `已保存：点击频率`（旧回包整包丢弃） |

**C. POST 永不返回**

| 版本 | 2.5s 界面 | 2.5s 状态条 | 7.5s 界面 | 7.5s 状态条 | 7.5s toast | 磁盘 |
| --- | --- | --- | --- | --- | --- | --- |
| 修复前 | 201 | 待命，等你开始 | 201 | 待命，等你开始 | 无 | 50 |
| 第二轮 | **50** | **保存超时** | 50 | **保存超时** | 保存超时：请求超时（2 秒后台没有响应）；本次未生效，已恢复上次保存的值 | 50 |

**D. 插入点**

| 回填方式 | 回滚后值 | 焦点 | 中间键入 `9` |
| --- | --- | --- | --- |
| 修复前（无回滚） | 201 | 保留 | 2901（非法值没被收回） |
| 朴素赋值（`el.value = v`） | 50 | 保留 | **509**（光标在末尾） |
| 第二轮（保插入点） | 50 | 保留 | **590** |

### 10.5 结论与复现

- 竞态下界面最终等于磁盘：三种路径（延迟 400、旧 GET 乱序、连续失败）都停在磁盘值，采样里没有回落旧值的帧。
- 挂起请求 2s 内按未生效处理，状态条「保存超时」跨周期刷新保持。
- 插入点与焦点在回滚后保留；取不到插入点时不动光标。
- 回归：`tests/test_frontend_switch_rollback.py` 15 passed；`tests/test_frontend_static.py`、
  `tests/test_switches.py`、`tests/test_feature_grade_stop.py` 共 88 passed。
- 隔离：无头 Chromium（`headless=True`）+ 进程内 `core.server` + 临时 config 副本；
  仓库 `config.json` sha256 前后一致（`d0f916ee85257a37…`），未 commit、未 push。
- 修复前页面由 `_work/_fe_before.py` 的 `make_before()` / `make_round1()` 在临时目录生成
  （`make_before` = 复核前无回滚无超时；`make_round1` = 第一轮有回滚但无门禁），仓库文件不动。
- 原始证据：`_rollback_race_evidence.json`（race/delayed_400/hang/caret 四组 before/after）。

```powershell
$env:PYTHONIOENCODING="utf-8"
C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe "C:\Users\26346\Documents\Trae-CN\temp_20261107_89\_work\_race_verify.py"
```
