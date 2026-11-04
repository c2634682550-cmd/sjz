"""购买结果弹窗识别。

录屏实测（`docs/auto-buy-design.md` §4.6）：三角币不够时游戏弹出

    您当前的三角币不足，是否前往充值？
    （缺少 N 三角币，推荐充值 ¥ 328 档位）
    [取消]              [充值]

**点「充值」会花真钱，不可逆。** 所以这个模块只做两件事：认出这个弹窗、
给出「取消」的坐标。它不返回任何指向「充值」的点击点 —— 调用方想点充值
得自己从 `buttons` 里取，绕过这个模块的设计意图，那样代码评审时一眼能看见。

识别不靠模板匹配：弹窗底色和确认弹窗接近，模板很容易互相命中；文字则是
独一无二的。代价是一次 OCR（弹窗区域约 50ms，见 tests 里的耗时断言）。
"""
from . import ocr

# 「三角币不足」是这句正文的要害。游戏里还有别的弹窗（确认购买、已售出），
# 它们都不含这四个字，所以用它做判据不会误伤。
_INSUFFICIENT = "三角币不足"
# 老版本/其它语言包可能的说法，一并认
_INSUFFICIENT_ALT = "余额不足"


def _chars(words):
    """把词拆成单字，每个字带自己的包围盒。

    Windows OCR 对中文基本是一字一词，但不保证 —— 遇到「取消充值」被当成
    一个词时，按字数均分宽度，仍然能定位到「取」和「充」各自的位置。
    """
    out = []
    for w in words:
        t = w.get("text") or ""
        n = len(t)
        if not n:
            continue
        step = w["w"] / float(n)
        for i, ch in enumerate(t):
            out.append({
                "ch": ch,
                "x": int(w["x"] + i * step),
                "y": w["y"],
                "w": max(1, int(round(step))),
                "h": w["h"],
            })
    return out


def _box(chars):
    x0 = min(c["x"] for c in chars)
    y0 = min(c["y"] for c in chars)
    x1 = max(c["x"] + c["w"] for c in chars)
    y1 = max(c["y"] + c["h"] for c in chars)
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "cx": (x0 + x1) // 2, "cy": (y0 + y1) // 2}


def _find_run(chars, word):
    """在按 x 排序的字序列里找连续出现的 `word`，返回这些字。"""
    seq = sorted(chars, key=lambda c: c["x"])
    n = len(word)
    for i in range(len(seq) - n + 1):
        if "".join(c["ch"] for c in seq[i:i + n]) == word:
            return seq[i:i + n]
    return None


def _buttons(rows):
    """找按钮行里的「取消」「充值」两个按钮。

    按钮行 = 出现「取消」的那一行。正文里也有「充值」二字（「是否前往充值？」），
    但正文没有「取消」，所以这个判据不会把正文误当成按钮行。
    """
    for row in rows:
        chars = _chars(row["words"])
        cancel = _find_run(chars, "取消")
        if not cancel:
            continue
        recharge = _find_run(chars, "充值")
        return {
            "cancel": _box(cancel),
            "recharge": _box(recharge) if recharge else None,
            "row_text": row["text"],
        }
    return None


def _shortfall(rows):
    """读「缺少 N 三角币」里的 N。

    OCR 对这段小字很不稳：实测录屏里 `2,001` 被读成 `2 ， 〔 〕 1 〔 〕`，
    千分位逗号和数字混在一起。**读不准就返回 None，不猜** —— 猜错的数字会
    让人以为余额够，进而反复重试。
    """
    for row in rows:
        t = "".join(row["text"].split())
        if "缺少" not in t:
            continue
        head = t.split("缺少", 1)[1].split("三角币", 1)[0]
        digits = "".join(ch for ch in head if ch.isdigit())
        # 只接受干净的 3~8 位纯数字；带括号/逗号残留的一律当没读到
        if 3 <= len(digits) <= 8 and not any(ch in head for ch in "〔〕[]()（）"):
            return int(digits)
        # 这一行读残了就看下一行。以前这里直接 return None，第一条噪声行
        # 会把后面干净的那条也一起废掉 —— 弹窗里「缺少 2,001 三角币」
        # 常常被拆成两三行，第一行全是括号。
        continue
    return None


def _recommend(rows):
    """读「推荐充值 ¥ 328 档位」里的 328。"""
    for row in rows:
        t = "".join(row["text"].split())
        if "推荐充值" not in t:
            continue
        tail = t.split("推荐充值", 1)[1]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        if digits:
            return int(digits)
    return None


def detect(words):
    """从 OCR 词表里判断当前是不是「余额不足」弹窗。

    返回 {found, kind, shortfall, recommend, buttons, text}。
    没认出时 found=False，其余字段为 None/空。
    """
    empty = {"found": False, "kind": None, "shortfall": None,
             "recommend": None, "buttons": None, "text": ""}
    if not words:
        return empty
    rows = ocr.rows(words)
    text = "\n".join(r["text"] for r in rows)
    squeezed = "".join(text.split())
    if _INSUFFICIENT not in squeezed and _INSUFFICIENT_ALT not in squeezed:
        return empty
    buttons = _buttons(rows)
    return {
        "found": True,
        "kind": "insufficient",
        "shortfall": _shortfall(rows),
        "recommend": _recommend(rows),
        "buttons": buttons,
        "text": text,
    }


def scan(img):
    """对一张截图（BGR ndarray）做识别。返回同 detect()。"""
    if img is None or getattr(img, "size", 0) == 0:
        return detect(None)
    return detect(ocr.read_words(img))


def cancel_point(result):
    """唯一对外暴露的点击点：弹窗里的「取消」。

    没认出弹窗、或没定位到「取消」时返回 None —— 调用方据此停手，
    而不是退回某个默认坐标去点。
    """
    if not result or not result.get("found"):
        return None
    btn = (result.get("buttons") or {}).get("cancel")
    if not btn:
        return None
    return btn["cx"], btn["cy"]


def describe(result):
    """一句中文结论，给日志直接用。"""
    if not result or not result.get("found"):
        return ""
    bits = ["三角币不足"]
    if result.get("shortfall") is not None:
        bits.append(f"缺 {result['shortfall']}")
    if result.get("recommend") is not None:
        bits.append(f"推荐充值 ¥{result['recommend']} 档")
    if cancel_point(result):
        bits.append("已定位「取消」")
    else:
        bits.append("没定位到「取消」")
    return "，".join(bits)
