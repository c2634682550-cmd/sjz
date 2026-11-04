"""交易行导航：从大厅走到「在售」页并把筛选条件设好。

为什么单独一个模块：抢购引擎（engine.py）只管「盯按钮 → 点 → 确认」，它假设
人已经站在正确的页面上。而「站到正确的页面上」这件事本身有十几步点击，每步
都可能点错——点错「售出」就变成卖东西，点错「充值」就花真钱。所以这一串动作
必须有自己的坐标表、自己的校验、自己的安全护栏，不能散在 engine 里。

模块分工（依赖单向，没有环）：

    core/nav_coords.py  坐标标定表 + 禁用点表 + NavError（纯数据，无逻辑）
    core/nav_col.py     典藏外观页（COL_*）的筛选面板与皮肤卡片操作
    core/nav_ons.py     在售页（ONS_*）的筛选面板操作
    core/nav.py         本文件：通用导航（NavBase）+ 页面识别 + Navigator 组装

链路（实测通过，每步都有画面变化校验）：

    大厅 --F4--> 市场 --点标签--> 典藏外观 --点卡片--> 选中皮肤
         --点购买--> 在售页 --点筛选--> 筛选面板 --设条件--> 确定

安全护栏（硬性，不可绕过）：

  1. 只点白名单里的坐标。`_CLICKABLE` 是唯一允许的点击集合，
     `Navigator.click()` 拒绝任何不在表里的目标——避免手滑点到「充值」。
  2. 「售出」永远不在白名单里。曼德尔砖页上「购买」(1642,959) 和
     「售出」(1634,1032) 只差 73px，坐标漂移一个卡片高度就会点错。
  3. 每次点击后校验画面变化。没变化就重试，连续失败就抛
     `NavError`，绝不「再试一次别的坐标」——那等于乱点。
"""
import re
import time

import numpy as np

from . import vision
from .nav_col import NavColPanel
from .nav_coords import (FORBIDDEN, NavError, TAB_CHARM, TAB_COLLECTION,
                         TAB_MANDEL, _scaled)
from .nav_ons import NavOnsPanel


