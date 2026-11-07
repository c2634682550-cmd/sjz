# -*- coding: utf-8 -*-
"""从 base→work 生成交付补丁（git apply -p1）。

本轮只交付一件：二级（PP-OCRv6）不再静默失败，且启动自检异步、探活预算 300ms。
线程分档那版（01/02/03）在真实流程上被守门数据否掉，不再交付，见 _perf_ocr.md §8。
"""
import difflib
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
# 基线必须用冻结快照 perf/base，而不是 perf/ocr/base（后者是旧副本，
# engine.py / market.py 已落后，基于它生成的 diff 会把别人的改动反向抹掉）。
B = os.path.join(ROOT, "..", "base")
W = os.path.join(ROOT, "work")
OUT = os.path.join(ROOT, "..", "patches", "ocr")
os.makedirs(OUT, exist_ok=True)

GROUPS = [
    ("01_tier2_never_dies_silently.patch",
     ["core/ocr_chain.py", "core/ocr.py", "core/ocr_ppocr.py", "core/engine.py",
      "tests/test_ocr_price.py", "tests/test_ocr_backend.py"],
     "说明：二级「端口通但推理 500」改为捕获+计数+记错+继续；启动自检异步、"
     "探活预算 300ms，状态栏显示二级真实状态。"
     "收益：不牺牲稳态速度，修掉二级静默死亡/异常上抛（本机 PP-OCRv6 CPU 分支必崩）"),
]


def raw(p):
    with open(p, "rb") as f:
        # 统一成 LF 再 diff：base 是 CRLF、work 里被编辑过的文件是 LF，
        # 不归一的话整个文件都会变成差异，补丁会膨胀到没有信息量。
        return f.read().decode("utf-8").replace("\r\n", "\n")


for name, files, note in GROUPS:
    chunk = []
    for rel in files:
        a = raw(os.path.join(B, rel)).splitlines(keepends=True)
        b = raw(os.path.join(W, rel)).splitlines(keepends=True)
        d = list(difflib.unified_diff(a, b, fromfile=f"a/{rel}",
                                     tofile=f"b/{rel}", n=3))
        if not d:
            raise SystemExit(f"no diff: {rel}")
        if not d[-1].endswith("\n"):
            d.append("\n")
        chunk.extend(d)
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="") as f:
        f.write(f"# {note}\n")
        f.writelines(chunk)
    print(name, len(chunk), "lines")
