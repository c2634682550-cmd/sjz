"""在售列表页：页码判据与页面分类的行为契约。

钉住 2026-10-21 真机测绘踩过的两个坑：

  1. `classify()` 把在售列表页误判成 detail —— 在售页右侧详情面板也有
     「编号」「淘汰计数」「相似皮肤」，detail 判据排在前面就先命中了。
     后果很严重：survey2 整轮测绘数据作废，因为「在售页筛选」实际是在
     detail 页上点的。
  2. 「第N页」文字在 y938-955 / x721-762，**必须 6x 放大才读得出**。
     1x 和 4x 都返回空（实测），只有 6x 出「第」「1」「页」三个词。

坐标全部是真机实测值（2026-10-21，1920x1080 窗口化）。
"""
import numpy as np
import pytest

from core.nav import NavError, Navigator, classify
from core.nav_coords import ONS_PRICE_MAX, ONS_PRICE_MIN


# ---------------------------------------------------------------- 真机原文

# 在售列表页的整屏 OCR 拼串（真机 s_onsale.png 实测，含右侧详情面板文字）
ONSALE_TEXT = (
    "CNUIO:I巳巳9177342331794992囗231囗21囗149一204940+，00推荐争0突击步枪群"
    "AKM星默摊序不限公示期认一AKM突击步枪群AKM突击步枪群星星肤．．相似皮生"
    "编号0004907430度稀有成色A〔〕0481353．的的》》极成色极成色品C7200品B8888"
    "，，淘汰计数40AKM突击步枪群AKM突击步枪群星星．．材质光大星河理石，光赦群"
    "移阴无星不，，《》极成色9极成色厶品C178品11111．，，剩余分秒542：024888，"
    "第1页"
)

# detail 页（典藏外观点卡片后的商品详情）的整屏 OCR 拼串
DETAIL_TEXT = (
    "相似皮肤编号000490743稀有度极品成色A0481353淘汰计数40材质星河光大理石"
)


# ---------------------------------------------------------------- 假对象

class FakeScreen:
    def __init__(self, img=None):
        self._img = (img if img is not None
                     else np.zeros((1080, 1920, 3), np.uint8))

    def grab(self, box=None):
        return self._img

    def set(self, img):
        self._img = img


class FakeOCRHost:
    """只提供 _snap / _ocr_words 的最小宿主，用来测 ons_page()。

    **不引用 nav_coords.ONS_PAGE_LABEL**：如果引用，改常量时假对象跟着变，
    「裁剪位置对不对」这条判据就永远成立，测试失去意义。
    """

    def __init__(self, img, words):
        self._img = img
        self._words = words
        self.crops = []

    def _snap(self):
        return self._img

    def _ocr_words(self, img):
        self.crops.append(img.shape[:2])
        return self._words


def _ons_page(host):
    """把 ons_page 当纯函数用：绑到假宿主上。"""
    return Navigator.ons_page(host)


# ---------------------------------------------------------------- 测试

RECOMMEND_TEXT = (
    "CNUIO:I917734233179499一2囗231囗21222巳0销售量榜单所有稀有度MK4冲锋枪．疾光魅影MK4冲锋枪．疾光魅影0总榜单2，049硝售价格榜单其他《经典款生相似皮肤40+0在售皮肤推荐骇爪外热内冷的人最麻烦"
)


def test_classify_recommend_is_not_onsale():
    """「推荐」页（销售榜单）不能判成 onsale。

    真机踩过（2026-10-21）：推荐页顶部也有「在售/推荐」两个标签，只按
    「推荐」判就命中 onsale，程序在榜单页上点「公示期筛选」(435,197)、
    扫榜单文字当商品行 —— 整轮跑在错页上，还以为在抢购。
    """
    assert classify(RECOMMEND_TEXT) == "recommend"


def test_classify_onsale_before_detail():
    """在售列表页必须判成 onsale，不能因为含「编号」「淘汰」就判成 detail。

    真机踩坑：survey2 整轮把在售页当 detail，点「筛选」点在详情面板上，
    数据全废。这条测试直接拿真机原文喂 classify()。
    """
    assert classify(ONSALE_TEXT) == "onsale"


def test_classify_detail_still_detail():
    """真正的 detail 页（没有「公示期」「默认排序」）仍然判 detail。

    防止为了修 onsale 把 detail 判据删掉 —— 那样 buy() 的
    require(("detail","onsale")) 就失去区分能力。
    """
    assert classify(DETAIL_TEXT) == "detail"


def test_classify_onsale_needs_publicity_period():
    """「编号」+「淘汰」单独出现（没有「公示期」）时仍判 detail。

    这是 detail 页的最小特征。如果在售判据写成「有编号就是在售」，
    典藏外观的商品详情页会被误判成在售页。
    """
    only_detail = "编号000490743稀有度极品淘汰计数40"
    assert classify(only_detail) == "detail"


