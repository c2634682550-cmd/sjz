# R13 数据持久化与配置一致性 — 动态实验审查

- 审查对象：`C:\Users\26346\Desktop\project\dafors`（`core/config.py`、`core/records.py`、`core/retention.py`）
- 审查方式：**只做动态实验**，真跑程序、真注入故障；不做纯代码推断（静态结论见 `_bug_r12_persist_static.md`，本文用实测确认/推翻）
- 环境：Windows 10 (10.0.26200)，CPython 3.11.9，非管理员
- 实验台：`C:\Users\26346\Documents\Trae-CN\temp_20261107_25\_r13_harness`（只复制 `core/config.py`、`core/records.py`、`core/retention.py` + 空 `__init__.py`，`ROOT` 自动指向实验台，**用户的真实 config.json / data/ 一个字节都没碰**）
- 约束遵守：未改项目任何文件（只新增本报告）、未 commit、未 push、子进程一律 `CREATE_NO_WINDOW`、脚本不移动系统光标
- 实验原始输出：实验台内 `result_e1.json` ~ `result_e6.json`

### 方法学说明（重要，影响结论强度）

1. **强杀怎么杀**：`Popen.kill()`（TerminateProcess），与任务管理器结束进程等价，不是优雅退出。
2. **为什么要给子进程加"节流垫片"**：真实配置 ~2.7KB、台账一行 ~300B，一次写入只有几十微秒，外部 Watchdog 不可能停在"写到 10%"。所以只在**子进程**里把 `write` 切成 8KB 片、每片 `flush`+睡 2ms，把写入窗口拉到秒级。**被审的写盘代码路径一行没改**（`open/add/save/os.replace` 原样），磁盘上留下的字节就是真实写路径产生的部分内容。
3. **满盘怎么造**：本机无管理员权限，无法挂小容量 VHD、无法设目录配额，因此用**语义等价注入**：`open(path,"w")` 先真实截断（这步是真文件系统行为），随后 `write` 抛 `OSError(28, "No space left on device")`。这精确复现了"真满盘时 open 成功、write 失败"的顺序。

---

## 一、问题清单

### R13-01 `.good` 备份是"先截断再重写"的非原子复制，中途被杀 = 备份报废，且下一次配置损坏会直接落成默认值（致命）

- 严重度：**致命**（可直接造成用户配置不可恢复丢失）
- 实验步骤：
  1. 造一份 4.19MB 的 `config.json` + 同内容 `.good`（哨兵字段 `sentinel="S1"`，含 regions/queue/threshold）；
  2. 子进程执行 `Config.load()` → 改字段 → `save()`（`save` 的顺序是：写 tmp → fsync → `os.replace` → **把新文件复制进 `.good`**）；
  3. 分别在其复制 `.good` 的 **12.5% / 50.2% / 90.2%** 处硬杀（实测落点，非计划落点）；
  4. 模拟"下一次启动前 `config.json` 被编辑器/云盘写坏"，用新进程 `Config.load()`。
- 实际观测（3/3 全部命中，无一例外）：

| 试验 | 杀在 | `.good` 能否 `json.load` | 重启日志 | 重启后 sentinel | 重启后 config 大小 |
|---|---|---|---|---|---|
| 1 | 526675 B (12.5%) | **否** | `已备份为 config.json.broken-…bad，本次用默认值启动` | `null` | 4197032 → **2693 B** |
| 2 | 2107266 B (50.2%) | **否** | 同上 | `null` | → **2693 B** |
| 3 | 3785375 B (90.2%) | **否** | 同上 | `null` | → **2693 B** |

  同时：`_restore_good` 因 `.good` 是半截 JSON 而失败，`Config._load_locked` 走"默认值 + 立刻写回"，把 4.19MB 用户配置替换成 2693B 出厂配置。
