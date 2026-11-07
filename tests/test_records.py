"""成交记录：一行一次尝试，抢到/没抢到都要记。

钉住三件事：
  1. 命中率的分母是「尝试次数」，不是成功次数（只报成功会让人以为每次都成）
  2. 写到一半被杀的坏行不能让整个文件读不出来
  3. 引擎真跑成功分支时会落一条 hit，没等到弹窗落一条 miss
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod
from core import records


@pytest.fixture
def rec(tmp_path, monkeypatch):
    """把记录文件指到临时目录，绝不碰用户真实的 data/records.jsonl。"""
    monkeypatch.setattr(records, "PATH", str(tmp_path / "records.jsonl"))
    return records


def test_add_then_load_newest_first(rec):
    rec.add(name="MK4", price=2050, result="hit", elapsed=1.2)
    rec.add(name="AKM", price=880, result="miss", elapsed=0.9)
    rows = rec.load()
    assert [r["name"] for r in rows] == ["AKM", "MK4"], "读出来不是新的在前"
    assert rows[1]["price"] == 2050.0


def test_stats_counts_every_attempt_as_the_denominator(rec):
    rec.add(name="A", price=100, result="hit", elapsed=1.0)
    rec.add(name="B", result="miss", elapsed=2.0)
    rec.add(name="C", price=300, result="hit", elapsed=3.0)
    st = rec.stats()
    assert st["hits"] == 2 and st["tries"] == 3
    assert st["spent"] == 400.0
    assert st["rate"] == pytest.approx(66.7), "命中率不是 2/3"
    assert st["median_s"] == pytest.approx(2.0)


def test_broken_line_is_skipped_not_fatal(rec):
    rec.add(name="好行", price=10, result="hit")
    with open(rec.PATH, "a", encoding="utf-8") as f:
        f.write('{"name": "写了一半')      # 进程被强杀留下的半行
    rows = rec.load()
    assert len(rows) == 1 and rows[0]["name"] == "好行"


def test_unknown_result_falls_back_to_hit(rec):
    row = rec.add(name="X", result="奇怪的码")
    assert row["result"] == "hit", "陌生结果码不能把记录写坏"


def test_clear_empties_but_keeps_file(rec):
    rec.add(name="A", price=1, result="hit")
    assert rec.clear() is True
    assert rec.load() == []
    assert os.path.isfile(rec.PATH)


def test_missing_file_reads_as_empty(rec):
    assert rec.load() == []
    assert rec.stats()["tries"] == 0 and rec.stats()["rate"] == 0.0


# ---------------------------------------------------------------- 引擎接线

class FakeScreen:
    def __init__(self):
        self._frame = np.zeros((100, 100, 3), np.uint8)

    def grab(self, box=None):
        return self._frame

    def norm_to_px(self, box):
        return (0, 0, 100, 100)

    def size(self):
        return 1920, 1080


class FakeMouse:
    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, expect_foreground=None, **kw):
        pass


class _Cfg(dict):
    def __init__(self, **kw):
        super().__init__({
            "threshold": 0.8, "click_hz": 200, "fire_timeout_s": 0.3,
            "confirm_clicks": 1, "confirm_interval_ms": 0, "failsafe": False,
            "save_shots": False,
            "active_task": {"name": "MK4 疾光魅影", "mode": "list", "quota": 2},
            "regions": {"buy": {"x": .5, "y": .5, "w": .1, "h": .05},
                        "confirm": {"x": .5, "y": .5, "w": .1, "h": .05}},
        })
        self.update(kw)

    def region(self, name):
        return self["regions"][name]


@pytest.fixture
def eng(rec, monkeypatch):
    e = engine_mod.Engine()
    e._target_hwnd = 1
    e._log = []
    e._say = lambda msg, level="info": e._log.append((level, msg))
    e._guard_insufficient = lambda screen, mouse, tries=3, gap=0.35: None
    e._insufficient_showing = lambda screen: False
    return e


def test_success_writes_a_hit_record(eng, rec, monkeypatch):
    monkeypatch.setattr(engine_mod, "match", lambda img, tpl, *a, **k: (0.99, (0, 0)))
    # 成交校验现在要看到结算弹窗（2026-11-02 起）。这条用例测的是「记账」，
    # 不是「认弹窗」—— 弹窗识别有它自己的用例，这里直接给个「看到了」。
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    # 成交校验现在要看到结算弹窗（2026-11-02 起）。这条用例测的是「记账」，
    # 不是「认弹窗」—— 弹窗识别有它自己的用例，这里直接给个「看到了」。
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    eng._fire_and_confirm(_Cfg(), FakeScreen(), FakeMouse(), np.zeros((10, 10), np.uint8),
                          (500, 500), __import__("time").perf_counter(),
                          item="AKM 群星", price=1888)
    rows = rec.load()
    assert len(rows) == 1
    assert rows[0]["result"] == "hit"
    assert rows[0]["name"] == "AKM 群星", "记的不是实际挑中的那件"
    assert rows[0]["price"] == 1888.0
    assert rows[0]["quota"] == 2


def test_no_popup_writes_a_miss_record(eng, rec, monkeypatch):
    monkeypatch.setattr(engine_mod, "match", lambda img, tpl, *a, **k: (0.1, (0, 0)))
    ok = eng._fire_and_confirm(_Cfg(), FakeScreen(), FakeMouse(), np.zeros((10, 10), np.uint8),
                               (500, 500), __import__("time").perf_counter(),
                               item="AKM 群星", price=1888)
    assert ok is False
    rows = rec.load()
    assert len(rows) == 1 and rows[0]["result"] == "miss"
    assert rows[0]["price"] == 1888.0, "没抢到也要留下当时的价格，事后才知道贵在哪"


def test_confirm_clicks_without_reward_dialog_is_a_miss(eng, rec, monkeypatch):
    """点完确认没看到结算弹窗 → 记 miss，不许记 hit。

    这是 2026-11-02 修的那个洞：主路径点完确认就无条件记 hit，data/ 里
    堆了 78 条「成交」，配的截图全是交易行列表 —— 一条真的都没有。
    别人先买走、余额不足、弹窗没响应，都会表现为「点了但什么都没发生」。
    """
    monkeypatch.setattr(engine_mod, "match", lambda img, tpl, *a, **k: (0.99, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: False)
    monkeypatch.setattr(eng, "_balance", lambda screen, img=None: None)
    ok = eng._fire_and_confirm(_Cfg(), FakeScreen(), FakeMouse(),
                               np.zeros((10, 10), np.uint8), (500, 500),
                               __import__("time").perf_counter(),
                               item="AKM 群星", price=1888)
    assert ok is False, "没看到结算弹窗却报了成交"
    rows = rec.load()
    assert len(rows) == 1 and rows[0]["result"] == "miss"
    assert rows[0]["verified"] is True, "要能看出这次是校验过的"
