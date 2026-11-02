"""队列的「看得见的进度」：启用开关、顺序、已抢几件。

参考竞品的任务清单：每行有 限量 / 已抢 / 状态，还有启用停用和拖拽排序。
我们原来这三样都没有 —— 队列只是一个列表，跑到第几条、抢到几件全靠翻日志。
"""
import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import scheduler as sch  # noqa: E402


class _Cfg(dict):
    def __init__(self):
        super().__init__({
            "queue": [],
            "regions": {"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                        "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1},
                        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.1}},
        })

    def save(self):
        pass

    def template_path(self, name, tid=None):
        return os.path.join("no-such-dir", f"{name}.png")

    def set_region(self, name, box):
        self["regions"][name] = box


def _scheduler(monkeypatch):
    # 同一个内存配置对象要一直用 —— 每次 load() 都给新对象的话队列根本存不住，
    # 加进去的任务下一行代码就没了（这个坑先踩过一次）
    import contextlib
    cfg = _Cfg()

    @contextlib.contextmanager
    def transaction():
        yield cfg
        cfg.save()

    monkeypatch.setattr(sch, "Config",
                        types.SimpleNamespace(load=lambda: cfg,
                                              transaction=transaction))
    s = sch.Scheduler()
    s._stop = threading.Event()
    return s


# ---------------- 引擎拿到的条件 ----------------

def test_engine_task_carries_the_quota_and_the_budget():
    """限量/预算要真的传给引擎 —— 之前这两个字段加进了队列却没传下去，
    界面上设了等于没设（最典型的半截接线）。"""
    t = {"name": "AKM", "min_price": 1000, "max_price": 3000,
         "quota": 3, "budget": 9000}
    got = sch.Scheduler._engine_task(t)
    assert got["quota"] == 3, "限量没传给引擎"
    assert got["budget"] == 9000, "预算没传给引擎"
    assert got["max_price"] == 3000 and got["name"] == "AKM"


def test_engine_task_defaults_are_safe_for_old_tasks():
    """老任务没有这两个字段：默认抢 1 件、不封预算，不能变成 0 件。"""
    got = sch.Scheduler._engine_task({"name": "老任务"})
    assert got["quota"] == 1
    assert got["budget"] == 0


# ---------------- 启用 / 停用 ----------------

def test_new_tasks_start_enabled_with_zero_progress(monkeypatch):
    s = _scheduler(monkeypatch)
    t, _ = s.add("甲", mode="list", max_price=3000)
    assert t["enabled"] is True
    assert t["bought"] == 0 and t["spent"] == 0.0


def test_disabled_task_is_not_run(monkeypatch):
    """停用的任务留在队列里但不跑 —— 改条件时先停掉，不用删。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    s.add("乙", mode="list", max_price=3000)
    s.update(s.tasks()[0]["id"], enabled=False)

    ran = []
    monkeypatch.setattr(s, "_run_task",
                        lambda t, d: (ran.append(t["name"]),
                                      s.update(t["id"], status="done")))
    s._run(dry_run=True)
    assert ran == ["乙"], f"停用的也被跑了：{ran}"


# ---------------- 已抢 / 已花 ----------------

class _FakeEngine:
    def __init__(self, bought=1, spent=2050.0):
        self._st = {"state": "done", "stats": {"clicks": 2, "bought": bought,
                                               "spent": spent}, "error": None}
        self.started = []

    def start(self, **kw):
        self.started.append(kw)
        return True, ""

    def status(self):
        return self._st

    def stop(self):
        pass


def test_bought_and_spent_accumulate_on_the_task(monkeypatch):
    """抢到几件要记在任务上，列表才显示得出「已抢 1 / 2」。"""
    s = _scheduler(monkeypatch)
    t, _ = s.add("甲", mode="list", max_price=3000, quota=2)
    monkeypatch.setattr(sch.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(sch.shutil, "copyfile", lambda a, b: None)
    monkeypatch.setattr(s, "_engine", _FakeEngine(bought=1, spent=2050.0))

    s._run_task(s.tasks()[0], dry_run=True)
    after = s.tasks()[0]
    assert after["bought"] == 1, after
    assert after["spent"] == 2050.0
    assert after["status"] == "done"


def test_progress_accumulates_across_runs(monkeypatch):
    """跑第二次要在第一次的基础上加，不是覆盖。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000, quota=2)
    monkeypatch.setattr(sch.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(sch.shutil, "copyfile", lambda a, b: None)
    monkeypatch.setattr(s, "_engine", _FakeEngine(bought=1, spent=2000.0))

    s._run_task(s.tasks()[0], dry_run=True)
    s._run_task(s.tasks()[0], dry_run=True)
    after = s.tasks()[0]
    assert after["bought"] == 2, after
    assert after["spent"] == 4000.0


class _StuckEngine(_FakeEngine):
    """引擎线程已经结束，状态却停在中间（fire）。

    以前调度器只认 done/abort，遇到这种就永远转下去 —— 队列卡死，
    后面的任务再也排不上。
    """

    def status(self):
        return {"state": "fire", "running": False,
                "stats": {"clicks": 9, "bought": 0, "spent": 0.0},
                "error": None}


def test_a_stuck_engine_does_not_block_the_queue(monkeypatch):
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    monkeypatch.setattr(sch.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(sch.shutil, "copyfile", lambda a, b: None)
    monkeypatch.setattr(s, "_engine", _StuckEngine())

    s._run_task(s.tasks()[0], dry_run=True)      # 不能一直转下去
    assert s.tasks()[0]["status"] == "failed"
