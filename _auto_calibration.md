# 区域标定改为内置：实现、独立复核修复与验收

目标：买家不再手拖 9 个框。坐标由程序内置（1080p 客户区基准），运行时按
**游戏客户区**换算，开抢前自动做锚点自检；手动拖框只保留在「高级设置 →
区域微调」里，默认收起。手拖代码一行未删。

## 0A. 第二轮独立复核（引擎点击口径）修复对照

复核指出上一轮的「三档误差归零」只覆盖 16:9、且工具量的是「报出偏移」，不是
「引擎真正点的坐标」。本轮按 **引擎实际点击坐标 vs 真实位置模型** 重做验收。

⚠ **前提（必须和数字一起读）**：这里的「真实位置」不是真机实测，而是
`core/truth_model.py` 的默认模型——假设游戏 UI 以 1920×1080 为基准、客户区
不是 16:9 时**等比缩放并居中**（黑边），`s = min(宽/1920, 高/1080)`。
所以下表能说明的只有：**在该模型成立的前提下**，引擎点击相对模型预测位置的
偏差 ≤0.5px；它不能说明「相对真机按钮位置的偏差」。模型若错（边角锚定、
按宽度独立拉伸、UI 独立缩放档），0.5px 就是自证。证伪/替换见 §0A-补1。

| 客户区 | 位置来源 | 状态 | 闸门 | 购买按钮偏差 | 确认按钮偏差 |
|---|---|---|---|---|---|
| 1280×720 | model | ok | 放行 | −0.0 / +0.0 | −0.0 / +0.0 |
| 1280×800 | model | adjusted | 放行 | +0.2 / +0.2 | −0.0 / +0.2 |
| 1366×768 | model | ok | 放行 | +0.2 / +0.0 | +0.1 / +0.0 |
| 1600×900 | model | ok | 放行 | −0.0 / +0.0 | −0.0 / +0.0 |
| 1920×1080 | model | ok | 放行 | −0.0 / +0.0 | −0.0 / +0.0 |
| 1920×1200@150%（物理 2880×1800） | model | adjusted | 放行 | −0.0 / −0.5 | −0.2 / −0.0 |
| 2560×1440 | model | ok | 放行 | −0.0 / +0.0 | −0.0 / +0.0 |
| 3440×1440 | model | adjusted | 放行 | −0.2 / −0.2 | +0.0 / −0.2 |
| 3840×2160 | model | ok | 放行 | −0.0 / +0.0 | −0.0 / +0.0 |

最大偏差 0.5px（相对上述模型，不是真机）。复现：
`python tools/verify_click_truth.py`（表头会打印同一句前提）。

### 0A-补1 真实位置是模型、不是实测（已修）

- 前提文案写进三处：`tools/verify_click_truth.py` 模块 docstring、
  运行时表头、本文件 §0A 与 §7。
- 缩放模型抽成可替换接口 `core/truth_model.py`：

      truth_scale(client_w, client_h, anchors=None) -> float
      truth_boxes(size, table=None, anchors=None) -> (boxes, meta)

  默认 `min(宽/1920, 高/1080)`；传入真机锚点表即覆盖，`meta.source` 会
  从 `model` 变成锚点里的来源，`meta.measured` 变 True。只做客户区尺寸
  精确匹配，没测过的尺寸不插值、不猜。
- 锚点表格式：

      {"anchors": [
         {"client": [3440, 1440], "scale": 1.3333,
          "dx": 0, "dy": 0, "source": "真机 2026-11-08 人工标注"}
      ]}

  用量：`python tools/verify_click_truth.py --anchors measured.json`。
- 新增真机比对工具 `tools/verify_truth_vs_annotations.py`：吃清单
  `{"items":[{"image","button","box":[x,y,w,h],"client","workflow"}]}`，
  同时出「模型预测 vs 人工标注」「引擎点击 vs 人工标注」两张偏差表。
  `box` 必须是人工在真实截图上量的，不是工具算的。
  **目前没有真机标注清单，未跑过任何真实截图比对**——脚本无 `--manifest`
  时只打印 schema 并以 2 退出，不会拿构造数据冒充验证。

### 0A-补2 `apply_auto` 直接写配置：备份 / 读回 / 回滚（已修）

`apply_auto=True` 会把标定结果直接写进 `regions`，所以每次写入都必须可退：

