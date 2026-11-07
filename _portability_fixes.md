# R09 可移植性与适配修复报告

对应审查报告：`_bug_r09_portability.md`
修复范围：`core/screen.py`、`core/nav*.py`（无改动）、`core/preflight.py`、`core/detector.py`（无改动）、`core/vision.py`、`core/selfcheck.py`、`desktop.py`、`sjz.spec`、`sjz-onefile.spec`、`start.bat`、`templates/`、`tools/calibrate.py`（新增）
未触碰：`core/config.py`、`core/server.py`、`core/scheduler.py`、`core/engine.py`、`web/index.html`、`core/records.py`、`tests/` 既有文件 —— 相关修复全部进入第 8 节《待接线清单》。

验证入口：`python -m pytest tests/test_portability_hardening.py -q` → **17 passed**；`python -m compileall -q core desktop.py tools/calibrate.py` → **OK**。

---

## 1. R09-02 缩放导致模板匹配失效（P0，最严重）

### 根因
`core/vision.py` 的 `match()` 只做单尺度 `cv2.matchTemplate(TM_CCOEFF_NORMED)`：模板是 100% 缩放下截的像素，一旦系统缩放变化，抓屏帧里目标尺寸被等比放大，模板与目标的像素尺寸不再一致，归一化互相关分数断崖式下跌。审查报告基线：1.25× → 0.4669，1.5× → 0.3963，2.0× → 0.3019，全部低于阈值 0.85，`detector` 每帧 `False`，程序静默空转（不报错、不退出、不提示）。

### 改法
1. **模板携带元数据**：`templates/meta.json` 记录采集时的 `capture_screen` 与 `dpi`，`templates/*.png` 旁可另放同名 `meta.json`。`vision.imread()` 解码后立即 `_bind_meta(img, _read_meta(path))`，`vision.to_gray()` 会传递元数据，保证 `imread → to_gray` 链路不丢。
2. **确定性候选尺度**（有元数据时，最多 3 个）：
   - 分辨率比例 `sqrt((cur_w/cap_w) * (cur_h/cap_h))`
   - DPI 比例 `cur_dpi / cap_dpi`
   - 两者乘积
   逐个去重后依次匹配，命中 ≥0.93 立即接受（fast accept），否则取最高分。
3. **无元数据兜底**：`_MATCH_SCALES = (1.0, 0.75, 0.8, 0.9, 1.1, 1.25, 1.333333, 1.5, 1.75, 2.0)` 全扫。
4. **重采样一致性**：`_resized()` 使用 `cv2.resize(img, None, fx=f, fy=f, ...)` 而非显式 `dsize`，避免非整倍数下 x/y 采样系数不一致（实测 `dsize` 版本 0.929 对 `fx` 版本 0.972）。
5. **对外接口不变**：`match()` 仍返回 `(score, loc)`，`engine` 无需改动；新增 `match_scaled()` 返回 `(score, loc, scale)` 供诊断/日志。
6. **尺寸/纯色/NaN 防护**：`_match_once()` 在模板大于帧、模板方差为 0、结果含 NaN 时直接返回 0，杜绝 OpenCV 断言崩栈。

### 验证证据（实测，非推理）
测试用例：真实 `templates/buy.png`（315×67）+ 1080p 合成纹理背景，按 1.0/1.25/1.5/2.0 等比放大整帧，`vision.set_target_screen((1920*f, 1080*f), 96)`：

```
  100%  legacy=1.0000  new=1.0000  独立复核=1.0000  loc=(800, 600) / (800, 600)
  125%  legacy=0.5431  new=0.9850  独立复核=0.9600  loc=(1000, 750) / (1000, 750)
  150%  legacy=0.4837  new=0.9947  独立复核=0.9844  loc=(1200, 900) / (1200, 900)
  200%  legacy=0.4260  new=0.9869  独立复核=0.9494  loc=(1600, 1200) / (1600, 1200)
```
DPI 路径单测（同 1920×1080、DPI=120，元数据 96）：
```
DPI 125%: new=0.9725  独立复核=0.9345  loc=(375, 500) / (375, 500)
```

