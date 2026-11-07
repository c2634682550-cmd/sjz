# 独立黑盒复核：四批“已修复”改动

- 验证对象：`C:\Users\26346\Desktop\project\dafors`
- 验证快照：**S1 = 2026-11-07 07:31:52**（50 个 `core/*.py` + `bricks.json`，SHA256 清单 `_verify_bb/snapshot_manifest.txt`）；复核末尾发现源码仍在改（新增 `autotune.py`、改动 12 个文件），追加 **S2 = 2026-11-07 07:51:56**（56 个 `.py` + `bricks.json`，清单 `_verify_bb/snapshot_manifest_s2.txt`）二次回归，见 §5.2。
- 验证方式：只信现象。把 `core` 整包复制到 `%TEMP%\dafors_bb`，让 `config.ROOT` / `records.PATH` 落在临时目录；用真实文件、真实子进程、真实 `kill()`、真实 UDP 报文、真实 Win32 隐藏窗口，不使用 mock。全程未动真实桌面、未开可见窗口、未移动光标、未装驱动。
- 运行器：项目自带 `.venv\Scripts\python.exe`（CPython 3.11）
- 证据脚本：`_verify_bb/t_persist.py`、`t_numeric.py`、`t_input.py`、`t_stability.py`、`probe_engine_handle.py`
- 证据日志：`_verify_bb/t_persist_0735.log`、`t_numeric_0732.log`、`t_input_0734.log`、`t_stability_0733.log`、`longrun.log`、`longrun_summary.json`
- 结果：**S1 四批 20/20 项按外部现象判定 PASS**；附加发现 **N4d 既有缺陷 FAIL**（S2 已修复）；**D1：报告里的 72h CPU 漂移数字未复现**（实测 +19.9%，报告 -14.3%，未越过 20% 阈值）；**D2：S2 新增加“≥3 个独立来源、≥2 个 /16 网段”规则，单服务器/单网段配置从此永久不校时**（行为收紧，需产品确认）。

## 0. 验证环境与源码并发改动（必读）

复核期间，仓库 `core/` 被**另一个会话持续修改**（不是本复核改动，本复核未改任何源码、未 commit）：

| 时间 | 现象 |
|---|---|
| 07:24 前后 | `input.py`、`config.py`、`timesync.py`、`scheduler.py`、`switches.py`、`engine.py` 出现新改动（F7/F8 日志、message 跳过前台校验、timesync 重试退避等） |
| 07:28 快照 | `core/timesync.py:177` 多一个右括号，`t_numeric.py` / `t_stability.py` 导入即 `SyntaxError: unmatched ')'`——仓库当时处于**不可导入**状态 |
| 07:29:31 | 该语法错误被改掉（`py_compile` 通过） |
| 07:31:53–07:39 | `config.py`、`switches.py`、`engine.py`、`timesync.py`、`repeat.py` 仍在变 |

因此：本报告的全部结论只对应 **07:31:52 快照**（该快照 `py_compile` 全部通过，四批测试均在同一份代码上跑完）。若源码继续变动，需按同一脚本重跑。07:28 的瞬时语法错误不计入 20 项判定，但作为风险提示单列在 §5。

## 1. 持久化（8 项）

