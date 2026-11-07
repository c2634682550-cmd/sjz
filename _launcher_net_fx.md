# SJZ 启动器（纯 .NET Framework 4.8）与卡密验签统一

> 交付范围：`launcher\` 全部、`core/license.py`、`tools/license_server.py`、
> `desktop.py` 的参数解析与父进程守护、`core/server.py` 的控制面鉴权。
> 未触碰 `core/config.py`、`core/switches.py`、`web/index.html`、鼠标注入与 OCR 模块。

## 1. 结论

| 项 | 结果 |
| --- | --- |
| 编译器 | 系统自带 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`（无需 VS） |
| 产物 | `launcher\SjzLauncher.exe`（winexe + requireAdministrator）、`launcher\SjzLauncherTest.exe`（console + asInvoker，仅供自动化） |
| 体积 | 两个 exe 均为 **72,704 bytes** |
| 算法 | **ECDSA P-256 (secp256r1) + SHA-256，签名 = IEEE P1363 定长 r‖s（各 32 字节）** |
| 旧卡密 | 一律拒绝（有意作废），原因可读，不是 500 |
| 全量 pytest | **2001 passed, 19 skipped, 2 xfailed, 0 failed**（8m35s） |
| 启动器自测 | `launcher\tests` **36 passed** |
| 卡密两套 | `tests\test_license.py` + `tests\test_license_server.py` **62 passed** |

## 2. 架构（文字图）

```
                ┌──────────────────────────── SjzLauncher.exe (管理员, WinForms) ────────────────────────────┐
                │ Global\SjzLauncher.Console.v1  单实例互斥                                      │
                │ app.manifest requireAdministrator + UAC 显式提权                              │
                │                                                                              │
   license.json │ 1) LicenseCheck: 读卡密 → ECDSA-P256 验签 → schema → 指纹(0.85/≥4/锚点) → 过期/宽限 │
  (%SJZ_DATA_DIR│ 2) Preflight: .NET / VC++ / 驱动目录+WinVerifyTrust / 游戏进程(UnrealWindow) │
   或 LOCALAPPDATA)│ 3) Updater: version.json 验签 → 下载 → SHA-256 → 解压防穿越 → 原子替换 → 重启   │
                │ 4) ProcessHost: Process.Start(runtime\pythonw.exe ...) + Job + 壳进程识别      │
                └───────────────┬──────────────────────────────────────────────┬───────────────┘
                                │ 子进程 + Job(KILL_ON_JOB_CLOSE)               │ HTTP 127.0.0.1
                                ▼                                              ▼
                 desktop.py --launcher-pid <pid> --ctrl-token <32hex>   控制面（core/server.py）
                 父进程 watcher：1s 轮询，父死 → 释放输入 → _shutdown_soon   GET  /api/ctrl/status
                 （正常停机路径，不硬杀）                                POST /api/ctrl/stop
```

要点：
- **Job 先套住自己再拉子进程**，子进程才继承 Job；启动器被强杀时 `KILL_ON_JOB_CLOSE` 收尾整棵树。
- 但 venv 的 `pythonw.exe` 是「壳进程」，会 breakaway 后 exec 真解释器；因此再加一层
  Toolhelp32 父子快照（`FindChildPid`/`KillDescendants`）来识别真身与清理树。
- 更新顺序写死在 `LauncherRuntime.Prepare`：**卡密 → 自检 → 更新**，验签不过不替换。

## 3. 两进程协议与参数

启动器 → Python 命令行（工作目录 = 项目根，`CreateNoWindow`，不弹控制台）：

```
runtime\pythonw.exe desktop.py --launcher-pid <launcher 的 pid>
    --ctrl-token <每次启动随机 16 字节 hex>
    --port <控制面端口> --host 127.0.0.1 --elevated
```

控制面（回环也验）：

| 项 | 规则 |
| --- | --- |
| 鉴权 | 请求头 `X-SJZ-Token`，或 Cookie `sjz_ctrl`（`HttpOnly; SameSite=Strict`，访问 `/` 时种下） |
| 比较 | `hmac.compare_digest`，不回显 token 片段 |
| Host | 必须是 `127.0.0.1` / `localhost` / `::1`（防 DNS rebinding），否则 403 |
| `GET /api/ctrl/status` | `pid / uptime_s / auth / license 摘要 / scheduler 摘要` |
| `POST /api/ctrl/stop` | 200 后走 `_shutdown_soon("api-stop")`：先停调度、抬输入、放单实例闸、再退出 |
| 未启用 token | `set_control_token(None)` 时放行（保持旧源码直跑/旧测试可用） |

