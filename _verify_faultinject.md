# dafors 四批修复 · 独立复核（故障注入派）

复核时间：2026-11-07　复核者：只验证、不改代码、不 commit、不 push
被测代码：`C:\Users\26346\Desktop\project\dafors\core`（工作副本另存于 `_fi/base`，`ROOT` 指向副本，真实 `config.json` / `data/` 全程未被读写）

## 0. 基线与被测版本

**被测快照 = `_fi\base\core`（工程 `core` 的副本，`ROOT` 指向副本）。下表哈希即结论所对应的代码（07:39 同步）。**

| 文件 | MD5(前 12) | 文件 | MD5(前 12) |
|---|---|---|---|
| config.py | 787375268240 | watchdog.py | B4032F14DC5F |
| records.py | B2F6B4E88955 | input.py | 6CF1D56E0E3E |
| timesync.py | F4635931C412 | nocursor.py | B4F5543E271A |
| switches.py | E81C02644AD1 | scheduler.py | AD8C1E8FDEB1 |
| ocr_chain.py | 02F44BB4FE1F | engine.py | 042AF3C8424D |
| retention.py | CC1D34363974 | | |

### 0.1 版本漂移（重要）

复核期间工程目录一直在被另一侧修改。共取过两个快照，各自跑 2 轮：

| 快照 | config.py | switches.py | input.py | engine.py | 结果 |
|---|---|---|---|---|---|
| A（07:33） | 0B878983DDFA | 02664F6CC6A3 | A3B2997DA105 | 07573850E132 | 18 FAIL |
| B（07:39，本报告基准） | 787375268240 | E81C02644AD1 | 6CF1D56E0E3E | 042AF3C8424D | **同样 18 FAIL，逐项一致** |

即在 07:35–07:39 期间对 config/switches/input 的增量改动**没有改变任何一条判定**。此后 07:40:01 工程 `engine.py` 又变成 `02B03BA9887B`（2770 行），但没有任何用例直接驱动 engine，故不影响结论。
若还要继续改，改完把 `_fi/base/core` 重新同步再跑第 9 节命令即可刷新。

复现脚本（全部在临时目录，`_fi/base` 为工程 `core` 的只读副本）：

```
C:\Users\26346\Documents\Trae-CN\temp_20261107_50\_fi\
  child.py        子进程：save_loop / save_slow_fsync / lock_hold / tx_counter / tx_threads / records_*
  fi_persist.py   批次1（P1–P15）
  fi_numeric.py   批次2（N1–N12）
  fi_input.py     批次3（I1–I9）
  fi_stability.py 批次4（S1–S9）
  fi_extra.py     补充注入（E1–E9b）
  out\*.txt       原始输出（每套在同步后的代码上跑了 2 轮，逐字一致）
```

运行方式：`$env:PYTHONPATH="...\_fi\base"; $env:FI_BASE="...\_fi\base"; python _fi\fi_xxx.py`

## 1. 结论速览

| 套件 | 用例 | PASS | FAIL | WARN | INFO |
|---|---|---|---|---|---|
| 批次1 持久化 | 15 | 12 | 2 | 1 | 0 |
| 批次2 数值与时间 | 12 | 6 | 6 | 0 | 0 |
| 批次3 输入通道 | 9 | 7 | 2 | 0 | 0 |
| 批次4 稳定性 | 9 | 2 | 5 | 0 | 2 |
| 补充注入 | 12 | 8 | 3 | 0 | 1 |
| **合计** | **57** | **33** | **18** | **1** | **3** |

**被攻破 18 项**，其中 3 项会直接丢用户数据（P11、P6、S1），5 项会让线程/接口崩掉或不可用（P5、N9、N12、E7 的两条），其余为静默错值或资源上限失效。

---

## 2. 批次1：持久化