- **写前备份**：`core/region_backup.py` 把整段 `regions` +
  `calibration.applied` 快照到用户数据目录
  （Windows `%LOCALAPPDATA%\dafors\region_backups\`，可用
  `DAFORS_USER_DIR` 覆盖），文件名带毫秒时间戳，保留最近 30 份；
  **不写仓库**。备份失败 → 本次不写坐标、闸门拦。
- **写后读回**：重新 `Config.load()` 核对每个写入区域是否等于提案值；
  不一致或写盘异常 → 按快照整体回滚，`calibration.block` 记原因，本次不启动。
- **一键回滚**：`python tools/rollback_regions.py --list` 列快照；
  不带参数回滚最近一次；`--at <时间戳>` 指定；`--config` 指定配置文件。
- 实测（`tests/test_calib_backup.py`）：写入失败 → 内存与磁盘都回到写入前；
  写后被外部改动 → 读回不一致触发整体回滚，9 个区域与 `applied` 全量恢复。

### P0-A `_scale_candidates` 无穷举兜底（已修）

- `vision._scale_candidates()` 现在把**信箱比例** `min(宽比, 高比)` 放在
  第一位；3440×1440 直接得到 1.3333，不再只有 1.5456/1.0。
- `match_scaled(..., deep=True)`：候选全部低于 0.80 时再穷举
  `DEEP_SCALES`（1.00–2.00 步进 0.05，外加 4:3 / 16:10 / 21:9 的
  1.3333 / 1.6 / 1.7778 / 2.3333 等）。自动标定走 deep，实时匹配仍走快路径。
- 实测：3440×1440 修复前 `skipped`（buy 最高分 0.522），修复后 `adjusted`，
  购买按钮偏差 −0.2/−0.2px。

### P0-B `skipped` 假绿灯（已修）

`gate_reason()` 里 `skipped` 与 `error` 同权。画面里没锚点 → 不核对就点，
1280×800 偏 29px、3440×1440 偏 307px，所以默认拦。**唯一放行**：本路线必需
的区域都已被用户手框（`_manual_ready()`），此时用配置坐标是用户自己定的。
`preflight` 同步：`skipped` 且非手框 → 红；手框 → 绿 + warn。
实测：无任何锚点的 3440×1440 → `skipped` + 拦，不进入点击流程。

### P0-C 闸门校验对象错（已修）

选「提案真正应用」：`calibration.apply_auto` 默认改为 **True**，标定结果直接
写进 `regions`，闸门用 `record()` 算出的 `gate` 校验**引擎会点的那套配置
坐标**，不再有两套坐标并存。`record()` 还会调 `_coords_mismatch()` 兜底：
必需区域的配置值若既不是本次结果、又不是手框，判不一致并拦。
`apply=False` 仍保留（只留提议），此时界面明示「提案已生成，未应用」。
手框区域永不覆盖（`apply_skipped_manual`）。

### P0-D 倒计时模式被 buy 锚点拦死（已修）

`REQUIRED_BY_WORKFLOW`：`countdown → (confirm,)`，`list/detail →
(buy, confirm)`。缺非必需锚点只降级（`adjusted`，文案写「本路线不要求
购买按钮，只用确认按钮校验」），不拦。
实测：1280×800 无 buy 模板 + countdown → `adjusted` + 放行；
同画面 + detail → `fallback`（购买按钮缺失）。

### P1-E 文案与实际状态不一致（已修）

`paintCalib()` 按 `applied_now / apply_auto` 区分「已微调并应用」与
「提案已生成，未应用，点击仍用当前配置坐标」；`CAL_TEXT.skipped` 改成
「按钮位置未经校对」，不再暗示沿用内置坐标是安全的。

### P1-F 手框后仍被拦（已修）

跨屏/遮挡仍是 `error`（不拿显示器矩形冒充），但手框必需区域后
`_manual_ready()` 为真，`gate` 为空 → 可启动；下一次自动标定不会覆盖手框。
用例：`test_manual_regions_lift_the_gate_for_skipped`（只框 buy 仍拦，
buy+confirm 都框才放行）。

### P2 边界

维护上一轮的结论：窗口跨屏/部分出屏（`client_partial`）→ `error` 拒绝标定，
不裁剪、不回落显示器矩形；手框一次即可启动。

## 0. 第一轮独立复核问题修复对照

### P0-1 模板中心用未缩放尺寸（已修）

`vision.match()` 内部按目标比例重采样模板，旧代码仍用原图 `th,tw` 求中心，
偏差恒为 `tpl_w*(1-r)/2`，并被当成真实偏差写进 9 个区域。现在
`_match_template()` 用 `vision.scaled(tpl, scale).shape` 求中心与命中框。

匹配几何误差实测（画布上把模板贴到内置坐标正中心，再走完整匹配链路）：

| 客户区 | 旧报出偏移 | 旧点击中心误差 | 新报出偏移 | 新点击中心误差 | 状态 |
|---|---|---|---|---|---|
| 1280×720 | +52.0 / +11.5 | +52 / +12 px | +0.0 / +0.0 | +0 / +0 px | ok |
| 1600×900 | +26.5 / +6.0 | +26 / +6 px | +0.0 / +0.0 | +0 / +0 px | ok |
| 1920×1080 | +0.5 / +0.0 | +0 / +0 px | +0.5 / +0.0 | +0 / +0 px | ok |
| 2560×1440 | −50.5 / −11.0 | −50 / −11 px | +0.0 / +0.0 | +0 / +0 px | ok |
| 2880×1620 | −75.5 / −16.0 | −76 / −16 px | +0.0 / +0.5 | +0 / +0 px | ok |
| 3840×2160 | −151.5 / −33.5 | −152 / −34 px | +0.0 / +0.0 | +0 / +0 px | ok |

复现：`python tools/verify_calib_bias.py`。回归用例
`test_template_matching_has_no_scale_bias` 覆盖 1280/2560/3840 三档。

**口径区分**：`tools/verify_autocalib.py` 的 12 组表只量「归一化值 × 客户区」
的取整误差（≤0.50 px），**不经过模板匹配**；匹配几何误差是上表，两者不能
混为一谈。

### P0-2 缺关键锚点不得判 ok（已修）

`buy / confirm` 为关键锚点。规则：

- 任一缺失且另一个也未找到 → `skipped`，消息列出缺哪个和最高分；
- 一个缺失、另一个在位 → `fallback`，回内置坐标；
- 只有 OCR 文字、没有任何像素锚点 → `skipped`，不得 `ok`；
- 两个都在但偏移方向矛盾 → `fallback`。

回归用例：`test_missing_critical_anchor_forces_fallback_to_builtin`、
`test_ocr_text_only_is_skipped_not_ok`。

### P0-3 用户手框优先（已修）

`_write_boxes()` 写之前逐个判断：**既不是内置值、又不是上一次自动写入值的
区域，视为手框，永远跳过**（`is_manual()`），`calibration.auto=True` 也压
不过手框。`describe()` 增加 `manual` 标记与 `source=auto`。

第二轮复核后口径调整为「默认直接落盘」（`apply_auto=True`），理由见
§0A P0-C：不落盘会形成「闸门校验提案、引擎点配置」的两套坐标。

回归用例：`test_record_applies_by_default_and_never_overwrites_manual_regions`。

### P1 状态语义与闸门（已修）

1. `skipped`（没锚点）与 `error`（抓屏失败/窗口越界）拆开；第二轮复核后
   两者**同权**都拦（`skipped` 不是绿灯），唯一放行是必需区域已手框
   （见 §0A P0-B）。
2. 闸门改为**当次即拦**：`_start_locked` 拿到 `fallback/error` 直接返回失败，
   不再只写两条 warn 继续点。
3. `adjusted` 不再一律放行：闸门统一走 `autocalib.gate_reason()`，
   微调量超过 `MAX_ADJUST_REF=24px`（按客户区宽度等比放大）即拦。
   `calibrate_image` 里超限本身会降级成 `fallback`，闸门这层是第二道；
   旧的 +53/−152px 污染坐标即使留在老配置里，`preflight` 也会按
   `calibration.block` 拦下，不再显示成「绿 + 警告」。
4. `fallback` 一律回内置坐标，不再「+40px 平移 + 5% 内缩」。
5. 每条消息都带数字：`购买按钮 +14.0/+2.0 px`、`购买按钮 没找到（最高分
   0.512）`、`整体偏移 +52.0/+11.5 px 超出可信范围（上限 24.0 px）`。
   界面 `#calibDetail` 渲染每个锚点的偏移/缺失状态，`calibration.anchors`
   也随状态落盘。

