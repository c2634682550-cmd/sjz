"""真机 where() 分项剖分（只读：不点击、不按键）。

    python perf/bench/live_where_profile.py [轮数]

逐段计时：抓屏 → 整幅 OCR → 页码条 6× OCR → 页面特征判定，
并尽量把整幅 OCR 再拆成 det / rec 两段（rapidocr 引擎暴露才拆）。
"""
import statistics
import sys
import time

import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core import nav, ocr  # noqa: E402
from core.screen import Screen  # noqa: E402


def pct(xs, q):
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))) )]


def main(n=6):
    sc = Screen(1)
    nv = nav.Navigator(screen=sc, mouse=None, snap=sc.grab,
                       log=lambda m, l="info": None, opts={})
    be = ocr._client()
    rap = getattr(be, "rapid", be)
    eng = rap._engine()
    names = [a for a in dir(eng) if not a.startswith("_") and
             ("det" in a.lower() or "rec" in a.lower() or "cls" in a.lower())]
    print("engine=%s det/rec attrs=%s" % (type(eng).__name__, names))

    rec = {k: [] for k in ("grab", "ocr", "pageno", "classify", "where",
                           "det", "rec")}
    pages = []
    try:
        for i in range(n):
            t = time.perf_counter()
            img = sc.grab()
            rec["grab"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            words = ocr.read_words(img)
            rec["ocr"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            pno = nv._read_page_no(img)
            rec["pageno"].append((time.perf_counter() - t) * 1000)

            text = "".join(w.get("text", "") for w in words) + pno
            t = time.perf_counter()
            page, _ = nav.classify(text), None
            rec["classify"].append((time.perf_counter() - t) * 1000)
            pages.append(page)

            # det / rec 拆分：只统计不参与判定
            if "text_det" in names:
                t = time.perf_counter()
                try:
                    eng.text_det(img)
                except Exception:
                    pass
                rec["det"].append((time.perf_counter() - t) * 1000)

            t = time.perf_counter()
            where_page, _ = nv.where()
            rec["where"].append((time.perf_counter() - t) * 1000)
            print(f"[{i}] page={page} where={rec['where'][-1]:.0f}ms "
                  f"grab={rec['grab'][-1]:.0f} ocr={rec['ocr'][-1]:.0f} "
                  f"pageno={rec['pageno'][-1]:.0f} "
                  f"classify={rec['classify'][-1]:.2f} "
                  f"det={rec['det'][-1] if rec['det'] else 0:.0f}")
    finally:
        sc.close()

    print("\n== 汇总（n=%d，p50 / p95） ==" % n)
    for k in ("grab", "ocr", "pageno", "classify", "where", "det"):
        xs = rec[k]
        if not xs:
            continue
        print(f"{k:9s} p50={statistics.median(xs):7.2f}  p95={pct(xs, .95):7.2f}"
              f"  share_of_where={statistics.median(xs)/statistics.median(rec['where'])*100:5.1f}%")
    print("页面判定：", pages)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
