# dafors 巡检快照

## 2026-11-07 16:10 首次快照

| 时间戳 | 会话 | 状态 | 关键数字 |
|---|---|---|---|
| 16:01 | 319accdc (缺口1 入池与抽签字段) | 完成+遗留FIX | 18/18 PASS; 入池属性字段不生效待修 |
| 15:44 | da85f6a6 (缺口2 发布链路) | PASS | 端到端 97.76%; 2风险项待决策 |
| 15:55 | eea45452 (缺口4 授权密钥) | PASS | 完整实现; 3决策项 |
| 15:50 | fa2c17bf (真机覆盖矩阵) | 完成 | 11/11 PASS; 必须开游戏=7项 |
| 15:57 | 44755992 (离线验证1 前端与开关) | PASS | 23/23 |
| (早) | 697db934 (离线验证2 输入链路) | 完成 | F2/F7/F8/F5 已修复 |
| 15:43 | b5a50cc0 (离线验证3 信任链与数据通路) | PASS | 6/6 (遥测游标已修复) |
| 07:31 | 25a3c42 (离线验证4 全量回归) | PASS | S1 20/20; S2 追加回归 |
| 15:43 | 935cc15a (离线验证5 性能与稳定性) | 稳定性PASS/性能退回 | 稳定性 7/7; 01b退回2次 |
| (早) | ea4e1e45 (01b 守门复验) | 退回待决策 | 01b换页证据方案退回 |
| 16:09 | fa59b8fa (合并收口) | 完成 | 55 tests PASS; 合并完成 |

## 2026-11-07 16:40 第二次快照（对比 16:10，仅记变化）

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 16:34 | 319accdc (缺口1 入池与抽签字段) | FIX已修完 | 复核打回三块全改完(解析串位/判定不硬猜/elapsed口径); 全量 1977 passed/1 已知无关失败; test_engine_list_queue 35 passed; 第三轮全量在跑(16:37时49%) |
| 16:37 | 44755992 (离线验证1 前端与开关) | 全过扩面 | 检查项 23→80, 80/0; 新增前端回滚竞态修复验证: test_frontend_switch_rollback 15 passed + 88 passed; 证据 _rollback_race_evidence.json |
| 16:36 | 697db934 (离线验证2 输入链路真实驱动) | 新缺陷 | 5轮采集: 还原可靠性 FAIL(30ms档 3/500 未还原); 占用指标 FAIL(p50 最高 +127.5%); PARTIAL×3(原因码误报/UIPI运行期误报/HookHotkey不互斥,已给修法); 探针残留进程需重启回收 |
| 16:32 | eea45452 (缺口4 授权密钥) | 门禁落地 | 仓库内dev私钥已移除, 全仓扫描0命中; 新增 tools/scan_secrets.py 门禁+pre-commit+build.bat+CI; PUBLIC_KEY_B64 未换 |
| 16:17 | b5a50cc0 (离线验证3 信任链与数据通路) | 6/6 PASS保持 | 遥测第二轮边界修复复测通过: 断网缓冲9→3条, 恢复后0, 轮转.1丢5→补齐, test_telemetry_cursor 8 passed |

无变化: da85f6a6(缺口2), fa2c17bf(真机矩阵), 25a3c42(离线4), 935cc15a(离线5), ea4e1e45(01b), fa59b8fa(合并收口)。
备注: 工作区大量改动未 commit(遵守不commit规则); 01b 退回决策仍未给, 935cc15a 性能复测持续挂起。

## 2026-11-07 17:12 第三次快照（对比 16:40，仅记变化）

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 16:45 | 319accdc (缺口1 入池与抽签字段) | FIX验证收尾 | 第三轮全量完成: 5个失败均为既有/环境项(金额口径/rapidocr缺件/installer规则/soak自身问题), 与本次改动无关; 改动相关三组(test_pool_fields/test_feature_telemetry/引擎回归)全绿无新增skip; 报告_pool_fields.md定稿, 17:10仍在小幅编辑 |
| 16:45 | b5a50cc0 (离线3 信任链与数据通路) | 6/6全过 | _nonreal_trust.md定稿: 1/2/4/5/6全过; 第3项遥测游标修复后复测 test_telemetry_cursor 23 passed, 断网缓冲3条/恢复0条/服务端9事件无膨胀; 漂移重锚10万行155-197ms一次性成本 |
| 17:09 | 697db934 (离线2 输入链路真实驱动) | 修复验证中 | 16:41起持续跑 _hk_probe/_hk_child 探针(temp_20261107_83); 热键互斥修复文档 _hotkey_mutex_fix_20261107.md 16:57落盘; 还原可靠性/占用指标复测结论未出 |
| 17:05 | fa59b8fa (合并收口) | 输入链路修复合并中 | 正在改 core/input.py, core/interception.py, _realdrv/probe_restore.py+probe_status.py — 对应离线2三处PARTIAL的合并动作 |
| 16:43 | 25a3c42 (离线4 全量回归) | 观察 | 16:43有会话动作但无文件产出, 之后静默30分钟, 是否仍在跑待确认 |
| 16:42起 | 新分支 8628cc85 | 新增 | 改 core/hotkey.py(16:42), 写 _hotkey_mutex_fix_20261107.md(16:57) — 热键互斥修复分支 |
| 16:50起 | 新分支 4bb6a406 | 新增 | 遥测探针 Temp\dafors_r0\probe_tel.py, 写 _iteration_loop.md(17:03) |
| 16:52起 | 新分支 4d2fab4d | 新增 | 写 _auto_calibration.md(16:52), desktop.py(17:10) |
| 17:02起 | 新分支 9ff932f4 | 新增 | 写 _perf_flow.md(17:03)与 _gaps.md(17:04) — perf流程报告+缺口清单更新 |
| 16:41起 | 新分支 4d1d264d | 新增 | build\rehearsal\launcher_cycle.ps1(16:41), desktop.py+desktop.log 持续增长(17:10) — 桌面启动器循环演练在跑 |

无变化: da85f6a6(缺口2), eea45452(缺口4), fa2c17bf(真机矩阵), 44755992(离线1), 935cc15a(离线5), ea4e1e45(01b)。
备注: 5个新分支(8628cc85/4bb6a406/4d2fab4d/9ff932f4/4d1d264d)不在既定巡检清单, 均在 dafors 工作区有实质文件动作; 01b退回决策+缺口2/缺口4决策项+01b性能复测仍挂起(>1.5h); B1-B5真机采集脚本已在 tools/ 且支持 --selftest, 工具链就绪。

## 2026-11-07 18:05 第四次快照（对比 17:12，仅记变化）

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 17:35 | fa59b8fa (合并收口) | 完成 | _feature_merge_report.md 定稿: 七路合并成立, 产品代码因合并缺陷改动0行, 21配置键×三表联通全过, perf补丁Q1-Q5全落地, 全量回归0 failed; 另打包演练产出 SJZ-Control.exe(108MB)+Setup-1.1.0.exe(60MB) |
| 17:54 | 697db934 (离线2 输入链路) | 缺陷裁定收口 | _click_cost(17:43/18:01): 占用FAIL平反—真实驱动合批480/480生效, +127.5%为并发负载污染非回归, 代码侧≈50µs, 旧阈值0.250/≤0.320作废; _nonreal_input终稿: 光标3/5轮严格还原(另2轮外部干扰), 驱动上下文5/5关闭无内核残留; 遗留: 探针退出路径卡死, PID 36984 需重启回收(产品路径可规避) |
| 17:51 | 319accdc (缺口1 入池抽签) | 二轮全量完成 | _pool_fields.md 三稿; _pool_r2_full2.log: 2105 passed/5 failed/134 skipped(8:27); 5失败中2个新面孔(test_autocalib断言消息不匹配, test_console_api期望400实得200, 疑并发改动漂移), 3个既有环境项(rapidocr缺件/installer规则/soak) |
| 17:35 | 935cc15a (离线5 性能) | 性能裁定完成(由衍生分支) | _perf_flow.md: 点击延迟非命中瓶颈(p50 0.25→0.73ms命中率仅差≤0.03pp), B1卡点分布才是准入指标; _baseline_r0.md+r07迭代环基线落盘; 会话本体15:52后无动作 |
| 17:24起 | 25a3c42d (离线4 全量回归) | 静默解除/在跑 | 30分钟静默解除: 改 test_preflight/test_autocalib, 产出 _ui_shots 四页截图+校准素材 data\<hash>×10+; 终稿未出 |
| 17:29 | (发布线) | 新缺陷 | test_installer_rules FAILED: installer/sjz.iss 的 UninstallDelete 命中 config.json、\data、\templates — 卸载会删用户数据 |
| 17:35 | (冲突项 9ff932f4 vs 319accdc) | 待裁定 | _gaps.md 静态审计判「入池六字段未实现」(engine.py:3050,3112-3137), 与缺口1会话「已修+116 passed」矛盾, 需第三方复核 |

无变化: da85f6a6(缺口2, 15:44起挂起2个决策项), eea45452(缺口4, 15:55起挂起3个决策项含PUBLIC_KEY_B64), fa2c17bf(真机矩阵), 44755992(离线1), ea4e1e45(01b)。
提醒(规则4触发): 真机矩阵77项清单已交付(必须开游戏25/不开游戏52), B1-B5脚本就绪, 离线五路实质收敛(占用FAIL已平反, 合并0 failed) — **现在可以开游戏做真机验证**。准备: ①开游戏固定1080p与UI缩放, 进仓库/挂摘页面; ②先跑 tools\collect_b1..b5 --selftest 确认采集链路; 建议开游戏前重启系统回收探针残留 PID 36984。
备注: 4d1d264d 启动器循环演练仍在跑(desktop.log 17:41仍在增长); 新增快照分支 30b9d576/a47a6d20/5b29c938/ede2ac95/92923288/b2ecf115 等未入既定清单, 近期有动作; 缺口2/缺口4/01b 决策项挂起>2h, 01b的性能退回已被 _click_cost/_perf_flow 新证据实质解决, 可按新证据直接关闭。

## 2026-11-07 19:50 第五次快照（对比 18:05，仅记变化）
判定口径: 本机无 list_sessions 接口, 改用会话快照 tag(ai-agent\snapshot\<id>\v2\.git\refs\tags)+工作区文件时间戳联合判定。

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 19:29 | fa59b8fa (合并收口) | 完成+回归复验 | _feature_merge_report.md 19:15 追加 §10.4: 单进程 2091 passed/19 skipped/2 failed(均非产品, 补齐 version_info.txt 后单跑 2 passed/4.68s); -n4 2093 passed/0 failed/180.01s; §10.5 .gitignore 五条只忽略不删; §10.6 evo 未进 import 依赖树(仅 datas) |
| 19:15 | b5a50cc0 (离线3 信任链) | 维持 6/6 PASS | _nonreal_trust.md 19:08 定稿: 1/2/4/5/6 全过, 第3项遥测游标 23 passed; 断网缓冲3条/恢复0条/服务端9事件无膨胀; 漂移重锚 10万行155-197ms 一次性成本 |
| 18:24 | 319accdc (缺口1 入池抽签) | 无新产物 | 18:24 有回合结束tag但工作区无新文件; _pool_fields.md 仍 17:51, 维持 2105 passed/5 failed(3既有环境项+2并发漂移) 待第三方复核六字段 |

无变化: da85f6a6(缺口2), eea45452(缺口4), fa2c17bf(真机矩阵), 44755992(离线1), 697db934(离线2), 25a3c42(离线4), 935cc15a(离线5), ea4e1e45(01b)。
异常: ①25a3c42 自 17:24:46 停在 toolcall tag, 2h25m 无新工具调用, 但 test_server.py(19:28)/test_screen.py(19:22)/_ui_shots(19:47-19:48) 仍在更新 — 疑似挂起或产物已被他分支接手, 建议确认存活再决定是否关闭本体; ②935cc15a 本体 15:52 后静默4h, 性能结论由衍生分支持续更新至 19:48(_perf_flow.md §11: 点击代码无可压结构, 81.5%占用为等落地/GIL; 旧阈值 0.250/≤0.320 作废) — 建议按衍生产物关闭本体。
其他: 探针残留 PID 36984 已不在进程表(已回收); 卸载演练 19:27 通过(S7_UNINSTALL exit=0 且 config/license/data/templates 全保留, 残留43文件属有意留存); 19:37 config.json 复原为 .good; 新增未入清单动作: evo/observe.py+pool_inbox.py+pool_contract.py(19:47)、tools/collect_b4_clean_click.py(19:46)、build.bat+market.py(18:30)、tools/license.db(19:49)。
提醒(规则4): 离线五路已实质交付+合并回归0 failed — 现在可以开游戏做真机验证。准备: ①游戏固定1080p、UI缩放100%, 停在仓库页/挂摘页面; ②先跑 tools\collect_b1..b5 --selftest 验采集链路, 再正式采集。

## 2026-11-07 19:53 第六次快照（对比 19:50，间隔仅 3 分钟）

清单内 11 个会话：**零状态变化**。最新快照 tag 全部停在原时间点 —
fa59b8fa 19:29 / b5a50cc0 19:15 / 319accdc 18:24 / 25a3c42 17:24 / 697db934 17:14 / 其余 da85f6a6 15:44、eea45452 15:55、fa2c17bf 15:51、44755992 15:43、935cc15a 15:52、ea4e1e45 15:43。

新增产物（均属未入清单的衍生分支，非清单会话）：
- _adv3\_common.py 19:52:02、_adv3\a1_line_shift.py 19:52:27
- tools\longrun_collect.py 19:51:46
- evo\fixtures 每 ~30s 重新生成一轮（state-main/crash + rollback，19:51:25→19:52:27），属自检/演练循环，非新缺陷
- dist\SJZ-Control\config.json 19:52:49 被演练循环改写（config.json.good 仍在）

异常维持（非新增）：
- 25a3c42 自 17:24:46 冻结已 2h28m，仍停在 toolcall tag；建议确认存活，无进展则按产物收口关闭本体
- 935cc15a 本体 15:52 后静默 4h，性能结论由衍生分支持续更新

挂起项不变：缺口1 六字段第三方复核、缺口2 两项决策、缺口4 三项决策（含 PUBLIC_KEY_B64）、01b 退回决策。
提醒维持：真机验证窗口已就绪（同 19:50 结论，不重复展开）。

## 2026-11-07 20:07 第七次快照（对比 19:53，仅记变化）

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 19:58 | 319accdc (缺口1 入池抽签) | **报出新缺陷** | 19:57 落地第三方独立复核 `_pool_fields_review2.md`：47 格用例 43 一致 4 FAIL — A6/A8「1 2 3 人参与」被拼成 12、A15 max>1,000,000 时 self 未整组清空、A18「1,25」千分位误判；裁定 D1 最需修（self 合法但 max 被吃小时整组丢失）；顺带平反 installer 一项（非"删用户数据"，为 iss 改口径后断言被放宽，现 PASS）。19:58:33 起新回合在跑，尚未产出新文件 |
| 19:55 | 25a3c42d (离线4 全量回归) | **解冻并完成一回合** | 冻结 2h31m 后 19:55:49 结束回合；产物 tests/_isolation/groups.py 19:54:50、tests/conftest.py 19:55:36（测试隔离分组，为并行跑铺路）；终稿仍未出 |

无变化: da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口)。

衍生动作（未入清单）：合并门逐文件快照机制落地 — worktree wt-a/wt-b 19:57:57/19:58:28 建立，tools/merge_gate.py 20:05:16、tools/check_locks.ps1 19:59:35；wt-a apply 2 文件成功，wt-b apply 撞 tools/check_locks.ps1 冲突（20:05:09），两次 dry-run 均 conflicts=[]；`_od_seed20261107.log` 全量回归跑到 60%，5 处 F 均为已知项（console_api list_region、soak end_to_end）；desktop.log 20:02:54 再次 UIPI 提权重启（预期行为，非崩溃）。

挂起项：缺口2 两决策、缺口4 三决策（含 PUBLIC_KEY_B64）、01b 退回决策 — 均挂起 >4h。
提醒：真机链路骨架仍就绪，缺口1 的 D1 属纯离线 toast 解析边界，不阻塞真机采集；建议 D1 修完再冻结快照。

## 2026-11-07 20:34 第八次快照（对比 20:07，仅记变化）

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 20:11 | 319accdc (缺口1 入池抽签) | **4项复核FAIL全关** | _pool_fields.md 第三轮 D1-D5: A6/A8「1 2 3 人参与」整串归人数→null(不再造 12); A15 max>1,000,000 两值都留只标 conflict; A18「1,25」→125; D1-1 self=999/max=1 不再整组丢; 48+14+62+116 passed; 20:24 起全量在跑 |
| 20:09 | b5a50cc0 (离线3 信任链) | **报出新缺陷/修复中** | _telemetry_review3.md 对抗复核打出 8 类: 1b/1c/1d 游标前增删等长行丢 seq 不可恢复; 2D .1 截短副本致重复120行; 3A/3B/3D 归档缺失致 gap 区间错位; 4e/4f 4096..12288 改写约107行未检出; 成本最坏 2.7s/10万行; 20:32 已改 tools/telemetry_agent.py + tests/test_telemetry_cursor.py |
| 20:12 | 935cc15a (离线5 性能稳定) | **长时稳定性交付** | _longrun_stability.md: 57.2分钟/61轮真实连续 + 480轮(8h等效) 全判据通过; 私有内存~517MB平坦/句柄336稳定; 修 core/screen.py mss失败泄漏 + core/server.py keep-alive 30s 超时; 73 passed/2 skipped/1 failed(既有 soak); 20:08 起又开一轮 longrun real 在跑 |
| 20:14 | 全量回归 (_od_seed 种子) | **10 failed/2230 passed** | 17:16; 5 新面孔: persist_hardening FileLock 10s 超时、click_speed 1563ms 与 20Hz(配50Hz)、frontend_switch_rollback x3 playwright 超时、mouse_human/skip_late traceback — 当时 longrun real 同机在跑，疑并发争抢，需隔离复跑裁定 |
| 20:05 | fa59b8fa (合并收口) | 合并门验证通过 | _merge_log.jsonl: wt-a apply 2 文件成功; wt-b conflicts=["tools/check_locks.ps1"]; core digest 前后均 d0dd4c77af3e2f6b(核心零漂移); 2 次 dry-run conflicts=[] |
| 20:28 | (installer 线) | 已知缺陷修复 | installer/sjz.iss 20:26 + test_preserve_failure.ps1 20:26 + verify_installer.py 20:28; dist-installer 20:28 重建 |
| 20:06 | (autocalib 复核线) | **新缺陷(潜在)** | _autocalib_review2.md: 界面 adjusted 显示「已微调」但 apply_auto 默认 False 坐标未落盘; 1920×1200@150% 实测偏 43px 而 preflight 仍绿 |
| 20:24 | 25a3c42 (离线4 全量回归) | 并行隔离验证中 | 20:24-20:31 多进程 pytest(全量/--collect-only --random/单文件 test_console_api/test_feature_mouse_human/test_feature_grade_stop); 仍无终稿 |
| 20:07 | (evo 线) | 首轮收口 | _evo_round1.md: accepted(观测级); 唯一真实数据 tuning 137行(128 hit/9 miss); 明确未声称游戏侧行为增益 |

无变化: da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、ea4e1e45(01b)。
挂起: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >5h。
异常: 25a3c42 解冻后仍无终稿(19:55 起 40 分钟); 离线3 从 6/6 PASS 回退为修复中, 真机提醒条件(五路全交付且无未合并修复)本轮回退, 不再重复触发, 但遥测游标不阻塞 B1-B5 采集。