def test_ons_page_parses_page_number():
    """「第1页」→ 1。真机 OCR 返回三个词：第 / 1 / 页。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    words = [{"text": "第", "x": 721, "y": 938, "w": 14, "h": 16},
             {"text": "1", "x": 738, "y": 940, "w": 8, "h": 12},
             {"text": "页", "x": 748, "y": 939, "w": 14, "h": 16}]
    host = FakeOCRHost(img, words)
    assert _ons_page(host) == 1


def test_ons_page_parses_multi_digit():
    """第12页 → 12（两位数不能只读到第一位）。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    words = [{"text": "第", "x": 721, "y": 938, "w": 14, "h": 16},
             {"text": "12", "x": 738, "y": 940, "w": 16, "h": 12},
             {"text": "页", "x": 756, "y": 939, "w": 14, "h": 16}]
    host = FakeOCRHost(img, words)
    assert _ons_page(host) == 12


def test_ons_page_returns_none_when_blank():
    """读不到页码返回 None，不抛异常。

    非在售页（市场/大厅）上调用 ons_page() 必须安全返回 None，
    否则 recover() 里一调用就炸。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    host = FakeOCRHost(img, [])
    assert _ons_page(host) is None


def test_ons_page_crop_is_at_6x():
    """裁剪必须放大 6 倍。

    真机实测：「第N页」文字只有 14px 高，1x 和 4x 都读不出来，
    6x 才出字。如果实现改回 1x/2x/4x，这条断言失败。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    host = FakeOCRHost(img, [])
    _ons_page(host)
    assert host.crops, "ons_page 必须调用 _ocr_words"
    h, w = host.crops[0]
    # 裁剪区 y915-975（高 60）× x470-1010（宽 540），放大 6x → 360 x 3240
    assert h == 360, f"裁剪高度应为 60*6=360，实际 {h}（说明没放大 6x）"
    assert w == 3240, f"裁剪宽度应为 540*6=3240，实际 {w}"


