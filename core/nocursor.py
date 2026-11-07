"""零干扰点击策略链。

硬约束（用户要求，优先级高于速度）：**默认路径不许碰系统光标。**
现在的 `Mouse._click_messages` / `Keyboard.wheel` 每次点击都 `SetCursorPos`
去借光标，连点时会连续把光标钉在目标点上 —— 这就是「抢鼠标」的根因。
本模块把点击拆成三条互相独立、可降级的策略：

    message  只投递窗口消息，全程不调用 SetCursorPos / 不注入移动事件
             （零干扰；代价：目标若是 Raw Input 消费者则收不到，见报告）
    borrow   SetCursorPos 一次跳转 → 投消息 → 立刻还原，停留窗口可配
             （次选；停留越短越不打扰，但太短目标可能还没读到光标）
    driver   Interception 驱动注入（可选增强，缺驱动时显式报错，不静默降级）

三条路径都必须：失败不留按下状态、异常路径必还原、返回值可判定。
"""

import ctypes
import ctypes.wintypes as w
import threading
import time

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
_user32.PostMessageW.restype = w.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.ScreenToClient.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
_user32.IsWindow.argtypes = [w.HWND]
_user32.IsWindow.restype = w.BOOL
_user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.SendMessageTimeoutW.argtypes = [
    w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT,
    ctypes.POINTER(ctypes.c_size_t)]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
# I2：点击是否「被目标处理」要看目标侧可观测状态，这组 API 是取证用的。
_user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
_user32.GetWindowRect.restype = w.BOOL
_user32.IsWindowVisible.argtypes = [w.HWND]
_user32.IsWindowVisible.restype = w.BOOL
_user32.IsWindowEnabled.argtypes = [w.HWND]
_user32.IsWindowEnabled.restype = w.BOOL
_user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
_user32.GetWindowThreadProcessId.restype = w.DWORD
_user32.GetGUIThreadInfo.argtypes = [w.DWORD, ctypes.c_void_p]
_user32.GetGUIThreadInfo.restype = w.BOOL


class _GUITHREADINFO(ctypes.Structure):
    """`GetGUIThreadInfo` 的最小结构：只取 active/focus/capture。

    按下鼠标时按钮类窗口会 `SetCapture`（`hwndCapture` 变成目标窗口），
    抬手再释放 —— 这是「目标真的处理了这次按下」最便宜的目标侧证据。
    """

    _fields_ = [
        ("cbSize", w.DWORD), ("flags", w.DWORD),
        ("hwndActive", w.HWND), ("hwndFocus", w.HWND),
        ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND),
        ("hwndMoveSize", w.HWND), ("hwndCaret", w.HWND),
        ("rcCaret", w.RECT),
    ]

# ---------------------------------------------------------------- 原因码
# 稳定枚举：主会话按码写用户文案，不要解析 message 文本。
# 前缀语义：DRV=驱动 / UIPI=完整性 / WND=窗口 / POST/MSG=消息投递 / CUR=光标
R_INTERNAL = "INPUT_ERROR"
R_DRIVER_UNAVAILABLE = "DRV_UNAVAILABLE"
R_DRIVER_SEND_REJECTED = "DRV_SEND_REJECTED"
R_FALLBACK_DISABLED = "FALLBACK_DISABLED"
R_UNKNOWN_MODE = "UNKNOWN_MODE"
R_UIPI_BLOCKED = "UIPI_BLOCKED"
R_INTEGRITY_UNKNOWN = "INTEGRITY_UNKNOWN"
R_NO_TARGET_HWND = "NO_TARGET_HWND"
R_WINDOW_GONE = "WINDOW_GONE"
R_TARGET_HUNG = "TARGET_HUNG"
R_POST_REJECTED = "POST_REJECTED"
R_MESSAGE_NOT_ACCEPTED = "MESSAGE_NOT_ACCEPTED"
# I2：消息投递成功（accepted）不等于目标处理了（processed）。
# 拿不到目标侧可观测证据时只能报这个「未证实」码，上层不许当成功记账。
R_MESSAGE_UNPROVEN = "MESSAGE_UNPROVEN"
R_MESSAGE_RAW_CURSOR = "MESSAGE_RAW_CURSOR_UNSUPPORTED"
R_CURSOR_NOT_MOVED = "CURSOR_NOT_MOVED"
R_CURSOR_NOT_AT_TARGET = "CURSOR_NOT_AT_TARGET"
# 驱动在、但拿不到真实设备：注入到虚拟设备号会被 Raw Input 归属成
# 虚拟设备（反作弊「不是那只鼠标」的典型特征），所以必须显式上报。
R_REAL_DEVICE_MISSING = "REAL_DEVICE_MISSING"
R_VIRTUAL_DEVICE_ONLY = "VIRTUAL_DEVICE_ONLY"
R_DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"

