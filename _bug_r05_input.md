# R05 输入链路专项审查

审查对象：`C:\Users\26346\Desktop\project\dafors` 输入层（三条通道 `interception` / `legacy` / `message`、`channel_status()`、`channel_events()`、`input_fallback`、`message_capability()`、稳定性原因码）
审查日期：2026-11-07

---

## 0. 快照与方法

### 0.1 代码状态（先说明，避免把 WIP 当回归）

- 分支：`main`，HEAD `5eef554 feat(ocr): ...`（复查时未变）
- 输入层相关文件状态：
  - `M core/input.py`（mtime 2026-11-07 05:20:58，56,430 B）
  - `M core/interception.py`、`M core/engine.py`（engine mtime 05:48:44）、`M core/server.py`
  - `?? core/nocursor.py`（未跟踪新文件）、`?? tests/test_input_channels.py`
  - 同工作区另有大量其它模块（`config.py`/`ocr_service.py`/`web/index.html` 等）未提交改动，属并行会话的工作，**与本报告结论无关，按“不要当 bug”处理**
- 结论：**不是“其他分支修改态”，也没有暂存/另一分支的并行版本**；HEAD 与输入层文件版本一致，行号就是本报告引用的那版。报告写完时间晚于 `input.py` / `engine.py` 的最后修改，探针与行号对得上；若之后这些文件再被编辑，行号会漂移。
- `core/actions.py`、`core/hotkey.py`、`core/nav*.py` 相对 HEAD 未改动，行号稳定；`core/config.py` 有并行改动，但本报告只在 `_config_value` 调用语义上引用 `input.py`，不依赖其内部行号。

### 0.2 探针与安全约束

探针文件（都在 `C:\Users\26346\Documents\Trae-CN\temp_20261107_17`，不在仓库内，未改仓库代码）：

- `probe_input_r05.py`：创建 **HWND_MESSAGE 隐藏消息窗**（类名可指定，如 `UnrealWindow`），真实 `PostMessageW` / `SendMessageTimeoutW`；驱动和光标 API 用桩替换。
- `probe_hotkey_stop.py`：用假 `SetWindowsHookExW` 句柄复现 `HookHotkey.stop()` 的投递失败路径，**不安装真实键盘钩子**。
- `probe_r05_out.txt`：完整原始输出。

约束执行情况：

- 全程未调用真实 `SetCursorPos` / `mouse_event` / `SendInput`（`core/input.py` 的 `_user32` 被替换成记录桩；驱动通道用 `FakeDrv`，不产生真实事件）。
- 只建不可见的 message-only 窗口，未在桌面创建任何可见窗口。
- 未向任何前台/真实程序发送点击或按键；消息只投递到探针自己创建的隐藏窗口。
- 未安装/卸载 Interception 驱动，未改注册表。

运行方式：

```powershell
cd C:\Users\26346\Desktop\project\dafors
.\.venv\Scripts\python.exe C:\Users\26346\Documents\Trae-CN\temp_20261107_17\probe_input_r05.py
.\.venv\Scripts\python.exe C:\Users\26346\Documents\Trae-CN\temp_20261107_17\probe_hotkey_stop.py
```

---

## 1. 结论摘要

