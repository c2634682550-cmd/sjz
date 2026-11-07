"""shutdown 阶段归因探针：量「到底是哪一步在冻结」。

用法（在仓库根执行）
--------------------
    .venv\\Scripts\\python.exe tests/_isolation/shutdown_probe.py --probe-timeout 120 -- tests/test_console_api.py::test_preflight_reports_every_item_with_a_fix -q

也可以把本文件当 pytest 插件用（`-p shutdown_probe`，需把 tests/_isolation 放进 PYTHONPATH）：
每个阶段都会往 stderr 打一行 ``PROBE <秒> <事件> <JSON>``，父进程据此算阶段耗时。

它只观测、不改行为：包住 ``FileLock.acquire/release/_break_stale`` 与
``record_wait`` 记录耗时；在 configure / sessionfinish / unconfigure / atexit
各拍一张「线程 + 句柄数」快照。父进程负责硬超时与 kill。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import time

_T0 = time.monotonic()
_EVENTS: list[dict] = []


def _dt() -> float:
    return time.monotonic() - _T0


def _emit(event: str, **extra) -> None:
    rec = {"dt": round(_dt(), 4), "event": event, **extra}
    _EVENTS.append(rec)
    sys.stderr.write("PROBE %.4f %s %s\n"
                     % (rec["dt"], event, json.dumps(extra, ensure_ascii=False)))
    sys.stderr.flush()


def _thread_snapshot() -> list[dict]:
    import threading
    return [{"name": t.name, "daemon": bool(t.daemon), "alive": bool(t.is_alive())}
            for t in threading.enumerate()]


def _handle_count() -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        n = ctypes.c_uint32(0)
        if k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
    except Exception:
        pass
    return None


def _patch_locks() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    tests_dir = os.path.dirname(here)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    try:
        from _isolation import lock as L
    except Exception as exc:                                  # noqa: BLE001
        _emit("lock.patch_failed", error=repr(exc))
        return

    def wrap(name: str, label: str) -> None:
        orig = getattr(L.FileLock, name)

        def wrapper(self, *a, **k):
            t = time.monotonic()
            try:
                return orig(self, *a, **k)
            finally:
                _emit(label, lock=getattr(self, "name", "?"),
                      ms=round((time.monotonic() - t) * 1000.0, 2))

        wrapper.__name__ = getattr(orig, "__name__", name)
        setattr(L.FileLock, name, wrapper)

    wrap("acquire", "lock.acquire")
    wrap("release", "lock.release")
    wrap("_break_stale", "lock.break_stale")

    orig_record = L.record_wait

    def record_wait(entry):
        t = time.monotonic()
        try:
            return orig_record(entry)
        finally:
            _emit("lock.record_wait", ms=round((time.monotonic() - t) * 1000.0, 2))

    L.record_wait = record_wait
    _emit("lock.patched")


# --------------------------------------------------------------------------- #
# pytest 插件钩子
# --------------------------------------------------------------------------- #
def _apply_disable(kinds: set[str]) -> None:
    """按 SJZ_PROBE_DISABLE 关掉某条外部依赖，用来二分定位冻结来源。

    只在本探针子进程里生效，不改仓库代码。
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        if "ocr" in kinds:
            from core import ocr
            ocr.available = lambda: False
            ocr.backend_name = lambda: "disabled-by-probe"
            _emit("disable.ocr")
        if "ocr_service" in kinds:
            from core import ocr_service
            ocr_service.current = lambda: None
            _emit("disable.ocr_service")
        if "screen" in kinds:
            from core import screen

            class _StubScreen:
                def __init__(self, *a, **k):
                    self.size = (1920, 1080)

                def grab(self):
                    raise RuntimeError("screen disabled by probe")

                def close(self):
                    pass

                def norm_to_px(self, box):
                    return (0, 0, 0, 0)

            screen.Screen = _StubScreen
            _emit("disable.screen")
        if "driver" in kinds:
            from core import interception
            interception.driver_ready = lambda: (False, "disabled-by-probe")
            _emit("disable.driver")
        if "filter_audit" in kinds:
            # get_filter 是唯一会让解释器 finalize 卡死的调用（实测）。
            # 这里只替换审计结果，不碰 context 创建，用来验证「不调它」即可
            # 消除 shutdown 冻结。
            from core import interception
            interception.filter_audit = lambda: {d: 0 for d in range(1, 21 + 1)}
            _emit("disable.filter_audit")
        if "timesync" in kinds:
            from core import timesync
            timesync.info = lambda: {"offset_s": 0.0, "synced": False,
                                     "source": "disabled-by-probe"}
            _emit("disable.timesync")
        if "input" in kinds:
            from core import input as core_input
            core_input.game_integrity = lambda: {"level": None, "why": "disabled"}
            core_input.process_integrity = lambda: None
            core_input.find_game_window = lambda: None
            _emit("disable.input")
    except Exception as exc:                                  # noqa: BLE001
        _emit("disable.failed", kinds=sorted(kinds), error=repr(exc))


