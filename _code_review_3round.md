# dafors 代码审查（三轮合并版 · 最终）

> 一句话结论：**这个项目当前最危险的三件事** —— ① `config.json` 在读失败时会被默认值整份覆盖（事务分支已复现），用户框选/队列/阈值会凭空消失；② 「确认模板」在 preflight、`scheduler.add`、`scheduler.start`、`Engine._validate` 四处口径相反，没有确认弹窗的界面列表模式根本启动不了，而为此写的 `_buy_without_dialog` 是死代码；③ 价格解析会把成色条带的 `05` 当价格/把磨损小数与价格粘连，配合同一 `Engine` 复用残留的价格筛选区间，会出现「预算判断基于错误价格集合」还能成交的错单。

- 项目：`C:\Users\26346\Desktop\project\dafors`（Windows / Python 3.11.9）
- 范围：`core/*.py` 全量（重点 engine / scheduler / config / preflight / market / nav_* / ocr / input）+ 根目录 `_*.py` 调用点 + `config.json` 结构
- 方式：只读；未改业务代码、未 git 操作、未启动 GUI、未抓屏、未点击、未联网
- 合并来源：`_review_input.md`、`_review_vision.md`、`_review_sched.md`（三份均已合并，重复项已去重并保留双方 `文件:行`）

---

## ① P0/P1：必须修（8 条，按「改完就能用」排序）

| # | 问题 | 位置 | 为什么必须先修（一句话） | 最小修法 |
|---|---|---|---|---|
| 1 | 价格/排序筛选跨任务只做一次（flag 是 `Engine` 实例属性，整次调度只建一个 Engine） | `engine.py:672-695` + `scheduler.py:339` | 第 2 个任务起沿用上一任务的筛选区间，且 flag 在 `try` 之前置真（首次抛异常也不重试）→ 预算判断基于错误价格集合 | 两个 flag 在 `Engine.start()` 里清零；置真移到 `_apply_price_filter` 成功之后 |
| 2 | 事务读盘失败 → 用默认值覆盖用户配置 | `config.py:270-273` + `config.py:295-315`（已复现） | 文件被占用/杀软锁 9×20ms 后走 `return cls()`，`transaction()` 仍无条件 `_save_locked()` → queue/regions/threshold 全丢，不可逆 | `_load_locked()` 返回 `(cfg, ok)` 或抛 `ConfigLoadError`；`ok=False` 时 `raise` 且不落盘 |
| 3 | 合法 JSON 非对象 → `AttributeError` 启动崩；`reload()` 读失败就地清空 `regions` | `config.py:169-186`、`288-306`、`275-285`；`engine.py:361-371` | `"abc"/123/[1,2]` 不进 `except ValueError` 分支，不留档；`reload` 清空后 `confirm_region=None` → 落进盲点购买路径 | 加载时 `isinstance(data, dict)` 校验并归为 ValueError；`reload()` 读失败直接 return（保留旧配置） |
| 4 | 「确认」模板四处口径相反 → 无弹窗界面列表模式永远开不了工（`_buy_without_dialog` 死代码） | `preflight.py:118-128` ↔ `scheduler.py:166` ↔ `scheduler.py:324/573` ↔ `engine.py:230-243`、`1426-1430` | 检查全绿、任务能加入、点开始秒失败报「缺少模板：confirm」；专为无弹窗界面写的余额兜底分支常态不可达 | 统一为「确认可缺」：`need=("buy",)`（list）、`_validate` 不再 return、`_fire_and_confirm` 直接进 `_buy_without_dialog`；反之一并改成强制并删死代码 |
| 5 | 列表演练仍真实操作游戏 + 导航执行两遍 | `engine.py:955-959`（演练早退）、`1073`（`dry_run=False` 硬编码）、`1133`、`306-307` | 演练模式照样 F4/切筛选/刷新/点卡片；两次导航把筛选跑两遍（约 18s），吃掉 `ARM_LEAD` 20s 窗口 | `_ons_ready` 用 `self._dry_run`；`_run` 列表分支删掉多余 `_navigate` |
| 6 | OCR 读不到列表就按固定表盲点卡片，点后不校验 → 买错货 | `nav_col.py:1227-1237`、`1260`；`nav.py:493-496` | 唯一「必错」的成交路径：OCR 一超时/空结果就直接踩表 | `skin_items()` 为空时抛 `Aborted`，禁止盲点；点后必须回读标题校验 |
| 7 | 价格解析：成色条带 `05` 当价格、磨损小数粘连、`price=None` 无上限放行 | `cards.py:450-458`；`market.py:52-66`、`106`、`131-149`、`443-455`、`508-520` | 过滤器「正确地执行了错误数据」→ 超预算成交 / 未知价成交 | 价格只从价格带取；`wear` 与价格按 y 分区；`price is None` 且有上下限时剔除 |
| 8 | `Screen`/`Mouse` 构造在 `try` 之外 → 线程静默死、调度误报「未抢到」 | `engine.py:283-293`（`try` 在 294）、`scheduler.py:591-629` | 构造失败（mss/驱动/`monitor` 字符串）无 `error` 字段，任务结果写「未抢到」，真实异常只剩 stderr 栈 | 构造挪进 `try`，失败置 `ABORT + _error=repr(e)`；`status()` 锁内一次性拷贝 |

