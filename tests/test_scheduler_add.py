"""加任务时把「本轮限量 / 预算上限」存进队列。

这两个字段是钱的事：限量少了白抢、预算没存等于不封顶。之前队列里只有
价格区间和成色，抢到一件就收工、花多少不封顶 —— 和竞品（Axon·skin）
一比就看得出来差在这。

这里只测「存得对」，不碰真机、不写真配置：Config 换成内存假对象。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import scheduler as sch  # noqa: E402


class _Cfg(dict):
    """内存版配置：不落盘，模板路径给一个不存在的文件（跳过复制）。"""

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


def _scheduler(monkeypatch):
    # 同一份内存配置：调度器每次 load() 拿到的必须是同一个对象，
    # 否则加进去的任务下一次读就没了（真 Config 读的是同一个文件）
    import contextlib
    cfg = _Cfg()

    @contextlib.contextmanager
    def transaction():
        yield cfg
        cfg.save()

    monkeypatch.setattr(sch, "Config",
                        types.SimpleNamespace(load=lambda: cfg,
                                              transaction=transaction))
    return sch.Scheduler()


def test_add_keeps_the_quota_and_the_budget(monkeypatch):
    s = _scheduler(monkeypatch)
    task, err = s.add("AKM 突击步枪·群星", mode="list",
                      min_price=1000, max_price=3000, quota=3, budget=9000)
    assert err is None, err
    assert task["quota"] == 3, f"限量没存住：{task['quota']}"
    assert task["budget"] == 9000.0, f"预算没存住：{task['budget']}"


def test_quota_defaults_to_one_and_invalid_is_rejected(monkeypatch):
    """不填就是抢一件；0 / 负数 / 小数 / Infinity 一律拒绝，绝不静默折算。"""
    s = _scheduler(monkeypatch)
    t1, err = s.add("A", mode="list", max_price=3000)
    assert err is None, err
    assert t1["quota"] == 1
    for i, bad in enumerate((0, -5, 2.5, "abc", "1e309")):
        t, err = s.add(f"B{i}", mode="list", max_price=3000, quota=bad)
        assert t is None, f"quota={bad!r} 应该被拒，实际存成了 {t}"
        assert "数量" in (err or ""), f"要说清是数量不合法：{err}"


def test_budget_zero_means_no_cap_and_negative_is_rejected(monkeypatch):
    s = _scheduler(monkeypatch)
    t, err = s.add("A", mode="list", max_price=3000, budget=0)
    assert err is None, err
    assert t["budget"] == 0.0
    t2, err = s.add("B", mode="list", max_price=3000, budget=-100)
    assert t2 is None, "负预算应该被拒"
    assert "预算" in (err or "") and "负数" in (err or ""), err

