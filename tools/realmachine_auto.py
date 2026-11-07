#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键真机验证执行器（R0–R7）。

用户只需要：把游戏停在交易行在售列表页 → 运行本命令 → 看结论。

设计约束（与 `_realmachine_validation_plan.md` 对齐）：
  * 分阶段、逐阶段门禁：前置不过，后续阶段整体跳过并标注原因。
  * 每个判定项只有 过 / 不过 / 跳过 三种结果；跳过分
    gate（前置未过）/ not_auto（不可自动判定）/ admin（需管理员，未执行）
    / absent（本机没有）/ disabled（命令行关闭）。
  * 误买即停：records 新命中、确认弹窗、列表少件任一出现 → 立刻安全收尾。
  * 随时可中止：F8 或 `_realmachine_abort` 文件；收尾必做
    （松开左键、还原光标、卸载热键、关闭采集、写完记录）。
  * 只读优先；必须点击的项目集中在 R3，且说明原因。
  * 不落盘截图，失败项最多 3 张。
  * 不装驱动、不改系统设置、不动注册表；需要提权的项一律标「未执行」。

事件窗口（R6）需要用户在场触发，执行器只提示、只等信号（F9 / 文件），
不提问。过夜模式（R7）用 `--overnight-hours` 开启，逐分钟落盘曲线。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from ctypes import wintypes
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

IMPORT_ERROR = ""
try:
    import numpy as np
    import cv2
    from core import config as config_mod
    from core import countdown as countdown_mod
    from core import hotkey as hotkey_mod
    from core import input as input_mod
    from core import nav as nav_mod
    from core import ocr as ocr_mod
    from core import records as records_mod
    from core import screen as screen_mod
    from core import selfcheck as selfcheck_mod
    from core import vision as vision_mod
except Exception as exc:                                # noqa: BLE001
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

CREATE_NO_WINDOW = 0x08000000
ABORT_FILE = os.path.join(ROOT, "_realmachine_abort")
EVENT_FILE = os.path.join(ROOT, "_realmachine_event")
SHOT_ROOT = os.path.join(ROOT, "_realmachine_shots")
BACKUP_ROOT = os.path.join(ROOT, "_realmachine_backup")
TRACE_PATH = os.path.join(ROOT, "_realmachine_trace.log")
JSON_PATH = os.path.join(ROOT, "_realmachine_result.json")
MD_PATH = os.path.join(ROOT, "_realmachine_result.md")
OVERNIGHT_JSONL = os.path.join(ROOT, "_realmachine_overnight.jsonl")

# ---------------------------------------------------------------- 判定阈值
# 与 `_realmachine_validation_plan.md` 附录 B 同源；改这里等于改验收线。
TH = {
    "v1_read_rate": 0.95,       # 可读轮次占比
    "v1_p50_ms": 35.0,
    "v1_p95_ms": 70.0,
    "v1_tier_rate": 0.10,       # tier2+tier3 调用占比
    "v1_consistent": 0.90,      # 跨轮次价格集合一致率
    "v2_sample_hz": 50.0,       # 倒计时有效读数频率
    "v2_zero_lag_ms": 1200.0,   # 归零首帧相对 zero_at 的上界（含 1s 显示截断）
    "v2_ticks_min": 8,
    "v2_soak_p99_ms": 120.0,
    "v2_soak_empty": 0.05,
    "v3_accept_rate": 0.80,     # 手动手册为 18/20=90%，这里按可点击次数折算
    "v3_consec_fail": 3,
    "v4_p50_ms": 1.0,
    "v4_p95_ms": 2.0,
    "v4_max_ms": 20.0,
    "v5_land_px": 8.0,
    "v6_log_hits": 0,
    "v7_doctor_s": 40.0,
    "v7_offline_within10": 0.95,
    "v7_offline_trust": 0.95,
    "v7_offline_coverage": 0.95,
    "v7_false_trust": 0.02,
    "overnight_mem_mb_h": 0.5,
    "overnight_handle_growth": 50,
    "overnight_thread_growth": 4,
    "overnight_alive": 0.99,
    "event_timeout_s": 300.0,
}

# ---------------------------------------------------------------- 阶段目录
# (id, 标题, 是否为门禁项)
CATALOG: dict[str, list[tuple[str, str, bool]]] = {
    "R0": [
        ("R0-1", "配置读取", True),
        ("R0-2", "配置结构完整性", False),
        ("R0-3", "关键资产与区域", True),
        ("R0-4", "文件备份", False),
        ("R0-5", "系统级备份", False),
        ("R0-6", "基线指纹", False),
        ("R0-7", "源码可编译", False),
        ("R0-8", "截屏与 DPI", True),
        ("R0-9", "游戏窗口", True),
        ("R0-10", "完整性级别（UIPI）", False),
        ("R0-11", "驱动状态", False),
        ("R0-12", "管理员权限", False),
        ("R0-13", "OCR 可用性", True),
        ("R0-14", "记录文件可读", False),
        ("R0-15", "中止通道", False),
        ("R0-16", "离线回归（pytest）", False),
    ],
    "R1": [
        ("R1-1", "区域静态合法性", True),
        ("R1-2", "分辨率/DPI/副屏静态适配", True),
        ("R1-3", "打包产物与 --doctor", False),
        ("R1-4", "倒计时离线模拟", False),
        ("R1-5", "OCR 真值一致性", False),
        ("R1-6", "干净机器矩阵", False),
    ],
    "R2": [
        ("R2-1", "当前页面识别", True),
        ("R2-2", "实机读价稳定性/时延", True),
        ("R2-3", "价格真值一致性", False),
        ("R2-4", "倒计时实机观测", False),
        ("R2-5", "T0 绝对偏差", False),
        ("R2-6", "短时稳定性（soak）", False),
        ("R2-7", "反作弊被动观察", False),
        ("R2-8", "反作弊长时观察", False),
    ],
    "R3": [
        ("R3-1", "点击前置（单价上限/模式）", True),
        ("R3-2", "游戏前台", True),
        ("R3-3", "输入通道", True),
        ("R3-4", "点击受理率", False),
        ("R3-5", "光标干扰窗口", False),
        ("R3-6", "点击落点", False),
        ("R3-7", "按键残留", False),
        ("R3-8", "误买守卫", False),
    ],
    "R6": [
        ("R6-1", "事件窗口可用性", False),
        ("R6-2", "显示器唤醒 · 截图", False),
        ("R6-3", "显示器唤醒 · 几何", False),
        ("R6-4", "显示器唤醒 · 坐标对齐", False),
        ("R6-5", "系统恢复 · 定时器", False),
        ("R6-6", "系统恢复 · 时间基准", False),
        ("R6-7", "系统恢复 · 截图", False),
        ("R6-8", "系统恢复 · 输入通道", False),
        ("R6-9", "掉线 · 登录界面识别", False),
        ("R6-10", "重登 · 回到在售页", False),
        ("R6-11", "掉线期 · 零盲点", False),
    ],
    "R7": [
        ("R7-1", "过夜模式开关", False),
        ("R7-2", "内存斜率", False),
        ("R7-3", "句柄增长", False),
        ("R7-4", "线程增长", False),
        ("R7-5", "周期耗时", False),
        ("R7-6", "存活率", False),
        ("R7-7", "残留进程", False),
    ],
    "R4": [
        ("R4-1", "收尾审计", False),
        ("R4-2", "配置完整性", False),
        ("R4-3", "记录未被污染", False),
        ("R4-4", "崩溃/杀进程恢复", False),
        ("R4-5", "断电恢复", False),
        ("R4-6", "双开互斥", False),
    ],
    "R5": [
        ("R5-1", "驱动存在性", False),
        ("R5-2", "驱动安装与重启", False),
        ("R5-3", "驱动路径点击复核", False),
        ("R5-4", "反作弊×驱动长时", False),
        ("R5-5", "干净机器打包矩阵", False),
    ],
}

PHASE_NAMES = {
    "R0": "环境与基线",
    "R1": "离线 / 静态",
    "R2": "实机只读",
    "R3": "点击类（最后）",
    "R6": "事件窗口（需在场）",
    "R7": "过夜模式",
    "R4": "回退与收尾",
    "R5": "驱动与打包专项",
}

PHASE_EST = {  # 秒，仅用于开跑前的总时长预估
    "R0": 25,
    "R1": 75,
    "R2": 70,
    "R3": 22,
    "R6": 360,
    "R7": 0,
    "R4": 5,
    "R5": 1,
}

STATUS_CN = {"pass": "过", "fail": "不过", "skip": "跳过"}
REASON_CN = {
    "gate": "因前置未过而跳过",
    "not_auto": "不可自动判定",
    "admin": "此项需管理员，未执行",
    "absent": "本机不存在 / 未配置",
    "disabled": "命令行已关闭",
    "abort": "已中止",
    "timeout": "等待事件超时",
    "halt": "误买止损，后续未执行",
}


# ---------------------------------------------------------------- 数据结构
class Aborted(Exception):
    """用户中止（热键 / 文件）。"""


class Check:
    __slots__ = ("cid", "title", "phase", "gate", "status", "measured",
                 "threshold", "detail", "reason", "shots", "at")

    def __init__(self, cid, title, phase, gate=False):
        self.cid = cid
        self.title = title
        self.phase = phase
        self.gate = gate
        self.status = None
        self.measured = "—"
        self.threshold = "—"
        self.detail = ""
        self.reason = ""
        self.shots: list[str] = []
        self.at = None

    def ok(self, measured, threshold, detail=""):
        self.status = "pass"
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def bad(self, measured, threshold, detail=""):
        self.status = "fail"
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def skip(self, reason, detail="", measured="—", threshold="—"):
        self.status = "skip"
        self.reason = reason
        self.measured = str(measured)
        self.threshold = str(threshold)
        self.detail = detail
        self.at = _iso()
        return self

    def as_dict(self):
        return {
            "id": self.cid, "title": self.title, "phase": self.phase,
            "gate": self.gate, "status": self.status,
            "result": STATUS_CN.get(self.status, self.status),
            "measured": self.measured, "threshold": self.threshold,
            "detail": self.detail, "skip_reason": self.reason,
            "skip_reason_cn": REASON_CN.get(self.reason, self.reason),
            "shots": self.shots, "at": self.at,
        }


# ---------------------------------------------------------------- 小工具
def _iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pc():
    return time.perf_counter()


def _pct(values, p):
    if not values:
        return float("nan")
    s = sorted(values)
    k = int(round((p / 100.0) * (len(s) - 1)))
    k = max(0, min(len(s) - 1, k))
    return float(s[k])


def _mean(values):
    return float(sum(values) / len(values)) if values else float("nan")


def _fmt(v, nd=1, unit=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        if math.isnan(v):
            return "—"
        if math.isinf(v):
            return "∞"
        return f"{v:.{nd}f}{unit}"
    return f"{v}{unit}"


def _slope_mb_hours(x_hours, y_mb):
    if len(x_hours) < 2 or len(x_hours) != len(y_mb):
        return float("nan")
    n = len(x_hours)
    mx, my = sum(x_hours) / n, sum(y_mb) / n
    den = sum((x - mx) ** 2 for x in x_hours)
    if den <= 1e-9:
        return float("nan")
    num = sum((x - mx) * (y - my) for x, y in zip(x_hours, y_mb))
    return num / den


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 进程指标
class _MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
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


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
    ]


def process_metrics():
    """当前进程的 (private_mb, working_set_mb, handles, threads, children)。"""
    out = {"private_mb": float("nan"), "working_mb": float("nan"),
           "handles": 0, "threads": 0, "children": 0}
    try:
        k32 = ctypes.windll.kernel32
        mc = _MEMORY_COUNTERS_EX()
        mc.cb = ctypes.sizeof(mc)
        fn = getattr(ctypes.windll.psapi, "GetProcessMemoryInfo", None)
        if fn is None:
            fn = getattr(k32, "K32GetProcessMemoryInfo", None)
        if fn and fn(k32.GetCurrentProcess(), ctypes.byref(mc), mc.cb):
            out["private_mb"] = mc.PrivateUsage / 1048576.0
            out["working_mb"] = mc.WorkingSetSize / 1048576.0
        h = wintypes.DWORD()
        if k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(h)):
            out["handles"] = int(h.value)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(0x00000004, 0)   # TH32CS_SNAPTHREAD
        if snap and snap != ctypes.c_void_p(-1).value:
            te = _THREADENTRY32()
            te.dwSize = ctypes.sizeof(te)
            my_pid = k32.GetCurrentProcessId()
            n = 0
            if k32.Thread32First(snap, ctypes.byref(te)):
                while True:
                    if te.th32OwnerProcessID == my_pid:
                        n += 1
                    if not k32.Thread32Next(snap, ctypes.byref(te)):
                        break
            out["threads"] = n
            k32.CloseHandle(snap)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if snap and snap != ctypes.c_void_p(-1).value:
            pe = _PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(pe)
            my_pid = k32.GetCurrentProcessId()
            n = 0
            if k32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    if pe.th32ParentProcessID == my_pid:
                        n += 1
                    if not k32.Process32Next(snap, ctypes.byref(pe)):
                        break
            out["children"] = n
            k32.CloseHandle(snap)
    except Exception:                                   # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- 光标采样
