"""区域标定：内置坐标、校验、像素换算。

坐标默认值来自 `core/region_table.py`（1080p 客户区实测表），这里不重复
写数字。配置里存的是**归一化**坐标（0..1，相对游戏客户区），引擎侧唯一的
换算入口是 `core/screen.Screen.norm_to_px()`；`pixel_rect()` 直接借用同一
个方法（传一个只有 `size` 的轻量对象），不复制四舍五入逻辑。

为什么单独一个模块：
  · GET /api/regions 要一次说清「现在是什么值、是不是用户改过、对应客户区
    上的哪块像素」；
  · PUT /api/regions 的校验必须与 HTTP 层解耦，才能被单元测试直接调用；
  · 不合法请求一律不落盘（校验全过之后才进 Config.transaction）。

区域名只认 `region_table.ORDER` 里的 9 个键 —— 瞎写的名字下游不认，存进去
只会让用户以为「设了但没生效」。
"""
import math

from .region_table import BUILTIN, LABELS, ORDER
from .screen import Screen

# 最小宽/高（归一化）。1920 宽下 1% ≈ 19px，再小就不是「一个按钮/一行字」，
# 而是拖出来的一条线 —— 引擎抓到的 ROI 几乎没有信息量。
MIN_W = 0.01
MIN_H = 0.01

# name, 中文名, 内置默认（归一化）, 是否可在高级设置里微调, 说明
#
# 9 个区域都参与自动标定的锚点校验：buy/confirm 走模板匹配，clock/price/
# name 走 OCR，list/list_item/bar/toast 做几何与内容校验。所以都可编辑，
# 不存在「框了也不生效」的假按钮。
SPECS = tuple((name, LABELS[name], dict(BUILTIN[name]), True, "")
              for name in ORDER)

_SPEC_BY_NAME = {s[0]: s for s in SPECS}
EDITABLE = tuple(ORDER)
# 历史版本用过的键。现在不读也不写，PUT 里出现要明确告诉用户「已废弃」，
# 而不是笼统报「未知区域」。
_LEGACY = ("list_refresh",)


class _SizeOnly:
    """只带 `size` 的替身：让 Screen.norm_to_px 在无屏幕环境也能算像素。"""

    __slots__ = ("size",)

    def __init__(self, size):
        self.size = (int(size[0]), int(size[1]))


def pixel_rect(norm, size):
    """归一化框 → 物理像素矩形 `(x, y, w, h)`，唯一实现仍是 Screen.norm_to_px。"""
    x, y, w, h = Screen.norm_to_px(_SizeOnly(size), dict(norm))
    return {"x": x, "y": y, "w": w, "h": h}


def px_box(norm, size):
    """同 `pixel_rect`，但返回 `(x, y, w, h)` 元组，给抓帧/校验用。"""
    r = pixel_rect(norm, size)
    return r["x"], r["y"], r["w"], r["h"]


