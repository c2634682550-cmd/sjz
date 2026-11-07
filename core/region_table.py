"""内置区域坐标表（1080p 客户区基准）。

这是**唯一**的内置标定表：配置默认值、界面展示、自动标定、离线预设
验证全部从这里取，不在别处再写一遍数字。

为什么以 1920×1080 客户区为基准：
  · 仓库里 5 张真实游戏截图（u1~u5）和 `docs/auto-buy-design.md` §4.x
    的实测坐标都是这个口径；
  · 归一化之后，换分辨率/窗口大小只做等比缩放，不需要重新标定；
  · 真正换算像素的地方仍然只有 `Screen.norm_to_px()` 一处。

每条坐标的来源（实测，不是猜的）：
  buy        N_BAR：购买按钮 x1482-1780 / y903-961（按钮上印的就是价格）
  confirm    确认页绿色价格按钮，按 `templates/confirm.png` 的 303×70
             与 N_CLOCK 倒计时位置 (1164..1280, 638) 推得；见
             `_video_analysis_ui.md` §2.5 的浮层几何
  list       N_LIST：在售列表 x60-1400 / y210-940
  clock      N_CLOCK：倒计时文字 (1164..1280, 638) 外扩到 300×80
  bar        u3_detail_price：详情页价格文字 (1593, 915, 110, 33)
  name       列表第一张卡片名称行（docs §4.3：名称在 y=236）
  price      u3_card_r1_l：列表第一张卡片价格 (620, 394, 120, 30)
  list_item  列表第一张卡片整体（名称+价格，供点击/锚点校验）
  toast      N_TOAST：顶部提示条 (520, 62, 880, 36)
"""

BASE_W = 1920.0
BASE_H = 1080.0

# 名称 → 1080p 客户区下的物理像素框 (x, y, w, h)
BUILTIN_PX = {
    "buy": (1482, 903, 298, 58),
    "confirm": (1164, 690, 303, 70),
    "list": (60, 210, 1340, 730),
    "clock": (1100, 600, 300, 80),
    "bar": (1593, 915, 110, 33),
    "name": (600, 232, 380, 34),
    "price": (620, 394, 120, 30),
    "list_item": (600, 220, 390, 250),
    "toast": (520, 62, 880, 36),
}

# 展示顺序 = 引导/高级设置里的顺序
ORDER = ("buy", "confirm", "list", "clock", "bar",
         "name", "price", "list_item", "toast")

LABELS = {
    "buy": "购买按钮",
    "confirm": "确认按钮",
    "list": "在售列表",
    "clock": "公示期倒计时",
    "bar": "详情价格",
    "name": "商品名称",
    "price": "价格文字",
    "list_item": "列表购买项",
    "toast": "顶部提示（toast）",
}


def norm_box(px):
    """1080p 像素框 → 归一化框（四舍五入到 6 位，和 PUT 校验口径一致）。"""
    x, y, w, h = px
    return {k: round(v, 6) for k, v in {
        "x": x / BASE_W, "y": y / BASE_H,
        "w": w / BASE_W, "h": h / BASE_H,
    }.items()}


BUILTIN = {name: norm_box(BUILTIN_PX[name]) for name in ORDER}


def clone(name=None):
    """返回内置坐标的副本；不传 name 返回整张表。"""
    import copy
    if name is None:
        return copy.deepcopy(BUILTIN)
    return dict(BUILTIN[name])
