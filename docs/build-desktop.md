# 打包成桌面 exe

成品默认是 **onedir 文件夹**：`dist\SJZ-Control\SJZ-Control.exe` 双击即开原生窗口
（内嵌系统 WebView2），不需要装 Python。交付时把整个 `dist\SJZ-Control` 目录交给
安装器（Inno Setup）打成 setup，未压缩约 223 MB、559 个文件；单文件版另出，
在 `dist-onefile\SJZ-Control.exe`（102 MB）。

为什么默认 onedir：冷启动快 2~3 倍、杀软误报面小、可差量更新、崩溃不留
`%TEMP%\_MEI*` 残留。取舍与实测数字见 `_packaging_license.md`。

```bat
build.bat
```

`build.bat` 会依次做四件事：同步依赖 → 跑测试 → 打 onedir → 打 onefile。
**测试不通过就不出包。** 想跳过测试直接打，用：

```bat
uv run pyinstaller sjz.spec --noconfirm
uv run pyinstaller sjz-onefile.spec --noconfirm --distpath dist-onefile
```

## 两个入口的分工

| 文件 | 场景 | 行为 |
|---|---|---|
| `run.py` | 开发 | 起服务后用默认浏览器打开 —— 改完前端刷新就行，不用重启 |
| `desktop.py` | 交付 | 起服务 + 开原生窗口；打包的就是它 |

两者共用 `core.server` 的全部端点，只有外壳不同。

`desktop.py` 还有 `--doctor`，打印运行时路径、OCR 自检，以及扩展段
（模型文件 sha256、Interception DLL/内核服务、WebView2 版本、字体、权限级别、
许可状态）后退出，排查打包问题时用：

```bat
"dist\SJZ-Control\SJZ-Control.exe" --doctor
```

windowed exe 没有控制台，输出同时写进 exe 同级的 `desktop.log`。

## 出问题时看日志

exe 是无控制台的（windowed），`print` 什么都看不见。所以 `desktop.py` 会把启动
过程写进 **exe 同级的 `desktop.log`**：

```
03:19:03 --- 启动 (frozen=True) ---
03:19:03 BASE=C:\Users\...\Temp\_MEI000153882
03:19:05 选中端口 8991
03:19:06 服务已就绪，开始建窗口
03:19:06 create_window 返回
03:19:06 调用 webview.start()
```

窗口起不来的典型症状是**双击后什么都没发生**。日志会写清楚停在哪一步：
端口没选出来、服务没起来、还是 `webview.start()` 立刻返回（后者基本是
WebView2 运行时缺失）。

## 打包时踩过的三个坑

### 1. winrt 是惰性导入，PyInstaller 扫不到

`core/ocr.py` 首次调用 OCR 时才 `import winrt.windows.media.ocr`。PyInstaller 的
静态分析只看源码里的 `import` 语句，因此**一个 winrt 模块都扫不到**。

实测：导入 `core` 之后、调用 OCR 之前，`sys.modules` 里 winrt 相关模块数为 **0**；
真实调用一次后才出现 21 个。

漏掉的后果很隐蔽：模板匹配抢购主路径照常工作，只有 OCR 悄悄失败。所以
`sjz.spec` 里把 7 个 `winrt._winrt_windows_*` 扩展模块和对应的 Python 包显式
写进了 `hiddenimports`。**新增 winrt 命名空间时必须同步补进去。**

验证方式（不用开窗口）：

```bat
"dist\SJZ-Control.exe" --doctor
# 应当看到：OCR 后端 = WinRT Windows.Media.Ocr（语言 zh-Hans-CN）
```

### 2. 单文件 exe 的配置会写到临时目录

单文件模式下 `sys._MEIPASS` 是临时解包目录，**退出即删**。如果 `config.json`
和 `templates/` 按 `__file__` 推导，用户每次启动都会丢配置。

`core/config.py` 因此分流：冻结时用 `sys.executable` 的目录（exe 同级），
源码运行时用仓库根。`core/server.py` 的静态资源不用改 —— `web/` 是只读资源，
放在解包目录里正合适。

**但只读数据文件要跟着解包目录走**，不能和 config 一样放 exe 同级：

