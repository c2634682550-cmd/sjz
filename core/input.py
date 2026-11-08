"""鼠标输入。

点击走 Interception 注入到**枚举出来的真实鼠标设备**（2026-10-30 起）：
系统看到的是真实硬件事件，不依赖前台，光标在注入窗口（<1ms）内被移到
目标点再按 FIFO 回原点。

系统鼠标兜底（SendInput / mouse_event / SetCursorPos 借光标）**默认关闭**：
驱动不可用时按 `input_fallback=fail` 显式报错，不静默退回系统鼠标。
legacy / message 两条通道只接受显式配置（`input_mode`），不会作为默认兜底。
"""
import ctypes
import logging
import threading
import time
from ctypes import wintypes

from .nocursor import (
    R_CURSOR_NOT_AT_TARGET,
    R_CURSOR_NOT_MOVED,
    R_DEVICE_NOT_FOUND,
    R_DRIVER_SEND_REJECTED,
    R_DRIVER_UNAVAILABLE,
    R_FALLBACK_DISABLED,
    R_INTEGRITY_UNKNOWN,
    R_MESSAGE_NOT_ACCEPTED,
    R_MESSAGE_RAW_CURSOR,
    R_NO_TARGET_HWND,
    R_POST_REJECTED,
    R_REAL_DEVICE_MISSING,
    R_UIPI_BLOCKED,
    R_UNKNOWN_MODE,
    R_VIRTUAL_DEVICE_ONLY,
)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_MK_LBUTTON = 0x0001
# legacy 通道「光标到位」核验：有界轮询，不是固定睡也不是立刻读。
# 立刻读有竞态（SetCursorPos / 合成输入要过消息泵，实机还有 DPI 缩放与
# 多屏换算，早一帧读到旧坐标 → 误判 CURSOR_NOT_AT_TARGET → 上层重试或
# 中止）；固定 sleep(0.05) 又在快路径白等 50ms。轮询兼顾两头。
_CURSOR_POLL_S = 0.001          # 轮询步长 1ms（0.5–1ms 区间取值）
_CURSOR_VERIFY_BUDGET_S = 0.050  # 到位上界：50ms 超时才判失败

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

# 提权重开一次之后，子进程靠这个环境变量知道「别再重开了」。
ELEVATED_ENV = "SJZ_ELEVATED"
# 同一条信息再走一遍命令行：`runas` 由 UAC 服务代启进程，环境块不一定
# 继承（实测没继承），只靠环境变量会让子进程再提一次，变成无限弹 UAC。
ELEVATE_FLAG = "--elevated"

# ShellExecute 的返回码里这些是「错误」但数值 > 32，只判 rc <= 32 会漏：
#   5    ERROR_ACCESS_DENIED      —— 组策略/安全软件拦下
#   740  ERROR_ELEVATION_REQUIRED —— 没提成
#   1060 ERROR_SERVICE_DOES_NOT_EXIST
#   1223 ERROR_CANCELLED          —— 用户在 UAC 上点了「否」
SHELL_ERROR_CODES = frozenset((5, 740, 1060, 1223))


class MouseStolen(Exception):
    """有别的程序或用户在动鼠标，导致点击没落到目标上。"""


class WindowNotForeground(Exception):
    """目标窗口不在前台，点击会被系统投递给别的窗口。

    实测：目标窗口非前台时 SendInput 不报错、光标也停在目标坐标，
    但目标窗口收不到任何点击事件 —— 静默失败，必须显式检出。
    """


# ---------------- 输入通道（正式三选一） ----------------
#
# interception  驱动注入：真实硬件事件、光标仅亚毫秒驻留、不依赖前台（唯一默认通道）
# legacy        SetCursorPos 借光标 + PostMessage：**已退出兜底策略**，只能显式配置
# message       纯 PostMessage 投递：全程不动光标；**目标读真实光标时无效**
#
INPUT_MODES = ("interception", "legacy", "message")
# 默认策略 fail：驱动不通就报错，不静默借系统鼠标。legacy 仍可被用户显式
# 写进 input_fallback（会写一条 warn 账本），但不是默认值。
FALLBACK_POLICIES = ("legacy", "message", "fail")
MODE_LABEL = {
    "interception": "驱动注入(interception)",
    "legacy": "PostMessage+借光标(legacy)",
    "message": "纯消息投递(message)",
}
# 通道身份文案（前端显示「是不是真实设备通道」用）
IDENTITY_LABEL = {
    "real_device": "真实设备注入",
    "virtual_device": "虚拟设备注入",
    "legacy": "legacy(借光标)",
    "message": "message(纯消息)",
    "unavailable": "驱动不可用",
}

_LOG = logging.getLogger(__name__)


def normalize_input_mode(value, fallback="interception"):
    """把配置里的通道名收敛到三选一；不认识的一律回落到 fallback。"""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in INPUT_MODES:
            return v
    return fallback


