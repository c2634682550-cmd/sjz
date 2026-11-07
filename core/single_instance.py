r"""单实例闸：同一时间只允许一个控制台在跑。

为什么必须有：两个实例各有自己的调度器和引擎，却共用同一份 config.json
和**同一个鼠标** —— 会重复下单，热键也会互相抢。以前没有这道闸，加上
「关窗不退出」那个 bug，多开成了常态：desktop.log 里端口从 8777 漂到
8780，就是四个进程叠在一起。

实现用 Windows 命名互斥量（CreateMutexW）而不是「试占端口」：
  · 端口可以被 --port 绕开，互斥量不行；
  · 进程被强杀时内核自动释放，不会留下僵尸锁文件。

命名用 Local\ 前缀：同一用户会话内唯一，不跨用户干扰（多用户登录同一台
机器时，各跑各的）。
"""
import ctypes
import json
import os
import sys
import time

MUTEX_NAME = "Local\\SJZ-Control-Console"
RESTORE_EVENT = "Local\\SJZ-Control-Restore"
ALLOW_MULTI_ENV = "SJZ_ALLOW_MULTI"
_ERROR_ALREADY_EXISTS = 183

_handle = None


def _mutex_name():
    """闸名可被 SJZ_MUTEX_NAME 覆盖：验收/排障时隔离用，不影响线上默认值。"""
    return (os.environ.get("SJZ_MUTEX_NAME") or "").strip() or MUTEX_NAME


def acquire(name=None):
    """抢闸。返回 True = 可以继续；False = 已经有一个在跑。

    开发时确实要同时开两个（源码 + 打包版），设 SJZ_ALLOW_MULTI=1。
    """
    global _handle
    name = name or _mutex_name()
    if os.environ.get(ALLOW_MULTI_ENV) == "1":
        return True
    try:
        k32 = ctypes.windll.kernel32
        # 64 位下 HANDLE 是 8 字节，ctypes 默认把返回值当 int(32 位) 截断，
        # 句柄高位丢失后 CloseHandle 关的是一个不存在的值，互斥量泄漏。
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        # GetLastError 是「上一次调用」的残留值，不归零会把上一次别处的
        # 183 当成「已经有实例在跑」（审查 S24）。
        k32.SetLastError(0)
        handle = k32.CreateMutexW(None, False, name)
        err = k32.GetLastError()
        if not handle:
            # 建不出来（权限/内核对象耗尽）：**不能放行**，否则多开又回来了。
            print(f"[single_instance] CreateMutexW 失败（err={err}），"
                  f"无法确认是否已有实例，按「已有实例」处理")
            return False
        if err == _ERROR_ALREADY_EXISTS:
            # 这个 handle 是我们打开的「已有对象」的引用，得关掉，
            # 否则会把别人的互斥量一直挂在进程里
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass
            return False
        _handle = handle
        return True
    except Exception:
        # 非 Windows（没有 kernel32）：没有闸可抢，别把程序拦死
        return True


def release():
    """放掉闸。

    提权重开之前必须放：子进程启动后第一件事就是抢它，抢不到会直接
    退出 —— 那就变成「点了管理员，什么都没发生」。
    """
    global _handle
    if not _handle:
        return
    try:
        ctypes.windll.kernel32.CloseHandle(_handle)
    except Exception:
        pass
    _handle = None


def activate_window(title):
    """把已经在跑的那个窗口提到前面，让用户知道程序在哪儿。

    找不到窗口返回 False（可能在浏览器模式，或者窗口被最小化到托盘）。
    """
    try:
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, title)
        if not hwnd:
            return False
        u.ShowWindow(hwnd, 9)             # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 实例登记
# 「双击没反应」的根因是：抢不到闸 → 找不到窗口 → 打一行日志就退出，
# 用户既看不到窗口也看不到提示，只能重启。下面这组函数给第二个实例
# 提供可排查、可自救的三件事：
#   1) 旧实例是谁（PID + exe 路径，写 instance.json）；
#   2) 它到底有没有窗口（按 PID 枚举，而不是按标题猜）；
#   3) 能不能让它自己把窗口找回来（命名事件），或者干脆结束它。
PROCESS_TERMINATE = 0x0001
PROCESS_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_EVENT_MODIFY_STATE = 0x0002
_INFINITE = 0xFFFFFFFF


