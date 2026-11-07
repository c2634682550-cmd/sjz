"""流程线守门：验「行为」而不是「仿真数字」。

流程线补丁改的是引擎控制流（等条件、判据顺序、预筛、缓存），换一台机器
数字就会变，但语义不该变。所以这里用假 Screen / 假 Mouse / 脚本化 OCR
把真实方法绑到桩对象上跑，验四件事：

  1) 条件等待的上界不变，且「永远等不到元素」不卡死；
  2) 守卫预筛在真实「余额不足」素材上不漏判、在正常帧上不误判；
  3) 帧判等缓存只在整帧逐字节相同时复用，改一个像素、reset()、换尺寸都失效；
  4) 抽签相关文件（countdown / click_burst / config）一个字节都没动。

用法：
  python perf/bench/flow_gate.py --snapshot perf/snapshots/g5 --out out/flow_gate_g5.json
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
import time
import types

import numpy as np

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if BENCH_DIR not in sys.path:
    sys.path.insert(0, BENCH_DIR)

import common  # noqa: E402

# 用「整块纯色」把区域编码进像素均值：upscale/resize 不改变常量图，假 OCR
# 就能只靠 img.mean() 分辨自己拿到的是哪一块，不用碰引擎里的 cv2 调用。
TAG_SKIP, TAG_REWARD, TAG_CONT, TAG_LIST, TAG_FULL, TAG_CLOCK, TAG_POPUP = (
    17, 34, 51, 68, 200, 85, 102)


class FakeScreen:
    def __init__(self, w=1920, h=1080):
        self.w, self.h = w, h
        self.grabs = {}
        self.state = {}

    def norm_to_px(self, box):
        if isinstance(box, (tuple, list)):
            return tuple(int(v) for v in box)
        return (int(box["x"] * self.w), int(box["y"] * self.h),
                max(1, int(box["w"] * self.w)), max(1, int(box["h"] * self.h)))

    def grab(self, px=None):
        name = self.name_of(px)
        self.grabs[name] = self.grabs.get(name, 0) + 1
        if name == "skip":
            tag = TAG_SKIP
        elif name == "reward":
            tag = TAG_REWARD
        elif name == "cont":
            tag = TAG_CONT
        elif name == "list":
            tag = TAG_LIST
        else:
            tag = TAG_FULL
        if name in self.state and self.state[name] is None:
            return None
        if name == "skip" and not self.state.get("skip", False):
            tag = TAG_FULL
        if name == "cont" and not self.state.get("cont", False):
            tag = TAG_FULL
        if name == "reward" and not self.state.get("reward", False):
            tag = TAG_FULL
        if px is None:
            w, h = self.w, self.h
        elif isinstance(px, dict):
            w, h = px["width"], px["height"]
        else:
            w, h = int(px[2]), int(px[3])
        return np.full((h, w, 3), tag, dtype=np.uint8)

    def name_of(self, px):
        if px is None:
            return "full"
        if isinstance(px, dict):
            key = (px["left"], px["top"], px["width"], px["height"])
        else:
            key = (int(px[0]), int(px[1]), int(px[2]), int(px[3]))
        for name, box in self.regions.items():
            if self.norm_to_px(box) == key:
                return name
        return "full"


class FakeMouse:
    def __init__(self):
        self.clicks = 0

    def ensure_foreground(self, hwnd, screen=None):
        return True

    def click(self, x, y, expect_foreground=None, hold=None):
        self.clicks += 1


def make_fake_ocr(ocr_mod, state):
    calls = {"read_text": 0, "read_words": 0, "by_tag": {}}

    def tag_of(img):
        return int(round(float(img.mean()))) if img is not None else -1

    def read_text(img):
        calls["read_text"] += 1
        tag = tag_of(img)
        calls["by_tag"][tag] = calls["by_tag"].get(tag, 0) + 1
        if tag == TAG_SKIP:
            return "跳过" if state.get("skip", True) else ""
        if tag == TAG_REWARD:
            return "获得枪械外观" if state.get("reward", True) else ""
        if tag == TAG_CONT:
            return "继续" if state.get("cont", True) else ""
        return ""

    def read_words(img):
        calls["read_words"] += 1
        tag = tag_of(img)
        calls["by_tag"][tag] = calls["by_tag"].get(tag, 0) + 1
        if tag == TAG_CONT:
            return [{"text": "继续"}] if state.get("cont", True) else []
        return []

    ocr_mod.read_text = read_text
    ocr_mod.read_words = read_words
    return calls


def stub_for(engine_mod, screen, mouse, nav):
    st = types.SimpleNamespace()
    st.screen = screen
    st.mouse = mouse
    st._target_hwnd = 1
    st._stop = __import__("threading").Event()
    st._says = []
    st._say = lambda msg, tag="info": st._says.append((tag, msg))
    st._click = lambda m, x, y, hold=None: m.click(x, y)
    st.regions = {}
    for name, key in (("skip", nav.N_SKIP), ("reward", nav.N_REWARD),
                      ("cont", getattr(engine_mod, "CONT_STRIP", nav.CONTINUE_POINT)),
                      ("list", nav.N_LIST),
                      ("popup", nav.N_POPUP_HINT), ("clock", nav.N_CLOCK)):
        st.regions[name] = key
    screen.regions = st.regions
    for meth in ("_bought_dialog", "_close_reward", "_insufficient_showing",
                 "_skip_shown", "_list_back"):
        fn = getattr(engine_mod.Engine, meth, None)
        if fn is not None:
            setattr(st, meth, types.MethodType(fn, st))
    # _center 是 staticmethod：不能绑 self，直接拿裸函数挂上去
    if hasattr(engine_mod.Engine, "_center"):
        st._center = engine_mod.Engine._center
    return st


# ---------------------------------------------------------------- 结构检查

def structural(snapshot):
    """用 AST/文本检查「上界有没有偷偷变」这类只能看代码的事。"""
    out = {}
    path = os.path.join(snapshot, "core", "engine.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    gaps = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "gaps":
            gaps = [float(e.value) for e in node.value.elts]
    out["verify_gaps"] = gaps
    out["verify_window_ms"] = round(sum(gaps) * 1000.0, 1) if gaps else None
    out["verify_first_check_immediate"] = bool(gaps and gaps[0] == 0.0)
    out["verify_checks"] = len(gaps) if gaps else None
    out["has_detail_wait"] = "_wait_detail_ready" in src
    out["detail_wait_cap"] = ("min(0.55, detail_timeout)" in src) if "_wait_detail_ready" in src else None
    out["close_reward_bounds"] = ("< 2.0" in src and "< 2.5" in src)
    out["guard_last_try_full_scan"] = "i < tries - 1" in src
    out["guard_fail_open"] = "hint = True" in src
    out["memo_in_market"] = "_memo_img" in open(
        os.path.join(snapshot, "core", "market.py"), encoding="utf-8").read()
    out["skip_first_in_bought_dialog"] = "((SKIP_N, (\"跳过\",))," in src
    return out


def file_hashes(snapshot):
    out = {}
    for name in ("countdown.py", "click_burst.py", "config.py", "market.py"):
        path = os.path.join(snapshot, "core", name)
        with open(path, "rb") as f:
            out[name] = hashlib.sha256(f.read()).hexdigest()[:12]
    return out


# ---------------------------------------------------------------- 行为检查

def test_bought_dialog(st, engine_mod, calls, state):
    res = {}
    for label, kv in (("skip_only", {"skip": True, "reward": False}),
                      ("reward_only", {"skip": False, "reward": True}),
                      ("none", {"skip": False, "reward": False}),
                      ("both", {"skip": True, "reward": True})):
        st.screen.state.update(kv)
        before = dict(calls["by_tag"])
        got = st._bought_dialog(st.screen)
        reads = sum(calls["by_tag"].values()) - sum(before.values())
        res[label] = {"result": bool(got), "regions_read": reads}
    return res


def test_close_reward(st, engine_mod, calls, nav, drop_after=None, cont=True,
                      list_back_after=None):
    st.screen.state.update({"skip": True, "reward": True, "cont": cont})
    st._bought_dialog = types.MethodType(engine_mod.Engine._bought_dialog, st)
    # 让「跳过」在 drop_after 秒后消失；list 在 list_back_after 秒后出现
    st._close_reward(st.screen, st.mouse)
    return st


def run_close_reward(st, engine_mod, calls, nav, drop_after=None,
                     cont=True, list_cards_after=None, timeout_s=6.0):
    """把时间相关的元素换成按绝对时间变化的假实现，避免真等 4.5s。"""
    t_start = time.perf_counter()

    def skip_shown(screen):
        if drop_after is None:
            return True
        return (time.perf_counter() - t_start) < drop_after

    def list_back(screen, list_px):
        if list_cards_after is None:
            return False
        return (time.perf_counter() - t_start) >= list_cards_after

    st._skip_shown = types.MethodType(lambda self, screen: skip_shown(screen), st)
    st._list_back = types.MethodType(lambda self, screen, px: list_back(screen, px), st)
    st._bought_dialog = types.MethodType(engine_mod.Engine._bought_dialog, st)
    st.mouse.clicks_before = st.mouse.clicks
    t0 = time.perf_counter()
    st._close_reward(st.screen, st.mouse)
    return round((time.perf_counter() - t0) * 1000.0, 1)


def compose_screen(img, w, h):
    """把素材还原成整屏：弹窗素材是 690x400 的裁剪，居中贴回 1920x1080。"""
    if img.shape[0] == h and img.shape[1] == w:
        return img
    canvas = np.zeros((h, w, 3), dtype=np.uint8)
    ih, iw = img.shape[:2]
    y0 = max(0, (h - ih) // 2)
    x0 = max(0, (w - iw) // 2)
    canvas[y0:y0 + min(ih, h), x0:x0 + min(iw, w)] = img[:min(ih, h), :min(iw, w)]
    return canvas


def test_guard_prefilter(st, mods, engine_mod, snapshot):
    """真实素材：不足弹窗必须命中；正常帧必须不命中。"""
    mats = common.materials(snapshot)
    fx = mats["market"]
    out = {"hit": [], "false_pos": 0, "checked": 0}
    ins = os.path.join(fx, "insufficient.png")
    real = mods["vision"].imread(ins)
    if real is None:
        out["error"] = "缺少 insufficient.png"
        return out
    px = st.screen.norm_to_px(st.regions["popup"])
    left, top, bw, bh = px
    canvas = compose_screen(real, st.screen.w, st.screen.h)
    crop = canvas[top:top + bh, left:left + bw]
    hit = st._insufficient_showing(types.SimpleNamespace(
        norm_to_px=lambda b: (0, 0, crop.shape[1], crop.shape[0]),
        grab=lambda b: crop))
    out["hit"].append(bool(hit))
    out["hint_text"] = mods["ocr"].read_text(crop)
    for name in sorted(os.listdir(fx)):
        if not name.lower().endswith(".png") or name == "insufficient.png":
            continue
        img = mods["vision"].imread(os.path.join(fx, name))
        if img is None:
            continue
        out["checked"] += 1
        c2 = compose_screen(img, st.screen.w, st.screen.h)[top:top + bh, left:left + bw]
        fp = st._insufficient_showing(types.SimpleNamespace(
            norm_to_px=lambda b, c=c2: (0, 0, c.shape[1], c.shape[0]),
            grab=lambda b, c=c2: c))
        if fp:
            out["false_pos"] += 1
            out["false_names"] = out.get("false_names", []) + [name]
    out["hit_rate"] = round(sum(1 for x in out["hit"] if x) / max(1, len(out["hit"])), 4)
    return out


def test_memo(mods, snapshot):
    """帧判等缓存：同帧复用、异帧重算、reset 失效。"""
    market = mods["market"]
    vision = mods["vision"]
    mats = common.materials(snapshot)
    img = vision.imread(os.path.join(mats["market"], "list_unfiltered.png"))
    sc = market.Scanner(upscale=1)
    res = {}

    def timed_scan(frame):
        t = common.now()
        before = sc.ocr_calls
        rows = sc.scan(frame)
        return round((common.now() - t) * 1000.0, 2), sc.ocr_calls - before, rows

    ms1, c1, rows1 = timed_scan(img)
    ms2, c2, rows2 = timed_scan(img)
    res["same_frame"] = {"first_ms": ms1, "second_ms": ms2, "ocr_calls_2nd": c2,
                         "rows_equal": all(a == b for a, b in zip(rows1, rows2))
                         and len(rows1) == len(rows2)}
    mod = img.copy()
    mod[5, 5, 0] = (int(mod[5, 5, 0]) + 1) % 255
    ms3, c3, _ = timed_scan(mod)
    res["one_pixel_changed"] = {"ms": ms3, "ocr_calls": c3, "recomputed": c3 > 0 or ms3 > ms2 * 2}
    sc.reset()
    ms4, c4, _ = timed_scan(img)
    res["after_reset"] = {"ms": ms4, "ocr_calls": c4, "recomputed": ms4 > ms2 * 2}
    small = vision.imread(os.path.join(mats["market"], "list_unfiltered.png"))
    if small is not None:
        crop = small[: small.shape[0] // 2]
        ms5, c5, _ = timed_scan(crop)
        res["shape_changed"] = {"ms": ms5, "recomputed": ms5 > ms2 * 2}
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", default=os.path.join(BENCH_DIR, "out", "flow_gate.json"))
    ap.add_argument("--baseline", default=None, help="对照快照（默认 base_0930）")
    args = ap.parse_args(argv)

    root = common.load_snapshot(args.snapshot)
    mods = common.import_core(root)
    cfg = common.load_json(os.path.join(BENCH_DIR, "bench_config.json"))
    common.pin_config(mods["config"], mods["ocr"], cfg.get("config_overrides"))

    engine_mod = __import__("core.engine", fromlist=["engine"])
    nav = __import__("core.nav_coords", fromlist=["nav_coords"])

    screen = FakeScreen()
    mouse = FakeMouse()
    st = stub_for(engine_mod, screen, mouse, nav)

    doc = {"snapshot": root, "structural": structural(root),
           "hashes": file_hashes(root)}

    # 先用真 OCR：预筛命中率与帧缓存都必须对着真素材量，脚本化 OCR 会得出假结论
    src = open(os.path.join(root, "core", "engine.py"), encoding="utf-8").read()
    if "i < tries - 1" in src:
        doc["guard_prefilter"] = test_guard_prefilter(st, mods, engine_mod, root)
    doc["memo"] = test_memo(mods, root)

    fake_state = {"skip": True, "reward": True, "cont": True}
    calls = make_fake_ocr(mods["ocr"], fake_state)
    if hasattr(engine_mod.Engine, "_bought_dialog"):
        doc["bought_dialog"] = test_bought_dialog(st, engine_mod, calls, fake_state)

    if hasattr(engine_mod.Engine, "_close_reward") and getattr(st, "_skip_shown", None):
        doc["close_reward"] = {
            "skip_never_gone": run_close_reward(st, engine_mod, calls, nav),
            "skip_gone_fast": run_close_reward(
                st, engine_mod, calls, nav, drop_after=0.15, list_cards_after=0.20),
            "no_strip_fallback_reads_full": None,
        }
        fake_state["cont"] = False
        calls["read_words"] = 0
        run_close_reward(st, engine_mod, calls, nav, drop_after=0.05,
                         list_cards_after=0.10)
        doc["close_reward"]["no_strip_fallback_reads_full"] = calls["read_words"] > 0
        fake_state["cont"] = True

    base = args.baseline or os.path.join(os.path.dirname(root), "base_0930")
    if os.path.isdir(base):
        doc["hash_vs_base"] = {}
        bh = file_hashes(base)
        for k, v in doc["hashes"].items():
            doc["hash_vs_base"][k] = "same" if v == bh.get(k) else "changed"

    doc["cpu_load"] = common.cpu_load()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
