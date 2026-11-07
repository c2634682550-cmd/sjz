# 自我诊断规则库（evo/diagnose）

## 0. 定位与边界

这一层只做**判断**，不做动作：

- 输入：历史日志 + 遥测（cycles / metrics）+ 台账 + 目录残留；
- 输出：一段时间窗内的**根因排序**（置信度、证据文件:行号、建议动作名）；
- 动作全部引用 `core/recovery.py` 已有的原子动作，**不新造**；
- 场景 id 全部引用 `core/scene_guard.py` 的 `SC_*`；
- 不改 `core/`，不 import `core/`，只做静态解析校验；
- 判不出来是合法结论：`ambiguous / conflict / insufficient` 都是一等公民。

规则与现象都用 JSON 描述，加规则不需要改代码。

```
evo/diagnose/
  rules/phenomena.json   28 个现象（五层）+ 根因定义（标签/先验/场景/动作/误判风险）
  rules/rules.json       35 条规则（hard / stat / corr 三档）
  rules/bench_cases.json 回测期望映射（soak 注入类型 + 22 条真实台架行）
  model.py               证据 / 根因命中 / 诊断结果
  sources.py             日志、cycles、metrics、台账读取（含防泄漏清洗）
  rules.py               规则加载、静态校验、六类匹配器
  engine.py              推理与放弃条件
  report.py              文本渲染
  bench.py               历史数据回测（窗口级 / 安静窗口 / 行级）
  cli.py                 命令行入口
```

## 1. 数据真源与防泄漏

| 来源 | 路径 | 用途 | 清洗 |
|---|---|---|---|
| soak 日志 | `_soak_data/*/soak.log` | 告警/错误文本、行号证据 | 剔除 `[fault]`、`fault_start/end/recovered` 行 |
| 逐轮遥测 | `cycles.jsonl` | state/clicks/hit_roll/bought/drift_ms | 剔除 `fault_active` 字段 |
| 采样遥测 | `metrics.csv` | cpu/rss/handles/queue/ledger_bytes | 剔除 `faults_active/faults_total/faults_recovered` 列 |
| 台账 | `data/records.jsonl` | 坏行、半行、末行换行 | 只读；坏行保留为证据 |
| 目录残留 | `config.json.broken-*.bad` 等 | 配置损坏的物证 | 必须 mtime 落在窗口内才采信 |
| 台架结果 | `_fi/out/*.txt` | 行级真实异常样本 | 按 `RESULT|case|...` 单行构造 |

防答案泄漏有三条硬规则：`fault_active`、`faults_*`、`[fault]` 一律不进入推理；`events.jsonl` 只用于**切窗和打分**，不参与特征；`bench_cases.json` 只在回测侧读取。否则回测会变成抄答案。

## 2. 现象库（28 条，五层）

字段说明：**信号** = 哪个字段/日志能看出来；**根因(先验)** = 按概率排序；**区分** = 二次观测；**动作** = 现有 recovery 动作；**置信** = hard/stat/corr 上限；**风险** = 误判风险。

### 2.1 输入层（7）

