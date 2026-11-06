# 前端 ↔ 后端接口契约 只读审查

## 0. 审查基线

| 项 | 值 |
| --- | --- |
| 仓库 | `C:\Users\26346\Desktop\project\dafors` |
| remote | `origin = https://github.com/c2634682550-cmd/sjz` |
| 分支 | `main`（无本地领先/落后） |
| HEAD | `62d5e57 test: 同步既有测试与审查/清理记录` |
| 工作区（开工时） | `git status --porcelain` 为空，干净，无未提交改动 |
| 工作区（交稿时） | 出现 5 项**非本次审查产生的**未跟踪文件：`_fe_probe_result.json`、`_fe_probe2_result.json`、`_fe_probe3_result.json`、`_fe_review_ui.md`、`_fe_shots/`（推测为并行的另一个会话/工具留下的产物）。本次审查只读了源码，未创建、未修改、未删除它们 |
| 本次改动 | 只新增本报告文件，未改任何源码，未 commit、未 push |

审查对象：`web/index.html`（1077 行，内联 JS/CSS）、`core/server.py`（1081 行，HTTP + 静态页）、
`core/config.py`、`core/switches.py`、`core/scheduler.py`、`core/preflight.py`，旁证
`core/engine.py`、`core/market.py`、`core/records.py`、`core/timesync.py`、`core/actions.py`、`desktop.py`。

---

## 1. 结论

**一句话**：接口形状大体对得上、错误体也统一走 `{"ok":false,"msg":...}`，真正危险的不是崩溃而是「界面显示的状态和引擎实际执行的状态是两套」——演练开关、任务目标、价格筛选都存在双写/三写源，其中任意一处都能在用户不知情时把「演练」变成「实战」、把旧目标再买一次。

最关键 3 条：

1. **演练模式形同虚设（高）**：`env.dry_run` 开关显示「开」，但默认 `run_mode=auto` 让 `resolve_dry_run()` 返回「实战」，而 `startRun()` 调 `/api/scheduler/start` 传的是空对象 `{}`，不传 `dry_run` → 点「开始抢购」是真点购买，界面却只有一处「当前生效：实战」的小字提示。（`web/index.html:812`、`core/config.py:86-87,228-232`、`core/server.py:631-652`）
2. **换目标不会替换旧任务，而是再排一条；`start` 在「没有可跑任务」时会把全部已完成任务重置为 pending（高）**：`syncTask()` 按 `name` 匹配，匹配不到就 `add`，旧目标任务留在队列里；把当前任务取消勾选后再点开始，`scheduler.start()` 会把旧的 `done` 任务复活并重新开抢。（`web/index.html:795-801`、`core/scheduler.py:413-422`）
3. **设置页和抢购页重复写同一批字段，互相覆盖（高）**：`min_price/max_price/quota/budget` 在设置页走 `/api/switches`（`console.*`），在抢购页走 `/api/config` 的 `console`；两个页面互不刷新，点开始时 `syncTask()` 拿表单旧值把用户刚在设置页改的值盖回去。（`web/index.html:757-770,812,925-934,1020-1034`、`core/switches.py:79-95`）

---

## 2. 接口对照表

### 2.1 端点总览（前端实际调用的 18 个）