> 紧随其后（第 2 批，不算 P0）：`/api/config` 裸赋值无类型校验（`server.py:298-307` → `"failsafe":"false"` 变 True、`"monitor":"2"` 触发第 8 条）；`on_hit` 自线程 join（`engine.py:346-348` + `192-199`，已复现 RuntimeError）；`LIVE_RELOAD` 死代码（`engine.py:1149-1161` + `scheduler.py:287-292`）。

---

## ② 全量问题清单

### 2.1 调度 / 引擎 / 配置 / 服务（含 `_review_sched.md` 合并）

| 编号 | 级别 | 位置 | 触发条件 | 后果 | 最小修法 |
|---|---|---|---|---|---|
| S1 | 高 | `config.py:270-273`、`295-315` | `transaction()` 期间读盘连续失败 | 默认值覆盖整份配置（已复现） | 见 P0-2 |
| S2 | 高 | `engine.py:672-695` + `scheduler.py:339` | 一轮 ≥2 个任务 | 价格/排序筛选跨任务残留；flag 早置真 | 见 P0-1 |
| S3 | 高 | `engine.py:955-959`、`1073`、`1133` | `dry_run=True` + list | 演练真实操作游戏；导航跑两遍吃窗口 | 见 P0-5 |
| S4 | 高/中 | `engine.py:1149-1161`、`361-371`；`scheduler.py:287-292` | 运行中改任务价格/数量/开抢时间 | `update()` 只写 `queue`，`_refresh_task` 读 `active_task` → 热更新永不生效 | 命中 active 任务时同步写 `active_task`（经 `transaction()`） |
| S5 | 高/中 | `engine.py:361-371` + `config.py:275-285`、`295-315` | 运行中配置被占用 | `regions`/`active_task` 就地清空 → 盲点购买、条件退化「不限价」 | `reload()` 失败直接 return；`fresh` 缺关键键则忽略 |
| S6 | 中 | `engine.py:311-313` vs `382-393`；`config.json` `trigger_mode:"countdown"` | 非法 `trigger_mode`（仓库现况即是） | 日志说 vanish、实际按 appear 且无宽限；`switches` 无该项，界面改不了 | 入口统一规范化并回写同一变量；补进开关表 |
| S7 | 中 | `scheduler.py:324-325/573`、`server.py:556-560/738`、`preflight.py evaluate` | 任务同时有 `workflow:"countdown"` 与 `mode:"list"`（现仓库即如此） | 倒计时任务前置检查只看 list 区域；`preflight` 从不检查 `regions.clock`；`server:560` 默认 `list` 与 `scheduler:520` 默认 `detail` 相反 | 以 `workflow` 为唯一真源，`mode` 只作迁移派生；`evaluate` 加 clock 分支；默认值收敛一处 |
| S8 | 中 | `server.py:746` | `env.dry_run=true` 时按 F7 | 硬编码 `dry_run=False` → 直接真实点击 | 与 `_sched_start` 共用同一判定函数 |
| S9 | 中 | `engine.py:283-293`/`294`；`scheduler.py:594-629` | 构造抛异常 | 线程死、状态停在 RUNNING/WATCH、记「未抢到」 | 见 P0-8 |
| S10 | 中 | `engine.py:682` + `1043-1057` | 预算/区间导致筛选结果为空 | `Aborted` 未在 `except (NavError, AttributeError)` 内 → 整任务中止，与「筛选失败继续逐行比对」相反 | `except` 加 `Aborted` 或空区间返回 False |
| S11 | 中 | `engine.py:346-348`、`192-199`；`scheduler.py:341-355` | `stop.after_hit=true` 且命中 | 引擎线程 `join` 自己 → `RuntimeError` 被 `except Exception: pass` 吞；ARMED/RUNNING→PENDING 复位跳过（已复现） | `stop()` 判断 `current_thread()`；或 `on_hit` 投递到新线程 |
| S12 | 中 | `pagewatch.py:121-133` | 控制台以独立进程（浏览器/Electron/exe）运行 | PID 不等 → 永不算「自己在前台」，把控制台画面当游戏 OCR | 按本进程窗口集合/标题类名匹配 |
| S13 | 中 | `engine.py:1718` 前后 | 等待解锁期间弹窗已关（残留/手点） | `return True` 后直接进确认连点，点击落到下层控件 | 返回前再校验一次确认按钮模板 |
| S14 | 中/低 | `scheduler.py:339-343/346-355/594-618`、`engine.py:192-199` | 停止后再次 `start()` | `join(2.0)` 超时仍复位 PENDING；旧线程可能还在点，新任务共享 `_stop`/`_thread` | `start()` 发现旧线程存活则先 stop+join，超时拒绝启动；join 失败保持 RUNNING |
| S15 | 中 | `records.py:35` 起 + `engine.py:1844-1861` | 引擎写记录 + HTTP `clear`/统计同时 | `append` 与 `clear` truncate 竞争产生半行 → `read` 返回 `[]` → 前端显示 0 条 | 模块级锁包 append/read/clear；`clear` 用临时文件 + `os.replace` |
| S16 | 低 | `engine.py:1316` + `scheduler.py:607` | countdown 成交 | `bought` 只在列表路径累加 → 面板「已抢」恒 0 | 倒计时成交同样 `_stats["bought"] += 1` |
| S17 | 低 | `engine.py:1844-1861` | 任何记录写入 | `mode` 恒 `""`（`active_task` 无 mode 键） | 保存 `self._task_mode or task.get("workflow")` |
| S18 | 低 | `engine.py:515/521/801` | OCR 没读到商品编号 | `serial==""` 多次入 `tried` → 同一件被重复点击，浪费 attempts | 空 serial 用位置索引/指纹去重 |
| S19 | 低 | `pagewatch.py:32-43/79` | 多显示器 | 参数不保存，`Screen(1)` 硬编码 → 监视区域错位 | `self._monitor = monitor` |
| S20 | 低 | `server.py:550/566` | API 未带 `dry_run` | `get(..., True)` 静默演练，用户以为在真抢 | 缺失时读 `env.dry_run` |
| S21 | 低 | `jobs.py` `_run` | 作业抛 `BaseException` | `_current.state` 永久 running → 后续全 `Busy` | `finally: state="idle"` |
| S22 | 低 | `records.py:35/51/71-72/84-111` | `price` 非数字；读失败 | `float(price)` 在 try 外；读失败伪装成 0 条；stats 不分 verified | `float` 挪进 try；读失败抛异常/返回 None；加 `verified_only` |
| S23 | 低 | `timesync.py:70`、`wait_until`、`offset()` | 锁外读；长期不刷新 | samples 迭代中被改写；过期 offset 仍被信任 | 锁内 `list(...)` 拷贝；删 `wait_until`；`offset` 加过期 |
| S24 | 低 | `single_instance.py:33-39` | `CreateMutexW` 失败 | 失败即返回 True → 放行多开，双实例抢购/双写配置（端口冲突行为未定义） | 失败返回 False 并提示；`SetLastError(0)`；声明 `restype=HANDLE` |
| S25 | 低 | `scheduler.py:408-441` | queue 里任务缺 `fire_at` | `pending.sort` `KeyError` 冒泡 → 调度线程退出，任务全停 PENDING | `task.get("fire_at") or ""`；`_run` 外层不 break，sleep 后继续 |
| S26 | 低 | `server.py:702-716`；`scheduler.stop` | 连按 F8 | 多个 daemon 线程并发 `stop()`、各自 join/复位，顺序不确定 | `stop()` 幂等（锁 + `_stopping` 标志）；热键 300ms 去抖 |

