# R10 许可体系修复报告

对应审查：`_bug_r10_license.md`（P0 三项、P1 四项、P2 三项）

改动文件：

| 文件 | 改动性质 |
|---|---|
| `core/license.py` | 重写判定层：验签固定公钥、指纹收紧、账本签名、吊销验签、试用防重置、`ensure()` 门禁 |
| `tools/license_server.py` | 修签名、异常不回显、并发、试用去重/限速、续期上限、吊销代数 |
| `tools/license_admin.py` | `sign()` 改为吃十六进制种子、`db_open` 跨线程、新增 `load_seed()` |
| `core/server.py` | **仅增量**：4 条许可路由 + 写操作许可闸（其他路由/状态接口未动） |
| `desktop.py` | 在「起服务/热键/调度器」之前插入许可门禁 + `--activate` / `--license-server` |
| `tests/test_license.py` | 原 22 例改造 + 新增 R10 回归用例 |
| `tests/test_license_server.py` | 新增，真 HTTP 端到端 |
| `tools/r10_regression_proof.py` | 新增，修前/修后差分证明（17 项） |
| `.gitignore` | 忽略 `tools/license_keys/`、`tools/*.db` |

未动：`core/engine.py`、`core/input.py`、`core/countdown.py`、`web/index.html`、`core/config.py`。

---

## 一、P0

### L-01 许可模块从未被调用（复制 dist 即可用）

- **根因**：`desktop.main()` 里没有许可分支，`core/server.py` 也没有任何许可路由；`license.py` 只是死代码。
- **改法**
  - `license.ensure()` 成为唯一门禁入口：本地判定 → 无许可则开/续本地试用 → 配了服务器就拉吊销 → 缓存 `_ENFORCED`。
  - `desktop.main()` 在 `_single_instance()` / `_elevate_if_needed()` 之后、`_log("--- 启动")` 之前调用 `_license_gate(args)`；`ok=False` 时弹 MessageBox（含原因 + 三种可操作激活方式）并返回退出码 3，**不是崩溃**。
  - 新增 `--activate KEY` 与 `--license-server URL`，让「没有 GUI 也能激活」。
  - `core/server.py` 的 `do_POST` 在分发前检查 `_license.server_gate_ok()`；未激活时写操作返回 `403 {"ok": false, "msg": "未授权…"}`。许可自身的 4 条路由不受此闸限制。
  - `_ENFORCED` 默认 `None`（源码运行 / 测试不拦），只有 `ensure()` 真正跑过才置 `True/False`，避免破坏已有测试与开发流程。

- **回归**：`test_r10_ensure_grants_trial_when_no_license`、`test_r10_ensure_blocks_when_trial_exhausted`、差分 `L-01`（静态断言 desktop 含 `licensing.ensure(`、server 含 `_license.server_gate_ok()`）。
- `pytest tests/test_server.py tests/test_skin_api.py tests/test_console_api.py` 全绿（门禁未启用时不误伤）。

### L-02 参考服务器签不出证（hex 当路径）

- **根因**：`tools/license_admin.sign(payload, key_file)` 里 `open(key_file)`，但 `license_server._pack()` 传的是 `S.seed`（64 位十六进制串）→ `FileNotFoundError` → `activate/refresh/trial` 恒 500。
- **改法**：`sign(payload, seed_hex)` 直接走 `lic.sign_blob()`；`license_admin.load_seed()` 才是「读文件」的唯一入口，`cmd_issue` 显式 `sign(payload, load_seed(args.key_file))`。`lic.sign_blob()` 内部对种子做 `len==64` 且全十六进制的断言，从根上不让「路径/任意串」混进签名函数。
- **并发**：`db_open()` 改 `check_same_thread=False`，`Store` 用 `threading.RLock()` 包住所有 SQL。

- **回归**：`test_activate_signs_verifiable_license`、`test_refresh_signs_and_keeps_expiry`、`test_trial_signs_and_binds`、`test_r10_concurrent_requests_do_not_hit_sqlite_thread_error`（5 线程并发激活全部 200）。
- **实机证据**（真进程 + 真 HTTP，用本地开发密钥）：

