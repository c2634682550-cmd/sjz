# R10 许可与打包完整性专项审查

- 审查对象：`C:\Users\26346\Desktop\project\dafors`（`core/license.py`、`core/selfcheck.py`、`tools/license_server.py`、`tools/license_admin.py`、`sjz.spec`、`sjz-onefile.spec`、`desktop.py --doctor`、`dist/SJZ-Control`）
- 审查范围：**仅授权/许可校验 + 打包产物完整性问题**。不评业务功能、不评 OCR 准确率、不改任何代码。
- 方法：静态审阅 + 攻击者视角实跑（受限于"不改系统时间、不改用户环境、不动光标、不开可见窗口"，所有写操作都落在 `%TEMP%`，通过 `SZJ_LICENSE_DIR` / `SZJ_NO_REGISTRY` 隔离；服务器只起在 `127.0.0.1`；`--doctor` 在临时副本上运行）。
- 结论：**当前形态不可作为付费产品交付**。许可模块整体未接线（P0），参考许可服务器所有签发端点恒 500 且**把私钥回显给请求方**（P0），本地试用与离线账本均可零成本重置/篡改（P1）。

---

## 一、问题清单

| 编号 | 问题 | 级别 | 能否白嫖/无限试用 |
|---|---|---|---|
| L-01 | 许可模块未接入任何运行路径，拷贝即用 | P0 | 完全白嫖 |
| L-02 | 参考许可服务器所有签发端点恒 500（私钥 hex 被当文件路径） | P0 | 无法售卖（无人能激活） |
| L-02B | 上述 500 的响应体把**私钥种子**原样回显给任意请求方 | P0 | 主密钥泄露=整条许可链作废 |
| L-03 | `/api/license/trial` 盲信客户端 `fp`/`fp_vec`，可无限领取 3 天许可 | P1 | 无限试用 |
| L-04 | 本地试用可重置（删文件+注册表、`SZJ_*` 环境开关、`hours` 可改、不校验指纹） | P1 | 无限试用 |
| L-05 | 时间账本明文可改：`NaN` 绕过租期，删账本+回拨墙钟复活过期许可 | P1 | 无限期离线/复活 |
| L-06 | 吊销机制实际无效：客户端无拉取调用方、吊销列表不验签、删除文件即失效、代数可被写死 | P1 | 吊销失效 |
| L-07 | 指纹"许可里存几个组件"决定阈值，单组件许可 100% 通过 | P2 | 换机可用/可爆破 |
| L-08 | 克隆/同型号 sysprep 部署仅丢 25 分 → 75% 通过，一证多机 | P2 | 一证多机 |
| L-09 | 公钥不是不可替换落点：`public_key_b64` 参数旁路、PYZ 可重建、开发私钥在仓库 | P2 | 可自签永久证 |
| L-10 | 打包产物无完整性校验：DLL/模型/web 可替换（DLL 可执行代码） | P2 | 可整体重打包分发 |
| L-11 | `refresh` 每次把 exp 重置为 `now + 原天数` → 一次购买可永久续 | P2 | 无限续期 |
| L-12 | 签名 payload 字段无类型/范围校验，`grace_h=null` 直接抛未捕获异常 | P3 | 启动崩溃/拒绝服务 |
| L-13 | `--doctor` 可靠性：退出码恒 0、`context` 假阳性、许可状态不推进账本、字体未入包、WebView2 结论与兜底矛盾 | P3 | 误导交付验收 |
| L-14 | spec 用 `os.getcwd()` 解析资源 → 换目录构建会静默丢资源 | P3 | 残缺包 |
| L-15 | DLL 候选路径首项与 spec 实际位置不符，可能误加载根目录同名 DLL | P3 | 劫持面 |

---

## 二、详述

