"""进程面：单实例、参数传递、子进程退出被感知、无孤儿、控制面鉴权。"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import (LAUNCHER, ROOT, run_cli, pid_alive, sandbox, run_sandbox,
                      set_config, put_license, payload_for, sign)

CMD = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- 单实例

def test_global_mutex_blocks_second_instance():
    import ctypes
    k32 = ctypes.windll.kernel32
    name = r"Global\SjzLauncher.Console.v1"
    h = k32.CreateMutexW(None, True, name)
    assert h, "无法创建全局互斥体"
    try:
        code, data, raw = run_cli(["--mutex-probe"])
        assert code == 1 and data["acquired"] is False, raw
    finally:
        k32.CloseHandle(h)
    code2, data2, raw2 = run_cli(["--mutex-probe"])
    assert code2 == 0 and data2["acquired"] is True, raw2


# ---------------------------------------------------------------- 子进程守护

def _events(raw):
    import json
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def test_child_exit_is_detected_and_restarted():
    code, data, raw = run_cli(
        ["--spawn", "--spawn-exe", CMD, "--seconds", "8",
         "--restart-max", "2", "--restart-backoff", "100",
         "--", "/c", "exit", "3"], timeout=40)
    ev = _events(raw)
    spawns = [e for e in ev if e.get("event") == "spawn"]
    exits = [e for e in ev if e.get("event") == "exit"]
    done = [e for e in ev if e.get("event") == "done"]
    assert code == 0 and done, raw
    assert len(spawns) == 3, raw                      # 1 次 + 2 次重启
    assert all(e["code"] == 3 for e in exits)
    assert done[0]["spawns"] == 3


def test_no_orphan_after_launcher_exit():
    """子进程再拉一层（cmd → timeout.exe），启动器退出后两层都得没。"""
    child_pid, grand_pid = 0, 0
    proc = subprocess.Popen(
        [str(LAUNCHER / "SjzLauncherTest.exe"), "--spawn", "--spawn-exe", CMD,
         "--seconds", "3", "--", "/c", "timeout", "/t", "20", "/nobreak"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=str(ROOT))
    deadline = time.time() + 10
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            m = re.search(r'"event":"spawn","pid":(\d+)', line)
            if m:
                child_pid = int(m.group(1))
                break
        assert child_pid, "没拿到子进程 pid"
        time.sleep(1.0)
        ps = ("(Get-CimInstance Win32_Process -Filter 'ParentProcessId=%d')"
              ".ProcessId" % child_pid)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
        grand_pid = int(out.strip().splitlines()[0]) if out.strip() else 0
        assert grand_pid, "没拿到孙进程 pid（cmd 没起 timeout）"
    finally:
        try:
            rest, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rest, _ = proc.communicate()
    assert '"event":"kill_tree"' in rest or "done" in rest
    time.sleep(0.6)
    assert not pid_alive(child_pid), "子进程成了孤儿"
    assert not pid_alive(grand_pid), "孙进程成了孤儿"


# ---------------------------------------------------------------- 控制面（真实 server.py）

@pytest.fixture
def real_ctrl():
    from http.server import ThreadingHTTPServer
    from core import server as srv

    tok = "t" * 32
    srv.set_control_token(tok)
    old_summary = srv._license.summary
    old_shutdown = srv._shutdown_soon
    srv._license.summary = lambda: {}
    srv._shutdown_soon = lambda reason="quit": None       # 别真把 pytest 杀了
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    assert wait_port(port)
    try:
        yield port, tok
    finally:
        httpd.shutdown()
        httpd.server_close()
        srv.set_control_token(None)
        srv._license.summary = old_summary
        srv._shutdown_soon = old_shutdown


def test_ctrl_no_token_rejected(real_ctrl):
    port, _tok = real_ctrl
    code, data, raw = run_cli(["--ctrl", "--port", str(port), "--token", ""])
    assert code == 1 and data["ok"] is False, raw


def test_ctrl_wrong_token_rejected(real_ctrl):
    port, _tok = real_ctrl
    code, data, raw = run_cli(["--ctrl", "--port", str(port), "--token", "x" * 32])
    assert code == 1 and data["ok"] is False, raw


def test_ctrl_right_token_passes(real_ctrl):
    port, tok = real_ctrl
    code, data, raw = run_cli(["--ctrl", "--port", str(port), "--token", tok])
    assert code == 0 and data["ok"] is True, raw
    assert data["status"]["pid"] == os.getpid()
    assert data["status"]["auth"] is True


def test_ctrl_stop_route(real_ctrl):
    port, tok = real_ctrl
    code, data, raw = run_cli(["--ctrl-stop", "--port", str(port), "--token", tok])
    assert code == 0 and data["ok"] is True, raw
    code2, data2, _ = run_cli(["--ctrl-stop", "--port", str(port), "--token", "bad"])
    assert code2 == 1 and data2["ok"] is False


def test_page_seeds_cookie_and_cookie_is_accepted(real_ctrl):
    port, tok = real_ctrl
    req = urllib.request.Request("http://127.0.0.1:%d/" % port)
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
        cookie = resp.headers.get("Set-Cookie") or ""
    assert "sjz_ctrl=" in cookie and "HttpOnly" in cookie
    assert b"<" in raw

    req2 = urllib.request.Request(
        "http://127.0.0.1:%d/api/ctrl/status" % port,
        headers={"Cookie": cookie.split(";")[0]})
    with urllib.request.urlopen(req2, timeout=5) as resp:
        assert resp.status == 200
    # 没有 cookie 也没有头：403
    req3 = urllib.request.Request("http://127.0.0.1:%d/api/ctrl/status" % port)
    try:
        urllib.request.urlopen(req3, timeout=5)
        raise AssertionError("未鉴权请求被放行")
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_non_loopback_host_rejected(real_ctrl):
    """DNS rebinding 防御：Host 不是回环名，即使带了正确 token 也拒。"""
    import http.client
    port, tok = real_ctrl
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("GET", "/api/ctrl/status", skip_host=True)
    conn.putheader("Host", "evil.example.com")
    conn.putheader("X-SJZ-Token", tok)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 403
    conn.close()


# ---------------------------------------------------------------- 端到端（沙箱）

def test_headless_end_to_end(sandbox, fp):
    """启动器 → pythonw → 控制面握手 → 查状态 → token 要求停止 → 正常退出。"""
    port = free_port()
    script = str((LAUNCHER / "tests" / "fake_desktop.py").resolve())
    pythonw = str((ROOT / ".venv" / "Scripts" / "pythonw.exe").resolve())
    set_config(sandbox, python=pythonw, script=script, ctrl_port=port,
               max_restarts=2)
    put_license(sandbox, payload_for(fp["vec"]))

    code, events, raw = run_sandbox(
        sandbox, ["--headless", "--seconds", "2", "--ignore-preflight-fail"],
        timeout=90)
    by = {e["event"]: e for e in events}
    assert "license" in by and by["license"]["ok"] is True, raw
    assert by["license"]["status"] == "ok"
    assert "spawn" in by, raw
    assert "ready" in by and by["ready"]["ok"] is True, raw
    assert by["ready"]["port"] == port
    assert by["ready"]["status"]["python_pid"] == by["spawn"]["pid"]
    assert "stopped" in by and by["stopped"]["forced"] is False, raw
    assert not pid_alive(by["spawn"]["pid"])
    assert code == 0, raw
