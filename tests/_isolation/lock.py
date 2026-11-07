"""跨进程互斥锁：文件锁 + PID 存活检测 + 陈旧锁接管 + 超时。

为什么需要它
------------
同一台机器上并发跑多份 pytest 时，重负载用例（时序 / 起子进程 / Playwright /
真 OCR / 真实驱动）会互相抢 CPU、抢 GPU、抢窗口焦点，产生**假失败**：
``test_late_cursor_bump`` 的 lag 从 0.4ms 涨到 8ms，子进程用例被判 dead，
Playwright 超时。锁只做一件事：让这些重负载用例在所有 pytest 进程之间
**串行**，轻量用例完全不受影响。

实现选型
--------
``O_CREAT|O_EXCL`` 原子创建锁文件 + 文件里写 JSON 元数据（pid、进程创建时间、
token、组、nodeid）。选择它而不是 ``msvcrt.locking`` 的原因：

* 创建/释放都是普通文件操作，跨进程、跨会话、跨 xdist worker 都成立；
* 崩溃的持有者（进程消失）留下的锁文件可以靠 **PID 存活检测** 判定死亡并接管；
* 活着的持有者卡死时，靠 **锁年龄** 判定陈旧并接管；
* 文件句柄在写入元数据后立刻关闭，因此接管方 ``os.replace`` 不会被 Windows
  的共享锁挡住（``msvcrt`` 句柄式锁则无法被别的进程打破）。

OS 级互斥只用在「接管」这一小步（``breaker.lock``，Windows 用
``msvcrt.locking``，POSIX 用 ``fcntl.flock``）：保证同一时刻只有一个等待者去
拆陈旧锁，避免两个等待者同时 rename 造成的双持锁。

死锁防线
--------
1. 所有等待都有上限（默认 300s，``--test-lock-timeout`` 可调），超时抛
   :class:`LockTimeout`，由 conftest 转成带诊断的可读失败；
2. 锁按固定顺序获取（先 ``heavy`` 总闸，再按组名字典序），不存在环；
3. 接管前在 breaker 锁内**重新检查**陈旧条件，避免误拆刚被释放/重建的锁；
4. 释放前校验 token，绝不删掉别人的锁。
"""
from __future__ import annotations

import ctypes
import glob
import hashlib
import json
import os
import random
import socket
import sys
import tempfile
import time
import uuid

VERSION = 1

#: 默认等锁上限（秒）
DEFAULT_TIMEOUT_S = 300.0
#: 默认「锁年龄超过这么久就视为陈旧」上限（秒）。必须远大于任何单条重负载用例。
DEFAULT_STALE_AFTER_S = 900.0
#: 元数据文件刚被创建、内容还没写完时的容忍窗口（秒）
CORRUPT_GRACE_S = 2.0
#: 总闸锁文件名（所有 heavy 组共用）
UMBRELLA_NAME = "heavy"

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def project_tag(root: str = _ROOT) -> str:
    """项目标识：目录名 + 规范化绝对路径的短哈希。

    锁目录在系统临时目录里，同机可能有别的仓库也用了这套锁；
    按项目分目录，互不阻塞。
    """
    name = os.path.basename(os.path.normpath(root)) or "root"
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name)[:24]
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (safe, digest)


def lock_dir(root: str = _ROOT) -> str:
    """锁目录：系统临时目录，绝不写仓库。"""
    return os.path.join(tempfile.gettempdir(), "sjz-pytest-isolation", project_tag(root))


# --------------------------------------------------------------------------- #
# 进程存活检测
# --------------------------------------------------------------------------- #
if os.name == "nt":                                     # pragma: no cover - 平台分支
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _K32.OpenProcess.restype = ctypes.c_void_p
    _K32.CloseHandle.argtypes = [ctypes.c_void_p]
    _K32.CloseHandle.restype = ctypes.c_bool
    _K32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _K32.WaitForSingleObject.restype = ctypes.c_uint32
    _K32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64),
    ]
    _K32.GetProcessTimes.restype = ctypes.c_bool

    _SYNCHRONIZE = 0x00100000
    _QUERY_LIMITED = 0x1000
    _ACCESS_DENIED = 5
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_OBJECT_0 = 0x00000000


