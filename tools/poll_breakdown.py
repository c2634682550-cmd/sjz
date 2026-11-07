"""UI 轮询延迟分段归因（185ms 单次尖刺）。

两条被怀疑的「轮询路径」都测：

  http   —— 前端真实轮询：每 1s GET /api/scheduler + /api/guard
            （web/index.html startPolling）。服务端在 Handler 里分段。
  direct —— 长时报告 `_longrun_stability.md` §2 用的代理：每 poll_s 调一次
            `records.load(200)` + `records.stats()`。按读写内部再分段。
  coldscan —— 只测「统计缓存失效 → 全量重扫」一次要多久（20k 行台账）。

分段（每段单独计时，p50/p95/p99/max 输出）：
  sched_lock       Scheduler._lock 等待
  engine_lock      Engine._lock 等待（在 scheduler.status 里）
  cfg_save_lock    Config.load 的进程内 _save_lock 等待
  cfg_proc_lock    Config.load 的跨进程文件锁等待
  cfg_read         Config._load_locked（读 + 解析 config.json）
  cfg_cleanup      _cleanup_orphan_tmp / sweep_logs
  timesync         timesync.info()
  rec_thread_lock  records._lock 等待
  rec_file_lock    records.load/stats 的跨进程文件锁等待（含 open lock 文件）
  rec_read         _load_impl（倒读 200 条）
  rec_stats        _stats_impl（增量缓存命中；未命中会记 full_scan_rows）
  serialize        Handler._json 里 json.dumps 的耗时（= _json - _send）
  net_write        Handler._send（send_response/end_headers/wfile.write）
  gil_gap          独立 tick 线程在本次请求窗口内的最大调度间隔（GIL/调度争用）
  cpu              本次请求消耗的进程 CPU 时间（process_time 差分）

用法（全部在临时目录跑，不碰仓库 data/）：
    python -m tools.poll_breakdown http --polls 90 --poll-s 1 --save-every 3
    python -m tools.poll_breakdown direct --polls 60 --poll-s 1 --add-every 3
    python -m tools.poll_breakdown coldscan --rows 20000
"""
import argparse
import contextlib
import csv
import http.client
import json
import os
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.stability import fakes, workload                    # noqa: E402
from core import config as config_mod                          # noqa: E402
from core import records                                       # noqa: E402

# ---------------------------------------------------------------- 计时壳
TL = threading.local()


def seg():
    d = getattr(TL, "seg", None)
    if d is None:
        d = TL.seg = {}
    return d


def add_seg(name, dt):
    seg()[name] = seg().get(name, 0.0) + float(dt)


def reset_seg():
    TL.seg = {}
    TL.t0 = time.perf_counter()
    TL.cpu0 = time.process_time()


class TimedLock:
    """RLock 外壳：只记「等锁」时长，不改变语义。"""

    def __init__(self, inner, name):
        self._inner = inner
        self._name = name

    def __enter__(self):
        t = time.perf_counter()
        self._inner.acquire()
        add_seg(self._name, time.perf_counter() - t)
        return self

    def __exit__(self, *a):
        self._inner.release()

    def acquire(self, *a, **k):
        t = time.perf_counter()
        r = self._inner.acquire(*a, **k)
        add_seg(self._name, time.perf_counter() - t)
        return r

    def release(self):
        return self._inner.release()


@contextlib.contextmanager
def timed_ctx(name, orig, *a, **k):
    t = time.perf_counter()
    cm = orig(*a, **k)
    cm.__enter__()
    add_seg(name, time.perf_counter() - t)
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def _wrap(callable_name, obj, name=None):
    """把 obj.<callable_name> 包一层，只累积整段耗时。"""
    orig = getattr(obj, callable_name)
    key = name or callable_name

    def wrapper(*a, **k):
        t = time.perf_counter()
        try:
            return orig(*a, **k)
        finally:
            add_seg(key, time.perf_counter() - t)

    setattr(obj, callable_name, wrapper)
    return orig