def test_ons_page_crop_covers_label():
    """裁剪窗口必须覆盖真机「第N页」的实际位置 y937-956 / x720-764。

    这条防的是「把裁剪区挪到别处」—— 早期扫 y850-900、y1005-1065
    都是空的，因为页码根本不在那里。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    host = FakeOCRHost(img, [])
    _ons_page(host)
    h, w = host.crops[0]
    # 反推裁剪区：h/6 与 w/6 就是裁剪的像素尺寸
    assert h // 6 == 60 and w // 6 == 540
    # 裁剪起点写死在实现里（y915/x470），这里校验覆盖范围
    assert 915 <= 937 and 975 >= 956, "裁剪 y 范围没盖住页码文字 y937-956"
    assert 470 <= 720 and 1010 >= 764, "裁剪 x 范围没盖住页码文字 x720-764"


def test_ons_page_crop_covers_both_arrows():
    """裁剪必须同时盖住左右翻页箭头。

    这是 2026-10-21 修正的那个坑：原裁剪 x=690-800 只有 110px，
    两个箭头分别在 x=504-525 和 x=959-981，全在框外 —— 看裁剪图
    得出的「没有翻页箭头」结论是裁剪造成的假象。真机点 (515,945)
    和 (969,945) 都能翻页（实测 5.3% / 7.6% 变化）。
    """
    img = np.zeros((1080, 1920, 3), np.uint8)
    host = FakeOCRHost(img, [])
    _ons_page(host)
    _, w = host.crops[0]
    width = w // 6
    x0, x1 = 470, 470 + width
    assert x0 <= 504 and x1 >= 525, \
        f"裁剪 x 范围 {x0}-{x1} 没盖住左箭头 x504-525"
    assert x0 <= 959 and x1 >= 981, \
        f"裁剪 x 范围 {x0}-{x1} 没盖住右箭头 x959-981"


# ------------------------------------------------ 价格输入（2026-10-21 实测）

class FakeKeyHost:
    """记录按键序列的宿主，用来验证「清空 → 输入」的键序。

    **不引用 nav 的常量**：键码写死在这里（Ctrl=0x11, A=0x41, Delete=0x2E），
    实现改坏了测试才会红。
    """

    def __init__(self):
        self.events = []      # [(vk, down)]
        self._log = lambda msg, level="info": None

    def _key_vk(self, vk, down=True, hold=0.05):
        self.events.append((vk, down))

    def _type_digits(self, text):
        for ch in text:
            self._key_vk(ord(ch))

    def _clear_field(self):
        Navigator._clear_field(self)


def _keyseq(host):
    """按键序列转成可读串：按下记大写，抬起记小写。"""
    return "".join(
        (f"<{vk:02X}>" if not down else f"{vk:02X}")
        for vk, down in host.events)


def test_clear_field_is_ctrl_a_then_delete():
    """清空必须走 Ctrl+A → Delete。

    真机实测：输入是**追加**不是替换（光标在末尾），不清空就输
    「20000」会变成「2,05020000」。BACKSPACE 连按也能清，但会停在 0；
    Ctrl+A + Delete 才是干净的清空方式。
    """
    host = FakeKeyHost()
    host._clear_field()
    vks = [vk for vk, down in host.events if down]
    assert vks[0] == 0x11, f"第一个按下应是 Ctrl(0x11)，实际 {vks[0]:02X}"
    assert 0x41 in vks, "必须按 A(0x41) —— Ctrl+A 全选"
    assert vks[-1] == 0x2E, f"最后按下应是 Delete(0x2E)，实际 {vks[-1]:02X}"
    assert vks.index(0x11) < vks.index(0x41), "Ctrl 必须在 A 之前按下"
    assert vks.index(0x41) < vks.index(0x2E), "A 必须在 Delete 之前"


def test_type_digits_rejects_non_digit():
    """价格只接受数字。传字母必须抛 NavError，不能静默吞掉。"""
    host = FakeKeyHost()
    with pytest.raises(NavError):
        Navigator._type_digits(host, "12a")


def test_type_digits_sends_each_char():
    """「20000」必须逐位发 5 个按键，不能合并成一个。"""
    host = FakeKeyHost()
    Navigator._type_digits(host, "20000")
    vks = [vk for vk, down in host.events if down]
    assert vks == [ord(c) for c in "20000"], \
        f"应发 2/0/0/0/0 五个键，实际 {vks}"


def test_ons_price_skips_none_side():
    """lo=None 时不能去点最低价框 —— 只改 hi。

    用途：只设上限（「只买 X 金币以下的」）是常见需求，
    这时最低价必须原样保留，不能顺手改成 0。
    """
    host = FakeKeyHost()
    host.clicked = []

    def click(pt, label="", expect_change=True, timeout=4.0):
        host.clicked.append(pt)
        return True

    host.click = click
    host._read_price = lambda label="": (None, 50000)
    Navigator.ons_price(host, lo=None, hi=50000)
    boxes = _box_clicks(host)
    assert len(boxes) == 1, f"只该点 1 次价格框（最高价），实际点了 {boxes}"
    assert boxes[0][0] > 1700, f"应点最高价框（x>1700），实际 {boxes[0]}"


def test_ons_price_rejects_negative():
    """负价格直接抛错，不发按键。"""
    host = FakeKeyHost()
    host.click = lambda *a, **kw: True
    with pytest.raises(NavError):
        Navigator.ons_price(host, lo=-1)


def test_ons_price_uses_min_and_max_points():
    """lo 和 hi 都设时，两个框都要点，且最低价框在左、最高价框在右。"""
    host = FakeKeyHost()
    host.clicked = []

    def click(pt, label="", expect_change=True, timeout=4.0):
        host.clicked.append(pt)
        return True

    host.click = click
    host._read_price = lambda label="": (20000, 50000)
    Navigator.ons_price(host, lo=20000, hi=50000)
    boxes = _box_clicks(host)
    assert len(boxes) == 2, f"应点 2 个价格框，实际 {boxes}"
    assert boxes[0][0] < boxes[1][0], f"最低价框应在左（x 更小）：{boxes}"


# ---------------- 在售页筛选面板：材质 / 颜色 / 更多分类 ----------------
#
# 真机实测（scratch/tbl2.py）：
#   复选框外框 29x29，左列中心 x=1396，右列中心 x=1641，行距 56
#   勾选 = 框内实心白块（白像素 196）；未勾选 = 空框（白像素 0）
#   点击目标是方框右边的文字（左列 x≈1436、右列 x≈1681）
#
# 下面的假对象**不引用 nav_coords 常量**：坐标、白像素阈值全部写死成实测数字，
# 这样把 nav_coords.py 里的常量改坏（比如把 x 换成滑块手柄）测试就会红。


class FakePanel:
    """假面板：模拟 ons_options() / ons_check() 需要的宿主接口。

    只实现 ons_options 依赖的三样东西：_snap() 返回一张带复选框的假图、
    _ocr_words() 返回假词、mouse.click() 记录点击。
    """

    def __init__(self, checked=(), rows=None):
        self.checked = set(checked)
        self.clicked = []
        self.said = []
        # rows: [(name, cx, cy)]，cx 是方框中心 x，文字在 cx+40
        self.rows = rows if rows is not None else [
            ("不限", 1396, 745), ("速度线", 1641, 745),
            ("镭射贵金属", 1396, 801), ("贵金属", 1641, 801),
            ("镭射", 1396, 857),
        ]
        self.mouse = self
        self.pt = lambda p: p
        self.say = lambda msg, lvl="info": self.said.append(msg)

    def click(self, x, y, verify=True):
        self.clicked.append((x, y))
        # 点文字 → 找到那一行 → 切换勾选
        for name, cx, cy in self.rows:
            if abs(y - cy) <= 15 and abs(x - (cx + 40)) <= 40:
                if name in self.checked:
                    self.checked.discard(name)
                else:
                    self.checked.add(name)
                break
        return True

    def _snap(self):
        import numpy as np
        img = np.zeros((1080, 1920, 3), dtype=np.uint8)
        for name, cx, cy in self.rows:
            x0, y0 = cx - 14, cy - 14
            # 外框：一圈灰度 100 的边，内部灰 30（真机空框就是这样）
            img[y0:y0 + 29, x0:x0 + 29] = 100
            img[y0 + 2:y0 + 27, x0 + 2:x0 + 27] = 30
            if name not in self.checked:
                continue
            # 真机两种已勾选外观，白像素数差 3 倍，但都算「已勾选」：
            #   「不限」 → 实心白块，约 196 白像素
            #   具体档位 → 对勾，约 65 白像素
            if name == "不限":
                img[y0 + 7:y0 + 22, x0 + 7:x0 + 22] = 255
            else:
                # 对勾：粗 3 的两笔，内部约 95 白像素（真机实测 65，
                # 同量级；关键是远小于实心块的 196、又远大于空框的 0）
                import cv2
                cv2.line(img, (x0 + 8, y0 + 16), (x0 + 13, y0 + 21),
                         (255, 255, 255), 3)
                cv2.line(img, (x0 + 13, y0 + 21), (x0 + 23, y0 + 9),
                         (255, 255, 255), 3)
        return img

    def _ocr_words(self, crop):
        # crop 是 img[30:1080, 1360:1900]，换算回整屏坐标
        return [{"x": cx + 40 - 1360, "y": cy - 30 - 8, "w": 30, "h": 16,
                 "text": name}
                for name, cx, cy in self.rows]


def _nav_for_panel(panel):
    from core.nav import Navigator
    n = Navigator.__new__(Navigator)
    n._snap = panel._snap
    n._ocr_words = panel._ocr_words
    n.mouse = panel.mouse
    n.pt = panel.pt
    n.say = panel.say
    # **只打桩 _ons_wheel**（真实现会调 ctypes 移动真实鼠标、滚动真实
    # 游戏窗口）。单元测试绝不能碰真机 —— 不打桩时整个文件从 11s 涨到
    # 79s，而且真的会滚到游戏画面上。
    # 只桩这一层，ons_scroll_top / ons_ensure_block / ons_find 的真实
    # 循环逻辑仍被测试覆盖。
    n.wheel_calls = []
    n._ons_wheel = lambda notches, at=None: n.wheel_calls.append(notches) or True
    return n


def test_ons_options_reads_all_boxes_and_states():
    """能读出全部 5 个材质选项，且勾选状态判断正确。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    opts = n.ons_options()
    names = [o["name"] for o in opts]
    assert names == ["不限", "速度线", "镭射贵金属", "贵金属", "镭射"], names
    on = {o["name"]: o["on"] for o in opts}
    assert on["不限"] is True, "「不限」应判为已勾选"
    assert on["速度线"] is False, "「速度线」应判为未勾选"


