# R23 · 测试与验证结论的黑盒可信度复核

> 复核对象：`C:\Users\26346\Desktop\project\dafors` 已产出的测试报告、基准数据与结论。
> 复核方式：不读产品实现，只把报告当“主张”，重新跑能跑的、重新算能算的。
> 约束：只审不改；未 commit / push；未移动物理光标；未开可见窗口。
> 环境：2026-11-07 06:00–06:30 CST，Windows，项目自带 `.venv`。
> 重要环境事实：复核期间机器上有 20+ 个 Python 进程（其他会话的 soak / longrun / bench），
> CPU 占用长期 93%。凡涉及毫秒/微秒的复跑都受此污染，下文单独标注，不把“负载导致的变慢”
> 直接当成原主张造假。

---

## 0. 结论速览

| # | 主张 | 出处 | 本次复核 | 判定 |
|---|---|---|---|---|
| 1 | 全量回归 `938 passed, 1 skipped`（307.62s / 305.13s） | `_ocr_engine_selection.md:243`、`_ocr_summary_report.md:155` | 同一命令当前收集 1313 项：**11 failed, 1299 passed, 3 skipped, 434.83s** | **需保留**：只能算 2026-11-06 的历史快照，不能代表当前代码 |
| 2 | `878 passed` | 无任何文件出处 | 全库 md/txt/json 检索不到该数字或对应日志 | **不可信**：无证据、无时间、无代码版本 |
| 3 | `790 passed` | 无任何文件出处 | 同上 | **不可信**：无证据、无时间、无代码版本 |
| 4 | 前端审计 `154 项断言，0 失败` | `_frontend_redesign.md:28` | 用系统 Python + Playwright 复跑：`检查 154 项，失败 0 项` | **可信**（但口径仅限“自带 mock 后端 + 无头 Chromium 的审计脚本”） |
| 5 | `230 项` | `_scene_guard.md:533` | `230 passed in 26.81s`；但静态 `assert` 只有 83 条，230 是 pytest 用例数 | **可信**（数量），措辞“断言”不准确 |
| 6 | 授权/打包 `31 passed` | `_packaging_license.md:19,329` | `31 passed in 16.90s` | **可信** |
| 7 | 精密触发 `18 passed` 全绿 | `_branchB_precision.md:238` | `17 passed, 1 failed`：`M0 p95=3539.6us / 11260.7us`，压不进 1.5ms | **不可信**（当前不成立，且强负载敏感） |
| 8 | 倒计时 `78 passed / 16.19s` | `_branchC_review.md:21` | 当前收集 123 项；`test_countdown.py + test_countdown_sim.py` 连跑两次都是 `1 failed, 122 passed`，但**两次失败用例不同**；单跑 `test_countdown.py` 则 115 passed | **不可信**（用例已增长 + 顺序依赖/flaky） |
| 9 | 价格 OCR `42/42` | `_ocr_engine_selection.md:242`、`_ocr_summary_report.md:152` | `test_ocr_price.py + test_ocr_grade.py`：`107 passed in 18.62s`；单独计时 21/21 + 21/21 = 42/42 | **可信**（同夹具回归），**不可当作准确率估计**（有效独立样本仅 21） |
| 10 | 品阶 OCR `58/58`，准确率 100% | `_ocr_summary_report.md:152` | 同上，29 条 × 2 尺度全过 | **需保留**：可复跑，但 29 条来自 5 张截图，且是调参后的同一批 |
| 11 | 价格 OCR 单条 `p50 13.4ms / 12.5ms` | `_ocr_engine_selection.md:242` | 预加载 ROI 后 `read_price` p50 **54.92ms**（p95 105.12ms，min 17.65，max 160.67）；含解码则 108–116ms | **需保留**：方法可解释，但复跑差 4 倍，原报告没记录 CPU 负载/频率/后台进程 |
| 12 | 整页 `17.5s → 1765ms`；整页 1× 609ms、列表区 237ms | `_ocr_summary_report.md:153` | 无原始计时日志；同夹具（1380×630）上 `market.scan` 1×=1718ms、3×=1409ms（3× 反而更快），与报告尺寸/排序都不一致 | **不可信**：不可复跑，历史数字 17.5s 无出处 |
| 13 | PostMessage 直投 `0.045ms` | `_branchA_input_bench.md:18,116` | `tools/bench_input.py --quick`：M2 p50 **0.0419ms**、p95 0.1257ms | **可信**（口径：message-only 窗口、跨进程观测） |
| 14 | 触发抖动 `p50 2.10µs / p95 3.40µs` | `_branchB_precision.md:64-71` | `bench_precision.py --external --shots 100`：M0 p50 **62.1ms**、p95 215.5ms；项目自己的 200 枪测试 p95 3.54ms / 11.26ms，首次就压不进 1.5ms | **不可信**（作为对外结论/可复现指标）；仅在“空闲机 + TIME_CRITICAL + 3ms 自旋”的限定口径下可能成立 |
| 15 | 模拟“彻底失败率 0”，16 场景误判率 0.0 | `_scene_guard.md:504-533` | `python tests/scene_sim.py` 数字逐行复现；但失败分支在模拟器里结构上不可达 | **不可信**（作为实证结论）；作为“内部一致性自检”成立 |
| 16 | 打包 `146.9MB` / `107.0MB` | `_ocr_summary_report.md:154`、`_packaging_license.md` | `dist/SJZ-Control.exe` 140.06 MiB = **146.87 MB(十进制)**；`dist-onefile/SJZ-Control.exe` 102.05 MiB = **107.0 MB(十进制)** | **可信**（十进制 MB）；资源管理器显示为 140/102 MiB，存在 4.7% 单位口径差 |
| 17 | 冷启动到窗口 `5.63s` | `_ocr_summary_report.md:154` | `dist/desktop.log` 两次：17:25:31→17:25:34（3s）、17:46:38→17:46:41（3s）；onefile 的 `--doctor` 05:15:30→05:15:32（2s） | **不可信**：没有留 5.63s 的原始记录，也不是干净环境（同机、热缓存、并发进程） |
| 18 | 72h 等效长跑 PASS（内存/线程/句柄/资源上限） | `_stability_report.md:13` | `longrun_72h_final/`、`longrun_72h_fixed/` **目录为空**；§8 判据表“修复后”全部“待填”；唯一的修复后 2h 数据 `fix_2h` 句柄 216→284、type24 +68，`句柄回落到基线±30 = false` | **不可信**：最终 72h 证据缺失，且已有的修复后短跑仍然 FAIL |
| 19 | “当前测试全绿”这一总体印象 | 多份报告交叉使用 | 当前全量：11 failed，其中 9 个 `test_frontend_static`、1 个 `test_fire_proc`、1 个 `test_robustness` | **不可信**：不是同一棵树，也不是同一时刻 |