父进程守护：`desktop.py` 每 1 秒 `OpenProcess + WaitForSingleObject(0)`；父进程消失 →
`_release_input_before_exit()` → `core.server._shutdown_soon("launcher-gone")`。`pid=0` 视为“未传/存活”。

## 4. 算法选择与理由

- **选 P-256 而不是 Ed25519**：.NET Framework 4.8 的 BCL 没有 Ed25519（只有 CNG/ECDsa）；
  走托管密码库要么引入第三方 DLL（体积、签名、供应链问题），要么自己写常数时间实现（风险更高）。
  P-256 在 `ECDsaCng` 原生支持，Python 侧纯 `hashlib/hmac` 实现即可（零依赖）。
- **编码统一用 P1363**：`sig = r‖s`，各 32 字节；公钥 `base64(X‖Y)` 64 字节。
  拒绝 DER（长度不固定，解析差异是典型跨语言坑）；C# 侧只接受 64 字节，长度不符直接拒。
- **规范化字节**：`json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=False)`；
  C# 侧 `Json.Canonical` 复刻同一套键序/数字/转义规则，跨语言逐字节一致。
- **确定性 nonce**：RFC 6979（HMAC-SHA256），并做 low-S 归一化；用 RFC 6979 §A.2.5 官方向量做已知答案测试。
- **不变项**：六特征指纹、权重（bios_uuid 30 / machine_guid 25 / board_serial 15 / cpu_id 15 /
  volume_serial 10 / mac 5）、阈值 0.85、≥4 组件、锚点（bios_uuid 或 machine_guid 命中其一）。
  （需求文档写 0.70，源码常量为 `FP_MIN_RATIO=0.85`；按“既有约束保持不变”保留 0.85，并在此标注。）

### 旧卡密迁移（作废重发）

1. 旧 Ed25519 卡密**不再兼容**：`verify_blob` 只走 P-256，64 字节的 Ed25519 签名会因曲线不匹配被拒，
   UI/接口给出「签名校验失败」中文原因；服务器 `/api/license/refresh` 返回 **400 + 可读原因**（非 500）。
