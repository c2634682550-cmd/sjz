# -*- coding: utf-8 -*-
"""会话池/两档引擎的内存与句柄代价：真机长期挂机前必须知道。

测：进程启动 → 主线程建小图 session → 主线程建大图 session
→ 3 个 scan-ocr 线程各建一个小图 session，记录工作集、私有字节、句柄数增长。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.environ.get("BENCH_SNAP", os.path.join(BASE, "ocr", "work"))
sys.path.insert(0, SNAP)

import cv2
import numpy as np
from core import ocr_rapid

K32 = ctypes.WinDLL("kernel32", use_last_error=True)
PSAPI = ctypes.WinDLL("psapi", use_last_error=True)
K32.GetCurrentProcess.restype = ctypes.c_void_p
PSAPI.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
PSAPI.GetProcessMemoryInfo.restype = wt.BOOL
K32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
K32.GetProcessHandleCount.restype = wt.BOOL


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def mem():
    m = PMC()
    m.cb = ctypes.sizeof(m)
    PSAPI.GetProcessMemoryInfo(K32.GetCurrentProcess(), ctypes.byref(m), m.cb)
    return m.WorkingSetSize / 1048576.0, m.PeakWorkingSetSize / 1048576.0


def handles():
    n = wt.DWORD(0)
    K32.GetProcessHandleCount(K32.GetCurrentProcess(), ctypes.byref(n))
    return n.value


def load(rel):
    p = os.path.join(SNAP, "tests", "fixtures", rel)
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


detail = load("market/detail_panel.png")
card = load("market/list_unfiltered.png")[10:110, 20:320]

b = ocr_rapid.make({"threads": 4, "big_threads": 24})
ws0, peak0 = mem()
h0 = handles()
print(f"起点: WS={ws0:.0f}MB peak={peak0:.0f}MB handles={h0}")
b.read_words(card)
ws1, _ = mem()
print(f"+小图 session: WS={ws1:.0f}MB (+{ws1 - ws0:.0f}) handles={handles()} "
      f"(+{handles() - h0})")
b.read_words(detail)
ws2, _ = mem()
print(f"+大图 session: WS={ws2:.0f}MB (+{ws2 - ws1:.0f}) handles={handles()} "
      f"(+{handles() - h0})")

errs = []


def worker():
    try:
        for _ in range(5):
            b.read_words(card)
    except Exception as e:                         # noqa: BLE001
        errs.append(repr(e))


ts = [threading.Thread(target=worker, name=f"scan-ocr-{i}") for i in range(3)]
for t in ts:
    t.start()
for t in ts:
    t.join()
ws3, peak3 = mem()
print(f"+3 个 worker session: WS={ws3:.0f}MB (+{ws3 - ws2:.0f}) "
      f"peak={peak3:.0f}MB handles={handles()} (+{handles() - h0}) errs={errs}")
print(f"合计: WS +{ws3 - ws0:.0f}MB, handles +{handles() - h0}")
