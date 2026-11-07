# 并行工作树规矩（所有会话必读）

主树只有一份。十几路会话同时写它，代价已经实测出来：

| 现象 | 实测 |
|---|---|
| `core_digest` 同一天三个值 | `7e7af994` → `540f063bb589857a` → `17ddac70b83e9895`（19:47）→ `d0dd4c77af3e2f6b`（19:50） |
| 两个会话同一分钟写 `core/input.py` | 109997 → 121094 → 121177 字节 |
| 并发跑 pytest 的假失败 | `lag 8.202ms`、`segment_preheat 37.174ms`、子进程判死、Playwright 超时；空载全绿 |

结论：**主树是合并目标，不是工作区。** 并行会话在 `..\dafors-wt\<name>` 里改，走合并闸门回主树。

---

## 1. 三种树，三种职责

| 角色 | 位置 | 允许的操作 |
|---|---|---|
| 主树 | `C:\Users\26346\Desktop\project\dafors` | 只读分析；闸门写入；单文件小改（见 §3） |
| 工作树 | `..\dafors-wt\<name>` | 随便改、随便跑；自带 `SNAPSHOT.txt` 锚定来源指纹 |
| 时间线 / 日志 | 主树 `_tree_timeline.jsonl`、`_merge_log.jsonl` | 只追加，不入库 |

工具（都在 `tools/`，只读业务代码，不改 `core/`、`engine.py`、`web/`、`installer/`）：

- `tree_digest.py`：算指纹、打时间线、解释两次指纹之间的文件差异
- `make_worktree.ps1`：从主树当前状态（含未提交改动）复制一份快照
- `merge_gate.py`：把快照的改动按文件/hunk 合回主树，冲突拒绝覆盖

---

## 2. 什么时候必须开 worktree

命中任意一条就开，不要犹豫：

1. 要改 **2 个及以上文件**；
2. 要动 `core/`、`engine.py`、`web/`、`installer/`、`tests/`（`tests/` 另有跨进程互斥线在改，更要隔离）；
3. 预计连续工作 **超过 10 分钟**；
4. 要跑 pytest、性能台架、任何时序敏感测量；
5. 要引用 **性能数字** 写报告或结论；
6. 不确定会不会和别人撞文件。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_worktree.ps1 -Name wt-<你的主题>
cd ..\dafors-wt\wt-<你的主题>
```

快照会打印 `snapshot_core_digest`；若和 `source_core_digest` 不一致，说明复制期间主树正被写，重跑一次。`SNAPSHOT.txt` 里同时记录了来源 git HEAD、dirty 文件数、创建时刻——报告里引用快照时以它为准。

---

## 3. 什么改动允许直接在主树做

只有这些，且必须留痕：

1. **只读分析**：读代码、跑 `tree_digest.py`、看日志。禁止 `touch`、禁止改 mtime。
2. **单文件小改**（≤20 行、不跨模块、不需要跑测试）：开工前 `tree_digest.py --record`，收工再 `--record`，确认只动了你声明的那一个文件。
   ```powershell
   .\.venv\Scripts\python.exe tools\tree_digest.py --record
   # ...改那一个文件...
   .\.venv\Scripts\python.exe tools\tree_digest.py --explain <OLD> <NEW>
   ```
   如果 `--explain` 列出别人的文件，说明你踩线了，停下来改用 worktree。
3. **新增独立文件**：新文档、`tools/` 下新工具（路径不与任何人重叠）。

不允许直接在主树：改 `core/`、跑耗时测试、调参、批量替换、格式化全仓库。

---

## 4. 引用性能数字的写法（强制）

任何性能数字后面必须跟三元组 **`数值（快照指纹，测量条件）`**，缺一不可：

```
lag 8.202ms（core_digest=d0dd4c77af3e2f6b，快照 wt-a，空载 i5-12400，n=30，2026-11-07 20:05）
```

- **快照指纹**：写 `core_digest=<16 位>`；数字来自 worktree 就写该 worktree 的指纹与名字，来自主树就写 `主树-非冻结`。
- **测量条件**：载重（空载/OCR 常驻/并发 N 路）、样本量、机时。
- 主树上的读数只允许当“现场观察”，不允许当基线、不允许写进对比表。
- 同一张表里的数字必须同指纹同载重，否则分表。

原因：`lag 8.202ms` 和空载全绿并不矛盾，它们根本不是同一棵树、同一载重下的数。

---

## 5. 并发跑测试的约束

1. **同一时刻只允许一个进程跑性能/时序台架**（`perf/bench`、`tools/bench_*`、`lag`/`segment_preheat` 类用例）。要并行就开 worktree，但性能测量仍然全局串行，排队。
2. pytest 统一加 `-p no:cacheprovider`，避免多会话争 `.pytest_cache`：
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider <路径>
   ```
