# 死代码与性能热点清单（只读盘点）

- 盘点对象：`C:\Users\26346\Desktop\project\dafors`（HEAD `5eef554`）
- 盘点方式：AST 自写扫描（无 vulture/pyflakes，环境里未安装且不往 venv 装包）+ 反向 grep 逐条取证 + `cProfile` 单轮短采样
- 约束遵守：**未修改任何 `.py`/`.md`**；只在只读副本（`%TEMP%\sjzdc\sbx`）里 import 与跑剖面；未 commit/push
- 扫描面：367 个 `.py`（已排除 `perf/`、`.venv*`、`build/`、`dist*`、`__pycache__`）
- 入口：`desktop.py`、`run.py`、`tools/fire_worker.py`、`tools/calibrate.py`、`core/fire_proc.py`、`evo/__main__.py` + `evo/cli.py`
- 可达性结论：**367 个模块里 75 个在产品运行时可到达**，其余 292 个分别属于 `tests/`、`tools/`（开发工具）、`evo/`（诊断 CLI）、`portal/`（服务端独立部署）、草稿目录

## 机器纪律（跑前 / 跑后）

| 采样点 | python.exe | 常驻内存 | 整机 CPU |
|---|---|---|---|
| 跑前（盘点开始） | 8 | 约 540 MB | 80–93% 波动（另一会话的 OCR 长跑 + pytest 在跑） |
| 跑后（盘点结束） | 8 | 545 MB | 80% |

- 全程未开可见终端/窗口（无 `http.server`、无 `cmd /c`、无 GUI 探针），剖面在临时副本内单轮完成，无忙等
- 因整机 CPU 已 80%+，本轮 cProfile 的**绝对耗时只能作相对占比用**；绝对值以 `_perf_ocr.md` / `_perf_flow.md` / `_click_cost_20261107.md` 的空载数为准

---

## 一、死代码分级清单

### A 级 —— 可直接删（反向搜索 0 命中、无测试依赖）

搜索面：`*.py / *.js / *.html / *.cs / *.ps1 / *.bat / *.spec / *.json / *.md`（排除 `perf/`、`.venv*`、`dist*`、`build/`）

| 类别 | 条目 | 证据 | 风险 | 建议 |
|---|---|---|---|---|
| 函数 | `core/countdown.py:1219` `enable_timer_resolution()` | 全仓 `\benable_timer_resolution\b` 命中 1 次＝定义本身；仅 `_branchC_review.md:123/127` 提到；tests 命中 0 | 可直接删 | 删。注意：内部只调 `timeBeginPeriod(1)` 而无 `timeEndPeriod` 配对，留着才是隐患 |
| 函数 | `core/ocr.py` `digit_runs()` | 命中 1（定义）；tests 0 | 可直接删 | 删；`_bug_r06` 文档说 market 用过，但代码里已无调用，属过期描述 |
| 函数 | `core/ocr.py` `digits_only()` | 命中 1（定义）；`core/market.py` 里已无此调用 | 可直接删 | 删 |
| 函数 | `core/digit_ocr.py` `read_digits()` | 命中 1（定义）；tests 0 | 可直接删 | 删（注意 `read_detail()` 是 tier3 活路径，**不能动**） |
| 函数 | `core/digit_ocr.py` `match_char()` | 命中 1（定义） | 可直接删 | 删 |
| 函数 | `core/digit_ocr.py` `read_detail_ex()` | 命中 1（定义） | 可直接删 | 删 |
| 函数 | `core/digit_ocr.py` `reset_template_cache()` | 命中 1（定义） | 可直接删 | 删 |
| 常量 | `core/nav_coords.py` `ONS_RARITY_BOX` | 命中 1（定义）；`ONS_` 前缀其余常量都有调用 | 可直接删 | 删（旧版荐品比对残留） |
| 常量 | `core/nav_coords.py` `ONS_CONDITION_BOX` | 命中 1（定义） | 可直接删 | 删 |
| 常量 | `core/nav_coords.py` `N_POPUP` | 命中 1；实际在用的是 `N_POPUP_HINT` | 可直接删 | 删 |
| 函数 | `core/server.py:87` `control_token()`（getter） | 命中 1；同文件 `set_control_token()` 在 1619 行被调用，`_ctrl_token` 在 72/389/396 被直接读 | 可直接删 | 只删这个只读 getter，保留 setter 与鉴权逻辑 |
| 函数 | `core/server.py:138` `idle_seconds()` | 命中 1；全仓含 `.cs/.md/.js` 命中 0 | 可直接删 | 删（`_last_hit` 仍被 `touch()` 维护，可选一并清） |
| 方法 | `core/input.py` `trajectory_status()` | 命中 1；tests 0 | 可直接删 | 删 |
| 方法 | `core/input.py` `channel_last_event()` | 命中 1；仅 `_input_fixes.md:60` 描述 | 可直接删 | 删 |
| 常量 | `core/selfcheck.py` `APP_NAME` | 命中 1 | 可直接删 | 删 |
| 函数 | `core/precision.py` `action_s()` | 命中 1（模块本身也零引用，见 B） | 可直接删 | 删 |
| 常量 | `core/precision.py` `THREAD_PRIORITY_NORMAL` | 命中 1 | 可直接删 | 删 |
| 常量 | `core/autotune.py` `SUCCESS_RESULTS` / `FAIL_RESULTS` / `RESULT_LABEL` | 各命中 1（定义）；同模块其余常量有测试引用 | 可直接删 | 删；`_label()` 之类内部是否会用到需在删前跑一遍 `tests/test_autotune.py` |
| 函数 | `evo/collect/parse.py` `read_csv_rows()` | 命中 1；`evo.collect` 已排除出打包 | 可直接删 | 删 |
| 常量 | `evo/collect/schema.py` `stage_names` | 命中 1 | 可直接删 | 删 |

