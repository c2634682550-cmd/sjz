# 前端开关回滚用例的全量超时：根因与修法

日期：2026-11-07　目标：`tests/test_frontend_switch_rollback.py::test_pair_inversion_rolls_back_to_disk`
（全量回归里唯一一条负载敏感用例）

## 1. 结论

**产品侧竞态 + 夹具等错信号，两者叠加**：

- 产品：`boot()` 收尾**无条件**执行 `go('run')`。启动要跑 6 个 loader + `renderAll()`，机器一忙就是几秒；用户/夹具在这段时间切到「设置」，收尾把页签**抢回「运行」**，`#page-set` 重新 `hidden`，页内 `.setnav` 按钮全部不可见。
- 夹具：等的是 `#sbConnTxt === '已连接'`，而它在**第一次 `api()` 成功**（`api()` → `noteOk()` → `setConn(true)`）就置上，此时 `boot()` 远未结束；后续「等元素存在」的等待在 `loadSwitches()` → `renderSettingsSwitches()` 阶段就满足，同样早于 `renderAll()` / `go('run')`。于是夹具**稳定地**踩进「启动未完成」这个窗口。
- 单跑为什么绿：boot 全链 < 500ms，点击落到 `go('run')` 之后，窗口根本不存在。

## 2. 超时的具体断言（不是 `to_be_visible`）

全量日志：`%TEMP%\dafors_full_seed20261107.log`（2026-11-07 22:01，`3 failed, 2322 passed, 20 skipped, 2 xfailed in 935.06s`）

```
tests/test_frontend_switch_rollback.py:255: in _ensure_pane      ← 改前行号，现为 L268
    page.click('.setnav [data-setpane="fire"]')
E   playwright._impl._errors.TimeoutError: Page.click: Timeout 30000ms exceeded.
    Call log:
      - waiting for locator('.setnav [data-setpane="fire"]')
      -   locator resolved to <button ... data-setpane="fire" aria-selected="false">
      - attempting click action
      -   element is not visible
      ...（30 秒内每一次重试都是 "element is not visible"）
```

同一份全量里的隔离锁统计：`heavy 锁 247 次获取、0 次超时、总等待 8.8s`（`p95 ≈ 0`）。
→ **不是抢锁、也不是 CPU 被抢占到点不动**；CPU 挤占只是把 boot 收尾的时间拉长，把窗口从 ~0 撑到几秒，让夹具必然踩中。

另一份更早（20:14，隔离锁上线前）的全量日志里，同一根因的另一副面孔：

- `test_frontend_switch_rollback.py:311` `locator.wait_for` 10s → `#swFire [data-sw="fire.hz"]` 持续 `hidden`
- `#tab-set` / `.setnav [data-setpane=...]` 单个 `Page.click` 30s —— 都是「设置页被收尾抢回运行页」的同一后果。

## 3. 因果复现（探针，headless、隐藏窗口）

`%TEMP%\dafors_fe_race_probe.py`：起进程内服务端 → 无头 Chromium → 记录 DOM 页签迁移序列。

| 组 | 迁移序列（相对 domcontentloaded） | 结果 |
|---|---|---|
| 对照（空载） | `page-run(163ms) → page-set(509ms)` | 停在设置页，`.setnav [data-setpane="fire"]` 可见 |
| 实验组（boot 的 6 个 loader GET 各延迟 1.5s，等价全量负载） | `page-run(128ms) → page-set(183ms) → **page-run(1762ms)**` | `#page-set.hidden=true`，分组按钮不可见 = 与全量失败栈逐字对齐 |

实验组说明：`已连接` 在 0.2s 内就置上，而 `go('run')` 在 1.76s 才来 —— 夹具等的那个信号确实早于启动完成 1.5 秒以上。

修复后同一探针（延迟组）：`page-run(128ms) → page-set(183ms)` 不再回退，`body.dataset.booted === '1'`。

## 4. 修法

### 4.1 产品（`web/index.html`，回滚逻辑所在前端文件）

```js
async function boot() {
  ...
  S.booting = true;
  const navToken = S.navToken;   // ① 启动期间用户切过页，就不许收尾把人抢回来
  ...
  renderAll();
  if (S.navToken === navToken) go('run');   // ② 从无条件 go('run') 改为条件执行
  document.body.dataset.booted = '1';       // ③ 启动全部完成的确定性信号
  startPolling();
```

- ① ②：语义收敛为「启动收尾只在用户没动过导航时才回运行页」。正常用户（启动完成后才点页签）行为不变；只有「启动中切页」这一种情况不再被弹回。
- ③：给测试/外部脚本一个**真正代表启动完成**的标记。原先把「已连接」当就绪信号，本质上测的是网络第一次成功，不是 UI 就绪。

### 4.2 夹具（`tests/test_frontend_switch_rollback.py`）