---

## 1. 测试数字：不是同一时刻、同一份代码

### 1.1 938 passed：历史快照，不是当前状态

- 原主张：`pytest tests` → **938 passed, 1 skipped**，连跑两遍 307.62s / 305.13s（`_ocr_engine_selection.md:243`）。
- 复核：当前同一命令收集 **1313 项**，结果 **11 failed, 1299 passed, 3 skipped, 434.83s**。
- 差距：
  1. 938 对应 2026-11-06 的提交 `5eef554`；当前工作树有大量未提交改动：
     `core/engine.py`、`core/countdown.py`、`core/input.py`、`core/ocr_service.py`、`core/scheduler.py`、`desktop.py`、`web/index.html` 等 20+ 文件被修改，另有 `core/precision.py`、`core/recovery.py`、`core/license.py`、`core/fire_proc.py` 等未跟踪新模块。
  2. 新增的 `test_countdown.py`、`test_countdown_sim.py`、`test_scene_guard.py`、`test_recovery.py`、`test_precision.py` 等把用例数从 938 推到 1313。
  3. 报告日期之后 `web/index.html` 被重做，但 `test_frontend_static.py` 仍按旧 DOM/旧字符串断言 → 当前 9 个静态前端用例失败。
- 结论：**938 只能作为“11-06 那个提交的历史记录”**。没有原始 pytest 日志留存，无法独立复核；把它当作“现在全绿”就是错的。

### 1.2 878 / 790：全库找不到出处

