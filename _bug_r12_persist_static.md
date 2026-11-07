# R12 数据持久化与配置一致性 — 纯静态审查

- 对象：`C:\Users\26346\Desktop\project\dafors`
- 方法：只读源码，按模块切块后逐分支推导；未运行任何程序、未改动任何代码或配置、未 commit / push。
- 严重度定义：致命＝直接造成用户配置/台账不可恢复丢失；高＝有明确触发路径且后果为用户数据错误或资金风险；中＝特定竞态或特定输入下行为错误；低＝体验/证据/维护性问题。

---

## 一、代码切块与分支覆盖面

| 块 | 文件 | 静态推导覆盖的分支 |
| --- | --- | --- |
| A 配置核心 | `core/config.py` | 文件存在/不存在；JSON 合法/非法/非 dict；读写 OSError/ValueError；`os.replace` 成功/PermissionError×6/最终失败；写 tmp 失败（ENOSPC）；`transaction` 正常/块内异常；normalize 的 5 类迁移；`_merge` 已知/未知键；gc 的 id 命中/不命中 |
| B 队列与借还配置 | `core/scheduler.py` | `add` 新增/命中 prior（含 prior 为 done/running）；`update` 活动任务/非活动；`remove`/`clear_finished`；`start` 前置闸；`_run_step` 读盘失败/无 pending/有定时/已到点/被停止；`_run_task` 状态机 RUNNING→done/failed/skipped；`_borrow_config`/`_return_config` 成功/抛错/崩溃；`_recover_stale_tasks` 的 load_ok 守卫 |
| C 台账与保留 | `core/records.py`、`core/retention.py` | `add` 轮转成功/不需要/失败；`load(limit>0)`/`load(0)`；`stats` 缓存命中/失效/并发；`clear` 当前文件替换成功/归档删除失败；`rotate` 各级 rename 失败；`prune_dir` 删除成功/失败 |
| D 外围写盘点 | `core/server.py`、`core/switches.py`、`core/license.py`、`core/preflight.py`、`core/engine.py`、`core/single_instance.py` | 开关保存（合法/非法/未知 key）；`/api/config` 白名单字段；`/api/tasks/update` 字段类型；`_set_hotkey`；`_seed_default_task`；license state 写；模板 copyfile；截图命名；engine 每 2s reload |

配置读写的实际链路只有三条：`Config.load()`（470 起，读即校验+可能自愈写盘）、`Config.transaction()`（431 起，读-改-写）、`Config.save()`（366 起，整体写）。台账链路两条：`records.add`（追加+轮转）与 `records.clear`（替换+删归档）。下面所有问题都可回溯到这几条链路的具体分支。

---

## 二、问题清单

### R12-01 `_save_locked` 的失败兜底会原地截断正式配置文件（致命）

- 位置：`core/config.py:411-419`（正常路径 `core/config.py:371-410`）
- 触发条件：写 tmp 或 `os.replace` 阶段出现非“文件被占用”类 `OSError`：磁盘满（ENOSPC）、目录/卷配额、网络盘/同步盘 IO 错误、杀软拦截写入。前 6 次 `PermissionError` 重试与第 410 行最后一次 `os.replace` 都失败即进入 411 分支。
- 推理依据（路径）：`_save_locked` 先把整份配置写 `config.json.<pid>.<tid>.tmp` 并 fsync（387-392）→ 393-410 循环 replace（每次失败 sleep）→ 只要这段抛 `OSError`（含 389-392 的 ENOSPC）→ 411 `except OSError` → 412-416 删 tmp → 418 `open(CONFIG_PATH, "w")` **原地截断正式文件** → 419 `json.dump`。若磁盘仍不可写，dump 再次抛错，函数退出，`config.json` 停留在 0 字节或半截 JSON。
- 后果：下一次任意 `Config.load()`（每次 API GET、调度器循环、engine 每 2s reload，`core/engine.py:86/1459-1461`）读到坏文件 → 走 496-514 隔离 → 用 `config.json.good` 恢复，即**回滚到上一次成功保存**：故障前新增/删除的任务、开关、框选全部消失；若 `.good` 也坏则回 DEFAULTS（队列清零）。本该防截断的兜底，反过来把好文件换成坏文件。

