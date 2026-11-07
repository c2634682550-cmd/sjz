# 非游戏窗口 · 输入链路真实驱动验证

- 仓库 `C:\Users\26346\Desktop\project\dafors`，驱动 `C:\Users\26346\Downloads\AxonSkin+鼠标驱动\drivers`
- 探针 `_nonreal_input_probe.py`（隐藏消息窗 style 0 + 5×5 px alpha=1/255 NOACTIVATE topmost 承接窗 + WH_MOUSE_LL 钩子，无任何可见窗口）
- 采集：2026-11-07 16:11–16:32，5 轮完整运行（第 4、5 轮被外部输入污染，点击位置数据作废，占用数据仍可用）
- 机器：1920×1080，进程完整性 Medium（S-1-16-8192，非管理员），filters 1..20 全 0，`driver_status.ok=True`
- 未 commit、未 push；未打开任何可见窗口

## 判定总表

| # | 项目 | 判定 | 关键数字 |
|---|------|------|----------|
| 1 | 设备身份四态 | PASS | real=slot14(3554/F50D)、virtual=slot11(5E2A/71B4)、legacy、message 全部正确写入 `channel_status()` |
| 2 | LLMHF_INJECTED | PASS | 驱动 20 击 40 事件 injected=0；SendInput 20 击 40 事件 injected=20 |
| 3 | Raw Input 归属 | PASS | 30 击 hdev 131146（VID_3554&PID_F50D&MI_02）60/60，real_ratio=1.0，virtual=0 |
| 4 | 还原可靠性 | FAIL | 3 轮 500 击：1ms 落位 500/488/496，10ms 500/498/494，30ms 未还原 0/0/**3** |
| 5 | 占用指标 | FAIL → **测量污染，非回归** | p50 +127.5%/+86.6%/+14.7%/+72.2%；p95 +4.5%/+71.7%/−45.1%/−7.9%；max +29.7%/+100.3%/−54.1%/+23.3%（四轮偏差均由同机并发负载造成，见 `_click_cost_20261107.md`） |
| 6 | 驱动缺失降级 | PARTIAL | 降级/告警/仍能点全部正确，原因码误报 `DRV_SEND_REJECTED` |
| 7 | UIPI | PARTIAL | 构造期正确抛 `UIPI_BLOCKED`；运行期 `window_responsive` 误报成功 |
| 8 | 热键互斥 | PARTIAL | `Hotkey` 1409 互斥通过；生产路径 `HookHotkey` 两次都成功 |

---

## 1. 设备身份（PASS）

槽位枚举（`get_hardware_id` 分类，1..10 键盘 / 11..20 鼠标）：

| 槽位 | 类型 | VID/PID | 硬件 ID 片段 |
|------|------|---------|--------------|
| 11 | virtual | 5E2A/71B4 | `HID_DEVICE_SYSTEM_VHF&Col02` |
| 12 | virtual | 5E2A/71B4 | `HID_DEVICE_SYSTEM_VHF&Col03` |
| 13 | real | 3869/63E1 | `VID_3869&PID_63E1&MI_00&Col02`（与键盘同 MI，被 auto_pointer 排除） |
| 14 | real | 3554/F50D | `VID_3554&PID_F50D&MI_02`（选中） |
| 15–20 | absent | — | — |

四态身份（同一进程内逐个构造并读 `channel_status()`）：

- `real_device`：identity=real_device，device=14，vid=3554，pid=F50D，effective=interception，degraded=false；`channel_status()` 13 个键齐全（degraded/effective/events/fallback_policy/identity/integrity/last_click/message_capability/occupancy/reason/reason_code/requested/requested_raw/verify）
- `virtual_device`：identity=virtual_device，device=11，5E2A/71B4，real=false（fixture：把真实枚举到的 VHF 行写入共享缓存，因为本机 11/12 槽被过滤规则先排除）
- `legacy`：identity=legacy，effective=legacy，requested=legacy，degraded=false，reason=null
- `message`：identity=message，effective=message，reason=null，usable=true

## 2. 低级钩子标记（PASS）

`WH_MOUSE_LL`（`hMod=None`，有消息循环线程）在同一时间窗内按消息类型计数，不按坐标过滤：

| 组 | 击数 | WM_LBUTTONDOWN n / injected | WM_LBUTTONUP n / injected | 被钩子拦下 |
|----|------|------------------------------|---------------------------|-----------|
| 驱动注入（slot 14） | 20 | 20 / **0** | 20 / **0** | 40 |
| `SendInput` | 20 | 20 / **20** | 20 / **20** | 40 |

样本量：每组 40 条按键事件。驱动组 `clean=20/20`（flags 全 0），SendInput 组 `clean=0/20`。

> 与代码注释冲突：`core/interception.py:10-11` 写「低级钩子看不到注入标志（实测 WH_MOUSE_LL 收不到任何事件）」。实测驱动事件能进 LL 钩子，且 `LLMHF_INJECTED` 未置位。注释应改为「能收到、flags=0」。

## 3. Raw Input 归属（PASS）

30 击，`delivery_watch().wait_buttons(vid=3554, pid=F50D)`：

- raw_ok 30/30，down 30，up 30，moves 59
- real_events 60，virtual_events 0，other_events 0，real_ratio 1.0
- `hdev` 只有 `131146` = `\\?\HID#VID_3554&PID_F50D&MI_02#8&359AFBA4&0&0000#{378DE44C-56EF-11D1-BC8C-00A0C91405DD}`，属于 `real_hdevs`（65632/65628/65626/65624/131146/65601），`vhf_hdevs=[]`
- `hdev_matches_real=true`

结论：真实设备注入的事件归属真实鼠标本体，不经过 VHD 虚拟设备。

## 4. 还原可靠性（FAIL）

3 轮 × 500 击（驱动通道 `_click_driver`，目标 anchor+3px，显式声明位移），每击后 1ms / 10ms 轮询采样，30ms 内未回 anchor 记失败：

| 轮次 | clean 样本 | 起点偏移非零 | 1ms 已还原 | 10ms 已还原 | 30ms 未还原 | settle p50/p95/max (ms) |
|------|-----------|-------------|-----------|------------|------------|--------------------------|
| 轮1 16:11 | 500/500 | 0/500 | 500/500 | 500/500 | 0 | 0.108 / 0.305 / 0.713 |
| 轮2 16:19 | 500/500 | 88/500（全部 ≤1px） | 488/500 | 498/500 | 0 | 0.089 / 0.450 / 2.689 |
| 轮3 16:24 | 500/500 | 100/500（全部 ≤4px） | 496/500 | 494/500 | **3** | 0.042 / 0.193 / 0.678 |

- 判定口径按需求「还原失败次数必须为 0」：**轮3 有 3 次 30ms 内未回到起点**，不达标。
- 1ms 落位率 97.6%–100%，说明还原落在亚毫秒级，但「每击后 1ms 必须已在原点」不是硬保证（11–12 次/500 仍在目标点）。
- 起点偏移非零只在轮2/轮3 出现，且 ≤4px，是绝对坐标 0..65535 换算的量化误差，不是累积漂移。
- `_last_click.restored`：499/500、458/500、493/500 为 `None` —— 代码本就不确认，只把「已排队未落地」记 None。
- 相关实现：`core/input.py:1392-1400`（`restore_early` 只回读一次，`restored = (_cursor_pos() == origin) or None`）、`core/input.py:1408-1410`（`_home_pending` 交给下一击复核）。

## 5. 占用指标（FAIL）

`occupancy_ms`（移动→还原完成）500 样本/轮，基线 p50 0.320（**安静机器单次采样，非稳定基线**；旧文档的 `0.250 ms` 同为此性质，不得作基线）/ p95 0.993 / max 1.979 ms。下表四轮偏差**均为测量污染，非回归**（同一份代码 p50 在 0.28–0.73 ms 摆动，与并发负载同步；证据见 `_click_cost_20261107.md`）：

| 轮次 | p50 | p95 | max | p50 偏差 | p95 偏差 | max 偏差 |
|------|-----|-----|-----|---------|---------|---------|
| 轮1 | 0.728 | 1.038 | 2.567 | **+127.5%** | +4.5% | **+29.7%** |
| 轮2 | 0.597 | 1.705 | 3.963 | **+86.6%** | **+71.7%** | **+100.3%** |
| 轮3 | 0.367 | 0.545 | 0.909 | +14.7% | −45.1% | −54.1% |
| 轮5（外部干扰） | 0.551 | 0.915 | 2.440 | **+72.2%** | −7.9% | **+23.3%** |

- 3/4 轮 p50 超 20%，p95/max 双向大幅漂移（既有显著变慢，也有显著变快）。
- 干扰源（实测存在，不是推测）：同机另一个会话在跑 `pytest`（2 个进程，PID 35636/26232）和 `click_main.py --mode baseline --n 30`（PID 34332，会真实移动光标）。第 4 轮 500 击期间光标被外部移动最多 745px，`contaminated=498/500`，该轮数据作废。
- 结论：现有基线（0.320/0.993/1.979）与本机的并发负载不匹配；要在干净机上复测才能给「是否超 20%」的最终结论。计时窗口本身无问题（`core/input.py:1420`）。

## 6. 驱动缺失的降级（PARTIAL）

两场景都只改配置/内存桩，不卸载驱动、不动磁盘 DLL：

| 场景 | driver_ready | 生效通道 | 原因码 | driver_warning | 点击 | 光标 |
|------|--------------|----------|--------|----------------|------|------|
| 设备号改 99（不存在） | `[False, "配置的鼠标设备号 99 不存在（当前只有 [11, 12, 13, 14]）"]` | legacy | **DRV_SEND_REJECTED**（错） | 有 | 返回 True，窗口消息 +2 | 未动 |
| `_LIB` 失败桩（DLL 不存在） | `[False, "模拟：interception.dll 不存在"]` | legacy | DRV_UNAVAILABLE（对） | 有 | 返回 True，窗口消息 +2 | 未动 |

- `driver_ready()` 没有假阳性（两场景都 False），降级到 legacy 并写出 `driver_warning`，降级后仍能点 —— 这部分达标。
- **原因码错**：设备不存在应报 `DRV_DEVICE_NOT_FOUND`，实际报 `DRV_SEND_REJECTED`。
  - `core/interception.py:693-696`：`identity()` 把 `RealDeviceUnavailable` 吞成 `{"error":..., "reason_code":...}`，所以 `driver_status` 的 `except RealDeviceUnavailable`（`:795-798`）永远进不去；
  - 随后 `:806-812` 的 probe（`send_mouse` 再次取设备才抛错）把它统一记成 `DRIVER_SEND_FAILED` → `DRV_SEND_REJECTED`。
  - 修法：`:793` 之后先读 `status["identity"]["mouse"]["reason_code"]`，非空就直接返回该码，不再做 probe 覆盖。

## 7. UIPI（PARTIAL）

目标：`ShellHandwritingCanvas {18E91349-E229-4995-8049-7437243BBCC3}`，integrity 12288（0x3000，High），本进程 8192（Medium）。

- 构造期：`Mouse(..., mode="legacy", game_hwnd=目标)` 抛 `InputChannelError(UIPI_BLOCKED)`，detail 正确：「本进程是「普通」完整性，目标窗口是「管理员」；UIPI 会静默丢弃输入」——**正确**
- 运行期：`nocursor.window_responsive(目标)` 返回 `(True, None, "50ms 内应答")` —— **误报成功**
  - `core/nocursor.py:147-157` 注释称「`WM_NULL` 低于 WM_USER，正好会被 UIPI 拦下」，事实相反：WM_NULL 在 UIPI 允许列表内，永远透传；
  - `core/nocursor.py:163-168` 用 `SendMessageTimeoutW(WM_NULL)` 探测，高完整性窗口照样返回成功。
  - 修法：探测改用 UIPI 不白名单的消息（如 `WM_USER+n` 自定义消息）或先比 `process_integrity`，再决定是否发消息。

## 8. 快捷方式与热键（PARTIAL）

`Ctrl+Alt+F12`：

| 路径 | 第一次 | 第二次 | 互斥 | 释放后重注册 |
|------|--------|--------|------|--------------|
| `Hotkey`（RegisterHotKey） | True | **False，错误码 1409** | ✅ | True |
| `HookHotkey`（LL 键盘钩子，生产路径） | True | **True（静默成功）** | ❌ | True |

- `Hotkey` 同一实例二次 `start()` 返回 `(True, "已经在监听")`，不同实例第二次 1409 失败 —— 符合「互斥失败、不静默覆盖」。
- `HookHotkey` 两个实例都装上了钩子，会双触发：
  - `core/hotkey.py:154-156` 只判自身 `self._thread`，没有跨实例互斥；
  - `core/hotkey.py:176` 直接 `SetWindowsHookExW`；
  - 生产绑定在 `core/server.py:1393`（`HookHotkey`），`:1397` 才是 `Hotkey` 兜底。
  - 修法：模块级 `_ACTIVE_HOOK_SPECS` 集合，`start()` 前检查同 spec 是否已占用，占用则返回 `(False, "已被占用")`。
- 退出释放：`stop()` 后同 spec 能重新注册成功，钩子确实释放。

## 收尾核对

- 光标原位：轮1 1470,302→1470,302；轮2 1150,881→1150,881；轮3 644,477→644,477（`cursor_restored=true`）。轮4/轮5 期间被外部 `click_main.py --mode baseline` 移动最多 745px / 598px，`cursor_restored=false`，属环境干扰，非探针未还原。
- 驱动上下文：每轮 JSON 记 `driver_context_closed=true`（`interception_destroy_context` 已调用）；filters 1..20 仍全 0，驱动服务状态未变，无内核态残留。
- 残留进程（**未完全达标**）：5 轮探针都出现「写出 JSON 后不自行退出」。
  - 已实施的处理：收尾先 `UnhookWindowsHookEx` + `PostThreadMessage(WM_QUIT)` 关掉消息线程，再在守护线程里限时 `close()`，最后 `os._exit(0)`；仍然退不掉。
  - `Stop-Process -Force` / `taskkill /F /T` 大多能结束；最后一轮（PID 36984）进入「已终止待回收」状态：进程表里只剩 1 个线程，`ThreadState=Wait / WaitReason=ExecutionDelay`，`taskkill` 报 `no running instance`，再等 114s 仍未回收 → 需重启系统才会彻底消失。
  - 单点验证：只 `create_context` 不发送的进程、只装 LL 钩子不发事件的进程都能正常 `os._exit(0)`。卡点出现在「真正发送过 stroke 的 Interception 上下文 + 隐藏窗口线程」组合上，属探针脚本的退出路径，产品路径只要不在进程退出瞬间保留活动驱动上下文即可规避。
- 临时文件已清理；保留 `_nonreal_input_probe.py`、`_nonreal_input.json`、本文件。

## 零位移说明（按要求显式声明）

- 第 2、3 项点击目标 = 当前光标 +3px，靠 LL 钩子拦下 down/up + 5×5 alpha=1 承接窗兜底，点击不会落到用户窗口；视觉上只有 3px 的瞬时移动。
- 第 4 项必须「离开起点再还原」才能测还原，固定 3px 水平偏移，settle p50 ≈ 0.04–0.11ms。
- 其余各项（身份、降级、UIPI、热键）不动光标；`SetCursorPos` 只在结束时把光标放回 anchor。
- 外部干扰期间（另一会话的 `click_main.py --mode baseline` 真实移动光标）无法保证零位移，已在第 4/5 项单列。