### L-01（P0）许可模块未接入任何运行路径

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L560-L582) 提供 `check()`，注释写明"启动时调用一次即可"。
- [desktop.py](file:///C:/Users/26346/Desktop/project/dafors/desktop.py#L284-L320) `main()` 从参数解析到起服务、开窗口、装热键，**没有任何 `license.check()` / `trial_check()`**。
- [server.py](file:///C:/Users/26346/Desktop/project/dafors/core/server.py) 无 `license` 引用、无许可路由。
- 全仓 `license` 只出现在 `core/license.py`、`core/selfcheck.py`、`tests/*`、`tools/*`。

**复现步骤（代码静态确认 + `--doctor` 实测；未启动图形界面）**
1. 清空 `%LOCALAPPDATA%\SJZ-Control`（等价新机：无 license.json / state.json / trial.json）。
2. 直接双击 `dist\SJZ-Control\SJZ-Control.exe`：`main()` 里没有任何许可分支，服务照常绑定 127.0.0.1:8777、前端照常加载。
3. 反向对照：临时副本上跑 `SJZ-Control.exe --doctor`，输出 `[许可] 状态 = none  ok=False`，进程退出码仍是 `0`（见 L-13）。

**影响**
- 买家拿到整个 `dist\SJZ-Control` 目录，复制给任意人即可用；卡密、试用、吊销全部是摆设。等于零收入保护。
- `--doctor` 会显示"许可 none / ok=False"，但程序照跑，进一步说明该判定没有消费者。

**加固方向**
1. 在 `desktop.main()` 里、`_single_instance()` 之前加唯一入口 `gate = license.ensure()`：`none→trial_begin/trial_evaluate`，`expired/fp_mismatch/revoked/need_online/clock_tamper → 只显示激活窗口或直接退出`，并且**任何返回 True 的分支都必须带 `lic_id`**。
2. 服务端线程启动 `serve()`、热键 `install_hotkeys()`、调度器 `_sched_start()` 全部挪到 gate 之后；`server.py` 的写操作类接口加进程内 `_licensed` 断言（防绕过 UI 直接打 API）。
3. 加一条 CI 断言：`grep -R "license.check\|license.ensure" desktop.py core/server.py` 必须非空，否则测试失败（防止以后再次脱钩）。

---

### L-02（P0）参考许可服务器所有签发端点恒 500

**位置**
- [license_server.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_server.py#L103-L106) `_pack()` → `sign(payload, S.seed)`。
- [license_admin.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_admin.py#L75-L78) `sign(payload, key_file)` 的第一个动作是 `open(key_file)`。
- `S.seed` 在 `main()` 里是**十六进制私钥串**，不是路径。

**复现步骤**
```powershell
.venv\Scripts\python.exe tools\license_server.py --db $env:TEMP\t.db --port 18090 --key tools\license_keys\dev_signing_key.txt
# 另开一个 shell
curl -X POST http://127.0.0.1:18090/api/license/trial -H "Content-Type: application/json" -d '{"fp":"x","fp_vec":{"mac":"y"}}'
```
实测返回：
```
HTTP 500 {"error": "FileNotFoundError: [Errno 2] No such file or directory: '<完整 64 hex 私钥种子>'" }
```
（`activate` / `refresh` / `trial` 三个端点共用 `_pack()`，全部 500；只有 `/api/license/revocations` 不经过 `sign()`，正常。）

> 同上请求的响应体还包含私钥种子，见 **L-02B**（同一行日志即证据）。

**另一处并发缺陷（同文件同链路）**
- [license_admin.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_admin.py#L45-L48) `db_open()` 用默认 `sqlite3.connect(path)`（`check_same_thread=True`），而 [license_server.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_server.py#L221) 用 `ThreadingHTTPServer`。
- 实测：先修 `_pack()` 后立刻暴露 `ProgrammingError: SQLite objects created in a thread can only be used in that same thread.`
- 即：**两个独立缺陷叠加，参考服务器上线后 100% 签不出证**。

**影响**
- 无法激活、无法续期、无法申请试用 → 卖出去也收不到钱；同时把 L-03 的"无限试用"暂时挡住了（修完 L-02 立刻暴露）。
- 属于交付阻断，必须先修。

**加固方向**
- `_pack()` 改为 `lic.sign_blob(payload, S.seed)`（`S.seed` 已是 hex，语义一致）。
- `db_open()` 用 `sqlite3.connect(path, check_same_thread=False)` + `_lock`（已有锁）；或改 `HTTPServer` 单线程并每次请求内开连接。
- 启动时自检：`POST /api/license/_selftest` 走一遍 `_make_license → _pack → verify_blob`，失败直接拒绝启动。

---

### L-02B（P0）500 响应体把私钥种子回显给任意请求方

**位置**
- [license_server.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_server.py#L202-L206)：`except Exception as e: return self._send(500, {"error": f"{type(e).__name__}: {e}"})`。
- `FileNotFoundError` 的消息里带着**被误当路径的完整私钥十六进制串**（64 hex = 32 字节种子）。

**复现步骤（已实跑，响应体已截断，实际是完整 64 hex）**
```powershell
curl -X POST http://LICENSE_HOST:8090/api/license/trial -H "Content-Type: application/json" -d '{"fp":"x","fp_vec":{"mac":"y"}}'
```
```
HTTP 500
{"error": "FileNotFoundError: [Errno 2] No such file or directory: '<完整 64 hex 私钥种子>'"}
```
右引号里的字符串就是 `dev_signing_key.txt` 的种子；用 `ed25519_ref.publickey(bytes.fromhex(...))` 即可复现出 `PUBLIC_KEY_B64`。

**影响**
- 部署到公网后，**任何一个扫描器/路人**发一次空请求就能拿到签发私钥 → 可自签任意 `exp=None` 永久证、可伪造吊销列表、可让整条许可链永久作废且无法通过换 key 挽回（已售出的客户端仍信任旧公钥）。
- 这比 L-09 严重得多：L-09 需要拿到安装包并逆向，这里只需一个 POST。

**加固方向**
1. 500 一律只回 `{"error":"internal"}` + 服务端日志记堆栈（不回显 `str(e)`）。
2. 立即把当前 dev 私钥视作**已泄露**：轮换新密钥对，旧公钥从客户端移除；已售客户走强制升级。
3. 私钥不要以"路径"形式在内存里传；`Store.seed` 改名 `seed_hex` 并做类型断言，避免再被 `open()`。

---

### L-03（P1）试用接口可无限重复领取

**位置**
- [license_server.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_server.py#L153-L167)：`fp` 与 `fp_vec` 都由客户端提供，服务端只用 `fp` 查 `trials` 去重，**不校验 `fp` 是不是 `fp_vec` 的哈希、也不校验 `fp_vec` 是否真来自硬件**。
- `_make_license` 直接把客户端 `vec` 原样签进许可证。

**复现步骤（已实跑）**
1. 用最小修复（仅让服务端能签发）起一个 loopback 服务。
2. 同一份 `fp_vec`（模拟本机真实硬件），每次换一个随机 `fp` 调用 `/api/license/trial`。

实测输出：
```
第1次 200 lic=lic-b65c8beb1c9346ce 有效=259200s trial=True
第2次 200 lic=lic-415925a142834ca9 有效=259200s trial=True
第3次 200 lic=lic-c5c1ea728f7944ad 有效=259200s trial=True
```
三次全部成功，每次都拿到一条绑定**攻击者自己上报的指纹**（可以是本机真实指纹）的 3 天试用证。

**影响**
- 试用期结束 → 删 `license.json`，换个随机 `fp` 再领一条 → 永久试用。
- 吊销试用只能按 `fp_ids`，而 `fp` 是攻击者随便填的 → 禁用无效。

**加固方向**
- 服务端不信任客户端 `fp`：改为客户端上报**原始组件**，服务端算 hash；或至少在服务端断言 `fp == sha256("...|".join(vec))` 形式一致性。
- `trials` 表按 `fp_vec` 的稳定子集（如 machine_guid+bios_uuid 的 hash）+ IP + 时间窗去重；加频率限制（每 IP/每 24h 上限）。
- 试用改为"必须先给手机号/邮箱/卡密前缀"才签发，单靠设备指纹在客户端可伪造的前提下不成立。

---

### L-04（P1）本地试用可零成本重置/延长

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L84-L97) `SZJ_LICENSE_DIR` 环境变量直接改整个许可目录。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L680-L695) `SZJ_NO_REGISTRY=1` 关掉注册表镜像；且写入条件是 `if reg_start is None`，用户预置 `trial_start=0` 会被 `if x` 过滤成"没有"。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L710-L724) `started = min(文件, 注册表) or now`。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L733) `hours = float(rec.get("hours"))` —— **时长来自本地文件**。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L742-L746) 指纹只算 `fp_score` 用于显示，**不参与判定**。

**复现步骤（已实跑）**

| 手法 | 结果 |
|---|---|
| 删除 `%LOCALAPPDATA%\SJZ-Control\trial.json` | 注册表还在，未重置 |
| 再删 `HKCU\Software\SJZ-Control\trial_start` | 重置成功，`started_wall` 回到"现在" |
| 设 `SZJ_NO_REGISTRY=1` + 删文件 | 一次即重置 |
| 设 `SZJ_LICENSE_DIR=<新目录>` + `SZJ_NO_REGISTRY=1` | 一次即重置（实测 `F2 started_wall` 与 `F1` 不同） |
| 改 `trial.json` 的 `hours` 为 8760 | 300 天后仍 `trial_ok=True`（实测） |
| 用另一套硬件组件调用 `trial_evaluate`（模拟换机） | `trial_ok=True`，`fp_score` 只做展示（实测 `L`） |

**影响**
- 无限试用，且不需要任何工具，记事本改 JSON 即可。
- 反过来，正式产品里这两个 `SZJ_*` 开关是给测试用的后门，**被测用户只要知道变量名就能无限重开**。

**加固方向**
- 打包形态（`getattr(sys,'frozen',False)`）下**禁止**读取 `SZJ_LICENSE_DIR` / `SZJ_NO_REGISTRY`，只在源码运行（非 frozen）时启用。
- 本地试用记录做 HMAC：`hmac(嵌入密钥 + 指纹, json)`，密钥不要以明文字符串存在（放 Cython/native 模块）；同时存 `%PROGRAMDATA%` + HKCU + NTFS ADS 三处，取最大值。
- `hours` 不从记录读，写死在代码或由服务端签发；记录里只留签名后的 `exp`。
- 试用判定加入 `fp_score >= 0.7` 硬门槛（现在只有服务端路径才校验）。

---

### L-05（P1）时间账本可篡改：NaN 绕过租期、删账本 + 回拨墙钟复活过期许可

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L358-L398) 账本是**明文 JSON**，无签名、无校验、无范围检查。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L378) `used = float(state.get("offline_used_s") or 0.0)`。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L544-L555) 只有 `used > lease` 才要求联网；`rollback_events >= 2` 才判定篡改。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L509-L514) `anchor=None` 时回落到 `payload["issued_at"]`，`check_now = max(now, anchor+used)`。

