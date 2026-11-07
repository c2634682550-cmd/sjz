# 前后端契约专项审查 · R07

- 对象：`web/index.html`（单文件前端，3278 行）、`core/server.py`、`core/scheduler.py`、`core/config.py`、`core/switches.py`、`core/timesync.py`、`tools/fe_audit.py`
- 目标：找「假按钮」——界面上有、点下去后端没反应，或者有反应但没人处理（前端读错字段、把失败当成功、结果被轮询覆盖）
- 方式：把 `dafors` 复制到隔离目录（`core`/`web`/`config.json`/`data/records.jsonl`），**没有改动仓库里的 `config.json`（本仓库没有 `config.yaml`）**；用真实 `Handler` 起在环回随机端口（不起 OCR / 不注册热键 / 不起调度循环 / 不开浏览器窗口），再用无头 Chromium 加载真实页面逐控件取证。
- 结论：**前端自测的 154 项全过不代表契约成立**。`tools/fe_audit.py` 的 mock 把后端「理想化」了：`/api/hotkey` 凭空多给 `ok:true`、`/api/config` 凭空多给 `version`、`/api/switches` 只造 bool/int 项、所有响应恒为 HTTP 200。以下问题在该 mock 下永远测不出来。

## 总览

| # | 严重度 | 控件 / 接口 | 一句话 |
|---|---|---|---|
| 1 | 高 | 任务「保存设置」→ `/api/tasks/update` | 磨损上限每次保存都被后端强制清 0 |
| 2 | 高 | 目标库「加入方案」/「添加任务」→ `/api/tasks/add` | 同名同路线不新增，整条覆盖并重置价格/数量/预算 |
| 3 | 高 | 设置页 8 个下拉开关 → `/api/switches` | 全部渲染成数字框，选了也改不动 |
| 4 | 高 | 设置 → 热键（`/api/hotkey`） | GET 无 `ok`，整页永不回填，保存必失败 |
| 5 | 高 | 设置 → 控制台「读取」（`/api/config`） | GET 无 `ok`，永远失败并弹 `[object Object]` |
| 6 | 高 | 运行页任务勾选 + 批量启用/停用 | 勾选被 1 秒轮询清空，批量退化为「全部」 |
| 7 | 高 | 新增任务「关键词」→ `/api/tasks/add` | 字符串被后端按列表迭代，关键词碎成单字 |
| 8 | 高 | 全局 `api()` | 所有 4xx 的后端错误文案被丢掉，只剩通用文案 |
| 9 | 中 | 运行页「全部类型」筛选 | 读不存在的 `season.weapons`，选项永远只有「全部类型」 |
| 10 | 中 | 已完成任务重跑 | 后端让点「重跑」，前端没有这个按钮 |
| 11 | 中 | 仪表卡「时间校准」 | 从未校准时显示「本机快 0 毫秒」 |
| 12 | 中 | 删除任务 | 删不存在的 id 也提示「任务已删除」 |
| 13 | 中 | 全局请求 / 轮询 | 无超时；一次挂起会让 `S.saving` 永久为真、轮询永久停摆 |
| 14 | 中 | 关于页 / 状态栏版本号 | `config.public()` 无 `version`，永远 `—` / `v—` |
| 15 | 中 | 目标库「加入方案」的成色 | `tier` 是品阶（传说/史诗…），`craft` 只认极品/优品，永远传空 |
| 16 | 低 | 热键开关按钮 | `aria-checked` 只在关闭时更新，视觉开而语义 false |
| 17 | 低 | 任务表每秒重绘 | 勾选之外，滚动位置/焦点/hover 也每秒被重置 |
| 18 | 低 | 目标库加入的任务副标题 | `keywords=[]` 为真值，`'未设置关键词'` 兜底失效，副标题空白 |
| 19 | 低 | 刷新与本地状态 | 仅 density 持久化；页签/筛选/选中/未保存表单全丢，无离开提示 |
| 20 | 低 | 双击提交 | `withBusy` 只计数不拦截，靠后端幂等兜底 |
| 21 | 低 | `data-act="task-edit"` | 分发器无此 case，靠 `#setForm` 另一套监听兜底 |

