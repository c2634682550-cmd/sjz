# 输入链路与倒计时卡点报告（进行中）

状态：**第 1 轮（抢鼠标根因 + 零干扰点击路径骨架）已完成；延迟基准与倒计时卡点待并行分支数据回传后集成。**
工程：`C:\Users\26346\Desktop\project\dafors`　机器：AMD Ryzen 7 7800X3D / Win11 26200 / Python 3.11.9（64 位）

---

## 0 结论速览

1. **「抢鼠标」不是设备被独占，是系统光标被挪走并被持续占用。** 实测单次 legacy 点击把光标按在目标上 **120.9ms**，连点 8 次累计 **966.6ms**；这 0.97s 里 **96.8% 的采样点光标都偏离用户原位**，最大位移 **472px**，最长连续偏离 **≈1370ms**。
2. **现在的点击默认路径不是驱动，是 legacy（`SetCursorPos` + `PostMessage`）**。`Interception.dll` 和驱动其实都已就位、`driver_ready()=True`，所以「没装驱动」不是原因；真正在抢的是 `_click_messages` 里那套「跳过去 → 停 40ms → 按下停 60ms → 还原」。
3. **零干扰唯一成立的是纯消息投递**：本地窗口实测连续 200 次点击，窗口收到 **200/200** 次 `WM_LBUTTONDOWN`，200Hz 采样 **无一次**光标落到目标点上，每次 0.025ms。代价：**Raw Input 层收不到**（实测 `WM_INPUT` = 0），所以游戏若只认 Raw Input 就必须退回「借光标」。
4. **驱动注入必然动光标**：Interception 绝对注入后 `GetCursorPos` 直接跳到目标点；且注入事件在途时立刻还原会被覆盖（两次独立实验都复现），所以它只能算「可选增强」，不能算零干扰。
5. 三条路径在两层独立观测上的表现（同一窗口、同一目标点）：

| 路径 | 窗口消息层 | Raw Input 层 | 低级钩子层 | 是否动系统光标 |
|---|---|---|---|---|
| `PostMessage` | ✅ 收到 | ❌ 0 条 | ❌ 0 条 | **不动** |
| `SendInput` | ✅ 收到 | ✅ 3 条（含 2 条按键） | ✅ 3 条，全部带 `LLMHF_INJECTED=1` | 动 |
| `Interception` | ✅ 收到 | ✅ 3 条（含 2 条按键） | ❌ 0 条（绕过低级钩子） | 动 |

---

## 1 根因：所有会占用物理输入的位置

逐条列出，行号以当前工作区为准。

### 1.1 直接挪光标（主要根因）

| 位置 | 行为 | 触发条件 |
|---|---|---|
| `core/input.py:231-234` `_click_messages` | `SetCursorPos(sx,sy)` 后固定 `sleep(0.04)` | 每次 legacy 点击（驱动不可用或 `mode="legacy"`） |
| `core/input.py:246-252` | 发 `WM_LBUTTONDOWN` 后固定 `sleep(0.06)` 才发 up | 同上，按住期间光标被钉在目标上 |
| `core/input.py:259-264` | `verify` 时 `sleep(0.05)` 读回光标，`finally` 才还原 | `verify=True` 时额外再占用 50ms |
| `core/input.py:159-170` `Mouse.click` 驱动分支 | `send_mouse(MOUSE_MOVE_ABSOLUTE)` + down/up | `mode="interception"` 且驱动就绪；**注入即真实移动系统光标** |
| `core/input.py:505-535` `Keyboard.wheel` | `SetCursorPos` → `sleep(0.05)` → 每格 `sleep(0.12)` → 末尾 `0.2`，`finally` 还原 | 任何滚轮操作（典藏外观列表等），最长可占用约 **0.05+0.12×N+0.2 s** |
| `core/input.py:356-369` `cursor_is_locked` | 主动把光标挪 40px 再挪回，用来判断游戏是否锁光标 | `engine.py:204` 启动自检时，**用户会看到光标抖一下** |
| `core/engine.py:200` `release_cursor_lock` | `ClipCursor(None)` 清掉游戏的裁剪 | 启动自检；把游戏设的光标约束直接拆掉 |

### 1.2 抢前台 / 抢焦点（不是鼠标，但一样打断用户）

| 位置 | 行为 | 触发条件 |
|---|---|---|
| `core/input.py:270-309` `ensure_foreground` | `AttachThreadInput` 挂到前台线程 + `SW_RESTORE` + `BringWindowToTop` + `SetForegroundWindow`，再 `sleep(gap)` | 每次点击前 `expect_foreground` 不满足；`engine.py:302` 走这条 |

