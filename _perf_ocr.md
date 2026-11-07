# OCR 识别速度专项（_perf_ocr.md）

日期：2026-11-07 ｜ 目标仓：`C:\Users\26346\Desktop\project\dafors` ｜ 环境：Windows / Python 3.11.9 / Ryzen 7 7800X3D（8C16T）

> 主仓 `core/` 未改动。全部改造在冻结快照里完成，产出补丁在 `perf/patches/ocr/`。
> 未 commit、未 push。

---

## 0. 快照与隔离

| 项 | 路径 | 说明 |
|---|---|---|
| 冻结快照（权威） | `perf/base/` | 补丁基线。最终核对：`core/market.py` 26301B、`core/engine.py` 170476B |
| 旧副本（**已弃用**） | `perf/ocr/base/` | 测量期早期复制，已落后：`engine.py` 169111B（少 1365B）、`market.py` 25285B（少 1016B）。基于它生成 diff 会把并行会话的改动反向抹掉（实测补丁从 908 行虚增回落到 555 行），见 §8.4 |
| 改造副本 | `perf/ocr/work/` | 所有补丁在此落地、测量 |
| 基准脚本 | `perf/bench/bench_ocr.py` | 子命令 `tiers/item/scan/cpu`，`BENCH_SNAP` 切换快照，`--label` 落 JSON |
| 实验脚本 | `perf/ocr/exp_*.py` | 每个方向一个，结论见 §3 |
| 原始数据 | `perf/ocr/out/*.json` | `baseline.json` / `after_t1.json` / `after_t2.json` / `p_{base,work}_{1,2}.json` |

注意：本机同时有七路分支合并与其它测量在跑，墙钟抖动很大（同一配置两次能差 1.7 倍）。因此：
- 单帧用 **每轮中位数** 再取 **最好一轮**（`best_round`），压掉抢核噪声；
- base→work 用 **同一时间窗交替跑** 的 `p_*` 配对数据，以及 **`process_time()` 的 CPU 毫秒**（不受抢核影响）做终判。

---

## 1. 基线（`perf/ocr/base`，`baseline.json`）

### 1.1 单帧耗时（21 个价格 ROI × 7 轮）

| 层级 | P50 | P95 | max | 最好轮 P50 | 准确率 |
|---|---|---|---|---|---|
| 一级 RapidOCR 原图 | **22.98ms** | 40.85ms | 51.1ms | 17.91ms | 21/21 价格 ROI |
| 二级 PP-OCRv6（3× 放大） | **未参与** | — | — | — | 服务未启动（探活不通，直接跳过） |
| 三级 字模兜底 | **3.67ms** | 8.79ms | 12.29ms | 3.52ms | 12/21（只认纯数字，本就只当兜底） |

基线时二级进程没起，TCP 探活不通，链路直接跳过（`pp.available()==False`）。**这份数字是「探活不通」口径**；
若服务在听但每次推理 500，旧代码会真的打过去并抛异常——那一版的缺陷与代价见 §7。启动服务后复核过：
21 张价格基准仍 `tier2=0`（一级全过，二级根本不触发），p50 12.9ms，0 条读错。

### 1.2 单件商品（`market/detail_panel.png`）

| 类别 | P50 | 最好 | 说明 |
|---|---|---|---|
| 价格区 | 31.21ms | 20.8ms | `read_price`，含闸门与可能的降级 |
| 品阶 | 86.44ms | 77.8ms | `read_text`（基准里 2× 放大裁片） |
| 数量 | 84.93ms | 65.98ms | 同上 |
| 按钮状态 | 37.22ms | 27.44ms | 同上 |
| **单件合计** | **231.81ms** | 219.76ms | P95 261.39 / max 265.87 |

每次单件 **4 次 OCR 调用**（价格 1 次 + 品阶/数量/按钮各 1 次）。

### 1.3 吞吐

| 路径 | 每帧墙钟（P50） | 商品/秒 |
|---|---|---|
| 无状态 `market.scan`（list_unfiltered，6 卡） | 670.51ms | **8.75** |
| 无状态 `market.scan`（list_filtered） | 518.35ms | 11.71 |
| `Scanner` 六卡全变（生产路径最坏情况） | 1170.74ms | **5.16** |

差分跳过在基线就是好的：同帧第二遍 0 次 OCR、6 卡全命中缓存，**不新增改动**，只需在报告里确认它可靠。

