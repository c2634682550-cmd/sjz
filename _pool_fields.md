# 入池与抽签证据字段（`_pool_fields.md`）

## 0. 结论

台账行尾现在带 8 个入池/抽签事实字段 + 挂牌时刻误差列 + 矛盾标记 + 结果来源
（共 10 列，整行 22 列），数据来源是游戏顶部 toast 的 OCR 文案；识别不到一律
`null` / `unknown`，没有任何一列用 `0` 冒充，单边点数也不推结论。

复核三轮打回的问题已全部改完：

1. **第一轮（13 条）**：单字拆词拼接、全角逗号不吞千分位、`最大点数` 整段挖掉再找
   自己的点数、五位数不截断、ZWNJ 等零宽字符清洗、`最高点数` 与 `最大点数`
   等价；等值平局 / 单边点数 / 未命中一律 `won=null`；否定句不判赢；`成功率`
   名词短语不判赢；新增 `join_conflict`；`matched` 只决定发不发旁路。
2. **第二轮（8 条同类变体）**：全角逗号 → 语义分隔（`99，125` 不再粘成 `99125`），
   千分位只认**原始 ASCII 逗号**且在 NFKC 之前剥；新增 `self<=max` 与 max 上界
   自洽校验（第二轮的做法是「整组置 null」，**第三轮 D1 已改为字段级失效**）；
   `您本次最高点数达到 125` 能读到 max；`没能中签` 进否定表；三段单字无锚点写
   `null`，`12 5` / `1 25` 反向补齐；新增 `won_source` 列；
   `tools/realmachine_v2.py` 证据列 7 → 10；列表直达路线补二次读数。
3. **第三轮（D1–D5）**：
   - **D1 字段级失效**：`self` 与 `max` 各自独立赋值，各自能读到可信数值就保留；
     两边都读到但 `self > max` 或 `max > 1,000,000` 只是这两条事实互相矛盾 →
     保留两个原值、`join_conflict=true`、`won=null`、`won_source=null`；只有某个
     字段自身无法可信解析（0 / 空 / 8 位以上噪声 / 参与人数）才把该字段置 `null`。
     可信的 `self` 不再被 max 的问题连带抹掉（下游算入池率缺 `self` 比缺 `max` 更致命）。
   - **D2**：`max>1M` 与 `self>max` 走同一套规则（两值都留 + `conflict`），
     不再出现「只清 max 不清 self」的半行。
   - **D3**：补「本轮最大值 125」变体（必须带轮次前缀，泛化的「最大值 125」不认）。
   - **D4**：数字后紧贴「本轮」即字段分界（`99本轮最大点数 125` → `99` / `125`）。
   - **D5**：整串拆词后面跟「人/名/位/参与」→ 整串都归参与人数，一个都不并入点数
     （`1 2 3 人参与` 不再造出 `12`）。
4. **开销与 elapsed**：探针每次尝试只读一次、读到即短路，耗时从 `elapsed`
   扣掉；开销按三档实测口径写（见 §5）。

`listed_at` 只在读到解锁倒计时时按 `首次读到时刻 − 剩余秒` 反推，并做一次
二次读数交叉校验：两次之间剩余秒的下降和墙钟差对不上（残影/跳变）→ 直接
`null`，不按正常值记账；倒计时路线与列表直达路线都做二次读数，详情页/列表
路线读数前的 0.55s 面板等待算进 ±误差。

`tools/telemetry_agent.py` 与 `evo/` **一行没改**：前者用白名单静默丢弃额外列，
不存在“未知字段整批拒收”；后者本来就在读 `evo/collect/inbox/*.jsonl` 这条
toast 旁路，引擎把同一段 toast 追加进去，evo 不改代码即可拿到 `join_result /
roll_self / roll_max / won`。老台账（10 列）与新台账（22 列）混排读入、
统计、轮转全部照旧。

## 1. 改动文件

