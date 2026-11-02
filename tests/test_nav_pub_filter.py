"""顶部公示期筛选：切到「只显示公示期」。

真机实测（2026-10-21）：
- 下拉里每个汉字都是**独立的词**，直接拿单字去比「只显示公示期」永远比不中，
  必须先按 y 聚行再拼整句。
- 只点不看会静默失败：下拉没打开时那一击落在列表上，筛选留在「不限公示期」，
  而详情面板照旧显示在售页的「剩余：X分Y秒」—— 归零商品下架，掐点白等。

所以 `pub_filter` 每一步都要 OCR 确认：先确认下拉开了，再按文字框点选项，
最后读按钮文字确认真的生效。假界面照抄「单字成词」和「点完不一定生效」。
"""
import time as _time

import numpy as np
import pytest

from core import nav_ons as nav_mod
from core.nav import Navigator

MENU_ROWS = ("不限公示期", "只显示公示期", "不显示公示期")
# 真机 OCR 实测：末尾那个「期」经常读成「恚」。所以匹配只能靠前两个字。
OCR_TYPO = {"不限公示期": "不限公示恚"}
# 原尺寸几何（1920x1080 下实测）：菜单 460x300，按钮 140x34，
# 行距 48px、字距 16px、字高 15px。假 OCR 按收到的图缩放回去给坐标。
MENU_BOX = (460, 250)       # (w, h)
BTN_BOX = (140, 34)
MENU_TOP = 235              # 菜单框在屏幕上的 y
ROW_STEP = 48
CHAR_STEP = 16
CHAR_H = 15
MENU_PAD = (60, 30)         # 菜单内第一行第一个字的偏移


class FakePubUi:
    """顶部按钮 + 三行下拉。文字全由假 OCR 给，像素只用来占位。"""

    def __init__(self, open_ok=True, honor_select=True):
        self.state = "不限公示期"
        self.open = False
        self.open_ok = open_ok
        self.honor_select = honor_select
        self.clicks = []
        self.said = []

    def snap(self):
        return np.zeros((1080, 1920, 3), np.uint8)

    def ocr_read(self, sub):
        """照抄真机两条行为：每个汉字一个词；14px 小字不放大就读不出。"""
        h, w = sub.shape[:2]
        if w >= MENU_BOX[0] * 2:            # 放大后的下拉（920 宽）
            if not self.open:
                return []
            scale, rows = w / MENU_BOX[0], MENU_ROWS
            ox, oy = MENU_PAD
        elif w >= BTN_BOX[0] * 2:           # 放大后的按钮（280 宽）
            scale, rows = w / BTN_BOX[0], (self.state,)
            ox, oy = 10, 5
        else:
            return []                       # 原尺寸：WinRT 整行跳过
        out = []
        for i, text in enumerate(rows):
            for j, ch in enumerate(OCR_TYPO.get(text, text)):
                out.append({"text": ch,
                            "x": (ox + j * CHAR_STEP) * scale,
                            "y": (oy + i * ROW_STEP) * scale,
                            "w": CHAR_H * scale, "h": CHAR_H * scale})
        return out

    def click(self, point, label=""):
        self.clicks.append((int(point[0]), int(point[1])))
        x, y = point
        if abs(x - 435) <= 40 and abs(y - 197) <= 20:
            self.open = self.open_ok
            return 1.0
        if self.open and 240 <= y <= 420 and 380 <= x <= 520:
            if self.honor_select:
                row = int(round((y - (MENU_TOP + MENU_PAD[1] + CHAR_H / 2))
                                / ROW_STEP))
                self.state = MENU_ROWS[row]
            self.open = False
            return 1.0
        return 0.0


class PubNav(Navigator):
    """只测筛选这一条：绕过真鼠标和像素校验，点击直接交给假界面。"""

    def __init__(self, ui):
        self.ui = ui
        self.dry_run = False
        self.size = (1920, 1080)
        self._snap = ui.snap
        self._ocr_read = ui.ocr_read
        self._log = lambda msg, level="info": ui.said.append(msg)

    def click(self, point, label="", expect_change=True, timeout=4.0):
        return self.ui.click(point, label)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """pub_filter 里有 0.9s / 1.4s 的等待，测试里没必要真等。"""

    class _T:
        sleep = staticmethod(lambda s: None)
        perf_counter = staticmethod(_time.perf_counter)

    monkeypatch.setattr(nav_mod, "time", _T)


def test_pub_filter_matches_despite_last_char_ocr_noise():
    """末尾「期」被读成「恚」也要能选中 —— 真机就是这么读的。"""
    ui = FakePubUi()
    assert PubNav(ui).pub_filter("不限公示期") is True
    assert ui.state == "不限公示期"


def test_pub_filter_selects_and_verifies():
    ui = FakePubUi()
    assert PubNav(ui).pub_filter("只显示公示期") is True
    assert ui.state == "只显示公示期"


def test_pub_filter_fails_when_menu_never_opens():
    """下拉没打开时不能假装成功 —— 否则筛选静默留在「不限公示期」。"""
    ui = FakePubUi(open_ok=False)
    assert PubNav(ui).pub_filter("只显示公示期") is False
    assert ui.state == "不限公示期"
    assert len(ui.clicks) == 6, "三次尝试，每次一击开下拉 + 一击收起"


def test_pub_filter_does_not_trust_the_click():
    """点下去了但按钮文字没变（界面没认），必须返回 False。"""
    ui = FakePubUi(honor_select=False)
    assert PubNav(ui).pub_filter("只显示公示期") is False
    assert ui.state == "不限公示期"


def test_crop_lines_joins_single_char_words():
    """下拉里每个字都是独立词，聚行后要拼成整句、坐标回填成整屏坐标。"""
    ui = FakePubUi()
    ui.open = True
    nav = PubNav(ui)
    rows = nav._crop_lines(nav.PUB_MENU_BOX)
    texts = [r["text"] for r in rows]
    assert texts == [OCR_TYPO.get(r, r) for r in MENU_ROWS]
    # 第一行在菜单框内，x 至少是框左边 + 60
    assert rows[0]["x"] >= nav.PUB_MENU_BOX[0] + 60