### 1.4 CPU

| 指标 | 基线 |
|---|---|
| 单核占用 | **1025.7%（=10.26 核）** |
| CPU 毫秒/帧 | 6324.2ms |
| 墙钟/帧 | 616.6ms |

10.26 核的根因：ORT 默认 `intra_op_num_threads=16`，小 ROI（341×64）上线程自旋比干活还贵。

---

## 2. 补丁（最终只交付一个；1–3 已被守门数据否掉）

> **最终交付**：`perf/patches/ocr/01_tier2_never_dies_silently.patch`（原 04，555 行，6 文件 +303/−43）。
> 下面「补丁 1/2/3」是历史记录：它们在小 ROI 批量口径上有效，但在真实流程（详情面板大图 + 单件端到端）
> 上净效果是**变慢**，已整体移动到 `perf/ocr/rejected_patches/`，不再交付。数据与判定见 §8。

### 交付补丁 — `01_tier2_never_dies_silently.patch`（原 `04_tier2_never_dies_silently.patch`）

- **改了什么**：二级调用统一收口到 `Backend._pp_words()`——异常只计数（`stats["tier2_fail"]`）+ 记错误（`_pp_err`）+ 返回 `None` 继续降级，一级结果不被二级的死拖累；一二级全死才继续上抛（`raise_on_error` 仍能拿到 `OCRError`）。新增启动自检 `ocr.selfcheck_pp()`（真推理一张 32×128 空白图，TCP 通不算数）、状态栏三态显示（`二级推理失败 N 次` / `二级不可用` / `二级已熔断` / `二级已关闭`）、`metrics()` 增加 `tier2_attempts/tier2_fails/tier2_error`。顺带修掉熔断文案套娃（`（连续 3 次）（连续 4 次）…`）。
- **启动自检异步化（响应守门意见 4）**：`engine._run()` 里改成 `threading.Thread(name="ocr-ppcheck", daemon=True).start()`，探活预算 `probe_timeout=0.3s`；主线程不再等自检。实测见 §8.5。
- **依据**：本机 PP-OCRv6 CPU 分支必崩，`paddlex` 端口在听、每次推理 500；只靠 TCP 探活会把这一级显示成「正常」，而旧代码把异常写在 `try` 外 → 被 `ocr.read_words` 边界吞成空串，与「图上真没字」无法区分。详见 §7。
- **实测**：不改稳态速度（21 张价格基准 `tier2=0`，p50 12.9ms 不变）；二级被触发时首次 500 往返 15.1ms，之后熔断挡住（每次 2–12ms）。
- **验证**：`git apply -p1` 于由 `perf/base` 重建的 `_applytest` 全部 cleanly；`verify_patches.py` 8/8 逐字节一致；定向用例 `test_ocr_price/test_ocr_grade/test_ocr_backend/test_market/test_cards` → 76 passed / 88 skipped，仅 1 条环境既有失败（`rapidocr` 未安装，纯净仓同样失败），见 §8.6。

### 历史：~~补丁 1~~ `01_ort_threads_and_session_pool.patch`（已否，归档）

- **改了什么**：`_PARAMS` 固定 `intra_op_num_threads=4 / inter_op_num_threads=1`；单例 `_eng` + 全局 `_lock` 换成 `threading.local()` 每线程一份 session（`_build_lock` 只护建会话）；新增 `warmup()`；`read_words` 不再持锁。
- **依据（线程扫描）**：16 线程 1935ms → 8 线程 643 → **4 线程 398** → 2 线程 601 → 1 线程 993。
- **实测收益**：单帧 P50 `22.98 → 12.91ms`（-44%）；单件最好 `219.76 → 144.48ms`；扫描 `383.14ms`（15.57 商品/s）。

### 历史：~~补丁 2~~ `02_warmup_on_startup.patch`（已否，归档）

- **改了什么**：`Backend.warmup()` / 模块级 `ocr.warmup()` / `engine._run()` 启动时预热一次并打印耗时；失败只告警不阻断。
- **依据**：首帧冷启动 747ms vs 稳态 22ms —— 这一份开销原先全额算在**第一件商品**头上。
- **实测收益**：首件不再含冷启动；稳态单帧不变（预热后残留约 100ms 的首调，已含在上表里）。

