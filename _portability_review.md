# 可移植性审查：换一台干净 Windows 机器能否直接跑起来

- 仓库：`origin = https://github.com/c2634682550-cmd/sjz`，分支 `main`，HEAD 审查时为工作区干净状态（`git status` 无跟踪文件改动）
- 审查方式：**只读**。不修改业务代码、不 commit、不 push。破坏性用例全部在项目外的临时副本（`%TEMP%\sjz_rev\proj`）上做「改名—还原」，临时目录已删除；仓库的 `config.json` 未被写入（LastWriteTime 仍为 2026-11-05 02:57）。
- 结论先行，证据在后。

---

## 一、结论

**总判断：分三层，只有第一层成立。**

| 层 | 能否直接换机器跑 | 依据 |
|---|---|---|
| 代码与依赖层 | **能**（装完依赖即可启动，任意 CWD 都能起） | 第三方 import 只有 `mss` / `cv2` / `numpy` / `pywebview`，四项全部在 `pyproject.toml` 声明；所有资源路径都按 `__file__` 定位；项目外 CWD 实测 200 |
| 「必须以独立窗口打开」硬约束 | **不能** | WebView2 缺失时 `desktop.py` 直接 `webbrowser.open()` 退成**浏览器标签**（`desktop.py:301`、`desktop.py:312`）；全仓没有任何 Edge/Chrome `--app` 兜底代码（grep `msedge|chrome|--app` 零命中）；`desktop.log` 里有 4 次真实降级记录 |
| 业务可用层 | **不能** | OCR 服务依赖仓外的 `.venv-vl`（`core/ocr_service.py:430`）且模型首次要联网下载（`:439`）；`templates/` 被 gitignore，新机器必须重新框选；启动检查硬性要求 1920×1080（`core/preflight.py:16`） |

### 最关键的 3 条

1. **缺 WebView2 → 变浏览器标签，而不是独立窗口。** `desktop.py` 的两条降级路径都调用 `_open_browser()`（`desktop.py:94-99` → 被 `:301` 和 `:312` 调用），即 `webbrowser.open(url)`，在默认浏览器里开标签页。这不仅违反本次硬约束，也在 `desktop.log:134 / :170 / :188` 留下真实事故（"多半是 WebView2 运行时缺失或初始化失败，改用浏览器打开"）。要满足约束，必须新增 `msedge --app` / `chrome --app` 兜底，并处理进程残留。
2. **OCR 是「半外置」的：换机器必然红灯。** 依赖清单里没有 paddle/paddleocr/easyocr（这是有意的，代码只通过 `urllib` 连本机 HTTP 服务，`core/ocr_ppocr.py:49`），但服务本体由 `core/ocr_service.py:430` 从 `<ROOT>/.venv-vl/Scripts/paddlex.exe` 拉起，该环境 2GB+ 且被 `.gitignore:3` 排除；模型首次运行才从 modelscope 下载。干净机器 clone 后 OCR 检查项恒为红（`core/preflight.py:98-101`），"启动"按钮被卡住。
3. **资源缺失不崩，但会静默降级/500。** `core/bricks.json` 缺失 → `_load()` 返回空库（`core/bricks.py:35-36`），皮肤定位接口全 404 且界面仍显示"11 个赛季"；`core/skins_raw.csv` 缺失 → `/api/skins/plan` 抛 `FileNotFoundError` 变 500（`core/skinfilter.py:76`，实测）。`web/index.html` 或 `web/assets/*` 缺失只是 404。

---

## 二、依赖对照表

扫描范围：`core/*.py`、`desktop.py`、`run.py`。用 AST 解析全部 `import` / `from ... import`，并单独检查 `importlib` / `__import__` / `import_module`（**零命中**，没有字符串形式的动态导入）。

