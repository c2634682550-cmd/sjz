"""导航链路的行为契约。

钉住三类 bug：
  1. 禁用坐标（售出/充值）永远点不到
  2. 页面没走对必须报错 —— 「画面变了」不等于「走对了」
  3. 等比缩放只在窗口比例一致时成立

页面识别用的拼串全部抄自真机 OCR 实测（2026-10-20，1920x1080 窗口化），
所以以后改 classify() 会立刻被这些用例拦住。
"""
import numpy as np
import pytest

from core.nav import NavError, Navigator, classify, tab_from_pixels
from core.nav_coords import (COL_BUY, COL_CONFIRM, COL_FILTER, FORBIDDEN,
                             ONS_CONFIRM, ONS_FILTER, ONS_RESET, SKIN_CARD_X,
                             SKIN_CARD_Y, TAB_CHARM, TAB_COLLECTION, TAB_MANDEL,
                             _scaled)


# ---------------------------------------------------------------- 真机 OCR 原文
#
# 下面的 MARKET_TEXT 抄自真机完整截图（engnav_after.png），是三个市场标签页
# 共有的内容 —— 注意它同时含「曼德尔砖」「典藏外观」「典藏挂饰」「典藏工坊」
# 「当前市场流通量」，所以**文本层面无法区分三个标签页**，只能判到 market。
# 早期测试里用过残缺片段，误以为能靠「曼德尔砖」区分，那个假设是错的。

MARKET_TEXT = ("CNUIO:I917734233179499一2囗231囗2囗19132，0490曼德尔砖交易筛选"
               "典藏外观典藏挂饰0-AKM突击步枪．群星当前市场流通量：999+交"
               "开放时间：08：00．次日05040+我的关注典藏工坊MK4冲锋枪．疾光魅影"
               "生典藏AKM突击步枪．群星生典藏KC17突击步枪．造物纪一生典藏")

ONSALE_TEXT = ("CNUIO:I巳巳917734233179499一2囗231囗2囗1巳57204940+，0推荐0不限公示期"
               "默0争认摊序MK4冲锋枪一疾光魅影MK4冲锋枪疾光魅影．MK4冲锋枪疾光魅影．")

DETAIL_TEXT = ("CNUJO:I巳巳917734233179499一2囗231囗2囗19囗1204940+，00不限公示期默0"
               "争认摊序MK4冲锋枪一疾光魅影肤生相似皮0编号0002223130稀有度"
               "成色B〔1．732825〕极品》021成色A》80000淘汰计数18材质镭射")

ONSALE_FILTER_TEXT = ("CN引O:I巳巳917734233179499一2囗231囗2囗1巳57推荐筛选价格区间"
                      "0205009999999，，，丫筛选不公示期MK4冲锋枪疾光魅．影稀有度"
                      "[@]所有稀有度极品》的成色22囤00极品成色B[@]所有成色材质"
                      "〔可选至多2种〕[@]不限速度线镭射贵金属贵金属")

COLLECTION_FILTER_TEXT = ("CN引0.1巳888巳917734233179499．2囗2E1囗2囗1巳54厶筛选赛季"
                          "全部赛季查看已拥有未拥有传说品0阶0史诗品阶稀有品阶曰通品阶"
                          "枪种0步枪凵冲锋枪霰弹枪轻机枪精准射手0步枪狙击步枪手枪确认")

# 大厅。实测原文（游戏退回主界面后抓的）。
HALL_TEXT = ("CNUIO:I巳巳917734233179499一2囗231囗2囗1917开始游戏，别然茄汞兰分虐、"
             "老太轼[世界]赛伊德F3：航天找老仓库特战干员部门交易行特勤处"
             "02，477K极星守望0显示装备s11：群星零量杰坝02，089+0返行动9天"
             "2时27分行前备找0游对局")