### 历史：~~补丁 3~~ `03_parallel_card_scan.patch`（已否，归档）

- **改了什么**：`Scanner` 惰性建 `ThreadPoolExecutor(max_workers=3)`（线程名 `scan-ocr`），只把「变了/没缓存」的卡派给池子；worker 内 ORT 自动降到 2 线程避免超订；抽出 `_read_one`；池子建时捕获 backend 并给每个 worker 预热。
- **依据**：ORT 推理期间释放 GIL，线程级并行真实有效；受控 A/B（每帧在卡片留白处点 24×24 块强制重认）：

  | 配置 | 墙钟 | CPU |
  |---|---|---|
  | threads=4 / parallel=1 | 479.8ms | 2937.5ms |
  | threads=4 / parallel=3（worker=2） | **304.4ms** | **2000.0ms** |
  | parallel=4 | 340.3ms | 上升 |
  | parallel=5 | 342.9ms | 上升 |

- **实测收益**：六卡全变 `1170.74 → 298.6ms`（**3.9×**），20.1 商品/s；每商品 CPU `1041.7 → 330.7 CPU-ms`（-68%）；识别文本逐字节一致（`rows=same`）。

### 历史：~~补丁 4~~ `04_tier2_never_dies_silently.patch` → 改名为**交付补丁**（内容见本节开头）

补丁顺序（历史）：`01 → 02 → 03 → 04`（02 依赖 01 的 `warmup()`，03 依赖 02 的 backend 预热）。**已否**，
其中 02 单独应用时 `Backend.warmup()` 返回 False（依赖 01 加的 `rapid.warmup()`），等于空操作——这是 §8 判定它失败的直接证据之一。

---

## 3. 试过但否掉的方向（按预期收益排序）

| # | 方向 | 实测 | 结论 |
|---|---|---|---|
| 1 | **跳过检测只跑识别**（省一次 det） | 卡底部文本 0/6 命中；价格 `25,000` 被读成 `25,800` | **否**。直接破准确率红线 |
| 2 | **检测参数 `limit_side_len` / `unclip` / `thresh`** | limit 32/48/96、unclip 1.0/1.2、thresh 0.2，价格 42/42、品阶 48/58 均不变；tier1 P50 13.5→14.6ms（thresh 0.2 最好，-4.5%） | **否**。端到端摊薄后 ≈2%，低于 3%；且降 thresh 抬高误检风险 |
| 3 | **前置预处理（`Global.use_preprocess_img=True`）** | 交替两轮：x1 `13.3` vs `13.3`；首轮 `11.1` 是冷启偏差，不可复现 | **否**。无收益 |
| 4 | **ROI 收紧 8%**（少截边框/邻行） | x1 `13.5` vs `13.4`，准确率不变 | **否**。`limit_type=min / limit_side_len=64` 把短边固定抬到 64，裁小 ROI 并不减小网络输入 |
| 5 | **差分跳过** | 已有：同帧第二遍 0 次 OCR、6 卡命中 | **已存在且可靠**，不改 |
| 6 | **更多并行度**（parallel 4/5、worker 4 线程） | 墙钟无改善（340/343ms），CPU 反而升 | **否**。超订 |
| 7 | **模型常驻 / 预热是否计入单件** | 冷启 747ms 全额落在首件 | **已修**（补丁 2） |
| 8 | **三级链触发过宽** | 触发条件是「分数 <0.5 或格式没过」；二级不可用时本就直接跳三级（4ms） | 无需收紧；它是兜底不是常态成本 |

---

## 4. 红线核对

- 准确率：`tests/test_ocr_price.py`、`test_ocr_grade.py`、`test_ocr_backend.py`、`test_market.py`、`test_cards.py` → **165 passed**（含 42/42 区域命中、58/58 品阶、全部价格解析用例；新增 6 条二级崩溃/熔断用例）。
- 未降低任何分辨率、未砍复核、未改 `Det/Rec` 分辨率或阈值。
- 无「识别失败就当默认值」的静默降级：未改动 `_price_tiers` 的拒识语义（读不到仍返回 `None`）；二级死掉时也不会把一级读到的字一起丢掉（见 §7.1）。

---

## 5. 历史对照（含已否的 01–03，仅供追溯；最终交付不再带这些改动）

