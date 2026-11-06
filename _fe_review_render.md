# dafors 前端真实浏览器渲染审查

审查对象：`C:\Users\26346\Desktop\project\dafors`（origin `https://github.com/c2634682550-cmd/sjz`，分支 `main`）
审查日期：2026-11-06　审查方式：本地起服务 + 真实 Chromium 渲染（未改业务代码、未 commit、未 push）

## 一句话结论

三种视口下布局都没有破损、首屏零报错、零 404，交互链路（自检/校准/启停/开关）都能走通；**最需要先修的是两件事：抢购条件完全没有校验且非法值会被静默写进配置（例如最低价高于最高价、数量 0），以及桌面壳在缺少 WebView2/pywebview 时会退化成默认浏览器标签页、在没有浏览器时则静默挂住——不满足“必须独立窗口”的硬约束。**

最关键三条：

1. **P2 条件零校验**：`最低价 5000 > 最高价 100`、`数量 0` 都能保存成功，界面无红框、无提示、无 `aria-invalid`，后端 `float()` 照收（`web/index.html:757-783`、`core/server.py:382-396`）。这会让调度器带着“永不成交/超额”的条件入库。
2. **P2 独立窗口会退化**：`.venv` 里 pywebview 缺失或 WebView2 初始化失败时走 `_open_browser()`（`desktop.py:240-243`、`294-303`），而 `webbrowser.open()` 失败被吞掉（`desktop.py:94-99`），进程转入 `_serve_until_interrupt()`，在无浏览器机器上表现为“双击后什么都没有、端口被占”。
3. **P2 键盘盲区**：自定义开关 `.swtog` 是纯 `div`（无 `tabindex`/`role`/键盘事件，`web/index.html:199-204,876-885,939`），日志折叠头 `.loghead` 也是 `div` 点击（`web/index.html:339`），Tab 永远到不了；按钮无可见焦点环（13_keyboard_focus.png）。

## 基线与环境

| 项 | 值 |
|---|---|
| 分支 | `main` |
| 最近提交 | `62d5e57 2026-11-05 02:58:16 +0800 test: 同步既有测试与审查/清理记录` |
| `git status` | 工作区无已跟踪改动；仅有两个既有未跟踪文档 `_fe_review_contract.md`、`_fe_review_ui.md`（保持原样未动） |
| 服务启动 | 实测 `python run.py --port 8900 --no-browser`（内部 `serve(host, port)`），日志确认 `SJZ 控制台已启动 -> http://127.0.0.1:8900` |
| 端口探测 | 8900–8999 全部空闲；8791 属于 WorkBuddyAI，与本项目无关 |
| `GET /` | `200`，HTML 含 `SJZ · 三角洲抢购` 页面节点 |
| 浏览器 | 本机未安装 Google Chrome（Edge 可用）。用 Playwright 内置 Chromium 与 `chrome-devtools` MCP 对齐启动；**控制台/网络取证走 MCP**，截图与交互矩阵由 Playwright 驱动同一 Chromium 内核 |

## 实测项清单