| 第三方包 | 代码位置（import 点） | 声明位置 | 锁定版本 | 结论 |
|---|---|---|---|---|
| `mss` | `core/screen.py:11` | `pyproject.toml:7` `mss>=9.0,<11` | `uv.lock` → `mss 10.2.0` | ✅ 一致 |
| `opencv-python`（`import cv2`） | `core/cards.py:19`、`core/engine.py:27`、`core/glyphs.py:36`、`core/nav_ons.py:924`、`:997`、`core/ocr_ppocr.py:183`、`:234`、`core/pagewatch.py:18`、`core/vision.py:4` | `pyproject.toml:8` `opencv-python>=4.8,<6` | `5.0.0.93` | ✅ 一致 |
| `numpy` | `core/cards.py:20`、`core/engine.py:28`、`core/nav.py:32`、`core/nav_ons.py:14`、`core/ocr_ppocr.py:163`、`core/ocr_service.py:315`、`:456`、`core/pagewatch.py:19`、`core/screen.py:12`、`core/vision.py:5` | `pyproject.toml:9` `numpy>=1.24,<3` | `2.2.6`(<3.11) / `2.4.6`(3.11) / `2.5.4`(>=3.12) | ✅ 区间可满足；本机 venv 为 2.4.6，与 3.11 的锁一致 |
| `pywebview`（`import webview`） | `desktop.py:28`（`try/except` 包裹，缺失不致命） | `pyproject.toml:13` `pywebview>=6.2.1` | `6.2.1` | ✅ 一致；注意它是**可选**依赖，缺了就退浏览器 |
| `pythonnet` / `bottle` / `proxy-tools` | 无直接 import（pywebview 在 Windows 上的传递依赖） | 未直接声明（正确做法） | `pythonnet 3.2.1`(>=3.11) / `3.1.0`(<3.11)、`bottle`、`proxy-tools` | ✅ 传递依赖，`uv.lock:821-834` 已解析 |
| `pywin32` | **无任何 import**（所有 Win32 调用走 `ctypes.windll`，见第四节） | 未声明 | — | ✅ 不需要声明；`build.bat` 里 PyInstaller 自带的 `pywin32-ctypes` 属构建期 |
| `Pillow` | 无 import（`tests/` 里也没有 `PIL`/`Image`） | `pyproject.toml:18`（**dev 组**） | `12.3.0` / `13.0.0` | ⚠️ 声明了但无人使用，可删（不影响可移植性） |
| `pyinstaller` | 无 import（构建工具） | `pyproject.toml:19`（dev 组） | `6.22.3` | ✅ `build.bat:9` 用 `--group dev` 同步 |
| `pytest` | 无 import（测试运行器） | `pyproject.toml:20`（dev 组） | `9.1.1` | ✅ |
| `paddle` / `paddleocr` / `paddlex` / `easyocr` | **无 import**（只通过 HTTP 调用，`core/ocr_ppocr.py:49`、`core/ocr_service.py:427-451`） | 未声明（有意为之，见 `pyproject.toml:10-12`） | — | ⚠️ 不是依赖缺失，而是"服务外置"：运行器在仓外 `.venv-vl` |
| `fastapi` / `uvicorn` | **无 import**（HTTP 服务用 `http.server`，`core/server.py:29`） | 未声明 | — | ✅ 不需要（uvicorn 只出现在 `.venv-vl` 由 paddlex 自己拉起的子进程里） |
| `interception`（第三方 Python 包） | **无 import**：`core/interception.py` 直接用 `ctypes.CDLL` 加载 DLL | 未声明 | — | ✅ 不需要（DLL 随仓库提供，`git ls-files` 确认 `core/assets/interception.dll` 已入库） |

**依赖清单完整性结论**：没有「代码 import 了但依赖没声明」的项；没有「只在 extras/group 里却运行期必需」的项；没有版本区间无法满足的项。唯一真正的问题是 **`uv.lock` 与 `pyproject.toml` 不同步**（见问题 M1），以及 **OCR 运行时被设计成仓外环境**（问题 H3）。

版本区间冲突检查：`numpy>=1.24,<3` + `opencv-python>=4.8,<6` → 锁到 `numpy 2.x + cv2 5.0.0.93`，无冲突；`requires-python = ">=3.10"`（`pyproject.toml:5`）覆盖 3.10/3.11/3.12 三个 marker 分支。

---

## 三、绝对路径与个人环境