| # | 接口 | 方法 | 前端调用点 | 请求体 / 查询 | 后端入口 | 实际返回 | 前端读取 | 契约结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `/api/scheduler` | GET | `index.html:596`（poll，1s） | — | `server.py:231-234` → `scheduler.status():502-518` | `{running, active, tasks[], config_ok, config_error, log[], engine{state,running,dry_run,mode,stats,error,rows,log}, clock}`，**无 `ok`** | `st.engine`、`eng.stats`、`eng.rows`、`eng.log`、`st.tasks`、`st.log`、`eng.error`、`eng.state`；**丢弃** `st.running`、`st.config_ok`、`st.active`、`st.clock` | 字段全部存在 ✔；`running`/`config_ok` 被丢弃导致按钮与状态判定错源（见 M6、M3） |
| 2 | `/api/tasks` | GET | `index.html:674`、`797` | — | `server.py:226-230` → `tasks_status():186-193` | `{tasks[], config_ok, config_error}`，**无 `ok`** | `r.tasks` | 字段存在 ✔；`config_ok` 被丢弃（M3） |
| 3 | `/api/tasks/add` | POST | `index.html:801` | `{name, mode:"list", status:"pending", min_price, max_price, quota, budget, workflow, wear?, craft?}` | `server.py:402-432` | 成功 `{ok:true, task, tasks}`；失败 **400** `{ok:false, msg}` | `r.ok`、`r.msg` | ✔ 类型一致（数值 `Number()` 后是 number，`name` 是 string）；语义不一致见 H4、M1 |
| 4 | `/api/tasks/update` | POST | `index.html:690`、`800` | 同 3 + `{id}`；`enabled` 为 bool | `server.py:569-601` | `{ok:true, task, tasks}` / 400 `{ok:false,msg}` / 404 `{ok:false,msg}` | `r.ok`（690 处**不读**） | ✔；690 处无 try/catch（L3） |
| 5 | `/api/tasks/remove` | POST | `index.html:683` | `{id}` | `server.py:565-567` | **永远** `{ok:true, removed:bool}` | 什么都不读 | 删不存在的任务也回 `ok:true`，前端无 catch（L3） |
| 6 | `/api/tasks/clear` | POST | `index.html:696` | `{}` | `server.py:603-605` | `{ok:true, tasks}` | `r.ok`（实际未读，异常走 catch） | ✔ |
| 7 | `/api/records` | GET | `index.html:556` | `?limit=200` | `server.py:239-247` | `{ok:true, rows[], stats{tries,hits,spent,rate}}`；`limit` 非整数 → 400 | `r.rows`、`st.tries/hits/spent/rate` | ✔；`stats` 是全量统计、`rows` 受 limit 限制（M9） |
| 8 | `/api/skins` | GET | `index.html:704` | — | `server.py:248-249,434-452` | `{ok:true, seasons[{season,season_name,series,tiers{tier:[{name,weapon,gun,skin}]}}]}` | `r.seasons`、`s.season`、`s.season_name`、`s.tiers`、`it.name` | ✔ |
| 9 | `/api/skins/target` | POST | `index.html:742`、`750` | `{name}`（清空时 `{name:""}`） | `server.py:545-563` | `{ok:true, target}` / 404 `{ok:false,msg}` | 742 只判 try/catch，750 静默吞 | ✔；清空路径 `name:""` 会命中 `target={}` 分支，正确 |
| 10 | `/api/config` | GET | `index.html:1021` | — | `server.py:213-214` → `config.public():508-518` | 全量 config + `templates_ready` + `config_ok` + `config_error` + `dry_run` + `dry_run_source` | `console.workflow/min_price/max_price/quota/budget/craft/wear`、`console.target.name`、`dry_run` | 字段存在 ✔；`config_ok/config_error/dry_run_source` 被丢弃（M3、H1） |
| 11 | `/api/config` | POST | `index.html:776`（防抖 600ms） | `{console:{min_price,max_price,quota,budget,workflow,wear?,craft?}}` | `server.py:366-398` | `{ok:true, config}`，异常 500 | **什么都不读** | 返回被丢弃 → `STATE.cfg` 不更新；空字段省略导致旧值留存（M1、M8） |
| 12 | `/api/scheduler/start` | POST | `index.html:812` | `{}` | `server.py:628-653` | 200 `{ok:false,msg}`（提权/闸门/队列空都走 200）；`{ok:true,msg}` | `r.ok`、`r.msg` | ✔ 判断正确；但**不传 `dry_run`** 导致演练失效（H1） |
| 13 | `/api/scheduler/stop` | POST | `index.html:829` | `{}` | `server.py:655-657` → `scheduler.stop():454-464` | `{ok:true|false, msg}`；幂等时 `msg="正在停止"` | **只判 catch，不读 `ok/msg`** | 无条件显示「已停止」（M5） |
| 14 | `/api/preflight` | POST | `index.html:975` | `{mode:"list"}` | `server.py:607-626`；`_preflight_workflow():797-807` | `{ok:bool, checks[{id,label,ok,warn,detail}]}` | `checks`、`byId.page/ocr/integrity/clock` | 字段存在 ✔；路线写死 `list`（M2） |
| 15 | `/api/timesync` | POST | `index.html:1040` | `{}` | `server.py:693-710` | `timesync.sync()` → `{ok,offset,server,stratum,delay}`；失败 `{ok:false,error}`；限流 429 `{ok:false,error}` | `r.ok`、`r.server`、`r.offset`、`r.error` | ✔（注意：错误键是 `error` 而不是 `msg`，前端正好读的是 `error`） |
| 16 | `/api/switches` | GET | `index.html:918` | — | `server.py:250-254` → `switches.snapshot():410-433` | `{ok:true, groups[{name, items[{key,label,desc,kind,path,default,options?,hint?,value,effective?,source?}]}]}` | `g.items`、`it.key/label/desc/kind/options/value/hint/source/effective` | ✔ 全部存在 |
| 17 | `/api/switches` | POST | `index.html:927` | `{values:{key:value}}`（bool 传 bool，数值传 `Number()`） | `server.py:511-527` → `apply_all():397-407` | `{ok:true, applied{key:coerced}, groups}` / 400 / 500 | `r.ok`、`r.applied[key]` | ✔ 类型一致；保存失败时 UI 已先行翻转（L4） |
| 18 | `/api/follow` | POST | `index.html:962` | `{want:bool}` | `server.py:529-543` → `actions.set_follow()` | `{ok:bool,before,after}`；运行中 409 `{ok:false,msg}`；异常 500 | `r.ok`、`r.after`、`r.msg` | ✔；`ok:false` 且无 `msg` 时回落「没点动」，不会显示 `undefined` |