2. 不保留双算法分支——避免“降级到旧算法”的攻击面。
3. 迁移步骤：用 `launcher\tools\genkey_p256.py` 生成生产密钥对 → 私钥离线保管、写进
   `tools/license_keys\`（gitignore）→ 公钥替换 `core/license.py: PUBLIC_KEY_B64` →
   用 `tools/license_admin.py` 按新库重签，批量重发；旧码在服务端标 `void`。

## 5. 构建

```powershell
powershell -ExecutionPolicy Bypass -File launcher\build.ps1
# 内部等价于：
# csc /nologo /optimize+ /platform:anycpu /utf8output /warn:4
#     /r:System.dll /r:System.Core.dll /r:System.Windows.Forms.dll /r:System.Drawing.dll
#     /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll
#     /target:winexe      /out:launcher\SjzLauncher.exe     /win32manifest:launcher\app.manifest
#     /target:exe /define:SJZ_TEST_BUILD /out:launcher\SjzLauncherTest.exe /win32manifest:launcher\app.test.manifest
```

## 6. 验证结果

| 维度 | 用例 | 结果 |
| --- | --- | --- |
| 编译 | csc 真编译两目标 | 通过，72,704 bytes ×2 |
| 验签 | Python 签 / 启动器验 | 通过（`status=ok`, `fp_score=1.0`, 6 命中） |
| 验签 | Python 签 / Python 验（回环） | 通过 |
| 验签 | 改 payload / 改签名字节 / Ed25519 旧签名 / DER 编码 / 空文件 / 截断 / 32 字节公钥 / 截断签名 | 全部拒绝（`sig_invalid` + 可读原因） |
| 指纹 | 全命中 | 通过（1.0） |
| 指纹 | 单特征缺失（mac，-5） | 通过（0.95），`fp_hits` 少 mac |
| 指纹 | 缺 bios_uuid+machine_guid（45%） | 拒（`fp_mismatch`，附机器码） |
| 指纹 | 仅 3 组件（70%） | 拒（“低于下限 4”） |
| 指纹 | 换机（全组件随机） | 拒（0.0），机器码两次一致 |
| 过期 | 超宽限 / 宽限内 / 长期 | 拒 / `ok_grace` / `ok` |
| 进程 | 单实例（Global 互斥体被占用时第二次拿不到） | 通过 |
| 进程 | 子进程退出被感知 + 退避重启（上限 2） | 3 次 spawn、退出码 3 |
| 进程 | 启动器退出后无孤儿（cmd → timeout.exe 两层） | 两层均死 |
| 进程 | 端到端：启动器 → pythonw(替身) → 握手 → 状态 → 停止 | `ready ok`，`python_pid` 正确，`stopped forced=false` |
| 控制面 | 无 token / 错 token / 正 token | 403 / 403 / 200（`pid` 匹配） |
| 控制面 | Cookie 种下后被接受、非回环 Host 被拒 | 通过 |
| 更新 | 正常包 | `status=updated`，落地 + 备份 + 受保护目录不动 |
| 更新 | 哈希不符 / 签名不符 | `rejected`，零落地 |
| 更新 | 解压路径穿越 `..\evil.txt` | `failed`（“路径穿越”），整包放弃、沙箱外无文件 |
| 更新 | 无卡密时想更新 | 停在卡密步骤，`license=none`，零下载 |

自动化入口：`pytest launcher\tests -q`（36 项）。全部不弹可见窗口、不动真实鼠标；
沙箱用 `SJZ_DATA_DIR` 把卡密/日志/更新暂存挪到 tmp，更新落地在 `<tmp>\sand`。

## 7. 变更清单

- 新增 `launcher\`：`Program.cs`、`LauncherForm.cs`、`LauncherRuntime.cs`、`LicenseCheck.cs`、
  `Fingerprint.cs`、`Crypto.cs`、`Json.cs`、`Native.cs`、`Preflight.cs`、`ProcessHost.cs`、
  `ControlPlane.cs`、`Updater.cs`、`AssemblyInfo.cs`、`app.manifest`、`app.test.manifest`、
  `launcher.config.json`、`build.ps1`、`tools\genkey_p256.py`、`tests\`。
- `core/license.py`：Ed25519 → P-256（P1363），新增 `p256_sign/p256_verify/public_key_b64_from_priv`，
  指纹与阈值常量未动；`verify_blob(blob)` 仍**不接受**外部 `public_key` 参数。
- `tools/license_server.py`：默认开发密钥切到 P-256、health 报算法、`--db` 支持 `SJZ_LICENSE_DB` 环境覆盖。
- `desktop.py`：`--launcher-pid` / `--ctrl-token`（SUPPRESS 帮助）、父进程 watcher；有 launcher 时跳过自提权。
- `core/server.py`：`set_control_token`、`_auth_gate`、`/api/ctrl/status`、`/api/ctrl/stop`、
  页面种 Cookie、`_shutdown_soon`、非回环 Host 拒绝。
- `tests/test_license*.py`：fixture 随算法迁移到 P-256（含 RFC 6979 已知答案、旧算法拒绝、长度严格性），
  顺带修正依赖健康检查旧返回体的一条断言。

## 8. 待办

1. **代码签名证书**：`SjzLauncher.exe`、`interception.sys`、`interception.dll` 都要 Authenticode 签名，
   否则 SmartScreen/杀软会拦；驱动还需 WHQL/EV 证书与时间戳。
2. **安装器**：负责 `runtime\` 释放、驱动安装/卸载、开机启动、卸载残留（`%LOCALAPPDATA%\SJZ-Control`）、
   首次 UAC 只提一次（`requireAdministrator` + 安装器权限继承）。
3. **Win7 目标机**：Win7 最高只有 .NET 4.6.2/4.5（4.8 需 Win7 SP1 + KB 补丁）。
   降级方案：`/target` 不变，去掉对 4.8 专属 API 的使用（当前代码只用 4.5 级 API 与 `ZipFile`），
   用 4.6.2 编译器重编；`app.manifest` 的 `longPathAware`（Win10 1607+）降级时删除；
   `WinVerifyTrust` 与 UAC 清单在 Win7 可用。代价：4.6.2 下 TLS 1.2 需显式
   `ServicePointManager.SecurityProtocol`，更新链路要补这一行。
4. **发布密钥替换**：当前 `PUBLIC_KEY_B64` 配对的是 `dev_signing_key_p256.txt`（已 gitignore）。
   上线前必须换成生产公钥，否则拿到开发私钥即可自签卡密。
5. **并发隔离（另一路在修）**：本实现的运行路径全部来自参数/环境
   （`--db`、`--port`、`SJZ_DATA_DIR`、`SJZ_LICENSE_DIR`、`LOCALAPPDATA`），
   唯一一处固定临时名（更新 zip 原在 `%TEMP%`）已改为 `DataDir` 内并带 pid。
   `tests\` 侧仍建议用独立 `--db`/`--port`/env，避免并行互踩。
