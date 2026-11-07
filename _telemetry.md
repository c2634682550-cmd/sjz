# 抢购遥测系统（收集 + 看板）

给「卖出去的卡」做的一套遥测：客户端只把**抢购结果**这类结构化数据增量上报，
作者在自己机器上开一个后台，按卡密看每一台机器的抢购情况。

新增文件（没有改 `core/engine.py`、`core/config.py`、`web/index.html`、
`tools/license_server.py` 的任何一行）：

| 文件 | 作用 |
| --- | --- |
| `tools/telemetry_agent.py` | 客户端上报器：tail 台账 → 白名单裁剪 → 批量签名上报 |
| `tools/telemetry_server.py` | 收集服务：SQLite、验签、限流、管理接口、CSV/清理命令 |
| `tools/telemetry_dashboard.html` | 看板：单文件、零外链、内联 SVG 图表 |
| `tools/telemetry_selftest.py` | 自检脚本（42 项，见「验证结果」） |

---

## 一、架构

```
core/records.py ──写──▶ data/records.jsonl
                              │  (只读，字节游标增量)
                              ▼
                    tools/telemetry_agent.py
                     ├─ 白名单裁剪（10 个字段）
                     ├─ 本地缓冲 data/telemetry/telemetry_queue.jsonl
                     └─ HMAC 签名 ──POST /api/telemetry/batch──▶ tools/telemetry_server.py
                                                                  ├─ 验签 / 许可有效性 / 限流 / schema
                                                                  ├─ SQLite: events + daily_agg
                                                                  ├─ GET /api/admin/*（admin token）
                                                                  └─ GET /  → telemetry_dashboard.html
```

- **游标**：`data/telemetry/telemetry_cursor.json` 记 `path / offset / last_line_sha256 / size`。
  重启从断点续读；台账被轮转（文件变小）或「最后一行 hash 对不上」时从 0 重读，
  重复事件由服务端 `event_id` 唯一索引吃掉，所以重读不会重复入库、也不会丢。
- **缓冲**：发送失败写 `telemetry_queue.jsonl`，指数退避（15s 起、上限 30 分钟）。
  4xx 的「永久拒收」（schema/许可）转存 `telemetry_rejected.jsonl` 留档，不刷限流。
- **签名**：`key = core.license._ledger_key(payload)`（嵌入密钥 + 许可里的稳定指纹组件）。
  服务端用许可库里的 `fp_vec` 重算出同一把 key，对**原始请求体字节**做 HMAC-SHA256。
  所以换一台机器、换一份许可都签不出来。
- **许可识别**：客户端只发 `sha256("lic:" + lic_id)[:32]`，服务端用许可库反查
  `lic_id` 拿掩码/到期/吊销状态。明文卡密既不上报也不入库（只有管理接口
  「显示完整」按需返回，且要二次确认 + 服务端记日志）。

---

## 二、字段字典

### 2.1 上报请求（batch）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `license_id` | string | `sha256("lic:"+lic_id)` 前 32 位十六进制 |
| `machine` | string | `sha256("fp:"+fp)` 前 16 位十六进制 |
| `client_version` | string ≤20 | 客户端版本（默认 `0.1.0`） |
| `sent_at` | number | 发送时间（epoch 秒），服务端只做格式校验 |
| `events` | array | 单批 1–200 条 |

### 2.2 事件（events[]）

| 字段 | 必填 | 类型 / 上限 | 说明 |
| --- | --- | --- | --- |
| `event_id` | 是 | 8–64 位十六进制 | 内容派生 id（台账行 sha256），幂等键 |
| `occurred_at` | 是 | number，1 – 1e11 | 发生时间（epoch 秒，保留 3 位小数） |
| `tier` | 是 | string ≤16 | 品阶：台账 `tier/craft/grade/rarity`；没有则按名字正则取「极品/优品」；都没有 = `未标注` |
| `name` | 是 | string ≤64 | 商品名；开脱敏后为「品阶｜价格区间」 |
| `price` | 否 | number 0 – 1e7 | 成交价（元）；失败可为 null |
| `result` | 是 | `hit / miss / skip` | 成功 / 失败 / 跳过（台账 `no_money`、`over_budget`、`aborted` 归入 miss） |
| `skew_ms` | 否 | 整数 -5000 – 5000 | 卡点误差毫秒；台账没有这一项时为 null |
| `skip_reason` | 否 | string ≤24 | 仅 `result=skip` 允许，如「来不及」 |

