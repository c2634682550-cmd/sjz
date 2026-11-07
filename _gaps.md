# _gaps.md — 只读缺口清点（以代码为准）

仓库：`C:\Users\26346\Desktop\project\dafors`　核查日期：2026-11-07　方式：静态阅读 + 只读脚本（未打包、未启动服务、未改任何代码）

## 总表

| # | 项 | 结论 | 关键证据 | 若未实现的补法（一句话） |
|---|---|---|---|---|
| 1 | 入池链路六字段 | **未实现** | `core/engine.py:3050,3104-3111,3112-3137`；`core/records.py:222-223,274` | 在收池/抽签分支把六个键塞进 `extra`，不动 `records.add` 签名 |
| 2 | 遥测 offset_ms / tier | **已实现**（`offset_ms` 入服务端时改叫 `skew_ms`） | `core/engine.py:3062-3069,3074-3082,3132-3136`；`tools/telemetry_agent.py:296`；`tools/telemetry_server.py:66-68,72-88,231-232`；`tools/telemetry_dashboard.html:192,275,465,711` | 只需删/改 `telemetry_agent.py:292-293` 的过期注释 |
| 3 | 安装与升级 | **部分实现**：更新链路有；安装器/卸载无 | `launcher/Version.g.cs:5-7`；`launcher/Program.cs:376`；`launcher/Updater.cs:109,202,252-260,440`；`launcher/launcher.config.json:3`；全仓 0 个 `.iss/.nsi`；`docs/build-desktop.md:5` 声称已打 Inno Setup | 补 `installer/sjz.iss`（含卸载段）+ 一个生成签名 `version.json` 的脚本 |
| 4 | 框选接口 | **接口与界面都已实现**，仅一处文案过期 | `core/server.py:425,558,778,783`；`core/config.py:848,851`；`web/index.html:3903,4000,4030`；过期文案 `web/index.html:1554` | 删/改 `index.html:1554` 那句"没有框选页" |
| 5 | 正式密钥 | **常量是正式公钥；与仓库内默认 dev 私钥不配对** | `core/license.py:60,262-274`；`tools/license_server.py:43-46,142-155`；`.gitignore:61` | 让签发服务默认读 `default_signing_key_path()`，或换 dev seed 与常量配对 |
| 6 | 打包模块收集 | **core 新模块无漏项；`evo/` 未进包** | `sjz.spec:46-74,76-99,114-119`；静态可达性 6/6 REACH、`evo.*` 全 MISSING | 若要随包分发 evo，在 spec 加 `collect_submodules("evo")` + `("evo","evo")` datas |
| 7 | 自诊断缺信号 | **3 条 corr + 19 处判据缺字段 + 5 个现象标"高风险"** | `rules.json:125-127,188-190,263-265`；`_self_diagnose.md:19,51,64,66,67,73,75,76,82,87,96,118,169,268`；`phenomena.json:31,56,100,123-124,147-148,170,216,239,262,285,354-355,515-516,562,607,631` | 先补 `match_score`、`frame_changed`、`click down/accepted` 计数、`额度+余额快照` |
| 8 | 测试素材缺口 | **一个实质缺口**：`_shots/drv_onsale.png` | `tests/test_market_video.py:51,156-163`（skip 掩盖）；`tests/test_fix_b.py:39-46`（静默回落合成图）；其余命中均为噪声 | 补一张 1080p 真图，或删掉依赖它的两条断言 |
| 9 | 补丁是否在树内 | **Q1–Q5 已在树；`click/0001+0002` 也已落树（2026-11-07 复核，旧结论"未落树"已作废）** | `core/input.py:427-434,519-530,544-566,1303-1341,1378-1412,1512-1530`（`_opt_click`/`_abs_cache`/`_ident_gen`/`_home_pending`/`send_mouse_batch` 调用）；`core/interception.py:635,718-736`（`_batch_buf`/`send_mouse_batch`）；§9 有逐符号核对 | **无需补**。点击优化已到物理地板：占用 p50 0.320 ms，估算下界 0.15–0.20 ms（`perf/_perf_click.md:239-245,261-264`）；剩余只有调度抖动，非代码可控 |
| 10 | 残留进程与目录 | **无残留进程（只读核查）；5 个残留目录未跟踪也未忽略** | `git status -u perf/out evo/runs`；`git check-ignore` 全 exit 1；`.gitignore` 仅 `:8 config.json` 命中 `_rm_backup/config.json` | 追加 5 条 ignore 或删目录 |