**复现步骤（已实跑）**

1. **NaN 绕过租期**
```json
{"last_check_wall": 1700000000, "last_check_mono": 1000,
 "offline_used_s": NaN, "last_server_epoch": 1700000000, "rollback_events": 0}
```
`json.load` 默认接受 `NaN`；`nan > lease` 为 `False`，`max(now, nan)` 返回 `now`。
实测：离线累计 **3000 天**仍返回 `status=ok`（`used=nan`）。

2. **负数** 只影响租期（`used > lease` 恒 False），对到期判定被 `max(now, ...)` 兜住 → 不能单独复活过期证，但可以和 3 组合。

3. **删账本 + 回拨墙钟**
```powershell
Remove-Item "$env:LOCALAPPDATA\SJZ-Control\state.json"
# 重启后把系统时间调到 issued_at + 1 天
```
实测：30 天许可在"真实时间 +200 天"下 `expired`；把 `state.json` 删掉、墙钟拨回 `+1 天` 后 → `status=ok`。
原因：删账本后 `last_server_epoch` 为空，`anchor` 回落到 `issued_at`，`used=0`，`check_now = now` = 被回拨的墙钟。注释里"只把系统时间往回拨不能复活"只在**有联网锚点**时成立。

4. **`rollback_events` 写成负数** → `st["rollback_events"] >= 2` 永不成立，回拨检测被永久关闭。

**影响**
- 买 30 天 → 到期后删一个 JSON + 改 BIOS 时间 → 继续用。
- 或者干脆不删：把 `offline_used_s` 改 NaN → 永远不需要联网，吊销也就永远拉不到（叠加 L-06）。

**加固方向**
- `state.json` 用机器绑定 HMAC（`hmac_key = KDF(嵌入密钥, 指纹)`）签名；校验失败按 `need_online` 处理，而不是按 0 处理。
- 读取时做范围校验：`0 <= used <= 10 年`、`0 <= rollback_events <= 10`、拒绝 `nan/inf/负数/非数字`（`math.isfinite` + 上下限）。
- `anchor` 缺失时**不回落**到 `issued_at`，而是按"从未联网"处理：`need_online`（离线试用只允许 `offline_grace`）。
- 把 `offline_used_s` 同时记在 HKCU DWORD、`%PROGRAMDATA%` 与 NTFS 隐藏流，取最大值；单一存储删改即失效的问题必须消除。
- 需要"防改系统时间"就必须有可信时间源：启动时校验 TLS 服务器 `server_now`（或签名时间戳），本地墙钟只作为上界。

---

