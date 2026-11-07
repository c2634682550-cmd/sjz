# 真机验证方案（dafors / SJZ 抢购控制台）

> 性质：**执行手册**，不是结论报告。文中所有“通过/失败”阈值都要在执行时用实测数据填。
> 适用对象：能独立操作 Windows、会看日志、能在游戏里手动买卖的人。
> 代码基线：执行前填 `git rev-parse HEAD` 与 `dist\SJZ-Control.exe` 的 SHA256。
> 已作为前提、不再重复论证的代码层结论：见 `_input_timing_report.md`、`_branchA_input_bench.md`、`_branchB_precision.md`、`_branchC_countdown.md`、`_ocr_bench_user_shots.md`。

---

## 0. 使用规则

### 0.1 编号

| 编号 | 含义 |
|---|---|
| `V1`–`V8` | 八项验证 |
| `B1.1`–`B8.4` | 每项验证的备选方案；`Bx.y` 的 x 对应 Vx |
| `P1`–`P15` | 问题预测条目，按“概率×影响”排序 |
| `R0`–`R5` | 执行阶段 |
| `S1`–`S6` | 全局止损条件 |

### 0.2 三条硬规则

1. **先备份，后动手。** 没有完成 §1.2 的备份，R4/R5 一律不开。
2. **一项 FAIL，先按 §5 回退，再继续。** 不允许“先记下来、回头再说”式跳项。
3. **只点测试商品。** 测试账号、最低价、可丢弃或可转卖；单场点击上限 300 次；F8 急停；鼠标甩左上角触发 `failsafe` 中止。

### 0.3 每项验证的固定结构

每项都按同一顺序写：目的 → 依赖 → 怎么测 → 测几次 → 通过标准 → 失败标准 → 备选方案。执行时把“实测”填进 §6 主表。

---

## 1. 验证前置（R0，全部串行）

### 1.1 冻结基线

在执行机器上跑一遍，把输出贴进 §6 环境头：

```powershell
git -C C:\Users\26346\Desktop\project\dafors rev-parse HEAD
git -C C:\Users\26346\Desktop\project\dafors status --porcelain
Get-FileHash C:\Users\26346\Desktop\project\dafors\dist\SJZ-Control.exe -Algorithm SHA256
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m compileall -q core web desktop.py run.py tools
```

判定：`pytest` 全绿、`compileall` 无输出、`git status` 里没有未提交的代码改动。任何一项不符，先修基线再谈真机。

### 1.2 备份清单（必须全部完成）

| 对象 | 路径 | 备份方式 | 恢复方式 | 不备份的后果 |
|---|---|---|---|---|
| 配置 | `config.json` | 复制到备份目录，另存 `config.json.bak-<ts>` | 覆盖回去 | 调坏的 `input_mode`/区域/预算回滚时缺旧值 |
| 模板 | `templates\buy.png`、`templates\confirm.png` | 整目录复制 | 覆盖回去 | 换分辨率后旧模板丢失，要重框 |
| OCR 资产 | `core\assets\`（Rapid 模型、`interception.dll`） | 整目录复制 | 覆盖回去 | 打包/驱动路径失效后要重下 60MB |
| 成交记录与截图 | `data\records.jsonl`、`data\<id>\` | 整目录复制 | 覆盖回去 | 崩溃/断电后复盘重复下单时缺依据 |
| 鼠标类过滤驱动 | 注册表 `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96f-e325-11ce-bfc1-08002be10318}` | `reg export` | `reg import` | 驱动卸载失败后 UpperFilters 残留，鼠标异常 |
| 键盘类过滤驱动 | 注册表 `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96b-e325-11ce-bfc1-08002be10318}` | `reg export` | `reg import` | 同上 |
| 系统还原点 | — | `Checkpoint-Computer` | 系统还原 | 驱动装坏后只能手动修注册表 |

执行：

```powershell
$ts = Get-Date -Format yyyyMMdd_HHmmss
$bk = "C:\Users\26346\Desktop\project\dafors\_realmachine_backup\$ts"
New-Item -ItemType Directory -Force -Path $bk | Out-Null
Copy-Item C:\Users\26346\Desktop\project\dafors\config.json "$bk\config.json"
Copy-Item C:\Users\26346\Desktop\project\dafors\config.json "$bk\config.json.bak-$ts"
Copy-Item C:\Users\26346\Desktop\project\dafors\templates "$bk\templates" -Recurse
Copy-Item C:\Users\26346\Desktop\project\dafors\data "$bk\data" -Recurse
Copy-Item C:\Users\26346\Desktop\project\dafors\core\assets "$bk\core_assets" -Recurse
reg export "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96f-e325-11ce-bfc1-08002be10318}" "$bk\mouse_class.reg" /y
reg export "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96b-e325-11ce-bfc1-08002be10318}" "$bk\keyboard_class.reg" /y
Checkpoint-Computer -Description "dafors-realmachine-baseline" -RestorePointType MODIFY_SETTINGS
```

`Checkpoint-Computer` 失败（系统还原被关）要当场记下来，后面 R5 装驱动前必须先开系统还原或手工确认两个 `.reg` 备份可导入。

### 1.3 环境登记（每个被测机器一份）

| 字段 | 取值 | 命令/来源 |
|---|---|---|
| 机器名 / OS build |  | `winver`、`Get-ComputerInfo` |
| CPU / 内存 |  | `Get-CimInstance Win32_Processor` |
| 显示器数量 / 主副屏布局 |  | `Get-CimInstance Win32_VideoController`、显示设置 |
| 分辨率 / 缩放 |  | 显示设置；`Screen.size` 日志 |
| 游戏窗口模式 | 全屏 / 无边框 / 窗口化 | 游戏设置 |
| 游戏客户区尺寸 |  | 预检「游戏窗口」项 |
| 控制台权限 | 普通 / 管理员 | 预检「程序权限」项 |
| 游戏进程完整性 | 普通 / 高 | 同上 |
| 输入驱动 | 就绪 / 未就绪 | `sc query mouse`、`sc query keyboard`、`driver_ready(probe=True)` |
| OCR 引擎链 | RapidOCR / 二级服务 / 字模 | 预检「OCR 识别」项、`ocr_service_8081.log` |
| 网络 / NTP |  | 预检「时间校准」项 |

### 1.4 工具清单

| 工具 | 用途 | 命令 | 是否动真实光标 |
|---|---|---|---|
| `core/nocursor.py` 自检 | 三条点击策略的零干扰自证 | `.venv\Scripts\python.exe -m core.nocursor` | 否（message 策略） |
| `tools/bench_input.py` 安全模式 | 消息/驱动单次延迟 | `.venv\Scripts\python.exe tools\bench_input.py` | 否（消息窗 + 零位移） |
| `tools/bench_input.py` 完整模式 | 会移动光标的点击基准 | 加 `--i-understand-this-moves-the-cursor` | **是** |
| `tools/bench_precision.py` | 卡点内核抖动 | `.venv\Scripts\python.exe tools/bench_precision.py --external --shots 200` | 否 |
| `tools/bench_countdown.py` | T0 估计误差分布（离线） | `.venv\Scripts\python.exe tools/bench_countdown.py --runs 200 --json tools\bench_countdown.json` | 否 |
| `tools/ocr_eval.py` | ROI 级 OCR 评测 | `.venv\Scripts\python.exe tools\ocr_eval.py --help` 先看参数 | 否 |
| `tools/fire_worker.py` | 独立进程卡点 | 按脚本内说明 | 否（只打点） |
| `dist\SJZ-Control.exe --doctor` | 打包产物自检 | 同左 | 否 |
| `pytest tests` | 离线回归 | `.venv\Scripts\python.exe -m pytest tests -q` | 否 |

### 1.5 测试边界（写进执行记录）

- 测试账号与主力账号分开；R5 驱动阶段只在测试账号。
- 单价上限、预算上限写 `console.max_price` / `console.budget`；`stop_after_fails` 设 3；单场点击上限 300 由执行人手动计数（该项没有对应配置）。
- 出现任何封禁/警告/踢下线信号，按 `S1` 立即停。
- 录屏只录游戏窗口，不录桌面其他内容。

---

## 2. 八项验证

### V1 价格识别在真机字体/分辨率下的准确性

**目的**：确认 RapidOCR 主链在目标字体、颜色、缩放下“读对”，且读错时不会把错价拿去下单。

**依赖**：游戏窗口化 1920×1080；OCR 三级链可用；ROI 已框选。不需要点击。

**怎么测**

1. 停在交易行「在售」页，选 10 个不同皮肤/价位，各截 1 张原图（不缩放、不压缩），存 `_realmachine_shots\v1\`。必须覆盖：
   - 卡片价（白字/黑字）；
   - 详情面板红字价；
   - 筛选弹窗的价格区间（下限/上限）；
   - 个位数货币 `0`、`8`；
   - 最高价 `9,999,999`；
   - 带逗号的多位数（`1,000,000`、`150,000`、`469,999`）。
2. 人工标真值：把每个价格 ROI 放大 6~10 倍（NEAREST）逐条目视读数，填进 §6 附表。**机器输出不作为真值。**
3. 用 `tools/ocr_eval.py` 对 ROI 逐条跑 3 轮，轮间打乱顺序。
4. 每次记录：读出的值、token、score、耗时、走了哪一级（`chain stats` 的 `tier2`/`tier3`）。
5. 游戏内实跑 10 分钟，人工旁证每次读价与界面实际价是否一致。

**测几次**：30 条价格串 × 3 轮 = 90 次；单字货币 10 条 × 3 轮 = 30 次；游戏内读价 ≥50 次。

**通过标准**

| 指标 | 阈值 |
|---|---|
| 全等率 | ≥95%（86/90） |
| 误识（返回数值且≠真值，且通过千分位闸门） | **0 条** |
| 拒识率 | ≤5% |
| 单字 `0`/`8` 全等 | ≥95% |
| 单次中位耗时 | ≤35 ms |
| 单次 p95 | ≤70 ms |
| 三级降级触发率 | ≤10% |
| 游戏内读价与界面一致 | ≥48/50 |

“误识”和“拒识”必须分开统计：拒识只是慢，误识会买错价。

**失败标准**

- 出现任意 1 条“误识且会被用于下单”；
- 全等率 <90%；
- p95 >120 ms，导致倒计时采样率跌破 80 Hz；
- 同一 ROI 两轮输出不一致 >5%。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B1.1` | 拒识 >10%，或单字漏读集中在小字号 | 收紧 ROI（去掉金币图标、`+` 按钮、相邻字段），单字与货币强制 3× 放大走二级 | 重框 10 分钟；单次 +8~20 ms | 恢复旧 ROI 截图与旧区域 |
| `B1.2` | 偶发 ±1 位误识（每 100 次 ≤2） | 同 ROI 连续 3 帧读数一致才采用，不一致就拒识 | +2 帧 ≈14 ms（按 145 Hz 计）；**需改引擎** | 关闭投票开关 |
| `B1.3` | 字体/语言包变更导致全等 <80% | 改用「列表价格升序 + 首行固定位取价」，OCR 只做校验 | 需开发 1~2 天 | 修好字体环境后回 OCR |
| `B1.4` | OCR 环境整体不可用 | `semi_mode=true`，停在购买前，人工确认 | 吞吐降为人工 | 环境恢复后关半自动 |