| ID | 严重度 | 问题 | 主要位置 |
|---|---|---|---|
| R05-01 | **高** | 驱动点击把真实光标留在点击点，点击路径完全没有还原；只有 HALT 收尾才还原 | `core/input.py:532-571`、`core/engine.py:540-545` |
| R05-02 | **高** | `fallback=message` 降级时不校验“游戏窗口纯消息无效”，静默走零响应通道，只记 warn | `core/input.py:249-254` |
| R05-03 | **高** | 键盘通道不受 `input_message_strict` / 能力评估约束；与鼠标可分裂成两套通道 | `core/input.py:214-215`、`core/input.py:865`、`core/engine.py:831/1250/1377/1937` |
| R05-04 | **高** | `Keyboard.tap` 无 `finally`：down 成功、up 抛异常 → 键卡在按下；抛原始异常且 cleanup 的 `release_input` 是空实现 | `core/input.py:920-935`、`core/engine.py:180` |
| R05-05 | 中高 | 运行中驱动掉线不按 `input_fallback` 降级，直接整轮中止并报成“鼠标被抢” | `core/input.py:532-571`、`core/engine.py:480-490` |
| R05-06 | 中高 | “点击成功”判定弱：驱动通道忽略 `verify`，死窗口也 accepted；legacy 在目标卡死时仍 True；message 把“窗口受理”记成 accepted | `core/input.py:532-571`、`573-663`、`664-691` |
| R05-07 | 中高 | 滚轮：`SetCursorPos` 失败不报错仍发真实滚轮；缺屏幕原点偏移，副屏滚错位置 | `core/input.py:953-1006`、`core/nav_col.py:922` |
| R05-08 | 中 | `legacy` 的 last-error 读取恒为 0（`windll` 无 `use_last_error`），UIPI 原因码不可达，失败原因误报 | `core/input.py:26`、`590-623` |
| R05-09 | 中 | `WM_NULL` 探活进入目标 WndProc；50ms 阈值对卡帧判定不稳定，能把用户选的 `message` 静默降成 `legacy` | `core/nocursor.py:133-160`、`core/input.py:170-207` |
| R05-10 | 中低 | `message` 通道仍被强制要求前台/抢前台，和“不依赖前台”的定位冲突 | `core/engine.py:427`、`825-830`、`core/input.py:512-524` |
| R05-11 | 中低 | 状态/日志/账本与实际不一致：message 被记成 legacy；Keyboard 状态缺字段；账本是全进程共享 | `core/engine.py:450`、`core/input.py:478-486`、`877-887` |
| R05-12 | 中低 | UIPI 预判有漏口：无 hwnd 直接跳过却会退回前台窗口；完整性读不到就放行；只比 RID | `core/input.py:373-404`、`608` |
| R05-13 | 中低 | 退出/注销不清理：`Interception.close()` 无调用点、`os._exit(0)`、热键 stop 不看投递结果 | `core/server.py:715/1123-1127`、`core/hotkey.py:214-227` |
| R05-14 | 低 | 整数扫描码在 legacy/message 被当 VK 用；keyup 失败只返回 False 无人检查；已销毁句柄丢 `WINDOW_GONE` 原因码 | `core/input.py:893-903`、`core/input.py:664-691` |

---

## 2. 问题详情

### R05-01 驱动点击不还原光标（高）

- **严重度**：高
- **文件与行号**
  - `core/input.py:66`（注释宣称 interception “不动光标”）
  - `core/input.py:495`（`click()` 文档写“不需要动真实光标”）
  - `core/input.py:532-571`（`_click_driver` 只有 `SET/MOVE → DOWN → UP`，无 `origin` 读取、无 `SetCursorPos` 还原、无落地等待）
  - `core/interception.py:10-20`（同一份代码的驱动层文档明确写：**系统光标会被真实移动**；`send` 返回早于落地，还原必须轮询/延迟）
  - `core/nocursor.py:278-305`（旧实现 `_driver_click` 有 `finally` + `sleep(0.001)` + `SetCursorPos(origin)`；`input.py` 这份拷贝把还原整段丢了）
  - `core/engine.py:540-545`（只有 guard HALTED 才 `_cleanup.run`，正常 DONE 不收尾）
- **复现方式**
  1. 打桩 `I._driver_probe = lambda: (True, "")`、`X.shared = lambda: FakeDrv()`，构造 `Mouse(FakeScreen(), game_hwnd=隐藏窗, mode="interception", fallback="legacy")`。
  2. `mouse.click(500, 300, expect_foreground=False)`。
  3. 用记录型 `_user32` 监视 `SetCursorPos` 调用。
- **实际观测现象**
  - `[P4_driver_click_cursor_calls] setcursor=[] strokes=[0, 1, 2] last_click={'channel': 'interception', 'accepted': True, ...}`
  - 也就是说整次点击只有 move/down/up 三条 stroke，**没有任何光标位置读写**。探针没有移真实光标（驱动是假的），但按 `interception.py:10-20` 与 `nocursor.py:300-305` 的实测结论，真实运行时每条 `MOUSE_MOVE_ABSOLUTE` 都会把系统光标落到 `(x,y)`，且落地是异步的（约 0.48ms）。
- **影响**：默认通道每点一次，用户真实光标就被甩到游戏里的点击点；整轮结束（DONE）不还原，只有硬停/误买收尾才还原。用户在窗口化模式会看到光标被“吸走/乱跳”，退出后光标停在最后点击点。这条直接违反本次验收线“绝不移动真实光标”。

---

### R05-02 `fallback=message` 降级跳过“纯消息对游戏无效”检查（高）

- **严重度**：高
- **文件与行号**
  - `core/input.py:249-254`（`_degrade` 到 `message` 只检查 `usable`，**没有**检查 `functional == "unsupported"`）
  - `core/input.py:283-287`（对照：`requested=message` 分支有这条检查）
  - `core/input.py:175-190`（`functional` 已算出 `unsupported`，且文案写明“branchA 实测 200/200 次窗口受理、游戏侧零响应”）
- **复现方式**
  1. `I._driver_probe = lambda: (False, "模拟驱动未就绪")`。
  2. 用类名 `UnrealWindow` 的隐藏消息窗作 `game_hwnd`。
  3. `Mouse(None, game_hwnd=unreal, mode="interception", fallback="message")`。
