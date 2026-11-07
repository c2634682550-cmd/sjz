# 数值与时间基准加固报告（对应 `_bug_r03_numeric.md`）

范围：本分支拥有并已修改的文件 —— `core/timesync.py`、`core/switches.py`、
`core/ocr_chain.py`、`core/market.py`、`core/jobs.py`、`core/screen.py`、
`core/nav_col.py`；新增测试 `tests/test_numeric_hardening.py`。
第二轮返工经明确授权，对 `core/engine.py` 只做了**行级**接线（F6：把直读配置
换成带校验的读取入口，约 20 行，不重构）；`server.py` / `countdown.py` /
`scheduler.py` / `config.py` / `license.py` / 既有 `tests/` **未改动**，
需要那边改的进「待接线清单」。

验证：`python -m compileall -q core tests` 通过；新增测试 65 条全绿
（第一轮 48 条 + 第二轮 F6/R2/N5-N7 共 17 条）；本轮相关既有测试
（`test_switches.py` / `test_fix_a.py` / `test_countdown.py`）全绿，
`from core import engine` 通过、全仓收集 1724 条。

**并行分支冲突（不在本分支权限内，已上报）**：工作区里另有一支正在改
`core/money.py`（未跟踪文件）并把 `core/ocr_chain.price_from_text` 改成
委托给它，导致既有 `tests/test_ocr_price.py:94` 的
`"2,049 0+ 0 40+" → 40` 与 `tests/test_engine_no_dialog.py` 两条余额用例
同时变红（`price_from_text` 返回 `(None, '')`）。这与本分支第二轮改动无关：
`git status` 显示 `core/money.py` 为 `??`、`core/ocr_chain.py` 的改动来自
该分支；本分支只在测试里引用既有断言，没有改那份实现。其余环境性失败仍是
`ocr_rapid` 模型缺失和 `test_fix_b` 缺截图。

---

## 高

### N01 · NTP 应答无校验，全零包可把时钟算偏 126 年

**根因**：`_query` 只做了 `recvfrom` 就直接用四个时间戳算 offset/delay：
长度不查、mode 不查、stratum 不查、origin 不回显校验、时间戳可以是 0。
一条 48 字节全零报文 → `t2=t3=0` → offset ≈ −4.2e9 秒（126 年）；
delay 还算出 0，`sync` 按「delay 最小」选最佳样本，正好把这条选上。
`sync` 只收 1 个样本就更新状态，也没有离群处理。

**改法**（[timesync.py](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py)）：
1. 请求的 transmit 字段写本次发送时间戳，低位掺 16 位随机数，应答的
   **origin 必须原样回显**才算匹配 —— 全零包、固定回显包、错配主机全部挡掉。
2. 校验链：`len(data) >= 48` → `mode ∈ {4,5}` → `stratum ∈ 1..15`
   （0=KoD，16..255 保留）→ origin 匹配 → `t2,t3 > 0` → `t3 >= t2-1`
   → `|offset| <= 86400s` → `|delay| <= 5s`。任一不过抛 `ValueError`，
   该样本丢弃。
3. 零点附近的微小负 delay（同网段测量噪声）夹到 0，其余照旧丢弃。
4. `sync` 改为：**少于 3 个有效样本不更新状态**；有效样本按 offset 中位数
   剔除偏离 >60s 的离群值，最终 offset 取**中位数**、delay 取存活样本的最小
   往返。delay=0 的伪造包不再因为「延迟最小」被选中。
5. 失败路径只写 `last_error`，不动 `offset/at/samples`（异常不污染时钟状态）。

**回归证据**（`tests/test_numeric_hardening.py`）：
`test_all_zero_reply_is_rejected` / `test_short_reply_is_rejected` /
`test_out_of_range_offset_is_rejected` / `test_forged_origin_is_rejected` /
`test_zero_delay_outlier_does_not_win`（4 个样本里 1 个 `(offset=1234, delay=0)`，
结果必须是正常三条的中位数 0.42、server 为 c）/ `test_too_few_valid_samples_keep_old_state`。
「修前」：全零包返回 −4.2e9；「修后」：抛 `ValueError`/保留旧状态。
第二种复核：测试里用 `statistics.median` 独立重算期望值；另有独立脚本用
排序取中与 numpy 三方比对一致。

