# 前端审查 · 页面交互与前端状态机（只读）

审查对象：`web/index.html`（单文件 SPA，内联 JS）、`desktop.py`（pywebview 壳）、`core/server.py`（HTTP 后端）。
审查日期：2026-11-06。全程只读，未修改任何代码、未 commit、未 push。

## 结论（一句话）

**前端没有任何「进行中」的互斥状态：`start` 的禁用态同时被 `startRun`、`finally` 和每秒轮询三个写入者控制，轮询本身又没有 in-flight/序号保护，因此「双提交」和「旧响应覆盖新状态」都是必然可复现的，不是理论风险；其次是价格/数量输入缺乏有限数与区间校验（`1e309` 会静默变成「不限价」），再次是 `wearTag` 把 OCR 文本原样拼入 `innerHTML`。**

最关键的 3 条：

1. **高｜[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L805-L824) L805-824 / L609-610**：`btnStart` 的 `disabled` 有 3 个写入者，启动链路要串 4 个请求且服务端还要跑约 1 秒的 `preflight.collect()`（抓屏+OCR），这期间轮询会把按钮重新放开；两次快速点击可以让 `syncTask` 各读到空队列 → 加两条同名任务，`syncTask` 只改第一条。
2. **高｜[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L594-L628) L594-628 + L1060**：`setInterval(poll, 1000)` 不等待上一次返回，也没有序号；`start` 后立刻 `stop` 时，在途的旧 `status` 响应会把 pill、`btnStart`、`btnStop` 一起打回「运行中」。
3. **高｜[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L757-L770) L757-770 / L778-786**：`Number(value || 0)` 没有有限性/区间校验，`1e309` → `Infinity` → `JSON.stringify` → `null` → 引擎按 0 处理（**不限价**），但输入框仍显示 `1e309`；负数 `min_price`、负数 `budget` 也会照收。

## 基线

```
分支          main
HEAD          62d5e57  2026-11-05  test: 同步既有测试与审查/清理记录
git status    干净（审查开始与结束时 `git status --porcelain` 均为空）
origin        https://github.com/c2634682550-cmd/sjz
fork          https://github.com/2233admin/sjz
```
审查产出的本文件是新增未跟踪文件，未提交。

## 前端状态机与定时器（文字版）

```
                    ┌──────────────── boot (L1049-1064) ───────────────┐
                    │ loadCatalog → loadSwitches → loadConfig → poll() │
                    │ 非 ?frozen 时：setInterval(poll,1000)            │
                    │              schedulePreflight()                 │
                    └───────────────────────┬──────────────────────────┘
                                            ▼
   ┌───────────────┐   click #btnStart / F7    ┌──────────────────────────┐
   │  IDLE(待机)    │ ───────────────────────▶ │ STARTING（仅本地瞬时态）  │
   │ Start 可用     │                          │ disabled=true, notice    │
   │ Stop  禁用     │                          │ 正在启动…                │
   └───────────────┘                          └───────────┬──────────────┘
          ▲                                                │ 4 个串行请求：
          │                                                │ /api/config
          │                                                │ GET  /api/tasks
          │                                                │ POST /api/tasks/{update|add}
          │                                                │ POST /api/scheduler/start
          │                                                │ （服务端还要 preflight.collect ≈1s）
          │  失败/启动被拒 → notice(err,15s)                 ▼
          │                                        ┌──────────────────┐
          │                                        │ RUNNING（服务端派生）│
          │                                        │ Start disabled    │
          │                                        │ Stop  可用        │
          │                                        └──┬────────────┬──┘
          │      引擎线程退出 / 抢够 / 队列跑完          │            │ click #btnStop / F7
          └────────────────────────────────────────────┘            ▼
          ┌─────────────────────── STOPPING（仅本地瞬时态）───────────┐
          │ 无 disabled 保护；POST /api/scheduler/stop（join ≤3s）    │
          │ 结束后 notice('已停止') → poll()                          │
          └──────────────────────────┬───────────────────────────────┘
                                     └──▶ 回到 IDLE
```

