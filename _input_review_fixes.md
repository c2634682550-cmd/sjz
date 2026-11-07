# 输入链独立复核返工（F2 / F7 / F8 / F5）

复核结论：`message` 通道在唯一有用的场景里失效（阻塞），降级无痕、状态位与日志不一致、
`/api/config` 收不到新通道。以下四项已改完，并给出三类独立证据。

---

## F2（阻塞）：message 通道被前台校验挡死

### 根因
`Mouse.click()` 里 `expect_foreground` 校验排在通道分发**之前**（原 `input.py:598-610`），
`engine.py:542` 每次都传 `expect_foreground`。于是：

- `message` 通道也必须先 `ensure_foreground` 抢一次前台；
- 抢不回来直接抛 `WindowNotForeground`，整场任务停住；
- 而这个通道的唯一价值就是「不碰前台、UIPI 下也能投递」——
  在它唯一有用的场景（游戏在前台、控制台在后）里直接失效。

### 改法
1. `message` 分支前移到前台校验**之前**，并跳过校验（`input.py` `click()`）。
2. 前台不匹配不再静默也不抢：作为可观测状态写进 `_last_click`：
   `{"channel":"message", "foreground_ok": True/False, "functional": ..., "accepted": ...}`。
3. `legacy` / `interception` 保留前台校验（它们需要真实光标落在目标窗口上，
   光标被盖住时点了也白点）。
4. `_click_message()` 新增 `expect_foreground` 参数，只观测不动作。

### 证据 A：静态审计
```
core/input.py:644   if not self.ensure_foreground(expect_foreground):   # 位于 message 分支之后
core/input.py:904   def ensure_foreground(...)
core/input.py:940       _user32.SetForegroundWindow(hwnd)               # 仅被 ensure_foreground 调用
```
`message` 通道路径上不存在 `ensure_foreground` / `SetForegroundWindow` 调用点。

### 证据 B：运行时实证（`tools/_verify_f2.py`，跑完即删）
| 目标 | 是否前台 | effective | accepted | 前台 before→after | 光标 | SetForegroundWindow 调用数 | foreground_ok |
|---|---|---|---|---|---|---|---|
| Progman 66090 | 是 | message | true | 66090 → **66090** | (80,80)→(80,80) | **0** | true |
| Shell_TrayWnd 66050 | **否** | message | true | 66090 → **66090** | (80,80)→(80,80) | **0** | **false** |

第二行是关键场景：目标不是前台窗口，旧实现会先抢前台（或抛错），
现在点击照常投递、前台纹丝不动、状态位如实写 `foreground_ok=false`。
单次点击耗时 0.071–0.073 ms，全程零光标位移。

### 证据 C：单测
`tests/test_input_review_fixes.py::test_message_click_skips_foreground_guard`
`::test_message_click_reports_foreground_ok_when_matching`
`::test_legacy_click_still_guards_foreground`

---

## F7：降级不留痕 + `channel_status()` 无调用点

### 根因
- `_degrade()` 只把事件塞进内存账本 `LEDGER`，没有日志输出；
- `channel_status()` 在生产代码里从来没有调用点，状态位等于拿不到。

### 改法
1. `_degrade()` 末尾统一 `logging.warning`：
   `输入通道降级：<请求> -> <实际>，原因码=<code>，详情=<why>，策略=<policy>`。
   `message → legacy` 与其它降级走同一条路径，不再有静默分支。
2. `Mouse.__init__` 末尾主动 `self._init_status = self.channel_status()`，
   并在「实际通道 ≠ 请求通道 或 有原因码」时再打一条 `输入通道生效=...（请求=...）`。
3. `channel_status()` 返回体保留 `requested / effective / degraded / reason_code /
   reason / fallback_policy / events`，上层随时可读。

### 证据
单测 `test_degrade_is_logged_and_status_is_queried`：把 `_driver_probe` 打成不可用，
断言 `mode == "legacy"`、`channel_status().degraded is True`、
`reason_code == DRIVER_UNAVAILABLE`、`_init_status` 是 dict（构造期确实调过），
且 `caplog` 里同时出现「输入通道降级」和具体原因文本。

---

## F8：`_input_mode` 不回写 + 日志把 message 写错

### 根因
`engine.py:556-565` 只把 `driver_warning` 当降级信号：
请求 `message` 却降级到 `legacy` 时不报警；日志固定写
`'驱动注入(interception)' if eff == 'interception' else 'PostMessage(legacy)'`，
于是 `message` 永远被写成 legacy；`self._input_mode` 保留的是请求值而非实际值，
后续键盘/日志都按错通道走。