| 现象 | 可观测信号 | 根因（先验） | 区分方法 | 建议动作 | 置信 | 误判风险 |
|---|---|---|---|---|---|---|
| 点击无效果 `in.click_no_effect` | warn「点了 N 下都没看到结算弹窗」；cycles `state=abort & clicks>=3`；台架 `accepted=True down=0` | 消息被受理但游戏不处理 0.55；普通漏抢 0.2 | 看 `hit_roll`：roll 中了却没买到才是故障；查 down 计数 | alt_template, reread_crop, ensure_foreground | .90/.72/.55 | 中：同一句 warn 普通漏抢也打，单条不足以定性 |
| 光标未动 `in.cursor_not_moved` | `click=True down=1 move=1` 但「光标未动=True」；降级 legacy 后光标没真动 | 借光标通道没把光标放到位 0.6 | GetCursorPos 前后位移；看降级原因码 | reset_cursor, alt_template | .88/.68/.50 | 中：`effective=message` 是正常路径，单独出现不算故障 |
| UIPI 拦截 `in.uipi_blocked` | 「游戏窗口的权限比本程序高（本程序 0x2000）」「UIPI 会把消息全部拦掉且不报错」；`code=UIPI_BLOCKED` | 完整性级别不一致 0.85 | 对照两侧完整性级别；提权后单点复测 down 计数 | reconnect_input, record, manual_handoff | .95/.70/.50 | 高：动作库治不了根，应输出「无可用动作」并转人工 |
| 注入器掉线 `in.injector_down` | `RuntimeError: injector unavailable`；调用栈落在 mouse.click → _click | 注入器进程/驱动掉线 0.7 | IsWindow 排除句柄失效；reconnect 后空点击探针 | reconnect_input, restart_worker | .93/.70/.50 | 低：错误文本唯一 |
| 目标窗口销毁 `in.target_lost` | 台架 `已销毁=True` → `code=INPUT_ERROR`；ensure_foreground 连续失败 | 旧 hwnd 失效 0.7 | IsWindow + pid 存在性；标题/尺寸比对 | rebind_window, restore_window | .90/.70/.50 | 低 |
| 抢不到前台 `in.foreground_lost` | warn「抢不到前台」，abort 归 `window_lost`；句柄有效但不在前台 | 被其它程序抢占或系统拒绝前台切换 0.7 | IsWindow 对照；GetForegroundWindow 的 pid/类名；ensure 后复测 clicks | ensure_foreground, restore_window, manual_handoff | .90/.72/.55 | 中：系统前台锁定策略下重试可能永远不成功 |
| 输入通道降级 `in.input_degraded` | 「输入通道降级：message → legacy，原因码=NO_TARGET_HWND」；`degraded=True` | 没有目标句柄，降级保护 0.6 | 查句柄为什么没了；降级后是否真能点中 | reconnect_input | .85/.65/.45 | 中：降级是保护行为，不必然是故障 |

### 2.2 视觉层（5）

| 现象 | 可观测信号 | 根因（先验） | 区分方法 | 建议动作 | 置信 | 误判风险 |
|---|---|---|---|---|---|---|
| OCR 读空/超时 `vi.ocr_timeout` | warn「OCR 失败：TimeoutError」；该窗口没有价格/倒计时数值 | 识别服务超时 0.65 | 异常类型：TimeoutError vs 返回空串；同窗口 cpu/queue | restart_ocr, alt_ocr, wait_retry | .92/.70/.50 | 低 |
| OCR 垃圾/误读 `vi.ocr_garbage` | warn「显示值 X 秒已经 Y 秒没再跳」；价格解析出荒谬值 | 裁剪/尺度漂移 0.50 | 页面锚点模板分数；reread_crop 后数值是否恢复跳秒 | reread_crop, reread_scale, alt_ocr | .85/.60/.45 | 高：与「界面不认识」同症状，现有数据分不开 |
| 价格格式不通 `vi.price_parse` | `'15 252'→252`、`'1 234 567'→567`、价格为 None/量级明显不对 | 解析器把空格当分隔符 0.60 | OCR 原始串与解析结果逐字符对照；换裁剪是否还丢段 | reread_crop, alt_ocr | .90/.70/.50 | 中：直接影响买/不买，高危，宁可转人工 |
| 模板分数低 `vi.template_low` | 锚点分数低于阈值进 `ui_changed`；坐标点击落空；多尺度都上不去 | 分辨率/缩放/UI 改版 0.55 | 多尺度重匹配：回升=尺度，仍低=改版 | alt_template, reread_scale, recalibrate | .85/.60/.45 | 高：历史日志没有 match score 字段，规则只能等补齐 |
| 界面不认识 `vi.unknown_page` | 出现「没再跳」但页面已切走；锚点识别失败进 `unexpected_page` | 弹窗/活动页/断线页覆盖 0.60 | 查关闭按钮/确认框模板；esc 后重新识别锚点 | close_popup, esc, tab_reenter, nav_home | .88/.62/.45 | 高：与 OCR 垃圾同症状，需锚点分数 |

