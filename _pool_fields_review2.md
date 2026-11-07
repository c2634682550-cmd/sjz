# 入池/抽签字段第二轮修复 —— 独立复核（`_pool_fields_review2.md`）

复核人方式：只读 `_pool_fields.md` 的 §2 行尾契约 + §2 结论矩阵，**没有读 `core/pooltrace.py` 的函数体**，
只用 `inspect.signature` / `__doc__` / `EVIDENCE_KEYS` 确认调用面，然后按契约自己写判定表，
用真实调用（`core.pooltrace.parse_toast(text)`）逐格比对。
产物：本文件；脚本 `review2_probe.py` / `review2_attack.py` / `soak_noprobe.py` / `poison_pooltrace.py`（均在工作区外）。
未 commit、未 push、未改任何实现代码、未开可见窗口、未动真实光标。

---

## 1. 独立判定表（先写期望，再跑）

期望全部由 §2「结论矩阵」+ §3「toast 解析、容错」推导，不参考实现输出：

- `won`：文案给了结论就优先文案（`won_source=text`）；否则只有 `self`、`max` 都存在且严格不等时才由点数推（`points`）；等值/单边/缺失一律 `null`。
- `join_conflict`：文案与点数矛盾、只有单边点数、`self>max` / `max>1,000,000` 自洽校验失败 → `true`。
- 千分位：只认**原始 ASCII 逗号**且后接恰好三位、再后不接数字；全角逗号一律语义分隔。
- `0` 当没读到；`>10,000,000` 当噪声；自点数 ≥3 段无锚点 → `null`。

### 1.1 第一轮 13 条（拆成 14 行，否定句分 4 种写法）

| # | 原始 toast 文本 | 期望 self | 期望 max | 期望 won | 期望 source | 期望 conflict |
|---|---|---|---|---|---|---|
| R1-1 | `随机点数 9 9，本轮最大点数 1 2 5` | 99 | 125 | false | points | false |
| R1-2 | `随机点数 99，125 人参与本轮抽签` | 99 | null | null | null | true |
| R1-3 | `本轮最大\n点数 125` | null | 125 | null | null | true |
| R1-4 | `随机点数 12,345` | 12345 | null | null | null | true |
| R1-5 | `随机点数 9\u200c9，本轮最大点数 1\u200d2\u20605` | 99 | 125 | false | points | false |
| R1-6 | `最高点数 125，随机点数 99` | 99 | 125 | false | points | false |
| R1-7 | `随机点数 100 / 本轮最大点数 100` | 100 | 100 | null | null | false |
| R1-8a | `没能中签，随机点数 99` | 99 | null | false | text | true |
| R1-8b | `没有中签，随机点数 99 / 本轮最大点数 125` | 99 | 125 | false | text | false |
| R1-8c | `并未抢购成功，随机点数 99 / 本轮最大点数 125` | 99 | 125 | false | text | false |
| R1-8d | `未抢购成功，随机点数 99 / 本轮最大点数 125` | 99 | 125 | false | text | false |
| R1-9 | `本次抢购成功率 12%` | null | null | null | null | false |
| R1-10 | `随机点数 99` | 99 | null | null | null | true |
| R1-11 | `恭喜您抢购成功！随机点数 99 / 本轮最大 125` | 99 | 125 | true | text | true |

### 1.2 第二轮 8 条（拆成 10 行）

| # | 原始 toast 文本 | 期望 self | 期望 max | 期望 won | 期望 source | 期望 conflict |
|---|---|---|---|---|---|---|
| R2-1a | `随机点数 99，125 / 本轮最大点数 125` | 99 | 125 | false | points | false |
| R2-1b | `未抢购成功，随机点数 99，125。` | 99 | null | false | text | true |
| R2-1c | `随机点数 1,250 人` | 1250 | null | null | null | true |
| R2-1d | `随机点数 1,250，共 12 人参与` | 1250 | null | null | null | true |
| R2-2 | `随机点数 99125 / 本轮最大点数 125` | null | null | null | null | true |
| R2-3 | `您本次最高点数达到 125` | null | 125 | null | null | true |
| R2-4 | `没能中签，随机点数 99` | 99 | null | false | text | true |
| R2-5a | `随机点数 1 2 3` | null | null | null | null | false |
| R2-5b | `随机点数 12 5` | 125 | null | null | null | true |
| R2-5c | `随机点数 1 25` | 125 | null | null | null | true |