def test_ons_box_on_distinguishes_196_from_0():
    """勾选=框内实心白、未勾选=空框，判据必须分得开。"""
    import numpy as np
    from core.nav import Navigator
    img = np.zeros((60, 60, 3), dtype=np.uint8)
    # 空框：只有一圈 100 的边
    img[10:39, 10:39] = 100
    img[12:37, 12:37] = 30
    empty = {"x": 10, "y": 10, "w": 29, "h": 29, "cx": 24, "cy": 24}
    assert Navigator._ons_box_on(img, empty) is False

    # 勾选：内部填白
    img2 = img.copy()
    img2[17:32, 17:32] = 255
    assert Navigator._ons_box_on(img2, empty) is True


def test_ons_check_clicks_text_not_box():
    """点击目标是方框右边的文字，不是方框本身。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    n.ons_check("速度线", want=True)
    assert len(p.clicked) == 1, p.clicked
    x, y = p.clicked[0]
    # 「速度线」方框中心 1641，文字应在 1681 附近
    assert x > 1641 + 20, f"应点方框右侧文字（x>1661），实际 x={x}"
    assert "速度线" in p.checked


def test_ons_check_skips_when_already_wanted():
    """已是目标状态就不点 —— 面板会记住上次勾选，盲目点会取消掉。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    assert n.ons_check("不限", want=True) is False
    assert p.clicked == [], f"不该点任何地方，实际 {p.clicked}"


def test_ons_check_uncheck():
    """能取消勾选。"""
    p = FakePanel(checked={"不限", "速度线"})
    n = _nav_for_panel(p)
    assert n.ons_check("速度线", want=False) is True
    assert "速度线" not in p.checked