---

### V2 倒计时归零触发的实际偏差

**目的**：测出厂 `countdown.Clock()` 路径的 `zero_at` 绝对偏差、首点时刻、归零后连点覆盖率，并给出「是否值得接线候选 `T0Estimator`」的判据。

**依赖**：V1 通过（时钟区域可读）；120 fps 录屏（OBS 或游戏内录屏，必须录进鼠标指针）。

**代码事实（执行时必须按它看日志）**：

- 出厂路径是 `countdown.Clock()`：只在读数**变化**时记一条上界 `t + S + DISPLAY_TRUNC_S`，`zero_at` 取所有上界的 `min`；它**没有 `lag_s`、没有 `trust`**。日志能看的是 `cd.info()` 的 `last`、`ticks`、`seen_zero`。
- `T0Estimator` 是模块里的候选反推器（吃每帧、吃 `lag_s`、输出 `trust/half_width`），**没有接进出厂引擎**，只在 `tools/bench_countdown.py` 离线跑。V2 顺带用它复算同一批录屏，作为“要不要接线”的依据，不改生产。
- 开点不是归零瞬间点一次：`FIRE_LEAD_S = 5.0` 秒前开始连点，`confirm_clicks = 20` × `confirm_interval_ms = 10`，直到弹窗或 `confirm_timeout_s` 超时。

因此 V2 要同时看三个量：**首点相对 T0**、**T0 后 200 ms 内的点击数**、**`zero_at` 的绝对偏差**。

**怎么测**

1. 选 25 个不同剩余时长的「公示期解锁」商品，剩余 ≥60 s 时开始录屏。
2. 录屏一屏同时包含**游戏画面 + 控制台窗口 + 鼠标指针**；帧率固定 120 fps。控制台要能看到状态行（`开点（距归零还有 Xms）` / `半自动：已停在购买按钮前`）。
3. 记下录屏起始时刻的 `perf_counter` 基准 `t_record0`，日志留档 `desktop.log`。
4. **偏移标定 3 次**（`semi_mode=true`，不点击）：逐帧读「读数跳 0 帧」与「按钮变亮帧」，差值记 `bias_ms`，不计入统计。
5. **半自动 20 次**（`semi_mode=true`，不点击）：每次记录 `zero_at`、日志「开点（距归零还有 Xms）」、控制台切到「已停在购买按钮前」的帧。这一组测触发时刻，不给游戏任何点击。
6. **实点 5 次**（`semi_mode=false`，每次单独一场）：逐帧记录 `T0_frame`、`first_click_frame`、`dialog_frame`，测首点与连点覆盖。
7. 后 25 次（20 半自动 + 5 实点）统一把日志 `zero_at` 换算到录屏帧时刻（`zero_at - t_record0` × 120），与 `T0_frame` 比较；核 `cd.info()` 的 `ticks`，`ticks < 8` 的场次单独标记。
8. 同一批录屏用 `tools/bench_countdown.py` 跑 `T0Estimator` 复算，只记录它的 `trust/half_width`，不替换生产路径。

