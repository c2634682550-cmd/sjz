"""单条链路的逐段耗时表（含嵌套），给 docs/perf_ocr_path.md 用。

    python perf/bench/round_report.py 0 0 3   # 优化前
    python perf/bench/round_report.py 1 1 3   # 优化后

按 perf_counter 的起止时刻还原嵌套：`where` 里套 `_read_text`/`_read_page_no`，
只算顶层（自身区间不被别的段包住）才是端到端口径。
"""
import collections
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "perf", "bench"))

import statistics  # noqa: E402

import chain_bench  # noqa: E402

PHASES = ["where", "enter_market", "tab", "skin_items", "pick_skin",
          "buy", "_buy_button_visible", "ensure_public_filter",
          "_read_text", "_read_page_no"]


def one(reuse, fast, rep):
    from core import nav as navmod
    from core import ocr
    chain_bench.configure(reuse, fast)
    world = chain_bench.World()
    nv = navmod.Navigator(screen=world, mouse=chain_bench.FakeMouse(world),
                          keyboard=chain_bench.FakeKeyboard(world),
                          snap=world.grab, log=lambda m, l="info": None, opts={})
    rows = []
    for name in PHASES:
        orig = getattr(nv, name)

        def wrap(fn, label):
            def inner(*a, **kw):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **kw)
                finally:
                    rows.append((label, t0, time.perf_counter()))
            return inner
        setattr(nv, name, wrap(orig, name))
    totals = []
    for _ in range(rep):
        ocr._frame.clear()
        rows.clear()
        world.set("hall")
        t0 = time.perf_counter()
        page = nv.go_to_onsale(from_hall=True)
        totals.append((time.perf_counter() - t0) * 1000.0)
        yield page, totals[-1], list(rows)


def depth_of(row, rows):
    name, a, b = row
    return sum(1 for _, a2, b2 in rows
               if a2 < a and b2 > b and not (a2 == a and b2 == b))


def main():
    reuse = bool(int(sys.argv[1])) if len(sys.argv) > 1 else True
    fast = bool(int(sys.argv[2])) if len(sys.argv) > 2 else True
    rep = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    e2e, top, nested, stats = [], collections.defaultdict(list), \
        collections.defaultdict(list), None
    page = None
    for pg, total, rows in one(reuse, fast, rep):
        page = pg
        e2e.append(total)
        for row in rows:
            (top if depth_of(row, rows) == 0 else nested)[row[0]].append(
                (row[2] - row[1]) * 1000.0)
    print(f"reuse={reuse} fast={fast} rep={rep} page={page} "
          f"e2e_p50={statistics.median(e2e):.0f} "
          f"min={min(e2e):.0f} max={max(e2e):.0f}")

    def show(title, d):
        print(f"-- {title} --")
        print(f"{'phase':22s} {'n/链':>5s} {'p50':>7s} {'total/链':>9s} {'占e2e':>7s}")
        for k, v in sorted(d.items(), key=lambda x: -sum(x[1])):
            p = statistics.median(v)
            print(f"{k:22s} {len(v) / rep:5.1f} {p:7.1f} {sum(v) / rep:9.1f} "
                  f"{sum(v) / rep / statistics.median(e2e) * 100:6.1f}%")
    show("顶层（不嵌套）", top)
    show("嵌套段", nested)
    from core import ocr
    print("ocr_stats", json.dumps(ocr._stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