# ---------------------------------------------------------------- 页面识别
#
# 只校验「画面变了」是不够的：点错地方也会变。实测踩过一次——起点本来在
# 在售页，点「标签」实际只是关掉了筛选面板（变化 33.6%），点「卡片」只是
# 换了另一条挂单（变化 99.6%），每一步都"有变化"，但一步都没走对。
# 所以每一步都要能回答「我现在到底在哪一页」。
#
# 判据用 OCR 里**扛得住误认**的短词。实测 OCR 把「默认排序」认成「争认摊序」、
# 「当前市场流量」认成「当前伟场流即5」、「拥有该皮肤」认成「拥有该电玩手s2」——
# 长词不可靠，短词和数字才可靠。
_PAGE_ANCHORS = [
    # (页面名, 必需词组, 可选词组)
    # 顺序即优先级：弹层盖在页面上，OCR 会同时读到两者，所以弹层先判。
    #
    # 判据必须「跨页唯一」。实测踩过的两个坑：
    #   - 「全部赛季」曼德尔砖筛选面板也有 → 必须配「枪种」
    #   - 「当前市场流通量」典藏外观页也有（显示选中皮肤行情）→ 不能当判据
    #   - 「曼德尔砖」典藏外观页的左侧目录项也叫这个 → 不能当判据
    # 所以「市场」这一层只判到 market，曼德尔砖 vs 典藏外观由绿框位置决定
    # （见 tab_from_pixels，那是唯一可靠的判据）。
    # 「推荐」页（销售榜单）必须排在 onsale 之前：它的顶部也有「在售/推荐」
    # 两个标签，只按「推荐」判会把榜单页当成在售页 —— 真机踩过：程序在榜单页
    # 上点「公示期筛选」(435,197)，点的是榜单筛选，整轮跑在错页上。
    # 「榜单」只有推荐页有（在售页 / detail 页都没有，见 ONSALE_TEXT）。
    ("recommend", ("榜单",), ()),
    ("filter_onsale", ("价格区间",), ("稀有度", "成色", "材质")),
    ("filter_onsale", ("所有稀有度", "所有成色"), ()),
    ("filter_collection", ("全部赛季", "枪种"), ()),
    ("filter_collection", ("枪种", "已拥有"), ()),
    ("filter_collection", ("已拥有", "未拥有", "品阶"), ()),
    # 在售列表页必须在 detail 之前判！实测踩坑：在售列表页右侧详情面板
    # 也显示「编号」「淘汰计数」「相似皮肤」，如果 detail 判据先命中，
    # 整页被误判成 detail，之后所有在售页测绘/筛选全部作用在错页上
    # （survey2 一整轮数据因此作废）。
    #
    # **判据是「推荐」，不是「公示期」**：detail 页顶部也有那条筛选条
    # （「不限公示期」「默认排序」），拿它当判据会把 detail 误判成 onsale
    # （tests/test_nav.py 的 DETAIL_TEXT 用例当场抓到）。在售列表页独有的
    # 是左上角「在售 / 推荐」两个标签里的「推荐」——detail 页没有。
    ("onsale", ("推荐",), ()),
    # 页码用正则，不写死「第1页」：翻到第 2 页以后 OCR 读到的就是「第2页」，
    # 写死之后这条判据失效，后面的「相似皮肤/编号」把整页判成 detail
    # （review V03，已用 "在售第3页…" 复现）。
    ("onsale", re.compile(r"第\s*\d+\s*页"), ()),
    ("detail", ("相似皮",), ()),
    ("detail", ("编号", "淘汰"), ()),
    # 市场页（曼德尔砖 / 典藏外观 / 典藏挂饰）的统一判据。
    #
    # 实测两种 OCR 形态：
    #   形态 A（内容多时）：「典藏工坊」「市场流通量」「交易」「典藏」
    #   形态 B（内容少时）：「当季产出」「往季产」「市场流通量」「成交均价」
    # OCR 会把「典藏外观」读成「典崆外」、把「典藏工坊」整段漏掉，所以
    # 判据不能只靠某几个词，得多留几条并集。
    ("market", ("典藏工坊",), ()),
    ("market", ("市场流通量",), ()),
    ("market", ("当季产出",), ()),
    ("market", ("往季产",), ()),
    ("market", ("交易", "筛选", "典藏"), ()),
    # 典藏外观页（砖皮列表）：页面上那些小字 1 倍 OCR 读不出来，只有
    # 「典藏外观」这四个字认得准 —— 认成 market 才不会让导航去乱按 ESC。
    # 放在 on sale / detail 之后：在售页的详情面板里也写着「典藏外观」，
    # 顺序反了会把在售页认成市场页。
    ("market", ("典藏外观",), ()),
    # 大厅。OCR 实测：「开始游戏」「特战干员」「部门」「交易行」「特勤处」
    # 「行前备战」。这些词只在主界面出现。
    ("hall", ("开始游戏",), ()),
    ("hall", ("特战干员", "部门"), ()),
    ("hall", ("特勤处", "行前"), ()),
]


def click_px(host, point, label="", **kw):
    """点真实像素坐标。Navigator 走 click_px；只有 click 的测试替身退回 click。

    测试替身（FakeOCRHost 之类）把 Navigator 的方法绑在自己身上，没有
    click_px；1920x1080 下 click 的缩放本来就是恒等，退回去结果一样。
    """
    fn = getattr(host, "click_px", None)
    if fn is None:
        return host.click(point, label, **kw)
    return fn(point, label, **kw)


def classify(text):
    """从 OCR 拼串判断当前在哪一页。认不出返回 "unknown"。

    注意：市场页只判到 "market"，不区分曼德尔砖/典藏外观/典藏挂饰——
    那三个标签页的 OCR 文字高度重叠（都含「典藏工坊」「市场流通量」
    「曼德尔砖」），文本层面无法区分。要细分请用 tab_from_pixels()。

    纯函数，不碰屏幕 —— 这样可以用真机日志里的原文做回归。
    """
    if not text:
        return "unknown"
    flat = "".join(str(text).split())
    for page, need, _opt in _PAGE_ANCHORS:
        # need 可以是词组元组，也可以是预编译正则（页码这类必须匹配数字的判据）
        if hasattr(need, "search"):
            if need.search(flat):
                return page
            continue
        if all(w in flat for w in need):
            return page
    return "unknown"


