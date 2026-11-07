"""离线验证内置区域表：12 组预设逐区域偏差 + 1080p 逐位一致 + 真实截图基准。

不打开游戏、不抓屏、不动鼠标；只做纯计算，可直接进 CI：

    python tools/verify_autocalib.py
    python tools/verify_autocalib.py --json out.json

输出：
  1. 1080p / 100% 下内置表换回像素，与 `region_table.BUILTIN_PX` 逐位比对；
  2. 3 档逻辑分辨率 × 4 档系统缩放，每区域 `(x,y,w,h)` 与「归一化值 × 客户区」
     的偏差（像素，四舍五入误差，应 ≤1）；
  3. 非 16:9 客户区（4:3 信箱）按比例居中后的落点；
  4. `tests/fixtures/price_roi/manifest.json` 里 5 张真实截图的人工 ROI
     与内置区域的包含/中心偏差。
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import autocalib as A          # noqa: E402
from core import region_table as RT     # noqa: E402

KEYS = ("x", "y", "w", "h")


def _exact(norm, size):
    """不做四舍五入的期望像素值，用来量换算误差。"""
    return (norm["x"] * size[0], norm["y"] * size[1],
            norm["w"] * size[0], norm["h"] * size[1])


def check_1080p_exact():
    """1080p/100%：归一化表换回像素必须与手工框选结果逐位一致。"""
    size = (1920, 1080)
    rows = []
    for name in RT.ORDER:
        want = RT.BUILTIN_PX[name]
        got = A.boxes_for(size)[name]
        rows.append((name, want, got, list(want) == list(got)))
    return rows


def _dev(got, want):
    """偏差取 2 位；-0.0 归一成 0.0，避免表里出现 `-0`。"""
    out = []
    for i in range(4):
        v = round(got[i] - want[i], 2)
        out.append(0.0 if abs(v) < 0.005 else v)
    return out


def preset_table():
    """12 组预设：每区域四个字段的最大偏差（像素）。"""
    out = []
    for p in A.presets():
        size = (p["width"], p["height"])
        boxes = A.boxes_for(size)
        row = {"name": p["name"], "size": size, "regions": {}}
        for name in RT.ORDER:
            want = _exact(RT.BUILTIN[name], size)
            got = boxes[name]
            dev = _dev(got, want)
            row["regions"][name] = {
                "box": list(got),
                "dev": dev,
                "max_abs": round(max(abs(v) for v in dev), 2),
            }
        row["max_abs"] = max(r["max_abs"] for r in row["regions"].values())
        out.append(row)
    return out


def aspect_case(size=(1440, 1080)):
    """4:3 客户区：内置表按 16:9 等比缩放居中，返回关键区域落点。"""
    boxes = A.boxes_for(size)
    fixed, meta = A._aspect_fix(boxes, size)
    return {"size": size, "meta": meta,
            "boxes": {k: list(v) for k, v in fixed.items()}}


def real_shots():
    """5 张真实截图的人工 ROI 与内置区域的对应关系。"""
    path = os.path.join(BASE, "tests", "fixtures", "price_roi", "manifest.json")
    data = json.load(open(path, encoding="utf-8"))
    if "5 张真实截图" not in data.get("source", ""):
        raise SystemExit("ROI manifest 不是预期的 5 张真实截图")
    boxes = A.boxes_for((1920, 1080))
    rows = []
    for item in data["items"]:
        x, y, w, h = item["box"]
        cx, cy = x + w / 2.0, y + h / 2.0
        if item["cls"] == "currency":
            continue                    # 右上角余额，不在 9 个区域内
        if item["id"] == "u3_detail_price":
            target, kind = "bar", "contain"
        elif y < 210:
            target, kind = None, "canvas"   # 顶部筛选区间，不参与内置表
        elif x < 1000 and y < 500:
            target, kind = "price", "center"
        else:
            target, kind = "list", "contain"
        rec = {"id": item["id"], "roi": item["box"], "target": target, "kind": kind}
        if kind == "contain":
            rx, ry, rw, rh = boxes[target]
            rec["ok"] = rx <= x and ry <= y and x + w <= rx + rw and y + h <= ry + rh
            rec["target_box"] = [rx, ry, rw, rh]
        elif kind == "center":
            rx, ry, rw, rh = boxes[target]
            rec["dev"] = [round(cx - (rx + rw / 2.0), 2),
                          round(cy - (ry + rh / 2.0), 2)]
            rec["ok"] = all(abs(v) <= 3 for v in rec["dev"])
            rec["target_box"] = [rx, ry, rw, rh]
        else:
            rec["ok"] = 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080
        rows.append(rec)
    return rows


def render():
    lines = ["# 内置区域表离线验证", ""]
    lines += ["## 1. 1080p / 100% 逐位一致", "",
              "| 区域 | 手工值 (x,y,w,h) | 归一化换算 | 一致 |",
              "|---|---|---|---|"]
    exact_rows = check_1080p_exact()
    for name, want, got, same in exact_rows:
        lines.append(f"| `{name}` | `{tuple(want)}` | `{got}` | {'是' if same else '否'} |")
    lines += ["", f"逐位一致：{sum(1 for r in exact_rows if r[3])}/{len(exact_rows)}", ""]

    lines += ["## 2. 三档分辨率 × 四档缩放（坐标算术误差）", "",
              "> 本节只量「归一化值 × 客户区」的取整误差，**不经过模板匹配**。",
              "> 匹配几何误差（模板重采样后的中心偏差）见 "
              "`python tools/verify_calib_bias.py`，两者不要混为一谈。", "",
              "| 预设 | 客户区 | " + " | ".join(RT.ORDER) + " | 最大偏差 |",
              "|---|---|" + "---|" * len(RT.ORDER) + "---|"]
    table = preset_table()
    for row in table:
        cells = []
        for name in RT.ORDER:
            d = row["regions"][name]["dev"]
            cells.append("/".join(f"{v:+.0f}" if float(v).is_integer() else f"{v:+.2f}"
                                  for v in d))
        lines.append(f"| {row['name']} | {row['size'][0]}×{row['size'][1]} | "
                     + " | ".join(cells) + f" | {row['max_abs']:.2f} |")
    lines += ["", f"全部 {len(table)} 组最大偏差 "
                  f"{max(r['max_abs'] for r in table):.2f} px（四舍五入误差）", ""]

    ac = aspect_case()
    lines += ["## 3. 非 16:9（4:3 信箱）", "",
              f"客户区 {ac['size'][0]}×{ac['size'][1]}，"
              f"等比 {ac['meta'].get('scale')}，偏移 "
              f"({ac['meta'].get('dx')}, {ac['meta'].get('dy')})", "",
              "| 区域 | 落点 (x,y,w,h) |", "|---|---|"]
    for name in RT.ORDER:
        lines.append(f"| `{name}` | `{tuple(ac['boxes'][name])}` |")
    lines.append("")

    shots = real_shots()
    lines += ["## 4. 5 张真实截图 ROI 基准", "",
              "| ROI | 对应区域 | 判定 | 结果 |", "|---|---|---|---|"]
    for r in shots:
        detail = (f"偏差 {r['dev']} px" if "dev" in r else
                  (f"框 {r['target_box']}" if "target_box" in r else "画面内"))
        lines.append(f"| `{r['id']}` | `{r['target'] or '-'}` | "
                     f"{'通过' if r['ok'] else '失败'} | {detail} |")
    lines += ["", f"通过 {sum(1 for r in shots if r['ok'])}/{len(shots)}", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="验证内置区域表与自动标定预设")
    ap.add_argument("--json", metavar="PATH", help="另外把明细写成 JSON")
    args = ap.parse_args()
    print(render())
    if args.json:
        payload = {
            "exact_1080p": [{"name": n, "manual": list(w), "px": list(g),
                             "same": s} for n, w, g, s in check_1080p_exact()],
            "presets": preset_table(),
            "aspect": aspect_case(),
            "real_shots": real_shots(),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"\n明细已写入 {args.json}")


if __name__ == "__main__":
    main()
