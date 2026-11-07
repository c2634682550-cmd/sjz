# 前端重做 · 接口清单与控件映射

依据：`core/server.py`（逐路由核对，2026-11-07）、`core/scheduler.py`、`core/engine.py`、`core/preflight.py`、`core/switches.py`、`core/records.py`、`core/timesync.py`、`core/config.py`。

分类定义：
- **A** 现有接口可完成，前端直接接线。
- **B** 后端缺接口/字段，前端置灰标「待接入」，接口补齐后接上。
- **C** 依赖尚未完成的能力（无数据源、无授权体系等），置灰并说明原因。

> 原则：B/C 一律渲染为禁用态 + 悬浮原因，不做假按钮。A 类才允许点击。

---

## 1. 现有接口清单

### GET

| 路径 | 返回 |
|---|---|
| `/`、`/index.html` | 单文件前端 |
| `/api/config` | 全量 config + `templates_ready:{buy,confirm}` + `config_ok` + `config_error` |
| `/api/jobs?[id]` | 不带 id：`{ok,busy,current}`；带 id：`{ok,job:{state,log,result,error,id,label}}` |
| `/api/tasks` | `{tasks:[...],config_ok,config_error}` |
| `/api/scheduler` | `{running,active,tasks,config_ok,config_error,log:[{t,ts,level,msg}],engine,clock,page}` |
| `/api/timesync` | `{offset,delay,server,at,samples:[{server,offset,delay}],stale}` |
| `/api/hotkey` | `{stop:{spec,active,msg,enabled,label},start:{...}}` |
| `/api/records?limit=1..5000` | `{ok,rows:[...],stats}` |
| `/api/skins` | `{ok,seasons:[{season,season_name,series,tiers:{tier:[{name,weapon,gun,skin}]}}]}` |
| `/api/switches` | `{ok,groups:[{name,items:[{key,label,kind,path,default,desc,hint,options,value}]}]}` |
| `/api/skins/plan?name=` | `{ok,plan,text}`；缺表 503，查无 404 |
| `/api/guard` | `{ok,guard{state,level,scene,goal,message,button{id,label},doing,plan,attempts,containment_pending,containment_ok,evidence,history[]},doing,engine_state}` |
| `/shots/<file>` | 成交截图 |
| `/assets/<file>` | 静态图片 |

### POST

| 路径 | 请求体 | 返回 |
|---|---|---|
| `/api/config` | `{console:{min_price,max_price,quota,budget,max_wear,workflow,craft,wear,sort,materials,colors,categories}}` + 顶层白名单 `threshold,click_hz,confirm_clicks,confirm_interval_ms,fire_timeout_s,failsafe,trigger_mode,workflow,monitor,save_shots,arm_grace_s,list_max_tries,input_mode` | `{ok,config}`；条件不合法 400 |
| `/api/tasks/add` | `{name,keywords,fire_at,note,template_from,mode,min_price,max_price,max_wear,craft,wear,season,sort,quota,budget,workflow,materials,colors,categories}` | `{ok,task,tasks}` |
| `/api/tasks/update` | `{id}` + `min_price,max_price,quota,budget,name,mode,workflow,craft,wear,season,max_wear,fire_at,sort,enabled,materials,colors,categories`，`status:"pending"` 仅用于重新排队 | `{ok,task,tasks}` |
| `/api/tasks/remove` | `{id}` | `{ok,removed}` |
| `/api/tasks/clear` | `{}` | `{ok,tasks}`（清已完成） |
| `/api/scheduler/start` | `{}` | `{ok,msg}`（权限/启动前检查在此拦截） |
| `/api/scheduler/stop` | `{}` | `{ok,msg}` |
| `/api/timesync` | `{samples?,servers?}` | `{ok,offset,delay,server,stratum,at,samples,tried}` 或 `{ok:false,error,tried}` |
| `/api/hotkey` | `{spec,enabled,start_spec,start_enabled}` | `{ok,msg,status}` |
| `/api/preflight` | `{mode?,workflow?}` | `{ok,checks:[{id,label,ok,detail,fix,warn}]}` |
| `/api/records/clear` | `{}` | `{ok,stats}` |
| `/api/skins/locate` | `{name,tier?,season?,sync?}` | `{ok,job,name,msg}`；抢购中 409 |
| `/api/follow` | `{want}` | `{ok,...}`；抢购中 409 |
| `/api/switches` | `{values:{key:value}}` | `{ok,applied,groups}` |
| `/api/skins/target` | `{name}`（空=所有） | `{ok,target}` |
| `/api/quit` | `{}` | `{ok,msg}` |
| `/api/guard/resume` | `{}` | `{ok,guard}`（对应 4 级暂停后「继续」） |
| `/api/guard/stop` | `{}` | `{ok,guard}`（对应 4 级「放弃这一轮」） |

