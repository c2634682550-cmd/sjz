"""卡点进程客户端（`core/fire_proc.py`）的失败语义与竞态回归。

2026-11-07 竞态：`_spawn()` 里「睡 50ms 再看一眼 poll」只探活一次，子进程
之后再退出就没有任何路径把 `dead` 置位 —— 隔离跑绿、冻结快照全量回归红
（`test_bad_worker_path_marks_dead`）。这里按五种现场补回归：

1. 子进程异常退出（退出码非 0）；
2. 子进程正常退出（exit=0，但同样不能再发）；
3. 启动即失败（路径错 / 解释器异常）；
4. 运行中被强杀（TerminateProcess）；
5. 发送瞬间死亡（可控桩：探活通过、写管道时断），必须且只重建重试一次。

外加控制态（暂停 / 停止 / 用户中止）与 `dead` 的区分，以及原有四项失败语义。
"""

import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fire_proc import FireProcess, FireState  # noqa: E402


def _wait_until(pred, timeout=3.0, step=0.01):
    """条件等待：等异步状态出现，而不是假定它已经出现。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _worker(tmp_path, name, body):
    w = tmp_path / name
    w.write_text(body, encoding="utf-8")
    return str(w)


# ---------------------------------------------------------------- 原有语义
def test_normal_roundtrip():
    fp = FireProcess()
    try:
        assert not fp.dead, fp.error
        assert fp.usable()
        assert fp.fire(time.perf_counter() + 0.010) is True
        fired = fp.wait(1.0)
        assert fired is not None
        assert abs(fired - time.perf_counter()) < 0.5
    finally:
        fp.close()


def test_silent_worker_times_out_without_hanging(tmp_path):
    w = _worker(tmp_path, "silent.py", "import time\ntime.sleep(10)\n")
    fp = FireProcess(worker=w)
    try:
        fp.fire(time.perf_counter() + 0.01)
        t0 = time.perf_counter()
        assert fp.wait(0.2) is None, "静默 worker 不该返回结果"
        assert time.perf_counter() - t0 < 2.0, "wait 出现了死等"
        assert fp.dead, "超时后必须标记 dead"
    finally:
        fp.close()


def test_close_is_idempotent():
    fp = FireProcess()
    fp.close()
    fp.close()                          # 重复关闭不抛异常即通过
    fp.close()


# ---------------------------------------------------------------- 五种现场
def test_bad_worker_path_marks_dead():
    """场景 3：启动即失败（路径错）。"""
    fp = FireProcess(worker="Z:/definitely/not/here.py")
    try:
        assert _wait_until(lambda: fp.dead), \
            f"worker 路径错没标 dead，error={fp.error!r}"
        assert fp.exit_code is not None, f"退出码没记下来：{fp.status()}"
        assert "退出" in fp.error, fp.error
        assert fp.fire(time.perf_counter() + 0.01) is False
        assert fp.ensure_ready(rebuild=False) is False
    finally:
        fp.close()


def test_worker_crash_marks_dead_with_exit_code(tmp_path):
    """场景 1：子进程读了第一发后异常退出（exit=7）。"""
    w = _worker(tmp_path, "crash.py",
                "import sys\nsys.stdin.readline()\nsys.exit(7)\n")
    fp = FireProcess(worker=w)
    try:
        assert not fp.dead
        assert fp.fire(time.perf_counter() + 1.0) is True
        assert _wait_until(lambda: fp.dead), "崩溃后 dead 没有被置位"
        assert fp.exit_code == 7, fp.status()
        assert "exit=7" in fp.error, fp.error
        assert fp.fire(time.perf_counter() + 0.01) is False
    finally:
        fp.close()


def test_worker_normal_exit_marks_dead(tmp_path):
    """场景 2：子进程正常退出（exit=0），同样不能再发。"""
    w = _worker(tmp_path, "normal_exit.py",
                "import sys, time\ntime.sleep(0.6)\nsys.exit(0)\n")
    fp = FireProcess(worker=w)
    try:
        assert fp.usable()
        assert _wait_until(lambda: fp.dead, timeout=3.0), \
            f"正常退出后 dead 没有被置位：{fp.status()}"
        assert fp.exit_code == 0, fp.status()
        assert "正常退出" in fp.error, fp.error
        assert fp.fire(time.perf_counter() + 0.01) is False
        assert fp.ensure_ready(rebuild=False) is False
    finally:
        fp.close()


def test_worker_killed_marks_dead():
    """场景 4：运行中被强杀（TerminateProcess / SIGKILL）。"""
    fp = FireProcess()
    try:
        assert fp.usable()
        fp.p.kill()
        assert _wait_until(lambda: fp.dead), "强杀后 dead 没有被置位"
        assert fp.exit_code is not None, f"退出码没记下来：{fp.status()}"
        assert "退出" in fp.error or "信号" in fp.error, fp.error
        assert fp.fire(time.perf_counter() + 0.01) is False
    finally:
        fp.close()


class _BrokenStdin:
    """写就断的管道：模拟「探活通过，但真正发送时进程刚死」。"""

    def write(self, _s):
        raise BrokenPipeError("模拟发送瞬间进程死亡")

    def flush(self):
        raise BrokenPipeError("模拟发送瞬间进程死亡")

    def close(self):
        pass


class _DeadPopen:
    """第一次 spawn 的假子进程：poll 说活着，写管道必失败。"""

    def __init__(self):
        self.stdin = _BrokenStdin()
        self.stdout = io.StringIO("")
        self.returncode = None
        self._gone = threading.Event()   # 只有 terminate 才让它「退出」

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._gone.wait(timeout)
        self.returncode = 1
        return 1

    def terminate(self):
        self._gone.set()

    def kill(self):
        self._gone.set()


class _FlakyFireProcess(FireProcess):
    """第一次 _popen 返回假死进程，之后的 spawn 是真的 worker。"""

    def __init__(self, **kw):
        self.spawn_calls = 0
        super().__init__(**kw)

    def _popen(self):
        self.spawn_calls += 1
        if self.spawn_calls == 1:
            return _DeadPopen()
        return super()._popen()


def test_send_moment_death_rebuilds_once_and_retries():
    """场景 5：发送瞬间死亡 → 恰好一次重建重试，而不是当普通错误上抛。"""
    fp = _FlakyFireProcess()
    try:
        assert not fp.dead
        assert fp.fire(time.perf_counter() + 0.30) is True, \
            f"重建重试没有救回来：{fp.status()}"
        assert fp.spawn_calls == 2, f"应当只重建一次，实际 {fp.spawn_calls}"
        assert fp.restarts == 1
        assert fp.fire_retries == 1
        assert "重建" in fp.error, fp.error
        got = fp.wait(1.0)
        assert got is not None, "重建后的这一发没有拿到实际触发时刻"
    finally:
        fp.close()


def test_send_moment_death_near_deadline_does_not_block():
    """发送瞬间死亡但已到卡点：关键路径不重建，立刻降级。"""
    fp = _FlakyFireProcess()
    try:
        t0 = time.perf_counter()
        assert fp.fire(time.perf_counter()) is False
        assert time.perf_counter() - t0 < 0.1, "临近卡点还在重建，阻塞了关键路径"
        assert fp.spawn_calls == 1, "临近卡点不许重建"
        assert fp.dead
        assert "不重建" in fp.error, fp.error
    finally:
        fp.close()


# ------------------------------------------------------------ 控制态不是 dead
def test_pause_resume_is_not_dead():
    fp = FireProcess()
    try:
        assert fp.pause() is True
        assert fp.state is FireState.PAUSED
        assert not fp.dead, "暂停不是故障"
        assert fp.alive(), "暂停只是不发，进程还在"
        assert fp.fire(time.perf_counter() + 0.05) is False
        assert not fp.dead, "暂停期间发送被拒，不能标 dead"

        assert fp.resume() is True
        assert fp.state is FireState.READY
        assert fp.fire(time.perf_counter() + 0.02) is True
        assert fp.wait(0.5) is not None

        fp.stop()
        assert fp.state is FireState.STOPPED
        assert not fp.dead, "用户停止不是故障"
        assert not fp.alive(), "停止必须把进程收掉"
        assert fp.fire(time.perf_counter() + 0.01) is False
        assert not fp.dead
    finally:
        fp.close()


def test_abort_is_not_dead():
    fp = FireProcess()
    try:
        fp.abort()
        assert fp.state is FireState.ABORTED
        assert not fp.dead, "用户中止不是故障"
        assert not fp.alive()
        assert fp.fire(time.perf_counter() + 0.01) is False
        assert not fp.dead
    finally:
        fp.close()
