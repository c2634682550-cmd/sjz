# 分支说明：空闲漫游（feature:mouse.idle）

- 配置：`mouse.idle.mode`（off/move/move_wheel）、`interval_ms`、`return_s`、`min_remain_s`、`wheel_notches`；默认 1200/2/8/3；越界与类型错由开关表拒收并在 message 中带键名。
- 行为：仅倒计时路线生效；`left > max(min_remain_s, FIRE_LEAD_S+return_s+1)` 才漫游；`left <= FIRE_LEAD_S+return_s` 立即归位并锁死；归位点取漫游前光标（在客户区内）否则客户区中心；全程零点击。
- 移动：一律调 `Mouse.move_path(points, total_ms)`，与拟人轨迹共用同一入口；点全部钳制在客户区内缩 8px 的范围。
- 降级：消息通道滚轮无效（`message_no_wheel`），且无真实光标移动；启动时一条 info 如实说明。客户区读不到 → 本轮不漫游 + 一条 warn。前台丢失 → 直接退出且不发漫游事件。
- 观测：`engine.status()["stats"]["roam"]` 暴露 mode/effective/phase/moves/wheel/clicks/settled_at/settled_left_s/events/trace/path/home/client/reason。
- 改动文件：`core/idle_roam.py`（新增）、`core/config.py`（mouse.idle 块）、`core/switches.py`（5 个开关 + BOUNDS）、`core/engine.py`（_run 构造 / _watch_countdown 每帧 tick / status.stats.roam）、`tests/test_feature_mouse_idle.py`（新增）。
- 前端：未改 `web/index.html`；控件由 `/api/switches` 自动渲染，共享分组脚本已把 `mouse.idle.` 归入 `#blk-fire-idle`（`#swFire` 内，data-sw/data-kind 绑定）。
- 验证：`python -m pytest tests/test_feature_mouse_idle.py -q` → 23 passed；连同 switches/numeric/frontend/countdown 共 155 passed。
- D1 证据：60s 模拟 → moves=43、clicks=0、settled_left_s=7.0，即开点窗口（剩余 5s）前 2.0s 归位，全部轨迹点 inside=True。
- D2 证据：GET 含 5 键 → POST 落盘 → `Config.load` 读回 → 同一配置喂给引擎侧会话，move_wheel 有移动+滚轮（2 格）。
- D3/D4 证据：非法模式/负值/None/字符串/小数/范围外全部 400 或 SwitchValueError 带键名；失败批次盘上 SHA256 不变。
- D5 证据：与 FakeTrajectory 同开时 `path_for` 被调用；剩余 6.5s 归位后继续 tick 零移动零事件；list 路线零事件 + 一条说明。
- 与早期口述的差异（已按契约收敛）：`blk-idle-*` → `blk-fire-idle`；`interval_s/start_after_s/radius/home` → `interval_ms/min_remain_s/return_s`（归位点派生，不设 home 键）；“开点前 N 秒”实现为“开点窗口（FIRE_LEAD_S=5s）前 return_s 秒”；不新增 `/api/idle` 路由。
- 结构说明：`mouse.idle` 必须并入既有 `"mouse"` 字典（契约要求同一命名空间只出现一次），因此其块标记位于 `mouse.human` 标记包络内，未改动 human 内容。
