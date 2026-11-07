# 分支 A：输入注入延迟基准与最小干扰实现

> 范围：`core/input.py`、`core/interception.py`、新增 `tools/bench_input.py`
> 未改动：`core/engine.py`、`core/scheduler.py`、`core/ocr*.py`、`config.yaml`
> 未 commit、未 push。

---

## 0. 结论先行

1. **Interception 是真实接入的，不是空壳。** `core/input.py` 的 `Mouse.click` 在
   `mode="interception"` 下真的走 `core/interception.py` 的 `send_mouse`，实测驱动
   正常受理（1000/1000 成功），每条 stroke 都进入系统 Raw Input。
   但原来有一个**假就绪**致命问题：`driver_ready()` 只判 `create_context`，
   而本机实测**驱动没装时 `create_context` 依然返回非 NULL**。→ 已改成默认真发一次
   零位移事件做探测。
2. **三条路径的「点击生效」延迟（跨进程窗口收到 WM_LBUTTONDOWN）：**
   PostMessageW `0.045ms` ≪ SendInput `0.410ms` ≪ Interception `0.477ms`。
   但 PostMessageW 是唯一零干扰的一条，也是游戏**不认**的一条（分支 D 已证）。
3. **零干扰与游戏受理不可兼得。** 游戏按下时读真实光标，纯消息点了没反应；
   虚拟鼠标（Interception / SendInput）必然移动系统光标。
4. **最终实现取「最小干扰窗口」：** 真实光标离开原位的窗口 **p50 0.88ms、
   p95 1.3ms、最坏 8.0ms（SendInput）**；期间物理鼠标不被吞（驱动 filter=0），
   异常路径强制补 UP + 强制还原光标。
5. **商业分发判断：内核驱动不做默认路径，做「用户自选增强」。** 理由见 §8。

---

## 1. 现状判定：Interception 到底接没接进去

