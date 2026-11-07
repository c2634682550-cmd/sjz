# -*- coding: utf-8 -*-
"""进详情条件等待（`_wait_detail_ready`）的专项测量（换页证据版）。

只测四件事，全部走引擎原方法、本机实测单价：
  1. 命中：面板先保留上一件（编号 A），`window_ms` 后换成这一件（编号 B）
     → 这次等待花多少毫秒（含换页窗口本身）；
  2. 未命中：编号一直读不出 → 会不会退化成老的固定等待（应≈cap，p95/max <600ms）；
  3. 连开十件：每件都重新判定，不许有「上次就绪」的记忆；
  4. 单件端到端：进详情等待 + 开点/确认/校验/收奖励（与 flow_bench 同口径）。

换页窗口按守门给的 50/100/200/300ms 四档扫；端到端取 50/100/300 三档。
单价、虚拟时钟、假帧、场景脚本全部复用 flow_bench.py，不另立一套。

用法：
  .venv\\Scripts\\python.exe perf\\bench\\flow_p1.py --root perf/merged_pre --run 40
  .venv\\Scripts\\python.exe perf\\bench\\flow_p1.py --root perf/merged     --run 40
"""
import argparse
import importlib
import json
import os
import random
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import flow_bench as fb          # noqa: E402

CAP = 0.55                       # 老代码固定睡的秒数；也是新等待的上界
OLD = "001738261"                # 上一件留在右侧面板上的编号
NEW = "001739887"                # 本次点开那一件的编号
WINDOWS = (50, 100, 200, 300)    # 换页窗口（守门复验用的四档）


def page_change(ocr_fake, scene):
    """给脚本化 OCR 加「换页」行为：点击后先显示 OLD，`window_ms` 后显示 NEW。

    读不出（未命中场景）时仍然照收一次 OCR 的钱（真机上一片空白也要跑检测）。
    """
    orig = ocr_fake.read_text

    def read_text(img, **kw):
        text = orig(img, **kw)
        if getattr(img, "kind", "") == "serial":
            if not scene.serial_ok:
                return ""
            if (scene.vc.t - scene.serial_t0) * 1000 >= scene.window_ms:
                return NEW
            return OLD
        return text

    ocr_fake.read_text = read_text
    return ocr_fake


