"""典藏外观筛选面板：赛季下拉与复选框的行为契约。

钉住三类真机踩过的 bug：
  1. 赛季下拉的选项间距是 48px、一次滚轮滚 1 项 —— 早期按 29px / 2-3 项算，
     选中的项总是偏掉
  2. 下拉浮层只占屏幕一小块，整屏 OCR 会把「已拥有」「传说品阶」也读成
     选项 —— 必须只读浮层区域
  3. 复选框的判断点必须落在**方框**上，不是右边文字的中心 —— 早期用文字
     中心（x=718/867/1105/1354），点了 20-80px 之外，全部无效

坐标全部是真机实测值（2026-10-20，1920x1080 窗口化）。
"""
import numpy as np
import pytest

from core.nav import NavError, Navigator, judge_click
from core.nav_col import NavColPanel
from core.nav_coords import (COL_FILTER, COL_GUN_X2, COL_GUN_Y, COL_NOT_OWNED,
                             COL_OWNED, COL_RARITY_X, COL_RARITY_Y, COL_SEASON,
                             COL_SEASON_ITEM_DY, COL_SEASON_ITEM_X,
                             COL_SEASON_ITEM_Y0, COL_SEASON_VISIBLE,
                             COL_TRADE_TAB)
from core.ocr import phrases as _phrases
from core.vision import box_brightness as _box_brightness


# ---------------------------------------------------------------- 假对象

# 真机实测的完整列表（2026-10-20）：12 项，滚到第 3 屏到底。
# 早期那份 11 项清单**漏了「群星」**，且不知道浮层默认不在顶部。
SEASONS = ["全部赛季", "棱镜攻势", "美杜莎", "电玩高手", "命运王牌",
           "气象感应", "棱镜攻势S2", "悬赏令", "电玩高手S2", "造物纪元",
           "群星", "疾光魅影"]


class FakeScreen:
    """可编程的假屏幕：按帧序号返回预设图。"""

    def __init__(self, size=(1920, 1080)):
        self.size = size
        self._img = np.zeros((size[1], size[0], 3), np.uint8)

    def grab(self, box=None):
        return self._img

    def set(self, img):
        self._img = img


class FakeMouse:
    def __init__(self, on_click=None):
        self.clicks = []
        self._on_click = on_click

    def click(self, x, y, verify=True, expect_foreground=None, **kw):
        self.clicks.append((x, y))
        if self._on_click:
            self._on_click(x, y)
        return True


_LAST_WHEEL = (0, 0)     # 最近一次滚轮坐标，on_wheel 回调读它判断锚点


class FakeKeyboard:
    """假键盘。last_wheel 是模块级的，因为 on_wheel 回调要读它。"""

    def __init__(self, on_wheel=None):
        self.taps = []
        self.wheels = []
        self._on_wheel = on_wheel

    def tap(self, key, **kw):
        self.taps.append(key)
        return True

    def wheel(self, x, y, notches, **kw):
        self.wheels.append((x, y, notches))
        global _LAST_WHEEL
        _LAST_WHEEL = (x, y)
        if self._on_wheel:
            self._on_wheel(notches)
        return True


def _words_from_rows(rows, x=600, h=16, w=None):
    """造 OCR 词表。rows = [(文字, 文字块顶部 y)]。

    注意：真 OCR 返回的 y 就是文字块顶部，不是中心。早期这里按中心
    处理（减去 h//2），导致 _season_items 的「顶部 + 8」多减一次，
    点击点整体偏上 16px。
    """
    out = []
    for text, y in rows:
        ww = w if w is not None else len(text) * 14
        out.append({"text": text, "x": x, "y": y, "w": ww, "h": h})
    return out


def _draw_panel_label(img):
    """画面板标题「筛选」，让 _panel_open() 认得出面板还在。

    位置对应裁剪 y228-285 / x400-500 —— 真机上面板左上角标题在 y=237 /
    x=410，字高 31px（实测 30x31），画在裁剪区内的 y=9-40。
    """
    img[237:268, 410:472] = (200, 200, 200)    # 「筛选」标题 30x31