### R12-02 外部程序瞬时半写被判定为“配置损坏”，并立刻用旧内容覆盖（高）

- 位置：`core/config.py:496-514`、`core/config.py:542-551`
- 触发条件：用户用编辑器/云同步盘保存 `config.json`（典型是先截断后写入），恰好撞上本进程读盘；或外部脚本 `open(...,"w")` 重写。
- 推理依据：`json.load` 读到半截内容 → `JSONDecodeError`(ValueError) → 496 分支**不做重试**（只有 OSError 分支 518-520 才重试 9×20ms）→ 500 `_quarantine` 把用户正在写的文件改名成 `.broken-<ts>.bad` → 503 `_restore_good()` → 507 `cfg._save_locked()` 立刻把旧内容写回 `config.json`。编辑器随后落盘，两方互相覆盖。命中窗口不窄：`Config.load()` 在每次 HTTP GET、调度器每轮、engine 每 2s reload 都会跑。
- 后果：一次正常的外部保存被当成损坏，用户配置整段回滚（含队列），且隔离目录里留下假的“损坏样本”；回滚后的旧配置若队列为空，`serve` 启动路径还会追加一条默认 list 任务（`core/server.py:996-1020`），出现“凭空多一条任务”。即“外部改动检测”在语义上做反了：把瞬时状态当永久损坏。

### R12-03 启动时配置读失败会导致把所有任务模板目录当孤儿删除（高）

- 位置：`core/scheduler.py:149-162`（`gc_task_dirs`）、`core/config.py:580-597`、`core/server.py:1102-1104`（启动调用）
- 触发条件：启动时 `config.json` 被占用/权限不足，超过 `_load_locked` 的 9×20ms 重试（`core/config.py:515-526` 返回 `load_ok=False`），且进程刚起（`_queue_cache` 为空）。
- 推理依据：`serve` 先 `_seed_default_task()` 再 `_scheduler.gc_task_dirs()`；gc 第 157 行 `keep=[t.get("id") for t in self.tasks()]`；`tasks()`(188-191) → `_read_queue`(173-186) 读失败时返回 `self._queue_cache`，新进程该缓存是 `[]`（106）。keep=[] → `Config.gc_task_dirs` 把 `data/` 下所有“8 位十六进制名”目录当孤儿 `shutil.rmtree`（594）。
- 后果：所有已排队任务的 `buy.png/confirm.png/name.png` 被永久删除（模板是用户手截的图，不可重建）。之后每条任务 `_drive_task` 同步模板失败、preflight 报缺模板，整条队列事实上报废；递归删除无事务，删一半崩溃还会留半目录。

### R12-04 状态机在“RUNNING 已落盘、借配置未完成”处有不可恢复的幽灵任务（高）

- 位置：`core/scheduler.py:753`（先写 RUNNING）、`761`（`_borrow_config` 在 try/finally 之外）；对照 `core/scheduler.py:644-648`（候选集只认 pending/armed）
- 触发条件：RUNNING 提交成功后，`_borrow_config` 内事务抛 `ConfigLoadError`（配置 200ms 内被占用）或任何异常（如 800 行 `_engine_task` 里深拷贝失败）。
- 推理依据：753 已把 RUNNING 落盘；761 不在 `try` 内，异常直接抛给 `_run_step` 调用方 `_run`，`_run` 只记日志继续；`_run_step` 只挑 `PENDING/ARMED`（644-645）。若这是最后一条，646-648 打出“队列跑完了”并返回 False，调度线程退出。
- 后果：该任务永远停在 RUNNING：界面显示“正在执行”，实际无线程在跑；点“开始”也跳过它；只有重启进程时 `_recover_stale_tasks`(131-141) 才放回队列。一次瞬时读盘抖动就把一条任务钉死。`stop()` 在 517-520 事务失败时也会留下同样的 RUNNING 残留。

### R12-05 借配置覆盖全局区域，崩溃后无任何标记可还原（高）

