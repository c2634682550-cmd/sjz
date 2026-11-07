# 输入链路三缺陷修复报告（`_input_fixes.md`）

分支范围：只改输入链路。涉及文件
`core/input.py`、`core/nocursor.py`、`core/config.py`、`core/switches.py`、
`core/preflight.py`、`tests/test_input_channels.py`（新增）。
未改 `core/engine.py`、`core/server.py`、`core/countdown.py`、`web/index.html`，
未 commit / push，未改用户 `config.json`，未在桌面开可见窗口、未动真实光标。

---

## 缺陷 1：消息通道没暴露成正式模式

### 根因
`core/nocursor.py` 里 `Clicker(strategy="message")` 已经是完整的
`PostMessageW` 纯消息点击，但 `core/input.py` 的 `Mouse` 只认
`interception`/`legacy`，`Mouse.click` 只有 legacy 分支才用 PostMessage，
且 `config.py` 与 `server.py` 只把这两个值当合法输入。纯消息通道从未接进
生产决策链。

### 改法
- `Mouse` 新增第三模式 `message`：`_click_message()` 直接走
  `nocursor.Clicker(hwnd, "message")`，全程不调用 `SetCursorPos`。
- 新增能力判定 `input.message_capability(hwnd)`（只读，不投鼠标消息）：
  - `usable`：窗口存在 + 线程在泵消息（`SendMessageTimeoutW(WM_NULL)` 成功）；
  - `functional`：对**游戏逻辑**是否有效。
    - `UnrealWindow` → `"unsupported"`：游戏按下时读真实光标位置
      （docs/auto-buy-design.md §4.8；branchA 实测 200/200 次窗口受理、
      游戏侧零响应），纯消息点击不会进游戏逻辑；
    - 其他窗口 → `"unknown"`：结构可用，但只有真机能证明游戏响应。
- `input_message_strict`（默认 `True`）：目标是 `UnrealWindow` 时按
  `input_fallback` 降级/失败；设 `False` 则只告警、照常投递。
- 每次点击后仍用 `WM_NULL` 同步往返校验窗口是否受理（失败给原因码）。
  即使调用方传 `verify=False`（导航旧代码为省时间），message 通道也强制校验
  —— 它的校验只有一次同步往返，很便宜。
- 键盘的 `wheel` 增加 message 分支（`WM_MOUSEWHEEL`，屏幕坐标，不碰光标）。

### 可用性矩阵（写进代码注释与报告，避免“假装能用”）
| 目标情形 | message 通道 | 依据 |
|---|---|---|
| 普通 Win32 控件/自绘界面，按窗口消息处理点击 | 可用（点击后校验受理） | 隐藏窗口实测 down/up 各 1 次 |
| 目标在按下时读真实光标位置 | 不可用（游戏逻辑零响应） | `_branchA_input_bench.md` 200/200 受理、游戏无响应 |
| 目标走 Raw Input | 不可用（同上） | 同上 |
| 目标完整性级别高于本进程（UIPI） | 不可用（`UIPI_BLOCKED`） | `WM_NULL` 被 UIPI 拦（err=5） |
| 目标窗口已退出/句柄失效 | 不可用（`WINDOW_GONE`） | `IsWindow` + `SendMessageTimeout` err=1400 |
| 目标线程忙/挂起 | 不可用（`TARGET_HUNG`） | `SendMessageTimeout` err=1460 |

---

## 缺陷 2：静默降级污染数据

### 根因
`Mouse.__init__` 在 `mode="interception"` 且 `driver_ready()` 失败时直接
`self._mode = "legacy"`，只存一个 `_drv_warn`，没有任何结构化记录，也没有
查询接口；外部把 legacy 的延迟/成功率当成驱动模式的结果。

### 改法
- **实际生效通道查询**：`Mouse.channel_status()` / `Keyboard.channel_status()`
  返回 `requested`、`effective`、`degraded`、`reason_code`、`reason`、
  `fallback_policy`、`integrity`、`message_capability`、`last_click`、`events`。
  另有模块级 `input.channel_events()` / `channel_last_event()`。
