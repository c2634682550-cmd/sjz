# R12/R13 持久化可靠性修复报告

范围：`core/config.py`、`core/records.py`、`core/retention.py`；第三轮另有一处 3 行改动在 `core/switches.py`（见文末说明）。
未触碰：`core/scheduler.py`、`core/engine.py`、`core/server.py`、`core/scene_guard.py`、`web/index.html`、`tests/` 下既有文件。
证据来源：`_bug_r12_persist_static.md`、`_bug_r13_persist_dynamic.md`。

新增测试：`tests/test_persist_hardening.py`（38 项，全绿，18.4s；含第二轮返工 10 项、第三轮故障注入 8 项）。
对照脚本（仓库外）：`%TEMP%\..\Trae-CN\temp_20261107_46\persist_compare.py`，基线副本 `baseline_persist/` 为修复前代码。

---

## 0. 结论速览（同一批场景，修复前 vs 修复后）

| 实验 | 修复前（原始代码） | 修复后 |
|---|---|---|
| 保存中强杀 3 次，`.good` 坏备份数 | **3/3** | **0/3** |
| 崩溃残行后再写一条 | 读到 **5** 条，`after` **丢失** | 读到 **6** 条，`after` 在；打印「截断 1720320 字节残行」 |
| 8 进程 × 100 条台账 | 落盘 **589**，合法 **587** | **800 / 800** |
| 6 进程 × 5 次配置事务 | 活下来 **5/30** | **30/30** |
| 老配置 `{"mode":"list"}` 迁移 | `workflow=countdown`（行为被换） | `workflow=list` |
| 满盘保存失败后原文件大小 | **0 字节**（被截断） | **19 字节**（原文件不动） |
| 只读台账写入 | 返回 `None`，无任何可见失败接口 | 返回 `None` + `last_error()` + 控制台记录 |
| 外部改配置后未 reload 就 save | `sentinel` 被覆盖回 `S1` | `sentinel=EXT` 保留，`note` 合并进盘 |

审查报告给出的原始观测（3/3 坏备份、八进程丢 126~154 条、六进程只活 7 次、4197032B→2693B）与此处复现的**同一类、不同量级**数字吻合，差异只来自进程调度时序。

---

## 1. `.good` 备份必须原子化（R13-01 / R12-13）

**根因**：`save()` 用 `open(CONFIG_PATH + ".good", "wb")` 直接覆盖——先截断再写。写到一半进程被杀，`.good` 是半截 JSON；下一次 `config.json` 一坏，`_restore_good()` 把半份备份当合法配置用，4197032 字节缩成 2693 字节，区域与队列全丢。

**改法**（`core/config.py`）
- `_write_backup(payload)`：写 `config.json.good.<pid>.<tid>.tmp` → `fsync` → **读回逐字节比对**（长度一致 + 内容一致 + `json.loads` 结果是 dict）→ `_replace_with_retry()` 原子替换。
- 校验不过：删 tmp、打印「保留上一份可用备份」、**不动旧 `.good`**；备份失败不影响本次主文件保存。
- `_read_backup()`（恢复侧）：`getsize<=0` → 当没有备份；读回字节数 ≠ `stat` 大小 → 当没有备份；解析失败/非 dict/空对象 → 当没有备份。**校验不过一律视为「没有备份」，绝不拿坏的当好的用。**
- `_replace_with_retry()` 只对 `PermissionError`（Windows 共享冲突）指数退避重试 7 次，其它 `OSError` 立即抛。

**证据**
- `test_kill_during_save_never_leaves_a_broken_backup`：真实子进程写 4MB 配置，在 1.15/1.45/1.75s 三次 `kill()`，断言 `config.json` 与 `.good` 都能解析、且孤儿 tmp 被清。
- `test_backup_replace_failure_keeps_previous_good`：monkeypatch 让 `.good` 的 replace 必败，旧备份内容保持不变。
- `test_broken_backup_is_ignored`：手工造半份 `.good`，坏配置加载后**不得**恢复成半份内容。
- 对照脚本 `backup_kill`：基线 3/3 坏、修复后 0/3。

---

## 2. 台账半行吞掉下一条（R13-02 / R12-08）

**根因**：`add()` 直接 `open(PATH, "a")`。崩溃后文件尾没有 `\\n`，新记录被拼在残行后面，**新记录自己也变成非法 JSON**；`_iter_rows` 逐行 `json.loads` 失败即静默跳过 → 每次崩溃丢两条，且没有任何提示。

