# 真机自动验证执行器 · 使用说明

一条命令跑完 R0–R7：采集、点击、判定、重试、止损、出报告全部由程序完成。
你只需要把游戏停在交易行在售列表页，然后说"开始"。

---

## 1. 三步

1. 游戏打开，停在**交易行 → 在售列表页**（不要停在详情页/弹窗上）。
2. 在项目根目录执行下面这条命令。
3. 跑完看 `_realmachine_result.md` 的第一屏结论。

```powershell
cd C:\Users\26346\Desktop\project\dafors
.\.venv\Scripts\python.exe tools\realmachine_auto.py
```

开跑前可以先看排期与阈值（不碰屏幕、不点击）：

```powershell
.\.venv\Scripts\python.exe tools\realmachine_auto.py --plan
.\.venv\Scripts\python.exe tools\realmachine_auto.py --self-check
```

`--self-check` 只做导入检查和 OCR 后端探测，输出 `import_error: 无` 才算环境就绪。

---

## 2. 常用参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `--click-tests N` | 8 | 点击测试次数（手册口径 20；任一次成交立即停） |
| `--click-interval S` | 0.9 | 点击间隔秒 |
| `--live-rounds N` | 25 | R2 实机读价采样轮次 |
| `--countdown-observe-s S` | 20 | 倒计时观察窗 |
| `--soak-s S` | 30 | R2 短时稳定性；`0` 关闭 |
| `--events auto\|on\|off` | auto | 事件窗口：auto=仅交互终端启用，on=强制，off=关闭 |
| `--event-timeout S` | 300 | 每个事件等信号的超时 |
| `--overnight-hours H` | 0 | 过夜模式时长，常用 6–12 |
| `--overnight-sample-s S` | 60 | 过夜采样间隔 |
| `--hotkey` / `--event-key` | F8 / F9 | 中止键 / 事件确认键 |
| `--no-click` | 关 | 强制只读，一次都不点 |
| `--no-doctor` | 关 | 跳过 dist exe 的 `--doctor` |
| `--pytest` | 关 | 跑离线 pytest（慢） |

---

## 3. 预估耗时

| 阶段 | 名称 | 预估 | 说明 |
|---|---|---|---|
| R0 | 环境与基线 | 25s | 配置、截屏/DPI、窗口、OCR、权限 |
| R1 | 离线 / 静态 | 75s | 区域合法性、`--doctor`、倒计时离线模拟 |
| R2 | 实机只读 | 70s | 页面识别、读价稳定率/时延、倒计时观测 |
| R3 | 点击类（最后） | 22s | 默认 8 次，任一次成交即停 |
| R6 | 事件窗口 | 5–8min | 4 个信号；每个信号最多等 `--event-timeout`（默认 300s），最坏 20min |
| R7 | 过夜模式 | `--overnight-hours` × 3600 | 默认关闭；6h≈6h，8h≈8h |
| R4 | 回退与收尾 | 5s | 收尾审计、配置完整性、记录污染 |
| R5 | 驱动与打包专项 | 1s | 多为"需管理员/不可自动判"的跳过项 |

不含过夜：**约 9–12 分钟**。含过夜：在上述基础上叠加小时数。

---

## 4. 门禁与失败行为

阶段链：`R0 → R1 → R2 → R3 → R6 → R7`（R4/R5 无论如何都跑）。

- 门禁项：R0-1/3/8/9/13、R1-1/2、R2-1/2、R3-1/2/3。
- 前置阶段门禁不过 → 后续阶段**整体跳过**，报告标注「因前置未过而跳过」，不会硬跑。
- 单项判定只有三种：**过 / 不过 / 跳过**（跳过原因附中文：因前置未过、不可自动判定、此项需管理员、本机不存在、命令行已关闭、已中止、等待事件超时、误买止损）。

R3 之前会先查前置，任一不满足就整体跳过并写明原因：

- `console.max_price` 未设 > 0 → 不点（防误买）
- `semi_mode=true` → 不点（不符合全自动语义）
- 游戏不在前台 / 输入通道未建立 / 本进程完整性级别低于游戏（UIPI）→ 不点