**A 级小计：20 条**

### B 级 —— 需确认（只被测试 / 开发工具 / 历史报告引用）

| 类别 | 条目 | 证据 | 风险 | 建议 |
|---|---|---|---|---|
| 模块 | `core/autotune.py`（58.4 kB / 1328 行） | 产品侧 import 命中 **0**；`tests/test_autotune.py` 38 处；`sjz.spec` HIDDEN 里没有它（不进 exe） | 需确认 | 与 `test_autotune.py` 一起下线，或明确标注「离线调参工具，不随产品走」 |
| 模块 | `core/precision.py`（20.9 kB / 452 行） | 产品 import 命中 0（唯一「命中」是自身 docstring 的示例）；`tests/test_precision.py` 2 处 | 需确认 | 同上；注意 fire 路径的计时已改走 `core/timesync.py`，删前确认无 `getattr` 取用 |
| 模块 | `core/truth_model.py`（4 kB） | import 0；`tests/test_calib_backup.py:20`、`tools/verify_click_truth.py` | 需确认 | 随 `verify_click_truth.py` 一起处置 |
| 模块 | `core/ed25519_ref.py`（4.5 kB） | import 0；`tests/test_license*.py`、`tools/r10*` 交叉验证用 | 需确认 | 纯 Python 参考实现，删了会失去「C 实现 vs 参考实现」对拍；建议保留但标注用途 |
| 草稿 | `_lottery_fsm.py`（25.2 kB）+ `_test_lottery_fsm.py`（17.2 kB） | 两个文件全仓互相引用之外 0 命中；文件名不以 `test_` 开头，pytest 不会收集 | 需确认 | 属状态机原型；确认规格已并入 `core/` 后整对删除 |
| 快照 | `perf/_ref`（998 个 py / 54 MB） | `perf/base` 的克隆分支；md 里仅 2 处引用，`perf/base` 被引用 33 次 | 需确认 | 删。先确认无脚本按路径读 `perf/_ref` |
| 快照 | `perf/merged_pre`（470 个 py / 33 MB） | 与 `perf/merged` 同结构；md 引用 2 次，`perf/merged` 21 次 | 需确认 | 删 |
| 快照 | `perf/snapshots`（1691 个 py / 100.9 MB，目录总计 364.8 MB） | md 引用 0；`perf/` 整体不在 `datas`，不进 exe | 需确认 | 外置归档（`git` 历史已够），不要留工作树 |
| 快照 | `perf/base`（973 个 py / 54 MB）、`perf/merged`（477 个 py / 33 MB） | 历史基线，md 引用 33 / 21 次 | 需确认 | 若还要复现老基线就留；否则连同引用一起清理 |
| 资源 | `evo/fixtures`（260 文件 / 42 MB） | `datas` 明确排除 fixtures；运行时不读 | 需确认 | 移出仓库或 `.gitignore` |
| 打包 | `evo/` 源码整体 | `datas` 打进了 evo 源码，但 `desktop.py`/`run.py` 无任何 evo import | 需确认 | exe 里少 125 个 py 的死重；确认交付不需要 `evo` CLI 后从 `datas` 拿掉 |
| 产物 | `_measure`（393 文件 / 95.8 MB） | 809 处引用（`ocr_price_remeasure.py` 等按路径读） | 需确认 | 测量数据仓库，建议外置 |
| 产物 | `_rm_shots`（93 文件 / 96.4 MB） | `replay_check.json` 117 处按文件名引用 | 需确认 | 回放夹具，删则 `replay_check` 失效；外置为压缩包 |
| 产物 | `_reverse`（1578 处引用）、`_rm_backup`、`_soak_data`、`_ui_shots`、`_r08_shots`、`_calib_thumbs` | `make_worktree.ps1:82-84`、`tree_digest.py:37-39`、`replay_check.json` 均按名字列入 | 需确认 | 与这三个脚本的白名单绑定，删除必须同步改三处 |
| 草稿 | `_adv3`（7 py）、`_verify_bb`（5 py）、`_timing_lab`（1 py） | md 引用若干（`_nonreal_trust.md` 等）；无产品 import | 需确认 | 归档，勿留工作树 |
| 探针 | `_nonreal_input_probe.py`（35.2 kB） | `_nonreal_input.md:4/148` 明确写「保留」 | 需确认 | 按文档保留，别当死代码删 |