# ---------------------------------------------------------------- 工作区
class Env:
    def __init__(self, tag, rows, shots=380):
        self.ws = fakes.TempWorkspace(f"pollbd_{tag}")
        workload.apply_workspace(self.ws)
        self.out = os.path.join(self.ws.root, "collect")
        os.makedirs(self.out, exist_ok=True)
        # config.json 也必须搬走：否则 Config.transaction() 会写用户真实配置
        config_mod.CONFIG_PATH = os.path.join(self.ws.data, "config.json")
        config_mod.DATA_DIR = self.ws.data
        self.rows = rows
        self._seed(rows)
        for i in range(shots):
            with open(os.path.join(self.ws.shots, f"s{i:04d}.png"), "wb") as f:
                f.write(b"\0" * 1200)

    def _seed(self, n):
        os.makedirs(os.path.dirname(records.PATH), exist_ok=True)
        now = time.time()
        with open(records.PATH, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({
                    "ts": round(now - (n - i) * 60, 3),
                    "name": f"seed-{i}", "price": 1000 + i % 500,
                    "result": "hit" if i % 3 == 0 else "miss",
                    "elapsed": 0.5, "verified": True,
                }, ensure_ascii=False) + "\n")

# ---------------------------------------------------------------- 采样器
SAMPLES = []
SAMPLE_LOCK = threading.Lock()


def record(kind, wall, cpu, extra):
    row = {"kind": kind, "wall_ms": round(wall * 1000, 3),
           "cpu_ms": round(cpu * 1000, 3)}
    for k, v in extra.items():
        if not isinstance(v, float):
            continue
        if k == "full_scan_rows":              # 行数不是耗时，不换算成 ms
            row[k] = int(v)
        else:
            row[k] = round(v * 1000, 3)
    with SAMPLE_LOCK:
        SAMPLES.append(row)


class GilWatch:
    """独立线程：sleep(2ms) 的实际超时，反映调度/GIL 恶化。"""

    def __init__(self):
        self.stop = threading.Event()
        self.last = time.perf_counter()
        self.gap = 0.0
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()
        return self

    def _loop(self):
        while not self.stop.is_set():
            time.sleep(0.002)
            now = time.perf_counter()
            self.gap = max(self.gap, now - self.last)
            self.last = now

    def peek(self):
        g, self.gap = self.gap, 0.0
        return g

    def close(self):
        self.stop.set()


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    idx = int(round((p / 100.0) * (len(vals) - 1)))
    idx = min(len(vals) - 1, max(0, idx))
    return vals[idx]

