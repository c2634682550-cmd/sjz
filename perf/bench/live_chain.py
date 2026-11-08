import statistics
import sys
import time

ROOT = r"C:\Users\26346\Desktop\project\dafors"
sys.path.insert(0, ROOT)

from core import ocr  # noqa: E402
from core.actions import _open_devices  # noqa: E402


def run(n=3):
    screen, mouse, nv = _open_devices({}, log=lambda m, l="info": print("  .", m))
    rec = {k: [] for k in ("e2e", "enter_market", "tab", "buy", "click",
                           "wait", "wait_f4", "wait_buy", "tab_wait")}
    pages = []

    orig_click = nv.click

    def click_w(*a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_click(*a, **kw)
        finally:
            rec["click"].append((time.perf_counter() - t0) * 1000)
    nv.click = click_w

    orig_tab = nv.tab

    def tab_w(*a, **kw):
        t0 = time.perf_counter()
        c0 = len(rec["click"])
        try:
            return orig_tab(*a, **kw)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            rec["tab"].append(ms)
            rec["tab_wait"].append(ms - sum(rec["click"][c0:]))
    nv.tab = tab_w

    orig_wait = nv._wait_page

    def wait_w(*a, **kw):
        label = kw.get("label") or ""
        t0 = time.perf_counter()
        try:
            return orig_wait(*a, **kw)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            rec["wait"].append(ms)
            rec["wait_buy" if label else "wait_f4"].append(ms)
    nv._wait_page = wait_w

    for name in ("enter_market", "buy"):
        orig = getattr(nv, name)

        def wrap(fn, label):
            def inner(*a, **kw):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **kw)
                finally:
                    rec[label].append((time.perf_counter() - t0) * 1000)
            return inner
        setattr(nv, name, wrap(orig, name))

    try:
        for i in range(n):
            for k in rec:
                rec[k].clear()
            st0 = ocr.metrics()
            t0 = time.perf_counter()
            try:
                pages.append(nv.go_to_onsale(from_hall=True))
            except Exception as e:                    # noqa: BLE001
                pages.append("ERR:" + str(e)[:60])
            rec["e2e"].append((time.perf_counter() - t0) * 1000)
            st1 = ocr.metrics()

            def g(k):
                return rec[k][-1] if rec[k] else 0.0
            print(f"\n[{i}] page={pages[-1]} e2e={rec['e2e'][-1]:.0f}ms "
                  f"enter={g('enter_market'):.0f} "
                  f"tab={g('tab'):.0f} buy={g('buy'):.0f} "
                  f"tab_wait={g('tab_wait'):.0f} "
                  f"buy_wait={g('wait_buy'):.0f} "
                  f"calls={st1.get('calls')-st0.get('calls')} "
                  f"reuse={st1.get('reuse')-st0.get('reuse')}")
    finally:
        screen.close()

    print("\n== 真机汇总（n=%d） ==" % n)
    for k in ("e2e", "enter_market", "tab", "buy", "tab_wait", "wait_buy"):
        xs = rec[k]
        if not xs:
            print(f"{k:12s} 无数据")
            continue
        print(f"{k:12s} p50={statistics.median(xs):7.0f} "
              f"max={max(xs):7.0f} min={min(xs):7.0f}")
    print("页面判定：", pages)


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
