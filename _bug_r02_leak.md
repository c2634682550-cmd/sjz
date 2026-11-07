# R02 · 资源生命周期与泄漏专项审查

- 对象：`C:\Users\26346\Desktop\project\dafors`（当前工作树，分支 `main`）
- 时间：2026-11-07 06:00–06:35（Asia/Shanghai）
- 范围：**只查资源生命周期/泄漏**（文件句柄、socket、Win32 句柄、GDI/User 对象、截图/图像缓冲、子进程与管道、临时文件、日志句柄、线程、闭包常驻、缓存上限、finally/异常路径清理）
- 边界：只读审查，**未改任何产品代码**，未 commit、未 push；未启动真机游戏、未移动真实光标、未开可见窗口
- 工作树状态：`git status` 有 107 项改动/未跟踪文件（`core/*.py`、`tests/*`、`web/index.html` 等均处于**未提交修改态**，同时有 9 个并行会话在写同一工作树）。本报告行号以本次快照为准，后续改动可能造成行号漂移；这些改动本身**不作为 bug**。

---

## 0. 结论摘要

| # | 问题 | 严重度 | 增长速率 |
|---|------|--------|----------|
| L01 | `Screen` 构造失败（显示器序号非法）泄漏 2 个 GDI 对象，且永不回收 | 中 | +2 GDI / 次失败建屏（GC 不回收） |
| L02 | HTTP Keep-Alive 无读超时：每条滞留连接常驻 1 线程 + ≈7 个内核句柄，半开连接不回收 | 中 | +7 句柄 +1 线程 / 条滞留连接（随异常断连单调累计） |
| L03 | `PageWatcher.stop()` 关屏但不 join、不置空 `_nav`，重启复用已关闭 Screen | 低 | 句柄不增长；watcher 永久异常空转 |
| L04 | OCR 子进程：Popen 失败时日志句柄滞留；`taskkill` 返回非 0 时不补 `kill`，旧进程可能残留 | 低 | 失败重启 +1 文件句柄（自愈，上限 1）/ +1 残留进程 |
| L05 | `license._write_json` 原子写无异常清理分支（config.py 已受保护） | 低（理论） | 正常 0/h；异常时 1 个 `.tmp`/次，文件名唯一不覆盖 |
| L06 | 热键每次触发新建线程，无并发上限 | 低 | 峰值 ≈8 线程后回落，非单调 |

**主路径无泄漏**：引擎每轮新建/丢弃、1000 轮异常注入、2h/7.02h 长跑，句柄/线程/内存均平（详见 §3）。历史 72h 数据里的单调增长（+168/4259 轮）发生在 `engine.py`（5:48）与 `tools/stability/workload.py`（5:54）修复之前，旧结果不能代表现状。

---

## 1. 方法与采样口径

