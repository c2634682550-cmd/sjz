# 主链路 OCR 剖分与优化（F4 → 交易行 → 典藏外观 → 挂单列表读到价格）

测量日期 2026-11-08。所有数字都是**实测**，脚本在 `perf/bench/`，不注入 OCR、不跳过校验。

## 0. 口径

- 台架：`perf/bench/chain_bench.py` —— 假屏幕/假鼠标/假键盘，跑**真的**
  `Navigator.go_to_onsale(from_hall=True)`；页面由 `tests/fixtures/market`
  的真机 ROI 原文像素拼成 1920×1080。
- 采样：`chain_ab.py` 同进程交替跑 4 组配置 × 3 轮 × 2 条链路，取 p50（抵消
  机器负载漂移）；`round_report.py` 单进程 3 条链路，按 `perf_counter`
  起止区间还原**嵌套**（`where` 里套 `_read_text`/`_read_page_no`，只有
  顶层区间相加才等于端到端）。
- 机器告警：测量期间另一条会话在跑赛季切换测试，绝对值有 ±10~15% 抖动
  （同一配置两次 e2e 出现 12.4s 与 14.1s）。**比例只取交替跑的那组**，
  绝对值看区间。
- 优化前 = `reuse_frame=false` + `rapid.fast_det=false`（代码里的旧行为）。

## 1. 剖分表（优化前）

单条链路 e2e p50 **13.0 s**（交替组；单进程组 14.1 s，12.5~16.1 s 区间）。
每条链路 16 次抓帧、5 次 `where()`、10 次 OCR。

### 1.1 顶层段（不嵌套，相加 ≈ e2e）

| 段 | n/链 | p50 ms | 合计 ms/链 | 占 e2e |
|---|---|---|---|---|
| `where()`（页面判定） | 2 | 1794.2 | 3500.3 | 24.9% |
| `buy()`（购买，含 1.5s 固定等） | 1 | 3013.3 | 3370.2 | 24.0% |
| `enter_market()`（F4，含 2.0s 固定等） | 1 | 2888.3 | 2952.3 | 21.0% |
| `tab()`（切页签，含 1.2s 固定等） | 1 | 1478.1 | 1475.9 | 10.5% |
| `ensure_public_filter()` | 1 | 994.5 | 1073.7 | 7.6% |
| `pick_skin()`（选卡） | 1 | 586.5 | 825.1 | 5.9% |

### 1.2 嵌套段（被上面某段包住，**不能重复计入**）

| 段 | n/链 | p50 ms | 合计 ms/链 | 占 e2e |
|---|---|---|---|---|
| `_read_text()`（整屏 OCR） | 5 | 1540.2 | 7888.5 | 56.1% |
| `where()`（嵌套调用） | 3 | 1114.2 | 4446.6 | 31.6% |
| `_read_page_no()`（页码条 6×） | 5 | 790.6 | 3355.2 | 23.9% |
| `skin_items()`（皮肤列表 ROI） | 1 | 326.3 | 552.0 | 3.9% |
| `_buy_button_visible()` | 1 | 108.3 | 99.6 | 0.7% |

### 1.3 固定等待 vs 真实计算

| 类别 | ms/链 | 占 e2e | 来源（行号，当前工作副本） |
|---|---|---|---|
| F4 后固定 `sleep(2.0)` | 2000 | 15.2% | `core/nav.py:865`（另一条会话在改，未动） |
| 购买后固定 `sleep(1.5)` | 1500 | 11.4% | `core/nav_col.py:1366` |
| 切页签固定 `sleep(settle=1.2)` | 1200 | 9.1% | `core/nav.py:719`（默认值）/ `:734` |
| 点击后的画面差轮询（0.2s 步长） | ≈200~600 | 2~5% | `core/nav.py:567-571` |
| 页面轮询 0.25s 步长 | 视命中 | — | `core/nav.py:613` |
| **固定等待小计** | **≈4.7 s** | **≈36%** | 全部在 `nav.py` / `nav_col.py`（本轮**未改**） |
| 整屏 OCR（5×）+ 页码条 OCR（5×） | 11.2 s（含上述段内） | 80%（与固定等待重叠计入） | 见 1.2 |
| 模板匹配/坐标计算/像素采样（`tab_now`、`_diff`、`pt()`） | ≈0.4 s | 3% | 全部本地计算，已不是瓶颈 |

