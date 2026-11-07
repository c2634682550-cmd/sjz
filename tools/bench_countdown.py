"""倒计时 T0 反推的误差分布基准。

模拟「每帧一次读数」：读数 = floor(T0 - t)（显示按秒截断），采样间隔带抖动，
随机 5%~10% 的帧读错（±1 为主、偶尔 ±2），3% 的帧完全读不到（丢帧）。

三组实验：

  base      —— 等间隔刷新假设下的两个独立采样模型（抖动/泊松）× 3 个种子。
  irregular —— 刷新**不**等间隔的现实场景（见 SCENARIOS）。
  feed      —— 不同**喂法**：整帧、只喂跳变沿、跳变前/后成对。

统计口径（每个预算都出）：

  * `p95/CI`：只在**可疑估出数**的样本上算绝对误差分位数，并给 bootstrap
    95% 置信区间；样本少时置信区间会明显变宽，这是如实呈现，不修饰。
  * `coverage`：跑出数的比例（不可信时 zero_at 为 None，视为没出数）。
  * `trust` / `gate`：可信率 / 硬门槛通过率 —— 分母是**全部** run，不是出数的 run。
  * `within20_all`：全部 run 里误差 ≤20ms 的比例（不可信算失败）。
  * `false_trust`：声称可信但误差 >50ms 的比例 —— 最危险的失败模式。
  * `recall` / `fp`：错读剔除率 / 干净读数被误杀率，逐预算统计。
  * `p95 batch`：把 run 分 10 批各算 p95，给 min/mean/max —— 单次 p95 会因
    抽样相位而漂，分批复现才能看出真实波动（审查要求）。

用法（在项目根目录）::

    .venv\\Scripts\\python.exe tools/bench_countdown.py --runs 200
    .venv\\Scripts\\python.exe tools/bench_countdown.py --runs 200 \\
        --json tools/bench_countdown.json
"""

import argparse
import json
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.countdown import T0Estimator, gate  # noqa: E402

# 上限到 300：EST_MAXLEN=320 已不再截断（旧版 256 会把 300 截成 256，报告作废）。
BUDGETS = (1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 80, 120, 160, 200, 250, 300)
TICK_BUDGETS = (1, 2, 3, 5, 8, 12, 16, 20, 25, 30)
# 成对喂法按「读数条数」算预算：6 个跳变 = 12 条。
PAIR_BUDGETS = (2, 4, 6, 8, 10, 12, 16, 20, 24, 32)

SEEDS = (20261107, 1234567, 987654321)
ERR_RANGE = (0.05, 0.10)

BASE_MODELS = ("jitter", "poisson")

# 场景定义。每个场景是一个完整的采样+刷新假设。
SCENARIOS = {
    # 90% 帧 4~6ms 一帧，10% 帧卡顿 20~80ms（OCR 偶发停顿 / GC / 抢 CPU）
    "irregular/bursty": dict(model="bursty"),
    # 显示受 60Hz 帧率影响：采样时看到的是上一帧，延迟 d ~ U(0, 16.7ms)。
    # 估计器不知道 d，会整段偏晚约 d/2。lag_s 补偿版单独跑一组。
    "irregular/frame60": dict(model="frame60", display_s=1.0 / 60.0),
    "irregular/frame60+lag": dict(model="frame60", display_s=1.0 / 60.0,
                                 lag_s=1.0 / 120.0),
    # 只在读数变化时喂（≈1 条/秒）：刷新等间隔，但**采样**只剩跳秒沿。
    "irregular/tickonly": dict(model="jitter", tick_only=True,
                               start=(26.0, 45.0), budgets=TICK_BUDGETS),
    # 同上但 OCR 完全干净（无 ±1 抖动）：真正的「只读到跳秒沿」。
    "irregular/tickonly-clean": dict(model="jitter", tick_only=True,
                                     err_override=0.0,
                                     start=(26.0, 45.0), budgets=TICK_BUDGETS),
    # 跳变前一帧 + 跳变后第一帧都喂（审查要求的成对喂法）：
    # 6 个跳变 = 12 条读数，应该能锁到 1~3ms。
    "feed/tickbracket": dict(model="jitter", pair_only=True,
                             start=(26.0, 45.0), budgets=PAIR_BUDGETS),
    # 120 条最坏相位：整个窗口落在同一个 tick 内（start 固定 8.0s）。
    # 这条专门暴露小样本 + 单段数据的退化，不许藏。
    "irregular/120-oneseg": dict(model="jitter", start=(8.0, 8.0),
                                 budgets=(120,)),
    # 300 条、起始相位随机：验证 EST_MAXLEN 不再截断。
    "irregular/300": dict(model="jitter", budgets=(250, 300)),
}


