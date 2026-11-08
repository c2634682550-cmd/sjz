# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：**onedir（默认交付形态）**。

为什么默认 onedir 而不是 onefile（2026-11-07 实测依据，见 _packaging_license.md）：
  · 冷启动：onefile 每次启动都要把 ~150MB 解包到 %TEMP%\\_MEIxxxx，实测 5.63s；
    onedir 直接 mmap 磁盘文件，实测见报告（排序后 2s 量级）。
  · 杀软：onefile「自解压 + 释放可执行片段 + 注入」是启发式高危组合；
    onedir 没有运行期解包，误报面小很多。
  · 更新：onedir 可以只替换变动的 dll/pyz（Inno Setup 差量覆盖）；
    onefile 运行中不能覆盖自身，只能改名分身再重启。
  · 残留：onefile 崩溃会留下 %TEMP%\\_MEIxxxx（几十~上百 MB，没人清）；
    onedir 卸载时删目录即可。

体积裁剪（本 spec 做了三件实事）：
  1. rapidocr 包**自带**一份同样的 PP-OCRv6 模型（det 9.5MB + rec 20.3MB），
     和 core/assets/rapid 完全重复 —— 这里过滤掉 .onnx，只保留 yaml；
     `_MEIPASS/core/assets/rapid` 那份才是运行时真正加载的。
  2. cv2 的 `opencv_videoio_ffmpeg*.dll`（约 29MB）只服务 VideoCapture/VideoWriter，
     本项目只用 matchTemplate/cvtColor，直接剔除。
  3. onnxruntime 的 quantization/tools/transformers/training 是训练侧模块，
     运行时用不到，排除（capi 里的 dll 保留）。

跑法：uv run pyinstaller sjz.spec --noconfirm
  产物：dist\\SJZ-Control\\SJZ-Control.exe（整个 dist\\SJZ-Control 目录一起交付）
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())

# 版本资源由 tools/version_sync.py 从 core/version.py 生成（单点）。
_VER_INFO = os.path.join(ROOT, "build", "version_info.txt")


def _require_ver_info():
    """生成前置缺失就硬失败：静默产出无版本号的 exe 会让更新比对失效。"""
    if not os.path.exists(_VER_INFO):
        raise SystemExit(
            "build/version_info.txt 缺失，EXE 会丢失文件版本/产品版本。\n"
            "先跑：uv run python tools/version_sync.py --write"
            "（build.bat 的 [2/7] 已内置这一步）"
        )
    return _VER_INFO

# 随包发布的运行时脚本（被 subprocess / importlib 按路径加载，静态分析看不见）：
#   fire_worker.py  core/fire_proc.py 起的独立自旋进程，缺了抢购降级
#   calibrate.py    desktop.py --calibrate 用 importlib 加载
#   OCR_*.yaml      core/ocr_service.py 的 paddlex 流水线配置
_RUNTIME_TOOLS = [
    "fire_worker.py",
    "calibrate.py",
    "OCR_game.yaml",
    "OCR_tiny.yaml",
]

datas = [
    # 前端页面：server.py 用 __file__ 推出 ROOT/web，冻结后落在 _MEIPASS
    (os.path.join(ROOT, "web"), "web"),
    # 砖皮知识库：core/bricks.py 用 __file__ 找同目录的 bricks.json
    (os.path.join(ROOT, "core", "bricks.json"), "core"),
    # 目标库（自查库 252 条）：core/bricks.py 的 load_skin_lib() 用 __file__
    # 找同目录的 skins_lib.json；漏收 → server._skins() 读不到，前端自查库为空
    (os.path.join(ROOT, "core", "skins_lib.json"), "core"),
    # 筛选表：core/skinfilter.py 用 __file__ 找同目录的 skins_raw.csv
    (os.path.join(ROOT, "core", "skins_raw.csv"), "core"),
    # 字形模板 + OCR 模型 + interception.dll（core/assets 整目录）
    (os.path.join(ROOT, "core", "assets"), os.path.join("core", "assets")),
    # 显式再声明一次 DLL：有人把它挪出 assets 时也能找到
    (os.path.join(ROOT, "core", "assets", "interception.dll"),
     os.path.join("core", "assets")),
    # 默认模板集（buy/confirm + meta.json）。desktop._seed_missing_assets()
    # 首次运行时会把它们从 _internal 释放到 exe 同级目录，否则交付包里
    # templates/ 是空的、装上不能直接用（审查 R09-05）。
    (os.path.join(ROOT, "templates"), "templates"),
    # 进化框架：`<runtime> -m evo.cli` 运行，源码 + 自带 json 配置
    # （runs/fixtures/state 是本机运行产物，不进交付包）
    (os.path.join(ROOT, "evo", "evo_config.json"), "evo"),
    # Interception 官方驱动安装器：安装器的可选任务 driver 直接调用
    (os.path.join(ROOT, "tools", "interception"), os.path.join("tools", "interception")),
]

# 运行时脚本逐个收（不收 tools/ 整目录：里面有几个 MB 的 bench 数据和开发脚本）
for _name in _RUNTIME_TOOLS:
    datas.append((os.path.join(ROOT, "tools", _name), "tools"))

# evo 源码 + 自身 json 配置（跳过 runs/fixtures/state 这些本机产物）
# collect/ 是采集与 bench 产物（events*.jsonl），不是运行时依赖，不能发给用户
_EVO_SKIP = {"runs", "fixtures", "state", ".state", "collect", "__pycache__"}


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

# rapidocr 的 yaml 要，模型不要（模型在 core/assets/rapid，避免重复 30MB）
_rapid = collect_data_files("rapidocr")
datas += [d for d in _rapid if not d[0].lower().endswith(".onnx")]

HIDDEN = [
    "mss.windows",
    "clr_loader",
    "pythonnet",
    "onnxruntime",
    # 动态/延迟 import 的核心模块，显式钉住（新增模块时同步补这一行）
    "core.humanmouse",
    "core.idle_roam",
    "core.repeat",
    "core.skip_late",
    "core.stop_grade",
    "core.click_burst",
    "core.money",
    "core.watchdog",
    "core.retention",
    "core.recovery",
    "core.scene_guard",
    "core.timesync",
    "core.ocr_rapid",
    "core.ocr_chain",
    "core.fire_proc",
    "core.version",
    "core.tier_enum",
]
HIDDEN += collect_submodules("rapidocr")
for _m in collect_submodules("webview"):
    if ".platforms." in _m and not _m.endswith(("edgechromium", "winforms")):
        continue
    HIDDEN.append(_m)

EXCLUDES = [
    "tkinter", "matplotlib", "pytest", "PyInstaller",
    "pandas", "scipy", "IPython", "notebook",
    # 训练/量化侧模块，运行时用不到
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

# 只保留 CPU 推理需要的 onnxruntime 二进制；顺手删掉 cv2 的 ffmpeg 大件
_DROP = ("opencv_videoio_ffmpeg",)
a.binaries = [b for b in a.binaries if not any(x in b[0].lower() for x in _DROP)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SJZ-Control",
    version=_require_ver_info(),
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SJZ-Control",
    contents_directory="_internal",
)