关键事实：**RUNNING / IDLE 不是前端状态，而是每次 `poll()` 从 `/api/scheduler` 的 `engine.running` 现算出来的**（L598-613）；`STARTING / STOPPING` 只存在于单次函数调用栈里，没有变量记录、没有互斥。竞态的根因就在这里。

### 定时器清单

| 定时器 | 位置 | 间隔 | 何时启动 | 清理 / 停止 | 备注 |
|---|---|---|---|---|---|
| `setInterval(poll, 1000)` | L1060 | 1s | boot 末尾，除 `?frozen` 外 | **从不清理**；切页、停止、窗口隐藏都照跑 | 无 in-flight 锁，请求可叠加 |
| `schedulePreflight()` 递归 `setTimeout` 链 | L1068-1074 | 3s（有检查项失败）/ 20s（全绿） | boot L1061 | 无 `clearTimeout`；`?frozen` 时提前 return，链永久不续 | 只有一条链，`STATE.preflightBad` 决定下一跳间隔 |
| `saveTimer`（条件防抖） | L774-786 | 600ms | 价格/数量/预算 `input` | 每次 `input` 时 `clearTimeout` 重建 | 切页/启动不清，可能晚于 `startRun` 再发一次 `/api/config` |
| （后端）`PageWatcher` / 引擎扫描循环 | — | 1.5s / 50ms 级 | `serve()` | 后端负责 | 不在前端审查范围 |

### 按钮 `disabled` 的写入者

| 控件 | 写入点 | 说明 |
|---|---|---|
| `#btnStart` | L807（`startRun` 进函数即 true）、L820-822（`finally` **无条件** false）、L609（每秒 poll 按 `running` 覆盖） | 三个写入者，无优先级；`finally` 会在启动失败或「已启动但引擎尚未 running」时把按钮放开 |
| `#btnStop` | HTML L275 初始 `disabled`；L610（poll 控制） | 点击后**不** disable（L827-833），可连发多次 stop（后端幂等，但 notice 会被刷掉） |
| `#btnFollow/#btnUnfollow` | L959、L967 | 正确：进入 disable，`finally` 后恢复（无 `finally`，异常路径由 catch 兜住后仍恢复） |
| 队列 `.taskDel` / `.taskOn` | 无 | 点击期间不禁用，`renderTasks` 每秒重建会与点击互撞（见 F8） |

**「点击两次发两个请求」的窗口期有多宽？** `startRun` = `saveConditions`(1) + `GET /api/tasks`(1) + `POST /api/tasks/update|add`(1) + `POST /api/scheduler/start`(1)，其中最后一步服务端要跑 `_preflight_gate → preflight.collect()`；`core/preflight.py:276` 自己注明「抓屏 + OCR，约 1 秒」。也就是说从第一次点击到 `running` 变 true，量级在 **0.3~2 秒以上**（推测：未实跑计时），而 poll 每 1 秒就把按钮放开一次——**窗口内必然至少有一次「重新可点」的机会**。

## 问题清单

> 每条格式：级别 / 位置 / 现象 / 复现 / 修复方向。标「推测」的为未实跑验证。

