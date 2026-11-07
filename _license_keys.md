# 授权链密钥：现状、改动与运维

> 结论先行：`PUBLIC_KEY_B64` **没有替换**。本轮修的是「签发服务默认落到仓库内 dev 私钥、自检拿固定公钥硬比」这条会把握手搞错的链路。

## 0. 当前生效的公钥（仓库里只有这一把）

```
PUBLIC_KEY_B64 =
VtWMhwsKS9K8VVHWI7ITr9O3jzWpPkcojeCvU8JGeGnRfVFE/FsxMn8OBxUuNZjAEPokQD+Iyd/YiPax/yZjWw==
```

- 编码：`base64(X || Y)`，X/Y 各 32 字节大端，合计 64 字节；曲线 P-256；签名 `base64(r || s)`，各 32 字节（IEEE P1363），未改协议。
- 指纹：`sha256(公钥字符串)[:16] = 51fa8e4069e22741`
- 同一行同时存在于 `core/license.py` 与 `launcher/Crypto.cs`（`PublicKeyB64`），换钥必须两处一起改并重编启动器。

## 1. 落库前的现状（实测，不是推测）

| 项 | 实测结果 |
| --- | --- |
| `core/license.py: PUBLIC_KEY_B64` | 与 `%LOCALAPPDATA%\SJZ-License-Keys\prod_signing_key_p256.txt` 派生出的公钥**逐字节相同**，sign → verify 往返通过 |
| 这把私钥的来历 | 原为仓库内 `tools/license_keys/dev_signing_key_p256.txt`（在 `.gitignore` 内、未被 git 跟踪），文件头自述是「开发/联调密钥，正式发布前必须重新生成」 |
| 仓库内是否还有私钥 | 无。`tools/license_keys/` 已整个移除；全仓 26254 个文件扫描私钥种子 **0 命中** |
| `tools/license_server.py` 旧默认 | 指向仓库内 dev 钥；启动自检拿 `lic.PUBLIC_KEY_B64` 当验签钥，私钥一换就启动失败，而且无法区分「配错」和「就应该用 dev 跑联调」 |
| 旧 Ed25519 钥 | `%LOCALAPPDATA%\SJZ-License-Keys\obsolete\dev_signing_key_ed25519.txt`，换算法后已无用，仅留档 |

要点：能与编译期公钥配对的这把钥，历史上就叫 **dev**。也就是说，客户端一直信任的是「dev 钥」，而不是一把独立生成、从未进过仓库的生产钥。本轮按生产路径收口，但**建议尽快轮换**（见 §4）。

## 2. 生产私钥在哪、怎么放

- 位置：`%LOCALAPPDATA%\SJZ-License-Keys\prod_signing_key_p256.txt`（即 `core/license.py: default_signing_key_path()`）。
- 覆盖方式（优先级从高到低）：
  1. `tools/license_server.py --key-file <path>`（`--key` 是同一参数的别名）；
  2. 环境变量 `SJZ_SIGNING_KEY=<path>`；
  3. 上面的默认路径。
- 目录 ACL 已收紧为「仅当前用户」：`icacls` 输出只剩 `<用户>:(OI)(CI)(F)`。
- 文件权限：写入时 `0600`，`tools/keygen.py` 用 `O_EXCL` 新建，**不覆盖任何已存在的私钥**。
- 私钥**永不**进仓库、进安装包、进日志、进测试夹具、进报告。

## 3. 本轮改了什么

### 3.1 `tools/keygen.py`（新增）

生成 **dev** 密钥对，不碰生产公钥：

```
.venv\Scripts\python.exe tools\keygen.py                 # 默认写仓库外 dev_signing_key_p256.txt
.venv\Scripts\python.exe tools\keygen.py --out D:\k\dev.txt
.venv\Scripts\python.exe tools\keygen.py --print-only    # 只打印公钥，不落盘
```

- 拒绝写到仓库内（退出码 2）；拒绝覆盖已存在文件（退出码 3）。
- 屏幕只输出公钥、`sha256[:16]` 指纹和一句「当前使用 dev 密钥：与编译期 PUBLIC_KEY_B64 不配对，它签出的卡只能用于开发环境」。
- 私钥只写文件，不打印。

### 3.2 `tools/license_server.py`

