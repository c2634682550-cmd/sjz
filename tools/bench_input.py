"""输入注入延迟 / 物理鼠标干扰 基准（分支 A）。

默认 = 安全模式：**不创建可见窗口、不移动真实光标、不抢前台**。
  - PostMessageW：投给 message-only 窗口（HWND_MESSAGE=-3），永不渲染、
    不进任务栏、不抢焦点；消息直投，桌面零痕迹。
  - SendInput / Interception：只发「零位移相对移动」(dx=dy=0)，
    没有按下/抬起，光标不动，只测调用往返耗时。

会动光标 / 建可见窗口的完整测试必须显式开闸：
    python tools/bench_input.py --i-understand-this-moves-the-cursor

两路独立测量：
  M1 调用侧：注入函数返回 - 调用发出（syscall / 驱动往返）
  M2 观测侧：**另一个进程**的窗口 WndProc 收到 WM_LBUTTONDOWN - 调用发出
             （QPC 全系统同一基准，跨进程无需校准）

用法:
    python tools/bench_input.py                 # 安全模式
    python tools/bench_input.py --quick
    python tools/bench_input.py --observe --message-only   # 内部用
"""
import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.DefWindowProcW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
_user32.CreateWindowExW.restype = w.HWND
_user32.RegisterRawInputDevices.restype = w.BOOL
_user32.RegisterRawInputDevices.argtypes = [ctypes.c_void_p, w.UINT, w.UINT]

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_INPUT = 0x00FF
WM_CLOSE, WM_DESTROY = 0x0010, 0x0002
MK_LBUTTON = 0x0001
HWND_MESSAGE = -3

_qpc_freq = ctypes.c_longlong()
_kernel32.QueryPerformanceFrequency(ctypes.byref(_qpc_freq))


def qpc():
    v = ctypes.c_longlong()
    _kernel32.QueryPerformanceCounter(ctypes.byref(v))
    return v.value


def ms(ticks):
    return ticks * 1000.0 / _qpc_freq.value


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, ctypes.c_uint,
                             w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT),
                ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


def _observe(message_only):
    """子进程：建窗口，把每个鼠标消息的到达时刻打到 stdout。"""
    sys.stdout.reconfigure(encoding="utf-8")
    lock = threading.Lock()

    def emit(s):
        with lock:
            sys.stdout.write(s + "\n")
            sys.stdout.flush()

    def proc(hwnd, msg, wp, lp):
        if msg in (WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP):
            emit("E %d %d" % (msg, qpc()))
            return 0
        if msg == WM_INPUT:
            emit("R %d" % qpc())
            return 0
        if msg == WM_CLOSE:
            _user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wp, lp)

    proc_ref = WNDPROC(proc)
    hinst = _kernel32.GetModuleHandleW(None)
    cls = "BenchInputObserver"
    wc = WNDCLASSW(style=0x0000, lpfnWndProc=proc_ref, cbClsExtra=0,
                   cbWndExtra=0, hInstance=hinst, hIcon=None, hCursor=None,
                   hbrBackground=None, lpszMenuName=None, lpszClassName=cls)
    _user32.RegisterClassW(ctypes.byref(wc))

    if message_only:
        # HWND_MESSAGE 必须按指针传 -3，直接传 int 会被截断成 0（实测失败）
        hwnd = _user32.CreateWindowExW(0, cls, "bench-observer", 0x80000000,
                                       0, 0, 0, 0, ctypes.c_void_p(-3), None,
                                       hinst, None)
        emit("RAWREG 0")
    else:
        hwnd = _user32.CreateWindowExW(0, cls, "bench-observer",
                                       0x80000000 | 0x10000000,
                                       240, 160, 640, 480, None, None,
                                       hinst, None)
        rid = RAWINPUTDEVICE(1, 2, 0x00000100, hwnd)
        ok = _user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                             ctypes.sizeof(RAWINPUTDEVICE))
        emit("RAWREG %d" % (1 if ok else 0))
    assert hwnd, "CreateWindowExW 失败"
    emit("HWND %d" % hwnd)

    msg = w.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))
    return 0


