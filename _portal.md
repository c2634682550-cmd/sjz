# 卡密下载站（portal）交付说明

独立可部署的卡密兑换下载站：用户输入卡密 → 服务端校验 → 一次性令牌 → 临时签名地址下载脚本；
后台可发卡、管版本、看记录。代码全部在 `portal/`，未改动 `core/` 与 `web/index.html`。

---

## 1. 架构

```
浏览器 ──HTTPS──> nginx ──127.0.0.1:8000──> gunicorn ──> Flask 应用工厂
                                                   ├─ routes_public.py   用户侧：校验 / 令牌 / 下发
                                                   ├─ routes_admin.py    后台：发卡 / 版本 / 记录 / 管理
                                                   ├─ cards_service.py   发卡、续期、改绑、作废、判定
                                                   ├─ signing.py         P-256 适配（复用 core/license.py）
                                                   ├─ tokens.py          一次性下载令牌
                                                   ├─ ratelimit.py       IP / 卡密双维度限流 + 算术验证码
                                                   ├─ versions_service.py 上传校验 / 上架 / 强制更新
                                                   └─ stats.py           概览与 7 日趋势
                                                        │
                                              SQLite(WAL) + portal/var/storage
```

关键点：

- **文件不在 web 根下**。版本 zip 与批次清单都在 `portal/var/storage/`，Flask 只通过 `send_file` 在令牌兑换成功后下发；nginx 也对 `/storage|/var` 直接 404。
- **不新增第二套密码学**。签名/验签全部走 `core/license.py` 的 P-256（secp256r1 + SHA-256 + IEEE P1363 r||s）：
  `PUBLIC_KEY_B64`、`p256_verify`、`canonical_bytes`、`sign_blob`。`portal/signing.py` 只是适配层，
  启动时做“签名探针 + 公钥验签”自检，私钥与公钥不配对就停用发卡并给出中文原因。
  Ed25519 → P-256 迁移完成后，只要 `core/license.py` 的公钥常量更新，本站在同一套接口上继续工作。
- **10 万张卡不逐张签名**：每批一次 P-256 签名覆盖“批次清单文件 SHA256”，清单内每行是
  `HMAC-SHA256(pepper, code)`；校验时先验批次签名，再做哈希成员判定。续期/改绑/改限额只对单卡追加
  `dl-card` 修订签名。

### 目录

```
portal/
  app.py config.py run.py wsgi.py cli.py schema.sql
  routes_public.py routes_admin.py cards_service.py versions_service.py
  signing.py tokens.py ratelimit.py stats.py totp.py cardcode.py security.py db.py
  templates/   base/index/login/overview/cards/card_new/batch_codes/versions/logs/admins/security/error
  static/      portal.css portal.js favicon.svg
  tests/       conftest + test_public + test_admin + test_tokens + test_security + test_perf + test_ui
  deploy/      nginx.conf portal.service portal-supervisor.conf backup.sh restore.sh
  var/         运行期数据（secret_key、card_pepper、portal.db、storage/、logs/、exports/）
```

---

## 2. 数据库表结构

时间统一 Unix 秒（INTEGER）；SQLite 开 WAL，`foreign_keys=ON`。完整 DDL 见 `portal/schema.sql`。

| 表 | 作用 | 关键字段 |
|---|---|---|
| `admins` | 管理员 | `username`(UNIQUE)、`password_hash`(scrypt)、`role`(owner/staff)、`totp_secret`、`failed_count`、`locked_until` |
| `login_events` | 登录审计 | `ts`、`username`、`ip`、`ua`、`ok`、`reason` |
| `batches` | 发卡批次 | `count`、`days`(0=永久)、`tier`、`note`、`max_per_day`、`bind_device`、`manifest_file`、`manifest_sha256`、`sig_blob` |
| `cards` | 卡密 | `code_hash`(UNIQUE，HMAC-SHA256)、`code_mask`、`batch_id`、`expires_at`、`revoked`、`revoke_reason`、`amend_blob`、`amend_gen`、`bound_device`、`bind_device`、`max_per_day`、`day_key`、`day_count`、`total_downloads` |
| `versions` | 版本 | `version`(UNIQUE)、`sha256`、`size`、`filename`、`stored_path`、`force_update`、`enabled`、`downloads` |
| `tokens` | 一次性下载令牌 | `token_hash`(UNIQUE，sha256(token))、`card_id`、`version_id`、`session_id`、`expires_at`、`consumed_at` |
| `downloads` | 下载记录 | `ts`、`card_mask`、`ip`、`ua`、`version` |
| `attempts` | 校验尝试（限流/爆破） | `ts`、`ip`、`card_hash`、`ok`、`reason` |
| `audit` | 后台操作审计 | `admin_id`、`action`、`target`、`detail` |
| `export_tokens` | 一次性 CSV 导出令牌 | `token_hash`、`batch_id`、`expires_at`、`consumed_at` |

