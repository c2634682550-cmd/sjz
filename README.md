# SJZ 抢购控制台

交易行自动抢购：模板匹配 + OCR + 倒计时/列表两条路线。界面是单文件
`web/index.html`，后端是同目录 `core/`，默认 `http://127.0.0.1:8777`。

**只有实战一种模式**：没有演练/模拟开关，启动即在游戏里真实点击。
不确定后果时请先在设置页用「半自动」（`run.semi`，只走到购买按钮前停手）。

## 快速开始

```powershell
# 有 .venv 时直接双击 start.bat；下面是手动方式
uv sync                 # 或 python -m pip install -e .
.venv\Scripts\python.exe run.py        # 浏览器模式（开发用，改完刷新即可）
.venv\Scripts\pythonw.exe desktop.py   # 桌面模式（最终用户）
```

双击 `start.bat` 会走 `desktop.py`：优先 pywebview（EdgeChromium）内嵌窗口，
没有 pywebview / WebView2 时退回本机 Edge/Chrome 的 `--app` 独立窗口
（无地址栏、无标签页）；两者都找不到会弹错误并退出，不会留下占端口的后台进程。
日志固定写 `desktop.log`。

## 换一台机器必须准备什么

| 依赖 | 是否入库 | 缺了会怎样 | 怎么补 |
|---|---|---|---|
| Python ≥ 3.10 + `uv`（或 pip） | — | 起不来 | 装 Python 3.11+，或 `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| `mss` / `opencv-python` / `numpy` / `pywebview` | 在 `pyproject.toml` + `uv.lock` | `uv sync` 会装 | `uv sync` |
| Edge 或 Chrome | 系统自带（Edge 通常有） | 没有 pywebview 时无窗口可开 | 装 Edge/Chrome |
| `core/bricks.json` | ✅ 入库 | 皮肤目录为空（界面还显示 11 个赛季，是假象） | 重新采集 |
| `core/skins_raw.csv` | ✅ 入库 | `/api/skins/plan` 直接 503 | 重新导出 |
| `templates/`（`buy.png` / `confirm.png`） | ❌ 不入库（游戏截图，按机器/分辨率采集，二进制不适合入库） | 启动闸门「模板」项不过，任务加不上 | 设置页重新框选并截图（`说明.txt`「模板」一节） |
| `.venv-vl`（PaddleX PP-OCRv6 服务） | ❌ 不入库（2GB+ 依赖，且首次要联网下模型） | 启动闸门「OCR 识别」不过并给出起服务的命令 | 见 `说明.txt` 第九节：`py -3.11 -m venv .venv-vl` → 装 `paddlepaddle-gpu` / `paddleocr[doc-parser]` → `paddlex --install serving` → `paddlex --serve --pipeline tools\OCR_game.yaml` |
| Interception 驱动 | 随 `tools/interception/` 提供安装器 | 不拦启动，退回旧输入通道（慢且易被拦） | 管理员运行 `install-interception.exe install` 后重启 |

启动时 `core/server.py::_startup_checks()` 会把这四项（bricks / skins_raw /
templates / .venv-vl）逐条打印并写进调度器日志；启动前检查（preflight）
再把缺的项拦下来并说明「哪一步缺什么」，不会只回一句「不可用」。

## 分辨率

所有坐标按 `1920×1080` 采集。判定不是写死这个数字，而是按比例比对：

```json
"screen": {"base_size": [1920, 1080], "tolerance": 0.0}
```

`tolerance` 是允许的相对偏差（0.02 = 2%）。换成 2560×1440 这类等比分辨率时
把 `base_size` 改掉即可；非等比分辨率即使放宽容差也会失真，请用等比分辨率。

## 测试

```powershell
python -m pytest tests -q
python -m compileall -q core web desktop.py run.py tools
```

测试全部离线：用 `tests/fakes.py` 里的假 `InputSink`/`Screen`/`Mouse`/`Keyboard`，
不启 GUI、不注入真实键鼠，断言 `mouse.total_calls == 0` 之类。

## 目录

```
core/       后端（config / engine / scheduler / server / nav* / ocr / preflight）
web/        index.html 单文件界面
tests/      离线测试
tools/      OCR 产线配置、Interception 安装器、探针脚本
templates/ 运行时采集的模板（不入库）
data/       成交记录与截图（不入库）
```

更多操作细节（框选、热键、记录、OCR 调优）见 `说明.txt`。