### 2.3 时序层（4）

| 现象 | 可观测信号 | 根因（先验） | 区分方法 | 建议动作 | 置信 | 误判风险 |
|---|---|---|---|---|---|---|
| 倒计时反推漂移 `ti.countdown_drift` | `drift_ms` 突增（正常 10~20ms，历史最大 1428ms）；`drift_p95_ms` 抬升 | 跳秒采样偏差累积 0.50 | NTP offset 对照；看 drift 是单点尖峰还是持续抬升 | resync_clock, wait_retry | .85/.68/.50 | 中：<200ms 淹没在正常抖动；尖峰在磁盘停顿窗口同样出现 |
| 时间源不可信 `ti.time_source_untrusted` | 「所有 NTP 都不通/应答不可信」；同源一致偏 +40000s 仍 `ok=True`；offset ≫100ms | 对时源被污染/单源投票 0.60 | 直接读 offset（>1s 基本可判）；看多源分布与样本数 | resync_clock, record | .92/.72/.50 | 低：offset 数值本身就是证据 |
| 倒计时停跳 `ti.countdown_stall` | warn「倒计时停了」；连续多帧同一秒数 | 倒计时真的结束/界面不再变化 0.30 | 帧差：有变化=OCR 卡，无变化=页面停；锚点分数 | reread, wait_retry | .80/.62/.45 | 高：与 OCR 垃圾/未知页面同症状，默认应 ambiguous |
| 卡点误差突增 `ti.anchor_error` | 台账 elapsed/offset_ms 突然变大；`drift_ms` 单点尖峰后回落 | 单轮调度/渲染抖动 0.55 | 同窗口 cpu/queue 对照；看序列形态 | wait_retry | .82/.65/.45 | 中：单点误差常是正常抖动 |

### 2.4 数据层（7）

| 现象 | 可观测信号 | 根因（先验） | 区分方法 | 建议动作 | 置信 | 误判风险 |
|---|---|---|---|---|---|---|
| 配置读写失败 `da.config_read_fail` | 「设置读不出来/回退默认」；残留 `config.json.broken-*.bad`；`PermissionError` | 配置损坏 0.55；写不进 0.20 | JSONDecodeError=读坏；PermissionError=写不进；restore 后校验键 | reload_config, restore_config, record | .92/.70/.50 | 低：注意「回退默认」是保护不是故障 |
| 配置值越界 `da.config_value_invalid` | `OverflowError: int too large to convert to float`；22 项上下界越界被拒 | 手改/迁移写入超大值 0.75 | 逐键 get+snapshot 找第一个抛错键；restore 后复测 | reload_config, restore_config | .93/.70/.50 | 低 |
| 台账残行 `da.ledger_residual` | `UnicodeDecodeError: unexpected end of data`；「上次崩溃留下 82 字节残行」；末行无换行 | 写一半被杀 0.70 | 读尾部字节：无换行=粘行，解码错=残行 | record, restart_round | .93/.70/.50 | 低：动作库没有「修复台账」动作，需补 |
| 台账写失败 `da.ledger_write_fail` | 「写台账失败（PermissionError）——这一行没有落盘」；`add` 返回 None；ledger_bytes 不涨 | 文件被占用/只读/目录不可写 0.65 | PermissionError vs 位置错乱；换可写目录试写 | record | .92/.70/.55 | 中：不阻断抢购易被忽略；「成交涨但台账不涨」在当前 soak 没出现 |
| 统计缓存陈旧 `da.ledger_cache_stale` | 同尺寸外部改写后 stats 仍是 50；跨进程追加打回全量扫描 30ms+；同尺寸+mtime 不变覆盖 quota | 指纹只含 size/mtime 0.70 | 对比 size/mtime 与内容哈希；看耗时 O(1) vs 全量 | restart_round, record | .90/.68/.50 | 中：账不准 ≠ 买不到 |
| 额度误判 `da.quota_false_block` | 出现 insufficient 但余额/预算实际充足；额度计数与台账 hit 数不一致 | 额度按缓存/重复计数 0.50 | quota 计数与台账 hit 逐条对账；抓余额快照 | reread, wait_retry | .80/.60/.45 | 高：当前没有额度快照，无法验证 |
| 日志轮转失效 `da.log_rotate_fail` | rotate 返回 False；current 未轮转却删了最老归档；只读归档删不掉仍计入 | 归档被占用/只读 0.75 | 对归档做 rename/delete 探针看 WinError；对比 log_bytes 与实际文件 | record, reduce_load | .90/.70/.50 | 中：日志丢不影响抢购，但会让后续诊断瞎眼 |

