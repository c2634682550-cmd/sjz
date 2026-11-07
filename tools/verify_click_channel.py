"""点击通道实测：连续注入 N 次点击，证明「不碰用户的鼠标」。

测三件事（全部实测，不读代码猜）：

1. 光标：每 5ms 采一次 GetCursorPos，给出完整坐标序列、变化次数、净位移。
2. 低层钩子：本进程装一个 WH_MOUSE_LL，分别统计「总事件」和「带
   LLMHF_INJECTED 的合成事件」。真实设备注入应当 total>0、injected=0。
3. 落地与系统输入时间戳：DeliveryWatch 收 Raw Input 的 down/up（命中率），
   GetLastInputInfo 看系统输入时间戳是否刷新。

安全边界：
- 点击目标是自己创建的 **0.1% 不透明度**分层窗口（WS_EX_LAYERED，
  alpha=1/255），放在工作区右下角 180×180，不抢焦点、不遮挡可见内容，
  跑完 DestroyWindow。整个过程 1~3 秒。
- 不发任何真实窗口的点击；窗口是 topmost，点击必然落在它身上。
- 不做裸循环：紧采样线程有硬截止时间；所有线程 join 时带超时。
- 驱动不可用时**一次都不点**，直接输出 driver_unavailable。

用法：
    python tools/verify_click_channel.py                 # 默认 50 次
    python tools/verify_click_channel.py --clicks 80
    python tools/verify_click_channel.py --out report.json
"""

import argparse
import ctypes
import datetime
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 显式签名：64 位下不声明 restype 会把 HWND/HOOK 截成 32 位
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = ctypes.c_bool
user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
user32.GetLastInputInfo.restype = ctypes.c_bool
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassExW.restype = ctypes.c_ushort
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
user32.PeekMessageW.restype = ctypes.c_bool
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                              ctypes.c_ubyte, wintypes.DWORD]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowRect.restype = ctypes.c_bool
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetTickCount.restype = wintypes.DWORD

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM,
                              wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HANDLE)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", wintypes.DWORD)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x00000001
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_NCHITTEST = 0x0084
WM_QUIT = 0x0012
HTCLIENT = 1
PM_REMOVE = 0x0001
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
LWA_ALPHA = 0x00000002
SW_SHOWNA = 8
SPI_GETWORKAREA = 0x0030