> 下表是「01+02+03 都在」时的数字，口径是**小 ROI 批量 / 理想空场**，不是真实流程。真实流程的 A/B 见 §8.3：
> 单件端到端 434.71ms（base）→ 433.58ms（两档线程），**−0.26%**，未达 3%。

| 指标 | 基线 | 最终 | 变化 |
|---|---|---|---|
| 单帧 OCR P50（一级） | 22.98ms | 12.9–13.4ms | **-44%** |
| 单件合计 P50 | 231.81ms | 153.0–155.3ms | **-34%** |
| 单件最好轮 | 219.76ms | 146.5ms | **-33%** |
| 无状态扫描 P50 | 650–1184ms | 365–395ms | **≈3×** |
| 生产 `Scanner` 六卡全变 | 1170.7ms | 298.6ms | **3.9×** |
| 商品/秒（生产路径） | 5.16 | **20.13** | **3.9×** |
| 每商品 CPU | 1041.7 CPU-ms | **330.7 CPU-ms** | **-68%** |
| 单核占用（Scanner 路径） | 1020.4%（10.2 核） | 664.5%（6.6 核） | 总 CPU 下降，并行只是换墙钟 |
| 单核占用（无状态路径） | 1055.8% | 605.4% | -43% |

---

## 6. 为什么停在这里

**结论：OCR 这条线收益不成立，停止。** 判据是「单件端到端相对基线 ≥3%」，不是小图帧耗时。

1. **第 3 轮候选（检测参数）**：端到端 ≈2%（<3%），且带误检风险 → 不采纳。
2. **第 4 轮候选（预处理 / ROI）**：0%（<3%）→ 不采纳。
3. **按尺寸分线程（本轮定向重试，守门指定的唯一方向）**：详情面板单帧 205.33 → 204.65ms（−0.3%），
   单件端到端 434.71 → 433.58ms（**−0.26%**，<3%）→ 不采纳。且这是最好的一次；三组重复里最差一组
   候选还比基线慢 0.44ms。数据与曲线见 §8.3。

前几轮的「−44% 单帧 / 3.9× 六卡」全部来自**小 ROI 批量口径**；真实流程里最贵的是详情面板那张大图，
固定等待 click(100ms)×2 + fire_frame 6.76ms + capture 10.85ms ≈ 228ms 已经把可优化空间压到 detail 的 ~205ms，
而大图在并存 session 的现场只回到 ~205ms（见 §8.2 的 4/8/24 线程曲线）。**小图快多少都不算数。**

**剩余空间**：只剩「异步流水线（det/rec 跨卡重叠）」这条，收益估计 10% 量级但需重构 `read_card` 调用边界；
在单件端到端已被固定等待稀释到 -0.26% 的现实下，不再追。降分辨率 / 跳过 det / 换模型都过不了准确率红线。

**另一处已独立解决的非速度问题**：二级 PP-OCRv6 服务在本机 CPU 分支必崩（Paddle 3.x PIR + oneDNN），
它不是速度问题、也不影响以上数字，但它曾经是「静默死亡」的真缺陷。完整结论、代码位置、用例、
触发条件与建议见 §7；交付补丁就是这一件。

---

## 7. 二级（PP-OCRv6）到底怎么了

### 7.1 异常是「捕获继续降级」还是「冒到上层」

**旧代码两种都有，取决于走哪条路**——这正是问题所在：

| 路径 | 旧行为 | 位置 |
|---|---|---|
| 价格链 `_price_tiers` | **捕获并继续**：`try: self.pp.read_words(...) except Exception` → 落三级字模 | `core/ocr_chain.py::Backend._price_tiers`（二级块） |
| 普通文本 `read_words`（一级报错分支） | **冒到上层**：`pp.read_words` 写在 `try` 外，异常一路穿到 `ocr.read_words` | `core/ocr_chain.py::Backend.read_words` |
| 普通文本 `read_words`（一级读空分支） | **同上，冒到上层** | 同上 |

冒到上层后并没有人接：`core/ocr.py::read_words/read_text` 在 `raise_on_error=False` 时把任何异常吞成 `[]`/`""`。
结果是**「二级死了」和「图上真没字」在日志里长得一模一样**——中间层从来没工作过，但没有任何一处会说。
价格链当时看似没事，只是因为它自己捕获了；而唯一覆盖「二级不可用」的用例（`test_tier3_is_the_last_resort`，`pp_boom=True`）
只模拟了 `available()==False`（服务没起），**从来没测过「端口在听、每次推理都抛」**——洞就留在这里。