### F1 · 高 · 启动可重复提交（双击 = 两个 start + 可能两条任务）

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L805-L824) L805-824（`startRun`）、L609-610（poll 改 disabled）、L789-803（`syncTask`）；[core/server.py](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L628-L653) L628-653；[core/scheduler.py](file:///C:/Users/26346/Desktop/project/dafors/core/scheduler.py#L386-L394) L386-394。
- 现象：`startRun` 只做 `btn.disabled = true`，随后 `finally` 无条件 `btn.disabled = false`；同一秒内 `poll()` 依据上一次 `/api/scheduler` 的 `running=false` 又把它设回可点。第二次点击会在第一次链路完成前后各发一遍 `saveConditions / syncTask / start`。两次 `syncTask` 的 `GET /api/tasks` 若都在「任务尚未落盘」前返回空队列，就各自 `POST /api/tasks/add` → 队列出现两条同名任务；之后 `syncTask` 用 `mine[0]` 只更新第一条（L798-800），另一条保留旧条件，`scheduler.start` 取第一条 `pending` 时可能跑到旧条件。第二次 `/api/scheduler/start` 则回「调度器已在运行」，界面弹红色 notice（体验上像「启动失败」）。
- 复现：双击「开始抢购」，或点一次后 1 秒内再点一次；随后看队列页是否出现两条同名任务、日志里是否有两条「执行模式」。
- 修复方向：模块级 `let starting = false`，入口即判即 return；`finally` 只 `poll()`、不直接改 `disabled`；poll 在 `STATE.pendingAction` 为真时**不写**按钮态；`syncTask` 改为按 `id` 精确更新或先 `remove` 同名多余任务。

### F2 · 高 · 轮询无 in-flight / 无序号，旧响应覆盖新状态

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L594-L628) L594-628、L1060、L827-833。
- 现象：`setInterval(poll,1000)` 不 `await`、不跳过飞行中的请求、不带序号。`stop` 时前端只发一次 `POST /stop`，但此时可能已有 1~2 个 `GET /api/scheduler` 在途；它们晚到的响应会执行 L602/L609/L610/L619，把 pill 改回「运行中」、`btnStart` 置灰、`btnStop` 置可用。用户看到的是「点了停止，按钮闪一下就变回去了」。
- 复现：点「开始抢购」→ 立刻点「停止」（或 F7 连按）；观察顶栏「游戏」pill 与两个按钮 1~2 秒内反复跳变。
- 修复方向：单一 in-flight 标志（`if(STATE.polling) return;`，`finally` 复位）；`STATE.pollSeq++`，响应回来时序号小于当前则整包丢弃；`stop/start` 成功后设 `STATE.transition={kind,until}`，该窗口内 poll 只更新数据、不写控件态。

### F3 · 高 · 数字条件无有限性/区间校验，提示值与实际生效值不一致

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L757-L770) L757-770（`condPayload`）、L778-786（防抖保存）、L948-954（开关数字项 `Number(raw)`）；[core/server.py](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L382-L396) L382-396（console 仅 `float()`）、L569-601（`/api/tasks/update` 不校验 `quota/budget/min_price`）；[core/engine.py](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1273-L1274) L1273-1274、L856-857。
- 现象：
  - `#fMin` 填 `1e309`：`Number` → `Infinity`；`JSON.stringify` 把它变成 `null`。走 `/api/config` 时 `float(None)` 抛 `TypeError` 被 `except` 吞掉（保留旧值），但同一个 payload 走 `syncTask → /api/tasks/update` 会把 `min_price` 写成 `null`，引擎 `float(task.get("min_price") or 0)` = 0 = **不限价**；输入框仍显示 `1e309`，队列页 `condText` 也显示 `价 0~…`。
  - 负数直接入库：`min_price = -1` 等于没有下限；`budget = -1` 让 `if(budget)` 成立且所有行 `spent+price <= -1` 不成立 → 全部「超预算」直接结束（engine L1344-1354），前端无任何提示。
  - `quota = 0` 被 `|| 1` 改写为 1（L761）；`quota = 2.5` 在 add 路径被 `max(1,int(quota))` 截成 2（scheduler L274），在 update 路径原样存 2.5 → 两条入口口径不一致。
  - `max_price < min_price` 时不报错，`condText` 仍显示「价 500~100」，实际一行都选不中。
- 复现：在「价格/数量/预算」分别输入 `1e309`、`-1`、`0`、`2.5`，点开始；对照队列页「条件」与运行日志里的执行模式/超预算提示。
- 修复方向：新增 `parseCond()`：`Number.isFinite` 否则报错并 return；`min>=0`、`max>=0`、`max===0||min<=max`、`quota` 取整 1..999、`budget>=0`；错误用 `notice(...,'err',10000)` 且不启动。后端在 `/api/config` console 段和 `/api/tasks/update` 补同一套 clamp（目前只有 `scheduler.add` 里有 `max(1,int(quota))`）。

