# _nonreal_trust — 信任链与数据通路实测

- 日期：2026-11-07
- 仓库：`C:\Users\26346\Desktop\project\dafors`
- 解释器：仓库 `.venv\Scripts\python.exe`
- 运行方式：全部在 `C:\Users\26346\Documents\Trae-CN\temp_20261107_84\_trust_run\` 下的临时目录；真实文件、真实子进程、真实 TCP/UDP 端口（`bind(0)` 随机端口）。
- 隔离：未读写仓库 `data/`（`git status --short data` 无输出）；未 commit、未 push、未开可见窗口。
- 许可签发使用临时测试私钥，公钥仅在测试子进程启动脚本里替换；验签/指纹/时间/签名长度等判定逻辑均为仓库原代码。

## 判定总表

| # | 项目 | 判定 | 关键数字 |
|---|---|---|---|
| 1 | 授权链 | PASS | 激活 fp=1.0；换 MAC fp=0.95 通过；换 BIOS fp=0.70 拒绝；篡改/截断/DER/短公钥全部拒绝；过期 7.0 天；吊销 403 |
| 2 | 离线与时钟 | PASS | 回拨墙钟 5 天仍按单调时钟累计离线 1,728,000s（20 天），过期 7 天；租期 20 天 > 7+3 天 → need_online；账本删改 → need_online |
| 3 | 遥测通路 | **PASS（已修复，含第二/三轮边界修复）** | 首传 6 事件/2 命中；重复批 inserted=0/duplicates=6；篡改 401/403/403/403；断网缓冲 3 条（修复前 9 条）；恢复后缓冲 0、服务端 9 条无膨胀；长末行 8192/1MB 空闲 0 行（修复前 6 行）；轮转 `.1` 5 条补齐（修复前丢 5）；替换保旧尾行 6 行（修复前 0）；改头不改尾 200 行（修复前 0）；归档断代出 1 条 gap（修复前静默丢 3）；陈旧 `.1` 重复 6→0；idle 1 万/10 万行 0.139/0.161ms、单轮 stat=1；漂移一次性 10 万行 155–197ms |
| 4 | 台账解析 | PASS | 4 行有效；hits=3 tries=4 spent=150 rate=75%；坏 UTF-8/残行跳过；44 字节残行自动修复 |
| 5 | 时间同步 | PASS | 多源 offset=5.0s indep=3；单源 offset=5.000529s degraded=true；11 小时 39,600s 拒绝；三域名同 IP（9 样本/独立 1）拒绝；断网后 offset 保持 5.0s |
| 6 | 保留与轮转 | PASS | 三种故障下哈希全保留；只读清理 removed=2 left=3；只读台账写入显式失败且原 2 行完好 |

---

## 1 授权链

真实 HTTP：`tools/license_server.py` 子进程（随机端口）+ 临时 `license.db` + `license_admin.py gen-codes` 发卡；客户端走 `core/license.activate()`，许可落在临时 `SZJ_LICENSE_DIR`。

| 用例 | 结果 | 证据 |
|---|---|---|
| 签发→激活→验签 | PASS | `status=ok`，`fp_score=1.0`，`tier=pro` |
| 改签名一字节 | PASS | `sig_invalid`，`签名校验失败（许可被改过或公钥不匹配）` |
| 改 payload 一字节 | PASS | `sig_invalid`，同上 |
| 截断 base64 签名 | PASS | `公钥/签名格式错误：签名不是合法 base64：Incorrect padding` |
| 32 字节签名 | PASS | `签名长度应为 64 字节（r||s），实际 32` |
| DER 编码签名（71 字节） | PASS | `签名长度应为 64 字节（r||s），实际 71` |
| 32 字节公钥 | PASS | `公钥长度应为 64 字节（X||Y），实际 32` |
| 换 BIOS UUID（权重 30） | PASS（拒绝） | `fp_mismatch`，`fp_score=0.70`，`硬件指纹匹配 70%（阈值 85%）` |
| 换 MAC（权重 5，同卡换机阈值内） | PASS（通过） | `status=ok`，**实际加权分数 0.95**，命中 5 项（仅 MAC 变） |
| 过期 | PASS（拒绝） | `expired`，`许可已过期 7.0 天` |
| 吊销（本地列表） | PASS（拒绝） | `revoked`，`许可 lic-83472106720d4081 已被吊销` |
| 吊销（联网 refresh） | PASS（拒绝） | HTTP 403 `{"error": "许可已吊销"}` |

加权分数口径：6 个组件权重合计 100；少 1 个 MAC（5）→ 95/100=0.95；少 BIOS UUID（30）→ 70/100=0.70。

## 2 离线与时钟

判定不依赖本地时间单点，当前实现为：

1. 联网时把服务端时间 `server_epoch` 写进 HMAC 账本（`state.json`）。
2. 同一次开机内离线秒数按 `time.monotonic()` 差值累加，墙钟回拨不减少。
3. 到期判定 `check_now = max(墙钟, server_epoch + 已离线秒数)`；账本被删/改则 `need_online`。

实测（T0 为锚点时刻，mono 只走 20 天）：

| 用例 | 结果 | 关键数字 |
|---|---|---|
| 墙钟回拨 5 天 + 单调时钟 20 天，exp=T0+10d | `expired` | `offline_used_s=1,728,000`；`check_now=T0+20d`；exp+72h=T0+13d → `已过期 7.0 天` |
| 未到期（exp=T0+40d）但离线 20 天 | `need_online` | 租期 168h=604,800s，宽限 259,200s；20 天 > 10 天 |
| 篡改 `offline_used_s=0`，签名不动 | `need_online` | `缺少可信的联网锚点（账本被删改或从未联网）` |
| 删除 `state.json` | `need_online` | 同上 |

结论：把系统时间往前调不能续命；本地墙钟不是唯一判据。

## 3 遥测通路

真实进程：`tools/telemetry_server.py serve`（随机端口、临时 SQLite、临时 license.db）；客户端 `tools/telemetry_agent.py --once` 作为独立子进程。

| 用例 | 结果 | 关键数字 |
|---|---|---|
| 首传 | PASS | HTTP 200；6 事件，hits=2；看板 `GET /api/admin/user/<hash>` 返回 6 条 |
| 重复上报幂等 | PASS | 同一 body/sig 连发 2 次：`received=6 inserted=0 duplicates=6`（两次相同） |
| 篡改 body | PASS | HTTP 403 `签名不匹配（报文被改过或密钥不对）` |
| 篡改签名 | PASS | HTTP 403 同上 |
| 未登记许可 | PASS | HTTP 403 `许可未登记（可能来自旧服务器或伪造）` |
| 缺少签名 | PASS | HTTP 401 `缺少签名（X-Telemetry-Signature）` |
| 断网缓冲 | **PASS（已修复）** | 新增 3 行，`queue_lines=3`（修复前 9）；不再出现「从 0 重读」 |
| 恢复补传 | **PASS（已修复）** | 恢复后缓冲 0（修复前一度 18）；服务端最终 9 事件、hits=4 fails=3 skips=2 spent=410 |

### 失败项（修复前基线）：CRLF 台账导致增量游标每次回退到 0

> 本节记录**修复前**的现场与行号（行号已因改动位移，仅作历史基线；修复后的新位置见 3.1）。

- 位置：
  - [core/records.py](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L302) — 生产写入用文本模式 `open(PATH, "a", encoding="utf-8")`，Windows 默认换行转换写出 `\r\n`。
  - [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L213) — 回看时 `rstrip(b"\r\n")` 把行尾 `\r` 去掉了。
  - [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L254) — 首次游标哈希用的是 `raw_lines[-1]`（按 `\n` 切分，保留行尾 `\r`）。
  - [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L237) — 两套哈希口径不等 → 判定“台账被替换” → `offset=0` 重读。
- 影响：
  - 每次追加后再次运行都会全量重扫台账，增量游标失效。
  - 断网时 3 条新行被重读成 9 条进入缓冲；恢复后缓冲到 18 条后整批重发。
  - 服务端 `events.event_id UNIQUE` + `INSERT OR IGNORE`（[tools/telemetry_server.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_server.py#L226)）兜住了重复入库，所以**入库不重、数据不丢**；但客户端“按增量游标上报/不重传”不成立。
- 最小复现（Windows，用产品自己的写路径 + agent 读路径）：
  1. 用 `core.records.add()` 写 6 行台账（得到 CRLF 文件）。
  2. `python tools/telemetry_agent.py --once ...` 跑第一遍，记下游标 `offset` 与 `last_line_sha256`。
  3. 再 `records.add()` 追加 3 行，跑第二遍；日志出现 `游标最后一行对不上（台账被替换过），从 0 重读`，`queue_lines` 为 9 而不是 3。
  - 已保存的可执行复现：`_trust_run\dbg_cursor.py`（CRLF → `match False`、第二次读 9 行）；对照 `_trust_run\dbg_cursor3.py`（LF → `match True`、第二次只读 3 行）。
- 修复建议（一行级）：`raw_lines` 在计算哈希前统一 `rstrip(b"\r")`，或 `_tail_line_sha256` 与 `read_new_lines` 共用同一 `strip` 口径；也可让 `records.add()` 显式 `newline="\n"`。

### 3.1 修复与复测（2026-11-07）

修复口径：**写入字节 = 回看读到的字节 = 哈希用的字节**，三处统一到「剥掉行尾 `\r\n`/`\n`/`\r` 后做 SHA-256」，不再只在读侧打补丁。

| 修改点 | 位置 | 作用 |
|---|---|---|
| 写侧 | [core/records.py](file:///C:/Users/26346/Desktop/project/dafors/core/records.py#L305) | 追加写显式 `newline="\n"`，Windows 上不再产生 CRLF；台账字节与读/哈希口径一致 |
| canonical 哈希 | [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L197-L203) | 新增 `_line_sha256()`，唯一哈希口径 |
| 回看哈希 | [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L206-L225) | `_tail_line_sha256()` 改调 `_line_sha256()` |
| 首次哈希 | [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L302) | 新游标哈希改调 `_line_sha256()`，与回看完全同口径 |
| 换行漂移重锚 | [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L228-L257) | 新增 `_find_reanchor()`：在 `offset-8KB .. offset+64KB` 内找 canonical 哈希等于旧值的完整行，命中则前移游标、不重读 |
| 判定分支 | [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L275-L284) | 哈希对不上先尝试重锚；只有重锚失败（真替换/截断）才 `offset=0` 全量重读 |
| 空读游标 | [tools/telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L294-L298) | `cut < 0` 时返回重锚后的新 `offset`，避免重锚结果丢失、下次再判一次 |

失效面收缩：真替换、轮转变小、内容改写仍走全量重读（保留原语义）；仅 CRLF↔LF 风格变化走重锚，不重读。

**§3 原场景修正前后并列**

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 首传 | 6 事件 / hits=2 / 看板 6 条 | 6 事件 / hits=2 / 看板 6 条（不变） |
| 幂等重复上报 | `received=6 inserted=0 duplicates=6` | `received=6 inserted=0 duplicates=6`（不变） |
| 篡改 body | 403 签名不匹配 | 403 签名不匹配（不变） |
| 篡改签名 | 403 签名不匹配 | 403 签名不匹配（不变） |
| 未登记许可 | 403 许可未登记 | 403 许可未登记（不变） |
| 缺少签名 | 401 缺少签名 | 401 缺少签名（不变） |
| 断网新增 3 行 | **`queue_lines=9`**（重读全量） | **`queue_lines=3`** |
| 恢复上传 | 缓冲一度 18；最终 9 事件 | **缓冲 0**；最终 9 事件 |
| 最终入库 | 9 事件 / hits=4 / fails=3 / skips=2 / spent=410 | 同左（无膨胀、无丢失） |

断网那轮的客户端日志已不再出现 `游标最后一行对不上（台账被替换过），从 0 重读`，改为只读新增行。

**回归测试锁死**（[tests/test_telemetry_cursor.py](file:///C:/Users/26346/Desktop/project/dafors/tests/test_telemetry_cursor.py)，`8 passed in 1.44s`）

| 测试 | 锁住的行为 |
|---|---|
| `test_crlf_and_lf_lines_hash_same` | CRLF 与 LF 台账同一行哈希一致，回看哈希等于游标哈希 |
| `test_append_reads_only_new_rows[CRLF/LF]` | 追加 3 行只读到 3 行，`offset` 到文件尾 |
| `test_newline_style_rewrite_does_not_reread` | 仅 CRLF↔LF 重写：0 行、不重读 |
| `test_true_replacement_forces_full_reread` | 内容替换：6 行全量重读 |
| `test_rotation_shrink_forces_full_reread` | 轮转变小：从头读 1 行 |
| `test_offline_buffer_is_exactly_new_rows` | 断网新增 3 行 → `queue_size()==3` |
| `test_server_event_id_dedupe_still_holds` | 服务端 `event_id` 兜底不退化 |

**服务端 `event_id` 唯一的兜底确认**（`server.Store.ingest` 同批连投两次）：

| 调用 | 返回 inserted | `events` 计数 | `daily_agg.hits` |
|---|---|---|---|
| 第一次 | 3 | 3 | 3 |
| 第二次 | 0 | 3 | 3 |

`INSERT OR IGNORE` + `event_id UNIQUE` 行为未变；客户端改动只影响读多少行，不影响服务端去重。

**回归面**：`t4_records.py`（台账解析）、`t6_retention.py`（保留与轮转）重跑结果与原报告一致，无非预期变化。

### 3.2 第二轮复核：4 个边界根因修复与复测（2026-11-07）

第一轮修复的判定主判据是「回看末行哈希」，回看窗口写死 8KB，且没有文件身份概念 —— 在长末行、整份替换、轮转、大漂移四类边界上不成立。本轮把判定换成三层互斥判据：

| 层 | 判据 | 位置 | 说明 |
|---|---|---|---|
| 0 文件身份 | `st_dev:st_ino:st_ctime_ns`（Windows）/ `st_dev:st_ino`（POSIX） | [telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L251-L260) | 身份变 = 换了文件，直接进轮转/替换分支，**不再走重锚** |
| 1 内容指纹 | 游标前 4096 字节 SHA-256 | [telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L263-L278) | 追加不改这段 → 恒定；单次窗口读 1 个 4KB 块（整轮巡检还有采样块，见 3.4 的 I/O 口径） |
| 2 严格重锚 | 末行哈希 + canonical 偏移 + 物理行号 + 前 3 行上下文，**四项全中** | [telemetry_agent.py](file:///C:/Users/26346/Desktop/project/dafors/tools/telemetry_agent.py#L311-L360) | 从 0 流式扫描，双向天然覆盖；「内容重复」伪造不了锚点 |

**逐根因判定**

| 根因 | 判定 | 修复点 |
|---|---|---|
| 1 末行 ≥8192 每轮全量重读 | 成立，已修 | 主判据改为 4KB 窗口指纹（不依赖单行完整性）；`_tail_line_sha256` 改为按块倍增向前找真实行首，8KB 只是一次读够的常态，超长行才多读（异常路径，上限 64MB） |
| 2 整份替换保留旧末行 → 静默跳行 | 成立，已修 | 新增文件身份判据，身份变直接全量重读；身份不变时重锚要求行号与 3 行上下文对得上，重复内容骗不过 |
| 3 轮转丢未采集尾部 | 成立，已修 | `_read_after_identity_change()` 在 `.1..32` 里按身份找旧文件，从 `.matched` 按代数补读到 `.1`，再切当前；归档被清理则显式 `skip_reason=archive_missing` gap 事件 |
| 4 重锚单向 + 固定 64KB | 成立，已修 | 重锚从 0 流式扫描并按 `canon_offset` 精确定位，双向；扫描上限由 8MB 轮转口径约束，不再有 65536 常数 |

状态字段 `last_action` 区分 `append` / `idle` / `reanchor` / `replaced` / `rotation`，落盘在 `telemetry_cursor.json`：

```json
{"offset": 9, "file_id": "2756842188:4503599628732683:1794039341288650200",
 "canon_offset": 9, "line_no": 1, "tail_context": ["07d8fcbc…"],
 "window_sha256": "8dac9ebe…", "last_action": "append"}