- **两种独立方式复核**：① 新实现（元数据驱动的 `fx` 重采样）；② 独立实现（`cv2.warpAffine` 仿射放大模板，不复用 `_resized`）。两者均 ≥0.85，分数差 ≤0.06，命中坐标完全一致且等于理论位置 `(800f, 600f)`。
- **失败基线同时被锁死**：断言非 100% 档 `legacy < 0.85`，防止回归到单尺度。
- 证据图（绿框=命中位置，标注各档 legacy/new 分数）：`scale_curve_measured.png`。
- 断言：`new ≥ 0.85`、`manual ≥ 0.85`、`|new - manual| ≤ 0.06`、`loc` 一致。

---

## 2. R09-03 多显示器选错屏（P0）

### 根因
`Screen._pick_monitor()` 原先按 `monitors[want]` 直接取索引（`want=1` 即物理 1 号屏），与游戏实际所在显示器无关。两块同分辨率屏时索引恒为 1 号，游戏在副屏 → 抓屏抓到主屏 → 匹配恒失败、静默空转。

### 改法
`core/screen.py` 重写选屏逻辑：
1. 新增 `find_game_window_rect(class="UnrealWindow")`：`EnumWindows` + `GetClassNameW` + `GetWindowRect`，过滤不可见/最小化，多候选取面积最大者。
2. `_pick_monitor(want)` 语义：
   - `0` → 虚拟全屏（mss 索引 0，保持原行为）
   - `None` / `1` / `"auto"` → **自动**：游戏窗口矩形与各物理屏求交集面积，按 `(area, -中心距离)` 复合键取最大；无窗口 → 主屏兜底 `_primary_index()`
   - `>=2` → 显式指定，越界抛 `ValueError`（不再静默回退）
3. `_monitor_for_rect(rect)`：同分辨率两块屏靠交集面积区分；窗口跨屏时靠中心距离决胜。
4. `_primary_index()`：`MonitorFromWindow(GetDesktopWindow(), MONITOR_DEFAULTTOPRIMARY)` + `GetMonitorInfoW(rcMonitor)`，先按 `left/top/width/height` 精确匹配，再按尺寸，最后按最大面积；单屏返回 0。
5. `pick_reason` 记录选择依据（"窗口交集" / "主屏兜底" / "显式"），供 `--doctor` 与日志排查。

### 验证证据
测试造两块 1920×1080 假 mss 屏（虚拟屏 + 屏1 + 屏2），游戏窗口置于右侧屏：
- `Screen(monitor="auto")` → `index == 2`（正确屏）
- 独立复核：手写交集面积循环（不复用 `_monitor_for_rect`）得同一结果
- 无游戏窗口时 → 主屏按 `left/top` 位置解析为 2 号而非 1 号（覆盖"同分辨率靠位置判断"）
- 显式越界 `Screen(monitor=5)` → `ValueError`

证据图：`monitor_pick_measured.png`（两块同分辨率屏布局 + 游戏窗口 + 选中 index=2 的示意）。

---

## 3. R09-04 DPI 感知被锁成 system-aware

### 根因
`desktop.py` 在 `pywebview.start()` 之前调 `SetProcessDPIAware()`，该 API 只能把进程提升到 **system-aware**。混合 DPI（笔记本 150% + 外接 100%）下，WebView 与抓屏/鼠标坐标系不再一致，出现错位与模糊。且该调用在导入 `webview` 之后，进程 DPI 感知可能在库初始化时已被固化。

### 改法
1. `core/screen.py` 新增 `enable_dpi_awareness()`，降级链：
   `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2 = -4)` → `shcore.SetProcessDpiAwareness(2)` → `SetProcessDPIAware()`，返回**实际生效**的值；`dpi_awareness()` 用 `GetProcessDpiAwareness` 读回；`awareness_name()` 给出可读名。
2. `desktop.py` 顶部结构重排：`BASE/APPDIR/LOG_PATH/_log` 先定义，**在 `import webview` 与 `core.server` 之前**执行 `enable_dpi_awareness()`，结果存入 `_DPI_AWARENESS`，`--doctor` 打印并在 0/1 时告警。
3. `Screen.__init__` 自身也调用一次（幂等），保证任何入口（含 `tools/calibrate.py`、测试）都能拿到 per-monitor aware。