- **实际观测现象**
  - `[P1_driver_missing_fallback_message] effective=message requested=interception degraded=True reason=DRV_UNAVAILABLE functional=unsupported`
  - 账本事件 `severity='warn'`，`ok=True`，warning 原文：“纯消息点击不会进入游戏逻辑”。
  - 对照 `[P1b_fallback_fail] raised=InputChannelError code=DRV_UNAVAILABLE`：`fail` 本身是生效的（见“确认没问题”）。
- **影响**：配置 `input_mode=interception` + `input_fallback=message` 的用户，在驱动缺失时整轮都会走一个**代码自己已知零响应**的通道；每次 click 仍 `accepted=True`，统计显示“点了几百次”，游戏毫无反应。这正是 `fail` 选项想避免的“静默白跑”，只是从 `fail` 路径漏到了 `message` 路径。

---

### R05-03 键盘通道绕开 strict/能力评估，且与鼠标可分裂（高）

- **严重度**：高
- **文件与行号**
  - `core/input.py:865`（`Keyboard.__init__` 调 `_decide_channel(mode, None, None)`，`message_cap_fn=None`）
  - `core/input.py:214-215`（`message_cap_fn` 为空时 `usable=True` / `functional="unknown"`）
  - `core/input.py:275-294`（因此 `requested=message` 的两条降级条件对键盘都不成立）
  - `core/engine.py:831/1250/1377/1937`（引擎用 `_input_mode`（配置原值）构造 `Keyboard`，不是 `mouse.mode`）
  - `core/actions.py:59`（对照：这里用 `mode=mouse.mode`，把 requested 覆盖成 effective）
- **复现方式**
  1. 隐藏 `UnrealWindow` 窗口。
  2. `mouse = Mouse(None, game_hwnd=unreal, mode="message", fallback="legacy")`（strict 取默认 True）。
  3. `kb = Keyboard(unreal, mode="message")`（引擎写法）/ `Keyboard(unreal, mode=mouse.mode)`（actions 写法）。
- **实际观测现象**
  - `[P11_split_channel] mouse_effective=legacy keyboard_effective=message strict_mouse_reason=MESSAGE_RAW_CURSOR_UNSUPPORTED`
  - `[P10_keyboard_requested_lost] mouse_effective=legacy kb_requested=legacy kb_effective=legacy kb_degraded=False`（actions 写法下，配置里的 `interception` 在键盘状态里彻底消失）
  - `[P7_legacy_tap_no_hwnd] tap_return=True hwnd=None status=legacy`：`Keyboard` 也没有任何“没有目标窗口”检查（`R_NO_TARGET_HWND` 常量定义了但没用）。
- **影响**：`input_mode=message` + `strict=True` 的场合，鼠标被正确降级到 legacy（会动光标），键盘却留在 message（对游戏零响应）。开局 F4 重置失效、后续按键全丢，而 `Keyboard.channel_status()` 仍报 `degraded=False`。同一轮里鼠标和键盘走不同通道，账本里只有鼠标那条降级事件。

---

### R05-04 `Keyboard.tap` 异常不补抬起，键会卡在按下（高）

- **严重度**：高
- **文件与行号**
  - `core/input.py:929-935`（down → `sleep(hold)` → up，无 `try/finally`）
  - `core/input.py:920-935`（异常直接抛 `InterceptionError`，不是带 `reason_code` 的 `InputChannelError`）
  - `core/engine.py:180`（`release_input=lambda: True`，注释“点击是即按即放，没有持续按下态”）
- **复现方式**
  1. 打桩驱动：第 1 条 `send_key` 成功，第 2 条（up）抛 `InterceptionError`（模拟驱动在 hold 期间掉线）。
  2. `Keyboard(隐藏窗, mode="interception").tap("F4", hold=0.05)`。
  3. 也等价于在 `sleep(hold)` 期间触发 `KeyboardInterrupt`（`BaseException` 不会被任何 `except` 捕获）。
- **实际观测现象**
  - `[P6_tap_exception] result=InterceptionError strokes=[(62, False, True), (62, True, False)] up_ok=False`
  - 解读：down 被受理（`ok=True`），up 被驱动拒绝（`ok=False`）；代码没有重试、没有 finally，异常原样逃逸。`up_ok=False` 说明抬起从未成功送达。
  - 对照鼠标驱动路径 `[P3b_down_ok_up_fail] strokes=[0,1,2,2]`：鼠标会补一次 up，键盘不会。
- **影响**：游戏侧看到 F4（或任何热键）**一直按着**；若该键是移动/交互键，会持续触发。引擎收尾的 `release_input` 是空实现，无法补救；异常类型也不是 `InputChannelError`，引擎只能走通用异常分支，账本拿不到稳定原因码。`Keyboard.tap` 的调用方（`core/nav.py:411` 等）也全部忽略返回值。

