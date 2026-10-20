# 打包成桌面 exe

成品是**单文件 exe**，双击即开原生窗口（内嵌系统 WebView2），不需要装 Python，
也不用开浏览器。生成物在 `dist/SJZ-Control.exe`。

```bat
build.bat
```

`build.bat` 会依次做三件事：同步依赖 → 跑测试 → 打包。**测试不通过就不出 exe。**
想跳过测试直接打，用：

```bat
uv run pyinstaller sjz.spec --noconfirm
```

## 两个入口的分工

| 文件 | 场景 | 行为 |
|---|---|---|
| `run.py` | 开发 | 起服务后用默认浏览器打开 —— 改完前端刷新就行，不用重启 |
| `desktop.py` | 交付 | 起服务 + 开原生窗口；打包的就是它 |

两者共用 `core.server` 的全部端点，只有外壳不同。

`desktop.py` 还有 `--doctor`，打印运行时路径和 OCR 自检后退出，排查打包问题时用：

```bat
"dist\SJZ-Control.exe" --doctor
```

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

## 打包时踩过的两个坑

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

## 体积

约 **66 MB**。主要来自 opencv（抢购主路径的模板匹配必需）、numpy 和
WebView2 的 .NET 桥（pythonnet/clr-loader）。UPX 关闭 —— 压过的 exe 常被杀软误报。