### 坐标系职责（只做一次）
- **归一化 → 物理像素**：唯一入口 `Screen.norm_to_px()`。调用方（`detector`、`preflight`、`calibrate`）不得自行乘缩放。
- **物理像素 → 鼠标绝对坐标 0..65535**：唯一入口 `core/input.Mouse`，基于虚拟屏 metrics 换算。
- **抓屏**：mss 的 `monitor["left"/"top"/"width"/"height"]` 在 per-monitor aware 下已是物理像素，与 `GetWindowRect`/`GetMonitorInfoW` 同域，不做二次换算。
- 因此：PMv2 只设一次（进程启动最早处），换算只在上述两处发生，其余代码全用物理像素。

### 验证证据
- 子进程实测：`enable_dpi_awareness()` 后 `GetProcessDpiAwareness`（两种读法 `shcore` / `ctypes`）均返回 **2 = per-monitor-aware**。
- 静态顺序断言：`enable_dpi_awareness()` 在源码中出现在 `"\n    import webview"` 之前。
- `python desktop.py --doctor` 实跑输出：`DPI awareness = 2 (per-monitor-aware)`。

---

## 4. R09-05 交付包 `templates/` 为空

### 根因
`templates/` 无内容，`cfg.template_path()` 指向不存在的文件，`match` 必然失败。前端（`web/index.html`）没有框选入口，用户既不知道缺什么，也没有补的路径，只能静默空转。

### 改法
1. **随包默认模板**：`templates/buy.png`、`templates/confirm.png` + `templates/meta.json`（含 `capture_screen`、`dpi`、每个模板的 `role` 与 `size_px`）。meta 是尺度对齐的输入，缺它只能退回多尺度兜底（更慢更易误配）。
2. **首次标定向导** `tools/calibrate.py`：
   - 预置 profile：`countdown`（区域 clock/buy/confirm，模板 confirm）、`list`（区域 list/buy，模板 buy）、`detail`（区域 buy/confirm，模板 buy/confirm）——与 `engine.start_check` 的校验要求严格对应。
   - 用法：`python tools/calibrate.py --workflow countdown`（交互输入 `x,y,w,h`）；非交互 `--box NAME=x,y,w,h`、`--box tpl:NAME=x,y,w,h`；`--screenshot` 先抓当前屏存 `DATA_DIR/calibration/<wf>_<ts>.png` 便于量坐标；`--monitor` 选屏；`--dry-run` 只打印计划。
   - 模板框留空时默认取区域外扩 8%，保存 PNG 并更新 `templates/meta.json`；`Config.transaction()` 写 `regions` 与默认 workflow；结束前用 `vision.match` 回验并对比 `cfg.get("threshold")` 打印分数。
   - 无 tty 时打印完整 cmd 用法并退出码 3（不静默）。
   - `desktop.py --calibrate [workflow]` 为等价入口（`importlib` 加载，兼容打包路径）。
3. **缺模板不再静默**：`core/preflight.py` 统一 `_CALIBRATE_FIX` 文案，明确"哪个文件（`templates/<name>.png`）+ `--calibrate` + `python tools/calibrate.py` + 写入 regions"。`--doctor` 增 `_doctor_templates()`：模板数量、有无元数据、缺失时直接给命令。
4. **模板/区域硬校验**：`_template_scale_issue()` —— 模板尺寸大于当前区域的物理像素 → 红；模板采集屏与当前屏比例 >2.2 或 <0.45（重采样必然失真）→ 红，并给出"重新采集"fix。防止在有模板的情况下继续静默空转。

### 验证证据
- `--doctor` 实跑：`模板文件 = 2 个：buy.png, confirm.png`、`模板元数据 = 有`。
- `python tools/calibrate.py --dry-run` 子进程退出 0，输出含 clock/buy/confirm 三项计划。
- preflight 用例：缺模板 hint 同时含 `calibrate` / `templates/buy.png` / `regions`；模板大于区域 → 红；正常尺寸 → ok。
- 两个 spec 均含 `("templates", "templates")` 与 `tools/calibrate.py`。

---

## 5. R09-06 `.venv` 不可移植但 `start.bat` 优先用它

### 根因
`pyvenv.cfg` 内 `home=` 写死开发机路径（如 `C:\Users\<dev>\AppData\Local\Programs\Python\Python311`），拷贝到别的机器后 `.venv\Scripts\python.exe` 起不来，bat 双击闪退、无提示。