---

## 5. 中止（随时可用）

两种方式等效：

1. 按 **F8**；
2. 在项目根目录创建一个空文件 `_realmachine_abort`。

中止后程序**先收尾再退出**，收尾内容固定：

- 停掉两个热键钩子
- 连发两次 `LEFTUP`，确保没有按键残留
- 解除 `ClipCursor` 锁定
- 光标还原到启动位置并测量还原误差（px）
- 还原启动时的前台窗口
- 关闭截屏对象
- 写完 `_realmachine_result.json` / `.md` 与 `_realmachine_trace.log`

已经在跑但没跑完的阶段会保留已执行部分，不整段丢弃（未执行项标「已中止，保留已执行部分」）。

热键：中止 `F8`，事件确认 `F9`，可用 `--hotkey` / `--event-key` 改。

---

## 6. 事件窗口（R6，需要你在场）

执行器只**提示**，然后等信号，**不提问、不要求回话**。信号：按 **F9**，或创建 `_realmachine_event` 文件。

三个事件按顺序进行，每个事件自动做前后对比：

| 事件 | 你要做的 | 执行器自动判定 |
|---|---|---|
| 显示器休眠→唤醒 | 熄屏（显示器电源键 / Win+L）保持 ≥10s，再唤醒，按 F9 | 截图非黑屏（均亮度 ≥3）、客户区尺寸一致、DPI/显示器数量/边界不变、坐标映射误差 ≤8px（临时移动光标后立刻还原） |
| 系统睡眠→恢复 | 开始菜单→睡眠（或合盖）≥20s，唤醒后按 F9 | 恢复后新起 2s 定时器须在 2.0±0.5s 内触发；单调时钟前进、0.5s 探针 450–800ms（**不看壁钟**，壁钟只作记录）；截图/尺寸/DPI；驱动探针 + 光标 + 游戏窗口都活着 |
| 游戏掉线/重登 | 让游戏掉线或退回登录页，停在登录页按 F9；重新登录回到在售列表页后**再按一次 F9** | 登录页识别（Navigator 页面 + 全屏关键词：登录/进入游戏/服务器/账号/密码/连接/重新连接）；掉线期间**点击次数必须为 0**；重登后页面须回到 `onsale`/`detail` |

- 超时（默认 300s）→ 该组项全部标「等待事件超时」，不阻塞后续阶段。
- 非交互终端且 `--events auto` → 整体标「命令行已关闭」，不会卡住自动化。
- 事件发现登录界面即**安全停下**，不盲点。

---

## 7. 误买即停

任何一次点击后出现以下任一证据，立即判定成交并终止全部后续阶段：

1. `core/records.py` 出现新成交记录（时间戳晚于点击前基线）；
2. 确认弹窗模板匹配 ≥0.85（且点击前基线 <0.85）；
3. 在售列表价格集合少件。

画面帧差 ≥6.0/255 只当**弱受理证据**（计入 R3-4 受理率，并单独计数），不单独触发止损。

命中后：

- 报告 `trade.suspected=true`，并单独标注类型与证据；
- 后续阶段全部标「误买止损，后续未执行」；
- 立即执行第 5 节的收尾流程；
- 退出码 `2`。

---

## 8. 截图与权限

- **默认不落盘任何截图**，画面只在内存里做判定。
- 只有失败项才截图，**每项最多 3 张**，存 `_realmachine_shots/<run_id>/`。
- 不装驱动、不改系统设置、不动注册表。
- 需要管理员的地方（注册表导出/还原点、驱动安装与重启）**先检测**，检测到就标「此项需管理员，未执行」，不提权。

---

## 9. 两份报告

| 文件 | 给谁看 | 内容 |
|---|---|---|
| `_realmachine_result.json` | 机器 | 逐项 `status`（pass/fail/skip）、`measured`、`threshold`、`detail`、`shots`；阶段汇总；阈值表；点击/事件/过夜数据；`conclusion` |
| `_realmachine_result.md` | 人 | 第一屏结论 + 阶段汇总表 + 逐项明细 + 实测数据 + 不可自动判定清单 + 失败截图路径 |

