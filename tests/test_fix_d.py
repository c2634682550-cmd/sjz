"""engine.py / desktop.py 第三轮审查修复的离线回归测试。

全部用假对象：不建窗口、不抓屏、不注入输入、不联网、不读真实配置。
"""
import threading
import time

import numpy as np
import pytest

from core import engine as engine_mod
from core.engine import (ABORT, DONE, Engine, Aborted, choose_countdown_target)


class _Screen:
    size = (1920, 1080)

    def __init__(self):
        self.closed = False

    def norm_to_px(self, region):
        return (0, 0, 200, 120)

    def grab(self, px=None):
        return np.zeros((60, 200, 3), np.uint8)

    def close(self):
        self.closed = True


class _Mouse:
    def __init__(self):
        self.clicks = []
        self.foreground_calls = 0

    def ensure_foreground(self, hwnd, screen=None):
        self.foreground_calls += 1
        return True

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))


class _Cfg(dict):
    """只实现引擎真正用到的读接口。"""

    def __init__(self, regions=None, templates=(), **kw):
        base = {
            "monitor": 1,
            "threshold": 0.85,
            "failsafe": False,
            "trigger_mode": "vanish",
            "workflow": "list",
            "input_mode": "legacy",
        }
        base.update(kw)
        super().__init__(base)
        self._regions = dict(regions or {})
        self._templates = set(templates)

    def region(self, name):
        return self._regions.get(name)

    def has_template(self, name):
        return name in self._templates

    def set_region(self, name, box):
        self._regions[name] = box


_BOX = lambda x, y, w, h: {"x": x, "y": y, "w": w, "h": h}   # noqa: E731

_LIST_CFG = dict(regions={"list": _BOX(0.1, 0.1, 0.5, 0.5),
                          "buy": _BOX(0.5, 0.6, 0.1, 0.05)},
                 templates={"buy"})


# ---------------- 一次性状态每轮清零 ----------------

def test_start_clears_one_shot_flags(monkeypatch):
    """价格/排序筛选的「已尝试」不许跨任务残留（S2）。"""
    cfg = _Cfg(**_LIST_CFG)
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(engine_mod, "find_game_window", lambda: None)
    monkeypatch.setattr(engine_mod.Mouse, "foreground", staticmethod(lambda: None))
    monkeypatch.setattr(Engine, "_run", lambda self, *a: None)
    e = Engine()
    e._price_filter_tried = True
    e._sort_tried = True
    ok, msg = e.start(dry_run=True, mode="list", workflow="list")
    assert ok, msg
    assert e._price_filter_tried is False
    assert e._sort_tried is False
    assert e._verified_by == ""


def test_start_refuses_without_game_window(monkeypatch):
    """实战找不到游戏窗口就拒绝启动，不拿控制台当前台盲点（I9）。"""
    cfg = _Cfg(**_LIST_CFG)
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(engine_mod, "find_game_window", lambda: None)
    e = Engine()
    ok, msg = e.start(dry_run=False, mode="list", workflow="list")
    assert ok is False
    assert "游戏窗口" in msg


# ---------------- 资源构造在 try 内 ----------------

def test_screen_construction_failure_becomes_engine_error(monkeypatch):
    """Screen 构造失败必须写成线程级 error（调度器记「异常失败」而不是「未抢到」）。"""
    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("没有可用显示器")

    monkeypatch.setattr(engine_mod, "Screen", _Boom)
    e = Engine()
    e._dry_run = False
    e._run(_Cfg(**_LIST_CFG), mode="list", workflow="list")
    assert "OSError" in (e._error or "")
    assert e.status()["state"] == ABORT


# ---------------- dry_run 不注入输入 ----------------

def test_dry_run_buy_without_dialog_does_not_click(monkeypatch):
    """没有确认弹窗的版式在演练里也不能点（点了就是真花钱，D4/P0-5）。"""
    cfg = _Cfg()
    monkeypatch.setattr(engine_mod.Config, "load", staticmethod(lambda: cfg))
    e = Engine()
    e._dry_run = True
    mouse = _Mouse()
    assert e._buy_without_dialog(_Screen(), mouse, (10, 20),
                                 time.perf_counter()) is True
    assert mouse.clicks == []