def build(root, mode, serial_ok, seed, window_ms=100):
    root = os.path.join(fb.PROJ, root)
    sys.path.insert(0, root)
    eng_mod = importlib.import_module("core.engine")
    popup_mod = importlib.import_module("core.popup")
    from core.nav_coords import N_BALANCE, N_CLOCK, N_LIST, N_POPUP_HINT, N_REWARD, N_SERIAL, N_SKIP

    regions = {
        "list": dict(N_LIST), "clock": dict(N_CLOCK), "balance": dict(N_BALANCE),
        "reward": dict(N_REWARD), "skip": dict(N_SKIP), "hint": dict(N_POPUP_HINT),
        "serial": dict(N_SERIAL),
        "buy": {"x": 0.772, "y": 0.836, "w": 0.155, "h": 0.054},
        "confirm": {"x": 0.47, "y": 0.60, "w": 0.06, "h": 0.03},
        "cont": getattr(eng_mod, "CONT_STRIP",
                        {"x": 0.40, "y": 0.890, "w": 0.20, "h": 0.100}),
    }

    class Cfg(object):
        def __init__(self):
            self.d = {"regions": regions, "failsafe": True,
                      "active_task": {"name": "枪械外观", "workflow": "list"},
                      "budget": 3000, "max_price": 3000, "switches": {}}

        def __getitem__(self, k):
            return self.d[k]

        def get(self, k, dv=None):
            return self.d.get(k, dv)

        def region(self, name):
            return self.d["regions"].get(name)

    SW = {}
    sw_mod = importlib.import_module("core.switches")
    for spec in sw_mod.SWITCHES:
        SW[spec["key"]] = spec.get("default")
    eng_mod.switches.get = lambda c, key: SW.get(key)

    rng = random.Random(seed)
    sampler = fb.Sampler(rng)
    vc = fb.VClock(sampler)
    scene = fb.Scene(vc, mode)
    scene.serial_ok = serial_ok
    scene.window_ms = window_ms
    scene.serial_t0 = vc.t
    scene.vc = vc
    screen = fb.FakeScreen(vc, scene, regions)
    mouse = fb.FakeMouse(vc, scene)
    up, tg, mt, _ocr, popf, _text = fb.make_shims(vc, scene, screen)
    ocr_fake = page_change(_ocr, scene)
    eng_mod.time = vc
    eng_mod.upscale = up
    eng_mod.to_gray = tg
    eng_mod.match = mt
    eng_mod.ocr = ocr_fake
    popup_mod.ocr = ocr_fake
    eng_mod.popup = popf

    eng = fb._make_bench_engine(eng_mod)()
    eng._stop = threading.Event()
    eng._target_hwnd = 123
    eng._say = lambda *a, **k: None
    eng._stats = {"hz": 0.0, "score": 0.0, "clicks": 0}
    eng._lock = threading.Lock()
    eng.time = vc

    cards_mod = importlib.import_module("core.cards")
    cards_mod.locate_cards = lambda img, *a, **k: [(0, 0, 10, 10)] * 6
    cards_mod.changed_cards = lambda a, b, c, *ar, **kw: []
    cards_mod.read_card = lambda frame, card, *a, **k: {
        "text": "枪械外观 2050", "price": "2050", "grade": "崭新出厂",
        "name": "枪械外观"}
    eng_mod.records.add = lambda **kw: vc.charge("records_add")

    cfg = Cfg()
    return eng_mod, eng, cfg, screen, mouse, scene, vc, ocr_fake


def entry_ms(eng_mod, eng, screen, vc, scene):
    """走引擎自己的「进详情后等面板」那一段：条件等待 + 首读帧。

    返回 (等待毫秒, 读钟毫秒, 这一段合计毫秒, 就绪时读到的编号)。
    最后一项是守门的「编号一致性」：等待一结束立刻再读一次面板编号，
    与本次点开的那一行（NEW）比对 —— 不一致就说明「就绪」判早了。
    """
    scene.serial_t0 = vc.t                  # 相当于「这一件刚点开」的瞬间
    t0 = vc.t
    cap = min(CAP, 2.0)
    # 不能用 hasattr：BenchEngine.__getattr__ 会给任何 _xxx 返回空占位。
    if "_wait_detail_ready" in vars(eng_mod.Engine):
        eng._wait_detail_ready(screen, cap)
    else:
        vc.sleep(cap)                       # 老代码：固定睡 0.55s
    t1 = vc.t
    try:
        eng._ocr_clock(screen, screen.norm_to_px(fb_N_CLOCK()))
    except Exception:                       # noqa: BLE001
        pass
    t2 = vc.t                                # 计时到此为止，别把一致性检查算进去
    seen = None                             # None = 这条路径没有「就绪时读编号」
    if "_read_serial_fast" in vars(eng_mod.Engine):
        seen = eng._read_serial_fast(screen) or ""
    return (t1 - t0) * 1000, (t2 - t1) * 1000, (t2 - t0) * 1000, seen


_clock_px = {}


def fb_N_CLOCK():
    if "px" not in _clock_px:
        from core.nav_coords import N_CLOCK
        _clock_px["px"] = dict(N_CLOCK)
    return _clock_px["px"]


