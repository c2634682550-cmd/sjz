"""GUI 的 GDI/USER 长跑：真实 pywebview 窗口 + 持续刷新，采样全进程树资源。

测的是什么
-----------
产品 GUI = pywebview(WinForms+WebView2) 窗口 + web/index.html 看板。看板每秒
轮询后端，框选页每秒拉一次 `/api/screen/capture`（这是全程序里唯一真正
CreateBitmap/DeleteObject 的路径：core.regions.open_screen → mss Screen
→ 抓帧 → close）。

所以这里的负载 = 窗口存在 + 页面在跑 + 每秒 1 次真实抓屏请求 + 每秒一次
强制重绘（改 documentElement.style.opacity，不新增 DOM 节点）。采样口径：
  · 宿主进程（python）：GDI/USER/句柄/线程/私有内存
  · 全部 WebView2 子进程（msedgewebview2.exe，按父链归属）：同上求和
  · 机器级 python 进程数 + 全机 CPU%，保证数字能对上当时的负载

不可见保证
---------
窗口建在所有显示器之外的负坐标区（离屏），并打上 WS_EX_TOOLWINDOW
（任务栏不出现图标）；如果算不出一个完全离屏的位置，就退回「建窗后 1s 内
SW_MINIMIZE」。两种情况都不会在用户桌面上留下可见窗口。

用法：
  .venv\\Scripts\\python.exe tools/soak_gui_gdi.py --minutes 45
  .venv\\Scripts\\python.exe tools/soak_gui_gdi.py --minutes 3 --sample-s 10   # 冒烟

产物写到 %TEMP%\\dafors_soak\\gui_<时间戳>\\：samples.csv / summary.json
"""
import argparse
import ctypes
from ctypes import wintypes as wt
import csv
import json
import os
import socket
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tools.stability import metrics       # noqa: E402

k32 = metrics.kernel32
psapi = metrics.psapi
u32 = metrics.user32


# ---------------------------------------------------------------- Win32 小工具

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


class PE32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("pid", wt.DWORD), ("heap", ctypes.c_void_p),
                ("mod", wt.DWORD), ("threads", wt.DWORD),
                ("ppid", wt.DWORD), ("pri", ctypes.c_long),
                ("flags", wt.DWORD), ("exe", ctypes.c_wchar * 260)]


def monitor_rects():
    out = []

    def cb(hmon, hdc, rect, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            out.append((mi.rcMonitor.left, mi.rcMonitor.top,
                        mi.rcMonitor.right, mi.rcMonitor.bottom))
        return True

    u32.EnumDisplayMonitors(None, None,
                            ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR,
                                               wt.HDC, ctypes.POINTER(wt.RECT),
                                               wt.LPARAM)(cb), 0)
    return out


def offscreen_xy(w, h):
    """所有显示器左侧之外的位置；放不下就返回 None。"""
    rects = monitor_rects()
    if not rects:
        return None
    left = min(r[0] for r in rects)
    return left - w - 16, 0


def processes():
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    e = PE32()
    e.dwSize = ctypes.sizeof(e)
    rows = {}
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        rows[e.pid] = (e.ppid, (e.exe or "").lower())
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return rows


def descendants(root_pid, exe_name):
    rows = processes()
    out = []
    for pid, (ppid, exe) in rows.items():
        if exe != exe_name:
            continue
        p = ppid
        depth = 0
        while p and depth < 20:
            if p == root_pid:
                out.append(pid)
                break
            p = rows.get(p, (0, ""))[0]
            depth += 1
    return out


_ACCESS = 0x1000 | 0x0400 | 0x0010        # LIMITED | QUERY_INFORMATION | VM_READ


def proc_res(pid):
    h = k32.OpenProcess(_ACCESS, False, pid)
    if not h:
        return None
    try:
        c = metrics.PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(c)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        n = wt.DWORD(0)
        k32.GetProcessHandleCount(h, ctypes.byref(n))
        return {"private_mb": round(c.PrivateUsage / 1048576, 2),
                "handles": int(n.value),
                "gdi": int(u32.GetGuiResources(h, 0)),
                "user": int(u32.GetGuiResources(h, 1))}
    finally:
        k32.CloseHandle(h)