**改法**（`core/records.py`）
- `_repair_tail(path)`：文件尾不是 `\\n` 时，从尾部按 8KB 倒查最后一个换行，`truncate()` + `fsync` 截掉残行；返回截掉字节数，不需要截返回 0，修不动（只读）返回 `-1`。
- 截断成功写 `_recovery_state` 并打印 `[records] 上次崩溃留下 N 字节残行，已截断：<path>`——**恢复过程有明确记录**，不是静默。
- 截不动时退化为 `_needs_newline()` + 追加前补一个 `\\n`，保证新记录仍从干净行边界开始。
- `stats()` 返回值新增 `recovered_lines`，数量可被上层看到。

**证据**
- `test_partial_line_is_truncated_so_next_record_survives`：子进程写一条 8MB 记录，0.5s 后强杀，重启后再 `add()`，断言下一次读到的条数 = 崩溃前条数 + 1，且 `after` 存在。
- 对照脚本 `residual`：基线 `5 条 / after=False`；修复后 `6 条 / after=True / 行尾换行=True`，并输出截断日志。

---

## 3. 多进程写入丢数据（R13-03 / R13-04 / R12-09）

**根因**：`_save_lock` 只是**进程内** `threading.RLock`，对另一个解释器完全无效。两个进程各自 load→改→save，后写的整份覆盖先写的；台账侧 `add()` 的 append 之间没有互斥，轮转 rename 还能和 append 相撞。所有调用方都拿到"成功"。

**改法**
- `core/config.py`：新增 `file_lock(path, timeout, poll)`（Windows `msvcrt.locking(LK_NBLCK, 1)`，POSIX `fcntl.flock(LOCK_EX|LOCK_NB)`），轮询 2ms 起指数退避到 20ms；拿不到锁抛 `FileLockTimeout`（`OSError` 子类）。
- `_proc_lock()`：同进程可重入壳（深度计数），`save()` / `_save_locked()` / `transaction()` / 全程持 `config.json.lock`。
- `transaction()`：读-改-写整段持锁，读失败抛 `ConfigLoadError`（不拿默认值覆盖原文件）。
- `core/records.py`：`_file_guard()` 套 `file_lock`，`add()` / `load()` / `stats()` / `clear()` 全在锁内；append 与 `retention.rotate` 也在同一把锁内（rename 不再撞写）。
- 锁文件位置：配置锁 `CONFIG_PATH + ".lock"`；台账锁 `sha1(abspath(PATH))[:16]` → 系统临时目录（避免在仓库/测试目录留 `.lock` 垃圾）。
- 失败语义：`save()` 等锁超时抛 `FileLockTimeout`；`records.add(strict=True)` 抛 `RecordWriteError`，默认返回 `None` 但 `last_error()` / `stats()["write_ok"]` 可见。

**证据**
- `test_eight_processes_keep_all_records`：8 个**真实进程** × 100 条，断言 800 条唯一、全部可解析。
- `test_six_processes_keep_all_config_writes`：6 个真实进程 × 5 次 `Config.transaction()` 入队，断言 30/30。
- `test_lock_timeout_is_an_explicit_failure`：另一进程持锁 3s，主进程超时 0.5s 必须抛 `FileLockTimeout`（不是假装成功）。
- 对照脚本 `mp_records` 589/587 → 800/800；`mp_config` 5/30 → 30/30（审查原值 7/30）。

---

## 4. `mode="list"` 被静默改成 `countdown`（R13-05）

**根因**：迁移判断用的是 `_merge` 之后的 `d["workflow"]`，而 `DEFAULTS` 已经填了 `countdown`，于是"用户没写 workflow"与"用户写了 countdown"无法区分；老配置只有 `mode` 时被默认值顶掉。

**改法**（`Config.__init__` 保存 `_raw` 原始输入；`_normalize()` 用 `_raw` 判断）
- 只有 `_raw` 里真的写了 `workflow` 才认它。
- 否则按 `_raw["mode"]` 推导：`list→list`、`detail→detail`。
- 推导不出来（老配置连 `mode` 都没有）才落到默认值——**不拿"默认值"当"用户选择"**。
- 幂等：`workflow` 一旦落盘，下次加载走「显式写了 workflow」分支，不再重推导。

**证据**
- `test_legacy_mode_list_is_not_silently_switched`：`{"mode":"list"}` 连续加载两次都得到 `workflow=list`（幂等）。
- `test_workflow_written_explicitly_still_wins`：显式 `workflow=detail` + 旧 `mode=list` 时不回退。
- 对照脚本 `migration`：`countdown` → `list`。

