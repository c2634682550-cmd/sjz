# 测试体系有效性静态审查（R21）

- 范围：`tests/` 70 个测试文件、861 个 `def test_*`，对照 `core/` 全部模块 + `server.py` / `desktop.py` / `web/index.html`
- 方法：**只读静态推导**。读测试与被测源码、按 import 与符号做覆盖映射、读 `tests/conftest.py`、`pyproject.toml`、`.pytest_cache/v/cache/lastfailed`（只读）
- 未执行任何程序、未改动任何源码、未 commit / push
- 判定标准只有一条：**把这个实现改坏，测试会不会红**

---

## 1. 结论摘要

1. 861 个用例里，真正能拦 bug 的集中在 `precision` / `license(离线)` / `input(通道决策)` / `glyphs` / `validate` / `ocr_service` / `process_lifecycle` / `desktop` / `packaging` 这几块；剩下的多数是"桩上验桩"。
2. **前端 10 条静态测试整体失效**：`test_frontend_static.py` 断言的是 `_fe_old_backup/index.html` 的符号（`实战`、`sbMode`、`function wearTag`、`STATE.pollSeq`、`condErr`、`dry_run`），当前 `web/index.html` 已重写，这些符号全部不存在。实测 9 条断 `src.index(...)` 必抛 ValueError，1 条只查 `&#39;` 子串存在而空转。前端实际覆盖 = 0。
3. **恢复能力"200 组 0 失败"是自己证自己**：`tests/scene_sim.py` 自己定义 `REQUIRED_GROUPS` / `fix_goal` / 动作成功即修复，再由 `test_recovery.py` / `test_scene_guard.py` 断言该模拟器的产出。
4. **有 4 个 core 模块在 tests 里零引用**：`retention.py`、`watchdog.py`、`selfcheck.py`、`digit_ocr.py`。另有 `license` 的联网激活/续租、`server` 的 9 个路由、`engine` 的 35 处 `except` 基本无覆盖。
5. 4 个模块整组被 `conftest.py` 的 `NEEDS_REAL_OCR` 静默 skip（`popup` / `market_video` / `engine_insufficient` / `skin_ocr_real`）。在没有 OCR 的机器上"全绿"= 0 条断言。

---

## 2. 按模块信任度表