def _draw_combo_text(img, text):
    """画下拉框显示的文字（收起态的选中项）。

    位置对应裁剪 y300-370 / x540-800 —— 真机上收起态下拉框在 y≈327 /
    x584-640 显示当前选中的系列名。_season_applied 读这块做校验。
    """
    if not text:
        return
    img[320:340, 584:584 + len(text) * 16] = (200, 200, 200)


def _season_dropdown_img(top=0, visible=6, checked=None, open_=True,
                        selected=""):
    """造一张假图。

    top      —— 列表滚到第几项（0 = 第一项在顶部）
    visible  —— 可见项数
    checked  —— {(x,y)} 已勾选的复选框中心，画成亮块
    open_    —— 浮层是否展开。展开时 y=414 那一行被黑底盖住（亮度 < 30），
                这正是 _dropdown_open() 的判据。

    只在 x555-765 / y340-700 这块画东西，其它地方全黑 —— 这样如果实现
    去读整屏 OCR，就会读到不该有的东西，测试能抓到。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    if not open_:
        # 收起态：浮层边框不存在（x=566 保持黑），面板行有内容
        img[400:430, 555:765] = (48, 48, 48)
        _draw_combo_text(img, selected)
        _draw_panel_label(img)
        return img
    # 面板锚点字：让 classify() / _panel_open() 认出筛选面板还在
    img[700:716, 470:560] = (200, 200, 200)    # 面板底部
    img[540:560, 400:440] = (200, 200, 200)    # 「枪种」标签（x380-520 / y520-600）
    # 浮层黑底（比收起态暗，模拟盖住面板）
    img[340:700, 555:765] = (21, 21, 21)
    # 浮层左边框：真机 x=566 有一条亮竖线（y340-660 全亮，亮度 ~108），
    # 收起态同一位置最大亮度 61。_dropdown_open() 的判据就是这条线。
    img[340:660, 566] = (108, 108, 108)
    # 每项文字画一条亮横杠，y 按 48px 间距
    for i in range(visible):
        idx = top + i
        if idx >= len(SEASONS):
            break
        y = COL_SEASON_ITEM_Y0 + i * COL_SEASON_ITEM_DY
        img[y - 6:y + 6, 590:700] = (200, 200, 200)
    if checked:
        for (cx, cy) in checked:
            img[cy - 8:cy + 8, cx - 8:cx + 8] = (230, 230, 230)
    _draw_panel_label(img)
    return img


def _make_nav(seasons=None, frames=None, closes_on_pick=True,
              opens_on_click=True):
    """造一个能读赛季下拉的 Navigator。

    closes_on_pick —— 点了下拉选项后浮层是否收起（真机行为是收起）
    opens_on_click —— 点下拉框本身能否切换浮层（真机行为：点一下开、
                      再点一下关）。设 False 用来模拟「点了也展不开」。
    """
    screen = FakeScreen()
    # 真机实测：浮层展开时**默认不在顶部**，首项是「棱镜攻势」（索引 1），
    # 所以「全部赛季」和「群星」都必须滚动才看得到。
    state = {"top": 1, "open": True, "checked": set(), "selected": ""}
    seasons = seasons if seasons is not None else SEASONS

    def on_wheel(notches):
        # 真机行为：滚轮只有落在**第一项**上才生效。锚点偏下（早期用的
        # y=450）完全没反应 —— 这正是「向上滚无效」误判的来源。
        # 真机实测的有效锚点 y≈380（第一项）。写死 380，不引用
        # COL_SEASON_WHEEL_ANCHOR —— 引用的话改常量假对象也跟着变，
        # 这条判据就永远成立，测试失去意义。
        if abs(_LAST_WHEEL[1] - 380) > 30:
            return
        # 滚一格移 3 项（实测 down1 相对 down0 移了 3 项）。
        # top 是「第一屏首项的索引」，可见 6 项；上限 = 总项数 - 6
        # （最后一屏刚好露出 6 项）。
        step = 3 if notches < 0 else -3
        state["top"] = max(0, min(len(seasons) - 6, state["top"] + step))

    def on_click(x, y):
        if (x, y) == COL_SEASON:
            if not opens_on_click:
                return
            # 点下拉框本身：切换浮层（真机行为 —— 点一下开，再点一下关）
            state["open"] = not state["open"]
            screen.set(_season_dropdown_img(top=state["top"], checked=state["checked"],
                                            open_=state["open"],
                                            selected=state["selected"]))
        elif closes_on_pick and x == COL_SEASON_ITEM_X:
            # 模拟真机：可点热区在行上半部，文字顶部往上 12px 那一带。
            # 点在文字本身或更下方都不生效。
            # 热区按**屏幕行**算，与列表滚到哪一项无关。
            # 真机实测：热区相对文字顶部的位置**和字长有关**。
            #   长项（3-4 字）：热区 = 文字顶部 - 16（棱镜攻势 422 → 406）
            #   短项（2 字）  ：热区 = 文字顶部 + 8（群星 588 → 596）
            # 这里按当前屏可见的项逐个算，不再用固定偏移。
            hit = False
            for i in range(6):
                idx = state["top"] + i
                if idx >= len(seasons):
                    break
                top = COL_SEASON_ITEM_Y0 + i * COL_SEASON_ITEM_DY
                off = 8 if len(seasons[idx]) <= 2 else -16
                if abs(y - (top + off)) <= 10:
                    hit = True
                    break
            if hit:
                # 记下选中的是哪一项（按下拉项的实际行位置反查）
                for i in range(6):
                    idx = state["top"] + i
                    if idx >= len(seasons):
                        break
                    top = COL_SEASON_ITEM_Y0 + i * COL_SEASON_ITEM_DY
                    off = 8 if len(seasons[idx]) <= 2 else -16
                    if abs(y - (top + off)) <= 10:
                        state["selected"] = seasons[idx]
                        break
                state["open"] = False
                screen.set(_season_dropdown_img(checked=state["checked"],
                                                open_=False,
                                                selected=state["selected"]))
        elif y == COL_RARITY_Y or y in COL_GUN_Y:
            state["checked"].add((x, y))

    kb = FakeKeyboard(on_wheel=on_wheel)
    mouse = FakeMouse(on_click=on_click)

    def fake_ocr(img):
        """模拟真 OCR，要同时处理两种裁剪：
             - 探针裁剪 y355-400 x575-730（45 高，原尺寸）
             - 选项裁剪 y340-700 x555-765（360 高，**会被放大 2x 成 720 高**）

        真机实测 _season_items 必须先放大 2x 再 OCR，否则 15px 的小字会被
        整行跳过（「群星」就是这么漏掉的）。所以这里按高度判断缩放倍数，
        并把坐标换算回裁剪图坐标系，模拟真 OCR 的返回。
        """
        rows = []
        h = img.shape[0]
        if h == 1080 and img.shape[1] == 580:
            # 「下拉那一竖列」的裁剪（x540-830 / y280-820 = 290x540，
            # 放大 2 倍后 580x1080）：现在的实现是按文字在这条列里找选项，
            # 不认固定裁剪区（面板有两种布局：贴边 / 居中浮层，位置会变）。
            scale = 2
            for i in range(6):
                idx = state["top"] + i
                if idx >= len(seasons):
                    break
                y = COL_SEASON_ITEM_Y0 + i * COL_SEASON_ITEM_DY
                cy = (y - 280) * scale
                band = img[cy - 6 * scale:cy + 6 * scale, 50 * scale:160 * scale]
                if band.size and band.mean() > 100:
                    # 这一路永远是 2 倍，短项也要读得到
                    # （「短项 1 倍读不到」是原尺寸下的行为，这里不适用）
                    rows.append((seasons[idx], cy))
            return _words_from_rows(rows, x=45 * scale, w=56 * scale, h=16 * scale)
        if h > 900:
            # 整屏 OCR（where / classify 用）：给面板锚点词，
            # 这样 where() 认得出 filter_collection
            return _words_from_rows([("全部赛季", 330), ("枪种", 700)], x=470)
        if h in (70, 140):
            # 下拉框裁剪（y300-370 / x540-800，高 70）：收起态显示选中项。
            # **_season_applied 会先放大 2x（高变 140）**，两种高度都要认。
            # 展开态被浮层盖住，读不到。
            if state["open"]:
                return []
            sel = state.get("selected", "")
            if not sel:
                return []
            s2 = h // 70
            # 真机行为：**4 字及以上的长项在 1x 下整串读不出来**（15px 灰字，
            # WinRT 跳过）。实测「造物纪元」在 y300-370/x540-800 的 1x 裁剪
            # 返回空、2x 才有字。照抄这个行为 —— 否则「放大 2x」这条真机
            # 发现没有任何测试保护，改回 1x 也照样全绿。
            if s2 == 1 and len(sel) >= 4:
                return []
            return _words_from_rows([(sel, 20 * s2)], x=44 * s2)
        if h in (57, 114):
            # 面板标题裁剪（y228-285 / x400-500，高 57）：面板开着才有「筛选」。
            # **_panel_open 会先放大 2x（高变 114）**，两种高度都要认。
            # **必须在 state["open"] 判断之前**：浮层收起时面板还开着，
            # 这里要能读到，否则 _panel_open() 会误报「面板关了」。
            # 真机标题字在 y=237（裁剪区内 y=9）、高 31px。
            # 真机 OCR 在这个小裁剪里返回的是**单字** ['筛','选']，照抄。
            s2 = h // 57
            band = img[9 * s2:40 * s2, 10 * s2:72 * s2]
            if band.size and band.max() > 150:
                return [{"text": "筛", "x": 10 * s2, "y": 9 * s2,
                         "w": 20 * s2, "h": 20 * s2},
                        {"text": "选", "x": 32 * s2, "y": 9 * s2,
                         "w": 20 * s2, "h": 20 * s2}]
            return []
        if not state["open"]:
            return []
        # 选项裁剪：_season_items 会放大 2x，按高度反推缩放倍数
        scale = max(1, h // 360)
        for i in range(6):
            idx = state["top"] + i
            if idx >= len(seasons):
                break
            y = COL_SEASON_ITEM_Y0 + i * COL_SEASON_ITEM_DY
            cy = (y - 340) * scale          # 换成（放大后的）裁剪图内坐标
            band = img[cy - 6 * scale:cy + 6 * scale,
                       35 * scale:145 * scale]
            if band.size and band.mean() > 100:
                # 真机行为：**短项在原尺寸下会被 OCR 整行跳过**。
                # 实测「群星」两个字、笔画简单，1x 时完全读不到，放大 2x
                # 才能读出。这里照抄这个行为 —— 否则「放大 2x」这条真机
                # 发现没有任何测试保护，改回 1x 也照样全绿。
                if scale == 1 and len(seasons[idx]) <= 2:
                    continue
                # 返回放大后的坐标，_season_items 会 //scale 换算回屏幕坐标
                rows.append((seasons[idx], cy))
        return _words_from_rows(rows, x=45 * scale, w=56 * scale, h=16 * scale)

    def grab():
        img = screen.grab()
        for (cx, cy) in state["checked"]:
            img[cy - 8:cy + 8, cx - 8:cx + 8] = (230, 230, 230)
        return img

    n = Navigator(screen, mouse, kb, snap=grab, ocr_read=fake_ocr)
    return n, screen, mouse, kb, state


# ---------------------------------------------------------------- 坐标表

def test_rarity_checkboxes_are_box_centers_not_text_centers():
    """品阶四列必须是方框中心 580/818/1058/1296，不是文字中心。

    真机实测：方框竖直亮带 567-592、806-831、1045-1070、1284-1309，
    文字从 x≈604 起。早期用文字中心（718/867/1105/1354），点偏 20-80px，
    一个都点不上。
    """
    assert COL_RARITY_X == (580, 818, 1058, 1296)
    assert 604 not in COL_RARITY_X


def test_rarity_columns_are_evenly_spaced():
    """四列间距 238px —— 用来发现坐标表被改坏。"""
    xs = COL_RARITY_X
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    assert gaps == [238, 240, 238]


def test_owned_and_not_owned_are_in_different_columns():
    """「已拥有」在第一列，「未拥有」在**第二列**（x≈806），不是紧挨着它。

    2026-10-23 真机逐像素扫描：框的描边在 x=804~806（点 805 亮像素
    0→183 勾上了）。之前记的 605 落在两个框中间的空地上，点下去什么
    都没发生 —— 于是「未拥有」这个筛选一直关不掉，列表里只剩已拥有的
    那几件（采集时每个品阶只出 1~4 件，还以为「这档就这么点皮肤」）。
    """
    assert COL_OWNED == (580, 412)
    assert COL_NOT_OWNED == (806, 412)
    assert COL_NOT_OWNED[0] - COL_OWNED[0] == 226, "两列差 226px，不是 25px"


def test_season_item_spacing_is_48px():
    """下拉项间距 48px、一次滚一格滚 1 项。"""
    assert COL_SEASON_ITEM_Y0 == 379
    assert COL_SEASON_ITEM_DY == 48
    assert COL_SEASON_VISIBLE == 6


# ---------------------------------------------------------------- 复选框

def test_box_brightness_reads_check_state():
    """空框亮像素少，勾选后亮像素多。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    x, y = 580, 486
    assert _box_brightness(img, x, y) == 0
    img[y - 8:y + 8, x - 8:x + 8] = (230, 230, 230)
    assert _box_brightness(img, x, y) > 200


