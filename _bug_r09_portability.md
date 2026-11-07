# R09 可移植性与换机部署专项审查

审查对象：`C:\Users\26346\Desktop\project\dafors`（源码 + `dist\SJZ-Control` + `dist-onefile`）
审查范围：只查可移植性/换机部署，不查业务正确性、不查 UI 视觉。
审查方式：静态逐条核对环境假设 + 在临时副本里造不满足条件的环境做动态模拟。
约束遵守：未改项目代码；未 commit/push；未动真实鼠标/焦点；未开可见窗口（打包版只用 `--doctor`）；模拟目录全部在 `temp_20261107_21` 下，结束后已删除。

---

## 0. 结论先行（卖给别人前必须先处理）

| 优先级 | 问题 | 一句话后果 |
|---|---|---|
| P0 | 安装到 `Program Files`（或任何标准用户不可写目录） | 首次启动直接崩；即使启动也保存不了任何设置，且 `desktop.log` 静默丢失 |
| P1 | 游戏必须铺满“选中的整块显示器”且模板必须是当前像素比例 | 换 1440p/窗口化后，检查可能全绿但购买键永远不出现，或点击落到空处 |
| P1 | 多显示器没有按游戏窗口自动选屏，也没有窗口矩形校验 | 游戏在副屏、配置仍是 1 号屏时，全程在抓/点主屏，静默空转 |
| P1 | 桌面模式 per-monitor DPI 感知被 pywebview 提前锁死为 system-aware | 混合缩放的多显示器（笔记本 150% + 外接 100%）坐标/截图可能对不上 |
| P1 | 交付包不带模板、不带已标定区域 | 每台买家机器都必须重新框选/截图，不能“装上就能用” |
| P2 | 源码交付时 `.venv` 不可移植；`start.bat` 却优先使用它 | 把整个项目文件夹拷给买家后双击可能完全没反应 |
| P2 | 单文件版依赖可写 `%TEMP%` | 临时目录被重定向/只读/满盘时，引导器直接失败 |
| P2 | exe 未签名、无版本资源 | 买家首次运行遇 SmartScreen；企业环境可能直接拦截 |
| P2 | 冻结版启动自检用 `_MEIPASS` 找 `templates/`、`.venv-vl`，且 `tools/OCR_*.yaml` 未打包 | 每次启动出现假告警；OCR 兜底说明在交付包里无法照做 |
| P2 | 控制台窗口固定 1180×800、最小 900×620 | 1366×768 或 150% 缩放的 768p 笔记本上底部控件超出屏幕 |
| P3 | 休市截前热键硬编码本机 05:00–08:00 | 系统时区不是 UTC+8 时功能时段错位 |
| P3 | 价格解析把 `1.234`、`12.345` 读成 234/345 | 若游戏/OCR 出现点分位或小数点，会静默读成错误价格 |
| P3 | 字体自检把 `segmdl2.ttf` 当“必需”，但 UI 实际未引用 MDL2 | 缺字体时只是字体降级/告警，不是崩溃；告警口径偏大 |

---

## 1. 环境假设清单与逐项验证结果

