"""倒计时模式的外层流程：挑最早解锁的那件 + 抢不到换下一件。

真机背景（2026-10-21）：公示期一解锁是一堆人同时抢，第一件抢不到是常态。
以前 `_execute_countdown` 只认「用户已经选好的那一件」，抢不到就整个任务结束；
现在自己扫列表挑目标，抢不到就刷新换下一件，最多 COUNTDOWN_ATTEMPTS 件。

`choose_countdown_target` 是纯函数，选择规则全在它身上；外层循环只负责
「准备 → 盯 → 开点 → 记下这件试过了」。
"""
import threading

import numpy as np
import pytest

from core import engine as eng_mod
from core.engine import Aborted, DONE, Engine, choose_countdown_target


def _cd_words(sec, kind="unlock"):
    """真机词序列替身：`0 | 分 | N | 秒 | 后 | 解 | 锁 | 购 | 买`。

    引擎的 _ocr_clock 返回的是词列表（整串文本会把锁图标读出的「6」和
    真正的「0」粘成「60」，见 countdown.parse_words），桩必须同构。
    """
    if kind == "unlock":
        ws = ["0", "分", str(sec), "秒", "后", "解", "锁", "购", "买"]
    else:
        ws = ["剩余", "：", "0", "分", str(sec), "秒"]
    return [{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
            for i, t in enumerate(ws)]


# ---------------- 选择规则 ----------------

def test_picks_the_earliest_unlock():
    cands = [{"serial": "A", "sec": 30, "kind": "unlock"},
             {"serial": "B", "sec": 7, "kind": "unlock"},
             {"serial": "C", "sec": 12, "kind": "unlock"}]
    assert choose_countdown_target(cands)["serial"] == "B"


def test_ignores_listing_remaining_time():
    """「剩余：X分Y秒」归零即下架，抢它没意义 —— 再早也不能选。"""
    cands = [{"serial": "A", "sec": 3, "kind": "remain"},
             {"serial": "B", "sec": 40, "kind": "unlock"}]
    assert choose_countdown_target(cands)["serial"] == "B"


def test_ignores_unparsable_and_unknown():
    cands = [{"serial": "A", "sec": None, "kind": "unlock"},
             {"serial": "B", "sec": 5, "kind": None},
             {"serial": "C", "sec": 20, "kind": "unlock"}]
    assert choose_countdown_target(cands)["serial"] == "C"


def test_skips_already_tried():
    cands = [{"serial": "A", "sec": 3, "kind": "unlock"},
             {"serial": "B", "sec": 9, "kind": "unlock"}]
    assert choose_countdown_target(cands, tried=["A"])["serial"] == "B"


def test_returns_none_when_nothing_left():
    cands = [{"serial": "A", "sec": 3, "kind": "unlock"}]
    assert choose_countdown_target(cands, tried=["A"]) is None
    assert choose_countdown_target([]) is None


# ---------------- 外层循环 ----------------

class FakeCfg(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setdefault("regions", {
            "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1},
        })

    def region(self, name):
        return (self.get("regions") or {}).get(name)


class FakeScreen:
    def norm_to_px(self, box):
        return (100, 200, 50, 30)

    def grab(self, px=None):
        return np.zeros((4, 4, 3), np.uint8)


def _engine():
    e = Engine()
    e._stop = threading.Event()
    e._target_hwnd = 1
    # 这些用例测的是**实战**流程（重试、放弃、用户停止），不是演练。
    # Engine() 默认 _dry_run=True，而演练会在选中后直接收工、不去等倒计时，
    # 那样「换下一件」「连续失败就停」这些分支根本走不到。
    e._dry_run = False
    e._said = []
    e._say = lambda msg, level="info": e._said.append((level, msg))
    return e


TARGETS = [
    {"serial": "A", "sec": 5, "text": "0分5秒后解锁购买", "x": 1, "y": 2},
    {"serial": "B", "sec": 9, "text": "0分9秒后解锁购买", "x": 3, "y": 4},
    {"serial": "C", "sec": 20, "text": "0分20秒后解锁购买", "x": 5, "y": 6},
]


def test_retries_next_item_after_a_miss(monkeypatch):
    """第一件没抢到 → 换下一件；第二件抢到就收工。"""
    e = _engine()
    seen_tried = []
    calls = {"prep": 0, "watch": 0, "fire": 0}

    def prep(cfg, screen, mouse, tried, refresh=False):
        seen_tried.append(list(tried))
        i = calls["prep"]
        calls["prep"] += 1
        return TARGETS[i] if i < len(TARGETS) else None

    def watch(cfg, screen, **kw):      # **kw: already_unlocked
        calls["watch"] += 1

    def fire(*a, **k):
        calls["fire"] += 1
        return calls["fire"] >= 2        # 第一件失败，第二件成功

    monkeypatch.setattr(e, "_countdown_prepare", prep)
    monkeypatch.setattr(e, "_watch_countdown", watch)
    monkeypatch.setattr(e, "_fire_and_confirm", fire)

    e._execute_countdown(FakeCfg(), FakeScreen(), None, None)

    assert calls == {"prep": 2, "watch": 2, "fire": 2}
    assert e._state == DONE
    assert seen_tried == [[], ["A"]], "第二件必须知道第一件已经试过了"


def test_retries_when_the_item_disappears_before_unlock(monkeypatch):
    """没等到解锁这件就没了（被买走）→ 换下一件，而不是整个任务结束。"""
    e = _engine()
    calls = {"prep": 0, "watch": 0}

    def prep(cfg, screen, mouse, tried, refresh=False):
        i = calls["prep"]
        calls["prep"] += 1
        return TARGETS[i] if i < len(TARGETS) else None

    def watch(cfg, screen, **kw):      # **kw: already_unlocked
        calls["watch"] += 1
        if calls["watch"] == 1:
            raise Aborted("显示值 5 秒已经 2.5 秒没再跳 —— 倒计时停了")

    monkeypatch.setattr(e, "_countdown_prepare", prep)
    monkeypatch.setattr(e, "_watch_countdown", watch)
    monkeypatch.setattr(e, "_fire_and_confirm", lambda *a, **k: True)

    e._execute_countdown(FakeCfg(), FakeScreen(), None, None)

    assert calls["watch"] == 2
    assert e._state == DONE


def test_gives_up_after_the_attempt_limit(monkeypatch):
    e = _engine()
    calls = {"prep": 0}

    def prep(cfg, screen, mouse, tried, refresh=False):
        i = calls["prep"]
        calls["prep"] += 1
        return TARGETS[i % len(TARGETS)]

    monkeypatch.setattr(e, "_countdown_prepare", prep)
    monkeypatch.setattr(e, "_watch_countdown", lambda cfg, screen, **kw: None)
    monkeypatch.setattr(e, "_fire_and_confirm", lambda *a, **k: False)

    with pytest.raises(Aborted):
        e._execute_countdown(FakeCfg(), FakeScreen(), None, None)
    assert calls["prep"] == eng_mod.COUNTDOWN_ATTEMPTS


def test_aborts_when_nothing_qualified(monkeypatch):
    e = _engine()
    monkeypatch.setattr(e, "_countdown_prepare",
                        lambda cfg, screen, mouse, tried, refresh=False: None)
    with pytest.raises(Aborted):
        e._execute_countdown(FakeCfg(), FakeScreen(), None, None)


# ---------------- 翻页挑目标 ----------------

class FakeNav:
    """只记调用顺序；页面内容由 fake_scan 决定。"""

    made = []

    def __init__(self, *a, **k):
        FakeNav.made.append(self)
        self.calls = []
        self.page = 1

    def where(self):
        return "onsale", 0.99

    def pub_filter(self, want):
        self.calls.append(("pub_filter", want))

    def ons_refresh(self):
        self.calls.append(("refresh",))

    def ons_page(self):
        return self.page

    def ons_next_page(self):
        self.calls.append(("next_page",))
        self.page += 1
        return self.page

    def ons_prev_page(self):
        self.calls.append(("prev_page",))
        self.page = max(1, self.page - 1)
        return self.page


class FakeMouse:
    """只回答「能不能抢到前台」，点击由各测试自己记。"""

    def __init__(self, focus=True):
        self.focus = focus
        self.clicked = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return self.focus

    def click(self, x, y, **kw):
        self.clicked.append((x, y))


class ListScreen(FakeScreen):
    """grab 只看形状，真正的行由 market.scan 的假实现给。"""

    def __init__(self):
        self.grabbed = []

    def grab(self, px=None):
        self.grabbed.append(px)
        return np.zeros((8, 8, 3), np.uint8)


def _row(price):
    return {"text": "KC17 突击步枪造物纪元", "price": float(price), "craft": None,
            "wear": None, "wear_value": None, "x0": 40.0, "cy": 30.0}


def test_prepare_picks_the_earliest_unlock_on_the_first_page(monkeypatch):
    """只看第一页，在里面挑最早解锁的那件。

    用户定的：翻页没用，第一页就能抢。翻一页要 1.5 秒，公示期随时可能在
    翻页途中解锁，等翻回来那件已经被别人买走了。
    """
    e = _engine()
    e._dry_run = False
    e._target_hwnd = 1
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)

    rows = [_row(2050), _row(2888), _row(2050)]
    seen = {"scan": 0}

    def fake_scan(img):
        seen["scan"] += 1
        return rows

    monkeypatch.setattr(eng_mod.market, "scan", fake_scan)
    clocks = iter([_cd_words(300), _cd_words(45), _cd_words(90)])
    serials = iter(["P1B"])        # 只有挑中的那件才认序号
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: next(clocks))
    monkeypatch.setattr(e, "_read_serial", lambda scr: next(serials))
    clicks = []
    monkeypatch.setattr(e, "_click", lambda m, x, y: clicks.append((x, y)))

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1000, "max_price": 3000}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    target = e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [])

    assert target is not None
    assert target["serial"] == "P1B", "45 秒那件才是最早解锁的"
    assert target["sec"] == 45
    nav = FakeNav.made[0]
    assert [c[0] for c in nav.calls] == ["pub_filter"],         f"不该刷新、不该翻页，实际 {nav.calls}"
    # 必须切到「只显示公示期」：切成「不显示」的话列表里全是已解锁的挂单，
    # 根本没有「X分Y秒后解锁购买」可以等（真机踩过，白跑一整轮）
    assert ("pub_filter", "只显示公示期") in nav.calls
    assert seen["scan"] == 1, "只看第一页"
    # 只扫前 COUNTDOWN_SCAN_CARDS 张卡：前两张各点一次读倒计时，选中目标
    # 再点一次。第三张卡的倒计时一次都没读 —— 列表默认按解锁时间升序，扫
    # 全屏只是把时间烧在长倒计时上（用户 2026-10-23 定的）。
    assert len(clicks) == 3, "前两张卡各点一次 + 最后选中目标再点一次"
    assert next(clocks, "READ") == _cd_words(90), "第三张卡不该被读"
    # market.scan 的 x0/cy 是浮点，直接塞给 SetCursorPos 会 ArgumentError
    assert all(isinstance(v, int) for xy in clicks for v in xy),         "点击坐标必须是整数"


