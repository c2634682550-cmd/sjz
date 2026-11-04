"""点击流程的延迟预算。

抢购拼的就是这几百毫秒。这组用例把「哪一段该多快」钉死，防止以后有人
顺手加个 sleep 又把它拖回去。

实测基线（本机 1920×1080，OCR 走本地服务）：

    抓确认弹窗区 340×90        1.85ms
    模板匹配                    0.25ms
    驱动点击（3 次 send_mouse） 0.01ms
    读一次倒计时（240×65 OCR）  24.7ms

也就是说「截屏+匹配」只要 2ms，任何超过这个量级的固定等待都是在白送。

被替换掉的三个固定开销（2026-11-03 优化前）：
    _wait_dialog_unlock 轮询 250ms   → 解锁最多晚 250ms 才发现
    解锁后固定 sleep 0.6s            → 无条件多等
    确认盲点 20 下 × 10ms            → 第一下成功也白等 190ms
合计最坏 ~1.03s，现在这些等待都变成了闭环。
"""

import copy
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod
from core.config import DEFAULTS


class _Screen:
    size = (1920, 1080)

    def grab(self, box=None):
        return np.zeros((40, 40, 3), np.uint8)

    def norm_to_px(self, box):
        return (0, 0, 40, 40)

    def close(self):
        pass


class _Mouse:
    def __init__(self):
        self.clicks = 0

    def click(self, x, y, **kw):
        self.clicks += 1
        return True

    def ensure_foreground(self, hwnd, screen=None):
        return True


class _Cfg(dict):
    def region(self, name):
        return self["regions"].get(name)


def _cfg(**over):
    cfg = _Cfg(copy.deepcopy(DEFAULTS))
    cfg["regions"] = {
        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
        "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1},
        "clock": {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.05},
    }
    cfg.update(over)
    return cfg


def _quiet(eng, monkeypatch, match):
    """把引擎里所有会碰真环境的钩子换成假的。"""
    monkeypatch.setattr(engine_mod, "match", match)
    monkeypatch.setattr(eng, "_click", lambda m, x, y: None)
    eng._insufficient_showing = lambda s: False
    eng._guard_insufficient = lambda *a, **k: None
    eng._wait_dialog_unlock = lambda *a, **k: True
    eng._balance = lambda *a, **k: None
    eng._bought_dialog = lambda s: True
    eng._close_reward = lambda *a, **k: None
    eng._record = lambda *a, **k: None
    eng._say = lambda *a, **k: None


# ---------------------------------------------------------------- 解锁检测

def test_unlock_waits_for_the_real_zero(monkeypatch):
    """读到「0秒」**不能**立刻返回 —— 显示值是 floor，那一刻真实还剩 0~1 秒。

    以前读到 0 就返回，点下去游戏直接忽略：用户当场抓过「你抢早了」，
    连续三单没抢到（2026-11-03）。现在按相位估计等到真正的零点
    （零点 = 读到时 + 秒数 + 1），既不能早、也不能晚太多。
    """
    eng = engine_mod.Engine()
    eng._dry_run = False
    eng._say = lambda *a, **k: None
    shown_zero_at = 0.30          # 屏幕开始显示「0秒」
    real_zero_at = shown_zero_at + 1.0   # 真正解锁（显示 0 的那一整秒）
    t0 = [None]

    def fake_clock(screen, px):
        now = time.perf_counter()
        if t0[0] is None:
            t0[0] = now
        elapsed = now - t0[0]
        left = real_zero_at - elapsed
        # 屏幕显示的是 floor：真实剩 0.4 秒时显示「0秒」
        text = "0秒" if left < 1.0 else "1秒"
        return [{"text": text, "x": 0, "y": 0}]

    monkeypatch.setattr(eng, "_ocr_clock", fake_clock)
    t_start = time.perf_counter()
    assert eng._wait_dialog_unlock(_Screen()) is True
    elapsed = time.perf_counter() - t_start

    # 不能早：早于真零点就是白点（游戏会忽略，那一下也就废了）
    assert elapsed >= real_zero_at - 0.05, \
        f"提前 {((real_zero_at - elapsed) * 1000):.0f}ms 就返回了 —— 这是「抢早了」"
    # 也不能晚太多：真零点之后 150ms 内必须返回
    late = elapsed - real_zero_at
    assert late < 0.15, f"真归零后 {late * 1000:.0f}ms 才返回"


# ---------------------------------------------------------------- 确认闭环