## 2026-11-07 21:06 第九次快照（对比 20:34，仅记变化）

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 21:01 | 319accdc (缺口1 入池抽签) | 完成(收尾) | 第三轮全量定稿: 6 failed/2144 passed/135 skipped/2 xfailed/644s; 6失败=3×autocalib并行改动(`_Cfg` 无 `data`)+3×rapidocr 缺件; 特性相关5文件 121 passed/0新增skip; 20:34 的四项复核FAIL 全关 |
| 21:03 | b5a50cc0 (离线3 信任链) | 缺陷闭环→PASS | 第五轮 6 项全修: 1b/1c/1d 净长度不变删插→丢 seq 改为 `replaced` 全交付; 2D 截短副本 125 行→6 行; 3A/3B/3D gap 区间按各代起点; 4e/4f 4096..12288 改写 0→600 行 `replaced`; `test_telemetry_cursor` 34 passed(+4 用例) |
| 21:02 | (autocalib 复核线, 未入清单) | 缺陷关闭 | 20:34 报的「adjusted 显示但坐标未落盘/43px 偏置」已修: 6 档匹配 0px、9 档点击 ≤0.5px; 19/28/23/42/141 passed; 前端无头 51/51 |
| 20:45起 | 25a3c42 (离线4 全量回归) | 异常(疑似再次冻结) | 回合 19:55:49 开始仍未结束, 最后工具调用 20:45:32, 20 分钟无新 tag; 但 21:04 起多路 pytest(全量 `-p no:cacheprovider` + 定向 test_fix_d/test_state_hardening/test_ocr_panel/test_records+pool_fields)在跑; 终稿仍未出 |

无变化: da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >5h。
真机提醒条件: 未恢复(离线4 无终稿、离线2 探针退出路径遗留、离线3 本轮修复未进冻结回归) — 不重复触发。
其他: dist\SJZ-Control\config.json 20:40:58 被演练循环改写后再复原(config.json.good 同秒同步); 新起全量 pytest 多路并存, 建议 25a3c42 本体若确认失活则按产物收口, 避免与回归裁定线抢 CPU。

## 2026-11-07 21:35 第十次快照（对比 21:06，仅记变化）

> 工具说明: 本环境无 list_sessions 可调用; 状态取自「会话快照 v2 最后写入时间 + 进程表 + 报告产物 mtime」。

| 时间戳 | 会话 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 21:15 | b5a50cc0 (离线3 信任链) | **交付并通过对抗复核** | 六攻击面全守: A1 size==offset 且 mtime/size 变更→强制 0..offset 行链复核; A2 按(文件身份,代)行哈希去重; A3 gap 行号按全量历史累计; A4 采样覆盖 0..offset 消除 4096..12288 盲区; 39 用例 + 全量 2175 通过(失败均 OCR 无关); 原三攻击面 0 重复/10万行最坏 2.66s 不变; 快照 21:24 收笔 |
| 21:20 | 935cc15a (离线5 性能稳定) | 数据前推 | _longrun_stability.md 更新: 真实连续 64.8min/70 轮(上版 57.2min/61 轮) + 480 轮 8h 等效; 私有内存/句柄/线程/单轮耗时 8h 外推全过; CPU 归因由 +18.8% 校正为每抓图 -5.7%; 两处真修(Screen 2 GDI、keep-alive 30s)维持 |
| 21:26 | 25a3c42d (离线4 全量回归) | **恢复活动 + 新交付** | _test_isolation_lock.md: heavy 分组跨进程锁(tests/_isolation + conftest), 4 并发假失败 5 处/3 实例红 → 0 处/4 全绿; 单次全量 775.47s→749.42s; 锁超时 0; 21:24-21:30 连写 test_click_speed/test_frontend_switch_rollback/order_isolation/test_load_gen; 冻结快照终稿仍未出 |
| 21:30 | (非清单衍生线) | 活跃写入 | tools/kill_stray.ps1 21:30:46; core/money.py 21:33:45、core/ocr_chain.py 21:34:00、tools/poll_breakdown.py 21:33:39; 无对应报告 |
| 21:33 | (并发现场) | 多路重负载并存 | 2×soak.py + 2×load_gen.py + 3 组 pytest(21:22/21:30/21:32 起)同机在跑; 引用任何性能数字须标注该并发数 |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、ea4e1e45(01b)、fa59b8fa(合并收口)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >6h。
真机提醒条件: 仍未满足(离线4 冻结终稿未出、离线2 探针退出路径遗留) — 不重复触发。
异常: 935cc15a 本体快照 15:52 后无写入, 21:20 的结论由报告与采集器前推, 建议确认本体存活, 否则按产物收口; 25a3c42 已从疑似冻结恢复, 但与衍生线同机抢 CPU, 建议全量回归独占跑一次再定稿。

## 2026-11-07 22:05 第十一次快照（对比 21:35，仅记变化）

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 21:47 | 稳定性 6 条线 (未入清单) | **全量 0 failed** | `_stable_failures_fix.md`: serial 2294p/4f → 2305p/2f → **`-n 4` 2321 passed/0 failed/20 skipped/2 xfailed**; 6 条中仅 #1(console_api 测试期望改 200) 与 #6(soak 假确认弹窗夹具) 需真修, #2-#5 已被并行线修掉; soak 单跑 10 passed |
| 21:53 | 935cc15a (离线5 性能稳定) | **数据修正(撤回临界结论)** | `_longrun_stability.md` 新增 §14: 「WS 临界 +16.2%」判为口径失真作废 —— 分母取了 48MB 工作集且斜率来自预热台阶; 真实 8h 为一次性 +2.7~4.1MB 后平台, 私有提交 +0.4~1.6%; 补 `test_longrun_ws_metric.py` 3 例(5.42%~7.35% ≤8%) |
| 21:45-22:01 | 25a3c42d (离线4 全量回归) | 活动(仍无终稿) | 21:45 起两路乱序全量 `--random-order-seed=20261107`(venv 与系统 Python 各一); 22:01 起 records/scheduler 定向 + fix_e/validate/state_hardening 定向; `_test_isolation_lock.md` 仍停 21:26 |
| 21:58-22:02 | 打包/收口线 | **重打包完成(版本同步)** | `sjz.spec` 21:58 补 hiddenimports、`tools/audit_packaging.py` 21:58 新入; `tools/version_sync.py` 生成 `launcher/Version.g.cs`+`installer/version.iss`=1.1.0(21:59); dist 22:00:48 重建、onefile `SJZ-Control.exe` 22:01:44、`SJZ-Control-Setup-1.1.0.exe` **79.3MB**(旧 60MB, 22:02:34) |
| 21:45-21:56 | 并行代码改动 | 活跃 | `core/config.py` 21:45、`core/scheduler.py` 21:51、`core/records.py` 21:52、`core/server.py` 21:56、`core/pagewatch.py` 21:56、`tools/poll_breakdown.py` 21:56、`tools/soak_gui_gdi.py` 21:54、`tests/test_gdi_stability.py` 21:58 |
| 21:47起 | 新起压测 | 在跑 | `soak_ocr.py --minutes 120` ×2(21:47/21:48)、`soak_gui_gdi.py --minutes 45 --port 8901` ×2(21:54/21:56); 同时 4 路 pytest 并存 → 引用延迟/CPU 数字须标并发 |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、ea4e1e45(01b)、fa59b8fa(合并收口, 报告停 19:15)。

异常: ①**桌面可见窗口违规** —— PID 43380(`soak_gui_gdi.py`, port 8901)主窗口标题「SJZ 抢购控制台」处于可见状态(MainWindowHandle≠0), 违反「桌面可见窗口=0」硬约束; 建议对该类必须真 GUI 的压测统一在启动后 3s 内 `ShowWindow(SW_MINIMIZE)`, 并在报告里标注窗口态; ②桌面可见窗口计数: 1(应为 0)。
挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >6.5h。
真机提醒条件: 仍未满足(离线4 冻结终稿未出、离线2 探针退出路径遗留、22:00 重打包尚未跑安装验证) — 不触发。

## 2026-11-07 22:36 第十二次快照（对比 22:05，仅记变化）

> 工具说明: 本环境仍无 list_sessions 可调用; `C:\Users\26346\.trae-cn\ai-agent\snapshot` 目录已不存在, 项目 git 无 tag → 本次判定改为「进程表 + 文件 mtime + 报告产物」。会话本体存活无法直接读取。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 22:28 | 稳定性 6 条线 (未入清单) | **6 项全关** | `_stable_failures_fix.md` §7/§8: 6 条全部关闭, 无 skip/xfail; serial 2327 passed/3 failed(均环境或夹具); `-n 4` 2321 passed/0 failed/20 skipped/2 xfailed; 夹具打桩 4 处: test_fix_e 19 passed、test_state_hardening 28 passed、test_click_speed 5 passed、tools/soak.py 假确认弹窗(单跑 10→15 passed); 根因之一 = 多会话共享 `%TEMP%\sjz-pytest-isolation` heavy 锁互删 |
| 22:16-22:20 | 25a3c42 (离线4 全量回归) | **仍无终稿(超 3h)** | 实质改动: core/server.py 22:16、core/records.py 22:19、tests/test_poll_path.py(21:52 新建/22:19 改)、test_server+test_console_api 22:20; `_test_isolation_lock.md` 仍停 21:26 |
| 22:26 | 打包线 (未入清单) | 版本产物重生成 | `tools/version_sync.py` 重写 launcher/Version.g.cs、installer/version.iss、build/version_info.txt; dist 22:00 重建 79.3MB 未变 |
| 21:54-22:36 | 桌面窗口 | **违规已消除** | soak_gui_gdi PID 43380「SJZ 抢购控制台」窗口可见 42 分钟; 22:36 EnumWindows→SW_HIDE 后本机自有可见窗口 = 0(全机 12 个均为用户程序) |

并发现场: 5 组 pytest(48828/38096/43976/41576 + 各自系统 Python) + 2×soak_ocr(120min) + 2×soak_gui_gdi(45min, port 8901) + 2×paddlex OCR(8081/8082), CPU 71% → 本时段任何延迟/CPU 数字须标注该并发数, 不得单独用作决策。

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >7h。
异常: ①25a3c42 自 17:24 起 5h 只有中间产物、终稿未出, 建议按产物收口或强制冻结快照后再收尾; ②697db934 探针残留 PID 36984 已回收, 但"探针退出路径卡死"仍属已知缺陷(产品路径可规避)。
真机提醒条件: 仍未满足(离线4 冻结终稿未出、22:00 重打包未跑安装验证、离线2 退出路径遗留) — 不触发。
工作区: 353 个文件改动未 commit(遵守不 commit 规则)。

## 2026-11-07 23:05 第十三次快照（对比 22:36，仅记变化）

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 22:50 | 全量回归线（离线4 关联，未署名终稿） | **1 failed（并发性抖动，已单独复跑通过）** | %TEMP%\dafors_full.log: 2330 passed / 1 failed / 20 skipped / 2 xfailed / 681s; 唯一失败 	est_pair_inversion_rolls_back_to_disk（Playwright 点击 .setnav [data-setpane="fire"] 元素不可见超时 30s），同轮 heavy 锁最长等待 24.4s(hotkey spawn)/7.0s(playwright 组); 22:56 dafors_pw.log 单独复跑 **1 passed / 6.81s** → 判定为并发争抢抖动，非产品缺陷 |
| 22:52 | 全量回归第二轮 | 在跑 | 22:52:44 新起 pytest --random-order-seed=424242（venv 与系统 Python 各一）；23:02 venv 路仍在跑，系统 Python 路已结束; 同机 CPU 90% |
| 22:41 | 25a3c42 (离线4 全量回归) | 仍无终稿（>5h50m） | 唯一新增：	ests/order_isolation.py 22:41:53; _test_isolation_lock.md 仍停 21:26; 终稿未出 |
| 22:56-22:58 | 性能测量线 | 新数据 | dafors_probe_snap.log(22:58): poll p50 0.89 / p95 7.04 / p99 9.28 / max 10.40ms，最大头 ec_thread_lock 94.5%; dafors_probe_http.log(22:57): scheduler 路由 p50 2.03 / p95 10.08 / p99 11.80ms。并发口径：2×pytest + 2×soak_ocr(120min) 同机 |
| 22:36-23:00 | 桌面窗口 | 0（违规已消除） | soak_gui_gdi 已退出; 23:02 EnumWindows 本机可见窗口 12 个（全为用户程序），自有窗口 = 0 |

并发现场(23:02): python 进程 10 个 = 2×soak_ocr(120min, 21:48 起，预计 23:48 结束) + 2×pytest 全量 + 桌面演练循环(desktop.log 22:58、data\confirm/buy.png 23:00 仍每轮刷新) ; CPU 90%。

无变化: 319accdc(缺口1，已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >7.5h。
异常: 25a3c42 终稿缺失近 6h，建议按 _test_isolation_lock.md + 本轮 2330/1 结果收口，不再等本体回合。
真机提醒: 条件仍差「离线4 冻结终稿」一项，不触发。

## 2026-11-07 23:31 第十四次快照（对比 23:05，仅记变化）

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 23:18-23:31 | 25a3c42 (离线4 全量回归) | **报出新缺陷 + 正在修** | `_frontend_rollback_flake.md`: 全量唯一超时用例根因=产品侧 `boot()` 收尾无条件 `go('run')` 抢回页签 + 夹具等错信号(`#sbConnTxt==='已连接'` 在首个 api 成功即置位); 23:31:14 改 `tests/test_frontend_switch_rollback.py` 后 23:31:19 定向复跑中。另 `_order_dependence_fix.md` 23:15: 3 条乱序失败=6 处模块级可变状态泄漏, 新增根 `conftest.py` 以 `pytest_plugins` 注册 `tests/order_isolation.py` autouse 复位; seed 20261107 2→0 failed, seed 424242 8+1→3 failed(关插件同样失败), -n4 三目标用例 0 失败。终稿仍未出 |
| 23:10 | 验收审计线 (未入清单) | **新缺陷(结论口径)** | `_acceptance_matrix.md`: R0 基线 7 项全判 B 档(%TEMP%\dafors_r0 已删, 原始 JSON 无法重算); `price_exact 1.0000(630)` = 21 裁片×30 次重复, 非独立帧; 独立 444 帧实测 **0.8468**; 代码内不存在 `item_e2e_full`, 只有 `item_e2e`; 同指纹实测 `item_e2e` **4509.46ms(n=12, +3.56%)** 文档未记 |
| 23:17-23:27 | 测量更正线 (未入清单) | 缺陷关闭 | `flow_bench.py` 23:17 补 `item_e2e_full`(list_prefix+entry+fire_all 逐件相加取分位); `_evidence_measure_20261107.md` 23:27: p50 **4413.77ms(n=40)**; 价格独立帧集(sha256 去重 21 帧) **1.0000(21/21)**、静默错价 0, 95% 下界 0.857; 四文档口径已同步更正 |
| 23:26 | OCR ROI 复核线 (未入清单) | **红线破了→已修** | `_ocr_roi_review.md`: 价格串被 ROI 切末位→千分位拒识→`money.scan_text` 静默读成数量 63(67 帧拼条错/整幅对); 价格 ROI 右缘 360→390 修复后独立帧 21/21; 另记「拼条 5 块而回退门槛按 4 词」疑漏检, 待确认 |
| 23:26 | 935cc15a 衍生 (离线5 性能) | 口径定稿 | `_perf_flow.md`: 主指标改 **4413.77ms(n=40)**; 点击 p50 四轮 0.316/0.401/0.373/0.474ms(p95 0.574–1.079) 随并发摆 2.6×; 旧 0.250/≤0.320 阈值作废; 点击延迟非命中瓶颈, B1 卡点分布才是准入指标 |
| 23:05-23:06 | 发布/构建线 (未入清单) | 前置固化 | `version_sync.py`/`build.bat`/`sjz.spec`/`test_release_pipeline.py` 同步; `_build_prereq.md`: 真源 `core/version.py` APP_VERSION=1.1.0 → `build/version_info.txt`、`launcher/Version.g.cs`、`installer/version.iss`、`pyproject.toml`; 清 build/ 致红属缺前置步骤, 已固化为不可跳过 |

并发现场(23:31): python 进程 15 = 2×soak_ocr(120min, 21:48 起, 预计 23:48 结束) + 3 组 pytest 全量(23:16/23:26 起) + 定向 test_frontend_switch_rollback(23:31) + flow_bench smoke(23:28, 已出 `_smoke_flow.json`) + 其他桌面工具; 桌面可见窗口 12 个全部为用户程序, **dafors 自有可见窗口 = 0**。

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、ea4e1e45(01b)、fa59b8fa(合并收口, 报告停 19:15)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >7.5h。
异常: 25a3c42 终稿缺失 6h+, 但 23:11 后连续实质修复(乱序隔离/前端回滚竞态), 属活跃而非冻结; 建议本轮回归收敛后即以 `_test_isolation_lock.md`+`_frontend_rollback_flake.md` 收口定稿, 不再等本体回合。
真机提醒: 仍差「离线4 冻结终稿 + 22:00 重打包安装验证」, 不触发。

## 2026-11-08 00:04 第十五次快照（对比 23:31，仅记变化）

> 工具说明: 无 list_sessions 可调用, 快照目录 ai-agent\snapshot 仍不存在; 判定依据 = 文件 mtime + 进程命令行 + 报告产物 + .pytest_cache。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 23:44 | 25a3c42 (离线4 全量回归) | **报出产品缺陷并已修复(未合并)** | `_frontend_rollback_flake.md` 定稿: 唯一负载敏感用例根因 = 产品侧 `boot()` 收尾**无条件** `go('run')` 抢回页签(启动 6 loader 拖到 1.76s) + 夹具等错信号(`#sbConnTxt=已连接` 在首次 api 成功即置位); 修法 `web/index.html` 条件 `go('run')` + `body.dataset.booted='1'`, 夹具 `_wait_booted`; 带载全量 #1 **0 failed / 2332 passed / 613.66s**, #2 1 failed(另一条 mouse_human 2s 客户端超时, 单跑 3/3 绿), 本条单跑 3/3 绿 + 模块 15 passed; 冻结终稿仍未出 |
| 00:00:36 | 价格 OCR 重测线 (未入清单) | **新缺陷(已量化)** | `_measure/result_price_remeasure.json`: **384 帧 / 37 独立源**, 51.0s / 126.5ms 每帧; panel 路由 **189/189 = 1.0000**; whole_card 91/131 精确 + 40 拒识(30.5%); **fallback(字模兜底) 26/64 精确、38 静默错(59.4%)**; 全量静默错 **38** 帧; 印证「拼条拒识→兜底」路径危险; 00:04 又在跑 `ocr_price_remeasure --build-only`(新语料) |
| 23:39 | 935cc15a 衍生 (离线5 性能) | 口径再定稿 | `_perf_flow.md` §11: 主指标改 **click_core_us**(DLL 调用 p50 **7.8–8.1µs**、组装≈13µs、与负载无关); 占用 p50 0.316/0.401/0.373/0.474ms, 同代码随并发摆 **2.6×**; 旧 `0.250/≤0.320` 明确作废; 点击延迟非命中瓶颈(p50 0.25→0.73ms 命中差 ≤0.03pp) |
| 00:00:33 | 全量回归线 (离线4 关联) | **新失败** | `.pytest_cache lastfailed` = `tests/test_console_api.py::test_preflight_reports_every_item_with_a_fix`; `core/preflight.py`(23:48)/`core/records.py`(23:47) 刚改; 00:02:36 起两路带 `shutdown_probe --test-lock-timeout 30` 定向复跑中, 未裁定是代码缺陷还是用例漂移 |
| 23:45-23:58 | 启动/单实例验证线 | **桌面窗口违规(2 个)** | `SjzLauncher`(PID 11936, 23:45:06)、`SJZ-Control`(PID 37932, 23:50:13) 均可见; desktop.log 23:56:53 记录「旧实例零窗口(僵尸)」、23:56:56 「--stop 结束=False」, 23:58:05 以 port 8803 **可见窗口重启**(「窗口已显示」); 违反「自有可见窗口=0」硬约束 |
| 00:02:58 | 死代码盘点 (未入清单) | 新产物 | `_deadcode_inventory.md`: 367 个 .py 中 **75 个运行时可达**, A 级可直接删 17 项(含 `enable_timer_resolution` 未配对 timeEndPeriod); 只读盘点, 未改任何文件 |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、ea4e1e45(01b)、fa59b8fa(合并收口)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >8h, 建议今晚给出裁定或明确关闭。
并发现场(00:04): python 9 个 = 2×corpus build + 1×pytest(shutdown_probe) + 2×isolation 探针 + 4×Token 看板/tok 采集; 另 SJZ-Control/SjzLauncher 两窗口可见。
异常: ①桌面窗口再次违规(2 个), 建议打包体启动后 3s 内 `ShowWindow(SW_MINIMIZE)` 并在报告标注窗口态; ②25a3c42 终稿仍缺, 但本轮已定位并修复真实产品竞态, 建议以 `_frontend_rollback_flake.md` + 带载 0 failed 全量直接收口。
真机提醒: 本轮不重复触发 — 离线4 的 boot 修复尚未进冻结回归, 且价格兜底 38 帧静默错属未合并缺陷。

## 2026-11-08 00:35 第十六次快照（对比 00:04，仅记变化）

> 工具说明: 本环境仍无 list_sessions; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 00:19 | 价格 OCR 重测线 (未入清单) | **静默错清零 → 该缺陷关闭** | `_measure/result_price_remeasure.json`: 385 帧 / 38 独立源, 159ms 每帧; panel 248/248=1.0000; whole_card 107/131(拒识 24); **fallback 仅 6 帧且全部拒识, wrong=0**(上轮 38 帧静默错 59.4%) → 兜底不再猜数 |
| 00:25 | 输入链路验证线 (离线2 关联) | **报出新缺陷: 光标未还原 717px** | `tools/click_channel_report.json`: 驱动真实鼠标 device14(VID 3554), 50/50 命中=1.0, 合批 70.59ms→1.412ms/击; **restore.ok=false, after=[1806,966], delta=717px**, tight_probe returned_to_origin=false; 00:27:58 `core/interception.py`、00:28:29 `tests/test_input_interception.py` 已改(疑在修) |
| 00:33 | 全量回归线 (离线4 关联) | **80 failed — 该轮判定无效(边改边跑)** | lastfailed(00:33:14) 80 项: preflight 23 / console_api 18 / isolation_lock 15 / input_channels 11 / input_interception 6 / config 4 / 其他 3; 单独复跑 `tests/test_preflight.py` = **24 passed**; 运行窗口内 `core/preflight.py`(00:31:32)、`core/config.py`(00:31:27)、`core/switches.py`(00:31:04) 与用例同刻被改 → 半成品树, 不作结论 |
| 00:36 | 隔离锁 × 输入通道 组合 | **顺序依赖残留(未闭环)** | 组合跑 `test_input_channels.py + test_isolation_lock.py` → 35 ERROR `core.engine 无 DATA_DIR`; 两者单独跑 = 15 passed/1 skipped, 单测通过 → 跨模块全局态泄漏 |
| 00:36 | 输入通道用例 | 用例漂移(待同步) | `test_input_channels` 11 failed 全部来自新默认 `input_fallback=fail` / `input_legacy_borrow=false`, 断言仍为旧回退语义(FALLBACK_DISABLED vs UIPI_BLOCKED) |
| 00:21-00:34 | 新代码线 (未入清单) | 活跃 | `core/timesync.py` 00:21:53、`core/hotkey.py` 00:23:10、**`core/template_heal.py` 00:29:08 + `tests/test_template_heal.py` 00:29:12(新模块 字模自愈)**、`core/shotproof.py` 00:29:39、`core/ocr_service.py` 00:33:38、`test_ocr_service.py`、`desktop.py` 00:33:57; desktop.log 00:19-00:30 全部 `SJZ_NO_WINDOW=1`(不建窗), 许可 DEV-0001/pro 剩余 26911s |
| 00:34 | 桌面窗口 | **违规已消除** | 巡检初 3 个可见("SJZ 悬浮窗"/"SJZ 抢购控制台"/"SJZ-Control 资源管理器") → SW_MINIMIZE 后 **dafors 相关可见窗口 0** |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 均无新报告产物。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >8.5h。
异常: ①80 failed 属边改边跑, 需在文件静止后独占跑一次才有结论; ②离线4 冻结终稿仍缺(自 17:24 起 7h+), 建议以 `_test_isolation_lock.md`+`_frontend_rollback_flake.md`+本轮数据收口; ③顺序依赖(engine.DATA_DIR)与 input_channels 用例漂移两条未修, 阻塞「五路全交付且无未合并修复」。
真机提醒: 矩阵「必须开游戏=7 项」+ B1–B5 脚本长期就绪, 但驱动路径还原当前记录为 false(717px) — 建议先复跑 `tools/verify_click_channel.py` 确认该缺陷已修, 再开游戏采集。

## 2026-11-08 01:06 第十七次快照（对比 00:35，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 查无此工具）; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 00:47 | 697db934 (离线2 输入链路真实驱动) | **717px 缺陷已修 → 但报出还原确认尾部退化** | `tools/click_channel_report.json`: 驱动真实鼠标 device14(VID 3554), **50/50 命中=1.0**, 合批 87.86ms→**1.757ms/击**, **restore.ok=true / delta_px=0**（上轮 after=[1806,966] delta=717px 已消除）; 同线 `perf/out/click_baseline.json`(00:44) **120 连击 restore_delayed_ok_rate=0.8417（19 次失败，索引 101–119）**, restore_unknown=116/120; `tight_probe` changed_samples=0/max_excursion=0 却 returned_to_origin=false（探针判定自相矛盾）; `delivery_watch.landed_events=0` 与 hit_rate=1.0 不一致 |
| 00:58 | 935cc15a (离线5 性能与稳定性) | 进行中(有新产物，未交付) | `perf/out/click_baseline.json`: 端到端 p50 **888.9µs** / p95 2174.9 / max 2748.3; 占用 p50 **0.837ms** / p95 2.002 / max 2.423; 「读取光标原点」p95 426.1µs / max 910.3µs; 00:58 `click_gate_smoke.json`: **filters_verified=false、physical.enabled=false、clicks=0**（仅冒烟，未证明零干扰）; 01:00 后 `click_perf.py --tag gate_smoke` 仍在跑 |
| 00:41 | 价格 OCR 重测线 (未入清单) | **新语料复核 100% → 缺陷关闭** | `_price_frames/result.json`: 23 帧 / 33 项 **33/33 exact、silent_wrong=0、panel 21/21=1.0000**; 采样时 CPU 31.9%→62.9%、python 8→9，该耗时不得单独用于性能结论 |
| 00:35–01:05 | 启动/桌面 e2e 线 | **可见窗口违规 7 次（复现）** | desktop.log「窗口已显示」7 次: 00:35:12 / 00:36:17 / 00:39:17 / 00:43:50 / 00:45:51 / 00:46:25 / 01:00:05; 01:04:2x 又建「SJZ 启动器」(pid 24564) → 巡检已 SW_MINIMIZE; 期间仅 00:37:56 一次 `SJZ_NO_WINDOW=1`。当前 dafors 自有可见窗口 0 |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) — 00:35 后均无新报告产物; lastfailed 停在 00:36:42(60 项)。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >9h, 建议给出裁定或明确关闭。
异常: ①120 连击还原确认 19/120 失败(15.8%) > 矩阵 S0-15 判据(20 次失败 ≤1 次, ≤5%), 开游戏前须在空闲 CPU 复测确认; ②可见窗口连续 7 次违规, 建议 e2e/启动器默认强制 `SJZ_NO_WINDOW=1`, 仅 WebView2 用例建窗且 3s 内最小化; ③离线4 终稿自 17:24 起 >7.6h 未出, 建议以 `_test_isolation_lock.md`+`_frontend_rollback_flake.md`+文件静止后的独立全量收口; ④顺序依赖(engine.DATA_DIR)与 input_channels 用例漂移仍未闭环。
真机提醒: **触发** — 矩阵口径更正为「必须开游戏 25 项」(上轮记 7 项有误), `collect_b1–b5` 脚本齐备, 50 连击还原 delta=0, 工具链可用。开游戏前先做两步: ① 空闲 CPU 跑 `tools/verify_click_channel.py`(≥120 连击), 确认尾部还原失败率 ≤5%; ② 所有采集用 `SJZ_NO_WINDOW=1`, 禁止可见窗口。然后按 B1(collect_b1_gate_p95.py)→B2–B5 顺序采集。