- 位置：`core/scheduler.py:786-801`（借）、`784`/`803-814`（还）
- 触发条件：任务执行中进程被强杀/断电/蓝屏。
- 推理依据：`_borrow_config` 在 792-800 一个事务里把任务 regions 覆盖进 `cfg["regions"]`、把 `active_task` 换成任务字段并落盘；还原只发生在 784 的 `finally` 内，是纯内存期约定。盘上没有“借用中/待还原”标记，`_recover_stale_tasks` 只改 status（131-141），不碰 regions。
- 后果：崩溃点落在 761 与 784 之间，用户的全局框选被该任务的区域永久顶替（buy/confirm/name 全变），`active_task` 也钉在任务值；下次手动定位或新建任务按错区域点，属“用户配置被悄悄改写且无感知”。

### R12-06 还原失败被 `except: pass` 吞掉，且会回滚用户在运行中的修改（高）

- 位置：`core/scheduler.py:803-814`（尤其 813-814）
- 触发条件：还原时配置被占用/权限不足（transaction 抛错）；或用户在任务运行期间通过 `/api/tasks/update` 改了当前条件。
- 推理依据：813-814 `except Exception: pass`，不重试、不落日志、不标脏，返回值也被 784 丢弃。812 无条件用快照覆盖 `cfg["active_task"]`。
- 后果：(1) 借用状态永久留在盘上（后果同 R12-05），且完全无日志线索；(2) 运行中改价/改限量立即生效，但任务结束瞬间被静默回滚到开跑前快照，界面与磁盘出现“改了又变回去”。

### R12-07 `_engine_task` 不检查 load_ok，读失败会退化成“不限价”执行（高，资金风险）

- 位置：`core/scheduler.py:717-718`、`731-739`；语义解释端 `core/engine.py:994-996`
- 触发条件：这次 `Config.load()` 返回 `load_ok=False`（被占用超过 9×20ms），或 `.good` 也坏、恢复成 DEFAULTS。
- 推理依据：717 不判 `load_ok`；718 `console = g.get("console") or {}`；731/732 `console.get("min_price") or 0` → 0；engine 996 把 0 当“无下/上限”。quota 同样落回 1（739-740），budget 落回 0。
- 后果：该任务以“不限价 + 限量 1”开跑：任何价格都会下单，预算保护一并失效。这是“默认值悄悄顶替用户设置”里代价最高的一条：触发窗口窄，但后果不可逆且直接涉及真实资金。

### R12-08 JSONL 追加无尾部修复，断电残行会连带吞掉下一条记录（高）

- 位置：`core/records.py:80-98`（写）、`105-123`/`147-180`（读侧容错）
- 触发条件：追加写盘过程中被杀/断电，落盘停在行中（没有结尾 `\n`），或 OS 层 torn write。
- 推理依据：84-85 以 `"a"` 打开直接写 `json+"\n"`，无 fsync、无“文件是否以 `\n` 结尾”的检查，启动也不做尾部修复；下一次 `add()` 仍用 `"a"` 追加，新记录被拼进那条残行，整行无法解析，读写两侧都跳过。
- 后果：丢 1 条变成丢 2 条，且丢的是最新成交（对账最敏感的一条）。`_signature()` 只看字节数，只能感知变化、不能修复。

### R12-09 台账读写锁名不副实：`load`/`stats` 无锁，清空可能“假成功”（高）

- 位置：`core/records.py:220-243`（load）、`246-278`（stats）、`337-364`（clear）、`82-96`（add）、`26-28`（锁声明注释）
- 触发条件：引擎线程 `add()` 与 HTTP 线程 `load()/stats()` 并发；或用户点“清空”与界面轮询同时发生。
- 推理依据：注释声称“追加/读/清空在同一把锁里”，但 `load`/`stats` 全程没有 `with _lock`；三个具体竞态：①`load` 先 `_paths()`(226) 再打开，期间 `add` 触发 `rotate` 改名 PATH → `FileNotFoundError`→`RecordReadError`→ `/api/records` 500；②`stats` 269 算签名、273 扫文件、277 再算签名，中间插入 add+rotate → 缓存签名与内容不自洽，统计长期少条；③`stats` 270 判 `acc is not None` 后被 `_cache_add` 置 None(213) → `_finish(None)` 抛 AttributeError → 500。另：clear 的 351-354 `except OSError: pass` 忽略归档删除失败。
- 后果：界面随机 500、“统计比手工记账少一条”；清空时若某归档被别的句柄占用，`clear` 仍返回成功，而 `_paths()` 照样把 `.1/.2` 计入统计 → “已清空”但命中数/金额没归零。

