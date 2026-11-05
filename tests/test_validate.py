"""条件数值校验 + 稀有度枚举：前后端同一条口径，后端独立兜底。

这里只测纯函数和调度器入队，不碰真机、不启窗口。
"""
import os
import sys
import types
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import scheduler as sch  # noqa: E402
from core.validate import CRAFT_OPTIONS, check_conditions  # noqa: E402


def test_valid_conditions_are_clean():
    clean, errs = check_conditions({"min_price": 100, "max_price": 3000,
                                    "quota": 2, "budget": 6000,
                                    "max_wear": 0.35, "craft": "极品"})
    assert errs == []
    assert clean == {"min_price": 100, "max_price": 3000, "quota": 2,
                     "budget": 6000, "max_wear": 0.35}


def test_infinity_is_rejected_not_silently_zeroed():
    """`1e309` → Infinity → JSON 里变 null → 引擎按 0（不限价），必须拦。"""
    for bad in ("1e309", float("inf"), "-1e309", float("-inf"), "NaN"):
        clean, errs = check_conditions({"max_price": bad})
        assert errs, f"{bad!r} 应该被拒"
        assert "有限数字" in errs[0]


def test_fractional_is_rejected_for_integer_fields():
    _, errs = check_conditions({"min_price": 2.5})
    assert errs and "整数" in errs[0]


def test_negative_is_rejected():
    for key in ("min_price", "max_price", "budget", "max_wear"):
        _, errs = check_conditions({key: -1})
        assert errs and "负数" in errs[0], (key, errs)


def test_quota_must_be_at_least_one():
    _, errs = check_conditions({"quota": 0})
    assert errs and "≥ 1" in errs[0]


def test_min_must_not_exceed_max():
    _, errs = check_conditions({"min_price": 500, "max_price": 100})
    assert errs and "最低价不能高于最高价" in errs[0]


def test_craft_enum_matches_the_backend_contract():
    """抢购页的「品阶」就是后端的 craft：只认 极品 / 优品 / 不限。

    以前前端往 craft 里塞 传说/史诗/稀有/普通，整行被后端拒掉。
    """
    assert CRAFT_OPTIONS == ("", "极品", "优品")
    for bad in ("传说", "史诗", "稀有", "普通"):
        _, errs = check_conditions({"craft": bad})
        assert errs and "稀有度" in errs[0], (bad, errs)


# ---------------------------------------------------------------- 入队兜底

class _Cfg(dict):
    def __init__(self):
        super().__init__({
            "queue": [],
            "regions": {"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                        "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1}},
        })

    def save(self):
        pass

    def template_path(self, name, tid=None):
        return os.path.join("no-such-dir", f"{name}.png")


def _scheduler(monkeypatch):
    cfg = _Cfg()

    @contextlib.contextmanager
    def transaction():
        yield cfg
        cfg.save()

    monkeypatch.setattr(sch, "Config",
                        types.SimpleNamespace(load=lambda: cfg,
                                              transaction=transaction))
    return sch.Scheduler()


def test_scheduler_rejects_legendary_tier_the_same_way(monkeypatch):
    s = _scheduler(monkeypatch)
    t, err = s.add("A", mode="list", max_price=3000, craft="传说")
    assert t is None, "后端也要拦，不能只靠前端"
    assert "稀有度" in (err or ""), err
    t, err = s.add("A", mode="list", max_price=3000, craft="优品")
    assert err is None, err
    assert t["craft"] == "优品"


def test_scheduler_rejects_infinity_price(monkeypatch):
    s = _scheduler(monkeypatch)
    t, err = s.add("A", mode="list", max_price="1e309")
    assert t is None, f"Infinity 被存进去了：{t}"
    assert "有限数字" in (err or ""), err


def test_same_name_and_workflow_updates_in_place(monkeypatch):
    """同名 + 同路线是「改条件」，不是「又加一条」——换目标不该留旧任务。"""
    s = _scheduler(monkeypatch)
    t1, err = s.add("AKM", mode="list", max_price=1000)
    assert err is None, err
    t2, err = s.add("AKM", mode="list", max_price=3000)
    assert err is None, err
    assert t2["id"] == t1["id"], "同名任务应当是 update，不是新增"
    assert len(s.tasks()) == 1, "队列里留了旧任务"
    assert s.tasks()[0]["max_price"] == 3000
    assert s.tasks()[0]["status"] == sch.PENDING


def test_update_does_not_revive_a_done_task_silently(monkeypatch):
    """update 只改字段；原状态是 done 时不能被顺手复活成 pending。

    重跑必须由用户显式传 status=pending（前端会先弹二次确认）。
    """
    s = _scheduler(monkeypatch)
    t, err = s.add("AKM", mode="list", max_price=1000)
    assert err is None, err
    s.update(t["id"], status=sch.DONE)
    t2, err = s.add("AKM", mode="list", max_price=2000)
    assert err is None, err
    assert t2["id"] == t["id"]
    assert s.tasks()[0]["status"] == sch.DONE, "update 把已完成的任务复活了"