### L-06（P1）吊销机制实际无效

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L452-L467) `_revoked_in` **不验签**：`revocations` 只取 `payload` 字段，`sig` 从未被 `verify_blob`。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L574-L577) `revoke_gen = max(本地, 列表 gen)`。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L599-L653)：只有 `activate` / `refresh` 会写吊销列表；全仓**没有**任何调用 `refresh()` 或 `/api/license/revocations` 的地方（无 UI、无启动任务、无定时器）。

**复现步骤（已实跑）**

| 手法 | 结果 |
|---|---|
| 正常吊销（`lic_ids` 命中） | `status=revoked ok=False`（E0） |
| 把 `state.json` 的 `revoke_gen` 改成 `9999999999` | 同一吊销列表 → `status=ok`（E1） |
| 删除 `%LOCALAPPDATA%\SJZ-Control\revocations.json` | `status=ok`（E2，`revocations=None` 直接放行） |
| 手写 `revocations.json`（不签名）`{"payload":{"gen":9999999999,"lic_ids":[]}}` | 与服务端无关，本地账本代数被永久抬高，之后所有真吊销列表都被"比本地旧"忽略 |

**影响**
- 卡密泄露/退款/盗版只能靠"永久停服"，单机吊销 0 生效。
- `gen` 用 `max(revoked_at)`（秒级时间戳），而账本可写死成 `9.9e9`（≈ 2286 年），一次写入永久免疫。

**加固方向**
1. `_revoked_in` 第一步必须 `verify_blob(revocations)`，失败按"列表不可信"处理（保留旧列表 + 记日志），而不是静默使用。
2. 客户端必须真的拉：启动异步 `POST /api/license/revocations`，成功则 `save_license(revocations=...)`；租期到期强制同步拉一次。
3. `revoke_gen` 存在**签名账本**里，且服务端改用单调递增计数器（不要用时间戳），并要求 `gen` 与服务端 `server_now` 一起下发、客户端校验 `gen <= now+1d`。
4. 吊销列表删除后应按"未知"处理（`need_online`），不是"没人被吊销"。

---

### L-07（P2）指纹阈值随"许可里存了几个组件"漂移，单组件许可 100% 通过

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L338-L353) `match_score` 的分母 `total` 只累加 `stored_vec` 里存在的组件。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L300-L317) `collect_components` 读不到的组件直接不放进表（注释自述）。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L497-L504) `if ratio < 0.70 or not hits: 拒绝`。

**复现步骤（已实跑）**
```python
# 许可里只存了 mac
blob = sign({"fp_vec": {"mac": "<hash>"}, "fp_weights": WEIGHTS, ...})
evaluate(blob, components={"mac": "aa:bb:cc:dd:ee:ff"})
# → ratio = 1.0, hits=['mac'], status=ok
```
即使再补一个不匹配的 `cpu_id`，只要 `mac` 命中，`1.0` 依旧通过（分母不包含 `cpu_id`，因为许可里没存）。

实测（许可里只存 `machine_guid + volume_serial` 两项，总权重 35）：

| 改装 | ratio | 结果 |
|---|---|---|
| 只换网卡 | 1.00 | ok（`mac` 不在许可里，分母直接不含它） |
| 只换系统盘 | 0.71 | ok（刚好压在 70% 线上） |
| 只换 MachineGuid | 0.29 | fp_mismatch（符合预期） |

**衍生风险（可爆破）**
- 若签发机器只采到 `volume_serial`（10 分）或 `cpu_id`（15 分），分母就是 10/15。攻击者只需对 32 位卷序列号做 SHA256 暴力（GPU 分钟级），或枚举有限的 CPU 型号串，就能**构造出能通过判定的本机**，无需知道原机。
- 与之相反，"过严"不是主要问题：全 6 组件场景下换系统盘仍 **0.90 通过**、换网卡 **0.95 通过**（实测），容差偏松而非偏紧。

**影响**
- 许可的可移植性完全由"签发时采到几个组件"决定，而不是产品策略；签发端没有任何下限保护。

**加固方向**
- `collect_components` 少于 4 项时**拒绝签发**（服务端 `activate` 校验 `len(vec) >= 4`），客户端 `evaluate` 同样拒绝 `len(stored_vec) < 4`。
- 分母改用固定 `sum(WEIGHTS.values()) = 100`，缺失组件按"未命中"计（宁可严一点，也要可预测）。
- 至少保留 `bios_uuid` + `machine_guid` 两项作为**必须命中**（否则仅靠低熵组件可爆破）。

---

### L-08（P2）克隆/同型号部署 75% 通过 → 一证多机

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L55-L63) 权重与阈值。

**复现步骤（已实跑，许可存满 6 组件）**

| 改装 | ratio | 结果 |
|---|---|---|
| 只换 `machine_guid`（sysprep / 重装系统） | 0.75 | **ok** |
| 换 `machine_guid` + `board_serial` | 0.60 | fp_mismatch |
| 只换系统盘（`volume_serial`） | 0.90 | **ok** |
| 只换网卡 | 0.95 | **ok** |

即：只丢掉 25 分以内的任何单项（含"重装系统"）都放行；要失配必须同时换主板序列号。

**影响**
- 同型号机器批量部署（网吧、客服工位、虚拟机模板克隆）共用一张证：虚拟机克隆通常 `bios_uuid`、`board_serial`、`cpu_id`、`volume_serial`、`machine_guid` 全部相同，只有 MAC 可能不同 → 95%~100% 通过。
- 与 L-07 叠加时更松。

**加固方向**
- 一证一机的前提是"阈值必须高于单组件最大权重"：`FP_MIN_RATIO = 0.70` 挡不住 30 分的 `bios_uuid` 或 25 分的 `machine_guid`。建议改为**必中项**（`bios_uuid` 或 `machine_guid` 任一命中）+ `ratio >= 0.85`。
- 服务端记录 `fp_vec`，同一 `fp_vec` 出现在第二个 `lic_id` 时告警/拒绝（一证多机检测）。
- 虚拟机检测（`Win32_ComputerSystem.Model` 含 VMware/VirtualBox/Hyper-V）时走更严策略或要求联网心跳。

