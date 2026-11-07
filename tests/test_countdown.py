"""剩余时间：解析 + 跳秒相位锁定 + 引擎的倒计时触发。

三条契约：

  1. `parse` 只认「…秒」，不把价格/数量当倒计时
  2. 相位锁定给出的归零时刻，比「看到 0 分 0 秒」准将近一秒 ——
     这是整个倒计时触发模式存在的理由，退化它就等于没做
  3. 引擎在归零那一刻真的会返回（不是等到下一跳，也不是永不返回）
"""
import math
import random
import threading
import time

import numpy as np
import pytest

from core import countdown
from core import engine as eng_mod
from core.countdown import Clock
from core.engine import Engine


def _one_word(text):
    """把一整串文本当成一个词。

    真机上 OCR 吐出来的是词序列（见 _cd_words），但「整串被当成一个词」
    这种情况也真实存在 —— 解析必须照样认得出来。
    """
    return [{"text": text, "x": 0, "y": 0, "w": 18, "h": 16}]


# ---------------------------------------------------------------- 解析

@pytest.mark.parametrize("text,want", [
    ("剩余：0分6秒", 6),
    ("0剩余：0分6秒", 6),        # 真机 fx=1 实测，开头的 0 是时钟图标
    ("0剩余．0分6秒", 6),        # fx=3
    ("．剩余：0分6秒", 6),       # fx=6
    ("剩余，0分6秒", 6),         # fx=4
    ("1分30秒", 90),
    ("0分0秒", 0),
    ("12秒", 12),
    ("1小时2分3秒", 3723),
    ("０分６秒", 6),             # 全角数字
])
def test_parse_reads_countdown(text, want):
    assert countdown.parse_words(_one_word(text)) == want


@pytest.mark.parametrize("text", [
    "", "剩余", "18,888", "￥18,888", "编号000490743", "第1页",
])
def test_parse_rejects_non_countdown(text):
    """价格、编号、页码都不带「秒」，绝不能被当成倒计时。

    误吃的后果是归零时刻算错，抢购提前或延后 —— 提前点没用，延后点买不到。
    """
    assert countdown.parse_words(_one_word(text)) is None


# ---------------------------------------------------------------- 词序列解析