```
activate -> 200
license  -> lic-450ea59e7eaf43e2 tier= pro exp-issued= 31536000
refresh  -> 200
```

### L-02B 私钥种子回显（最严重）

- **根因**：`do_POST` 的异常分支 `return self._send(500, {"error": f"{type(e).__name__}: {e}"})`；L-02 那个 `FileNotFoundError` 的 `str(e)` 里就是完整种子。
- **改法（三层，任一层都足以拦住）**
  1. 响应体固定为 `{"error": "internal error"}`，**不拼任何 `str(e)`**；
  2. 异常只进服务端 stderr，且先过 `_redact()`（把种子的小写/大写形态替换成 `[REDACTED]`）；
  3. 根因本身被修掉（签名函数不再把种子送进 `open()`），所以不会再产生带种子的异常消息。
  - 另加 `Store.__init__` 的 `_require_seed_hex()`：格式不对抛「签名种子必须是 64 位十六进制」，消息里不带内容。
  - `log_message` 也只记方法/路径/状态行，请求体/响应体不入日志。

- **回归**：`test_r10_error_response_never_contains_seed`（同时断言响应体 == `{"error":"internal error"}`、响应与 **stderr 都不含种子**、stderr 出现 `[REDACTED]`）；`test_r10_store_rejects_bad_seed_without_echo`；`test_r10_health_has_no_key_material`。
- **实机证据**（强制 `sign` 抛异常）：

```
[license-server] /api/license/activate 处理失败：Traceback ...
    raise RuntimeError("sign failed for " + seed_hex)
RuntimeError: sign failed for [REDACTED]
POST /api/license/activate 500
error    -> 500 {"error": "internal error"} | seed in body: False
```

- **密钥分离**：`core/license.py:PUBLIC_KEY_B64` 换成一个**新的发布占位公钥**（对应私钥本轮生成后即丢弃，仓库里不存在）；测试私钥只出现在 `tests/`。`test_release_key_is_separate_from_test_key` 反向断言「仓库里任何种子推不出发布公钥」，并替代了旧用例 `test_dev_key_matches_embedded_public_key`（那条用例等于把测试私钥钉进发布包，必须删）。

---

## 二、P1

### L-03 试用可无限领（服务端盲信客户端 fp）

- **改法**（`tools/license_server.trial()`）
  - `_clean_vec()`：键必须在 `license.WEIGHTS` 内、值必须是 16–64 位十六进制、**至少 4 个组件**，否则 400；
  - `_derive_fp()` 由服务端按组件自己算指纹，客户端上报的 `fp` 只用来**比对**（不一致直接 400），不再被信任或入库；
  - `_dedupe_key()` 优先取稳定子集（bios_uuid/machine_guid/board_serial），换 MAC/换盘不能重领；
  - 同 IP 24h 内最多 3 次（`trials_v2` 表 + `TRIAL_MAX_PER_IP`），超限 429。

- **回归**：`test_r10_trial_dedupe_is_server_side`（第二次甚至不上报 fp，仍判 409）、`test_r10_trial_rejects_fp_vec_mismatch`、`test_r10_trial_rejects_too_few_components`、`test_r10_trial_rate_limited_per_ip`。
- **残余风险（需知晓）**：服务端拿不到真实硬件，只能靠「组件一致性 + 稳定子集去重 + IP 限速」。要更强约束得接设备证明（TPM/attestation），超出本轮范围。

### L-04 本地试用可重置（三条路径）