---

## 详细

### 1. 高 · 任务「保存设置」把磨损上限清零

- 控件：运行页任务表单 `#setWearMax` → `POST /api/tasks/update`
- 位置：[index.html#L2226](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2226)（`saveTask`）、[index.html#L2191](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2191)（回填 `t.max_wear || ''`）、[server.py#L618-L628](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L618-L628)
- 实际现象：表单填 `0.05` → 点保存 → toast「任务已保存」→ 重新读 `/api/tasks` 得 `max_wear: 0.0`，表单回填为空。
- 根因：`_task_update` 只把 `min_price/max_price/quota/budget/craft` 交给 `check_conditions`，但 `check_conditions` 返回的是**整份条件字典**（含默认 `max_wear: 0.0`），随后 `for k,v in clean.items(): if k in fields: fields[k]=v` 把用户传来的 `max_wear` 覆盖成 0。前端每次保存都带 `max_wear`，所以必现。
- 影响：磨损上限（0=不限）实际上永远存不进；用户以为设了上限，实际按不限磨损抢。

### 2. 高 · 同名同路线「添加」= 静默覆盖并重置整条任务

- 控件：目标库卡片「＋加入方案」[index.html#L2802](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2802)、[libAdd #L2814-L2824](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2814-L2824)；新增对话框 `#addOk` → [addTask #L2315-L2333](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2315-L2333)
- 位置：[scheduler.py#L308-L320](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L308-L320)
- 实际现象（实测）：先建 `探针A`（quota=1，价格 100–500），再对同名同路线加一次 → 返回**同一个 id**，`quota` 变 2，`min_price/max_price` 归 0，`max_wear/craft/wear/fire_at/enabled` 一并回到默认；前端仍提示「已加入方案：探针A」/「任务已添加」。
- 影响：用户调好的价格、限量、预算、成色被无声清掉；`bought/spent` 保留、状态沿用旧值（done 仍是 done），随后点「开始」会得到「队列里只剩已完成的任务」。

### 3. 高 · 设置页所有 select 类开关渲染成数字框

- 控件：设置页「运行模式 / 价格与筛选 / 抢购设置 / 截前 / 环境与安全」里的下拉项
- 位置：[index.html#L2110](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2110)（`it.kind === 'choice'`）、[index.html#L2055-L2067](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2055-L2067)（`numHtml` / `selectHtml`）、[switches.py#L12](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py#L12)、[switches.py#L382](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py#L382)
- 实际现象（无头实测）：`#swRunMode` 里「抢购路线」是 `<input type="number" data-kind="select">`，值为空。
  涉及 8 项：`run.workflow`（抢购路线）、`filter_defaults.craft`、`filter_defaults.sort`、`filter_defaults.pub`、`filter_defaults.ons_sort`、`premarket.hotkey`（盘前热键）、`input_mode`（输入通道）、`input_fallback`（通道降级）。
- 二次问题：就算把判断改成 `'select'`，`selectHtml` 也读不了后端的选项——后端 `options` 是 `["countdown","倒计时"]` 这样的数组，前端按 `o.value / o.label` 读对象，会渲染成 `value="undefined"`。
- 用户影响：输入通道、通道降级这类关键项无法在界面切换；输入数字后 `_coerce` 判定「不在选项里」→ 回默认值，但 `applySwitch` 仍 toast「已保存：抢购路线」。**保存成功是假的。**

### 4. 高 · 热键页整体不可用

- 控件：设置 → 热键；`#hkStartSpec` / `#hkStopSpec` / 「保存热键」
- 位置：[index.html#L3019-L3022](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3019-L3022)、[server.py#L243-L246](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L243-L246)、[server.py#L741-L753](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L741-L753)
- 实际现象（无头实测）：两个输入框都是空串，两个开关显示 `switch on`，`#hkMsg` 为空；点「保存热键」只得到「两个热键都要填，例如 F7、F8 或 Ctrl+Shift+A。」
- 根因：`GET /api/hotkey` 返回 `{"stop":..., "start":...}`，**没有 `ok` 字段**；`loadHotkey` 第 3022 行 `if (!r.ok) return;` 直接 return，后面的回填/状态刷新全被跳过。
- 影响：热键配置只能改 `config.json`；界面上的开关是纯装饰。

### 5. 高 · 控制台「读取」配置永远失败并弹 `[object Object]`

- 控件：设置 → 控制台 →「读取」`[data-act="cfg-refresh"]`
- 位置：[index.html#L1429](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1429)、[index.html#L3171-L3175](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3171-L3175)、[config.py#L557-L564](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L557-L564)
- 实际现象（无头实测）：点击后 `#cfgView` 仍是「点「读取」查看」，toast 显示 `[object Object]`。
- 根因：`Config.public()` 不含 `ok`；`loadConfigView` 用 `if (!r.ok)` 判失败，进入 `toast(humanize(r), 'err')`，而 `r` 是配置对象（`String(obj)`）→ `[object Object]`。
- 影响：配置快照读取功能完全不可用，且错误信息不可读。

### 6. 高 · 勾选被轮询清空，批量操作退化成「全部」

- 控件：任务表行勾选框、`#chkAll`、「批量启用」`[data-act="bulk-on"]`
- 位置：[index.html#L2004-L2005](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2004-L2005)、[index.html#L2461-L2465](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2461-L2465)、[index.html#L2266-L2272](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2266-L2272)
- 实际现象（无头实测）：点选一条 `checked=true`，等 1.6 秒后再读 `false`；此时 `bulk` 收集到的 ids 为 `[]`。
- 根因：空闲态每秒 `poll → paintRunIdle → renderTaskTable()`，整表 `innerHTML` 重写并 `#chkAll.checked=false`；而 `bulk()` 在 `ids.length===0` 时把 target 换成 `S.tasks` 全部。
- 影响：想「批量启用两条」→ 实际把**所有**任务（含已手动停用的）打开；同一根因还会每秒重置表格滚动位置与焦点。

### 7. 高 · 关键词以字符串发给按列表解析的后端

- 控件：新增任务对话框 `#addKw` → 「添加」
- 位置：[index.html#L2325](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2325)（`keywords: kw`）、[server.py#L431](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L431)、[scheduler.py#L268](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L268)
- 实际现象（实测）：提交 `"棱镜 攻势"`，落库为 `["棱","镜"," ","攻","势"]`；任务表副标题显示 `棱,镜, ,攻,势`。
- 影响：关键词匹配变成「任一单字命中」，几乎必中；该任务的筛选与「抢对东西」能力失效。同接口用数组 `["棱镜","攻势"]` 则正常，说明纯类型契约不符。

### 8. 高 · 所有 4xx 的后端提示被前端吞掉

- 控件：全局 `api()`，以及所有用它的按钮（添加任务、定位、关注、校时、删除、更新…）
- 位置：[index.html#L1594-L1602](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1594-L1602)
- 实际现象（无头实测）：未框选购买按钮时点「添加」，后端 400 的 `msg` 是「先把购买按钮框选好再添加任务」，界面 `#addMsg` 只显示「这一项暂时没成功，稍后再试一次」。
- 影响面（均为 200 以外的真实提示）：
  - `/api/tasks/add` 400「先把购买按钮框选好再添加任务」「列表模式要先框选交易行列表区域」
  - `/api/skins/locate` 409「正在抢购，先停掉再定位」
  - `/api/tasks/update` 400「条件不合法：…」/ 404「没有这条任务」
  - `/api/timesync` 429「上一次校时还没完成」
- 结果：用户看得见「失败」，但永远不知道为什么。

### 9. 中 · 「全部类型」筛选永远只有一项

- 控件：运行页 `#fltType`
- 位置：[index.html#L2017](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2017)、[server.py#L483-L520](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L483-L520)
- 实际现象（无头实测）：`#fltType` 的 options 只有 `["|全部类型"]`；目标库页的类型 chip 却完整（`.357左轮手枪`、`AK-12突击步枪`…）。
- 根因：`/api/skins` 每个赛季只有 `season / season_name / series / tiers`，没有 `weapons`；武器名在 `tiers[*].weapon`。前端 `.flatMap(s => s.weapons || [])` 永远为空。
- 附带：`visibleTasks` 的类型匹配用 `t.name.includes(type)`（[#L1937](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1937)），而不是比对 `weapon`，即使选项补上也会漏筛。

### 10. 中 · 已完成任务无法从界面重跑

- 控件：运行页「开始抢购」
- 位置：[scheduler.py#L455-L456](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L455-L456)
- 实际现象：队列里只剩 `done` 时，后端提示「要重跑请先在队列页点「重跑」并确认」；但 `index.html` 全文没有「重跑」控件（`data-act` 取值集合里也没有 `rerun`）。
- 唯一可行路径是改任意字段再点「保存设置」（`saveTask` 固定带 `status:'pending'`），既隐蔽又与文案矛盾。

### 11. 中 · 时间校准卡在未校准时显示「本机快 0 毫秒」

- 控件：仪表卡「时间校准」、设置 → 识别与校时 `#tsOffset`
- 位置：[index.html#L2374-L2377](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2374-L2377)、[index.html#L3063](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3063)、[timesync.py#L19](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py#L19)、[timesync.py#L94-L102](file:///C:/Users/26346/Desktop/project/dafors/core/timesync.py#L94-L102)
- 实际现象（无头实测）：卡片为「时间校准 / 本机快 0 毫秒 / 重新校准」，`#tsOffset` 显示 `0`。
- 根因：`_state["offset"]` 初值是 `0.0`、过期也返回 `0.0`，前端用 `offset != null` 判「已校准」，于是「还没校准」分支永远走不到。同一页的 `#tsServer` 是诚实的（「还没有校准过」），两处口径不一致。

### 12. 中 · 删除不存在的任务仍提示成功

- 控件：「删除任务」、批量操作
- 位置：[server.py#L587-L589](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L587-L589)、[index.html#L2250-L2263](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2250-L2263)
- 实际现象（实测）：`POST /api/tasks/remove {"id":"does-not-exist"}` → `{"ok": true, "removed": 0}`；前端只判 `r.ok`，仍 toast「任务已删除」。
- 影响：多端/重复点击时看不见「没删掉」，队列与本地视图可能分叉。

### 13. 中 · 请求无超时，一次挂起就冻住轮询

- 控件：全局 `api()` / 一切「正在…」的按钮
- 位置：[index.html#L1585-L1602](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1585-L1602)、[index.html#L1793](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1793)、[index.html#L2226-L2248](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2226-L2248)
- 实际现象：`fetch` 没有 `AbortController`/超时；后端崩溃（连接拒绝）有降级（`noteFail → 离线`），但后端「卡住」（preflight/OCR 长阻塞）时请求永久 pending，`saveTask` 的 `S.saving` 停在 `true`，`poll` 第 1793 行 `if (S.saving) return;` 之后每秒空转。
- 影响：界面数据永久停止更新且不报错，看起来像「程序没反应」。

### 14. 中 · 版本号永远 `—` / `v—`

- 控件：设置 → 关于 `#aboutVer`、状态栏 `#sbVer`
- 位置：[index.html#L3016-L3017](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3016-L3017)、[config.py#L557-L564](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L557-L564)
- 实际现象（无头实测）：`aboutVer = "—"`，`sbVer = "v—"`。
- 根因：`Config.public()` 没有 `version` 字段（后端也没有任何版本常量写进配置）。

### 15. 中 · 「加入方案」永远传不出成色

- 控件：目标库卡片「＋」
- 位置：[index.html#L2818](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2818)、`core/bricks.json`（`tiers` 为 普通/稀有/史诗/传说/典藏）
- 实际现象：`craft: (tier === '极品' || tier === '优品') ? tier : ''` 恒为 `''`，因为 `tier` 取的是**品阶**，不是成色；`check_conditions` 的 `craft` 只接受 极品/优品。
- 影响：从目标库加入的任务永远「成色不限」；卡片上 `i.tier === '极品'` 的金色高亮也永不生效（目标库里没有「极品」这个 tier）。

### 16. 低 · 热键开关 `aria-checked` 只更新一半

- 位置：[index.html#L1868](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1868)
- 现象：`btn.classList.toggle('on') || btn.setAttribute(...)`——打开时 `toggle` 返回 `true`，短路，`aria-checked` 保持旧的 `false`；只有关闭时才写 `false`。视觉开、语义关。

### 17. 低 · 任务表每秒重绘的副作用

- 位置：[index.html#L2465](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2465)、[index.html#L2004](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L2004)
- 现象：空闲 1 秒一次 `innerHTML` 重写，除勾选外，滚动位置、键盘焦点、hover 全部重置；任务多时无法稳定选中第二屏的行。

### 18. 低 · 库加入的任务副标题空白

- 位置：[index.html#L1957](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1957)、[server.py#L431](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L431)
- 现象：`libAdd` 不传 `keywords`，落库为 `[]`；`t.keywords || t.note || '未设置关键词'` 中 `[]` 为真值，`esc([])` 得空串，兜底文案永远不出现。

### 19. 低 · 刷新与本地状态

- 现象：`localStorage` 只有 `dafors.density`（字符串，无结构升级风险）。刷新后页签、筛选、选中任务、未保存表单全部丢失，也没有 `beforeunload` 拦截；刷新会回到运行页第一条任务。

### 20. 低 · 无前端防抖

- 位置：[index.html#L1808-L1816](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1808-L1816)
- 现象：`withBusy` 只做计数与状态条，不阻断第二次点击；双击「开始」会发两次 POST，靠后端「已在运行/正在收尾」兜底；双击「添加」则触发第 2 条的覆盖逻辑。

### 21. 低 · `data-act="task-edit"` 没有对应分支

- 位置：[index.html#L1880 附近](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1880)
- 现象：文档级 `[data-act]` 分发器对 `task-edit` 无 case，静默返回；实际行为靠 `#setForm` 的独立监听。功能可用，但两套事件路径容易在后续改动中冲突。

---

## 为什么 154 项自测全过（mock 契约失真）

对照 [fe_audit.py](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py)：

| 行 | mock 行为 | 掩盖的真实问题 |
|---|---|---|
| [#L105-L107](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py#L105-L107) | `/api/hotkey` 返回 `{"ok": True, ...}` | 真实接口没有 `ok` → 热键页整页失效（#4） |
| [#L115-L116](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py#L115-L116) | `/api/config` 造了 `"version": "audit"`，且没有 `ok` | 真实接口既无 `version` 也无 `ok` → 读取失败 + 版本 `—`（#5/#14） |
| [#L77-L101](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py#L77-L101) | `/api/switches` 只有 `bool/int` 项 | 真实后端有 8 个 `select` 项 → 数字框（#3） |
| [#L209-L232](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py#L209-L232) | 所有 POST 恒 200，只有 `start` 会 `ok:false` | 4xx 文案丢失路径完全未覆盖（#8） |
| [#L131](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py#L131) | `keywords` 当数组下标取第 0 个 | 字符串类型错误不会被断言（#7） |
| [#L139-L140](file:///C:/Users/26346/Desktop/project/dafors/tools/fe_audit.py#L139-L140) | `/api/tasks/update` 支持 `values` 包裹 | 真实接口不接受 `values`（当前前端没用到，但 mock 与真源不一致） |

复核口径：它测的是「点得动、请求发出去了」；本次测的是「落库值对不对、界面显示对不对、失败有没有被当成功」。

---

## 确认没问题的项

1. 所有 `data-act` 都有真实后端路由，没有「前端有、后端无」的接口（逐项核对 `/api/tasks|scheduler|guard|skins|records|preflight|timesync|hotkey|follow|jobs|quit`）。
2. 所有 `data-gap="…"` 节点都带 `disabled`（脚本扫描 0 例外），文档级分发器还额外判 `b.disabled` → 「待接入」确实点不动，不会产生空请求。
3. `GET /api/records` 的 `rows` 与 `stats` 字段全部对得上（`hits/verified_hits/unverified_hits/spent/tries/median_s/rate/miss/no_money`、行内 `ts/name/price/result/verified/verified_by/elapsed/mode/shot`）。
4. `GET /api/scheduler` 的 `running/page{page,at,stale}/clock/tasks/log/engine/active/config_ok` 与前端读取一致。
5. `GET /api/guard` 的 `guard` 对象字段与 `paintException` 完全一致（`level/state/message/doing/goal/plan/attempts/history/button{id,label}/containment_pending/containment_ok/scene_text`）。
6. `/api/tasks` 任务对象字段与任务表单/表格读取一致（`id/name/keywords/season/wear/craft/max_wear/min_price/max_price/quota/budget/fire_at/workflow/enabled/status/bought/spent/result`）。
7. `/api/jobs` 与 `checkJob/trackJob` 字段一致（`current{id,name,state,elapsed}` / `job{state,elapsed,log,error}`）。
8. `/api/preflight` 的 `fix` 是对象 `{hint}`，与前端 `c.fix.hint` 一致（这条曾怀疑是字符串，实测证伪）。
9. 截图链接 `/shots/${encodeURIComponent(r.shot)}` 与服务端 `unquote` + `data/shots/<文件名>` 一致，无路径穿越/双编码问题。
10. 开关的 `change` 事件统一发 `{values:{key:value}}`，bool/int/float 三类都能正确落库并回读（只有 `select` 类坏，见 #3）。
11. 清空记录、清理已结束任务、删任务、退出都有二次确认（`ask()`），不会误触即执行。
12. 页面刷新后队列、记录、开关、皮肤目录都从后端重新拉取，不依赖前端缓存，不会显示陈旧数据。

## 无法验证的项

1. **真实抢购动作**：`/api/scheduler/start` 会真的操作游戏窗口（移动/点击），未执行，避免影响本机；只验证了请求体与错误分支。
2. **OCR / 游戏内定位链路**：`/api/skins/locate` 的 job 结果字段、`/api/preflight` 的真实检测项，需要游戏窗口与模板，只能验证接口形状。
3. **NTP 校时真实偏差**：只验证「未校准 = offset 0.0」的显示逻辑，未连外网校时。
4. **全局热键**：未实际注册/触发 F7/F8，只验证 GET/POST 契约；热键页的问题由 GET 缺 `ok` 即可判定，与注册结果无关。
5. **`/api/quit`**：会真正退出程序，未调用。
6. **`POST /api/config` 与 `/api/skins/plan`**：后端存在但前端完全未接线，只能确认「存在且可调用」，无法验证 UI 契约（这两条本身也说明前端漏接了功能）。

---

## 复核产物

- 隔离副本：`C:\Users\26346\Documents\Trae-CN\temp_20261107_19\dafors_probe`（`config.json` 为副本，仓库原件未改）
- 接口探针：`probe.py` / `probe2.py` → `probe_out.json`
- 无头 UI 探针：`ui_probe.py` → `ui_out.json`（Chromium headless，无可见窗口，`page_errors` 为空）
- 未修改任何源码，未 commit，未 push。