**测几次**：标定 3 次 + 半自动 20 次 + 实点 5 次 = 28 次完整倒计时；每次采样 ≥120 帧。实点每场单独算一次（单场 ≤300 次点击）。

**通过标准**

| 指标 | 阈值 |
|---|---|
| `zero_at` 相对 `T0_frame` 的偏差 p50 | ≤10 ms |
| 同上 p95 | ≤20 ms |
| 同上 max | ≤40 ms |
| 首点不早于 `T0-5.2s` | 5/5 |
| 首点 ≤ `T0+10ms` | ≥4/5 |
| 首点 ≤ `T0+20ms` | 5/5 |
| T0 后 200 ms 内点击次数 | ≥5 次（5/5） |
| `ticks ≥ 8` 的场次 | ≥95% |
| 固定偏移 `bias_ms` | ≤±15 ms |

**失败标准**

- p95 >50 ms 或 max >100 ms；
- 任一首点晚于 `T0+20ms`，或 ≥2/5 晚于 `T0+10ms`；
- `ticks < 8` 的场次 >10%；
- 出现 1 次 `is_stale` 停更判定；
- 读到「剩余：X分Y秒」（上架剩余时间）≥1 次——直接判失败，回 V1 检查 `ban` 词与 `kind()`。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B2.1` | 连续 3 次同向固定偏移 >20 ms | 用 `fire_delay_ms` 把开点往后推对齐（该项只支持 ≥0，提前需改 `FIRE_LEAD_S` 常量） | 配置 1 分钟；改常量需重打包 10 分钟 | `fire_delay_ms=0` |
| `B2.2` | p95 >50 ms，或 `ticks < 8` 占比 >10% | 接线候选 `T0Estimator`（吃每帧、`lag_s`、`trust/half_width`），并由独立进程卡点 | **需开发并接调度器 1~2 天**；上线前重跑 V2 | 关掉，回 `Clock` |
| `B2.3` | 首点晚 20~80 ms，但 `zero_at` 准 | 加大连点窗口：`confirm_clicks` 20→30、`confirm_interval_ms` 10→8；要更早则 `FIRE_LEAD_S` 5→5.5（改常量） | 空点增多，干扰窗口 +0.5 s | 恢复默认值 |
| `B2.4` | 倒计时区域读不出或频繁停更 | 改「定时开卖」：`time_sync` + 队列 `fire_at` | 需要准确开卖时刻；配置 10 分钟 | 回倒计时路线 |

---

### V3 点击是否真的被游戏受理

**目的**：证明“注入发生了”之外，游戏逻辑确实响应了点击（弹窗、余额、库存三选一作证）。

**依赖**：V2 通过；V5-a 基线分辨率通过；测试商品可买。

**通道事实（避免测错对象）**：

- 生产可切换的 `input_mode` 只有 `interception`（驱动）和 `legacy`（`SetCursorPos` + `PostMessage`，带 40/60/50 ms 固定 sleep）。
- `input_mode="interception"` 在驱动不可用时会**静默降级**成 `legacy`。每次测试必须记录实际生效通道（引擎日志的 `eff`/`mouse.mode`），只看配置会把 legacy 的结果误记成驱动。
- 两个入口启动时都会 `_elevate_if_needed`：检测到游戏完整性更高就用管理员身份重开自己。测试要记录“是否触发自动提权、UAC 是否通过”；UAC 被拒后仍是 Medium，会回到 UIPI 静默丢弃。
- 纯 `message`（零干扰）目前只存在于 `core/nocursor.py` 的实验策略，**没有接进设置页**。所以要测“游戏受理不受理纯消息”，用 `nocursor` 探针或临时脚本，不以改配置的方式测。

**怎么测**

1. **纯消息子测（20 次）**：用 `core/nocursor.py` 的 `message` 策略对准购买按钮，连点 20 次，间隔 0.5 s。记录每次是否出现弹窗/余额变化。预期：窗口消息层 20/20 收到，游戏逻辑大概率 0/20。
2. **驱动子测（20 次）**：`input_mode="interception"`，同样 20 次。记录弹窗/余额/库存。
3. **legacy 子测（10 次）**：`input_mode="legacy"`，只做对照，不追求速度。
4. 每次点击后 1 s 内检查三件证据，按优先级取一条：
   - 确认弹窗模板命中（`confirm.png`，阈值 `threshold=0.85`）；
   - 余额/货币数量变化；
   - `data\records.jsonl` 新增 `hit` 记录。
5. 三次连续失败即停，记录失败时刻的预检项、前台窗口、权限、光标位置。

**测几次**：message 20 次 + 驱动 20 次 + legacy 10 次 = 50 次；每种通道至少 2 轮（不同商品），共 100 次以内。

**通过标准**

| 通道 | 受理率 | 附加条件 |
|---|---|---|
| 纯 message | 只记录，不作放行依据 | 若 ≥18/20，可直接选零干扰路径 |
| 驱动 `interception` | ≥18/20（90%） | 弹窗/余额/库存至少 1 项可证 |
| legacy | ≥16/20 | 仅作兜底，不作为主路径 |

**失败标准**

- 驱动通道 <16/20，或连续 3 次失败；
- 弹窗出现但 `records.jsonl` 没有 `hit`（说明确认逻辑与游戏实际不一致）；
- 同一坐标在窗口化下成功、全屏下 0/20（说明是窗口模式问题，转 `B3.1`）。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B3.1` | 全屏/独占全屏下 0/20，窗口化下正常 | 游戏改「无边框窗口」或「窗口化」，重采模板 | 重启游戏、画质/帧率变化、重框 10 分钟 | 改回原窗口模式 |
| `B3.2` | 某通道受理率 <50%，其他通道正常 | 按 message → 驱动 → SendInput 顺序换通道；驱动需先过 V6 | 重启控制台；驱动有安装/重启成本 | 回上一通道 |
| `B3.3` | 受理率 50~80%，且失败集中在按钮边缘 | 坐标从“区域中心”改为 3×3 网格（±6 px）轮询，或模板匹配中心 | 点击次数 ×3，干扰窗口变长 | 回单点中心 |
| `B3.4` | 鼠标通道整体 <50%，或反作弊拦鼠标注入 | 改键盘导航：Tab 切焦点 + Space/Enter 确认 | 需开发并实测键序；键盘注入同样可能被拦 | 回鼠标通道 |
| `B3.5` | 上述全部 <50%，或风险不可接受 | `semi_mode=true`，脚本停在购买前，人工点最后一下 | 吞吐降为人工，做不到规模化 | 通道恢复后关半自动 |

---

### V4 光标干扰窗口的体感与实测

**目的**：确认“受理路径”对用户鼠标的干扰在可接受范围，且异常时不留按下状态、不把光标丢在目标点。

**依赖**：V3 已选出可用通道；独立采样进程（200 Hz 读 `GetCursorPos`）。

**怎么测**