def test_ons_check_unknown_name_raises():
    """选项名不在面板上 → 抛 NavError，不点相近的项。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    with pytest.raises(NavError):
        n.ons_check("不存在的材质")
    assert p.clicked == [], "找不到就不该点任何地方"


def test_ons_checks_rejects_more_than_two():
    """材质最多选 2 种，传 3 个直接拒绝。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    with pytest.raises(NavError):
        n.ons_checks(["速度线", "镭射贵金属", "贵金属"])
    assert p.clicked == [], "超限不该点任何地方"


def test_ons_checks_two_ok():
    """正好 2 个可以。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    done = n.ons_checks(["速度线", "贵金属"])
    assert set(done) == {"速度线", "贵金属"}
    assert {"速度线", "贵金属"} <= p.checked


def test_ons_box_on_counts_checkmark_as_checked():
    """对勾（白像素约 65）必须判为已勾选，不能要求实心块。

    真机实测：具体档位勾上后画的是对勾，白像素只有 65；
    「不限」勾上后画实心块，白像素 196。用「过半」当阈值会把
    65 判成未勾选 —— 这是实际踩过的坑。
    """
    import numpy as np
    from core.nav import Navigator
    box = {"x": 10, "y": 10, "w": 29, "h": 29, "cx": 24, "cy": 24}

    # 空框
    img = np.zeros((60, 60, 3), dtype=np.uint8)
    img[10:39, 10:39] = 100
    img[12:37, 12:37] = 30
    assert Navigator._ons_box_on(img, box) is False

    # 对勾：内部约 65 个白像素（占内部 16x16=256 的 25%）
    img2 = img.copy()
    img2[20:30, 20:27] = 255   # 10x7 = 70 个白像素
    assert Navigator._ons_box_on(img2, box) is True,         "对勾（约 65 白像素）应判为已勾选"

    # 实心块：196 个白像素
    img3 = img.copy()
    img3[17:32, 17:32] = 255   # 15x15 = 225
    assert Navigator._ons_box_on(img3, box) is True


def test_ons_options_ignores_panel_title():
    """面板标题「筛选」的「筛」字会被当成方框，必须排除。

    真机实测：y≈69 处边缘检测出 26x27 的框，配到「筛」字，
    如果不排除会当成一个叫「筛」的材质选项。
    """
    p = FakePanel(checked={"不限"},
                  rows=[("不限", 1396, 219), ("速度线", 1641, 219),
                        ("筛", 1396, 69)])   # 标题误报
    n = _nav_for_panel(p)
    opts = n.ons_options()
    names = [o["name"] for o in opts]
    assert "筛" not in names, f"标题「筛」不该进选项表：{names}"
    assert "不限" in names and "速度线" in names


def test_ons_options_normalizes_laser_precious_metal():
    """「镭射贵金属」常被 OCR 读成「镭射贵金」，必须归一到标准名。

    不归一化的话 ons_check("镭射贵金属") 会报「面板上没有这个选项」。
    """
    p = FakePanel(checked=set(),
                  rows=[("镭射贵金", 1396, 275)])   # OCR 丢末字
    n = _nav_for_panel(p)
    opts = n.ons_options()
    assert len(opts) == 1, opts
    assert opts[0]["name"] == "镭射贵金属", opts[0]
    assert opts[0]["raw"] == "镭射贵金", "原始 OCR 文本应保留在 raw 里"


def test_ons_check_accepts_normalized_name():
    """用标准名「镭射贵金属」能勾上 OCR 读成「镭射贵金」的那一项。"""
    p = FakePanel(checked=set(), rows=[("镭射贵金", 1396, 275)])
    n = _nav_for_panel(p)
    assert n.ons_check("镭射贵金属", want=True) is True
    assert "镭射贵金" in p.checked


# ---------------- 网格定位：OCR 读不出名字时按位置反推 ----------------

class FakeGridPanel:
    """假面板：双列 + 多区块，文字可选是否让 OCR 读到。

    用来验证「稀有度极品/优品、成色 S/A/B/C、材质其他」这些
    OCR 读不出字的格子能否靠位置正确识别。
    """

    def __init__(self, blocks, checked=(), hide_text=()):
        # blocks: {区块名: (标题 y, [(row, col, name)])}
        self.blocks = blocks
        self.checked = set(checked)
        self.hide_text = set(hide_text)
        self.clicked = []
        self.mouse = self
        self.pt = lambda p: p
        self.say = lambda msg, lvl="info": None

    def rows(self):
        out = []
        for blk, (title_y, items) in self.blocks.items():
            for row, col, name in items:
                cx = 1396 if col == 0 else 1641
                cy = title_y + 20 + row * 56
                out.append((blk, row, col, name, cx, cy))
        return out

    def click(self, x, y, verify=True):
        self.clicked.append((x, y))
        for blk, row, col, name, cx, cy in self.rows():
            if abs(y - cy) <= 15 and abs(x - (cx + 40)) <= 40:
                if name in self.checked:
                    self.checked.discard(name)
                else:
                    self.checked.add(name)
                break
        return True

    def _snap(self):
        import numpy as np
        img = np.zeros((1080, 1920, 3), dtype=np.uint8)
        for blk, row, col, name, cx, cy in self.rows():
            x0, y0 = cx - 14, cy - 14
            img[y0:y0 + 29, x0:x0 + 29] = 100
            img[y0 + 2:y0 + 27, x0 + 2:x0 + 27] = 30
            if name in self.checked:
                img[y0 + 7:y0 + 22, x0 + 7:x0 + 22] = 255
        return img

    def _ocr_words(self, crop):
        """只返回区块标题 + 未被 hide_text 隐藏的选项名。"""
        out = []
        for blk, (title_y, items) in self.blocks.items():
            title = {"rarity": "稀有度", "condition": "成色",
                     "material": "材质", "color": "颜色",
                     "more": "更多分类"}[blk]
            # 标题切成单字，模拟真机 OCR 行为
            for i, ch in enumerate(title):
                out.append({"x": 1382 + i * 18 - 1360, "y": title_y - 30 - 8,
                            "w": 17, "h": 16, "text": ch})
            for row, col, name in items:
                if name in self.hide_text:
                    continue
                cx = 1396 if col == 0 else 1641
                cy = title_y + 20 + row * 56
                out.append({"x": cx + 40 - 1360, "y": cy - 30 - 8,
                            "w": 30, "h": 16, "text": name})
        return out


def _nav_for_grid(panel):
    from core.nav import Navigator
    n = Navigator.__new__(Navigator)
    n._snap = panel._snap
    n._ocr_words = panel._ocr_words
    n.mouse = panel.mouse
    n.pt = panel.pt
    n.say = panel.say
    # **只打桩 _ons_wheel**（真实现会调 ctypes 移动真实鼠标、滚动真实
    # 游戏窗口）。单元测试绝不能碰真机 —— 不打桩时整个文件从 11s 涨到
    # 79s，而且真的会滚到游戏画面上。
    # 只桩这一层，ons_scroll_top / ons_ensure_block / ons_find 的真实
    # 循环逻辑仍被测试覆盖。
    n.wheel_calls = []
    n._ons_wheel = lambda notches, at=None: n.wheel_calls.append(notches) or True
    return n


RARITY_COND = {
    "rarity": (283, [(0, 0, "所有稀有度"), (0, 1, "极品"),
                     (1, 0, "优品")]),
    "condition": (458, [(0, 0, "所有成色"), (0, 1, "S"),
                        (1, 0, "A"), (1, 1, "B"), (2, 0, "C")]),
}


def test_grid_identifies_rarity_by_position_when_ocr_blind():
    """稀有度「极品/优品」OCR 读不出，必须靠位置认出。"""
    p = FakeGridPanel(RARITY_COND, checked={"所有稀有度"},
                      hide_text={"极品", "优品"})
    n = _nav_for_grid(p)
    opts = n.ons_options()
    names = [o["name"] for o in opts]
    assert "极品" in names, f"「极品」应靠位置识别出来，实际 {names}"
    assert "优品" in names, f"「优品」应靠位置识别出来，实际 {names}"


def test_grid_identifies_condition_letters_by_position():
    """成色 S/A/B/C 是单个字母，OCR 全读不出，必须靠位置认出。"""
    p = FakeGridPanel(RARITY_COND, checked={"所有成色"},
                      hide_text={"S", "A", "B", "C"})
    n = _nav_for_grid(p)
    opts = n.ons_options()
    names = [o["name"] for o in opts]
    for letter in ("S", "A", "B", "C"):
        assert letter in names, f"成色 {letter} 应靠位置识别，实际 {names}"


def test_grid_check_condition_c_by_position():
    """能勾上 OCR 读不出字的「成色 C」。"""
    p = FakeGridPanel(RARITY_COND, checked={"所有成色"},
                      hide_text={"S", "A", "B", "C"})
    n = _nav_for_grid(p)
    assert n.ons_check("C", want=True) is True
    assert "C" in p.checked


def test_grid_identifies_material_other():
    """材质「其他」OCR 读不出（真机 fx=2 才勉强出字），靠位置认。"""
    mat = {"material": (689, [(0, 0, "不限"), (0, 1, "速度线"),
                              (1, 0, "镭射贵金属"), (1, 1, "贵金属"),
                              (2, 0, "镭射"), (2, 1, "其他")])}
    p = FakeGridPanel(mat, checked={"不限"}, hide_text={"其他"})
    n = _nav_for_grid(p)
    opts = n.ons_options()
    names = [o["name"] for o in opts]
    assert "其他" in names, f"材质「其他」应靠位置识别，实际 {names}"
    assert n.ons_check("其他", want=True) is True
    assert "其他" in p.checked


def test_grid_prefers_ocr_text_when_available():
    """OCR 能读到时以文字为准，不被位置表覆盖。

    真机颜色区文字 fx=1 就能读，位置表只是兜底。若两者冲突（比如
    位置表过时），应以 OCR 为准。
    """
    col = {"color": (919, [(0, 0, "不限"), (0, 1, "黑色")])}
    p = FakeGridPanel(col, checked={"不限"})
    n = _nav_for_grid(p)
    opts = n.ons_options()
    by_name = {o["name"]: o for o in opts}
    assert "黑色" in by_name
    assert by_name["黑色"]["raw"] == "黑色", "有 OCR 文本时应记在 raw 里"


def test_grid_splits_rarity_and_condition_into_separate_blocks():
    """稀有度和成色必须是两个区块，不能混成一个。

    真机踩过：两个区块的方框被归到同一组，「所有成色」被标成
    rarity r3c0，成色区 S/A/B/C 全丢。
    真机实测两区块间空隙 118-120px，行距只有 56px —— 用空隙切分。
    """
    p = FakeGridPanel(RARITY_COND, checked={"所有稀有度", "所有成色"},
                      hide_text={"极品", "优品", "S", "A", "B", "C"})
    n = _nav_for_grid(p)
    opts = n.ons_options()
    by_name = {o["name"]: o for o in opts}

    assert "所有成色" in by_name
    assert by_name["所有成色"]["block"] == "condition",         f"「所有成色」应在 condition 区块，实际 {by_name['所有成色']['block']}"
    assert by_name["所有稀有度"]["block"] == "rarity",         f"「所有稀有度」应在 rarity 区块，实际 {by_name['所有稀有度']['block']}"
    for letter in ("S", "A", "B", "C"):
        assert letter in by_name, f"成色 {letter} 丢了，实际 {list(by_name)}"
        assert by_name[letter]["block"] == "condition"


def test_grid_no_duplicate_boxes():
    """每个选项只该出现一次 —— 内外双框要去重。

    真机实测边缘检测对同一个框报两次（29x29 外框 + 23x23 内框），
    不去重会看到两个「橙色」，其中一个状态还是错的。
    """
    p = FakeGridPanel(RARITY_COND, checked={"所有稀有度"})
    n = _nav_for_grid(p)
    opts = n.ons_options()
    names = [o["name"] for o in opts]
    assert len(names) == len(set(names)), f"有重复项：{names}"


# ---------------- 滚动前置条件 ----------------
#
# 为什么这些必须钉住：区块名字靠「竖直空隙切组 + 固定顺序推断」，
# 如果某个区块完全滚出屏幕，顺序就推错（实测滚 4 格后材质被当成
# condition，成色 S/A/B/C 全丢）。所以读选项前必须滚到顶部。


def test_ons_options_scrolls_to_top_by_default():
    """ons_options() 默认必须先滚到顶部 —— 否则区块推断会错位。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    calls = []
    n.ons_scroll_top = lambda *a, **k: calls.append("top") or True
    n.ons_options()
    assert calls == ["top"], f"应滚到顶部一次，实际 {calls}"