# ---------------------------------------------------------------- 仪表安装
def install():
    """所有 hook 都装在探针进程里；不改仓库源码。"""
    # records：锁等待 / 文件锁 / 读 / 统计 / 全扫行数
    orig_fg = records._file_guard

    @contextlib.contextmanager
    def fg(timeout=None):
        t = time.perf_counter()
        cm = orig_fg(timeout)
        cm.__enter__()
        add_seg("rec_file_lock", time.perf_counter() - t)
        try:
            yield
        finally:
            cm.__exit__(None, None, None)

    records._file_guard = fg
    records._lock = TimedLock(records._lock, "rec_thread_lock")
    _wrap("_load_impl", records, "rec_read")
    _wrap("_stats_impl", records, "rec_stats")
    _wrap("_tail_rows", records, "_tail_once")
    _wrap("_signature", records, "_sig_once")

    # 全扫行数（stats 缓存失效时才会调用 _iter_rows）
    orig_iter = records._iter_rows
    records._iter_rows = lambda p: _count_iter(orig_iter(p))

    # Config.load 内部分段
    orig_load = config_mod.Config.load
    orig_load_locked = config_mod.Config._load_locked
    orig_proc_lock = config_mod._proc_lock
    orig_cleanup = config_mod._cleanup_orphan_tmp
    orig_sweep = config_mod._retention.sweep_logs

    @contextlib.contextmanager
    def proc_lock(timeout=None):
        with timed_ctx("cfg_proc_lock", orig_proc_lock, timeout):
            yield

    @classmethod
    def load(cls):
        t = time.perf_counter()
        try:
            return orig_load()
        finally:
            add_seg("cfg_load_total", time.perf_counter() - t)

    @classmethod
    def load_locked(cls):
        t = time.perf_counter()
        try:
            return orig_load_locked()
        finally:
            add_seg("cfg_read", time.perf_counter() - t)

    def cleanup():
        t = time.perf_counter()
        try:
            return orig_cleanup()
        finally:
            add_seg("cfg_cleanup", time.perf_counter() - t)

    def sweep(*a, **k):
        t = time.perf_counter()
        try:
            return orig_sweep(*a, **k)
        finally:
            add_seg("cfg_cleanup", time.perf_counter() - t)

    config_mod.Config.load = load
    config_mod.Config._load_locked = load_locked
    config_mod._proc_lock = proc_lock
    config_mod._cleanup_orphan_tmp = cleanup
    config_mod._retention.sweep_logs = sweep
    config_mod._save_lock = TimedLock(config_mod._save_lock, "cfg_save_lock")

    # Config.load_cached（classmethod，单独包）
    orig_cached = config_mod.Config.load_cached

    def load_cached(cls, *a, **k):
        t = time.perf_counter()
        try:
            return orig_cached(*a, **k)
        finally:
            add_seg("cfg_cached", time.perf_counter() - t)

    config_mod.Config.load_cached = classmethod(load_cached)

    # records.snapshot（轮询新路径）
    _wrap("snapshot", records, "snap_total")
    _wrap("_stats_for_poll", records, "stats_poll")
    _wrap("_schedule_stats_rebuild", records, "rebuild_kick")

    # fsync（config 保存里两处）单独计时
    orig_fsync = os.fsync

    def fsync(fd):
        t = time.perf_counter()
        try:
            return orig_fsync(fd)
        finally:
            add_seg("cfg_fsync", time.perf_counter() - t)

    os.fsync = fsync


def _count_iter(gen):
    n = 0
    for r in gen:
        n += 1
        add_seg("full_scan_rows", 1)       # 用 float 累加，最后按整数看
        yield r


def install_server():
    from core import server as server_mod
    from core import scheduler as sched_mod
    from core import engine as engine_mod
    from core import timesync

    # Scheduler / Engine 锁
    server_mod._scheduler._lock = TimedLock(server_mod._scheduler._lock, "sched_lock")
    _wrap("status", server_mod._scheduler, "sched_status")
    eng = server_mod._scheduler._engine
    eng._lock = TimedLock(eng._lock, "engine_lock")
    _wrap("status", eng, "engine_status")
    _wrap("info", timesync, "timesync")

    H = server_mod.Handler
    orig_get = H._get
    orig_json = H._json
    orig_send = H._send

    def _get(self, path, query):
        t = time.perf_counter()
        try:
            return orig_get(self, path, query)
        finally:
            add_seg("route_%s" % path.strip("/").replace("/", "_"),
                    time.perf_counter() - t)

    def _json(self, obj, code=200):
        t = time.perf_counter()
        try:
            return orig_json(self, obj, code)
        finally:
            add_seg("json_total", time.perf_counter() - t)

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        t = time.perf_counter()
        try:
            return orig_send(self, code, body, ctype)
        finally:
            add_seg("net_write", time.perf_counter() - t)

    H._get = _get
    H._json = _json
    H._send = _send

    orig_do_get = H.do_GET
    WATCH = GilWatch().start()          # 全局一个：per-request 建线程会污染计时

    def do_GET(self):
        reset_seg()
        WATCH.gap = 0.0
        try:
            return orig_do_get(self)
        finally:
            wall = time.perf_counter() - TL.t0
            cpu = time.process_time() - TL.cpu0
            d = dict(TL.seg)
            d["gil_gap"] = WATCH.peek()
            record(self.path.split("?")[0], wall, cpu, d)

    H.do_GET = do_GET
    return server_mod