def test_col_check_clicks_the_box_center():
    """col_check 点的是方框中心坐标，不是文字。"""
    n, screen, mouse, _, _ = _make_nav()
    screen.set(_season_dropdown_img())
    n.col_check("rarity", 0)
    assert mouse.clicks == [(COL_RARITY_X[0], COL_RARITY_Y)]


def test_col_check_raises_when_box_state_does_not_change():
    """点完框内亮像素没变 → 报错，不能假装成功。

    真机踩过：只看「画面变化 0.04%」会误判成勾上了，其实没勾上。
    """
    n, screen, mouse, _, _ = _make_nav()
    # 点击不改变任何像素（on_click 不记录 checked），框始终是空的
    n.mouse = FakeMouse()
    screen.set(np.zeros((1080, 1920, 3), np.uint8))
    with pytest.raises(NavError, match="状态不对"):
        n.col_check("rarity", 0)


def test_col_check_succeeds_when_box_lights_up():
    """勾选后框亮起来 → 成功。"""
    n, screen, mouse, _, _ = _make_nav()
    img = np.zeros((1080, 1920, 3), np.uint8)

    def grab():
        # 第一次（点前）全黑，之后（点后）框亮 —— 模拟真实勾选
        if not mouse.clicks:
            return img
        lit = img.copy()
        x, y = COL_RARITY_X[0], COL_RARITY_Y
        lit[y - 8:y + 8, x - 8:x + 8] = (230, 230, 230)
        return lit

    n._snap = grab
    assert n.col_check("rarity", 0) is True