def make_series(rng, model, n_max, mean_dt=0.008, display_s=0.0,
                start=(6.0, 40.0), tick_only=False, err_override=None,
                pair_only=False, max_frames=200000):
    """返回 (T0, [(t, 读数, 是否错读, 真值), ...])。

    display_s > 0 时模拟帧率刷新：采样看到的画面是 d 秒前渲染的，
    即 s = floor(T0 - t + d)，d ~ U(0, display_s)。
    tick_only=True 时只保留「显示值发生变化」的采样（上层仅在跳变时喂）。
    pair_only=True 时对每次变化同时保留**前一帧**与**变化后第一帧**
    （成对夹逼用；只喂沿会丢掉「跳变前最后一帧」，信息不可恢复）。
    """
    t0 = rng.uniform(60.0, 90.0)          # 归零时刻（随便放，避免 0 附近的假象）
    t = t0 - rng.uniform(*start)          # 起始剩余秒数
    err_rate = rng.uniform(*ERR_RANGE) if err_override is None else float(err_override)
    frames = []
    n_change = 0          # 观测值变化的次数（调用方看到的变化）
    n_true_change = 0     # 真值跳秒次数（物理跳变，成对喂法按它截断）
    prev_obs = None
    prev_true = None
    while len(frames) < max_frames:
        if model == "bursty":
            if rng.random() < 0.10:
                dt = rng.uniform(0.020, 0.080)
            else:
                dt = rng.uniform(0.004, 0.006)
        elif model == "poisson":
            dt = rng.expovariate(1.0 / mean_dt)
        else:                              # jitter / frame60 / tickonly 底层采样
            dt = rng.uniform(0.55 * mean_dt, 1.45 * mean_dt)
        t += max(dt, 0.0015)
        delta = rng.uniform(0.0, display_s) if display_s > 0 else 0.0
        s = int(math.floor(t0 - t + delta))
        if s < 0:
            break
        if rng.random() < 0.03:            # 丢帧：这一帧 OCR 没结果
            continue
        bad = rng.random() < err_rate
        s_obs = s
        if bad:
            # 单字抖动：±1 为主，偶尔 ±2（把 25 读成 27 之类）
            s_obs = max(0, s + rng.choice([-1, -1, -1, -1, 1, 1, 1, 1, -2, 2]))
        if prev_obs is not None and s_obs != prev_obs:
            n_change += 1
        if prev_true is not None and s != prev_true:
            n_true_change += 1
        prev_obs = s_obs
        prev_true = s
        frames.append((t, s_obs, bad, s))
        # 攒够就停：成对模式按**真跳变**计数（每对 2 条读数），其余按观测变化/帧数
        if pair_only:
            if n_true_change * 2 >= n_max:
                break
        elif tick_only:
            if n_change >= n_max:
                break
        elif len(frames) >= n_max:
            break

    if pair_only:
        # 只在**真跳变**处取一对：跳变前最后一帧 + 跳变后第一帧。
        # 前一帧的**读数**照样可能是 ±1 错读（真实数据就是这样），
        # 由 _pair_brackets 的 sa-sb==1 与整数格点校验自己筛掉。
        obs = []
        prev_true = None
        for i, fr in enumerate(frames):
            if prev_true is not None and fr[3] != prev_true and i > 0:
                obs.append(frames[i - 1])
                obs.append(fr)
            prev_true = fr[3]
        obs = obs[:n_max]
        return t0, obs

    if tick_only:
        obs = []
        prev_obs = None
        for fr in frames:
            if prev_obs is not None and fr[1] == prev_obs:
                continue
            prev_obs = fr[1]
            obs.append(fr)
        return t0, obs[:n_max]

    return t0, frames[:n_max]


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round((len(xs) - 1) * q)))]


