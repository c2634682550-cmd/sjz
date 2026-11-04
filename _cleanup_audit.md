# dafors 清理审计（只读取证）

- 仓库：`C:\Users\26346\Desktop\project\dafors` ｜ 远端 `c2634682550-cmd/sjz` ｜ 分支 `main` ｜ HEAD `cf91c04 修复第三轮代码审查发现的 P0/P1（配置/调度、视觉/OCR、输入/交易、引擎四路）`
- 基线：`git ls-files` = **152** 个 tracked 文件；工作区另有 **14 个 tracked 文件已修改未提交**（`core/config.py`、`core/input.py`、`core/nav_ons.py`、`core/scheduler.py`、`core/server.py`、`tests/*` 8 个、`web/index.html`）+ 3 个 untracked 报告（`_review_*.md`）。
- 本次全程只读：未执行 `add / commit / checkout / reset / stash / clean`，未改任何源码。唯一写出的文件是本报告。
- **并发提醒**：审计期间另一会话正在向工作区写入 R4/R5 修复（`core/input.py` +13、`core/nav_ons.py` +111、`core/server.py`、`core/scheduler.py`、`core/config.py` 等 14 个 tracked 文件）。下表的行号已按**最新工作区**复核，`nav_ons`/`input` 的"0 引用"结论也已重跑确认；执行删除前请再跑一次 `git status` 对齐。

## 扫描口径（证据可复现）

- **引用扫描范围**：`core/`、`tests/`、`tools/`、`docs/`、`web/`、`templates/` 下全部 `.py/.md/.html/.json/.yaml/.bat/.csv`，加根目录 12 类文件（`*.py/*.bat/*.toml/*.spec/*.json/*.yaml/*.md`）。**不含** `.venv`、`.venv-vl`、`.git`、`_reverse`、`_shots`、`_probe`、`_re` 等产物目录内部。
- **匹配方式**：文件名 / 模块名的字面量匹配（含注释、docstring、文档），再对死代码符号做 `\bNAME\b` 词边界复核。
- **AST 分析范围**：`core/*.py`（34 个文件 13588 行）、`run.py`（55 行）、`desktop.py`（317 行）；测试与工具仅作为引用语料。
- 豁免项：`__all__` 导出、`@property/@staticmethod/@classmethod/fixture`、`if __name__` 入口、HTTP 基类回调（`log_message`、`do_*`）、`getattr` 动态分发。结论：`core/` 内 **无 `TYPE_CHECKING` 块、无 `__all__`、无 `try/except ImportError` 兜底**（已全量 grep 确认）。

---

## ① 结论摘要

| 指标 | 数值 |
|---|---|
| 可删/可隔离文件 | **约 1899 个** |
| 可删/可隔离体积 | **约 2.05 GB**（`_reverse` 1.61 GB + `_shots` 调试截图 205.8 MB + 其余 0.24 GB） |
| 可立即删除的死代码 | **≈380 行**（含 `core/weapons.py` 整模块 169 行） |
| 仅测试引用、需确认后退役 | **156 行**（`select_skin` 67 / `card_regions` 65 / 其余 24） |
| 不可达分支 / 重复 except / 注释旧代码 | **0 / 0 / 0** |
| 风险最高的一项 | **整目录删除 `_shots`**：`tests/test_fix_b.py:73,120` 是硬断言（缺图 = fail 而非 skip）；`tools/build_bricks.py:27` 默认从 `_shots/gun_map.json` 读输入 |
| 体积陷阱 | **`.venv-vl`（4.23 GB）不能删**：`core/ocr_service.py:430` 启动 `.venv-vl\Scripts\paddlex.exe`，删掉 OCR 全挂 |

候选里明确不存在、无需处理：`_full.log`、`.pytest_cache/`、venv 之外的 `__pycache__/`（均已逐一 Test-Path 确认）。

---

## ② 文件表

> tracked = 是否被 git 跟踪；"untracked" 若同时被 `.gitignore` 命中会注明规则行号。

### 2.1 根目录一次性脚本（21 个）

统一证据：21 个模块名在扫描范围内 **全部 0 处引用**（唯一命中是同一批待删文档 `_review_input.md:148` 提到 `_click_fhdd.py`）。无 `import _xxx`、无 `from _xxx import`、无字符串路径、未被 `build.bat`/`sjz.spec`/`pyproject.toml` 使用（构建链只认 `desktop.py` + `sjz.spec`）。

