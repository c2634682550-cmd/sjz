# 输入驱动（Interception）安装说明

## 一句话事实（先纠正三个常见误判）

| 误判 | 事实 |
|---|---|
| 驱动文件是 `System32\drivers\interception.sys` | Interception 的驱动是 **`keyboard.sys` + `mouse.sys`** 两个上滤驱动 |
| 服务名是 `interception` | 服务名是 **`keyboard` 和 `mouse`**（`UpperFilters` 绑到 kbdclass / mouclass） |
| 包里应该带 `.sys/.inf/.cat` 三件套 | 官方 v1.0.1 发行包里**没有**独立驱动文件，驱动以 `DRIVER/KBDNT`、`DRIVER/MOUNT` 资源**内嵌在 `install-interception.exe` 里**，安装时释放 |

设备名才是 `\\.\interception00`（dll 里写死的，由上面两个驱动创建）。

## 正确判据（三条全中才算装好）

```powershell
# 1) 驱动文件在位（x64 为 18,536 字节）
Get-Item C:\Windows\System32\drivers\keyboard.sys, C:\Windows\System32\drivers\mouse.sys | Select Name,Length
# 2) 服务存在且在跑
sc query keyboard ; sc query mouse          # 期望 STATE=4 RUNNING
reg query "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96b-e325-11ce-bfc1-08002be10318}" /v UpperFilters
reg query "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e96f-e325-11ce-bfc1-08002be10318}" /v UpperFilters
#    期望含 keyboard,kbdclass / mouse,mouclass
# 3) 程序侧
SJZ-Control.exe --doctor    # [输入驱动] 段显示 内核服务=已安装、驱动文件=存在
```

`sc query interception` 报 1060、`interception.sys` 不存在，**都是正常的**。

## 安装器报 `Could not write to \system32\drivers`

原因有三种，按概率排序：

1. **已装且正在运行**（最常见）：`keyboard.sys/mouse.sys` 已被内核加载、镜像被锁，安装器覆盖写入被系统拒绝。这不是缺件，是幂等冲突——无需重装，先跑上面三条判据确认即可。
2. 目标机把 `System32\drivers` 写了 ACL 限制 / 杀软拦截写入。
3. 安装器没真正提权（它自带 `requireAdministrator`，但被某些沙箱/兼容层降权）。

注意：报错里 `\system32\drivers` **没有盘符**是官方安装器源码里写死的字面串，不是取错系统目录，与中文路径无关（已实测：复制到 `C:\ic\` 重跑同样报错）。

### 真要重装（三步，需管理员）

```powershell
sc stop keyboard ; sc stop mouse                       # 1) 卸下在跑的镜像，解除文件锁
C:\Windows\System32\drivers\keyboard.sys ...           # （可选）确认文件已可写
& "<安装目录>\tools\interception\install-interception.exe" /install   # 2) 提权执行，随后重启
```

第 2 步必须在**提权**上下文执行（`Start-Process -Verb RunAs` 让用户点 UAC）。若 sc stop 返回「标记为删除」，重启后文件即可释放，再跑安装器。

## 分发包里的驱动资源

- 源文件：`tools\interception\install-interception.exe`（470,528 B）
- 版本：Interception **v1.0.1**，来源 github.com/oblitum/Interception releases
- SHA256：`E137863A79DA797F08E7A137280FF2A123809044A888FD75CE9C973198915ABE`
- 内嵌资源：`DRIVER/KBDNT`（键盘上滤）× 5 个版本、`DRIVER/MOUNT`（鼠标上滤）× 5 个版本，各含 x86/x64/IA64
- 构建期由 `tools\check_driver_assets.py` 校验：文件存在、哈希命中、两类资源齐全；缺任一项构建失败。

## 安装包侧行为

- 安装向导有可选任务「安装高速输入驱动 Interception（需要重启，可稍后再装）」。
- 已在位则**跳过**（幂等，不再触发上面那个锁文件报错）。
- 只有本次真的装了驱动，完成页才出现「立即重启电脑」勾选项。
- 失败时弹中文说明（含返回码与下一步），不静默跳过。

## 运行时不装驱动会怎样

程序本身不依赖驱动即可运行，`core/input.py` 有通道选择：

| 通道 | 需要驱动 | 能力 | 代价 |
|---|---|---|---|
| Interception | 是 | 内核级注入按键/鼠标，能稳定打到前台/后台游戏窗口 | 要装驱动、要重启 |
| legacy（SendInput / PostMessage） | 否 | 前台窗口可用；部分游戏只认硬件输入时会丢事件 | 后台注入成功率低 |

驱动缺失时界面会明确写「内核驱动未安装或未生效 —— 请重新运行安装程序并勾选『安装高速输入驱动』，安装完成后重启电脑」，不会只丢一句「输入不可用」。

## 杀软与系统策略

- 驱动安装器会被 Defender/360 提示「内核驱动」风险，属正常，需用户确认；
- 主程序侧不为「降低误报」做混淆/加壳，那反而提高启发式评分；
- 开了内存完整性（HVCI）的机器可能拒绝加载未通过微软签名的驱动，此时只能走 legacy。