## 2026-11-08 01:36 第十八次快照（对比 01:06，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 再次查无此工具）; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 01:13:17 | 发布链路线（缺口2 关联，未入清单） | **新产物：安装包已出** | `dist-installer\SJZ-Control-Setup-1.1.0.exe` **83.3MB**（`build\sjz\warn-sjz.txt` 01:12:43 / 45KB）；`dist\SJZ-Control` 01:12:45 收集完整（webview / onnxruntime / rapidocr / templates 全在）；01:12:37 doctor 自检全绿: RapidOCR 模型 31.2MB、interception.dll sha ab88164c、内核服务 Start=3、WebView2 157.0.4341.42、许可 pro 剩余 23890s |
| 01:10:48 / 01:11:41 | 697db934 (离线2 输入链路) | **仍有未合并修复在改** | `core/interception.py`(01:10:48)、`core/input.py`(01:11:41) 再次被改；`tools/click_channel_report.json` 仍停在 00:47（50/50 命中、restore delta 0）；新增 `tools/check_driver_assets.py`(01:07:44) + `docs/driver-install.md`(01:05:21) 驱动资产核验 |
| 01:11:53 | 全量回归线（离线4 关联） | 仍属边改边跑，不作结论 | `.pytest_cache lastfailed` **40 项**（00:36 为 60 项）: isolation_lock 15 / console_api 18 / config 4 / robustness 1 / cursor_lock 1；运行窗口内 input.py、interception.py 同刻被改；快照时无 pytest 进程 |
| 01:12:25 | 自检线（未入清单） | 新模块改动 | `core/selfcheck.py`(01:12:25) + `tests/test_selfcheck.py`(01:04:25)；01:12:37 doctor 输出与之对应 |
| 01:06:11 / 01:06:44 | 桌面 e2e 线 | **可见窗口违规 +2** | desktop.log「窗口已显示」两次；01:06:44 后无新建；巡检时 EnumWindows 复核 dafors 自有可见窗口 **0** |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 01:06 后均无新报告产物；`perf/out` 最新仍是 click_baseline.json(00:44)，上轮记录的 click_gate_smoke.json 已被删除（该冒烟结论现无产物支撑）；`_merge_log.jsonl` 停在 20:05。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >9.5h。

异常: ①安装包已产出但无「安装-启动-验收」记录，发布链路不算闭环；②离线2 代码仍在改，120 连击还原尾部 19/120(15.8%) 未复测；③离线4 冻结终稿自 17:24 起 >8h 未出；④顺序依赖(engine.DATA_DIR)未闭环。

真机提醒: 本轮**不重复触发**（01:06 已提醒）。门槛仍差三条: 离线4 冻结终稿、离线5 交付、输入链路 120 连击还原 ≤5%。

## 2026-11-08 02:05 第十九次快照（对比 01:36，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第三次查无此工具）; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 01:56–02:03 | 悬浮窗真机验收线（未入清单） | 活跃，连续第 6+ 轮 | desktop.log: 每轮「悬浮窗已显示（SW_SHOWNOACTIVATE）」；01:56:44 定位报 `ArgumentError: argument 5: TypeError: wrong type` → 01:58:11 起已定位 (1584,16)；`overlay_verify.py` 进程每分钟重启（02:02:47 pid 29524），`%TEMP%\overlay_verify.json` 尚未落盘（跑完才写） |
| 01:56:45–02:01:32 | 探针线（未入清单） | 新活动 | `Documents\Trae-CN\temp_20261108_13`: `_probe_exit.py`(01:56:45)、`_probe_auth.py`(01:58:20)、`_probe_auth2.py`(01:59:22)、`_probe_auth3.py`(02:00:09)、`_probe_cookie_watch.py`(02:00:42)、`_probe_close.py`(02:01:32)，另有 playwright driver 02:01:35 启动 |
| 01:56:49 / 01:57:56 / 02:00:58 | 代码改动 | 未提交 | `config.json` + `config.json.good`(01:56:49)、`desktop.py`(01:57:56)、`web/overlay.js`(02:00:58) |

无变化: 319accdc、da85f6a6、eea45452、fa2c17bf、44755992、697db934、b5a50cc0、25a3c42d、935cc15a、ea4e1e45、fa59b8fa 全部无新报告产物 — `tools/click_channel_report.json` 仍 00:47、`perf/out` 最新 00:44、`lastfailed` 39 项@01:11:53（isolation 15 / console_api 18 / config 4 / cursor_lock 1 / robustness 1）、`_merge_log.jsonl` 20:05、两份收口文档 21:26 / 23:44；快照时无 pytest 进程。git HEAD 仍 `5eef554`，未提交改动 381 项。

异常: ①可见窗口 01:56–02:01 每轮 1 个「SJZ 悬浮窗」重复显示（第 7+ 次），当前 EnumWindows 复核 1 个，与「0 可见窗口」硬约束冲突（NOACTIVATE 不抢焦点，但每轮约 7s 可见）；②离线4 冻结终稿自 17:24 起 >9.6h、离线5 无交付、离线2 120 连击尾部 19/120 未复测。

真机提醒: 本轮不重复触发（01:06 已提醒），门槛仍差三条（离线4 终稿、离线5 交付、120 连击还原 ≤5%）。

## 2026-11-08 02:36 第二十次快照（对比 02:05，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第四次查无此工具）; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 02:31:13 | 打包审计门（未入清单） | **复验全绿** | `build\pack_audit.json` **ok=True**: datas 44 / hidden 20 / required 420 / dangling 0 / data_diff 0 / module_diff 0; 同刻 `tools/scan_secrets.py --all` 双解释器在跑（2 进程） |
| 02:32:22 | 全量回归线（离线4 关联，未署名） | **新起全量** | venv 与系统 Python 各一路 `pytest tests/ -q`（PID 9124 / 21444）；上次全量停在 01:11:53（lastfailed 39 项），本轮为文件相对静止后首次重跑，结论未出 |
| 02:05–02:32 | 悬浮窗/桌面 e2e 线（未入清单） | **可见窗口违规 21 轮（升级）** | desktop.log「窗口已显示」02:05 后 **21 次**（02:13/02:14/02:15/02:16/02:26/02:27 各轮），「悬浮窗已显示 SW_SHOWNOACTIVATE」同步 8 次（02:23–02:27）；每轮显示约 2s 后自行关闭；巡检时 EnumWindows 自有可见窗口 **0** |
| 02:26:23 / 02:27:56 | 代码改动 | 活跃 | `core\server.py`(02:26:23)、`desktop.py`(02:27:56) 再改；config.json + config.json.good 02:27:12 同步改写；`%TEMP%\overlay_verify.json` 仍未落盘 → 悬浮窗定位验收（1584,16）无结论 |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话 **零状态变化**；`tools\click_channel_report.json` 仍 00:47、`perf\out` 最新 00:44、`_perf_flow.md` 00:12、`_frontend_rollback_flake.md` 23:44、`_merge_log.jsonl` 20:05、`_stable_failures_fix.md` 22:39。

清理: `Documents\Trae-CN\temp_20261108_13` 已清空（上轮 6 个探针脚本已删）。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >10h。

异常: ①可见窗口违规从「每轮 1 次」升级为 02:05 后 21 轮，建议桌面 e2e/悬浮窗验收统一 `SJZ_NO_WINDOW=1`，仅 WebGL/WebView2 必需时建窗并 3s 内 `SW_MINIMIZE`，报告标注窗口态；②离线4 终稿自 17:24 起 >9.2h 仍缺，建议以 `_test_isolation_lock.md`+`_frontend_rollback_flake.md`+02:32 全量结果直接收口；③输入链路 120 连击还原尾部 19/120 自 00:44 起 1h50m 未复测，是真机门槛唯一硬缺口。

真机提醒: 本轮不重复触发（01:06 已提醒）。

## 2026-11-08 03:04 第二十一次快照（对比 02:36，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第五次查无此工具）; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 03:00:42 | 全量回归线（离线4 关联，未署名） | **40 项 → 6 项（大幅收敛，仍属边改边跑）** | `lastfailed` 6 项: cursor_lock::test_reports_the_lock_it_found / desktop_jsapi::test_minimize_and_destroy_reach_the_window / mutation_killers ×2(unknown_fallback_policy、unknown_config_fallback) / nav_scroll::test_wheel_uses_real_mouse_event / portability_hardening::test_exit_waits_for_input_release; **全部来自 02:36–02:51 刚新增/重写的用例文件**（test_cursor_lock 02:36:07、test_desktop_jsapi 02:36:22、test_mutation_killers 02:47:45、test_nav_scroll 02:48:21、test_portability_hardening 02:51:16），同窗口 `core/input.py`(02:51:02) 亦被改 → 结论未定；03:01:02 两路 `pytest tests/ -q` 重跑中（PID 29188/29340） |
| 02:59–03:02 | 发布门构建（未入清单） | **进行中：4/7 步全绿** | `%TEMP%\sjz_build2.log`: [2/7] version_sync 一致性「结果：一致」；[3/7] pack_audit datas 44 / hidden 20 / required 420 / core 模块 200，数据与模块差集均空；[4/7] scan_secrets --all 扫 7481 文件 **0 私钥**；[5/7] 测试跑到 **23%**（03:02:36） |
| 02:49–02:59 | 输入链路（离线2 关联） | **新产品侧疑点（连续 3 次复现）** | desktop.log 三次同句: 02:49:50 / 02:51:26 / 02:59:27「退出时输入可能没释放干净：正在停止（上一轮还在收尾），0ms」；与失败用例 `test_exit_waits_for_input_release` 同指向 `core/input.py`(02:51:02) 的退出释放路径 |
| 02:39–02:48 | 桌面 e2e 线 | 可见窗口 7 次瞬时显示 | 「窗口已显示」02:39:13/02:41:26/02:43:00/02:43:11/02:47:03/02:48:36（+02:30:52）; 巡检时 EnumWindows dafors 自有可见窗口 **0**（全机仅用户程序「鹰角启动器」匹配到关键词） |
| 02:49:19 / 02:49:12 | 依赖锁线 | 小动作 | `uv.lock`(02:49:19)、`pyproject.toml`(02:49:12) 更新；02:36–02:51 另改 test_hotkey / test_hotkey_start / test_hotkey_mutex / test_fix_e / test_robustness |

无变化: 319accdc(缺口1,已完成)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话 **零新报告产物**（`_pool_fields.md` 21:01、`_nonreal_trust.md` 21:14、`_frontend_rollback_flake.md` 23:44、`_test_isolation_lock.md` 21:26、`_perf_flow.md` 00:12、`_merge_log.jsonl` 20:05、`tools\click_channel_report.json` 00:47、`perf\out` 00:44）。git HEAD 仍 `5eef554`，未提交改动 384 项。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >10.5h。

异常: ①退出时输入释放疑点连续 3 次，若 03:01 全量仍失败则应作为产品缺陷单独立项，不应按用例漂移关闭；②6 项失败的用例文件与 `core/input.py` 在跑动窗口内被改，最后一轮只可作趋势不作定论，需文件静止后独占跑一次；③离线4 冻结终稿自 17:24 起 >9.5h 仍缺；④输入链路 120 连击还原尾部 19/120 自 00:44 起 >2h 未复测，仍是真机门槛唯一硬缺口。

真机提醒: 本轮不重复触发（01:06 已提醒）。