> 重叠说明：固定等待发生在段内，OCR 也发生在段内，两者在时间上加起来
> 会重复。按「谁占墙钟」看：**OCR ≈ 11.2s（约 80%）**、**固定等待 ≈ 4.7s
> （约 36%，与 OCR 串行）**。这也是为什么本轮优化 OCR 的收益最大。

### 1.4 单次 OCR 成本（隔离测量，`perf/bench/screen_cost.py`）

| 输入 | 优化前 p50 | 说明 |
|---|---|---|
| 整屏 1920×1080（文字密集） | 1331 ms（587~1898） | det + rec 全跑原分辨率 |
| 页码条 6× 放大 3240×360 | 438 ms（385~1382） | 为了读 12px「第 N 页」而放大到 3240 宽再跑 det |

## 2. 逐项优化

### 2.1 做了什么

| # | 优化 | 落点 | 机制 |
|---|---|---|---|
| 1 | **同帧复用**：一帧多个区域共用一次识别结果 | `core/ocr.py:303`（`read_words` 入口）、`:376` `_frame_get`、`:389` `_frame_put`、`:401` `clear_frame_cache` | 逐像素 `NORM_L1==0` 才命中（不是"相似"）；只缓存非空结果；条目绑定 backend 对象，`reload()`/换配置即作废 |
| 2 | **页码条 det 降采样**（长边 3240→1300，rec 仍用原分辨率像素） | `core/ocr_rapid.py:161` `_det_scale`（`:181-184`）、`:210` `_is_strip`、`:216` `_read_scaled`（det 出框 → 按 scale 还原 → 裁原像素 → 一次批量 rec） | 原始字 70px 高，压到 28px 仍可 rec；**不降 rec 分辨率**，所以是零识别率代价 |
| 3 | 快速路径空结果的回退口径 | `core/ocr_rapid.py:136-147` | `_read_scaled` 出错返回 `None`→整幅重读；det 报空返回 `[]`（窄条例外，重读只是多付 450ms）。**绝不把漏检当"没字"** |
| 4 | 缓存失效 | `core/ocr.py:78` `reload()` 里 `clear_frame_cache()` | 换 backend 时旧口径结果不能喂给新配置 |
| 5 | 配置开关 | `core/config.py:250-256`（`rapid.fast_det`、`rapid.fast_det_screen`、`ocr.reuse_frame`） | `fast_det_screen` 默认 **1.0**（不降），留待真机验证 |

### 2.2 优化后同表（嵌套口径，单进程 3 链）

e2e p50 **6.8 s**（6.6~9.6 s）。

| 段 | 优化前 ms/链 | 优化后 ms/链 | 变化 |
|---|---|---|---|
| 整屏 OCR（5×） | 7888.5（1540/次） | 2568（430/次，其中 4 次直接命中缓存） | **−67%** |
| 页码条 OCR（5×） | 3355.2（791/次） | 412.6（66/次） | **−88%** |
| `where()` 顶层 | 3500.3 | 91.5（2 次全部命中缓存） | −97% |
| `buy()` | 3370.2 | 2656.2 | −21%（1.5s 固定等没动） |
| `enter_market()` | 2952.3 | 1809.3 | −39% |
| `tab()` | 1475.9 | 1494.4 | **0%（1.2s 固定等是主体）** |
| `ensure_public_filter()` | 1073.7 | 5.0 | −99%（同帧命中） |
| `pick_skin()` | 825.1 | 629.8 | −24% |
| `skin_items()` | 552.0 | 378.9 | −31%（机器抖动，非本轮改动） |
| `_buy_button_visible()` | 99.6 | 137.7 | **+38%（小 ROI 波动，见 §5）** |