```

日志文案也已分开：`换行漂移：重锚 978→993（行号/上下文校验通过，不重读）` vs `台账被替换（游标前内容变了），从 0 重读`、`检测到轮转：先补 .1 未采集的尾巴…`、`轮转归档里找不到上次采集的文件，记缺口事件…`。

**6 类场景修复前后并列**（`_trust_run\p7_cursor_edge.py`，同一场景分别喂给旧版与修复版，各自独立临时目录）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| CRLF 遗留：首读 / 追加 3 行 | 6 / 3 | 6 / 3（不变） |
| LF：首读 / 追加 3 行 | 6 / 3 | 6 / 3（不变） |
| 半行（写到一半 → 写完） | 0 行、offset 不动 / 1 行 | 同左（不变，不重复） |
| 整份替换、旧尾行逐字节保留 | **0 行**（游标跳到 EOF） | **6 行**，`last_action=replaced` |
| 整份替换、旧尾行出现在旧 EOF 之后 | **1 行**（6 行新内容丢失） | **7 行**，`replaced` |
| 截断成 1 行 | 1 行 | 1 行，`replaced` |
| 轮转（`.1` 里 5 条未采集） | **1 行**（5 条永久丢失） | **6 行**（`.1` 5 条 + 当前 1 条），`rotation` |
| 轮转但归档已被清理 | 1 行，gap=0（静默） | 2 行（1 条 gap + 1 条真事件），gap=1 |
| `.1` 被占用导致 rename 失败 | rename 失败，3 行 | rename 失败，3 行（不变） |
| 连续两次轮转（`.2`/`.1`/当前） | **1 行** | **7 行**（行 6..12 按序） |
| 并发追加 50 条 | 50 / 唯一 50 / 重复 0 | 50 / 50 / 0（不变） |
| 末行整行 8191 字节：空闲 / 追加 2 行 | 0 / 2 | 0 / 2（不变） |
| 末行整行 **8192** 字节：空闲 / 追加 2 行 | **6 / 8**（每轮重读全量） | **0 / 2** |
| 末行整行 **1MB**：空闲 / 追加 2 行 | **6 / 8** | **0 / 2** |

8191 与 8192 的差异复现了复核口径：行内容刚好装满旧的 8KB 回看窗口时，剥掉换行后少一个字节，哈希必不相等；8191 还差一个字节所以碰巧通过。

**常数级开销（20 次取中位数，同一台机器、独立临时目录）**

| 档位 | 修复前 idle | 修复后 idle | 修复前 +3 行 | 修复后 +3 行 |
|---|---|---|---|---|
| 1 万行 | 0.081ms（max 0.165） | 0.092ms（max 0.180） | 4.001ms | 3.746ms |
| 10 万行 | 0.080ms（max 0.162） | 0.085ms（max 0.217） | 3.851ms | 3.861ms |

单轮 `os.stat` 调用数：修复前后均为 **1 次**；正常轮另有 3 次 `open` / 16,384 字节（4KB 窗口 + 3 个 4KB 采样块），与行数无关（见 3.4 的 I/O 实测）。追加轮的毫秒数与台账规模无关（受 Windows 首次写后扫描影响，两档同量级）。

**端到端未回归**（`t3_telemetry.py` 重跑）

| 项 | 数字 |
|---|---|
| 首传 | 6 事件 / hits=2 / 看板 6 条 |
| 幂等重复上报 | `received=6 inserted=0 duplicates=6`（两次一致） |
| 断网新增 3 行 | `queue_lines=3` |
| 恢复上传 | `queue_lines=0` |
| 最终入库 | 9 事件 / hits=4 / fails=3 / skips=2 / spent=410 |
| 缺少签名 / 篡改 body / 篡改签名 / 未登记 | 401 / 403 / 403 / 403 |

**回归测试锁死**（[tests/test_telemetry_cursor.py](file:///C:/Users/26346/Desktop/project/dafors/tests/test_telemetry_cursor.py)，`20 passed in 3.72s`）

新增 12 个用例覆盖本轮全部根因：长末行 8191/8192/1MB 不重读、重复尾行替换必须全量重读（`write` 与 `os.replace` 两种写法）、轮转补 `.1` 未采集尾巴、连转两次按序补齐、归档清理出 gap、7 万行双向漂移只记 `reanchor`、半行等待不重复、并发 50 条无重无丢。另外在 `test_newline_style_rewrite_does_not_reread` 增加 `last_action == "reanchor"` 断言，锁住「漂移不得记成 replaced」。

**回归面**：`t4_records.py` 解析数字不变（4 行、hits=3/tries=4/spent=150/rate=75、残行恢复 1 条）；`t6_retention.py` 三种轮转失败场景前后文件哈希全部保留、只读台账写入报错且不落盘。

### 3.3 第三轮复核：改头、归档链断裂、陈旧归档与漂移成本（2026-11-07）

第二轮把「尾部四项判据」做严了，但判据仍然只看尾部；归档补读也没有处理「中间断代」和「陈旧副本」。本轮补三条判据 + 一组成本口径。

**A. 改头不改尾仍静默跳行 —— 成立，已修**

新增「已读区域采样」：游标里除了末尾 4KB 窗口，再存文件头与游标 1/3、2/3 处的 4KB 原始块哈希（`_sample_blocks`）。每轮巡检把这几块重读一遍，任一块对不上就判 `replaced` 全量重读。同时新增 `head_canon`：文件头 4096 个 canonical 字节的哈希，换行漂移下不变，所以漂移分支也能识别「头被改了」。旧游标没有这两项指纹时（判据不足），一旦窗口对不上就保守全量重读一次，宁可多读不静默跳行。

| 场景（200 行，只改前 5 行、末 3 行逐字节保留、大小不变） | 修复前 | 修复后 |
|---|---|---|
| 返回行数 | **0**（5 条静默跳过） | **200** |
| 前 5 行 | 空 | 改0..改4 |
| `last_action` | —（当 append/idle） | `replaced` |
| 游标里是否有采样 | 无 | 有（3 块） |

**B3. 归档链中间断裂少读且不报 gap —— 成立，已修**

`range(matched, 0, -1)` 遇到缺失代原来直接跳过。现在每一代缺失都发一条 gap，`shot` 里带 `gen=N`，`name` 写明「.N 代缺失」，并保留其余代的补读。

| 场景 A(3 已采+3 未采) → B(3 未采) → C(1)；删掉 `.1` 保留 `.2` | 修复前 | 修复后 |
|---|---|---|
| 返回行数 | **1**（只有 行9） | **5** |
| 真实行 | 行9 | 行3、行4、行5、行9 |
| gap 事件 | **0**（静默丢 3 条） | **1**（`.1 代缺失`） |

**B4. `.1` 陈旧导致重复上报 —— 成立，已修**

两代轮转后 `.1` 被「已采内容的副本」占用（身份不匹配、内容已采）。旧循环对非匹配代一律从 0 全读，于是已采行再报一遍。现在归档先按采样指纹判断「是不是已读内容」：

- 指纹一致 + 不超出游标 → 跳过，不重读（陈旧归档，无未采集行）；
- 指纹一致 + 超出游标 → 从游标 offset 续读，只补后面的新行；
- 指纹对不上 → 当作更新的那一代，正常补读。

当前台账也套同一条 `_resume_offset`：若当前文件只是旧内容的副本，就从 offset 续读，不从头再来。

| 场景（游标=已采 6 行的 A；`.1` 被 A 的副本占用） | 修复前 | 修复后 |
|---|---|---|
| 返回行数 | 7 | **1** |
| 重复行 | **6** | **0** |
| 实际返回 | 行0..行5（重复）+ 行10 | 行10 |
| `last_action` | — | `rotation` |

**D. 漂移成本：一次性、随行数线性 —— 已如实记录**

代码注释与日志都分开写：`_scan()` 注明「重锚是一次性成本，与行数线性相关」；日志打印 `一次性扫描 N 字节 X ms`；游标落盘 `last_scan_ms`，巡检轮为 0。idle 巡检与漂移重锚不能混成一档。

| 行数 | 方向 | 第 1 轮中位（3 次） | 第 2 轮中位（3 次） | 返回行数 |
|---|---|---|---|---|
| 1 万 | LF→CRLF | 21.4ms | 31.6ms | 0（内容未变） |
| 1 万 | CRLF→LF | 21.0ms | 20.1ms | 0 |
| 10 万 | LF→CRLF | 154.4ms | 181.6ms | 0 |
| 10 万 | CRLF→LF | 196.9ms | 220.5ms | 0 |
| 50 万 | LF→CRLF | 737.0ms | 1075.3ms | 0 |
| 50 万 | CRLF→LF | 752.2ms | 953.6ms | 0 |

三轮同机独立测量，差异来自磁盘缓存与后台负载（50 万档波动最大，单次最高 1575ms）。按量级取区间：**1 万 ~13–32ms、10 万 ~125–221ms、50 万 ~687–1075ms**，随字节数线性增长，**不是常数级**。同机 idle 巡检 10 万行为 0.13–0.23ms，10 万行漂移是它的数百到上千倍。可接受，但不能写成常数级。

**暖 / 冷两档**（上表是暖缓存值，不能当唯一口径）：

| 档位 | 暖缓存（重复扫描，上表） | 冷缓存首扫（复核独立实测） | 倍数 |
|---|---|---|---|
| 10 万行 | ~154–221ms | **1532.8ms** | 约 7–10 倍 |
| 50 万行 | ~737–1075ms | **4261.2ms** | 约 4–6 倍 |

重锚只发生一次（之后靠增量游标），所以按「一次性成本」记账，但注释/日志/文档都必须区分「暖缓存重锚」与「冷缓存首扫」；日志打印的 `X ms` 是暖值，已注明冷值约为其 5–7 倍。

**巡检成本 vs 一致性成本（20 次中位数）**

| 档位 | 修复前 idle | 修复后 idle | 修复前 +3 行 | 修复后 +3 行 |
|---|---|---|---|---|
| 1 万行 | 0.091–0.109ms | 0.139–0.196ms | 3.845–5.013ms | 3.989–5.508ms |
| 10 万行 | 0.084–0.091ms | 0.161–0.226ms | 4.082–4.985ms | 4.434–4.754ms |

（两轮测量并列取值区间。）idle 增加约 0.05–0.13ms，来源是每轮多读 3 个 4KB 采样块；与行数无关，仍是常数级。`os.stat` 每轮两版都是 **1 次**。逐字节口径：空闲轮 3 次 `open` / 16,384 字节，追加 1 行约 6 次 `open` / 49.4KB（采样 12KB + `head_canon` 16KB 的重建），见 3.4。

**两条设计内行为（不改，记录在案）**

| 现象 | 说明 |
|---|---|
| 并发整份改写时约 20 条重复进缓冲 | 采集端与写端并发，读到的可能是「写了一半的中间态」；服务端 `event_id UNIQUE` + `INSERT OR IGNORE` 兜底去重，最终入库无重复。 |
| `os.replace` 同内容原子替换 | **已修（第四轮）**：身份变化不再直接 `replaced`，先做内容校验；内容一致判「身份抖动」继续追加，见 3.4 |

**测试与回归**

- `pytest tests/test_telemetry_cursor.py` → **23 passed**（新增改头不改尾、归档链断代 gap、陈旧归档不重复三个用例）。
- `t3_telemetry.py` 端到端重跑：首传 6/hits2、`duplicates=6`、断网缓冲 3、恢复 0、最终 9/4/3/2/410、401/403/403/403 —— 全部未变。

### 3.4 第四轮复核：两处静默丢行、缺口行号、换行翻转误判、ACL 身份抖动与 I/O 口径（2026-11-07）

本轮全部场景用 `_trust_run\p8_review3.py` 原样重跑（真实临时文件、独立目录），前后分别喂给「修复前等价路径」与当前版本。

**R3b. 连续两代缺失 + 身份找不到 → 静默丢行 —— 成立，已修**

两处改动：`_archive_generations()` 不再「缺 i 且缺 i+1 就 break」，1..32 全扫；`_read_after_identity_change()` 在 `matched=None` 但仍有归档时，先按 `top..1` 逐代发 gap，再逐代补读。

| 场景（A 3 已采+3 未采 → B 3 未采 → C → D；游标停在 A） | 修复前 | 修复后 |
|---|---|---|
| `matched=None`（旧身份已被清理，只剩 `.3`，当前文件更大） gap 数 | **0** | **2**（`.2 代缺失`、`.1 代缺失`） |
| 真实行数 | 只读当前文件，A/B 共 6 行永久丢失 | **37 行**（`.3` 补读 + 当前文件），`rotation` |
| `matched=.3` 但 `.1/.2` 同时缺失 gap 数 | **0**（旧实现连 `.3` 都枚举不到） | **2**，真实行 `行3/行4/行5` + 当前 `行8`，`rotation` |

gap 事件的行号区间按代顺次分配：`.1 缺失 → 行4–9`、`.2 缺失 → 行10–15`，不重叠。

**R4b. 陈旧 `.1` 头部相同、总大小 ≤ 游标 → 静默丢行 —— 成立，已修**

`_archive_harvested()` 从「任一可比块一致即可」改为「**全部**采样块都必须可比且一致」；少一块可比就判「未采完」，走正常补读。宁可多读，不静默丢行。

| 场景（游标已读 140 行 @22150 字节；`.1`=前 4KB 原样 + 5 条补录，总 4882 字节 ≤ offset） | 修复前 | 修复后 |
|---|---|---|
| 新行找回 | **0**（判「整份已采」后跳过），gap=0 | **5/5**（`补0..补4`） |
| 本轮返回 / 其中跨轮已报 | 1（当前文件行） | 32 / 26（服务端 `event_id` 去重，新增 5） |
| 本轮重复行 | 0 | 0 |

**R3d. 缺口事件没有行号区间 —— 成立，已补**

gap 行走正常事件管线，除 `shot=old_id@offset|gen=N` 外新增 `line_no`（起始行，= 游标已读行数+1）、`line_no_end`（按平均行长估算的结束行）、`est_lines`，`elapsed` 记估算字节数。`name` 直接写区间；连续多代缺失时从**最旧**代起顺次分配估算区间，保证各代区间不重叠、不重复：

```
台账轮转缺口（.1 代缺失，行4–9，估算约6行）
台账轮转缺口（.2 代缺失，行10–15，估算约6行）
台账轮转缺口（.N 代缺失，自第N行起，行数不可知）   # 平均行长未知时
```

估算口径已在 `_gap_line` / `gap_starts` 注释写明是估算值，不是精确行号。

**R1d2. 已读区中间 CRLF↔LF 等量翻转被误判 replaced —— 成立，已修**

根因比复核给的更深一层：**只把采样块改成 canonical 哈希不够**。采样块按「固定原始字节位置」存放，中间 `\r` 增删会让整段位移，固定窗口切出的 canonical 内容随之改变，所以 canonical 采样在这个场景仍然失配（实测仍是 200 行 `replaced`）。

最终修法是加一条**按行定义、与原始偏移无关**的基线：`line_chain`（`acc = sha256(acc || sha256(canonical 行))` 逐行折叠）。行哈希剥掉行尾 `\r\n`，所以换行风格翻转不改链；行的增删/改写/移位一定改链。采样失配时先做一次 0..offset 的行链复核：一致 = 只是换行翻转，不重读；不一致才 `replaced`。

| 场景（200 行，CRLF×50 + LF×50 + CRLF×50 + LF×50 → CRLF×100 + LF×100，净 31790→31790） | 修复前（裸字节采样） | 只改 canonical 采样 | 最终（+ 行链） |
|---|---|---|---|
| 返回行数 | **200** | **200** | **0** |
| `last_action` | `replaced` | `replaced` | `reanchor`（offset 不动，只刷新采样/窗口指纹） |

复核通过后必须落一次新采样：否则下一轮采样仍是旧的，会反复触发前缀复核（10 万行每次约 0.3s、50 万行约 1.2s，白烧）。用例里已锁死「第二轮不再复核」。

行链不是「改写豁免」：`test_middle_equal_size_edit_still_replaced`（等长改第 1 行）仍 `replaced` 200 行；`test_identity_change_with_content_change_still_rereads`（身份变 + 内容变）仍 `replaced` 140 行。

**ACL 重写改 `st_ino` 触发全量重读 —— 成立，已修**

身份变化不再直接判 `replaced`，顺序改为：旧身份还在 `.1..32` 里 → 真轮转；否则做内容校验，三条全中才判「身份抖动」：游标前 4KB 窗口逐字节一致、`head_canon` 一致、**每一块**采样一致；再要求四项重锚（末行哈希/canonical 偏移/行号/前 3 行上下文）恰好落在 offset。任一缺失或对不上 → 仍走轮转/替换分支。

| 场景（`icacls /deny(R)` 等价：同内容原子替换，`st_ino` 变） | 修复前 | 修复后 |
|---|---|---|
| 140 行、无新增 | **140 行全量重读**（22290 字节、全量重发） | **0 行**，`append`，offset 不变 |
| 140 行 + 同批追加 2 行 | **142 行全量重读** | **2 行**（只读新增），`append` |
| 10 万行、无新增 | **100000 行全量重读**（16.28MB、按 batch=100/20 批每分钟 ≈ 50 分钟补传） | **0 行**，`append` |

判定依据就是上面三条内容指纹 + 四项锚点；没有新增「靠文件大小/时间戳豁免」的弱判据，所以身份变化 + 内容变化仍全量重读（有用例锁死）。

**I/O 口径：不再写「每轮只读 4KB」**

`_STATE` 实测（包装 `builtins.open` 与 `os.stat` 计数，10 万行与 1 万行同值）：

| 轮型 | `open` | `os.stat` | 字节 |
|---|---|---|---|
| 空闲轮 | **3**（窗口 1 + 采样 1 + 读剩余 1） | **1** | **16,384** |
| 追加 1 行 | **6**（多的是重建 `samples` 12KB + `head_canon` 16KB） | 1 | **49,314** |

仍与台账行数无关；代码注释（`_WINDOW_BYTES` 与 `_OPEN_PER_ROUND` 上方）、日志、本文三处已统一改成这个口径，`os.stat` 1.0 次/轮保留。

**行链复核的一次性成本（暖缓存，3 次中位）**

| 行数 | 0..offset 字节 | 行链复核 | 游标 JSON |
|---|---|---|---|
| 1 万 | 1.67MB | 26.9ms | 1.1KB（`line_chain` 64 hex） |
| 10 万 | 16.79MB | 304.8ms | 1.1KB |
| 50 万 | 84.39MB | 1185.3ms | 1.1KB |

只在「采样失配」这一异常路径触发；冷缓存口径与重锚同档（复核给的 10 万 1532.8ms / 50 万 4261.2ms 是同一类 0..offset 顺序扫）。旧版游标（有采样、无行链）第一次落盘时建一次基线（实测 10 万行约 0.3s 暖缓存），之后只做增量折叠。

**每个改动是否引入新的全量重读路径**

| 改动 | 新全量重读路径？ |
|---|---|
| R3b 全量枚举归档 + 逐代 gap | 无。只多 32 次 `stat`（仅轮转/身份分支） |
| R4b 要求全部采样可比 | 无（不读当前文件）。代价是轮转时可能多读一份陈旧归档（`_resume_offset` 命中则仍从 offset 续读；R4b 场景该归档只有 4.8KB）→ 客户端可能重复几行，服务端去重 |
| R3d 行号区间 | 无（只多字段） |
| R1d2 行链 | 无。异常时用「0..offset 顺序读」代替「整份重读 + 全量重发」，读量不增，重发量归零；判据不足（链空/游标不在行尾/读不全）时仍回落 `replaced` |
| ACL 身份抖动 | 无。每次身份变化只多一次 `_find_reanchor` 顺序扫（一次性）；内容对不上仍 `replaced` |

**测试与哈希**

- `pytest tests/test_telemetry_cursor.py` → **34 passed**。新增 4 个用例：`test_middle_equal_size_edit_still_replaced`、`test_identity_change_with_content_change_still_rereads`、`test_line_chain_matches_prefix_and_extends_on_append`、`test_legacy_cursor_without_chain_builds_it_once`（R1d2 / ACL / R3b / R4b 的 7 个用例为上一批次已加）。
- 版本哈希（SHA-256）：`tools/telemetry_agent.py` = `66B29C995827B60DC475823FB171D6AD93FAC9BA8AE93DDC3DFBDD23A001406E`；`tests/test_telemetry_cursor.py` = `1CAB9CB6D7A882D41D28DADE1EEF14ABF0C35D6838753AF9053DAA76A3CE0348`。上一轮复核绑定的 `DDC2AC85…FD795C6` 已失效。

### 3.5 第五轮复核：A1 净长度不变、A2 归档截短、A3 缺口区间、A4 采样缝（2026-11-07）

六个攻击面用 `_adv3\a1_line_shift.py` … `a6_scale.py` 原样复跑，真实临时文件、独立目录（`%TEMP%\sjz_adv3`）。结论：三个原本守住的保持一致（正常轮转 3 代 0 重复、空闲轮 3 `open` / 16,384B、10 万行最坏 2.66s→0.39s），三个破的已修，A4 的采样缝关闭。

**A1. 游标前「插一行 + 删一行」净长度不变 → 静默丢行 —— 成立，已修**

根因是两层：`_find_reanchor` 的四项锚点（末行哈希 / canonical 偏移 / 行号 / 前 3 行上下文）在净位移后仍恰好落在 `offset`，调用方把「锚点仍在 offset」当成免复核；另外 `size < offset` 分支会 `offset ±1` 微调。长度不变时尾部窗口、三个采样块、四项锚点全部命中，于是一行不见天日。

三处改动：

1. 游标新增 `mtime`（ns）。`_file_stat(path)` 一次 `stat` 同时取回 `(身份, size, mtime_ns)`，`os.stat` 仍是每轮 1 次。
2. **新增 A1 门**：`action == "append" and not jitter and size == offset and old_mtime and (mtime != old_mtime or size != old_size)` → 强制做一次 `_prefix_line_chain(path, offset)`。链一致 → 只刷新指纹（`reanchor`，不重发）；不一致 → `replaced` 从 0 全量重读（插入的新行因此不会丢）。
3. `size < offset` 分支删掉 ±1 微调：头 `head_canon` 变 → `replaced`；否则当前文件完整行链 == 旧链 → 重锚到文件尾（只可能是 `\r` 反向漂移），对不上 → `replaced`。尾部窗口失配且锚点仍在 `offset` 时，也不再直接放行，改为行链定论。

| 变体 | 修复前 | 修复后 |
|---|---|---|
| 1a 等长行互换 | 静默放行 | `replaced` 重读 150 行（内容顺序变化被检出） |
| 1b 插 `seq=2060` + 删 1 行（总字节不变） | **丢 2060（永不返回）** | `replaced`，2060 已交付，`missing=[]` |
| 1c 插 LF 行、删 CRLF 行（raw −1B） | 丢 3060 | `replaced`，3060 已交付 |
| 1d 先删后插（总字节不变） | 丢 4060 | `replaced`，4060 已交付 |
| 1e `[offset, EOF)` 等长替换 | 正常读到 | 不变（对照组） |
| 1f 单纯插入 1 行（长度变） | 丢 5060 | `replaced`，5060 已交付 |

六个变体 `missing=[]`、`判定=守住`。1a 的注记「新旧内容顺序变化不进上报」是攻击脚本硬编码的说明文字，不是丢失判定——该轮 150 行全部交付，`last_action=replaced`。

**A2. `.1` 被换成 120 行截短副本 → 单轮重发 120 行 —— 成立，已修**

游标新增 `sent_head` / `sent_tail`：每行一条 16 hex 的 canonical 内容哈希，各留 512 条（约 +19KB 游标体积）。`_sent_state()` 返回 head/tail/并集，`_sent_add()` 是 O(1) 插入且只保留最近 512 条。`_read_after_identity_change` 里归档补读一律走 `take(part, dedupe=True)` —— 命中已发送集合的行直接丢弃；**当前台账（含 `replaced` 后的新内容）不去重**，保留「替换后重报」既有语义（`test_replacement_with_duplicate_tail_rereads` 仍过）。

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 2A 每轮都巡检、连续 3 代轮转 | 重复 0 | 重复 0（60 行） |
| 2B 连转 3 代、期间不巡检 | 重复 0 | 重复 0（70 行） |
| 2C 轮转后当前文件再被替换 | 重复 0 | 重复 0 |
| 2D `.1` 换成 120 行截短副本 + 当前文件 +5 | **125 行（120 重复 + 5）** | **6 行**（1 条「上一份文件已清理」gap + 5 新行） |
| 2E 对照：`.1` 换成等长已投递副本 | 125 行 | 6 行 |

**A3. 缺口区间整体错位 —— 成立，已修**

行号口径从「当前文件行号」改为「全量历史行号」：起点 `cur_line = line_no + 1`，按**最旧代到 `.1`** 顺次累加 `_count_lines(该代)`；缺失代没有文件可量，用现存归档平均体量 `_est_lines(gen_bytes, avg)` 估算并继续推进。这样每一代的 `line_no` 都是「该代在全量历史里的起点」，不再整体前移。旧文件未读尾巴（`matched=None`、`est_tail > 0` 或 `size < offset`）单独发一条 `gen=None` 的 gap，不再因为主文件已处理就跳过。

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 3A `.2` 缺失（`.3`/`.1` 在） | gap 写 `101–140`（错位 40 行） | `line_no=141 / line_no_end=180 / est 40`；真实缺失 seq 2141–2180 = 行 141–180 |
| 3B 旧文件 10 行未读尾巴被清理 | **0 条 gap，10 行无声消失** | **1 条 gap**（`line_no=101`，「上一份文件已清理，自第101行起，行数不可知」） |
| 3C `.1..32` 全在，`.7/.15/.28` 是 3 行陈旧副本 | 陈旧副本重复上报 | 300 行数据 + 1 条保守 gap，重复 0 |
| 3D `.1..12` 在，`.2/.5/.9` 缺失 | 三条区间一起前移 | `.9=131–140`、`.5=171–180`、`.2=201–210`，按各代起点、互不重叠 |

3D 里 `a3_gap_range.py` 打印的「真实缺失」标注采用「`.1` 最旧」的假设，与本仓库轮转语义（`.1` 最新、`.N` 最旧）相反，所以标注与代码输出对不上；脚本本身无 assert，代码按真实轮转方向计算。3A/3C/3D 会额外多一条「上一份文件已清理」的保守 gap——那是 3B 必须留的痕，归档是陈旧副本、身份无法对齐，宁可留痕不静默丢行。

**A4. 4096..12288 采样缝 —— 成立，已修**

不扩 `head_canon`（那会把空闲轮读取量从 16KB 抬到 `offset/8` 级），改走「行链兜底」：身份变化时「是否抖动」的判据从 `_scan()` + 锚点 == offset 换成 `_prefix_line_chain(path, offset) == chain`；同身份、`size == offset` 且 mtime/size 有变时走 A1 门的同一条行链。行链按行覆盖 `0..offset`，与原始偏移无关，4096..12288 不再落在指纹之外。

| 场景（600 行 ≈46KB） | 修复前 | 修复后 |
|---|---|---|
| 4a 追加 1 行 + 身份变 | `append` 1 行 | 不变 |
| 4b 追加 1000 行 + 身份变 | `append` 1000 行 | 不变 |
| 4c 改 0..12288 + 身份变 | `replaced` 300 行 | 不变 |
| 4d 改 0..12288 + 身份不变 | `replaced` 300 行 | 不变 |
| 4e **只改 4096..12288**、头 4KB/窗口/三采样全不动、身份变 | **0 行，判「抖动」** | **`replaced` 600 行** |
| 4f 同上、身份不变 | **0 行** | **`replaced` 600 行** |

**成本对照（逐字节，包装 `builtins.open` / `os.stat` 实测，台账 1000 行 ≈90.9KB）**

| 轮型 | 修复前 | 修复后 |
|---|---|---|
| 空闲轮（无变化） | 3 `open` / 16,384B / 1 `stat` | **3 / 16,384B / 1**（不变） |
| 空闲轮（mtime 变、内容同） | 3 / 16,384 / 1，**静默** | 7 / **140,042** / 1（0..90,890 一次性行链复核，1.2ms），`reanchor` 不重发 |
| 空闲轮（mtime 变、内容改） | 6 / 218,644 / 1 | 6 / 218,644 / 1（`replaced`） |
| 追加 1 行（80B 行） | 6 / 49,314 | 6 / **49,232** |
| 追加 1 行（162B 行） | 6 / 49,314 | 6 / **49,314** |

空闲轮无变化时一字节未涨；只有「mtime 变了而长度没变」这一条新路径会多读一次 0..offset（90.9KB 台账 1.2ms、10 万行按行链档位约 0.3s，且只在异常轮触发一次，之后指纹刷新不再触发）。游标 JSON 由 1.1KB 增至约 20KB（`sent_head`/`sent_tail` 各 512 条），只影响本地落盘，不涉及台账读取。

**每个改动是否引入新的全量重读路径**

| 改动 | 新全量重读路径？ |
|---|---|
| A1 `mtime` 门 + 行链复核 | 无。仅在 `size == offset` 且 mtime/size 变的异常轮触发一次；正常追加 `size != offset` 不触发 |
| A2 `sent_head`/`sent_tail` | 无（只做过滤），代价是游标 +19KB |
| A3 全量历史行号 | 无（只多 `_count_lines` 每代一次，仅轮转/身份分支） |
| A4 行链抖动判据 | 无。替换原来同档的 `_scan()`/前缀扫，读量不增 |

**测试与哈希**

- `pytest tests/test_telemetry_cursor.py` → **39 passed**（26.69s）。新增 5 个用例：`test_equal_size_insert_delete_does_not_lose_line`、`test_truncated_archive_copy_is_not_resent`、`test_removed_old_file_with_unread_tail_emits_gap`、`test_sample_gap_between_fingerprints_is_detected`、`test_sample_gap_with_new_identity_is_not_jitter`；`test_two_missing_generations_emit_one_gap_each` 按新行号口径更新断言。
- `pytest tests` 全量 → **2175 passed / 6 failed / 136 skipped / 2 xfailed**（793.87s）。6 条失败全在 OCR 与自动标定子系统，与遥测游标无关：`test_ocr_panel`（`ModuleNotFoundError: rapidocr`）、`test_ocr_price`（`Backend.available=False`）、`test_fix_d` / `test_state_hardening`（`'_Cfg' object has no attribute 'data'`）。均为本机缺依赖/既有状态，非本轮改动引入。
- `a6_scale.py` 10 万行（7.99MB）：基线首读 249ms；两条最坏路径（采样失配行链复核、窗口失配 `_find_reanchor`）3 次实测 365.9–426.0ms，均 `replaced` 全量重读，与修复前 2.66s 同路径口径相比无回退。
- 版本哈希（SHA-256）：`tools/telemetry_agent.py` = `CC91E54B00D7ED051C961B93F8D9F933D4F9F4A7E4B001B0A8AB23CAF613EBF7`；`tests/test_telemetry_cursor.py` = `344D24941F3FE1E40516C2AAA381F3C9A52C1E6B999C44893AA8B3838638528E`。上一轮绑定的 `66B29C99…001406E` / `1CAB9CB6…3CE0348` 已失效。

## 4 台账解析

构造文件：正常行、`null` 字段、3000 字超长 `name`、未知字段、坏 UTF-8 行、合法非对象 JSON、无换行残行。

| 指标 | 结果 |
|---|---|
| 读路径异常 | 无，`load_no_exception=true` |
| 有效行 | 4（正常/ null/ 超长/ 未知字段） |
| 坏 UTF-8、非对象、残行 | 跳过，不抛异常 |
| hits / tries / spent / rate | 3 / 4 / 150.0 / 75.0% |
| miss | 1 |
| 残行修复 | 44 字节被截断，`recovered_lines=1` |
| 二次读取 | 仍 4 行、tries=4，稳定 |

## 5 时间同步

真实 UDP：`ntp_server.py` 子进程（127.0.0.1 随机端口）+ 真实 NTP 报文；`_resolve_ip` 仅用于模拟 DNS 映射，NTP 解析/多源去重/偏差判定为仓库原逻辑。

| 场景 | 结果 | 关键数字 |
|---|---|---|
| 三源正常（10.0.0.1 / 10.1.0.1 / 10.2.0.1） | PASS | `ok=true offset=5.0 delay=0.0 independent=3 degraded=false` |
| 单源降级 | PASS | `offset=5.000529s`，`degraded=true`，3 个稳定样本，偏差 ≤300s |
| 单源偏差 11 小时 | PASS（拒绝） | `39,600s 超出可信范围 ±300s（1 个独立来源）` |
| 三域名塌缩同一 IP | PASS（拒绝） | 9 个有效样本、独立来源 1（需 ≥3）→ `同一来源的重复应答不能当多源` |
| 断网后状态 | PASS | `ok=false`，`offset` 仍为 5.0s，`degraded=false`，`stale=false`，仅更新 `last_error` |

## 6 数据保留与轮转

真实文件 + 真实 Windows 文件占用/只读属性；故障注入只作用于 `retention.os.replace`，回滚逻辑未改。

| 场景 | 结果 | 关键数字 |
|---|---|---|
| 轮转最后一步 rename 失败（先删后失败） | PASS | `rotated=false`；cur/.1/.2 三个哈希前后完全一致 |
| 归档 `.2` 被占用 | PASS | `rotated=false`；三份内容哈希不变 |
| 截图目录含只读文件 | PASS | `removed=2`，`left=3`，两个只读文件均被清掉 |
| 日志 `.1` 被占用 | PASS | `rotate_log=false`；log/bak 哈希不变 |
| 台账只读 | PASS | `RecordWriteError`；2 行 → 2 行；文件哈希不变；`write_ok=false` |

---

## 复现入口

`_trust_run\` 下脚本（用仓库 `.venv` 解释器执行）：

```powershell
cd C:\Users\26346\Documents\Trae-CN\temp_20261107_84\_trust_run
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t1_license.py
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t2_clock.py
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t3_telemetry.py
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t4_records.py
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t5_timesync.py multi
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t5_timesync.py solo
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t5_timesync.py solo11
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t5_timesync.py collapse
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t5_timesync.py offline
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" t6_retention.py
```

结论：1/2/4/5/6 全通过。第 3 项曾因 Windows CRLF 台账导致增量游标退化（断网 3 行被读成 9 行、恢复后缓冲到 18 条），经 3.1 统一写入/回看/哈希口径、3.2 四类边界判据、3.3 改头/归档链/陈旧归档三处补丁后，复测断网缓冲 3 条、恢复后 0 条、服务端 9 事件无膨胀，`tests/test_telemetry_cursor.py` 23 passed。漂移重锚是一次性成本（10 万行 ~155–197ms），idle 巡检 10 万行 0.16ms，两者不可混为一档。

```powershell
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" -m pytest tests/test_telemetry_cursor.py -q
```