def dpi_aware():
    """按物理像素取坐标，避免 DPI 虚拟化让 GetCursorPos 与窗口坐标不一致。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Per-Monitor V2
    except Exception:                                    # noqa: BLE001
        try:
            user32.SetProcessDPIAware()
        except Exception:                                # noqa: BLE001
            pass


def cursor_pos():
    p = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(p)):
        return None
    return (p.x, p.y)


def last_input_tick():
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(li)):
        return None
    return int(li.dwTime)


class ProbeWindow:
    """0.1% 不透明度的分层窗口：点击落在它身上，用户看不见也点不到内容。"""

    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.down = 0
        self.up = 0
        self.hwnd = None
        self._proc = WNDPROC(self._on_message)
        self._cls = "dafors_click_probe_%d" % os.getpid()

    def create(self):
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._cls
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(),
                          "RegisterClassExW 失败")
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST,
            self._cls, "", WS_POPUP, self.x, self.y, self.w, self.h,
            None, None, wc.hInstance, None)
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW 失败")
        user32.SetLayeredWindowAttributes(hwnd, 0, 1, LWA_ALPHA)
        user32.ShowWindow(hwnd, SW_SHOWNA)
        user32.UpdateWindow(hwnd)
        self.hwnd = hwnd
        return hwnd

    def _on_message(self, hwnd, msg, wp, lp):
        if msg == WM_LBUTTONDOWN:
            self.down += 1
            return 0
        if msg == WM_LBUTTONUP:
            self.up += 1
            return 0
        if msg == WM_NCHITTEST:
            return HTCLIENT
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def pump(self, ms=0):
        """在创建窗口的线程上泵消息（点击消息只会投到这条线程）。"""
        msg = wintypes.MSG()
        end = time.perf_counter() + ms / 1000.0
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0,
                                      PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if time.perf_counter() >= end:
                return

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        user32.UnregisterClassW(self._cls, kernel32.GetModuleHandleW(None))


class CursorSampler(threading.Thread):
    """每 interval 秒采一次 GetCursorPos，记录完整序列。"""

    def __init__(self, interval_s):
        super().__init__(daemon=True)
        self.interval = interval_s
        self.stop_evt = threading.Event()
        self.ready = threading.Event()
        self.samples = []          # [[ms, x, y], ...]
        self.t0 = None
        self.thread_id = 0
        self.gap_max_us = 0.0

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        self.t0 = time.perf_counter()
        self.ready.set()
        p = wintypes.POINT()
        last = self.t0
        while not self.stop_evt.is_set():
            user32.GetCursorPos(ctypes.byref(p))
            now = time.perf_counter()
            gap = (now - last) * 1e6
            if gap > self.gap_max_us:
                self.gap_max_us = gap
            last = now
            self.samples.append([round((now - self.t0) * 1000.0, 3),
                                 p.x, p.y])
            time.sleep(self.interval)


class LowLevelMouseCounter(threading.Thread):
    """本进程的 WH_MOUSE_LL：统计总事件与带 LLMHF_INJECTED 的合成事件。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.total = 0
        self.left_down = 0
        self.left_up = 0
        self.injected = 0
        self.ready = threading.Event()
        self.installed = False
        self.error = None
        self.thread_id = 0
        self._proc = HOOKPROC(self._callback)

    def _callback(self, code, wp, lp):
        if code >= 0:
            try:
                info = ctypes.cast(lp, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                self.total += 1
                if info.flags & LLMHF_INJECTED:
                    self.injected += 1
                if wp == WM_LBUTTONDOWN:
                    self.left_down += 1
                elif wp == WM_LBUTTONUP:
                    self.left_up += 1
            except Exception:                            # noqa: BLE001
                pass
        return user32.CallNextHookEx(None, code, wp, lp)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        try:
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc,
                                                  None, 0)
            self.installed = bool(self._hook)
            if not self.installed:
                self.error = "SetWindowsHookExW 失败 err=%d" % \
                    ctypes.get_last_error()
        except Exception as e:                           # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.ready.set()
        if not self.installed:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


def tight_excursion_probe(mouse, target, budget_ms=400):
    """单次点击的极紧采样：量出光标真实驻留时长与最大偏移。

    5ms 采样器抓不到亚毫秒的瞬移，这里用 ~50µs 的紧循环（硬截止 400ms，
    不做裸循环）。只用来量化「录屏可见性」，不影响第 1 项的结论。
    """
    pos0 = cursor_pos()
    samples = []
    t0 = time.perf_counter()
    end = t0 + budget_ms / 1000.0
    clicked = False
    p = wintypes.POINT()
    while time.perf_counter() < end:
        if not clicked and (time.perf_counter() - t0) * 1000 > 15:
            mouse.click(target[0], target[1], verify=False,
                        expect_foreground=False, fire=False, hold=0.0)
            clicked = True
        user32.GetCursorPos(ctypes.byref(p))
        samples.append([round((time.perf_counter() - t0) * 1000.0, 4),
                        p.x, p.y])
        if clicked and (p.x, p.y) == pos0 and len(samples) > 40:
            # 已经回到原点：再多观察 20 个样本就收工
            break
    changed = [s for s in samples if (s[1], s[2]) != pos0]
    excursions = [max(abs(s[1] - pos0[0]), abs(s[2] - pos0[1])) for s in changed]
    occupancy_ms = None
    returned = False
    if changed:
        first = changed[0][0]
        back = None
        for s in samples:
            if s[0] > first and (s[1], s[2]) == pos0:
                back = s[0]
                returned = True
                break
        occupancy_ms = round((back - first) if back is not None else None, 3) \
            if back is not None else None
    return {
        "clip_budget_ms": budget_ms,
        "samples": len(samples),
        "changed_samples": len(changed),
        "max_excursion_px": max(excursions) if excursions else 0,
        "occupancy_ms": occupancy_ms,
        "returned_to_origin": returned,
        "not_returned": bool(changed) and not returned,
        "visible_at_60fps": (occupancy_ms >= 16.6
                             if occupancy_ms is not None else None),
        "visible_at_144fps": (occupancy_ms >= 6.9
                              if occupancy_ms is not None else None),
        "enumeration": "1 次点击；紧循环轮询（非 5ms 采样）",
        "origin": list(pos0) if pos0 else None,
    }