| # | 缺陷名 | 触发方式 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| P1 | `.good` 备份原子化 | 子进程循环写 3MB 配置，随机时刻 `kill()` **8 次**；另造“半份 .good + 坏 config”与“完整 .good + 坏 config”；造 >60s 孤儿 tmp | 每次 config 与 .good 均可解析；半份备份不被当合法配置；完整备份能恢复；旧 tmp 被清、新 tmp 保留 | `kills=8 坏 config=0 坏 .good=0`；半份：`load_ok=False queue=0 err=…损坏…`；完整：`queue=2 ids=[a1,a2]` + 日志“已从最近一次正常配置备份恢复”；tmp：`old=False oldgood=False new=True` | **PASS**（P1a/b/c/d） |
| P2 | 台账残行吞下一条 | 手工把台账尾截成 25 字节残行，再 `add()` | 截掉残行、旧记录与新记录都可读、文件以 `\n` 结尾、`recovery_log()` 记录字节数 | `rows=['new','old'] tail=b'\n' recovery.removed=25 size=81->224`，stdout“上次崩溃留下 25 字节残行，已截断” | **PASS** |
| P3 | 多进程写丢数据 | 6 个**真实进程**×5 次 `Config.transaction()`；8 个**真实进程**×100 条台账 | 30/30、800/800，无坏行无重复 | `rows=30 unique=30 rc=[0]*6`；`rows=800 unique=800 bad=0 rc=[0]*8 用时5.2s` | **PASS** |
| P4 | `mode=list` 被改成 countdown | 老配置 `{"mode":"list"}` 连读两次；`mode=detail`+显式 `workflow=countdown`；显式 `workflow=list` | 推导稳定且显式 workflow 优先 | `1次=('list','list') 2次=('list','list')`；`('countdown','detail')`；`('list','list')` | **PASS** |
| P5 | 部分字典整体替换 | 只写 `console.min_price` 等半节配置 | 未写的默认子键保留 | `console` 4 键齐全、`premarket` 2 键、`screen` 2 键、`ocr.ppocr` 6 键全在 | **PASS** |
| P6 | 满盘/写失败截断原文件 | tmp 路径被目录占用（PermissionError）；config.json 设只读 | 显式抛 `OSError`，原文件与 `.good` 字节不变 | `err=PermissionError… cfg_same=True good_same=True`；只读：`WinError 5 bytes_same=True size=45` | **PASS** |
| P7 | 台账写失败静默 | 台账设只读后 `add()`；`strict=True`；恢复可写后再写 | 返回 `None` 但 `last_error()`/`write_ok=False` 可见且 stdout 告警；strict 抛 `RecordWriteError`；恢复后清错误 | `ret=None last_error=PermissionError… write_ok=False`；`成交记录写不进去：[Errno 13]…`；`ret=True last_error=None write_ok=True` | **PASS** |
| P8 | 外改配置被未 reload 的 save 覆盖 | 内存改 `threshold`，盘上外部改 `sentinel/note/console`，直接 `save()` | 三方合并，外部字段保留、内存字段生效 | `sentinel=EXT note=USER-NOTE threshold=0.7 console.max_price=99`，日志“盘上配置被外部改过，已按字段合并后保存” | **PASS** |

证据：`_verify_bb/t_persist.py` → `SUMMARY|persist|15/15 PASS`。

## 2. 数值与时间（5 项 + 附加 1 项）

本地起真实 UDP 服务器（端口 123 被系统保留，用 1123 + 仅端口重定向 shim，报文与校验逻辑原样）。NTP 用例：