- **删除记录**：移除旧的单一 `%LOCALAPPDATA%\SJZ-Control\trial.json` 一删就重来的问题 —— 记录同时写**用户目录 + `%PROGRAMDATA%\SJZ-Control\trial.json` + HKCU**，读取取**最早**的 `started_wall`；任一存在即不重置。
- **换目录**：`SZJ_LICENSE_DIR` / `SZJ_NO_REGISTRY` 只在 `not sys.frozen` 时生效（`_test_hooks_allowed()`）；打包后一律忽略，只看 `LOCALAPPDATA`。
- **改小时数**：`TRIAL_HOURS = 72` 是常量，`trial_evaluate()` 只用它，**不再读记录里的 `hours`**。
- **记录防篡改**：`trial.json` 变成 `{"payload", "sig"}`，`sig = HMAC(嵌入密钥 + 本机指纹, payload)`；明文/别机器的记录一律不认。
- **指纹绑定**：`trial_evaluate()` 里先过 `_fp_passes()`。

- **回归**：`test_r10_trial_hours_field_ignored`、`test_r10_trial_env_override_ignored_when_frozen`、`test_r10_trial_survives_single_file_delete`、`test_r10_trial_record_is_signed_and_fp_bound`、`test_trial_begin_does_not_reset_when_record_exists`。
- **代价**：老版本的明文试用记录不再被认（升级后会给一次新试用），这是 fail-open → fail-closed 的必然取舍，已在代码注释里写明。

### L-05 时间账本（NaN / 删状态回拨 / 负回拨计数）

- **所有数值入口**统一走 `_finite(value, lo, hi)`：非数字、非有限（NaN/±inf）、越界一律返回 `None`；`_sanitize_state()` 对 `offline_used_s`、`rollback_events`、`revoke_gen`、两个时间戳逐个收敛。
- **NaN**：`offline_used_s=NaN` 现在被收敛成 `0`，随后按单调时钟增量累加 → 超租期照样 `need_online`；不会再出现「`NaN > lease` 恒 False」。
- **删状态 + 回拨**：账本文件改成 `{"payload","sig"}`，HMAC key = 嵌入密钥 + 许可里的稳定组件子集。删文件 / 改任意数字 / 去掉签名 → `_load_state()` 返回不可信 → `evaluate(state_trusted=False)` → `need_online`。
- **不再回落到 issued_at 当锚点**：`last_server_epoch` 缺失即「从未联网」→ `need_online`。
- **负回拨计数**：`rollback_events` 经 `_safe_int(0..1000)`；负数变 0，回拨照记。
- **另修**：`grace_h=null` 旧实现会抛 `TypeError`（接进启动路径就是闪退）→ 现在 `_validate_payload()` 直接判 `sig_invalid`，`evaluate()` 永不上抛。

- **回归**：`test_r10_nan_state_cannot_buy_perpetual_offline`、`test_r10_state_without_anchor_needs_online`、`test_r10_untrusted_state_needs_online`、`test_r10_state_for_other_license_needs_online`、`test_r10_check_rejects_plaintext_state`、`test_r10_check_detects_state_tamper_value`、`test_r10_nan_and_inf_payload_fields_rejected`。

### L-06 吊销

- **必须验签**：`_revoked_in()` 先 `verify_blob()`；验签失败 = 不可信（不是「无列表」）。
- **缺失不等于放行**：签名账本里记 `rev_seen`/`revoke_gen`；若曾同步过而当前列表缺失或验签失败 → `need_online`。
- **版本号不能被写死**：`revoke_gen` 只写在签名账本里（明文改 → HMAC 失败 → 整体不可信）；列表 `gen` 小于账本 `gen` 视为旧列表直接忽略。
- **服务端代数**：`meta.rev_gen` 单调递增，`revocations_doc()` 每次签发都 +1。
- **客户端真的有调用方**：新增 `license.pull_revocations()`，`ensure()` 在配置了服务器时必调一次；`server.py` 新增 `POST /api/license/revocations` 与 `GET /api/license` 暴露状态。

- **回归**：`test_revocation_blocks_license`、`test_stale_revocation_list_ignored`、`test_r10_unsigned_revocation_list_not_trusted`、`test_r10_missing_revocations_after_sync_needs_online`、`test_r10_revocation_gen_written_into_signed_state`、`test_r10_revocations_gen_is_monotonic`。