def data_dir():
    """和启动器一致：实例登记放 %LOCALAPPDATA%\\SJZ-Control。"""
    env = (os.environ.get("SJZ_DATA_DIR") or "").strip()
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SJZ-Control")


def owner_path():
    return os.path.join(data_dir(), "instance.json")


def write_owner(pid=None, extra=None):
    """登记「当前在跑的是谁」，第二个实例据此定位旧实例。"""
    pid = int(pid or os.getpid())
    d = {"pid": pid, "exe": sys.executable, "started": int(time.time())}
    if extra:
        d.update(extra)
    try:
        os.makedirs(data_dir(), exist_ok=True)
        tmp = owner_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, owner_path())
    except OSError:
        pass
    return d


def read_owner():
    try:
        with open(owner_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def clear_owner(pid=None):
    """删登记。带 pid 时只删自己的，别把新实例的登记抹掉。"""
    try:
        d = read_owner()
        if pid and d and int(d.get("pid") or 0) != int(pid):
            return False
        os.remove(owner_path())
        return True
    except OSError:
        return False


def pid_alive(pid):
    """进程还在不在（不要求权限，只要 SYNCHRONIZE）。"""
    if int(pid or 0) <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        # 句柄是 64 位：不给 WaitForSingleObject/CloseHandle 声明签名，
        # ctypes 会按 32 位 int 传，句柄高位被截掉 → 永远返回「不可等待」，
        # 于是活着的进程被判成已退出（terminate 前的存活检查也会失灵）。
        k32.WaitForSingleObject.restype = ctypes.c_uint
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(PROCESS_SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def windows_for_pid(pid):
    """按 PID 枚举窗口，返回 (总数, 可见数, 可见 hwnd)。

    以前靠标题 FindWindowW 判断「有没有窗口」，无边框窗口标题对不上、
    WebView 内嵌窗口也会漏，结论就成了「可能在浏览器模式」——没法查。
    """
    out = {"total": 0, "visible": 0, "hwnds": []}
    pid = int(pid or 0)
    if pid <= 0:
        return out
    try:
        u = ctypes.windll.user32
        cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lp):
            owner = ctypes.c_ulong()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                out["total"] += 1
                if u.IsWindowVisible(hwnd):
                    out["visible"] += 1
                    out["hwnds"].append(int(hwnd))
            return True

        u.EnumWindows(cb_t(_cb), None)
    except Exception:
        pass
    return out


def activate_pid(pid):
    """按 PID 把窗口恢复并置前（不依赖窗口标题）。"""
    hwnds = windows_for_pid(pid)["hwnds"]
    if not hwnds:
        return False
    try:
        u = ctypes.windll.user32
        hwnd = hwnds[0]
        u.ShowWindow(hwnd, 9)             # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def terminate_pid(pid):
    """同权限自我收尾：结束残留实例（提权实例会返回 False）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return False
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        k32.TerminateProcess.restype = ctypes.c_int
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def create_restore_event():
    """旧实例监听这个命名事件：收到信号就把自己的窗口找回来。"""
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_wchar_p]
        return k32.CreateEventW(None, False, False, RESTORE_EVENT)
    except Exception:
        return None


def signal_restore():
    """通知旧实例恢复窗口；它没在监听就返回 False。"""
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenEventW.restype = ctypes.c_void_p
        k32.OpenEventW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
        h = k32.OpenEventW(_EVENT_MODIFY_STATE, False, RESTORE_EVENT)
        if not h:
            return False
        try:
            return bool(k32.SetEvent(h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def wait_restore(handle):
    """阻塞等恢复信号（在工作线程里调）。"""
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, _INFINITE) == 0
    except Exception:
        return False


def mutex_free(name=None):
    """单实例闸现在空着吗（真去建一次，建到就立刻关掉）。"""
    name = name or _mutex_name()
    if os.environ.get(ALLOW_MULTI_ENV) == "1":
        return True
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        k32.SetLastError(0)
        h = k32.CreateMutexW(None, False, name)
        err = k32.GetLastError()
        if not h:
            return False
        try:
            return err != _ERROR_ALREADY_EXISTS
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False
