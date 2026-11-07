"""长时连续挂机的资源漂移采集器（真实墙钟，不依赖游戏）。

为什么另起一个工具：`tools/stability/longrun.py` 用虚拟时钟把 72h 压成几分钟，
能验证逻辑稳定性，但**没有跨越小时级的真实时间序列**。这里用真实 sleep +
真实 `GetProcessTimes`，回答「连续挂机 8 小时会不会漂」。

两种采集段：
  · real       —— 真实连续 ≥60 分钟。每轮会话 60s 倒计时，真实墙钟。
  · compressed —— 压缩会话周期（prepare_s=1），跑 480 轮 = 8h 的会话数。
                  每个会话的写盘/截图/记账都是真动作，只有「等 60 秒」被
                  缩短；8h 外推按会话数换算，不伪造资源量。

指标：私有工作集、句柄、GDI/USER、线程（进程/Python）、累计 CPU、
主日志与台账体积、截图目录文件数、自愈次数、未捕获异常、单轮耗时。
数据全部写到独立临时目录（默认 %TEMP%\\dafors_longrun\\<tag>），不碰仓库 data/。

用法：
    python -m tools.longrun_collect run --mode real --minutes 62 --tag real62
    python -m tools.longrun_collect run --mode compressed --cycles 480 --tag comp480
    python -m tools.longrun_collect probe-gdi       # 非法显示器构造的 GDI 泄漏
    python -m tools.longrun_collect probe-screen    # 真实 Screen 建/抓/关
    python -m tools.longrun_collect probe-http      # HTTP 半开连接常驻
    python -m tools.longrun_collect probe-records   # 台账规模 vs 读取延迟
"""
import argparse
import csv
import gc
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.stability import fakes, metrics, workload  # noqa: E402
from core import engine as engine_mod, records, retention  # noqa: E402

# ---------------------------------------------------------------- 异常计数
_EXC = {"main": 0, "thread": 0}
_old_excepthook = sys.excepthook
_old_threadhook = getattr(threading, "excepthook", None)


def _install_hooks():
    def _main(exc_type, exc, tb):
        _EXC["main"] += 1
        _old_excepthook(exc_type, exc, tb)

    def _thread(args):
        _EXC["thread"] += 1
        try:
            print("[longrun] 线程未捕获异常：%s: %s"
                  % (getattr(args.exc_type, "__name__", args.exc_type), args.exc_value),
                  flush=True)
        except Exception:                       # noqa: BLE001
            pass
        if _old_threadhook is not None:
            _old_threadhook(args)

    sys.excepthook = _main
    if _old_threadhook is not None:
        threading.excepthook = _thread


# ---------------------------------------------------------------- 时钟替身
class RealClock:
    """和 VirtualClock 同接口，但 sleep 是真睡。"""

    sleep_step = 0.0

    def time(self):
        return time.time()

    def perf_counter(self):
        return time.perf_counter()

    def monotonic(self):
        return time.monotonic()

    def strftime(self, fmt, t=None):
        base = time.time() if t is None else t
        return time.strftime(fmt, time.localtime(base))

    def advance(self, seconds):
        if seconds and seconds > 0:
            time.sleep(float(seconds))

    def sleep(self, seconds):
        if seconds and seconds > 0:
            time.sleep(float(seconds))


# ---------------------------------------------------------------- 辅助
def _pct(a, b):
    return (a - b) / b * 100.0 if b else 0.0


def _median(vals):
    vals = sorted(vals)
    if not vals:
        return 0.0
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _slope(xs, ys):
    """最小二乘斜率 + R²。"""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return 0.0, 0.0
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    b = my - k * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (k * x + b)) ** 2 for x, y in zip(xs, ys))
    r2 = 0.0 if not ss_tot else 1.0 - ss_res / ss_tot
    return k, r2


