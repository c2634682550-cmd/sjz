# 前端交互与新手可用性修复报告

对象：`web/index.html`（单文件，零外部依赖）。未改动 `core/`、`app/`、`desktop.py`、`config.yaml`、`tests/`。

验收方式：无头 Chromium 单实例，连真实后端（`core.server`，127.0.0.1:8899，仅关闭全局热键注册与 OCR 服务管家，API 语义不变）。全部用 DOM 断言判定，不落盘截图（本次按要求另截 4 张，见文末）。
结果：**50/50 通过**。脚本：`_fe_verify.py`（项目根目录，可重复执行）。

## 一、高 · 交互

| # | 根因 | 改法 | 验证（脚本内编号） |
|---|---|---|---|
| 1 轮询竞态 | 1 秒定时器无互斥，`poll()` 可重入；响应按到达顺序直接覆盖 `S.status`，慢的旧快照回写；`refreshJournal()` 也独立写状态 | `poll()` 加单飞 `S.pollBusy` + 递增序号 `S.pollSeq` + 发起时间 `issuedAt`；仅当序号最新且 `issuedAt >= S.statusApplied` 才应用；若后端给 `_v` 且小于当前值也丢弃；`refreshJournal()` 共用同一套时间戳门槛 | T1：人为让旧快照（6/10、`_v:1`）晚 1.6 秒返回、新快照（9/10、`_v:2`）先到，断言 `#topProg` 稳定为 `9/10`，不回退 |
| 2 勾选被清空 / 批量静默全量 | 每秒 `renderTaskTable()` 从任务数组重建 DOM，勾选状态只存在于 DOM；批量在未勾选时默认全部且无确认 | 勾选改为 `S.checked`（Set，按字符串 id）；`syncChk()` 回填勾选与全选框三态；`tasksSig()` 签名未变时跳过重绘；批量前 `ask()` 确认，文案显式「将对 N 个任务生效：已勾选的任务 / 全部任务（你没有勾选任何一条）」 | T2：勾 3 条跨两轮轮询仍为 3；`#bulkScope` 显示「已勾选 3 条」；弹窗含「将对 3 个任务生效」；取消 0 请求，确认后 3 条 update |
| 3 错误页固化 | `loadTasks` 失败后停在 error 态，只有手动点才重试；轮询成功也不清错误 | 失败时 3 秒后自动 `loadTasks(true)` 自愈（错误文案写明会自动再试）；`poll()` 拿到带 `tasks` 的状态时把 `taskMode` 从 error 切回 ok 并重绘 | T3：首次 `/api/tasks` 返回 500 显示错误态；不点任何按钮，4 秒后自动恢复 |
| 4 添加连点 | 按钮无进行中互斥，三次点击三次请求 | `S.adding` 单飞 + 按钮 `disabled`/「正在添加…」；`finally` 复原 | T4：同帧点三次，fetch 只发 1 次，且进行中按钮为 disabled |
| 5 接口 500 静默冻结 | 只按“最后一次成功”判连接；500 只在控制台留痕，界面无反馈 | `noteHttpFail(path, why)` 记录失败接口与原因，顶栏状态变「已连接 · 有接口报错」（黄点），并 toast「路径：原因」（4 秒去重）；页面内错误态同时显示具体原因 | T5：`/api/records` 500 → 状态条为「已连接 · 有接口报错」，出现「/api/records?limit=200：台账文件被占用」toast，记录页显示原因 |

## 二、高 · 新手可用性

| # | 根因 | 改法 | 验证 |
|---|---|---|---|
| 6 失败原因被吞 | 所有非 2xx/`ok:false` 统一换成「稍后再试一次」 | `api()` 解析任意状态码响应体，`apiMsg()` 依次取 `msg/message/error/reason/detail`，拿不到才兜底；`humanize()` 不再删除「OCR/锚点」等关键词，只去 `XxxError:` 前缀与 traceback | T6：后端返回「先把购买按钮框选好再添加任务」，`#addMsg` 与原文逐字相同，且不含兜底词 |
| 7 启动失败只留「重新检查环境」 | 弹窗不渲染 `checks`，按钮只重跑同一个 preflight，回到同一个框 | `startFailed()` 先立刻展示真实原因，再异步拉 `/api/preflight` 并逐项渲染（通过/注意/没通过 + 「怎么处理：fix.hint」）；`#excBtn2`「再检查一次」；`excCustomFix()` 复查成功后自动关弹窗；`preflight` 结果用 `allowFail` 读取（后端 `ok:false` 是“检查未通过”，不是接口错误） | T7：弹窗标题「还不能开始」，7 个检查项，3 条「怎么处理」，`再检查一次` 可见 |
| 8 设置页 8 个下拉异常 | 后端 `kind` 是 `select`，前端只认 `choice`，于是按数字输入框渲染，`value="countdown"` 落进 `input[type=number]` → 空白；`options` 是 `[value,label]` 数组对，代码当对象取 `.value` → `undefined` | `selectHtml()` 支持数组对/对象/裸值三种；`data-kind="select"`；`kind === 'select' || 'choice'` 都走下拉；无选项时给出「这一项没有可选值」禁用态而不是空白 | T8：8 个 `select[data-sw]`，每个都有非空中文选中项且非禁用；`run.workflow` 选中「公示期倒计时」 |

## 三、中低危（逐条）