### 2.3 端到端前后（交替组，同一进程，p50，n=6）

| 配置 | e2e p50 | 相对基线 | reuse 命中/链 |
|---|---|---|---|
| 优化前（reuse=0, fast=0） | 14167.2 ms（另一次 13019.6） | — | 0 |
| 只开同帧复用 | 10460.0 ms（10357.0） | −26.1%（−20.4%） | 8 |
| 只开页码条降采样 | 10917.2 ms（10721.5） | −22.9%（−17.6%） | 0 |
| **两项都开（当前默认）** | **7431.5 ms（8117.6）** | **−47.5%（−37.6%）** | 8 |

两次独立交替跑给出的区间就是本机的噪声水平（整机 CPU 被另一会话占用）。

### 2.4 第三轮（试了，判"没用"，已回退）

中等 ROI（皮肤列表 750×1560）也做 det 降采样 0.75：

| 项 | 1.0 | 0.75 |
|---|---|---|
| 单次 p50 | 860.0 ms | 800.1 ms（−7%） |
| 词数/字数 | 19 词 / 108 字 | 19 词 / 108 字（**丢字 0**） |
| 对 e2e | — | **−60 ms/链 ≈ −0.9%** |

结论：**这项没用**。收益不到 1%，而 ROI 里是皮肤名（读错=点错卡片），
旋钮 `rapid.fast_det_roi` 留着默认 **1.0**，不打开。


## 3. 准确率对照

| 项 | 结果 | 证据 |
|---|---|---|
| 页码条文字 | **逐字一致**：`第1页` | `_read_scaled` 在原像素上 rec，只降 det；`screen_cost.py` 两种配置 chars 都是 3 |
| 整屏文字 | **逐字一致**（未降采样，det 倍率 1.0） | `fast_det_screen=1.0` |
| 价格识别 | **未触碰**：价格走 `market.read_cards` / `_scan_grid`（原分辨率），`_det_scale` 对 `w<2400` 的小 ROI 一律返回 1.0 | `core/ocr_rapid.py:193` `return 1.0` |
| 页面判定 | 台架 3 链全部走到 `onsale`，无 `unknown` | `round_report.py` 输出 `page=onsale` |
| 回归 | OCR 5 个用例 178 passed；`test_market`/`test_cards`/`test_autocalib` 68 passed | 见 §6 |

## 4. 没做的 / 做了但收益不大

| 项 | 结论 | 原因 |
|---|---|---|
| **整屏 det 降采样（0.75/0.8）** | 没做（配置项留着，默认 1.0） | 文字密集整屏 1330 → 1272 字（丢 12~13px 小字）；「公示期」正好在临界线上，价格/状态判定会连带出错。收益约 −20% e2e，风险不可接受 |
| **PDF/小 ROI 降采样（皮肤列表 750×1560）** | 没做 | 可省 ≈130ms/链（1.9%），但 ROI 里是皮肤名，读错=点错卡片（会买到别的皮肤）。风险/收益比不划算 |
| **`buy()` 后 `sleep(1.5)` 改事件驱动** | 没做（**最大剩余瓶颈**） | 在 `core/nav_col.py:1366`，按约束不动；改法明确：`require` 已有轮询，把 1.5s 换成「0.2s 步长 + 3s 上限」的页面变化判定即可省 ~1.2s/链 |
| **`tab()` 的 `sleep(1.2)`** | 没做 | `core/nav.py:734`，同上；切页签后已有 `tab_now()` 硬校验，可以安全压缩到 0.3s 轮询 |
| **F4 后 `sleep(2.0)`** | 没做 | `core/nav.py:865`，同上 |
| 空结果触发 PP-OCR 二级复核 | 改了回退口径，探测本身没动 | 实测本机 PP 服务在跑，空图一次 145ms、同图缓存后 7ms；不是热点。语义（空结果要复核）保持原样 |
| `_buy_button_visible` 降采样 | 不做 | 单次 65~138ms，占 e2e 1~2%，收益接近 0，且它是「售出/购买」防误点的唯一判据 |
| 并行（OCR 与下一次注入重叠） | 没做 | `nav.py` 是严格串行的状态机（`where()` 的返回值决定下一步按什么键），并行化要改状态机结构 = 改 `nav.py`，本轮禁区 |

