"""轮询式等待：A/B 对照 + 超时是否仍按原路径报错。

    python perf/bench/poll_bench.py

A. 失败路径（cap 到达仍不满足必须抛，不能当成功）
B. 整条链 A/B（before = 打补丁还原固定 sleep；after = 现在的轮询）
C. 三段等待单独扫描：把假屏幕的换页延迟设成 L 秒，量「等待段」本身
"""
import os
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "perf", "bench"))

import chain_bench  # noqa: E402
from core.nav import NavError  # noqa: E402

N = 5            # 每格 5 条，p95 用 n=5 的最大值当上界（报告里会写明）
LS = (0.0, 0.8, 1.5, 2.5)


def build(world):
    from core import nav as navmod
    return navmod.Navigator(screen=world, mouse=chain_bench.FakeMouse(world),
                            keyboard=chain_bench.FakeKeyboard(world),
                            snap=world.grab, log=lambda m, l="info": None,
                            opts={})


def delay_world(world, sec):
    """把 set() 改成「sec 秒后才真的换页」，模拟游戏渲染延迟。"""
    orig = world.set

    def later(s):
        threading.Timer(sec, orig, args=(s,)).start()
    world.set = later


def p95(xs):
    s = sorted(xs)
    return s[max(0, int(round(0.95 * (len(s) - 1))) )]


def install_legacy(nv):
    """把实例方法换回改前的「固定 sleep」实现，用于同台架 A/B。"""
    from core import nav as navmod
    table = {"mandel": navmod.TAB_MANDEL, "collection": navmod.TAB_COLLECTION,
             "charm": navmod.TAB_CHARM}

    def legacy_wait(want, cap=2.0, step=0.25, label="", activate=""):
        if activate:
            nv._activate(activate)
        time.sleep(1.5 if label else 2.0)      # buy 1.5s / F4 2.0s
        page, text = nv.where()
        wants = (want,) if isinstance(want, str) else tuple(want)
        if page not in wants and label:
            raise NavError(f"{label}：期望页面 {wants}，实际 {page}"
                           f"（OCR：{text[:80]}）")
        return page

    def legacy_tab(name, settle=1.2):
        if name not in table:
            raise NavError(f"没有这个标签：{name}")
        if nv.tab_now() == name:
            return True
        nv.click(table[name], f"标签 {name}", timeout=3.0)
        time.sleep(settle)                     # 固定 1.2s
        got = nv.tab_now()
        if got != name:
            raise NavError(f"点标签 {name} 没切过去（绿框现在在 {got}）——"
                           f"可能不在市场页")
        return True

    nv._wait_page = legacy_wait
    nv.tab = legacy_tab


def part_a():
    print("== A. 失败路径 ==")
    w = chain_bench.World()
    chain_bench.configure(True, True)
    nv = build(w)
    w.set("market")

    t = time.perf_counter()
    got = nv._wait_page("onsale", cap=0.40, step=0.10)
    el = (time.perf_counter() - t) * 1000
    print(f"_wait_page(cap=0.40) 不匹配 → 返回 {got!r} 用时 {el:.0f}ms "
          f"(应 ≥400 且不抛)")
    assert got == "market" and el >= 400

    t = time.perf_counter()
    try:
        nv._wait_page(("detail", "onsale"), cap=0.40, step=0.10, label="点购买")
        raise AssertionError("应该抛 NavError")
    except NavError as e:
        el = (time.perf_counter() - t) * 1000
        print(f"_wait_page(label) → NavError（{el:.0f}ms）：{str(e)[:70]}")
        assert el >= 400

    w.set("detail")
    t = time.perf_counter()
    try:
        nv.tab("collection", settle=0.40)
        raise AssertionError("应该抛 NavError")
    except NavError as e:
        el = (time.perf_counter() - t) * 1000
        print(f"tab 切不过去 → NavError（{el:.0f}ms）：{str(e)[:70]}")
        assert el >= 400
    print("A 通过：三处超时都走原报错路径，没有「超时当成功」")


# ---------------------------------------------------------------- B. 整链 A/B