def _num(value):
    """只接受真的数字：bool 是 int 的子类，必须单独挡掉。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _check_box(name, raw):
    """校验单个区域。返回 `(norm|None, [错误文案])`。

    norm 统一成 `{"x","y","w","h"}`（x/y 左上角，w/h 宽高）。调用方传
    `x1/y1/x2/y2` 也收，先换算成同一形状再校验。
    """
    spec = _SPEC_BY_NAME[name]
    label = spec[1]
    if not isinstance(raw, dict):
        return None, [f"「{label}」的坐标必须是对象（x/y/w/h 或 x1/y1/x2/y2）"]

    if all(k in raw for k in ("x1", "y1", "x2", "y2")):
        vals = {k: _num(raw.get(k)) for k in ("x1", "y1", "x2", "y2")}
        bad = [k for k, v in vals.items() if v is None]
        if bad:
            return None, [f"「{label}」的 {', '.join(bad)} 必须是有限数字（0~1）"]
        if vals["x1"] >= vals["x2"]:
            return None, [f"「{label}」左右颠倒：x1 必须小于 x2"]
        if vals["y1"] >= vals["y2"]:
            return None, [f"「{label}」上下颠倒：y1 必须小于 y2"]
        box = {"x": vals["x1"], "y": vals["y1"],
               "w": vals["x2"] - vals["x1"], "h": vals["y2"] - vals["y1"]}
    else:
        vals = {k: _num(raw.get(k)) for k in ("x", "y", "w", "h")}
        bad = [k for k, v in vals.items() if v is None]
        if bad:
            return None, [f"「{label}」的 {', '.join(bad)} 必须是有限数字（0~1 的比例）"]
        box = vals

    errors = []
    if box["x"] < 0 or box["y"] < 0:
        errors.append(f"「{label}」左上角不能在画面外（x={box['x']:.4f}, "
                      f"y={box['y']:.4f}）")
    if box["w"] > 1 or box["h"] > 1:
        errors.append(f"「{label}」宽高不能超过整幅画面（w={box['w']:.4f}, "
                      f"h={box['h']:.4f}）")
    if box["x"] + box["w"] > 1 + 1e-9:
        errors.append(f"「{label}」右边越界：x + w = "
                      f"{box['x'] + box['w']:.4f} > 1")
    if box["y"] + box["h"] > 1 + 1e-9:
        errors.append(f"「{label}」下边越界：y + h = "
                      f"{box['y'] + box['h']:.4f} > 1")
    if box["w"] < MIN_W:
        errors.append(f"「{label}」太窄（w={box['w']:.4f}，至少 {MIN_W}）—— "
                      f"拖成一条线会让识别拿到空图")
    if box["h"] < MIN_H:
        errors.append(f"「{label}」太矮（h={box['h']:.4f}，至少 {MIN_H}）—— "
                      f"拖成一条线会让识别拿到空图")
    if errors:
        return None, errors
    return {k: round(box[k], 6) for k in ("x", "y", "w", "h")}, []


def validate_update(payload):
    """校验 PUT 请求体。返回 `(clean|None, [错误])`。

    clean 的键只可能是 `EDITABLE`；值 None 表示「恢复该项内置值」，这里
    直接换成内置坐标（盘上不写 null，下一次启动就是确定的值）。
    任何一条错误都返回 None —— 调用方不得部分保存。
    """
    if not isinstance(payload, dict):
        return None, ["请求体必须是 JSON 对象"]
    raw = payload.get("regions")
    if not isinstance(raw, dict) or not raw:
        return None, ["请求体里没有 regions（要保存的区域），什么都没改"]
    clean, errors = {}, []
    for name in raw:
        if name in _LEGACY:
            errors.append(f"区域「{name}」的旧键已废弃，请改用 9 个内置区域名："
                          f"{', '.join(EDITABLE)}")
        elif name not in _SPEC_BY_NAME:
            errors.append(f"未知区域「{name}」，可选：{', '.join(EDITABLE)}")
    for name, value in raw.items():
        if name not in _SPEC_BY_NAME:
            continue
        if value is None:
            clean[name] = dict(BUILTIN[name])       # 恢复内置默认
            continue
        box, errs = _check_box(name, value)
        if errs:
            errors.extend(errs)
        else:
            clean[name] = box
    if errors:
        return None, errors
    return clean, []


def _usable_box(box):
    """盘上值可能是手改坏的字典（缺键/字符串/NaN）；这种不当成用户设定。"""
    if not isinstance(box, dict):
        return None
    vals = {}
    for k in ("x", "y", "w", "h"):
        v = _num(box.get(k))
        if v is None:
            return None
        vals[k] = v
    if vals["w"] <= 0 or vals["h"] <= 0:
        return None
    return vals


def same_box(a, b, tol=1e-6):
    """两个归一化框是否等值（用来判断「用户改过没有」）。"""
    if not a or not b:
        return False
    return all(abs(float(a[k]) - float(b[k])) <= tol for k in ("x", "y", "w", "h"))


def is_manual(cfg, name, auto_applied=None):
    """这个区域是不是用户手动框过（自动标定不得覆盖）。

    `auto_applied` 是上一次自动写入的值：盘上和它一致说明是程序写的，
    仍可再次自动更新；既不是内置值、也不是上次自动值，才算手动。
    """
    raw = cfg.region_raw(name) if hasattr(cfg, "region_raw") else None
    user = _usable_box(raw)
    if user is None:
        return False
    if same_box(user, BUILTIN.get(name)):
        return False
    if isinstance(auto_applied, dict) and same_box(user, auto_applied):
        return False
    return True


def describe(cfg, size, screen_meta=None, screen_error=None, calibration=None):
    """拼 GET / PUT 的返回体：每个区域的当前值、来源、实际像素矩形。"""
    regions = []
    cal = calibration if isinstance(calibration, dict) else {}
    applied = cal.get("applied") if isinstance(cal.get("applied"), dict) else {}
    for name, label, default, editable, note in SPECS:
        raw = cfg.region_raw(name) if hasattr(cfg, "region_raw") else cfg.region(name)
        user = _usable_box(raw)
        auto_box = applied.get(name) if isinstance(applied.get(name), dict) else None
        if user is None:
            source = "invalid" if raw is not None else "builtin"
            norm = dict(default)
        elif same_box(user, default):
            source = "builtin"
            norm = dict(default)
        elif auto_box and same_box(user, auto_box):
            source = "auto"
            norm = dict(user)
        else:
            source = "user"
            norm = dict(user)
        regions.append({
            "name": name,
            "label": label,
            "norm": norm,
            "builtin": dict(default),
            "px": pixel_rect(norm, size),
            "source": source,
            "manual": is_manual(cfg, name, auto_box),
            "editable": bool(editable),
            "note": note,
        })
    out = {
        "ok": True,
        "screen": {
            "width": int(size[0]),
            "height": int(size[1]),
            "available": screen_error is None,
            "error": screen_error,
            **(screen_meta or {}),
        },
        "min_size": {"w": MIN_W, "h": MIN_H},
        "editable": list(EDITABLE),
        "regions": regions,
    }
    if calibration is not None:
        out["calibration"] = calibration
    return out


def screen_meta(sc):
    """把 Screen 的描述性字段转成可 JSON 化的字典。"""
    scale = float(getattr(sc, "scale", 1.0) or 1.0)
    frame = getattr(sc, "frame", None) or {}
    full = getattr(sc, "client_rect", None) or {}
    return {
        "dpi": int(getattr(sc, "dpi", 96) or 96),
        "scale": round(scale, 3),
        "scale_percent": int(round(scale * 100)),
        "monitor": getattr(sc, "index", 1),
        "bounds": {k: int(v) for k, v in (getattr(sc, "bounds", {}) or {}).items()
                   if k in ("left", "top", "width", "height")},
        "client": {k: int(v) for k, v in frame.items()
                   if k in ("left", "top", "width", "height")},
        # 客户区原始矩形（即使有一部分在屏幕外也照实给出）与越界标记：
        # 前端据此提示「把窗口移回屏幕内」，不拿显示器矩形冒充客户区。
        "client_full": {k: int(v) for k, v in full.items()
                        if k in ("left", "top", "width", "height")},
        "client_partial": bool(getattr(sc, "client_partial", False)),
        "client_is_window": bool(frame),
        "game_rect": getattr(sc, "game_rect", None),
        "pick_reason": getattr(sc, "pick_reason", ""),
    }


def open_screen(cfg):
    """按配置打开抓屏对象。测试里 monkeypatch 这个函数即可注入假屏幕。"""
    monitor = cfg.get("monitor")
    if not isinstance(monitor, int) or isinstance(monitor, bool):
        monitor = 1
    return Screen(monitor)


def base_size(cfg):
    """拿不到屏幕时的兜底尺寸（不宣称是实际屏幕，只在返回值里标注）。"""
    scr = cfg.get("screen") if isinstance(cfg.get("screen"), dict) else {}
    base = scr.get("base_size") if isinstance(scr.get("base_size"), list) else None
    if base and len(base) == 2:
        try:
            w, h = int(base[0]), int(base[1])
            if w > 0 and h > 0:
                return w, h
        except (TypeError, ValueError):
            pass
    return 1920, 1080
