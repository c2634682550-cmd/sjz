"""进程级稳定性指标采样（Windows）。

要回答的问题：
  · 内存是不是只涨不跌？（PrivateWorkingSet / 工作集 / tracemalloc）
  · 句柄、GDI/USER 对象、线程是不是只增不减？
  · Python 对象是不是越堆越多（GC 各代计数 / 存活对象数）？
  · 我们自己打开的文件句柄有没有漏关？

实现说明：
  · 进程内存、句柄数、GDI/USER、线程数走 Win32 API（psapi / kernel32 /
    user32），不依赖 psutil（这台机器没装）；
  · 「打开的文件句柄」统计包住 `builtins.open`：Python 侧每次 open 计数 +1、
    close 计数 -1。真实进程里还有 DLL 自己开的句柄，那部分只能靠
    GetProcessHandleCount 的总量趋势判断；
  · tracemalloc 只在不频繁的深采样里做快照，否则自身开销会污染曲线。
"""
import builtins
import ctypes
import ctypes.wintypes as wt
import gc
import os
import threading
import tracemalloc
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")


class SYSTEM_HANDLE_ENTRY_EX(ctypes.Structure):
    """SystemExtendedHandleInformation 的一条记录（64 位，共 40 字节）。"""
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_void_p),
        ("HandleValue", ctypes.c_void_p),
        ("GrantedAccess", wt.DWORD),
        ("CreatorBackTraceIndex", wt.WORD),
        ("ObjectTypeIndex", wt.WORD),
        ("HandleAttributes", wt.DWORD),
        ("Reserved", wt.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

    def _secs(self):
        v = (self.dwHighDateTime << 32) | self.dwLowDateTime
        return v / 1e7                       # 100ns 单位 → 秒


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ThreadID", wt.DWORD),
        ("th32OwnerProcessID", wt.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wt.DWORD),
    ]


_psapi_proto = False
_th32_proto = False


def _init_protos():
    global _psapi_proto, _th32_proto
    if not _psapi_proto:
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL
        kernel32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        kernel32.GetProcessHandleCount.restype = wt.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wt.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        kernel32.GetProcessTimes.restype = wt.BOOL
        user32.GetGuiResources.argtypes = [wt.HANDLE, wt.DWORD]
        user32.GetGuiResources.restype = wt.DWORD
        _psapi_proto = True
    if not _th32_proto:
        kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        kernel32.Thread32First.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32First.restype = wt.BOOL
        kernel32.Thread32Next.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
        kernel32.Thread32Next.restype = wt.BOOL
        _th32_proto = True


def _mem():
    _init_protos()
    c = PROCESS_MEMORY_COUNTERS_EX()
    c.cb = ctypes.sizeof(c)
    h = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
        return {}
    return {
        "working_set_mb": round(c.WorkingSetSize / 1048576, 2),
        "private_mb": round(c.PrivateUsage / 1048576, 2),
        "peak_working_set_mb": round(c.PeakWorkingSetSize / 1048576, 2),
        "pagefile_mb": round(c.PagefileUsage / 1048576, 2),
    }


def _handles():
    _init_protos()
    n = wt.DWORD(0)
    ok = kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(n))
    return int(n.value) if ok else -1


def handle_types():
    """按对象类型统计本进程句柄：{type_index: count}。

    `GetProcessHandleCount` 只给总数，说不清是文件、事件还是线程句柄在涨。
    这里走 `NtQuerySystemInformation(SystemExtendedHandleInformation=64)`
    把整张句柄表的明细拉出来，按 `ObjectTypeIndex` 分组。type_index 是系统
    内部序号（同一台机器上稳定），用来对比「哪一类在涨」。
    """
    pid = os.getpid()
    ntdll.NtQuerySystemInformation.argtypes = [
        wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
    ntdll.NtQuerySystemInformation.restype = wt.LONG
    buf = ctypes.create_string_buffer(1 << 20)
    need = wt.ULONG(0)
    code = 0xC0000004                            # STATUS_INFO_LENGTH_MISMATCH
    for _ in range(6):
        st = ntdll.NtQuerySystemInformation(64, buf, len(buf), ctypes.byref(need)) & 0xFFFFFFFF
        code = st
        if st != 0xC0000004:
            break
        buf = ctypes.create_string_buffer(int(need.value) + 65536)
    if code != 0:
        return {}
    total = int(ctypes.cast(buf, ctypes.POINTER(ctypes.c_size_t))[0])
    off = ctypes.sizeof(ctypes.c_size_t) * 2
    step = ctypes.sizeof(SYSTEM_HANDLE_ENTRY_EX)
    out = {}
    for i in range(total):
        e = SYSTEM_HANDLE_ENTRY_EX.from_buffer(buf, off + i * step)
        if int(e.UniqueProcessId or 0) != pid:
            continue
        out[e.ObjectTypeIndex] = out.get(e.ObjectTypeIndex, 0) + 1
    return out


def _cpu_s():
    """本进程累计 CPU 时间（内核+用户，秒）。

    为什么要有这个：长跑用墙钟衡量「周期耗时」，一旦机器上有其它进程抢
    CPU，测出来的「越跑越慢」全是调度噪声。CPU 时间只统计本进程真正占用
    的处理时间，与机器负载无关，用它算漂移才能分清「代码变慢」和「机器忙」。
    `GetProcessTimes` 精度 100ns。
    """
    _init_protos()
    h = kernel32.GetCurrentProcess()
    c, e, k, u = (FILETIME() for _ in range(4))
    ok = kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e),
                                  ctypes.byref(k), ctypes.byref(u))
    if not ok:
        return -1.0
    return round(k._secs() + u._secs(), 4)