| # | 缺陷名 | 触发方式 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| N1 | NTP 应答无校验 | 依次回：全零 48B、短包 20B、mode=3、stratum=0、origin 错配、t2/t3=0、t3<t2、offset=99999；再回正常包 | 8 种坏包全部 `ValueError` 丢弃；正常包 offset≈0.25 | `zero48/short20/mode3/stratum0/badorigin/zerotime/t3_lt_t2/huge_offset` 全部 `ValueError`；`valid offset=0.2500 stratum=2` | **PASS** |
| N1b | delay=0 伪造包被“最低延迟”选中 | 3 个正常样本 + 1 个 `offset=1234, delay≈0` 伪造包 | 中位数 0.42，伪造包不赢 | `ok=True offset=0.4200 kept=3`，samples 中 1234 被剔除 | **PASS** |
| N1c | 样本不足仍更新 | 只给 2 个有效样本 | 不更新状态，保留上次 0.42 | `ok=False err=有效样本只有 2 个（少于 3）… offset=0.4200` | **PASS** |
| N1d | TTL 走壁钟 | 单调轴推 7h；壁钟回拨 100s | 7h→过期；回拨→判“回拨” | `expired=校时结果已过期`；`back=系统时钟被回拨` | **PASS** |
| N2 | 开关边界表缺失 | 遍历 `switches.BOUNDS`：22 个数值开关的 lo/hi/越界/NaN/Inf；`apply_all` 混入非法；手改 config 写 NaN；字符串 `"3"` | 22 个全有边界且行为正确；整批原子拒绝；脏值回默认并记 `issues()`；字符串归一 | `numeric=22 missing_bounds=[] violations=[]`；`raised=values：price.quota：0 小于下限 1 d={}`；`value=0.85 issues={'fire.threshold':'必须是有限数字…'}`；`ok=True` | **PASS**（N2a-d） |
| N3 | 货币串取末尾数字 | 16 条：`¥1,234.5`、`¥12 345`、`¥1,234.567`、`-5`、`9,99`、`1,00,000`、`025`、全角等 | 按声明全部符合 | 16/16 符合：`¥1,234.5->1234.5`、`¥12 345->12345`、`¥1,234.567->None`、`-5->None`、`9,99->None`、`025->None`、`¥１，２３４->1234`（宽进） | **PASS** |
| N4 | `¥` 后无下限/小数/负号 | `¥5`、`¥15.5`、`-15000`、`¥15,000`；`pick()` 竞选；NaN/负阈值/0 元 | 前三判无价；`¥15,000`=15000；pick 跳过假低价；NaN/负阈值当未设置、0 元拒 | `¥5->None; ¥15.5->None; -15000->None; ¥15,000->15000.0`；`best=¥9,999/9999.0`（跳过 ¥5）；`nan_pass=True neg_pass=True zero_rejected=True` | **PASS**（N4a-c） |
| N4d | [附加，超出本轮声明范围] 型号 token 紧邻价格被吞 | `M250 15,000`、`QBZ95-1 15,000`、`QBZ951 突击步枪 25,000` | 仍能读出价格 | `M250 15,000->None`、`QBZ95-1 15,000->None`、`QBZ951 突击步枪 25,000->25000.0` | **FAIL（既有缺陷，非本轮引入）** |
| N5 | 超时用壁钟 | 壁钟回拨 100000s 后跑 `jobs` 计时；`wait_frame_change` 假帧差 255 | elapsed 仍≈真实值、非 None/负 | `elapsed=0.36 state=done`；`returned=0.062 calls=3` | **PASS**（N5a/b） |

N4d 归属证据：用 `git show HEAD:core/market.py` 单独加载同一沙箱，`M250 15,000->None`、`QBZ95-1 15,000->None`、`QBZ951 突击步枪 25,000->25000.0` 与当前完全一致；`git diff HEAD -- core/market.py` 显示 `_MODEL_TOKEN` 摘除顺序未被本轮改动。结论：**修复前就存在，不是本轮改坏**。

证据：`_verify_bb/t_numeric.py` → `SUMMARY|numeric|14/15 PASS`（唯一 FAIL 为附加项 N4d）。

## 3. 输入通道（3 项）

用真实 Win32 隐藏窗口（`WS_POPUP`、不 `ShowWindow`），记录窗口过程收到的消息与光标坐标。

| # | 缺陷名 | 触发方式 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| I1 | 缺 `message` 第三通道 | 隐藏窗口 + `Mouse(mode="message")` 点 (50,60)；然后 `DestroyWindow` 再判定/点击 | 窗口收到 move/down/up 各 1、光标不动、`accepted=True`；销毁后 `WINDOW_GONE` 且点击显式抛错 | `counts={'move':1,'down':1,'up':1,'null':1,'last_xy':(50,60)}`，`cursor (1601,48)->(1601,48)`，`effective=message`；销毁 `cap_rc=WINDOW_GONE usable=False`，`click_err=INPUT_ERROR: 纯消息点击失败…` | **PASS**（I1a/b） |
| I2 | 降级静默 | 无 `interception.dll`：默认策略 / `fallback=fail` / 键盘 / 未知 mode / message 无 hwnd | 实际通道与原因码可见、账本留痕；fail 策略显式抛错 | `effective=legacy degraded=True rc=DRV_UNAVAILABLE`，账本 `from=interception to=legacy`；`err=DRV_UNAVAILABLE ok=False`；键盘同；`UNKNOWN_MODE` 入账；`err=NO_TARGET_HWND` | **PASS**（I2a-e） |
| I3 | UIPI 静默失效 | 同权限窗口；`EnumWindows` 找到真实管理员窗口（RID=12288）；`process_integrity` 调 500 次 | 同权限不误报；高完整性构造前即 `UIPI_BLOCKED`；句柄不涨 | `ours=8192 target=8192 usable=True`；管理员窗口 `legacy_err=UIPI_BLOCKED detail=目标窗口是「管理员」权限，本进程是「普通」…`；`handles 0->0` | **PASS**（I3a-c） |