def test_confirm_stops_as_soon_as_the_dialog_closes(monkeypatch):
    """确认弹窗一消失就收手，不再盲点满 20 下。

    以前盲点 20 × 10ms：第一下成功的话，剩下 19 下全打在奖励弹窗上
    （那上面有「跳过」），还白等 190ms。
    """
    eng = engine_mod.Engine()
    eng._dry_run = False
    mouse = _Mouse()
    state = {"open": True}

    def fake_match(img, tpl, *a, **k):
        return (0.99 if state["open"] else 0.1), (0, 0)

    def fake_click(m, x, y):
        mouse.clicks += 1
        # 购买按钮在 (500,500)，确认按钮在确认区域中心。只有点「确认」
        # 才让弹窗消失 —— 购买那一下是把弹窗点出来。
        if (x, y) != (500, 500):
            state["open"] = False

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    t = time.perf_counter()
    assert eng._fire_and_confirm(_cfg(), _Screen(), mouse,
                                 np.zeros((10, 10), np.uint8), (500, 500), t) is True
    elapsed = time.perf_counter() - t

    assert mouse.clicks <= 3, f"点了 {mouse.clicks} 下才停（闭环应该 1~2 下）"
    assert elapsed < 0.12, f"整轮用了 {elapsed * 1000:.0f}ms"


def test_confirm_keeps_clicking_while_the_dialog_is_still_there(monkeypatch):
    """弹窗还开着就一直点（覆盖「按钮比倒计时晚亮」），但不超过兜底时长。"""
    eng = engine_mod.Engine()
    eng._dry_run = False
    mouse = _Mouse()
    state = {"n": 0}

    def fake_match(img, tpl, *a, **k):
        return 0.99, (0, 0)          # 弹窗永远开着

    def fake_click(m, x, y):
        mouse.clicks += 1
        state["n"] += 1
        # 弹窗永远关不掉（模拟按钮一直没亮）

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    t = time.perf_counter()
    eng._fire_and_confirm(_cfg(confirm_timeout_s=0.2, confirm_clicks=3),
                          _Screen(), mouse, np.zeros((10, 10), np.uint8),
                          (500, 500), t)
    elapsed = time.perf_counter() - t
    # 至少点够 3 下（confirm_clicks），但被 0.2s 兜底掐住
    assert mouse.clicks >= 3
    assert elapsed < 0.6, f"兜底时长没生效：{elapsed:.2f}s"


# ---------------------------------------------------------------- 空档采样

def test_dialog_appearing_between_clicks_is_seen_within_a_few_ms(monkeypatch):
    """两次点击之间出现的弹窗，不该等「睡醒」才发现。

    以前空档是纯 sleep(20ms)：弹窗在中间出现，平均白等 10ms、最坏 20ms。
    现在按 4ms 采样。
    """
    eng = engine_mod.Engine()
    eng._dry_run = False
    mouse = _Mouse()
    t0 = [0.0]
    appear_at = 0.050
    seen = {"at": None, "closed": False}

    def fake_match(img, tpl, *a, **k):
        now = time.perf_counter()
        open_now = (now - t0[0]) >= appear_at and not seen["closed"]
        if open_now and seen["at"] is None:
            seen["at"] = now
        return (0.99 if open_now else 0.1), (0, 0)

    def fake_click(m, x, y):
        mouse.clicks += 1
        if (x, y) != (500, 500) and seen["at"] is not None:
            seen["closed"] = True

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    t0[0] = time.perf_counter()
    eng._fire_and_confirm(_cfg(), _Screen(), mouse,
                          np.zeros((10, 10), np.uint8), (500, 500), t0[0])
    assert seen["at"] is not None, "弹窗一直没被认出来"
    late = (seen["at"] - t0[0]) - appear_at
    assert late < 0.020, f"空档里晚了 {late * 1000:.0f}ms 才发现（旧实现最坏 20ms）"


# ---------------------------------------------------------------- 点击频率

def test_click_hz_is_actually_reached(monkeypatch):
    """配置的 click_hz 要真的达到，而不是被每轮的截屏+匹配吃掉。

    以前是「干完活再睡满 interval」，实际只有 44Hz；现在睡「间隔 - 耗时」。
    """
    eng = engine_mod.Engine()
    eng._dry_run = False
    mouse = _Mouse()

    def fake_match(img, tpl, *a, **k):
        return 0.1, (0, 0)           # 弹窗永不出现

    def fake_click(m, x, y):
        mouse.clicks += 1

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_click", fake_click)

    cfg = _cfg(click_hz=50, fire_timeout_s=0.4)
    t = time.perf_counter()
    eng._fire_and_confirm(cfg, _Screen(), mouse, np.zeros((10, 10), np.uint8),
                          (500, 500), t)
    elapsed = time.perf_counter() - t
    hz = mouse.clicks / elapsed
    # 50Hz × 0.4s ≈ 20 下。给足余量（负载高的机器会抖），但不能低到 35Hz
    assert hz >= 35, f"实际只有 {hz:.0f}Hz（配置 50Hz）"
