"""点击组：连点节奏（间隔/抖动/漂移）、按住时长、通道往返。

约束：默认**不发任何真实输入事件**。连点节奏用假 sink（真实 sleep 按住时长）
测 click_burst 控制器本身；物理层往返只在 `--allow-inject` 打开时才发
**零位移相对事件**（不动光标、不按键）测 send 往返。「落地延迟」需要
Raw Input 旁观窗口，属于注入通道专项，基线里单列引用，不在本组伪造。
"""

from __future__ import annotations

import time

from common import Summary, now


def _slope_ms_per_1000(xs, ys):
    """一阶最小二乘斜率，放大到「每 1000 次点击漂移多少 ms」。"""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den) * 1000.0


def run(mods, root, args, cfg):
    burst = mods["click_burst"]
    inter = mods.get("interception")
    c = cfg["click"]
    hold_ms = float(c["hold_ms"])
    interval_ms = float(c["interval_ms"])
    run_s = float(c["run_s"])

    plan = {"mode": burst.MODE_BURST, "hold_ms": hold_ms,
            "interval_ms": interval_ms, "window": burst.WINDOW_ALL}

    beats = []

    def click(hold_s):
        # 假 sink：真实占用按住时长，不碰任何输入通道
        time.sleep(hold_s)

    def dialog():
        return False

    def beat():
        beats.append(now())

    notes = {}
    t_fire = now()
    res = burst.run(plan, t_fire=t_fire, fire_timeout=run_s, click=click,
                    dialog=dialog, beat=beat)
    elapsed_ms = (now() - t_fire) * 1000.0

    holds = res["holds_ms"]
    periods = [(beats[i] - beats[i - 1]) * 1000.0 for i in range(1, len(beats))]
    gaps = []
    for i in range(1, min(len(beats), len(holds))):
        gaps.append((beats[i] - beats[i - 1]) * 1000.0 - holds[i - 1])

    s_hold = Summary(warmup=5)
    s_hold.extend(holds)
    s_gap = Summary(warmup=5)
    s_gap.extend(gaps)
    s_period = Summary(warmup=5)
    s_period.extend(periods)

    s_overhead_us = Summary(unit="us", warmup=5)
    s_overhead_us.extend([(h - hold_ms) * 1000.0 for h in holds])

    period_p50 = s_period.summary()["p50"]
    period_p95 = s_period.summary()["p95"]
    jitter = (period_p95 - period_p50) if period_p50 is not None else None
    drift = _slope_ms_per_1000(list(range(len(periods))), periods)

    # ---------- 调度粒度下界：sleep(1ms) 的实际落点 ----------
    s_quant = Summary(warmup=3)
    for _ in range(50):
        t0 = now()
        time.sleep(0.001)
        s_quant.add((now() - t0) * 1000.0)

    metrics = {
        "hold_ms": s_hold.summary(),
        "sleep_quantum_ms": s_quant.summary(),
        "gap_ms": s_gap.summary(),
        "period_ms": s_period.summary(),
        "hold_overhead_us": s_overhead_us.summary(),
        "first_lead_us": {
            "n": 1, "n_raw": 1, "warmup": 0, "cold_p50": None,
            "p50": (beats[0] - t_fire) * 1e6 if beats else None,
            "p95": None, "max": None, "mean": None, "min": None,
            "dropped": 0, "unit": "us",
        },
    }
    extra = {
        "clicks": res["clicks"],
        "stopped_by": res["stopped_by"],
        "elapsed_ms": round(elapsed_ms, 1),
        "clicks_per_s": round(res["clicks"] / (elapsed_ms / 1000.0), 3) if elapsed_ms else None,
        "jitter_ms": round(jitter, 3) if jitter is not None else None,
        "drift_ms_per_1000_clicks": round(drift, 3) if drift is not None else None,
        "plan": plan,
    }

    # ---------- 物理通道 ----------
    land_note = ("落地延迟与光标占用需要「真发 down/up + Raw Input 旁观窗口」，"
                 "按本次约束不发；引用 _branchA_input_bench.md 的实测区间，"
                 "需要新数值时用 --allow-inject 单跑。")
    driver = None
    try:
        ready, why = inter.driver_ready(probe=False)
        ident = inter.driver_status(probe=False)
        driver = {"ready": ready, "why": why,
                  "mouse": (ident.get("mouse") or {}).get("device")}
    except Exception as e:  # noqa: BLE001
        driver = {"ready": False, "why": str(e)}

    if args.allow_inject and driver and driver.get("ready"):
        drv = inter.shared()
        dev = drv.mouse_device()["device"]
        s_send = Summary(warmup=5)
        for _ in range(int(cfg["nop"]["count"])):
            t0 = now()
            drv.send_mouse(0, x=0, y=0, device=dev)   # 零位移：光标不动
            s_send.add((now() - t0) * 1000.0)
        metrics["send_roundtrip_ms"] = s_send.summary()
        notes["物理层"] = ("send 往返（异步受理，不含落地）；零位移相对事件，"
                          "光标未移动、未按键。")
        extra["driver"] = driver
    else:
        extra["driver"] = driver
        notes["物理层"] = land_note + " 本地未开 --allow-inject。"

    notes["节奏"] = (f"burst hold={hold_ms:g}ms interval={interval_ms:g}ms 跑 {run_s:g}s；"
                    "间隔/抖动来自 beat 时间戳，漂移=周期对点击序号的最小二乘斜率×1000。")
    return {"metrics": metrics, "accuracy": {}, "extra": extra, "notes": notes}