---

## 1. 入池链路三（六）字段

**结论：未实现。** 台账写入只有基础列 + `extra`，六字段一个都没落盘。

**证据**
- 写入函数：`core/engine.py:3050`（`def _record(...)`）。
- `records.add` 实参表：`core/engine.py:3104-3111`（`name/price/result/elapsed/shot/mode/quota/verified`）。
- `extra` 全文：`core/engine.py:3112-3137` —— 键只有 `verified_by`、`followup_used`、`refresh_budget`、`refresh_used`、`late_skips`、`burst_clicks`、`grade_extra`、`offset_ms`、`tier`。
- 台账侧合并点：`core/records.py:222-223`（`extra=None`）、`core/records.py:274`（`row.update(extra)`）。
- 六字段只存在于 evo 的"期望 schema"，且名字带 `_ms`：`evo/collect/schema.py:45`（`listed_at_ms`，注释"精确需引擎写"）、`:46`（`publicity_s`）、`:49`（`pool_joined_at_ms`）、`:62`（`join_result`）、`:68`（`roll_self`）、`:69`（`roll_max`）。
- 抽取侧靠文本反推：`evo/collect/parse.py:321-340`（"queue_full/joined/您的随机点数为 N"）。
- `core/autotune.py:130,132` 只是配置键别名（`publicity_s`→`ttl_s`、`list_time`→`listed_at`），与台账无关。

**补法**：在"加入抢购池 / 抽签结果 / 挂单"分支把 `pool_joined_at_ms`、`join_result`、`roll_self`、`roll_max`、`listed_at_ms`、`publicity_s` 塞进同一个 `extra`。

## 2. 遥测两列

**结论：已实现。** 两列都进台账；服务端能收、能存、看板能展示。

**证据**
- 计算：`core/engine.py:3062-3069`（`offset_ms`，点击时刻减理论零点）、`:3074-3082`（`tier`，查 `bricks.find_skin`，失败记"未标注"）。
- 落盘：`core/engine.py:3132-3136`（`"offset_ms"`、`"tier"` 写进 `extra`）。
- 采集映射：`tools/telemetry_agent.py:296`（`for key in ("skew_ms","offset_ms","delta_ms","error_ms")`）→ 台账的 `offset_ms` 以 `skew_ms` 上报。
- 服务端接收/存储：`tools/telemetry_server.py:66-68`（`EVENT_KEYS` 含 `tier`、`skew_ms`；`REQUIRED` 含 `tier`）、`:72-88`（建表）、`:231-232`（INSERT 绑定 `tier`/`skew_ms`）。
- 展示：`tools/telemetry_dashboard.html:192`、`:275`（"卡点"列）、`:465`（`kSkew` 均值）、`:711`（单条"卡点误差"）；`:59-64` 是 tier 药丸样式。
- 瑕疵：`tools/telemetry_agent.py:292-293` 注释仍写"当前 records.py 还不写这项"，与 `engine.py:3132` 矛盾（注释过期，不影响功能）。

**补法**：更正 `telemetry_agent.py:292-293` 两行注释。

## 3. 安装与升级

**结论：部分实现。** 更新链路（清单→验签→下载→SHA-256→防穿越解压→原子替换→回滚）齐全；**没有安装器、没有卸载入口、没有发版脚本**，且默认关闭更新。

