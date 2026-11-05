"""筛选面板的「先读后点」：状态一致就一个框都不点。

勾选框是记忆的（上次勾了什么这次还勾着）。以前每次都把 14 个框挨个点一遍，
实测一轮定位光在筛选面板上就耗掉十几秒，而且多数点击是无效的（本来就是
勾着的，点一下等于取消再勾回来）。

现在的做法：一次截图读出全部 14 个框的状态，和目标的差集才是要点的。
这里钉的就是这个差集 —— 尤其是「完全一致时零点击」。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nav import Navigator  # noqa: E402
from core.nav_coords import (COL_GUN_X, COL_GUN_X2, COL_GUN_Y, COL_NOT_OWNED,
                             COL_OWNED, COL_RARITY_X, COL_RARITY_Y)  # noqa: E402

W, H = 1920, 1080


class _Screen:
    """按「哪些框是勾着的」画出来的假屏幕。"""

    def __init__(self, on_boxes=()):
        self.size = (W, H)
        self.on = set(on_boxes)
        self.grabs = 0

    def grab(self, box=None):
        self.grabs += 1
        img = np.zeros((H, W, 3), np.uint8)
        for x, y in self.on:
            img[y - 8:y + 8, x - 8:x + 8] = 255      # 亮块 = 勾上了
        return img


class _Mouse:
    """点一下就翻转那个框的状态 —— 真游戏就是这样，不翻的话校验会报错。"""

    def __init__(self, screen=None):
        self.clicks = []
        self._scr = screen

    def click(self, x, y, **kw):
        p = (int(x), int(y))
        self.clicks.append(p)
        if self._scr is not None:
            if p in self._scr.on:
                self._scr.on.discard(p)
            else:
                self._scr.on.add(p)
        return True


class _Keyboard:
    def wheel(self, *a, **k):
        return True

    def tap(self, *a, **k):
        return True


def _pts():
    """面板上 14 个框的坐标（和 nav_coords 一致）。"""
    out = {}
    for i, x in enumerate(COL_RARITY_X):
        out[f"tier{i}"] = (x, COL_RARITY_Y)
    for i, x in enumerate(COL_GUN_X):
        out[f"gun{i}"] = (x, COL_GUN_Y[0])
    for i, x in enumerate(COL_GUN_X2):
        out[f"gun2_{i}"] = (x, COL_GUN_Y[1])
    out["owned"] = COL_OWNED
    out["not_owned"] = COL_NOT_OWNED
    return out


def _nav(on_boxes=()):
    scr = _Screen(on_boxes)
    m = _Mouse(scr)
    nav = Navigator(scr, m, _Keyboard(), snap=scr.grab)
    return nav, scr, m


def test_reads_all_fourteen_boxes_from_one_screenshot():
    """一次截图读出 4 品阶 + 7 枪种 + 已拥有/未拥有。"""
    p = _pts()
    on = [p["tier0"], p["gun0"], p["gun2_0"], p["owned"]]
    nav, scr, _m = _nav(on)
    st = nav.col_filter_states()
    assert st["tier"] == [True, False, False, False]
    assert st["guns"]["步枪"] is True
    assert st["guns"]["精准射手步枪"] is True
    assert st["guns"]["手枪"] is False
    assert st["owned"] is True and st["not_owned"] is False
    assert scr.grabs == 1, f"应该只截一次，实际 {scr.grabs} 次"


def test_clicks_nothing_when_everything_already_matches():
    """14 个框全勾着，目标也是全勾 → 一次点击都不发。

    这就是「重合就不动」。以前这里会发 14 次点击。
    """
    p = _pts()
    nav, scr, m = _nav(list(p.values()))
    before = scr.grabs
    nav.col_apply(tier=[True] * 4,
                  guns={n: True for n in nav.GUN_ROWS},
                  owned=True, not_owned=True)
    assert m.clicks == [], f"不该点：{m.clicks}"
    assert scr.grabs - before <= 1, "状态一致时连校验截图都不该多拍"


def test_clicks_only_the_boxes_that_differ():
    """只勾了传说和步枪，目标是全勾 → 只点缺的那几个。"""
    p = _pts()
    nav, _scr, m = _nav([p["tier0"], p["gun0"], p["owned"], p["not_owned"]])
    nav.col_apply(tier=[True] * 4,
                  guns={n: True for n in nav.GUN_ROWS},
                  owned=True, not_owned=True)
    assert len(m.clicks) == 3 + 6, f"应该只点 3 个品阶 + 6 个枪种：{m.clicks}"
    assert p["tier1"] in m.clicks and p["tier0"] not in m.clicks
    assert p["gun1"] in m.clicks and p["gun0"] not in m.clicks


def test_set_gun_only_turns_off_the_other_six():
    """7 个枪种都勾着，只要步枪 → 只点另外 6 个。"""
    p = _pts()
    nav, _scr, m = _nav(list(p.values()))
    nav.set_gun("步枪")
    assert len(m.clicks) == 6, f"应该只点 6 个：{m.clicks}"
    assert p["gun0"] not in m.clicks, "步枪本来该留着，不该点它"


def test_set_tier_only_turns_off_the_other_three():
    p = _pts()
    nav, _scr, m = _nav(list(p.values()))
    nav.set_tier(3, only=True)          # 只要「普通」
    assert len(m.clicks) == 3, f"应该只点 3 个：{m.clicks}"
    assert p["tier3"] not in m.clicks


def test_col_apply_actually_clicks_the_boxes():
    """col_apply 会真的点筛选框（不再有「只说不点」的旁路）。"""
    nav, _scr, m = _nav([])
    nav.col_apply(tier=[True] * 4, guns={n: True for n in nav.GUN_ROWS})
    assert len(m.clicks) >= 4, f"应该逐项点击：{m.clicks}"
