"""真实设备注入对照实验 / 占用窗口 / 最短安全间隔 —— 2026-11-07

用途：回答「注入经过真实鼠标设备栈之后，在低级钩子和 Raw Input 两边
分别像什么」，以及「每次点击占用光标多久、各操作最短安全间隔」。

安全约束（本工具自带）：
  · 只点桌面（WindowFromPoint 判定为 Progman/WorkerW/SysListView32），
    不点任何真实窗口，不会误触按钮；
  · 每次点击后立刻还原光标，并把占用时长记进结果；
  · 隐藏窗口 + RIDEV_INPUTSINK 旁观，不弹可见窗口、不抢前台；
  · filter 全程 0（不截获物理输入）。

用法：
  python tools/bench_realmouse.py --mode devices
  python tools/bench_realmouse.py --mode detect  --n 30 --out bench_detect.json
  python tools/bench_realmouse.py --mode occupancy --n 60
  python tools/bench_realmouse.py --mode intervals
  python tools/bench_realmouse.py --mode all --out bench_real.json
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import interception as ic                      # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_INPUT = 0x00FF
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x01
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003

user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPCWSTR, ctypes.c_int]
user32.GetForegroundWindow.restype = wt.HWND
user32.RegisterRawInputDevices.restype = wt.BOOL
user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, wt.UINT, wt.UINT]
user32.GetRawInputData.restype = wt.UINT
user32.GetRawInputData.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p,
                                   ctypes.POINTER(wt.UINT), wt.UINT]
user32.GetRawInputDeviceList.restype = wt.UINT
user32.GetRawInputDeviceList.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(wt.UINT), wt.UINT]
user32.GetRawInputDeviceInfoW.restype = wt.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_void_p,
                                          ctypes.POINTER(wt.UINT)]
user32.SendInput.restype = wt.UINT
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
kernel32.GetModuleHandleW.restype = wt.HMODULE

_qpc_freq = ctypes.c_longlong()
kernel32.QueryPerformanceFrequency(ctypes.byref(_qpc_freq))


def qpc():
    v = ctypes.c_longlong()
    kernel32.QueryPerformanceCounter(ctypes.byref(v))
    return v.value


def ms(ticks):
    return ticks * 1000.0 / (_qpc_freq.value or 1)


def cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


class _LLHook(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]


class _RawHeader(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]


class _RawMouse(ctypes.Structure):
    class _Buttons(ctypes.Union):
        _fields_ = [("usButtonFlags", wt.USHORT), ("usButtonData", wt.USHORT),
                    ("ulButtons", wt.DWORD)]

    _anonymous_ = ("buttons",)
    _fields_ = [("usFlags", wt.USHORT), ("buttons", _Buttons),
                ("ulRawButtons", wt.DWORD), ("lLastX", wt.LONG),
                ("lLastY", wt.LONG), ("ulExtraInformation", wt.DWORD)]


class _RawInputDevice(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]


class _RawDeviceList(ctypes.Structure):
    _fields_ = [("hDevice", wt.HANDLE), ("dwType", wt.DWORD)]


class Observer:
    """三层观测：低级鼠标钩子(LLMHF_INJECTED) + Raw Input(hDevice + 按键位)
    + 光标轨迹采样。全部在一条自带消息泵的线程里，无可见窗口。"""

    def __init__(self):
        self.ll = []
        self.raw = []
        self.cursor = []
        self.names = {}
        self._lock = threading.Lock()
        self._stop = False
        self._ready = threading.Event()
        self._hwnd = None
        self._class = f"BenchObs{os.getpid()}"
        self._hook = None
        self._proc_ref = None
        self._hook_ref = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._sampler = threading.Thread(target=self._sample_cursor, daemon=True)

    # ---- 生命周期 ----
    def start(self, timeout=2.0):
        self._thread.start()
        self._sampler.start()
        return self._ready.wait(timeout) and self._hwnd is not None

    def stop(self):
        self._stop = True
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, 0x0012, 0, 0)
        self._thread.join(timeout=0.5)
        self._sampler.join(timeout=0.5)

    def clear(self):
        with self._lock:
            self.ll.clear()
            self.raw.clear()
            self.cursor.clear()

    def snapshot(self):
        with self._lock:
            return list(self.ll), list(self.raw), list(self.cursor)

    # ---- 线程 ----
    def _sample_cursor(self):
        last = cursor()
        while not self._stop:
            now = cursor()
            if now != last:
                with self._lock:
                    self.cursor.append({"t": qpc(), "x": now[0], "y": now[1]})
                last = now
            time.sleep(0)

    def _run(self):
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                                     wt.WPARAM, wt.LPARAM)
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                      wt.WPARAM, wt.LPARAM)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                             wt.HINSTANCE, wt.DWORD]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          wt.WPARAM, wt.LPARAM]

        def wndproc(hwnd, msg, wp, lp):
            if msg == WM_INPUT:
                size = wt.UINT(0)
                user32.GetRawInputData(lp, RID_INPUT, None, ctypes.byref(size),
                                       ctypes.sizeof(_RawHeader))
                buf = ctypes.create_string_buffer(max(1, size.value))
                n = user32.GetRawInputData(lp, RID_INPUT, buf,
                                           ctypes.byref(size),
                                           ctypes.sizeof(_RawHeader))
                if n:
                    self._on_raw(buf, n)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        def on_hook(code, wp, lp):
            if code == 0:
                info = ctypes.cast(lp, ctypes.POINTER(_LLHook)).contents
                with self._lock:
                    self.ll.append({"t": qpc(), "flags": int(info.flags),
                                    "injected": bool(info.flags & LLMHF_INJECTED),
                                    "x": info.pt.x, "y": info.pt.y})
            return user32.CallNextHookEx(None, code, wp, lp)

        self._proc_ref = WNDPROC(wndproc)
        self._hook_ref = HOOKPROC(on_hook)
        WNDCLASS = type("WNDCLASS", (ctypes.Structure,), {"_fields_": [
            ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]})
        hinst = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASS(
            0, self._proc_ref, 0, 0, hinst, None, None, None, None, self._class)))
        self._hwnd = user32.CreateWindowExW(
            0, self._class, "bench", 0, 0, 0, 0, 0, None, None, hinst, None)
        rid = _RawInputDevice(1, 2, RIDEV_INPUTSINK, self._hwnd)
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(_RawInputDevice))
        self.names = self._raw_names()
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_ref,
                                              hinst, 0)
        self._ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _raw_names(self):
        n = wt.UINT(0)
        user32.GetRawInputDeviceList(None, ctypes.byref(n),
                                     ctypes.sizeof(_RawDeviceList))
        if not n.value:
            return {}
        arr = (_RawDeviceList * n.value)()
        got = wt.UINT(n.value)
        user32.GetRawInputDeviceList(arr, ctypes.byref(got),
                                     ctypes.sizeof(_RawDeviceList))
        out = {}
        for i in range(got.value):
            h = arr[i].hDevice
            if not h:
                continue
            size = wt.UINT(0)
            user32.GetRawInputDeviceInfoW(h, 0x20000007, None,
                                          ctypes.byref(size))
            if not size.value:
                continue
            buf = ctypes.create_unicode_buffer(size.value + 1)
            user32.GetRawInputDeviceInfoW(h, 0x20000007, buf, ctypes.byref(size))
            out[int(h)] = buf.value
        return out

    def _on_raw(self, buf, n):
        hdr = ctypes.cast(buf, ctypes.POINTER(_RawHeader)).contents
        if hdr.dwType != 0:
            return
        if n < ctypes.sizeof(_RawHeader) + ctypes.sizeof(_RawMouse):
            return
        m = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(_RawHeader),
                        ctypes.POINTER(_RawMouse)).contents
        flags = int(m.usButtonFlags)
        if flags & 0x0001:
            kind = "down"
        elif flags & 0x0002:
            kind = "up"
        elif flags & 0x0400:
            kind = "wheel"
        elif flags & 0x0800:
            kind = "hwheel"
        else:
            kind = "move"
        hdev = int(hdr.hDevice or 0)
        with self._lock:
            self.raw.append({"t": qpc(), "kind": kind, "hdev": hdev,
                             "flags": flags, "dx": int(m.lLastX),
                             "dy": int(m.lLastY),
                             "name": self.names.get(hdev, ""),
                             "us_flags": int(m.usFlags)})


# ---------------------------------------------------------------- 安全落点
SAFE_CLASSES = ("Progman", "WorkerW", "SysListView32", "LockScreenBackGround")


def point_class(pt):
    hwnd = user32.WindowFromPoint(wt.POINT(*pt))
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_safe_point(screen=None):
    """找一个 WindowFromPoint 是桌面的点：点下去不会触到任何真实窗口。"""
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)
    step_x = max(40, vw // 12)
    step_y = max(40, vh // 8)
    for y in range(vy + 80, vy + vh - 80, step_y):
        for x in range(vx + 80, vx + vw - 80, step_x):
            cls = point_class((x, y))
            if cls in SAFE_CLASSES:
                return (x, y), cls
    return None


# ---------------------------------------------------------------- 注入器
def dev_meta(device):
    """按 device id 取枚举元信息（供 watch 用 vid/pid 过滤）。"""
    for d in ic.enumerate_devices("mouse"):
        if d.get("device") == device:
            return d
    return {}


def ic_click(device, target, watch=None, restore=True, hold_ms=0.0, verify=False):
    """Interception 注入一次完整点击（绝对移动→down→up），可选还原。

    verify=True 时用生产 DeliveryWatch 等 down/up 真落地再还原，返回
    (origin, t0, landing)。verify=False 只投递，调用方自己观测。
    """
    drv = ic.shared()
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = max(1, user32.GetSystemMetrics(78) - 1)
    vh = max(1, user32.GetSystemMetrics(79) - 1)

    def to_abs(x, y):
        return (int(round((x - vx) * 65535 / vw)),
                int(round((y - vy) * 65535 / vh)))

    ax, ay = to_abs(*target)
    flags = ic.MOUSE_MOVE_ABSOLUTE | ic.MOUSE_VIRTUAL_DESKTOP
    origin = cursor()
    landing = None
    w = ic.delivery_watch() if verify else None
    if w is not None:
        w.reset()
    t0 = qpc()
    drv.send_mouse(0, x=ax, y=ay, flags=flags, device=device)
    drv.send_mouse(ic.MOUSE_LEFT_DOWN, x=ax, y=ay, flags=flags, device=device)
    if hold_ms:
        time.sleep(hold_ms / 1000.0)
    drv.send_mouse(ic.MOUSE_LEFT_UP, x=ax, y=ay, flags=flags, device=device)
    if w is not None:
        meta = dev_meta(device)
        landing = w.wait_buttons(vid=meta.get("vid"), pid=meta.get("pid"),
                                 timeout_ms=3.0)
    if restore:
        user32.SetCursorPos(*origin)
    return origin, t0, landing


class _MI(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)]


class _MIU(ctypes.Union):
    _fields_ = [("mi", _MI)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _MIU)]


LSF_ABS = 0x8001
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def sendinput_move_click(target, restore=True):
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = max(1, user32.GetSystemMetrics(78) - 1)
    vh = max(1, user32.GetSystemMetrics(79) - 1)
    ax = int(round((target[0] - vx) * 65535 / vw))
    ay = int(round((target[1] - vy) * 65535 / vh))
    origin = cursor()
    t0 = qpc()
    move = _INPUT(type=0)
    move.mi = _MI(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)
    user32.SendInput(1, ctypes.byref(move), ctypes.sizeof(_INPUT))
    down = _INPUT(type=0)
    down.mi = _MI(ax, ay, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT))
    up = _INPUT(type=0)
    up.mi = _MI(ax, ay, 0, MOUSEEVENTF_LEFTUP, 0, 0)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT))
    if restore:
        user32.SetCursorPos(*origin)
    return origin, t0


# ---------------------------------------------------------------- 实验
def wait_landing(obs, t_mark, timeout_ms=6.0):
    deadline = qpc() + int(timeout_ms / 1000.0 * _qpc_freq.value)
    while qpc() < deadline:
        _, raw, _ = obs.snapshot()
        downs = [e for e in raw if e["t"] >= t_mark and e["kind"] == "down"]
        ups = [e for e in raw if e["t"] >= t_mark and e["kind"] == "up"]
        if downs and ups:
            return downs[0], ups[0]
        time.sleep(0)
    return None, None


def mode_devices(device=None):
    snap = ic.driver_status()
    out = {"driver": snap["ok"], "identity": snap["identity"],
           "filters": snap["filters"],
           "mice": ic.enumerate_devices("mouse", force=True),
           "keyboards": ic.enumerate_devices("keyboard", force=True)}
    if device:
        out["forced"] = device
    return out


def mode_detect(obs, device, n=30, target=None):
    found = target or find_safe_point()
    if not found:
        raise SystemExit("找不到桌面安全落点，停止（不在真实窗口上点）")
    point, cls = found
    rows = {}
    paths = [("SendInput", None), ("Interception-真实设备", device)]
    try:
        virtual = [d for d in ic.enumerate_devices("mouse", force=True)
                   if d["kind"] == "virtual"]
    except Exception:
        virtual = []
    if virtual:
        paths.append(("Interception-虚拟设备", virtual[0]["device"]))

    fg_before = user32.GetForegroundWindow()
    for label, dev in paths:
        fg0 = user32.GetForegroundWindow()
        stats = {"clicks": 0, "ll_events": 0, "ll_injected": 0,
                 "raw_events": 0, "raw_down": 0, "raw_up": 0,
                 "hdev": {}, "cursor_moved": 0, "occupancy": []}
        for _ in range(n):
            obs.clear()
            origin = cursor()
            t_mark = qpc()
            if dev is None:
                sendinput_move_click(point, restore=False)
            else:
                ic_click(dev, point, restore=False)
            down, up = wait_landing(obs, t_mark, 6.0)
            time.sleep(0.0015)
            ll, raw, cur = obs.snapshot()
            stats["clicks"] += 1
            stats["ll_events"] += len(ll)
            stats["ll_injected"] += sum(1 for e in ll if e["injected"])
            stats["raw_events"] += len(raw)
            stats["raw_down"] += sum(1 for e in raw if e["kind"] == "down")
            stats["raw_up"] += sum(1 for e in raw if e["kind"] == "up")
            for e in raw:
                key = e["name"] or f"hdev{e['hdev']}"
                stats["hdev"][key] = stats["hdev"].get(key, 0) + 1
            if any(c["x"] == point[0] and c["y"] == point[1] for c in cur):
                stats["cursor_moved"] += 1
            if up:
                stats["occupancy"].append(round(ms(up["t"] - t_mark), 3))
            if cursor() != origin:
                user32.SetCursorPos(*origin)
            time.sleep(0.008)
        occ = sorted(stats.pop("occupancy"))
        if occ:
            stats["land_p50_ms"] = round(statistics.median(occ), 3)
            stats["land_max_ms"] = round(occ[-1], 3)
        fg1 = user32.GetForegroundWindow()
        stats["fg_before"] = int(fg0 or 0)
        stats["fg_after"] = int(fg1 or 0)
        stats["fg_unchanged"] = int(fg0 or 0) == int(fg1 or 0)
        rows[label] = stats
    fg_after = user32.GetForegroundWindow()
    return {"target": point, "target_class": cls,
            "foreground_unchanged": int(fg_before or 0) == int(fg_after or 0),
            "foreground_before": int(fg_before or 0),
            "foreground_after": int(fg_after or 0),
            "rows": rows}


def mode_occupancy(device, n=60, target=None):
    from core.input import Mouse
    found = target or find_safe_point()
    if not found:
        raise SystemExit("找不到桌面安全落点，停止（不在真实窗口上点）")
    point, cls = found
    mouse = Mouse(mode="interception", verify=True)
    if device:
        # 显式绑定设备：直接写共享层，避免再跑一次活动度探测
        ic.shared()._mouse_dev = ic.select_mouse(prefer=device, probe_ms=0)  # noqa: SLF001
    samples = []
    restores = []
    for _ in range(n):
        mouse.click(point[0], point[1])
        last = mouse._last_click or {}          # noqa: SLF001
        samples.append(last.get("occupancy_ms"))
        restores.append(last.get("restored"))
        time.sleep(0.01)
    clean = [s for s in samples if isinstance(s, (int, float))]
    clean.sort()
    out = {"target": point, "target_class": cls, "n": len(clean),
           "all_restored": all(restores),
           "restore_fail_index": [i for i, r in enumerate(restores)
                                  if r is False],
           "restore_unknown": sum(1 for r in restores if r is None),
           "channel_status": mouse.channel_status(),
           "occupancy": mouse.occupancy_stats(),
           "samples": samples}
    if clean:
        out["p50"] = round(clean[int(0.50 * (len(clean) - 1))], 3)
        out["p95"] = round(clean[int(0.95 * (len(clean) - 1))], 3)
        out["max"] = round(clean[-1], 3)
    return out


def mode_intervals(obs, device, target=None, rounds=20):
    found = target or find_safe_point()
    if not found:
        raise SystemExit("找不到桌面安全落点，停止（不在真实窗口上点）")
    point, cls = found
    drv = ic.shared()
    vx = user32.GetSystemMetrics(76)
    vy = user32.GetSystemMetrics(77)
    vw = max(1, user32.GetSystemMetrics(78) - 1)
    vh = max(1, user32.GetSystemMetrics(79) - 1)
    ax = int(round((point[0] - vx) * 65535 / vw))
    ay = int(round((point[1] - vy) * 65535 / vh))
    flags = ic.MOUSE_MOVE_ABSOLUTE | ic.MOUSE_VIRTUAL_DESKTOP

    def to_abs(sx, sy):
        """屏幕坐标 → 驱动绝对坐标（0..65535 虚拟桌面）。"""
        return (int(round((sx - vx) * 65535 / vw)),
                int(round((sy - vy) * 65535 / vh)))

    def burst(gap_ms, count, kind="click"):
        origin = cursor()
        obs.clear()
        t_mark = qpc()
        for _ in range(count):
            if kind == "click":
                drv.send_mouse(0, x=ax, y=ay, flags=flags, device=device)
                drv.send_mouse(ic.MOUSE_LEFT_DOWN, x=ax, y=ay, flags=flags,
                               device=device)
                drv.send_mouse(ic.MOUSE_LEFT_UP, x=ax, y=ay, flags=flags,
                               device=device)
            elif kind == "wheel":
                drv.send_mouse(ic.MOUSE_WHEEL, rolling=120, device=device)
            if gap_ms > 0:
                time.sleep(gap_ms / 1000.0)
        time.sleep(0.004)
        _, raw, _ = obs.snapshot()
        user32.SetCursorPos(*origin)
        return raw

    def run_grid(kind, gaps, count):
        grid = {}
        for gap in gaps:
            ok = 0
            trials = 6
            for _ in range(trials):
                raw = burst(gap, count, kind)
                if kind == "click":
                    downs = sum(1 for e in raw if e["kind"] == "down")
                    ups = sum(1 for e in raw if e["kind"] == "up")
                    good = downs == count and ups == count
                else:
                    good = sum(1 for e in raw if e["kind"] == "wheel") == count
                ok += 1 if good else 0
            grid[gap] = ok / trials
        return grid

    def drag_burst(gap_ms, steps=8, dist=120):
        """down → N 次绝对移动 → up，每步之间 sleep gap_ms。"""
        origin = cursor()
        obs.clear()
        drv.send_mouse(0, x=ax, y=ay, flags=flags, device=device)
        drv.send_mouse(ic.MOUSE_LEFT_DOWN, x=ax, y=ay, flags=flags, device=device)
        time.sleep(gap_ms / 1000.0)
        gx, gy = ax, ay
        for i in range(1, steps + 1):
            px = point[0] + int(dist * i / steps)
            py = point[1]
            gx, gy = to_abs(px, py)
            drv.send_mouse(0, x=gx, y=gy, flags=flags, device=device)
            time.sleep(gap_ms / 1000.0)
        drv.send_mouse(ic.MOUSE_LEFT_UP, x=gx, y=gy, flags=flags, device=device)
        time.sleep(0.004)
        _, raw, _ = obs.snapshot()
        user32.SetCursorPos(*origin)
        return raw

    def run_drag_grid(gaps, steps=8):
        grid = {}
        for gap in gaps:
            ok = 0
            trials = 6
            for _ in range(trials):
                raw = drag_burst(gap, steps)
                downs = [i for i, e in enumerate(raw) if e["kind"] == "down"]
                ups = [i for i, e in enumerate(raw) if e["kind"] == "up"]
                moves = [e for e in raw if e["kind"] == "move"
                         and (e["dx"] or e["dy"])]
                good = (len(downs) == 1 and len(ups) == 1
                        and downs[0] < ups[0]
                        and len(moves) >= max(1, steps - 1))
                ok += 1 if good else 0
            grid[gap] = ok / trials
        return grid

    clicks = run_grid("click", [0, 0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0], 6)
    wheels = run_grid("wheel", [0, 0.1, 0.2, 0.4, 0.6, 1.0, 1.5], 6)
    drags = run_drag_grid([0, 0.1, 0.2, 0.4, 0.6, 1.0, 1.5])
    return {"target": point, "target_class": cls, "rounds": rounds,
            "click_success_by_gap": clicks,
            "wheel_success_by_gap": wheels,
            "drag_success_by_gap": drags,
            "legend": "key=相邻两次 send 的 sleep 间隔(ms), value=6 次试验里"
                      " Raw Input 层完全收齐（down/up/move 计数与顺序）的比例"}


def main():
    ap = argparse.ArgumentParser(description="真实鼠标设备注入实测")
    ap.add_argument("--mode", default="devices",
                    choices=["devices", "detect", "occupancy", "intervals", "all"])
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    dev = args.device or 0
    if dev:
        meta = dev_meta(dev)
        if not meta:
            raise SystemExit(f"device {dev} 不存在（先跑 --mode devices）")
        if meta.get("virtual"):
            print(f"[warn] device {dev} 是虚拟设备，仅用于对照，不做生产通道")
    result = {}
    if args.mode in ("devices", "all"):
        result["devices"] = mode_devices()
    if args.mode in ("detect", "all"):
        obs = Observer()
        if not obs.start():
            raise SystemExit("观测窗口创建失败（无法做对照实验）")
        try:
            result["detect"] = mode_detect(obs, dev, n=args.n)
        finally:
            obs.stop()
    if args.mode in ("occupancy", "all"):
        result["occupancy"] = mode_occupancy(dev, n=max(30, args.n))
    if args.mode in ("intervals", "all"):
        obs = Observer()
        if not obs.start():
            raise SystemExit("观测窗口创建失败")
        try:
            result["intervals"] = mode_intervals(obs, dev)
        finally:
            obs.stop()

    text = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    if args.out:
        path = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"written: {path}")
    print(text)
    sys.stdout.flush()
    os._exit(0)      # 驱动 context 已建，正常解释器退出可能被挂住


if __name__ == "__main__":
    main()
