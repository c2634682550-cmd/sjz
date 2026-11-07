# 真实鼠标设备通道：实测数据与结论

目标：注入必须经过**真实物理鼠标的设备栈**输出，低级钩子与 Raw Input 两侧都
「看不出这是注入」；同时不长时间占用光标、不抢前台、不吞物理输入。

采集环境：Windows 10/11 + Interception 驱动已装；目标为本机桌面安全落点
（`SysListView32`，屏幕 (80,80)，不点在真实窗口上）；真实鼠标
dev14 = LAMZU Pro 1K（`VID_3554&PID_F50D&MI_02`）。

---

## 1. 设备枚举（`interception_get_device` 的替代实现）

本仓库随附的驱动 DLL **没有导出 `interception_get_device`**（导出表里只有
`interception_create_context/destroy_context/send/receive/set_filter/get_filter/
get_hardware_id/is_invalid/is_mouse/is_keyboard`）。因此用一个不依赖该导出的
等价实现：对设备槽 11..20 逐个 `get_hardware_id`，按硬件 ID 分类。

| device | 类型 | 判定依据 | 硬件 ID 关键段 |
|---|---|---|---|
| 11 | 虚拟 | `HID_DEVICE_SYSTEM_VHF` | `VID_5E2A&PID_71B4`（VHF 虚拟鼠标） |
| 12 | 虚拟 | `HID_DEVICE_SYSTEM_VHF` | `VID_5E2A&PID_71B4` |
| 13 | 真实但排除 | 与真实键盘同 `(VID,PID,MI_00)` | `VID_3869&PID_63E1&MI_00&Col02`（键盘附赠鼠标集合） |
| **14** | **真实（选中）** | 非虚拟 + 非键盘附赠 + 1.2s Raw Input 活动度最高 | `VID_3554&PID_F50D&MI_02`（LAMZU Pro 1K） |

选择结果：`device=14, source=auto_pointer, real=true`。
枚举不到真实鼠标时 `driver_ready()` 返回 `REAL_DEVICE_MISSING` 并**显式降级**
（`input_allow_virtual` 默认 false，绝不静默改用 dev11/12 这类虚拟设备号）。

驱动会话 filter 快照：所有槽 `filter=0` —— 驱动只做转发，不吞物理输入。

---

## 2. 可检测性对照（核心证据）

同一目标点，每路径 30 次点击；同时开三层观测：

- 低级鼠标钩子 `WH_MOUSE_LL`（看 `LLMHF_INJECTED`）
- Raw Input（`RIDEV_INPUTSINK` 隐藏窗口，看能否收到、`hDevice` 归属哪台设备）
- 鼠标轨迹采样 + 前台窗口前后

| 路径 | LL 事件 | LL `injected` | Raw 事件 | Raw down/up | `hDevice` 归属 | 光标位移 | 前台 before→after |
|---|---|---|---|---|---|---|---|
| SendInput | 90 | **90（全带注入标志）** | 90 | 30/30 | `hdev0`（内核合成，无设备归属） | 0 | 66090→66090 |
| Interception(真实 dev14) | 150 | **0** | 90 | 30/30 | `HID#VID_3554&PID_F50D&MI_02…`（**真实鼠标本体**） | 0 | 66090→66090 |
| Interception(虚拟 dev11) | 150 | **0** | 90 | 30/30 | `HID_DEVICE_SYSTEM_VHF&Col02…`（VHF 虚拟设备） | 0 | 66090→66090 |

落地延迟（Raw Input 收到 down/up 的时延）：

| 路径 | land p50 | land max |
|---|---|---|
| SendInput | 0.862 ms | 2.891 ms |
| Interception 真实设备 | **0.467 ms** | 0.792 ms |
| Interception 虚拟设备 | 0.413 ms | 0.763 ms |

### 结论
1. **SendInput 在低级钩子层就暴露**（`LLMHF_INJECTED=1` 全带），且 Raw Input 拿不到
   设备归属（`hdev0`）—— 两条独立证据都能判定为合成输入。
2. **真实设备 Interception 在两层都「像物理输入」**：钩子看不到注入标志，
   Raw Input 把事件记在**用户那只鼠标的硬件 ID**上。
3. 虚拟设备路径虽然钩子层也干净，但 Raw Input 能看出 `hDevice` 是 VHF 虚拟设备
   —— 这正是为什么默认禁止虚拟设备当作「真实通道」。

---

## 3. 占用窗口（移动→按键落地→还原）

`Mouse.click()` 的占用 = 从第一帧移动 send 到 `SetCursorPos(origin)` 并确认还原。

| n | p50 | p95 | max | 目标 | 结果 |
|---|---|---|---|---|---|
| 80 | **0.534 ms** | **0.843 ms** | **2.565 ms** | p95 ≤ 1ms、max ≤ 3ms | **达标** |

- down/up 落地等待：单次 `landing.elapsed_ms = 0.1935 ms`（驱动 send 异步入栈，
  平均 ~0.48ms 才进 Raw 队列；还原必须等 down+up 都落地，否则会被尚未落地的
  移动事件覆盖 —— 这是旧实现「还原后又跳一下」的根因）。
