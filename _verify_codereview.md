# 独立复核：四批「已修」改动的代码审查

- 审查对象：`C:\Users\26346\Desktop\project\dafors`
- 审查方式：**只读代码推演**。未运行任何业务逻辑、未启动引擎/服务器、未操作桌面与光标、未 commit。
- 判定口径：不只看「改的那行对不对」，看**该缺陷的全部触发路径**（异常路径 / 并发路径 / 跨平台路径 / 调用方路径）。
- 结论：**未通过**。四批里 6 项 FAIL，另有 3 个由修复本身引入的新问题（其中 2 个会直接让功能不可用）。

---

## 批次一：持久化

### 1.1 `.good` 备份原子化 — PASS（但恢复路径引入 P0，见 1.9）
- 修复点：[core/config.py:709-736](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L709-L736)（临时文件 → fsync → 读回逐字节比对 → `_replace_with_retry`）；读取侧 [core/config.py:868-898](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L868-L898) 校验长度 + 非空 JSON 对象。
- 覆盖判断：覆盖。写 `.good` 永远不直接截断目标文件；校验失败/替换失败只 `print` 并保留上一份可用备份（[core/config.py:733-736](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L733-L736)），不会把好的备份弄坏，也不会让本次保存失败。
- 推理：`_write_payload` 先成功才写备份，备份失败只告警 → 「payload 已落盘但 save() 抛异常」这种半成功状态不会被备份路径制造出来。`_read_backup` 的 `len(raw) != size` 比对能挡住半份文件（写一半被杀）。
- 残留（低）：`.good` 的临时文件名是 `config.json.good.<pid>.<tid>.tmp`，而孤儿清理正则是 `^config\.json(?:\.good)?\.(\d+)\.(\d+)\.tmp$`（[core/config.py:342](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L342)）——**能匹配**，无残留问题。此项无缺陷。

### 1.2 台账残行 — PASS
- 修复点：[core/records.py:112-155](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L112-L155)（从文件尾反向找 `\n`，`truncate(cut)`），配合 [core/records.py:218-220](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L218-L220)（修不动就补一个 `\n` 前缀保证行边界）。
- 覆盖判断：覆盖。三条触发路径（进程被杀留半行 / 文件只读修不动 / 文件被占用）都有对应动作：截断、补换行、写失败记账。
- 推理：`cut` 初值 0，只有「整份文件没有任何 `\n`」才会 `truncate(0)` —— 这种文件本来就只是一条残缺行，全删是正确语义。修复后 `_recovery_state["count"] += 1`（[core/records.py:147-149](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L147-L149)）并出现在 `stats()["recovered_lines"]`，可观测性到位。
- 残留（低）：`_repair_tail` 每次 `add()` 都跑（两次 open + 一次 seek 读），属于每写一条多两次句柄开销；不影响正确性。

### 1.3 多进程写互斥 — PASS（写路径）/ 读路径仍有跨进程窗口
- 修复点：[core/config.py:285-301](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L285-L301)（`_proc_lock` 可重入壳 + `_proc_state["depth"]`）、[core/config.py:225-276](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L225-L276)（`msvcrt.LK_NBLCK` 字节锁，2ms→20ms 退避，操作系统释放不死锁）。
- 覆盖判断：**写路径覆盖，读路径未覆盖**。`_proc_lock` 的前提是「调用方已持有 `_save_lock`」，而 `_save_lock` 是 `RLock`（串行化线程，`depth` 全局计数因此在单临界区内安全）——这一层没问题。
- **遗漏（新风险 P2）**：`_load_locked`（[core/config.py:803-857](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L803-L857)）只拿线程锁、不拿文件锁；`_quarantine` 的 `os.replace`（[core/config.py:916](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L916)）也在文件锁外。于是存在这条跨进程路径：
  1. 甲进程判定内容损坏 → `os.replace(config.json → .broken-*)`；
  2. 乙进程在这几毫秒里 `Config.load()`：`os.path.isfile` 为 False（[core/config.py:808](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L808)）→ 走「第一次运行」分支，`cls()` + `_save_locked()` 把**默认值**写进 config.json 并返回 `ok=True`；
  3. 甲随后从 `.good` 恢复并回写（[core/config.py:834-839](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L834-L839)），两者谁后写谁生效 —— `.good` 想救的那份配置可能反被默认值盖掉。
