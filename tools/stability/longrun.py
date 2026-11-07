"""等效 72 小时加速长跑。`python -m tools.stability.longrun --hours 72`

时间压缩原理：整块时间源换成 `VirtualClock`（见 clock.py）。引擎内层循环每次
sleep 0.002 秒，虚拟钟至少推进 `--sleep-step`（默认 0.1）秒，于是「60 秒倒计时」
只跑约 600 次迭代而不是 3 万次，72 小时等效时长压缩到几分钟。

采样口径：每 `--sample-every-min` 虚拟分钟采一次进程指标；每 6 次做一次深采样
（GC 存活对象 + tracemalloc 峰值）。第 2 小时后才用于算内存斜率，避开解释器
/ numpy / cv2 的预热上升。

产出（`tools/stability/_results/<run>/`）：
  metrics.csv       —— 时间序列（eq_hours 为横轴）
  summary.json      —— 斜率、回归、判据结论
  treemalloc_top.json —— tracemalloc 相对基线涨得最多的分配点
"""
import argparse
import csv
import gc
import json
import os
import statistics
import sys
import time
import tracemalloc

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import countdown, engine as engine_mod, pagewatch, records, retention  # noqa: E402
from core import scheduler, watchdog as watchdog_mod  # noqa: E402

from . import clock as clock_mod  # noqa: E402
from . import fakes, metrics, workload  # noqa: E402

RESULT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_results")

# ---- 判据阈值（写进报告，改这里就等于改判据）----
LEAK_MB_PER_HOUR = 0.5      # 预热后内存线性增长率上限
HANDLE_DELTA_MAX = 30       # 末值相对基线允许的句柄净增
THREAD_DELTA_MAX = 2        # 线程数净增
OPEN_FILE_DELTA_MAX = 5     # 已打开文件净增
CYCLE_DRIFT_MAX_PCT = 20.0  # 后段周期耗时相对基准窗口的劣化上限

# ---- 漂移判定的测量口径（2026-11-07 修订，理由见 _soak_cpu_recheck.md）----
# 旧口径（保留为对照）：前 10% 命中轮 vs 后 10% 命中轮的 CPU 中位数。
#   问题一：`GetProcessTimes` 的单轮读数被调度时钟量化成 15.625ms 一格，
#   94ms 的一轮只有 6 格；一格 = 16.7%，20% 的阈值就落在这格噪声里
#   （v5 的 -14.3% = 109.4→93.8，黑盒的 +19.9% = 78.2→93.8，都是 ±1 格）。
#   问题二：前 10% 里含着懒加载、首个 GC 周期、截图目录还没攒满（修剪开销
#   是阶跃的），起点不可复现，也不代表稳态。
# 新口径：
#   · 起点 = 进入稳态后 [BASELINE_H0, BASELINE_H1) 的**每小时均值**再取中位数；
#   · 终点 = 最后 6 个完整小时的均值再取中位数；
#   · 主指标 = 「单轮 CPU / 同期标定耗时」（见 metrics.calibrate_ms），
#     剥离 CPU 降频/睿频/大小核调度/机器负载对「CPU 秒」的污染。
# 为什么起点要等到 8h：截图目录在前 ~7h 从 0 张涨到上限 400 张，修剪开销是
# 阶跃式的，8h 之后才真正进入稳态；拿 0~2h 当起点必然虚低。
BASELINE_H0, BASELINE_H1 = 8.0, 24.0
TAIL_HOURS = 6



