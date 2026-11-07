"""desktop.py 侧：父进程存活探测 + 父进程消失时走安全停机路径（不硬杀）。"""
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


@pytest.fixture(scope="module")
def D():
    pytest.importorskip("webview")
    import desktop
    return desktop


def test_parent_alive_true_for_self(D):
    assert D._parent_alive(os.getpid()) is True


def test_parent_alive_false_after_child_exit(D):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    time.sleep(0.2)
    assert D._parent_alive(p.pid) is False


def test_parent_alive_zero_means_unknown_is_alive(D):
    assert D._parent_alive(0) is True          # 0 = 没传，按存活处理


def test_watchdog_takes_safe_shutdown_path(D, monkeypatch):
    calls = []
    import core.server as srv

    class FakeThread(object):
        def __init__(self, target=None, name=None, daemon=None):
            self.target = target

        def start(self):
            pass                                   # 不真起线程，下面同步跑

    monkeypatch.setattr(D.threading, "Thread", FakeThread)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.setattr(D, "_parent_alive", lambda pid: False)
    monkeypatch.setattr(D, "_release_input_before_exit",
                        lambda: calls.append("release"))
    monkeypatch.setattr(srv, "_shutdown_soon",
                        lambda reason="quit": calls.append("shutdown:" + reason))

    t = D._start_parent_watchdog(os.getpid())
    t.target()                                     # 跑一轮就 return

    assert calls == ["release", "shutdown:launcher-gone"], calls
