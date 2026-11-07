# R03 专项审查：数值边界与时间基准

- 目标：`C:\Users\26346\Desktop\project\dafors`
- 范围：**只查** NaN / ±inf / 负数 / 零 / 极值进入计算路径、除零、整数溢出、浮点累积、`round`/`int` 截断方向、货币价格精度、时间源混用（QPC / `perf_counter` / `time.time` / 系统时钟）、NTP 跳变与手动改时、单调时钟回绕、壁钟超时、毫秒/秒单位、配置里的字符串数字。
- 方式：静态读码 + 纯函数级模糊测试（未启动引擎、未抓屏、未动光标、未改系统时间、未联网校时）。复现全部用 monkeypatch / 假 socket / 假时钟，**没有写入项目内的运行时数据**。
- 代码未改动，未 commit、未 push。

## 0. 先看工作区状态（重要）

`git status` 显示以下文件处于**未提交修改态**（当前分支工作区是脏的），本次审查以磁盘上的现行代码为准：

```
M core/config.py  core/countdown.py  core/engine.py  core/records.py
M core/scheduler.py  core/server.py  core/switches.py  core/input.py
M core/ocr_service.py  core/preflight.py  core/interception.py
M web/index.html  desktop.py  build.bat  sjz.spec  + 一批 tests/*
?? core/license.py  core/precision.py  core/recovery.py  core/retention.py
?? core/scene_guard.py  core/watchdog.py  core/nocursor.py  core/selfcheck.py
```

- 下文每条都标了 `【改动态】` 或 `【新增未提交】`。
- 其中 **N02** 命中的 `scheduler.py` 第 670 行附近与 `engine.py:227` 属于本次改动新增的行，如果这条改动还在别人手上，请当作「进行中的实现」而非已上线回归。
- 其余命中的行不在本次 diff 内（`git diff` 已核对），是既有代码。
- `market.py`、`ocr_chain.py`、`nav_col.py`、`screen.py`、`timesync.py` 不在改动清单里（干净文件）。

## 1. 结论摘要

| 级别 | 数量 | 编号 |
|---|---|---|
| 高 | 3 | N01 N02 N03 |
| 中 | 7 | N04 N05 N06 N07 N08 N09 N10 |
| 低 | 8 | N11 N12 N13 N14 N15 N16 N17 N18 |

最该先修的三条：**N01（一条脏 NTP 应答把整个时间基准打到 1970）**、**N02（`fire_at=NaN` 直接变成"立刻开抢"，`inf` 变成永不执行）**、**N03（`threshold=NaN` 让引擎静默永不识别按钮）**。这三条共同点：不报错、不崩溃，只是**安静地不工作 / 提前工作**。

---

## 2. 高

### N01 一条 48 字节脏 NTP 应答 → 时间基准偏移 −126.8 年，delay=0 还被当成最优样本

- 严重度：**高**
- 位置：`core/timesync.py:38-47`（解析应答，`stratum` 只回传不使用、offset 无合理性校验、也不校验 origin 时间戳）、`core/timesync.py:67`（`sync()` 取 delay 最小）、`core/timesync.py:81-89`（`offset()` 直接用）
- 输入样例：任意主机向 123 端口回一个 48 字节全零报文（`b"\x00"*48`；stratum 字段 = 0，transmit/receive 时间戳 = 0）
- 实际输出：
  ```
  {'offset': -4002991886.772255, 'delay': 0.0, 'stratum': 0, 'server': '127.0.0.1'}
  ```
  `delay=0.0` 会让这一条成为 `sync()` 里 delay 最小的「最佳样本」，而且它**只发一次包**（`_query` 里没有多次采样/剔除离群）。结果是 `true_now() = time.time() - 4.0e9 ≈ 1970 年`：
  - `scheduler.py:658` 的 `now` 变负 → `now < fire_at - 20` 恒真，每 30s 无限轮询 → **所有定时任务永不触发**，界面只显示"等待"，没有错误。
  - `engine.py:227` 的 `deadline - true_now()` 变成一个 +4e9 的巨值 → `_deadline` 等于"127 年后"，引擎启动后一直在等开卖。