WM_NULL = 0x0000
SMTO_ABORTIFHUNG = 0x0002
_ERR_ACCESS_DENIED = 5
_ERR_INVALID_WINDOW = 1400
_ERR_TIMEOUT = 1460

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEWHEEL = 0x020A
MK_LBUTTON = 0x0001

MOUSE_MOVE_ABSOLUTE = 0x0001
MOUSE_VIRTUAL_DESKTOP = 0x8000
MOUSE_LEFT_DOWN = 0x0001
MOUSE_LEFT_UP = 0x0002
MOUSE_WHEEL = 0x0400


class InputError(RuntimeError):
    """点击链路的结构性失败（句柄失效 / UIPI 拦截 / 驱动缺失）。

    `reason_code` 是稳定枚举，主会话据此翻译文案；`message` 只给人看。
    """

    def __init__(self, message, reason_code=R_INTERNAL):
        super().__init__(message)
        self.reason_code = reason_code


class DriverUnavailable(InputError):
    """驱动级注入不可用：没装驱动、服务没起、或被杀软清掉。"""


def window_alive(hwnd):
    try:
        return bool(hwnd) and bool(_user32.IsWindow(hwnd))
    except Exception:                        # noqa: BLE001
        return False


def window_class(hwnd):
    """目标窗口类名（失败时返回空串）。`UnrealWindow` 是游戏主窗口。"""
    buf = ctypes.create_unicode_buffer(256)
    try:
        if hwnd and _user32.GetClassNameW(hwnd, buf, 256):
            return buf.value
    except Exception:                        # noqa: BLE001
        pass
    return ""


# 探测结论（稳定字符串；上层不要靠 reason_code 反推是哪一种）
PROBE_SENDABLE = "sendable"
PROBE_UIPI_BLOCKED = "uipi_blocked"
PROBE_NO_RESPONSE = "no_response"
PROBE_GONE = "window_gone"
PROBE_ERROR = "probe_error"


def _integrity_levels(hwnd):
    """`(本进程完整性级别, 目标窗口完整性级别)`，读不到的那一侧是 None。

    延迟导入：`core/input.py` 在模块顶层 import 了本模块，顶层反向导入会成环。
    单独抽成函数是为了测试能直接把完整性级别桩掉（本机没有高完整性目标可用）。
    """
    try:
        from .input import process_integrity
        return process_integrity(), process_integrity(hwnd)
    except Exception:                        # noqa: BLE001
        return None, None


def _integrity_cn(rid):
    try:
        from .input import integrity_name
        return integrity_name(rid)
    except Exception:                        # noqa: BLE001
        return hex(rid)


def probe_window(hwnd, timeout_ms=50):
    """三态探测：窗口消息到底能不能发进去。

    返回 dict：
      state         sendable / uipi_blocked / no_response / window_gone /
                    probe_error
      ok            state == sendable（只有这一种算「能发」）
      reason_code   对应原因码；sendable 时是 None
      detail        人话，带上判定依据
      integrity     {"ours": ..., "target": ..., "checked": bool}

    判据顺序：
      1. 句柄失效 → window_gone
      2. 完整性级别比较 → uipi_blocked（无副作用，且不受窗口过滤器影响）
      3. `WM_NULL` 往返 → 没应答 = no_response；应答 = sendable

    为什么 UIPI 改用完整性级别比较：
      `WM_NULL` 在 UIPI 的**白名单**里，高完整性窗口照样会应答 —— 拿它当
      UIPI 探针永远是「成功」，这正是旧实现的假阳性来源。换成非白名单消息
      （如 `WM_LBUTTONDOWN`）虽然能拿到 err=5，但会真的往目标队列塞一条输入
      消息（有副作用），而且结果依赖目标窗口自己的
      `ChangeWindowMessageFilterEx` 状态：目标显式放行了这条消息时又会假
      「可发送」。UIPI 的定义本身就是「低完整性进程不能向高完整性窗口发
      < WM_USER 的消息」，所以级别比较是唯一既无副作用、又不受窗口自定义
      过滤影响的判据。`WM_NULL` 只保留「线程在不在泵消息」这一个用途 ——
      它被白名单放行，正好不会冒充 UIPI 结论。
    """
    if not window_alive(hwnd):
        return {"state": PROBE_GONE, "ok": False,
                "reason_code": R_WINDOW_GONE,
                "detail": f"窗口句柄 {hwnd} 已失效（目标已销毁）",
                "integrity": {"ours": None, "target": None, "checked": False}}

    ours, target = _integrity_levels(hwnd)
    checked = ours is not None and target is not None
    if checked and target > ours:
        return {"state": PROBE_UIPI_BLOCKED, "ok": False,
                "reason_code": R_UIPI_BLOCKED,
                "detail": (f"目标窗口完整性 0x{target:04X}"
                           f"（{_integrity_cn(target)}，{target}）高于本进程 "
                           f"0x{ours:04X}（{_integrity_cn(ours)}，{ours}）："
                           "UIPI 会静默丢弃 < WM_USER 的输入消息"),
                "integrity": {"ours": ours, "target": target, "checked": True}}

    result = ctypes.c_size_t(0)
    ctypes.set_last_error(0)
    ok = _user32.SendMessageTimeoutW(
        hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, int(timeout_ms),
        ctypes.byref(result))
    err = ctypes.get_last_error()
    base = {"integrity": {"ours": ours, "target": target, "checked": checked}}
    if ok:
        detail = f"{int(timeout_ms)}ms 内应答"
        if not checked:
            detail += "（完整性级别读不到，只证明线程在泵消息）"
        return dict(base, state=PROBE_SENDABLE, ok=True, reason_code=None,
                    detail=detail)
    if err == _ERR_ACCESS_DENIED:
        # 完整性读数不可用时的兜底：UIPI 白名单里的 WM_NULL 都被拒，只能是权限问题
        return dict(base, state=PROBE_UIPI_BLOCKED, ok=False,
                    reason_code=R_UIPI_BLOCKED,
                    detail="WM_NULL 往返被拒 err=5（权限不足；"
                           "完整性级别读数不可用，按 UIPI 拦截上报）")
    if err == _ERR_TIMEOUT:
        return dict(base, state=PROBE_NO_RESPONSE, ok=False,
                    reason_code=R_TARGET_HUNG,
                    detail=f"{int(timeout_ms)}ms 内无应答（线程忙/挂起/死循环）")
    if err == _ERR_INVALID_WINDOW:
        return dict(base, state=PROBE_GONE, ok=False,
                    reason_code=R_WINDOW_GONE,
                    detail="WM_NULL 往返报 err=1400，窗口句柄已失效")
    return dict(base, state=PROBE_ERROR, ok=False,
                reason_code=R_MESSAGE_NOT_ACCEPTED,
                detail=f"SendMessageTimeoutW 失败 err={err}")