### 1.3 只读、不占用（确认不是根因）

- `core/hotkey.py` 的低级键盘钩子结尾调 `CallNextHookEx`，**不吞键**。
- 全工程没有 `WH_MOUSE_LL`、没有 `SetCapture`/`ReleaseCapture`、没有 `SendInput` 之外的设备独占、没有 raw input 注册。
- 结论：**不存在「物理鼠标被锁住不能用」的代码路径**。

---

## 2 到底是哪种抢？——证据

判定方法：分别验证「设备是否被独占」与「光标是否被挪走」。

1. **设备没被锁**：`GetClipCursor` 为空/全屏、`SetCursorPos` 能改光标（游戏不在前台时实测「生效=True」）、无鼠标钩子、无 capture → 物理鼠标事件一直正常进栈。
2. **光标被挪走**：legacy 单击实测占用 120.9ms；连点 8 次 966.6ms，其中 96.8% 的采样点偏离原位，最大 472px，最长连续 1370ms。用户感知到的「鼠标被抢」就是这个区间。
3. **叠加的第二层原因（本机实测）**：当目标/前台是 **Unity 高完整性进程** 时（`fg='明日方舟' class=UnityWndClass`），Medium 权限进程调 `SetCursorPos` **返回 0、`GetLastError=0`、光标不动** —— 这是 UIPI 拦截，和 `input.py:741-743` 记录的「高完整性前台 → 四种注入全失效」是同一件事。
   - 所以：不提权时点不动、光标也没动；**一提权就真的把光标钉在目标上**。这解释了为什么用户看到的行为像「抢鼠标」，而日志里又经常是「点了没反应」。

**结论：属于「系统光标被挪走 + 被短暂占用」类，不是「物理鼠标被锁死」类。** 修法完全不同——不需要解设备独占，只需要把点击路径改成不碰光标（或把占用窗口压到毫秒级）。

---

## 3 零干扰点击路径骨架（已落代码）

