"""真机标注比对：模型预测 vs 人工标注 vs 引擎点击。

用途：`tools/verify_click_truth.py` 的「真实位置」是自建模型。本工具拿
**真实游戏截图 + 人工标注的按钮像素框**当真值，分别量：

  A. 模型预测中心  vs 标注中心   → 检验「等比居中」假设本身
  B. 引擎点击中心  vs 标注中心   → 检验标定链路在真机上的最终误差

用法：

    python tools/verify_truth_vs_annotations.py --manifest truth.json
    python tools/verify_truth_vs_annotations.py --manifest truth.json \
        --anchors measured_anchors.json

manifest（`box` 必须是人工在截图上量的真实像素框，不是本工具算的）：

    {
      "defaults": {"workflow": "list"},
      "items": [
        {"image": "shots/3440x1440.png",
         "button": "buy",
         "box": [1482, 903, 298, 58],
         "client": [3440, 1440],
         "workflow": "countdown"}
      ]
    }

`client` 可省，默认取截图尺寸；多显示器/带黑边截图时请显式给客户区。
没有 manifest 或标注缺失时只打印 schema 并以 2 退出 —— 不会用构造数据
冒充「已验过」。
"""
import argparse
import json
import os
import sys
import tempfile

import cv2

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import autocalib as A          # noqa: E402
from core import config as cfgmod        # noqa: E402
from core import region_table as RT      # noqa: E402
from core import truth_model as TM       # noqa: E402

SCHEMA = """{
  "defaults": {"workflow": "list"},
  "items": [
    {"image": "shots/3440x1440.png",
     "button": "buy",
     "box": [x, y, w, h],
     "client": [3440, 1440],
     "workflow": "countdown"}
  ]
}"""


def _center(box):
    return (box[0] + box[2] / 2.0, box[1] + box[3] / 2.0)


def _fmt(pt, truth):
    if pt is None:
        return "—"
    dx, dy = pt[0] - truth[0], pt[1] - truth[1]
    dist = (dx * dx + dy * dy) ** 0.5
    return f"{dx:+.1f} / {dy:+.1f}（{dist:.1f}）"


def _valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) for v in box)
            and box[2] > 0 and box[3] > 0)