def _linfit(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, my


def _records_scale_probe(tmpdir):
    """台账「越来越大」到底慢多少：在 5 万行上测 load/stats。"""
    p = os.path.join(tmpdir, "scale_records.jsonl")
    row = ('{"ts": 1700000000.0, "t": "11-07 10:00:00", "name": "AK-12", '
           '"price": 1234.0, "result": "hit", "elapsed": 61.2, "shot": "a.jpg", '
           '"mode": "countdown", "quota": null, "verified": true}')
    with open(p, "w", encoding="utf-8") as f:
        for _ in range(50000):
            f.write(row + "\n")
    old = records.PATH
    records.PATH = p
    try:
        t0 = time.perf_counter()
        records.load(200)
        t_load = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        st = records.stats()
        t_stats = (time.perf_counter() - t0) * 1000
        hits = st["hits"]
    finally:
        records.PATH = old
        try:
            os.remove(p)
        except OSError:
            pass
    return {"rows": 50000, "load200_ms": round(t_load, 2),
            "stats_ms": round(t_stats, 2), "hits": hits,
            "bytes": 50000 * (len(row) + 1)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--sleep-step", type=float, default=0.1)
    ap.add_argument("--cycle-sec", type=float, default=60.0)
    ap.add_argument("--sample-every-min", type=float, default=10.0)
    ap.add_argument("--max-real-s", type=float, default=1800.0,
                    help="真机时钟保护：跑太久就停（避免 CI 里挂死）")
    ap.add_argument("--warmup-cycles", type=int, default=8,
                    help="先空跑几轮再取基线（避开懒加载/首次导入的句柄上升）")
    ap.add_argument("--cal-every", type=int, default=25,
                    help="每 N 轮跑一次固定工作量标定（剥离 CPU 频率/负载噪声）")
    ap.add_argument("--tracemalloc", action="store_true",
                    help="开 tracemalloc 归因（会拖慢 5-10 倍，只用于短跑探针）")
    ap.add_argument("--out", default="")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    outdir = args.out or os.path.join(RESULT_ROOT, f"longrun_{stamp}")
    os.makedirs(outdir, exist_ok=True)

    ws = fakes.TempWorkspace("longrun")
    workload.apply_workspace(ws)

    clock = clock_mod.VirtualClock(sleep_step=args.sleep_step)
    horizon = args.hours * 3600.0
    sample_gap = max(1.0, args.sample_every_min * 60.0)

    rows = []
    cycle_rows = []
    # tracemalloc 的分配追踪会给每个对象加栈，跑 72 小时会把耗时不真实地
    # 放大 5-10 倍。默认关；归因用 `--tracemalloc` 跑一段短的。
    if args.tracemalloc:
        tracemalloc.start(15)
    metrics.track_files()
    gc.collect()
    baseline = metrics.sample(deep=True)
    last_snap = tracemalloc.take_snapshot() if args.tracemalloc else None
    top_diffs = None

    real_start = time.perf_counter()
    next_sample = 0.0
    deep_tick = 0
    cycles = 0
    next_report = 6.0 * 3600.0

    with clock_mod.install([engine_mod, countdown, records, pagewatch,
                            scheduler, watchdog_mod], clock):
        # 预热：首批周期会触发 numpy/cv2/引擎子模块的懒加载，句柄和内存
        # 都会跳一次。先把这段吃掉，再取基线，否则「句柄增长」全是假阳性。
        for _ in range(max(0, args.warmup_cycles)):
            workload.run_cycle(clock)
            workload.idle_tick(clock, seconds=5.0)
        gc.collect()
        baseline = metrics.sample(deep=True)
        ht_base = metrics.handle_types()
        warm_hours = clock.perf_counter() / 3600.0
        cal_ms = metrics.calibrate_ms()

        while clock.perf_counter() - warm_hours * 3600 < horizon:
            if time.perf_counter() - real_start > args.max_real_s:
                print(f"[长跑] 真机时钟超过 {args.max_real_s}s，提前收尾"
                      f"（等效 {clock.perf_counter() / 3600:.1f}h）")
                break
            # 标定必须在被测窗口之外：它自己也要吃 CPU，算进单轮耗时会失真。
            # 每 cal_every 轮一次，占比 <0.2%；期间取最近一次标定值。
            if cycles % max(1, args.cal_every) == 0:
                cal_ms = metrics.calibrate_ms()
            cpu0 = metrics.cpu_seconds()
            res, eng = workload.run_cycle(clock, prepare_sec=args.cycle_sec)
            cycles += 1
            # 界面轮询：读列表 + 查统计（这是「越跑越慢」的高危路径）
            tick = workload.idle_tick(clock, seconds=5.0, records_limit=200)
            cpu1 = metrics.cpu_seconds()
            cache = records.cache_stats()
            cycle_rows.append({
                "cycle": cycles,
                "eq_h": round(clock.perf_counter() / 3600.0, 3),
                "real_ms": res["real_ms"],
                "cpu_ms": round((cpu1 - cpu0) * 1000, 2) if cpu1 > cpu0 > 0 else 0.0,
                "cal_ms": cal_ms,
                "cpu_norm": round(((cpu1 - cpu0) * 1000) / cal_ms, 4)
                if cpu1 > cpu0 > 0 and cal_ms else 0.0,
                "virt_s": res["virt_s"],
                "grabs": res["grabs"],
                "ocr": res["ocr_calls"],
                "records_bytes": tick["records_bytes"],
                "cache_elapsed": cache["elapsed_samples"],
                "cache_bytes": cache["bytes_est"],
                "outcome": res["outcome"],
            })
            if tick["real_ms"] > 200:
                print(f"[长跑] 警告：一次记录轮询耗时 {tick['real_ms']}ms（"
                      f"{tick['records_bytes']} 字节 / {tick['rows']} 行）")

            while clock.perf_counter() >= next_sample:
                deep = deep_tick % 6 == 5
                s = metrics.sample(deep=deep)
                s["eq_h"] = round(next_sample / 3600.0, 3)
                s["real_s"] = round(time.perf_counter() - real_start, 1)
                s["cycles"] = cycles
                s["records_bytes"] = tick["records_bytes"]
                nfiles, nbytes = _dir(ws.shots)
                s["shots_files"] = nfiles
                s["shots_bytes"] = round(nbytes / 1048576, 2)
                s["records_rows"] = tick["rows"]
                s["last_cycle_ms"] = res["real_ms"]
                rows.append(s)
                deep_tick += 1
                next_sample += sample_gap

                if clock.perf_counter() >= next_report:
                    next_report += 6.0 * 3600.0
                    if not args.quiet:
                        print(f"[长跑] 等效 {clock.perf_counter() / 3600:5.1f}h · "
                              f"真实 {s['real_s']}s · cycles={cycles} · "
                              f"private={s.get('private_mb')}MB · "
                              f"handles={s.get('handles')} · "
                              f"threads={s.get('py_threads')}/{s.get('proc_threads')} · "
                              f"records={s['records_bytes'] / 1024:.0f}KB")

        # 收尾：等几秒让后台线程/GC 落定，再采一次
        clock.advance(30)
        final = metrics.sample(deep=True)
        final["eq_h"] = round(clock.perf_counter() / 3600.0, 3)
        final["cycles"] = cycles
        # 句柄明细：总数在涨的话，至少要能说清是哪一类内核对象在涨。
        ht_before, ht_after = ht_base, metrics.handle_types()
        if args.tracemalloc:
            top_diffs = metrics.snapshot_diff(last_snap, top=10)
        scale = _records_scale_probe(ws.data)

    csv_path = os.path.join(outdir, "metrics.csv")
    _write_csv(csv_path, rows)
    _write_cycles_csv(os.path.join(outdir, "cycles.csv"), cycle_rows)
    hourly = _hourly(rows, cycle_rows)
    _write_hourly_csv(os.path.join(outdir, "hourly.csv"), hourly)

    summary = _summarize(rows, cycle_rows, baseline, final, scale, top_diffs,
                         ws, clock, args, warm_hours, hourly)
    summary["hourly"] = hourly
    summary["handle_types_baseline"] = ht_before
    summary["handle_types_final"] = ht_after
    summary["handle_types_diff"] = {
        str(k): ht_after.get(k, 0) - ht_before.get(k, 0)
        for k in sorted(set(ht_before) | set(ht_after))
        if ht_after.get(k, 0) - ht_before.get(k, 0)}

    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    if not args.quiet:
        v = summary["verdict"]
        print("\n===== 长跑结论 =====")
        print(f"等效时长：{summary['eq_hours']}h（cycles={cycles}，"
              f"真实 {summary['real_s']}s）")
        print(f"内存斜率：{summary['mem_slope_mb_per_h']} MB/h（阈值 "
              f"{LEAK_MB_PER_HOUR}）")
        print(f"句柄：{baseline.get('handles')} → {final.get('handles')}；"
              f"线程：{baseline.get('py_threads')} → {final.get('py_threads')}")
        print(f"周期耗时漂移（主指标·小时均值 [8h,24h)→最后6h，标定归一）："
              f"{summary['hourly_norm_drift_pct']}%"
              f"（{summary['hourly_norm_baseline']} → "
              f"{summary['hourly_norm_tail']} 比值）；基准窗口噪声底 "
              f"±{summary['hourly_noise_pct']}%")
        print(f"  对照·小时均值原始 CPU：{summary['hourly_cpu_drift_pct']}%"
              f"（{summary['hourly_cpu_baseline_ms']} → "
              f"{summary['hourly_cpu_tail_ms']}ms，单轮分辨率 15.625ms）")
        print(f"  对照·逐轮稳态窗口 [8,24h)→后10% 标定归一："
              f"{summary['cycle_norm_drift_pct']}%（{summary['cycle_norm_window']}"
              f" → {summary['cycle_norm_tail']}）")
        print(f"  对照·旧口径 CPU（前10%→后10%）："
              f"{summary['cycle_cpu_drift_pct']}%（{summary['cycle_cpu_head_ms']}"
              f" → {summary['cycle_cpu_tail_ms']}ms）；墙钟 "
              f"{summary['cycle_drift_pct']}%（仅参考）")
        print("\n每小时：eq_h  轮数  CPU均值/p50/p95  归一均值  cal  内存MB  句柄  "
              "GC2  台账KB  缓存项")
        for h in summary["hourly"]:
            print(f"  {h['eq_h']:>3}h {h['cycles']:>5}  "
                  f"{h['cpu_mean_ms']:>6}/{h['cpu_p50_ms']:>6}/"
                  f"{h['cpu_p95_ms']:<6} {h['norm_mean']:>7}  "
                  f"{h['cal_ms']:>4}  "
                  f"{h.get('private_mb','-')}  {h.get('handles','-')}  "
                  f"{h.get('gen2_total','-')}  {h['records_kb']}  "
                  f"{h['cache_items']}")
        for k, ok in v.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {k}")
        print(f"明细：{outdir}")
    return 0 if all(summary["verdict"].values()) else 1


def _write_cycles_csv(path, rows):
    """逐轮明细：漂移到底发生在第几轮、工作量和缓存是不是同时在变。"""
    keys = ["cycle", "eq_h", "real_ms", "cpu_ms", "cal_ms", "cpu_norm",
            "virt_s", "grabs", "ocr", "records_bytes", "cache_elapsed",
            "cache_bytes", "outcome"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in rows:
            w.writerow([r.get(k, "") for k in keys])


def _pct(vals, q):
    """线性插值分位数（q=0.5 即中位数）。空列表返回 None。"""
    if not vals:
        return None
    xs = sorted(vals)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _hourly(rows, cycle_rows):
    """按等效小时聚合：单轮 p50/p95、RSS、句柄、GC、日志大小、缓存条目。"""
    buckets = {}
    for c in cycle_rows:
        buckets.setdefault(int(c["eq_h"]), []).append(c)
    for s in rows:
        buckets.setdefault(int(s["eq_h"]), []).append(s)

    out = []
    for h in sorted(buckets):
        cs = [b for b in buckets[h] if "cycle" in b]
        ss = [b for b in buckets[h] if "cycle" not in b]
        hit = [c for c in cs if c["outcome"] == "hit"]
        row = {
            "eq_h": h,
            "cycles": len(cs),
            "cpu_p50_ms": round(_pct([c["cpu_ms"] for c in hit], .5) or 0, 2),
            "cpu_p95_ms": round(_pct([c["cpu_ms"] for c in hit], .95) or 0, 2),
            "real_p50_ms": round(_pct([c["real_ms"] for c in hit], .5) or 0, 2),
            "real_p95_ms": round(_pct([c["real_ms"] for c in hit], .95) or 0, 2),
            "norm_p50": round(_pct([c["cpu_norm"] for c in hit if c["cpu_norm"]], .5) or 0, 3),
            "norm_p95": round(_pct([c["cpu_norm"] for c in hit if c["cpu_norm"]], .95) or 0, 3),
            # 均值口径：单轮读数被 15.625ms 量化，均值/求和才是去量化的真值
            "cpu_mean_ms": round(sum(c["cpu_ms"] for c in hit) / len(hit), 2) if hit else 0,
            "norm_mean": round(sum(c["cpu_norm"] for c in hit if c["cpu_norm"])
                               / max(1, len([1 for c in hit if c["cpu_norm"]])), 4),
            "cal_ms": round(_pct([c["cal_ms"] for c in cs], .5) or 0, 3),
            "grabs_avg": round(sum(c["grabs"] for c in hit) / len(hit), 1) if hit else 0,
            "records_kb": round(max([c["records_bytes"] for c in cs] or [0]) / 1024, 1),
            "cache_items": max([c["cache_elapsed"] for c in cs] or [0]),
            "cache_kb": round(max([c["cache_bytes"] for c in cs] or [0]) / 1024, 1),
        }
        if ss:
            row.update({
                "private_mb": round(sum(s.get("private_mb") or 0 for s in ss) / len(ss), 2),
                "working_mb": round(max(s.get("working_set_mb") or 0 for s in ss), 2),
                "handles": ss[-1].get("handles"),
                "py_threads": ss[-1].get("py_threads"),
                "proc_threads": ss[-1].get("proc_threads"),
                "gen0_total": ss[-1].get("gen0_collections"),
                "gen1_total": ss[-1].get("gen1_collections"),
                "gen2_total": ss[-1].get("gen2_collections"),
                "shots_files": ss[-1].get("shots_files"),
                "shots_kb": round((ss[-1].get("shots_bytes") or 0) * 1024, 1),
            })
        out.append(row)
    return out


def _write_hourly_csv(path, hourly):
    keys = ["eq_h", "cycles", "cpu_p50_ms", "cpu_p95_ms", "cpu_mean_ms",
            "norm_p50", "norm_p95", "norm_mean",
            "real_p50_ms", "real_p95_ms", "cal_ms", "grabs_avg", "private_mb",
            "working_mb", "handles", "py_threads", "proc_threads", "gen0_total",
            "gen1_total", "gen2_total", "records_kb", "cache_items", "cache_kb",
            "shots_files", "shots_kb"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in hourly:
            w.writerow([r.get(k, "") for k in keys])


def _dir(path):
    try:
        names = os.listdir(path)
    except OSError:
        return 0, 0
    n = 0
    total = 0
    for name in names:
        p = os.path.join(path, name)
        try:
            if os.path.isfile(p):
                n += 1
                total += os.path.getsize(p)
        except OSError:
            pass
    return n, total


def _write_csv(path, rows):
    keys = ["eq_h", "real_s", "cycles", "private_mb", "working_set_mb",
            "handles", "gdi", "user", "proc_threads", "py_threads",
            "open_files", "cpu_s", "gc0", "gc1", "gc2", "gen0_collections",
            "gen1_collections", "gen2_collections", "objects",
            "traced_mb", "traced_peak_mb", "records_bytes", "records_rows",
            "shots_files", "shots_bytes", "last_cycle_ms"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in rows:
            w.writerow([r.get(k, "") for k in keys])


def _summarize(rows, cycle_rows, baseline, final, scale, top_diffs, ws, clock,
               args, warm_hours=0.0, hourly=None):
    hourly = hourly or []
    eq_hours = round((clock.perf_counter() - warm_hours * 3600) / 3600.0, 2)
    real_s = round(rows[-1]["real_s"], 1) if rows else 0.0

    # 预热 2 小时后才算斜率：numpy/cv2/日志缓冲在启动阶段本来就会涨
    warm = [r for r in rows if r["eq_h"] >= 2.0] or rows
    xs = [r["eq_h"] for r in warm]
    ys = [r.get("private_mb") or 0.0 for r in warm]
    slope, _ = _linfit(xs, ys) if len(xs) >= 3 else (0.0, 0.0)
    ws_ys = [r.get("working_set_mb") or 0.0 for r in warm]
    ws_slope, _ = _linfit(xs, ws_ys) if len(xs) >= 3 else (0.0, 0.0)

    base_h = baseline.get("handles", -1)
    fin_h = final.get("handles", -1)
    handle_delta = fin_h - base_h if base_h >= 0 and fin_h >= 0 else 999
    handle_peak = max([r.get("handles") or 0 for r in rows] or [0])

    thread_delta = final.get("py_threads", 0) - baseline.get("py_threads", 0)
    open_delta = final.get("open_files", 0) - baseline.get("open_files", 0)

    # ---- 漂移：三种口径一起算 ----
    # 1) 旧口径（保留作对照）：前 10% 命中轮 vs 后 10% 命中轮，原始 CPU 中位数。
    # 2) 中口径：稳态窗口内逐轮中位数 vs 后 10%（仍受 15.625ms 量化影响）。
    # 3) 主口径：每小时 CPU 均值（求和除以轮数，天然去量化）→
    #    [8h,24h) 的小时均值中位数 vs 最后 6 个完整小时的中位数；
    #    同时用「CPU/标定耗时」再做一遍，剥离机器快慢。
    hit = [c for c in cycle_rows if c["outcome"] == "hit"]
    times = [c["real_ms"] for c in hit]
    ctimes = [c.get("cpu_ms") or 0.0 for c in hit]
    norms = [c.get("cpu_norm") or 0.0 for c in hit if c.get("cpu_norm")]
    base_win = [c for c in hit if BASELINE_H0 <= c["eq_h"] < BASELINE_H1]
    if len(times) >= 20:
        k = max(5, len(times) // 10)
        head = statistics.median(times[:k])
        tail = statistics.median(times[-k:])
        drift = round((tail - head) / head * 100, 1) if head else 0.0
        chead = statistics.median(ctimes[:k])
        ctail = statistics.median(ctimes[-k:])
        cpu_drift = round((ctail - chead) / chead * 100, 1) if chead else 0.0
    else:
        head = tail = drift = 0.0
        chead = ctail = cpu_drift = 0.0

    if len(base_win) >= 5 and len(ctimes) >= 20:
        w_cpu = statistics.median([c["cpu_ms"] for c in base_win])
        w_norm = statistics.median([c["cpu_norm"] for c in base_win
                                    if c["cpu_norm"]])
        t_cpu = statistics.median(ctimes[-k:])
        t_norm = statistics.median([c["cpu_norm"] for c in hit[-k:]
                                    if c["cpu_norm"]])
        w_cpu_drift = round((t_cpu - w_cpu) / w_cpu * 100, 1) if w_cpu else 0.0
        norm_drift = round((t_norm - w_norm) / w_norm * 100, 1) if w_norm else 0.0
    else:
        w_cpu = w_norm = t_cpu = t_norm = 0.0
        w_cpu_drift = norm_drift = 0.0

    # 主口径：小时均值（只取轮数≥20 的完整小时，避免末尾半截小时）
    full_h = [h for h in hourly if h["cycles"] >= 20]
    base_h = [h for h in full_h if BASELINE_H0 <= h["eq_h"] < BASELINE_H1]
    tail_h = full_h[-TAIL_HOURS:]
    if len(base_h) >= 3 and len(tail_h) >= 2:
        hb_cpu = statistics.median([h["cpu_mean_ms"] for h in base_h])
        hb_norm = statistics.median([h["norm_mean"] for h in base_h])
        ht_cpu = statistics.median([h["cpu_mean_ms"] for h in tail_h])
        ht_norm = statistics.median([h["norm_mean"] for h in tail_h])
        hour_cpu_drift = round((ht_cpu - hb_cpu) / hb_cpu * 100, 1) if hb_cpu else 0.0
        hour_norm_drift = round((ht_norm - hb_norm) / hb_norm * 100, 1) if hb_norm else 0.0
        # 噪声底：基准窗口内小时均值的极差，用来解释「为什么容差必须 ≥20%」
        hs = [h["cpu_mean_ms"] for h in base_h]
        hour_noise = round((max(hs) - min(hs)) / hb_cpu * 100, 1) if hb_cpu else 0.0
    else:
        hb_cpu = hb_norm = ht_cpu = ht_norm = 0.0
        hour_cpu_drift = hour_norm_drift = hour_noise = 0.0
    # 主判定：有 8~24h 基准时用小时均值口径，否则退回逐轮稳态口径
    primary_drift = hour_norm_drift if hb_norm else norm_drift
    primary_basis = "hourly 8-24h" if hb_norm else "cycle 2-6h"

    # 最后 1 小时（而不是最后 10%）也要有一份：越靠后越接近「真挂机第 72 小时」。
    last_h = [c for c in hit if c["eq_h"] >= max(0.0, eq_hours - 1.0)]
    lh_norm = statistics.median([c["cpu_norm"] for c in last_h if c["cpu_norm"]]) \
        if last_h else 0.0
    last1h_drift = round((lh_norm - w_norm) / w_norm * 100, 1) if w_norm else 0.0

    nshots, shot_bytes = _dir(ws.shots)
    rec_bytes = os.path.getsize(records.PATH) if os.path.isfile(records.PATH) else 0
    verdict = {
        "内存增长 < 0.5MB/h": slope < LEAK_MB_PER_HOUR,
        "句柄回落到基线±30": handle_delta <= HANDLE_DELTA_MAX,
        "线程数收敛": thread_delta <= THREAD_DELTA_MAX,
        "已打开文件不增长": open_delta <= OPEN_FILE_DELTA_MAX,
        "周期耗时不劣化 >20%（标定归一，8~24h 基准）": primary_drift <= CYCLE_DRIFT_MAX_PCT,
        "截图目录受上限约束": nshots <= (retention.SHOTS_MAX_FILES
                                     + retention.SHOTS_PRUNE_EVERY + 2),
        "台账受轮转约束": rec_bytes <= 9 * 1024 * 1024,
    }

    return {
        "eq_hours": eq_hours,
        "requested_hours": args.hours,
        "cycles": len(cycle_rows),
        "hit_cycles": sum(1 for c in cycle_rows if c["outcome"] == "hit"),
        "aborted_cycles": sum(1 for c in cycle_rows if c["outcome"] != "hit"),
        "real_s": real_s,
        "sleep_step": args.sleep_step,
        "baseline": baseline,
        "final": final,
        "mem_slope_mb_per_h": round(slope, 3),
        "workingset_slope_mb_per_h": round(ws_slope, 3),
        "handle_baseline": base_h,
        "handle_final": fin_h,
        "handle_peak": handle_peak,
        "thread_baseline": baseline.get("py_threads"),
        "thread_final": final.get("py_threads"),
        "open_files_baseline": baseline.get("open_files"),
        "open_files_final": final.get("open_files"),
        "cycle_head_ms": round(head, 2),
        "cycle_tail_ms": round(tail, 2),
        "cycle_drift_pct": drift,
        "cycle_cpu_head_ms": round(chead, 3),
        "cycle_cpu_tail_ms": round(ctail, 3),
        # 旧口径保留为对照（与历史报告同一定义）
        "cycle_cpu_drift_pct": cpu_drift,
        # 新口径：稳态窗口 [8h,24h) 起点 + 最后 6 个完整小时终点（逐轮中位数）
        "primary_drift_pct": primary_drift,
        "primary_basis": primary_basis,
        "baseline_window_h": [BASELINE_H0, BASELINE_H1],
        "baseline_window_cycles": len(base_win),
        "cycle_cpu_window_ms": round(w_cpu, 3),
        "cycle_cpu_tail_ms_window": round(t_cpu, 3),
        "cycle_cpu_window_drift_pct": w_cpu_drift,
        # 主口径：每小时均值 → 中位数（去量化）
        "hourly_cpu_baseline_ms": round(hb_cpu, 2),
        "hourly_cpu_tail_ms": round(ht_cpu, 2),
        "hourly_cpu_drift_pct": hour_cpu_drift,
        "hourly_norm_baseline": round(hb_norm, 3),
        "hourly_norm_tail": round(ht_norm, 3),
        "hourly_norm_drift_pct": hour_norm_drift,
        "hourly_noise_pct": hour_noise,
        "hourly_baseline_hours": [h["eq_h"] for h in base_h],
        "hourly_tail_hours": [h["eq_h"] for h in tail_h],
        "cycle_norm_window": round(w_norm, 3),
        "cycle_norm_tail": round(t_norm, 3),
        "cycle_norm_drift_pct": norm_drift,
        "cycle_norm_last1h": round(lh_norm, 3),
        "cycle_norm_last1h_drift_pct": last1h_drift,
        "cycle_norm_median": round(statistics.median(norms), 3) if norms else 0,
        # 单位说明：cycle_norm_* 是「CPU 毫秒 / 标定毫秒」的比值，不是毫秒
        "cycle_norm_unit": "cpu_ms / cal_ms",
        "records_bytes": rec_bytes,
        "shots_files": nshots,
        "shots_bytes": round(shot_bytes / 1048576, 2),
        "records_scale_50k": scale,
        "tracemalloc_top": top_diffs,
        "verdict": verdict,
        "max_real_s": args.max_real_s,
        "temp_workspace": ws.root,
        "note": ("等效时长由虚拟时钟推进；cycles 是按 60 秒/轮折算的等效轮数，"
                 "不是真机 60 秒级断言。真机验证阶段另做。"),
    }


if __name__ == "__main__":
    sys.exit(main())