后端存在但**前端没有任何调用**的接口：`GET /api/jobs`（215-225）、`GET /api/hotkey`（237-238）、`POST /api/hotkey`（285）、`POST /api/skins/locate`（289,454-492）、`GET /api/skins/plan`（255-264）、`POST /api/records/clear`（287-288）、`POST /api/quit`（293,659-689）、`GET /api/timesync`（235-236，前端只 POST）。见 L5。

### 2.2 字段级核对（前端每一个 `data.xxx` / `cfg.xxx` / `arr[0].xxx`）

| 前端读取 | 位置 | 后端真实来源 | 存在 | 类型一致 | 备注 |
| --- | --- | --- | --- | --- | --- |
| `st.engine.rows[i].name/price/wear/craft/cy` | `index.html:518-525` | `engine.py:1339` 附近（`{"name","price","wear","craft","cy"}`） | ✔ | `cy` 是 number（`round(float,1)`）；`price` 可能是 `None` | `price=null` 有 `fmtPrice` 兜底 ✔；`cy` 是中心 y 像素，不是序号（L6） |
| `st.engine.stats.clicks` | `index.html:607` | `engine.py:139-149`（初值只有 `clicks`，无 `bought`） | ✔ | number | 标签是「已买」，但 `clicks` 是点击次数；成交数在 `engine.py:611` 的 `bought`（M4） |
| `st.engine.stats.scans/hz` | `index.html:605-606` | 同上 + `1332` | ✔ | number | ✔ |
| `st.engine.error` | `index.html:619` | `engine.py:249` | ✔ | string/None | ✔ |
| `st.tasks[i].status/enabled/bought/quota/spent/result/workflow/min_price/max_price/craft/wear/budget/id/name` | `index.html:635-669` | `scheduler._read_queue()` 的任务字典 | ✔ | `enabled` 是 bool（`=== false` 判断正确）；`bought/spent/quota` 是 number | ✔ |
| `t.workflow` 显示中文 | `index.html:643` | `task_workflow()` 得到 `list/countdown/detail` | ✔ | string | ✔ |
| `cfg.console.*`、`cfg.console.target.name` | `index.html:1023-1033` | `server.py:382-396` 写入的 `console` | ✔ | `min/max/quota/budget` 是 **float**（`float()`），前端写进 `<input type=number>` 无碍 | `console.craft` 的合法值是 `极品/优品`，但被回填进只有 `传说/史诗/稀有/普通` 的 `#fTier` → select 值被浏览器清空（H4） |
| `cfg.dry_run` | `index.html:873` | `config.py:517` | ✔ | bool | 这是**有效值**，与开关的 raw `value` 不同（H1） |
| `r.checks[i].id/label/ok/warn/detail` | `index.html:976-1006` | `preflight.evaluate()` | ✔ | `detail` 是 string | ✔ |
| `st.tries/hits/spent/rate` | `index.html:564-566` | `records.stats()` | ✔ | number | `rate` 是 0-100 的数（直接拼 `%`） |
| `x.t/name/price/elapsed/result/shot/verified` | `index.html:572-588` | `records.load()` 行 | ✔ | `verified` 可能为 `null`（旧记录），`=== false` 不会误判 | ✔ |
| `r.applied[key]` | `index.html:929` | `switches.apply_all()` 返回强转后的值 | ✔ | bool/number/string 与控件一致 | ✔ |
| `r.after`（follow） | `index.html:964` | `actions.set_follow()` | ✔ | bool | ✔ |
| `r.offset`（timesync） | `index.html:1042` | `timesync.sync()` | ✔ | number（秒） | 前端 `*1000` 转毫秒 ✔ |
| `r.config_ok / r.config_error / r.dry_run_source` | **未读取** | `config.py:513-517`、`scheduler.py:193,513-514` | 存在但被丢弃 | — | 后端为「读配置失败」专门加的字段，前端没用（M3） |