任务字段（`/api/tasks` 实返）：`id,name,keywords,mode,workflow,regions,fire_at,status,note,created,result,min_price,max_price,max_wear,craft,wear,season,sort,materials,colors,categories,quota,budget,enabled,bought,spent`。
状态：`pending / armed / running / done / failed / skipped`。
引擎状态：`scan / watch / fire / confirm / done / abort`。
统计：`hz,score,clicks,elapsed,started_at,trigger_mode,mode,workflow/scans/rows,bought,spent`。

---

## 2. 控件映射表

### 顶部栏

| 控件 | 接口 | 类 |
|---|---|---|
| 产品标识 | 静态 | A |
| 当前方案名 | 无方案接口 | **B** |
| 任务进度（已完成/总数、已抢/限量） | `GET /api/tasks`、`GET /api/scheduler` | A |
| 主导航 抢购/目标库/记录/分析/日记/设置 | 前端路由 | A（分析=客户端聚合记录；日记见 B） |
| 紧凑密度切换 | localStorage | A |
| 运行状态点 | `GET /api/scheduler` | A |
| 启动抢购 F7 | `POST /api/scheduler/start` + `GET /api/hotkey` | A |
| 窗口最小化/关闭 | pywebview API（`window.pywebview`） | A |

### 抢购页 · 四状态卡

| 控件 | 接口 | 类 |
|---|---|---|
| 游戏窗口（实测值 + 修复） | `GET /api/preflight` → `window` | A（修复=重测） |
| OCR 识别（实测值 + 修复） | `preflight` → `ocr` | A（重测）；拉起服务 **B** |
| 时间校准（偏差 + 校准按钮） | `GET /api/timesync`、`POST /api/timesync` | A |
| 购买延迟（设定/实测） | 设定=`switches.fire.delay`；实测字段不存在 | A（设定）/ **B**（实测） |
| 页面偏离横幅 | `GET /api/scheduler` → `page` | A |
| 异常页文案 / 唯一主按钮 | `GET /api/guard` 的 `message`、`button` | A |
| 异常页进度「第 N 步 / 共 M 步」 | `GET /api/guard` 的 `attempts` + `plan` 长度（不展示 `plan` 原文） | A |
| 异常页「继续」 | `POST /api/guard/resume`（`button.id` 由后端给） | A |
| 异常页「放弃这一轮」 | `POST /api/guard/stop`（`button.id` 由后端给） | A |
| 异常页「关闭程序」结束页 | `POST /api/quit`（`button.id=quit`） | A |
| 收尾中状态（疑似误买） | `containment_pending / containment_ok` | A |
| 「最近发生了什么」折叠列表 | `guard.history[]` | A |
| 「跳过本任务」 | 无单人任务跳过接口 | **B-13** |

### 抢购页 · 左任务清单

| 控件 | 接口 | 类 |
|---|---|---|
| 方案下拉 | 无方案接口 | **B** |
| 搜索 | 客户端过滤 | A |
| 赛季筛选 / 类型筛选 | 客户端（任务 `season` / `workflow` / `craft`） | A |
| 表格：目标/成色·磨损/价格区间/限量/已抢/状态 | `GET /api/scheduler`（tasks） | A |
| 行选中 → 右侧即时编辑 | 前端状态 | A |
| `+ 添加任务` | `POST /api/tasks/add` | A |
| 批量启用 / 全部停用 | 循环 `POST /api/tasks/update`（逐条） | A（非原子，见 B-3） |
| 拖动排序 | 无顺序字段/接口 | **B** |
| 导入 · 导出 | 无方案级导入导出 | **B** |
| 清空已完成 | `POST /api/tasks/clear` | A |
| 删除任务（二次确认） | `POST /api/tasks/remove` | A |

### 抢购页 · 右任务设置面板

| 控件 | 接口 | 类 |
|---|---|---|
| 赛季目标 | `POST /api/tasks/update {season}` | A |
| 成色选择 | `{wear}` | A |
| 极品 / 优品 | `{craft}` | A |
| 磨损上限 | `{max_wear}` | A |
| 价格区间 | `{min_price,max_price}` | A |
| 本轮限量 | `{quota}` | A |
| 公示期（开卖时刻） | `{fire_at}`（Unix 秒） | A |
| 启用开关 | `{enabled}` | A |
| 任务路线切换 | `{workflow}` | A |
| 材质/颜色/分类多选 | 能存（`{materials,colors,categories}`），但**选项列表无来源** | A（存）/ **B**（选项源） |

### 抢购页 · 节拍面板