- 数据损失范围：**全量**——regions 坐标、queue 任务、threshold/click_hz/hotkey、ocr 配置、所有自定义字段一次性归零；仅剩 `config.json.broken-*.bad`（被写坏的那份）和半截 `.good`，**两个文件都不完整，无法人工拼回**。
- 与 R12 的关系：确认 R12-13（`.good` 非原子）与 R12-14（只有一份会被覆盖的回滚点）。静态审查判断它是"中"，**实测后果应为致命**：因为 `.good` 是唯一恢复源，它的写入窗口出现在**每一次 save**。

---

### R13-02 台账追加被杀：残行会连带吞掉"重启后的第一条新记录"，且被 `_iter_rows` 静默跳过（高）

- 严重度：**高**
- 实验步骤：5 条种子行 → 子进程 `records.add()` 写一条 4.19MB 大行 → 在写入的 **10.2% / 50.2% / 90.0%** 处硬杀 → 检查文件尾、有效行数 → 同进程重启后调一次 `records.add(name="after-restart")` → 再统计。
- 实际观测：

| 杀在 | 杀时字节 | 尾字节是否以 `\n` 结束 | 杀后有效行 | 重启追加一条后 | "after-restart" 是否在台账里 |
|---|---|---|---|---|---|
| 10.2% | 428001 | **否** | 5（种子） | 5 | **否** |
| 50.2% | 2105971 | **否** | 5 | 5 | **否** |
| 90.0% | 3775807 | **否** | 5 | 5 | **否** |

  三次全部：残行没有换行符，`open(...,"a")` 直接把新 JSON 拼到残行尾部，**新记录也变成非法 JSON 被跳过**；`records.load()` 正常返回 5 条，`stats()` 报 5 条，**全程没有任何告警或日志**。
- 数据损失范围：单次崩溃 = **2 条记录**（正在写的 1 条 + 重启后第 1 条）；若重启后连续多次崩溃，每次都再吃 1 条。丢失的金额/命中结果无任何补偿路径（成交截图仍在，但台账口径永久少一条）。
- 与 R12 的关系：确认 R12-08（静态判断"高"），并补上实测细节：**不需要断电，普通强杀即可稳定复现 3/3**。

---

### R13-03 多进程同时写台账：15%~19% 的记录静默消失，`add()` 却全部返回成功（高）

- 严重度：**高**（触发受 `single_instance` 保护，但 `SJZ_ALLOW_MULTI=1` 双开即复现）
- 实验步骤：8 个独立进程各 `records.add()` 100 次（每行 ~2KB，行内含唯一 `wid` 做对账），并发跑完后全量扫描 `records.jsonl*`。
- 实际观测：

| 轮次 | 期望 | 实际行数 | 坏 JSON 行 | 唯一 wid | 丢失 | 重复 |
|---|---|---|---|---|---|---|
| 第一轮 | 800 | 675 | 1 | 674 | **126 (15.8%)** | 0 |
| 第二轮（带探针） | 800 | 646 | 0 | 646 | **154 (19.3%)** | 0 |

  第二轮探针结果：8 个进程各报 `{"ok":100,"none":0,"errs":[]}` —— **100 次 `add()` 全部返回行对象、没有一次 None、没有一个异常**，但盘上少了 154 条。
- 机制（实测推断）：Windows 下跨进程 `append` 不是原子操作，各进程把自己的文件指针 seek 到 EOF 再写，两进程同时写同一偏移 → 后写覆盖先写，**整行整行地消失**（所以坏行少、丢行多）。
- 数据损失范围：并发 8 进程时约 **1/6 的成交记录永久丢失且无迹可查**；单进程（正常部署）不受影响。
- 与 R12 的关系：确认 R12-27e 的"跨实例整文件互相覆盖"，并给出量化：台账侧 15.8%/19.3%。

---

### R13-04 多进程并发配置事务：30 次入队只剩 7 条（丢 76.7%）（高）