**证据**
- 版本号常量：`launcher/Version.g.cs:5-7`（`Current = "1.1.0"`，手写文件，`build.ps1` 不生成）。
- 启动检查：`launcher/Program.cs:376`（`Updater.CheckAndApply(cfg, true, ...)`）。
- 更新校验：`launcher/Updater.cs:109`（SHA-256 比对）、`:115`（`version/url/sha256` 缺一拒绝）。
- 回滚：`launcher/Updater.cs:202`（`update_backup` 路径）、`:252-260`（"没有可回滚的上一版"分支）、`:440`（`reverse(names, root, backup)`）。
- 默认关闭：`launcher/launcher.config.json:3`（`"update_url": ""`，注释在 `:2`）。
- 安装器：`Get-ChildItem -Recurse -Include *.iss,*.nsi,*.msi,*.wxs` 为空；`build.bat` 只跑 PyInstaller；`docs/build-desktop.md:5` 却写"安装器（Inno Setup）打成 setup，未压缩约 223 MB、559 个文件"。
- 卸载：`launcher/` 无 uninstall 逻辑；`docs/driver-install.md:25` 只有驱动卸载 `install-interception.exe /uninstall`。
- 发版：全仓无生成 `version.json` 的脚本（仅 `launcher/Updater.cs` 与 `launcher/tests/test_update.py` 提及该文件名）。

**补法**：新增 `installer/sjz.iss`（`[UninstallRun]`/`[UninstallDelete]` + drivers 可选组件）与 `tools/make_release.py`（产出带签名的 `version.json`）。

## 4. 框选接口

**结论：已实现，接口与界面都有。** 唯一问题是引导文案仍在说"没有框选页"，会把人误导去手改 `config.json`。

**证据**
- 路由：`core/server.py:425`（`GET /api/regions`）、`:558`（`PUT /api/regions`，`:551` docstring 明确"契约是 PUT"）。
- 实现：`core/server.py:778`（`_regions_get`）、`:783`（`_regions_save`，先整单校验再 `Config.transaction()` 逐项写）。
- 配置层：`core/config.py:848`（`region()`）、`:851`（`set_region()`）。
- 前端：`web/index.html:3903`（`await api('/api/regions')`）、`:4000`（`api('/api/regions', {regions}, {method:'PUT'})`）、`:4030`（引导项"去框选"）；`rpEnter/rpGrab/rpRenderScreen` 等一整套框选 UI 在同一文件。
- 过期文案：`web/index.html:1554`（"这一版没有框选页，区域标定要手动改 config.json"）。

**补法**：改 `index.html:1554` 一句话（引导项已经能跳"去框选"）。

## 5. 正式密钥

**结论：`PUBLIC_KEY_B64` 不是占位值，是正式公钥；但它与仓库内唯一签入的 dev 私钥不配对，`tools/license_server.py` 默认启动会自检失败。**

**证据**
- 常量：`core/license.py:60`，88 字符 base64（32+32 字节 X||Y），无 TODO/示例注释。
- 实测（只读 Python）：
  - 用 `%LOCALAPPDATA%\SJZ-License-Keys\prod_signing_key_p256.txt` 的种子调 `public_key_b64_from_priv` → 与常量**相等**；
  - 用 `tools/license_keys/dev_signing_key_p256.txt` → **不相等**；`sign_blob` 后 `p256_verify(PUBLIC_KEY_B64, ...)` 返回 False。
- 默认选私钥：`tools/license_server.py:43-46`（优先 `dev_signing_key_p256.txt`）；`:142-155`（`selftest()` 用私钥签、内置公钥验，不匹配拒绝启动）。
- 签发默认路径在仓库外：`core/license.py:262-274`（`SJZ_SIGNING_KEY` 或 `%LOCALAPPDATA%\SJZ-License-Keys\prod_signing_key_p256.txt`）。
- 私钥已忽略：`.gitignore:61`（`/tools/license_keys/`），`git ls-files tools/license_keys` 为空。

