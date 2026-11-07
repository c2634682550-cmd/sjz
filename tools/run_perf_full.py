# -*- coding: utf-8 -*-
"""隐藏窗口跑 perf/bench/flow_bench.py，并记录当时的 Python 进程数与 CPU 占用。

为什么单独一个壳：
  - 采集进程带 CREATE_NO_WINDOW + SW_HIDE，桌面不留可见控制台；
  - 子进程带硬超时（默认 900s），超时 kill 并确认退出，不留僵尸；
  - 报告任何数字时同时给出「当时 python 进程数」和「CPU 占用」，否则数字不用于决策。

用法：
    .venv\\Scripts\\pythonw.exe tools\\run_perf_full.py --run 40 --root perf/merged ^
        --out perf\\out\\flow_full_20261107.json
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

CREATE_NO_WINDOW = 0x08000000
SW_HIDE = 0

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(PROJ, ".venv", "Scripts", "python.exe")


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong)]


# --- Windows Job Object：确保超时/退出时整棵进程树一起结束 ---
class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9


def make_job():
    k32 = ctypes.windll.kernel32
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        k32.CloseHandle(job)
        return None
    return job


def kill_job(job):
    if job:
        ctypes.windll.kernel32.TerminateJobObject(job, 1)


def _ft(v):
    return (v.dwHighDateTime << 32) | v.dwLowDateTime


def cpu_times():
    """(idle, kernel, user) 累计 100ns；kernel 含 idle。"""
    idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        raise ctypes.WinError()
    return _ft(idle), _ft(kern), _ft(user)


def cpu_pct():
    """用 1 秒窗口算当前整机 CPU 占用。"""
    a = cpu_times()
    time.sleep(1.0)
    b = cpu_times()
    di = b[0] - a[0]
    dt = (b[1] - a[1]) + (b[2] - a[2])
    return 0.0 if dt <= 0 else round(100.0 * (1.0 - di / float(dt)), 1)


def python_procs():
    """tasklist 统计 python.exe / pythonw.exe 进程数（隐藏窗口调用）。"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=flags, startupinfo=si,
        timeout=15).stdout
    out += subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq pythonw.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=flags, startupinfo=si,
        timeout=15).stdout
    return sum(1 for ln in out.splitlines() if ".exe" in ln.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="perf/merged")
    ap.add_argument("--run", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    outset = os.path.join(PROJ, args.out) if not os.path.isabs(args.out) else args.out
    logp = outset + ".log"
    envp = outset + ".env.json"
    os.makedirs(os.path.dirname(outset), exist_ok=True)

    cmd = [PY, os.path.join(PROJ, "perf", "bench", "flow_bench.py"),
           "--root", args.root, "--run", str(args.run), "--seed", str(args.seed),
           "--out", outset]
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_HIDE

    env = {"cmd": cmd, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "python_procs_before": python_procs(), "cpu_window_pct": cpu_pct(),
           "cpu_samples": [], "python_procs_samples": []}

    t0 = time.time()
    with open(logp, "w", encoding="utf-8") as lf:
        p = subprocess.Popen(cmd, cwd=PROJ, stdout=lf, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW, startupinfo=si)
        job = make_job()
        if job:
            ctypes.windll.kernel32.AssignProcessToJobObject(job, int(p._handle))
        samples = []
        killed = False
        last_cpu = cpu_times()
        while p.poll() is None:
            time.sleep(3.0)
            now = cpu_times()
            di = now[0] - last_cpu[0]
            dt = (now[1] - last_cpu[1]) + (now[2] - last_cpu[2])
            pct = 0.0 if dt <= 0 else round(100.0 * (1.0 - di / float(dt)), 1)
            last_cpu = now
            samples.append(pct)
            env["python_procs_samples"].append(python_procs())
            if time.time() - t0 > args.timeout:
                # venv 的 python.exe 是启动壳，真正解释器是其子进程；杀整棵 Job 树
                kill_job(job)
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True, creationflags=CREATE_NO_WINDOW,
                               startupinfo=si, timeout=30)
                killed = True
                break
        p.wait()
        if job:
            # 关 Job：KILL_ON_JOB_CLOSE 兜底清掉解释器退出时挂住的非守护线程/子进程
            ctypes.windll.kernel32.CloseHandle(job)
        time.sleep(1.0)

    env["cpu_samples"] = samples
    env["cpu_avg_pct"] = round(sum(samples) / len(samples), 1) if samples else None
    env["cpu_max_pct"] = max(samples) if samples else None
    env["python_procs_mid_max"] = max(env["python_procs_samples"]) \
        if env["python_procs_samples"] else None
    env["python_procs_after"] = python_procs()
    env["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")
    env["elapsed_s"] = round(time.time() - t0, 1)
    env["exit_code"] = p.returncode
    env["timed_out"] = killed
    env["log"] = logp
    with open(envp, "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, indent=1)

    print(json.dumps({k: env[k] for k in (
        "exit_code", "timed_out", "elapsed_s", "cpu_avg_pct", "cpu_max_pct",
        "python_procs_before", "python_procs_mid_max", "python_procs_after")},
        ensure_ascii=False))
    if killed:
        print("已超时 kill；确认退出码:", p.returncode)
        return 2
    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