- 严重度：**高**（同样是"双开/多实例"才触发，但后果是任务被整条覆盖）
- 实验步骤：6 个进程 × 5 次 `with Config.transaction() as cfg: cfg["queue"].append({...})`，事务内停留 10ms（模拟真实事务里的读取与校验耗时）。
- 实际观测：`expected=30, queue_len=7, lost=23, unique_ids=7`，`config.json` 每次都能正常解析（无撕裂、无损坏）。
- 数据损失范围：**23/30 条任务静默消失**，配置本身不损坏（所以没有任何"配置坏了"的告警），用户只会看到"我加的任务没了"。
- 对照实验：同一进程内 4 线程 × 5 次事务 → **20/20 全对**；4 线程"各自 load→改→save"（不走 transaction）→ **2/4，丢 2 条**。说明进程内 `RLock` 有效，**跨进程无任何互斥**，且 `load→save` 的误用模式即使单进程也会丢。

---

### R13-05 旧配置迁移：顶层 `mode` 被 `DEFAULTS.workflow` 遮蔽，列表模式被静默改成倒计时（高，条件触发）

- 严重度：**高**（触发条件：旧配置没有 `workflow` 键——即 workflow 功能上线前的所有配置）
- 实验步骤：写一份"升级前结构"的 `config.json`（`mode="list"`、`run_mode="auto"`、`dry_run=true`、`queue` 三条、自定义区域 `buy2_custom`、未知顶层键 `my_plugin`）→ 用新版 `Config.load()` → 检查内存 → `save()` → 再 `load()` 校验幂等。
- 实际观测：

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 顶层 `workflow` | 不存在 | **`countdown`** |
| 顶层 `mode` | `list` | **`detail`** |
| `run_mode` / `dry_run` | `auto` / `true` | **键被删除** |
| `regions` 键 | buy/confirm/buy2_custom/price | bar/buy/clock/confirm/list/list_refresh/name/price（**buy2_custom 消失**） |
| `queue[0]` | mode=list, fire_at="1600000000.5"(字符串) | mode=list, workflow=list, fire_at=1600000000.5(浮点), status=pending, `custom_field` 保留 |
| `queue[1]` | mode=None | mode=**detail**, workflow=countdown, fire_at=0.0, status=pending |
| `queue[2]` | 无 mode | mode=**detail**, workflow=countdown |

  原因：`_merge` 先拿 `DEFAULTS` 打底，`workflow` 默认值就是 `countdown`；`_normalize` 里 `d.get("workflow")` 永远非空，**"没有 workflow 就用 legacy mode 推断"的分支永远走不到**。
- 数据损失范围：用户原本的**列表模式**在升级后被改成**倒计时模式**（抢购路线完全不同：一条要等开抢时刻，一条要反复刷列表）；同时全局 `mode=detail` 与 `queue[0].mode=list` 自相矛盾。属"配置没丢但行为被改"，比丢字段更难察觉。
- 与 R12 的关系：R12-14 只说"迁移单向不可逆"，实测给出了具体不可逆点：`mode` 语义被覆盖、`run_mode/dry_run` 被删。

---

### R13-06 部分字典被整体替换（不补默认键），回退到老版本会 KeyError（中）

- 严重度：**中**
- 实验步骤：旧配置里 `console` 只写 `{"min_price":100}`、`nav` 只写 `{"ons_panel":[...]}`、`screen` 只写 `{"base_size":[...]}`、`filter_defaults` 只写两项 → 迁移 → 用"老版本读法"（按老结构直接取键）读盘。
- 实际观测：迁移后 `console={"min_price":100}`（无 `quota/budget`）、`nav={"ons_panel":[...]}`（无 `ons_columns`）、`screen={"base_size":[...]}`（无 `tolerance`）、`filter_defaults` 无 `sort`；老读法 `KeyError: 'quota' / 'budget' / 'ons_columns' / 'tolerance' / 'sort'`。
  另外迁移**新增**了一个回退回归：`run_mode` 被删除 → 老读法 `KeyError: 'run_mode'`（迁移前是 `"auto"`）。
  新版自身不受影响：`nav`/`console` 消费端用的是 `.get` 兜底（`core/nav_ons.py:830,865` 取 `ons_panel/ons_columns`，`core/server.py:408,583`、`core/scheduler.py:718` 取 `cfg.get("console") or {}`），不会炸。