| 文件 | 改动 |
|---|---|
| `core/pooltrace.py` | **新增**。toast 规范化/解析、`listed_at` 反推、证据容器、evo inbox 旁路 |
| `core/engine.py` | 接线：顶部 toast ROI 探针、倒计时首读时刻、`_record` 行尾追加证据 |
| `core/nav_coords.py` | **新增** `N_TOAST`（顶部提示条 ROI，1920×1080 口径） |
| `tests/test_pool_fields.py` | **重写**。复核三轮用例 + D1 对抗（self 保全）+ 残影二次读数 + 探针开销 + 端到端 10 列 + 老/新台账混排与轮转 |
| `tests/test_feature_telemetry.py` | 行尾断言固定为「`offset_ms` 起 10 列切片」（`offset_ms/tier` + 本特性 10 列），后续别的特性再追加不受牵连 |
| `tools/realmachine_v2.py` | 证据列 `SEVEN_FIELDS`(7) → `EVIDENCE_FIELDS`(10)，S6 回放/导出同步 |

没有改 `web/`，没有 commit / push，没有开可见窗口，没有动真实光标。

## 2. 字段契约（追加在 `tier` 之后）

| 字段 | 类型 | 取值 | 缺失时 |
|---|---|---|---|
| `pool_joined_at` | `int` / `null` | 读到“已加入抢购池” toast 的毫秒时刻 | `null` |
| `join_result` | `str` | `joined` / `queue_full` / `unknown` | `unknown` |
| `roll_self` | `int` / `null` | 本次点数 | `null` |
| `roll_max` | `int` / `null` | 本轮最大点数 | `null` |
| `won` | `bool` / `null` | 是否中签 | `null` |
| `listed_at` | `int` / `null` | 挂牌时刻（毫秒，反推值） | `null` |
| `publicity_s` | `null` | 公示时长：**不可反推**，固定 `null`（理由见 §4） | `null` |
| `listed_at_err_s` | `float` / `null` | `listed_at` 的 ±秒误差（含二次读数实测偏差） | `null` |
| `join_conflict` | `bool` | 文案结论与点数矛盾 / 只有单边点数 / `self>max` / `max>1,000,000` → `true`；无矛盾 → `false` | `false` |
| `won_source` | `str` / `null` | `text`（文案给的结论）/ `points`（点数推的）/ `null`（没有结论） | `null` |

既有字段 `ts/t/name/price/result/elapsed/shot/mode/quota/verified` 名称与顺序未动，
`offset_ms`、`tier` 的相对位置也未动；新列严格追加在 `tier` 之后。

**行尾契约（复核追加项）**：`_record` 里 `extra` 的最后一个块是
`**pool_extra`，其后没有任何键；`offset_ms`/`tier` 仍是 10 列之后的第 11、12
列。契约由一条断言锁死：

```python
assert keys[keys.index("offset_ms"):] == [
    "offset_ms", "tier",
    "pool_joined_at", "join_result", "roll_self", "roll_max", "won",
    "listed_at", "publicity_s", "listed_at_err_s", "join_conflict",
    "won_source"]
```

（`tests/test_feature_telemetry.py::test_new_fields_are_appended_at_row_end`；
`won_source` 作为第 10 个证据列固定在第 22 列，任何后续列插到它前面都会红。）

**结论矩阵（复核第三轮 D1 重新定值）**：

| 文案 | 点数 | `roll_self` / `roll_max` | `won` | `won_source` | `join_conflict` |
|---|---|---|---|---|---|
| 赢 | `self < max` | 原值 / 原值 | `true` | `text` | `true`（矛盾） |
| 赢 | 等值 | 原值 / 原值 | `true` | `text` | `false`（点数无法复核，单证据） |
| 赢 | `self > max` 或 `max>1M` | **原值 / 原值** | `null` | `null` | `true`（点数互相矛盾） |
| 输 | `self < max` | 原值 / 原值 | `false` | `text` | `false` |
| 输 | `self > max` 或 `max>1M` | **原值 / 原值** | `null` | `null` | `true` |
| 无 | `self < max` | 原值 / 原值 | `false` | `points` | `false` |
| 无 | `self > max` 或 `max>1M` | **原值 / 原值** | `null` | `null` | `true` |
| 无 | 等值 | 原值 / 原值 | `null` | `null` | `false` |
| 无 | 单边 / 缺失 | 读到的那一边保留，另一边 `null` | `null` | `null` | `true` |

关键点：矛盾组合下 **两个原值都留在台账里**，只是不落结论 —— 这与第二轮
「整组置 null」的口径相反。原先「文案赢 + 点数等值」只有一个 `conflict=false`，
下游分不出这格是「点数也支持」还是「点数没说话」；现在 `won_source=text`
明确它是单一证据。

