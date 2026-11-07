"""`core.watchdog`：心跳年龄、连续超时确认、冷却期。

以前零覆盖。它错的方式是「误报重启」或「永不重启」：
  · 没跳过的心跳被当成卡死 → 刚启动就把一轮干掉；
  · 一次超时就触发（没有 confirm_times 防抖）→ 一次抖动就重启；
  · 冷却期不生效 → 连着重启，把已经抢到的收尾打断；
  · `age == limit` 的方向搞反 → 边界永远不触发或永远触发。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.watchdog import Heartbeat, Watchdog  # noqa: E402


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- Heartbeat

def test_age_is_none_before_the_first_beat():
    hb = Heartbeat(clock=Clock())
    assert hb.age("main") is None
    assert hb.age("worker") is None


def test_age_tracks_the_injected_clock():
    clk = Clock(100.0)
    hb = Heartbeat(clock=clk)
    hb.beat("main", at=100.0)
    clk.t = 130.0
    assert hb.age("main") == 30.0


def test_beat_uses_the_clock_when_at_is_omitted():
    clk = Clock(7.0)
    hb = Heartbeat(clock=clk)
    hb.beat()
    clk.t = 12.0
    assert hb.age() == 5.0


def test_snapshot_reports_every_named_beat():
    clk = Clock(10.0)
    hb = Heartbeat(clock=clk)
    hb.beat("a", at=8.0)
    hb.beat("b", at=10.0)
    clk.t = 11.0
    assert hb.snapshot() == {"a": 3.0, "b": 1.0}


def test_reset_forgets_all_beats():
    hb = Heartbeat(clock=Clock())
    hb.beat("main")
    hb.reset()
    assert hb.age("main") is None
    assert hb.snapshot() == {}


# ---------------------------------------------------------------- Watchdog

def _wd(limit=60.0, confirm=3, cooldown=300.0, t=0.0):
    clk = Clock(t)
    hb = Heartbeat(clock=clk)
    return Watchdog(hb, limit_s=limit, confirm_times=confirm,
                    cooldown_s=cooldown, clock=clk), hb, clk


def test_no_beats_means_no_stall():
    wd, _hb, _clk = _wd()
    assert wd.poll() is False, "还没起跳就判卡死，会把刚启动的一轮直接重启"
    assert wd.fired == 0


def test_requires_consecutive_strikes_before_firing():
    wd, hb, clk = _wd(limit=10.0, confirm=3)
    hb.beat("main", at=0.0)
    clk.t = 30.0
    assert wd.poll() is False      # 第 1 次
    assert wd.poll() is False      # 第 2 次
    assert wd.poll() is True       # 第 3 次才确认
    assert wd.fired == 1
    assert wd.info()["strikes"] == 0, "触发后应该清空计数"


def test_a_healthy_beat_resets_the_strike_counter():
    wd, hb, clk = _wd(limit=10.0, confirm=3)
    hb.beat("main", at=0.0)
    clk.t = 30.0
    assert wd.poll() is False
    assert wd.poll() is False
    # 心跳恢复 → 计数清零，再坏掉要重新累计 3 次
    hb.beat("main", at=30.0)
    assert wd.poll() is False
    assert wd.info()["strikes"] == 0
    clk.t = 60.0
    assert wd.poll() is False
    assert wd.poll() is False
    assert wd.poll() is True


def test_age_exactly_at_the_limit_counts_as_stalled():
    wd, hb, clk = _wd(limit=10.0, confirm=1)
    hb.beat("main", at=0.0)
    clk.t = 10.0
    assert wd.poll() is True, "age == limit 时判成健康，边界方向反了"


def test_cooldown_blocks_a_second_fire():
    wd, hb, clk = _wd(limit=10.0, confirm=1, cooldown=300.0)
    hb.beat("main", at=0.0)
    clk.t = 20.0
    assert wd.poll() is True
    assert wd.fired == 1

    # 冷却期内：即使仍然卡死也不许再触发
    clk.t = 21.0
    assert wd.poll() is False
    clk.t = 100.0
    assert wd.poll() is False
    assert wd.fired == 1, "冷却期内重复触发了"

    # 过了冷却期，再确认一次就该触发
    clk.t = 320.0
    assert wd.poll() is True
    assert wd.fired == 2


def test_multiple_names_use_the_worst_age():
    wd, hb, clk = _wd(limit=10.0, confirm=1)
    hb.beat("a", at=0.0)
    hb.beat("b", at=9.0)
    clk.t = 10.0
    assert wd.poll(names=("a", "b")) is True, "应该按最差的那条心跳判"

    clk.t = 11.0
    hb.beat("a", at=11.0)
    hb.beat("b", at=11.0)
    assert wd.poll(names=("a", "b")) is False


def test_any_missing_name_means_unknown_not_stalled():
    wd, hb, clk = _wd(limit=10.0, confirm=1)
    hb.beat("a", at=0.0)
    clk.t = 100.0
    assert wd.poll(names=("a", "b")) is False, "缺一条心跳就判卡死，太激进"


def test_age_override_bypasses_the_heartbeat():
    wd, _hb, _clk = _wd(limit=10.0, confirm=1)
    assert wd.poll(age_override=5.0) is False
    assert wd.poll(age_override=12.0) is True


def test_info_exposes_the_current_state():
    wd, hb, clk = _wd(limit=10.0, confirm=3)
    hb.beat("main", at=0.0)
    clk.t = 30.0
    wd.poll()
    info = wd.info()
    assert info["limit_s"] == 10.0
    assert info["strikes"] == 1
    assert info["fired"] == 0
    assert info["ages"]["main"] == 30.0


def test_confirm_times_is_at_least_one():
    wd, hb, clk = _wd(limit=10.0, confirm=0)
    assert wd.confirm_times == 1
    hb.beat("main", at=0.0)
    clk.t = 20.0
    assert wd.poll() is True
