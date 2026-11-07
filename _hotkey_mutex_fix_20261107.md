# 热键跨实例互斥修复报告（2026-11-07）

## 1. 缺陷与根因

生产走的是 `HookHotkey`（`core/server.py:1386` `_make_hotkey` 优先低级钩子），而
低级键盘钩子（`SetWindowsHookExW`）**没有任何独占语义**：两个实例各装一个都会
「成功」，按键进入系统后两个钩子各收一份，两个进程都执行抢购。

对照 `Hotkey`：`RegisterHotKey` 对同一 (修饰键, 键码) 是系统级独占，第二次注册
以 `1409` 失败，所以那条路本来就是对的。缺的是给钩子补一层同等的互斥。

## 2. 机制选择

**命名互斥体 `CreateMutexW` + `ERROR_ALREADY_EXISTS`**（`core/hotkey.py:62`
`_HotkeyMutex`）。理由：

- `CreateMutexW` 的「首次创建 / 已存在」判定是内核原子操作。两个实例同时注册，
  必然一个拿到新对象（`last_error=0`）、另一个拿到 `183`，不存在两边都成功的窗口。
- **不持有所有权、也不 `WaitForSingleObject`**：这里只需要「名字归谁」，不需要
  串行化。`bInitialOwner=False`，谁都不 `ReleaseMutex`，只 `CloseHandle`。
  因此不存在「abandoned mutex」判定的歧义——句柄还在，就一定有活着的实例在用这个键。
- **生命周期**：句柄存在对象里，`release()` 关掉。进程正常退出、异常退出、
  被 `TerminateProcess` 强杀，内核都会回收它所有句柄，名字随最后一个句柄消失而
  销毁 → **不会留下永久占位**。`release()` 可重入（`core/hotkey.py:318` / `:477`），
  stop 后重新 start 正常。
- `_HotkeyMutex.acquire()` 在探测失败时会立刻 `CloseHandle` 那个探测句柄，
  避免测试方自己变成新的占位者。
- `ctypes` 必须给 `CreateMutexW` 声明 `restype=HANDLE`（`core/hotkey.py:27`），
  否则默认按 `c_int` 截断 64 位句柄，`CloseHandle` 拿到错值，名字永远放不掉。

**命名规则**（`core/hotkey.py:44`）：

```
Local\dafors.hotkey.{mods:x}.{vk:02x}      # 实测：F12 -> Local\dafors.hotkey.0.7b
```

- 用 `Local\` 而不是 `Global\`：热键本来就是「每个登录会话一份」，和
  `RegisterHotKey` 的语义一致；也不需要 `SeCreateGlobalPrivilege`。
- 名字取 `parse()` 之后的数值，`"f8"` / `"F8"` / `" F8 "` 归一成同一个名字，
  写法差异绕不过互斥。

**两条实现共用同一个名字**：`HookHotkey.start()`（`core/hotkey.py:234`）和
`Hotkey.start()`（`core/hotkey.py:409`）都先抢名字再干活。这样跨实现也互斥，
顺带堵死了 `_make_hotkey` 回退路径的双触发漏洞（见第 5 节）。

## 3. 失败文案与返回值（统一）

两条路的 `start()` 都返回 `(ok, msg)`，失败时 `msg` 用同一个模板
（`core/hotkey.py:57` `_msg_occupied`）：

| 场景 | 返回值 | 文案 |
| --- | --- | --- |
| 钩子实例撞上另一个实例 | `(False, msg)` | `热键 F12 已被占用（错误码 183）——另一个实例正在用它，先关掉或换一个键` |
| RegisterHotKey 撞上占用 | `(False, msg)` | `热键 F12 已被占用（错误码 1409）——另一个实例或别的程序正在用它，先关掉或换一个键` |
| 互斥体本身创建失败（少见） | `(False, msg)` | `热键 F12 跨实例占位失败（错误码 N）——先关掉别的实例再试` |

结构一致（`热键 X 已被占用（错误码 N）——…，先关掉或换一个键`），只有错误码和
「谁占的」不同；上层只读 `msg`，不分支。

另外，`status()` 发现钩子线程真的没了（不是 start 进行中）时会主动放名字
（`core/hotkey.py:371`、`:508`），免得一个空壳把别的实例挡在外面、UI 却显示
「热键被占用」。

## 4. 五个场景实测

测试文件 `tests/test_hotkey_mutex.py`（新增）+ `tests/test_hotkey.py`（补假
`CreateMutexW`）；跨进程用 `tests/_hotkey_child.py` 真子进程（真互斥体、真低级钩子）。

```
.venv\Scripts\python.exe -m pytest tests/test_hotkey.py tests/test_hotkey_mutex.py \
    tests/test_hotkey_start.py tests/test_fix_c.py -q
