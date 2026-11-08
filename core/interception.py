"""Interception 驱动绑定 —— 内核级鼠标/键盘注入。

为什么换掉 SendInput/PostMessage 组合（2026-10-30 决定）：
游戏读 Raw Input，SendInput 注入的事件带 LLMHF_INJECTED 标志被它过滤；
PostMessage 又依赖真实光标位置。SetCursorPos+PostMessage 组合能用，但每次
click 要 0.13s 左右（挪光标 40ms + 消息 60ms + 校验 50ms），火力点击太慢。

Interception（oblitum/Interception，Oblita 官方 v1.00，家用免费）挂在
kbdclass/mouclass 之上：程序把事件发给驱动，驱动在内核注回输入栈 ——
游戏收到的是**真实硬件事件**，低级钩子看不到注入标志（实测 WH_MOUSE_LL
收不到任何事件），**但系统光标会被真实移动**：它是货真价实的鼠标输入，
不是「幽灵事件」。所以驱动注入只能算「可选增强」，不能算零干扰。

2026-11-07 实测补充（tools/bench_input.py，1000 样本/路径，独立观测进程）：
- 驱动注入的每条 stroke 都会进 Raw Input：一次 click（move+down+up）
  在观测窗口收到约 3.1 条 WM_INPUT（已扣掉本机 81.7 条/s 的本底噪声）。
- 消息到达跨进程观测窗口 p50 0.477ms / p95 0.796ms（SendInput 0.410/0.945）。
- **注入是异步的**：interception_send 返回（≈0.017ms）时光标还没动，
  要过约 0.1~0.7ms 才落地。所以「send 返回后立刻 SetCursorPos 还原」
  会还原失败（还原发生在落地之前），必须轮询确认落地再还原。
- create_context 在驱动未装载时仍返回非 NULL → 就绪判定必须真发一次事件。

DLL 按位数放在 core/assets/（或打包后的 _MEIPASS），路径一律按 __file__
相对解析，不写死本机目录。驱动必须先装好：
    install-interception.exe /install    （管理员 + 重启一次）

API 备忘（照 library/interception.h 抄，不能凭感觉填）：
- 设备编号 1..10 = 键盘，11..20 = 鼠标（INTERCEPTION_KEYBOARD/MOUSE 宏）
- stroke 大小 = sizeof(InterceptionMouseStroke) = 20 字节（i16×3 + i32×2
  + u32，按 4 对齐）。InterceptionStroke 就是这个 20 字节缓冲，键盘的
  8 字节 KeyStroke 装进它的前 8 字节。
- interception_send 返回发送的 stroke 数；驱动没装时 create_context
  返回 NULL 或 send 返回 0 —— 两种失败都要当「驱动不可用」。
- 绝对坐标：flag INTERCEPTION_MOUSE_MOVE_ABSOLUTE + 坐标 0..65535，
  加 VIRTUAL_DESKTOP 跨多显示器时按虚拟屏算。
"""
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import subprocess
import sys
import threading
import time

# ---------------- 常量（interception.h 原文） ----------------

MAX_KEYBOARD = 10
MAX_MOUSE = 10

KEYBOARD = lambda index: index + 1                    # noqa: E731
MOUSE = lambda index: MAX_KEYBOARD + index + 1        # noqa: E731

# 键盘 stroke 的 state 位
KEY_DOWN = 0x00
KEY_UP = 0x01
KEY_E0 = 0x02

# 鼠标 stroke 的 state
MOUSE_LEFT_DOWN = 0x001
MOUSE_LEFT_UP = 0x002
MOUSE_WHEEL = 0x400

# 鼠标 stroke 的 flag
MOUSE_MOVE_RELATIVE = 0x000
MOUSE_MOVE_ABSOLUTE = 0x001
MOUSE_VIRTUAL_DESKTOP = 0x002


class MouseStroke(ctypes.Structure):
    """InterceptionMouseStroke，20 字节。

    布局是 u16 state / u16 flags / i16 rolling / i32 x / i32 y / u32 information；
    rolling 后面有 2 字节对齐填充，所以不是 2+2+2+4+4+4=18，而是 20。
    send 时按这个大小传，尺寸算错驱动会直接收 0（点击静默失效）。
    """
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_short),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("information", ctypes.c_uint),
    ]


class KeyStroke(ctypes.Structure):
    """InterceptionKeyStroke，8 字节。"""
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    ]


def _dll_candidates():
    """DLL 的查找顺序。只按 __file__ 相对解析，不写死任何本机目录。

    打包 exe 时 DLL 跟着 _MEIPASS 走（spec 里 datas 带上）；
    源码运行时放 core/assets/（随仓库）；同目录放一份也认。
    """
    out = []
    if getattr(sys, "frozen", False):
        out.append(os.path.join(sys._MEIPASS, "interception.dll"))  # noqa: SLF001
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "assets", "interception.dll"))
    out.append(os.path.join(here, "interception.dll"))
    return out


class InterceptionError(RuntimeError):
    """驱动不可用（没装 / 没重启 / DLL 缺失）。"""


class _Lib:
    """惰性加载 + API 签名。加载一次，失败记住原因。"""

    def __init__(self):
        self._lib = None
        self._err = None

    def load(self):
        if self._lib is not None or self._err is not None:
            return self._lib
        for path in _dll_candidates():
            if not os.path.isfile(path):
                continue
            try:
                lib = ctypes.WinDLL(path)
            except OSError as e:
                self._err = f"{path}: {e}"
                continue
            try:
                # 签名照 interception.h：context 是 void*，send 收 (ctx, dev,
                # stroke*, n) 返回 nstroke 数
                lib.interception_create_context.restype = ctypes.c_void_p
                lib.interception_create_context.argtypes = []
                lib.interception_destroy_context.argtypes = [ctypes.c_void_p]
                lib.interception_send.restype = ctypes.c_uint
                lib.interception_send.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
                lib.interception_is_keyboard.restype = ctypes.c_int
                lib.interception_is_keyboard.argtypes = [ctypes.c_int]
                lib.interception_is_mouse.restype = ctypes.c_int
                lib.interception_is_mouse.argtypes = [ctypes.c_int]
                # filter 必须能读写：create_context 的默认 filter 在部分
                # 构建里不是 0，靠默认值就有吞掉物理鼠标的风险（见 _open）
                lib.interception_set_filter.restype = None
                lib.interception_set_filter.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ushort]
                lib.interception_get_filter.restype = ctypes.c_uint
                lib.interception_get_filter.argtypes = [ctypes.c_void_p,
                                                        ctypes.c_int]
                # 设备身份：哪台设备是真实鼠标，直接决定注入是否会被判外挂
                lib.interception_get_hardware_id.restype = ctypes.c_uint
                lib.interception_get_hardware_id.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                    ctypes.c_uint]
                lib.interception_is_invalid.restype = ctypes.c_int
                lib.interception_is_invalid.argtypes = [ctypes.c_int]
                # 部分构建多导出一个 interception_get_device；有就用，没有就
                # 退回 hardware_id 长度判定（两路都在，不依赖单一来源）
                get_device = getattr(lib, "interception_get_device", None)
                if get_device is not None:
                    try:
                        get_device.restype = ctypes.c_int
                        get_device.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_int]
                    except Exception:
                        pass
            except AttributeError as e:
                self._err = f"{path}: 缺函数 {e}"
                continue
            self._lib = lib
            self.path = path
            return self._lib
        if self._err is None:
            self._err = "interception.dll 找不到（应放在 core/assets/ 下）"
        return None

    def error(self):
        if self._lib is None and self._err is None:
            self.load()
        return self._err


_LIB = _Lib()