def threads_of(pid):
    snap = k32.CreateToolhelp32Snapshot(0x4, 0)
    if snap in (None, -1, 0):
        return -1
    e = metrics.THREADENTRY32()
    e.dwSize = ctypes.sizeof(e)
    n = 0
    try:
        ok = k32.Thread32First(snap, ctypes.byref(e))
        while ok:
            if e.th32OwnerProcessID == pid:
                n += 1
            ok = k32.Thread32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return n


def force_tool_window(hwnd):
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    u32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
    ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)


def find_hwnd(pid, title):
    found = []

    def cb(hwnd, _lp):
        p = wt.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(1, n + 1))
        u32.GetWindowTextW(hwnd, buf, n + 1)
        if title in (buf.value or ""):
            found.append(int(hwnd))
        return True

    u32.EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return found[0] if found else 0


def window_rect(hwnd):
    r = wt.RECT()
    if u32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.left, r.top, r.right, r.bottom
    return None


def intersects_monitor(rect):
    for l, t, rr, bb in monitor_rects():
        if rect[0] < rr and rect[2] > l and rect[1] < bb and rect[3] > t:
            return True
    return False


# ---------------------------------------------------------------- 主流程

def run(args):
    outdir = args.out or os.path.join(
        os.environ.get("TEMP", os.path.expanduser("~")), "dafors_soak",
        "gui_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)

    # DPI 口径与 desktop.py 一致，必须在建窗前
    try:
        from core.screen import enable_dpi_awareness
        enable_dpi_awareness()
    except Exception:                    # noqa: BLE001
        pass

    import webview
    from core.server import serve

    port = args.port
    t = threading.Thread(target=serve, args=(args.host, port, None), daemon=True)
    t.start()
    url = f"http://{args.host}:{port}"
    deadline_wait = time.time() + 15
    while time.time() < deadline_wait:
        with socket.socket() as s:
            if s.connect_ex((args.host, port)) == 0:
                break
        time.sleep(0.05)
    else:
        raise SystemExit(f"服务没起来：{url}")

    xy = offscreen_xy(args.width, args.height)
    if xy is None:
        xy = (0, 0)
    print(f"[gui] 服务就绪 {url}；窗口位置 {xy}（离屏）", flush=True)

    stats = {"minute": [], "shots": 0, "errors": 0, "repaints": 0,
             "hwnd": 0, "visible": None, "rect": None, "samples": []}
    stop = threading.Event()
    t_start = time.time()
    stop_at = t_start + args.minutes * 60

    def driver(win):
        hwnd = 0
        target_xy = xy
        for _ in range(60):
            hwnd = find_hwnd(os.getpid(), args.title)
            if hwnd:
                break
            time.sleep(0.05)
        stats["hwnd"] = hwnd
        if hwnd:
            force_tool_window(hwnd)                    # 任务栏不出现图标
            if target_xy != (0, 0):
                u32.SetWindowPos(hwnd, 0, target_xy[0], target_xy[1],
                                 args.width, args.height, 0x0010 | 0x0004)
            stats["rect"] = window_rect(hwnd)
            stats["visible"] = bool(u32.IsWindowVisible(hwnd))
            # 兜底：万一离屏位置被系统纠正回可见区域，就 3 秒内最小化
            if stats["rect"] and intersects_monitor(stats["rect"]):
                u32.ShowWindow(hwnd, 6)                 # SW_MINIMIZE
                print("[gui] 窗口与显示器相交，已立即最小化", flush=True)
        print(f"[gui] hwnd={hwnd} rect={stats['rect']} "
              f"visible={stats['visible']}", flush=True)

        n = 0
        while time.time() < stop_at:
            loop_t = time.time()
            # 1) 每秒一次真实抓屏（框选页同款请求；mss 建/删 DC+位图）
            try:
                with urllib.request.urlopen(
                        f"{url}/api/screen/capture", timeout=5) as r:
                    r.read()
                stats["shots"] += 1
            except Exception as e:               # noqa: BLE001
                stats["errors"] += 1
                if stats["errors"] <= 3:
                    print(f"[gui] !! 抓屏失败 {type(e).__name__}: {e}", flush=True)
            # 2) 每秒一次强制重绘（只改样式，不新增 DOM 节点）
            try:
                win.evaluate_js(
                    "document.documentElement.style.opacity="
                    f"'{0.999 if n % 2 else 1}';")
                stats["repaints"] += 1
            except Exception:                    # noqa: BLE001
                pass
            n += 1
            dt = time.time() - loop_t
            if dt < 1.0:
                time.sleep(1.0 - dt)
        stop.set()
        try:
            win.destroy()
        except Exception:                        # noqa: BLE001
            pass

    def sampler():
        last = 0.0
        while not stop.is_set():
            now = time.time()
            if now - last >= args.sample_s:
                last = now
                row = metrics.sample(deep=False)
                row["minute"] = round((now - t_start) / 60, 2)
                wv = descendants(os.getpid(), "msedgewebview2.exe")
                agg = {"gdi": 0, "user": 0, "handles": 0, "private_mb": 0.0,
                       "threads": 0, "count": 0}
                for cp in wv:
                    r = proc_res(cp)
                    if not r:
                        continue
                    agg["count"] += 1
                    agg["gdi"] += r["gdi"]
                    agg["user"] += r["user"]
                    agg["handles"] += r["handles"]
                    agg["private_mb"] += r["private_mb"]
                    agg["threads"] += threads_of(cp)
                row["wv_count"] = agg["count"]
                row["wv_gdi"] = agg["gdi"]
                row["wv_user"] = agg["user"]
                row["wv_handles"] = agg["handles"]
                row["wv_private_mb"] = round(agg["private_mb"], 1)
                row["wv_threads"] = agg["threads"]
                row["gdi_total"] = (row.get("gdi") or 0) + agg["gdi"]
                row["user_total"] = (row.get("user") or 0) + agg["user"]
                row["shots"] = stats["shots"]
                row["repaints"] = stats["repaints"]
                row["errors"] = stats["errors"]
                stats["samples"].append(row)
                print(f"[{row['minute']:6.2f}m] host_gdi={row.get('gdi')} "
                      f"wv_gdi={agg['gdi']}({agg['count']}p) "
                      f"total_gdi={row['gdi_total']} "
                      f"user={row['user_total']} hd={row.get('handles')} "
                      f"mem={row.get('private_mb')}+{row['wv_private_mb']}MB "
                      f"shots={stats['shots']}", flush=True)
            time.sleep(2.0)

    smp = threading.Thread(target=sampler, daemon=True)
    smp.start()

    win = webview.create_window(
        args.title, url,
        width=args.width, height=args.height,
        x=xy[0], y=xy[1],
        frameless=True, easy_drag=False, shadow=True,
        background_color="#0d1117", focus=False,
    )
    print("[gui] create_window 完成，进入 start()", flush=True)
    webview.start(driver, win)
    stop.set()

    # ---------------- 收尾：确认窗口/子进程/句柄回落 ----------------
    time.sleep(1.0)
    end_host = metrics.sample(deep=False)
    # WebView2 子进程要跟着窗口退出；给它 15 秒
    gone_by = time.time() + 15
    left = descendants(os.getpid(), "msedgewebview2.exe")
    while left and time.time() < gone_by:
        time.sleep(1.0)
        left = descendants(os.getpid(), "msedgewebview2.exe")
    killed = 0
    for cp in left:                       # 极端情况兜底，不留僵尸
        h = k32.OpenProcess(0x0001, False, cp)      # PROCESS_TERMINATE
        if h:
            k32.TerminateProcess(h, 1)
            k32.CloseHandle(h)
            killed += 1
    time.sleep(0.5)
    rows = stats["samples"]
    dur_h = (time.time() - t_start) / 3600.0
    # 前两行是「建窗/WebView2 冷启动」阶段，GDI 从 3 涨到 51 是必然的，
    # 趋势只看窗口就绪后的样本
    post = rows[2:] if len(rows) > 4 else rows

    def slope(key):
        xs = [(r["minute"], r.get(key)) for r in post
              if isinstance(r.get(key), (int, float))]
        if len(xs) < 4:
            return 0.0
        n = len(xs)
        mx = sum(x for x, _ in xs) / n
        my = sum(y for _, y in xs) / n
        den = sum((x - mx) ** 2 for x, _ in xs)
        return sum((x - mx) * (y - my) for x, y in xs) / den if den else 0.0

    def mono(key):
        ys = [r.get(key) for r in post if isinstance(r.get(key), (int, float))]
        if len(ys) < 5:
            return False
        ups = sum(1 for a, b in zip(ys, ys[1:]) if b > a)
        downs = sum(1 for a, b in zip(ys, ys[1:]) if b < a)
        return ups >= downs and (ys[-1] - ys[0]) >= 2

    ext = {}
    for key in ("gdi_total", "wv_gdi", "user_total", "handles",
                "private_mb", "wv_private_mb"):
        sl = slope(key)
        now_v = post[-1].get(key) if post else 0
        ext[key] = {"slope_per_hour": round(sl * 60, 3),
                    "now": now_v,
                    "extrapolated_8h": round((now_v or 0) + sl * 60 * 8, 2)}

    base_gdi = post[0].get("gdi_total") if post else None
    enough = len(post) >= 10 and dur_h >= args.min_hours
    verdict = {
        "insufficient_data": not enough,
        "gdi_leak": bool(enough and mono("gdi_total")),
        "wv_gdi_leak": bool(enough and mono("wv_gdi")),
        "user_leak": bool(enough and mono("user_total")),
        "handle_leak": bool(enough and mono("handles")),
    }
    # 内存：8h 外推相对首个就绪样本增长 >3% 才算
    base_mem = ((post[0].get("private_mb") or 0) +
                (post[0].get("wv_private_mb") or 0)) if post else 0
    ext_mem = ((ext["private_mb"]["extrapolated_8h"] or 0) +
               (ext["wv_private_mb"]["extrapolated_8h"] or 0))
    mem_pct = ((ext_mem - base_mem) / base_mem * 100.0) if base_mem else 0.0
    verdict["memory_leak"] = bool(enough and mem_pct > 3.0)
    verdict["mem_growth_pct_8h"] = round(mem_pct, 2)

    summary = {
        "minutes": round(dur_h * 60, 2),
        "window": {"hwnd": stats["hwnd"], "rect": stats["rect"],
                   "visible": stats["visible"],
                   "intersects_monitor": intersects_monitor(stats["rect"])
                   if stats["rect"] else None},
        "shots": stats["shots"], "errors": stats["errors"],
        "repaints": stats["repaints"],
        "gdi_base": base_gdi,
        "gdi_end": rows[-1].get("gdi_total") if rows else None,
        "host_gdi_end": end_host.get("gdi"),
        "host_user_end": end_host.get("user"),
        "host_handles_end": end_host.get("handles"),
        "webview_procs_end": len(descendants(os.getpid(), "msedgewebview2.exe")),
        "leftover_killed": killed,
        "extrapolation": ext,
        "verdict": verdict,
    }
    with open(os.path.join(outdir, "samples.csv"), "w", newline="",
              encoding="utf-8") as f:
        keys = ["minute", "gdi", "user", "wv_gdi", "wv_user", "gdi_total",
                "user_total", "handles", "private_mb", "wv_private_mb",
                "wv_handles", "proc_threads", "wv_threads", "wv_count",
                "shots", "repaints", "errors", "cpu_s"]
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("[gui] 结论：" + json.dumps(verdict, ensure_ascii=False))
    print(f"[gui] GDI 合计 {base_gdi} → {summary['gdi_end']}；"
          f"窗口 {summary['window']}；残留 webview 进程 {summary['webview_procs_end']}；"
          f"killed={killed}", flush=True)
    print(f"[gui] 输出目录：{outdir}", flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="GUI GDI 长跑（真实窗口 + 持续刷新）")
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--sample-s", type=float, default=20.0)
    ap.add_argument("--min-hours", type=float, default=0.5)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--title", default="SJZ 抢购控制台")
    ap.add_argument("--out", default="")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