def pytest_configure(config):
    _emit("configure", thread_count=len(_thread_snapshot()), handles=_handle_count())
    _patch_locks()
    kinds = {k.strip() for k in os.environ.get("SJZ_PROBE_DISABLE", "").split(",") if k.strip()}
    if kinds:
        _apply_disable(kinds)
    # 每 10s 把当前 Python 栈打到 stderr：进程卡在 finalize / __del__ 时
    # 这些 dump 就是唯一能拿到的现场。
    try:
        import faulthandler
        faulthandler.enable()
        faulthandler.dump_traceback_later(10.0, repeat=True)
        _emit("faulthandler.enabled")
    except Exception as exc:                                  # noqa: BLE001
        _emit("faulthandler.failed", error=repr(exc))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # 这个钩子在 conftest 的汇总打印之后执行（插件后注册 → 先调用）
    _emit("terminal_summary", exitstatus=int(exitstatus))


def pytest_sessionfinish(session, exitstatus):
    _emit("sessionfinish", exitstatus=int(exitstatus),
          threads=_thread_snapshot(), handles=_handle_count())


def pytest_unconfigure(config):
    _emit("unconfigure", threads=_thread_snapshot(), handles=_handle_count())


@atexit.register
def _on_atexit():
    _emit("atexit", threads=_thread_snapshot(), handles=_handle_count())
    if os.environ.get("SJZ_PROBE_CLOSE_INTERCEPTION") == "1":
        t0 = time.monotonic()
        try:
            from core import interception
            interception.shared().close()
            _emit("interception.closed",
                  ms=round((time.monotonic() - t0) * 1000.0, 2))
        except Exception as exc:                              # noqa: BLE001
            _emit("interception.close_failed", error=repr(exc),
                  ms=round((time.monotonic() - t0) * 1000.0, 2))