---

## 3. 问题清单

### 高

**H1 演练开关显示「开」，实际是真点购买**
- 级别：高
- 位置：`web/index.html:812`（`api('/api/scheduler/start', {})` 不传 `dry_run`）；`web/index.html:871-876`（`isDry()` 读 `cfg.dry_run`）；`web/index.html:881-882,904-905`（开关渲染 raw `value` + 「当前生效」小字）；`core/config.py:86-87`（默认 `run_mode:"auto"`、`dry_run:True`）；`core/config.py:228-232`（`auto/semi` → 返回 `False,"run_mode=auto"`）
- 现象：设置页「演练模式」开关是亮的，「当前生效：实战（run_mode=auto）」只是行内小字；抢购页按钮文案取决于 `cfg.dry_run`（=False）所以显示「开始抢购」；点下去引擎真的发点击。用户把「开关亮着」理解成「在演练」，整套连招、点购买都会真的发生。
- 触发条件：任何一次「从没改过执行模式」的默认配置（`run_mode` 默认 `auto`）。只要 `run_mode` 是 `auto/semi`，翻 `env.dry_run` 开关对它完全没有作用，`saveSwitch()` 里重载 `loadConfig()` 后按钮文案也不变。
- 修复方向：`startRun()` 里显式带 `dry_run: isDry()`；或者把「演练模式」开关直接接到 `run_mode`（`dry`/`auto`）而不是 `dry_run`，并在开关旁把「raw 值 / 有效值」两个口径做成一个。若保持双口径，至少让 `resolve_dry_run` 在 `run_mode=auto` 时也不忽略 `dry_run=True`。

**H2 换目标后旧任务留在队列里，且 `start` 有「复活已完成任务」的分支**
- 级别：高
- 位置：`web/index.html:795-801`（`mine = tasks.filter(t => (t.name||'')===name)`，匹配不到就 `add`）；`web/index.html:790-794`（注释自称「控制台只维护一条」）；`core/scheduler.py:413-422`（当队列里没有 `PENDING/ARMED` 且 `enabled` 的任务时，把**全部** `DONE/FAILED/SKIPPED` 的启用任务重置为 `PENDING`）；`core/scheduler.py:246-256,274-277`（`add` 的 `status=PENDING`、`quota>=1`）
- 现象：先选皮肤 A 开始 → 队列里一条 `name="A"`（`done`）；再选皮肤 B 点开始 → 队列变成 `A` + `B` 两条，控制台「队列」页会显示两条，用户以为自己只维护了一条。平时 `B` 是 pending 所以只跑 B，但一旦当前那条被停用（勾掉「启用」）或已经被清成不可跑状态，`start()` 的复活分支会把 A 重置成 pending 并重新开抢旧目标 —— 一条 `done` 的任务带着 `bought/quota` 的残余计数重新进入购买流程。
- 触发条件：换过目标之后，把当前任务取消勾选（或它已 failed/skipped）再点「开始抢购」；或两次开抢之间队列里没有 pending。
- 修复方向：`syncTask()` 在换目标时 `remove` 旧的控制台任务（按「非本次 name 的、由控制台创建的任务」清理），或由后端提供 `replace` 语义；`scheduler.start()` 的复活只作用于「本次被更新过的那一条」，不要批量重置。

