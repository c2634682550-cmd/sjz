# -*- coding: utf-8 -*-
"""从 paddlex 服务日志里抓 500 的完整原因（PIR/oneDNN）。"""
import re

p = "perf/ocr/out/ppocr_serve.log"
s = open(p, encoding="utf-8", errors="replace").read()
for kw in ["ConvertPirAttribute", "oneDNN", "mkldnn", "ArrayAttribute"]:
    print(kw, len(re.findall(kw, s)))
i = s.find("ConvertPirAttribute")
print("---- excerpt ----")
print(s[max(0, i - 300):i + 400] if i >= 0 else s[-1000:])