### 改法（`start.bat` 全量重写，三级探针）
1. **一级：`.venv` 真跑验证** —— 不只看文件存在，而是
   `"%VENV_PY%" -c "import sys,cv2,mss,numpy; assert sys.prefix"`，退出码 0 才用；用 `pythonw.exe`（存在时）静默启动，否则 `python.exe` 保留控制台。
2. **二级：`uv`** —— `uv sync` 后 `uv run python desktop.py`（首次自动建环境）。
3. **三级：系统 `python`** —— 校验 `sys.version_info[:2] >= (3,10)` 且能 `import cv2,mss,numpy`。
4. **全失败**：输出可读中文提示（缺什么、装什么、或用 `uv`），`pause` 保留窗口，不闪退。

### 验证证据
静态断言（bat 无副作用，不做真跑）：含 `if exist "%VENV_PY%"`、`-c "import sys,cv2,mss,numpy; assert sys.prefix"`、`uv sync`、版本探测、`pause` 分支。

---

## 6. R09-08 `_bye` 不看 `stop()` 结果直接退出

### 根因
`core/server.py` 的 `_bye`/`_quit` 直接 `os._exit()`；`scheduler.stop()` 是异步收尾（`(ok, msg)`），输入（`pydirectinput`/鼠标按键）可能仍在按下状态，退出后系统层面残留按键。

### 改法
- 该文件属"不要改"范围 → 真正落点在 `core/server.py`，见第 8 节。
- 本次在 `desktop.py` 提供可复用实现 `_release_input_before_exit(timeout=3.0)`：
  `sched.stop()` → 若返回 `False`，轮询 `engine.status().get("running")` 直到 `False` 或超时，循环内调 `engine.force_release_input()`；确认退出才置 `ok=True`；全过程写日志。
- `desktop.main()` 以 `try: return _run_app(args) finally: _release_input_before_exit()` 包裹，窗口关闭路径已生效。

### 验证证据
`FakeSched`：`stop() → (False, "正在停止（上一轮还在收尾）")`，`_engine.status() = {"running": False}` → `_release_input_before_exit()` 返回 `True` 且 `force_release_input` 被调用一次；无 scheduler 时直接返回 `True`（不阻塞）。

---

## 7. 其余条目

### R09-10 窗口尺寸固定 → 1366×768 / 150% 缩放下底部控件出屏
根因：`create_window(width=1180, height=800)` 写死，800 > 768 屏可用高度（且 150% 下逻辑像素更少）。
改法：`_primary_work_area()`（`SPI_GETWORKAREA`）→ `_fit_window(width, height, margin=16)` 按工作区夹紧并给 `min_size=(min(900,w), min(620,h))`。
证据：work area (1366×728) → `fit(1180, 800) = (1180, 712)`，高 ≥700；2560×1440 不越界。

### R09-12 价格解析把 `1.234` 读成 234
根因：点分位歧义（`.` 可能为千分位或小数点）。
现状复核：`core/ocr_chain.price_from_text()` 已返回 `(value, token)` 并对歧义串返回 `(None, token)` —— 已拒识，非本次新修。
证据（锁定防回归）：`"1.234" → None`、`"12.345" → None`、`"1,234" → 1234`、`"12,345" → 12345`。

### R09-13 字体自检把未引用字体标为必需
根因：`FONT_FILES` 全量当必需，`segmdl2.ttf`（Segoe MDL2 图标字体）在 UI 未引用，缺失即报红。
改法：`core/selfcheck.py` 拆 `FONT_REQUIRED = ("msyh.ttc",)`、`FONT_ICON_OPTIONAL = ("segmdl2.ttf",)`；`doctor_lines` 分 tag：必需 / 可选（UI 未引用）/ 可选；结论只看必需字体。
证据：`FONT_REQUIRED` 仅 `msyh.ttc`；`doctor_lines(deep=False)` 文本含 `mdl2` 且标"可选"。