- 新增 `--key-file`（`--key` 别名）；默认改为 `SJZ_SIGNING_KEY` 或仓库外的 `prod_signing_key_p256.txt`，**不再指向仓库内 dev 私钥**。
- `Store.selftest()` 改为：用**本次实际加载的私钥**派生公钥，再做签 → 验往返。不再拿固定 `PUBLIC_KEY_B64` 当验签钥匙。
- 与编译期公钥不配对时：`store.dev_key = True`，stderr 告警「当前使用 dev 密钥（指纹 …），与内置公钥不配对，签出的卡只能用于开发环境」。
- `/api/license/health` 增加 `key_mode: "prod" | "dev"`。
- 私钥文件缺失 → `找不到签发私钥：<path>，用 --key-file 指定，或设 SJZ_SIGNING_KEY。`（不回显密钥）。

启动输出形如：

```
许可服务已启动 http://127.0.0.1:8093  db=...
签名算法 ECDSA-P256-SHA256（P-256 / r||s），私钥 <path>
本次私钥派生公钥指纹 xxxx；内置公钥指纹 51fa8e4069e22741
模式：prod（与内置公钥配对）
```

### 3.3 `portal/` 签发侧

- `portal/config.py`：`signing_key_file` 默认改为 `core_license.default_signing_key_path()`（仓库外 / `SJZ_SIGNING_KEY`），不再指向 `tools/license_keys/`。
- `portal/signing.py`：`Signer._selftest()` 同样改为「本次私钥派生公钥做往返」，不配对时 `ready=True` 但 `dev_key=True` 并 `logging.warning`；自检失败才停用发卡。
- `portal/cli.py`：批量发卡前若 `signer.dev_key`，打印「当前使用 dev 密钥，签出的卡只能用于开发环境」。
- `portal/routes_public.py`：健康检查增加 `signing_key_mode`。

### 3.4 启动器

- `launcher/Crypto.cs` 的 `PublicKeyB64` **保持原值**（与 `core/license.py` 逐字符一致），已按原钥重编 `SjzLauncher.exe` / `SjzLauncherTest.exe`。
- 仅 **测试构建**（`#if SJZ_TEST_BUILD`，即 `SjzLauncherTest.exe`）允许 pytest 用 `SJZ_LAUNCHER_PUBKEY_B64` 注入本轮临时公钥。发布版 `SjzLauncher.exe` 没有这条分支（二进制里也搜不到该字符串，已实测）。

### 3.5 其它

- `tools/license_admin.py`：`gen-key` 改为调用 `tools/keygen.py`（P-256），`DEFAULT_KEY` 改仓库外路径；删掉不再使用的 Ed25519 依赖。
- 测试：所有夹具私钥改为每轮 `core.license.new_private_key_hex()` 随机生成、只在内存里，跑完随进程销毁；新增 `tests/test_license_keys.py`；`tests/test_license_server.py` 增加「自检用本次私钥」「显式 key 文件」「默认不在仓库」「health=prod」用例。
- 仓库内 `tools/license_keys/` 已删除（旧 dev 钥已移到仓库外，见 §1）。

## 4. 换钥与旧卡迁移（两句话流程）

1. **重签**：用 `tools/keygen.py --out <新路径>` 生成新钥 → 把新公钥写进 `core/license.py: PUBLIC_KEY_B64` 与 `launcher/Crypto.cs: PublicKeyB64` 并重编启动器 → 签发服务用 `--key-file <新路径>` 重启 → 用新钥重签存量卡。
2. **旧客户端提示升级**：换钥后旧卡与旧签名一律失效，客户端验签失败时提示「授权已失效，请升级到最新版本并重新激活」，后台同步作废旧卡批次。

换钥要同步的 4 处（缺一处就有一端不认卡）：

| # | 位置 | 内容 |
| --- | --- | --- |
| 1 | `core/license.py` | `PUBLIC_KEY_B64` |
| 2 | `launcher/Crypto.cs` | `PublicKeyB64` + 重编 `SjzLauncher.exe` |
| 3 | portal | `signing_key_file`（默认路径 / `SJZ_SIGNING_KEY`）指向新私钥 |
| 4 | `tools/license_server.py` | `--key-file` 或 `SJZ_SIGNING_KEY` 指向新私钥 |

备份：私钥 + 公钥指纹一起备份到离线介质两份，登记“生成日期 / 指纹 / 保管人 / 作废日期”。**私钥丢失 = 无法再签新卡；私钥泄露 = 任何人都能自签。**

## 5. 验收（逐条实测）