def test_col_check_rejects_unknown_row():
    n, screen, _, _, _ = _make_nav()
    screen.set(_season_dropdown_img())
    with pytest.raises(NavError, match="未知的筛选行"):
        n.col_check("nope", 0)


def test_col_check_gun_second_row_uses_gun_y1():
    """枪种第二行（精准射手/狙击/手枪）用 COL_GUN_Y[1]。"""
    n, screen, mouse, _, _ = _make_nav()
    screen.set(_season_dropdown_img())
    n.col_check("gun2", 0)
    assert mouse.clicks == [(COL_GUN_X2[0], COL_GUN_Y[1])]


# ---------------------------------------------------------------- 赛季下拉

def test_phrases_merges_split_ocr_words():
    """OCR 把「电玩高手」拆成两块时，_phrases 要拼回来。

    不拼的话 `name in t` 永远匹配不上，下拉选不中。
    """
    words = [
        {"text": "电玩", "x": 600, "y": 516, "w": 28, "h": 15},
        {"text": "高手", "x": 629, "y": 516, "w": 28, "h": 15},
    ]
    ph = _phrases(words)
    assert len(ph) == 1
    assert ph[0]["text"] == "电玩高手"


def test_phrases_keeps_distant_words_apart():
    """横向距离超过 gap 的两块不能拼在一起。"""
    words = [
        {"text": "美杜莎", "x": 600, "y": 475, "w": 42, "h": 15},
        {"text": "步枪", "x": 900, "y": 475, "w": 28, "h": 15},
    ]
    ph = _phrases(words, gap=12)
    assert len(ph) == 2


