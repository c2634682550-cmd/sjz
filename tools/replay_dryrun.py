#!/usr/bin/env python
"""离线帧回放（dry-run）：只跑识别 / 决策链路，一个注入调用都不碰。

输入
  · `--preset fixtures`  仓库里的真实素材（tests/fixtures/market、price_roi）
  · `--frames DIR`       一目录 PNG 帧；`--kind` 指定这目录里是什么画面
  · `--text-frames FILE` 「大厅 → 交易行」文本帧序列（JSONL，真机 OCR 原文）

输出
  · 控制台摘要表
  · `--out` 指定的 JSON（每帧一条：页面、价格原文/解析值、匹配结果、
    命中则该点的坐标、整帧耗时）

硬约束
  · 不 import core.input，不构造 Mouse / Keyboard，不调用任何发送函数；
    需要注入才能测的路径标 `skipped` 并写原因。
  · 默认不碰屏幕；只有 `--frames` 指向的磁盘图片会被读。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BASE_SIZE = (1920, 1080)
_IMPORT_BASELINE = set(sys.modules)     # 本模块加载瞬间的模块快照（注入审计基线）


# --------------------------------------------------------------------------- #
# 素材清单：每一项都写明「这一帧是什么 / 坐标原点在哪」。
# 这些是 tests/fixtures 下的真实截图片段，不是合成图。
# `origin` 是 ROI 在客户区里的左上角（归一化→像素由 config 决定），
# 用来把 ROI 内坐标换算成客户区坐标；ROI 尺寸与配置区域不完全一致时
# 标 `origin_source="assumed"`，只作参考，不当成真机落点证据。
# --------------------------------------------------------------------------- #
PRESET = [
    {"id": "list_unfiltered", "file": "tests/fixtures/market/list_unfiltered.png",
     "kind": "list", "region": "list", "origin": None,
     "note": "在售列表 6 卡（高价，用于验证「不匹配」）"},
    {"id": "list_filtered", "file": "tests/fixtures/market/list_filtered.png",
     "kind": "list", "region": "list", "origin": None,
     "note": "在售列表 6 卡（低价优品，用于验证「命中」）"},
    {"id": "detail_panel", "file": "tests/fixtures/market/detail_panel.png",
     "kind": "detail", "origin": None,
     "note": "详情面板（含价格条与购买按钮）"},
    {"id": "insufficient", "file": "tests/fixtures/market/insufficient.png",
     "kind": "popup", "origin": None,
     "note": "余额不足弹窗（含「取消」按钮）"},
    {"id": "catalog", "file": "tests/fixtures/market/catalog.png",
     "kind": "no_target", "origin": None,
     "note": "典藏外观列表（无价格、无目标 → 不该点）"},
    {"id": "filter_panel", "file": "tests/fixtures/market/filter_panel.png",
     "kind": "no_target", "origin": None,
     "note": "筛选面板（无目标 → 不该点）"},
]

# 目标库条件：同一帧按两组条件分别判，覆盖「命中 / 不匹配」两种结果。
DEFAULT_TASKS = [
    {"id": "t_strict", "name": "QBZ95-1突击步枪-王牌之剑",
     "keywords": ["王牌之剑"], "craft": "优品", "max_price": 3000},
    {"id": "t_loose", "name": "QBZ95-1突击步枪-王牌之剑",
     "keywords": ["王牌之剑"], "max_price": 30000},
]


def norm_px(region, size=BASE_SIZE):
    """归一化区域 → (x, y, w, h) 像素。与 core.screen.Screen.norm_to_px 同义。"""
    if not isinstance(region, dict):
        return None
    x = int(round(float(region["x"]) * size[0]))
    y = int(round(float(region["y"]) * size[1]))
    w = int(round(float(region["w"]) * size[0]))
    h = int(round(float(region["h"]) * size[1]))
    return (x, y, w, h)


def center_px(region, size=BASE_SIZE):
    box = norm_px(region, size)
    if box is None:
        return None
    return int(box[0] + box[2] / 2.0), int(box[1] + box[3] / 2.0)


def _clip(text, n=40):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _price_token(text):
    """从卡片文字里抠出价格原文（最后一个带千分位/纯数字的串）。"""
    import re
    toks = re.findall(r"\d[\d,]*", str(text or ""))
    return toks[-1] if toks else None


# --------------------------------------------------------------------------- #
# 各画面的产品链路
# --------------------------------------------------------------------------- #

def decide_list(frame, cfg, tasks, origin, scene):
    """列表页：market.scan（真实定位+OCR）→ check/pick → 卡片落点。"""
    from core import market
    t0 = time.perf_counter()
    rows = market.scan(frame)
    ms = (time.perf_counter() - t0) * 1000.0

    frame_rows = []
    for r in rows:
        frame_rows.append({
            "text": _clip(r.get("text"), 46),
            "price_raw": _price_token(r.get("text")),
            "price": r.get("price"),
            "craft": r.get("craft"),
            "wear": r.get("wear"),
            "box": [round(r.get("x0", 0), 1), round(r.get("y0", 0), 1),
                    round(r.get("x1", 0), 1), round(r.get("y1", 0), 1)],
        })

    div = (cfg.region("list") if cfg is not None else None)
    list_px = norm_px(div) if div else (0, 0, 0, 0)
    if origin is None:
        ox, oy = list_px[0], list_px[1]
    else:
        ox, oy = origin

    decisions = []
    for task in tasks:
        best, why, diag = market.pick(rows, task)
        point = None
        if best is not None:
            x = int(ox + max(best.get("x0", 0), 0) + 40)
            y = int(oy + int(best.get("cy", 0)))
            point = {
                "x": x, "y": y, "space": "client",
                "source": "engine.list_card（与 _execute_list 同式：x0+40 / cy）",
                "bbox": [int(ox + best.get("x0", 0)), int(oy + best.get("y0", 0)),
                         int(ox + best.get("x1", 0)), int(oy + best.get("y1", 0))],
            }
        decisions.append({
            "task": task.get("id"), "matched": best is not None, "reason": why,
            "row": None if best is None else {
                "price": best.get("price"),
                "text": _clip(best.get("text"), 46),
                "craft": best.get("craft"), "wear": best.get("wear"),
            },
            "point": point,
            "diagnostics": diag,
        })

    prices = [r.get("price") for r in rows]
    return {
        "page": scene, "page_source": "ROI 场景声明",
        "price_raw": " / ".join(_clip(r.get("text"), 22) for r in rows[:6]),
        "price_value": prices,
        "rows": frame_rows, "tasks": decisions, "ms": round(ms, 1),
        "decision": "click" if any(d["matched"] for d in decisions) else "none",
    }


def decide_detail(frame, cfg, tasks, scene):
    """详情页：面板 OCR → 价格解析 → 条件比对 → 购买按钮落点。"""
    from core import bricks, ocr, ocr_chain
    t0 = time.perf_counter()
    words = ocr.read_words(frame)
    text = "".join(str(w.get("text") or "") for w in words)
    price, price_tok = ocr_chain.price_from_words(words)
    craft = bricks.parse_craft(text)
    wear = bricks.parse_wear(text)
    ms = (time.perf_counter() - t0) * 1000.0

    buy_region = cfg.region("buy") if cfg is not None else None
    point = None
    if buy_region is not None:
        x, y = center_px(buy_region)
        b = norm_px(buy_region)
        point = {"x": x, "y": y, "space": "client",
                 "source": "cfg.region('buy') 中心（与 _fire_and_confirm 同式）",
                 "bbox": [b[0], b[1], b[0] + b[2], b[1] + b[3]]}

    decisions = []
    for task in tasks:
        why = []
        ok = True
        hi = float(task.get("max_price") or 0)
        lo = float(task.get("min_price") or 0)
        if price is None:
            ok = False
            why.append("价格没读出来")
        else:
            if hi and price > hi:
                ok = False
                why.append(f"价格 {price:.0f} 高于上限 {hi:.0f}")
            if lo and price < lo:
                ok = False
                why.append(f"价格 {price:.0f} 低于下限 {lo:.0f}")
        want_craft = (task.get("craft") or "").strip()
        if want_craft and craft and craft != want_craft:
            ok = False
            why.append(f"做工是 {craft}，要 {want_craft}")
        decisions.append({"task": task.get("id"), "matched": ok,
                          "reason": "、".join(why) or "符合",
                          "point": point if ok else None})

    return {"page": scene, "page_source": "ROI 场景声明",
            "price_raw": price_tok, "price_value": price,
            "fields": {"craft": craft, "wear": wear, "text": _clip(text, 90)},
            "tasks": decisions, "ms": round(ms, 1),
            "decision": "click" if any(d["matched"] for d in decisions) else "none"}


def decide_popup(frame, cfg, tasks, scene):
    """弹窗：popup.scan（真实识别）→ 该点「取消」还是不动。"""
    from core import popup
    t0 = time.perf_counter()
    res = popup.scan(frame)
    ms = (time.perf_counter() - t0) * 1000.0
    point = None
    if res.get("found") and res.get("kind") == "insufficient":
        pt = popup.cancel_point(res)
        if pt is not None:
            x, y = norm_px_pt(pt)
            point = {"x": x, "y": y, "space": "frame-local(ROI)",
                     "source": "popup.cancel_point",
                     "bbox": None}
    return {"page": scene, "page_source": "ROI 场景声明",
            "price_raw": _clip(res.get("text"), 80),
            "price_value": res.get("shortfall"),
            "popup": {k: res.get(k) for k in ("found", "kind", "shortfall")},
            "tasks": [{"task": "*", "matched": bool(res.get("found")),
                       "reason": res.get("why") or res.get("kind") or "未识别到弹窗",
                       "point": point}],
            "ms": round(ms, 1),
            "decision": "click" if point else "none"}


def norm_px_pt(pt):
    try:
        return int(pt[0]), int(pt[1])
    except (TypeError, IndexError):
        return None, None


def decide_no_target(frame, cfg, tasks, scene):
    """无目标页 / 筛选面板：只识别文字，决策恒为「不点」。"""
    from core import ocr, nav
    t0 = time.perf_counter()
    text = "".join(str(w.get("text") or "") for w in ocr.read_words(frame))
    ms = (time.perf_counter() - t0) * 1000.0
    return {"page": scene, "page_source": "ROI 场景声明",
            "price_raw": _clip(text, 80), "price_value": None,
            "nav_classify": nav.classify(text),
            "tasks": [{"task": "*", "matched": False,
                       "reason": "本页没有可购买目标", "point": None}],
            "ms": round(ms, 1), "decision": "none"}


def decide_price_roi(frame, expected, name):
    """价格 ROI：直接走 engine._bar_price 同源的 ocr.read_price。"""
    from core import ocr
    t0 = time.perf_counter()
    value = ocr.read_price(frame)
    ms = (time.perf_counter() - t0) * 1000.0
    ok = value == expected
    return {"page": "price_bar", "page_source": "price_roi manifest",
            "price_raw": expected, "price_value": value,
            "tasks": [{"task": "expected", "matched": ok,
                       "reason": f"期望 {expected}，读到 {value}", "point": None}],
            "ms": round(ms, 1), "decision": "none", "file": name}


# --------------------------------------------------------------------------- #
# 「大厅 → 进入交易行」文本帧回放
# --------------------------------------------------------------------------- #

def replay_text_frames(path, cfg, tasks):
    from core import nav

    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            page = nav.classify(item.get("text") or "")
            want = item.get("expect")            # hall / transition / loading / market
            if want == "market":
                ok = page in nav.MARKET_PAGES     # 在售/详情等子页也算进了交易行
            elif want in ("transition", "loading"):
                # 过渡/加载帧绝不能判成交易行任一页；判 hall / unknown 都可接受
                ok = page not in nav.MARKET_PAGES
            elif want == "hall":
                ok = page == "hall"
            else:
                ok = page == want
            out.append({"id": item.get("id"), "file": path, "kind": "text_frame",
                        "phase": item.get("phase"), "page": page, "expect": want,
                        "ok": ok, "source": item.get("source"),
                        "price_raw": _clip(item.get("text"), 30),
                        "price_value": None, "ms": 0.0,
                        "decision": "none",
                        "tasks": [{"task": "nav.classify", "matched": ok,
                                   "reason": f"期望 {want}，判为 {page}",
                                   "point": None}]})
    return out


# --------------------------------------------------------------------------- #
# 注入隔离自检
# --------------------------------------------------------------------------- #

def injection_audit():
    """回放期间不许「新加载」注入栈；基线里已有的（被同进程别处导入）单列。"""
    needle = ("core.input", "core.interception", "core.winput")
    bad = [m for m in needle if m in sys.modules and m not in _IMPORT_BASELINE]
    pre = [m for m in needle if m in _IMPORT_BASELINE]
    return {"loaded_injection_modules": bad, "baseline_injection_modules": pre,
            "injections": 0, "ok": not bad}


# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="离线帧回放（dry-run）")
    ap.add_argument("--preset", default="fixtures", choices=("fixtures", "none"))
    ap.add_argument("--frames", help="PNG 帧目录")
    ap.add_argument("--kind", default="list",
                    choices=("list", "detail", "popup", "no_target", "price_roi"))
    ap.add_argument("--origin", default=None, help="ROI 在客户区的左上角，如 60,210")
    ap.add_argument("--text-frames", help="大厅→交易行 文本帧 JSONL")
    ap.add_argument("--price-manifest",
                    default="tests/fixtures/price_roi/manifest.json")
    ap.add_argument("--tasks", help="目标库条件 JSON 文件（数组）")
    ap.add_argument("--out", default=os.path.join("_dryrun_out", "replay.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)

    from core.config import Config
    from core.vision import imread

    cfg = Config.load()
    tasks = DEFAULT_TASKS
    if args.tasks:
        with open(args.tasks, "r", encoding="utf-8") as f:
            tasks = json.load(f)

    origin = None
    if args.origin:
        origin = tuple(int(v) for v in args.origin.split(","))

    frames = []

    # 1) 预设素材
    if args.preset == "fixtures":
        for scene in PRESET:
            path = os.path.join(ROOT, scene["file"])
            img = imread(path)
            if img is None:
                frames.append({"id": scene["id"], "file": scene["file"],
                               "kind": scene["kind"], "ms": 0.0,
                               "page": "missing", "price_value": None,
                               "tasks": [{"task": "*", "matched": False,
                                          "reason": "素材文件读不出来",
                                          "point": None}],
                               "decision": "skip",
                               "price_raw": None, "file_missing": True})
                continue
            kind = scene["kind"]
            if kind == "list":
                r = decide_list(img, cfg, tasks, scene.get("origin"), "onsale/list")
            elif kind == "detail":
                r = decide_detail(img, cfg, tasks, "detail")
            elif kind == "popup":
                r = decide_popup(img, cfg, tasks, "popup")
            else:
                r = decide_no_target(img, cfg, tasks,
                                     "no_target(%s)" % scene["id"])
            r.update({"id": scene["id"], "file": scene["file"],
                      "kind": kind, "note": scene.get("note")})
            frames.append(r)

        # 价格 ROI：manifest 里有真机原价，逐张验读完的值
        man = os.path.join(ROOT, args.price_manifest) if args.price_manifest else None
        if man and os.path.exists(man):
            with open(man, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            entries = manifest if isinstance(manifest, list) else manifest.get("items", [])
            for e in entries[: (args.limit or None)]:
                if e.get("cls") not in (None, "price"):
                    continue          # currency 是三角币余额，不是价格条，跳过
                rel = e.get("file") or e.get("path")
                if not rel:
                    continue
                p = rel if os.path.isabs(rel) else os.path.join(
                    ROOT, "tests", "fixtures", "price_roi", rel)
                img = imread(p)
                if img is None:
                    continue
                digits = str(e.get("digits") or "").replace(",", "").strip()
                expect = int(digits) if digits.isdigit() else None
                name = os.path.basename(rel)
                r = decide_price_roi(img, expect, name)
                r.update({"id": "price_" + e.get("id", name),
                          "file": os.path.relpath(p, ROOT), "kind": "price_roi"})
                frames.append(r)

    # 2) 目录帧
    if args.frames:
        base = args.frames if os.path.isabs(args.frames) else os.path.join(ROOT, args.frames)
        names = sorted(n for n in os.listdir(base) if n.lower().endswith(".png"))
        for n in names[: (args.limit or None)]:
            img = imread(os.path.join(base, n))
            if img is None:
                continue
            kind = args.kind
            if kind == "list":
                r = decide_list(img, cfg, tasks, origin, "onsale/list")
            elif kind == "detail":
                r = decide_detail(img, cfg, tasks, "detail")
            elif kind == "popup":
                r = decide_popup(img, cfg, tasks, "popup")
            elif kind == "no_target":
                r = decide_no_target(img, cfg, tasks, "no_target")
            else:
                r = decide_price_roi(img, None, n)
            r.update({"id": n, "file": os.path.relpath(os.path.join(base, n), ROOT),
                      "kind": kind})
            frames.append(r)

    # 3) 文本帧序列
    if args.text_frames:
        frames.extend(replay_text_frames(
            args.text_frames if os.path.isabs(args.text_frames)
            else os.path.join(ROOT, args.text_frames), cfg, tasks))

    audit = injection_audit()

    # ---------------- 控制台摘要 -----------------------------
    if not args.json_only:
        print("%-30s %-9s %-13s %-10s %-12s %s"
              % ("帧", "类型", "页面", "命中", "落点", "耗时ms"))
        print("-" * 96)
        for fr in frames:
            hit = sum(1 for t in fr.get("tasks", []) if t.get("matched"))
            pt = None
            for t in fr.get("tasks", []):
                if t.get("matched") and t.get("point"):
                    pt = t["point"]
                    break
            spot = "-" if not pt else "(%d,%d)" % (pt["x"], pt["y"])
            print("%-30s %-9s %-13s %-10s %-12s %s"
                  % (_clip(fr.get("id"), 28), fr.get("kind", ""),
                     str(fr.get("page")), "%d/%d" % (hit, len(fr.get("tasks", []))),
                     spot, fr.get("ms", 0)))
        print("-" * 96)
        tasks_n = sum(len(f.get("tasks", [])) for f in frames)
        hits_n = sum(1 for f in frames for t in f.get("tasks", []) if t.get("matched"))
        print("帧数 %d｜条件判定 %d 次（命中 %d）｜注入模块 %s"
              % (len(frames), tasks_n, hits_n,
                 audit["loaded_injection_modules"] or "未加载"))

    report = {
        "kind": "replay_dryrun",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
        "frames": frames,
        "audit": audit,
        "summary": {
            "frames": len(frames),
            "matched": sum(1 for f in frames for t in f.get("tasks", [])
                           if t.get("matched")),
            "checks": sum(len(f.get("tasks", [])) for f in frames),
            "missing_frames": [f["id"] for f in frames
                               if f.get("page") == "missing"],
            "injections": 0,
        },
    }
    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    if not args.json_only:
        print("→ %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