---

### R05-05 运行中驱动掉线不按 `input_fallback` 降级（中高）

- **严重度**：中高
- **文件与行号**
  - `core/input.py:532-571`（`_click_driver` 直接 `raise InputChannelError(..., R_DRIVER_SEND_REJECTED)`）
  - `core/input.py:211-338`（通道决策只在构造时算一次，点击时不再决策）
  - `core/engine.py:480-490`（`InputChannelError` 继承 `MouseStolen`，被当作“鼠标被抢”走 SC_SELF_FAULT/停轮）
- **复现方式**
  1. 打桩 `_driver_probe=(True,"")`，`shared()` 返回第 2 次调用抛 `InterceptionError` 的假驱动。
  2. `Mouse(..., mode="interception", fallback="legacy").click(...)`。
- **实际观测现象**
  - `[P3_runtime_driver_lost] result=InputChannelError:DRV_SEND_REJECTED effective_after=interception`
  - 明明配了 `fallback=legacy`，实际没有降级；`mode` 仍是 `interception`。
  - 日志语义会走“鼠标被抢”，而不是“驱动掉线，已降级/已失败”。
- **影响**：驱动在抢购过程中被卸载/杀软清理（很常见的运行时事件）时，整轮直接终止，`legacy` 兜底完全没用；用户看到的是“鼠标被抢”，排查方向会被带偏。

---

### R05-06 “点击成功”判定弱，三种误判各有样本（中高）

- **严重度**：中高
- **文件与行号**
  - `core/input.py:501-503`（文档宣称“三条通道各有每次点击后真实性校验”）
  - `core/input.py:532-571`（`_click_driver` 的 `verify` 参数**从未使用**；不检查 hwnd、不检查 IsWindow、不检查光标）
  - `core/input.py:573-663`（`_click_messages` 只校验“光标在目标点”，不校验窗口是否处理了消息）
  - `core/input.py:664-691`（`_click_message` 把 `Clicker` 的“窗口受理”直接作为 `accepted=True`）
- **复现方式**
  1. 驱动：`game_hwnd=0xDEAD`（无效句柄）+ 假驱动，`click(500,300)`。
  2. legacy：目标窗口 WndProc 里 `sleep(200ms)`（模拟卡帧），`_click_messages(100,100,verify=True)`。
  3. message：`UnrealWindow` + `input_message_strict=False`，`Mouse(mode="message").click(..., verify=True)`。
- **实际观测现象**
  - `[P4b_driver_click_dead_hwnd] click_return=True last_click={'channel':'interception','accepted':True,...} strokes=3`
  - `[P12c_legacy_click_while_busy] click_return=True click_ms=336.53 post_ok=True target_responsive=False`
  - `[P11b_message_unsupported_accepted] click_return=True last_click={'channel':'message','accepted':True,'functional':'unsupported','window_class':'UnrealWindow'}`
- **影响**：`accepted` 的实际含义分别是“驱动受理了 stroke”“光标到过目标点”“窗口 WM_NULL 有应答”，都不是“游戏逻辑生效”。默认通道（驱动）反而是三者中最弱的；上层如果把 `accepted`/返回值当成功，会把整轮无效点击记成成功。`strict=False` 那条属于配置明示的“只告警、照常投递”，但状态字段仍容易被误用（单列为低优先语义问题）。

---

### R05-07 滚轮路径：失败不报错 + 缺原点偏移（中高）

- **严重度**：中高
- **文件与行号**
  - `core/input.py:971`（`_user32.SetCursorPos(int(x), int(y))` 返回值被忽略，之后无条件发滚轮）
  - `core/input.py:971` vs `core/input.py:584`（click 用 `origin_left + x`，wheel 用裸 `x`）
  - `core/input.py:1004`（message 滚轮把 `x,y` 直接当屏幕坐标打包；`WM_MOUSEWHEEL` 的 lParam 需要屏幕坐标）
  - `core/nav_col.py:922`、`core/nav_ons.py` 的滚轮调用点传入的是**监视器内相对像素**（与 click 同一套坐标）
  - `core/input.py:775-782`（注释明确说 ClipCursor 时 `SetCursorPos` 会静默失败）
- **复现方式**
  1. 桩 `_user32.SetCursorPos` 返回 0（模拟 ClipCursor/UIPI），`Keyboard(hwnd, mode="legacy").wheel(100,100,3)`。
  2. 用 `FakeScreen(left=1920)` 的 `Mouse` 先 `click(100,100)`，再 `Keyboard.wheel(100,100,1)`，记录 `SetCursorPos`。