索引：`cards(batch_id)`、`cards(code_mask)`、`tokens(expires_at)`、`downloads(ts)`、`attempts(ip,ts)`、`attempts(card_hash,ts)`、`login_events(ts)`、`audit(ts)`。

### 迁移 Postgres 路径

- 时间字段已是 INTEGER 秒，直接映射 `bigint`；无 SQLite 方言函数。
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `bigserial primary key`；`INSERT ... lastrowid` 改为 `RETURNING id`。
- 布尔字段是 0/1 整数，迁移时可保留或换 `boolean` 并把比较写成 `= 0/1`。
- 原子限额更新 `UPDATE ... WHERE day_key=? AND day_count<max_per_day` 在 Postgres 同样成立（行级锁）。
- WAL 相关 PRAGMA 去掉；连接层换 `psycopg`，其余 DAO 不变。
- 备份/恢复命令换成 `pg_dump` / `psql`，目录结构（storage 与密钥文件）保持原样。

---

## 3. 接口清单

### 用户侧

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 单输入框 + 下载按钮页面（中文、离线、响应式） |
| POST | `/` | 卡密校验；成功返回页面内一次性下载链接与剩余次数 |
| GET | `/dl/<token>` | 兑换令牌并下发 zip；一次性、60 秒、绑会话 |
| GET | `/healthz` | `{"ok":true,"signer_ready":...}` |
| GET | `/api/integration/metrics` | 预留看板对接位（`X-Integration-Token`） |

### 后台（前缀 `/admin`，全部要求登录；写操作全部校验 CSRF）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/admin/login` | 强口令 + 可选 TOTP；失败计数与锁定 |
| POST | `/admin/logout` | 注销 |
| GET | `/admin/` | 概览：总卡/有效卡/今日下载/7 日趋势 |
| GET | `/admin/api/overview` | 概览 JSON |
| GET | `/admin/cards` | 卡密列表（掩码），按时间/卡密筛选、分页 |
| GET/POST | `/admin/cards/new` | 单张发卡 |
| POST | `/admin/cards/batch` | 批量发卡（数量/有效期/备注/档位/日限额/设备绑定） |
| GET | `/admin/cards/batch/<id>/csv` | 一次性明文 CSV（1 小时、单次） |
| POST | `/admin/cards/<id>/revoke` `/unrevoke` `/renew` `/rebind` `/limits` | 作废 / 解封 / 续期 / 改绑设备 / 改限额 |
| GET | `/admin/versions` | 版本列表（含历史） |
| POST | `/admin/versions/upload` | 上传 zip（流式限大小、SHA256、拒穿越条目） |
| POST | `/admin/versions/<id>/toggle` `/force` | 上下架 / 强制更新开关 |
| GET | `/admin/versions/<id>/file` | 管理员下载版本包（复核 SHA256） |
| GET | `/admin/logs` | 下载 / 发卡 / 登录记录，按时间与卡密筛选 |
| GET | `/admin/export/<kind>.csv` | 导出，`kind ∈ cards/downloads/logins/batches` 白名单 |
| GET | `/admin/admins` | 管理员列表（owner） |
| POST | `/admin/admins/create` `/admin/admins/<id>/delete` | 新建/删除管理员（owner） |
| GET/POST | `/admin/security` | 自助 TOTP 绑定 |

### 预留对接（抢购 / 数据看板）

`GET /api/integration/metrics`：

- 未配置 `integration_token` → `404`（默认关闭，避免裸奔）。
- 请求头 `X-Integration-Token` 不匹配 → `401`。
- 匹配 → `{"ok":true,"enabled":false,"overview":{...},"fields":{"pending_orders":null,"flash_sale_slots":null}}`。
- **字段协议未定，不硬编码**：对接时只替换 `data` 内容，鉴权与路径保持不变。

---

## 4. 部署步骤

> 占位：`DOMAIN=portal.example.com`，部署目录 `/opt/dafors`，服务用户 `portal`。

### 4.1 服务器准备（Ubuntu 22.04/24.04 示例）

```bash
sudo apt update && sudo apt install -y python3-venv nginx certbot python3-certbot-nginx
sudo useradd -r -s /sbin/nologin portal
sudo mkdir -p /opt/dafors && sudo chown portal:portal /opt/dafors
# 上传代码（举例）
sudo -u portal rsync -a --exclude '.venv' --exclude 'portal/var' ./ portal@server:/opt/dafors/
```

### 4.2 Python 环境与初始化