| # | 攻击手段 | 注入位置 | 期望（修复后应守住） | 实测（同步后代码，2 轮一致） | 判定 |
|---|---|---|---|---|---|
| P1 | 连续事务中随机 0.12–0.55s 强杀，×12 | `_save_locked` 写 tmp/fsync/replace 全窗口 | 主文件与 `.good` 始终可解析；后续事务不卡 | 12/12 后续事务 10–20ms、`problems=[]`；孤儿 tmp 0–1 个（下次 load 清） | PASS |
| P2 | 持锁子进程 `lock_hold` 30s 中强杀 | `file_lock` 持锁期 | 不得死锁、锁必须释放 | 等锁 14–18ms（<15s 超时） | PASS |
| P3 | `icacls /deny USER:(W)` 真实拒绝写（非 mock） | `_write_payload` open(tmp) | 抛 OSError，原文件不变，绝不能出 0 字节 | `PermissionError`，原文件字节一致，0 字节=False | PASS |
| P4 | `attrib +R` 只读台账 | `records.add` 写路径 | 失败可见（返回 None + last_error + write_ok） | `add→None`，`last_error=PermissionError`，`write_ok=False`，并打印"这一行没有落盘" | PASS |
| **P5** | 台账尾部写半个 UTF-8 字符（真实截断字节） | `records._iter_rows` 文本模式 `for line in f` | 读路径跳过残行并自愈，不得抛异常 | `stats()` / `load(0)` 均抛 `UnicodeDecodeError`；再读一次仍抛（读路径不自愈） | **FAIL** |
| P6 | 完整 JSON 行但缺行尾 `\n` | `_repair_tail` | 应识别为"完整行"或至少不丢数据 | 该行被当残行截掉（removed=82），`加前=1 加后=1`，现存只剩 next | WARN |
| P7 | 8 进程 × 30 次 `+1` 事务 | `transaction` + 跨进程锁 | 计数无丢更新 | 期望 241，实际 241.0 | PASS |
| P8 | 8 进程 × 100 次并发追加台账 | `records.add` + 文件锁 | 行数精确、无坏行 | 800 行、坏行 0 | PASS |
| P9 | 清空 vs 追加并发 | `clear` 与 `add` | 无半行、无坏行 | 行数 54–80，坏行 0 | PASS |
| P10 | 追加写入中强杀 ×6 | `_repair_tail` → append | 下次启动自愈、只丢被杀那一条 | 6/6 `rows+1`、`recov=1` | PASS |
| **P11** | 外部同尺寸改写 + 保留 mtime（`copy /b` 或备份还原都会这样） | `_disk_sig` = `(mtime_ns, size)` | 检测到外部改动并合并 | 指纹相同 → 跳过合并，`quota 5 → 1`，外部改动被静默覆盖 | **FAIL** |
| P12 | 旧配置 `mode=list` 迁移 | `_normalize` | workflow=list、不再残留 countdown | `workflow=list mode=list task=('list','list')` | PASS |
| P13 | 预先堆积 120s 前的孤儿 tmp | `_cleanup_orphan_tmp` | 只清旧的、不误删活跃写 | 清理 1 个 | PASS |
| P14 | `.good` 只读（`attrib +R`） | `_write_backup` | 主文件照常更新，`.good` 保持上一份可用 | 主文件已更新、`.good` 未变、无异常抛出，打印"保留上一份可用备份" | PASS |
| P15 | 同进程 8 线程 save/load 压测 | `_save_lock` + `_proc_lock` | 无坏文件、无锁泄漏 | `problems=[] errs=[] tmp_orphans=0` | PASS |

关键输出：

```
P5: stats() 抛 UnicodeDecodeError: 'utf-8' codec can't decode ... unexpected end of data ; load(0) 抛同错 | 读后仍失败
P11: 指纹(同尺寸+mtime不变)=True 外部 quota=5 保存后盘上 quota=1（期望 5）
P6 : 加前=1 加后=1 现存=['next'] 修复记录 removed=82
```

## 3. 批次2：数值与时间

