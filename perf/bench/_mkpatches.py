# -*- coding: utf-8 -*-
"""把 perf/base 里的改动拆成逐个可独立应用的补丁（面向 perf/_pristine 快照）。"""
import difflib
import io
import os

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(PROJ, "perf", "patches", "flow")


def load(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read().splitlines()


a = load(os.path.join(PROJ, "perf", "_pristine", "engine.py"))
b = load(os.path.join(PROJ, "perf", "base", "core", "engine.py"))
am = load(os.path.join(PROJ, "perf", "_pristine", "market.py"))
bm = load(os.path.join(PROJ, "perf", "base", "core", "market.py"))

ops = [(i1, i2, j1, j2) for t, i1, i2, j1, j2
       in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
       if t != "equal"]
assert len(ops) == 13, len(ops)
blk = ops[2]


def variant(actions):
    out = list(a)
    acts = [ops[i] for i in actions]
    for i1, i2, j1, j2 in sorted(acts, key=lambda x: -x[0]):
        out[i1:i2] = b[j1:j2]
    return out


def write(name, old, new, path):
    body = list(difflib.unified_diff(old, new, fromfile="a/" + path,
                                     tofile="b/" + path, n=3, lineterm=""))
    assert body, name
    with io.open(os.path.join(OUT, name), "w", encoding="utf-8",
                 newline="\n") as f:
        f.write("diff --git a/%s b/%s\n" % (path, path))
        f.write("\n".join(body) + "\n")
    print("%-46s %d 行" % (name, len(body)))


if not os.path.isdir(OUT):
    os.makedirs(OUT)

write("01-detail-conditional-wait.patch", a, variant([2, 3]),
      "core/engine.py")
write("02-bought-dialog-skip-first.patch", a, variant([6]), "core/engine.py")
write("03-verify-frontload-gaps.patch", a, variant([4, 5]), "core/engine.py")
write("04-guard-hint-prefilter.patch", a, variant([12]), "core/engine.py")
write("05-close-reward-conditional.patch",
      a, variant([0, 1, 7, 8, 9, 10, 11]), "core/engine.py")
write("06-scan-frame-memo.patch", am, bm, "core/market.py")