### F4 · 高（利用条件受限）· `wearTag` 未转义，OCR 文本直入 `innerHTML`

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L502-L505) L502-505（`'<span class="tag ' + String(w).toLowerCase() + '">' + w`）、使用点 L522；数据源 [core/engine.py](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1335-L1340) L1335-1340（`"wear": r.get("wear")`，来自列表截图 OCR）。
- 现象：`esc()` 写得很规范，但 `wearTag` 既没转义 class 也没转义文本；同页可触达的敏感能力包括 `/api/quit`、`/api/scheduler/stop`、`/api/follow` 以及 `window.pywebview.api.destroy()`。只要 OCR 把某个字符序列认成 `wear`（例如商品名串列、贴图文字），就能注入标签。
- 复现（只读审查，未实跑）：用本地 mock 让 `/api/scheduler` 返回 `rows:[{wear:'<img src=x onerror=alert(1)>'}]`，刷新页面即触发；生产路径是否可被诱导取决于 OCR 的列切分（**推测**）。
- 修复方向：`wearTag` 内部 `esc(w)`，class 只允许 `s/a/b/c` 白名单；全仓 `innerHTML` 拼接统一走 `esc`（`renderRows / renderTasks / loadRecords / renderSwitches / loadPreflight` 其余字段已转义，仅此处漏）。

### F5 · 中 · 后端已给 `config_ok/config_error`，前端全部丢弃

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L615) L615、L647-670（`renderTasks` 无该参数）、L672-677（`loadTasks` catch 静默）；[core/server.py](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L186-L193) L186-193。
- 现象：`/api/tasks` 与 `/api/scheduler` 都返回 `config_ok/config_error`（后端注释明确说「界面要显示读失败，不能把快照当成队列空了」），前端只读 `tasks`。配置读坏时队列页照旧显示「队列是空的 —— 在抢购页点开始抢购会自动建一条」，把用户引向「再建一条任务」。
- 复现：把 `config.json` 临时改成非法 JSON（勿在真实配置上操作），刷新页面 → 队列页仍是空提示。
- 修复方向：`renderTasks(tasks, meta)`，`meta.configOk===false` 时整表替换为「配置读取失败：…」；`loadTasks` 的 catch 把原因写进 `#taskHint`。

### F6 · 中 · 同一个「游戏」pill 有两个写入者，preflight 结论被每秒覆盖

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L602) L602（poll 写 `pillGame` = 运行中/待机）、L986-987（preflight 写 `pillGame` = 游戏已就绪/未就绪）。
- 现象：游戏没开时 preflight 亮红并写「游戏未就绪」，下一次 poll（≤1s）把它改成灰点「待机」；「就绪」也会被「运行中」覆盖。顶栏唯一的环境指示灯形同虚设。
- 复现：关掉游戏 → 设置页点「重新检查」→ 回抢购页观察「游戏」pill 1 秒内变色变字。
- 修复方向：拆成「调度状态」与「环境状态」两个 pill；或约定 poll 只更新调度类 pill，`pillGame` 归 preflight 独占。

