"""跨进程互斥锁（tests/_isolation/lock.py）的回归用例。

覆盖范围
--------
* 获取 / 续租 / 释放 / 超时诊断；
* 四类陈旧接管：``holder-dead``、``pid-reused``、``holder-wedged``、``corrupt-meta``；
* 两级锁（总闸 ``heavy`` → 组锁）的固定加锁顺序与死锁规避；
* 持有进程异常退出（kill / ``os._exit``）后锁能被接管；
* 残余风险里可自动验证的部分：
  - R1：所有等待都有硬上限，持锁者释放后等待者被及时唤醒（CPU 饱和版是
    显式开关的重负载用例，默认跳过）；
  - R2：用例体自身的异常原样抛出，不会被锁包装成「等锁超时」；
  - R4/续租：``renew()`` 能让长跑持锁者不被 ``holder-wedged`` 误拆；
  - R6：两个进程用同一把锁做「读-改-写」，写入不会互相覆盖。

隔离
----
全部用例把 ``root`` 指向 ``tmp_path`` 下的空目录，锁目录随之落在
``%TEMP%/sjz-pytest-isolation/<随机目录名>-<哈希>/``，跑完由夹具删除；
不读、不写真实仓库的锁与数据文件。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from _isolation import lock as iso_lock  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #
@pytest.fixture
def iso_root(tmp_path):
    """隔离的项目根：锁目录按它的路径哈希分目录，跑完整目录删除。"""
    root = tmp_path / "repo"
    root.mkdir()
    d = iso_lock.lock_dir(str(root))
    try:
        yield str(root)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def children():
    """登记本用例起的子进程，收尾一律 kill + wait，绝不留僵尸。"""
    procs = []
    yield procs
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
        try:
            p.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try:
                p.kill()
            except OSError:
                pass
        for stream in (p.stdout, p.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


def _lock_path(root: str, name: str) -> str:
    d = iso_lock.lock_dir(root)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + ".lock")


def _write_meta(path: str, meta: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def _fake_meta(pid: int, **kw) -> dict:
    base = {
        "version": iso_lock.VERSION,
        "token": "fake-token",
        "name": "fake",
        "pid": pid,
        "pid_start": iso_lock.pid_start_marker(pid),
        "groups": ["timing"],
        "nodeid": "fake::holder",
        "host": "localhost",
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "created": time.time(),
    }
    base.update(kw)
    return base


def _age_file(path: str, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _hidden_popen(args, **kw):
    """起子进程一律不可见：CREATE_NO_WINDOW + SW_HIDE。"""
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kw.setdefault("startupinfo", si)
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.Popen(args, **kw)


# 子进程：拿锁 -> 写结果 -> 按 mode 保持 / 释放 / 异常退出
_CHILD_HOLD = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from _isolation import lock as L
root, name, mode, out = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
lk = L.FileLock(name, root=root, timeout_s=20.0)
t0 = time.monotonic()
lk.acquire()
info = {"pid": os.getpid(), "acquired": time.monotonic(),
        "waited": time.monotonic() - t0, "mode": mode}
if mode == "release":
    info["release_ok"] = lk.release()
    info["released"] = time.monotonic()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(info, f)
    sys.exit(0)
if mode == "abrupt":
    with open(out, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os._exit(0)
with open(out, "w", encoding="utf-8") as f:
    json.dump(info, f)
time.sleep(60)
"""