# ---------------- 设备身份：真实设备 / 虚拟设备 ----------------
#
# 为什么要分：Interception 的 20 个设备槽里，本机实测同时存在
#   真实鼠标  dev14  HID\VID_3554&PID_F50D&REV_0121&MI_02   （LAMZU 接收器）
#   真实键盘  dev4   HID\VID_3554&PID_F50D&REV_0121&MI_00
#   虚拟鼠标  dev11  HID\HID_DEVICE_SYSTEM_VHF&Col02 / VID_5E2A&PID_71B4
# 把事件注入到虚拟设备（dev11/12）虽然也能动光标，但 Raw Input 里 hDevice
# 归属的是一台 VHF 虚拟设备 —— 这正是反作弊最容易抓的「不是你那只会动的
# 鼠标」。所以默认只允许真实设备；虚拟设备必须显式放行（input_allow_virtual）。

REAL_DEVICE_MISSING = "REAL_DEVICE_MISSING"   # 一个真实设备都没有
VIRTUAL_DEVICE_ONLY = "VIRTUAL_DEVICE_ONLY"   # 只有虚拟设备
DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"         # 显式指定的设备号不存在
DRIVER_UNAVAILABLE = "DRIVER_UNAVAILABLE"
DRIVER_SERVICE_MISSING = "DRIVER_SERVICE_MISSING"   # 驱动文件/服务不存在
DRIVER_NOT_RUNNING = "DRIVER_NOT_RUNNING"           # 服务在但没跑 / 设备打不开
DRIVER_SEND_FAILED = "DRIVER_SEND_FAILED"

# 硬件 ID 里出现这些标记就是虚拟/软件设备：
#   VHF = Virtual HID Framework（Windows 虚拟 HID 设备）
#   ROOT\ = 无物理父设备（软件枚举，例如远控/宏/虚拟外设驱动）
VIRTUAL_ID_MARKS = ("VHF", "ROOT\\", "ROOT#")

_RE_VIDPID = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")
_RE_MI = re.compile(r"MI_([0-9A-Fa-f]{2})")


def _interface_key(hardware_id):
    """(vid, pid, MI_xx) —— 用来判定「这个鼠标集合是不是键盘那个接口的附赠」。"""
    match = _RE_VIDPID.search(hardware_id or "")
    if not match:
        return None
    mi = _RE_MI.search(hardware_id or "")
    return (match.group(1).upper(), match.group(2).upper(),
            mi.group(1).upper() if mi else "")

_DEV_CACHE = {}
_DEV_LOCK = threading.RLock()
_DEV_TTL = 8.0


def _cfg_int(key, default):
    """读配置，读不到就用默认值（不写死用户配置）。"""
    try:
        from .config import Config
        value = Config.load().get(key, default)
        return int(value)
    except Exception:
        return default


def _cfg_bool(key, default):
    try:
        from .config import Config
        value = Config.load().get(key, default)
        return bool(value)
    except Exception:
        return default


def classify_hardware_id(hardware_id):
    """硬件 ID → ('absent'|'virtual'|'real', vid, pid)。"""
    if not hardware_id:
        return "absent", "", ""
    upper = hardware_id.upper()
    virtual = upper.startswith("ROOT") or any(
        mark in upper for mark in VIRTUAL_ID_MARKS)
    match = _RE_VIDPID.search(hardware_id)
    vid = match.group(1).upper() if match else ""
    pid = match.group(2).upper() if match else ""
    return ("virtual" if virtual else "real"), vid, pid


def _read_hardware_id(lib, ctx, device):
    """读设备硬件 ID。返回 (文本, 字节长度)；空 = 该槽位没有设备。"""
    buf = ctypes.create_string_buffer(1024)
    n = lib.interception_get_hardware_id(ctx, device, buf, 1024)
    if not n:
        return "", 0
    try:
        parts = [p for p in buf.raw[:n].decode("utf-16-le", "replace")
                 .split("\x00") if p]
    except Exception:
        return "", 0
    return "|".join(parts), int(n)


def _device_present(lib, ctx, device):
    """设备槽位是否有设备。优先用 interception_get_device（有的构建导出），
    没有该导出时退回硬件 ID 长度（驱动自己的设备枚举接口就是它）。"""
    get_device = getattr(lib, "interception_get_device", None)
    if get_device is not None:
        try:
            return bool(get_device(ctx, None, device))
        except Exception:
            pass
    hw, n = _read_hardware_id(lib, ctx, device)
    return n > 0 and bool(hw)


def enumerate_devices(kind="mouse", force=False, ttl=None):
    """枚举鼠标(11..20)/键盘(1..10)槽位，返回设备身份表。

    每行：{"device", "index", "exists", "kind": real|virtual|absent,
           "hardware_id", "vid", "pid", "virtual"}
    """
    ttl = _DEV_TTL if ttl is None else ttl
    with _DEV_LOCK:
        cached = _DEV_CACHE.get(kind)
        if cached and not force and (time.monotonic() - cached[0]) < ttl:
            return [dict(row) for row in cached[1]]
        lib = _LIB.load()
        if lib is None:
            raise InterceptionError(_LIB.error())
        ctx = _SHARED._open()  # noqa: SLF001 同模块内共享 context
        rows = []
        total = MAX_KEYBOARD if kind == "keyboard" else MAX_MOUSE
        base = 1 if kind == "keyboard" else MAX_KEYBOARD + 1
        for index in range(total):
            device = base + index
            hardware_id, n = _read_hardware_id(lib, ctx, device)
            state, vid, pid = classify_hardware_id(hardware_id)
            present = _device_present(lib, ctx, device) if n else False
            if not present:
                state = "absent"
            rows.append({
                "device": device,
                "index": index,
                "exists": present and state != "absent",
                "kind": state,
                "virtual": state == "virtual",
                "hardware_id": hardware_id,
                "vid": vid,
                "pid": pid,
            })
        _DEV_CACHE[kind] = (time.monotonic(), rows)
        return [dict(row) for row in rows]