### 1.3 我自己的对抗 15 条（另加 5 条矩阵边界）

| # | 原始 toast 文本 | 期望 self | 期望 max | 期望 won | 期望 source | 期望 conflict |
|---|---|---|---|---|---|---|
| A1 | `随机点数 99，本轮最大点数 125`（全角逗号） | 99 | 125 | false | points | false |
| A2 | `随机点数 9\u200b9\u180e，本轮最大点数 1\u200c2\u200d5` | 99 | 125 | false | points | false |
| A3 | `随机点数 1,25` | 1 | null | null | null | true |
| A4 | `随机点数 1250,5` | 1250 | null | null | null | true |
| A5 | `随机点数 1,250,000` | 1250000 | null | null | null | true |
| A6 | `随机点数 1 2 3 人参与` | null | null | null | null | false |
| A7 | `随机点数 125 人` | 125 | null | null | null | true |
| A8 | `随机点数 1 2 5 人参与` | null | null | null | null | false |
| A9 | `随机点数 125 / 本轮最大点数 0` | 125 | null | null | null | true |
| A10 | `随机点数 125 / 本轮最大点数 125`（等值） | 125 | 125 | null | null | false |
| A11 | `随机点数 ９９，本轮最大点数 １２５`（全角数字） | 99 | 125 | false | points | false |
| A12 | `随机\n本轮最大\n点数 125`（换行嵌套） | null | 125 | null | null | true |
| A13 | `随机点数99，本轮最大点数125`（数字紧贴中文） | 99 | 125 | false | points | false |
| A14 | `本轮最大点数 125`（只有 max） | null | 125 | null | null | true |
| A15 | `随机点数 99 / 本轮最大点数 1250000`（max 上界） | null | null | null | null | true |
| A16 | `随机点数 99 / 本轮最大点数 125（MAX）` | 99 | 125 | false | points | false |
| A17 | `随机点数 九十九`（中文数字） | null | null | null | null | false |
| A18 | `随机点数 99，本轮最大点数 1,25` | 99 | 1 | false | points | true |
| B1 | `恭喜您抢购成功！随机点数 125 / 本轮最大点数 125`（文案赢+等值） | 125 | 125 | true | text | false |
| B2 | `未抢购成功，随机点数 125 / 本轮最大点数 99`（文案输+self>max） | null | null | false | text | true |
| B3 | `恭喜您抢购成功！随机点数 125 / 本轮最大点数 99`（文案赢+self>max） | null | null | true | text | true |
| B4 | `随机点数 99，本轮最大点数 12`（max 少读一位） | null | null | null | null | true |
| B5 | `随机点数 999，本轮最大点数 1，250`（全角千分位） | null | null | null | null | true |

> B2/B3/B4/B5 的期望是按 §0/§3.3「`self>max` → 整组 null」写的，不是按“尽量保留单边”写的。

---

## 2. 通过/失败矩阵（真实调用 vs 独立期望）

执行：`python review2_probe.py`（42 条）+ 5 条补充（B1–B5），逐字段比对 `roll_self / roll_max / won / won_source / join_conflict`。

**合计 47 格用例，43 一致，4 不一致。**

| # | 原始 toast 文本 | 期望 (self/max/won/source/conflict) | 实际 | 判定 |
|---|---|---|---|---|
| A6 | `随机点数 1 2 3 人参与` | `null/null/null/null/false` | `12/null/null/null/true` | **FAIL** |
| A8 | `随机点数 1 2 5 人参与` | `null/null/null/null/false` | `12/null/null/null/true` | **FAIL** |
| A15 | `随机点数 99 / 本轮最大点数 1250000` | `null/null/null/null/true` | `99/null/null/null/true` | **FAIL** |
| A18 | `随机点数 99，本轮最大点数 1,25` | `99/1/false/points/true` | `null/null/null/null/true` | **FAIL（我初判与契约冲突，见 §3-D1）** |