- 数据损失范围：不丢数据，丢的是**降级能力**——降回旧版会直接抛异常或退回 `manual` 模式；用户手写残缺 `console` 时，UI 侧按索引取值会 KeyError（取决于具体调用点）。

---

### R13-07 满盘时"失败兜底"把正式配置截断成 0 字节（中；与 R13-01 叠加即致命）

- 严重度：**中**（`.good` 完好时可自愈；`.good` 也坏时等同致命）
- 实验步骤：注入 ENOSPC 到 **tmp 写** 和 **正式文件直写** 两条路径（`open` 已截断，`write` 抛 28）→ 调 `Config.save()` / `Config.transaction()` → 检查 `config.json` → 换新进程重启。
- 实际观测：
  - 只让 tmp 写失败：`save()` 成功（走回退直写），`config.json` 完整可解析（2714B，哨兵 `S3` 已生效）——**这条路径是好的**；
  - tmp 与直写都失败：`save()` / `transaction()` 双双抛 `OSError`，`config.json` 变成 **0 字节**；
  - 0 字节后重启：`已备份为 config.json.broken-…bad` → `已从最近一次正常配置备份恢复`，`ok=false`，哨兵回到 `S1`（**上一次保存前的值**）。
- 数据损失范围：满盘这一刻的所有未保存修改（1 次保存量）；若同时命中 R13-01（`.good` 半截），则升级为全量丢失。

---

### R13-08 台账写失败被完全静默：目录只读 / 台账只读 / 满盘，三种故障下 `add()` 都只返回 `None`（中）

- 严重度：**中**（成交记录丢失且无任何提示；配置侧会明确抛错，台账侧不会）
- 实验步骤与观测：

| 故障注入 | 手段 | `Config.save()` | `records.add()` 返回 | 文件是否变化 |
|---|---|---|---|---|
| 目录拒绝新建文件/子目录（`icacls /deny USER:(OI)(CI)(WD,AD)`） | 真实 ACL | **PermissionError 抛出**（`transaction` 同样抛出） | `None` | 台账 14461B/13 行 → 不变 |
| 仅 `records.jsonl` 只读（`attrib +R`） | 真实属性 | — | `None` | 13 行 → 不变 |
| 满盘（所有写路径 ENOSPC） | 注入 | `OSError` 抛出 | `None` | 13 行 → 不变 |

  三种情况 `records.stats()` 都照常返回 13（读没问题），引擎侧无任何错误回调/日志。
- 数据损失范围：**当次记录 1 条 / 每次**，可持续累积；磁盘慢、杀软锁文件时同样会静默丢。

---

### R13-09 运行中外改配置会被"下一次保存"整体覆盖，且运行中的实例不会主动感知（中）

- 严重度：**中**
- 实验步骤：长驻子进程 `Config.load()` 后持有 cfg；父进程在外部把 `config.json` 的 `sentinel` 改成 `EXT`；子进程先 `get` 再 `set note hello`（保存）。
- 实际观测：子进程读到的仍是 `S1`（无 mtime/版本校验，**不感知**）；它保存后盘上 `sentinel=S1`，**外部写入的 `EXT` 被静默覆盖**。
  对照：先 `reload()` 再保存 → `EXT2` 被正确保留（`reload` 路径本身没问题）。
- 数据损失范围：外部那一次修改（手工编辑、同步盘回写、另一实例写入）——1 次被覆盖；若外部改的是 regions/threshold，直接回到旧值。

---

### R13-10 低严重度（实测确认 3 项）

