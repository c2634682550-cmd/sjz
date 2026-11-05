"""在售页（ONS_*）的筛选面板操作：顶部公示期筛选 + 价格 + 稀有度/成色/材质/颜色。

在售页的筛选面板会滚动，所以这里的坐标全是**相对量**：先读当前可见的方框，
再按区块锚点反推名字。坐标表在 core/nav_coords.py。

本模块只提供方法（纯 mixin，不定义 __init__），由 core/nav.py 的
`Navigator` 组合成可用的对象。
"""
import ctypes
import inspect
import re
import time

import numpy as np

from . import ocr, vision
from .nav_coords import (COL_FOLLOW, ONS_CONFIRM, ONS_FILTER, ONS_GRID,
                         ONS_GRID_ROW_H, ONS_GRID_TOP_Y,
                         ONS_MAX_PICK, ONS_NEXT, ONS_PREV, ONS_PRICE_BLUR,
                         ONS_PRICE_MAX, ONS_PRICE_MIN, ONS_RESET, ONS_TAB,
                         NavError, _ONS_ALIAS, _scaled)


# 筛选面板两列复选框的**兜底**列心（1920×1080 基准坐标）。
# 正常运行用不到它：列心由面板几何推导（见 Navigator._ons_columns），
# 几何或配置都算不出时才回落到这里（review R5）。
ONS_COLUMN_FALLBACK = (1396.0, 1641.0)
# 列心相对**面板宽度**的比例，由真机标定反推：
#   面板 1360-1900（宽 540），首列内缩 36px、列距 245px。
# 用比例不用像素：分辨率/面板宽度一变，列心跟着面板走。
ONS_COL_INSET_RATIO = 36.0 / 540.0
ONS_COL_PITCH_RATIO = 245.0 / 540.0
# 面板左右边界读不到时的兜底（基准坐标）。
ONS_PANEL_FALLBACK = (1360.0, 1900.0)


def _ons_nav_cfg():
    """读 config 的 nav 段；读不到/被改坏时返回 {}。"""
    try:
        from .config import Config
        nav = Config.load().get("nav")
        return nav if isinstance(nav, dict) else {}
    except Exception:                        # noqa: BLE001
        return {}


def _host_size(host):
    """取分辨率。测试替身只借方法、不带 size，按 1080p 处理（恒等换算）。"""
    return getattr(host, "size", (1920, 1080))


def _pt(host, point):
    return _scaled(point, *_host_size(host))


def _px_box(host, box):
    x, y, w, h = box
    x0, y0 = _pt(host, (x, y))
    x1, y1 = _pt(host, (x + w, y + h))
    return (int(x0), int(y0), max(1, int(x1 - x0)), max(1, int(y1 - y0)))


def _crop(host, img, box):
    x, y, w, h = _px_box(host, box)
    return img[y:y + h, x:x + w]


def _click_px(host, point, label="", **kw):
    """点真实像素坐标（OCR/模板返回值），兼容只有 click 的测试替身。"""
    fn = getattr(host, "click_px", None)
    if fn is not None and getattr(host, "mouse", None) is not None:
        return fn(point, label, **kw)
    return host.click(point, label, **kw)


def _words(host, img, strict=False):
    """读词表。测试替身常把 _ocr_words 重写成单参数版本，按签名降级。"""
    fn = host._ocr_words
    try:
        ps = inspect.signature(fn).parameters
        takes = ("strict" in ps
                 or any(p.kind == p.VAR_KEYWORD for p in ps.values()))
    except (TypeError, ValueError):
        takes = False
    return fn(img, strict=strict) if takes else fn(img)


def _ons_grid_name(block, row, col):
    """按 (区块, 行, 列) 查表拿选项名。查不到返回 None。"""
    if block is None or row is None:
        return None
    spec = ONS_GRID.get(block)
    if not spec:
        return None
    col_spec = spec[1] if col else spec[0]
    for r, name in col_spec:
        if r == row:
            return name
    return None