- 原主张：两次 `878 passed`、`790 passed`。
- 复核：对项目内 `*.md`、`*.txt`、`*.json` 做检索，除删除清单里恰好含 878/790 的无关数字外，**没有一条测试报告、日志或 JSON 记录这两个总数**。
- 结论：**不可信**。它们无法归属到任何提交、任何时间、任何测试范围；很可能是其他会话口头转述的中间数，没有落盘证据。

### 1.3 154 项断言 0 失败：数字真，但不能推“前端全绿”

- 复跑：系统 Python 3.11（带 Playwright）+ `tools/fe_audit.py` → `检查 154 项，失败 0 项`，与报告完全一致。
- 但同一棵树上项目自己的前端静态测试是红的：9 个 `test_frontend_static` 失败，原因分别是找不到 `实战`、`if(STATE.starting) return;`、`STATE.pollSeq += 1`、`condErr` 等旧标记。
- 差距：**154/0 是“审计脚本对自己 154 条断言的通过率”**，不是“前端没有失败”。它跑的是重做后的页面 + mock 后端；静态测试跑的是源码字符串契约。两者覆盖不同，不能互相担保。把 154/0 当成“前端验收全绿”是口径偷换。

### 1.4 230 项：数量真，措辞与结论都要打折

- 复跑：`230 passed in 26.81s`，与报告一致。
- 但 `230` 是 pytest 参数化后的**用例数**；两个文件里静态 `assert` 只有 83 条。报告写“230 项断言”，会让读者以为是 230 条独立断言。
- 更关键：这 230 项验证的是模拟器（见 §4），不是真机。数量可信，推论不可信。

### 1.5 31 passed：唯一完全干净的一个

- 复跑：`tests/test_license.py + tests/test_packaging.py` → `31 passed in 16.90s`，与报告一致。
- 这一条不依赖 OCR、不依赖负载、不依赖模拟器状态，**可信**。

### 1.6 18 passed / 78 passed：当前都不成立，而且暴露了测试不稳定

- 精密触发：
  - 报告：`18 项测试全绿（18 passed）`（`_branchB_precision.md:238`）。
  - 复跑：`17 passed, 1 failed`。失败断言是 `M0 p95=3539.6us`（另一次单独跑是 `11260.7us`），门槛 1.5ms。
  - 差距：不是“少了一个用例”，而是**核心时延指标在当前普通运行环境下过不了项目自己写的回归门槛**。即使把原因归为并发负载，也说明报告没有记录“必须空闲机”这一前提，结论不可当通用保证。
- 倒计时：
  - 报告：`78 passed / 16.19s`（`_branchC_review.md:21`）。
  - 当前：`test_countdown.py` 单独跑 115 passed；与 `test_countdown_sim.py` 连跑两次都是 `1 failed, 122 passed`，但**第一次失败在 `test_120_readings_is_borderline_and_honestly_bounded`，第二、三次失败在 `test_single_tick_with_plus_one_misread_cannot_fake_a_narrow_interval`**。
  - 差距：用例数从 78 涨到 123；且同一组文件连跑会出现**不同位置**的失败，说明测试之间存在共享状态/导入顺序依赖。一个会漂移的“全绿”不能当验收证据。
  - 该报告自己还承认：250/300 条、1000 组、2~2.5s 的几组数字“无法用已提交脚本复现”（`BUDGETS` 上限 200，JSON 里也没有 250/300）（`_branchC_review.md:20`）。
  - 其中 120 条读数的失败文案尤其直接：`120 条只有 5/20 可信，退化比报告里写的更严重`（断言要求 ≥8）。

---

## 2. OCR 准确率：数字可复现，但不是“准确率”

### 2.1 42/42 是可复跑的回归基线

- 复跑命令：`pytest tests/test_ocr_price.py tests/test_ocr_grade.py -q` → `107 passed in 18.62s`。
- 其中价格：第一轮 21/21、第二轮打乱 21/21，合计 42/42；品阶：29 条 × 1×/3× = 58/58。
- 所以“在这批夹具上全对”是**真的**，不是编的。

### 2.2 但 42/42 的有效样本量是 21，不是 42

- 第二轮只是把同一批 21 张 ROI 打乱顺序再读一遍；它验证的是“顺序无关”，不是 21 个新样本。
  注意：`_ocr_engine_selection.md:12` 却把这 42 写成“×2 种尺度”，与测试代码实际的“2 轮、第二轮打乱”不一致——报告连分母的构造都描述错了，42 更像是一个被凑出来的展示数。
