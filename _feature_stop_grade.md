# 按品阶分别计数停止（stop.by.grade）改动说明

契约：`_feature_contract.md`（先读后按 §2.5 / §4.1–4.5 / §5 / D1–D5 / 验收门禁 2、4 实现）。
本文件只覆盖这一路功能，未动其它六个命名空间的块。

## 改动文件

| 文件 | 改动 |
| --- | --- |
| `core/stop_grade.py` | 新增。判阶（复用 `core/bricks.find_skin`）、三档计数 `Counter`、达标/说明函数 |
| `core/config.py` | `DEFAULTS` 末尾追加 `stop.by_grade`，带 feature 起止标记与合并提示 |
| `core/switches.py` | 「抢购设置」组追加 5 个开关（1 个总开关 + 3 个件数 + 1 个下拉）；`BOUNDS` 追加 3 条 0~99；均带 feature 起止标记 |
| `core/engine.py` | 品阶计数/闸门/停止原因接入：列表、倒计时、详情三条路线；`_record` 追加 extra；`stats.grade_counts` / `stats.stop_reason` |
| `tests/test_feature_grade_stop.py` | 新增，44 个用例（D1/D2/D3/D4/D5.7 + 总开关 enabled + 前端无头渲染/置灰） |
| `web/index.html` | `#swFire` 内补 `#blk-fire-grade` 静态容器（用 `blk-fire-grade-start/end` 注释包住），供选择器静态解析；运行时由 `FEATURE-BLOCKS` 共享脚本按 MAP 顺序复用/创建七个容器，三个数字框与总开关都被搬入该容器。末尾追加 `FEATURE-GRADE-UI` 独立脚本块（append-only）：总开关关掉时三个数字框置灰、值保留；不写共享 `FEATURE-BLOCKS` 区，不改渲染器与事件委托 |

## 新增配置键（命名空间只有 `stop.by_grade.*`）

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `stop.by_grade.enabled` | bool(strict) | `false` | 总开关。关掉时整个功能不参与判定：不计数、不影响停止原因、不写台账；三档目标原样保留 |
| `stop.by_grade.legend` | int(integral)，0–99 | 0 | 传说目标件数，0 = 不限 |
| `stop.by_grade.epic` | int(integral)，0–99 | 0 | 紫（目录中的「史诗」）目标件数，0 = 不限 |
| `stop.by_grade.rare` | int(integral)，0–99 | 0 | 蓝（目录中的「稀有」）目标件数，0 = 不限 |
| `stop.by_grade.scope` | select(strict)：`all` / `grade` | `all` | 任一档达标后：整轮停止 / 只停该品阶 |

界面下拉只渲染中文「整轮停止」「只停该品阶」，英文枚举仅存在于 `option value`，不显示。
`enabled=false` 时三个数字框 `disabled=true`（只置灰，不改 `value`、不清空），重新打开即恢复上次填的值。
原需求里的 `on_reach`/`target.*` 旧键按契约作废，未保留第二套。

## 计数与停止的实际判定路径

1. 判阶：`stop_grade.grade_key(text)` → `bricks.find_skin(text)` 的 `tier`，
   `传说/史诗/稀有 → legend/epic/rare`，`普通 → other`（不计数），查不到 → `unknown`（不计数、不拦、info 日志一条）。
2. 总开关：`stop_grade.enabled(s)` 为 false（默认）时，以下 3–4 步全部短路，
   `_record` 的 `extra` 不出现任何品阶字段；`stop_grade.armed(s)` = 开关打开且至少一档 > 0，
   只有 armed 时闸门/收口才真正工作（打开 + 全 0 = 只计数、不停）。
3. 计数（唯一入口）：`Engine._record()` 里 `result == "hit"` 时调用 `_grade_on_hit()`；
   `_record` 只在校验成交后被 `_fire_and_confirm` 调用，miss / 未校验不计数。
   每次计数同时写 `stats.grade_counts`，并把 `grade`、`grade_counts`、`stop_reason`、`stop_detail` 追加进 `records.add(extra=)`。
4. 尝试前闸门：列表模式 `_execute_list` 选卡后、倒计时模式 `_countdown_prepare` 的 `sweep()` 里，
   已达标的档位：`scope=all` → `Aborted`（“X 品阶已达 N 件”）；`scope=grade` → 跳过该件，其它档继续。
5. 成交后收口：`_grade_after_hit()` 在列表成功分支、倒计时成功分支、详情成功分支各调一次；
   `scope=all` 抛 `Aborted`，`scope=grade` 只记日志并在后续闸门跳过该档。
6. 停止原因优先级：`grade`（品阶）> `quota`（总量）——同一件同时触发时以品阶为准；
   `budget` 是购买前的独立拦截（买不起就一件不点）。三者都写 `stats.stop_reason` 与 `stats.stop_detail`；
   台账：品阶/总量写进触发成交那件的 `extra.stop_reason`，预算写一条 `result=over_budget` + `extra.stop_reason=budget`。
   开关关闭时 `grade` 这一路的 extra 一律不写，`quota`/`budget` 原因照旧。

## 验证结果