### 2.5 资源层（5）

| 现象 | 可观测信号 | 根因（先验） | 区分方法 | 建议动作 | 置信 | 误判风险 |
|---|---|---|---|---|---|---|
| 句柄增长 `re.handle_leak` | handles 216→298（60min）；同进程 GDI/USER 同步涨 | 截图/窗口句柄没释放 0.65 | 线性拟合斜率与 R²；掉头=尖峰不是泄漏 | restart_round, restart_worker | .85/.75/.55 | 中：启动预热也抬句柄，需 ≥10min 窗口 |
| 内存增长 `re.memory_growth` | rss_mb 单调涨；py_traced_mb 同步涨 | 缓存/列表无上限 0.60 | py_traced vs rss 增速；重启后是否归零 | restart_round, reduce_load | .85/.72/.50 | 中：45→55MB/小时在长跑里可能是正常缓存 |
| CPU 抬升 `re.cpu_growth` | cpu_pct 均值/峰值抬升；单轮 elapsed 变长 | 识别/截图负载过重 0.55 | 分进程 CPU；同窗口 OCR 失败与 queue_pending | reduce_load, restart_worker | .82/.70/.50 | 中：台架峰值 39.5%，阈值 60% 才不误报 |
| 卡死无心跳 `re.heartbeat_missing` | `cycles_stuck` 增长；queue_running≠0 且 cycles 长时间不增 | 卡点线程僵死 0.60 | 心跳间隔+完成数：完全不动=僵死；不要被 60s 静默骗 | restart_worker, restart_round | .85/.72/.55 | 中：GC/长识别会静默，需两段确认 |
| 队列过载 `re.overload` | queue_pending/running 抬升；elapsed 拉长；连点/补刷增多 | 并发+识别排队超能力 0.60 | 看 queue_pending 持续段长度；reduce 后 30s 观察 | reduce_load, wait_retry | .80/.70/.50 | 中：正常抢购瞬间队列=1，阈值要 ≥3 |

每个现象同时带 `differential`（同名根因在别处出现时只引用、不重复定义标签/动作），避免同一根因两套文案。

## 3. 规则表（35 条）

匹配器六类：`log`（正则+最小条数）、`cycles`（字段阈值+比率）、`metric`（max/mean/delta）、`slope`（趋势+最小跨度）、`ledger`（坏行/字节/命中率）、`artifact`（目录残留+mtime 窗口）、`absent`。多条 `match` 取**交集**，强度取最小。

