"""每件随机刷新次数（配置命名空间 scan.repeat.*）。

为什么要有这一层
    倒计时路线里「挑不到这一件就刷新」原来看的是全局节奏，一页只刷一次。
    但不同挂单的竞争强度不一样：热门货刚被人抢走时，刷新往往还能看到新挂单；
    冷门货刷几次也一样。让**每一件各自**抽一个刷新次数，热门货多试几次，
    冷门货也不会把时间全烧在刷新上。

语义（契约 §6，界面文案必须一致）
    刷新是**重新排队/重新抽样**，不是「同一件多一次中签机会」。所以这里只
    控制「这件没挑到时还刷不刷、刷几次」，不承诺提高成功率。

两个字段（只有这两个）
    scan.repeat.min   最少刷新次数，默认 1，合法 0~10
    scan.repeat.max   最多刷新次数，**默认 1**，合法 0~10
    0/0 = 完全不刷新。跨字段 min <= max，倒挂整批拒收。

默认值为什么不按契约写 1~3
    契约示例里 default 是 1/3。但 1/3 会让所有老用户升级后「空列表刷一次」
    变成「随机刷 1~3 次」—— 默认值不得改变既有行为。所以默认落 1/1，
    等价于现状（没货只刷一次就放弃）；随机区间只有在用户显式把 max 调大
    之后才生效。这条差异已写进分支报告。

硬约束
    · 每件独立 randint(min, max)，不复用上一件的值。
    · 刷新次数在**点刷新之前**扣：刷新失败也算一次，不能白刷。
    · 次数用尽即退出，由调用方进入下一步（不无限刷）。
    · 两次刷新之间至少隔 GAP_FLOOR_S 秒；间隔取「列表扫描间隔」配置，
      再压上这个硬下限，避免把刷新按钮点爆。
"""

from __future__ import annotations

import random

from . import switches

# 与契约 §2.3 一致：0~10，0/0 合法（完全不刷新）
HARD_MIN = 0
HARD_MAX = 10
DEFAULT_MIN = 1
DEFAULT_MAX = 1      # 等价现状：没货只刷一次。显式调大 max 才启用随机区间

# 两次刷新之间的硬下限（秒）。间隔本身走既有的「列表扫描间隔」
# （scan.interval → list_scan_interval），这里只兜底，避免配置成 0.001 秒
# 时把刷新点成连发。
GAP_FLOOR_S = 0.5


class RepeatConfigError(ValueError):
    """区间倒挂（最少 > 最多）：入口必须拒收，引擎侧只能显式放弃本功能。"""


def limits(cfg):
    """读 (最少, 最多)。单字段越界由 switches.read 退回默认并记 issues；
    倒挂没法退回（那会静默变成另一个区间），抛 RepeatConfigError。"""
    lo = switches.read(cfg, "scan.repeat.min", DEFAULT_MIN, "int",
                       HARD_MIN, HARD_MAX, key="scan.repeat.min")
    hi = switches.read(cfg, "scan.repeat.max", DEFAULT_MAX, "int",
                       HARD_MIN, HARD_MAX, key="scan.repeat.max")
    if lo > hi:
        raise RepeatConfigError(f"最少 {lo} 大于最多 {hi}")
    return lo, hi


def draw(lo=None, hi=None, rng=None):
    """这一件抽到的刷新次数。传 cfg 时先走 limits()。

    抽签用独立的 Random，测试可以固定种子复现分布。
    """
    rng = rng or random
    if lo is None or hi is None:
        return rng.randint(DEFAULT_MIN, DEFAULT_MAX)
    return rng.randint(int(lo), int(hi))


def gap_floor(interval):
    """把「列表扫描间隔」压成刷新间隔：不低于 GAP_FLOOR_S。"""
    try:
        value = float(interval or 0)
    except (TypeError, ValueError):
        value = 0.0
    return max(GAP_FLOOR_S, value)