**H3 设置页与抢购页双写同一批字段，互相覆盖**
- 级别：高
- 位置：写入口 A：`web/index.html:925-934`（`saveSwitch` → `/api/switches`，`core/switches.py:79-95` 的 `price.min/price.max/price.quota/price.budget` 路径是 `console.min_price` 等）；写入口 B：`web/index.html:757-770,776`（`condPayload()` → `POST /api/config {console}`）；回填只在这里：`web/index.html:1020-1034`（只在 boot 与 `dry_run/run.mode` 变更时调用）
- 现象：在设置页把「最低价」改成 500 → `console.min_price=500`，抢购页 `#fMin` 仍显示旧值 100（不会刷新）；接着点「开始抢购」→ `startRun()` 先 `saveConditions()` 把 100 写回去，用户刚设的 500 被静默覆盖。反向也一样。
- 触发条件：任意一次「先在设置页改价格/限量/预算，再去抢购页点开始」。
- 修复方向：二选一。①设置页与抢购页共用同一份表单状态，`saveSwitch` 成功后统一 `loadConfig()` 回填所有控件；②去掉重复控件，`price.*` 开关页只做只读展示。同一状态不要有两个写入源。

**H4 「品阶」写进了 `craft`，值与后端期望的「做工」枚举不兼容**
- 级别：高
- 位置：`web/index.html:287-290`（`#fTier` 选项 `传说/史诗/稀有/普通`）；`web/index.html:767-768`（`out.craft = tier`）；`web/index.html:1029`（`if(c.craft) $('#fTier').value = c.craft`）；`core/bricks.py:189-192`（`parse_craft` 只返回 `极品/优品`）；`core/market.py:484-491`（`row["craft"] != craft` 就整行拒）；`core/switches.py:99-102`（同一个「做工」开关的合法值是 `极品/优品`）
- 现象：选了品阶 → 任务 `craft="传说"`；OCR 读出卡片做工 `极品/优品` 时，`market.check()` 每一行都回「做工不是传说」，一条都买不到；OCR 读不出做工（`None`）时又整行放行，过滤等于随机生效。同一个字段在设置页叫「做工」、在抢购页叫「品阶」，值域完全不同。另外把 `console.craft="优品"` 回填进 `#fTier` 会得到一个空 select（浏览器丢弃非法 option），过滤条件被静默清掉。
- 触发条件：在抢购页选任一品阶后开抢。
- 修复方向：`#fTier` 改成独立的 `tier` 字段（后端 `_task_update` 目前只认 `craft/wear/season/max_wear` 等，需要新增 `tier` 或映射到 `filter_defaults.tier`），`craft` 下拉统一用 `极品/优品/不限`；`condPayload()` 不再把品阶塞进 `craft`。

### 中

**M1 清空成色/品阶时旧值不会清除**
- 级别：中
- 位置：`web/index.html:765-768`（只有非空才放进 payload）；`core/server.py:390-392`（`_CONSOLE_TEXT` 只覆盖「请求里出现的键」）；`core/server.py:579-583`（`_task_update` 同理）
- 现象：先选 `S`，再改回「不限」→ `wear` 键根本不发送 → `console.wear` 和任务上的 `wear` 都还是 `S`，界面显示不限、引擎按 S 过滤。
- 触发条件：任何一次「先设条件、后清空」。
- 修复方向：`condPayload()` 始终发送 `wear/craft`（空串也发），后端 `str(... or "")` 已能处理空串。

**M2 启动前检查固定按 `list` 路线判定，与真实任务路线不一致**
- 级别：中
- 位置：`web/index.html:975`（`api('/api/preflight', {mode:'list'})` 硬编码）；`core/server.py:617-626,797-807`（后端支持 `workflow` 参数，且注释明确说「以前写死 list 是坑」）；`core/server.py:772-794`（`_next_task_route()` 按真实任务推导）、`826-842`（`_preflight_gate` 用同一套）
- 现象：用户选「倒计时」或「详情页」路线时，设置页的检查项仍按「列表区域/列表模板」判定 → 检查区可能是绿的；点开始被 `_preflight_gate` 拦下回「启动前检查没过：…」，两个界面结论相反。
- 触发条件：`#fWorkflow` 不是 `list`。
- 修复方向：`loadPreflight()` 传 `workflow: $('#fWorkflow').value`（后端已支持），或直接 `GET` 用一个「按队列推导」的参数。