### R12-10 `records.load(limit=0)` 的排序把最旧归档排在最新之前（中）

- 位置：`core/records.py:239-243`
- 触发条件：内部调用 `load(limit=0)`（`tools/stability` 的 caps 统计；HTTP 侧 248 行把 limit 夹到 1..5000，走不到）。
- 推理依据：`paths` 顺序 [当前, .1, .2]，每个文件 `_iter_rows` 旧→新；239-241 顺序 extend 后 242 整体 reverse，把“当前文件”整段推到列表末尾。
- 后果：返回顺序变成 `[.2 新→旧][.1 新→旧][当前 新→旧]`，与 docstring“新的在前”相反；只影响内部工具按顺序截取“最近 N 条”。

### R12-11 轮转失败不回滚，且 Windows 读句柄可让轮转长期失败（中）

- 位置：`core/retention.py:41-64`、`67-74`
- 触发条件：轮转时目标文件被其它句柄打开（Windows `os.replace` 共享冲突）或磁盘错误；或某级 rename 失败。
- 推理依据：46-49 先删 oldest；50-59 逐级改名，任一步失败 `return False`，但前面已删/已改名的动作不回滚；61 失败同样 False。调用方 `records.add`(88-96) 只把 False 当“没轮转”，继续追加。
- 后果：每次失败都可能白吃一代历史（第一次失败删掉 `.2` 后 61 又失败，下次再删时 `.2` 已是旧 `.1` 的内容）；长期失败则文件永远超 8MB，`stats` 每次全量重扫，越跑越慢。

### R12-12 配置文件“不存在”直接写默认值，并顺手覆盖唯一的 `.good`（中）

- 位置：`core/config.py:481-484`，配合 `400-405`
- 触发条件：两次读之间文件消失：杀软隔离、OneDrive/坚果云冲突改名、误移走、清理脚本。
- 推理依据：文件不存在即视为首次运行，直接 `cls()`（DEFAULTS）并 483 落盘，完全不看 `config.json.good`；此后任何一次成功保存都会把 DEFAULTS 复制成新的 `.good`。
- 后果：一次瞬时“文件不在”就把队列/开关重置为空，并把唯一恢复源覆盖成空配置，此前配置彻底不可恢复（隔离区里也没有，因为不是 ValueError）。

### R12-13 `.good` 备份是非原子复制（中）

- 位置：`core/config.py:400-405`
- 触发条件：复制过程中断电/被杀，或读源文件被占用。
- 推理依据：两个 `open` 直接 `dst.write(src.read())` 覆盖 `.good`，无 tmp+replace、无 fsync。中途失败即半截 JSON。
- 后果：之后一旦 `config.json` 损坏，`_restore_good()`(529-540) 读 `.good` 抛 ValueError → 回退 DEFAULTS（509-513）——“最近一次正常配置”的保险丝在最需要时熔断。

### R12-14 无版本号、迁移单向不可逆、回滚点只有一份会被覆盖的 `.good`（中）

- 位置：`core/config.py:261-315`（无 version/schema 字段），破坏性改写位于 `274-275`、`276-277`、`283-289`
- 触发条件：任何一次版本升级后首次加载 → 之后任意保存。
- 推理依据：`_normalize` 在内存里做不可逆改写：`pop("run_mode")/pop("dry_run")`(274-275，注释说“不写回”，但任何后续 save 都会永久写掉)、非法 `trigger_mode` 一律改 `vanish`(276-277)、`mode` 由 `workflow` 派生(283-289)。回滚手段只有 529-540 的单份 `.good`，而它每次保存都被覆盖(400-405)。
- 后果：无法判断盘上是哪个版本的配置，也无法按版本回滚；用户手改的 `trigger_mode` 等会在下次保存时静默永久改写，行为变化无告警。破坏性迁移+单点备份=只能恢复到“最后一次保存”，不能恢复到“升级前”。

### R12-15 未知字段保留策略不一致：顶层保留，区域与 hotkey 被删（中）