1. 起一个独立采样进程，200 Hz 记录光标坐标与时间戳，写 `_realmachine_shots\v4\cursor_<ts>.csv`。
2. 在测试商品上做 3 组 × 200 次点击（组间停 30 s），另做 20 次单点。
3. 每次点击记录：调用发出、光标离开原位、光标回到原位、`WM_LBUTTONDOWN` 到达（若有观测窗）。
4. 人工体感：10 轮 × 20 次点击，每轮结束填 1~5 分（1=明显被抢，5=几乎无感），并记录“是否出现按住不放/光标停在别处”。
5. 检查 `interception_get_filter` 全设备为 0（物理输入透传），检查进程内没有常驻钩子。

**测几次**：自动 620 次（3 组 × 200 + 20 单点）+ 人工 200 次 = 820 次点击；自动分 4 场、人工 1 轮完成。

**通过标准**

| 指标 | 阈值 |
|---|---|
| 单次离开原位 p50 | ≤1.0 ms |
| 单次离开原位 p95 | ≤2.0 ms |
| 单次离开原位 max | ≤20 ms |
| 点击结束后 1 s 光标仍在目标点 | 0 次 |
| 残留按下（`GetAsyncKeyState(VK_LBUTTON)` 为按下且本进程不在点） | 0 次 |
| 物理鼠标可用（结构：filter=0、无钩子、无独占） | 必须成立 |
| 人工体感评分 ≥4 的比例 | ≥8/10 轮 |

**失败标准**

- 任意 1 次残留按下，或光标 1 s 后仍在目标点；
- max >50 ms；
- 人工评分 ≤2 的轮次 ≥3/10；
- 采样期间物理鼠标事件被吞（`filter` 非 0，或用户报告移动无效）。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B4.1` | p95 >3 ms，但受理正常 | 压缩窗口：去掉不必要的 settle，单次只发 move+down+up；确认“先落地再还原” | 需调参与复测；还原过早会把光标留在目标点 | 恢复原 settle |
| `B4.2` | 人工评分 ≤3 但实测达标 | 约定操作模式：开抢前 10 s 用户放手，界面显示倒计时与“勿动鼠标” | 依赖用户配合，无硬保证 | 取消提示 |
| `B4.3` | 用户要求绝对零干扰 | 切 `message` 策略（若 V3 子测受理率 ≥90%） | 受理率可能归零，等于放弃抢购 | 回驱动路径 |
| `B4.4` | 长时间持续干扰 | 只在 `[T0-5s, T0+0.5s]` 启用注入，其余时间不碰鼠标 | 需把注入开关接到卡点窗口；窗口内仍会干扰 | 回常开 |

---

### V5 分辨率 / DPI / 副屏适配

**目的**：确认 `screen.base_size` 的比例判定、`monitor` 选择、`SetProcessDpiAwareness(2)` 在目标机器上让坐标与截图对齐。

**依赖**：V1/V3 在 1920×1080 基线通过；每换一种显示配置都要重采模板。

**怎么测**

按矩阵逐格执行，每格做：预检 1 次 + OCR 10 条 + 点击 10 次 + 落点核对。

| 格 | 配置 | 关键检查 |
|---|---|---|
| A | 1920×1080，100%，主屏，窗口化 | 基线 |
| B | 2560×1440，100%，主屏，窗口化 | 等比放大，改 `base_size` |
| C | 2560×1440，125%/150% 缩放 | DPI 感知是否按物理像素 |
| D | 1920×1080，副屏（有负坐标或右侧偏移） | `monitor` 编号与虚拟屏偏移 |
| E | 全屏 / 无边框 / 窗口化 三模式 | 与 V3 的窗口模式结论交叉 |

落点核对方法：点击后 200 ms 内读光标坐标，与目标区域中心比较；同时看是否出现确认弹窗。

**测几次**：5 格 × 3 轮 = 15 组；每格切换后重启游戏与控制台各 1 次。

**通过标准**

| 指标 | 阈值（1080p 基线 / 1440p） |
|---|---|
| 预检「游戏窗口」 | 通过（允许仅 `warn`） |
| 落点偏差 | ≤8 px / ≤12 px |
| OCR 全等 | ≥90% |
| 点击受理 | ≥8/10 |
| 切换分辨率后模板匹配分 | ≥0.85 |

**失败标准**

- 预检红且提示尺寸不符；
- 落点偏差 >16 px，或点击落到相邻卡片；
- 副屏抓到的画面不是目标屏（幽灵屏/错屏）；
- 125%/150% 缩放下截图与坐标整体偏移 >2%。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B5.1` | 换等比分辨率后偏差 | 改 `screen.base_size=[W,H]`，`tolerance` 保持 0 | 5 分钟 + 重采模板 | 改回旧值 |
| `B5.2` | 非 16:9、DPI 异常、缩放导致坐标不稳 | 游戏强制窗口化 1920×1080 | 重启游戏、帧率下降 | 回原分辨率 |
| `B5.3` | 副屏/负坐标偏差 | 设 `monitor=2`（或 `0` 虚拟屏），确认区域按该屏坐标采集 | 10 分钟，可能重采全部区域 | 回主屏单屏 |
| `B5.4` | 模板分 <0.85 | 每台机器重新框选 `buy.png`/`confirm.png` | 10 分钟/机器 | 回旧模板（仅同分辨率同机器） |

---

### V6 反作弊是否拦截输入或驱动

**目的**：确认游戏不因注入方式/驱动而拦截输入、踢下线或封禁；区分“输入被拦”与“坐标错/窗口模式错”。

**依赖**：V3/V4 已选定路径；测试账号；隔离机器（R5 驱动阶段必须）。

**怎么测**

1. **无驱动观察**：用 V3 通过的通道（优先 message 或 legacy），3 场 × 30 分钟，累计 ≤300 次点击，记录每次受理结果。
2. 每场开始/结束各查一次：
   - `sc query mouse`、`sc query keyboard` 是否仍 `RUNNING`；
   - 游戏是否弹出“环境异常/非法程序/驱动冲突”；
   - 是否被踢下线、是否收到警告邮件/站内信。
3. **驱动观察**（仅 R5）：装驱动并重启后重复上述 3 场，另加：
   - `driver_ready(probe=True)` 是否仍 `(True, "")`；
   - 游戏启动前后 `UpperFilters` 是否被改动；
   - 驱动服务是否被反作弊停止。
4. 失败时立刻保存：游戏日志、系统事件查看器（System/Application）、`desktop.log`、截图。

**测几次**：无驱动 3 场 + 驱动 3 场 = 6 场 × 30 分钟；累计点击 ≤600 次。

**通过标准**

| 指标 | 阈值 |
|---|---|
| 踢下线 / 封禁 / 警告 | 0 次 |
| “环境异常/非法程序”提示 | 0 次 |
| 驱动服务被停 | 0 次 |
| 受理率相对 V3 基线下降 | ≤10% |
| 游戏正常启动 | 6/6 场 |

**失败标准**