# 顶部标签的绿色选中框。像素判据比 OCR 可靠得多：实测选中「曼德尔砖」时
# 绿框在 x127-257，选中「典藏外观」时在 x257-387，位置本身就是答案。
_TAB_SAMPLE_Y = 53
_PAGE_NO_BOX = (470, 915, 540, 60)   # 「第 N 页」ROI（基准坐标）
_TAB_CENTERS = (192, 322, 454)   # 曼德尔砖 / 典藏外观 / 典藏挂饰
_TAB_NAMES = ("mandel", "collection", "charm")


def tab_from_pixels(img, y=53, x0=100, x1=600, min_run=60, band=8):
    """从顶部标签栏的绿色选中框判断当前选中哪个标签。

    绿色判据：G 明显高于 R 和 B。实测选中框颜色 BGR(133,177,86)，
    G/R≈2.06、G/B≈1.33 —— 注意 G/B 只有 1.33，蓝绿分量很接近，所以
    只能拿 G 跟 R 比，不能同时要求 G 远高于 B。
    早期写成 `g > r*1.4 and g > b*1.4` 并要求 g>100，实测全部漏检。

    纵向在 y-band..y+band 内逐行取最长绿色 run 再取最大：判据只有 1~2px
    厚时（实测 y=53 有框、y=51/55 全无），DPI 或窗口缩放稍微一动整条判据
    就失效（review V04）。

    找不到就返回 None。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return None
    h, w = img.shape[:2]
    if y >= h or x1 > w:
        return None
    best = None
    for row_y in range(max(0, y - band), min(h, y + band + 1)):
        row = img[row_y, x0:x1].astype(np.int16)
        b, g, r = row[:, 0], row[:, 1], row[:, 2]
        mask = (g > 80) & (g > r * 1.2) & (g > b * 1.05)

        run, start = None, None
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if run is None or (i - start) > (run[1] - run[0]):
                    run = (start, i)
                start = None
        if start is not None and (run is None or (len(mask) - start) > (run[1] - run[0])):
            run = (start, len(mask))
        if run is not None and (best is None or (run[1] - run[0]) > (best[1] - best[0])):
            best = run
    if best is None or best[1] - best[0] < min_run:
        return None

    center = x0 + (best[0] + best[1]) // 2
    idx = min(range(3), key=lambda i: abs(_TAB_CENTERS[i] - center))
    return _TAB_NAMES[idx]


class NavBase:
    """通用导航：坐标换算、点击护栏、页面识别。

    screen  —— core.screen.Screen
    mouse   —— core.input.Mouse（要带 game_hwnd，走 SetCursorPos+PostMessage）
    keyboard—— core.input.Keyboard（F4 只能靠 PostMessage 送进去）
    snap    —— 可选，返回当前整屏 BGR 数组的函数；默认用 screen.grab()
    dry_run —— True 时只打印不点击
    """

    # 单步点击的重试次数。2 次够了：第一次可能是画面还在动，第二次还不行
    # 就是真点错了，继续重试只会乱点。
    RETRIES = 2

    def __init__(self, screen, mouse, keyboard=None, snap=None, dry_run=False,
                 log=None, ocr_read=None):
        self.screen = screen
        self.mouse = mouse
        self.keyboard = keyboard
        self.dry_run = dry_run
        self._snap = snap or (lambda: screen.grab())
        self._log = log or (lambda msg, level="info": None)
        self._ocr_read = ocr_read
        # 测试注入的 OCR 只认固定文本，不能再叠真 OCR 的读数
        self._ocr_injected = ocr_read is not None
        w, h = screen.size
        self.size = (w, h)

    # ---------------- 底层 ----------------

    def pt(self, point):
        return _scaled(point, self.size[0], self.size[1])

    def px_box(self, box):
        """把 1920x1080 基准坐标系里的矩形 (x, y, w, h) 裁到当前分辨率。

        ROI 常量全部是基准坐标，裁剪前必须过这一道：以前直接拿 1080p 的
        数字去切 2560x1440 的图，切到的是另一块界面（review V02）。
        """
        x, y, w, h = box
        x0, y0 = self.pt((x, y))
        x1, y1 = self.pt((x + w, y + h))
        return (int(x0), int(y0),
                max(1, int(x1 - x0)), max(1, int(y1 - y0)))

    def crop_box(self, img, box):
        """按基准坐标裁一块（自动换算到当前分辨率）。"""
        x, y, w, h = self.px_box(box)
        return img[y:y + h, x:x + w]

    def say(self, msg, level="info"):
        self._log(msg, level)

    def _read_page_no(self, img, min_score=0.6):
        """单独高倍读页码那一小块（「第 N 页」）。

        为什么不能只靠整屏 OCR：在售页和详情页的**右侧详情面板长得一样**
        （都有「相似皮肤」「编号」「淘汰计数」），区分两者靠的是在售页底部
        的「第 N 页」—— 可那是 12px 的小字，整屏 1 倍 OCR 直接读不到
        （真机实测：在售页被判成 detail，导航因此走错路）。

        这块放大 6 倍读，只要几毫钟。页码是「在售还是详情」的唯一判据，
        所以低置信度（<0.6）的碎块一律丢掉，不能拿它下结论（review V07）。
        OCR 本身失败时抛 NavError，而不是返回空串冒充「没有页码」（V08）。
        """
        from . import ocr as _ocr
        x, y, w, h = self.px_box(_PAGE_NO_BOX)
        crop = img[y:y + h, x:x + w]
        if crop.size == 0:
            raise NavError(f"页码 ROI 超出画面：{self.px_box(_PAGE_NO_BOX)} / {img.shape}")
        big = vision.upscale(crop, 6)
        try:
            words = _ocr.read_words(big, raise_on_error=True)
        except Exception as e:
            raise NavError(f"读页码时 OCR 不可用：{e}") from e
        return "".join(w.get("text", "") for w in words
                       if float(w.get("score", 1.0)) >= min_score)

    def _read_text(self, img):
        """OCR 整屏，返回拼串。ocr_read 可注入，测试不用真 OCR。"""
        if self._ocr_read is None:
            from . import ocr as _ocr
            self._ocr_read = _ocr.read_words
        words = self._ocr_read(img)
        text = "".join(w.get("text", "") for w in words)
        if not self._ocr_injected:
            text += self._read_page_no(img)
        return text

    def where(self):
        """当前在哪一页。返回 (页面名, 拼串)。页面名认不出是 "unknown"。"""
        img = self._snap()
        text = self._read_text(img)
        return classify(text), text

    def tab_now(self):
        """当前选中的顶部标签（靠绿色选中框的像素位置判断）。

        采样行 y 和 x 范围都按实际分辨率缩放：标签栏在非 1080p 下是等比例的
        另一条线，写死 y=53 会采到空白（review V04/V02）。
        """
        img = self._snap()
        y = self.pt((0, _TAB_SAMPLE_Y))[1]
        x0 = self.pt((100, 0))[0]
        x1 = self.pt((600, 0))[0]
        min_run = max(20, int(60 * self.size[0] / 1920.0))
        return tab_from_pixels(img, y=y, x0=x0, x1=x1, min_run=min_run)

    def require(self, want, step=""):
        """断言当前页是 want 之一，不是就抛 NavError。

        want 可以是字符串或字符串元组。这是「假阳性」的解药：只有确认到
        了目标页，这一步才算成功。

        演练模式下不校验：屏幕上是用户当前的画面，不是我们走出来的页面，
        拿它断言只会误报。
        """
        if self.dry_run:
            self.say(f"  [演练] 跳过页面校验（{step or want}）", "warn")
            return None
        # 断言前先把游戏抢回前台：`where()` 读的是屏幕截图，前台一旦被
        # 别的窗口占住，读到的就是那个窗口的内容 —— 实测点完「购买」后
        # 读到「编辑(E)窗口(W)帮助(H)关于(A)」，于是把一次成功的导航判成
        # 失败（日志：「点购买：期望页面 ('detail','onsale')，实际 market」）。
        # 已经在前台时 ensure_foreground 立即返回，不额外花钱。
        hwnd = getattr(self.mouse, "_game_hwnd", None)
        ensure = getattr(self.mouse, "ensure_foreground", None)
        if hwnd and ensure:
            ensure(hwnd)
        page, text = self.where()
        wants = (want,) if isinstance(want, str) else tuple(want)
        if page in wants:
            self.say(f"  页面确认：{page}", "info")
            return page
        raise NavError(f"{step or '当前步骤'}：期望页面 {wants}，实际 {page}"
                       f"（OCR：{text[:80]}）")


    @staticmethod
    def _diff(a, b):
        """两张整屏图的差异比例。阈值 30 是实测出来的：正常画面波动 <2%，
        真正的界面切换 >10%。

        抓帧失败（锁屏、显示模式切换、mss 抛错）时 `_snap()` 会给 None，
        形状也可能变。这两种情况以前是 AttributeError/ValueError —— 不是
        NavError，会绕过上层的兜底把线程打挂（review V19）。一律当作「变了」。
        """
        if a is None or b is None:
            return 1.0
        if getattr(a, "shape", None) != getattr(b, "shape", None):
            return 1.0
        d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        return float((d > 30).mean())

    def _assert_safe(self, point, raw=False):
        """拒绝点到禁用坐标附近。半径 40px —— 比按钮间距（73px）的一半稍大，
        宁可误报也不能漏报。

        raw=True 表示 point 已经是真实屏幕像素（OCR/模板返回值），
        不能再缩放一次（review V02）。
        """
        x, y = (int(point[0]), int(point[1])) if raw else self.pt(point)
        for name, bad in FORBIDDEN.items():
            bx, by = self.pt(bad)
            if abs(x - bx) <= 40 and abs(y - by) <= 40:
                raise NavError(f"拒绝点击 ({x},{y})：太接近「{name}」({bx},{by})")

    def click(self, point, label="", expect_change=True, timeout=4.0, raw=False):
        """点一个标定好的点（**基准坐标**），等画面变化。

        raw=True 时 point 已经是真实屏幕像素，直接按原值点。坐标口径只有
        一处缩放：基准坐标 → 实际分辨率。以前 OCR 像素又被 pt() 乘一次缩放
        系数，2560x1440 下 y=345 的卡片点到了 460（下一张卡）—— 买错货的
        直接原因（review V02）。
        """
        self._assert_safe(point, raw=raw)
        x, y = (int(point[0]), int(point[1])) if raw else self.pt(point)
        return self._click_at(x, y, label or str(point), expect_change, timeout)

    def click_px(self, point, label="", expect_change=True, timeout=4.0):
        """点**真实屏幕像素**坐标（OCR / 模板返回的坐标，不再换算）。"""
        x, y = int(point[0]), int(point[1])
        self._assert_safe((x, y), raw=True)
        return self._click_at(x, y, label or str(point), expect_change, timeout)

    def _click_at(self, x, y, label, expect_change, timeout):
        """按真实像素点下去并等画面变化。

        **每次点击都校验游戏在前台**（Mouse 带了游戏窗口句柄时才有）：
        游戏不是前台时会忽略鼠标输入 —— 点击「成功」了但状态一点没变，
        表现是「点击后状态不对」「点了「筛选」但面板没打开」这类看不懂的
        报错（实测：控制台窗口或终端压着游戏时，点全落在那个窗口上）。
        早报「目标窗口不在前台」比事后猜要省事得多。
        """
        if self.dry_run:
            self.say(f"[演练] 点「{label}」({x},{y})", "warn")
            return 1.0

        before = self._snap()
        hwnd = getattr(self.mouse, "_game_hwnd", None)
        ok = self.mouse.click(x, y, verify=False, expect_foreground=hwnd)
        if ok is False:
            # 真机返回 0 表示这一击根本没送出去（窗口不在前台/被拦截），
            # 以前静默继续，上层看到的只是「画面没变化」（review I8）。
            raise NavError(f"点「{label}」({x},{y}) 失败：鼠标点击没有生效")
        if not expect_change:
            time.sleep(0.3)
            return 0.0

        best = 0.0
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            time.sleep(0.2)
            d = self._diff(before, self._snap())
            best = max(best, d)
            if d > 0.10:
                break
        self.say(f"点「{label}」({x},{y}) 变化 {best * 100:.1f}%"
                 + ("★" if best > 0.10 else "（无响应）"),
                 "info" if best > 0.10 else "warn")
        return best

    # ---------------- 各步骤 ----------------

    def open_market(self):
        """大厅按 F4 打开市场。

        F4 只能靠 PostMessage 送进去：SendInput 无论虚拟键还是扫描码都被
        游戏过滤（Raw Input 只认真实硬件事件），实测提权也一样。F4 只开
        不关——这是用户确认过的行为，别指望它来回切。
        """
        if self.dry_run:
            self.say("[演练] 按 F4 打开市场", "warn")
            return True
        if self.keyboard is None:
            raise NavError("没有 Keyboard，按不了 F4")
        before = self._snap()
        self.keyboard.tap("F4")
        best = 0.0
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 6.0:
            time.sleep(0.3)
            d = self._diff(before, self._snap())
            best = max(best, d)
            if d > 0.10:
                break
        self.say(f"F4 打开市场 变化 {best * 100:.1f}%"
                 + ("★" if best > 0.10 else "（无响应）"),
                 "info" if best > 0.10 else "warn")
        return best > 0.10

    def tab(self, name, settle=1.2):
        """切到顶部标签。name: mandel / collection / charm。

        校验方式是**读绿色选中框的位置**，不是「画面变了」。实测踩过坑：
        在售页点 (323,55) 只是关掉了筛选面板，画面变化 33.6%，看着像成功，
        其实标签栏根本没动。
        """
        table = {"mandel": TAB_MANDEL, "collection": TAB_COLLECTION,
                 "charm": TAB_CHARM}
        if name not in table:
            raise NavError(f"没有这个标签：{name}")
        if self.dry_run:
            self.say(f"[演练] 切标签 {name}", "warn")
            return True
        if self.tab_now() == name:
            self.say(f"已经在 {name} 标签上", "info")
            return True
        self.click(table[name], f"标签 {name}", timeout=3.0)
        time.sleep(settle)
        got = self.tab_now()
        if got != name:
            raise NavError(f"点标签 {name} 没切过去（绿框现在在 {got}）——"
                           f"可能不在市场页")
        self.say(f"标签确认：{got}", "info")
        return True

    def _ocr_words(self, img, strict=False):
        """读一片区域的词表。

        strict=True 时，「OCR 服务挂了」抛 NavError，与「这里真的没字」区分开
        （review V08）：以前异常被吞成 []，调用方据此报「面板上没有 X」。
        注入的测试 OCR 不走 strict —— 它不会失败，也不需要真服务。
        """
        if strict and not getattr(self, "_ocr_injected", True):
            from . import ocr as _ocr
            try:
                return _ocr.read_words(img, raise_on_error=True)
            except Exception as e:
                raise NavError(f"OCR 不可用：{e}") from e
        if self._ocr_read is None:
            from . import ocr as _ocr
            self._ocr_read = _ocr.read_words
        return self._ocr_read(img)

    def recover(self, max_esc=5):
        """从任意页面回到「市场」页，为后续步骤准备干净的起点。

        用户点开始时可能停在任意位置：大厅、在售页、筛选面板开着、典藏外观页。
        硬编码「从大厅按 F4」在那种情况下会失败 —— 实测踩过：起点在在售
        页且筛选面板开着，F4 只开不关所以毫无反应。

        分两种情况：
          在大厅 → 按 F4 打开市场（ESC 在大厅会弹出退出菜单，不能按）
          在市场内其它页 → 连按 ESC 逐层退出（ESC 只关面板/退页面，安全）

        返回最终页面名。识别不出就抛 NavError，不盲试。
        """
        if self.dry_run:
            self.say("[演练] 跳过页面复位", "warn")
            return "unknown"
        if self.keyboard is None:
            raise NavError("没有 Keyboard，没法复位")

        page, _ = self.where()
        self.say(f"复位前页面：{page}")
        if page == "market":
            self.say("已在市场", "info")
            return page

        if page == "hall":
            # 大厅按 F4 进市场。F4 只开不关，所以按一次就够。
            self.keyboard.tap("F4")
            time.sleep(2.0)
            page, _ = self.where()
            self.say(f"  F4 后：{page}")
            if page == "market":
                return page
            raise NavError(f"大厅按 F4 后仍不在市场（现在 {page}）")

        # 市场内的其它页（在售页/详情页/筛选面板）：ESC 逐层退出。
        # 不按超过 max_esc 次 —— 退到大厅再按 ESC 会弹出退出菜单。
        #
        # 「unknown」不能当成「继续按 ESC」：实测从筛选面板连按两次会进到
        # 一个认不出的画面（详情页残留 / 退出菜单过渡），再按就卡死。
        # 所以 unknown 连续出现两次就改按 F4 重进市场 —— F4 只开不关，
        # 已经在市场里时按它无副作用。
        unknown = 0
        for i in range(max_esc):
            # 面板开着时 ESC 只关浮层/面板，页面名可能还停在
            # filter_collection（面板里的词还在 OCR 结果里）。所以先看
            # 面板在不在，关掉了就再按一次 ESC 退回市场页。
            was_open = self._panel_open() or self._ons_filter_open()
            self.keyboard.tap("ESC")
            time.sleep(0.9)
            still_open = self._panel_open() or self._ons_filter_open()
            page, _ = self.where()
            self.say(f"  第 {i + 1} 次 ESC 后：{page}"
                     f"（面板 {was_open}→{still_open}）")
            if still_open:
                # ESC 只关了浮层/一层面板 —— 再来一次
                continue
            if page == "market":
                return page
            if page == "hall":
                # 退过头了，回到大厅；按 F4 重新进市场
                self.keyboard.tap("F4")
                time.sleep(2.0)
                page, _ = self.where()
                self.say(f"  退到大厅后按 F4：{page}")
                if page == "market":
                    return page
                break
            if page == "unknown":
                unknown += 1
                if unknown >= 2:
                    self.say("  连续认不出页面，按 F4 重进市场", "warn")
                    self.keyboard.tap("F4")
                    time.sleep(2.0)
                    page, _ = self.where()
                    self.say(f"  F4 后：{page}")
                    if page == "market":
                        return page
                    break
            else:
                unknown = 0
        raise NavError(f"复位失败，当前页面 {page}")

    def go_to_onsale(self, skin_y=None, skin_index=0, from_hall=True):
        """走完「市场 → 典藏外观 → 选皮肤 → 购买 → 在售页」。

        每一步都确认页面，任一步不对就抛 NavError，绝不带着错误的页面
        往下走 —— 那会把曼德尔砖当成典藏外观，买错东西。

        from_hall=True 时先按 F4 打开市场；已经在市场里时 F4 无响应是
        正常的（F4 只开不关），所以那一步失败不算错。

        skin_y / skin_index: 选哪张卡片。skin_y 优先；否则按 skin_index
        取 SKIN_CARD_Y（0 = 第一张，实测 y=272）。
        返回当前页面名。
        """
        if from_hall:
            self.open_market()
            time.sleep(1.5)
        page, _ = self.where()
        if page != "market":
            page = self.recover()
        self.tab("collection")
        if not self.pick_skin(y=skin_y, index=skin_index):
            raise NavError(f"点皮肤卡片没响应（y={skin_y} index={skin_index}）")
        time.sleep(1.0)
        self.buy()
        page, _ = self.where()
        return page


class Navigator(NavOnsPanel, NavColPanel, NavBase):
    """通用导航 + 两个页面面板拼出来的完整导航器。

    MRO 是 NavOnsPanel → NavColPanel → NavBase：面板方法优先于基类同名方法。
    三方目前没有同名方法，但顺序先定死，以后加方法时不会意外覆盖基类实现。
    """