---

## 5. 部分字典整体替换（R13-06 / R12-15）

**根因**：`_merge` 对 `console/nav/screen/filter_defaults/premarket/hotkey/ocr` 整节替换，用户只写了半节就把默认子键抹掉；回退老版本读 `console["quota"]` 直接 `KeyError`。

**改法**
- 新增 `_deep_merge(base, incoming)`：dict ∩ dict 递归按字段合并，缺失字段保留；非 dict 才整体替换。
- `regions` 仍走白名单（保持既有 `test_unknown_region_keys_are_dropped` 语义）；`queue` 仍整表替换（列表语义）。

**证据**：`test_partial_sections_keep_default_keys` 断言 `premarket` 等节的默认子键仍在且用户值生效。

---

## 6. 满盘兜底截断配置（R13-07 / R12-01）

**根因**：`_save_locked` 在 tmp 写失败后"回退直写"`open(CONFIG_PATH, "w")`——这行本身在满盘时也会先截断成 0 字节再失败，原配置直接没了。

**改法**
- 删掉整个直写兜底。`_write_payload()`：写 tmp → `fsync` → `os.replace`；任何 `OSError` 清 tmp 后**原样抛出**，`config.json` 保持不变（宁可这次不保存）。
- 自愈回写 `_try_write_back()`：写不进去只告警，不让整次加载失败。

**证据**：`test_save_failure_keeps_original_config`（monkeypatch 让 tmp 写入 ENOSPC）断言抛 `OSError` 且原文件字节未变；对照脚本 `enospc`：0 字节 → 19 字节（原内容完整）。

---

## 7. 台账写失败静默返回 None（R13-08）

**根因**：`add()` 把 `OSError` 全吞成 `return None`，只读目录 / 只读文件 / 满盘三种故障下调用方无法区分"记下了"与"丢了"，统计照常显示旧数据。

**改法**
- 新增 `_error_state` / `_recovery_state`、`last_error()`、`recovery_log()`、`_note_error()`（同类错误只打一次日志，避免刷屏）。
- `add(strict=False)` 默认仍返回 `None`（不拦成交），但失败进 `_error_state` 并打印 `[records] 写台账失败（...）—— 这一行没有落盘`；`strict=True` 抛 `RecordWriteError`。
- `stats()` 额外返回 `write_ok` / `write_error` / `recovered_lines`。写成功后 `_note_ok()` 清除错误态。

**证据**：`test_readonly_records_file_reports_failure`（chmod 只读）、`test_disk_full_records_write_reports_failure`（ENOSPC 注入）均断言 `stats()["write_ok"] is False` 且 `last_error()` 非空；对照脚本 `readonly`：不可见 → 可见。

---

## 8. 其余四条（R13-09 / R13-10a / R13-10b / R13-10c）

| 项 | 根因 | 改法 | 证据 |
|---|---|---|---|
| 外改配置被未 reload 的 save 覆盖（R13-09） | save 只有"本对象快照"，不知道盘上变了 | `__init__` 记 `_disk_sig=(st_mtime_ns, st_size)` 与 `_loaded_data` 基线；`__setitem__`/`set_region` 记 `_dirty`；`save()` 先 `_maybe_merge_external()` 做 ours/base/theirs 三方合并（本对象没碰过的顶层键一律采用盘上新值），再写 | `test_save_merges_external_edits_instead_of_clobbering`；对照脚本 `external`：`S1` → `EXT`+`note` |
| 孤儿 tmp 不清理（R13-10a） | 只有失败路径 remove | `_cleanup_orphan_tmp()`：`Config.load()` 时按 `_TMP_RE` 清理 mtime > 60s 的 `config.json[.good].<pid>.<tid>.tmp`；10s 节流，避免每次 load 扫目录 | `test_orphan_tmp_files_are_cleaned`（旧的删、正在写的不删） |
| 同秒备份同名互覆盖（R13-10b） | 隔离名只精确到秒 | `_quarantine()` 文件名加纳秒后缀 `_%09d`，仍冲突再追加 `-n`；保留 `.broken-<ts>` 前缀兼容既有断言 | `test_corruption_backups_do_not_overwrite_each_other`（同秒坏两次留两份） |
| 构造期 `FileExistsError 183` 文案误导（R13-10c） | `makedirs` 的原始异常直出 | `_ensure_dirs()` 包装成 `OSError("无法使用目录 <d>：同名路径已存在但不是目录，或没有读写权限（原始错误：...）")` | `test_dir_error_message_points_at_the_real_cause` |