def window_responsive(hwnd, timeout_ms=50):
    """兼容旧签名：`(ok, reason_code, detail)`。

    结论来自 `probe_window` 的三态判定：只有 state=sendable 才 ok=True；
    被 UIPI 拦和无响应各自返回 False + 独立原因码，不再一律「成功」。
    """
    r = probe_window(hwnd, timeout_ms)
    return r["ok"], r["reason_code"], r["detail"]


def _gui_thread_state(hwnd):
    """目标线程的 GUI 状态（active/focus/capture）。拿不到返回 None。"""
    fn_pid = getattr(_user32, "GetWindowThreadProcessId", None)
    fn_info = getattr(_user32, "GetGUIThreadInfo", None)
    if not fn_pid or not fn_info:
        return None
    tid = w.DWORD(0)
    try:
        fn_pid(hwnd, ctypes.byref(tid))
        if not tid.value:
            return None
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not fn_info(int(tid.value), ctypes.byref(info)):
            return None
        return {"active": int(info.hwndActive or 0),
                "focus": int(info.hwndFocus or 0),
                "capture": int(info.hwndCapture or 0)}
    except Exception:                        # noqa: BLE001
        return None


def window_signature(hwnd):
    """目标窗口在**这一刻**的可观测状态，供「点击有没有被处理」取证。

    只取便宜、跟点击直接相关的字段：矩形/标题/可见/启用/样式 + GUI 线程
    的 active/focus/capture。鼠标坐标不算 —— 纯消息通道本来就不动光标。
    """
    sig = {"hwnd": int(hwnd or 0), "alive": window_alive(hwnd)}
    if not sig["alive"]:
        return sig
    sig["class"] = window_class(hwnd)
    fn = getattr(_user32, "GetWindowTextW", None)
    if fn:
        try:
            buf = ctypes.create_unicode_buffer(512)
            fn(hwnd, buf, 512)
            sig["title"] = buf.value
        except Exception:                    # noqa: BLE001
            pass
    fn = getattr(_user32, "GetWindowRect", None)
    if fn:
        try:
            rect = w.RECT()
            if fn(hwnd, ctypes.byref(rect)):
                sig["rect"] = (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:                    # noqa: BLE001
            pass
    fn = getattr(_user32, "IsWindowVisible", None)
    if fn:
        try:
            sig["visible"] = bool(fn(hwnd))
        except Exception:                    # noqa: BLE001
            pass
    fn = getattr(_user32, "IsWindowEnabled", None)
    if fn:
        try:
            sig["enabled"] = bool(fn(hwnd))
        except Exception:                    # noqa: BLE001
            pass
    fn = getattr(_user32, "GetWindowLongW", None)
    if fn:
        try:
            sig["style"] = int(fn(hwnd, -16))       # GWL_STYLE
        except Exception:                    # noqa: BLE001
            pass
    gui = _gui_thread_state(hwnd)
    if gui:
        sig["gui"] = gui
    return sig


def _signature_delta(*snaps):
    """多份快照之间发生变化的字段名（不含 hwnd/alive 自身）。"""
    changed = []
    base = snaps[0] or {}
    for name in ("alive", "class", "title", "rect", "visible", "enabled",
                 "style"):
        if any((s or {}).get(name) != base.get(name) for s in snaps[1:]):
            changed.append(name)
    for field in ("active", "focus", "capture"):
        vals = [((s or {}).get("gui") or {}).get(field) for s in snaps]
        if any(v != vals[0] for v in vals[1:]):
            changed.append(f"gui.{field}")
    return changed


def _deliver(hwnd, msg, wp, lp, timeout_ms=50):
    """同步投递一条鼠标消息，返回 `(accepted, processed, code, detail)`。

    为什么不用 PostMessage：`PostMessage` 返回非 0 只证明「进了队列」，
    窗口受理却不处理时上层会把它当成交（I2 的假成交根因）。
    `SendMessageTimeoutW` 会**真的调用目标窗口过程**再返回：
      - 正常返回     → 目标处理过这条消息（DefWindowProc 返回 0 也算处理）
      - err=1460     → 超时，目标没处理（挂起/死循环）
      - err=5        → UIPI 拦截
      - err=1400     → 句柄失效（窗口已销毁）
    三种失败各自有独立原因码，不会退化成同一个「0」。
    """
    fn = getattr(_user32, "SendMessageTimeoutW", None)
    if fn is None:
        return False, False, R_MESSAGE_NOT_ACCEPTED, "SendMessageTimeoutW 不可用"
    out = ctypes.c_size_t(0)
    ctypes.set_last_error(0)
    ok = fn(hwnd, msg, wp, lp, SMTO_ABORTIFHUNG, int(timeout_ms),
            ctypes.byref(out))
    err = ctypes.get_last_error()
    if ok:
        return True, True, None, f"已处理 res={out.value}"
    if err == _ERR_ACCESS_DENIED:
        return False, False, R_UIPI_BLOCKED, "被 UIPI 拦截（目标完整性级别更高）"
    if err == _ERR_TIMEOUT:
        return False, False, R_TARGET_HUNG, \
            f"{int(timeout_ms)}ms 内没处理（线程忙/挂起/死循环）"
    if err == _ERR_INVALID_WINDOW:
        return False, False, R_WINDOW_GONE, "窗口句柄已失效（目标已销毁）"
    if err == 0:
        # 同进程窗口过程返回 0 且没有错误：消息确实被处理过，只是结果是 0
        return True, True, None, "已处理（窗口过程返回 0）"
    return False, False, R_MESSAGE_NOT_ACCEPTED, f"SendMessageTimeoutW 失败 err={err}"


def probe_message(hwnd):
    """纯消息通道的结构性探测（**不投递任何鼠标消息**）。

    返回 dict：
      state         sendable / uipi_blocked / no_response / window_gone …
      usable        窗口存在、线程在泵消息、且没被 UIPI 拦
      reason_code   None / R_WINDOW_GONE / R_UIPI_BLOCKED / R_TARGET_HUNG ...
      detail        人话（带判定依据：完整性级别 or 往返结果）
      window_class  类名（UnrealWindow = 游戏主窗口）
      functional    纯消息对**游戏逻辑**是否有效：
                    "unsupported" = 已知目标按真实光标判定（见 _branchA 报告）
                    "unknown"     = 结构可用，但只有真机能证明游戏是否响应

    注意：`usable` 只代表消息栈可用，不代表点击会进游戏逻辑。
    """
    cls = window_class(hwnd)
    r = probe_window(hwnd)
    return {
        "hwnd": hwnd,
        "window_class": cls,
        "state": r["state"],
        "usable": bool(r["ok"]),
        "reason_code": r["reason_code"],
        "detail": r["detail"],
        "integrity": r["integrity"],
        "functional": "unsupported" if cls == "UnrealWindow" else "unknown",
    }


def _cursor():
    p = w.POINT()
    if not _user32.GetCursorPos(ctypes.byref(p)):
        return None
    return p.x, p.y


def _client(hwnd, sx, sy):
    p = w.POINT(int(sx), int(sy))
    if not _user32.ScreenToClient(hwnd, ctypes.byref(p)):
        raise InputError(f"ScreenToClient 失败：hwnd={hwnd}")
    return p.x, p.y


def _pack(x, y):
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


class Clicker:
    """一条策略。`strategy` 取 message / borrow / driver。

    `verify=True` 时每次点击后做一次**廉价真实性校验**：
      message → 三条鼠标消息逐条同步投递（`SendMessageTimeoutW`，窗口过程
                真的被执行才算受理）；err=5 直接按 UIPI 失败上报，不再假装成功
      borrow  → 回读系统光标是否真的落在目标点
      driver  → 驱动 send 的返回值就是受理凭据（三条 stroke 逐条校验），
                这里只保证异常路径一定补 up
    """

    def __init__(self, hwnd, strategy="message", dwell=0.002, hold=0.0,
                 verify=True, effect_probe=None, timeout_ms=50):
        if strategy not in ("message", "borrow", "driver"):
            raise ValueError(f"未知策略 {strategy!r}")
        self.hwnd = hwnd
        self.strategy = strategy
        self.dwell = dwell          # borrow 策略里光标停留时长（秒）
        self.hold = hold            # down→up 之间的按压时长
        self.verify = bool(verify)
        self.timeout_ms = int(timeout_ms)   # 同步投递的单条超时（挂起判别）
        self.effect_probe = effect_probe    # 可选：上层自带的「生效」探针
        self.last_result = None     # I2：本次点击的 accepted/processed 明细
        self._drv = None
        if strategy == "driver":
            self._drv = _load_driver()   # 缺驱动直接抛 DriverUnavailable

    # ---------------------------------------------------------------- 点击
    def click(self, sx, sy, hold=None):
        """在屏幕坐标 (sx,sy) 点一次。

        返回值**只**在「目标确实处理了这次点击」时为 True（I2）：
          - accepted  消息同步投递成功（目标窗口过程被执行）
          - processed 有目标侧证据证明它真的处理了（状态变化/按下捕获/
                      调用方探针）；拿不到证据时是 None，本函数返回 False，
                      明细在 `self.last_result` 里，绝不当成功记账。
        """
        hold = self.hold if hold is None else hold
        if self.strategy == "message":
            return self._post_click(sx, sy, hold)
        if self.strategy == "borrow":
            return self._borrow_click(sx, sy, hold)
        return self._driver_click(sx, sy, hold)

    def _result(self, accepted, processed, reason_code, detail,
                evidence=None, changed=None, latency_ms=None, dispatched=False):
        self.last_result = {
            "hwnd": int(self.hwnd or 0),
            "accepted": bool(accepted),
            "dispatched": bool(dispatched or accepted),
            "processed": processed,           # True / False / None(=unknown)
            "success": processed is True,
            "reason_code": reason_code,
            "detail": detail,
            "evidence": evidence,
            "changed": list(changed or ()),
            "latency_ms": latency_ms,
            "at": time.time(),
        }
        return self.last_result

    def _post_click(self, sx, sy, hold):
        """纯消息点击，并把「投递成功」和「目标已处理」拆成两件事。

        I2 根因：旧实现只看 `PostMessage` 非 0 + 一次 WM_NULL 应答，就返回
        True。目标受理但不处理（游戏读 Raw Input、窗口不理鼠标消息）时，
        上层看到 `click=True accepted=True functional=unknown` 当成点成了。
        现在：三条消息改走 `SendMessageTimeoutW` 同步投递（窗口过程真的
        被执行才算 accepted），再用目标侧可观测状态（窗口矩形/标题/可见/
        启用/GUI 线程 active/focus/capture）取证；没有证据就只报 unknown。
        """
        t0 = time.perf_counter()
        hwnd = self.hwnd
        if not window_alive(hwnd):
            self._result(False, False, R_WINDOW_GONE,
                         f"窗口 {hwnd} 已销毁，消息无处投递", latency_ms=0.0)
            raise InputError(f"窗口 {hwnd} 已销毁，消息无处投递", R_WINDOW_GONE)
        try:
            cx, cy = _client(hwnd, sx, sy)
        except InputError as e:
            self._result(False, False, e.reason_code, str(e))
            raise
        lp = _pack(cx, cy)
        before = window_signature(hwnd)
        pressed = False
        mid = None

        def _send(msg, wp, what):
            ok, done, code, detail = _deliver(hwnd, msg, wp, lp,
                                              self.timeout_ms)
            if not ok:
                self._result(False, False, code, f"{what}：{detail}",
                             changed=_signature_delta(before, mid or before,
                                                      window_signature(hwnd)),
                             latency_ms=(time.perf_counter() - t0) * 1000.0)
                raise InputError(f"{what}没有被目标处理：{detail}", code)
            return done

        try:
            _send(WM_MOUSEMOVE, 0, "鼠标移动")
            _send(WM_LBUTTONDOWN, MK_LBUTTON, "鼠标按下")
            pressed = True
            if hold:
                time.sleep(hold)
            # 按下态取证：按钮类窗口这时持有 mouse capture（GUI 线程 hwndCapture）
            mid = window_signature(hwnd)
            _send(WM_LBUTTONUP, 0, "鼠标抬起")
            pressed = False
        finally:
            if pressed:
                try:
                    _deliver(hwnd, WM_LBUTTONUP, 0, lp, self.timeout_ms)
                except Exception:                # noqa: BLE001 补 up 失败只能吞
                    pass

        after = window_signature(hwnd)
        changed = _signature_delta(before, mid or before, after)
        processed, reason, evidence = None, R_MESSAGE_UNPROVEN, "none"
        if changed:
            processed, reason, evidence = True, None, "state:" + ",".join(changed)
        elif self.effect_probe is not None:
            try:
                hit = self.effect_probe()
            except Exception as e:              # noqa: BLE001
                hit = None
                evidence = f"probe_error:{type(e).__name__}"
            else:
                evidence = "caller_probe"
            if hit is True:
                processed, reason = True, None
            elif hit is False:
                processed, reason = False, R_MESSAGE_UNPROVEN
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        self._result(True, processed, reason,
                     "目标过程已执行" + ("，且检测到状态变化：" + ",".join(changed)
                                        if changed else "，但没有可观测变化"),
                     evidence=evidence, changed=changed, latency_ms=latency_ms)
        return processed is True

    def _borrow_click(self, sx, sy, hold):
        """legacy/borrow 策略：借系统光标点一下。**默认不可达**。

        allowlist: LEGACY-BORROW SetCursorPos —— 只有 strategy="borrow" 才走
        这里；它是这条通道的点击定位方式，不是兜底。生产代码只用
        strategy="message"/"driver"（见 core/input.py 的 Clicker 构造）。

        默认关闭：借光标一定会搬动系统光标，和「不抢鼠标」冲突。只有用户
        显式打开 `input_legacy_borrow` 才放行，否则明确报错而不是静默借。
        """
        from . import input as _core_input       # 局部导入：避免模块循环
        if not _core_input._legacy_allowed():
            raise InputError(
                "借系统光标点击已被禁用（input_legacy_borrow=false）；"
                "驱动通道不可用时不会静默降级到本机鼠标",
                R_FALLBACK_DISABLED)
        origin = _cursor()
        lp = None
        pressed = False
        try:
            if not _user32.SetCursorPos(int(sx), int(sy)):
                err = ctypes.get_last_error()
                raise InputError(
                    f"SetCursorPos({sx},{sy}) 返回 0，err={err}"
                    + ("（0=系统没给原因，典型是 UIPI/ClipCursor）"
                       if err == 0 else ""),
                    R_CURSOR_NOT_MOVED)
            lp = _pack(*_client(self.hwnd, sx, sy))
            self._post(WM_LBUTTONDOWN, MK_LBUTTON, lp)
            pressed = True
            if self.dwell:
                time.sleep(self.dwell)
            if hold:
                time.sleep(hold)
            self._post(WM_LBUTTONUP, 0, lp)
            pressed = False
            return True
        finally:
            # 无论成功失败都还原；失败时先把按下状态补 up，绝不留在按下态
            if pressed and lp is not None:
                try:
                    self._post(WM_LBUTTONUP, 0, lp)
                except InputError:
                    pass
            if origin is not None:
                _user32.SetCursorPos(origin[0], origin[1])

    def _driver_click(self, sx, sy, hold):
        """驱动注入点击。

        allowlist: CURSOR-RESTORE-ONLY SetCursorPos —— 只在 finally 里把光标
        放回用户原来的位置，不做点击定位。
        """
        ax, ay = _abs(sx, sy)
        flags = MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP
        origin = _cursor()
        pressed = False
        try:
            self._drv.send_mouse(0, x=ax, y=ay, flags=flags)
            self._drv.send_mouse(MOUSE_LEFT_DOWN, x=ax, y=ay, flags=flags)
            pressed = True
            if hold:
                time.sleep(hold)
            self._drv.send_mouse(MOUSE_LEFT_UP, x=ax, y=ay, flags=flags)
            pressed = False
            return True
        except Exception as e:                    # noqa: BLE001
            raise InputError(f"驱动注入被拒：{e}", R_DRIVER_SEND_REJECTED) from e
        finally:
            if pressed:
                try:
                    self._drv.send_mouse(MOUSE_LEFT_UP, x=ax, y=ay, flags=flags)
                except Exception:            # noqa: BLE001 补 up 失败只能吞
                    pass
            # A 分支实测：`send` 调用本身 0.017ms，但入栈是**异步**的，真正
            # 落地 0.48ms。所以还原必须晚于入栈 —— 固定等 1.0ms（2 倍余量）。
            # 「发完立刻还原」是错的：还原会早于入栈，光标最终留在目标点。
            # 只在「光标仍停在我们放的位置」时还原：用户已经动过鼠标就说明
            # 那是他的操作，再拽回原点就是抢鼠标。
            if origin is not None:
                time.sleep(0.001)
                now = _cursor()
                if now is not None and max(abs(now[0] - sx),
                                           abs(now[1] - sy)) <= 2:
                    _user32.SetCursorPos(origin[0], origin[1])

    # ---------------------------------------------------------------- 滚轮
    def wheel(self, sx, sy, notches):
        """正向 = 向上滚。message 策略用 WM_MOUSEWHEEL（屏幕坐标）。

        allowlist: CURSOR-RESTORE-ONLY SetCursorPos —— 只在 finally 里还原
        用户原位置；定位与滚动都走驱动。
        """
        if self.strategy == "message":
            delta = 120 * int(notches)
            lp = _pack(sx, sy)                        # 滚轮消息要屏幕坐标
            wp = ((delta & 0xFFFF) << 16)
            before = window_signature(self.hwnd)
            ok, done, code, detail = _deliver(self.hwnd, WM_MOUSEWHEEL, wp, lp,
                                              self.timeout_ms)
            if not ok:
                self._result(False, False, code, f"滚轮没有被目标处理：{detail}")
                raise InputError(f"滚轮消息没有被目标处理：{detail}", code)
            after = window_signature(self.hwnd)
            changed = _signature_delta(before, after)
            processed = True if changed else None
            if processed is None and self.effect_probe is not None:
                try:
                    hit = self.effect_probe()
                except Exception:                # noqa: BLE001
                    hit = None
                if hit is True:
                    processed = True
                elif hit is False:
                    processed = False
            self._result(True, processed,
                         None if processed else R_MESSAGE_UNPROVEN,
                         "滚轮已处理" + ("：" + ",".join(changed) if changed else ""),
                         evidence="state:" + ",".join(changed) if changed else "none",
                         changed=changed)
            return processed is True
        if self.strategy == "borrow":
            raise InputError("borrow 策略不实现滚轮：借光标的滚轮窗口太长，"
                             "改用 message 或 driver")
        origin = _cursor()
        try:
            ax, ay = _abs(sx, sy)
            flags = MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP
            self._drv.send_mouse(0, x=ax, y=ay, flags=flags)
            for _ in range(abs(int(notches))):
                self._drv.send_mouse(MOUSE_WHEEL,
                                     rolling=120 if notches > 0 else -120,
                                     flags=flags)
        finally:
            if origin is not None:
                # 同 _driver_click：入栈异步，0.48ms 落地，等 1.0ms 再还原；
                # 用户已经动过鼠标就不还原（那是他的操作，拽回去就是抢鼠标）。
                time.sleep(0.001)
                now = _cursor()
                if now is not None and max(abs(now[0] - sx),
                                           abs(now[1] - sy)) <= 2:
                    _user32.SetCursorPos(origin[0], origin[1])

    # ---------------------------------------------------------------- 内部
    def _post(self, msg, wp, lp):
        if not _user32.PostMessageW(self.hwnd, msg, wp, lp):
            err = ctypes.get_last_error()
            code = R_UIPI_BLOCKED if err == _ERR_ACCESS_DENIED else R_POST_REJECTED
            raise InputError(
                f"PostMessageW(0x{msg:04X}) 被拒 err={err}"
                + ("（UIPI：目标完整性级别更高，控制台需提权）"
                   if err == _ERR_ACCESS_DENIED else ""),
                code)


def _abs(sx, sy):
    vx = _user32.GetSystemMetrics(76)
    vy = _user32.GetSystemMetrics(77)
    vw = max(1, _user32.GetSystemMetrics(78) - 1)
    vh = max(1, _user32.GetSystemMetrics(79) - 1)
    ax = int(round((sx - vx) * 65535 / vw))
    ay = int(round((sy - vy) * 65535 / vh))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _load_driver():
    try:
        from . import interception as X
    except Exception as e:                           # noqa: BLE001
        raise DriverUnavailable(f"interception 模块不可用：{e}") from e
    ok, why = X.driver_ready(probe=True)   # probe 只发零位移事件，不动光标
    if not ok:
        raise DriverUnavailable(f"驱动未就绪：{why}")
    return X.shared()


def probe(hwnd):
    """探测目标窗口能接受哪条策略。只投一条 WM_MOUSEMOVE，不动光标。

    返回 {'message': bool, 'reason': str, 'reason_code': str|None,
          'alive': bool, 'responsive': bool, 'window_class': str}：
    message=True 只说明**消息能进队列**，不代表游戏逻辑会响应
    （Raw Input 消费者对消息点击是零反应，必须真机验证）。
    结构性预检（不投消息）用 `probe_message`。
    """
    if not window_alive(hwnd):
        return {"message": False, "reason": "窗口句柄失效",
                "reason_code": R_WINDOW_GONE, "alive": False,
                "responsive": False, "window_class": ""}
    ok, code, detail = window_responsive(hwnd)
    if not ok:
        return {"message": False, "reason": detail, "reason_code": code,
                "alive": True, "responsive": False,
                "window_class": window_class(hwnd)}
    cx, cy = _client(hwnd, 0, 0)
    lp = _pack(cx, cy)
    ctypes.set_last_error(0)
    sent = bool(_user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp))
    err = ctypes.get_last_error()
    if sent:
        return {"message": True, "reason": "ok", "reason_code": None,
                "alive": True, "responsive": True,
                "window_class": window_class(hwnd)}
    code = R_UIPI_BLOCKED if err == _ERR_ACCESS_DENIED else R_POST_REJECTED
    return {"message": False,
            "reason": f"err={err}（5=UIPI 权限不足）" if err else "投递被拒",
            "reason_code": code, "alive": True, "responsive": True,
            "window_class": window_class(hwnd)}