| ID | 档 | 现象 | 关键信号/阈值 | 推出根因 |
|---|---|---|---|---|
| R-IN-01 | stat | 点击无效果 | cycles: `bought=false & hit_roll=true & clicks>=3`，rate≥0.2 | click_message_ignored 1.0 |
| R-IN-02 | hard | 抢不到前台 | log「抢不到前台」 | foreground_lost 1.0 |
| R-IN-03 | hard | UIPI | log「UIPI / 权限比本程序高」 | uipi_blocked 1.0 |
| R-IN-04 | hard | 注入器掉线 | log「injector unavailable」 | injector_unavailable 1.0 |
| R-IN-05 | hard | 窗口销毁 | log「已销毁=True / INPUT_ERROR」 | target_destroyed 1.0 |
| R-IN-06 | hard | 通道降级 | log「输入通道降级 / NO_TARGET_HWND / degraded=True」 | channel_degraded 1.0 |
| R-IN-07 | hard | 光标未动 | log「光标未动」 | cursor_not_moved 1.0 |
| R-IN-08 | hard | 点击无效果 | log `accepted=True...functional=unknown` 或「实际down=0」 | click_message_ignored 1.0 |
| R-IN-09 | hard | 点击无效果 | log「只改类名 / functional=unknown」 | click_message_ignored 1.0 |
| R-VI-01 | hard | OCR 超时 | log「OCR 失败：TimeoutError」 | ocr_timeout 1.0 |
| R-VI-02 | stat | 倒计时停跳 | log「没再跳」 | ocr_garbage 0.5 ≈ unknown_page 0.5（**刻意同分**） |
| R-VI-03 | hard | 界面不认识 | log「认不出/陌生页面/锚点失败」 | unknown_page 1.0 |
| R-VI-04 | hard | 价格格式 | log「price-parse」且「✗」（两条件同时满足） | price_parse 1.0 |
| R-VI-05 | hard | 模板分数低 | log「match_score / 模板分数」 | template_low 1.0 |
| R-TI-01 | corr | 倒计时漂移 | cycles `drift_ms>=200`，rate≥0.1 | countdown_drift 1.0（**弱假设，只加分**） |
| R-TI-02 | stat | 倒计时漂移 | metrics `drift_p95>=250` | countdown_drift 1.0（历史未触发，阈值待补） |
| R-TI-03 | hard | 时间源不可信 | log「NTP 不可信 / offset 超阈值 / DNS 伪造」 | time_source_untrusted 1.0 |
| R-TI-04 | stat | 卡点误差 | metrics `drift_p95>=150` | anchor_error 1.0（历史未触发） |
| R-DA-01 | hard | 台账残行 | log「残行 / UnicodeDecodeError / 没有换行符」 | ledger_residual 1.0 |
| R-DA-02 | stat | 台账残行 | ledger `bad_rows>=1` | ledger_residual 1.0 |
| R-DA-03 | hard | 台账写失败 | log「写台账失败 / add返回=None / write_ok=False」 | ledger_write_fail 1.0 |
| R-DA-04 | corr | 台账写失败 | metrics `ledger_bytes delta==0` 且 `cycles_ok delta>=1` | ledger_write_fail 1.0 |
| R-DA-05 | hard | 配置读写失败 | log「设置读不出来 / config.json.good 未更新 / .broken-」 | config_read_fail 1.0 |
| R-DA-06 | hard | 配置读写失败 | artifact `config.json.broken-*.bad`（mtime 在窗口内） | config_read_fail 1.0 |
| R-DA-07 | hard | 配置值越界 | log「OverflowError / int too large / 越界被拒」 | config_value_invalid 1.0 |
| R-DA-08 | hard | 日志轮转失效 | log「rotate 返回=False / 上限失效 / 只读删不掉」 | log_rotate_fail 1.0 |
| R-DA-09 | hard | 缓存陈旧 | log「stats 未变 / 同尺寸 / 71×」 | ledger_cache_stale 1.0 |
| R-DA-10 | corr | 额度误判 | log「余额不足 / 额度拦截 / insufficient」≥3 次 | quota_false_block 1.0（弱假设，当前无额度快照不可验证） |
| R-DA-11 | hard | 配置写被拒 | log「save抛出=PermissionError」 | config_write_denied 1.0 |
| R-RE-01 | stat | 句柄增长 | slope handles≥0.05/s，跨度≥120s，净增≥6 | handle_leak 1.0 |
| R-RE-02 | stat | 内存增长 | slope rss≥0.35/min，跨度≥180s，净增≥10MB | memory_growth 1.0 |
| R-RE-03 | stat | CPU 抬升 | metrics `cpu_pct mean>=60` | cpu_growth 1.0 |
| R-RE-04 | stat | 队列过载 | metrics `queue_pending>=3` | overload 1.0 |
| R-RE-05 | stat | 无心跳 | metrics `cycles_stuck>=1` | heartbeat_missing 1.0 |
| R-RE-06 | hard | 无心跳 | log「watchdog 未接线 / 卡死」 | heartbeat_missing 1.0 |