| 验收项 | 结果 |
| --- | --- |
| 签名 → 验签往返 | PASS：`sign_blob` → `verify_blob` 返回 `lic_id` |
| 改一个字节 | PASS：`sig_invalid`，文案「签名校验失败（许可被改过或公钥不匹配）」 |
| 截断签名（32 字节） | PASS：`签名长度必须是 64 字节（r‖s）` |
| DER 编码签名 | PASS：同上，长度校验直接拒 |
| 32 字节公钥 | PASS：`公钥长度必须是 64 字节（X‖Y）` |
| 128 字节公钥 | PASS：同上 |
| 同一套密钥互通 | PASS：签发服务 HTTP `/api/license/activate` 签出的卡被 `core.verify_blob` 接受，`health.key_mode=prod` |
| 换一套密钥必不通 | PASS：dev 钥可启动但 `dev_key=True`、`health.key_mode=dev`，它签出的卡被客户端以 `sig_invalid` 拒绝 |
| `verify_blob` 无测试钩子 | PASS：源码不含 `_test_hooks_allowed / environ / getattr / frozen`，签名只有一个参数 `blob`；`sys.frozen=True` + 注入 5 个环境变量仍拒签 |
| 私钥不进仓库 | PASS：全仓 26254 文件扫描 0 命中；`tools/license_keys/` 不存在；`git status` 无密钥文件 |
| 私钥不进日志/异常栈 | PASS：`keygen` 输出无种子；私钥缺失/换钥失败路径只报路径与原因；测试断言输出中无种子串 |
| 全量 `pytest tests launcher\tests` | 见下 |

全量测试结果：`pytest tests launcher\tests -q` → **2126 passed, 19 skipped, 2 xfailed, 0 failed**（498s）。
本轮同机还有其它分支在并行跑 pytest，CPU 100%，`portal/tests/test_perf.py` 的 p95<100ms 断言会抖（实测 avg 26ms、p95 114ms）；它不在本条验收命令里，且与许可改动无关。

---

# 6. 泄露核实与处置（2026-11-07）

## 6.1 结论

**判定：未泄露（未进过任何 commit，也未出现在任何远端仓库）。不执行换钥。**

依据（三条互相独立）：

1. 本机 `dafors` 的 33 个提交、全部 git 对象（826 个 blob 及全部 commit/tree，含 reflog / stash / 悬空对象）中，私钥种子、种子前 16 位、`dev_signing_key_p256`、`PRIVATE KEY` 头部**全部 0 命中**；
2. 远端 `c2634682550-cmd/sjz` 只有 `main` 一个 ref、无 tag、SHA 与本机 `origin/main` 同为 `5eef554`，历史即本机历史；
3. `c2634682550-cmd/delta-ammo-bot`（1 commit / 413 blobs）与唯一 fork `2233admin/sjz`（10 commits / 63 blobs）镜像克隆后全对象扫描 **0 命中**。

另一处必须记录的发现：**仓库工作树里的 `perf/` 快照目录存在 17 份私钥明文副本**（未跟踪、但此前未被忽略）。它们从未进过 git，但 `git add -A` 会直接把它们提交上去。本轮已删除并加固忽略规则，详见 6.4。上一节 §1/§5 里「全仓 26254 文件扫描 0 命中」的结论**不成立**，那次扫描漏掉了 `perf/` 快照。

**因此：不换钥、不改 `PUBLIC_KEY_B64`、不重编 exe、不作废存量卡、不需要 `git filter-repo`。**

## 6.2 被核实的密钥

| 项 | 值 |
| --- | --- |
| 私钥文件 | `%LOCALAPPDATA%\SJZ-License-Keys\prod_signing_key_p256.txt`（655 字节，sha256 `5AFCA5BC…F3BEE`） |
| 种子 | 文件最后一行 64 位 hex（本报告不回显） |
| 派生公钥 | `VtWMhwsKS9K8VVHWI7ITr9O3jzWpPkcojeCvU8JGeGnRfVFE/FsxMn8OBxUuNZjAEPokQD+Iyd/YiPax/yZjWw==`，与 `core/license.py: PUBLIC_KEY_B64` **逐字节相同** |
| 指纹 | `sha256(公钥)[:16] = 51fa8e4069e22741` |
| 目录 ACL | `26346:(OI)(CI)(F)`；文件 `(I)(F)` —— 仅当前用户 |
| 历史身份 | 原 `tools/license_keys/dev_signing_key_p256.txt`（文件头自述开发钥），现已移到仓库外 |

## 6.3 证据命令与输出摘要

### 本地历史（`C:\Users\26346\Desktop\project\dafors`）

```
git rev-list --all --count                     → 33
git branch -a                                  → main + origin/fork/parent 的 main（无其它分支）
git tag                                        → （空）
git stash list                                 → （空）
git log --all --full-history -- tools/license_keys   → （空，路径从未入库）
git rev-list --all --objects | grep license_keys     → （空）
git fsck --unreachable --dangling              → 2 commit / 2 tree / 1 blob
```

