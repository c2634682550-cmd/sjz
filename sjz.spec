# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、内嵌 WebView2 窗口。

OCR 不再需要任何 hiddenimports：
  2026-11-01 起 OCR 只剩 PP-OCRv6 一个后端，走本机的 PaddleX 服务
  （core/ocr_ppocr.py 只用标准库 urllib + cv2 发 base64）。paddle、
  winrt 都不在 exe 里 —— 前者会把包撑到 1.5GB，后者已经不用了。
  真正跑模型的是 .venv-vl 里那个服务进程，见 说明.txt 第九节。

  所以这个 spec 现在只关心：前端静态资源、砖皮知识库、以及 webview。

跑法：uv run pyinstaller sjz.spec --noconfirm
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.getcwd())

datas = [
    # 前端页面：server.py 用 __file__ 推出 ROOT/web，冻结后落在 _MEIPASS
    (os.path.join(ROOT, "web"), "web"),
    # 砖皮知识库：core/bricks.py 用 __file__ 找同目录的 bricks.json。
    # 漏掉它的表现是「静默空库」—— _load() 读不到就返回空 seasons，
    # 识别永远回「对不上任何赛季砖皮」，而界面上仍写着「11 个赛季」。
    # 这条路径和 config.json 不同：config 要用户能改，所以放 exe 同级；
    # bricks.json 是只读数据，跟着 _MEIPASS 走才对。
    (os.path.join(ROOT, "core", "bricks.json"), "core"),
    # 筛选表：core/skinfilter.py 用 __file__ 找同目录的 skins_raw.csv，
    # 算出「这件皮肤在筛选面板上点哪儿」。漏掉它的话，打包后所有皮肤都
    # 只能靠全库扫描找 —— 慢十倍，而且品阶/枪种筛不了。
    (os.path.join(ROOT, "core", "skins_raw.csv"), "core"),
    # 内置字形模板：core/glyphs.py 用 __file__ 找同目录的 assets/glyphs。
    # 和 templates/ 不是一回事 —— templates/ 是用户自己框选的购买按钮模板
    # （运行时生成、放在 exe 同级）；这里是游戏字体渲染的只读参考数据，
    # 漏掉它的表现是 OCR 读不出的小字全部退回网格推断，滚动后可能认错名。
    (os.path.join(ROOT, "core", "assets"), os.path.join("core", "assets")),
    # Interception 用户态 DLL：core/input.py 驱动注入走它。DLL 本体只
    # 14KB，跟着 assets 目录已经带上；这里显式声明是防御 —— 有人把
    # DLL 挪出 assets 目录时打包还能找到。缺了它的表现：启动检查
    # 「输入驱动」亮黄灯、自动退 PostMessage，不会崩。
    (os.path.join(ROOT, "core", "assets", "interception.dll"),
     os.path.join("core", "assets")),
]

HIDDEN = [
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
