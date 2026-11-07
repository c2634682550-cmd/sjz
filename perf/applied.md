# 补丁应用台账

守门流程（每个补丁单独走一遍，禁止打包）：

1. 补丁应用前：在干净快照上跑 `python perf/bench/run.py --snapshot . --with-redlines`，基线以 `perf/bench/baseline.json` 为准；
2. 应用补丁（只应用一个），复跑同一命令，结果落到 `perf/bench/out/<时间戳>/perf.json`；
3. `python perf/bench/gate.py --base perf/bench/baseline.json --cand <结果> --redlines <旁证>`：
   - 退出码 0：放行；
   - 退出码 1：任一红线退化 → **立即回滚**，不看收益；
   - 退出码 2：红线证据缺失 → 视为阻断，补齐后再判；
4. 放行的补丁更新基线（`--freeze-baseline`），并在下表记一行；回滚的补丁也要记，写清退化项。

## 补丁来源基线（强制）

- **唯一允许的补丁来源基线：`perf/base`**，core digest `cf532c70c48dfa8c`。
- **禁止**再从旧 `perf/ocr/base` 生成补丁：它是过期副本（`core/engine.py` 少 1365 字节、`core/market.py` 少 1016 字节），从它发补丁会反向抹掉并行会话的改动、虚增约 **908 行**。
- 任何新补丁的说明/提交信息里必须写明：`base=perf/base @ cf532c70`。旧 OCR 01/02/03 已归档到 `perf/ocr/rejected_patches/`，不再参与队列。
- 例外（仅本轮复验）：`flow/01b`、`flow/07` 按要求以**合并后的树** `perf/merged` 为来源（digest `1582096906c8`，= `core/engine.py` + `core/fire_proc.py` + `tools/fire_worker.py` 拼接哈希）。二者已在 `perf/merged` 里落地（`git apply -R --check` exit 0）。它们若最终要进主仓，仍需按 `perf/base` 重新出补丁，不得引用 `perf/merged` 作为基线。

## 待应用队列（等七路功能合并完成后统一进主仓）

**顺序敏感，不能打乱**；队列内 `core/engine.py` 有重叠（不同函数），合并时人工复核。

| 顺序 | 补丁 | 状态 | 入队条件 |
| --- | --- | --- | --- |
| Q1 | `ocr/01_tier2_never_dies_silently.patch` | **已通过，待应用** | 来源必须 `perf/base`；与 flow 的 `engine.py` 改动在不同函数，合并后复核 |
| Q2 | `flow/02_bought_dialog_order.patch` | **已通过，待应用** | 无 |
| Q3 | `flow/03_verify_grace.patch` | **已通过，待应用** | 必须在 02 之后 |
| Q4 | `flow/05_close_reward_wait.patch` | **已通过，待应用** | 必须在 03 之后 |
| Q5 | `flow/06_scan_memo.patch` | **已通过，待应用** | 必须在 05 之后 |
| — | `flow/01_detail_wait.patch` | **退回，待重出** | 上下文过期，按 `perf/base` rebase 后重跑 `flow_gate` |
| — | `flow/01b-detail-conditional-wait.patch`（新版，17688 B，换页证据） | **退回，这条线放弃** | 硬项②未命中 200 次 max **776.71 / 799.22ms**（5–7 次 ≥600ms）；硬项①「空帧→回退旧编号」场景 97–99% 读到上一件编号并读旧钟；台架单件 **−0.57%**（<3%）。详见 `_perf_baseline.md` §5.4 |
| — | `flow/07-fireproc-ready-handshake.patch` | **正确性单列，不进性能队列，显示点待功能性改动** | 三种失败快速死/原因可读/无重试/无孤儿全过；单件 0.00%；`core/` 无调用点、应用层无显示对象（全树仅 `core/fire_proc.py`、`tests/`、`tools/stability/faults.py` 引用） |
| — | `flow/04_guard_prefilter.patch` | **退回（砍）** | 真实整屏「余额不足」素材命中 0/1，漏判不能证明为 0 |
| — | `ocr/01_ort_threads_and_session_pool.patch` | **退回（已归档）** | 小图 −69% 但详情面板 +48%、单件 +23%；需改按图像面积自适应线程 |
| — | `ocr/02_warmup_on_startup.patch` | **退回（已归档）** | 单独应用是静默空操作（`warmup()` 返回 False，首件仍 700ms） |
| — | `ocr/03_parallel_card_scan.patch` | **退回（已归档）** | 六卡墙钟 +30%、CPU +12%；并发收益被抢核吃掉 |

**文件重叠（队列内）**：

- `core/engine.py`：`ocr/01` 新增异步自检块（`_run` 内 +18 行）与 flow-02/03/05（`_fire_and_confirm` / `_bought_dialog` / `_close_reward`）——函数级不重叠，同文件多 hunk，按 Q1→Q5 顺序合并后人工复核。
- 旧 OCR-03 已归档，`core/market.py` 在队列内只剩 `flow/06`，不再有同区冲突。

## 已执行的守门记录

