"""关注星标：靠颜色判断，不靠 OCR。

交易行右侧详情面板顶部有个星标（标题右边、放大镜左边）。点一下从灰白变
**金色**，再点回灰白。这个差别用 RGB 一眼能分，OCR 认不出一个小星星 ——
所以判据是「R 比 B 高多少」。

坐标 (1754, 239) 是 2026-11-02 真机量的：连点两次，第一次 diff=19
（灰→金），确认它确实是关注按钮。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nav import Navigator  # noqa: E402
from core.nav_coords import COL_FOLLOW  # noqa: E402

W, H = 1920, 1080


GOLD = (95, 205, 230)      # BGR：金色的星
GRAY = (200, 200, 200)     # BGR：灰白的星（未关注）


class _Screen:
    def __init__(self, color):
        self.size = (W, H)
        self.color = color          # BGR

    def grab(self, box=None):
        img = np.zeros((H, W, 3), np.uint8)
        x, y = COL_FOLLOW
        img[y - 8:y + 8, x - 8:x + 8] = self.color
        return img


class _Mouse:
    def __init__(self, screen):
        self.clicks = []
        self._scr = screen

    def click(self, x, y, **kw):
        self.clicks.append((int(x), int(y)))
        # 点一下就把颜色翻过来 —— 真游戏就是这样
        self._scr.color = GOLD if self._scr.color == GRAY else GRAY
        return True


def _nav(color):
    scr = _Screen(color)
    return Navigator(scr, _Mouse(scr), None, snap=scr.grab), scr


def test_gold_star_means_followed():
    nav, _ = _nav(GOLD)
    assert nav.follow_state() is True


def test_gray_star_means_not_followed():
    nav, _ = _nav(GRAY)
    assert nav.follow_state() is False


def test_set_follow_clicks_once_when_state_differs():
    nav, _scr = _nav(GRAY)
    assert nav.set_follow(True) is True
    assert len(nav.mouse.clicks) == 1
    assert nav.mouse.clicks[0] == COL_FOLLOW


def test_set_follow_skips_when_already_in_that_state():
    """已经关注了就别再点 —— 再点一次等于取消关注。"""
    nav, _scr = _nav(GOLD)
    assert nav.set_follow(True) is True
    assert nav.mouse.clicks == [], f"不该点：{nav.mouse.clicks}"


def test_set_follow_on_gray_still_follows():
    nav, _scr = _nav(GRAY)
    assert nav.set_follow(True) is True
    assert nav.mouse.clicks, "灰色星标也要点一下才能关注"