## 3. toast 解析、容错与复核用例对照

`core.pooltrace.normalize()` 的顺序是：去零宽（ZWSP/ZWNJ/ZWJ/WORD JOINER/BOM/
U+180E）→ **在原始文本上按 ASCII 逗号剥千分位** → `NFKC`（全角数字/冒号 →
半角，全角逗号也变 `,`）→ 换行/多空格压成单空格。

千分位规则：只有**原始文本里的 ASCII 逗号**、且后面正好三位、再后面不再接
数字时才剥。`12,345 → 12345`、`1,250 人 → 1250`、`1,250，共 12 人参与 → 1250`
都能还原；全角逗号一律按语义分隔，`99，125` 保持 `99` + `125` 两个数，绝不
粘成 `99125`（复核第二轮第 1 条）。

| 类别 | 文案 | 解析结果 |
|---|---|---|
| 进池成功 | `您已加入抢购池，将…进行随机抽取` | `joined`，`pool_joined_at`=当时毫秒 |
| 排队已满 | `排队已满` / `抢购池已满，人数过多` | `queue_full`，`pool_joined_at=null` |
| 未进池 | `未进入抢购池，请稍后再试` | `unknown`，其余 `null` |
| 抽签失败带点数 | `未抢购成功，随机点数 99 / 本轮最大 125` | `99/125`，`won=false` |
| 抽签胜利 | `恭喜您抢购成功！随机点数 125 / 本轮最大点数 125` | `125/125`，`won=true`（`won_source=text`） |
| 识别不到 | `系统提示：请稍后再试` | `unknown`，全部 `null` |

> 口径变更说明：`1，２５０`（全角逗号 + 全角数字）现在按语义分隔解析为 `1`，
> 不再还原成 `1250`。复核第二轮定死“只认原始 ASCII 逗号”，全角逗号一律当分隔；
> 旧用例已按新口径改写（见 §8 的用例清单）。

### 3.1 复核用例逐条：修复前 → 修复后

| # | 用例 | 修复前 | 修复后（实测） |
|---|---|---|---|
| 1 | `随机点数 9 9，本轮最大点数 1 2 5` | `9 / 1`，误判赢 | `99 / 125`，`won=false` |
| 2 | `随机点数 99，125 人参与本轮抽签` | `self=99125（复核读数 9912）` | `self=99`，`max=null`，`won=null`，`conflict=true` |
| 3 | `本轮最大\n点数 125` | `self=125` | `self=null`，`max=125`，`won=null` |
| 4 | `随机点数 12,345` | 截断成 `1234` | `12345`（不设四位上限） |
| 5 | `随机点数 9\u200c9，本轮最大点数 1\u200d2\u20605` | 零宽字符导致串联失败 | `99 / 125` |
| 6 | `最高点数 125，随机点数 99` | `最高` 不认 | `max=125`，`self=99` |
| 7 | `随机点数 100 / 本轮最大点数 100` | `won=true`（等值判赢） | `won=null`（等值不推） |
| 8 | `没能 / 没有 / 并未 / 未抢购成功` | 判成功 | `won=false` |
| 9 | `本次抢购成功率 12%` | 判成功 | `won=null`，`matched=false` |
| 10 | `随机点数 99`（只有单边） | 可能推 `won` / 漏记 | `won=null`，证据照落账，`conflict=true`，不发旁路 |
| 11 | `恭喜您抢购成功！随机点数 99 / 本轮最大 125` | 无矛盾标记 | `won=true`（文本）且 `join_conflict=true`（点数矛盾） |
| 12 | 残影：2s 后二次读数仍 40s | 按正常值落账、误差低估 | `listed_at=null`、误差 `null` |

### 3.2 第二轮复核用例：修复前 → 修复后（实测）