→ 70 passed in 10.99s        # 第二轮补了 2 个用例后
```

独立探针（`_probe_hotkey_mutex.py`，真实钩子 + 真实子进程）测得：

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | 同进程内两个 `HookHotkey` 注册 F12 | 第一个 `(True, "已就绪（低级钩子）")`；第二个 `(False, "…已被占用（错误码 183）…")`，钩子线程未启动，`status().active=False`，`msg` 带原因；第一个 `stop()` 后新对象立刻 `(True, …)` |
| 2 | 两个真实子进程同时注册 F12 | 第一个 `(True, …)`（hello 0.04s），第二个 `(False, "…错误码 183…")`（0.037s），第一个进程仍存活；互斥体名 `Local\dafors.hotkey.0.7b` |
| 3 | 第一个进程被强杀（`TerminateProcess`）后第二个注册 | 强杀前 `(True, …)`；`kill()+wait()` 后新进程 `(True, "已就绪（低级钩子）")`（0.04s），没有残留占位 |
| 4 | 释放后重新注册 | 子进程收到 `stop` 正常退出（`stopped=true`）→ 下一个进程 `(True, …)`；同对象 `stop()→start()` 也是 `(True, …)` |
| 5 | `server._make_hotkey` 在别的实例占着时 | `(False, "…错误码 183…")`，`_conflict=True`，**没有**回退 RegisterHotKey（`"改用 RegisterHotKey"` 未出现）；`status()` 把原因带到 UI |

对应自动化用例：`test_second_hook_instance_in_same_process_is_rejected`、
`test_two_real_processes_second_registration_fails`、
`test_killed_first_process_leaves_no_permanent_placeholder`、
`test_graceful_stop_lets_the_next_process_register`、
`test_real_mutex_blocks_a_second_hook_in_this_process`、
`test_server_does_not_fall_back_when_another_instance_holds_the_key` 等。

## 5. server 调用处 / 前端呈现

`_make_hotkey`（`core/server.py:1386`）改了两点：

1. 跨实例占用时**不回退** `RegisterHotKey`（`core/server.py:1401`）。原来钩子失败
   就无脑回退，而系统热键和对方的钩子会同时响应——比原来更糟，属于「换个实现
   继续双触发」。现在只有钩子**安装**失败（`_conflict=False`）才回退。
2. 失败原因原样保留：`_start_hotkeys` 把 `(ok, msg)` 塞进返回字典，
   `_set_hotkey`（`core/server.py:1198`）据此返回 `{"ok": false, "msg": 原因}`。

前端链路本来就会把 `ok:false` 的 `msg` 显示到设置页提示（`api()` 对 `ok:false`
抛错，`saveHotkey` 的 `catch` 里 `humanize(e)`）；本次补了一行
`web/index.html:3476` 的 `await loadHotkey()`，让失败时顶部状态条也刷新成
「未生效 + 原因」，不会停留在旧的「已生效」。`GET /api/hotkey` 的
`_hotkey_status`（`core/server.py:1184`）本来就透传 `msg`，启动期也会
`print`/`note` 出失败原因。

## 6. 改动清单

| 文件 | 改动 |
| --- | --- |
| `core/hotkey.py` | 新增 `_mutex_name` / `_msg_occupied` / `_HotkeyMutex`；`HookHotkey`/`Hotkey` 的 start 抢名字、stop/线程退出/线程死亡时释放；`CreateMutexW` 签名；第二轮新增 `_mods_now`/`_matches` 精确修饰键匹配、`status()` 异常死亡分支 |
| `core/server.py` | `_make_hotkey` 冲突时不回退；`_set_hotkey` 注册失败回滚到上一次成功配置 |
| `web/index.html` | 保存失败时刷新热键状态条 |
| `tests/test_hotkey.py` | 假 `FakeKernel32` 支持命名互斥体 |
| `tests/test_hotkey_mutex.py`（新） | 16 个用例：同进程、跨进程、强杀、释放、server 呈现、修饰键精确匹配、status 死线程文案、保存失败回滚 |
| `tests/_hotkey_child.py`（新） | 跨进程用例的真子进程脚本 |

## 7. 边界与说明

- **未提交**：没有 `git commit` / `git push`；测试全程无可见窗口（子进程只跑
  钩子/互斥体，不启动 GUI）。
- 回归：`tests/test_server.py`、`test_frontend_static.py`、`test_process_lifecycle.py`
  全过。另跑 `test_console_api.py` 时有一个**与本改动无关的既有失败**
  （`test_a_task_without_the_list_region_is_refused`，期望 400 实际 200），
  属于工作区里其他未提交改动，本次没有触碰那条逻辑。
- ~~已知边界：`F8` 与 `Ctrl+F8` 名字不同、不互斥、按 F8 双触发~~ —— 第二轮已修，
  见第 8 节；现在 `F8` 与 `Ctrl+F8` 是两个互不触发的独立热键。

## 8. 第二轮：复核三项修复

### 8.1 修饰键精确匹配（选 `GetAsyncKeyState` 方案）

**方案**：handler 里比对修饰键状态，而不是把名字里的 mods 归一为 0。

```python
def _matches(mods, vk, kb_vk):
    return kb_vk == vk and _mods_now() == mods      # _mods_now() 读 GetAsyncKeyState 高位