| 模块 | 主测试 | 信任度 | 判定依据 |
|---|---|---|---|
| `core/precision.py` | test_precision | **高** | 真 QPC、真线程、跨进程复核，阈值是真实性能判据 |
| `core/glyphs.py` | test_glyphs | **高** | 逐模板真图片识别 + 阈值/越界断言 |
| `core/validate.py` | test_validate | **高** | Infinity / 负数 / 小数 / 枚举 / 后端二次校验全断 |
| `core/preflight.py`(+integrity) | test_preflight, test_integrity | **高** | 真环境探测与签名/哈希断言 |
| `core/ocr_service.py` | test_ocr_service | **高** | 蓝绿切换、孤儿进程、端口占用、冷启动、锁阻塞 |
| `core/input.py`（ABI + 通道决策） | test_input_abi, test_input_channels | **高** | INPUT/MOUSEINPUT sizeof/offset 是外部契约；通道降级断外部结果 |
| `core/desktop.py` | test_desktop_jsapi | **高** | 真实的 pywebview 递归展开事故回归，断言公开面 |
| `core/license.py`（离线验签） | test_license | **高** | RFC8032 已知答案、篡改、回滚、撤销、试用生命周期 |
| `core/skinfilter.py` | test_skinfilter | **高** | 243 条全量映射 + 空组合反例 |
| `core/packaging`(spec↔数据) | test_packaging | **高** | 枚举真实 `core/` 数据文件比对 spec，能拦漏打包 |
| `core/process` 生命周期 / 配置事务 / 单实例 | test_process_lifecycle | **高** | 真并发写盘、事务合并、Mutex 抢占 |
| `core/robustness`(并发/兜底) | test_robustness | **高** | 真并发写盘、队列韧性、500 兜底 |
| `core/countdown.py` | test_countdown | **中** | 算法真测，但 `ok>=8/20`、`isinstance(int)` 两处阈值放空 |
| `core/engine.py` | test_engine_*（约 90 例） | **中** | 路由/流程骨架真测；执行器与 `_navigate` 大面积替换为 lambda，35 处 `except` 无触达 |
| `core/nav.py` / `nav_coords` | test_nav, test_nav_onsale | **中** | 坐标数据实，但 buy 守卫与 `_ocr_words` 调用被桩化/断言实现细节 |
| `core/nav_col.py` | test_skinfilter, test_skin_api | **中** | 筛选映射实，但枪种维度未验（见 3.4） |
| `core/nav_ons.py` | test_nav_onsale, test_nav_pub_filter | **低** | 替身 `click` 绕过安全坐标校验；精确点击次数绑常量 |
| `core/market.py` | test_market | **中** | 价格解析真，但"后端不可用"直接 return 静默通过 |
| `core/cards.py` | test_cards | **低** | 缓存重认次数上下界双向放空（`<=2` / `>=6`） |
| `core/bricks.py` | test_bricks_catalog, test_skin_api | **低** | 枪种过滤断言与被测签名不匹配，等于没测 |
| `core/switches.py` | test_switches | **低** | 关键用例把 `_follow` 换成测试自己的 lambda 后自证 |
| `core/cursor`/`nocursor` | test_cursor_lock, test_input_channels | **中低** | 通道决策有真覆盖；`test_cursor_lock` 仅断 `isinstance(bool)` |
| `core/hotkey.py` | test_hotkey_start | **低** | 只测 server 的开关函数，`Hotkey` 类（注册/回调/去抖）零覆盖 |
| `core/scheduler.py` | test_scheduler_* | **中** | 入队/配额/条件真测，`save()` 被空实现，落盘与回滚未验 |
| `core/server.py` | test_server, test_console_api | **中** | 主要路由行为级；9 个路由零覆盖；全局 `_scheduler` 与真实 config 共享 |
| `core/ocr.py`/`chain`/`ppocr`/`rapid` | test_ocr_backend, test_ocr_price, test_ocr_grade | **中** | 超时/500/熔断真测；`errorCode≠0`、PNG 编码失败、`_broken` 早退未测 |
| `core/screen.py` | test_screen | **低** | 依赖真 `GetSystemMetrics`，显式屏号 ValueError、`grab`、`norm_to_px` 零覆盖 |
| `core/records.py` | test_records | **中** | 读写/脏行/统计真；缓存与轮转零覆盖 |
| `core/fire_proc.py` | test_fire_proc, test_fire_process | **中** | 子弹进程往返真；时间阈值随机器负载翻转 |
| `core/pagewatch.py` | test_fix_a, test_resume | **低** | 仅 `_make_nav`/`_refresh_once` 被间接触达，`start/stop/_loop/_signature` 零覆盖 |
| `core/timesync.py` | test_fix_a | **低** | 仅正常路径，`_query` 失败/回滚零覆盖 |
| `core/actions.py` | test_fix_c | **中低** | 价格带/排除键有真断言，其余靠桩 |
| `core/recovery.py` | test_recovery | **低** | `performer` 全桩、每个恢复动作实现零验证、0 失败由模拟器自定 |
| `core/scene_guard.py` | test_scene_guard | **中高** | 矩阵/优先级/滞回/术语真测；但统计基线来自 `scene_sim` |
| `core/digit_ocr.py` | — | **零** | tests 内零引用；`test_ocr_price` 用 `type("T",…)` 假对象 |
| `core/retention.py` | — | **零** | tests 内零引用 |
| `core/watchdog.py` | — | **零** | tests 内零引用 |
| `core/selfcheck.py` | — | **零** | tests 内零引用 |
| `core/jobs.py` | — | **零** | 仅 `current()` 之外被间接调用，无测试 |
| `web/index.html` | test_frontend_static | **零（且当前红）** | 断言的是已废弃前端符号 |
| `run.py` / `tools/*` | — | **零** | 无引用 |

---