- 位置：`core/config.py:317-334`（`regions` 过滤在 320，`else` 在 333-334）、`core/server.py:744-760`（`_set_hotkey` 整字典替换）
- 触发条件：用户/外部工具在 `config.json` 加自定义区域名（`"regions": {"extra": ...}`），或在 `hotkey` 段加字段。
- 推理依据：320 行 `{n: b for n, b in v.items() if n in self.data["regions"]}` 把未知区域名过滤掉（内存里就没了，下一次 save 即从盘上消失）；`_set_hotkey` 用新构造的四字段字典整体赋值，`hotkey` 里的额外键被丢掉；而顶层未知键走 333-334 反而保留。
- 后果：自定义区域/热键扩展在用户不知情下被删除，且 `.good` 同步覆盖后不可恢复。同一份 schema 两套保留策略，第三方工具读写同一文件时容易踩。

### R12-16 同一字段两条写路径白名单不一致，`input_mode=message` 被静默丢弃（中）

- 位置：`core/server.py:120-121` 对照 `core/config.py:190`（`INPUT_MODES` 含 message）、`switches.py` 的 `env.input_mode` 选项
- 触发条件：`POST /api/config` 传 `"input_mode": "message"`。
- 推理依据：`_as_input_mode` 只接受 interception/legacy，其余返回 None；`_save_config`(395-400) 对 None 值“丢掉、保留原值”且不报错；而 `POST /api/switches` 走 `apply_all` 能写成 message。
- 后果：调用方拿到 `ok:true` 以为已保存，盘上仍是旧通道；排障时“我明明设了 message”无法复现。典型的一致性与静默丢弃。

### R12-17 `console.workflow` / `console.max_wear` 是可写但永不被读的死配置（中）

- 位置：`core/server.py:162-163`（白名单）、`408-418`（写入）；对照 `core/config.py:79`（DEFAULTS console 无这两键）、`core/config.py:288`（真源 workflow）、`core/scheduler.py:733-734`（真源 max_wear）
- 触发条件：通过 `/api/config` 写这两个键。
- 推理依据：`_CONSOLE_NUM` 含 max_wear、`_CONSOLE_TEXT` 含 workflow，408-417 原样写进 `console`；但引擎/调度器读的是顶层 `workflow` 与 `filter_defaults.max_wear`/任务字段。
- 后果：写入成功但永不生效的假开关，且与真源并存于同一文件——外部工具读 `console.workflow` 会得出与真实行为不一致的结论。

### R12-18 开关非法值一律回落默认值，且无范围校验（中）

- 位置：`core/switches.py:372-387`（`_coerce`）、`apply_all` 未知 key 静默跳过；`_set_switches` 回包 `core/server.py:543-546`
- 触发条件：前端/外部脚本给开关传非法值（`"12件"`、`threshold=60`（百分数）、负数点击次数、已下线的 select 选项）。
- 推理依据：372-387 解析失败一律返回 `default` 并写盘；select 不在候选项同样回落 default；没有上下限校验（hint 仅用于显示）；`apply_all` 遇到表外 key 直接跳过，接口仍返回 `ok:true`。
- 后果：用户既有设置被“默认值”悄悄替换（不是报错、也不是保留原值）；越界值（`threshold>1`、`confirm_clicks<0`、`click_hz=0`）被真实写进配置，引擎静默不成交/空转，而配置看起来“合法”。

### R12-19 走开关改 `workflow` 不派生 `mode`，盘上留下自相矛盾组合（中）

- 位置：`core/switches.py` 的 `run.workflow` → path `"workflow"`；`core/server.py:543-546` 对照 `401-404`
- 触发条件：在界面切换“抢购路线”（走 switches 接口）。
- 推理依据：`apply_all` 只 `_plant` 了 `workflow`，没有像 `_save_config`(401-404) 那样同步派生 `cfg["mode"]`；`snapshot` 只读。
- 后果：盘上留下 `workflow=list` + `mode=detail` 这类矛盾组合（本仓库 `config.json:144-145` 就是 `mode` 派生副本实存于盘上的证据）。本进程下次 load 会归一，但直接读 `config.json` 的外部工具/报表会读到旧 mode。

### R12-20 `add` 的去重键太窄，且沿用旧状态（中）