```bash
cd /opt/dafors
sudo -u portal python3 -m venv .venv
sudo -u portal .venv/bin/pip install -U pip
sudo -u portal .venv/bin/pip install flask gunicorn
# 初始化库 + 管理员（口令 ≥10 位；--totp 生成 TOTP，密钥只显示一次）
sudo -u portal .venv/bin/python -m portal.cli init-db
sudo -u portal .venv/bin/python -m portal.cli init-admin --username admin --role owner --totp
# 可选：命令行发一批卡
sudo -u portal .venv/bin/python -m portal.cli issue-batch --count 100 --days 365 --tier pro --out /tmp/cards.csv
```

`portal/config.json`（可选，缺省值见 `portal/config.py`）：

```json
{
  "site_name": "SJZ 卡密下载站",
  "trust_proxy": true,
  "https_only": true,
  "signing_key_file": "/opt/dafors/portal/var/keys/prod_signing_key_p256.txt",
  "integration_token": "",
  "max_upload_bytes": 209715200
}
```

生产务必替换签名私钥（见第 7 节），并保证 `portal/var` 权限 700/600。

### 4.3 nginx + 证书

```bash
sudo cp portal/deploy/nginx.conf /etc/nginx/sites-available/portal.conf
sudo sed -i 's/DOMAIN/portal.example.com/g' /etc/nginx/sites-available/portal.conf
sudo ln -s /etc/nginx/sites-available/portal.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 首次签发证书（需要 80 端口可达、DNS 已指向本机）
sudo certbot --nginx -d portal.example.com --agree-tos -m admin@example.com
sudo certbot renew --dry-run
```

配置要点：80 → 301 HTTPS；TLS1.2/1.3；HSTS；`client_max_body_size 210m`；`X-Forwarded-For/Proto`；
静态目录直出；`^/(storage|var|portal/var)/` 显式 404。

### 4.4 进程守护

systemd（二选一）：

```bash
sudo cp portal/deploy/portal.service /etc/systemd/system/portal.service
sudo systemctl daemon-reload
sudo systemctl enable --now portal
sudo systemctl status portal
journalctl -u portal -f
```

supervisor（二选一）：

```bash
sudo cp portal/deploy/portal-supervisor.conf /etc/supervisor/conf.d/portal.conf
sudo supervisorctl reread && sudo supervisorctl update
```

### 4.5 备份与恢复

```bash
# 备份（内含一致快照 portal.db + secret_key + card_pepper + storage）
sudo -u portal .venv/bin/python -m portal.cli backup --out /var/backups/portal/portal-$(date +%F).zip
# 每日 03:00 自动备份并保留 14 天
echo '0 3 * * * /opt/dafors/portal/deploy/backup.sh' | sudo tee /etc/cron.d/portal-backup

# 恢复（先停服务；脚本会自动留一份 var 回滚点）
sudo systemctl stop portal
sudo /opt/dafors/portal/deploy/restore.sh /var/backups/portal/portal-2026-11-07.zip
sudo systemctl start portal
```

恢复必须成对恢复 `portal.db`、`secret_key`、`card_pepper`、`storage/`：缺 pepper 会让旧卡哈希全部失效，缺 storage 会让批次清单验签失败。

### 4.6 本地（Windows 开发）

```powershell
cd C:\Users\26346\Desktop\project\dafors
.\.venv\Scripts\python.exe -m portal.run          # http://127.0.0.1:8080
.\.venv\Scripts\python.exe -m pytest portal/tests -q
```

---

## 5. 安全措施与实测