### 2.2 输入 / 市场 / 卡片（源：`_review_input.md`）

| 编号 | 级别 | 位置 | 触发条件 | 后果 | 最小修法 |
|---|---|---|---|---|---|
| I1 | 高 | `cards.py:450-458`、`market.py:350-372` | 卡片含「成色 S 05」 | 残数字当价格 → `price=5`，低价优先命中更贵的 | 价格只取价格带，成色区不参与数字提取 |
| I2 | 高 | `market.py:52-66`、`106`、`131-149` | `100.951000` 这类磨损+价格粘连 | 价格低读（500 而非 9500） | 用 x 间隙/位图分区切分磨损与价格 |
| I3 | 高 | `market.py:443-455`、`508-520` | 未配上下限、`price=None` | 未知金额被买；预算失效 | 有预算/区间时 `price is None` 直接剔除 |
| I4 | 中/高 | `bricks.py:327-349`、`skinfilter.py:115-124` | 只给型号 | 模糊解析成任一皮肤 → 买错 | 解析出多候选时报错而非取第一个 |
| I5 | 中 | `skinfind.py:287-307`、`nav_col.py:942` | 上一张卡标题残留 | 正确卡被判「不是它」→ 漏选/超时 | 开卡后先等标题变化再比对 |
| I6 | 高 | `hotkey.py:145-151`、`115-131` | 热键回调里同步跑 `Scheduler.stop()` | 超 `LowLevelHooksTimeout` → 钩子被系统摘掉，但 `status()` 仍报 active | 回调只投递，实际动作放工作线程 |
| I7 | 中 | `input.py:186-193` | 驱动 down 后 up 抛错 | 左键卡在按下状态（系统级拖拽） | up 放 `finally`，失败也要恢复 |
| I8 | 中 | `input.py:195-221`、`486`、`nav.py:311` | legacy 点击 | 真点返回 0 静默；失败不恢复光标 | 校验返回值；`finally` 回原位置 |
| I9 | 中 | `actions.py:46`、`engine.py:172` | 找不到游戏窗口 | 退出码复用 → 当前台（控制台） | 找不到就抛错，不兜底 |
| I10 | 低 | `input.py:549-560`、`584-585` | 每次 `process_integrity` | 进程句柄未关 → 长跑泄漏 | `CloseHandle` 放 finally |
| I11 | 低 | `input.py:459-476`、`179` | 调用 `Keyboard.click` | `Mouse.__new__` 缺 `_mode`/`_drv_warn` → `AttributeError`（已复现，当前无调用方） | 走 `Mouse(...)` 正常构造 |
| I12 | 低 | `market.py:500-503` | 传 `exclude` | 用 cy 排除→连带同排另一张 | 按行号/坐标二维排除 |
| I13 | 中 | `market.py:266-292`、`cards.py:504-527` | 卡片数量变化（6→2）但帧尺寸不变 | `changed_cards` 只比 `shape`，`changed=[]`（已复现）→ 缓存按下标错配，点进去是另一件 | 比较 `len(cards)`；变化即清空缓存 |
| I14 | 低 | `skinfind.py:234-246`、`196-213` | `assume_top=True` | 筛完未回顶部 → 顶部若干张永远扫不到 | 筛后等滚动位置归零或索引复位 |
| I15 | 低 | `cards.py:345-347` | 卡片有 5 条色带 | 超 4 条按高度截断 → 丢价格带 → 整卡丢弃 | 按 y 聚类而非固定 4 条 |
| I16 | 低 | `interception.py:59-68`、`81` | 阅读/维护 | 注释写 16 字节、实际 20；`Stroke` 未用 | 改注释/删死常量 |
| I17 | 低 | `interception.py:170-184` | 并发建 context | 无锁 → 句柄泄漏、down/up 交错 | 加锁 + 单例 |
| I18 | 低 | `input.py:293-309` | 脚本把返回值当布尔 | 实际返回二元组 | 统一返回 bool 或文档化 |
| I19 | 低 | `hotkey.py:160`、`232` | 注册失败 | `get_last_error()` 恒 0，原因不可读 | `ctypes.get_last_error()` 或 `use_last_error=True` |
| I20 | 低 | `hotkey.py:156-197`、`257-263` | 并发读写 | `_ok/_msg/_tid` 无同步 → `status()` 报旧值 | 加锁或原子字典替换 |
| I21 | 低 | `input.py:415-425`、`366-374` | legacy 下 `tap("A")` | VK 表缺字母数字 → 无效 | 补 VK 映射或断言拦截 |
| I22 | 低 | `popup.py:95-105` | 余额行残破 | 遇第一个残破行就放弃 → 少关键信息 | 继续扫描后续行 |
| I23 | 低 | `preflight.py:266-274`、`161-169` | legacy 模式 | 仍报「驱动未就绪」→ 文案与配置相反 | 按 `input_mode` 分支文案 |
| I24 | 低 | `preflight.py:296` | 每次预检 | 构造带驱动模式的 `Mouse`，白建 context | 预检只读，不构造输入对象 |