阈值来源不是拍脑袋：安静窗口实测 cpu 峰值 39.5%、queue 最大 1、handles 216→298/60min（0.023/s）、rss 45→54.85MB/60min（0.15/min），所以 CPU 线设 60、queue 设 3、句柄斜率设 0.05/s 并要求 ≥120s 跨度。

## 4. 推理流程与放弃条件

```
按 stage 排序求值
  hard：必现硬信号 —— 一条就能定性（错误码、异常类名、唯一告警）
  stat：统计信号 —— 速率/阈值/斜率，必须跟安静基线比
  corr：相关性 —— 台账不涨但成交在涨；只做加分，不单独结论
        ↓
  加权分 score = then权重 × 匹配强度 × rule.weight
  置信度 = 1 - exp(-1.5 × score)
  hard 下限 0.85；stat 上限 0.75；corr 上限 0.60
        ↓
  排序取前 5 → 查放弃条件 → verdict
```

为什么这个顺序：硬信号误报率最低，先让「错误码/异常类」把范围锁死；统计信号需要基线，容易被短窗口和噪声骗；相关性最弱，只配当第 2、3 名，不许单独定罪。

**放弃条件（任一命中即不硬给答案）**

| 条件 | verdict | 说明 |
|---|---|---|
| 窗口 <20s 且无硬信号，且最高分 <0.4 | `insufficient` | 统计量不足，不猜 |
| 最高置信度 <0.35 | `insufficient` | 低于结论线 |
| 前两名置信度差 <0.06 | `ambiguous` | 分不开，要求二次观测 |
| ≥2 个 hard 信号指向**互不相交**的根因集合 | `conflict` | 硬信号自相矛盾，转人工 |
| 没有任何可读数据 | `insufficient` | 列缺什么 |
| 有数据但无规则命中 | `normal` | 只能写「未见已知异常」，**不等于健康** |

另外两条防「永远有答案」的硬约束：

1. **同症状必须允许同分**：R-VI-02 把 OCR 垃圾与未知页面设成 0.5/0.5，宁可 ambiguous 也不硬选一个；
2. **弱证据不许升级**：R-TI-01 是 corr/0.25，单靠 drift 尖峰只能得到 0.31 分，低于 0.35，不出结论。

## 5. 实跑结果（历史数据）

回测命令：

```powershell
cd C:\Users\26346\Desktop\project\dafors
.\.venv\Scripts\python.exe -m evo.diagnose.bench
```

窗口切法：每个 `fault_start` 取 `[ts-3, ts+duration+12]`；安静窗口取 `fault_recovered+12 ~ +42`，并剔除与任何注入窗口重叠的时段；`fault_active`/`faults_*`/`[fault]` 全程不参与推理。

### 5.1 soak 窗口级（40 个注入窗口）

| 注入类型 | 次数 | 命中 | 误判 | 歧义 | 无据 | 盲区 |
|---|---|---|---|---|---|---|
| click_no_effect | 5 | 3 | 0 | 0 | 0 | 2 |
| ocr_timeout | 4 | 4 | 0 | 0 | 0 | 0 |
| config_corrupt | 4 | 4 | 0 | 0 | 0 | 0 |
| focus_lost | 4 | 4 | 0 | 0 | 0 | 0 |
| injector_down | 3 | 3 | 0 | 0 | 0 | 0 |
| ocr_garbage | 10 | 0 | 0 | 6 | 0 | 4 |
| unknown_page | 3 | 0 | 0 | 3 | 0 | 0 |
| disk_write_fail | 6 | 0 | 0 | 0 | 4 | 2 |
| clock_jump | 1 | 0 | 0 | 0 | 0 | 1 |
| **合计** | **40** | **18（45%）** | **0（0%）** | **9（22.5%）** | **4（10%）** | **9（22.5%）** |

