"""找皮肤：从整个典藏外观库里精确找到并选中某一件。

为什么单独一个模块
------------------
「找一件皮肤」是这个程序最核心的动作，但它由三件事拼起来：筛（缩小范围）、
滚（遍历列表）、认（读名字 + 点开确认）。以前这三件事散在 nav_col.select_skin
里，只有一个「筛 + 滚 40 屏」的直线流程 —— 筛选条件一旦跟实际不符（知识库把
枪种写成「步枪」而游戏叫「战斗步枪」、赛季对不上），就一路滚到底也找不到，
报一句「列表里找不到」了事。

这里的做法是**逐级放宽**：先用最精确的组合筛，找不到就把条件一个个摘掉再找，
最后一档是全库扫描。这样只要这件皮肤在库里，就一定能翻到 —— 代价是慢，
但「找不到」比「慢」严重得多。

实测数据（2026-11-02，1920×1080）
  · 一格滚轮 = 一张卡（滚 1/2/3 格分别移动 1/2/3 张，没有惯性）
  · 屏幕上一屏 7 张卡
  · 一屏 OCR ≈ 250ms，点开读标题 ≈ 350ms
所以一次滚动取 6 格（重叠 1 张，保证不漏），一屏 7 张覆盖 6 张新卡。
"""
import time

from . import ocr, skinfilter

# 一次滚几格。一格 = 一张卡，一屏 7 张 → 滚 6 格重叠 1 张，既不漏也最快。
DEFAULT_STEP = 6
# 单次扫描最多看多少屏。7 张/屏 × 60 屏 ≈ 400 张，比全库（243 件）还宽。
MAX_SCREENS = 60
# 标题相似度到多少算认对了。列表名会被截断，标题才是全名。
TITLE_FLOOR = 0.72
# 列表名相似度低于它、且型号也不匹配的，不点开（省时间）
CAND_FLOOR = 0.6