# 市场页的第二种 OCR 形态：内容稀疏时「典藏工坊」会整段漏掉，
# 「典藏外观」被读成「典崆外」，只剩「当季产出」「往季产」「市场流通量」。
MARKET_TEXT_SPARSE = ("典崆外当季产出曼德尔砖．疾光魅影：：：二一一往季产曼德尔砖．星"
                      "尔气感应。典藏饰0曼德尔砖·电玩高手2=当市流通量999+个拥有，"
                      "0/1000个成交均价2，04900+，4044142交开放、400次日05，00")


# ---------------------------------------------------------------- 假对象

class FakeScreen:
    def __init__(self, size=(1920, 1080), frames=None):
        self.size = size
        self._frames = list(frames or [])
        self._i = 0

    def grab(self, box=None):
        if self._frames:
            f = self._frames[min(self._i, len(self._frames) - 1)]
            self._i += 1
            return f
        h, w = self.size[1], self.size[0]
        return np.zeros((h, w, 3), np.uint8)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, verify=True, expect_foreground=None):
        self.clicks.append((x, y))
        return True


class FakeKeyboard:
    def __init__(self):
        self.taps = []

    def tap(self, key, **kw):
        self.taps.append(key)
        return True


def _nav(size=(1920, 1080), frames=None, ocr_text="",
         logs=None, buy_visible=True):
    screen = FakeScreen(size, frames)
    mouse = FakeMouse()
    kb = FakeKeyboard()
    logs = logs if logs is not None else []

    def fake_ocr(img):
        return [{"text": ocr_text, "x": 0, "y": 0, "w": 10, "h": 10}]

    n = Navigator(screen, mouse, kb, snap=screen.grab,
                  log=lambda m, level="info": logs.append((level, m)),
                  ocr_read=fake_ocr)
    # buy() 点之前会先确认右侧真的是「购买」；假屏幕读不出字，这里直接给定
    n._buy_button_visible = lambda: buy_visible
    return n, screen, mouse, kb, logs


def _blank(h=1080, w=1920):
    return np.zeros((h, w, 3), np.uint8)


def _white(h=1080, w=1920):
    return np.full((h, w, 3), 255, np.uint8)


def _tab_img(green_x0, green_x1, width=1920, height=1080, y=53):
    """造一张只有标签栏绿框的假截图。

    颜色用真机实测值 BGR(133,177,86) —— 早期测试里用的是亮绿
    BGR(90,210,90)，比真机亮得多，于是「判据过严」这个 bug 测不出来。
    """
    img = np.zeros((height, width, 3), np.uint8)
    img[:, :] = (33, 33, 22)                    # BGR 深色底
    img[y, green_x0:green_x1] = (133, 177, 86)  # 真机实测的绿框颜色
    return img


# ---------------------------------------------------------------- 安全护栏

def test_refuses_to_click_sell_button():
    """「售出」永远点不到 —— 点错就是卖东西，不可逆。"""
    n, _, mouse, _, _ = _nav()
    with pytest.raises(NavError, match="售出"):
        n.click(FORBIDDEN["售出"])
    assert mouse.clicks == []


def test_refuses_to_click_recharge_button():
    """「充值」永远点不到 —— 点错就花真钱。"""
    n, _, mouse, _, _ = _nav()
    with pytest.raises(NavError, match="充值"):
        n.click(FORBIDDEN["充值"])
    assert mouse.clicks == []


def test_refuses_near_miss_above_sell_button():
    """坐标漂移到「售出」上方 30px 也要拒绝。

    曼德尔砖页「购买」(1642,959) 和「售出」(1634,1032) 只差 73px；
    按卡片高度漂移一次就可能落到售出上，所以护栏半径必须够大。
    """
    n, _, mouse, _, _ = _nav()
    with pytest.raises(NavError, match="售出"):
        n.click((1634, 1002))
    assert mouse.clicks == []


def test_buy_uses_the_collection_page_coordinate():
    """购买只认典藏外观页的 (1700,885)，不试备选坐标。

    曼德尔砖页的购买在 (1642,959)，两个坐标差 74px —— 如果 buy() 去试
    备选，第一次点空之后就可能点到别的东西上。
    """
    n, _, mouse, _, _ = _nav(frames=[_blank(), _white()], ocr_text=DETAIL_TEXT)
    n.buy()
    assert mouse.clicks == [COL_BUY]