- 期望：`_query` 里校验 `stratum in 1..15`（0=KoD 无效）、`|offset| <= 86400`、`|delay| <= 5`；不合法丢弃该样本；`sync()` 至少在 3 个样本里做中位数/离群剔除，失败则保留旧 offset（stale）。
- 备注：这是"极小/极大值进入计算路径"的最典型一条。真实可达性取决于 123 端口上有什么在应答（见"无法验证"）。

### N02 `fire_at = NaN` → 立即布防、立刻开抢；`fire_at = inf` → 永不执行且不报错

- 严重度：**高**　`【改动态】`
- 位置：`core/scheduler.py:62`（`_fire_at` 只 `float()`，不校验有限性）、`core/scheduler.py:655-668`（布防判定）、`core/server.py:432`（`fire_at=data.get("fire_at") or 0` 原样下传）、`core/engine.py:227-228`（`max(0.0, deadline - true_now())`）
- 输入样例（走 HTTP，不需要改配置文件）：
  ```json
  POST /api/task/add   {"name":"x","fire_at":"nan"}
  POST /api/task/add   {"name":"x","fire_at":1e400}
  ```
- 实际输出：
  - `"nan"` → `_fire_at = nan`，`nan` 是真值，`now < nan - 20` 为 **False** → 立刻置 ARMED → 引擎拿到 `deadline=nan` → `max(0.0, nan - true_now())` 返回 **0.0** → `_deadline = perf_counter()` → 循环不等待，**当场进入连点**（50Hz × 8s 的空点，可能触发游戏限流，也可能抢在开卖前把配额/预算耗掉）。
  - `1e400` / `"1e400"` → JSON 解析成 `inf` → `_fire_at = inf` → `now < inf` 恒真 → 每 30s 醒一次，**永久等待**，无异常、无状态变化。实测：`_fire_at({"fire_at":"1e400"}) -> inf`。
- 期望：入队时校验 `math.isfinite(fire_at)` 且落在 `[now-86400, now+365*86400]`；`NaN`/缺失一律按 0（手动触发）；不合理返回 400。`engine.start` 里也要 `isfinite` 兜底（`max(0.0, nan)` 这个写法把 NaN 静默变成"现在"）。

### N03 开关里的 `NaN` / 负数 / `Infinity` 无校验，直接落进引擎

- 严重度：**高**（静默失效面很宽）
- 位置：`core/switches.py:377-381`（`float` 分支只有 `try float(value)`，没有 `isfinite`、没有区间）、`core/switches.py:372-376`（`int` 分支 `int(float(value))` 不捕获 `OverflowError`）；消费端 `core/engine.py:619`（threshold）、`:1411-1412`（scan interval/timeout）、`:1472`、`:1486`、`:1727`、`:1759`、`:1815`
- 输入样例与实测归一化结果：
  ```
  fire.threshold = "nan"   -> nan          （而 hint 写的是 0~1）
  fire.threshold = "1e400" -> inf
  fire.threshold = -0.2    -> -0.2
  scan.interval  = -0.05   -> -0.05
  fire.timeout    = "inf"  -> inf
  follow.retry    = "inf"  -> OverflowError: cannot convert float infinity to integer
  price.min       = -5     -> -5
  ```
- 实际后果：
  1. `threshold = NaN` → `engine.py:619/1410` 后续所有 `cscore >= threshold` 恒为 False → **引擎永远认不出购买按钮/确认弹窗**，界面表现"到点了但什么都没发生"，日志无报错。这是最隐蔽的一种。
  2. `scan.interval = NaN/-0.05` → `engine.py:1486/1544` 的 `time.sleep(scan_interval)` 抛 `ValueError: Invalid value NaN` / `sleep length must be non-negative` → **任务线程崩**。
  3. `fire.timeout = inf` → `engine.py:1759` 的 `while t - t_fire < fire_timeout` 无上限 → 只要确认弹窗不出现就**无限连点购买**。
  4. `follow.retry = inf` → `int(inf)` 抛 `OverflowError`，而 `_coerce` 只捕获 `TypeError/ValueError` → 整个 `/api/switches` 保存 500，**同一次提交里其他合法字段也一起丢失**。
  5. `countdown.min_lead = -3` → `0 < sec < -3` 恒 False → "最短倒计时"限制静默消失。
