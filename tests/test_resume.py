"""「停掉再启动能不能接着干」的两条保证 + 页面 watcher。

1. 任务状态存在 config.json 里，进程被强杀时状态会停在 running ——
   调度器只挑 pending/armed 的跑，那条任务就永远不再跑了。启动时要把它
   放回队列。
2. 界面上的抢购条件要存盘：重启后不用重填一遍。
3. 页面 watcher：画面没变就不该做 OCR（整屏 OCR 0.5~1 秒，每秒都做会
   把一个核吃光）。
"""
import json
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import scheduler as sch  # noqa: E402
from core.pagewatch import PageWatcher  # noqa: E402


class _Cfg(dict):
    def __init__(self):
        super().__init__(clock=False, trigger_mode="vanish")
        self["regions"] = {}
        self["queue"] = []

    def save(self):
        pass

    def region(self, name):
        return None

    def has_template(self, name):
        return False

    def template_path(self, name, tid=None):
        return os.path.join("no-such-dir", f"{name}.png")

    def set_region(self, name, box):
        self["regions"][name] = box


def _scheduler(monkeypatch, queue=None):
    import types
    cfg = _Cfg()
    cfg["queue"] = queue or []
    monkeypatch.setattr(sch, "Config", types.SimpleNamespace(load=lambda: cfg))
    return sch.Scheduler(), cfg


# ---------------- ① 没跑完的任务要放回队列 ----------------

def test_a_task_stuck_in_running_is_put_back(monkeypatch):
    """被强杀的任务卡在 running → 重启后它必须还能跑。"""
    stuck = {"id": "a", "name": "", "status": "running", "enabled": True,
             "quota": 1, "bought": 0, "spent": 0.0}
    s, cfg = _scheduler(monkeypatch, [stuck])
    assert cfg["queue"][0]["status"] == "pending"
    assert "没跑完" in cfg["queue"][0]["note"]


def test_a_finished_task_is_left_alone(monkeypatch):
    """已经跑完的任务不能被放回队列 —— 否则每次重启都重抢一遍。"""
    done = {"id": "a", "name": "", "status": "done", "enabled": True,
            "quota": 1, "bought": 1, "spent": 2.0}
    s, cfg = _scheduler(monkeypatch, [done])
    assert cfg["queue"][0]["status"] == "done"


def test_a_disabled_task_stays_disabled(monkeypatch):
    """停用的任务保持停用 —— 恢复队列不该把用户手动停掉的任务放出来。"""
    off = {"id": "a", "name": "", "status": "running", "enabled": False,
           "quota": 1, "bought": 0, "spent": 0.0}
    s, cfg = _scheduler(monkeypatch, [off])
    assert cfg["queue"][0]["status"] == "pending"
    assert cfg["queue"][0]["enabled"] is False


# ---------------- ② 界面条件要存盘 ----------------

def test_console_conditions_round_trip(tmp_path, monkeypatch):
    import core.config as cfgmod
    path = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))

    cfg = cfgmod.Config.load()
    assert cfg["console"]["max_price"] == 0

    cfg["console"] = {"min_price": 100, "max_price": 3000, "quota": 2, "budget": 500}
    cfg.save()

    again = cfgmod.Config.load()
    assert again["console"] == {"min_price": 100, "max_price": 3000,
                                "quota": 2, "budget": 500}


# ---------------- ③ 页面 watcher：画面不变就不做 OCR ----------------

class _FakeNav:
    def __init__(self):
        self.frame = np.zeros((40, 60, 3), np.uint8)
        self.reads = 0

    def _snap(self):
        return self.frame

    def where(self):
        self.reads += 1
        return "onsale", "在售"


def test_watcher_only_reads_when_the_screen_changed():
    nav = _FakeNav()
    w = PageWatcher(interval=0.05, nav_factory=lambda: nav)

    w._refresh_once()
    assert nav.reads == 1, "第一次总要读一次"
    w._refresh_once()
    assert nav.reads == 1, "画面没动就不该再读（整屏 OCR 很贵）"

    nav.frame[:] = 200                      # 画面变了
    w._refresh_once()
    assert nav.reads == 2
    assert w.snapshot()["page"] == "onsale"


def test_watcher_reports_the_page():
    nav = _FakeNav()
    w = PageWatcher(interval=0.05, nav_factory=lambda: nav)
    w.start()
    deadline = time.time() + 3
    while time.time() < deadline and w.snapshot()["page"] != "onsale":
        time.sleep(0.05)
    w.stop()
    assert w.snapshot()["page"] == "onsale"