| 日期 | 补丁 | 变更摘要 | 来源基线 | 红线结论 | 关键指标变化 | 处理 |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-07 | ocr/01（旧） | ORT 16→4 线程 + 线程局部 session + warmup | `perf/ocr/base`（旧，作废） | 全过（price 1.0 / grade 0.8621 / 183 passed） | 小 ROI 43→37ms；详情面板 198→296（+48%）；单件 449→553（+23%）；CPU/卡 868→379 | **退回，归档** |
| 2026-11-07 | ocr/02（旧） | 启动预热 | `perf/ocr/base`（旧，作废） | 全过 | 依赖 01 的 `rapid.warmup()`；单独应用 `warmup=False`、首件 700ms | **退回，归档** |
| 2026-11-07 | ocr/03（旧） | Scanner 三线程认卡 | `perf/ocr/base`（旧，作废） | 全过 | 六卡墙钟 330→431（+30%）；CPU/卡 +12%；单帧 p99 未放大 | **退回，归档** |
| 2026-11-07 | ocr/04（旧版） | 二级不静默 + 自检 + 三态 | `perf/ocr/base`（旧，作废） | 全过（183 passed vs base 177） | 稳态 ±10% 内、item_total +0.9%；启动 852→2399（+1.5s TCP 探活） | **有条件通过 → 已由新 ocr/01 取代** |
| 2026-11-07 | **ocr/01（新）** | 二级不静默：探活 1.5s→0.3s、自检异步 daemon、三态文案 | **`perf/base` @ `cf532c70`** | 全过（183 passed；误买+身份 114 passed / 1 skipped） | 启动串行最坏 +295~309ms（真实路径 +0.2ms）；不可达 302.5ms 返回「未起」；500→「不可用」→3 次→「已熔断」；正常→`PP-OCRv6 tiny`；off 0.0ms「已关闭」；自检中退进程 455ms 不拖住；成功路径 ±5% 内 | **通过，入队 Q1** |
| 2026-11-07 | flow/01 | 进详情条件等待 | `base_0930` @ 5eef554 | — | 补丁上下文过期，打不上 | **退回（rebase）** |
| 2026-11-07 | flow/02 | 弹窗判据调序 | `base_0930` @ 5eef554 | 全过 | 读区 2→1，四种组合判定不变 | **通过** |
| 2026-11-07 | flow/03 | 校验前紧后松 | `base_0930` @ 5eef554 | 全过 | gaps 合计 1500ms / 6 拍 / 首检立即 | **通过** |
| 2026-11-07 | flow/04 | 守卫预筛 | `base_0930` @ 5eef554 | **不通过** | 真实弹窗命中 0/1，误判 0/5；漏判不能证明为 0 | **退回（砍）** |
| 2026-11-07 | flow/05 | 收奖励条件等待 | `base_0930` @ 5eef554 | 全过 | 上界 2.0/2.5s；永不出现 2013.6ms 封顶不卡死；快速路径 166ms | **通过** |
| 2026-11-07 | flow/06 | 帧判等回缓存 | `base_0930` @ 5eef554 | 全过 | 同帧 422.6→0.16ms；scan_list 10.57→0.21ms；逐字节判等，语义翻转不误判 | **通过** |
| 2026-11-07 | flow/01b（旧版） | 进详情条件等待（读编号即就绪） | `perf/merged` @ `1582096906c8` | 13 passed | 真树判据读取 p50 47.46 / p95 55.06；编号一致性 10/10 不一致（复用面板模型）；未命中名义 550.39≈老 550.28，但 1.5s 探活把上界破到 1728–1796ms；单件 4392→3905（−11.08%） | **退回**（已被新版取代） |
| 2026-11-07 | **flow/01b（新版）** | 换页证据 + `_read_serial_fast`（只走一级、80ms 预算、三态） | `perf/merged` 去旧 01b（`m0`），打后 `m2` | 16 passed（`test_perf_detail_wait.py`） | 编号一致：旧→新 / 空→新 / 双空→新 全 0 不一致；**空帧→回退旧编号 99%/97% 不一致且读旧钟**；未命中 200 次（空帧）550.33/565.7/**776.71**、点开无效 550.42/588.76/**635.18**、尾部回退 **799.22**，`over600` 5–7/200；`pp.available()` 循环内 **0 次**；单次判据读 p50 59.63 / p95 82.74 / max 105.29（>80ms 占 7.5%）；台架基线 entry **607.71 未变**，m2 entry 608.70，`fire_all` 3784.41→3762.98（**−0.57%**） | **退回，这条线放弃**：硬项②上界破 600；硬项①回退场景误判 |
| 2026-11-07 | flow/07 | 卡点 READY 握手 | **`perf/merged` @ `1582096906c8`** | 13 passed | 三种失败 28.93 / 29.24 / 505.25ms 快速死，原因可读；无静默重试；父进程 `os._exit` 后 300ms 内无孤儿子进程；启动 22.21ms（老 50ms，−27.8ms）；单件 **0.00%**；`core/` 无调用点、应用层无显示对象 | **正确性单列，不进性能队列，显示点待功能性改动** |

> 备注：`perf/base` 自带测试桩 `FakeMouse.click(x, y, expect_foreground)` 缺 `hold` 参数，`test_engine_insufficient.py` 的 2 条误买用例在 base 上即失败；复验时只在**快照内**给桩加 `**kw`，产品代码未动，随后 114 passed / 1 skipped。该桩缺陷需在主仓测试里单独修。

> 判据版本：`perf/bench/criteria.json` 的 `version`。判据只能补充指标/下界/证据来源，不允许放宽阈值；改动判据必须在提交说明里写清「为什么这是完善而不是放宽」。