1. 新增 `_wait_booted(page)`：等 `document.body.dataset.booted === '1'`，替换三处等 `#sbConnTxt === '已连接'` 的位置（module fixture、`_new_page`、`test_pair_inversion` 的 `page.reload()` 之后）。
2. `_ensure_pane()` 去掉 `wait_for_timeout(80)`，改为等两个确定性状态：`#page-set` 可见 → `#pane-<id>` 可见。
3. module fixture / `_new_page` 里的 `wait_for_timeout(300/200)` 同样换成等 `#page-set` 可见。
4. 没有加任何 timeout 数值、没有 skip、没有 xfail。

> 注意：`_ensure_pane` 的第二个等待用 `(id) => ...` + `arg=pane`。写成 `([id]) => ...` 会把字符串 `"fire"` 当数组解构成 `"f"`，导致 `#pane-f` 找不到 —— 中途踩过这个坑，已修正。

## 5. 验证与带载对照

机器上同时跑着：另一会话的全量 `pytest tests/ -q`、`tools/soak_ocr.py`（2h 常驻）、以及其它测试子集。

| 轮次 | 时刻 / 耗时 | 同机负载 | 结果 | 本条 |
|---|---|---|---|---|
| 改前全量（22:01） | 935.06s | 另一全量 + soak | `3 failed, 2322 passed, 20 skipped, 2 xfailed` | **红**（`Page.click 30s`） |
| 改后全量 #1 | 613.66s | load 68%，python 20 个，另一全量在跑 | `0 failed, 2332 passed, 20 skipped, 2 xfailed` | 绿 |
| 改后全量 #2 | 671.03s | load 71%，python 14 个，soak 在跑 | `1 failed, 2331 passed`（见下，非本条） | 绿 |

- 全量 #1 隔离锁：`heavy 604 次、0 超时`，最长等锁 9.3s（`spawn` 组，与本条无关）。
- 全量 #2 隔离锁：`heavy 604 次、0 超时`，最长等锁 21.0s（`ocr` 组）。本条 `playwright` 组等锁 ≤ 0.1s。
- 单跑 3 次（改后）：`10.57s / 4.52s / 4.22s`，3/3 绿；改前单跑 6.81s 也绿 —— 说明这条的触发条件确实是「同机负载把 boot 拖过夹具的点击」。
- 模块内全量：`15 passed`（改后两次复跑均 34s 内）。

全量 #2 的唯一失败不在本次文件边界内：

```
FAILED tests/test_feature_mouse_human.py::test_frontend_control_reaches_backend
  AssertionError: 子脚本 <string>:48  page.wait_for_function Timeout 30000ms exceeded
```

- 该子脚本在 `_CHILD` 里等 `#toastHost` 出现「上限」字样；它的 `applySwitch` 客户端超时是 **2s**，负载下 POST 一旦超过 2s，toast 文案就变成「保存超时：……」而不是「上限」，于是白等 30s —— 属于那个用例自身的「2s 客户端超时 vs 30s 文案等待」负载敏感，与本次改动无关（本次只动 `boot()` 收尾导航与 `data-booted` 标记，没碰 POST / toast / applySwitch）。
- 单独复跑该用例 3 次：`9.45s / 9.83s / 7.64s`，3/3 绿。

## 6. 文件边界与 hash 核对

只改了这两个文件（其余：`core/records.py`、`core/server.py`、`core/config.py`、`tests/conftest.py`、
`tests/test_poll_path.py`、`tools/soak.py` 均未触碰）。

| 文件 | 时点 | sha256 |
|---|---|---|
| `web/index.html` | 改前（任务开始时记录） | `912DBB0D1A6C52A1B300988A2DE63183CD13A835C11A3430FBE5D645B84F4E6C` |
| `web/index.html` | 改后（两次复核未变） | `EF1F2964B2FDB1FD847B29298404AD1C6F61D5E80C07F89B3F957C0DA391E347` |
| `tests/test_frontend_switch_rollback.py` | 反推「本次改动之前」 | `E37831739D704B575FAF6D1C4E438531FE979C8724D172596A594E939D198665` |
| `tests/test_frontend_switch_rollback.py` | 改后（磁盘现状，15 passed） | `CF3AE2E4C34674D065BCC746DC61CE09F7931BA76E897D7E470A83EC17239313` |

说明：

- 「改前」由「当前内容反向套用本次全部改动」重建（`%TEMP%\dafors_pre_hash_reconstruct.py`，
  测试侧 5 组替换 + 前端侧 2 组替换，全部唯一命中），证明本会话没有夹带别的东西。
- 该测试文件在全量跑期间（23:32:41）被**并行分支**改写过一次（新增 `_reload_page` / `_sync_locator`
  等辅助，测试数仍为 15，且沿用了本会话的 `_wait_booted`）。因此两次时点反推出的「改前」hash 不同；
  每次反推都唯一命中本会话的 6 处改动，最终以磁盘现状为准 —— 改完后整文件复跑 `15 passed`。

## 7. 复跑命令

```powershell
# 单跑
& .venv\Scripts\python.exe -m pytest "tests/test_frontend_switch_rollback.py::test_pair_inversion_rolls_back_to_disk" -q
# 带载全量
& .venv\Scripts\python.exe -m pytest tests/ -q
```