| # | 实测项 | 结果 | 证据截图 |
|---|---|---|---|
| 1 | `GET /` 200 且含页面节点 | 通过 | 01_first_load_1920x1080.png |
| 2 | 首屏控制台 error/warning | 通过：MCP 读到 0 条 error/warn，仅 2 条无障碍 issue 提示 | 01 |
| 3 | 首屏网络 404/失败 | 通过：10/10 请求 200（`/`、`/api/skins`、`/api/switches`、`/api/config`、`/api/scheduler`×5、`POST /api/preflight`） | — |
| 4 | 1920×1080 布局 | 通过：无横向滚动、无元素越界、无同层重叠 | 01 |
| 5 | 1366×768 布局 | 通过：筛选行不换行、无越界 | 02_1366x768.png |
| 6 | 1280×720 布局 | 通过：按钮/状态栏均在可视区 | 03_1280x720.png |
| 7 | 设置页三视口（自检胶囊换行） | 通过：1920 单行、1366/1280 两行，与按钮间距 8px，无重叠 | 21_settings_1920x1080.png / 21_settings_1366x768.png / 21_settings_1280x720.png |
| 8 | 自检（重新检查） | 通过：`POST /api/preflight` 200，7 项胶囊可读，未通过项红色标注“游戏页面 没找到游戏窗口” | 05_preflight_result.png |
| 9 | 时间校准 | 通过：`POST /api/timesync` 200，偏差 <1ms“已补偿” | 06_timesync_result.png |
| 10 | 开关保存 | 通过：点击 `.swtog` 后状态保持、无 console 错误（保存走 `/api/switches`） | 07_settings_switches.png |
| 11 | 开始（演练模式） | 通过：按钮文案变“开始抢购（演练）”，点击后禁用态正确，错误文案可读 | 08_run_before_start.png / 09_after_start.png |
| 12 | 停止 | 通过（未启动时 `#btnStop` 为 disabled，不会误点） | 09_after_start.png |
| 13 | 错误路径：非法条件保存 | **不通过**：最低价 5000 > 最高价 100 仍保存成功，无红框/无提示/无 `aria-invalid` | 11_invalid_values.png |
| 14 | 数量 0 的非法输入 | **不通过**：`checkValidity()=false` 但界面不提示，仍被写入 `console.quota=0` | 17_quota_zero.png |
| 15 | 超长名称/超长价格 | 通过：`.name` 省略号生效，表格未撑破（`tablewrap sw==cw`） | 15_long_text_frozen.png |
| 16 | 超长日志行 | **不通过（局部）**：日志区内部横向滚动 22833px vs 可视 1890px | 15_long_text_frozen.png |
| 17 | 队列页超长目标名 | **不通过（局部）**：名称被硬裁切、无省略号（auto 表格下 `max-width:420px` 未产生 ellipsis） | 16_queue_long_text.png |
| 18 | Tab 顺序 | 通过：顶栏页签 → 目标/启停 → 筛选 → 表单，顺序合理 | 13_keyboard_focus.png |
| 19 | 键盘焦点可见性 | **不通过**：按钮 outline 0px / 同底色，Tab 到按钮看不见焦点；输入框有蓝框 | 13_keyboard_focus.png |
| 20 | Enter 是否误提交 | 通过：页面无 `<form>`，Enter 未产生额外 POST（仅 600ms 防抖保存） | — |
| 21 | 长文本提示条 | 通过：单行省略号，不撑破顶栏 | 15_long_text_frozen.png |
| 22 | 桌面壳（pywebview）窗口 | 通过：无系统标题栏、右上角“—/×”渲染（`pywebviewready` 桥接生效）、页面布局正常 | 20_desktop_printwindow.png |
| 23 | 关窗 → 进程/端口 | 通过：WM_CLOSE 后进程退出，端口无 LISTEN 残留，日志“窗口已关闭，退出” | 20 + `desktop.log` |
| 24 | 单实例保护 | 通过：第二次启动输出“已经有一个实例在跑，已把它的窗口提到前面”并 `exit 0` | — |

## 问题清单