def cpu_seconds():
    """公开别名：测「一段代码真正花了多少 CPU」。"""
    return _cpu_s()


_CAL_BUF = None
CAL_LAST = {"sha_ms": 0.0, "mem_ms": 0.0}


def calibrate_ms(rounds=250, size=32768, mem_mb=16, mem_passes=8, tries=3):
    """固定工作量的速度标尺（毫秒），取多次里的最小值。

    为什么需要：`GetProcessTimes` 给的是「CPU 秒」，但同样的指令在 CPU 降频、
    睿频、被别的进程挤占物理核（含大小核调度）时，消耗的 CPU 秒数并不相同。
    长跑用「后段 CPU 时间 / 前段 CPU 时间」判越跑越慢时，机器忙闲变化会被
    整段算成代码漂移（v5 跑出 -14.3%、黑盒复跑 +19.9%，两次尾部都是
    93.8ms，起点差 40%，差的就是这一项）。

    所以每个采样窗口跑一段**完全相同**的工作（sha256 定长块），把它的
    耗时当分母：`单轮 CPU / 标定耗时` 才是与机器频率无关的量。标定本身
    也吃 CPU，环境变慢时分子分母同向变化，比值不变。

    取 250×32KB≈8MB 哈希（计算密集，看频率）+ 16MB 大数组来回拷 8 遍
    （≈256MB 内存流量，看内存带宽/共享缓存）。两段各占一半才是这台机器此刻的
    真实速度：只测 sha256 会漏掉「别的进程在抢内存带宽」这一项——引擎每轮
    557 次 numpy/cv2 缩放是内存密集的，实测 72h 后段 CPU 涨 20% 而 sha256
    标定只涨 3%，差的就是内存带宽。3 次取最小值可剔掉被抢占的那几次。
    整段 ~6ms，每 25 轮一次，占比 <1%。
    """
    import hashlib
    global _CAL_BUF
    blob = b"d" * int(size)
    best = None
    parts = None
    for _ in range(int(tries)):
        t0 = time.perf_counter()
        h = hashlib.sha256()
        for i in range(int(rounds)):
            h.update(blob)
            h.update(str(i).encode())
        h.digest()
        sha = (time.perf_counter() - t0) * 1000
        mem = 0.0
        if mem_mb > 0:
            import numpy as np
            if _CAL_BUF is None:
                _CAL_BUF = np.zeros(int(mem_mb * 1024 * 1024) // 4,
                                    dtype=np.float32)
            tmp = np.empty_like(_CAL_BUF)
            t1 = time.perf_counter()
            for _ in range(int(mem_passes)):
                np.copyto(tmp, _CAL_BUF)
            mem = (time.perf_counter() - t1) * 1000
        dt = sha + mem
        if best is None or dt < best:
            best = dt
            parts = (sha, mem)
    if parts:
        CAL_LAST["sha_ms"] = round(parts[0], 3)
        CAL_LAST["mem_ms"] = round(parts[1], 3)
    return round(best, 3)


def _gui():
    _init_protos()
    h = kernel32.GetCurrentProcess()
    return {"gdi": int(user32.GetGuiResources(h, 0)),
            "user": int(user32.GetGuiResources(h, 1))}