**B 级小计：16 条**

### C 级 —— 不可删（动态引用 / 框架按名调用 / spec 绑定）

| 类别 | 条目 | 证据 | 风险 | 建议 |
|---|---|---|---|---|
| 模块 | `core/fire_proc.py` + `tools/fire_worker.py` | `sjz.spec:126`、`sjz-onefile.spec:72` 的 `hiddenimports` 显式 pin；`tests/test_fire_proc.py`、`tests/test_release_pipeline.py:41`、`tools/stability/faults.py:27` 在用 | 不可删 | 产品路径（`core/engine.py`）实际走 `click_burst`，**没有任何地方实例化 `FireProcess`**；要动只能同时改 spec + 测试 + `tools/stability`，属于架构决策，不在清死代码范围 |
| 方法 | `core/server.py:555` `do_PUT()` | `BaseHTTPRequestHandler` 按方法名分发；`web/index.html:4282` 真的发 `PUT /api/regions` | 不可删 | 保留（调试期曾判成死代码的高危项） |
| 工厂 | `core/ocr_chain.py` `make()`、`core/ocr_rapid.py` `make()`、`core/ocr_ppocr.py` `make()` | 测试按名注入 backend；`ocr_chain.Backend` 内部按名取 `rapid/pp/tpl` | 不可删 | 保留 |
| 方法 | `tools/realmachine_v2.py` `stage_S0`…`stage_S7` | `tools/realmachine_v2.py:943` `getattr(self, f"stage_{sid}", None)` 字符串拼接分发 | 不可删 | 保留；A 级误判高发区 |
| 路由 | `portal/routes_admin.py` / `routes_public.py` 里所有「零引用」视图函数 | Flask `@app.route` 装饰器注册，函数名不出现在任何调用点 | 不可删 | 保留 |
| 测试 | `tests/**` 的 `test_*` 与 fixture | pytest 按名收集，文本搜索必然 0 命中 | 不可删 | 不参与死代码判定 |
| 资产 | `core/assets/digits`（344 文件 / 0.21 MB）、`core/assets/rapid`（2 onnx / 30 MB）、`core/assets/glyphs` | `digits` 被 `digit_ocr.read_detail()`（tier3 价格兜底）读；`rapid` 被 `ocr_rapid` 读；`glyphs` 被 `glyphs.py` 读 | 不可删 | 保留；`datas` 里 `core/assets` 整目录打包是对的 |

**C 级小计：7 条**

---

## 二、重复实现 / 重复资源

