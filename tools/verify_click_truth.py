"""按「引擎实际点击坐标 vs 真实位置模型」口径出偏差矩阵。

⚠ 口径与前提（必读）：

  · 这里的「真实位置」**不是真机实测**，而是 `core/truth_model.py` 的
    默认模型：假设游戏 UI 以 1920×1080 为基准、客户区非 16:9 时**等比
    缩放并居中**（黑边/信箱），s = min(宽/1920, 高/1080)。
  · 所以本表能说明的只有一件事：**在该模型成立的前提下，引擎点击相对
    模型预测位置的偏差 ≤0.5px**。它不能说明「相对真机按钮位置的偏差」。
  · 游戏若锚定边角、按宽度独立拉伸、或 UI 有独立缩放档，模型就不成立。
    要证伪/替换模型，用 `--anchors` 注入真机标注表，或跑
    `tools/verify_truth_vs_annotations.py`（真实截图 + 人工标注）。

口径区别（这是本工具存在的理由）：

  · `verify_autocalib.py` 量的是「归一化值 × 客户区」的取整误差；
  · `verify_calib_bias.py` 量的是「报出偏移 vs 框中心」的匹配几何误差；
  · 本工具走完整链路：贴模板 → 自动标定 → 落盘坐标 → 读配置 →
    算引擎会点的那个点（区域中心）→ 和真实位置模型比。

    python tools/verify_click_truth.py
    python tools/verify_click_truth.py --anchors anchors.json

每一格要么给出偏差像素（目标：非 16:9 也 ≤2px），要么明确写「拦住」——
后者表示闸门不会带着未校对的坐标去点。
"""
import argparse
import json
import os
import sys
import tempfile

import cv2
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import autocalib as A          # noqa: E402
from core import config as cfgmod        # noqa: E402
from core import region_table as RT      # noqa: E402
from core import truth_model as TM       # noqa: E402
from core import vision                  # noqa: E402

# (显示名, 客户区物理像素)。1920×1200@150% → 物理 2880×1800。
CASES = (
    ("1280×720", (1280, 720)),
    ("1280×800", (1280, 800)),
    ("1366×768", (1366, 768)),
    ("1600×900", (1600, 900)),
    ("1920×1080", (1920, 1080)),
    ("1920×1200@150%", (2880, 1800)),
    ("2560×1440", (2560, 1440)),
    ("3440×1440", (3440, 1440)),
    ("3840×2160", (3840, 2160)),
)


def _tpl(name):
    return os.path.join(BASE, "templates", name + ".png")


def _true_boxes(size, anchors=None):
    """真实位置模型（可被 anchors 覆盖）。返回 `(boxes, meta)`。"""
    return TM.truth_boxes(size, RT.BUILTIN_PX, anchors)


def _frame(size, true_boxes, scale, skip=()):
    w, h = size
    frame = np.full((h, w, 3), 245, np.uint8)
    for name in ("buy", "confirm"):
        if name in skip:
            continue
        tpl = cv2.imread(_tpl(name))
        used = vision.scaled(tpl, scale)
        x, y, bw, bh = true_boxes[name]
        tx = int(round(x + bw / 2.0 - used.shape[1] / 2.0))
        ty = int(round(y + bh / 2.0 - used.shape[0] / 2.0))
        frame[ty:ty + used.shape[0], tx:tx + used.shape[1]] = used
    return frame


def _click_center(cfg, name, size):
    box = cfg.region(name)
    if not box:
        return None
    return ((box["x"] + box["w"] / 2.0) * size[0],
            (box["y"] + box["h"] / 2.0) * size[1])