| # | 用例 | 修复前 | 修复后（实测） |
|---|---|---|---|
| 1a | `随机点数 99，125 / 本轮最大点数 125` | `self=99125`，`won=true` | `self=99` `max=125` `won=false` `won_source=points` |
| 1b | `未抢购成功，随机点数 99，125。` | `self=99125` | `self=99` `won=false` |
| 1c | `随机点数 1,250 人` | `self=1` | `self=1250`（ASCII 逗号才剥千分位） |
| 1d | `随机点数 1,250，共 12 人参与` | `self=1250`（自相矛盾） | `self=1250`（与 1c 一致） |
| 2 | `随机点数 99125 / 本轮最大点数 125` | `99125>125` 仍落账并推 `won=true` | 第二轮：整组 `null`；**第三轮 D1 后：`self=99125` `max=125` 两值都留，`won=null`、`won_source=null`、`conflict=true`** |
| 3 | `您本次最高点数达到 125` | `max=null` | `max=125`，`self=null` |
| 4 | `没能中签，随机点数 99` | `won=null` | `won=false`，`won_source=text` |
| 5a | `随机点数 1 2 3` | `self=123` | `self=null`（三段无锚点，歧义不猜） |
| 5b | `随机点数 12 5` | `self=12`（漏拼） | `self=125` |
| 5c | `随机点数 1 25` | `self=1`（漏拼） | `self=125` |
| 6 | 台账结果来源 | 只有 `join_conflict`，分不清文案/点数 | 新增 `won_source`=text/points/null；「文案赢+点数等值」= `text`+`conflict=false` |
| 7 | `tools/realmachine_v2.py` 证据列 | `SEVEN_FIELDS` 只有 7 | `EVIDENCE_FIELDS` 10，S6 回放/导出不再丢 `listed_at_err_s`/`join_conflict`/`won_source` |
| 8 | 列表直达路线残影 | 只有 0.55s 面板等待并入误差，无二次读数 | 首读后 0.12s 再读一次，超差 → `listed_at=null` |

### 3.3 实现细节

- **失败先于成功**：`未抢购成功` 里含子串 `抢购成功`，顺序反了会把失败当中签。
  现在先把 `(没能|没有|并未|未|没)…成功` 这一整段从文本里挖掉，再找成功；
  并补上 `没能中签 / 未能中签 / 未中签` 等不带“成功”的否定句。
  `成功率/概率/次数/记录` 用负向断言挡掉。
- **拆词数字**：把紧邻的、空白分隔的数字段接起来（`12 5`、`1 25` 都补成 `125`）；
  自己的点数出现 **≥3 段**且无锚点（`1 2 3`）→ 歧义写 `null`；最大/最高一侧
  允许三段（`1 2 5 → 125`，第一轮 PASS 用例）。
- **参与人数后缀排斥整串（D5）**：拼接完成后，如果**整串（≥2 段）**后面紧跟
  「人/名/位/参与」，这串数字全是参与人数，一个都不并入点数 ——
  `1 2 3 人参与`、`1 2 5 人参与` 都写 `null`，不再产出文本里不存在的 `12`。
  单段（`1,250 人`）不适用：那是千分位还原后的一个完整数字，仍取 `1250`。
- **最大/最高点数**：三种表达都认 —— `(本轮|本次|当前|这轮)?(最大|最高)点数`、
  `(本轮|本次|当前|这轮)(最大|最高)值`、`(本轮|本次)(最大|最高)`（裸写）。
  泛化的「最大值 125」不带轮次前缀，**不当本轮最大点数**（D3）。
  找自己的点数前，这三种表达整段替换成字段边界 `|`，因此数字紧贴中文的
  `99本轮最大点数 125` 不会再被拼成 `99125`（D4）。关键词与数字之间的
  「达到 / 为 / 是 / 共 / 有 / 即 / 等于」作为连接词跳过。
- **自洽校验（字段级失效，D1/D2）**：`self` 与 `max` 各自独立赋值，各自能读到
  可信数值就保留；`max > 1,000,000` 或 `self > max` 只置 `inconsistent`，
  **不改写两个字段**，再由 `inconsistent` 把 `won`/`won_source` 置 `null`、
  `join_conflict` 置 `true`。只有字段自身不可信（0 / 空 / 8 位以上 / 参与人数）
  才单独置 `null`。
- **等值/单边不推**：只有双方点数都存在、没被标矛盾且严格不等时才由点数推
  `won`；等值、单边、未命中一律 `null`。
- **矛盾标记**：文案赢但 `self<max`、文案与点数结论相反、只有单边点数、
  `self>max` / `max>1M` → `join_conflict=true`；`won_source` 记录结论出处。