def test_prepare_never_pages(monkeypatch):
    """一页都不翻 —— 翻页途中公示期可能就解锁了，抢的就是那一下。"""
    e = _engine()
    e._dry_run = False
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    seen = {"scan": 0}

    def fake_scan(img):
        seen["scan"] += 1
        return [_row(2050)]

    monkeypatch.setattr(eng_mod.market, "scan", fake_scan)
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(30))
    monkeypatch.setattr(e, "_read_serial", lambda scr: "S1")
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1000, "max_price": 3000}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    target = e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [])
    assert target is not None and target["serial"] == "S1"
    assert seen["scan"] == 1
    assert "next_page" not in [c[0] for c in FakeNav.made[0].calls]


def test_prepare_gives_up_when_the_list_did_not_move(monkeypatch):
    """页码读不出、列表也没变 → 真的没有下一页了，别硬翻。"""
    e = _engine()
    e._dry_run = False
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(FakeNav, "ons_next_page", lambda self: None)
    seen = {"scan": 0}

    def fake_scan(img):
        seen["scan"] += 1
        return []

    monkeypatch.setattr(eng_mod.market, "scan", fake_scan)
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: [])
    monkeypatch.setattr(e, "_read_serial", lambda scr: "")
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    assert e._countdown_prepare(cfg, ListScreen(), FakeMouse(), []) is None
    # 一页都没有公示期的货 → 点一次刷新再看（用户定的），所以是两遍扫描
    assert seen["scan"] == 2, f"扫了 {seen['scan']} 遍"
    assert ("refresh",) in FakeNav.made[0].calls, "没货要点一次刷新"