def _stat(xs, with_max=True):
    s = fb._stat(xs)
    if with_max:
        s["max"] = round(max(xs), 2) if xs else 0.0
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="perf/merged")
    ap.add_argument("--run", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    res = {}

    # ---- 命中：换页窗口四档，只测「进详情后等面板」这一段 ----
    hit = {}
    for wm in WINDOWS:
        eng_mod, eng, cfg, screen, mouse, scene, vc, ocr = build(
            args.root, "hit", True, args.seed, wm)
        waits, after, bad = [], [], 0
        checked = 0
        for i in range(args.run):
            w, a, _tot, seen = entry_ms(eng_mod, eng, screen, vc, scene)
            waits.append(w)
            after.append(a)
            if seen is not None:
                checked += 1
                if seen != NEW:
                    bad += 1
        hit[str(wm)] = {"wait": _stat(waits), "clock_read": _stat(after),
                        "serial_mismatch": (bad if checked else None)}
    res["hit"] = hit

    # ---- 未命中：编号一直读不出（面板空/读失败） ----
    eng_mod, eng, cfg, screen, mouse, scene, vc, ocr = build(
        args.root, "hit", False, args.seed)
    waits, after, bad = [], [], 0
    for i in range(args.run):
        w, a, _tot, _seen = entry_ms(eng_mod, eng, screen, vc, scene)
        waits.append(w)
        after.append(a)
    res["miss"] = {"wait": _stat(waits), "clock_read": _stat(after),
                   "serial_mismatch": None,      # 未命中场景面板本来就空，不适用
                   "cap_ms": CAP * 1000}

    # ---- 连开十件：窗口 50 / 100 各十次，每次都必须重新建立换页证据 ----
    ten_all = {}
    for wm in (50, 100):
        eng_mod, eng, cfg, screen, mouse, scene, vc, ocr = build(
            args.root, "hit", True, args.seed, wm)
        each, bad = [], 0
        checked = 0
        for i in range(10):
            w, _a, _tot, seen = entry_ms(eng_mod, eng, screen, vc, scene)
            each.append(round(w, 2))
            if seen is not None:
                checked += 1
                if seen != NEW:
                    bad += 1
        ten_all[str(wm)] = {"each_ms": each, "max_ms": max(each),
                            "all_hit": max(each) < CAP * 1000 - 1,
                            "serial_mismatch": (bad if checked else None)}
    res["hit10"] = ten_all

    # ---- 单件端到端：进详情等待 + 开点/确认/校验/收奖励 ----
    e2e = {}
    for wm in (50, 100, 300):
        entry, fire, total, okc, bad = [], [], [], [], 0
        checked = 0
        for i in range(args.run):
            seed = args.seed + i
            # 进详情等待与后面的开点/确认用同一个种子但各自独立的时钟与采样器：
            # 这样「等待」少耗的样本不会把后面那段的随机流整体挪位，两边的
            # fire 数字逐个可比，total 的差才是纯提速。
            eng_mod, eng, cfg, screen, mouse, scene, vc, ocr = build(
                args.root, "hit", True, seed, wm)
            w, _a, tot, seen = entry_ms(eng_mod, eng, screen, vc, scene)
            entry.append(w)
            if seen is not None:
                checked += 1
                if seen != NEW:
                    bad += 1
            eng_mod2, eng2, cfg2, screen2, mouse2, scene2, vc2, ocr2 = build(
                args.root, "hit", True, seed, wm)
            scene2.phase = "fire"
            ok, dur = fb.run_fire(eng2, cfg2, screen2, mouse2, scene2, ocr2, None)
            fire.append(dur * 1000)
            total.append(tot + dur * 1000)
            okc.append(1 if ok is True else 0)
        e2e[str(wm)] = {"entry": _stat(entry), "fire": _stat(fire),
                        "total": _stat(total), "ok": _stat(okc),
                        "serial_mismatch": (bad if checked else None)}
    res["e2e"] = e2e
    res["has_cond_wait"] = "_wait_detail_ready" in vars(eng_mod.Engine)

    print(json.dumps(res, ensure_ascii=False, indent=1))
    outp = args.out or os.path.join(
        fb.PROJ, "perf", "out", "p1_%s.json" % os.path.basename(args.root))
    with open(outp, "w", encoding="utf-8") as f:
        json.dump({"root": args.root, "res": res}, f, ensure_ascii=False, indent=1)
    print("输出:", outp)


if __name__ == "__main__":
    main()