附带的同类修正（同一根因面，2 处，均有测试或既有用例覆盖）：
- `_maybe_merge_external()` 合并后把 `_raw` 换成盘上那份；`_normalize()` 的 `had_wf` 同时认「内存里改过 workflow」（`"workflow" in _dirty`）。否则外部合并会把内存里刚选的 workflow 按旧 `mode` 推导回去——和 R13-05 是同一类静默改行为。
- `Config.gc_task_dirs()`：`load_ok` 为 False 时直接返回 0（R12-03 的删除面在本人文件里封死：配置读不出来时 `keep=[]` 会把 `data/` 下所有任务目录当孤儿删掉）。测试 `test_gc_task_dirs_is_skipped_when_config_unreadable`。

---

## 9. 两种独立复核方式

1. **pytest（`tests/test_persist_hardening.py`，18 项）**
   - 真实子进程强杀：`subprocess` + `CREATE_NO_WINDOW` + `p.kill()`，`Slow` 包装器把写盘切成 8KB 片逐片 flush，把"写到一半"的窗口放大到秒级。
   - 真实多进程：6/8 个解释器，不是线程假装。
   - `python -m pytest tests/test_persist_hardening.py -q` → `18 passed in 15.30s`。
2. **仓库外对照脚本**（不同实现、不同载荷、不同时序，跑同一批场景）
   - `python persist_compare.py baseline`（修复前副本 `baseline_persist/`）与 `python persist_compare.py fixed`（`core`），输出见第 0 节。
   - 基线侧 `backup_kill` 用「patch `os.replace` 写 phase 标记 + 0.35s 后杀」确保落在备份写入窗口内。

编译与回归：
- `python -m compileall -q core/config.py core/records.py core/retention.py tests/test_persist_hardening.py` 通过。
- `pytest tests/test_config.py tests/test_records.py tests/test_fix_a.py tests/test_fix_e.py tests/test_server.py tests/test_switches.py tests/test_scheduler_queue.py tests/test_scheduler_add.py -q` → `95 passed, 2 skipped`。
- `pytest tests/test_persist_hardening.py tests/test_config.py tests/test_records.py tests/test_fix_a.py tests/test_fix_e.py tests/test_robustness.py -q` → `81 passed, 1 failed`；失败项为**既有问题**：`test_every_source_file_compiles` 报 `tools/_r08_dbg.py` 与 `tools/_r08_merge.py` 带 UTF-8 BOM（不在本次改动范围）。

---

## 10. 待接线清单（需要 `scheduler.py` / `server.py` / `web/` / `tools/` 配合，本次未改）

1. **启动 GC 顺序**（`core/server.py` + `core/scheduler.py`）：`Config.gc_task_dirs()` 已在 `load_ok=False` 时拒绝删除，但启动序列仍建议「先确认 `load_ok` 再 GC」，并在删除前打印将删目录列表（R12-03 的可见性部分）。
2. **台账健康度上抛**（`core/server.py` `/api/records`、`web/index.html`）：`stats()` 已返回 `write_ok` / `write_error` / `recovered_lines`，接口与界面需要透传并展示（例如红色状态点 + "上次崩溃修复 N 行"）。`records.add(strict=True)` 的调用点由上层自行选择（成交记录建议保持非 strict，不拦成交）。
3. **保存失败的用户可见性**（`core/server.py` 各开关接口、`core/scheduler.py` 任务落盘）：`save()` 现在会抛 `OSError` / `FileLockTimeout` / `ConfigLoadError`，这些调用点需要 catch 并回 5xx 或写调度日志，否则异常会顶掉线程而用户只看到「改了没生效」。
4. **多开保护**（`core/server.py` / 入口）：`SJZ_ALLOW_MULTI=1` 仍然关掉单实例检测；现在由文件锁兜底不丢数据，但建议在多开时给出明确提示（多开下配置改动是"后来者合并"而非"先来者胜"）。
5. **长时间循环里的旧快照**（`core/engine.py`）：引擎若持有 cfg 对象跨多轮，建议在每轮开头 `cfg.reload()`（现在 reload 失败会保留内存原值，不再被默认值清空），或改用 `Config.transaction()`。
6. **启动早期清 tmp**（可选）：`_cleanup_orphan_tmp()` 目前挂在 `Config.load()` 上（10s 节流）。若希望进程一启动就清，可在 `server.serve` 显式调一次。
7. **`tools/_r08_dbg.py`、`tools/_r08_merge.py`** 去掉 UTF-8 BOM，即可让 `test_every_source_file_compiles` 转绿（既有失败，与本次改动无关）。
8. `config.json.good`、`config.json.lock` 在仓库根由既有测试/生产运行产生（未跟踪文件），本次未动；如不需要可删除。

