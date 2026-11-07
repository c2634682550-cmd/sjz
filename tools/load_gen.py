"""dafors 规范负载发生器 —— 替代裸忙等。

背景：2026-11-07 后台残留 23 个 `python -c "while True: pass"` 忙等进程，
其中 11 个满核，把 CPU 推到 90%，让当天所有性能数字互相矛盾。
根因是造负载的脚本没有硬超时、没有清理、没有降优先级。

本文件是**唯一允许**的造负载入口，四个硬约束：

1. 进程优先级 = BELOW_NORMAL（默认）或 IDLE，用户前台程序永远优先；
2. watchdog 线程到了 --duration 无条件 os._exit，不依赖主线程能否正常返回；
3. KeyboardInterrupt / SIGTERM / SIGBREAK / atexit 四条路径都清理子 worker；
4. 父进程消失时子 worker 自杀（OpenProcess 句柄 + os.getppid 双保险）。

用法：

    python tools/load_gen.py --workers 4 --duration 5
    python tools/load_gen.py --workers 4 --duration 30 --target-cpu 50
    python tools/load_gen.py --workers 2 --duration 10 --priority idle

退出时固定打印一行：

    [load_gen] pid=... workers=... elapsed=...s cleaned=ok
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

IS_WIN = os.name == "nt"

# Windows priority class 常量
PRIORITY_CLASS = {
    "idle": 0x00000040,          # IDLE_PRIORITY_CLASS
    "below_normal": 0x00004000,  # BELOW_NORMAL_PRIORITY_CLASS
    "normal": 0x00000020,        # NORMAL_PRIORITY_CLASS
}
PRIORITY_NAME = {
    0x00000040: "idle",
    0x00004000: "below_normal",
    0x00000020: "normal",
    0x00000080: "high",
    0x000000100: "realtime",
}
CREATE_NO_WINDOW = 0x08000000
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259

TEMP_DIR = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "dafors_load"

_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WIN else None
if IS_WIN:
    _k32.GetCurrentProcess.restype = ctypes.c_void_p
    _k32.SetPriorityClass.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _k32.SetPriorityClass.restype = ctypes.c_int
    _k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    _k32.GetPriorityClass.restype = wintypes.DWORD
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
    _k32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME)]


# ------------------------------------------------------------------ 基础工具

def set_priority(name: str) -> str:
    """把当前进程优先级降到 name。返回实际生效的名字。"""
    if not IS_WIN:
        return "unsupported"
    cls = PRIORITY_CLASS[name]
    if not _k32.SetPriorityClass(_k32.GetCurrentProcess(), cls):
        return "failed"
    got = _k32.GetPriorityClass(_k32.GetCurrentProcess())
    return PRIORITY_NAME.get(got, hex(got))


def get_priority(pid: int) -> str | None:
    """查另一个进程的优先级类名（跨进程）。"""
    if not IS_WIN:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        got = _k32.GetPriorityClass(h)
        return PRIORITY_NAME.get(got, hex(got))
    finally:
        _k32.CloseHandle(h)


def proc_started_at(pid: int) -> float | None:
    """进程创建时刻（epoch 秒）。进程不存在返回 None。

    用于清扫陈旧 pid 文件时防止 PID 复用误杀。
    """
    if not IS_WIN:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                    ctypes.byref(k), ctypes.byref(u)):
            return None
        ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return (ft - 116444736000000000) / 1e7
    finally:
        _k32.CloseHandle(h)


def proc_alive(pid: int) -> bool:
    if not IS_WIN:
        return False
    h = _k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            if code.value == STILL_ACTIVE:
                return True
        return _k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        _k32.CloseHandle(h)


def kill_proc(pid: int) -> bool:
    if not IS_WIN:
        return False
    h = _k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_k32.TerminateProcess(h, 1))
    finally:
        _k32.CloseHandle(h)


def cpu_count() -> int:
    try:
        n = len(os.sched_getaffinity(0))       # type: ignore[attr-defined]
    except AttributeError:
        n = os.cpu_count() or 4
    return max(1, n)


def registry_path(pid: int) -> Path:
    return TEMP_DIR / f"{pid}.json"


def stop_path_for(pid: int) -> Path:
    return TEMP_DIR / f"{pid}.stop"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                          # noqa: BLE001
        return None


def remove_own_registry(pid: int, started_at: float | None) -> None:
    """worker 发现父进程已死后，顺手删掉父进程那份 pid 文件。

    只在「文件里的 pid == 父 PID 且启动时刻对得上」时才删，避免 PID 复用
    时误删新一轮 load_gen 的登记文件。
    """
    f = registry_path(pid)
    info = _read_json(f)
    if not info:
        return
    try:
        if abs(float(info.get("started_at", 0)) - (started_at or 0)) > 3:
            return
    except (TypeError, ValueError):
        return
    try:
        f.unlink()
    except OSError:
        pass


def sweep_stale(verbose: bool = True) -> int:
    """清扫超过自身 duration 2 倍的陈旧 pid 文件，并结束仍然存活的对应进程。"""
    if not TEMP_DIR.exists():
        return 0
    now = time.time()
    killed = 0
    for f in sorted(TEMP_DIR.glob("*.json")):
        info = _read_json(f)
        try:
            started = float(info["started_at"]) if info else f.stat().st_mtime
        except Exception:                      # noqa: BLE001
            started = f.stat().st_mtime
        dur = float(info.get("duration") or 0) if info else 0
        if now - started <= max(2 * dur, 10):
            continue
        pids = []
        if info:
            pids = [info.get("pid")] + list(info.get("child_pids") or [])
        for pid in [p for p in pids if isinstance(p, int)]:
            # 防 PID 复用：创建时刻必须和登记时刻对得上（±3s）
            ct = proc_started_at(pid)
            if ct is None or abs(ct - started) > 3:
                continue
            if proc_alive(pid) and kill_proc(pid):
                killed += 1
                if verbose:
                    print(f"[load_gen] 清扫陈旧进程 pid={pid} (file={f.name})")
        try:
            f.unlink()
        except OSError:
            pass
        if verbose:
            print(f"[load_gen] 删除陈旧 pid 文件 {f.name}")
    for f in TEMP_DIR.glob("*.stop"):
        try:
            if now - f.stat().st_mtime > 60:
                f.unlink()
        except OSError:
            pass
    return killed


# ------------------------------------------------------------------ 子 worker

def worker_main(parent_pid: int, duty: float, priority: str, stop_file: str) -> None:
    """子 worker：占 CPU，父进程死了就自杀。

    Windows 上 os.getppid() 返回的是创建时记录的父 PID，父进程退出后它不会
    变（不像 POSIX 变成 1），所以真正管用的是启动时 OpenProcess 拿到的父进程
    句柄：父进程一退出，该句柄立即 signaled，PID 复用也影响不到它。
    """
    set_priority(priority)

    parent_h = _k32.OpenProcess(SYNCHRONIZE, False, parent_pid) if IS_WIN else None
    parent_ct = proc_started_at(parent_pid)
    # 注意：本机实测 os.getppid() 不一定返回真正的父 PID（venv 的 python.exe
    # 是转发器时，子进程记录的是更上一层的 PID），所以它只能当兜底，不能当
    # 判死依据。真正管用的是父进程句柄：父进程一退出，句柄立刻 signaled，
    # PID 复用也影响不到它。
    if parent_h is None and (not parent_pid or os.getppid() != parent_pid):
        os._exit(0)

    cycle_s = 0.010                      # 10ms 一个周期，duty<1 时按比例休眠
    burn_s = cycle_s * min(1.0, max(0.0, duty))
    next_parent_check = time.perf_counter() + 0.5

    while True:
        now = time.perf_counter()
        if now >= next_parent_check:
            dead = False
            if parent_h:
                dead = _k32.WaitForSingleObject(parent_h, 0) != WAIT_TIMEOUT
            else:
                # 拿不到句柄（老系统/权限）才退回 getppid
                dead = os.getppid() != parent_pid
            if dead or (stop_file and os.path.exists(stop_file)):
                if dead:
                    remove_own_registry(parent_pid, parent_ct)
                os._exit(0)
            next_parent_check = time.perf_counter() + 0.5

        end = time.perf_counter() + burn_s
        while time.perf_counter() < end:
            pass
        if burn_s < cycle_s:
            time.sleep(cycle_s - burn_s)


# ------------------------------------------------------------------ 父进程

class LoadGen:
    def __init__(self, workers: int, duration: float, target_cpu: float | None,
                 priority: str):
        self.workers = max(1, workers)
        self.duration = max(0.1, duration)
        self.target_cpu = target_cpu
        self.priority = priority
        self.start = time.perf_counter()
        self.stop_file = stop_path_for(os.getpid())
        self.children: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self._done = False

        total_cores = cpu_count()
        if target_cpu is None:
            self.duty = 1.0
        else:
            per = (target_cpu / 100.0) * total_cores / self.workers
            self.duty = min(1.0, max(0.0, per))

    # ---- 登记 / 注销

    def register(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "workers": self.workers,
            "duration": self.duration,
            "parent_pid": os.getppid(),
            "priority": self.priority,
            "target_cpu": self.target_cpu,
            "duty": round(self.duty, 4),
            "child_pids": [],
        }
        registry_path(os.getpid()).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def update_children(self) -> None:
        payload = _read_json(registry_path(os.getpid())) or {}
        payload["child_pids"] = [p.pid for p in self.children]
        try:
            registry_path(os.getpid()).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def unregister(self) -> None:
        for f in (registry_path(os.getpid()), self.stop_file):
            try:
                f.unlink()
            except OSError:
                pass

    # ---- 生命周期

    def start_workers(self) -> None:
        script = os.path.abspath(__file__)
        for _ in range(self.workers):
            self.children.append(subprocess.Popen(
                [sys.executable, script, "--_worker", str(os.getpid()),
                 f"{self.duty:.6f}", self.priority, str(self.stop_file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW if IS_WIN else 0))
        self.update_children()

    def shutdown(self, code: int = 0, reason: str = "done") -> None:
        """幂等清理：写 stop 文件、结束子 worker、删 pid 文件、打印摘要。"""
        with self._lock:
            if self._done:
                return
            self._done = True
        try:
            self.stop_file.write_text(reason, encoding="utf-8")
        except OSError:
            pass
        # 先礼后兵：worker 每 0.5s 看一次 stop 文件，给它 1.5s 自己走
        deadline = time.perf_counter() + 1.5
        while time.perf_counter() < deadline and any(p.poll() is None for p in self.children):
            time.sleep(0.05)
        for p in self.children:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:              # noqa: BLE001
                    pass
        for p in self.children:
            try:
                p.wait(1.0)
            except Exception:                  # noqa: BLE001
                try:
                    p.kill()
                    p.wait(1.0)
                except Exception:              # noqa: BLE001
                    pass
        self.unregister()
        elapsed = time.perf_counter() - self.start
        survivors = sum(1 for p in self.children if p.poll() is None)
        cleaned = "ok" if survivors == 0 else f"leak={survivors}"
        print(f"[load_gen] pid={os.getpid()} workers={len(self.children)} "
              f"elapsed={elapsed:.2f}s cleaned={cleaned}", flush=True)
        if reason != "done":
            print(f"[load_gen] 退出原因: {reason}", flush=True)
        os._exit(code)

    def run(self) -> None:
        atexit.register(self.shutdown)
        for sig in ("SIGTERM", "SIGBREAK", "SIGINT"):
            s = getattr(signal, sig, None)
            if s is None:
                continue
            try:
                signal.signal(s, lambda _s, _f: self.shutdown(130, f"signal {_s}"))
            except (ValueError, OSError):
                pass

        self.register()
        real_prio = set_priority(self.priority)
        self.start_workers()

        print(f"[load_gen] start pid={os.getpid()} workers={self.workers} "
              f"duration={self.duration:g}s priority={real_prio} "
              f"duty={self.duty:.3f}"
              + (f" target_cpu={self.target_cpu:g}%" if self.target_cpu else ""),
              flush=True)
        print(f"[load_gen] pid_file={registry_path(os.getpid())}", flush=True)

        def watchdog():
            # 硬超时：到点无条件退出，不看主线程脸色
            remaining = self.duration
            while remaining > 0:
                time.sleep(min(0.05, remaining))
                remaining = self.duration - (time.perf_counter() - self.start)
            self.shutdown(0, "done")

        threading.Thread(target=watchdog, name="load_gen_watchdog",
                         daemon=True).start()

        try:
            # 主线程只负责守夜；真正退出由 watchdog 兜底
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.shutdown(130, "KeyboardInterrupt")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 内部子 worker 模式，不走 argparse（--duration 对子进程无意义）
    if argv and argv[0] == "--_worker":
        parent_pid = int(argv[1])
        duty = float(argv[2])
        priority = argv[3]
        stop_file = argv[4]
        worker_main(parent_pid, duty, priority, stop_file)
        return 0

    ap = argparse.ArgumentParser(description="dafors 规范负载发生器")
    ap.add_argument("--workers", type=int, default=1, help="worker 进程数（默认 1）")
    ap.add_argument("--duration", type=float, required=True,
                    help="硬超时秒数，到点强制退出（必填）")
    ap.add_argument("--target-cpu", type=float, default=None,
                    help="目标 CPU 百分比（整机口径），按占空比调节而不是跑满")
    ap.add_argument("--priority", choices=["below_normal", "idle"], default="below_normal",
                    help="进程优先级，默认 below_normal")
    args = ap.parse_args(argv)

    if args.duration <= 0:
        ap.error("--duration 必须 > 0")
    if not (0 < (args.target_cpu or 100) <= 100):
        ap.error("--target-cpu 必须在 (0, 100] 内")

    gen = LoadGen(args.workers, args.duration, args.target_cpu, args.priority)
    try:
        sweep_stale()
        gen.run()
    except Exception:                          # noqa: BLE001
        gen.shutdown(1, "exception")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