### 2.3 视觉 / 坐标 / OCR（源：`_review_vision.md`）

| 编号 | 级别 | 位置 | 触发条件 | 后果 | 最小修法 |
|---|---|---|---|---|---|
| V01 | 致命 | `nav_col.py:1227-1237`、`1260`；`nav.py:493-496` | OCR 超时/空结果 | 盲点固定表卡片且不校验 → 买错货 | 见 P0-6 |
| V02 | 高 | `nav_coords.py:286-293`、`nav_col.py:885/1175/1108/1260`、`nav_ons.py:521/647/884`、`nav.py:217` | 分辨率≠1920×1080 | 固定 ROI 后又把 OCR 坐标按同比例缩一次（双缩放）→ 读错区、点错位 | 全链路只在一个边界缩放；ROI 用 `norm_to_px` 一次 |
| V03 | 高 | `nav.py:79-82` | 在售页第 ≥2 页 | `推荐` 锚点命中 → 判成 detail | 先判页码/详情锚点，再判列表 |
| V04 | 高 | `nav.py:147-149` | tab 行偏 1~2px | 单行采样判 None → NavError 卡死 | 采样 3~5 行并取多数 |
| V05 | 高 | `screen.py:82-90` | 双屏同分辨率 | `want=2 → 1`（已复现），抓/点都落在主屏 | 优先按 `want` 索引，尺寸仅作兜底 |
| V06 | 中 | `nav_ons.py:806-822` | 面板里任意 29×29 方框 | 幽灵选项行 → 点到翻页/空白 | 加列约束 + 左右配对 |
| V07 | 中 | `ocr.py:204-224`、`nav_ons.py:521-529/1042`、`nav.py:217` | 低置信碎块 | `score_thresh=0.0`，碎块即结论 | 关键字段（页码/价格/面板）设阈值 |
| V08 | 中 | `ocr.py:210-219`、`187-196` | OCR 服务报错 | 异常吞成 `[]`，与「真的没有」不可区分 → ons_check 报「面板没有」 | 失败返回 `None` 或抛，由调用方区分 |
| V09 | 中 | `ocr_service.py:177-192` | 模型挂死 | 只 TCP 判活 → 「进程活着但十几分钟不可用」 | 加推理心跳/微小请求探活 |
| V10 | 低 | `ocr_service.py:405-410` | 每次 spawn | 日志句柄不关 → 句柄泄漏 | 父进程 `f.close()` |
| V11 | 低 | `ocr_service.py:213-223` | 关机 | `_wait_idle` 在 stop 置位时返回 True → spawn 孤儿 | 先判 `_stop` 再 spawn |
| V12 | 高（推测） | `OCR.yaml:6-22`、`ocr_ppocr.py:182-209`、`318-330` | 服务端做旋转/去畸变 | 客户端不回映射 → 整帧坐标错 | 关闭 doc 预处理或回传映射 |
| V13 | 中 | `ocr_ppocr.py:308-309` | 反向框修正 | `[300,100,40,900]` → `w=40,h=900` | 只在 `ratio<0` 时交换 w/h |
| V14 | 中 | `ocr.py:251-262` | 标题只读出一字 | 包含式加成让 `sim` 过 0.72 → 点错卡 | 去掉包含式加分，或要求覆盖比 |
| V15 | 低 | `ocr.py:376-380` | 名称跨距离碎片 | 用最后 append 的词判间隙 → 拼接污染 | 记录每个词的 `x0/x1` 判空隙 |
| V16 | 低 | `ocr.py:87-97`、`182/204` | 传 `timeout` | 形参被忽略 | 透传给 `_post` |
| V17 | 低 | `ocr.py:182`、`204` | 每次 `_client()` | 重建客户端语义不明 | 复用或明确文档 |
| V18 | 低 | `nav_col.py:1012-1019` | 从详情页起手 | 只退一级 Esc → NavError | 循环退到大厅并校验 |
| V19 | 低 | `nav.py:275-280`、`291-300` | `_diff` 收到 None/形状不同 | `AttributeError/ValueError` 绕过 NavError 兜底 | 入口做 None/形状校验 |
| V20 | 低 | `nav_col.py:1142-1146` | — | 型号粗筛是死代码，注释承诺不存在 | 删除或实现 |
| V21 | 低 | `nav_ons.py:571` | 12px 分箱 | 锚点丢失 → 顺序硬贴 | 用行聚类而非固定分箱 |
| V22 | 低 | `ocr_service.py:227-250` | `restart()` | 备用端口硬编码，与配置端口打架 | 端口取自配置 |

