"""锁住「工作集 8h 外推」的口径约束。

背景（`_longrun_stability.md` §14）：65 分钟线性外推把预热期一次性装载台阶
放大成 +16.2%~+18.96%，三组实测（real65 / real62 / comp480）绝对增量都封顶在
+3.5~4.1 MB。probe-wssplit 150 轮实测：平台期每会话常驻增量只有预热期的
**3.3%**（`tail_over_head=0.033`），私有提交从第 45 轮起完全不再上涨。

两个不变量：
  1. 饱和序列必须判成 `bounded`，8h 增量取实测封顶值，而不是斜率 × 8；
  2. 平台期每会话增量不得超过预热期的 5%（实测 3.3%，留 1.5 倍余量）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.longrun_collect import _extrapolate, _head_tail_ratio  # noqa: E402

X_PLATEAU_RATIO = 5.0     # 平台期/预热期 每会话增量上限（%）
X_CAPPED_PCT = 8.0        # 饱和后 8h 外推上限（%），实测 7.35%~7.8%


def _ramp_then_flat(n=130, base=48.0, step=3.7, ramp_frac=0.66):
    """0.5min 采样：前 66% 时间爬完 +step MB，之后平台。"""
    xs = [i * 0.5 / 60.0 for i in range(n)]
    span = xs[-1] * ramp_frac
    ys = [round(base + step * min(1.0, x / span), 2) for x in xs]
    return xs, ys


def _cycle_rows(n=150, base=47.9, step=2.16, ramp_cycles=60, every=15):
    """probe-wssplit 同构的逐轮序列：前 60 轮爬完，之后平台。"""
    rows = [{"cycle": 0, "ws_mb": base}]
    for c in range(every, n + 1, every):
        rows.append({"cycle": c,
                     "ws_mb": round(base + step * min(1.0, c / ramp_cycles), 2)})
    return rows


def test_saturating_ramp_is_not_extrapolated_as_leak():
    xs, ys = _ramp_then_flat()
    ex = _extrapolate(xs, ys)
    assert ex["bounded"] is True, "台阶饱和序列必须被判成 bounded"
    # 线性口径会凭空造出 >15% 的假增长
    assert abs(ex["d8_linear"] * 8.0) > 0.1
    assert ex["d8"] == ys[-1] - ys[0], "饱和时 8h 增量必须取实测封顶值"
    assert ex["pct8"] <= X_CAPPED_PCT


def test_plateau_per_cycle_cost_under_5pct_of_warmup():
    rows = _cycle_rows()
    head, tail, ratio = _head_tail_ratio(rows, "ws_mb")
    assert head > 0
    assert ratio is not None and ratio * 100 <= X_PLATEAU_RATIO, (
        "平台期每会话增量 %.1f%% 超过预热期的 %.1f%%"
        % (ratio * 100, X_PLATEAU_RATIO))


def test_linear_ramp_stays_unbounded():
    """真泄漏（全程线性）不能被「饱和」判据漏掉。"""
    xs = [i * 0.5 / 60.0 for i in range(130)]
    ys = [48.0 + 1.0 * x for x in xs]
    ex = _extrapolate(xs, ys)
    assert ex["bounded"] is False
    assert ex["d8"] > 7.9