def _run(size, skip=(), workflow=None, anchors=None):
    path = os.path.join(tempfile.mkdtemp(prefix="clicktruth_"), "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"queue": []}, fh)
    cfgmod.CONFIG_PATH = path
    cfg = cfgmod.Config.load()
    true_boxes, meta = _true_boxes(size, anchors)
    frame = _frame(size, true_boxes, meta["scale"], skip)
    res = A.calibrate_image(frame, size=size, cfg=cfg, workflow=workflow)
    A.record(cfg, res, save=True, workflow=workflow)
    again = cfgmod.Config.load()
    out = {"status": res.get("status"), "gate": res.get("gate"),
           "msg": res.get("message"), "source": meta["source"],
           "deltas": {}}
    for name in ("buy", "confirm"):
        if name in skip:
            continue
        got = _click_center(again, name, size)
        tx = true_boxes[name][0] + true_boxes[name][2] / 2.0
        ty = true_boxes[name][1] + true_boxes[name][3] / 2.0
        out["deltas"][name] = (got[0] - tx, got[1] - ty) if got else None
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--anchors", default="",
                    help="真机锚点表 JSON（覆盖默认的等比居中模型）；"
                         "不给则全部按模型算，结果里会标 source=model")
    args = ap.parse_args(argv)

    anchors, bad = ({}, [])
    if args.anchors:
        anchors, bad = TM.load_anchors(args.anchors)
        for msg in bad:
            print("锚点表：" + msg)
        if not anchors:
            print("锚点表里没有可用条目，退回默认模型（source=model）。")

    # 快照/回滚目录指到临时目录，别污染真实用户数据。
    os.environ["DAFORS_USER_DIR"] = tempfile.mkdtemp(prefix="clicktruth_user_")

    print("引擎实际点击坐标 vs 真实位置模型（单位 px）")
    print("前提：真实位置 = UI 等比居中模型（s = min(宽/1920, 高/1080)），"
          "不是真机实测。\n")
    print("| 客户区 | 位置来源 | 状态 | 闸门 | 购买按钮偏差 | 确认按钮偏差 |")
    print("|---|---|---|---|---|---|")
    worst = 0.0
    blocked = 0
    for label, size in CASES:
        r = _run(size, anchors=anchors or None)
        if r["gate"]:
            blocked += 1
            cells = "拦住（不点）"
            print(f"| {label} | {r['source']} | {r['status']} | 拦 "
                  f"| {cells} | {cells} |")
            continue
        cells = []
        for name in ("buy", "confirm"):
            d = r["deltas"].get(name)
            if d is None:
                cells.append("—")
                continue
            cells.append(f"{d[0]:+.1f} / {d[1]:+.1f}")
            worst = max(worst, abs(d[0]), abs(d[1]))
        print(f"| {label} | {r['source']} | {r['status']} | 放行 "
              f"| {cells[0]} | {cells[1]} |")

    print(f"\n最大偏差 {worst:.1f}px（相对上述模型，不是真机实测）；"
          f"拦住 {blocked} 格。")
    if not anchors:
        print("本次全部使用默认模型（source=model）。模型是否成立未验证——"
              "真机阶段用 --anchors 注入标注表，或跑 "
              "tools/verify_truth_vs_annotations.py。")

    # P0-D：倒计时模式没有 buy 模板也能通过（不要求 buy 锚点）。
    r = _run((1280, 800), skip=("buy",), workflow="countdown", anchors=anchors or None)
    print("\n倒计时模式无 buy 模板（1280×800）：status=%s gate=%s"
          % (r["status"], "拦" if r["gate"] else "放行"))
    print("  " + (r["msg"] or ""))

    # P0-B：一个锚点都没有 → skipped 且必拦，绝不静默用内置坐标点。
    r = _run((3440, 1440), skip=("buy", "confirm"), anchors=anchors or None)
    print("\n无任何锚点（3440×1440）：status=%s gate=%s"
          % (r["status"], "拦" if r["gate"] else "放行"))

    # P0-A：21:9 穷举兜底命中真实比例 1.3333。
    r = _run((3440, 1440), anchors=anchors or None)
    print("21:9 兜底：status=%s 购买偏差=%s"
          % (r["status"],
             "拦住" if r["gate"] else
             "%+.1f/%+.1f" % r["deltas"]["buy"]))


if __name__ == "__main__":
    main()
