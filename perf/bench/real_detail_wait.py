"""在**当前代码树**上用真 OCR 测详情等待（不用任何单价假设表）。

测四件事（全部真实墙钟、真实 `core.ocr`、真实 `core.engine` 方法）：

  1. 判据读本身的耗时：`_read_serial`（老路径，含二级探活） vs
     `_read_serial_fast`（新路径，只碰一级）；
  2. 空帧会不会触发 1.5s 探活：老路径每 10s（`probe_ttl_s`）探一次，
     20 次里能抓到 1.5s 长尾；新路径探活次数必须为 0；
  3. `_wait_detail_ready` 的真实命中/未命中耗时（含换页窗口）与十连开；
  4. 编号一致性：等待一结束立刻再读一次，必须与本次点开的行逐位相同。

仓库里没有真实的详情面板整屏截图（`_rm_shots` 是列表页，编号区是空的），
所以两帧「旧编号/新编号」用 cv2 画成 340×30、3× 放大，与真实 ROI 同尺寸；
OCR 耗时主要取决于区域尺寸与文字行数，与画面内容基本无关。

用法：.venv\\Scripts\\python.exe perf\\bench\\real_detail_wait.py
"""
import json
import os
import statistics
import sys
import threading
import time

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# 要测的代码树：默认 perf/merged（合并快照，含本轮改动）；传 main 测主仓基线。
ROOT_ARG = (sys.argv[1] if len(sys.argv) > 1 else "perf/merged")
ROOT = ROOT_ARG if os.path.isabs(ROOT_ARG) else os.path.join(PROJ, ROOT_ARG)
sys.path.insert(0, ROOT)

import cv2                      # noqa: E402
import numpy as np              # noqa: E402

from core import ocr            # noqa: E402
from core.engine import Engine  # noqa: E402
from core.nav_coords import N_SERIAL  # noqa: E402  (归一化 ROI)

HAS_FAST = hasattr(Engine, "_read_serial_fast")
OUT_NAME = "p1b_real_merged.json" if "merged" in ROOT.replace("\\", "/") else "p1b_real_main.json"
assert os.path.abspath(ocr.__file__).startswith(os.path.abspath(ROOT)), \
    "导入的不是目标树: %s" % ocr.__file__

OLD_TXT = "No.001738261"
NEW_TXT = "No.001739887"
OLD = "001738261"
NEW = "001739887"
W, H = 340, 30
WINDOWS = (50, 100, 200, 300)


def make_frame(text):
    img = np.full((H, W, 3), 28, np.uint8)
    cv2.putText(img, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                (238, 238, 238), 1, cv2.LINE_AA)
    return img


class FakeScreen(object):
    """只实现 `_read_serial*` 用到的那两个方法。"""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.window = 0.0
        self.blank = False
        self.old = make_frame(OLD_TXT)
        self.new = make_frame(NEW_TXT)

    def norm_to_px(self, norm):
        return (int(norm["x"] * 1920), int(norm["y"] * 1080),
                int(norm["w"] * 1920), int(norm["h"] * 1080))

    def grab(self, px):
        if self.blank:
            return np.zeros((H, W, 3), np.uint8)
        if (time.perf_counter() - self.t0) < self.window:
            return self.old
        return self.new


def engine_obj():
    eng = object.__new__(Engine)          # 只要方法，不跑 __init__
    eng._stop = threading.Event()
    return eng


def stat(xs):
    xs = sorted(xs)
    return {"n": len(xs), "p50": round(statistics.median(xs), 2),
            "p95": round(xs[min(len(xs) - 1, int(round(0.95 * len(xs)) - 1))], 2),
            "max": round(xs[-1], 2),
            "mean": round(statistics.fmean(xs), 2)}


