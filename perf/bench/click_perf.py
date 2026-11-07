"""点击延迟专项基准（只在 perf/base 冻结快照上跑，不碰 core/）。

用法（在 dafors 根目录，用项目 venv）：
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py                    # 默认 = 冒烟，不注入
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py --smoke            # 同上
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py --mode baseline --enable-physical
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py --mode audit --enable-physical
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py --mode burst --enable-physical
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py --mode drift --n 10000 --enable-physical
  .venv\\Scripts\\python.exe perf\\bench\\click_perf.py --mode all --enable-physical

物理输入闸门（缺任一条都不注入，只打印「未启用物理注入，基准不会真的点击」）：
  1) 显式 --enable-physical；
  2) driver_status() 通过（ok=True）；
  3) 终端交互确认（非交互 stdin 或未输入 YES → 退出码 2）。
  默认档 / --smoke 只做几何与时序计算，不初始化 Mouse、不发任何注入事件。

安全设计：
  · 只点 WindowFromPoint 判定为桌面的安全落点（Progman/WorkerW/SysListView32）；
  · 每次点击后按生产路径还原光标并回读确认；
  · 不建可见窗口、不抢前台、驱动 filter 保持全 0；
  · drift 用「停靠模式」：先移动一次到安全落点，之后只发 down/up，
    每轮检查光标是否被用户挪走（挪走就重停靠，超过 3 次直接中止）。
  · driver_status() 会打开驱动 context；本进程退出时可能卡在驱动 native
    teardown（任务管理器里 1 线程 / 0 CPU，taskkill 也收不掉）。这是驱动侧
    已知问题，不要反复尝试终止，也不要为它卸载驱动 —— 留给重启解决。

输出字段口径（`setup` / 顶层 `physical`）：
  · filters 有两种形状：根目录 core/interception.py 的新结构是状态字典
    {"expected","verified","values"}；本脚本实际 import 的 perf/base 冻结
    副本仍返回旧形状（槽位→数值）。两种都支持，数值全 0 才允许继续。
  · filters_all_zero：True=已回读且全 0；False=已回读但有非 0 槽（跑基准
    前直接中止，不会写出结果）；None=未回读或部分槽位读失败（值未知，
    不可当 False 用，也不代表没验证过）。
  · physical.injected / injections / clicks / target_title：本次到底有没有
    动过真鼠标、发了多少条 stroke、点了多少次、目标窗口是谁 —— 回看日志即可判断。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import gc
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(os.path.dirname(HERE), "base")
OUT = os.path.join(os.path.dirname(HERE), "out")
sys.path.insert(0, BASE)

import core.input as ci                                   # noqa: E402
from core import interception as ic                      # noqa: E402

assert os.path.dirname(ci.__file__).startswith(BASE), ci.__file__

if os.environ.get("PERF_DUMP_TIMEOUT"):
    import faulthandler
    faulthandler.dump_traceback_later(float(os.environ["PERF_DUMP_TIMEOUT"]),
                                      exit=True)


def log(*a):
    print(*a, flush=True)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
SAFE_CLASSES = ("Progman", "WorkerW", "SysListView32", "LockScreenBackGround")

user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPCWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR, ctypes.c_int]

_qpc_freq = ctypes.c_longlong()
kernel32.QueryPerformanceFrequency(ctypes.byref(_qpc_freq))


def qpc():
    v = ctypes.c_longlong()
    kernel32.QueryPerformanceCounter(ctypes.byref(v))
    return v.value


def qms(t):
    return t * 1000.0 / (_qpc_freq.value or 1)


def cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def point_class(pt):
    h = user32.WindowFromPoint(wt.POINT(*pt))
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def find_safe_point():
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    sx = max(40, vw // 12)
    sy = max(40, vh // 8)
    for y in range(vy + 80, vy + vh - 80, sy):
        for x in range(vx + 80, vx + vw - 80, sx):
            cls = point_class((x, y))
            if cls in SAFE_CLASSES:
                return (x, y), cls
    return None, ""


def pct(data, q):
    if not data:
        return None
    d = sorted(data)
    return round(d[min(len(d) - 1, max(0, int(round(q * (len(d) - 1)))))] * 1.0, 3)


def summarize(values, scale=1.0):
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return {"n": 0}
    return {"n": len(vals),
            "mean": round(statistics.fmean(vals) * scale, 3),
            "p50": round(statistics.median(vals) * scale, 3),
            "p95": pct([v * scale for v in vals], 0.95),
            "max": round(max(vals) * scale, 3),
            "min": round(min(vals) * scale, 3),
            "stdev": round(statistics.pstdev(vals) * scale, 3)}


# ---------------------------------------------------------------- 计数代理
_COUNTED = ("GetCursorPos", "SetCursorPos", "GetForegroundWindow",
            "GetSystemMetrics", "WindowFromPoint")


_ORIGINALS = {}


class _U32Proxy:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        fn = getattr(self._real, name)
        if name not in _COUNTED:
            return fn

        def wrapper(*a, **kw):
            ci._PERF_COUNTERS[name] = ci._PERF_COUNTERS.get(name, 0) + 1
            return fn(*a, **kw)
        return wrapper


def install_counters():
    counters = {}
    ci._PERF_COUNTERS = counters
    ci._user32 = _U32Proxy(user32)

    cls = ic.Interception
    _ORIGINALS["open"] = cls._open
    _ORIGINALS["send"] = cls.send_mouse
    _ORIGINALS["dev"] = cls.mouse_device

    def counted_open(self):
        counters["driver_open"] = counters.get("driver_open", 0) + 1
        return _ORIGINALS["open"](self)

    def counted_send(self, *a, **kw):
        counters["driver_send"] = counters.get("driver_send", 0) + 1
        return _ORIGINALS["send"](self, *a, **kw)

    def counted_dev(self, *a, **kw):
        counters["mouse_device"] = counters.get("mouse_device", 0) + 1
        return _ORIGINALS["dev"](self, *a, **kw)

    cls._open, cls.send_mouse, cls.mouse_device = counted_open, counted_send, counted_dev
    return counters


def uninstall_counters():
    cls = ic.Interception
    cls._open = _ORIGINALS["open"]
    cls.send_mouse = _ORIGINALS["send"]
    cls.mouse_device = _ORIGINALS["dev"]
    ci._user32 = user32


# ---------------------------------------------------------------- 进程 IO / 句柄
class _IOC(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


def io_snapshot():
    c = _IOC()
    kernel32.GetProcessIoCounters(kernel32.GetCurrentProcess(), ctypes.byref(c))
    h = wt.DWORD(0)
    kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(h))
    return {"read_ops": c.ReadOperationCount, "write_ops": c.WriteOperationCount,
            "other_ops": c.OtherOperationCount, "handles": h.value}


# ---------------------------------------------------------------- 单次分解
STAGE_PAIRS = [
    ("click_enter", "driver_enter", "入口(轨迹规划+前台校验)"),
    ("driver_enter", "ident", "设备身份获取"),
    ("ident", "abs", "坐标换算"),
    ("abs", "origin", "读取光标原点"),
    ("origin", "plan", "拟人轨迹发送"),
    ("plan", "watch", "落地观测器取用"),
    ("watch", "send_move", "构造+提交 MOVE"),
    ("send_move", "send_down", "构造+提交 DOWN"),
    ("send_down", "send_up", "构造+提交 UP"),
    ("send_up", "land", "内核落地等待"),
    ("land", "cursor_ok", "落点回读"),
    ("cursor_ok", "restore", "SetCursorPos 还原"),
    ("restore", "restore_ok", "还原确认"),
    ("restore_ok", "occ", "占用记账"),
    ("occ", "record", "last_click 记账"),
]


def stages_of(last):
    raw = last.get("stages") or []
    m = dict(raw)
    out = {}
    for a, b, label in STAGE_PAIRS:
        if a in m and b in m:
            out[label] = (m[b] - m[a]) * 1e6
    if "send_up" in m and "send_move" not in m and "watch" in m:
        out["提交(批量3条)"] = (m["send_up"] - m["watch"]) * 1e6
    if raw:
        out["端到端(click_enter→record)"] = (raw[-1][1] - raw[0][1]) * 1e6
    return out


def run_baseline(mouse, point, n=120, cooldown=0.006, opts=None,
                 delayed_check=True):
    if opts:
        mouse._opt_click.update(opts)
    # 生产里光标原点通常不在点击目标上：把光标停在 200px 外，
    # 这样每次都能测到「移动→点击→跨距离还原」的真实占用
    saved = cursor()
    vw = user32.GetSystemMetrics(78)
    off = (point[0] + 200, point[1]) if point[0] + 200 < vw \
        else (point[0] - 200, point[1])
    user32.SetCursorPos(*off)
    time.sleep(0.002)
    expect = cursor()
    mouse._restore_probe_fail = 0
    mouse._perf_trace = True
    rows, occ, land, restores, delayed = [], [], [], [], []
    cursors = []
    errors = []
    for _ in range(n):
        try:
            mouse.click(point[0], point[1])
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
            time.sleep(cooldown)
            continue
        last = dict(mouse._last_click or {})
        rows.append(stages_of(last))
        occ.append(last.get("occupancy_ms"))
        land.append((last.get("landing") or {}).get("elapsed_ms"))
        restores.append(last.get("restored"))
        cursors.append((last.get("landing") or {}).get("cursor_after"))
        if delayed_check:
            time.sleep(0.001)
            delayed.append(bool(cursor() == expect))
        time.sleep(cooldown)
    mouse._perf_trace = False
    user32.SetCursorPos(*saved)

    per_stage = {}
    for (_a, _b, label) in STAGE_PAIRS:
        vals = [r[label] for r in rows if label in r]
        per_stage[label] = summarize(vals)
    return {
        "n": n,
        "stage_us": per_stage,
        "end_to_end_us": summarize([r.get("端到端(click_enter→record)") for r in rows]),
        "occupancy_ms": summarize(occ),
        "landing_ms": summarize(land),
        "restore_fail_index": [i for i, r in enumerate(restores) if r is False],
        "restore_unknown": sum(1 for r in restores if r is None),
        "restore_delayed_ok_rate": round(sum(delayed) / len(delayed), 4)
        if delayed else None,
        "restore_delayed_fail_index": [i for i, v in enumerate(delayed) if not v],
        "restore_probe_fail": getattr(mouse, "_restore_probe_fail", 0),
        "cursor_after_outliers": sum(
            1 for c in cursors
            if c is not None and c != point and c != expect),
        "click_errors": errors,
        "occupancy_stats": mouse.occupancy_stats(),
        "raw_stages_ms": summarize([v for v in occ]),
    }


def run_micro(mouse):
    dev = ic.shared()
    abs_ms = []
    for _ in range(200000):
        t = time.perf_counter()
        mouse._abs(80, 80)
        abs_ms.append((time.perf_counter() - t) * 1e6)
    dev_ms = []
    for _ in range(50000):
        t = time.perf_counter()
        dev.mouse_device()
        dev_ms.append((time.perf_counter() - t) * 1e6)
    cur_ms = []
    for _ in range(20000):
        t = time.perf_counter()
        ci._cursor_pos()
        cur_ms.append((time.perf_counter() - t) * 1e6)
    watch_ms = []
    for _ in range(20000):
        t = time.perf_counter()
        ic.delivery_watch()
        watch_ms.append((time.perf_counter() - t) * 1e6)
    return {"abs_us": summarize(abs_ms), "mouse_device_us": summarize(dev_ms),
            "get_cursor_pos_us": summarize(cur_ms),
            "delivery_watch_us": summarize(watch_ms)}


# ---------------------------------------------------------------- 审计
def run_audit(mouse, point, n=200):
    counters = install_counters()
    io0 = io_snapshot()
    gc.collect()
    g0 = gc.get_count()
    mouse._perf_trace = False
    for _ in range(n):
        mouse.click(point[0], point[1])
        time.sleep(0.002)
    io1 = io_snapshot()
    gc.collect()
    g1 = gc.get_count()
    uninstall_counters()
    per = {k: round(v / n, 3) for k, v in counters.items() if k in
           ("driver_open", "driver_send", "mouse_device", "GetCursorPos",
            "SetCursorPos", "GetForegroundWindow", "GetSystemMetrics",
            "WindowFromPoint")}
    return {"n": n, "counts": counters, "per_click": per,
            "io_before": io0, "io_after": io1,
            "io_delta": {k: io1[k] - io0[k] for k in io1},
            "gc_count_delta": sum(g1) - sum(g0)}


# ---------------------------------------------------------------- 连点
def run_burst(mouse, point, n=200):
    mouse._perf_trace = True
    starts, occ, land, ok, origins = [], [], [], [], []
    home = cursor()
    probe_every, probe_n, probe_ok, probe_idx = 25, 0, 0, set()
    target_n, target_ok = 0, 0
    for i in range(n):
        mouse.click(point[0], point[1])
        last = dict(mouse._last_click or {})
        same_pt = tuple(last.get("origin") or ())
        starts.append(last["stages"][0][1])
        occ.append(last.get("occupancy_ms"))
        land.append((last.get("landing") or {}).get("elapsed_ms"))
        ok.append(bool((last.get("landing") or {}).get("ok")))
        origins.append(same_pt)
        # 独立探针：每 25 击插一次「这一击之后 1ms 光标在哪」。
        # 对照物是本击自己读到的原点（真正的还原目标），不是 burst 起始 home：
        # 若 home 恰好等于目标点（find_safe_point 把光标留在目标上），
        # 「光标回到 home」会天然成立，不能作为归位证据。
        # 探针会人为拉长相邻 gap，统计 gap 时把这两跳剔除。
        if (i + 1) % probe_every == 0:
            time.sleep(0.001)
            probe_idx.add(i)
            probe_n += 1
            cur_now = cursor()
            if cur_now == home:
                probe_ok += 1
            target_n += 1
            if cur_now == same_pt:
                target_ok += 1
    mouse._perf_trace = False
    gaps = [(starts[i + 1] - starts[i]) * 1000.0 for i in range(len(starts) - 1)
            if i not in probe_idx and (i + 1) not in probe_idx]
    cur_end = cursor()
    return {"n": n,
            "gap_ms": summarize(gaps),
            "distance_to_0.1ms": round(statistics.median(gaps) - 0.1, 3),
            "landing_ok_rate": round(sum(ok) / len(ok), 4) if ok else None,
            # 口径：每击**开始时**读到的原点（经 _home_pending 解析后）是否
            # 等于 burst 起始 home。等价于「有没有空间漂移」；样本是 N 次
            # 「点击前」读数，不是「点击后」读数，且 home==目标点时天然为 1。
            "origin_home_rate": (round(sum(1 for o in origins if o == home)
                                      / len(origins), 4) if origins else None),
            "origin_distinct": len(set(origins)),
            # 真·归位口径：本击 +1ms 后光标是否落在**本击自己的还原目标**上。
            "restore_target_rate": (round(target_ok / target_n, 4)
                                    if target_n else None),
            "restore_probe_n": probe_n,
            "restore_probe_rate": (round(probe_ok / probe_n, 4)
                                   if probe_n else None),
            "cursor_home_after": cur_end == home,
            "cursor_at_last_origin": (cur_end == origins[-1]) if origins else None,
            "occupancy_ms": summarize(occ),
            "landing_ms": summarize(land)}


def run_raw_collection(point, gaps=(0.0, 0.05, 0.1, 0.2), count=6, trials=6):
    """连发 count 次点击（每次 3 条 stroke），看 Raw Input 是否全收齐。"""
    drv = ic.shared()
    ident = drv.mouse_device()
    ax, ay = _to_abs(point)
    flags = ic.MOUSE_MOVE_ABSOLUTE | ic.MOUSE_VIRTUAL_DESKTOP
    watch = ic.delivery_watch()
    if watch is None:
        raise RuntimeError("落地观测器不可用")
    origin = cursor()
    out = {}
    for gap in gaps:
        ok = 0
        detail = []
        for _ in range(trials):
            watch.reset()
            for i in range(count):
                drv.send_mouse(0, x=ax, y=ay, flags=flags, device=ident["device"])
                drv.send_mouse(ic.MOUSE_LEFT_DOWN, x=ax, y=ay, flags=flags,
                               device=ident["device"])
                drv.send_mouse(ic.MOUSE_LEFT_UP, x=ax, y=ay, flags=flags,
                               device=ident["device"])
                if gap > 0 and i < count - 1:
                    time.sleep(gap / 1000.0)
            time.sleep(0.003)
            evs = _own_events(watch.events(), ident)
            downs = sum(1 for e in evs if e["kind"] == "down")
            ups = sum(1 for e in evs if e["kind"] == "up")
            moves = sum(1 for e in evs if e["kind"] == "move"
                        and (e.get("dx") or e.get("dy")))
            good = downs == count and ups == count
            ok += 1 if good else 0
            detail.append({"down": downs, "up": ups, "move": moves})
            user32.SetCursorPos(*origin)
        out[f"{gap:g}"] = {"rate": round(ok / trials, 3),
                           "samples": detail[:2]}
    return {"count": count, "trials": trials,
            "legend": "连发 6 次点击（18 条 stroke）后 Raw Input 全收齐比例",
            "by_gap_ms": out}


def _own_events(events, ident):
    """只保留本真实鼠标设备（VID/PID）的事件，排除用户物理操作干扰。"""
    vid = str(ident.get("vid") or "").upper()
    pid = str(ident.get("pid") or "").upper()
    if not vid or not pid:
        return list(events)
    out = []
    for e in events:
        name = (e.get("name") or "").upper()
        if f"VID_{vid}" in name and f"PID_{pid}" in name:
            out.append(e)
    return out


def _to_abs(pt):
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = max(1, user32.GetSystemMetrics(78) - 1)
    vh = max(1, user32.GetSystemMetrics(79) - 1)
    return (int(round((pt[0] - vx) * 65535 / vw)),
            int(round((pt[1] - vy) * 65535 / vh)))


# ---------------------------------------------------------------- 长跑漂移
def run_drift(point, n=10000, block=1000, cooldown=0.002, max_repark=3):
    """停靠模式：一次移动，之后只发 down/up，不每击还原（避免长时间拽光标）。

    每击仍用 DeliveryWatch 确认 down/up 落地，失败即中止（不假装成功）。
    """
    drv = ic.shared()
    ident = drv.mouse_device()
    ax, ay = _to_abs(point)
    flags = ic.MOUSE_MOVE_ABSOLUTE | ic.MOUSE_VIRTUAL_DESKTOP
    watch = ic.delivery_watch()
    if watch is None:
        raise RuntimeError("落地观测器不可用")
    origin = cursor()
    user32.SetCursorPos(*point)
    time.sleep(0.002)
    repark = 0
    blocks = []
    land, py_over = [], []
    t_block = time.perf_counter()
    cur_block = []
    io0 = io_snapshot()
    g0 = gc.get_count()
    try:
        for i in range(n):
            if i % 200 == 0:
                cls = point_class(point)
                if cls not in SAFE_CLASSES:
                    raise RuntimeError(f"安全落点失效（当前 {cls}），中止")
            pos = cursor()
            if pos != point:
                repark += 1
                if repark > max_repark:
                    raise RuntimeError(f"光标被用户挪走 {repark} 次，中止长跑")
                user32.SetCursorPos(*point)
            watch.reset()
            t = time.perf_counter()
            drv.send_mouse(ic.MOUSE_LEFT_DOWN, x=ax, y=ay, flags=flags,
                           device=ident["device"])
            drv.send_mouse(ic.MOUSE_LEFT_UP, x=ax, y=ay, flags=flags,
                           device=ident["device"])
            landing = watch.wait_buttons(vid=ident.get("vid"),
                                         pid=ident.get("pid"), timeout_ms=3.0)
            dt = (time.perf_counter() - t) * 1000.0
            if not landing.get("ok"):
                raise RuntimeError(f"第 {i} 次落地确认失败：{landing}")
            cur_block.append(dt)
            py_over.append(dt)
            if (i + 1) % block == 0:
                blocks.append({"n": len(cur_block),
                               "p50_ms": round(statistics.median(cur_block), 3),
                               "p95_ms": pct(cur_block, 0.95),
                               "mean_ms": round(statistics.fmean(cur_block), 3),
                               "wall_s": round(time.perf_counter() - t_block, 2)})
                cur_block = []
                t_block = time.perf_counter()
            if cooldown:
                time.sleep(cooldown)
    finally:
        user32.SetCursorPos(*origin)
    io1 = io_snapshot()
    gc.collect()
    g1 = gc.get_count()
    first = blocks[0]["p50_ms"] if blocks else None
    last = blocks[-1]["p50_ms"] if blocks else None
    drift = round((last - first) / first * 100.0, 2) if first else None
    return {"n": n, "block": block, "mode": "parked(down/up)",
            "blocks": blocks, "first_block_p50_ms": first,
            "last_block_p50_ms": last, "drift_pct": drift,
            "repark": repark, "io_delta": {k: io1[k] - io0[k] for k in io1},
            "gc_delta": sum(g1) - sum(g0),
            "overall": summarize(py_over)}


# ---------------------------------------------------------------- 物理输入闸门
PHYSICAL_MSG = "未启用物理注入，基准不会真的点击"
_INJ = {"strokes": 0, "clicks": 0}


def virtual_metrics():
    return {"x": user32.GetSystemMetrics(76), "y": user32.GetSystemMetrics(77),
            "w": user32.GetSystemMetrics(78), "h": user32.GetSystemMetrics(79)}


def window_title(pt):
    """安全落点所在窗口的标题；读不到就返回空串，不影响基准。"""
    try:
        h = user32.WindowFromPoint(wt.POINT(*pt))
        if not h:
            return "", 0
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, buf, 512)
        return buf.value, int(h)
    except Exception:                    # noqa: BLE001
        return "", 0


def install_inject_counter():
    """统计真实注入：send_mouse 每条 stroke、Mouse.click 每次点击。"""
    cls = ic.Interception
    orig_send, orig_click = cls.send_mouse, ci.Mouse.click

    def send(self, *a, **kw):
        _INJ["strokes"] += 1
        return orig_send(self, *a, **kw)

    def click(self, *a, **kw):
        _INJ["clicks"] += 1
        return orig_click(self, *a, **kw)

    cls.send_mouse = send
    ci.Mouse.click = click
    return _INJ


def physical_summary(enabled, title, hwnd, gate=""):
    print(f"[physical] injected={bool(_INJ['strokes'])} "
          f"strokes={_INJ['strokes']} clicks={_INJ['clicks']} "
          f"target={title!r}", flush=True)
    out = {"enabled": enabled, "injected": bool(_INJ["strokes"]),
           "injections": _INJ["strokes"], "clicks": _INJ["clicks"],
           "target_hwnd": hwnd, "target_title": title}
    if gate:
        out["gate"] = gate
    return out


def physical_gate(status, title, hwnd):
    """缺任一条就拒绝并退出：driver_status() / 终端交互确认。

    --enable-physical 已在 main 入口先查（不通过就不碰驱动），这里只做
    驱动可用与终端确认两道闸。
    """
    if not status.get("ok"):
        print(json.dumps({k: status.get(k) for k in
                          ("ok", "detail", "reason_code", "identity")},
                         ensure_ascii=False, indent=1))
        log(PHYSICAL_MSG + "（driver_status() 未通过）")
        physical_summary(False, title, hwnd, "driver_not_ok")
        raise SystemExit(2)
    if not (sys.stdin and sys.stdin.isatty()):
        log(PHYSICAL_MSG + "（非交互终端，拒绝物理注入）")
        physical_summary(False, title, hwnd, "non_interactive")
        raise SystemExit(2)
    try:
        ans = input("即将驱动真实鼠标/键盘（会动光标、发点击）。"
                    "输入 YES 继续，其它取消：")
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans.strip().lower() not in ("yes", "y"):
        log(PHYSICAL_MSG + "（终端未确认）")
        physical_summary(False, title, hwnd, "not_confirmed")
        raise SystemExit(2)
    log("[gate] 已获终端确认，允许物理注入")
    return True


def run_smoke(metrics, point, cls):
    """冒烟：只做几何 + 时序（qpc / perf_counter 调用开销），不碰 Mouse。"""
    n = 50000
    t0 = time.perf_counter()
    for _ in range(n):
        qpc()
    qpc_us = round((time.perf_counter() - t0) * 1e6 / n, 4)
    t0 = time.perf_counter()
    for _ in range(n):
        time.perf_counter()
    pc_us = round((time.perf_counter() - t0) * 1e6 / n, 4)
    return {"safe_point": point, "safe_class": cls,
            "virtual_screen": metrics,
            "qpc_call_us": qpc_us, "perf_counter_call_us": pc_us}


def write_out(tag, out):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"click_{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"[written] {path}", flush=True)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="smoke",
                    choices=["smoke", "baseline", "audit", "burst", "drift",
                             "all", "ab"])
    ap.add_argument("--smoke", action="store_true",
                    help="等价于 --mode smoke（默认档，不注入）")
    ap.add_argument("--enable-physical", action="store_true",
                    dest="enable_physical",
                    help="显式允许驱动真实鼠标/键盘（仍需终端确认）")
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--tag", default="")
    ap.add_argument("--opts", default="",
                    help="逗号分隔的开关覆盖，如 batch=0,cache=0,restore_fast=0")
    args = ap.parse_args()
    if args.smoke:
        args.mode = "smoke"
    if args.mode != "smoke" and not args.enable_physical:
        # 先拒绝，再去碰驱动：driver_status() 会打开驱动 context，
        # 本进程退出时会卡在驱动 native teardown（1 线程 0 CPU 收不掉）。
        log(PHYSICAL_MSG + "（缺少 --enable-physical）")
        physical_summary(False, "", 0, "no_flag")
        raise SystemExit(0)
    opts = {}
    for part in filter(None, args.opts.split(",")):
        k, _, v = part.partition("=")
        opts[k.strip()] = v.strip() not in ("0", "false", "off", "no")

    log("[1/6] driver_status ...")
    status = ic.driver_status()
    log("[1/6] ok")
    # filters 有两种形状（本脚本 import 的是 perf/base 冻结副本）：
    #   · 新结构（根目录 core/interception.py）：状态字典
    #     {"expected","verified","values"}，values=None 表示未回读；
    #   · 旧快照（perf/base/core）：槽位→数值的字典，等价于「已回读」。
    # 两种都按「数值全 0 才允许继续」处理，未回读只标注、不阻塞。
    filters = status.get("filters")
    fvalues = None
    filters_verified = False
    filters_expected = None
    if isinstance(filters, dict) and ("values" in filters or "expected" in filters):
        fvalues = filters.get("values")
        filters_verified = bool(filters.get("verified"))
        filters_expected = filters.get("expected")
    elif isinstance(filters, dict):
        fvalues = filters
        filters_verified = True
        filters_expected = "all-zero"
    filters_all_zero = None
    bad_filter = {}
    unread = []
    if fvalues is None:
        log("[1/6] filter 未回读（expected=%s，verified=%s）；不阻塞基准"
            % (filters_expected, filters_verified))
    else:
        for slot, v in fvalues.items():
            if v is None:
                unread.append(slot)
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                bad_filter[slot] = v
                continue
            if iv != 0:
                bad_filter[slot] = v
        if bad_filter:
            filters_all_zero = False
        elif unread:
            log("[1/6] filter 部分槽位回读失败（%s），值未知；不阻塞基准"
                % (unread,))
        else:
            filters_all_zero = True
            log("[1/6] filter 全 0（%d 个槽位）" % len(fvalues))

    log("[2/6] geometry ...")
    found, cls = find_safe_point()
    log("[2/6] point=", found, cls)
    origin = cursor()
    metrics = virtual_metrics()
    title, hwnd = window_title(found) if found else ("", 0)
    print(f"[setup] target={found} class={cls} title={title!r} "
          f"cursor_origin={origin} screen={metrics}")
    if not status.get("ok"):
        print("[1/6] driver_status 未通过：",
              json.dumps({k: status.get(k) for k in
                          ("ok", "detail", "reason_code")},
                         ensure_ascii=False))

    if args.mode == "smoke":
        out = {"setup": {"target": found, "target_class": cls,
                         "target_title": title, "target_hwnd": hwnd,
                         "cursor_origin": origin, "screen": metrics,
                         "driver_ok": bool(status.get("ok")),
                         "identity": (status.get("identity") or {}).get("mouse"),
                         "filters": filters, "filters_all_zero": filters_all_zero,
                         "filters_verified": filters_verified,
                         "python": sys.version.split()[0],
                         "when": time.strftime("%Y-%m-%d %H:%M:%S")},
               "smoke": run_smoke(metrics, found, cls),
               "physical": physical_summary(False, title, hwnd, "smoke")}
        print("[smoke]", json.dumps(out["smoke"], ensure_ascii=False))
        write_out(args.tag or "smoke", out)
        return

    # 非冒烟档：必须过物理闸门（缺任一条件直接退出，不初始化 Mouse）
    physical_gate(status, title, hwnd)
    if bad_filter:
        raise SystemExit(f"驱动 filter 不是全 0：{bad_filter}，拒绝继续")
    if not found:
        raise SystemExit("找不到桌面安全落点，中止（不在真实窗口上点）")
    point = found
    install_inject_counter()

    log("[3/6] Mouse init ...")
    mouse = ci.Mouse(mode="interception", verify=True)
    log("[3/6] mode=", mouse.mode)
    if mouse.mode != "interception":
        raise SystemExit(f"通道降级到 {mouse.mode}，基准中止")
    ident = ic.shared().mouse_device()
    if ident.get("virtual"):
        raise SystemExit("枚举到的是虚拟设备，基准中止")

    out = {"setup": {"target": point, "target_class": cls,
                     "target_title": title, "target_hwnd": hwnd,
                     "cursor_origin": origin,
                     "identity": {k: ident.get(k) for k in
                                  ("device", "vid", "pid", "virtual",
                                   "source", "hardware_id")},
                     "filters": filters, "filters_all_zero": filters_all_zero,
                     "filters_verified": filters_verified,
                     "python": sys.version.split()[0],
                     "when": time.strftime("%Y-%m-%d %H:%M:%S")},
           "micro": run_micro(mouse)}
    print("[micro]", json.dumps(out["micro"], ensure_ascii=False))

    if args.mode == "ab":
        n = args.n or 200
        base = {"batch": True, "cache": True, "plan_skip": True,
                "restore_fast": True, "btn_noabs": False,
                "restore_drv": False, "drop_move": False,
                "restore_early": False}
        combos = [
            ("off", {"batch": False, "cache": False, "plan_skip": False,
                     "restore_fast": False, "btn_noabs": False,
                     "restore_drv": False, "drop_move": False,
                     "restore_early": False}),
            ("prior", dict(base)),
            ("drv", dict(base, restore_drv=True)),
            ("dropmove", dict(base, drop_move=True)),
            ("slim", dict(base, drop_move=True, restore_drv=True)),
            ("slimE", dict(base, drop_move=True, restore_drv=True,
                           restore_early=True)),
            ("noabs", dict(base, btn_noabs=True)),
        ]
        ab = {}
        for name, combo in combos:
            r = run_baseline(mouse, point, n=n, opts=combo)
            ab[name] = {
                "end2end_p50_us": r["end_to_end_us"].get("p50"),
                "end2end_p95_us": r["end_to_end_us"].get("p95"),
                "occ_p50_ms": r["occupancy_ms"].get("p50"),
                "occ_p95_ms": r["occupancy_ms"].get("p95"),
                "landing_p50_ms": r["landing_ms"].get("p50"),
                "restore_delayed_ok": r["restore_delayed_ok_rate"],
                "restore_bad": len(r["restore_fail_index"]),
                "restore_probe_fail": r["restore_probe_fail"],
                "click_errors": len(r["click_errors"]),
                "stage_us": {k: v.get("p50") for k, v in r["stage_us"].items()},
            }
            print(f"[ab:{name}] e2e={ab[name]['end2end_p50_us']}us "
                  f"occ={ab[name]['occ_p50_ms']}ms "
                  f"land={ab[name]['landing_p50_ms']}ms "
                  f"restore_ok={ab[name]['restore_delayed_ok']}", flush=True)
        out["ab"] = ab
    if args.mode in ("baseline", "all"):
        n = args.n or 120
        out["opts"] = dict(mouse._opt_click) if opts else None
        out["baseline"] = run_baseline(mouse, point, n=n, opts=opts or None)
        print("[baseline] end2end_us=", out["baseline"]["end_to_end_us"])
        print("[baseline] occupancy_ms=", out["baseline"]["occupancy_ms"])
    if args.mode in ("audit", "all"):
        out["audit"] = run_audit(mouse, point, n=args.n or 200)
        print("[audit] per_click=", out["audit"]["per_click"])
        print("[audit] io_delta=", out["audit"]["io_delta"])
    if args.mode in ("burst", "all"):
        out["burst"] = run_burst(mouse, point, n=args.n or 200)
        out["raw_collection"] = run_raw_collection(point)
        print("[burst] gap_ms=", out["burst"]["gap_ms"])
        print("[raw] ", json.dumps(out["raw_collection"]["by_gap_ms"],
                                   ensure_ascii=False))
    if args.mode in ("drift", "all"):
        n = args.n or 10000
        out["drift"] = run_drift(point, n=n)
        print("[drift] blocks=", out["drift"]["blocks"])
        print("[drift] drift_pct=", out["drift"]["drift_pct"])

    # 本档一定已经过闸门，这里把「到底注入了多少」写进结果
    out["physical"] = physical_summary(True, title, hwnd, "confirmed")
    write_out(args.tag or args.mode, out)


if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    os._exit(code)