**M3 后端专为 UI 加的 `config_ok / config_error` 前端一个都没读**
- 级别：中
- 位置：`core/server.py:511-514`（注释：界面据此显示「配置读不出来」）；`core/scheduler.py:186-193`、`502-518`；`web/index.html:1020-1034`（只取 `console`）；`web/index.html:647-651`（空队列直接显示「队列是空的」）；`web/index.html:674-676`（`loadTasks` 忽略返回体）
- 现象：`config.json` 读不出来/被别的进程占着时，`scheduler._read_queue()` 回的是上一次快照或空队列，界面显示「队列是空的 —— 点开始会自动建一条」，用户以为任务被删了，实际是配置文件读失败（后端自己会在 `start` 时拒绝并给出原因 `scheduler.py:397-401`）。
- 触发条件：`config.json` 损坏/被锁。
- 修复方向：`loadConfig/loadTasks/poll` 读到 `config_ok===false` 时在状态条显示 `config_error`，并把「队列是空的」文案换成「配置读失败」。

**M4 「已买」统计读的是点击次数**
- 级别：中
- 位置：`web/index.html:333`（标签「已买」）、`web/index.html:607`（`stats.clicks`）；`core/engine.py:139-149`（`_stats` 初值没有 `bought`）、`611`（成交后才 `_set(bought=...)`)
- 现象：`clicks` 是「真的按下去的鼠标次数」（`engine.py:1629-1631`、`2008-2010`），没抢到时可能连点几十次 → 状态条写「已买 37」而实际 0 件；成交时 `bought` 才是件数。演练模式反而不累加（`engine.py:1611-1616`、`1986-1991` 提前返回），所以演练时永远是「已买 0」，也不像「在模拟」。字段类型都对，是**语义**错位。
- 触发条件：任意一次点了没抢到（连点重试），或一轮里抢到多件。
- 修复方向：改用 `stats.bought || 0`，或把标签改成「点击 N 次」。

**M5 停止按钮不看后端返回，永远显示「已停止」**
- 级别：中
- 位置：`web/index.html:827-833`（只 `await` 后无条件 `notice('已停止')`）；`core/server.py:655-657`；`core/scheduler.py:462-465`（幂等分支返回 `(True,"正在停止")`）
- 现象：连点两次停止、或引擎还在收尾（join 中）时，界面已经写「已停止」，实际还在跑；`poll()` 紧接着又把按钮恢复成「停止可用」，用户以为没停。
- 触发条件：停止未完成时再点一次，或点停止后 1 秒内看状态。
- 修复方向：按 `r.ok` 和 `r.msg` 显示（`已停止` / `正在停止`）。

**M6 启停按钮用 `engine.running`，忽略调度器 `running`**
- 级别：中
- 位置：`web/index.html:600-610`（`const running = !!eng.running` 决定 `btnStart/btnStop`）；`core/scheduler.py:502-518`（`running`（调度器线程）与 `engine.running` 是两个量）
- 现象：倒计时任务在等待 `fire_at`、或两条任务之间的间隙，引擎没起但调度器在跑 → 界面「待机」、开始可点、停止不可点；点开始回 `{ok:false,msg:"调度器已在运行"}`，看起来像「点了没反应」。
- 触发条件：倒计时路线、队列里有多条任务。
- 修复方向：`running = !!st.running || !!eng.running`，按钮状态与「谁在跑」解耦。

**M7 每秒重建任务表，勾选/滚动/焦点被旧快照覆盖**
- 级别：中
- 位置：`web/index.html:615`（每次 `poll` 调 `renderTasks`）、`654-669`（整表 `innerHTML`）；`web/index.html:687-691`（勾选后 `POST`，无刷新、无 catch）
- 现象：点「启用」复选框 → POST 尚未返回时若跨过一次 `poll`（1s），整表重绘把复选框刷回旧值；用户看到「勾了又自己弹回去」。同理表格滚动位置、焦点每秒丢失。
- 触发条件：1 秒内完成一次勾选/查看。
- 修复方向：`renderTasks` 做 diff（只更新变化的单元格），或在轮询里跳过用户正在操作的行；提交成功后本地更新再重绘。