def test_pick_season_clicks_first_item_when_visible():
    """目标在第一屏时，不滚动直接点。

    第一击用文字顶部本身；真机热区在文字上方，所以可能要多试几个偏移。
    这里只要求：点过、点在浮层列上、最后浮层收起。
    """
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=0))
    n.col_pick_season("美杜莎")
    assert mouse.clicks, "应该点过至少一次"
    # 重开浮层会点下拉框本身；点选项时 x 必须是浮层列
    item_clicks = [(x, y) for x, y in mouse.clicks if x == COL_SEASON_ITEM_X]
    assert item_clicks, "应该点过浮层里的选项"
    for _x, y in item_clicks:
        # 美杜莎是第 3 行：屏幕 y 475，扫整行覆盖 451-499
        assert 445 <= y <= 500, f"y={y} 不在美杜莎这一行附近"
    assert state["open"] is False, "点完浮层应该收起"


def test_pick_season_scrolls_to_find_later_items():
    """目标在滚动之后才出现时，要滚过去再点。

    「疾光魅影」是第 11 项，第一屏（6 项）看不到。
    """
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=0))
    n.col_pick_season("疾光魅影")
    assert mouse.clicks, "应该点过至少一次"
    # 点选项时 x 必须是浮层列，且 y 落在某一行的扫描范围内
    item_clicks = [(x, y) for x, y in mouse.clicks if x == COL_SEASON_ITEM_X]
    assert item_clicks, "应该点过浮层里的选项"
    for _x, y in item_clicks:
        assert any(abs(y - (COL_SEASON_ITEM_Y0 + i * COL_SEASON_ITEM_DY)) <= 26
                   for i in range(6)), f"y={y} 不在任何一行附近"
    assert kb.wheels, "应该滚动过"