- 位置：`core/scheduler.py:304-322`（比较在 308-310，替换在 320，状态沿用 316，累计值沿用 317-319）
- 触发条件：(a) 队列已有同名同路线但另一赛季/另一品阶的任务，再 add；(b) 重加一条已 DONE 或已卡 RUNNING 的同名任务。
- 推理依据：只比较 `name.strip()` 与 `workflow`，`season/craft/regions` 不参与；命中即原位替换。
- 后果：(a) 用户以为新增，实际静默替换前一条，其筛选条件/创建时间丢失；(b) 新增后状态仍 done，点“开始”不跑；若状态是 R12-04 的 running 幽灵则永远不跑。

### R12-21 `/api/tasks/update` 不校验类型，脏值直接落盘并驱动引擎（中）

- 位置：`core/server.py:591-634`（字段白名单 601-605、数值校验只覆盖 618-620）；落盘 `core/scheduler.py:406-413`；受害点 `core/scheduler.py:594`（`.strip()`）、`644-645`（`enabled` 真值）、`core/engine.py:994-996`（价格比较）、`core/engine.py:1459-1461`（每 2s 刷新）
- 触发条件：`POST /api/tasks/update` 传 `name: 123`、`enabled: "false"`、`max_wear: -5`、`mode: "list"` 等。
- 推理依据：`update` 只 `t.update(fields)`，不校验类型；`enabled` 用真值判断；`_label` 对 name 调 `.strip()`；`max_wear` 不在 `num_fields`（618-620）故完全不过校验；`mode` 可任意写入且引擎 751 由 workflow 重推（脏值只污染盘面）。
- 后果：非字符串 name 会在引擎每 2s 刷新里抛 AttributeError，任务中途中断；“false”字符串为真→“停用”的任务照跑；负数 max_wear 让磨损比较永不成立→静默 0 成交。

### R12-22 `preflight` 用扁平 `get` 读点分键，`screen.base_size` 永不生效（中）

- 位置：`core/preflight.py:323-324` 对照 `core/config.py:342-343`（`get` 是 `self.data.get(key)`）、`core/config.py:99`（`screen` 默认段）
- 触发条件：用户按注释改 `screen.base_size` / `screen.tolerance` 适配 2K/4K 或不同 DPI。
- 推理依据：`Config.get` 不接受点分路径，`cfg.get("screen.base_size")` 恒为 None，`or` 后落到 `WANT_SIZE`/0.0 常量；即使 `cfg` 是普通 dict，结果同样是 None。
- 后果：该配置项 100% 不生效（非概率问题），分辨率基准仍写死 1920×1080：换机器/改缩放后 preflight 判定与引擎裁剪全按旧基准，用户以为改过配置。比 R12-17 更隐蔽，因为代码“看起来读了配置”。

### R12-23 license 状态写入的临时名只含 pid，无锁无重试（中）

- 位置：`core/license.py:118-123`（`_write_json`），调用点 `562-580`（`check`）
- 触发条件：同进程两线程同时 `check()`（`/api/license` 轮询 + 调度启动 + desktop 启动各一次）；或多开后第二个实例。
- 推理依据：tmp 名只用 `pid`（无 thread id），无锁、无重试、无 fsync；两线程都 `open(tmp,"w")` 截断同一个文件再各写一份 → 其中一份被截断 → `os.replace` 把半截 JSON 换到 `state.json`；Windows 上目标被读句柄占用时 replace 抛 OSError（上调未捕获者）。
- 后果：`state.json` 损坏 → 下次 `_read_json` 返回 None → 试用期/回滚计数/安装时间重置，门槛判定回到“首次安装”；多实例下计数互相覆盖（lost update）。

### R12-24 模板复制非原子，半截 PNG 会被当成“模板已就绪”（中）

- 位置：`core/scheduler.py:340-351`（`shutil.copyfile`）
- 触发条件：加任务/从别的任务复制模板的瞬间进程被杀/断电；全局模板正被替换时。
- 推理依据：`copyfile` 直接以目标路径为写入端；`has_template` 只判存在性。
- 后果：半截 PNG 被 cv2 读入，尺寸/内容不完整→匹配恒不通过；因为文件“存在”，preflight 通过、引擎不报缺模板，表现为“跑完 0 成交且无异常”——最难排查的一类。