| 编号 | 观测 | 损失范围 |
|---|---|---|
| 10a | 配置 tmp 写入被杀 3/3 都留下孤儿文件：281230B / 2100873B / 3778884B 的 `config.json.<pid>.<tid>.tmp`，**启动不清理**，只在失败路径里 remove | 长期堆积占用磁盘（每次崩溃 max 1 份，只增不减） |
| 10b | 同一秒内两次配置损坏：隔离文件名只到秒，两次都叫 `config.json.broken-20261107_062036.bad`，后一次 `os.replace` **覆盖前一份证据** | 损坏取证只剩最后一份 |
| 10c | 目录 ACL 若连"读属性"一起拒（`(OI)(CI)(W)`），`Config` 构造期直接抛 `FileExistsError: [WinError 183] 当文件已存在时，无法创建该文件`（指向已存在的 `templates`） | 无数据损失，但启动失败且错误信息完全误导；只拒 `WD,AD` 时正常（仅保存时报 PermissionError） |

  补充：另一进程用 Python 方式**持有 `config.json` 读句柄**时，`save()` 仍成功（0.43s，`os.replace` 重试后通过），配置文件无损 —— 静态审查 R12-01 里"读句柄占用导致回退直写"的场景在 Python 读句柄下**不成立**，但编辑器/云盘用 `FILE_SHARE_DELETE=0` 打开时仍可能触发。

---

## 二、通过的项目（实测无问题）

| 编号 | 实验 | 结果 |
|---|---|---|
| P-01 | 配置 tmp 中途强杀 3 次（6.7% / 50.0% / 90.0%） | `config.json` **每次都完好可解析**，哨兵/大小不变（4197032B），`os.replace` 原子性有效，**没有出现半截配置** |
| P-02 | 8 线程同进程并发追加 1600 条 | 1600/1600 落盘，坏行 0，重复 0，`stats.hits=1600` 与行数一致 |
| P-03 | 4 线程同进程并发 `transaction` 20 次 | 20/20 全对，无丢更新 |
| P-04 | 6 进程 × 20 次并发 `save()`（每次 200KB 载荷） | 最终 `config.json` 可解析、`.good` 可解析、**无 tmp 残留**，无撕裂 |
| P-05 | 运行中外部追加 2 条台账 | `stats` 从 3 条 → 5 条，金额 30 → 70，**能感知并重算** |
| P-06 | 外部**同字节数**原地改写价格（3×10.0 → 3×99.0，文件长度不变） | `stats.spent` 30 → 337，**仍反映新值**（不因"大小未变"读到脏缓存） |
| P-07 | 运行中把 `config.json` 写成半截 JSON → `reload()` | 自动隔离为 `.bad` + 从 `.good` 恢复，内存不被清空，盘上被写回为合法配置；再重启 `ok=true` |
| P-08 | 只读 `config.json`（`attrib +R`） | `save()`/`transaction()` 抛 `PermissionError`（明确报错），原文件 2714B/哨兵 `S1` 无损 |
| P-09 | 迁移幂等性 | `load()+save()` 连做两次，文件字节完全一致（3378B = 3378B） |
| P-10 | 未知字段保留 | 未知顶层键 `my_plugin` 完整保留；`ocr.ppocr.custom_opt`/`ocr.paddle_vl` 保留；`queue[0].custom_field="keep-me"` 保留；缺省值正常补齐（`has_new_defaults=true`，`threshold=0.9`、`click_hz=77` 未被覆盖） |

---

## 三、无法验证 / 只能部分验证