def main():
    # 预热：一级/二级都建起来，别把冷启动算进第一拍
    img = make_frame(OLD_TXT)
    from core.ocr_chain import upscale
    _ = ocr.read_text(upscale(img, 3))

    b = ocr._client()
    probe_calls = {"n": 0}
    if getattr(b, "pp", None) is not None:
        orig = b.pp.available

        def counting():
            probe_calls["n"] += 1
            return orig()
        b.pp.available = counting

    res = {"root": os.path.relpath(ROOT, PROJ), "ocr_ok": bool(ocr.available()),
           "ocr_file": ocr.__file__}
    scr = FakeScreen()
    eng = engine_obj()

    # ---- 1. 两种判据读的耗时（图上确实有编号） ----
    tags = [("slow_read_serial", lambda: Engine._read_serial(scr))]
    if HAS_FAST:
        tags.append(("fast_read_serial", lambda: Engine._read_serial_fast(scr)))
    for tag, fn in tags:
        xs = []
        for _ in range(40):
            t = time.perf_counter()
            v = fn()
            xs.append((time.perf_counter() - t) * 1000)
            assert v == OLD or v == NEW, "读到的不是编号: %r" % (v,)
        res[tag] = stat(xs)

    # ---- 2. 空帧：老路径会在 probe_ttl 到期时探活 1.5s，新路径必须 0 次 ----
    scr.blank = True
    p0 = probe_calls["n"]
    slow = []
    t_start = time.perf_counter()
    while time.perf_counter() - t_start < 12.0:      # 跨过 10s 的 probe_ttl
        t = time.perf_counter()
        Engine._read_serial(scr)
        slow.append((time.perf_counter() - t) * 1000)
        time.sleep(0.6)
    slow_probes = probe_calls["n"] - p0
    blank = {"slow_read_serial": stat(slow), "slow_probe_calls": slow_probes}
    if HAS_FAST:
        p1 = probe_calls["n"]
        fast = []
        t_start = time.perf_counter()
        while time.perf_counter() - t_start < 12.0:
            t = time.perf_counter()
            Engine._read_serial_fast(scr)
            fast.append((time.perf_counter() - t) * 1000)
            time.sleep(0.6)
        blank["fast_read_serial"] = stat(fast)
        blank["fast_probe_calls"] = probe_calls["n"] - p1
    res["blank_frame"] = blank
    scr.blank = False

    # ---- 3+4. 等待：命中四档窗口 / 未命中 / 十连开，并校验编号一致性 ----
    hit = {}
    for wms in WINDOWS:
        scr.window = wms / 1000.0
        waits, bad, ready_n = [], 0, 0
        for _ in range(20):
            scr.t0 = time.perf_counter()
            t = time.perf_counter()
            ok = Engine._wait_detail_ready(eng, scr, 0.55)
            ready_n += 1 if ok else 0
            waits.append((time.perf_counter() - t) * 1000)
            seen = (Engine._read_serial_fast if HAS_FAST else Engine._read_serial)(scr)
            if seen != NEW:
                bad += 1
        hit[str(wms)] = {"wait": stat(waits), "ready": ready_n,
                         "serial_mismatch": bad,
                         "not_ready": 20 - ready_n}
    res["hit"] = hit

    scr.blank = True
    waits = []
    for _ in range(20):
        t = time.perf_counter()
        Engine._wait_detail_ready(eng, scr, 0.55)
        waits.append((time.perf_counter() - t) * 1000)
    res["miss"] = {"wait": stat(waits), "cap_ms": 550.0}
    scr.blank = False

    ten = {}
    for wms in (50, 100):
        scr.window = wms / 1000.0
        each, bad = [], 0
        for _ in range(10):
            scr.t0 = time.perf_counter()
            t = time.perf_counter()
            Engine._wait_detail_ready(eng, scr, 0.55)
            each.append(round((time.perf_counter() - t) * 1000, 2))
            if (Engine._read_serial_fast if HAS_FAST else Engine._read_serial)(scr) != NEW:
                bad += 1
        ten[str(wms)] = {"each_ms": each, "max_ms": max(each),
                         "serial_mismatch": bad}
    res["hit10"] = ten
    res["probe_calls_total"] = probe_calls["n"]

    print(json.dumps(res, ensure_ascii=False, indent=1))
    out = os.path.join(PROJ, "perf", "out", OUT_NAME)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("输出:", out)


if __name__ == "__main__":
    main()