- 期望：`_coerce` 对 `int/float` 统一做 `isfinite` + spec 里补 `min`/`max`（threshold 0~1、interval ≥0、retry 0~5、lead ≥0），越界 400 并指明字段；`int` 分支补 `except OverflowError`。

---

## 3. 中

### N04 `/api/config` 数值归一：`inf` 抛 500、`nan` 静默落盘，并写坏 JSON

- 严重度：**中**
- 位置：`core/server.py:97-101`（`_as_int`）、`:104-108`（`_as_float`）、`:128-142`（`_CONFIG_COERCERS`）
- 输入样例 / 实测：
  ```
  _as_int(inf)      -> OverflowError: cannot convert float infinity to integer   （未捕获 → 500）
  _as_int("1e400")  -> OverflowError                                          （未捕获 → 500）
  _as_int(nan)      -> None   （ValueError 被吞，字段被当成"没给"，静默不改）
  _as_int(3.9)      -> 3      （int(float) 向零截断，不是四舍五入）
  _as_float(nan)    -> nan    （原样接受）
  _as_float(inf)    -> inf
  ```
- 后果：`{"threshold":"nan"}` 保存成功，`config.json` 里就是 `NaN`；`json.dump` 默认 `allow_nan=True` 会写出非严格 JSON（`NaN` / `Infinity`），Python 还能读回来，**浏览器的 `JSON.parse` 会直接抛** → 整个设置页读配置失败。`_as_int` 遇到 `inf` 抛的 `OverflowError` 不在 `except (TypeError, ValueError)` 里，只能落到外层 500。
- 期望：两个归一函数都加 `isfinite`；`_as_int` 捕获 `OverflowError`；持久化时 `allow_nan=False`，并在写盘前拒绝非有限值。

### N05 `offset()` 的 TTL 用壁钟：时钟回拨后旧偏移永不过期，前跳则立刻掉回 0

- 严重度：**中**　（时间基准，被 N01 放大）
- 位置：`core/timesync.py:89`、`:100`（`(time.time() - at) > OFFSET_TTL_S`，`OFFSET_TTL_S = 6*3600`，`at` 也是用 `time.time()` 记的，见 `:64/:72`）
- 输入样例 / 实测（注入 `offset=123.456`，然后只伪造 `time.time()`）：
  ```
  回拨 365 天 -> offset() = 123.456   （应为 0，已 stale）
  回拨 60 秒  -> offset() = 123.456   （应为 0）
  前跳 3 天   -> offset() = 0.0       （立即失效，补偿瞬间丢失）
  ```
  回拨后 `time.time() - at` 为负数，永远不 `> TTL`，于是**过期偏移一直生效**（`true_now()` 持续偏早，调度继续用错时间）；前跳则相反，6 小时的 TTL 一秒都用不到。
- 期望：用独立单调源记 `at`（`time.monotonic()`/`perf_counter()`），TTL 与 `at` 都放在单调轴上；壁钟只用于算 offset 本身。

### N06 价格解析取"最后一个数字串"：千分位被空格拆开 / 带小数时价格缩 10~1000 倍

- 严重度：**中**（价格路径核心；预算闸门会因此失效）
- 位置：`core/ocr_chain.py:26`（`_PRICE_TOKEN = \d+(?:,\d+)*`）、`:42-45`（`m = matches[-1]`）、`:56`（长度阈 1~9）
- 输入样例 / 实测：
  ```
  price_from_text("¥12 345")   -> (345, '345')     期望 12345 或拒识
  price_from_text("¥1,234.5")  -> (5, '5')         期望 1234.5 或拒识
  price_from_text("1,234.567") -> (567, '567')     期望 拒识
  price_from_text("-5")        -> (5, '5')         期望 拒识（负号被丢）
  price_from_text("¥15,000")   -> (15000, ...)     正确（对照项）
  ```
