"""tools/load_gen.py 的验收测试。

三条硬要求：
  1. --workers 2 --duration 2 自己退出、退出码 0、pid 文件被删；
  2. 父进程被强杀后，子 worker 5 秒内自杀（不留僵尸）；
  3. 父子进程优先级确实是 BELOW_NORMAL / IDLE。

测试本身必须干净退出：跑完不留任何 load_gen 进程、不留 pid 文件。

注意：本机 `.venv\\Scripts\\python.exe` 是 uv 生成的转发器（trampoline），
subprocess.Popen 拿到的 PID 是转发器，不是真正跑 load_gen 的进程。所以
所有断言都用 load_gen 自己打印的 `pid_file=<pid>.json` 里的真实 PID。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOAD_GEN = ROOT / "tools" / "load_gen.py"
TEMP_DIR = Path(os.environ.get("TEMP") or ".") / "dafors_load"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = ctypes.c_void_p
k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
k32.GetPriorityClass.restype = wintypes.DWORD
k32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
k32.CloseHandle.argtypes = [ctypes.c_void_p]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259
PRIO_IDLE = 0x00000040
PRIO_BELOW_NORMAL = 0x00004000


def is_alive(pid: int) -> bool:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def priority_class(pid: int) -> int | None:
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        return int(k32.GetPriorityClass(h))
    finally:
        k32.CloseHandle(h)


def hard_kill(pid: int) -> bool:
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 1))
    finally:
        k32.CloseHandle(h)


def wait_until(pred, timeout_s: float, interval_s: float = 0.1) -> bool:
    end = time.perf_counter() + timeout_s
    while time.perf_counter() < end:
        if pred():
            return True
        time.sleep(interval_s)
    return pred()


class Run:
    """一次 load_gen 启动，自动从 stdout 解析出真实 PID。"""

    def __init__(self, *args: str):
        self.proc = subprocess.Popen(
            [sys.executable, str(LOAD_GEN), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=str(ROOT))
        self.lines: list[str] = []
        self.real_pid: int | None = None
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.lines.append(line)
            m = re.search(r"pid_file=.*[\\/](\d+)\.json", line)
            if m:
                self.real_pid = int(m.group(1))

    def wait_pid(self, timeout_s: float = 10.0) -> int:
        assert wait_until(lambda: self.real_pid is not None, timeout_s), \
            f"没等到 pid_file 输出，已收到：{self.lines}"
        return int(self.real_pid)

    def registry(self) -> dict | None:
        try:
            return json.loads((TEMP_DIR / f"{self.real_pid}.json").read_text(encoding="utf-8"))
        except Exception:                      # noqa: BLE001
            return None

    def wait_children(self, expect: int, timeout_s: float = 10.0) -> list[int]:
        self.wait_pid()
        box: dict = {}

        def ok():
            info = self.registry()
            if info and len(info.get("child_pids") or []) >= expect:
                box["info"] = info
                return True
            return False

        assert wait_until(ok, timeout_s), f"pid 文件始终没有登记 {expect} 个 worker"
        return [int(c) for c in box["info"]["child_pids"]]

    def kill(self) -> None:
        if self.real_pid and is_alive(self.real_pid):
            hard_kill(self.real_pid)
        if self.proc.poll() is None:
            hard_kill(self.proc.pid)


@pytest.fixture
def spawned():
    runs: list[Run] = []
    yield runs
    for r in runs:
        r.kill()
        try:
            r.proc.wait(3)
        except subprocess.TimeoutExpired:
            r.proc.kill()
            r.proc.wait(3)


def test_self_exit_and_registry_cleanup(spawned):
    """① --workers 2 --duration 2：2 秒内退出、退出码 0、pid 文件删除。"""
    r = Run("--workers", "2", "--duration", "2")
    spawned.append(r)
    t0 = time.perf_counter()
    out_code = r.proc.wait(timeout=20)
    elapsed = time.perf_counter() - t0
    real_pid = r.wait_pid()

    assert out_code == 0, f"退出码 {out_code}，输出={r.lines}"
    assert 1.5 <= elapsed <= 2 + 4.0, f"退出耗时 {elapsed:.2f}s 不符合 2s 硬超时"
    text = "\n".join(r.lines)
    assert "[load_gen]" in text and "cleaned=ok" in text, text
    assert not (TEMP_DIR / f"{real_pid}.json").exists(), "退出后 pid 文件没有删除"


def test_children_suicide_when_parent_killed(spawned):
    """② 强杀父进程：子 worker 5 秒内自杀，pid 文件被 worker 顺手删掉。"""
    r = Run("--workers", "2", "--duration", "60")
    spawned.append(r)
    children = r.wait_children(2)
    assert len(children) == 2
    assert wait_until(lambda: all(is_alive(c) for c in children), 4.0), "worker 没起来"

    assert hard_kill(r.real_pid), "强杀父进程失败"
    r.proc.wait(5)

    assert wait_until(lambda: not any(is_alive(c) for c in children), 5.0), \
        f"父进程死后仍有 worker 存活：{[c for c in children if is_alive(c)]}"
    assert wait_until(lambda: not (TEMP_DIR / f"{r.real_pid}.json").exists(), 5.0), \
        "父进程死后 pid 文件没有被清理"


def test_priority_is_low(spawned):
    """③ 父子进程优先级都必须是 BELOW_NORMAL 或 IDLE。"""
    r = Run("--workers", "2", "--duration", "8", "--priority", "below_normal")
    spawned.append(r)
    children = r.wait_children(2)

    parent_prio = priority_class(r.real_pid)
    assert parent_prio in (PRIO_BELOW_NORMAL, PRIO_IDLE), hex(parent_prio or 0)

    for c in children:
        assert wait_until(lambda c=c: priority_class(c) is not None, 4.0)
        prio = priority_class(c)
        assert prio in (PRIO_BELOW_NORMAL, PRIO_IDLE), f"worker {c} 优先级 {hex(prio or 0)}"


def test_idle_priority_accepted(spawned):
    """--priority idle 也必须可跑并能干净退出。"""
    r = Run("--workers", "1", "--duration", "1", "--priority", "idle")
    spawned.append(r)
    assert r.proc.wait(timeout=20) == 0, r.lines
    assert "priority=idle" in "\n".join(r.lines), r.lines


def test_no_leftovers_after_suite():
    """跑完不留 load_gen 进程，也不留 pid 文件。"""
    stray = []
    for f in TEMP_DIR.glob("*.json"):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                      # noqa: BLE001
            continue
        pid = info.get("pid")
        if isinstance(pid, int) and is_alive(pid):
            stray.append(pid)
    assert not stray, f"仍有 load_gen 父进程存活：{stray}"
    assert not list(TEMP_DIR.glob("*.stop")), "残留 stop 文件"