| 要求 | 实现 | 实测（自动化用例） |
|---|---|---|
| 卡密不明文入库 | 只存 `HMAC-SHA256(pepper, code)` 与 `SJZ-前4-****-****-后4`；明文仅在发卡响应/一次性 CSV 出现 | `test_card_plaintext_never_stored`：遍历 `cards` 全字段、并扫描 `portal.db`/`-wal` 原始字节，均无明文；`test_admin_page_shows_mask_only` 后台页面无完整卡密 |
| 令牌一次性/限时/绑会话 | `nonce.hmac` 令牌，DB 只存 `sha256(token)`；60s TTL；原子 `UPDATE ... WHERE consumed_at IS NULL AND expires_at>?` | `test_token_is_single_use`（重放 410）、`test_token_expires_after_ttl`（真实 1s TTL 超时 410）、`test_token_bound_to_session`（换会话 403）、`test_concurrent_redeem_only_one_wins`（4 并发仅 1 个 200） |
| 双维度限流 + 爆破锁定 | `attempts` 按 IP/卡密计数；5 次失败要求算术验证码；同卡 5 次失败锁 10 分钟 | `test_public.py`：IP 429、验证码出现与校验、卡密锁定 429、8 线程并发只 1 成功 |
| 管理接口权限/CSRF/上传白名单 | 未登录 JSON 401 + 页面跳转；staff 越权 403；所有 `/admin` 写操作校验 CSRF；仅 `.zip` + 200MB | `test_admin.py`：401/403/CSRF/大小/类型全覆盖 |
| 路径穿越（3 处） | 上传包条目、下载参数、导出参数 | `test_inspect_zip_rejects_traversal_entries`（`../` 与绝对路径均拒）、`test_safe_storage_path_blocks_escape`、`test_storage_dir_not_web_reachable`（6 种猜测路径 403/404）、`/admin/export/unknown.csv?type=../../etc` → 400 |
| 全站 HTTPS + 强制跳转 | nginx 80→301、TLS1.2/1.3、HSTS；应用 `https_only` 时 Cookie 加 Secure | `test_https_mode_cookie_and_hsts`；`test_session_cookie_flags`（HttpOnly + SameSite=Lax） |
| 日志脱敏 | `RedactFilter`：卡密→掩码、64hex→[KEY]、≥32 位串→[TOKEN]；口令/密钥不进日志 | `test_redact_function_masks_card_and_key`、`test_logs_do_not_leak_plaintext_card`（真实日志文件中无明文卡密/pepper） |
| 篡改检测 | 批次清单 SHA256 + P-256 验签；单卡修订 `dl-card` 验签 | `test_tampered_batch_manifest_rejected`、`test_tampered_amend_rejected`（均 403「签名无效」） |
| 其他 | CSP `default-src 'self'`、XFO DENY、nosniff、Referrer-Policy、`/admin`、`/dl/` no-store | `test_security_headers_on_public_page` |

---

## 6. 验证结果（自动化）

命令：`.\.venv\Scripts\python.exe -m pytest portal/tests -q`

- **55 passed**（37 基础 + 14 安全 + 2 性能 + 2 UI），总耗时 28.94s。
- 功能矩阵：正确卡可下；错误/过期/封禁/设备不匹配/格式错误分别给出中文原因；无版本 503；
  IP 限流 429；卡密锁定 429；验证码生效；并发 8 线程仅 1 次成功。
- 令牌矩阵：重放 410、超时 410、换会话 403、乱码 404。
- 直链：`/storage/...`、`/var/storage/...`、`/portal/var/storage/...`、`/static/...`、`/`+相对路径、`/downloads/<文件名>` 全部 403/404。
- 后台：未登录 `/admin/api/overview` 401；staff 访问 owner 接口 403；无 CSRF 400；含 `../` 的 zip 400；非 zip 400；超限 413。
- 10 万卡：插入 **4.07s（24,580 张/秒）**；单卡哈希查询 2000 次 **avg 0.016ms / p95 0.028ms / max 0.189ms**。
- 下载校验 `POST /`：120 次 **avg 8.70ms / p95 12.71ms / max 26.71ms**（含整页渲染，Flask 测试客户端）。
- 布局（无头 Edge，headless）：1366×800 与 390×844 两个视口 × 8 个页面（`/`、`/admin/login`、概览、卡密、发卡、版本、记录、安全）共 16 组，
  `scrollWidth == innerWidth`，**0 横向溢出**；控制台 **0 error**。

---

## 7. 还需要你准备

1. **域名**：一个已备案/可解析的域名，A 记录指向服务器；用于 nginx `server_name` 与证书。
2. **服务器**：Linux VPS（建议 2C2G 起），开放 80/443；`/opt/dafors` 部署目录与服务用户 `portal`。
3. **证书**：可用 Let's Encrypt（`certbot --nginx`，需 80 端口可达）；若已有商业证书，替换 nginx 里的 `ssl_certificate` 路径。
4. **正式 P-256 密钥对**：当前默认使用项目现有的 `tools/license_keys/dev_signing_key_p256.txt`（开发私钥）。
   上线前请生成独立的正式私钥，并确保其对应公钥与 `core/license.py` 的 `PUBLIC_KEY_B64` 一致
   （`Signer` 启动自检会拦截不配对的情况）；私钥只放服务器 `portal/var/keys/`，权限 600，永不入库、不进日志。
5. **管理员口令 + TOTP**：用 `portal.cli init-admin --totp` 生成，TOTP 密钥只显示一次，请立即录入验证器。
6. **备份落点**：确认 `/var/backups/portal` 的磁盘/远端同步策略（异地备份更稳）。
7. **对接字段**：抢购/看板接口目前是占位外壳；等字段定了，改 `/api/integration/metrics` 的 `data` 部分即可，鉴权头与路径不用动。

---

## 8. 纪律确认

- 未执行 `git commit` / `git push`。
- 未修改 `core/` 任何文件与 `web/index.html`。
- 新增代码全部位于 `portal/`，部署件位于 `portal/deploy/`。
- 验证全部通过本地自动化测试（pytest + 无头 Edge），没有向公网部署，没有弹出可见窗口。