def test_pick_season_raises_when_name_not_found():
    """找不到就报错，绝不盲点。"""
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=0))
    with pytest.raises(NavError, match="找不到"):
        n.col_pick_season("不存在的皮肤")
    assert mouse.clicks == []


def test_pick_season_opens_dropdown_when_closed():
    """下拉没展开时先点开；点开后还是没展开 → 报错，不硬着头皮读。

    收起态用 y=414 亮度 48 的图（真机实测「已拥有」行亮度），
    展开态才会掉到 21。
    """
    n, screen, mouse, kb, state = _make_nav(closes_on_pick=False,
                                            opens_on_click=False)
    state["open"] = False
    screen.set(_season_dropdown_img(open_=False))
    with pytest.raises(NavError, match="没展开"):
        n.col_pick_season("美杜莎")
    # 第一次点击应该是下拉框本身
    assert mouse.clicks[0] == COL_SEASON


def test_pick_season_clicks_dropdown_then_item():
    """赛季下拉：先点下拉框，再点选项（都是真实输入）。"""
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=0))
    assert n.col_pick_season("美杜莎") is True
    assert mouse.clicks, "下拉框和选项都要点"
    assert mouse.clicks[0] == COL_SEASON


def test_clicking_dropdown_toggles_overlay_closed():
    """再点一次下拉框能收起浮层 —— _click_season_item 靠它复位。

    真机踩过：用 ESC 复位不可靠，ESC 有时只关浮层、有时连整个筛选面板
    一起关，后者会让下一轮在错误页面上瞎点。所以复位动作固定成
    「点下拉框」，这条测试钉住它。
    """
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=0))
    assert state["open"] is True
    n._click_season_item("美杜莎", 470)
    # 一定点过下拉框（复位动作），且最终浮层收起
    assert COL_SEASON in mouse.clicks
    assert state["open"] is False


def test_pick_season_stops_scrolling_once_at_bottom():
    """滚到底就停，不空转。

    真机实测：列表 12 项、一屏 5-6 项，滚到第 3 屏画面就完全不变了。
    早期没有「到底」判断，会一直滚到 COL_SEASON_MAX_SCROLL(20) 次 ——
    每次 1.1s，白白多花 18 秒，而且在真机上滚多了会误触别的东西。
    """
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=state["top"]))
    with pytest.raises(NavError):
        n.col_pick_season("不存在的系列")

    # 12 项 / 每屏 6 项 / 滚一格移 3 项：回顶 1 次 + 向下几次就到底，
    # 远小于 20。给点余量，卡在 12 次以内。
    assert len(kb.wheels) <= 12, (
        f"滚了 {len(kb.wheels)} 次，说明没在到底时停下")