def test_ons_ready_dry_run_does_not_touch_game(monkeypatch):
    """演练模式下 _ons_ready 不切前台、不点筛选（S3）。"""
    built = []

    class _Nav:
        def pub_filter(self, value):
            built.append(("pub_filter", value))

        def ons_refresh(self):
            built.append(("ons_refresh",))

    monkeypatch.setattr(engine_mod, "Navigator", lambda *a, **k: _Nav())
    e = Engine()
    e._dry_run = True
    mouse = _Mouse()
    e._ons_ready(_Cfg(**_LIST_CFG), _Screen(), mouse)
    assert mouse.foreground_calls == 0
    assert built == []


# ---------------- 确认模板的统一口径 ----------------

def test_validate_list_mode_allows_missing_confirm():
    """list 模式是无弹窗列表版式，不要求确认模板/区域。"""
    cfg = _Cfg(**_LIST_CFG)
    assert Engine._validate(cfg, "list", "list") is None


def test_validate_detail_mode_still_requires_confirm():
    cfg = _Cfg(regions={"buy": _BOX(0.5, 0.6, 0.1, 0.05)}, templates={"buy"})
    problem = Engine._validate(cfg, "detail", "detail")
    assert problem and "确认" in problem


# ---------------- 空 serial 去重 ----------------

def test_tried_mark_uses_position_when_serial_is_empty():
    assert Engine._tried_mark({"serial": "", "x": 10, "y": 20}) == ("pos", 10, 20)
    assert Engine._tried_mark({"serial": "A", "x": 10, "y": 20}) == "A"


def test_choose_target_skips_tried_position():
    cands = [{"serial": None, "sec": 3, "kind": "unlock", "x": 10, "y": 20},
             {"serial": None, "sec": 9, "kind": "unlock", "x": 30, "y": 40}]
    assert choose_countdown_target(cands, [("pos", 10, 20)])["x"] == 30
    assert choose_countdown_target(
        cands, [("pos", 10, 20), ("pos", 30, 40)]) is None


def test_countdown_marks_position_after_missing_serial(monkeypatch):
    """编号读不出时用坐标记进 tried，下一轮不再挑同一张卡（S18）。"""
    passed = []

    def _prepare(self, cfg, screen, mouse, tried=(), refresh=False):
        passed.append(list(tried))
        if not passed[0] or len(passed) == 1:
            return {"serial": "", "sec": 4, "text": "x", "x": 10, "y": 20,
                    "price": 100}
        return {"serial": "", "sec": 4, "text": "y", "x": 30, "y": 40,
                "price": 100}

    monkeypatch.setattr(Engine, "_countdown_prepare", _prepare)

    def _gone(self, cfg, screen, already_unlocked=False):
        raise Aborted("这件没了")

    monkeypatch.setattr(Engine, "_watch_countdown", _gone)
    cfg = _Cfg(regions={"buy": _BOX(0.5, 0.6, 0.1, 0.05)},
               countdown_attempts=2)
    e = Engine()
    e._dry_run = False
    with pytest.raises(Aborted):
        e._execute_countdown(cfg, _Screen(), _Mouse(), None)
    assert passed == [[], [("pos", 10, 20)]]


# ---------------- 倒计时成交也累加 bought ----------------

def test_countdown_success_increments_bought(monkeypatch):
    monkeypatch.setattr(Engine, "_countdown_prepare",
                        lambda self, cfg, s, m, tried=(), refresh=False:
                        {"serial": "", "sec": 0, "text": "x", "x": 10, "y": 20,
                         "price": 100})
    monkeypatch.setattr(Engine, "_watch_countdown", lambda *a, **k: None)
    monkeypatch.setattr(Engine, "_fire_and_confirm", lambda *a, **k: True)
    cfg = _Cfg(regions={"buy": _BOX(0.5, 0.6, 0.1, 0.05)}, countdown_attempts=1)
    e = Engine()
    e._dry_run = False
    e._execute_countdown(cfg, _Screen(), _Mouse(), None)
    assert e._stats["bought"] == 1
    assert e.status()["state"] == DONE


# ---------------- 弹窗提前消失 ----------------