---

# 第二轮返工（独立复核 F1 / F3 / R1 / R5）

| 项 | 根因 | 改法 | 证据 |
|---|---|---|---|
| **F1（阻塞）** 恢复成功却 `load_ok=False` | `_load_locked` 从 `.good` 恢复出 cfg 后仍写 `_load_error`，调度器拒启动、孤儿任务不复位、目录不清理、界面显示「配置读不出来」 | 恢复成功 = 加载成功：不再置 `_load_error`，损坏改走 `_notice`（只提示、不影响可用性）；只有真读不出/真无法恢复才置错误 | `test_recovered_config_is_reported_as_ok`；`test_recovered_config_starts_and_runs_a_task`：真起 `Scheduler()`、`start()` 后轮询到任务 `done`（不是只测返回值） |
| **F3** 外部新增任务被丢 | 三方合并里 `queue` 整份取本地 | 新增 `_merge_queue()`：按任务 `id`（无 id 用规范化 JSON）做任务粒度三方合并——本地改/新增保留、本地没动盘上改则用盘上、本地没动盘上删则跟删、盘上新加追加末尾 | `test_external_queue_addition_is_not_dropped`；`test_external_queue_deletion_wins_when_local_untouched` |
| **R1** 隔离瞬间走「首启」覆盖备份 | 读路径没进 `_proc_lock`，另一进程看到 `config.json` 短暂不存在 → 写默认值 | `Config.load()` / `reload()` 读路径同样进 `_proc_lock`；且「文件不存在」分支先尝试 `.good` 恢复，恢复不到才按首启写默认 | `test_read_path_waits_for_the_cross_process_lock`；`test_missing_config_restores_backup_instead_of_defaults` |
| **R5** OCR 日志轮转吞错且无兜底 | `rotate_log` 吞 `OSError`；只在 spawn 时调用 | 失败进 `_note_rotate_error`（同类只打一次）；新增 `sweep_logs()` 按目录 600s 节流扫描超限 `ocr_service_*.log`，挂在 `Config.load()` 兜底触发 | `test_log_rotation_failure_is_recorded`；`test_log_sweep_rotates_oversized_ocr_logs`；`test_config_load_triggers_the_log_sweep` |

第二轮自证：`tests/test_persist_hardening.py` 27 passed；把 F1/F3/R1/R5 四处行为回退到修复前（`prev_repo/`）后，7 个新增用例全部失败；恢复修复后全通过。

---

# 第三轮：故障注入（`_verify_faultinject.md`）

新增用例后 `tests/test_persist_hardening.py` **38 passed in 18.37s**；`tests/` 全量 `1840 passed, 133 skipped, 3 failed`（3 项失败均为既有环境缺资源：`_shots/drv_onsale.png` 缺失、OCR onnx 模型不可用，与本次改动无关）。

