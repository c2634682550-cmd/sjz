"""成交截图：抢到之后留一张整屏图。

为什么值得单测：这张图是事后唯一能证明「到底买到了什么、花了多少」的东西。
它挂掉的方式都很安静 —— 关掉开关还存、演练也存、写失败把整场抢购带崩。

用假的 Screen/Mouse 跑真实的 `_fire_and_confirm` 成功分支，不点鼠标、不抓屏。
"""
import os
import sys

from collections import deque

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod
from core import records


@pytest.fixture(autouse=True)
def _no_real_records(tmp_path, monkeypatch):
    """成交截图那几条测试会走成功分支，顺带写成交记录 —— 别写进用户真实的 data/。"""
    monkeypatch.setattr(records, "PATH", str(tmp_path / "records.jsonl"))


class FakeScreen:
    def __init__(self, frame=None):
        self._frame = frame if frame is not None else np.zeros((100, 100, 3), np.uint8)

    def grab(self, box=None):
        return self._frame

    def norm_to_px(self, box):
        return (0, 0, 100, 100)

    def size(self):
        return 1920, 1080


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None):
        self.clicks.append((x, y))


class _Cfg(dict):
    """够用的配置假对象：只回引擎真会读的那几个键。"""

    def __init__(self, **kw):
        super().__init__({
            "threshold": 0.8,
            "click_hz": 200,
            "fire_timeout_s": 0.5,
            "confirm_clicks": 1,
            "confirm_interval_ms": 0,
            "failsafe": False,
            "save_shots": True,
            "active_task": {"name": "MK4 疾光魅影"},
            "regions": {"buy": {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.05},
                        "confirm": {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.05}},
        })
        self.update(kw)

    def region(self, name):
        return self["regions"][name]


@pytest.fixture
def eng():
    e = engine_mod.Engine()
    e._target_hwnd = 1
    e._log = deque()
    e._say = lambda msg, level="info": e._log.append((level, msg))
    e._guard_insufficient = lambda screen, mouse, tries=3, gap=0.35: None
    e._insufficient_showing = lambda screen: False
    return e


@pytest.fixture
def wired(monkeypatch):
    """把「确认弹窗出现」这件事变成必然，并把写盘换成记账。"""
    monkeypatch.setattr(engine_mod, "match", lambda img, tpl, *a, **k: (0.99, (0, 0)))
    written = []

    def fake_imwrite(path, img):
        written.append(path)
        return True

    monkeypatch.setattr(engine_mod, "imwrite", fake_imwrite)
    return written


def _run(eng):
    return eng._fire_and_confirm(_Cfg(), FakeScreen(), FakeMouse(),
                                 np.zeros((10, 10), np.uint8), (500, 500),
                                 __import__("time").perf_counter())


def test_success_saves_a_screenshot(eng, wired, monkeypatch):
    # 成交校验要看到结算弹窗；这条测的是「存图」，弹窗直接给个「看到了」
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    assert _run(eng) is True
    assert len(wired) == 1, "成交了却没存图"
    path = wired[0]
    assert os.path.basename(path).endswith(".jpg")
    assert "MK4" in os.path.basename(path), "文件名里要能认出抢的是哪件"
    assert os.path.basename(os.path.dirname(path)) == "shots"


def test_switch_off_saves_nothing(eng, wired, monkeypatch):
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    assert eng._fire_and_confirm(_Cfg(save_shots=False), FakeScreen(), FakeMouse(),
                                 np.zeros((10, 10), np.uint8), (500, 500),
                                 __import__("time").perf_counter()) is True
    assert wired == [], "关掉了还存图"


def test_write_failure_only_warns(eng, wired, monkeypatch):
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    monkeypatch.setattr(engine_mod, "imwrite", lambda path, img: False)
    assert _run(eng) is True, "存图失败不该把成交判成失败"
    assert any(lv == "warn" and "截图" in msg for lv, msg in eng._log)


def test_no_confirm_no_shot(eng, wired, monkeypatch):
    monkeypatch.setattr(engine_mod, "match", lambda img, tpl, *a, **k: (0.1, (0, 0)))
    assert _run(eng) is False
    assert wired == [], "没抢到却存了成交截图"