### 7.2 修成了什么样（补丁 4）

- `ocr_chain.py::Backend._pp_words()` 是二级的唯一入口：异常 → `stats["tier2_fail"] += 1`、`_pp_err = "类型: 信息"`、返回 `None`。
  两条文本路径都改走它：二级失败时**保留一级结果**（只有一级也报错才继续上抛）。
- `ocr_chain.py::Backend.name()` 三态可见：有真实失败 → `二级推理失败 N 次（<错误>）`；启动自检失败 → `二级不可用（…）`；
  熔断 → `二级已熔断（…）`；配置关 → `二级已关闭`；真没起 → `二级服务未起（价格二次确认自动跳过）`。
- `ocr_chain.py::Backend.stats_line()` 带 `tier2=` / `tier2_fail=`；`ocr.metrics()` 带 `tier2_attempts/tier2_fails/tier2_error`。
- `ocr_ppocr.py::Backend.selfcheck()`：真推理一张 32×128 空白图，**TCP 通不算数**，不动熔断状态。
- `engine.py::_run()` 启动后跑一次自检并打印：未启用/不可用分别提示，不再等第一次价格复核失败才发现。
- 顺带修掉熔断文案套娃：熔断期内每被挡一次都会重写 `last_error`，旧写法变成
  `…（连续 3 次）（连续 4 次）（连续 5 次）`；现在统一走 `_trip()`，只留最底层原因。

**新增用例**（`tests/test_ocr_price.py`、`tests/test_ocr_backend.py`）：

1. `test_tier2_listening_but_500_degrades_counts_and_keeps_error` —— 价格链捕获+计数+落三级。
2. `test_tier2_500_on_text_path_does_not_raise_or_lose_tier1` —— 文本路径不抛异常、一级结果不被拖累。
3. `test_tier1_and_tier2_both_dead_still_raises_for_strict_callers` —— 两层全瞎仍上抛（严格调用方拿 `OCRError`）。
4. `test_selfcheck_pp_reports_configured_but_crashing_tier2` —— 启动自检能区分「关掉/没起/在听但在崩」。
5. `test_name_shows_breaker_and_off_instead_of_missing_service` —— 熔断/关闭不许都写成「服务未起」。
6. `test_breaker_message_does_not_nest_on_repeated_blocks` —— 熔断文案不套娃。

### 7.3 实测（服务真的起在 8081，端口通、每次 500）

```
probe: True | name: RapidOCR → PP-OCRv6 tiny（HTTP http://127.0.0.1:8081/ocr） → 字模兜底
selfcheck_pp → (False, 'HTTPError: HTTP Error 500: Internal Server Error')
backend_name → RapidOCR → 二级不可用（HTTPError: HTTP Error 500: Internal Server Error） → 字模兜底
```

代价（`exp_tier2_dead_cost.py`）：

| 场景 | 结果 |
|---|---|
| 单次 500 往返 | 首次 15.1ms（真 HTTP）；之后被短冷却挡下，≈0ms |
| 21 张价格基准（正常链路） | `tier2=0`、p50 12.9ms、0 条读错 —— 二级不触发，崩不崩都不花钱 |
| 强制一级读空以逼出二级 | 第 1 次 121.6ms（500 + 落三级），之后 2–12ms；p50 5.8ms |
| 熔断 | 连续 3 次 → 600s 长冷却，冷却期内每 10s 半开真推理一次；`tier2_fail` 继续累计，状态栏显示「二级已熔断」 |

所以：**21 张价格基准全部一级通过，二级一次都没被调用**，前面所有速度数字与「二级死不死」无关；
只有在「一级读不准/读空」的真实场景里，死掉的二级才会多付一次 500 往返（15ms 级，随后熔断），
不会像旧代码那样把异常冒到文本路径再被吞成空串。

### 7.4 环境还是配置？触发条件与建议

**环境，不是配置写错。** 决定性证据是服务端自己的堆栈：

```
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute not support
[pir::ArrayAttribute<pir::DoubleAttribute>]
(at ..\paddle\fluid\framework\new_executor\instruction\onednn\onednn_instruction.cc:118)
```

