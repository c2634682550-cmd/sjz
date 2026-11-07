# 构建前置：版本生成物（build/ 可清理版）

结论：`build/version_info.txt` 是**生成物**，不是源码；`build/` 被 `.gitignore` 忽略。
因此「清掉 build/ → `test_release_pipeline` 变红」属于**前置顺序缺失**，已固化为不可跳过的一步。

## 1. 生成物清单与依赖

真源只有一个：`core/version.py` 的 `APP_VERSION`（当前 `1.1.0`），由 `tools/version_sync.py`
用 AST 读取，不 import 任何重依赖。

| 生成物（相对仓库根） | 消费者 | 内容 |
|---|---|---|
| `build/version_info.txt` | `sjz.spec` / `sjz-onefile.spec` 的 `EXE(version=...)` | PyInstaller `VSVersionInfo`：filevers/prodvers + FileVersion/ProductVersion |
| `launcher/Version.g.cs` | C# 启动器更新比对 | `AppVersion.Current` 常量 |
| `installer/version.iss` | `installer/sjz.iss` | `#define MyAppVersion` |
| `pyproject.toml` | 依赖/元数据 | `[project] version`（仅正则替换该行，其它内容不动） |

`--check` 额外只读校验（**不生成**）：`web/index.html` 无硬编码版本号、关于页走
`S.config.version`、`core/config.py public()` 注入 `version`。

输入依赖：仅 `core/version.py`。生成器本身不依赖 `build/` 是否已存在（`write_all` 会
`os.makedirs`），也不依赖 PyInstaller。

## 2. 幂等性（实测）

连续执行两次 `uv run python tools/version_sync.py --write`：

- 四个文件的 **sha256 全部不变**；
- 改前 `build/version_info.txt` 等三个文件每次都被无条件重写，**内容幂等但 mtime 每次都变**；
  已改为「内容不变就不落盘」（`_write_if_changed`），复测 `mtime_stable=True`；
- `pyproject.toml` 本来就走「有变化才写」分支，连 mtime 都不变。

结论：`--write` 可安全重复执行，可放进任何前置步骤，不会造成无谓的增量重建。

## 3. 顺序约束

- `build.bat`：`[2/7] version_sync --write` 已在 `[5/7] pytest tests/` 之前；并在其后再加
  `if not exist "build\version_info.txt" goto nover`，生成器“假成功”也会被拦住。
- CI：仓库内**没有任何 CI 配置**（无 `.github/`、`.gitlab-ci.yml`、`Jenkinsfile`、
  `azure-pipelines.yml` 等），因此没有需要同步修改的 CI 文件。
- `sjz.spec`：`version=_require_ver_info()`，缺文件时**硬失败**并打印修复命令，
  不再静默产出无文件版本/产品版本的 exe（原实现是 `if os.path.exists(...) else None`）。

## 4. 自检与失败信息

`tests/test_release_pipeline.py::_assert_version_artifacts()` 在版本一致性断言前先做闸门，
失败文案（实测输出）：

```
版本派生文件缺失或与真源 core/version.py 不一致：
  - build/version_info.txt：缺失，先跑 --write
先跑生成前置：uv run python tools\version_sync.py --write
（build.bat 的 [2/7] 已内置这一步；build/ 被 .gitignore 忽略，清掉 build/ 后必须先重新生成再跑测试）
```

不再出现 `[{'name': ..., 'ok': False, 'detail': ...}]` 这种看不懂的 dict dump。

## 5. 测试能否自己生成（第 3 项评估）

**部分能，且已做；完全自动生成仓库内的 build/ 不采纳。**

- 已做：`test_version_generator_runs_without_build_dir(tmp_path)` 在**临时目录**完整渲染
  三份派生文件，验证「生成器不依赖 build/ 存在」「两次渲染逐字节幂等」「仓库现有生成物与
  生成器输出逐字节一致（防手改漂移）」。为此 `version_sync.py` 抽出 `render_all(v)`，
  `--write` 与测试共用同一份 `DERIVED` 清单。
- 不采纳：让测试在检测到 `build/version_info.txt` 缺失时**自动写回仓库**。理由：
  1. 与验证②（前置缺失时必须给出可读失败）直接冲突，会把顺序缺陷重新掩盖成绿色；
  2. PyInstaller 打包时仍要读这个文件，测试替它“兜底”会让打包前置继续依赖人记；
  3. 测试静默改工作树会污染 diff、干扰并行会话。

## 6. 三种验证结果

| 场景 | 命令 | 结果 |
|---|---|---|
| ① 删除 `build/` 后按正确顺序跑 | `rm -r build` → `python tools/version_sync.py --write` → `pytest tests/test_release_pipeline.py -q` | **9 passed**（25.63s，3 warnings） |
| ② 删除 `build/` 直接跑测试 | `rm -r build` → `pytest tests/test_release_pipeline.py -q` | **按预期失败**：`test_version_single_source` 输出第 4 节的完整中文修复指引（另 7 passed） |
| ③ 正常顺序跑全量 | `python tools/version_sync.py --write` → `pytest tests/ -q` | **2332 passed, 20 skipped, 2 xfailed, 0 failed**（840.87s / 14:00，274 warnings；无新增红） |

环境读数（Windows 本机，`Get-Process python` + `Win32_Processor.LoadPercentage`）：

- ② 执行前：`python_procs=20`，`cpu_load=72%`
- 全量执行前：`python_procs=20`，`cpu_load=74%`；执行后：`python_procs=12`，`cpu_load=63%`
- 全量期间同机还有其它会话的 3 份 `pytest tests/` 在并发跑，heavy 锁共 604 次获取、
  24 次等待 ≥100ms、0 次超时（max 43.2s），属并发噪声，非本次改动引入；0 failed 说明门槛未被干扰打红。

## 7. 改动文件

| 文件 | 改动 |
|---|---|
| `tools/version_sync.py` | 新增 `DERIVED` / `render_all(v)`；`write_all` 改走 `_write_if_changed`（mtime 幂等） |
| `tests/test_release_pipeline.py` | 新增 `_assert_version_artifacts()` 可读闸门；新增临时目录可复现测试；`test_exe_version_resource_declared` 兼容 `_require_ver_info()` |
| `build.bat` | `[2/7]` 后加 `build\version_info.txt` 存在性硬校验 |
| `sjz.spec` | `version=_require_ver_info()`，缺失即 `SystemExit` 并打印修复命令 |

未改动（其它会话在改）：`core/`、`web/`、`tests/conftest.py`、`tools/soak.py`、
`tests/test_frontend_switch_rollback.py`、`tests/test_poll_path.py`。
另：`sjz-onefile.spec` 仍是旧写法（`... else None`），不在本次文件边界内，建议后续同步。