- 品阶 58/58 同理：29 条 × 2 个放大尺度，独立样本仍是 29。
- 来源更窄：全部来自 **5 张 1920×1080 真实截图**，同一游戏、同一 UI 缩放、同一账号场景；没有第二套分辨率、没有低画质/遮挡/动态背景样本。
- 价格集没有负样本（没有“无价格/价格被遮挡”的拒识测试）；品阶集只有 5 条负样本。

### 2.3 同一批样本被用来调参，100% 是 in-sample

- 报告自己写明：成绩建立在三个必改参数上（`Global.use_preprocess_img=False`、`Det.limit_side_len=64/limit_type=min`、`Det.box_thresh=0.5`）；**用默认参数会掉到 18/36**（`_ocr_engine_selection.md:13`）。
- 随后最终验收仍在同一批“用户 5 图 21 条 ROI”上取 42/42（`:242`）。
- 差距：这是“在训练/调参集上评估”，没有留出集。**42/42 能证明“回归没坏”，不能证明“准确率 100%”**；换分辨率、换皮肤、换遮挡，分母会重新说话。
- 判定：42/42、58/58 **可信**（同夹具回归）；“100% 准确率”**不可信**（作为可泛化指标）。

### 2.4 13.4ms：方法自洽，复跑对不上

- 原报告口径：单条 ROI、预裁剪、RapidOCR 主路径、p50 13.4ms / 12.5ms。
- 本次复跑（预加载图像、只计 `read_price`）：p50 **54.92ms**，p95 105.12ms，min 17.65ms，max 160.67ms。
- 差距 4 倍。原因不是“代码坏了”，而是测量条件没被记录：复核时 CPU 93%、20+ 个 Python 进程在跑 soak/longrun/bench；原报告只说“p50”，没有说空闲、频率、后台进程、是否锁核。
- 判定：**需保留**。方法本身可解释、可复跑，但 13.4ms 不能作为可复现的 SLO；至少要在“空闲机 + 固定电源计划 + 无并发”下重测并留存原始 CSV。

---

## 3. 延迟数字：只有 0.045ms 站得住

### 3.1 0.045ms：复现 0.0419ms，可信

- `tools/bench_input.py --quick` 输出：
  - PostMessageW：M1 p50 0.025ms，M2 p50 **0.0419ms**，p95 0.1257ms；
  - SendInput(nop)：M1 p50 0.0537ms；
  - Interception(nop)：M1 p50 0.0136ms。
- 与报告 `0.045ms` 同一量级、同一测量点（M2 跨进程收到消息）。
- 口径限制（报告也写了）：这是 **message-only 窗口** 的直投；游戏不认（分支 D 的 200 次受理但无响应）。它能证明“投递便宜且零光标干扰”，不能证明“点击生效 0.045ms”。

### 3.2 2.10µs：方法与口径成立，作为结论不可复现

- 原报告明确限定：只统计“deadline → 回调入口”，空闲机，200 枪，spin=3ms，TIME_CRITICAL；p50 2.10µs、p95 3.40µs。
- 本次 `bench_precision.py --external --shots 100`：M0 p50 **62.1ms**、p95 215.5ms、max 390.9ms；M1 与 M0 几乎相同（说明不是两钟差，是线程没被及时调度）。
- 项目自己的 `test_precision.py` 200 枪门槛：
  - 全量跑：p95 3539.6us；
  - 单独跑：p95 11260.7us；
  - 门槛 1500us，**两次都失败**。
- 差距：4 个数量级。根本原因不是 QPC 不准，而是 **2.10µs 只在“线程已自旋、CPU 不被抢、计时器精度已提到最高”的窗口内成立**；报告把它放在“核心指标”表里，摘要再引用时就成了“端到端 2µs”，这是不可复现的。
- 判定：**不可信（作为对外/通用结论）**。若保留，必须写成“自旋后的回调入口抖动，空闲机专用口径，不代表点击落点精度”。

### 3.3 1765ms / 17.5s：没有原始数据，无法复核

