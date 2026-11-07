# R06 识别与坐标几何专项审查

- 审查对象：`C:\Users\26346\Desktop\project\dafors`（branch `main`，工作树有大量未提交修改）
- 审查范围：**只做识别（OCR/字模/价格解析）与坐标几何（归一化↔像素↔绝对屏幕）**，不涉及并发、状态机、网络、UI
- 方式：只读源码 + 仓库外脚本（`C:\Users\26346\AppData\Local\Temp\r06_vision\`）批量跑真实截图与对抗样本；**未改一行业务代码，未 commit，未 push，未开窗，未动真实光标**
- 结论口径：本机 `python 3.x + Win11`，主屏 1920×1080 @100%，进程初始 DPI awareness=0，`Screen()` 构造后=2（per-monitor）
- **重要前提（影响所有 OCR 实测）**：本机 `ocr.available() == False`（RapidOCR 与 PP-OCRv6 服务都不可用）。因此 `read_price` 的所有实测数字都来自**第三级「自建字模」兜底**，不是装了 RapidOCR 的完整三级链。这一条本身也是缺陷之一（见 R06-09）。

## 文件状态标注（按用户要求：修改态不当 bug 计）

| 状态 | 文件 |
|---|---|
| `M` 修改态（结论仅供该分支参考，**不计入稳定 bug 统计**） | `core/screen.py`、`core/engine.py`、`core/ocr_chain.py`、`core/market.py`、`core/preflight.py`、`core/config.py` |
| 未修改（结论可直接当 bug） | `core/nav_ons.py`、`core/nav_coords.py`、`core/cards.py`、`core/digit_ocr.py`、`core/glyphs.py`、`core/ocr.py` |
| 测试 | `tests/test_ocr_price.py`（M） |

> 说明：`core/nav_ons.py`、`core/cards.py`、`core/digit_ocr.py`、`core/glyphs.py`、`core/nav_coords.py` 在 `git status` 里**没有** `M`，下列相关条目是确定 bug。

## 结论摘要（按严重度）

| # | 严重度 | 问题 | 位置 | 状态 |
|---|---|---|---|---|
| R06-01 | 严重 | 字模兜底把「漏读的残缺数字」当有效价格：真实 1,000,000 → **1**（score 0.669 过闸） | `digit_ocr.py:284-289,494-496,568` | 未修改 ✅bug |
| R06-02 | 严重 | `_balance` 取「最后一个数字」，右侧货币 OCR 失败时静默回退到别的货币；且 `"0"`/`"2,537K"` 都被当有效值 | `engine.py:1799-1814,2296-2313` + `ocr.py:324-335` | 修改态 |
| R06-03 | 高 | 底带 `digits_only` 把磨损小数与价格拼成一个数：`0.120 9,500` → **1209500** | `market.py:411-433` | 修改态 |
| R06-04 | 高 | 成色带磨损丢小数点后被当价格：`成色 S 01209` → **1209**；`14,0002` → **140002** | `cards.py:38-79,510-516` | 未修改 ✅bug |
| R06-05 | 高 | `price_from_text` 取最后一个 token：`14,000 2` → **2**；`9 999` → **999**；9 位上限与注释不符 | `ocr_chain.py:99-124` | 修改态 |
| R06-06 | 高 | `read_price` 没有完整性校验，截半图被当有效读数（`150,000`→150，接受率 40%/53%） | `ocr_chain.py:259-294` | 修改态 |
| R06-07 | 高 | 面板滚轮用屏幕相对坐标直接 `SetCursorPos`，次屏偏移 ±1920px，且恒 `return True` | `nav_ons.py:361-363` | 未修改 ✅bug |
| R06-08 | 高 | `CONTINUE_POINT` 与 `ONS_GRID_ROW_H` 不随分辨率缩放，1440p 起行号/点击点全错 | `nav_coords.py:162,312`、`nav_ons.py:795`、`engine.py:2231` | 未修改 ✅bug |
| R06-09 | 中 | OCR 引擎全不可用时静默降级到字模，无状态上报，上层仍按「正常识别」跑 | `ocr_chain.py:169-177,288-294` | 修改态 |
| R06-10 | 中 | 字模只在 1080p 有效：0.833/1.333/1.5/2.0 缩放后 14/14 全拒识；纵向 +6px 抖动也全拒 | `glyphs.py:47-48,92-118` | 未修改 ✅bug |
| R06-11 | 中 | `grab` 不裁剪越界 → 99% 黑图；`_pick_monitor(want=0)` 返回虚拟屏（多屏含负偏移） | `screen.py:60-124` | 修改态 |
| R06-12 | 中 | 型号以数字结尾时价格整行丢失：`M250 15,000` → price None | `market.py:52,60-89,146-168` | 修改态 |
| R06-13 | 低 | 两套坐标换算实现并存（`_scaled` vs `norm_to_px`），当前同为 round，实测 Δ=0，仅右边缘 1px | `nav_coords.py:247-254` / `screen.py:126-148` | 混合 |
| R06-14 | 低 | `SetProcessDpiAwareness` 的 HRESULT 未校验，GUI 先建窗时静默保持旧 DPI 模式 | `screen.py:15-20` | 修改态 |

---

## 一、识别：能蒙混通过的路径

### R06-01 严重｜字模兜底把「没读全」当读数（未修改文件，确认 bug）

- 位置：`core/digit_ocr.py:284-289`（`cw > 1.05*ch` 投影切分、`ch > 0.98*h` 丢弃）、`494-496`（`_drop_icons`：`w >= 0.85*h` 直接丢）、`568`（`if score < min_conf: return None`），阈值 `33`（`DEFAULT_MIN_CONF = 0.55`）
- 对抗样本：`tests/fixtures/price_roi/u3_card_r1_l.png` —— **这甚至不是对抗样本，是基准图**
- 实际输出 / 正确值：

```
u3_card_r1_l  want=1,000,000  got=1         (score 0.669 ≥ 0.55，判为有效)
u3_card_r2_l  cut_left_50   want=150,000   got=150    (score 0.914，判为有效)
u3_card_r3_l  cut_right_50  want=469,999   got=999    (判为有效)
```

- 机理：`_drop_icons` 用「宽≥0.85×高」判图标，粘连的两个数字整体比高更宽，于是**整段被当图标丢弃**，剩下的单字仍然过 `min_conf` 闸门；`read_detail` 只返回拼出来的数，不校验「位数是否与图宽匹配」，也没有「本图应有两个以上字形」的下限。
- 影响：价格被低读 1000~1,000,000 倍后，仍以合法 int 返回，`pick()`/预算闸门/`_price_from_block` 全部当真实价格使用。

### R06-02 严重｜余额 = 「最后一个数字」，右侧货币读不出就回退（修改态，供分支参考）

- 位置：`core/engine.py:1799-1814`（`_balance` → `ocr.last_number`）、`2296-2313`（成交判定与误买止损）、`core/ocr.py:324-335`（`last_number` = `price_from_text`）、`core/ocr_chain.py:99-103`（取 `matches[-1]`）
- 设计假设：右上角三组货币「左=纸币、中=青铜币、右=三角币」，取最后一个数。实测放大图确认三组图标外观排列正确，**但代码不校验「读到的就是最右那组」**。
- 真实截图实测（`_backup_20261105\_shots_legacy\`）：

| 截图 | OCR 原文 | `_balance` 返回 | 正确值 |
|---|---|---|---|
| `drv_asval_onsale.png` | `2,049 0+ 22+` | `22` | 22 ✅ |
| `n_04_applied.png` | `2,049 0+ 26 +` | `26` | 26 ✅ |
| `_probe_02_market.png` | `2,537K 2,075 + à` | **`2075`** | 第三组三角币（OCR 成了 `à`）→ 应 `None` |
| `p_filter.png` / `dbg_list.png` / `now8.png` | 空文本 | `None` | 静默跳过校验 |

- 后果链：
  1. `2297` `got < before` —— 用**前两张截图的不同货币**比较，或某页最右货币读成 `0` 时，`0 < before` 恒真 → **误判成交**；
  2. `2311-2313` 误买止损 `(before-after) > price*1.2` —— 用错货币，止损可能永不触发或直接误停；
  3. `price_from_text("0") == 0`（`MIN_DIGITS=1`，单字符 0 不触发前导零拒绝）→ 余额读成 `0` 就是合法读数；
  4. `"2,537K"` 的 `K` 后缀无人处理（`_PRICE_TOKEN = \d+(?:,\d+)*` 只吃到 `2,537`，后一个字符不是 `,` 就不管）→ `2,537K` → 2537，静默 2537 倍假余额；
  5. `before/after` 任一为 `None` 时整段 `if` 跳过（`2297`、`2311`）→ 没有弹窗的成交路径上，**校验被静默关闭**。
- 该假设已被测试固化：`tests/test_ocr_price.py:94` `("2,049 0+ 0 40+", 40)`。

### R06-03 高｜底带先把所有数字拼起来再转 int（修改态）

- 位置：`core/market.py:411-433`（`_price_from_block`：`digits_only(text)`）、`350-373`（`_scan_cards` 缺价时用 `bottom["price"]` 整条原文重试）
- 实测（`t1_price_text.py`）：

```
"极品 成色 S 0.120 9,500"  want=9500   got=1209500   ← 磨损 0.120 被拼进价格
"无 0.120 9,500"           want=9500   got=1209500
"极品 成色 S 0.1209 14,000" want=14000  got=None      ← >8 位被上限挡住（安全侧）
```

- 机理：`digits_only` 剥掉分隔符后**顺序拼接**所有数字段，`0120` + `9500` = `01209500` → `1209500`；`≤8 位` 限制是最后一道闸，但拼出来的 8 位内数值完全合法。
- 影响：`_price_from_block` 是卡片缺价时的兜底，一触发就会得到 100 倍以上的假价格。

### R06-04 高｜成色带磨损丢小数点被当价格（未修改文件，确认 bug）

- 位置：`core/cards.py:54-59`（整条带是纯数字时只要求 `int>=1` 就返回原文）、`510-516`（`read_card` 遇「成色」先取价再 `break`，不再看真正价格带）、`_MIN_PRICE = 100`（`35`）
- 实测：

```
cards._price_token_in("成色 S 01209") → "01209"   随后 int → 1209（应拒识：0.1209 磨损）
cards._price_token_in("14,0002")      → "14,0002" 随后 digits_only → 140002（价格+数量粘连）
cards._price_token_in("成色 S 05")    → ""         （这条按设计拒绝）
cards._price_token_in("05")          → "05"        （两条挂单，按设计接受）
```

- 「成色」路径并没有真正堵死：它只在**同一条带里同时出现 3 位以上整数**时才会拿走来之不易的价格，而 `01209` 正好满足 `len>=3` 且 `>=100`；`break` 之后真正价格带不会被读。

### R06-05 高｜`price_from_text` 取最后一段数字（修改态）

- 位置：`core/ocr_chain.py:99-124`
- 实测（`t1_price_text.py`，当前工作树）：

```
"14,000 2"      want=14000  got=2          ← 价格 + 数量，取到数量
"2 14,000"      want=14000  got=14000      ← 反过来就正常，说明纯顺序问题
"9 999"         want=None   got=999        ← 空格千分位未合并
"100,000,000"   want=None   got=100000000  ← MAX_DIGITS=9，注释写「上限 99,999,999」
"9.000"         want=9000   got=None       ← 句点千分位在 cards 认、在这里不认
"05"            want=5      got=None       ← cards 认两币挂单，这里不认
```

- 影响：`ocr.read_price` 的 tier1/tier2 文本闸门就是它，`_bar_price`（`engine.py:1198-1214`）直接吃这个返回值；「价格在前、数量在后」是交易行的常见排布，此路径会把数量当价格。

### R06-06 高｜读价没有「是否读全」的结构校验（修改态）

- 位置：`core/ocr_chain.py:259-294`（三级链）、`22`（`SCORE_MIN=0.5`）
- 对抗样本：`price_roi` 22 条 price 类夹具 × 9 变体（`t3_price_ocr.py`，共 198 次）。**本机 OCR 引擎不可用 → 全部走字模兜底**：

```
base           OK=8/15  误识=1  拒识=6   （唯一误识就是 u3_card_r1_l 1,000,000→1）
noise15        OK=8/15  误识=1  拒识=6
contrast_low   OK=9/15  误识=1  拒识=5
contrast_high  OK=6/15  误识=1  拒识=8
scale_0.75     OK=10/15 误识=0  拒识=5
scale_1.5      OK=7/15  误识=1  拒识=7
dilate_x       OK=0/15   误识=1  拒识=14  （数字粘连 → 只剩 1 条被接受）
cut_left_50    OK=0/15   误识=6  拒识=9   → 40% 的「半张图」被当有效读数
cut_right_50   OK=0/15   误识=8  拒识=7   → 53% 被当有效读数
```

- 关键对比：`cut_left_50` 的 `150,000 → 150`、`cut_right_50` 的 `150,000 → 1000`、`469,999 → 460/999`。半张图必然只能读到一半数字，但返回值和完整图没有任何区别，调用方无法区分。
- 影响：这是「识别失败返回值被上层当有效读数」的**最大入口**。ROI 若因坐标漂移少裁了 1/3 或晚了一帧（滚动中），就会得到一个小的合法 int。

### R06-09 中｜引擎缺失时静默降级（修改态）

- 位置：`core/ocr_chain.py:169-177`（`available()` 故意不把字模算作可用，但 `read_price` 仍然无条件走三级）、`288-294`
- 现状：本机 `ocr.available()==False`，`read_price` 仍然照常返回数字（只是精度掉到只有字模），**调用方拿不到「当前只有字模」这个事实**，`stats["tier3"]` 只在内部计数，不参与任何放行判断。
- 建议方向（不改代码，仅记录）：`available()==False` 时 `_bar_price` 应把结果标记为「低置信」，或至少在日志里每次写明 tier3。

### R06-12 中｜型号以数字结尾时价格整行丢失（修改态）

- 位置：`core/market.py:60-89`（`_squeeze_digits` 把数字之间的空格挤掉）、`52`（`_MODEL_TOKEN` 以「字母开头、含数字」整段摘除）、`146-168`
- 实测（`parse_row`）：

```
"M250 15,000"   → text "M25015,000" → price=None   （正确 15000）
"M7 15,000"     → text "M715,000"   → price=None   （正确 15000）
"QBZ95-1 突击步枪 15,000" → price=15000 ✅（因为 -1 后面是汉字，空格没被挤）
```

- 机理：`_DIGIT_GAP` 把「0 空格 1」挤成「01」，型号 token 于是变成 `M25015`，整段被摘掉，只剩 `,000` → `numbers=[0.0]`。
- 影响：安全侧（`None` 不买），但会整行漏掉——凡是名字以数字结尾的枪（M250、M7 等）全部读不出价。

---

## 二、坐标几何

### R06-07 高｜滚轮点漏了显示器原点（未修改文件，确认 bug）

- 位置：`core/nav_ons.py:361-363`

```python
u = ctypes.windll.user32
x, y = _pt(self, at or self.ONS_SCROLL_AT)   # 屏幕相对坐标（已按分辨率缩放）
u.SetCursorPos(int(x), int(y))               # ← 缺 self.screen.bounds["left"/"top"]
```

- 数学验证（`t2_geom.py` 第 5 节）：

```
monitor left=0     → 送 (1600,600)   应为 (1600,600)   偏差 (0,0)
monitor left=1920  → 送 (1600,600)   应为 (3520,600)   偏差 (1920,0)
monitor left=-1920 → 送 (1600,600)   应为 (-320,600)   偏差 (-1920,0)
```

- 对比：同文件的 `click`/`click_px` 都走 `Mouse`（`core/input.py` 内部加 `_origin_left/_origin_top`），只有这个「必须用真实鼠标事件」的特例手写了 `SetCursorPos`，绕过了补偿。
- 影响：`ons_scroll_top`/`ons_scroll` 在副屏上把光标放到别的显示器，滚轮事件落到别的窗口；函数末尾 `return True`，上层以为「已滚到顶」，后续网格分组基于没滚动的画面 → 行号/分组全错。

### R06-08 高｜基准像素常量直接用在真实像素上（未修改文件，确认 bug）

- 位置：`core/nav_coords.py:162`（`ONS_GRID_ROW_H = 56`）、`312`（`CONTINUE_POINT = (960, 1000)`）；使用处 `core/nav_ons.py:795`（`row = round((b["cy"] - base) / ONS_GRID_ROW_H)`）、`core/engine.py:2231`（`self._click(mouse, *CONT_CLICK)`）
- 数学验证（`t2_geom.py` 第 3/4 节，按等比缩放后的真实行距）：

```
屏高 1080：真实 56.0px  → row0=0 row1=1 row2=2 row3=3   ✅
屏高 1440：真实 74.7px  → row0=0 row1=1 row2=3 row3=4   ← row2 已经错位
屏高 2160：真实 112.0px → row0=0 row1=2 row2=4 row3=6

