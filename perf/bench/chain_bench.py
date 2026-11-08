"""主链路离线台架：真的跑 `Navigator.go_to_onsale`，屏幕用合成页面。

页面素材来自 tests/fixtures/market 的真机 ROI（原文像素粘贴），
在此基础上补齐导航必需的像素/文字（绿色页签框、购买按钮、页码、公示期标签）。

用法：
    .\.venv\Scripts\python.exe perf\bench\chain_bench.py --verify   # 先验收每页识别
    .\.venv\Scripts\python.exe perf\bench\chain_bench.py --rep 3    # 计时

口径：
  · 与真机同一条代码路径（F4 → 交易行 → 典藏外观 → 选皮肤 → 购买 → 挂单列表）；
  · 不注入 OCR、不跳过任何校验；
  · 结果只反映「OCR/等待」成本，不含真实显卡渲染抖动。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

W, H = 1920, 1080
FONT = "C:/Windows/Fonts/msyh.ttc"
GREEN = (86, 177, 133)          # BGR，与真机选中框一致

_cache = {}


def _text(img, xy, s, size=16, color=(235, 235, 235)):
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    d.text(xy, s, font=ImageFont.truetype(FONT, size), fill=color[::-1])
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def _rect(img, x0, y0, x1, y1, color):
    cv2.rectangle(img, (x0, y0), (x1, y1), color, -1)


def _paste(img, name, x, y):
    src = cv2.imread(os.path.join(ROOT, "tests", "fixtures", "market", name))
    h, w = src.shape[:2]
    img[y:y + h, x:x + w] = src


def page_hall():
    img = np.zeros((H, W, 3), np.uint8)
    img = _text(img, (300, 40), "开始游戏", 30)
    img = _text(img, (300, 100), "特战干员   部门   交易行   特勤处   行前备战", 16)
    return img


def page_market():
    # 灰底：真机页面在切页时整屏变化远大于 click() 的 10% 判据；
    # 纯黑底会让「黑→暗色 UI」的差分不够，台架会白等满 3s 超时。
    img = np.full((H, W, 3), 100, np.uint8)
    _rect(img, 127, 50, 257, 58, GREEN)          # 曼德尔砖选中
    img = _text(img, (140, 24), "曼德尔砖   典藏外观   典藏挂饰", 16)
    img = _text(img, (560, 140), "典藏工坊", 24)
    img = _text(img, (560, 190), "市场流通量 28,000", 16)
    img = _text(img, (60, 300), "交易   筛选   典藏", 16)
    return img


def page_collection(selected=False):
    img = np.zeros((H, W, 3), np.uint8)
    _rect(img, 257, 50, 387, 58, GREEN)          # 典藏外观选中
    img = _text(img, (140, 24), "曼德尔砖   典藏外观   典藏挂饰", 16)
    _paste(img, "catalog.png", 55, 230)
    _rect(img, 1640, 858, 1740, 902, (40, 120, 60))
    img = _text(img, (1666, 866), "购买", 16, (250, 250, 250))
    if selected:
        # 点中之后整块右栏换成详情（真机也是这样变化，差分远超 5% 判据）
        _paste(img, "detail_panel.png", 1330, 150)
        _rect(img, 1640, 858, 1740, 902, (40, 120, 60))
        img = _text(img, (1666, 866), "购买", 16, (250, 250, 250))
        _rect(img, 60, 272, 420, 360, (200, 200, 200))
        img = _text(img, (1380, 300), "MK4冲锋枪-疾光魅影典藏", 18)
        img = _text(img, (1380, 340), "当前市场流通量 32,000", 14)
    return img


def page_detail():
    img = np.zeros((H, W, 3), np.uint8)
    _paste(img, "list_unfiltered.png", 0, 240)     # 1380×630 挂单列表
    _paste(img, "detail_panel.png", 1330, 150)     # 590×860 右侧详情
    img = _text(img, (250, 185), "只显示公示期", 14)
    img = _text(img, (480, 920), "第 1 页", 12)
    return img


def frames():
    if not _cache:
        _cache["hall"] = page_hall()
        _cache["market"] = page_market()
        _cache["collection"] = page_collection(False)
        _cache["collection_sel"] = page_collection(True)
        _cache["detail"] = page_detail()
    return _cache


class World:
    """假屏幕：按状态返回合成整屏。"""

    def __init__(self):
        self.size = (W, H)
        self.state = "hall"
        self.calls = 0

    def grab(self):
        self.calls += 1
        return frames()[self.state].copy()

    def set(self, s):
        self.state = s


class FakeMouse:
    def __init__(self, world):
        self.w = world

    def click(self, x, y, verify=False, expect_foreground=None):
        s = self.w.state
        if s == "market" and abs(x - 323) <= 70 and 40 <= y <= 70:
            self.w.set("collection")
        elif s == "collection" and 60 <= x <= 430 and 240 <= y <= 870:
            self.w.set("collection_sel")
        elif s == "collection_sel" and abs(x - 1700) <= 60 and abs(y - 885) <= 40:
            self.w.set("detail")
        return True

    def channel_status(self):
        return {"last_click": {"channel": "synth", "accepted": True}}


class FakeKeyboard:
    def __init__(self, world):
        self.w = world

    def tap(self, key):
        if key.upper() == "F4" and self.w.state == "hall":
            self.w.set("market")
        return True


class Timer:
    def __init__(self):
        self.rows = []

    def row(self, name, ms):
        self.rows.append((name, ms))


def verify():
    from core import nav
    from core import ocr
    for k, img in frames().items():
        words = ocr.read_words(img)
        text = "".join(w.get("text", "") for w in words)
        x, y, w, h = nav._PAGE_NO_BOX
        crop = img[y:y + h, x:x + w]
        from core import vision
        pno = "".join(w.get("text", "") for w in ocr.read_words(vision.upscale(crop, 6)))
        print(f"[{k}] page={nav.classify(text + pno):8s} page_no={pno!r} "
              f"chars={len(text)}")
        print(f"    text={text[:90]}")


def configure(reuse=True, fast=True):
    """按开关重建 OCR 配置，用于 A/B。"""
    from core import ocr
    cfg = ocr._load_settings()
    cfg["reuse_frame"] = bool(reuse)
    cfg["rapid"] = dict(cfg.get("rapid") or {})
    cfg["rapid"]["fast_det"] = bool(fast)
    ocr._settings = cfg
    ocr._backend = None
    ocr._frame.clear()
    ocr._stats = {"calls": 0, "empty": 0}


def run(rep=3, reuse=True, fast=True, quiet=False):
    from core import nav as navmod
    configure(reuse, fast)

    world = World()
    mouse = FakeMouse(world)
    kb = FakeKeyboard(world)
    logs = []
    nv = navmod.Navigator(screen=world, mouse=mouse, keyboard=kb,
                          snap=world.grab, log=lambda m, l="info": logs.append((l, m)),
                          opts={})

    phases = ["where", "enter_market", "tab", "skin_items", "pick_skin",
              "buy", "_buy_button_visible", "ensure_public_filter",
              "_read_text", "_read_page_no"]
    for name in phases:
        orig = getattr(nv, name)

        def wrap(fn, label):
            def inner(*a, **kw):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **kw)
                finally:
                    T.row(label, (time.perf_counter() - t0) * 1000.0)
            return inner
        setattr(nv, name, wrap(orig, name))

    from core import ocr
    T = Timer()
    totals = []
    for _ in range(rep):
        ocr._frame.clear()          # 每条链路从冷缓存开始（真机上一轮的帧早已不同）
        world.set("hall")
        t0 = time.perf_counter()
        page = nv.go_to_onsale(from_hall=True)
        totals.append((time.perf_counter() - t0) * 1000.0)
        if page not in ("detail", "onsale"):
            raise SystemExit(f"链路没走到挂单列表：{page}")

    agg = {}
    for name, ms in T.rows:
        agg.setdefault(name, []).append(ms)
    out = {"end_to_end_ms": totals,
           "phases": {k: {"n": len(v), "total_ms": round(sum(v), 1),
                          "avg_ms": round(sum(v) / len(v), 1)}
                      for k, v in sorted(agg.items())},
           "screen_grabs": world.calls,
           "rows": T.rows,
           "ocr": dict(ocr._stats)}
    if not quiet:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--rep", type=int, default=3)
    ap.add_argument("--reuse", type=int, default=1, help="1=同帧复用开，0=关")
    ap.add_argument("--fast", type=int, default=1, help="1=降采样 det 开，0=关")
    a = ap.parse_args()
    if a.verify:
        verify()
    else:
        run(a.rep, bool(a.reuse), bool(a.fast))