def chain_run(mode, rep=N, L=0.0):
    from core import ocr
    w = chain_bench.World()
    if L:
        delay_world(w, L)
    chain_bench.configure(True, True)
    nv = build(w)
    if mode == "before":
        install_legacy(nv)

    rec = {k: [] for k in ("e2e", "enter_market", "tab", "buy", "click",
                           "tab_click", "wait")}

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
            rec["tab_click"].append(sum(rec["click"][c0:]))

    nv.tab = tab_w

    orig_wait = nv._wait_page

    def wait_w(*a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_wait(*a, **kw)
        finally:
            rec["wait"].append((time.perf_counter() - t0) * 1000)

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

    for _ in range(rep):
        for k in rec:
            rec[k].clear()
        w.state = "hall"
        ocr._frame.clear()
        t0 = time.perf_counter()
        nv.go_to_onsale(from_hall=True)
        rec["e2e"].append((time.perf_counter() - t0) * 1000)
    return rec


def part_b():
    print("\n== B. 整链 A/B（L=0，每格 %d 条；p95≈n=%d 的最大值） ==" % (N, N))
    out = {m: chain_run(m) for m in ("before", "after")}
    hdr = (f"{'mode':>7} {'e2e_p50':>8} {'e2e_p95':>8} {'enter':>7} "
           f"{'tab':>7} {'buy':>7} {'tab_wait':>9} {'buy_wait':>9}")
    print(hdr)
    for m in ("before", "after"):
        r = out[m]
        tab_wait = statistics.median(
            [t - c for t, c in zip(r["tab"], r["tab_click"])])
        print(f"{m:>7} {statistics.median(r['e2e']):8.0f} {p95(r['e2e']):8.0f} "
              f"{statistics.median(r['enter_market']):7.0f} "
              f"{statistics.median(r['tab']):7.0f} "
              f"{statistics.median(r['buy']):7.0f} "
              f"{tab_wait:9.0f} "
              f"{statistics.median(r['wait']):9.0f}")
    b, a = out["before"], out["after"]
    print("  分段节省 p50：")
    for k, label in (("enter_market", "enter_market"), ("tab", "tab"),
                     ("buy", "buy")):
        d = statistics.median(b[k]) - statistics.median(a[k])
        print(f"    {label:<14} {statistics.median(b[k]):7.0f} → "
              f"{statistics.median(a[k]):7.0f}  = {d:+7.0f}ms")
    d = statistics.median(b["e2e"]) - statistics.median(a["e2e"])
    print(f"    {'e2e':<14} {statistics.median(b['e2e']):7.0f} → "
          f"{statistics.median(a['e2e']):7.0f}  = {d:+7.0f}ms")
    return out


# ------------------------------------------------- C. 三段等待单独扫描

def seg_run(seg, mode, L, rep=N):
    """单独量「等待段」：tap/click 已经发出 → 到条件成立（或上限）的耗时。"""
    from core import ocr
    chain_bench.configure(True, True)
    durs, pages = [], []
    for _ in range(rep):
        w = chain_bench.World()
        if L:
            delay_world(w, L)
        nv = build(w)
        if mode == "before":
            install_legacy(nv)
        ocr._frame.clear()
        if seg == "f4":
            w.state = "hall"
            t0 = time.perf_counter()
            nv.keyboard.tap("F4")
            page = nv._wait_page("market", cap=2.0, step=0.25)
            pages.append(page)
        elif seg == "tab":
            w.state = "market"
            c0 = time.perf_counter()
            try:
                nv.tab("collection")
                page = nv.tab_now()
            except NavError:
                page = "NavError"
            dur = (time.perf_counter() - c0) * 1000
            # 减掉 click 那段（改前改后都有 click 的内部等待）
            durs.append(dur)
            pages.append(page)
            continue
        elif seg == "buy":
            w.state = "collection_sel"
            t0 = time.perf_counter()
            try:
                nv.buy()
                page = nv.where()[0]
            except NavError:
                page = "NavError"
            pages.append(page)
        durs.append((time.perf_counter() - t0) * 1000)
    return durs, pages


def part_c():
    print("\n== C. 三段等待单独扫描（每格 %d 次；after=轮询 / before=固定 sleep） ==" % N)
    print(f"{'seg':>4} {'L':>4} {'before_p50':>11} {'before_p95':>11} "
          f"{'after_p50':>10} {'after_p95':>10} {'saved':>7} {'after_page':>11}")
    for seg in ("f4", "tab", "buy"):
        for L in LS:
            bd, _ = seg_run(seg, "before", L)
            ad, ap = seg_run(seg, "after", L)
            sv = statistics.median(bd) - statistics.median(ad)
            print(f"{seg:>4} {L:4.1f} {statistics.median(bd):11.0f} "
                  f"{p95(bd):11.0f} {statistics.median(ad):10.0f} "
                  f"{p95(ad):10.0f} {sv:+7.0f} {ap[0]:>11}")


if __name__ == "__main__":
    part_a()
    part_b()
    part_c()