| # | 攻击手段 | 注入位置 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| N1 | 诚实 NTP 回包（48B、mode4、stratum1、origin 回显） | `_query` | 正常采纳 0 偏移 | offset=0.000，ok=True | PASS |
| N2 | 全零 48B 报文 | origin/t2/t3 校验 | 拒绝 | ok=False，err=应答不可信 | PASS |
| N3 | origin 时间戳错配（+1s） | origin 回显 | 拒绝 | ok=False | PASS |
| N4 | 偏移 +90000s（>±86400） | `MAX_OFFSET_S` | 拒绝 | ok=False | PASS |
| **N5** | 单源 4 个样本一致偏 +40000s（11.1h） | `sync` 中位数/离群 | 拒绝或降级为"不可信" | ok=True，采纳 offset=40000 | **FAIL** |
| **N6** | 同上但 −40000s | 同上 | 拒绝 | ok=True，offset=-40000 | **FAIL** |
| **N7** | 3 个不同主机名全解析到同一伪造源（DNS 投毒） | `servers` 循环 | 识别为同源或要求多源一致 | 有效样本 13、ok=True、offset=40000 | **FAIL** |
| N8 | 22 个开关逐个打上下界外值 | `switches.set` | 全部拒绝 | 22/22 拒绝 | PASS |
| **N9** | 手改 config 为 `10**400` 整数 | `_number` 的 `float(value)` | 拒绝或归回默认 | `get()`/`snapshot()` 抛 **OverflowError**（未捕获） | **FAIL** |
| N10 | NaN / Inf / "1e400" / "abc" / True | 同上 | 脏值拒绝 | 全部拒绝（True 被当 1，可接受） | PASS |
| **N11** | 无 `¥` 的价格 `"15 252"`、`"1 234 567"`；跨行 `"¥12 345\n678"` | `_CURRENCY_AMOUNT` / 逗号分词 | 空格千分位不拆、不吞下一行 | `252`（缩 60 倍）、`567`（缩 2178 倍）、`12345678`（吞了下一行 678） | **FAIL** |
| **N12** | 文件本身就是 `{"click_hz": 10^400}` | `json.loads` → 直接进 `snapshot` | 同上 | `snapshot()` 抛 OverflowError | **FAIL** |
| E5 | 休眠唤醒（单调钟 +8h）+ 壁钟回拨 −10s（模块时钟注入） | `timesync.time` | offset 必须归 0 并标 stale | 唤醒后 offset=0 stale=True(已过期)；回拨后 offset=0 stale=True(系统时钟被回拨) | PASS |
| E6 | 7 种畸形 NTP：8B / 47B / mode3 / stratum0 / stratum16 / t3 早于 t2 / 往返 6s | `_query` 长度/mode/stratum/delay | 全部拒绝 | 7/7 拒绝 | PASS |

关键输出：

```
N5: 同一源 4 样本一致偏 +40000s → ok=True 采纳 offset=40000（±86400 只挡 >24h）
N7: 3 个主机名全指向同一伪造源 → 有效样本=13 ok=True offset=40000
N9/N12: get()/snapshot() 抛 OverflowError: int too large to convert to float
N11: '15 252'→252(期望15252)✗ ; '1 234 567'→567(期望1234567)✗ ; '¥12 345\n678'→12345678(期望12345)✗
```

> N5/N6/N7 的根因是 `MAX_DEVIAION_S=60` 只能剔除"与中位数偏离 >60s"的样本；所有样本一起偏的时候中位数本身就错了，`MIN_SAMPLES=3` 也挡不住同源复制。

## 4. 批次3：输入通道

| # | 攻击手段 | 注入位置 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| I1 | 隐藏窗口正常收 `WM_*` | `nocursor.Clicker._post_click` | move/down/up 到齐、真实光标不动 | move=1 down=1 up=1，光标未动，effective=message | PASS |
| **I2** | 窗口**受理消息但不处理**（真实"点了没反应"） | `window_responsive`(WM_NULL) | 必须暴露"未生效"或标 functional=unknown 并告警/降级 | `click=True accepted=True`，实际 down=0，`functional=unknown`，上层会当成功 | **FAIL** |
| I3 | 类名 `UnrealWindow` + 默认 strict | `probe_message` 启发式 | 拦截 | `InputChannelError code=MESSAGE_RAW_CURSOR_UNSUPPORTED` | PASS |
| **I4** | 同样是读真实光标的窗口，**只换类名** | 同上（仅靠类名字符串） | 应识别"读光标"这一行为而不是名字 | `effective=message functional=unknown`，判定被绕过 | **FAIL** |
| I5 | 目标窗口已销毁（`WM_CLOSE` 自毁） | `PostMessage` 前 | 报错不假成功 | `InputChannelError code=INPUT_ERROR` | PASS |
| I6 | 无目标句柄降级 | `channel_status` | 降级可见（degraded + reason_code） | `effective=legacy reason=NO_TARGET_HWND degraded=True events=2` | PASS |
| I7 | 高完整性子进程（UIPI） | `_gate_integrity` | 主动拦截 | 高完整性→`UIPI_BLOCKED`；同/低→放行 legacy；读不到→放行 unknown | PASS |
| I8 | 完整性读不到（unknown） | 同上 | 至少不静默 | 放行 message，`integrity={'ours':None,'target':None}` 有记录 | PASS（但属放宽放行） |
| I9 | strict=false + UnrealWindow | 记账 | 不拦但必须留痕 | `effective=message`、reason 有码、账本 6 条 | PASS |