- **实际观测现象**
  - `[P9a_wheel_setcursor_fail] return_value=True set_calls=[(100,100),(0,0)] wheel_events=3`
  - `[P9b_origin_mismatch] click_set_calls=[(2020,100),(0,0)] wheel_set_calls=[(100,100),(0,0)] screen_left=1920`
- **影响**：
  1. 光标没移过去时，滚轮事件会落在**用户当前光标所在的窗口**（可能是浏览器/聊天窗），把别人的页面滚走，而 `wheel()` 返回 True；
  2. 副屏/非 (0,0) 原点时，点击在 `(2020,100)`，滚轮却在 `(100,100)`（主屏左上），滚错屏；message 通道同理会打包错误坐标。驱动通道下滚轮跟随真实光标，同样受影响。

---

### R05-08 `legacy` 的 last-error 恒为 0，UIPI 原因码不可达（中）

- **严重度**：中
- **文件与行号**
  - `core/input.py:26`（`_user32 = ctypes.windll.user32`，没有 `use_last_error=True`）
  - `core/input.py:590/620`（`ctypes.set_last_error(0)`）
  - `core/input.py:592/622`（`ctypes.get_last_error()`）
  - `core/input.py:623`（`R_UIPI_BLOCKED if err == 5 else R_POST_REJECTED`）
  - 对照 `core/nocursor.py:22`（`WinDLL("user32", use_last_error=True)`）
- **复现方式**
  1. `ctypes.set_last_error(0); ctypes.windll.user32.PostMessageW(0xDEAD, 0x0201, 1, 0)`；读 `ctypes.get_last_error()` 与 `kernel32.GetLastError()`。
  2. 用 `core/input.py` 同款无 `use_last_error` 的 `_user32` 走到 `_click_messages` 的 PostMessage 失败分支。
- **实际观测现象**
  - `[P5a_lasterror_windll] post_ok=0 ctypes_slot=0 native=1400`
  - `[P5b_lasterror_use_last_error] post_ok=0 ctypes_slot=1400`
  - `[P5c_legacy_post_rejected] code=POST_REJECTED text=PostMessageW(0x0200) 被拒 err=0：窗口 57005 收不到点击消息（已退出或被 UIPI 拦下）`
- **影响**：`err` 永远是 0，`R_UIPI_BLOCKED` 这条分支是死代码；已退出窗口（原生 1400）与 UIPI 拒绝（5）被统一报成 `POST_REJECTED`，且文案里写“err=0”，排障时拿不到真实原因。`_gate_integrity` 只能覆盖“目标 IL 更高”，覆盖不到的场景（见 R05-12）会在这里退化成一句模糊报错。

---

### R05-09 `WM_NULL` 探活的干扰、成本与不稳定判定（中）

- **严重度**：中
- **文件与行号**
  - `core/nocursor.py:133-160`（`window_responsive`：`SendMessageTimeoutW(WM_NULL, SMTO_ABORTIFHUNG, 50ms)`；`message_capability` 的 `usable` 直接依赖它）
  - `core/input.py:170-207`（每次构造/每次 message 点击都会走到这里；`detail` 无条件被覆盖）
  - `core/input.py:249-254`、`283-287`（这个 `usable` 直接决定是否降级）
- **复现方式**
  1. 隐藏窗 WndProc 计数 WM_NULL；调 `message_capability` 5 次，记录耗时和计数。
  2. 把 WndProc 改成每次睡 120ms（模拟游戏卡帧），再探活；同一轮里分别对两个窗口探活。
  3. `Clicker(hwnd,"message",verify=True)` 连点 10 次，数 WM_NULL 增量。
- **实际观测现象**
  - `[P8a_wmnull_healthy] probes=5 elapsed_ms=0.8 wm_null_delivered=5 usable=True`：WM_NULL 确实进了目标 WndProc，是**同步**往返。
  - `[P8b_wmnull_slow_target] elapsed_ms=52.54 usable=False reason=TARGET_HUNG`
  - 同一份脚本、同一轮的另一个窗口：`[P8b2_unreal_slow] usable=True reason=None`（同样 120ms 睡眠却判“50ms 内应答”）
  - `[P8b3_message_config_downgraded_by_hitch] requested=message effective=legacy reason=MESSAGE_RAW_CURSOR_UNSUPPORTED`（另一轮同一用例 reason 为 `TARGET_HUNG`）
  - `[P8c_click_verify_wmnull] clicks=10 wm_null_added=10`：message 通道每点一次多一次同步 WM_NULL；单次 0.096ms，但目标卡顿时最坏阻塞 50ms。