```

**理由（对比两种修法）**：

1. `GetAsyncKeyState` 方案把「修饰键组合」作为匹配条件。`F8` 只在无修饰键时响应，
   `Ctrl+F8` 只在 Ctrl 按下时响应，语义和 `RegisterHotKey` 完全一致（那条路本来就是
   精确匹配 (mods, vk)）。互斥名也继续按 (mods, vk) 生成，不需要改。
2. 「mods 归一为 0」会把 `F8`、`Ctrl+F8`、`Alt+F8`、`Ctrl+Alt+Shift+F8` 全部压成
   同一个名字 `…0.77`。后果是这几种组合**互相阻挡**：用户想同时配「停止=Ctrl+F8」
   「开抢=Alt+F8」直接注册不上，这正是复核提醒要确认的误伤。要避免误伤，就得在
   handler 里重新按修饰键分流——绕一圈还是回到 `GetAsyncKeyState`，所以直接选它。
3. 代价是行为变更：以前按 `Ctrl+F8` 会连带触发 `F8`，现在不会。这是把「组合键
   退化成单键」的 bug 修正掉，属于预期变更。

`_mods_now()` 的位定义与 `parse()` 返回值一致（ALT=1 / CONTROL=2 / SHIFT=4 /
WIN=8），左/右 Win 都归到 WIN 位；读的是高位 `0x8000`（当前按下），不是
「上次读取后按过」的低位。

### 8.2 实测对照（`_probe_round2.py`，真实钩子 + `keybd_event` 注入）

**场景 1：同进程同时挂 `F8` 与 `Ctrl+F8`，各注入一次**

| 版本 | 注入 1 次 F8（2 个事件） | 注入 1 次 Ctrl+F8 |
| --- | --- | --- |
| 修复前（handler 只比 vkCode） | `fires_plain_f8=1  fires_ctrl_f8=1` | —（同理会双触发） |
| 修复后 | `fires_plain_f8=1  fires_ctrl_f8=0` | `fires_plain_f8=0  fires_ctrl_f8=1` |

回调里读到的修饰键位：按下 F8 时 `mods=0`；按下 Ctrl+F8 时 `mods=2`（CONTROL）。
互斥名仍是 `Local\dafors.hotkey.0.77` 与 `Local\dafors.hotkey.2.77`，两个热键可
共存但互不触发。

**场景 2：钩子线程异常死亡后的 `status()`**

故障注入：`PyThreadState_SetAsyncExc(SystemExit)` + `PostThreadMessageW(WM_NULL)`
把钩子线程从 `GetMessageW` 唤醒并抛异常杀死（不走 loop 末尾的清理块）。

| 版本 | `status()` |
| --- | --- |
| 修复前（内部 `_ok=True`、`_msg` 未更新，直接透出） | `{"active": false, "msg": "热键 F8 已就绪（低级钩子）"}` |
| 修复后 | `{"active": false, "msg": "热键 F8 钩子线程已退出（已释放占用）"}` |

修复后同时置 `_ok=False`、释放互斥名；随后同键能被重新占位（`acquire()=True`），
不再出现「名字被空壳占着、界面却说已就绪」。

**场景 3：原配置 F8 → 改成被别的进程占着的 F12 → 保存失败**

子进程真持有 `F12`（`tests/_hotkey_child.py`，`ok=True`）。

| 流程 | 结果 |
| --- | --- |
| 旧流程（等价「只 stop 全部再按新配置注册」，无回滚） | `停止: (False, "热键 F12 已被占用（错误码 183）…")`；盘上配置变成 `F12`；原对象 `status()` = `{"active": false, "msg": "已停止"}` —— **F8 已失效** |
| 修复后 `_set_hotkey({"spec": "F12"})` | 返回 `{"ok": false, "msg": "停止：热键 F12 已被占用（错误码 183）…；已恢复原热键（F8）"}`；盘上配置回到 `F8`；`status()` = `{"active": true, "msg": "热键 F8 已就绪（低级钩子）"}`；再真实按一次 F8，回调触发（`_last_fire` 前进）——**F8 仍可用** |

回滚实现（`core/server.py:1210`）：第一次 `_start_hotkeys` 有失败项时，把 `old`
（进入 `_set_hotkey` 时读到的上一份成功配置）写回并重新注册；恢复也失败时在 `msg`
里追加「恢复原热键也失败：…」，不谎报成功。