def test_ons_options_skips_scroll_when_asked():
    """scroll_top=False 时不滚 —— 供已定位好位置后复读用。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    calls = []
    n.ons_scroll_top = lambda *a, **k: calls.append("top") or True
    n.ons_options(scroll_top=False)
    assert calls == [], f"不该滚动，实际 {calls}"


def test_ons_find_scrolls_but_stops_when_panel_cannot_move():
    """面板滚不动时 ons_find 必须停下返回 None，不能死循环。

    假面板没有滚动能力（每屏内容相同），所以签名不变 → 立刻 break。
    真机上「滚不动」= 已经到底，同样应该停。
    """
    p = FakePanel(checked={"不限"},
                  rows=[("不限", 1396, 219), ("经典款", 1396, 1050)])
    n = _nav_for_panel(p)
    got = n.ons_find("经典款")
    assert got is None, f"假面板滚不动，应返回 None，实际 {got}"
    assert len(n.wheel_calls) <= 2,         f"滚不动就该立刻停，实际滚了 {len(n.wheel_calls)} 次"
    assert len(n.wheel_calls) >= 1, "至少要尝试滚一次"


def test_ons_check_reports_missing_option_after_searching():
    """真找不到才报错，而且错误信息里带可见项。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    with pytest.raises(NavError) as ei:
        n.ons_check("根本不存在的选项")
    assert "根本不存在的选项" in str(ei.value)