def bootstrap_ci(xs, q=0.95, n_boot=2000, seed=0):
    """分位数的 bootstrap 95% 置信区间。样本 <5 直接返回 None。"""
    if len(xs) < 5:
        return None, None
    rng = random.Random(seed)
    n = len(xs)
    boots = []
    for _ in range(n_boot):
        boots.append(pct([xs[rng.randrange(n)] for _ in range(n)], q))
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[min(n_boot - 1, int(0.975 * n_boot))]
    return lo, hi


def run_combo(model, seed, runs, budgets, display_s=0.0, start=(6.0, 40.0),
              tick_only=False, lag_s=0.0, err_override=None, pair_only=False):
    """逐预算跑 runs 组，返回 {预算: [每条 run 的记录]}。

    每条记录 = (err_ms 或 None, trust, gate_ok, reason_code, hw, span,
                 n_inliers, recall, fp)。逐预算保留原始记录，才能算分批 p95
    和剔除率，也能整包落盘复核。
    """
    rng = random.Random(seed)
    recs = {b: [] for b in budgets}
    for _ in range(runs):
        t0, obs = make_series(rng, model, n_max=max(budgets),
                              display_s=display_s, start=start,
                              tick_only=tick_only, err_override=err_override,
                              pair_only=pair_only)
        if not obs:
            for b in budgets:
                recs[b].append((None, False, False, "no_data", None, 0.0, 0, 0.0, 0))
            continue
        for b in budgets:
            est = T0Estimator(lag_s=lag_s)
            for t, s, _bad, _true in obs[:b]:
                est.observe(s, t)
            e = est.estimate(want_qpc=False)
            ok, _why = gate(e)
            err = None if e.zero_at is None else (e.zero_at - t0) * 1000.0

            # 该预算对应的缓冲窗口（maxlen 截断后与 mask 对齐）
            buf = obs[max(0, b - est.maxlen):b]
            if len(buf) != len(e.mask):
                buf = obs[:b][-len(e.mask):]
            bad_idx = [i for i, o in enumerate(buf) if o[2]]
            n_bad = len(bad_idx)
            recall = (sum(1 for i in bad_idx if not e.mask[i]) / n_bad
                      if n_bad else None)
            fp = sum(1 for i, o in enumerate(buf) if not o[2] and not e.mask[i])
            n_clean = max(1, len(buf) - n_bad)
            recs[b].append((err, e.trust, ok, e.reason_code,
                            e.half_width, e.span, e.n_inliers,
                            recall, fp / n_clean))
    return recs


def _batch_p95(trusted_errs, batch_n):
    """把可信误差按 run 顺序分批，每批算 p95，返回 (min, mean, max)。"""
    if batch_n < 3 or len(trusted_errs) < batch_n * 3:
        return None, None, None
    outs = []
    for i in range(0, len(trusted_errs), batch_n):
        chunk = trusted_errs[i:i + batch_n]
        if len(chunk) >= 3:
            outs.append(pct(chunk, 0.95))
    if not outs:
        return None, None, None
    return min(outs), statistics.mean(outs), max(outs)