### 改法
1. `self._input_mode = eff` —— 回写**实际生效**通道。
2. 文案改用 `mouse.channel_label`（`input.py` 新增 property，按
   `channel_status().identity` 映射：真实设备注入 / 虚拟设备注入 / legacy(借光标) /
   message(纯消息) / 驱动不可用）。
3. 降级时日志同时带上实际通道与原因：`输入通道：message(纯消息)（已降级：…）`。

### 证据
- 单测 `test_input_mode_label_follows_effective_channel`：
  message → `"message(纯消息)"`，legacy → `"legacy(借光标)"`。
- `test_degrade_is_logged_and_status_is_queried` 同时覆盖 `_input_mode` 取值来源
  （`mouse.mode`）。

---

## F5：`/api/config` 拒收 message / 新配置项

### 根因
`server.py:121-122` 的 `_as_input_mode` 白名单只有 `interception/legacy`；
`129-143` 的 `_CONFIG_COERCERS` 完全没有 `input_fallback` / `input_message_strict`。
前端存 `message` 会被 coerce 成 `None` 然后**静默丢弃**（没有任何提示）。

### 改法（只动 `/api/config` 这一段）
1. `_as_input_mode` 收 `interception / legacy / message`，大小写归一。
2. 新增 `_as_input_fallback`（`legacy / message / fail`）。
3. 新增 `_as_strict_bool`：只认 `true/false`、`1/0`、`on/off`、`yes/no`、`是/否`；
   `"maybe"` 不再被 truthy 蒙成 `True`。
4. `_CONFIG_COERCERS` 增加 `input_fallback`、`input_message_strict`。
5. 落盘前对三项前置校验，非法值整单 `400` 并给可读原因：
   `配置值不合法：input_mode='telepathy' 不合法，可选：interception / legacy / message`。

### 证据
- 单测 `test_config_api_accepts_message_and_new_keys`
- 单测 `test_config_api_rejects_bad_values_with_readable_reason`（三条非法值均 400 + 可读 msg）

---

## 回归

```
python -m pytest tests/test_input_channels.py tests/test_input_review_fixes.py \
                 tests/test_input_interception.py tests/test_input_abi.py \
                 tests/test_server.py tests/test_console_api.py \
                 tests/test_config.py -q
```

（结果见下方回填）

## 主会话接线须知

| 位置 | 变化 |
|---|---|
| `Mouse.click(..., expect_foreground=...)` | message 通道不再抢前台；返回 `accepted` 同前 |
| `Mouse.channel_status()` | 新增 `identity` / `occupancy`，`last_click.foreground_ok` 可用于提示「目标不在前台」 |
| `Mouse.channel_label` | 一句人话通道名，直接上日志/前端 |
| `Mouse.mode` | 实际生效通道（`engine._input_mode` 已回写） |
| `POST /api/config` | 收 `input_mode=message`、`input_fallback`、`input_message_strict`；非法值 400 |

---

# I2：窗口受理消息但不处理 = 假成交（优先级最高）

## 根因
旧 `_post_click` 的三条鼠标消息走 `PostMessageW`，之后只做一次
`SendMessageTimeoutW(WM_NULL)`（`window_responsive`）。`WM_NULL` 只证明
**目标线程在泵消息**，不证明它处理了 `WM_LBUTTONDOWN`。于是目标窗口
（游戏读 Raw Input、自绘面板不理鼠标消息）会出现：

```
click=True   accepted=True   functional=unknown
```

上层据此记账 = 一次根本没发生的点击被当成成交。

## 改法
1. **投递改为同步**（`core/nocursor.py::_deliver`）：三条消息改走
   `SendMessageTimeoutW`，目标窗口过程**真的执行过**才算 `accepted`。
   三种失败各有独立原因码，不再退化成同一个 `0`：
   | err | 原因码 | 含义 |
   |---|---|---|
   | 1460 `ERROR_TIMEOUT` | `TARGET_HUNG` | 挂起/死循环/没处理 |
   | 5 `ERROR_ACCESS_DENIED` | `UIPI_BLOCKED` | 完整性级别更高 |
   | 1400 `ERROR_INVALID_WINDOW_HANDLE` | `WINDOW_GONE` | 窗口已销毁 |
   | 其它非 0 | `MESSAGE_NOT_ACCEPTED` | 投递失败 |