## 3. 高危问题

### 3.1 前端静态测试测的是已经不存在的文件
- **严重度**：高
- **模块与测试**：`web/index.html` ↔ `tests/test_frontend_static.py:35-90`
- **为什么拦不住**：全部是 `src.index("function wearTag")` / `"实战" in src` / `"sbMode" in src` 一类字符串断言。当前 `web/index.html` 不含 `实战`、`sbMode`、`function wearTag`、`STATE.pollSeq`、`condErr`；这些只存在于 `_fe_old_backup/index.html`。即函数名在、函数体改坏也照样过；而真实前端已重写却无人发现测试失效——测试的"存在感"比它的保护力大得多。
- **怎么改**：把纯函数（`esc`、`wearTag`、条件渲染）抽到独立 `.js`，用 node + vitest 跑真实输入输出；HTML 层只保留"元素 id 存在"的冒烟。删除 `src.index` 这种代号断言。

### 3.2 恢复能力的"200 组 0 失败"是模拟器自证
- **严重度**：高
- **模块与测试**：`core/recovery.py` ↔ `tests/test_recovery.py:27-29,203-230`；`tests/scene_sim.py:140-197,251-280,283-343`
- **为什么拦不住**：`scene_sim.SimEnv._perform` 里动作成功就直接 `fix_goal=True`，期望值 `REQUIRED_GROUPS`、`MANUAL_PROB` 全由测试自己定义；`performer=lambda: {"ok": True}` 让所有恢复动作恒成功。于是"恢复成功率 100%、0 失败"只证明模拟器与断言一致，与真实 `recovery.ACTIONS`（freeze / release_input / reset_cursor / unhook / reap / close_capture / record）能不能恢复没有任何关系。
- **怎么改**：把该测试降级、重命名为"编排冒烟"；恢复能力用真实信号回放（仓库已有截图/遥测 fixture）或独立预言机；至少为每个 `ACTIONS` 项写"构造真实故障态 → 调动作 → 断言状态恢复"的用例。

### 3.3 `switches` 关键用例把被测逻辑换成测试自己的函数
- **严重度**：高
- **模块与测试**：`core/switches.py` ↔ `tests/test_switches.py:259-288`
- **为什么拦不住**：用例 `monkeypatch` 掉 `eng._follow`，然后把**自己装的 lambda** 再调一遍并断言 `called == [True]`；关闭分支写成 `if not switches.get(...): pass` 之后断言 `called2 == []`——空转。真实引擎里"进详情后按开关调 `Nav.set_follow`"的调用点一次都没有执行。开关读错、调用点被删、条件写反，这条测试仍然绿。
- **怎么改**：走真实买入流程，用假 `Nav` 记录 `set_follow` 调用次数，断言"开=1 次 / 关=0 次"，并断言开关值来源（config 而非默认）。

### 3.4 `skins_of` 的枪种过滤断言与被测签名不匹配
- **严重度**：高
- **模块与测试**：`core/bricks.py` ↔ `tests/test_skin_api.py:39-47`
- **为什么拦不住**：`bricks.skins_of(season=None, tier=None, series=None)` **没有 gun 参数**，测试却断言"返回行的 `gun` 全等于某一枪"，而该枪取自全库第一行；`rows` 为空时 `:41-45` 提前 `return`。这条要么必然误报、要么空跑。"按枪种过滤"这个产品能力从未被验证。
- **怎么改**：确认产品是否需要 gun 维度——需要则给 `skins_of` 加参数并断言"两种枪互斥"；不需要则删掉伪断言，改成对 `catalog` 的 gun 字段分布与 `NavColPanel.GUN_ROWS` 的一致性校验。