class RealDeviceUnavailable(InterceptionError):
    """枚举不到可用的真实设备（或指定的设备是虚拟设备）。"""

    def __init__(self, message, reason_code=REAL_DEVICE_MISSING,
                 candidates=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.candidates = list(candidates or [])


# ---- 被动活动度探针：多只真实鼠标时挑「你正在用的那只」 ----
#
# 只注册 Raw Input（RIDEV_INPUTSINK），不设 filter、不钩、不发事件：
# 纯旁观。用户 1 秒内没有移动鼠标就按设备号顺序兜底，并在结果里标
# source=auto_first，绝不假装是「选中的那只」。

class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class _RAWINPUTDEVICELIST(ctypes.Structure):
    _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


_VPID = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")


def _raw_device_names(user32):
    """Raw Input 设备句柄 → 设备接口名（含 VID/PID）。"""
    count = wintypes.UINT(0)
    user32.GetRawInputDeviceList(None, ctypes.byref(count),
                                 ctypes.sizeof(_RAWINPUTDEVICELIST))
    if not count.value:
        return {}
    arr = (_RAWINPUTDEVICELIST * count.value)()
    got = wintypes.UINT(count.value)
    if not user32.GetRawInputDeviceList(arr, ctypes.byref(got),
                                        ctypes.sizeof(_RAWINPUTDEVICELIST)):
        return {}
    names = {}
    for i in range(got.value):
        handle = arr[i].hDevice
        if not handle:
            continue
        size = wintypes.UINT(0)
        user32.GetRawInputDeviceInfoW(handle, 0x20000007, None,
                                      ctypes.byref(size))
        if not size.value:
            continue
        buf = ctypes.create_unicode_buffer(size.value + 1)
        user32.GetRawInputDeviceInfoW(handle, 0x20000007, buf,
                                      ctypes.byref(size))
        names[int(handle)] = buf.value
    return names


def raw_mouse_activity(ms_budget=1200):
    """旁观 Raw Input，返回 {"by_vidpid": {"3554_F50D": n}, "names": {...}}。"""
    user32 = ctypes.windll.user32
    counts = {}
    holder = {}
    lock = threading.Lock()

    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterRawInputDevices.restype = wintypes.BOOL
    user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                               wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                       ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.UINT),
                                       wintypes.UINT]
    user32.GetRawInputDeviceList.restype = wintypes.UINT
    user32.GetRawInputDeviceList.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]
    user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
    user32.GetRawInputDeviceInfoW.argtypes = [
        wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT)]

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                 wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM)

    def wndproc(hwnd, msg, wp, lp):
        if msg == 0x00FF:  # WM_INPUT，lParam 是 HRAWINPUT
            size = wintypes.UINT(0)
            user32.GetRawInputData(lp, 0x10000003, None,
                                   ctypes.byref(size),
                                   ctypes.sizeof(_RAWINPUTHEADER))
            buf = ctypes.create_string_buffer(max(1, size.value))
            n = user32.GetRawInputData(lp, 0x10000003, buf,
                                       ctypes.byref(size),
                                       ctypes.sizeof(_RAWINPUTHEADER))
            if n:
                hdr = ctypes.cast(buf, ctypes.POINTER(_RAWINPUTHEADER)).contents
                if hdr.hDevice:
                    with lock:
                        counts[int(hdr.hDevice)] = counts.get(
                            int(hdr.hDevice), 0) + 1
                        holder.update(counts)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    proc_ref = WNDPROC(wndproc)
    class_name = f"TraeInputWatch{os.getpid()}"
    try:
        user32.RegisterClassW
    except AttributeError:
        return {"by_vidpid": {}, "counts": {}, "names": {}}
    WNDCLASS = type("WNDCLASS", (ctypes.Structure,), {"_fields_": [
        ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]})
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASS(0, proc_ref, 0, 0, hinst, None, None, None, None, class_name)
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, class_name, "trae-watch", 0,
                                  0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        return {"by_vidpid": {}, "counts": {}, "names": {}}
    rid = _RAWINPUTDEVICE(1, 2, 0x00000100, hwnd)  # RIDEV_INPUTSINK
    user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                   ctypes.sizeof(_RAWINPUTDEVICE))
    names = _raw_device_names(user32)
    deadline = time.monotonic() + max(0.0, ms_budget / 1000.0)
    msg = wintypes.MSG()
    while time.monotonic() < deadline:
        moved = False
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            moved = True
        if not moved:
            time.sleep(0.002)
        with lock:
            if counts:
                break
    user32.DestroyWindow(hwnd)
    by_vidpid = {}
    for hdev, cnt in holder.items():
        name = names.get(hdev, "")
        m = _VPID.search(name)
        key = f"{m.group(1).upper()}_{m.group(2).upper()}" if m else f"hdev{hdev}"
        by_vidpid[key] = by_vidpid.get(key, 0) + cnt
    return {"by_vidpid": by_vidpid, "counts": dict(holder), "names": names}


class PhysicalMouseWatch:
    """旁路监听**物理鼠标**的 Raw Input 活动，不拦截、不阻塞。

    为什么不用 interception_set_filter 监听：driver 的 filter 位一旦打开，
    事件会先进本进程队列、必须 interception_send 再放行，物理鼠标就被人为
    拖慢甚至卡住（正是「抢鼠标」的另一面）。Raw Input 的 RIDEV_INPUTSINK
    只是复制一份给本进程，事件照常到游戏，filter 保持 0。

    注入事件的剔除：注入与本机物理鼠标共用同一设备号，Raw Input 无法从
    事件本身区分来源；所以由发送方在注入窗口内调 `suppress()`，窗口内到达
    的事件计入 injected_skipped，不触发让路。

    回调在监听线程里执行，必须极轻（只置标志）。
    """

    def __init__(self, vidpid=None, on_activity=None):
        self.vidpid = {str(v).upper() for v in (vidpid or []) if v}
        self._cb = on_activity
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._hwnd = None
        self._proc_ref = None
        self._lock = threading.Lock()
        self.suppress_until = 0.0
        self.events = 0
        self.injected_skipped = 0
        self.other_device = 0
        self.last_at = 0.0
        self.error = None

    # ---- 生命周期 ----
    def start(self, timeout=1.0):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="phys-mouse-watch",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ready.is_set()

    def stop(self):
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=0.6)
        self._thread = None

    # ---- 供发送方调用 ----
    def suppress(self, ms=8.0):
        """声明接下来 ms 内的事件是注入产生的，不算人工操作。"""
        now = time.perf_counter()
        self.suppress_until = max(self.suppress_until, now + ms / 1000.0)

    def poke(self, n=1):
        """测试用：模拟一次物理鼠标活动，走与真实事件完全相同的回调路径。"""
        for _ in range(n):
            self._fire(time.perf_counter())

    def _fire(self, ts):
        self.events += 1
        self.last_at = ts
        cb = self._cb
        if cb is not None:
            try:
                cb(ts)
            except Exception:                                # noqa: BLE001
                pass

    # ---- 内部 ----
    def _run(self):
        try:
            self._loop()
        except Exception as e:                               # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()

    def _loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                                   wintypes.UINT]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.UINT),
                                           wintypes.UINT]
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                     wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM)
        names = _raw_device_names(user32)

        def wndproc(hwnd, msg, wp, lp):
            if msg == 0x00FF:  # WM_INPUT
                size = wintypes.UINT(0)
                user32.GetRawInputData(lp, 0x10000003, None, ctypes.byref(size),
                                       ctypes.sizeof(_RAWINPUTHEADER))
                buf = ctypes.create_string_buffer(max(1, size.value))
                n = user32.GetRawInputData(lp, 0x10000003, buf,
                                           ctypes.byref(size),
                                           ctypes.sizeof(_RAWINPUTHEADER))
                if n:
                    hdr = ctypes.cast(buf,
                                      ctypes.POINTER(_RAWINPUTHEADER)).contents
                    if hdr.dwType == 0:  # RIM_TYPEMOUSE
                        if self.vidpid:
                            m = _VPID.search(names.get(int(hdr.hDevice), ""))
                            key = (f"{m.group(1).upper()}_{m.group(2).upper()}"
                                   if m else "")
                            if key and key not in self.vidpid:
                                self.other_device += 1
                                return 0
                        now = time.perf_counter()
                        with self._lock:
                            if now < self.suppress_until:
                                self.injected_skipped += 1
                                return 0
                        self._fire(now)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc_ref = WNDPROC(wndproc)
        class_name = f"TraePhysMouse{os.getpid()}"
        WNDCLASS = type("WNDCLASS", (ctypes.Structure,), {"_fields_": [
            ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]})
        hinst = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASS(0, self._proc_ref, 0, 0, hinst,
                                                     None, None, None, None,
                                                     class_name)))
        self._hwnd = user32.CreateWindowExW(0, class_name, "trae-phys", 0,
                                            0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            raise InterceptionError("物理鼠标旁路监听窗口创建失败")
        rid = _RAWINPUTDEVICE(1, 2, 0x00000100, self._hwnd)  # RIDEV_INPUTSINK
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(_RAWINPUTDEVICE))
        msg = wintypes.MSG()
        self._ready.set()
        while not self._stop.is_set():
            got = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                got = True
            if not got:
                time.sleep(0.002)
        user32.DestroyWindow(self._hwnd)
        self._hwnd = None


_PHYS_WATCH = None
_PHYS_LOCK = threading.Lock()


def physical_mouse_watch(vidpid=None, on_activity=None):
    """进程级单例：物理鼠标活动监听（首次调用才建线程）。"""
    global _PHYS_WATCH
    with _PHYS_LOCK:
        if _PHYS_WATCH is None:
            w = PhysicalMouseWatch(vidpid=vidpid, on_activity=on_activity)
            if not w.start():
                return None
            _PHYS_WATCH = w
        else:
            if on_activity is not None:
                _PHYS_WATCH._cb = on_activity
            if vidpid:
                _PHYS_WATCH.vidpid = {str(v).upper() for v in vidpid}
        return _PHYS_WATCH