- **影响**：
  - WM_NULL 本身无副作用，但它会进目标 UI 线程；message 通道高频连点等于每点一次同步“敲”一次游戏消息泵，卡帧时会拖慢点击循环。
  - 50ms 阈值对 100ms 级卡顿判定不稳定（同一轮出现 True/False 两种结论）。启动瞬间游戏只要卡一下，用户配置的 `message`（不动光标）就会被静默改成 `legacy`（动光标），或按 `fail` 直接失败；这属于“配置没有按预期执行”。
  - `detail` 在 `input.py:203-207` 被无条件覆盖：`reason=TARGET_HUNG` 时 `detail` 仍可能是“游戏读真实光标…”的类名文案，状态自相矛盾。

---

### R05-10 `message` 通道仍被强制前台/抢前台（中低）

- **严重度**：中低
- **文件与行号**
  - `core/engine.py:427`（`_click` 恒定 `expect_foreground=self._target_hwnd`）
  - `core/engine.py:825-830`（每轮开始 `ensure_foreground`）
  - `core/input.py:512-524`（前台不符时 `AttachThreadInput + SetForegroundWindow` 抢回来）
  - `core/input.py:68`（message 定位是“纯 PostMessage，全程不动光标”）
- **复现方式**：静态路径即可；运行时可把控制台切到前台后调用 `engine._click`，观察 `ensure_foreground` 被触发（探针未做，避免抢真实前台）。
- **实际观测现象**：代码无条件走前台校验；与通道无关。`[P12c]` 的 legacy 路径也验证了 `expect_foreground` 是点击入口的统一前置。
- **影响**：message 通道本来按 hwnd 投递、不依赖前台，却仍会在用户切到控制台时把游戏抢回前台（每个 click 一次），既违背“零干扰”定位，也可能和用户的其它窗口抢焦点。

---

### R05-11 状态/日志/账本与实际行为不一致（中低）

- **严重度**：中低
- **文件与行号**
  - `core/engine.py:450`（`'驱动注入(interception)' if eff == 'interception' else 'PostMessage(legacy)'` → **message 被写成 legacy**）
  - `core/input.py:877-887`（`Keyboard.channel_status()` 缺 `requested_raw`、`verify`、`integrity`、`message_capability`、`last_click`，无法审计键盘是否“已知无效”）
  - `core/input.py:478-486`（`channel_status()` 返回的是**全进程共享**的 `LEDGER` 事件，不按实例过滤）
  - `core/input.py:224-230` + `478`（未知 mode（如 `"foo"`）被规范成 `interception`，`requested` 也变 `interception`，`degraded=False`，原始值只在事件里）
- **复现方式**
  1. 任意一次 `Mouse(mode="message")` 后读 `channel_status()["events"]`，可见历史任务遗留的 `DRV_UNAVAILABLE`、`INTEGRITY_UNKNOWN`。
  2. 构造 `Keyboard(hwnd, mode=mouse.mode)` 后读 `requested`。
- **实际观测现象**
  - `[P14_events_tail]` 同一份 `channel_events()` 里混着 `legacy→legacy INTEGRITY_UNKNOWN`、`message→legacy MESSAGE_RAW_CURSOR_UNSUPPORTED`、`interception→legacy DRV_UNAVAILABLE`，来自不同实例/不同用例。
  - `[P10_keyboard_requested_lost] kb_requested=legacy kb_degraded=False`（用户配置是 interception）。
- **影响**：前端/日志页把“本次任务实际通道”和“历史降级原因”混在一起展示；message 通道在日志里显示成 legacy，排障时会去查错的通道。未知 mode 被替换时 `degraded=False`，掩盖了“配置没被识别”。

---

### R05-12 UIPI 预判的漏口（中低）

- **严重度**：中低
- **文件与行号**
  - `core/input.py:373-377`（`_gate_integrity`: `if not hwnd: return`，**不做任何预判**）
  - `core/input.py:608`（`_click_messages` 在 hwnd 为空时退回 `self.foreground()`，即对前台窗口发点击）
  - `core/input.py:397-404`（完整性读不到（`None`）只记 `INTEGRITY_UNKNOWN` 后放行）
  - `core/input.py:1078-1102`（`process_integrity` 只比较 RID 整数；AppContainer SID、跨用户、UIAccess 未处理）
- **复现方式**：`Mouse(None, game_hwnd=0xDEAD, mode="legacy", fallback="legacy")`，读 `channel_status()["integrity"]` 与账本。
- **实际观测现象**
  - `[P13_integrity_unknown] integrity={'ours': 8192, 'target': None, 'ours_name': '普通', 'target_name': '未知'} last_event={... 'reason_code': 'INTEGRITY_UNKNOWN', ... 'severity': 'info'}`
  - 即“读不到目标完整性”时没有任何拦截，直接继续。