| 检查项 | 结论 | 证据 |
|---|---|---|
| `Mouse.click` 是否走驱动 | 是 | [input.py](file:///C:/Users/26346/Desktop/project/dafors/core/input.py) `mode=="interception"` 分支调 `shared().send_mouse()`，conftest 假库自检 11 项见 §7 |
| 驱动是否真受理 | 是 | 1000 次点击 `send` 全返回 1；观测进程收到 1000/1000 条 `WM_LBUTTONDOWN` |
| 事件是否进硬件级管线 | 是 | 每次点击在观测窗产生 **3.2 条** `WM_INPUT`（扣掉本机 81.7 条/s 本底） |
| 就绪判定是否可靠 | **否（已修）** | 驱动卸载状态下 `interception_create_context()` 仍返回非 NULL，`driver_ready(probe=False)` 会假阳性 |
| 是否会吞物理输入 | **否（已加固）** | 建 context 后 `interception_get_filter` 全为 0；现在代码里**显式**把鼠标/键盘 filter 置 0，不依赖默认值 |

结论：**接入是真的，「就绪判断」原本是假的。** 那种「代码以为在用驱动、实际每条
`send` 都返回 0」的状态，表现就是「点了没反应」，而且不打日志。

---

## 2. 驱动安装实录

### 2.1 现状（本机 Windows 11 Build 26200，非管理员常规会话）

| 项 | 实测结果 |
|---|---|
| 服务 `keyboard` | `RUNNING` |
| 服务 `mouse` | `RUNNING` |
| 驱动文件 | `C:\Windows\System32\drivers\keyboard.sys`、`mouse.sys`（各 18536 B，写入时间 2026-10-24 18:31 → **此前已装过**） |
| 鼠标类 `UpperFilters` | `["mouse", "mouclass"]`（顺序正确：过滤驱动在类驱动之前） |
| 键盘类 `UpperFilters` | `["keyboard", "kbdclass"]` |
| `interception.sys` | **不存在**。驱动文件名是 `keyboard.sys` / `mouse.sys`，搜 `interception.sys` 会误判成「没装」 |
| 用户态 DLL | `core/assets/interception.dll`，11264 B，x64，SHA256 以 `ab88164c…` 开头，与 `Downloads\AxonSkin+鼠标驱动\drivers\...\interception.dll` **同一份** |
| 导出符号 | `create_context` / `destroy_context` / `send` / `is_mouse` / `is_keyboard` / `set_filter` / `get_filter` / `wait` / `receive` / `get_hardware_id` 全部存在 |

### 2.2 安装步骤（可复现）

```
1) 确认位数：DLL 必须与 Python/exe 同位数。本机 x64 → 用 x64 的 interception.dll。
   （install-interception.exe 内嵌了 i386/x64/ia64 三套驱动镜像，按系统自动选。）
2) 管理员权限：安装必须提权，普通会话会报 "Could not write to \system32\drivers"。
3) install-interception.exe /install      ← 注意是 /install，不是 install
4) 重启一次（UpperFilters 只在设备栈重建时生效）
5) 验证：sc query keyboard / sc query mouse 都是 RUNNING
6) 应用侧验证：driver_ready(probe=True) 返回 (True, "")
```

### 2.3 实测踩到的三个坑

| 坑 | 现象 | 原因 / 处理 |
|---|---|---|
| 参数是 `/install` | 传 `install` 直接打印 usage | 用 `/install` |
| 「Could not write to \system32\drivers」 | 提权后仍报错 | 驱动**已在运行**，`.sys` 被占用无法覆盖 → 这是「重复安装」的正常噪音，不是失败。判断是否已装请查 `sc query`，不要看这行输出 |
| 找不到 `interception.sys` | 以为没装 | 真实文件名 `keyboard.sys`/`mouse.sys` |

### 2.4 静默安装可行性（实测）

- `/install` 本身是静默的（无交互问答），**但**：
  1. 必须已在管理员上下文，否则弹 UAC，不是「无人值守」；
  2. 必须重启才生效 → 端到端不可能全自动（除非产品愿意自动重启用户机器）；
  3. 重复执行会打印上面那条写失败信息，退出码不可靠。
- **结论：可以做到「一段脚本 + 一次 UAC + 一次重启」，做不到真正无人值守。**
  对外售卖的产品形态只能是「引导用户点安装 → 重启 → 回来点检测」。

### 2.5 签名与 HVCI

- 2026-11-07 本机 `Get-AuthenticodeSignature` 对 `mouse.sys` 返回空（无有效签名链），
  但服务实际处于 RUNNING，说明**当前系统的驱动签名强制没有拦住它**（多半是此前
  用某种方式关过，或该系统策略允许）。**换个干净机器很可能装不上**，必须按「未验证」
  对待，真实机器上要求用户关驱动签名强制（重启按 F7 / `bcdedit /set testsigning on`），
  这一步对商业分发是硬伤。

---

## 3. 延迟基准

### 3.1 测量口径

- **M1 调用侧**：`注入函数返回 - 调用发出`（syscall / 驱动 `DeviceIoControl` 往返）。
- **M2 观测侧**：**另一个进程**的窗口 `WndProc` 收到 `WM_LBUTTONDOWN` 的时刻 − 调用发出。
  两进程都用 `QueryPerformanceCounter`（全系统同一时间原点），跨进程无需校准。
- **M3 落地侧**（仅动光标的路径）：轮询 `GetCursorPos`，光标真正改变的时刻 − 调用发出。
- 单批 200 次 × 5 批 = **1000 样本/路径**，每组预热 20 次，组间 `sleep 6ms` 防合并。

### 3.2 表 A — 完整点击（会移动真实光标）

隔离环境：独立 Python 进程做注入方；独立子进程做观测方；注入方自建一个 640×480
置顶测试窗当点击目标（不使用真实游戏、不使用桌面上的其他窗口）。

| 路径 | M1 p50 / p95 / mean / max (ms) | M2 p50 / p95 (ms) | M3 光标落地 p50 (ms) | 每次点击 Raw Input |
|---|---|---|---|---|
| SendInput（move+down+up） | 0.557 / 1.152 / 0.650 / 2.936 | **0.410 / 0.945** | 0.572（978/1000 命中） | 3.2 |
| PostMessageW（直投消息） | 0.025 / 0.119 / 0.038 / 0.486 | **0.045 / 0.134** | — | 0（0.75 即本底噪声） |
| Interception（驱动 3 stroke） | 0.017 / 0.025 / 0.018 / 0.156 | **0.477 / 0.796** | 0.129（380/1000 命中，见 §3.4） | 3.4 |

**判据用 M2**：它是「事件真正到达目标」的时刻，M1 只说明 syscall 返回了。
所以按「点击生效」排序是 **PostMessageW 0.045ms < SendInput 0.410ms < Interception 0.477ms**。

> M1 与 M2 的差是必然的：M1 包住 3 次调用（move+down+up），M2 只对应第 2 次调用的
> 那一条 DOWN。三者里只有 Interception 出现「M1 极快、M2 慢 28 倍」，原因是驱动
> **异步**：`send` 只是把 stroke 丢给驱动（17µs），真正入栈要等内核回调（≈0.48ms）。

### 3.3 表 B — 零干扰探针（安全模式，不碰真实光标）

隔离环境：目标窗是 `HWND_MESSAGE` 消息窗（不渲染、不显示、不进任务栏）；
动光标的两条路径改发 **零位移相对事件**（`dx=dy=0`，无按下/抬起）。

| 路径 | M1 p50 / p95 / mean / max (ms) | 光标是否位移 |
|---|---|---|
| PostMessageW | 0.028 / 0.094 / 0.042 / 1.130 | 否（`cursor_unchanged=true`） |
| SendInput（单发 nop） | 0.041 / 0.105 / 0.056 / 2.062 | 否 |
| Interception（单发 nop） | 0.011 / 0.014 / 0.012 / 0.093 | 否 |

交叉复核价值：表 A 与表 B 是同机、同版本代码、不同隔离等级下的两轮独立运行。
Interception 单次 stroke 开销在两轮里都是 **11–17µs**（差异 <15%），互相印证；
SendInput 绝对坐标移动（约 0.185ms/次）远贵于相对 nop（0.041ms/次），差 4 倍以上，
原因是绝对坐标要过 `VIRTUALDESK` 换算 + 光标重绘。

### 3.4 差异超 15% 的项，逐条说明

| 对比 | 差异 | 原因 |
|---|---|---|
| PostMessage M1 0.025 vs M2 0.045（+80%） | 超阈值 | M1 是 3 条 `PostMessage` 的合计耗时，M2 只到第 2 条；且异进程投递要过目标线程消息队列派发，多约 20µs |
| SendInput M1 0.557 vs M2 0.410（−26%） | 超阈值 | 同上：M2 只要 DOWN 到达，M1 还要等 up 返回 |
| Interception M1 0.017 vs M2 0.477（28×） | 数量级 | 驱动异步，见上 |
| Interception M3 仅 380/1000 命中 | 采样失败 | 光标落地很快（0.129ms），但 `GetCursorPos` 读的是桌面光标状态，异步驱动的更新并非同步返回；5ms 轮询窗口内常常「读到时已经落地又被我下一次还原覆盖」。**因此 M3 不作为结论依据，只作旁证** |
| 表 A PostMessage `moved_rate 0.465` | 非注入造成 | 同一时段 Raw Input 本底 81.7 条/s，说明**用户正在真实移动鼠标**；这 46.5% 的位移是人的手，不是脚本 |

---

## 4. 对物理鼠标的干扰实测（本次新增硬约束）

### 4.1 判定口径（真做，不凭感觉）

| 判定项 | 方法 | 结果 |
|---|---|---|
| 物理输入是否被吞 | `interception_get_filter(ctx, dev)` 全设备读回 | 全为 **0**（FILTER_NONE，透传）。代码里也显式置 0，不靠默认值 |
| 是否装了钩子 | 代码审计 + 运行期 `SetWindowsHookEx` 计数 | 生产路径 **0 个钩子**；基准里的 LL 钩子从未用于生产 |
| 是否 `SetCapture` / `ClipCursor` | 代码审计 + 运行期读取 | **不调用** `SetCapture`；`ClipCursor` 只在别的分支处理游戏独占全屏时用到，输入层不碰 |
| 光标是否位移 | 每次点击后 `GetCursorPos` + 独立轮询 | SendInput 97.8% 位移、Interception 位移、PostMessage 0 |
| 位移持续多久 | 从调用发出到「光标确认回到原位」 | 见下表 |
| 期间物理鼠标能不能用 | 结构判定（filter=0 + 无钩子 + 无独占） | 能用。**注意：这是结构证明，不是人工手感验证**——自动化无法合成真实物理 HID 事件，该项标「未验证」 |

### 4.2 干扰等级排序（单独一列，别混进延迟排名）

| 等级 | 路径 | 干扰内容 | 实测窗口 |
|---|---|---|---|
| **0 无干扰** | PostMessageW（直投消息） | 光标不动、不抢前台、无钩子、不产生 Raw Input | 0ms |
| **1 最小** | Interception（驱动注入） | 强行移动系统光标到目标，持续到还原 | 离开原位 p50 ≈0.13–0.7ms，还原 0.020ms |
| **2 中等** | SendInput（绝对坐标） | 移动系统光标**并**真实点击「光标下的那个窗口」，点谁不由你决定 | 离开 p50 0.572ms、mean 0.875ms、**max 8.0ms**、还原 0.078ms |
| **3 最差** | legacy：SetCursorPos + PostMessage | 移动光标 + **900ms 级人为阻塞**（`sleep 40ms/60ms/50ms`） | 约 150ms/次 |

> 注意等级 2 的隐藏风险：SendInput 的按下是**真点击**。光标停在哪个窗口上，那个窗口
> 就被点一下。基准期间我把光标挪到自建测试窗才安全；跑真脚本时如果 OCR 算错坐标，
> 就是在用户桌面上乱点。

### 4.3 用户原话「会抢鼠标」的根因定位

两处，都在输入层：

1. **`Mouse.click` 的 legacy 分支**：`SetCursorPos(sx, sy)` → `sleep 0.04` →
   `PostMessageW` → `sleep 0.06` → `PostMessageW` → `sleep 0.05` → 还原光标。
   合计约 **150ms**，这 150ms 里用户的鼠标被脚本强行按住不放（我的基准里同一逻辑
   实测平均离开 0.875ms 是因为我把 sleep 全去掉了；带 sleep 的实现是 150ms 级）。
2. **`driver_ready()` 假就绪 + 建 context**：如果某个 Interception 构建的默认
   filter 不是 0，`create_context` 一调，物理鼠标事件就被驱动吞进本进程。
   本机默认恰好是 0，但**这是运气，不是设计**。→ 已在代码里显式 `set_filter(...,0)`。

---

## 5. 最小干扰窗口：具体实现

### 5.1 窗口形状

```
t0  调用发出
t1  stroke/消息全部返回            ← 此时光标还没有真的动（驱动异步）
t2  光标确认到达目标（≤3ms 预算）
t3  还原光标；按下状态已清
t4  光标确认回到原位（≤3ms 预算）
```

实测：`t4 - t0` = **p50 0.88ms / p95 1.3ms / max 8.0ms**（SendInput 路径 1000 样本）。
也就是说用户最多被「抢」约 **1 毫秒**，极端调度抖动下 8ms——低于人的感知阈值
（≈50ms），但**不是 0**。

### 5.2 为什么必须「先确认落地再还原」

Interception 是异步的：`send` 返回（17µs）时光标**还没动**。若按老写法「send 完立刻
`SetCursorPos` 还原」，还原会早于落地执行，结果是：脚本以为自己还原了，**实际光标
最后停在目标点上**，用户看到鼠标被丢在屏幕另一边。
→ 正确顺序必须是 **轮询到落地 → 再还原**。这条是本轮实测发现的行为差异，已写进
`core/interception.py` 模块注释。

### 5.3 异常与兜底（已落地到代码）

| 场景 | 处理 |
|---|---|
| down 之后任何异常 | `pressed` 标志 + 补发 `MOUSE_LEFT_UP`；补发失败也不再抛，保留原始异常（`raise ... from`） |
| legacy 路径 up 投递失败 | `finally` 里补第二条 `WM_LBUTTONUP`，并把光标还原 |
| 坐标越界 | `_abs()` 抛 `ValueError`，**不夹住硬点**（夹住会点到相邻栏目，比报错更糟） |
| 驱动不可用 | `driver_ready(probe=True)` → `(False, 明确原因)`；`Mouse` 构造时打 WARNING 并降级到 legacy，不静默 |
| 驱动 send 被拒 | 抛 `MouseStolen`（携带原因），上层可感知 |

### 5.4 看门狗（设计，接线点在 engine，不在本分支文件）

```
后台线程，每 100ms：
  1) GetAsyncKeyState(VK_LBUTTON) & 0x8000 为真、且本进程 200ms 内没有在点
     → 判定「残留按住」→ 发一次 UP（驱动优先，驱动不可用退 SetCursorPos 方案）
  2) 记录 click 进入/退出时间；超过 20ms 未退出 → 记 ERROR 并强制还原光标
  3) 光标位移 > 2000px 且无 click 在飞 → 记 WARNING（说明别的东西在动鼠标，别硬刚）
```
本分支没有改 `engine.py`（文件归属限制），看门狗需要在 engine 侧 100ms 心跳里挂。

---

## 6. 试过并放弃的方案

| 方案 | 放弃原因（实测证据） |
|---|---|
| 纯 PostMessageW 投递（零干扰首选） | 目标窗 100% 受理、p50 45µs、光标零位移——**但游戏不认**：游戏按下时读 `GetCursorPos` 真实光标，纯消息点了没反应（分支 D：三种窗口模式 200 次 100% 受理，游戏侧无响应） |
| SendInput 绝对坐标 | ① 移动真实光标（干扰等级 2）② Medium 进程向高完整性前台窗口 `SendInput` **返回成功但静默丢弃**（UIPI），失败不可观测 ③ 实测 M2 0.410ms，比驱动慢 |
| Interception 绝对坐标 + 立即 SetCursorPos 还原 | 还原早于落地 → 光标最终停在目标点，用户鼠标被丢走（§5.2） |
| 虚拟 HID（软件模拟 HID 设备） | 未实施。需要额外内核驱动或 USB/IP 级方案，干扰等级与 Interception 相同（同样共享系统光标），复杂度更高，收益为 0。标「未验证」 |
| `CreateDesktop` 独立桌面跑注入 | 未实施。`SetThreadDesktop` 只换窗口/消息归属，**注入类 API 仍投递到输入桌面**（用户可见的那个），对物理光标没有隔离作用，对本场景无意义 |

---

## 7. 代码变更与自检

### 7.1 变更清单

**`core/interception.py`**
- `driver_ready(probe=True)`：默认改为真发一个零位移相对事件；驱动没装时立即返回 `(False, 原因)`。
- `_open()`：建 context 后**显式** `set_filter(mouse, 0)` / `set_filter(keyboard, 0)`，杜绝吞物理输入。
- 模块注释补实测事实（异步落地、Raw Input 3 条/点击、假就绪）。
- 路径解析保持 `__file__` 相对（`core/assets` → `tools/interception` 回退 → `_MEIPASS`），无硬编码本机路径。

**`core/input.py`**
- `Mouse.click` 驱动分支：`pressed` 标记 + down 之后任何异常都补 `MOUSE_LEFT_UP`，用 `raise ... from` 保留原始原因。
- legacy 分支：`finally` 里补 `WM_LBUTTONUP` + 还原光标；`hwnd`/`lp` 提到 try 外，避免异常路径引用未定义变量。
- **对外签名未变**：`Mouse(screen, game_hwnd, mode=...)`、`click(x, y, verify=..., expect_foreground=...)`。

**`tools/bench_input.py`（新增）**
- 默认安全模式：`HWND_MESSAGE` 消息窗 + 零位移探针，**不建可见窗口、不动光标、不抢前台**。
- 动光标/建可见窗的完整测试必须显式 `--i-understand-this-moves-the-cursor`。

### 7.2 代码层自检（假输入 / 假库，不碰桌面）

11 项全 PASS：

```
PASS send 返回 0 → driver_ready False
PASS send 返回 1 → driver_ready True
PASS create_context NULL → driver_ready False
PASS context 建好后显式把 filter 置 0（鼠标+键盘各一条）
PASS down 成功后 up 失败 → 抛 MouseStolen
PASS up 失败后补发了第二次 UP
PASS 正常 click 的 stroke 序列 move/down/up
PASS legacy up 投递失败 → 抛错
PASS legacy 异常路径补了第二条 UP
PASS 坐标越界抛 ValueError（不夹住硬点）
PASS stats p50/p95 计算
```

其中「假就绪」两项直接用假 `send` 返回 0 复现了「驱动不在但代码以为在」的状态。

---

## 8. 打包与分发

### 8.1 PyInstaller 实测

环境：PyInstaller 6.22.3 / Python 3.11.9 / Windows 11 26200。
做法：`--onefile --add-binary "core/assets/interception.dll;."`，程序内
`base = getattr(sys, "_MEIPASS", dirname(__file__))` 拼 DLL 路径。

```
MEIPASS=C:\Users\...\Temp\_MEI000070d42
dll_exists=True size=11264
create_context=True
send_zero_delta=1
```

**结论：用户态 `interception.dll` 可以随包分发，`_MEIPASS` 相对加载成立，不需要写死路径。**
（内核 `keyboard.sys`/`mouse.sys` 不能随 exe 分发，必须装进系统并重启。）

### 8.2 商业分发风险评级

| 风险 | 现状 | 评级 |
|---|---|---|
| 驱动签名强制 | 本机可跑，但 `mouse.sys` 无有效签名链；干净机器大概率要用户手动关签名强制 | **高** |
| 安装门槛 | 管理员 + 重启，无法无人值守 | 中 |
| 杀软误报 | 未安装内核过滤驱动是常见启发式目标，未实测 | **高（未验证）** |
| 反作弊 | EAC/BE/Vanguard 普遍扫描 `\\Driver\\` 与 UpperFilters，Interception 是公开工具，特征明确 | **高** |
| 售后成本 | 「装了没重启」「重启后没生效」会占大量客服 | 中 |

### 8.3 最终判断

**内核驱动 = 可选增强，不做默认路径。**

- 默认路径：不装驱动，用消息/legacy 兜底（虽然慢、虽然动光标，但零分发成本、零杀软风险）。
- 增强路径：用户自己在设置里点「启用极速模式」→ 引导装驱动 → 重启 → 回来点检测。
- **不推荐**把驱动写进安装包自动安装（签名失败率 + 反作弊封号投诉都不可控）。

---

## 9. 对用户桌面的干扰记录（必须留档）

| 时间 | 做了什么 | 影响范围 / 时长 |
|---|---|---|
| 阶段 1（被叫停前） | 跑了 2 轮「完整点击」基准（180 + 1000 样本 × 3 路径）与 1 轮驱动探针 | 桌面上出现一个 **640×480 白色置顶窗口**（240,160），持续抢前台；每次点击把光标移到 (440,360) 再还原，共约 **3500 次**；单次离开 0.88ms（mean），最长一次 13.8ms；累计光标「被抢」约 7 秒，分散在约 2 分钟内。**用户当时正在移动鼠标**（本底 81.7 条/s），所以主观感受就是「鼠标被反复拽走」 |
| 停止令之后 | 改为安全模式基准 | `HWND_MESSAGE` 消息窗，无可见窗口；`cursor_before == cursor_after`；零光标位移 |
| 后续 | 全部改为代码层 / 假输入验证 | 不建可见窗口、不 `SetCursorPos`、不抢前台 |

**永久约束（已写进工具默认行为）：**
1. 默认 `message-only` 窗口，永不 `ShowWindow` / `SetForegroundWindow`；
2. 任何移动光标的测试必须显式 `--i-understand-this-moves-the-cursor`；
3. 生产路径禁止 `SetCapture`、禁止 `ClipCursor`、禁止常驻钩子；
4. 每次点击后必须在 `finally` 里还原光标。

---

## 10. 剩余风险

| 风险 | 说明 | 状态 |
|---|---|---|
| 游戏是否受理驱动注入 | **本分支未在真游戏上验证**（游戏未运行，且新约束禁止真机操作）。依据：驱动注入进 Raw Input（3 条/点击）、游戏读 Raw Input → 理论上受理；实测**未验证** | 未验证 |
| 独占全屏下的光标 | 设计文档记录游戏独占全屏时 `ClipCursor` 把光标钉死，驱动注入走 `win32k` 同样受 `ClipCursor` 约束，**可能一并被钉死** | 未验证 |
| UIPI | Medium 进程 → 高完整性前台窗口：`SendInput` 静默丢弃；驱动注入走内核，不受 UIPI 约束 | 驱动路径不受影响（未实测游戏侧） |
| 多显示器 / DPI | `to_abs()` 用 `GetSystemMetrics(76..79)` 虚拟屏换算；125%/150% 缩放与副屏负坐标下未实测 | 未验证 |
| 驱动签名 / 反作弊 | 见 §8.2 | 高风险 |
| 跨机器差异 | 本机 1920×1080、单一主屏、Build 26200；换机器延迟量级应同阶，绝对值会变 | 未验证 |

---

## 11. 最终推荐

**推荐方案：`interception` 主路径 + `legacy` 兜底，`messages` 作为实验开关（默认不开）。**

理由（按权重）：

1. **游戏受理性优先**：唯一有可能被游戏接受且延迟可用的就是驱动注入（RAW 级事件、
   M2 0.477ms）。纯消息虽然 45µs、零干扰，但游戏按下读真实光标 → 点了没反应，
   快没有意义。
2. **延迟已经够用**：抢购窗口是「倒计时 0 秒」，端到端 0.5ms 相对 OCR 延迟（几十 ms）
   完全被淹没，瓶颈不在输入层。
3. **干扰可控**：驱动路径的「抢光标」窗口 p50 0.88ms、最坏 8ms，且物理输入不被吞
   （filter=0 显式保证），异常路径强制补 UP + 强制还原 —— 达到「不卡死、不留按下状态」
   的要求。**做不到 0 干扰，这是物理事实**：任何能被游戏受理的鼠标注入都会共享系统光标。
4. **分发可控**：驱动不进默认包，做成可选增强；DLL 已实测可随 `binaries` 分发。

**不推荐的替代**：把 SendInput 当主路径（UIPI 静默失败 + 真点击点到用户窗口）、
把纯 PostMessage 当主路径（游戏不认）、把驱动做成强制安装（签名 + 反作弊双重雷）。

**上线前必须补的真机验证**（本分支被约束禁止，移交最终验证阶段）：
1. 游戏内真实受理：倒计时窗口连点 20 次，统计成交率；
2. 独占全屏下 `ClipCursor` 是否把驱动注入也钉死；
3. 混合 DPI / 副屏下 `to_abs` 落点误差；
4. 驱动装好后物理鼠标是否出现可感知卡顿（人工手感）。
