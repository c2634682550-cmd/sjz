"""典藏外观页 + 两套筛选面板的**真截图**识别测试。

为什么要有这个文件
------------------
这些链路（筛选 → 滚动 → OCR 列表 → 点卡片 → 读标题）以前只有「假 OCR +
假屏幕」的坐标契约测试：它们能证明「点击落在第 3 个像素」是对的，但证明
不了「真图上认不认得出 MK4冲锋枪-疾光魅影」。2026-11-01 换 OCR 引擎
（Windows OCR → PP-OCRv6）时，坐标契约全绿，真图却没人验过 —— 这里补上。

素材是 `_shots/` 下的 1920×1080 真机截图（窗口化，和 nav_coords 的标定
同分辨率，坐标不用换算）。每条断言都是肉眼能核对的事实，不是构造值。

跑法：.venv\\Scripts\\python.exe -m pytest tests/test_skin_ocr_real.py -v
（需要 PP-OCR 服务起着，见 说明.txt 第九节；没起会整组 skip）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vision  # noqa: E402
from core.nav import Navigator  # noqa: E402
from core.nav_coords import NavError  # noqa: E402

SHOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "_shots")


# ---------------------------------------------------------------- 假外设

class _Screen:
    def __init__(self, img):
        self.size = (img.shape[1], img.shape[0])
        self._img = img

    def grab(self, box=None):
        return self._img


class _Mouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y, **kw):
        self.clicks.append((int(x), int(y)))
        return True


class _Keyboard:
    def __init__(self):
        self.wheels = []

    def wheel(self, x, y, notches, **kw):
        self.wheels.append((x, y, notches))
        return True

    def tap(self, key, **kw):
        return True


def _nav(shot):
    """用一张真截图搭一个 Navigator。ocr_read 不注入 —— 走真 OCR。"""
    path = os.path.join(SHOTS, shot)
    img = vision.imread(path)
    if img is None:
        pytest.skip(f"缺截图 {shot}")
    return Navigator(_Screen(img), _Mouse(), _Keyboard())


# ---------------------------------------------------------------- 皮肤列表

def test_skin_items_reads_every_card_on_screen():
    """一屏 7 张卡，名字要全对（含枪种和破折号后半段）。

    真机这一屏是「夜穹 / 叠锋 / 幻夜」三组，每组按枪种排列。名字读不全
    就匹配不上 —— 早期 Windows OCR 会把「·」全读成「一」，整名相似度
    掉到 0.5 以下。
    """
    nav = _nav("drv_sel.png")
    names = [t for t, _y in nav.skin_items()]
    assert names == [
        "AS Val突击步枪-夜穹", "M16A4突击步枪-夜穹", "腾龙突击步枪-夜穹",
        "QBZ95-1突击步枪-叠锋", "PTR-32突击步枪-幻夜",
        "SCAR-H战斗步枪-幻夜", "G3战斗步枪-叠锋",
    ]


def test_skin_items_click_y_lands_on_card_centers():
    """点击 y 要均匀落在卡片中部，不能落在名字那一行上。

    实测教训：点文字 y=259 完全没响应，卡片中部才选中（文字中心 + 40）。
    卡片高度固定，所以相邻两项的 y 间距应该稳定在 114px 左右。
    """
    nav = _nav("drv_sel.png")
    ys = [y for _t, y in nav.skin_items()]
    assert len(ys) == 7
    assert ys == sorted(ys), "y 必须按从上到下排"
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert all(110 <= g <= 118 for g in gaps), f"卡片间距不稳：{gaps}"
    assert all(230 < y < 1010 for y in ys), "点击点跑出列表区域了"


def test_skin_title_reads_the_full_name():
    """右侧标题要读全名，且**不带左边那个品阶图标**。

    2026-11-02 修：标题框左边界从 470 收到 505。原来会把菱形图标一起
    框进去，OCR 读成「中」，标题变成「中 MK4冲锋枪-疾光魅影」。
    """
    nav = _nav("drv_after_filter.png")
    assert nav.skin_title() == "MK4冲锋枪-疾光魅影"


def test_skin_title_works_on_a_single_card_result():
    """筛到只剩一张时标题照样要读对（这时列表只有一项，卡片本来已选中）。"""
    nav = _nav("cat_S1_0.png")
    assert nav.skin_title() == "M4A1突击步枪-棱镜攻势"


# ---------------------------------------------------------------- 定位目标

def test_locate_picks_the_exact_card():
    """给定完整皮肤名，要指到对应那张卡（不是第一张）。"""
    nav = _nav("drv_sel.png")
    hit = nav.locate_skin_card("QBZ95-1突击步枪-叠锋")
    assert hit is not None
    text, y, score = hit
    assert text == "QBZ95-1突击步枪-叠锋"
    assert score == pytest.approx(1.0)
    assert 630 < y < 655, f"y={y} 不在第 4 张卡上"


def test_locate_returns_none_when_the_skin_is_not_on_screen():
    """不在这一屏就返回 None —— 不能「最像的就当命中」。

    这条很重要：命中一个不相干的皮肤会点开它、读标题、然后按错的
    价格去抢。
    """
    nav = _nav("drv_sel.png")
    assert nav.locate_skin_card("AK-12突击步枪·水墨") is None


def test_rank_prefers_the_full_name_over_a_partial_match():
    """七张卡里只有一张叫「AS Val…-夜穹」，排序第一必须是它。

    用型号粗筛的意义就在这里：七张卡名字后半段全是「-夜穹」，
    只按相似度排会把 M16A4/腾龙 也排上来。
    """
    nav = _nav("drv_sel.png")
    ranked = nav.rank_skin_cards("AS Val突击步枪-夜穹")
    assert ranked[0][0] == "AS Val突击步枪-夜穹"
    assert ranked[0][2] == pytest.approx(1.0)


# ---------------------------------------------------------------- 筛选面板

def test_collection_filter_states_read_the_checkboxes():
    """三张截图分别对应「全没勾」「只勾普通」「勾了普通+已拥有+未拥有」。"""
    assert _nav("n_02_filter.png").col_filter_states()["tier"] == [False] * 4
    assert _nav("n_03_checked.png").col_filter_states()["tier"] == [False, False, False, True]

    st = _nav("drv_colfilter.png").col_filter_states()
    assert st["tier"] == [False, False, False, True]
    assert st["owned"] is True and st["not_owned"] is True


def test_market_filter_options_read_the_whole_panel():
    """在售筛选面板：稀有度 / 成色 / 材质三组共 14 项，名字和勾选态都要对。

    这组是「按材质颜色缩小范围」的唯一依据 —— 读错一个名字，
    后面就勾错一档，抢到的是另一件。
    """
    nav = _nav("drv_filter.png")
    opts = nav.ons_options(scroll_top=False)
    names = [o["name"] for o in opts]
    assert names == ["所有稀有度", "极品", "优品",
                     "所有成色", "S", "A", "B", "C",
                     "不限", "速度线", "镭射贵金属", "贵金属", "镭射", "其他"]
    on = [o["name"] for o in opts if o["on"]]
    assert on == ["所有稀有度", "所有成色", "不限"], f"默认勾选态不对：{on}"


# ---------------------------------------------------------------- 滚动找皮肤

def test_select_skin_scrolls_until_the_target_shows_up(monkeypatch):
    """目标不在第一屏时，select_skin 要滚下去找到它并停在它上面。

    用两帧假列表模拟「滚一屏」：第一屏七张都不是目标，第二屏第三张才是。
    断言：滚轮确实发了、点的是目标那张、返回的 y 是目标卡片的 y。

    真机上这条链路 2026-10-23 踩过两个坑，都在这里钉住：
      · 滚轮符号没翻 → 在列表顶部滚了 12 屏全在原地
      · 只按名字包含判断 → 七张同名不同枪种的卡全算命中
    """
    nav = _nav("drv_sel.png")
    first = [("AS Val突击步枪-夜穹", 300), ("M16A4突击步枪-夜穹", 414),
             ("腾龙突击步枪-夜穹", 528)]
    second = [("AKM突击步枪-群星", 300), ("M7战斗步枪-棱镜攻势S2", 414),
              ("M250通用机枪-电玩高手S2", 528)]
    frames = [first, second]
    calls = {"n": 0}

    def fake_items():
        return frames[min(calls["n"], len(frames) - 1)]

    def fake_scroll(notches):
        calls["n"] += 1          # 每滚一屏，换下一帧列表

    monkeypatch.setattr(nav, "skin_items", fake_items)
    monkeypatch.setattr(nav, "skin_scroll", fake_scroll)
    monkeypatch.setattr(nav, "skin_scroll_top", lambda *a, **k: None)
    monkeypatch.setattr(nav, "skin_title", lambda: "M250通用机枪-电玩高手S2")

    y = nav.select_skin(name="M250通用机枪-电玩高手S2")
    assert y == 528, f"点到了 y={y}，不是目标卡片"
    assert nav.mouse.clicks[-1] == (150, 528)
    assert calls["n"] >= 1, "根本没滚动就找到了，说明第一屏就命中了（不该）"


def test_select_skin_raises_when_never_found(monkeypatch):
    """滚到底也找不到要报错，不能返回一个「差不多」的位置。"""
    nav = _nav("drv_sel.png")
    monkeypatch.setattr(nav, "skin_items", lambda: [("AS Val突击步枪-夜穹", 300)])
    monkeypatch.setattr(nav, "skin_scroll", lambda n: None)
    monkeypatch.setattr(nav, "skin_scroll_top", lambda *a, **k: None)
    monkeypatch.setattr(nav, "skin_title", lambda: "AS Val突击步枪-夜穹")
    with pytest.raises(NavError):
        nav.select_skin(name="QBZ95-1突击步枪-叠锋")