### 3.5 `test_server.py` 共享全局调度器并直写真实配置
- **严重度**：高
- **模块与测试**：`core/server.py` ↔ `tests/test_server.py:27-45,298-316`
- **为什么拦不住**：`server` 是 module 作用域 fixture，`_scheduler` 是导入期创建的全局单例；`:299` 直接 `srv._scheduler.add(...)` 写队列，`:316` 靠用例末尾 `remove` 收尾——断言失败就残留，污染后续用例和用户 `config.json`。`:40-45` 还读真实 `config.json`，机器/配置不同则红绿不定。顺序一换就假红/假绿。
- **怎么改**：autouse fixture 把 `scheduler.Config`、`CONFIG_PATH`、`DATA_DIR` 全部指到 `tmp_path`；`add` 用 `try/finally` 清理；服务器 fixture 改 function 作用域。

### 3.6 四个模块整组被静默 skip，绿是"跳过绿"
- **严重度**：高
- **模块与测试**：`core/popup.py`、`core/market.py`（视频路径）、`core/engine.py`（余额不足路径）、`core/nav_col.py`（典藏页） ↔ `tests/conftest.py:52-98`
- **为什么拦不住**：`NEEDS_REAL_OCR` 命中的文件整个 `pytest.skip`，`pyproject.toml` 里没有 `[tool.pytest.ini_options]`，既无 `--strict-markers` 也无 skip 计数门禁。没有 OCR 的机器上这 4 个模块就是 0 断言，但报表显示"passed"。
- **怎么改**：把纯逻辑断言（点击次数、窗口时长、坐标、充值点永不被点）与真 OCR 分层，前者用假 OCR 常跑；层二用 `pytest.mark.real_ocr` 并在 CI 强制开启；CI 输出 skip 数量并对 `NEEDS_REAL_OCR` 做阈值告警。**尤其 `engine_insufficient`（资金安全，点击上限 ≤120）不该整组跳过。**

### 3.7 `license` 的联网激活/续租零覆盖
- **严重度**：高
- **模块与测试**：`core/license.py:599-655`（`_post_json` / `activate` / `refresh`）↔ `tests/test_license.py`
- **为什么拦不住**：25 个用例全部集中在离线验签、试用、撤销、时钟回拨；`grep activate|refresh|_post_json` 在 tests 里 0 命中。401、超时、半截 JSON、重放、重复激活都无人测——而这正是授权被绕过/被误杀的高发面。
- **怎么改**：起本地假 HTTP 服务覆盖 200/401/403/超时/坏 JSON；断言"网络失败必须保持旧 license 且不进 grace 误降级"。

### 3.8 `OCR errorCode≠0` 与编码失败分支无覆盖
- **严重度**：高
- **模块与测试**：`core/ocr_ppocr.py:207-209`、`core/ocr.py:96-97` ↔ `tests/test_ocr_backend.py:225-298`
- **为什么拦不住**：假服务只造了超时与 HTTP 500（走 urllib `HTTPError`），没有造 `{"errorCode": 123}` 的 200 响应；`imencode` 失败返回 `[]`、`_broken` 早退（`test_ocr_price.py:198` 显式置 False）也未触达。服务端换了错误码语义就会静默返回空结果，而测试仍绿。
- **怎么改**：假服务加 `errorCode` 分支，断言 strict 抛 `OCRError`、非 strict 返回空且熔断计数 +1。

---

## 4. 中危问题

