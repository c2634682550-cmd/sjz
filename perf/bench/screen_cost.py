"""整屏 / 页码条 OCR 单点耗时（fast_det 开 vs 关）。

    python perf/bench/screen_cost.py
"""
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "perf", "bench"))

import chain_bench  # noqa: E402


def bench(img, fast, tag, n=5, roi=None):
    from core import ocr
    chain_bench.configure(reuse=False, fast=fast)
    ocr._backend = None
    if roi is not None:
        be = ocr._client()
        rap = getattr(be, "rapid", be)      # chain 外面套一层，里面才是 ocr_rapid
        rap.cfg["fast_det_roi"] = roi
    ms, texts = [], set()
    for _ in range(n):
        t = time.perf_counter()
        w = ocr.read_words(img)
        ms.append((time.perf_counter() - t) * 1000.0)
        assert w is not None
        texts = {x["text"] for x in w}
        chars = len("".join(x["text"] for x in w))
    print(f"{tag:28s} fast={int(fast)} roi={roi} "
          f"p50={statistics.median(ms):7.1f} min={min(ms):7.1f} "
          f"max={max(ms):7.1f} 词={len(texts)} 字={chars}")
    return texts


def main():
    fr = chain_bench.frames()
    screen = fr["detail"]
    from core import vision, nav
    x, y, w, h = nav._PAGE_NO_BOX
    strip = vision.upscale(screen[y:y + h, x:x + w], 6)
    bench(screen, False, "整屏 1920x1080", 4)
    bench(screen, True, "整屏 1920x1080", 4)
    bench(strip, False, "页码条 3240x360", 4)
    bench(strip, True, "页码条 3240x360", 4)
    # 中等 ROI（近似皮肤列表 750×1560）：det 降到 0.75，看省多少 + 丢不丢字
    mid = screen[60:60 + 1560, 1000:1000 + 750]
    a = bench(mid, True, "中等 ROI 750x1560", 5, roi=1.0)
    b = bench(mid, True, "中等 ROI 750x1560", 5, roi=0.75)
    print(f"中等 ROI 0.75 丢字: {sorted(a - b)}  多出: {sorted(b - a)}")


if __name__ == "__main__":
    main()