| 级别 | 位置 | 现象 | 截图证据 | 修复方向 |
|---|---|---|---|---|
| **P2** | `web/index.html:757-783`；`core/server.py:382-396` | 条件无任何校验：`min_price>max_price`、`quota=0`、负数都能 `POST /api/config` 成功；后端 `float()` 直接入库，后续 `POST /api/scheduler/start` 会按非法条件建任务 | 11_invalid_values.png、17_quota_zero.png | 前端保存前做区间校验（min≤max、quota≥1、价格≥0）并标红 + 提示；后端 `_CONSOLE_NUM` 写入时加范围钳制，拒绝时返回 400 与字段名 |
| **P2** | `desktop.py:240-243`、`294-303`、`94-99` | 缺 pywebview / WebView2 初始化失败时退化为默认浏览器标签页；`webbrowser.open()` 的 False 返回值被吞，无浏览器机器上进程转入 `_serve_until_interrupt()` 静默挂起（看门狗 180s 才退） | 无（静态 + 日志分支） | 删除浏览器回退或改成“必须独立窗口”的硬失败对话框（附 WebView2 安装指引）；兜底也只允许 `msedge --app=...` 无地址栏窗口，且必须检测返回值 |
| **P2** | `web/index.html:199-204,876-885,939`；`web/index.html:339` | `.swtog` 与 `.loghead` 是纯 div：无 `tabindex`、无 `role`、无 Space/Enter 处理，键盘用户无法切换开关/折叠日志 | 13_keyboard_focus.png（Tab 序列里不出现开关） | 给可点击 div 补 `role`/`tabindex="0"` 与 keydown（Space/Enter），或改用原生 `input[type=checkbox]` + label |
| **P3** | `web/index.html:92-103` | `.btn` 没有 `:focus-visible` 样式；实测按钮 `outline-width: 0`，Dark 主题下键盘焦点不可见 | 13_keyboard_focus.png | 加 `:focus-visible{outline:2px solid #4c8dff;outline-offset:2px}`，或用与 hover 不同的环 |
| **P3** | `web/index.html:167-171`（`.logbox` 仅 `overflow-y:auto`） | 超长无空格日志在日志区产生横向滚动（`scrollWidth 22833`），内容被右侧裁切 | 15_long_text_frozen.png | 加 `overflow-wrap:anywhere` / `word-break:break-all`，或 `overflow-x:hidden` + `white-space:pre-wrap` |
| **P3** | `web/index.html:136`、`651-657` | 队列/记录名称列在 auto 表格布局下 `max-width:420px` 不产生省略号，长名被硬裁切（半个字） | 16_queue_long_text.png | 给名称单元格内层加 `display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap`，或用 `table-layout:fixed` |
| **P3** | `web/index.html` 表单区（整页 36 处） | CDP issue：6 个表单字段无关联 label、30 个字段无 `id/name`；读屏与表单语义不完整 | MCP console issue（msgid 1-2） | 为每个 input/select 补 `id` 与 `<label for>`，或补 `aria-label` |
| **P3** | `web/assets/df-bg.jpg`(63KB)、`df-logo.png`(5.5KB) | 页面从未引用（`index.html` 无 `assets/`、无 `url()`、无 favicon），网络请求也没有 `/assets/*`；同时缺 `<link rel="icon">`，标签页/任务栏图标空白 | —（静态检查 + 网络清单） | 删掉两个死资源，或用 `df-logo.png` 作 favicon 与窗口图标 |
| **P3** | `desktop.py:267-273` | 窗口无图标参数；无尺寸记忆（每次 1180×800）；无最大化按钮；实测内容区 1164×761（与设定差 16/39px，疑似阴影/DPI，需在 125% 缩放复核） | 20_desktop_printwindow.png | `webview.start(icon=...)` + `.ico`；关闭时把宽高/位置写入配置并在启动时校验屏幕范围；顶栏补最大化按钮或双击标题区最大化 |
| **P3** | `web/index.html:236` | 整个顶栏是 `pywebview-drag-region`，页签、启停胶囊、`.winctl` 都在拖拽区内，部分 pywebview 版本存在“按下按钮被当成拖窗”的吞点击风险 | 20_desktop_printwindow.png | 只给空白标题区加拖拽类，或把 `.tabs/.pills/.winctl` 排除出拖拽区 |

## 独立窗口可行性

### 1. 现在实际怎么起页面

- `start.bat:27` → `start "" ".venv\Scripts\pythonw.exe" desktop.py`（uv/系统 Python 两条后备也走同一入口，`start.bat:35,39`）；**没有 `start http://...` 弹默认浏览器**。
- `desktop.py:221` 生成 `http://{host}:{port}`，`desktop.py:267-273` 用 `webview.create_window(WINDOW_TITLE, url, width=1180, height=800, min_size=(900,620), frameless=True, easy_drag=False, shadow=True, js_api=ctl)` —— **加载 URL，不是 file://**；服务在 daemon 线程里跑（`desktop.py:225`），主线程交给 pywebview。
- `run.py:56` 在非 `--no-browser` 时会 `webbrowser.open(url)` —— 这是开发入口，仍是浏览器标签页形态。
- 会退化成浏览器标签的地方：`desktop.py:240-243`（pywebview 导入失败）与 `294-303`（WebView2 初始化失败，`start()` 在窗口显示前返回）。

### 2. 没有任何浏览器 / 没有 WebView2 的机器

- **有 pywebview、无 WebView2**：`create_window` 能返回，`webview.start()` 立刻返回且未触发 `shown` → 走 `_open_browser(url)` + `_serve_until_interrupt()`。有默认浏览器就是标签页（违反硬约束）；没有任何浏览器时 `webbrowser.open()` 返回 False 被 `except` 吞掉，进程继续跑服务，窗口/提示都没有，只剩 180s 空闲看门狗或手动杀进程（`desktop.py:317-356`）。
- **无 pywebview**：导入即失败（`desktop.py:28-30`），直接命中同一条浏览器回退路径，控制台日志会写“没装 pywebview，退回浏览器模式”。
- 结论：当前实现对“缺运行库”的兜底方向恰好与硬约束相反，必须改成硬失败 + 可操作提示，或只允许 Edge/Chrome 的 `--app` 独立窗口。

### 3. 独立窗口下还需要补的东西