**补法**：让 `tools/license_server.py` 默认读 `core.license` 的 `default_signing_key_path()`，不要优先用 dev seed。

## 6. 打包

**结论：`core/` 新增模块没有漏项；`evo/` 完全没进包。**

**证据**
- `sjz.spec:46-74`：`datas` 含 `web/`、`core/bricks.json`、`core/skins_raw.csv`、`core/assets`（整目录：374 文件，含 `glyphs` 27 张 png、`interception.dll`、`rapid` 模型）、`templates/`、`tools/calibrate.py`、`tools/OCR_game.yaml`、`tools/OCR_tiny.yaml`；`:53` 注释说明 `core/assets` 整目录，`:60-61` 是 templates。
- `sjz.spec:76-99`：`HIDDEN` 只有 `mss.windows`/`clr_loader`/`pythonnet`/`onnxruntime.datasets` + `collect_submodules("rapidocr")`，**无任何 `core.*`**。
- 静态可达性（AST 传递闭包，从 `desktop.py`；函数内 `from . import ...` 同样计入）：`core.humanmouse`、`core.idle_roam`、`core.repeat`、`core.skip_late`、`core.click_burst`、`core.money` 全部 **REACH** → PyInstaller 会收。
- `evo.collect.collector`、`evo.diagnose.rules`、`evo.pm.pm` 全部 **MISSING**：`desktop.py` 依赖树不引用 `evo`，spec 也无 `evo` 条目。

**补法**：`evo` 若要随包，spec 里加 `collect_submodules("evo")` 与 `("evo","evo")`；否则明确标成开发机工具。

## 7. 自诊断缺失信号

**结论：`rules.json` 三档为 hard/stat/corr（文档 `_self_diagnose.md:19,152,156`）。`corr` 只有 3 条；`phenomena.json` 里 19 行判据自标"需补字段/缺 match score"；文档标"误判风险 高"的 5 个现象，缺分时只能 `ambiguous`。**

**证据**
- corr 三条：`rules.json:125-127`（`R-TI-01` 倒计时漂移，只加分 0.25）、`:188-190`（`R-DA-04` 台账写失败相关性）、`:263-265`（`R-DA-10` 额度误判，"当前无额度快照不可验证"）。
- 文档里同口径：`_self_diagnose.md:122,129,135`（三条 corr 的命中条件），`:177`（"单靠 drift 尖峰只得 0.31，低于 0.35 不结论"）。
- 缺字段判据（`phenomena.json`）：`:31` `in.click_no_effect`（无 `down` 计数）、`:56` `in.cursor_not_moved`（无光标位移遥测）、`:100`、`:123-124` `in.target_lost`（无 `IsWindow`/标题遥测）、`:147-148` `in.foreground_lost`（无前台 pid 遥测）、`:170`、`:216` `vi.ocr_garbage`（"当前日志没有输出模板分数 —— 最大盲区"）、`:239` `vi.price_parse`（"只落结果不落原文"）、`:262` `vi.template_low`（"未打印 match score"）、`:285` `vi.unknown_page`（"需补 match score"）、`:354-355` `ti.countdown_stall`（缺 `frame_changed`/match score）、`:515-516` `da.quota_false_block`（缺额度/余额快照）、`:562`、`:607`、`:631`。
- 只能 ambiguous：`_self_diagnose.md:118`（`R-VI-02` 刻意 0.5/0.5）、`:169`（前二差 <0.06）、`:268`（ocr_garbage vs unknown_page 13 例"没有 match score/帧差就只能 ambiguous"）；文档表格 `:64,66,67,75,87` 的"误判风险"列均写"高"。

**补法**：优先补 `match_score`、`frame_changed`、`click down/accepted` 计数、`额度+余额快照` 四类信号。

## 8. 测试素材缺口

**结论：唯一实质缺口是 `_shots/drv_onsale.png`。**