### R09-09 冻结版自检路径 / OCR yaml 缺失
- 本次已做：`sjz.spec`、`sjz-onefile.spec` 的 `datas` 补 `templates/`、`tools/calibrate.py`、`OCR_game.yaml`、`OCR_tiny.yaml`；`desktop.py` 增 `_seed_missing_assets()`（`BASE(templates/tools) → APPDIR` 只补缺不覆盖，返回复制数），解决 onefile 下 `sys._MEIPASS` 资源在 exe 同级不可见。
- 服务端自检路径在 `core/server.py` → 第 8 节。
- 证据：seed 用例（monkeypatch `BASE`/`APPDIR`，复制 2 个文件；已存在的用户模板不被覆盖）。

### R09-07 TEMP / R09-11 时区
均落在非本范围文件 → 第 8 节。

---

## 8. 待接线清单（本文件范围内不可改，需接力）

| 编号 | 文件 / 位置 | 所需改动 | 影响 |
|---|---|---|---|
| R09-01 (P0) | `core/config.py` | 数据根改 `%LOCALAPPDATA%\SJZ-Control`；`task_dir`/保存/日志失败**显式抛错**而非吞掉 | 安装到 `Program Files` 时 `data/`、`config.json` 不可写 → 启动即失效 |
| R09-01 (P0) | `core/server.py` | 移除导入期 `_scheduler = Scheduler()`，改惰性构造 | 模块导入即建调度器，环境异常时启动直接崩 |
| R09-08 | `core/server.py` `_bye` / `_quit` | 用 `desktop._release_input_before_exit()` 等 `stop()` 确认后再退，禁止裸 `os._exit` | 退出残留按下状态 |
| R09-09 | `core/server.py` 自检段（约 1074–1083） | `server.__file__` 改 `config.TEMPLATE_DIR` / `DATA_DIR`；`.venv-vl` 告警仅在 RapidOCR 不可用时出现 | 冻结版自检误报路径 |
| R09-11 | `core/server.py`（约 929–932） | 硬编码 05:00–08:00 改为 `Asia/Shanghai` 取时或配置化 | 非该时段/跨时区休市判断错 |
| R09-02 补 | `core/scheduler.py` | 拷贝任务模板到 `task_dir` 时**一并复制 `meta.json`** | 缺 meta → 退回多尺度兜底，慢且易误配 |
| R09-07 | `core/config.py` / 打包 | 单文件模式 TEMP 写盘需可写目录与清理策略 | onefile 下运行期不可写 |
| R09-08 打包 | 发布流程 | exe 代码签名 + 版本资源 | 未签名触发 SmartScreen/杀软拦截 |
| R09-03 补 | `core/input.py` | `Mouse` 构造时缓存的虚拟屏 metrics 需在显示器拔插/RDP 后刷新 | 分辨率变化后鼠标坐标越界 |
| 前端 | `web/index.html` | 显示器选择项、框选入口（当前无 UI 入口，只能走 `--calibrate` / CLI） | 用户无法在界面内标定 |
| — | `core/scheduler.py`（他人进行中） | `stop()` 文案/返回契约与 `_release_input_before_exit` 对齐 | 退出确认依赖 `(ok, msg)` 语义稳定 |

---

## 9. 复核与回归

- **两种独立方式复核**：
  - 缩放：新实现（元数据 + `fx` 重采样）vs 独立仿射实现（`warpAffine`），分数差 ≤0.06、坐标一致。
  - 选屏：`_monitor_for_rect` vs 测试内手写交集面积循环（不复用被测函数）。
  - DPI：`shcore.GetProcessDpiAwareness` vs 另一读法，两值均为 2。
- `compileall`：`core`、`desktop.py`、`tools/calibrate.py` 全部通过。
- 新增测试：`tests/test_portability_hardening.py`，**17 passed**，单进程、按需，无长循环、无基准脚本，不移动真实光标、不开可见窗口。
- 全量回归（排除 soak / skin_ocr_real / market_video）：1449 passed，9 failed —— 失败项均为**他人进行中的改动或环境缺资产**，不在本次范围：
  - `test_fix_b`：`_shots/drv_onsale.png` 缺失（环境）
  - `test_robustness`：`tools/_r08_dbg.py`、`tools/_r08_merge.py` BOM（既有文件）
  - `test_state_hardening` ×5、`test_scheduler_*`：`core/scheduler.py` 正被他人修改（diff 已核实：`stop()` 文案改为"正在停止（引擎还没退出）"、新增模板检查）
