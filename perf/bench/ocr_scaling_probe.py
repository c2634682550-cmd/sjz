"""量「整帧 OCR 成本随像素面积怎么变」，以及 det / rec 的拆分。

用于判断 `where()`（整屏 1× OCR）能不能靠「降采样 det + 原图 rec」提速。
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402

from perf.bench.common import Summary  # noqa: E402

FULL = os.path.join(ROOT, "_rm_shots", "20261107_154521", "S6-4_1.png")


def timed(fn, rep=4):
    s = Summary(warmup=1)
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        s.add((time.perf_counter() - t0) * 1000.0)
    return s.summary()


def main():
    from core import ocr_rapid

    img = cv2.imread(FULL)
    if img is None:
        raise SystemExit(f"缺帧：{FULL}")
    h, w = img.shape[:2]

    be = ocr_rapid.make({})
    out = {"frame": FULL, "shape": [w, h], "segments": {}}
    for scale in (1.0, 0.75, 0.5, 0.35, 0.25):
        im = img if scale == 1.0 else cv2.resize(
            img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        out["segments"][f"rapid_full_x{scale:g}"] = timed(
            lambda im=im: be.read_words(im))

    # det / rec 拆分：直接调 rapidocr 内部
    eng = be._engine()
    try:
        from rapidocr import RapidOCR  # noqa: F401
        det = eng.text_det
        rec = eng.text_rec
    except Exception as e:                       # noqa: BLE001
        out["split_error"] = f"{type(e).__name__}: {e}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    for scale in (1.0, 0.5, 0.35):
        im = img if scale == 1.0 else cv2.resize(
            img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        def _det(im=im):
            det(im)

        out["segments"][f"det_only_x{scale:g}"] = timed(_det)

    # rec 成本：用其中一张 ROI（走 RapidOCR 的 TextRecInput 数据类）
    try:
        from rapidocr.utils.typings import TextRecInput
        crop = img[300:430, 1180:1500]
        boxes = [[[0, 0], [crop.shape[1], 0],
                  [crop.shape[1], crop.shape[0]], [0, crop.shape[0]]]]
        out["segments"]["rec_only_320x130"] = timed(
            lambda: rec(TextRecInput(img=crop, box=boxes)))
    except Exception as e:                       # noqa: BLE001
        out.setdefault("split_error_rec", f"{type(e).__name__}: {e}")

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
