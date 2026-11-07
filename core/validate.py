"""条件数值校验 —— 后端独立兜底，不信任前端。

前端 web/index.html 的 parseCondInputs 用同一套文案；两边都要拦，
因为 `1e309` 在浏览器里是 Infinity，JSON.stringify 之后变成 null，
引擎读到就会按 0 处理（= 不限价），这种「非法值静默变成默认值」
比直接报错危险得多。
"""

import math

# 稀有度（在售列表的「做工」）只认后端契约里的两档。前端下拉项来自
# switches 的 filter.craft.options，不再自己编「传说/史诗/稀有/普通」。
CRAFT_OPTIONS = ("", "极品", "优品")

LABELS = {
    "min_price": "最低价",
    "max_price": "最高价",
    "quota": "数量",
    "budget": "预算",
    "max_wear": "磨损上限",
}


def _number(values, key, default, errors):
    raw = values.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        errors.append(f"「{LABELS[key]}」必须是有限数字")
        return default
    try:
        n = float(raw)
    except (TypeError, ValueError, OverflowError):
        # 400 位整数（如 `10**400`）float() 直接 OverflowError：这是「超出
        # 可表示范围」，必须变成可读的 400，不能让接口抛 500（N9/E7）。
        errors.append(f"「{LABELS[key]}」必须是有限数字")
        return default
    if not math.isfinite(n):
        errors.append(f"「{LABELS[key]}」必须是有限数字")
        return default
    if n != int(n):
        errors.append(f"「{LABELS[key]}」必须是整数")
        return default
    if n < 0:
        errors.append(f"「{LABELS[key]}」不能为负数")
        return default
    return int(n)


def _real(values, key, default, errors):
    """磨损这类小数：只要是有限数且非负即可，不要求整数。"""
    raw = values.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        errors.append(f"「{LABELS[key]}」必须是有限数字")
        return default
    try:
        n = float(raw)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"「{LABELS[key]}」必须是有限数字")
        return default
    if not math.isfinite(n):
        errors.append(f"「{LABELS[key]}」必须是有限数字")
        return default
    if n < 0:
        errors.append(f"「{LABELS[key]}」不能为负数")
        return default
    return n


def check_conditions(values):
    """返回 `(clean, errors)`。

    clean 只在没有错误时有意义；调用方看到 errors 就应该整单拒绝，
    绝不能「错一项、其余照存」。
    """
    values = values or {}
    errors = []
    min_price = _number(values, "min_price", 0, errors)
    max_price = _number(values, "max_price", 0, errors)
    quota = _number(values, "quota", 1, errors)
    budget = _number(values, "budget", 0, errors)
    max_wear = _real(values, "max_wear", 0.0, errors)

    if quota < 1:
        errors.append("「数量」必须 ≥ 1")
    if max_price and min_price > max_price:
        errors.append("最低价不能高于最高价")

    craft = values.get("craft")
    if craft is not None and craft not in CRAFT_OPTIONS:
        errors.append("「稀有度」只支持 极品 / 优品 / 不限")

    clean = {"min_price": min_price, "max_price": max_price,
             "quota": quota, "budget": budget, "max_wear": max_wear}
    return clean, errors