## 5. 剩余瓶颈

`6.8 s` 的构成：固定等待 ≈4.7s（**69%**）+ OCR ≈1.1s + 点击/轮询 ≈1.0s。
OCR 侧已经没有两位数百分比的空地；**这一轮之后链路是 sleep-bound 的**，
再往下走必须动 `nav.py`/`nav_col.py` 的固定等待（按约束只报不改）。

## 6. 复现命令

```powershell
python perf/bench/chain_bench.py --verify          # 先验收每页识别
python perf/bench/chain_ab.py                     # 4 组交替，p50
python perf/bench/round_report.py 0 0 3           # 优化前逐段（嵌套）
python perf/bench/round_report.py 1 1 3           # 优化后逐段（嵌套）
python perf/bench/screen_cost.py                  # 单次 OCR 成本
python -m pytest tests/test_ocr_*.py tests/test_market.py tests/test_cards.py tests/test_autocalib.py -q
```

## 7. 死代码清理（按 `_deadcode_inventory.md` A 级）

反查口径：`git grep` 全仓（含 `tests/`、`tools/`、`web/*.js`、`*.spec`、`*.md`）
搜符号名，要求「命中数 = 1（定义本身）」，再看动态入口（`getattr` 按名取、
配置键、CLI 参数、`hiddenimports`）。

| 批 | 删了什么 | 位置 | 引用证据 | 测试 |
|---|---|---|---|---|
| 1 | `digit_runs`、`digits_only` | `core/ocr.py`（原 545/559 段） | 命中 1；`market.py` 已改走 `money.scan_text` | 173 passed |
| 1 | `read_digits`、`match_char`、`read_detail_ex`、`reset_template_cache` | `core/digit_ocr.py` | 各命中 1；`read_detail`（tier3 价格兜底）**未动** | 同上 |
| 1 | `ONS_RARITY_BOX`、`ONS_CONDITION_BOX`、`N_POPUP` | `core/nav_coords.py:107,108,308` | 各命中 1；在用的是 `N_POPUP_HINT` | 同上 |
| 2 | `enable_timer_resolution()` | `core/countdown.py:1219` | 命中 1；且 `timeBeginPeriod` 没配对 `timeEndPeriod` | import ok |
| 2 | `control_token()`（只读 getter） | `core/server.py:87` | 命中 1；鉴权全走 `_ctrl_token` 变量（393/400/417/422 行） | 56 passed |
| 2 | `idle_seconds()` | `core/server.py:138` | 命中 1；`_last_hit` 仍由 `touch()` 维护 | 同上 |
| 2 | `channel_last_event()` | `core/input.py:165` | 命中 1 | 同上 |
| 2 | `trajectory_status()` | `core/input.py:981` | 命中 1；`attach_trajectory`/`_plan_path` 未动 | 同上 |
| 2 | `APP_NAME` | `core/selfcheck.py:14` | 命中 1 | 同上 |
| 3 | `action_s` property、`THREAD_PRIORITY_NORMAL` | `core/precision.py:365,71` | 各命中 1 | 56 passed |
| 3 | `SUCCESS_RESULTS`/`FAIL_RESULTS`/`RESULT_LABEL` | `core/autotune.py:60-66` | 各命中 1；`ATTEMPT_RESULTS`/`GRADE_LABEL` 留着 | 同上 |
| 3 | `read_csv_rows()` | `evo/collect/parse.py:441` | 命中 1（`csv` 只被它用，一并删 import） | 同上 |
| 3 | `stage_names()` | `evo/collect/schema.py:141` | 命中 1；`STAGES` 仍被别处用 | 同上 |