# --------------------------------------------------------------------------- #
# 运行器
# --------------------------------------------------------------------------- #
def _hidden_kwargs() -> dict:
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return {"startupinfo": si,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _duration_from_stdout(out: str) -> float | None:
    matches = re.findall(r"in ([0-9.]+)s(?: \([0-9:]+\))?", out or "")
    return float(matches[-1]) if matches else None


def _parse_events(err: str) -> list[dict]:
    events = []
    for line in (err or "").splitlines():
        if not line.startswith("PROBE "):
            continue
        try:
            _, dt, event, raw = line.split(" ", 3)
            events.append({"dt": float(dt), "event": event,
                           "data": json.loads(raw or "{}")})
        except ValueError:
            continue
    return events


def _lock_ops_after(events: list[dict], event: str) -> list[dict]:
    marker = next((e for e in reversed(events) if e["event"] == event), None)
    if not marker:
        return []
    return [e for e in events if e["dt"] >= marker["dt"]
            and e["event"].startswith("lock.")]


def _kill_tree(pid: int) -> None:
    """杀整棵进程树。venv 的 python.exe 会再 exec 一个 base 解释器，
    只 kill 直接子进程会留下孙进程占着输出管道，导致父进程永远等不到 EOF。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, **_hidden_kwargs())
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def run(pytest_args: list[str], timeout: float, keep_dir: str | None = None) -> dict:
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["SJZ_TEST_RUN_ID"] = "shutdown-probe-%d" % os.getpid()
    # 直接起 base 解释器，避免 venv shim 再 exec 一层；用 __PYVENV_LAUNCHER__
    # 让 base 解释器仍按 venv 的 site-packages 找 pytest。
    launcher = sys.executable
    base = getattr(sys, "_base_executable", "") or launcher
    if os.path.normcase(base) != os.path.normcase(launcher):
        env["__PYVENV_LAUNCHER__"] = launcher
    cmd = [base, "-m", "pytest", "-p", "shutdown_probe"] + pytest_args

    # 输出写文件而不是 PIPE：孙进程继承管道写端时，kill 后 communicate 会
    # 永远等等不到 EOF。文件没有这个坑。
    import tempfile
    fd_out, out_path = tempfile.mkstemp(prefix="shutdown-probe-", suffix=".out")
    fd_err, err_path = tempfile.mkstemp(prefix="shutdown-probe-", suffix=".err")
    os.close(fd_out)
    os.close(fd_err)
    t0 = time.monotonic()
    timed_out = False
    with open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
        proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=fout, stderr=ferr,
                                **_hidden_kwargs())
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    wall = time.monotonic() - t0
    exit_code = proc.returncode
    try:
        with open(out_path, encoding="utf-8", errors="replace") as f:
            out = f.read()
        with open(err_path, encoding="utf-8", errors="replace") as f:
            err = f.read()
        if keep_dir:
            import shutil
            os.makedirs(keep_dir, exist_ok=True)
            shutil.copy2(out_path, os.path.join(keep_dir, "pytest.out"))
            shutil.copy2(err_path, os.path.join(keep_dir, "pytest.err"))
    finally:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    events = _parse_events(err)
    atexit_dt = next((e["dt"] for e in reversed(events) if e["event"] == "atexit"), None)
    exit_gap = round(wall - atexit_dt, 3) if atexit_dt is not None else None
    post_summary_locks = _lock_ops_after(events, "terminal_summary")
    non_daemon = []
    for e in events:
        snap = e.get("data", {}).get("threads")
        if not isinstance(snap, list):
            continue
        for t in snap:
            if not t["daemon"] and t["alive"] and t["name"] != "MainThread" \
                    and t not in non_daemon:
                non_daemon.append(t)
    at_exit = next((e for e in reversed(events) if e["event"] == "atexit"), {})
    threads_at_exit = at_exit.get("data", {}).get("threads") or []

    stage_dt = {}
    for name in ("terminal_summary", "sessionfinish", "unconfigure", "atexit"):
        found = next((e["dt"] for e in reversed(events) if e["event"] == name), None)
        stage_dt[name] = found
    summary_to_atexit = (round(stage_dt["atexit"] - stage_dt["terminal_summary"], 3)
                         if stage_dt.get("atexit") is not None
                         and stage_dt.get("terminal_summary") is not None else None)
    report = {
        "cmd": pytest_args,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "wall_s": round(wall, 3),
        "pytest_duration_s": _duration_from_stdout(out),
        "stage_dt": stage_dt,
        "summary_to_atexit_s": summary_to_atexit,
        "atexit_to_process_exit_s": exit_gap,
        "post_summary_lock_ops": [
            {"event": e["event"], "lock": e["data"].get("lock"), "ms": e["data"].get("ms")}
            for e in post_summary_locks
        ],
        "non_daemon_threads": non_daemon,
        "threads_at_exit": threads_at_exit,
        "handles": {e["event"]: e["data"].get("handles")
                    for e in events if e["data"].get("handles") is not None},
        "events": events,
    }
    return report


def _fmt(value, unit="s"):
    return "n/a" if value is None else ("%.3f%s" % (value, unit))


def _print_report(rep: dict) -> None:
    print("=" * 72)
    print("cmd      :", " ".join(rep["cmd"]))
    print("exit=%s timed_out=%s wall=%.3fs pytest_duration=%s"
          % (rep["exit_code"], rep["timed_out"], rep["wall_s"],
             rep["pytest_duration_s"]))
    sd = rep["stage_dt"]
    print("阶段时间点: terminal_summary=%s sessionfinish=%s unconfigure=%s atexit=%s" % (
        _fmt(sd.get("terminal_summary")),
        _fmt(sd.get("sessionfinish")),
        _fmt(sd.get("unconfigure")),
        _fmt(sd.get("atexit")),
    ))
    print("summary→atexit=%s | atexit→进程退出=%s"
          % (_fmt(rep["summary_to_atexit_s"]), _fmt(rep["atexit_to_process_exit_s"])))
    print("汇总之后仍有锁操作：%s" % json.dumps(rep["post_summary_lock_ops"], ensure_ascii=False))
    print("非 daemon 线程：%s" % json.dumps(rep["non_daemon_threads"], ensure_ascii=False))
    print("atexit 时线程：%s" % json.dumps(rep["threads_at_exit"], ensure_ascii=False))
    print("句柄数：%s" % json.dumps(rep["handles"], ensure_ascii=False))
    print("=" * 72)


def main(argv: list[str]) -> int:
    timeout = 120.0
    json_path = None
    keep_dir = None
    rest = list(argv)
    if "--probe-timeout" in rest:
        i = rest.index("--probe-timeout")
        timeout = float(rest[i + 1])
        del rest[i:i + 2]
    if "--probe-json" in rest:
        i = rest.index("--probe-json")
        json_path = rest[i + 1]
        del rest[i:i + 2]
    if "--probe-keep" in rest:
        i = rest.index("--probe-keep")
        keep_dir = rest[i + 1]
        del rest[i:i + 2]
    if "--" in rest:
        rest = rest[rest.index("--") + 1:]
    rep = run(rest, timeout, keep_dir)
    _print_report(rep)
    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    return 0 if rep["exit_code"] == 0 and not rep["timed_out"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