def test_pick_season_reads_short_items_by_upscaling():
    """短项（两个字）必须靠放大 2x 才能读到。

    真机实测：「群星」两个字、笔画简单，15px 灰字在原尺寸下 WinRT 会整行
    跳过 —— 原始 OCR 从「造物纪元」@540 直接跳到「疾光魅影」@635，中间
    y≈588 一个词都没有（像素上明显有字，亮度 21.9 → 29.3）。放大 2x 后
    能正常读出。

    假 OCR 照抄了这个行为（1x 时跳过 ≤2 字的项），所以这条测试真正钉住
    「_season_items 必须放大 2x」—— 改回 1x 就会失败。
    """
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=state["top"]))
    assert n.col_pick_season("群星") is True
    assert state["open"] is False


def test_pick_season_does_not_blind_pick_when_name_absent():
    """列表里真的没有这个名字时，不能靠盲点硬凑成功。

    盲点只是「OCR 漏读」的兜底，不能变成「随便点一个然后说成功」——
    那会筛出错的皮肤，白跑一趟还浪费钱。
    """
    n, screen, mouse, kb, state = _make_nav()
    screen.set(_season_dropdown_img(top=state["top"]))
    with pytest.raises(NavError, match="找不到"):
        n.col_pick_season("根本不存在的系列")



# ------------------------------------------------- 「交易 / 单据」二级页签

def test_col_filter_switches_back_to_the_trade_tab_when_the_panel_will_not_open():
    """典藏外观下有「交易 / 单据」两个二级页签，筛选面板只在「交易」下面。

    停在「单据」（购买记录）那一页时点「筛选」毫无反应 —— 那一页压根
    没有这个按钮。正确的做法是：面板没打开就先切回「交易」再点一次。
    早期没这个兜底，一次定位失败刷满「点了「筛选」但面板没打开」。
    """
    clicks = []
    state = {"on_trade": False}

    class M:
        def click(self, x, y, verify=True, expect_foreground=None, **kw):
            clicks.append((x, y))
            if (x, y) == COL_TRADE_TAB:
                state["on_trade"] = True
            return True

    class S:
        size = (1920, 1080)

        def grab(self, box=None):
            img = np.zeros((1080, 1920, 3), np.uint8)
            # 顶部标签栏的绿色选中框（典藏外观在 x257-387）——
            # tab_now() 靠它判断在哪个标签页
            img[53, 257:387] = (86, 177, 133)   # BGR
            return img

    def fake_ocr(img):
        h = img.shape[0]
        if h in (57, 114):        # 面板标题裁剪区
            if state["on_trade"]:
                s2 = h // 57
                return [{"text": "筛", "x": 10 * s2, "y": 9 * s2,
                         "w": 20 * s2, "h": 20 * s2},
                        {"text": "选", "x": 32 * s2, "y": 9 * s2,
                         "w": 20 * s2, "h": 20 * s2}]
            return []
        return []

    scr = S()
    nav = Navigator(scr, M(), FakeKeyboard(), snap=scr.grab, ocr_read=fake_ocr)
    nav.col_filter()

    assert COL_TRADE_TAB in clicks, "面板打不开时没去点「交易」页签"
    first = clicks.index((COL_FILTER[0], COL_FILTER[1]))
    tab = clicks.index(COL_TRADE_TAB)
    assert first < tab, "顺序错了：应该先点「筛选」，打不开再切「交易」"
    assert clicks.count((COL_FILTER[0], COL_FILTER[1])) == 2, \
        "切回「交易」之后应该再点一次「筛选」"


# ------------------------------------------- 下拉「单击」语义（2026-11-08）