- 原主张：整页识别 `17.5s → 1765ms`；整页 1× 609ms；只扫列表区 237ms；静止帧 16–20ms。
- 复核：
  - 项目内没有这组测量的原始 CSV/JSON/日志；
  - 同一批报告内部数字就打架：`_ocr_engine_selection.md:12` 写整页 **678ms**，`_ocr_summary_report.md:153` 写整页 **609ms**，相差 11%，没有说明是不同机器、不同图还是不同缩放；
  - `17.5s` 是历史管线数字，报告自己也没给出同一张图、同一分辨率、同一机器的对照；
  - 用现有 `tests/fixtures/market/list_unfiltered.png`（1380×630，不是报告的全页 1920×1080）在同样高负载下测：`market.scan` 1×=1718ms、3×=1409ms——不仅绝对值对不上，连“3× 更慢”的排序都反了。
- 差距：夹具分辨率不同、缓存状态不同、并发负载不同，三个变量都没控制，所以这组数字**不可作为性能结论**。可以保留“新链路比旧链路快”的方向性判断，但不能保留 17.5s→1765ms 这个 10× 比例。
- 判定：**不可信**。

---

## 4. “彻底失败率 0”：模拟器自己保证了自己

### 4.1 数字确实能逐行复现

- `python tests/scene_sim.py` 输出与 `_scene_guard.md:504-524` 完全一致：
  点了没生效 187/13/0、游戏断开 80/120/0、金币不够 0/200/0、设置读不出来 200/0/0（61 次安全硬停）、误判率 16 个场景全 0.0。
- 所以不是抄错数，是**模拟器的构造问题**。

### 4.2 失败分支在代码里不可达

- `tests/scene_sim.py` 的判定：
  - `STATE_OK` → `auto` / `manual_continue`；
  - `STATE_HALTED` → `containment_ok` 为真则 `auto`，为假才 `fail`；
  - 其它状态继续循环，跑完 `max_ticks` 才可能落到默认 `fail`。
  - 见 `tests/scene_sim.py:294-312`。
- 但模拟器的清理适配器全部是 `_ok(name)`，**无条件 `return True`**（`:238-242`）；`record` 也恒 `True`。
- 于是 `containment_ok` 永远为真 → 只要进 `HALTED` 就记 `auto`；`fail` 只剩“40 拍内既没 OK 也没 HALTED/WAITING”这一种边角。
- 结论：**“彻底失败 0”不是被测系统的属性，是模拟器把失败定义为“清理失败”，而清理永远不会失败**。这正好回答了“是否只是模拟了自己实现出来的状态”：是。

### 4.3 误判率 0.0 同理

- `normal_signals()` 由同一个作者写死：anchor_hits 2–9、click_effect_age 0.05–0.34s、frame_dt 14–70ms、p95 16–230ms……全部落在守卫阈值的安全侧（`tests/scene_sim.py:34-46`）。
- 用“作者自己定义的安全分布”去测“作者自己定的阈值”，误判率当然接近 0。它能证明“阈值不会在理想抖动下误报”，不能证明“真实游戏抖动下不误报”。
- 判定：**不可信（作为实证结论）**；作为内部一致性/回归自检，成立。

---

## 5. 稳定性、打包、冷启动

### 5.1 72h PASS：报告自己的明细在打脸

- 报告首页表：`72h 等效长跑（4259 轮） 内存、线程、文件句柄、周期耗时、资源上限 PASS`（`_stability_report.md:13`）。
- 但同一份报告：
  - §3.3 第一次跑：句柄 `214 → 382`，**FAIL**；
  - §8 判据清单：修复后 1–9 项全部写 **“待填”**；
  - §3.5 说“见 `tools/stability/_results/longrun_72h_final/`”，但该目录**是空的**；`longrun_72h_fixed/` 也是空的。
  - 唯一有数据的修复后跑是 `fix_2h/summary.json`：`handles 216 → 284`、`handle_types_diff {"24": 68}`、`verdict["句柄回落到基线±30"] = false`。
- 差距：**修复后没有 72h 数据，只有一个 2h 等效跑，而且它仍然 FAIL**。首页的 PASS 是拿“修复意图”当“修复结果”。
- 判定：**不可信**。

### 5.2 打包体积：可信，但要注意单位