---

### L-09（P2）公钥不是"编译进去且无替换落点"

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L46-L49) `PUBLIC_KEY_B64` 是模块级明文字符串，注释自述"发布前用 gen-key 换一对"——**当前源码里就是开发公钥**；`tests/test_license.py::test_dev_key_matches_embedded_public_key` 实测通过（`ed25519(dev 私钥) == PUBLIC_KEY_B64`），`dist` 的构建时间晚于 `license.py`，即**已构建产物绑定的也是这把可泄露的开发密钥**。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L134-L151) `verify_blob(blob, public_key_b64=PUBLIC_KEY_B64)`、[L470-L471](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L470-L471) `evaluate(..., public_key_b64=None)`、[L562-L563](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L562-L563) `check(..., public_key_b64=...)` —— **三处都允许调用方自带公钥**。
- `tools/license_keys/dev_signing_key.txt` 存在且**未纳入 git，但也没有被 `.gitignore` 忽略**（`git status` 显示 `?? tools/license_keys/`）；一次 `git add -A` 就会把私钥提交进仓库。

**复现步骤（已实跑）**
```python
seed2 = bytes(range(32,64))
forged = sign_blob({... "exp": None, "perpetual": True ...}, seed2.hex())
evaluate(forged, public_key_b64=b64(publickey(seed2)))
# → status=ok（永久许可）
```
只要有任何一处调用点把公钥从配置/插件/参数读进来，就能自签永久证。

**另两条替换路径（静态确认）**
1. PyInstaller 6 onedir 把纯 Python 放进 exe 内 PYZ；`pyinstxtractor` 解出 `core/license.pyc` → `pycdc` 反编译（本项目 Python 3.11.9，主流反编译器可处理）→ 改 `PUBLIC_KEY_B64` → 重打包/替换 exe。不需要签名校验（L-10）。
2. 即便只做字符串替换，PYZ 条目是 zlib 压缩的，需解压-替换-重压，属于"几分钟到半小时"的成熟流程，不是"改一个字节"——但门槛仍然很低。

**补充：目录投放覆盖实测有效吗？——无效（这条是好消息）**
把 `_internal\core\license.py` 换成打印 `status=ok` 的桩，跑 `--doctor`：桩里的 marker 文件**没有**生成，`[许可]` 仍读 PYZ 里的真实模块。
（原因：PyInstaller 的 PYZ finder 先于 `_MEIPASS` 文件系统 finder。）所以"往目录里丢一个 .py 就劫持许可"这条路走不通。

**影响**
- 换公钥即可签发任意 `exp=None` 的永久证，且完全绕过卡密/服务端。
- 开发私钥留在开发机 + 未 ignore，任何一次"打包源码给代理/外包/客户排障"都会让整条许可链作废。

**加固方向**
- 生产路径去掉 `public_key_b64` 参数（或只在 `__debug__` / 明确测试开关下允许），公钥以 `bytes` 常量编译进 **native 模块**（Cython/Nuitka），并让许可校验在该模块内完成（Python 侧只拿到 `bool`）。
- 发布前 `gen-key` 换正式密钥，且把 `tools/license_keys*`、`tools/license.db` 加进 `.gitignore`；私钥只存在服务器 KMS/HSM，不进任何仓库或安装包。
- exe 做 Authenticode 签名，启动时自校验（WinVerifyTrust）+ 校验关键资源清单（见 L-10）。

---

### L-10（P2）打包产物无完整性校验，DLL/模型/前端均可替换