| # | 严重度 | 模块 / 测试 | 为什么拦不住 | 怎么改 |
|---|---|---|---|---|
| 1 | 中 | `core/cursor` ↔ `test_cursor_lock.py:24-32` | 只 `assert isinstance(x, bool)`，`return (True, False)` 即通过；`GetClipCursor/ClipCursor` 调用序列与"API 失败"分支未验 | monkeypatch `_user32`，断言 未锁/已锁/失败 三种组合与调用次数 |
| 2 | 中 | `core/cards.py` ↔ `test_cards.py:146,156` | `extra <= 2` 允许 0 次重认（缓存永不失效也绿）；`>= 6` 允许全量重认（每次都全读也绿）。核心行为被双向放空 | 收紧为 `== 2` / `== 6`，或断言"重认的具体卡号集合" |
| 3 | 中 | `core/engine.py` ↔ `test_engine_dispatch.py:64-68` | 四个执行器和 `_navigate` 全替成 lambda，`_run` 的真实前置（屏幕/hwnd/异常处理）不执行 | 保留路由测试，另加"导航抛 `NavError` → 必须 Aborted/报错而非继续" |
| 4 | 中 | `core/engine.py` ↔ `test_engine_reward_flow.py:161` | 期望坐标用被测的 `Engine._center` 现算，公式写错两边一起错 | 写死实测像素常量 |
| 5 | 中 | `core/engine.py` ↔ `test_engine_list_queue.py:90-98,280` | `FakeNav.page/can_switch/price_returns` 是类变量，用例直接改写且无 autouse 复位 → 顺序依赖 | autouse fixture 每例重置全部类变量 |
| 6 | 中 | `core/scene_guard.py` ↔ `test_scene_guard.py`（统计段） | 200 组成功率基线来自 `scene_sim` 自定义 `REQUIRED_GROUPS`，与真实分类器无关 | 用真实截图回放做基线，模拟器仅保留确定性单测 |
| 7 | 中 | `core/ocr_service.py` ↔ `test_ocr_service.py` | `_spawn/_kill/_rotate` 全 stub，只验编排 | 至少加一条"子进程崩溃 → 自动重启"的真进程用例 |
| 8 | 中 | `core/nav_col.py` ↔ `test_nav_filter_clear.py` | `Navigator.__new__` 手搭 6 个属性；实现新增依赖即失效 | 用真实构造函数 + 注入依赖，而非裸 `__new__` |
| 9 | 中 | `core/nav_ons.py` ↔ `test_nav_pub_filter.py:86-97,129` | 替身 `click` 绕过坐标白名单/落点校验/变更确认；`:129` 死绑"6 次点击" | 只桩底层输入，保留真实 `click` 守卫；点击次数按事件集合断言 |
| 10 | 中 | `core/market.py` ↔ `test_market.py:167-170` | `assert isinstance(name, str) and name`；后端不可用直接 `return` → 坏掉也是绿 | 不可用路径断言 `read_price is None` 且不抛；可用路径拆成必跑 |
| 11 | 中 | `core/scheduler.py` ↔ `test_scheduler_add.py:29-30` | `save()` 空实现，落盘/事务/回滚完全未验，`add` 只证明内存里有值 | 用真 `Config` + `tmp_path`，断言文件内容与事务失败回滚 |
| 12 | 中 | `core/scheduler.py` ↔ `test_scheduler_queue.py:121-123` | 全局 mock `os.path.isfile/shutil.copyfile`，模板复制失败分支永不触发 | 复制失败用真实权限/不存在路径构造 |
| 13 | 中 | `core/hotkey.py` ↔ `test_hotkey_start.py:47-49` | `FakeConfig.__getitem__` 里 `assert k=="queue"`：新增配置键会以断言失败掩盖真 bug；`Hotkey` 类零覆盖 | 用真 `Config`（内存）与假 `user32`；补注册/回调/去抖用例 |
| 14 | 中 | `core/screen.py` ↔ `test_screen.py:35` | 依赖真 `GetSystemMetrics`，异分辨率必红；显式屏号 ValueError、`grab`、`norm_to_px` 零覆盖 | 注入 metrics provider；补 DPI/越界用例 |
| 15 | 中 | `core/timesync.py` ↔ `test_fix_a.py` | 只走正常路径，`_query` 失败、`true_now` 回退零覆盖 | 假 socket 返回错误/超时，断言不污染系统时钟 |
| 16 | 中 | `core/ocr_price`… ↔ `test_ocr_price.py:152-159` | `type("T",…)` 桩替代 `digit_ocr`，三级兜底只验路由不验读数 | `digit_ocr` 用真实字体模板单测（`read_number/segment/binarize` 现为零覆盖） |
| 17 | 中 | `core/soak` ↔ `test_soak.py:248` | `assert v in ("PASS","FAIL","INCOMPLETE")` 恒真（三值全覆盖），等于没断言 | 用固定指标数据集断言具体结论与关键数值 |
| 18 | 中 | `core/engine.py` ↔ `test_engine_shot.py:89` | `match` 恒返回 0.99（阈值 0.8），`>=`/`>` 写反也过 | 加 0.799 / 0.801 边界 |
| 19 | 中 | `core/countdown.py` ↔ `test_countdown.py:878,909` | `ok >= 8`（共 20）允许退化一半；`:909` 只断 `isinstance(int)` | 与 bench 阈值对齐，按窗口断言分布 |
| 20 | 中 | `core/server.py` ↔ `test_console_api.py:230-238` | 只断 key 存在，引擎统计算错照样过 | 断言数值由什么决定（guard level 的输入→输出映射） |
| 21 | 中 | `core/config.py` ↔ `test_config.py` | 仅 4 条正常路径；损坏/迁移/白名单边界在别处零散 | 补"读失败→默认值""未知键丢弃""输入模式归一"（`normalize_input_mode` 零覆盖） |
| 22 | 中 | `core/interception.py` ↔ `test_interception.py:26-60,78` | 常量与实现同源（头文件写错测试一起错）；`:78` 收集期加载真 DLL，不可还原且会改变后续用例环境 | 常量改从外部契约取；DLL 加载移入 fixture 并 `finally` 释放 |