def test_prepare_refuses_to_click_when_it_cannot_take_focus(monkeypatch):
    """抢不到前台就别开始点。

    真机实测：Hermes 窗口会自己弹回前台，`SetForegroundWindow` 又被前台锁
    挡掉。这时点下去只会落在别人的窗口上（甚至点到别的按钮），所以宁可直接
    报错让用户把游戏点回前台。
    """
    e = _engine()
    e._dry_run = False
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: [_row(2050)])

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1000, "max_price": 3000}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    with pytest.raises(Aborted) as ei:
        e._countdown_prepare(cfg, ListScreen(), FakeMouse(focus=False), [])
    assert "前台" in str(ei.value)
    assert not FakeNav.made, "抢不到前台就不该动界面"


def test_refresh_only_after_a_failed_attempt(monkeypatch):
    """第一轮不刷新，没抢到才刷新（用户定的）。

    每轮都刷会一直闪，而且刷新期间那件可能正好解锁，刷完就错过了。
    """
    e = _engine()
    e._dry_run = False
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: [_row(2050)])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(30))
    monkeypatch.setattr(e, "_read_serial", lambda scr: "S1")
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1000, "max_price": 3000}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [])
    first = [c[0] for c in FakeNav.made[-1].calls]
    assert "refresh" not in first, f"第一轮就刷新了：{first}"

    e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [], refresh=True)
    second = [c[0] for c in FakeNav.made[-1].calls]
    assert second.count("refresh") == 1, f"没抢到要刷新一次：{second}"