- 任意 1 次封禁/警告/踢下线；
- 驱动服务被停或 `UpperFilters` 被改；
- 游戏起不来（反作弊直接拦驱动）；
- 受理率下降 >30% 且归因不到坐标/窗口模式。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B6.1` | 驱动路径出现 1 次异常信号 | 卸载驱动，回 message/legacy | 卸载 + 重启 10 分钟；受理率可能下降 | 重装驱动（需再重启） |
| `B6.2` | 高频连点被限流/检测 | `click_hz` 50→20，`confirm_clicks` 20→8，拉长间隔 | 覆盖率下降，可能错过窗口 | 恢复默认频率 |
| `B6.3` | 出现警告但未封 | 换测试账号/隔离机器，降低单场量到 50 次 | 新号/新机准备时间 | 回原账号（风险自负） |
| `B6.4` | 封禁风险不可接受 | 停用自动点击，只保留半自动与 OCR 辅助 | 产品价值下降 | 风险策略调整后再评估 |

---

### V7 打包产物在干净机器上能否启动

**目的**：确认 `dist\SJZ-Control.exe` 在没有 Python、没有 `.venv`、没有开发工具的机器上能起界面、能过预检、能离线 OCR。

**依赖**：`build.bat` 产物；3 台干净机器/干净虚拟机。

**怎么测**

1. 三台机器：Win10 22H2 x64、Win11 23H2/24H2、一台未装 Python/未装 uv 的机器。
2. 每台执行：
   - 把 `dist\SJZ-Control.exe` 放到**非 Program Files、非中文路径**的目录（如 `D:\SJZ\`），双击启动 3 次（冷启动）；
   - 跑 `SJZ-Control.exe --doctor`；
   - 断网启动 1 次，确认 RapidOCR 离线可用；
   - 打开设置页，确认模板/OCR/驱动三项给出的提示与缺失项一致。
3. 记录启动耗时、窗口是否空白、`desktop.log` 是否有 `ModuleNotFoundError`/`FileNotFoundError`。
4. 杀软（Defender 默认开启）是否拦截/隔离 exe，单独记一行。

**测几次**：3 台 × 3 次冷启动 = 9 次；每台 1 次断网启动。

**通过标准**

| 指标 | 阈值 |
|---|---|
| 界面出现 | 9/9，≤15 s |
| `--doctor` 正常退出 | 9/9，输出含 `OCR 可用 = True`、`配置目录可写 = True` |
| 缺资产报错（`bricks.json`/`skins_raw.csv`/Rapid 模型/`interception.dll`） | 0 次 |
| 离线 RapidOCR | 3/3 可用 |
| 预检提示 | 与实际缺失项一致，无“全绿但跑不动” |
| 杀软隔离 | 0 次（误报要记录并处理） |

**失败标准**

- 启动崩溃、白窗、10 s 内无界面；
- `ModuleNotFoundError: rapidocr` / `onnxruntime` / `webview`；
- 断网后 OCR 不可用；
- 预检全绿但点击时缺模板/缺区域；
- Defender 直接隔离 exe。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B7.1` | exe 起不来但源码可跑 | 源码模式 `run.py`/`start.bat` 兜底 | 需要 Python + uv，终端用户门槛高 | 修好打包后回 exe |
| `B7.2` | 白窗/缺运行库 | 装 WebView2 Runtime、VC++ 2015-2022 x64 | 10 分钟/机器 | 回 exe |
| `B7.3` | 权限/路径问题 | 放非 Program Files、非中文路径、给目录写权限 | 用户操作说明 1 页 | 回默认目录 |
| `B7.4` | 原因不明 | 用 `--doctor` + `desktop.log` 定位，必开 `console=False` 下的日志落盘 | 10 分钟 | 回上一版构建 |

---

### V8 崩溃、断电、进程被杀后的回退状态

**目的**：确认异常退出后不残留按下状态、不丢配置、不重复下单、能单实例重启。

**依赖**：§1.2 备份完成；测试账号；可断电的测试机或虚拟机。

**怎么测**（6 个场景，逐个做，不并行；C3 优先在虚拟机上做，物理机只在备份可恢复时做 1 次）

| 场景 | 操作 | 观察点 |
|---|---|---|
| C1 空闲被杀 | 任务空闲时 `Stop-Process -Name SJZ-Control -Force` | 互斥量释放、重启 ≤15 s、无残留进程 |
| C2 连点中被杀 | 连点窗口内强杀 | 左键是否残留、光标是否在目标点、`records.jsonl` 最后一行是否截断 |
| C3 断电 | 任务运行中硬断电/强制关机 | `config.json` 是否完整、`records.jsonl` 是否可读、重启后是否重复下单 |
| C4 写配置时被杀 | 改设置触发保存的同时强杀 | `config.json` 是否合法 JSON，是否生成 `.broken-<ts>.bad` |
| C5 双开 | 连续启动两个实例 | 第二个是否被互斥量拦住，端口是否漂移 |
| C6 断网 | 抢购中拔网线/断 Wi-Fi | OCR 服务/校时是否降级，是否卡死，恢复后能否继续 |

每个场景做完，执行 §5.3 的恢复检查再进下一个。

**测几次**：C1/C2/C3/C5/C6 各 2 次，C4 3 次（保存窗口短，需多次撞），共 13 次。

**通过标准**

| 指标 | 阈值 |
|---|---|
| 重启到界面 | ≤15 s |
| 左键残留 | 0 次（`GetAsyncKeyState(VK_LBUTTON)` 松开） |
| 光标 1 s 后仍在目标点 | 0 次 |
| `config.json` 可解析或可恢复 | 13/13；损坏时必须存在 `.broken-<ts>.bad` 且恢复成功 |
| 重复下单 | 0 次（以 `records.jsonl` 与游戏库存为准） |
| 双开 | 第二实例被拦住，端口不漂移 |
| 断网恢复 | 恢复网络后 60 s 内 OCR/校时回到可用 |

**失败标准**

- 任意 1 次左键残留或光标留在目标点；
- `config.json` 损坏且没有备份；
- 重启后重复下单；
- 双开成功（两个实例同时在跑）；
- 断网后进程卡死，恢复网络也不恢复。

**备选方案**

| 编号 | 触发条件 | 动作 | 切换成本 | 回退 |
|---|---|---|---|---|
| `B8.1` | 残留按下/光标未还原 | 启动自愈：`release_cursor_lock`、清零 pressed、100 ms 看门狗补 UP | 看门狗尚未接线，需开发；本方案内先手工补 UP | 关闭看门狗 |
| `B8.2` | 配置损坏 | 自动/手工恢复 `.broken-<ts>.bad` 或 `config.json.bak-<ts>` | 1 分钟 | 回默认配置 |
| `B8.3` | 怀疑重复下单 | 用 `data\records.jsonl` + `data\<id>\buy.png` 回放核对 | 10 分钟/次 | 无 |
| `B8.4` | 无人值守时进程死亡 | 外部看门狗脚本/计划任务拉起，或改回人工值守 | 需部署脚本 | 关闭计划任务 |

---

## 3. 预估可能出现的问题（按概率×影响排序）

概率：高=3 / 中=2 / 低=1；影响：致命=3（整条链路不可用）/ 高=2（功能受损）/ 中=1（体验或效率受损）。排序按乘积。

