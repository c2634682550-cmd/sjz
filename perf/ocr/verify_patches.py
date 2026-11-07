# -*- coding: utf-8 -*-
"""校验补丁应用结果与 work 一致（CRLF/LF 归一后逐字节比对）。

基线是冻结快照 perf/base。_applytest 必须由 perf/base 重建后再 git apply。
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
A = os.path.join(ROOT, "_applytest")
W = os.path.join(ROOT, "work")
BASE = os.path.join(ROOT, "..", "base")

# 补丁覆盖的文件
FILES = ["core/ocr_chain.py", "core/ocr.py", "core/ocr_ppocr.py",
         "core/engine.py", "tests/test_ocr_price.py", "tests/test_ocr_backend.py"]
# 未进补丁、但 work 必须保持与基线一致的文件（防止实验残留混进交付）
UNTOUCHED = ["core/market.py", "core/ocr_rapid.py"]


def norm(p):
    with open(p, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


bad = 0
for rel in FILES:
    a, w = norm(os.path.join(A, rel)), norm(os.path.join(W, rel))
    if a == w:
        print(f"OK   {rel} {len(a)}B")
        continue
    bad += 1
    n = min(len(a), len(w))
    k = next((i for i in range(n) if a[i] != w[i]), n)
    print(f"DIFF {rel} apply={len(a)}B work={len(w)}B 首个差异@{k}")

for rel in UNTOUCHED:
    b, w = norm(os.path.join(BASE, rel)), norm(os.path.join(W, rel))
    if b == w:
        print(f"OK   {rel} 与基线一致（未进补丁）")
    else:
        bad += 1
        print(f"DIFF {rel} base={len(b)}B work={len(w)}B（work 残留实验改动）")

print("结果:", "一致" if not bad else f"{bad} 个文件不一致")