def _cd_words(sec, kind="unlock"):
    """把秒数变成真机那样的**词序列**。

    真机实测（2026-10-23）：OCR 吐出来的是 `0 | 分 | 43 | 秒 | 后 | 解 | 锁 |
    购 | 买` 这样的词，不是一个字符串。引擎靠词序列把锁图标读出的「6」和
    真正的「0」隔开 —— 整串文本解析会把它们粘成「60」，详见 parse_words。
    """
    if kind == "unlock":
        ws = ["0", "分", str(sec), "秒", "后", "解", "锁", "购", "买"]
    else:
        ws = ["剩余", "：", "0", "分", str(sec), "秒"]
    return [{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
            for i, t in enumerate(ws)]


@pytest.mark.parametrize("words,want", [
    (_cd_words(5), 5),
    (_cd_words(43), 43),
    (_cd_words(90), 90),
    (_cd_words(0), 0),
    # 真机事故复现：锁图标被读成「6」，和真正的「0」分成两个词。
    # 整串文本解析会把它当成 60 分 5 秒，傻等一小时。
    ([{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
      for i, t in enumerate(["6", "0", "分", "5", "秒", "后", "解", "锁", "购", "买"])], 5),
    # OCR 把「0分」粘成一个词时，数字在单位词自己身上
    ([{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
      for i, t in enumerate(["6", "0分", "5", "秒", "后", "解", "锁", "购", "买"])], 5),
    # 小时
    ([{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
      for i, t in enumerate(["1", "小时", "2", "分", "3", "秒"])], 3723),
])
def test_parse_words_reads_countdown(words, want):
    assert countdown.parse_words(words) == want


@pytest.mark.parametrize("words", [
    [],
    [{"text": "18,888", "x": 0, "y": 0, "w": 10, "h": 10}],
    [{"text": "编号000490743", "x": 0, "y": 0, "w": 10, "h": 10}],
    [{"text": "第1页", "x": 0, "y": 0, "w": 10, "h": 10}],
    # 只有分没有秒、只有秒没有分，都算读不出倒计时
    [{"text": "0", "x": 0, "y": 0, "w": 10, "h": 10},
     {"text": "分", "x": 20, "y": 0, "w": 10, "h": 10}],
])
def test_parse_words_rejects_non_countdown(words):
    """价格、编号、页码同样不许被词序列版当成倒计时。"""
    assert countdown.parse_words(words) is None


@pytest.mark.parametrize("words,want", [
    (_cd_words(27), "unlock"),
    (_cd_words(30, "remain"), "remain"),
    # 已解锁那行没有「秒」，整串解析会判 None，词序列版仍要认出 remain ——
    # 引擎要靠它报「你盯的是上架剩余时间」
    ([{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
      for i, t in enumerate(["剩余", "：", "2", "天", "23", "小时"])], "remain"),
    ([{"text": "已过期", "x": 0, "y": 0, "w": 10, "h": 10}], None),
    ([], None),
])
def test_kind_words_tells_the_two_countdowns_apart(words, want):
    assert countdown.kind_words(words) == want


# ---------------------------------------------------------------- 相位锁定

def test_phase_lock_predicts_zero_exactly():
    """每秒一跳、真实归零在 t0+7 时，锁定结果必须就是 t0+7。

    显示是 floor：在 t0 看到「6」的那一刻真实还剩 7 秒。
    """
    t0 = 1000.0
    c = Clock()
    for i in range(7):            # 6,5,4,3,2,1,0 分别在 t0 .. t0+6
        c.observe(6 - i, t0 + i)
    assert abs(c.zero_at() - (t0 + 7.0)) < 1e-9


def test_phase_lock_beats_waiting_for_zero():
    """核心契约：比「看到 0 分 0 秒」早将近一秒知道该点了。

    真机上「0分0秒」会持续整整一秒，等它才点最坏晚 1 秒。这里模拟一个
    真实归零在 t0+6 的倒计时，只用 6→5→4 三次跳秒就要能定出归零时刻。
    """
    t0 = 2000.0
    c = Clock()
    for i in range(3):            # 只喂 6、5、4
        c.observe(6 - i, t0 + i)
    z = c.zero_at()
    assert z is not None
    # 显示 6 的那一刻真实剩 7 秒（floor），所以归零在 t0+7
    assert abs(z - (t0 + 7.0)) < 1e-9
    # 还没跳到 0 就已经知道该点了
    assert c.due(t0 + 7.0)
    assert not c.due(t0 + 6.99)
    # 提前量：归零前 100ms 就算到点
    assert c.due(t0 + 6.9, lead=0.1)
    assert not c.due(t0 + 6.89, lead=0.1)


def test_late_first_observation_is_corrected_by_later_ticks():
    """进来时已经过了半秒，第一次估得松，后续跳秒必须把它收紧。

    这是 `min()` 的意义：第一次看到某个值时不知道它显示了多久，上界偏松；
    后面每次跳秒都给更紧的上界。
    """
    t0 = 3000.0
    c = Clock()
    c.observe(6, t0 + 0.7)                 # 晚了 0.7s，松上界 t0+7.7
    assert abs(c.zero_at() - (t0 + 7.7)) < 1e-9
    for i in range(1, 6):
        c.observe(6 - i, t0 + i)           # 后续跳秒收紧
    assert abs(c.zero_at() - (t0 + 7.0)) < 1e-9


def test_repeated_same_value_records_one_estimate():
    """同一个值连续看到很多帧只算一次观测。

    不这样的话 t+S 会越算越大（虽然 min 不受影响），而且每秒上百帧的
    观测会把列表撑爆。
    """
    c = Clock()
    for k in range(80):
        c.observe(6, 100.0 + k * 0.01)
    assert c.info()["ticks"] == 1
    assert c.zero_at() == 107.0


def test_countdown_restart_resets_estimates():
    """倒计时变长了 = 换了目标，旧观测必须作废。

    不作废的话 min() 被上一个目标的归零时刻钉死，会立刻判定「到点了」
    —— 对着新目标乱点。
    """
    c = Clock()
    c.observe(3, 100.0)
    assert c.zero_at() == 104.0
    c.observe(30, 101.0)          # 换成 30 秒的新目标
    assert c.zero_at() == 132.0


def test_due_is_false_without_any_observation():
    """一次都没读到就不能判定到点 —— 不能凭「没有数据」就开抢。"""
    c = Clock()
    assert c.zero_at() is None
    assert c.due(1e12) is False
    assert c.remaining(1e12) is None


def test_is_stale_only_after_data():
    """从来没读到过不算停更（那是配置问题，调用方另行报错）。"""
    c = Clock()
    assert c.is_stale(1e12) is False
    c.observe(9, 100.0)
    assert c.is_stale(100.5) is False
    assert c.is_stale(110.0) is True


# ---------------------------------------------------------------- 引擎接线

class FakeCfg(dict):
    def __init__(self, *a, clock=True, **kw):
        super().__init__(*a, **kw)
        self._clock = clock
        self.setdefault("trigger_mode", "countdown")

    def region(self, name):
        if name == "clock" and self._clock:
            return {"x": 0.8, "y": 0.5, "w": 0.1, "h": 0.02}
        return None


class ValidateCfg(FakeCfg):
    """带 region/has_template 的配置替身，专给 _validate 用。"""

    def __init__(self, regions=(), templates=(), trig="countdown"):
        super().__init__(clock=False, trigger_mode=trig)
        self._regions = set(regions)
        self._templates = set(templates)

    def region(self, name):
        return {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1} if name in self._regions else None

    def has_template(self, name):
        return name in self._templates


class FakeCountdownScreen:
    """假屏幕：像素值编码当前显示的秒数，秒数变了画面才变。

    真机上倒计时一秒跳一次，像素差那一步就是靠这个特性省掉 99% 的 OCR。

    model="ceil"  跳进 S 的那一刻剩余正好 S —— T+S 就是归零时刻
    model="floor" 跳进 S 的那一刻剩余是 S+1 —— T+S 早 1 秒（安全的一侧）
    """

    size = (1920, 1080)

    def __init__(self, zero_at, model="floor", const=None, floor_at=0,
                 subtle=False):
        self.zero_at = zero_at
        self.model = model
        self.const = const
        self.floor_at = floor_at     # 显示值不低于这个数（模拟「从不显示 0」）
        # subtle=True：值变了，但整幅图只有一个像素变 —— 真机上单个数字变化
        # 就长这样，均值差小到能被任何像素差阈值滤掉
        self.subtle = subtle

    def norm_to_px(self, box):
        return (0, 0, 16, 16)

    def grab(self, px=None):
        if self.const is not None:
            sec = self.const
        else:
            left = self.zero_at - time.perf_counter()
            sec = (max(0, math.ceil(left)) if self.model == "ceil"
                   else max(0, int(math.floor(left))))
            sec = max(self.floor_at, sec)
        img = np.zeros((16, 16, 3), np.uint8)
        if self.subtle:
            img[0, 0, 0] = sec
        else:
            img[:, :] = sec
        return img

    def close(self):
        pass


def _engine():
    e = Engine()
    e._stop = threading.Event()
    e._set = lambda **kw: None
    said = []
    e._say = lambda msg, level="info": said.append((level, msg))
    return e, said


def test_watch_countdown_fires_five_seconds_early(monkeypatch):
    """**距归零还有 5 秒**就开点 —— 用户实测那时按钮已经能点了。

    等归零是跟所有人同一刻抢；早 5 秒反而稳。

    钉住的是整条接线：像素差 → OCR → parse → observe → due。
    任何一环断掉，这个测试都会因为「等太久」而超时或晚到。
    """
    e, _said = _engine()
    zero = time.perf_counter() + 7.0
    screen = FakeCountdownScreen(zero)          # 默认 floor，就是实测的模型
    monkeypatch.setattr(eng_mod.ocr, "read_words",
                        lambda img, **kw: _cd_words(int(img[0, 0, 0])))
    e._watch_countdown(FakeCfg(failsafe=False), screen)
    late = time.perf_counter() - zero
    # 提前量 5 秒，正常落在 -5.0 附近；允许一秒抖动
    assert -5.5 <= late <= -4.5, f"距归零 {late * 1000:+.0f}ms，应当早 5 秒开点"


def test_watch_countdown_never_fires_late(monkeypatch):
    """显示值若是 floor（剩余在 [S,S+1)），估计会早 1 秒 —— 但绝不能晚。

    两个方向代价不对称：早了只是空点几下，_fire_and_confirm 会一直点到
    弹窗出现；晚了就是东西被别人买走。所以这条测试只钉「不许晚」。
    """
    e, _said = _engine()
    zero = time.perf_counter() + 7.0
    screen = FakeCountdownScreen(zero)
    monkeypatch.setattr(eng_mod.ocr, "read_words",
                        lambda img, **kw: _cd_words(int(img[0, 0, 0])))
    e._watch_countdown(FakeCfg(failsafe=False), screen)
    late = time.perf_counter() - zero
    assert late <= -4.5, f"距归零 {late * 1000:+.0f}ms —— 晚于「还剩 5 秒」才开点"
    assert late >= -5.5, f"早了 {-late * 1000:.0f}ms，提前量不该超过 5 秒太多"


def test_watch_countdown_never_needs_to_see_zero(monkeypatch):
    """界面从 1 直接跳到「没了」、一帧 0 都不显示时，也必须在归零那刻开点。

    这才是相位锁定存在的理由。真机上「0分0秒」可能根本不会出现，凡是
    「等看到 0 再点」的实现在这里都会一直等到看门狗把它掐掉。
    """
    e, _said = _engine()
    seen = []

    def fake_ocr(img, **kw):
        v = int(img[0, 0, 0])
        seen.append(v)
        return _cd_words(v)

    monkeypatch.setattr(eng_mod.ocr, "read_words", fake_ocr)
    zero = time.perf_counter() + 7.0
    screen = FakeCountdownScreen(zero, floor_at=1)
    dog = threading.Timer(9.0, e._stop.set)   # 卡住就掐掉，别让测试挂死
    dog.start()
    try:
        e._watch_countdown(FakeCfg(failsafe=False), screen)
    finally:
        dog.cancel()
    late = time.perf_counter() - zero
    # 显示值是 floor，相位估计本身就会差一秒左右：5 秒提前量实际落在
    # 4~5.5 秒之间都算对，关键是「必须早于归零」
    assert -6.0 <= late <= -3.8, f"距归零 {late * 1000:+.0f}ms"
    assert 0 not in seen, "假界面不该显示 0 —— 显示了说明测的不是这条路"


def test_watch_countdown_raises_without_region():
    """没框选倒计时区域要直接报错，不能空转到天荒地老。"""
    e, _said = _engine()
    with pytest.raises(RuntimeError):
        e._watch_countdown(FakeCfg(failsafe=False, clock=False),
                           FakeCountdownScreen(time.perf_counter() + 5))


def test_watch_countdown_aborts_when_region_reads_nothing(monkeypatch):
    """框错了区域（读到的是不动的画面）也要中止。

    这种情况下像素差恒为 0，OCR 一次都不跑，`is_stale` 也永远不成立
    —— 曾经就是这样在真机上无限空转。6 秒兜底必须把它救出来。
    """
    e, _said = _engine()
    monkeypatch.setattr(eng_mod, "CLOCK_READ_TIMEOUT_S", 0.3)
    monkeypatch.setattr(eng_mod.ocr, "read_words", lambda img, **kw: [])
    screen = FakeCountdownScreen(time.perf_counter() + 99.0, const=7)
    t0 = time.perf_counter()
    with pytest.raises(eng_mod.Aborted):
        e._watch_countdown(FakeCfg(failsafe=False), screen)
    assert time.perf_counter() - t0 < 2.0


def test_watch_countdown_aborts_when_countdown_freezes(monkeypatch):
    """倒计时停更（界面切走/被买走）要中止，不能一直等下去。

    停更时 `due()` 永远是 False，没有这条就会卡死到用户手动停。
    真机上的停更就是「文字不变 → 像素不变 → 不再 OCR」，所以这里给一张
    恒定不变的图。
    """
    e, _said = _engine()
    monkeypatch.setattr(eng_mod.ocr, "read_words", lambda img, **kw: _cd_words(9))
    monkeypatch.setattr(countdown, "STALE_S", 0.25)
    screen = FakeCountdownScreen(time.perf_counter() + 9.0, const=9)
    with pytest.raises(eng_mod.Aborted):
        e._watch_countdown(FakeCfg(failsafe=False), screen)


def test_validate_countdown_needs_clock():
    """倒计时模式没框「倒计时」区域就该拦住，并说清楚缺什么。"""
    from core.engine import Engine
    problem = Engine._validate(ValidateCfg(regions={"buy", "confirm"},
                                           templates={"confirm"}))
    assert problem and "倒计时" in problem


def test_validate_countdown_does_not_need_buy_template():
    """倒计时模式不看购买按钮长什么样，只关心它在哪 —— 别拿模板拦它。

    这一条是真机上会踩的：vanish/appear 必须先截到按钮模板，而倒计时
    模式下按钮从「不可点」到「可点」可能压根没有可辨识的像素差异。
    """
    from core.engine import Engine
    problem = Engine._validate(ValidateCfg(
        regions={"clock", "buy", "confirm"}, templates={"confirm"}),
        workflow="countdown")
    assert problem is None, problem


def test_validate_vanish_still_needs_buy_template():
    """反过来，详情页路线（vanish）少模板仍然必须拦住 —— 别被上面的分支改坏。"""
    from core.engine import Engine
    problem = Engine._validate(ValidateCfg(
        regions={"clock", "buy", "confirm"}, templates={"confirm"},
        trig="vanish"), workflow="detail")
    assert problem and "购买按钮模板" in problem


def test_watch_countdown_reads_even_when_pixels_barely_change(monkeypatch):
    """单个数字变化只改十几个像素 —— 不能因为「画面看起来没变」就不 OCR。

    真机原话（2026-10-21）：20 次真实跳秒里，`mean(absdiff)>0.5` 这道门漏了
    7 次；而这块区域自身噪声能到 0.756，两个区间重叠，阈值怎么调都不行。
    漏两次就触发 2.5 秒的「停更」误判，把好好的倒计时掐掉 —— 生产循环就是
    这么在第 20 秒自杀的。所以这里给一个「值变了但画面只动一个像素」的假
    屏幕：任何像素差预筛都会在这里饿死。
    """
    e, _said = _engine()
    # 停更阈值调到 1.6 秒：比一次跳秒（1 秒）长、比整段倒计时（3 秒）短，
    # 这样「只读到第一帧就再也不读」会被判停更，而正常跳秒不会。
    monkeypatch.setattr(countdown, "STALE_S", 1.6)
    zero = time.perf_counter() + 8.0
    screen = FakeCountdownScreen(zero, subtle=True)
    monkeypatch.setattr(eng_mod.ocr, "read_words",
                        lambda img, **kw: _cd_words(int(img[0, 0, 0])))
    e._watch_countdown(FakeCfg(failsafe=False), screen)
    late = time.perf_counter() - zero
    # 显示值是 floor，相位估计本身就会差一秒左右：5 秒提前量实际落在
    # 4~5.5 秒之间都算对，关键是「必须早于归零」
    assert -6.0 <= late <= -3.8, f"距归零 {late * 1000:+.0f}ms"


@pytest.mark.parametrize("text,want", [
    ("0分27秒后解锁购买", "unlock"),
    ("01 分 7 秒后解锁购买", "unlock"),
    ("剩余：2分31秒", "remain"),
    ("0 剩余 ： 3 分 27 秒", "remain"),
    ("已过期", None),
    ("", None),
    ("18,888", None),
])
def test_kind_tells_the_two_countdowns_apart(text, want):
    """「…后解锁购买」和「剩余：…」归零后结果相反，必须能分开。

    真机实测（2026-10-21）：在售页那行「剩余：X分Y秒」归零时商品直接变
    「已过期」、按钮变灰；公示期那行「X分Y秒后解锁购买」归零才解锁购买。
    """
    assert countdown.kind_words(_one_word(text)) == want


def test_watch_countdown_refuses_listing_remaining_time(monkeypatch):
    """盯着「剩余：…」掐点是白等 —— 引擎要当场拒绝，而不是闷头等归零。"""
    e, _said = _engine()
    monkeypatch.setattr(eng_mod.ocr, "read_words", lambda img, **kw: _cd_words(30, "remain"))
    screen = FakeCountdownScreen(time.perf_counter() + 30.0)
    with pytest.raises(eng_mod.Aborted) as ei:
        e._watch_countdown(FakeCfg(failsafe=False), screen)
    msg = str(ei.value)
    assert "剩余" in msg and "只显示公示期" in msg


def test_watch_countdown_fires_through_the_unlock_flip(monkeypatch):
    """解锁那一刻文字会换成「剩余：…」，不能因此重置相位估计。

    真机实测：公示期到点后同一行立刻变成上架剩余时间。要是照常 observe，
    Clock 看到「值变大」会 reset，zero_at 被推到新目标的归零时刻，永远等
    不到开点。这里用「剩余：1分30秒」（能解析出 90 秒，比刚才的 0 秒大），
    并把提前量设成负的（晚 200ms 才开点），强制循环必须跨过那次文字翻转。
    """
    e, _said = _engine()
    zero = time.perf_counter() + 2.0
    screen = FakeCountdownScreen(zero, subtle=True)
    monkeypatch.setattr(eng_mod, "FIRE_LEAD_S", -0.2)
    flipped = {"n": 0}

    def fake_ocr(img, **kw):
        if time.perf_counter() >= zero:
            flipped["n"] += 1
            return _cd_words(90, "remain")
        return _cd_words(int(img[0, 0, 0]))

    monkeypatch.setattr(eng_mod.ocr, "read_words", fake_ocr)
    e._watch_countdown(FakeCfg(failsafe=False), screen)
    late = time.perf_counter() - zero
    assert flipped["n"] > 0, "循环根本没跨过解锁那一刻，测试是空转的"
    assert 0.0 <= late <= 0.5, f"距归零 {late * 1000:+.0f}ms"


# ---------------------------------------------------------------- T0 反推
#
# 契约（2026-11 独立审查后重写，与实现解耦，逐条对应审查里的 P0）：
#   1. 不可信时 `zero_at / remaining` 必须是 None、`due` 必须 False；
#      原始粗估只能从 `zero_at_raw` 拿到，调用方拿不到会被误用的数
#   2. 显示值冻结 0.90~1.60s（含「一开始就冻结」和「跑一截再冻结」）
#      一律不可信，`due()` 自己挡住，不依赖调用方先调 `gate()`
#   3. inf / NaN / 离谱大数 / 非法时间戳：忽略或明确拒绝，绝不许抛
#   4. 只喂跳变沿（没有「跳变前一帧」）锁不死 T0；喂成对数据可以压到毫秒级
#   5. 时钟前跳 / 后跳 / 缓冲乱序：要么重置，要么明确不可信

def _series(t0, start_s, n, dt=(0.004, 0.012), seed=0, err_rate=0.0,
            drop_rate=0.0):
    """造观测序列：读数 = floor(t0 - t)，可带 OCR 错读和丢帧。

    t 轴放到 1000 秒量级，和真机 perf_counter 同数量级 —— 用 0 起会掩盖
    大数相减的浮点精度问题。
    """
    rng = random.Random(seed)
    t = t0 - start_s
    obs = []
    for _ in range(n):
        t += rng.uniform(*dt)
        true = int(math.floor(t0 - t))
        if true < 0:
            break
        if rng.random() < drop_rate:
            continue
        bad = rng.random() < err_rate
        s = true
        if bad:
            # 单字 OCR 抖动：±1 为主，偶尔 ±2
            s = max(0, true + rng.choice([-1, -1, -1, -1, 1, 1, 1, 1, -2, 2]))
        obs.append((t, s, bad))
    return obs


def _feed(obs, **kw):
    est = countdown.T0Estimator(**kw)
    for t, s, _bad in obs:
        est.observe(s, t)
    return est


def _pairs(t0, ticks, seed=0, err_rate=0.075, dt=0.008, start_left=None):
    """只喂「跳变前一帧 `(t, S+1)`」+「跳变后第一帧 `(t, S)`」的成对数据。

    这是审查里指出的另一种喂法：不是只喂跳变沿，而是把跳变前后的两帧
    都交出来。每个跳变 2 条读数，6 个跳变 12 条。

    取对按**真值跳变**触发（物理上就是那一次跳秒），前一帧的读数用干净值
    `S+1`（契约就是这样喂）；跳变后那一帧允许 ±1 错读 —— 错读会把这对的
    `sa-sb==1` 破坏，由实现自己筛掉，这里不替它擦屁股。
    """
    rng = random.Random(seed)
    if start_left is None:
        start_left = ticks + 2.0
    t = t0 - start_left
    obs = []
    prev_true = int(math.floor(t0 - t))
    prev_frame = None
    real = 0
    while real < ticks and t0 - t < 60:
        t += max(rng.uniform(0.55 * dt, 1.45 * dt), 0.0015)
        s = int(math.floor(t0 - t))
        if s < 0:
            break
        bad = rng.random() < err_rate
        so = max(0, s + rng.choice([-1, -1, -1, -1, 1, 1, 1, 1, -2, 2]))
        if not bad:
            so = s
        if s != prev_true:                 # 真跳变：交出一对
            real += 1
            prev_true = s
            if prev_frame is not None:
                obs.append(prev_frame)
            obs.append((t, so))
        prev_frame = (t, s)
    return obs


def _feed_pairs(t0, ticks, seed, err_rate=0.075):
    est = countdown.T0Estimator()
    for t, s in _pairs(t0, ticks, seed, err_rate):
        est.observe(s, t)
    return est


def test_estimator_first_reading_is_raw_only():
    """第一个读数就有粗估，但只能从 `zero_at_raw` 拿；对外的 `zero_at` 是 None。"""
    t0 = 1050.0
    est = countdown.T0Estimator()
    est.observe(6, t0 - 6.2)
    e = est.estimate(want_qpc=False)
    assert e.n == 1 and e.n_inliers == 1
    assert abs(e.zero_at_raw - t0) <= 0.51
    assert e.zero_at is None
    assert not e.trust
    assert "有效读数" in e.reason


def test_estimator_two_readings_still_untrusted():
    """最坏采样条件：只读到 1~2 个读数。误差仍有几百毫秒，必须报不可信。"""
    t0 = 1050.0
    est = countdown.T0Estimator()
    obs = _series(t0, 6.0, 2, seed=3)
    for t, s, _bad in obs:
        est.observe(s, t)
    e = est.estimate(want_qpc=False)
    assert e.n == 2
    assert abs(e.zero_at_raw - t0) <= 0.51
    assert e.zero_at is None
    assert not e.trust


def test_untrusted_never_exposes_a_usable_number():
    """P0-2：不可信时 zero_at / remaining / due / info 都不能给出像样的数。"""
    est = countdown.T0Estimator()
    est.observe(6, 1000.0)
    e = est.estimate(want_qpc=False)
    assert not e.trust
    assert e.zero_at is None
    assert est.zero_at() is None
    assert est.remaining(1000.2) is None
    assert est.due(1e9) is False
    assert est.trusted() is False
    assert est.info()["zero_at"] is None


@pytest.mark.parametrize("mode,dur", [
    ("start", 0.90), ("start", 0.96), ("start", 0.99), ("start", 1.05),
    ("start", 1.20), ("start", 1.50), ("start", 1.60),
    ("after", 0.90), ("after", 0.96), ("after", 0.99), ("after", 1.05),
    ("after", 1.20), ("after", 1.50), ("after", 1.60),
])
def test_frozen_display_is_never_trusted(mode, dur):
    """P0-1：冻结 0.96~1.5s 被判成可信是审查的原始 bug。

    start：缓冲里只有一段冻结读数（相当于一进来界面就不动）
    after：先跑 1.4s 正常副（读数 3、2），再冻结 dur 秒
    """
    t0 = 1050.0
    dt = 0.010
    obs = []
    if mode == "start":
        for k in range(int(dur / dt)):
            obs.append((t0 - 1.0 + k * dt, 9))
    else:
        t = t0 - 3.0
        while t < t0 - 1.6 - 0.001:
            t += dt
            obs.append((t, int(math.floor(t0 - t))))
        fv = int(math.floor(t0 - t))
        for k in range(int(dur / dt)):
            t += dt
            obs.append((t, fv))
    est = countdown.T0Estimator()
    for tt, ss in obs:
        est.observe(ss, tt)
    e = est.estimate(want_qpc=False)
    assert not e.trust, f"{mode}/{dur}s 冻结被判可信：{e.reason}"
    assert e.zero_at is None
    assert est.zero_at() is None
    # due() 自己必须挡住 —— 不传 gate 也要 False
    assert est.due(t0) is False
    assert est.due(t0 - 0.2) is False


def test_frozen_reading_still_untrusted_after_long_freeze():
    """同一读数持续 2s 以上（界面切走）：更不许可信，也不许 due。"""
    est = countdown.T0Estimator()
    for k in range(200):
        est.observe(9, 1000.0 + k * 0.01)
    e = est.estimate(want_qpc=False)
    assert not e.trust
    assert e.zero_at is None
    assert est.due(1009.0) is False
    assert est.is_frozen(1002.5) is True


def test_estimator_converges_on_dense_clean_readings():
    """干净密集采样（≈2.4 秒、300 帧）：误差要进 20ms，且报可信。"""
    t0 = 1050.0
    obs = _series(t0, 20.0, 300, seed=7)
    est = _feed(obs)
    e = est.estimate(want_qpc=False)
    assert abs(e.zero_at - t0) < 0.020, f"误差 {(e.zero_at - t0) * 1000:.1f}ms"
    assert e.half_width < 0.020
    assert e.trust, e.reason


def test_estimator_survives_wrong_digits():
    """8% 错读（±1/±2）：估计误差仍进 20ms，大部分错读被剔掉。"""
    t0 = 1050.0
    obs = _series(t0, 25.0, 320, seed=11, err_rate=0.08)
    est = _feed(obs)
    e = est.estimate(want_qpc=False)
    assert abs(e.zero_at - t0) < 0.020, f"误差 {(e.zero_at - t0) * 1000:.1f}ms"
    assert e.trust, e.reason
    # 估计器只保留最近 maxlen 条，比对也要对齐到这段窗口
    buf = obs[-est.maxlen:]
    bad = [o for o in buf if o[2]]
    assert len(bad) >= 15, "样本里错读太少，测不到剔除逻辑"
    assert e.n_outliers >= len(bad) * 0.9, (e.n_outliers, len(bad))
    flagged = sum(1 for i, o in enumerate(buf) if o[2] and not e.mask[i])
    assert flagged >= len(bad) * 0.9, (flagged, len(bad))


def test_estimator_tolerates_lost_frames():
    """3Hz 低采样（丢帧 + 间隔 0.3 秒）：精度下降但仍有界、可解释。"""
    t0 = 1050.0
    obs = _series(t0, 12.0, 40, dt=(0.28, 0.32), seed=5)
    est = _feed(obs)
    e = est.estimate(tolerance_s=0.05, want_qpc=False)
    assert abs(e.zero_at - t0) < 0.050, f"误差 {(e.zero_at - t0) * 1000:.1f}ms"
    assert e.trust, e.reason


def test_estimator_resets_on_target_switch():
    """读数突然变大 20 秒以上 = 换了目标，旧观测作废。"""
    est = countdown.T0Estimator()
    for i in range(10):
        est.observe(5, 1000.0 + i * 0.1)
    est.observe(120, 1001.5)            # 新目标，剩余 120 秒
    e = est.estimate(want_qpc=False)
    assert e.n == 1
    assert abs(e.zero_at_raw - (1001.5 + 120 + 0.5)) <= 0.51


def test_estimator_small_ocr_jump_is_not_a_switch():
    """OCR 把 5 读成 6（+1）不算换目标，只当异常值，不能被 reset 掉历史。"""
    est = countdown.T0Estimator()
    t0 = 1050.0
    est.observe(5, t0 - 5.4)
    est.observe(6, t0 - 4.9)            # 错读
    assert est.estimate(want_qpc=False).n == 2


def test_estimator_stale_and_empty():
    est = countdown.T0Estimator()
    assert est.is_stale(1e12) is False
    assert est.estimate(want_qpc=False).zero_at is None
    est.observe(9, 1000.0)
    assert est.is_stale(1002.5) is False
    assert est.is_stale(1003.0) is True


@pytest.mark.parametrize("bad", [
    float("inf"), float("-inf"), float("nan"), 1e300, -1, 6.7, None, True,
])
def test_observe_rejects_bad_reading_without_raising(bad):
    """P0-3：inf/NaN/离谱值/非整数一律拒绝，不许抛 OverflowError。"""
    est = countdown.T0Estimator()
    assert est.observe(bad, 1000.0) is False
    assert est.estimate(want_qpc=False).reason_code == "no_data"


@pytest.mark.parametrize("bad_t", [
    float("nan"), float("inf"), float("-inf"), 1e300, "x",
])
def test_observe_rejects_bad_timestamp_without_raising(bad_t):
    est = countdown.T0Estimator()
    assert est.observe(9, bad_t) is False
    assert est.estimate(want_qpc=False).reason_code == "no_data"


def test_estimate_survives_poisoned_buffer():
    """就算有人绕过 observe 往缓冲里塞 NaN，estimate 也不许抛。"""
    est = countdown.T0Estimator()
    est.observe(9, 1000.0)
    est._buf.append((float("nan"), 8))
    est._buf.append((1001.0, float("inf")))
    e = est.estimate(want_qpc=False)    # 不抛即通过
    assert e.trust is False


def test_clock_backward_jump_resets():
    """NTP 后跳 / 手动改表：旧缓冲必须作废。"""
    est = countdown.T0Estimator()
    for i in range(5):
        est.observe(9 - i, 1000.0 + i * 0.1)
    assert est.observe(6, 998.0) is True     # 倒退 1.4s
    e = est.estimate(want_qpc=False)
    assert e.n == 1
    assert e.zero_at is None
    assert est.info()["note"] == "clock_backward"


def test_clock_forward_gap_resets():
    """长时间断喂（前跳 >8s）：两段轴混在一起没有意义，必须清空。"""
    est = countdown.T0Estimator()
    for i in range(5):
        est.observe(9 - i, 1000.0 + i * 0.1)
    assert est.observe(6, 1010.0) is True
    e = est.estimate(want_qpc=False)
    assert e.n == 1
    assert e.zero_at is None
    assert est.info()["note"] == "clock_gap"


def test_out_of_order_buffer_is_untrusted():
    """缓冲里出现明显倒退（例如两个线程打点没加锁）：明确不可信。"""
    est = countdown.T0Estimator()
    est._buf.append((1001.0, 5))
    est._buf.append((999.0, 5))
    e = est.estimate(want_qpc=False)
    assert not e.trust
    assert e.reason_code == "bad_time"


def test_tick_edges_only_cannot_lock():
    """只喂跳变沿（每个跳变 1 条读数）：数学上锁不死，必须报不可信。"""
    t0 = 1050.0
    est = countdown.T0Estimator()
    for i in range(16):
        est.observe(20 - i, t0 - 20.0 + i)
    e = est.estimate(want_qpc=False)
    assert not e.trust
    assert e.zero_at is None
    assert e.reason_code == "sparse"


@pytest.mark.parametrize("seed", [1, 3, 5, 7, 9, 11])
def test_jump_pairs_lock_to_milliseconds(seed):
    """P0 后继：6 个跳变 = 6 对 12 条读数，误差要压到毫秒级。"""
    t0 = 1050.0
    est = _feed_pairs(t0, 6, seed)
    e = est.estimate(want_qpc=False)
    assert e.trust, e.reason
    assert abs(e.zero_at - t0) < 0.010, f"误差 {(e.zero_at - t0) * 1000:.2f}ms"
    assert e.half_width < 0.010
    assert e.n_ticks >= 3


@pytest.mark.parametrize("seed", [1, 4, 7])
def test_pair_feed_passes_gate(seed):
    """成对喂法整批只有十几条读数，`gate()` 不能拿「≥100 条」把它全拒。

    gate 的 100 条门槛是为整帧流定的；成对档用「≥12 条 + 跨度 ≥5s」等价替换，
    否则只喂跳变的调用方永远点不出去（就算估计已经准到 ±2ms）。
    """
    t0 = 1050.0
    est = _feed_pairs(t0, 10, seed)          # 10 次跳变 ≈ 14~20 条读数
    e = est.estimate(want_qpc=False)
    ok, why = countdown.gate(e)
    assert ok, f"{why}（inl={e.n_inliers} span={e.span:.2f}s hw={e.half_width * 1e3:.2f}ms）"
    assert e.n_inliers < 100 and e.span >= 5.0
    assert e.admissible


@pytest.mark.parametrize("seed", [2, 5, 9])
def test_jump_pairs_never_false_trust_under_heavy_noise(seed):
    """15% 错读下：要么不可信，要么可信且误差 < 50ms，不许假可信。"""
    t0 = 1050.0
    est = _feed_pairs(t0, 6, seed, err_rate=0.15)
    e = est.estimate(want_qpc=False)
    if e.trust:
        assert abs(e.zero_at - t0) < 0.050


def test_single_tick_with_plus_one_misread_cannot_fake_a_narrow_interval():
    """一个 tick 内两条 +1 错读会把交集从「整秒宽」钉成 8ms 的假窄点。

    实测（tools/bench，30 条全是同一个读数）：错读落在 v=t+s 的高端，
    交集变成 [max v, min v+1)，宽度 8ms，中点偏 +541ms，而内点计数 27/30
    完全正常 —— 只靠计数和半宽都拦不住。必须靠「读数只能不变或变小」这条
    物理约束把 +1 错读整条剔掉，剔完后交集回到 0.6s 宽 → wide → 不可信。
    """
    t0 = 1050.0
    t = t0 - 31.6
    obs = []
    for _ in range(30):
        t += 0.008
        obs.append((t, int(math.floor(t0 - t)), False))
    obs[1] = (obs[1][0], obs[1][1] + 1, True)
    obs[2] = (obs[2][0], obs[2][1] + 1, True)
    e = _feed(obs).estimate(want_qpc=False)
    assert not e.trust, e.reason
    assert e.zero_at is None
    assert e.mask[1] is False and e.mask[2] is False


def test_120_readings_is_borderline_and_honestly_bounded():
    """120 条 ≈0.96s：区间半宽正好卡在 20ms 容差线上，可信率会掉。

    最坏相位（窗口完全落在同一格读数内部）下，交集宽度就是 1-0.96=40ms，
    半宽 20ms，一半样本被判 wide；这不是算法问题，是数据只剩这么多信息。
    这里钉住两点：仍有一半可信；所有可信样本误差都在 20ms 内。
    随机相位下的整体表现见 tools/bench_countdown.json。
    """
    ok = 0
    errs = []
    for seed in range(1, 21):
        t0 = 1050.0
        obs = _series(t0, 8.0, 120, seed=seed, err_rate=0.075)
        est = _feed(obs)
        e = est.estimate(want_qpc=False)
        if e.trust:
            ok += 1
            errs.append(abs(e.zero_at - t0))
    assert ok >= 8, f"120 条只有 {ok}/20 可信，退化比报告里写的更严重"
    assert max(errs) < 0.020, f"可信样本里出现 {max(errs) * 1000:.1f}ms 误差"


def test_expired_target_reports_negative_remaining():
    """T0 已过期：remaining 返回负数（由上层判断），不是 None 也不是 0。"""
    t0 = 1050.0
    est = _feed(_series(t0, 6.0, 240, seed=4))
    e = est.estimate(want_qpc=False)
    assert e.trust, e.reason
    r = est.remaining(t0 + 0.5)
    assert r is not None and -0.55 < r < -0.45, r


def test_due_fires_near_zero_on_dense_feed():
    """正常密集数据：归零前 False、归零后 True，且别被「距上次跳变 ≈1s」误挡。"""
    t0 = 1050.0
    est = _feed(_series(t0, 3.0, 320, seed=7))
    assert est.estimate(want_qpc=False).trust
    assert est.due(t0 - 0.3) is False
    assert est.due(t0 + 0.05) is True


def test_estimator_zero_at_qpc_is_qpc_count():
    """T0 要能以 QPC 计数给出，且与 perf_counter 秒一致（Windows）。"""
    freq = countdown.qpc_frequency()
    if freq is None:
        pytest.skip("非 Windows，没有 QPC")
    t0 = 1050.0
    est = _feed(_series(t0, 20.0, 300, seed=9))
    e = est.estimate()
    assert isinstance(e.zero_at_qpc, int)
    p = time.perf_counter()
    q = countdown.qpc_now()
    gap = (e.zero_at_qpc - q) / freq - (e.zero_at - p)
    assert abs(gap) < 0.020, f"QPC 与秒轴差 {gap * 1000:.1f}ms"