其余 43 格全部一致，含第一轮 14 行、第二轮 10 行、对抗 15 行与 B1–B5。关键佐证：

| # | 原始 toast | 实际 (self/max/won/source/conflict) | 佐证点 |
|---|---|---|---|
| R1-2 | `随机点数 99，125 人参与本轮抽签` | `99/null/null/null/true` | 全角逗号不粘连，`matched=false` |
| R1-4 | `随机点数 12,345` | `12345/null/null/null/true` | 五位数不截断 |
| R1-5 | `随机点数 9\u200c9，本轮最大点数 1\u200d2\u20605` | `99/125/false/points/false` | ZWNJ/ZWJ/WORD JOINER 全清 |
| R2-2 | `随机点数 99125 / 本轮最大点数 125` | `null/null/null/null/true` | 自洽校验把整组打空 |
| R2-3 | `您本次最高点数达到 125` | `null/125/null/null/true` | 连接词「达到」被跳过 |
| A3 | `随机点数 1,25` | `1/null/null/null/true` | 逗号后不是 3 位 → 不剥千分位 |
| A5 | `随机点数 1,250,000` | `1250000/null/null/null/true` | 两段千分位都剥掉，未截断 |
| A9 | `随机点数 125 / 本轮最大点数 0` | `125/null/null/null/true` | `0` 按缺失处理 |
| A10 | `随机点数 125 / 本轮最大点数 125` | `125/125/null/null/false` | 等值不推 won、不误标矛盾 |
| A11 | `随机点数 ９９，本轮最大点数 １２５` | `99/125/false/points/false` | NFKC 全角数字 |
| A12 | `随机\n本轮最大\n点数 125` | `null/125/null/null/true` | 跨行关键词仍能锚定 max |
| A16 | `随机点数 99 / 本轮最大点数 125（MAX）` | `99/125/false/points/false` | 英文括号不污染 |
| B1 | `恭喜…随机点数 125 / 本轮最大点数 125` | `125/125/true/text/false` | 文案赢+等值 → 单证据，不算矛盾 |
| B2 | `未抢购成功，随机点数 125 / 本轮最大点数 99` | `null/null/false/text/true` | 文案结论保留，点数整组打空 |
| B3 | `恭喜您抢购成功！随机点数 125 / 本轮最大点数 99` | `null/null/true/text/true` | 文案赢 + 点数矛盾 → conflict=true |

补充实测（负面但可解释，供后续看）：

| 原始 toast | 实际 | 说明 |
|---|---|---|
| `随机点数 99，本轮最大值 125` | `99/null/null/null/true` | 只认「最大/最高**点数**」，「最大值」变体丢 max |
| `随机点数 99本轮最大点数 125` | `null/null/null/null/true` | 数字后紧贴中文且无标点，整段不可解析 |
| `随机点数 99，本轮最大点数 第 12 名` | `99/null/null/null/true` | 名次不被当 max（正确） |
| `随机点数 99，本轮最大点数 125.0` | `99/125/false/points/false` | 小数尾巴被忽略（可接受） |
| `随机点数 99，本轮最大点数 1.25` | `null/null/null/null/true` | 小数点被当边界 → self>max → 整组打空（见 D1） |

### 2.1 关于它自报的测试数字

- `pytest tests/test_pool_fields.py -q` → **48 passed**（单人跑，2026-11-07 19:5x），与它自述一致。
- 同文件在我与其它的 pytest 进程并行跑时，`test_accounting_overhead_under_5ms` 破线：`台账单件 6.698 → 13.239ms（+6.541ms）`，
  单独复跑 3 次为 `+0.289 / +0.150 / +0.038ms`。**该用例对机器负载敏感，不是回归；但 5ms 硬阈值在并行负载下会假红。**

---

## 3. 新缺陷（都可复现）

### D1（高）max 被吃成更小值 → `self` 连带被整组打空

契约要求 `self>max` 时整组 `null`（`_pool_fields.md` §0、§3.3）。该规则把“max 读数失败”升级成“self 也丢”，
而 minimax 里 `self` 往往比 max 更可信（max 是别人的、位置更靠右、更容易被 ROI 裁掉）。