- **影响**：目标进程受保护/权限受限（PPL、系统进程、AppContainer）时，UIPI 预判形同虚设；真正被拦时只能落到 R05-08 的模糊 `POST_REJECTED`。无 hwnd 时退回前台窗口发点击，等于对用户当前前台程序动手，且完全绕过 UIPI 预判。

---

### R05-13 退出/注销的句柄与钩子清理（中低）

- **严重度**：中低
- **文件与行号**
  - `core/server.py:715`（`/api/quit` 直接 `os._exit(0)`）
  - `core/server.py:1123-1127`（主循环退出只停 watcher/scheduler/svc，**没有** `hotkey.stop()`）
  - `core/interception.py:209-215`（`close()` 存在且幂等，但全仓**没有调用点**；`grep -r "interception.close"` 为空）
  - `core/hotkey.py:214-227`（`stop()` 不检查 `PostThreadMessageW` 返回值；`join(1.0)` 超时后仍返回 True 并写 `已停止`）
- **复现方式**：`probe_hotkey_stop.py`：假 `SetWindowsHookExW` 返回句柄、假 `PostThreadMessageW` 返回 0，`start()` 后 `stop()`。
- **实际观测现象**
  - `start= True / stop= True / thread_alive= True / hook= 48879 / ok= False / msg= 已停止`
  - 即：stop 报告成功，线程仍卡在 `GetMessageW`，假 hook 句柄仍在 `_hook` 上，`_ok=False` 让 `status()` 判 `live=False`（UI 显示“已停止”，实际钩子还挂着）。
- **影响**：热键线程若在队列建立前被 stop（或投递失败），会留下一个永不退出的线程和一个仍生效的全局键盘钩子；之后重新注册热键会**叠钩子**，一次 F8 触发多次急停回调。`Interception` 上下文与键盘钩子都靠进程退出兜底：单进程内不致命（OS 退出时回收），但“注销即干净”不成立，反复启动/内嵌复用的场景会累积。

---

### R05-14 低优先边角（低）

- **严重度**：低
- **文件与行号**
  - `core/input.py:895-896`（`tap()` 允许整数扫描码，但 legacy/message 里 `vk = scan`，把**扫描码当虚拟键**投递）
  - `core/input.py:899-903`（keyup `PostMessageW` 返回 0 时只 `return False`，不补发、不抛、无原因码；`nav` 等调用方不看返回值）
  - `core/input.py:664-691`（已销毁句柄的 message 点击最终报 `InputError`/`INPUT_ERROR`，而不是刚刚算出的 `WINDOW_GONE`）
  - `core/input.py:613`（`ScreenToClient` 返回值不检查，失败时把屏幕坐标当客户区坐标打包，点击会偏但仍“受理”）
  - `core/input.py:142-149`（每次构造都 `Config.load()` 重读磁盘，忽略引擎传入的 cfg 快照；任务级覆盖 `input_fallback/input_message_strict` 不生效，探针只能 monkeypatch）
  - `core/input.py:937-951`（`Keyboard.click` 缓存的 `Mouse` 用 `screen=None`、`mode=self._mode`：副屏缺原点偏移，且首次点击会重新做一次通道决策）
- **复现方式**
  - keyup 失败：`FakeU32.post_seq=[1,0]` → `Keyboard(hwnd,"legacy").tap("F4")`。
  - 已销毁句柄：创建隐藏窗后 `PostMessage(WM_CLOSE)`，等 50ms，`Clicker(dead,"message").click(...)`。
  - 整数键：`Keyboard(hwnd,"legacy").tap(0x3E)`（期望 F4 的 VK 0x73，实际 wParam=0x3E）。
- **实际观测现象**
  - `[P7b_legacy_keyup_fail] tap_return=False raised=None posts_total=2`
  - `[P12b_click_destroyed] raised=InputError code=INPUT_ERROR`（而 `[P12_destroyed_hwnd] reason=WINDOW_GONE` 是能力评估已经算对的）
- **影响**：hotkey 抬起可能静默丢；message 通道失败原因码退化；整数键 API 实际不可用；多屏偏移与 R05-07 同源。

---

## 3. 确认没问题的项