**M8 条件保存失败完全静默**
- 级别：中
- 位置：`web/index.html:781`、`785`（`saveConditions().catch(() => {})`）；`core/server.py:366-398`（`Config.transaction()` 抛异常 → `_guard` 500 `server.py:312-318`）
- 现象：`config.json` 不可写（被占用、权限、磁盘满）时，输入框照常改、界面上没有任何提示；关掉再开条件全丢，`startRun` 里 `await saveConditions()` 抛错还会被归到「启动失败」提示里（`818-819`），用户以为是抢购启动失败。
- 触发条件：配置文件写失败。
- 修复方向：捕获后显示「条件未保存」并标红，`startRun` 里把保存失败与启动失败分开提示。

**M9 记录页「共 N 次尝试」与表格行数不一致**
- 级别：中（偏低）
- 位置：`web/index.html:556`（`limit=200`）、`564-566`（`st.tries` 是全量）；`core/server.py:239-247`、`core/records.py` 的 `stats()`（独立全量扫描）
- 现象：表格只显示最近 200 行，统计写「共 500 次尝试」，对不上；用户无法判断是不是被截断。
- 触发条件：记录超过 200 条。
- 修复方向：文案改成「最近 200 条 / 全量 N 次」，或分页。

### 低

**L1 响应包结构不统一（`ok` 时有时无）** — `core/server.py:214,230,234`（`/api/config`、`/api/tasks`、`/api/scheduler` 无 `ok`）vs 其余接口都有 `ok`。前端靠各自约定读取，`api()` 只判 HTTP 状态（`web/index.html:448-455`）。新增接口时容易再踩一次「200 + 无 ok」。修复方向：统一信封，或至少在 `api()` 里对缺少 `ok` 的响应做兼容。

**L2 坏 JSON 请求体被当成空对象** — `core/server.py:193-200`：`json.loads` 失败直接 `return {}`，于是 `POST /api/config` 带半截 JSON 会回 `{"ok":true}` 而什么都没存；`POST /api/tasks/add` 会变成「加一条不限条件的任务」。修复方向：解析失败返回 400 `{ok:false,msg:"body 不是合法 JSON"}`。

**L3 删除任务无错误处理，后端永远回成功** — `web/index.html:683`（`await api(...)` 无 try/catch，失败是未捕获的 Promise 拒绝）；`core/server.py:565-567`（`removed` 为 `False` 也回 `ok:true`）。现象：删不掉时界面无提示，行还在。修复方向：读 `removed`，失败给提示。

**L4 开关保存失败时 UI 已先行翻转** — `web/index.html:940-945`（先 `classList.toggle` 再 `saveSwitch`）、`928`（失败只弹提示不回滚）。现象：保存失败后开关停在错误位置，下次 `poll` 也不会纠正（`renderSwitches` 只在 `loadSwitches` 时跑）。修复方向：失败回滚 DOM 或重新 `loadSwitches()`。

**L5 一批接口与静态资源是死的** — 无前端调用的接口见 §2.1 末尾；`/assets/*` 路由（`core/server.py:268-269,331-348`）与 `web/assets/` 下的 `df-bg.jpg`、`df-logo.png` 在 `web/index.html` 里**没有任何引用**（全文件仅 1 处 `fetch`，无 `assets/`、无 `<img>`）。因此「非根路径访问 `web/assets/*` 会不会 404」在当前代码下不会发生，但也意味着这批资源与路由是遗留物。修复方向：删除或真正引用。

**L6 「位置」列显示的是中心 y 像素** — `web/index.html:524`（`Math.round(r.cy)`）；`core/engine.py:1339`（`"cy": round(float(r.get("cy") or 0),1)`）。字段存在、类型对，但语义不是「第几个位置」，值域是屏幕像素（如 640）。修复方向：改成序号或标题写明「Y 坐标」。

**L7 `_static` 只服务 `index.html`** — `core/server.py:211-212,324-329`（`GET /` 与 `/index.html` 都指向同一文件，其余路径 404）。当前页面所有请求都是绝对路径（`/api/...`、`/shots/...`，`web/index.html:452,578`），所以非根路径访问也不会打空主机名；但以后加 `<script src="/app.js">` 会直接 404。修复方向：给 `_static` 加目录级白名单。