| # | 代码依赖的环境假设 | 验证方式 | 结果 |
|---|---|---|---|
| 1 | exe 同级目录可写（config/templates/data/log 都放这里） | 临时目录 ACL 设为只读，分别测“目录已存在/不存在” | **不成立即 P0**，见 R09-01 |
| 2 | 安装路径为纯 ASCII | 复制产物到 `中文 目录 测试\SJZ 控制台` 并跑 `--doctor` | 通过（onedir/onefile 都正常） |
| 3 | `ProgramFiles` 等环境变量存在 | 清空 `ProgramFiles/ProgramFiles(x86)/LOCALAPPDATA` 后调用 `_find_browser()` | 通过（有硬编码兜底路径） |
| 4 | 游戏铺满选中的整块显示器，且等于 `screen.base_size` | 读 `preflight`/`Screen`/`Mouse` 实现 + 合成缩放图匹配 | **不成立会静默失败**，见 R09-02 |
| 5 | 模板图与当前分辨率同一像素比例 | 合成 1080p 底图 + 真实 `buy.png`，等比放大后匹配 | **1.25× 分数从 1.0 掉到 0.4669**，阈值 0.85 |
| 6 | 游戏就在配置选中的那块屏上 | 读 `find_game_window`/`_pick_monitor`/preflight | **未校验，多屏会点错屏**，见 R09-03 |
| 7 | 进程能拿到 PER_MONITOR_DPI_AWARE | 复现 `SetProcessDPIAware()` → `Screen()` 的真实顺序 | **桌面模式最终是 system-aware（1），不是 per-monitor（2）** |
| 8 | 100% 缩放或所有屏同一缩放 | 本机 1920×1080@96DPI，单屏 | 未实测混合 DPI（见第 5 节） |
| 9 | 系统有微软雅黑 / Segoe MDL2 | 伪造 `SystemRoot` 指向空目录后跑 `selfcheck.fonts()` | 只告警、不崩；UI 字体有 CSS 兜底 |
| 10 | 系统区域格式只影响显示，不影响解析 | 直接调 `price_from_text` 输入全角/逗号/点 | 逗号/全角通过；**点分位有误读**，见 R09-11 |
| 11 | `%TEMP%` 可写（单文件版） | 中文+空格 TEMP 实测通过；只读 TEMP 未测 | 部分通过，见 R09-07 |
| 12 | WebView2 已安装 | 只做代码路径核对 + 浏览器探测实测 | 有 `--app` 兜底；真实缺 WebView2 未实测 |
| 13 | 有网络 | NTP 指向不存在域名实测；打包版 OCR 实测 | 通过（NTP 0.72s 失败、offset=0；RapidOCR 离线可用） |
| 14 | 管理员权限可用 | `--doctor` 实测当前非管理员；读驱动/提权降级代码 | 可降级：无驱动走 legacy、拒绝 UAC 继续运行 |
| 15 | Python/VC 运行库齐全 | 查 `_internal`：`python311.dll`、`vcruntime/msvcp/ucrtbase` 均在 | 打包版通过；源码版需 Python≥3.10 |
| 16 | 单用户独占同一安装目录 | 读 config/license/单实例实现 | 多用户共享安装目录会共用配置与记录，见 R09-01 备注 |

---

## 2. 问题详表

### R09-01 [P0] 装到 Program Files / 只读目录：首次启动崩溃或全程无法保存

**依赖的环境假设**
- exe 所在目录对当前用户可写；
- `templates/`、`data/` 两个子目录在首次启动时可由当前用户创建。

**不满足时的表现（已实测）**
1. **全新安装、目录不存在**：`Config.__init__` 的 `os.makedirs()` 抛 `PermissionError`；`Config.load` 在 `except OSError` 里重试 9 次后再次 `cls()`，异常最终逃出。`core/server.py` 在模块导入期就执行 `_scheduler = Scheduler()`，而 `Scheduler.__init__` 会 `Config.load()`，因此 `desktop.py` 顶部的 `from core.server import serve` 直接失败。
   - 窗口版 exe：没有控制台，`desktop.log` 也还没机会打开 → 用户看到的是“双击没反应”或 PyInstaller 的错误框；
   - 实测异常：`PermissionError: [WinError 5] 拒绝访问`。
2. **目录已存在但只读**（Inno 安装到 `{pf}` 后最常见）：
   - 启动能过，但 `Config.save()` 抛 `PermissionError`；改区域、改开关、加任务、任务状态持久化全部失败，接口统一回 500；
   - `Config.task_dir()`（`os.makedirs(data/<id>)`，config.py:568-571）同样会抛；
   - `desktop.log` 的写入被 `except OSError: pass` 吞掉 → 售后没有任何现场日志；
   - `--doctor` 的“配置目录可写”判断用 `os.access(..., os.W_OK)`（desktop.py:226），在 ACL 只读目录上实测仍返回 `True`，但真实 `open(...,'w')` 失败 → **自检会误报可写**。

**证据**
- 配置根目录固定在 exe 同级：`core/config.py:22-32`（frozen 用 `sys.executable` 目录）；
- 构造时建目录：`core/config.py:259`；
- 保存失败最终直写再抛：`core/config.py:386-419`；
- 导入期建调度器：`core/server.py:40-45`；桌面入口导入：`desktop.py:32`；
- 日志静默失败：`desktop.py:38-47`；可写性误判：`desktop.py:226`；
- 实测：只读 ACL 目录 `os.access(...,W_OK)=True`，但写文件 `PermissionError`；无 `templates/data` 时 `Config.load()` 抛 `PermissionError`。

