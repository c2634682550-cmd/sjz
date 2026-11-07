"""开抢热键（F7）：热键跳过界面，但不能跳过启动前检查。

三种必须钉住的行为：
  1. 在跑的时候按 = 停，不是再开一场
  2. 检查没过就不开 —— 并把没过的那几项写进日志，否则按了没反应、查不出为什么
  3. 队列空（或全停用）不开

用假的调度器/配置/检查，不注册真热键、不抓屏。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import server


class FakeSched:
    def __init__(self, running=False):
        self._running = running
        self.notes = []
        self.started = []
        self.stops = 0

    def status(self):
        return {"running": self._running}

    def stop(self):
        self.stops += 1
        self._running = False
        return True, "已停止"

    def note(self, msg, level="info"):
        self.notes.append((level, msg))

    def start(self):
        self.started.append(True)
        self._running = True
        return True, "调度器已启动"


class FakeConfig:
    def __init__(self, queue):
        self._queue = queue

    def __getitem__(self, k):
        assert k == "queue"
        return self._queue

    @classmethod
    def load(cls):
        return cls(cls.queue)

    queue = []


def _facts(**kw):
    """preflight.collect 回来的「事实」。判断交给真的 evaluate。

    这里不替 evaluate 做决定 —— 决定权留在真代码里，才测得出
    「collect 的返回形状接得上 evaluate」这件事（之前就是这里形状不对，
    F7 开抢每次都抛异常）。
    """
    base = {"size": (1920, 1080), "ocr": True, "ocr_name": "WinRT",
            "clock": {"offset": 0.01, "at": 1700000000.0},
            "templates": {"buy": True, "confirm": True},
            "regions": {"buy": True, "confirm": True, "list": True},
            "page": "onsale"}
    base.update(kw)
    return base


@pytest.fixture
def env(monkeypatch):
    """把 server 里的三个外部依赖换成假的：调度器、配置、启动前检查。"""
    sched = FakeSched()
    monkeypatch.setattr(server, "_scheduler", sched)

    state = {"queue": [{"id": "a", "mode": "list", "enabled": True}],
             "facts": _facts(),
             "boom": None}

    class Cfg(dict):
        @classmethod
        def load(cls):
            return cls(queue=state["queue"])

    monkeypatch.setattr(server, "Config", Cfg)

    def collect(cfg, mode="list", workflow=None):
        if state["boom"]:
            raise state["boom"]
        return state["facts"]

    monkeypatch.setattr(server.preflight, "collect", collect)
    return sched, state


def test_pressing_while_running_stops(env):
    sched, _ = env
    sched._running = True
    server._hotkey_toggle()
    assert sched.stops == 1
    assert sched.started == [], "在跑的时候按开抢，又开了一场"


def test_blocked_by_preflight_does_not_start(env):
    sched, state = env
    # 用 OCR 不可用当「检查没过」的样本（页面不对现在只是警告，不拦）
    state["facts"] = _facts(ocr=False)
    server._hotkey_toggle()
    assert sched.started == [], "检查没过还是开抢了"
    assert any("OCR" in m for _, m in sched.notes), "没说是哪一项没过"


def test_all_checks_green_starts_the_scheduler(env):
    sched, _ = env
    server._hotkey_toggle()
    # 只剩实战一种路径：热键和按钮走同一个 start()。
    assert sched.started == [True], "热键没有真正启动调度器"
    assert any("热键开抢" in m for _, m in sched.notes)


def test_empty_queue_does_not_start(env):
    sched, state = env
    state["queue"] = []
    server._hotkey_toggle()
    assert sched.started == []
    assert any("队列是空的" in m for _, m in sched.notes)


def test_disabled_tasks_count_as_empty(env):
    sched, state = env
    state["queue"] = [{"id": "a", "mode": "list", "enabled": False}]
    server._hotkey_toggle()
    assert sched.started == []


def test_errors_are_logged_not_raised(env):
    sched, state = env
    state["boom"] = RuntimeError("抓屏失败")
    server._hotkey_toggle()          # 不能把异常抛进热键的消息循环
    assert any("出错" in m for _, m in sched.notes)