| 重复对象 | 活的那份 | 死/冗余的那份 | 证据 |
|---|---|---|---|
| `_words(host, img, strict)` | `core/nav_col.py:22` | `core/nav_ons.py:76` | 两份函数体 AST 逐节点完全相同（同哈希），两份都在被同模块内部调用 → 可抽成一个 `core/ocr_roi.py` 工具，属「真重复」 |
| OCR 三条管线同名接口 `read_words/read_text/make/stats_line` | `ocr_rapid`（默认一档）、`ocr_ppocr`（二档）、`ocr_chain`（调度） | —— | 同名不同实现是刻意分层，**不是重复**；勿合并 |
| 价格解析 | `core/money.py:scan_text`（唯一权威） | 历史 `read_price` / `_price_from_block` / `cards._price_token_in` 已收敛到 money | 已在 N11 做过；本轮复查无第二套实现（文档里的旧说法已过期） |
| `perf/base` vs `perf/_ref` | `perf/base`（md 引用 33 次） | `perf/_ref`（md 引用 2 次） | 同哈希文件占比极低（27/214 与当前 core 一致），是**带旧补丁的分支快照**，不是最新代码 |
| `perf/merged` vs `perf/merged_pre` | `perf/merged`（21 次） | `perf/merged_pre`（2 次） | 同上 |
| `tools/collect_b1..b5` 的 `check()` | 五份完全同体（137/174/127/202 行） | —— | `tools` 内跨文件同体函数 10 组，其中 `check` 5 份、`entropy`/`read` 各 4 份、`sha256_file`/`log_message`/`streams` 各 2–3 份 |
| `tools/axon_watch`/`catch_unpack_window`/`compare_mem_vs_file`/`dump_live_sections` 的 `entropy()`/`read()` | 同体 4 份 | —— | 建议抽 `tools/_memio.py`；这些只在开发机跑，不进 exe，风险低 |

---

## 三、性能热点画像（cProfile，单轮，80% 负载下）

> 采样环境：只读副本 + 真实夹具（`tests/fixtures/market/*.png`），每路径 1–3 轮。cProfile 自身开销约 1.5×，**占比可信、绝对值偏高**。

### 1) 列表冷扫一轮（`market.Scanner.scan` → `cards.read_cards`）

| 热点 | cumtime | 占比 |
|---|---|---|
| `onnxruntime run`（9 次） | 358 ms | 84.0% |
| └ `recognize_txt`（3 次，6 张卡批识别） | 263 ms | 61.7% |
| └ `detect_and_crop`（6 次，每卡一次 det） | 147 ms | 34.5% |
| └ det 预处理 `normalize`（6 次，numpy） | 17 ms | 4.0% |
| `cards.locate_cards` | 9 ms | 2.1% |
| 其余 Python（裁切/拷贝/坐标） | ≈10 ms | 2.4% |

墙钟 426 ms（带载）；**同帧二次扫描命中 memo 后 0.10 ms/帧**。

### 2) OCR 面板读取一轮（`ocr_chain.read_panel` 拼条）

| 热点 | cumtime | 占比 |
|---|---|---|
| `initialize_session`（2 次） | 159 ms | 36.7%（**一次性**：det+rec 模型加载，非每轮） |
| `recognize_txt`（2 次，含上面 rec 加载 131 ms） | 272 ms | 62.8% |
| `onnxruntime run`（4 次 = 2 det + 2 rec） | 154 ms | 35.5% |
| `read_panel`/`read_words` 的 Python 层（拼条画布 `np.zeros` + 坐标还原） | ≈15 ms | 3.5% |

稳态单轮 ≈137 ms（cProfile）≈90 ms（无插桩，与 `_perf_ocr.md` 的 89.6 ms 吻合）。

### 3) 点击一次（stroke 组装 + `send_mouse_batch`）

| 热点 | 耗时 | 归类 |
|---|---|---|
| `humanmouse.plan()` 轨迹 | 0.504 ms/次（200 次合计 100.8 ms） | 纯计算 |
| └ `_windmouse` | 0.29 ms / 58% | 纯计算 |
| └ `_sample_at` / `_cum_len` | 0.15 / 0.095 ms（30% / 19%） | 纯计算（`math.hypot` 被调 60972 次） |
| `send_mouse_batch` | 1 次调用 | 批次已合并（8→1） |
| 等 Raw Input 落地 | 墙钟 81–90% | **I/O 等待，不可优化**（`_click_cost_20261107.md:57`） |

### 4) 一轮 telemetry harvest（1500 新行）

| 热点 | cumtime | 占比 |
|---|---|---|
| `io.open`（同一队列文件被开 6 次） | 31 ms | 32.6% |
| `read_new_lines` 增量读 | 26 ms | 27.3% |
| `_event` ×1500 | 22 ms | 23.1% |
| `queue_size()`（再全量数一遍行） | 20 ms | 21.0% |
| `queue_append`（json + 落盘） | 12 ms | 12.6% |
| `_line_sha256` ×4501 | 8 ms | 8.4% |

≈63 µs/行；无新行时 0.9 ms/轮。按现场事件量（<1 条/秒）折算，日常开销可忽略。

### 三类归因

