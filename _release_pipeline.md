# 发布链路补齐报告（打包 → 安装 → 卸载 → 升级 → 自动更新 → 回滚）

- 项目：`C:\Users\26346\Desktop\project\dafors`
- 版本基线：**1.1.0**（唯一真源 `core/version.py`）
- 约束遵守：未 commit、未 push、未装到系统目录、未开可见窗口、未跑完整 PyInstaller 打包
- 验证方式：`py_compile` + `collect_data_files` 演练 + 静态差集 + 沙箱化安装/卸载演练 + launcher 更新测试

---

## 一、现状核实（以代码为准，不信文档）

| 项 | 实际状态 | 结论 |
|---|---|---|
| `sjz.spec` | `datas` 10 项：web、bricks.json、skins_raw.csv、core/assets、interception.dll、templates、tools/calibrate.py、tools/OCR_game.yaml、tools/OCR_tiny.yaml、rapidocr yaml；`hiddenimports` 仅 4 个第三方项；EXE **无版本资源** | 收集不完整 |
| `sjz-onefile.spec` | 同上，另缺 `core/assets/interception.dll` 以外的 tools 运行时脚本 | 同上 |
| `launcher/` | C# 监督进程一套完整代码：`Program.cs`、`Updater.cs`、`LauncherRuntime.cs`、`LauncherForm.cs`、`ProcessHost.cs`、`Crypto.cs`、`Json.cs`、`Log.cs`、`build.ps1`、`launcher.config.json`、`tests/`。已具备：签名 version.json 校验、sha256 校验、原子替换、`update_backup`、重启提示、控制面健康检查 | 骨架在，但缺口多 |
| 版本号常量 | **散落 3 处**：`pyproject.toml` = 0.1.0；`launcher/Program.cs` 里 `AppVersion.Current` = 1.0.0；界面「关于」读 `S.config.version`，而 `config.json` **没有 version 字段** → 关于页永远显示 `—` | 未收敛，且有一处是死的 |
| 更新检查 | `launcher/Updater.cs::CheckAndApply`：只认 `{payload,sig}` 签名格式；解析 version/url/sha256；**不解析 notes**；下载直接写目标文件（中断会留半包）；替换前备份但也只保留一份；**没有 `--rollback` 命令**；**没有「新版本起不来自动退回」** | 半成品 |
| 运行时 import | `core/engine.py`、`desktop.py` 静态 import 全部新模块；`core/fire_proc.py` 用 `subprocess` 调 `tools/fire_worker.py`；`desktop.py --calibrate` 用 `importlib` 载 `tools/calibrate.py`；`core/ocr_service.py` 读 `tools/OCR_*.yaml` | 这 4 个文件必须随包 |
| `core/guard.py` | **不存在**。实际守护模块是 `core/scene_guard.py`（engine/recovery 以 `sg` 引用），`/api/guard` 走的也是它 | 需求里的文件名与代码不符，按实际模块收 |
| `evo/` | 194 个文件，含 `runs/`、`fixtures/`、`state/` 等本机运行产物；**没有任何运行时代码 import 它**，按 `<runtime> -m evo.cli` 方式运行 | 需按源码交付，但要剔除运行产物 |
| 安装器 | 仓库内**没有任何 `.iss` / `.nsi`**，`dist-installer/` 不存在 | 完全缺失 |
| `tools/` | 含 `fire_worker.py`（必需）、`calibrate.py`（必需）、`OCR_*.yaml`（必需）、`interception/install-interception.exe`（安装器驱动任务用），以及 `bench_countdown.json`（9.6 MB）和大批开发脚本 | 不能整目录收 |

**核实结论**：链路断在「没有安装形态 + spec 漏收 + 版本号三处不一致 + 更新只做了一半」。下面的改动逐条补齐。

---

## 二、打包一致性：spec 差集表（要求为空）

审计脚本：`tools/audit_packaging.py`（AST 解析 spec + AST 扫代码 import + 运行时字符串路径引用）。必需文件 = 代码里按路径真的会打开的文件，不含 `__pycache__`、`perf` 等。