class Observer:
    def __init__(self, message_only=True):
        cmd = [sys.executable, os.path.abspath(__file__), "--observe"]
        if message_only:
            cmd.append("--message-only")
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True,
                                  bufsize=1, encoding="utf-8")
        self.q = queue.Queue()
        self.hwnd = None
        self.raw_reg = False
        self.raw_total = 0
        threading.Thread(target=self._read, daemon=True).start()
        for _ in range(400):
            try:
                item = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            self._dispatch(item)
            if self.hwnd:
                break

    def _read(self):
        for line in self.p.stdout:
            self.q.put(line.strip())

    def _dispatch(self, line):
        if line.startswith("HWND "):
            self.hwnd = int(line.split()[1])
        elif line.startswith("RAWREG "):
            self.raw_reg = line.split()[1] == "1"

    def drain(self, count_raw=False):
        while True:
            try:
                line = self.q.get_nowait()
            except queue.Empty:
                return
            if count_raw and line.startswith("R "):
                self.raw_total += 1

    def wait_event(self, msg_id, t0, timeout=0.25):
        deadline = time.perf_counter() + timeout
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                return None
            try:
                line = self.q.get(timeout=left)
            except queue.Empty:
                return None
            if line.startswith("E "):
                _, m, ts = line.split()
                if int(m) == msg_id and int(ts) >= t0:
                    return int(ts)
            elif line.startswith("R "):
                self.raw_total += 1

    def close(self):
        try:
            _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        try:
            self.p.terminate()
            self.p.wait(timeout=3)
        except Exception:
            pass


class _INPUT_MOUSE(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_ulonglong)]


class _INPUT_U(ctypes.Union):
    _fields_ = [("mi", _INPUT_MOUSE)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _INPUT_U)]


MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000


def _sendinput(flags, x=0, y=0):
    inp = _INPUT(type=0)
    inp.mi = _INPUT_MOUSE(x, y, 0, flags, 0, 0)
    return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def to_abs(px, py):
    vx = _user32.GetSystemMetrics(76)
    vy = _user32.GetSystemMetrics(77)
    vw = max(1, _user32.GetSystemMetrics(78) - 1)
    vh = max(1, _user32.GetSystemMetrics(79) - 1)
    return (int(round((px - vx) * 65535 / vw)),
            int(round((py - vy) * 65535 / vh)))


class _M(ctypes.Structure):
    _fields_ = [("state", ctypes.c_ushort), ("flags", ctypes.c_ushort),
                ("rolling", ctypes.c_short), ("x", ctypes.c_int),
                ("y", ctypes.c_int), ("information", ctypes.c_uint)]


class Injectors:
    def __init__(self):
        self.ic_lib = None
        self.ic_ctx = None
        self.ic_err = None
        self.ic_path = None
        self._load_ic()

    def _load_ic(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cand = [os.path.join(root, "core", "assets", "interception.dll"),
                os.path.join(root, "tools", "interception", "interception.dll")]
        path = next((p for p in cand if os.path.isfile(p)), None)
        if not path:
            self.ic_err = "找不到 interception.dll"
            return
        try:
            lib = ctypes.WinDLL(path)
        except OSError as e:
            self.ic_err = str(e)
            return
        lib.interception_create_context.restype = ctypes.c_void_p
        lib.interception_destroy_context.argtypes = [ctypes.c_void_p]
        lib.interception_send.restype = ctypes.c_uint
        lib.interception_send.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_void_p, ctypes.c_uint]
        lib.interception_get_filter.restype = ctypes.c_uint
        lib.interception_get_filter.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.interception_set_filter.restype = None
        lib.interception_set_filter.argtypes = [ctypes.c_void_p,
                                                ctypes.c_void_p, ctypes.c_ushort]
        self.ic_lib = lib
        ctx = lib.interception_create_context()
        if not ctx:
            self.ic_err = "create_context 返回 NULL（驱动没装）"
            return
        lib.interception_set_filter(
            ctx, ctypes.cast(lib.interception_is_mouse, ctypes.c_void_p), 0)
        lib.interception_set_filter(
            ctx, ctypes.cast(lib.interception_is_keyboard, ctypes.c_void_p), 0)
        self.ic_ctx = ctx
        self.ic_path = path

    def ic_filter_state(self):
        if not self.ic_ctx:
            return {}
        return {d: self.ic_lib.interception_get_filter(self.ic_ctx, d)
                for d in range(1, 21)}

    def ic_close(self):
        if self.ic_ctx:
            self.ic_lib.interception_destroy_context(self.ic_ctx)
            self.ic_ctx = None

    # 零干扰探针：0 位移相对移动，光标不动、无按键

    def sendinput_nop(self):
        t0 = qpc()
        ok = _sendinput(MOUSEEVENTF_MOVE, 0, 0)
        return t0, qpc(), bool(ok)

    def ic_nop(self):
        t0 = qpc()
        ok = self.ic_lib.interception_send(
            self.ic_ctx, 11, ctypes.byref(_M(0, 0x000, 0, 0, 0, 0)), 1) == 1
        return t0, qpc(), ok

    # 会动光标的完整点击（安全模式不调用）

    def sendinput_click(self, sx, sy):
        ax, ay = to_abs(sx, sy)
        a = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE
        t0 = qpc()
        ok = _sendinput(a, ax, ay)
        ok &= _sendinput(a | MOUSEEVENTF_LEFTDOWN, ax, ay)
        ok &= _sendinput(a | MOUSEEVENTF_LEFTUP, ax, ay)
        return t0, qpc(), bool(ok)

    def post_click(self, hwnd, cx, cy):
        lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        t0 = qpc()
        ok = _user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
        ok &= _user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        ok &= _user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
        return t0, qpc(), bool(ok)

    def ic_click(self, sx, sy):
        ax, ay = to_abs(sx, sy)
        fl = 0x001 | 0x002
        t0 = qpc()
        ok = True
        for st in (0, 0x001, 0x002):
            if self.ic_lib.interception_send(
                    self.ic_ctx, 11, ctypes.byref(_M(st, fl, 0, ax, ay, 0)),
                    1) != 1:
                ok = False
                break
        return t0, qpc(), ok