class NavOnsPanel:
    """在售页的操作集合（mixin，依赖 NavBase 提供的底层能力）。"""

    def to_onsale(self):
        """从「推荐」切回「在售」。已经在在售页就不点。"""
        if self.where()[0] == "onsale":
            return True
        self.click(ONS_TAB, "顶部·在售", expect_change=False)
        time.sleep(1.2)
        return self.where()[0] == "onsale"

    def ons_filter(self):
        """在售页打开筛选面板（价格/稀有度/成色/材质/颜色）。"""
        self.click(ONS_FILTER, "在售·筛选")
        time.sleep(1.0)
        self.require("filter_onsale", "打开在售筛选")
        return True

    def ons_filter_confirm(self):
        """在售页筛选面板点「确定」。"""
        self.click(ONS_CONFIRM, "筛选·确定")
        time.sleep(1.0)
        page, _ = self.where()
        if page == "filter_onsale":
            raise NavError("点「确定」没关掉筛选面板")
        return True

    # 在售页右上角的刷新图标（模板匹配 1.000，63x72 图标中心）。
    ONS_REFRESH = (1377, 195)

    def ons_refresh(self, wait=2.5):
        """点刷新，让列表按当前筛选重新拉一遍。

        改完筛选不会自动重排已加载的卡片 —— 旧卡片还带着旧状态。真机上
        「只显示公示期」切完不刷新，列表里还留着已经解锁的商品。
        """
        self.click(self.ONS_REFRESH, "刷新列表", expect_change=False)
        time.sleep(wait)
        return True

    # ---------------- 顶部公示期筛选 ----------------
    # 三个选项：不限公示期 / 只显示公示期 / 不显示公示期。倒计时模式必须切到
    # 「只显示公示期」—— 在售页那行「剩余：X分Y秒」是上架剩余时间，归零时
    # 商品直接下架，掐它毫无意义。
    PUB_BTN = (435, 197)
    PUB_BTN_BOX = (370, 178, 140, 34)
    # 菜单从按钮下沿展开。框从 y=235 起，把按钮自己那行排除掉 —— 否则
    # OCR 会把按钮上的当前值也当成一个选项，点它只是把菜单关掉。
    PUB_MENU_BOX = (330, 235, 460, 250)

    # 下拉文字只有 14px，原尺寸下 WinRT 会整行跳过笔画少的字，必须放大再 OCR。
    PUB_OCR_FX = 2.0

    # ---------------- 顶部排序 ----------------
    # 五个选项：默认排序 / 按价格升序 / 按价格降序 / 按成色升序 / 按成色降序。
    # 按钮在公示期下拉右边（1920×1080 实测：文字起点 x≈861）。
    SORT_OPTIONS = ("默认排序", "按价格升序", "按价格降序",
                    "按成色升序", "按成色降序")
    # 菜单从按钮下沿展开，比公示期那个长（5 项）。x 从 780 起，
    # 和公示期菜单（330~790）错开，互不干扰。
    SORT_MENU_BOX = (780, 235, 520, 340)
    # 顶部筛选栏里**排序那一块**。x 从 800 起：公示期下拉的右沿在 790 附近，
    # 再往左会把两个下拉的文字拼成一行（实测读出「只显示公示期默认排序」），
    # 既认不出选项，算出来的中心点还落在两个下拉中间，点下去什么都不是。
    TOP_BAR_BOX = (800, 170, 500, 75)

    def _crop_lines(self, box):
        """OCR 一个矩形区域，把词按行拼成整句（坐标回填成整屏坐标）。

        两个坑叠在一起：14px 小字要放大 2x 才读得出；读出来的又是**单个汉字**
        成词，直接拿「只」去匹配「只显示公示期」永远比不中。所以先 `vision.upscale`
        再 `ocr.phrases` 归行拼句，最后把坐标除回去。
        """
        img = self._snap()
        # 裁剪框是 1080p 基准坐标，必须按实际分辨率换算（review V02）
        x, y, w, h = _px_box(self, box)
        fx = self.PUB_OCR_FX
        sub = vision.upscale(img[y:y + h, x:x + w], fx)
        if self._ocr_read is None:
            from . import ocr as _ocr
            self._ocr_read = _ocr.read_words
        # gap 放大到 60：菜单里字距本来就宽（放大后 30px 左右），用默认的
        # 12 会把每个汉字切成独立词组，「只显示公示期」永远拼不起来。
        return [{"text": p["text"],
                 "x": x + p["x0"] / fx,
                 "y": y + p["y0"] / fx,
                 "w": (p["x1"] - p["x0"]) / fx,
                 "h": (p["y1"] - p["y0"]) / fx}
                for p in ocr.phrases(self._ocr_read(sub), gap=60)]

    def _pub_shows(self, want):
        """顶部那行筛选文字现在是不是 want（如「只显示公示期」）。"""
        try:
            crop = _crop(self, self._snap(), (60, 180, 940, 60))
            if getattr(crop, "size", 0) == 0:
                return False
            words = self._ocr_words(vision.upscale(crop, 2))
            text = "".join(w.get("text", "") for w in words)
            return want[:2] in text
        except Exception:
            return False

    # ---------------- 顶部排序（读/切） ----------------

    def _top_bar_items(self):
        """读顶部筛选栏的文字，返回 [{"text","cx","cy"}]，坐标是整屏坐标。"""
        return [{"text": r["text"],
                 "cx": int(r["x"] + r["w"] / 2),
                 "cy": int(r["y"] + r["h"] / 2)}
                for r in self._crop_lines(self.TOP_BAR_BOX)]

    def _sort_state(self, items=None):
        """返回 (顶部栏里那一项, 它对应哪个选项)。认不出返回 (None, None)。

        认法按可靠性排：
          1. **子串**——OCR 偶尔把相邻文字粘在一起（「只显示公示期默认排序」），
             整串相似度会被拉低到认不出，但选项本身还在串里
          2. 相似度——OCR 缺字时兜底（「默认排」「按价格升」）
        """
        items = items if items is not None else self._top_bar_items()
        for it in items:
            for opt in self.SORT_OPTIONS:
                if opt in it["text"]:
                    return it, opt
        best, best_opt, best_score = None, None, 0.0
        for it in items:
            for opt in self.SORT_OPTIONS:
                s = ocr.similarity(it["text"], opt)
                if s > best_score:
                    best, best_opt, best_score = it, opt, s
        if best_score < 0.8:
            return None, None
        return best, best_opt

    def _sort_now(self, items=None):
        """当前排序值（顶部栏里那项）。读不到返回 None。"""
        return self._sort_state(items)[1]

    def sort_by(self, want="默认排序", tries=2):
        """把顶部排序切到 want，返回是否切成功。

        为什么要能选：列表默认按上架时间排，「按价格升序」才是捡漏的起点
        —— 默认排序里最便宜的那件可能在第 3 页，而倒计时模式只看第一页。

        按钮位置靠 OCR 找当前值，不写死坐标：**每个等待都改成「生效就
        返回」**（原来固定 sleep 0.9+1.4=2.3 秒，现在正常情况 0.3 秒）。
        """
        if want not in self.SORT_OPTIONS:
            raise NavError(f"不认识的排序：{want}")
        items = self._top_bar_items()
        btn, now = self._sort_state(items)
        if now == want:
            self.say(f"排序已经是「{want}」，不用再切", "info")
            return True
        if btn is None:
            # 读不到当前值就别乱点 —— 多半是不在在售页，点下去会点错地方。
            # 把读到的内容打出来：不然只知道「找不到」，没法判断是坐标偏了
            # 还是 OCR 把「默认排序」读成了别的字。
            self.say("顶部读到：" + str([it["text"] for it in items]), "warn")
            raise NavError("顶部找不到排序下拉（当前可能不在在售页）")

        for _ in range(tries):
            # 排序当前值是 OCR 读到的**真实像素**，不能再过 pt()（review V02）
            _click_px(self, (btn["cx"], btn["cy"]), "排序下拉")
            # 菜单展开要一会儿：轮询到能读出选项就停（最坏 0.9s）
            rows, t0 = [], time.perf_counter()
            while time.perf_counter() - t0 < 0.9:
                rows = self._crop_lines(self.SORT_MENU_BOX)
                if any(r["text"] in self.SORT_OPTIONS for r in rows):
                    break
                time.sleep(0.08)
            # 菜单里的选项也按相似度认（「按价格升序 / 按价格降序」只差一个字，
            # 用前两字匹配会点错方向）
            hit, hit_score = None, 0.0
            for r in rows:
                s = ocr.similarity(r["text"], want)
                if s > hit_score:
                    hit, hit_score = r, s
            if hit_score < 0.8:
                hit = None
            if hit is None:
                self.say("排序下拉里读到：" + str([r["text"] for r in rows]), "warn")
                self.click((960, 700), "收起排序下拉", expect_change=False)
                time.sleep(0.3)
                continue
            _click_px(self, (int(hit["x"] + hit["w"] / 2),
                             int(hit["y"] + hit["h"] / 2)),
                      f"排序·{want}")
            # 生效就立刻返回（最坏 1.2s），不再固定 sleep
            t1 = time.perf_counter()
            while time.perf_counter() - t1 < 1.2:
                if self._sort_now() == want:
                    self.say(f"排序已切到「{want}」")
                    return True
                time.sleep(0.15)
        self.say(f"没能把排序切到「{want}」—— 请手动在顶部切一下", "warn")
        return False

    def pub_filter(self, want="只显示公示期", tries=3):
        """把顶部公示期筛选切到 want。返回是否切成功。

        每次都先确认下拉真的打开了（读到 want 那一行）再点，点完再读一次
        按钮文字确认生效 —— 只点不看会静默失败，真机上就是这么把筛选留在
        「不限公示期」的。
        """
        # 已经切好了就直接返回：这一步开面板→读菜单→点→校验要 5 秒，
        # 而倒计时模式每件都要跑一遍，白等这 5 秒可能就错过开点。
        if self._pub_shows(want):
            self.say(f"公示期筛选已经是「{want}」，不用再切", "info")
            return True
        for _ in range(tries):
            self.click(self.PUB_BTN, "公示期筛选")
            # 菜单展开要一会儿：轮询到读出选项就停（最坏 0.9s）。
            # 以前是固定 sleep(0.9)，快的时候白等。
            rows, t0 = [], time.perf_counter()
            while time.perf_counter() - t0 < 0.9:
                rows = self._crop_lines(self.PUB_MENU_BOX)
                if rows:
                    break
                time.sleep(0.08)
            # 用前两个字当键：「不限」「只显」「不显」互相区分得开，而末尾
            # 那个「期」真机 OCR 经常读成「恚」或直接丢掉。
            key = want[:2]
            hit = [r for r in rows if key in r["text"]]
            if hit:
                r = hit[0]
                _click_px(self, (int(r["x"] + r["w"] / 2),
                                 int(r["y"] + r["h"] / 2)), f"公示期·{want}")
                # 生效就立刻返回（最坏 1.4s）。切完这一下后面马上就要
                # 扫列表/点卡片，固定 sleep 全是白等。
                t1 = time.perf_counter()
                while time.perf_counter() - t1 < 1.4:
                    now = "".join(x["text"] for x in
                                  self._crop_lines(self.PUB_BTN_BOX))
                    if key in now:
                        self.say(f"公示期筛选已切到「{want}」")
                        return True
                    time.sleep(0.1)
            else:
                self.say("下拉里读到：" + str([r["text"] for r in rows]), "warn")
                self.click((960, 700), "收起公示期下拉", expect_change=False)
                time.sleep(0.4)
        self.say(f"没能把公示期筛选切到「{want}」—— 请手动在顶部切一下", "warn")
        return False

    # ---------------- 筛选面板滚动 ----------------

    # 滚动时鼠标停的位置：面板中部空白处，不会碰到任何选项或按钮。
    # 实测 (1600,600) 在材质区和颜色区之间的空隙里，滚它只滚面板。
    ONS_SCROLL_AT = (1600, 600)
    ONS_SCROLL_NOTCH = 120      # 一格滚轮的 delta

    def _ons_wheel(self, notches, at=None):
        """在面板上滚动。正数向上、负数向下。

        **必须用真实鼠标事件**（SetCursorPos + mouse_event）。实测
        `core.input.Mouse.wheel` 的 PostMessage 对游戏无效 —— 滚了没反应，
        这是和价格输入框同一类坑。
        """
        u = ctypes.windll.user32
        x, y = _pt(self, at or self.ONS_SCROLL_AT)
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.2)
        for _ in range(abs(int(notches))):
            delta = self.ONS_SCROLL_NOTCH if notches > 0 else -self.ONS_SCROLL_NOTCH
            u.mouse_event(0x0800, 0, 0, delta, 0)
            time.sleep(0.12)
        time.sleep(0.8)
        return True

    def ons_scroll_top(self, max_notches=25):
        """把筛选面板滚到顶部。

        **为什么需要这个**：`ons_options()` 靠「方框间竖直空隙」切分区块，
        再按固定顺序 rarity→condition→material→color→more 贴名字。如果
        滚动位置让某个区块完全看不见，顺序推断就会错位 —— 比如只看到
        材质和颜色两个区块时，无法知道材质前面还有没有稀有度和成色。

        滚到顶部后第一个可见区块一定是「稀有度」（或更靠上的价格区），
        顺序推断就唯一确定了。

        实测滚 25 格一定到底（面板内容约 1300px，一格约 53px）。
        """
        self._ons_wheel(max_notches)
        return True

    def ons_find(self, name, max_notches=25, step=3):
        """在整个面板里找某个选项，必要时滚动。找到返回该项，否则 None。

        返回的项 `text_pt` 是**当前屏**的坐标，可以直接点。

        **为什么不能只读一屏**：面板比一屏长（实测顶部只能看到 14 项，
        全量 23 项），「经典款」「彩绘款」这些在底部。只读一屏就报
        「面板上没有这个选项」是错的 —— 它只是不在这一屏。

        先读当前屏：已经在视野里就直接返回，不滚（避免白滚一次打乱面板）。
        """
        def onscreen(o):
            return 200 <= o["text_pt"][1] <= 1000

        hit = [o for o in self.ons_options(scroll_top=False)
               if o["name"] == name and onscreen(o)]
        if hit:
            return hit[0]

        self.ons_scroll_top(max_notches)
        prev_sig = None
        for _ in range(max(1, max_notches // step + 1)):
            opts = self.ons_options(scroll_top=False)
            hit = [o for o in opts if o["name"] == name and onscreen(o)]
            if hit:
                return hit[0]
            sig = tuple(sorted((o["name"], o["text_pt"][1]) for o in opts))
            if sig == prev_sig:
                break       # 滚不动了 = 到底
            prev_sig = sig
            self._ons_wheel(-step)
        return None

    def ons_price(self, lo=None, hi=None):
        """设价格区间。lo / hi 是整数（游戏金币），None 表示不改。

        返回读回的 (最低价, 最高价) —— 没设的一侧返回 None，读不出的也是
        None。**调用方必须比对读回值**：设不进去就等于没筛，继续按行挑货
        会买错档位。

        实测结论（2026-10-21 真机验证，见常量块注释）：
          * 点 (1550,159) 进最低价框、(1737,159) 进最高价框；
            旧值 (1590,168) 压在最低价框右边缘外，点了不聚焦，字全丢
          * 输入是**追加**，光标在末尾 → 必须先 Ctrl+A + Delete 清空
          * 清空后归 0，删不到空字符串，这是输入框的最小值行为
          * 逐位输入必须慢（见 _type_digits）：快一点就丢键，
            输 3000 只剩 30
        """
        for point, val, label in ((ONS_PRICE_MIN, lo, "最低价"),
                                  (ONS_PRICE_MAX, hi, "最高价")):
            if val is None:
                continue
            if val < 0:
                raise NavError(f"{label}不能是负数：{val}")
            self.click(point, f"筛选·{label}输入框", expect_change=False)
            time.sleep(0.5)
            self._clear_field()
            self._type_digits(str(int(val)))
            # 失焦才会把最后一位画出来；不失焦读到的是「30」这种半截值
            self.click(ONS_PRICE_BLUR, "价格行空白（失焦）", expect_change=False)
            time.sleep(0.8)
        got = self._read_price()
        self._log(f"  价格框读回 {got}（要 {lo}~{hi}）")
        return (got[0] if lo is not None else None,
                got[1] if hi is not None else None)

    def _clear_field(self):
        """清空当前聚焦的输入框：Ctrl+A 后 Delete。

        实测：BACKSPACE 连按也能清，但会停在 0；Ctrl+A + Delete 更干净。
        """
        for vk, hold in ((0x11, 0.03), (0x41, 0.03)):
            self._key_vk(vk, down=True, hold=hold)
        for vk in (0x41, 0x11):
            self._key_vk(vk, down=False)
        time.sleep(0.25)
        self._key_vk(0x2E, hold=0.08)   # Delete
        self._key_vk(0x2E, down=False)  # 必须抬起：卡住的键会吃掉后面的数字
        time.sleep(0.35)

    def _type_digits(self, text):
        """逐位输入，**每位按下再抬起**，间隔 0.9s。

        真机实测（2026-10-21，逐位读回验证）：
          * 只发 keydown 不发 keyup，键卡在按下状态 → 输 3000 只进去 30
          * 补了 keyup 但间隔 0.35s，仍然丢键（1000→10、3000→30）
          * 0.15s 按住 + 0.9s 间隔才一位不漏；4 位数约 4s，
            只在设筛选时付一次，值这个钱
        """
        for ch in text:
            if not ch.isdigit():
                raise NavError(f"价格只能输数字：{text!r}")
            self._key_vk(ord(ch), hold=0.15)
            self._key_vk(ord(ch), down=False)
            time.sleep(0.9)

    def _key_vk(self, vk, down=True, hold=0.05):
        """发一个虚拟键。走 keybd_event，不是 PostMessage。

        实测：PostMessage 的按键对输入框无效（数字进不去），
        必须用 keybd_event 走系统输入队列。
        """
        flags = 0 if down else 2
        ctypes.windll.user32.keybd_event(vk, 0, flags, 0)
        if down:
            time.sleep(hold)

    def _read_price(self):
        """读价格行两个框里的数字，返回 (最低价, 最高价)；读不出的一侧是 None。

        两个框**分别拼**，不靠「取前两个数」：
          * 旧实现把整行 OCR 结果按顺序取前两个数字。真机 OCR 会把
            2,050 切成 "2" + "050"，框里的金币图标又读成 "0"，
            「前两个」根本不对应两个框 —— 读回 (1000, 9) 这种鬼值就是
            这么来的。
          * 现在按 x 中线 1625 分左右，各自按 x 排序拼串再转整数。
          * 金币图标 h≈26px（放大 3x 后 77），比数字（46）高一大截，
            按高度滤掉。
        """
        img = self._snap()
        if img is None:
            return (None, None)
        # 价格行 ROI 是基准坐标；mid 也要按同一口径换算（review V02）
        x0, y0, w, h = _px_box(self, (1370, 140, 510, 50))
        words = self._ocr_words(vision.upscale(img[y0:y0 + h, x0:x0 + w], 3))
        mid = (int(_pt(self, (1625, 0))[0]) - x0) * 3
        left, right = [], []
        for w in words:
            t = str(w.get("text", "")).replace(",", "").replace("，", "")
            # 置信度低的碎块（图标、描边）当数字用会拼出鬼值（review V07）
            if float(w.get("score", 1.0)) < 0.6:
                continue
            if not t.isdigit() or w.get("h", 0) > 60:
                continue
            cx = w.get("x", 0) + w.get("w", 0) / 2.0
            (left if cx < mid else right).append((w.get("x", 0), t))
        def join(items):
            if not items:
                return None
            return int("".join(t for _, t in sorted(items)))
        return (join(left), join(right))

    def ons_next_page(self):
        """翻到下一页。返回新页码（读不到返回 None）。"""
        self.click(ONS_NEXT, "翻页·下一页", expect_change=False)
        time.sleep(1.5)
        return self.ons_page()

    def ons_prev_page(self):
        """翻到上一页。返回新页码（读不到返回 None）。"""
        self.click(ONS_PREV, "翻页·上一页", expect_change=False)
        time.sleep(1.5)
        return self.ons_page()

    # ---------------- 在售页筛选：材质 / 颜色 / 更多分类 ----------------

    # 区块标题文字 → 区块名。OCR 读到这些词就当锚点。
    _ONS_TITLES = {
        "稀有度": "rarity",
        "成色": "condition",
        "材质": "material",
        "颜色": "color",
        "更多分类": "more",
    }

    def _ons_anchors(self, words):
        """从 OCR 结果里找区块标题，返回 [(y, 区块名)]。

        标题文字是「稀有度」「成色」「材质」「颜色」「更多分类」。
        OCR 会把它们切成单字（「稀」「有」「度」），所以要把同一行
        相邻的字拼起来再比对。

        面板会滚动，所以锚点必须现读 —— 用固定 y 在滚动后全错。
        """
        # 行聚合：以前用 round(y/12) 分桶，y=54 和 y=66 这种跨桶边界的相邻字
        # 会被拆成两行，标题就永远拼不出来。改成「与上一行首字的 y 差 ≤8」的
        # 顺序聚类（review V21）。
        parts = sorted(words, key=lambda w: (w["y"], w["x"]))
        rows = []
        for w in parts:
            if rows and abs(w["y"] - rows[-1][0]["y"]) <= 8:
                rows[-1].append(w)
            else:
                rows.append([w])
        out = []
        for ws in rows:
            ws.sort(key=lambda w: w["x"])
            # 只拼 x 连续（间隙 < 20px）的字
            buf = []
            prev_x2 = None
            for w in ws:
                if prev_x2 is not None and w["x"] - prev_x2 > 20:
                    self._match_title(buf, ws[0]["y"], out)
                    buf = []
                buf.append(w["text"])
                prev_x2 = w["x2"]
            if buf:
                self._match_title(buf, ws[0]["y"], out)
        # 去重（同一区块只留最靠上的那个标题）
        seen = {}
        for y, block in sorted(out):
            if block not in seen:
                seen[block] = y
        return sorted((y, b) for b, y in seen.items())

    def _match_title(self, buf, y, out):
        """把拼好的字符串和标题表比对，命中就 append (y+8, block)。"""
        txt = "".join(buf)
        # 面板标题可能被 OCR 拆成两截、左边多出图标残字，startswith 判不到；
        # 改成包含匹配，并取命中里最长的那个标题（「更多分类」优先于「更多」）
        # （review V21）。
        best = None
        for title, block in self._ONS_TITLES.items():
            if title in txt and (best is None or len(title) > len(best[0])):
                best = (title, block)
        if best:
            out.append((y + 8, best[1]))

    def ons_options(self, scroll_top=True):
        """读当前面板里所有可见的复选框，返回 [{name, raw, box_pt, text_pt,
        on, block, row, col}]。

        为什么要现读而不是用固定坐标表：面板可以滚动，材质/颜色的 y 会
        整体上移（实测滚 4 格后材质从 y745 移到 y219，差 526px）。固定表
        只能对上「面板刚打开且没滚过」那一种状态。

        识别方式是**三通道**，按可靠性从高到低：

          1. 字形模板匹配（core/glyphs.py）→ 有模板且分数够就用它。覆盖
             稀有度「极品/优品」、成色 S/A/B/C、材质「其他」这些 14px 高的
             小字，**与滚动位置无关**
          2. OCR 读方框右边的文字 → 没有模板的选项（颜色区）靠它
          3. 按「框在第几行第几列」查 ONS_GRID 反推 → 最后兜底

        为什么字形排在 OCR 前面：这几处小字 OCR 放大 1-6 倍全空，偶尔出字
        也可能把「极品」读成「优品」。字形匹配是像素级比对（实测同一选项
        跨滚动位置 1.000、勾选态 0.978），有模板时严格更可信。

        通道 3 是 2026-10-21 之前的唯一兜底，它依赖滚动位置推断（区块归属
        靠竖直空隙切组 + 固定顺序），推断一错名字就错且毫无信号。现在它
        降到最后，只在没有模板、OCR 也没字时才用。

        返回项里的 `src` 记录名字来自哪条通道（glyph/ocr/grid），`ocr` 和
        `table` 是另外两条通道各自的结果 —— 三者不一致时说明有一路跑偏了，
        测试和排查都靠这个信号。

        三条通道都对不上时该行**跳过**，不猜名字 —— 宁可少一项，不能点错。

        scroll_top：**默认 True，先滚到面板顶部再读**。这是必须的 ——
        区块名字靠「竖直空隙切组 + 固定顺序推断」，如果某个区块完全
        滚出屏幕，顺序就推错了（实测滚 4 格后材质被当成 condition）。
        滚到顶部后第一个可见区块一定是稀有度，推断才唯一。
        只有调用方明确知道自己要读「当前这一屏」时才传 False。

        判勾选见 _ons_box_on()。
        """
        if scroll_top:
            self.ons_scroll_top()
        img = self._snap()
        if img is None:
            return []
        boxes = [b for b in self._ons_find_boxes(img)
                 if b["cy"] >= 120 and not self._ons_is_decoration(b)]
        # 面板区域是 1080p 基准坐标，先换算成实际像素再切（review V02）。
        # 左右边界走 nav.ons_panel，和 _ons_columns 推导用的是同一份几何（R5）。
        pl, pr = self._ons_panel_base()
        px0, py0, pw, ph = _px_box(self, (pl, 30, pr - pl, 1050))
        words = _words(self, img[py0:py0 + ph, px0:px0 + pw], strict=True)
        # 低置信度的碎块不当名字用：OCR 在 14px 小字上会吐 0.2~0.4 分的
        # 猜测，拿来当选项名就是「点了个名字像的错选项」（review V07）
        words = [{"x": w["x"] + px0, "x2": w["x"] + px0 + w["w"],
                  "y": w["y"] + py0, "text": w["text"],
                  "score": w.get("score", 1.0)} for w in words
                 if float(w.get("score", 1.0)) >= 0.5]

        # 先按「列 + 行距」把方框归到 (block, row, col)
        anchors = self._ons_anchors(words)
        # 面板在顶部时的区块首行坐标（真实像素），用于锚点缺失时的几何自证
        tops = {k: int(_pt(self, (0, y))[1]) for k, y in ONS_GRID_TOP_Y.items()}
        tol = max(4, int(10 * _host_size(self)[1] / 1080.0))
        # 左右列的分界 = 两列列心的中点（不要再写死 1520，R5）
        cols = getattr(self, "_ons_cols", None) or self._ons_columns()[0]
        grid = self._ons_assign_grid(boxes, anchors, tops, tol,
                                     col_split=(cols[0] + cols[1]) / 2.0)
        out = []
        for b, (block, row, col) in zip(boxes, grid):
            near = [w for w in words
                    if abs(w["y"] + 8 - b["cy"]) <= 15
                    and 0 <= w["x"] - (b["x"] + b["w"]) <= 70]
            near.sort(key=lambda w: w["x"])
            raw = "".join(w["text"] for w in near)
            ocr_name = _ONS_ALIAS.get(raw, raw) if raw else None
            from . import glyphs as _glyphs
            glyph_name = _glyphs.identify(img, b["cx"], b["cy"])[0]
            table = _ons_grid_name(block, row, col)
            if glyph_name:
                name, src = glyph_name, "glyph"
            elif ocr_name:
                name, src = ocr_name, "ocr"
            elif table:
                name, src = table, "grid"
            else:
                continue        # 三条通道都认不出，跳过
            tx = (int((near[0]["x"] + near[-1]["x2"]) / 2) if near
                  else b["cx"] + 40)
            out.append({
                "name": name,
                "raw": raw,
                "src": src,
                "ocr": ocr_name,
                "table": table,
                "box_pt": (b["cx"], b["cy"]),
                "text_pt": (tx, b["cy"]),
                "on": self._ons_box_on(img, b),
                "block": block,
                "row": row,
                "col": col,
            })
        return out

    @staticmethod
    def _ons_assign_grid(boxes, anchors=None, tops=None, tol=10, col_split=None):
        """把方框归到 (block, row, col)。

        分三步：

        1. **按竖直空隙切分区块**。实测行距 56px、区块间空隙 118-120px，
           差距一倍以上，用 gap >= 90 切分非常稳。这比靠标题 y 可靠 ——
           标题可能滚出屏幕，空隙不会。

        2. **给每个区块贴名字**。区块顺序固定为
           rarity → condition → material → color → more，
           用 OCR 标题锚点确定其中几个，剩下的按索引差推。**一个锚点都没有
           时不猜** —— 宁可让网格通道空着，也不按顺序硬贴（review V03）。

        3. **算区块内行号**：以该区块最靠上的框为第 0 行，用
           (y - 首行y) / 56 反推。左右两列共用同一套行号，所以某行
           右列没选项时左列不会错位。

        返回与 boxes 等长的 [(block, row, col)]。
        """
        if not boxes:
            return []
        order = ["rarity", "condition", "material", "color", "more"]

        # --- 1. 按空隙切组（先按 y 排序）---
        # 实测行距 56px。同一区块内的行间距正好 56；区块之间的空隙
        # 明显更大（稀有度→成色 175px、成色→材质 230px、材质→颜色 174px）。
        # 但**成色最后一行（C）到材质首行（不限）只有 118px**，而材质
        # 区块内部 745→801 也是 56 —— 所以阈值不能设 90 以下，也不能
        # 设 130 以上。取 100 能同时分开这两类。
        srt = sorted(boxes, key=lambda b: b["cy"])
        groups = [[srt[0]]]
        for b in srt[1:]:
            if b["cy"] - groups[-1][-1]["cy"] >= 100:
                groups.append([b])
            else:
                groups[-1].append(b)

        # --- 2. 贴名字 ---
        # 每个组的「首行 y」和标题锚点比对：锚点 y + 56 就是该组首行
        names: list = [None] * len(groups)
        if anchors:
            for gi, g in enumerate(groups):
                top = g[0]["cy"]
                for ay, block in anchors:
                    # 标题在首行上方约 56px（实测：材质标题 y689，首行 y745）
                    if abs(top - (ay + 56)) <= 40:
                        names[gi] = block
                        break
        # 用已知名字推未知的（区块顺序固定）
        if any(n is not None for n in names):
            known = [(i, order.index(n)) for i, n in enumerate(names)
                     if n is not None]
            for gi in range(len(groups)):
                if names[gi] is not None:
                    continue
                # 找最近的已知组，按索引差推
                nearest = min(known, key=lambda kv: abs(kv[0] - gi))
                idx = nearest[1] + (gi - nearest[0])
                if 0 <= idx < len(order):
                    names[gi] = order[idx]
        if not any(n is not None for n in names) and tops:
            # 一个锚点都没有：**不按顺序猜**（review V03/V21）。唯一例外是
            # 几何自证 —— 某组的首行正好落在「面板滚到顶部」时该区块首行的
            # 位置上（±tol 像素，实测整像素滚动）。而且要求命中的区块序列
            # 是 rarity 开头的连续前缀：面板滚过之后位置全偏，单组命中的
            # 概率不为零（稀有度第 3 行 ≈ 成色首行），前缀约束能挡住它。
            cand = []
            for g in groups:
                hit = None
                for block, ty in tops.items():
                    if abs(g[0]["cy"] - ty) <= tol:
                        hit = block
                        break
                cand.append(hit)
            idx = [order.index(b) for b in cand if b]
            if idx and idx == list(range(len(idx))):
                names = cand

        # --- 3. 算行号 ---
        out: list = [None] * len(boxes)
        idx_of = {id(b): i for i, b in enumerate(boxes)}
        for gi, g in enumerate(groups):
            block = names[gi]
            base = g[0]["cy"]
            for b in g:
                row = round((b["cy"] - base) / ONS_GRID_ROW_H)
                # 左右列分界由调用方给（两列列心中点）。没给时按单列处理，
                # 不再写死 1520（review R5）。
                col = 0 if (col_split is None or b["cx"] < col_split) else 1
                out[idx_of[id(b)]] = (block, row, col)
        return out

    def _ons_is_decoration(self, b):
        """这个「方框」是不是装饰，而不是可勾选的复选框。

        真机上会被误检成方框的东西：
          - 价格输入框里的金币图标（y≈170，在价格区内）
          - 「〔可选至多2种〕」里的「〔」括号（y≈698，只有 26x20）
          - 面板右边缘的滚动条/头像（x>1860，已在 find_boxes 排除）

        判据：
          1. y < 200 一律排除（价格区，面板选项都在 300 以下）
          2. 尺寸明显偏小（宽或高 < 24）的排除 —— 真选项统一 29x29

        两个数字都是基准坐标，按实际分辨率换算（review V02）。
        """
        price_line = _pt(self, (0, 200))[1]
        if b["cy"] < price_line:
            return True
        small = _pt(self, (24, 24))
        small = min(int(small[0]), int(small[1]))
        if b["w"] < small or b["h"] < small:
            return True
        return False

    def _ons_panel_base(self):
        """筛选面板左右边界（1920×1080 基准坐标，整数）。

        优先 config `nav.ons_panel`，坏了用 ONS_PANEL_FALLBACK。
        """
        panel = _ons_nav_cfg().get("ons_panel")
        try:
            a, b = float(panel[0]), float(panel[1])
            if b > a:
                return a, b
        except (TypeError, ValueError, IndexError):
            pass
        return ONS_PANEL_FALLBACK

    def _ons_columns(self):
        """两列复选框的列心（当前分辨率像素）+ 来源标签。

        推导链（review R5）：
          1. **几何**：列心 = 面板左边界 + 面板宽 × 首列内缩/列距比例。
             面板边界与宽度都来自 nav.ons_panel，改版后只改这一处。
          2. 几何算不出（配置缺失、面板宽 ≤ 0、换算后顺序反了）→
             配置 nav.ons_columns（兜底标定）
          3. 配置也没有/被改坏 → 代码里的 ONS_COLUMN_FALLBACK
        """
        left, right = None, None
        panel = _ons_nav_cfg().get("ons_panel")
        try:
            a, b = float(panel[0]), float(panel[1])
            if b > a:
                left, right = a, b
        except (TypeError, ValueError, IndexError):
            pass
        if left is not None:
            width = right - left
            ca = left + width * ONS_COL_INSET_RATIO
            cb = ca + width * ONS_COL_PITCH_RATIO
            pa = float(_pt(self, (ca, 0))[0])
            pb = float(_pt(self, (cb, 0))[0])
            if pb > pa > 0:
                return (pa, pb), "几何"
        cols = _ons_nav_cfg().get("ons_columns")
        try:
            ca, cb = float(cols[0]), float(cols[1])
            src = "配置标定"
        except (TypeError, ValueError, IndexError):
            ca, cb = ONS_COLUMN_FALLBACK
            src = "兜底"
        return (float(_pt(self, (ca, 0))[0]),
                float(_pt(self, (cb, 0))[0])), src

    def _ons_warn_columns(self, cols, src, off, tol):
        """列位对不上、过滤后一个框都没剩时留一条可查日志（review R5）。

        真机改版/换分辨率后列会整体偏移，块头全部被列约束丢掉，表面
        现象是「面板上一项都读不到」。这里把实测列位和判定列位都打出来，
        并指出改哪个配置。60 秒节流，避免每轮都刷屏。
        """
        log = getattr(self, "_log", None)
        if log is None:
            return
        now = time.time()
        if now - float(getattr(self, "_ons_colwarn_at", 0) or 0) < 60:
            return
        try:
            self._ons_colwarn_at = now
        except Exception:                    # noqa: BLE001
            pass
        seen = sorted({int(cx) for cx in off})[:6]
        cols_s = "/".join(str(int(c)) for c in cols)
        try:
            log(f"筛选面板列位对不上：实测方框 x={seen}（容差 ±{int(tol)}px），"
                f"判定列心 x={cols_s}（来源：{src}）；块头全被过滤，选项可能"
                f"一个都读不到。请按实测列位改 config nav.ons_panel / "
                f"nav.ons_columns", "warn")
        except Exception:                    # noqa: BLE001
            pass

    def _ons_find_boxes(self, img):
        """在右侧面板找复选框外框（29x29 见方）。

        真机上每个框会被边缘检测**检出两次**：外框 29x29 和内框 23x23，
        两者中心只差 3px。不去重会让每个选项出现两次，其中一个还带
        错误的勾选状态（内框读到的白像素数不同）。

        去重规则：中心距离 < 12px 视为同一个框，**保留面积大的那个**
        （外框 29x29 才是完整的框，内框 23x23 只是它的一部分）。

        另外排除三类误检：
          - x 超出面板右边界：滚动条/头像，不是选项
          - **列约束**：选项框只出现在两列上（列心见 `_ons_columns`，
            由面板几何推导，不再在这里写死 1396/1641），
            离两列超过 6px 的框一律不是选项 —— 以前只按尺寸筛，面板里
            任何一处 29x29 的方角（分割线端头、滚动条端头）都会被当成选项
            （review V06 / R5）
          - 同一行最多两个框（左右两列各一个），多出来的丢掉
        """
        import cv2 as _cv
        pl, pr = self._ons_panel_base()
        x0, y0 = _pt(self, (pl, 30))
        x1 = int(_pt(self, (pr, 0))[0])
        x0, y0 = int(x0), int(y0)
        if x1 <= x0:
            return []
        g = _cv.cvtColor(img[y0:, x0:x1], _cv.COLOR_BGR2GRAY)
        edges = _cv.Canny(g, 20, 60)
        edges = _cv.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)
        cnts, _ = _cv.findContours(edges, _cv.RETR_LIST,
                                   _cv.CHAIN_APPROX_SIMPLE)
        sin = _pt(self, (22, 0))[0]
        tol = max(3, int(6 * self.size[0] / 1920.0)) if hasattr(self, "size") else 6
        cols, cols_src = self._ons_columns()
        self._ons_cols = cols
        self._ons_cols_src = cols_src
        found = []
        off = []        # 尺寸合格但不在列上的框：列位偏移的唯一线索
        for c in cnts:
            x, y, w, h = _cv.boundingRect(c)
            if not (int(sin) <= w <= int(sin * 1.64) and
                    int(sin * 0.82) <= h <= int(sin * 1.55)):
                continue
            if abs(w - h) > 10:
                continue
            cx = x + x0 + w // 2
            cy = y + y0 + h // 2
            if min(abs(cx - cols[0]), abs(cx - cols[1])) > tol:
                off.append(cx)
                continue        # 不在两列上，不是选项框
            found.append({"x": x + x0, "y": y + y0, "w": w, "h": h,
                          "cx": cx, "cy": cy})
        # 去重：中心 <12px 合并，保留面积最大的
        found.sort(key=lambda b: -b["w"] * b["h"])
        uniq = []
        for b in found:
            if any(abs(b["cx"] - u["cx"]) < 12 and
                   abs(b["cy"] - u["cy"]) < 12 for u in uniq):
                continue
            uniq.append(b)
        # 同一行（±8px）最多两个框，多出来的按「离两列更近」保留前两个
        band = max(6, int(8 * _host_size(self)[1] / 1080.0))
        rows = {}
        for b in uniq:
            key = None
            for k in rows:
                if abs(b["cy"] - k) <= band:
                    key = k
                    break
            rows.setdefault(b["cy"] if key is None else key, []).append(b)
        kept = []
        for grp in rows.values():
            grp.sort(key=lambda b: min(abs(b["cx"] - cols[0]),
                                       abs(b["cx"] - cols[1])))
            kept.extend(grp[:2])
        if not kept and off:
            # 全被列约束丢掉了：列位偏移/面板改版最典型的现象。
            self._ons_warn_columns(cols, cols_src, off, tol)
        return sorted(kept, key=lambda b: (b["cy"], b["cx"]))

    @staticmethod
    def _ons_box_on(img, b):
        """框内是否勾选。**三种状态，不是两种**（2026-10-21 真机实测）：

            白像素   0  → 未勾选（空框）
            白像素  65  → 已勾选，具体档位（画的是对勾 ✓）
            白像素 196  → 已勾选，「不限」这类（画的是实心白块）

        所以阈值必须取 20-30 这一档，不能取一半（≥136）—— 那会把
        65 的对勾判成未勾选，导致「明明勾上了却报状态不对」。
        0 与 65 之间是断崖，判据极稳。
        """
        import cv2 as _cv
        ix = b["x"] + int(b["w"] * 0.22)
        iy = b["y"] + int(b["h"] * 0.22)
        iw = max(4, int(b["w"] * 0.56))
        ih = max(4, int(b["h"] * 0.56))
        sub = _cv.cvtColor(img[iy:iy + ih, ix:ix + iw], _cv.COLOR_BGR2GRAY)
        if sub.size == 0:
            return None
        return int((sub > 150).sum()) > 30

    def ons_check(self, name, want=True, options=None):
        """按选项名勾/取消一个复选框。返回是否真的做了动作。

        点击目标是**方框右边的文字**（用户实测确认）。点方框本身也能
        切换，但方框边缘只有 1-2px 亮线，容易点到框外。

        name 必须精确匹配面板上的选项名（如 "速度线" / "黑色"）。
        找不到就抛 NavError —— 不猜、不点相近的项，那会买错。

        **选项可能在屏幕外**：面板比一屏长（顶部只见 14 项，全量 23 项），
        「经典款」「彩绘款」在底部。所以这里用 `ons_find()` 边滚边找，
        找不到才报错 —— 不能只读一屏就断定「面板上没有」。
        """
        if options is not None:
            hit = [o for o in options if o["name"] == name]
            o = hit[0] if hit else None
        else:
            o = self.ons_find(name)
        if o is None:
            names = [x["name"] for x in self.ons_options()]
            raise NavError(f"筛选面板上没有 {name!r}，可见项：{names}")

        if o["on"] == want:
            self.say(f"{name} 已经是{'勾选' if want else '未勾选'}状态，跳过")
            return False
        # 选项文字的 x/y 都是 OCR 读到的**真实像素**（review V02）
        x, y = int(o["text_pt"][0]), int(o["text_pt"][1])
        self.mouse.click(x, y, verify=False)
        time.sleep(0.7)
        after = self.ons_options(scroll_top=False)
        got = [a for a in after if a["name"] == name]
        if not got:
            # 点完可能因自动滚动而位置变了，再找一次
            got2 = self.ons_find(name)
            if got2 is None:
                raise NavError(f"点 {name} 后读不到该选项了")
            got = [got2]
        if got[0]["on"] != want:
            if self._ons_clear_via_all(o, after):
                return True
            raise NavError(f"点 {name} 后状态不对"
                           f"（期望{'勾选' if want else '取消'}，实际 {got[0]['on']}）")
        self.say(f"{'勾' if want else '取消'} {name} ({x},{y}) 成功", "hit")
        return True

    def _ons_clear_via_all(self, o, after):
        """取消勾选没生效时的退路：改选该区块的「所有…」项。

        **真机实测（2026-10-21）**：稀有度和成色这两组**不允许一项都不选**。
        勾上「极品」后「所有稀有度」自动取消，再点「极品」没有任何反应 ——
        点文字、点字母本身、点方框中心都一样，不是点击位置的问题。
        要清掉这组只能点回「所有稀有度」。材质和颜色组没有这个限制
        （取消「速度线」后可以一项都不选，实测通过）。

        语义上等价：调用方要的是「不要再按极品过滤」，选「所有稀有度」
        正好达成，而且比「点不动就报错」更接近用户意图。

        只在**该区块只剩这一项勾着**时才走这条路。还有别的项勾着说明
        不是「最后一项」问题，直接报错更安全 —— 别静默改成别的筛选条件。
        """
        block = o.get("block")
        spec = ONS_GRID.get(block)
        if not spec:
            # 区块名认不出来时（标题锚点缺了）按**选项名**反查所属区块：
            # 「极品」只在稀有度里有、字母只在成色里有，名字本身就能定位。
            # 命中多个区块（「不限」这种三组都有的）就不猜，直接放弃。
            hits = [b for b, sp in ONS_GRID.items()
                    if any(o["name"] == nm for col in sp for _r, nm in col)]
            if len(hits) != 1:
                return False
            spec = ONS_GRID[hits[0]]
        all_name = spec[0][0][1]        # 区块首行左列就是「所有…」
        if all_name == o["name"]:
            return False
        # 同区块的其他勾选项：按区块的选项名集合判，不依赖 a["block"]
        # （区块名可能是空的）
        names = {nm for col in spec for _r, nm in col}
        others = [a for a in after
                  if a["name"] in names and a["on"] and a["name"] != o["name"]]
        if others:
            return False
        target = [a for a in after if a["name"] == all_name]
        if not target:
            return False
        x, y = int(target[0]["text_pt"][0]), int(target[0]["text_pt"][1])
        self.mouse.click(x, y, verify=False)
        time.sleep(0.7)
        check = [a for a in self.ons_options(scroll_top=False)
                 if a["name"] == all_name]
        if check and check[0]["on"]:
            self.say(f"取消 {o['name']} 不被支持，已改选 {all_name}", "warn")
            return True
        return False

    def ons_checks(self, names, want=True):
        """批量勾选。超过 ONS_MAX_PICK 直接拒绝 —— 游戏只让选 2 个。

        每一项都**重新找**（`ons_check` 内部走 ons_find），不缓存上一项的
        选项表 —— 勾选具体档位会让「不限」自动取消，缓存会读到过期状态。
        """
        if want and len(names) > ONS_MAX_PICK:
            raise NavError(f"最多选 {ONS_MAX_PICK} 种，收到 {len(names)} 个：{names}")
        done = []
        for n in names:
            if self.ons_check(n, want=want):
                done.append(n)
        return done

    # ---------------- 关注（星标） ----------------

    def follow_state(self):
        """当前选中的这件挂单「关注」了没有。

        判据是**颜色**不是 OCR：星标只有 14px，点一下从灰白变金色
        （实测 RGB 约 230,205,95 → R 比 B 高 130 左右），这个差别一眼能分，
        OCR 反而认不出一个小星星。

        注意它读的是**交易行右侧详情面板**顶部那个星（标题右边、放大镜左边），
        不是典藏外观页的「★我的关注」入口。
        """
        img = self._snap()
        x, y = _pt(self, COL_FOLLOW)
        patch = img[int(y) - 10:int(y) + 10, int(x) - 10:int(x) + 10]
        if patch.size == 0:
            return False
        # **只统计星本身的亮像素**：取样框里大部分是深色底，直接取全框平均
        # 会把金色的 R-B 从 135 稀释到 43，判成「未关注」（实测踩过）。
        mask = patch.max(axis=2) > 110
        if int(mask.sum()) < 20:
            return False                                  # 连星都没有
        px = patch[mask].astype("float32")
        b, _g, r = px.mean(axis=0)                        # OpenCV 是 BGR
        return bool(r - b > 40)

    def set_follow(self, want=True, wait=0.45, skip_done=True):
        """把当前这件挂单关注 / 取消关注。状态已经对就不点。

        wait：点完等界面刷新多久再读颜色。游戏卡的时候 0.45s 不够，
        颜色还没变回来就会被判成「没点动」（见 config 的 follow_wait_ms）。
        """
        now = self.follow_state()
        if now == want:
            if skip_done:
                self.say(f"关注状态已经是{'关注' if want else '未关注'}，跳过", "info")
                return now
            # skip_done=False：不管当前状态都点一下。用于「星标颜色读不准」
            # 的界面版本 —— 宁可盲点一次（可能反而取消），也别卡着不动。
            self.say("按设置强制点一次星标（不读当前状态）", "warn")
        x, y = _pt(self, COL_FOLLOW)
        self.mouse.click(x, y, verify=False)
        time.sleep(max(0.05, float(wait)))
        after = self.follow_state()
        if after != want:
            self.say(f"关注没点动（星标颜色没变，期望{'金' if want else '灰'}）", "warn")
        return after

    def ons_page(self):
        """读在售列表的页码，返回整数（读不到返回 None）。

        「第N页」文字在 y937-956 / x720-764，6x 放大才稳定读出（1x/4x 全空）。

        注意裁剪宽度：早期用 x=690-800（110px），把两侧箭头切掉了，
        于是误判「没有翻页箭头」。现在裁 x=470-1010，覆盖
        ONS_PREV(515,945) / 页码(742,946) / ONS_NEXT(969,945) 三处。
        """
        img = self._snap()
        if img is None or getattr(img, "size", 0) == 0:
            return None
        crop = _crop(self, img, (470, 915, 540, 60))
        if getattr(crop, "size", 0) == 0:
            return None
        # 低置信度的碎块不能拿来拼页码（review V07）
        words = [w for w in _words(self, vision.upscale(crop, 6), True)
                 if float(w.get("score", 1.0)) >= 0.6]
        flat = "".join(w.get("text", "") for w in words)
        m = re.search(r"第\s*(\d+)\s*页", flat)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*页", flat)
        return int(m.group(1)) if m else None

    def _ons_filter_open(self):
        """在售页的筛选面板是否开着。

        判据：**「价格区间」四个字**（y110-135 / x1382-1460）。这个位置是
        在售筛选面板独占的，页面上别处没有。

        注意和 `_panel_open()`（典藏外观的筛选面板）是两个不同的面板：
        典藏外观那个标题是「筛选」在 y237，在售这个标题在 y56、「价格
        区间」在 y110。混用会让 `recover()` 判不出面板还在，连按 ESC
        卡死（实测踩过）。
        """
        img = self._snap()
        if img is None or getattr(img, "size", 0) == 0:
            return False
        crop = _crop(self, img, (1370, 100, 150, 40))
        if getattr(crop, "size", 0) == 0:
            return False
        # strict：OCR 挂了就抛 NavError，不能返回 False 让 recover() 以为
        # 「面板已经关了」然后空转（review V08/V18）
        words = _words(self, vision.upscale(crop, 3), strict=True)
        flat = "".join(w.get("text", "") for w in words
                       if float(w.get("score", 1.0)) >= 0.6)
        return "价" in flat and "格" in flat