def _run_unlock(e, monkeypatch, match_score):
    """让 _wait_dialog_unlock 走到「文本提前消失」分支，返回它的结果。"""
    seq = iter([3, None])

    monkeypatch.setattr(engine_mod.countdown, "parse_words",
                        lambda words: next(seq, None))

    class _Clock:
        def observe(self, sec, now):
            pass

        def due(self, now, margin):
            return False

        def zero_at(self):
            return time.perf_counter() + 2.0      # 离零点还有 2 秒

    monkeypatch.setattr(engine_mod.countdown, "Clock", _Clock)
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (match_score, None))
    return e._wait_dialog_unlock(_Screen(), timeout=5.0,
                                 confirm_tpl=np.zeros((8, 8), np.uint8),
                                 confirm_px=(0, 0, 20, 20), threshold=0.85)


def test_wait_dialog_unlock_reports_dialog_gone(monkeypatch):
    """文本提前消失且确认按钮也没了 → 返回 "gone"，不许调用方盲点（S13）。"""
    e = Engine()
    assert _run_unlock(e, monkeypatch, 0.2) == "gone"


def test_wait_dialog_unlock_true_when_confirm_button_still_there(monkeypatch):
    e = Engine()
    assert _run_unlock(e, monkeypatch, 0.95) is True


# ---------------- 价格筛选：失败可重试 ----------------

def test_price_filter_flag_stays_false_when_filter_not_applied(monkeypatch):
    """筛选没设进去就不能标「已尝试」，空区间也不许把整任务炸掉（S2/S10）。"""
    class _Nav:
        def where(self):
            return ("onsale", 0.9)

        def pub_filter(self, value):
            pass

        def ons_page(self):
            return 1

        def ons_prev_page(self):
            pass

        def ons_filter(self):
            pass

        def ons_price(self, lo, hi):
            raise Aborted("价格区间是空集")

        def ons_refresh(self):
            pass

    monkeypatch.setattr(engine_mod, "Navigator", lambda *a, **k: _Nav())
    monkeypatch.setattr(engine_mod.market, "scan", lambda frame: [])
    cfg = _Cfg(regions={"clock": _BOX(0.1, 0.1, 0.2, 0.1),
                        "list": _BOX(0.1, 0.2, 0.5, 0.5)},
               active_task={"min_price": 5000, "max_price": 1000})
    e = Engine()
    e._dry_run = False
    assert e._countdown_prepare(cfg, _Screen(), _Mouse(), refresh=False) is None
    assert e._price_filter_tried is False


# ---------------- 列表模式导航只走一遍 ----------------

def test_ons_ready_reuses_prepared_nav(monkeypatch):
    """_navigate 已经把价格筛选做完了，_ons_ready 不许再建 Navigator 重做（S3）。"""
    calls = []

    class _Nav:
        def pub_filter(self, value):
            calls.append(("pub_filter", value))

        def ons_refresh(self):
            calls.append(("ons_refresh",))

    def _boom(*a, **k):
        raise AssertionError("不该再建 Navigator（导航跑了两遍）")

    monkeypatch.setattr(engine_mod, "Navigator", _boom)
    e = Engine()
    e._dry_run = False
    mouse = _Mouse()
    e._ons_ready(_Cfg(**_LIST_CFG), _Screen(), mouse, _Nav())
    assert calls == [("pub_filter", "不显示公示期"), ("ons_refresh",)]
    assert mouse.foreground_calls == 0


# ---------------- 记录里的 mode / verified ----------------

def test_record_writes_mode_and_verified_by(monkeypatch):
    got = {}
    monkeypatch.setattr(engine_mod.records, "add",
                        lambda **kw: got.update(kw))
    monkeypatch.setattr(engine_mod.switches, "get",
                        lambda cfg, key: True)
    cfg = _Cfg(active_task={"name": "任务A"})
    e = Engine()
    e._mode = "list"
    e._verified_by = "balance"
    e._save_shot = lambda *a, **k: None
    e._record(cfg, _Screen(), item="AK", price=100, result="hit")
    assert got["mode"] == "list"
    assert got["verified"] is True
    assert got["extra"]["verified_by"] == "balance"


# ---------------- stop() 不在自己线程里 join ----------------

def test_stop_from_engine_thread_does_not_join_itself():
    e = Engine()
    e._state = engine_mod.WATCH
    done = []

    def _body():
        e._thread = threading.current_thread()
        ok, _ = e.stop()
        done.append(ok)

    t = threading.Thread(target=_body, daemon=True)
    t.start()
    t.join(2.0)
    assert not t.is_alive()
    assert done == [True]
    assert e.status()["state"] == ABORT