**未知字段一律整批 400 拒收**（白名单是这个接口的第一道边界）。

### 2.3 客户端配置（可选）

`config.json` 里加一段 `telemetry` 即可（不加也能跑，全部有默认值；上报器只读这一段）：

```json
"telemetry": {
  "server_url": "http://127.0.0.1:8091",
  "records_path": "data/records.jsonl",
  "interval_s": 30,
  "batch_size": 100,
  "client_version": "0.1.0",
  "desensitize_names": true
}
```

命令行参数（`--server / --records / --interval / --batch-size / --desensitize /
--client-version / --state-dir / --license`）优先于配置；环境变量
`TELEMETRY_URL`、`TELEMETRY_STATE_DIR` 次之。

---

## 三、接口清单

### 3.1 `POST /api/telemetry/batch`

头：`X-Telemetry-Signature: <hex>`、`X-Telemetry-License: <license_id>`。
校验顺序：schema → 许可存在/未吊销/未到期 → HMAC → 限流（每 license 每分钟 20 批）→ 幂等入库。

```bash
curl -X POST http://127.0.0.1:8091/api/telemetry/batch \
  -H "Content-Type: application/json" \
  -H "X-Telemetry-Signature: <hmac hex>" \
  -H "X-Telemetry-License: 3f2a…" \
  -d '{"license_id":"3f2a…","machine":"a1b2c3d4e5f60718","client_version":"0.1.0","sent_at":1794006123.5,
       "events":[{"event_id":"e12ab34cd56ef7890","occurred_at":1794006100.123,"tier":"传说","name":"战术手电","price":1860,"result":"hit","skew_ms":37,"skip_reason":""}]}'
```

```json
{"ok": true, "received": 1, "inserted": 1, "duplicates": 0}
```

| 状态 | 含义 |
| --- | --- |
| 200 | 收到；`inserted=0` 表示整批都是重复（重复上报也算成功） |
| 400 | schema 错（未知字段/缺字段/类型/超长/单批 >200） |
| 401 | 缺签名 |
| 403 | 许可未登记 / 已吊销 / 已到期 / 签名不匹配 / 头体 license 不一致 |
| 413 | 请求体 >512KB |
| 429 | 超限，带 `Retry-After` 和中文原因 |

### 3.2 管理接口（全部要 token）

头：`X-Admin-Token: <TOKEN>` 或 `Authorization: Bearer <TOKEN>`；缺失/错误一律 401。

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/overview` | 在线台数（10 分钟）、今日成交/花费/平均卡点误差、各品阶分布 |
| `GET /api/admin/trend?bucket=hour\|day` | 24 个小时桶或 30 个天桶（成交/失败/跳过/花费） |
| `GET /api/admin/users?q=&from=&to=&sort=hits\|spent\|last_seen\|fails&order=desc` | 用户列表（卡密掩码、机器码、各品阶成交、花费、最后在线、版本、在线状态） |
| `GET /api/admin/user/<lic_hash>` | 最近 50 条明细 + 30 天按天趋势 |
| `GET /api/admin/reveal?id=<lic_hash>` | 完整卡密；额外要求 `X-Confirm: yes`（缺 = 428） |
| `GET /api/admin/export?scope=overview\|users\|user&id=<lic_hash>` | CSV（中文表头、UTF-8 BOM） |

```bash
curl -H "X-Admin-Token: $T" http://127.0.0.1:8091/api/admin/overview
# {"server_time":1794006800.1,"today":"2026-11-07","online":3,"today_events":192,
#  "today_hits":120,"today_spent":222440.0,"today_skew_avg_ms":74.6,
#  "tiers":[{"tier":"传说","hits":38,"fails":0,"skips":0,"spent":66800.0}, …]}
```

`GET /` 直接返回看板 HTML（同源，不引任何外网资源）。

---

## 四、部署命令

### 4.1 服务端（作者机器）

```powershell
# 生成一个随机令牌（只存环境变量，不写进代码/仓库）
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"