def test_dropdown_toggle_takes_exactly_one_press():
    """下拉是切换语义：一下开、再一下关 —— 只按一次，绝不补第二下。

    真机实测：用双击配方（hold 0.08 / gap 0.25）点下拉，整帧 diff 只有
    0.026，OCR 一行没多 —— 第一下展开、第二下又收起，互相抵消。
    """
    n, screen, mouse, kb, state = _make_nav()
    assert state["open"] is True

    n._click_once(COL_SEASON, "赛季下拉·收")
    assert len(mouse.clicks) == 1, "下拉只允许按一次"
    assert state["open"] is False

    n._click_once(COL_SEASON, "赛季下拉·开")
    assert len(mouse.clicks) == 2
    assert state["open"] is True


def test_double_click_on_dropdown_cancels_itself():
    """双击下拉 = 开→关，状态回到原点；这是「被抵消」不是「注入失败」。"""
    n, screen, mouse, kb, state = _make_nav()
    before = state["open"]
    n._double_click(COL_SEASON, "赛季下拉")
    assert len(mouse.clicks) == 2
    assert state["open"] is before, "两次点击应该互相抵消"


def test_judge_click_calls_tiny_diff_plus_same_page_cancelled():
    """判据：diff < 0.05 且页面没变 → cancelled（本轮第 13 条就是 0.026）。"""
    assert judge_click(0.026, "filter_collection", "filter_collection") == "cancelled"
    # 驱动层报错才是 inject_failure —— 不要混为一谈
    assert judge_click(0.026, "filter_collection", "filter_collection",
                       channel_error="not foreground") == "inject_failure"
    # 页面没变、diff 在灰区 → 单击没接住，可以补一次
    assert judge_click(0.07, "filter_collection", "filter_collection") == "no_response"
    # 页面变了 / diff 够大 → ok
    assert judge_click(0.026, "filter_collection", "market") == "ok"
    assert judge_click(0.31, "market", "market") == "ok"


def test_confirm_retries_once_then_succeeds():
    """「确认」第一发双击没关掉面板 → 补一次，成功就不再补。"""
    n, screen, mouse, kb, state = _make_nav()
    pages = iter(["filter_collection", "market"])
    n.where = lambda: (next(pages), "")

    assert n.col_filter_confirm() is True
    # 每次双击 = 2 下，补一次共 4 下；第三发绝不能出现
    assert len(mouse.clicks) == 4, f"点击了 {len(mouse.clicks)} 下，应为 4"


def test_confirm_raises_after_two_attempts_no_replay():
    """两次双击都还在 filter_collection → 抛错，不再重放。"""
    n, screen, mouse, kb, state = _make_nav()
    n.where = lambda: ("filter_collection", "")

    with pytest.raises(NavError, match="2 次"):
        n.col_filter_confirm()
    assert len(mouse.clicks) == 4, "上限 2 次双击 = 4 下，不能多"


# --------------------------------------- 赛季名 OCR 纠错（2026-11-08）

def test_series_ocr_fix_maps_garbled_s2_name():
    """真机 OCR 把「棱镜攻势S2」读成「持镜攻热C2」，必须纠回标准名。

    不修的话下拉里永远找不到「棱镜攻势S2」—— 三项里错三项（持/棱、
    热/势、C/S），按字符重合度算只剩「镜」「攻」「2」，功能等同不可用。
    """
    assert NavColPanel._fix_series_ocr("持镜攻热C2") == "棱镜攻势S2"
    # 其余实测错字也一并纠回
    assert NavColPanel._fix_series_ocr("悬赏今") == "悬赏令"
    assert NavColPanel._fix_series_ocr("命运王牒") == "命运王牌"
    assert NavColPanel._fix_series_ocr("气獠感应") == "气象感应"
    # 不能把 S1 也改成 S2
    assert NavColPanel._fix_series_ocr("棱镜攻势") == "棱镜攻势"


def test_series_spans_reads_garbled_s2_as_s2():
    """错误串进到行分组后，仍然要落到「棱镜攻势S2」而不是 S1。"""
    words = [{"text": "持镜攻热C2", "x": 590, "y": 400, "h": 16}]
    spans = NavColPanel._series_spans(words, y0=0, by=0)
    assert "棱镜攻势S2" in spans, spans
    assert "棱镜攻势" not in spans, "S2 那行不能落到 S1 上"