关键输出：

```
I2: 窗口收到消息但不处理：click=True 实际down=0 accepted=True functional=unknown effective=message
I4: 同样是读真实光标的窗口，只改类名 → effective=message functional=unknown（判定只认类名字符串）
```

## 5. 批次4：稳定性 / 资源上限

| # | 攻击手段 | 注入位置 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| **S1** | 轮转时 `.1` 被读句柄占用（杀毒/索引器常态） | `retention.rotate` 先删 `.2` | 失败要回滚，不能先毁最老归档 | `rotate=False/False`，`.2` 两次都被删（current 未轮转） | **FAIL** |
| **S2** | `.2` 被占用 | 同上 | 声明的上限必须生效或至少告警 | 不轮转、current 仍 9000 字节（8MB 上限照旧失效），无任何日志 | **FAIL** |
| **S3** | 500 个截图，前 450 个只读 | `prune_dir` 删除失败仍扣减 | 上限 400 必须收敛 | 删除函数返回 0，实际剩 500 个 | **FAIL** |
| **S4** | 外部同尺寸改写 `hit→bad`，mtime 不变 | `records._signature` = `(path,size)` | 缓存失效重扫 | stats 仍 50，不更新 | **FAIL** |
| S5 | 同进程 60 次 add+stats | 增量缓存 | O(1) | 后 40 次最大 0.43–0.60ms / 平均 0.33ms | PASS |
| S6 | 另一进程追加 1 行后再 stats | 跨进程签名 | 只要求正确，可退全量 | 0.33→29~66ms（87–129×），结论正确 | INFO |
| **S7** | 三份归档 + `load(0)` 全量 | `_load_impl(limit=0)` | 与 `limit>0` 一致：新的在前 | 返回 `[oldest2, oldest1, older2, older1, new2, new1]`（最旧在前） | **FAIL** |
| S8 | 心跳停跳 + 防抖 + 冷却 | `Watchdog.poll` | 3 次确认才触发、冷却内不重复 | `[F,F,T,F]`，冷却后 `[F,T,F]`（前一次已累计 1 strike），触发后归零 | PASS |
| S9 | 检查调度器是否真的用 `core/watchdog.py` | `scheduler.py` | 应引用模块 | **未引用**；实际是内联 `WATCHDOG_STALL_S=60/MAX_RESTARTS=2` | INFO（声称的"看门狗"未接线） |

关键输出：

```
S1: rotate 返回=False/False，.2 被删=True/True（current 未轮转，却已经删掉最老归档）
S3: 上限 400，删函数返回 0，实际剩 500 个
S4: 同尺寸外部改写后 stats 未变：50 → 50（缓存指纹只认 size）
S7: load(0) 顺序=['oldest2','oldest1','older2','older1','new2','new1']
S9: scheduler.py 引用 core/watchdog.py=False
```

## 6. 补充注入（覆盖任务点名的其余手段）