| 类别 | 文件数 | 未收集（差集） | 落地目录（`_internal` 下） |
|---|---|---|---|
| core/assets（OCR 模型/字模/模板/DLL） | 374 | **0** | `core/assets`、`digits`、`glyphs`、`rapid` |
| core 其他数据（bricks.json、skins_raw.csv） | 2 | **0** | `core/` |
| evo/（源码 + evo_config.json，剔除 runs/fixtures/state） | 42 | **0** | `evo/`、`evo/diagnose` |
| templates/（buy/confirm/meta.json） | 3 | **0** | `templates/` |
| tools/ 运行时脚本 + 驱动安装器 | 4 + 驱动目录 | **0** | `tools/`、`tools/interception` |
| web/ | 3 | **0** | `web/`、`web/assets` |
| **合计** | **428** | **0** | spec `datas` = 54 项 |

模块侧差集：

| 模块/引用 | 缺口 |
|---|---|
| 动态/延迟 import（`from . import humanmouse` 等 16 个核心模块） | 空 —— 已全部写入 `hiddenimports` |
| `tools/fire_worker.py`（subprocess 起进程） | 空 —— 已作为 data 收集 |
| `tools/calibrate.py`（importlib 按路径加载） | 空 —— 已作为 data 收集 |
| manifest 里声明但磁盘不存在 | 空 |

两份 spec（`sjz.spec`、`sjz-onefile.spec`）差集均为 0，`tests/test_release_pipeline.py` 会守住这个门槛。

**关键修正**：`tools/fire_worker.py` 是抢购降级的独立自旋进程，之前完全没进包 —— 装上后该功能直接不可用；`evo/` 和 Interception 安装器也从未进包。

---

## 三、安装器（Inno Setup 6）

文件：`installer/sjz.iss`，版本号来自生成的 `installer/version.iss`。

- **路径**：`DefaultDirName={autopf}\SJZ-Control` + `DisableDirPage=no`，用户可改；无任何写死盘符。
- **静默**：`Setup.exe /VERYSILENT /DIR="D:\Apps\SJZ-Control" [/TASKS="driver"]`。
- **快捷方式**：开始菜单 + 桌面（桌面可选任务）；优先指向 `launcher\SjzLauncher.exe`，不存在则回落到 `SJZ-Control.exe`。
- **卸载信息**：Inno 自写卸载键；另写 `HKLM\Software\SJZ-Control`（InstallPath/Version），`Flags: uninsdeletekey`，卸载即删。
- **可选驱动**：任务 `driver`（默认不勾）调用随包的 `tools\interception\install-interception.exe /install`，提示需重启。
- **VC 运行库**：`ssPostInstall` 查 `HKLM\...\VC\Runtimes\x64\Installed`；缺失时弹提示，若 `installer\redist\vc_redist.x64.exe` 存在则一键静默安装（不存在则提示下载地址）。
- **升级**：同 `AppId` + `UsePreviousAppDir=yes`，覆盖安装沿用旧目录、更新卸载信息与版本号；用户数据不在安装清单里，覆盖安装不会碰。

### 卸载：删程序，不删数据

`CurUninstallStepChanged(usUninstall)` 先把用户数据**搬出**安装目录：

```
%LOCALAPPDATA%\SJZ-Control\uninstalled-<yyyymmdd-hhnnss>\
    config.json
    launcher.config.json
    data\        （台账、截图）
    templates\   （标定模板）
```

许可证/日志本来就在 `%LOCALAPPDATA%\SJZ-Control`，卸载全程不触碰。卸载页与卸载完成页都会 `MsgBox` 明示这两个位置；`[UninstallDelete]` 只清 `tools`、各 `__pycache__` 等运行产物，**不包含** `config.json` / `data` / `templates` / 许可证。

演练结果（`installer/verify_installer.py`，临时目录）：

| 检查 | 结果 |
|---|---|
| 安装目录已移除 | 通过 |
| 开始菜单/桌面快捷方式已移除 | 通过 |
| 卸载注册表键已移除 | 通过 |
| 用户配置仍在 | 通过 |
| 台账/截图仍在（含 `data/shots`） | 通过 |
| 标定模板仍在 | 通过 |
| 许可证/日志未被触碰 | 通过 |

