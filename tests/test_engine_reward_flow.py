"""成交后的两段新流程：等弹窗共享倒计时归零 + 收掉奖励弹窗。

真机背景（2026-10-23）：T-5s 点购买弹出的确认页和列表共享同一个倒计时，
归零前点购买毫无反应；成交后还有「跳过」→ 全屏展示页「继续」两层要收。
早点的代价用户当场抓到过（「你点早了」）—— 弹窗边框装饰是绿的，像素
检测会把没解锁的按钮误判成可点，所以解锁信号只认分秒文本归零。
"""
import threading

import numpy as np
import pytest

from core import engine as engine_mod
from core.engine import Engine


def _cd_words(sec, kind="unlock"):
    """真机词序列替身：`0 | 分 | N | 秒 | 后 | 解 | 锁 | 购 | 买`。"""
    if kind == "unlock":
        ws = ["0", "分", str(sec), "秒", "后", "解", "锁", "购", "买"]
    else:
        ws = ["剩余", "：", "0", "分", str(sec), "秒"]
    return [{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
            for i, t in enumerate(ws)]


class FakeScreen:
    def __init__(self):
        self.frame = np.zeros((60, 400, 3), np.uint8)

    def grab(self, box=None):
        return self.frame

    def norm_to_px(self, box):
        return (0, 0, 400, 60)


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None):
        self.clicks.append((x, y))


def _engine():
    e = Engine()
    e._dry_run = False
    e._target_hwnd = 123
    e._stop = threading.Event()
    said = []
    e._say = lambda msg, level="info": said.append((level, msg))
    return e, said


# ---------------- 等弹窗共享倒计时归零 ----------------

def test_dialog_unlock_waits_for_the_text_to_hit_zero(monkeypatch):
    """前四帧都还有 4 秒 —— 一下都不能早，更不能第一帧就当解锁。

    这就是用户当场抓到的「你点早了」：旧探针把弹窗绿色边框当成可点信号，
    cd=4s 就点了。钉住：只在读到 0 之后才放行。
    """
    e, _said = _engine()
    calls = {"n": 0}

    def fake_clock(scr, px):
        calls["n"] += 1
        return _cd_words(4 if calls["n"] < 5 else 0)

    monkeypatch.setattr(e, "_ocr_clock", fake_clock)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0) is True
    # 前 4 次轮询都在等，第 5 次读到 0 才放行 —— 早一次都是「点早了」
    assert calls["n"] == 5, f"读了 {calls['n']} 次就放行，应当等读到 0"


def test_dialog_unlock_accepts_the_text_vanishing_after_being_seen(monkeypatch):
    """读过分秒结构之后文本消失 = 解锁（归零后那一行换成价格/剩余）。"""
    e, _said = _engine()
    seq = iter([_cd_words(2), _cd_words(1), []])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: next(seq))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0) is True


def test_dialog_unlock_does_not_mistake_an_unread_first_frame_for_zero(monkeypatch):
    """弹窗刚弹出那一两帧可能什么都读不到 —— 那不是归零，不能放行。

    没有这条，开场一次 OCR 失误就会早点点出去，正是要防的事。
    """
    e, _said = _engine()
    # 前 3 次读不出（弹窗刚开的过渡帧），第 4 次起才读到 1 秒，再归零
    seq = iter([[], [], [], _cd_words(1), _cd_words(0)])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: next(seq))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    assert e._wait_dialog_unlock(FakeScreen(), timeout=5.0) is True
    assert list(seq) == [], "没读到倒计时的那几帧不该被当成解锁"


def test_dialog_unlock_gives_up_on_timeout_without_raising(monkeypatch):
    """一直读不到归零就超时返回 False，让调用方照点一次，不能挂死。"""
    e, said = _engine()
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: _cd_words(9))

    class FakeTime:
        """只替换 engine 模块里的 time 引用，不动全局 time。"""

        def __init__(self):
            self.t = 0.0

        def sleep(self, _s):
            self.t += 0.5

        def perf_counter(self):
            self.t += 1.0
            return self.t

    monkeypatch.setattr(engine_mod, "time", FakeTime())

    assert e._wait_dialog_unlock(FakeScreen(), timeout=3.0) is False
    assert any("超时" in msg for _lv, msg in said)


# ---------------- 收掉奖励流程 ----------------

def test_close_reward_clicks_skip_then_continue(monkeypatch):
    """奖励弹窗 → 点「跳过」→ 全屏展示页 → 点「继续」→ 回在售列表。"""
    e, said = _engine()
    e._bought_dialog = lambda screen: True
    mouse = FakeMouse()
    screens = iter([["获", "得", "外", "观"],          # 跳过之后的展示页
                    ["只", "显", "示", "公", "示", "期"]])  # 继续之后回列表
    monkeypatch.setattr(engine_mod.ocr, "read_words",
                        lambda img, **kw: [{"text": t, "x": i * 20, "y": 0,
                                      "w": 18, "h": 16}
                                     for i, t in enumerate(next(screens))])
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)

    e._close_reward(FakeScreen(), mouse)

    assert len(mouse.clicks) == 2, "先点跳过、再点继续，缺一下就回不到列表"
    assert mouse.clicks[0] == engine_mod.Engine._center(FakeScreen(),
                                                        engine_mod.SKIP_N)
    assert mouse.clicks[1] == engine_mod.CONT_CLICK
    assert any("已回在售列表" in msg for _lv, msg in said)


def test_close_reward_clicks_nothing_without_a_reward(monkeypatch):
    """没成交也来收尾就是乱点 —— 屏幕上没有奖励痕迹就一下都不点。"""
    e, _said = _engine()
    e._bought_dialog = lambda screen: False
    mouse = FakeMouse()

    e._close_reward(FakeScreen(), mouse)

    assert mouse.clicks == []


def test_bought_dialog_recognizes_the_new_reward_popup(monkeypatch):
    """成交判定认「获得/拥有」和「跳过」—— 旧版那行「购买成功」已不存在。"""
    e, _said = _engine()

    def fake_read(img, **kw):
        return "获得枪械外观 拥有：1"

    monkeypatch.setattr(engine_mod.ocr, "read_text", fake_read)
    assert e._bought_dialog(FakeScreen()) is True

    monkeypatch.setattr(engine_mod.ocr, "read_text", lambda img, **kw: "价格 2")
    assert e._bought_dialog(FakeScreen()) is False