- 推理：`isfile` 判断与首次写盘不在同一把文件锁里，是 TOCTOU；修「写-写互斥」没有顺手把「读-改-写」的读也纳入锁。

### 1.4 `mode=list` 被改成 countdown — PASS
- 修复点：[core/config.py:528-548](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L528-L548)（`had_wf` 只看「盘上真写了 workflow」或「内存里改过 workflow」，否则才用老 `mode` 迁移），任务级 [core/config.py:443-454](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L443-L454)（`task_workflow` 同样按 `mode` 迁移）。
- 覆盖判断：覆盖。两条触发路径都堵住了：全局配置（`_normalize`）和队列任务（`_normalize:563-566` + `_run_step` 的 `task_workflow`）。
- 推理：以前 `d["workflow"]` 在 `_merge` 之后永远等于默认 `countdown`，拿它判断就必然把 `mode="list"` 的老配置改成 countdown；现在用 `_raw` 判断「原始输入写过没有」，语义正确。
- **副作用（中，行为变化）**：[core/config.py:563-566](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L563-L566) 会把**每条任务**的 `workflow` 固化成当时全局的值。任务一旦被规范化（任何一次 `_set_status` 落盘都会），之后在界面切换全局路线对这些已排队任务不再生效。

### 1.5 部分字典合并 — PASS（顶层）/ regions 与 queue 仍有损
- 修复点：[core/config.py:576-584](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L576-L584)（`regions` 按名字白名单合并且保序，`queue` 仍是整体替换）。
- 覆盖判断：部分覆盖。`regions` 的**新增区域名被静默丢弃**（`if n in cur`），这是有意的下游约束，但用户手写新区域名会「改了等于没改」，且没有任何提示。
- 推理：`queue` 保留整体替换语义本身没错（列表无法按键合并），但它正是 1.8 里丢数据的地方。

### 1.6 满盘兜底 — PASS（写侧）/ 调用方有缺口（见 1.10）
- 修复点：[core/config.py:695-706](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L695-L706)（失败删临时文件并 `raise`，原文件不变），[core/config.py:669-692](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L669-L692) 整段在文件锁内。
- 覆盖判断：覆盖。三条路径（磁盘满 / 只读目录 / 目标被占用 `PermissionError` 共享冲突）分别是：立即抛（不重试）、立即抛、`_replace_with_retry` 退避 7 次后抛（[core/config.py:312-324](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L312-L324)）。
- 推理：`_dirty` 在写成功后才 `clear`，写失败时内存状态保留，重试可以再存；不会留下 0 字节 config.json（这是原来的缺陷）。

### 1.7 台账写失败可见 — PASS（API 可见）/ 运行日志不可见
- 修复点：[core/records.py:81-87](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L81-L87)（`_note_error`）、[core/records.py:431-433](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L431-L433)（`stats()` 带 `write_ok` / `write_error` / `recovered_lines`）、[core/server.py:248-256](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L248-L256) 已透出。
- 覆盖判断：覆盖（数据可见）。但**引擎侧不会提示**：`add()` 吞掉异常返回 `None`（[core/records.py:238-244](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L238-L244)），而 [core/engine.py:2367-2368](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2367-L2368) 的 `except` 只在**抛异常**时说话 —— 于是「记不下这条成交」只存在于 `/api/records` 的 `stats.write_ok`，抢购日志里依然是静默的。
- 推理：可见性被放在读接口而不是写现场，用户不看记录页就不会知道。