- **误判率 0/40**：没有任何一次「给了结论但根因错」。
- 9 个歧义全部来自 OCR 冻结：ocr_garbage 6 + unknown_page 3，输出的是「OCR 垃圾 vs 未知页面 0.49 vs 0.49」。
- 9 个盲区是**注入没有产生可观测差异**（smoke5/6 的 click_no_effect 窗口仍 `hit_roll=false`；部分短 ocr_garbage 窗口没有打出「没再跳」）。
- 4 个无据是 disk_write_fail：最高分 0.31（弱时钟假设），按放弃条件转 insufficient，而不是硬猜。

安静窗口：30 个样本，误报 **0**（误报率 0%）。这 30 个窗口已剔除与注入重叠的时段，且 smoke/smoke4 因 events 只有 `fault_recovered`、没有 `fault_start`，无法切窗，不参与该统计。

### 5.2 行级（22 条真实台架异常）

**22/22 命中（100%）**，全部给到正确根因，并带正确行号。覆盖 8 类：

| 类别 | 用例 | 结果 |
|---|---|---|
| 输入 | I2 message 被受理但不处理；I4 只认类名；I5 目标销毁；I6 无句柄降级；I7 UIPI | 5/5 |
| 视觉 | N11 价格 `'15 252'→252` | 1/1 |
| 时序/时间源 | N2 零时间戳；N5 单源偏 11h；N7 DNS 污染 | 3/3 |
| 数据-配置 | N9 10^400；N12 超大 int；P3 ACL 拒写 | 3/3 |
| 数据-台账 | P4 只读台账；P5 半 UTF-8 残行；P6 完整行无换行；P11 同尺寸外部改写 | 4/4 |
| 数据-日志 | S1 归档被锁；S2 上限失效；S3 只读删不掉 | 3/3 |
| 资源 | S9 watchdog 未接线 | 1/1 |
| 缓存 | S4 同尺寸 stats 不变；S6 跨进程 71× | 2/2 |

### 5.3 一次完整输出的样子

```powershell
.\.venv\Scripts\python.exe -m evo.diagnose.cli ^
  --log _soak_data\baseline\soak.log ^
  --cycles _soak_data\baseline\cycles.jsonl ^
  --metrics _soak_data\baseline\metrics.csv ^
  --artifact-dir _soak_data\baseline ^
  --since "2026-11-07 05:40:20" --until "2026-11-07 05:40:50"
```

真实输出（节选）：

```
结论：ambiguous —— 分不开（需二次观测）
根因排序：
  #1 裁剪/尺度漂移，读到了错误区域    置信度 0.49（stat） 场景 ocr_fail
     建议动作：reread_crop, reread_scale, alt_ocr
     证据：_soak_data/baseline/soak.log:623 ... 显示值 8 秒已经 2.5 秒没再跳
  #2 弹窗/活动页/断线页覆盖了原页面    置信度 0.49（stat） 场景 unexpected_page
     建议动作：close_popup, esc, tab_reenter, nav_home
盲区 / 缺什么：
  - 裁剪/尺度漂移 与 弹窗/活动页覆盖 只差 0.00：现有信号分不开，需要二次观测
```

点击无效果的窗口则给出：

```
结论：conclusive —— 有结论
#1 消息被窗口受理但游戏逻辑不处理（I2/I4）  置信度 0.75（stat）  场景 click_no_effect
   建议动作：alt_template, reread_crop, ensure_foreground
```

## 6. 判定不了 / 缺什么

这些不是「规则没写」，是**现有数据里没有区分信号**。都已在推理器的 `missing` 里显式输出。

