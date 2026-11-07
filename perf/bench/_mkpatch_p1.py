# -*- coding: utf-8 -*-
"""把 perf/merged 相对主仓的改动切成补丁（LF 换行，git apply 可直接吃）。

perf/merged 是「合并后的 core + 本次两条改动」的快照；主仓文件是基线。
"""
import difflib
import io
import os

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(PROJ, "perf", "patches", "flow")

PAIRS = [
    # (补丁名, 相对主仓路径, perf/merged 里的对应路径)
    ("01b-detail-conditional-wait.patch", [
        ("core/engine.py", "perf/merged/core/engine.py"),
        ("core/ocr.py", "perf/merged/core/ocr.py"),
        ("tests/test_perf_detail_wait.py", "perf/merged/tests/test_perf_detail_wait.py"),
    ]),
    ("07-fireproc-ready-handshake.patch", [
        ("core/fire_proc.py", "perf/merged/core/fire_proc.py"),
        ("tools/fire_worker.py", "perf/merged/tools/fire_worker.py"),
        ("tests/test_fire_proc.py", "perf/merged/tests/test_fire_proc.py"),
        ("tests/test_fire_process.py", "perf/merged/tests/test_fire_process.py"),
    ]),
]


def read(p):
    path = os.path.join(PROJ, p)
    if not os.path.exists(path):
        return ""
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def udiff(rel, other):
    a = read(rel)
    b = read(other)
    if a == b:
        return ""
    relu = rel.replace(os.sep, "/")
    pre = "diff --git a/%s b/%s\n" % (relu, relu)
    if not a:
        # 新建文件：git apply 需要 new file mode + /dev/null 头
        pre += "new file mode 100644\n"
        fromfile = "/dev/null"
    else:
        fromfile = "a/" + relu
    d = difflib.unified_diff(
        a.splitlines(True), b.splitlines(True),
        fromfile=fromfile, tofile="b/" + relu,
        n=3, lineterm="\n")
    body = "".join(d)
    if not body.endswith("\n"):
        body += "\n"
    return pre + body


def main():
    n = 0
    for name, spec in PAIRS:
        parts = []
        items = spec if isinstance(spec, list) else [(spec[0], spec[1])]
        for rel, other in items:
            p = udiff(rel, other)
            if p:
                parts.append(p)
        if not parts:
            print("空补丁，跳过:", name)
            continue
        path = os.path.join(OUT, name)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(parts))
        n += 1
        print("%-40s %6d B" % (name, os.path.getsize(path)))
    print("共 %d 个补丁 →" % n, OUT)


if __name__ == "__main__":
    main()