def test_user_stop_is_not_swallowed(monkeypatch):
    """用户按停不能当成「这件没了」继续换下一件。"""
    e = _engine()
    calls = {"watch": 0}

    def watch(cfg, screen, **kw):      # **kw: already_unlocked
        calls["watch"] += 1
        e._stop.set()
        raise Aborted("用户停止")

    monkeypatch.setattr(e, "_countdown_prepare",
                        lambda cfg, screen, mouse, tried, refresh=False: TARGETS[0])
    monkeypatch.setattr(e, "_watch_countdown", watch)
    with pytest.raises(Aborted) as ei:
        e._execute_countdown(FakeCfg(), FakeScreen(), None, None)
    assert calls["watch"] == 1, "按停之后不许再换下一件"
    assert "用户停止" in str(ei.value)


def test_prepare_goes_back_to_the_first_page_before_scanning(monkeypatch):
    """上一轮试完会停在最后一页；不先翻回去，第二轮就是从第 3 页开扫。

    真机踩过：列表停在第 3 页（空页），扫出 0 行，直接报「没有符合条件
    的商品」，其实第 1 页有货。
    """
    e = _engine()
    e._dry_run = False
    e._target_hwnd = 1
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)

    class Page3Nav(FakeNav):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.page = 3

    monkeypatch.setattr(eng_mod, "Navigator", Page3Nav)

    scanned_at = []

    def fake_scan(img):
        scanned_at.append(Page3Nav.made[-1].page)
        return [_row(2050)]

    monkeypatch.setattr(eng_mod.market, "scan", fake_scan)
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(45))
    monkeypatch.setattr(e, "_read_serial", lambda scr: "P1A")
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1000, "max_price": 3000}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    target = e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [])

    assert target is not None
    assert scanned_at, "一次都没扫"
    assert scanned_at[0] == 1, f"第一次扫描时停在第 {scanned_at[0]} 页，应当先翻回第 1 页"


def test_empty_list_triggers_exactly_one_refresh_then_gives_up(monkeypatch):
    """列表里一件公示期的都没有：点一次刷新再看，还没有就收工。

    不能一直刷 —— 用户明确说过「没抢到再去点刷新」，一轮只刷一次。
    """
    e = _engine()
    e._dry_run = False
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: [])
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    assert e._countdown_prepare(cfg, ListScreen(), FakeMouse(), []) is None
    calls = [c[0] for c in FakeNav.made[0].calls]
    assert calls.count("refresh") == 1, f"刷新了 {calls.count('refresh')} 次：{calls}"


def test_price_is_read_from_the_modal_when_the_list_shows_none(monkeypatch):
    """列表里价格读不出（公示期版式的价签会漂）→ 点进去从弹窗价格条读。

    真机踩过：整页 6 行全是「价格没读出来」，一件也抢不了，直接报
    「没有一件是 X分Y秒后解锁购买」收工。
    """
    e = _engine()
    e._dry_run = False
    e._target_hwnd = 1
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)

    no_price = dict(_row(0))
    no_price["price"] = None
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: [no_price])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(30))
    monkeypatch.setattr(e, "_read_serial", lambda scr: "S9")
    monkeypatch.setattr(e, "_bar_price", lambda cfg, scr: 2)
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1, "max_price": 10}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    target = e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [])
    assert target is not None, "价格能从弹窗读回来，就该有候选"
    assert target["serial"] == "S9"


def test_modal_price_outside_the_range_is_still_skipped(monkeypatch):
    """弹窗读回来的价超区间照样跳过 —— 不能因为补读就放行。"""
    e = _engine()
    e._dry_run = False
    e._target_hwnd = 1
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    no_price = dict(_row(0))
    no_price["price"] = None
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: [no_price])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(30))
    monkeypatch.setattr(e, "_read_serial", lambda scr: "S9")
    monkeypatch.setattr(e, "_bar_price", lambda cfg, scr: 99999)
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)

    cfg = FakeCfg()
    cfg["active_task"] = {"min_price": 1, "max_price": 10}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}

    assert e._countdown_prepare(cfg, ListScreen(), FakeMouse(), []) is None
