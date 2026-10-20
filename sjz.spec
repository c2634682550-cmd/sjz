# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、内嵌 WebView2 窗口。

为什么 hiddenimports 要手写 winrt：
  core/ocr.py 是惰性导入 —— 首次调用 OCR 时才 import winrt.windows.media.ocr。
  PyInstaller 的静态分析只看源码里的 import 语句，因此**一个 winrt 模块都
  扫不到**。实测在导入 core 之后、调用 OCR 之前，sys.modules 里 winrt 相关
  模块数为 0；真实调用一次后才出现 21 个。不写死这些名字，打包出的 exe 会在
  第一次识别时报 ModuleNotFoundError，而模板匹配主路径正常 —— 很难发现。

跑法：uv run pyinstaller sjz.spec --noconfirm
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.getcwd())

# winrt 是按名字拼出来的扩展模块，静态分析扫不到（见文件头注释）
WINRT_HIDDEN = [
    "winrt",
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.storage.streams",
    # 真正的二进制扩展（_winrt_windows_* 是 C++ 编译产物）
    "winrt._winrt",
    "winrt._winrt_windows_foundation",
    "winrt._winrt_windows_foundation_collections",
    "winrt._winrt_windows_globalization",
    "winrt._winrt_windows_graphics_imaging",
    "winrt._winrt_windows_media_ocr",
    "winrt._winrt_windows_storage_streams",
]

datas = [
    # 前端页面：server.py 用 __file__ 推出 ROOT/web，冻结后落在 _MEIPASS
    (os.path.join(ROOT, "web"), "web"),
]

HIDDEN = WINRT_HIDDEN + [
    "mss.windows",
    "clr_loader",
    "pythonnet",
]
# pywebview 的平台后端是按名字动态导入的，只收 Windows 用得到的那个；
# 全量 collect_submodules 会把 android/gtk/qt 这些本机没有的平台也拉进来报错。
for _m in collect_submodules("webview"):
    if ".platforms." in _m and not _m.endswith(("edgechromium", "winforms")):
        continue
    HIDDEN.append(_m)

hiddenimports = HIDDEN

a = Analysis(
    ["desktop.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SJZ-Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # upx 压过的 exe 常被杀软误报
    console=False,          # 桌面版不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