| 项 | 根因 | 改法 | 证据（修前 → 修后） |
|---|---|---|---|
| **P11** 同尺寸同 mtime 的外部改动被旧快照覆盖 | `_disk_sig=(mtime_ns,size)`，备份还原 / `copy /b` / 同步盘可以做到两者都不变 | `_disk_sig` 加 `sha1(内容)`；配置只有几 KB，每次多读一遍是微秒级，不做缓存表 | `test_external_same_size_same_mtime_edit_survives`：外部把 quota 1→5、`os.utime` 还原 mtime、再 save → 5 保留。把指纹换回旧实现（只 mtime+size）对照复现：保存后 quota 被盖回 1 |
| **P5** 台账尾部半个 UTF-8 字符 | 文本模式迭代中途抛 `UnicodeDecodeError`，前面的有效行一起炸，重读仍炸 | `_iter_rows` 改二进制逐行 + `json.loads(bytes)`，坏行只影响自己；最后一行不完整时 `_repair_tail` 自动截到最后一个完整行 | `test_half_utf8_tail_is_tolerated_and_repaired`：2 条有效记录全部读出、`spent=30`、尾部被自动截掉、再读正常。旧文本模式读法对照：抛 `UnicodeDecodeError` |
| **E1/E1b** BOM / UTF-16 被当坏文件 | 固定 `encoding="utf-8"`，BOM 字符导致 JSON 解析失败 → 隔离 + 旧 `.good` 覆盖 | 新增 `_decode_config`：UTF-8 BOM / UTF-16 BOM / 无 BOM UTF-16 启发式；解析成功即按 UTF-8 回写，并给 `_notice` | `test_non_utf8_config_is_loaded_not_quarantined`（3 组参数全过）。官方注入：`E1.config-utf8-bom PASS`、`E1b.config-utf16 PASS`（threshold=0.55 保留、隔离文件 0 个） |
| **S1** 轮转先删最老归档再失败 | 先 `os.remove(.2)`，再挪 `.1`；`.1` 被占用时轮转失败但 `.2` 已没了 | 改成「先把现有归档改名到 `.rot-` 临时名 → 全部到位才删退役份」；任何一步失败调用 `_rollback_archives` 原样放回，最老归档不动 | `test_rotate_failure_keeps_the_oldest_archive`；官方注入 `S1.rotate-archive-locked PASS`（两次都 `rotate=False`、`.2` 被删=False） |
| **S2** `.2` 被占用上限永不生效、无告警 | `os.remove(dst)` 失败直接 return，且不记录 | 同上：无法改名的归档不动索引，失败一律 `_note_rotate_error`（带原始 `PermissionError`） | `test_rotate_when_oldest_archive_is_locked_tells_the_truth`；官方注入输出 `[retention] 日志轮转失败…records.jsonl.2…WinError 32`。备注：文件被别的进程独占时无法凭空删除，占用解除后下一次写即恢复轮转；期间不再静默 |
| **S3** 只读截图删不掉仍扣减 | `except OSError: total -= size` 且已 pop，循环提前结束，上限假达标 | `_remove_cache_file`：先删，只读则 `chmod S_IWRITE` 再删；失败保留在计数里（不扣减）并继续尝试下一个 | `test_prune_converges_even_with_readonly_old_files`：500 张、最旧 450 张只读 → 删后 `left <= 400`（实测收敛） |
| **S7** `load(0)` 归档顺序反了 | 全部读完再整体 `reverse()`，最旧归档排最前 | 每个文件内部反转后按「新文件在前」拼接，与 `load(N)` 一致 | `test_full_load_is_newest_first_across_archives`：`new2,new1,older2,older1,oldest2,oldest1` |
| **写入污染** 孤代理 / NaN | 孤代理让 `json` 输出后 utf-8 编码抛 `UnicodeEncodeError`；NaN 写进 JSONL | `records.add` 写入前 `_invalid_value` 递归校验（孤代理、NaN/Inf、10**400、不可序列化类型），非法行**拒收**并给可读原因（`last_error()` / `stats()["write_error"]`），`json.dumps(allow_nan=False)` 兜底 | `test_poisoned_values_never_reach_the_ledger`：6 种毒值全部返回 `None`、台账 0 字节、原因含「非法」、合法记录照常写入。官方注入 `E7 PASS` / `E7b PASS`（1MB name 正常） |
| **N9/N12** `10**400` 打炸 get/snapshot | `switches._number` 的 `float(value)` 未捕获 `OverflowError` | ① `core/config.py` 加载入口 `_sanitize_tree`：溢出整数 / NaN / Inf / 孤代理递归退回同路径默认值；② `core/switches.py:_number` 补 3 行 `except OverflowError → SwitchValueError`（**唯一一处越界改动，见下**） | `test_huge_numbers_in_config_are_sanitized_at_load`；官方注入 `N9 PASS`（get 返回 50、snapshot 正常）、`N12 PASS`（未抛异常） |

**关于 `core/switches.py` 的一处改动（需你确认）**：N9/N12 的崩点在 `switches._number` 的 `float(10**400)`，注入脚本直接拿裸 dict 调 `switches.get/snapshot`，不经过 `Config.load`，在 `config.py` 里无法拦截。为让这两条真正转绿，只在 `_number` 的 `float()` 上补了 `except OverflowError → SwitchValueError`（3 行 + 注释），其余逻辑未动；`tests/test_switches.py`、`tests/test_server.py` 36 passed，N8/N10 边界用例仍 PASS。若该文件不在授权范围，可回退这 3 行，N9/N12 的配置入口侧净化仍然有效（真实 `Config` 路径已不再抛）。

第三轮纪律：未 commit、未 push；未改 `scheduler.py` / `engine.py` / `server.py` / `scene_guard.py` / `web/index.html` / `tests/` 下既有文件。

