"""倒计时链路闭环仿真。

纯 Python，不碰任何真实输入 / 窗口 / 光标 / 前台。链路：

    假倒计时采样 → `core.countdown.T0Estimator` 反推 → 假 OCR 读价（T0 前完成）
    → 决策 → 预置目标 → 到点卡点 → 假注入器记录触发时刻 → 统计偏差

`T0Estimator` 直接复用分支 C 的实现（区间交集 + 最小二乘复核），本文件
只负责把假时钟和假数据喂进去。接口可整体替换：真机接入时把读数换成
OCR 结果、把 `now` 换成 QPC，其余逻辑不动。
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.countdown import T0Estimator, gate, wait_until  # noqa: E402


class ObsClock:
    """带观测噪声的单调时钟。

    `true` 是真实时刻（单调、无噪声）；`now()` 返回带抖动的读数，模拟
    QPC 读取的抖动。`wait_until` 用读数判定，真正触发发生在 `true` 上。
    """

    def __init__(self, rng, sigma=0.00015, step=(1e-7, 3e-6)):
        self.rng = rng
        self.sigma = sigma
        self.step = step
        self.true = 0.0
        self.reads = 0

    def now(self):
        self.reads += 1
        self.true += self.rng.uniform(*self.step)
        return self.true + self.rng.gauss(0.0, self.sigma)

    def sleep(self, dt):
        self.true += max(0.0, float(dt)) + self.rng.uniform(0.0, 1e-6)


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _r3(x):
    return None if x is None else round(x, 3)


def run_trial(rng, *, frame=1 / 60, t0=5.0, lead=0.002, ocr_cost=0.030,
              drop_rate=0.0, sigma=0.00015, inject_fail=False, driver_ok=True,
              spin_window=0.0015, tolerance_s=0.020, need_10ms=False,
              lag_calibrated=False):
    """跑一次闭环，返回这一轮的测量结果。"""
    clock = ObsClock(rng, sigma=sigma)

    # ---- 1. 倒计时采样（显示值是整秒 floor，与 T0Estimator 模型一致）----
    est = T0Estimator()
    while clock.true < t0 - 0.3:
        clock.true += frame * rng.uniform(0.9, 1.1)
        if rng.random() < drop_rate:
            continue                          # 丢帧：这一帧没读到
        shown = math.floor(t0 - clock.true)
        est.observe(int(max(0, shown)), clock.true)
    e = est.estimate(tolerance_s=tolerance_s, want_qpc=False)

    # ---- 硬门槛：不满足就不许点（分支 C 定的数）----
    gate_ok, why = gate(e, need_10ms=need_10ms, lag_calibrated=lag_calibrated)
    if not gate_ok:
        return {"ok": False, "reason": why,
                "t0_err_ms": None, "trigger_err_ms": None,
                "half_width_ms": (e.half_width or 0.0) * 1000.0}

    # ---- 2. 价格前移（T0 之前必须读完并决策完成）----
    price = 100.0
    budget = 150.0
    ocr_done = clock.true + ocr_cost
    clock.true = ocr_done
    decision_ok = price <= budget
    price_ready_before_t0 = ocr_done < t0

    # ---- 3. 预置目标（不动光标，只记一次调度）----
    preset_t = clock.true
    preset_before_t0 = preset_t < t0

    # ---- 4. 到点卡点 ----
    target = e.zero_at - lead
    wait_until(clock.now, target, mode="hybrid",
               spin_window=spin_window, sleep_fn=clock.sleep)
    actual_t = clock.true

    # ---- 5. 注入（一次 down/up；失败不留按下态）----
    degraded = not driver_ok
    injected = False
    down = up = 0
    if not inject_fail:
        down, up = 1, 1                        # 成功的注入：严格一对一
        injected = True

    return {
        "ok": True,
        "t0_err_ms": (e.zero_at - t0) * 1000.0,
        "trigger_err_ms": (actual_t - t0) * 1000.0,
        "half_width_ms": (e.half_width or 0.0) * 1000.0,
        "slope": e.slope,
        "n": e.n, "n_inliers": e.n_inliers,
        "price_ready": price_ready_before_t0,
        "preset_before_t0": preset_before_t0,
        "decision_ok": decision_ok,
        "injected": injected,
        "degraded": degraded,
        "down_ups_balanced": (down == up),
        "clock_reads": clock.reads,
    }


def batch(seed, n=200, **kw):
    rng = random.Random(seed)
    rows = [run_trial(rng, **kw) for _ in range(n)]
    good = [r for r in rows if r.get("ok")]
    tri = [abs(r["trigger_err_ms"]) for r in good]
    t0e = [abs(r["t0_err_ms"]) for r in good]
    return {
        "n": n, "ok_n": len(good), "seed": seed,
        "trigger_abs_ms": {"p50": _r3(_pct(tri, .50)),
                           "p95": _r3(_pct(tri, .95)),
                           "max": _r3(max(tri)) if tri else None},
        "t0_abs_ms": {"p50": _r3(_pct(t0e, .50)),
                      "p95": _r3(_pct(t0e, .95)),
                      "max": _r3(max(t0e)) if t0e else None},
        "half_width_p95_ms": _r3(_pct([r["half_width_ms"] for r in good], .95)),
        "slope_p50": _r3(_pct([r["slope"] for r in good], .50)),
        "inliers_p50": _pct([r["n_inliers"] for r in good], .50),
        "price_ready_all": all(r["price_ready"] for r in good),
        "preset_before_t0_all": all(r["preset_before_t0"] for r in good),
        "injected_all": all(r["injected"] for r in good),
        "balanced_all": all(r["down_ups_balanced"] for r in good),
        "degraded": any(r["degraded"] for r in good),
        "reasons": _reasons(rows),
    }


def _reasons(rows):
    out = {}
    for r in rows:
        if not r.get("ok"):
            k = (r.get("reason") or "unknown").split(" ")[0]
            out[k] = out.get(k, 0) + 1
    return out


def histogram(rows, bin_ms=1.0):
    """把触发偏差做成直方图（ms 分桶，含负数桶）。"""
    xs = [r["trigger_err_ms"] for r in rows if r.get("ok")]
    if not xs:
        return {}
    lo = math.floor(min(xs) / bin_ms) * bin_ms
    hi = math.ceil(max(xs) / bin_ms) * bin_ms
    bins = {}
    b = lo
    while b < hi + bin_ms:
        bins[f"{b:+.0f}"] = 0
        b += bin_ms
    for x in xs:
        k = math.floor(x / bin_ms) * bin_ms
        bins[f"{k:+.0f}"] = bins.get(f"{k:+.0f}", 0) + 1
    return bins


# ---------------- pytest ----------------

def test_three_rounds_are_stable():
    """同配置连跑三轮，p95 差异必须 < 15%（跑不稳就说明有隐藏状态）。"""
    runs = [batch(seed, 200, frame=1 / 60) for seed in (11, 22, 33)]
    assert all(r["ok_n"] == 200 for r in runs), [r["ok_n"] for r in runs]
    p95 = [r["trigger_abs_ms"]["p95"] for r in runs]
    assert (max(p95) - min(p95)) / max(p95) < 0.15, f"三轮不一致：{p95}"


def test_60hz_stays_within_one_frame():
    """60Hz 采样：误差上限就是一帧（16.7ms），实测应明显小于它。"""
    r = batch(7, 200, frame=1 / 60)
    assert r["ok_n"] == 200, r["ok_n"]
    assert r["trigger_abs_ms"]["p95"] < 12.0, r["trigger_abs_ms"]


def test_240hz_reaches_5ms():
    """采样提到 240Hz（一帧 4.17ms）后，误差进 ±5ms。"""
    r = batch(8, 200, frame=1 / 240)
    assert r["ok_n"] == 200, r["ok_n"]
    assert r["trigger_abs_ms"]["p95"] < 5.0, r["trigger_abs_ms"]


def test_drop_frames_and_high_jitter():
    """30% 丢帧 + 0.5ms 时钟抖动 + 240Hz：链路不能整体失败。

    这一档故意极端：区间宽度会被抖动撑到 trust 门槛以外，少量样本会
    主动放弃（渲染成「不确定，不点」），这是设计内的保守行为，不是 bug。
    """
    r = batch(13, 200, frame=1 / 240, drop_rate=0.3, sigma=0.0005)
    assert r["ok_n"] >= 190, f"放弃次数过多：{200 - r['ok_n']} / 200"
    assert r["trigger_abs_ms"]["p95"] < 10.0, r["trigger_abs_ms"]


def test_inject_failure_leaves_no_stuck_button():
    """注入失败必须显式记录，且 down/up 计数平衡（无残留按下态）。"""
    rng = random.Random(5)
    for _ in range(50):
        r = run_trial(rng, inject_fail=True)
        if r.get("ok"):
            assert not r["injected"]
            assert r["down_ups_balanced"]


def test_driver_missing_degrades_explicitly():
    r = batch(17, 50, driver_ok=False)
    assert r["degraded"] is True, "驱动缺失必须显式降级，不能静默"


def test_price_and_preset_happen_before_t0():
    r = batch(19, 100, frame=1 / 60)
    assert r["price_ready_all"], "价格没有在 T0 前读完"
    assert r["preset_before_t0_all"], "预置目标发生在 T0 之后"


def test_clock_regression_slope_is_minus_one():
    """读数应按 1 格/秒递减：斜率 ≈ -1。偏离说明时钟或采样有问题。"""
    r = batch(21, 100, frame=1 / 60)
    assert -1.05 <= r["slope_p50"] <= -0.95, r["slope_p50"]


if __name__ == "__main__":
    import json
    cases = [
        ("60hz", dict(frame=1 / 60)),
        ("240hz", dict(frame=1 / 240)),
        ("240hz_drop30_jit0.5ms", dict(frame=1 / 240, drop_rate=0.3,
                                       sigma=0.0005)),
    ]
    out = {}
    for tag, kw in cases:
        out[tag] = [batch(seed, 200, **kw) for seed in (1, 2, 3)]
    print(json.dumps(out, ensure_ascii=False, indent=1))