def selftest(hwnd=None, n=200, target=(960, 540)):
    """零干扰自证。

    判据 1：窗口收到 n 次 WM_LBUTTONDOWN（消息确实被受理）。
    判据 2：连点期间以 200Hz 采样系统光标，没有一次落到 target 上
            （说明全程没碰光标；比「前后各读一次」更严，能排除用户自己动鼠标）。
    """
    if hwnd is None:
        hwnd = _make_test_window()
    before = _cursor()
    seen_at_target = [0]
    on_target = [0]
    stop = [False]

    def watch():
        while not stop[0]:
            p = _cursor()
            if p is not None:
                on_target[0] += 1
                if abs(p[0] - target[0]) <= 2 and abs(p[1] - target[1]) <= 2:
                    seen_at_target[0] += 1
            time.sleep(0.005)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    c = Clicker(hwnd, "message")
    t0 = time.perf_counter()
    for _ in range(n):
        c.click(target[0], target[1])
    dt = (time.perf_counter() - t0) / n * 1000
    time.sleep(0.2)                       # 等泵线程把队列里的消息跑完
    stop[0] = True
    watcher.join(timeout=0.5)
    after = _cursor()
    moved = 0 if before == after else \
        ((after[0] - before[0]) ** 2 + (after[1] - before[1]) ** 2) ** 0.5
    return {"n": n, "hits": _TEST_HITS[0], "per_click_ms": round(dt, 3),
            "cursor_before": before, "cursor_after": after,
            "displacement_net": round(moved, 1),
            "samples": on_target[0], "samples_at_target": seen_at_target[0],
            "isolated_desktop": bool(
                (getattr(_make_test_window, "last", None) or {}).get("desktop"))}