**L8 全文件没有一个 `console.*`，静默 `catch` 共 6 处（全部 `catch(e){}` 或 `catch(e){}` 注释）** — 位置清单：`web/index.html:750`（清空目标失败静默，且随后无条件把界面置空 → 后端 `console.target` 仍留着旧目标）、`922`（`loadSwitches`，注释「后端没起时静默」）、`1050`（`loadCatalog` 失败 → 赛季下拉为空、界面无提示）、`1052`（`loadConfig` 失败 → 表单全空、`STATE.cfg` 为 `null`，`isDry()` 退化到读 raw 开关）、`676`（`loadTasks`）、`781/785`（条件保存，见 M8）；另有 `683`、`691` 两处 `await` 完全无 catch（未捕获拒绝）。要求里说的「catch 只 `console.log`」在这里更彻底 —— 是空 catch，连日志都没有。修复方向：统一走一个 `reportError(scope, e)`，至少写进状态条与 `data/log`。

**L9 轮询失败只改一行小字，无退避** — `web/index.html:625-627`（catch 里只写「连接断开」）。定时器本身是 `setInterval(poll,1000)`（`1059-1061`），没有泄漏（`?frozen=1` 会跳过轮询，`1069` 也一样）；但后端挂掉时用户只看到底部「连接断开」，不知道原因，且每 1000ms 继续打。

---

## 4. 静态页与桌面壳核对（对应要求第 4 条）

| 检查项 | 结论 | 依据 |
| --- | --- | --- |
| `GET /` 路由 | ✔ 只映射 `web/index.html` | `core/server.py:211-212` |
| `Content-Type` | ✔ `text/html; charset=utf-8` | `core/server.py:212,324-329` |
| 缓存头 | ✔ 所有响应统一 `Cache-Control: no-store` | `core/server.py:180` |
| 文件不存在 | ✔ 404 + `text/plain`，不会抛异常 | `core/server.py:326-327` |
| `web/assets/*` 相对路径 | ✔ 不是问题：页面里没有任何 `assets/` 引用；即便有，`/assets/` 也是绝对路径且后端有白名单路由 | `core/server.py:268-269,331-348`；`web/index.html` 中 grep `assets/` 无命中 |
| `pywebview` 加载 URL 还是本地文件 | ✔ **URL**：`f"http://{args.host}:{port}"`，不是 `file://`，所以 `fetch('/api/...')` 不会打空主机名 | `desktop.py:220-221,267-273` |
| 端口就绪 | ✔ 先 `connect_ex` 轮询再建窗口 | `desktop.py:228-237` |
| 截图链路 | ✔ `/shots/<encodeURIComponent(name)>` → 后端 `unquote` + `normpath` + `.jpg` 白名单 | `web/index.html:577-579`；`core/server.py:265-266,350-364` |
| 新增风险 | 图片/静态资源同样被 `no-store`，每次「看图」都会重新读盘；200 张截图反复浏览有 IO 浪费（低） | `core/server.py:180,363-364` |

---

## 5. 未验证项

1. **`window.confirm` 在无边框 pywebview 里是否可用（推测）**：`web/index.html` 里没有 `confirm` 调用（grep 无命中），但以后加确认框需实测 WebView2 的弹窗策略。无法证实的原因：需要真实桌面环境 + `pywebview` 运行时。
2. **`craft` 过滤在实际数据里是否真的全部拦掉（推测）**：取决于 OCR 是否能从列表卡片读出 `做工`。`bricks.parse_craft` 需要「极品/优品」字样出现在 `row["text"]` 里；真实列表页是否带这段文字无法从代码证实，需要一次真实扫盘日志（看 `market.check` 的拒绝原因）。
3. **`/api/scheduler` 每秒调用的实际开销**：`Config.load()` + `timesync.info()` + `engine.status()` 每秒一次，代码上看没有缓存；具体 CPU/磁盘影响需要在本机 `perfmon` 或 `py-spy` 上看，静态分析给不出数字。
4. **`?frozen=1` 是否有意面向前端**：仅 `tools/probe_frontend.py:79` 与 `web/index.html:1059,1069` 使用，未在文档/注释里说明用途；是否存在「用户手动加 `?frozen` 后界面永不刷新」的可用性坑，需要产品侧确认。
