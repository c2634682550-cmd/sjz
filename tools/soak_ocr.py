"""真实 OCR 后端长跑：RapidOCR 连续推理 + 进程资源采样。

和 tools/longrun_collect.py 的 `real` 模式不同：那条链路用 FakeOCR（不建
ONNXRuntime session、不跑 det/rec），测不到真实推理会创建/释放什么。这里
直接走 `core.ocr` 的**真实主链**（RapidOCR 进程内 ONNX，模型在
core/assets/rapid/），用仓库里的真实游戏截图（_rm_shots/ 1920×1080）反复
喂，覆盖三条常用路径：

  · 整帧 read_words（列表扫描，最重）
  · 详情面板 590×860 read_words（走 read_panel 拼条，每轮 np.zeros 画布）
  · read_price 价格条（Rapid → PP(若在跑) → 字模三级降级）

采样口径与 tools/stability/metrics.py 一致：GetProcessMemoryInfo.PrivateUsage、
GetGuiResources(GDI/USER)、GetProcessHandleCount、线程数、累计 CPU。另记
机器级并发（python.exe 个数 + 全机 CPU%），保证每个数字都能对得上当时的
机器负载。

用法：
  .venv\\Scripts\\python.exe tools/soak_ocr.py --minutes 120
  .venv\\Scripts\\python.exe tools/soak_ocr.py --minutes 5 --sample-s 5   # 冒烟

产物（默认写到 %TEMP%\\dafors_soak\\ocr_<时间戳>\\）：
  samples.csv  每采样一行
  summary.json 斜率 / 8h 外推 / 判定
  top_alloc.txt tracemalloc 归因（每 10 分钟一次对比）

硬约束：
  · --minutes 到点自己停（每轮都查 deadline），外部还有看门狗兜底；
  · 退出时释放 OCR 后端并写 summary，不留下僵尸进程；
  · 单线程跑推理，不额外造负载。
"""
import argparse
import csv
import gc
import json
import os
import sys
import threading
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import cv2
import numpy as np

from tools.stability import metrics


# ---------------------------------------------------------------- 机器级并发

kernel32 = metrics.kernel32
psapi = metrics.psapi


class _PROCESSENTRY32W(metrics.ctypes.Structure):
    _fields_ = [
        ("dwSize", metrics.wt.DWORD),
        ("cntUsage", metrics.wt.DWORD),
        ("th32ProcessID", metrics.wt.DWORD),
        ("th32DefaultHeapID", metrics.ctypes.c_void_p),
        ("th32ModuleID", metrics.wt.DWORD),
        ("cntThreads", metrics.wt.DWORD),
        ("th32ParentProcessID", metrics.wt.DWORD),
        ("pcPriClassBase", metrics.ctypes.c_long),
        ("dwFlags", metrics.wt.DWORD),
        ("szExeFile", metrics.ctypes.c_wchar * 260),
    ]


def _python_procs():
    """当前 python.exe 进程数（含本进程）。用 Toolhelp，比 tasklist 便宜。"""
    try:
        kernel32.CreateToolhelp32Snapshot.restype = metrics.wt.HANDLE
        snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        if snap in (None, -1, 0):
            return -1
        e = _PROCESSENTRY32W()
        e.dwSize = metrics.ctypes.sizeof(e)
        n = 0
        try:
            ok = kernel32.Process32FirstW(snap, metrics.ctypes.byref(e))
            while ok:
                if (e.szExeFile or "").lower() in ("python.exe", "pythonw.exe"):
                    n += 1
                ok = kernel32.Process32NextW(snap, metrics.ctypes.byref(e))
        finally:
            kernel32.CloseHandle(snap)
        return n
    except Exception:                       # noqa: BLE001
        return -1


class _SysCpu:
    """全机 CPU 占用（两次采样的 GetSystemTimes 差分）。"""

    def __init__(self):
        self.prev = self._read()

    def _read(self):
        idle, kern, user = (metrics.FILETIME() for _ in range(3))
        kernel32.GetSystemTimes(metrics.ctypes.byref(idle),
                                metrics.ctypes.byref(kern),
                                metrics.ctypes.byref(user))
        return idle._secs(), kern._secs(), user._secs()

    def pct(self):
        now = self._read()
        if self.prev is None:
            self.prev = now
            return 0.0
        di = now[0] - self.prev[0]
        dk = now[1] - self.prev[1]
        du = now[2] - self.prev[2]
        self.prev = now
        total = dk + du
        if total <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (1.0 - di / total) * 100.0)), 1)