- 后果：确认框读价（`engine` 里的预算闸门）会拿 345 / 5 去和预算比较 → **预算上限形同不存在**；同时 5 元这种"极低价"在按价升序里排第一（见 N07）。
- 期望：对"最后一个 token"的做法加两条约束：① 若文本里存在被空格/点号拆开的相邻数字段，先合并或直接判"没读全"→None；② `_PRICE_TOKEN` 不接受小数点，包含小数的字符串整体按噪声处理；② 长度 < `_MIN_PRICE` 的位数直接 None。

### N07 带 `¥` 的价格分支没有下限、允许小数；0/5 元假价会被 `pick()` 优先选中

- 严重度：**中**（价格路径）
- 位置：`core/market.py:130-132`（`m = _CURRENCY.search(...)` → `price = _to_number(...)`，**没有 `_MIN_PRICE`、没有整数校验**）、`:152-155`（只有无 `¥` 分支才 `>= _MIN_PRICE`）、`:558-582`（`pick()` 按价格升序）
- 输入样例 / 实测：
  ```
  parse_row("¥5 崭新 0.01")     -> price 5.0    （_MIN_PRICE=100 只对无 ¥ 分支生效）
  parse_row("¥0 崭新 0.01")     -> price 0.0
  parse_row("¥15.5 崭新")       -> price 15.5   （价格变成小数，影响 budget/spent 比较）
  parse_row("-15000 崭新")      -> price 15000.0（负号被丢，按正价买）
  pick([15000, 3000, price=0.0]) -> 选中 price 0.0 那行
  ```
- 期望：`¥` 分支同样要求 `>= _MIN_PRICE`；价格必须是整数（游戏价格无小数），出现小数/负号按"价格不可信"置 None；`pick()` 对 `price <= 0` 直接跳过。

### N08 多处超时用壁钟（`time.time()`）：回拨会卡死，前跳会提前放弃

- 严重度：**中**
- 位置：
  - `core/nav_col.py:1001-1003` `_wait_title_change`（1.2s 等标题变化）
  - `core/nav_col.py:1090-1091` `_wait_skin_list`（6s 等列表刷新）
  - `core/screen.py:37-47` `wait_frame_change`（等画面变化，返回值也是壁钟差）
  - `core/server.py:520-521` `_wait_job`（120s 同步等待，超时提示"等太久了"）
  - `core/jobs.py:30/45/75`（`elapsed` 用壁钟差值）
  - `core/nav_ons.py:885-886`（60s 告警节流）
- 输入样例：运行中把系统时间向回拨 60 秒（或 NTP 步进）
- 实际输出：
  - 回拨：`time.time() - t0` 变负 → 循环条件继续成立，实际等待被拉长到 **61.2s / 66s / 180s**，即"卡死到墙钟追平"；导航线程停在这里，整个抢购流程不前进。
  - 前跳：条件立刻不成立 → 返回 `None`/旧标题 → 把**正确的卡片判成"不是它"**而跳过（`nav_col` 注释里 review I5 描述的正是要避免这种情况）。
  - `jobs` 的 `elapsed` 可能为负，界面显示"用时 -58.2s"。
- 期望：全部换 `time.monotonic()`（或 `perf_counter`），与 `countdown`/`watchdog` 保持一致；`screen.wait_frame_change` 的返回值也应是单调差。

### N09 倒计时解析丢负号、把小数当整数放大（`1.999秒` → 999 秒）

- 严重度：**中**（时间路径；会造成整场空等）
- 位置：`core/countdown.py:71`（`_PAT = ...(\d+)\s*秒`，无小数点/符号处理）、`:96-102`（`int(m.group(3))`）
- 输入样例 / 实测：
  ```
  _parse_text("-5秒")   -> 5
  _parse_text("1.999秒") -> 999
  _parse_text("0.5秒")  -> 5
  parse_words([{"text":"1.999秒"}]) -> 999（走 _parse_text 兜底）
  ```
- 后果：小数/负号读数不会被拒识，而是**静默变成更大的正数**。若 OCR 把 `0` 后面的一段噪声识别成 `.5`，`0.5秒` 就变 5 秒，`Clock` 把归零时刻推后 4.5s，开卖点已经过了；`-5秒` 会被当成"还剩 5 秒"正常等待。
- 期望：`_PAT` 匹配到小数点或前导 `-` 时，要么按小数解析（`float`），要么整体判"没读全"返回 None；绝不让 `\d+` 跳过小数点只吃尾段。