- `dist/SJZ-Control.exe`：140.06 MiB = 146.87 MB（十进制），对应报告 146.9MB；
- `dist-onefile/SJZ-Control.exe`：102.05 MiB = 107.0 MB（十进制），对应报告 107.0MB；
- 资源管理器按 MiB 显示，会看到 140MB / 102MB，和报告数字差约 4.7%。这是单位口径，不是造假。
- onefile 的 `--doctor` 日志确认 OCR 已进包：`PP-OCRv6_det_small.onnx 9.9MB`、`rec_small.onnx 21.2MB`、`OCR 可用=True`。这一条**可信**。

### 5.3 冷启动 5.63s：没有证据，也不是干净环境

- 报告：`冷启动到窗口 5.63s`。
- 留存日志：
  - `dist/desktop.log`：17:25:31 启动 → 17:25:34 窗口已显示（3s）；
  - 同文件另一次：17:46:38 → 17:46:41（3s）；
  - `dist-onefile/desktop.log`：05:15:30 → 05:15:32 完成 `--doctor`（2s），但这不是“到窗口”。
- 差距：没有 5.63s 的原始记录；“干净环境”也没有证据（同机、热缓存、并发 20+ Python 进程，且机器以前多次启动过同款 exe）。冷启动必须定义为“新机/清空文件缓存/无 AV 首次扫描”，这里一个都没控制。
- 判定：**不可信**。

---

## 6. “不可信”项逐条对照（原主张 / 复核结果 / 差距）

| 原主张 | 本次复核结果 | 差距在哪 |
|---|---|---|
| `878 passed` | 全库无可查记录 | 无提交、无日志、无时间，无法归属 |
| `790 passed` | 全库无可查记录 | 同上 |
| 整页 `17.5s → 1765ms` | 无原始计时；同夹具 1×=1718ms、3×=1409ms（负载 93%） | 无同图/同分辨率/同机对照；3× 排序甚至相反 |
| 触发 `p50 2.10µs / p95 3.40µs` | 复跑 M0 p50 62.1ms；项目自身 200 枪门槛 p95 3.5–11.3ms 失败 | 只适用于“空闲 + 已自旋 + 最高计时精度”；普通环境差 4 个数量级 |
| `彻底失败 0` | 模拟器逐行复现，但 `fail` 仅在 `containment_ok=False`；清理恒 True | 失败分支结构上不可达，等于自证 |
| 16 场景 `误判率 0.0` | 逐行复现；正常信号由作者写在安全区间内 | 用自定义安全分布测自定义阈值，不能外推真机 |
| `72h 长跑 PASS` | 最终 72h 目录为空；修复后 2h 跑句柄 216→284 仍 FAIL | 首页 PASS 无数据支撑，明细“待填” |
| `冷启动 5.63s` | 日志为 3s（onedir 到窗口）/2s（onefile doctor） | 无 5.63s 记录，非干净环境 |
| `18 项测试全绿` | 17 passed, 1 failed（p95 3.5ms / 11.3ms） | 核心门槛在当前环境过不了 |
| `78 passed` | 123 项；连跑两次 1 failed，且失败位置漂移 | 用例已增长；测试顺序依赖/flaky |
| `938 passed` 当作当前状态 | 当前 11 failed, 1299 passed, 3 skipped | 不是同一份代码（20+ 文件未提交改动、9 个新测试文件） |
| `154 项 0 失败` 当作前端全绿 | 154/0 复现；但 9 个 `test_frontend_static` 失败 | 审计脚本与项目静态契约测试覆盖不同，不能互保 |
| `42/42`、`58/58` 当作准确率 | 同夹具全过；有效独立样本 21 / 29，来自 5 张截图，调参用同一批 | 无留出集、无第二分辨率、价格无负样本 |

---

## 7. 复核后依然站得住的关键结论