def select_mouse(prefer=None, allow_virtual=None, probe_ms=None):
    """挑一只用于注入的鼠标设备。

    优先级：显式 device id → 唯一真实设备 → 被动活动度（多只时）→ 顺序兜底。
    allow_virtual=False（默认）时，虚拟设备一律拒绝，连配置指定也不放行。
    """
    if allow_virtual is None:
        allow_virtual = _cfg_bool("input_allow_virtual", False)
    if prefer is None:
        prefer = _cfg_int("input_mouse_device", 0)
    if probe_ms is None:
        probe_ms = _cfg_int("input_mouse_activity_ms", 1200)
    mice = enumerate_devices("mouse", force=True)
    present = [d for d in mice if d["exists"]]
    real = [d for d in present if d["kind"] == "real"]
    virtual = [d for d in present if d["kind"] == "virtual"]

    if prefer:
        hit = [d for d in present if d["device"] == int(prefer)]
        if not hit:
            raise RealDeviceUnavailable(
                f"配置的鼠标设备号 {prefer} 不存在（当前只有 "
                f"{[d['device'] for d in present] or '无设备'}）",
                DEVICE_NOT_FOUND, present)
        chosen = dict(hit[0])
        if chosen["virtual"] and not allow_virtual:
            raise RealDeviceUnavailable(
                f"设备 {prefer} 是虚拟鼠标（{chosen['hardware_id'][:70]}），"
                "按真实设备要求不使用；确需使用请设 input_allow_virtual",
                VIRTUAL_DEVICE_ONLY, present)
        chosen["source"] = "config"
        chosen["candidates"] = present
        return chosen

    if not real:
        if allow_virtual and virtual:
            chosen = dict(virtual[0])
            chosen["source"] = "auto_virtual"
            chosen["candidates"] = present
            return chosen
        code = VIRTUAL_DEVICE_ONLY if virtual else REAL_DEVICE_MISSING
        raise RealDeviceUnavailable(
            "枚举不到真实鼠标设备" +
            (f"（只有 {len(virtual)} 个虚拟设备）" if virtual else "（没有设备）") +
            "；不会退回虚拟设备号", code, present)

    # 排除「键盘附赠的鼠标集合」：键盘(比如带旋钮/触摸的型号)会在同一个
    # MI_xx 接口上再挂一个 HID 鼠标集合。它确实是真实设备，但不是用户手上
    # 那只鼠标。判据：该鼠标的 (VID,PID,MI_xx) 与某个真实键盘接口完全一致。
    kbd_keys = {_interface_key(d["hardware_id"])
                for d in enumerate_devices("keyboard")
                if d["kind"] == "real"}
    kbd_keys.discard(None)
    pointers = [d for d in real
                if _interface_key(d["hardware_id"]) not in kbd_keys]
    pool = pointers or real

    if len(pool) == 1:
        chosen = dict(pool[0])
        chosen["source"] = ("auto_single" if len(real) == 1
                            else "auto_pointer")
        chosen["candidates"] = present
        return chosen

    activity = {}
    if probe_ms > 0:
        try:
            activity = raw_mouse_activity(probe_ms)["by_vidpid"]
        except Exception:
            activity = {}
    best = None
    best_hits = 0
    for row in pool:
        key = f"{row['vid']}_{row['pid']}" if row["vid"] else ""
        hits = activity.get(key, 0)
        if hits > best_hits:
            best, best_hits = row, hits
    if best is not None:
        chosen = dict(best)
        chosen["source"] = "auto_activity"
        chosen["candidates"] = present
        return chosen
    chosen = dict(pool[0])
    chosen["source"] = "auto_first"
    chosen["candidates"] = present
    return chosen


def select_keyboard(prefer=None, allow_virtual=None):
    """键盘同策略：默认挑真实键盘，不用虚拟键盘槽。"""
    if allow_virtual is None:
        allow_virtual = _cfg_bool("input_allow_virtual", False)
    if prefer is None:
        prefer = _cfg_int("input_keyboard_device", 0)
    rows = enumerate_devices("keyboard", force=True)
    present = [d for d in rows if d["exists"]]
    real = [d for d in present if d["kind"] == "real"]
    virtual = [d for d in present if d["kind"] == "virtual"]
    if prefer:
        hit = [d for d in present if d["device"] == int(prefer)]
        if not hit:
            raise RealDeviceUnavailable(
                f"配置的键盘设备号 {prefer} 不存在", DEVICE_NOT_FOUND, present)
        chosen = dict(hit[0])
        if chosen["virtual"] and not allow_virtual:
            raise RealDeviceUnavailable(
                f"设备 {prefer} 是虚拟键盘（{chosen['hardware_id'][:70]}）",
                VIRTUAL_DEVICE_ONLY, present)
        chosen["source"] = "config"
        chosen["candidates"] = present
        return chosen
    if not real:
        if allow_virtual and virtual:
            chosen = dict(virtual[0])
            chosen["source"] = "auto_virtual"
            chosen["candidates"] = present
            return chosen
        raise RealDeviceUnavailable(
            "枚举不到真实键盘设备；不会退回虚拟设备号",
            VIRTUAL_DEVICE_ONLY if virtual else REAL_DEVICE_MISSING, present)
    chosen = dict(real[0])
    chosen["source"] = "auto_first"
    chosen["candidates"] = present
    return chosen