# ---------------------------------------------------------------- 语料

def load_corpus(limit=24):
    """真实游戏截图 + 真实的按钮/数字小块。全部仓库内文件，不合成、不改写。"""
    root = os.path.join(REPO, "_rm_shots")
    frames = []
    for d in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        dd = os.path.join(root, d)
        if not os.path.isdir(dd):
            continue
        for f in sorted(os.listdir(dd)):
            if f.lower().endswith(".png"):
                frames.append(os.path.join(dd, f))
    # 均匀抽，避免只取到某一轮会话的同一屏
    if len(frames) > limit:
        step = max(1, len(frames) // limit)
        frames = frames[::step][:limit]
    small = []
    data = os.path.join(REPO, "data")
    if os.path.isdir(data):
        for d in sorted(os.listdir(data)):
            for name in ("buy.png", "confirm.png"):
                p = os.path.join(data, d, name)
                if os.path.isfile(p):
                    small.append(p)
    digits = []
    dd = os.path.join(REPO, "core", "assets", "digits", "real")
    if os.path.isdir(dd):
        for f in sorted(os.listdir(dd)):
            if f.lower().endswith(".png"):
                digits.append(os.path.join(dd, f))
    if len(digits) > 20:
        step = max(1, len(digits) // 20)
        digits = digits[::step][:20]
    return frames, small, digits


def load_images(paths, want=()):
    out = []
    for p in paths:
        img = cv2.imread(p)
        if img is None:
            continue
        h, w = img.shape[:2]
        if want and (w, h) != tuple(want):
            continue
        out.append(img)
    return out


# ---------------------------------------------------------------- 主流程

def run(args):
    outdir = args.out or os.path.join(
        os.environ.get("TEMP", os.path.expanduser("~")), "dafors_soak",
        "ocr_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)
    pidfile = os.path.join(outdir, "pid.txt")
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    print(f"[soak] 启动 pid={os.getpid()} 脚本={os.path.abspath(__file__)} "
          f"minutes={args.minutes} out={outdir}", flush=True)

    frames, small, digits = load_corpus(args.frames)
    imgs_full = load_images(frames)
    imgs_small = load_images(small)
    imgs_digit = load_images(digits)
    if not imgs_full:
        raise SystemExit("没有可用真实截图（_rm_shots 为空）")
    print(f"[soak] 语料：整帧 {len(imgs_full)} 张、按钮 {len(imgs_small)} 张、"
          f"数字 {len(imgs_digit)} 张", flush=True)

    # 强制启用 RapidOCR（config.json 里可能被人关过），只影响本进程
    from core import ocr, ocr_rapid
    s = ocr._load_settings()
    s.setdefault("rapid", {})["enabled"] = True
    ocr._settings = s
    ocr._backend = None
    if not ocr.available():
        raise SystemExit(f"真实 OCR 不可用：{ocr.backend_name()} / {ocr.rapid_error()}")
    print(f"[soak] 后端：{ocr.backend_name()}", flush=True)

    # 面板裁切：真实整帧里取中间竖条 590×860（覆盖 read_panel 的拼条路径）
    panels = []
    for img in imgs_full[:6]:
        h, w = img.shape[:2]
        x0 = max(0, (w - 590) // 2)
        y0 = max(0, (h - 860) // 2)
        panels.append(img[y0:y0 + 860, x0:x0 + 590].copy())

    tracemalloc.start(10)
    cpu = _SysCpu()
    base_mem = None
    t_start = time.time()
    deadline = t_start + args.minutes * 60.0
    stop_at = deadline

    # 看门狗：单次推理卡死时也不会赖在机器上（到点 +5min 硬退）
    def _watchdog():
        while time.time() < stop_at + 300:
            time.sleep(10)
        print("[soak] !! 超过硬超时 5 分钟，强制退出（不写 summary）", flush=True)
        os._exit(70)

    threading.Thread(target=_watchdog, name="soak-watchdog", daemon=True).start()

    samples = []
    calls = 0
    fails = 0
    lat = []
    last_sample = 0.0
    next_snap = t_start + args.snap_s
    snap_ref = None
    alloc_lines = []

    def sample(force=False):
        nonlocal last_sample
        now = time.time()
        if not force and now - last_sample < args.sample_s:
            return
        last_sample = now
        row = metrics.sample(deep=True)
        row["minute"] = round((now - t_start) / 60.0, 2)
        row["calls"] = calls
        row["fails"] = fails
        row["p50_ms"] = round(float(np.median(lat[-200:])), 1) if lat else 0.0
        row["py_procs"] = _python_procs()
        row["sys_cpu_pct"] = cpu.pct()
        samples.append(row)
        print(f"[{row['minute']:7.2f}m] mem={row.get('private_mb')}MB "
              f"gdi={row.get('gdi')} user={row.get('user')} "
              f"hd={row.get('handles')} th={row.get('proc_threads')} "
              f"calls={calls} p50={row['p50_ms']}ms "
              f"py={row['py_procs']} syscpu={row['sys_cpu_pct']}%", flush=True)

    sample(force=True)

    # 预热：3 轮整帧 + 1 轮面板，把 ONNX session 首次分配（约 700MB）单独算，
    # 不进趋势；同时打印单帧耗时，让后面的斜率有对照。
    tw = time.perf_counter()
    for i in range(min(3, len(imgs_full))):
        try:
            ocr.read_words(imgs_full[i])
            calls += 1
        except Exception:                   # noqa: BLE001
            pass
    try:
        ocr.read_words(panels[0])
        calls += 1
    except Exception:                       # noqa: BLE001
        pass
    warm_s = time.perf_counter() - tw
    gc.collect()
    sample(force=True)
    base_mem = samples[-1].get("private_mb") or 0.0
    warm_idx = len(samples)               # 趋势只取预热之后的行
    snap_ref = tracemalloc.take_snapshot()
    print(f"[soak] 预热 {warm_s:.1f}s（3 整帧 + 1 面板），基线 private={base_mem}MB，"
          f"开始计时 {args.minutes}min", flush=True)

    idx = 0
    while time.time() < stop_at:
        t0 = time.perf_counter()
        try:
            # 1) 整帧列表读：产品里列表扫描才做，比较重，每 5 轮一次
            if idx % 5 == 0:
                ocr.read_words(imgs_full[idx % len(imgs_full)])
                calls += 1
            # 2) 详情面板 590×860：引擎热路径（拼条 canvas 每轮新建）
            ocr.read_words(panels[idx % len(panels)])
            calls += 1
            # 3) 价格条三级降级（Rapid + 可选的 PP/字模）
            if imgs_small:
                ocr.read_price(imgs_small[idx % len(imgs_small)])
                calls += 1
            # 4) 小数字块（短图，det 走 limit_side_len 的另一分支）
            if imgs_digit:
                ocr.read_words(imgs_digit[idx % len(imgs_digit)])
                calls += 1
        except Exception as e:              # noqa: BLE001
            fails += 1
            if fails <= 5:
                print(f"[soak] !! 推理失败：{type(e).__name__}: {e}", flush=True)
        lat.append((time.perf_counter() - t0) * 1000.0)
        idx += 1

        sample()

        if time.time() >= next_snap:
            next_snap = time.time() + args.snap_s
            snap = tracemalloc.take_snapshot()
            top = snap.compare_to(snap_ref, "lineno")[:12]
            lines = [f"--- t={round((time.time()-t_start)/60.0,1)}min 增长前 12 处"]
            for st in top:
                tb = st.traceback[0]
                lines.append(f"{st.size_diff/1024:9.1f}KB  "
                             f"{os.path.relpath(tb.filename, REPO)}:{tb.lineno}")
            alloc_lines.extend(lines)
            snap_ref = snap
            with open(os.path.join(outdir, "top_alloc.txt"), "a",
                      encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print("\n".join(lines[:4]), flush=True)

    # ---------------- 收尾 ----------------
    gc.collect()
    sample(force=True)
    post = samples[warm_idx:]                 # 预热之后才参与趋势
    if not post:
        post = [samples[-1]]
    end_mem = post[-1].get("private_mb") or 0.0

    def slope(key, rows):
        xs = [(r["minute"], r.get(key)) for r in rows
              if isinstance(r.get(key), (int, float))]
        if len(xs) < 3:
            return 0.0
        n = len(xs)
        mx = sum(x for x, _ in xs) / n
        my = sum(y for _, y in xs) / n
        den = sum((x - mx) ** 2 for x, _ in xs)
        if den == 0:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in xs) / den

    dur_h = (time.time() - t_start) / 3600.0
    ext = {}
    for key in ("private_mb", "gdi", "user", "handles", "proc_threads"):
        sl = slope(key, post)
        last = post[-1].get(key) or 0
        ext[key] = {"slope_per_min": round(sl, 4),
                    "slope_per_hour": round(sl * 60, 3),
                    "now": last,
                    "extrapolated_8h": round(last + sl * 60 * 8, 2)}

    # 判定：8h 外推内存增长 >3%，或 GDI/USER 单调上涨不回落
    mem_growth_pct = ((ext["private_mb"]["extrapolated_8h"] - base_mem) / base_mem
                      * 100.0) if base_mem else 0.0

    def monotonic(key):
        ys = [r.get(key) for r in post if isinstance(r.get(key), (int, float))]
        if len(ys) < 4:
            return False
        # 允许 ±1 的抖动：整体趋势仍算单调
        ups = sum(1 for a, b in zip(ys, ys[1:]) if b > a)
        downs = sum(1 for a, b in zip(ys, ys[1:]) if b < a)
        return ups >= downs and (ys[-1] - ys[0]) >= 2

    # 样本不够（短跑）不做泄漏判定，避免把启动分配外推成天文数字
    enough = (len(post) >= args.min_samples) and (dur_h >= args.min_hours)
    verdict = {
        "insufficient_data": (not enough),
        "memory_leak": bool(enough and mem_growth_pct > 3.0),
        "gdi_leak": bool(enough and monotonic("gdi")),
        "user_leak": bool(enough and monotonic("user")),
        "handle_leak": bool(enough and monotonic("handles")),
        "thread_leak": bool(enough and monotonic("proc_threads")),
    }

    summary = {
        "minutes": round(dur_h * 60, 2),
        "calls": calls,
        "fails": fails,
        "p50_ms": round(float(np.median(lat)) if lat else 0.0, 1),
        "p95_ms": round(float(np.percentile(lat, 95)) if lat else 0.0, 1),
        "base_private_mb": base_mem,
        "end_private_mb": end_mem,
        "mem_growth_pct_observed": round((end_mem - base_mem) / base_mem * 100, 3)
        if base_mem else 0.0,
        "extrapolation": ext,
        "mem_growth_pct_8h": round(mem_growth_pct, 3),
        "verdict": verdict,
        "threads_final": post[-1].get("proc_threads"),
        "handles_final": post[-1].get("handles"),
        "gdi_final": post[-1].get("gdi"),
        "user_final": post[-1].get("user"),
        "python_procs_final": post[-1].get("py_procs"),
        "sys_cpu_pct_final": post[-1].get("sys_cpu_pct"),
        "host": ocr.backend_name(),
    }

    csv_path = os.path.join(outdir, "samples.csv")
    keys = ["minute", "ts", "private_mb", "working_set_mb", "handles", "gdi",
            "user", "proc_threads", "py_threads", "calls", "fails", "p50_ms",
            "py_procs", "sys_cpu_pct", "cpu_s", "gen0_collections",
            "gen2_collections", "objects"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in samples:
            w.writerow(r)
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "top_alloc.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(alloc_lines) + "\n")

    print("[soak] 结论：" + json.dumps(verdict, ensure_ascii=False))
    print(f"[soak] 内存 {base_mem}→{end_mem}MB，8h 外推 {mem_growth_pct:+.2f}%；"
          f"GDI={samples[-1].get('gdi')} USER={samples[-1].get('user')} "
          f"句柄={samples[-1].get('handles')} 线程={samples[-1].get('proc_threads')}",
          flush=True)
    print(f"[soak] 输出目录：{outdir}", flush=True)

    # 释放 ONNX session，别把内存留在退出前的进程快照里
    try:
        ocr._backend = None
        ocr._settings = None
        gc.collect()
    except Exception:                        # noqa: BLE001
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="真实 OCR 长跑（RapidOCR 连续推理）")
    ap.add_argument("--minutes", type=float, default=120.0)
    ap.add_argument("--sample-s", type=float, default=20.0)
    ap.add_argument("--snap-s", type=float, default=600.0)
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--min-hours", type=float, default=1.5)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