全仓 grep `C:\`、`C:/`、`Desktop`、`26346`、`.venv`、`AppData`、`Users\`、`python.exe` 的结果，按「配置默认值（可改）」/「写死在代码里」分类：

| 位置 | 内容 | 性质 | 换机器表现 |
|---|---|---|---|
| `core/config.py:26-30` | `ROOT = dirname(dirname(__file__))`（冻结时 `dirname(sys.executable)`）；`CONFIG_PATH`/`TEMPLATE_DIR`/`DATA_DIR` 全部由 `ROOT` 拼出 | ✅ 结构性，无硬编码 | 正常 |
| `core/interception.py:94-95` | `r"C:\Users\26346\Downloads\AxonSkin+鼠标驱动\drivers\drivers\interception.dll"` —— DLL 候选列表的**最后一档兜底** | ⚠️ 写死的个人路径 | 干净机器上该路径不存在，自然跳过；不影响主流程（仓库自带 DLL，前三档优先） |
| `core/interception.py:87-93` | 候选顺序：`_MEIPASS/interception.dll` → `core/assets/interception.dll` → `core/interception.dll`，全部按 `__file__` 定位 | ✅ 不依赖 CWD | 正常 |
| `core/ocr_service.py:429-430` | `root = config.ROOT`；`exe = root/.venv-vl/Scripts/paddlex.exe` | ⚠️ 拼路径正确，但**仓外环境** | 干净机器必报 `FileNotFoundError: 没有 ...paddlex.exe` |
| `core/ocr_service.py:434` | `yaml = root/tools/OCR_game.yaml`，存在才用，否则回落 `--pipeline OCR` | ✅ 相对 `ROOT` | 正常（回落会丢自定义产线） |
| `core/ocr_service.py:440` | `ocr_service_<port>.log` 写在 `ROOT` | ✅ 相对 `ROOT` | 正常（exe 同级目录可写） |
| `core/server.py:41-42` | `ROOT = dirname(dirname(__file__))`；`STATIC = ROOT/web` | ✅ 冻结后在 `_MEIPASS/web` | 正常 |
| `core/config.py:30-32` | `config.json` / `templates` / `data` 放 `ROOT` | ✅ exe 同级、源码仓库根 | 正常 |
| `core/bricks.py` / `core/skinfilter.py` / `core/glyphs.py` | 数据文件按 `__file__` 同目录/同年目录 `assets` 定位（`core/glyphs.py:55` 注释明确） | ✅ | 正常 |
| `sjz.spec:19` | `ROOT = os.path.abspath(os.getcwd())` | ⚠️ 以 **CWD** 为基准 | `build.bat:3` 已 `cd /d "%~dp0"`，从仓库根跑没问题；手动在别处跑 pyinstaller 会打出空 datas |
| `tools/*.py`（10 个文件） | `r"C:\Users\26346\Downloads\AxonSkin v2.3新算法.exe"` 等默认参数（如 `tools/axon_watch.py:117`、`tools/dump_axon.py:28`、`tools/capture_auth.py:26`） | ⚠️ 个人逆向工具，非主程序 | 不影响启动/运行；只有人肉跑这些脚本时报文件不存在 |
| `docs/*.md`、`说明.txt` | 大量 `C:\Users\...` 示例路径 | ✅ 文档 | 无影响 |

补充事实：`core/assets/` 实际只有 `glyphs/` 和 `interception.dll`；**任务清单里的 `core/assets/digits/` 不存在，代码里也没有任何 `assets/digits` 引用**（`digits` 只作为函数名出现于 `core/market.py:57`、`core/ocr.py:343`），这一项为 N/A。

---

## 四、相对路径与工作目录假设（实测）

结论：**所有 `open()`/`Path()` 都以 `__file__` 推导出的 `ROOT` 为基准，不以 CWD 为基准。** 实测在项目目录外启动，全部正常：

| 用例 | 命令 | 结果 |
|---|---|---|
| 项目外跑入口 | `cd C:\` + `python <repo>\run.py --help` | ✅ 正常打印帮助，无路径报错 |
| 项目外跑自检 | `cd C:\` + `python <repo>\desktop.py --doctor` | ✅ 打印 `BASE / config.json / templates / data / web` 均为仓库内绝对路径；`cwd = C:\` 但 `web 存在 = True` |
| 项目外真正起服务 | `CWD=C:\` + `python <repo>\run.py --no-browser --port 8810` | ✅ `GET /` → 200（40608 字符）、`GET /api/config` → 200；stderr 空 |
| 副本内跑自检 | `python <copy>\desktop.py --doctor` | ✅ `BASE` 指向副本，路径随之改变（证明是 `__file__` 推导而非写死） |

原理：`run.py` 由 `python <path>/run.py` 启动时 `sys.path[0]` 是脚本目录；`desktop.py:25` 显式 `sys.path.insert(0, BASE)`；`core/config.py:26-28` 用 `__file__` 推 `ROOT`。

---

## 五、缺失资源时的行为（实测）

方式：把整个仓库（除 `.git`/`.venv`/`.venv-vl`）复制到 `%TEMP%\sjz_rev\proj`，在副本里逐项改名 → 起服务/调函数 → 立即还原 → 用 SHA256 比对，最后删除整个临时目录。仓库本体零改动。

| 缺失项 | 操作 | 启动 | 具体表现 | 判定 |
|---|---|---|---|---|
| `core/bricks.json` | 改名 | ✅ 200 | `bricks._load()` → `seasons=0`；`identify()` → `{"ok": False, "reason": "对不上任何赛季砖皮"}`；`/api/skins/plan` 全部 **404**（"表里没有"） | ❌ **静默降级**，与"名字写错"无法区分；界面仍显示"11 个赛季"（`sjz.spec:24-28` 注释已自述） |
| `core/skins_raw.csv` | 改名 | ✅ 200 | `skinfilter.load()` 抛 `FileNotFoundError`；`/api/skins/plan` → **500**，服务端打印完整 traceback（`core/skinfilter.py:76`） | ⚠️ 明确报错但以 500 形式，前端只能看到通用错误 |
| `core/assets/glyphs/` | 改名 | ✅ 200 | `glyphs.load()` → `{}`；`identify()` → `(None, 0.0, 0.0)` | ❌ 静默降级（OCR 小字退回网格推断） |
| `core/assets/interception.dll` | 改名 | ✅ 200 | 候选里第 4 档命中 `C:\Users\26346\Downloads\...\interception.dll` → `driver_ready = (True, '')`（**本机特有**，干净机器上会变成 `"interception.dll 找不到（应放在 core/assets/ 下）"`，见 `core/interception.py:141`） | ✅ 有明确错误文本；不崩 |
| `web/assets/*`（整个目录） | 改名 | ✅ 200 | `GET /` → 200；`GET /assets/df-logo.png` → **404**（`core/server.py:343-346`）。注：`web/index.html` 里**没有任何** `assets/` 引用（grep 零命中），这两张图目前是死资源 | ✅ 无害 |
| `web/index.html` | 改名 | ✅ 200 | `GET /` → **404**（`core/server.py:326-327`，响应体 `missing index.html`）；`/api/config` 仍 200 | ⚠️ 前端整体不可用，但错误文本明确 |
| `templates/`（整目录，模拟 clone 后） | 改名 | ✅ 200 | 启动时 `core/config.py:252` 自动重建空目录；`templates_ready = {'buy': False, 'confirm': True}`；`/api/config` 200 | ✅ 预期行为：要求用户重新框选 |
| `templates/buy.png` | 改名 | — | `has_template('buy') = False`，`templates_ready` 中 `buy=False` | ✅ 明确（启动检查红灯） |
| `config.json`（删除） | 删除 | ✅ 200 | `Config.load()` 自动创建默认 `config.json`，`load_ok=True`，`regions.buy=None` | ✅ 首启可跑（功能待配置） |
| `config.json`（语法坏 `{ not json`） | 覆写 | ✅ 200 | 隔离为 `config.json.broken-<时间戳>.bad` + 本次用默认值，日志 `[config] config.json 读不出来（...），已备份为 ...` | ✅ |
| `config.json`（根是数组 `[1,2,3]`） | 覆写 | ✅ 200 | 同上；错误文本 `顶层不是 JSON 对象（list）` | ✅ |
| 端口被占（`run.py --port 8809` 起第二个） | — | ❌ | `!! 端口 8809 起不来（多半是另一个控制台还在跑）：[WinError 10048]` + 完整 traceback（`core/server.py:1051-1053`），退出码 1 | ⚠️ 明确但粗暴；`desktop.py` 会自动换端口 |
| `uv.lock` 一致性 | `uv lock --check` | — | `error: The lockfile at uv.lock needs to be updated, but --check was provided.`（退出码 1） | ❌ 见 M1 |

还原校验：`core/bricks.json`、`core/skins_raw.csv`、`core/assets/interception.dll`、`config.json` 改名还原后 SHA256 完全一致（如 `interception.dll` `ab88164c11b1b484 → ab88164c11b1b484`）。

---

## 六、平台与权限假设

**Windows 专属 API（静态清单）**

| API | 位置 | 用途 | 非 Windows 后果 |
|---|---|---|---|
| `ctypes.windll.user32` / `kernel32` / `advapi32` / `shell32` / `shcore` | `core/input.py:10-11`、`:565-566`、`:781`；`core/hotkey.py:15`、`:89`、`:298`；`core/nav_ons.py:364`、`:494`；`core/pagewatch.py:141`；`core/screen.py:18`、`:21`；`core/single_instance.py:34`、`:75`、`:87`；`desktop.py:372` | 输入注入、热键、DPI、单实例、截图尺寸 | `ctypes` 在 Win 以外无 `windll` 属性 → **import 阶段即 `AttributeError`**，三条入口全崩（静态判断，未实跑） |
| `CreateMutexW` | `core/single_instance.py:37-43` | 单实例闸 | 同上 |
| `SetWindowsHookExW` | `core/hotkey.py:89`、`:176`（`WH_KEYBOARD_LL`） | 游戏独占全屏时的热键 | 同上 |
| `RegisterHotKey` | `core/hotkey.py:298` | 备用热键路径 | 同上 |
| `SendInput` | `core/input.py:118` | legacy 输入通道 | 同上 |
| `ShellExecuteW(..., "runas")` | `core/input.py:781` → `core/input.py:768` 左右（`elevate_self`） | 游戏是管理员时自动提权重启 | 无 UAC 的平台不可用 |
| `MessageBoxW` | `desktop.py:372` | 无控制台时弹启动失败框 | 异常路径里二次异常 |

**管理员 / 权限**：不强制以管理员启动；但当检测到游戏进程完整性级别更高时会主动 `ShellExecuteW runas` 重开（`core/input.py:768-785`），干净机器上会弹 UAC。Interception 走内核驱动，需要**单独安装驱动**（`tools/interception/install-interception.exe` 已入库）且安装需管理员、通常需重启；未装驱动时 `core/input.py` 会自动退 `PostMessage`/legacy（启动检查黄灯，不崩）。

**DPI**：`core/screen.py:15-21` 有 `SetProcessDpiAwareness(2)` → `SetProcessDPIAware()` 兜底，但**调用点在 `Screen.__init__`（`core/screen.py:53`）**，而桌面模式下 pywebview 窗口在此之前已创建（`desktop.py:267`）。Windows 规定进程 DPI 感知必须在创建任何窗口前设置，之后再调通常返回 `E_ACCESSDENIED`（`SetProcessDpiAwareness` 返回 HRESULT，不是异常，代码不会感知失败）→ 125%/150% 缩放下 mss 拿到的是虚拟化坐标，框选坐标会整体偏移（**推测**：需在 125% 缩放机器上复现；`core/nav.py:152` 的注释也提示同一风险）。

**pywebview 后端**：Windows 上走 EdgeChromium（WebView2），依赖 `pythonnet` + `clr_loader`（`sjz.spec:48-51` 已收 hiddenimports）。WebView2 Runtime 不随 Python/uv 安装，需系统自带或单独安装。

---

## 七、配置与首次启动

- `config.json` 被 `.gitignore:8` 排除 → 新机器 clone 后不存在；实测缺失时 `Config.load()` **自动生成默认文件**并以默认值运行，`load_ok=True`（`core/config.py:430-520` 区间逻辑）。
- 默认值里**没有**指向本机不存在路径的项：`ROOT` 由 `__file__` 推导，`OCR url` 固定 `http://127.0.0.1:8081`（`core/config.py:135`），`regions` 全为 `None`。唯一"默认指向不存在的东西"的是 OCR 服务本体（`.venv-vl`），但那是运行时目录而非配置值。
- `config.json` 损坏（语法错 / 顶层非对象）→ 隔离为 `config.json.broken-YYYYMMDD_HHMMSS.bad` + 用默认值启动，输出明确中文提示（实测见第五节）。
- `templates/` 与 `data/` 也被忽略（`.gitignore:9-10`），首次启动由 `core/config.py:252`、`:524` 的 `makedirs` 自动创建。

---

## 八、打包路径（`build.bat` + `sjz.spec`）

| 检查项 | 结论 |
|---|---|
| `web/` 是否收集 | ✅ `sjz.spec:23` `(ROOT/web, "web")` |
| `core/bricks.json` | ✅ `sjz.spec:29` |
| `core/skins_raw.csv` | ✅ `sjz.spec:33` |
| `core/assets/`（含 glyphs + interception.dll） | ✅ `sjz.spec:38`，且 `:43` 显式再声明 DLL |
| `sys._MEIPASS` 分支 | ✅ `desktop.py:19-24`（`BASE = sys._MEIPASS`，`APPDIR = exe 同级`）；`core/config.py:22-26`；`core/server.py:41-42` 靠 `__file__` 自然落到 `_MEIPASS/web` —— 没有被 `__file__` 相对路径绕过 |
| `core/assets/digits` | N/A（不存在） |
| `tools/OCR_game.yaml` | ❌ **未收集**（spec 的 datas 里没有 tools）。exe 里 `core/ocr_service.py:434-436` 会回落 `--pipeline OCR` |
| `.venv-vl`（OCR 服务） | ❌ 不可能进 exe（2GB+，`sjz.spec:8` 注释也承认"真正跑模型的是 .venv-vl 那个服务进程"）→ **打包版永远没有 OCR 服务**，除非用户手工在 exe 同级放一个 `.venv-vl` |
| spec 的 ROOT 基准 | ⚠️ `sjz.spec:19` 用 `os.getcwd()`；`build.bat:3` 先 `cd /d "%~dp0"`，从仓库根跑没问题，但换个目录直接 `pyinstaller sjz.spec` 会打出空 datas（且测试 `tests/test_packaging.py` 因为同样 exec 逻辑也发现不了） |
| 文档一致性 | ⚠️ `docs/build-desktop.md:52-69` 仍在教"winrt 惰性导入 + hiddenimports 7 个 winrt 模块"，但 `pyproject.toml:10-12` 已把 winrt 全删、`sjz.spec` 也没有 winrt（stale doc） |

---

## 九、干净机器启动链路（本次追加的硬约束专项）

> 硬约束：**前端必须以独立窗口打开（内嵌 WebView2 的桌面窗口，缺运行库时退到 Edge/Chrome 的 `--app` 模式窗口），不得以浏览器标签页形式呈现；启动脚本不得 `start http://...` 弹默认浏览器。**

### 9.1 桌面壳依赖现状

- `pywebview` 已声明：`pyproject.toml:13` `pywebview>=6.2.1`，锁 `6.2.1`（`uv.lock:821`），Windows 传递依赖 `pythonnet`/`bottle`/`proxy-tools` 齐全（`uv.lock:825-834`）。
- WebView2 Runtime 分布：Win11 全系预装；Win10 在装有 Microsoft Edge 的常规版本上通常已随 Edge 更新装上 Evergreen Runtime，但 **LTSC / 精简版 / 长期未更新 / 部分企业镜像可能没有**。本机实测注册表 `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` → `pv = 156.0.4314.58`（已装）。
- 缺 WebView2 时的**实际报错**：`webview.create_window()` 抛异常，或 `webview.start()` 立刻返回（`desktop.py:295-299` 注释即为此现象）。两条路径都只写日志 + `_open_browser(url)`。
- 是否有降级路径：**有，但降级目标是默认浏览器标签页**（`desktop.py:94-99`），不满足硬约束，"缺运行库时退到 Edge/Chrome `--app` 独立窗口"这一档**完全缺失**。

### 9.2 若补 Edge/Chrome `--app` 兜底：定位与残留（当前 0 覆盖）

| 事项 | 现状 | 建议实现 |
|---|---|---|
| 判断 WebView2 是否存在 | 无检查（`--doctor` 也不查） | 读 EdgeUpdate Clients 注册表键的 `pv`；或查 `C:\Program Files (x86)\Microsoft\EdgeWebView\Application\<ver>\msedgewebview2.exe` |
| 定位 Edge | 无 | ① `HKLM/HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe`（多数机器没有此项）② `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`、`C:\Program Files\Microsoft\Edge\Application\msedge.exe` ③ `where msedge`（实测本机 **不在 PATH**，`where.exe msedge` 返回 INFO: Could not find） |
| 定位 Chrome | 无 | `App Paths\chrome.exe` 默认值 → `C:\Program Files\Google\Chrome\Application\chrome.exe` → `C:\Program Files (x86)\...`（实测本机 `Get-Command chrome` 不存在） |
| 两个都没有 | 无处理 | 回落到"提示安装 Evergreen WebView2 Runtime（离线安装包）"或"提示装 Edge"，不要静默失败 |
| 启动形式 | 无 | `msedge.exe --app=<url> --user-data-dir=<%TEMP%\sjz-app-profile> --no-first-run --no-default-browser-check` |
| 进程残留 | 无处理 | Edge/Chrome `--app` 是**独立窗口但共享浏览器进程模型**；必须用独立 `--user-data-dir` 隔离，退出时按 profile 路径 `taskkill` 整棵进程树，否则窗口关了仍驻留后台（与当前 `desktop.log` 里"关窗不退出 → 端口漂移"的历史问题同类） |
| 启动脚本 | `start.bat:27` `start "" ".venv\Scripts\pythonw.exe" desktop.py`、`:39` `python desktop.py` —— **没有** `start http://` | ✅ 这一条已符合硬约束；真正违反的是 `run.py:56`（开发入口默认开浏览器）和 `desktop.py:301/312`（降级开浏览器标签） |

### 9.3 后端端口与绑定

| 项 | 结论 | 证据 |
|---|---|---|
| 是否固定端口 | 桌面版默认 8777，但 `_pick_port()` 会从 8777 起找 8777~8796 的空闲端口；`url` 用**实际选中**的端口 | `desktop.py:193`、`:81-91`、`:220-222` |
| 端口被占（桌面版） | 自动换端口，前端随之（URL 由选中端口拼出），不会崩 | 实测：8777 占用时会顺延；`desktop.log` 里 8777→8778→8779→8780 即此机制 |
| 端口被占（`run.py`） | **直接崩**：`WinError 10048` + traceback，退出码 1 | 实测；`core/server.py:1051-1053` |
| 绑定地址 | 默认 `127.0.0.1`，仅本机 | `desktop.py:194`、`run.py:13`、`core/server.py:1048` |
| 防火墙弹窗 | 不会弹（环回地址不触发 Windows 防火墙规则）；`/api/quit` 还额外校验来源 host | `core/server.py:672` |

### 9.4 首次启动是否要联网

| 环节 | 是否需要网络 | 证据 |
|---|---|---|
| Python 依赖 | **要**：`start.bat:32` `uv sync`（uv 本身也要先装）；`:39` 直接用系统 Python 跑但**不装依赖**，缺 `cv2` 即 `ImportError` | `start.bat:26-40` |
| OCR 运行环境 | **要**：`.venv-vl` 2GB+ 不在仓库（`.gitignore:3`），需按 `说明.txt` 第九节另装 | `core/ocr_service.py:430-433` |
| OCR 模型权重 | **要**：`PADDLE_PDX_MODEL_SOURCE=modelscope`，首次 `--serve` 才拉模型 | `core/ocr_service.py:439` |
| 字模 / 知识库 | **不要**：`core/assets/glyphs/`（24 张 PNG）、`core/bricks.json`、`core/skins_raw.csv` 均已入库 | `git ls-files` |
| 前端静态资源 | **不要**：`web/index.html`、`web/assets/*` 已入库 | `git ls-files` |

结论：**离线机器能启动窗口、能打开界面，但 OCR 检查恒红 → 不能抢购**（`core/preflight.py:89-101` 把 OCR 作为硬性检查项）。

### 9.5 全新 Win10 + 只装官方 Python 的最小步骤

| 步 | 动作 | 会不会卡 |
|---|---|---|
| 1 | 装 Python 3.11（勾 Add to PATH） | 不卡 |
| 2 | `git clone`（或下 zip） | 需网络；无 git 时用 zip |
| 3 | 装运行依赖：`pip install mss opencv-python numpy pywebview`（或装 uv 后 `uv sync`，注意会因 lock 过期而重解析） | 需网络；**这一步跳过就是黑色控制台闪退/`ImportError`** |
| 4 | 双击 `start.bat` → 走到 `:syspy` 分支 `python desktop.py` | **卡点 A**：WebView2 缺失 → 不是独立窗口，而是浏览器标签（违反硬约束）；且 `python` 分支留黑框 |
| 5 | （若卡点 A 命中）手动装 Evergreen WebView2 Runtime | 需网络/离线安装包；装完才有独立窗口 |
| 6 | 按 `说明.txt` 第九节装 `.venv-vl` + 首次下载模型 | 需网络、2GB+、GPU 驱动；不做则 OCR 红、不能开工 |
| 7 | 进游戏（窗口化 1920×1080），框选 buy/confirm 模板 | `templates/` 不入库，必须手工；非 1080p 屏幕直接红（`core/preflight.py:16`） |

**结论**：到"看到独立窗口"最少 **4 步**（Python → clone → 装依赖 → `start.bat`），前提是机器自带 WebView2；没有 WebView2 时第 4 步就退化成浏览器标签，需要第 5 步补救。到"能真正抢购"要 **7 步**，第 6 步（OCR 环境）和第 7 步（框选 + 1080p）是硬门槛。

---

## 十、问题清单

级别：**高** = 违反硬约束或干净机器直接不可用；**中** = 能起但功能显著降级/错误难排查；**低** = 体验、文档或冗余。

### 高

| # | 级别 | 位置 | 现象 | 干净机器上的表现 | 修复方向 |
|---|---|---|---|---|---|
| H1 | 高 | `desktop.py:298-303`、`desktop.py:308-314` → `desktop.py:94-99` | WebView2 缺失/初始化失败时 `_open_browser(url)` 用 `webbrowser.open` 开**默认浏览器标签** | 双击后看到浏览器标签页，不是独立窗口 → 直接违反本次硬约束；`desktop.log:134/:170/:188` 是真实发生过的记录 | 降级改为 Edge/Chrome `--app` 独立窗口（见 9.2）；彻底无浏览器时给出安装 WebView2 的明确提示 |
| H2 | 高 | `desktop.py:94-99`、`run.py:56`（全仓 grep `msedge|chrome|--app` 零命中） | 没有任何 Edge/Chrome app 模式实现；`run.py` 开发入口也默认开浏览器 | 机器只有 Edge 且 WebView2 缺失时，没有任何路径能开出独立窗口 | 新增 `_open_app_window()`：注册表/常见路径/`where` 三档定位 + 独立 `--user-data-dir` + 退出杀进程树；`run.py` 保持 `--no-browser` 之外的行为可不动（开发入口） |
| H3 | 高 | `core/ocr_service.py:429-439`、`.gitignore:3`、`core/preflight.py:98-101` | OCR 服务靠仓外 `.venv-vl`，模型首次联网下载 | clone 后 OCR 检查恒红 → "启动"被卡住，抢购不可用；打包 exe 同样无解 | 文档前置化（README 写明必须先装 `.venv-vl`）；或提供一键安装脚本 + 模型预置；或增加"无 OCR 只跑模板匹配"的可运行模式并让 preflight 明确降级 |

### 中

| # | 级别 | 位置 | 现象 | 干净机器上的表现 | 修复方向 |
|---|---|---|---|---|---|
| M1 | 中 | `pyproject.toml:6-14` vs `uv.lock:877-890` | `uv.lock` 仍把 7 个 `winrt-*` 当成 `sjz` 的依赖，而 `pyproject.toml:10-12` 已删除 | `uv lock --check` 报 `needs to be updated`（实测退出码 1）；`uv sync --frozen`/`--locked` 会失败；普通 `uv sync` 需联网重解析并改写 lock | 跑一次 `uv lock` 提交新 lock；CI 加 `uv lock --check` |
| M2 | 中 | `run.py:12`、`:57`、`core/server.py:1048-1053` | `run.py` 端口被占直接抛 `OSError`（WinError 10048）+ traceback | 开发/命令行入口在 8777 被占时直接崩（桌面版有 `_pick_port` 兜底） | 把 `_pick_port` 提到 `run.py` 复用，或捕获后打印"换 `--port`"并正常退出 |
| M3 | 中 | `core/bricks.py:35-36` | 缺 `bricks.json` 时返回空库而不是报错 | 皮肤识别/定位全部失效，界面仍显示"11 个赛季"，用户无法察觉是资源缺失 | `_load()` 区分"文件不存在"与"空库"：文件缺失时 `raise` 或写入调度器日志 warn |
| M4 | 中 | `core/skinfilter.py:76`（经 `core/server.py:260`） | 缺 `skins_raw.csv` 时 `io.open` 抛 `FileNotFoundError` → `/api/skins/plan` 500 | 前端只看到通用 500，服务端 traceback 在 stderr（pythonw 下看不到） | 捕获 `FileNotFoundError`，返回 409/503 + 中文提示"筛选表缺失" |
| M5 | 中 | `core/server.py:326-327` | 缺 `web/index.html` 时返回 `404 missing index.html` | 用户看到 404 文本页，不知道是安装不完整 | 返回带"安装不完整/请重新 clone"的 HTML，或启动时自检并写日志 |
| M6 | 中（推测） | `core/screen.py:53` → `:15-21`；窗口创建在 `desktop.py:267` | DPI 感知在**窗口创建之后**才设置，`SetProcessDpiAwareness` 通常返回 `E_ACCESSDENIED`（HRESULT，代码不感知失败） | 125%/150% 缩放的普通笔记本上截图/框选坐标整体偏移，表现为"点空" | 在 `desktop.py` 最顶部（`import` 之前、创建窗口之前）调用 `enable_dpi_awareness()`；`Run` 入口同理 |
| M7 | 中 | `sjz.spec:21-45`、`core/ocr_service.py:434-436` | 打包没收 `tools/OCR_game.yaml`，也不可能收 `.venv-vl` | exe 里 OCR 会回落 `--pipeline OCR` 且找不到 paddlex → OCR 永久不可用 | spec 增加 `tools/OCR_game.yaml`；文档明确"exe 需在同级目录自带 `.venv-vl`"或统一改为外部安装器 |
| M8 | 中 | `core/preflight.py:16`（`WANT_SIZE = (1920, 1080)`） | 硬编码 1080p 坐标 | 1366×768 / 2560×1440 的机器直接红，且无法通过任何配置绕过 | 至少给出"检测到分辨率不匹配"的独立文案与支持列表；长期做多分辨率标定 |
| M9 | 中 | `sjz.spec:19`（`ROOT = os.path.abspath(os.getcwd())`） | datas 以 CWD 为基准；测试用同样方式 exec，无法捕获 | 在仓库根跑没问题（`build.bat:3` 已 cd）；换目录手动打包 → 打出缺资源的 exe | 改为 `os.path.dirname(os.path.abspath(SPECPATH))` |

### 低

| # | 级别 | 位置 | 现象 | 修复方向 |
|---|---|---|---|---|
| L1 | 低 | `core/interception.py:94-95` | 个人目录 DLL 兜底路径写死 | 删除该档，或改为仅当存在 `SJZ_DEV` 环境变量时启用 |
| L2 | 低 | `tools/*.py`（`axon_watch.py:117`、`dump_axon.py:28`、`capture_auth.py:26` 等 10 处） | `C:\Users\26346\Downloads\AxonSkin v2.3新算法.exe` 作为默认参数 | 工具脚本，不影响主程序；可改成必填参数 |
| L3 | 低 | `docs/build-desktop.md:52-69` | 仍在讲 winrt hiddenimports，与现状相反 | 更新文档 |
| L4 | 低 | `pyproject.toml:18` | `pillow` 在 dev 组但全仓无 `PIL` 引用 | 删掉 |
| L5 | 低 | `start.bat:39` | 系统 Python 分支不装依赖，缺 `cv2` 时控制台 traceback；且 `python` 会留黑框（`:27` 的 pythonw 不留） | 该分支先 `pip install -r` 或提示"请先装依赖"；统一用 `pythonw` |
| L6 | 低 | `web/index.html`（全文无 `assets/`）vs `web/assets/df-bg.jpg`、`df-logo.png` | 两张图是死资源，缺失只 404 | 要么在前端引用，要么从仓库删掉 |
| L7 | 低 | `desktop.py:372` | 异常路径再调 `MessageBoxW`，非 Windows 下二次异常（已被 `except Exception` 兜住） | 无需改；仅记录 |
| L8 | 低 | `core/config.py` 默认 OCR 配置 | 新机默认 `ocr.enabled` 实际为 `None`（嵌套在 service 子字典里） | 检查前端对 `None` 的渲染是否等价于"默认开" |

---

## 十一、已实测项 / 未实测项

### 已实测（本机，Windows，Python 3.11，venv 内依赖齐全）

1. `run.py --help` 在 `CWD=C:\` —— 正常。
2. `desktop.py --doctor` 在 `CWD=C:\` 与临时副本内 —— 路径全对，`web 存在 = True`。
3. `run.py --no-browser --port 8810` 在 `CWD=C:\` —— `/` 200、`/api/config` 200、stderr 空。
4. 副本内基线起服务 —— `/api/config` 200。
5. `core/bricks.json` 改名 —— 启动 200，`plan` 404，`_load()` 空库，`identify()` 明确 reason。
6. `core/skins_raw.csv` 改名 —— 启动 200，`/api/skins/plan` 500，`FileNotFoundError` traceback。
7. `core/assets/glyphs/` 改名 —— `glyphs.load() = {}`、`identify() = (None, 0.0, 0.0)`。
8. `core/assets/interception.dll` 改名 —— 回落个人 Downloads 路径并 `driver_ready=True`（本机特有），错误文本路径可用。
9. `web/assets/` 改名 —— `/` 200，`/assets/df-logo.png` 404。
10. `web/index.html` 改名 —— `/` 404（`missing index.html`），`/api/config` 200。
11. `templates/` 整目录改名 —— 启动自动重建空目录，`templates_ready.buy=False`。
12. `templates/buy.png` 改名 —— `has_template('buy')=False`。
13. `config.json` 删除 / 语法坏 / 根为数组 —— 自动生成默认、隔离 `.broken-*.bad` + 默认启动，`load_ok=True`。
14. `run.py` 端口占用 —— `WinError 10048` + traceback，退出码 1。
15. `uv lock --check` —— 报 lock 过期，退出码 1。
16. 环境探测：WebView2 Runtime `156.0.4314.58`（注册表）；Edge 在 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；`where msedge` 找不到；无 Chrome。
17. 资源还原校验：`bricks.json` / `skins_raw.csv` / `interception.dll` / `config.json` SHA256 一致；临时目录已删除；仓库 `git status` 无跟踪文件改动。
18. `desktop.log` 历史证据：4 处 `webview.start() 立刻返回 / WebView2 运行时缺失或初始化失败，改用浏览器打开`。

### 未实测（明确标注）

1. **真开 pywebview 独立窗口**：会弹实体窗口干扰桌面，只做静态分析 + 历史日志证据。
2. **干净 Win10 无 WebView2**：无法在本机卸载运行时来复现；结论基于代码路径 + 注册表/发行事实。
3. **非 Windows 运行**：按要求只做静态判断（`ctypes.windll` 在 import 阶段即失败）。
4. **打包 exe 实际构建/运行**：未跑 `build.bat`（会产出 `dist/` 与构建缓存）。打包结论来自 `sjz.spec` 静态核对 + `tests/test_packaging.py` 的覆盖面分析。
5. **125%/150% 缩放下的 DPI 行为**（M6 标"推测"）。
6. **Interception 驱动缺失后的输入降级链路**：本机已装驱动，未卸载验证。
7. **全新机器的 `uv sync` 全流程耗时/是否二次改写 lock**：只做了 `uv lock --check`（不联网）。

---

## 十二、可直接给用户看的摘要

> 这份代码换一台干净 Windows 电脑**能起来**，前提是装好依赖、机器自带 WebView2、再配一套 OCR 环境。具体说：
>
> **能直接复制走的部分**：`mss`/`opencv-python`/`numpy`/`pywebview` 四个第三方包在 `pyproject.toml` 里都声明了，没有"代码用了但没写进依赖"的东西；所有文件路径都用代码自身位置推导，从任何目录启动都不会因为相对路径崩（实测在 `C:\` 下跑正常）。
>
> **会卡住的三处**：
> 1. **独立窗口**。缺 WebView2 时程序不会开独立窗口，而是直接调默认浏览器开标签页（`desktop.py:301`/`:312`），仓库里没有 Edge/Chrome `--app` 兜底，`desktop.log` 里有 4 次真实降级记录。这一条不满足你的硬约束，需要补 app 模式窗口 + 进程收敛。
> 2. **OCR**。识别服务跑在仓库外一个 2GB+ 的 `.venv-vl` 环境里，模型第一次还要联网下载；干净机器 clone 后 OCR 检查永远是红的，"启动"按钮被卡住。
> 3. **配置与模板**。`config.json`、`templates/`、`data/` 都不入库——这部分设计是对的（缺 `config.json` 会自动生成默认值，坏了会备份成 `.broken-*.bad` 再用默认值），但新机器必须重新框选购买/确认按钮，且屏幕必须是 1920×1080，否则启动检查直接红。
>
> **资源缺失的行为**：`bricks.json` 缺失是"静默空库"（皮肤定位全 404，界面还显示 11 个赛季，最难查）；`skins_raw.csv` 缺失是 500 + traceback；`web/index.html` 缺失是 404；`glyphs`/`web/assets` 缺失只是能力降级，不影响启动。这些都不崩，但除了 CSV 之外都没有明确提示。
>
> **最小路径**：装 Python → clone → 装依赖（联网）→ `start.bat`，这 4 步能看到窗口；要有独立窗口必须机器自带 WebView2（没有就多一步装 Evergreen Runtime）；要能真正抢购还得再加 OCR 环境（联网、2GB+）和进游戏框选模板。