- **`matched` 只决定发不发 evo 旁路**，不再拦证据写入：单边点数照样进台账。
- 数字 `0` 一律当没读到（OCR 把 `O/○` 读成 `0` 时不污染台账）；超过
  10,000,000 的读数按噪声写 `null`。

### 3.4 第三轮复核 D1–D5：修复前 → 修复后（实测）

| # | 用例 | 修复前（第二轮） | 修复后（实测） |
|---|---|---|---|
| D1-1 | `随机点数 999，本轮最大点数 1，250` | `null / null / null / null / true`（整组丢） | `self=999` `max=1` `won=null` `source=null` `conflict=true` |
| D1-2 | `未抢购成功，随机点数 99，本轮最大点数 12` | `null / null / false / text / true` | `self=99` `max=12` `won=null` `source=null` `conflict=true` |
| D1-3 | `随机点数 99，本轮最大点数 1.25` | `null / null / null / null / true` | `self=99` `max=1` `won=null` `conflict=true` |
| D1-4 | `随机点数 99，本轮最大点数 0` | `self=99` `max=null` `conflict=true` | 同左（0 仍按缺失，只清该字段） |
| D1-5 | `随机点数 99，本轮最大点数 无` | `self=99` `max=null` `conflict=true` | 同左 |
| D1-6 | `随机点数 99，本轮最大点数 1 2 5人参与` | `self=99` `max=null` `conflict=true` | 同左（`max` 一整串归人数） |
| D1-7 | `本轮最大点数 125，随机点数 99` | `self=99` `max=125` `won=false` `points` | 同左（max 在前不影响） |
| D2-1 | `随机点数 99 / 本轮最大点数 1250000` | `self=99` `max=null` `conflict=true`（只清 max） | `self=99` `max=1250000` `won=null` `conflict=true`（两值都留） |
| D2-2 | `随机点数 99，本轮最大点数 1,200,000` | 同上 | 同上 |
| D3-1 | `随机点数 99，本轮最大值 125` | `self=99` `max=null` `conflict=true` | `self=99` `max=125` `won=false` `points` |
| D3-2 | `本轮值 125` 一类泛化表述 | 不认 | 不认（`最大值 125 仅供参考` → `max=null`） |
| D4-1 | `随机点数 99本轮最大点数 125` | `null / null`（整段不可解析） | `self=99` `max=125` `won=false` `points` |
| D4-2 | `随机点数99本轮最大点数125` | `null / null` | `self=99` `max=125` |
| D5-1 | `随机点数 1 2 3 人参与` | `self=12`（造出不存在的数） | `self=null` `max=null` `conflict=false` |
| D5-2 | `随机点数 1 2 5 人参与` | `self=12` | `self=null` |

**D1 对抗 10 条**（`self=99` 合法 + max 各种坏法，目标：`self` 一律留下）：

| # | max 的坏法 | 文案 | 实测 |
|---|---|---|---|
| 1 | 缺位 | `随机点数 99，本轮最大点数 无` | `99 / null / conflict=true` |
| 2 | 全角千分位 | `随机点数 99，本轮最大点数 1，250` | `99 / 1 / null / conflict=true` |
| 3 | 粘连中文 | `随机点数 99，本轮最大点数 1 2 5人参与` | `99 / null / conflict=true` |
| 4 | 为 0 | `随机点数 99，本轮最大点数 0` | `99 / null / conflict=true` |
| 5 | 为空 | `随机点数 99，本轮最大点数 ` | `99 / null / conflict=true` |
| 6 | 在 self 之前 | `本轮最大点数 125，随机点数 99` | `99 / 125 / false / points` |
| 7 | 在 self 之前且更小 | `本轮最大点数 12，随机点数 99` | `99 / 12 / null / conflict=true` |
| 8 | 超 1M | `随机点数 99，本轮最大点数 1,200,000` | `99 / 1200000 / null / conflict=true` |
| 9 | 8 位噪声 | `随机点数 99，本轮最大点数 12,345,678` | `99 / null / conflict=true` |
| 10 | 小数点截断 | `随机点数 99，本轮最大点数 1.25` | `99 / 1 / null / conflict=true` |