逐个提交 × 每行 grep（种子、种子前 16 位、`dev_signing_key_p256`、`BEGIN [EC ]PRIVATE KEY`）：

```
for c in $(git rev-list --all): git grep -n -I -e <seed> -e <seed[:16]> \
    -e dev_signing_key_p256 -e 'BEGIN EC PRIVATE KEY' -e 'BEGIN PRIVATE KEY' $c
→ NO_HIT_IN_COMMITS
```

全对象扫描（`git cat-file --batch-all-objects`，含不可达对象）：

```
BLOB_COUNT=826 → NO_HIT_IN_ALL_BLOBS
```

不可达对象逐个核对：`a3e07e4`（stash 提交 `On main: tmp-probe`）、`5e7f5ff`（index 提交）、
两个 tree、一个 22911 字节 blob（内容为 `core/ocr_service.py`）——均与密钥无关。

### 远端

`gh` 未登录（`gh auth status` → not logged into any GitHub hosts），改用 GitHub REST API（匿名读公开仓库）+ `git clone --mirror` 全对象扫描。

| 仓库 | 可见性 | 默认分支 | 提交数 | 全对象命中 | fork |
| --- | --- | --- | --- | --- | --- |
| `c2634682550-cmd/sjz` | public | main @ `5eef554` | 33（与本机同） | 0 | 1 个：`2233admin/sjz` |
| `c2634682550-cmd/delta-ammo-bot` | public | main @ `d0bb5c6` | 1 | 0（413 blobs） | 0 |
| `2233admin/sjz`（fork） | public | main @ `da40d24` | 10 | 0（63 blobs） | 0 |

三个远端均**无 tag**，只有一个 `main`。默认分支树递归枚举（584 / 772 / 38 个文件，`truncated=false`）中无 `license_keys`、`dev_signing*`、`*.pem`、`*p256*` 路径。

### 工作树 / 暂存区

```
git ls-files | grep -E 'license_keys|key|pem|p256'   → 仅 core/hotkey.py、tests/test_hotkey_start.py
git diff --cached --name-only | grep -i key          → （空）
```

全仓逐字节扫描（17380 个文件，排除 .git/.venv/.venv-vl/缓存）：

- 清理前：**34 份私钥明文** —— 17 份 P-256（`dev_signing_key_p256.txt`，哈希全为 `5AFCA5BC…F3BEE`，与仓库外生产钥**文件哈希完全相同**）+ 17 份旧 Ed25519（`dev_signing_key.txt`，`A116F510…1A139`），全部位于未跟踪的 `perf/` 快照目录。
- 清理后：**0 份私钥明文**；剩余 44 处命中均为「文件名字符串」出现在代码/文档/测试里（如 `tools/keygen.py`、`tests/test_license_keys.py`、`perf/**/license_server.py`），不含种子或 PEM 头部。

## 6.4 本轮执行的改动

1. 删除 `perf/` 下 17 份 P-256 私钥副本 + 17 份旧 Ed25519 副本，并清掉随之变空的 `tools/license_keys/` 目录（17 个）。
   - 只删私钥文件，`perf/` 的基准快照、补丁、报告原样保留。
   - `perf/ocr/SNAPSHOT.sha256` 仍列有这两个文件的哈希行（只是摘要，不是密钥材料），未改动。
2. `.gitignore` 增加一条全局忽略：

```
**/tools/license_keys/
```

   （原有 `/tools/license_keys/` 只匹配仓库根，覆盖不到 `perf/snapshots/*/tools/license_keys/`；实测 `git check-ignore -v perf/base/tools/license_keys/x.txt` 命中第 63 行。）

3. 未改动：`core/license.py`、`launcher/Crypto.cs`、`portal/signing.py`、`portal/config.py`、`tools/license_server.py`、`tools/keygen.py`；未重编任何 exe。

## 6.5 验收

```
.venv\Scripts\python.exe -m pytest tests launcher\tests -q
→ 2133 passed, 19 skipped, 2 xfailed, 8 warnings in 528.20s
```

（同机另一个会话在并行跑同仓 `pytest tests`，CPU 满载；本次 0 failed。）

## 6.6 仍需人工确认 / 可选动作

