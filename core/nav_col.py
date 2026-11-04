"""典藏外观页（COL_*）的筛选面板与皮肤卡片操作。

这一页的操作分两段：面板里的复选框（赛季/拥有/品阶/枪种），以及选中皮肤后
点购买。两者共用同一批实测坐标，坐标表在 core/nav_coords.py。

本模块只提供方法（纯 mixin，不定义 __init__），由 core/nav.py 的
`Navigator` 组合成可用的对象。
"""
import re
import time

from . import ocr, vision
from .nav_coords import (COL_BUY, COL_CONFIRM, COL_FILTER, COL_GUN_X, COL_GUN_X2,
                         COL_GUN_Y, COL_NOT_OWNED, COL_OWNED, COL_RARITY_X,
                         COL_RARITY_Y, COL_SEASON, COL_SEASON_ITEM_X,
                         COL_SEASON_MAX_SCROLL, COL_SEASON_WHEEL_ANCHOR,
                         COL_TRADE_TAB, NavError,
                         SKIN_CARD_X, SKIN_CARD_Y)


class NavColPanel:
    """典藏外观页的操作集合（mixin，依赖 NavBase 提供的底层能力）。"""

    def col_filter(self):
        """典藏外观页打开筛选面板（赛季/拥有/品阶/枪种）。

        **面板已经开着就直接返回**，不要再点「筛选」——那个按钮是开关，
        再点一次会把面板关掉。早期没这个判断，`_click_season_item` 重试时
        调 col_filter() 反而把面板关上了，之后所有点击都落空（实测
        「点筛选变化 0.7%（无响应）」刷满日志）。

        **先确认在典藏外观标签页**：recover() 之后可能停在曼德尔砖页，
        那时点 (275,206) 是别的按钮，面板打不开。tab() 有幂等判断，
        已经在目标标签上就不动。

        **还要确认在「交易」二级页签上**：典藏外观下面有「交易 / 单据」
        两个页签，皮肤列表和筛选面板都在「交易」里；停在「单据」（购买
        记录）那一页时点「筛选」毫无反应 —— 那一页压根没有这个按钮
        （实测报「点了「筛选」但面板没打开」刷满日志）。面板打不开就
        先切回「交易」再试一次。
        """
        if self.dry_run:
            self.say("[演练] 打开典藏外观筛选", "warn")
            return True
        if self._panel_open():
            self.say("筛选面板已经开着", "info")
            return True
        if self.tab_now() != "collection":
            self.say("当前不在典藏外观标签，先切过去", "info")
            self.tab("collection")
            time.sleep(0.8)
        for attempt in (1, 2):
            self.click(COL_FILTER, "典藏外观·筛选")
            time.sleep(0.8)
            if self._panel_open():
                return True
            if attempt == 1:
                self.say("筛选面板没打开 —— 可能在「单据」页签上，切回「交易」",
                         "warn")
                self.click(COL_TRADE_TAB, "交易页签", expect_change=True)
                time.sleep(0.8)
        raise NavError("点了「筛选」但面板没打开")

    def col_filter_confirm(self):
        """关掉典藏外观的筛选面板。"""
        self.click(COL_CONFIRM, "筛选·确认")
        time.sleep(0.8)
        page, _ = self.where()
        if page == "filter_collection":
            raise NavError("点「确认」没关掉筛选面板")
        return True

    # ---------------- 典藏外观筛选：赛季下拉 ----------------
    #
    # 实测行为（2026-10-20 真机）：
    #   点 (615,334) 展开一列浮层，第一项「全部赛季」文字中心 y≈379，
    #   每项间距 48px，一屏见 6 项；滚轮向下滚一格 = 列表上移 1 项。
    #   选项是皮肤系列名（棱镜攻势/美杜莎/电玩高手/命运王牌/气象感应/
    #   棱镜攻势S2/悬赏令/电玩高手S2/造物纪元/疾光魅影），共 10 项 + 全部赛季。
    #
    # 为什么不能只点固定 y：列表比一屏长，目标项可能在需要滚动之后。
    # 所以先滚到顶，再逐项下滚，每步 OCR 读当前可见项，找到目标才点。

    # 面板本身固定出现的词。浮层没展开时 OCR 会读到这些，必须排除，
    # 否则会把「已拥有」「传说品阶」「步枪」当成下拉选项 —— 真机踩过，
    # 于是 col_pick_season 以为浮层已开，不点开就去读，永远找不到目标。
    # 从浮层 OCR 结果里剔除的**面板固定词**（不是下拉选项）。
    #
    # 注意「赛季」不能按子串剔除：「全部赛季」是合法的下拉选项，早期写成
    # `any(w in t for w in _PANEL_WORDS)` 会把「全部赛季」当成面板标签剔掉，
    # 导致列表里永远看不到第一项 —— 真机上表现为「浮层默认不在顶部」，
    # 其实是自己读漏了。所以这里只放**整串相等**才剔除的词。
    _PANEL_WORDS = ("已拥有", "未拥有", "品阶", "枪种", "查看", "赛季")

    # 浮层左边框：实测 x=566 有一整条竖线（y340-660 里 320 个像素全部
    # 亮于 70，均值 ~108），浮层收起时同一位置最大亮度只有 61、亮像素 0。
    # 这是比「探针区有没有字」可靠得多的判据 —— 探针区固定在 y355-400，
    # 浮层滚到第 3 屏时首项在 y=395，字正好卡在探针区边缘被切掉，于是
    # 浮层明明开着也报 False（实测「造物纪元」就这么被判「没展开」）。
    _DROPDOWN_BORDER_X = 566
    _DROPDOWN_BORDER_Y = (340, 660)

    def _dropdown_open(self):
        """浮层是否展开（自己抓屏）。"""
        return self._dropdown_open_img(self._snap())

    def _dropdown_open_settled(self, tries=2, gap=0.12):
        """判浮层开没开，读不到再确认一次。

        **单次判断不可靠。** 判据是「面板上能不能读出赛季选项名」，而 OCR
        偶尔就是读不到 —— 这时会把「明明开着」判成「没开」，于是再点一下
        把它收起来，下一轮又判「没开」再点一下才展开。用户看到的就是
        「赛季下拉点了两次」（每次还白等 0.9 秒）。
        """
        for i in range(tries):
            if self._dropdown_open():
                return True
            if i + 1 < tries:
                time.sleep(gap)
        return False

    def _dropdown_open_img(self, img):
        """浮层是否展开。

        两种判据，从严到宽：
          1. 贴边布局：浮层左边框那条竖线（x=566 / y340-660 亮像素 320/320）
          2. **任意布局**：面板上能不能读出赛季选项名（居中浮层时竖线不在
             那个位置，但选项名照样在）

        第 2 条是后来加的：面板在「列表空」时会变成居中浮层，浮层的
        位置跟着变，第 1 条判据就废了（实测「下拉展不开」「不在可见范围」，
        整个赛季选择卡死）。
        """
        if img is None or getattr(img, "size", 0) == 0:
            return False
        if not self._panel_open_img(img):
            return False
        x = self._DROPDOWN_BORDER_X
        y0, y1 = self._DROPDOWN_BORDER_Y
        h, w = img.shape[:2]
        if y1 <= h and x < w:
            col = img[y0:y1, x].max(axis=1)
            if int((col > 70).sum()) >= (y1 - y0) * 0.8:
                return True
        # 退路：面板上读得出选项名就算浮层开着
        return bool(self._season_items_anywhere(img))

    def _panel_open(self):
        """筛选面板是否还开着（自己抓屏）。"""
        return self._panel_open_img(self._snap())

    def _panel_open_img(self, img):
        """筛选面板是否还开着。

        判据：**面板标题「筛选」的位置和字号**。真机实测（2026-10-20）：
          - 面板关：「筛选」在按钮上，y=200 / x=244，字 16x15
          - 面板开：「筛选」是面板标题，y=237 / x=410，字 30x31
        位置差 40px、字号差一倍，用裁剪区就能分开。

        注意裁剪高度：标题字高 31px、从 y=237 起，裁剪区必须覆盖到 y=270
        以上，否则字被切掉 OCR 读不出来（早期裁 225-255 只覆盖到 255，
        结果无论开没开都读到空 → 判据永远 False）。

        不用 where()：浮层收起后下拉框显示已选中的系列名，面板顶部那行
        「筛选中-xxx」被面板挡住，OCR 拼串对不上页面锚点，where() 会误报
        unknown（实测踩过，导致误判「已离开面板」→ 重开面板 → 流程乱掉）。
        """
        if img is None or getattr(img, "size", 0) == 0:
            return False
        crop = img[228:285, 400:500]
        # 放大 2 倍再 OCR：30px 的字在 100x57 的小裁剪里，WinRT 常常读不出来
        # （全屏 OCR 时上下文多才读得出「筛选」）。放大后稳定。
        words = self._ocr_words(vision.upscale(crop, 2))
        # **必须拼串再判，且不能依赖顺序**：WinRT 在裁剪区里返回的是单字
        # ['筛','选']，不是合成词 '筛选'；而两字的 y 可能差 1px，按
        # (y, x) 排序会拼成「选筛」。所以只要求两个字都出现。
        # 早期写 any("筛选" in w["text"]) 永远匹配不上，导致面板明明开着
        # 也报 False（实测 12 个系列全挂在这）。
        text = "".join(w.get("text", "") for w in words)
        return "筛" in text and "选" in text

    # 赛季下拉里的选项名（固定十几个词）。用来在**任意位置**找选项 ——
    # 面板有两种布局（列表有货时贴边、列表空时居中浮层），固定裁剪区
    # 换个布局就失效（实测「下拉展不开」「不在可见范围」）。
    #
    # 「棱镜攻势S2」「电玩高手S2」也在列表里（真机实测 2026-10-23）——
    # 它们和 S1 的名字只差后缀，靠「长名优先」区分：认「棱镜攻势S2」
    # 要求 S/2 这两个字符也在那一行里。
    _SERIES_WORDS = ("全部赛季", "棱镜攻势S2", "电玩高手S2", "棱镜攻势", "美杜莎",
                     "电玩高手", "命运王牌", "气象感应", "悬赏令", "造物纪元",
                     "群星", "疾光魅影")

    @classmethod
    def _series_spans(cls, words, y0, by):
        """按**单字**把选项归组，返回 {赛季名: [该名各字的 y]}。

        为什么不能按整行认：OCR 会认错字也会并错行（实测「群星」读成
        「难星」、「疾光魅影」读成「疾光赃影」，相邻两项还会并成一行）。
        **按单字归组就没有这个问题** —— 各赛季名的字互不重叠
        （全部赛季/棱镜攻势S2/电玩高手S2/棱镜攻势/美杜莎/电玩高手/
        命运王牌/气象感应/悬赏令/造物纪元/群星/疾光魅影 两两不相交），
        一个字只属于一个名字。

        认哪个名字：一行里可能同时符合好几个（「棱镜攻势」和
        「棱镜攻势S2」的字是包含关系）。按**完整度**排 —— 命中字数占
        名字字数的比例高者优先，比例一样时取**更长**的：
          * S1 那一行「棱镜攻势」：棱镜攻势 4/4=100%，棱镜攻势S2 4/6=67%
            → 选「棱镜攻势」✓
          * S2 那一行「棱镜攻势 S2」：两个都是 100% → 取更长的
            「棱镜攻势S2」✓
        门槛：命中 ≥2 个字（两个字的名字「群星」只认出「星」也算），
        留出 OCR 认错一个字还能认出来的余量。

        返回的 y 是该名字各字 y 的中位数 —— 就是这一项在屏幕上的位置。
        """
        rows_src = {}
        for w in words:
            t = (w.get("text") or "").strip()
            if not t:
                continue
            cy = y0 + (w["y"] + w["h"] / 2) / 2
            if cy <= by + 16:          # 按钮那一行，不是选项
                continue
            rows_src.setdefault(int(cy // 6), []).append((t, cy))
        out = {}
        for _key, chars in rows_src.items():
            text = "".join(t for t, _ in chars)
            cands = []
            for name in cls._SERIES_WORDS:
                n = sum(1 for ch in name if ch in text)
                if n < 2 and not (n >= 1 and len(name) == 2):
                    continue
                cands.append((n / len(name), len(name), name, n))
            if not cands:
                continue
            _ratio, _ln, best, _n = max(cands)
            ys = [cy for t, cy in chars if any(ch in best for ch in t)]
            out[best] = sorted(ys)
        return out

    def _season_items_anywhere(self, img=None, tries=3):
        """在面板上找赛季选项，返回 [(赛季名, 点击y)]。

        按**文字**找，不认固定位置：面板两种布局下浮层的位置不一样
        （贴边 vs 居中浮层），坐标是记不住的，选项名才是固定的。

        裁剪范围只取**下拉那一竖列**（x 540-830）：下拉按钮的位置在两种
        布局下都一样（真机实测 x562-813），浮层是它下面展开的，所以这一
        列永远装着选项 —— 整屏 2 倍 OCR 要好几秒，不能那么干。

        下拉按钮上显示的「当前系列」也要排除 —— 它也在同一列里，但不在
        选项列表中（点了只会把浮层收起来）。判据：选项都在按钮**下面**。

        返回的是**标准赛季名**（不是 OCR 原文），下游匹配才稳。

        **连读几次合并**：浮层在滚动/做动画时读会漏字（实测同一张静止
        截图离线重读 6/6 全中，真机上却只读到 5 项 —— 是瞬时漏读）。
        读 3 次把结果并起来，哪一项漏了别的次数会补上。
        """
        x0, y0 = 540, 280
        by = self.pt(COL_SEASON)[1]
        merged = {}
        prev = 0
        for attempt in range(max(1, tries)):
            if img is not None and attempt:
                break                     # 调用方给了固定画面，没得重读
            img = self._snap() if img is None else img
            crop = img[y0:820, x0:830]
            words = self._ocr_words(vision.upscale(crop, 2))
            for name, ys in self._series_spans(words, y0, by).items():
                cy = int(ys[len(ys) // 2])
                key = (name, cy // 6)          # 6px 一档，容忍抖动
                merged.setdefault(key, (name, cy))
            if len(merged) == prev:
                break                     # 这一轮没多读出东西，不必再读
            prev = len(merged)
        out = sorted(merged.values(), key=lambda x: x[1])
        return out

    def _season_items(self):
        """读下拉浮层里当前可见的选项，返回 [(文字, 点击y)]，按 y 排序。

        **只对贴边布局可靠**（固定裁剪区 x555-765 / y340-700）。面板在
        「列表空」时会变成居中浮层，位置就变了 —— 所以流程里统一用
        `_season_items_anywhere()`（按文字找），这个留作对照。

        只 OCR 浮层那一小块（x555-765 / y340-700），并且剔除面板固定词 ——
        整屏 OCR 会把「已拥有」「传说品阶」也读成下拉项，早期就踩过。

        **必须先放大 2 倍再 OCR**。浮层选项是 15px 高的小字、灰字，原尺寸
        下 WinRT 会整行跳过短项：实测「群星」两个字被完全漏掉（原始 OCR 从
        「造物纪元」@540 直接跳到「疾光魅影」@635，中间 y≈588 一个词都没
        有，但像素上明显有字）。放大 2x 后能正常读出「群星」。3x/4x 反而
        因为插值过度变糊，2x 是实测最稳的。

        返回的 y 是**文字块顶部**（屏幕坐标，已从放大坐标换算回来）。真正
        的可点位置不在文字上，由 _click_season_item 在附近扫描 —— 实测行高
        48px，文字画在行中下部，可点热区在行的上半部，只有 ±8px 宽。

        **浮层收起时必须返回空**。这个裁剪区 x555-765 在浮层收起后盖着的
        是面板里的复选框文字（「传说品阶」「步枪」「精准射手」），OCR 照样
        读得出来。早期没这个判断，重试路径读到面板文字当成选项，于是报
        「找不到造物纪元（见过的：[...传说品, 步枪, 精准射手]）」—— 那根本
        不是下拉里的东西。
        """
        img = self._snap()
        if not self._dropdown_open_img(img):
            return []
        crop = img[340:700, 555:765]
        words = self._ocr_words(vision.upscale(crop, 2))
        ph = ocr.phrases(words, gap=24)      # 坐标放大 2x，横向间距阈值也 ×2
        out = []
        for p in ph:
            t = p["text"]
            if len(t) < 2:
                continue
            if t in self._PANEL_WORDS:
                continue
            out.append((t, p["y0"] // 2 + 340))
        out.sort(key=lambda it: it[1])
        return out

    def col_pick_season(self, name):
        """在赛季下拉里选中 name（系列名，如「美杜莎」「群星」）。

        流程：确认展开 → 滚到顶 → 逐屏向下读 → 命中就点 → 校验下拉框。

        真机实测（2026-10-20，1920x1080）：
          - 列表共 **12 项**：全部赛季 / 棱镜攻势 / 美杜莎 / 电玩高手 /
            命运王牌 / 气象感应 / 棱镜攻势S2 / 悬赏令 / 电玩高手S2 /
            造物纪元 / 群星 / 疾光魅影
          - 浮层**默认不在顶部**：刚打开时首项是「棱镜攻势」，「全部赛季」
            和「群星」都看不到 —— 必须滚动才找得到
          - 滚轮锚点必须是**第一项的位置** (613, 380)；用 y=450 落点不对，
            滚了没反应（早期据此误判「向上滚无效」）
          - 滚一格移约 3 项，滚到第 3 屏就到底了（再滚画面完全不变）

        找不到就抛 NavError，不盲点 —— 点错项会筛出别的皮肤，白跑一趟。
        """
        if self.dry_run:
            self.say(f"[演练] 赛季下拉选「{name}」", "warn")
            return True
        if self.keyboard is None:
            raise NavError("没有 Keyboard，滚不了下拉列表")

        # 先确保面板开着（col_filter 自带「已开就不点」的判断）
        if not self._panel_open():
            # 再确认一次：这里误判的代价是**再点一次筛选按钮**，而那一按
            # 会把刚打开的面板关掉，后面所有操作全落空。
            time.sleep(0.15)
            if not self._panel_open():
                self.col_filter()
                time.sleep(0.8)

        # 已经是目标赛季就别再滚一遍下拉：选一次要滚 3 屏、每屏一次 OCR，
        # 实测十几秒；读一眼下拉框只要一次 OCR。
        now = self._season_now()
        if now and self._season_match(name, now):
            self.say(f"下拉框已经是「{now}」，跳过重选", "info")
            return True

        # 下拉必须是「刚打开、在默认位置」的状态。上一轮可能停在滚动
        # 中途、或者动画还没结束，直接接着用会读到面板文字（实测批量跑
        # 到第 4 个系列时 _season_items 读到 ['[@]传说品','步枪','精准
        # 射手'] —— 那是面板里的复选框文字，说明浮层其实没开着）。
        #
        # 复位方式：点一下下拉框（收/开各一次），每次都验证。不猜初始
        # 状态 —— 点一次的结果直接决定下一步。
        # 判断用 _dropdown_open_settled（读不到会再确认一次），点完**轮询**
        # 而不是固定等 0.9 秒：展开得快时 0.3 秒就能往下走。
        opened = False
        for _ in range(4):
            if self._dropdown_open_settled():
                opened = True
                break
            self.click(COL_SEASON, "赛季下拉", expect_change=False)
            t_open = time.perf_counter()
            while time.perf_counter() - t_open < 0.9:
                if self._dropdown_open_settled(tries=1):
                    opened = True
                    break
                time.sleep(0.08)
            if opened:
                break
        if not opened:
            raise NavError("点了赛季下拉但没展开（读不到首项文字）")

        ax, ay = COL_SEASON_WHEEL_ANCHOR

        # 滚到顶：浮层默认在中间位置，不滚上去就看不到「全部赛季」「群星」。
        # 滚到「全部赛季」出现就停，别固定滚 20 次 —— 每次 1.1s，白等 20 秒。
        for _ in range(COL_SEASON_MAX_SCROLL):
            items = self._season_items_anywhere()
            if items and any("全部赛季" in t for t, _ in items):
                self.say("已滚到顶部（看到「全部赛季」）", "info")
                break
            self.keyboard.wheel(ax, ay, 1)
            time.sleep(0.6)
        time.sleep(0.5)

        seen = []
        prev = None
        tried = 0        # 已经完整点过几轮（每次失败重扫一次，最多 3 轮）
        for step in range(COL_SEASON_MAX_SCROLL):
            items = self._season_items_anywhere()
            names = [t for t, _ in items]
            if not names:
                # 浮层收了（_season_items 判空）或者这一刻读不到字。
                # 别直接抛错 —— 先重开浮层再读一次，真机上动画期间会空一拍。
                reopened = False
                for _ in range(4):
                    if self._dropdown_open():
                        reopened = True
                        break
                    self.mouse.click(COL_SEASON[0], COL_SEASON[1],
                                     verify=False)
                    time.sleep(0.9)
                time.sleep(0.5)
                items = self._season_items_anywhere()
                names = [t for t, _ in items]
                if not names:
                    raise NavError(f"赛季下拉读不到选项（第 {step + 1} 屏，"
                                   f"浮层{'已重开' if reopened else '重开失败'}）")

            # 命中：整串相等，或互相包含，或大半的字对得上（OCR 会认错字，
            # 实测「悬赏令」被读成「悬赏今」、「命运王牌」被读成「命运王牒」）
            hit = [(t, y) for t, y in items if self._season_match(name, t)]
            if hit:
                t, y = hit[0]
                self.say(f"赛季下拉命中「{t}」文字 y={y}", "info")
                if self._click_season_item(t, y):
                    return True
                tried += 1
                if tried >= 3:
                    raise NavError(f"点了 3 轮「{t}」都没成功")
                # 重试前必须把浮层重新展开。_click_season_item 失败后浮层
                # 通常是收起状态，这时滚轮打在面板上没有任何效果，而
                # _season_items 会返回空（浮层收起时判空），白滚一轮
                # （实测「造物纪元」就是在这一步把面板文字读成了选项）。
                self.say(f"  点「{t}」没成功，重新展开浮层再试（第 {tried + 1} 轮）",
                         "warn")
                for _ in range(4):
                    if self._dropdown_open():
                        break
                    self.mouse.click(COL_SEASON[0], COL_SEASON[1],
                                     verify=False)
                    time.sleep(0.9)
                time.sleep(0.4)
                continue

            for t in names:
                if not any(self._season_match(t, s) for s in seen):
                    seen.append(t)

            # 画面和第上一屏完全一样 → 已经滚到底，别再空转
            if prev is not None and names == prev:
                self.say("列表已到底（画面不再变化）", "info")
                break
            prev = names

            self.keyboard.wheel(ax, ay, -1)
            time.sleep(1.1)

        raise NavError(f"赛季下拉里找不到「{name}」（见过的：{seen}）")

    @staticmethod
    def _season_match(target, text):
        """判断 OCR 读到的选项 text 是不是目标 target。

        三级判据，从严到宽：
          1. 整串相等
          2. 互相包含（OCR 可能多认或少认字，如「气象应」缺了「感」）
          3. 至少一半的字对得上（「悬赏令」vs「悬赏今」、「命运王牌」vs
             「命运王牒」这种同音错字）

        第 3 级是必须的：OCR 把同音字认错很常见，只做前两级会漏掉
        「悬赏令」这种目标。但阈值不能更松，否则「棱镜攻势」和
        「棱镜攻势S2」会互相命中。
        """
        if not text or not target:
            return False
        if text == target:
            return True
        # 互相包含只对「长度相近」的成立。不设这条限制时，OCR 把
        # 「电玩高手」拆成两块「电玩」「高手」，其中「电玩」会因为
        # `text in target` 命中，于是去点那半截字 —— 热区在整行中部，
        # 点「电玩」两字的位置根本点不中（实测「电玩高手」就是这么挂的）。
        if abs(len(text) - len(target)) <= 1 and (target in text or text in target):
            return True
        hit = sum(1 for ch in target if ch in text)
        return hit * 2 >= len(target) and len(text) >= len(target) - 1

    # 命中目标项后要试的 y 偏移（相对 OCR 读到的文字中心）。
    #
    # 真机实测（2026-10-20，贴边布局）热区相对「文字中心」的位置**和字长有关**：
    #   - 3-4 字的长项（棱镜攻势 / 美杜莎 / 电玩高手 / 命运王牌 / 气象感应）：
    #     热区 ≈ 文字中心 - 16
    #   - 2 字的短项（群星）：热区 ≈ 文字中心 + 8
    # 两者差 24px，用单一偏移必然有一类点空，所以按「覆盖整行」取一组偏移。
    #
    # 面板在「列表空」时会变成**居中浮层**，行高和文字位置都变了，偏得更多
    # （实测「棱镜攻势」文字在 y=412，热区在它上方 20~36px），所以负偏移
    # 给到 -32。
    _SEASON_PICK_OFFSETS = (-16, 8, -12, 12, -8, 16, -4, 20, 0, 24, -20, 28,
                            -24, 32, -28, 36, -32, 40)

    def _click_season_item(self, text, y):
        """点中浮层里的目标项，成功后校验下拉框。

        流程（每轮）：
          1. 确保浮层展开（点下拉框；点了没开就再点一次）
          2. 把浮层滚到**目标所在的那一屏**（先向上回顶，再逐格向下找）
          3. 重读一次位置（消除滚动惯性），按偏移表试点
          4. 浮层收起后读下拉框，确认选中的就是目标

        关浮层只用「再点一次下拉框」，不用 ESC —— ESC 有时只关浮层、有时连
        整个筛选面板一起关，后者会让下一轮在错误的页面上瞎点。

        面板是否还在用 `_panel_open()`（面板专属词「枪种」）判，不用
        `where()` —— 浮层收起后下拉框显示的是系列名，页面锚点对不上，
        `where()` 会误报 unknown。
        """
        if self.keyboard is None:
            raise NavError("没有 Keyboard，重开不了下拉")
        ax, ay = COL_SEASON_WHEEL_ANCHOR

        for off in self._SEASON_PICK_OFFSETS:
            # --- 1. 确保浮层展开 ---
            if self._dropdown_open():
                # 先收起，回到确定状态
                self.mouse.click(COL_SEASON[0], COL_SEASON[1], verify=False)
                time.sleep(0.8)
            if not self._panel_open():
                self.say(f"  偏移 {off}：筛选面板已关闭，重开", "warn")
                self.col_filter()
                time.sleep(1.0)
                if not self._panel_open():
                    return False
            if not self._dropdown_open():
                self.mouse.click(COL_SEASON[0], COL_SEASON[1], verify=False)
                time.sleep(1.2)
            if not self._dropdown_open():
                # 点一次没开就再点一次（真机上偶尔会漏掉第一下）
                self.mouse.click(COL_SEASON[0], COL_SEASON[1], verify=False)
                time.sleep(1.2)
            if not self._dropdown_open():
                self.say(f"  偏移 {off}：下拉展不开，跳过", "warn")
                continue

            # --- 2. 滚到目标那一屏：先回顶，再逐格向下 ---
            for _ in range(COL_SEASON_MAX_SCROLL):
                items = self._season_items_anywhere()
                if items and any("全部赛季" in t for t, _ in items):
                    break
                self.keyboard.wheel(ax, ay, 1)
                time.sleep(0.7)
            time.sleep(0.6)

            cur = {t: yy for t, yy in self._season_items_anywhere()}
            if not any(self._season_match(text, t) for t in cur):
                for _ in range(COL_SEASON_MAX_SCROLL):
                    self.keyboard.wheel(ax, ay, -1)
                    time.sleep(1.0)
                    cur = {t: yy for t, yy in self._season_items_anywhere()}
                    if any(self._season_match(text, t) for t in cur):
                        break

            # --- 3. 重读一次，消除滚动惯性 ---
            time.sleep(0.8)
            cur = {t: yy for t, yy in self._season_items_anywhere()}
            hit = [(t, yy) for t, yy in cur.items()
                   if self._season_match(text, t)]
            if not hit:
                self.say(f"  偏移 {off}：「{text}」不在可见范围，跳过", "warn")
                continue
            _t, ty = hit[0]
            ty = ty + off
            self.mouse.click(COL_SEASON_ITEM_X, ty, verify=False)
            time.sleep(1.0)

            # --- 4. 校验 ---
            #
            # **先看下拉框显示的是不是目标项，别先看浮层收没收起**。
            # 点中了以后浮层不一定收起（有时要点两下才收），浮层还开着
            # 不等于没选中 —— 早前就是这里把「点中了」判成「没命中」，
            # 于是一轮 18 个偏移全试一遍还是失败（实测卡了两个多小时）。
            if self._season_applied(text):
                self.say(f"点「{text}」y={ty}（偏移 {off}）选中了", "info")
                if self._dropdown_open():
                    # 浮层还开着 → 点一下下拉框把它收掉
                    self.mouse.click(COL_SEASON[0], COL_SEASON[1], verify=False)
                    time.sleep(0.6)
                return True
            if not self._dropdown_open():
                # 浮层收起来了但选中的不是目标 → 重新展开，换个偏移再试
                self.say(f"  选中的不是「{text}」（浮层已收），换个偏移再试", "warn")
            else:
                self.say(f"  偏移 {off} → y={ty} 没选中，再试", "warn")
        return False

    def _season_now(self):
        """读下拉框当前显示的赛季（收起态），一次 OCR，不重试。"""
        crop = self._snap()[300:370, 540:800]
        words = self._ocr_words(vision.upscale(crop, 2))
        return "".join(w.get("text", "") for w in sorted(words, key=lambda w: w["x"]))

    def _season_applied(self, text):
        """浮层收起后，确认下拉框显示的就是目标项。

        读**下拉框本身**（收起态 y≈327 / x584-640 显示选中项），不读面板
        外面那行「筛选中-xxx」—— 筛选面板还开着的时候那行被面板盖住，
        OCR 读不到，早期据此判「读不到就按成功处理」，等于没有校验。

        实测下拉框文字是 15px 高的小字，OCR 偶尔认错（「命运王牌」读成
        「命运王牒」、「气象感应」读成「气獠感应」），所以用「目标里至少
        一半的字出现在读到的串里」判，不要求整串相等。

        **读不到就返回 False**（早期返回 True「按成功处理」，等于没有校验：
        实测放过了一次误选 —— 点「群星」实际选中「电玩高手S2」，下拉框
        当时读不到，于是当成成功，最后筛出错的皮肤）。宁可重试一轮，也
        不能带着错的筛选条件往下走。
        """
        img = self._snap()
        combo = ""
        # 读不到就重试几次：浮层刚收起时下拉框可能还在做过渡动画，
        # 那一刻 OCR 读到的是空的（实测「造物纪元」连试 6 个偏移都因为
        # 这个被判失败，其实点中了）。
        #
        # **必须放大 2 倍再 OCR**：下拉框文字是 15px 高的小字，1x 下
        # 常常整串读不出来（实测「造物纪元」在 y300-370/x540-800 的 1x
        # 裁剪里返回空，2x 才有字）。这和 _season_items 是同一个坑。
        for attempt in range(4):
            crop = img[300:370, 540:800]
            words = self._ocr_words(vision.upscale(crop, 2))
            combo = "".join(w.get("text", "") for w in sorted(words,
                                                             key=lambda w: w["x"]))
            if combo:
                break
            time.sleep(0.6)
            img = self._snap()
        if not combo:
            self.say("下拉框文字读不到，视为未确认", "warn")
            return False
        hit = sum(1 for ch in text if ch in combo)
        ok = hit * 2 >= len(text)
        self.say(f"下拉框显示「{combo}」{'匹配' if ok else '不匹配'}「{text}」",
                 "info" if ok else "warn")
        return ok

    def col_check(self, row, index, want=True):
        """勾/取消典藏外观筛选面板里的一个复选框。

        row   —— "owned" / "not_owned" / "rarity" / "gun" / "gun2"
        index —— 该行第几个（0 起）
        want  —— True 勾上，False 取消。面板会记住上次的勾选状态，
                 所以不能盲目切换 —— 必须读到当前状态再决定点不点。

        校验方式是**读框内亮像素**：勾选后框里出现亮色对勾，亮像素从 0
        跳到 300+。实测过「只看画面变化」会误判 —— 0.04% 的变化也可能
        只是光标划过，但复选框状态其实没变。
        """
        if row == "owned":
            pt = COL_OWNED
        elif row == "not_owned":
            pt = COL_NOT_OWNED
        elif row == "rarity":
            pt = (COL_RARITY_X[index], COL_RARITY_Y)
        elif row == "gun":
            pt = (COL_GUN_X[index], COL_GUN_Y[0])
        elif row == "gun2":
            pt = (COL_GUN_X2[index], COL_GUN_Y[1])
        else:
            raise NavError(f"未知的筛选行 {row!r}")

        if self.dry_run:
            self.say(f"[演练] {'勾' if want else '取消'} {row}[{index}] {pt}", "warn")
            return True

        x, y = self.pt(pt)
        before = vision.box_brightness(self._snap(), x, y)
        # 亮像素 > 100 视为已勾选（空框 0-60，勾上 300+，中间空档很大）
        is_on = before > 100
        if is_on == want:
            self.say(f"{row}[{index}] 已经是{'勾选' if want else '未勾选'}状态，跳过",
                     "info")
            return True

        self.mouse.click(x, y, verify=False)
        time.sleep(0.7)
        after = vision.box_brightness(self._snap(), x, y)
        self.say(f"{'勾' if want else '取消'} {row}[{index}] ({x},{y}) "
                 f"亮像素 {before}→{after}",
                 "info" if (after > 100) == want else "warn")
        if (after > 100) != want:
            raise NavError(
                f"{row}[{index}] 点击后状态不对"
                f"（期望{'勾选' if want else '取消'}，亮像素 {after}）"
                f" —— 多半是**游戏不在前台**：游戏不是前台时会忽略鼠标输入，"
                f"点击「成功」了但状态一点没变。把游戏切到前台再试。")
        return True

    def col_filter_states(self):
        """读筛选面板当前的勾选状态，一次截图读全 14 个框。

        返回 {"tier": [4 个 bool], "guns": {枪种: bool}, "owned": bool,
              "not_owned": bool}

        为什么能读：勾选后框里会出现亮色对勾，亮像素从 0-60 跳到 300+
        （`vision.box_brightness`），中间空档很大，不用 OCR —— 14px 的小字
        OCR 认不准，框里那个对勾反而好认得多。

        tier 的顺序是页面上从左到右：传说 / 史诗 / 稀有 / 普通。
        """
        return self._read_boxes(self._snap())

    def _read_boxes(self, img):
        """在一张截图上读全部勾选框。"""
        def on(pt):
            x, y = self.pt(pt)
            return vision.box_brightness(img, x, y) > 100

        guns = {}
        for name, (row, idx) in self.GUN_ROWS.items():
            pt = ((COL_GUN_X[idx], COL_GUN_Y[0]) if row == "gun"
                  else (COL_GUN_X2[idx], COL_GUN_Y[1]))
            guns[name] = on(pt)

        return {
            "tier": [on((COL_RARITY_X[i], COL_RARITY_Y)) for i in range(4)],
            "guns": guns,
            "owned": on(COL_OWNED),
            "not_owned": on(COL_NOT_OWNED),
        }

    def _gun_point(self, name):
        row, idx = self.GUN_ROWS[name]
        return ((COL_GUN_X[idx], COL_GUN_Y[0]) if row == "gun"
                else (COL_GUN_X2[idx], COL_GUN_Y[1]))

    def col_apply(self, tier=None, guns=None, owned=None, not_owned=None):
        """把面板调成指定状态，**只点不一致的框**。

        tier      —— 4 个 bool（从左到右：传说/史诗/稀有/普通），None = 不动
        guns      —— {枪种: bool}，没写到的枪种不动；None = 全部不动
        owned / not_owned —— bool 或 None（None = 不动）

        为什么要先读再点：勾选框是**记忆**的，上次勾了什么这次还勾着。
        以前每次都把 14 个框挨个点一遍（9 次无效点击 × 0.7 秒），
        实测一轮定位光在筛选面板上就耗掉十几秒。现在先一次截图读出全部
        状态，只点真正需要变的 —— 状态本来就对时一次点击都不发。
        """
        if self.dry_run:
            self.say(f"[演练] 调筛选 tier={tier} guns={guns}", "warn")
            return self.col_filter_states()

        img = self._snap()
        now = self._read_boxes(img)

        todo = []          # [(说明, 点, 期望)]
        if tier is not None:
            for i, want in enumerate(tier):
                if now["tier"][i] != want:
                    todo.append((f"品阶[{i}]", (COL_RARITY_X[i], COL_RARITY_Y), want))
        if guns is not None:
            for name, want in guns.items():
                if name not in self.GUN_ROWS:
                    raise NavError(f"没有这个枪种：{name}")
                if now["guns"].get(name) != want:
                    todo.append((f"枪种「{name}」", self._gun_point(name), want))
        if owned is not None and now["owned"] != owned:
            todo.append(("已拥有", COL_OWNED, owned))
        if not_owned is not None and now["not_owned"] != not_owned:
            todo.append(("未拥有", COL_NOT_OWNED, not_owned))

        if not todo:
            self.say("筛选面板状态已经和目标一致，一个框都不用点", "info")
            return now

        def _missing(state):
            """哪些框的状态还不对（和上面 todo 的 label 同名，便于补点）。"""
            bad = []
            if tier is not None:
                for i, want in enumerate(tier):
                    if state["tier"][i] != want:
                        bad.append(f"品阶[{i}]")
            if guns is not None:
                for name, want in guns.items():
                    if state["guns"].get(name) != want:
                        bad.append(f"枪种「{name}」")
            if owned is not None and state["owned"] != owned:
                bad.append("已拥有")
            if not_owned is not None and state["not_owned"] != not_owned:
                bad.append("未拥有")
            return bad

        # 每个框点完只等 0.15 秒：勾选框是**即时切换**的，0.35 秒纯属白等
        # （9 个框就是 3 秒，抢购里够别人把货买走了）。真被吞了事件的话，
        # 下面统一校验会抓出来并补点一次。
        for label, pt, want in todo:
            x, y = self.pt(pt)
            self.mouse.click(x, y, verify=False)
            time.sleep(0.15)
        time.sleep(0.25)

        after = self._read_boxes(self._snap())
        bad = _missing(after)
        if bad:
            # 补点一次：游戏刚切到前台时，第一下点击偶尔会被吞掉
            again = [t for t in todo if t[0] in bad]
            if again:
                self.say(f"有 {len(bad)} 个框没生效，补点一次：{bad}", "warn")
                for label, pt, want in again:
                    x, y = self.pt(pt)
                    self.mouse.click(x, y, verify=False)
                    time.sleep(0.18)
                time.sleep(0.3)
                after = self._read_boxes(self._snap())
                bad = _missing(after)
        if bad:
            raise NavError(
                f"点了 {len(todo)} 个框，但 {bad} 的状态还是不对 —— "
                "多半是**游戏不在前台**：游戏不是前台时会忽略鼠标输入，"
                "点击「成功」了但状态一点没变。把游戏切到前台再试。")
        self.say(f"筛选面板：改了 {len(todo)} 个框（{', '.join(t[0] for t in todo)}）",
                 "info")
        return after

    def col_filter_reset(self):
        """把筛选恢复成「全要」：四个品阶、七个枪种都勾上，已拥有/未拥有都勾上。

        为什么要它：列表空的时候筛选面板会变成**居中浮层**，面板里的坐标
        （赛季下拉、选项行）全变 —— 实测「下拉展不开」「不在可见范围」，
        选系列直接卡死。列表满的时候面板贴边，坐标才对得上。所以定位某件
        砖之前先复位一次。
        """
        self.col_filter()
        # 一次读全 + 只点不一致的：状态本来就对时一个框都不点
        self.col_apply(tier=[True] * 4,
                       guns={name: True for name in self.GUN_ROWS},
                       owned=True, not_owned=True)
        self.col_filter_confirm()
        time.sleep(0.8)

    def set_tier(self, tier_index, only=True):
        """把品阶筛成只剩某一档（only=True）或把某一档加上（only=False）。

        只留一档很重要：勾选框是累加的，不取消其它三档，列表里会混着
        别的档（实测「史诗」那档里还带着传说的货）。
        """
        if only:
            want = [i == tier_index for i in range(4)]
        else:
            now = self.col_filter_states()["tier"]
            want = [True if i == tier_index else now[i] for i in range(4)]
        return self.col_apply(tier=want)

    # 枪种行：两行七项（页面上从左到右）
    GUN_ROWS = {
        "步枪": ("gun", 0),
        "冲锋枪": ("gun", 1),
        "霰弹枪": ("gun", 2),
        "轻机枪": ("gun", 3),
        "精准射手步枪": ("gun2", 0),
        "狙击步枪": ("gun2", 1),
        "手枪": ("gun2", 2),
    }

    def set_gun(self, gun=None):
        """把枪种筛成只剩某一种（gun=None = 全部都留）。

        枪种是**识别皮肤枪种的最可靠办法**：型号那截 OCR 认不准
        （AS Val → ASVa），但「勾上冲锋枪，列表里出来的就都是冲锋枪」
        是游戏自己给的答案，不用猜。
        """
        if gun is None:
            return self.col_apply(guns={name: True for name in self.GUN_ROWS})
        if gun not in self.GUN_ROWS:
            raise NavError(f"没有这个枪种：{gun}")
        return self.col_apply(guns={name: (name == gun) for name in self.GUN_ROWS})

    # 左侧列表的卡片位置不是固定的 —— 筛选后条目数量变了，列表会重新
    # 排布。实测筛选「美杜莎 + 传说」后第一张卡的皮肤名在 y=392-421，
    # 而筛选前第一张在 y=253-268。所以不能靠 SKIN_CARD_Y 固定表，
    # 必须每次读 OCR 现算。
    _SKIN_LIST_X = (55, 430)        # 左侧列表的 x 范围
    _SKIN_LIST_Y = (230, 1010)      # 左侧列表的 y 范围
    _SKIN_CLICK_DY = 40             # 皮肤名中心 → 卡片中部的偏移
    _SKIN_NAME_WORDS = ("突击步枪", "冲锋枪", "步枪", "机枪", "霰弹枪",
                        "射手步枪", "狙击", "手枪", "战斗步枪", "通用机枪")

    def skin_scroll(self, notches):
        """滚左侧皮肤列表（正数向下）。

        列表一屏只显示 7 张卡，一个赛季的四个品阶加起来几十张 —— 想找
        某一件必须能滚。

        滚轮发在列表上方：游戏按光标位置决定滚哪个区域，位置不对
        会滚到别的地方去（实测滚轮发在页面中间，列表纹丝不动）。

        **符号**：这一层是「正数向下」，而底下的 `Keyboard.wheel` 用的是
        Windows 惯例「正数向上」，这里要翻过来 —— 之前没翻，滚的是反方向，
        在列表顶部滚了 12 屏全在原地（实测每屏都是同样 6 件）。
        """
        x = self._SKIN_LIST_X[0] + 120
        for _ in range(abs(int(notches))):
            self.keyboard.wheel(x, 600, -1 if notches > 0 else 1)
            time.sleep(0.18)
        time.sleep(0.6)

    def skin_scroll_top(self, max_notches=40):
        """把皮肤列表滚回顶。

        **不能固定滚满 40 格**：一格 0.18 秒，40 格就是 7.2 秒，而每扫一次
        列表开头都要滚一次（真机上「筛选完到点中卡片」要 20 多秒，一半耗在
        这里）。改成边滚边看：每 6 格读一眼列表，第一项不再变化就说明到底了。
        """
        x = self._SKIN_LIST_X[0] + 120
        prev = None
        done = 0
        while done < max_notches:
            for _ in range(min(6, max_notches - done)):
                self.keyboard.wheel(x, 600, 1)       # 正数向上
                time.sleep(0.12)
                done += 1
            time.sleep(0.35)
            items = self.skin_items()
            first = items[0][0] if items else None
            if first is not None and first == prev:
                break                                 # 滚不动了 = 到顶
            prev = first

    # 选中皮肤的**完整名字**（标题）。
    #
    # 位置是 2026-10-23 真机量的：典藏外观页的标题在屏幕**中间偏左**
    # （不是旧标定里那个右侧详情面板），字号大（28~31px），1 倍就认得准。
    #
    # 左边界 2026-11-02 从 470 收到 505：标题左边有个品阶菱形图标，
    # 470 起裁会把图标一起框进去，OCR 把它读成「中」—— 读出来的标题
    # 变成「中 MK4冲锋枪-疾光魅影」，相似度比对因此白掉一截。
    _TITLE_BOX = (505, 168, 900, 225)

    def skin_title(self):
        """读右侧详情面板的标题（当前选中皮肤的完整名字）。

        为什么不能直接用左侧列表里的名字：**游戏自己把它截断了** ——
        那一列很宽，「AK-12突击步枪·水墨」只显示「AK-12突…」。真机上
        列表里读出来的是「AK-12突」「M700狙击步」这种半截名，按它去匹配
        永远匹配不上。要完整名字只能点开卡片读右侧标题。
        """
        img = self._snap()
        x0, y0 = self.pt((self._TITLE_BOX[0], self._TITLE_BOX[1]))
        x1, y1 = self.pt((self._TITLE_BOX[2], self._TITLE_BOX[3]))
        crop = img[int(y0):int(y1), int(x0):int(x1)]
        if getattr(crop, "size", 0) == 0:
            return ""
        big = vision.upscale(crop, 2)
        rows = ocr.rows(ocr.read_words(big))
        if not rows:
            return ""
        # 框里偶尔会多出一行（上边框的装饰字、残留的标签）。皮肤名一定
        # 含枪种关键词，按这个挑。
        # **挑不到就返回空，不能退回第一行**：退回第一行会把「默认」
        # 「相似皮肤」这种无关文字当成标题返回，调用方（SkinFinder）拿它
        # 去比对，相似度 0.00 → 判「不是它」→ 明明是对的卡片被否掉，
        # 于是反复点开下一张 —— 又慢又容易点错（真机日志：「点开是「默认」
        # （0.00），不是它」）。空字符串让调用方知道「这次没读到，再等一会儿」。
        for r in rows:
            if any(w in r["text"] for w in self._SKIN_NAME_WORDS):
                return r["text"].strip()
        return ""

    def close_filter_panel(self):
        """把开着的筛选面板关掉（「确认」按钮的位置两种布局都一样）。

        面板开着的时候，背后的市场页看不见，标签绿框也读不到 ——
        后面所有按坐标点的操作都会落空。先收拾干净再干活。

        **点「确认」不一定关得掉**：浮层（下拉）展开着的时候，点击被
        浮层吃掉，面板还在 —— 后面 `tab("collection")` 因为读不到标签
        绿框直接报错（实测报「点标签 collection 没切过去（绿框现在在
        None）」）。所以点完要**校验**，没关掉就按 ESC（ESC 只关浮层/
        面板，不退页面，安全）。
        """
        if not self._panel_open():
            return True
        try:
            self.click((959, 754), label="筛选·确认（关面板）", expect_change=False)
            time.sleep(0.8)
        except Exception:
            pass
        if self._panel_open():
            self.say("「确认」没关掉面板，按 ESC", "warn")
            if self.keyboard is not None:
                self.keyboard.tap("ESC")
                time.sleep(0.8)
        return not self._panel_open()

    def goto_collection(self):
        """走到典藏外观页（从任何状态都能进）。

        步骤：关掉可能开着的筛选面板 → **在售页/详情页先按 Esc 退回上一级**
        → 不是市场页就按 F4 进市场 → 切到典藏外观标签。

        为什么必须先按 Esc：在售页（点「购买」进来的那个挂单列表）按 F4
        **回不到典藏外观页** —— 实测 F4 之后绿框停在「曼德尔砖」，接着点
        「典藏外观」标签连报三次「没切过去」，整个定位流程就死在这一步
        （日志 20:28:33~20:28:59 就是这么卡死的）。Esc 是游戏左下角写着
        的那个「返回」，一级一级退，退得回去。
        """
        self.close_filter_panel()
        page, _ = self.where()
        if page in ("onsale", "detail"):
            if self.dry_run:
                self.say(f"[演练] 按 Esc 从「{page}」返回上一级", "warn")
            elif self.keyboard is not None:
                self.say(f"当前在「{page}」，按 Esc 返回上一级", "info")
                self.keyboard.tap("ESC")
                time.sleep(0.9)
            page, _ = self.where()
        if page not in ("market", "filter_collection"):
            self.say(f"当前在「{page}」，按 F4 进市场", "info")
            self.open_market()
            time.sleep(1.5)
        if self.tab_now() != "collection":
            try:
                self.tab("collection")
            except NavError:
                # 有弹窗挡着（最常见：「该商品已被购买」提示还没收）。
                # 按 Esc 收掉再点一次，否则整个定位流程就死在这一步。
                if self.keyboard is not None and not self.dry_run:
                    self.say("标签点不动，先按 Esc 收掉挡着的弹窗", "warn")
                    self.keyboard.tap("ESC")
                    time.sleep(0.6)
                self.tab("collection")
            time.sleep(0.8)
        return self.where()[0]

    def _wait_skin_list(self, timeout=6.0, before=None):
        """等筛选生效后左侧列表刷出**新内容**，返回有没有等到。

        光等「非空」不够：筛选生效前列表里还是上一档的内容（非空 ✓）。
        实测换赛季后第一档读到的是上一季的五件（疾光魅影·传说 列出来的
        是造物纪元·普通那批）—— 等非空等于没等。

        `before` 是改筛选之前首屏第一件的文字；传了就比对它变没变，
        没传就只等列表非空。
        """
        t0 = time.time()
        while time.time() - t0 < timeout:
            items = self.skin_items()
            if items:
                if before is None or items[0][0] != before:
                    return True
            time.sleep(0.4)
        return False

    def select_skin(self, series=None, tier=None, name=None, gun=None):
        """按「系列 + 品阶 + 枪种 + 名字」定位并选中一件皮肤。返回点击 y。

        这是「我要抢某一砖」的完整链路：
          设系列（赛季下拉）→ 只留一个品阶 → 只留一个枪种 → 确定
          → 在列表里滚着找 → 点它

        **枪种一起筛**是有用的：这一档里同名的皮肤可能有好几个（不同枪种
        各一件），枪种一筛就只剩一件，不用在列表里猜。

        名字比对用**模糊匹配**：游戏的花体字 OCR 认不准的字很多
        （「彩」→「采」「白」→「自」），字符串相等永远匹配不上。

        列表里的名字是被游戏截断的（「AK-12突…」），所以先用武器型号
        粗筛（那一段认得准），点开再读标题做精确比对。
        """
        if series or tier is not None or gun:
            # 先收拾干净：面板开着就关掉、回到典藏外观页、筛选复位成「全要」
            # （列表空的时候面板会变成居中浮层，赛季下拉的位置跟着变）。
            self.goto_collection()
            self.col_filter_reset()
            self.col_filter()
            # 记下改筛选之前列表第一件是什么 —— 等列表刷新要用它比对
            # （光等「非空」不够，旧列表也是非空的）
            before_items = self.skin_items()
            before = before_items[0][0] if before_items else None
            if series:
                self.col_pick_season(series)
            if tier is not None:
                index = ("传说", "史诗", "稀有", "普通").index(tier)
                self.set_tier(index, only=True)
            if gun:
                self.set_gun(gun)
            self.col_filter_confirm()
            # 列表要重载才生效，重载期间读到的是旧的（甚至空的）——
            # 得等它刷出**新内容**再找
            self._wait_skin_list(before=before)

        if not name:
            return None

        # 武器型号（名字最前面那段字母数字）用来粗筛
        self.skin_scroll_top()
        for step in range(40):
            items = self.skin_items()
            if not items:
                break
            # 这一屏里所有像目标的卡片，按相似度从高到低试 —— 只试最像的一张
            # 不够：同一档里常有七张同名不同枪种的卡（「…-夜穹」七连），
            # 跳过其它候选就会一路滚到底都找不到。
            for short, y, _score in self.rank_skin_cards(name, items=items):
                self.click((150, y), label="皮肤卡片", expect_change=False)
                time.sleep(0.4)
                title = self.skin_title()
                if not title:
                    continue
                score = ocr.similarity(title, name)
                if score >= 0.72:
                    self.say(f"选中「{title}」（相似度 {score:.2f}）y={int(y)}", "hit")
                    return int(y)
            self.skin_scroll(1)
        raise NavError(f"列表里找不到「{name}」（系列={series} 品阶={tier}）")

    def rank_skin_cards(self, name, items=None, floor=0.6):
        """把当前列表里的卡片按「像不像目标」排序，返回 [(文字, 点击y, 相似度)]。

        纯读不点 —— 测试要能离线验「这一屏里有没有它、点哪儿」，引擎也能
        先拿它判断值不值得点开。

        两级匹配：
          1. 粗筛看武器型号（名字最前面那段字母数字，如 `QBZ95`、`AS Val`）。
             这一段 OCR 认得最准，先用它把候选从七张缩到一两张。
          2. 精筛看整名相似度。游戏花体字常被认错一两个字（「彩」→「采」），
             所以用相似度不用相等。

        不用「包含」判断：列表里的名字可能被游戏截断（「AK-12突…」），
        包含永远不成立；反过来 `short in name` 又会把同名不同枪种全算命中。
        """
        if not name:
            return []
        if items is None:
            items = self.skin_items()
        token = re.split(r"[^\w]", name)[0] or ""
        out = []
        for text, y in items:
            score = ocr.similarity(text, name)
            if token and token not in text and score < floor:
                continue
            if score >= floor:
                out.append((text, y, score))
        out.sort(key=lambda it: -it[2])
        return out

    def locate_skin_card(self, name, items=None, floor=0.6):
        """当前这一屏里最像目标的那张卡：(文字, 点击y, 相似度)，没有就 None。

        注意它只看**当前可见**的列表 —— 「找不到」不等于「游戏里没有」，
        完整链路是 select_skin 里那个「滚一屏再找」的循环。
        """
        hits = self.rank_skin_cards(name, items=items, floor=floor)
        return hits[0] if hits else None

    def skin_items(self):
        """读左侧列表里当前可见的皮肤名，返回 [(文字, 点击y)]，按 y 排序。

        只认含枪种关键词的行 —— 列表里还有「典藏」标签、价格、滚动条
        之类的噪声。

        点击 y 取「文字中心 + 40」而不是文字本身：卡片是一整块（皮肤名在
        上、「典藏」标签在下，两者相距约 78px），文字那一行正好压在卡片
        上边框附近，实测点文字 y=259 完全没响应；点卡片中部才选中。
        """
        img = self._snap()
        x0, x1 = self._SKIN_LIST_X
        y0, y1 = self._SKIN_LIST_Y
        # 放大 2 倍再认：皮肤名是 14px 的小字，1 倍认出来「·」全是「一」、
        # 名字还会被截断（「M4A1突击步枪·纯白涂装」→「步枪一纯白涂《」）。
        scale = 2
        big = vision.upscale(img[y0:y1, x0:x1], scale)
        words = self._ocr_words(big)
        # 词组间距要跟着放大倍数走：还是 14 的话（放大后相当于 7px），
        # 「AK-12突击步枪·水墨」会被拆成「AK-12突击步枪」和「水墨」两段。
        ph = ocr.phrases(words, gap=14 * scale)
        out = []
        for p in ph:
            t = p["text"]
            if not any(w in t for w in self._SKIN_NAME_WORDS):
                continue
            out.append((t, p["cy"] / scale + y0 + self._SKIN_CLICK_DY))
        out.sort(key=lambda it: it[1])
        return out

    def _buy_button_visible(self):
        """典藏外观页右侧有没有「购买」按钮 —— 有就说明皮肤已经选好了。

        真机实测位置：购买 (1664,876)-(1701,895)，售出 (1664,951)。
        只读这一小块，几十毫秒。
        """
        crop = self._snap()[850:915, 1600:1780]
        if getattr(crop, "size", 0) == 0:
            return False
        words = self._ocr_words(vision.upscale(crop, 2))
        return "购买" in "".join(w.get("text", "") for w in words)

    def pick_skin(self, y=None, index=0, name=None):
        """在典藏外观左侧列表里点一张卡片。

        name  —— 按皮肤名匹配（推荐）。读到列表后找包含 name 的那一项。
        y     —— 卡片中心（屏幕坐标），给了就用它
        index —— 既没给 name 也没给 y 时，按序号取（0 = 第一张）

        位置优先从 OCR 现读（skin_items），读不到才退回 SKIN_CARD_Y 固定表。
        原因：筛选后列表条目变了，固定表必然错位。

        读一次就点不可靠：筛选确认后列表会重新渲染，位置在「读」和「点」
        之间就变了。所以这里循环「重读 → 点 → 校验」，最多试 3 轮。

        **点不动不一定是失败**：列表只有一张卡时它本来就是选中状态，
        再点它画面不会变。实测筛选后只剩一项，逐 y 扫了 14 个位置，
        变化全部 0.05%（噪声级）—— 因为已经选中了。所以列表只剩一项
        时直接返回成功，不去点。

        注意：典藏外观页点卡片是**在页内切换选中**，画面会变但页面不变。
        所以这里不能要求页面变化，只能要求画面变化；选中是否真的换了，
        由调用方看右侧标题。
        """
        for attempt in range(3):
            ty = y
            if name or ty is None:
                items = self.skin_items()
                if not items:
                    # 读不到列表：退回固定表（只在没指定 name 时可行）
                    if name:
                        self.say(f"第 {attempt + 1} 轮读不到列表", "warn")
                        time.sleep(0.8)
                        continue
                    if not 0 <= index < len(SKIN_CARD_Y):
                        raise NavError(f"卡片序号 {index} 超出范围（共 "
                                       f"{len(SKIN_CARD_Y)} 张）")
                    ty = SKIN_CARD_Y[index]
                    self.say(f"读不到列表，退回固定表 y={ty}", "warn")
                elif name:
                    hit = [(t, yy) for t, yy in items if name in t]
                    if not hit:
                        raise NavError(f"左侧列表里找不到「{name}」"
                                       f"（看到的：{[t for t, _ in items]}）")
                    t, ty = hit[0]
                    self.say(f"列表命中「{t}」y={ty}", "info")
                    if len(items) == 1:
                        self.say("列表只有这一张，已处于选中状态，无需点击",
                                 "info")
                        return True
                else:
                    if not 0 <= index < len(items):
                        raise NavError(f"卡片序号 {index} 超出范围"
                                       f"（当前可见 {len(items)} 张）")
                    t, ty = items[index]
                    self.say(f"列表第 {index + 1} 张「{t}」y={ty}", "info")
                    if len(items) == 1:
                        self.say("列表只有这一张，已处于选中状态，无需点击",
                                 "info")
                        return True

            d = self.click((SKIN_CARD_X, ty), f"皮肤卡片 y={ty}", timeout=3.0)
            if d > 0.05:
                return True
            # 点的是**已经选中**的那张卡：画面当然不变。此时右侧已经显示
            # 「购买」，再点三轮也不会有任何变化，只会把一次正常的启动判成
            # 「点不中卡片」（真机实测：列表第一张默认就是选中态）。
            if self._buy_button_visible():
                self.say(f"卡片 y={ty} 已经是选中态（右侧有「购买」），不再点",
                         "info")
                return True
            page, _ = self.where()
            if page == "onsale":
                self.say(f"卡片 y={ty} 本来就在在售页，不用再点", "info")
                return True
            self.say(f"卡片 y={ty} 没响应，重读列表再试（第 {attempt + 1} 轮）",
                     "warn")
            time.sleep(0.6)

        self.say("连试 3 轮都没点中卡片", "warn")
        return False

    def buy(self):
        """点购买，进入在售页。

        这是全程最危险的一步：曼德尔砖页上「购买」(1642,959) 和
        「售出」(1634,1032) 只差 73px。所以这里**只认 COL_BUY 一个坐标**，
        不试备选。实测典藏外观页购买在 (1700,885)，变化 62.5%。

        校验：必须落到 detail（商品详情+挂单列表）或 onsale（挂单列表）。
        """
        if self.dry_run:
            self.say(f"[演练] 点购买 {COL_BUY}", "warn")
            return True
        # 点之前先确认右侧真的是「购买」。皮肤已经拥有时，这个位置变成
        # 「售出」—— 盲点就是把自己那件挂出去（真机踩过，用户当场喊停）。
        if not self._buy_button_visible():
            raise NavError("右侧看不到「购买」按钮（这个皮肤可能已经拥有了，"
                           "页面显示的是「售出」）—— 不点，先换一个皮肤")
        self.click(COL_BUY, "购买", timeout=6.0)
        time.sleep(1.5)
        self.require(("detail", "onsale"), "点购买")
        return True