| 排名 | 编号 | 问题 | 概率 | 影响 | 症状 | 30 秒判断 | 对应备选 |
|---|---|---|---|---|---|---|---|
| 1 | `P1` | 游戏只认 Raw Input，纯消息点了没反应 | 高 | 致命 | 窗口消息 20/20 收到，游戏 0/20 响应 | 对照 V3 子测：message 0/20、驱动正常 → 消息路径不可用 | `B3.2`、`B3.1`、`B3.4` |
| 2 | `P2` | UIPI：普通权限进程向高完整性前台发输入被静默丢弃 | 高 | 致命 | 调用返回成功、游戏无反应；`SetCursorPos` 返回 0；预检「程序权限」红；或自动提权时 UAC 被拒 | 预检看“控制台普通 / 游戏管理员”；看日志 `elevate_self`；`GetLastError=0` 但光标不动 | `B3.2`（驱动走内核不受 UIPI）、提权重开（会真抢光标） |
| 3 | `P3` | 驱动签名/反作弊拦截，或干净机器装不上 | 高 | 致命 | `install-interception` 报写盘失败；`sc query` 非 RUNNING；游戏起不来；被踢 | `sc query mouse/keyboard`；`Get-AuthenticodeSignature`；游戏日志 | `B6.1`、`B6.3`、`B6.4`；分发侧回 `B3.2` |
| 4 | `P4` | 分辨率/DPI/副屏导致坐标整体偏移 | 高 | 高 | 点击稳定落在按钮外；预检尺寸红；截图与坐标错位 | 预检「游戏窗口」；落点与区域中心差 >16 px | `B5.1`–`B5.4` |
| 5 | `P5` | Clock 上界不准 / 界面停更，T0 偏几百毫秒 | 高 | 高 | 总是早/晚固定一截；`ticks` 长期 <8；读数长时间不变 | 日志 `cd.info()` 的 `last/ticks`；录屏对比 `zero_at` 与 `T0_frame` | `B2.1`、`B2.2`、`B2.3` |
| 6 | `P6` | OCR 小字/红字/单字拒识或误识 | 中 | 致命 | 价格读成 `9,9`/`1,888,888`，或空输出 | 看 `tier2/tier3` 触发率与 `price_from_text` 拒识日志 | `B1.1`–`B1.4` |
| 7 | `P7` | 打包缺资产：`bricks.json`/`skins_raw.csv`/Rapid 模型/`interception.dll` | 中 | 致命 | exe 白窗、OCR 不可用、皮肤库空 | `--doctor`；`desktop.log` 的 `FileNotFoundError` | `B7.1`–`B7.4` |
| 8 | `P8` | 驱动注入异步，还原早于落地，光标停在目标点 | 中 | 高 | 点完鼠标被丢在屏幕另一边 | V4 采样：1 s 后光标仍在目标点 | `B4.1`、`B8.1` |
| 9 | `P9` | 崩溃/断电后配置损坏或残留按下 | 中 | 高 | 重启读默认配置；鼠标一直按住 | 检查 `config.json.broken-*`、`GetAsyncKeyState` | `B8.1`、`B8.2` |
| 10 | `P10` | 连点过快被游戏限流/反作弊标记 | 中 | 高 | 前几下有效，后面全无反应；收到警告 | 降频后复测 20 次对比受理率 | `B6.2`、`B6.3` |
| 11 | `P11` | OCR 服务冷启动/端口占用，预检长时间“正在启动” | 中 | 中 | 预检 OCR 黄/红；`ocr_service_8081.log` 报端口占用 | `netstat -ano` 查 8081 监听；看日志最后 20 行 | `B7.4`、`B1.4` |
| 12 | `P12` | 抢前台失败/焦点被别的窗口抢走 | 中 | 中 | 点击投递到控制台或别的窗口 | 预检「游戏页面」；`Mouse.foreground()` 对照 | `B3.1`、`B4.4` |
| 13 | `P13` | 模板在换分辨率/换机器后失效 | 中 | 中 | 确认弹窗识别不到，`threshold` 一直低于 0.85 | 模板匹配分数日志；重截模板对比 | `B5.4` |
| 14 | `P14` | 副屏负坐标/虚拟屏偏移 | 低 | 中 | 副屏点击落在主屏；抓屏抓错屏 | `Screen.monitors` 打印；`monitor` 配置 | `B5.3` |
| 15 | `P15` | 多开/端口漂移（8777→8780） | 低 | 中 | 两个窗口、两份日志、重复下单 | 任务管理器进程数；`netstat` 端口 | `B8.4`、`B8.2` |

### 3.1 前三个问题的判断决策树

```text
点了没反应
 ├─ 预检「程序权限」红（控制台普通 / 游戏管理员）
 │    └─ UIPI（P2）→ 提权重开（会真抢光标）或走驱动（内核不受 UIPI）
 ├─ 预检绿
 │    ├─ message 0/20，驱动 ≥18/20 → 游戏只认 Raw Input（P1）→ 用驱动
 │    ├─ message 0/20，驱动 0/20
 │    │    ├─ 落点偏差 >16px → P4 → 先修分辨率/坐标
 │    │    └─ 落点正确 → 窗口模式（B3.1）→ 通道（B3.2）→ 键盘（B3.4）
 │    └─ 驱动就绪但受理 0/20 → 查反作弊是否停驱动（P3）→ B6.1/B6.3
 └─ 有弹窗但记录无 hit → 确认模板/确认逻辑问题 → 重截 confirm 模板（B5.4）
```

---

## 4. 验证执行顺序

### 4.1 阶段表

| 阶段 | 内容 | 串/并行 | 前置 | 产出 |
|---|---|---|---|---|
| `R0` | 基线冻结、备份、环境登记、离线测试 | 串行 | 无 | §1 全部完成 |
| `R1` | V7 干净机打包 ∥ V1 离线截图 ∥ V2 离线模拟 ∥ V5 坐标静态核对 | 并行 | R0 | V7/V1/V2 的离线部分 |
| `R2` | V5-a 基线 → V1 游戏内 → V2 标定+统计 → V3 受理 → V4 干扰 | 串行 | R1 | 主链路放行结论 |
| `R3` | V5-b/c 分辨率/DPI/副屏，V6 无驱动观察 | V5-b 与 V6 可并行（不同会话）；V5 各格串行 | R2 | 适配矩阵与反作弊基线 |
| `R4` | V8 崩溃/断电/双开/断网 | 串行 | R3 | 回退能力结论 |
| `R5` | 装驱动 → 重启 → V6 驱动观察 → V3/V4 子集复验 | 串行，最后 | R4 + 备份/还原点 | 驱动路径最终结论 |

### 4.2 为什么是这个顺序

1. **先便宜后昂贵**：离线与打包验证不碰游戏、不碰鼠标，失败成本最低，能最早排除“环境根本不对”。
2. **先只读后写入**：V1/V2 只读屏幕，V3 才开始点击，V4 才测干扰。受理不通过时，测干扰没有意义。
3. **先基线后变体**：V5 必须先在 1920×1080 基线通过，再测 2K/DPI/副屏；否则偏移原因分不清是分辨率还是代码。
4. **破坏性放最后**：V8 会强杀进程、断电、写坏配置，必须在功能验证完成后做，且每场后按 §5 恢复。
5. **驱动最后**：装驱动会改 `UpperFilters`、需要重启、暴露给反作弊，卸载也要重启；它必须是“其他路径都不行”时才启用的增强项。