| 项 | 是否必须 | 说明 |
| --- | --- | --- |
| 强制推送 / `git filter-repo` | **不需要** | 密钥从未进入任何 commit，历史里没有可清除的对象 |
| 换钥、重编 exe、作废旧卡 | **不需要**（按「未泄露」判定） | 见 6.1 |
| 通知已购用户 | **不需要** | 未换钥，客户端行为不变 |
| `gh auth login` | 建议 | 本轮用匿名 API + 镜像克隆完成核实；后续要在 CI 里复跑需要 token |
| 是否把 `/perf/` 整体加入 `.gitignore` | 可选 | `perf/` 是整目录未跟踪的基准快照，内含仓库多份拷贝；只忽略密钥目录已足够，整目录忽略更省心 |
| 主动轮换（业务判断） | 可选 | 这把「生产钥」历史身份是 dev 钥，且明文在 `perf/` 里躺过一段时间。若该目录曾被整体打包外发/上传网盘，应按已泄露处理并换钥。本轮未发现 git 或远端侧证据，故未换 |

---

# 7. 私钥闸门（pre-commit + 打包前扫描，2026-11-07）

## 7.1 扫什么

`tools/scan_secrets.py` 认四类信号，任一命中即退出码 1：

| 规则 | 判据 |
| --- | --- |
| `pem` | PEM / OpenSSH / PuTTY 私钥头（头在行首，或嵌在代码字符串但下一行是 base64 正文） |
| `seed_hex` | 32 字节私钥种子，即 64 位十六进制（P-256 与 Ed25519 同形） |
| `known_hash` | 已知生产私钥的精确哈希：种子 sha256 `fcac1235…`、整文件 sha256 `5afca5bc…`（不做上下文判断） |
| `filename` | `signing_key*`、`*_key.txt`、`id_rsa`、`license_keys/` 等名字 |

- 输出只有路径、行号、规则名和 sha256 前 8 位指纹，**不回显密钥内容**。
- 已知豁免：旧快照 tests 里 `SEED_HEX = "a3b1c8d5…"`（注释写明「测试专用 P-256 标量」）按 sha256 记在 `KNOWN_TEST_SEED_SHA256`，不算命中；其它任何种子照旧命中。
- 两种模式：`--staged` 只读暂存区（`git show :path`）；`--all` 扫整个工作树（含未跟踪的 `perf/` 快照，历史泄露就在那里）。退出码：0 干净 / 1 命中 / 2 用法或 git 错误。
- `--all` 不进 `.git`、`.venv*`、`build`、`dist*`；单文件内容上限 4MB，超限只按文件名判定。

## 7.2 装钩子

```
python tools/install_hooks.py
```

- 只写 `.git/hooks/pre-commit`（源模板 `tools/hooks/pre-commit`，LF 写入、置可执行位）；**不改任何 git 配置**。
- 已有钩子先备份成 `pre-commit.bak.<时间戳>`，不静默覆盖别人的钩子。
- 若仓库设了 `core.hooksPath`，脚本会告警：git 会忽略 `.git/hooks/`，钩子不生效。
- 钩子不进版本库，**换 clone 后要重装一次**。

自检 / 验证：

```
python tools/scan_secrets.py --staged
git add <一份假私钥> && git commit    # 应被阻断，并打印命中项
```

## 7.3 绕过（以及绕过时必须做什么）

git 原生绕过：`git commit --no-verify`。只在确认误报时用，并且：

1. 看命中是什么：`--staged` 输出给了路径、行号、规则；
2. 若是新的良性模式（例如另一个测试夹具种子），把它的 sha256 加进 `KNOWN_TEST_SEED_SHA256` 并注释来源 —— **不要放宽规则**；
3. 若是真钥：先移出仓库（`%LOCALAPPDATA%\SJZ-License-Keys\`），`git restore --staged <path>`；再确认它是否进过历史（`git log --all --full-history -- <path>`），进过就按泄露处置（换钥 + 重签存量卡，见 §4）；
4. 提交说明里写清为什么用了 `--no-verify`。

`--no-verify` 只跳过提交钩子，**跳不过** build.bat / CI 的 `--all`。

## 7.4 CI / 打包

`build.bat` 第 `[4/7]` 步：`uv run python tools\scan_secrets.py --all`，命中即跳到 `:nosecrets` 中止，位置在 version sync / 打包审计之后、pytest 与 pyinstaller 之前。

CI（没有 build.bat 的环境）一行：

```
python tools/scan_secrets.py --all
```

GitHub Actions 片段：

```yaml
- name: Secret gate
  run: python tools/scan_secrets.py --all
```

换生产钥后：同步更新 `KNOWN_SEED_SHA256` / `KNOWN_KEY_FILE_SHA256`，否则新钥只剩 PEM 与种子规则兜底、旧钥的精确哈希失效。

测试：`python -m pytest tests/test_scan_secrets.py -q`（覆盖 PEM、hex 种子、指定哈希、可疑文件名，以及公钥/源码常量/内存临时钥的不误报，和 `--staged` 阻断→撤下→放行）。