证据：`_verify_bb/t_input.py` → `SUMMARY|input|10/10 PASS`。

## 4. 稳定性（4 项）

| # | 缺陷名 | 触发方式 | 期望 | 实测 | 判定 |
|---|---|---|---|---|---|
| S1 | 看门狗缺失 | 真实时间轮询：不心跳；连续超时；冷却期；冷却后；`age_override` 喂 `beat_age_s`；读 `Engine.status` | 不误判、3 次确认、冷却内不重复、冷却后再触发；Engine 暴露真实 `beat_age_s` | `never=False seq=[False,False,True,False] fired=1 after_cooldown=[False,True] fired=2`；override `[False,False,True]`；`after_beat=0.031` | **PASS**（S1a-c） |
| S2 | 资源无上限 | `rotate(keep=3)` 连续轮转；70 张截图上限 40；20×300KB 上限 1MB；日志 8KB；台账超限轮转后再 `stats()` | 归档只留 .1/.2；删最旧保留 40；按字节删到 ≤1MB；日志改名 .1；缓存失效后统计一致 | `archives=['rot.bin.1','rot.bin.2']`；`removed=30 left=40`；`removed=17 left_files=3 left_bytes=921600`；`rotated=True bak=9000`；`kept_rows=6 stats_hits=6` | **PASS**（S2a-e） |
| S3 | Engine↔Cleanup 引用环 | `gc.disable()` 后建/删 Engine，weakref 观察；20 次与 200 次构造统计内核句柄 | 引用计数归零即释放；句柄不增长 | `del 后对象仍存活=False`；`20 个 handles 0->0 delta=0`；独立探针 `200 个 h0=0 h1=0 delta=0`（gc 关闭） | **PASS**（S3a/b + 探针） |
| S4 | 台账 O(N²) | 1k 与 50k 行各跑 `load(200)`；50k 全量 `stats()` 首次/二次 | load(200) 耗时基本不变、全量正确；stats 命中缓存 | `1k=0.74ms 50k=0.81ms 比值=1.10`，全量 `50k=89.4ms rows=50000`；`hits=10000 首次=84.7ms 二次=0.355ms 三次=0.270ms` | **PASS**（S4a/b） |

证据：`_verify_bb/t_stability.py` → `SUMMARY|stability|12/12 PASS`；`probe_engine_handle.py` → `ENGINE_HANDLE_PROBE engines=200 h0=0 h1=0 delta=0`。

## 5. 修复报告“声称数字”的独立复现

| 报告声称 | 本复核实测 | 结论 |
|---|---|---|
| 保存中强杀 3 次，坏备份 0/3 | 强杀 **8** 次，坏 `config` 0、坏 `.good` 0 | 复现（更强） |
| 残行后读到 6 条且 `after` 在 | 残行 25B 截断，`rows=['new','old']`，行尾 `\n`，`removed=25` | 复现 |
| 8 进程台账 800/800；6 进程配置 30/30 | `800/800 unique=800 bad=0`；`30/30 rc 全 0` | 复现 |
| 老配置 `mode:list` → `workflow=list` | 连读两次均 `list`，显式 workflow 优先 | 复现 |
| 满盘失败原文件 0 字节 → 19 字节保持 | 写 tmp 失败与只读替换均 `bytes_same=True`（45B） | 复现（同现象，载荷不同） |
| 只读台账从“不可见”到 `last_error()+write_ok=False` | `ret=None last_error=PermissionError… write_ok=False`，stdout 有告警 | 复现 |
| 外改 `sentinel=S1` → `EXT` 保留 | `sentinel=EXT note=USER-NOTE`，内存 `threshold=0.7` 同时生效 | 复现 |
| NTP 全零包 → `ValueError`，伪造 1234 被剔除，中位 0.42 | 8 种坏包全 `ValueError`；`offset=0.42 kept=3` | 复现 |
| 22 个数值开关全部有边界 | `numeric=22 missing_bounds=[]` | 复现 |
| 隐藏窗口 down/up 各 1 次、UIPI `err=5` | `move/down/up 各 1`；管理员窗口 `UIPI_BLOCKED`（err=5 同一路径） | 复现 |
| 72h 等效长跑 7/7、句柄 216→221、修环后“+0 锁句柄” | 同 harness 在 07:31 快照上跑 72.02h（4259 轮，真实 508s）：**7/7 PASS**；句柄 **218→223**（+5，平稳）；内存斜率 0.045 MB/h；**周期 CPU 漂移 +19.9%**（78.2→93.8ms，阈值 20%，边缘通过）；独立探针 200 次 Engine 句柄 delta=0 | 同类现象复现；**CPU 漂移数字与报告的 -14.3% 不一致** |