### N10 `countdown.Clock.observe` 接受 NaN/Inf → 归零时刻变 NaN，永远不到点，且兜底不触发

- 严重度：**中低**（当前 OCR 通路产不出 NaN，属防御缺口；一旦上游换成浮点解析就会中招）
- 位置：`core/countdown.py:191-212`（`if seconds is None or seconds < 0`，NaN 两个判断都为 False → 收下）、`:211`（`t + seconds + DISPLAY_TRUNC_S`）、`:214-218`（`min` 保留 NaN）；消费端 `core/engine.py:1129/1212/1222`，兜底 `:1174`
- 输入样例 / 实测：
  ```
  c.observe(float("nan"), 1000.0) -> True
  c.zero_at() -> nan
  c.due(1003.0, 1.0) -> False
  c.remaining(1003.0) -> nan
  ```
- 后果：`engine.py:1222` 的 `due` 永远 False，`is_stale` 又因为"有新观测"而不 stale，`engine.py:1174` 的 `cd.info()["last"] is None` 兜底也因为 last=NaN 而失效 → **倒计时模式原地空转**，心跳照跳，`watchdog` 不会判卡死。
- 期望：`observe` 用 `math.isfinite(seconds) and seconds >= 0` 做前置校验；`zero_at` 遇到非有限值直接丢弃该观测。

---

## 4. 低

### N11 许可：重启后墙钟前跳 30 天 → 离线额度一次耗尽，直接判过期

- 严重度：**低**（不属抢购路径，但会挡住整个程序）　`【新增未提交】`
- 位置：`core/license.py:383-394`（`delta = now - last_wall`，只有下界 `max(0.0, delta)`，没有上界）、`:514`（`check_now = max(now, anchor + used)`）、`:508`
- 输入 / 实测：
  ```
  重启后 last_wall=1729000000, now=1729000000+30d -> offline_used_s = 2592000.0
  重启后 now-last_wall=80s 但 monotonic 差=80s       -> offline_used_s = 80.0（这条正确）
  重启后 now-last_wall=-1d                          -> rollback_events=1, used 不变（这条正确）
  ```
- 后果：改一次系统时间（或 CMOS 归零、时区/夏令时误设）重启后，`used` 直接 2,592,000s，超过 `offline_grace_h`；同时 `check_now = max(now, …)` 直接越过 `exp` → 用户看到"许可已过期"。回拨也不会把这段额度还回来。
- 期望：`delta` 加上界（如 `> 7*86400` 视为不可信，记 `clock_anomaly` 并置 0），到期判定对 `now` 也做同样的异常封顶。

### N12 区域坐标无数值校验：NaN 抛异常、Inf 溢出、超界坐标点/抓到屏外

- 严重度：**低**
- 位置：`core/screen.py:108-125`（`int(round(box["x"] * w))` 等）、`core/config.py` 的 region 读取（不校验 0~1 / 有限性）
- 输入 / 实测（1920×1080）：
  ```
  {"x":nan,"y":0,"w":1,"h":1}            -> ValueError: cannot convert float NaN to integer
  {"x":inf,...}                          -> OverflowError: cannot convert float infinity to integer
  {"x":2.0,"y":-1.0,"w":-0.5,"h":0}      -> (3840, -1080, 1, 1)   屏外 + 1px
  ```
- 后果：NaN/Inf 在引擎线程里抛异常（任务失败）；负数/超界得到负坐标或 1px 区域，`mss` 抓空图 → OCR 恒空 → 死循环或误判，点击落在屏外。
- 期望：`region` 读取时校验每个字段 `isfinite` 且 `0 <= v <= 1`、`x+w <= 1`、`y+h <= 1`，非法即拒绝启动并指明字段。

### N13 `config.json` 手改的字符串数字没有归一：ValueError 打线程，NaN 变静默跳过