| 项 | 现状 | 结论 |
|---|---|---|
| 窗口图标 | `create_window` 无 `icon` 参数，无 `.ico` 资源 | 缺，任务栏/Alt-Tab 是 pythonw 默认图标 |
| 窗口标题 | `WINDOW_TITLE = "SJZ 抢购控制台"`（`desktop.py:34`）；页面 `<title>` 是“SJZ · 三角洲抢购” | 可用；无边框下只在任务栏/Alt-Tab 可见 |
| 最小尺寸 | `min_size=(900,620)`；无最大化/无尺寸记忆（`--width/--height` 默认 1180/800） | 有下限，无记忆、无最大化入口 |
| 关闭 → 线程 | daemon 线程 + `events.closed` → `return 0`；实测 WM_CLOSE 后进程退出、端口无 LISTEN、`paddlex` 无残留 | 通过，无 uvicorn 残留 |
| JS 桥接 | `WinCtl.minimize/destroy`（`desktop.py:102-124`）→ `window.pywebview.api`；页面 `wvApi()` + `pywebviewready` 才显示 `.winctl`（`index.html:62-63,839-855`） | 通过，桌面窗口右上角实测出现“—/×” |
| 单实例 | `Local\SJZ-Control-Console` 命名互斥量（`core/single_instance.py:18,25-59`） | 通过，第二次启动被拦并前置已有窗口、`exit 0` |
| 窗口尺寸记忆 | 无实现，每次固定 1180×800 | 缺 |
| 拖拽区 | 整个 `<header class="topbar pywebview-drag-region">`，含页签与窗口按钮 | 有吞点击风险，应收窄 |

### 4. 建议落地方式与风险

1. **入口收敛**：只保留 `desktop.py`；把 `run.py` 的 `webbrowser.open` 收到 `--dev-browser` 显式开关后面，避免误用。
2. **硬失败而非退化**：开窗前用注册表/`EdgeWebView\Application` 目录探测 WebView2；缺失就弹 MessageBox（`desktop.py:375` 已有 MessageBoxW 路径）给安装指引并退出；彻底不调 `_open_browser`。
3. **只有一种兜底**：若业务坚持“没有 WebView2 也要独立窗口”，用 `msedge.exe --app=http://127.0.0.1:PORT --window-size=1180,800 --user-data-dir=<独立目录>`，仍是无地址栏/无标签的独立窗口；必须检查 `Start-Process` 是否成功，失败要报错退出，不得静默挂服务。
4. **窗口体验**：补 `.ico`（16/32/48/256）并 `webview.start(icon=...)`；关闭时把 `width/height/x/y/maximized` 写入 `config.json` 的 `ui` 段，启动时按当前显示器工作区钳制；顶栏加最大化按钮或双击标题区。
5. **拖拽区收窄**：只给标题文字区加 `.pywebview-drag-region`，`.tabs/.pills/.winctl` 明确排除，避免点页签被当成拖窗。
6. 风险点：frameless + 无边框后系统缩放边不可见（用户不知道能否拉伸）；125%/150% DPI 下实测尺寸与设定值有偏差（本次 1164×761 vs 1180×800，需在缩放机器复核）；多显示器拔插后恢复几何要校验矩形是否落在可见区域；`--app` 模式仍依赖 Edge/Chrome 存在，且任务栏图标是浏览器图标而不是应用图标。

## 未覆盖项

- 未在“完全没有 WebView2、也没有任何浏览器”的机器上实机验证（当前机器 WebView2 156.0.4314.58 已装，行为为静态推断 + 代码路径确认）。
- 未在 125%/150% DPI 缩放、多显示器、1024×768 以下视口复测（本次为 1× 缩放）。
- 未跑真实抢购链路（游戏未运行，预检在“游戏页面”一步即阻断）；为避免误点鼠标，开始动作在“演练模式”下执行。
- 未测 OCR 冷启动期间的界面态（本次 OCR 服务在 8081 已就绪）。
- 未做长时间运行（日志增长、内存、轮询 1s 的 CPU 占用）与弱网/服务中断重连表现。
- 未做读屏软件实测，仅采集 CDP 无障碍 issue。

## 复现与清理

- 复现：`python run.py --port 8900 --no-browser` → 浏览器打开 `http://127.0.0.1:8900/?frozen=1`（`frozen` 会停掉 1s 轮询，便于长文本注入）；桌面壳 `python desktop.py --port 8901`。
- 收尾：8900 服务与它拉起的 `paddlex`(8081) 已结束；8901–8905 均无 LISTEN；审查用 Chromium 已关闭；`config.json` 已按备份还原（SHA256 与备份一致）；临时探针脚本已删除，仅保留 `_fe_shots/` 与本报告。