$env:TELEMETRY_ADMIN_TOKEN = "<上一步生成的值>"
.\.venv\Scripts\python.exe tools\telemetry_server.py serve --port 8091
# 或者不使用环境变量：
# .\.venv\Scripts\python.exe tools\telemetry_server.py serve --port 8091 --admin-token <TOKEN>
```

打开 `http://127.0.0.1:8091/`，把同一个 token 填进登录框即可。
默认只监听 `127.0.0.1`；要给外部机器用，见「待办」里的 HTTPS/反代。

数据落在 `data/telemetry/telemetry.db`；许可只读 `tools/license.db`
（`--license-db` 可改路径，库不存在会直接报错退出）。

### 4.2 客户端（每一台卖出去的机器）

```powershell
# 前台常驻（能看到日志）
.\.venv\Scripts\python.exe tools\telemetry_agent.py --interval 30

# 以后由启动器拉起的一行（无控制台窗口；工作目录 = 仓库根）
.\.venv\Scripts\pythonw.exe tools\telemetry_agent.py --interval 30
```

启动器那一侧现在**没有改**：只要它按上面的 argv + cwd 拉起即可。
如果收集服务不在本机，给每台机器配 `TELEMETRY_URL`（或 config 的
`telemetry.server_url`），例如 `https://telemetry.example.com`。

---

## 五、数据保留与清理

- 默认保留 **180 天**（`DEFAULT_RETENTION_DAYS`）。
- 清理必须显式执行，服务不会自动删数据：

```powershell
# 删 180 天前的事件；先归档成 NDJSON（都在 data/telemetry/ 里）
.\.venv\Scripts\python.exe tools\telemetry_server.py purge --days 180 --archive archive/2026-05.ndjson

# 聚合表被误删/口径改过时，按原始事件重建
.\.venv\Scripts\python.exe tools\telemetry_server.py rebuild-agg

# 导出（同样只能写进 data/telemetry/）
.\.venv\Scripts\python.exe tools\telemetry_server.py export --scope users --out exports/users.csv
```

`--out` / `--archive` 只接受**相对路径**且必须落在 `data/telemetry/` 下；
绝对路径、`..`、软链逃逸全部拒绝（报错「导出路径被拒 / 归档路径被拒」）。

---

## 六、隐私边界（绝不上报）

客户端只发第二节表里的 10 个字段。以下内容**不会**出现在请求体、日志或数据库里：

- 游戏账号、密码、登录态；
- 截图（文件或内容）、截图路径；
- 进程列表、窗口标题、本机安装路径、用户名；
- 明文卡密（只发 `lic_id` 的哈希；卡密本体只在管理接口按需 reveal）；
- IP 之外的任何个人信息（上报器不取 IP；服务端也不把 remote_addr 写进库）。

可选脱敏：`"desensitize_names": true` 后商品名变成「品阶｜价格区间」
（如 `传说｜¥1000-2000`），连商品名都不落服务端。

真实账号/密码在台账里本来就没有，上报器也没有读取它们的代码路径。

---

## 七、验证结果（42 项全部通过）

`.\.venv\Scripts\python.exe tools\telemetry_selftest.py`（临时目录跑，不写项目数据、不弹窗、不动鼠标）

