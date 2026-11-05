"""引擎按什么走：任务说的那条路线。

以前工作流由全局 trigger_mode 决定，任务自己说了不算 —— 界面上写「公示期
倒计时归零就买」，配置里一改就悄悄换成另一条完全不同的路线。现在**任务
说了算**（workflow），全局配置只当没写时的默认值。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod


class FakeScreen:
    def __init__(self, monitor=1):
        self.size = (1920, 1080)

    def grab(self, box=None):
        return np.zeros((10, 10, 3), np.uint8)

    def norm_to_px(self, box):
        return (0, 0, 10, 10)

    def close(self):
        pass


class FakeMouse:
    def __init__(self, screen=None, game_hwnd=None, mode="interception"):
        # mode 是 2026-10-30 起的输入通道参数（interception/legacy）
        pass

    def click(self, *a, **k):
        pass


class FakeCfg:
    def __init__(self, values):
        self.values = values

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def region(self, name):
        return None


def _run(monkeypatch, mode, workflow, trigger_mode="vanish", default_workflow=None):
    cfg = FakeCfg({"monitor": 1, "trigger_mode": trigger_mode,
                   "threshold": 0.85, "failsafe": False})
    if default_workflow is not None:
        cfg.values["workflow"] = default_workflow
    e = engine_mod.Engine()
    e._target_hwnd = 1
    e._say = lambda *a, **k: None
    called = []
    e._execute_countdown = lambda *a: called.append("countdown")
    e._execute_list = lambda *a: called.append("list")
    e._execute = lambda *a: called.append("detail")
    e._navigate = lambda *a: called.append("navigate")
    e._load = lambda cfg, name: None      # 详情模式要读购买模板，这里不读文件

    monkeypatch.setattr(engine_mod, "Screen", FakeScreen)
    monkeypatch.setattr(engine_mod, "Mouse", FakeMouse)
    monkeypatch.setattr(engine_mod, "Config", type("C", (), {
        "load": staticmethod(lambda: cfg)}))

    e._run(cfg, mode, workflow)
    return called


def test_countdown_workflow_wins_over_list_mode(monkeypatch):
    """任务说走倒计时就走倒计时 —— 掉进列表模式会一直点刷新。"""
    called = _run(monkeypatch, "list", "countdown")
    assert called == ["countdown"], f"实际走了 {called} —— 列表模式会一直点刷新"
    assert "list" not in called


def test_list_workflow_walks_the_list(monkeypatch):
    called = _run(monkeypatch, "list", "list")
    assert called == ["navigate", "list"]


def test_detail_workflow_uses_the_button_watch(monkeypatch):
    called = _run(monkeypatch, "detail", "detail")
    assert called == ["detail"]


def test_task_workflow_overrides_the_global_default(monkeypatch):
    """全局默认是倒计时，任务点名要走列表 —— 听任务的。

    以前这里没有选择权：全局 trigger_mode 一改，界面上写的和实际跑的
    就对不上了。
    """
    called = _run(monkeypatch, "list", "list", default_workflow="countdown")
    assert called == ["navigate", "list"]


def test_without_a_task_workflow_the_global_default_is_used(monkeypatch):
    called = _run(monkeypatch, "list", None, default_workflow="list")
    assert called == ["navigate", "list"]