**最小复现 1（解析器自身导致，非 OCR）**

```
随机点数 999，本轮最大点数 1，250
实际：self=null max=null won=null source=null conflict=true
```

真值应是 `self=999, max=1250`（`1，250` 是全角逗号千分位）。按本轮定死的口径“全角逗号一律语义分隔”，
max 被读成 `1`，随后 `999>1` 触发整组打空。**契约自己制造了这个洞：全角逗号既是“语义分隔”又可能是“千分位”。**

**最小复现 2（OCR 截断/漏读一位，现场更可能）**

```
未抢购成功，随机点数 99，本轮最大点数 12
实际：self=null max=null won=false source=text conflict=true
```

文案结论保住了，但 `self=99` 与 `max=12` 双丢；`self` 本来完全合法，只是 max 少读了一位。
台账里既查不到自己多少点，也查不到 max，只剩下一个 `won=false`。

**同类**：`随机点数 99，本轮最大点数 1.25` → 小数点当边界，max=1 → 整组打空。

影响：`_pool_fields.md` §2 声称“单边点数照样进台账”，但只要另一边存在且更小，就一行都不剩；
数据源缺 self/max 会让后续“点数分布/差值/是否被压点”整类分析失效。

### D2（中）`max>1,000,000` 只清 max，不清 self —— 与自述“整组置 null”不符

```
随机点数 99，本轮最大点数 1250000
实际：self=99 max=null won=null source=null conflict=true
```

`_pool_fields.md` §0/§3.3 写的是「`self>max` 或 `max>1000000` 时**整组**置 null」。
实测 `self>max` 分支整组清空，`max>1M` 分支却保留 self，两条规则行为不一致；若下游按文档假设“整组为空”，
会读到“有 self 没 max”的半行。

### D3（中）`本轮最大值 125` 变体丢 max

```
随机点数 99，本轮最大值 125  →  max=null
```

只认「最大点数 / 最高点数」。若线上文案是「本轮最大值」，max 永远落不了账（self 正常、conflict=true）。
同类变体还有「最大点数为」「最大点数约 125」（连接词表已含 达到/为/是/共/有/即/等于，未含“约”）。

### D4（中）数字后紧贴中文、无标点 → 整段不可解析

```
随机点数 99本轮最大点数 125  →  self=null max=null conflict=true
```

OCR 把两行合并、或去掉了分隔标点时（`99本轮`），self/max 全丢。对照 `随机点数99，...` 是可解析的，
说明失败点在“数字后必须跟非中文边界”。

### D5（中低）`1 2 3 人参与` 会造出文本里不存在的 `12`

```
随机点数 1 2 3 人参与  →  self=12
随机点数 1 2 5 人参与  →  self=12
```

契约写「自点数 ≥3 段且无锚点 → null」。实现先把“3 人”剔成参人数，剩 2 段又拼成 `12`，
于是产出文本中没有任何读法支持的 `12`（`1 2 5 人参与` 的真实读法是 125 人参与）。
与 A6/A8 两格失败对应。

### D6（中，测试护栏）installer 的静态断言被放宽成空转

见 §4。

### D7（低，测试护栏）`test_accounting_overhead_under_5ms` 并行负载下假红

见 §2.1。

---

## 4. installer 那条到底谁对

**结论：安装器不会删用户数据（先搬后删，11 个目录实证）；那条测试的“期望”确实过时，
但过时的部分在 18:45 被改成了“只要存在 PreserveUserData 就跳过检查”，这等于护栏空转。**

### 4.1 安装器行为（证据行号）

- 卸载时先搬数据：`installer/sjz.iss:223-229` → `CurUninstallStepChanged(usUninstall)` 调 `PreserveUserData`。
- 搬什么：`sjz.iss:199-221` —— `config.json`(L207-208)、`launcher.config.json`(L209-210)、
  `data`（台账/截图，L212-213）、`templates`（L215-216）、`%LOCALAPPDATA%\SJZ-Control\license.json`(L218-220)，
  目标 `{localappdata}\SJZ-Control\uninstalled-<yyyymmdd-hhnnss>`（L203-205）。