| 编号 | 项 | 原因 | 替代做法与残余风险 |
|---|---|---|---|
| U-01 | **真·小容量卷写满** | 本机非管理员：`New-VHD/Mount-VHD`、`diskpart`、`fsutil quota` 均不可用；RAM 盘需装驱动 | 用 ENOSPC 注入（`open` 真截断 + `write` 抛 28）复现代码路径。**未覆盖**：NTFS 保留簇、系统盘"低磁盘"预留、写一半后元数据刷盘顺序、杀软在满盘时的行为 |
| U-02 | **真实断电** | 无法断掉本机电源 | 用 `TerminateProcess` 强杀。**未覆盖** OS 写缓存未落盘、磁盘重排、目录项与文件大小不一致——真断电下 R13-01/R13-02 只会更糟，不会更好 |
| U-03 | **真·旧版本回退** | 仓库没有历史可执行体，无法真的降级运行 | 用"按老结构读取"的函数模拟老代码。**未覆盖**老版本的真实容错写法（是否 `.get` 兜底） |
| U-04 | 杀软/云同步盘占用 `config.json` 的碰撞概率 | 本机未装同类软件 | 只测了 Python 读句柄（结论：不阻塞 replace）。真实 OneDrive/360 的 `FILE_SHARE_DELETE=0` 行为未测 |
| U-05 | 真实 `data/records.jsonl` 轮转与保留策略压力（>8MB 反复轮转） | 按"不动用户台账"要求，未在真实数据上做大文件压力 | 只在实验台做单条 4.19MB 行（已验证单条超阈值会触发轮转），未做"8 个进程 + 持续轮转"的组合压力 |
| U-06 | OCR/服务侧对 `add()` 返回 `None` 的后续处理 | 需要跑起完整引擎与 playwright 流程 | 只验证到"`add()` 返回 None"，未验证引擎是否把这一单算作成功（静态看是忽略返回值） |

---

## 四、结论与处置优先级

**必须改（会造成不可恢复丢失或行为错误）**

1. **R13-01**：`.good` 必须改成"写 `good.tmp` + `os.replace`"，或干脆只用 tmp+replace 的原子路径；否则每次 save 都在制造唯一回滚点的损坏窗口。
2. **R13-02**：`add()` 前补"尾部 `\n` 修复"（或写前检查文件结尾字节），否则一次强杀永久吃掉 2 条并从此静默。
3. **R13-05**：迁移里判断"配置里到底有没有 `workflow` 键"要基于**原始文件**（`_merge` 之前）而不是合并后的 `d`；否则老用户的列表模式会被静默改成倒计时。
4. **R13-09**：保存前比对 mtime/内容哈希，冲突时提示而不是静默覆盖（这是"我改的设置自己变回去了"的直接来源）。

**应当改（有明确触发路径）**

5. **R13-03 / R13-04**：跨进程要么真互斥（命名互斥体/文件锁），要么在 `single_instance` 失效时直接拒绝启动；`SJZ_ALLOW_MULTI=1` 目前等于关掉了唯一保护。
6. **R13-08**：`add()` 的 `None` 必须向上冒泡成可见错误（至少日志 + UI 提示），否则"运行中丢单"永远查不出来。
7. **R13-07**：兜底直写前先确认"tmp 失败是不是 ENOSPC"，满盘时不要截断正式文件（先 rename 旧文件为 `.bak` 再写）。

**可以顺手改**

8. R13-06（迁移时对 `console/nav/screen/filter_defaults` 做深合并，不要整体替换）、R13-10a（启动清 tmp）、R13-10b（隔离名加毫秒/序号）、R13-10c（构造期捕获 `OSError` 并给出"目录不可写"的可读信息）。

---

### 实验证据索引

| 文件 | 内容 |
|---|---|
| `_r13_harness\result_e1.json` | 强杀：台账 3 次、配置 tmp 3 次、配置 `.good` 3 次 |
| `_r13_harness\result_e2.json` | 并发：8 进程台账、8 线程台账、6 进程配置事务、4 线程事务、load→save 误用、clear 撞追加、配置保存风暴 |
| `_r13_harness\result_e3.json` / `result_e3b.json` | 只读目录（宽/窄两种 ACL）、只读台账、只读配置、满盘（tmp 失败 / 直写也失败 / 重启恢复） |
| `_r13_harness\result_e4.json` | 运行中外改台账（追加/同尺寸改写/删除）、外改配置（未 reload 保存 / reload 保存 / 写坏 reload） |
| `_r13_harness\result_e5.json` | 旧配置迁移全字段对照、老版本读法回退、幂等、未知字段保留 |
| `_r13_harness\result_e6.json` | 读句柄占用 save、同秒两次损坏隔离名冲突 |