def pid_alive(pid: int) -> bool:
    """PID 还活着吗？拿不到句柄时保守判活（宁可不接管，也不误拆活锁）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":                                 # pragma: no cover - 平台分支
        handle = _K32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ACCESS_DENIED
        try:
            return _K32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
        finally:
            _K32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def pid_start_marker(pid: int) -> int | None:
    """进程创建时间（FILETIME 整数），用来识别 PID 复用。

    只存 PID 不安全：旧进程死后 PID 被新进程复用，会让「死锁」看起来还活着。
    比对创建时间可以把这两种情况分开。
    """
    if os.name != "nt":                                 # pragma: no cover - 平台分支
        return None
    handle = _K32.OpenProcess(_QUERY_LIMITED | _SYNCHRONIZE, False, pid)
    if not handle:
        return None
    try:
        created = ctypes.c_uint64(0)
        exited = ctypes.c_uint64(0)
        kernel = ctypes.c_uint64(0)
        user = ctypes.c_uint64(0)
        if not _K32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return int(created.value)
    finally:
        _K32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# OS 级小锁：只用于「拆陈旧锁」的临界区
# --------------------------------------------------------------------------- #
class _OsGuard:
    """breaker 临界区。Windows 走 ``msvcrt.locking``，POSIX 走 ``fcntl.flock``。

    这个锁的持有时间只有一次 rename + unlink（毫秒级），拿不到就说明别的
    等待者正在拆锁，直接让出再轮询即可，不会形成死锁。
    """

    def __init__(self, path: str, timeout_s: float = 3.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fd = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout_s
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        while True:
            try:
                _os_lock(self._fd)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise TimeoutError("breaker lock busy")
                time.sleep(0.01)

    def __exit__(self, *exc):
        if self._fd is None:
            return False
        try:
            _os_unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
        return False


def _os_lock(fd: int) -> None:
    if os.name == "nt":                                # pragma: no cover - 平台分支
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:                                              # pragma: no cover - 平台分支
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if os.name == "nt":                                # pragma: no cover - 平台分支
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:                                              # pragma: no cover - 平台分支
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 锁本体
# --------------------------------------------------------------------------- #
class LockTimeout(RuntimeError):
    """等锁超过上限。消息里必须带「谁在持有、等了多久、怎么自救」。"""


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _read_meta(path: str) -> tuple[dict | None, float]:
    """读锁文件元数据。返回 (meta 或 None, 文件年龄秒)。"""
    try:
        st = os.stat(path)
    except OSError:
        return None, 0.0
    age = max(0.0, time.time() - st.st_mtime)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if not raw:
            return None, age
        meta = json.loads(raw.decode("utf-8"))
        if not isinstance(meta, dict):
            return None, age
        return meta, age
    except (OSError, ValueError, UnicodeDecodeError):
        return None, age


def _write_meta(path: str, meta: dict) -> None:
    with open(path, "wb") as f:
        f.write(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def _describe(meta: dict | None, age: float) -> str:
    if not meta:
        return "锁文件内容不可读（可能是刚创建/刚崩溃），年龄 %.1fs" % age
    pid = meta.get("pid")
    status = "存活" if pid_alive(pid) else "已退出"
    return ("pid=%s（%s）组=%s 用例=%s 持有 %.1fs"
            % (pid, status, ",".join(meta.get("groups") or []) or "-",
               meta.get("nodeid") or "-", age))


class FileLock:
    """一个具名跨进程锁。

    :param name: 锁文件名（不含 .lock），必须带组标识，例如 ``heavy-timing``
    :param groups: 本次获取所属的组（写进元数据，便于诊断）
    :param nodeid: 用例 nodeid（写进元数据）
    """

    def __init__(self, name: str, *, groups=(), nodeid="", timeout_s=DEFAULT_TIMEOUT_S,
                 stale_after_s=DEFAULT_STALE_AFTER_S, root: str = _ROOT):
        self.name = name
        self.dir = lock_dir(root)
        self.path = os.path.join(self.dir, name + ".lock")
        self.breaker_path = os.path.join(self.dir, "breaker.lock")
        self.groups = tuple(groups)
        self.nodeid = nodeid
        self.timeout_s = float(timeout_s)
        self.stale_after_s = float(stale_after_s)
        self.token = uuid.uuid4().hex
        self.waited_s = 0.0
        self._held = False

    # -- 获取 ---------------------------------------------------------------
    def acquire(self) -> float:
        """阻塞到拿到锁，返回等待秒数。超时抛 :class:`LockTimeout`。"""
        _ensure_dir(self.dir)
        start = time.monotonic()
        deadline = start + self.timeout_s
        last_reason = ""
        while True:
            try:
                self._create(exclusive=True)
            except FileExistsError:
                meta, age = _read_meta(self.path)
                stale = self._stale_reason(meta, age)
                if stale:
                    last_reason = stale
                    if self._break_stale(stale):
                        continue
            except PermissionError:
                # 杀软/索引器短暂占用，按竞争处理
                last_reason = "permission-denied"
            else:
                self.waited_s = time.monotonic() - start
                self._held = True
                return self.waited_s
            if time.monotonic() >= deadline:
                meta, age = _read_meta(self.path)
                raise LockTimeout(
                    "等锁超时：%s\n"
                    "  等了 %.1fs（上限 %.1fs）\n"
                    "  当前持有者：%s\n"
                    "  锁文件：%s\n"
                    "  自救：1) 确认没有别的 pytest 在跑后删除上面的锁文件；"
                    "2) 用 --no-test-lock 跳过本次互斥；"
                    "3) 调大 --test-lock-timeout。"
                    % (self.name, time.monotonic() - start, self.timeout_s,
                       _describe(meta, age), self.path))
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(remaining, random.uniform(0.02, 0.08)))

    def _create(self, exclusive: bool = True) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        fd = os.open(self.path, flags, 0o644)
        try:
            meta = {
                "version": VERSION,
                "token": self.token,
                "name": self.name,
                "pid": os.getpid(),
                "pid_start": pid_start_marker(os.getpid()),
                "groups": list(self.groups),
                "nodeid": self.nodeid,
                "host": socket.gethostname(),
                "platform": sys.platform,
                "cwd": os.getcwd(),
                "created": time.time(),
            }
            _write_meta_fd(fd, meta)
        except BaseException:
            try:
                os.close(fd)
            finally:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            raise
        os.close(fd)

    # -- 陈旧判定 / 接管 ----------------------------------------------------
    def _stale_reason(self, meta: dict | None, age: float) -> str:
        if meta is None:
            # 刚创建、还没写完：给一个很短的重试窗口
            return "corrupt-meta" if age > CORRUPT_GRACE_S else ""
        pid = meta.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return "bad-pid" if age > CORRUPT_GRACE_S else ""
        if not pid_alive(pid):
            return "holder-dead"
        recorded = meta.get("pid_start")
        actual = pid_start_marker(pid)
        if recorded and actual and abs(int(recorded) - int(actual)) > 1:
            return "pid-reused"
        if age > self.stale_after_s:
            return "holder-wedged"
        return ""

    def _break_stale(self, reason: str) -> bool:
        """在 breaker 临界区内重检并拆掉陈旧锁。成功返回 True。"""
        try:
            with _OsGuard(self.breaker_path):
                meta, age = _read_meta(self.path)
                if not self._stale_reason(meta, age):
                    return False                    # 已被释放/重建，让持锁方继续
                stale_name = "%s.stale-%d-%s" % (self.path, os.getpid(),
                                                 uuid.uuid4().hex[:6])
                try:
                    os.replace(self.path, stale_name)
                except FileNotFoundError:
                    return True
                except OSError:
                    return False
                try:
                    os.unlink(stale_name)
                except OSError:
                    pass
                return True
        except TimeoutError:
            return False

    # -- 释放 ---------------------------------------------------------------
    def release(self) -> bool:
        """释放锁。token 不匹配说明锁已被接管，绝不删别人的锁。"""
        if not self._held:
            return False
        self._held = False
        meta, _ = _read_meta(self.path)
        if not meta or meta.get("token") != self.token:
            return False
        for attempt in range(3):
            try:
                os.unlink(self.path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if attempt == 2:
                    return False
                time.sleep(0.02)
        return False

    # -- 续租 ---------------------------------------------------------------
    def renew(self) -> bool:
        """刷新锁文件年龄，避免长跑持锁者被 ``holder-wedged`` 误拆。

        陈旧判定只看文件 mtime，所以续租用 ``os.utime``：不改元数据内容，
        不会在崩溃时写坏 JSON。token 不匹配（锁已被接管）或本对象没持锁时
        返回 False —— 绝不给别人的锁续命。
        """
        if not self._held:
            return False
        meta, _ = _read_meta(self.path)
        if not meta or meta.get("token") != self.token:
            return False
        try:
            os.utime(self.path, None)
        except OSError:
            return False
        return True

    # -- 其他 ---------------------------------------------------------------
    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def __repr__(self):
        return "<FileLock %s groups=%s>" % (self.name, ",".join(self.groups))


def _write_meta_fd(fd: int, meta: dict) -> None:
    data = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    os.write(fd, data)


def build_locks(groups, nodeid: str, *, timeout_s=DEFAULT_TIMEOUT_S,
                stale_after_s=DEFAULT_STALE_AFTER_S, root: str = _ROOT) -> list[FileLock]:
    """按固定顺序构造锁链：总闸 -> 各组的组锁（字典序）。

    固定顺序是防死锁的硬规矩：所有进程都按同一顺序拿，就不会出现
    A 拿 timing 等 playwright、B 拿 playwright 等 timing 的环。
    """
    groups = tuple(sorted(groups))
    locks = [FileLock(UMBRELLA_NAME, groups=groups, nodeid=nodeid,
                      timeout_s=timeout_s, stale_after_s=stale_after_s, root=root)]
    for g in groups:
        locks.append(FileLock("%s-%s" % (UMBRELLA_NAME, g), groups=(g,), nodeid=nodeid,
                              timeout_s=timeout_s, stale_after_s=stale_after_s, root=root))
    return locks


# --------------------------------------------------------------------------- #
# 等待统计（用于验收报告与残余风险分析）
# --------------------------------------------------------------------------- #
_STATS_PREFIX = "waits"


def _stats_file(run_id: str, pid: int) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in run_id)[:64]
    return os.path.join(lock_dir(), "%s-%s-%d.jsonl" % (_STATS_PREFIX, safe, pid))


def record_wait(entry: dict) -> None:
    """追加一条等待记录。写失败一律吞掉 —— 统计不能拖垮测试。"""
    try:
        _ensure_dir(lock_dir())
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        with open(_stats_file(str(entry.get("run", "unknown")), os.getpid()), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def collect_waits(run_id: str | None = None, since_ts: float | None = None) -> list[dict]:
    """读回等待记录。

    先按 ``run_id`` 严格过滤（xdist worker 继承 controller 的环境变量，同一场
    运行的所有进程共用一个 id）；一条都没读到（例如 worker 没继承环境变量）
    再退回「按时间窗」聚合，保证汇总不会因为 id 不匹配而整段消失。
    """
    files = glob.glob(os.path.join(lock_dir(), _STATS_PREFIX + "-*.jsonl"))
    strict: list[dict] = []
    windowed: list[dict] = []
    for path in files:
        try:
            if since_ts is not None and os.path.getmtime(path) < since_ts:
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if run_id and item.get("run") == run_id:
                        strict.append(item)
                    elif since_ts is not None and item.get("ts", 0) >= since_ts:
                        windowed.append(item)
        except OSError:
            continue
    return strict if strict else windowed


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return ordered[idx]


def summary_lines(entries: list[dict]) -> list[str]:
    """把等待记录整理成可读的分布摘要。"""
    if not entries:
        return ["  heavy 锁：本次没有等待记录（无竞争或全部命中空闲窗口）"]
    waits = [float(e.get("waited_ms") or 0.0) for e in entries]
    held = [float(e.get("held_ms") or 0.0) for e in entries]
    timed_out = sum(1 for e in entries if e.get("timed_out"))
    long_waits = sum(1 for w in waits if w >= 100.0)
    lines = [
        "  heavy 锁：%d 次获取，其中 %d 次等待 ≥100ms，%d 次超时"
        % (len(entries), long_waits, timed_out),
        "  等锁耗时(ms)：p50=%.1f p95=%.1f max=%.1f 总计=%.0f"
        % (_percentile(waits, 0.50), _percentile(waits, 0.95), max(waits), sum(waits)),
        "  持锁耗时(ms)：p50=%.0f p95=%.0f max=%.0f 总计=%.0f"
        % (_percentile(held, 0.50), _percentile(held, 0.95), max(held), sum(held)),
    ]
    slowest = sorted(entries, key=lambda e: float(e.get("waited_ms") or 0.0),
                     reverse=True)[:5]
    if slowest and float(slowest[0].get("waited_ms") or 0.0) >= 20.0:
        lines.append("  等待最久的 5 条：")
        for e in slowest:
            lines.append("    %6.0fms  %s  %s"
                         % (float(e.get("waited_ms") or 0.0),
                            ",".join(e.get("groups") or []),
                            e.get("nodeid") or "-"))
    return lines