---

## 三、P2

### L-07 指纹单组件即可通过

- **改法**：`match_score()` 分母**固定** `sum(WEIGHTS)=100`（许可里没存的组件按未命中计）；`_fp_passes()` 要求 ① 许可里 ≥ `FP_MIN_COMPONENTS=4` 个组件、② `bios_uuid`/`machine_guid` 至少命中一个、③ 比例 ≥ `FP_MIN_RATIO=0.85`。
- **回归**：`test_r10_fp_single_component_license_rejected`（只绑 MAC → 5%，拒绝）、`test_r10_fp_requires_anchor_component`。

### L-08 只换 MachineGuid 有 75% 通过率

- **改法**：阈值 0.70 → 0.85（高于单组件最大权重 30）。0.85 仍容忍换网卡（95%）、换 CPU（85%）、加内存（100%），但拒绝任何锚点级替换。
- **回归**：`test_r10_fp_machine_guid_only_change_rejected`（75% → `fp_mismatch`），`test_fingerprint_tolerates_nic_and_memory_change`（正常变化仍放行）。

### L-09 公钥可经参数旁路自签

- **改法**：`verify_blob(blob)` / `evaluate()` / `check()` / `save_license()` **全部删除** `public_key_b64` 参数；公钥只读模块常量 `PUBLIC_KEY_B64`。测试通过 `monkeypatch.setattr` 改常量（进程内，发布包里没有测试入口）。
- **回归**：`test_r10_public_key_cannot_be_overridden_by_parameter`（`inspect.signature` 断言四个函数都没有公钥参数）、差分 `L-09`（`evaluate(..., public_key_b64=...)` → `TypeError`）。

### L-11 refresh 永久续期

- **改法**：`refresh()` 不再计算 `now + days`，直接 `new["exp"] = min(old_exp, DB 行里的 exp)` —— 签发时的 `exp` 就是绝对上限，续期只续租约（`refreshed_at`）。`_make_license()` 另外把天数钳到 `MAX_LICENSE_DAYS=3650`。
- **回归**：`test_refresh_signs_and_keeps_expiry`（连续 refresh 3 次，`exp` 一次都没变）。

---

## 四、验证与复核

三种**互相独立**的手段，关键结论都至少被其中两种覆盖：

1. **单元测试**（`tests/test_license.py`，46 例全绿）：纯判定、文件路径、试用生命周期；Ed25519 用 RFC 8032 §7.1 官方向量做已知答案（不是自签自验），指纹用手写第二套实现（逐组件 `sha256[:32]` + 加权）对照。
2. **真 HTTP 端到端**（`tests/test_license_server.py`，13 例全绿）：`127.0.0.1` 随机端口、真实 `ThreadingHTTPServer`，覆盖签发、续期、试用去重/限速、并发、异常不回显（同时断言 stderr 已脱敏）。
3. **修前/修后差分**（`tools/r10_regression_proof.py`，17 项，异常 0）：

```
编号   说明                                            旧=可利用  新=已阻断
L-05a  NaN 让 offline_used_s 失效 → 离线 3000 天仍免联网      True      True
L-05b  删 state.json + 回拨墙钟 → 过期证复活                 True      True
L-05c  负数 rollback_events 关闭回拨检测                    True      True
L-06a  明文账本把 revoke_gen 写死 9e9 → 真吊销被忽略           True      True
L-06b  未验签的吊销列表被当成可信数据                          True      True
L-06c  删除吊销列表 = 放行                                   True      True
L-07   只绑 MAC 一个组件 → 旧实现 100% 通过（可爆破）             True      True
L-08   只换 MachineGuid（sysprep）→ 旧 75% 通过              True      True
L-09   public_key_b64 参数旁路自签永久证                     True      True
L-12   grace_h=null → 旧实现抛未捕获 TypeError              True      True
L-04a  改 trial.json 的 hours=8760 → 300 天后仍试用          True      True
L-04b  换 SZJ_LICENSE_DIR 目录重置试用                       True      True
L-02   私钥 hex 被当文件路径 → 三个端点恒 500                  True      True
L-02B  500 响应体回显完整 64 位私钥种子                         True      True
L-03   同一 fp_vec 换随机 fp 重复领试用                        True      True
L-11   refresh 把 exp 推成 now+原天数（无限续期）               True      True
L-01   许可模块未接线（复制 dist 即可用）                       True      True
```