def _spawn_child(children, root: str, name: str, mode: str, timeout: float = 10.0):
    """起子进程并等它写出「已拿锁」的结果文件。返回 (proc, info)。"""
    out = os.path.join(root, "child-%s-%s.json" % (mode, time.monotonic_ns()))
    p = _hidden_popen([sys.executable, "-c", _CHILD_HOLD,
                       TESTS_DIR, root, name, mode, out],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    children.append(p)
    deadline = time.monotonic() + timeout
    while not os.path.exists(out):
        if p.poll() is not None:
            err = (p.stderr.read() if p.stderr else "") or ""
            pytest.fail("子进程没拿锁就退出：%s" % err.strip())
        if time.monotonic() > deadline:
            p.kill()
            pytest.fail("子进程 %.1fs 内没写出拿锁结果" % timeout)
        time.sleep(0.01)
    with open(out, encoding="utf-8") as f:
        info = json.load(f)
    return p, info


# R6 子进程：同一把锁下循环做「读-改-写」
_CHILD_RMW = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from _isolation import lock as L
root, name, cfg, times = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
lk = L.FileLock(name, root=root, timeout_s=20.0)
for _ in range(times):
    lk.acquire()
    try:
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        data["n"] = int(data.get("n") or 0) + 1
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(data, f)
    finally:
        lk.release()
"""


# --------------------------------------------------------------------------- #
# 锁目录与获取 / 续租 / 释放
# --------------------------------------------------------------------------- #
def test_lock_dir_is_outside_the_repository(tmp_path):
    a = tmp_path / "repo-a"
    b = tmp_path / "repo-b"
    a.mkdir()
    b.mkdir()
    dir_a = iso_lock.lock_dir(str(a))
    dir_b = iso_lock.lock_dir(str(b))
    temp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    assert os.path.normcase(os.path.abspath(dir_a)).startswith(temp)
    assert os.path.normcase(os.path.abspath(dir_a)) != os.path.normcase(os.path.abspath(str(a)))
    assert dir_a != dir_b, "不同项目根必须分目录，互不阻塞"


def test_acquire_writes_metadata_and_release_removes_it(iso_root):
    lk = iso_lock.FileLock("basic", groups=("timing", "ocr"),
                           nodeid="tests/x.py::case", root=iso_root)
    waited = lk.acquire()
    assert waited >= 0.0
    assert lk.waited_s == pytest.approx(waited, abs=0.05)
    path = lk.path
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["token"] == lk.token
    assert meta["pid"] == os.getpid()
    assert meta["groups"] == ["timing", "ocr"]
    assert meta["nodeid"] == "tests/x.py::case"
    assert meta["version"] == iso_lock.VERSION

    assert lk.release() is True
    assert not os.path.exists(path)
    assert lk.release() is False, "重复释放不能报成功"


def test_acquire_times_out_with_readable_holder_diagnostics(iso_root):
    holder = iso_lock.FileLock("busy", groups=("timing",),
                               nodeid="tests/holder.py::case", root=iso_root)
    holder.acquire()
    waiter = iso_lock.FileLock("busy", root=iso_root, timeout_s=0.4)
    t0 = time.monotonic()
    with pytest.raises(iso_lock.LockTimeout) as ei:
        waiter.acquire()
    elapsed = time.monotonic() - t0
    msg = str(ei.value)
    assert 0.3 <= elapsed <= 2.0, "超时必须按上限收口，不能无限等"
    assert "等锁超时：busy" in msg
    assert "pid=%d" % os.getpid() in msg
    assert "timing" in msg
    assert "自救" in msg
    assert holder.release() is True


def test_waiter_wakes_on_release_and_holder_can_renew(iso_root):
    """续租：持锁期间反复 renew 成功；释放后等待者被及时唤醒。"""
    holder = iso_lock.FileLock("wake", groups=("timing",), root=iso_root)
    holder.acquire()
    renewed = []

    def hold_then_release():
        try:
            for _ in range(8):
                renewed.append(holder.renew())
                time.sleep(0.05)
        finally:
            holder.release()

    t = threading.Thread(target=hold_then_release, name="holder")
    t.start()
    waiter = iso_lock.FileLock("wake", root=iso_root, timeout_s=2.0)
    t0 = time.monotonic()
    waited = waiter.acquire()
    elapsed = time.monotonic() - t0
    t.join(timeout=5)
    assert not t.is_alive()
    assert all(renewed), "持锁期间的续租必须全部成功"
    assert 0.25 <= elapsed < 1.5, "释放后等待者应在一个续租周期内被唤醒"
    assert abs(waited - elapsed) < 0.2
    assert waiter.release() is True


def test_renew_rejects_non_owner_and_refuses_to_delete_others_lock(iso_root):
    lk = iso_lock.FileLock("token", root=iso_root)
    assert lk.renew() is False, "没持锁不能续租"
    lk.acquire()
    assert lk.renew() is True

    # 模拟锁已被别人接管：元数据里的 token 不再属于本对象
    path = lk.path
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    _write_meta(path, {**meta, "token": "someone-else"})
    assert lk.renew() is False, "不能给别人的锁续命"
    assert lk.release() is False, "已被接管时绝不删别人的锁"
    assert os.path.exists(path)
    os.unlink(path)


# --------------------------------------------------------------------------- #
# 四类陈旧接管
# --------------------------------------------------------------------------- #
def test_takeover_holder_dead_after_process_killed(iso_root, children):
    p, info, = _spawn_child(children, iso_root, "dead-holder", "hold")
    assert info["mode"] == "hold"
    path = _lock_path(iso_root, "dead-holder")
    assert os.path.exists(path)
    p.kill()
    p.wait(timeout=5)

    lk = iso_lock.FileLock("dead-holder", root=iso_root, timeout_s=2.0)
    t0 = time.monotonic()
    lk.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, "持有者已被 kill，应立即判 holder-dead 并接管"
    assert lk.release() is True


def test_takeover_holder_dead_after_abrupt_exit(iso_root, children):
    p, info, = _spawn_child(children, iso_root, "abrupt-holder", "abrupt")
    assert p.wait(timeout=10) == 0
    assert os.path.exists(_lock_path(iso_root, "abrupt-holder"))

    lk = iso_lock.FileLock("abrupt-holder", root=iso_root, timeout_s=2.0)
    t0 = time.monotonic()
    lk.acquire()
    assert time.monotonic() - t0 < 1.0
    assert lk.release() is True


@pytest.mark.skipif(os.name != "nt", reason="pid_start 只在 Windows 上可取")
def test_takeover_pid_reused(iso_root):
    actual = iso_lock.pid_start_marker(os.getpid())
    if not actual:
        pytest.skip("当前进程拿不到创建时间标记")
    path = _lock_path(iso_root, "reused")
    # pid 活着，但元数据里的创建时间与真实进程差 5 —— 旧 PID 被复用
    _write_meta(path, _fake_meta(os.getpid(), pid_start=int(actual) + 5))

    lk = iso_lock.FileLock("reused", root=iso_root, timeout_s=2.0)
    t0 = time.monotonic()
    lk.acquire()
    assert time.monotonic() - t0 < 1.0, "PID 复用必须被判 pid-reused"
    with open(lk.path, encoding="utf-8") as f:
        assert json.load(f)["token"] == lk.token
    assert lk.release() is True


def test_takeover_holder_wedged(iso_root):
    path = _lock_path(iso_root, "wedged")
    _write_meta(path, _fake_meta(os.getpid()))
    _age_file(path, 20.0)

    lk = iso_lock.FileLock("wedged", root=iso_root, timeout_s=2.0, stale_after_s=5.0)
    t0 = time.monotonic()
    lk.acquire()
    assert time.monotonic() - t0 < 1.0, "超过 stale_after_s 的活锁必须被接管"
    assert lk.release() is True


def test_takeover_corrupt_meta(iso_root, monkeypatch):
    # 把「刚创建还没写完」的容忍窗口压到 50ms，避免用例白等 2s
    monkeypatch.setattr(iso_lock, "CORRUPT_GRACE_S", 0.05)
    path = _lock_path(iso_root, "corrupt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b'{"pid": 1, "token": ')     # 半截 JSON
    _age_file(path, 1.0)

    lk = iso_lock.FileLock("corrupt", root=iso_root, timeout_s=2.0)
    t0 = time.monotonic()
    lk.acquire()
    assert time.monotonic() - t0 < 1.0, "超容忍窗口的坏元数据必须被接管"
    assert lk.release() is True


def test_renew_prevents_holder_wedged_takeover(iso_root):
    """R4：续租可以替代「900s 缓冲」—— 长跑持锁者不会被误拆。"""
    holder = iso_lock.FileLock("renew-wedged", root=iso_root, stale_after_s=0.25)
    holder.acquire()
    time.sleep(0.35)
    meta, age = iso_lock._read_meta(holder.path)
    assert holder._stale_reason(meta, age) == "holder-wedged"

    assert holder.renew() is True
    meta, age = iso_lock._read_meta(holder.path)
    assert holder._stale_reason(meta, age) == "", "续租后必须不再是陈旧锁"

    # 停止续租 -> 再次超过阈值 -> 等待者接管
    time.sleep(0.35)
    waiter = iso_lock.FileLock("renew-wedged", root=iso_root, stale_after_s=0.25,
                               timeout_s=2.0)
    t0 = time.monotonic()
    waiter.acquire()
    assert time.monotonic() - t0 < 1.5
    assert holder.release() is False, "被接管后原持有者不能删别人的锁"
    assert waiter.release() is True


# --------------------------------------------------------------------------- #
# 两级锁：固定顺序 + 死锁规避
# --------------------------------------------------------------------------- #
def test_build_locks_is_umbrella_then_sorted_groups():
    locks = iso_lock.build_locks(("timing", "playwright"), "tests/x.py::case",
                                 root=os.path.join(tempfile.gettempdir(), "no-such-repo"))
    assert [lk.name for lk in locks] == [
        iso_lock.UMBRELLA_NAME, "heavy-playwright", "heavy-timing"]
    assert locks[0].groups == ("playwright", "timing")
    assert locks[0].nodeid == "tests/x.py::case"
    # 所有等待者都按这个顺序拿锁，构造上不存在环
    assert locks[0].name < locks[1].name <= locks[2].name


def test_two_level_locks_serialize_two_threads_without_deadlock(iso_root):
    errors = []

    def worker():
        try:
            for _ in range(6):
                locks = iso_lock.build_locks(("timing", "playwright"), "t",
                                             timeout_s=8.0, root=iso_root)
                try:
                    for lk in locks:
                        lk.acquire()
                finally:
                    for lk in reversed(locks):
                        lk.release()
        except BaseException as exc:        # noqa: BLE001 - 线程里必须带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, name="w%d" % i) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads), "固定顺序下两个线程不应互等"
    assert not errors, errors


# --------------------------------------------------------------------------- #
# R2：用例体异常与锁失败的责任隔离
# --------------------------------------------------------------------------- #
class _StubNode:
    def __init__(self, nodeid):
        self.nodeid = nodeid

    def get_closest_marker(self, name):
        return None


class _StubConfig:
    def __init__(self, **opts):
        self._opts = {"no_test_lock": False, "test_lock_timeout": 2.0,
                      "test_lock_stale": 5.0, **opts}

    def getoption(self, name, default=None):
        return self._opts.get(name, default)


class _StubRequest:
    def __init__(self, nodeid, **opts):
        self.node = _StubNode(nodeid)
        self.config = _StubConfig(**opts)


def _conftest_module():
    """拿到 pytest 已经加载的 tests/conftest.py，不重复 exec。"""
    target = os.path.normcase(os.path.abspath(os.path.join(TESTS_DIR, "conftest.py")))
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if path and os.path.normcase(os.path.abspath(path)) == target:
            return mod
    raise RuntimeError("tests/conftest.py 尚未被 pytest 加载")


def test_body_error_is_not_mislabelled_as_lock_timeout(iso_root, monkeypatch):
    """R2：core/ 被改坏导致的 AttributeError 必须原样抛出，不能被锁改写。"""
    mod = _conftest_module()
    fixture_fn = getattr(mod._cross_process_heavy_lock, "__wrapped__",
                         mod._cross_process_heavy_lock)
    real_build = iso_lock.build_locks
    monkeypatch.setattr(
        iso_lock, "build_locks",
        lambda groups, nodeid, **kw: real_build(groups, nodeid, root=iso_root, **kw))
    monkeypatch.setattr(iso_lock, "record_wait", lambda entry: None)
    monkeypatch.setattr(mod.iso_priority, "boost_for_timing", lambda: None)
    monkeypatch.setattr(mod.iso_priority, "restore", lambda old: None)

    request = _StubRequest("tests/test_precision.py::test_anything")
    gen = fixture_fn(request)
    next(gen)                                        # 拿锁并停在 yield
    assert os.path.exists(_lock_path(iso_root, "heavy"))
    assert os.path.exists(_lock_path(iso_root, "heavy-timing"))

    err = AttributeError("'dict' object has no attribute 'data'")
    with pytest.raises(AttributeError) as ei:
        gen.throw(err)
    assert ei.value is err, "用例体异常必须原样向上，不能被锁替换"
    assert not os.path.exists(_lock_path(iso_root, "heavy")), "失败后锁必须释放"
    assert not os.path.exists(_lock_path(iso_root, "heavy-timing"))


# --------------------------------------------------------------------------- #
# R6：同一把锁下的跨进程「读-改-写」串行化
# --------------------------------------------------------------------------- #
def test_cross_process_config_writes_are_serialized(iso_root, children):
    name = "config-write"
    cfg = os.path.join(iso_root, "config.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"n": 0}, f)

    procs = [
        _hidden_popen([sys.executable, "-c", _CHILD_RMW,
                       TESTS_DIR, iso_root, name, cfg, "40"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    children.extend(procs)
    for p in procs:
        try:
            out, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            pytest.fail("跨进程读改写超时（锁可能没生效）")
        assert p.returncode == 0, err
        assert "Traceback" not in (err or "")

    with open(cfg, encoding="utf-8") as f:
        assert json.load(f)["n"] == 80, "两个进程各写 40 次，丢失更新 = 串行失效"
    assert not os.path.exists(_lock_path(iso_root, name))


# --------------------------------------------------------------------------- #
# R1：CPU 饱和下的获取/续租（默认跳过，显式开启）
# --------------------------------------------------------------------------- #
_BURN = r"""
import sys, time
end = time.monotonic() + float(sys.argv[1])
while time.monotonic() < end:
    pass
"""


@pytest.mark.skipif(os.environ.get("SJZ_LOCK_LOADTEST") != "1",
                    reason="短时占满 CPU 的重负载用例：显式设 SJZ_LOCK_LOADTEST=1 才跑")
def test_lock_ops_stay_bounded_under_cpu_saturation(iso_root, children):
    burners = []
    n = max(4, min(16, (os.cpu_count() or 8) // 2))
    for _ in range(n):
        p = _hidden_popen([sys.executable, "-c", _BURN, "3.0"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        burners.append(p)
        children.append(p)
    try:
        time.sleep(0.3)
        waits = []
        t_start = time.monotonic()
        while time.monotonic() - t_start < 2.2:
            lk = iso_lock.FileLock("loadtest", root=iso_root, timeout_s=5.0)
            t0 = time.monotonic()
            lk.acquire()
            waits.append(time.monotonic() - t0)
            assert lk.renew() is True
            lk.release()
        assert max(waits) < 2.0, "CPU 饱和下获取延迟仍应远小于 5s 上限：%r" % waits
    finally:
        for p in burners:
            if p.poll() is None:
                p.kill()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
    print("R1 loadtest: burners=%d cycles=%d max_wait_ms=%.1f p95_ms=%.1f"
          % (n, len(waits), max(waits) * 1000.0,
             sorted(waits)[max(0, int(len(waits) * 0.95) - 1)] * 1000.0))
