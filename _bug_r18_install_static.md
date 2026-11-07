# R18：安装 / 卸载 / 升级 / 更新机制静态审查

审查对象：`C:\Users\26346\Desktop\project\dafors`（纯静态推导，未安装、未改系统、未弹窗、未 commit / push）
审查日期：2026-11-07
审查范围：打包产物形态（`sjz.spec` / `sjz-onefile.spec` / `build.bat`）、两个启动入口（`desktop.py` / `run.py`）、路径与落盘策略（`core/config.py`、`core/records.py`、`core/license.py`、`core/selfcheck.py`、`core/ocr_service.py`、`core/single_instance.py`）、交付目录实物（`dist/`）、文档声明（`docs/build-desktop.md`、`docs/driver-install.md`、`说明.txt`）。

---

## 0. 事实基线：四套机制一个都不存在

这一条决定了后面所有问题的性质：**当前没有安装器、没有卸载器、没有升级器、没有更新器，也没有版本号。**

| 机制 | 现状 | 证据 |
|---|---|---|
| 安装 | 手工复制 `dist\SJZ-Control\` 整个目录 | `docs/build-desktop.md:3-6` 明说"交付时把整个目录交给安装器（Inno Setup）"——安装器尚不存在 |
| 卸载 | 手工删目录 | 全仓不存在 `.iss` / `.nsi` / `.msi`（Glob `**/*.{iss,nsi,wxs,msi}` 零命中）；无 Add/Remove Programs 注册 |
| 升级 | 手工覆盖同名目录 | 同上，无版本目录、无 staging、无备份 |
| 自动更新 | 完全没有 | `core/server.py` 路由表无 update 相关端点；全仓无下载 / 校验代码；唯一网络客户端是 `core/license.py` 的 urllib |
| 版本号 | 不存在 | `pyproject.toml:3` 的 `0.1.0` 无人读取；前端版本槽 `web/index.html:1471` 固定渲染 `v—`（`3007` 行读 `S.config.version`，而 `config.json` 无该键） |

因此"装到 `Program Files` 会怎样""卸载会不会误删数据""降级能不能读旧配置"这些问题，答案不是"某段代码写错了"，而是**这些分支压根没有被实现**，只能靠现有路径策略去推导后果。下面每条都按"现有代码在某个条件成立时走到哪一步"来写。

---

## 1. 【致命】装进 `Program Files`：首次启动在 import 阶段就死，且不留任何痕迹

**触发条件**：安装目录不可写（`Program Files` 默认 ACL：`Users` 只有读 + 执行）。首次运行，`templates/`、`data/`、`config.json` 都还不存在。

**推理路径**：
1. `desktop.py:32` 在**模块顶层**执行 `from core.server import serve`；
2. `core/server.py:45` 执行 `_scheduler = Scheduler()`；
3. `core/scheduler.py:124` 构造时立刻 `Config.load()`；
4. `core/config.py:481-484` 判定 `config.json` 不存在 → `cfg = cls()` → `cfg._save_locked()`；
5. `Config.__init__`（`core/config.py:258-259`）先 `os.makedirs(TEMPLATE_DIR/DATA_DIR, exist_ok=True)` → `Program Files` 下抛 `PermissionError`；
6. 若安装器事先建好了这两个目录，则走到 `_save_locked`：临时文件 `open(tmp, "w")` 抛 `OSError` → 兜底分支 `open(CONFIG_PATH, "w")` 再次抛 `OSError`（`core/config.py:411-419`），异常穿透 `_load_locked` → `Config.load()` → `Scheduler.__init__` → `core.server` 导入失败；
7. `desktop.py` 的 `if __name__ == "__main__"`（`:419-432`）**还没轮到执行**，模块级异常直接终止进程。windowed exe 没有控制台，`desktop.log` 也写不进 `Program Files`（`:38`、`:44-47` 的 `except OSError: pass`）。

**用户实际会遇到什么**：双击图标，屏幕闪一下就没了；没有窗口、没有报错框、没有 `desktop.log`、事件查看器里也只是一条无参数的应用程序错误。用户唯一能做的判断是"这软件坏了 / 装错了"。连官方诊断手段 `--doctor` 也同样死掉——`desktop.py:210` 的 `from core import config, server` 会重复同一条路径。

**附带影响**：这条路径把"安装到 `Program Files`"这一最常见的企业部署方式直接判死，而不是降级运行。

---

## 2. 【高】即使首次能起来（目录预置可写 / 曾管理员跑过），运行期写入全部静默失效

**触发条件**：`config.json` 已存在且可读，但目录对当前用户不可写（典型：安装器以管理员生成配置后交付；或用户曾提权跑过一次）。

**推理路径**：
- 日志：`desktop.py:38-47`，`LOG_PATH` 在 exe 同级，`open` 失败被 `except OSError: pass` 吞掉——**所有启动诊断、端口选择、提权、开窗失败记录全部消失**；
- 配置：`core/config.py:366-419`，`os.replace` 连续 6 次 `PermissionError` 后走兜底直写，最终抛 `OSError`；`transaction()`（`:432-454`）与各 API（`core/server.py:215/259/643/726/742/780/848/897/967/987/1008`）都会失败；
- 台账：`core/records.py:81-98`，`add()` 内 `except OSError: return None`——抢到了也不记账；
- 截图：引擎写 `DATA_DIR/shots`，同样失败（`records.py:24` 与 `config.py:32` 同一目录）；
- OCR 服务日志：`core/ocr_service.py:453-463`，`open(log_path, "ab")` 失败时 `f = None`，服务照样 `Popen`（输出直接丢弃），故障时**没有日志可查**。

**用户实际会遇到什么**：界面能开、按钮能点，但"设置改完自己变回去"（保存 500 / 无提示）、"抢到了但历史里没有"、"任务状态一直停在 running"、出问题时技术支持要日志而机器上什么日志都没有。这是比"打不开"更难排查的一类故障。

---

## 3. 【高】提权运行会把配置写进 `Program Files`，之后普通用户再也写不动

**触发条件**：游戏以管理员权限运行 → `core/input.py` 的 `needs_elevation()` 为真 → `desktop.py:246-281` 用 `elevate_self()` 以管理员重开。

**推理路径**：重开后的进程确实能写 `Program Files`（ACL 对 `Administrators` 放行），于是 `config.json` / `config.json.good` / `templates/` / `data/` 全部以管理员身份落盘，**文件 ACL 仍继承 `Program Files`（`Users` 只读）**。之后用户平时双击（不提权）启动，读得到、写不了。

**用户实际会遇到什么**：行为"一半正常一半坏"——管理员模式（游戏提权时）一切正常；平时双击时界面能用但所有保存/记录/截图静默失败。由于第 1 条，普通用户首次启动本来就进不来，所以这条通常表现为"装了以后必须右键以管理员运行"，而程序没有任何地方告诉用户这件事。

---

## 4. 【高】升级=覆盖目录，而交付目录里已经带着开发机的运行态配置

**触发条件**：按文档交付/升级（`docs/build-desktop.md:3-6`：把整个 `dist\SJZ-Control` 目录给用户；升级即同名覆盖）。

**推理路径**：
1. 实物证据：`dist\SJZ-Control\config.json`（2693 B）、`dist\SJZ-Control\config.json.good`、`dist\SJZ-Control\desktop.log` 全部存在；`dist\SJZ-Control\config.json` 是**运行 exe 时生成**的运行态文件，不是模板。
2. 覆盖安装 → 用户的 `config.json` 被替换成开发机/打包机快照 → `_merge`（`core/config.py:317-334`）+ `_normalize`（`:261-315`）后：`queue` 整份替换、`regions` 按名覆盖、`input_mode`/`threshold`/`console` 全部变回快照值。用户配置不会进隔离区（只有解析失败才 `_quarantine`），**是直接消失**。
3. 更糟的一步：`Scheduler.gc_task_dirs()`（`core/scheduler.py:149-160` → `core/config.py:580-598`）以"队列里没有的 8 位十六进制目录"为孤儿，`shutil.rmtree` 掉 `data/<taskid>/`。队列被换成开发机那条 `id: "deadbeef"`（仓库 `config.json:88-104` 实例，`deadbeef` 恰好匹配 `_TASK_ID_RE`），于是用户所有真实任务的模板目录在下次启动时被删。

**用户实际会遇到什么**：升级后"框选全没了"、"队列里冒出一条没名字的已完成任务"、"以前任务的标定图和成交截图目录被清空"。而这些数据不可重建：`regions` 是用户对着游戏界面手拖出来的归一化坐标（`core/config.py:36-46`），`templates/` 是同一屏截下来的按钮模板（`:573-578`）。

**根因**：程序文件与用户数据同目录（见第 7 条），打包产物没有"剥离运行态"步骤（`.gitignore:8-13` 忽略了这些文件，但 `build.bat` 不清理、`PyInstaller --noconfirm` 只清 `dist` 输出目录里的构建内容，不会清掉之外残留的 `config.json` / `desktop.log`）。

---

## 5. 【高】程序运行中执行覆盖升级 → 半更新状态（exe 旧、`_internal` 新）

**触发条件**：窗口还开着（或最小化到任务栏、或残留进程占着单实例闸）时执行复制/覆盖；Inno 默认的"关闭再复制"若没有正确等待进程退出，同样命中。

**推理路径**：Windows 对运行中映像加读锁；onedir 形态下 `SJZ-Control.exe`、`_internal\python311.dll`、`*.pyd`、`onnxruntime` 的 dll 都被 mmap。复制时这些文件必然 `拒绝访问`，其余文件替换成功 → 新旧混装。仓库内**没有**任何 staging（先解到临时目录再原子切换）、**没有**版本目录、**没有**文件完整性校验、**没有**回滚备份。

**用户实际会遇到什么**：升级"完成"后双击报 `Failed to execute script desktop` / `ImportError: DLL load failed` / 页面 404（`core/server.py:326-327` 的 `missing index.html`）；此时既跑不起来，也没有"退回上一版"的入口，只能整包重装——而重装如果又走了第 4 条，配置和试验数据一起丢。

---

## 6. 【高】"更新后自动重启"会被单实例闸挡掉，并以退出码 0 假装成功

**触发条件**：更新器（或用户点的"重启完成更新"）在旧进程尚未真正退出时启动新 exe。

**推理路径**：`desktop.py:303` `if not _single_instance(): return 0`；互斥量名 `Local\SJZ-Control-Console`（`core/single_instance.py:18`），同一会话内有效；`run.py:24-27` 同理返回 1。新进程不做任何事就退出，退出码为 0。

**用户实际会遇到什么**：更新器显示"更新成功，正在启动"，实际起来的是旧进程（或只是旧窗口被 `activate_window` 提到前面）；用户以为升级了，报 bug 时给的还是旧版本行为。若更新器靠"exit code == 0"判定成功，会把一次完全没生效的更新记为成功，后续不再重试。

---

## 7. 【高】卸载=删目录，而用户数据就放在程序目录里 → 一次清理就删掉台账、模板、截图

**触发条件**：任何"删除安装目录"的卸载方式（手工、或未来 Inno 的 `[UninstallDelete]` 整目录删除）。

**推理路径**：`core/config.py:30-32` 三个路径全部由 exe 同级推出：
- `config.json`（用户设置 + 队列 + 运行状态，`DEFAULTS` 见 `:34-175`）
- `templates/`（`buy.png` / `confirm.png` 标定模板）
- `data/`（`records.jsonl` 台账、`shots/` 成交截图、`<taskid>/` 任务模板）

`core/records.py:24`、`docs/build-desktop.md:98-106` 都明确写了这些是运行期写进 exe 同级。也就是说**程序资产和用户资产在同一个目录里，没有任何物理隔离**。

**用户实际会遇到什么**：卸载（或"重装前先清空目录"）之后，历史成交台账、成交截图、手工框选的模板、任务的条件全部永久消失，且没有任何提示。反过来也一样：想保留数据，就只能保留整个目录，于是"卸载不干净"。

**同时**：因为卸载器不存在，现在连"是否删数据"这个选项都没有——用户要么全删，要么全留。

---

## 8. 【中】卸载后必然残留：内核驱动 + `%LOCALAPPDATA%` + HKCU 注册表 + `%TEMP%`

**触发条件**：装过驱动、激活过许可 / 开过试用、跑过 onefile 版本。

**推理路径（逐项）**：

| 残留物 | 产生位置 | 现有清理逻辑 |
|---|---|---|
| 内核驱动服务 `HKLM\SYSTEM\CurrentControlSet\Services\interception` + `System32\drivers\interception.sys` | `install-interception.exe /install`（`docs/driver-install.md:19-25`；探测见 `core/selfcheck.py:86-98`） | **无**。卸载目录后 `install-interception.exe` 也随之消失，用户再也无法执行官方 `/uninstall` |
| `%LOCALAPPDATA%\SJZ-Control\{license,state,revocations,trial}.json` | `core/license.py:84-107` | 无（卸载器不存在） |
| `HKCU\Software\SJZ-Control\trial_start` | `core/license.py:680-695`（`CreateKey` + `SetValueEx`） | 无 |
| `%TEMP%\_MEIxxxxxx`（几十~上百 MB） | onefile 崩溃（`sjz.spec` 文档头注释、`docs/build-desktop.md:8-9` 自述） | 无 |
| `config.json.<pid>.<tid>.tmp`、`config.json.broken-*.bad` | `core/config.py:387`、`:545-548` | 无（只在写失败时删当次 tmp；`.bad` 永久累积） |
| `desktop.log`（无上限、无轮转） | `desktop.py:38` | 无（`core/retention.py` 只轮转 `records` / `shots` / `ocr_service_*.log`，不含它） |

**用户实际会遇到什么**：重装后"试用怎么又用过了"（`trial_start` 残留 + 文件双存储，`core/license.py:700-711` 取更早者，删文件也重置不了）；驱动仍在、每次开机加载内核服务，安全软件持续报"内核驱动"；磁盘里留着几份 `.bad` 和上百 MB 的 `_MEI`；售后让"先卸载重装"其实什么都没清掉。

---

## 9. 【中】多用户切换：配置是机器级共享、许可是用户级私有，两者对不齐

**触发条件**：同一台机器两个 Windows 账户先后 / 同时使用。

**推理路径**：
1. 单实例闸用 `Local\` 前缀（`core/single_instance.py:5-18`，注释明确"同一用户会话内唯一，不跨用户干扰"）→ **A、B 各自的会话可以同时各跑一个实例**；
2. `config.json` / `templates/` / `data/` 在安装目录（机器级共享）；`_save_lock`（`core/config.py:178-179`）只是**进程内 `RLock`**，跨进程完全无效 → 两个进程各自持有全量快照，`os.replace` 原子但"后写覆盖先写"，A 的修改被 B 静默抹掉；
3. `Scheduler.gc_task_dirs()`（`core/config.py:580-598`）以**当前进程读到的队列**为准删目录：B 启动时队列里没有 A 的任务 id → 删掉 A 的模板目录；
4. 许可相反：`license.json` / `state.json` / `trial.json` 在 `%LOCALAPPDATA%`（`:84-107`）、试用锚点在 `HKCU`（`:680-695`）→ **B 没有任何许可，各自开试用**；A 买过的授权在 B 名下不存在。

**用户实际会遇到什么**：B 一登录就看到 A 的队列、A 的价格区间和预算（隐私 + 误操作风险）；A 挂机中的任务被 B 启动时清掉模板、状态被覆盖；家庭/共用机器上换一个 Windows 账户就能重置试用（同一台机器、同一指纹）。

---

## 10. 【中】升级保留配置"看起来能保留"，但字段语义没有版本约束 → 降级后静默错乱

**触发条件**：升级 → 使用 → 回退到旧版（客户回滚、灰度失败、临时换包）。

**推理路径**：
- `license.py:44` `VERSION = 1`，`:142` 判定 `payload.get("v") != VERSION` **精确相等**。一旦服务端开始签发 `v: 2`：新客户端正常，旧客户端一律 `sig_invalid`——**降级不是"降级到宽限期"，而是所有许可立即失效**，且没有"容忍高版本 / 只读模式"的分支。
- `config.json` **没有版本字段**（`DEFAULTS` 全表 `core/config.py:34-175` 无 `version`），升级时不做迁移表，只做 `_merge` + `_normalize`；`_normalize` 会**静默改语义**：`trigger_mode` 的非法值一律改 `vanish`（`:276-277`）、`workflow` 与 `mode` 互相派生（`:283-289`）、`fire_at` 强制转 float（`:308-311`）、丢弃 `run_mode` / `dry_run`（`:274-275`）。
- 降级后旧版读新配置：未知键会被 `_merge` 保留并原样写回（`:317-334`，`else: self.data[k] = v`），所以"不炸"；但像 `input_mode: "interception"` / `"message"` 这种旧版不认识的枚举，会被旧版 `normalize_input_mode` 回落成默认，**通道静默换成 legacy**（`core/input.py:79-85`）——真实光标开始被移动、后台注入失效，用户看到的是"怎么突然点不动了"。
- 同理，任何未来"单位变更"的字段（`ms` → `s`、`*_s` 与 `*_ms` 混用，现在 `confirm_interval_ms` / `confirm_timeout_s` / `arm_grace_s` 已经并存）旧版会按旧单位解释 → **抢购时机偏一个数量级**，比打不开更危险且更难发现。

**用户实际会遇到什么**：回滚当天许可全部报"签名无效"；老版本能跑，但点击时机/输入通道和升级前不同，且界面没有任何提示。

---

## 11. 【中】版本号比较：现在没有版本号；唯一存在的"版本比较"是不能用于升级的精确等值

**触发条件**：将来任何人实现"有新版本吗"的判断。

**推理路径**：
- 仓库内**没有任何应用版本常量**：`pyproject.toml:3` 的 `0.1.0` 仅元数据；打包无 version 资源（`sjz.spec:89-105` 无 `version=` 参数）；前端 `web/index.html:3017` 读 `S.config.version`，服务端 `Config.public()`（`core/config.py:557-564`）不产出该字段 → 版本槽永远显示 `v—`。
- 仅有的两处"版本/代次"比较：`core/license.py:142` 用 `!=`（精确相等，语义是"格式版本必须一致"，不能拿来做 `new > old`）；`core/license.py:459-460` 的 `revoke_gen` 用 `max()` 数值比较（写法正确，但只覆盖吊销列表回滚）。
- 因此如果按常见写法补一个 `if remote_ver > local_ver`，两边都是字符串 → `"1.10" < "1.9"`、`"1.0.0-beta" > "1.0.0"` 都会判错，而项目没有任何测试钉住这一点。

**用户实际会遇到什么**：更新逻辑一旦按字符串比较落地，会出现"1.10 版用户永远收不到更新"或"beta 被当成正式版更优、反复提示升级"；同时因为没有版本号，售后无法确认用户装的是哪一版，灰度 / 回滚也无法定位。

---

## 12. 【中】自动更新下载与校验：签名、完整性、断点续传、回滚四项全缺

**触发条件**：将来实现"检查更新 → 下载 → 覆盖"。

**推理路径**：`core/server.py` 路由表（`:280-300` 一带）没有任何 update 端点；全仓无 HTTP 下载代码（唯一网络客户端 `core/license.py:604` 的 urllib，默认 TLS 校验、无证书固定、无签名校验、无分块续传）。打包链路也没有签名步骤：`build.bat` 全流程只有 `uv sync` / `pytest` / 两次 `pyinstaller`；`sjz.spec:103` `codesign_identity=None`。

按现有条件硬加自动更新会得到：无 Authenticode → 每个用户首次运行被 SmartScreen 拦（"未知发布者"）；更新包无 hash / 无签名 → CDN 或中间人可替换任意 exe（本项目的许可体系已有 Ed25519，`core/license.py:49` 公钥可复用，但目前没有"更新清单签名"这一层）；无断点续传 → 102 MB（onefile）/ 223 MB（onedir，`docs/build-desktop.md:150`）的包在网络抖动时整包重下；无回滚 → 下载/替换失败即不可运行。

**用户实际会遇到什么**：企业网络里下载到 90% 断线要重来；更新"完成"后被杀软拦或直接打不开；没有任何"上一版可用"的退路。

---

## 13. 【中】驱动安装链在成品包里是断的：文档承诺的入口不存在

**触发条件**：用户机器缺 Interception 驱动（干净机器默认如此）。

**推理路径**：
- `sjz.spec:32-44` 的 `datas` 只收 `web/`、`core/bricks.json`、`core/skins_raw.csv`、`core/assets/`；**不收 `tools/interception/install-interception.exe`**（该文件确实在仓库：`tools/interception/install-interception.exe`，470 KB）。
- 代码里没有任何调用它安装/卸载的分支（全仓检索只有提示文案：`core/interception.py:25/197/227-228`、`core/preflight.py:257`、`docs/driver-install.md`）。
- `docs/driver-install.md:44-47` 描述的是"主安装器复选框 + 首次启动提示条 → 打开随包的 `driver-setup.exe`"，两样都不存在。

**用户实际会遇到什么**：驱动缺失时程序永远停在降级通道；提示文案让用户"管理员运行 install-interception.exe install 后重启"，而那个 exe 在安装目录里根本找不到（只有在源码仓库里才有）。同理卸载时也没法执行 `/uninstall`（第 8 条）。

---

## 14. 【中】驱动装不上 / 装了没重启时的整体可用性：能跑，但核心能力静默降级，且默认配置让它"看起来在工作"

**触发条件**：HVCI / 驱动签名强校验机器装不上驱动；装了但没重启；杀软清掉驱动。

**推理路径**：
- 默认 `input_mode: "interception"` + `input_fallback: "legacy"`（`core/config.py:65-71`）；`core/input.py:211-254` 的 `_decide_channel` 会降级并写账本（不再静默，这点是对的），`input.py:506` 注释"驱动没装/没重启 → 按 input_fallback 降级或直接失败（记账）"。
- `legacy` 通道是 `SetCursorPos` + `PostMessage`，**会真实移动系统光标**（`core/interception.py:8-12` 亦承认驱动注入本身也会动光标）；而部分游戏按真实光标判定，后台注入仍会失效。
- 预检侧只给 warn/hint（`core/preflight.py:257`），不阻断开抢；若用户把 `input_fallback` 改成 `fail`，则直接失败、全部功能不可用。
- `core/selfcheck.py:200-202` 只做"驱动未安装 → 走 legacy，功能降级但可用"的说明，没有对"HVCI 下永远装不上"给出上层文案或替代路径。

**用户实际会遇到什么**：界面绿灯、任务能加、开抢后"点了没反应"或光标在桌面上乱跑；日志里只有一条降级记录，用户看不懂；在开了内存完整性的机器上无论怎么装都装不上，程序内没有任何解释。

---

## 15. 【低-中】首次运行与升级运行共用同一条落盘分支，没有迁移步骤，也没有升级前备份

**触发条件**：任何一次版本升级后的第一次启动。

**推理路径**：`Config._load_locked`（`core/config.py:475-526`）只有两支："文件不存在 → 写默认值"（`:481-484`）与"存在 → 读取 + `_merge` + `_normalize`"。没有 `config.version`、没有迁移函数、没有"迁移前把旧文件复制成 `config.json.pre-<ver>`"。唯一的备份是 `config.json.good`（`:398-405`，每次成功 save 后覆盖一份），它只保证"最近一次正常"，不保证"升级前"。
升级后旧键被 `_normalize` 静默丢弃/改写（第 10 条），且不产生任何用户可见提示。

**用户实际会遇到什么**：升级后个别开关"自己变了"（例如触发方式从 countdown 变 vanish），没有提示、也没有旧值可查。

---

## 16. 【低】安装/数据目录落在 OneDrive、网络盘或含空格/中文的路径时的偶发写失败

**触发条件**：用户把程序解压到 `桌面`（OneDrive 重定向）、映射网络盘或 `Program Files` 之外的同步目录。

**推理路径**：`core/config.py:393-410` 的原子写依赖 `os.replace`；在 OneDrive / 网络重定向目录上该调用会偶发 `PermissionError`（同步客户端持有句柄），6 次重试后抛 `OSError` 并退到直写（`open(CONFIG_PATH, "w")` 先截断再写，`:418-419`）——**这正好退回到它注释里说要避免的"半截 JSON"路径**。同时 `data/` 与 `templates/` 会被同步客户端反复上传，截图目录（上限 256 MB，`core/retention.py:21-22`）会把同步盘撑爆。

**用户实际会遇到什么**：偶尔一次保存失败导致 `config.json` 变成半截 → 下次启动走隔离 + 从 `.good` 恢复；队列回退到上一次正常保存的状态（最近改动丢失），用户看到的是"队列莫名其妙少了一条"。

---

## 17. 【低】日志/备份产物无上限、无清理（与第 8 条互补的容量问题）

**触发条件**：长期挂机 + 反复调试配置。

**推理路径**：`desktop.log` 追加写、无轮转（`desktop.py:44`）；`config.json.broken-*.bad` 每次损坏留一份（`core/config.py:545-548`）；`config.json.<pid>.<tid>.tmp` 在进程被杀时残留（`:387`）。`core/retention.py` 只覆盖 `records.jsonl`、`shots/`、`ocr_service_*.log`。

**用户实际会遇到什么**：安装目录里堆几十个 `.bad` / `.tmp`，磁盘占用缓慢增长，且这些文件会跟着"整个目录交付/备份"一起被复制（第 4 条）。

---

## 汇总

| # | 严重度 | 一句话 |
|---|---|---|
| 1 | 致命 | 装到 `Program Files` 首次启动在 import 阶段崩，无窗口、无日志、无提示 |
| 2 | 高 | 不可写目录下日志/配置/台账/截图全部静默失效 |
| 3 | 高 | 提权运行写入 `Program Files` 后，普通用户永久写不动 |
| 4 | 高 | 覆盖升级把开发机 `config.json` 带上，队列被换 + 任务模板目录被当孤儿删除 |
| 5 | 高 | 运行中覆盖 → 半更新（exe 旧 / `_internal` 新），无 staging、无回滚 |
| 6 | 高 | 更新后重启被单实例闸挡掉，退出码 0 让更新器误判成功 |
| 7 | 高 | 卸载=删目录，台账/模板/截图与程序同目录，一次清理即永久丢 |
| 8 | 中 | 卸载残留：内核驱动、`%LOCALAPPDATA%`、`HKCU\Software\SJZ-Control`、`%TEMP%\_MEI*` |
| 9 | 中 | 多用户：配置机器级共享 + 进程内锁 → 互相覆盖、互删模板；许可却是用户级 → 试用可重置 |
| 10 | 中 | 降级：许可 `v != VERSION` 精确相等 → 全灭；配置无语义版本 → 单位/枚举静默错乱 |
| 11 | 中 | 根本没有版本号（UI 永远 `v—`）；仓内唯一"版本比较"是不能用于升级的 `!=` |
| 12 | 中 | 更新下载无签名/无 hash/无续传/无回滚；构建产物也未 Authenticode 签名 |
| 13 | 中 | 成品包不含 `install-interception.exe`，文档承诺的驱动安装入口不存在 |
| 14 | 中 | 驱动装不上时静默降级 legacy（真实移动光标、后台点击失效），上层无解释 |
| 15 | 低-中 | 首次/升级共用一条落盘分支，无迁移表、无升级前备份 |
| 16 | 低 | OneDrive/网络盘上 `os.replace` 偶发失败后回退直写 → 半截 JSON |
| 17 | 低 | `desktop.log`、`.bad`、`.tmp` 无上限无清理，随目录一起被复制 |

---

## 需要实测才能定的项

以下结论必须有真机证据才能定级或确认，静态推导只能到"很可能"：

1. **`Program Files` 首次启动的真实表现**：用普通账户安装到 `C:\Program Files\SJZ-Control` 后双击，确认是"进程一闪而过"还是被某层 ACL（安装器改过权限）放行；同时抓 `desktop.log` 是否真的不存在、事件查看器里的异常模块。
2. **提权写盘后的 ACL 继承**：管理员跑一次后，`config.json` 的实际 ACL 是否仍为 `Users: 读`（默认继承），还是被安装器改写；这决定第 3 条是"必然"还是"依安装器而定"。
3. **覆盖升级时具体被锁的文件集合**：用 Process Monitor 观测运行中复制 `dist\SJZ-Control` 的失败项，确认是仅 `exe` + `*.dll/*.pyd`，还是连 `_internal` 里的模型 `.onnx` 也被锁（影响"能否热替换模型"）。
4. **两个 Windows 账户同时运行的实际冲突表现**：是否真能各起一个实例（`Local\` 互斥量跨会话原则成立，但需实测）、以及并发保存时 `config.json` 的实际落盘结果（是否只丢更新、会不会出现半截）。
5. **驱动在 HVCI / 驱动签名强校验机器上的安装失败码**，以及装完后不重启时 `driver_ready(probe=True)` 的真实返回值（代码注释已提示 `create_context` 会假阳性）。
6. **卸载后注册表与驱动残留**：装驱动 → 删目录 → 检查 `HKLM\SYSTEM\CurrentControlSet\Services\interception` 与 `interception.sys` 是否仍在，以及重启后是否仍加载。
7. **`%TEMP%\_MEI*` 残留的真实体积与触发条件**：onefile 正常退出 vs 强杀 vs 崩溃三种情况分别留多少（文档给了"几十~上百 MB"的估计，需实测确认）。
8. **OneDrive / 网络重定向目录上的 `os.replace` 失败率**：需要在这类目录上跑一段时间的保存压测，确认第 16 条的"偶发"到底是千分之一还是百分之一。
9. **SmartScreen / Defender 对未签名 onedir 与 onefile 的实际拦截形态**：是否只提示"未知发布者"，还是直接隔离（尤其 `interception.dll` 与 `onnxruntime`）。
10. **`config.json.good` 恢复路径在真实损坏下的行为**：手动写坏 `config.json` 后启动，确认是否从 `.good` 恢复、恢复后的队列是否就是用户预期的那一份（注意 `.good` 是"最近一次 save"而非"最近一次用户确认"）。