- 触发条件：**Paddle 3.x（PIR 新执行器）+ oneDNN CPU 分支**推理，即 `paddlex --serve ... --device cpu`。
  端口能起、模型能加载、TCP 探活全绿，第一次真推理就 500；GPU 路径（`--device gpu:0`）不走这段 oneDNN 指令，不受影响。
- 不是配置问题：`FLAGS_enable_pir_api=0`、`FLAGS_use_mkldnn=0` 都试过，不解决；请求体格式也排除了（422 与 500 是两回事，500 来自服务端推理）。
- 是不是「只在我这台机器崩」：判定标准不是机器，是 **wheel 版本 + CPU 设备**。同一 `paddlex/paddlepaddle` 版本、同样 `--device cpu`
  的部署都会命中；换成支持该 PIR 属性的 Paddle 轮子或走 GPU 才不崩。这台机器只是恰好是 CPU-only 部署，所以必现。
- 结论与建议：**CPU-only 部署上，这一级应默认关闭，并在界面上明确标注「未启用」**，不要留一个「看起来有、实际在崩」的中间层。
  现在代码已经做到「不静默」：默认仍会尝试（有人修好服务就能自动恢复），但启动即自检、失败即计数、状态栏写清原因；
  若确定要默认关掉，配置里设 `ocr.ppocr.mode=off`，状态栏会显示 `二级已关闭（ocr.ppocr.mode=off）`，价格二次确认直接走字模兜底（3.67ms）。

**对前面数字的影响（明确回答「是否在结构不完整的机器上测的」）**：是。基线与最终数字都是「二级不参与」口径，
即实际运行链路是 **一级 → 三级**。这不是隐瞒，而是这台机器上二级本来就跑不起来；补丁 4 做的是让这个事实**可见、可计数、可恢复**，
而不是假装中间层在工作。若二级在别的机器上能跑，它带来的只是「一级不确定时多一次 3× 复核」，与本报告的速度结论正交。

---

## 8. 定向重试（按尺寸分线程）——不成立，停止

守门方给的判据只有一条：**单件端到端相对基线 ≥3%**。小图帧快多少都不算数。

### 8.1 守门实测（否掉 01/02/03 的直接证据）

| 补丁 | 小图批量口径 | 真实流程 | 结论 |
|---|---|---|---|
| 01（ORT 16→4 线程 + 线程局部 session） | 47.4 → 14.7ms（**−69%**），每卡 CPU −56% | 详情面板 198 → **296ms（+48%）**；单件 449 → **553ms（+23%）** | 否 |
| 02（启动 warmup） | —— | 单独应用 `Backend.warmup()` 返回 **False**（依赖 01 加的 `rapid.warmup()`），首件仍 **700.7ms** | 否（空操作） |
| 03（三线程认卡） | 六卡 1170.7 → 298.6ms（3.9×） | 六卡墙钟 330 → **431ms（+30%）**，每卡 CPU +12% | 否 |

`1170.7 → 298.6ms` 是真的，但它测的是小 ROI 批量吞吐；真实流程最贵的一段是详情面板大图。
三个补丁已移到 `perf/ocr/rejected_patches/`，`core/ocr_rapid.py`、`core/market.py` 已回退到 `perf/base`。

### 8.2 两档线程的实测曲线（`exp_size_threads.py` / `exp_size_threads2.py` / `exp_diag_threads.py`）

大图（详情面板 590×860，短边 590）：

| intra_op 线程 | 单帧 |
|---|---|
| 4 | 296.9ms |
| 8 | 197–205ms |
| 10/12/16（有小图 session 并存） | 异常慢（session 重叠、ORT 自旋抢核） |
| 24 | **192–193ms** |
| 28+ | 回退到 215–228ms |

小图（卡片/价格条，短边 64）：

| intra_op 线程 | 单帧 |
|---|---|
| 4 | 13.9–22.6ms |
| 8 | 14.0–18.9ms（与 4 持平） |
| 24 | 14.4–17.1ms（略慢） |

**阈值与两档取值都由上表定，不是拍的**：`big_short_side = 256`（实测两类短边分别在 64 与 590，中间无样本，
取两者几何中位附近）；短边 < 256 → `threads=4`；≥ 256 → `big_threads=24`（16 档实测异常慢，按数据跳过它）。

### 8.3 A/B（真实 `flow` 组，base 与候选交替跑三组）