---

## 5. 零覆盖清单

### 5.1 整模块零引用（`grep` 在 `tests/` 内 0 命中）
| 模块 | 未覆盖的公开函数 |
|---|---|
| `core/retention.py` | `_size:28`、`rotate:35`、`archives:67`、`prune_dir:77` |
| `core/watchdog.py` | `beat:27`、`age:31`、`snapshot:39`、`reset:44`、`poll:63`、`info:91` |
| `core/selfcheck.py` | `models:49`、`driver:66`、`webview2:101`、`fonts:125`、`privilege:134`、`license_status:151`、`doctor_lines:159` |
| `core/digit_ocr.py` | `read_number:656`、`read_detail:576`、`read_detail_ex:619`、`read_digits:707`、`ensure_templates:95`、`load_templates:158`、`segment:268`、`binarize:211`、`DigitReader:666` |
| `core/jobs.py` | `current:98` |
| `run.py` / `tools/*` / `build` 脚本 | 全部 |

### 5.2 模块内零覆盖函数
- `core/engine.py`：`guard_snapshot:372`、`guard_stop:381`、`scan_first_page:899`、`sweep:965`；另有 **35 处 `except` 分支**（含 `:514,531,555,583,1683,1991,2065,2123,2236,2263`）无任何用例触达
- `core/scheduler.py`：`_recover_stale_tasks:111`、`clear_finished:383`、`_hit_stop:526`、`guard_status:553`、`guard_stop:569`
- `core/hotkey.py`：`Hotkey` 类 `:256-341`（注册、回调、去抖）
- `core/screen.py`：`grab:108`、`norm_to_px:125`、`_pick_monitor` 显式/无效分支 `:92-101`
- `core/pagewatch.py`：`start:60`、`stop:67`、`_loop:157`、`_signature:92`
- `core/license.py`：`activate:623`、`refresh:638`、`_post_json:599`、`mark_online:401`
- `core/vision.py`：`match:68`、`expand:83`、`upscale:94`
- `core/config.py`：`drop_task_dir:601`、`normalize_input_mode:194`、`normalize_input_fallback:203`
- `core/switches.py`：`apply_all:407`
- `core/timesync.py`：`_query:27`、`true_now:106`、`sync` 失败分支 `:62-66`
- `core/single_instance.py`：`activate_window:81`、`CreateMutexW` 失败分支 `:45-49`
- `core/nocursor.py`：`probe:371`、`selftest:406`、`window_alive:90`、`window_class:97`、`window_responsive:108`（通道决策经 `input.Mouse` 有行为覆盖，独立入口无）