> 说明：修复前的 `core/license.py` / `tools/license_server.py` 不在 git 里（HEAD 无此文件，只有 `__pycache__`），无法 checkout 旧版直接跑。差分脚本把 `_bug_r10_license.md` 里**逐条引用的缺陷表达式**重建为 `legacy_*`（每段标了报告行号），用同一组攻击输入喂旧语义与新实现；「旧=可利用」与报告「复现步骤」的实测输出一致。

**全量测试**：`1450 passed, 17 skipped, 2 xfailed, 6 failed`。6 个失败与本任务无关，且都在本轮改动之前就存在（属其他分支/环境）：

- `test_cursor_lock.py::test_calls_into_ctypes_are_the_ones_we_think`（`ctypes.CArgObject` 缺失，Python 版本相关）
- `test_fix_b.py::test_crop_box_on_a_real_screenshot` / `test_tab_from_pixels_on_real_screenshot`（依赖 `_shots/drv_onsale.png`）
- `test_hotkey.py::test_hook_hotkey_fire_is_debounced_for_0_4s`（钩子未派发，环境）
- `test_precision.py::test_two_hundred_shots_deviation_distribution`（57.99us 抖动）
- `test_robustness.py::test_every_source_file_compiles`（`tools/_r08_dbg.py`、`tools/_r08_merge.py` 带 BOM，别的分支产物）

---

## 五、主会话还需要接线的地方

1. **发布密钥（必须）**：本轮把 `PUBLIC_KEY_B64` 换成发布占位公钥（对应私钥已丢弃）。发布前执行
   `python tools/license_admin.py gen-key`，把输出的公钥填进 `core/license.py:PUBLIC_KEY_B64`，私钥文件**只放签发机**（已在 `.gitignore`）。服务器 `--key` 指向同一份。
2. **旧开发密钥作废**：`tools/license_keys/dev_signing_key.txt` 与现在内置公钥**不再配对**，只留作本地测试；确认打包 spec 不含 `tools/`（已核 `sjz.spec` / `sjz-onefile.spec` 只收 `core`、`assets`、`web`、`index.html`）。
3. **UI 面板**：`web/index.html` 归另一分支，我没有动。需要它接四件事：`GET /api/license` 展示状态、`POST /api/license/activate` 兑卡密、`POST /api/license/refresh` 手动校验、到期/试用剩余倒计时（`remaining_s`）。
4. **服务器地址下发**：`ensure()` 只有拿到 `--license-server` 或 `SJZ_LICENSE_SERVER` 才拉吊销。打包时建议写默认值，否则「缺失列表 → 联网校验」这一步实际不会发生（只能靠本地租期）。
5. **服务端试用签发**：`POST /api/license/trial` 已可用（服务端去重/限速）；目前 `ensure()` 在无许可时先走**本地兜底试用**（72h，防重置）。要彻底做到「试用只能服务端发」，需在 UI 里把本地兜底改成调用 `/trial`。
6. **老用户一次性迁移**：账本从明文变签名，老安装第一次启动会得到 `need_online`（需联网校验一次）。如果这类用户量大，可在发布说明里写一句，别当成故障。
7. **多模型交叉审核**：本轮用三种独立手段（单测 / 真 HTTP / 差分）交叉验证关键结论，但没有跑跨模型复核；建议主会话按惯例再开 3 个不同模型审一遍 `core/license.py` 与 `tools/license_server.py` 的判定分支与账本字段，重点看「新增状态字段是否能被组合绕过」。