| 组 | base 单件端到端 | 候选单件端到端 | base 详情面板 | 候选详情面板 |
|---|---|---|---|---|
| 1 | 434.62ms | 433.44ms | 205.33ms | 204.65ms |
| 2 | 434.71ms | 433.58ms | —— | —— |
| 3 | 438.30ms | 438.74ms | —— | —— |
| **中位** | **434.71** | **433.58** | **205.33** | **204.65** |

- 单件端到端中位 **−1.13ms / −0.26%**（第 3 组还倒贴 +0.44ms）；详情面板 **−0.68ms / −0.3%**。
- 原因不是口径：固定等待 click×2 + fire_frame + capture ≈ 228ms 不可压缩，可优化空间只剩 detail 的 ~205ms；
  4 线程把 detail 拖到 296ms（守门数据）后，靠把大图提到 24 线程才回到 205ms —— 也就是「修回基线」，没有净增益。
- **判定：未达 3%，不采纳。** 两档实现已从 `work` 移除（`ocr_rapid.py` 回退 base）。

### 8.4 会话池的内存与句柄代价（真机长期挂机口径，`exp_pool_cost.py`）

| 阶段 | 工作集 | 句柄数 |
|---|---|---|
| 起点（只有解释器 + 依赖） | 35MB | 185 |
| + 小图 session | 131MB（+96） | +123 |
| + 大图 session | 177MB（+47） | +140 |
| + 3 个 worker session | 收盘 180MB / **峰值 328MB** | 333（+148） |

多 session 并存没有持续增长（构建完成后稳定），但峰值 +293MB、句柄 +148。会话池的收益确实只来自「避免重复初始化」；
既然整体方向不成立，会话池不进入交付。若以后单独需要，必须连这笔峰值一起算。

### 8.5 交付补丁的启动自检：1.5s → 异步 + 300ms（`exp_startup_selfcheck.py`）

| 场景 | 结果 |
|---|---|
| 端口没起，旧预算 1.5s | `probe` **1502.9ms**（本机对关闭的本地端口静默丢包，connect 吃满 timeout —— 守门看到的 1.5s 就是这个） |
| 端口没起，新预算 0.3s | `probe` **300.3ms** |
| `selfcheck` 完整路径（端口没起） | **300.4ms**，返回「二级服务未起（300ms 内探活不通）」 |
| 端口在听但每次 500 | `selfcheck` **112.7ms** 返回 `HTTPError: HTTP Error 500`；同一时刻 `available()=True`（所以必须真推理） |
| 异步块 | `Thread.start()` **0.2ms**，主线程立即继续初始化；自检 1500.7ms 在 `ocr-ppcheck` 守护线程里跑完，结果再进状态栏 |

### 8.6 交付物与验证状态

- 补丁：`perf/patches/ocr/01_tier2_never_dies_silently.patch`（555 行；`core/engine.py +18/−0`、`core/ocr.py +32/−6`、
  `core/ocr_chain.py +96/−20`、`core/ocr_ppocr.py +44/−12`、`tests/test_ocr_price.py +78/−5`、`tests/test_ocr_backend.py +12/−0`）。
- 基线修正：生成补丁的基线从 `perf/ocr/base` 改为 **`perf/base`**。旧副本 `engine.py` 少 1365B、`market.py` 少 1016B，
  基于它生成的 diff 会把并行会话的改动反向抹掉（补丁虚增到 908 行）；换回 `perf/base` 后 555 行，且 `engine.py` 只剩 +18 行异步块。
- 验证：由 `perf/base` 重建 `_applytest` → `git apply -p1` 六文件 **cleanly**；`verify_patches.py` **8/8 一致**
  （含 `core/market.py`、`core/ocr_rapid.py` 与基线逐字节相同，即无实验残留）。
- 用例：`tests/test_ocr_price.py test_ocr_grade.py test_ocr_backend.py test_market.py test_cards.py`
  → **76 passed / 88 skipped / 1 failed**；唯一失败是 `test_local_models_are_preferred_and_cold_start_is_small`
  （`ModuleNotFoundError: No module named 'rapidocr'`，本机环境未装包；在纯净仓 `tests/` 与 `perf/base` 上同样失败，与本补丁无关）。
  `test_cooldown_lets_go_once_the_service_recovers` 整组跑两次均通过。
- 未 commit、未 push；未开可见窗口、未动真实光标（全部为进程内推理与本地 socket 测量）。