- 句柄总数：`GetProcessHandleCount`
- 句柄类型分布：`NtQuerySystemInformation(SystemExtendedHandleInformation)` + `NtQueryObject(ObjectTypeInformation)`；本机类型编号实测：**24=Semaphore（CPython `_thread.LockType/RLock` 底层）、21=Event、9=Section、41=WaitCompletionPacket、56=EtwRegistration**
- GDI/USER：`GetGuiResources(GR_GDIOBJECTS / GR_USEROBJECTS)`
- 线程：`CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD)`；同时统计 `threading.enumerate()`
- 文件句柄：包装 `builtins.open` 记账（未关闭项计数）；辅以 `psutil` 句柄类型
- 内存：`GetProcessMemoryInfo.PrivateUsage` + `tracemalloc` 可选
- 长循环：仓库自带 `tools/stability/longrun.py`（`VirtualClock` 把 60s 周期压缩为真实 0.1–0.02s/步），每 10 虚拟分钟采样一次
- 异常注入：仓库 `tools/stability/faults.py` 8 个场景 + 自建 1000 轮异常循环（**每轮新建异常实例**，避免测试自身持有 traceback）+ 200 次非法显示器序号构造
- 探针脚本放在 `%TEMP%\dafors_leak_probe\`（probe.py / probe2.py / probe3.py / probe5.py），**未写入仓库**

### 趋势数据

| 运行 | 等效时长 | 轮次 | 采样点 | 句柄 | Python 线程 | open files | Private |
|------|----------|------|--------|------|-------------|-----------|---------|
| 2h 长跑 | 2.0h | 119 | 12 | 319 → 323（+4） | 1 | 0 | 平 |
| 72h 加速长跑 | 7.02h | 415 | 43 | 324 → 324（min=max=324） | 1 | 0 | 521 → 515 MB |
| 异常注入循环 | 1000 轮 | 1000 | 首尾 | +8（一次性） | 1 | 0 | — |
| 历史 pre-fix 对照 | 72h | 4259 | 432 | 214 → 382（峰值 410，+168，类型 24） | — | — | — |

> +4 与 +8 均为“当前仍存活 1 个 Engine（4 个锁/Event）+ 进程初始化”的一次性量，不随轮次增长。

---

## 2. 问题清单

### L01 · `Screen` 构造失败泄漏 2 个 GDI 对象（永不回收）

- **严重度**：中
- **文件与行号**
  - `core/screen.py:55` `self._sct = mss.mss()`
  - `core/screen.py:57` `self.index = self._pick_monitor(monitor)` → 校验失败在 `:96 / :98 / :102` 抛 `ValueError`
  - `core/screen.py:150` `def close()`；整个类**没有 `__del__`**（文件共 134 行）
  - 触发现场：`core/engine.py:551-553`（每轮启动）、`core/actions.py:44`（每次定位/导航/校准）
- **增长速率**：**+2 GDI / 次失败建屏**；实测 200 次非法 monitor 构造 → GDI **+400**，`gc.collect()` 后不回落（388 → 788）。
  - 例：配置 `monitor=2` 而副屏被拔掉时，每次“开始任务”或每次点“定位”都 +2；按 10 次/分点击 → **1200 GDI/h**；进程 GDI 上限 10000 → 约 8h 后 `CreateCompatibleDC/CreateCompatibleBitmap` 开始失败，表现为截图/OCR/界面渲染全线报错。
- **定位依据（动态）**
  - `mss.windows.MSS.__init__` 在构造时就 `CreateCompatibleDC` + `CreateCompatibleBitmap`（2 个 GDI 对象），而 `_pick_monitor` 的 `ValueError` 在其**之后**抛出；`Screen` 无 `__del__`，`mss` 实例也不依赖 GC 释放 DC → CPython 只回收 Python 对象，GDI 句柄只能等进程退出。
  - 正常路径对照：30 轮 `Screen → grab×3 → close()`，GDI 回到基线（0 增量），证明 `close()` 本身有效，问题只在构造失败路径。
- **修复方向**：`__init__` 内把 `_pick_monitor` 包进 `try/except`，异常时先 `self._sct.close()` 再 `raise`；或先做纯函数式参数校验（只用 `self.monitors` 列表，不建 mss）再创建 `mss.mss()`；另加 `__del__`/上下文管理器兜底。

### L02 · HTTP Keep-Alive 无读超时 → 每条滞留连接 1 线程 + ≈7 句柄，半开连接不回收

- **严重度**：中
- **文件与行号**
  - `core/server.py:168-169` `class Handler` + `protocol_version = "HTTP/1.1"`，**未设置 `timeout`**（继承 `StreamRequestHandler.timeout = None` → socket 无读超时）
  - `core/server.py:1099` `daemon_threads = True`（ThreadingMixIn 每连接一线程，退出不阻塞，但运行期线程不会被回收）
  - `core/server.py:1163` `serve_forever()`；`core/server.py:1170` `server_close()`
- **增长速率**：实测 60 条“拿到响应后不关闭”的连接 → **+60 线程、+424 内核句柄（≈7.07/条）**；客户端 `close()` 后 2.2s 内全部回落。
  - 正常浏览器会复用并关闭连接（稳态≈连接池大小 6–12，非无限）；
  - **异常断连**（浏览器崩溃、系统休眠、断网、调试器直接杀进程、未发 FIN）时，服务端阻塞在 `rfile.readline()` 且该 socket 从未启用 `SO_KEEPALIVE`、也无应用层超时，服务端只读不写 → 不会触发 TCP 重传超时 → 线程与句柄**常驻到进程退出**，每多一次异常断连就 +7 句柄 +1 线程，无上限。
- **定位依据（动态）**：`probe2.py server`：20/40/60 条空闲 Keep-Alive 连接时线程 1→21/41/61、句柄 234→295→355→377；关闭客户端后回落 —— 证明是“按连接线性常驻”，回收只发生在客户端关闭，而不是服务端超时。
- **修复方向**：`Handler.timeout = 30`（`StreamRequestHandler.setup` 会 `settimeout`；`handle_one_request` 捕获 `socket.timeout` 会把 `close_connection=True`）；或 `protocol_version = "HTTP/1.0"` / 响应加 `Connection: close`；或对监听/接受到的 socket 设 `SO_KEEPALIVE` + `TCP_KEEPIDLE/INTERVAL`。

### L03 · `PageWatcher.stop()` 关闭 Screen 但不 join、不置空 `_nav`

- **严重度**：低
- **文件与行号**：`core/pagewatch.py:67-73`（`stop()`：`_stop.set()`、`self._nav.screen.close()`，**无 `join`、`_nav` 未置空**）；`:82`（`start()` 在已有 `_nav` 时直接复用）；`:88`（Navigator 持有该 Screen）
- **增长速率**：句柄不增长（Screen 已被 close）。问题是生命周期错误：`stop()` 后 `_nav` 仍引用**已关闭**的 mss；再次 `start()` 若旧线程已退出，新循环用已关闭 Screen → 每 `interval`（默认 1.5s）抛一次异常且被 `_loop` 吞掉 → watcher 永久“活着但失效”。若 `stop()` 时旧线程正处在 `nav.where()`（OCR 中）→ 关闭中的 mss 被并发使用。
- **定位依据**：静态路径 + 100 轮 fake-nav `start/stop` 实测线程 0 增量（说明无泄漏，但“关闭后复用”路径确实存在）。当前唯一调用点是 `core/server.py:1167` 退出流程，所以现实影响小；一旦以后加“运行中重启监视”就是必现故障。
- **修复方向**：`stop()` 里 `self._thread.join(timeout=interval*2)` 并 `self._nav = None`（Screen 生命周期交给 nav）；或 `_refresh_once` 使用局部 nav 引用，stop 不直接关 Screen。

### L04 · OCR 子进程重启的两个异常路径

- **严重度**：低
- **文件与行号**
  - `core/ocr_service.py:464` `self._log_fh = f` → `:469` `subprocess.Popen(...)`：**Popen 抛异常时 `f` 未关闭**，直到下次 `_spawn_real`（`:456` 的 `_close_log()`）或 `stop()`（`:141`）才释放
  - `core/ocr_service.py:548-557` `_kill_real`：`proc.terminate(); proc.wait(8)` 失败后走 `taskkill`，但**未检查 `taskkill` 的 returncode**，返回 1 且不抛异常时不会再执行 `proc.kill()`
- **增长速率**
  - 日志句柄：每次 Popen 失败滞留 1 个文件句柄（自愈，稳态上限 1）；期间日志文件被占用 → `_rotate` 的重命名会失败
  - 残留进程：每次“杀不掉”的重启残留 1 个 paddlex 进程（内存/GPU/端口），会被下一轮 `_sweep_port` 收养或再叠一个
- **定位依据**：代码路径审查（本机无 `.venv-vl\paddlex.exe`，真实 Popen 失败注入无法执行，见 §4）。`_kill_real` 的 `except` 分支里有 `proc.kill()`，但 `taskkill` 正常返回（fail code）时不进 `except`。
- **修复方向**：`Popen` 包 `try/except` → `f.close(); self._log_fh = None; raise`；`taskkill` 后补 `if proc.poll() is None: proc.kill()`（或直接 `check=True` 并捕获 `CalledProcessError`）。

### L05 · `license._write_json` 原子写无异常清理分支

- **严重度**：低（理论路径）
- **文件与行号**：`core/license.py:167-172`（`_write_json`：`json.dump(obj, f)` → `os.replace(tmp, path)`，**无 `try/except`、无 `finally` 清理**）
  - 对照（**不构成问题**）：`core/config.py:652` 先 `payload = json.dumps(...)` 再开临时文件（序列化异常时文件还没创建）；`core/config.py:660-672` 的 `_write_payload` 有 `except OSError → _remove_quietly(tmp)`；`:328-358` 的 `_cleanup_orphan_tmp()` 在启动/保存时按 `_TMP_RE` 清掉 60s 以上的崩溃残留；`:699` 的 `_write_backup` 捕获 `(OSError, ValueError, UnicodeDecodeError)`。原稿把 config.py 列入本条，经复核不成立，已收窄到 license。
- **增长速率**：正常 0/h（实测 12000 次保存无 `.tmp` 残留）；只有写入不可序列化对象时每失败 1 个，文件名含 PID 不会互相覆盖 → 理论无上限，但 license 载荷是 JSON 原生类型，正常不可达。
- **定位依据**：动态 12000 次 `Config.save`/`records.clear` 采样 + `license.py:167-172` 异常路径静态分析。`PermissionError`（真正常见失败）落在 `open`，此时 tmp 尚不存在，不产生残留。
- **修复方向**：`try: ... except (OSError, TypeError, ValueError): _remove_quietly(tmp); raise`，或 `finally` 里“存在才删”。

### L06 · 热键每次触发新建线程，无并发上限

- **严重度**：低
- **文件与行号**：`core/hotkey.py:121-152`（`_dispatch` 在 :121，:141 每次 `Thread(target=run, name="hotkey-fire")`，仅 :149 的 0.4s 去重）；同类写法还有 `:272/:281`（HookHotkey 的 `_dispatch`）；回调链 `_hotkey_stop → Scheduler.stop()`（`join` 3s）
- **增长速率**：触发频率 ≤2.5/s（0.4s 去重）× 单线程存活 ≤3s → 峰值 ≈8 条瞬时线程，任务结束即回落，**非单调增长**。
- **定位依据**：静态审查。未做全局键盘钩子实机按压（约束见 §4），因此按“代码路径 + 最坏并发估算”给低危。
- **修复方向**：单 worker 线程 + 队列；或在 `Thread.is_alive()` 时跳过；`_hotkey_stop` 加 non-blocking guard。

---

## 3. 已确认无问题的项（含证据）

1. **Engine 主路径无泄漏**：单独 60 次 `Engine()` 创建/删除 → 0 增量；工作负载 120 轮逐轮采样无增长；2h（119 轮）319→323；7.02h（415 轮/43 采样）恒 324、线程恒 1、`open_files` 恒 0、Private 521→515MB。
   修复依据：`core/engine.py:170-193`（注释 :170-174，实现 :175-193）把 `_cleanup` 的适配器改为弱引用闭包（`self_ref = weakref.ref(self)` 在 :175，`recovery.Cleanup(...)` 在 :189），断开了 `Engine → Cleanup → MethodType → Engine` 环；`tools/stability/workload.py:96-106` 解开了测试侧 `MethodType` 环。实测 `weakref` 存活引擎数 = 0。
2. **异常路径无泄漏**：1000 轮异常注入（每轮新建 `OSError`）→ 句柄总增量 **+8**（类型：9:1、21:3、24:1、41:2、56:1，全为进程初始化一次性），`Event/lock/RLock` 对象数前后不变，引擎存活 0。`tools/stability/faults.py` 8 个场景全部 PASS，尾部线程/文件句柄 0 增长。
   - **测试假阳性披露**：曾用“复用同一个异常实例”的写法跑到 +4 Semaphore/轮，原因是 `exc.__traceback__` 持有引擎帧、而异常实例被测试字典持有；换成每轮新建异常即归零。**这是测试假阳性，不是产品泄漏**，记在此防止他人误读。
3. **句柄类型映射已确认**：本机 24=Semaphore、21=Event、9=Section、41=WaitCompletionPacket、56=EtwRegistration；历史增长全部落在 24（CPython 锁），说明是对象引用环拖住锁，不是文件/驱动/网络句柄泄漏。
4. **jobs**：300 次提交 + 逐个完成，`_jobs` 稳态 ≤20（`core/jobs.py:20,53-57`），线程 0 增长，已完成项会被驱逐。
5. **pagewatch 线程**：100 轮 `start/stop`（fake nav）线程 0 增长；`stop` 语义问题见 L03，但不是句柄泄漏。
6. **fire_proc**：25 次重启 + 每轮 `fire/wait` + `close()`，线程 2→1，句柄 +12 恒定（1 个常驻 worker 的 stdin/stdout/进程/线程句柄），无累计；`_pump` 在 terminate 后读 EOF 退出（`core/fire_proc.py:101-130`）。
7. **precision**：200 发 `Shot` 建/`join`/`del` → 句柄回基线、线程 0 增长（Shot 持有 2 个 Event 属正常对象持有，不是泄漏）。
8. **records / shots 上限有效**：12000 行写入后 `open_files` 0 增量；轮转上限 3 个文件（`core/retention.py:17-18`）；72h 采样 `records_bytes≈110KB`、`shots_files=400`（上限 400/256MB），无无界增长。
9. **Screen 正常路径**：30 轮 `create → grab×3 → close()`，GDI 回到基线；`close()` 有效。
10. **HTTP 正常关闭**：`shutdown()` + `server_close()` 后线程/句柄归零；`daemon_threads=True` 保证进程退出不被 handler 拖住。
11. **缓存全有上限**：`ocr_ppocr._Cache`（`cache` 默认 64，`popitem` 淘汰）、`digit_ocr` 索引缓存单例且 reset 清理、`ocr_rapid.recent=deque(maxlen=20)`、`scene_guard` 证据 `deque(maxlen=60)`、Engine 日志 `deque(maxlen=300)`；7.02h 运行 `gc.get_objects` 35591→35632（平）。
12. **Interception 是单例**：`core/interception.py:247` `_SHARED = Interception()`（`:169-215` RLock 串行化），不会每个 `Mouse`/每个动作新建驱动 context；`Mouse()` 只做模式判断。
13. **子进程/管道**：fire worker 的 stdout 泵线程在终止后退出；OCR Supervisor 只有一个轮询线程（restart 不新起线程），`stop()` 会 join 并 `_close_log()`（`core/ocr_service.py:130-145`）。
14. **日志句柄**：OCR 日志在 `_spawn_real` 开、`stop`/下次 spawn 关；`_rotate` 在 `_close_log` 之后执行，无“打开中重命名”失败；server 无长期持有的日志文件句柄。
15. **config.py 临时文件已有保护（原稿误列，撤回）**：`:652` 的 `payload = json.dumps(self.data, ...)` 在打开 `config.json.<pid>.<tid>.tmp` **之前**执行，序列化异常时临时文件尚未创建；`:660-672` `_write_payload` 的 `except OSError` 会 `_remove_quietly(tmp)` 后重抛；`:674-700` `_write_backup` 捕获 `(OSError, ValueError, UnicodeDecodeError)` 并清理；`:328-358` `_cleanup_orphan_tmp()` 按 `_TMP_RE` 清理 60s 以上的崩溃残留（`:712` 处调用）。动态 12000 次保存 `open_files` 0 增量、目录无 `.tmp` 残留。**此条不计入 L05。**

---

## 4. 无法验证 / 未覆盖

1. **真实 mss 长时间抓屏曲线**：只做了 30 个正常周期与 200 次非法构造；**未做多显示器热插拔、改分辨率、睡眠唤醒**（需要真机交互，超出“不开可见窗口/不碰真机”的约束）。
2. **OCR 子进程真实启动**：本机无 `.venv-vl\paddlex.exe`，`Supervisor._spawn_real` 的 Popen 失败注入只做了**代码路径分析**（L04）；真实安装模型后可复现。
3. **半开连接最终回收时间**：只验证了“客户端主动 close 会回收”，未实测 Windows TCP 栈放弃半开连接所需的完整时间窗（与 L02 的“常驻到退出”结论一致，但未跑到 2h+）。
4. **全局热键钩子**：`HookHotkey` 的 `WH_KEYBOARD_LL` 安装/卸载 100 轮未实机执行（避免在用户真实输入链上装全局钩子）；L06 为静态结论。
5. **taskkill 被权限拒绝的真实概率**：未在受限账户下验证 L04(b)。
6. **并发写工作树**：其他 9 个会话正在改同一目录（`core/*` 为未提交修改态），本次行号/内容可能已被后续改动覆盖；如需复核请以 commit 后的快照为准。
7. **压力归因的环境噪声**：本次 7h 加速跑中 `last_cycle_ms` 从 564ms 漂到 2923ms（长跑里另有“记录轮询 232–406ms/次”告警）；同时段机器上有 10 个并行会话跑同类 soak，磁盘/CPU 竞争明显。该漂移属于**性能问题、不是本专项的泄漏结论**，故不在此定级，只标注为环境干扰。

---

## 5. 复跑命令（只读，产物写临时目录）

```powershell
# 2h 等效
python -m tools.stability.longrun --hours 2 --out "$env:TEMP\dafors_leak_probe\r02_2h"

# 72h 等效（加速，约 8.5 分钟真实时间）
python -m tools.stability.longrun --hours 72 --sleep-step 0.02 --max-real-s 520 --out "$env:TEMP\dafors_leak_probe\r02_72h_fast"

# 8 项故障注入（结果目录重定向到临时目录，避免污染仓库）
python -c "import sys; sys.path.insert(0,r'C:\Users\26346\Desktop\project\dafors'); from tools.stability import faults; faults.RESULT_ROOT=r'$env:TEMP\dafors_leak_probe\faults'; sys.exit(faults.main())"
```

- 分段探针（句柄类型/GDI/连接/重启/异常）：`%TEMP%\dafors_leak_probe\probe*.py`，不在仓库内。
- 历史对照数据在 `tools/stability/_results/`（5:18–5:51 采集），对应**修复前**工作树，仅作趋势对照，不代表当前状态。