| 路径 | tracked | 大小 | 引用证据 | 判定 |
|---|---|---|---|---|
| `_apply_gunmap.py` / `_buy_dczc.py` / `_calib_pubwatch.py` / `_click_fhdd.py` / `_click_price.py` / `_click_price2.py` / `_close_panel.py` | untracked（`.gitignore:22` `/_*.py`） | 3.2 / 3.0 / 10.4 / 1.7 / 3.9 / 3.4 / 1.7 KB | 0 处 | **可删** |
| `_collect_guns.py` / `_collect_skins.py` / `_confirm_buy.py` / `_filter_pubonly.py` / `_kill_and_buy.py` | 同上 | 4.5 / 7.3 / 3.6 / 2.9 / 4.2 KB | 0 处（`_collect_guns.py:15`、`_collect_skins.py:12` 只在自己 docstring 里写产物路径） | **可删**（产物 `_shots/gun_map.json`、`skin_catalog.json` 另判保留） |
| `_recon_market.py` / `_recover_market.py` / `_refresh_scan.py` / `_restore_scan.py` / `_scan_listings.py` | 同上 | 5.0 / 3.0 / 3.4 / 3.3 / 3.1 KB | 0 处 | **可删** |
| `_snipe2.py` / `_snipe3.py` / `_snipe_flow.py` / `_tab_dczc.py` | 同上 | 7.3 / 12.4 / 5.5 / 1.7 KB | 0 处 | **可删** |

合计 **21 个文件 / 94.4 KB / 2485 行**，全部 untracked，删除不影响 git 历史。

### 2.2 分析产物目录