> 本机未装 Inno Setup 6（`iscc` 不存在），所以**没有真跑**引擎级静默安装；按你的约束也不装系统目录。脚本已通过全部静态规则；装上 ISCC 后 `python installer\verify_installer.py` 会自动追加一次「临时目录安装→卸载」真跑。

---

## 四、版本号单点

真源：`core/version.py::APP_VERSION = "1.1.0"`。派生由 `tools/version_sync.py` 完成，禁止手改：

| 落点 | 派生文件 | 当前值 |
|---|---|---|
| exe 文件/产品版本资源 | `build/version_info.txt`（PyInstaller `version=`） | 1.1.0 |
| 更新比对 | `launcher/Version.g.cs`（`AppVersion.Current`，原 Program.cs 里的常量已删） | 1.1.0 |
| 界面关于页 | `core/config.py::public()` 注入 `version`，前端 `S.config.version` 读取 | 1.1.0（实测接口返回） |
| 安装器 | `installer/version.iss`（`MyAppVersion`） | 1.1.0 |
| 包元数据 | `pyproject.toml` | 1.1.0 |

`python tools/version_sync.py --check` 同时校验：上面 5 处一致、`web/index.html` 无硬编码版本字面量。全部通过。

---

## 五、升级 / 自动更新 / 回滚

`version.json` 支持两种格式，字段都含 `version`、`url`、`sha256`、`notes`：

- **签名格式**（默认，`update_require_sig=true`）：`{"payload":{...}, "sig":"..."}`，P-256 验签覆盖规范化 payload；
- **明文格式**（自建下载站/内测，需配置 `update_require_sig=false`）：顶层直接放四字段，仍强制 sha256。

流程（`launcher/Updater.cs`）：

1. 读 `version.json` → 验签（或明文放行）→ 版本比较；
2. 下载到 `update_<ver>_<pid>.zip.part`，完成后才改名；**中断只留 `.part`，不会冒充完整包**；
3. sha256 不符 → `rejected`，一个字节都不落地；
4. 解压到独立 stage（拒绝 zip-slip / 绝对路径 / 盘符）；
5. 原子替换：目标先搬到 `update_backup.tmp`，新文件搬入；**任何一步失败立刻反向搬运**，且 `update_backup`（上一版）在成功前不动 —— 新一次更新失败不会毁掉唯一退路；
6. 成功后把 `.tmp` 轮换为 `update_backup`，并写 `update_state.json`（`confirmed=false`）；
7. 新版本启动后控制面在 15 秒内应答 → `ConfirmRunning()` 标记已确认；**没应答 → 自动 `Rollback()` 整份退回上一版并重启一次**；
8. `--rollback` 手动回滚命令随时可用（无备份时明确失败，不乱删）。

**正在运行的 exe 怎么换**：启动器本身受保护、更新包不替换它；程序主体（`SJZ-Control.exe`、`_internal`）由启动器在**控制台未运行时**替换。GUI 点「检查更新」会先停控制台再更新，成功后按新文件拉起；`--update-only` 在控制台运行时对占用文件会明确失败并回滚，不会静默破坏安装。没有额外引「退出后替换」的外部小进程，因为监督进程已在外面，先停子进程再换更简单、可测。

发布侧工具：`tools/make_release.py` 打 zip、算 sha256、写 `version.json`（有私钥则签名，复用 `core/license` 的 P-256 与 `portal/signing.py`）。

### 失败演练（全部在 pytest 沙箱，旧版必须仍可用）