- 严重度：**低**（`/api/config` 有归一，手改才中招）
- 位置：`core/engine.py:1411-1414`（`float/int(cfg.get(...))`）、`:1726-1727`（`int(cfg["click_hz"])` / `float(cfg["fire_timeout_s"])`）、`:1759`（`t - t_fire < fire_timeout`）、`:619`、`:637`
- 输入 / 实测：
  ```
  config.json {"click_hz": "abc"}      -> ValueError: invalid literal for int()
  config.json {"fire_timeout_s": "nan"} -> 1759 条件立即 False → 0 次点击，
                                          报"8 秒内没等到确认弹窗"，实际只过了 0 秒
  config.json {"threshold": "0.85"}     -> 正常（float 字符串能吃）
  config.json {"failsafe": "false"}     -> bool("false") = True（方向相反）
  ```
- 期望：`Config.load` 里按已知 key 表做类型归一（等价于 server 的 `_CONFIG_COERCERS`），布尔按字符串语义解析；引擎侧读数字用统一小工具（`isfinite` + 默认值），不要裸 `int/float`。

### N14 `market.check()` 对区间字段无类型兜底

- 严重度：**低**
- 位置：`core/market.py:495-496`（`lo/hi = float(task.get(...) or 0)`）、`:529`（`max_wear = float(...)`）
- 输入 / 实测：
  ```
  min_price="abc"   -> ValueError: could not convert string to float: 'abc'   每轮扫描都抛
  min_price=nan     -> (True, '符合（1.0）')   下限静默失效
  min_price=-100    -> (True, '符合（1.0）')   下限静默失效
  max_price=inf     -> (True, '符合（1.0）')   上限静默失效
  max_wear=nan      -> (True, '符合（1.0）')   磨损上限静默失效
  ```
- 说明：`check_conditions`（validate.py）已经对 API 入队的价格做了 `isfinite`/整数/非负校验，所以正常链路安全；这里缺的是"配置文件/旧队列脏数据"的兜底。NaN 参与比较恒 False，而 `lo and ...` 又因 `nan` 为真值不短路，于是上下限一起消失。
- 期望：`check` 里 `isfinite(v) and v >= 0`，否则该条件按"未设置"处理并记一条 warn。

### N15 成交记录接受 NaN/Inf/负价 → `stats.spent = nan`，写出非严格 JSON

- 严重度：**低**
- 位置：`core/records.py:56`（`price in (None, "", 0)` 只挡这三个，NaN/Inf/负数都放行）、`:60/85`（`json.dumps` 默认 `allow_nan=True`）、`:295`（`acc["spent"] += float(...)`）
- 输入 / 实测（`records.PATH` 指向临时文件）：
  ```
  add(price=nan) -> price NaN     jsonl 里写成  "price": NaN
  add(price=inf) -> price Infinity                "price": Infinity
  add(price=-5)  -> -5.0          （负数计入消费额）
  stats()        -> {'spent': nan, ...}
  ```
- 后果：`records.jsonl` 含 `NaN/Infinity`，任何严格 JSON 解析器（浏览器、其它语言、日志分析）都会报错；`stats.spent` 一旦变 NaN，界面"累计花费"永久 NaN。当前引擎传进来的价格是整数，所以是防御缺口。
- 期望：`isfinite(price) and price >= 0`，否则按 None 记；`json.dumps(..., allow_nan=False)`。

### N16 `_budget` 闸门拿单件价格比"总预算"，不是"剩余预算"

- 严重度：**低**　`【改动态】`
- 位置：`core/engine.py:1811-1815`（`if price is not None and budget > 0 and float(price) > budget`）
- 输入样例：`budget=10000`，上一轮已花 `spent=9000`，本件 `price=2500`
- 实际：`2500 <= 10000` 通过 → 累计 11500，**超总预算 1500**
- 期望：`float(price) > budget - spent`（列表模式 `engine.py:1513` 用的就是 `spent + r["price"] <= budget`，两处口径不一致）。

### N17 毫秒字段的读取没有下限，负数导致 `sleep` 抛错