- 之后才删运行副本：`[UninstallDelete]` `sjz.iss:104-117` 里确实有 `{app}\data`(L115)、`{app}\templates`(L116)，
  以及 `{app}\config.json`(L110-112)、`{app}\logs`(L117)。Inno 的 `usUninstall` 在文件删除前触发，顺序成立。
- 卸载页文案与行为一致：`sjz.iss:231-237`。

### 4.2 现场实证（不是“静态推断”）

`%LOCALAPPDATA%\SJZ-Control` 下当前有 **11 个** `uninstalled-20261107-*`（161154/161328/161553/161859/162118/
163135/163153/164507/164738/165537/165707）。抽查 `uninstalled-20261107-165707` 实际内容：

```
config.json 6234 | launcher.config.json 799 | license.json 33
data\ledger\records-2026-11-07.json 30 | data\shots\rehearsal-shot.png 10
data\f1cd7d30\buy.png 15182 | data\f1cd7d30\confirm.png 17216
templates\buy.png 15182 | templates\confirm.png 17216 | templates\meta.json 964
```

与“保留 config/ledger/shots/templates/license 到 uninstalled-<时间>”完全对上。

### 4.3 测试为什么不绿、后来为什么绿

- 测试本体：`tests/test_release_pipeline.py:61-66` → 调 `installer/verify_installer.py::static_checks()`。
- 旧断言下的命中项 (`verify_installer.py:75-77` 的 `leaks`)：`config.json`、`\data`、`\templates`
  都能在 `[UninstallDelete]` 里匹配到。**`sjz.iss` 17:29 改成“删运行副本”后，旧断言必红**——
  这正是它 18:0x 报的那条 `UninstallDelete 命中 config.json,\data,\templates`。
- 15:22 的历史产物 `build/installer_verify.json` 显示当时的同名检查（`"UninstallDelete 不含用户数据": true`），
  说明旧 iss 里并没有这些删除项，是 **iss 变了**，不是测试一直红。
- 18:45 的 `verify_installer.py:70-76` 加了 `preserved` 例外：只要文件里有 `PreserveUserData` 和
  `{localappdata}\sjz-control`，`leaks` 直接置空。于是现在 **PASS**（我复跑确认 1 passed）。

**判定**：不是“安装器删用户数据”，是「iss 有意改成删运行副本 → 旧断言只按字符串匹配 → 红了 → 断言被放宽」。
`sjz.iss` 17:29 与 `verify_installer.py` 18:45 的 mtime 就是时间线证据。

### 4.4 但这条护栏现在挡不住东西（D6）

- `verify_installer.py:75` 的 `leaks` 在 `preserved` 为真时恒为空 —— **空转**。
  它不再校验 `PreserveUserData` 是否真的覆盖了 `[UninstallDelete]` 里的每一条用户数据路径。
  将来若有人往 `[UninstallDelete]` 加 `{app}\data2` 却没加进 `PreserveUserData`，这条检查照样绿。
- `verify_installer.py:83-142` 的 `simulate()` 用的是**硬编码**路径表（L136-142），不解析 iss，
  所以它证明的是“这套 Python 演练逻辑能搬”，不是“iss 里的搬法覆盖了 iss 里的删法”。
- 安装器自身还有一个真风险：`CopyFile`/`CopyTree` 的返回值全被忽略（`sjz.iss:191, 208, 210, 213, 216, 219-220`），
  `ForceDirectories` 也不查结果（L205）。拷贝失败（磁盘满、文件被占用、权限、shots 很大超时）时仍继续卸载，
  而 `[UninstallDelete]` 是无条件删除 → **静默丢数据**。这条与“先搬后删”的正确性不冲突，但值得补一个失败即中止 + 校验。

---

## 5. “5 条都是既有问题”核实

复跑命令：`pytest <5 条> -q --tb=line`；并额外用 `poison_pooltrace.py`（任何 `parse_toast` 调用直接抛 `POOLTRACE_CALLED`）
作 A/B，证明用例是否触碰本轮改动。