### 5.3 `server.py` 路由覆盖差
- **零覆盖**：`GET /api/jobs:216`、`GET /api/skins/plan:261`、`POST /api/tasks/clear:290`、`/api/scheduler/stop:294`、`/api/guard/stop:297`、`/api/timesync:299`、`/api/follow:305`、`/api/switches:306`、`/api/quit:308`
- **有覆盖**：`/api/config`、`/api/tasks`、`/api/tasks/add|update|remove`、`/api/scheduler`、`/api/guard(GET+resume)`、`/api/hotkey(GET)`、`/api/preflight`、`/api/records(+clear)`、`/api/skins(GET|target|locate)`、`/assets/*`、`/shots/*`
- 函数级零覆盖：`_preflight_gate:839`、`_next_task_route:801`、`_premarket_fire:913`、`_make_hotkey:943`、`_start_hotkeys:959`、`_seed_default_task:996`、`_start_ocr_service:1023`、`_startup_checks:1059`、`serve:1093`

---

## 6. 「测试全绿」不可信的模块

按不可信程度排序：

1. **`web/index.html`（前端）** —— 10 条静态测试对的是已废弃版本，当前实现 0 断言；如果报表显示绿，那是子串巧合或根本没跑。
2. **`core/recovery.py` / `core/scene_guard.py` 的统计段** —— "0 失败 / 恢复率 100%" 由 `scene_sim` 自己定义成功条件，全绿是构造出来的。
3. **`core/switches.py`** —— `follow_before_buy` 把被测调用点替换成测试自己的 lambda，绿不代表开关在工作。
4. **`core/bricks.py`（guns 维度）** —— 断言与被测签名不匹配，绿是空跑。
5. **`core/engine.py` 的"余额不足 / 无对话框 / 奖励流程"** —— `NEEDS_REAL_OCR` 下整组跳过；`_navigate`、`_buy_button_visible`、`_chip_ready` 多处被桩化。
6. **`core/popup.py` / `market_video` / `skin_ocr_real`** —— 无 OCR 时 0 断言。
7. **`core/cursor`** —— 只断 `bool` 类型。
8. **`core/soak`** —— 结论断言是三值枚举恒真。
9. **`core/hotkey`** —— 只有 server 包装层被测，真正的热键注册/回调节奏无人验。
10. **`core/digit_ocr` / `retention` / `watchdog` / `selfcheck`** —— 不是"绿"，是**没有测试**；这四个模块任何 PR 都能无声通过。

---

## 7. 测试质量确实好的模块

- **`core/precision.py`**：真 QPC、真线程、跨进程复核，阈值是真判据，不是自证。
- **`core/license.py`（离线部分）**：RFC8032 官方向量、篡改/回滚/撤销/试用状态机，都能拦住真实回归。
- **`core/input.py`（ABI + 通道决策）**：`INPUT/MOUSEINPUT` 的 `sizeof/offset` 是外部契约；通道降级账本断外部可观测结果。
- **`core/glyphs.py`**：逐模板真图片识别 + 分数/阈值/越界。
- **`core/validate.py`**：Infinity / 负数 / 小数 / 枚举非法值 / 后端二次拦截都断到位。
- **`core/ocr_service.py`**：蓝绿切换、孤儿进程、端口占用、冷启动、锁阻塞，分支覆盖最完整的一块。
- **`core/process` 生命周期与配置事务**（`test_process_lifecycle.py`）：真并发 30 轮写盘、事务不覆盖他人字段、单实例抢占/释放，都是行为级断言。
- **`core/robustness` 并发与 500 兜底**、**`core/packaging` 漏打包检测**、**`desktop.WinCtl` 公开面回归**：都有明确事故背景、断言外部可观测结果。
- **`core/skinfilter.py`**：243 条全量映射 + 空组合反例。

---

## 8. 建议的修复顺序（只列，不改）

1. 先修"假绿"：3.1 前端、3.3 switches、3.4 skins_of —— 这三处会让人误判系统有保护。
2. 再补"资金/授权"路径：3.6 `engine_insufficient`、3.7 `license` 联网、3.8 `errorCode≠0`。
3. 再做"环境隔离"：3.5 `test_server` 全局状态 + `tmp_path`，消除顺序依赖。
4. 最后补零覆盖：`retention/watchdog/selfcheck/digit_ocr` 四个模块，以及 `server` 的 9 个零覆盖路由。