- `pytest tests/test_frontend_static.py -q` → **19 passed**。此前红的 `test_every_selector_resolves_to_an_element` 已恢复：源码里 `getElementById('blk-fire-grade')` 有对应的静态 `id="blk-fire-grade"` 锚点。
- `pytest tests/test_feature_grade_stop.py -q` → **44 passed**（2.93 秒；本次新增 13 个用例）。
  - D1：单档整轮停、多档同时达标（`reached` 一次列出三档）、全 0 不限但计数照更、未知品阶不计数且不猜、普通不参与。
  - 总开关（新增）：默认关闭 → 即使填了 `legend=1` 也 3 件不停、计数全 0、台账无 `grade`/`grade_counts`、日志无“品阶”字样；
    打开 + 全 0 → 计数照更但不达标；打开 + 某档达标 → 按 scope 停；严格布尔拒收 `maybe/2/"2"/None/[]/{}/NaN`，报“不是布尔值”；
    关→开往返后 `2/3/5` 三个目标仍在（落盘 + 重新加载各断言一次）。
  - D2：开关表 5 项在「抢购设置」组（bool 默认 false）；`switches.set → cfg.save → Config.load → stop_grade.settings` 三段断言；引擎读 `Config` 对象。
  - D3/D4：`abc/""/None/[]/{}/NaN/Inf` 拒收；`-1/100/999/1.5/-0.5` 拒收并给“小于下限/大于上限/必须是整数”原因；`scope=stop_all` 拒收为“不在可选值”；批量一坏全拒；拒收后 `config.json` 字节不变。
  - D5.7：grade+quota 同触发 → 原因 `grade`；scope=grade + quota=3 → 三档各 1 件后按 quota 停；budget=100 < 单价 → 0 点击、原因 `budget`、台账有 `over_budget` 行。
  - 前端：node 无头跑真实 `numHtml`/`selectHtml`/`switchHtml`，断言 5 行控件（1 个 `data-kind=bool`、3 个 `data-kind=int`、1 个 select），下拉中文且无 `>all<`/`>grade<`；5 行可归入 `section#blk-fire-grade`。
    另跑 `gradeUiState(false/true)`：关掉时三个 `stop.by_grade.*` 全部 `disabled=true`，打开时全部 `false`；脚本块内无 `el.value` 写入（值不清空）。
- 相关回归（`pytest tests/test_switches.py tests/test_feature_followup.py tests/test_engine_list_queue.py tests/test_numeric_hardening.py -q`）：
  **146 passed, 1 failed**。唯一失败是 `test_numeric_bounds_cover_every_numeric_switch` 报 `click.burst.hold_ms` 不在 `BOUNDS`——
  该键来自 `click.burst` 分支的并行改动，与本功能无关（本功能 3 个 int 键的 `BOUNDS` 均在表内）。
- 全量（含许可排除）此前一轮：**1744 passed, 14 failed**，失败均为既有输入/OCR 环境类用例；许可相关文件因内置公钥长度本来失败，未改其代码。

## 与契约的差异（按要求列明）

1. **新增了 `stop.by_grade.enabled` 总开关**：契约 §2.5 的字段表与 §4.1 `DEFAULTS` 只有四个字段，本次按追加指令补 `enabled`（bool、strict、默认 `false`）。
   行为：关闭时完全不参与判定（不计数 / 不影响停止原因 / 不写台账），三档目标值原样保留；打开后按原三档逻辑走。
2. **计数落在 `_record` 而不是 `_fire_and_confirm` 内部**：`_record` 只由校验成交后的代码调用，语义满足“校验成交后才 +1”，且能一次覆盖列表/倒计时/详情三条路线；开关关闭时在函数入口短路。
3. **尝试前闸门放在 `_execute_list` 与 `_countdown_prepare` 的 `sweep()`**：`sweep()` 是契约给的扩展点；`_execute_list` 选卡处严格说不在此表，但它是“下一件闸门”的唯一入场点，未重排既有语句。
4. **预算停止写一条 `over_budget` 台账行**：契约只允许“extra 追加字段、不许新增枚举”。为满足“台账能查到预算停止原因”，复用既有 `over_budget` 结果码并追加 `extra.stop_reason`；该行会让统计的“尝试数”+1（仅预算停止的轮次）。开关关闭时该行不带 `grade_counts`。
5. **`stop_after_hit` 在引擎里没有消费点**（基线既有缺口，不是本次引入）；D5.7 的“命中即停优先”无法实测，本次确定顺序为：成交后先判品阶（all 则停），再判 quota。
6. **`web/index.html` 追加了独立 `FEATURE-GRADE-UI` 脚本块**：只做总开关 → 三个数字框置灰/恢复的交互，append-only，不写契约 §4.4 的共享 `FEATURE-BLOCKS` 区，不改渲染器、事件委托与其它区块。契约原本没有这条交互要求，属新增。

## 未覆盖边界

- 倒计时路线一次运行只成交 1 件，`scope=grade` 的多件累计只在列表路线验证；倒计时闸门只验证了“跳过/整轮停”入口。
- 判阶依赖 OCR 卡面文本命中皮肤目录；目录外皮肤一律 `unknown`（不计数、不拦），不会按颜色/价格猜。
- 确认价超出预算的 `Aborted` 分支沿用原行为：只记 miss + 中止日志，不写预算台账行（预算台账只覆盖“剩下的都买不起”的主路径）。
- `scope=grade` 且余下档位都达标又都抢不到时，会继续扫描到 quota/budget/超时/手动停止，不会自行收尾。
- 引擎被 watchdog 重启后计数从 0 重来（与 quota/budget 同为“本轮”语义）。
- 未做真机验证：按要求不开可见窗口、不移动真实鼠标，全部为假环境 + node 无头断言。
- `enabled` 关闭的“完全不参与”在三条路线的共用入口（`_record` / `_grade_gate`）上验证；置灰交互只做 node 层 `gradeUiState` 与源码断言，未跑真实浏览器渲染。