**建议改法方向**
- 把**可写数据根**从 exe 目录拆出来：优先 `%LOCALAPPDATA%\SJZ-Control\`（配置/模板/记录/截图/日志），exe 目录只读也能跑；若坚持绿色便携，则启动时探测可写，不可写就切到 LocalAppData 并在界面提示“配置已保存到 …”。
- `Config`/`task_dir`/`log` 的创建与保存加统一兜底：首次失败 → 切目录重试 → 仍失败则弹一次明确的中文错误框（而不是静默）。
- 安装器不要默认 `{pf}`；用 `{localappdata}\Programs\SJZ-Control`，或安装时给 `Users` 对该目录 `Modify` 权限。
- `--doctor` 的可写判断改成“创建并删除一个临时文件”的真实探针。

**备注（多用户共享）**：即使解决了权限，`config.json`/`data` 仍在同一目录，多用户共用一台机器会共享队列、区域和记录；改用 LocalAppData 可顺带解决。

---

### R09-02 [P1] 分辨率/窗口化/模板比例：检查可能全绿，实际永远匹配不到

**依赖的环境假设**
- 游戏画面铺满配置选中的整块显示器；
- 显示器分辨率等于 `screen.base_size`（默认 1920×1080）；
- `templates/*.png` 是在**当前像素比例**下截的。

**不满足时的表现**
1. **2560×1440 全屏 + 沿用 1080p 模板**：区域按比例缩放是对的，但模板是固定像素。合成实测：底图 1080p 时匹配分 1.0；放大 1.25× 后 0.4669、1.333× 后 0.4279、1.5× 后 0.3963，而阈值是 0.85（`core/config.py:50`）→ 购买键永远判定“没出现”，任务超时/空转到结束。
2. **大屏上窗口化 1920×1080**：`Screen` 抓的是整块显示器，`preflight` 的 `facts["size"]` 也来自 `Screen.size`（`preflight.py:393`），不是游戏客户区。若用户把 `base_size` 改成 2560×1440 让检查变绿，归一化区域会按 2560 宽计算，而游戏只占左上 1920×1080 → 区域和点击整体偏移约 1/3 屏宽，且不报错。
3. **preflight 的提示反而会把人带进坑**：`preflight.py:72-93` 在尺寸不符时提示“把游戏改成窗口化 1920×1080”。在 2560 显示器上这不会改变 `Screen.size`，检查仍然红；用户若改 `base_size` 绕过，就进入第 2 条的静默错位。
4. **模板只查“文件在不在”**：`preflight.py:151-173` 只用 `tpl.get("buy")` 判断存在，不校验模板尺寸与当前区域像素尺寸是否匹配 → 旧模板能通过检查。

**证据**
- `Screen` 只用显示器 bounds，不定位游戏窗口：`core/screen.py:52-57`；
- `Mouse` 用选中显示器的 origin + 虚拟屏尺寸：`core/input.py:318-450`，数学本身正确，但前提是“游戏铺满该屏”；
- 尺寸检查只看显示器：`core/preflight.py:47-93`、`facts["size"]` 赋值在 `preflight.py:393`；
- 模板固定像素匹配：`core/vision.py:72-80`（`matchTemplate`，无多尺度）；
- 实测分数：1.0 / 0.4669 / 0.4279 / 0.3963（1.25× / 1.333× / 1.5×），阈值 0.85。

**建议改法方向**
- 匹配前按“当前区域像素宽 / 采集时区域像素宽”自动缩放模板（或模板旁存 `capture_size`），至少做 1.0/1.25/1.33/1.5 多尺度尝试；
- preflight 增加“模板宽高 vs 当前区域像素宽高”的硬校验，不匹配直接红并说明要重截；
- 用 `UnrealWindow` 的 `GetWindowRect` 校验游戏客户区确实覆盖选中显示器；不覆盖就明确报“游戏不在选中屏/没有铺满”，不要用显示器尺寸冒充游戏窗口；
- 修正 `preflight` 文案：窗口化只有在“窗口恰好等于选中显示器且无边框铺满”时等价；否则要求全屏无边框或按窗口矩形裁剪抓图。

---

### R09-03 [P1] 多显示器：默认永远选主屏，且不校验游戏在哪块屏

**依赖的环境假设**
- 游戏在 `monitor=1` 对应的主显示器上；
- 运行期间显示器布局不变。

**不满足时的表现**
- `monitor=1` 的实现是“按 `GetSystemMetrics` 拿主屏尺寸，再在 mss 列表里找第一块同尺寸的屏”（`screen.py:72-88`）。双 1920×1080 时永远返回 1 号屏；主屏 2560×1440、副屏 1920×1080 时也返回主屏。
- 游戏若在副屏，抓图和点击都发生在主屏；`find_game_window` 只判断“有没有前台 UnrealWindow”，不检查矩形（`input.py` 内实现）→ preflight 仍可能全绿，实际全程空点。
- 界面没有显示器选择项（`web/index.html` 与 `switches.py` 均无 `monitor`），买家只能手改 `config.json`。
- `Mouse` 在构造时缓存了虚拟屏 metrics（`input.py:318-340`），运行中拔插显示器/改分辨率/RDP 重连后不刷新，直到重启。

**建议改法方向**
- 枚举 `UnrealWindow` 的 `GetWindowRect`，取面积最大的显示器，自动写入/覆盖 `monitor`；设置页提供下拉框；
- preflight 增加“游戏窗口矩形 ∩ 选中显示器 ≥ 95%”检查；
- 每次开抢前重建 `Screen`/`Mouse`（或监听 `WM_DISPLAYCHANGE`），不要跨会话缓存 metrics。

---

### R09-04 [P1] DPI：桌面模式 per-monitor 感知被提前锁死，且失败是静默的

**依赖的环境假设**
- `Screen.__init__` 里的 `SetProcessDpiAwareness(2)` 能成功。

**不满足时的表现（已实测）**
- 桌面入口先调用 `webview.start()`；pywebview 的 winforms 后端在此之前已调用 `SetProcessDPIAware()`（system-aware）。进程 DPI 感知一旦设定，后续 `SetProcessDpiAwareness(2)` 返回 `E_ACCESSDENIED (0x80070005, -2147024891)`，不会抛异常，所以 `screen.py:17-23` 的 `try/except` 永远走不到兜底。
- 实测对照：新进程直接调用 `SetProcessDpiAwareness(2)` → HRESULT 0；先 `SetProcessDPIAware()` 再调用 → `0x80070005`；按桌面真实顺序（先 `SetProcessDPIAware`，再 `Screen()`）最终 `GetProcessDpiAwareness` 返回 **1（system-aware）**；浏览器模式（`Screen` 先执行）返回 **2（per-monitor）**。
- 单显示器、单一缩放时 system-aware 通常仍按物理像素工作（本机 96DPI 无法验证 125%/150%）；**混合 DPI 多显示器**（例如笔记本 150% + 外接 100%）下，`mss` 拿到的是物理 bounds，而 `GetSystemMetrics` 的虚拟屏尺寸可能按 system DPI 虚拟化 → `Mouse._abs` 的 0–65535 映射偏，点击落点与截图 ROI 对不上。
- 该失败不会出现在日志里，用户只会看到“点不中/找不到按钮”。

**证据**
- `core/screen.py:15-23`（HRESULT 未检查，`except` 只覆盖 `AttributeError/OSError`）；
- pywebview `winforms.py` 在 `start()` 中先 `SetProcessDPIAware()`；
- `desktop.py` 先 `webview.start()`，`Screen` 在任务/预检时才创建（`engine.py:440`）；
- 实测 HRESULT 与 awareness 值见上。

**建议改法方向**
- 在 `desktop.py` **导入 pywebview 之前**（文件顶部）设置 DPI 上下文：`SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)`；或至少在 `webview.start()` 前调用 `SetProcessDpiAwareness(2)` 并检查 HRESULT；
- `Screen` 用 `GetMonitorInfo`/`GetDpiForMonitor` 取**选中显示器**的物理 bounds 与 DPI，不要依赖全局 `GetSystemMetrics`；
- 记录一次 `awareness=1/2` 到日志，换机排查时能直接看到。

---

### R09-05 [P1] 交付包不带模板/区域：不能“装上就能用”

**依赖的环境假设**
- 买家机器上已经有与当前分辨率匹配的 `templates/buy.png` 等模板和已标定 `regions`。

**不满足时的表现**
- `dist\SJZ-Control\templates` 为空、`data` 为空、`config.json` 的 `regions.buy/confirm/clock/list` 全是 `null`；
- `preflight_required=true` 且启动闸门会拦：新买家首次运行必须自己把游戏停在对应页面、框区域、截模板；否则任何任务都不能开始；
- 这不是“提醒一下”级别：模板与分辨率绑定（R09-02），卖家在开发机标好的模板复制到 1440p 买家机器上直接失效。

**证据**
- `dist\SJZ-Control\config.json`（`regions` 全 null、`workflow=countdown`）；
- `dist\SJZ-Control\templates`（0 个文件）；
- `preflight.py:151-173` 与 `scheduler` 的模板/区域口径。

**建议改法方向**
- 交付前明确二选一：① 按买家分辨率远程采集一套模板并预置；② 首次启动做“校准向导”，引导框区域+自动截模板，并在完成后校验模板尺寸；
- 在 `config.json` 里记录模板采集分辨率（或模板旁 `.meta.json`），换分辨率时 preflight 直接提示“模板比例不符，请重新采集”。

---

### R09-06 [P2] 源码交付：`.venv` 不可移植，`start.bat` 却优先使用它

**依赖的环境假设**
- 项目目录里的 `.venv` 是在本机创建的，或买家会先删掉它再 `uv sync`。

**不满足时的表现**
- `.venv\pyvenv.cfg` 的 `home` 指向开发机的 `C:\Users\26346\AppData\Local\uv\python\...`；把整个项目文件夹拷到别人机器后，`.venv\Scripts\pythonw.exe` 存在但基础解释器不存在。
- `start.bat` 只判断 `pythonw.exe` 是否存在就进 venv 分支 → 双击后 `pythonw` 起不来，窗口版无控制台，表现就是“完全没反应”。
- 买家若手动跑 `uv sync`，也可能沿用坏 venv 而不是重建。

**证据**
- `.venv\pyvenv.cfg`（`home = C:\Users\26346\AppData\Local\uv\python\cpython-3.11.9-windows-x86_64-none`）；
- `start.bat` 第 8-15 行的 venv 优先逻辑。

**建议改法方向**
- 源码交付永不打包 `.venv`；`start.bat` 增加真实探针（`".venv\Scripts\python.exe" -c "print(1)"` 失败则删/重建 venv 再 `uv sync`）；
- 在 README 用一行命令明确“首次运行需要联网”，或只交付 dist。

---

### R09-07 [P2] 单文件版依赖可写 `%TEMP%`

**依赖的环境假设**
- `%TEMP%` 存在、可写、磁盘未满，且路径可被 PyInstaller 引导器解包。

**不满足时的表现**
- 单文件 exe 启动时把 `_MEIxxxx` 解到 `%TEMP%`；只读/满盘/被组策略重定向到不可用位置时，引导器在进入 Python 前就失败，通常弹一个可见错误框后退出（不是应用内可捕获的错误）。
- onedir 版不受此影响（代码在 `_internal` 原地加载）。

**已验证**
- 把单文件 exe 放到 `中文 目录 测试\SJZ 单文件.exe`，并把 `TEMP/TMP` 指到 `中文 目录 测试\临时 目录`（中文+空格）：`--doctor` 正常退出，RapidOCR 正常初始化，说明中文/空格 TEMP 本身没问题。
- 只读 TEMP **未实测**：按约束不能开可见错误框。

**建议改法方向**
- 对外售卖优先发 onedir（或安装器安装 onedir）；单文件只作为“应急版”并注明 TEMP 要求；
- 启动前无法可靠自检（引导器先于代码），只能在文档/安装器里校验 `%TEMP%` 可写。

---

### R09-08 [P2] exe 未签名、无版本资源：SmartScreen/企业管控

**依赖的环境假设**
- 买家能接受“未知发布者”，且安全软件不拦截 PyInstaller 产物。

**不满足时的表现**
- `Get-AuthenticodeSignature` = `NotSigned`；`VersionInfo.FileVersion` 为空。从网盘/邮件下载后带 Mark-of-the-Web，首次运行出现 SmartScreen“Windows 已保护你的电脑”；企业 AppLocker/WDAC 可能直接禁止。
- 防火墙方面反而没问题：HTTP 只绑 `127.0.0.1`，不会弹 Windows 防火墙授权框。
- 拦截驱动（interception）未随包自动安装，装不上时走 legacy 路径，不会因此起不来；但杀软若直接删掉 `interception.dll`，等于永久 legacy（更慢、会动真实光标）。

**建议改法方向**
- 代码签名（至少 OV 证书）+ 填 CompanyName/FileVersion/ProductVersion；
- 安装器附带“添加 Defender 排除项”的可选步骤（需用户确认），并在 README 说明误报处理；
- 发布包提供 SHA-256 校验值。

---

### R09-09 [P2] 冻结版启动自检的路径假设错误 + OCR 兜底文件未打包

**依赖的环境假设**
- 冻结后 `server.py` 的 `__file__` 父目录旁边有 `templates/` 和 `.venv-vl`；
- 交付包里有 `tools/OCR_*.yaml`。

**不满足时的表现**
- frozen 下 `__file__` 在 `_MEIPASS`（onedir 为 `_internal`），`root` 被算成 `_internal`；而运行时真正的模板在 **exe 同级** `templates/`（`config.TEMPLATE_DIR`）。因此打包版每次启动都会报“templates/ 没入库”，即使 exe 同级模板齐全。
- `_internal\.venv-vl` 在打包版永远不存在（spec 未收集），所以每次还报“OCR 运行环境 .venv-vl 不在仓内，需要联网下载模型”；实际上 RapidOCR 已随包且离线可用（打包版 `--doctor` 实测 OCR 可用）。
- 如果买家真按提示去装 `.venv-vl`，交付包里又没有 `tools/OCR_game.yaml`（`dist` 无 `tools/` 目录），`ocr.ppocr.pipeline` 指向的相对路径无法使用。

**证据**
- `core/server.py:1074-1083`（用 `__file__` 算 `root`）；
- `core/config.py:22-32`（真正数据根用 exe 同级）；
- `dist\SJZ-Control\_internal` 无 `templates/`、无 `.venv-vl`；`dist\SJZ-Control` 无 `tools/`；
- 打包版 `--doctor` 日志显示 RapidOCR 可用、配置目录正常。

**建议改法方向**
- 自检统一用 `config.TEMPLATE_DIR` / `config.DATA_DIR`，不要用 `server.__file__`；
- 只在 RapidOCR 不可用时才提示 `.venv-vl`；否则把该告警降级为 debug；
- 若保留二级 OCR 兜底，把 `tools/OCR_*.yaml` 一起打进 dist，并在 README 明确它是可选组件。

---

### R09-10 [P2] 固定窗口 1180×800、最小 900×620：小屏/高缩放笔记本放不下

**依赖的环境假设**
- 屏幕工作区至少 1180×800（或缩放后仍有这么大）。

**不满足时的表现**
- `desktop.py:288-289` 默认 1180×800，`desktop.py:364-366` `min_size=(900, 620)`，无按工作区自适应。
- 1366×768 笔记本：窗口高 800 > 屏高 768（还要减任务栏），底部控件在屏幕外；无边框窗口只能靠顶部拖动区整体上移，移上去后顶部又被裁掉。
- 150% 缩放的 1366×768（逻辑约 911×512）：逻辑高度 512 < 最小 620，用户连缩小都做不到。

**建议改法方向**
- 启动时读 `SystemParametersInfo(SPI_GETWORKAREA)`（pywebview 窗口所在屏），`w=min(1180, wa_w), h=min(800, wa_h)`；`min_size` 同步降到能容纳核心按钮的值；
- 或允许窗口最大化并提供页面内滚动。

---

### R09-11 [P3] 时区：休市截前热键硬编码本机 05:00–08:00

**依赖的环境假设**
- 买家 Windows 时区是 UTC+8。

**不满足时的表现**
- `server.py:929-932` 用 `datetime.datetime.now().time()` 直接比较 05:00–08:00；系统时区改到 UTC+9/UTC 后，功能窗口整体平移，用户以为坏了。
- 其余定时/倒计时用 epoch + NTP offset，不受时区影响；这里只是功能可用性窗口。

**建议改法方向**
- 以 `Asia/Shanghai` 计算，或把时段放进 `config.json` 并允许界面调整。

---

### R09-12 [P3] 区域格式：`1.234` / `12.345` 被读成 234 / 345

**依赖的环境假设**
- 游戏价格只用逗号千分位，或纯数字。

**不满足时的表现（实测）**
- `price_from_text` 取“最后一个数字串”：
  - `'1,234' → 1234`、`'１２３４' → 1234`、`'1，234' → 1234`（正常）；
  - `'1.234' → 234`、`'12.345' → 345`（**点分位被当尾段**）；
  - `'1,23' → None`（小数逗号被拒，安全）。
- 如果游戏客户端/系统区域让价格显示成点分位，或 OCR 把逗号识别成点，会静默按错误金额判断，而不是拒识。

**建议改法方向**
- 数字串内部出现 `.` 且两侧都是数字时直接判“格式不确定”返回 None；
- 同时支持 `,`/`.` 两种千分位并统一成整数，或只允许配置里声明的分隔符；
- 把原始 token 写进失败日志，售后可定位。

---

### R09-13 [P3] 字体：自检口径偏大；缺字体只降级不崩

**依赖的环境假设**
- `msyh.ttc`、`segmdl2.ttf` 存在。

**不满足时的表现**
- `selfcheck.fonts()` 会把 `segmdl2.ttf` 标成“图标必需”（`selfcheck.py:22-27`），但 `web/index.html` 里没有任何 `Segoe MDL2` 引用，图标并非依赖它；缺它时只是告警，不影响功能。
- CSS 的字体栈有 `Microsoft YaHei UI` → `PingFang SC` → `sans-serif` 兜底；缺中文字体时是字形降级（可能方块/替代字体），不会崩。
- OCR 不依赖系统字体：RapidOCR 用自带模型，数字兜底用 OpenCV 内置字模（`digit_ocr.py` 训练模板），所以“缺字体”不会影响识别。

**建议改法方向**
- 把 `segmdl2` 从“必需”改成“可选/仅图标”，或删掉该检查；
- 若要保证中文观感，考虑随包一个开源中文字体子集并在 CSS 里 `@font-face` 兜底。

---

## 3. 已确认没问题的项

1. **代码/配置里没有写死开发机绝对路径或用户名**：全仓 grep `C:\Users`、`26346`、`Desktop\project`、`C:/Users` 均无命中；`config.json` 也没有绝对路径。
2. **路径含中文和空格可启动**：`dist\SJZ-Control` 复制到 `中文 目录 测试\SJZ 控制台` 后 `--doctor` 正常；单文件 exe 同样正常，且 `TEMP/TMP` 也指到中文+空格目录。
3. **中文/Unicode 文件读写有处理**：截图/模板读写走 `vision.imread/imwrite`，不是裸 `cv2.imread/imwrite`，中文文件名不会读成 None。
4. **离线可运行**：NTP 不可达时实测 0.72s 返回 `ok=False, offset=0.0`，不阻塞、不崩；打包版 RapidOCR 离线初始化成功；页面是本地 `127.0.0.1`；许可证模块当前未接入启动路径，不构成离线门槛。
5. **防火墙不会弹窗**：HTTP 只绑 `127.0.0.1`（`server.serve` 默认），不是 `0.0.0.0`。
6. **VC 运行库随包**：`_internal` 内有 `python311.dll`、`vcruntime140*.dll`、`msvcp140*.dll`、`ucrtbase.dll`，干净 Win10 不必另装 VC++ Redist。
7. **无管理员也能降级运行**：拦截驱动未随包安装，加载失败自动走 legacy；UAC 拒绝后进程继续，preflight 检测前台/完整性并提示。
8. **区域/点击坐标的归一化数学本身正确**：区域按当前 `Screen.size` 归一化，`Mouse._abs` 正确处理显示器 origin 与负虚拟屏原点，并做越界拒绝；问题只在“游戏必须铺满该屏”和“模板比例”这两个前提。
9. **区域格式对整数价格基本健壮**：逗号千分位、全角逗号、全角数字都能解析成整数，`int()` 不受系统区域影响。
10. **缺字体不会导致崩溃**：伪造 `SystemRoot` 后 `selfcheck.fonts()` 只返回缺失项；OCR/数字识别不依赖系统字体。
11. **单实例与配置重试**：`Local\` 互斥名按会话隔离；`Config.load` 对文件占用有 9 次重试与 `.good` 备份恢复。

---

## 4. 复现记录（本次实际执行的模拟）

> 全部在临时副本执行；项目源码未改；结束后临时目录已删除。

1. **只读安装目录（Program Files 模拟）**
   - ACL：`icacls <dir> /inheritance:r /grant:r "<user>:(OI)(CI)RX"`。
   - 目录不存在 `templates/data`：`Config.load()` → `PermissionError: [WinError 5] 拒绝访问`。
   - 目录已存在：`load_ok=True`，`cfg.save()` → `PermissionError`。
   - `os.access(dir, os.W_OK)` → `True`，实际 `open(dir\x.txt,'w')` → `PermissionError`。
2. **中文+空格路径**
   - onedir：`Start-Process ...\SJZ-Control.exe --doctor`，exit 0；日志显示 exe 同级 `config.json`、OCR 可用。
   - onefile：`SJZ 单文件.exe --doctor`，`TEMP/TMP=...\临时 目录`，exit 0，RapidOCR 初始化正常。
3. **DPI 顺序**
   - 新进程 `SetProcessDpiAwareness(2)` → `hr=0`；
   - 先 `SetProcessDPIAware()` 再调用 → `hr=-2147024891 (0x80070005)`；
   - 按桌面真实顺序 `SetProcessDPIAware()` → `Screen(1)`：`GetProcessDpiAwareness` = **1**；
   - 按浏览器顺序先 `Screen(1)`：= **2**。
4. **模板缩放匹配**
   - 1080p 合成底图 + 真实 `templates/buy.png`（67×315）：1.0 → 1.0；1.25× → 0.4669；1.333× → 0.4279；1.5× → 0.3963；2.0× → 0.3019；阈值 0.85。
5. **NTP 断网**
   - `sync(servers=['no-such-host.invalid'], samples=4, timeout=0.5)` → `{'ok': False, ... 'offset': 0.0}`，耗时 0.72s。
6. **区域格式**
   - `'1,234'→1234`、`'１２３４'→1234`、`'1，234'→1234`、`'1.234'→234`、`'12.345'→345`、`'1,23'→None`。
7. **缺字体**
   - `SystemRoot` 指向空目录后 `selfcheck.fonts()` 返回缺失项；进程未崩。
8. **显示器基线**
   - 本机：`1920×1080`、96 DPI、1 块屏；虚屏 origin `(0,0)`。

---

## 5. 未实测的项（受环境/约束限制）

1. **混合 DPI 双屏的真实坐标偏移**：本机只有单屏 1920×1080@96DPI，无法把 125%/150% 与副屏组合起来实测；R09-04 的结论来自 API 返回值 + 代码路径，实际偏移量需要在 150%+100% 双屏机器上验证。
2. **真实缺失 WebView2**：需要在系统上卸载/禁用 Evergreen Runtime 才能复现；按约束未改系统组件。已核对 `webview` 启动失败 → `_open_app_window` → `Edge/Chrome --app` → 都没有则消息框提示的手动兜底路径。
3. **只读/满盘 `%TEMP%` 下的单文件启动**：引导器会弹可见错误框，违反“不开可见窗口”约束，未执行。
4. **杀软/EDR 实际拦截**：未修改 Defender 设置、未做样本上报；只确认 exe 未签名、无版本资源，以及驱动/loopback 的设计事实。
5. **Windows 7 / Windows 8 / 32 位系统**：打包解释器是 64 位 Python 3.11（`python311.dll`），32 位系统无法运行；Win7/8 还受 WebView2/Python 3.11 支持范围限制。无对应机器，未实测。有效最低要求应写成 **64 位 Windows 10 1809+**。
6. **缺中文字体时的实际 UI 观感**：没有安全办法移除系统字体；只验证了“检查不崩、CSS 有兜底、OCR 不依赖字体”。
7. **非中文系统区域/时区的端到端行为**：没有改系统区域/时区；只做了代码层核对与函数级输入测试（R09-11、R09-12）。
8. **Inno 安装器**：仓库里还没有 `.iss`，无法验证默认安装目录/权限设置；R09-01 针对的是“若按默认 `{pf}` 安装”的必然结果。
9. **RDP/虚拟机/无 GPU 环境**：未测。RapidOCR 走 CPU 应该可用，但无 AVX 的老 CPU、远程桌面切换桌面、锁屏状态下的抓屏/输入未覆盖。
10. **长时间运行中的显示器热插拔**：未做拔插/改分辨率动态测试；代码显示 metrics 在 `Mouse` 构造时缓存，属于已识别风险（R09-03）。

---

## 6. 最小修复清单（按顺序）

1. 数据根切到 `%LOCALAPPDATA%\SJZ-Control`（或安装器给可写 ACL），并把 `Config`/`task_dir`/日志的失败改成显式错误；改掉 `os.access` 自检。
2. 修正安装器目录策略（禁止默认 `Program Files`，或安装时授权）。
3. `base_size`/模板比例做硬校验 + 模板自动缩放；preflight 用游戏窗口矩形而非显示器尺寸；修正“窗口化 1920×1080”提示。
4. 自动按 `UnrealWindow` 所在屏选 `monitor`，加界面选项与窗口覆盖校验。
5. 在 `webview.start()` 之前设置 PerMonitorV2 DPI 上下文，并记录 awareness。
6. 交付前预置/首次向导采集模板；模板旁记录采集分辨率。
7. `start.bat` 增加 venv 真实探针；源码交付不打包 `.venv`。
8. 打包 `tools/OCR_*.yaml`（若保留兜底），自检改用 `config.TEMPLATE_DIR`。
9. 代码签名 + 版本资源；文档写明 64 位 Win10 1809+、TEMP 要求、安装目录要求。
10. 窗口按工作区自适应；修时区与点分位解析；字体检查降级为可选。
