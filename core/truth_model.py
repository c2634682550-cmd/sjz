"""按钮真实位置的**可替换模型**（默认实现是假设，不是实测）。

⚠ 默认模型建立在一条未经验证的假设上：

    游戏 UI 以 1920×1080 为基准，客户区不是 16:9 时**等比缩放并居中**
    （黑边/信箱），缩放系数 s = min(宽/1920, 高/1080)。

如果游戏实际是锚定边角、按宽度独立拉伸、或 UI 有独立缩放档，这个模型
就不成立。工具在这种前提下量出来的「偏差 ≤0.5px」只是在说「引擎点击
和该模型的预测一致」，**不能**当成「相对真机按钮位置的偏差」。

要推翻假设，必须用真机标注表覆盖。表格式：

    {"anchors": [
       {"client": [3440, 1440], "scale": 1.3333,
        "dx": 0, "dy": 0, "source": "真机 2026-11-08 人工标注"}
    ]}

只做**客户区尺寸精确匹配**：没测过的尺寸继续走默认模型，并在返回的
meta 里标 `source="model"` —— 不插值、不猜。

对外入口：
    truth_scale(client_w, client_h, anchors=None) -> float
    truth_boxes(size, table=None, anchors=None) -> (boxes, meta)
"""
import json

from . import region_table as RT


def load_anchors(path):
    """读真机锚点表。返回 `({(w,h): anchor}, [问题列表])`。

    读不了/格式不对时返回空表与原因，调用方必须显式提示「没有实测数据」，
    不能静默退回模型还假装是实测。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return {}, [f"锚点表读不出来：{e}"]
    rows = data.get("anchors") if isinstance(data, dict) else data
    out, bad = {}, []
    if not isinstance(rows, list):
        return {}, ["锚点表必须是 {\"anchors\": [...]} 或数组"]
    for i, row in enumerate(rows):
        try:
            w, h = int(row["client"][0]), int(row["client"][1])
            s = float(row["scale"])
        except (KeyError, TypeError, ValueError, IndexError):
            bad.append(f"第 {i + 1} 条缺 client/scale 或不是数字，已跳过")
            continue
        if w <= 0 or h <= 0 or s <= 0:
            bad.append(f"第 {i + 1} 条 client/scale 非正数，已跳过")
            continue
        out[(w, h)] = {
            "client": (w, h), "scale": s,
            "dx": row.get("dx"), "dy": row.get("dy"),
            "source": str(row.get("source") or "measured"),
        }
    return out, bad


def _hit(client_w, client_h, anchors):
    if not anchors:
        return None
    key = (int(round(float(client_w))), int(round(float(client_h))))
    return anchors.get(key) if isinstance(anchors, dict) else None


def truth_scale(client_w, client_h, anchors=None):
    """客户区 → 按钮真实缩放系数。

    默认 `min(宽/1920, 高/1080)`（信箱模型）；给了该尺寸的实测锚点就用
    实测值。这是**唯一**需要替换的接口 —— 真机阶段只换锚点表，不改工具。
    """
    hit = _hit(client_w, client_h, anchors)
    if hit:
        return float(hit["scale"])
    return min(float(client_w) / RT.BASE_W, float(client_h) / RT.BASE_H)


def truth_boxes(size, table=None, anchors=None):
    """按模型/实测锚点算出 9 个区域的「真实像素框」，返回 `(boxes, meta)`。

    meta 里 `source` 明确写 `model` 还是实测来源，避免把模型输出当成
    真机真值（上一轮「误差归零是自证」的坑）。
    """
    w, h = float(size[0]), float(size[1])
    s = truth_scale(w, h, anchors)
    hit = _hit(w, h, anchors)
    dx = (float(hit["dx"]) if hit and hit.get("dx") is not None
          else (w - RT.BASE_W * s) / 2.0)
    dy = (float(hit["dy"]) if hit and hit.get("dy") is not None
          else (h - RT.BASE_H * s) / 2.0)
    src = table or RT.BUILTIN_PX
    boxes = {name: (x * s + dx, y * s + dy, bw * s, bh * s)
             for name, (x, y, bw, bh) in src.items()}
    return boxes, {
        "scale": s, "dx": dx, "dy": dy,
        "source": (hit["source"] if hit else "model"),
        "measured": bool(hit),
    }