### 4.3 可并行 / 必须串行

**可并行**

- V7（3 台干净机）与 V1 离线截图标注：互不依赖。
- V2 离线模拟（`bench_countdown.py`）与 V1：都不碰游戏输入。
- V5-b/c（第二台机器或第二块屏）与 V6 无驱动观察：不同会话、不同账号。
- V6 的被动观察可以挂在 R3 的其他测试上，但单场点击量要单独计数。

**必须串行**

- R0 全部（备份没完成，后面都免谈）。
- V1 → V2 → V3 → V4（前一项是后一项的前置）。
- V5 基线 → 变体（每换分辨率都要重采模板）。
- V8 六个场景之间（会互相污染状态）。
- R5 驱动安装、重启、卸载（系统级状态变更）。

### 4.4 执行批次

- **批次 1**：R0 + R1 + R2 的 V5-a / V1 / V2。
- **批次 2**：R2 的 V3 / V4 + R3。
- **批次 3**：R4；R5 仅在 V3 判定消息路径不可用、且驱动为唯一可行路径时执行。

---

## 5. 回退与止损

### 5.1 全局止损条件（S1–S6）

| 编号 | 触发 | 动作 |
|---|---|---|
| `S1` | 账号出现封禁/警告/踢下线 | 立即停；卸载驱动；换测试账号；保留全部日志与截图 |
| `S2` | 蓝屏、驱动崩溃、系统不稳定 | 立即停；卸载驱动；按 §5.4 回滚；必要时系统还原 |
| `S3` | 连续 3 次点击不受理 | 停；按 §3.1 决策树定位；未定位前不继续点击 |
| `S4` | 发生一次“误识价格导致下单” | 停；回 V1；在修好前 `semi_mode=true` |
| `S5` | 用户连续 3 场报告“鼠标失控/按住不放” | 停；回 V4；未修好前改半自动 |
| `S6` | 配置或记录损坏且无备份 | 停；人工恢复；恢复前不启动任务 |

### 5.2 单项失败的回退状态

| 项 | FAIL 后系统回到什么状态 | 用户怎么恢复 | 需要保留的证据 |
|---|---|---|---|
| V1 | `semi_mode=true`，停止自动下单；保留旧 ROI | 设置页重新框选价格 ROI；必要时换 3× 放大 | 截图、ROI 坐标、OCR 输出表 |
| V2 | 保留 `Clock` + 5 s 提前连点；固定偏移用 `fire_delay_ms` 往后再对齐 | 重做偏移标定；或改定时开卖 | 录屏、`zero_at` 日志、`cd.info()` 的 `ticks` |
| V3 | `input_mode="legacy"` 或停自动；`semi_mode=true` | 换窗口模式/通道；按 §3.1 决策树 | 每次点击的通道、坐标、受理证据 |
| V4 | 停自动点击，改半自动；或只在 `[T0-5s,T0+0.5s]` 注入 | 手工点最后一下；或接受更大干扰窗口 | 光标采样 CSV、体感评分表 |
| V5 | 回 1920×1080 窗口化 + 旧 `base_size` + 旧模板 | 显示设置改回；设置页重采模板 | 每格落点偏差、模板分 |
| V6 | 卸载驱动；`input_mode="legacy"`；停自动 | 换测试账号/机器；降低频率 | 游戏日志、事件查看器、服务状态 |
| V7 | 产物标记“不可分发”；源码模式兜底 | 按 `--doctor` 补运行库/资产 | `desktop.log`、机器矩阵 |
| V8 | 恢复配置备份；清理残留按下/光标；重启 | 按 §5.3 手工恢复 | `config.json`、`records.jsonl`、截图 |

### 5.3 恢复手册（可直接复制执行）

**恢复配置**

```powershell
$bk = "C:\Users\26346\Desktop\project\dafors\_realmachine_backup\<ts>"
Copy-Item "$bk\config.json" C:\Users\26346\Desktop\project\dafors\config.json -Force
```

**配置损坏时**

```powershell
Get-ChildItem C:\Users\26346\Desktop\project\dafors\config.json.broken-*.bad
# 选最近一个人工比对；或直接用 §1.2 的备份覆盖
```

**清残留按下状态**（左键若被脚本按住）

```powershell
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class U {
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  public static void Up(){ mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero); }
}
"@
[U]::Up()
```

**解除光标裁剪锁**（游戏独占全屏退出后光标仍被限制）

```powershell
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class C {
  [DllImport("user32.dll")] public static extern bool ClipCursor(IntPtr r);
  public static void Free(){ ClipCursor(IntPtr.Zero); }
}
"@
[C]::Free()
```

**杀进程与单实例**

```powershell
Stop-Process -Name SJZ-Control -Force -ErrorAction SilentlyContinue
Stop-Process -Name pythonw -Force -ErrorAction SilentlyContinue
# 互斥量随进程退出由内核释放；重启后应能正常启动
```

**记录文件损坏**

```powershell
# records.jsonl 是追加写，最后一行可能截断；读的时候跳过最后一行即可
Get-Content C:\Users\26346\Desktop\project\dafors\data\records.jsonl -Tail 5
# 需要回滚就用备份覆盖
Copy-Item "$bk\data\records.jsonl" C:\Users\26346\Desktop\project\dafors\data\records.jsonl -Force
```

### 5.4 驱动卸载与回滚（R5 专用）

```powershell
# 1) 卸载（管理员）
C:\Users\26346\Desktop\project\dafors\tools\interception\install-interception.exe /uninstall
# 2) 重启
Restart-Computer
# 3) 重启后核对：UpperFilters 应只剩类驱动
(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96f-e325-11ce-bfc1-08002be10318}").UpperFilters
(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96b-e325-11ce-bfc1-08002be10318}").UpperFilters
sc query mouse
sc query keyboard
```

判定：鼠标类 `UpperFilters` 只剩 `mouclass`，键盘类只剩 `kbdclass`，`sc query` 报服务不存在或 STOPPED。

若卸载后仍残留：

```powershell
$bk = "C:\Users\26346\Desktop\project\dafors\_realmachine_backup\<ts>"
reg import "$bk\mouse_class.reg"
reg import "$bk\keyboard_class.reg"
Restart-Computer
```

### 5.5 普通用户视角的三步恢复

1. **鼠标被按住/光标乱跳**：先按一次物理左键；再按 `F8` 急停；仍不恢复就注销当前用户或重启。
2. **程序打不开/白窗**：看同目录 `desktop.log` 最后 30 行；把 `config.json` 改名为 `config.json.broken`，重启生成默认配置。
3. **抢购点不动**：先把游戏切到前台并确认是「公示期」页；再看设置页预检的红项，按提示修一项再试。

---

## 6. 验证记录模板

### 6.1 环境头（每台机器/每轮填一次）