## 2026-11-08 03:36 第二十二次快照（对比 03:04，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第六次查无此工具）; 判定依据 = 文件 mtime + 进程命令行 + .pytest_cache + 构建日志 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 03:11:39 | 发布门构建（未入清单） | **7/7 全绿 + 产物落盘** | `%TEMP%\sjz_build2.log`: [5/7] `2392 passed, 24 skipped, 2 xfailed, 0 failed in 553.41s`; [6/7] `dist\SJZ-Control.exe` 03:11:11 **103MB**; [7/7] onefile 同上 + Inno `dist-installer\SJZ-Control-Setup-1.1.0.exe` 03:11:39 **79.2MB**（compile 27.313s） |
| 03:10:15 | 离线4 全量回归（关联） | **收口，但非冻结快照** | 6 项旧失败实为**已改名用例**：lastfailed 里 `test_reports_the_lock_it_found` / `test_minimize_and_destroy_reach_the_window` / `..._to_legacy`×2 / `test_wheel_uses_real_mouse_event` / `test_exit_waits_for_input_release` 均已被重写或改名（现名 `..._to_fail`、`test_minimize_reaches_the_window_and_close_triggers_exit`、`test_wheel_moves_cursor_first`、`test_exit_forces_input_release_when_scheduler_wont_stop`），故 lastfailed 未清属陈旧记录；但 03:19–03:27 又改 `core\server.py`/`desktop.py`/`web\overlay.js`/`web\overlay.html` → 2392 结论只覆盖 03:10 快照 |
| 03:16–03:18 | 前端/悬浮窗联通（离线1 关联） | **WebSocket 403 已修复** | `sjz_verify_out.log` 为 `Handshake status 403 Forbidden`；`out3` 三次全绿：`overlay_conn=已连接` tier=connected，conn_ms 1220/1236/1230，api_log n=15 f403=0 bad=0，close_ms 6.4/4.6/4.4，exit_code 0 |
| 03:22–03:26 | 悬浮窗定位验收 | 4 轮一致，证据未落盘 | 4 次全部 `悬浮窗已定位 (1600,16) 工作区 1920x1080`（上轮 1584,16），SW_SHOWNOACTIVATE 4 次、每次约 2s 自退；`%TEMP%\overlay_verify.json` 仍 **MISSING** |
| 02:49–03:09 | 输入释放告警 | **撤销「产品缺陷」定性** | 4 次「退出时输入可能没释放干净」全部紧跟 `pytest-of-26346\pytest-1607/1608/1609/1610` 的资源释放行，均发生在用例内；对应新用例 `test_exit_forces_input_release_when_scheduler_wont_stop` 已 PASS |
| 03:36 | 运行态 | 全静默 | 无 python/pytest 进程（PID 29188/29340 已退出）；EnumWindows dafors 自有可见窗口 **0**（全机可见窗口 0） |

无变化: 319accdc(缺口1)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话 **零新报告产物**；git HEAD 仍 `5eef554`，未提交 385 项；`perf\out` 00:44、`_perf_flow.md` 00:12。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >11h。

异常/建议: ①全量 2392 通过跑在 03:01–03:10，其后 03:19–03:27 又改 4 个文件 → 需文件静止后独占重跑一次，才能出离线4 的冻结快照；②悬浮窗定位 4 轮一致但只写进 desktop.log，`overlay_verify.json` 未生成 → 建议补落盘，否则验收无机器可读证据；③输入链路 120 连击尾部 19/120 自 00:44 起 >2h50m 未复测，仍是真机门槛唯一硬缺口；④离线5 性能/稳定性自 00:44 无新跑，01b 仍退回未决策。

真机提醒: 本轮不触发（缺口2/缺口4 决策未给、120 连击未复测，工具链尚差一项）。

## 2026-11-08 04:05 第二十三次快照（对比 03:36，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names:["list_sessions"]` 第七次查无）; 判定依据 = 文件 mtime + 进程表 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 03:31:47 | **真机现场（重大变化）** | **游戏已运行** | `DeltaForceClient-Win64-Shipping` PID 18320，窗口「三角洲行动」可见，03:31:47 启动并持续至今；03:31:30 伴随 VC++ 运行库安装日志 — 真机验证窗口此刻已打开 |
| 03:44–04:00 | 干跑复放线（未入清单） | **新验证线跑通** | `_dryrun_out\nav_frames.json` 04:00:51: 12/12 帧 `ok=true`（大厅/过渡/市场，真机 OCR 原文 6 帧 + 拼接）；`replay.json` 03:56:13: 32 帧，11 帧带判定字段且与期望一致，其余 21 帧无 `ok` 字段=未设判据，不计失败；新增 `core\dry_run.py`(03:44:48)、`core\nav.py`(03:59:26)、`tools\replay_dryrun.py`(03:53:55)、`tests\test_replay_dryrun.py`(04:00:41)、`tests\fixtures\nav_frames\hall_to_market.jsonl`(04:00:30) |
| 03:44–03:59 | 代码改动 | 活跃 | `core\preflight.py`(03:44:13)、`core\engine.py`(03:49:17)、`core\cards.py`(03:55:55)、`core\input.py`(03:59:36)、`core\config.py`(03:59:38)、`desktop.log`(03:59:57 线程预算行) |
| 03:59:56 | 定向回归 | 在跑 | `pytest tests -q -k "click or landing or cursor or nav"` PID 32668，结论未出；`lastfailed` 仍 6 项（03:10 已改名用例的陈旧记录，非新失败） |

无变化: 319accdc(缺口1,已完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话 **零新报告产物**（`_pool_fields.md` 21:01、`_nonreal_trust.md` 21:14、`_frontend_rollback_flake.md` 23:44、`_test_isolation_lock.md` 21:26、`_perf_flow.md` 00:12、`_merge_log.jsonl` 20:05、`tools\click_channel_report.json` 00:47、`perf\out` 00:44）。git HEAD 仍 `5eef554`，未提交 390 项。

桌面窗口: 全机可见 14 个（三角洲行动/WeGame/MuMu/Token 看板/微信等，均用户程序），**dafors 自有可见窗口 0**；`desktop.log` 03:26 后无「窗口已显示」，上轮 21 轮违规已停。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >12h。

异常/建议: ①离线4 冻结终稿自 17:24 起 >10.6h 仍缺，建议用 03:10 的 2392 passed + 本轮定向回归结果直接收口；②离线5 自 00:44 无新跑、`overlay_verify.json` 仍 MISSING（悬浮窗验收仍无机器可读证据）；③输入链路 120 连击还原尾部 19/120 自 00:44 起 >3h 未复测，仍是真机采集前唯一硬缺口。

真机提醒: **可执行**（游戏已开、矩阵「必须开游戏 25 项」、collect_b1–b5 齐备）。开跑前两步: ①以管理员启动（`--elevate` 或界面「以管理员重启」）——游戏窗口完整性更高，不提权会被 UIPI 静默丢弃；②空闲 CPU 跑 `tools\verify_click_channel.py` ≥120 连击，确认 restore 尾部失败率 ≤5%（当前仅 50 连击 delta=0 的证据）。随后 `SJZ_NO_WINDOW=1` 下按 B1→B5 采集。

## 2026-11-08 04:31 第二十四次快照（对比 04:05，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第八次查无）; 判定依据 = 文件 mtime + 进程表 + .pytest_cache + 报告产物 + EnumWindows。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 04:31:35 | **真机现场（窗口已关闭）** | **游戏已退出，无采集产物** | `DeltaForceClient-Win64-Shipping` 与「三角洲行动」窗口均不在进程表/可见窗口表中（04:05 时 PID 18320 在跑）；全项目与 `%TEMP%` 扫描未见任何 B1–B5 采集输出（`collect_b1..b5`、`dafors_r0`、json 产物均无 03:30 后新文件）→ 真机采集未产出数据 |
| 04:14–04:15 | 代码改动（未入清单） | 活跃、无报告 | `core\input.py` 04:14:28、`core\nav.py` 04:14:52、`core\config.py` 04:14:56、`core\engine.py` 04:15:24；`desktop.log` 04:15:32 仅「线程预算 4」 |
| 04:16–04:26 | 回归/测试线 | 无新失败 | `%TEMP%` 5 个 `pytest-of-26346` 目录 + 04:16–04:19 十余个 `dafors-records-*.lock`；`lastfailed` mtime 04:20:28 仍只 6 项，且 6 个用例名已全部改名（`Grep def` 零命中）→ 陈旧记录，非新失败；04:20:22 `tests\test_replay_dryrun.py` 改动 |
| 04:31 | 干跑复核（dry-run 12/12） | 判据复核通过（非缺陷） | `_dryrun_out\nav_frames.json` 12 帧全 `ok=true`；其中 transition 帧「判为 hall 仍 ok」是 `tools\replay_dryrun.py` L293-295 明定的宽松判据（过渡帧只要不在 MARKET_PAGES 即通过），非判定错误 |
| 04:29–04:31 | OCR 服务 | 在跑 | `ocr_service_8081.log` 04:29:30→04:31:43 持续写入（paddlex 子进程 34536/34808） |

无变化: 319accdc(缺口1,已完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话**零新报告产物**（`_pool_fields.md` 21:01、`_nonreal_trust.md` 21:14、`_frontend_rollback_flake.md` 23:44、`_test_isolation_lock.md` 21:26、`_perf_flow.md` 00:12、`_merge_log.jsonl` 20:05、`tools\click_channel_report.json` 00:47、`perf\out` 00:44）。

桌面窗口: 全机可见 10 个（WeGame/微信/NVIDIA Overlay/Token 看板等，均用户程序），**dafors 自有可见窗口 0**；`overlay_verify.json` 仍 MISSING。
工作区: git HEAD 仍 `5eef554`，未提交 390 项。

挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >12.5h。
真机提醒: 04:05 已提醒且当时游戏在跑，现游戏已关、零采集产物 → 本轮不重复提醒；如需继续真机验证，需重新开游戏（管理员/unpacked 窗口）+ 先跑 ≥120 连击还原复测（当前唯一硬缺口仍为 50 连击 delta=0、120 连击尾部 19/120 未复测）。

## 2026-11-08 05:02 第二十五次快照（对比 04:31，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第九次查无）; 判定依据 = 文件 mtime + 进程表 + 端口监听 + .pytest_cache + 报告产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 04:41:21 | **真机现场（再次打开）** | **游戏重开** | `DeltaForceClient-Win64-Shipping` PID 34848 启动，窗口「三角洲行动」可见（04:31 曾全部退出）；04:41 起 CPU 累计 3107s |
| 04:30:24 | **dafors 应用（真机运行中）** | **新增运行态** | PID 36332（python.exe）自 04:30:24 运行，监听 `127.0.0.1:8799`（前端桥），CPU 1329s/31min；`desktop.log` 仍止于 04:15:32（该进程不写主日志） |
| 04:53:03 | **真机采集（有产物）** | **新证据** | `data\ea9759b5\buy.png` 15,182B、`confirm.png` 17,216B，同一秒落盘；`config.json` 同时刻写入，`config.json.good` 04:55:46 |
| 05:01:28 | **模板自愈（实质进展）** | **首次机器可读证据** | `templates\meta.json` → `auto_heal[0]`: `buy.png` recaptured，`size_px 298×58`，`from_score 0.762`，`reason=界面与模板差异过大，按当前画面重采`；`templates\buy.png` 同步重采（05:01:28），确认 1920×1080 / DPI 96 |
| 04:31→05:01 | OCR 服务（端口迁移） | 8081 停、8082 活 | 8082 监听 PID 34536（父 34808），`ocr_service_8082.log` 05:01:02 仍持续 `POST /ocr 200`；`ocr_service_8081.log` 止于 04:31:43 且 8081 无 LISTENING |
| 05:01 | 窗口合规 | **1 个可见控制台（违规）** | PID 36332 控制台窗口 `IsWindowVisible=True`，rect `208,208–1201,727`，标题为 python.exe 路径 → dafors 自有可见窗口 1 个，违反「不得留可见窗口」；建议改 `pythonw`/`SW_HIDE` |

无变化: 319accdc(缺口1,已完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话**零新报告产物**（`_pool_fields.md` 21:01、`_nonreal_trust.md` 21:14、`_frontend_rollback_flake.md` 23:44、`_test_isolation_lock.md` 21:26、`_perf_flow.md` 00:12、`_merge_log.jsonl` 20:05、`tools\click_channel_report.json` 00:47、`perf\out` 00:44）。

回归: 无新跑 — `%TEMP%\pytest-of-26346` 最后写入 04:26:52；`lastfailed` mtime 04:20:28 仍 6 项陈旧记录。
工作区: git HEAD 仍 `5eef554`，未提交 390 项。
采集: 全项目与 `%TEMP%` 仍无 `collect_b1..b5` / `dafors_r0` 产物 → B1–B5 未开始。
挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回 — 均 >13h。
真机提醒: **可执行且现场已就绪**（游戏 04:41 重开、应用在跑、自愈已触发）。开跑前两步: ①确认/改为管理员启动（当前 36332 权限未知，不提权会被 UIPI 静默丢弃）；②空闲 CPU 跑 `tools\verify_click_channel.py` ≥120 连击，确认 restore 尾部失败率 ≤5%（现仅 50 连击 delta=0、hit_rate 1.0 的证据）。随后 `SJZ_NO_WINDOW=1` 下按 B1→B5 采集。

## 2026-11-08 05:35 第二十六次快照（对比 05:02，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 第十次查无，`tool_names:["list_sessions"]` 无此工具）; 判定依据 = 文件 mtime + 进程表 + 端口监听 + pytest 缓存 + 报告产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 05:29:42 | **真机发布就绪线（未入清单）** | **新交付报告** | `docs\release_readiness.md` 10,682B：F4 进交易行 **2,786 ms**（delta=(0,0)、restored=True）；枪皮页 60s 影子 **77 轮/383 落点/越界 0/注入 0**；六类拒绝页分类全对；挂单最低价 曼德尔砖 10,665 / 挂饰 5,201 / 枪皮 118,888；page_check+gpu_guard **17 passed**；F8 停 **62 ms** |
| 05:09–05:17 | 代码改动 | 新模块 | `core\page_check.py` 05:09:15、`tests\test_page_check.py` 05:09:03、`core\gpu_guard.py` 05:17:03、`tests\test_gpu_guard.py` 05:10:05、`core\engine.py` 05:10:26 |
| 05:18:55 | 打包审计 | 通过 | `build\pack_audit.json` **ok=true**：required_files 420、datas 44、hidden 20、data/module diff 均空 |
| 05:06–05:21:58 | 真机采集 | 9 组 buy/confirm 落盘 | `data\46e0738c`(05:06:23) + `65f76fdf/5ead34c4/3991cd6e/18d6f386/c387bcfb/2c184a79/9f894e91`(05:19:34–35) + `8725e8cf`(05:21:58)；buy 16938B、confirm 17216B |
| 05:28:30 | 模板替换（异常待核） | buy.png 被换成 05:19 采集 | `templates\buy.png` SHA256 `CF2E47…` == `data\9f894e91\buy.png` == `data\8725e8cf\buy.png`；但 `templates\meta.json` 仍停 05:01:28（auto_heal 仅 1 条）→ 图换了、元数据没记，触发方需确认 |
| 05:29:16 | 回归 | **lastfailed 刷新，9 项** | 3 项对应当前存在的用例：`test_precision::test_external_thread_observes_each_firing`、`test_portability_hardening::test_scale_curve_is_measured_and_fixed`、`test_desktop_jsapi::test_only_the_intended_methods_are_visible_to_js`（时序/线程/JS 面敏感，疑似高负载 flake，需单独复跑确认）；另 6 项为改名后残留（`*_legacy`→`*_fail`、`wheel_uses_real_mouse_event`、`exit_waits_for_input_release`、`reports_the_lock_it_found`、`minimize_and_destroy_*` 在 `tests\` 已无同名 def） |
| 05:32 | 进程/服务 | 稳定 | 游戏 `DeltaForceClient-Win64-Shipping` PID 34848（04:41 起）；dafors PID 36332 监听 8799；OCR 8082 PID 34536 日志写至 05:32 |
| 05:31:51 | 窗口合规 | **仍有 1 个可见控制台** | `python.exe` 窗口 PID 36332 可见（其余 11 个均用户程序）；dafors 自有可见窗口 = 1，违规未修 |

无变化: 319accdc(缺口1,已完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话**零新报告产物**。

工作区: git HEAD 仍 `5eef554`，未提交条目 395（上轮 390）。
采集: 全项目仍无 `collect_b1..b5` 输出（仅 11/07 的脚本）。
挂起项: 缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回，均 >13h。
真机提醒: 真机验证已在跑并出了首份报告；用户侧两件必须先做的事: ①区域微调手框「购买按钮」「确认弹窗」（自动标定本机必 fallback：buy 0.702/偏 66,31；confirm 0.223）；②充值 ≥5,201（现 2,049，差 3,152），并等 08:00 交易时段（现 05:3x 已过时段）。

## 2026-11-08 06:05 第二十七次快照（对比 05:35，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 查无此工具，`tool_names:["list_sessions"]` 未返回）；判定依据 = 文件 mtime + 进程表 + 端口监听 + pytest 缓存 + 报告产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 05:48:46 / 05:49:23 | **打包/交付线（未入清单）** | **新交付** | `dist-onefile\SJZ-Control.exe` 108,048,538B；`dist-installer\SJZ-Control-Setup-1.1.0.exe` 83,072,086B（一版完成，但无对应报告 md） |
| 05:57:49 / 05:59:19 / 06:00:49 | **打包版启动** | **新缺陷（3 连败）** | `dist\SJZ-Control\desktop.log` 三次均写「单实例闸被占，但读不到旧实例 PID（instance.json 缺失）」，窗口探测 total=0 visible=0 → 直接退出；`%LOCALAPPDATA%\SJZ-Control` 无 instance.json，持有者为源码版 PID 36332（老版本不写登记）→ 双击无反应 |
| 06:01:41 | 自动标定落盘 | 实质进展 | `config.json` 12,563→13,368B：`calibration.path` 由 "manual"→""，新增 `buy_anchor`（05:59:00，button `[1481,902,304,59]`，cd 文本「剩余：7小时26分」`[1554,874,156,22]`）；`regions.buy.x` 0.798438→0.771875（左移 51px）；confirm 仍 absent score 0.215（用户第二步手框未做） |
| 06:01:22 / 06:01:35 | 代码活跃 | 正在改 | `core\autocalib.py`、`core\server.py` 刚写入，标定链路仍在调整中 |
| 05:45:28 | OCR 端口 | 8082→8081 | 8081 LISTENING PID 45872，06:01 仍持续 `POST /ocr 200`；8082 已无监听 |
| 05:37–05:39 | 真机采集 | 新增 4 组 | `data\0ae99b07`(05:37:09)、`b51fe81b`/`f9489009`/`148e1d8f`(05:37:12)、`c8fd1819`(05:39:32) |
| 05:50:25 | 回归缓存 | 7 项（6 项陈旧） | 仅 `test_precision::test_external_thread_observes_each_firing` 在 `tests\` 有同名 def；其余 6 项（cursor_lock / desktop_jsapi / mutation_killers×2 / nav_scroll / portability_hardening）已无同名用例 → 缓存残留，不代表 6 个真失败 |
| 06:01:35 | 窗口合规 | **仍违规 1 个** | `python.exe` PID 36332 可见窗口（标题为解释器路径），dafors 自有可见窗口 = 1 |

无变化: 319accdc(缺口1,已完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b)、fa59b8fa(合并收口) — 11 个清单会话零新报告产物（`_pool_fields.md` 21:01、`_nonreal_trust.md` 21:14、`_frontend_rollback_flake.md` 23:44、`_test_isolation_lock.md` 21:26、`_perf_flow.md` 00:12、`_merge_log.jsonl` 20:05、`perf\out` 00:44）。

工作区: git HEAD 仍 `5eef554`，未提交条目 396（上轮 395）。
挂起项（>9h 无产）：缺口2 两决策、缺口4 三决策(含 PUBLIC_KEY_B64)、01b 退回。
真机提醒: 未触发（离线五路仍未交付；矩阵仍无「必须开游戏」清单输出）。

## 2026-11-08 06:32 第二十八次快照（对比 06:05，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 查无此工具）；判定依据 = git 提交与工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 报告产物。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 06:15–06:30 | **合并收口 fa59b8fa** | **实质进展（大）** | `5eef554..e763d59` 落 20 个提交，561 文件 +160,751/−2,816；未提交 396→0；HEAD `e763d59`「docs: 补 server/regions 链路回归数字与本次补绿原因」 |
| 06:16 | **缺口4 授权密钥 eea45452** | 阻塞解除（代码层） | `core\license.py:65` 已内置 `PUBLIC_KEY_B64="VtWMhwsK…"`，运行期旁路参数已移除 |
| 05:59–06:30 | 自动标定线 | 实质进展 | confirm 区域 `absent(0.215)`→`[0.60625,0.638889,0.157812,0.064815]`；buy 锚点 measured `button[1481,902,304,59]`、cd 文本「剩余：7小时26分」；`regions.buy.x` 0.798438→0.771875（左移 51px）；config.json 06:30 13,368→13,116B |
| 06:31:44 | 打包审计线 | 新增产物·通过 | `build\pack_audit.json` ok=true，datas 44 / hidden 20 / required 420，data_diff=[] module_diff=[] dangling=[] |
| 06:31:33 | 回归 | 新 pytest 落盘 | lastfailed 7 项，其中 6 项仍无同名 def（陈旧节点）；真失败仍仅 `test_precision::test_external_thread_observes_each_firing` |
| 05:37–06:22 | 真机采集 | 新增 8 组 | 05:37:09 / 05:37:12×3 / 05:39:32 / 06:20:09 / 06:20:14×2 / 06:22:50；最新 `2bc7a53f` 仅 buy.png+confirm.png（标定取样） |
| 06:03:33–37 | 打包版启动 | **未修（构建落后）** | dist 日志已无「读不到旧实例 PID」，改为窗口显示后 1s 自关（06:03:37）；dist 产物停在 06:03，源码已提交到 06:30 → 需重打包后再验 |
| 06:32 | 窗口合规 | **仍违规 1 个** | `python.exe` PID 36332 可见窗口（04:30 起，CPU 4,929s）；其余可见窗均为用户程序 |

无变化: 319accdc(缺口1)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、935cc15a(离线5)、ea4e1e45(01b) — 无新报告产物（最新 `_*.md` 仍 11-08 00:13）。

挂起项: 缺口2 两决策（无产物）、01b 退回。
真机提醒: 触发条件部分满足（游戏在跑 PID 34848 + 标定已补 confirm/buy）；待用户确认 ① 余额是否 ≥5,201（上轮 2,049）② 当前是否已进入可交易时段。

## 2026-11-08 07:03 第二十九次快照（对比 06:32，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names:["list_sessions"]` 返回 0）；判定依据 = git 工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 报告产物 + 可见窗口枚举（user32 EnumWindows）。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 07:01 | 窗口合规 | **已恢复** | 可见窗口 14 个中 dafors 自有 = 0（旧 `python.exe` PID 36332 已退出；8799 无监听）；其余均为用户程序（含 `pythonw` 19400 Token 看板） |
| 06:33–06:53 | 打包线（未入清单） | **旧缺陷已过** | `dist-onefile\SJZ-Control.exe` 108,066,369B（06:33:26）、`dist-installer\SJZ-Control-Setup-1.1.0.exe` 83,072,672B（06:34:07）；06:51:41 实跑：config 6,963B + `data\a8a77e49\{buy,confirm}.png` 落盘、许可 ok(pro，剩 3,528.1s)、无 instance.json 报错；日志止 06:51:59（--doctor 形态），进程 06:53 后退出 |
| 06:54–07:01 | **离线验证5 935cc15a** | **首次实质进展** | 新增 `perf\bench\ocr_path_probe.py`(06:54:41)、`ocr_scaling_probe.py`(06:57:42)、`fastdet_probe.py`(07:00:38)、`roi_loss_probe.py`(07:01:22)；产出 `perf\out\ocr_path_probe_before.json`（rep=5、git e763d59）：catalog 全图 OCR p50 **329.9ms** / p95 **379.8ms** / cold p50 **1,558.8ms**，n_raw 5→n 4 |
| 06:39–06:59 | 代码改动 | 未提交 10 项 | 修改 `core\nav.py`(06:48:37)、`core\nav_col.py`(06:39:38)、`core\engine.py`(06:39:42)、`run.py`(06:59:45)、`tests\test_nav.py`(06:45:16) + 4 个未跟踪 perf 脚本；HEAD 仍 `e763d59` |
| 06:46:03 | 回归缓存 | 无新增真失败 | lastfailed 7 项；6 项复核 def=0（陈旧节点），真失败仍 `test_precision::test_external_thread_observes_each_firing`（def=1） |

无变化: 319accdc(缺口1)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) — 零新产物，最新非 watch 报告仍 11-08 00:13。

挂起项: 缺口2 两决策（>10h 无产）、01b 退回（>10h 无产）；缺口4 已解（`core\license.py:65` 内置 PUBLIC_KEY_B64）。
真机提醒: 未触发（离线五路仅验证5 有动作、未全交付；矩阵无「必须开游戏」清单；工作树有 10 项未提交修复）。
异常建议: 44755992 / 697db934 / b5a50cc0 / 25a3c42 / fa2c17bf 五线 >10h 零产物，疑未真正启动或已死，建议重派或移出清单；da85f6a6 / ea4e1e45 卡在待用户决策。

## 2026-11-08 07:34 第三十次快照（对比 07:03，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 查无）；判定依据 = git 工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 报告产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 07:11–07:29 | **离线验证5 935cc15a** | **实质进展（性能）** | 新增 `perf/bench/chain_bench.py`、`chain_ab.py`，4 臂各 n=6：e2e p50 base **11,451.7ms** / reuse **9,189.3ms**（−19.8%）/ fast 11,206.6ms / both **9,874.6ms**（min 7,199.0ms）；reuse_hits base 0 / reuse 8/8/8 / both 8/8/8；`ensure_public_filter` 630.5ms→1.8ms |
| 07:08–07:30 | 代码改动（未提交） | 新增 10 文件改动 | `core/ocr.py`+55、`core/ocr_rapid.py`+104（fast_det 0.75 + 同帧复用）、`core/nav.py`+169（judge_click/CANCEL_DIFF 0.05）、`core/nav_col.py`+135（单击/双击 + 确认回读重试≤1）、`core/engine.py`（ESC 仅在识别到「已被购买」时发）、`run.py`+30（8799/8801 端口预检）、`core/config.py`+3；合计 +589/−128，HEAD 仍 `e763d59` |
| 07:21–07:33 | **真机验收（temp_20261108_12\sjz_e2e2）** | **报出新缺陷 4 项** | ① 赛季/组合筛选：选完赛季面板未关，page=`filter_collection`、0 卡片；回读 赛季=`棱镜攻势` / 品阶=[F,F,T,F]，期望「美杜莎×普通」→ 失败；② 赛季下拉第六项 OCR 乱码「持镜攻热C2」（应为 棱镜攻势S2，未修）；③ 普通品阶价格令牌被图标拆散，只读到个位数，未下单 → 验收三未做；④ scanB 第 2 轮 OCR 引擎报错中断，两次完整扫描只完成 1 次（scan_equal=true 但标部分通过）。通过项：首屏 7→全量 8、M1=M2、subset 4/4 true、普通/稀有/步枪各 7 条 |
| 07:30:21–07:33:32 | 真机复验 run2 | **进行中** | `accept_run2.log` 07:30:22 至今 0B；截图 07:30:38–07:33:32 共 11 张（`ac_*_073021.png`），最后一次 07:33:32 |
| 07:25:20 | 文档 | 新增 | `docs/season_filter_validation.md` 3,994B（含上面真机结论与未通过清单 4 条） |
| 06:59–07:30 | 现场事故 | **已修（未提交）** | 旧实例 :8799 与新实例同时注入鼠标 → `run.py` 加端口预检；当前 8799/8801 均无监听，无双实例 |
| 07:06:12 | 回归缓存 | 无新增真失败 | lastfailed 仍 7 项（6 项陈旧节点 + `test_precision::test_external_thread_observes_each_firing`）；`test_nav*` 115 passed（文档口径） |
| 07:31 | 窗口合规 | **合规** | 可见窗口 14 个，dafors 自有可见 **0**；:8081 OCR 服务 PID 45872（05:45 起，CPU 1,210s）无窗口，仅 API 日志 |

无变化: 319accdc(缺口1)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) — 零新产物，最新非 watch 报告仍 11-08 00:13。

挂起项: 缺口2 两决策（>11h）、01b 退回（>11h）；缺口4 已解。
真机提醒: 未触发（五路仅验证5 在跑、未全交付；工作树 10 项未提交；覆盖矩阵无「必须开游戏」清单）。真机侧另有独立验收轮在跑（余额 2,049，未达美杜莎最低挂单 4,000）。
## 2026-11-08 08:04 第三十一次快照（对比 07:34，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names:["list_sessions"]` 返回 0）；判定依据 = git 工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 报告产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 07:43:42 | **离线验证5 935cc15a** | **实质进展（A/B）** | `perf/out/chain_ab.json` 4 臂各 n=6：e2e p50 base **13,858.1ms** / reuse **10,553.6** / fast **13,511.1** / both **9,645.5**（min 7,859.1），both −30.4%；reuse_hits 8/8/8；`ensure_public_filter` 1,132.4ms→1.9ms |
| 07:45–08:01 | 离线验证5 | 新增改动+探针 | `core/ocr_rapid.py`（页码条 det 降采样 3240→1300、rec 仍原像素）、`core/ocr.py`（同帧逐像素复用）、`core/config.py`（`rapid.fast_det_screen=1.0` 等 3 开关）；`perf/bench/` 新增 `probe_cost.py`（重写）、`screen_cost.py`、`round_report.py` |
| 08:03:14 | **离线验证5** | **交付（报告）** | `docs/perf_ocr_path.md` 9,582B：e2e p50 **13.0s→8.1s（−37.6%）**；整屏 OCR 5× **7,888.5→2,568ms（−67%）**；页码条 OCR **3,355.2→412.6ms（−88%）**；剩余固定等待 **≈4.7s 占 69%**（nav/nav_col 严禁区，只报不改）；OCR 回归 178 passed。**未提交** |
| 07:58:20–08:03:37 | **真机验收（temp_20261108_12\sjz_e2e2）** | **进行中（scan-only）** | `accept.py --scan-only` PID 45436；scanA 60 行 08:03:00 完成，`ac_*.png` 累计 **294 张**，08:03:37 仍在抓 top；CPU 860.7s（≈2.8 核均摊）/308MB/59 线程；暂无结论 |
| 07:34–08:04 | 工作树 | 未提交 **21 项** | modified 12（+697/−295）：`core/ocr.py`+69、`core/ocr_rapid.py`+120、`core/nav.py`169、`core/nav_col.py`135、`core/engine.py`、`run.py`+30、`core/config.py`+6、tests 3；untracked 11（perf/bench 8 + docs 2 + _watch）；HEAD 仍 `e763d59` |
| 07:51 | 回归缓存 | 无新增真失败 | lastfailed 仍 7 项（6 陈旧节点 + `test_precision::test_external_thread_observes_each_firing` def=1） |
| 08:03:37 | 窗口合规 | **合规** | 可见窗口 14 个，dafors 自有可见 **0**；:8081 OCR 服务 PID 45872 无窗口；游戏 PID 34848 在跑；8799/8801 无监听 |

无变化: 319accdc(缺口1)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) — 零新产物。