### F7 · 中 · 关窗/Alt+F4 不经过确认，抢购中途被硬中断

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L852-L863) L852-863；[desktop.py](file:///C:/Users/26346/Desktop/project/dafors/desktop.py#L282-L307) L282-307。
- 现象：`running` 取自 `STATE.sched`（上一次 poll 的快照，最多旧 1 秒）；刚点开始（引擎还没 running）时点「×」不弹确认。即使弹了，也只 `POST /stop` 后立刻 `a.destroy()`；`webview.start()` 返回 → `main()` 返回 → daemon 线程随进程结束，`stop()` 里 `join(timeout=3.0)` 的「还在收尾」分支（scheduler L478-483）会被直接跳过。用 **Alt+F4 / 任务栏关闭** 完全绕过这段 JS。
- 复现：抢购中点窗口「×」（应弹确认）；或抢购中按 Alt+F4（不弹确认直接退出）。
- 修复方向：`btnClose` 里先 `await api('/api/scheduler')` 现查一次再决定是否 confirm；desktop.py 用 `create_window(..., confirm_close=True)` 或监听 `closed` 事件先 `_scheduler.stop()`；前端加 `beforeunload` 提示仅作辅助（WebView2 对 beforeunload 支持有限，需实测）。

### F8 · 中 · 队列页每秒整表重建：勾选/删除被回滚、行位置漂移

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L647-L670) L647-670（`tb.innerHTML = tasks.map(...)`）、L615（每秒调用）、L679-692（点击后才发请求）。
- 现象：勾选请求在飞时，下一轮 poll 用旧快照重建整表 → 勾选状态闪回；删除按钮所在行随重建上下跳动，连点时容易删错行（后端 `remove(id)` 本身幂等，但目标 id 可能已不是用户以为的那行）。
- 复现：队列页勾选一项后立刻点下面一行的「删」。
- 修复方向：按 `data-id` 差量更新（只改「状态/进度/启用」单元格）；或点击后本地先行改 UI 并在响应回来前保持；渲染期间不打乱行序（顺序由后端稳定排序）。

### F9 · 中 · 轮询不区分页面；设置页重渲染丢焦点、丢未提交输入

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1060) L1060（无条件 `setInterval`）、L596-621（无论当前页都渲染）、L896-914 + L916-934（`env.dry_run/run.mode` 变更后 `loadSwitches()` 重建 `#switchGroups`）、L1020-1034（`loadConfig` 覆盖输入框）。
- 现象：`#switchGroups` 整块 `innerHTML` 重建 → 正在输入的数字框失焦，未触发 `change` 的值丢失；`loadConfig()` 又把 `#fMin/#fMax/#fQuota/#pBudget` 用盘上值覆盖。表格重建同样会重置滚动位置（见 F10）。
- 复现：设置页在「点击频率」输入 `30`（不回车、不失焦），去拨「演示模式」开关 → 输入消失、焦点丢失。
- 修复方向：`showPage()` 内按页启停轮询（记录/设置页可 5s 或仅在可见时轮询）；`loadSwitches` 就地更新 `value/class`，仅在分组结构变化时重建；`loadConfig` 只在没有 focus 的输入上回填。

### F10 · 中 · 挂单表滚动位置每秒被重置（日志区有保护，表格没有）

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L511-L527) L511-527（`tb.innerHTML`）；对照 L529-539（`logbox` 有 `atBottom` 判断）。
- 现象：用户在 `#rows` 向下滚到第 20 条，下一秒 `innerHTML` 重建后回到顶部；行数变化时 `scrollTop` 被夹紧，位置也会跳。
- 复现：抢购中把挂单表滚到底部，观察每次 poll 后的跳动。
- 修复方向：渲染前存 `.tablewrap.scrollTop/.scrollLeft`，渲染后恢复；或改差量渲染（同 F8/F9 一套方案）。

### F11 · 中 · 无障碍：开关是键盘不可达的 `div`，页签/折叠头同理

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L881-L882) L881-882（`.swtog` 有 `role="switch"` 与 `aria-checked`，**无 `tabindex`、无 keydown**）、L936-946（只处理 `click`）、L909（`.swhead` 纯 div）、L866-868（`#logToggle` 纯 div）。
- 现象：Tab 遍历时焦点落不到任何开关，读屏/键盘用户无法切换；折叠头也不可用键盘展开。状态本身不只靠颜色（pill 有文字、开关有 `aria-checked`），这点是达标的。
- 复现：只用键盘 Tab + Space 操作设置页，开关无法切换。
- 修复方向：改为 `<button type="button" role="switch" aria-checked>`；或补 `tabindex="0"` + `keydown`（Space/Enter）；`.swhead` 加 `role="button" tabindex="0" aria-expanded`。