### 1.8 外改配置被覆盖 — **FAIL（部分覆盖）**
- 修复点：[core/config.py:634-667](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L634-L667) 三方合并（`ours` / `_loaded_data` / `fresh`），指纹 `_disk_sig`（[core/config.py:327-337](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L327-L337)）。
- 覆盖判断：**顶层标量覆盖了，`queue` 没覆盖**。判「本对象碰过这个键」用的是 `k in self._dirty or ours.get(k) != base.get(k)`（[core/config.py:654-657](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L654-L657)），命中就用 `ours`。
- 推理（丢数据路径）：调度器每改一次任务状态都会 `save()`，`queue` 必然与 `base` 不同 → 判定为「本地修改」→ 整份用本地队列。此时若盘上被**别的进程/用户**新增过一条任务（`fresh.data["queue"]` 有而本地没有），合并结果取本地 → 那条新增任务被静默丢掉。也就是说：**原来的「后写整份覆盖」缺陷，在最关键的 `queue` 上仍在**，只是从「整份」缩小到了「逐键」。
- 另有两点（低）：指纹是 `(mtime_ns, size)`，同尺寸改写识别不了（代码注释已承认）；`_disk_sig is None` 时（配置刚被自愈/首启）直接跳过合并（[core/config.py:642-643](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L642-L643)），后续 save 不再看外部改动。

### 1.9 新问题（修复引入，P0）：自动恢复成功反而「开不了抢」
- 位置：[core/config.py:835-839](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L835-L839) → [core/config.py:606-613](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L606-L613) → [core/scheduler.py:458-465](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L458-L465)
- 推理：从 `.good` 恢复成功时，代码把 `cfg._load_error = "config.json 损坏，已从最近一次正常备份恢复"` 写上，同时 `return cfg, True`；而 `load_ok` 的定义是 `self._load_error is None`。于是：
  1. `Scheduler.start()` 在 `if not getattr(cfg, "load_ok", True)` 处直接拒绝启动，提示「配置读不出来……先别开抢（关掉占着 config.json 的程序后重试）」——**配置其实已经恢复且已回写**，提示词还是另一码事；
  2. `_recover_orphans`（[core/scheduler.py:129-140](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L129-L140)）跳过，残留 `running` 任务永远不会被复位；
  3. `gc_task_dirs` 跳过（[core/config.py:953-957](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L953-L957)），`data/` 继续膨胀；
  4. `/api/config` 的 `config_ok`/`last_error` 让界面显示「配置读不出来」。
- 影响：这条修复的**正向路径**（坏配置 → 自动从备份救回）把程序锁死在「不能开始」状态，只能重启进程才恢复（重启后 `_load_error` 为空）。属于同一批改动自己造出来的阻塞级问题。

### 1.10 新问题（修复引入，P1）：借配置不在 `try` 里，异常会留下永久 RUNNING
- 位置：[core/scheduler.py:906](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L906)（先置 RUNNING）→ [core/scheduler.py:917](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L917)（`saved = self._borrow_config(task)` **在 try 之外**）→ [core/scheduler.py:918-939](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L918-L939) 才是 `try/finally`。
- 推理：`_borrow_config` 走 `Config.transaction()`（读盘 + 落盘 + 复制模板）。磁盘满/被占用时这里抛 `OSError`/`ConfigLoadError`，异常越过 `try` 直接冒到 `_run` 的兜底 except（[core/scheduler.py:741-752](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L741-L752)）；而 `finally` 里的 `_return_config` 没机会执行 → 任务永远停在 `running`，`active_task` 与借来的模板残留在全局配置里，`_pending` 过滤又跳过 `running`，该任务再也排不上。
- 结论：满盘兜底修了「写不会毁文件」，但没修「写失败时调用方怎么收场」。

---

## 批次二：数值与时间