class SkinFinder:
    """在典藏外观页找到并选中一件皮肤。"""

    def __init__(self, nav, say=None, step=DEFAULT_STEP, max_screens=MAX_SCREENS):
        self.nav = nav
        self._say = say or (lambda msg, level="info": None)
        self.step = max(1, int(step))
        self.max_screens = max(1, int(max_screens))
        self.screens = 0
        self.tried = 0
        self._baseline_done = False
        self._narrowed = False

    # ---------------- 对外 ----------------

    def find(self, name, season=None, tier=None, gun=None):
        """找到 name 并选中它，返回结果字典。

        返回：
          ok        找到了没有
          y         卡片中心 y（屏幕坐标，已点开并确认）
          title     点开后读到的完整名字
          score     标题与目标的相似度
          plan      最终生效的筛选方案（"全筛" / "不限枪种" / …）
          screens   一共看了几屏
          tried     一共点开了几张卡
          elapsed_s 耗时
        """
        t0 = time.time()
        name = (name or "").strip()
        if not name:
            return {"ok": False, "msg": "名字是空的", "screens": 0, "tried": 0}

        plans = self._plans(name, season, tier, gun)
        self.screens = 0
        self.tried = 0
        last_err = None

        # 0. 先看当前这一屏，不动筛选。
        #    抢购前用户通常已经把这件选好了，这时候只需要确认一眼 ——
        #    实测走完整流程（复位 + 选赛季 + 扫）要一两分钟，而这一步
        #    只要几百毫秒。找不到再走下面的筛选流程。
        hit = self._scan_current(name)
        if hit:
            hit.update(ok=True, plan="当前屏", screens=1, tried=self.tried,
                       elapsed_s=round(time.time() - t0, 1), quick=True)
            self._say(f"当前屏就有「{hit['title']}」", "hit")
            return hit

        for i, plan in enumerate(plans):
            t_filter = time.time()
            try:
                self._apply(plan, baseline=(not self._baseline_done))
                self._baseline_done = True
            except Exception as e:                  # noqa: BLE001
                last_err = f"套用筛选「{plan['label']}」失败：{e}"
                self._say(last_err, "warn")
                continue
            # 筛空了就立刻停手：游戏这时会把面板变成居中浮层，坐标全变，
            # 后面所有点击都会落空。与其在那儿瞎点，不如直接说清楚。
            if not self.nav.skin_items():
                return {"ok": False, "screens": self.screens, "tried": self.tried,
                        "elapsed_s": round(time.time() - t0, 1),
                        "msg": f"筛「{plan['label']}」把列表筛空了 —— "
                               "面板已变成居中浮层，请手动把筛选复位后再试"}

            t_scan = time.time()
            hit = self._scan(name)
            if hit:
                hit.update(ok=True, plan=plan["label"],
                           elapsed_s=round(time.time() - t0, 1),
                           filter_s=round(t_scan - t_filter, 1),
                           scan_s=round(time.time() - t_scan, 1),
                           screens=self.screens, tried=self.tried)
                self._say(f"找到「{hit['title']}」（{plan['label']}，"
                          f"{self.screens} 屏 / 点开 {self.tried} 次）", "hit")
                return hit

        return {"ok": False, "screens": self.screens, "tried": self.tried,
                "elapsed_s": round(time.time() - t0, 1),
                "msg": last_err or f"翻遍列表也没找到「{name}」"}

    # ---------------- 筛选方案 ----------------

    def _plans(self, name, season, tier, gun):
        """按「筛选表」给出从精确到宽松的方案。

        表（core/skins_raw.csv）里写着每件皮肤的：赛季下拉选哪个砖名、
        品阶第几个框、枪种选哪个框。三样都齐、而且这个组合在表里确实有货
        （`skinfilter.count > 0`）时，才把三样都用上 —— 因为**筛出空列表**
        会让游戏把筛选面板变成屏幕中央的浮层，坐标全变、复位点不动，
        整个流程卡死（实测 2026-11-02）。

        所以方案是：
          1. 赛季 + 品阶 + 枪种（表里保证非空）→ 一屏就够
          2. 只按赛季                          → 一个赛季二十来件
          3. 全库扫描                          → 兜底，243 件一定翻得到
        """
        p = skinfilter.plan(name) if not any((season, tier, gun)) else None
        # 调用方直接给了品阶（引擎那条路）时，也要换算成「第几个框」
        tier_index = (skinfilter.TIERS.index(tier)
                      if tier in skinfilter.TIERS else None)
        if p:
            season = p["season_item"]
            tier = p["tier"]
            gun = p["gun"]
            tier_index = p["tier_index"]

        plans = []
        if season and tier and gun and skinfilter.count(season, tier, gun) > 0:
            plans.append({"season": season, "tier": tier, "gun": gun,
                          "tier_index": tier_index,
                          "label": f"{season}·{tier}·{gun}"})
        if season:
            plans.append({"season": season, "tier": None, "gun": None,
                          "label": f"赛季 {season}"})
        plans.append({"season": None, "tier": None, "gun": None,
                      "label": "全库扫描"})
        return plans

    def _apply(self, plan, baseline=False):
        """把筛选面板调成 plan 指定的样子。

        baseline=True 时先做一次全复位（品阶/枪种/拥有全勾上）—— 只做一次，
        因为它要点十几个框，一次就够。之后每档只改赛季。
        """
        nav = self.nav
        nav.goto_collection()
        if baseline:
            nav.col_filter_reset()
        before_items = nav.skin_items()
        before = before_items[0][0] if before_items else None

        nav.col_filter()
        nav.col_pick_season(plan.get("season") or "全部赛季")
        if plan.get("tier_index") is not None:
            nav.set_tier(plan["tier_index"], only=True)
        elif self._narrowed:
            # 上一档筛过品阶，这一档不筛了 —— 必须把它放回去，
            # 否则面板记着上次的品阶，放宽等于没放宽（实测会一直找不到）。
            for i in range(4):
                nav.col_check("rarity", i, want=True)
        if plan.get("gun"):
            nav.set_gun(plan["gun"])
        elif self._narrowed:
            nav.set_gun(None)
        self._narrowed = bool(plan.get("tier_index") is not None or plan.get("gun"))
        nav.col_filter_confirm()
        # 列表重载期间读到的是旧的（甚至空的），要等它刷出新内容
        nav._wait_skin_list(before=before)
        self._say(f"筛选：{plan['label']}", "info")

    # ---------------- 扫描 ----------------

    def _scan_current(self, name):
        """只在当前这一屏里找，不滚动、不动筛选。"""
        try:
            items = self.nav.skin_items()
        except Exception:                        # noqa: BLE001
            return None
        if not items:
            return None
        self.screens += 1
        for text, y, score in self.nav.rank_skin_cards(name, items=items,
                                                       floor=CAND_FLOOR):
            self.tried += 1
            got = self._open_and_verify(text, y, name)
            if got:
                return got
        return None

    def _scan(self, name):
        """从列表顶部往下翻，边翻边认。找到返回结果，翻完没有返回 None。"""
        nav = self.nav
        nav.skin_scroll_top()
        seen = set()
        tried = set()
        empty_streak = 0

        for screen in range(self.max_screens):
            items = nav.skin_items()
            self.screens += 1
            if not items:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                nav.skin_scroll(self.step)
                continue
            empty_streak = 0

            fresh = [t for t, _ in items if t not in seen]
            seen.update(t for t, _ in items)

            for text, y, score in nav.rank_skin_cards(name, items=items,
                                                      floor=CAND_FLOOR):
                if text in tried:
                    continue
                tried.add(text)
                self.tried += 1
                got = self._open_and_verify(text, y, name)
                if got:
                    return got

            # 这一屏全是见过的 = 到底了（滚不动了）
            if screen and not fresh:
                break
            nav.skin_scroll(self.step)
        return None

    def _open_and_verify(self, text, y, name):
        """点开一张卡，读标题确认。对上了返回结果，没对上返回 None。"""
        nav = self.nav
        nav.click((150, y), label="皮肤卡片", expect_change=False)
        time.sleep(0.35)
        title = nav.skin_title()
        if not title:
            return None
        score = ocr.similarity(title, name)
        if score < TITLE_FLOOR:
            self._say(f"「{text}」点开是「{title}」（{score:.2f}），不是它", "info")
            return None
        return {"y": int(y), "title": title, "score": round(score, 3),
                "list_text": text}


def find(nav, name, season=None, tier=None, gun=None, say=None):
    """一行调用的便捷入口。"""
    return SkinFinder(nav, say=say).find(name, season=season, tier=tier, gun=gun)