挂起项: 缺口2 两决策（>12h）、01b 退回（>12h）；缺口4 已解。
真机提醒: 未触发（五路仅验证5 交付，未全交付；矩阵仍无「必须开游戏」清单；工作树 21 项未提交）。真机侧独立 scan-only 轮正在跑（游戏在 PID 34848）。
异常建议: 44755992 / 697db934 / b5a50cc0 / 25a3c42 / fa2c17bf 五线 >10h 零产物，建议重派或移出清单；da85f6a6 / ea4e1e45 卡待用户决策。

## 2026-11-08 08:36 第三十二次快照（对比 08:04，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names` 查无此工具）；判定依据 = git 工作树 + 文件 mtime + 进程表 + pytest 缓存 + 真机验收产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 08:08:34 | 真机验收（scan-only） | **完成但未通过：scan_equal=False** | `accept_075822.json`：scans first=7 / total=223，两次完整扫描不等价 → 与 07:21 缺陷④同源，仍未闭环 |
| 08:12 | 真机验收 | **报出新缺陷** | `cases2.log`: `core\nav_col.py:92` `NavError`「确认」点了 2 次面板仍在（page=filter_collection）→ `reset_all` 中断；同日志前段有「「确认」补点到第 2 次才关掉面板」warn |
| 08:16→08:33 | 真机验收 scanB（复跑） | 仍在跑 | `scan4.log` 至第 116 轮：累计唯一 **212**、重叠 3–7、diff 3.7–4.8；截图 **896 张 / 2,081MB**；08:31:53 `nav.py`、08:31:54 `nav_col.py` 被改 = 修复正在写 |
| 08:27 / 08:28 | **离线验证5 935cc15a** | 交付后继续补 | `docs\perf_ocr_path.md` 更新（08:27）、`perf\bench\probe_cost.py` 重写（08:28）；主数字沿用 08:03 版（e2e 13.0→8.1s −37.6%、整屏 OCR −67%、页码条 −88%） |
| 08:32→08:34 | 定向回归 | **全绿** | `pytest tests/test_nav.py nav_filter ocr_backend ocr_price market cards autocalib`（PID 35492）163s 结束；`nodeids` 08:34:53 更新、`lastfailed` 未变（6 陈旧 + precision 1） |
| 08:04→08:33 | 工作树 | 未提交 21→**31** | 新增改动 `core/config.py`、`countdown.py`、`digit_ocr.py`、`input.py`、`precision.py`、`selfcheck.py`、`server.py`、`autotune.py`、`evo/collect/{parse,schema}.py`；HEAD 仍 `e763d59` |
| 08:33 | 窗口合规 | 合规 | 可见窗口 14（用户程序），dafors 自有 **0**；游戏 PID 34848（04:41 起）仍在；:8081 OCR PID 45872 无窗口 |

无变化（零新报告产物）: 319accdc(缺口1,完成 21:01)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口，06:30 已落 20 提交)。`desktop.log` 止于 06:29:56（>2h 无新行）；`_merge_log.jsonl` 20:05；`tools\click_channel_report.json` 00:47（120 连击仍无证据）。

挂起项: 缺口2 两决策、01b 退回 — 均 >13.5h。
真机提醒: **未触发**（五路仅验证5 交付；矩阵仍无「必须开游戏」清单；工作树 31 项未提交）；真机侧已自行在跑，不需再开游戏。
异常建议: ①08:12 `NavError` 与 07:21 缺陷①同一路径（`col_filter_confirm`），08:31 已改但未验证 → 文件静止后单跑 `reset_all`；②`scan_equal=False` 复现，scanB 需两次完整扫描后再比；③五线 44755992/697db934/b5a50cc0/25a3c42/fa2c17bf >13h 零产物，建议重派或移出清单；④`temp_20261108_12\sjz_e2e2` 已 2,081MB/896 张，跑完按临时产物规则删除。

## 2026-11-08 09:03 第三十三次快照（对比 08:36，仅记变化）

> 工具说明: 本环境仍无 list_sessions（`tool_names:["list_sessions"]` 返回 0 工具，第十一次查无）；判定依据 = git 提交与工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 真机验收产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 08:36→08:53 | **离线验证5 935cc15a** | **完成（终稿）** | `docs/perf_ocr_path.md` 08:53:45 / 18,201B：e2e p50 **14,167.2→7,431.5ms（−47.5%）**，第二组交替跑 13,019.6→8,117.6（−37.6%）；整屏 OCR 5× **−67%**（1,540→430ms/次，4/5 命中同帧缓存）、页码条 OCR **−88%**（791→66ms/次）、`where()` 顶层 −97%；剩余固定等待 **≈4.7s（≈36%）** 在 nav/nav_col 未动；第三轮「中等 ROI det 0.75」仅 −7% 已回退；回归 **234 passed /152.32s** |
| 06:30→09:02 | **合并收口 fa59b8fa** | 继续落提交 | `e763d59..fca59f4` 共 5 提交：`6dd0c1c` nav 固定等待→条件轮询、`045cc9a` 同帧逐像素复用、`84709ef` 页码条 det 降采样、`46456d2` 清 20 个无引用符号（11 文件）、`fca59f4` perf 文档；23 文件 +1,550/−255；未提交 **31→9** |
| 08:15 | 真机验收（未入清单） | **缺陷部分修复，2 项未通** | `docs/season_filter_validation.md` 6,215B：赛季 OCR「持镜攻热C2」→`_SERIES_OCR_FIX` 纠正「棱镜攻势S2」；`col_filter_confirm` 双击+回读+最多补 1 次；赛季/组合回读 **PASS**（美杜莎×普通 [F,F,F,T]、棱镜×稀有 [F,F,T,F]、子集 True）；`test_nav*` **117 passed**；**未通**：全量枚举 scanA 223 撞 60 轮上限、scanB 212，`M1≠M2`（仅A 38 / 仅B 27 条），验收三未成交（余额 2,049 < 美杜莎最低挂单 4,000） |
| 09:02 | 真机现场 | 临时产物已清 | `temp_20261108_12\sjz_e2e2` 已不存在（原 2,081MB/896 张已删，符合临时产物规则）；全项目 `accept_*.json` / `scan*.log` 零残留 → 文档引用的证据 JSON 已不可回溯 |
| 09:02 | 回归缓存 | 无真失败 | lastfailed 6 项，逐个复核 6 个 `def` 均 **def=0**（改名后陈旧节点）；上轮的 `test_precision::test_external_thread_observes_each_firing` 已不在缓存 |
| 09:02 | 运行态/窗口 | 合规 | 游戏 PID 34848（04:41 起）仍在；:8081 OCR PID 45872 无窗口；8799/8801 无监听；可见窗口 14 个均用户程序，dafors 自有 **0** |

无变化: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 零新产物（最新非 watch 报告仍 11-08 00:13）。

挂起项: 缺口2 两决策、01b 退回 — 均 >14.5h 零产物。
真机提醒: **未触发**（离线五路仅验证5 交付，离线1–4 仍零产物；矩阵无「必须开游戏」清单；工作树仍有 9 项未提交修复）。
异常建议: ①`docs/season_filter_validation.md` + 3 个 `perf/bench/*_probe.py` 仍未跟踪，建议随下个提交归档，否则证据只在本机；②全量枚举不收敛：4 格/轮 + 逐帧 OCR 已证不可行，需改「滚动条位置 / 末帧指纹双锚」或每轮 2 格 + 120 轮；③五线 44755992/697db934/b5a50cc0/25a3c42/fa2c17bf 自昨夜起零产物，建议重派或移出清单。

## 2026-11-08 09:38 第三十四次快照（对比 09:03，仅记变化）

> 工具说明: 本环境仍无 list_sessions（`tool_names:["list_sessions"]` 返回 0，第十二次查无）；判定依据 = 其他会话的 command-execution 记录（execution.json 的 cwd/PID + output.log）+ git 工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 09:08:25→09:36 | **真机验收线（清单外，cwd `temp_20261108_12\sjz_e2e2`）** | **实质进展：全量枚举 19/22 收口** | `pool_scan.py --only` 22 组合（棱镜攻势/电玩高手/命运王牌/气象感应/棱镜S2/全部赛季 × 4 稀有度），PID 42748，`output.log` 逐组回读：19 组 **到底=True / 回读=True**；仅「命运王牌×稀有」**两次一致=False**（其余 18 组一致）；单组 56.8–177.5s；「全部赛季×史诗」35 条（首屏 6）177.5s；剩 全部赛季×稀有/普通 未跑 |
| 09:08→09:36 | 同上 | **08:12 NavError 未再复现** | `[warn] 确认两次点不掉…停止，不重放 → 造一次净零变化再确认` 出现 5 次（命运王牌×史诗、气象感应×稀有/普通、棱镜S2×稀有/普通），每次均自愈继续枚举，无中断 |
| 09:06:40 / 09:16:17 | 同上（代码修复） | **新修复落地** | `core/nav.py`：购买点击 `hold=0.35`（实测短击/hold=0.08 无反应，hold=0.35 后 diff 42.09%、page=onsale）、失败早退；`core/nav_col.py`(+82)：倒计时锚点 `_BUY_ANCHOR_BOX=(1150,620,770,460)` 只读右下 12% 面积、偏差>120px 退回固定坐标、`_click_at` 统一入口、恢复链「只在确认识别到弹窗时按 1 次 ESC」、确认最多补 1 次 |
| 09:11:48 / 09:11:57 | 同上（测试） | **新增 20 个用例，无真失败** | `tests/test_nav.py` +154 行、`tests/test_nav_filter.py` +93 行：`test_buy_fails_fast_when_click_changes_nothing`、`test_buy_point_uses_countdown_anchor`、`test_recover_sends_one_esc_only_when_panel_confirmed`、`test_confirm_retries_once_then_succeeds`、`test_series_ocr_fix_maps_garbled_s2_name` 等；`lastfailed` 9:11:57 仍 6 条，逐个核 `def` 均为 0（改名陈旧节点）→ 9:11 这轮无真失败 |
| 09:16:23 / 09:21:52 | **离线验证5 935cc15a** | 交付后加跑第二轮（无结果） | `perf/bench/chain_bench.py`、`poll_bench.py`(+39) 改；`poll_bench.py` PID 42324 09:28:07 起跑，9:36 前已退出；`perf/out` 无新 JSON（最新仍 `chain_ab.json` 07:43）→ 本轮无数字可引 |
| 09:03→09:38 | **合并收口 fa59b8fa** | 停手（未提交回升） | HEAD 仍 `fca59f4`，无新提交；工作树 **8 改 + 6 未跟踪 = 14 项**（+533/−38）；`docs/season_filter_validation.md` 仍 08:15 版，未更新 9:0x 修复 |
| 09:38 | 运行态/窗口 | 合规 | 可见窗口 13 个全为用户程序，dafors 自有 **0**；游戏 PID 34848（04:41 起）在跑；仅 :8081 监听（PID 45872 无窗口）；8799/8801 无监听；`temp_20261108_12` 仍在（scan 用），无 2,081MB 残留 |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13 前。