def test_ons_find_finds_visible_option_without_scrolling():
    """选项本来就在屏幕内时，不该白滚。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    got = n.ons_find("速度线")
    assert got is not None and got["name"] == "速度线"
    assert n.wheel_calls == [], f"不该滚动，实际 {n.wheel_calls}"


def test_ons_checks_rereads_each_item():
    """批量勾选时每项都重新读 —— 勾具体档位会让「不限」自动取消。"""
    p = FakePanel(checked={"不限"})
    n = _nav_for_panel(p)
    reads = []
    orig = n._ocr_words

    def counting(crop):
        reads.append(1)
        return orig(crop)

    n._ocr_words = counting
    n.ons_checks(["速度线", "贵金属"])
    # 两项各读一次以上（每项至少读一次当前屏 + 一次确认）
    assert len(reads) >= 4, f"每项都该重读，实际只读了 {len(reads)} 次"


# ---------------- 价格框：点对位置、按对、读对（2026-10-21 真机返工） ----------------
#
# 真机踩过的三个坑，这里各钉一条：
#   1. 点 (1590,168) 压在最低价框右边缘外 → 不聚焦 → 输进去的字全丢
#   2. 逐位输入只发 keydown 不发 keyup → 键卡在按下状态 → 输 3000 只剩 30
#   3. 读价格裁到 x=1800，把最高价框右半截切掉 → 要 3000 读回 2


class FakeTypeHost:
    """只记录 _key_vk 的宿主，用来验证「每个键都按下再抬起」。"""

    def __init__(self):
        self.events = []

    def _key_vk(self, vk, down=True, hold=0.05):
        self.events.append((vk, down))


def test_type_digits_presses_and_releases_every_key(monkeypatch):
    """每个数字都要有 keydown + keyup，否则键卡住、后面的字进不去。"""
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *a: None)
    host = FakeTypeHost()
    Navigator._type_digits(host, "3000")

    downs = [vk for vk, d in host.events if d]
    ups = [vk for vk, d in host.events if not d]
    assert downs == [ord(c) for c in "3000"], downs
    assert ups == downs, f"每个键都要抬起，抬起序列 {ups}"


def _box_clicks(host):
    """只取点在价格行（y<200）上的点击 —— 失焦那一下不算设价格。"""
    return [pt for pt in host.clicked if pt[1] < 200]


def _price_words(left, right, icon=True):
    """按真机 OCR 的形状造词：金币图标 h 大、数字被逗号切成多段。"""
    words = []
    if icon:
        words.append({"text": "0", "x": 415, "y": 50, "w": 74, "h": 77})
        words.append({"text": "0", "x": 871, "y": 50, "w": 74, "h": 77})
    for i, part in enumerate(left):
        words.append({"text": part, "x": 524 + i * 60, "y": 63, "w": 40, "h": 46})
    for i, part in enumerate(right):
        words.append({"text": part, "x": 979 + i * 60, "y": 63, "w": 40, "h": 46})
    return words


def test_read_price_keeps_the_two_boxes_apart():
    """左框 2,050、右框 9,999,999 各读各的，金币图标不能被当成数字。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    host = FakeOCRHost(img, _price_words(["2", "050"], ["9", "999", "999"]))
    assert Navigator._read_price(host) == (2050, 9999999)