def _proc_threads():
    _init_protos()
    pid = os.getpid()
    snap = kernel32.CreateToolhelp32Snapshot(0x4, 0)      # TH32CS_SNAPTHREAD
    if snap in (None, -1, 0):
        return -1
    count = 0
    e = THREADENTRY32()
    e.dwSize = ctypes.sizeof(e)
    try:
        ok = kernel32.Thread32First(snap, ctypes.byref(e))
        while ok:
            if e.th32OwnerProcessID == pid:
                count += 1
            ok = kernel32.Thread32Next(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return count


class FileHandleTracker:
    """统计 Python 侧打开的文件数。用法：

        tr = FileHandleTracker().start()
        ...被测代码...
        print(tr.live)
        tr.stop()

    只统计「成功打开且还没 close」的。泄漏判定看 `live` 的长期趋势：
    每轮任务结束应该回落到基线附近。
    """

    def __init__(self):
        self.live = 0
        self.total = 0
        self.peak = 0
        self._lock = threading.Lock()
        self._orig = None
        self._orig_open = None
        self._orig_osopen = None

    def start(self):
        self._orig = builtins.open
        tracker = self

        def _open(*a, **kw):
            f = tracker._orig(*a, **kw)
            return _TrackedFile(f, tracker)

        builtins.open = _open
        self._orig_open = os.open

        def _osopen(*a, **kw):
            fd = tracker._orig_open(*a, **kw)
            with tracker._lock:
                tracker.live += 1
                tracker.total += 1
                tracker.peak = max(tracker.peak, tracker.live)
            return fd
        os.open = _osopen
        return self

    def stop(self):
        if self._orig is not None:
            builtins.open = self._orig
            self._orig = None
        if self._orig_open is not None:
            os.open = self._orig_open
            self._orig_open = None

    def _inc(self):
        with self._lock:
            self.live += 1
            self.total += 1
            self.peak = max(self.peak, self.live)

    def _dec(self):
        with self._lock:
            self.live = max(0, self.live - 1)


class _TrackedFile:
    __slots__ = ("_f", "_t", "_closed")

    def __init__(self, f, tracker):
        object.__setattr__(self, "_f", f)
        object.__setattr__(self, "_t", tracker)
        object.__setattr__(self, "_closed", False)
        tracker._inc()

    def close(self):
        if not self._closed:
            object.__setattr__(self, "_closed", True)
            self._t._dec()
        return self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __iter__(self):
        return iter(self._f)

    def __next__(self):
        return next(self._f)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_f"), name)

    def __del__(self):
        try:
            self.close()
        except Exception:            # noqa: BLE001
            pass


_tracker = FileHandleTracker()


def track_files():
    """全局只装一次：避免采样器自己把 open 包了好几层。"""
    global _tracker
    if _tracker._orig is None:
        _tracker.start()
    return _tracker


def file_handles():
    return _tracker.live


def sample(deep=False):
    """一次采样。deep=True 才统计 GC 存活对象（较慢，别每次都做）。"""
    out = {"ts": time.time(), "pid": os.getpid()}
    out.update(_mem())
    out["handles"] = _handles()
    out.update(_gui())
    out["proc_threads"] = _proc_threads()
    out["py_threads"] = threading.active_count()
    out["open_files"] = file_handles()
    out["cpu_s"] = _cpu_s()
    gc_count = gc.get_count()
    out["gc0"] = gc_count[0]
    out["gc1"] = gc_count[1]
    out["gc2"] = gc_count[2]
    # 累计回收次数（gc.get_count 是「距上次回收又分配了多少」，会回绕，
    # 不能用来判「GC 越来越频繁」）。get_stats() 很便宜，每次都采。
    st = gc.get_stats()
    out["gen0_collections"] = st[0]["collections"]
    out["gen1_collections"] = st[1]["collections"]
    out["gen2_collections"] = st[2]["collections"]
    if tracemalloc.is_tracing():
        cur, peak = tracemalloc.get_traced_memory()
        out["traced_mb"] = round(cur / 1048576, 2)
        out["traced_peak_mb"] = round(peak / 1048576, 2)
    if deep:
        out["objects"] = len(gc.get_objects())
        st = gc.get_stats()
        out["gen0_collections"] = st[0]["collections"]
        out["gen1_collections"] = st[1]["collections"]
        out["gen2_collections"] = st[2]["collections"]
    return out


def snapshot_diff(before, top=8):
    """tracemalloc 快照对比：涨得最多的几处分配。"""
    if before is None:
        return []
    snap = tracemalloc.take_snapshot()
    lines = snap.compare_to(before, "lineno")
    out = []
    for stat in lines[:top]:
        out.append({"where": f"{stat.traceback[0].filename}:{stat.traceback[0].lineno}",
                    "diff_bytes": int(stat.size_diff),
                    "size_bytes": int(stat.size)})
    return out


def fmt_mb(v):
    return f"{v:.2f}MB" if isinstance(v, (int, float)) else "-"