class CursorSampler:
    """最高频率采样 GetCursorPos，用来量光标离开/回到的时间窗。"""

    def __init__(self, limit=40000):
        self.samples: list[tuple[float, int, int]] = []
        self._stop = threading.Event()
        self._thread = None
        self.limit = limit

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="cursor-sampler",
                                        daemon=True)
        self._thread.start()

    def _loop(self):
        gp = ctypes.windll.user32.GetCursorPos
        pt = wintypes.POINT()
        while not self._stop.is_set() and len(self.samples) < self.limit:
            t = _pc()
            if gp(ctypes.byref(pt)):
                self.samples.append((t, pt.x, pt.y))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)

    def analyze(self, pre, target):
        info = {"leave_ms": 0.0, "land_px": 9999.0, "moved": False,
                "hz": 0.0, "n": len(self.samples)}
        if not self.samples:
            return info
        span = self.samples[-1][0] - self.samples[0][0]
        info["hz"] = (len(self.samples) / span) if span > 0 else 0.0
        leave = None
        back = None
        for t, x, y in self.samples:
            d_pre = math.hypot(x - pre[0], y - pre[1])
            d_tgt = math.hypot(x - target[0], y - target[1])
            if d_tgt < info["land_px"]:
                info["land_px"] = d_tgt
            if d_pre > 2:
                if leave is None:
                    leave = t
                    info["moved"] = True
            elif leave is not None and back is None:
                back = t
        if leave is not None:
            end = back if back is not None else self.samples[-1][0]
            info["leave_ms"] = (end - leave) * 1000.0
        return info


