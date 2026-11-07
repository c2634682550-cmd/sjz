# -*- coding: utf-8 -*-
"""B5 列表帧 memo 命中率采集：整帧相同 / 只有时间列变化 / 完全不同。

用途：判断扫描循环里「帧 memo」到底省了多少次 OCR。只有「整帧相同」才算
精确 memo 命中；「仅时间列变化」是把它排除后可以额外吃到的收益（扩展命中）。

用法：
  # 分析已存帧（按文件名排序）
  python tools/collect_b5_frame_memo.py --frames D:\\shots\\list
  # 现采（需屏幕/游戏窗口，opt-in；不打开新窗口、不动鼠标）
  python tools/collect_b5_frame_memo.py --grab --region 0,0,1920,1080 --count 40
  # 自检（合成帧）
  python tools/collect_b5_frame_memo.py --selftest
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _perf_collect_common import PROJ, default_out_dir, write_report  # noqa: E402

IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
PIXEL_DIFF = 6          # 单像素阈值（0-255）
MEAN_DIFF_FLAT = 0.5    # 平均差低于此值视为采集噪声
NOISE_RATIO = 0.001     # 变化像素占比低于此值视为噪声


def _np():
    import numpy as np
    return np


def _cv2():
    import cv2
    return cv2


def parse_roi(text, w, h):
    """'x,y,w,h'；全 ≤1 视为相对比例，否则视为像素。返回 (x1,y1,x2,y2)。"""
    parts = [float(x) for x in text.split(",")]
    if len(parts) != 4:
        raise ValueError("time-roi 需要 x,y,w,h")
    x, y, rw, rh = parts
    if max(parts) <= 1.0:
        x1, y1 = int(x * w), int(y * h)
        x2, y2 = int((x + rw) * w), int((y + rh) * h)
    else:
        x1, y1, x2, y2 = int(x), int(y), int(x + rw), int(y + rh)
    return max(0, x1), max(0, y1), min(w, x2), min(h, y2)


def load_frames(directory):
    files = [os.path.join(directory, f) for f in sorted(os.listdir(directory))
             if f.lower().endswith(IMG_EXT)]
    cv2 = _cv2()
    frames = []
    for p in files:
        img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            frames.append((p, img))
    return frames


def grab_frames(region, count, interval_ms, out_dir):
    """用 mss 采屏并存盘；不涉及鼠标/窗口。返回帧文件列表。"""
    import mss
    cv2 = _cv2()
    import numpy as np
    import time
    x, y, w, h = [int(v) for v in region.split(",")]
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with mss.mss() as sct:
        mon = {"left": x, "top": y, "width": w, "height": h}
        for i in range(count):
            shot = np.array(sct.grab(mon))[:, :, :3]
            p = os.path.join(out_dir, "grab_%04d.png" % i)
            cv2.imwrite(p, shot)
            paths.append(p)
            time.sleep(max(0.0, interval_ms / 1000.0))
    return paths


def classify_sequence(frames, roi_frac):
    """逐对比较，返回三类计数与明细。frames: [(path, gray)]。"""
    np = _np()
    cv2 = _cv2()
    counts = {"identical": 0, "time_only": 0, "different": 0}
    detail = []
    if len(frames) < 2:
        return counts, detail, 0
    h, w = frames[0][1].shape[:2]
    x1, y1, x2, y2 = parse_roi(roi_frac, w, h)

    for (pa, a), (pb, b) in zip(frames[:-1], frames[1:]):
        if a.shape != b.shape:
            counts["different"] += 1
            detail.append({"a": os.path.basename(pa), "b": os.path.basename(pb),
                           "class": "different", "reason": "shape"})
            continue
        diff = cv2.absdiff(a, b)
        mean = float(diff.mean())
        mask = diff > PIXEL_DIFF
        changed = int(mask.sum())
        ratio = changed / float(mask.size)
        if mean < MEAN_DIFF_FLAT and ratio < NOISE_RATIO:
            cls, reason = "identical", "mean=%.2f ratio=%.4f" % (mean, ratio)
        else:
            ys, xs = np.where(mask)
            if changed == 0:
                bx1 = by1 = bx2 = by2 = 0
            else:
                bx1, bx2 = int(xs.min()), int(xs.max())
                by1, by2 = int(ys.min()), int(ys.max())
            inside = (changed > 0 and bx1 >= x1 and bx2 < x2 and
                      by1 >= y1 and by2 < y2)
            if inside:
                cls, reason = "time_only", "bbox=(%d,%d)-(%d,%d)" % (bx1, by1, bx2, by2)
            else:
                cls, reason = "different", "bbox=(%d,%d)-(%d,%d)" % (bx1, by1, bx2, by2)
        counts[cls] += 1
        detail.append({"a": os.path.basename(pa), "b": os.path.basename(pb),
                       "class": cls, "reason": reason, "changed_ratio": round(ratio, 4)})
    return counts, detail, len(frames) - 1


def build_report(counts, detail, transitions, frames_n, roi, args, mode):
    pct = {k: (round(100.0 * v / transitions, 2) if transitions else None)
           for k, v in counts.items()}
    return {
        "script": "B5_frame_memo",
        "objective": "列表帧 memo 三类命中率",
        "mode": mode,
        "frames": frames_n,
        "transitions": transitions,
        "counts": counts,
        "ratios_pct": pct,
        "exact_memo_hit_pct": pct["identical"],
        "extended_hit_pct": (None if not transitions else
                             round(100.0 * (counts["identical"] + counts["time_only"])
                                   / transitions, 2)),
        "time_roi": roi,
        "detail": detail[:50],
        "status": "OK" if transitions >= args.min_transitions else "INSUFFICIENT",
        "source": args.frames or ("grab" if mode == "grab" else "selftest"),
    }


def render(rep):
    lines = ["B5 列表帧 memo 命中率",
             "帧数: %d  相邻对: %d  时间列 ROI: %s" %
             (rep["frames"], rep["transitions"], rep["time_roi"])]
    for k in ("identical", "time_only", "different"):
        lines.append("  %-11s %4d  %s%%" %
                     (k, rep["counts"][k],
                      "-" if rep["ratios_pct"][k] is None else rep["ratios_pct"][k]))
    lines.append("精确 memo 命中（整帧相同）: %s%%" % rep["exact_memo_hit_pct"])
    lines.append("扩展命中（整帧相同 + 仅时间列变）: %s%%" % rep["extended_hit_pct"])
    if rep["status"] == "INSUFFICIENT":
        lines.append("注意: 相邻对不足，不给结论。")
    return lines


def selftest():
    np = _np()
    cv2 = _cv2()
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))

    h, w = 200, 320
    base = np.zeros((h, w), dtype="uint8")
    cv2.rectangle(base, (10, 10), (140, 180), 60, -1)
    cv2.rectangle(base, (160, 40), (250, 90), 120, -1)
    cv2.putText(base, "ITEM 123", (20, 150), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, 200, 1, cv2.LINE_AA)

    seq = []
    for _ in range(10):                     # 10 张整帧相同
        seq.append(base.copy())
    for i in range(12):                      # 12 张只有右侧时间列变
        f = base.copy()
        val = 255 if i % 2 else 40
        cv2.rectangle(f, (292, 90), (310, 110), val, -1)
        seq.append(f)
    for i in range(8):                       # 8 张左半区大幅不同
        f = base.copy()
        cv2.rectangle(f, (20, 60), (120, 160), 255 if i % 2 else 10, -1)
        seq.append(f)
    frames = [("f%02d.png" % i, f) for i, f in enumerate(seq)]
    counts, _detail, n = classify_sequence(frames, "0.88,0,0.12,1")

    check("相邻对总数", n == len(seq) - 1, "n=%d" % n)
    check("整帧相同 = 9", counts["identical"] == 9, str(counts))
    check("仅时间列 = 12", counts["time_only"] == 12, str(counts))
    check("完全不同 = 8", counts["different"] == 8, str(counts))
    rep = build_report(counts, [], n, len(seq), "0.88,0,0.12,1",
                       argparse.Namespace(min_transitions=10, frames=None), "selftest")
    check("精确命中率", abs(rep["exact_memo_hit_pct"] - 31.03) <= 0.2,
          str(rep["exact_memo_hit_pct"]))
    check("扩展命中率", abs(rep["extended_hit_pct"] - 72.41) <= 0.3,
          str(rep["extended_hit_pct"]))

    # ROI 边界：把 ROI 缩到左上角，时间列变化应被判 different
    counts2, _, _ = classify_sequence(frames[9:22], "0,0,0.3,0.3")
    check("ROI 不含时间列时不再算 time_only", counts2["time_only"] == 0, str(counts2))

    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B5 列表帧 memo 命中率采集")
    ap.add_argument("--frames", help="已存帧目录")
    ap.add_argument("--grab", action="store_true", help="现采（opt-in）")
    ap.add_argument("--region", default="0,0,1920,1080")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--interval-ms", type=int, default=200)
    ap.add_argument("--time-roi", default="0.88,0,0.12,1",
                    help="时间列区域 x,y,w,h；全 ≤1 为比例")
    ap.add_argument("--min-transitions", type=int, default=20)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    out_dir = args.out_dir or default_out_dir("b5_frame_memo")
    if args.grab:
        grab_dir = os.path.join(out_dir, "frames")
        paths = grab_frames(args.region, args.count, args.interval_ms, grab_dir)
        mode = "grab"
        cv2 = _cv2()
        frames = []
        for p in paths:
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if img is not None:
                frames.append((p, img))
    elif args.frames:
        mode = "dir"
        frames = load_frames(args.frames)
    else:
        ap.error("需要 --frames / --grab / --selftest")

    counts, detail, n = classify_sequence(frames, args.time_roi)
    rep = build_report(counts, detail, n, len(frames), args.time_roi, args, mode)
    rep["out_dir"] = out_dir
    lines = render(rep)
    print("\n".join(lines))
    jp, tp = write_report(out_dir, "b5_frame_memo", rep, lines)
    print("输出: %s\n      %s" % (jp, tp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