class Interception:
    """一个进程一个 context。注入目标 = 枚举出来的**真实设备**。

    设备不是写死的：`mouse_device()` 用硬件 ID 区分真实/虚拟，默认只接受
    真实鼠标；枚举不到就抛 `RealDeviceUnavailable`，由上层明确降级，
    绝不退回 11 号（虚拟）设备号「凑合用」。
    """

    # 显式能力位：本实现支持整批提交。上层只认这个契约，不靠
    # `hasattr(drv, "send_mouse_batch")` 的偶然性判断 —— 驱动对象被代理/
    # façade/测试替身包过时方法会藏起来，那样会静默退回逐条发送，
    # 性能掉一档却没有任何记录。
    supports_batch = True

    def __init__(self):
        self._ctx = None
        self._lock_err = None
        self._mouse_dev = None
        self._kbd_dev = None
        # perf：设备代际。重新枚举/切换设备时 +1，上层据此丢弃缓存的设备
        # 句柄，避免「设备热插拔后还拿旧编号往空气里发事件」。
        self.gen = 0
        # 设备数快照 + 上次热插拔探测时刻（最多 1s 查一次，避免每击枚举）
        self._mouse_count = None
        self._enum_at = 0.0
        # perf：一次提交多条 stroke 的复用缓冲（8 条够一次点击的
        # move/down/up/还原；不够就临时扩，不截断）。
        self._batch_buf = (MouseStroke * 8)()
        # 多线程（引擎线程 + 热键/扫描线程）同时第一次点击时会各建一个
        # context：后建的那个覆盖 self._ctx，先建的那个内核句柄就永久泄漏，
        # 而且两个 context 互相抢设备。创建/销毁必须串行化。
        self._lock = threading.RLock()

    def _open(self):
        with self._lock:
            if self._ctx is not None:
                return self._ctx
            lib = _LIB.load()
            if lib is None:
                raise InterceptionError(_LIB.error())
            ctx = lib.interception_create_context()
            # 驱动没装时 create_context 可能返回 NULL，也可能返回一个
            # send 全失败的 context —— 两种都等第一次 send 才暴露。
            if not ctx:
                raise InterceptionError(
                    "Interception 驱动不可用：先以管理员运行 "
                    "install-interception.exe install 并重启")
            # FILTER_NONE：我们只发事件、不截获设备事件，物理鼠标原样透传。
            # 本机实测 create_context 的默认 filter 已是 0，但不同构建/
            # 不同版本未必 —— 显式置 0，杜绝「创建 context 就把物理鼠标吞掉」
            # 这种静默事故（用户报的「会抢鼠标」就是这一类）。
            lib.interception_set_filter(
                ctx, ctypes.cast(lib.interception_is_mouse, ctypes.c_void_p), 0)
            lib.interception_set_filter(
                ctx, ctypes.cast(lib.interception_is_keyboard, ctypes.c_void_p), 0)
            self._ctx = ctx
            # 新 context 意味着设备句柄可能重排：代际 +1，旧缓存作废。
            self.gen += 1
            self._mouse_dev = None
            self._kbd_dev = None
            return self._ctx

    def close(self):
        with self._lock:
            if self._ctx is not None:
                lib = _LIB.load()
                if lib is not None:
                    lib.interception_destroy_context(self._ctx)
                self._ctx = None
            # close 之后设备号一律不可信：bump 代际并清缓存，别让下一次
            # 点击用一个已经销毁的 context 里选出来的旧编号。
            if self._mouse_dev is not None or self._kbd_dev is not None:
                self.gen += 1
            self._mouse_dev = None
            self._kbd_dev = None
            self._mouse_count = None

    def mouse_device(self, force=False):
        """当前注入用的真实鼠标设备（dict）。首次调用做枚举/选择。"""
        with self._lock:
            if self._mouse_dev is not None and not force:
                # 热插拔探测：枚举结果有缓存，最多每 1s 比一次设备数；
                # 数量变了说明设备重排，旧编号不能再信。
                now = time.monotonic()
                if now - self._enum_at >= 1.0:
                    self._enum_at = now
                    try:
                        count = len(enumerate_devices("mouse"))
                        if self._mouse_count is not None \
                                and count != self._mouse_count:
                            force = True
                    except Exception:                       # noqa: BLE001
                        pass
            if self._mouse_dev is None or force:
                self._open()
                self._mouse_dev = select_mouse()
                try:
                    self._mouse_count = len(enumerate_devices("mouse"))
                except Exception:                           # noqa: BLE001
                    self._mouse_count = None
                self._enum_at = time.monotonic()
                self.gen += 1
            return dict(self._mouse_dev)

    def keyboard_device(self, force=False):
        with self._lock:
            if self._kbd_dev is None or force:
                self._open()
                self._kbd_dev = select_keyboard()
            return dict(self._kbd_dev)

    def identity(self):
        """通道身份快照，给 channel_status()/前端显示用。"""
        out = {"mouse": None, "keyboard": None, "driver": getattr(_LIB, "path", None)}
        try:
            out["mouse"] = self.mouse_device()
        except RealDeviceUnavailable as e:
            out["mouse"] = {"error": str(e), "reason_code": e.reason_code}
        try:
            out["keyboard"] = self.keyboard_device()
        except RealDeviceUnavailable as e:
            out["keyboard"] = {"error": str(e), "reason_code": e.reason_code}
        return out

    def send_mouse(self, state, x=0, y=0, rolling=0, flags=MOUSE_MOVE_RELATIVE,
                   device=None):
        with self._lock:
            ctx = self._open()
            lib = _LIB.load()
            target = device if device is not None else self.mouse_device()["device"]
            stroke = MouseStroke(state=state, flags=flags, rolling=rolling,
                                 x=x, y=y, information=0)
            n = lib.interception_send(ctx, target, ctypes.byref(stroke), 1)
            if n != 1:
                # 发不出去：设备号大概率已失效（拔插/重载）。清缓存 + bump
                # 代际，让下一次点击重新枚举，而不是一直拿旧号撞墙。
                self._mouse_dev = None
                self.gen += 1
                raise InterceptionError(
                    f"Interception 驱动没有接收事件（send 返回 {n}，设备 "
                    f"{target}）。常见原因：装完驱动没重启；或驱动被卸载/"
                    f"被杀软清掉；或该设备号已被拔掉。")
        return n

    def send_mouse_batch(self, strokes, device=None):
        """一次提交多条 mouse stroke（同一次锁、同一个驱动调用）。

        strokes: [(state, x, y, flags), ...]，按顺序进设备 FIFO。
        为什么重要：单击要 move/down/up/还原 四条事件，逐条 send 就是
        四次 RLock + 四次 DLL 调用 + 四份结构体；更要命的是每条之间会被
        其它线程插队，落地时间抖动。合成一次后设备队列里的顺序有保证。
        """
        n = len(strokes)
        if n <= 0:
            return 0
        with self._lock:
            ctx = self._open()
            lib = _LIB.load()
            target = device if device is not None else self.mouse_device()["device"]
            arr = self._batch_buf
            if len(arr) < n:
                arr = (MouseStroke * n)()
                self._batch_buf = arr
            for i, (state, x, y, flags) in enumerate(strokes):
                arr[i].state = state
                arr[i].flags = flags
                arr[i].rolling = 0
                arr[i].x = x
                arr[i].y = y
                arr[i].information = 0
            got = lib.interception_send(ctx, target, ctypes.byref(arr), n)
            if got != n:
                # 部分/全部没进队列：旧设备号不可信。清缓存并 bump 代际；
                # sent 让上层区分「0 条入队（可安全重发）」和「部分入队
                # （再发会重复 down/up，只能如实失败）」。
                self._mouse_dev = None
                self.gen += 1
                err = InterceptionError(
                    f"Interception 驱动没有接收整批事件（send 返回 {got}/"
                    f"{n}，设备 {target}）。")
                err.sent = got
                raise err
            return got

    def send_key(self, scan, up=False, e0=False, device=None):
        with self._lock:
            ctx = self._open()
            lib = _LIB.load()
            target = device if device is not None else self.keyboard_device()["device"]
            state = (KEY_UP if up else KEY_DOWN) | (KEY_E0 if e0 else 0)
            stroke = KeyStroke(code=scan, state=state, information=0)
            # KeyStroke 8 字节，但 API 要求 20 字节 stroke —— 官方样例传
            # InterceptionStroke（20B char 数组）装它。这里照做：先填
            # MouseStroke 缓冲的前 8 字节。
            buf = MouseStroke()
            ctypes.memmove(ctypes.byref(buf), ctypes.byref(stroke),
                           ctypes.sizeof(stroke))
            n = lib.interception_send(ctx, target, ctypes.byref(buf), 1)
            if n != 1:
                raise InterceptionError(
                    f"Interception 驱动没有接收按键（send 返回 {n}，设备 {target}）")


_SHARED = Interception()


def shared():
    """进程级共享 context。"""
    return _SHARED


_KERNEL_DRIVERS = ("keyboard.sys", "mouse.sys")
_KERNEL_SERVICES = ("keyboard", "mouse")
_SERVICE_RUNNING = 4


def _service_state(name):
    """查服务状态 → (state|None, err)。4 = RUNNING；None = 查不到/查询失败。"""
    if os.name != "nt":
        return None, 0
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenSCManagerW.restype = ctypes.c_void_p
    adv.OpenSCManagerW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD]
    adv.OpenServiceW.restype = ctypes.c_void_p
    adv.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD]
    adv.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    adv.QueryServiceStatus.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    class _SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD),
                    ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD),
                    ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD),
                    ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    scm = adv.OpenSCManagerW(None, None, 0x0001)          # SC_MANAGER_CONNECT
    if not scm:
        return None, ctypes.get_last_error()
    try:
        svc = adv.OpenServiceW(scm, name, 0x0004)          # SERVICE_QUERY_STATUS
        if not svc:
            return None, ctypes.get_last_error()           # 1060 = 服务不存在
        try:
            st = _SERVICE_STATUS()
            if not adv.QueryServiceStatus(svc, ctypes.byref(st)):
                return None, ctypes.get_last_error()
            return int(st.dwCurrentState), 0
        finally:
            adv.CloseServiceHandle(svc)
    finally:
        adv.CloseServiceHandle(scm)