### F12 · 低 · 按钮缺 `type`：当前无表单所以不刷新，属潜在坑

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L239-L242) L239-242、L272-275、L355、L384、L406-407、L423-424；整页无 `<form>`（L263-428）。
- 现象：今天没有 submit 路径，`Enter` 在 `#fMin` 里也不会触发 click（无 form）——**「按钮无 type 导致表单刷新」「Enter 触发两次」当前均不成立**，只是将来把筛选行包进 `<form>` 就会立刻出现。
- 复现：无（现状不可复现）。
- 修复方向：统一补 `type="button"`。

### F13 · 低 · 选皮肤与「开始抢购」竞态：可能用旧目标开跑

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L738-L747) L738-747（`await POST` 之后才写 `STATE.target`）、L795（`syncTask` 读 `STATE.target`）。
- 现象：选中皮肤后立刻点开始，`STATE.target` 仍是上一件/空 → 任务名落成旧值或空名（**空名 = 抢所有挂单**，配合 `max_price=0` 就是「看到就买」）。
- 复现：下拉选皮肤后 ≤50ms 点「开始抢购」；看队列页目标列。
- 修复方向：`change` 里先写 `STATE.target` 再发请求（失败回滚并提示）；`startRun` 里以 `#skinPick.value || STATE.target` 为准，或 await 一个 `pendingTarget` Promise。

### F14 · 低 · 重载后目标标签与下拉框不一致

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1031-L1033) L1031-1033、L715-728。
- 现象：`loadConfig` 只 `setTargetLabel(name)`，不还原 `#skinSeason/#skinPick`；顶栏显示某件皮肤，下拉仍是「全部赛季 / — 选皮肤 —」。
- 复现：设好目标后刷新页面。
- 修复方向：按 `target.season` 选中赛季 → `fillSkinPick()` → 选中 `name`。

### F15 · 低 · `?frozen` 分支是死代码

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1059) L1059、L1069；[desktop.py](file:///C:/Users/26346/Desktop/project/dafors/desktop.py#L221) L221（`url` 不带 query）。
- 现象：想做「冻结/截图时不轮询」的开关从未生效。
- 复现：无（未接线）。
- 修复方向：若需要就在 desktop.py 拼 `url + "?frozen"`，否则删掉两个分支。

### F16 · 低 · `file://` 直接打开时全部 API 失效且无原因提示

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L448-L455) L448-455（`fetch('/api/...')` 绝对路径）、L1050（`loadCatalog` 失败被静默吞）、L625-627（只写「连接断开」）。
- 现象：双击 `web/index.html` → `file:///api/config` → 赛季下拉空、记录页报「读记录失败」/状态栏「连接断开」，用户不知道是打开方式不对。桌面壳固定走 `http://127.0.0.1:<port>`（desktop.py L220-221），正常路径不受影响。
- 复现：资源管理器里双击 index.html。
- 修复方向：`api()` 开头判断 `location.protocol === 'file:'` → notice 明说「请用 run.py / desktop.py 启动」。

### F17 · 低 · 失败路径吞错误、无退避

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L625-L627) L625-627（poll catch 只改连接文案）、L672-677（`loadTasks` catch 空）、L916-923（`loadSwitches` catch 空）。
- 现象：后端挂掉后每秒一次失败请求（HTTP 失败，日志刷屏）；队列页停在旧快照，看不出是「读失败」。
- 复现：抢购中停掉后端进程，观察状态栏与队列页。
- 修复方向：连续失败 N 次后指数退避到 5s/10s；catch 至少把原因写进对应页面的提示区。