- **降级账本**：进程级 `_ChannelLedger`（线程安全，保留最近 200 条），
  每条含 `at`（时间戳）、`from`、`to`、`reason_code`、`reason`、`policy`、
  `ok`、`stage`；失败记录 `to=None, ok=False`。降级/失败不再静默。
- **可配置策略**：`input_fallback` ∈ `legacy`（默认，旧行为）/ `message` /
  `fail`。`fail` 时构造期直接抛 `InputChannelError`（带根因码），不换通道。
- 键盘复用同一套决策（`_decide_channel`），驱动不可用时同样降级并记账。
- 配置兼容：`DEFAULTS` 新增 `input_fallback`、`input_message_strict`；
  `Config._normalize` 归一化，老 `config.json` 缺字段自动取默认值。

---

## 缺陷 3：UIPI 下的静默失效

### 根因
- `SetCursorPos` 在 UIPI/ClipCursor 下可能返回 0 且 `GetLastError()==0`，
  光标不动、没有任何可读错误；旧代码只看返回值、没有回读坐标。
- `PostMessage` 在完整性不匹配时也可能“看起来成功”，目标收不到。
- 构造时从不比较自身与目标窗口的完整性级别，走到点击才发现点不动。

### 改法
1. **构造期主动判定**（legacy/message 通道）：`Mouse._gate_integrity()` 比较
   `process_integrity()` 与 `process_integrity(hwnd)`；目标 RID 更高直接抛
   `InputChannelError(R_UIPI_BLOCKED)`，不进点击循环。驱动通道不做此判定
   （内核注入不受 UIPI 限制）。
2. **legacy 每次点击的真实性校验**：
   - `SetCursorPos` 返回 0 → `CURSOR_NOT_MOVED`（消息文本附带“err=0 时典型是
     UIPI/ClipCursor”）；
   - 返回成功但立即 `GetCursorPos` 回读不在目标（±2px）→
     `CURSOR_NOT_AT_TARGET`（不投任何消息，直接失败）；
   - 消息投递后再回读一次（无 sleep，不加延迟）。
3. **message 每次点击的受理校验**：投递后 `SendMessageTimeoutW(WM_NULL)`；
   0+err=5 → `UIPI_BLOCKED`，0+err=1460 → `TARGET_HUNG`，0+err=1400 →
   `WINDOW_GONE`；`PostMessageW` 返回 0 → `POST_REJECTED`。
4. **legacy 投递失败**：`PostMessageW` 返回 0 → `POST_REJECTED`
   （err=5 → `UIPI_BLOCKED`）。

### 稳定原因码（主会话按码写文案，不要解析 message 文本）
| 原因码 | 含义 |
|---|---|
| `DRV_UNAVAILABLE` | 驱动没装/服务没起/被杀软清掉 |
| `DRV_SEND_REJECTED` | 驱动受理失败（send 返回 0 或抛异常） |
| `FALLBACK_DISABLED` | 降级目标与请求通道相同，或策略禁止 |
| `UNKNOWN_MODE` | 配置里的通道名不认识，按 interception 处理 |
| `UIPI_BLOCKED` | 目标完整性更高，输入会被静默丢弃（需提权） |
| `INTEGRITY_UNKNOWN` | 读不到完整性级别（信息级，不阻断） |
| `NO_TARGET_HWND` | 没有目标窗口句柄 |
| `WINDOW_GONE` | 窗口已退出/句柄失效 |
| `TARGET_HUNG` | 目标线程 50ms 内不响应消息 |
| `POST_REJECTED` | `PostMessageW` 返回 0（队列拒收） |
| `MESSAGE_NOT_ACCEPTED` | 消息已投递但目标未受理 |
| `MESSAGE_RAW_CURSOR_UNSUPPORTED` | 目标读真实光标，纯消息不进游戏逻辑 |
| `CURSOR_NOT_MOVED` | `SetCursorPos` 失败（含 err=0 的静默场景） |
| `CURSOR_NOT_AT_TARGET` | 光标没停在目标（被 UIPI/裁剪锁/人抢） |

`InputChannelError` 继承 `MouseStolen`，engine 现有「鼠标被抢 → 停」的安全
路径无需改接线即可接住；根因在 `e.reason_code` / `e.channel` / `e.detail`。