### R12-25 成交截图按秒命名，同秒两单互相覆盖证据（中）

- 位置：`core/engine.py:2248-2257`
- 触发条件：同一任务同一秒成交两件（列表模式连抢，或 watchdog 重开后紧接着再成交）。
- 推理依据：文件名 `%Y%m%d_%H%M%S_{safe}.jpg` 只到秒；`imwrite` 对同名目标直接覆盖。
- 后果：后一单覆盖前一单截图，而 `records.jsonl` 两条记录（`core/records.py:66` 的 `shot`）指向同一路径——前一条成交的“证据”被换成后一件商品，审计链无法还原（截图是唯一不可重建的证据）。

### R12-26 `fire_at` 是客户端本地时钟，调度侧却用校时后的 `true_now()`（中）

- 位置：`core/scheduler.py:657-668`；`web/index.html` 的 `fire_at = Math.floor(Date.now()/1000)`；持久化数字化在 `core/config.py:308-311`
- 触发条件：本机时钟与 NTP 有偏差（未校时、休眠唤醒、手动改时间）时添加定时任务，且该时间戳跨重启保留。
- 推理依据：`fire_at` 按本地时钟生成并落盘；`_run_step` 与 `true_now()`（带 NTP 偏移）比较；`true_now` 离线时等于本地时钟，在线时偏差可达数分钟。
- 后果：定时任务提前/延后开抢，界面倒计时（本地时钟）与引擎判断不一致；错误随 `fire_at` 持久化跨重启保留。

### R12-27 低严重度若干（合并）

| 编号 | 位置 | 问题 | 后果 |
| --- | --- | --- | --- |
| 27a | `core/config.py:542-551` | 隔离备份名只到秒（`%Y%m%d_%H%M%S`） | 同一秒两次损坏时后一次 `os.replace` 覆盖前一份证据 |
| 27b | `core/config.py:387`、`411-417`；`core/records.py:344` | tmp 文件在非 OSError 异常（如 TypeError）时不清理；`records.clear` 的 tmp 名固定 | 残留 `*.tmp` 长期堆积，启动不清理；跨实例 clear 会共用同一 tmp |
| 27c | `core/config.py:182`、`580-597` | gc 用 `^[0-9a-f]{8}$` 判任务目录 | 任何 8 位十六进制名的非任务目录（如 `deadbeef` 哨兵、用户自建 `a1b2c3d4`）被当孤儿删除 |
| 27d | `core/retention.py:96-105` | `prune_dir` 删除失败仍 `total -= size` | 实际占用未降但循环停止，容量上限形同虚设；`.bak` 等非 `.1/.2/.3` 历史文件不计不删（仓库内 `data/records.jsonl.bak` 即此类孤儿） |
| 27e | `core/single_instance.py:31-32`、`60-62`；`core/config.py:179-180` | 互斥锁可被 `SJZ_ALLOW_MULTI=1` 绕过，非 Windows 失败即放行；配置锁是进程内 `RLock` | 多实例时 `config.json` 整文件读写互相覆盖（lost update），records 轮转与追加交错 |
| 27f | `core/server.py:252-253` | `/api/records` 先 `load()` 再 `stats()`，两次独立扫描 | 两次之间发生轮转/追加时，行列表与统计口径不一致（R12-09 的展示面） |

---

## 三、已确认正确的路径（静态可证）