def _engine_center(image_path, client, button, workflow):
    """跑完整链路，返回（引擎会点的中心, 状态, 闸门原因）。"""
    path = os.path.join(tempfile.mkdtemp(prefix="truth_ann_"), "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"queue": []}, fh)
    cfgmod.CONFIG_PATH = path
    cfg = cfgmod.Config.load()
    img = cv2.imread(image_path)
    if img is None:
        return None, "读不到截图", ""
    res = A.calibrate_image(img, size=client, cfg=cfg, workflow=workflow)
    A.record(cfg, res, save=True, workflow=workflow)
    if res.get("gate"):
        return None, res.get("status") or "拦住", res.get("gate")
    box = cfgmod.Config.load().region(button)
    if not box:
        return None, "配置里没有该区域", ""
    return _center((box["x"] * client[0], box["y"] * client[1],
                    box["w"] * client[0], box["h"] * client[1])), \
        res.get("status") or "", ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default="",
                    help="真机标注清单 JSON；不给我只打印 schema 并退出")
    ap.add_argument("--anchors", default="",
                    help="真机锚点表 JSON（替换默认等比居中模型）")
    args = ap.parse_args(argv)

    if not args.manifest:
        print("没有 --manifest，未执行任何比对。\n清单格式：\n" + SCHEMA)
        return 2
    try:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"清单读不出来：{e}\n格式：\n{SCHEMA}")
        return 2

    items = manifest.get("items") if isinstance(manifest, dict) else None
    if not isinstance(items, list) or not items:
        print("清单里没有 items 数组，未执行比对。")
        return 2
    defaults = (manifest.get("defaults") or {}) if isinstance(manifest, dict) else {}
    base_dir = os.path.dirname(os.path.abspath(args.manifest))

    anchors, bad = ({}, [])
    if args.anchors:
        anchors, bad = TM.load_anchors(args.anchors)
        for msg in bad:
            print("锚点表：" + msg)

    os.environ["DAFORS_USER_DIR"] = tempfile.mkdtemp(prefix="truth_ann_user_")

    print("真机标注比对（单位 px；Δ 列格式 dx/dy（欧氏距离））")
    print("模型来源：" + ("实测锚点表 " + os.path.basename(args.anchors)
                        if anchors else "默认等比居中模型（未实测）"))
    print()
    print("| 截图 | 按钮 | 标注中心 | 模型预测 | Δ模型 | 引擎点击 | Δ引擎 | 状态 |")
    print("|---|---|---|---|---|---|---|---|")

    worst_model = worst_engine = 0.0
    n_ok = n_blocked = n_bad = 0
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            print(f"| #{i} | — | — | — | — | — | — | 条目不是对象 |")
            n_bad += 1
            continue
        img_rel = str(item.get("image") or "")
        img_path = img_rel if os.path.isabs(img_rel) else os.path.join(base_dir, img_rel)
        button = str(item.get("button") or "")
        box = item.get("box")
        if not img_rel or not _valid_box(box) or button not in RT.BUILTIN:
            print(f"| {os.path.basename(img_rel) or '#' + str(i)} | "
                  f"{button or '—'} | — | — | — | — | — | "
                  f"标注/按钮缺失或非法，跳过 |")
            n_bad += 1
            continue
        img = cv2.imread(img_path)
        if img is None:
            print(f"| {os.path.basename(img_rel)} | {button} | — | — | — | — | — "
                  f"| 截图读不到 |")
            n_bad += 1
            continue
        client = item.get("client") or [img.shape[1], img.shape[0]]
        if (not isinstance(client, (list, tuple)) or len(client) != 2
                or client[0] <= 0 or client[1] <= 0):
            print(f"| {os.path.basename(img_rel)} | {button} | — | — | — | — | — "
                  f"| client 非法 |")
            n_bad += 1
            continue
        client = (int(client[0]), int(client[1]))
        workflow = item.get("workflow") or defaults.get("workflow") or None

        truth = _center(box)
        pred_boxes, _meta = TM.truth_boxes(client, RT.BUILTIN_PX, anchors or None)
        pred = _center(pred_boxes[button])
        d_model = ((pred[0] - truth[0]) ** 2 + (pred[1] - truth[1]) ** 2) ** 0.5
        worst_model = max(worst_model, d_model)

        click, status, gate = _engine_center(img_path, client, button, workflow)
        if click is None:
            n_blocked += 1
            d_engine_cell = "—"
        else:
            d_engine = ((click[0] - truth[0]) ** 2
                        + (click[1] - truth[1]) ** 2) ** 0.5
            worst_engine = max(worst_engine, d_engine)
            d_engine_cell = _fmt(click, truth)
            n_ok += 1
        print(f"| {os.path.basename(img_rel)} | {button} "
              f"| {truth[0]:.1f}, {truth[1]:.1f} "
              f"| {pred[0]:.1f}, {pred[1]:.1f} | {_fmt(pred, truth)} "
              f"| {('—' if click is None else '%.1f, %.1f' % click)} "
              f"| {d_engine_cell} | {status}{('：' + gate) if gate else ''} |")

    print(f"\n共 {len(items)} 条：引擎放行 {n_ok}、拦住 {n_blocked}、数据问题 {n_bad}。")
    print(f"模型预测最大偏差 {worst_model:.1f}px（检验假设本身）；"
          f"引擎点击最大偏差 {worst_engine:.1f}px（相对人工标注）。")
    if n_ok == 0:
        print("没有一条走到「引擎点击 vs 标注」——本次不能说明标定链路在真机上的误差。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