def wait_until_idle(max_wait_s, window_ms=300):
    """等用户鼠标真正停下来（默认最多等 20 秒）。

    机器是用户的：只要他还在动鼠标 / 按着键，注入就会和他的操作打架，
    测出来的光标序列也没法解释。宁可报告 user_mouse_busy 也不硬测。
    """
    deadline = time.perf_counter() + max_wait_s
    stable_since = None
    last = cursor_pos()
    while time.perf_counter() < deadline:
        time.sleep(0.03)
        if user32.GetAsyncKeyState(0x01) & 0x8000:      # VK_LBUTTON
            stable_since = None
            last = cursor_pos()
            continue
        cur = cursor_pos()
        if cur != last:
            stable_since = None
            last = cur
            continue
        if stable_since is None:
            stable_since = time.perf_counter()
        elif (time.perf_counter() - stable_since) * 1000 >= window_ms:
            tick = last_input_tick()
            idle_ms = None
            if tick is not None:
                idle_ms = (kernel32.GetTickCount() - tick) & 0xFFFFFFFF
                if idle_ms >= 0x80000000:
                    idle_ms = None
            # 判据只看「光标是否在动 + 左键是否按住」：键盘输入也会刷新
            # LastInputInfo，但它不会和我们的点击打架。idle_ms 只作参考。
            return True, idle_ms
    return False, None


