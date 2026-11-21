"""假交易行：给识别 + 点击链路一个可控、可断言的对手。

只存在于测试里，不碰真游戏、不碰真屏幕、不注入任何输入。

三个部件
--------
  · `FakeMarket`   状态机 + 渲染器。页面：列表 → 详情 → 确认框 → 结果
                   （成功 / 被抢走 / 余额不足）。按钮热区由它自己判定，
                   每次点击都落一条账，供断言。
  · `FakeScreen`   `core.screen.Screen` 的替身：`grab()` 返回 FakeMarket 当前帧
                   （1920x1080 BGR），识别链路照常抓图、OCR。
  · `MarketMouse`  `core.dry_run.DryRunMouse` 的子类：仍然只记账、不注入，
                   同时把点击转给 FakeMarket 推进页面。它就是 RecordingDriver。

坐标一律用 1920x1080 基准的客户区像素（与 `core/config.py` 的归一化区域一致）。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core.dry_run import DryRunMouse

W, H = 1920, 1080

# 与默认配置里 list / confirm / buy 三块区域一致（core/config.py）
LIST_ORIGIN = (60, 210)
LIST_SIZE = (1340, 520)
CARD_W, CARD_H = 620, 206
CARD_GAP_X, CARD_GAP_Y = 30, 4
BUY_BTN = (1616, 1020, 1920, 1080)        # buy 区域 (0.8417,0.9454,0.1583,0.0546)
CONFIRM_BTN = (1164, 690, 1467, 760)      # confirm 区域
CANCEL_BTN = (880, 690, 1140, 760)

PAGES = ("list", "detail", "confirm", "success", "sold_out", "insufficient")

_FONT_CANDIDATES = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc")


def _font(size):
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


@dataclass
class Card:
    name: str = "QBZ95-1突击步枪-王牌之剑"
    craft: str = "优品"
    wear: str = "成色B"
    price: int | None = 2050      # None = 卡面上价格读不出（只画「暂无」）


def default_cards():
    return [Card(wear="成色C", price=2050), Card(price=2050),
            Card(price=2100), Card(price=2500)]


@dataclass
class FakeMarket:
    cards: list = field(default_factory=default_cards)
    #: 确认后的结局：success / sold_out / insufficient
    outcome: str = "success"
    balance: int = 100000
    #: True = 点卡片只是选中（详情面板出现在右边，列表还在），倒计时流程就是这样用的
    select_keeps_list: bool = False
    page: str = "list"
    selected: int | None = None
    clicks: list = field(default_factory=list)      # 每次点击的判定账
    purchases: list = field(default_factory=list)   # 真正成交的卡
    _frame: np.ndarray | None = field(default=None, repr=False)

    # ------------------------------------------------------------ 几何

    def card_box(self, i):
        """第 i 张卡在整帧里的矩形 (x0,y0,x1,y1)。"""
        col, row = i % 2, i // 2
        x0 = LIST_ORIGIN[0] + 30 + col * (CARD_W + CARD_GAP_X)
        y0 = LIST_ORIGIN[1] + 4 + row * (CARD_H + CARD_GAP_Y)
        return x0, y0, x0 + CARD_W, y0 + CARD_H

    # ------------------------------------------------------------ 点击

    def click(self, x, y):
        """按当前页面热区判定一次点击并推进页面，返回判定结果。"""
        before = self.page
        hit = None
        if self.page == "list":
            for i, _c in enumerate(self.cards):
                x0, y0, x1, y1 = self.card_box(i)
                if x0 <= x <= x1 and y0 <= y <= y1:
                    hit, self.selected = f"card:{i}", i
                    if not self.select_keeps_list:
                        self.page = "detail"
                    break
            if (hit is None and self.select_keeps_list and self.selected is not None
                    and _inside(BUY_BTN, x, y)):
                hit, self.page = "buy", "confirm"
        elif self.page == "detail":
            if _inside(BUY_BTN, x, y):
                hit, self.page = "buy", "confirm"
        elif self.page == "confirm":
            if _inside(CONFIRM_BTN, x, y):
                hit = "confirm"
                if self.outcome == "success":
                    self.page = "success"
                    self.balance -= self.cards[self.selected].price or 0
                    self.purchases.append(self.cards[self.selected])
                else:
                    self.page = self.outcome
            elif _inside(CANCEL_BTN, x, y):
                hit, self.page = "cancel", "detail"
        elif self.page in ("success", "sold_out", "insufficient"):
            if _inside((760, 620, 1160, 700), x, y):
                hit, self.page = "dismiss", "list"
        self._frame = None
        rec = {"t": time.time(), "x": int(x), "y": int(y), "page_before": before,
               "page_after": self.page, "hit": hit}
        self.clicks.append(rec)
        return rec

    # ------------------------------------------------------------ 渲染

    def frame(self):
        if self._frame is None:
            self._frame = self._render()
        return self._frame

    def _render(self):
        img = Image.new("RGB", (W, H), (12, 20, 24))
        d = ImageDraw.Draw(img)
        for y in range(0, H, 4):                       # 竖向渐变，别全黑
            v = 14 + int(26 * y / H)
            d.rectangle([0, y, W, y + 4], fill=(v, v + 8, v + 10))
        if self.page == "list":
            self._draw_list(d)
            if self.select_keeps_list and self.selected is not None:
                self._draw_detail(d, self.cards[self.selected])
        else:
            if self.selected is not None:
                self._draw_detail(d, self.cards[self.selected])
            if self.page == "confirm":
                self._draw_dialog(d, "确认购买", "确定", cancel=True)
            elif self.page == "success":
                self._draw_dialog(d, "购买成功", "确定")
            elif self.page == "sold_out":
                self._draw_dialog(d, "商品已被抢走", "确定")
            elif self.page == "insufficient":
                self._draw_dialog(d, "余额不足", "确定")
        return np.asarray(img)[:, :, ::-1].copy()      # RGB → BGR

    def _draw_list(self, d):
        f_tag, f_small, f_price = _font(20), _font(22), _font(26)
        for i, c in enumerate(self.cards):
            x0, y0, x1, y1 = self.card_box(i)
            d.rectangle([x0, y0, x1, y1 - 4], fill=(30, 40, 46))
            tw = d.textlength(c.name, font=f_tag)
            d.rectangle([x0 + 4, y0 + 4, x0 + 12 + tw, y0 + 32], fill=(70, 58, 50))
            d.text((x0 + 8, y0 + 6), c.name, font=f_tag, fill=(225, 215, 200))
            d.polygon([(x0 + 298, y0 + 120), (x0 + 314, y0 + 92), (x0 + 330, y0 + 120)],
                      fill=(150, 156, 160))
            d.text((x0 + 6, y1 - 42), f"{c.craft} | {c.wear}", font=f_small,
                   fill=(160, 170, 175))
            price = "暂无" if c.price is None else f"{c.price:,}"
            pw = d.textlength(price, font=f_price)
            d.text((x1 - 8 - pw, y1 - 44), price, font=f_price, fill=(235, 238, 240))
            d.ellipse([x1 - 46 - pw, y1 - 40, x1 - 22 - pw, y1 - 16], fill=(190, 160, 90))

    def _draw_detail(self, d, c):
        f_title, f_body, f_price = _font(30), _font(24), _font(34)
        d.rectangle([1300, 80, 1900, 1040], fill=(20, 28, 33))
        d.text((1330, 100), f"{c.name}", font=f_title, fill=(230, 232, 235))
        for k, (a, b) in enumerate((("稀有度", c.craft), ("成色", c.wear))):
            d.text((1330, 420 + k * 56), a, font=f_body, fill=(150, 160, 165))
            d.text((1700, 420 + k * 56), b, font=f_body, fill=(220, 224, 228))
        d.text((1560, 940), "剩余: 9分37秒", font=f_body, fill=(220, 224, 228))
        x0, y0, x1, y1 = BUY_BTN
        d.rectangle([x0 + 10, y0 + 4, x1 - 10, y1 - 4], outline=(60, 200, 130), width=3,
                    fill=(20, 40, 36))
        d.text((x0 + 80, y0 + 14), "暂无" if c.price is None else f"{c.price:,}", font=f_price, fill=(235, 70, 70))

    def _draw_dialog(self, d, title, ok_text, cancel=False):
        f_title, f_btn = _font(34), _font(28)
        d.rectangle([700, 380, 1220, 780], fill=(24, 32, 38), outline=(90, 110, 120), width=2)
        d.text((820, 440), title, font=f_title, fill=(235, 238, 240))
        x0, y0, x1, y1 = CONFIRM_BTN if cancel else (760, 620, 1160, 700)
        d.rectangle([x0, y0, x1, y1], fill=(36, 90, 66))
        d.text(((x0 + x1) // 2 - 28, y0 + 16), ok_text, font=f_btn, fill=(240, 245, 242))
        if cancel:
            cx0, cy0, cx1, cy1 = CANCEL_BTN
            d.rectangle([cx0, cy0, cx1, cy1], fill=(50, 56, 60))
            d.text(((cx0 + cx1) // 2 - 28, cy0 + 16), "取消", font=f_btn, fill=(220, 224, 228))


def _inside(box, x, y):
    x0, y0, x1, y1 = box
    return x0 <= x <= x1 and y0 <= y <= y1


class FakeScreen:
    """`Screen` 的替身：抓的是 FakeMarket 当前帧，接口与 core.screen.Screen 对齐。"""

    base_rect = {"left": 0, "top": 0, "width": W, "height": H}
    size = (W, H)
    origin = (0, 0)

    def __init__(self, market):
        self.market = market

    def grab(self, box=None):
        frame = self.market.frame()
        if box is None:
            return frame.copy()
        x, y, w, h = (int(v) for v in box)
        return frame[y:y + h, x:x + w].copy()

    def norm_to_px(self, box):
        return (int(round(box["x"] * W)), int(round(box["y"] * H)),
                max(1, int(round(box["w"] * W))), max(1, int(round(box["h"] * H))))

    def close(self):
        pass


class MarketMouse(DryRunMouse):
    """RecordingDriver：只记账、不注入（沿用 DryRunMouse），点击同时推进假交易行。"""

    def __init__(self, market, screen=None):
        super().__init__(screen or FakeScreen(market))
        self.market = market

    def click(self, x, y, expect_foreground=None, hold=None, verify=True, **kw):
        super().click(x, y, expect_foreground=expect_foreground, hold=hold,
                      verify=verify, **kw)
        self.market.click(x, y)
        return True