### N03 · 开关数值入口不校验，NaN/Inf/负数静默进入引擎

**根因**：`_coerce` 对 int 用 `int(float(value))`、对 float 直接 `float(value)`。
`"nan"` 合法通过（`fire.threshold` 比较恒假 → 永不识别）；`-0.05` 通过
（`scan.interval` → `sleep(-0.05)` 抛错，线程崩）；`"inf"` 通过
（`fire.timeout` → while 条件恒真 → 无限连点）；`follow.retry="inf"` 让
`int(inf)` 抛未捕获 `OverflowError`，整次保存 500。

**改法**（[switches.py](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py)）：
1. 新增 `SwitchValueError(key, reason)`，消息形如
   `fire.threshold：必须是有限数字（NaN/Infinity 不接受）：'nan'`。
2. 新增集中边界表 `BOUNDS`，覆盖**全部 22 个数值开关**：int/float 统一走
   `_number()`：先 `float()` 捕获类型错误 → `math.isfinite` → 上下界 →
   再按 kind 截断。`inf` 在 isfinite 就被拒，不会走到 `int()`。
3. `set()` 非法即抛；`apply_all()` 改为「先全部校验、再统一落盘」，一批里
   有一个非法就整批拒绝并列出所有字段 —— 调用方即使忘了 `Config.transaction`
   也不会出现半截写入。
4. `get()` 对**手改坏的 config.json** 做安全回退：脏值（NaN/负数/超界）
   返回该开关默认值，并记进 `switches.issues()`，引擎永远拿不到脏数值。

**回归证据**：`test_numeric_bounds_cover_every_numeric_switch`（表完整性）、
`test_switch_values_are_rejected`（8 组参数：nan / −0.05 / inf / 0 / 1.5 / 11）、
`test_follow_retry_inf_raises_switch_error_not_overflow`、
`test_dirty_config_value_falls_back_to_default`、`test_apply_all_is_all_or_nothing`、
`test_valid_values_still_pass`。既有 `tests/test_switches.py` 全绿。

### N05 · `offset()` 的有效期用壁钟，回拨一年旧偏移永不过期

**根因**：`_state["at"] = time.time()`，`offset()` 用 `time.time() - at` 判 TTL。
回拨 365 天 → age 为负 → 永远「新鲜」，把 123ms 旧偏差无限期当准确值；
前跳 3 天 → 立刻归零，补偿瞬间丢失。

**改法**：锚点同时记 `at`（壁钟）与 `at_mono`（`time.monotonic()`）。
TTL **只走单调轴**；另加判据「壁钟相对 at 回退 > 2s = 有人在改时间」→ 失效。
前跳不再影响有效期；旧状态（只有 `at` 没有 `at_mono`）保留壁钟判据以兼容。
`info()` 增加 `stale` / `stale_reason`，stale 时上报 offset=0（与实际可用值一致）。

**回归证据**：`test_forward_clock_jump_keeps_offset`（前跳 3 天仍为 0.7）、
`test_backward_clock_jump_invalidates_offset`（回拨 60s → 0.0 且原因含「回拨」）、
`test_ttl_still_expires`、`test_legacy_wall_anchor_still_works`（既有
`test_fix_a::test_stale_offset_is_ignored` 依赖这条，实测通过）。

### N06 · `price_from_text` 取最后一个数字串，`¥1,234.5` 读成 5

**根因**：无脑 `finditer` 取末尾 token。`¥1,234.5` → `5`；`¥12 345` → `345`；
`1,234.567` → `567`；`-5` → `5`（负号丢失）。价格被缩小 10~1000 倍，
预算/限价闸门形同虚设。

**改法**（[ocr_chain.py](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_chain.py)）：
1. 新增 `_parse_currency_amount()`：有 `¥/￥` 时把符号后**整段**当一个数解析：
   空格/逗号分隔的三位组先合并（只认「后接正好 3 位数字」的空格），
   逗号分组必须严格 `\d{1,3}(,\d{3})+`，小数最多 2 位，负号拒识，
   1~9 位、禁止前导 0。解析不出返回 None。
2. 无货币符号时保留「取最后一个数字串」的既有语义（价格条只印一个数），
   但新增三条拒识：紧贴 `-`/`−`、是小数尾段（前一位是 `.`）、逗号悬空。