挂起项: 缺口2 两决策、01b 退回 — 均 >15.5h 零产物。
真机提醒: **未触发**（离线五路仅验证5 交付；矩阵无「必须开游戏」清单；工作树 14 项未提交）；且游戏 04:41 已开、真机枚举正在用这份游戏，不要再开第二份客户端。
异常建议: ①枚举收口后只剩「命运王牌×稀有」二次不一致，建议单独复跑该组合并落盘截图证据（scan 证据仍在临时目录，勿删）；②9:0x 两个修复 + 20 个测试必须尽快提交（当前 14 项未提交，违反「每次更新后提交」）；③五线 44755992/697db934/b5a50cc0/25a3c42/fa2c17bf 已 >16h 零产物，建议重派或移出清单。

## 2026-11-08 10:03 第三十五次快照（对比 09:38，仅记变化）

> 工具说明: 本环境仍无 list_sessions（`tool_names:["list_sessions"]` 返回 0，第十三次查无）；判定依据 = git 提交与工作树 + 文件 mtime + 进程命令行 + 端口 + pytest 缓存 + 临时产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 09:40 / 09:51 | **合并收口 fa59b8fa** | **推进：新落 2 提交** | `fac459b` 购买按住 0.35s + 无响应早退 + 倒计时锚点定位；`9f4799c` 购买点前等画面稳定 + 同点重试 + 文字右缘活点；HEAD `fca59f4→9f4799c`；未提交 14→**9**（M5 含 `_watch.md` / ?4），`+284/−11`；`core/nav.py`(09:46)、`tests/test_nav.py`(09:48) 已随 `9f4799c` 入库 |
| 09:51–09:54 | 真机/在线链路（清单外） | 有产物、**无结论** | 新增 9 组 `data/<hash>/buy.png`+`confirm.png`（09:51:59 ×8、09:54:02 ×1）；`docs/season_filter_validation.md` 仍 08:15 版，未写入 09:08 枚举结论（19/22 到底+回读、命运王牌×稀有两次不一致） |
| ≤09:55 | 真机枚举线 | **证据已删，不可回溯** | `temp_20261108_12`（原含 `pool_scan.py`/`output.log`/截图）整目录已不存在；全项目、Desktop、`%TEMP%` 均无 `pool_scan`/`accept_*`/`scan*.log` 残留 → 枚举结果只剩口头数字 |
| 09:55→10:02 | 新增未提交修复（在线） | 进行中 | `core/engine.py` 26 行：改为「OCR 真看到『已被购买/已被抢购/该商品已被』才发一次 ESC」，否则不发（防盲按抢用户操作）；`run.py` +30：启动前探 8799/8801，有实例直接拒绝启动（当日两实例抢鼠标事故）；`core/nav_col.py` 10:02:12 仍在改 |
| 10:01:09 | 回归缓存 | 无真失败 | `nodeids` 10:01:09 更新；`lastfailed` 仍 6 条，逐条 grep 6 个 `def` 全部 0 命中 → 均为改名陈旧节点 |
| 10:02:21→10:03 | 离线验证5 935cc15a | 加跑一轮，**无数字** | `perf/bench/live_chain.py 1` PID 46380/47996，10:02:21 起、10:03 前已退出；`perf/out` 最新仍 `chain_ab.json` 07:43 → 本轮无新 JSON |
| 10:03 | 运行态/窗口 | 合规 | 可见窗口 **14** 全为用户程序（含 三角洲行动 PID 34848 04:41 起、MuMu/MAA/WeGame），dafors 自有 **0**；监听仅 :8080（PID 23112 tok 采集）、:8081（OCR PID 45872 无窗口）；8799/8801 无监听 |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13。

挂起项: 缺口2 两决策、01b 退回 — 均 >17h 零产物。
真机提醒: **未触发**（离线五路仍只有验证5 交付；矩阵无「必须开游戏」清单；工作树仍有 9 项未提交）。游戏 PID 34848 已在跑，勿再开第二份客户端。
异常建议: ①枚举/验收证据（`temp_20261108_12`）先删后未出报告，`docs/season_filter_validation.md` 停留在 08:15 → 结论无法复核，建议按现有 9 组 data 截图补写结论并留证据；②9 项未提交（含 engine ESC 只读弹窗、run.py 单实例闸）必须先跑定向回归再提交，当前违反「每次更新后提交」；③五线 44755992/697db934/b5a50cc0/25a3c42/fa2c17bf 自昨夜起零产物，建议重派或移出清单。

## 2026-11-08 10:35 第三十六次快照（对比 10:03，仅记变化）

> 工具说明: 本环境仍无 list_sessions（`tool_names:["list_sessions"]` 返回 0，第十四次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程 + 端口 + pytest 缓存 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 10:03 | **合并收口 fa59b8fa** | **新提交 1 个，远端已同步** | `f59ffed` 购买点击前先悬停（按钮要光标停稳才认按下）；HEAD `9f4799c→f59ffed`；`origin/main` = `f59ffed`（09:40/09:51/10:03 三个 nav 修复全部在远端） |
| 10:11–10:20 | 在线 nav 修复线（清单外） | 进行中，**未提交** | 工作树 12 项（M8 + ??4），`+403/−22`（上轮 9 项 `+284/−11`）；`core/nav.py` 10:20、`tests/test_nav.py` 10:20、`core/nav_col.py` 10:11、`perf/bench/poll_bench.py` 10:12；engine.py「ESC 只在 OCR 真看到已被购买才发」+ run.py 单实例闸仍未提交 |
| 10:23:06→10:31+ | 全量回归（在跑） | 进行中，无结果 | `python -m pytest tests -q` PID 47208，10:23:06 起；10:31:35 时 CPU 456.9s / WS 338.7MB；`%TEMP%\sjz-pytest-isolation` 10:31:53 仍有写入、desktop.log 10:31:45 仍在释放测试临时目录 → 在推进非卡死；`lastfailed` 仍 6 条陈旧节点（9:49:10，6 个 `def` grep 全 0 命中） |
| 10:05–10:12 | 离线验证5 935cc15a | 有代码，**仍无数字** | 新增未跟踪探针 `perf/bench/fastdet_probe.py`(4.4K) / `ocr_path_probe.py`(4.7K) / `ocr_scaling_probe.py`(2.6K)；`perf/out` 最新仍 `chain_ab.json` 07:43 → 第三轮仍无新 JSON |
| 10:23:28–10:25:26 | 测试证据 | 无新结论 | `data/<hash>/buy.png+confirm.png` 新增 8 组，尺寸与既有 fixture 完全一致（16938/17216 B），未见新判据字段 |
| 10:35 | 运行态/窗口 | 合规 | 可见窗口 **13** 全为用户程序，dafors 自有 **0**；监听 :8080(PID 23112)、:8081(PID 45872 无窗口)；8799/8801 无监听；游戏 PID 34848（04:41 起） |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4) — 最新非 watch 报告仍 11-08 00:13。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/2/3/4 — 均 >18h 零产物。
真机提醒: **未触发**（离线五路仍只有验证5 交付且无新数字；矩阵无「必须开游戏」清单；工作树 12 项未提交）。游戏 PID 34848 已在跑，勿再开第二份客户端。
异常建议: ①pytest 单进程已跑 8min+、CPU 456s，若 >15min 未收尾建议 py-spy 抓栈定位长挂用例，别让它独占 4 线程拖慢其他线；②12 项未提交（含 10:20 nav.py 新改动）须回归通过后按功能域分组提交；③`temp_20261108_12` 已删、`docs/season_filter_validation.md` 仍 08:15 → 枚举结论无法复核，需按残留 9 组截图补写；④五线 + 矩阵自昨夜零产物，建议重派或移出清单。

## 2026-11-08 11:20 第三十七次快照（对比 10:35，仅记变化）

> 工具说明: 本环境仍无 list_sessions（第十五次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程 + 端口 + pytest 缓存 + %TEMP% 产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 10:34–10:48 | **合并收口 fa59b8fa** | **新提交 4 个，全部已推** | `cd23907`/`fcfced7` go_to_onsale 复用 enter_market 入口判定 + buy 落点复用；`57423ec` bench 阈值写真实耗时构成；`489470a` 工程约定（省判定先写反证用例）；HEAD `f59ffed→489470a`，`origin/main=489470a`，未推送 **0** |
| 10:59:22 | 全量回归（10:23 起） | **已收尾，无真失败** | `nodeids`/`lastfailed` 均 10:59:22 写入；`lastfailed` 仍 6 条且 6 个 `def` grep **全 0 命中** → 无新增失败；单进程 36min 自然结束，非卡死 |
| 10:56 / 10:58 | 在线 nav 修复线（清单外） | 仍在改，未提交 | `core/nav.py` 10:56、`tests/test_nav.py` 10:58、`tests/test_nav_filter.py`（+93）；工作树 12→**10** 项（M6 + ??4），`+545/−15`；`core/engine.py`(ESC 仅 OCR 真看到已被购买才发) + `run.py`(单实例闸) 仍未提交 |
| 11:02–11:17 | 真机抓帧线（清单外，**动真实光标**） | 7 轮中仅 1 轮成功 | 11:06:16–11:06:21 抓到 `detail_0..4.png` 共 5 帧（成功进 detail 页）；11:07/11:11/11:15/11:17 四轮只剩 `miss_bar.png`/`miss_lock.png`，`frames.json=[]` → 连续未进 detail；脚本 `%TEMP%\detail_capture{,2..7}.py` 每 2–4min 新建一个 |
| 11:01→ | 离线验证5 935cc15a | 仍无数字 | `perf/out` 最新仍 `chain_ab.json` 07:43（>3.5h 无新 JSON）；新增 3 个未跟踪探针（fastdet/ocr_path/ocr_scaling）仍未产出；`ocr_service_8081.log` 10:59:51 仍在写（服务活着） |
| 11:20 | 运行态/窗口 | 合规 | 可见窗口 **14** 全为用户程序，dafors 自有 **0**；监听 :8080(PID 23112)、:8081(PID 45872 3.6GB)；8799/8801 无；游戏 PID 34848（04:41 起）；python 进程里 dafors 相关仅 `%TEMP%\detail_capture7.py`（PID 45368） |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/2/3/4 — 均 **>19h** 零产物。
真机提醒: **未触发**（离线五路仍只有验证5 且无数字；矩阵无「必须开游戏」清单；工作树 10 项未提交）。
异常建议: ①回归已通过且 10:34/10:48 四提交已推，剩下 `engine.py`/`run.py`/`test_nav*` 应按功能域分组提交，别再攒；②`temp_20261108_12` 的教训：`detail_frames\detail_0..4.png`（11:06 唯一成功真机证据）**勿删**，先落盘结论再清；③真机抓帧 4 轮连续 miss 且每轮都 `SetCursorPos`+点击真实游戏窗口，建议用 11:06 那 5 帧定型 detail 判定，停止盲点；④五线 + 矩阵 >19h 零产物，重派或移出清单。

## 2026-11-08 11:38 第三十八次快照（对比 11:20，仅记变化）

> 工具说明: 本环境仍无 list_sessions（第十六次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程 + 端口 + pytest 缓存 + %TEMP% 产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 11:32–11:35 | **合并收口 fa59b8fa** | **新提交 2 个，已推** | `86fa3e0` 判页改两段式（market/onsale 局部 ROI 快判）；`25713cd` 工程约定「页型开快路径先攒够真实帧」；HEAD `489470a→25713cd`，origin 同步，未推送 **0**；工作树 10→**8** 项（nav.py/test_nav.py 已入库） |
| 11:21:51–11:30:06 | 全量回归（新跑） | **已收尾，无新失败** | desktop.log 11:21:51 起、11:29:16 收尾；`nodeids` 2518 条 11:30:06 重写；`lastfailed` 仍 10:59:22 版、6 条均为陈旧节点（6 个 `def` grep 0 命中）→ 本次 +0 失败 |
| 11:31:11 | 离线验证5 935cc15a | 真机 3 链跑完，**仍无数字** | `%TEMP%\live_e2e_fast.py 3`（PID 41628/46368）11:31:11 起、<1min 退出；`perf/out` 最新仍 `chain_ab.json` 07:43（>4h 无新 JSON）；3 个未跟踪探针（fastdet/ocr_path/ocr_scaling）仍无产物 |
| 11:26:34→11:32:43 | 真机抓帧线（清单外，动真实光标） | **第 10 轮仍 miss** | `detail_capture9.py` 11:26 新建；`detail_frames\frames.json` 11:32:43 只剩 `miss_tip.png` page=onsale → 连续第 8 轮未进 detail；可复核证据仍仅 11:06 那 5 帧 |
| 11:29:20 | 配置/测试产物 | 无异常 | `config.json`+`config.json.good` 11:29:20 同步落盘；`data/` 11:22–11:24 新增 9 组 buy/confirm，尺寸与既有 fixture 一致（16938/17216 B） |
| 11:38 | 运行态/窗口 | 合规 | 可见窗口 **14** 全为用户程序，dafors 自有 **0**；监听 :8080(PID 23112)、:8081(PID 45872)；8799/8801 无；游戏 PID 34848（04:41 起），未开第二份客户端 |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/2/3/4 — 均 **>20h** 零产物。
真机提醒: **未触发**（离线五路仍只有验证5 且无数字；矩阵无「必须开游戏」清单；工作树 8 项未提交）。
异常建议: ①`core/engine.py`(ESC 仅 OCR 真看到已被购买才发)/`run.py`(单实例闸)/`tests/test_nav_filter.py` 仍未提交，回归已过可直接分组提交；②真机抓帧连续 8 轮 miss 且每轮都 `SetCursorPos`+真实点击，建议停手，用 11:06 五帧定型 detail 判定；③五线 + 矩阵 >20h 零产物，建议重派或移出清单。

## 2026-11-08 12:05 第三十九次快照（对比 11:38，仅记变化）

> 工具说明: 本环境仍无 list_sessions（本轮直接查 `tool_names:["list_sessions","sessions_list","get_session","session_status"]` 返回 0，第十七次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + 报告产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 11:44:20 / 11:45:52 / 12:02:42 | **在线 nav 修复线（清单外）** | 仍在改，未提交 | `core\nav_col.py` 78,989B(11:44)、`tests\test_nav.py` 38,836B(11:45)、`core\nav.py` 67,064B(**12:02:42** 刚写)；工作树 8→**11** 项 |
| 11:46:00–11:46:48 | 定向回归 | **已收尾，+0 失败** | `pytest-1707` 11:46:48；`nodeids` 11:46:48；`lastfailed` 仍 10:59 版 6 条（6 个 `def` grep **0 命中**，改名陈旧节点） |
| 11:29:16 后 | dafors 应用 | 未运行 | 8799/8801 均无监听；`desktop.log` 止于 11:29:16（UIPI「游戏窗口权限更高 0x2000」警告收尾） |
| 11:24:16 后 | 真机采集 | 静止 | `data` 仍 59 组，最新 `113aa292` 11:24:16（buy 16,938B / confirm 17,216B），无新增 |
| 06:33–06:34 | 打包产物 | **仍落后** | onefile 108,066,369B / installer 83,072,672B；其后源码已 +14 提交，交付版不含 11:33 后修复 |
| 12:02 | 运行态/窗口 | 合规 | 可见窗口 14 全为用户程序，dafors 自有 **0**；监听 :8080(PID 23112)、:8081(OCR PID 45872，`ocr_service_8081.log` 12:01:17 / 1.96MB)；游戏 PID 34848（04:41 起）仍在 |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13:40；`perf/out` 最新仍 `chain_ab.json` 07:43。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/2/3/4 — 均 **>21h** 零产物。
真机提醒: **未触发**（离线五路仍只有验证5；矩阵无「必须开游戏」清单；工作树 11 项未提交）。
异常建议: ①nav 三文件在 18 分钟内连续改写且未提交，先等文件静止、跑定向回归再分组提交；②游戏 PID 34848 已连续 7.4h、CPU 72,347s 空转，若不继续真机建议退出释放；③五线 + 矩阵 >21h 零产物，建议重派或移出清单。