### 2.4 桌面 / 持久化 / 其它

| 编号 | 级别 | 位置 | 触发条件 | 后果 | 最小修法 |
|---|---|---|---|---|---|
| D1 | 低 | `desktop.py:275-279` vs `285` | 老版本 pywebview 无 `events` 且正常关窗 | `AttributeError` → 被当成「开窗失败」→ 弹浏览器 + 驻留到 idle 超时 | `hasattr(win,"events")` 保护 |
| D2 | 低 | `engine.py:1850-1858` | 任何成交记录 | `verified` 记的是开关，余额兜底成交也标 True | 增 `verified_by`（dialog/balance） |
| D3 | 低 | `engine.py:1200-1209`、`1315` | OCR 读不出价格 + 预算 | 未知价行永远在候选且计入 0 → 预算形同虚设 | 同 I3 |
| D4 | 低 | `engine.py:1426-1433` | 演练中确认区域被清空 | `expand(None)` `TypeError`（已复现） | `None` 一律走 `_buy_without_dialog` |

> 说明：上表每条都带「级别 / 位置 / 触发条件 / 后果 / 最小修法」；完整推导与截图式证据见三份分报告原文（`_review_input.md`、`_review_vision.md`、`_review_sched.md`）。

---

## ③ 根因归类

| 根因 | 条目 | 为什么最伤 |
|---|---|---|
| **① 配置层不可靠（读失败即降级/覆盖）** | S1、S3、S5、C7（S6/S7）、A2 | 配置是整条链的地基：一次读失败就让「框选/队列/阈值」凭空消失，且不可逆；比任何单点 bug 都难排查 |
| **② 接口契约不一致** | P0-4、S7、S8、S20、A5、I23、I24、I11 | 检查说能跑、引擎说不能跑；同一键在不同层语义不同 → 「全绿但点开始没反应」「未抢到且无原因」 |
| **③ 单 Engine 复用 + 一次性标记残留** | S2、S14、C2 | 多任务场景必错，且只在第 2 个任务后暴露，最容易被误判成「游戏改了」 |
| **④ 坐标 / DPI / 分辨率** | V02、V05、V01、V04、V19 | 环境一变（1440p、双屏、窗口化）整链路错位，不报错、只表现为「一直没抢到」 |
| **⑤ OCR 误判与解析** | I1、I2、I3、V07、V08、V13、V14、I13 | 读错价格/标题后，逻辑是「正确地执行错误数据」→ 直接成交错单 |
| **⑥ 异常吞没 / 失败静默** | S9、S11、V01、V08、I5、I8、I9、S10 | 失败伪装成「没有/未抢到」，日志无法定位；V01 最危险（OCR 空→盲点） |
| **⑦ 演练/真实双轨** | S3、S8、S20、D4 | 「演练」会真点游戏，等于安全阀失效 |
| **⑧ 资源与并发** | I6、I10、I17、V10、S15、S24、S26 | 长跑/多开/热键连按时暴露；急停不可靠是底线问题 |