| 路径 | tracked | 大小 | 引用证据 | 判定 |
|---|---|---|---|---|
| `_reverse\` | untracked（`.gitignore:30`） | **1611.91 MB / 1566 文件** | 唯一引用：`tools/verify_axon_report.py:21` 硬编码 `_reverse\axonlogin.dll`（该工具本身是 untracked 逆向脚本） | **可删**（连带该工具失效，见 §5） |
| `_shots\_probe_watch\` | untracked（`.gitignore:20`） | 234.91 MB / 78 文件 | 0 处 | **可删** |
| `_shots\_probe_elev\` | 同上 | 45.04 MB / 38 文件 | 0 处 | **可删** |
| `_shots\` 根目录（126 png / 25 txt / 3 json / 1 bak = 155 文件，497 MB 中根目录占 217 MB） | 同上 | 建议删 **118 个 png + 25 个 txt + 1 个 bak ≈ 205.8 MB** | 见 §2.3 保留集；其余 0 处引用 | **建议隔离**（标定证据，先移到仓库外观察） |
| `_probe\` | untracked（`.gitignore:31`） | 0.80 MB / 28 文件 | 0 处（`_probe_at`、`_probe_ok` 等是代码变量名，非目录引用） | **可删** |
| `_re\` | untracked（`.gitignore:29`） | 0.43 MB / 14 文件（`analysis_report.md`、`auth_protocol.md`、`shell_analysis.md` 等逆向报告） | 0 处 | **可删/归档**（内容已被 `_code_review_3round.md` 与 docs 取代） |

### 2.3 `_shots` 必须保留集（缺一即坏）

| 文件 | 大小 | 证据 |
|---|---|---|
| `_shots\drv_onsale.png` | 1334.5 KB | `tests/test_fix_b.py:73,120` **硬断言**；`tests/test_market_video.py:156`、`tests/test_server.py:111,135`、`core/cards.py:159` 注释引用 |
| `_shots\drv_sel.png` | 2570.5 KB | `tests/test_skin_ocr_real.py:81,96,125,140,150,196,223` |
| `_shots\drv_after_filter.png` | 2695.6 KB | `tests/test_skin_ocr_real.py:111` |
| `_shots\cat_S1_0.png` | 2593.2 KB | `tests/test_skin_ocr_real.py:117` |
| `_shots\n_02_filter.png` / `n_03_checked.png` | 456 / 456 KB | `tests/test_skin_ocr_real.py:160,161` |
| `_shots\drv_colfilter.png` / `drv_filter.png` | 418.4 / 944.8 KB | `tests/test_skin_ocr_real.py:163,174` |
| `_shots\gun_map.json` | 6.8 KB | `tools/build_bricks.py:27` 默认输入（重新生成 `core/bricks.json` 用） |
| `_shots\skin_catalog.json` | 21.3 KB | 仅 `_collect_skins.py:12` 写入；脚本删除后作为数据快照保留 |

合计 **8 张图 11.2 MB + 2 个 json 28 KB**。`test_skin_ocr_real.py:63-68` 对缺图是 `pytest.skip`（整组静默跳过），`test_server.py:141` 只要求 `_shots` 下存在任意 PNG —— 所以保留 `drv_onsale.png` 就能同时满足硬断言与 skip 门槛。

### 2.4 根目录杂物

| 路径 | tracked | 大小 | 引用证据 | 判定 |
|---|---|---|---|---|
| `apps.json` | untracked（`.gitignore:59`） | 3.5 KB | 0 处代码引用；`_code_review_3round.md:189` 结论"未被读取，疑似遗留" | **可删** |
| `cu.json` | untracked（`.gitignore:58`） | 11.4 KB | 0 处 | **可删** |
| `OCR.yaml` | untracked（`.gitignore:63`） | 1.2 KB | 0 处代码引用；真正被用的是 `tools/OCR_game.yaml`（`core/ocr_service.py:434`、`core/preflight.py:99`、`core/ocr.py:25`） | **可删** |
| `_bal.png` | untracked（`.gitignore:26` `/_*.png`） | 33.2 KB | 0 处 | **可删** |
| `desktop.log` | untracked（`.gitignore:11`） | 60.6 KB / 940 行 | `desktop.py:38` 运行时写入（日志，可再生） | **可删** |
| `ocr_service_8081.log` / `ocr_service_8082.log` | untracked | 1545.9 / 9.8 KB | `core/ocr_service.py:440` 运行时 `ocr_service_{port}.log`（可再生） | **可删** |
| `_review_input.md` / `_review_sched.md` / `_review_vision.md` | **untracked 且未 ignore**（`git status` 里 `??`） | 20.5 / 25.0 / 21.6 KB | 已被 `_code_review_3round.md:8,62,91,127` 合并引用（"三份均已合并，重复项已去重"） | **可删（需你确认是否留底）** |
| `_code_review_3round.md` | **tracked** | 25.3 KB | `tests/test_fix_c.py:5` 指明用例对应 I1~I22 | **必须保留** |
| `config.json` | untracked（`.gitignore:8`） | 6.0 KB | 用户运行时配置（服务端读写） | **必须保留** |
| `idapro.hexlic` | untracked（`.gitignore:57`） | 2.2 KB | 0 处引用，IDA 授权文件，非项目资产 | **需你确认**（删除会影响本机 IDA） |
| `build.bat` | **tracked** | 0.9 KB | `docs/build-desktop.md:10` 为唯一构建入口（sync → pytest → pyinstaller） | **必须保留** |
| `start.bat` / `sjz.spec` / `pyproject.toml` / `uv.lock` / `说明.txt` / `run.py` / `desktop.py` | tracked | — | 入口/依赖/文档 | **必须保留** |

### 2.5 环境与工具目录

| 路径 | tracked | 大小 | 引用证据 | 判定 |
|---|---|---|---|---|
| `.venv-vl\` | untracked（`.gitignore:3`） | **4229.13 MB / 23922 文件** | `core/ocr_service.py:430` 拼 `.venv-vl\Scripts\paddlex.exe`；`ocr_service.py:434` 用 `tools/OCR_game.yaml` | **必须保留**（OCR 服务本体） |
| `.venv\` | untracked（`.gitignore:2`） | 193.89 MB | 开发/测试环境（`build.bat` 用 uv，不走它；但本机 pytest 在用它） | **必须保留** |
| `data\` | untracked（`.gitignore:5`） | 34.66 MB / 553 文件（`records.jsonl` 24.9 KB + 179 张成交截图 28.88 MB + 170 个记录目录） | `ocr_ppocr.py:5` 用 `data/shots` 做评测；服务端读 `records.jsonl` | **必须保留（用户数据）** |
| `templates\`（`buy.png` 14.8 KB / `confirm.png` 16.8 KB） | untracked（`.gitignore:6`） | 0.03 MB | 运行时用户模板 | **必须保留** |
| `web\`（`index.html` 43.2 KB） | `index.html` 为 tracked（当前已修改未提交） | 0.11 MB | `sjz.spec:23` 打包 `web/` | **必须保留** |
| `.omp\` / `_bmad\` / `_bmad-output\`（空） | untracked | 0.56 MB | 代理工具元数据 | **建议保留**（清理可选） |
| `tools\` 内 22 个未跟踪逆向脚本（`re_axon.py`、`axon_dump.py`、`probe_mem.py`、`capture_auth.py` 等） | untracked | 约 0.5 MB | `verify_axon_report.py:21` 依赖 `_reverse`；其余 0 处引用 | **建议随 `_reverse` 一并隔离** |

---

## ③ 死代码表

### 3.1 可直接删除（约 380 行）

| 文件:行 | 符号 | 证据 | 建议 | 行数 |
|---|---|---|---|---|
| `core/weapons.py` 全文 1-169 | 整个模块 | 全仓 0 处 `import weapons` / `from .weapons` / `core.weapons`；`bricks.py` 里的 "weapons" 是数据键不是模块 | **删除整文件**（知识表估算已被 `bricks.json` 取代） | 169 |
| `core/nav_ons.py:430-466` | `NavOnsPanel.ons_ensure_block` | 0 处调用；`ons_find` 内联了同一套滚动循环；仅 `tests/test_nav_onsale.py:406,646` 注释提到 | 删除 | 37 |
| `core/nav_ons.py:88-115` | `_ons_block_at` | 0 处调用（`ons_ensure_block` 的旧依赖） | 删除 | 28 |
| `core/nav_ons.py:425-428` | `ons_scroll_bottom` | 0 处调用（docs 里有 API 说明，但服务端无对应路由） | 删除 | 4 |
| `core/nav_ons.py:159-161` | `ons_filter_reset` | 0 处调用，从未接线 | 删除 | 3 |
| `core/cards.py:89-117` | `_peaks` | 0 处调用（卡片纵向切分已改走 `card_regions`） | 删除 | 29 |
| `core/cards.py:120-128` | `_edges` | 0 处调用 | 删除 | 9 |
| `core/cards.py:26-29` | `ICON_BOX / NAME_BOX / GRADE_BOX / PRICE_BOX` | 0 处引用（实际 ROI 在 `nav_coords.py`） | 删除 | 4 |
| `core/bricks.py:89-102` | `knowledge` | 0 处调用（服务端无 `/api/bricks/knowledge` 路由，前端也不请求） | 删除 | 14 |
| `core/bricks.py:251-267` | `summary` | 0 处调用 | 删除 | 17 |
| `core/bricks.py:197` | `_GRADE` | 0 处引用 | 删除 | 1 |
| `core/nav_coords.py` 19 个常量 + 1 个函数 | `REC_TAB`(:67)、`ONS_PRICE_LABEL_Y`(:82)、`ONS_PRICE_TEXT_Y`(:90)、`ONS_RARITY_TEXT`(:115)、`ONS_CONDITION_TEXT`(:117)、`ONS_RARITY`(:118)、`ONS_CONDITION`(:119)、`ONS_MATERIAL_BOX`(:122)、`ONS_MATERIAL_Y`(:123)、`ONS_MATERIAL_X`(:124)、`ONS_MATERIAL_TEXT_X`(:125)、`ONS_MATERIALS`(:126)、`ONS_COLOR_Y`(:132)、`ONS_COLOR_X`(:133)、`ONS_COLOR_TEXT_X`(:134)、`ONS_COLORS`(:135-136)、`ONS_MORE_Y`(:146)、`ONS_MORE`(:147)、`ONS_PRICE_BOX`(:86)、`npoint`(:312-314) | 均在代码中 0 处引用；`ONS_PRICE_BOX` 只在 `docs/auto-buy-design.md:711` 出现 | 删除（矩形定义替代） | 23 |
| `core/nav.py:112-121` | 模块级 `click_px(host,...)` | 0 处调用；`nav_ons.py:47-50` 复制了同一逻辑；`NavBase.click_px`(:379) 是真正被 `getattr` 动态调用的那个，**保留** | 删除模块级函数 | 10 |
| `core/input.py:161-163` | `Mouse.move` | 0 处调用（移动统一走 `click`/`_send`） | 删除 | 3 |
| `core/switches.py:426-431` | `defaults()` | 0 处调用，docstring 写的"给 config.DEFAULTS 用"但 `config.DEFAULTS` 已是字面量 | 删除 | 6 |
| `core/screen.py:125-127` | `Screen.grab_norm` | 0 处调用 | 删除 | 3 |
| `core/actions.py:126-130` | `preflight_now` | 0 处调用；`core/server.py:631` 注释明确"直接调 preflight（而不是 actions.preflight_now）" | 删除 | 5 |
| `core/input.py:14-15` | `_LEFTDOWN / _LEFTUP` | 0 处引用（驱动路径不用这批 SendInput 标志） | 删除 | 2 |
| `core/input.py:428-429` | `_WM_MOUSEWHEEL / MK_LBUTTON` | 0 处引用 | 删除 | 2 |
| `core/hotkey.py:70` | `_WM_KEYUP` | 0 处引用 | 删除 | 1 |
| `core/interception.py:35,57,87` | `MAX_DEVICE / FILTER_NONE / _PRED` | 0 处引用（DLL 结构体字段声明不需要这些别名） | 删除 | 3 |
| `core/scheduler.py:42` | `POLL_IDLE` | 0 处引用 | 删除 | 1 |
| `core/engine.py:36` | `POPUP_N`（import 项） | 唯一未使用 import（其余 `from __future__ import annotations` 属编译器指令，不删） | 从 import 列表移除 | 1 |
| `core/input.py:18` vs `:425` | `_WM_MOUSEMOVE` 重复定义 | 同名同值定义两次（`_WM_LBUTTONDOWN/_WM_LBUTTONUP` :19 与 :426 同样重复） | 删除 425-426 的重复块 | 2 |
| `core/cards.py:202` | `_tile_from(spans,...)` 形参 | 函数体未用该形参，调用点 `:265` 仍在传 `cols` | 去掉形参 + 调用实参 | 1 |
| `core/market.py:112` | `parse_row(text, words=None)` 的 `words` | 全部 20+ 调用点都没传（`market.py:305,333,423` 与全部测试） | 去掉形参 | 1 |
| `core/input.py:499` | `click(..., hold=0.06)` 的 `hold` | 无任何调用方传 `hold` | 去掉形参 | 1 |
| `core/nav_ons.py:882` | `_ons_is_decoration(b, img)` 的 `img` | 函数体未用；唯一调用点仍传 `img` | 去掉形参 + 实参 | 1 |
| `core/nav_col.py:532` | `_click_season_item(text, y)` 的 `y` | 函数体未用；调用 `:457` 与 `tests/test_nav_filter.py:547` 都传 | 保留（改测试成本 > 收益） | 0 |

### 3.2 仅测试/文档引用 —— 需确认后随测试一起退役（156 行）

| 文件:行 | 符号 | 证据 | 建议 | 行数 |
|---|---|---|---|---|
| `core/nav_col.py:1116-1182` | `select_skin` | 生产路径已迁到 `skinfind.py`（`skinfind.py:6` 自述"以前这三件事散在 nav_col.select_skin"）；仅 `tests/test_skin_ocr_real.py:215,229` 与 docs 用 | 退役需同改 2 处测试；**保留待决策** | 67 |
| `core/cards.py:417-481` | `card_regions` | 仅 `tests/test_cards.py:53,78,95,102` | 保留（测试在覆盖真实行为） | 65 |
| `core/bricks.py:313-324` | `skins_of` | 仅 `tests/test_catalog.py:42`、`test_skin_api.py:46` | 保留 | 12 |
| `core/config.py:295-298` | `Config.save` | 生产改配置走 `_save_config`；仅 4 个测试调 | 保留（测试依赖） | 4 |
| `core/nav_coords.py:264-265` | `COL_SEASON_ITEM_Y0 / DY` | 仅 `tests/test_nav_filter.py`（断言 ==379 / ==48 的标定值） | 保留 | 2 |
| `core/nav_coords.py:267` | `COL_SEASON_VISIBLE` | 同上（断言 ==6） | 保留 | 1 |
| `core/nav.py:379-383` | `NavBase.click_px` | 生产经 `nav_ons.py:47` 的 `getattr(host,"click_px")` 动态调用 | **必须保留** | 5 |

### 3.3 其他静态检查结果

- **未使用的 import**：`core/` + 根目录仅 2 条 —— `core/engine.py:36 POPUP_N`（真死）、`core/switches.py:24 from __future__ import annotations`（编译器指令，保留）。测试/工具侧另有 `pytest`、`json`、`glyphs`、`struct`、`re` 等 8 条，不影响主程序。
- **不可达分支**：0 处（无 `if False` / `while 0` / `return` 后语句 / 重复 `except`；脚本已扫全部 `Try` 节点）。
- **被注释掉的旧代码**：0 块。50 处 ≥4 行的连续注释全部是中文设计说明（如 `nav_coords.py:69-79` 的实测标定记录），不是可还原的代码。
- **重复定义**：`core/input.py:18` 与 `418` 的 `_WM_MOUSEMOVE`；测试侧 `tests/test_server.py:280,318` 的 `_post`、`tests/test_switches.py:296,354` 的 `_fake_sched`（同一文件内后定义覆盖，需确认是否为有意的分支版本）。

---

## ④ 绝不能动的清单

| 路径 | 理由 |
|---|---|
| `core/assets/interception.dll`（11 KB） | 输入驱动注入本体，`sjz.spec:43` 显式打包；缺失 → 自动退 PostMessage，点击变慢且需要前台 |
| `core/bricks.json`（97.6 KB） | `sjz.spec:29` 打包；缺 → 砖皮识别静默空库 |
| `core/skins_raw.csv`（42.2 KB） | `sjz.spec:33` 打包；缺 → 皮肤只能全库扫描，慢十倍 |
| `core/assets/glyphs/*.png`（22 张） | `sjz.spec:38` 打包；缺 → 小字 OCR 退回网格推断 |
| `.venv-vl\`（4.23 GB） | `core/ocr_service.py:430` 的 `paddlex.exe` 唯一来源 |
| `.venv\` | 本机测试/运行环境 |
| `data\records.jsonl`、`data\shots\`（28.88 MB，179 张成交截图）、`data\<hash>\` | 真实成交记录与截图，属用户数据 |
| `templates\buy.png`、`templates\confirm.png` | 用户框选的点击模板，运行时生成 |
| `web\index.html`（当前有未提交修改） | 前端唯一入口，`sjz.spec:23` 打包 |
| `config.json` | 用户运行时配置 |
| `build.bat`、`start.bat`、`sjz.spec`、`pyproject.toml`、`uv.lock` | 构建/启动链 |
| `_code_review_3round.md`（tracked） | 第三轮审查结论，`tests/test_fix_c.py:5` 以此编号 |
| `tools/OCR_game.yaml`、`tools/build_bricks.py`、`tools/bricks_source.csv` | OCR 管线配置 + 砖皮数据源 |

---

## ⑤ 执行建议

**顺序（从零风险到需确认）：**

1. **开工前固定基线**：确认 13 个已修改的 tracked 文件是否要保留（本审计基于当前工作区）。给 untracked 目录做移动备份而非直接 `rm`：`move _reverse D:\archive\dafors\...`。
2. **第一批（纯 untracked、0 引用、可再生）**：21 个根 `_*.py` → `_probe\` → `_re\` → `desktop.log` + `ocr_service_80*.log` + `_bal.png` → `apps.json` / `cu.json` / `OCR.yaml`。约 1 MB。
3. **第二批（大体积）**：`_reverse\`（1.61 GB）、`_shots\_probe_watch\` + `_probe_elev\`（280 MB）。删前先确认 `tools/verify_axon_report.py` 是否还要用。
4. **第三批（需人工确认）**：`_shots` 除 §2.3 保留集外的 118 png + 25 txt（205.8 MB）、3 份 `_review_*.md`、`idapro.hexlic`、`tools\` 里 22 个逆向脚本。
5. **第四批（代码）**：按模块删 §3.1 —— 建议顺序 `weapons.py` → `nav_ons.py`(72 行) → `cards.py`(42 行) → `bricks.py`(32 行) → `nav_coords.py`(23 行) → 零散常量/形参。每批一次测试。

**每批后的验证：**

```powershell
uv run pytest tests/ -q
uv run python -c "import core.engine, core.server, core.scheduler, core.nav_ons, core.cards, core.bricks"
uv run python run.py --doctor          # 输入驱动 / OCR / 前台三项自检
uv run pyinstaller sjz.spec --noconfirm # 只在删了 core/ 下文件时跑，确认 datas 仍齐
```

**必须先由你确认的 3 项：**

1. **`_shots` 的 118 张历史截图 + 25 个 txt**：是坐标标定的一次性证据，还是需要长期留档？（保留集 10 个文件无论如何不能动）
2. **`core/weapons.py` 整模块（169 行）**：`bricks.json` 已覆盖枪种/筛选表，确认它是被取代而非"待接入"。
3. **`core/nav_col.py:1116-1182 select_skin`（67 行）**：确认 `skinfind.py` 已完全接管，可连同 `tests/test_skin_ocr_real.py:215,229` 两个用例一起退役。

**不要做**：`git clean -fd`（会把 `data\`、`templates\`、`web\index.html` 的未提交修改一起清掉）；删除 `.venv-vl`（OCR 立即不可用）。