| 控件 | 接口 | 类 |
|---|---|---|
| 购买延迟 | `switches.fire.delay` | A |
| 动态调整开关 | `switches.fire.adaptive` | A |
| 历史推荐一键采用 | 无实测延迟历史接口 | **C** |
| 连续点击次数 | `switches.fire.confirm_clicks` | A |
| 补点间隔 | `switches.fire.confirm_gap` | A |
| 抖动次数（次数） | 只有 `fire.jitter`（毫秒） | **B** |
| 随机间隔（毫秒） | `switches.fire.jitter` | A |
| 停止条件·按品阶计数 | 只有全局 `stop.on_fail` | **B** |
| 停止条件·按失败次数 | `switches.stop.on_fail` | A |
| 点击频率 / 按钮阈值 / 连点超时 / 确认兜底 | `switches` 同名项 | A |
| 本轮进度与花费 | `GET /api/scheduler`（active task 的 `bought/quota/spent/budget`） | A |

### 运行态

| 控件 | 接口 | 类 |
|---|---|---|
| 倒计时大字 | `task.fire_at` + `scheduler.clock.offset` | A |
| 五步执行清单 | 仅 5 个粗粒度 `engine.state`，无 phase 0–4 | **B**（近似映射见备注） |
| 基础延迟 | `switches.fire.delay` | A |
| 当前延迟（实测） | `engine._fire_lag` 未暴露 | **B** |
| 最近反馈 | 无结构化字段（只有自由文本日志） | **B** |
| 连续失败次数 | `engine._fails_in_row` 未暴露 | **B** |
| 任务队列（当前/完成/等待、进度、成功方式） | `GET /api/scheduler`（status/bought/quota/result） | A |
| 收藏总额度环图 + 本次花费 | active task `budget/spent`；`engine.stats.spent` | A |
| 关键动作日志（时间戳+结果） | `scheduler.log[{t,ts,level,msg}]` | A（毫秒级迟到值见 B-4） |

### 目标库

| 控件 | 接口 | 类 |
|---|---|---|
| 赛季/类型/品阶筛选 + 数量 | `GET /api/skins` + 任务计数 | A |
| 卡片/列表视图 | 前端 | A |
| `+` 一键加入方案 | `POST /api/tasks/add`（同名同路线会复用） | A |
| 已加入对勾与数量 | `GET /api/tasks` 匹配 `name` | A |
| 游戏内定位 | `POST /api/skins/locate` + 轮询 `GET /api/jobs?id=` | A |
| 关注/取消关注 | `POST /api/follow` | A |
| 目标标记 | `POST /api/skins/target` | A |
| 「方案」概念本身 | 见 B-1 | **B** |

### 设置页

| 控件 | 接口 | 类 |
|---|---|---|
| 分组导航 | 前端 | A |
| 运行开关（7 组） | `GET/POST /api/switches` | A |
| 热键设置 | `GET/POST /api/hotkey` | A |
| 启动前检查（含修复提示） | `POST /api/preflight` | A |
| NTP 服务器测速表 | `POST /api/timesync` → `samples[]`；`GET /api/timesync` 存最近样本 | A（失败服务器仅 `tried[]`，不驻留） |
| 时间校准按钮 | `POST /api/timesync` | A |
| OCR 线程数 | 无 OCR 配置接口（`/api/config` 不收 `ocr`） | **B** |
| 识别测试 | 无接口 | **B** |
| 效率档（极速/快速/谨慎） | 无预设接口 | **B** |
| 定时任务窗口 | 无 | **B** |
| 通知开关 / Webhook | 无 | **B** |
| 方案管理 | 无 | **B** |
| 实验工具（定位/关注/校时） | 现有接口 | A |
| 关于（版本/路径） | 可静态 + `config_ok` | A |
| 授权状态 | 无授权体系 | **C** |

### 记录 / 分析 / 日记

| 控件 | 接口 | 类 |
|---|---|---|
| 记录表 + 统计 | `GET /api/records` | A |
| 清空记录（二次确认） | `POST /api/records/clear` | A |
| 截图查看 | `/shots/<file>` | A |
| 分析页（按时间/结果/命中率聚合） | 客户端聚合 `/api/records?limit=5000` | A（大数据量见 B-8） |
| 分析页的延迟/品阶维度 | 记录无 `tier/lag_ms` 字段 | **B** |
| 日记页（持久化动作流水） | 只有内存 80 行 `scheduler.log` | **B** |

---

## 3. 缺口清单（后端补这些，前端才解除置灰）

### B-1 方案（Scheme）

- `GET /api/plans` → `{ok,plans:[{id,name,note,task_count,updated}]}`
- `POST /api/plans` `{name,note,clone_from?}` → `{ok,plan}`
- `POST /api/plans/rename` `{id,name}` / `POST /api/plans/remove` `{id}`
- `POST /api/plans/activate` `{id}` → 切换当前队列
- 被用：顶部方案名、左侧方案下拉、设置·方案管理、导入/导出。
- 备注：没有它之前，「加入方案」按「加入当前任务队列」实现（现有行为）。