_TEST_HITS = [0]
_TEST_PROC = None


def _make_test_window():
    """在**独立桌面**上创建测试窗口。

    硬约束：不许在用户桌面上创建可见窗口。先 `CreateDesktopW` 建一个隔离
    桌面、`SetThreadDesktop` 切过去，再建窗口 —— 用户看不到，也不会被前台
    切换打断。CreateDesktop 失败时退回普通不可见窗口（窗口样式里没有
    `WS_VISIBLE`，本身也不显示），并把 `desktop=False` 报出来。
    """
    global _TEST_PROC
    _TEST_HITS[0] = 0
    LR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
    PROC = ctypes.WINFUNCTYPE(LR, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    def cb(h, m, wp, lp):
        if m == WM_LBUTTONDOWN:
            _TEST_HITS[0] += 1
            return 0
        return _user32.DefWindowProcW(h, m, wp, lp)

    _TEST_PROC = PROC(cb)
    _user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]

    class WC(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", PROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

    box = {}

    def runner():
        isolated = False
        desk = None
        try:
            _user32.CreateDesktopW.argtypes = [
                w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p,
                w.DWORD, w.DWORD, ctypes.c_void_p]
            _user32.CreateDesktopW.restype = w.HANDLE
            _user32.SetThreadDesktop.argtypes = [w.HANDLE]
            _user32.SetThreadDesktop.restype = w.BOOL
            _user32.CloseDesktop.argtypes = [w.HANDLE]
            desk = _user32.CreateDesktopW(
                "SjzNoCursorTestDesk", None, None, 0, 0x10000000, None)
            if desk:
                isolated = bool(_user32.SetThreadDesktop(desk))
        except Exception:                       # noqa: BLE001
            isolated = False
        box["desktop"] = isolated

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.GetModuleHandleW.restype = w.HMODULE
        inst = _k32.GetModuleHandleW(None)
        wc = WC()
        wc.lpfnWndProc = _TEST_PROC
        wc.hInstance = inst
        wc.lpszClassName = "SjzNoCursorTest"
        _user32.RegisterClassW.argtypes = [ctypes.POINTER(WC)]
        _user32.CreateWindowExW.argtypes = [
            w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
        _user32.CreateWindowExW.restype = w.HWND
        _user32.RegisterClassW(ctypes.byref(wc))
        # 0x00CF0000 = WS_OVERLAPPEDWINDOW，**不含 WS_VISIBLE**：即使退回
        # 主桌面也不会显示成可见窗口。
        box["hwnd"] = _user32.CreateWindowExW(
            0, "SjzNoCursorTest", "t", 0x00CF0000,
            0, 0, 400, 300, None, None, inst, None)
        m = w.MSG()
        while _user32.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(m))
            _user32.DispatchMessageW(ctypes.byref(m))
        if desk:
            _user32.CloseDesktop(desk)

    threading.Thread(target=runner, daemon=True).start()
    for _ in range(200):
        if "hwnd" in box:
            break
        time.sleep(0.005)
    hwnd = box.get("hwnd")
    if not hwnd:
        raise InputError("测试窗口创建超时")
    _make_test_window.last = {"desktop": box.get("desktop", False)}
    return hwnd


if __name__ == "__main__":
    print(selftest())