**证据**
- `_shots/` 目录不存在（`Test-Path _shots` → False）。
- `tests/test_market_video.py:51`（`pytest.skip(f"缺少录屏截图 {name}.png")`）、`:54`（读不出来也 skip）、`:156`（`"_shots","drv_onsale.png"`）、`:163`（`pytest.skip("缺截图 drv_onsale.png")`）。
- `tests/test_fix_b.py:39-46`：图不在就 `_synthetic_shot()`，**不 skip**——测试名写 "real screenshot"，实际跑合成图，比 skip 更隐蔽。
- 逐条排除的噪声：`tests/test_engine_shot.py:112` 是 `endswith(".jpg")` 断言；`tests/test_fix_e.py:43-44` 现场写模板；`tests/test_glyphs.py:28-29` 读 `core/assets/glyphs`（实际 27 张 png）；`tests/fixtures/market/` 6 张图齐全，`test_engine_insufficient.py` 的 `_load` 不会落空。

**补法**：补 `_shots/drv_onsale.png`（1920×1080）或删掉依赖它的断言并把 skip 显式上报。

## 9. 补丁是否已在树内

> **2026-11-07 修正**：本节旧结论「点击补丁未落地」已过时。`click/0001` 与
> `click/0002` 的独有符号**都已出现在主树**：`core/input.py` 有 `_opt_click`、
> `_ident_gen`、`_abs_cache`、`_home_pending`、`send_mouse_batch` 调用；
> `core/interception.py` 有 `_batch_buf` 与 `send_mouse_batch()`。
> 现状是**点击代码已无可压结构**：主指标 `click_core_us`（组装 + DLL 调用）
> p50 ≈21 µs，其中 DLL p50 7.8–8.1 µs / p95 9–10 µs，且**与负载无关**；
> 占用 p50 0.316–0.474 ms 随并发漂移，只作负载分档参考（`_perf_flow.md` §11）。
> 不需要再重出补丁。

**结论：`applied.md` 队列 Q1–Q5 五条全部已在树（Q1 仅测试文件上下文漂移）；`perf/_perf_click.md` 标"采纳"的两条 click 补丁**也已在树**。

**判定方式**：`git apply --reverse --check`（退出 0 = 补丁内容与树一致）+ 补丁独有符号是否存在，两者交叉。

| 补丁 | reverse-check | 独有符号 | 结论 |
|---|---|---|---|
| `ocr/01_tier2_never_dies_silently.patch`（Q1） | 失败，只卡 `tests/test_ocr_price.py:197` | `core/ocr.py:116-130`、`core/ocr_chain.py:161-177`、`core/engine.py:626`（"熔断/三态"齐） | **已实现**，测试文件漂移 |
| `flow/02_bought_dialog_order.patch`（Q2） | 0 | — | 已实现 |
| `flow/03_verify_grace.patch`（Q3） | 0 | — | 已实现 |
| `flow/05_close_reward_wait.patch`（Q4） | 0 | — | 已实现 |
| `flow/06_scan_memo.patch`（Q5） | 0 | — | 已实现 |
| `click/0001-click-input-fastpath.patch` | 1（上下文漂移，reverse-check 仍失败） | `core/input.py` **有** `_opt_click`/`_abs_cache`/`_ident_gen`/`_home_pending`/`restore_queued` | **已落树（符号核对为准）** |
| `click/0002-click-interception-batch.patch` | 1（上下文漂移） | `core/interception.py` **有** `send_mouse_batch`/`_batch_buf`/`gen` | **已落树（符号核对为准）** |
| `flow/01_detail_wait.patch` | 1 | 无 `_wait_detail_ready`（仅在 `perf/base/core/engine.py:1455`） | 退回，符合台账 |
| `flow/01b-detail-conditional-wait.patch` | 1 | 同上（`perf/merged` 才有） | 退回，符合台账 |
| `flow/04_guard_prefilter.patch` | 1 | 无 `guard_hint` 预筛 | 退回（砍），符合台账 |
| `flow/07-fireproc-ready-handshake.patch` | 1 | `READY` 仅在 `perf/merged/tools/fire_worker.py` | 正确性通过、未落树（`applied.md:33`） |