| 失败项 | 现在是否失败 | 是否触碰本轮改动 | 证据 |
|---|---|---|---|
| `test_autocalib::test_large_offset_is_not_trusted_and_returns_builtin` | **PASS**（单独跑 1 passed） | 否：poison 下仍 PASS | 它自述“本机缺素材”已不成立，现状可跑绿，属环境/顺序相关，不能算“稳定既有失败” |
| `test_console_api::test_a_task_without_the_list_region_is_refused` | **FAIL**（`assert 200 == 400`，tests/test_console_api.py:179） | 否：poison 下同样 FAIL，且 `core/server.py` 里没有 `list_region`/列表区域校验（全文件仅 L1316/L1498 两处无关命中） | 是“产品缺校验 vs 测试期望”的真缺口，与本轮无关；改前改后都红 |
| `test_ocr_price::test_local_models_are_preferred_and_cold_start_is_small` | **PASS** | 否：poison 下 PASS | 它自述“本机未装 rapidocr”，实测 `importlib.util.find_spec("rapidocr") is True`（`rapidocr_onnxruntime` 才是 False），说法已过期 |
| `test_release_pipeline::test_installer_rules` | **PASS**（1 passed） | 否：纯静态文件检查 | 18:45 改了 `verify_installer.py` 的判定口径才转绿；18:0x 那次红是 iss(17:29) 变更导致（见 §4） |
| `test_soak::test_short_run_end_to_end` | **FAIL**（`assert any(state=="done")`） | 否：见下方 A/B | 失败原因是 `3.0s 内没等到确认弹窗，中止` + `连续 1 件都没抢到，停`（soak.log 第 12/14 行） |

**soak 的 A/B（独立做过，不是引用它的话）**

1. 正常跑：`cycles_ok=0, cycles_fail=6`；6 轮里 4 轮日志都是 `3.0s 内没等到确认弹窗，中止`。
2. 把 `engine.Engine._probe_toast` 打空后同参数再跑（`soak_noprobe.py`）：**仍 `cycles_ok=0, cycles_fail=6`**，
   日志同样是确认弹窗超时。→ 与本轮 toast 探针无关，是 `SoakEngine` 的假确认边界（`tools/soak.py:924` 配了
   `"confirm": None`，走“无弹窗版式”，但引擎仍按等弹窗路径判定）没接上。

**对“5 条都是既有问题”的判定**：

- `console_api`、`soak` 现在确实失败，且都不是本轮引入 → 这两条成立。
- `autocalib`、`ocr_price`、`installer` 现在都绿了；其中 installer 是 18:45 放宽断言后绿的（`_pool_fields.md` 的
  §8.1 写于 17:51，早于 18:45 的改动，**该表已过期**）。所以“这 5 条都是既有问题”按现在的事实只对 2 条；
  报告里“未改该模块”对 installer 也不成立（改的是 `installer/verify_installer.py`，不是安装器本体）。

---

## 6. 结论

1. 第二轮 8 条 + 第一轮 13 条：我这份独立判定表 **全部一致**；`48 passed` 也复现。
2. 我的 47 格对抗/边界里有 4 格不符：`1 2 3 人参与`/`1 2 5 人参与` 造出 `12`（D5）、
   `max>1M` 未整组清空（D2）、`1,25` 那格是我初判与契约冲突（契约内，但正说明 D1）。
3. 新缺陷里 **D1 最值得修**：`self` 合法却因 max 被吃小（全角千分位 `1，250` 或漏读一位 `12`）而整组丢失，
   最小复现 `随机点数 999，本轮最大点数 1，250` 与 `未抢购成功，随机点数 99，本轮最大点数 12`。
4. installer：**保留逻辑是对的**，11 个 `uninstalled-20261107-*` 实证；那条红是 iss 17:29 变更 + 旧断言只做字符串匹配，
   18:45 放宽断言后转绿，但护栏自此空转（D6），且拷贝失败无校验（静默丢数据风险）。
5. 5 条“既有问题”现只剩 2 条真的红（`console_api`、`soak`），且都与本轮无关；另 3 条已绿，
   其中 installer 的“绿”是测试被动放宽的结果，文档需更正。

未 commit、未 push。
