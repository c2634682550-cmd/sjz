"""真机整屏 det 降采样是否丢锚点（只读）。

    python perf/bench/live_screen_scale.py [轮数]

同一张真机帧上对比：1.0（现状）/ 0.8 / 0.75 的 det 降采样，
看识别字数、页面判定与耗时，判定「能不能在不掉判对率的前提下提速」。
"""
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core import nav, ocr, ocr_rapid  # noqa: E402
from core.screen import Screen  # noqa: E402


def main(n=4):
    sc = Screen(1)
    be = ocr._client()
    rap = getattr(be, "rapid", be)
    eng = rap._engine()
    rows = []
    for i in range(n):
        img = sc.grab()
        base_t = time.perf_counter()
        base = ocr_rapid._words(eng(img))
        base_ms = (time.perf_counter() - base_t) * 1000
        base_text = "".join(w["text"] for w in base)
        base_page = nav.classify(base_text)
        for scale in (0.8, 0.75):
            t = time.perf_counter()
            words = ocr_rapid._read_scaled(eng, img, scale) or []
            ms = (time.perf_counter() - t) * 1000
            text = "".join(w["text"] for w in words)
            page = nav.classify(text)
            rows.append((i, scale, ms, len(words), len(text), page, base_page))
            print(f"[{i}] scale={scale:.2f} {ms:6.0f}ms words={len(words):3d} "
                  f"chars={len(text):4d} page={page} (baseline {base_page}, "
                  f"{base_ms:.0f}ms {len(base_text)}chars)")
    sc.close()

    print("\n== 汇总 ==")
    print("baseline 页判定：", len({r[6] for r in rows}))
    for scale in (0.8, 0.75):
        ms = [r[2] for r in rows if r[1] == scale]
        bad = [r for r in rows if r[1] == scale and r[5] != r[6]]
        print(f"scale={scale}: p50={statistics.median(ms):.0f}ms "
              f"判错={len(bad)}/{len(ms)}")
        for r in bad:
            print("   丢锚点样例：", r[:6])


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 4)
