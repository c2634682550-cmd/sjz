# 假购买补刀（buy.followup）分支说明

- 改动文件：`core/config.py`（FEATURE-CONFIG 块内 `buy.followup.enabled=false`）；`core/switches.py`（`fire.verify` 后新增开关项 + `_coerce` 的 strict bool 分支）；`core/engine.py`（`_fire_and_confirm` 开火 miss 分支、`_record` 的 extra、`status()` stats 稳定形状）；新增 `tests/test_feature_followup.py`。
- 新增键（仅此一个，严格布尔，缺键走默认）：`buy.followup.enabled`（默认 false）。没有数值字段，故无 BOUNDS 追加。
- 判定路径：开火循环 `fire.timeout` 内没等到确认弹窗 → 开关开且 `click_burst.begin()` 没进 burst → 在同一购买点 `buy_click` 补 1 次 → 最多看 0.25s 的确认弹窗；出现则继续走原确认闭环，仍没有就按 `miss` 记一次并返回 False。抽签模型下只重试同一次进场；补刀结果没有二次确认前不会算成交。
- 记账：整次尝试只走一次 `_record`；`extra.followup_used` 仅记录事实，金额/预算不经补刀路径；`stats.followup={enabled,effective,used}` 未开跑也有稳定形状（`status()`）。
- 互斥：连点 `burst` 生效时补刀显式跳过并打 info「连点开启，假购买补刀本次不生效」，配置照常保存；`append` 放行。半自动在更早处 Abort，永不补刀；无确认区域版式走 `_buy_without_dialog`，不进入补刀。
- 验证数字：新用例 35 passed；相关回归（switches/frontend_static/click_speed/config/numeric_hardening/engine_shot/followup）141 passed；全程无真实输入、无窗口、无光标移动。
- 未覆盖边界：`blk-fire-followup` 区块与 `data-sw` 控件由合并方的共享脚本生成，本分支不碰 `web/index.html`，只断言挂载点 `#swFire`、`data-sw/data-kind` 与分组映射；调度器层「买到即停」只验证单次 hit 记录，停止动作随既有链路；0.25s 观察窗是硬编码常量，未做可配置。
- 与契约差异：先前口头版四字段（delay_ms/max_attempts/require_evidence）按契约收敛掉，只留 `enabled`；先前 `blk-buy-followup-*` 作废；契约的 `FEATURE-CONFIG:BEGIN` 在盘上已被写成 `FEATURE-CONFIG BEGIN`（非本分支写入，未擅自改）。