def _device_openable(path=r"\\.\interception00"):
    """能不能打开驱动创建的设备对象 → (bool, err)。

    设备由驱动本身创建：驱动没装载时必然打不开，这比 create_context 返回值
    （驱动缺席时也能返回非 NULL，见文件头 2026-11-07 备注）可靠得多。
    """
    if os.name != "nt":
        return False, 0
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.CreateFileW(path, 0, 0x3, None, 3, 0, None)   # 0 访问 / 共享读写 / OPEN_EXISTING
    if not h or h == ctypes.c_void_p(-1).value:
        return False, ctypes.get_last_error()
    k32.CloseHandle(h)
    return True, 0


def kernel_state():
    """内核驱动是否真的装好并在跑（硬判据，不依赖 dll 能不能建 context）。

    Interception 的驱动是 **keyboard.sys + mouse.sys 两个上滤驱动**，服务名
    **keyboard / mouse**；官方 v1.0.1 发行包里没有独立的 .sys/.inf/.cat，
    驱动以内嵌资源塞在 install-interception.exe 里。所以「无 interception.sys、
    sc query interception 报 1060」是装好机器的正常表现，不能拿它当判据。
    """
    out = {"ok": False, "reason_code": DRIVER_SERVICE_MISSING, "detail": "",
           "files": {}, "services": {}, "device": None}
    if os.name != "nt":
        out["detail"] = "非 Windows 平台，无内核驱动"
        return out
    drv_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "drivers")
    out["files"] = {n: os.path.isfile(os.path.join(drv_dir, n))
                    for n in _KERNEL_DRIVERS}
    missing = [n for n, ok in out["files"].items() if not ok]
    if missing:
        out["detail"] = ("内核驱动未安装：System32\\drivers 下缺 " + "、".join(missing))
        return out

    out["services"] = {n: _service_state(n) for n in _KERNEL_SERVICES}
    absent = [n for n, (st, _e) in out["services"].items() if st is None]
    not_run = [n for n, (st, _e) in out["services"].items()
               if st is not None and st != _SERVICE_RUNNING]
    if absent:
        out["reason_code"] = DRIVER_SERVICE_MISSING
        out["detail"] = ("内核驱动文件在，但服务不存在：" + "、".join(absent) +
                         "（驱动可能只落了 .sys、没注册上滤服务）")
        return out
    if not_run:
        out["reason_code"] = DRIVER_NOT_RUNNING
        out["detail"] = ("内核驱动服务未运行：" + "、".join(not_run) +
                         "（多为尚未重启或服务被停）")
        return out

    ok, err = _device_openable()
    out["device"] = {"path": r"\\.\interception00", "openable": ok, "err": err}
    if not ok:
        out["reason_code"] = DRIVER_NOT_RUNNING
        out["detail"] = (f"服务已运行，但驱动设备 \\\\.\\interception00 打不开"
                         f"（错误码 {err}）")
        return out
    out["ok"] = True
    out["reason_code"] = ""
    return out


def driver_status(probe=True, verify_filters=None):
    """驱动 + 真实设备的完整状态（带稳定原因码）。

    返回 {"ok", "reason_code", "detail", "identity", "filters"}。
    与 driver_ready 的区别：这里区分「驱动没装」和「驱动在但枚举不到
    真实鼠标」，前者降级到 legacy/message，后者同样降级但原因不同。

    ``filters`` 默认是「期望值 + 未验证」的记录，**不再真去回读**：
    `interception_get_filter` 是唯一会让解释器 finalize 在 native 层
    无界等待的调用（本机实测 atexit→进程退出 57s+，而且进程卡在驱动
    里的线程 taskkill /F 也收不掉）。`_open()` 已经显式置 0，逐槽回读
    只是「驱动是否照做」的诊断，代价却是每个调 /api/preflight 的进程
    都留一份不可回收的 native 状态。
    ``verify_filters=True`` 或环境变量 SJZ_FILTER_READBACK=1 时才回读，
    并且回读放在一次性子进程里（见 filter_audit_isolated）。
    """
    status = {"ok": False, "reason_code": DRIVER_UNAVAILABLE, "detail": "",
              "identity": None,
              "filters": {"expected": FILTER_EXPECTED_ZERO, "verified": False,
                          "values": None}}
    # 硬判据先跑：文件 + 服务 RUNNING + 设备可打开。create_context 会在驱动
    # 缺席时也返回非 NULL（文件头 2026-11-07 实测），不能单靠它判「就绪」。
    ks = kernel_state()
    status["kernel"] = ks
    if not ks["ok"]:
        status["reason_code"] = ks["reason_code"]
        status["detail"] = ks["detail"]
        return status
    try:
        _SHARED._open()
    except InterceptionError as e:
        status["detail"] = str(e)
        return status
    try:
        status["identity"] = _SHARED.identity()
        status["filters"] = _filter_state(verify_filters)
    except RealDeviceUnavailable as e:
        status["reason_code"] = e.reason_code
        status["detail"] = str(e)
        return status
    except InterceptionError as e:
        status["detail"] = str(e)
        return status
    except Exception as e:  # 枚举/读硬件 ID 的意外失败
        status["reason_code"] = DRIVER_UNAVAILABLE
        status["detail"] = f"设备枚举失败：{e}"
        return status
    # identity() 内部把 RealDeviceUnavailable 收成了
    # {"error", "reason_code"}，上面的 except 永远等不到它。必须在 probe 之前
    # 把鼠标侧的真实原因码抬上来：「设备不存在」和「发送被拒」是两回事，
    # 否则会被下面的 send_mouse 误记成 DRIVER_SEND_FAILED。
    mouse = (status["identity"] or {}).get("mouse") or {}
    if mouse.get("reason_code"):
        status["reason_code"] = mouse["reason_code"]
        status["detail"] = mouse.get("error") or "驱动在，但没有可注入的真实鼠标"
        return status
    if probe:
        try:
            _SHARED.send_mouse(0, x=0, y=0, flags=MOUSE_MOVE_RELATIVE)
        except InterceptionError as e:
            status["reason_code"] = DRIVER_SEND_FAILED
            status["detail"] = str(e)
            return status
    status["ok"] = True
    status["reason_code"] = ""
    return status


FILTER_EXPECTED_ZERO = "all-zero"
# 显式调试开关：置 1 才在一次性子进程里回读 filter（默认关闭）。
FILTER_READBACK_ENV = "SJZ_FILTER_READBACK"
FILTER_SLOTS = tuple(range(1, MAX_KEYBOARD + 1)) + \
    tuple(range(MAX_KEYBOARD + 1, MAX_KEYBOARD + MAX_MOUSE + 1))

# 子进程脚本：只做「建 context → 置 0 → 逐槽回读」，打印一行 JSON 后
# os._exit(0)。绝对不能走正常退出路径 —— get_filter 会让 finalize 卡死，
# 卡死必须发生在可丢弃的进程里。
_FILTER_CHILD_CODE = r"""
import ctypes, json, os, sys
path = sys.argv[1]
try:
    lib = ctypes.WinDLL(path)
    lib.interception_create_context.restype = ctypes.c_void_p
    lib.interception_create_context.argtypes = []
    lib.interception_set_filter.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_ushort]
    lib.interception_get_filter.restype = ctypes.c_uint
    lib.interception_get_filter.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.interception_destroy_context.argtypes = [ctypes.c_void_p]
    lib.interception_is_mouse.restype = ctypes.c_int
    lib.interception_is_keyboard.restype = ctypes.c_int
    ctx = lib.interception_create_context()
    if not ctx:
        raise RuntimeError("create_context 返回 NULL")
    lib.interception_set_filter(
        ctx, ctypes.cast(lib.interception_is_mouse, ctypes.c_void_p), 0)
    lib.interception_set_filter(
        ctx, ctypes.cast(lib.interception_is_keyboard, ctypes.c_void_p), 0)
    values = {}
    for dev in list(range(1, 11)) + list(range(11, 21)):
        values[dev] = int(lib.interception_get_filter(ctx, dev))
    lib.interception_destroy_context(ctx)
    print(json.dumps({"ok": True, "values": values}))
except Exception as e:
    print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}))
sys.stdout.flush()
os._exit(0)
"""