Markdown 的结论区固定为这几行，一屏可读完：

```
结论：通过 / 不通过 —— N 项未过；首要不通过项 R2-2（实测 … / 阈值 …）
通过 X · 未过 Y · 跳过 Z（不可自动判定 a / 因前置未过 b / 需管理员 c …）
误买：未发生 / **列表少件** · …
中止：否 · 用时 9.2 分钟 · 点击 8 次
门禁未过的阶段：…
```

辅助文件：

- `_realmachine_trace.log`：运行日志（阶段、判定、异常）
- `_realmachine_shots/<run_id>/`：失败截图
- `_realmachine_backup/<run_id>/`：配置与关键文件备份

---

## 10. 过夜模式（R7）

```powershell
.\.venv\Scripts\python.exe tools\realmachine_auto.py --overnight-hours 8 --overnight-sample-s 60
```

- 只读采样，**期间不点击**；随时可按 F8 中止。
- 每 `--overnight-sample-s` 写一行到 `_realmachine_overnight.jsonl`（`flush`+`fsync`，掉电也不丢已采样本）。
- 每行含：`kind`、`run_id`、时间、`elapsed_s`、`private_mb`、`working_mb`、`handles`、`threads`、`children`、`ocr_ms`、`prices`、`game_alive`、`frame_ok`。
- 判据（沿用现有阈值表）：

| 项 | 判据 |
|---|---|
| R7-2 内存斜率 | ≤0.5 MB/h（最小二乘拟合私有内存） |
| R7-3 句柄 | 增量 ≤50 且**非单调增长** |
| R7-4 线程 | 波动 ≤4 |
| R7-5 周期耗时 | OCR p95 ≤120ms |
| R7-6 存活率 | ≥99% |
| R7-7 残留进程 | 0 |

失败与异常**必须落盘**：中止写 `kind="abort"`，异常/中止收尾写 `kind="interrupted"`（带 `reason`、`samples`），正常结束写 `kind="finish"`（带斜率与末样本）。Markdown 报告的实测数据区同步给出一行可读结论（样本数、斜率、首末内存、句柄、存活率、曲线路径）；若启用但没产生样本，也会明确写出来，不静默。

---

## 11. 不可自动判定（报告里明确列出，不假装能判）

- R1-5 OCR 真值一致性（需人工标注 30 条价格）
- R1-6 干净机器矩阵（需另一台机器/虚拟机）
- R2-3 价格真值一致性（同上）
- R2-5 T0 绝对偏差（需 120fps 录屏回放对照，本执行器只有软件时间轴）
- R2-8 反作弊长时观察（需测试账号 + 3×30min 值守）
- R4-4 / R4-5 崩溃、杀进程、断电恢复（破坏性操作不在本执行器范围）
- R5-2 驱动安装与重启 / R5-4 反作弊×驱动长时 / R5-5 干净机器打包矩阵

---

## 12. 退出码

| 码 | 含义 |
|---|---|
| 0 | 通过（无未过项） |
| 1 | 有未过项 |
| 2 | 中止或误买止损 |
| 3 | 执行异常（报告里带 `fatal`） |

---

## 13. 常见问题

- **R6 全被跳过**：非交互终端下 `--events auto` 不阻塞；要在终端里跑，或加 `--events on`。
- **R2-4 跳过**：没配 `regions.clock`，或当前没有公示期商品，属正常。
- **R3 整体跳过**：检查 `console.max_price`、`semi_mode`、游戏是否在前台。
- **R1-4 的 coverage**：`bench_countdown` 的 `coverage` 是"跑出数比例"，判据为 **≥95%**，与"剔除率"方向相反。
- **报告写不进**：目录只读或文件被占用；程序会打印失败原因但不中断收尾。

---

# v2 全自动真机验证（realmachine_v2.py）

`tools/realmachine_auto.py` 的扩展版：八阶段、可单跑、不卡壳、即修即验。
基础版 R0–R7 的命令与参数原样保留，两者互不替代。

覆盖矩阵是唯一真源：`_realmachine_coverage.md`（77 项，与执行器同源生成，改代码即同步）。

## V1. 前置条件

