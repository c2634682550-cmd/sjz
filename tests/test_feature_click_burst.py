"""连点参数（click.burst.*）的 D1–D5 验收用例。

全程离线：不构造真实 Mouse，不打开窗口，不移动光标；时间断言全部走
注入的假时钟，不真 sleep。

语义：每次按下按住 `hold_ms`，抬起后隔 `interval_ms` 再按下；`window`
决定连点阶段多长，窗口内排不下一次完整按住就停手，不压缩 hold。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from core import click_burst, config, switches

ROOT_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 工具

class FakeClock:
    """假时钟：sleep 只推进虚拟时间，并记账。"""

    def __init__(self, start=100.0):
        self.t = start
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, sec):
        self.sleeps.append(sec)
        self.t += sec


def _cfg(**over):
    d = json.loads(json.dumps(config.DEFAULTS))
    d["regions"] = {
        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
        "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1},
    }
    d.update(over)
    return d


def _plan(**burst):
    p = dict(click_burst.DEFAULTS)
    p.update(burst)
    return p


def _runner(clk, hold_arg=None):
    """按真实物理形态点击：按下真的按住 hold_s，抬起后再算间隔。"""
    starts = []

    def click(hold_s):
        starts.append(clk.now())
        if hold_arg is not None:
            hold_arg.append(hold_s)
        clk.sleep(hold_s)

    return starts, click


# ---------------------------------------------------------------- D1 功能可用

def test_hold_100_interval_5_window_1s_shape():
    """hold=100 / interval=5 / window=1s：窗口内 9 次，每次按住 100ms。

    物理上自洽：1000ms 窗口 = 9 × (100 + 5) 次排得下，第 10 次只剩 55ms
    不够按住 100ms，所以停手 —— 不为了凑次数压缩 hold。
    """
    clk = FakeClock()
    starts, click = _runner(clk)
    rep = click_burst.run(
        _plan(mode="burst", hold_ms=100, interval_ms=5, window="1s"),
        t_fire=clk.now(), fire_timeout=10.0,
        click=click, dialog=lambda: False, now=clk.now, sleep=clk.sleep)

    assert rep["clicks"] == 9, rep
    assert rep["stopped_by"] == "window"
    # 每次按住时长都在容差内（假时钟精确到微秒）
    assert len(rep["holds_ms"]) == 9
    assert all(abs(h - 100.0) <= 1.0 for h in rec_holds(rep)), rep["holds_ms"]
    # 相邻两次按下的起点间距 = hold + interval = 105ms
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(abs(g * 1000.0 - 105.0) <= 2.0 for g in gaps), gaps
    assert rep["stage_ms"] == 1000.0
    assert rep["elapsed_ms"] <= 1004.5


def rec_holds(rep):
    return rep["holds_ms"]


def test_burst_stops_immediately_when_dialog_appears():
    """弹窗出现后零点击：第三下之后弹窗出现，第四下绝不能发。"""
    clk = FakeClock()
    starts = []

    def click(hold_s):
        starts.append(clk.now())
        clk.sleep(hold_s)

    def dialog():
        return len(starts) >= 3

    rep = click_burst.run(
        _plan(mode="burst", hold_ms=20, interval_ms=5, window="all"),
        t_fire=clk.now(), fire_timeout=10.0,
        click=click, dialog=dialog, now=clk.now, sleep=clk.sleep)
    assert rep["stopped_by"] == "dialog"
    assert len(starts) == 3, f"弹窗后还多点：{len(starts)}"


@pytest.mark.parametrize("window,stage_ms", [
    ("all", 500.0),
    ("1s", 1000.0),
    ("2s", 2000.0),
])
def test_window_three_tiers_duration(window, stage_ms):
    """window=全部 / 1 秒 / 2 秒：阶段时长按窗口走（全部时按开火超时）。"""
    clk = FakeClock()
    starts, click = _runner(clk)
    rep = click_burst.run(
        _plan(mode="burst", hold_ms=20, interval_ms=5, window=window),
        t_fire=clk.now(), fire_timeout=0.5 if window == "all" else 10.0,
        click=click, dialog=lambda: False, now=clk.now, sleep=clk.sleep)

    assert rep["stage_ms"] == stage_ms
    assert rep["window"] == window
    # window 内能排下几次就点几次：floor(stage / (hold + interval))
    assert rep["clicks"] == int(stage_ms // 25), rep
    assert rep["elapsed_ms"] <= stage_ms + 4.5


def test_burst_never_exceeds_fire_timeout():
    """fire_timeout 比 window 短时按 fire_timeout 收，绝不越界。"""
    clk = FakeClock()
    starts, click = _runner(clk)
    rep = click_burst.run(
        _plan(mode="burst", hold_ms=20, interval_ms=5, window="2s"),
        t_fire=clk.now(), fire_timeout=0.05,
        click=click, dialog=lambda: False, now=clk.now, sleep=clk.sleep)
    assert rep["stage_ms"] == 50.0
    assert rep["clicks"] == 2, rep
    assert rep["elapsed_ms"] <= 54.5


def test_hold_is_never_compressed_to_fit_one_more_click():
    """剩余 15ms 排不下 100ms 的按住：停手，而不是把 hold 压到 15ms。"""
    clk = FakeClock()
    starts, click = _runner(clk)
    rep = click_burst.run(
        _plan(mode="burst", hold_ms=100, interval_ms=5, window="all"),
        t_fire=clk.now(), fire_timeout=0.12,
        click=click, dialog=lambda: False, now=clk.now, sleep=clk.sleep)
    assert rep["clicks"] == 1, rep
    assert abs(rep["holds_ms"][0] - 100.0) <= 1e-6, rep["holds_ms"]


def test_append_mode_does_not_click_at_all():
    """append = 保持 click_hz 原节奏：burst 层一下都不点。"""
    cfg = _cfg()
    cfg["click"]["burst"]["mode"] = "append"
    assert click_burst.begin(cfg) is None
    assert click_burst.run(_plan(mode="append"),
                           t_fire=0.0, fire_timeout=10.0,
                           click=lambda hold: pytest.fail("append 不许自己点"),
                           dialog=lambda: False)["clicks"] == 0


# ---------------------------------------------------------------- D2 前后端联通

def test_snapshot_exposes_four_click_burst_keys():
    groups = switches.snapshot(_cfg())
    items = {it["key"]: it for g in groups for it in g["items"]}
    want = {"click.burst.mode": ("select", "append"),
            "click.burst.hold_ms": ("int", 100),
            "click.burst.interval_ms": ("int", 5),
            "click.burst.window": ("select", "all")}
    for key, (kind, default) in want.items():
        assert key in items, f"GET /api/switches 缺 {key}"
        assert items[key]["kind"] == kind
        assert items[key]["default"] == default
        assert items[key]["value"] == default
    assert [o[1] for o in items["click.burst.mode"]["options"]] == ["追加点击", "连点"]
    assert [o[1] for o in items["click.burst.window"]["options"]] == ["全部", "1 秒", "2 秒"]
    assert items["click.burst.hold_ms"]["hint"] == "毫秒"
    assert items["click.burst.interval_ms"]["hint"] == "毫秒"


def test_value_survives_save_and_load(tmp_path, monkeypatch):
    """控件值 → Config 落盘 → Config.load 读回，三段同值。"""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg = config.Config()
    applied = switches.apply_all(cfg, {"click.burst.hold_ms": 120,
                                       "click.burst.interval_ms": 9})
    assert applied["click.burst.hold_ms"] == 120             # ① 接口返回
    assert cfg.data["click"]["burst"]["hold_ms"] == 120      # ② 内存
    cfg.save()
    back = config.Config.load()                              # ③ 真读盘
    assert back.data["click"]["burst"]["hold_ms"] == 120
    assert back.data["click"]["burst"]["interval_ms"] == 9


def test_engine_reads_saved_value(tmp_path, monkeypatch):
    """写进配置的值必须被引擎侧读到，并按 D1 改变行为。"""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg = config.Config()
    switches.apply_all(cfg, {"click.burst.mode": "burst",
                             "click.burst.hold_ms": 60,
                             "click.burst.interval_ms": 20})
    plan, errors = click_burst.read(cfg)
    assert not errors
    assert plan == {"mode": "burst", "hold_ms": 60, "interval_ms": 20,
                    "window": "all"}
    clk = FakeClock()
    starts, click = _runner(clk)
    click_burst.run(plan, t_fire=clk.now(), fire_timeout=10.0,
                    click=click, dialog=lambda: False,
                    now=clk.now, sleep=clk.sleep)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert gaps and all(abs(g * 1000.0 - 80.0) <= 6.0 for g in gaps), \
        "引擎侧没按 hold+interval 生效"


# ---------------------------------------------------------------- D3 异常输入

@pytest.mark.parametrize("key,value,frag", [
    ("click.burst.interval_ms", "abc", "不是数字"),
    ("click.burst.interval_ms", None, "不是数字"),
    ("click.burst.hold_ms", float("nan"), "有限数字"),
    ("click.burst.hold_ms", float("inf"), "有限数字"),
    ("click.burst.interval_ms", 1.5, "必须是整数"),
    ("click.burst.hold_ms", 20.5, "必须是整数"),
    ("click.burst.mode", "bogus", "不在可选值"),
    ("click.burst.window", "3s", "不在可选值"),
])
def test_bad_values_are_rejected_with_reason(key, value, frag):
    cfg = _cfg()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, key, value)
    msg = str(ei.value)
    assert key in msg and frag in msg


def test_bool_and_negative_are_rejected_by_config_layer():
    """手改配置塞布尔 / 负数：模块层同样拒收（不靠上层兜住）。"""
    with pytest.raises(click_burst.BurstConfigError) as ei:
        click_burst.field("click.burst.hold_ms", True)
    assert "不是数字" in str(ei.value)
    with pytest.raises(click_burst.BurstConfigError) as ei2:
        click_burst.field("click.burst.hold_ms", -3)
    assert "小于下限" in str(ei2.value)


def test_batch_with_one_bad_value_writes_nothing():
    cfg = _cfg()
    before = json.dumps(cfg["click"], sort_keys=True)
    with pytest.raises(switches.SwitchValueError):
        switches.apply_all(cfg, {"click.burst.hold_ms": 70,
                                 "click.burst.interval_ms": "oops"})
    assert json.dumps(cfg["click"], sort_keys=True) == before


def test_hand_edited_config_reports_reason_not_silent_default():
    """手改 config.json 写成越界值：走默认，但必须给出可读原因。"""
    cfg = _cfg()
    cfg["click"]["burst"]["hold_ms"] = 19
    plan, errors = click_burst.read(cfg)
    assert plan["hold_ms"] == click_burst.DEFAULTS["hold_ms"]
    assert any("小于下限" in e and "click.burst.hold_ms" in e for e in errors)

    said = []
    click_burst.begin(cfg, say=lambda m, lv: said.append((m, lv)))
    assert said and said[0][1] == "warn", "被拒的值必须打进日志，不能静默"


# ---------------------------------------------------------------- D4 越界拒收

@pytest.mark.parametrize("key,value,frag", [
    ("click.burst.hold_ms", 19, "小于下限 20"),
    ("click.burst.hold_ms", 1001, "大于上限 1000"),
    ("click.burst.interval_ms", 0, "小于下限 1"),
    ("click.burst.interval_ms", 51, "大于上限 50"),
])
def test_out_of_range_is_rejected_with_bound(key, value, frag):
    cfg = _cfg()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, key, value)
    msg = str(ei.value)
    assert key in msg and frag in msg


def test_rejected_write_leaves_config_file_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg = config.Config()
    cfg.save()
    raw_before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(switches.SwitchValueError):
        switches.apply_all(cfg, {"click.burst.hold_ms": 1001})
    cfg.save()
    assert (tmp_path / "config.json").read_bytes() == raw_before


# ---------------------------------------------------------------- D5 互斥 / 共存

def test_burst_blocks_followup_with_readable_reason():
    cfg = _cfg()
    cfg["click"]["burst"]["mode"] = "burst"
    cfg["buy"]["followup"]["enabled"] = True
    blocked, reason = click_burst.followup_gate(cfg)
    assert blocked is False
    assert "连点与补刀互斥" in reason and "不生效" in reason

    cfg["click"]["burst"]["mode"] = "append"
    allowed, reason2 = click_burst.followup_gate(cfg)
    assert allowed is True and reason2 == ""


def test_switches_save_returns_warning_but_keeps_both_values(tmp_path, monkeypatch):
    """互斥：保存成功、返回 warnings；两个值都落盘，运行时只按连点。"""
    from core import server

    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    config.Config().save()

    class _H:
        def _json(self, obj, code=200):
            self.out = (obj, code)

    h = _H()
    server.Handler._set_switches(h, {"values": {
        "click.burst.mode": "burst",
        "click.burst.hold_ms": 100,
        "click.burst.interval_ms": 5,
        "click.burst.window": "1s",
        "buy.followup.enabled": True,
    }})
    resp, code = h.out
    assert code == 200 and resp["ok"] is True
    assert resp["warnings"] == [click_burst.MUTEX_WARNING]

    back = config.Config.load()
    assert back.data["click"]["burst"]["mode"] == "burst"
    assert back.data["buy"]["followup"]["enabled"] is True
    assert click_burst.followup_gate(back)[0] is False, "运行时没有按连点优先"

    # 只开补刀、连点 append：不互斥，warnings 为空
    server.Handler._set_switches(h, {"values": {
        "click.burst.mode": "append", "buy.followup.enabled": True}})
    resp2, code2 = h.out
    assert code2 == 200 and resp2["warnings"] == []


def test_followup_never_fires_when_burst_is_on(monkeypatch):
    """同一次进场只由连点负责：burst 开着，补刀点击数为 0。"""
    from core import engine as engine_mod

    eng = engine_mod.Engine()
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (0.0, (0, 0)))
    monkeypatch.setattr(eng, "_guard_insufficient", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_record", lambda *a, **k: None)

    class _Screen:
        size = (1920, 1080)

        def grab(self, box=None):
            return np.zeros((20, 20, 3), np.uint8)

        def norm_to_px(self, box):
            return (0, 0, 20, 20)

    class _Mouse:
        def __init__(self):
            self.clicks = []
            self.holds = []

        def click(self, x, y, **kw):
            self.clicks.append((int(x), int(y)))
            self.holds.append(kw.get("hold"))
            if kw.get("hold"):
                import time
                time.sleep(kw["hold"])
            return True

        def ensure_foreground(self, hwnd, screen=None):
            return True

    cfg = _CfgDict(click={"burst": {"mode": "burst", "hold_ms": 20,
                                    "interval_ms": 5, "window": "all"}},
                   buy={"followup": {"enabled": True}})
    cfg["semi_mode"] = False
    cfg["fire_timeout_s"] = 0.1
    mouse = _Mouse()
    assert eng._fire_and_confirm(cfg, _Screen(), mouse, np.zeros((10, 10), np.uint8),
                                 (500, 500), 0.0) is False
    st = eng.status()["stats"]
    assert st["burst"]["clicks"] >= 1
    assert st["followup"]["effective"] is False
    assert st["followup"]["used"] is False
    assert eng._followup_used is False
    # 连点结束后回落 click_hz，仍按原节奏点；但补刀一下都没补。
    assert len([c for c in mouse.clicks if c == (500, 500)]) == st["clicks"]


def test_engine_burst_stops_clicking_once_dialog_appears(monkeypatch):
    """引擎里：弹窗在第 3 下出现 → 购买点只挨 3 下，第 4 下绝不发出。"""
    from core import engine as engine_mod

    eng = engine_mod.Engine()

    class _Screen:
        size = (1920, 1080)

        def grab(self, box=None):
            return np.zeros((20, 20, 3), np.uint8)

        def norm_to_px(self, box):
            return (0, 0, 20, 20)

    class _Mouse:
        def __init__(self):
            self.clicks = []
            self.holds = []

        def click(self, x, y, **kw):
            self.clicks.append((int(x), int(y)))
            self.holds.append(kw.get("hold"))
            if kw.get("hold"):
                import time
                time.sleep(kw["hold"])
            return True

        def ensure_foreground(self, hwnd, screen=None):
            return True

    mouse = _Mouse()

    # 「弹窗」按购买点的真实点击数出现：第 3 下之后才算弹窗出现
    monkeypatch.setattr(
        engine_mod, "match",
        lambda *a, **k: (0.99, (0, 0)) if len(mouse.clicks) >= 3 else (0.0, (0, 0)))
    monkeypatch.setattr(eng, "_wait_dialog_unlock", lambda *a, **k: "ok")
    monkeypatch.setattr(eng, "_record", lambda *a, **k: None)

    cfg = _CfgDict(click={"burst": {"mode": "burst", "hold_ms": 20,
                                    "interval_ms": 5, "window": "all"}})
    cfg["semi_mode"] = False
    cfg["fire_timeout_s"] = 0.2
    cfg["confirm_clicks"] = 1
    cfg["confirm_timeout_s"] = 0.1
    try:
        eng._fire_and_confirm(cfg, _Screen(), mouse,
                              np.zeros((10, 10), np.uint8), (500, 500), 0.0)
    except Exception:               # noqa: BLE001  后续确认流程不参与本断言
        pass
    st = eng.status()["stats"]
    assert st["burst"]["stopped_by"] == "dialog"
    assert st["burst"]["clicks"] == 3
    assert len([c for c in mouse.clicks if c == (500, 500)]) == 3
    # 引擎把 hold 真的传到了鼠标通道（不是写死 sleep）
    assert all(abs(h - 0.02) <= 0.005 for h in mouse.holds if h), mouse.holds


def test_stop_after_hit_wins_over_burst():
    """买到即停：成交后补点立刻取消，不再往购买点发点击。"""
    clk = FakeClock()
    clicks = []
    hit = {"on": False}

    def click(hold_s):
        clicks.append(clk.now())
        clk.sleep(hold_s)

    def dialog():
        hit["on"] = True
        return True

    rep = click_burst.run(
        _plan(mode="burst", hold_ms=100, interval_ms=5, window="all"),
        t_fire=clk.now(), fire_timeout=10.0,
        click=click, dialog=dialog, now=clk.now, sleep=clk.sleep)
    assert rep["stopped_by"] == "dialog" and rep["clicks"] == 1


class _CfgDict(dict):
    def __init__(self, **over):
        super().__init__(**_cfg())
        self["regions"] = {"confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
                           "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1}}
        self.update(over)

    def region(self, name):
        return self["regions"].get(name)


# ---------------------------------------------------------------- 默认值直断

def _bare_mouse(input_mod):
    """不初始化 Win32 的 Mouse 壳：只测「hold 缺省时按下→抬起等多久」。"""
    m = input_mod.Mouse.__new__(input_mod.Mouse)
    m._origin_left = 0
    m._origin_top = 0
    m._game_hwnd = 777
    m._last_click = {}
    m._clicker = None
    m._message_cap = {"functional": "unknown"}
    m._abs = lambda x, y: (x, y)
    m._emit_plan = lambda plan: None
    m._record_occupancy = lambda ms: None
    m._record_fire = lambda *a, **k: None
    m._verify_cursor_at = lambda *a, **k: None
    return m


def test_hold_none_keeps_original_press_wait_on_all_three_channels(monkeypatch):
    """直断：`hold=None` 时按下→抬起之间的等待 = driver 0 / legacy 0.06 / message 0。

    这条不依赖通道回归的间接覆盖：谁把默认值改成 0.06、或把 `if hold:` 写成
    `if hold is not None:`，这里立刻红，不用等连点时序悄悄变了才发现。
    """
    import types

    from core import input as I

    events = []

    def fake_sleep(sec):
        events.append(("sleep", float(sec)))

    fake_time = types.SimpleNamespace(
        sleep=fake_sleep, perf_counter=time.perf_counter,
        time=time.time, monotonic=time.monotonic)
    monkeypatch.setattr(I, "time", fake_time)
    monkeypatch.setattr(I, "_cursor_pos", lambda: None)

    # ---- driver：DOWN 与 UP 之间不许有任何 sleep ----
    class _Drv:
        def mouse_device(self):
            return {"device": 1, "virtual": False, "vid": 1, "pid": 2}

        def send_mouse(self, code, **kw):
            events.append(("send", code))

    from core import interception as inter
    monkeypatch.setattr(inter, "shared", lambda: _Drv())
    m = _bare_mouse(I)
    m._click_driver(10, 10, verify=False, hold=None)
    down = [i for i, e in enumerate(events) if e == ("send", inter.MOUSE_LEFT_DOWN)][0]
    up = [i for i, e in enumerate(events) if e == ("send", inter.MOUSE_LEFT_UP)][0]
    assert events[down + 1:up] == [], f"driver 在 DOWN→UP 之间睡了：{events}"

    # ---- legacy：DOWN 之后固定等 0.06 ----
    events.clear()
    monkeypatch.setattr(I, "_user32", types.SimpleNamespace(
        SetCursorPos=lambda *a: True,
        ScreenToClient=lambda *a: True,
        PostMessageW=lambda *a: True))
    monkeypatch.setattr(I, "_cursor_pos", lambda: (10, 10))
    m = _bare_mouse(I)
    m._click_messages(10, 10, verify=False, hold=None)
    # legacy 用 PostMessageW：fake 只留 sleep 记账。
    # 与 HEAD（git show HEAD:core/input.py）逐项对齐：SetCursorPos 后 0.04、
    # 移动后 0.02、按下后 down_hold，UP 后不睡。
    # 注：HEAD 的 verify=True 分支还有一个审核 0.05 的等待，那是 mouse.human
    # 分支有意去掉的（避免每次点击多 50ms），不属于连点参数，也不在本用例路径。
    sleeps = [e[1] for e in events if e[0] == "sleep"]
    assert sleeps == [0.04, 0.02, 0.06], f"legacy 按下等待不是原 60ms：{sleeps}"

    # ---- message：Clicker 收到的 hold 就是 0.0 ----
    got = []

    class _Clicker:
        hwnd = 777
        verify = False
        last_result = {"accepted": True, "processed": True}

        def click(self, x, y, hold=0.0):
            got.append(float(hold))
            return True

    m = _bare_mouse(I)
    m._clicker = _Clicker()
    m._click_message(10, 10, verify=False, hold=None)
    assert got == [0.0], f"message 通道 hold 缺省值被改了：{got}"


# ------------------------------------------------- verify=True 的到位核验

class _StaleThenFreshUser32:
    """假 user32：前 `stale` 次回读给旧坐标，之后给目标点。

    模拟 SetCursorPos / 合成输入晚一帧才落地（要过消息泵，实机还有 DPI
    缩放与多屏换算）。旧的「立刻回读」会在这里误判成 CURSOR_NOT_AT_TARGET。
    """

    def __init__(self, old=(10, 10), new=(300, 200), stale=1):
        self.old, self.new, self.stale = old, new, stale
        self.reads = 0

    def GetCursorPos(self, pref):
        self.reads += 1
        x, y = self.old if self.reads <= self.stale else self.new
        pref._obj.x, pref._obj.y = x, y
        return 1


def _fake_time(monkeypatch, input_mod):
    """只换 sleep 记账，perf_counter 仍走真表（轮询要真的计预算）。"""
    import types

    sleeps = []

    def fake_sleep(sec):
        sleeps.append(float(sec))

    monkeypatch.setattr(input_mod, "time", types.SimpleNamespace(
        sleep=fake_sleep, perf_counter=time.perf_counter,
        time=time.time, monotonic=time.monotonic))
    return sleeps


def test_verify_poll_accepts_arrival_after_one_stale_read(monkeypatch):
    """回读第一次是旧坐标、第二次是目标点 → 判成功，不是 CURSOR_NOT_AT_TARGET。"""
    from core import input as I

    fake = _StaleThenFreshUser32()
    monkeypatch.setattr(I, "_user32", fake)
    sleeps = _fake_time(monkeypatch, I)

    m = I.Mouse.__new__(I.Mouse)
    rep = I.Mouse._verify_cursor_at(m, 300, 200, "legacy")

    assert rep["ok"] is True, rep
    assert rep["polls"] == 2 and fake.reads == 2, (rep, fake.reads)
    assert rep["cursor"] == (300, 200)
    # 只等了一个轮询步长就放行，不是固定 50ms
    assert sleeps == [I._CURSOR_POLL_S], sleeps
    assert rep["waited_ms"] < I._CURSOR_VERIFY_BUDGET_S * 1000.0


def test_verify_poll_timeout_reports_waited_ms(monkeypatch):
    """上界兜底：一直不到位才失败，且原因里带实际等待毫秒。"""
    from core import input as I

    monkeypatch.setattr(I, "_user32", _StaleThenFreshUser32(stale=10 ** 9))
    sleeps = _fake_time(monkeypatch, I)

    m = I.Mouse.__new__(I.Mouse)
    with pytest.raises(I.InputChannelError) as ei:
        I.Mouse._verify_cursor_at(m, 300, 200, "legacy", budget=0.006)

    msg = str(ei.value)
    assert ei.value.reason_code == I.R_CURSOR_NOT_AT_TARGET
    assert "等待" in msg and "上界 6ms" in msg, msg
    assert sleeps and all(s == I._CURSOR_POLL_S for s in sleeps), sleeps


def test_verify_true_sequence_keeps_three_sleeps_and_no_fixed_50ms(monkeypatch):
    """verify=True 的完整序列 = [0.04, 0.02, 0.06]；0.05 固定睡已换成轮询。

    0.05 不是被删掉：光标本来就到位时轮询一次即返回（等待 ≈0ms），
    比原来的固定 50ms 更快；没到位才轮到 50ms 上界兜底。
    """
    import types

    from core import input as I

    sleeps = _fake_time(monkeypatch, I)
    monkeypatch.setattr(I, "_user32", types.SimpleNamespace(
        SetCursorPos=lambda *a: True,
        ScreenToClient=lambda *a: True,
        PostMessageW=lambda *a: True))
    monkeypatch.setattr(I, "_cursor_pos", lambda: (10, 10))

    m = _bare_mouse(I)
    reps = []
    real = I.Mouse._verify_cursor_at

    def spy(self, sx, sy, channel, **kw):
        rep = real(self, sx, sy, channel, **kw)
        reps.append(rep)
        return rep

    m._verify_cursor_at = types.MethodType(spy, m)
    m._click_messages(10, 10, verify=True, hold=None)

    assert sleeps == [0.04, 0.02, 0.06], f"序列变了：{sleeps}"
    assert 0.05 not in sleeps, f"固定 50ms 又回来了：{sleeps}"
    assert reps and reps[0]["ok"] is True and reps[0]["polls"] >= 1, reps
    assert reps[0]["waited_ms"] < 5.0, reps

# ---------------------------------------------------------------- 前端（无头）

def _html():
    return (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")


def test_feature_block_markers_exist():
    html = _html()
    assert "<!-- FEATURE-BLOCKS BEGIN -->" in html
    assert "<!-- FEATURE-BLOCKS END -->" in html
    assert "['click.burst.', 'blk-fire-burst', 'click.burst']" in html
    assert "<!-- CLICK-BURST-NOTICE BEGIN" in html
    assert "<!-- CLICK-BURST-NOTICE END -->" in html


def test_desc_carries_lottery_sentence():
    items = {it["key"]: it for g in switches.snapshot(_cfg())
             for it in g["items"]}
    assert "连点只缩短再次进池的延迟，不提高单次中签概率" in items["click.burst.mode"]["desc"]


def test_all_four_controls_come_from_switches():
    keys = [s["key"] for s in switches.SWITCHES]
    for k in ("click.burst.mode", "click.burst.hold_ms",
              "click.burst.interval_ms", "click.burst.window"):
        assert k in keys
    # 渲染器会把 data-sw / data-kind 逐字写到控件上（契约 3.1）
    html = _html()
    assert 'data-sw="${esc(it.key)}"' in html
    assert 'data-kind="${esc(it.kind || \'int\')}"' in html
    assert "data-kind=\"select\"" in html


def test_dom_grouping_headless(tmp_path):
    """无头 DOM 断言：共享分组脚本真的把 click.burst 归进 #blk-fire-burst。"""
    shim = Path(__file__).with_name("_fe_click_burst_dom.js")
    keys = ["click.burst.mode", "click.burst.hold_ms",
            "click.burst.interval_ms", "click.burst.window",
            "buy.followup.enabled"]
    spec = tmp_path / "keys.json"
    spec.write_text(json.dumps(keys), encoding="utf-8")
    out = subprocess.run(
        ["node", str(shim), str(ROOT_DIR), str(spec)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["ok"] is True, data
    assert data["burst"] == keys[:4], data
    assert data["feature"] == "click.burst"
    assert data["after_render"] == keys[:4], data


def test_mutex_notice_visible_headless():
    """互斥提示真的显示在连点区块下方；保存回包的 warnings 也直接可见。"""
    shim = Path(__file__).with_name("_fe_click_burst_notice.js")
    out = subprocess.run(
        ["node", str(shim), str(ROOT_DIR)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["ok"] is True, data
    assert data["text"].startswith("连点与补刀互斥，当前按连点执行"), data
    assert data["shown_when_conflict"] == "block", data
    assert data["hidden_when_clear"] == "none", data
    assert data["shown_after_save"] == "block", data
    assert data["save_text"].startswith("连点与补刀互斥"), data
    assert data["in_burst_section"] is True, data