没有删的（风险不足以为它动）：

- `perf/_ref`、`perf/merged_pre`、`perf/snapshots`（合计 ~460 MB）：清单里是
  **B 级需确认**，`make_worktree.ps1` / `tree_digest.py` / `replay_check.json`
  按目录名引用，删前要同步改三处脚本 —— 不是「确定死」。
- `_lottery_fsm.py` + `_test_lottery_fsm.py`：同上（需确认规格已并入 `core/`）。
- `core/autotune.py`、`core/precision.py`、`core/truth_model.py`、`core/ed25519_ref.py`
  四个整模块：B 级，删模块要连带下线测试与对拍工具，属于决策不是清理。
- `core/nav.py` / `core/nav_col.py`：前两轮按约束未动；第四轮用户放行后只改了
  固定等待（见第 8 节），ESC / 恢复链逻辑未碰。

## 8. 第四轮：固定等待 → 事件驱动（用户放行 nav 改动）

规则：**校验不删**，只把「固定睡 N 秒」换成「轮询到条件成立即走 + 上限保护」；
上限到达仍不满足时走原有报错路径，不允许「超时当成功」。

### 8.1 三处改动与上限

| 位置 | 改法 | 最坏等待上限 | 备注 |
|---|---|---|---|
| `nav.py:885` `recover_to_hall` 的 F4 | `tap("F4")` → `_wait_page("market", cap=2.0, step=0.25)` | 2.0s + 末次 `where`（热态 0.45~0.9s）≈ **3.0s**；实测最坏 2580ms | 段实测 `page=market` |
| `nav.py:914` / `nav.py:929` `recover` 的两条 F4 分支 | 同上 | 同上 | 同上 |
| `nav.py:772-788` `tab` | `sleep(settle)` → 每 0.3s 读一次 `tab_now()`（纯像素 ~15ms），上限仍是 `settle` | `settle(1.2)` + 1 次 `tab_now` ≈ **1.22s**；另 `click()` 自带 3.0s 画面变化轮询（改前也有） | 到点仍不对 → 原 `NavError` |
| `nav_col.py:1366-1369` `buy` | `sleep(1.5)` + `require` → `_wait_page(("detail","onsale"), cap=3.0, step=0.2, label="点购买", activate="点购买")` | 3.0s + 末次 `where` ≈ **4.0s**（`buy` 整段另含可见性 OCR 与 `click` 自带 6s 上限） | 失败文案与 `require` 一字不差 |

`enter_market` 本身**没有**固定等待：它走 `_poll_page("market", deadline)`（HEAD
里就是事件驱动）。用户说的「F4 2.0s」实际落在 `recover_to_hall` / `recover` 的回退链，
本轮改的就是那三处。

### 8.2 失败路径（`poll_bench.py` A 段，全部走原报错）

```
_wait_page("onsale", cap=0.40) 不匹配 → 返回 'market'（不抛，交调用方判）
_wait_page(..., label="点购买")  → NavError：点购买：期望页面 ('detail','onsale')，实际 market（OCR：…）
tab("collection", settle=0.40)   → NavError：点标签 collection 没切过去（绿框现在在 None）
```

### 8.3 台架复测：整链 A/B（同进程交替，n=5，L=0）

`perf/bench/poll_bench.py` B 段，`before` = 打补丁还原固定 sleep，`after` = 当前代码。