10 条全部保住 `self=99`，没有一条整组丢。对应测试
`test_self_survives_conflicting_roll_pair` /
`test_self_is_never_dropped_when_max_is_unreadable` /
`test_max_above_ceiling_keeps_both_values` / `test_max_before_self_preserves_self`。

## 4. `listed_at` 反推与误差

- 取值点：倒计时 OCR 返回的那一瞬间同时取 `epoch_ms` 和该帧的剩余秒，
  两者来自同一帧，避免“时间戳是这一帧、剩余秒是上一帧”。
- 公式：`listed_at = seen_ms − remaining_s * 1000`（与项目既有旁路口径
  `_bypass_collect.md §3` 的 `first_seen − remaining` 一致）。
- 误差：`listed_at_err_s = max(两次 OCR 延迟) + 1.0 + 二次读数实测偏差`。`+1.0`
  是倒计时显示向下取整最多带来的 1 秒偏差；OCR 延迟按每次读数用
  `time.perf_counter()` 实测，写进同一行。
- **残影交叉校验**：倒计时路线在选中/读编号之后再读一次（间隔约 2–3s），
  要求「剩余秒的下降 ≈ 墙钟差」。偏差 > 1.5s（已覆盖 1s 显示截断 + 抖动）说明
  第一帧是上一件留下的旧画面 → `listed_at=null`、误差 `null`，不按正常值记账
  （复核用例 12）。偏差在容差内则把实测偏差加到误差上，不假装比实际更准。
- **列表直达路线也做二次读数**（复核第二轮第 8 条）：首读后 0.12s 再读同一块
  clock ROI，两次剩余秒的下降与墙钟差超 1.5s → `listed_at=null`。此前这条
  路线只有单次读数 + 0.55s 面板等待并入误差，残影会静默按正常值落账。
- **详情页路线**：读数前有 0.55s 面板渲染等待，这段算进误差
  （`scan_lag += 0.55`），否则 `listed_at` 会显得比实际更准。
- 读不到 clock、`remaining_s <= 0`（已解锁或“剩余：X天”这种上架剩余读数）→
  `listed_at=null`、误差 `null`，不硬算。
- `publicity_s` 保持 `null`：画面只能看到“剩余”，看不到公示起点；实测公示时长
  在 6:17–6:43 之间波动，没有稳定常量，按“不要硬猜”留空。

## 5. 不阻塞主循环 / 开销实测

- toast 探针只读 `N_TOAST`（1920×1080 下 880×36 的小条）；**每次尝试只读 1 次**，
  且放在确认连点结束之后，不占“点得快”的那段窗口；读到有效 toast 后
  `has_toast()` 直接短路，后续重复探针 0 成本。
- **探针耗时不计入 `elapsed`**：`_probe_toast` 累加到 `_probe_s`，
  `_elapsed_since()` 统一扣掉（下限 0）。
- **单次探针实测三档（复核实测口径，不再写单一数字）**：
  - 本地 RapidOCR：中位 **215.5ms**（区间 **181–247ms**）；
  - PP-OCR 服务在线：**805–1218ms**；
  - 后端冷启动：**3229ms**（首次调用）。
  修复前每次尝试 3 次探针（3×215.5 ≈ 646ms，PP-OCR 在线时 2.4–3.6s）全落在
  1.5s 校验窗并计入 `elapsed`；修复后每次尝试只读 1 次，`elapsed` 里的探针
  增量为 **0**（测试用 140ms 假 OCR 验证扣减后回到真实耗时）。
- **注意**：探针耗时虽然从 `elapsed` 扣掉，但墙钟上单次判定仍可能多出最多约
  **1.2 秒**（PP-OCR 服务在线档）；这是“证据优先”的代价，已写进 `pooltrace`
  的模块注释，不当作零成本。
- 解析/写入无 I/O，纯正则 + 一次 `records.add`；evo 旁路是追加一行小 JSON，
  失败静默，绝不因为证据读取拦住抢购。

测量方法（已固化为测试
`tests/test_pool_fields.py::test_accounting_overhead_under_5ms` 与
`test_probe_is_read_once_and_short_circuits` /
`test_probe_seconds_are_deducted_from_elapsed`）：