**权重排序**：①配置层 ≥ ②契约不一致 > ③单 Engine 残留 > ④坐标 > ⑤OCR > ⑥静默失败 > ⑦演练 > ⑧资源。

---

## ④ 修复路线（分批，每批带验收）

### 第 1 批：让「配置不再丢」+「启动别再假失败」（改完即可正常开工）
- 修：S1（`transaction` 不落盘）、S5/`reload`（失败保留旧配置）、A2（非对象 JSON 归类为 ValueError）、P0-4（确认口径统一）、P0-8（初始化挪进 try）。
- 验收：
  1. 只读/占用 `config.json` 后跑任一 `with Config.transaction()`，磁盘内容**不变**；
  2. `config.json` 写 `"abc"` / `{"queue":{}}` → 有明确提示并留档，进程能起；
  3. 无 confirm 区域/模板时 `preflight`、`Scheduler.start`、`Engine._validate` 三处结论**一致**（都放行或都拦）；
  4. 单测：`Screen` 构造抛异常时 `status()["error"]` 非空、任务结果不是「未抢到」。

### 第 2 批：让「多任务不再串味」+「演练真的只是演练」
- 修：S2（筛选 flag 每任务清零、成功后再置真）、S3（`_ons_ready` 用 `_dry_run`、删重复导航）、S8/S20（F7 与 API 共用演练判定）、S4（`update()` 同步 `active_task`）。
- 验收：
  1. 一轮 3 个任务、价格区间各不同：日志中每个任务都有一次 `_apply_price_filter`；
  2. `dry_run=true` 全程不出现任何注入调用（可用驱动计数器/日志断言）；
  3. 运行中改价格 → 下一个 `LIVE_RELOAD_S` 周期日志显示新值；
  4. `stop.after_hit` 触发时日志出现一条明确 stop，且 ARMED 任务全部回到 PENDING。