3. 任何解析不出都返回 `(None, token)`，绝不返回尾数。

**回归证据**：`test_currency_amount_parses_as_a_whole`（`¥1,234.5→1234.5`、
`¥12 345→12345`、`¥ 250,000→250000`）、`test_bad_amounts_are_refused`
（`¥1,234.567`、`¥12 34`、`¥-5`、`-5`、`1,234.567` 全 None）、
`test_existing_ocr_cases_unchanged`（既有锚点不回归）；`tests/test_ocr_price.py`
全绿。独立复核：用 `Decimal` 手工核算 4 组期望，差异 0。

### N07 · `parse_row` 的 `¥` 分支无下限、允许小数、丢负号

**根因**：`price = _to_number(m.group(1))`，`¥5→5.0`、`¥15.5→15.5`；
`_NUM` 正则在 `-15000` 上只匹配 `15000`，负号被丢 → `+15000`。
`pick()` 按价格升序，0 元/假低价行永远排第一。

**改法**（[market.py](file:///C:/Users/26346/Desktop/project/dafors/core/market.py)）：
1. `_CURRENCY` 增加可选符号组；`¥` 分支要求**整数**、**≥ 下限(100)**，
   带负号或带小数点直接判不可用。
2. 普通数字扫描改用 `finditer`，紧贴 `-`/`−` 的 token 直接跳过（保留符号语义）。
3. `check()` 增加兜底：`price` 非有限 → 拒；`price <= 0` → 拒并给原因
   「价格 0 不可信」。`pick()` 因此不可能再选中 0 元行。
4. `check()` 的 `min_price/max_price/max_wear` 改走 `_task_number()`：
   非有限或负数按「未设置」处理 —— `NaN` 不能把上下限静默关掉（N14）。

**回归证据**：`test_parse_row_currency_semantics`（6 组）、
`test_pick_never_takes_zero_price_row`（0 元行被拒、15,000 被选中且 diag 含「不可信」）、
`test_check_ignores_nan_limits`；`tests/test_market.py` 全绿
（`¥15,000` 仍为 15000，“15 252” 等空格千分位行为不变）。

---

## 中

### N08 · 超时/耗时用壁钟

已成单调时钟：
- `jobs.py`：`started` / `finished` / `elapsed` 全部 `time.monotonic()`，
  elapsed 再夹 `max(0, …)`。
- `screen.wait_frame_change()`：`t0`、循环判据、返回值全部 monotonic。
- `nav_col._wait_title_change()`、`_wait_skin_list()`：同上。
- `server._wait_job`（server.py）**只读未改** → 待接线。

回归证据：`test_jobs_elapsed_survives_backward_wall_clock`、
`test_wait_frame_change_uses_monotonic`、
`test_wait_title_change_uses_monotonic` / `test_wait_skin_list_uses_monotonic`
（用只回拨的壁钟代理，并断言壁钟调用次数为 0）。

### N12/N18 · 归一化区域非有限值、像素取整

`Screen.norm_to_px`（screen.py）：逐字段校验必须是有限数，否则抛带字段名的
`ValueError`；像素统一 `int(round(...))`。0~1 与 `x+w<=1` 的**启动期校验**
依赖 config 入口 → 待接线（config.py 不在权限内）。
回归：`test_norm_to_px_rejects_non_finite`。

注：落地期间检测到 `screen.py` 被并行分支重写过一次（per-monitor DPI +
`wait_frame_change` 内联 `import time`）。已把 `import time` 收回模块级，
保证计时可测且不重复导入；`norm_to_px` 的校验与取整逻辑保留。

---

## 第二轮返工（独立复核未过项）

### F6 · 22 条边界表只挡写入口，引擎直读配置仍能把引擎打挂

**根因**：`switches.BOUNDS` 只保护 `set/apply_all` 这条写入口。引擎里
`cfg.get("threshold")`、`int(cfg.get("confirm_clicks"))`、
`float(cfg.get("confirm_timeout_s"))` 等直读路径完全绕过校验，手改
`config.json` 写 `NaN/Inf/负数/"abc"` 即可让列表模式永不停、线程
`sleep(-3)` 崩、`int(inf)` 抛 `OverflowError`。

**改法**（改动只到行级，不重构引擎）：
1. [switches.py](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py)
   新增带校验的读取入口
   `read(cfg, path, default, kind="int", lo=None, hi=None, key=None)`：
   缺失返回默认值；`float()` 失败 / `NaN / Inf` / 越界 → 记进
   `issues()` 并返回默认值；合法值原样返回。`_number()` 增加可选
   `bounds` 参数，供非开关表字段显式传边界。
2. [engine.py](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py)
   复核点名的 5 处全部换掉：
   [602](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L602)
   `monitor`、[830](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L830)
   `fire.threshold`、[1764-1768](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1764-L1768)
   `scan.*`、[2134-2170](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2134-L2170)
   `fire.hz/fire.timeout/confirm_clicks/confirm_gap/fire.delay/fire.jitter`、
   [2344](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2344)
   `confirm_timeout_s`。
3. 同类直读顺带兜住（同样是「手改配置打挂引擎」，不再有绕过口）：
   [849](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L849)
   `arm_grace_s`（`inf` 会 `sleep` 挂死）、
   [941](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L941)
   `countdown_attempts`（`"abc"` 直接 `ValueError`）、
   [2053](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2053)
   `stop_after_fails`、[2448-2449](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L2448-L2449)
   `follow_retry/follow_wait_ms`。

**实际证据 1（读取入口）** —— 手改值全为脏值时：

```
switches.read('monitor')          -> 1   （默认 1，已记录）
switches.read('arm_grace_s')      -> 1.5 （默认 1.5，已记录）
switches.read('confirm_timeout_s')-> 2.0 （默认 2.0，已记录）
引擎读到的值: threshold=0.85 click_hz=50 fire_timeout=8.0
```

**实际证据 2（引擎照常跑完）** —— 把 `threshold=NaN`、`click_hz=Inf`、
`fire_timeout_s=-5`、`confirm_clicks="not-a-number"`、
`confirm_interval_ms=1e9`、`list_scan_interval=-3`、`list_scan_timeout=NaN`、
`list_detail_timeout=Inf`、`list_max_tries=-7` 全部塞进 cfg：

```
tests/.../test_engine_list_mode_survives_hand_edited_out_of_range_config PASSED
tests/.../test_fire_and_confirm_survives_hand_edited_out_of_range_config PASSED
```

列表模式走到 `DONE`、只点 1 次购买；连点/确认路径 1 次购买 + 1 次确认后
正常返回 `True`；每个脏字段都在 `switches.issues()` 里留下记录。

### R2 · 首次丢包就放弃整台服务器，叠加 MIN_SAMPLES=3 导致校时长期不更新

**根因**：采样循环 `for _ in range(samples)` 里任何一个异常都 `break`，
直接放弃整台主机。三台服务器各丢一个包 → 0 个样本 → 保留旧 offset，
下一次还是同样结果，校时长期不更新。

**改法**（[timesync.py](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py)）：
1. 每台服务器改成「凑够 `samples` 个**有效**样本」的循环：
   `while ok_here < samples` —— 丢包只占请求次数，不占样本名额。
2. 单次失败只计一次 `fails` 并就地重试；退避
   `min(0.5, 0.1 * 2**(fails-1))`（0.1 → 0.2 → 封顶 0.5）。
3. 连续失败 `MAX_CONSEC_FAIL=3` 次才本轮放弃该主机，转到下一台。
4. 单轮总请求硬上限 `MAX_TOTAL_ATTEMPTS=16`（3 台×4 次基准 + 4 次重试余量），
   全都不通也不会无限重试。

**实际证据（连续丢包行为）**：

```
A 单次丢包（a 首包丢失）      ok=True  请求=7  样本=7  退避=[0.1]
B 一台持续丢包 → 轮换         ok=True  请求=9  样本=7  退避=[0.1, 0.2]
C 全部丢包 → 请求上限         ok=False 请求=16 样本=0  退避=[0.1,0.2]×5+[0.1]
```

A 证明单次丢包不会让该主机少样本、也不会整台放弃；B 证明连续 3 次失败才
轮换且其余主机正常出样本；C 证明上限生效（正好 16 次，不会无限重试）。
对应测试：`test_single_packet_loss_is_retried_not_abandoned` /
`test_consecutive_failures_abandon_host_and_rotate` /
`test_total_attempts_are_capped` / `test_backoff_grows_and_is_capped` /
`test_isolated_losses_do_not_starve_the_update`。

### N5/N6/N7 · 时间同步能被单源带偏 11 小时

**根因**：判据只看「单个包合不合理」+「有效样本 ≥3」。4 个样本全来自同一台
伪造源、一起偏 +40000s 时，每个包都自洽、数量也够；3 个域名被 DNS 投毒到
同一 IP 时，「3 个主机」被当成 3 个来源。一致性本身反而成了伪造的掩护。

**改法**（[timesync.py](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py)）：
1. **来源独立性**：`_resolve_ip()` 解析每个主机名，样本按 IP 去重
   （`_dedupe_by_source`）；同一 IP 的重复应答只算 1 个样本，
   找不到 IP 时退化为按主机名去重。
2. **网段多样性（严格路径）**：解析得到 IP 时，≥3 个独立 IP 且覆盖 ≥2 个
   /16 网段才走严格路径；不满足则进入降级判定。
3. **偏差量级**：`MAX_PLAUSIBLE_OFFSET_S=300s`（分钟级），超过即拒收并给出
   可读原因；`sync(allow_large_offset=True)` 或环境变量
   `DAFORS_NTP_ALLOW_LARGE_OFFSET=1` 为显式放宽（放宽后硬上限仍是 24h）。
   包级 `_query` 的 86400s 只作为脏包兜底，不再作为可信判据。
4. **一致但离谱**：所有样本偏离中位数 ≤60s 且绝对值超阈值 → 原因里明确写
   「所有来源高度一致但绝对值异常大 —— 同源伪造的典型特征」，不因一致而采纳。
5. **单调时钟交叉验证**（`_drift_conflict`）：用上一锚点的
   `(at, at_mono)` 算「期间壁钟 vs 单调时钟」差值，得到 offset 应有的变化量；
   实测变化量与漂移预算（`max(0.6s, 3×500ppm×Δ)`）矛盾则拒收 ——
   壁钟被回拨/被改、或外部源在说谎都会被拦下。
6. **单源降级（黑盒复核追加，不能把单源用户判死）**：只有「只配了 1 个主机
   且只解析出 1 个来源」才允许走降级路径，且必须同时满足：
   `|offset| ≤ 300s`（硬上限，不随 allow_large_offset 放宽）、
   与单调时钟漂移一致、**连续 ≥2 个采样**且互相偏离 ≤ `DEGRADED_STABLE_S=1s`。
   通过则 `ok=True, degraded=True`，`logging.warning` 写警告日志，
   `info()` 暴露 `degraded` / `warning` 供界面显示。多个域名塌缩到同一 IP
   仍按 DNS 投毒拒绝，不享受降级。
7. **内置源自动补齐**：`DEFAULT_SERVERS` 含 5 个不同来源（阿里云、腾讯云、
   NTP Pool、AWS、国家授时中心）。没配 → 全用默认；只配 1 台 → 自动补齐到
   多源（除非显式声明 `only_configured=True` /
   `DAFORS_NTP_ONLY_CONFIGURED=1`，此时才按单源走降级）。

**实际证据（六种场景，脚本实跑输出）**：

```
① 单源正常（降级）              ok=True  degraded=True  样本=5 offset=0.2005
   警告: 单源降级校时：仅 1 个 NTP 来源（a），偏差 0.201s，已用 4 个稳定样本确认
② 单源偏 11 小时（拒）          ok=False degraded=False 样本=4
   原因: 偏差 40000.0s 超出可信范围 ±300s（1 个独立来源）；所有来源高度一致
         但绝对值异常大 —— 同源伪造的典型特征
③ 多源正常（严格）              ok=True  degraded=False 样本=4 offset=0.32
④ 3 域名同 IP（拒）             ok=False degraded=False 样本=6
   原因: 独立来源只有 1 个（有效样本 6 个，需要 ≥3 个来自不同 IP）
⑤ 只有 1 个 IP 且偏差稳定（降级）ok=True  degraded=True  样本=4 offset=0.3
⑥ 只配 1 台但未声明「只用这些」  ok=True  degraded=False 样本=7（自动补齐 5 个默认源）
```

对应用例：`test_single_source_normal_sync_degrades_but_succeeds` /
`test_eleven_hours_offset_from_one_source_is_rejected` /
`test_independent_multi_source_syncs_normally` /
`test_three_hostnames_on_one_ip_are_one_source` /
`test_single_source_one_sample_is_not_enough_for_degraded` /
`test_single_source_unstable_samples_are_rejected` /
`test_unset_servers_fall_back_to_builtin_list` /
`test_one_configured_server_is_auto_filled_to_multi_source`，另有
`test_clock_backstep_is_rejected_by_monotonic_cross_check`、
`test_consistent_but_absurd_offset_across_sources_is_rejected`、
`test_sources_within_one_net16_are_rejected`、
`test_large_offset_needs_explicit_confirmation` 覆盖其余分支。

**复核**：`tests/test_numeric_hardening.py` 69 条全绿；相关既有测试
（switches / fix_a / countdown / 引擎列表与连点 8 个文件）217 passed +
8 skipped；`python -c "from core import engine"` 通过，
`--collect-only` 收集 1724 条。

---

## 待接线清单（要改但不属于本分支权限）

1. **`core/scheduler.py`（N02）**：`server._as_int(0)` → `days=0/interval=0/hhmm=0`
   合法通过，落入「无到期日 + 到期判据」分支。入口需：`days >= 1`、
   `interval_min >= 1`、`hhmm ∈ 0..2359` 且分秒合法、`quota` 同 switches 边界。
   `windows=[]` 不应表示「全天」。
2. **`core/server.py`（N04）**：`_as_int/_as_float` 静默回落默认值（`"abc"`→默认、
   `-999`→-999）。应改为有限性 + 上下界校验，非法返回 400；同时把
   `switches.SwitchValueError` 映射成 400（现在会落到通用 500，消息虽可读但状态码不对）。
   `_wait_job` 的超时改 monotonic。
3. **`core/server.py`（N13）**：登录失败计数 `+=1` 只增不减、无窗口，会把正常用户
   永久锁死。改成「滑动窗口内计数」或成功登录清零。
4. **`core/config.py`（N12/N13）**：region 读取时校验 0≤x,y,w,h≤1 且 `x+w<=1`、
   `y+h<=1`，非法即拒绝启动并指明字段；数值开关默认值在加载时走
   `switches.BOUNDS`（现在手改 config 的脏值只会在 `get()` 时回退，UI 仍显示脏值）。
5. **`core/countdown.py`（N09/N10）**：`_parse_text` 的 `1.999秒→999`、
   `-5秒→5`；`fire_at` 缺上限。按「整段正则 + 拒绝负号/小数秒」修。
6. **`core/engine.py`（N16/N17）**：价格/预算比较用 `round(price)` 整数比较
   （浮点误差）。毫秒字段已改走 `switches.get`（R2 返工），下限由边界表兜住。
7. **`core/records.py`（N15/N18）**：负价行仍会被写入记录并参与统计；金额显示
   缺 `¥` 与千分位、单位混用（元/万）。统一格式化并过滤 `price<=0`。
8. **`core/license.py`（N11）**：期限比较若用本地壁钟，回到过去即可续命；
   改用「单调 uptime + 服务端时间」双判据（该文件不在本分支权限内）。
9. **`web/nav_ons.js`（N08）**：节流时间戳用 `Date.now()`，系统时间回拨会卡死；
   改 `performance.now()`（前端文件不在本分支权限内）。
10. **`tests/test_fix_d.py` / 其余全量失败**：本次全量运行 28 failed，均落在
    其他分支在改的文件上（`engine.stop` 的 alive 判据、`web/index.html` 静态、
    `server` 403、`ocr_rapid` 模型缺失、`tools/_r08_dbg.py` BOM 等），与
    timesync/switches/ocr_chain/market/jobs/screen/nav_col 无关；本分支相关
    测试 134 通过。

---

## N11 补：价格解析的数量级 / 跨行（2026-11-07）

故障注入复核判出的 N11：价格解析在「没有货币符号」和「跨行」两种情况下
读错数量级，预算闸门因此误放行/误拦截。

### 一、三条复现（修前 → 修后）

| 输入 | 修前 ocr_chain | 修前 market | 修前 cards | 修后（三处一致） |
|---|---|---|---|---|
| `15 252` | 252 | 15252 | 252 | 15252 |
| `1 234 567` | 567 | 1234567 | 567 | 1234567 |
| `¥12 345` 换行 `678` | 12345678 | 12345678 | 678 | 12345 |

修前错因：
- 无符号分支取「最后一个数字串」→ 多段数字只剩尾段；
- `_join` 无条件用空格拼词、`_CURRENCY_AMOUNT` 用 `\s` 吃换行、`ocr.clean`
  把 `\n` 压成空格 → 解析窗口跨行，吞掉下一行；
- 三处各有一份正则，同一个输入读出三个值。

### 二、修法定稿

1. **收敛到一处**：新建 `core/money.py`，`ocr_chain.price_from_text`、
   `market.parse_row`、`cards._price_token_in` 全部改为调用它；三处只保留
   「价格判定策略」（下限、型号 token 排除、磨损/价格分工），语法不再各写一份。
2. **无货币符号**：相邻数字段按千分位分组**整段合并**（首组 1~3 位、其后每组
   正好 3 位）；合并失败即拒识，不回落到尾段。逗号与空格混用、逗号分组不严
   （`9,9` / `1,00,000` / `1,000,`）一律拒识。
3. **跨行**：`_join` 按词框 y 分行（同行空格、跨行换行）；`ocr.clean` 改为逐行
   清洗保留 `\n`；`parse_row` / `scan_text` / `cards` 逐行解析，遇换行即截止。
4. **逐类规则**：
   - 小数：金额最多 2 位（`¥1,234.5` → 1234.5）；4~6 位只作为磨损值，价格路
     径拒识；
   - 三位小数：默认拒识（磨损或没读全）；仅交易行/价格带显式
     `dot3_as_thousands=True` 且整数段 >5 时按千分位还原（`9.000`→9000），
     整数段 0~5 仍当磨损（`2.500`→2.5）；
   - 负号：`−¥250`、`-5`、紧贴数字的负号一律拒识（不丢号变正数）；
   - 全角：先 NFKC 归一，`１２３`→123，全角逗号/句点/空格同规则；
   - 前导 0：语法层拒识；价格带/交易行显式 `strip_leading_zeros=True`
    （金币图标，`025,000`→25000）；
   - 位数 1~9 位，超长按「没读全」拒识；
   - 字段标记（`+`/`%`）所在行，空格视为字段分隔（余额行
     `2,049 0+ 0 40+` → 40 的既有行为不变）。

### 三、判据

对每个输入，三个入口的结果必须是「正确值」或「明确拒识（None / ""）」，
不允许「读到一个错值但看起来合理」。用例落在 `tests/test_price_units.py`
（24 条），覆盖三条原案、`¥1,234.5`、`¥12 345`、`−¥250`、`１２３`、
`1,234 567`、`12 34`、`1 234.567`、`9,9`、`1,00,000`、`1,000,`、
`0.583 172`、`1234567890` 及跨行词拼接。

### 四、验证

- `tests/test_price_units.py` 24 passed；
- 既有价格相关 `test_market.py / test_cards.py / test_fix_c.py /
  test_numeric_hardening.py` 136 passed（余额行、`05`、`9.000`、
  `2.500`、`0.583172`、`0.120 9,500` 等锚点不变）；
- 全量 `pytest tests`：1836 passed，失败项全部落在 license/precision 等
  与本分支无关的模块（签名密钥长度、p95 计时；当时并行跑了两个 pytest，
  计时项受负载影响）。

### 五、仍会被拒识的畸形输入（明确清单）

- `12 34`（空格分组不成立）、`1,234 567`（逗号+空格混用）、`1 234.567`
  （三位小数）、`9,9`、`1,00,000`、`1,000,`（逗号分组不严）；
- `−¥250`、`-5`、`¥-5`（负金额）；
- `025,000`（OCR 后处理路径；价格带/交易行按策略 strip）；
- `0.583 172`（小数+残片：只留磨损，不猜价格）、`¥1,234.5`（价格带里
  的小数金额）；
- 10 位以上（`1234567890`）、一行出现两个 `¥`、带货币符号但整段解析失败
  （不回落到行内其它数字）。