def test_all_clickable_points_are_far_from_forbidden():
    """所有会真的点下去的坐标，离禁用按钮至少 40px。

    这条钉住的是「以后加新坐标时不能不小心加在售出/充值旁边」。
    """
    clickable = {
        "TAB_MANDEL": TAB_MANDEL,
        "TAB_COLLECTION": TAB_COLLECTION,
        "TAB_CHARM": TAB_CHARM,
        "COL_FILTER": COL_FILTER,
        "COL_BUY": COL_BUY,
        "ONS_FILTER": ONS_FILTER,
        "ONS_CONFIRM": ONS_CONFIRM,
        "ONS_RESET": ONS_RESET,
        "COL_CONFIRM": COL_CONFIRM,
    }
    for name, pt in clickable.items():
        for bad_name, bad in FORBIDDEN.items():
            dx = abs(pt[0] - bad[0])
            dy = abs(pt[1] - bad[1])
            assert not (dx <= 40 and dy <= 40), (
                f"{name}{pt} 离「{bad_name}」{bad} 太近（dx={dx} dy={dy}）")


# ---------------------------------------------------------------- 页面识别

@pytest.mark.parametrize("text,want", [
    (MARKET_TEXT, "market"),
    (MARKET_TEXT_SPARSE, "market"),
    (HALL_TEXT, "hall"),
    (ONSALE_TEXT, "onsale"),
    (DETAIL_TEXT, "detail"),
    (ONSALE_FILTER_TEXT, "filter_onsale"),
    (COLLECTION_FILTER_TEXT, "filter_collection"),
])
def test_classify_real_ocr_text(text, want):
    """真机 OCR 原文必须能认出正确的页面。

    这些原文里的错字（「伟场流」「争认摊序」「拥有该电玩手s2」）是真实
    存在的，判据必须绕开它们。
    """
    assert classify(text) == want


def test_classify_empty_is_unknown():
    assert classify("") == "unknown"
    assert classify(None) == "unknown"
    assert classify("       ") == "unknown"


# 典藏外观页（砖皮列表）真机 1 倍 OCR 原文：页面上全是小字，只剩碎片，
# 但「曼德尔砖典藏外观典藏」这几个字认得准 —— 靠它认出这是市场页，
# 否则导航会以为站错页、按 ESC 乱复位。
COLLECTION_TEXT = ("CNuJ18333巳917734233179499一2囗231囗23173囗时钟+0m52，0490"
                   "ASM1腾龙SMVec生S12曼德尔砖典藏外观典藏")


def test_classify_collection_page_is_the_market():
    assert classify(COLLECTION_TEXT) == "market"


def test_onsale_page_wins_over_the_collection_anchor():
    """在售页的详情面板里也写着「典藏外观」—— 顺序反了会把它认成市场页。"""
    assert classify(ONSALE_TEXT + "典藏外观") == "onsale"
    assert classify(DETAIL_TEXT + "典藏外观") == "detail"


def test_market_tabs_are_indistinguishable_by_text():
    """三个市场标签页的 OCR 文字高度重叠，文本层面只能判到 market。

    真机实测：曼德尔砖页和典藏外观页的 OCR 都含「典藏工坊」「市场流通量」
    「曼德尔砖」「典藏外观」「典藏挂饰」。所以**不要试图用文本区分它们**，
    唯一可靠的判据是顶部绿框的位置（tab_from_pixels）。

    这条测试是给未来的自己留的警告：如果哪天有人往 _PAGE_ANCHORS 里加
    ("market_mandel", ("曼德尔砖",))，这里会立刻红。
    """
    assert classify(MARKET_TEXT) == "market"
    # 关键：不能有任何一个锚点把这段文本判成具体标签页
    assert classify(MARKET_TEXT) not in ("market_mandel", "market_collection",
                                         "market_charm")