### 第 3 批：让「买对货」
- 修：V01（禁止盲点 + 点后校验）、I1/I2/I3、I13、V07/V08、S6/S7（`workflow` 唯一真源 + `clock` 前置检查）。
- 验收：
  1. `skin_items()` 返回空时任务 FAILED（不是盲点）；
  2. 用 `_shots` 回放含「成色 S 05」「100.951000」的卡片，解析出的价格与人工标注一致；
  3. 卡片 6→2 时 `changed` 覆盖全部下标；
  4. 倒计时任务未框 clock 区域时，preflight 直接标红。

### 第 4 批：环境适配与长跑健壮性
- 修：V02/V05、V04/V03、I6、I10、V10、S15/S22、S24/S25/S26、D1/D2。
- 验收：
  1. 2560×1440 回放：ROI 与点击落点都只缩放一次，目视对齐；
  2. 双同分辨率屏 `monitor=2` 选中索引 2；
  3. 连按 F8 10 次无并发 `stop`、钩子不掉；
  4. `records` 边写边清 1000 次无半行；`jobs` 抛 `BaseException` 后仍可再跑。

---

## ⑤ 本次审查的盲区与未验证项

1. **未跑真机/GUI/驱动/OCR 服务**：所有「渲染时序、钩子超时、余额弹窗动画」类结论均为静态推导（已标 `推测`：V12、I5、I14、I6 的实际超时表现）。
2. **`nav_ons.py` / `nav_col.py` 内部状态机**：只合并视觉分报告结论，未独立复核筛选面板滚动与勾选回读。
3. **`ocr_service` 蓝绿切换**：未验证「引擎正在发 OCR 请求时 `restart()`」的失败重试链路。
4. **`apps.json`**：未被上述模块读取，本轮无结论（结构存在但疑似遗留）。
5. **`timesync` NTP 计算**：涉网，按要求跳过，只看了锁与过期策略。
6. **未验证**：S14（join 超时后旧线程仍在点的实际概率）、S26（连按 F8 的真实并发度）、S12（控制台进程模型的真实形态）。