### 5.1 72h 长跑（同 harness 复跑，07:31 快照）

命令：`python -m tools.stability.longrun --hours 72 --max-real-s 900`（把 `tools/` 与 `core/` 都复制进沙盒，避免碰真实数据）。

```
等效时长：72.02h（cycles=4259，真实 508.0s）
内存斜率：0.045 MB/h（阈值 0.5）
句柄：218 → 223；线程：1 → 1
周期耗时漂移：CPU 19.9%（78.2 → 93.8ms）；墙钟 14.5%
  [PASS] 内存增长 < 0.5MB/h
  [PASS] 句柄回落到基线±30
  [PASS] 线程数收敛
  [PASS] 已打开文件不增长
  [PASS] 周期耗时不劣化 >20%
  [PASS] 截图目录受上限约束
  [PASS] 台账受轮转约束
```

结论：**7/7 PASS 复现**；句柄“+5 且全程平坦”与报告同类；**CPU 漂移 19.9% 与报告声称的 -14.3%（后段更快）方向相反，且只差 0.1 个百分点就触阈**。判定为“数字未逐字复现、但未越过阈值”，不升级为 FAIL，属于需要修复者解释的数字差异。
日志中的“记录轮询 209–258ms”警告是 harness 把 2ms 锁轮询 sleep 放大成 0.1 虚拟秒的产物；本复核在**真实时钟**下测得 `load(200) 50k 行 = 0.81ms`（S4a），两者不矛盾。

明细：`_verify_bb/longrun.log`、`_verify_bb/longrun_summary.json`。

### 5.2 第二快照 S2（07:51:56）二次回归

S1 之后仓库又改了 12 个文件并新增 `autotune.py`，因此冻结 S2 重跑。证据：`_verify_bb/s2_t_persist.log`、`s2_t_numeric_final.log`、`s2_t_input.log`、`s2_t_stability.log`。

| 批次 | S2 结果 | 与 S1 的差异 |
|---|---|---|
| 持久化 | **15/15 PASS** | 无差异 |
| 输入通道 | **10/10 PASS** | 无差异 |
| 稳定性 | **12/12 PASS** | 无差异 |
| 数值与时间 | **16/18 PASS** | N4d **已修好**（`M250 15,000`→15000、`QBZ95-1 15,000`→15000）；NTP 改为“每主机 4 样本 × ≥3 独立来源、≥2 个 /16 网段”；开关从 22 个增至 **36** 个（新增 `scan.repeat.min/max`，带跨字段 `min≤max`） |

S2 数值批的两个 FAIL 都是同一件事——**单源 NTP 被新规则拒绝**，不是旧缺陷复发：

- `N1b`（原用例：单服务器 4 样本 + 1 个伪造）→ `ok=False err=独立来源只有 1 个（有效样本 2/4 个，需要 ≥3 个来自不同 IP）`。
- `N1c`（原用例：2 个有效样本保留旧值）→ 同上被“独立来源不足”拒绝。

为确认新规则下“中位数/剔伪造”仍然成立，追加了受控输入用例（只替换 DNS 解析结果与 UDP 目标，校验/去重/中位数代码原样执行）：

| 用例 | 实测 | 判定 |
|---|---|---|
| N1e：3 个独立来源（10.0.0.2/10.1.0.2/10.2.0.2）×4 样本 | `ok=True offset=0.4197 independent=3 kept=3` | **PASS** |
| N1e2：其中 1 个 `offset=1234, delay≈0` 伪造 | `ok=True offset=0.4200 independent=3 kept=3`（伪造被剔） | **PASS** |
| N1f：只有 2 个独立来源 | `ok=False err=独立来源只有 2 个（有效样本 8 个，需要 ≥3…）` | **PASS（按新契约）** |

