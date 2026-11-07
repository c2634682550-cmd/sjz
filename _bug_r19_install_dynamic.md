# R19 安装 / 卸载 / 升级 动态实测报告

- 日期：2026-11-07
- 环境：Windows 11 专业工作站版（10.0.26200）
- 被测产物：
  - 新版 onedir：`dist\SJZ-Control\`（`SJZ-Control.exe` 9,788,907 B，2026-11-07 05:13:52）
  - 新版 onefile：`dist-onefile\SJZ-Control.exe`（107,005,730 B，2026-11-07 05:15:21）
  - 旧版 onefile：`dist\SJZ-Control.exe`（2026-11-06 17:46 构建）
- 方法：每个用例都把产物**拷贝到独立临时目录**再启动；临时目录用 ACL 拒绝写来模拟只读安装位置；**没有启动用户真实的安装目录，没有写真实 `%LOCALAPPDATA%` 授权文件，没有改注册表**。临时目录在测试结束后已全部删除。
- 结论先行：**能装、能升级、能回退，但“安装目录必须可写”和“配置读不出来时会误删任务目录”是两条硬伤；卸载没有安装器/卸载器，删目录会连台账和模板一起删掉。**

---

## 一、落盘位置（实测）

`--doctor` 输出的真实路径（onedir 版，临时目录）：

| 内容 | 实际位置 | 是否在程序目录 |
|---|---|---|
| `config.json` | `<安装目录>\config.json` | 是 |
| `config.json.good` | `<安装目录>\config.json.good` | 是 |
| `desktop.log` | `<安装目录>\desktop.log` | 是 |
| `templates\` | `<安装目录>\templates\` | 是 |
| `data\records.jsonl`（台账） | `<安装目录>\data\records.jsonl` | 是 |
| `data\shots\`（截图） | `<安装目录>\data\shots\` | 是 |
| `data\<任务id>\`（任务模板） | `<安装目录>\data\<8位hex>\` | 是 |
| `license.json` / `state.json` / `revocations.json` / `trial.json` | `%LOCALAPPDATA%\SJZ-Control\` | 否 |
| `trial_start`（DWORD） | `HKCU\Software\SJZ-Control\trial_start` | 否 |

代码依据：`core/config.py:26`（frozen 时 `ROOT=dirname(sys.executable)`）、`core/config.py:30`（`CONFIG_PATH`）、`core/license.py:101-106`、`core/license.py:685-693`。

> **结论：正常使用必须能写程序目录。** 把整个目录放进 `Program Files`（普通用户只读）会在启动阶段直接失败，见问题 1/2。onefile 版同样把 `config.json` 写在 exe 旁边（已实测），解包目录 `%TEMP%\_MEI*` 只是运行时代码，不是用户数据。

---

## 二、问题清单

### P1【严重】只读安装目录 + 首次启动 = 直接崩溃，不是明确报错

- **实验步骤**
  1. 把 `dist\SJZ-Control\` 整个拷到临时目录 `C_ro_fresh`（不带 `config.json`/`data`/`templates`）。
  2. `icacls C_ro_fresh /deny "<用户>:(OI)(CI)(WD,AD,WA)"`，用 `File.WriteAllText` 验证写入被拒。
  3. `SJZ-Control.exe --doctor` 启动，等待 20 秒。
  4. 另用源码同路径复现：把 `core/config.py` 放进只读目录，`python -c "from core import config; config.Config.load()"` 抓异常。
- **实际结果**
  - 进程 20 秒不退出，窗口标题为 `Unhandled exception in script`（PyInstaller 的未处理异常弹窗），只能强杀；没有 `config.json`、没有 `desktop.log`。
  - 源码同路径异常：`PermissionError: [WinError 5] 拒绝访问。: '...\templates'`，位置 `core/config.py:259` 的 `os.makedirs(d, exist_ok=True)`，调用链 `Config.load → Config() → desktop 导入 core.server`。
  - 也就是说：**初始化在 import 阶段就把 `templates/`、`data/` 建起来，没有 `%LOCALAPPDATA%` 回退，也没有“请把程序放到可写目录”的提示。**
- **会不会丢用户数据**：不会（首次启动没有数据）；但用户完全无法安装到只读位置，属于“装不上”。若已有配置但 `templates\` 缺失，同样在启动阶段崩溃（第二次只读实验里已复现 ready=False）。

### P2【高】配置、模板、台账、日志全部依赖程序目录写权限

- **实验步骤**：同 P1 的落盘位置核对；再在只读目录中放入完整的 `config.json` + `templates\` + `data\records.jsonl`，正常启动。
- **实际结果**：应用能打开、`/api/config` 可读、`/api/records` 可读（3 行），但任何保存都失败（见 P4）。`desktop.log` 因为无法追加，整个文件不存在，排障信息为零。
- **会不会丢用户数据**：不会直接丢，但用户的修改全部无法落盘；卸载时这些数据又全在程序目录里，见 P7。

### P3【严重】`config.json` 被占用/读不出来时，启动会按空队列清理 `data\<任务id>\`，删掉任务模板

- **实验步骤**
  1. 临时目录 `I_lock`：`config.json` 的 `queue` 只有一条 `id=abcdef01`；`data\abcdef01\buy.png` 已存在。
  2. 用另一个句柄以 `FileShare.None` 独占打开 `config.json`（模拟杀软/同步盘/备份软件短暂锁文件）。
  3. 启动 `SJZ-Control.exe --port 8991`，等 `/api/config` 就绪。
  4. 查询 `/api/config`、检查 `data\abcdef01\buy.png` 是否还在。
- **实际结果**
  - `/api/config` 返回 `config_ok=false`、`queue=0`；窗口照常打开。
  - 启动后 `data\abcdef01\buy.png` **已被删除**（`after_exists=False`），`data` 目录为空；`desktop.log` 没有任何“配置读取失败/清理目录”的记录。
  - 代码路径：`core/scheduler.py:149-158` 用 `Config.load().gc_task_dirs(keep=[t.get("id") for t in self.tasks()])`；读盘失败时 `tasks()` 返回启动时的空快照（`core/scheduler.py:166-186`），`keep=[]`；`Config.gc_task_dirs`（`core/config.py:580-598`）**没有检查 `load_ok`**，把 `data` 下所有 8 位 hex 目录全删。调用顺序在 `core/server.py:1102-1104`（先 `_seed_default_task()` 再 `gc_task_dirs()`）。
  - 本次启动后如果用户再改任何设置，`Config.save` 会把默认空队列写回盘（原配置被覆盖）。
- **会不会丢用户数据**：**会。** 丢的是每个任务框选生成的任务模板目录（`data\<id>\`）；配置本身在被锁期间未被覆盖，但后续一次保存就会把队列写成空。这是最需要修的一条。

### P4【中】只读/保存失败时界面无提示，修改返回 500 且没有日志

- **实验步骤**：`C2_ro_existing`（已有 `config.json` + `templates\`，ACL 拒绝写）启动，`POST /api/config {"threshold":0.77}`。
- **实际结果**：`POST` 返回 `500 Internal Server Error`；磁盘上的 `threshold` 仍是 `0.85`；目录里没有 `desktop.log`。用户看不到“配置保存失败”，重启后改动静默消失。
- **会不会丢用户数据**：会丢“本次修改”（不是历史数据），且没有可排查的日志。

### P5【中】回退到旧版后失去 `config.json.good`；旧版遇到损坏配置会重置为默认

- **实验步骤**
  1. 用新版写出一份完整配置（含队列），拷到 `F_roll`，用旧版 `dist\SJZ-Control.exe` 启动，`POST /api/config` 触发一次保存。
  2. 再把 `config.json` 截断成 200 字节，用旧版 `--doctor` 启动。
- **实际结果**
  - 旧版启动不崩溃，队列/区域/`bar`/`user_notes`/`console.target` 全部保留，台账 3 行可读可继续用——**回退到 11/6 这一版是安全的**。
  - 但旧版保存后目录里**没有 `config.json.good`**（新版会自动维护），备份机制随回退消失。
  - 截断配置后旧版把坏文件留档为 `config.json.broken-20261107_061811.bad`，但因为无 `.good`，直接回落默认值：`queue=0`、`threshold=0.85`、`user_notes` 清空。
- **会不会丢用户数据**：会。损坏时会重置运行配置（`.bad` 还能人工捞回），队列在运行态丢失。旧版配置代码（`a7f332c`，早于 `bar`/`workflow`）实测更严重：坏文件被 `Config.load` 吞掉并**直接覆盖写默认值**，连 `.bad` 都没有。

### P6【中】升级/回退都会丢弃“当前版本不认识的区域名”

- **实验步骤**
  1. 用旧版配置代码生成配置，额外加入 `regions.legacy_box`，用新版启动并 `POST /api/config` 触发保存。
  2. 反向：用新版配置（含 `regions.bar`）跑旧版配置代码 `Config.load()+save()`。
- **实际结果**
  - 新版保存后 `regions` 只剩 `buy,clock,confirm,list,list_refresh,name,price`，`legacy_box` 消失。
  - 旧版代码保存后 `regions.bar` 消失（旧版默认里没有 `bar`）。
  - 原因：`core/config.py:317-320` 只保留 `self.data["regions"]` 里已存在的名字。
  - 其余未知字段表现良好：顶层 `user_notes`、`latency`、任务里的 `note`/`threshold`、`console.target`、`screen.*`、`filter_defaults.*`、`ocr.*`/`ocr.service.*`、`hotkey.*`、`time_sync.*` 都被保留（`ocr/hotkey/time_sync` 走 update 合并，其余整字典保留）。
- **会不会丢用户数据**：会丢“区域框选”配置（价格条/旧版扩展区域），需要用户重新框选；不影响台账。

### P7【中】没有安装器/卸载器：卸载=删目录，配置/队列/模板/台账/截图一起消失

- **实验步骤**：`U_uninstall` 里放 `config.json`（队列 1 条）+ `data\records.jsonl`（3 行）→ 直接删整个目录 → 扫描用户目录/注册表/启动项。
- **实际结果**
  - 删目录后：`queue=0`、台账 `3 行` 全部不可恢复；没有导出/提示/保留选项。
  - 残留：
    - `%LOCALAPPDATA%\SJZ-Control\`（`app_dir` 存在；本机为空目录，因为未激活。实机激活后这里是 `license.json` / `state.json` / `revocations.json` / `trial.json`）
    - `HKCU\Software\SJZ-Control\trial_start = 1700000000`（既有值，本次只读未改）
    - onefile 被强杀时残留 `%TEMP%\_MEI*`（见 P8）
  - 无 `Run` 键、无启动文件夹快捷方式、无系统服务（Interception 驱动未安装，所以驱动残留未验证）。
- **会不会丢用户数据**：**会。** 台账（成交记录）、任务模板、截图证据、价格/区域设置都在安装目录里，删目录即全部丢失。授权文件和试用标记反而留在用户目录/注册表里（重装可继续用授权，但无法通过“卸载”清除）。

### P8【低】onefile 异常退出/强杀残留 `%TEMP%\_MEI*`（单次约 200–270MB）

- **实验步骤**：onefile 版从临时目录启动，`/api/config` 就绪后 `Stop-Process -Force`，3 秒后检查 `%TEMP%`。
- **实际结果**：运行中 `_MEI0000403c2` 214.1MB；强杀后仍存在（`after_kill_count=1`）。正常 `--doctor` 退出时该目录会被清理。之前还观察到 208.8MB / 273.7MB 两个残留，数分钟后自行消失，无法稳定复现长期残留。
- **会不会丢用户数据**：不会，占磁盘；频繁崩溃可能累积。

### P9【低】空队列每次启动都会自动补一条默认任务（用户主动清空也会“复活”）

- **实验步骤**：`H_seed` 队列设为 `[]`，启动，查 `/api/tasks`。
- **实际结果**：启动后队列变 1 条，`id` 是新生成的 `6f654827`（上次是 `eba2c1c7`）。代码：`core/server.py:1008-1020`，只要 `queue` 为空就 `add`。
- **会不会丢用户数据**：不会；但用户“删光任务、下次启动再说”的意图无法保持，且配上 `max_price=0` 时新任务等于“看到就买”（`_seed_default_task` 注释里也提到这个风险）。

---

## 三、通过项（动态实测）

1. **可写目录首次启动**：自动生成 `config.json`（2693 B）+ `config.json.good` + `templates\` + `data\`，无需管理员；`--doctor` 显示 `配置目录可写 = True`。
2. **二次启动不覆盖配置**：同一目录第二次启动前后 `config.json` SHA256 不变，队列仍为 1 条，没有重复播种。
3. **升级迁移正确**：旧配置里的 `mode=list` 任务升级后同时具备 `mode=list` 与 `workflow=list`；`fire_at` 从字符串 `"123.4"` 规范成 `123.4`；`status=""` 规范成 `pending`；`active_task`/`console`/`latency` 保留。
4. **未知字段保留**：顶层、任务级、`screen`/`filter_defaults`/`ocr`/`hotkey`/`time_sync`/`console` 子字段全部保留；区域子字段（`regions.buy.future_rect_key`）也保留。例外只有“未知区域名”（见 P6）。
5. **台账升级连续**：旧格式 2 行（含一行 `verified` 缺失）→ 新版读取 `rows=2`；用同模块 `records.add` 追加第 3 行 → 重启后 `rows=3`，统计 `tries=3`。
6. **旧版回退不崩溃**：新版配置（含 `bar`、`workflow`、台账 3 行）用 11/6 旧版启动，正常打开；`/api/records` 读到 3 行；保存后关键字段全部保留（仅丢 `.good`，见 P5）。
7. **新版损坏配置可自愈**：截断 `config.json` 后启动，生成 `config.json.broken-20261107_061118.bad`，并从 `config.json.good` 恢复，`queue=1` 保留。
8. **onefile 正常退出清理**：`--doctor` 正常结束后 `%TEMP%\_MEI*` 被删除。
9. **无自启动残留**：未写入 HKCU/HKLM `Run`、未创建启动文件夹快捷方式、未安装服务（Interception 驱动本次未安装）。
10. **卸载后的“必要保留数据”边界清楚**：授权/试用信息在用户目录/注册表，重装可继续生效；但业务数据（配置、台账、模板、截图）全在程序目录，见 P7。

---

## 四、无法验证项

1. **真实成交触发台账追加**：需要游戏内真实成交。本次用同模块 `records.add` 直接追加到 `data\records.jsonl`，只验证了文件格式与读取连续性，未验证购买流程写台账。
2. **激活/试用文件的真实残留内容**：为避免污染真实环境，未在 `%LOCALAPPDATA%` 写入 `license.json`/`state.json`/`trial.json`，只验证了路径与空目录；`HKCU\trial_start` 是既有值，未修改。
3. **Interception 驱动卸载**：未安装内核驱动；`install-interception.exe` 的残留/卸载需要管理员操作，本次未跑。
4. **断电/写盘中断下的配置半写**：新版是“临时文件 + `os.replace` + `.good`”，旧版配置代码是非原子写；未做断电注入，无法给出确定性结论。
5. **以管理员身份安装到 `C:\Program Files`**：本次用 ACL 拒绝写来模拟只读目录，没有真的以管理员写系统目录。
6. **多用户/漫游配置/OneDrive 同步目录**：配置随程序目录走，放在 OneDrive 下会同时被同步；未测同步冲突与多用户切换。
7. **onefile 长期 `_MEI` 残留的稳定性**：观察到强杀后残留 214MB，也观察到数分钟后自行消失，无法稳定复现“永久残留”。
8. **WebView2 缓存位置**：本机 `%LOCALAPPDATA%\SJZ-Control` 未产生 WebView 缓存，其他机器（不同 WebView2 版本）未验证。

---

## 五、修复优先级建议

1. **P3（最高）**：`gc_task_dirs` 必须检查 `Config.load_ok`，读盘失败时直接 `return 0`；`scheduler.gc_task_dirs` 也要在 `tasks_status()["config_ok"] == False` 时跳过。
2. **P1/P2（高）**：程序目录不可写时给出明确错误（或把 `config.json`/`data` 迁到 `%LOCALAPPDATA%\SJZ-Control`），不要 import 阶段崩溃；至少把 `os.makedirs` 的 `PermissionError` 转成可读弹窗。
3. **P7（中）**：卸载/删除目录前提示“配置、台账、模板、截图将一并删除”，或提供导出；说明授权/trial 残留在 `%LOCALAPPDATA%` 与 `HKCU`。
4. **P5/P6（中）**：回退旧版前备份 `config.json.good`；旧版加载保留未知区域名（至少原样写回，不要过滤）。
5. **P4/P8/P9（低）**：保存失败要在界面给出“配置写不进去（目录只读/被占用）”；异常退出清理 `_MEI`；空队列播种改成“仅首次安装时一次”，用持久标记区分“用户主动清空”。
