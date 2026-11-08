import os
import sys
import time

ROOT = r"C:\Users\26346\Desktop\project\dafors"
sys.path.insert(0, ROOT)

import cv2  # noqa: E402

from core import nav, ocr  # noqa: E402
from core.screen import Screen  # noqa: E402


def main():
    sc = Screen(1)
    print("monitors=%s client_rect=%s len(frame)=%s game_rect=%s" % (
        sc.index, getattr(sc, "client_rect", None),
        None if sc.frame is None else (sc.frame["width"], sc.frame["height"]),
        getattr(sc, "game_rect", None)))
    nv = nav.Navigator(screen=sc, mouse=None, snap=sc.grab,
                       log=lambda m, l="info": None, opts={})

    print("\n== where() 真机读数（只读，不注入任何输入） ==")
    base = ocr.metrics()
    for i in range(6):
        t0 = time.perf_counter()
        page, text = nv.where()
        ms = (time.perf_counter() - t0) * 1000
        st = ocr.metrics()
        print(f"[{i}] page={page:9s} {ms:6.0f}ms calls={st.get('calls')} "
              f"reuse={st.get('reuse')} text={text[:60]!r}")

    print("\n== 连续两帧是否逐像素相同（真机同帧复用的判据） ==")
    same_n = 0
    for i in range(8):
        a = sc.grab()
        b = sc.grab()
        eq = a.shape == b.shape and float(cv2.norm(a, b, cv2.NORM_L1)) == 0.0
        same_n += 1 if eq else 0
        d = (float(cv2.norm(a.astype("int16"), b.astype("int16"), cv2.NORM_L1))
             / max(1, a.size))
        print(f"[{i}] same={eq} diff/px={d:.3f}")
        time.sleep(0.05)
    st = ocr.metrics()
    print(f"\n两帧相同帧对：{same_n}/8")
    print(f"metrics: calls={st.get('calls')} reuse={st.get('reuse')} "
          f"empty={st.get('empty')}")


if __name__ == "__main__":
    main()