### F18 · 低 · 开关数字项「清空」被保存为 0

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L948-L954) L948-954（`Number(raw)`，`Number('') === 0`）；[core/switches.py](file:///C:/Users/26346/Desktop/project/dafors/core/switches.py#L362-L371) L362-371（`int(float(0))` 合法通过）。
- 现象：清空输入框后触发 `change` → 写入 0 且无提示（如「点击频率」变 0）。引擎侧是否有 `max(1,…)` 兜底未核对（**推测**，需确认 `click_hz` 的消费点）。
- 复现：设置页清空任一数字开关输入框，点别处失焦。
- 修复方向：`raw === ''` 时不保存，恢复旧值并 `notice('已恢复原值','warn')`。

### F19 · 低 · 数据格式化的小缺口

- 位置：[web/index.html](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L498-L501) L498-501（`fmtPrice` 用 `Number(v).toLocaleString`，`Infinity → "∞"`，`NaN → "NaN"`）、L524（`r.cy ? Math.round(r.cy) : '—'`，`cy=0` 显示「—」）。
- 现象：异常价格显示成 `∞`/`NaN` 而非「—」，误导为有效价格。
- 复现：mock 返回 `price:null` 以外再返回 `"abc"`（后端目前给 float，属防御性缺口）。
- 修复方向：`Number.isFinite` 才格式化，否则「—」。

## 已核对、无问题的点（回应审查项）

1. **重复绑定**：全仓只有两处 `document.addEventListener('click')`（L679、L936）与一次 `change`（L948）；所有表格行/开关都是委托绑定，`renderRows/renderTasks/renderSwitches` 不绑事件 → **不存在「渲染函数里反复绑定导致请求翻倍」**。`#btnRecRefresh/#btnPreflight/#btnStart/#btnStop/#skinSeason/#skinPick/#btnClearPick/#btnFollow/#btnUnfollow` 均只绑一次。
2. **pywebview JS API 匹配**：页面调用 `a.minimize()`（L849）与 `a.destroy()`（L862），与 `WinCtl.minimize/destroy`（desktop.py L118-124）一一对应，`js_api=ctl` 在 L272 传入；`body.has-wv` 同时由 `wvApi()` 与 `pywebviewready`（L844-845）两条路径点亮，脚本在文档末尾同步执行，事件不会早于监听器（**推测**：老版本 pywebview 的注入时序未实测）。
3. **错误文本注入**：`notice()`/`renderNotice()` 用 `textContent`（L478-479、L484-485），`cleanErr()` 能从 `{"ok":false,"msg":…}` 里抠出 msg（L459-463），`esc()` 覆盖 `& < > "`（L506-509）——除 F4 的 `wear` 外，`renderTasks/loadRecords/renderSwitches/loadPreflight` 的数据都已转义。
4. **`setPill` 选择器**：`el.querySelector('span:last-child')`（L543）在 `<i class="dot"></i><span>…</span>` 结构下命中正确节点（L245-247）。
5. **`records` 契约**：`loadRecords` 用到的 `rows/stats.tries/stats.hits/stats.spent/stats.rate/row.result/row.shot/row.verified/row.elapsed` 与 `core/records.py:109-156` 完全对齐。

## 未验证项

1. **未实跑**：本次为只读静态审查，未启动 `desktop.py`/`run.py`，未在浏览器或 WebView2 里复现交互。所有「窗口期宽度」「OCR 是否会把注入字符带进 wear 列」均标为推测。
2. **`1e309` 的浏览器行为**：`<input type="number">` 对超范围的输入可能直接返回空 `value`（而非 `1e309`），因此 F3 的「Infinity → null」路径需在 WebView2 里实测一次；无论哪种结果，现有代码都没有「字段无效 → 报错」的分支。
3. **`preflight.collect()` 的实测耗时**：仅依据 `core/preflight.py:276` 的自述注释（约 1 秒），未计时。
4. **Alt+F4 / 任务栏关闭时 pywebview 是否触发 `closed`**：desktop.py 已做 `closed` 事件 + `wait(0.5)` 兜底（L288-293），但没在该路径里 `_scheduler.stop()`；实际进程退出时机未观测。
5. **`window.confirm` 在 frameless + WebView2 下的表现**（是否被 `easy_drag=False` 或焦点问题影响）未验证。
6. **`click_hz = 0` 的后端兜底**（F18 的推测部分）未读 `core/engine.py` 里频率消费点。
7. **多实例端口回退（desktop.py L81-91）对前端的影响**：页面始终用相对路径，理论上无影响，未实测 8778/8779 端口场景。