class InputChannelError(MouseStolen):
    """通道级失败：带稳定原因码，主会话按码写用户文案。

    继承 MouseStolen（而非新异常类型）：engine 现有的「鼠标被抢 → 停」安全
    路径不用改接线就能接住，原因码另取 `reason_code`。
    """

    def __init__(self, message, reason_code, channel=None, detail=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.channel = channel
        self.detail = detail


class _ChannelLedger:
    """输入通道决策账本。降级/失败一律落一条记录，不许静默。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = []

    def record(self, event):
        ev = dict(event)
        ev.setdefault("at", time.time())
        with self._lock:
            self._events.append(ev)
            del self._events[:-200]          # 只留最近 200 条，避免长跑吃内存
        return ev

    def events(self):
        with self._lock:
            return [dict(e) for e in self._events]

    def clear(self):
        with self._lock:
            self._events.clear()

    def last(self):
        with self._lock:
            return dict(self._events[-1]) if self._events else None


LEDGER = _ChannelLedger()


def channel_events():
    """本进程内所有输入通道决策/降级记录（按发生顺序）。"""
    return LEDGER.events()


def _config_value(key, default):
    """读配置项；配置不可用时给默认值（老配置文件永远能读）。"""
    try:
        from .config import Config
        return Config.load().get(key, default)
    except Exception:                        # noqa: BLE001
        return default


def _resolve_fallback(value=None):
    """降级策略。**默认 fail**：驱动不可用时必须显式报错。

    `legacy` 会 SetCursorPos 借系统光标（录屏可见、会抢用户的鼠标），
    只能由用户显式写进配置才可达；默认值不再指向它。
    """
    if value is not None:
        v = str(value).strip().lower()
    else:
        v = _config_value("input_fallback", "fail")
        v = str(v).strip().lower() if isinstance(v, str) else "fail"
    if not v in FALLBACK_POLICIES:
        return "fail"
    if v == "legacy" and not _legacy_allowed():
        # 兜底默认关闭：legacy 会 SetCursorPos 借系统光标（录屏可见、占用户
        # 鼠标），必须显式打开 input_legacy_borrow 才生效。
        LEDGER.record({"stage": "init", "from": "legacy", "to": "fail",
                       "reason_code": R_FALLBACK_DISABLED,
                       "reason": "legacy 需要借系统光标；input_legacy_borrow="
                                 "false，按 fail 处理（不静默降级）",
                       "policy": "fail", "ok": True, "severity": "warn"})
        return "fail"
    return v


def _legacy_allowed():
    """legacy（借系统光标）是否被显式允许。默认 False。

    老配置里残留 `input_fallback: "legacy"` 时不能自动生效 —— 那正是
    「静默抢用户鼠标」的来源。必须同时打开 `input_legacy_borrow`。
    """
    v = _config_value("input_legacy_borrow", False)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(v)


def _driver_probe():
    """驱动是否就绪。失败不抛异常，统一变成 (ok, 人话, 稳定原因码)。

    原因码要区分三件事，否则用户看到「驱动不可用」却不知道是没装、
    还是装了但枚举不到真实鼠标（后者才是本机最容易踩的坑）。
    """
    from .interception import driver_ready, driver_status
    code_map = {
        "REAL_DEVICE_MISSING": R_REAL_DEVICE_MISSING,
        "VIRTUAL_DEVICE_ONLY": R_VIRTUAL_DEVICE_ONLY,
        "DEVICE_NOT_FOUND": R_DEVICE_NOT_FOUND,
        "DRIVER_SEND_FAILED": R_DRIVER_SEND_REJECTED,
        "DRIVER_UNAVAILABLE": R_DRIVER_UNAVAILABLE,
        # 硬判据细分（core/interception.py: kernel_state）
        "DRIVER_SERVICE_MISSING": R_DRIVER_UNAVAILABLE,
        "DRIVER_NOT_RUNNING": R_DRIVER_UNAVAILABLE,
    }
    try:
        ready, why = driver_ready()
    except Exception as e:                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", R_DRIVER_UNAVAILABLE
    if ready:
        return True, "", None
    # 不可用：再问一次细分原因码（driver_ready 只给二元组）
    code, detail = R_DRIVER_UNAVAILABLE, (why or "驱动未就绪")
    status = None
    try:
        status = driver_status()
        if not status.get("ok"):
            mapped = code_map.get(status.get("reason_code"))
            if mapped:
                code = mapped
            detail = status.get("detail") or detail
    except Exception:                        # noqa: BLE001
        pass
    # 只有「硬判据证明内核驱动没装」时才补安装指引；dll 缺失、context 打不开等
    # 情况加这句会把用户带去重装驱动，反而更误导。
    ks = (status or {}).get("kernel")
    if code == R_DRIVER_UNAVAILABLE and ks and not ks.get("ok"):
        detail = (f"{detail}；内核驱动未安装或未生效 —— 请重新运行安装程序并勾选"
                  "『安装高速输入驱动』，安装完成后重启电脑")
    return False, detail, code


def message_capability(hwnd=None):
    """纯消息通道对目标窗口的能力评估（不投鼠标消息、不动光标）。

    返回 dict：
      usable        结构可用（窗口在、线程在泵消息、UIPI 不拦）
      functional    "unsupported" = 目标读真实光标，消息进不了游戏逻辑
                    "unknown"     = 结构可用，但只有真机能证明游戏响应
      reason_code   usable=False 的原因码；结构可用时为 None
      detail        人话

    **`usable=True` 不代表点击会生效**：它只证明消息能进窗口的消息栈。
  """
    from .nocursor import probe_message

    if not hwnd:
        return {"hwnd": None, "window_class": "", "usable": False,
                "reason_code": R_NO_TARGET_HWND,
                "detail": "没有目标窗口句柄，纯消息无处投递",
                "functional": "unknown"}
    cap = probe_message(hwnd)
    try:
        ours = process_integrity()
        target = process_integrity(hwnd)
    except Exception:                        # noqa: BLE001
        ours = target = None
    cap["integrity"] = {"ours": ours, "target": target}
    if ours is not None and target is not None and target > ours:
        cap.update({
            "usable": False,
            "reason_code": R_UIPI_BLOCKED,
            "detail": (f"目标窗口是「{integrity_name(target)}」权限，本进程是"
                       f"「{integrity_name(ours)}」；UIPI 会静默丢掉消息"),
        })
    if cap.get("functional") == "unsupported":
        cap["detail"] = (
            "目标窗口类 UnrealWindow：游戏按下时读真实光标位置"
            "（docs/auto-buy-design.md §4.8；branchA 实测 200/200 次窗口受理、"
            "游戏侧零响应），纯消息点击不会进入游戏逻辑")
    return cap


def _decide_channel(requested_raw, fallback=None, message_cap_fn=None):
    """通道决策：返回实际生效通道 + 原因，降级/失败都记账。

    `message_cap_fn` 为 None 时不评估消息通道的结构可用性（Keyboard 用：
    它不需要窗口能力评估，投递失败会在 PostMessage 返回值上暴露）。
    """
    requested = normalize_input_mode(requested_raw, "interception")
    policy = _resolve_fallback(fallback)
    if requested == "legacy" and not _legacy_allowed():
        why = ("legacy 通道用 SetCursorPos 借系统光标（录屏可见、占用户的"
               "鼠标）；input_legacy_borrow=false，默认禁用")
        LEDGER.record({"stage": "init", "from": "legacy", "to": None,
                       "reason_code": R_FALLBACK_DISABLED, "reason": why,
                       "policy": policy, "ok": False, "severity": "warn"})
        raise InputChannelError(why, R_FALLBACK_DISABLED, channel="legacy",
                                detail=why)
    out = {"requested": requested, "effective": requested, "policy": policy,
           "reason_code": None, "reason": "", "driver_warn": None,
           "message_cap": None}

    raw = requested_raw.strip().lower() if isinstance(requested_raw, str) else ""
    if raw not in INPUT_MODES:
        reason = f"未知输入通道 {requested_raw!r}，按 {requested} 处理"
        LEDGER.record({"stage": "init", "from": str(requested_raw),
                       "to": requested, "reason_code": R_UNKNOWN_MODE,
                       "reason": reason, "policy": policy, "ok": True})
        out["reason_code"] = R_UNKNOWN_MODE
        out["reason"] = reason

    def _raise(code, why, target):
        LEDGER.record({"stage": "init", "from": requested, "to": target,
                       "reason_code": code, "reason": why,
                       "policy": policy, "ok": False})
        msg = f"输入通道不可用（{MODE_LABEL.get(requested, requested)}）：{why}"
        if policy == "fail":
            msg += "；input_fallback=fail，按配置直接失败（不降级）"
        raise InputChannelError(msg, code, channel=requested, detail=why)

    def _degrade(code, why):
        target = policy
        if target == "fail":
            _raise(code, why, None)
        if target == requested:
            _raise(R_FALLBACK_DISABLED,
                   f"{why}；降级目标与请求通道相同（input_fallback={policy}）",
                   None)
        if target == "message" and message_cap_fn is not None:
            cap = message_cap_fn()
            out["message_cap"] = cap
            if not cap.get("usable"):
                _raise(cap.get("reason_code") or R_MESSAGE_NOT_ACCEPTED,
                       f"降级目标 message 也不可用：{cap.get('detail')}", None)
        out["effective"] = target
        out["reason_code"] = code
        out["reason"] = why
        if requested == "interception":
            out["driver_warn"] = why
        ev = {"stage": "init", "from": requested, "to": target,
              "reason_code": code, "reason": why, "policy": policy, "ok": True}
        if target == "legacy":
            # legacy 借系统光标：命中率能保，但录屏可见、会抢用户的鼠标。
            # 只有用户显式把 input_fallback 写成 legacy 才会走到这里，仍然要记账。
            ev["severity"] = "warn"
            ev["warning"] = (f"已按配置降级到 legacy：该通道用 SetCursorPos 借"
                             f"系统光标，录屏可见并占用用户鼠标；要避免请把 "
                             f"input_fallback 设为 fail")
        cap = out.get("message_cap") or {}
        if cap.get("functional") == "unsupported":
            ev["severity"] = "warn"
            ev["warning"] = cap.get("detail")
        LEDGER.record(ev)
        # review F7：降级不能只躺在内存账本里 —— 日志里必须能查到
        # 「什么时候、因为什么、从哪条降到哪条」。
        _LOG.warning("输入通道降级：%s -> %s，原因码=%s，详情=%s，策略=%s",
                     MODE_LABEL.get(requested, requested),
                     MODE_LABEL.get(target, target), code, why, policy)
        return out

    if requested == "interception":
        probe = _driver_probe()
        ok, why = probe[0], probe[1]
        code = (probe[2] if len(probe) > 2 else None) or R_DRIVER_UNAVAILABLE
        if not ok:
            return _degrade(code, why or "驱动未就绪")
        return out

    if requested == "message":
        cap = message_cap_fn() if message_cap_fn is not None else {
            "usable": True, "functional": "unknown", "reason_code": None,
            "detail": ""}
        out["message_cap"] = cap
        if not cap.get("usable"):
            return _degrade(cap.get("reason_code") or R_MESSAGE_NOT_ACCEPTED,
                            cap.get("detail") or "目标窗口不接收消息")
        if cap.get("functional") == "unsupported":
            strict = bool(_config_value("input_message_strict", True))
            if strict:
                return _degrade(R_MESSAGE_RAW_CURSOR,
                                cap.get("detail") or "目标读真实光标，纯消息无效")
            LEDGER.record({"stage": "init", "from": "message", "to": "message",
                           "reason_code": R_MESSAGE_RAW_CURSOR,
                           "reason": cap.get("detail", ""),
                           "policy": policy, "ok": True, "severity": "warn"})
            out["reason_code"] = R_MESSAGE_RAW_CURSOR
            out["reason"] = cap.get("detail", "")
        return out

    return out


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        # 必须是 ULONG_PTR（指针大小），不能是 POINTER(c_ulong)：
        # 后者在 64 位下是 8 字节指针但 INPUT 的 union 需要按 8 字节对齐，
        # 实测用 POINTER 会让 sizeof(INPUT)=28 而不是 40，SendInput 直接返回 0
        # 并报 ERROR_INVALID_PARAMETER(87) —— 点击全部静默失效。
        ("dwExtraInfo", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
         else ctypes.c_ulong),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _cursor_pos():
    """真实光标位置；读不到（测试桩 / 无桌面）返回 None。

    不要直接用 `_user32.GetCursorPos`：测试里 `_user32` 会被换成只实现了
    SetCursorPos 的桩，AttributeError 会从「点完之后恢复光标」这一步抛出来，
    把真正的点击错误盖掉。读不到就不恢复，不影响主流程。
    """
    try:
        p = wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(p)):
            return p.x, p.y
    except Exception:                        # noqa: BLE001
        return None
    return None


def landing_unobservable(status):
    """上一击是否「已发出但落地不可观测」—— 失活自愈的判据。

    返回 (blind, last_click)。判据来自真机：游戏内部激活态丢失时，注入仍被
    受理（accepted=True），但游戏零响应，观测器也绑不上 HID 设备
    （hdev=None、down/up/moves 全 0）。只有这一组合才允许提示用户「点一下
    游戏画面」—— 把「点空了」和「没点成」分开，避免误导。

    status 传 Mouse.channel_status() 的返回值（或它的子集）。
    """
    last = (status or {}).get("last_click") or {}
    if last.get("accepted") is not True:
        return False, last
    land = last.get("landing") or {}
    blind = (land.get("mode") == "raw_input_blind"
             or (int(land.get("down") or 0) == 0
                 and int(land.get("up") or 0) == 0))
    return blind, last


# ---- feature:mouse.human BEGIN ----
def trajectory_capability(mode="interception", cfg=None):
    """拟人轨迹在当前通道下到底能不能生效（引擎启动打日志、状态回读用）。

    返回 core/humanmouse.py:Trajectory.status() 的字典；模块不可用时给一份
    degraded 的说明，绝不默认「应该是生效的」。
    """
    try:
        from .humanmouse import Trajectory
    except Exception as e:                                   # noqa: BLE001
        return {"effective": "degraded", "active": False,
                "reason": f"轨迹模块不可用：{type(e).__name__}: {e}"}
    try:
        return Trajectory(cfg, mode=mode).status()
    except Exception as e:                                   # noqa: BLE001
        return {"effective": "degraded", "active": False,
                "reason": f"轨迹规划不可用：{type(e).__name__}: {e}"}
# ---- feature:mouse.human END ----


# 点击快路径默认开关。类级默认值与实例共用这一份只读语义；实例在
# `_init_click_perf()` 里换成自己的 dict，基准工具可逐项 A/B。
CLICK_OPT_DEFAULTS = {"batch": True, "cache": True,
                      "restore_drv": True, "drop_move": True,
                      "restore_early": True, "restore_wait_move": True,
                      "btn_noabs": False}


class Mouse:
    # ---- 点击性能/快速路径字段的类级默认值 BEGIN ----
    # 这些字段只在 `_init_click_perf()` 里被实例化成可变对象。测试与
    # `Keyboard.click` 兼容路径会用 `object.__new__` 绕过 `__init__`：
    # 读到这里拿到的就是安全默认值，任何构造路径都不会 AttributeError。
    # 可变默认值一律用不可变类型（()），真要用时由 `_init_click_perf()` 覆盖。
    _perf_trace = False
    _stage = ()
    _click_t0 = None
    _opt_click = CLICK_OPT_DEFAULTS
    _ident = None
    _ident_gen = -1
    _abs_cache = None
    _watch = None
    _home = None
    _home_target = None
    _home_target_ts = 0.0
    _home_pending = False
    _home_deadline = None
    _click_state_ready = False
    _batch_warn_once = False
    # 像素基准/虚拟屏几何：每次点击前刷新，窗口移动/改分辨率/切全屏都要跟上。
    _screen = None
    _vx = 0
    _vy = 0
    _vw = 1
    _vh = 1
    # ---- 点击性能/快速路径字段的类级默认值 END ----
    # 通道状态字段的同类兜底：折叠/裸构造时 channel_status 也要能读。
    _verify = True
    _occ = ()
    _last_click = None
    _clicker = None
    # ---- feature:mouse.human BEGIN ----
    _traj = None        # attach_trajectory() 之后才有；没挂上就完全走老路子
    _last_path = None
    _prepared = None    # 提前量窗口内预热好的落点（开火时零位移）
    _tail_s = 0.0       # 开火前预留的收尾短移窗口（秒）
    _fire_deadline = None   # 计划的按下时刻（perf_counter）
    _fire_planned = None    # 最近一次开火计划时刻（量按下误差用）
    _fire_first = False     # 下一次开火点击是不是卡点那一击
    # ---- feature:mouse.human END ----

    def __init__(self, screen=None, game_hwnd=None, mode="interception",
                 fallback=None, verify=True):
        # screen 可为 None：Keyboard.click 的兼容路径只需要「原点 0,0」的
        # 绝对坐标语义，没必要为了一个原点去造一个 Screen（那要开 mss、
        # 还要用户有第二个显示器）。以前走 Mouse.__new__ 手填字段，
        # 漏掉 _mode/_drv_warn 之后第一次 click 才 AttributeError。
        # 像素坐标原点 = 游戏客户区左上角（找不到窗口时就是显示器左上角）。
        # 归一化区域按客户区换算，鼠标落点必须用同一个原点，否则窗口化时
        # 会整体偏一个边框的距离。
        base = getattr(screen, "base_rect", None) if screen is not None else None
        self._screen = screen
        self._origin_left = base["left"] if base else (
            screen.bounds["left"] if screen is not None else 0)
        self._origin_top = base["top"] if base else (
            screen.bounds["top"] if screen is not None else 0)
        self._game_hwnd = game_hwnd
        # 三个正式通道（interception / legacy / message）+ 可配置降级策略。
        # 降级不再静默：实际生效通道、原因码、时间全部进 LEDGER。
        self._requested_raw = mode
        self._requested_mode = normalize_input_mode(mode)
        self._fallback_policy = _resolve_fallback(fallback)
        self._verify = bool(verify)
        decision = _decide_channel(
            mode, fallback, lambda: message_capability(self._game_hwnd))
        self._mode = decision["effective"]
        self._drv_warn = decision.get("driver_warn")
        self._reason_code = decision.get("reason_code")
        self._reason = decision.get("reason", "")
        self._message_cap = decision.get("message_cap")
        self._integrity = None
        self._last_click = None
        self._occ = []          # 近 200 次驱动点击的占用窗口（ms）
        self._clicker = None
        self._init_click_perf()
        # ---- feature:mouse.human BEGIN ----
        self._prepared = None   # 预热落点：{"x","y","at","ms"}; 开火命中后零位移
        self._tail_s = 0.0
        self._fire_deadline = None
        self._fire_planned = None
        self._fire_first = False
        # ---- feature:mouse.human END ----
        if self._mode in ("legacy", "message"):
            # UIPI 主动判定：不匹配就当场失败，别等点不动了才发现
            self._gate_integrity()
        self._vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        self._vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        self._vw = max(1, _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) - 1)
        self._vh = max(1, _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) - 1)
        # review F7：`channel_status()` 在生产代码里必须真的有调用点 ——
        # 构造时取一次快照，降级当场留日志，上层随时能读到。
        self._init_status = self.channel_status()
        if self._mode != self._requested_mode or self._reason_code:
            _LOG.warning("输入通道生效=%s（请求=%s），原因码=%s，详情=%s",
                         self.channel_label,
                         MODE_LABEL.get(self._requested_mode,
                                        self._requested_mode),
                         self._reason_code, self._reason)

    def _init_click_perf(self):
        """点击性能字段（构造与 Mouse.__new__ 两条路径共用）。

        只在基准工具里打开 _perf_trace；默认关闭，行为与逐条 send 一致。
        """
        self._perf_trace = False
        self._stage = []
        self._click_t0 = None
        # 每项可单独关掉，便于 A/B 定位是哪一项带来的收益。
        self._opt_click = dict(CLICK_OPT_DEFAULTS)
        self._ident = None
        self._ident_gen = -1
        self._abs_cache = None
        self._watch = None
        # 还原是「最后一条 stroke 一定回原点」的异步动作：点击返回时光标
        # 可能还在目标点。连点时下一击会把这瞬态读成「原点」，于是把原点
        # 记成目标点、逐击漂移。这里记住上一击的原点与目标点：读到目标点
        # 就沿用缓存的原点，读到原点就清 pending。
        self._home = None
        self._home_target = None
        self._home_target_ts = 0.0
        self._home_pending = False
        self._home_deadline = None
        self._click_state_ready = True

    def _ensure_click_state(self):
        """点击字段的统一惰性入口（幂等）。

        `Mouse.__new__`（测试/Keyboard 兼容路径）不跑 `__init__`，直接调
        `_click_driver` 也不经过 `click()`。两条入口都先过这里，字段才存在；
        类级默认值再兜一层，读的时候永远不会 AttributeError。
        """
        if not self.__dict__.get("_click_state_ready"):
            self._init_click_perf()

    def _settle_home_pending(self):
        """收口「回原点未确认」：超时后主动再设一次，不依赖下一击。

        回原点 stroke 已按 FIFO 排队，但桌面位置更新可能晚于事件几毫秒。
        以前只把 pending 挂给下一次点击复核：点击一停就永远挂着，且下一击
        首段读到的可能是被延迟更新覆盖后的目标点。这里在截止时刻后先回读
        一次：已经在原点就只清状态（不额外动光标）；确实没回去才补一次
        SetCursorPos 并复核。
        """
        if not getattr(self, "_home_pending", False):
            return
        home = getattr(self, "_home", None)
        if home is None:
            self._home_pending = False
            return
        deadline = getattr(self, "_home_deadline", None)
        if deadline is not None and time.perf_counter() < deadline:
            return
        if _cursor_pos() == home:
            self._home_pending = False
            self._home_deadline = None
            return
        if not self._cursor_reachable(home):
            # 被 ClipCursor 裁掉：SetCursorPos 也到不了，别空转；标记仍在
            # pending，等裁剪解除后再收口。
            self._home_deadline = time.perf_counter() + 0.05
            return
        self._emergency_cursor_restore(home)
        end = time.perf_counter() + 0.002
        while _cursor_pos() != home and time.perf_counter() < end:
            time.sleep(0)
        if _cursor_pos() == home:
            self._home_pending = False
            self._home_deadline = None
        else:
            self._home_deadline = time.perf_counter() + 0.01   # 再给 10ms

    def _refresh_geometry(self):
        """刷新像素基准与虚拟屏尺寸，返回几何快照（用于坐标缓存失效）。

        窗口移动/客户区变化/改分辨率/切全屏都会改基准；以前只在 __init__
        取一次，`_abs_cache` 又只按 (x,y) 命中，就会点到旧屏幕位置，还绕过
        `_abs` 的越界检查。这里每次点击前刷新，缓存携带几何快照。
        """
        screen = getattr(self, "_screen", None)
        if screen is not None:
            base = getattr(screen, "base_rect", None)
            bounds = getattr(screen, "bounds", None)
            if base:
                self._origin_left, self._origin_top = base["left"], base["top"]
            elif bounds:
                self._origin_left, self._origin_top = bounds["left"], bounds["top"]
            # 几何变化就丢弃旧换算/观测窗口缓存
            if getattr(self, "_geom", None) != (self._origin_left,
                                                self._origin_top):
                self._abs_cache = None
        gsm = getattr(_user32, "GetSystemMetrics", None)
        if gsm is not None:
            self._vx = gsm(_SM_XVIRTUALSCREEN)
            self._vy = gsm(_SM_YVIRTUALSCREEN)
            self._vw = max(1, gsm(_SM_CXVIRTUALSCREEN) - 1)
            self._vh = max(1, gsm(_SM_CYVIRTUALSCREEN) - 1)
        geom = (self._origin_left, self._origin_top, self._vx, self._vy,
                self._vw, self._vh)
        self._geom = geom
        return geom

    def _clip_rect(self):
        """当前前台进程的鼠标裁剪矩形；无裁剪/等同全屏时返回 None。"""
        r = wintypes.RECT()
        get_clip = getattr(_user32, "GetClipCursor", None)
        if get_clip is None or not get_clip(ctypes.byref(r)):
            return None
        if r.left >= r.right or r.top >= r.bottom:
            return None
        if (r.left, r.top) == (self._vx, self._vy) and \
                (r.right, r.bottom) == (self._vx + self._vw, self._vy + self._vh):
            return None
        return (r.left, r.top, r.right, r.bottom)

    def _cursor_reachable(self, pt):
        c = self._clip_rect()
        if c is None:
            return True
        return c[0] <= pt[0] <= c[2] and c[1] <= pt[1] <= c[3]

    def _emergency_cursor_restore(self, pos):
        """把光标放回**用户原来的位置**。只还原，不做任何点击定位。

        只有驱动 stroke 已经发不出去（句柄失效 / 驱动被卸载）时才用：
        否则用户的鼠标会永久停在点击点，那才是真正的「抢鼠标」。

        allowlist: CURSOR-RESTORE-ONLY  SetCursorPos
        """
        if pos is None:
            return False
        _user32.SetCursorPos(int(pos[0]), int(pos[1]))
        end = time.perf_counter() + 0.002
        while _cursor_pos() != tuple(pos) and time.perf_counter() < end:
            time.sleep(0)
        return _cursor_pos() == tuple(pos)

    def _legacy_place_cursor(self, x, y):
        """legacy 通道把系统光标搬到目标点。**默认不可达**。

        只有用户显式把 `input_mode` 或 `input_fallback` 设成 legacy 才会走到这里。
        它是这条通道的点击定位方式（游戏按下时读真实光标），不是兜底。

        allowlist: LEGACY-BORROW  SetCursorPos
        """
        return bool(_user32.SetCursorPos(int(x), int(y)))


    def _force_restore(self, drv, ident, origin, target, flags):
        """异常/中断路径的硬还原：回原点 stroke +（未被裁剪时）SetCursorPos。

        走到这里的场景：批次部分写入后抛错、StopSignal/KeyboardInterrupt、
        `wait_buttons` 异常。任何一条都不能把光标永久留在点击点。
        """
        if origin is None:
            return
        # 目标点就是原点时无需再发一条回原点 stroke
        need_move = target is None or tuple(target) != tuple(origin)
        ox = oy = None
        try:
            ox, oy = self._abs(origin[0] - self._origin_left,
                               origin[1] - self._origin_top)
        except Exception:                                   # noqa: BLE001
            ox = oy = None
        if ox is not None and need_move:
            try:
                drv.send_mouse(0, x=ox, y=oy, flags=flags,
                               device=ident["device"])
            except Exception:                               # noqa: BLE001
                pass
        if self._cursor_reachable(origin):
            self._emergency_cursor_restore(origin)
        ok = _cursor_pos() == origin
        self._home = origin
        self._home_target = target
        self._home_target_ts = time.perf_counter()
        self._home_pending = not ok
        self._home_deadline = None if ok else time.perf_counter() + 0.02

    def _gate_integrity(self):
        """自身完整性 vs 目标窗口：UIPI 下所有非驱动输入都会被静默丢弃。"""
        hwnd = self._game_hwnd
        if not hwnd:
            return
        try:
            ours = process_integrity()
            target = process_integrity(hwnd)
        except Exception as e:                   # noqa: BLE001
            # 读不到就按「未知」处理，不做假判定（测试替身也会走到这里）
            self._integrity = {"ours": None, "target": None,
                              "error": f"{type(e).__name__}: {e}"}
            LEDGER.record({
                "stage": "init", "from": self._mode, "to": self._mode,
                "reason_code": R_INTEGRITY_UNKNOWN,
                "reason": f"读不到完整性级别：{e}",
                "policy": self._fallback_policy, "ok": True,
                "severity": "info"})
            return
        self._integrity = {
            "ours": ours, "target": target,
            "ours_name": integrity_name(ours),
            "target_name": integrity_name(target),
        }
        if ours is None or target is None:
            LEDGER.record({
                "stage": "init", "from": self._mode, "to": self._mode,
                "reason_code": R_INTEGRITY_UNKNOWN,
                "reason": "读不到自身/目标完整性级别，跳过 UIPI 预判",
                "policy": self._fallback_policy, "ok": True,
                "severity": "info"})
            return
        if target > ours:
            detail = (f"本进程是「{integrity_name(ours)}」完整性，目标窗口是"
                      f"「{integrity_name(target)}」；UIPI 会静默丢弃输入"
                      f"（SetCursorPos/PostMessage 返回成功但光标不动、目标无感）")
            LEDGER.record({
                "stage": "init", "from": self._mode, "to": None,
                "reason_code": R_UIPI_BLOCKED, "reason": detail,
                "policy": self._fallback_policy, "ok": False})
            raise InputChannelError(detail, R_UIPI_BLOCKED,
                                    channel=self._mode, detail=detail)


    def _send(self, flags, dx=0, dy=0):
        """**仅测试夹具**：TEST-FIXTURE-ONLY —— `_nonreal_input_probe.py` 的
        SendInput 对照组。

        产品点击路径禁止调用它 —— SendInput 是系统级合成输入，事件带
        LLMHF_INJECTED 且会走本机鼠标栈，与「不抢本机鼠标」的约束冲突。
        （静态不变量测试 `tests/test_click_channel.py` 按 allowlist 校验。）
        """
        # allowlist: TEST-FIXTURE-ONLY  SendInput（仅 _nonreal_input_probe.py 调用）
        inp = _INPUT(type=0)
        inp.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, 0)
        return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def _abs(self, x, y):
        """把本屏像素坐标换算成虚拟屏 0-65535 绝对坐标。

        **出界直接报错，不夹住硬点。** 以前没有这层检查：坐标算错（模板匹配
        给了个离谱的位置、配置里的归一化比例被写成 1.3）时，换算结果超出
        0-65535，驱动/系统会把它夹到屏幕边缘 —— 于是点击落在屏幕边角上。
        而这个程序的急停判据正是「鼠标甩到左上角」，一个越界点击看起来就像
        用户按了急停，或者干脆点到别的窗口上。
        宁可这一轮报错停下，也不要静悄悄点错地方。

        容 2px：`_vw` 是「宽 - 1」，右/下边界上取整时可能多出 1px。
        """
        px = self._origin_left + x - self._vx
        py = self._origin_top + y - self._vy
        if px < -2 or py < -2 or px > self._vw + 2 or py > self._vh + 2:
            raise ValueError(
                f"点击坐标越界：({x}, {y}) 换算后是虚拟屏 ({px}, {py})，"
                f"有效范围 0~{self._vw} × 0~{self._vh} —— 坐标算错了，"
                f"这一下不点（越界会被系统夹到屏幕边缘，点到别处去）")
        px = min(max(px, 0), self._vw)
        py = min(max(py, 0), self._vh)
        return (
            int(round(px * 65535 / self._vw)),
            int(round(py * 65535 / self._vh)),
        )

    @property
    def mode(self):
        """实际生效的输入通道（引擎启动时读它写日志）。"""
        return self._mode

    @property
    def requested_mode(self):
        """配置里请求的通道（可能因为降级而 != mode）。"""
        return self._requested_mode

    @property
    def driver_warning(self):
        """驱动不可用时的原因（从 interception 降级才非空）。"""
        return self._drv_warn

    @property
    def reason_code(self):
        """当前通道选择的稳定原因码（None = 按配置直接生效）。"""
        return self._reason_code

    def _record_occupancy(self, value_ms):
        # 类级默认是 ()，裸构造没跑 __init__ 时先换成实例列表。
        occ = getattr(self, "_occ", None)
        if not isinstance(occ, list):
            occ = self._occ = []
        occ.append(float(value_ms))
        if len(occ) > 200:
            del occ[:len(occ) - 200]

    def occupancy_stats(self):
        """驱动点击占用窗口统计（p50/p95/max），没有样本时返回空。"""
        data = sorted(getattr(self, "_occ", None) or ())
        if not data:
            return {"n": 0, "p50": None, "p95": None, "max": None}
        n = len(data)

        def pick(q):
            idx = min(n - 1, max(0, int(round(q * (n - 1)))))
            return round(data[idx], 3)

        return {"n": n, "p50": pick(0.50), "p95": pick(0.95),
                "max": round(data[-1], 3)}

    def channel_identity(self):
        """通道身份：真实设备注入 / 虚拟设备 / legacy / message。

        前端据 `identity` 显示「当前是否真实设备通道」：
          real_device    驱动 + 枚举到的真实鼠标（Raw Input hDevice 归属它）
          virtual_device 驱动 + 虚拟设备（需 input_allow_virtual，默认不允许）
          legacy         SetCursorPos + PostMessage（LL 钩子看得到注入标志）
          message        纯窗口消息（不动光标）
        """
        base = {"requested": self._requested_mode, "effective": self._mode}
        if self._mode == "interception":
            from .interception import RealDeviceUnavailable, shared
            try:
                ident = shared().mouse_device()
            except RealDeviceUnavailable as e:
                base.update({"identity": "unavailable",
                             "reason_code": e.reason_code,
                             "detail": str(e)})
                return base
            except Exception as e:                        # noqa: BLE001
                base.update({"identity": "unavailable",
                             "reason_code": R_DRIVER_UNAVAILABLE,
                             "detail": f"{type(e).__name__}: {e}"})
                return base
            base.update({
                "identity": "virtual_device" if ident.get("virtual") else "real_device",
                "device": ident.get("device"),
                "hardware_id": (ident.get("hardware_id") or "")[:140],
                "vid": ident.get("vid"),
                "pid": ident.get("pid"),
                "device_source": ident.get("source"),
                "real": not ident.get("virtual"),
            })
            return base
        base.update({"identity": self._mode, "real": False})
        return base

    @property
    def channel_label(self):
        """一句人话的通道身份（日志/前端直接用，别各自拼字符串）。

        review F8：日志里不能再把 message 写成「PostMessage(legacy)」——
        文案一律以实际生效通道 + 实际设备身份为准。
        """
        ident = self.channel_identity().get("identity")
        return IDENTITY_LABEL.get(ident) or MODE_LABEL.get(self._mode, self._mode)

    def channel_status(self):
        """此刻真正在用哪条通道、为什么、降级过几次 —— 随时可查。

        主会话应该把这个字典原样带进日志/接口；`events` 是全进程账本，
        不只本实例，便于定位「谁先降的级」。
        """
        # 状态查询也是 pending 的收口点：点击停了以后没有「下一击」可等。
        self._settle_home_pending()
        return {
            "requested": self._requested_mode,
            "requested_raw": self._requested_raw,
            "effective": self._mode,
            "degraded": self._mode != self._requested_mode,
            "reason_code": self._reason_code,
            "reason": self._reason,
            "fallback_policy": self._fallback_policy,
            "verify": self._verify,
            "identity": self.channel_identity(),
            "occupancy": self.occupancy_stats(),
            "integrity": dict(self._integrity or {}),
            "message_capability": dict(self._message_cap or {}),
            "last_click": dict(self._last_click or {}),
            "events": LEDGER.events(),
        }

    # ---------------- 拟人轨迹（feature:mouse.human） ----------------
    def attach_trajectory(self, traj):
        """挂上轨迹决策器（引擎在 `_run` 里构造）。不挂 = 行为与改动前一致。"""
        self._traj = traj

    def _plan_path(self, x, y):
        """给这一次点击算一条路径。返回 (points, total_ms) 或 None。"""
        traj = getattr(self, "_traj", None)
        if traj is None:
            return None
        pos = _cursor_pos()
        if pos is None:
            self._last_path = {
                "used": False, "channel": self._mode,
                "reason": "读不到当前光标位置，跳过拟人轨迹（点击照常）"}
            return None
        # click 的 x/y 是「截屏原点」相对坐标，光标是虚拟屏绝对坐标，先对齐
        return traj.path_for(x, y,
                             start=(pos[0] - self._origin_left,
                                    pos[1] - self._origin_top))

    @staticmethod
    def _split_path(path):
        if isinstance(path, tuple) and len(path) == 2:
            return list(path[0] or []), float(path[1] or 0.0)
        return list(path or []), 0.0

    def move_path(self, points, total_ms=0):
        """沿点序列移动真实光标（**不点击**）。

        points 是 [x, y] 或 [x, y, t_ms]（t 为累计毫秒，plan 直接给）。
        message 通道没有真实光标可动 → 原地不动，并把原因留在 _last_path，
        绝不假装「已经走过去了」。
        """
        pts, ts = [], []
        for p in points or []:
            try:
                pts.append((float(p[0]), float(p[1])))
                ts.append(float(p[2]) if len(p) > 2 else None)
            except (TypeError, ValueError, IndexError):
                continue
        if len(pts) < 2:
            return False
        if ts[0] is None:
            total = float(total_ms or 0.0)
            ts = [total * i / (len(pts) - 1) for i in range(len(pts))]
        else:
            ts = [max(0.0, t or 0.0) for t in ts]

        if self._mode == "message":
            self._last_path = {
                "used": False, "channel": "message", "points": len(pts),
                "reason": "纯消息通道无真实移动，拟人轨迹不生效"}
            return False

        started = time.perf_counter()
        try:
            if self._mode == "legacy":
                for (px, py), t in zip(pts, ts):
                    self._legacy_place_cursor(
                        int(round(self._origin_left + px)),
                        int(round(self._origin_top + py)))
                    gap = (started + t / 1000.0) - time.perf_counter()
                    if gap > 0:
                        time.sleep(gap)
            else:
                from .interception import (MOUSE_MOVE_ABSOLUTE,
                                           MOUSE_VIRTUAL_DESKTOP, shared)
                drv = shared()
                ident = drv.mouse_device()
                flags = MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP
                for (px, py), t in zip(pts, ts):
                    try:
                        ax, ay = self._abs(px, py)
                    except ValueError:
                        break     # 出界的点直接停，后面交给正常点击/还原
                    drv.send_mouse(0, x=ax, y=ay, flags=flags,
                                   device=ident["device"])
                    gap = (started + t / 1000.0) - time.perf_counter()
                    if gap > 0:
                        time.sleep(gap)
        except Exception as e:                               # noqa: BLE001
            # 轨迹是观感层，不能因为它把整场抢购打断：记录降级，点击照常走
            self._last_path = {
                "used": False, "channel": self._mode, "points": len(pts),
                "reason": f"轨迹事件发送失败，本次点击改走直线："
                          f"{type(e).__name__}: {e}"}
            return False

        self._last_path = {
            "used": True, "channel": self._mode, "points": len(pts),
            "ms": round(float(ts[-1]), 2), "at": time.time()}
        return True

    def _emit_plan(self, path):
        if not path:
            return False
        points, total_ms = self._split_path(path)
        return self.move_path(points, total_ms)

    def _prepared_hit(self, x, y):
        """预热落点是否仍然有效（同一目标、未超时、光标确实停在上面）。"""
        from .humanmouse import PREHEAT_MAX_AGE_S
        p = getattr(self, "_prepared", None)
        if not p:
            return None
        try:
            if abs(float(p["x"]) - float(x)) > 1.0 \
                    or abs(float(p["y"]) - float(y)) > 1.0:
                return None
            age = time.perf_counter() - float(p["at"])
        except (TypeError, ValueError, KeyError):
            self._prepared = None
            return None
        if age > PREHEAT_MAX_AGE_S:
            self._prepared = None
            return None
        pos = _cursor_pos()
        if pos is not None:
            sx = self._origin_left + float(x)
            sy = self._origin_top + float(y)
            if abs(pos[0] - sx) > 2 or abs(pos[1] - sy) > 2:
                # 光标被挪走了：这次不算零位移命中，但保留预热记录，
                # 让 _prepared_tail 有机会用 8~20ms 短移补回来（不是瞬移）。
                return None
        return p

    def preheat(self, x, y, budget_s=None):
        """提前量窗口内把光标走到目标附近；开火点击随后只做收尾 + down/up。

        必须在开火前调用。`budget_s` 是到开火的剩余时间，落点策略分三档：
        - 预算够走完整条曲线：直接落到按钮上，开火零位移；
        - 预算不够：分段预热——预热点停在按钮前，开火前留 8~20ms 短移
          补齐剩余位移，短移窗口从等待时间里扣，按下时刻不变；
        - 连 30ms 预热段都塞不下，或剩余位移 >120px：退回瞬时直线落点，
          并在 `_last_path` / stats 里明确记成「瞬移特征」。
        返回 True = 光标已停在目标或安全区，开火不再走完整曲线。
        """
        import math as _math
        from .humanmouse import (PREHEAT_MAX_AGE_S, SEGMENT_LEG_MIN_MS,
                                SEGMENT_RESERVE_MS, TAIL_JUMP_BUDGET_MS,
                                TAIL_MAX_MS, TAIL_MAX_PX, TAIL_MIN_MS,
                                TAIL_SPEED_PX_S)
        traj = getattr(self, "_traj", None)
        if traj is None or not getattr(traj, "active", False):
            self._prepared = None
            self._tail_s = 0.0
            self._fire_deadline = None
            self._last_path = {
                "used": False, "preheat": True, "channel": self._mode,
                "reason": "拟人轨迹未生效，开火按瞬时直线落点"}
            return False
        if self._mode == "message":
            self._prepared = None
            self._tail_s = 0.0
            self._fire_deadline = None
            self._last_path = {
                "used": False, "preheat": True, "channel": "message",
                "reason": "纯消息通道无真实移动，拟人轨迹不生效"}
            return False
        if self._prepared_hit(x, y) is not None:
            return True

        need_s = 0.0
        try:
            need_s = float(traj.spec.get("move_ms") or 0) / 1000.0
        except (AttributeError, TypeError, ValueError):
            need_s = 0.0
        now = time.perf_counter()
        budget = None if budget_s is None else max(0.0, float(budget_s))
        pos = _cursor_pos()
        if pos is None:
            self._prepared = None
            self._tail_s = 0.0
            self._fire_deadline = None
            self._last_path = {
                "used": False, "preheat": True, "channel": self._mode,
                "reason": "读不到当前光标位置，预热取消（开火按瞬时直线落点）"}
            return False
        sx = pos[0] - self._origin_left
        sy = pos[1] - self._origin_top
        dist = _math.hypot(float(x) - sx, float(y) - sy)

        # ---- 一档：预算够，整条曲线落到按钮上 ----
        if budget is None or budget + 0.002 >= need_s:
            path = traj.prewarm_path(x, y, start=(sx, sy))
            if not path:
                self._prepared = None
                self._tail_s = 0.0
                self._fire_deadline = None
                return False
            points, ms = self._split_path(path)
            ok = self.move_path(points, ms)
            done = time.perf_counter()
            if ok:
                self._prepared = {"x": float(x), "y": float(y), "at": done,
                                  "ms": float(ms), "seg": False,
                                  "tx": float(x), "ty": float(y)}
                # 预留一个尾部窗口：等待期光标被挪走几像素时用短移补救，
                # 而不是瞬移。按下时刻不变（等待时间少了这么多）。
                # budget=None 是倒计时路线（离 due 还有几百毫秒）：同样留
                # 20ms 短移窗口，由引擎在 due 前调用 close_tail 提前收尾，
                # 不能在开火瞬间移动 —— 那会把按下时刻拖后 8~20ms。
                self._tail_s = SEGMENT_RESERVE_MS / 1000.0 \
                    if budget is None else min(SEGMENT_RESERVE_MS / 1000.0, budget)
                self._fire_deadline = (now + budget) if (budget is not None
                                                        and self._tail_s) else None
            else:
                self._prepared = None
                self._tail_s = 0.0
                self._fire_deadline = None
            self._last_path = {
                "used": bool(ok), "preheat": True, "channel": self._mode,
                "points": len(points), "ms": round(float(ms), 2),
                "at": time.time(),
                "reserve_ms": round(self._tail_s * 1000.0, 2),
                "reason": "" if ok else "预热移动失败，开火按瞬时直线落点"}
            return bool(ok)

        # ---- 二档：分段预热，停在按钮前，开火前收尾 ----
        tail_s = max(TAIL_MIN_MS / 1000.0,
                     min(TAIL_MAX_MS / 1000.0, dist / TAIL_SPEED_PX_S))
        leg_s = budget - tail_s
        if dist >= 2.0 and leg_s >= SEGMENT_LEG_MIN_MS / 1000.0:
            d_tail = dist * tail_s / (tail_s + leg_s)
            if d_tail <= TAIL_MAX_PX:
                ux, uy = (float(x) - sx) / dist, (float(y) - sy) / dist
                stop = (float(x) - ux * d_tail, float(y) - uy * d_tail)
                path = traj.prewarm_path(x, y, start=(sx, sy), stop=stop,
                                         ms=leg_s * 1000.0)
                points, ms = self._split_path(path) if path else ([], 0.0)
                ok = self.move_path(points, ms) if path else False
                done = time.perf_counter()
                if ok:
                    self._prepared = {"x": float(stop[0]), "y": float(stop[1]),
                                      "at": done, "ms": float(ms), "seg": True,
                                      "tx": float(x), "ty": float(y)}
                    self._tail_s = tail_s
                    self._fire_deadline = now + budget
                    self._last_path = {
                        "used": True, "preheat": True, "segment": True,
                        "channel": self._mode, "points": len(points),
                        "ms": round(float(ms), 2), "tail_ms": round(tail_s * 1000.0, 2),
                        "tail_px": round(d_tail, 2), "at": time.time(),
                        "reason": ""}
                    return True

        # ---- 三档：瞬移兜底（明确记账，别当没发生） ----
        self._prepared = None
        self._tail_s = 0.0
        self._fire_deadline = None
        reason = (f"本次开火存在瞬移特征：提前量不足（剩余 {dist:.0f}px / 预算 "
                  f"{budget * 1000.0:.0f}ms 塞不下短移"
                  f"（≤{TAIL_MAX_PX:g}px、{SEGMENT_LEG_MIN_MS:g}+"
                  f"{TAIL_MIN_MS:g}~{TAIL_MAX_MS:g}ms），已计入 "
                  f"stats.trajectory.fire.degrade）") if budget is not None else \
            "本次开火存在瞬移特征：无提前量窗口，已计入 stats.trajectory.fire.degrade"
        self._last_path = {
            "used": False, "preheat": True, "channel": self._mode,
            "tail_jump": True, "degrade": True,
            "tail_budget_ms": TAIL_JUMP_BUDGET_MS,
            "degrade_px": round(dist, 1),
            "reason": reason}
        return False

    def _prepared_tail(self, x, y):
        """预热落点与目标只差一小段时，给出收尾短移方案（≤120px）。"""
        import math
        from .humanmouse import (PREHEAT_MAX_AGE_S, TAIL_MAX_MS, TAIL_MAX_PX,
                                 TAIL_MIN_MS, TAIL_SPEED_PX_S)
        p = getattr(self, "_prepared", None)
        if not p:
            return None
        try:
            age = time.perf_counter() - float(p["at"])
        except (TypeError, ValueError, KeyError):
            self._prepared = None
            return None
        if age > PREHEAT_MAX_AGE_S:
            self._prepared = None
            return None
        pos = _cursor_pos()
        if pos is None:
            return None
        cx = pos[0] - self._origin_left
        cy = pos[1] - self._origin_top
        dist = math.hypot(float(x) - cx, float(y) - cy)
        if dist < 2.0 or dist > TAIL_MAX_PX:
            return None
        reserve = float(getattr(self, "_tail_s", 0.0) or 0.0)
        need = max(TAIL_MIN_MS / 1000.0,
                   min(TAIL_MAX_MS / 1000.0, dist / TAIL_SPEED_PX_S))
        if reserve + 0.002 < need:
            return None
        return {"x": float(cx), "y": float(cy), "at": float(p["at"]),
                "tx": float(x), "ty": float(y), "seg": True, "tail": True,
                "ms": need, "tail_px": round(dist, 2)}

    def tail_reserve_s(self):
        """开火前预留的收尾窗口（秒）；调用方要从等待时间里扣掉。"""
        return max(0.0, float(getattr(self, "_tail_s", 0.0) or 0.0))

    def close_tail(self, x, y):
        """在 due 之前把「安全区 → 按钮」的余量走完，开火仍是零位移。

        倒计时路线不能靠绝对截止时刻对时（零点估计每秒都可能修订），
        所以由引擎在 due 前 tail_reserve_s 处调用；短移 8~20ms、≤120px，
        走完把预热落点改成按钮坐标，随后的开火点击直接命中缓存。
        """
        p = self._prepared_tail(x, y)
        if p is None:
            return False
        traj = getattr(self, "_traj", None)
        if traj is None:
            return False
        tp = traj.tail_path(x, y, start=(p["x"], p["y"]), ms=p["ms"] * 1000.0)
        if not tp:
            return False
        points, ms = self._split_path(tp)
        if not self.move_path(points, ms):
            return False
        self._prepared = {"x": float(x), "y": float(y), "at": time.perf_counter(),
                          "ms": float(ms), "seg": True,
                          "tx": float(x), "ty": float(y)}
        self._tail_s = 0.0
        return True

    def _wait_fire_deadline(self, lead_s=0.0):
        """睡到「按下时刻 - lead_s」，让收尾短移正好在开火点收尾。"""
        deadline = getattr(self, "_fire_deadline", None)
        if deadline is None:
            return
        self._fire_deadline = None
        self._fire_planned = deadline
        target = float(deadline) - max(0.0, float(lead_s or 0.0))
        while True:
            gap = target - time.perf_counter()
            if gap <= 0:
                return
            if gap > 0.004:
                time.sleep(min(gap - 0.002, 0.02))
            else:
                time.sleep(0)

    def _record_fire(self, t_down, t_up, prepared=None, jumped=False,
                     mode=None, first=False):
        """把开火阶段实测交给轨迹决策器（status().trajectory.fire 可读）。"""
        traj = getattr(self, "_traj", None)
        rec = getattr(traj, "record_fire", None)
        if rec is None or t_down is None or t_up is None:
            return
        gap = None
        if prepared is not None:
            try:
                gap = (t_down - float(prepared.get("at"))) * 1000.0
            except (TypeError, ValueError):
                gap = None
        lag = None
        planned = getattr(self, "_fire_planned", None)
        if planned is not None:
            lag = (t_down - float(planned)) * 1000.0
            self._fire_planned = None
        if mode == "segment":
            jumped = False
        try:
            rec(press_ms=(t_up - t_down) * 1000.0, gap_ms=gap,
                preheated=prepared is not None, jumped=bool(jumped),
                mode=mode, first=bool(first), lag_ms=lag)
        except Exception:                                    # noqa: BLE001
            pass

    def click(self, x, y, verify=None, expect_foreground=None, path=None,
              hold=None, fire=None):
        """移动 + 按下 + 抬起。默认走 Interception 驱动（2026-10-30 起）。

        为什么换掉「SetCursorPos + PostMessage」组合（旧做法，实测有效但慢）：
        驱动注入把事件从 kbdclass/mouclass 层送进输入栈，游戏读到的是
        **真实硬件事件**（无 LLMHF_INJECTED 标志，Raw Input 收得到），
        不需要动真实光标、也不依赖前台。每次 click 从约 0.13s 降到亚毫秒级。

        expect_foreground 仍然要查：驱动点击与前台无关是**对游戏而言**，
        但游戏收不到点击时的表现和旧路子一样是静默失败 —— 站错窗口照样
        白点。安全语义保留，速度语义换掉。

        verify=None 时取实例默认（默认 True）。三条通道各有自己的
        **每次点击后真实性校验**（见各 _click_* 实现），失败一律抛
        InputChannelError（带稳定 reason_code），不再"以为点成功了"。

        失败模式（都显式报错，不静默）：
        - 驱动没装/没重启 → 按 input_fallback 降级或直接失败（记账）
        - send 返回 0 → DRV_SEND_REJECTED（驱动装了但被卸了/杀软清了）
        - UIPI 不匹配 → 构造时即 UIPI_BLOCKED，不会走到点击

        `hold`：按下到抬起之间真实停留的秒数（连点参数 `click.burst.hold_ms`）。
        None = 各通道沿用原行为（legacy 60ms / 驱动亚毫秒 / message 0）。
        """
        if verify is None:
            verify = getattr(self, "_verify", True)
        self._ensure_click_state()

        # ---- perf 专项：点击入口时间戳（端到端起点） ----
        if self._perf_trace:
            self._stage = [("click_enter", time.perf_counter())]
            self._click_t0 = self._stage[0][1]

        # ---- feature:mouse.human BEGIN ----
        if fire is None:
            fire = bool(getattr(self, "_fire_hint", False))
        prepared = None
        mode = None
        first = False
        if fire:
            first = bool(getattr(self, "_fire_first", False))
            self._fire_first = False
        if path is None:
            if fire:
                # 开火路径绝不跑完整计时曲线：优先命中预热（零位移），
                # 其次用 8~20ms 收尾短移补齐安全区到按钮的余量，最后才是
                # 瞬时直线落点（明确记「瞬移特征」，不是无声降级）。
                prepared = self._prepared_hit(x, y)
                if prepared is not None:
                    mode = "zero"
                    self._last_path = {
                        "used": True, "channel": self._mode, "preheat": True,
                        "points": 0, "ms": 0.0, "at": time.time()}
                else:
                    prepared = self._prepared_tail(x, y)
                    if prepared is not None \
                            and getattr(self, "_fire_deadline", None) is None:
                        # 倒计时路线没有可信的绝对截止时刻：收尾短移必须在
                        # due 之前由 close_tail 走完。这里再动会把按下时刻
                        # 拖后 8~20ms —— 宁可退化成瞬移并记账，也不破坏卡点。
                        prepared = None
                    if prepared is not None:
                        traj = getattr(self, "_traj", None)
                        start = (prepared["x"], prepared["y"])
                        tp = traj.tail_path(x, y, start=start,
                                           ms=prepared["ms"] * 1000.0) \
                            if traj is not None else None
                        if tp:
                            path = tp
                            mode = "segment"
                            self._last_path = {
                                "used": True, "channel": self._mode,
                                "preheat": True, "tail": True,
                                "points": len(tp[0]),
                                "ms": round(float(tp[1]), 2),
                                "px": prepared.get("tail_px"),
                                "at": time.time()}
                        else:
                            prepared = None
                    if prepared is None or mode is None:
                        from .humanmouse import TAIL_JUMP_BUDGET_MS
                        mode = "straight"
                        self._last_path = {
                            "used": False, "channel": self._mode,
                            "preheat": False, "tail_jump": True,
                            "degrade": True,
                            "tail_budget_ms": TAIL_JUMP_BUDGET_MS,
                            "reason": "本次开火存在瞬移特征：预热落点与目标"
                                      "对不上且短移窗口不够，已计入 "
                                      "stats.trajectory.fire.degrade"}
            else:
                path = self._plan_path(x, y)
        # ---- feature:mouse.human END ----

        # message 通道先走，且**跳过前台校验**（review F2）。
        # 这个通道唯一的价值就是不碰前台：UIPI 下 PostMessage 仍能投递，
        # 而抢前台会让它在唯一有用的场景里直接失效（甚至先抛一次错）。
        # 不抢前台 ≠ 不告知：前台不匹配时把状态记进 _last_click，由上层展示。
        if self._mode == "message":
            # ---- feature:mouse.human BEGIN ----
            if path or fire:
                self._last_path = {
                    "used": False, "channel": "message",
                    "reason": "纯消息通道无真实移动，拟人轨迹不生效"}
            # ---- feature:mouse.human END ----
            return self._click_message(x, y, verify,
                                       expect_foreground=expect_foreground,
                                       hold=hold)

        # 只有需要「真实光标落在目标窗口上」的通道（legacy / interception）
        # 才校验前台：光标被别的窗口盖住时点了也白点。
        if expect_foreground:
            fg = self.foreground()
            if fg != expect_foreground:
                # 前台被抢走了 —— 最常见的原因就是**用户刚点了控制台上的
                # 「开始抢购」**，控制台窗口自己跑到前台，游戏被压下去。
                # 以前这里直接抛 WindowNotForeground，整场抢购就停在这一步
                # （日志里就是「目标窗口不在前台」+ 一秒后任务失败）。
                # 用户要的是「别停」，所以先抢一次；抢不回来才报错。
                if not self.ensure_foreground(expect_foreground):
                    raise WindowNotForeground(
                        f"目标窗口 {expect_foreground} 不在前台"
                        f"（当前前台 {self.foreground()}），抢不回来。"
                        f"点击会被系统投递给前台窗口，目标收不到。")

        if self._mode == "legacy":
            return self._click_messages(x, y, verify=verify, path=path,
                                        hold=hold, prepared=prepared,
                                        fire=fire, mode=mode, first=first)
        return self._click_driver(x, y, verify, path=path, hold=hold,
                                  prepared=prepared, fire=fire,
                                  mode=mode, first=first)

    def _click_driver(self, x, y, verify=True, path=None, hold=None,
                      prepared=None, fire=False, mode=None, first=False):
        """驱动通道：注入到**真实鼠标设备**，点完等 down/up 落地再还原光标。

        2026-11-07 重做，三个硬约束：
        1. 目标设备必须是枚举出来的真实鼠标（`mouse_device()` 按硬件 ID
           排除 VHF/ROOT 虚拟设备），不再用 11 号虚拟槽凑。
        2. 驱动 send 是异步的（实测 0.1~0.5ms 才进 Raw Input 栈），
           `send` 返回就直接 `SetCursorPos` 还原会被还在路上的注入覆盖，
           光标最后停在目标点 —— 也就是用户看到的「鼠标被抢」。所以还原
           必须等 down 和 up 两条 WM_INPUT 真正落地（DeliveryWatch 旁观）。
        3. 占用窗口（移动→还原完成）要记进 _last_click.occupancy_ms，
           p95 ≤1ms、最坏 ≤3ms 是验收线。
        """
        from .interception import (InterceptionError, RealDeviceUnavailable,
                                   MOUSE_LEFT_DOWN, MOUSE_LEFT_UP,
                                   MOUSE_MOVE_ABSOLUTE, MOUSE_VIRTUAL_DESKTOP,
                                   delivery_watch, shared)
        drv = shared()
        self._ensure_click_state()
        geom = self._refresh_geometry()
        marks = self._stage if self._perf_trace else None

        def mark(name):
            if marks is not None:
                marks.append((name, time.perf_counter()))

        mark("driver_enter")
        opt = self._opt_click
        cache = bool(opt.get("cache"))
        # drv.gen 是「设备句柄缓存仍然有效」的唯一凭据。假驱动/旧驱动可能
        # 根本没有这个字段：按「无代际信息」处理 —— 不缓存句柄、也不整批
        # 提交，退回逐条 send。快路径可以少走，但不能假设字段一定存在。
        has_gen = hasattr(drv, "gen")
        gen = getattr(drv, "gen", None)
        try:
            if cache and has_gen:
                # 设备句柄常驻：mouse_device() 每次都是 dict 拷贝 + 锁，
                # 而且枚举结果只会在热插拔/驱动重载时变。用代际 gen 失效。
                if self._ident is None or self._ident_gen != gen:
                    self._ident = drv.mouse_device()
                    self._ident_gen = gen
                ident = self._ident
            else:
                ident = drv.mouse_device()
        except RealDeviceUnavailable as e:
            self._ident = None
            raise InputChannelError(
                f"Interception 真实设备不可用：{e}", e.reason_code,
                channel="interception", detail=str(e)) from e

        mark("ident")
        if cache and self._abs_cache is not None \
                and self._abs_cache[0] == x and self._abs_cache[1] == y \
                and self._abs_cache[4] == geom:
            # 同一目标点 + 同一几何：换算结果完全一样，没必要每击重算。
            # 缓存带走 origin/vx/vy/vw/vh，窗口移动或改分辨率后自然失效，
            # 不会再用旧屏幕的换算值绕过 `_abs` 的越界检查。
            dx, dy = self._abs_cache[2], self._abs_cache[3]
        else:
            dx, dy = self._abs(x, y)
            if cache:
                self._abs_cache = (x, y, dx, dy, geom)
        mark("abs")
        flags = MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP
        sx, sy = self._origin_left + x, self._origin_top + y
        self._settle_home_pending()      # 上一击没确认的还原，先收口
        origin = _cursor_pos()
        if origin == (sx, sy) and self._home is not None \
                and self._home != origin:
            # 读到目标点：只可能是我们自己刚注入的瞬态（设备 FIFO 里还有
            # down/up，回原点 stroke 还没落地）。**一律沿用缓存的原点**，
            # 不看 pending 也不看「过了几毫秒」—— 连点时旧逻辑会把目标点
            # 认成用户的原位置，`_home` 被污染后逐击漂移，最后一击
            # need_restore=False，光标就永久停在目标上（实测 50 连点净位移
            # 717~980px，就是这个）。用户真把鼠标挪到恰好这一点属于可忽略的
            # 概率，下一击读到别的位置就会刷新 `_home`。
            origin = self._home
            self._home_pending = True
        elif self._home_pending:
            if origin == self._home:
                self._home_pending = False
            else:
                self._home_pending = False     # 用户真的动了鼠标
        mark("origin")
        # ---- feature:mouse.human BEGIN ----
        # 必须在 origin 取好之后再走轨迹：走完再还原，才不会把光标留在按钮上。
        # 开火可能带一段 8~20ms 收尾短移（分段预热），零位移时 path 为 None。
        if path:
            self._emit_plan(path)
        # 短移先走完，再对时到计划按下时刻：即使短移执行慢了，也不会把
        # 按下时刻往后拖（等待会把误差吃掉）。
        if fire:
            self._wait_fire_deadline(0.0)
        # ---- feature:mouse.human END ----
        mark("plan")
        if verify:
            if cache and self._watch is not None:
                watch = self._watch
            else:
                watch = delivery_watch()
                if cache:
                    self._watch = watch
            if watch is not None:
                watch.reset()
        else:
            watch = None
        mark("watch")
        t0 = time.perf_counter()
        t_down = t_up = None
        # 批处理只对「不按住」的快路径成立：hold 必须真的在 down/up 之间停住，
        # 那段的语义是时间间隔本身，没法合并。能力判定走**显式契约**
        # （drv.supports_batch + 代际 gen），不靠 hasattr 的偶然性 —— 驱动
        # 对象被代理/façade/测试替身包过时方法会藏起来，若只看方法存在性
        # 就会静默退回逐条发送，性能掉一档却查不到原因。
        batch_cap = bool(getattr(drv, "supports_batch", False))
        use_batch = bool(opt.get("batch")) and not hold and has_gen and batch_cap
        if not use_batch and bool(opt.get("batch")) and not hold \
                and not self._batch_warn_once:
            self._batch_warn_once = True
            if not has_gen:
                why = "驱动对象没有代际信息 gen，无法确认设备句柄有效"
            elif not batch_cap:
                why = ("驱动对象没有声明 supports_batch 能力位"
                       "（被代理/包装过，或不是 Interception 驱动）")
            else:
                why = f"点击参数关闭了 batch（opt={opt.get('batch')!r}）"
            _LOG.warning("Interception 整批提交不可用，退回逐条 send：%s；"
                         "本次点击占用会变高，但功能不受影响", why)
        # 回原点走驱动 stroke：和 down/up 同一批提交，FIFO 保证最后生效的是
        # 原点。比 SetCursorPos 更确定（不会被子设备延迟的位置更新覆盖）。
        # hold/逐条路径也一样：UP 之后再单发一条回原点 stroke —— 30ms hold
        # 下实测 SetCursorPos 会被仍在队列里的 UP 位置更新覆盖（0.6% 停在
        # 偏移位置）。目标点与原点重合时不必多此一举。
        need_restore = origin is not None and (sx, sy) != origin
        ox = oy = None
        if origin is not None and opt.get("restore_drv") and need_restore:
            ox, oy = self._abs(origin[0] - self._origin_left,
                               origin[1] - self._origin_top)
        use_drv_restore = bool(use_batch and opt.get("restore_drv")
                               and ox is not None)
        # 逐条路径只在「真的要按住」时补回原点 stroke：这正是 30ms hold 下
        # SetCursorPos 被队列里的 UP 位置更新覆盖的场景。普通无 hold 的
        # 逐条路径（旧驱动/假驱动）保持原来的 3 条事件不变。
        use_send_restore = bool(hold and not use_batch and ox is not None
                                and getattr(drv, "send_mouse", None) is not None)
        # 等回原点 stroke 的 Raw Input move 事件落地，而不是盲等固定时长。
        wait_move = bool(use_drv_restore and opt.get("restore_wait_move"))
        pressed = False
        try:
            if use_batch:
                noabs = bool(opt.get("btn_noabs"))
                _dn = ((MOUSE_LEFT_DOWN, 0, 0, 0) if noabs
                       else (MOUSE_LEFT_DOWN, dx, dy, flags))
                _up = ((MOUSE_LEFT_UP, 0, 0, 0) if noabs
                       else (MOUSE_LEFT_UP, dx, dy, flags))
                strokes = [] if opt.get("drop_move") else [(0, dx, dy, flags)]
                strokes.append(_dn)
                strokes.append(_up)
                if use_drv_restore:
                    strokes.append((0, ox, oy, flags))
                pressed = True
                try:
                    drv.send_mouse_batch(strokes, device=ident["device"])
                except InterceptionError as e:
                    if getattr(e, "sent", 0) != 0 \
                            or getattr(drv, "send_mouse", None) is None:
                        raise
                    # 0 条都没进队列（句柄大概率失效：拔插/重载）：重枚举一次
                    # 再逐条重发，别把一个已经证明失效的设备号当成最终结论。
                    self._ident = None
                    self._ident_gen = -1
                    ident = drv.mouse_device(force=True)
                    for _st, _sx, _sy, _fl in strokes:
                        drv.send_mouse(_st, x=_sx, y=_sy, flags=_fl,
                                       device=ident["device"])
                    mark("send_batch_retry")
                else:
                    mark("send_batch")
                t_down = t_up = time.perf_counter()
                pressed = False
            else:
                drv.send_mouse(0, x=dx, y=dy, flags=flags, device=ident["device"])
                mark("send_move")
                t_down = time.perf_counter()
                drv.send_mouse(MOUSE_LEFT_DOWN, x=dx, y=dy, flags=flags,
                               device=ident["device"])
                mark("send_down")
                pressed = True
                # 连点参数要求「按住 hold_ms」真的发生：这里就是 down→up 之间。
                if hold:
                    time.sleep(float(hold))
                drv.send_mouse(MOUSE_LEFT_UP, x=dx, y=dy, flags=flags,
                               device=ident["device"])
                mark("send_up")
                t_up = time.perf_counter()
                pressed = False
                if use_send_restore:
                    # 逐条路径的回原点：必须是最后一条位置事件，才能压掉
                    # 仍在队列里的 UP 位置更新覆盖（hold 越大越明显）。
                    drv.send_mouse(0, x=ox, y=oy, flags=flags,
                                   device=ident["device"])
                    mark("send_restore")
        except BaseException as e:
            # 含 StopSignal / KeyboardInterrupt：先把光标送回原点再往外抛，
            # 否则用户按了停止键，光标就永久停在点击点。
            if pressed:
                try:
                    drv.send_mouse(MOUSE_LEFT_UP, x=dx, y=dy, flags=flags,
                                   device=ident["device"])
                except BaseException:                        # noqa: BLE001
                    pass
            try:
                self._force_restore(drv, ident, origin, (sx, sy), flags)
            except BaseException:                            # noqa: BLE001
                pass
            if isinstance(e, InterceptionError):
                raise InputChannelError(
                    f"Interception 驱动点击失败：{e}", R_DRIVER_SEND_REJECTED,
                    channel="interception", detail=str(e)) from e
            raise

        landing = {"ok": None, "mode": "unverified"}
        landing_failed = False
        try:
            if watch is not None:
                # 批内既有 move 又有回原点 stroke 时，要等两条 move 事件；
                # drop_move 只剩回原点那一条。
                _want_moves = (2 if not opt.get("drop_move") else 1) \
                    if wait_move else 0
                landing = dict(watch.wait_buttons(
                    vid=ident.get("vid"), pid=ident.get("pid"),
                    timeout_ms=3.0, want_moves=_want_moves))
                mark("land")
                landing["mode"] = "raw_input"
                if landing.get("hdev") is None and not landing.get("down") \
                        and not landing.get("up") and not landing.get("moves"):
                    # 观测器根本没绑定到 HID 设备（真机：hdev=None，本进程
                    # 收不到任何 WM_INPUT）。此时 ok=False 是「观测不到」，
                    # 不是「点击没落地」——按未验证处理。
                    # 不改的话 verify=True 会把每一次点击都判失败，等于
                    # 永远开不了火（真机踩过：nav 点击全被这条拦下）。
                    landing["ok"] = None
                    landing["mode"] = "raw_input_blind"
                    landing["reason"] = "观测器未绑定设备（hdev=None），落地不可观测"
                if landing.get("ok") and not use_drv_restore:
                    # 驱动按设备 FIFO 投递：down/up 到了说明移动早已落地。
                    # 仍回读一次，防「事件到了但光标被 ClipCursor 卡住」。
                    # 回原点 stroke 也在同一批里时跳过：它会把光标拉回原点，
                    # 这里的「到达目标点」检查只会白转 2ms。
                    now = time.perf_counter()
                    while (sx, sy) != (_cursor_pos() or (sx, sy)) \
                            and time.perf_counter() - now < 0.002:
                        time.sleep(0)
                mark("cursor_ok")
            else:
                # 没有观测窗口（极少数环境创建失败）：退化成「等光标到位 +
                # 保守再等 0.6ms」，并在 last_click 里标 unverified，
                # 不假装验证过。
                deadline = time.perf_counter() + 0.003
                while _cursor_pos() != (sx, sy) \
                        and time.perf_counter() < deadline:
                    time.sleep(0)
                time.sleep(0.0006)
        except Exception as e:                           # noqa: BLE001
            landing["error"] = f"{type(e).__name__}: {e}"
            landing_failed = True
        except BaseException:
            # StopSignal / KeyboardInterrupt：先还原再抛
            landing_failed = True
            raise
        finally:
            if landing_failed and origin is not None:
                try:
                    self._force_restore(drv, ident, origin, (sx, sy), flags)
                except BaseException:                        # noqa: BLE001
                    pass

        # 还原光标：这一步必须在 down/up 落地之后，否则会被注入覆盖。
        # 主路径不再补 SetCursorPos：回原点 stroke 已是最后一条位置事件，
        # 这里只回读一次确认；未命中的「桌面还没更新」交给
        # _settle_home_pending 在状态查询/下一击时收口 —— 既不占主路径，
        # 也不会把未还原状态无限挂着。
        restored = None
        restore_done = False
        try:
            if origin is not None:
                if use_drv_restore or use_send_restore:
                    if opt.get("restore_early"):
                        restored = (_cursor_pos() == origin) or None
                    else:
                        end = time.perf_counter() + 0.004
                        while _cursor_pos() != origin \
                                and time.perf_counter() < end:
                            time.sleep(0)
                        restored = (_cursor_pos() == origin) or None
                    mark("restore_drv")
                    if restored is None:
                        # 回原点 stroke 没在 4ms 窗口里体现：**立刻**补一次
                        # SetCursorPos 硬还原，不能只挂 _home_pending。
                        # 真机踩过：一次性点击的短进程没有下一次状态查询，
                        # _settle_home_pending 永远不跑，系统光标就永久停在
                        # 点击点（实测停在 (581,50)）—— 这是产品约束不允许的。
                        if self._cursor_reachable(origin):
                            self._emergency_cursor_restore(origin)
                            restored = (_cursor_pos() == origin) or None
                        self._home_deadline = time.perf_counter() + 0.02
                    self._home = origin
                    self._home_target = (sx, sy)
                    self._home_target_ts = time.perf_counter()
                    self._home_pending = restored is None
                elif self._cursor_reachable(origin):
                    # 没有驱动回原点且未被裁剪：只做「还原用户原位置」，
                    # 不参与点击定位。
                    self._emergency_cursor_restore(origin)
                    mark("restore")
                    end = time.perf_counter() + 0.002
                    while _cursor_pos() != origin \
                            and time.perf_counter() < end:
                        time.sleep(0)
                    restored = _cursor_pos() == origin
                    self._home = origin
                    self._home_target = (sx, sy)
                    self._home_target_ts = time.perf_counter()
                    self._home_pending = not restored
                    if self._home_pending:
                        self._home_deadline = time.perf_counter() + 0.02
                else:
                    # 被 ClipCursor 裁掉：光标本来就不可能回原点，先记 pending，
                    # 裁剪解除后由 settle 收口（SetCursorPos 现在只会被夹住）。
                    self._home = origin
                    self._home_target = (sx, sy)
                    self._home_target_ts = time.perf_counter()
                    self._home_pending = True
                    self._home_deadline = time.perf_counter() + 0.05
                mark("restore_ok")
            restore_done = True
        finally:
            if not restore_done and origin is not None:
                # 还原分支内部抛错（含 StopSignal/KeyboardInterrupt）：硬还原，
                # 不能把光标留在点击点。
                try:
                    self._force_restore(drv, ident, origin, (sx, sy), flags)
                except BaseException:                        # noqa: BLE001
                    pass

        occupancy_ms = (time.perf_counter() - t0) * 1000.0
        mark("occ")
        # hold 是用户要的「按住时长」，不算注入占用：占用指标只记
        # 「移动→down/up 落地→还原」这一段，否则 p95 ≤1ms 的验收线会被
        # 按住时长污染。
        if hold:
            occupancy_ms = max(0.0, occupancy_ms - float(hold) * 1000.0)
        self._record_occupancy(occupancy_ms)
        # ---- feature:mouse.human BEGIN ----
        if fire:
            if mode is None:
                mode = "segment" if (prepared is not None
                                     and prepared.get("tail")) else \
                    ("zero" if prepared is not None else "straight")
            self._record_fire(t_down, t_up, prepared=prepared,
                              jumped=(mode == "straight"),
                              mode=mode, first=first)
        # ---- feature:mouse.human END ----
        self._last_click = {
            "channel": "interception",
            "identity": "real_device" if not ident.get("virtual") else "virtual_device",
            "device": ident["device"],
            "hardware_id": (ident.get("hardware_id") or "")[:140],
            "virtual": bool(ident.get("virtual")),
            "device_source": ident.get("source"),
            "accepted": True,
            "landing": landing,
            "occupancy_ms": round(occupancy_ms, 3),
            "restored": restored,
            "origin": origin,
            "restore_queued": bool(self._home_pending),
            "at": time.time(),
        }
        mark("record")
        if marks is not None:
            self._last_click["stages"] = marks
        if verify and landing.get("ok") is False:
            raise InputChannelError(
                "Interception 已受理但 down/up 未在 3ms 内落地，"
                "无法确认点击生效（不当作成功）",
                R_DRIVER_SEND_REJECTED, channel="interception",
                detail=f"landing={landing}")
        return True

    def _click_messages(self, x, y, verify=True, path=None, hold=None,
                        prepared=None, fire=False, mode=None, first=False):
        """legacy 通道：SetCursorPos 借光标 + PostMessage。

        游戏必须用「SetCursorPos + PostMessage」组合，不能只发消息：
        它按下时读「当前真实光标位置」（docs/auto-buy-design.md §4.8）。

        **每一步都查返回值 + 每次点击后回读光标**：SetCursorPos 返回 0 且
        GetLastError=0 是 UIPI/ClipCursor 的典型静默失败（实测：光标纹丝
        不动、没有任何错误可读），必须自己读回坐标判定，不能只看返回值。
        光标位置在 finally 里还回去。失败一律带稳定 reason_code。
        """
        sx, sy = self._origin_left + x, self._origin_top + y
        origin = _cursor_pos()
        # ---- feature:mouse.human BEGIN ----
        if path:
            self._emit_plan(path)
        if fire:
            self._wait_fire_deadline(0.0)
        # ---- feature:mouse.human END ----
        hwnd = None
        lp = 0
        pressed = False
        t_down = t_up = None
        try:
            ctypes.set_last_error(0)
            if not self._legacy_place_cursor(sx, sy):
                err = ctypes.get_last_error()
                raise InputChannelError(
                    f"SetCursorPos({sx},{sy}) 返回 0，err={err}"
                    + ("（0=系统没给原因，典型是 UIPI/ClipCursor，光标会纹丝不动）"
                       if err == 0 else ""),
                    R_CURSOR_NOT_MOVED, channel="legacy")
            # ---- feature:mouse.human BEGIN ----
            # 预热命中：光标早就在目标上，不再等 40ms 让光标「飞过去」
            # ---- feature:mouse.human END ----
            if prepared is None:
                time.sleep(0.04)

            # 低成本真实性校验 ①：光标真的到目标了吗（不等消息、立刻回读）
            pos = _cursor_pos()
            if pos is not None and (abs(pos[0] - sx) > 2 or abs(pos[1] - sy) > 2):
                raise InputChannelError(
                    f"SetCursorPos 报成功但光标没动（想在 {sx},{sy}，实际 "
                    f"{pos[0]},{pos[1]}）—— 被 UIPI/裁剪锁拦下，或有人动鼠标",
                    R_CURSOR_NOT_AT_TARGET, channel="legacy")

            hwnd = self._game_hwnd or self.foreground()
            if not hwnd:
                raise InputChannelError("没有目标窗口句柄，无法投递点击",
                                        R_NO_TARGET_HWND, channel="legacy")
            pt = wintypes.POINT(sx, sy)
            _user32.ScreenToClient(hwnd, ctypes.byref(pt))
            lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
            # (消息, wParam, 按下后的等待)
            # 按住时长：None = 沿用原 60ms；连点参数传入的秒数优先，
            # 这样 hold_ms 在这个通道也是真的生效，而不是写死。
            down_hold = 0.06 if hold is None else max(0.0, float(hold))
            # ---- feature:mouse.human BEGIN ----
            move_wait = 0.0 if prepared is not None else 0.02
            # ---- feature:mouse.human END ----
            steps = ((_WM_MOUSEMOVE, 0, move_wait),
                     (_WM_LBUTTONDOWN, _MK_LBUTTON, down_hold),
                     (_WM_LBUTTONUP, 0, 0.0))
            for msg, wp, wait_s in steps:
                ctypes.set_last_error(0)
                if msg == _WM_LBUTTONDOWN:
                    t_down = time.perf_counter()
                if not _user32.PostMessageW(hwnd, msg, wp, lp):
                    err = ctypes.get_last_error()
                    code = (R_UIPI_BLOCKED if err == 5 else R_POST_REJECTED)
                    raise InputChannelError(
                        f"PostMessageW(0x{msg:04X}) 被拒 err={err}：窗口 {hwnd} "
                        f"收不到点击消息（已退出或被 UIPI 拦下）",
                        code, channel="legacy")
                if msg == _WM_LBUTTONUP:
                    t_up = time.perf_counter()
                pressed = (msg == _WM_LBUTTONDOWN)
                if wait_s:
                    time.sleep(wait_s)

            if verify:
                # HEAD 这里固定 sleep(0.05) 再回读一次；去掉固定睡以后不能
                # 改成立刻读（会误判未到位）。改由 _verify_cursor_at 有界
                # 轮询：到位即返回，50ms 上界兜底。
                self._verify_cursor_at(sx, sy, "legacy")
            self._last_click = {
                "channel": "legacy", "accepted": True, "at": time.time(),
                "cursor": _cursor_pos(),
            }
            # ---- feature:mouse.human BEGIN ----
            if fire:
                if mode is None:
                    mode = "segment" if (prepared is not None
                                         and prepared.get("tail")) else \
                        ("zero" if prepared is not None else "straight")
                self._record_fire(t_down, t_up, prepared=prepared,
                                  jumped=(mode == "straight"),
                                  mode=mode, first=first)
            # ---- feature:mouse.human END ----
            return True
        finally:
            # 按下之后任何异常都要补一条抬起，别把目标窗口留在「按住」状态。
            if pressed and hwnd:
                try:
                    _user32.PostMessageW(hwnd, _WM_LBUTTONUP, 0, lp)
                except Exception:                             # noqa: BLE001
                    pass
            # 光标是用户的东西：点完放回原处，别把用户的鼠标丢在游戏里。
            if origin is not None:
                self._emergency_cursor_restore(origin)

    def _verify_cursor_at(self, sx, sy, channel, tolerance=6,
                          budget=None, poll=None):
        """回读光标：**有界轮询**到目标点，到位立刻放行，超时才判失败。

        HEAD 版这里是 `time.sleep(0.05)` 后只读一次（鼠标飞过去靠固定等待）；
        改成立刻读会有竞态：SetCursorPos / 合成输入落地不是瞬时的（要过消息
        泵，实机还有 DPI 缩放与多屏换算），早一帧就能读到旧坐标，被判成
        CURSOR_NOT_AT_TARGET → 上层按点击失败重试或中止。为了省 50ms 制造
        假失败不划算。所以两头都避开：每 `poll` 秒读一次，到位立即返回
        （快路径通常 0 次等待，比原来的 50ms 更快），`budget` 到点仍不到位
        才报错，且原因里带实际等待毫秒数，方便现场归因。

        返回 {"ok": True, "waited_ms", "polls", "cursor"}；读不到光标
        （无桌面 / 只实现了 SetCursorPos 的桩）按原行为放行，ok=None；真等到
        上界仍不到位才抛 InputChannelError(R_CURSOR_NOT_AT_TARGET)。
        """
        budget = (_CURSOR_VERIFY_BUDGET_S if budget is None
                  else max(0.0, float(budget)))
        step = _CURSOR_POLL_S if poll is None else max(0.0, float(poll))
        t0 = time.perf_counter()
        polls = 0
        last = None
        while True:
            pos = _cursor_pos()
            polls += 1
            if pos is None:
                return {"ok": None, "waited_ms": (time.perf_counter() - t0) * 1000.0,
                        "polls": polls, "cursor": None}
            last = pos
            if abs(pos[0] - sx) <= tolerance and abs(pos[1] - sy) <= tolerance:
                return {"ok": True,
                        "waited_ms": (time.perf_counter() - t0) * 1000.0,
                        "polls": polls, "cursor": last}
            waited_ms = (time.perf_counter() - t0) * 1000.0
            if waited_ms >= budget * 1000.0:
                break
            time.sleep(step)
        waited_ms = (time.perf_counter() - t0) * 1000.0
        raise InputChannelError(
            f"等待 {waited_ms:.1f}ms（上界 {budget * 1000.0:.0f}ms）光标仍未"
            f"到位：想在 {sx},{sy}（容差 ±{tolerance}px），{polls} 次回读最后"
            f"停在 {last[0]},{last[1]} —— 有别的程序或人在动鼠标，或输入被系统丢弃",
            R_CURSOR_NOT_AT_TARGET, channel=channel)

    def _click_message(self, x, y, verify=True, expect_foreground=None,
                       hold=None):
        """message 通道：纯投递消息，全程不碰系统光标。

        适用：目标窗口**按窗口消息**处理点击（普通 Win32 控件/自绘界面）。
        不适用：目标在按下时读真实光标位置 / 走 Raw Input —— 游戏就是这种，
        消息能进队列但不会进入游戏逻辑（branchA 实测 200/200 受理、游戏零响应）。

        I2（假成交）：返回值语义已收紧 —— `accepted`（消息被目标窗口过程
        执行）与 `processed`（目标确实处理出效果）分开记账；拿不到
        「已处理」证据时 `processed=None`、本函数返回 False，只在
        `_last_click` 里留 unknown，绝不把「投递成功」当成「点成功」。

        `expect_foreground` 只用于**观测**，不在这里抢前台（review F2）：
        前台不匹配时 `_last_click.foreground_ok=False`，上层据此提示用户。
        """
        foreground_ok = None
        if expect_foreground:
            foreground_ok = self.foreground() == expect_foreground
        hwnd = self._game_hwnd or self.foreground()
        if not hwnd:
            raise InputChannelError("纯消息通道需要目标窗口句柄",
                                    R_NO_TARGET_HWND, channel="message")
        from .nocursor import Clicker, InputError

        sx, sy = self._origin_left + x, self._origin_top + y
        if self._clicker is None or self._clicker.hwnd != hwnd:
            self._clicker = Clicker(hwnd, "message", verify=True)
        else:
            self._clicker.verify = True
        try:
            # None = 沿用原 0（不额外按住）；连点参数传入的秒数在这里生效。
            accepted = self._clicker.click(
                sx, sy, hold=0.0 if hold is None else max(0.0, float(hold)))
        except InputError as e:
            last = dict(getattr(self._clicker, "last_result", None) or {})
            self._last_click = {
                "channel": "message", "accepted": bool(last.get("accepted")),
                "processed": last.get("processed"),
                "success": False,
                "reason_code": getattr(e, "reason_code", R_MESSAGE_NOT_ACCEPTED),
                "evidence": last.get("evidence"),
                "functional": (self._message_cap or {}).get("functional",
                                                            "unknown"),
                "foreground_ok": foreground_ok,
                "at": time.time(),
            }
            raise InputChannelError(
                f"纯消息点击失败：{e}",
                getattr(e, "reason_code", R_MESSAGE_NOT_ACCEPTED),
                channel="message", detail=str(e)) from e

        res = dict(getattr(self._clicker, "last_result", None) or {})
        processed = res.get("processed")          # True / False / None(unknown)
        cap = self._message_cap or message_capability(hwnd)
        self._last_click = {
            "channel": "message", "accepted": bool(res.get("accepted", accepted)),
            "processed": processed,
            "success": processed is True,
            # unknown 不是成功：上层据此降级/重试，而不是记成一次成交
            "retry_suggested": processed is None,
            "evidence": res.get("evidence"),
            "reason_code": res.get("reason_code"),
            "changed": res.get("changed"),
            "latency_ms": res.get("latency_ms"),
            "functional": cap.get("functional", "unknown"),
            "window_class": cap.get("window_class", ""),
            "foreground_ok": foreground_ok,
            "at": time.time(),
        }
        # 只有拿到「已处理」证据才算成功（I2）
        return processed is True

    @staticmethod
    def ensure_foreground(hwnd, tries=3, gap=0.25, screen=None):
        """把目标窗口抢回前台，成功返回 True。

        `SetForegroundWindow` 受 Windows 前台锁限制：调用方不是前台进程时
        直接失败（返回 0，窗口不动），提权也绕不过去。`AttachThreadInput`
        把自己的输入队列挂到当前前台线程上，再调就生效 —— 真机上只有这个
        做法稳定（纯 ShowWindow + BringWindowToTop 实测无效）。

        抢不回来不要硬点：`click(expect_foreground=)` 会拦下，点击不会投递
        到别的窗口去。

        screen：给了就在**真的切过窗口**之后等画面换过来再返回。合成器要
        一两帧才把游戏画面贴上来，这期间抓到的是上一个窗口的内容（实测把
        游戏标题读成了窗口标题）。本来就在前台就不等 —— 否则每次白等 1 秒。
        """
        if not hwnd:
            return False
        before = None
        if screen is not None:
            try:
                before = screen.grab()      # 切窗前的画面，用来判断「换过来了没」
            except Exception:               # noqa: BLE001
                before = None
        switched = False
        for _ in range(max(1, tries)):
            if _user32.GetForegroundWindow() == hwnd:
                break
            fg = _user32.GetForegroundWindow()
            tid_fg = _user32.GetWindowThreadProcessId(fg, None)
            tid_me = _kernel32.GetCurrentThreadId()
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(_user32.AttachThreadInput(tid_me, tid_fg, True))
            try:
                _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
                _user32.BringWindowToTop(hwnd)
                _user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    _user32.AttachThreadInput(tid_me, tid_fg, False)
            switched = True
            time.sleep(gap)
            if _user32.GetForegroundWindow() == hwnd:
                break
        else:
            return False
        if _user32.GetForegroundWindow() != hwnd:
            return False
        if switched and screen is not None:
            # 真切过窗口才等画面：合成器贴新画面要一两帧，这期间抓到的
            # 是上一个窗口的内容。本来就在前台就不等，省 1 秒。
            try:
                from .screen import wait_frame_change
                wait_frame_change(before, screen.grab)
            except Exception:                   # noqa: BLE001
                pass
        return True

    @staticmethod
    def foreground():
        """当前前台窗口句柄。"""
        return _user32.GetForegroundWindow()

    @staticmethod
    def cursor():
        p = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    @staticmethod
    def release_cursor_lock():
        """**已停用**：不再调用 ClipCursor 改写用户的光标裁剪区。

        旧实现用 `ClipCursor(None)` 清掉游戏独占全屏留下的裁剪锁。它确实能
        让 SetCursorPos 恢复，但那是**全局改写用户桌面状态**：裁剪区是用户
        与其他程序共用的，和「不碰本机鼠标」的要求直接冲突。驱动注入不依赖
        光标可达性，裁剪锁不会再让点击失效，所以这个动作整体移除。

        返回值保持 (ok, had) 二元组以兼容调用方；恒为 (True, False) =
        「没有做任何清锁动作」。检测到裁剪区时只写账本，不改写。
        """
        before = wintypes.RECT()
        get_clip = getattr(_user32, "GetClipCursor", None)
        had = bool(get_clip(ctypes.byref(before))) if get_clip is not None else False
        if had:
            LEDGER.record({
                "stage": "cursor_lock", "from": None, "to": None,
                "reason_code": None,
                "reason": "检测到鼠标裁剪区；本版本不改写 ClipCursor（不影响"
                          "驱动注入），也不移动光标",
                "policy": None, "ok": True, "severity": "info"})
        return True, False

    # 已移除 cursor_is_locked()：旧实现靠「先挪 40px 再挪回」来判断光标是否
    # 被裁剪锁钉住，每次启动自检都会真实移动用户的鼠标。release_cursor_lock()
    # 也已停用写操作（只读 GetClipCursor 记账，不改写裁剪区、不动光标）。
    # 裁剪锁不会再阻断驱动注入；真出问题只能靠点击失败时的报错链发现。


# 扫描码表：游戏读的是硬件扫描码，不是虚拟键码
SCAN = {
    "ESC": 0x01, "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E,
    "F5": 0x3F, "F6": 0x40, "F7": 0x41, "F8": 0x42, "F9": 0x43,
    "F10": 0x44, "F11": 0x57, "F12": 0x58,
    "TAB": 0x0F, "ENTER": 0x1C, "SPACE": 0x39, "BACKSPACE": 0x0E,
    "UP": 0x48, "DOWN": 0x50, "LEFT": 0x4B, "RIGHT": 0x4D,
    "HOME": 0x47, "END": 0x4F, "PAGEUP": 0x49, "PAGEDOWN": 0x51,
    "INSERT": 0x52, "DELETE": 0x53,
    "A": 0x1E, "B": 0x30, "C": 0x2E, "D": 0x20, "E": 0x12, "F": 0x21,
    "G": 0x22, "H": 0x23, "I": 0x17, "J": 0x24, "K": 0x25, "L": 0x26,
    "M": 0x32, "N": 0x31, "O": 0x18, "P": 0x19, "Q": 0x10, "R": 0x13,
    "S": 0x1F, "T": 0x14, "U": 0x16, "V": 0x2F, "W": 0x11, "X": 0x2D,
    "Y": 0x15, "Z": 0x2C,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
}

# 虚拟键码：PostMessage 的 wParam 用这个（_click_messages 兜底路径仍在用）
VK = {
    "ESC": 0x1B, "TAB": 0x09, "ENTER": 0x0D, "SPACE": 0x20, "BACKSPACE": 0x08,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "INSERT": 0x2D, "DELETE": 0x2E,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,
}
# 字母和数字也必须补齐：legacy 路径拿 VK 当 wParam，表里查不到就是 None，
# PostMessageW(hwnd, WM_KEYDOWN, None, ...) 会被按 0 处理 —— 打字和字母
# 快捷键全部静默失效（扫描码表里有，容易漏掉这一张）。
VK.update({chr(c): c for c in range(0x41, 0x5B)})     # A-Z
VK.update({chr(c): c for c in range(0x30, 0x3A)})     # 0-9

# 扩展键（键盘右侧小键盘区/方向键区）：Interception 驱动注入要带 KEY_E0
# 位，PostMessage 兜底路径要 bit24 扩展标志。表里没有的键都不是扩展键。
_EXTENDED = {
    "UP": True, "DOWN": True, "LEFT": True, "RIGHT": True,
    "HOME": True, "END": True, "PAGEUP": True, "PAGEDOWN": True,
    "INSERT": True, "DELETE": True,
}

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
_WM_RBUTTONDOWN, _WM_RBUTTONUP = 0x0204, 0x0205


class Keyboard:
    """给游戏窗口发按键，走 Interception 驱动（2026-10-30 起）。

    旧路子踩过两个坑，都实测过（docs/auto-buy-design.md §4.8）：

    1. **SendInput 对游戏无效。** 游戏读 Raw Input，只认真实硬件事件；
       SendInput 照返回成功、系统光标照动，游戏一点反应没有。
    2. **PostMessage 必须带扫描码**，wParam 用 VK —— 有效但只是
       「窗口消息」层面的有效，跟驱动注入比是两个层次。

    驱动注入直接从 kbdclass 层发**真实硬件事件**（扫描码原样送达），
    上面两个坑都不存在了：不需要窗口句柄、不依赖前台、没有注入标志。
    """

    def __init__(self, hwnd=None, mode="interception"):
        # hwnd 参数保留：调用方（engine/nav）都传，但驱动注入用不到它。
        self.hwnd = hwnd
        # 键盘和鼠标共用同一套通道决策：驱动不可用时按 input_fallback
        # 降级（或 fail），并写账本 —— 不许悄悄换通道。
        decision = _decide_channel(mode, None, None)
        self._requested_mode = decision["requested"]
        self._mode = decision["effective"]
        self._drv_warn = decision.get("driver_warn")
        self._reason_code = decision.get("reason_code")
        self._reason = decision.get("reason", "")
        self._fallback_policy = decision.get("policy")

    @property
    def mode(self):
        return self._mode

    def channel_status(self):
        """键盘此刻实际使用的通道（与 Mouse.channel_status 同构）。"""
        return {
            "requested": self._requested_mode,
            "effective": self._mode,
            "degraded": self._mode != self._requested_mode,
            "reason_code": self._reason_code,
            "reason": self._reason,
            "fallback_policy": self._fallback_policy,
            "events": LEDGER.events(),
        }

    def _stroke(self, scan, up, e0):
        from .interception import shared
        shared().send_key(scan, up=up, e0=e0)

    def _tap_messages(self, key, hold):
        """旧 PostMessage 路径（legacy 模式）。"""
        scan = SCAN.get(key.upper()) if isinstance(key, str) else key
        vk = VK.get(key.upper()) if isinstance(key, str) else scan
        ext = _EXTENDED.get(key.upper(), False) if isinstance(key, str) else False
        lp = self._lparam(scan, up=False, extended=ext)
        ok1 = bool(_user32.PostMessageW(self.hwnd, WM_KEYDOWN, vk, lp))
        time.sleep(hold)
        lp = self._lparam(scan, up=True, extended=ext)
        ok2 = bool(_user32.PostMessageW(self.hwnd, WM_KEYUP, vk, lp))
        return ok1 and ok2

    def _lparam(self, scan, up=False, extended=False):
        """WM_KEYDOWN/WM_KEYUP 的 lParam 位布局（legacy 路径用）。

        bits 0-15  重复次数 = 1
        bits 16-23 扫描码
        bit  24    扩展键标志
        bits 30-31 WM_KEYUP 时置 1（先前状态 / 转换状态）
        """
        v = 1 | (scan << 16)
        if extended:
            v |= 1 << 24
        if up:
            v |= (1 << 30) | (1 << 31)
        return v

    def tap(self, key, hold=0.05):
        """按一下。key 可以是 "F4" 或扫描码整数。

        hold 是按下到抬起之间的间隔。驱动注入即时生效，这个间隔
        只防游戏把过快的 down/up 当成抖动（保持旧节奏，不冒进）。
        """
        scan = SCAN.get(key.upper()) if isinstance(key, str) else key
        if scan is None:
            raise KeyError(f"不认识的键：{key}")
        if self._mode in ("legacy", "message"):
            return self._tap_messages(key, hold)
        e0 = _EXTENDED.get(key.upper()) if isinstance(key, str) else False
        self._stroke(scan, up=False, e0=bool(e0))
        time.sleep(hold)
        self._stroke(scan, up=True, e0=bool(e0))
        return True

    def click(self, x, y):
        """键盘类上的「点击」是旧接口遗留（nav 曾把鼠标点击放这里）。

        现在驱动注入后 Mouse 自己就能点，这个方法按**鼠标左键**处理 ——
        保持旧签名，避免调用方跟着改。位置参数 (x, y) 用绝对坐标。
        """
        from .input import Mouse  # 本模块内的自引用，避免循环导入
        m = getattr(self, "_mouse_for_click", None)
        if m is None:
            # 走正常构造，别用 __new__ 手填字段：__new__ 绕过了
            # __init__ 里的模式判定（_mode / _drv_warn / 虚拟屏尺寸），
            # 少填一个就 AttributeError，而报错点在「第一次点击」时才出现。
            m = Mouse(None, game_hwnd=self.hwnd, mode=self._mode)
            self._mouse_for_click = m
        m.click(x, y)

    # 滚轮要把像素坐标换算成虚拟屏 0-65535 绝对坐标，需要几何信息。
    # Keyboard 没有 Screen，默认按主屏；每次滚之前刷新一次（改分辨率 /
    # 多屏时跟着走）。字段放在类上，裸构造也能读到。
    _vx = 0
    _vy = 0
    _vw = 1919
    _vh = 1079
    _origin_left = 0
    _origin_top = 0

    def _refresh_geometry(self):
        """读虚拟屏几何；读不到（离线测试/无桌面）就保留默认主屏。"""
        try:
            self._vx = int(_user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
            self._vy = int(_user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
            self._vw = max(1, int(_user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)) - 1)
            self._vh = max(1, int(_user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)) - 1)
        except Exception:                    # noqa: BLE001
            pass

    def _abs(self, x, y):
        """像素坐标 → 虚拟屏绝对坐标（与 Mouse._abs 同一套换算与越界检查）。"""
        px = self._origin_left + x - self._vx
        py = self._origin_top + y - self._vy
        if px < -2 or py < -2 or px > self._vw + 2 or py > self._vh + 2:
            raise ValueError(
                f"滚轮坐标越界：({x}, {y}) 换算后是虚拟屏 ({px}, {py})，"
                f"有效范围 0~{self._vw} × 0~{self._vh} —— 坐标算错了，这一滚不发")
        px = min(max(px, 0), self._vw)
        py = min(max(py, 0), self._vh)
        return (int(round(px * 65535 / self._vw)),
                int(round(py * 65535 / self._vh)))

    def wheel(self, x, y, notches):
        """在屏幕坐标 (x,y) 滚轮。正数向上，负数向下。

        **只走驱动注入，没有系统鼠标兜底**：不再 SetCursorPos、不再
        mouse_event。驱动不可用或通道不是 interception 时直接抛
        InputChannelError（界面上能看见），不静默换通道。

        滚动仍要先把光标移进目标区：游戏按「当前光标位置」决定把滚动交给
        哪个列表。这是**唯一会短暂占用光标的路径**（滚动期间驻留，结束后
        按驱动 FIFO 回原位），已写进账本；点击路径不受影响。

        message 通道例外：纯消息滚轮（WM_MOUSEWHEEL）全程不碰光标，
        也**不移动光标** —— 目标若按光标位置决定滚动区域，这条通道无效。
        """
        if self._mode == "message":
            return self._wheel_message(x, y, notches)
        if self._mode != "interception":
            raise InputChannelError(
                "滚轮只走 Interception 驱动注入；当前通道是 "
                f"{MODE_LABEL.get(self._mode, self._mode)}，该通道的系统鼠标"
                "兜底已关闭（改用 input_mode=message，或先修好驱动）",
                R_DRIVER_UNAVAILABLE, channel=self._mode)
        from .interception import (MOUSE_MOVE_ABSOLUTE, MOUSE_VIRTUAL_DESKTOP,
                                   MOUSE_WHEEL, shared)
        drv = shared()
        ident = drv.mouse_device()
        flags = MOUSE_MOVE_ABSOLUTE | MOUSE_VIRTUAL_DESKTOP
        self._refresh_geometry()
        ax, ay = self._abs(x, y)
        home = _cursor_pos()
        hx = hy = None
        if home is not None:
            try:
                hx, hy = self._abs(home[0] - self._origin_left,
                                   home[1] - self._origin_top)
            except ValueError:
                hx = hy = None
        LEDGER.record({"stage": "wheel", "from": self._mode,
                       "to": "interception", "reason_code": None,
                       "reason": "滚轮期间光标暂驻目标区（游戏按光标位置路由"
                                 "滚动），结束回原位；点击路径不受影响",
                       "policy": self._fallback_policy, "ok": True,
                       "severity": "info"})
        self._home = home
        self._home_target = (self._origin_left + x, self._origin_top + y)
        self._home_target_ts = time.perf_counter()
        self._home_pending = home is not None
        try:
            drv.send_mouse(0, x=ax, y=ay, flags=flags, device=ident["device"])
            time.sleep(0.05)
            step = 120
            for _ in range(abs(int(notches))):
                delta = step if int(notches) > 0 else -step
                # rolling 是有符号 i16：120 = 向上一格
                drv.send_mouse(MOUSE_WHEEL, rolling=delta,
                               device=ident["device"])
                time.sleep(0.12)
            time.sleep(0.2)
            return True
        finally:
            # 回原点也走驱动 stroke（最后一条位置事件），不发 SetCursorPos。
            if hx is not None:
                try:
                    drv.send_mouse(0, x=hx, y=hy, flags=flags,
                                   device=ident["device"])
                except Exception:                        # noqa: BLE001
                    self._emergency_cursor_restore(home)
            elif home is not None:
                self._emergency_cursor_restore(home)
            if _cursor_pos() == home:
                self._home_pending = False

    def _wheel_message(self, x, y, notches):
        """纯消息滚轮：不移动光标、不发真实事件（目标不读光标时才有效）。"""
        if not self.hwnd:
            raise InputChannelError("纯消息滚轮需要目标窗口句柄",
                                    R_NO_TARGET_HWND, channel="message")
        from .nocursor import Clicker, InputError
        try:
            clicker = getattr(self, "_wheel_clicker", None)
            if clicker is None or clicker.hwnd != self.hwnd:
                clicker = Clicker(self.hwnd, "message", verify=True)
                self._wheel_clicker = clicker
            clicker.wheel(x, y, int(notches))
            return True
        except InputError as e:
            raise InputChannelError(
                f"纯消息滚轮失败：{e}",
                getattr(e, "reason_code", R_MESSAGE_NOT_ACCEPTED),
                channel="message", detail=str(e)) from e


# ---------------- 完整性级别（UIPI） ----------------
#
# Windows 的 UIPI：低完整性进程**不能**向高完整性窗口发输入消息
# （WM_KEYDOWN / WM_LBUTTONDOWN 这类低于 WM_USER 的消息一律被拦），
# SendInput 也一样。被拦时**没有任何报错** —— 表现就是「点击、F4 全都没
# 反应」，看起来像代码坏了。
#
# 真机实测（2026-10-23）：游戏以管理员（High）运行、控制台以普通权限
# （Medium）运行 —— 点击和键盘全部无效；把控制台提权后，同一套代码
# 立刻全部生效（点交易行 54%、F4 进市场 21%、价格框读回准确）。

INTEGRITY_CN = {0x1000: "低", 0x2000: "普通", 0x3000: "管理员", 0x4000: "系统"}


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


def process_integrity(hwnd=None):
    """进程完整性级别（RID）。取不到返回 None。

    hwnd 给了就查那个窗口所属的进程，否则查当前进程。
    """
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    token_query = 0x0008
    token_integrity_level = 25

    # 必须显式声明参数类型：句柄是 64 位指针，不声明的话 ctypes 按 32 位
    # 整数传，OpenProcessToken 会拿着截断的句柄去查 —— 静默失败，
    # 读出来是「权限未知」。
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]

    if hwnd:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return None
        # 这个句柄是自己开的，必须自己关；不关就是每次查完整性泄漏一个
        # 内核句柄（preflight 每次启动都查，长时间跑会耗光）。
        own_handle = True
    else:
        h = kernel32.GetCurrentProcess()
        own_handle = False

    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(h, token_query, ctypes.byref(token)):
            return None
        try:
            n = wintypes.DWORD()
            advapi32.GetTokenInformation(token, token_integrity_level,
                                        None, 0, ctypes.byref(n))
            if not n.value:
                return None
            buf = ctypes.create_string_buffer(n.value)
            advapi32.GetTokenInformation(token, token_integrity_level,
                                        buf, n.value, ctypes.byref(n))
            label = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
            advapi32.ConvertSidToStringSidW.restype = ctypes.c_void_p
            advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                        ctypes.POINTER(ctypes.c_void_p)]
            p = ctypes.c_void_p()
            if not advapi32.ConvertSidToStringSidW(label.Label.Sid, ctypes.byref(p)):
                return None
            try:
                rid = int(ctypes.wstring_at(p).split("-")[-1])
            finally:
                kernel32.LocalFree(p)
            return rid
        except Exception:
            return None
        finally:
            kernel32.CloseHandle(token)
    finally:
        if own_handle:
            kernel32.CloseHandle(h)


def integrity_name(rid):
    """完整性级别的中文名。"""
    if rid is None:
        return "未知"
    return INTEGRITY_CN.get(rid, hex(rid))


def foreground_integrity():
    """前台窗口所属进程的完整性级别。"""
    return process_integrity(_user32.GetForegroundWindow())


def find_game_window():
    """找出游戏的窗口句柄。

    为什么不能直接用前台窗口：前台可能是控制台自己、浏览器、IDE ——
    拿它当「游戏」会把点击发到错的窗口上（实测定位功能一直报
    「点标签没切过去」）。按**窗口类**找：Unreal 的渲染窗口类名是
    `UnrealWindow`，别的程序不会有这个名字。
    """
    from ctypes import wintypes

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _):
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(h, buf, 256)
        if buf.value == "UnrealWindow" and _user32.IsWindowVisible(h):
            found.append(h)
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        return None
    return found[0] if found else None


def visible_max_integrity(exclude_pid=None):
    """所有**可见**顶层窗口里，最高的进程完整性级别。

    为什么不用前台窗口：前台可能是控制台自己、IDE、浏览器 —— 游戏窗口
    这时被盖着，前台查不到它的权限，权限检查就漏了。而游戏窗口一直
    是个可见的顶层窗口，扫一遍就抓得到。

    自己的进程跳过（它当然跟自己一样高）。
    """
    if exclude_pid is None:
        exclude_pid = _kernel32.GetCurrentProcessId()
    best = None
    handles = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _):
        handles.append(h)
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        return None

    for h in handles:
        if not _user32.IsWindowVisible(h):
            continue
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == exclude_pid:
            continue
        rid = process_integrity(h)
        if rid is not None and (best is None or rid > best):
            best = rid
    return best



def game_integrity():
    """游戏窗口的完整性级别。没找到游戏窗口返回 None。

    判权限只该看**这个窗口**：UIPI 拦的是「往哪个窗口发消息」，
    跟机器上别的窗口权限多高没有关系。
    """
    hwnd = find_game_window()
    if not hwnd:
        return None
    return process_integrity(hwnd)


def needs_elevation():
    """游戏窗口比我们权限高吗？高的话我们点不动它。

    **只看游戏窗口**。以前这里是 `visible_max_integrity()`（扫所有可见
    窗口取最高权限），于是任务管理器、安装器、某些启动器一开着就判
    「需要提权」，提权重开之后新进程又扫到同一个高权限窗口、再判一次
    —— 死循环。日志实测 01:07–01:39 连弹 8 次 UAC，用户看到的就是
    「双击之后一直弹窗、窗口永远出不来」。
    """
    try:
        ours = process_integrity()
        game = game_integrity()
    except Exception:
        return False
    return ours is not None and game is not None and game > ours


def elevate_self():
    """用管理员身份把自己重开一遍。返回 True = 已经重开，调用方应退出。

    为什么必须有这一步：UIPI 规定低完整性进程不能向高完整性窗口发输入
    消息（鼠标、按键全被拦），而且**一声不吭** —— 症状是「点击没反应、
    F4 没反应」，看起来像代码坏了。

    真机实测（2026-10-23）：游戏以管理员（High）运行、控制台以普通权限
    （Medium）运行时，PostMessage / SendInput 四种注入方式全部无效；
    把控制台提权后，**同一套代码**立刻全部生效（点交易行 54%、F4 进市场
    21%、价格框读回准确）。
    """
    import os
    import subprocess
    import sys

    # 已经提过一次就别再提。判定逻辑万一还是认为「权限不够」（比如游戏
    # 以 System 跑），重开只会变成无限弹 UAC —— 子进程进来第一件事就是
    # 放弃，把选择权交回用户（界面上会提示手动以管理员运行）。
    #
    # 两条判据并用，缺一不可：
    #   · 环境变量：同一个父进程链里最直接；
    #   · 命令行开关：`runas` 是**由 UAC 服务代启**的，新进程的环境块不一定
    #     继承父进程的（实测这台机器上就没继承）—— 只靠环境变量，子进程会
    #     认为「我没提过」，于是再提一次，desktop.log 里连着一串
    #     「已以管理员身份重新启动，本进程退出」就是这么来的。
    if os.environ.get(ELEVATED_ENV) == "1" or ELEVATE_FLAG in sys.argv[1:]:
        return False

    if not needs_elevation():
        return False

    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:] + [ELEVATE_FLAG])
    else:
        exe = sys.executable
        params = subprocess.list2cmdline(
            [os.path.abspath(sys.argv[0])] + sys.argv[1:] + [ELEVATE_FLAG])
    try:
        os.environ[ELEVATED_ENV] = "1"      # 子进程继承：它不会再重开自己
        # ShellExecuteW 的 "runas" 会弹 UAC；返回 > 32 一般表示成功启动。
        # 但**不能只看 > 32**：UAC 被点「否」时返回的是 ERROR_CANCELLED
        # (1223)、被策略拦下是 5 / 740 —— 这些都是错误码，却都大于 32。
        # 以前只判 rc <= 32，于是「用户点了否」被当成「已重开」：本进程
        # 退出、又没有新窗口，用户看到的就是双击之后什么都没发生，再点
        # 一次再弹一次 UAC。
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params,
                                                 None, 1)
    except Exception:
        return False
    if rc <= 32 or rc in SHELL_ERROR_CODES:
        # 没提起来（UAC 被拒/找不到程序）：把自己身上的标记撤掉，
        # 免得用户手动再点一次「以管理员运行」时反而不提权了
        os.environ.pop(ELEVATED_ENV, None)
        return False
    return True



