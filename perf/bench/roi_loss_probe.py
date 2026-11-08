"""验「det 降采样」在各类页面 ROI 上丢不丢锚点词。

对每个真机 ROI：baseline(1×) 与 fast(0.5×) 的拼串对比，
列出丢字/变字的具体位置。用于判断快速路径能不能默认开。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "perf", "bench"))
from fastdet_probe import fast_words  # noqa: E402

NAMES = ("catalog.png", "detail_panel.png", "filter_panel.png",
         "list_filtered.png", "list_unfiltered.png")


def diff(a, b):
    """极简：按字符集合给出丢失的字符数与新增。"""
    ca, cb = {}, {}
    for ch in a:
        ca[ch] = ca.get(ch, 0) + 1
    for ch in b:
        cb[ch] = cb.get(ch, 0) + 1
    lost = {}
    for ch, n in ca.items():
        if cb.get(ch, 0) < n:
            lost[ch] = n - cb.get(ch, 0)
    return lost


def main():
    from core import ocr_rapid
    be = ocr_rapid.make({})
    eng = be._engine()
    out = {}
    for n in NAMES:
        img = cv2.imread(os.path.join(ROOT, "tests", "fixtures", "market", n))
        base = "".join(w["text"] for w in be.read_words(img))
        rec = {}
        for s in (0.75, 0.5):
            t = "".join(w["text"] for w in fast_words(eng, img, s))
            rec[f"{s:g}"] = {"chars": len(t), "delta": len(t) - len(base),
                             "lost": diff(base, t)}
        out[n] = {"base_chars": len(base), "base_text": base[:120],
                  "scaled": rec}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