---

## 验证

### 方式一：单元/纯逻辑测试（新增 20 条）
`python -m pytest tests/test_input_channels.py -q` → **20 passed**
（另跑相关回归 `test_input_abi / test_input_interception / test_fix_c /
test_fix_e / test_config / test_switches / test_preflight / test_console_api /
test_nav / test_nav_scroll`：**113 passed, 2 skipped**）。
覆盖：三模式配置归一、老配置兼容、降级记账、fail 直失败、键盘同策略、
UIPI 构造期拦截、`SetCursorPos` 返回 0 / 返回成功但光标不动、message 投递
被拒、点击后 UIPI 校验、UnrealWindow 严格降级/硬失败/告警模式、
无句柄报 `NO_TARGET_HWND`。

### 方式二：真实 Win32 API（隐藏窗口，非 mock）
在本进程注册隐藏 `WS_POPUP` 窗口（不显示、不抢焦点）并泵消息，实测：
```
cap_before: usable=true, functional=unknown, window_class=DaforsMsgProbeWindow
click_ok=true   hits: down=1 up=1 move=1
cursor_before=(958,942)  cursor_after=(958,942)  cursor_moved=false
effective=message, reason_code=null, degraded=false
DestroyWindow 后：usable=false, reason_code=WINDOW_GONE
```
即真实 `PostMessageW` 投递进目标窗口的消息队列、窗口确实收到 down/up，
系统光标零位移；句柄失效后能力判定如实变红。

### 全量回归
`python -m pytest tests -q`：输入相关用例全绿。失败项全部属于其他并行分支/
本机环境，与本分支无关：
- `test_countdown.py`（4 条）：`core/countdown.py` 正由倒计时分支改动中，
  本分支按要求未碰；
- `test_frontend_static.py`（7~8 条）：针对 `web/index.html`，前端分支改动中
- `test_ocr_price.py`（1 条）：本机 OCR 模型不可用。
（共享工作区里其他分支每次落盘都会让总数小幅浮动；最近一次为
1115 passed / 131 skipped / 13 failed，失败项均不属于输入链路。）

---

## 主会话接线清单（需要在别的分支完成）

1. **`core/server.py`**（禁止我改）：
   - `_as_input_mode` 目前只放行 `("interception", "legacy")`，必须加
     `"message"`，否则 `POST /api/config` 会把第三通道丢掉。
   - `_CONFIG_COERCERS` 增加 `"input_fallback": <str 枚举校验>` 与
     `"input_message_strict": _as_bool`，否则界面拨了不落盘。
2. **`core/engine.py`**（禁止我改）：启动时把 `mouse.channel_status()`
   （至少 `requested/effective/reason_code/events`）写进日志/状态接口；
   引擎向 `Keyboard(...)` 传原始 `input_mode` 即可，Keyboard 现在自己会
   降级并记账；若想避免重复探测，可传 `mouse.mode`。
3. **UI（`web/index.html`）**：输入通道下拉加第三项
   `message`（纯消息投递）。`core/switches.py` 已提供 `options` 与
   `input_fallback` / `input_message_strict` 两条开关，接口读 switch 表即可。
4. **文案映射**：按上表原因码给用户文案（8 种主因，`INTEGRITY_UNKNOWN`
   只提示不阻断）。
5. **倒计时**：本分支未碰，按原计划等另一分支审查结论。

## 注意
- `message` 通道在 `UnrealWindow` 上默认**不可用于抢购**（严格模式按
  `input_fallback` 处理）；这是实测事实，不是保守设计。
- `channel_events()` 是全局账本（最多 200 条），跨 Mouse/Keyboard 共享。
- legacy 的每次点击会在 `SetCursorPos` 后立即回读光标；用户/其他程序在
  这 40ms 内移动鼠标会被判为 `CURSOR_NOT_AT_TARGET`（这是有意的防替身
  校验，和 failsafe 语义一致）。
- 驱动通道不做完整性预判（不受 UIPI 约束），其「受理」凭据是三条
  `send_mouse` 的返回值；游戏逻辑是否响应仍需真机验证，不在本分支伪造。