- 严重度：**低**（与 N03 同源，单列便于定位）
- 位置：`core/engine.py:724`（`int(cfg.get("countdown_attempts") or ...)`）、`switches.py` 的 `fire.confirm_gap` / `fire.delay` / `fire.jitter`（`int` 分支）
- 输入 / 实测：`confirm_interval_ms = -10` → `_coerce` 接受 -10 → 引擎 `max(0.0, -10/1000)` 把负数抹平（不会崩，但**和前端显示不一致**，用户以为设了 -10 会生效）；而 `scan.interval=-0.05`（float 分支）没有 `max()` 保护，直接崩。同一类字段两种行为。

### N18 截断/舍入方向不统一（`int()` 向零截断 vs `round()` 银行家舍入）

- 严重度：**低**（只影响边界像素/显示，但会让"读回校验"差 1）
- 位置与实测：
  ```
  server.py:97  int(float(3.9))        -> 3     （向零截断）
  engine.py:1314-1315 int(lo)/int(hi)  -> 1000（价格 1000.9 被截成 1000）
  nav_ons.py:795 round((cy-base)/h)    -> round(2.5)=2（银行家舍入，不是 3）
  engine.py:1554 int(row["cy"])        -> 205.9 -> 205
  ```
- 后果：价格 1000.9 生成为 1000，输入框下限变宽；行号在正好 .5 的位置会落到上一行，可能点错卡片。全部差 1 以内，不致命，但和 `_check_price_box` 读回校验组合时会出现"设置成功但校验说不对"。
- 期望：像素/行号统一用 `int(round(...))`；价格字段要求整数就用 `math.floor` 并在校验时不接受非整数。

---

## 5. 确认没问题的项（已核对/已实测）

1. **`T0Estimator` 的数值防御是完整的一套**（`countdown.py:573+`），实测 `nan / inf / -1 / "abc" / True / None / 1e18` → 全部 `False`，只有 `0` 接受。问题是引擎用的是简单 `Clock`（`engine.py:1129/2026`），不是这个估计器。
2. **毫秒→秒的换算一致**：`confirm_interval_ms`、`fire_delay_ms`、`fire_jitter_ms`、`follow_wait_ms` 全部在消费端 `/ 1000`，`scene_guard` 的 `frame_dt_ms` 与阈值都在毫秒域，没有发现"毫秒当秒"。
3. **`engine._deadline` 的"壁钟 → QPC"是一次性换算**（`engine.py:227`），之后循环里全部 `perf_counter`（`:1477/:1481/:660/:666`），不存在中途混用；这次换算本身是对的。
4. **`watchdog` 全部用 `monotonic`**；`scene_guard` 与 `recovery` 共用同一 `monotonic` 时钟（`guard.since` 是单调值，`recovery._elapsed` 也是），没有跨源相减导致的负 elapsed。
5. **`fire_at` 与 `true_now` 的口径一致**：前端的 `Date.now()/1000` 是本机墙钟标签，`true_now()` 把同一标签重映射到真实时间，两边都按"墙钟标签"理解，补偿方向没有问题（`preflight` 里"偏移被补偿掉"这句话在这条链路成立）。
6. **除零有守卫**：`engine.py:1468` 的 hz 计算有 0.25s 窗口，`records.stats` 的命中率在 `tries==0` 时返回 0，`countdown` 的配对/斜率计算都有最小样本数（`EST_MIN_TRUST_N`/`EST_BRACKET_MIN_PAIRS`）。
7. **无整数溢出风险**：全项目价格/秒数都是 Python `int`（无界）；`perf_counter` 的双精度在 2^53 ≈ 9.0e15 上按 10MHz 也要连续开机约 28 年才会丢 1 tick，本机 uptime 远未到。
8. **`check_conditions`（`validate.py`）对 API 入队的价格/配额/预算做了 `isfinite`、整数、非负校验**，`scheduler.add` 会调用它 —— 价格条件从 HTTP 入队是安全的；N14 只是配置文件/旧队列的兜底缺口。
9. **`_as_bool` 正确处理 `"0"/"false"/"off"` 字符串**（`server.py`），`failsafe` 走 HTTP 时不会出现 `"false"→True` 的反向问题；只有手改 `config.json` 才会（N13）。
10. **`parse_words` 的抗噪设计有效**：`"0分6秒"` → 6、`"99小时59分59秒"` → 359999，锁图标被识别成 `6` 时也不会拼成 60 分 5 秒。
11. **`market.parse_row` 的千位分隔符误判已被专门处理**（`9,000` 被读成 `9.000` 的修复在 `:157-169`），`"0"` 单独成行时返回 `price=None`（不参与排序），这两条是稳的。
12. **`records.add` 对字符串脏价（`"¥1,230"`、`"abc"`）已经不会打死线程**（`:55-58`），只丢价格；N15 剩下的只是"非有限值"没挡。