1. **`input_fallback=fail` 在构造期确实直接失败，不会偷偷降级**：`[P1b] raised=InputChannelError code=DRV_UNAVAILABLE`；`core/input.py:235-240` 的 `_raise` 路径生效。
2. **`requested=message` + 默认 strict + 游戏窗口**：会正确降级并带 `MESSAGE_RAW_CURSOR_UNSUPPORTED`：`[P2] effective=legacy reason=MESSAGE_RAW_CURSOR_UNSUPPORTED degraded=True`。
3. **驱动通道 down 成功、up 被拒时会补一次 up**：`[P3b] strokes=[0,1,2,2]`（两个 UP）；`core/input.py:556-570` 的 `pressed` + `except InterceptionError` 分支工作正常（但不覆盖非 `InterceptionError`，见 R05-04 同类问题）。
4. **legacy 点击有 `finally` 补 up 与光标还原**：`pressed` 只在 down 受理后置 True，up 失败会补发；`origin` 在 `SetCursorPos` 前读取，`finally` 用 `SetCursorPos(origin)` 还原（`core/input.py:573-663`）。
5. **message 点击不调用 `SetCursorPos`、不注入移动事件**：`Clicker._post_click` 只发 WM_*（`core/nocursor.py` message 分支）；P11b 的 message 点击期间也没有任何光标 API 调用。
6. **账本写入有锁、上限 200、事件字段完整**：`core/input.py` `_ChannelLedger.record` 串行化，`channel_events()` 返回拷贝（仅“全进程共享”语义见 R05-11）。
7. **`Interception.close()` 本身幂等**：`_ctx is None` 直接返回，重复调用安全（只是没人调用，见 R05-13）。
8. **`passable` 的 `WM_NULL` 不会改变目标窗口状态**：探针 WndProc 只收到 WM_NULL，DefWindowProc 路径无副作用；问题在“同步成本/阈值判定”，不在消息本身。

---

## 4. 无法验证的项

1. **真实 Interception 驱动路径的光标落地时序**：本机没有安装/加载驱动，无法实测 `MOUSE_MOVE_ABSOLUTE` 真实落地 0.48ms 与“还原早于入栈”的时序；结论来自 `core/interception.py:10-20` 与 `core/nocursor.py:300-305` 的实测注释，控制流用假驱动验证。若驱动版本行为不同，R05-01 的“光标被留在点击点”仍成立（代码没有还原），只是滞留时长可能不同。
2. **高完整性目标（UIPI 实际拦截）**：没有构造管理员/系统级目标进程，无法实测 `PostMessage=0 + GetLastError=5` 的完整链路；只验证了 `input.py` 读不到这个 5（R05-08）。
3. **真游戏（UnrealWindow）对 WM_* 的实际响应**：按要求没有开真游戏；“200/200 受理、游戏零响应”来自仓库内 `docs/auto-buy-design.md §4.8` 与代码注释，未在本轮复测。
4. **60Hz 连点下 WM_NULL 对游戏 UI 线程的帧耗时**：隐藏窗测得 0.096ms/次；真实 D3D 窗口的 WndProc 成本与卡帧分布无法在无游戏环境量化，R05-09 的“最坏 50ms”是超时上界。
5. **`process_integrity` 对 AppContainer / 跨用户 / PPL 的具体返回值**：无法在当前会话构造这些进程，只按代码路径判断“RID 比较不覆盖”。
6. **进程退出后钩子/驱动上下文是否由 OS 完全回收**：按 Windows 语义应当回收，但未做“进程退出后另一进程检测残留钩子”的实验；R05-13 只对“进程内 stop 假成功”给了可复现证据。

---

## 5. 附：关键观测原始行

```
[P1]  effective=message requested=interception degraded=True reason=DRV_UNAVAILABLE functional=unsupported
[P1b] raised=InputChannelError code=DRV_UNAVAILABLE
[P2]  effective=legacy reason=MESSAGE_RAW_CURSOR_UNSUPPORTED degraded=True
[P3]  result=InputChannelError:DRV_SEND_REJECTED effective_after=interception
[P3b] strokes=[0, 1, 2, 2]
[P4]  setcursor=[] strokes=[0, 1, 2] accepted=True
[P4b] click_return=True accepted=True strokes=3
[P5a] post_ok=0 ctypes_slot=0 native=1400
[P5c] code=POST_REJECTED err=0
[P6]  strokes=[(62,False,True),(62,True,False)] up_ok=False
[P7]  tap_return=True hwnd=None
[P7b] tap_return=False raised=None
[P8a] wm_null_delivered=5 usable=True
[P8b] elapsed_ms=52.54 usable=False reason=TARGET_HUNG
[P8b2] usable=True reason=None  (同一轮、同样 120ms 卡帧，结论相反)
[P8c] clicks=10 wm_null_added=10
[P9a] return_value=True set_calls=[(100,100),(0,0)] wheel_events=3
[P9b] click=(2020,100) wheel=(100,100) screen_left=1920
[P10] kb_requested=legacy kb_degraded=False
[P11] mouse_effective=legacy keyboard_effective=message
[P12] reason=WINDOW_GONE / [P12b] code=INPUT_ERROR
[P12c] click_return=True target_responsive=False
[P13] target=None reason_code=INTEGRITY_UNKNOWN severity=info
hotkey: stop=True thread_alive=True hook=48879 ok=False msg=已停止
```