1. **授权/打包的 31 项测试是真的**：`tests/test_license.py + tests/test_packaging.py` 复跑 `31 passed`，覆盖 Ed25519 向量与打包钩子；这一块可以作为可信基线。
2. **PostMessage 直投确实便宜且零光标干扰**：M2 p50 0.0419ms、`moved_rate=0.0`、`raw_events=0`；但它只是“投递成功”，游戏侧不认，不能当点击方案。
3. **OCR 在现有夹具上是可复跑的回归基线**：价格 42/42、品阶 58/58，`107 passed`；换引擎/换参数后拿这组夹具守住不退化，是有效的。
4. **RapidOCR 调参方向有效**：报告给出的“默认参数 18/36 → 调参后 36/36”有明确对照，至少证明那三个参数不是拍脑袋。
5. **前端重做后的页面在 mock 后端下能过 154 条运行时断言**：六页、增删改、热键、guard 4/5 级、四档宽度等；作为“页面自身没有崩”的证据成立。
6. **场景/恢复模拟器内部是自洽的**：230 项用例全过，16 场景的随机变体统计确定性复现；它证明了“按这套模型，规划器能在预算内收敛”，但**不证明真机不会彻底失败**。
7. **打包体积与 OCR 入包可信**：146.9/107.0 MB（十进制）与 31.2MB 模型文件均有产物/日志佐证；单位口径需说明。
8. **倒计时的 jitter 基准 JSON 本身可信**：`tools/bench_countdown.json` 里 120 条读数 trust=98.5%、p95=11.78ms、within20=97.5%，与报告方向一致；问题在于它假设的误差模型比 `test_countdown` 的“最坏相位 + 7.5% 错读”更乐观，后者会掉到 5/20。
9. **当前 11 个失败是真实、可复现的工程问题**（不是“报告造假”）：9 个前端静态契约过期、1 个 `fire_proc` 坏路径未标 dead、1 个源码 BOM 编译失败。它们应当作为待修清单，而不是被“全绿”叙事盖住。

---

## 8. 复跑命令与证据位置

```powershell
# 当前全量（11 failed / 1299 passed / 3 skipped / 434.83s）
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider

# 分项
.\.venv\Scripts\python.exe -m pytest tests\test_license.py tests\test_packaging.py -q -p no:cacheprovider   # 31 passed
.\.venv\Scripts\python.exe -m pytest tests\test_ocr_price.py tests\test_ocr_grade.py -q -p no:cacheprovider # 107 passed
.\.venv\Scripts\python.exe -m pytest tests\test_scene_guard.py tests\test_recovery.py -q -p no:cacheprovider # 230 passed
.\.venv\Scripts\python.exe -m pytest tests\test_precision.py -q -p no:cacheprovider                          # 17 passed, 1 failed
.\.venv\Scripts\python.exe tests\scene_sim.py                                                               # 逐行复现 0 失败
.\.venv\Scripts\python.exe tools\bench_input.py --quick                                                     # PostMessage M2 p50 0.0419ms
.\.venv\Scripts\python.exe tools\bench_precision.py --external --shots 100                                  # M0 p50 62.1ms
& "C:\Users\26346\AppData\Local\Programs\Python\Python311\python.exe" tools\fe_audit.py                     # 154 项，失败 0
```

关键证据文件：

- 当前失败清单：本次全量输出（11 failed）；`tests/test_frontend_static.py`、`tests/test_fire_proc.py`、`tests/test_robustness.py:55`、`tests/test_precision.py`。
- 顺序依赖：`tests/test_countdown.py:879`、`:900` 在合并 `test_countdown_sim.py` 时失败，单跑通过。
- 模拟器不可达分支：`tests/scene_sim.py:238-242`、`:294-312`。
- 72h 缺失：`tools/stability/_results/longrun_72h_final/`（空）、`longrun_72h_fixed/`（空）、`fix_2h/summary.json`（句柄 verdict=false）。
- 冷启动日志：`dist/desktop.log`、`dist-onefile/desktop.log`。
- 打包体积：`dist/SJZ-Control.exe`、`dist-onefile/SJZ-Control.exe`。
- 调参与评估同批：`_ocr_engine_selection.md:13`、`:242-243`。

---

## 9. 一句话总结

**能复跑的数字，大多只是“在这批夹具/这台机器/这个模拟器里成立”；不能复跑的数字，基本都没有留原始数据。**
真正可信的是：31 项授权/打包、154 项页面审计、230 项模拟器自检、PostMessage 0.042ms、打包体积与 OCR 入包。
不可信的是：878/790、1765ms/17.5s、2.10µs（作为通用指标）、彻底失败 0、72h PASS、冷启动 5.63s，以及任何“当前全绿”的说法。