## 6. 无法验证的项（本次没有条件做）

1. **N01 的真实可达性**：本环境没有可应答 UDP/123 的服务器，也未联网校时；只用假 socket 做了报文级注入。无法确认实际部署里 123 端口会不会被中间盒/蜜罐/错配主机应答，也无法确认 `sync()` 的 `samples=4` 是否足以剔除离群。
2. **N06/N07 在真机 OCR 上的出现率**：没有真机截图/OCR 输出样本，无法给出"千分位被空格拆开""价格带小数"的实际百分比；只能确认解析层的错误方向。
3. **N08 的实际卡死时长**：按代码路径推算是"回拨多少秒就多卡多少秒"，但没有在运行中的导航流程里实测（不碰真机、不改系统时间）。
4. **N12 在真实多屏/DPI 缩放下的表现**：需要真实 `mss` 抓屏（本次未开可见窗口/未抓屏），只能验证 `norm_to_px` 的纯算术。
5. **N04 前端后果**：浏览器 `JSON.parse("NaN")` 必然抛，但没有在页面上实测"设置页整体读不出配置"的具体表现（含 `web/index.html` 的降级逻辑）。
6. **N11 的许可策略边界**：`offline_grace_h` 由服务端 payload 下发，不知道生产值；30 天前跳是否一定越过 `exp` 取决于真实租期。
7. **QPC 回绕/精度**：未读取本机 `QueryPerformanceFrequency`（无法确认是否 10MHz 以下），结论按通用 2^53 分析。
8. **`--`（负数价格/负磨损）在真实游戏 UI 是否可打印**：`-15000`、`¥0` 这类样本是构造的，无法确认真机是否会出现。

## 7. 复现方式（不依赖真机）

所有样本都在纯函数层复现，可直接粘贴运行（在项目根目录）：

```powershell
python -c @"
import sys, math; sys.path.insert(0, '.')
from core import market, ocr_chain, scheduler, switches, server, countdown, screen

# N02  fire_at 的 NaN / inf
print('N02', scheduler._fire_at({'fire_at': 'nan'}), scheduler._fire_at({'fire_at': '1e400'})

# N03  开关 float 分支
print('N03', switches._coerce(switches.spec('fire.threshold'), 'nan'),
             switches._coerce(switches.spec('scan.interval'), -0.05))

# N04  配置归一
try:
    server._as_int(float('inf'))
except Exception as e:
    print('N04', type(e).__name__, e)
print('N04', server._as_float('nan'))

# N06 / N07  价格
print('N06', ocr_chain.price_from_text('¥12 345'), ocr_chain.price_from_text('¥1,234.5'))
print('N07', market.parse_row('¥5 崭新 0.01')['price'], market.parse_row('-15000 崭新')['price'])

# N09 / N10  倒计时
print('N09', countdown._parse_text('1.999秒'), countdown._parse_text('-5秒'))
c = countdown.Clock()
print('N10', c.observe(float('nan'), 1000.0), c.zero_at(), c.due(1003.0, 1.0))

# N12  区域坐标
class D:
    size = (1920, 1080)
print('N12', screen.Screen.norm_to_px(D, {'x': 2.0, 'y': -1.0, 'w': -0.5, 'h': 0}))
"@
```

`timesync` 的 N01/N05、`license` 的 N11 需要假 socket / 假时钟，写法见正文各条（`FakeSock` 返回 `b"\x00"*48`；`ts.time` 替换为只返回固定值的对象）。

审查用的临时探针脚本在 `C:\Users\26346\Documents\Trae-CN\temp_20261107_15\_r03_probe.py`、`_r03_probe2.py`（工作区根目录，不在项目内，未写入项目任何文件）。