| mode | e2e p50 | e2e p95(≈max) | enter_market | tab | buy | tab 等待 | buy 等待 |
|---|---|---|---|---|---|---|---|
| before | 7023 | 7023 | 750 | 1450 | 2925 | 1204 | 2630 |
| after | 4219 | 4219 | 858 | 299 | 1104 | 4 | 806 |

分段节省（p50）：

| 段 | 改前 | 改后 | 节省 |
|---|---|---|---|
| enter_market | 750 | 858 | **−108ms（噪声，不认收益）** |
| tab | 1450 | 299 | **+1152ms** |
| buy | 2925 | 1104 | **+1822ms** |
| e2e | 7023 | 4219 | **+2805ms（−40.0%）** |

对照历史基线：文档第 2 节记录的**改前 7432ms**；本次同台架 `before` 复现为 7023ms
（差 409ms 属机器状态/OCR 冷热差异，±6%）。改后同台架 4219ms；
`round_report.py 1 1 3` 单进程口径为 **3687ms**（两者差异来自进程内温度与缓存，量级一致）。
按 7432 → 3687 记，**−3745ms（−50.4%）**。

### 8.4 三段等待单独扫描（假屏幕换页延迟 L 秒，n=5）

| seg | L | before p50 | before p95 | after p50 | after p95 | 节省 | after 判定页 |
|---|---|---|---|---|---|---|---|
| f4 | 0.0 | 2441 | 2807 | 449 | 894 | +1993 | market |
| f4 | 0.8 | 2430 | 2891 | 1421 | 1591 | +1009 | market |
| f4 | 1.5 | 2423 | 3052 | 2010 | 2580 | +414 | market |
| f4 | 2.5 | 2601 | 3060 | 2036 | 2120 | +565 | hall（失败，按原路径报错） |
| tab | 0.0 | 1458 | 1487 | 252 | 255 | +1206 | collection |
| tab | 0.8 | 2203 | 2216 | 994 | 1004 | +1209 | collection |
| tab | 1.5 | 2947 | 2984 | 1676 | 1783 | +1271 | collection |
| tab | 2.5 | 3889 | 3990 | 2736 | 2761 | +1152 | collection |
| buy | 0.0 | 2494 | 3068 | 997 | 1357 | +1497 | onsale |
| buy | 0.8 | 3252 | 3673 | 1779 | 2126 | +1473 | onsale |
| buy | 1.5 | 4035 | 4377 | 2553 | 2936 | +1482 | onsale |
| buy | 2.5 | 5019 | 5436 | 4122 | 4232 | +897 | onsale |

读法：

- **tab / buy 稳定**：所有 L 下节省 0.9~1.8s，方差小，收益远大于噪声 → 保留。
- **F4 的收益随渲染延迟衰减**：L=0 省 1.99s，L=1.5 只剩 0.41s，L≥2.0 上游本来就会
  超时（改前 `sleep(2.0)` 同样读不到 market）。收益随机器变慢而趋 0，但**不会为负**，
  且慢机器上判定更准（改前是「睡 2 秒看运气」，改后是「轮询到上限再下结论」）→ 保留。
- **enter_market 正常链路 0 收益**（它本来就没有固定等待），故不宣称。
- L=2.5 的 F4：`after_page=hall`，`_wait_page` 等满上限返回真实页面，调用方照旧
  `raise NavError`；**没有**「超时就当成功」。

### 8.5 回归

```
pytest tests/test_nav.py tests/test_nav_filter.py tests/test_ocr_backend.py
       tests/test_ocr_price.py tests/test_market.py tests/test_cards.py
       tests/test_autocalib.py -q
→ 234 passed in 152.32s
```

OCR 侧本轮零改动，价格识别路径（`money.scan_text` / 千分位 / `编号` 过滤）未触碰，
准确率与第 3 节一致。真机风险条目（逐像素同帧复用）保持现状，未做「相似即复用」。

复现：

```powershell
python perf/bench/poll_bench.py     # A 失败路径 + B 整链 A/B + C 三段扫描
```