| 项 | 要求 |
|---|---|
| 游戏 | 段位/账号无关；打开游戏客户端即可（窗口类名 `UnrealWindow`） |
| 权限 | 建议管理员（`--auto-elevate` 默认开，会弹 UAC 并自动重启自身） |
| 输入驱动 | 只检测不安装；不可用自动降级（`input_fallback`），不阻塞阶段 |
| S2/S3 动手 | 必须显式给 `--allow-buy`；S3 点火还需 `--allow-fire` |
| S5 | 离线可跑，不需要游戏；本地起 `core.server` + 无头浏览器（chromium/chrome/msedge 依次尝试） |
| S6 | 数据字段与遥测：S6-1..S6-5 用合成记录离线可跑；S6-6/S6-7 需真实成交 |
| S7 | 性能基线：S7-0/S7-2 离线可跑；S7-1/S7-3 需真机购买；S7-4 需 `--overnight-hours` |
| 单实例 | 全程互斥量 `Local\SJZ-Realmachine-V2`，重复启动直接拒绝（退出码 2） |

## V2. 命令

```powershell
cd C:\Users\26346\Desktop\project\dafors
$py = ".\.venv\Scripts\python.exe"

# 只看排期（不碰屏幕）
& $py tools\realmachine_v2.py --plan
& $py tools\realmachine_v2.py --self-check

# 只生成/刷新覆盖矩阵，并打印两串清单（不执行任何检查）
& $py tools\realmachine_v2.py --coverage-only

# 单个阶段，可反复重跑
& $py tools\realmachine_v2.py --stage S0
& $py tools\realmachine_v2.py --stage S5
& $py tools\realmachine_v2.py --stage S6
& $py tools\realmachine_v2.py --stage S7
& $py tools\realmachine_v2.py --stage S0 --stage S5

# 全部阶段（无游戏时 S0/S1/S3/S6/S7 相关项自动 BLOCKED，不算失败）
& $py tools\realmachine_v2.py --stage all --no-auto-elevate

# 真机抢购链路（标的与预算必须给全）
& $py tools\realmachine_v2.py --stage S2 --allow-buy --target "测试标的" --max-price 100 --budget 300

# 卡点精度（不足 16 条成对读数不点火）
& $py tools\realmachine_v2.py --stage S3 --allow-fire --s3-p95-ms 80 --s3-window-ms 120

# 性能基线长时项（过夜）
& $py tools\realmachine_v2.py --stage S7 --overnight-hours 8
```

## V3. 阶段与检查项

| 阶段 | 内容 | 项数 | 其中必须开游戏 |
|---|---|---|---|
| S0 | 环境预检（配置/合法性/许可/磁盘/DPI/权限/驱动/窗口/区域/OCR/中止/三通道身份/钩子/RawInput/光标还原/前台/缩放/多显示器/三源校时/单源降级/断网） | 21 | 5 |
| S1 | 任意界面 → 交易行主界面 → 目标列表 + 空闲漫游三条 + 最大化还原 | 8 | 5 |
| S2 | 单件全链路 + 刷新次数 1–3 / 买到即停 / 来不及跳过 / 按品阶停 / 连点 / 补刀 | 12 | 5 |
| S3 | 反推解锁点点火 + 拟人轨迹三项（开火零位移 / 非开火曲线 / 瞬移降级率） | 8 | 5 |
| S4 | 九类故障自愈（含卡顿/掉线重登/休眠唤醒）+ 看门狗卡死恢复 | 11 | 0 |
| S5 | 控件→接口→配置→行为 四段联调（67 个开关，含 21 个新增专项汇总） | 5 | 0 |
| S6 | 数据字段与遥测：七字段落盘/类型/null 语义/台账可解析/事件入库/看板/真实成交行/上报 | 7 | 2 |
| S7 | 性能基线：换算自检 / 单件端到端 / 点击光标占用 / offset 分布 / 过夜资源 | 5 | 3 |

矩阵共 **77 项**：必须开游戏 **25**，不开游戏 **52**。每行都有 ID、阶段、类别、前置、量化判据、超时、失败归属、是否必须开游戏 —— 见 `_realmachine_coverage.md`。

