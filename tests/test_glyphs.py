"""字形模板匹配：OCR 读不出的小字靠像素认名。

钉住四件事：

  1. 内置模板认得出来，而且认的是对的那个
  2. 不像的格子必须返回 None —— 宁可退回网格推断，也不能猜一个名字出去
  3. ons_options 的通道优先级：字形 > OCR > 网格查表
  4. 模板目录空掉时降级回网格，不崩

**几何（文字在方框右侧多少像素）不在单元测试里钉**：模板贴在哪儿由
`glyphs.search_rect()` 给出，测试跟着被测模块走，改偏移不用改测试。
偏移对不对只有真机说了算 —— 2026-10-21 在真机上验证过（跨滚动位置
分数 1.000、勾选态 0.978）。

模板是 core/assets/glyphs/*.png，从真机截图切出来的固定窗口，
不是 templates/ 下用户自己框选的购买按钮模板。
"""
import os

import numpy as np
import pytest

from core import glyphs, vision


def _tpl(name):
    """读内置模板（生产数据）。"""
    img = vision.imread(os.path.join(glyphs.dir_path(), name + ".png"))
    assert img is not None, f"缺少内置模板 {name}.png"
    return img


def _canvas(cx, cy, name=None, dx=0, dy=0):
    """造一张只有一块字的假面板图。

    字贴在 search_rect() 给出的窗口原点 + (dx, dy) —— dx/dy 模拟滚动
    带来的位移。背景纯黑，和真机面板底色（灰度 18）同量级。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    if name is None:
        return img
    tpl = _tpl(name)
    x, y, _w, _h = glyphs.search_rect(cx, cy)
    img[y + dy:y + dy + tpl.shape[0], x + dx:x + dx + tpl.shape[1]] = tpl
    return img


# ---------------------------------------------------------------- 识别本身

@pytest.mark.parametrize("name", ["极品", "优品", "S", "A", "B", "C",
                                  "其他", "镭射贵金属", "速度线"])
def test_identify_recognizes_each_shipped_template(name):
    """每个内置模板贴回面板都能认出自己。

    这 9 个正是 OCR 读不出的：稀有度「极品/优品」、成色 S/A/B/C、
    材质「其他」。它们原来只能靠网格位置反推。
    """
    got, score, _gap = glyphs.identify(_canvas(1641, 340, name), 1641, 340)
    assert got == name, f"认成 {got!r}，分数 {score:.3f}"
    assert score > 0.99


def test_identify_tolerates_few_pixel_drift():
    """滚动可能带来几像素位移，仍要认得出来。

    真机面板按整像素滚动（实测跨位置 1.000），但不同窗口缩放/DPI 下
    可能出现亚像素偏移，搜索窗口留了 PAD 余量就是为这个。
    """
    got, _score, _gap = glyphs.identify(_canvas(1396, 515, "S", dx=3, dy=2),
                                        1396, 515)
    assert got == "S"


def test_identify_returns_none_on_blank_region():
    """空白格子返回 None —— 不能凭「最高分」硬认一个名字。

    没有这一条，颜色区那些没模板的格子会被认成某个小字，然后点错选项。
    """
    got, _score, _gap = glyphs.identify(_canvas(1396, 745), 1396, 745)
    assert got is None


def test_identify_returns_none_on_noise():
    """噪声（画面撕裂、特效遮挡）不能被认成某个选项。"""
    img = np.random.RandomState(0).randint(0, 255, (1080, 1920, 3), np.uint8)
    got, _score, _gap = glyphs.identify(img, 1396, 745)
    assert got is None


def test_identify_respects_score_threshold(monkeypatch):
    """分数门槛真的生效：把门槛抬到不可能达到的高度，完全一致的模板也拒。

    没有这条，「阈值被删掉/写成 0」不会有任何测试变红 —— 而阈值是
    「不猜名字」这条契约的唯一执行者。
    """
    img = _canvas(1641, 340, "极品")
    assert glyphs.identify(img, 1641, 340)[0] == "极品"
    monkeypatch.setattr(glyphs, "MIN_SCORE", 1.01)
    assert glyphs.identify(img, 1641, 340)[0] is None


def test_identify_respects_gap_threshold(monkeypatch):
    """最佳与次优必须拉开差距，否则宁可放弃。

    「镭射」「镭射贵金属」是前缀关系，低分时容易互相抢。门槛失效就会
    在这种地方认错。
    """
    img = _canvas(1396, 857, "镭射")
    assert glyphs.identify(img, 1396, 857)[0] == "镭射"
    monkeypatch.setattr(glyphs, "MIN_GAP", 1.01)
    assert glyphs.identify(img, 1396, 857)[0] is None


def test_identify_handles_out_of_bounds():
    """方框贴到屏幕边缘时不能抛异常，只返回 None。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    assert glyphs.identify(img, 1900, 340)[0] is None
    assert glyphs.identify(img, 10, 2)[0] is None
    assert glyphs.identify(None, 1396, 745)[0] is None


# ---------------------------------------------------------------- 接进面板