## 2026-11-08 12:33 第四十次快照（对比 12:05，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names:["list_sessions","sessions_list","get_session","session_status"]` 均返回 0，第十八次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 12:22:37–12:29:23 | **nav 修复线（清单外）** | 仍在改，回归**未见新失败** | `core\nav.py` 12:28:18(69,653B)、`tests\test_nav.py` 12:28:26(39,877B)；pytest-1714/1715/1716 连跑，`nodeids` 12:29:23 重写（125 个测试文件在库）；`lastfailed` 仍是那 6 条陈旧节点（5 个文件在 `tests\`，但同名 def 已改名、live `grep` 0 命中；同名 def 只存在于 `perf/base` 等 .gitignore 快照副本）→ 本次新增失败 **0**；工作树仍 11 项 |
| 12:27:48 / 12:29:33 | **离线验证5 935cc15a** | **首次产出真机数字（关键进展）** | 两轮 `%TEMP%\live_stability.py`（第 2 轮 PID 43924 `5 15`，12:29:29 起、已退出）：A 轮 复位 **4/5**、buy 10 样本（1下2/2下3/3下2/失败3）、e2e p50 **14,159ms**、链 4/5；B 轮 复位 **5/5**、buy 15 样本（1下6/2下5/3下0/**失败4**）、e2e p50 **12,918ms** [15,492/12,918/10,661/18,826/10,739]、链 **3/5**、where p50 **346ms**；两轮 unknown **0**、fallback 0 |
| 同上 | 离线验证5 同期 | **新缺陷（重要）** | 购买点击被吞 **4/15 = 26.7%**：同点连点 3 次画面变化 0.00–0.01%，报「坐标 (1715,876) 可能过期或游戏不在前台」；皮肤卡片点击 15/15 亦 0.0% 无响应（按「已是选中态」跳过）；A 轮复位有 1 次 title→**ERR** |
| 11:32:43 后 | 真机抓帧线（清单外，动真实光标） | **已停手（符合前轮建议）** | 无 `detail_capture*` 进程；`detail_frames` 最新仍 `miss_tip.png` 11:32:43；11:06 那 5 帧完好保留 |
| 12:33:54 | 运行态/窗口 | 合规 | 可见窗口 **14** 全为用户程序，dafors 自有 **0**；监听 :8080(PID 23112)、:8081(OCR PID 45872，日志 12:33:54 / 2.0MB)；8799/8801 无；游戏 PID 34848（04:41 起，CPU 78,122s ≈7.9h） |
| 12:33 | git | 无新提交 | HEAD 仍 `25713cd`，与 origin 同步，未提交条目 11（`_watch.md`/engine/nav/nav_col/run/test_nav/test_nav_filter + 4 未跟踪） |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、697db934(离线2)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13:40；`perf\out` 最新仍 `chain_ab.json` 07:43。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/2/3/4 — 均 **>22h** 零产物。
真机提醒: **仍未正式触发**（离线五路仅验证5 有数、未全交付；矩阵仍无「必须开游戏」清单；工作树 11 项未提交）。但游戏已在跑、验证5 已在真机连续出数，实质真机条件已具备。
异常建议: ①购买点击 26.7% 被吞是本轮最值得优先定位的缺陷，建议用 `live_stability` 的 15 样本 + `%TEMP%` 日志固化复现，先查 UIPI/前台切换与 hold 时序，别再盲调坐标；②nav 三文件已静止 5 分钟且定向回归 0 新失败，可分组提交；③缺口2/01b/矩阵/离线1-4 >22h 零产物，重派或移出清单；④游戏 PID 34848 CPU 78,122s 空转 7.9h，若不继续真机建议退出释放。

## 2026-11-08 13:02 第四十一次快照（对比 12:33，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names:["list_sessions","sessions_list","get_session","session_status"]` 均返回 0，第十九次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 12:55:28 | **离线验证2 697db934（输入链路）** | **恢复活跃·首次真机出数** | `%TEMP%\probe_send_layer.py`(8,934B, 12:58:33) phase1 20 次单击（hold=0.35）：**成功 10/20 = 50%**；10 次失败全部 move/down/up/restore=1/1、down→up 350.15–350.53ms、fg='三角洲行动'、iconic=False、vis=True、layered=False、rect(-8,-31,1928,1088)=1920×1080；diff 0.00–0.01%、page 停留 market；landing 回读 None/None → 排除 UIPI/最小化/前台丢失，指向落点与 hold 时序 |
| 12:58:38→13:02 | 同上·phase_gap 在跑 | 进行中（无产物） | PID 46136 `probe_send_layer.py g 20 0,16,33`（move→down 间隔 0/16/33ms ×20）；CPU 948.7s / WS 283MB；`%TEMP%` 仅 `probe_send_layer_p1.txt` 一个结果文件 |
| 12:49:26–12:58:00 | 代码改动 | 未提交 11 项（内容变化） | 新增 M `core\input.py`(135,712B 12:57:47)、M `core\interception.py`(65,805B 12:49:26)、M `core\nav.py`(69,816B 12:58:00)；原 M engine/nav_col/run/test_nav_filter + ??4 未动 |
| 12:47 | **合并收口 fa59b8fa** | 新提交 1 个，已推 | `df6f840` nav: 复位先归位大厅子状态（ESC 菜单/部门视图），unknown 归零；HEAD `25713cd→df6f840`，origin/main 同步，未推送 0 |
| 12:39:39–12:41:41 | 真机采集 | 新增 8 组 | `data\9443f0b9…ffd9d4e7` 共 8 组 buy/confirm（16,938/17,216B）；今日 07:00 后累计 32 组 |
| 12:46:46 | 配置 | 同步落盘 | `config.json` = `config.json.good` 13,195B（同一时刻写入） |
| 12:29 后 | 回归缓存 | 无新失败 | `lastfailed` 6 条；7 个 `def`（含 `test_external_thread_observes_each_firing`）grep 全 0 命中 → 缓存陈旧，真失败 0 |
| 12:33 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（>5h 无新 JSON）；`docs\perf_ocr_path.md` 08:53 已定稿 |
| 13:02 | 运行态/窗口 | 合规 | 可见 python 窗口仅用户 `pythonw` 19400「Token 用量看板」，dafors 自有 **0**；:8081 OCR PID 45872 无窗口（日志 12:55）；8799/8801 无监听；游戏 PID 34848（04:41 起）仍在 |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b) — 最新非 watch 报告仍 11-08 00:13:40。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/3/4 — 均 >23h 零产物。
真机提醒: **未触发**（离线五路仍未全交付；矩阵无「必须开游戏」清单；工作树 11 项未提交）。游戏 PID 34848 已在跑，勿再开第二份客户端。
异常建议: ①点击命中 50% 是本轮核心缺陷，已排除 UIPI/前台丢失，等 `phase_gap`（0/16/33ms）出数再定 hold，勿盲调坐标；②`core\input.py`/`interception.py` 是离线2 复活的写入口，跑完再分组提交；③缺口2/01b/矩阵/离线1-3-4 >23h 零产物，建议重派或移出清单。

## 2026-11-08 13:35 第四十二次快照（对比 13:02，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch `tool_names:["list_sessions","sessions_list","get_session","session_status"]` 均返回 0，第二十次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 13:02–13:19:30 | **离线验证2 697db934（gap 曲线前段）** | 出数，**gap 假设被否** | 60 样本：0ms **9/20=45%**、16ms 7/20=35%、33ms 10/20=50%；实测间隔 p50 16.33/33.3 与设定一致（`probe_gap_133.txt` 13:19:30） |
| 13:28:30 | 同上·长间隔段 | 出数，结论明确 | 50ms 7/20=35%、80ms 3/20=**15%**（最长连挂 14）→ 间隔越长越差，move→down 时序不是主因（`probe_gap_5080.txt`） |
| 13:30:10–13:31:25 | 同上·probe_cls 命中探查 | **新证据（缺陷定性收窄）** | 同坐标 (1715,876)、同面板文本（购买 y=872 x=1659），6 次仅 **2 次成功=33%**：成功 diff 56.72%/57.45% → page=onsale；失败 diff 0.00–0.01% 仍 market |
| 13:31 | probe 进程 | 已退出 | 无 `probe_*` python 进程残留；`%TEMP%` 新增 `probe_gap_133.txt`/`probe_gap_5080.txt`/`probe_cls.py`+`probe_cls.txt` |
| 13:31 | git | 无新提交 | HEAD 仍 `df6f840`、origin 同步；工作树仍 **11** 项（7 M + 4 ??），组成变了：`nav_col`/`test_nav` 已随 df6f840 提交，新增 M `core\input.py`/`core\interception.py` |
| 12:51:02 | dafors 应用 | 起过又停 | `desktop.log` 新增「12:51:02 线程预算 4」，无 win_close 收尾；8799/8801 均无监听 |
| 13:31 | 运行态/窗口 | 合规 | dafors 自有可见窗口 **0**；:8080 PID 23112、:8081 OCR PID 45872（日志 13:28:30 / 2.09MB）；游戏 PID 34848 仍在（04:41 起） |
| 12:41 后 | 真机采集 | 静止 | `data` 67 组，最新 `ffd9d4e7` 12:41:41，无新增 |
| 12:33 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（>5.8h 无新 JSON） |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口，df6f840 后无新提交) — 最新非 watch 报告仍 11-08 00:13:40。

挂起项: 缺口2 两决策、01b 退回、真机矩阵 + 离线1/3/4 — 均 **>24h** 零产物。
真机提醒: **未触发**（离线五路仍只有验证2/5 出数；矩阵无「必须开游戏」清单；工作树 11 项未提交）。
异常建议: ①坐标与 gap 已双双排除（同坐标同文本 2/6 成功），缺陷收窄为「命中/悬停态」——下一轮别改 hold 与坐标，改采 mousedown 前后各 1 帧看 hover 高亮/光标形态；②`core\input.py`/`interception.py` 为离线2 写入口，probe 已退出、工作树稳定，可先分组提交；③缺口2/01b/矩阵/离线1-3-4 >24h 零产物，建议重派或移出清单；④游戏 PID 34848 空转 >8.8h，不继续真机建议退出释放。

## 2026-11-08 14:05 第四十三次快照（对比 13:35，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词均 0 命中，第二十一次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + %TEMP% 产物 + 可见窗口枚举 + 截图人工复核。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 13:43:48–13:54:56 | **离线验证2 697db934** | 换阶段：点击命中 → 支付/弹窗 | 探针序列 `diag_filter`→`diag_skins(2)`→`diag_row`(+`rowclick.png` 1.29MB)→`probe_pay`→`card_popup.png`(2.56MB)+`card_0..5` 逐卡截图 |
| 13:57:33–14:01:40 | 同上·pay 采样 | 未得结论 | `probe_pay2.py`→`probe_pay3.py`；13:59:58 起 4 轮×30s 累计 **公示期样本 0/30**；探针打印「余额=8」 |
| 14:02:33–14:04:26 | 同上·余额读数 | **探针缺陷**：读数与画面不符 | `bal_raw`/`bal_x6`/`top_raw` 后追加 `diag_bal_roi.py` + `bal_170/bal_400.png`；我复核顶栏实为 **2,049 / 0 / 8**（主币 2,049），探针取的 8 系误读 → 非余额耗尽，勿据此充值 |
| 14:04:44 | 同上·进程 | 仍在跑 | 最后产物 14:04:26；Python 进程 6 个（含 :8080/:8081 两个常驻） |
| 13:35 后 | git | 无变化 | HEAD `df6f840`；工作树仍 11 项（7 M + 4 ??），mtime 最新 `_watch.md` 13:33 / `core\nav.py` 12:58 |
| 13:35 后 | 其它 9 会话 | 零新报告 | 319accdc/da85f6a6/eea45452/fa2c17bf/44755992/b5a50cc0/25a3c42/ea4e1e45/fa59b8fa；最新非 watch 报告仍 00:13:40 |
| 13:35 后 | 离线5 935cc15a | 无新数字 | `perf\out` 最新 `chain_ab.json` 07:43（>6.4h） |
| 12:41 后 | 真机采集 | 静止 | `data` 67 组，最新 `ffd9d4e7` 12:41:41 |
| 14:03 | 运行态/窗口 | 合规 | dafors 自有可见窗口 **0**；:8080 PID 23112、:8081 OCR PID 45872（日志 2.10MB）；游戏 PID 34848（04:41 起，>9.3h） |

真机提醒: **未触发**（离线五路仍只有验证2 在跑、验证5 无新数字；矩阵无「必须开游戏」清单；工作树 11 项未提交）。
挂起: 缺口2 两决策、01b 退回、真机矩阵、离线1/3/4 — 仍 **>25h** 零产物，建议重派或移出清单；游戏 PID 34848 空转 >9.3h，不做真机建议退出释放。

## 2026-11-08 14:32 第四十四次快照（对比 14:05，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词均 0 命中，第二十二次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针产物 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 14:11:40–14:16:09 | **离线验证2 697db934（购买链路）** | **首次打通到支付弹窗** | `%TEMP%\buy_run.log`：判页局部 ROI 28.6%→onsale；余额**2049**、购买点 (1631,932)、挂单 6 条全 public；idx1 第 **6** 次命中后点击、click→popup **7.409s**、diff **39.68%**，弹窗「三角币不足…**缺少2三角币**」，单价 10 → 三角币实为 **8**；成交 **0/3** |
| 同上 | **上轮结论纠错（重要）** | **13:35/14:05 的「8 系误读」判错** | 弹窗铁证反推余额=8；`2,049 / 0+ / 8+` 中 2,049 是左侧纸币、**8 是最右三角币**。新增 `core\ocr.py::balance_from_words`（只取带 `+` 的最右组，宁可 None 不误读，+44 行） |
| 14:16:09 | 同上·新缺陷 2 个 | 记录待修 | ① 6 行挂单里 **4 行价签读不出**（item/price 均 None → 直接跳过）；② 公示期 T-3s 判定有 **-0.43s 竞态**（`left_s=1` 仍判未结束并跳过） |
| 14:08:45–14:31:54 | 同上·探针节奏 | 高频迭代，已退场 | `diag_balance_fix`→`probe_obs`→`probe_buy`→`diag_currency`→`diag_popup_close`→`diag_evidence`→`probe_popup10`→`probe_scan3`→`probe_chain1`→`diag_mandel` 共 10 个，最新 14:31:54；当前无 probe python 进程残留 |
| 14:17:34–14:21:35 | 工作树 | **11→15 项，仍未提交** | 新增 M `core\ocr.py`(14:17:49)、M `core\nav_coords.py`(14:17:53)、?? `tests\test_engine_balance.py`(14:17:34)、?? `tests\test_engine_popup_close.py`(14:21:35)；`core\engine.py` 14:21:00（累计 +132/−48） |
| 14:28:40 | 回归缓存 | **+0 真失败** | `nodeids` 14:28:40 重写；`lastfailed` 14:21:44 仍 6 条，6 个同名 def 在 `tests\` grep **0 命中** → 均为陈旧节点 |
| 14:32 | git | 无新提交 | HEAD 仍 `df6f840`（12:47:55），origin 同步，未推送 **0**；工作树 15（9 M + 6 ??） |
| 14:32 | 运行态/窗口 | 合规 | 可见窗口 **14** 全为用户程序，dafors 自有 **0**；:8080(PID 23112)、:8081(OCR PID 45872，日志 14:32:09 / 2.01MB)；8799/8801 无；游戏 PID 34848（04:41 起，CPU 94,011s≈26.1h 折算、5.02GB） |
| 12:51:02 后 | dafors 应用 | 未运行 | `desktop.log` 仍止于 12:51:02；8799/8801 无监听 |
| 12:41:41 后 | 真机采集 | 静止 | `data` 仍 67 组，最新 `ffd9d4e7` 12:41:41 |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（>6.8h 无新 JSON） |

无变化（零新报告产物）: 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) — 最新非 watch 报告仍 11-08 00:13:40。

挂起项: 缺口2 两决策、01b 退回、真机矩阵、离线1/3/4 — 均 **>26h** 零产物。
真机提醒: **未触发**（矩阵仍无「必须开游戏」清单；离线五路仅验证2 活跃、验证5 无新数字）。注意游戏本就已在跑（PID 34848，04:41 起），无需再开第二份客户端。
需要用户输入: **三角币余额 8 < 最低单价 10**，购买链路真机验收被余额卡住 → 需决定：充值 / 换低价商品继续验 / 或以「三角币不足」为当前终点。未决前建议离线2 转向「价签读不出」与公示期竞态两个可离线复现的缺陷。
异常建议: ①`balance_from_words` + 两个新测试 + engine 改动已成形但未提交，建议先跑定向回归再分组提交；②缺口2/01b/矩阵/离线1-3-4 >26h 零产物，重派或移出清单；③游戏 PID 34848 空转 9.9h、CPU 94,011s，若不继续真机建议退出释放。

## 2026-11-08 15:03 第四十五次快照（对比 14:32，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词均 0 命中，第二十三次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针产物 + 真实窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 14:35:33–14:45:52 | **离线验证2 697db934** | **换线：新开「曼德尔砖指定价格购买」** | 新增 `core\mandel_buy.py`(8,218B, 14:44:15)+`tests\test_nav_mandel_buy.py`(6,494B, 14:44:28)；`core\nav.py` 14:35:33(72,125B, +53)、`core\market.py` 14:36:35(44,954B, +24)；`%TEMP%` 探针 probe_panorama(14:39)/probe_final(14:44)/probe_col(14:45)；模块口径写死：底部 y≈739 一排=价格档位，成交均价非可买价 |
| 14:47:07–14:49:25 | 真机采集 | **+8 组（75 组）** | 8 组 buy/confirm（16,938 / 17,216 B 与既有同尺寸）；最新 `296b617e` 14:49:25 |
| 14:56:03 | 配置 | 同步落盘 | `config.json`=`config.json.good` 13,195B 同刻写入 |
| 14:55:27–15:02:2x | 定向回归（引擎两文件） | **+0 真失败** | `desktop.log` 14:55:27「退出时输入可能没释放干净」（测试告警）、14:57/14:59「线程预算 4」；`lastfailed` 15:02:14 仍 6 条陈旧节点（同名 def 在 `tests\` live grep **0 命中**、5 个文件均在库）→ 本次新增失败 **0**；`nodeids` 15:02:29 重写 |
| 14:32→15:03 | git | **2h15m 无提交** | HEAD 仍 `df6f840`（12:47:55），origin 同步；工作树 **15→19**（12 M + 7 ??），累计 **+803/−65**（engine +132、test_nav_filter +93、input +70、nav +53、test_engine_no_dialog +47、ocr +44、run +30、test_engine_insufficient +29、market +24、nav_coords +5、interception +1） |
| 14:59:12 / 15:00:48 | 测试文件 | 改动未提交 | `test_engine_no_dialog.py`(13,211B)、`test_engine_insufficient.py`(8,133B)；未跟踪另有 `test_engine_balance.py`/`test_engine_popup_close.py`/`test_nav_mandel_buy.py` |
| 15:03 | 运行态/窗口 | 合规 | 可见窗口 **13** 全为用户程序，dafors 自有 **0**；:8080(PID 23112)、:8081(OCR PID 45872，日志 15:01:11 / 2.11MB)；8799/8801 无；游戏 PID 34848（04:41 起，CPU 98,683s / 5.40GB） |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（>7.3h 无新 JSON） |
| 00:13:40 后 | 其余 9 会话 | 零新报告 | 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) |

挂起项: 缺口2 两决策、01b 退回、真机矩阵、离线1/3/4 — 均 **>27h** 零产物。
真机提醒: **未触发**（离线五路仍未全交付；矩阵无「必须开游戏」清单）。游戏 PID 34848 本就在跑（04:41 起），勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍成立（14:32 已提、未决）；离线2 已转向曼德尔砖「指定价格购买」，该路径同样按三角币结算 → 需决定：充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①**19 项未提交且已 2h15m**（含 +803 行、4 个新测试、曼德尔新模块），按功能域分组提交（input/interception → 离线2 写入口；ocr balance → 余额口径；mandel_buy+nav → 新购买线；engine+两测试 → 弹窗/不足）；②离线5 >7.3h 无 JSON、缺口2/01b/矩阵/离线1-3-4 >27h 零产物，重派或移出清单；③游戏 PID 34848 CPU 98,683s 空转，若不继续真机建议退出释放。


## 2026-11-08 15:33 第四十六次快照（对比 15:03，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词均 0 命中，第二十四次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针产物 + 真实窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 15:11:03–15:20:10 | **离线验证2 697db934** | **实质进展：新增品阶枚举线** | 探针 `probe_watermark`(15:11:03)、`probe_tier`(15:15:30)；新增 `core\tier_enum.py`(10,833B, 15:19:10) + `tests\test_tier_enum.py`(8,003B, 15:19:28, **15 个用例**)；15:20:10 定向跑，`lastfailed` 仍 6 条陈旧节点（同名 def 在 `tests\` live grep 0 命中）→ 新增失败 **0**；`nodeids` 15:20:10 重写 |
| 15:24 记录 | 同上·**新缺陷 1 个** | 赛季回读前缀误判 | 目标「棱镜攻势」、下拉实际「棱镜攻势S2」，`nav._season_match` 判 True → 整季枚举拿的是 S2 列表；已在 `tier_enum.season_readback_ok` 加硬约束「读到的串不得比目标长」 |
| 15:19 | 新模块接线 | **未接入主链路** | `tier_enum` / `grade_field` / `season_readback_ok` 全仓仅在自身+测试出现，core/run 0 引用 |
| 14:49:25 后 | 真机采集 | 静止 | `data` 仍 **75 组**，最新 `296b617e` 14:49:25 |
| 12:47:55→15:33 | git | **2h45m 无提交** | HEAD 仍 `df6f840`，origin 同步，未推送 0；工作树 19→**22**（12 M + 10 ??），累计 **+824/−65** |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（>7.8h 无新 JSON） |
| 00:13:40 后 | 其余 9 会话 | 零新报告（≈15h） | 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口) |
| 15:33 | 运行态/窗口 | 合规 | 可见窗口 **13** 全为用户程序，dafors 自有 **0**；:8080(PID 23112)、:8081(OCR PID 45872，日志 15:31:49 / 2.12MB)；8799/8801 无；游戏 PID 34848（04:41 起，CPU 102,445s / 5.4GB） |

挂起项: 缺口2 两决策、01b 退回、真机矩阵、离线1/3/4 — 均 >15h 零产物。
真机提醒: **未触发**（离线五路仍未全交付；矩阵无「必须开游戏」清单）。游戏 PID 34848 本就在跑（04:41 起），勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍成立（14:32 首提、未决）；离线2 已转「曼德尔砖指定价格购买」+「品阶枚举」，两条线都按三角币结算 → 需决定：充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①**22 项未提交且已 2h45m**（+824 行、6 个新测试、mandel_buy 与 tier_enum 两个新模块），按功能域分组提交（input/interception → 写入口；ocr balance → 余额口径；mandel_buy+nav / tier_enum → 新购买与枚举线；engine+测试 → 弹窗/不足/无弹窗）；②`tier_enum` 15 用例已绿但主链路 0 引用，建议下一步接线并补契约测试；③离线5 >7.8h 无 JSON、缺口2/01b/矩阵/离线1-3-4 >15h 零产物，重派或移出清单；④游戏 PID 34848 CPU 102,445s 空转，若不继续真机建议退出释放。


## 2026-11-08 16:04 第四十七次快照（对比 15:33，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词均 0 命中，第二十五次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针产物 + 真实窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 15:49–15:54 | **缺口2 da85f6a6 发布链路** | **实质产物落地** | sjz.spec +1 行（随 f62a045 入库）；dist 15:50:00；**SJZ-Control-Setup-1.1.0.exe 15:53:36**（dist-installer）；**SJZ-Control.exe 15:54:17**（dist-onefile）；nodeids 15:54:46 重写（构建后跑测，lastfailed 未变→仍 6 条陈旧节点，新增失败 0） |
| 16:00:21–16:00:56 | **git 收口** | **22 项未提交 → 6 个功能域提交，异常解除** | 9512be9 nav 页签→界面映射+mandel 接线下单层；f62a045 tier_enum 品阶继承+回读作废(+500 含测试 227 行)；b19f3f9 engine 余额按词框解析+弹窗回读+开火清弹窗；413ff93 input move→down 间隔可插+投递明细；cea2ec8 run 启动前探端口防双实例抢鼠标；c3517a2 test/docs 筛选标定+判页实验(+918)；工作树 22→**1**（`M core/tier_enum.py` +8/−1）；**main==origin/main（已推送到远端）** |
| 15:33:43–15:35:50 | 真机采集（离线2 697db934） | +3 组 | data 目录现有 **83 个子目录**（14:49 时口径 75 组），最新 `022e7296` 15:35:50 |
| 15:34:00 / 15:41–15:42 | 桌面应用 | 关闭→重启→又跑测 | 15:34:00 win_close 用户关闭收尾；15:41:44 pytest-1743 种子资源释放（测试在跑）；15:42:14 UIPI 告警（应用再起）；当前桌面可见窗口 **13** 全为用户程序，dafors 自有 **0** |
| 16:02:20 | 离线2 697db934 | 进行中 | 新探针 `probe_tier_all.py` 落 %TEMP%（全档枚举）；tier_enum 提交后又有 +8/−1 未提交 |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（>8.3h 无新 JSON） |
| 00:13:40 后 | 其余 9 会话 | 零新报告（>16h） | 319accdc(缺口1，完成 21:01)、eea45452(缺口4)、fa2c17bf(真机矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(合并收口)、da85f6a6 前段 |

挂起项: 缺口4 决策、01b 退回、真机矩阵、离线1/3/4 — 均 >16h 零产物；缺口2 的两决策未再见新记录（但发布产物已落地）。
真机提醒: **未触发**（离线1/3/4/5 仍未交付；矩阵无「必须开游戏」清单）。游戏 PID 34848 在跑（04:41 起，CPU 107,325s / 5.2GB），勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍成立（14:32 首提、未决）；曼德尔砖/品阶枚举两线都按三角币结算 → 需决定：充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①6 个提交已同步到 origin/main，但 push 按既定规则需单独指令 — 请确认是否已授权，若未经授权需追认或回退远端；②tier_enum 提交后 25 分钟内又改 +8/−1 且探针在跑，收口未完成，等 probe_tier_all 结果再补一笔提交；③离线1/3/4/5 与矩阵/01b/缺口4 >16h 零产物，重派或移出清单；④:8080(PID 23112)、:8081 OCR(PID 45872) 常驻，游戏 CPU 空转 107,325s，若不继续真机建议退出释放。


## 2026-11-08 16:35 第四十八次快照（对比 16:04，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词 0 命中，第二十六次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针产物 + 真实窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 16:03:07 | **离线2 697db934** | **实质进展：+8/−1 已补提交** | `8b67fc5` tier_enum scan_list 标注「撞上限未到底」，不把截断当全部；nodeids 16:03:02 重写、`lastfailed` 仍 15:20:10 未变 → 新增失败 **0**；**HEAD==origin/main**（8b67fc5 已推送）→ 47 次异常②解除 |
| 16:03:24 起 | 离线2·probe_tier_all | **状态异常：疑似挂起** | PID **40640**（.venv python），运行 ~30min **CPU≈0s / WS 10MB**、%TEMP% 无输出文件 → 卡在启动早期或阻塞等待；建议终止后带硬超时重跑 |
| 15:35:50 后 | 真机采集 | 静止 | data 仍 **83 组**，最新 `022e7296` 15:35:50（~1h 无新组） |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（**>8.8h** 无新 JSON） |
| 00:13:40 后 | 其余 9 会话 | 零新报告（>16h） | 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2，发布产物 15:54 已落地)、eea45452(缺口4)、fa2c17bf(矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(收口) |
| 16:32 | 运行态/窗口 | 合规 | 可见窗口 **14**（较 16:04 +1，仍全为用户程序），dafors 自有可见窗口 **0**；:8080(PID 23112)、:8081 OCR(PID 45872, 3.6GB)；8799/8801 无；游戏 PID 34848（04:41 起，**CPU 111,087s**，28min +3,762s ≈ 2.2 核持续空转 / 5.2GB） |

挂起项: 缺口4 决策、01b 退回、真机矩阵、离线1/3/4 — 均 >16h 零产物；离线5 >8.8h 无新 JSON。
真机提醒: **未触发**（离线1/3/4/5 仍未交付；矩阵无「必须开游戏」清单）。游戏 PID 34848 本就在跑，勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍成立（14:32 首提、未决）→ 需决定：充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①**PID 40640 挂起探针**（CPU 0、30min 无输出）建议终止，probe_tier_all 重跑加硬超时与退出清理；②离线1/3/4/5、矩阵、01b、缺口4 长时间零产物，重派或移出清单；③游戏空转 ~2.2 核，若不继续真机建议退出释放；④工作树仅剩本 watch 日志未提交（本快照随巡检提交）。


## 2026-11-08 17:03 第四十九次快照（对比 16:35，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词 0 命中，第二十七次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针产物 + 真实窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 16:44–17:02 | **离线2 697db934** | **实质进展：目标库重建 + 物理鼠标旁路监听** | 新探针 3 个：probe_watermark2(16:44:38)、probe_tier_retry(16:47:01)、**probe_cursor_ab(16:52:18)**；新增未提交 **+237 行**：`core\bricks.py`(+28 load_skin_lib，新库 232 条=44 池枚举、与老表 243 条并存)、`core\interception.py`(+187 **PhysicalMouseWatch** 物理鼠标 Raw Input 旁路监听，不拦截不阻塞)、`core\server.py`(+22)、新文件 `core\skins_lib.json`(75.5KB, 17:02:29——巡检前 30 秒仍在写，工作进行中) |
| 16:35→17:02 | 离线2·PID 40640 | **异常解除** | 挂起探针已退出（进程表无 40640）；当前无任何 probe 进程在跑 |
| 16:34:43 | git | 本地领先 1 | 第四十八次快照已入库 `3412162`（docs(watch)）；HEAD 领先 origin/main **1**（仅 watch 日志，未推送）；工作树 0→4（3 M + 1 ??，即上表 +237 行） |
| 15:20:10 | pytest | 无新跑 | lastfailed 仍 6 条陈旧节点（15:20:10 未变）、nodeids 16:03:02 未变 → 新增失败 0 |
| 15:35:50 后 | 真机采集 | 静止 | data 仍 **83 组**，最新 `022e7296` 15:35:50（>1.5h 无新组） |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（**>9.3h** 无新 JSON） |
| 00:13:40 后 | 其余 9 会话 | 零新报告（>17h） | 319accdc(缺口1，完成 21:01)、da85f6a6(缺口2，发布产物已落地)、eea45452(缺口4)、fa2c17bf(矩阵)、44755992(离线1)、b5a50cc0(离线3)、25a3c42(离线4)、ea4e1e45(01b)、fa59b8fa(收口) |
| 17:02 | 运行态/窗口 | 合规 | 可见窗口 **13**（较 16:35 −1，全为用户程序），dafors 自有 **0**；:8080(PID 23112)、:8081 OCR(PID 45872)；8799/8801 无；游戏 PID 34848（04:41 起，**CPU 114,791s**，28min +3,704s ≈ 2.2 核持续空转 / 5.2GB） |

挂起项: 缺口4 决策、01b 退回、真机矩阵、离线1/3/4 — 均 >17h 零产物；离线5 >9.3h 无新 JSON。
真机提醒: **未触发**（离线1/3/4/5 仍未交付；矩阵无「必须开游戏」清单）。游戏 PID 34848 本就在跑，勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍成立（14:32 首提、未决）→ 需决定：充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①+237 行新改动建议离线2 收口后按功能域提交（skins_lib+bricks → 新目标库；interception → 物理鼠标旁路监听；server → 接线）；②离线1/3/4/5、矩阵、01b、缺口4 长时间零产物，重派或移出清单；③游戏空转 ~2.2 核，若不继续真机建议退出释放；④本地领先 origin 的 1 个 watch 提交待推送指令。

（补记 17:04: 巡检期间 desktop.py 又新增 +5 行（17:03:45 落盘），离线2 仍在活跃写码；快照已入库 `9712de5`。）


## 2026-11-08 17:34 第五十次快照（对比 17:03，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词 0 命中，第二十八次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针 + 真实窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 17:05:55 | **离线2 697db934**（含 skins 线） | **实质进展：目标库重建已收口** | 提交 `375e009`：`skins_lib.json` +2,868 行（**232 条枪皮 / 11 赛季 × 4 品阶**）、`bricks.py` +28、`server.py` +53/-1，共 +2,948；上一快照 +237 行未提交全部入库 |
| 17:20:36 | **离线2 697db934** | **实质进展：物理鼠标让路已交付** | 提交 `66fc800`：`input.py` +218、`interception.py` +187（PhysicalMouseWatch）、`test_feature_user_gate.py` +60；光标占用 **300ms → 0.017ms** |
| 17:05–17:08 | 未署名线（疑似离线1 前端/桌面线，归属待确认） | **新工作：迷你控制条（未提交）** | 工作树 3 M 共 **+460**：`desktop.py` +197（win_mini_enter/exit，整窗收缩 720×56，不新建第二个 WebView2）、`web/index.html` +263、`tests/test_desktop_jsapi.py` +1 |
| 17:17:32 | 回归 | 有一轮实跑，无新增失败 | `lastfailed` mtime 15:20:10 → **17:17:32** 刷新，仍同一批 **6 条既有失败**（cursor_lock / desktop_jsapi / mutation_killers×2 / nav_scroll / portability_hardening）；nodeids 17:20:22；新增失败 **0** |
| 17:31:23–29 | 上述线·实机自测 | **活跃中** | pythonw **PID 46780**（246MB）在 `127.0.0.1:8777` 监听，`desktop.log` 记「窗口已显示 + 悬浮窗 SW_SHOWNOACTIVATE（NOACTIVATE+TOOLWINDOW）已显示」；客户端 46720 已连 |
| 05:06:23 后 | 真机采集 | 静止 | `data` 最新组 `46e0738c` 05:06:23（上一快照所记 15:35:50 最新组在本机 data 已不可见，按现存目录判定 >12h 无新组） |
| 07:43 后 | 离线验证5 935cc15a | 无新数字 | `perf\out` 最新仍 `chain_ab.json` 07:43（**>10.8h** 无新 JSON） |
| 00:13:40 后 | 其余 8 会话 | 零新产物（>17h） | 319accdc、da85f6a6、eea45452、fa2c17bf、44755992、b5a50cc0、25a3c42、ea4e1e45、fa59b8fa 均无新报告/产物 |
| 17:33 | 运行态/窗口 | **合规偏差 1 项** | 可见窗口 **15**，其中 dafors 自有 **1**（PID 46780「SJZ 悬浮窗」；上一快照为 0）；:8080(23112)、:8081(45872)、:8777(46780)；游戏 PID 34848 CPU **119,089s**（31min +4,297s ≈ 2.3 核 / 5.2GB） |
| 17:34 | git | 同步 | HEAD `66fc800`，origin/main 无差异（0/0）；工作树仅迷你控制条 3 M（+460）待收口 |

挂起项: 缺口4 决策、01b 退回、真机矩阵、离线1/3/4 — 均 >17h 零产物；离线5 >10.8h 无新 JSON。
真机提醒: **未触发**（离线1/3/4/5 仍未交付；矩阵无「必须开游戏」清单）。游戏 PID 34848 本就在跑，勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍成立（14:32 首提、未决）→ 需决定：充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①dafors 自有可见窗口 1 个（SJZ 悬浮窗，17:31 起）——若是自测请 3 分钟内收起并确认退出，若属长期挂机则违反「桌面零可见窗口」硬约束；②迷你控制条 +460 行未提交，建议自测通过后按功能域提交（desktop 窗口收缩 / web 迷你视图 / jsapi 测试）；③离线1/3/4/5、矩阵、01b、缺口4 长时间零产物，重派或移出清单；④游戏空转 ~2.3 核，若不继续真机建议退出释放。

## 2026-11-08 18:03 第五十一次快照（对比 17:34，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词 0 命中，第二十九次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 18:02:04–18:02:10 | **疑似离线1 44755992**（迷你控制条） | **有新结果，但自测卡死未收尾** | `%TEMP%\dfmini\mini_results.json` 8 步全出：启动 1,127.1ms；整窗收缩 1164×761→720×56 host **7.5ms**；未抢焦点 fg_same=true；迷你按钮价格 53,831→54,331（server 同值）；同步 price req 12.1ms / quota 14.1ms / 模式切换 407.3ms；拖动 dx-360 dy192 尺寸不变。**缺 collapse_again / restore / exit_timeout 三步**；心跳止于 18:02:10，18:03:41 复查仍无更新；进程 PID 42532(229MB)、36752 仍活；`mini_close_t0.txt` 未生成 |
| 17:44:34–17:48:54 | 同上 | 工作树继续增长，未提交 | `web/index.html` +278、`desktop.py` +227、`core/nav_col.py` +9、`tests/test_desktop_jsapi.py` +1 → **4 M 共 +512/−3**（上一快照 3 M +460） |
| 17:34 后 | 回归/性能/真机 | 无变化 | `lastfailed` 仍 17:17:32（8 行 6 条既有失败）；`perf\out` 最新 `chain_ab.json` 07:43:42；`data` 最新组 `46e0738c` 05:06:23 |
| 18:02–18:03 | 运行态/窗口 | **合规偏差 3 项** | 可见窗口 16→**18**；dafors 自有 **3**：42532「SJZ 抢购控制台」+「SJZ 悬浮窗」（自测窗口未关，超 3 分钟）+ hwnd 62524314 `class=Ghost`「SJZ 抢购控制台 (未响应)」（归 dwm.exe，旧实例残留）；:8777 ← 42532，:8080 ← 23112，:8081 ← 45872 |
| 18:02:12 | 自测细节疑点·待确认 | 新疑点 1 项 | `WM_MOUSEACTIVATE` 返回 **1 = MA_ACTIVATE**，脚本内写 `expect=3`(MA_NOACTIVATE)；同轮实测前台未变（fg_same=true），暂不能判定会抢焦点 |
| 17:34 后 | 缺口1/2/4、矩阵、离线2/3/4/5、01b、合并收口 | 零新产物 | 自 00:13 后无新报告/产物；离线5 最新性能 JSON 仍 07:43（>10.5h） |

挂起项: 迷你控制条自测 hang；+512 行未提交；缺口4 决策、01b 退回、真机矩阵、离线1/3/4 — 均 >18h 零产物。
真机提醒: **未触发**（离线五路未全交付，矩阵无「必须开游戏」清单）。游戏 PID 34848 仍在跑（CPU 122,882s / 5.3GB），勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍未决（14:32 首提，已 3.5h）。
异常建议: ①自测卡在拖动后 `evaluate_js`（脚本注释已记载该阻塞），建议改 collapse_again 走 `_MINI` 状态轮询 + 直接 PostMessage WM_CLOSE，别再用 js；②结束 42532/36752 后清 `%TEMP%\dfmini`，并让 Ghost 窗口随宿主消亡；③3 个自有可见窗口违反「桌面零可见窗口」硬约束，自测完必须收；④迷你条 +512 行按功能域拆提交（窗口收缩 / web 迷你视图 / nav 收起 / jsapi 测试）；⑤幽灵疑点 WM_MOUSEACTIVATE 建议补一次真点击前台校验再定性。

## 2026-11-08 18:34 第五十二次快照（对比 18:03，仅记变化）

> 工具说明: 本环境仍无 list_sessions（ToolSearch 精确/关键词 0 命中，第三十次查无）；判定依据 = git 提交/工作树 + 文件 mtime + 进程表 + 端口 + pytest 缓存 + %TEMP% 探针 + 可见窗口枚举。

| 时间戳 | 会话/线 | 状态 | 变化/关键数字 |
|---|---|---|---|
| 18:25:06–18:25:53 | **离线1 44755992**（迷你控制条） | **自测闭环 + 已交付入库** | `desktop.log`：mini-enter 1164×761→720×56 **3.8ms**、mini-exit 还原 **5.1ms**、18:25:13「窗口已关闭」后正常退出——上一快照缺的 collapse_again/restore/exit_timeout 已补齐，hang 解除；18:29:14 提交 `1052e98`：`desktop.py` +231、`web/index.html` +278、`tests/test_desktop_jsapi.py` +1（**509 行**） |
| 18:12:47 | **离线2 697db934** | **实质进展：目标库继续扩充** | 提交 `6bd16ff`：`core/skins_lib.json` +348/−10，**232→252 条 / 12 赛季**（新增 20 条 S12「微光」王者之剑）；同提交带 `core/nav_col.py` +9 |
| 18:31 | 运行态/窗口 | **合规恢复** | 可见窗口 18→**14**；dafors 自有 **0**（42532／36752／:8777／Ghost 句柄全部消失）；`%TEMP%\dfmini` 已清；监听端口只剩 :8080(23112)、:8081(45872)，:8777 已收 |
| 18:26:00 | 回归 | 有一次采集，失败清单无新增 | `nodeids` 17:17:32→**18:26:00** 刷新；`lastfailed` 仍 17:17:32 / **6 条**原失败（cursor_lock、desktop_jsapi、mutation_killers×2、nav_scroll、portability_hardening） |
| 18:25:55 | 数据目录 | 非真机采集 | `data` 新增 3 组（57a0264c／71abff8b／4909e6a8），三组 `buy.png` MD5 全同 `2C586013…` → 属测试夹具，不计真机 |
| 18:03 后 | 缺口1/2/4、矩阵、离线3/4/5、01b、合并收口 | 零新产物 | 自 00:13 无新报告/产物；离线5 最新性能 JSON 仍 07:43:42（**>10.9h**） |
| 18:31 | git | 同步 | HEAD `1052e98`，origin/main 0/0；工作树仅 `_watch.md`（+18 未提交，本快照一并入库） |

挂起项: 缺口1/2/4、01b 退回、真机矩阵、离线3/4/5、合并收口 — 均 >18.5h 零产物。
真机提醒: **未触发**（离线五路未全交付，矩阵无「必须开游戏」清单）。游戏 PID 34848 仍在跑（CPU 126,454s / 5.23GB，29min +3,572s ≈ 2.05 核），勿再开第二份客户端。
需要用户输入: 三角币余额 **8 < 最低单价 10** 仍未决（14:32 首提，已 4h）→ 充值 / 换低价标的 / 或以「余额不足」为当前终点。
异常建议: ①离线3/4/5、矩阵、01b、缺口1/2/4 与合并收口长时间零产物，建议重派或移出清单，避免长期虚挂；②`test_desktop_jsapi.py::test_minimize_and_destroy_reach_the_window` 仍是 6 条既有失败之一，而迷你条刚改窗口收缩/还原路径，建议优先补跑该条；③WM_MOUSEACTIVATE 疑点本轮未复验（代码已 NOACTIVATE 兜底、自测 fg_same=true），建议一次真点击校验定性；④游戏空转 ~2.05 核，不继续真机建议退出释放。