```powershell
python -m pytest tests/test_pool_fields.py::test_accounting_overhead_under_5ms -q -s
python -m pytest tests/test_pool_fields.py::test_probe_is_read_once_and_short_circuits tests/test_pool_fields.py::test_probe_seconds_are_deducted_from_elapsed -q -s
```

实测输出（本机 Windows / CPython 3.11，多轮复跑取最近一轮）：

```
[pool] 纯解析 0.016ms/件；台账单件 9.091 → 11.451ms（+2.360ms）
```

- 纯解析 + 取证据：**0.009–0.016ms/件**。
- `records.add` 前后对比：本轮 **+2.360ms/件**；多轮区间 **+0.045 ~ +2.360ms**
  （波动来自机器上并行跑的多个 pytest 进程，不是本改动本身）。
- 台账单件绝对值的 ~5–11ms 主要是既有文件锁 + JSON 落盘，与本改动无关；即使
  按最坏一轮，也远低于 5ms 的验收线。
- 探针三档：**本地 215.5ms（181–247）/ PP-OCR 在线 805–1218ms / 冷启动 3229ms**；
  修复前每次尝试 3 次并计入 elapsed，修复后 1 次、`elapsed` 增量 0，但墙钟
  仍可能多出最多约 1.2s。

## 6. 消费方兼容（未改一行代码，怎么确认的）

### 6.1 `tools/telemetry_agent.py`

读源码确认：它把每条台账行按固定 `EVENT_KEYS` 白名单拼事件
（`tools/telemetry_agent.py` 的 `_event()`），**只输出白名单里的键**；新列既不会
进入请求体，也不会触发“未知字段”整批拒收。测试
`test_telemetry_whitelist_drops_unknown_fields_without_rejecting` 把带 10 个新列的
行喂给 `_event()`，断言输出键集合与 `EVENT_KEYS` 逐字一致（而不是报错或透传）。

### 6.2 `evo/`

- 台账解析：`evo/collect/parse.py::parse_ledger_line` 只读它认识的键，多出的列
  直接忽略；测试用带全部新列的行调用它，返回的事件 `stage_reached` 正常、不抛异常。
- toast 事实：`evo/collect/collector.py` 本来就会扫
  `evo/collect/inbox/*.jsonl`（`inbox_paths()` / `parse_toast_line()`）。引擎在
  `core/pooltrace._emit()` 里把识别到的 toast 追加到同一目录（`YYYYMMDD.jsonl`，
  字段 `ts/text/raw/name/source`），evo **不改代码**即可读到。
- 为适配 evo 既有正则，旁路 `text` 不是原样转发，而是用解析出的**事实**重写成
  evo 认得的规范句（`已加入抢购池` / `排队已满` / `随机点数 N` /
  `本轮最大点数 M` / `抢购成功` / `未抢购成功`），原文完整留在 `raw` 里。
  这样 evo 不改代码也能算 `roll_self / roll_max / won`。测试
  `test_evo_ledger_parser_ignores_extra_fields_and_inbox_toast_is_readable`
  用 evo 自己的 `parse_toast_line` 读我们写出的行，得到
  `roll_self=99 / roll_max=125 / won=false`。

## 7. 老台账回归

`test_old_ledger_without_new_columns_still_loads_stats_and_rotates`：

1. 手写两条没有新列的旧行（10 列）→ `records.load(0)` 正常读回 2 条，
   `records.stats()` 得到 `tries=2 / hits=1`；
2. **混排**：在旧文件上追加一条带全套 10 个新列的新行（22 列），旧行原样保留
   （`"roll_self" not in 旧行`），新行完整可读；统计 `tries=3 / hits=2`；
3. 把 `retention.RECORDS_MAX_BYTES` 压到 1 字节再追加，文件被轮转为 `.1` 归档，
   `records.load(0)` 仍能从归档里读到旧行和新行；
4. **轮转后 stats 一致**：`stats.tries == load(0) 的行数 == 4`、`hits == 2`，
   旧行（老记录A/B）与带新列的“新记录C”都还在，没有任何行因为轮转或混排丢失。
5. 老行缺 `listed_at_err_s`/`join_conflict`/`won_source` 时，下游按 `.get()` 取到
   `None`（`realmachine_v2.py` 的 S6 回放同样兼容），不要求老行补列。

## 8. 验证结果