---

## 附：验证记录

```
.venv\Scripts\python.exe -m py_compile core\*.py      -> 34/34 exit 0
.venv\Scripts\python.exe -m pytest tests -q           -> 666 passed, 45 skipped
逐个 import core.<module>（34 个）                     -> 全部 OK（无缺包、无循环导入）
```

一次性复现脚本（`_tmp_review\`，跑完已删除）：

| 项 | 手段 | 结果 |
|---|---|---|
| 确认模板口径矛盾 | `preflight.evaluate` + `Engine._validate` 同一份 cfg | preflight `all_ok=True`；engine 返回「还没框选确认按钮区域」 |
| 事务覆盖配置（S1） | 重定向 `CONFIG_PATH` + 假 `open` 抛 `PermissionError` | 磁盘配置被写成默认值，queue/regions/threshold 全丢 |
| 非对象 JSON（A2） | `"abc"/123/[1,2]` | `AttributeError`（不进 ValueError 分支、不留档） |
| `queue:{}` 类型 | `Config.load()` | `type=dict`（调度按 list 使用 → `AttributeError`） |
| 自线程 join（S11） | 真实 `Engine` + `_thread=current_thread()` | `RuntimeError: cannot join current thread` |
| `Keyboard.click`（I11） | `Mouse.__new__` 复刻 | `_mode`/`_drv_warn` 均不存在 |
| 显示器选择（V05） | monitors=[虚拟,1920×1080,1920×1080] | `want=0/1/2 → 0/1/1` |
| 缓存防护（I13） | 3 卡→4 卡、帧尺寸一致 | `changed_cards = []` |
| 演练无确认（D4） | `vision.expand(None, 0.25)` | `TypeError` |
| `countdown.parse_words`/`Clock` | 断言脚本 | 解析与跳秒逻辑无 bug（`0分6秒→6`、`2天23小时→None`、`due/is_stale` 正常） |