| 类型 | 冷扫 | 面板 | 点击 | harvest | 合计判断 |
|---|---|---|---|---|---|
| 纯计算（可优化，代码层） | 68 ms（16%） | ≈15 ms（3.5%） | 0.5 ms（100%） | 45 ms（47%） | 冷扫与 harvest 有真余量；面板/点击几乎无 |
| I/O 等待（不可优化） | 0 | 0 | 81–90% 墙钟 | 53 ms（56%） | 点击的时间全在这里 |
| GIL 争用 | ORT run 期间释放 GIL，Python 层仅 16% | 同左 | 无 | 全 Python、无并发 | **这四条路径里没有 GIL 热点**；唯一被证实的 GIL 伤害是抢购自旋（已用 `fire_proc` 移出进程，文档 p50 0.3→5.8 ms） |

---

## 四、已经被优化过的热点（不要重做）

| 已做过 | 数据 | 出处 |
|---|---|---|
| 单帧 OCR | 22.98 → 12.91 ms p50（**−44%**） | `_perf_ocr.md` |
| 详情面板拼条（4 ROI 一次进模型） | 222.8 → 89.6 ms p50（**−60%**，另一版记 −66%） | `_perf_ocr.md` / `_ocr_speed_report.md` |
| 列表扫描 memo | 二次扫描 **0.10 ms/帧**（本轮实测） | 本轮 cProfile A2 |
| 点击批次合并 | 8 次 `send_mouse` → **1 次** `send_mouse_batch`；纯 Python 组装 21.3 µs | `_click_cost_20261107.md` |
| 抢购自旋移出主进程（解 GIL） | p50 0.3 → 0.0003 ms（独立进程） | `core/fire_proc.py` 头注释 |
| 冷扫预热前移 | `serve()` 同步预热，避免请求线程撞冷扫 | `core/server.py:1645` |

## 五、还剩多少真实空间

| 路径 | 代码层可动 | 只能靠模型/架构的部分 | 结论 |
|---|---|---|---|
| 列表冷扫 426 ms | Python 层 68 ms 里可砍一半 ≈ **30 ms**（det 归一化 17 ms + 裁切/坐标） | 84%（358 ms）在 ORT 里：要快只能 rec int8 量化、或把 6 次 det 并成 1 次（属识别方案变更） | 代码层 ≈ **−7%**，模型层 20–35%，需单独评审 |
| 面板 90 ms（稳态） | Python ≈10–15 ms，拼条画布可复用 buffer | 一次性 159 ms 模型加载：确认 `ocr_service` 是否每 worker 各建一份（若是，改共享 = 省 159 ms × worker 数） | 代码层 ≈ **−10%**，一次性加载是唯一大额 |
| 点击 | 0.5 ms（`hypot`/采样向量化可到 0.2 ms） | 81–90% 是等 Raw Input | **无空间**（相对 21 µs 组装无意义） |
| harvest | 6 次 `open` 合 1 次，可省 ≈**50 ms / 1500 行** | 无 | 按现场量折算 ≈0；只有补采积压时才有感 |
| 全链路 R0 p50 = 4413.77 ms | 上述合计 ≈ **90–110 ms** | guard（整屏 OCR ×3）= 2092.75 ms，占 **55.9%**，是语义成本 | **清死代码不等于提速**；代码层总余量 ≈ **2%** |

---

## 六、最该先删的 10 条（按「省体积 / 风险比」排序）

1. `perf/_ref` 整目录（998 py / **54 MB**，md 仅 2 处引用）
2. `perf/merged_pre` 整目录（470 py / **33 MB**，md 仅 2 处引用）
3. `perf/snapshots`（1691 py / **100.9 MB**，零引用 → 外置归档）
4. `_rm_shots`（93 截图 / **96.4 MB**，只被 `replay_check.json` 按名引用 → 压缩外置）
5. `_measure`（393 文件 / **95.8 MB**，只被测量脚本按路径读 → 外置）
6. `evo/fixtures`（260 文件 / **42 MB**，打包已排除、运行时不读）
7. `core/ocr.py` `digit_runs()` + `digits_only()`（0 引用，且文档已过期）
8. `core/digit_ocr.py` `read_digits` / `match_char` / `read_detail_ex` / `reset_template_cache`（同模块 `read_detail` 才是活的）
9. `core/countdown.py` `enable_timer_resolution()`（0 引用，且 `timeBeginPeriod` 无配对）
10. `core/nav_coords.py` `ONS_RARITY_BOX` / `ONS_CONDITION_BOX` / `N_POPUP`

合计：**A 级 20 条 / B 级 16 条 / C 级 7 条**；仅第 1–6 项可回收约 **421 MB**（仓库 982.8 MB 的 43%）。