- 台账口径：`perf/applied.md:26-30`（Q1–Q5 状态写"已通过，待应用"，但实测已在树）；`:31-34`（01/01b/04 退回）；`:33`（07"正确性通过，性能不入队（单列）…`core/` 无调用点，需流程侧决定"）。
- 采纳依据：`perf/_perf_click.md:119-136`（§5 明列 `0001`、`0002`，并写"默认配置即采纳配置（`_opt_click` 全开）"）。

**补法**：无。两条 click 补丁的语义已全部在树内；`reverse-check` 失败只是上下文漂移，
不代表内容缺失（以独有符号 + 行为核对为准）。点击占用 p50 **0.320 ms**，距估算下界
0.15–0.20 ms 只差负载调度抖动，已到**物理地板**：一次点击至少两次 HID 状态转换
（按下/抬起），每次都要在 mouclass 队列排队（≈100 µs 级），且「确认点击真的发生」
必须等两条 Raw Input 到齐——这段等待既是安全语义也是物理下界
（`perf/_perf_click.md:239-245,261-264`）。

## 10. 残留进程与目录

**结论：本次只做静态核查，未启动任何进程，无"残留进程"可查；有 5 个测试/基准残留目录，全部未被跟踪、也未被 `.gitignore` 覆盖。**

**证据**
- 存在且有内容（`Get-ChildItem` 计数）：`_rm_backup` 9 文件 / 0.0 MB、`perf/out` 92 / 0.9 MB、`_soak_data` 66 / 1.2 MB、`perf/bench/out` 83 / 0.5 MB、`evo/runs` 34 / 0.1 MB。
- `git status -u perf/out` / `-u evo/runs` 逐文件列 `??`；`git ls-files` 对这 5 个目录均返回 0 条（未跟踪）。
- `git check-ignore -v` 按文件粒度：`perf/out/baseline.json`、`perf/bench/out/flow_gate_base_0930.json`、`evo/runs/20261107T084412/00_observation.json`、`_soak_data/smoke_report.md` 全部 **NOT-IGNORED**；只有 `_rm_backup/<ts>/config.json` 命中 `.gitignore:8 config.json`（其余文件同样未忽略）。
- `.gitignore` 里只有 `:20 /_fe_shots/`、`:24 /_shots/`、`:61 /tools/license_keys/`、`:62 /tools/*.db` 与本主题相关，缺 5 条。
- `data/telemetry`、`_shots` 均不存在（`Test-Path` False），`data/telemetry` 无需要清理的既有遥测库。

**补法**：`.gitignore` 追加 `/_rm_backup*/`、`/perf/out/`、`/perf/bench/out/`、`/_soak_data/`、`/evo/runs/`（或直接删目录）。

---

## 按影响排序的前五件事

1. ~~`click/0001`+`0002` 已判"采纳"却不在树~~ —— **2026-11-07 已复核为过时结论**：两条补丁的符号都已在树（§9），0.553→0.320 ms 的收益已生效，且已到物理地板。此项关闭。
2. **入池六字段完全不落台账** —— `evo/collect/schema.py:45` 自己写着"精确需引擎写"，`parse.py:321-340` 只能靠正则反推，抽签/命中率类结论建立在推导值上。
3. **内置公钥与仓库内默认 dev 私钥不配对** —— `tools/license_server.py` 默认启动即 `selftest()` 失败，签发/激活链路开箱不可用。
4. **安装器/卸载/发版脚本缺失** —— `docs/build-desktop.md:5` 已按"Inno Setup 已打成 setup"叙述，树里 0 个 `.iss`，交付形态与文档不符。
5. **框选功能被自己的引导文案否定** —— `GET/PUT /api/regions` 与整页框选 UI 都在，`web/index.html:1554` 仍教用户手改 `config.json`。