### B-2 任务拖动排序

- `POST /api/tasks/reorder` `{ids:[...按新顺序]}` 或 `{id,before_id}`
- 需要 `queue` 顺序即执行顺序（现状是插入顺序，页面无法改）。
- 被用：任务清单「拖动排序」。

### B-3 任务批量操作（可选，原子性）

- `POST /api/tasks/bulk` `{ids:[],patch:{enabled:bool}}` → `{ok,updated}`
- 现状可用逐条 `update` 循环替代；仅当任务多时避免半成功。

### B-4 引擎运行指标

扩展 `GET /api/scheduler.engine`：
- `fire_lag_ms`（上轮「点击→弹窗」实测，来源 `_fire_lag`）
- `fails_in_row`（`_fails_in_row`）
- `phase` 0–4（准备与校时 / 筛选收藏 / 监控倒计时 / 提交连点 / 结果确认）
- `last_feedback` `{ts,text,level}`（最近一条结构化结果）
- 被用：四状态卡「购买延迟实测」、五步清单、最近反馈、连续失败、日志迟到毫秒值。

### B-5 异常恢复（已落地，除「跳过本任务」）

- 已完成：`GET /api/guard` + `POST /api/guard/resume` + `POST /api/guard/stop`，前端异常页只读 guard，不再自行拼文案。
- 仍缺：`POST /api/tasks/skip` `{id}` → 跳过当前任务；`guard` 里没有对应 `button.id`。
- 被用：异常页第二动作「跳过本任务」（当前置灰「待接入」，不占主按钮位）。

### B-6 日记（持久化动作流水）

- `GET /api/journal?limit=&from=&to=&level=` → `{ok,rows:[{ts,t,level,msg,task_id,result}]}`
- 现状 `scheduler.log` 仅内存 80 行，重启即失。
- 被用：日记页。

### B-7 OCR 设置

- `GET /api/ocr` → `{ok,threads,backend,available,service:{...},efficiency}`
- `POST /api/ocr` `{threads,efficiency}`（`/api/config` 不收 `ocr` 段）
- `POST /api/ocr/test` `{}` → `{ok,text,ms,confidence}`
- 被用：设置·OCR 线程数、识别测试、效率档。

### B-8 分析聚合

- `GET /api/analysis?days=7` → 按日/结果/赛季/品阶聚合的命中率、花费、`median_lag_ms`、`verified_rate`。
- 现状可前端聚合 5000 条；记录没有 `tier`、`lag_ms` 字段，深层维度做不了。
- 被用：分析页图表与「按品阶」。

### B-9 通知

- `GET/POST /api/notify` `{enabled,webhook,events:["hit","fail","page_lost"]}` + `POST /api/notify/test`
- 被用：设置·通知与 Webhook。

### B-10 定时任务窗口

- `GET/POST /api/schedule` `{enabled,start,end,days[]}`
- 被用：设置·定时任务窗口。

### B-11 任务选项源（材质/颜色/分类）

- `GET /api/tasks/options` → `{ok,materials:[],colors:[],categories:[]}`（来自 `nav` 面板常量）
- 现状只能自由文本/手写，界面无法给出多选清单。

### B-12 授权与版本

- `GET /api/about` → `{ok,version,build,data_dir,license:{state,expires}}`
- 被用：设置·关于与授权（授权部分 C）。

### B-13 跳过当前任务

- `POST /api/tasks/skip` `{id}` → `{ok,task,tasks}`
- 期望 guard 在 4 级场景下能给出 `button.id="skip"` 的备选按钮（当前只有一个主按钮）。
- 被用：异常页「跳过本任务」。

### C 类（依赖能力未落地，保持置灰）

- 历史推荐一键采用：依赖 B-4 的 lag 历史 + 推荐算法。
- 授权状态：无授权体系。

---

## 4. 前端执行规则

1. 上表 **A** 才渲染为可点击；每个可点击控件带 `data-api`（请求路径）供审计脚本核对。
2. **B/C** 统一渲染 `.disabled-reason` 按钮：`disabled` + `aria-disabled` + `data-gap="B-1"`，悬浮/聚焦显示「待接入：<缺什么>」。
3. 顶部/页面级显示 `缺口 N 项` 入口，点开列出本页 B/C 项，避免用户以为功能坏了。
4. 审计脚本 `tools/fe_audit.py` 只对 A 类元素触发，断言：发出对应请求 + 发生后端/界面状态变化；B/C 断言：`disabled` 且 `data-gap` 存在。