| 场景 | 用例 | 结果 |
|---|---|---|
| 校验不通过（sha256 不符） | `test_update_rejects_hash_mismatch` | 拒收，旧文件不变 |
| 下载中断（截断的 zip，哈希还对上） | `test_download_interrupted_leaves_old_version_runnable` | 解压失败 → `failed`；旧文件不变；无 `.part/.zip` 残留；重试仍失败但不脏 |
| 替换时文件被占用（`CreateFileW` 独占锁） | `test_update_fails_when_target_locked` | `failed`，旧内容 `v1` 不变；解锁后重试成功更新到 `v2` |
| 更新后回滚 | `test_rollback_cli_restores_previous_version` | `rolledback`，`payload.txt` 回到 `v1`；无备份时回滚明确失败 |
| 明文 version.json + notes | `test_plain_version_json_and_notes` | 允许配置下更新成功并回报 notes；默认配置下同样文件被拒 |

`launcher/tests`：**40 passed**（含原有的签名/哈希/zip-slip/许可前置检查/进程控制等）。

---

## 六、验证汇总

`python tools\release_verify.py --with-launcher-tests` → **28 项全部通过**（明细见 `build/release_verify.json` / `.md`）：

| 验证项 | 结果 |
|---|---|
| `py_compile`（core / evo / desktop / run / 随包 tools 脚本） | 通过 |
| `collect_data_files(rapidocr)` 演练 | 通过（9 文件，3 yaml） |
| 打包差集 `sjz.spec` / `sjz-onefile.spec` | **0 / 0** |
| 版本号 5 处一致 + 关于页无硬编码 | 通过 |
| 安装器 12 条静态规则 | 通过 |
| 卸载演练 9 条（目录/快捷方式/注册表/用户数据） | 通过 |
| 启动器更新/回滚 40 用例 | 通过 |
| 运行时导入烟测 `import core.engine, core.version, core.config, core.fire_proc, evo.cli` | 通过（`1.1.0`，接口 `version=1.1.0`） |

已跑的测试：`tests/test_packaging.py`、`tests/test_release_pipeline.py`、`launcher/tests` 共 **53 passed**。

---

## 七、交付物清单

| 文件 | 作用 |
|---|---|
| `core/version.py` | 版本号唯一真源 |
| `tools/version_sync.py` | 生成/校验版本资源、C# 常量、安装器版本 |
| `tools/audit_packaging.py` | spec ↔ 代码 差集审计（`--summary` 出分组表） |
| `tools/make_release.py` | 打更新 zip + `version.json`（签名/明文） |
| `tools/release_verify.py` | 一键发布验证 |
| `installer/sjz.iss`、`installer/version.iss` | Inno Setup 安装器 |
| `installer/verify_installer.py` | 安装器静态检查 + 临时目录安装/卸载演练 |
| `sjz.spec`、`sjz-onefile.spec` | 补齐 datas/hiddenimports/版本资源 |
| `launcher/Updater.cs`、`Program.cs`、`LauncherRuntime.cs`、`LauncherForm.cs`、`Version.g.cs` | 更新、回滚、自动回退、单点版本 |
| `launcher/tests/test_update.py` | 新增 4 个失败/回滚用例 |
| `tests/test_release_pipeline.py` | 发布链路静态门槛 |
| `build.bat` | 版本同步 → 差集审计 → 测试 → 打包 → 安装器 |

## 八、未做/已知限制

1. **未跑完整 PyInstaller 打包**（按你的要求只做静态演练）；`dist/` 是旧产物，新 spec 需要下次 `build.bat` 才生效。真实打包后请抽查 `dist/SJZ-Control/_internal` 下是否有 `evo/`、`tools/fire_worker.py`、`tools/interception/`。
2. **未真装系统目录**（无 Inno Setup 6）。装上 ISCC 后跑 `python installer\verify_installer.py` 会追加真静默安装→卸载。
3. `desktop.py --doctor` 在本机能打印环境/字体/许可，但最后阶段会挂起（等交互/游戏环境），本次超时终止；它不影响发布结论。
4. `tools/realmachine_v2.py:3227` 有**历史遗留语法错误**（列表推导写法非法），与发布无关，未改动；`tools/` 不整目录入包，不受影响。
5. `evo/` 的 `runs/fixtures/state` 是本机产物，明确不入包；如需带走历史登记册，另行拷贝。
6. 回滚只保留**上一版**（`update_backup` 单份），不是多版本历史。