- `python -m pytest tests/test_pool_fields.py -q` → **65 passed**（含第二轮 8 条、
  第三轮 D1–D5 与 10 条 D1 对抗）
- `python -m pytest tests/test_feature_telemetry.py -q` → **14 passed**
- 两文件合跑 → **79 passed**；再加倒计时 / 台账 / 轮转三条回归 5 文件合跑
  → **121 passed**（0 新增 skip）
- 47 格复核 + 9 条 D3/D4/D5 变体 + 10 条 D1 对抗，按新语义逐格断言 →
  `grid=47 extra=9 d1=10 mismatch=0`，D1「self 能读到就留下」**10/10**
- 全量 `python -m pytest tests -q --tb=line` → 见 §8.1。

### 8.1 全量结果与已知无关失败

第三轮修复后全量（2026-11-07 20:3x 启动，Windows / CPython 3.11）：

```
6 failed, 2144 passed, 135 skipped, 2 xfailed, 8 warnings in 644.40s (0:10:44)
FAILED tests/test_fix_d.py::test_start_clears_one_shot_flags
FAILED tests/test_ocr_panel.py::test_panel_rois_read_price_craft_wear
FAILED tests/test_ocr_panel.py::test_read_words_routes_panel_and_switch_off
FAILED tests/test_ocr_price.py::test_local_models_are_preferred_and_cold_start_is_small
FAILED tests/test_state_hardening.py::test_engine_start_resets_task_residue
FAILED tests/test_state_hardening.py::test_engine_start_is_atomic_under_barrier
```

6 条失败与本次改动无交集，逐条证据：

| 失败 | 所属模块（本次**未改**） | 证据 |
|---|---|---|
| `test_start_clears_one_shot_flags` | `core/autocalib.py` + `core/config.py` | 报错原文 `自动标定未通过：自动标定没跑起来：AttributeError: '_Cfg' object has no attribute 'data'`；全量跑的同时，`core/autocalib.py`（20:35）、`core/config.py`（20:34）被并行会话改动，本次改动只落在 `core/pooltrace.py`（20:08） |
| `test_panel_rois_read_price_craft_wear` | `core/ocr_panel.py` / `core/ocr_rapid.py` | `RuntimeError: RapidOCR 不可用：ModuleNotFoundError: No module named 'rapidocr'`，环境缺件 |
| `test_read_words_routes_panel_and_switch_off` | 同上 | 同一后端不可用 → 读回 0 个词 |
| `test_local_models_are_preferred_and_cold_start_is_small` | `core/ocr_rapid.py` | `available()` 为 False，环境缺件 |
| `test_engine_start_resets_task_residue` | `core/autocalib.py` | `AttributeError: 'dict' object has no attribute 'data'`，同一处并行改动 |
| `test_engine_start_is_atomic_under_barrier` | `core/autocalib.py` | 8 线程全部被 `自动标定未通过` 拦下，根因同上 |

- 这 6 个用例都不引用 `pooltrace`（全仓库只有 `tests/test_pool_fields.py` 引用），
  失败信息全部来自 autocalib / rapidocr 两条路径。
- 本次改动直接相关的 5 个文件（`test_pool_fields` / `test_feature_telemetry` /
  `test_engine_countdown_flow` / `test_records` / `test_retention`）合跑
  **121 passed**，没有新增 skip。
- 另：并行会话在 `core/records.py` 给 `records.add` 追加了 `first_seen_ms` /
  `listed_at_ms` / `no_evidence` 三个行尾键。本特性的 10 列仍紧接 `tier`、顺序
  不变；`test_new_fields_are_appended_at_row_end` 已改成「从 `offset_ms` 起
  固定长度的 10 列切片」断言，不再被后续追加牵连。

## 9. 边界与未做

- `web/` 未改；未 commit / push；测试全程无可见窗口、无真实光标操作（用合成
  `numpy` 帧 + 假 OCR）。
- `publicity_s` 按设计恒为 `null`，理由见 §4；若后续能从后台接口或日志拿到公示
  起点，再补这一列即可，台账读取不受影响。
- 顶部 toast ROI 默认用 `N_TOAST`；如果以后游戏改版，只需在配置里加
  `regions.toast`，`_probe_toast` 会优先用它。