| 维度 | 结果（数字来自实际运行） |
| --- | --- |
| 自检项数 | **42 / 42 通过** |
| 幂等 | 同一批重发两次：库 **600 → 600**，`inserted=0`；重读整段台账也只新增 0 |
| 验签 | 空签名 **401**；篡改 payload **403 签名不匹配**；篡改签名 **403**；过期许可 **403 已到期**；未登记许可 **403** |
| 限流 | 前 20 次 200，第 21 次 **429**：`每 license 每分钟最多 20 批，请 59s 后再试` |
| schema | 未知字段 `screenshot` / 缺 `result` / `price="abc"` / name 100 字 / 201 条 → 全部 **400** 且原因可读 |
| 鉴权 | overview、users、export 无 token / 错 token → **401** |
| 数据正确性 | 3 个假用户 × 200 条 = **600** 条；今日成交 **363 件**、花费 **¥624,220**；品阶 **稀有 98 / 传说 90 / 普通 89 / 史诗 86**，与源 jsonl 逐项一致；3 台用户成交数/花费/机器码/事件数全部一致；在线 **3**；单用户明细 **50** 条上限 |
| 断网与重启 | 断网跑一轮：缓冲 **40** 条、库 0 条；恢复后：缓冲 0、库 **40** 条；再跑一轮：库仍 40、游标 **6997 = 文件 6997** |
| 性能 | 1 万条导入 **720 ms / 1028 ms**（两次运行，约 1.0–1.4 万条/秒）；`overview` 20 次平均 **1.5 ms** |
| 路径穿越 | `../evil.csv`、`..\evil.csv`、`exports/../../evil.csv`、`C:\Windows\evil.csv` 全部被拒；CLI 同样拒绝 |
| 保留策略 | 插入 200 天前事件后 `purge --days 180`：**10642 → 10641**，归档文件已写 |
| CSV | 总览 15 行、用户 7 行、单用户 205 行；均为中文表头 + BOM |
| 看板 | 1366×768 与 1920×1080 下 `scrollWidth == innerWidth`（**无横向溢出**）；首页 34KB，**无任何 http(s) 外链** |

看板布局（Edge 无头实拍，1366×768 / 1920×1080 / 详情抽屉）：

- `data/telemetry/screenshots/dashboard_1366x768.png`
- `data/telemetry/screenshots/dashboard_1920x1080.png`
- `data/telemetry/screenshots/dashboard_detail_1920x1080.png`

- 顶部 4 张卡片：在线台数、今日成交件数、今日总花费、平均卡点误差；
- 品阶分布条（今日），每个品阶显示成交件数 + 花费 + 占比条；
- 趋势图：小时/天切换，成交/失败/跳过堆叠柱 + 花费折线（右轴）；
- 用户表：卡密掩码（前 4 后 4）、机器码短哈希、各品阶成交 chips、成交、总花费、
  卡点、最后在线、版本；支持搜索/时间范围/排序；超长内容省略号 + 悬浮全文；
- 点击行 → 右侧抽屉：3 张小结卡片、30 天条形趋势、
  时间轴「`07:50:23 · 信号干扰器 · 史诗 · ¥3,100 · 成功 · 卡点误差 +208ms`」，
  成功绿 / 失败红 / 跳过黄，跳过单独一行「跳过原因：窗口关闭」；
- 卡密「显示完整」需输入「确认」+ 再弹一次 confirm，服务端还要 `X-Confirm: yes` 并记日志。

---

## 八、已知限制与待办

1. **卡点误差目前多数是 null**。台账 `data/records.jsonl` 还没有这项
   （引擎的 `precision.offset_s` 没有落盘）。上报器已支持
   `skew_ms / offset_ms / delta_ms / error_ms` 四种键名，服务端与看板对 null 显示「—」。
   下一步：在引擎写台账那处补一个 `offset_ms`（这次按要求没动 `core/`）。
2. **HTTPS / 反代**：现在只监听 `127.0.0.1`，远程机器要靠 SSH 隧道，
   或在前面加 nginx/Caddy（TLS + 只暴露 `/api/telemetry/batch`），
   管理接口建议再套一层 Basic Auth / IP 白名单。
3. **启动器拉起**：客户端那一行命令已定稿
   （`pythonw tools/telemetry_agent.py --interval 30`），启动器文件按要求没改。
4. **多作者分流**：当前一台服务 = 一个作者。要分作者就在 `events` 加
   `author_id`（或每个作者独立 db），接口加过滤参数即可，schema 已预留扩展位。
5. **许可 P-256 迁移**：另一路在改 `license_server` 的签发/公钥，这边只读
   `licenses` 表的 `lic_id / fp / fp_vec / tier / exp / revoked`，不参与签发；
   agent 的本地验签会随 `PUBLIC_KEY_B64` 换成真公钥后自动生效（现在失败只告警）。
6. **大数据量**：`events` 目前单表 + 两个索引。到百万级再考虑按月分表/归档库；
   `overview` 已走 `day` 索引，1.5ms 量级。
7. **看板登录**：token 存在 sessionStorage，刷新要重输；若长期挂在反代后，
   可以把登录换成反代侧认证，页面不再收 token。