**D2 结论**：新的多源校验本身工作正常，但把“单台 NTP 服务器（或同 /16 的几台）”从可用配置变成**永久不校时**。修改前单源 4 样本能出 0.42；现在直接拒绝。默认配置是 3 个公网域名（不同 /16），正常用户不受影响；但用户若把 `timesync.servers` 改成 1 台（内网/离线环境常见），`sync()` 会一直 `ok=False` 且保留旧偏移。这是行为收紧，属**“修了但引入的新约束”**，需要产品明确是否接受。

## 6. FAIL 汇总

| 编号 | 项 | 判定 | 分类 | 说明 |
|---|---|---|---|---|
| N4d | 型号 token 紧邻价格（空格）时价格读不出 | **S1 FAIL → S2 PASS** | **既有缺陷，S2 已修复** | S1：`M250 15,000`、`QBZ95-1 15,000` 解析为 `None`；`git show HEAD:core/market.py` 在同一沙箱复现同样结果，说明不是本轮引入。S2 重跑三个用例全部读价正确。 |
| — | 其余 20 项 | **PASS** | — | S1 见 §1–§4；S2 复核见 §5.2 |
| D1 | 72h 长跑“CPU 漂移 -14.3%”数字 | **FAIL（仅数字复核）** | **未复现：报告数字与实测不符，非功能回归** | 同 harness 07:31 快照实测 **+19.9%**（78.2→93.8ms），与报告“109.4→93.8ms，-14.3%”方向相反；阈值 20%，属边缘 PASS。机制（内存/句柄/线程/文件/截图/台账）7/7 PASS 已复现。 |
| D2 | 单源/单网段 NTP 配置永久不校时 | **FAIL（S2 行为收紧）** | **修了但引入的新约束** | 新规则要求 ≥3 个不同 IP 且 ≥2 个 /16：`N1b`（单服务器 4 个有效样本）实测 `ok=False err=独立来源只有 1 个`；`N1c` 同。多源场景（N1e/N1e2/N1f）全部 PASS，说明校验本身正常；问题在“单台 NTP 服务器”这一合法配置被永久拒绝。 |

**观察项（不计入 FAIL，但必须知悉）**：
1. 07:28 快照 `core/timesync.py:177` 多一个 `)`，仓库当时**不可导入**；07:29:31 已修复，07:31 快照 `py_compile` 通过。说明验证期间仓库存在“编辑半成品”窗口；建议每次改完先跑 `python -m compileall -q core`。
2. 源码在复核全程仍在被另一会话修改（07:24–07:51 至少 12 个文件，新增 `autotune.py`）。本报告对 S1（07:31:52）与 S2（07:51:56）两个快照有效；若后续继续改，需要重跑 `_verify_bb/t_*.py`。
3. 修复报告里的“48 条新增测试全绿”“18 项 pytest”属于修复者自测，不是黑盒现象，本复核未以其作为证据；结论只取上述独立脚本的现象。

## 7. 复现方式

```powershell
# 1) 建沙盒（core 副本 + bricks.json + 脚本），避免碰真实 config/data
$bb = "$env:TEMP\dafors_bb"
# 2) 四个批次
$env:PYTHONPATH = $bb
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" "$bb\scripts\t_persist.py"
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" "$bb\scripts\t_numeric.py"
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" "$bb\scripts\t_input.py"
& "C:\Users\26346\Desktop\project\dafors\.venv\Scripts\python.exe" "$bb\scripts\t_stability.py"
```

清理：测试产物只在 `%TEMP%\dafors_bb` 与 `_verify_bb/` 证据副本；沙盒已整目录删除。未修改、未提交任何源码。

注：`_verify_bb/t_numeric_0732.log` 等 S1 日志由脚本早期版本生成；当前 `_verify_bb/t_numeric.py` 增加了 S2 的 N1e/N1e2/N1f 多源用例，并把 N2a 的成对开关改用 `apply_all`（不改变 S1 的结论）。`snapshot_manifest.txt` = S1，`snapshot_manifest_s2.txt` = S2。