- **选中高亮失效**：`String(t.id) === String(activeId)`，跨轮询保持。T9 通过。
- **节拍面板永久骨架**：`S.switchesErr` 时给「重新读取」；无数据才显示骨架；补回 `fire.hz`。T10：7 行真实标签，数值已回填。
- **2000 条列表每秒重绘**：`tasksSig()` 内容签名 + 未变化直接返回。T11：静置 2.6 秒 `MutationObserver` 计数为 0。
- **目标库截断仍显示 720**：去掉 `slice(0,400)` 全量渲染，计数取实际条数。T12：`243 / 243 件`，卡片 243 张。
- **记录页 `r.price` 可注入 HTML**：所有动态文本走 `esc()`/`num()`；非数字价格显示占位。T13：`<img onerror>` 未执行，无 img 元素。
- **键盘焦点每秒丢失**：重绘前记录 `activeElement` 的 `data-id`，重绘后复原。T14：焦点停在原行。
- **重试叠加多条启动链**：`boot()` 单飞 `S.booting`。T15：连调三次只发 1 次 `/api/config`。
- **toast 互相覆盖**：`#toastHost` 堆叠容器 + 最多 4 条 + 先出动画。T16：5 次调用保留 4 条，矩形不重叠。
- **窄屏运行页右栏消失**：1200px 媒体查询改单列并 `display:flex`。T17：右栏 1176×173 可见。
- **切页后 job 轮询不停**：`go()` 增加 `S.navToken`，离开设置页 `stopJobPolling()`。T18：切页后 `/api/jobs` 请求数冻结。
- **裸显示工单号/接口路径**：任务行不再渲染 id；错误文案只给接口名不给完整路径；技术细节收进 `<details>`。T26 通过。
- **日记页裸甩 `FileNotFoundError` 与绝对路径**：`journalView()` 把异常映射成中文原因（缺模板图/用例文件等），原文只在「技术细节」折叠里。T20 通过。
- **「连点超时 8 毫秒」**：单位只取后端 `hint`（`秒`），不再用启发式兜底。T8/T10 通过。
- **「中位耗时 112275 秒」**：`fmtDur()` 秒→毫秒/秒/分钟/小时，>3600 秒标注「数值异常偏大，可能混入了单位不一致的旧记录」。T22：`31.2 小时`。
- **价格全「—」却显示成交总额**：价格空显示「未回填」，总额副标题「已确认且有价格 N 条；价格空着的不计入」。T23 通过。
- **示例台账冒充真实消费**：台账页与统计只读 `/api/records`，空态明确「还没有记录」，不再内置示例行。

## 四、占位与教学

- 32 处「待接入」、15 个灰色控件、`查看待接入功能` 入口、`.gap-btn` 样式全部移除（T24：0 处）。
- 通知页、方案管理页整段删除；导出/排序/拖动/OCR 档位/授权占位等无后端支撑的入口隐藏；授权改为读 `/api/license` 真实状态。
- 「画面识别」整块换成**区域标定**：显示四个区域是否合法 + 复制命令 + 说明改 `config.json` 的 `regions`、模板图放 `templates/`（后端没有框选接口，不再给走不通的指引）。
- 首次引导 `guideDlg`：四步（① 标定区域（含“这一版没有框选页，改 config.json”）→ ② 点「重新检查」→ ③ 添加任务 → ④ 校时后开抢），每步说明“现在点哪里、为什么”，按钮直达对应页；`localStorage.dafors.guided` 记录已读。T25 通过。

## 五、需要后端配合的待办

1. `/api/scheduler` 的 status 没有快照版本号。前端已用“发起时间戳 + 单飞”兜住同页乱序，但跨进程/跨标签页仍建议后端加单调递增 `rev`，前端再按 `rev` 丢弃旧快照。
2. 没有框选/选区接口。要做真正的“框选标定”，需要 `/api/regions/select`（截一张交易行画面 → 前端画框 → 回写 `regions`），并让 `/api/preflight` 的 `regions` 检查给出可点击修复。
3. `/api/preflight` 语义混用：HTTP 200 且 `ok:false` 表示“检查没通过”。已用 `allowFail` 兼容，但建议区分 `ok`（传输）与 `passed`（结果）。
4. preflight 实测约 2 秒。启动失败后清单是异步补上的（先显示原因）；若想做成启动前阻塞式检查，建议后端返回 job id + 轮询，或缓存 10 秒。
5. `/api/skins` 没有武器类型列表，目标库“全部类型”由前端从 `seasons.*.tiers[].weapon` 推导。建议后端直接给 `types`。
6. `/api/records` 的 `stats.spent` 与行内 `price` 口径可能不一致（有总额、行内全空）。建议后端只对 `price != null` 的行计入，或返回 `priced_hits`。
7. 台账存在 `median_s=112275` 这类旧记录（疑似毫秒当秒写入）。前端已格式化为 31.2 小时并标注异常，根治需后端迁移旧数据。
8. `fire.*` 的单位靠 `hint` 字符串。建议给结构化 `unit` 字段，避免中文文案改动导致单位错位。

## 六、界面截图（本次唯一授权，1440×900 / DPR 1，无可见窗口）

1. `C:\Users\26346\Desktop\project\dafors\_ui_shots\01-首页.png` — 抢购页首屏（真实后端，当前 0 条任务）
2. `C:\Users\26346\Desktop\project\dafors\_ui_shots\02-任务列表（mock三行）.png` — 任务清单与右侧任务设置；**行数据为 mock，文件名已标注**
3. `C:\Users\26346\Desktop\project\dafors\_ui_shots\03-设置.png` — 设置 → 抢购高级（真实后端，可见「做工 / 版序顺序 / 公屏期筛选 / 列表排序」等下拉均正常）
4. `C:\Users\26346\Desktop\project\dafors\_ui_shots\04-记录.png` — 记录/台账（真实后端，17 条本机记录）

复跑验收：`python _fe_verify.py`（需先起 `core.server` 于 8899；脚本内全部断言，覆盖上述 50 项，任一项退化即非零退出）。