def stats(samples):
    s = sorted(samples)
    n = len(s)
    return {"n": n, "mean": statistics.fmean(s),
            "p50": s[int(0.50 * (n - 1))],
            "p95": s[int(0.95 * (n - 1))],
            "max": s[-1], "min": s[0]}


def _cursor():
    p = w.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _poll_cursor(target, want_equal, budget_s=0.005):
    deadline = time.perf_counter() + budget_s
    while True:
        pos = _cursor()
        if (pos == target) if want_equal else (pos != target):
            return qpc(), pos
        if time.perf_counter() >= deadline:
            return None, pos


def run_clicks(fn, restore_after, batches, per_batch, obs, settle=0.006):
    """完整点击（会动光标）。返回 (m1, m2, m3落地, 干扰样本)。"""
    m1s, m2s, m3s, inter = [], [], [], []
    for _ in range(batches):
        for _ in range(per_batch):
            p0 = _cursor()
            obs.drain()
            obs.raw_total = 0
            t0, t1, ok = fn()
            t_land, p_now = _poll_cursor(p0, want_equal=False)
            moved = p_now != p0
            if restore_after:
                _user32.SetCursorPos(p0[0], p0[1])
                t2 = qpc()
                t_back, _ = _poll_cursor(p0, want_equal=True)
                t3 = t_back or qpc()
            else:
                t2 = t3 = qpc()
            got = obs.wait_event(WM_LBUTTONDOWN, t0)
            if got is None:
                ok = False
            time.sleep(0.003)
            obs.drain(count_raw=True)
            m1s.append(t1 - t0)
            if got is not None:
                m2s.append(got - t0)
            if t_land is not None:
                m3s.append(t_land - t0)
            inter.append({"moved": moved, "away_ms": ms(t3 - t0),
                          "restore_ms": ms(t3 - t2), "raw": obs.raw_total,
                          "ok": ok})
            time.sleep(settle)
    return m1s, m2s, m3s, inter


def run_nop(fn, batches, per_batch, settle=0.004):
    """零干扰探针：只测 M1。"""
    m1s, oks = [], []
    for _ in range(batches):
        for _ in range(per_batch):
            t0, t1, ok = fn()
            m1s.append(t1 - t0)
            oks.append(ok)
            time.sleep(settle)
    return m1s, oks