3. 功能测试可以在不同 worktree 并行；时序敏感用例（子进程存活、Playwright 窗口、倒计时）只在单路空载下跑。
4. 看到 `lag`/`segment_preheat` 超阈值、子进程判死、Playwright 超时，**先查是否有人并发跑测试**：`Get-Process python*`，再看时间线里 `core_digest` 是否在你跑的过程中变过。变了就作废重跑，不要把失败归给代码。
5. 不要为了让用例变绿去放宽阈值；先排除环境并发。

---

## 6. 标准闭环

```powershell
# 1) 开工：取快照
powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_worktree.ps1 -Name wt-input

# 2) 在快照里工作，改前打点
cd ..\dafors-wt\wt-input
C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe tools\tree_digest.py --record

# 3) 需要回答「这个指纹在哪个时间段存在」时挂 watch
C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe tools\tree_digest.py --watch --interval 10

# 4) 收工：先 dry-run 看闸门打算做什么
cd C:\Users\26346\Desktop\project\dafors
.\.venv\Scripts\python.exe tools\merge_gate.py --from ..\dafors-wt\wt-input --verbose

# 5) 无冲突再实写
.\.venv\Scripts\python.exe tools\merge_gate.py --from ..\dafors-wt\wt-input --apply --operator <你的会话名>
```

闸门行为：

- 只把 **你真的改过** 的文件当作候选（基线来自 `SNAPSHOT.files.json`）；
- 主树没动过 → 直接采用你的版本；
- 主树也动过 → `git merge-file` 三路合并，成功才写；
- 合并失败 / 新增同名不同内容 / 两边都改二进制 → **整文件拒绝**，主树保持原样，退出码 2，冲突单独列出；
- 每次运行（含 dry-run）追加一条 `_merge_log.jsonl`：操作者、时间、来源 worktree、文件、hunk 数、前后 `core_digest`、冲突列表；
- `git worktree` 模式下基线默认是 HEAD 提交内容，同样适用。

**冲突处理**：不要手工覆盖。要么在 worktree 里把主树的新改动合进来（`git merge`/手工对齐）再重跑闸门，要么另开一个基于最新主树的快照重做这一段。冲突文件只允许人工裁决。

---

## 7. 指纹与时间线的用法

```powershell
# 当前值（与 perf/bench/common.py::core_digest 同算法）
.\.venv\Scripts\python.exe tools\tree_digest.py

# 每个 core 文件的大小/哈希/mtime
.\.venv\Scripts\python.exe tools\tree_digest.py --files

# 某两个指纹之间到底哪些文件变了、变了几字节
.\.venv\Scripts\python.exe tools\tree_digest.py --explain 540f063bb589857a 17ddac70b83e9895
```

- 时间线只在 `--record` / `--watch` 时增长；`--explain` 依赖时间线里的逐文件快照，**改代码前先 `--record`**，否则只能解释“有记录的那一段”。
- `--scope all` 把同一算法扩到全部源码（排除 `.venv/build/dist/perf 大目录/日志/快照元数据`），用来判断两棵树的内容是否收敛；默认 `core` 与台架口径完全一致。
- `--watch --only-changes` 只在指纹变化时落盘；终端仍会逐次打印，方便肉眼盯。

---

## 8. 禁止事项

1. 不 commit、不 push（本仓的并行期约定）。
2. 不删别人的 worktree，不在别人的 worktree 里改文件。
3. 不在 worktree 里改 `.git`、不 `git worktree remove` 别人的目录。
4. 不把 `_tree_timeline.jsonl`、`_merge_log.jsonl`、`SNAPSHOT*`、`_snapshot_base/` 提交入库（`.gitignore` 已覆盖前两者与 `dafors-wt/`）。
5. 不把主树的实时读数当基线；不在主树跑耗时测试。
6. 不为了让数字好看而挑树：报告里必须写清指纹与载重。