| # | 攻击手段 | 注入位置 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| **E1** | 用户用记事本/PowerShell5.1 另存 UTF-8 **带 BOM** 的 config | `open(CONFIG_PATH, encoding="utf-8")` | 视为合法配置（或用 `utf-8-sig`） | JSONDecodeError→隔离→**用旧 `.good` 覆盖用户改动**：`threshold 0.55→0.85`，原文件留成 `.broken-*.bad` | **FAIL** |
| **E1b** | 同上，另存 UTF-16（记事本"Unicode"） | 同上 | 同上 | 同样重置为 0.85 | **FAIL** |
| E2 | 把 fsync 窗口人为拉长到 120ms，在窗口内强杀 ×3（真实 fsync 仍执行） | `_write_payload` 的 `os.fsync` 调用点 | 主文件/`.good` 可解析、锁可再获取 | 3/3 无坏文件；后续事务 0–16ms；无锁错误 | PASS |
| E3 | 4 进程 × 4 线程 × 20 次事务（进程内锁+跨进程锁叠加） | `transaction` | 计数精确 | 期望 321，实际 321.0 | PASS |
| E4 | 300 次取锁，其中 1/3 在锁内抛异常 | `file_lock` 的 finally | 句柄不泄漏、锁可重入 | 句柄 0→0（+0），随后取锁 0–15ms | PASS |
| E5 | 休眠唤醒 / 壁钟回拨（模块时钟注入） | `timesync.time` | 旧 offset 必须失效 | 见批次2，PASS | PASS |
| **E7** | `add(name="\ud800…")`；`add(price=10**400)`；`price=NaN/Inf` | `records.add` 的 `float()` 与 `f.write` | 脏值不得打断引擎 | 孤代理抛 **UnicodeEncodeError**、超巨整数抛 **OverflowError**（均未捕获）；NaN/Inf 直接写进台账，`stats.spent=nan`，再序列化成 `"spent": NaN` | **FAIL** |
| E7b | 1MB 的 name | 台账追加 | 可写可读 | 写入/回读均成功 | PASS |
| E8 | 配置尾部截断在多字节字符中间 | `_load_locked` 的 ValueError 分支 | 隔离坏文件并从 `.good` 恢复，不丢数据 | 隔离 1 份，`threshold=0.85 note='用户备注'` 全部恢复 | PASS |
| E9 | 300 个 tmp（20 个 120s 前 + 280 个刚写） | `_cleanup_orphan_tmp` | 只清旧的，不误删活跃写 | 清理 20，剩 280 | PASS |
| E9b | 用 `SetFileInformationByHandle(FileAllocationInfo)` 预分配 200GB（卷剩 ~106GB） | 内核 | 返回真实 ENOSPC | Win32 错误码 **112 = ERROR_DISK_FULL** | INFO（环境能力确认） |

关键输出：

```
E1 : 带 BOM 的手改配置(3223 字节) → threshold 读回=0.85（期望 0.55），隔离 1 份，notice='从备份恢复'
E7 : add 异常=["lone-surrogate → UnicodeEncodeError", "price=10**400 → OverflowError"]
     台账出现 NaN/Infinity=True | stats.spent=nan | 该统计再序列化={"hits":2,...,"spent": NaN,...}
E9b: 预分配 200GB → 错误码=112
```

---

## 7. 汇总：被攻破的项

严重度口径：**丢数据** = 用户数据/配置被覆盖或删除；**卡死** = 线程/接口抛未捕获异常或资源上限失控；**静默错** = 返回值看似正常但结果错。
"复现 4/4" = 两个代码快照（A/B）× 每快照 2 轮，四次结果逐字一致。

| 编号 | 被攻破的表现 | 严重度 | 复现 | 会不会丢数据 | 会不会卡死 | 会不会静默出错 |
|---|---|---|---|---|---|---|
| P11 | 同尺寸+同 mtime 的外部改配置被整份覆盖 | **高** | 4/4 | 是（配置改动） | 否 | 是 |
| P5 | 半 UTF-8 尾行让 `stats/load` 抛 UnicodeDecodeError | **高** | 4/4 | 否（写路径会自愈） | 是（接口 500、统计不可用） | 否（有异常） |
| P6 | 无换行的完整行被当残行截掉 | 中 | 4/4 | 是（1 条台账） | 否 | 是 |
| N5/N6 | 单源一致偏 11h 被采纳 | **高** | 4/4 | 否 | 否 | 是（倒计时整体偏 11h） |
| N7 | DNS 投毒（3 名同源）被采纳 | **高** | 4/4 | 否 | 否 | 是 |
| N9/N12 | 超大整数让 `get/snapshot` 抛 OverflowError | 中 | 4/4 | 否 | 是（引擎/接口线程崩） | 否 |
| N11 | 无 `¥` 时价格缩 60–2178 倍；跨行吞下一行 | **高** | 4/4 | 否 | 否 | 是（预算闸门误判） |
| I2 | 受理但未生效的点击返回成功 | **高** | 4/4 | 否 | 否 | 是（假成交） |
| I4 | 只认类名，换名即绕过 | 中 | 4/4 | 否 | 否 | 是（点击无效） |
| S1 | 轮转先删 `.2` 再失败 | 中 | 4/4 | 是（最老一份归档） | 否 | 是 |
| S2 | 归档被占用 → 上限永不生效 | 中 | 4/4 | 否 | 长跑撑满磁盘 | 是 |
| S3 | 只读截图删不掉但仍扣减 → 上限失效 | 中 | 4/4 | 否 | 长跑撑满磁盘 | 是 |
| S4 | 同尺寸外部改写后统计不动 | 低 | 4/4 | 否 | 否 | 是 |
| S7 | `load(0)` 返回最旧在前 | 低 | 4/4 | 否 | 否 | 是（顺序错） |
| E1/E1b | BOM/UTF-16 配置被当坏文件，用户改动被旧备份覆盖 | 中 | 4/4 | 是（配置改动，原文件留 `.bad` 可捞） | 否 | 是 |
| E7 | 孤代理/超大整数抛未捕获异常；NaN 落盘污染统计 | 中 | 4/4 | 否 | 是（引擎线程崩） | 是（NaN 让前端 JSON.parse 失败、预算比较恒 False） |