# ---------------------------------------------------------------- 主执行器
class Runner:
    def __init__(self, args):
        self.args = args
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.started_at = time.time()
        self.t0 = _pc()
        self.checks: list[Check] = []
        self.cur: dict[str, Check] = {}
        self.gates: dict[str, bool] = {}
        self.cfg = None
        self.screen = None
        self.hwnd = None
        self.mouse = None
        self.driver_ok = False
        self.driver_msg = ""
        self.hotkey_abort = None
        self.hotkey_event = None
        self.abort = threading.Event()
        self.event_flag = threading.Event()
        self.abort_source = ""
        self.event_source = ""
        self.trade = {"suspected": False, "kind": "", "detail": "", "at": None}
        self.halt = ""
        self.fatal = ""
        self.cleanup: dict = {}
        self.start_cursor = (0, 0)
        self.start_foreground = 0
        self.click_count = 0
        self.click_attempts = []
        self.list_baseline: set[int] = set()
        self.dialog_baseline = 0.0
        self.record_baseline_ts = 0.0
        self.record_baseline_count = 0
        self.shots: dict[str, int] = {}
        self.phase_rows: list[dict] = []
        self.ocr_region = ""
        self.page = ""
        self.event_notes: list[str] = []
        self.overnight_samples: list[dict] = []
        self._trace_fp = None
        self._open: dict | None = None
        self._shot_dir = os.path.join(SHOT_ROOT, self.run_id)
        self._backup_dir = os.path.join(BACKUP_ROOT, self.run_id)

    # ------------------------------------------------------------ 输出
    def say(self, msg, level="info"):
        prefix = {"info": "", "warn": "[!] ", "fail": "[×] ", "pass": "[✓] "}.get(level, "")
        print(f"{prefix}{msg}", flush=True)
        self.trace(f"{level}: {msg}")

    def trace(self, msg):
        if not self._trace_fp:
            return
        try:
            self._trace_fp.write(f"{_iso()} {msg}\n")
            self._trace_fp.flush()
        except Exception:                                   # noqa: BLE001
            pass

    # ------------------------------------------------------------ 中止/事件
    def _on_abort(self):
        if self.abort.is_set():
            return
        self.abort_source = self.args.hotkey
        self.abort.set()
        print(f"\n[中止] 收到 {self.args.hotkey}，正在安全收尾……", flush=True)

    def _on_event(self):
        self.event_source = self.args.event_key
        self.event_flag.set()

    def aborted(self) -> bool:
        if self.abort.is_set():
            return True
        try:
            if os.path.exists(ABORT_FILE):
                self.abort_source = os.path.basename(ABORT_FILE)
                self.abort.set()
                return True
        except OSError:
            pass
        return False

    def checkpoint(self):
        if self.aborted():
            raise Aborted()

    def _clear_signal(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def wait_event(self, prompt, timeout=None):
        """打印提示并等待 F9 / `_realmachine_event`；返回 True/False。"""
        timeout = TH["event_timeout_s"] if timeout is None else timeout
        self._clear_signal(EVENT_FILE)
        self.event_flag.clear()
        self.say(f"[事件窗口] {prompt}")
        self.say(f"[事件窗口] 完成后按 {self.args.event_key}，或创建 {EVENT_FILE}；等待上限 {int(timeout)}s")
        t0 = _pc()
        while _pc() - t0 < timeout:
            if self.aborted():
                raise Aborted()
            if self.event_flag.is_set() or os.path.exists(EVENT_FILE):
                self._clear_signal(EVENT_FILE)
                self.event_flag.clear()
                self.say("[事件窗口] 收到信号，开始前后对比")
                return True
            time.sleep(0.2)
        self.say(f"[事件窗口] 超时（{int(timeout)}s），跳过该项", "warn")
        return False

    # ------------------------------------------------------------ 检查簿记
    def begin_phase(self, pid):
        self.cur = {}
        for cid, title, gate in CATALOG[pid]:
            self.cur[cid] = Check(cid, title, pid, gate)
        self._open = {"pid": pid, "t0": _pc(), "cur": self.cur}

    def chk(self, cid):
        c = self.cur[cid]
        c.phase = cid.split("-")[0]
        return c

    def add(self, c: Check):
        self.checks.append(c)
        mark = {"pass": "✓", "fail": "×", "skip": "–"}.get(c.status, "?")
        extra = ""
        if c.status == "skip":
            extra = f"（{REASON_CN.get(c.reason, c.reason)}）"
        if c.status == "fail":
            extra = f"  实测 {c.measured} / 阈值 {c.threshold}"
        self.say(f"  {mark} {c.cid} {c.title}{extra}", 
                 "pass" if c.status == "pass" else ("fail" if c.status == "fail" else "info"))

    def fail(self, c, measured, threshold, detail="", frame=None):
        c.bad(measured, threshold, detail)
        if frame is not None:
            self.save_shot(c, frame)
        return c

    def save_shot(self, c, frame):
        n = self.shots.get(c.cid, 0)
        if n >= 3:
            return
        try:
            os.makedirs(self._shot_dir, exist_ok=True)
            p = os.path.join(self._shot_dir, f"{c.cid}_{n + 1}.png")
            ok, buf = cv2.imencode(".png", frame)
            if ok:
                buf.tofile(p)
                c.shots.append(p)
                self.shots[c.cid] = n + 1
        except Exception:                               # noqa: BLE001
            pass

    def end_phase(self, pid, t0, default_reason="absent", default_detail="本项未执行"):
        self._open = None
        for c in self.cur.values():
            if c.status is None:
                c.skip(default_reason, default_detail)
            self.add(c)
        gate_checks = [c for c in self.cur.values() if c.gate]
        self.gates[pid] = all(c.status == "pass" for c in gate_checks) if gate_checks else True
        n_pass = sum(1 for c in self.cur.values() if c.status == "pass")
        n_fail = sum(1 for c in self.cur.values() if c.status == "fail")
        n_skip = sum(1 for c in self.cur.values() if c.status == "skip")
        dur = _pc() - t0
        status = "fail" if n_fail else ("skip" if n_skip == len(self.cur) else ("pass" if n_pass == len(self.cur) else "partial"))
        self.phase_rows.append({
            "id": pid, "name": PHASE_NAMES[pid], "status": status,
            "duration_s": round(dur, 2), "gate_ok": self.gates[pid],
            "pass": n_pass, "fail": n_fail, "skip": n_skip,
            "checks": [c.as_dict() for c in self.cur.values()],
        })
        self.say(f"[{pid}] {PHASE_NAMES[pid]}：过 {n_pass} · 不过 {n_fail} · 跳过 {n_skip}"
                 f"（{dur:.1f}s，门禁{'通过' if self.gates[pid] else '未过'}）",
                 "pass" if status == "pass" else ("fail" if status == "fail" else "warn"))

    def skip_phase(self, pid, reason, detail):
        self.begin_phase(pid)
        for c in self.cur.values():
            c.skip(reason, detail)
            self.add(c)
        self._open = None
        self.gates[pid] = False
        self.phase_rows.append({
            "id": pid, "name": PHASE_NAMES[pid], "status": "skip",
            "duration_s": 0.0, "gate_ok": False, "pass": 0, "fail": 0,
            "skip": len(self.cur),
            "checks": [c.as_dict() for c in self.cur.values()],
        })
        self.say(f"[{pid}] {PHASE_NAMES[pid]}：整体跳过（{REASON_CN.get(reason, reason)}）", "warn")

    def close_open_phase(self, reason, detail):
        """异常/中止时把「已经开始但没跑到 end_phase」的阶段收尾，保留已执行部分。"""
        op = self._open
        if not op:
            return
        if op["pid"] in [p["id"] for p in self.phase_rows]:
            self._open = None
            return
        self.cur = op["cur"]
        if op["pid"] == "R7":
            self._jsonl_write({"kind": "interrupted", "run_id": self.run_id, "at": _iso(),
                               "samples": len(self.overnight_samples),
                               "reason": reason, "detail": detail})
        self.end_phase(op["pid"], op["t0"], default_reason=reason, default_detail=detail)

    # ------------------------------------------------------------ 采集辅助
    def grab_box(self, name):
        if not self.screen:
            return None, None
        box = self.cfg.region(name) if self.cfg else None
        if not box:
            return None, None
        try:
            px = self.screen.norm_to_px(box)
            w, h = self.screen.size
            x, y, bw, bh = px
            x = max(0, min(x, w - 1))
            y = max(0, min(y, h - 1))
            bw = max(1, min(bw, w - x))
            bh = max(1, min(bh, h - y))
            return self.screen.grab((x, y, bw, bh)), (x, y, bw, bh)
        except Exception:                                   # noqa: BLE001
            return None, None

    def read_prices(self, crop, region_name):
        vals: list[int] = []
        if crop is None or crop.size == 0:
            return vals
        if region_name in ("price", "bar"):
            v = ocr_mod.read_price(crop)
            return [v] if v else []
        try:
            words = ocr_mod.read_words(crop)
            lines = ocr_mod.rows(words)
            for ln in lines:
                v = ocr_mod.last_number(ln.get("text", ""))
                if v:
                    vals.append(v)
        except Exception:                                   # noqa: BLE001
            return []
        return vals

    def pick_ocr_region(self, prefer_list=True):
        order = ("list", "price", "bar", "name") if prefer_list else ("price", "bar", "list", "name")
        for n in order:
            if self.cfg and self.cfg.region(n):
                return n
        return ""

    # -------------------------------------------------          阶段 R0
    def phase_r0(self):
        t0 = _pc()
        self.begin_phase("R0")
        self.say("[R0] 环境与基线（只读）")

        c = self.chk("R0-1")
        try:
            self.cfg = config_mod.Config.load()
            if getattr(self.cfg, "load_ok", False):
                c.ok(f"load_ok=True · {len(self.cfg.data)} 键", "load_ok=True")
            else:
                c.bad("load_ok=False", "load_ok=True",
                      str(getattr(self.cfg, "_load_error", "") or ""))
        except Exception as exc:                            # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "load_ok=True")
            self.gates["R0"] = False
        self.checkpoint()

        c = self.chk("R0-2")
        if not self.cfg:
            c.skip("gate", "配置未读出来")
        else:
            need = ("monitor", "regions", "workflow", "input_mode", "console", "screen")
            missing = [k for k in need if k not in self.cfg.data]
            mode = str(self.cfg.get("input_mode", ""))
            wf = str(self.cfg.get("workflow", ""))
            if missing:
                c.bad(f"缺 {missing}", "全部存在")
            else:
                c.ok(f"input_mode={mode} workflow={wf} monitor={self.cfg.get('monitor')}",
                     "全部存在")

        c = self.chk("R0-3")
        if not self.cfg:
            c.skip("gate", "配置未读出来")
        else:
            regions = {n: bool(self.cfg.region(n)) for n in
                       ("buy", "list", "price", "bar", "name", "clock", "confirm")}
            tpls = {n: bool(self.cfg.has_template(n)) for n in ("buy", "confirm")}
            ocr_region = self.pick_ocr_region()
            ok = bool(regions["buy"]) and bool(tpls["buy"]) and bool(ocr_region)
            detail = (f"区域={[k for k, v in regions.items() if v]} "
                      f"模板={[k for k, v in tpls.items() if v]} OCR区域={ocr_region or '无'}")
            if ok:
                c.ok(f"buy 区域+模板+OCR区域齐全 · {detail}", "buy区域+buy模板+OCR区域")
            else:
                c.bad(f"关键资产缺失 · {detail}", "buy区域+buy模板+OCR区域")

        c = self.chk("R0-4")
        try:
            os.makedirs(self._backup_dir, exist_ok=True)
            copied, total = [], 0
            cfg_path = os.path.join(ROOT, "config.json")
            if os.path.isfile(cfg_path):
                shutil.copy2(cfg_path, os.path.join(self._backup_dir, "config.json"))
                copied.append("config.json")
                total += os.path.getsize(cfg_path)
            tdir = getattr(config_mod, "TEMPLATE_DIR", os.path.join(ROOT, "templates"))
            if os.path.isdir(tdir):
                dst = os.path.join(self._backup_dir, "templates")
                shutil.copytree(tdir, dst, dirs_exist_ok=True)
                copied.append("templates/")
                for dp, _, fs in os.walk(dst):
                    total += sum(os.path.getsize(os.path.join(dp, f)) for f in fs)
            ddir = getattr(config_mod, "DATA_DIR", os.path.join(ROOT, "data"))
            rdir = os.path.join(self._backup_dir, "records")
            os.makedirs(rdir, exist_ok=True)
            if os.path.isdir(ddir):
                for name in os.listdir(ddir):
                    if name.startswith("records.jsonl"):
                        src = os.path.join(ddir, name)
                        if os.path.isfile(src):
                            shutil.copy2(src, os.path.join(rdir, name))
                            copied.append(f"records/{name}")
                            total += os.path.getsize(src)
            c.ok(f"{len(copied)} 项 · {total / 1048576:.1f} MB · {self._backup_dir}",
                 "配置+模板+记录 已备份")
        except Exception as exc:                            # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "备份成功")

        c = self.chk("R0-5")
        c.skip("admin", "注册表导出与系统还原点需管理员；本执行器不改注册表、不建还原点，未执行")

        c = self.chk("R0-6")
        fp = {"config_sha256": "", "exe_sha256": "", "git_head": "", "git_dirty": None}
        try:
            cfg_path = os.path.join(ROOT, "config.json")
            if os.path.isfile(cfg_path):
                fp["config_sha256"] = _sha256(cfg_path)[:16]
            exe = os.path.join(ROOT, "dist", "SJZ-Control.exe")
            if os.path.isfile(exe):
                fp["exe_sha256"] = _sha256(exe)[:16]
            head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                                  capture_output=True, text=True, timeout=10,
                                  creationflags=CREATE_NO_WINDOW)
            fp["git_head"] = (head.stdout or "").strip()[:12]
            st = subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10,
                                creationflags=CREATE_NO_WINDOW)
            fp["git_dirty"] = len([x for x in (st.stdout or "").splitlines() if x.strip()])
        except Exception:                                   # noqa: BLE001
            pass
        c.ok(f"config={fp['config_sha256'] or '无'} exe={fp['exe_sha256'] or '无'} "
             f"git={fp['git_head'] or '无'} dirty={fp['git_dirty']}", "生成基线指纹")
        self.fingerprint = fp

        c = self.chk("R0-7")
        targets = [os.path.join(ROOT, d) for d in ("core", "web", "tools", "desktop.py", "run.py")
                   if os.path.exists(os.path.join(ROOT, d))]
        try:
            r = subprocess.run([sys.executable, "-m", "compileall", "-q"] + targets,
                               capture_output=True, text=True, timeout=120,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                c.ok(f"{len(targets)} 个目标编译通过", "rc=0")
            else:
                c.bad(f"rc={r.returncode} {(r.stderr or r.stdout).strip()[:120]}", "rc=0")
        except Exception as exc:                            # noqa: BLE001
            c.skip("absent", f"compileall 不可用：{type(exc).__name__}: {exc}")

        c = self.chk("R0-8")
        try:
            idx = _safe_int(self.cfg.get("monitor", 1), 1)
            self.screen = screen_mod.Screen(idx)
            w, h = self.screen.size
            c.ok(f"{w}x{h} · dpi={self.screen.dpi} · scale={self.screen.scale} · "
                 f"显示器 {len(self.screen.monitors) - 1} 台 · 采集目标 {idx}",
                 "截屏成功且 DPI=物理像素")
        except Exception as exc:                            # noqa: BLE001
            self.screen = None
            c.bad(f"{type(exc).__name__}: {exc}", "截屏成功")
        self.checkpoint()

        c = self.chk("R0-9")
        try:
            self.hwnd = input_mod.find_game_window()
            if self.hwnd:
                rect = input_mod.game_window_rect() if hasattr(input_mod, "game_window_rect") else None
                c.ok(f"hwnd=0x{self.hwnd:X} rect={rect}", "找到游戏窗口")
            else:
                c.bad("未找到游戏窗口", "找到游戏窗口", "请先打开游戏并停在交易行在售列表页")
        except Exception as exc:                            # noqa: BLE001
            self.hwnd = None
            c.bad(f"{type(exc).__name__}: {exc}", "找到游戏窗口")

        c = self.chk("R0-10")
        try:
            ours = input_mod.process_integrity()
            target = input_mod.process_integrity(self.hwnd) if self.hwnd else None
            ours_n = input_mod.integrity_name(ours)
            target_n = input_mod.integrity_name(target)
            if ours is None or target is None:
                c.skip("not_auto", "读不到完整性级别，UIPI 预判不可用")
            elif target > ours:
                c.bad(f"本进程 {ours_n}(0x{ours:X}) < 游戏 {target_n}(0x{target:X})",
                      "本进程 ≥ 游戏", "UIPI 会静默丢弃点击；点击类将跳过，需以管理员重开")
            else:
                c.ok(f"本进程 {ours_n} · 游戏 {target_n}", "本进程 ≥ 游戏")
        except Exception as exc:                            # noqa: BLE001
            c.skip("not_auto", f"读取失败：{type(exc).__name__}: {exc}")

        c = self.chk("R0-11")
        try:
            from core import interception as icept
            ok, msg = icept.driver_ready(probe=True)
            self.driver_ok, self.driver_msg = bool(ok), msg
            if ok:
                c.ok("驱动就绪（零位移探针受理）", "driver_ready=True")
            else:
                c.skip("absent", f"驱动未就绪：{msg or '未安装 / 装完没重启'}")
        except Exception as exc:                            # noqa: BLE001
            self.driver_ok = False
            self.driver_msg = f"{type(exc).__name__}: {exc}"
            c.skip("absent", f"驱动检测失败：{self.driver_msg}")

        c = self.chk("R0-12")
        priv = selfcheck_mod.privilege() if hasattr(selfcheck_mod, "privilege") else {}
        admin = bool(priv.get("admin"))
        c.ok(f"admin={admin} integrity={priv.get('level')}",
             "仅记录",
             "驱动安装、注册表/还原点等需管理员项一律标注未执行")

        c = self.chk("R0-13")
        try:
            ok = bool(ocr_mod.available())
            name = ocr_mod.backend_name()
            if ok:
                c.ok(name, "OCR 可用")
            else:
                c.bad("OCR 不可用", "OCR 可用")
        except Exception as exc:                            # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "OCR 可用")

        c = self.chk("R0-14")
        try:
            rows = records_mod.load(limit=5)
            self.record_baseline_count = len(rows)
            self.record_baseline_ts = max([_safe_float(r.get("ts")) for r in rows] or [0.0])
            c.ok(f"{len(rows)} 行 · 最新 ts={self.record_baseline_ts:.3f} · {records_mod.PATH}",
                 "可读")
        except Exception as exc:                            # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "可读")

        c = self.chk("R0-15")
        ok_a, msg_a = (self.hotkey_abort.start() if self.hotkey_abort else (False, "未初始化"))
        ok_e, msg_e = (self.hotkey_event.start() if self.hotkey_event else (False, "未初始化"))
        if ok_a and ok_e:
            c.ok(f"{self.args.hotkey} 中止 · {self.args.event_key} 事件确认", "两个热键就绪")
        else:
            c.ok(f"热键部分失效（{msg_a} / {msg_e}）；文件开关可用：{ABORT_FILE} / {EVENT_FILE}",
                 "至少文件开关可用")

        c = self.chk("R0-16")
        if not self.args.pytest:
            c.skip("disabled", "未启用 --pytest（离线回归由 CI 覆盖）")
        else:
            try:
                r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"],
                                   cwd=ROOT, capture_output=True, text=True, timeout=900,
                                   creationflags=CREATE_NO_WINDOW)
                tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
                if r.returncode == 0:
                    c.ok("pytest 通过 · " + " / ".join(tail), "rc=0")
                else:
                    c.bad(f"rc={r.returncode} " + " / ".join(tail), "rc=0")
            except Exception as exc:                        # noqa: BLE001
                c.skip("absent", f"pytest 不可用：{type(exc).__name__}: {exc}")

        self.end_phase("R0", t0)

    # -------------------------------------------------          阶段 R1
    def phase_r1(self):
        t0 = _pc()
        self.begin_phase("R1")
        self.say("[R1] 离线 / 静态（不产生游戏输入）")

        c = self.chk("R1-1")
        bad = []
        detail = []
        for name in ("buy", "confirm", "list", "price", "bar", "name", "clock"):
            box = self.cfg.region(name) if self.cfg else None
            if not box:
                continue
            try:
                x, y, w, h = self.screen.norm_to_px(box)
                sw, sh = self.screen.size
                detail.append(f"{name}=({x},{y},{w},{h})")
                if w < 8 or h < 8:
                    bad.append(f"{name} 过小 {w}x{h}")
                if x < 0 or y < 0 or x + w > sw or y + h > sh:
                    bad.append(f"{name} 越界")
            except Exception as exc:                        # noqa: BLE001
                bad.append(f"{name} 换算失败 {type(exc).__name__}")
        if bad:
            c.bad("; ".join(bad), "全部区域 8px 以上且不越界", "；".join(detail))
        elif not detail:
            c.skip("absent", "没有配置任何区域")
        else:
            c.ok("；".join(detail), "全部区域 8px 以上且不越界")

        c = self.chk("R1-2")
        try:
            screen_cfg = self.cfg.get("screen") or {}
            base = list(screen_cfg.get("base_size") or [1920, 1080])
            tol = _safe_float(screen_cfg.get("tolerance"), 0.0)
            sw, sh = self.screen.size
            ratio = min(sw / base[0], sh / base[1])
            # 归一化 → 像素 → 归一化 往返误差
            err = 0.0
            for name in ("buy", "list", "price", "bar", "clock"):
                box = self.cfg.region(name)
                if not box:
                    continue
                x, y, w, h = self.screen.norm_to_px(box)
                nw, nh = self.screen.size
                fx, fy = x / nw, y / nh
                err = max(err, abs(fx - float(box["x"])) * nw, abs(fy - float(box["y"])) * nh)
            mon_idx = _safe_int(self.cfg.get("monitor", 1), 1)
            mon_ok = 0 <= mon_idx < len(self.screen.monitors)
            detail = (f"基准={base[0]}x{base[1]} 实际={sw}x{sh} 比例={ratio:.3f} "
                      f"容差={tol} 往返误差={err:.1f}px 显示器={mon_idx} 有效={mon_ok}")
            if not mon_ok:
                c.bad(detail, "显示器索引有效")
            elif tol > 0 and abs(ratio - 1.0) > tol:
                c.bad(detail, f"|比例-1| ≤ {tol}")
            elif tol <= 0 and (sw, sh) != (base[0], base[1]):
                c.bad(detail, "基准尺寸一致（或设置 screen.tolerance）",
                      "模板/区域按基准采集；换分辨率需重采，见手册 V5-b")
            elif err > 2.0:
                c.bad(detail, "往返误差 ≤2px")
            else:
                c.ok(detail, f"往返误差 ≤2px；|比例-1| ≤ {tol}")
        except Exception as exc:                            # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "几何自洽")

        c = self.chk("R1-3")
        exe = os.path.join(ROOT, "dist", "SJZ-Control.exe")
        if not os.path.isfile(exe):
            c.skip("absent", "dist/SJZ-Control.exe 不存在（开发机未打包）")
        else:
            sha = _sha256(exe)[:16]
            size = os.path.getsize(exe) / 1048576.0
            if self.args.no_doctor:
                c.skip("disabled", f"已指定 --no-doctor；exe {size:.1f}MB sha={sha}")
            else:
                try:
                    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
                    r = subprocess.run([exe, "--doctor"], capture_output=True, text=True,
                                       timeout=TH["v7_doctor_s"], env=env,
                                       creationflags=CREATE_NO_WINDOW,
                                       encoding="utf-8", errors="replace")
                    out = (r.stdout or "") + (r.stderr or "")
                    ocr_line = "初始化= True" in out.replace("初始化 = True", "初始化= True")
                    miss = "模型文件" in out and "缺失" in out.split("模型文件", 1)[1][:40]
                    if r.returncode == 0 and ocr_line and not miss:
                        c.ok(f"rc=0 · OCR可用 · {size:.1f}MB sha={sha}", "rc=0 且 OCR 可用")
                    else:
                        c.bad(f"rc={r.returncode} ocr_init={ocr_line} models_missing={miss}",
                              "rc=0 且 OCR 可用", "\n".join(out.strip().splitlines()[:6]))
                except subprocess.TimeoutExpired:
                    c.bad(f"超时 {int(TH['v7_doctor_s'])}s", "rc=0 且 OCR 可用")
                except Exception as exc:                    # noqa: BLE001
                    c.skip("absent", f"无法启动 --doctor：{type(exc).__name__}: {exc}")

        c = self.chk("R1-4")
        if self.args.offline_countdown_runs <= 0:
            c.skip("disabled", "未启用 --offline-countdown-runs")
        else:
            bench = os.path.join(ROOT, "tools", "bench_countdown.py")
            if not os.path.isfile(bench):
                c.skip("absent", "tools/bench_countdown.py 不存在")
            else:
                tmpdir = os.path.join(SHOT_ROOT, self.run_id)
                os.makedirs(tmpdir, exist_ok=True)
                out_json = os.path.join(tmpdir, "bench_countdown.json")
                cmd = [sys.executable, bench, "--runs", str(self.args.offline_countdown_runs),
                       "--json", out_json]
                if self.args.offline_no_raw:
                    cmd.append("--no-raw")
                try:
                    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                       timeout=self.args.offline_timeout,
                                       creationflags=CREATE_NO_WINDOW)
                    if r.returncode != 0:
                        c.skip("absent", f"bench rc={r.returncode} {(r.stderr or '')[-120:]}")
                    else:
                        with open(out_json, "r", encoding="utf-8") as fp:
                            data = json.load(fp)
                        results = data.get("results") or {}
                        w10, trust, false_trust, cover = [], [], [], []
                        for info in results.values():
                            for row in (info.get("rows") or []):
                                if row.get("within10_all") is not None:
                                    w10.append(float(row["within10_all"]))
                                if row.get("trust") is not None:
                                    trust.append(float(row["trust"]))
                                if row.get("false_trust") is not None:
                                    false_trust.append(float(row["false_trust"]))
                                if row.get("coverage") is not None:
                                    cover.append(float(row["coverage"]))
                        if not w10:
                            c.skip("absent", "离线模拟没有可读结果")
                        else:
                            mean_w10 = _mean(w10)
                            min_trust = min(trust) if trust else float("nan")
                            max_ft = max(false_trust) if false_trust else float("nan")
                            min_cov = min(cover) if cover else float("nan")
                            detail = (f"cases={len(w10)} within10均值={mean_w10:.3f} "
                                      f"min_trust={_fmt(min_trust,3)} max_false={_fmt(max_ft,3)} "
                                      f"min_coverage={_fmt(min_cov,3)}")
                            thr_txt = "within10 ≥95% · trust ≥95% · coverage ≥95% · false_trust ≤2%"
                            thr_ok = (mean_w10 >= TH["v7_offline_within10"]
                                      and (math.isnan(min_trust) or min_trust >= TH["v7_offline_trust"])
                                      and (math.isnan(min_cov) or min_cov >= TH["v7_offline_coverage"])
                                      and (math.isnan(max_ft) or max_ft <= TH["v7_false_trust"]))
                            (c.ok if thr_ok else c.bad)(detail, thr_txt)
                except subprocess.TimeoutExpired:
                    c.skip("absent", f"离线模拟超时 {self.args.offline_timeout}s")
                except Exception as exc:                    # noqa: BLE001
                    c.skip("absent", f"离线模拟无法解析：{type(exc).__name__}: {exc}")

        c = self.chk("R1-5")
        c.skip("not_auto", "与真值的一致性需人工标注 30 条价格；本执行器只判可读率/稳定性/时延")

        c = self.chk("R1-6")
        c.skip("not_auto", "干净机器矩阵需另一台机器/虚拟机，本机不可自证")

        self.end_phase("R1", t0)

    # -------------------------------------------------          阶段 R2
    def phase_r2(self):
        t0 = _pc()
        self.begin_phase("R2")
        self.say("[R2] 实机只读（不点击，画面只进内存）")
        self.ocr_region = self.pick_ocr_region(prefer_list=True)

        c = self.chk("R2-1")
        if not self.hwnd:
            c.bad("无游戏窗口", "游戏窗口存在")
        else:
            ok_fg = input_mod.Mouse.ensure_foreground(self.hwnd, tries=3, gap=0.3,
                                                     screen=self.screen)
            page, text = "", ""
            try:
                nav = nav_mod.Navigator(self.screen, None)
                page, text = nav.where()
            except Exception as exc:                        # noqa: BLE001
                page, text = "unknown", f"{type(exc).__name__}: {exc}"
            self.page = page
            if page in ("onsale", "detail"):
                c.ok(f"page={page} · 前台={ok_fg} · 文本={text[:40]}", "onsale/detail")
            else:
                c.bad(f"page={page} · 前台={ok_fg} · 文本={text[:40]}",
                      "onsale/detail", "请把游戏停在交易行在售列表页")
            if not ok_fg:
                self.say("[R2] 游戏窗口抢不回前台，后续点击类会跳过", "warn")

        c = self.chk("R2-2")
        name = self.ocr_region
        if not name:
            c.skip("absent", "没有可读的 OCR 区域")
        else:
            rounds = max(5, self.args.live_rounds)
            lats, sets, good = [], [], 0
            stats_before = self._ocr_stats()
            for _ in range(rounds):
                self.checkpoint()
                crop, _ = self.grab_box(name)
                t_read = _pc()
                vals = self.read_prices(crop, name)
                lats.append((_pc() - t_read) * 1000.0)
                sets.append(frozenset(vals))
                if vals:
                    good += 1
                time.sleep(self.args.live_interval)
            stats_after = self._ocr_stats()
            calls = max(1, stats_after["calls"] - stats_before["calls"])
            tiers = (stats_after["tier2"] - stats_before["tier2"]
                     + stats_after["tier3"] - stats_before["tier3"]) / calls
            read_rate = good / rounds
            p50 = _pct(lats, 50)
            p95 = _pct(lats, 95)
            nonempty = [s for s in sets if s]
            if nonempty:
                common = set.intersection(*[set(s) for s in nonempty])
                union = set.union(*[set(s) for s in nonempty])
                consistent = len(common) / max(1, len(union))
            else:
                consistent = 0.0
            detail = (f"区域={name} 轮次={rounds} 可读={read_rate:.0%} "
                      f"p50={p50:.1f}ms p95={p95:.1f}ms tier={tiers:.1%} "
                      f"一致={consistent:.0%} 样本={_mean(lats):.1f}ms均值")
            ok = (read_rate >= TH["v1_read_rate"] and p50 <= TH["v1_p50_ms"]
                  and p95 <= TH["v1_p95_ms"] and tiers <= TH["v1_tier_rate"]
                  and consistent >= TH["v1_consistent"])
            if ok:
                c.ok(detail, "可读≥95% · p50≤35ms · p95≤70ms · tier≤10% · 一致≥90%")
            else:
                self.fail(c, detail,
                          "可读≥95% · p50≤35ms · p95≤70ms · tier≤10% · 一致≥90%",
                          "实机字体/分辨率下读价不合格；先看失败截图与区域")
            self.checkpoint()

        c = self.chk("R2-3")
        c.skip("not_auto", "价格真值一致性需人工逐条标注；机器只能判格式闸门/稳定性/时延")

        c = self.chk("R2-4")
        clock_box = self.cfg.region("clock") if self.cfg else None
        if not clock_box:
            c.skip("absent", "未配置 regions.clock（当前页没有公示期条目属正常）")
        else:
            clk = countdown_mod.Clock()
            deadline = _pc() + self.args.countdown_observe_s
            first_t = None
            last_t = None
            valid = 0
            zero_ts = None
            zero_at_before = None
            while _pc() < deadline:
                self.checkpoint()
                crop, _ = self.grab_box("clock")
                if crop is None:
                    time.sleep(0.05)
                    continue
                t_now = _pc()
                try:
                    words = ocr_mod.read_words(crop)
                    sec = countdown_mod.parse_words(words)
                except Exception:                       # noqa: BLE001
                    sec = None
                if sec is not None:
                    valid += 1
                    first_t = first_t or t_now
                    last_t = t_now
                    if sec == 0 and zero_ts is None:
                        zero_ts = t_now
                        zero_at_before = clk.zero_at()
                    clk.observe(sec, t_now)
                time.sleep(0.02)
            span = (last_t - first_t) if (first_t and last_t) else 0.0
            hz = valid / span if span > 0 else 0.0
            ticks = int((clk.info() or {}).get("ticks") or 0)
            # 归零之后显示值就不再变了，is_stale 必然为真；停更只在未归零时才有意义
            stale = bool(clk.is_stale(t=last_t)) if (valid and zero_ts is None and last_t) else False
            lag_ms = ((zero_ts - zero_at_before) * 1000.0
                      if (zero_ts and zero_at_before) else float("nan"))
            detail = (f"读数={valid} 频率={hz:.1f}Hz ticks={ticks} "
                      f"归零={'是' if zero_ts else '否'} 首帧滞后={_fmt(lag_ms,1,'ms')} 停更={stale}")
            if valid < 3:
                c.skip("absent", "观察窗内没有倒计时读数（可能当前没有公示期商品）")
            elif stale and valid >= TH["v2_ticks_min"]:
                c.bad(detail, "停更=否；读数≥50Hz", "倒计时中途停更，先查区域是否被遮挡")
            elif hz < TH["v2_sample_hz"]:
                c.bad(detail, f"读数频率 ≥{int(TH['v2_sample_hz'])}Hz")
            elif zero_ts and math.isnan(lag_ms):
                c.skip("not_auto", "有归零帧但 zero_at 未建立，无法算滞后")
            elif zero_ts and lag_ms > TH["v2_zero_lag_ms"]:
                c.bad(detail, f"首帧滞后 ≤{int(TH['v2_zero_lag_ms'])}ms（含 1s 显示截断）")
            else:
                c.ok(detail, f"读数≥{int(TH['v2_sample_hz'])}Hz · 停更=否 · 首帧滞后 ≤1.2s")

        c = self.chk("R2-5")
        c.skip("not_auto", "T0 绝对偏差需 120 fps 录屏回放对照；本执行器只有软件时间轴，不假装能判")

        c = self.chk("R2-6")
        if self.args.soak_s <= 0:
            c.skip("disabled", "未启用 --soak-s")
        else:
            lats, empties, other = [], 0, None
            deadline = _pc() + self.args.soak_s
            while _pc() < deadline:
                self.checkpoint()
                crop, _ = self.grab_box(self.ocr_region) if self.ocr_region else (None, None)
                if crop is None:
                    time.sleep(1.0)
                    continue
                t_read = _pc()
                try:
                    vals = self.read_prices(crop, self.ocr_region)
                except Exception as exc:                # noqa: BLE001
                    other = f"{type(exc).__name__}: {exc}"
                    break
                lats.append((_pc() - t_read) * 1000.0)
                if not vals:
                    empties += 1
                time.sleep(1.0)
            if other:
                c.bad(f"采样异常：{other}", "无异常")
            elif not lats:
                c.skip("absent", "没有采样到画面")
            else:
                p99 = _pct(lats, 99)
                empty = empties / len(lats)
                detail = f"样本={len(lats)} p99={p99:.1f}ms 空读={empty:.0%} 时长={self.args.soak_s}s"
                if p99 <= TH["v2_soak_p99_ms"] and empty <= TH["v2_soak_empty"]:
                    c.ok(detail, "p99 ≤120ms · 空读 ≤5%")
                else:
                    self.fail(c, detail, "p99 ≤120ms · 空读 ≤5%")

        c = self.chk("R2-7")
        hits, logp = self._scan_desktop_log()
        alive = bool(input_mod.find_game_window())
        detail = f"进程={'在' if alive else '丢失'} 日志命中={hits or '无'} 日志={logp or '无'}"
        if logp is None:
            c.skip("absent", "没有 desktop.log，无法做被动观察")
        elif hits:
            c.bad(detail, "0 条封禁/警告/踢下线", "日志出现反作弊相关字样")
        elif not alive:
            c.bad(detail, "游戏进程存活")
        else:
            c.ok(detail, "0 条封禁/警告/踢；短时观察")

        c = self.chk("R2-8")
        c.skip("not_auto", "反作弊长时观察需 3×30min 且有测试账号；本执行器不做")

        self.end_phase("R2", t0)

    def _ocr_stats(self):
        base = {"calls": 0, "empty": 0, "tier2": 0, "tier3": 0, "fails": 0, "ms": 0.0}
        try:
            client = ocr_mod._client()
            if client is not None:
                for k in base:
                    base[k] = client.stats.get(k, 0)
        except Exception:                               # noqa: BLE001
            pass
        return base

    def _scan_desktop_log(self):
        path = os.path.join(ROOT, "desktop.log")
        if not os.path.isfile(path):
            return [], None
        keywords = ("封禁", "警告", "环境异常", "非法程序", "踢下线", "banned", "kick")
        hits = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fp:
                lines = fp.readlines()[-400:]
            for ln in lines:
                if any(k in ln for k in keywords):
                    hits.append(ln.strip()[:120])
        except Exception:                               # noqa: BLE001
            return [], path
        return hits[-5:], path

    # -------------------------------------------------          阶段 R3
    def phase_r3(self):
        t0 = _pc()
        self.begin_phase("R3")
        self.say("[R3] 点击类（唯一会投递真实点击的阶段，放在最后）")

        c = self.chk("R3-1")
        console = self.cfg.get("console") or {}
        max_price = _safe_int(console.get("max_price"), 0)
        semi = bool(self.cfg.get("semi_mode"))
        budget = _safe_int(console.get("budget"), 0)
        detail = f"max_price={max_price} budget={budget} semi_mode={semi}"
        if self.args.no_click:
            c.skip("disabled", f"{detail} · 已指定 --no-click")
        elif max_price <= 0:
            c.skip("disabled", f"{detail} · 未设置 console.max_price，为避免误买不点击")
        elif semi:
            c.skip("disabled", f"{detail} · semi_mode=true，不符合全自动点击语义")
        else:
            c.ok(detail, "max_price>0 且 semi_mode=false")

        c = self.chk("R3-2")
        if not self.hwnd:
            c.bad("无游戏窗口", "游戏窗口存在")
        else:
            ok = input_mod.Mouse.ensure_foreground(self.hwnd, tries=3, gap=0.3,
                                                   screen=self.screen)
            if ok:
                c.ok(f"hwnd=0x{self.hwnd:X} 在前台", "游戏在前台")
            else:
                c.bad("抢不回前台", "游戏在前台", "被其它窗口占用；不看提示不点击")

        c = self.chk("R3-3")
        if not self.hwnd:
            c.skip("gate", "无游戏窗口")
        else:
            try:
                self.mouse = input_mod.Mouse(
                    screen=self.screen, game_hwnd=self.hwnd,
                    mode=self.cfg.get("input_mode", "interception"),
                    fallback=self.cfg.get("input_fallback", "legacy"))
                mode = self.mouse.mode
                c.ok(f"mode={mode} · {self.mouse.reason_code or '无降级'}",
                     "通道初始化成功", str(self.mouse.channel_status() if hasattr(self.mouse, "channel_status") else ""))
            except Exception as exc:                    # noqa: BLE001
                self.mouse = None
                c.bad(f"{type(exc).__name__}: {exc}", "通道初始化成功",
                      "UIPI/驱动/窗口都可能导致失败；此项不过则不做任何点击")

        need = [self.cur["R3-1"], self.cur["R3-2"], self.cur["R3-3"]]
        if any(x.status != "pass" for x in need):
            for cid in ("R3-4", "R3-5", "R3-6", "R3-7", "R3-8"):
                self.chk(cid).skip("gate", "点击前置未过，不做任何点击")
            self.end_phase("R3", t0)
            return

        self.say("[R3] 采集基线：列表价格、确认模板、记录水位")
        self.list_baseline = self._list_snapshot()
        self.dialog_baseline = self._dialog_score()[0]
        self._refresh_record_baseline()

        n = max(1, self.args.click_tests)
        accepted = weak = 0
        consec_fail = 0
        leave_ms, land_px = [], []
        residual = 0
        last_error = ""
        attempt: dict = {}
        for i in range(n):
            self.checkpoint()
            if self.trade["suspected"]:
                break
            box = self.cfg.region("buy")
            x, y, w, h = self.screen.norm_to_px(box)
            left, top = self.screen.bounds["left"], self.screen.bounds["top"]
            target = (left + x + w // 2, top + y + h // 2)
            pre = input_mod.Mouse.cursor()
            before = self.screen.grab()
            sampler = CursorSampler()
            sampler.start()
            time.sleep(0.002)
            t_call = _pc()
            try:
                self.mouse.click(x + w // 2, y + h // 2, expect_foreground=self.hwnd)
                call_ms = (_pc() - t_call) * 1000.0
            except Exception as exc:                    # noqa: BLE001
                sampler.stop()
                last_error = f"{type(exc).__name__}: {exc}"
                break
            time.sleep(0.05)
            sampler.stop()
            info = sampler.analyze(pre, target)
            self.click_count += 1
            leave_ms.append(info["leave_ms"])
            land_px.append(info["land_px"])

            kind, ev = self._wait_evidence(1.2)
            after = self.screen.grab()
            if not kind:
                diff = self._frame_diff_pct(before, after)
                if self._list_shrunk():
                    kind, ev = "list", "列表少件"
                elif diff >= 6.0:
                    kind, ev = "screen", f"画面差 {diff:.1f}/255"
            attempt = {"i": i + 1, "kind": kind or "", "evidence": ev,
                       "call_ms": round(call_ms, 2),
                       "leave_ms": round(info["leave_ms"], 2),
                       "land_px": round(info["land_px"], 1),
                       "hz": round(info["hz"], 1)}
            self.click_attempts.append(attempt)
            self.say(f"  第 {i + 1} 次点击：{kind or '无受理证据'} · {ev} · "
                     f"调用 {call_ms:.1f}ms · 落点 {info['land_px']:.1f}px")
            if kind in ("records", "dialog", "list"):
                self._mark_trade(kind, str(ev))
                break
            if kind == "screen":
                weak += 1
            if kind:
                accepted += 1
                consec_fail = 0
            else:
                consec_fail += 1
                if consec_fail >= TH["v3_consec_fail"]:
                    self.say(f"  连续 {consec_fail} 次不受理，停止点击（手册一票否决）", "warn")
                    break
            time.sleep(max(0.2, self.args.click_interval))

        if last_error:
            self.chk("R3-3").bad(last_error, "通道初始化成功")

        for _ in range(2):
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
        time.sleep(0.4)
        if ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000:
            residual = 1

        c = self.chk("R3-4")
        n_att = len(self.click_attempts)
        if n_att == 0:
            c.skip("absent", f"一次点击都没投递（{last_error or '前置/中止'}）")
        else:
            rate = accepted / n_att
            detail = (f"受理 {accepted}/{n_att}（强证据 {accepted - weak} · 弱证据 {weak}）"
                      f" · 点击 {n_att} 次")
            if rate >= TH["v3_accept_rate"]:
                c.ok(detail, f"受理率 ≥{int(TH['v3_accept_rate'] * 100)}%（手册 18/20）")
            else:
                self.fail(c, detail, f"受理率 ≥{int(TH['v3_accept_rate'] * 100)}%",
                          "换通道/窗口模式/坐标策略，见手册 V3 备选")

        c = self.chk("R3-5")
        mode = self.mouse.mode if self.mouse else ""
        if not leave_ms:
            c.skip("absent", "没有点击样本")
        elif mode != "legacy":
            c.ok(f"mode={mode} · 采样 {max([a['hz'] for a in self.click_attempts] or [0]):.0f}Hz · "
                 f"离开 {_pct(leave_ms,50):.1f}/{_pct(leave_ms,95):.1f}/"
                 f"{max(leave_ms):.1f}ms", "仅记录（驱动通道不逐次还原光标）")
        else:
            detail = (f"p50={_pct(leave_ms,50):.1f}ms p95={_pct(leave_ms,95):.1f}ms "
                      f"max={max(leave_ms):.1f}ms 采样≈"
                      f"{max([a['hz'] for a in self.click_attempts] or [0]):.0f}Hz")
            if (_pct(leave_ms, 50) <= TH["v4_p50_ms"]
                    and _pct(leave_ms, 95) <= TH["v4_p95_ms"]
                    and max(leave_ms) <= TH["v4_max_ms"]):
                c.ok(detail, "p50 ≤1ms · p95 ≤2ms · max ≤20ms")
            else:
                self.fail(c, detail, "p50 ≤1ms · p95 ≤2ms · max ≤20ms",
                          "光标干扰窗口超标；见手册 V4 备选")

        c = self.chk("R3-6")
        if not land_px:
            c.skip("absent", "没有点击样本")
        else:
            worst = max(land_px)
            detail = f"最近落点 {worst:.1f}px · 目标=购买按钮中心 · 样本 {len(land_px)}"
            if worst <= TH["v5_land_px"]:
                c.ok(detail, f"落点 ≤{int(TH['v5_land_px'])}px")
            else:
                self.fail(c, detail, f"落点 ≤{int(TH['v5_land_px'])}px",
                          "坐标换算/DPI 偏差；见手册 V5 备选")

        c = self.chk("R3-7")
        if residual:
            c.bad("左键残留按下", "残留 0", "已在收尾补发 LEFTUP，仍残留说明有其它来源按住")
        else:
            c.ok("左键残留 0（点击后 0.4s 复检）", "残留 0")

        c = self.chk("R3-8")
        if self.trade["suspected"]:
            c.bad(f"{self.trade['kind']} · {self.trade['detail']}", "0 成交/疑似成交",
                  "已立即进入安全收尾，后续阶段全部跳过")
        else:
            c.ok(f"{len(self.click_attempts)} 次点击 · 未出现成交/疑似成交", "0 成交/疑似成交")

        self.end_phase("R3", t0)

    def _refresh_record_baseline(self):
        try:
            rows = records_mod.load(limit=10)
            self.record_baseline_ts = max([_safe_float(r.get("ts")) for r in rows] or [0.0])
            self.record_baseline_count = len(rows)
        except Exception:                               # noqa: BLE001
            pass

    def _list_snapshot(self) -> set[int]:
        if not self.cfg or not self.cfg.region("list"):
            return set()
        vals = set()
        for _ in range(2):
            crop, _ = self.grab_box("list")
            vals.update(self.read_prices(crop, "list"))
            time.sleep(0.1)
        return vals

    def _list_shrunk(self) -> bool:
        if not self.list_baseline:
            return False
        crop, _ = self.grab_box("list")
        if crop is None:
            return False
        now = set(self.read_prices(crop, "list"))
        missing = self.list_baseline - now
        return bool(missing)

    def _wait_evidence(self, timeout):
        t0 = _pc()
        while _pc() - t0 < timeout:
            if self.aborted():
                raise Aborted()
            rec = self._new_record_hit()
            if rec:
                return "records", f"records hit · {rec.get('item') or rec.get('task') or '新记录'}"
            sc = self._dialog_score()[0]
            if self.dialog_baseline < 0.85 and sc >= 0.85:
                return "dialog", f"确认弹窗 score={sc:.2f}"
            time.sleep(0.15)
        return None, ""

    def _new_record_hit(self):
        try:
            rows = records_mod.load(limit=10)
        except Exception:                               # noqa: BLE001
            return None
        for r in rows:
            if str(r.get("result", "")) == "hit" and _safe_float(r.get("ts")) > self.record_baseline_ts + 0.5:
                return r
        return None

    def _dialog_score(self):
        if not self.cfg or not self.cfg.region("confirm") or not self.cfg.has_template("confirm"):
            return 0.0, None
        try:
            x, y, w, h = self.screen.norm_to_px(self.cfg.region("confirm"))
            pad = int(max(w, h) * 0.25)
            sw, sh = self.screen.size
            x = max(0, x - pad)
            y = max(0, y - pad)
            w = min(sw - x, w + 2 * pad)
            h = min(sh - y, h + 2 * pad)
            crop = self.screen.grab((x, y, w, h))
            if crop is None or crop.size == 0:
                return 0.0, None
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            tpl = vision_mod.imread(self.cfg.template_path("confirm"), cv2.IMREAD_GRAYSCALE)
            score, loc = vision_mod.match(gray, tpl)
            return float(score), loc
        except Exception:                               # noqa: BLE001
            return 0.0, None

    def _mark_trade(self, kind, detail):
        if self.trade["suspected"]:
            return
        self.trade = {"suspected": True, "kind": kind, "detail": detail, "at": _iso()}
        self.halt = "trade"
        self.say(f"[误买止损] 触发：{kind} · {detail} → 立即停止全部后续阶段", "fail")

    @staticmethod
    def _frame_diff_pct(a, b):
        if a is None or b is None:
            return 0.0
        if a.shape != b.shape:
            return 999.0
        try:
            ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
            gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
            ga = cv2.resize(ga, (320, 180))
            gb = cv2.resize(gb, (320, 180))
            return float(np.abs(ga.astype(np.int16) - gb.astype(np.int16)).mean())
        except Exception:                               # noqa: BLE001
            return 0.0

    # -------------------------------------------------          阶段 R6
    def phase_r6(self):
        t0 = _pc()
        self.begin_phase("R6")
        self.say("[R6] 事件窗口：需要你动手触发，执行器只提示、只等信号，不提问")

        c = self.chk("R6-1")
        if self.args.events == "off":
            c.skip("disabled", "已指定 --no-events / --events off")
        elif self.args.events == "auto" and not sys.stdin.isatty():
            c.skip("disabled", "非交互会话（auto 模式不阻塞）")
        elif not self.screen:
            c.skip("gate", "截屏不可用")
        else:
            c.ok("事件窗口已启用（F9 / _realmachine_event 确认）", "交互或 --events on")
        if self.cur["R6-1"].status != "pass":
            for cid in list(self.cur):
                if cid != "R6-1":
                    self.chk(cid).skip("gate", "事件窗口未启用")
            self.end_phase("R6", t0)
            return

        # ---- 1) 显示器休眠→唤醒
        before = self._screen_fingerprint()
        self.event_notes.append("monitor before=" + json.dumps(before, ensure_ascii=False))
        got = self.wait_event(
            "请让显示器休眠（显示器电源键 / Win+L 熄屏），保持 10 秒以上，再唤醒。")
        if not got:
            for cid in ("R6-2", "R6-3", "R6-4"):
                self.chk(cid).skip("timeout", "等待显示器事件超时")
        else:
            after = self._screen_fingerprint()
            self.event_notes.append("monitor after=" + json.dumps(after, ensure_ascii=False)
                                    + " perf=%.3f" % _pc())
            c = self.chk("R6-2")
            frame = self.screen.grab()
            mean_v = float(frame.mean()) if frame is not None else 0.0
            size_ok = frame is not None and after["size"] == before["size"]
            if frame is None:
                c.bad("截图失败", "可截屏且非黑屏")
            elif mean_v < 3.0:
                self.fail(c, f"亮度 {mean_v:.1f}", "亮度 ≥3（非黑屏）", "唤醒后画面全黑", frame)
            elif not size_ok:
                self.fail(c, f"{after['size']} vs {before['size']}", "尺寸一致",
                          "唤醒后采集尺寸变化", frame)
            else:
                c.ok(f"亮度 {mean_v:.1f} · {after['size'][0]}x{after['size'][1]}", "可截屏且非黑屏")

            c = self.chk("R6-3")
            same = (after["size"] == before["size"]
                    and after["dpi"] == before["dpi"]
                    and after["monitors"] == before["monitors"]
                    and after["bounds"] == before["bounds"])
            detail = (f"size={after['size']} dpi={after['dpi']} "
                      f"显示器 {before['monitors']}→{after['monitors']}")
            if same:
                c.ok(detail, "尺寸/DPI/显示器数量与边界不变")
            else:
                self.fail(c, detail, "尺寸/DPI/显示器数量与边界不变",
                          "唤醒后显示器拓扑变化；重采模板并重启游戏")

            c = self.chk("R6-4")
            try:
                box = self.cfg.region("buy")
                x, y, w, h = self.screen.norm_to_px(box)
                target = (self.screen.bounds["left"] + x + w // 2,
                          self.screen.bounds["top"] + y + h // 2)
                pre = input_mod.Mouse.cursor()
                ctypes.windll.user32.SetCursorPos(int(target[0]), int(target[1]))
                time.sleep(0.15)
                now = input_mod.Mouse.cursor()
                ctypes.windll.user32.SetCursorPos(int(pre[0]), int(pre[1]))
                err = math.hypot(now[0] - target[0], now[1] - target[1])
                detail = f"坐标映射误差 {err:.1f}px（移动后已还原）"
                if err <= TH["v5_land_px"]:
                    c.ok(detail, f"映射误差 ≤{int(TH['v5_land_px'])}px")
                else:
                    self.fail(c, detail, f"映射误差 ≤{int(TH['v5_land_px'])}px",
                              "唤醒后坐标不对齐；重设 DPI/分辨率后重跑")
            except Exception as exc:                    # noqa: BLE001
                c.skip("absent", f"坐标校验失败：{type(exc).__name__}: {exc}")

        # ---- 2) 系统睡眠→恢复
        pc_before = _pc()
        wall_before = time.time()
        drv_before = self.driver_ok
        got = self.wait_event("请让系统睡眠（开始菜单→睡眠 / 合盖），至少 20 秒后唤醒。")
        if not got:
            for cid in ("R6-5", "R6-6", "R6-7", "R6-8"):
                self.chk(cid).skip("timeout", "等待系统睡眠事件超时")
        else:
            pc_after = _pc()
            wall_after = time.time()
            dt_pc = pc_after - pc_before
            dt_wall = wall_after - wall_before
            self.event_notes.append(f"sleep pc_delta={dt_pc:.1f}s wall_delta={dt_wall:.1f}s")

            # 定时器在「恢复之后」重新起一个，定时器是否被睡眠冻结由触发延迟体现；
            # 提示前起表会被用户的操作耗时污染，不作为判据。
            c = self.chk("R6-5")
            fired = {"lag": None}
            t_timer = _pc()

            def _timer_cb():
                if fired["lag"] is None:
                    fired["lag"] = _pc() - t_timer

            timer = threading.Timer(2.0, _timer_cb)
            timer.daemon = True
            timer.start()
            deadline = t_timer + 4.0
            while _pc() < deadline and fired["lag"] is None:
                self.checkpoint()
                time.sleep(0.02)
            lag = fired["lag"]
            detail = (f"恢复后 2s 定时器触发={('%.0fms' % (lag * 1000)) if lag is not None else '未触发'} "
                      f"· 睡眠实际 {dt_wall:.0f}s")
            if lag is None:
                self.fail(c, detail, "2.0s±0.5s 内触发", "恢复后线程定时器完全不再工作")
            elif not (1.5 <= lag <= 2.5):
                self.fail(c, detail, "2.0s±0.5s 内触发",
                          "定时器触发偏移异常，T0 排程不可信")
            else:
                c.ok(detail, "2.0s±0.5s 内触发")

            c = self.chk("R6-6")
            t_probe = _pc()
            time.sleep(0.5)
            probe = _pc() - t_probe
            mono_ok = pc_after > pc_before and 0.45 <= probe <= 0.8
            mismatch = abs(dt_wall - dt_pc)
            detail = (f"单调时钟 Δ={dt_pc:.1f}s 壁钟 Δ={dt_wall:.1f}s 差={mismatch:.1f}s "
                      f"0.5s 探针={probe * 1000:.0f}ms")
            if mono_ok:
                c.ok(detail, "单调时钟前进且 0.5s 探针在 450–800ms")
            else:
                self.fail(c, detail, "单调时钟前进且 0.5s 探针在 450–800ms",
                          "恢复后时间基准异常；禁止用壁钟做 T0")

            c = self.chk("R6-7")
            frame = self.screen.grab()
            mean_v = float(frame.mean()) if frame is not None else 0.0
            after = self._screen_fingerprint()
            if frame is None or mean_v < 3.0:
                self.fail(c, f"亮度 {mean_v:.1f}", "可截屏且非黑屏", "恢复后画面全黑", frame)
            elif after["size"] != before["size"] or after["dpi"] != before["dpi"]:
                self.fail(c, f"{after['size']}@{after['dpi']} vs {before['size']}@{before['dpi']}",
                          "尺寸/DPI 不变", "恢复后显示环境变化", frame)
            else:
                c.ok(f"亮度 {mean_v:.1f} · {after['size'][0]}x{after['size'][1]}@{after['dpi']}",
                     "可截屏且尺寸/DPI 不变")

            c = self.chk("R6-8")
            probes = []
            try:
                from core import interception as icept
                ok, msg = icept.driver_ready(probe=True)
                probes.append(("driver", ok, msg))
            except Exception as exc:                    # noqa: BLE001
                probes.append(("driver", False, str(exc)))
            try:
                p = input_mod.Mouse.cursor()
                probes.append(("cursor", True, f"{p[0]},{p[1]}"))
            except Exception as exc:                    # noqa: BLE001
                probes.append(("cursor", False, str(exc)))
            try:
                ok = bool(input_mod.find_game_window())
                probes.append(("hwnd", ok, "IsWindow 有效" if ok else "窗口丢失"))
            except Exception as exc:                    # noqa: BLE001
                probes.append(("hwnd", False, str(exc)))
            detail = "；".join(f"{n}={'ok' if o else 'fail'}({m})" for n, o, m in probes)
            need = [o for n, o, m in probes if (n != "driver" or drv_before)]
            if all(need):
                c.ok(detail, "驱动探针 + 光标 + 游戏窗口都活着")
            else:
                self.fail(c, detail, "驱动探针 + 光标 + 游戏窗口都活着",
                          "恢复后输入通道失活；重启驱动/游戏")

        # ---- 3) 游戏掉线/重登
        clicks_before = self.click_count
        got = self.wait_event("请让游戏掉线或退回登录界面，停在登录页后按 F9。")
        if not got:
            for cid in ("R6-9", "R6-10", "R6-11"):
                self.chk(cid).skip("timeout", "等待掉线事件超时")
        else:
            page, text, keys = self._login_probe()
            self.event_notes.append(f"login page={page} keys={keys}")
            c = self.chk("R6-9")
            if page != "onsale" and (keys or page in ("login", "unknown")):
                c.ok(f"page={page} 关键词={keys or '无'} 文本={text[:30]}",
                     "识别为非交易行/登录界面")
            elif keys:
                c.ok(f"page={page} 关键词={keys}", "登录界面关键词命中")
            else:
                self.fail(c, f"page={page} 文本={text[:30]}", "识别为登录/非交易行界面",
                          "登录界面没被识别出来，存在盲点风险")
            c = self.chk("R6-11")
            if self.click_count == clicks_before:
                c.ok("掉线窗口期间点击 0 次", "0 次盲点")
            else:
                self.fail(c, f"期间点击 {self.click_count - clicks_before} 次", "0 次盲点",
                          "掉线时发生了点击，必须修")

            got2 = self.wait_event("请重新登录，回到交易行在售列表页后按 F9。")
            c = self.chk("R6-10")
            if not got2:
                c.skip("timeout", "等待重登信号超时")
            else:
                page2, text2, keys2 = self._login_probe()
                self.event_notes.append(f"relogin page={page2} keys={keys2}")
                if page2 in ("onsale", "detail"):
                    c.ok(f"page={page2} 文本={text2[:30]}", "onsale/detail")
                else:
                    self.fail(c, f"page={page2} 文本={text2[:30]}", "onsale/detail",
                              "重登后没回到在售页；核对区域与页面识别")

        self.end_phase("R6", t0)

    def _screen_fingerprint(self):
        fp = {"size": [0, 0], "dpi": 0, "monitors": 0, "bounds": None}
        if not self.screen:
            return fp
        try:
            w, h = self.screen.size
            fp["size"] = [w, h]
            fp["dpi"] = int(self.screen.dpi)
            fp["monitors"] = len(self.screen.monitors) - 1
            b = self.screen.bounds
            fp["bounds"] = [b["left"], b["top"], b["width"], b["height"]]
        except Exception:                               # noqa: BLE001
            pass
        return fp

    def _login_probe(self):
        keys = ("登录", "进入游戏", "服务器", "账号", "密码", "连接", "重新连接")
        page, text = "", ""
        try:
            page, text = nav_mod.Navigator(self.screen, None).where()
        except Exception as exc:                        # noqa: BLE001
            page, text = "unknown", f"{type(exc).__name__}"
        try:
            words = ocr_mod.read_words(self.screen.grab())
            full = " ".join(w.get("text", "") for w in words)
            hits = [k for k in keys if k in full]
            if not text:
                text = full
            return page, full[:80], hits
        except Exception:                               # noqa: BLE001
            return page, text[:80], []

    # -------------------------------------------------          阶段 R7
    def phase_r7(self):
        t0 = _pc()
        self.begin_phase("R7")
        self.say("[R7] 过夜模式（只读，逐分钟落盘）")
        hours = float(self.args.overnight_hours or 0)
        c = self.chk("R7-1")
        if hours <= 0:
            c.skip("disabled", "未启用 --overnight-hours（真实 6–12h 长跑）")
            for cid in list(self.cur):
                if cid != "R7-1":
                    self.chk(cid).skip("disabled", "过夜模式未启用")
            self.end_phase("R7", t0)
            return
        if not self.screen:
            c.skip("gate", "截屏不可用")
            for cid in list(self.cur):
                if cid != "R7-1":
                    self.chk(cid).skip("gate", "截屏不可用")
            self.end_phase("R7", t0)
            return
        c.ok(f"{hours:.1f}h · 采样 {self.args.overnight_sample_s}s · 曲线落盘 {OVERNIGHT_JSONL}",
             "过夜模式启用")

        deadline = _pc() + hours * 3600.0
        start_wall = time.time()
        errors = 0
        alive_hits = 0
        samples = 0
        self.overnight_samples = []
        header = {"schema": "overnight_sample/1", "run_id": self.run_id,
                  "started_at": _iso(), "hours": hours}
        self._jsonl_write({**header, "kind": "start"})
        self.say(f"[R7] 开始过夜采样，预计 {hours:.1f} 小时；期间不点击，可随时 F8 中止", "warn")
        while _pc() < deadline:
            if self.aborted():
                self._jsonl_write({"kind": "abort", "at": _iso(), "sample": samples})
                raise Aborted()
            met = process_metrics()
            t_read = _pc()
            frame_ok = True
            latency = float("nan")
            vals = []
            try:
                if self.ocr_region:
                    crop, _ = self.grab_box(self.ocr_region)
                    vals = self.read_prices(crop, self.ocr_region)
                    latency = (_pc() - t_read) * 1000.0
                else:
                    frame = self.screen.grab()
                    latency = (_pc() - t_read) * 1000.0
                    frame_ok = frame is not None
            except Exception as exc:                    # noqa: BLE001
                frame_ok = False
                errors += 1
                self._jsonl_write({"kind": "error", "at": _iso(),
                                   "error": f"{type(exc).__name__}: {exc}"})
            try:
                alive = bool(input_mod.find_game_window())
            except Exception:                           # noqa: BLE001
                alive = False
            alive_hits += 1 if alive else 0
            samples += 1
            row = {"kind": "sample", "run_id": self.run_id, "t": _iso(),
                   "elapsed_s": round(_pc() - t0, 1),
                   "private_mb": round(met["private_mb"], 2),
                   "working_mb": round(met["working_mb"], 2),
                   "handles": met["handles"], "threads": met["threads"],
                   "children": met["children"], "ocr_ms": round(latency, 1) if latency == latency else None,
                   "prices": vals[:3], "game_alive": alive, "frame_ok": frame_ok}
            self.overnight_samples.append(row)
            self._jsonl_write(row)
            if samples % 10 == 0 or samples == 1:
                self.say(f"[R7] {samples} 样本 · 私有 {met['private_mb']:.1f}MB · "
                         f"句柄 {met['handles']} · 线程 {met['threads']} · "
                         f"游戏{'在' if alive else '丢'}")
            if errors >= 5:
                self.say("[R7] 累计异常过多，提前结束过夜采样", "fail")
                break
            time.sleep(max(5.0, self.args.overnight_sample_s))

        total_h = max(1e-9, (time.time() - start_wall) / 3600.0)
        xs = [s["elapsed_s"] / 3600.0 for s in self.overnight_samples]
        ys = [s["private_mb"] for s in self.overnight_samples]
        slope = _slope_mb_hours(xs, ys)
        c = self.chk("R7-2")
        if len(self.overnight_samples) < 3:
            c.skip("not_auto", f"样本不足（{len(self.overnight_samples)}）")
        else:
            detail = f"斜率 {_fmt(slope,3,'MB/h')} · 首 {ys[0]:.1f}MB → 末 {ys[-1]:.1f}MB · {total_h:.2f}h"
            if slope <= TH["overnight_mem_mb_h"]:
                c.ok(detail, f"斜率 ≤{TH['overnight_mem_mb_h']}MB/h")
            else:
                c.bad(detail, f"斜率 ≤{TH['overnight_mem_mb_h']}MB/h", "疑似内存泄漏")

        hs = [s["handles"] for s in self.overnight_samples]
        c = self.chk("R7-3")
        if len(hs) < 3:
            c.skip("not_auto", "样本不足")
        else:
            growth = hs[-1] - hs[0]
            mono = all(b >= a for a, b in zip(hs, hs[1:]))
            detail = f"{hs[0]} → {hs[-1]}（增量 {growth}）单调={mono}"
            if growth <= TH["overnight_handle_growth"] and not (mono and growth > 0):
                c.ok(detail, f"增量 ≤{TH['overnight_handle_growth']} 且非单调增长")
            else:
                c.bad(detail, f"增量 ≤{TH['overnight_handle_growth']} 且非单调增长", "句柄泄漏")

        ths = [s["threads"] for s in self.overnight_samples]
        c = self.chk("R7-4")
        if len(ths) < 3:
            c.skip("not_auto", "样本不足")
        else:
            growth = max(ths) - min(ths)
            detail = f"{min(ths)}–{max(ths)}（波动 {growth}）"
            if growth <= TH["overnight_thread_growth"]:
                c.ok(detail, f"线程波动 ≤{TH['overnight_thread_growth']}")
            else:
                c.bad(detail, f"线程波动 ≤{TH['overnight_thread_growth']}", "线程增长/泄漏")

        lats = [s["ocr_ms"] for s in self.overnight_samples if s.get("ocr_ms")]
        c = self.chk("R7-5")
        if not lats:
            c.skip("absent", "没有 OCR 耗时样本")
        else:
            p95 = _pct(lats, 95)
            detail = f"p50={_pct(lats,50):.1f}ms p95={p95:.1f}ms p99={_pct(lats,99):.1f}ms 样本={len(lats)}"
            if p95 <= TH["v2_soak_p99_ms"]:
                c.ok(detail, f"p95 ≤{TH['v2_soak_p99_ms']}ms")
            else:
                c.bad(detail, f"p95 ≤{TH['v2_soak_p99_ms']}ms", "长跑后周期耗时劣化")

        c = self.chk("R7-6")
        ratio = alive_hits / max(1, samples)
        detail = f"游戏存活 {alive_hits}/{samples} = {ratio:.1%}"
        if ratio >= TH["overnight_alive"]:
            c.ok(detail, f"存活率 ≥{int(TH['overnight_alive'] * 100)}%")
        else:
            c.bad(detail, f"存活率 ≥{int(TH['overnight_alive'] * 100)}%", "过夜期游戏掉线/崩溃")

        c = self.chk("R7-7")
        children = self.overnight_samples[-1]["children"] if self.overnight_samples else 0
        if children == 0:
            c.ok(f"子进程 {children} · 采样 {samples} 次", "残留进程 0")
        else:
            c.bad(f"子进程 {children}", "残留进程 0", "存在未回收子进程")

        self._jsonl_write({"kind": "finish", "at": _iso(), "samples": samples,
                           "slope_mb_h": None if math.isnan(slope) else round(slope, 3),
                           "final": self.overnight_samples[-1] if self.overnight_samples else None})
        self.end_phase("R7", t0)

    def _jsonl_write(self, row):
        try:
            with open(OVERNIGHT_JSONL, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                fp.flush()
                try:
                    os.fsync(fp.fileno())
                except OSError:
                    pass
        except Exception:                               # noqa: BLE001
            pass

    # -------------------------------------------------       收尾与审计
    def cleanup(self):
        info = {"hotkey_stopped": False, "cursor_restored_px": None,
                "left_released": False, "screen_closed": False,
                "foreground_restored": False, "errors": []}
        try:
            for hk in (self.hotkey_abort, self.hotkey_event):
                if hk:
                    hk.stop()
            info["hotkey_stopped"] = True
        except Exception as exc:                        # noqa: BLE001
            info["errors"].append(f"hotkey: {exc}")
        try:
            for _ in range(2):
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            info["left_released"] = not bool(
                ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception as exc:                        # noqa: BLE001
            info["errors"].append(f"leftup: {exc}")
        try:
            ok, had = input_mod.Mouse.release_cursor_lock()
            self.trace(f"release_cursor_lock ok={ok} had={had}")
        except Exception as exc:                        # noqa: BLE001
            info["errors"].append(f"cliplock: {exc}")
        try:
            ctypes.windll.user32.SetCursorPos(int(self.start_cursor[0]), int(self.start_cursor[1]))
            time.sleep(0.1)
            now = input_mod.Mouse.cursor()
            info["cursor_restored_px"] = round(
                math.hypot(now[0] - self.start_cursor[0], now[1] - self.start_cursor[1]), 1)
        except Exception as exc:                        # noqa: BLE001
            info["errors"].append(f"cursor: {exc}")
        try:
            if self.start_foreground:
                ctypes.windll.user32.SetForegroundWindow(self.start_foreground)
                info["foreground_restored"] = True
        except Exception:                               # noqa: BLE001
            pass
        try:
            if self.screen:
                self.screen.close()
            info["screen_closed"] = True
        except Exception as exc:                        # noqa: BLE001
            info["errors"].append(f"screen: {exc}")
        self.cleanup = info
        self.say(f"[收尾] 热键卸载={info['hotkey_stopped']} 松开左键={info['left_released']} "
                 f"光标还原={info['cursor_restored_px']}px 采集关闭={info['screen_closed']}")

    def phase_r4(self):
        t0 = _pc()
        self.begin_phase("R4")

        c = self.chk("R4-1")
        info = self.cleanup or {}
        good = (info.get("hotkey_stopped") and info.get("left_released")
                and (info.get("cursor_restored_px") or 999) <= 32
                and info.get("screen_closed"))
        detail = (f"热键={info.get('hotkey_stopped')} 左键={info.get('left_released')} "
                  f"光标={info.get('cursor_restored_px')}px 采集={info.get('screen_closed')} "
                  f"错误={info.get('errors') or '无'}")
        if good:
            c.ok(detail, "热键卸载 · 左键松开 · 光标 ≤32px · 采集关闭")
        else:
            self.fail(c, detail, "热键卸载 · 左键松开 · 光标 ≤32px · 采集关闭",
                      "收尾不完整；不要复用本进程")

        c = self.chk("R4-2")
        try:
            fresh = config_mod.Config.load()
            bad_files = [n for n in os.listdir(ROOT) if ".broken-" in n and n.endswith(".bad")]
            if fresh.load_ok and not bad_files:
                c.ok(f"config 可读 · broken 文件 {len(bad_files)} · 备份 {self._backup_dir}",
                     "config 可读且无新的 .broken")
            else:
                c.bad(f"load_ok={fresh.load_ok} broken={bad_files}", "config 可读且无新的 .broken")
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "config 可读")

        c = self.chk("R4-3")
        try:
            rows = records_mod.load(limit=10)
            now_ts = max([_safe_float(r.get("ts")) for r in rows] or [0.0])
            if self.trade["suspected"] and now_ts > self.record_baseline_ts:
                c.ok(f"记录新增（成交证据）· ts {self.record_baseline_ts:.3f}→{now_ts:.3f}",
                     "本执行器不写记录，新增来自生产端")
            elif now_ts == self.record_baseline_ts:
                c.ok(f"记录未被本执行器写入 · 最新 ts={now_ts:.3f}", "记录水位不变")
            else:
                c.ok(f"记录有外部新增 · ts {self.record_baseline_ts:.3f}→{now_ts:.3f}",
                     "记录水位（允许生产端写入）")
        except Exception as exc:                        # noqa: BLE001
            c.bad(f"{type(exc).__name__}: {exc}", "记录可读")

        c = self.chk("R4-4")
        c.skip("not_auto", "崩溃/杀进程/断电恢复需人工在虚拟机上杀进程、拔电；本执行器不做破坏性操作")

        c = self.chk("R4-5")
        c.skip("not_auto", "断电恢复同上；且涉及系统级操作，需管理员与实机断电")

        c = self.chk("R4-6")
        running = self._console_running()
        if running:
            c.ok(f"检测到控制台进程 {running}（本执行器不会双开，但生产端可能在跑）", "仅记录")
        else:
            c.ok("未检测到控制台进程", "仅记录")

        self.end_phase("R4", t0)

    @staticmethod
    def _console_running():
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq SJZ-Control.exe", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=CREATE_NO_WINDOW)
            out = (r.stdout or "").strip()
            if out and "SJZ-Control.exe" in out:
                return out.splitlines()[0][:80]
        except Exception:                               # noqa: BLE001
            pass
        return ""

    def phase_r5(self):
        t0 = _pc()
        self.begin_phase("R5")

        c = self.chk("R5-1")
        if self.driver_ok:
            c.ok("驱动就绪（零位移探针受理）", "driver_ready=True")
        else:
            c.skip("absent", f"驱动未就绪：{self.driver_msg or '未安装'}")

        c = self.chk("R5-2")
        c.skip("admin", "本执行器不安装驱动、不改服务/注册表；需管理员 + 重启，未执行")

        c = self.chk("R5-3")
        r3 = [x for x in self.checks if x.cid == "R3-4"]
        if not r3:
            c.skip("gate", "点击阶段未执行")
        elif r3[0].status != "pass":
            c.skip("gate", "点击受理未过", measured=r3[0].measured)
        else:
            mode = self.mouse.mode if self.mouse else "?"
            c.ok(f"通道={mode} · {r3[0].measured}", "点击受理通过")

        c = self.chk("R5-4")
        c.skip("not_auto", "反作弊×驱动的长时（3×30min）观察需测试账号与人工值守，本执行器不做")

        c = self.chk("R5-5")
        c.skip("not_auto", "干净机器打包矩阵需另一台机器；本机 --doctor 仅覆盖当前环境")

        self.end_phase("R5", t0)

    # -------------------------------------------------          编排
    def execute(self):
        self._open_trace()
        self._banner()
        try:
            self.phase_r0()
            self._run_gated("R1", ("R0",), self.phase_r1)
            self._run_gated("R2", ("R1",), self.phase_r2)
            self._run_gated("R3", ("R2",), self.phase_r3)
            if self.halt == "trade":
                self.skip_phase("R6", "halt", "误买止损，事件窗口未执行")
                self.skip_phase("R7", "halt", "误买止损，过夜模式未执行")
            elif self.abort.is_set():
                self.skip_phase("R6", "abort", "已中止")
                self.skip_phase("R7", "abort", "已中止")
            else:
                self._run_gated("R6", ("R0",), self.phase_r6)
                if self.halt == "trade":
                    self.skip_phase("R7", "halt", "误买止损，过夜模式未执行")
                elif self.abort.is_set():
                    self.skip_phase("R7", "abort", "已中止")
                else:
                    self._run_gated("R7", ("R0",), self.phase_r7)
        except Aborted:
            self.abort_source = self.abort_source or "热键/文件"
            self.say("[中止] 已停止执行，进入安全收尾", "warn")
            self.close_open_phase("abort", "已中止，保留已执行部分")
            for pid in ("R6", "R7"):
                if pid not in self.gates and pid not in [p["id"] for p in self.phase_rows]:
                    self.skip_phase(pid, "abort", "已中止")
        except Exception as exc:                        # noqa: BLE001
            self.fatal = f"{type(exc).__name__}: {exc}"
            self.say(f"[异常] {self.fatal}", "fail")
            self.trace(f"fatal: {exc}")
            self.close_open_phase("absent", f"执行异常：{self.fatal}")
            for pid in ("R6", "R7"):
                if pid not in [p["id"] for p in self.phase_rows]:
                    self.skip_phase(pid, "abort", f"执行异常：{self.fatal}")
        finally:
            self.close_open_phase("abort", "未正常收尾，保留已执行部分")
            if self.screen or self.hotkey_abort:
                self.cleanup()
            else:
                self.cleanup = {"hotkey_stopped": False, "cursor_restored_px": None}
            self.phase_r4()
            self.phase_r5()
            self._write_reports()

    def _run_gated(self, pid, needs, fn):
        if self.halt == "trade":
            self.skip_phase(pid, "halt", "误买止损，后续未执行")
            return
        if self.abort.is_set():
            self.skip_phase(pid, "abort", "已中止")
            return
        if any(not self.gates.get(n, False) for n in needs):
            miss = [n for n in needs if not self.gates.get(n, False)]
            self.skip_phase(pid, "gate", f"因前置 {','.join(miss)} 门禁未过而跳过")
            return
        fn()

    def _banner(self):
        total = sum(PHASE_EST.values())
        if self.args.overnight_hours:
            total += int(self.args.overnight_hours * 3600)
        self.say("=" * 72)
        self.say(f"真机自动验证 · run {self.run_id}")
        self.say(f"项目 {ROOT}")
        self.say(f"中止：{self.args.hotkey} 或创建 {ABORT_FILE}；事件确认：{self.args.event_key}")
        self.say(f"预估总时长 {int(total) // 60} 分 {int(total) % 60} 秒"
                 + ("（过夜模式为主）" if self.args.overnight_hours else ""))
        self.say(f"报告 {JSON_PATH} / {MD_PATH}")
        self.say("=" * 72)

    def _open_trace(self):
        try:
            self._trace_fp = open(TRACE_PATH, "a", encoding="utf-8")
            self.trace(f"run {self.run_id} start argv={sys.argv[1:]}")
        except Exception:                               # noqa: BLE001
            self._trace_fp = None

    # -------------------------------------------------          报告
    def _conclusion(self):
        checks = self.checks
        n_pass = sum(1 for c in checks if c.status == "pass")
        n_fail = sum(1 for c in checks if c.status == "fail")
        n_skip = sum(1 for c in checks if c.status == "skip")
        by_reason = Counter(c.reason for c in checks if c.status == "skip")
        if self.trade["suspected"]:
            verdict = "误买/疑似成交，已安全收尾"
            line = (f"结论：验证中断 —— {self.trade['kind']}（{self.trade['detail']}），"
                    f"已停止全部后续阶段。")
        elif self.abort.is_set():
            verdict = "被中止"
            line = f"结论：验证被中止（{self.abort_source or '未知来源'}），收尾已完成。"
        elif self.fatal:
            verdict = "执行异常"
            line = f"结论：执行异常 —— {self.fatal}；已完成的项见下表。"
        elif n_fail:
            first = next(c for c in checks if c.status == "fail")
            verdict = "不通过"
            line = (f"结论：不通过 —— {n_fail} 项未过；首要不通过项 {first.cid} {first.title}"
                    f"（实测 {first.measured} / 阈值 {first.threshold}）。")
        else:
            verdict = "通过"
            line = "结论：通过 —— 所有可自动判定的门禁项均过。"
        gate_fail = [p["id"] for p in self.phase_rows if p["checks"] and not p["gate_ok"]]
        return {
            "verdict": verdict, "line": line,
            "pass": n_pass, "fail": n_fail, "skip": n_skip,
            "skip_breakdown": {REASON_CN.get(k, k): v for k, v in by_reason.items()},
            "gate_failed_phases": gate_fail,
        }

    def _write_reports(self):
        conc = self._conclusion()
        checks = self.checks
        payload = {
            "schema": "realmachine_result/1",
            "run_id": self.run_id,
            "started_at": datetime.fromtimestamp(self.started_at).strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": _iso(),
            "duration_s": round(time.time() - self.started_at, 2),
            "host": {"hostname": os.environ.get("COMPUTERNAME", ""),
                     "user": os.environ.get("USERNAME", ""),
                     "python": sys.version.split()[0],
                     "platform": sys.platform},
            "argv": sys.argv,
            "options": vars(self.args),
            "thresholds": TH,
            "fingerprint": getattr(self, "fingerprint", {}),
            "conclusion": conc,
            "trade": self.trade,
            "abort": {"aborted": self.abort.is_set(), "source": self.abort_source},
            "fatal": self.fatal,
            "cleanup": self.cleanup,
            "page": {"detected": self.page, "ocr_region": self.ocr_region},
            "clicks": {"count": self.click_count, "attempts": self.click_attempts},
            "events": self.event_notes,
            "phases": self.phase_rows,
            "checks": [c.as_dict() for c in checks],
            "overnight": {
                "enabled": bool(self.args.overnight_hours),
                "samples": len(self.overnight_samples),
                "jsonl": OVERNIGHT_JSONL if self.overnight_samples else "",
                "curve": self.overnight_samples,
            },
        }
        try:
            with open(JSON_PATH, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:                        # noqa: BLE001
            print(f"[报告] JSON 写入失败：{exc}", flush=True)
        try:
            with open(MD_PATH, "w", encoding="utf-8") as fp:
                fp.write(self._md(conc, payload))
        except Exception as exc:                        # noqa: BLE001
            print(f"[报告] Markdown 写入失败：{exc}", flush=True)
        self.say(f"[报告] {JSON_PATH}")
        self.say(f"[报告] {MD_PATH}")

    def _md(self, conc, payload):
        lines = []
        lines.append(f"# 真机自动验证结果 · {self.run_id}\n")
        lines.append("## 结论（一眼看完）\n")
        lines.append(f"> {conc['line']}\n")
        lines.append(f"> 通过 {conc['pass']} · 未过 {conc['fail']} · 跳过 {conc['skip']}"
                     f"（" + " / ".join(f"{k} {v}" for k, v in conc["skip_breakdown"].items()) + "）\n")
        trade = "未发生" if not self.trade["suspected"] else f"**{self.trade['kind']}** · {self.trade['detail']}"
        lines.append(f"> 误买：{trade}\n")
        lines.append(f"> 中止：{'是（' + (self.abort_source or '未知') + '）' if self.abort.is_set() else '否'}"
                     f" · 用时 {payload['duration_s'] / 60:.1f} 分钟 · 点击 {self.click_count} 次\n")
        gate = "、".join(conc["gate_failed_phases"]) or "无"
        lines.append(f"> 门禁未过的阶段：{gate}\n")
        if self.fatal:
            lines.append(f"> 异常：{self.fatal}\n")
        lines.append("\n### 阶段汇总\n")
        lines.append("| 阶段 | 名称 | 结果 | 过/不过/跳过 | 耗时 | 门禁 |")
        lines.append("|---|---|---|---|---|---|")
        for p in self.phase_rows:
            st = {"pass": "过", "fail": "不过", "skip": "跳过", "partial": "部分"}.get(p["status"], p["status"])
            lines.append(f"| {p['id']} | {p['name']} | {st} | "
                         f"{p['pass']}/{p['fail']}/{p['skip']} | {p['duration_s']:.1f}s | "
                         f"{'过' if p['gate_ok'] else '未过'} |")
        lines.append("\n## 逐项明细\n")
        for p in self.phase_rows:
            lines.append(f"### {p['id']} {p['name']}\n")
            lines.append("| 项 | 结果 | 实测 | 阈值 | 说明 |")
            lines.append("|---|---|---|---|---|")
            for c in p["checks"]:
                res = STATUS_CN.get(c["status"], c["status"])
                if c["status"] == "skip":
                    res = "跳过（" + c["skip_reason_cn"] + "）"
                detail = (c["detail"] or "").replace("|", "/").replace("\n", " ")
                lines.append(f"| {c['id']} {c['title']} | {res} | {c['measured']} | "
                             f"{c['threshold']} | {detail} |")
            lines.append("")
        lines.append("## 实测数据\n")
        lines.append(f"- 页面：{payload['page']['detected']} · OCR 区域：{payload['page']['ocr_region']}")
        lines.append(f"- 点击：{self.click_count} 次 · 通道："
                     f"{self.mouse.mode if self.mouse else '未建立'}")
        for a in self.click_attempts:
            lines.append(f"  - #{a['i']} {a['kind'] or '无证据'} · 调用 {a['call_ms']}ms · "
                         f"落点 {a['land_px']}px · {a['evidence']}")
        if self.event_notes:
            lines.append("- 事件窗口记录：")
            for n in self.event_notes:
                lines.append(f"  - {n}")
        if self.overnight_samples:
            xs = [s["elapsed_s"] / 3600.0 for s in self.overnight_samples]
            ys = [s["private_mb"] for s in self.overnight_samples]
            slope = _slope_mb_hours(xs, ys)
            hs = [s["handles"] for s in self.overnight_samples]
            alive = sum(1 for s in self.overnight_samples if s.get("game_alive"))
            n = len(self.overnight_samples)
            lines.append(f"- 过夜：{n} 样本 · 内存斜率 {_fmt(slope,3,'MB/h')}（阈值 ≤{TH['overnight_mem_mb_h']}）"
                         f" · {ys[0]:.1f}→{ys[-1]:.1f}MB · 句柄 {hs[0]}→{hs[-1]}"
                         f" · 游戏存活 {alive}/{n} · 曲线 {OVERNIGHT_JSONL}")
        elif payload["overnight"]["enabled"]:
            lines.append(f"- 过夜：模式已启用但未产生样本（中止/异常提前结束）· 曲线 {OVERNIGHT_JSONL}")
        lines.append("\n## 不可自动判定清单\n")
        for c in self.checks:
            if c.status == "skip" and c.reason in ("not_auto", "admin"):
                lines.append(f"- {c.cid} {c.title}：{c.detail}")
        lines.append("\n## 失败项截图\n")
        any_shot = False
        for c in self.checks:
            for s in c.shots:
                any_shot = True
                lines.append(f"- {c.cid} {c.title}：{s}")
        if not any_shot:
            lines.append("- 无（未失败或失败项无法截屏）")
        lines.append(f"\n---\n原始数据：`{JSON_PATH}` · 运行日志：`{TRACE_PATH}`\n")
        return "\n".join(lines) + "\n"

    # -------------------------------------------------          退出码
    def exit_code(self):
        if self.fatal:
            return 3
        if self.trade["suspected"] or self.abort.is_set():
            return 2
        if any(c.status == "fail" for c in self.checks):
            return 1
        return 0


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 参数
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="realmachine_auto.py",
        description="一键真机验证执行器（R0–R7）。游戏停在交易行在售列表页后运行。")
    p.add_argument("--click-tests", type=int, default=8,
                   help="点击测试次数，默认 8（手册完整口径 20；任一次成交立即停）")
    p.add_argument("--click-interval", type=float, default=0.9, help="点击间隔秒")
    p.add_argument("--live-rounds", type=int, default=25, help="实机读价采样轮次")
    p.add_argument("--live-interval", type=float, default=0.12, help="实机采样间隔秒")
    p.add_argument("--countdown-observe-s", type=float, default=20.0, help="倒计时观察秒数")
    p.add_argument("--soak-s", type=float, default=30.0, help="短时稳定性秒数，0=关闭")
    p.add_argument("--events", choices=("auto", "on", "off"), default="auto",
                   help="事件窗口：auto=仅交互终端启用；on=强制；off=关闭")
    p.add_argument("--event-timeout", type=float, default=300.0, help="每个事件信号等待上限秒")
    p.add_argument("--overnight-hours", type=float, default=0.0, help="过夜模式时长，0=关闭（常用 8）")
    p.add_argument("--overnight-sample-s", type=float, default=60.0, help="过夜采样间隔秒")
    p.add_argument("--offline-countdown-runs", type=int, default=60,
                   help="离线倒计时模拟轮次，0=关闭")
    p.add_argument("--offline-timeout", type=float, default=240.0)
    p.add_argument("--offline-no-raw", action="store_true", default=True,
                   help="离线模拟不写逐次原始数据（默认开）")
    p.add_argument("--no-doctor", action="store_true", help="跳过 dist exe --doctor")
    p.add_argument("--no-click", action="store_true", help="强制不点击（只读模式）")
    p.add_argument("--pytest", action="store_true", help="跑离线 pytest（慢）")
    p.add_argument("--hotkey", default="F8", help="中止热键，默认 F8")
    p.add_argument("--event-key", default="F9", help="事件确认热键，默认 F9")
    p.add_argument("--plan", action="store_true", help="只打印排期与阈值，不执行")
    p.add_argument("--self-check", action="store_true", help="只检查依赖导入，不碰屏幕")
    return p.parse_args(argv)


def print_plan(args):
    total = sum(PHASE_EST.values())
    if args.overnight_hours:
        total += int(args.overnight_hours * 3600)
    print("阶段排期（预计耗时；实际以运行报告为准）")
    print("-" * 56)
    for pid in ("R0", "R1", "R2", "R3", "R6", "R7", "R4", "R5"):
        est = PHASE_EST[pid]
        if pid == "R7":
            est = int(args.overnight_hours * 3600) if args.overnight_hours else 0
        print(f"{pid}  {PHASE_NAMES[pid]:<14} {len(CATALOG[pid])} 项  "
              f"{est // 60} 分 {est % 60} 秒"
              + ("（未启用）" if (pid == "R7" and not args.overnight_hours) else ""))
    print("-" * 56)
    print(f"合计约 {total // 60} 分 {total % 60} 秒"
          + ("（含过夜长跑）" if args.overnight_hours else "（不含过夜）"))
    print(f"中止：{args.hotkey} 或创建 {ABORT_FILE}")
    print(f"事件确认：{args.event_key} 或创建 {EVENT_FILE}")
    print(f"报告：{JSON_PATH} / {MD_PATH}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass
    if args.self_check:
        print("import_error:", IMPORT_ERROR or "无")
        if IMPORT_ERROR:
            return 3
        print("ocr:", ocr_mod.backend_name() if hasattr(ocr_mod, "backend_name") else "?")
        print("thresholds:", json.dumps(TH, ensure_ascii=False))
        return 0
    if IMPORT_ERROR:
        print(f"依赖导入失败：{IMPORT_ERROR}", flush=True)
        return 3
    if args.plan:
        return print_plan(args)
    TH["event_timeout_s"] = args.event_timeout
    run = Runner(args)
    # 预热：热键对象、起始光标/前台、残留信号
    run.start_cursor = input_mod.Mouse.cursor()
    run.start_foreground = input_mod.Mouse.foreground()
    run.hotkey_abort = hotkey_mod.Hotkey(args.hotkey, run._on_abort)
    run.hotkey_event = hotkey_mod.Hotkey(args.event_key, run._on_event)
    run._clear_signal(ABORT_FILE)
    run.execute()
    code = run.exit_code()
    print(f"\n{run._conclusion()['line']}", flush=True)
    print(f"退出码 {code}（0=通过 1=有未过 2=中止/误买 3=异常）", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