| 字段 | 填写 |
|---|---|
| 执行日期 / 执行人 |  |
| commit / exe SHA256 |  |
| 机器 / OS build |  |
| CPU / 内存 |  |
| 分辨率 / 缩放 / 显示器数 |  |
| 游戏窗口模式 / 客户区尺寸 |  |
| 游戏版本 |  |
| 控制台权限 / 游戏完整性 |  |
| 输入通道 / 驱动状态 |  |
| OCR 引擎链 / 二级服务状态 |  |
| 网络 / 校时偏差 |  |
| 备份目录 |  |
| 系统还原点 | 已建 / 未建（原因） |

### 6.2 主表（每项一行；子项另起行）

| 编号 | 项目 | 方法（一句话+命令/文件） | 预期 | 实测 | 结论（PASS/FAIL） | 用了哪个备选 | 数据文件 |
|---|---|---|---|---|---|---|---|
| V1 | 价格识别 | 30 条 × 3 轮，`tools/ocr_eval.py` | 全等 ≥95%，误识 0 |  |  |  | `_realmachine_shots\v1\` |
| V1-a | 单字 `0`/`8` | 10 条 × 3 轮，3× 放大 | ≥95% |  |  |  |  |
| V2 | 倒计时偏差 | 3 偏移 + 20 半自动 + 5 实点，120 fps 录屏 | p95 ≤20 ms，首点 ≤T0+10ms |  |  |  | `_realmachine_shots\v2\` |
| V2-a | 连点覆盖率 | 5 次实点，T0 后 200 ms 点击数 | ≥5 次（5/5） |  |  |  |  |
| V3 | 点击受理 | message 20 + 驱动 20 + legacy 10 | 驱动 ≥18/20 |  |  |  | `data\records.jsonl` |
| V4 | 光标干扰 | 620 自动 + 200 人工 | p95 ≤2 ms，残留 0 |  |  |  | `_realmachine_shots\v4\cursor_*.csv` |
| V5 | 分辨率/DPI/副屏 | 5 格 × 3 轮 | 偏差 ≤8/12 px，受理 ≥8/10 |  |  |  | `_realmachine_shots\v5\` |
| V6 | 反作弊 | 无驱动 3 场 + 驱动 3 场 | 0 封禁/警告/踢 |  |  |  | 游戏日志、事件查看器 |
| V7 | 干净机启动 | 3 台 × 3 次 + 断网 1 次 | 9/9 起界面，`--doctor` 正常且 OCR 可用 |  |  |  | `desktop.log` |
| V8 | 崩溃/断电 | 6 场景 × 2~3 次 | 重启 ≤15 s，重复下单 0 |  |  |  | `config.json*`、`records.jsonl` |

### 6.3 原始数据附表

**V1 逐条**

| 图号 | ROI | 真值 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 走了哪级 | 中位 ms | 备注 |
|---|---|---|---|---|---|---|---|---|

**V2 逐次**

| 序号 | 商品/剩余 | `zero_at` | `T0_frame` | 偏差 ms | 首点帧 | 首点偏差 ms | T0+200ms 点击数 | `ticks` | 备注 |
|---|---|---|---|---|---|---|---|---|---|

**V3 逐次**

| 序号 | 配置通道 | 实际生效通道 | 坐标 | 受理证据（弹窗/余额/库存） | 结果 | 备注 |
|---|---|---|---|---|---|---|

**V4 分布**

| 组 | n | p50 | p95 | max | 残留按下 | 光标留在目标 | 体感均分 |
|---|---|---|---|---|---|---|---|

**V5 矩阵**

| 格 | 分辨率/缩放/屏 | 预检 | 落点偏差 px | OCR 全等 | 受理 | 模板分 |
|---|---|---|---|---|---|---|

**V6 时间线**

| 场次 | 通道 | 时长 | 点击数 | 踢/警告 | 驱动服务 | 受理率 | 备注 |
|---|---|---|---|---|---|---|---|

**V7 机器矩阵**

| 机器 | OS | Python/uv | 启动 | `--doctor` | 离线 OCR | 杀软 | 备注 |
|---|---|---|---|---|---|---|---|

**V8 场景矩阵**

| 场景 | 次数 | 重启耗时 | 残留按下 | 光标异常 | 配置 | 重复下单 | 备注 |
|---|---|---|---|---|---|---|---|

### 6.4 放行结论（Go / No-Go）

| 条件 | 结论 |
|---|---|
| V1–V8 全部 PASS，或 FAIL 项已启用备选并复测 PASS | **Go**：可作为候选版本 |
| 任意 1 项“致命”FAIL 且备选未复测通过 | **No-Go**：不进入分发 |
| V6 出现封禁/警告 | **No-Go**：无论其他项结果 |
| V8 出现重复下单 | **No-Go**：先修幂等与回退 |
| 驱动路径失败但 message/legacy 路径通过 | **Conditional Go**：只分发无驱动路径，驱动标“可选增强，未验证” |

最终签字：执行人 ______　复核人 ______　日期 ______

---

## 附录 A 命令速查

```powershell
# 基线
git -C C:\Users\26346\Desktop\project\dafors rev-parse HEAD
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m compileall -q core web desktop.py run.py tools

# 打包产物
dist\SJZ-Control.exe --doctor
Get-FileHash dist\SJZ-Control.exe -Algorithm SHA256

# 点击链路
.venv\Scripts\python.exe -m core.nocursor
.venv\Scripts\python.exe tools\bench_input.py
.venv\Scripts\python.exe tools\bench_input.py --i-understand-this-moves-the-cursor

# 卡点
.venv\Scripts\python.exe tools\bench_precision.py --external --shots 200
.venv\Scripts\python.exe tools\bench_countdown.py --runs 200 --json tools\bench_countdown.json

# 驱动
sc query mouse
sc query keyboard
(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96f-e325-11ce-bfc1-08002be10318}").UpperFilters
(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e96b-e325-11ce-bfc1-08002be10318}").UpperFilters
.venv\Scripts\python.exe -c "from core.interception import driver_ready; print(driver_ready(probe=True))"

# 日志
Get-Content desktop.log -Tail 50
Get-Content ocr_service_8081.log -Tail 30
netstat -ano | findstr 8081

# 进程/端口
Get-Process SJZ-Control,pythonw -ErrorAction SilentlyContinue
netstat -ano | findstr 8777
```

## 附录 B 判定阈值汇总

| 项 | 核心阈值 | 一票否决 |
|---|---|---|
| V1 | 全等 ≥95%，误识 0，p95 ≤70 ms | 任一误识被用于下单 |
| V2 | 偏差 p95 ≤20 ms，首点 ≤T0+10ms | 首点晚于 T0+20 ms |
| V3 | 驱动受理 ≥18/20 | 连续 3 次不受理 |
| V4 | p95 ≤2 ms，残留 0 | 任一残留按下或光标留在目标 |
| V5 | 落点 ≤8/12 px，受理 ≥8/10 | 点到相邻卡片 |
| V6 | 0 封禁/警告/踢 | 任一封禁或警告 |
| V7 | 9/9 起界面，`--doctor` 正常且 OCR 可用 | 白窗/缺资产 |
| V8 | 重启 ≤15 s，重复下单 0 | 重复下单或配置丢失 |

---

*本文件只描述验证方法，不含任何实测结论；执行完成后把数据填进 §6，另存为 `_realmachine_validation_report_<日期>.md`。*