1. `Config.transaction()` 在块内抛异常时不落盘（`core/config.py:431-454`），不会写出半成品业务状态。
2. `_load_locked` 的 OSError 分支只重试、不写盘（`core/config.py:515-526`），不会把“读不到”误当“空配置”写回。
3. 正常保存路径是 tmp + fsync + `os.replace`，临时名带 pid 与 thread id（`core/config.py:387-395`），同进程多线程不会共用 tmp。
4. 顶层未知键走 `else` 分支保留（`core/config.py:333-334`）；`ocr`/`time_sync`/`hotkey` 是浅合并，用户多写的子键不会被清空（`core/config.py:321-332`）。
5. `_normalize` 对 `queue` 做逐项类型过滤（非 dict 丢弃）、`fire_at` 数字化、`status` 补默认（`core/config.py:291-315`），避免了大量 KeyError。
6. `_recover_stale_tasks` 先只读判断 `load_ok`，配置读失败直接跳过、不写（`core/scheduler.py:124-145`）。
7. `_run_step` 读盘失败时不推进状态机（`core/scheduler.py:635-643`），不会把好状态改坏。
8. `records.add` 的“追加+轮转”在同一把锁内，轮转失败不影响记录本身是否写入成功（`core/records.py:82-96`）。
9. 台账读侧容忍坏行与半行：`_iter_rows` 逐行 try 跳过、`_tail_rows` 跳过首段残行（`core/records.py:105-123`、`147-180`），不会因一条坏行整页失败。
10. `records.clear` 对当前文件用 tmp + `os.replace`（`core/records.py:337-349`），当前文件替换是原子的。
11. 归档顺序与清理顺序自洽：`archives` 新→旧（`core/retention.py:67-74`），`prune_dir` 从最老开始删（`core/retention.py:94-97`）。
12. `stats` 缓存用“写前签名”校验、轮转/清空后作废（`core/records.py:204-217`、`90-94`），正常路径不会重复计数。
13. `_save_config` 对数值字段整单校验、非法即 400 且不落盘，`None` 值丢弃保留原值（`core/server.py:381-400`）。
14. license 的 state_patch 是完整骨架（含所有已持久字段），不会因“只写增量”丢字段（`core/license.py:420-524`、`566-580`）。
15. engine 的 `cfg.reload()` 失败时保留内存配置（`core/config.py:456-473`、`core/engine.py:564-600`），不会把默认值灌进运行中任务。

---

## 四、需要动态验证才能定的项

1. Windows 上 `os.replace` 对“被读句柄占用”的真实失败率，以及 6 次重试是否足够（`core/config.py:393-410`）；同时确认该场景下 `open(CONFIG_PATH,"w")` 是否也失败——这决定 R12-01 的实际触发面（若同样失败则不截断，只有 ENOSPC 类才会）。
2. 用配额/小容量卷复现“磁盘满”路径，确认 R12-01 的截断链条与 `.good` 回滚范围。
3. 用编辑器与云同步盘（先截断后写）实测 R12-02 的误判概率，尤其是与 engine 2s 轮询的相位关系。
4. 启动瞬间人为占用 `config.json` 超过 200ms，确认 R12-03 是否真会走到 `keep=[]`（取决于占用时长与杀软行为）。
5. R12-07 的 0/0 语义需在引擎里实测一单：静态显示 0=不限，但要确认 `_execute_list`/`_execute_detail` 之外是否还有兜底判断（如下单前的二次校验）。
6. R12-25 同秒两单的现实性：列表模式在同一秒内是否可能连续成交两次（与点击节奏、watchdog 重开有关）。
7. R12-26 时钟偏差的实际影响量：需在无 NTP、有 NTP、休眠唤醒三种状态下各测一次 `fire_at` 与 `true_now` 的偏差。
8. R12-27e 跨进程丢更新的可复现性（`SJZ_ALLOW_MULTI=1` 双开，交替保存开关与任务）。
9. R12-11/R12-09 中“UI 轮询持读句柄导致 rename 失败”的实际概率（取决于打开/关闭时机与文件大小）。
10. R12-02 的次生后果：配置被恢复成旧 `.good`（空队列）时，`_seed_default_task` 是否真的追加一条 list 任务并落盘。

---

## 五、结论

最需要优先处理的四条，都是“本意是自愈、实际在制造损坏”：

1. R12-01 失败兜底的原地直写（致命）；
2. R12-02 把外部半写当损坏并立刻覆盖（高）；
3. R12-03 配置读失败时清空 `keep` 删模板（高）；
4. R12-04/R12-05/R12-06 借还配置的崩溃与静默失败（高）。

台账侧的关键是把 `load`/`stats` 纳入同一把锁、清空时校验归档是否真的删掉、启动时补一次“尾部 `\n` 修复”（R12-08/R12-09/R12-11）。配置一致性侧的关键是给配置加版本号并把迁移改成可回滚、把 `console.workflow`/`screen.base_size` 这类死配置删掉或接上真源（R12-14/R12-17/R12-19/R12-22）。