单独重跑：任意阶段都可 `--stage <Sx>` 反复执行，报告按 run 目录独立落盘。

## V4. 不卡壳机制

1. **单项超时 = PENDING（待判）**，不算失败，继续下一项；阶段/全局超时同理。
2. **阶段重试预算** `--retry`（默认 3）：阶段异常重试，耗尽标 BLOCKED 继续下一阶段。
3. **阶段隔离**：`run_stage` try/catch 边界 + `safe_reset`（松左键、`release_cursor_lock`、只发 key-up 清按下态、不动用户光标）。
4. **单实例**：重复启动拒绝。
5. **中止**：F8，或创建 `_realmachine_abort`（启动前存在即按中止处理，文件保留）。中止立即进安全态，退出码 2。
6. **进程收尾**：报告与清理完成后用 `TerminateProcess` 硬退出，绕开驱动 DLL detach 卡死（本机实测 `interception_get_filter` 会在收尾挂住，故 S0 驱动检查只做 open+identity）。

## V5. 即修即验

失败项自动收集：最小复现步骤（最近 60 步）、日志片段（最近 300 行）、配置快照、前后端返回码、必要截图。

归属判定（与报告内「归属判定规则」一致）：

| 归属 | 判据 |
|---|---|
| 环境 | 驱动/提权/窗口/DPI/磁盘/OCR/截图/浏览器缺失 |
| 前端 | S5 控件段：控件缺失、disabled、点击无请求、值不一致 |
| 后端 | S5 接口/配置/行为段：5xx、ok=false、合法值被拒、未落盘、读不回 |
| 后端 | S2/S3/S4 业务链路异常 |

修复：
- 登记在 `FIXERS` 的项自动改（当前 S0-2 开关越界、S0-9 区域越界）。
- 检查项可通过 `c.codes["fix_hint"]` 声明 `{file, old, new}`，由 `apply_code_fix` 执行：限仓库内、唯一匹配、备份到 `_rm_backup/<run>/code/`、生成 unified diff。
- 每项最多 3 轮，只重跑该项；3 轮不过标 FAIL 并写复现路径。
- 所有修改进报告 `fixes[]`：文件、行号、依据、before/after、diff、重跑结果。无记录不改。

## V6. 报告与证据

| 产物 | 路径 |
|---|---|
| JSON | `_realmachine_v2_result.json`（schema `realmachine_v2_result/1`） |
| Markdown | `_realmachine_v2_result.md`（结论、阶段总表、逐项明细、归属规则、即修即验、复现路径、总账） |
| 截图 | `_rm_shots/<run_id>/<cid>_N.png`，每项 ≤3 张，仅失败/关键节点 |
| 备份 | `_rm_backup/<run_id>/`（config 与代码原文） |
| 追踪 | `_realmachine_v2_trace.log` |
| 覆盖矩阵 | `_realmachine_coverage.md`（77 项，每次启动自动刷新） |

清理策略：`_rm_shots` / `_rm_backup` 各保留最近 12 个 run 目录且不超过 14 天，当前 run 永不删。

## V7. 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部可判定项通过 |
| 1 | 存在 FAIL |
| 2 | 中止（F8 / `_realmachine_abort`）或提权重启 |
| 3 | 执行异常（报告 `fatal`） |
| 4 | 仅 BLOCKED / PENDING（不算失败） |

## V8. 真机开跑顺序

1. 用户打开游戏，停在任意已知界面（不需要手动摆位）。
2. `--stage S0` → 看窗口/OCR/驱动是否 PASS。
3. `--stage S1` → 自动回到交易行主界面并进目标列表。
4. 给 `--allow-buy` + 标的/预算 → `--stage S2`，触发超预算迹象立即回安全态。
5. `--stage S3`（可选 `--allow-fire`）→ 看偏差分布与超窗比例。
6. `--stage S4 --allow-faults` → 让脚本自己制造九类故障并自愈。
7. `--stage S5` → 前端四段联调（可随时离线跑）。
8. `--stage S6` → 七字段落盘与遥测；S6-6/S6-7 需要刚成交的 1 单。
9. `--stage S7` → 性能三数；S7-4 用 `--overnight-hours 8` 过夜。