# ---------------------------------------------------------------- 场景
def run_http(args):
    env = Env("http", args.rows)
    install()
    server_mod = install_server()
    srv = server_mod.ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 和 serve() 里的同步预热一致：界面打开前快照已预热，首屏请求不该
    # 再全扫一次台账。预热要等完成信号，别用 sleep 赌时间。
    records.warm(200)
    assert records.wait_ready(10.0), "预热没完成"
    config_mod.Config.load_cached()

    stop = threading.Event()

    # 后台「真动作」：调度器的任务状态写盘 = 每轮结束时 Config.transaction()
    # （fsync ×2）。这是生产里真实发生的事，不是人造负载。
    def writer():
        i = 0
        while not stop.is_set():
            try:
                with config_mod.Config.transaction() as cfg:
                    cfg["pollbd_tick"] = {"i": i, "at": time.time()}
                i += 1
            except Exception as e:                  # noqa: BLE001
                print("[pollbd] 写配置失败：%s: %s" % (type(e).__name__, e), flush=True)
            stop.wait(args.save_every)

    if args.save_every > 0:
        threading.Thread(target=writer, daemon=True).start()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    for i in range(args.polls):
        paths = ["/api/scheduler", "/api/guard"]
        if i % 5 == 0:                     # 长时报告里 records 是 5s 一轮
            paths.append("/api/records")
        for path in paths:
            t = time.perf_counter()
            conn.request("GET", path)
            r = conn.getresponse()
            r.read()
            add_seg("client_wall", time.perf_counter() - t)
        time.sleep(args.poll_s)
    conn.close()
    stop.set()
    srv.shutdown()
    srv.server_close()
    return env

def run_direct(args):
    env = Env("direct", args.rows)
    install()
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            records.add(name=f"w{i}", price=100 + i, result="hit", elapsed=0.5)
            i += 1
            stop.wait(args.add_every)

    if args.add_every > 0:
        threading.Thread(target=writer, daemon=True).start()

    # 和长时采集器一致：先预热统计缓存，第一条轮询才不是「冷启动全扫」
    records.load(args.limit)
    records.stats()
    w = GilWatch().start()
    for _ in range(args.polls):
        reset_seg()
        TL.gil = w
        w.gap = 0.0                            # 只统计本次请求窗口内的调度间隙
        t0 = TL.t0
        cpu0 = TL.cpu0
        records.load(args.limit)
        records.stats()
        wall = time.perf_counter() - t0
        cpu = time.process_time() - cpu0
        d = dict(TL.seg)
        d["gil_gap"] = max(0.0, w.peek())
        record("poll", wall, cpu, d)
        time.sleep(args.poll_s)
    w.close()
    stop.set()
    return env


def run_snapshot(args):
    """长时报告 5s 一轮 records 代理路径的修复版：一次 snapshot(rows+stats)。"""
    env = Env("snap", args.rows)
    install()
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            records.add(name=f"w{i}", price=100 + i, result="hit", elapsed=0.5)
            i += 1
            stop.wait(args.add_every)

    if args.add_every > 0:
        threading.Thread(target=writer, daemon=True).start()

    records.warm(args.limit)                   # 首屏冷启动一次，不计入样本
    assert records.wait_ready(10.0), "预热没完成"
    w = GilWatch().start()
    for _ in range(args.polls):
        reset_seg()
        TL.gil = w
        w.gap = 0.0
        t0, cpu0 = TL.t0, TL.cpu0
        records.snapshot(args.limit)
        wall = time.perf_counter() - t0
        cpu = time.process_time() - cpu0
        d = dict(TL.seg)
        d["gil_gap"] = max(0.0, w.peek())
        record("poll", wall, cpu, d)
        time.sleep(args.poll_s)
    w.close()
    stop.set()
    return env


def run_coldscan(args):
    env = Env("cold", args.rows)
    install()
    t = time.perf_counter()
    records.stats()                                # 预热
    warm = (time.perf_counter() - t) * 1000
    print("[coldscan] 首次全扫 %.1f ms（%d 行）" % (warm, args.rows))
    for i in range(args.repeat):
        with records._lock._inner:                 # 强制下一次走全量
            records._cache.update(sig=None, acc=None)
        reset_seg()
        t = time.perf_counter()
        records.stats()
        ms = (time.perf_counter() - t) * 1000
        n = int(seg().get("full_scan_rows", 0))
        print("[coldscan] 缓存失效重扫 #%d：%.1f ms，扫 %d 行" % (i + 1, ms, n))
    return env

