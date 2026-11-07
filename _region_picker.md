# 区域框选：核实 / 文案修正 / 补验

结论先行：`GET/PUT /api/regions`、整页框选 UI、落盘与引擎读取都在树里，链路完整。
本轮唯一实质缺口是**文案与事实相反**（界面说「没有框选页」、命令行工具 docstring
说「前端框选入口还没做」），已改；另外补了 27 条接口契约测试和三档缩放等价验证。

---

## 1. 核实：逐条给证据

### 1.1 `GET /api/regions` 返回什么

- 路由：[core/server.py](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L425-L430)
  → `_regions_get()` [server.py#L778-L781](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L778-L781)
  → `regions.describe()` [core/regions.py#L178-L215](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L178-L215)。
- 每个区域返回 `name / label / norm / px / source / editable / note`：
  - `source` 四态：`user`（用户设定）、`default`（引擎内置默认）、`unset`（没设）、
    `invalid`（盘上手改坏了，不冒充用户设定）——[regions.py#L186-L205](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L186-L205)。
  - `px` 是"实际生效像素矩形"，由 `pixel_rect()` 调
    `Screen.norm_to_px()` 算出——[regions.py#L58-L61](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L58-L61)。
  - `screen` 里带 `width/height/dpi/scale_percent/monitor/game_rect/pick_reason`
    ——[regions.py#L216-L231](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L216-L231)。
- 拿不到屏幕时不清零、不冒充：`screen.available=false` + `error` 原文
  ——[server.py#L766-L776](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L766-L776)。

### 1.2 `PUT /api/regions` 的校验

路由是 `do_PUT`：契约就是 PUT，不是拿 POST 凑
——[server.py#L550-L563](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L550-L563)。
逐条规则（全部在 [core/regions.py](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L75-L160)）：

| 规则 | 实现位置 | 行为 |
|---|---|---|
| 数值必须有限 | `_num()` 用 `math.isfinite`，且单独挡 `bool` | NaN/Inf/-Inf/字符串/布尔全拒 |
| 0..1 且不出界 | `x<0 / y<0`、`x+w>1`、`y+h>1`、`w>1 / h>1` | 400，中文说明带实际数值 |
| `x1<x2`、`y1<y2` | 同时接受 `x/y/w/h` 与 `x1/y1/x2/y2` 两种形状 | 倒置直接拒 |
| 最小宽高 | `MIN_W = MIN_H = 0.01`（[#L23-L24](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L23-L24)） | 防拖成一条线 |
| 未知键拒收 | `_SPEC_BY_NAME` 白名单（键集合 = `config.DEFAULTS["regions"]`） | 400，列出可选项 |
| 不生效的旧键 | `name/price/list_refresh` 标 `editable=false` 并给原因 | 400，不假装能改 |
| 非法不落盘 | `_regions_save` 先 `validate_update`，全过之后才进事务 | 400 时连 `Config.load()` 都不做 |

- 校验全过才进 `Config.transaction()`，块内异常不落盘
  ——[server.py#L783-L800](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L783-L800)，
  [config.py#L995-L1021](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L995-L1021)。
- 保存 = 临时文件 + `os.replace` + `.good` 备份（备份本身也走临时文件+读回校验）
  ——[config.py#L901-L918](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L901-L918)，
  [config.py#L942-L949](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L942-L949)。
- `null` 表示恢复默认（删除用户设定）——[regions.py#L149-L151](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L149-L151)。

### 1.3 界面拖拽真的会调用它

- 画布与工具栏：[web/index.html#L1284-L1313](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1284-L1313)。
- 指针拖拽（画新框 / 框内移动 / 四角缩放）：
  [index.html#L3816-L3872](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3816-L3872)，
  坐标归一化在 `rpPoint()`，落到 `RP.draft`。
- 数值微调（x/y/w/h 输入框）与"恢复单项/全部默认"：
  [index.html#L3878-L3899](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3878-L3899)，
  [index.html#L3974-L3991](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3974-L3991)。
- 保存确实发 PUT：`api('/api/regions', {regions}, {method:'PUT'})`
  ——[index.html#L3993-L4018](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3993-L4018)。
- 截图走 `GET /api/screen/capture`，抓的是引擎同一块屏
  ——[index.html#L3920-L3972](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3920-L3972)，
  [server.py#L802-L820](file:///C:/Users/26346/Desktop/project/dafors/core/server.py#L802-L820)。
  "抓取画面"按钮带 3 秒倒数，给用户切回游戏的时间，不抢鼠标。

### 1.4 保存后引擎下次读取用新坐标

链路（每一跳都有代码位置）：

```
指针拖拽 → RP.draft            index.html:3816-3872
        → PUT /api/regions     index.html:4000
        → 校验 + 事务落盘        server.py:783-800 / config.py:995-1021
        → Config.load()        engine.py:253
        → cfg.region("buy")    engine.py:884
        → screen.norm_to_px()  screen.py:369-395
```

- 引擎启动时一次性 `cfg = Config.load()`：[engine.py#L253](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L253)；
  列表路线 [#L1311](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1311)、倒计时 [#L1120](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L1120)、
  购买按钮 [#L884-L885](file:///C:/Users/26346/Desktop/project/dafors/core/engine.py#L884-L885)。
- 语义是"下一次开抢生效"：正在跑的那一轮用启动时的快照。界面文案已按这个口径写
  （[index.html#L1285](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1285)）。

### 1.5 无假按钮

- 后端把 `name/price/list_refresh` 标成 `editable=false` 并附原因
  ——[regions.py#L31-L44](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L31-L44)。
- 前端只把可编辑项放进下拉框；列表里不可编辑项置灰、标签写"不可编辑"、悬停给原因，
  点它只弹原因不改值——[index.html#L3674-L3682](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3674-L3682)，
  [index.html#L3889-L3896](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3889-L3896)。

---

## 2. 文案修正

| 位置 | 原文 | 现在 |
|---|---|---|
| [web/index.html#L1554](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L1554) | "这一版没有框选页，区域标定要手动改 config.json" | "区域标定在「设置 → 区域框选」里完成：先抓一张游戏画面，再在图上拖框，保存即生效，不用手改 config.json" |
| [说明.txt#L213-L240](file:///C:/Users/26346/Desktop/project/dafors/说明.txt#L213-L240) | 标题"（这一版界面里没有框选页）"+ 整节教人改 JSON | 改成界面拖框步骤；保留 CLI 作为批量/CI 备选；写明"换模板要重启，区域保存后不用重启" |
| [tools/calibrate.py#L5](file:///C:/Users/26346/Desktop/project/dafors/tools/calibrate.py#L5) | "前端的框选入口还没做" | "界面里已有「设置 → 区域框选」；这个命令行工具留给批量/无界面/CI 场景" |
| [_fe_verify.py#L414-L422](file:///C:/Users/26346/Desktop/project/dafors/_fe_verify.py#L414-L422) | 断言"明确没有框选页"、找 `copy-cmd` | 反过来断言引导指向「区域框选」、视觉页有 `rp-grab`；否则该脚本会把正确实现判成回归 |

全页扫过一遍，其余提到 `regions/config.json` 的地方都与现状一致：
- [README.md#L32](file:///C:/Users/26346/Desktop/project/dafors/README.md#L32) 说"设置页重新框选并截图"——现在成立。
- [core/preflight.py#L31-L40](file:///C:/Users/26346/Desktop/project/dafors/core/preflight.py#L31-L40) 的校准提示讲的是 `--calibrate` 命令行补模板，命令真实存在（`tools/calibrate.py`），不算过期。
- 引导第 1 步 [index.html#L4030](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L4030) 已于前一轮改成"去「设置 → 区域框选」"。

`_bug_r11_usability.md` / `_gaps.md` / `_fe_fixes.md` 等是历史审查快照，保留原样，不回改。

---

## 3. 补验

### 3.1 接口契约测试（新增 `tests/test_regions_api.py`）

27 条全过：`python -m pytest tests/test_regions_api.py -q` → `27 passed`。

覆盖点与用例：

| 验的点 | 用例 |
|---|---|
| GET 全量 + 来源 + 像素矩形 | [test_get_reports_every_region_with_source_and_pixels](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L120) |
| 没有屏幕也不报 500 | [#L146](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L146) |
| PUT 落盘 + 引擎读新值 + px 一致 | [#L157](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L157) |
| `x1/y1/x2/y2` 形状 | [#L183](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L183) |
| `null` 恢复默认 | [#L192](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L192) |
| 越界/倒置/过小/未知键/不生效键/字符串/布尔/缺字段/空体 → 400 且磁盘字节不变 | [#L219-L230](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L219-L230)（13 组参数） |
| NaN / +Inf / -Inf → 400 且磁盘不变 | [#L232-L238](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L232-L238) |
| 一项合法一项非法 → 整单拒绝 | [#L241](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L241) |
| 老配置没有 `regions` → 默认值、不报错 | [#L254](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L254) |
| 三档缩放换算与引擎同一函数 | [#L265](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L265) |
| 抓帧 PNG / 无屏 503 可读错误 | [#L284](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L284) / [#L293](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L293) |

"磁盘字节不变"是比较请求前后的 `read_bytes()`，同时确认没有新生成 `.good`。

### 3.2 真机跑了一遍（1920×1080 100%）

用真实 `Screen` 起临时服务，手工打接口：

```
SCREEN  width=1920 height=1080 dpi=96 scale=1.0 awareness=2(per-monitor-aware)
GET buy → norm {0.7,0.8,0.1,0.06} → px {1344,864,192,65}  source=user
PUT buy {0.5,0.5,0.2,0.1} → 响应 px {960,540,384,108}
DISK   {"x":0.5,"y":0.5,"w":0.2,"h":0.1}
ENGINE Screen.norm_to_px(Config.load().region("buy")) → (960,540,384,108)
```

响应 px、盘上值、引擎换算三者逐位一致。

### 3.3 125% / 150% / 200%

本机是 96 DPI（100%），切系统缩放要注销，没法在当前会话里真的切三档。
用等价验证 + 静态证据：

- 全项目归一化→像素只有一处实现：`Screen.norm_to_px`
  ——[screen.py#L369-L395](file:///C:/Users/26346/Desktop/project/dafors/core/screen.py#L369-L395)；
  文档明确"per-monitor aware 下 `self.size` 是物理像素，不再乘缩放"。
- 接口返回的 `px` 也走这一个函数（`pixel_rect` 借 `Screen.norm_to_px`，不复制四舍五入）
  ——[regions.py#L58-L61](file:///C:/Users/26346/Desktop/project/dafors/core/regions.py#L58-L61)。
- 界面显示的像素由 `rpPx()` 用**同一组** `screen.width/height` 按同一公式
  `round(norm × 物理尺寸)` 算——[index.html#L3626-L3631](file:///C:/Users/26346/Desktop/project/dafors/web/index.html#L3626-L3631)。
- DPI 感知在创建窗口之前设置：`desktop.py:59` 调 `enable_dpi_awareness()`，
  降级链 PER_MONITOR_AWARE_V2 → shcore(2) → SetProcessDPIAware
  ——[desktop.py#L59](file:///C:/Users/26346/Desktop/project/dafors/desktop.py#L59)，
  [screen.py#L44-L86](file:///C:/Users/26346/Desktop/project/dafors/core/screen.py#L44-L86)；
  `Screen.__init__` 也有兜底调用——[screen.py#L243](file:///C:/Users/26346/Desktop/project/dafors/core/screen.py#L243)。
- 三档等价测试：同一归一化框在 1920×1080@125%、2304×1296@150%、3072×1728@200%
  三种物理尺寸下，`regions.pixel_rect()` 与 `Screen.norm_to_px()` 逐位相等
  ——[test_regions_api.py#L265-L281](file:///C:/Users/26346/Desktop/project/dafors/tests/test_regions_api.py#L265-L281)。

### 3.4 老配置兼容

`Config._normalize()` 在没有 `regions` 字段时整段补默认
——[config.py#L743-L747](file:///C:/Users/26346/Desktop/project/dafors/core/config.py#L743-L747)；
`describe()` 把 `list/clock/bar` 标成 `default`、其余标 `unset`，不抛异常
——用例 3.1 的 `test_old_config_without_regions_uses_defaults`。

### 3.5 全量 pytest

`python -m pytest tests -q`：

```
1 failed, 1931 passed, 134 skipped, 2 xfailed, 5 warnings in 447.50s
```

唯一失败是 `tests/test_ocr_price.py::test_local_models_are_preferred_and_cold_start_is_small`
（`rapidocr` 后端 `available() == False`），属于已知的"rapidocr 未安装"类；
另有缺截图素材导致的 skip，均与本次改动无关。新增的 27 条全过，前端静态检查
`tests/test_frontend_static.py` 19 条全过。

---

## 4. 本轮改动文件

| 文件 | 改动 |
|---|---|
| `core/regions.py` | 新增：区域规格表、校验、像素换算、屏幕描述 |
| `core/server.py` | `GET /api/regions`、`PUT /api/regions`、`GET /api/screen/capture` |
| `web/index.html` | 设置页「区域框选」整块（抓帧/拖拽/微调/恢复默认/保存），修 1554 行过期文案 |
| `tests/test_regions_api.py` | 新增 27 条契约测试 |
| `说明.txt` | 第四节改成界面框选流程 |
| `tools/calibrate.py` | 去掉"前端框选入口还没做" |
| `_fe_verify.py` | 把"没有框选页"断言改成"有框选入口" |

未 commit、未 push、未开可见窗口、未移动真实光标。抓帧只在用户点按钮后发生，
测试全部用假屏幕。