新增 [core/nocursor.py](file:///C:/Users/26346/Desktop/project/dafors/core/nocursor.py)，三条策略可独立降级：

```
message  只 PostMessage(WM_MOUSEMOVE/WM_LBUTTONDOWN/WM_LBUTTONUP)，全程零 SetCursorPos
borrow   SetCursorPos 一次 → 投消息 → 立即还原（停留窗口可配，默认 2ms），finally 必还原
driver   Interception 绝对注入（可选增强；缺驱动抛 DriverUnavailable，不静默降级）
```

结构性保证：

- `message` 分支里**没有任何** `SetCursorPos` / 注入调用（可静态审计）。
- `borrow`/`driver` 都用 `finally` 还原；按下态用 `pressed` 标记，异常必补 up。
- 驱动缺失 → `DriverUnavailable`（明确报错），调用方可自行降级，不存在「悄悄换路径」。
- `PostMessageW` 返回 0 时抛 `InputError` 并带上 `GetLastError`（5 = UIPI，附中文提示），不再静默。

### 3.1 自证结果（`python -m core.nocursor`）

```
{'n': 200, 'hits': 200, 'per_click_ms': 0.025,
 'cursor_before': (903, 454), 'cursor_after': (903, 454),
 'displacement_net': 0, 'samples': 41, 'samples_at_target': 0}
```

- 判据 1：窗口收到 200/200 次 down（消息确实被受理）。
- 判据 2：200Hz 采样里 **0 次**光标落到目标点（不是「前后各读一次」，用户自己动鼠标也不会误判）。
- 单次 3 条消息 0.025ms，比 legacy 的 0.13s 固定开销小 3 个数量级。

### 3.2 三条策略的取舍

| 策略 | 干扰 | 适用于 | 已知代价 |
|---|---|---|---|
| `message` | 零 | 走窗口消息的 UI（含大部分 Unity/UE 的 Slate/UGUI 消息层） | Raw Input 消费者完全收不到（实测 `WM_INPUT=0`），必须真机验证 |
| `borrow` | 光标被借 2ms~ 数十 ms | `message` 被忽略、但游戏按下时读 `GetCursorPos` | 与游戏抢光标；且在 UIPI 下 `SetCursorPos` 直接失败 |
| `driver` | 光标被真实移动 | 前两者都无效、且用户愿意接受驱动 | 需装内核驱动/重启/提权；反作弊可能封；注入在途时还原会被覆盖 |

---

## 4 驱动现状（已核对，非「没装」）

- `C:\Windows\System32\drivers\keyboard.sys` / `mouse.sys` 已存在且服务 `keyboard`/`mouse` 处于 **RUNNING**（Interception 以 `keyboard`/`mouse` 作为 kbdclass/mouclass 上位过滤安装，文件名不是 `interception.sys`）。
- `install-interception.exe /install` 再跑会失败（文件被占用），属预期。
- `core/interception.py` 的 `driver_ready()` 返回 **True**；加 `probe=True` 后发一次零位移相对事件，`send_mouse` **受理成功**（`(True, '')`），说明不是「装了没重启」也不是「被拦」。
- `core/assets/interception.dll` 已在仓库内；已删掉 `_dll_candidates()` 里写死的 `C:\Users\26346\Downloads\...` 兜底，现在只按 `__file__` / `_MEIPASS` 相对解析。
- 驱动注入实测：绝对坐标注入后 `GetCursorPos` 直接跳到目标；**立刻还原会被在途事件覆盖**（独立两次复现），需要 ≥4ms settle 再还原。
- 模块文档里「驱动注入不需要动真实光标」这句结论是错的，已改掉（实测会动，见 §0 第 4 条）。

### 4.1 本轮环境阻塞（需上报）

- 当前前台是 **Unity 高完整性进程**（`明日方舟 / UnityWndClass`），Medium 权限下 `SetCursorPos` 返回 0、`GetLastError=0`、光标不动 → `borrow` 策略在本机此刻**无法验证**（直接抛 `InputError`）。这不是代码问题，是 UIPI；要验证必须换一个前台窗口，或把被测进程提权（提权又会真抢光标，与硬约束冲突）。

---

## 5 与架构约束的冲突（分发 / 卡密）

1. **驱动只能做「可选增强」**：`.sys` 要管理员安装 + 通常要重启 + 签名/杀软/反作弊三重风险，不能是唯一路径。骨架已按此设计：默认 `message`，驱动缺失就抛 `DriverUnavailable` 让上层回落，激活/启动流程不依赖驱动。
2. **卡密（后续做）**：关键校验不能放纯 Python 明文；PyInstaller 产物可还原 `.pyc` 再反编译。建议核心判定放服务端，本地只做「签名过的许可文件 + 离线宽限」，用 Nuitka 或 Cython 编译校验桩。

---

## 6 待办与未验证（本轮不编数）

- [x] `core/interception.py` 里写死本机路径的兜底候选已删（只保留 `__file__` 相对解析）。
- [x] 驱动可用性改为 `driver_ready(probe=True)`：真发一次零位移事件才算就绪。
- [ ] 三条路径「调用→入栈」端到端延迟 p50/p95 基准（等并行分支 A 的数据，不重复测）。
- [ ] PostMessage 在**真游戏**上是否受理（本地窗口只能证明「消息进队列」，证明不了游戏逻辑响应）。
- [ ] `borrow` 策略在游戏前台时的最小可用停留时间（本机当前被 UIPI 挡住，测不到）。
- [ ] 虚拟 HID（ViGEmBus/HIDAPI）：**先验判断仍会移动共享光标**（Windows 把所有鼠标合并成一个光标），待实测确认后再决定是否放弃。
- [ ] 直接输入（DirectInput）注入路径，待评估。
- [ ] 倒计时 T0 对齐 / 延迟补偿 / 自旋卡点 / ≥200 次偏差分布（等 B/C 分支）。
- [ ] 关键结论双路复核与 >15% 差异说明（本轮仅「PostMessage 不进 Raw Input」做了双路：`WM_INPUT` 计数 + `WH_MOUSE_LL` 钩子，两路一致）。

---

## 7 对用户桌面的干扰记录（按用户要求补记，永久约束的由来）

### 7.1 做了什么、持续多久

| # | 动作 | 来源 | 持续时间 | 用户可见表现 | 处置 |
|---|---|---|---|---|---|
| 1 | 探针进程 `ic_probe2.py` / `bench_input.py` 在**默认桌面**建白色测试窗口，并把系统光标反复钉到窗口点 | 本轮为验证点击路径临时写的探针 | 约 04:10–04:28，**≈18 分钟** | 屏幕左上角白色窗口、物理鼠标被反复拽走 | 两个进程（PID 35668 / 21372）父进程已死成为孤儿且高完整性，`Stop-Process` 首次报「没有运行的实例」、提权 `$_.Kill()` 也 Access Denied；重试 `Stop-Process` 后成功终止，白窗随之消失。另有 PID 14192 为 WMI 幽灵条目，`taskkill` 报无实例，实际已无进程 |
| 2 | `cursor_is_locked()` 启动自检：把光标挪 40px 再挪回 | `core/input.py`，由 `core/engine.py` 启动流程调用 | 每次启动 <50ms | 每次启动光标抖一下 | **已彻底删除**：`core/input.py` 删除函数本体，`core/engine.py` 删除调用；`tests/test_cursor_lock.py` 改为断言该 API 不存在 |
| 3 | legacy 点击路径 `SetCursorPos(目标) → sleep 40ms → 按下 60ms → 还原` | `core/input.py` `_click_messages` | 单次 120.9ms；连点 8 次累计 966.6ms | 光标被按在目标上；96.8% 采样点偏离原位，最大位移 472px，最长连续偏离 ≈1370ms | 默认路径改为零干扰的纯 `PostMessage`（`core/nocursor.py`）；legacy 仅作显式兜底 |
| 4 | 滚轮 `SetCursorPos → sleep 0.05 → 每格 0.12 → 末尾 0.2` | `core/input.py` `Keyboard.wheel` | 每次 0.05+0.12×N+0.2s | 滚动时光标被占 | 保留语义（游戏按光标位置分流），但幅度/停留改为可配，测试用 spy 拦截 |
| 5 | `nocursor.selftest` 原在默认桌面建窗并点击 200 次 | `core/nocursor.py` | 每次自测数十秒 | 白窗 + 光标被拽 | 改为 `CreateDesktopW("SjzNoCursorTestDesk")` 独立桌面 + `SetThreadDesktop` + `CloseDesktop`；自测结果 `isolated_desktop=True`、`hits=200/200`、光标前后一致、`samples_at_target=0` |
| 6 | 并行会话遗留的 pytest 全量测试（`tests -k "records or config or countdown_flow"`）仍在跑 | 另一会话的 shell（PID 25608）+ pytest（PID 5020） | 04:46:21 启动，04:51 被我终止 | 无窗口，但会加载真实输入模块 | 子进程与父 shell 已 `Stop-Process -Force` 终止 |

### 7.2 当前状态核验（04:51:25 起）

- 进程：`dafors/.venv` 下已无任何 python/pytest 进程；仅剩用户自己的 `Token 用量看板` 与 `tok/trae_capture`（非本项目）。
- 窗口：可见窗口枚举（`MainWindowTitle != ""`）中无本项目创建的窗口；左上角白窗已消失。
- 光标：5 秒只读采样（`GetCursorPos`，**不写**）971 次，`moves=0`，位置恒为 `(944, 928)`，与采样开始一致。
- 临时文件：临时工作目录里 40+ 个探针脚本与日志（`_cursor_probe` / `_llhook_probe` / `_drv_probe` / `_accept_probe` / `_diag_observe` / `_kill_admin*` / `_fulltest_2x.log` 等）已全部删除；项目内 `_tmp_market.diff` 已删。

### 7.3 以后怎么避免（已落为硬规则）

1. **窗口**：一律 `CreateDesktopW` 独立桌面 + `SetThreadDesktop`，或 `WS_OVERLAPPEDWINDOW`（不含 `WS_VISIBLE`）的离屏窗口；绝不在默认桌面建可见窗口。
2. **光标**：代码层阶段禁止任何写操作（`SetCursorPos` / `SendInput` / `Interception send`）；只读 `GetCursorPos` 允许。判断「光标是否被锁」不再用挪动探测，只靠点击失败报错链暴露。
3. **测试**：凡调用 `Mouse.click` / `Keyboard.wheel` 必须把 `_user32` 与 `interception` 全量 monkeypatch/spy；真实注入冒烟测试加 `SJZ_LIVE=1` 双重闸门（`tests/test_input_interception.py` 已改）。
4. **探针**：只放系统临时目录，跑完同轮删除；不在项目根留 `_probe*` / `_tmp_*`。
5. **并行会话**：共享同一 `dafors` 工程时，启动任何全量测试前先确认没有别的会话在跑同一套测试（本轮已因并发启动导致两个 pytest 同时加载输入模块）。

### 7.4 受影响的结论

- `tests` 全量 ×2 的回归被本次紧急停止打断（第一遍已在 04:46 启动、按指令终止），本轮未重跑；重跑必须在上述隔离模式下进行，不恢复「真实桌面探针」。
- `_tmp_sim_numbers.json` / `_tmp_fire_numbers.json` 两份仿真与卡点进程实测数据保留在项目根作为数据来源，报告写入后可删。