## V9. 覆盖矩阵（`_realmachine_coverage.md`）

`COVERAGE_ROWS` 是执行器里的唯一真源：`V2_CHECKS`（阶段兜底清单）、
`_realmachine_coverage.md`、报告里的两串清单、`--coverage-only` 输出都由它派生。
**改代码即改矩阵**，不会出现「测了却没登记」或「登记了却没人跑」。

报告 JSON 顶层多一段 `coverage`：

```json
{"path": ".../_realmachine_coverage.md",
 "counts": {"total": 77, "must_game": 25, "no_game": 52},
 "must_game": [...], "no_game": [...],
 "uncovered": [],
 "diff": {"stages_ran": [...], "missing": [], "extra": [], "must_game_not_run": []}}
```

Markdown 报告对应「覆盖清单（矩阵 → 本次实跑）」一节，两串清单 + 对账结果。

### 必须开游戏才能测的项（25）

```
S0-8  S0-9  S0-10 S0-15 S0-16
S1-1  S1-2  S1-3  S1-4  S1-8
S2-1  S2-2  S2-3  S2-4  S2-5
S3-1  S3-2  S3-3  S3-4  S3-5
S6-6  S6-7
S7-1  S7-3  S7-4
```

- S0-8/9/10：游戏进程、客户区区域映射、真实帧 OCR。
- S0-15/16：光标还原、前台不被抢（需真实窗口与真实光标）。
- S1-1..4：页面识别、回主界面、进在售列表、步数预算。
- S1-8：最大化/还原后区域坐标。
- S2-1..5：读价 / 选量 / 确认 / 台账 / 全链路耗时（需 `--allow-buy`）。
- S3-1..5：成对读数 ≥16、反推零点、偏差分布、超窗比例、成对策略（需 `--allow-fire`）。
- S6-6：真实成交行七字段取值；S6-7：真实遥测上报。
- S7-1：单件端到端耗时；S7-3：`offset_ms` 分布；S7-4：过夜资源。

### 不开游戏也能测的项（52）

```
S0-1..7  S0-11..14 S0-17..21
S1-5..7
S2-0  S2-6..11
S3-6..8
S4-1..11
S5-0..4
S6-1..5
S7-0  S7-2
```

## V10. 离线自测（不需要游戏）

```powershell
& $py tools\realmachine_v2.py --stage all --no-auto-elevate
```

判定口径：**FAIL=0 即通过**；`BLOCKED` 是「需要游戏/需要真实成交」，不是失败；`SKIP` 是「本机不存在/命令行关闭」。

最近一次离线自测（2026-11-07）：

| 阶段 | 过 | 不过 | 阻塞 | 跳过 |
|---|---|---|---|---|
| S0 | 14 | 0 | 4 | 3 |
| S1 | 4 | 0 | 4 | 0 |
| S2 | 6 | 0 | 0 | 6 |
| S3 | 3 | 0 | 1 | 4 |
| S4 | 11 | 0 | 0 | 0 |
| S5 | 5 | 0 | 0 | 0 |
| S6 | 5 | 0 | 2 | 0 |
| S7 | 2 | 0 | 2 | 1 |

- 退出码 `4`（仅阻塞/待判），**不通过 0 项**。
- 矩阵对账：`missing=[] extra=[] must_game_not_run=[]`，无漏项。
- 离线期间：不开可见窗口（S5 用无头浏览器）、不动真实光标（S7-2 用独立桌面测试窗口）、不联网（S0-19/20 用假 NTP）。

开游戏后只需重跑这些被 BLOCKED 的 13 项（S0-8/10/15/16、S1-1/2/3/8、S3-1、S6-6/7、S7-1/3），其余项本轮已验过。

## V11. 纪律

- 不 commit、不 push。
- 不开可见窗口；真机阶段才允许真实点击/移动光标。
- 失败证据每项 ≤3 张截图，存 `_rm_shots/<run_id>/`。
- 报告双份：`_realmachine_v2_result.json` + `_realmachine_v2_result.md`。
