# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件（备用形态，只在必须「一个 exe」时用）。

和 sjz.spec 的差异只有最后一步：把 binaries/datas 塞进 EXE。
体积裁剪逻辑完全一致（rapidocr 重复模型剔除、cv2 ffmpeg 剔除、
onnxruntime 训练模块排除）。

代价（实测见 _packaging_license.md）：
  · 每次启动都要把全部内容解到 %TEMP%\\_MEIxxxx，冷启动慢 3~4s；
  · 崩溃残留 _MEI 目录；
  · 运行中无法自更新。

跑法：uv run pyinstaller sjz-onefile.spec --noconfirm
  产物：dist-onefile\\SJZ-Control.exe
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())
_VER_INFO = os.path.join(ROOT, "build", "version_info.txt")

_RUNTIME_TOOLS = [
    "fire_worker.py",
    "calibrate.py",
    "OCR_game.yaml",
    "OCR_tiny.yaml",
]

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "core", "bricks.json"), "core"),
    (os.path.join(ROOT, "core", "skins_lib.json"), "core"),
    (os.path.join(ROOT, "core", "skins_raw.csv"), "core"),
    (os.path.join(ROOT, "core", "assets"), os.path.join("core", "assets")),
    (os.path.join(ROOT, "core", "assets", "interception.dll"),
     os.path.join("core", "assets")),
    # 默认模板集 + 首次标定向导（onefile 也随包带上，_seed_missing_assets
    # 会释放到 exe 同级目录）
    (os.path.join(ROOT, "templates"), "templates"),
    # 进化框架源码（runs/fixtures/state 是本机运行产物，不进交付包）
    (os.path.join(ROOT, "evo", "evo_config.json"), "evo"),
    # 驱动安装器（安装器可选任务用）
    (os.path.join(ROOT, "tools", "interception"), os.path.join("tools", "interception")),
]
for _name in _RUNTIME_TOOLS:
    datas.append((os.path.join(ROOT, "tools", _name), "tools"))

_EVO_SKIP = {"runs", "fixtures", "state", ".state", "__pycache__"}


def _evo_datas():
    out = []
    for base, dirs, files in os.walk(os.path.join(ROOT, "evo")):
        dirs[:] = [d for d in dirs if d not in _EVO_SKIP]
        for f in files:
            p = os.path.join(base, f)
            rel = os.path.relpath(p, ROOT)
            out.append((p, os.path.dirname(rel)))
    return out


datas += _evo_datas()

_rapid = collect_data_files("rapidocr")
datas += [d for d in _rapid if not d[0].lower().endswith(".onnx")]

HIDDEN = ["mss.windows", "clr_loader", "pythonnet", "onnxruntime",
          # 动态/延迟 import 的核心模块（与 sjz.spec 保持一致）
          "core.humanmouse", "core.idle_roam", "core.repeat", "core.skip_late",
          "core.stop_grade", "core.click_burst", "core.money", "core.watchdog",
          "core.retention", "core.recovery", "core.scene_guard", "core.timesync",
          "core.ocr_rapid", "core.ocr_chain", "core.fire_proc", "core.version"]
HIDDEN += collect_submodules("rapidocr")
for _m in collect_submodules("webview"):
    if ".platforms." in _m and not _m.endswith(("edgechromium", "winforms")):
        continue
    HIDDEN.append(_m)

EXCLUDES = [
    "tkinter", "matplotlib", "pytest", "PyInstaller",
    "pandas", "scipy", "IPython", "notebook",
    "onnxruntime.quantization", "onnxruntime.tools",
    "onnxruntime.transformers", "onnxruntime.training",
    "onnxruntime.datasets",
]

a = Analysis(
    ["desktop.py"],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
a.binaries = [b for b in a.binaries
              if "opencv_videoio_ffmpeg" not in b[0].lower()]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SJZ-Control",
    version=_VER_INFO if os.path.exists(_VER_INFO) else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