| 类别 | 现状 | 缺什么 |
|---|---|---|
| clock_jump（soak 1 例） | 窗口内无任何日志/遥测差异。`drift_p95_ms` 是**滚动累计值**，全程 45→108 缓慢上升，窗口内只 +2.24ms；单条 `drift_ms` 尖峰（655/964ms）全时段都有，且在磁盘停顿窗口同样出现 | 需要「系统时间 vs 单调时钟偏移量」或 NTP offset 采样，才能把时钟跳变从调度延迟里分开 |
| disk_write_fail（6 例） | 日志无「写失败」，cycles 正常成交，`ledger_bytes` 每窗都在增长；`data/records.jsonl` 真源不存在 | 需要「台账写入返回值/失败计数」或「目录可写探针」，否则写失败完全隐形 |
| ocr_garbage vs unknown_page（13 例） | 两者打**同一句**「显示值 X 秒已经 2.5 秒没再跳」，cycles 都是 `state=abort clicks=0 drift=null` | 需要页面锚点模板分数（match score）或帧差标志；没有就只能 ambiguous |
| template_low | 历史日志从不打印模板分数 | 需要 `match_score` 字段 |
| price_parse | 只落解析结果，不落 OCR 原始串 | 需要原始串，才能分「解析器错」还是「识别错」 |
| quota_false_block | 没有额度/余额快照 | 需要 quota 计数与余额字段，才能和台账 hit 对账 |
| click_no_effect 的 2 个盲区 | smoke5/6 注入后仍是普通漏抢（`hit_roll=false`），症状未出现 | 不是诊断问题，是注入未生效；另需 `down` 计数才能区分「消息没发出去」和「发了没人理」 |
| config_corrupt | cycles 的 `config_ok` 恒 True，只能靠 `.bad` 残留 + mtime | 若清理了 `.bad`，就失去唯一物证；需要配置校验失败计数 |
| 资源类（句柄/内存） | 规则要求 ≥120/180s 跨度 | 20~40s 注入窗口永远算不出趋势，只能在长跑时间线上验证 |
| smoke / smoke4 | events 只有 `fault_recovered`，没有 `fault_start` | 无法切真实注入窗口；参与窗口级统计会把后续故障误算成「安静」 |

## 7. 用法与加规则

离线诊断一段历史日志：

```powershell
.\.venv\Scripts\python.exe -m evo.diagnose.cli `
  --log _soak_data\baseline\soak.log `
  --cycles _soak_data\baseline\cycles.jsonl `
  --metrics _soak_data\baseline\metrics.csv `
  --ledger _soak_data\baseline\data\records.jsonl `
  --artifact-dir _soak_data\baseline `
  --since "2026-11-07 05:40:20" --until "2026-11-07 05:40:50" --json
```

- `--json` 输出完整结构（根因、证据、缺什么），便于被规划器消费；
- 退出码：0 有结论/正常，1 证据不足/歧义/冲突，2 规则自检失败；
- 规则自检会校验：`phenomenon` 存在、`then` 的根因在现象库有定义、场景在 `scene_guard.py` 存在、动作在 `recovery.py` 存在。

加一条规则 = 只改 JSON：

```json
{
  "id": "R-XX-01",
  "phenomenon": "vi.ocr_timeout",
  "stage": "hard",
  "weight": 1.0,
  "match": [{"kind": "log", "pattern": "新错误码 ABC-123", "min_count": 1}],
  "then": {"c.vision.ocr_timeout": 1.0},
  "note": "新错误码唯一"
}
```

不需要写 Python；匹配器不够用时才在 `rules.py` 加一种 `kind`。

## 8. 结论

- 现象库：28 个现象覆盖输入/视觉/时序/数据/资源五层，每个都给了信号、根因排序、区分方法、现有动作、三档置信度和误判风险。
- 规则表：35 条声明式规则，hard/stat/corr 三档，阈值用真实安静窗口校准。
- 推理器：可喂任意时间段的历史日志，输出根因排序 + 证据文件:行号 + 缺什么；判不出来会明确 `ambiguous/conflict/insufficient`，且 `normal` 只表示「未见已知异常」。
- 实跑：soak 40 个注入窗口命中 18、误判 0、歧义 9、无据 4、盲区 9；安静窗口 30 个误报 0；22 条真实台架行 22/22 命中。
- 已证实的 5 类可诊断异常：`click_no_effect`、`ocr_timeout`、`config_corrupt`、`focus_lost`、`injector_down`。
- 已明确标出的 4 类数据盲区：`clock_jump`、`disk_write_fail`、`ocr_garbage/unknown_page` 二选一、`template_low/quota/price_parse` 缺字段。