def _hidden_popen_kwargs():
    """子进程一律隐藏：不产生可见窗口、不抢焦点。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": si,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, **_hidden_popen_kwargs())
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _filter_verify_enabled():
    v = os.environ.get(FILTER_READBACK_ENV, "")
    return v.strip().lower() in ("1", "true", "yes", "on")


def _filter_state(verify=None):
    """filter 审计状态。默认只有期望值，不真去读（见 driver_status）。"""
    state = {"expected": FILTER_EXPECTED_ZERO, "verified": False, "values": None}
    want = _filter_verify_enabled() if verify is None else bool(verify)
    if not want:
        return state
    audit = filter_audit_isolated()
    state["verified"] = bool(audit.get("ok"))
    state["values"] = audit.get("values")
    state["ms"] = audit.get("ms")
    values = audit.get("values") or {}
    state["all_zero"] = bool(audit.get("ok") and values
                             and all(int(v) == 0 for v in values.values()))
    if not audit.get("ok"):
        state["error"] = audit.get("error")
    return state


def filter_audit_isolated(timeout=5.0):
    """在一次性子进程里回读所有设备槽的 filter（默认没人调）。

    返回 {"ok", "values": {设备号: int}, "error", "ms"}。
    为什么不用本进程读：get_filter 会在 native 层卡死解释器 finalize，
    这种卡死连 taskkill /F 都收不掉。放到子进程里，最坏情况只是丢掉一个
    短命进程；父进程用文件接输出 + 硬超时，不会跟着卡。
    """
    lib_path = getattr(_LIB, "path", None)
    if not lib_path:
        lib_path = next((p for p in _dll_candidates() if os.path.isfile(p)), None)
    if not lib_path:
        return {"ok": False, "values": None, "error": _LIB.error() or "DLL 未找到",
                "ms": 0.0}
    if getattr(sys, "frozen", False) or not sys.executable:
        return {"ok": False, "values": None,
                "error": "打包环境没有独立解释器，无法起一次性回读子进程",
                "ms": 0.0}
    import tempfile
    fd, out_path = tempfile.mkstemp(prefix="sjz-filter-audit-", suffix=".json")
    os.close(fd)
    t0 = time.monotonic()
    try:
        with open(out_path, "wb") as fout:
            proc = subprocess.Popen(
                [sys.executable, "-c", _FILTER_CHILD_CODE, lib_path],
                stdout=fout, stderr=subprocess.DEVNULL, **_hidden_popen_kwargs())
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc.pid)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                alive = proc.poll() is None
                return {"ok": False, "values": None,
                        "error": (f"回读子进程超过 {timeout:.0f}s 未返回，已丢弃"
                                  f"（PID {proc.pid}"
                                  + ("；get_filter 卡住的线程连 TerminateProcess "
                                     "都挡得住，这个进程可能收不回来"
                                     if alive else "") + "）"),
                        "pid": proc.pid, "unreclaimed": bool(alive),
                        "ms": round((time.monotonic() - t0) * 1000.0, 2)}
        with open(out_path, encoding="utf-8", errors="replace") as f:
            raw = f.read().strip()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    rec = None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                rec = json.loads(line)
                break
            except ValueError:
                continue
    if not isinstance(rec, dict):
        return {"ok": False, "values": None,
                "error": f"子进程没有返回可解析结果（exit={proc.returncode}）",
                "ms": round((time.monotonic() - t0) * 1000.0, 2)}
    rec["ms"] = round((time.monotonic() - t0) * 1000.0, 2)
    return rec


def filter_audit():
    """**调试专用**：在本进程里逐槽回读 filter，返回 {设备号: int|None}。

    正常路径不许调它 —— get_filter 会让解释器 finalize 在 native 层卡死，
    进程随即变成 taskkill /F 也收不掉的僵尸。要回读就调
    `filter_audit_isolated()`（子进程、可丢弃、默认关闭）。
    """
    lib = _LIB.load()
    ctx = _SHARED._open()
    out = {}
    for device in FILTER_SLOTS:
        try:
            out[device] = int(lib.interception_get_filter(ctx, device))
        except Exception:
            out[device] = None
    return out


def driver_ready(probe=True):
    """驱动是否已装并可用（兼容旧接口，保留二元组返回）。

    probe=True 连发一个**零位移相对事件**（x=0,y=0，不动光标、不按键）
    确认 `interception_send` 真受理：create_context 在驱动没装时照样返回
    非 NULL，只判 context 会得到「假就绪」。
    """
    status = driver_status(probe=probe)
    return bool(status["ok"]), status["detail"] if not status["ok"] else ""


# ---------------- 落地观测：等 down/up 真正进栈再还原光标 ----------------
#
# 驱动 send 是**异步**的：interception_send 返回时（≈0.017ms）事件还没进栈，
# 本机实测要过 0.1~0.5ms 才落到 Raw Input。所以「send 完立刻 SetCursorPos
# 还原」会输给还在路上的注入，光标被拽回目标点、用户看到鼠标被抢。
#
# 解法：注册一个**只旁观**的 Raw Input 窗口（RIDEV_INPUTSINK，隐藏窗口、
# 不钩、不截获、不抢焦点），等本设备的 down 和 up 两条 WM_INPUT 都到了
# 再还原。整个过程 3ms 上限，超时就按「未验证」记录，绝不假装成功。
#
# 观测窗口不会吞事件：RIDEV_INPUTSINK 只是复制一份给本进程，事件照常
# 送到前台程序（游戏）；filter 仍是 0，物理输入全程透传。

class _RAWMOUSE(ctypes.Structure):
    class _Buttons(ctypes.Union):
        _fields_ = [("usButtonFlags", wintypes.USHORT),
                    ("usButtonData", wintypes.USHORT),
                    ("ulButtons", wintypes.DWORD)]

    _anonymous_ = ("buttons",)
    _fields_ = [("usFlags", wintypes.USHORT), ("buttons", _Buttons),
                ("ulRawButtons", wintypes.DWORD),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG),
                ("ulExtraInformation", wintypes.DWORD)]


# Raw Input 鼠标按钮位（windowsx.h / RAWINPUT）
RI_MOUSE_LEFT_BUTTON_DOWN = 0x0001
RI_MOUSE_LEFT_BUTTON_UP = 0x0002
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_HWHEEL = 0x0800


def _qpc():
    value = ctypes.c_longlong()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(value))
    return value.value


_QPC_FREQ = ctypes.c_longlong()
ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(_QPC_FREQ))


def _qpc_ms(ticks):
    return ticks * 1000.0 / (_QPC_FREQ.value or 1)


class DeliveryWatch:
    """旁观本进程注入事件的 Raw Input 观测器（单例可用 delivery_watch()）。"""

    def __init__(self):
        self._hwnd = None
        self._thread = None
        self._lock = threading.Lock()
        # 落地事件用 Condition 广播：主线程 wait 时释放 GIL，事件一到立刻唤醒。
        # 原来的 sleep(0) 自旋会被 CPython 的 GIL 切换间隔（默认 5ms）拖出
        # 毫秒级尾巴，占用窗口的 p95 就是这么被抬上去的。
        self._cv = threading.Condition(self._lock)
        self._events = []
        self._names = {}
        self._ready = threading.Event()
        self._error = None
        self._proc_ref = None
        self._class_name = f"TraeDeliveryWatch{os.getpid()}"
        self._registered = False
        self._reset_ticks = _qpc()

    # -- 生命周期 --
    def start(self, timeout=1.5):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="delivery-watch",
                                            daemon=True)
            self._thread.start()
        ok = self._ready.wait(timeout)
        return bool(ok and self._hwnd)

    def stop(self):
        hwnd = self._hwnd
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0012, 0, 0)  # WM_QUIT
        thread = self._thread
        if thread is not None:
            thread.join(timeout=0.5)
        self._thread = None
        self._hwnd = None

    def status(self):
        return {"ready": bool(self.start()), "error": self._error,
                "hwnd": int(self._hwnd or 0), "events": len(self._events)}

    # -- 观测 --
    def reset(self):
        with self._cv:
            self._events.clear()
        self._reset_ticks = _qpc()

    def events(self):
        with self._lock:
            return list(self._events)

    def wait_buttons(self, vid=None, pid=None, timeout_ms=3.0,
                     want_down=1, want_up=1, since=None, want_moves=0):
        """等 down/up 落地。返回 {"ok", "down", "up", "t_down", "t_up",
        "elapsed_ms", "hdev"}；超时 ok=False 且带已看到的事件。

        want_moves>0 时额外要求看到本设备的 move 事件（回原点 stroke 落地）。

        三个 want_* 都为 0 是合法输入：表示「不需要任何事件」，立刻返回已满足。
        以前 `downs[0]` 会在空序列上抛 IndexError，接口复用迟早踩到。
        """
        start = since or self._reset_ticks
        if want_down <= 0 and want_up <= 0 and want_moves <= 0:
            return {"ok": True, "down": 0, "up": 0, "moves": 0,
                    "t_down": None, "t_up": None, "t_move": None,
                    "elapsed_ms": 0.0, "move_elapsed_ms": None,
                    "hdev": None, "device_name": ""}
        deadline = time.perf_counter() + max(0.0, timeout_ms) / 1000.0
        with self._cv:
            while True:
                # 已持有锁，直接读 _events；不能再调 self.events()（同一把锁）
                evs = [e for e in self._events
                       if e["t"] >= start and e["kind"] in ("down", "up")]
                downs = [e for e in evs
                         if e["kind"] == "down" and self._match(e, vid, pid)]
                ups = [e for e in evs
                       if e["kind"] == "up" and self._match(e, vid, pid)]
                moves = [e for e in self._events
                         if e["t"] >= start and e["kind"] == "move"
                         and self._match(e, vid, pid)]
                if (len(downs) >= want_down and len(ups) >= want_up
                        and len(moves) >= want_moves):
                    # downs/ups 可能为空（want_down=0 或 want_up=0 的合法用法），
                    # 一律按「有就取、没有就 None」取值，不再盲取 [0]。
                    _end = (ups[0]["t"] if ups
                            else (downs[0]["t"] if downs else start))
                    return {"ok": True,
                            "down": len(downs), "up": len(ups),
                            "moves": len(moves),
                            "t_down": downs[0]["t"] if downs else None,
                            "t_up": ups[0]["t"] if ups else None,
                            "t_move": moves[-1]["t"] if moves else None,
                            "elapsed_ms": _qpc_ms(_end - start),
                            "move_elapsed_ms": (_qpc_ms(moves[-1]["t"] - start)
                                                if moves else None),
                            "hdev": downs[0]["hdev"] if downs else None,
                            "device_name": (downs[0].get("name", "")
                                            if downs else "")}
                if time.perf_counter() >= deadline:
                    return {"ok": False, "down": len(downs), "up": len(ups),
                            "moves": len(moves),
                            "t_down": downs[0]["t"] if downs else None,
                            "t_up": ups[0]["t"] if ups else None,
                            "elapsed_ms": _qpc_ms(_qpc() - start),
                            "hdev": downs[0]["hdev"] if downs else None}
                remain = deadline - time.perf_counter()
                # 短等待兜底：即使 notify 丢了也能在下个 1ms 边界重查
                self._cv.wait(min(max(remain, 0.0), 0.001))

    @staticmethod
    def _match(event, vid, pid):
        if not vid or not pid:
            return True
        name = (event.get("name") or "").upper()
        return f"VID_{str(vid).upper()}" in name and f"PID_{str(pid).upper()}" in name

    # -- 线程体 --
    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                                   wintypes.UINT]
        user32.GetRawInputData.restype = wintypes.UINT
        user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                           ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.UINT),
                                           wintypes.UINT]
        user32.GetRawInputDeviceList.restype = wintypes.UINT
        user32.GetRawInputDeviceList.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
        user32.GetRawInputDeviceInfoW.argtypes = [
            wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
            ctypes.POINTER(wintypes.UINT)]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                     wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM)

        def wndproc(hwnd, msg, wp, lp):
            if msg == 0x00FF:  # WM_INPUT
                size = wintypes.UINT(0)
                user32.GetRawInputData(lp, 0x10000003, None,
                                       ctypes.byref(size),
                                       ctypes.sizeof(_RAWINPUTHEADER))
                buf = ctypes.create_string_buffer(max(1, size.value))
                n = user32.GetRawInputData(lp, 0x10000003, buf,
                                           ctypes.byref(size),
                                           ctypes.sizeof(_RAWINPUTHEADER))
                if n:
                    self._on_raw(buf, n)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        try:
            self._proc_ref = WNDPROC(wndproc)
            WNDCLASS = type("WNDCLASS", (ctypes.Structure,), {"_fields_": [
                ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]})
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASS(0, self._proc_ref, 0, 0, hinst, None, None, None, None,
                          self._class_name)
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(0, self._class_name, "delivery-watch",
                                          0, 0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                self._error = "创建隐藏观测窗口失败"
                self._ready.set()
                return
            self._hwnd = hwnd
            rid = _RAWINPUTDEVICE(1, 2, 0x00000100, hwnd)
            self._registered = bool(user32.RegisterRawInputDevices(
                ctypes.byref(rid), 1, ctypes.sizeof(_RAWINPUTDEVICE)))
            self._names = _raw_device_names(user32)
        except Exception as e:  # 观测失败不影响注入本身
            self._error = f"观测窗口初始化失败：{e}"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _on_raw(self, buf, n):
        hdr = ctypes.cast(buf, ctypes.POINTER(_RAWINPUTHEADER)).contents
        if hdr.dwType != 0:  # RIM_TYPEMOUSE
            return
        if n < ctypes.sizeof(_RAWINPUTHEADER) + ctypes.sizeof(_RAWMOUSE):
            return
        mouse = ctypes.cast(
            ctypes.addressof(buf) + ctypes.sizeof(_RAWINPUTHEADER),
            ctypes.POINTER(_RAWMOUSE)).contents
        flags = int(mouse.usButtonFlags)
        kind = None
        if flags & RI_MOUSE_LEFT_BUTTON_DOWN:
            kind = "down"
        elif flags & RI_MOUSE_LEFT_BUTTON_UP:
            kind = "up"
        elif flags & (RI_MOUSE_WHEEL | RI_MOUSE_HWHEEL):
            kind = "wheel"
        else:
            kind = "move"
        hdev = int(hdr.hDevice or 0)
        event = {"t": _qpc(), "kind": kind, "hdev": hdev,
                 "flags": flags, "dx": int(mouse.lLastX),
                 "dy": int(mouse.lLastY),
                 "us_flags": int(mouse.usFlags),
                 "name": self._names.get(hdev, "")}
        with self._cv:
            self._events.append(event)
            if len(self._events) > 64:
                del self._events[:len(self._events) - 64]
            self._cv.notify_all()


_WATCH = DeliveryWatch()


def delivery_watch():
    """进程级共享落地观测器（懒启动；失败返回 None 由调用方决定降级）。"""
    if _WATCH.start():
        return _WATCH
    return None