def summarize(inter):
    return {"ok_rate": sum(1 for i in inter if i["ok"]) / len(inter),
            "moved_rate": sum(1 for i in inter if i["moved"]) / len(inter),
            "away_ms_mean": statistics.fmean([i["away_ms"] for i in inter]),
            "away_ms_max": max(i["away_ms"] for i in inter),
            "restore_ms_mean": statistics.fmean([i["restore_ms"] for i in inter]),
            "raw_events": sum(i["raw"] for i in inter)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--observe", action="store_true")
    ap.add_argument("--message-only", action="store_true")
    ap.add_argument("--i-understand-this-moves-the-cursor", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--batches", type=int, default=5)
    ap.add_argument("--per-batch", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    if a.observe:
        return _observe(a.message_only)

    if a.quick:
        a.batches, a.per_batch, a.warmup = 3, 60, 10

    invasive = a.i_understand_this_moves_the_cursor
    inj = Injectors()
    obs = Observer(message_only=not invasive)
    try:
        if not obs.hwnd:
            print("观测窗没起来", file=sys.stderr)
            return 2

        result = {"mode": "invasive" if invasive else "safe",
                  "config": {"batches": a.batches, "per_batch": a.per_batch,
                             "warmup": a.warmup,
                             "samples": a.batches * a.per_batch},
                  "observer": {"hwnd": obs.hwnd, "message_only": not invasive,
                               "raw_input": obs.raw_reg},
                  "interception": (inj.ic_path if inj.ic_ctx
                                   else "不可用 — %s" % inj.ic_err)}
        print("模式: %s | 观测窗 hwnd=%s message_only=%s"
              % (result["mode"], obs.hwnd, not invasive))
        print("Interception: %s" % result["interception"])

        cur_before = _cursor()
        result["cursor_before"] = cur_before

        rows = []

        # PostMessageW：两条模式下都跑；安全模式目标是 message-only 窗口
        if invasive:
            m1, m2, m3, it = run_clicks(
                lambda: inj.post_click(obs.hwnd, 200, 200), False,
                a.batches, a.per_batch, obs)
            rows.append({"method": "PostMessageW",
                         "m1_callside_ms": stats([ms(x) for x in m1]),
                         "m2_observer_ms": stats([ms(x) for x in m2]) if m2 else None,
                         "m3_cursor_land_ms": None,
                         "interference": summarize(it)})
        else:
            for _ in range(a.warmup):
                inj.post_click(obs.hwnd, 100, 100)
            m1, m2, m3, it = [], [], [], []
            for _ in range(a.batches):
                for _ in range(a.per_batch):
                    obs.drain()
                    t0, t1, ok = inj.post_click(obs.hwnd, 100, 100)
                    got = obs.wait_event(WM_LBUTTONDOWN, t0)
                    m1.append(t1 - t0)
                    if got is not None:
                        m2.append(got - t0)
                    it.append({"moved": False, "away_ms": 0.0,
                               "restore_ms": 0.0, "raw": 0,
                               "ok": got is not None and ok})
                    time.sleep(0.002)
            rows.append({"method": "PostMessageW",
                         "m1_callside_ms": stats([ms(x) for x in m1]),
                         "m2_observer_ms": stats([ms(x) for x in m2]) if m2 else None,
                         "m3_cursor_land_ms": None,
                         "interference": summarize(it)})

        # SendInput
        if invasive:
            for _ in range(a.warmup):
                inj.sendinput_click(440, 360)
                _user32.SetCursorPos(cur_before[0], cur_before[1])
            m1, m2, m3, it = run_clicks(lambda: inj.sendinput_click(440, 360),
                                        True, a.batches, a.per_batch, obs)
            rows.append({"method": "SendInput",
                         "m1_callside_ms": stats([ms(x) for x in m1]),
                         "m2_observer_ms": stats([ms(x) for x in m2]),
                         "m3_cursor_land_ms": stats([ms(x) for x in m3]) if m3 else None,
                         "interference": summarize(it)})
        else:
            for _ in range(a.warmup):
                inj.sendinput_nop()
            m1, oks = run_nop(inj.sendinput_nop, a.batches, a.per_batch)
            rows.append({"method": "SendInput(nop)",
                         "m1_callside_ms": stats([ms(x) for x in m1]),
                         "m2_observer_ms": None, "m3_cursor_land_ms": None,
                         "interference": {"ok_rate": sum(oks) / len(oks),
                                          "moved_rate": 0.0, "away_ms_mean": 0.0,
                                          "away_ms_max": 0.0,
                                          "restore_ms_mean": 0.0,
                                          "raw_events": 0}})

        # Interception
        if inj.ic_ctx:
            if invasive:
                for _ in range(a.warmup):
                    inj.ic_click(440, 360)
                    _user32.SetCursorPos(cur_before[0], cur_before[1])
                m1, m2, m3, it = run_clicks(lambda: inj.ic_click(440, 360),
                                            True, a.batches, a.per_batch, obs)
                rows.append({"method": "Interception",
                             "m1_callside_ms": stats([ms(x) for x in m1]),
                             "m2_observer_ms": stats([ms(x) for x in m2]),
                             "m3_cursor_land_ms": stats([ms(x) for x in m3]) if m3 else None,
                             "interference": summarize(it)})
            else:
                for _ in range(a.warmup):
                    inj.ic_nop()
                m1, oks = run_nop(inj.ic_nop, a.batches, a.per_batch)
                rows.append({"method": "Interception(nop)",
                             "m1_callside_ms": stats([ms(x) for x in m1]),
                             "m2_observer_ms": None, "m3_cursor_land_ms": None,
                             "interference": {"ok_rate": sum(oks) / len(oks),
                                              "moved_rate": 0.0, "away_ms_mean": 0.0,
                                              "away_ms_max": 0.0,
                                              "restore_ms_mean": 0.0,
                                              "raw_events": 0}})
                result["ic_filter_nonzero"] = {k: v for k, v in
                                               inj.ic_filter_state().items() if v}

        result["cursor_after"] = _cursor()
        result["cursor_unchanged"] = result["cursor_after"] == cur_before
        result["rows"] = rows

        text = json.dumps(result, ensure_ascii=False, indent=2)
        print(text)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(text)
        sys.stdout.flush()
        # 加载过 interception.dll 的进程在解释器退出阶段会卡住（实测两次）
        os._exit(0)
    finally:
        obs.close()
        inj.ic_close()


if __name__ == "__main__":
    sys.exit(main())