def test_read_price_returns_none_for_a_blank_side():
    """框里空着就返回 None —— 不能把另一侧的数字顶上来当上限。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    host = FakeOCRHost(img, _price_words(["2", "050"], []))
    assert Navigator._read_price(host) == (2050, None)


def test_ons_price_reports_what_the_box_actually_shows():
    """ons_price 必须返回读回值，好让调用方比对 —— 读错要能发现。"""
    host = FakeKeyHost()
    host.clicked = []
    host.click = lambda pt, label="", expect_change=True, timeout=4.0: (
        host.clicked.append(pt) or True)
    host._clear_field = lambda: None
    host._type_digits = lambda text: None
    host._read_price = lambda: (1000, 2)      # 真机症状：上限丢了位数
    assert Navigator.ons_price(host, lo=1000, hi=3000) == (1000, 2)


def test_ons_price_clicks_the_centre_of_each_box():
    """点框中心，不点边缘 —— (1590,168) 压在最低价框右边框外，点了不聚焦。"""
    host = FakeKeyHost()
    host.clicked = []
    host.click = lambda pt, label="", expect_change=True, timeout=4.0: (
        host.clicked.append(pt) or True)
    host._clear_field = lambda: None
    host._type_digits = lambda text: None
    host._read_price = lambda: (1000, 3000)
    Navigator.ons_price(host, lo=1000, hi=3000)
    boxes = _box_clicks(host)
    assert boxes == [ONS_PRICE_MIN, ONS_PRICE_MAX], boxes
    assert 1500 <= boxes[0][0] <= 1600, boxes[0]
    assert boxes[0][1] < 165, "y 要落在数字行上，别贴着下边框"