| 文件 | 放哪 | 为什么 |
|---|---|---|
| `config.json` / `templates/` / `data/` | exe 同级 | 用户要能改、能备份 |
| `web/` | `_MEIPASS` | 只读静态资源 |
| `core/bricks.json` | **`_MEIPASS/core/`** | `bricks.py` 用 `__file__` 找同目录 |

`data/` 里有两个东西是运行时写的，都必须跟着 exe 走：

| 文件 | 谁写的 | 放哪 |
|---|---|---|
| `data/records.jsonl` | `core/records.py` | `config.DATA_DIR`（exe 同级的 `data/`） |
| `data/shots/*.jpg` | `core/engine.py` 成交后存图 | 同上 |

这两处以前各自用 `dirname(dirname(__file__))` 推路径 —— 冻结后那是解包目录，
**记录和截图写进去随 exe 退出一起没了**。现在统一走 `config.DATA_DIR`。

`core/bricks.json` 漏收进 `sjz.spec` 的 `datas` 时**不会报错**：`_load()` 读不到
就返回空 seasons，`identify()` 永远回「对不上任何赛季砖皮」，而界面上仍然
写着「11 个赛季」。这种静默空库只能靠测试抓 —— 见 `tests/test_packaging.py`。

### 3. js_api 对象的属性名必须以下划线开头

`desktop.py` 的 `WinCtl` 要持有 pywebview 的 `Window` 引用才能 minimize/destroy。
pywebview 的 `get_functions()`（`webview/util.py`）会把 js_api 对象的**所有不以
`_` 开头的属性递归展开**成 JS 可调用项，而 `Window` 的对象图里有大量互相引用。

后果是**窗口能开、页面白屏、所有 HTTP 请求超时** —— 后台线程连 `Thread.start()`
都等不到 GIL。Python 3.11 和 3.14 都能复现，源码运行和打包 exe 都一样。
排查时 `faulthandler` 的线程栈一眼能看出来：一个线程停在
`webview/util.py:196 get_functions` 的递归里。

所以那条引用必须叫 `_win`。`tests/test_desktop_jsapi.py` 钉住了 `WinCtl`
对外可见的名字集合。

## 参数

```bat
"dist\SJZ-Control.exe" --port 8777 --host 127.0.0.1 --width 1180 --height 800
```

端口被占用时会自动往后找（最多试 20 个），不会弹错误让用户去杀进程。

## 已知的无害警告

构建日志里这些可以忽略：

- `Failed to collect submodules for 'webview.platforms.android'` —— 本机没有
  android 平台，`collect_submodules` 扫描目录时冒出来的；spec 里已过滤掉非
  Windows 平台
- `Hidden import "pycparser.lextab"/"yacctab" not found` —— cffi 的可选加速表，
  缺失时自动回退到纯 Python 实现
- 大量 `missing module named pwd/grp/posix/termios/fcntl` —— POSIX 专用模块，
  Windows 上本来就不存在

## 体积（2026-11-07 实测）

| 产物 | 体积 | 冷启动（`--doctor` 全程） |
|---|---|---|
| onedir `dist\SJZ-Control\` | 223.4 MB / 559 文件（exe 本体 9.3 MB） | 2.2 s（Python 计时）/ 3.0 s（PowerShell 计时） |
| onefile `dist-onefile\SJZ-Control.exe` | 102.0 MB | 5.6 s / 5.0 s |
| 旧 onefile（含重复模型 + cv2 ffmpeg） | 146.9 MB | 5.63 s |

主要构成：cv2 81.9 MB、onnxruntime 37.5 MB、`core/assets`（模型 + 字形模板）30.1 MB、
numpy 25.9 MB、PIL 12.9 MB。OPENCV 占大头，但模板匹配离不开它。
UPX 关闭 —— 压过的 exe 常被杀软误报。

两处已做的裁剪（`sjz.spec` / `sjz-onefile.spec` 里都写了注释）：

1. `rapidocr` 包自带的 PP-OCRv6 模型和 `core/assets/rapid` 完全重复（约 30 MB），
   打包时只收它的 yaml，不收 `.onnx`；
2. `opencv_videoio_ffmpeg*.dll`（约 29 MB）只服务 VideoCapture/VideoWriter，本项目
   不用视频 IO，直接剔除。

想再往下压，见 `_packaging_license.md` 的「模型量化」一节：能做，但必须先过
`tools/ocr_eval.py` 的精度门槛，不能凭感觉换模型。