class FakePanel:
    """假面板：真机上「29x29 方框 + 右边一行字」的最小复刻。

    方框坐标写死成真机实测值（左列 x=1396、右列 x=1641），**不引用
    nav 的常量** —— 引用的话改坏常量测试也照样过，判据就失效了。
    """

    def __init__(self, boxes, glyphs_at=(), ocr_text=()):
        # boxes: [(cx, cy)]
        self.boxes = list(boxes)
        self.glyphs_at = dict(glyphs_at)      # {(cx,cy): 模板名}
        self.ocr_text = dict(ocr_text)        # {(cx,cy): OCR 读出的字}

    def _snap(self):
        img = np.zeros((1080, 1920, 3), np.uint8)
        for cx, cy in self.boxes:
            x0, y0 = cx - 14, cy - 14
            img[y0:y0 + 29, x0:x0 + 29] = 100
            img[y0 + 2:y0 + 27, x0 + 2:x0 + 27] = 30
        for (cx, cy), name in self.glyphs_at.items():
            tpl = _tpl(name)
            x, y, _w, _h = glyphs.search_rect(cx, cy)
            img[y:y + tpl.shape[0], x:x + tpl.shape[1]] = tpl
        return img

    def _ocr_words(self, crop):
        # crop 是 img[30:1080, 1360:1900]，换算回整屏坐标
        out = []
        for (cx, cy), text in self.ocr_text.items():
            out.append({"x": cx + 40 - 1360, "y": cy - 30 - 8,
                        "w": 30, "h": 16, "text": text})
        return out


def _nav(panel):
    from core.nav import Navigator
    n = Navigator.__new__(Navigator)
    n._snap = panel._snap
    n._ocr_words = panel._ocr_words
    n.mouse = None
    n.pt = lambda point: point
    n.say = lambda msg, level="info": None
    # 只桩 _ons_wheel：真实现会调 ctypes 移动真实鼠标、滚动游戏窗口。
    n._ons_wheel = lambda notches, at=None: True
    return n


def test_panel_prefers_glyph_over_ocr():
    """字形和 OCR 冲突时用字形 —— 有模板就是像素证据，比 14px 小字的 OCR 可信。

    真机场景：OCR 偶尔把「极品」读成「优品」，读错就会点错档位。
    """
    p = FakePanel([(1396, 340)], glyphs_at={(1396, 340): "极品"},
                  ocr_text={(1396, 340): "优品"})
    o = _nav(p).ons_options(scroll_top=False)[0]
    assert o["name"] == "极品"
    assert o["src"] == "glyph"
    assert o["ocr"] == "优品"          # 冲突被记录下来，不是被吞掉


def test_panel_uses_ocr_when_no_template():
    """没有模板的选项（颜色区）仍然走 OCR。"""
    p = FakePanel([(1396, 745)], ocr_text={(1396, 745): "速度线"})
    o = _nav(p).ons_options(scroll_top=False)[0]
    assert o["name"] == "速度线"
    assert o["src"] == "ocr"


def test_panel_falls_back_to_grid_table():
    """没有模板、OCR 也没字 → 最后才用网格查表，并标出来源。"""
    p = FakePanel([(1396, 340)])        # 空白格子
    o = _nav(p).ons_options(scroll_top=False)[0]
    assert o["src"] == "grid"
    assert o["name"]                      # 表里能查到名字


def test_panel_reports_grid_conflict_with_glyph():
    """字形认出的名字和网格查表不一致时，两个值都要能取到。

    这是排查「滚动后位置推断跑偏」的唯一信号：网格说 A、字形说 B，
    上层据此能发现推断错了，而不是默默按错的名字去点。
    """
    p = FakePanel([(1396, 340)], glyphs_at={(1396, 340): "极品"})
    o = _nav(p).ons_options(scroll_top=False)[0]
    assert o["name"] == "极品"
    assert o["table"] not in (None, "极品")


def test_panel_survives_missing_templates(tmp_path, monkeypatch):
    """模板目录空掉时降级回网格，不崩、不少项。

    分发时漏打包 core/assets 就是这个场景。少了它，整个筛选面板读取
    会抛异常，抢购链路直接断。
    """
    monkeypatch.setattr(glyphs, "_DIR", str(tmp_path))
    glyphs.load(force=True)
    try:
        assert glyphs.load() == {}
        p = FakePanel([(1396, 340), (1396, 515)])
        opts = _nav(p).ons_options(scroll_top=False)
        assert len(opts) == 2
        assert all(o["src"] == "grid" for o in opts)
    finally:
        monkeypatch.undo()
        glyphs.load(force=True)


def test_shipped_templates_are_all_usable():
    """内置模板必须尺寸一致 —— 尺寸不对的会被 load() 静默跳过。

    真机上表现是「某个选项突然认不出」，很难查。这里提前拦住。
    """
    tpls = glyphs.load(force=True)
    assert len(tpls) >= 9, f"只读到 {len(tpls)} 个模板"
    for name, t in tpls.items():
        assert t.shape == (glyphs.WIN_H, glyphs.WIN_W), \
            f"{name} 尺寸 {t.shape} 不是 ({glyphs.WIN_H}, {glyphs.WIN_W})"