> S9 单独记一笔：`core/watchdog.py` 本身行为正确（S8 PASS），但 `scheduler.py` 并没有引用它，实际卡死判定是内联的 `WATCHDOG_STALL_S=60 / MAX_RESTARTS=2`。若这批"看门狗修复"指的是那个模块，它处于**未接线**状态。

## 8. 未被本次覆盖的手段（原因）

| 手段 | 状态 | 原因 / 替代 |
|---|---|---|
| 真实小容量卷（VHD/内存盘）制造 ENOSPC / 只读卷 | 未做 | 当前会话非管理员，`Mount-DiskImage`/`New-VHD`/imdisk 都要管理员或驱动；任务禁止装驱动。改用 **真实 ACL 拒绝(P3)**、**只读属性(P4/P14)**、**只读句柄(S1/S2)**，并用 `FileAllocationInfo` 触发真实 **112 ERROR_DISK_FULL (E9b)** 证明内核错误码链路一致 |
| 真实休眠/唤醒、改系统时间 | 未做 | 需要管理员或电源操作；用模块时钟注入（E5）+ 单调钟推进等价覆盖 |
| 蓝屏/断电级掉电 | 未做 | 无法安全制造；P1/E2 的强杀已覆盖 tmp/fsync/replace 三个窗口 |
| 句柄/线程总量打满 | 部分 | E4 覆盖锁句柄泄漏（+0）；未做线程池耗尽（会波及宿主系统） |

## 9. 复现命令

```powershell
$fi = "C:\Users\26346\Documents\Trae-CN\temp_20261107_50\_fi"
$src = "C:\Users\26346\Desktop\project\dafors\core"
# 0) 重新建立被测副本（报告完成后已按"清理临时产物"删掉 base，只剩脚本与日志）
Remove-Item -Recurse -Force "$fi\base" -ErrorAction SilentlyContinue
Copy-Item -Recurse -Path $src -Destination "$fi\base\core"
$env:PYTHONPATH = "$fi\base"; $env:FI_BASE = "$fi\base"
# 1) 五套注入
python "$fi\fi_persist.py"    # 批次1
python "$fi\fi_numeric.py"    # 批次2
python "$fi\fi_input.py"      # 批次3
python "$fi\fi_stability.py"  # 批次4
python "$fi\fi_extra.py"      # 补充注入
```

被测路径在运行时恒为 `..._fi\base\config.json` 与 `..._fi\base\data\records.jsonl`（日志可核对），真实 `C:\Users\26346\Desktop\project\dafors\config.json`、`data\` 未被打开写入；未弹出可见窗口、未移动光标、未安装任何驱动、未 commit。

**清理记录**：临时副本 `_fi\base`（33 MB，含测试台账与 1MB name 样本）、`_fi\out\work`、`__pycache__` 已删除；保留 6 个注入脚本与 30 份原始输出（合计 129 KB）用于复现与取证。P3 的 `icacls /deny` 全程在 `finally` 里 `/reset`，复核结束时 `_fi` 下无 DENY 条目；未残留任何 `child.py` 进程。

## 10. 一句话结论

四批修复中，**持久化的主路径（原子写、锁、强杀恢复）确实守住了**；但**"外部改动检测""读路径自愈""轮转顺序与上限""NTP 多源校验""价格千分位""点击生效判定""脏值入口"仍然可以被故障注入打穿**，其中 P11 / N5-N7 / N11 / I2 会直接造成错买、漏买或配置丢失，建议按上表优先级继续修。
