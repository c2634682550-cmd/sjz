"""时序组持锁期间的优先级保护。

为什么需要它（而不是只靠文件锁）
--------------------------------
文件锁只保证「同一时刻全机只有一个 heavy 用例在跑」。但同机还可能有别的
pytest 会话在跑**轻量**用例（这是设计允许的），以及游戏本体、OCR 服务、
长跑采集等外部进程。这台机器实测在并发场景下会 100% 占满 16 个逻辑核，
2ms 级的 lag 断言会被进程调度延迟直接击穿。

所以时序组在**持锁期间**把当前 pytest 进程提到 HIGH，跑完立刻还原。
因为锁已经保证全机只有一个时序用例在跑，提升优先级不会造成多个 HIGH
互相抢占，只是让它在被外部普通优先级负载挤到时还能拿回时间片。

非 Windows 平台是空操作（POSIX 由 nice/实时调度策略决定，不在这里改）。
"""
from __future__ import annotations

import ctypes
import os

if os.name == "nt":                                     # pragma: no cover - 平台分支
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.GetCurrentProcess.restype = ctypes.c_void_p
    _K32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    _K32.GetPriorityClass.restype = ctypes.c_uint32
    _K32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _K32.SetPriorityClass.restype = ctypes.c_bool

_HIGH_PRIORITY_CLASS = 0x0080
_NORMAL_PRIORITY_CLASS = 0x0020
_BELOW_NORMAL_PRIORITY_CLASS = 0x4000
_ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
_REALTIME_PRIORITY_CLASS = 0x0100
_BOOSTABLE = {_NORMAL_PRIORITY_CLASS, _BELOW_NORMAL_PRIORITY_CLASS,
              _ABOVE_NORMAL_PRIORITY_CLASS}


def boost_for_timing() -> int | None:
    """把当前进程提到 HIGH。返回旧的优先级类，供 :func:`restore` 使用。

    已经是 HIGH/REALTIME 的不动（避免和用户/环境设置打架）。
    """
    if os.name != "nt":                                 # pragma: no cover - 平台分支
        return None
    handle = _K32.GetCurrentProcess()
    old = _K32.GetPriorityClass(handle)
    if old not in _BOOSTABLE:
        return None
    if not _K32.SetPriorityClass(handle, _HIGH_PRIORITY_CLASS):
        return None
    return int(old)


def restore(old: int | None) -> None:
    """还原优先级。失败静默 —— 不能因为收尾失败把测试带崩。"""
    if os.name != "nt" or old is None:                  # pragma: no cover - 平台分支
        return
    try:
        _K32.SetPriorityClass(_K32.GetCurrentProcess(), int(old))
    except Exception:
        pass