### 2.1 NTP 应答校验 — PASS（有误杀风险）
- 修复点：[core/timesync.py:58-113](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py#L58-L113)（长度、mode∈{4,5}、stratum 1..15、**origin 回显随机化 transmit 时间戳**），[core/timesync.py:175-189](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py#L175-L189)（中位数 + `MAX_DEVIATION_S` 剔离群，不再按最小 delay 选）。
- 覆盖判断：覆盖。伪造包（delay=0）已经不能「因为延迟最小」被选中；origin 不匹配直接拒绝。
- **残留（中，误杀合法输入）**：`sync()` 对每个服务器**首次失败即 break**（[core/timesync.py:150-152](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py#L150-L152)），而门槛是 `MIN_SAMPLES = 3`（[core/timesync.py:41](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py#L41)）。三个服务器各 4 采样的设计下，任意一个服务器掉一个包就整台作废：只要三台各掉一次，有效样本 0 → "所有 NTP 服务器都不通"，保留旧 offset。校时长期不更新会让归零时刻偏，属于用「严判」换来的可用性下降。
- 推理：`break` 的合理性在于「应答不可信就别重复试」，但它把「UDP 偶发丢包」和「服务器应答伪造」归成同一类。

### 2.2 22 个开关的边界表 — **FAIL（表对了，下游绕过）**
- 修复点：[core/switches.py:345-410](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py#L345-L410)（`BOUNDS` 22 条，与 `SWITCHES` 的 22 个数值项一一对应），校验/归一 [core/switches.py:411-454](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py#L411-L454)，写入口 [core/switches.py:472-510](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py#L472-L510)。
- 覆盖判断：**只覆盖「经 `switches` 写入」这一条路径**。读侧仍有 5 处直接读配置：`cfg["threshold"]` [core/engine.py:750](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L750)、[core/engine.py:1541](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1541)、[core/engine.py:1836](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1836)，`click_hz`/`fire_timeout_s`/`confirm_clicks`/`confirm_interval_ms` [core/engine.py:1857-1860](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1857-L1860)，`cfg["monitor"]` [core/engine.py:555](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L555)。
- 推理：`_normalize`（[core/config.py:494-574](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L494-L574)）只归一 `trigger_mode` / `input_*` / `workflow` / `queue`，**不夹带 `BOUNDS`**。所以手改 config.json（或旧版本进程写的）`"threshold": "abc"` / `"click_hz": -3` 依旧直达引擎：`float("abc")` 在开火路径上抛 `ValueError`，靠上层 except 变成「本轮失败」；`confirm_clicks: 0` 被 `max(1, …)` 兜住，但 `threshold: 2.0` 会让所有卡片判为命中。
- 结论：边界表是正确且完整的，但「全部触发路径」没覆盖 —— 配置文件本身就是一条写路径。
- 另：`/api/switches` 的 `SwitchValueError` 目前走 `_guard` 的通用 500（[core/server.py:322](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L322)），不是 400；报告里的「待接线」已自认这条，此处确认为真。

### 2.3 货币符号解析 — PASS（有误杀）
- 修复点：[core/market.py:32](file:///C:/Users/26346/Desktop/project/dafors/core/market.py#L32)、[core/market.py:131-142](file:///C:/Users/26346/Desktop/project/dafors/core/market.py#L131-L142)。
- 覆盖：`¥` / `￥` / 负号 / 千分位都能过；小数直接排除（`"." not in m.group(2)`），防止把「磨损 0.25」当价格。
- **残留（中）**：`_MIN_PRICE = 100`（[core/market.py:57](file:///C:/Users/26346/Desktop/project/dafors/core/market.py#L57)）是硬下限，而开关里 `price.min` 的下界允许 0 —— 任何 99 及以下挂单在「先按货币符号取价」和「整数启发式」两条路径上都会被拒。若玩家挂低价品，等于永远匹配不到。

### 2.4 `¥` 分支 — PASS
- 修复点同上（`_CURRENCY` 正则 + 符号 + 数字分组）。`￥`（全角）已覆盖；`$`/`€` 不覆盖，但会落到整数启发式分支，不是硬失败。
- 推理：`_CURRENCY.search` 是「整段文字里找第一个带符号的数」，OCR 文本里符号常被切成独立 token，`\s*` 已容忍。多段文字共用一条正则时贪婪吞空白，末尾 `rstrip(" .")` 处理（[core/ocr_chain.py](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_chain.py) 的 `_parse_currency_amount`）不会把后续数字粘进来。

### 2.5 超时改用单调时钟 — PASS（两处显示项仍是墙钟）
- 已改：`jobs.py` 进度 `elapsed` 用 `time.monotonic`（注释直言「系统时间回拨会显示还剩 -3 秒」）；`screen.py:227-237`；`nav_col.py:1001-1091`；`ocr_service.py:236-237 / 349-352`；`countdown.py` 全链路 `perf_counter`。
- 残留（低，仅显示）：[core/pagewatch.py:63](file:///C:/Users/26346/Desktop/project/dafors/core/pagewatch.py#L63) 与 [core/pagewatch.py:146](file:///C:/Users/26346/Desktop/project/dafors/core/pagewatch.py#L146) 用 `time.time()` 算页面快照「年龄」；[core/scheduler.py:1044](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L1044)、[core/scheduler.py:1078](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L1078) 用墙钟算 `elapsed`。都不参与超时判定，只影响显示值。

---

## 批次三：输入通道

### 3.1 `message` 第三通道 — **FAIL（被调用方废掉）**
- 修复点：[core/input.py:73](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L73)（三选一）、[core/input.py:820-858](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L820-L858)（纯消息点击 + 每次 `WM_NULL` 受理校验）、[core/input.py:612-613](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L612-L613)（分发）。
- **缺陷（P0）**：前台校验发生在**分发之前**，且对 `message` 同样生效：[core/input.py:598-610](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L598-L610)。而引擎每次都带 `expect_foreground` 调用：[core/engine.py:540-542](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L540-L542)。
- 推理：`message` 通道存在的**唯一理由**就是「目标不在前台也能点」。现在它先被 `ensure_foreground(hwnd)` 抢一次前台：
  - 抢到 → 通道的优势消失（游戏被拉到前台、用户被打断），只是「不移动光标」；
  - 抢不到 → 直接 `WindowNotForeground` → 整轮中止。
  即：在它唯一适用的场景（目标不在前台）里，它不是失效就是多余的。这正是「修了 A（新增通道），但调用方还在用旧语义（必须前台）」。
- 附带：`_click_message` 的 `functional` 只写进 `_last_click`，不参与判定；`input_message_strict=False` 时对着游戏（`functional="unsupported"`）会 200/200 受理、游戏零响应，而运行时没有任何提示。

### 3.2 降级不再静默（`channel_status`） — **FAIL（只有数据结构，没有出口）**
- 修复点：[core/input.py:552-573](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L552-L573)、[core/input.py:1033-1040](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L1033-L1040)（`requested/effective/degraded/last_click`）。
- **缺陷**：`channel_status()` 全仓库只有 `tools/` 与 `tests/` 调用（grep 无生产调用点）。引擎只读 `driver_warning`（[core/engine.py:560](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L560)），而这个属性只在 **requested == "interception"** 的降级时才有值。于是 `message → legacy` 的降级：既不进日志、也不进 `status()`、`/api/scheduler` 也没有 channel 字段 —— 用户选了「不动光标」，实际每次点击都在动光标，且全程无提示。
- 推理：`_decide_channel`（[core/input.py:236-305](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L236-L305)）的默认 `policy="legacy"`，加上游戏窗口 `functional="unsupported"`，`message` 请求**默认就会降级到 legacy**。默认值踩中最常见的组合，而降级又恰好不可见。

### 3.3 UIPI 主动判定与逐次校验 — PASS（判定只做一次）
- 修复点：[core/input.py:401](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L401)（构造期比完整性级别，不匹配直接 `UIPI_BLOCKED`），[core/input.py:843-849](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L843-L849)（每次点击 `WM_NULL` 同步往返校验，`0 + err=5` 报 UIPI）。
- 覆盖判断：覆盖「构造时已知不匹配」和「点完没被受理」两类。`message` 通道拒绝时不回退到 legacy（`raise`），不会偷偷动光标 —— 这点是对的。
- 残留（低）：构造后目标窗口进程提升完整性级别的情况不重判（游戏不会中途提权，可接受）；`_gate_integrity` 抛错会让整轮中止而不是降级，属于有意的硬失败。

### 3.4 `/api/config` 入口 — **FAIL（第三通道存不进去）**
- 修复点：[core/server.py:121-143](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L121-L143)。
- **缺陷**：`_as_input_mode` 只放行 `("interception", "legacy")`；`_CONFIG_COERCERS` 里也没有 `input_fallback` / `input_message_strict`。经 `/api/config` 提交 `input_mode="message"` 会被判非法 → 保留原值 → **静默丢掉**；提交降级策略同样丢掉。
- 推理：配置层（[core/config.py:399-418](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L399-L418)）与开关层（`env.input_mode` select）都认第三个值，唯独 HTTP 这条旧入口不认。界面目前走 `/api/switches` 才绕得过去；任何直接调 `/api/config` 的客户端（老前端、脚本）都会得到「设置没生效」。
- 附加：`/api/config` 也完全没法设置 `input_fallback`，而 `input_fallback` 决定了降级目标是 legacy（动光标）还是 fail —— 用户无法通过这条路把「宁可不点也别动我光标」配出来。

### 3.5 状态位不同步 — **FAIL（轻）**
- [core/engine.py:556-565](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L556-L565)：`self._input_mode` 存的是**配置里的原始值**，没有回写成 `eff`；`Keyboard` 又拿这个原始值各自决策（[core/input.py:1023](file:///C:/Users/26346/Desktop/project/dafors/core/input.py#L1023)），于是鼠标与键盘可能落在不同通道（例如 Mouse 降级到 legacy、Keyboard 却探针成功走 interception）。
- 同一处日志把 `mode="message"` 写成「输入通道：PostMessage(legacy)」（`eff == interception` 的三元判断只有两个分支），日志与真实通道不一致。

---

## 批次四：稳定性

### 4.1 看门狗 — PASS（机制存在，但恢复基本无效）
- 修复点：`Heartbeat`（[core/watchdog.py:19-45](file:///C:/Users/26346/Desktop/project/dafors/core/watchdog.py#L19-L45)，时钟注入、默认 `time.monotonic`）；引擎心跳与出口 [core/engine.py:369](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L369)（`beat_age_s`）；判定 [core/scheduler.py:1044-1065](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L1044-L1065)（`WATCHDOG_STALL_S=60`）；重开 [core/scheduler.py:919-938](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L919-L938)（最多 2 次）；失败硬停 [core/scheduler.py:947-952](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L947-L952)。
- 覆盖判断：覆盖「引擎线程还在、但不再前进」这个原始症状。`watchdog.Watchdog` 类在运行时未被实例化，但判定逻辑已在调度侧落地，功能等价。
- 残留（中）：重开依赖 `engine.start()`，而卡死线程**仍然 alive**，`start` 会拒绝（「已经在运行了」）→ 两次重开大概率都是立刻 FAILED。机制真正的作用是「判定 + 复位 + 不重复点击」，不是恢复。另外判定只在 `_drive_task` 内，引擎启动前、配置归还阶段无监控；60s 阈值对「OCR 冷启动 + 驱动加载」这种长同步调用存在误判可能。

### 4.2 资源上限轮转 — PASS（两处静默失败点）
- 修复点：[core/retention.py:21-25](file:///C:/Users/26346/Desktop/project/dafors/core/retention.py#L21-L25)（400 张 / 256MB / 8MB）；`prune_dir` [core/retention.py:77](file:///C:/Users/26346/Desktop/project/dafors/core/retention.py#L77) 在 [core/engine.py:2390-2391](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2390-L2391)（每次存图后按 mtime 删最旧）；`rotate_log` 在 [core/ocr_service.py:41-46](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_service.py#L41-L46)+[core/ocr_service.py:459](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_service.py#L459)。
- 覆盖判断：覆盖「只增不减」。`prune_dir`/`imwrite`/`records.add` 全在 `try` 内，失败只降级为一条 warn（[core/engine.py:2394-2396](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2394-L2396)），不影响抢购。
- 残留（中）：`_rotate` 只吞 `OSError` 不做任何记录（[core/ocr_service.py:44-46](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_service.py#L44-L46)）；且它只在 spawn 时执行，而子进程通过 `stdout=f`（[core/ocr_service.py:469](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_service.py#L469)）继承同一句柄。若旧子进程尚未退出，`os.replace` 在 Windows 上必然失败 → 日志上限永不生效，且无人知晓。

### 4.3 句柄引用环 — PASS
- 修复点：[core/engine.py:175](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L175)（`self_ref = weakref.ref(self)`）、[core/engine.py:189](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L189)（`recovery.Cleanup(recovery.ResourceAdapters(self_ref ...))`）、[core/vision.py:130](file:///C:/Users/26346/Desktop/project/dafors/core/vision.py#L130) / [core/vision.py:217](file:///C:/Users/26346/Desktop/project/dafors/core/vision.py#L217)（模板缓存用 weakref 回调清键）。
- 推理：`Engine → Cleanup → 适配器闭包 → Engine` 这条环被 `weakref` 打断；模板缓存不再随 `id()` 复用而持住旧 ndarray。`Engine` 是每轮构造、线程退出后由 GC 回收，`on_hit` 持有 Scheduler 的 bound method 属「长期对象互相引用」，不构成按任务累积的泄漏。
- 未能静态确认（低）：`Cleanup.run()`（[core/engine.py:463](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L463)、[core/engine.py:661](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L661)）的实际释放动作在 `recovery.py` 里，需真机句柄计数才能证伪「释放了但仍漏」。此项按「断环」判 PASS。

### 4.4 台账 O(N²) — PASS
- 修复点：[core/records.py:40-43](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L40-L43)（进程内累计缓存 + 指纹）、[core/records.py:348](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L348)（`_cache_add` 增量累计）、[core/records.py:402-433](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L402-L433)（`stats` 走缓存，`write_ok` 一并输出）。
- 覆盖判断：覆盖「每写一条就全量重扫」。缓存以 `_signature()`（`_paths` 的 `(path, size)` 列表，[core/records.py:270-289](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L270-L289)）判脏，跨进程追加能发现；轮转成功时强制作废（[core/records.py:230-234](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L230-L234)）。
- 残留（低）：指纹只含 `size`，外部同尺寸改写会漏（但落盘只追加，实际不会同尺寸）；`_repair_tail` 修残行改变了 size → 下一次 stats 丢缓存重扫一次，属一次性成本。

---

## 汇总表

### FAIL 项

| # | 项 | 位置 | 性质 | 严重度 |
|---|---|---|---|---|
| F1 | 自动恢复成功后 `load_ok=False` → 调度器拒启动、孤儿任务不复位、任务目录不清理、界面显示读失败 | `config.py:835-839` + `606-613` + `scheduler.py:458-465` | 修复引入新问题 | **P0** |
| F2 | `message` 通道在前台校验之后才分发，`ensure_foreground` 抢前台/抛错 → 通道唯一适用场景失效 | `input.py:598-610` + `engine.py:540-542` | 覆盖不全（调用方旧语义） | **P0** |
| F3 | 三方合并里 `queue` 仍整体取本地 → 外部新增任务被静默丢弃 | `config.py:634-667` | 原缺陷残留 | P1 |
| F4 | `_borrow_config` 在 `try` 之外 → 落盘失败时任务永久 RUNNING、`active_task` 残留 | `scheduler.py:906/917-939` | 异常路径未收场 | P1 |
| F5 | `/api/config` 拒收 `input_mode="message"`，且不收 `input_fallback`/`input_message_strict` | `server.py:121-122/129-143` | 调用方未接线 | P1 |
| F6 | 边界表只有写入口覆盖，引擎 5 处直读 `cfg[...]` → 手改配置仍可越界 | `switches.py:345-410` vs `engine.py:750/1541/1836/1857-1860/555` | 覆盖不全 | P1 |
| F7 | `message → legacy` 降级无日志/无状态出口，`channel_status` 未入 `status()`/API | `input.py:552-573` + `engine.py:560` | 覆盖不全 | P1 |
| F8 | `_input_mode` 未回写 effective，鼠标/键盘各自决策；日志把 message 写成 legacy | `engine.py:556-565` + `input.py:1023` | 状态位不同步 | P2 |

### 新增风险

| # | 风险 | 位置 | 说明 |
|---|---|---|---|
| R1 | 跨进程「隔离瞬间」另一进程走首启分支写默认值，可能盖掉刚恢复的配置 | `config.py:803-857` + `916` | 读路径未纳入 `_proc_lock`（TOCTOU） |
| R2 | NTP 单次丢包即放弃整台服务器，叠加 `MIN_SAMPLES=3` → 校时长期不更新 | `timesync.py:150-152` | 严判换可用性 |
| R3 | `_normalize` 把每任务 `workflow` 固化为全局值，切换全局路线对已排队任务失效 | `config.py:563-566` | 行为变化 |
| R4 | `_MIN_PRICE=100` 硬下限，低于 100 的挂单两条解析路径都拒 | `market.py:57/140/182` | 误杀合法输入 |
| R5 | `_rotate` 吞 `OSError` 且只在 spawn 时执行，日志上限可能永不生效 | `ocr_service.py:41-46/459/469` | 静默失效 |
| R6 | 看门狗重开依赖 `engine.start()`，卡死线程仍在 → 两次重开几乎必然失败 | `scheduler.py:919-938` | 恢复能力有限 |

### 通过项（PASS）
1.1 `.good` 原子化 · 1.2 台账残行 · 1.3 写互斥 · 1.4 list→countdown · 1.5 regions 合并 · 1.6 满盘兜底 · 1.7 台账失败可见（API 侧）· 2.1 NTP 校验 · 2.3 货币符号 · 2.4 `¥` 分支 · 2.5 单调时钟 · 3.3 UIPI · 4.1 看门狗机制 · 4.2 上限轮转 · 4.3 引用环 · 4.4 O(N²)

---

## 判定

**未通过**。F1/F2 是阻塞级：一个让「配置坏了也能救回来」变成「救回来也开不了抢」，一个让「不动光标的第三通道」在它唯一有用的场景下失效。F3/F4/F5/F6/F7 属于同一类问题 —— **改在了库函数层，调用方或另一条入口仍是旧语义**，正是本次复核要求重点排查的三处（调用方 / 并发 / 失败路径）里命中的两处。

## 建议的最小修复顺序（不含实现）
1. F1：恢复成功后不要用 `_load_error` 表达「已恢复」，改为独立字段（或恢复成功后清空），`load_ok` 只表示「盘上那份读不到」。
2. F2：`click()` 里 `if expect_foreground and self._mode != "message"`；或在 `_click_message` 前短路前台校验。
3. F4：`saved = self._borrow_config(task)` 移进 `try`，并用 `try/finally` 保证 `_return_config`（`saved is None` 时跳过）。
4. F5/F7：`_as_input_mode` 补 `message`、coercers 补两个字段；`Engine.status()` 增加 `channel`（`mouse.channel_status()`），`message` 日志分支补齐。
5. F3：`queue` 合并至少按 `id` 做并集（本地优先同名项），或明确在「本地队列脏」时把盘上新增项 append 回去。
6. F6：`_normalize` 对 5 个直读键套 `BOUNDS`（或在 `_CONFIG_COERCERS` 之外补一层读侧兜底），保证手改配置文件不会把引擎打挂。