def test_classify_filter_wins_over_page_underneath():
    """筛选面板盖在页面上时 OCR 会同时读到两者，必须判成筛选面板。

    判错成「在售页」的后果：以为面板没开，再点一次「筛选」把它关掉，
    然后对着关闭的面板输价格 —— 输到商品列表上。
    """
    assert classify(ONSALE_FILTER_TEXT + ONSALE_TEXT) == "filter_onsale"


# ---------------------------------------------------------------- 标签像素

def test_tab_pixels_detects_mandel():
    """绿框在 x127-257 → 曼德尔砖。"""
    assert tab_from_pixels(_tab_img(127, 257)) == "mandel"


def test_tab_pixels_detects_collection():
    """绿框在 x257-387 → 典藏外观。实测点 (323,55) 后就是这个位置。"""
    assert tab_from_pixels(_tab_img(257, 387)) == "collection"


def test_tab_pixels_detects_charm():
    """绿框在 x389-519 → 典藏挂饰。"""
    assert tab_from_pixels(_tab_img(389, 519)) == "charm"


def test_tab_pixels_returns_none_without_green():
    """没有绿框 → None（不在市场页，或者标签栏被弹层盖住）。"""
    assert tab_from_pixels(_blank()) is None


def test_tab_pixels_ignores_gray_underline():
    """灰色下划线不能当成选中框。

    未选中的标签也有下划线，只是灰的。判据要求 G 明显高于 R/B，
    灰色（R≈G≈B）会被排除。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[53, 257:387] = (150, 150, 150)
    assert tab_from_pixels(img) is None


# ---------------------------------------------------------------- 假阳性

def test_tab_raises_when_green_box_does_not_move():
    """点了标签但绿框没动 → 抛 NavError。

    这是真机踩到的坑：起点在在售页，点 (323,55) 只是关掉了筛选面板，
    画面变化 33.6%，看着像切成功了，其实标签栏一动没动。
    """
    n, _, _, _, _ = _nav(frames=[_tab_img(127, 257)] * 30)
    with pytest.raises(NavError, match="没切过去"):
        n.tab("collection")


def test_tab_succeeds_when_green_box_moves():
    """绿框真的移过去了才算成功。"""
    frames = [_tab_img(127, 257)] * 3 + [_tab_img(257, 387)] * 30
    n, _, _, _, logs = _nav(frames=frames)
    assert n.tab("collection") is True
    assert any("标签确认：collection" in m for _, m in logs)


def test_tab_is_noop_when_already_there():
    """已经在目标标签上就不点 —— 避免点一下反而切走。"""
    n, _, mouse, _, _ = _nav(frames=[_tab_img(257, 387)] * 30)
    assert n.tab("collection") is True
    assert mouse.clicks == []


def test_buy_raises_when_page_is_still_collection():
    """点购买后还在典藏外观页 → 抛 NavError，不能当成成功。

    静默继续的后果：以为进了在售页，对着商品列表点「筛选」，把列表
    上的东西点乱了。
    """
    n, _, _, _, _ = _nav(frames=[_blank(), _white()], ocr_text=MARKET_TEXT)
    with pytest.raises(NavError, match="期望页面"):
        n.buy()


def test_buy_succeeds_when_page_is_detail():
    n, _, _, _, _ = _nav(frames=[_blank(), _white()], ocr_text=DETAIL_TEXT)
    assert n.buy() is True


def test_ons_filter_raises_when_panel_does_not_open():
    """点筛选但面板没开 → 抛 NavError。"""
    n, _, _, _, _ = _nav(frames=[_blank(), _white()], ocr_text=ONSALE_TEXT)
    with pytest.raises(NavError, match="打开在售筛选"):
        n.ons_filter()


def test_ons_filter_succeeds_when_panel_opens():
    n, _, _, _, _ = _nav(frames=[_blank(), _white()],
                         ocr_text=ONSALE_FILTER_TEXT)
    assert n.ons_filter() is True


def test_require_reports_actual_page_in_error():
    """报错信息要带实际页面和 OCR 原文，方便定位。"""
    n, _, _, _, _ = _nav(ocr_text=MARKET_TEXT)
    with pytest.raises(NavError) as ei:
        n.require("onsale", "测试步骤")
    msg = str(ei.value)
    assert "market" in msg
    assert "测试步骤" in msg


# ---------------------------------------------------------------- 点击反馈

def test_click_reports_change_ratio():
    """画面变了就返回 >0.10 的比例。"""
    n, _, _, _, _ = _nav(frames=[_blank(), _white()])
    assert n.click((100, 100), "测试") > 0.10


def test_click_reports_zero_when_unchanged():
    """画面没变返回 0（不抛错，由调用方决定要不要抛）。"""
    n, _, _, _, _ = _nav(frames=[_blank()] * 20)
    assert n.click((100, 100), "测试", timeout=0.5) == 0.0


# ---------------------------------------------------------------- 缩放

def test_scaling_is_identity_at_base_resolution():
    assert _scaled((1700, 885), 1920, 1080) == (1700, 885)


def test_scaling_halves_at_half_resolution():
    """960x540 下坐标减半。等比缩放只在窗口比例一致时成立。"""
    assert _scaled((1700, 885), 960, 540) == (850, 442)


def test_navigator_scales_click_coordinates():
    """Navigator 按自己 screen 的尺寸缩放点击坐标。"""
    n, _, mouse, _, _ = _nav(size=(960, 540), frames=[_blank(540, 960),
                                                      _white(540, 960)],
                             ocr_text=DETAIL_TEXT)
    n.buy()
    assert mouse.clicks == [(850, 442)]


# ---------------------------------------------------------------- F4

def test_open_market_presses_f4_via_keyboard():
    """F4 必须走 Keyboard（PostMessage），不能用 SendInput。

    SendInput 被游戏的 Raw Input 过滤，实测提权也一样无效。
    """
    n, _, _, kb, _ = _nav(frames=[_blank(), _white()])
    assert n.open_market() is True
    assert kb.taps == ["F4"]


def test_open_market_returns_false_when_nothing_happens():
    """F4 只开不关：已经在市场里时按 F4 没反应是正常的，返回 False 不报错。"""
    n, _, _, kb, _ = _nav(frames=[_blank()] * 30)
    assert n.open_market() is False
    assert kb.taps == ["F4"]


def test_open_market_without_keyboard_raises():
    n, _, _, _, _ = _nav()
    n.keyboard = None
    with pytest.raises(NavError, match="F4"):
        n.open_market()


# ---------------------------------------------------------------- 全链路

def test_go_to_onsale_stops_at_first_wrong_page():
    """链路中途页面不对就停，不继续往下点。

    这是最重要的一条：错了就停，绝不「再试试下一个坐标」。
    """
    n, _, mouse, _, _ = _nav(frames=[_tab_img(127, 257)] * 30,
                             ocr_text=MARKET_TEXT)
    with pytest.raises(NavError):
        n.go_to_onsale(from_hall=False)
    assert len(mouse.clicks) <= 1


# ---------------------------------------------------------------- 皮肤卡片

def test_skin_cards_start_at_298_not_272():
    """典藏外观页第一张卡片的中心是 y≈298，不是 272，也不是曼德尔砖页的 437。

    卡片高 114px（两行字：皮肤名 + 「典藏」标签），实测皮肤名中心 260、
    「典藏」标签 337，卡片中心取中点 298。早期按 76px 卡片高估成 272，
    真机点 (150,272) 完全没响应。
    """
    assert SKIN_CARD_Y[0] == 298
    assert SKIN_CARD_Y[0] != 272
    assert SKIN_CARD_Y[0] != 437
    assert len(SKIN_CARD_Y) == 6


def test_skin_card_pitch_is_about_114():
    """相邻卡片间距 ≈114px（真机实测）。间距错了就会点到两张卡片之间。"""
    ys = SKIN_CARD_Y
    for a, b in zip(ys, ys[1:]):
        assert 108 <= b - a <= 120, f"{a} -> {b} 间距 {b - a} 不合理"


def test_pick_skin_by_index_uses_the_table():
    """按序号选卡片用 SKIN_CARD_Y 里的值。

    pick_skin 现在会重试（读一次就点不可靠），所以这里只要求点到过
    正确位置，不要求只点一次。
    """
    n, _, mouse, _, _ = _nav(frames=[_blank(), _white()])
    n.pick_skin(index=1)
    assert (SKIN_CARD_X, SKIN_CARD_Y[1]) in mouse.clicks


def test_pick_skin_explicit_y_wins():
    """给了 y 就用给的，不看序号。"""
    n, _, mouse, _, _ = _nav(frames=[_blank(), _white()])
    n.pick_skin(y=999, index=0)
    assert mouse.clicks == [(SKIN_CARD_X, 999)]


def test_pick_skin_rejects_out_of_range_index():
    n, _, mouse, _, _ = _nav()
    with pytest.raises(NavError, match="超出范围"):
        n.pick_skin(index=99)
    assert mouse.clicks == []



# ---------------------------------------------------------------- 复位

def test_recover_presses_esc_until_market():
    """从在售页按 ESC 退回市场。

    实测场景：用户点开始时可能停在在售页、筛选面板开着。硬编码「按 F4
    开市场」在那种情况下无效（F4 只开不关），必须能自己退回来。
    """
    n, _, _, kb, _ = _nav(frames=[_blank()] * 40, ocr_text=ONSALE_TEXT)
    n._ocr_read = None
    seq = [ONSALE_TEXT, "随便", MARKET_TEXT]

    def fake_ocr(img):
        return [{"text": seq.pop(0) if seq else MARKET_TEXT,
                 "x": 0, "y": 0, "w": 10, "h": 10}]

    n._ocr_read = fake_ocr
    # recover() 每轮还会调 _panel_open() / _ons_filter_open()（判两个筛选
    # 面板在不在），它们走 OCR 会消耗上面的 seq。这里直接给结果 —— 本测试
    # 关心的是 ESC 次数，面板判据在 test_nav_filter.py 里有专门的真机标定。
    n._panel_open = lambda: False
    n._ons_filter_open = lambda: False
    assert n.recover() == "market"
    assert kb.taps == ["ESC", "ESC"]


def test_recover_raises_when_esc_does_nothing():
    """按了 ESC 还在原地 → 抛 NavError，不盲试更多次。"""
    n, _, _, kb, _ = _nav(frames=[_blank()] * 40, ocr_text=ONSALE_TEXT)
    with pytest.raises(NavError, match="复位失败"):
        n.recover(max_esc=2)
    assert kb.taps == ["ESC", "ESC"]


def test_recover_presses_f4_when_in_hall():
    """在大厅要按 F4，不能按 ESC —— ESC 在大厅会弹出退出菜单。

    实测踩过：游戏退回大厅后 recover() 继续按 ESC，结果 ESC 打开了
    游戏菜单，页面变得无法识别。
    """
    n, _, _, kb, _ = _nav(frames=[_blank()] * 40)
    seq = [HALL_TEXT, MARKET_TEXT]

    def fake_ocr(img):
        return [{"text": seq.pop(0) if seq else MARKET_TEXT,
                 "x": 0, "y": 0, "w": 10, "h": 10}]

    n._ocr_read = fake_ocr
    assert n.recover() == "market"
    assert kb.taps == ["F4"]      # 只按 F4，一次 ESC 都没按


def test_recover_is_noop_when_already_in_market():
    n, _, _, kb, _ = _nav(ocr_text=MARKET_TEXT)
    assert n.recover() == "market"
    assert kb.taps == []



def test_buy_refuses_to_click_when_the_button_is_not_there():
    """右侧看不到「购买」时一次都不能点。

    皮肤已经拥有时那个位置变成「售出」—— 盲点就是把自己那件挂出去
    （真机踩过，用户当场喊停）。
    """
    n, _, mouse, _, logs = _nav(frames=[_blank(), _white()],
                                ocr_text=DETAIL_TEXT, buy_visible=False)
    with pytest.raises(NavError) as ei:
        n.buy()
    assert "购买" in str(ei.value)
    assert mouse.clicks == [], f"点了 {mouse.clicks}"