CONTINUE_POINT(960,1000)：
  1920×1080 应为 (960,1000)    偏差 (0,0)
  2560×1440 应为 (1280,1333)   偏差 (320,333)
  3840×2160 应为 (1920,2000)   偏差 (960,1000)
```

- 为什么只有这两处漏：同文件 `_ons_find_boxes`（`sin = _pt((22,0))[0]`）、`_ons_is_decoration`（`int(6*size/1920)`）都做了缩放 —— 说明作者知道规则，网格/常量两处是漏改。
- 影响：非 1080p 上勾选项被归到错误行号 → 选错稀有度/成色档位 → 筛出错误的货；奖励页「继续」点在屏幕中部（1440p 上偏 333px），可能点到展示图或无效区域，卡在奖励页。

### R06-10 中｜字模只在 1080p 有效（未修改文件，确认 bug）

- 位置：`core/glyphs.py:47-48`（`MIN_SCORE=0.88`/`MIN_GAP=0.10`）、`92-118`（`search_rect` 固定 `(cx+20, cy-13, 88, 26)`）、`WIN_W=80/WIN_H=18`
- 实测（`t5b_glyph_backup.py`，截图 `_shots\p_filter.png` 已被并行会话清理，改用 `_backup_20261105\_shots_legacy\p_filter.png`）：

```
k=0.833  正确=0  误认=0  拒识=14/14
k=1.000  正确=14 误认=0  拒识=0/14
k=1.333  正确=0  误认=0  拒识=14/14
k=1.500  正确=0  误认=0  拒识=14/14
k=2.000  正确=0  误认=0  拒识=14/14
```

- 中心抖动（dx∈{-6..6}, dy∈{-6,0,6}）：`dy=+6` 的 5 个点**全部**返回 `None`，`dy=0/-6` 全部正确 → 窗口在纵向是偏置的，容差不对称。
- 阈值本身没问题：面板区 2911 个扫描点只有 3 个通过（0.10%），且 3 个都落在真实文字位置（`贵金属`、`B`、`S`），不存在「随机噪声蒙混过关」。
- 影响：字模是三级兜底，非 1080p 下等于没有兜底；`dy>+6` 的轻微漂移（滚动半行、卡片行距差）也直接判无。属于「失效」而非「认错」，但会让 `_ons_assign_grid` 拿不到任何锚点。

### R06-11 中｜越界抓图不裁剪、虚拟屏误选（修改态）

- 位置：`core/screen.py:109-124`（`grab`：`left = bounds.left + x`，无 clamp、无裁剪）、`60-103`（`_pick_monitor`）
- 实测（`t2_geom.py` 第 7 节）：`grab((1920-60, 1080-60, 60, 60))` 这类越界框 → 返回 `(60,60,3)`，**黑像素占比 0.989**；不报错、不返回 None。
- 后果：任何把 ROI 算到屏幕边缘外 1px 的路径（价格条右移、`N_BALANCE` 右侧货币条、2160p 下 `norm_to_px` 取整后超界）都会得到「合法但全黑」的图，OCR 返回空，上层看到的是「读不到」而非「坐标错了」。
- 另：`_pick_monitor(want=0)` 走 `self._sct.monitors[0]`，多屏时是**虚拟桌面**（left/top 可能为负、尺寸含所有屏），而 `grab` 又按 `bounds.left/top` 加偏移 —— 这条组合没有测试覆盖（本机单屏，mss monitors 两项都是 0,0,1920,1080，无法区分）。

### R06-13 低｜两套换算实现并存（混合）

- 位置：`core/nav_coords.py:247-254`（`_scaled` 用 `base=(1920,1080)` + `round`）、`core/screen.py:126-148`（`norm_to_px` 用 `self.size` + `round`）
- 实测（`t2_geom.py` 第 1/2 节，1080p/1440p/900p/2160p/768p 全跑）：

```
所有 ROI 左上角 Δ=0；右边缘最大误差 1px（x+w 与 round(x+w) 各自取整）
```

- 结论：**当前没有二次缩放**，两处都已是 `round`，比历史版本好；但「归一化 ↔ 像素」仍有两份独立实现、两个不同的输入尺寸来源（`screen.size`=显示器 bounds，而 `_scaled` 用 `screen_w/screen_h`）。一旦有人改其中一处，就会重新分叉。属结构风险，不是现网 bug。

### R06-14 低｜DPI awareness 的 HRESULT 未校验（修改态）

- 位置：`core/screen.py:15-20`

```python
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    ...
```

- 实测（`t2_geom.py` 第 6 节）：进程启动时 `GetProcessDpiAwareness` = 0（unaware），`Screen()` 之后 = 2；本机 100% 缩放下 `GetSystemMetrics` 与 mss 一致（1920×1080）。
- 风险：若 `desktop.py`/pywebview 在 `Screen()` 之前创建了窗口，`SetProcessDpiAwareness` 返回 `E_ACCESSDENIED`（不是异常，是 HRESULT），代码静默继续 → 进程保持 DPI-unaware，`GetSystemMetrics` 返回 1536×864 之类的逻辑尺寸，与 mss 物理截图差 1.25 倍 → 所有 `norm_to_px` 与截图坐标错位。**本机无法复现**（见「无法验证」）。
- 另外：`screen.size` 取的是**显示器 bounds**，不是客户区；全仓 grep 无 `GetClientRect`（见 R06-15 的说明），窗口化/带边框运行时没有对齐手段。

### R06-15 说明项｜「截图像素 vs 屏幕坐标」错位一个标题栏

- 位置：`core/screen.py:105-107`（`size = bounds`）、`core/preflight.py:70-95`（只校验 `facts["size"]` 与 `WANT_SIZE`）、全仓无 `GetClientRect`/`ClientToScreen`
- 现状：`mss` 抓的是**显示器**，`Mouse` 也是**绝对屏幕**坐标，两者同源，所以**全屏独占/无边框全屏**下不会错位（本机测量：`mm 与 metrics 一致=True`）。
- 风险点：窗口化 + 有标题栏时，游戏画面左上角 = `window rect + 标题栏 + 边框`，而所有 ROI 仍按「显示器 0,0」算 → 整体上移一个标题栏高度（~30px），价格条/余额条全部裁到上一行。`preflight` 只比对显示器尺寸，抓不到这个状态（见「无法验证」）。

---

## 三、对抗样本清单（可复现）

脚本都在仓库外：`C:\Users\26346\AppData\Local\Temp\r06_vision\`

| 脚本 | 作用 | 本次输出要点 |
|---|---|---|
| `t1_price_text.py` | 纯文本价格解析 24 例 + `parse_row` 6 例 + `cards._price_token_in` 9 例 + `_price_from_block` 4 例 | 5 处 `price_from_text` 不符预期；`M250` 丢价；`01209`/`14,0002` 被接受；`1209500` |
| `t2_geom.py` | 5 种分辨率 × 6 个 ROI 的换算差、行号反推、CONTINUE 偏差、多屏滚轮点、DPI、越界抓图 | Δ=0/右边缘 1px；1440p row2→3；2160p row1→2；±1920px；黑图 98.9% |
| `t3_price_ocr.py` | 15 条真实价格 ROI × 9 变体（base/噪声/对比度/缩放/粘连/裁半） | 见 R06-06 表 |
| `t5b_glyph_backup.py` | 27 个字模自一致 + 2911 点假阳扫描 + 5 档缩放 + ±6px 抖动 | 14/14 基准正确；假阳 0.10%；非 1.0 缩放全拒 |
| `t6_market.py` | 整屏列表卡扫描 × 4 变体 | **本机 0 行**（OCR 不可用）→ 见「无法验证」 |

跑法：

```
cd C:\Users\26346\AppData\Local\Temp\r06_vision
python t1_price_text.py
python t2_geom.py
python t3_price_ocr.py
python t5b_glyph_backup.py
```

---

## 四、确认没问题的项（实测/代码验证）

1. **`_read_price` 的 3× 回映正确**：`core/nav_ons.py:511-513` 用 `_px_box` 取 ROI、`mid = (int(_pt(self,(1625,0))[0]) - x0) * 3`，左右半区按 `w["x"]` 排序拼接，`h>60` 与 `score<0.6` 过滤图标碎块 —— 与 `_crop_lines`（`/fx` 后加回 `(x0,y0)`）同一口径，数学上无二次缩放。
2. **`Mouse` 原点补偿正确**：`click`/`click_px` 走 `Mouse`，内部加 `bounds.left/top`；`_center`（`engine.py:2430-2432`）只做 `norm_to_px` + 取中心，不重复加偏移。
3. **`glyphs.identify` 的拒识闸门没有蒙混漏洞**：越界直接 `return None`；`MIN_SCORE=0.88` + `MIN_GAP=0.10` 在 2911 个扫描点里只有 0.10% 通过，且全部落在真实文字上；低分模板（颜色类 `max≈0.66-0.76`）一律 `None`。
4. **`norm_to_px` 的 NaN/Inf 守卫有效**：`screen.py:135-141` 对每个字段做 `float` + `isfinite`，不再出现 OverflowError/负尺寸。
5. **`_ons_is_decoration` / `_ons_find_boxes` 的缩放写法正确**：`int(6*size/1920)`、`sin = _pt((22,0))[0]` 是同一文件里的正确范式（R06-08 的两处是漏改）。
6. **`market.check` 对 `price=None` 拒买**；`_chip_ready` 读空即 `return False`；`_price_from_block` 的 `≤8 位` 上限挡住拼接后的超长串（副作用是 `None`，安全侧）。
7. **`_check_price_box`（`engine.py:1453-1482`）读回 `None` 时放行是可控的**：注释与行为一致，且后续 `market.check` 按行过滤；实测 `(None,None)` 不会产生越界购买（只会「没筛窄」）。仅在「行级过滤本身也读到 `None`」时才漏，属于 R06-03/06 的下游。
8. **`_pick_monitor` 主屏匹配**：`want=None/1` 时按 `size` 找主屏，不会误选虚拟屏；只有 `want=0` 才有 R06-11 的问题。
9. **`_ons_assign_grid` 的列分界不再写死 1520**（`col_split` 由调用方给），区块命名不再按顺序猜（`tops` 几何自证 + 前缀约束）。

## 五、无法验证的项（本机条件不具备）

1. **完整三级 OCR 链**（RapidOCR → PP-OCRv6 → 字模）：本机 `ocr.available()==False`，t3/t6 全部落在字模层。装了 RapidOCR 时 tier1/tier2 的误识/拒识率、`SCORE_MIN=0.5` 的实际作用，本次**没有数据**。
2. **`markets` 整屏卡片扫描的鲁棒性**（噪声/对比度/缩放/位移）：`t6_market.py` 两次都是 0 行（无 OCR），此前会话记录的「base 全对、变体价格集合一致」**在当前工作树无法复现**。
3. **150% DPI / 多屏异 DPI / 负坐标实机**：本机单屏 100%，`mss.monitors` 两项都是 `(0,0,1920,1080)`，无法区分主屏/虚拟屏；`SetProcessDpiAwareness` 失败路径（GUI 先建窗）也无法在不启动 GUI 的前提下复现。
4. **窗口化（非全屏）客户区偏移**：需要真实窗口，本审查按要求不开窗、不碰真机游戏。
5. **真实游戏里「价格 + 数量」落在同一 OCR 文本流**：只有构造文本（R06-05）与真实 ROI（R06-06），没有两者的交集样本。
6. **`_balance` 在烽火地带等其它页面**：可用截图只有交易行/筛选页，`_probe_02_market` 已暴露回退问题，但无法穷举所有页面的货币条排布。
7. **字模在 1440p/2160p 实机**：只有缩放模拟（`k=1.333/1.5/2.0` 全拒），无法判断实机上是「全拒」还是「部分命中」。
8. **`_shots` 目录已被并行会话清理**（`DELETED_FILES_MANIFEST.md` 2026-11-05 记录），`t5_glyph.py` 原图缺失；本次改用 `_backup_20261105\_shots_legacy\p_filter.png`，两份截图的 UI 版本若有差异，字模结论需按新图复核。