### P2 窗口跨屏/部分出屏（已修）

`Screen` 新增 `client_rect` 与 `client_partial`；客户区只要有一部分在屏幕外，
`run_for_engine` 直接返回 `error`，消息带客户区与显示器坐标，并提示把窗口
移回屏幕内或进「区域微调」。**不裁剪、不拿显示器矩形冒充客户区**，避免
+46px 这类偏移。回归用例：`test_partial_offscreen_window_refuses_instead_of_guessing`。

### 默认值

`calibration.auto` 保持 `True`，`calibration.apply_auto` 本轮改为 `True`
（见 §0A P0-C）。依据：9 档引擎点击坐标偏差最大 0.5 px；手框区域
`apply_skipped_manual` 有回归保护；`skipped/fallback/error` 全部当次拦截；
前端无头验收 51/51。若要回到「只提议不落盘」，把 `apply_auto` 设为
`False`，此时界面会明示「提案已生成，未应用」。若要关掉自动标定，把
`auto` 设为 `False`——闸门仍要求必需区域被手框过，未手框会拦。

## 1. 内置坐标表

唯一来源 [core/region_table.py](file:///C:/Users/26346/Desktop/project/dafors/core/region_table.py)：
`BUILTIN_PX` 是 1080p 客户区像素框，`norm_box()` 转 0..1（6 位小数）得到
`BUILTIN`；配置默认值、界面、自动标定、验证工具全部读这张表。

| 区域 | 1080p 像素 (x,y,w,h) | 来源 |
|---|---|---|
| `buy` 购买按钮 | 1482, 903, 298, 58 | N_BAR 实测 |
| `confirm` 确认按钮 | 1164, 690, 303, 70 | `templates/confirm.png` 303×70 + N_CLOCK 几何推得 |
| `list` 在售列表 | 60, 210, 1340, 730 | N_LIST 实测 |
| `clock` 公示倒计时 | 1100, 600, 300, 80 | N_CLOCK 文字区外扩 |
| `bar` 详情价格 | 1593, 915, 110, 33 | `u3_detail_price` ROI |
| `name` 商品名称 | 600, 232, 380, 34 | 列表首卡名称行 |
| `price` 价格文字 | 620, 394, 120, 30 | `u3_card_r1_l` ROI |
| `list_item` 列表购买项 | 600, 220, 390, 250 | 列表首卡整体 |
| `toast` 顶部提示 | 520, 62, 880, 36 | N_TOAST 实测 |

老配置逐键迁移，非法值回落内置；`Config.region()` 对缺失键也返回内置副本。

## 2. 按实际客户区换算

- `find_game_client_rect()`：`GetClientRect` + `ClientToScreen`，客户区绝对
  矩形（物理像素），不是整屏、不是显示器分辨率。
- `Screen.frame` 只在客户区完整落在本显示器内时启用；`base_rect` 优先客户区。
  部分越界时记录 `client_partial=True`，由自动标定明确拒绝。
- 归一化→像素仍只有 `Screen.norm_to_px()` 一个入口。
- 鼠标原点同步用 `base_rect`，窗口化/无边框不会多算标题栏偏移。

## 3. 坐标算术误差（12 组预设）

`python tools/verify_autocalib.py`：偏差 = 换算像素 − 归一化值 × 客户区。

| 预设 | 客户区 | 最大偏差 |
|---|---|---|
| 1080p@100% | 1920×1080 | 0.00 |
| 1080p@125% | 2400×1350 | 0.50 |
| 1080p@150% | 2880×1620 | 0.50 |
| 1080p@200% | 3840×2160 | 0.00 |
| 1440p@100% | 2560×1440 | 0.33 |
| 1440p@125% | 3200×1800 | 0.33 |
| 1440p@150% | 3840×2160 | 0.00 |
| 1440p@200% | 5120×2880 | 0.34 |
| 2160p@100% | 3840×2160 | 0.00 |
| 2160p@125% | 4800×2700 | 0.50 |
| 2160p@150% | 5760×3240 | 0.00 |
| 2160p@200% | 7680×4320 | 0.00 |

1080p/100% 下 `boxes_for((1920,1080))` 与 `BUILTIN_PX` 逐位一致（9/9）。

## 4. 5 张真实截图基准

真值来自 `tests/fixtures/price_roi/manifest.json`（人工标注 ROI）：

- `u3_card_r1_l` 与内置 `price` 中心偏差 `[0.0, 0.0] px`；`u4/u5_card_r1_l`
  为 `[-2.0, -1.0] px`。
- `u3_detail_price` 完整落在 `bar` 内（`1593,915,110,33` 完全吻合）。
- 其余左列/右列价格全部落在 `list`（`60,210,1340,730`）内；顶部筛选区间
  不属于 9 个区域，只校验在画面内。

15/15 通过。

## 5. 自动标定四态与入口

| 状态 | 含义 | 坐标 | 闸门/开抢 |
|---|---|---|---|
| `ok` | 双锚点在位且偏移 ≤6px | 内置（写回，手框除外） | 放行 |
| `adjusted` | 偏移在 6–24px（等比）内，微调后复验通过 | 写进 `regions`（手框除外） | 放行 |
| `skipped` | 没有任何可校验锚点 / 只有 OCR 文字 | 不写 | **拦**；必需区域都手框后放行 |
| `fallback` | 缺关键锚点、偏移超上限、复验失败、锚点矛盾 | 内置（恢复默认） | **拦**；手框后放行 |
| `error` | 抓屏失败、窗口部分出屏、读图失败 | 不写 | **拦**；手框后放行 |

入口：开抢前 `engine._start_locked`；界面「一键自动标定」`POST /api/calibrate`；
启动闸门 `preflight` 第 ④b 项；`calibrate_image()` 纯函数可离线跑。

## 6. 引导流程与文案

- 四步第一步：「自动标定（一键，几秒）」，说明「9 个区域的坐标已经内置……
  你不用拖框」。
- 「高级设置 → 区域微调」默认收起、带「一般不需要改」，原抓画面/拖框/保存
  逻辑一行未删。
- 全页无「去框选」「没有框选页」「区域框选」字样。
- 标定结果默认**直接写入 `regions`**（`apply_auto=True`，写前备份、写后读回）；
  只有关掉 `apply_auto` 才出现「应用标定提议（N 个区域）」按钮，手框区域会
  明确提示「保持原样」。
- `#calibDetail` 展示每个锚点的偏移/缺失，不再只显示「未通过」。

## 7. 验证记录

| 项 | 结果 |
|---|---|
| `tests/test_autocalib.py` | 19 passed |
| `tests/test_calib_backup.py` | 7 passed（备份 / 写失败回滚 / 外部改动回滚 / restore / truth_model 3 条） |
| `tests/test_regions_api.py` | 28 passed |
| `tests/test_preflight.py` | 23 passed（含 error/skipped/adjusted 超阈值分支） |
| 标定 + 状态两文件 | 42 passed |
| 标定 + 状态 + 区域接口 | 70 passed |
| `tests/test_fix_d.py` + `tests/test_state_hardening.py` | 45 passed（含 3 条 dict/Config 回归） |
| 引擎/服务/配置/屏幕/前端静态 12 个文件 | 141 passed / 10 skipped |
| 前端无头验收 `_fe_verify.py` | 51/51 |
| 匹配几何误差 | 6 档 16:9 实测 0 px（见 §0） |
| 引擎点击坐标误差 | 9 档相对**模型**最大 0.5 px；模型不是真机实测（见 §0A） |
| 备份 / 回滚 CLI | 备份在用户目录、回滚后 9 区域 + applied 全部一致（见 §0A-补2） |

**类型回归（dict vs Config）**：`run_for_engine(cfg)` 早先假设 `cfg` 一定是
`Config`（有 `.data`），但 `Engine.start()` 的测试替身是 `dict` 子类；当它没有
`calibration` 节时走进 `record()` 的 `cfg.data[...]`，抛
`'dict' object has no attribute 'data'`（`test_fix_d` 1 条、`test_state_hardening`
2 条）。修法是在**调用点**显式区分类型：不是 Config（没有 `.data`）的映射视为
「旧接口 / 自动标定不适用」，返回 `skipped + gate=""` 并放行；**真 Config 的
空节/坏节仍按「自动标定关闭」走闸门**，不能靠清空配置绕过 P0-B。没有给 dict
补 `.data`，也没有把 dict 强转成 Config。

全量 `pytest tests` 修复前为 `10 failed, 2166 passed, 136 skipped, 2 xfailed`
（含 3 条类型回归；其余 7 条为 `rapidocr` 未安装 / OCR 素材 / 高负载计时）；
修复后复跑 **`3 failed, 2182 passed, 136 skipped, 2 xfailed`（10 分 29 秒）**，
3 条全部是 `test_ocr_panel.py` / `test_ocr_price.py` 的
`ModuleNotFoundError: No module named 'rapidocr'`（本机未装该可选依赖），
与本次改动无关；3 条 dict/Config 回归全绿，无新增红。

## 8. 边界

- 未 commit、未 push。
- 验证无可见游戏窗口、无真实鼠标动作。
- 手动拖框逻辑完整保留，只是不再是主路径。
- 未改已通过项：客户区/窗口换算、副屏负坐标、150% 无 DPI 二次乘算、
  无边框全屏、高级设置默认收起。

## 9. 复现命令

```bat
cd C:\Users\26346\Desktop\project\dafors
python tools\verify_calib_bias.py
python tools\verify_autocalib.py
python -m pytest tests\test_autocalib.py tests\test_preflight.py tests\test_regions_api.py -q
python _fe_verify.py          :: 需先起服务 python -c "from core.server import serve; serve(port=8899)"
```