def _extrapolate(xs, ys, hours=8.0, sat_ratio=0.15):
    """斜率 + 8h 外推，并识别「台阶饱和」：饱和时改用实测封顶值。

    为什么不能一律线性外推：工作集/私有提交在预热期一次性上涨后平台化
    （装载阶跃：Python 堆 arena、DLL 代码页被逐段触碰），对 65 分钟拟合
    出的斜率外推 8 小时，会凭空造出 16%~20% 的假增长。判据是「后 1/4 段
    增量 / 前 1/4 段增量」：< `sat_ratio` 视为平台化，8h 增量取
    「末值 - 首值」的实测封顶值，不再按小时放大。用增量比而不是斜率比，
    是因为斜率比会被「斜坡尾部刚好落在拟合窗口」误导（real65 就是这个坑）。
    """
    n = len(xs)
    if n < 8:
        return {"slope": 0.0, "r2": 0.0, "tail_slope": 0.0, "tail_over_head": None,
                "plateau_ratio": None, "bounded": False, "d8": 0.0,
                "pct8": 0.0, "d8_linear": 0.0}
    k, r2 = _slope(xs, ys)
    cut = max(1, int(n * 0.6))
    k_tail, _ = _slope(xs[cut:], ys[cut:])
    base = _median(ys[:max(1, n // 5)]) or 0.0
    tail_ratio = (abs(k_tail) / abs(k)) if k else None
    q = max(1, n // 4)
    head_d = ys[q] - ys[0]
    tail_d = ys[-1] - ys[-1 - q]
    bounded = (abs(tail_d) <= sat_ratio * abs(head_d)) if head_d else True
    d8_linear = k * hours
    d8 = (ys[-1] - ys[0]) if bounded else d8_linear
    return {"slope": k, "r2": r2, "tail_slope": k_tail,
            "tail_over_head": round(tail_ratio, 3) if tail_ratio is not None else None,
            "plateau_ratio": round(abs(tail_d) / abs(head_d), 3) if head_d else None,
            "bounded": bounded, "d8": d8, "pct8": _pct(base + d8, base),
            "d8_linear": d8_linear}


def _dir_stats(path):
    """目录文件数 + 总字节（扫一次，供采样线程用）。"""
    n = 0
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                if st.st_mode & 0o170000 == 0o100000:
                    n += 1
                    total += st.st_size
    except OSError:
        pass
    return n, total


# ---------------------------------------------------------------- 采集器
class Collector:
    def __init__(self, args):
        self.args = args
        self.ws = fakes.TempWorkspace(f"longrun_{args.tag}")
        workload.apply_workspace(self.ws)
        self.out = args.out or os.path.join(self.ws.root, "collect")
        os.makedirs(self.out, exist_ok=True)
        self.samples = []
        self.cycles = []
        self.polls = []
        self.stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._prune_ms = []
        self._prune_at = 0.0
        self.selfheal = {"grab": 0, "ocr": 0, "degrade": 0, "retry": 0}
        self._wrap_prune()
        self._seed_records()
        self._seed_shots()
        self.app_log = os.path.join(self.ws.root, "app.log")

    # -- 预置数据：让台账/截图目录一开始就是「挂机几天后」的规模
    def _seed_records(self):
        n = int(self.args.seed_records or 0)
        if n <= 0 or os.path.exists(records.PATH):
            return
        os.makedirs(os.path.dirname(records.PATH), exist_ok=True)
        now = time.time()
        with open(records.PATH, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({
                    "ts": now - (n - i) * 60, "name": "seed-%d" % i,
                    "price": 1000 + i % 500, "result": "hit", "elapsed": 0.5,
                    "verified": True,
                }, ensure_ascii=False) + "\n")
        t0 = time.perf_counter()
        records.stats()             # 预热增量缓存
        print("[longrun] 预置台账 %d 行，首扫 %.1fms"
              % (n, (time.perf_counter() - t0) * 1000), flush=True)

    def _seed_shots(self):
        n = int(self.args.seed_shots or 0)
        if n <= 0:
            return
        import numpy as np
        from core.vision import imwrite
        img = np.zeros((65, 240, 3), np.uint8)
        for i in range(n):
            imwrite(os.path.join(self.ws.shots, "seed_%04d.jpg" % i), img)
        print("[longrun] 预置截图 %d 张" % n, flush=True)

    def _wrap_prune(self):
        orig = retention.prune_dir
        state = self

        def timed(*a, **k):
            t0 = time.perf_counter()
            try:
                return orig(*a, **k)
            finally:
                dt = (time.perf_counter() - t0) * 1000
                with state._lock:
                    state._prune_ms.append(dt)
        retention.prune_dir = timed
        self._prune_orig = orig

    # -- 采样线程
    def _sampler(self):
        t0 = time.monotonic()
        while not self.stop_evt.is_set():
            row = metrics.sample()
            shots_n, shots_b = _dir_stats(self.ws.shots)
            row.update({
                "elapsed_min": round((time.monotonic() - t0) / 60.0, 2),
                "cycles_done": len(self.cycles),
                "records_bytes": os.path.getsize(records.PATH)
                if os.path.isfile(records.PATH) else 0,
                "shots_files": shots_n,
                "shots_bytes": shots_b,
                "app_log_bytes": os.path.getsize(self.app_log)
                if os.path.isfile(self.app_log) else 0,
                "selfheal_grab": self.selfheal["grab"],
                "selfheal_ocr": self.selfheal["ocr"],
                "selfheal_degrade": self.selfheal["degrade"],
                "uncaught_main": _EXC["main"],
                "uncaught_thread": _EXC["thread"],
                "prune_ms_last": round(self._prune_ms[-1], 2) if self._prune_ms else 0.0,
                "prune_count": len(self._prune_ms),
            })
            with self._lock:
                self.samples.append(row)
            self.stop_evt.wait(self.args.sample_s)

    # -- 界面轮询线程：每 poll_s 读一次台账（真实 UI 行为）
    def _poller(self):
        n = 0
        while not self.stop_evt.is_set():
            t0 = time.perf_counter()
            try:
                rows = records.load(self.args.poll_limit)
                st = records.stats()
                err = ""
            except Exception as e:              # noqa: BLE001
                rows, st, err = [], {}, "%s: %s" % (type(e).__name__, e)
            ms = (time.perf_counter() - t0) * 1000
            n += 1
            with self._lock:
                self.polls.append({
                    "ts": time.time(), "ms": round(ms, 3),
                    "rows": len(rows), "hits": (st or {}).get("hits", 0),
                    "records_bytes": os.path.getsize(records.PATH)
                    if os.path.isfile(records.PATH) else 0,
                    "error": err,
                })
            self.stop_evt.wait(self.args.poll_s)

    # -- 一轮会话
    def _one_cycle(self, clock, idx):
        prepare = float(self.args.prepare_s)
        screen = fakes.FakeScreen(fail_every=int(self.args.fail_grab_every or 0))
        target_pc = clock.perf_counter() + prepare
        ocr = fakes.FakeOcr(clock, target_pc,
                            fail_after=int(self.args.fail_ocr_after or 0) or None)
        cpu0 = metrics.cpu_seconds()
        t0 = time.perf_counter()
        res, eng = workload.run_cycle(clock, screen=screen, ocr=ocr,
                                      prepare_sec=prepare)
        real_ms = (time.perf_counter() - t0) * 1000
        cpu_ms = (metrics.cpu_seconds() - cpu0) * 1000
        selfheal = {"grab": 0, "ocr": 0, "degrade": 0, "retry": 0}
        try:
            for line in list(eng._log)[-120:]:
                msg = str(line.get("msg") if isinstance(line, dict) else line)
                if "截图恢复" in msg:
                    selfheal["grab"] += 1
                elif "OCR 恢复" in msg:
                    selfheal["ocr"] += 1
                elif "降级" in msg:
                    selfheal["degrade"] += 1
                elif "重试" in msg:
                    selfheal["retry"] += 1
        except Exception:                       # noqa: BLE001
            pass
        with self._lock:
            for k in selfheal:
                self.selfheal[k] += selfheal[k]
        try:
            with open(self.app_log, "a", encoding="utf-8") as f:
                f.write("%s cycle=%d outcome=%s real_ms=%.1f cpu_ms=%.1f\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), idx,
                           res.get("outcome"), real_ms, cpu_ms))
        except OSError:
            pass
        row = {
            "idx": idx, "ts": time.time(),
            "outcome": res.get("outcome"), "error": res.get("error") or "",
            "real_ms": round(real_ms, 1), "cpu_ms": round(cpu_ms, 1),
            "grabs": res.get("grabs"), "ocr_calls": res.get("ocr_calls"),
            "records_bytes": os.path.getsize(records.PATH)
            if os.path.isfile(records.PATH) else 0,
            "shots_files": _dir_stats(self.ws.shots)[0],
            "prune_ms": round(max(self._prune_ms[-3:] or [0]), 2),
            "selfheal": sum(selfheal.values()),
            "uncaught": _EXC["main"] + _EXC["thread"],
            "state": res.get("state"),
        }
        del eng
        return row

    def run(self):
        args = self.args
        clock = RealClock()
        _install_hooks()
        metrics.track_files()
        mode = args.mode
        if mode == "real":
            # 以 deadline 为准；这里的上界只防死循环（单轮实际 ~56s，留足余量）
            total_cycles = int(args.minutes * 60 / 50.0) + 5
        else:
            total_cycles = int(args.cycles)
        print("[longrun] mode=%s tag=%s cycles=%d prepare_s=%.1f sample_s=%.1f"
              % (mode, args.tag, total_cycles, args.prepare_s, args.sample_s),
              flush=True)
        sampler = threading.Thread(target=self._sampler, name="sample", daemon=True)
        poller = threading.Thread(target=self._poller, name="poll", daemon=True)
        sampler.start()
        poller.start()
        t_start = time.monotonic()
        deadline = t_start + (args.minutes * 60 if mode == "real" else 1e9)
        try:
            for i in range(1, total_cycles + 1):
                if mode == "real" and time.monotonic() >= deadline:
                    break
                if len(self.cycles) and time.monotonic() - t_start >= deadline:
                    break
                row = self._one_cycle(clock, i)
                self.cycles.append(row)
                if i % 10 == 0 or i <= 3:
                    print("[longrun] cycle %d/%d %s real=%.0fms cpu=%.0fms "
                          "handles=%d gdi=%d threads=%d priv=%.1fMB%s"
                          % (i, total_cycles, row["outcome"], row["real_ms"],
                             row["cpu_ms"], self.samples[-1]["handles"] if self.samples else -1,
                             self.samples[-1]["gdi"] if self.samples else -1,
                             self.samples[-1]["proc_threads"] if self.samples else -1,
                             self.samples[-1]["private_mb"] if self.samples else -1,
                             (" PRUNE" if row["prune_ms"] > 0 else "")), flush=True)
                if mode == "compressed":
                    # 压缩段：会话之间留 1s 真实间隔，避免退化成纯 CPU 压测
                    clock.sleep(1.0)
        finally:
            self.stop_evt.set()
            sampler.join(timeout=5)
            poller.join(timeout=5)
            self._dump()
        return 0

    def _dump(self):
        def _csv(path, rows):
            if not rows:
                return
            keys = sorted({k for r in rows for k in r})
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for r in rows:
                    w.writerow(r)
        _csv(os.path.join(self.out, "samples.csv"), self.samples)
        _csv(os.path.join(self.out, "cycles.csv"), self.cycles)
        _csv(os.path.join(self.out, "polls.csv"), self.polls)
        meta = {
            "argv": sys.argv, "tag": self.args.tag, "mode": self.args.mode,
            "prepare_s": self.args.prepare_s, "sample_s": self.args.sample_s,
            "cycles": len(self.cycles), "poll_count": len(self.polls),
            "selfheal": self.selfheal,
            "uncaught": dict(_EXC),
            "workspace": self.ws.root,
            "git_head": _git_head(),
        }
        with open(os.path.join(self.out, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        summary = self._summary()
        with open(os.path.join(self.out, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print("[longrun] 数据：%s" % self.out, flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    def _summary(self):
        """斜率 + 8h 外推。real 段按分钟，compressed 段按等效小时（60s/会话）。"""
        mode = self.args.mode
        # 进程级指标（内存/句柄/GDI/线程/CPU）用采样点的 x 轴：
        #   · real       ：真实小时数；
        #   · compressed ：按「已完成会话数 × 60s」折算成等效小时（一个会话
        #                  就是真机上的一分钟），不拿被压缩的墙钟当分母。
        if mode == "real":
            xs = [s["elapsed_min"] / 60.0 for s in self.samples]
        else:
            xs = [s.get("cycles_done", 0) * 60.0 / 3600.0 for s in self.samples]
        seq = self.samples
        keys = ["private_mb", "working_set_mb", "handles", "gdi", "user",
                "proc_threads", "py_threads", "cpu_s"]
        out = {"mode": mode, "hours_covered": round(xs[-1], 3) if xs else 0,
               "slope_per_h": {}, "delta_8h": {}, "pct_8h": {},
               "tail_slope_per_h": {}, "delta_8h_capped": {},
               "pct_8h_capped": {}, "bounded": {},
               "thresholds": {"mem_pct": 20, "cpu_pct": 15, "loop_pct": 10}}
        for k in keys:
            ys = [float(r.get(k) or 0) for r in seq]
            ex = _extrapolate(xs, ys)
            base = _median(ys[:max(1, len(ys) // 5)]) or 0.0
            out["slope_per_h"][k] = round(ex["slope"], 4)
            out["delta_8h"][k] = round(ex["d8_linear"], 3)
            out["pct_8h"][k] = round(_pct(base + ex["d8_linear"], base), 2)
            out["tail_slope_per_h"][k] = round(ex["tail_slope"], 4)
            out["delta_8h_capped"][k] = round(ex["d8"], 3)
            out["pct_8h_capped"][k] = round(_pct(base + ex["d8"], base), 2)
            out["bounded"][k] = ex["bounded"]
            out.setdefault("r2", {})[k] = round(ex["r2"], 3)
        # 单轮耗时 / CPU 漂移：前 20% vs 后 20%
        n = len(self.cycles)
        cut = max(1, n // 5)
        first = self.cycles[:cut]
        last = self.cycles[-cut:] if n else []
        out["cycle_cpu_ms_first"] = round(_median([c["cpu_ms"] for c in first]), 1)
        out["cycle_cpu_ms_last"] = round(_median([c["cpu_ms"] for c in last]), 1)
        # 单轮 CPU 必须按「本轮工作量」归一：倒计时循环每轮迭代次数取决于宿主
        # 计时器分辨率（15.6ms 时 ~5k 次抓图，1ms 时 ~21k 次），原始 CPU 会随
        # 宿主机状态漂 5 倍，不代表泄漏。这里同时保留原始值与 per-grab 值。
        norm = [c["cpu_ms"] / max(1, int(c["grabs"] or 0)) for c in self.cycles]
        ncut = max(1, n // 5)
        out["grabs_first"] = round(_median([c["grabs"] or 0 for c in first]), 0)
        out["grabs_last"] = round(_median([c["grabs"] or 0 for c in last]), 0)
        out["cpu_us_per_grab_first"] = round(_median(norm[:ncut]) * 1000, 1)
        out["cpu_us_per_grab_last"] = round(_median(norm[-ncut:]) * 1000, 1)
        out["cpu_drift_pct"] = round(
            _pct(out["cpu_us_per_grab_last"], out["cpu_us_per_grab_first"]), 2)
        out["cpu_drift_raw_pct"] = round(
            _pct(out["cycle_cpu_ms_last"], out["cycle_cpu_ms_first"]), 2)
        out["cycle_real_ms_first"] = round(_median([c["real_ms"] for c in first]), 1)
        out["cycle_real_ms_last"] = round(_median([c["real_ms"] for c in last]), 1)
        out["loop_drift_pct"] = round(
            _pct(out["cycle_real_ms_last"], out["cycle_real_ms_first"]), 2)
        out["poll_ms_median"] = round(_median([p["ms"] for p in self.polls]), 3)
        out["poll_ms_p95"] = round(sorted(p["ms"] for p in self.polls)[
            int(len(self.polls) * 0.95)] if self.polls else 0, 3)
        out["prune_ms_max"] = round(max(self._prune_ms or [0]), 2)
        out["prune_calls"] = len(self._prune_ms)
        out["selfheal"] = dict(self.selfheal)
        out["uncaught"] = dict(_EXC)
        out["verdict"] = _verdict(out)
        return out


def _verdict(s):
    bad = []
    if abs(s["pct_8h"].get("private_mb", 0)) > 20:
        bad.append("private_mb 8h 外推 %.1f%% > 20%%" % s["pct_8h"]["private_mb"])
    if s["delta_8h"].get("handles", 0) > 20:
        bad.append("handles 8h 外推 %+d > +20" % s["delta_8h"]["handles"])
    if abs(s["cpu_drift_pct"]) > 15:
        bad.append("单轮 CPU(每抓图归一) 漂移 %.1f%% > 15%%" % s["cpu_drift_pct"])
    if abs(s["loop_drift_pct"]) > 10:
        bad.append("单轮耗时漂移 %.1f%% > 10%%" % s["loop_drift_pct"])
    return {"pass": not bad, "reasons": bad}


def _git_head():
    try:
        import subprocess
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                       text=True).strip()
    except Exception:                            # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 探针
def probe_gdi(n=200):
    """非法显示器号构造 Screen：mss 已建 DC/Bitmap 后才抛 Value，
    看这两个 GDI 对象是否随失败次数线性增长（R02-L01）。"""
    from core.screen import Screen
    gc.collect()
    a = metrics.sample()
    fails = 0
    for _ in range(n):
        try:
            Screen(monitor=99)
        except ValueError:
            fails += 1
    gc.collect()
    b = metrics.sample()
    print(json.dumps({
        "probe": "gdi-invalid-monitor", "rounds": n, "fails": fails,
        "gdi_before": a["gdi"], "gdi_after": b["gdi"],
        "delta_gdi": b["gdi"] - a["gdi"], "delta_per_fail": round(
            (b["gdi"] - a["gdi"]) / fails, 2) if fails else 0,
        "handles_before": a["handles"], "handles_after": b["handles"],
        "private_before": a["private_mb"], "private_after": b["private_mb"],
    }, ensure_ascii=False, indent=2))


def probe_screen(n=200, monitor=1):
    """真实 Screen：建 → 抓 3 次 → 关。看 GDI/句柄是否回到基线。"""
    from core.screen import Screen
    gc.collect()
    a = metrics.sample()
    t0 = time.perf_counter()
    for _ in range(n):
        s = Screen(monitor)
        for _ in range(3):
            s.grab((10, 10, 200, 100))
        s.close()
        del s
    gc.collect()
    b = metrics.sample()
    print(json.dumps({
        "probe": "screen-create-grab-close", "rounds": n,
        "ms_per_round": round((time.perf_counter() - t0) * 1000 / n, 2),
        "gdi_before": a["gdi"], "gdi_after": b["gdi"],
        "handles_before": a["handles"], "handles_after": b["handles"],
        "private_before": a["private_mb"], "private_after": b["private_mb"],
    }, ensure_ascii=False, indent=2))


def probe_http(n=20, wait_s=45, port=0, no_timeout=False):
    """HTTP Keep-Alive 半开连接：客户端拿到响应后不关闭（模拟浏览器崩溃）。
    看服务端线程/句柄是否按连接常驻；修复后应在一个超时窗口内回落。"""
    import http.server
    from core import server

    class Probe(http.server.BaseHTTPRequestHandler):
        protocol_version = getattr(server.Handler, "protocol_version", "HTTP/1.1")
        # --no-timeout 复现修复前（无读超时）的基线；默认跟随修复后的 Handler
        timeout = None if no_timeout else getattr(server.Handler, "timeout", None)

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Probe)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    time.sleep(0.3)
    gc.collect()
    base = metrics.sample()
    socks = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        s.recv(4096)                    # 读到响应，然后**故意不关**
        socks.append(s)
    time.sleep(1.0)
    hold = metrics.sample()
    time.sleep(wait_s)
    after = metrics.sample()
    st = {"probe": "http-halfopen", "conns": n, "wait_s": wait_s,
          "handler_timeout": Probe.timeout,
          "threads_base": base["proc_threads"], "threads_hold": hold["proc_threads"],
          "threads_after": after["proc_threads"],
          "handles_base": base["handles"], "handles_hold": hold["handles"],
          "handles_after": after["handles"],
          "delta_per_conn": round((hold["handles"] - base["handles"]) / n, 2)}
    for s in socks:
        try:
            s.close()
        except OSError:
            pass
    time.sleep(1.5)
    end = metrics.sample()
    st["threads_released"] = end["proc_threads"]
    st["handles_released"] = end["handles"]
    httpd.shutdown()
    httpd.server_close()
    print(json.dumps(st, ensure_ascii=False, indent=2))


def probe_win32(n=2000):
    """OpenProcess/EnumWindows 类调用：句柄必须回到基线（64 位句柄泄漏点）。"""
    from core import input as inp
    gc.collect()
    hwnd = inp.Mouse.foreground()
    a = metrics.sample()
    t0 = time.perf_counter()
    ok = 0
    for _ in range(n):
        try:
            inp.process_integrity(hwnd)
            inp.find_game_window()
            inp.game_integrity()
            ok += 1
        except Exception:                       # noqa: BLE001
            pass
    time.sleep(0.5)
    gc.collect()
    b = metrics.sample()
    print(json.dumps({
        "probe": "win32-process-handles", "rounds": n, "ok": ok,
        "ms_per_round": round((time.perf_counter() - t0) * 1000 / n, 3),
        "handles_before": a["handles"], "handles_after": b["handles"],
        "delta_handles": b["handles"] - a["handles"],
        "gdi_before": a["gdi"], "gdi_after": b["gdi"],
        "private_before": a["private_mb"], "private_after": b["private_mb"],
    }, ensure_ascii=False, indent=2))


def probe_timer(seconds=30):
    """宿主计时器分辨率：解释「原始单轮 CPU 会随 15.6ms/1ms 抖动 5 倍」。"""
    import ctypes
    nt = ctypes.windll.ntdll
    mn, mx, cur = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
    rows = []
    for _ in range(max(1, int(seconds))):
        nt.NtQueryTimerResolution(ctypes.byref(mn), ctypes.byref(mx), ctypes.byref(cur))
        t = time.perf_counter()
        time.sleep(0.002)
        dt = (time.perf_counter() - t) * 1000
        rows.append({"timer_ms": round(cur.value / 1e7, 4), "sleep2ms_actual_ms": round(dt, 2)})
        time.sleep(0.5)
    res = sorted({r["timer_ms"] for r in rows})
    dts = [r["sleep2ms_actual_ms"] for r in rows]
    print(json.dumps({
        "probe": "timer-resolution", "samples": rows,
        "timer_ms_values": res,
        "sleep2ms_actual_min_med_max": [min(dts), sorted(dts)[len(dts) // 2], max(dts)],
    }, ensure_ascii=False, indent=2))


def _seed_ledger(path, n):
    import json as _json
    now = time.time()
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(_json.dumps({
                "ts": now - (n - i) * 60, "name": "seed-%d" % i,
                "price": 1000 + i % 500, "result": "hit", "elapsed": 0.5,
                "verified": True,
            }, ensure_ascii=False) + "\n")


def probe_records(sizes=(5000, 50000, 200000), rounds=10, poll_limit=200):
    """台账规模 vs `load(200)`/`stats()` 延迟。真尾读应当与 N 无关。"""
    import tempfile
    from core import records as R
    R._cache["acc"] = None
    R._cache["sig"] = None
    rows = []
    for n in sizes:
        d = tempfile.mkdtemp(prefix="dafors_rec_probe_")
        p = os.path.join(d, "records.jsonl")
        _seed_ledger(p, n)
        R.PATH = p
        R._cache["acc"] = None
        R._cache["sig"] = None
        t0 = time.perf_counter()
        R.stats()                           # 首次全量（冷缓存）
        t_cold = (time.perf_counter() - t0) * 1000
        lt, st = [], []
        for _ in range(rounds):
            t0 = time.perf_counter()
            R.load(poll_limit)
            lt.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            R.stats()
            st.append((time.perf_counter() - t0) * 1000)
        rows.append({"lines": n, "bytes": os.path.getsize(p),
                     "load_ms_median": round(sorted(lt)[len(lt) // 2], 3),
                     "stats_ms_median": round(sorted(st)[len(st) // 2], 3),
                     "stats_ms_cold": round(t_cold, 2)})
    print(json.dumps({"probe": "records-tail", "rows": rows}, ensure_ascii=False,
                     indent=2))


# ---------------------------------------------------------------- 工作集构成
def _ws_split():
    """把当前工作集拆成「常驻私有页 / 常驻镜像页 / 常驻映射页」。

    `WorkingSetSize` 只有一个总数：51MB 里到底是 Python 堆的常驻页，还是
    DLL 代码页、mmap 数据页，决定了「稳态 +16.2%」是真实增长还是装载阶跃。
    按 VirtualQueryEx 的 Region Type 分类，再用 QueryWorkingSetEx 判断每页
    是否常驻（Flags bit0 = Valid），逐类累加。
    """
    import ctypes
    from ctypes import wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ps = ctypes.WinDLL("psapi", use_last_error=True)

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p),
                    ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("a1", wt.DWORD),
                    ("RegionSize", ctypes.c_size_t),
                    ("State", wt.DWORD), ("Protect", wt.DWORD),
                    ("Type", wt.DWORD), ("a2", wt.DWORD)]

    class WSI(ctypes.Structure):
        _fields_ = [("VirtualAddress", ctypes.c_void_p), ("Flags", ctypes.c_uint64)]

    k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                   ctypes.POINTER(MBI), ctypes.c_size_t]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    ps.QueryWorkingSetEx.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
    ps.QueryWorkingSetEx.restype = wt.BOOL

    PAGE, BATCH, LIMIT = 4096, 8192, 0x7FFFFFFFFFFF
    MEM_COMMIT, MEM_MAPPED, MEM_IMAGE = 0x1000, 0x40000, 0x1000000
    out = {"resident_private_mb": 0.0, "resident_image_mb": 0.0,
           "resident_mapped_mb": 0.0}
    h = k32.GetCurrentProcess()
    mbi = MBI()
    buf = (WSI * BATCH)()
    addr = 0x10000
    while addr < LIMIT:
        if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                              ctypes.sizeof(mbi)) == 0:
            break
        base, size = int(mbi.BaseAddress or 0), int(mbi.RegionSize or 0)
        if not size:
            break
        if int(mbi.State) == MEM_COMMIT:
            kind = int(mbi.Type)
            key = ("resident_image_mb" if kind == MEM_IMAGE else
                   "resident_mapped_mb" if kind == MEM_MAPPED else
                   "resident_private_mb")
            off = 0
            while off < size:
                n = min(BATCH, (size - off) // PAGE)
                if n <= 0:
                    break
                for i in range(n):
                    buf[i].VirtualAddress = ctypes.c_void_p(base + off + i * PAGE)
                    buf[i].Flags = 0
                if ps.QueryWorkingSetEx(h, ctypes.byref(buf), n * ctypes.sizeof(WSI)):
                    hit = sum(1 for i in range(n) if buf[i].Flags & 1)
                    out[key] += hit * PAGE / 1048576.0
                off += n * PAGE
        addr = base + size
    for k in list(out):
        out[k] = round(out[k], 2)
    out["resident_total_mb"] = round(
        out["resident_private_mb"] + out["resident_image_mb"]
        + out["resident_mapped_mb"], 2)
    return out


def _head_tail_ratio(rows, key):
    """前后半段的「每会话增量」之比：<0.05 说明已平台化，不是线性泄漏。"""
    n = len(rows)
    half = n // 2
    head = ((rows[half][key] - rows[0][key])
            / max(1, rows[half]["cycle"] - rows[0]["cycle"]))
    tail = ((rows[-1][key] - rows[half][key])
            / max(1, rows[-1]["cycle"] - rows[half]["cycle"]))
    return head, tail, (tail / head if head else None)


def _ws_snap():
    s = metrics.sample()
    d = {"ws_mb": s["working_set_mb"], "private_mb": s["private_mb"]}
    d.update(_ws_split())
    return d


def probe_wssplit(cycles=150, prepare_s=1.0, every=15, max_s=300.0):
    """复现工作集增长并拆分归属：私有常驻堆 vs DLL 镜像页 vs 映射页。

    同时记录逐段斜率，用来区分「线性泄漏」（斜率不衰减）与「一次性装载
    阶跃」（前段陡、后段平）。
    """
    ns = argparse.Namespace(tag="wssplit", out="", seed_records=20000,
                            seed_shots=380, prepare_s=prepare_s,
                            fail_grab_every=0, fail_ocr_after=0,
                            sample_s=0, poll_s=0, poll_limit=200)
    c = Collector(ns)
    clock = RealClock()
    rows = [{"cycle": 0, "s": 0.0, **_ws_snap()}]
    t0 = time.monotonic()
    stopped_early = False
    for i in range(1, int(cycles) + 1):
        if time.monotonic() - t0 >= max_s:
            stopped_early = True
            break
        c._one_cycle(clock, i)
        if i % every == 0 or i == int(cycles):
            rows.append({"cycle": i, "s": round(time.monotonic() - t0, 1),
                         **_ws_snap()})
    res = {"probe": "ws-split", "cycles_run": rows[-1]["cycle"],
           "stopped_early": stopped_early, "rows": rows}
    for k in ("ws_mb", "private_mb", "resident_private_mb",
              "resident_image_mb", "resident_mapped_mb"):
        if len(rows) >= 3:
            head, tail, ratio = _head_tail_ratio(rows, k)
            res["per_cycle_mb_" + k] = {
                "first_half": round(head, 5), "second_half": round(tail, 5),
                "tail_over_head": round(ratio, 3) if ratio is not None else None}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["real", "compressed"], default="real")
    r.add_argument("--minutes", type=float, default=62.0)
    r.add_argument("--cycles", type=int, default=480)
    r.add_argument("--prepare-s", type=float, default=60.0)
    r.add_argument("--sample-s", type=float, default=30.0)
    r.add_argument("--poll-s", type=float, default=5.0)
    r.add_argument("--poll-limit", type=int, default=200)
    r.add_argument("--tag", default="longrun")
    r.add_argument("--out", default="")
    r.add_argument("--seed-records", type=int, default=20000)
    r.add_argument("--seed-shots", type=int, default=380)
    r.add_argument("--fail-grab-every", type=int, default=0)
    r.add_argument("--fail-ocr-after", type=int, default=0)
    g = sub.add_parser("probe-gdi")
    g.add_argument("--n", type=int, default=200)
    s = sub.add_parser("probe-screen")
    s.add_argument("--n", type=int, default=200)
    s.add_argument("--monitor", type=int, default=1)
    h = sub.add_parser("probe-http")
    h.add_argument("--n", type=int, default=20)
    h.add_argument("--wait-s", type=float, default=45.0)
    h.add_argument("--port", type=int, default=0)
    h.add_argument("--no-timeout", action="store_true")
    d = sub.add_parser("probe-records")
    d.add_argument("--rounds", type=int, default=10)
    w = sub.add_parser("probe-win32")
    w.add_argument("--n", type=int, default=2000)
    tm = sub.add_parser("probe-timer")
    tm.add_argument("--seconds", type=int, default=30)
    ws = sub.add_parser("probe-wssplit")
    ws.add_argument("--cycles", type=int, default=150)
    ws.add_argument("--prepare-s", type=float, default=1.0)
    ws.add_argument("--every", type=int, default=15)
    ws.add_argument("--max-s", type=float, default=300.0)
    a = ap.parse_args()
    if a.cmd == "run":
        return Collector(a).run()
    if a.cmd == "probe-gdi":
        return probe_gdi(a.n)
    if a.cmd == "probe-screen":
        return probe_screen(a.n, a.monitor)
    if a.cmd == "probe-http":
        return probe_http(a.n, a.wait_s, a.port, a.no_timeout)
    if a.cmd == "probe-records":
        return probe_records(rounds=a.rounds)
    if a.cmd == "probe-win32":
        return probe_win32(a.n)
    if a.cmd == "probe-timer":
        return probe_timer(a.seconds)
    if a.cmd == "probe-wssplit":
        probe_wssplit(a.cycles, a.prepare_s, a.every, a.max_s)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