def analyze_cursor(samples, interval_ms):
    if not samples:
        return {"samples": 0, "changed_samples": 0,
                "net_displacement_px": 0, "origin": None}
    origin = (samples[0][1], samples[0][2])
    changed = [s for s in samples if (s[1], s[2]) != origin]
    net = max((max(abs(s[1] - origin[0]), abs(s[2] - origin[1]))
               for s in samples), default=0)
    gaps = [samples[i][0] - samples[i - 1][0] for i in range(1, len(samples))]
    return {
        "interval_ms_target": interval_ms,
        "samples": len(samples),
        "interval_ms": {"min": round(min(gaps), 3) if gaps else 0,
                        "max": round(max(gaps), 3) if gaps else 0,
                        "avg": round(sum(gaps) / len(gaps), 3) if gaps else 0},
        "origin": list(origin),
        "final": [samples[-1][1], samples[-1][2]],
        "changed_samples": len(changed),
        "net_displacement_px": net,
        "first_change_ms": changed[0][0] if changed else None,
        "sequence": samples,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clicks", type=int, default=50)
    ap.add_argument("--interval-ms", type=float, default=5.0)
    ap.add_argument("--click-gap-ms", type=float, default=2.0,
                    help="两击之间的间隔；调大可以观察光标瞬态是否收敛")
    ap.add_argument("--out", default=os.path.join(ROOT, "tools",
                                                  "click_channel_report.json"))
    ap.add_argument("--no-tight-probe", action="store_true")
    ap.add_argument("--wait-idle", type=float, default=20.0,
                    help="最多等用户鼠标停下来多少秒；0 = 不等")
    args = ap.parse_args()

    dpi_aware()
    started = datetime.datetime.now().isoformat(timespec="seconds")
    out = {
        "generated_at": started,
        "pid": os.getpid(),
        "clicks_requested": args.clicks,
        "python": sys.version.split()[0],
    }

    try:
        from core.input import Mouse
        from core.interception import (MOUSE_MOVE_ABSOLUTE,
                                       MOUSE_VIRTUAL_DESKTOP, driver_status,
                                       shared)
    except Exception as e:                               # noqa: BLE001
        out["status"] = "import_failed"
        out["error"] = "%s: %s" % (type(e).__name__, e)
        _emit(out, args.out)
        return 2

    status = driver_status(probe=True)
    out["driver"] = {
        "ok": status.get("ok"),
        "reason_code": status.get("reason_code"),
        "detail": status.get("detail"),
        "identity": status.get("identity"),
    }
    if not status.get("ok"):
        out["status"] = "driver_unavailable"
        out["note"] = "驱动不通：按 input_fallback=fail 一次都没点（不借系统鼠标）"
        _emit(out, args.out)
        return 3

    drv = shared()
    ident = drv.mouse_device()
    out["device"] = {k: ident.get(k) for k in
                     ("device", "is_virtual", "vid", "pid", "path")
                     if k in ident} or ident

    try:
        mouse = Mouse(None)
    except Exception as e:                               # noqa: BLE001
        out["status"] = "channel_init_failed"
        out["error"] = "%s: %s" % (type(e).__name__, e)
        out["note"] = "按 input_fallback=fail，没有降级到系统鼠标，一次都没点"
        _emit(out, args.out)
        return 4

    out["channel"] = getattr(mouse, "mode", None) or getattr(mouse, "_mode", "?")
    out["fallback_policy"] = getattr(mouse, "_fallback_policy", None)
    if out["channel"] != "interception":
        out["status"] = "channel_not_interception"
        _emit(out, args.out)
        return 4

    # ---- 用户鼠标必须先停下来，否则注入和用户操作打架 ----
    idle_ok, idle_ms = (True, None)
    if args.wait_idle > 0:
        idle_ok, idle_ms = wait_until_idle(args.wait_idle)
    out["user_idle"] = {"ok": bool(idle_ok), "idle_ms": idle_ms,
                        "waited_s": args.wait_idle}
    if not idle_ok:
        out["status"] = "user_mouse_busy"
        out["note"] = ("用户一直在动鼠标/按住键；不去抢他的鼠标，一次都没点。"
                       "鼠标停稳 300ms 后重跑即可")
        _emit(out, args.out)
        return 6

    # ---- 目标窗口：工作区右下角 180×180，alpha=1/255 ----
    work = RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
    win = ProbeWindow(max(work.left, work.right - 204),
                      max(work.top, work.bottom - 204), 180, 180)
    try:
        win.create()
    except Exception as e:                               # noqa: BLE001
        out["status"] = "probe_window_failed"
        out["error"] = "%s: %s" % (type(e).__name__, e)
        _emit(out, args.out)
        return 5
    target = (win.x + 90, win.y + 90)
    gr = RECT()
    user32.GetWindowRect(win.hwnd, ctypes.byref(gr))
    out["target_window"] = {"rect": [win.x, win.y, win.w, win.h],
                            "window_rect": [gr.left, gr.top, gr.right, gr.bottom],
                            "visible": bool(user32.IsWindowVisible(win.hwnd)),
                            "alpha": "1/255", "topmost": True,
                            "target_point": list(target)}

    sampler = CursorSampler(args.interval_ms / 1000.0)
    hook = LowLevelMouseCounter()
    try:
        hook.start()
        hook.ready.wait(2.0)
        time.sleep(0.15)                 # 等钩子真正挂上
        out["low_level_hook_installed"] = hook.installed
        if not hook.installed:
            out["low_level_hook_error"] = hook.error

        origin = cursor_pos()
        out["cursor_origin_before"] = list(origin) if origin else None
        tick_before = last_input_tick()

        sampler.start()
        sampler.ready.wait(1.0)
        time.sleep(0.05)
        win.pump(0.02)

        # 预热 5 次：不计入统计，让驱动 FIFO 与窗口都进入稳定态
        gap = max(0.0, args.click_gap_ms / 1000.0)
        for _ in range(5):
            win.pump(0.001)
            mouse.click(target[0], target[1], verify=False,
                        expect_foreground=False, fire=False, hold=0.0)
            win.pump(gap)

        base_down = win.down
        t_batch = time.perf_counter()
        for _ in range(args.clicks):
            win.pump(0.001)
            mouse.click(target[0], target[1], verify=False,
                        expect_foreground=False, fire=False, hold=0.0)
            win.pump(gap)
        batch_ms = (time.perf_counter() - t_batch) * 1000.0
        win.pump(0.02)

        # 排队排空：设备 FIFO 每条 stroke 都要按报告率落地，批量发得快，
        # 光标可能在批次结束后还在走。这里 2ms 采 500ms，看它到底回不回去。
        trace = []
        t_end = time.perf_counter() + 0.5
        p = wintypes.POINT()
        while time.perf_counter() < t_end:
            user32.GetCursorPos(ctypes.byref(p))
            trace.append([round((time.perf_counter() - t_end) * 1000.0 + 500.0,
                                3), p.x, p.y])
            time.sleep(0.002)
        out["post_batch_trace"] = trace
        out["post_batch_unique_positions"] = len({(t[1], t[2]) for t in trace})

        sampler.stop_evt.set()
        sampler.join(1.0)
        out["batch_ms"] = round(batch_ms, 2)
        out["batch_per_click_ms"] = round(batch_ms / max(args.clicks, 1), 3)

        out["cursor"] = analyze_cursor(sampler.samples, args.interval_ms)
        out["cursor"]["sampler_gap_max_us"] = round(sampler.gap_max_us, 1)
        out["cursor"]["origin_before"] = out.get("cursor_origin_before")
        out["cursor"]["final_after"] = list(cursor_pos() or ())

        tick_after = last_input_tick()
        refreshed = (tick_before is not None and tick_after is not None
                     and tick_after != tick_before)
        delta_ms = None
        if refreshed:
            d = (tick_after - tick_before) & 0xFFFFFFFF
            if d < 0x80000000:
                delta_ms = d
            else:
                delta_ms = d - 0x100000000
        out["last_input"] = {"before": tick_before, "after": tick_after,
                             "refreshed": bool(refreshed),
                             "delta_ms": delta_ms}

        after_pos = cursor_pos()
        out["restore"] = {
            "origin": list(origin) if origin else None,
            "after": list(after_pos) if after_pos else None,
            "ok": bool(origin is not None and after_pos == origin),
            "delta_px": (max(abs(after_pos[0] - origin[0]),
                            abs(after_pos[1] - origin[1]))
                         if origin and after_pos else None),
        }

        hits = win.down - base_down
        out["hits"] = {
            "window_lbutton_down": hits,
            "window_lbutton_up": win.up,
            "clicks": args.clicks,
            "hit_rate": round(hits / args.clicks, 4),
        }
        out["low_level_hook"] = {
            "installed": hook.installed,
            "events_total": hook.total,
            "left_down": hook.left_down,
            "left_up": hook.left_up,
            "injected_events": hook.injected,
            "injected_ratio": round(hook.injected / max(hook.total, 1), 4),
        }
        out["delivery_watch"] = None
        try:
            from core.interception import delivery_watch
            dw = delivery_watch()
            out["delivery_watch"] = {
                "status": dw.status(),
                "landed_events": len(dw.events()),
            }
        except Exception as e:                           # noqa: BLE001
            out["delivery_watch_error"] = "%s: %s" % (type(e).__name__, e)

        # ---- 极紧采样：量化单次点击的光标驻留（录屏可见性）----
        if not args.no_tight_probe:
            try:
                out["tight_probe"] = tight_excursion_probe(mouse, target)
            except Exception as e:                       # noqa: BLE001
                out["tight_probe"] = {"error": "%s: %s" % (type(e).__name__, e)}

        out["status"] = "ok"
        return _emit(out, args.out)
    finally:
        try:
            hook.stop()
        except Exception:                                # noqa: BLE001
            pass
        try:
            win.destroy()
        except Exception:                                # noqa: BLE001
            pass


def _emit(payload, path):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:                               # noqa: BLE001
        sys.stderr.write("写 JSON 失败：%s: %s\n" % (type(e).__name__, e))
    print(text)
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()
        sys.exit(9)