- 还原确认：80 次里 79 次一次确认到位；`restore_fail_index=[0]`（第一次点击时
  物理鼠标在场移动，2ms 确认窗口内被用户事件覆盖），`restore_unknown=0`。
- 轮询改造：`DeliveryWatch` 从 `time.sleep(0)` 自旋改为 `threading.Condition`
  广播唤醒，消除 GIL 5ms 切换造成的 ms 级尾巴（p95 1.595 → **0.843ms**）。

---

## 4. 不抢前台 / 不吞物理输入

| 检查项 | 结果 |
|---|---|
| 三条路径前台窗口 before→after | 66090 → 66090（全部 `fg_unchanged=true`） |
| `SetForegroundWindow`/等价调用 | 0 次（驱动路径不调用，仅 legacy/message 有各自逻辑） |
| 驱动 filter | 全槽 0（`filter_audit()`），不拦截物理输入 |
| 点击窗口内物理事件 | Raw `RIDEV_INPUTSINK` 持续收到真实设备事件；还原确认失败的一次即物理鼠标移动干扰的直接证据 |

---

## 5. 各类操作的最短安全间隔

方法：连续 send N 条，检查 Raw Input 层 down/up/move 计数与顺序是否**完全收齐**；
每个间隔 6 次试验（`rounds=20`）。

| 操作 | 间隔 0ms | 0.1ms | 0.2ms | 0.4ms | 0.6ms | 0.8ms | 1.0ms | 1.5ms | 2.0ms | 最短安全间隔 |
|---|---|---|---|---|---|---|---|---|---|---|
| 单击（连点） | 33% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | **0.1 ms** |
| 滚轮 | 100% | 100% | 100% | 100% | 100% | — | 100% | 100% | — | **0 ms**（单条即可） |
| 拖拽（8 步移动） | 33% | 100% | 100% | 100% | 100% | — | 100% | 100% | — | **0.1 ms** |

说明：0ms（同一批 send 不留间隔）时点击/拖拽只有 1/3 试验能收齐 down/up，
说明驱动发送队列在高密度连发时会合并/丢事件；**留 0.1ms 即 100% 收齐**。
生产路径 `_click_driver` 的连点/拖拽按 0.1ms 下限加间隔即可；单击本身不需要间隔。

结论（给引擎参数）：单击/连点最小间隔 `0.1ms`；滚轮无间隔要求；拖拽每步
`0.1ms`（8 步共约 0.8ms 额外延迟，可忽略）。

---

## 6. 通道身份可读（前端）

`channel_status()` 现返回：

```json
{
  "requested": "interception", "effective": "interception", "degraded": false,
  "reason_code": null,
  "identity": {"identity": "real_device", "device": 14, "vid": "3554",
               "pid": "F50D", "real": true, "device_source": "auto_pointer",
               "hardware_id": "HID\\VID_3554&PID_F50D&MI_02|…"},
  "occupancy": {"n": 80, "p50": 0.534, "p95": 0.843, "max": 2.565},
  "last_click": {"channel": "interception", "identity": "real_device",
                 "device": 14, "accepted": true, "restored": true, …}
}
```

`identity` 取值直接对应人话通道名（`channel_label`）：
`real_device → 真实设备注入`、`virtual_device → 虚拟设备注入`、
`legacy → legacy(借光标)`、`message → message(纯消息)`、`unavailable → 驱动不可用`。

---

## 7. 降级路径（不许静默）

| 情形 | 原因码 | 行为 |
|---|---|---|
| 驱动 DLL 缺失/加载失败 | `DRIVER_UNAVAILABLE` | 走 `input_fallback`（legacy/message/fail），日志 + `channel_status` |
| 驱动在但枚举不到真实鼠标 | `REAL_DEVICE_MISSING` | 同上；**不**回落到虚拟设备号 |
| 只枚举到虚拟设备且未开 `input_allow_virtual` | `REAL_DEVICE_MISSING` | 同上 |
| 真实设备枚举成功但 send 失败 | `DRIVER_SEND_FAILED` | 记录并降级/失败，`last_click` 留痕 |

`Mouse.__init__` 结束时主动读一次 `channel_status()` 并在「请求 ≠ 实际」时
`logging.warning`：`输入通道降级：… -> …，原因码=…，详情=…，策略=…`。

---

## 复现命令

```
python tools/bench_realmouse.py --mode devices    --out bench_devices.json
python tools/bench_realmouse.py --mode detect     --device 14 --out bench_detect.json
python tools/bench_realmouse.py --mode occupancy  --device 14 --n 80 --out bench_occupancy.json
python tools/bench_realmouse.py --mode intervals  --device 14 --out bench_intervals.json
```

工具全程只用隐藏的 `WS_POPUP` 观测窗（`RIDEV_INPUTSINK`）和 ≤3ms 的光标占用，
不弹可见窗口；每次点击结束立即还原并回读确认。