2. **返回值语义分层**（`Clicker.last_result` / `Mouse._last_click`）：

   | 字段 | 含义 | 成功判定 |
   |---|---|---|
   | `accepted` | 消息被目标窗口过程执行 | 不算成功 |
   | `processed` | 有目标侧证据：窗口状态变化 / 按下态 `hwndCapture` / 上层探针 | **只有 True 才算成功** |
   | `success` | `processed is True` | 真·成交 |
   | `retry_suggested` | `processed is None` | 上层应降级或重试 |

3. **证据采集**（不靠 `PostMessage` 非 0）：
   - `window_signature()` 回读目标窗口：矩形 / 标题 / 可见 / 启用 / `GWL_STYLE`
     + `GetGUIThreadInfo` 的 `active / focus / capture`；
   - `down` 与 `up` 之间采样一次（按钮类窗口按下时会 `SetCapture`，抬手释放），
     这是「目标真的处理了按下」最便宜的目标侧证据；
   - `Clicker(effect_probe=...)` 允许上层注入自己的「生效」探针。
   - 三者都没有 → `processed=None`、返回 `False`、原因码 `MESSAGE_UNPROVEN`，
     **绝不把 unknown 当成功**。
4. **上层记账**（`core/nav.py::_click_at`）：`click()` 返回 `False` 时区分
   「没送出去」（抛 `NavError`，保持 review I8 语义）与「送出去了但未证实」
   （不抛错、不记成功，交给既有的画面差 `_diff` 判定，并打 warn）。

## 结果码对照（用例）
`tests/test_input_message_processed.py`：

| 场景 | 返回 / 异常 | `accepted` | `processed` | 原因码 |
|---|---|---|---|---|
| 受理但不处理 | `False` | `True` | `None` | `MESSAGE_UNPROVEN` |
| 正常处理（标题变化） | `True` | `True` | `True` | — |
| 上层探针判定生效 | `True` | `True` | `True` | — |
| 目标销毁 | `WINDOW_GONE` | `False` | `False` | `WINDOW_GONE` |
| 目标挂起 | `TARGET_HUNG` | `False` | `False` | `TARGET_HUNG` |
| UIPI 拦截 | `UIPI_BLOCKED` | `False` | `False` | `UIPI_BLOCKED` |

---

# N9/E7：`10**400` 溢出 → 可读 4xx（不许 500）

## 根因
- `core/server.py::_as_int` 用 `int(float(value))`：`float(10**400)` 直接抛
  `OverflowError`，`except` 只接 `TypeError/ValueError` → 未捕获 → 500；
  `"1e400"` → `float=inf` → `int(inf)` 同样 `OverflowError`。
- `core/validate.py::_number/_real` 同样的 `float()` 写法。
- `_json` 用默认 `json.dumps`，`Infinity` 会写出非法 JSON（前端 `JSON.parse` 失败）。

## 改法
1. `_as_int/_as_float` 接 `OverflowError`，并用 `math.isfinite` 拒绝 `NaN/Inf`。
2. `_save_config` 对数值键（`threshold/click_hz/confirm_clicks/
   confirm_interval_ms/fire_timeout_s/monitor/arm_grace_s/list_max_tries`）
   前置 `_overflow_reason()`：溢出/非有限值 → 整单 `400`，原因可读且截断超长
   repr（`threshold=1000… 不合法：数值超出可表示范围（太大）`）。
3. `validate._number/_real` 接 `OverflowError` → 变成 `errors` 里的可读条目
   （`check_conditions` 返回给接口，自然 400）。
4. `Handler._json` 用 `allow_nan=False`，序列化异常兜底成 `400`，不再 500。

## 证据
`tests/test_server_overflow.py`：
- `threshold=10**400` → `400` + `超出可表示范围`
- `click_hz / fire_timeout_s / monitor / list_max_tries = "1e400"` → `400` + `不是有限值`
- `check_conditions({"min_price": 10**400})` → 返回可读错误（不抛异常）
- `_json({"x": inf})` → `400` + `无法序列化`

---

## I2 回归

```
python -m pytest tests/test_input_channels.py tests/test_input_review_fixes.py \
                 tests/test_input_message_processed.py tests/test_server_overflow.py -q
37 passed
```

全量回归（`python -m pytest tests -q`）：

```
1689 passed, 37 failed, 133 skipped, 2 xfailed, 12 errors in 450.27s
```

37 failed / 12 errors **全部**落在 `test_license.py` / `test_license_server.py`
（公钥长度 32 vs 64，另一分支的密钥材料问题）与 `test_ocr_price.py`
（本机缺 OCR 模型），与本轮改动（input / nocursor / nav / server / validate）无关；
本轮触达模块的单测全部通过。