**位置**
- [sjz.spec](file:///C:/Users/26346/Desktop/project/dafors/sjz.spec#L32-L44) 把 `web/`、`core/bricks.json`、`core/skins_raw.csv`、整个 `core/assets`（含 `interception.dll`、OCR 模型、字形模板）以**普通文件**放进 `_internal`。
- [selfcheck.py](file:///C:/Users/26346/Desktop/project/dafors/core/selfcheck.py#L163-L202) `--doctor` 只**打印** `size/sha16`，代码里没有任何"期望哈希"常量可对比。

**实测产物状态（`dist\SJZ-Control`）**
- `_internal\core\assets\rapid`：模型齐全（det ≈ 9.9MB、rec ≈ 21.2MB），与文档一致。
- `_internal\core\assets\interception.dll`：存在，11,264 字节。
- 包内**没有任何** `.ttc/.ttf`（见 L-13）。
- `rapidocr` 自带 `.onnx` 已剔除，仅余 yaml（省 ~30MB，符合 spec 注释）。

**复现步骤（可替换性，静态确认 + 路径核对）**
1. 用 `ctypes.WinDLL("_internal/core/assets/interception.dll")` 加载 → 把该文件换成任意同名 DLL（带 `DllMain`）即可在启动时执行代码（`core/interception.py` 的候选路径里 `_MEIPASS/core/assets/interception.dll` 命中）。
2. 替换 `_internal/web/*`（`server.py` 从 `__file__` 推出的 `_MEIPASS/web` 直读）→ 可改界面、伪造"已授权"提示、注入脚本。
3. 替换模型 `.onnx` → 改变 OCR 识别结果（影响抢购判断）。
4. 上述任一替换都**不会被 `--doctor` 判失败**（它只打印哈希）。

**影响**
- 破解者可以整体重打包目录二次分发（去掉许可 / 植入后门 / 改 UI），正版没有任何检测手段；也给了本地提权/投毒面。
- 对"卖的东西"最直接的影响：盗版包与正版包在 `--doctor` 眼里完全一样。

**加固方向**
1. 发布时生成 `manifest.json`（每个资源 `相对路径 + sha256`）并**签名**；启动时校验，缺失/不匹配 → `license` 直接 `tamper` 拒绝启动。
2. exe 与 DLL 做 Authenticode；`WinVerifyTrust` 自校验（注意校验自身 exe 时 PYZ 已被加载，需校验磁盘文件）。
3. 把 DLL/模型放只读位置（安装器设 ACL：`Users: Read`），或首次运行复制到 `%PROGRAMDATA%` 并校验哈希。
4. `.pyc` 还原难度的描述在 `_packaging_license.md` 里是**诚实的**（原话"几分钟，0 成本""等于没有"），但文档里提的 Cython/native 加固**尚未落地**；请把它当成"未完成项"，不要当成已具备的保护。

---

### L-11（P2）`refresh` 每次把到期日重置为 `now + 原天数`

**位置**
- [license_server.py](file:///C:/Users/26346/Desktop/project/dafors/tools/license_server.py#L142-L150)：`days = (exp-issued_at)//86400`；`new["issued_at"]=now`；`new["exp"]=now+days*86400`。

**复现步骤（代码静态可判定）**
- 买 365 天 → 第 364 天调一次 `refresh` → `exp` 变成 `今天+365` → 再一年；无限循环，且服务端不校验支付。

**影响**
- 一次性买断型产品被续成永久；`refresh` 本应是"校验 + 拉吊销"，现在变成了"免费续期"。

**加固方向**
- 续期必须由服务端按订单驱动：`exp = max(old_exp, purchased_until)`，`refresh` 不改 `exp`，只改 `last_seen`。
- 若确实要做"租约续期"，用独立字段 `lease_until = now + lease_h`，与 `exp` 分离。

---

### L-12（P3）签名 payload 无类型/范围校验，非法值直接抛异常

**位置**
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L516-L519) `float(payload.get("grace_h", ...))`；`lease_h`/`offline_grace_h` 同理；[L528](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L528) `float(exp)`。
- [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L459) `int(rp.get("gen") or 0)` —— `revocations.json` 是本地文件，**不验签**（L-06），可写入 `{"gen":"abc"}`。

**复现步骤（已实跑）**
```
payload grace_h=null → TypeError: float() argument must be ... not 'NoneType'
```
异常从 `evaluate` 直接冒出，`check()` 不捕获；若接到启动路径，表现为"双击没反应/闪退"。

**影响**
- 服务端一次字段写错（或本地 revocations 被写坏）→ 客户端启动崩溃；诊断成本高（`--doctor` 里也是 `status=error`）。

**加固方向**
- `verify_blob` 之后统一做 schema 校验：`grace_h/lease_h/offline_grace_h ∈ [0, 8760]`、`exp ∈ [0, now+50y]` 或 `None`、`fp_vec` 必须是 `str→str`、`lic_ids` 必须是 `list[str]`；不合法 → `sig_invalid`（fail closed），不抛裸异常。
- `check()` 外层 `try/except Exception` 兜底为 `Decision("error", False)`，避免启动闪退。

---

### L-13（P3）`--doctor` 的"通过"不可靠

| 现象 | 位置 | 实测/静态证据 | 为什么不可靠 |
|---|---|---|---|
| 退出码恒 0 | [desktop.py](file:///C:/Users/26346/Desktop/project/dafors/desktop.py#L297-L299) | 临时副本上 `--doctor`，无许可、无驱动，`ExitCode=0` | 安装器/CI 无法用它 gate |
| `context = True` 假阳性 | [selfcheck.py](file:///C:/Users/26346/Desktop/project/dafors/core/selfcheck.py#L196-L199) | 无 `interception.sys` 时仍 `context=True` | 看似通过，实际驱动没装（文档里承认会假阳性，但输出仍把它放在"通过项"位置） |
| 许可状态不推进账本 | [license.py](file:///C:/Users/26346/Desktop/project/dafors/core/license.py#L588) `summary()` → `check(save_state=False)` | — | 离线秒数不增加，doctor 可能报 `ok`，而真实启动（会推进账本）已经 `need_online` |
| 字体只查系统、包内没有 | [selfcheck.py](file:///C:/Users/26346/Desktop/project/dafors/core/selfcheck.py#L214-L221) + spec 无字体 datas | `dist` 内 `.ttc/.ttf` 数量为 0 | 英文版 Windows 上 `msyh.ttc` 缺失 → 方框；doctor 建议"安装包需带上"，但包没带 |
| WebView2 结论与兜底矛盾 | [selfcheck.py](file:///C:/Users/26346/Desktop/project/dafors/core/selfcheck.py#L210-L212) vs desktop 的浏览器兜底 | 代码有 `--app` 浏览器降级路径 | 报"界面起不来"，实际能起（假阴性） |
| 模型"可用"可能靠联网下载 | [selfcheck.py](file:///C:/Users/26346/Desktop/project/dafors/core/selfcheck.py#L173-L182) `ocr.available()` | rapidocr 自带 onnx 已被 spec 剔除 | 本地模型损坏但能联网时仍 `True`；断网才暴露，掩盖打包缺陷 |
| 不做完整性比对 | [selfcheck.py](file:///C:/Users/26346/Desktop/project/dafors/core/selfcheck.py#L163-L170) | 仅打印 sha16 | 被替换的 DLL/模型/前端全部"通过"（L-10） |
| 不检查许可是否真的生效 | 无 | doctor 报 `none ok=False`，程序照跑 | 自检没有"契约检查"：许可模块是否被调用没人验 |

**加固方向**
- `--doctor` 任何 FAIL → 非 0 退出；输出末尾一行机器可读 `DOCTOR_RESULT=FAIL:code`。
- `context` 改名为 `context_api`，并加 `driver_installed` 作为唯一通过项。
- 许可段加两项：`enforced`（进程内是否存在 gate 调用标记）、`offline_remaining`（用推进后的账本模拟，不改文件）。
- 引入 `manifest.json` + 期望哈希（L-10），doctor 逐项比对。
- 字体改成随包内置（`datas` 加 `fonts/` 并在 UI 里 `@font-face` 指向 `_MEIPASS`），或明确"仅支持中文 Windows"并在 doctor 里给出可执行修复命令。

---

### L-14（P3）打包配置依赖构建时的工作目录

**位置**
- [sjz.spec](file:///C:/Users/26346/Desktop/project/dafors/sjz.spec#L30) `ROOT = os.path.abspath(os.getcwd())`；[sjz-onefile.spec](file:///C:/Users/26346/Desktop/project/dafors/sjz-onefile.spec#L20) 同样。

**影响**
- 在仓库根目录以外执行 `pyinstaller <path>/sjz.spec` → `web/`、`core/assets` 全部找不到，PyInstaller 只会 warn，**产出一个缺模型/缺 DLL 的包**，而 `--doctor` 仍可能通过（L-13 模型回退下载）。
- 属"打包完整性"典型坑：构建可复现性不成立。

**加固方向**
- 改为 `ROOT = os.path.dirname(os.path.abspath(SPEC))`（PyInstaller 提供 `SPEC` 全局），并在 `Analysis` 前断言关键路径 `os.path.isdir`，缺失直接 `raise SystemExit`。
- 构建脚本里固定 `cwd`，并让 `build.bat` 在结束时调用 `SJZ-Control.exe --doctor`，非 0 即失败。

---

### L-15（P3）DLL 候选路径与 spec 实际位置不一致

**位置**
- `core/interception.py` 的 `_dll_candidates()`：候选顺序为 `_MEIPASS/interception.dll` → `_MEIPASS/core/assets/interception.dll` → 源码同级 `assets/interception.dll`。
- spec 只把 DLL 放到 `core/assets`（[sjz.spec](file:///C:/Users/26346/Desktop/project/dafors/sjz.spec#L40-L43)）。

**影响**
- 首项候选是 `_MEIPASS` 根目录。若安装目录里被放入同名 `interception.dll`（或攻击者拷入），会**优先加载**它，而不是包内的那份 → 直接代码执行（与 L-10 叠加）。
- 当前只是"多了一个不该存在的搜索位"，不是崩溃问题。

**加固方向**
- 删掉首项候选，或在加载前对 DLL 做哈希/签名校验再 `WinDLL`（优先 `LoadLibraryEx(..., LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR)` 限定搜索路径）。

---

## 三、攻击手法覆盖表（试过什么 / 结果）

| 手法 | 结果 |
|---|---|
| 改 `license.json` 的 `exp`/`fp_vec`/`tier`，保留原签名 | 拒绝（`sig_invalid`） |
| 用攻击者密钥自签 + 默认公钥验签 | 拒绝 |
| 用攻击者密钥自签 + `public_key_b64` 覆盖 | **通过**（L-09） |
| 往 `_internal/core/` 投放 `license.py` 桩 | **不生效**（PYZ 优先，marker 未生成） |
| 删 `license.json` | 变成 `none`（若已接线，等于"未激活"；未接线=无影响） |
| 删/改 `state.json`（NaN、负数、`revoke_gen` 写大、`rollback_events` 负） | **全部生效**（L-05/L-06） |
| 删 `revocations.json` | **吊销失效**（L-06） |
| 本地改 `trial.json` 的 `hours` / 拷到别的机器 | **延长/继续试用**（L-04） |
| 删 `trial.json` + HKCU 键 / `SZJ_NO_REGISTRY` / `SZJ_LICENSE_DIR` | **重置试用**（L-04） |
| `/api/license/trial` 同 vec 换 fp 重复领 | **无限 3 天证**（L-03） |
| `/api/license/trial` 直接调用（不改服务端） | 500，签不出（L-02），**且响应体把私钥种子交出来**（L-02B） |
| 只换 `machine_guid`（sysprep） | **75% 通过**（L-08） |
| 只换网卡 | 95% 通过（符合设计） |
| 换主板序列号 + 换系统 | 失败（符合设计） |
| 单组件许可 + 同 MAC | **100% 通过**（L-07） |
| 换目录启动（`cd %TEMP%` 起 exe） | 正常，`config.json`/日志都落在 exe 目录（**无 CWD 依赖**） |
| 删 API 目录里的系统字体 | 未测（不改用户环境）；静态确认包内无字体 |

---

## 四、确认没问题的项

1. **Ed25519 校验本身可用**：改 payload、改 sig、换版本号、非 dict 输入全部被拒（`LicenseError`），实测无绕过。
2. **签名覆盖完整**：`canonical_bytes` 用 `sort_keys + 紧凑分隔符`，payload 任一字段改动都会破坏签名。
3. **不依赖当前工作目录**：`config.json`、日志、模板、`web/`、`core/assets` 都从 exe 或 `__file__` 推导；在 `%TEMP%` 目录下启动 `--doctor` 仍指向 exe 同级（已实测）。
4. **文件写入是原子的**：`.pid.tmp` + `os.replace`，断电不会留下半个 JSON。
5. **目录投放式模块劫持无效**：`_internal/core/license.py` 覆盖不会抢在 PYZ 前被导入（已实测 marker 未生成）。
6. **资源确实进包**：OCR 模型（det ≈9.9MB / rec ≈21.2MB）、`interception.dll`（11,264B）、`bricks.json`、`skins_raw.csv`、`web/` 均在 `_internal` 内；`rapidocr` 重复 onnx 已按 spec 剔除。
7. **指纹在本机两次采集一致**：`components_present = 6`，两次 `hash` 相同（时钟、顺序、临时挂载未影响结果）。
8. **"换主板/重装系统会失配"的设计目标成立**：同时变 `machine_guid` + `board_serial` 后判定失败。
9. **`--doctor` 不会真发输入事件**：`driver_ready(probe=False)`，不抢光标、不弹窗（已实测：无窗口、无鼠标移动）。

---

## 五、无法验证的项

1. **真实硬件换件**（物理换 CPU / 主板 / 系统盘）—— 只用组件替换做了模拟判定（L-07/L-08 的表），未做物理操作。
2. **虚拟机克隆实机验证** —— 未开 VM；结论来自权重推算（L-08）。
3. **正式公钥是否已轮换** —— 当前 `dist` 与源码都是开发公钥；发布前是否换、换成什么，无法验证。
4. **PYZ 重建/字符串替换的实际耗时** —— 未做二进制重建（不修改产物），只确认 onedir 的 PYZ 在 exe 内且模块覆盖无效；可行性依据是 PyInstaller 3.11 的公开流程。
5. **onefile 形态的 `_MEIPASS` 投放覆盖** —— 解包目录随机，未实测。
6. **服务端并发/压测** —— 只做了单请求验证；两个 500 缺陷已足以阻断，未继续压。
7. **端到端激活链路** —— 被 L-02 挡住，`activate → save_license → check` 从未跑通。
8. **`--doctor` 在真实发布机（无 WebView2、英文系统）的表现** —— 只在本机验证退出码/输出结构。
9. **模型是否可被远程下载兜底** —— 由 rapidocr 行为推断，未断网实测。
10. **当前这把开发私钥是否已在公网暴露过** —— 只在本地 loopback 复现了"回显"；若 `tools/license_server.py` 曾部署到公网且被扫过，按已泄露处理，但历史访问日志不在审查范围内。
11. **`dist` 是否由当前源码逐字节构建** —— 只用时间戳（dist 晚于 `license.py`）推断同源，未做可复现构建比对。

---

## 六、修复顺序（建议）

1. **L-02B**（私钥已通过 HTTP 泄露）：先轮换密钥对，再改错误回显；这把 key 不能再用于任何发布。
2. **L-02**（服务器签不出证）+ **L-01**（客户端不校验）：这两个不修，其他都是空谈。
3. **L-06 + L-05**（吊销/时间账本）：决定"能不能被白嫖"，必须签名账本 + 真拉吊销。
4. **L-03 + L-04**（试用）：决定"能不能无限试用"。
5. **L-07 + L-08**（指纹）：决定"一证几机"。
6. **L-09 + L-10**（公钥与产物完整性）：决定"盗版包能不能以假乱真"。
7. **L-11**（续期）按商业模式定策略。
8. **L-12 ~ L-15**：健壮性与交付质量，随版本修。

---

## 附录：最小复现片段（全部只依赖 `core.license` 纯函数）

```python
import base64, json, os, sys, tempfile
sys.path.insert(0, r"C:\Users\26346\Desktop\project\dafors")
os.environ["SZJ_LICENSE_DIR"] = tempfile.mkdtemp()      # 隔离，不写真实用户目录
from core import license as L, ed25519_ref as E
SEED = bytes(range(32)); PUB = base64.b64encode(E.publickey(SEED)).decode()
C = {"bios_uuid":"b","machine_guid":"g","board_serial":"s",
     "cpu_id":"c","volume_serial":"v","mac":"m"}

# L-05: NaN 绕过租期
fp = L.fingerprint(C)
b = L.sign_blob({"v":1,"lic_id":"x","tier":"pro","issued_at":1700000000,
                 "exp":1700000000+3650*86400,"grace_h":72,"lease_h":168,
                 "offline_grace_h":72,"fp":fp["hash"],"fp_vec":fp["vec"],
                 "fp_weights":L.WEIGHTS,"trial":False}, SEED.hex())
st = {"last_check_wall":1700000000,"last_check_mono":1000,
      "offline_used_s":float("nan"),"last_server_epoch":1700000000,
      "rollback_events":0}
print(L.evaluate(b, state=st, now=1700000000+3000*86400,
                 mono=1000+3000*86400, components=C,
                 public_key_b64=PUB).status)   # ok

# L-07: 单组件许可 100% 通过
b1 = L.sign_blob({"v":1,"lic_id":"y","tier":"pro","issued_at":1700000000,
                  "exp":1700000000+86400*365,"grace_h":72,"lease_h":168,
                  "offline_grace_h":72,"fp":"","fp_vec":{"mac":L.fingerprint({"mac":"aa:bb:cc:dd:ee:ff"})["vec"]["mac"]},
                  "fp_weights":L.WEIGHTS,"trial":False,"perpetual":True}, SEED.hex())
print(L.evaluate(b1, now=1700000000, mono=1000, public_key_b64=PUB,
                 components={"mac":"aa:bb:cc:dd:ee:ff"}).status)   # ok / ratio=1.0

# L-09: 公钥旁路自签永久证
S2 = bytes(range(32,64)); P2 = base64.b64encode(E.publickey(S2)).decode()
f = L.sign_blob({"v":1,"lic_id":"z","tier":"pro","issued_at":1700000000,
                 "exp":None,"grace_h":72,"lease_h":168,"offline_grace_h":72,
                 "fp":fp["hash"],"fp_vec":fp["vec"],"fp_weights":L.WEIGHTS,
                 "trial":False,"perpetual":True}, S2.hex())
print(L.evaluate(f, now=1700000000, mono=1000, components=C,
                 public_key_b64=P2).status)   # ok
```

> 说明：以上片段写入的目录为 `tempfile.mkdtemp()`，不改 `%LOCALAPPDATA%`、不改系统时间、不联网、不产生可见窗口。审查期间唯一起过的网络服务是 `127.0.0.1` 上的参考许可服务器（测试后已停止），唯一被写入的磁盘区域是 `%TEMP%` 下的临时副本（已清理）。
