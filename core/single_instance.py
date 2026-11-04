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
import os

MUTEX_NAME = "Local\\SJZ-Control-Console"
ALLOW_MULTI_ENV = "SJZ_ALLOW_MULTI"
_ERROR_ALREADY_EXISTS = 183

_handle = None


def acquire(name=MUTEX_NAME):
    """抢闸。返回 True = 可以继续；False = 已经有一个在跑。

    开发时确实要同时开两个（源码 + 打包版），设 SJZ_ALLOW_MULTI=1。
    """
    global _handle
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