# ---------------------------------------------------------------- 报告
SEG_KEYS = [
    "sched_lock", "sched_status", "engine_lock", "engine_status",
    "cfg_load_total", "cfg_cached", "cfg_save_lock", "cfg_proc_lock",
    "cfg_read", "cfg_cleanup", "cfg_fsync", "timesync",
    "rec_thread_lock", "rec_file_lock", "rec_read", "_tail_once", "_sig_once",
    "rec_stats", "stats_poll", "snap_total", "rebuild_kick",
    "route_api_scheduler", "route_api_guard", "route_api_records",
    "client_wall", "json_total", "net_write", "serialize", "gil_gap",
]


def report(env):
    kinds = sorted({r["kind"] for r in SAMPLES})
    path = os.path.join(env.out, "poll_breakdown.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        cols = ["kind", "wall_ms", "cpu_ms"] + SEG_KEYS + ["full_scan_rows"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in SAMPLES:
            w.writerow({k: r.get(k, "") for k in cols})

    print("\n=== 每类请求的墙钟延迟（带载：本机 python 进程数见命令输出）===")
    for kind in kinds:
        walls = [r["wall_ms"] for r in SAMPLES if r["kind"] == kind]
        print("%-14s n=%-4d p50=%7.2f p95=%9.2f p99=%9.2f max=%9.2f"
              % (kind, len(walls), statistics.median(walls),
                 pct(walls, 95), pct(walls, 99), max(walls)))

    print("\n=== 分段耗时（取该路径 p99 最大的一条做样本，单位 ms）===")
    for kind in kinds:
        rows = [r for r in SAMPLES if r["kind"] == kind]
        if not rows:
            continue
        worst = max(rows, key=lambda r: r["wall_ms"])
        print("\n[%s] 最慢一次 wall=%.2f ms cpu=%.2f ms" %
              (kind, worst["wall_ms"], worst["cpu_ms"]))
        # sum 剩余：json_total 减去 net_write 就是序列化
        d = dict(worst)
        if d.get("json_total"):
            d["serialize"] = round(d["json_total"] - d.get("net_write", 0.0), 3)
        total = d["wall_ms"]
        for k in SEG_KEYS:
            v = d.get(k, 0.0)
            if v:
                print("  %-15s %8.2f ms  %5.1f%%" % (k, v, v / total * 100.0))
        print("  %-15s %8.2f ms  %5.1f%%" % ("(其余/未计入)", total - sum(
            d.get(k, 0.0) for k in SEG_KEYS if k != "serialize" and
            not (k == "json_total")), 0.0))
        print("  full_scan_rows=%d" % int(d.get("full_scan_rows", 0)))

        # 全量延迟分段统计
        print("  分段 p50/p95/p99/max:")
        for k in SEG_KEYS + ["full_scan_rows"]:
            vals = [r.get(k, 0.0) for r in rows]
            if not any(vals):
                continue
            print("    %-15s %7.2f %9.2f %9.2f %9.2f"
                  % (k, statistics.median(vals), pct(vals, 95),
                     pct(vals, 99), max(vals)))
    print("\n原始数据：%s" % path)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("http", "direct", "snapshot", "coldscan"):
        p = sub.add_parser(name)
        p.add_argument("--rows", type=int, default=20000)
        p.add_argument("--polls", type=int, default=60)
        p.add_argument("--poll-s", dest="poll_s", type=float, default=1.0)
        p.add_argument("--limit", type=int, default=200)
        p.add_argument("--save-every", dest="save_every", type=float, default=3.0)
        p.add_argument("--add-every", dest="add_every", type=float, default=3.0)
        p.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    if args.mode == "http":
        env = run_http(args)
    elif args.mode == "direct":
        env = run_direct(args)
    elif args.mode == "snapshot":
        env = run_snapshot(args)
    else:
        env = run_coldscan(args)
    report(env)


if __name__ == "__main__":
    main()
