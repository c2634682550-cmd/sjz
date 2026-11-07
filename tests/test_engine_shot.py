"""成交截图：抢到之后把画面丢给后台队列，由 shotproof 存图打水印。

为什么值得单测：这张图是事后唯一能证明「到底买到了什么、花了多少」的东西。
挂掉的方式都很安静 —— 关掉开关还投递、演练也投递、投递失败把整场抢购带崩。

这里只验证成交路径「投递了什么」；真正的抓屏/绘图/落盘在 test_shotproof.py。
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

    base_rect = {"left": 0, "top": 0, "width": 100, "height": 100}


class FakeMouse:
    def __init__(self):
        self.clicks = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None, **kw):
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


class FakeWorker:
    """只记账的假工作线程：不抓屏、不落盘。"""

    def __init__(self, fail=False):
        self.tasks = []
        self.fail = fail
        self.closed = False

    def submit(self, rect, meta, opts):
        if self.fail:
            raise RuntimeError("boom")
        self.tasks.append((rect, meta, opts))
        return True

    def close(self):
        self.closed = True


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
    """把「确认弹窗出现」变成必然，随机截图队列换成记账。"""
    monkeypatch.setattr(engine_mod, "match",
                        lambda img, tpl, *a, **k: (0.99, (0, 0)))
    worker = FakeWorker()
    monkeypatch.setattr(engine_mod.Engine, "_shot_worker", lambda self: worker)
    return worker


def _run(eng, cfg=None):
    return eng._fire_and_confirm(cfg or _Cfg(), FakeScreen(), FakeMouse(),
                                 np.zeros((10, 10), np.uint8), (500, 500),
                                 __import__("time").perf_counter())


def test_success_queues_a_watermarked_shot(eng, wired, monkeypatch):
    # 成交校验要看到结算弹窗；这条测的是「投递了什么」，弹窗直接给个「看到了」
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    assert _run(eng) is True
    assert len(wired.tasks) == 1, "成交了却没投递截图任务"
    rect, meta, opts = wired.tasks[0]
    assert rect["width"] == 100 and rect["height"] == 100
    name = meta["_name"]
    assert name.endswith(".png")
    assert "MK4" in name, "文件名里要能认出抢的是哪件"
    assert os.path.basename(os.path.dirname(os.path.join(opts["dir"], name))) == "shots"
    # 真实数据：物品名 / 品阶 / 价格 / 延迟 / 轮次都在 meta 里
    assert meta["item"] == "MK4 疾光魅影"
    assert meta["tier"], "品阶不许空着"
    assert meta["round"] >= 1
    assert opts["watermark"] is True and opts["keep_raw"] is True


def test_switch_off_queues_nothing(eng, wired, monkeypatch):
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    assert _run(eng, _Cfg(save_shots=False)) is True
    assert wired.tasks == [], "关掉了还投递截图"


def test_enabled_off_queues_nothing(eng, wired, monkeypatch):
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    assert _run(eng, _Cfg(shot={"enabled": False})) is True
    assert wired.tasks == [], "shot.enabled 关掉了还投递截图"


def test_submit_failure_only_warns(eng, monkeypatch):
    monkeypatch.setattr(engine_mod, "match",
                        lambda img, tpl, *a, **k: (0.99, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    monkeypatch.setattr(engine_mod.Engine, "_shot_worker",
                        lambda self: FakeWorker(fail=True))
    assert _run(eng) is True, "截图失败不该把成交判成失败"
    assert any(lv == "warn" and "截图" in msg for lv, msg in eng._log)


def test_no_confirm_no_shot(eng, wired, monkeypatch):
    monkeypatch.setattr(engine_mod, "match",
                        lambda img, tpl, *a, **k: (0.1, (0, 0)))
    assert _run(eng) is False
    assert wired.tasks == [], "没抢到却存了成交截图"


def test_no_client_rect_only_warns(eng, wired, monkeypatch):
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)

    class NoRect(FakeScreen):
        base_rect = None

    assert eng._fire_and_confirm(_Cfg(), NoRect(), FakeMouse(),
                                 np.zeros((10, 10), np.uint8), (500, 500),
                                 __import__("time").perf_counter()) is True
    assert wired.tasks == []
    assert any(lv == "warn" and "截图" in msg for lv, msg in eng._log)


def test_hit_to_disk_end_to_end(eng, monkeypatch):
    """成交 → 投递 → 后台抓屏 → 水印 → 落盘 → 界面提示，全链路走一遍。"""
    from core import shotproof

    monkeypatch.setattr(engine_mod, "match",
                        lambda img, tpl, *a, **k: (0.99, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    monkeypatch.setattr(
        shotproof.Worker, "_grab",
        lambda self, rect: np.zeros((540, 960, 3), np.uint8)[:, :, ::-1].copy())

    cfg = _Cfg()
    assert _run(eng, cfg) is True
    w = getattr(eng, "_shot_worker_obj", None)
    assert w is not None, "成交后没有起后台截图线程"
    assert w.drain(3.0), "截图任务没写完"
    d = shotproof.settings(cfg)["dir"]
    files = sorted(os.listdir(d))
    assert any(f.endswith(".png") and "MK4" in f for f in files), files
    assert any("已保存截图" in msg for _lv, msg in eng._log)
    w.close()
    eng._shot_worker_obj = None