def summarize(recs, budgets, runs, ci_seed=1):
    rows = []
    for b in budgets:
        rs = recs[b]
        errs = [r[0] for r in rs if r[0] is not None]
        ae = [abs(x) for x in errs]
        te = [abs(r[0]) for r in rs if r[0] is not None and r[1]]
        false_trust = sum(1 for x in te if x > 50.0)
        n_trust = sum(1 for r in rs if r[1])
        n_gate = sum(1 for r in rs if r[2])
        recalls = [r[7] for r in rs if r[7] is not None]
        fps = [r[8] for r in rs]
        codes = {}
        for r in rs:
            codes[r[3]] = codes.get(r[3], 0) + 1
        ci_lo, ci_hi = bootstrap_ci(te, 0.95, seed=ci_seed + b)
        bp = _batch_p95(te, max(5, runs // 10))
        rows.append({
            "budget": b,
            "runs": runs,
            "coverage": round(len(errs) / max(1, runs), 3),
            "p50_abs_ms": round(pct(ae, 0.50), 2) if ae else None,
            "p95_abs_ms": round(pct(ae, 0.95), 2) if ae else None,
            "max_abs_ms": round(max(ae), 2) if ae else None,
            "signed_p50_ms": round(pct(errs, 0.50), 2) if errs else None,
            "p95_ci95_ms": ([None if ci_lo is None else round(ci_lo, 2),
                             None if ci_hi is None else round(ci_hi, 2)]),
            "p95_batch_min_ms": None if bp[0] is None else round(bp[0], 2),
            "p95_batch_mean_ms": None if bp[1] is None else round(bp[1], 2),
            "p95_batch_max_ms": None if bp[2] is None else round(bp[2], 2),
            "within20_est": round(sum(1 for x in ae if x <= 20) / max(1, len(ae)), 3)
            if ae else None,
            "within20_all": round(sum(1 for x in ae if x <= 20) / max(1, runs), 3),
            "within10_all": round(sum(1 for x in ae if x <= 10) / max(1, runs), 3),
            "trust": round(n_trust / max(1, runs), 3),
            "gate_ok": round(n_gate / max(1, runs), 3),
            "trusted_p95_abs_ms": round(pct(te, 0.95), 2) if te else None,
            "trusted_max_abs_ms": round(max(te), 2) if te else None,
            "false_trust": round(false_trust / max(1, runs), 3),
            "half_width_med_ms": (round(pct([r[4] * 1000 for r in rs
                                             if r[4] is not None], 0.5), 2)
                                  if any(r[4] is not None for r in rs) else None),
            "span_med_s": round(pct([r[5] for r in rs], 0.5), 3),
            "inliers_med": round(pct([r[6] for r in rs], 0.5), 1),
            "outlier_recall_mean": (round(statistics.mean(recalls), 3)
                                    if recalls else None),
            "outlier_recall_min": round(min(recalls), 3) if recalls else None,
            "false_positive_rate": round(statistics.mean(fps), 4) if fps else 0.0,
            "codes": codes,
        })
    return rows


def need_for(rows, key, limit_ms):
    for r in rows:
        v = r.get(key)
        if v is not None and v <= limit_ms:
            return r["budget"], r["span_med_s"]
    return None, None


def run_scenario(name, spec, seed, runs):
    budgets = spec.get("budgets", BUDGETS)
    recs = run_combo(
        spec["model"], seed, runs, budgets,
        display_s=spec.get("display_s", 0.0),
        start=spec.get("start", (6.0, 40.0)),
        tick_only=spec.get("tick_only", False),
        lag_s=spec.get("lag_s", 0.0),
        err_override=spec.get("err_override"),
        pair_only=spec.get("pair_only", False),
    )
    rows = summarize(recs, budgets, runs, ci_seed=seed % 9973)
    raw = {str(b): [[None if r[0] is None else round(r[0], 3), r[1], r[2], r[3],
                     None if r[4] is None else round(r[4] * 1000, 3),
                     round(r[5], 4), r[6],
                     None if r[7] is None else round(r[7], 3),
                     round(r[8], 4)]
                    for r in recs[b]]
           for b in budgets}
    return {"rows": rows, "raw": raw, "seed": seed, "runs": runs}, rows, budgets


def print_table(key, runs, rows):
    print(f"\n=== {key}  ({runs} 组) ===")
    print(f"{'读数':>4} {'覆盖':>5} {'p50ms':>7} {'p95ms':>7} {'p95CI':>13} "
          f"{'maxms':>7} {'≤20全':>6} {'可信':>5} {'Gate':>5} {'假信':>5} "
          f"{'批p95':>13} {'半宽ms':>7} {'剔除':>5}")
    for r in rows:
        ci = r["p95_ci95_ms"]
        cis = "-" if ci[0] is None else f"{ci[0]:.1f}-{ci[1]:.1f}"
        bp = r["p95_batch_mean_ms"]
        bps = "-" if bp is None else (f"{r['p95_batch_min_ms']:.1f}/"
                                      f"{bp:.1f}/{r['p95_batch_max_ms']:.1f}")
        hw = "-" if r["half_width_med_ms"] is None else f"{r['half_width_med_ms']:.2f}"
        rc = "-" if r["outlier_recall_mean"] is None else f"{r['outlier_recall_mean']:.3f}"
        p50 = "-" if r["p50_abs_ms"] is None else f"{r['p50_abs_ms']:.2f}"
        p95 = "-" if r["p95_abs_ms"] is None else f"{r['p95_abs_ms']:.2f}"
        mx = "-" if r["max_abs_ms"] is None else f"{r['max_abs_ms']:.2f}"
        print(f"{r['budget']:>4} {r['coverage']:>5.2f} {p50:>7} {p95:>7} "
              f"{cis:>13} {mx:>7} "
              f"{r['within20_all']:>6.2f} {r['trust']:>5.2f} {r['gate_ok']:>5.2f} "
              f"{r['false_trust']:>5.2f} {bps:>13} {hw:>7} {rc:>5}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-raw", action="store_true",
                    help="json 里不写逐 run 原始记录（体积大）")
    args = ap.parse_args()

    results = {}

    # ---------------- base：等间隔刷新假设，两个独立采样模型 × 三个种子
    for model in BASE_MODELS:
        for seed in SEEDS:
            key = f"{model}/{seed}"
            info, rows, _budgets = run_scenario(key, dict(model=model), seed, args.runs)
            results[key] = info
            print_table(key, args.runs, rows)
            print("  (剔除率/误杀见上表最后一列与 json；逐预算)")
            print("  错读剔除率 min=" + str(min(r["outlier_recall_min"] for r in rows
                                             if r["outlier_recall_min"] is not None))
                  + "  误杀率 max=" + f"{max(r['false_positive_rate'] for r in rows):.4f}")

    print("\n=== 交叉复核（base）：p95（CI 上界）跨模型/种子 ===")
    base_keys = [f"{m}/{s}" for m in BASE_MODELS for s in SEEDS]
    print(f"{'读数':>4} " + " ".join(f"{k:>20}" for k in base_keys) + "  spread")
    for i, b in enumerate(BUDGETS):
        cells = []
        vals = []
        for k in base_keys:
            r = results[k]["rows"][i]
            if r["p95_abs_ms"] is None:
                cells.append("-")
                continue
            hi = r["p95_ci95_ms"][1]
            cells.append(f"{r['p95_abs_ms']:.2f}({hi if hi is None else round(hi, 1)})")
            vals.append(r["p95_abs_ms"])
        if not vals:
            continue
        lo, hi2 = min(vals), max(vals)
        ratio = (hi2 / lo) if lo > 0 else float("inf")
        flag = "  >15%" if ratio > 1.15 else ""
        print(f"{b:>4} " + " ".join(f"{c:>20}" for c in cells)
              + f"  {ratio:.2f}x{flag}")

    print("\n=== 需求点（base） ===")
    for limit in (20, 10):
        print(f"可信 p95 进 ±{limit}ms:")
        for k in base_keys:
            b, span = need_for(results[k]["rows"], "trusted_p95_abs_ms", limit)
            print(f"  {k:<18} 读数 {b}  跨度 {span}s")

    # ---------------- irregular / feed：刷新方式与喂法
    print("\n\n############ 不规律刷新 / 不同喂法 ############")
    for name, spec in SCENARIOS.items():
        for seed in SEEDS:
            key = f"{name}/{seed}"
            info, rows, _budgets = run_scenario(key, spec, seed, args.runs)
            results[key] = info
            print_table(key, args.runs, rows)

    print("\n=== 场景交叉复核（同场景三种子） ===")
    for name in SCENARIOS:
        keys = [f"{name}/{s}" for s in SEEDS]
        budgets = SCENARIOS[name].get("budgets", BUDGETS)
        print(f"\n{name}")
        for i, b in enumerate(budgets):
            r0 = results[keys[0]]["rows"][i]
            if r0["p95_abs_ms"] is None:
                continue
            cells = []
            for k in keys:
                r = results[k]["rows"][i]
                cells.append("-" if r["p95_abs_ms"] is None
                             else f"{r['p95_abs_ms']:.2f}(trust {r['trust']:.2f})")
            print(f"  读数 {b:>3}  " + " / ".join(cells))

    if args.json:
        if args.no_raw:
            for info in results.values():
                info.pop("raw", None)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"runs": args.runs, "seeds": list(SEEDS),
                       "budgets": list(BUDGETS),
                       "tick_budgets": list(TICK_BUDGETS),
                       "pair_budgets": list(PAIR_BUDGETS),
                       "results": results}, fh, ensure_ascii=False, indent=1)
        print(f"\n已写入 {args.json}")


if __name__ == "__main__":
    main()
