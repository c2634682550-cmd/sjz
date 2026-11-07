"""每件随机刷新次数（scan.repeat.*）的回归用例 —— 契约 §7 的 D1~D5。

不碰真窗口、不移动真实光标：导航/屏幕/键盘全是替身，刷新只记调用次数。
D2 走真实的 /api/switches（GET + POST），并在落盘后真的跑一遍
_countdown_prepare，证明「改控件 → 配置变 → 实际刷新次数变」。
"""

import json
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import config as cfgmod
from core import engine as eng_mod
from core import records, repeat, switches
from core.server import Handler, ThreadingHTTPServer
from core.switches import SwitchValueError

from test_engine_countdown_flow import FakeCfg, FakeMouse, FakeNav, ListScreen, _engine


CLOCK = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.05}


def _cfg(min=None, max=None):
    """倒计时路线的最小替身配置；不写 scan.repeat 时用默认 1~3。"""
    cfg = FakeCfg()
    cfg["active_task"] = {}
    cfg["regions"]["clock"] = dict(CLOCK)
    if min is not None or max is not None:
        cfg["scan"] = {"repeat": {"min": 1 if min is None else min,
                                  "max": 3 if max is None else max}}
    return cfg


def _prepare(monkeypatch, cfg, nav=None, engine=None):
    """跑一遍 _countdown_prepare：列表永远为空，只观察刷新次数与状态。"""
    nav = nav or FakeNav
    nav.made = []
    e = engine or _engine()
    monkeypatch.setattr(eng_mod, "Navigator", nav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: [])
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: [])
    monkeypatch.setattr(e, "_read_serial", lambda scr: "")
    monkeypatch.setattr(e, "_click", lambda m, x, y: None)
    res = e._countdown_prepare(cfg, ListScreen(), FakeMouse(), [])
    return e, res


def _refresh_calls(nav=None):
    nav = nav or FakeNav
    return [c for c in nav.made[0].calls if c[0] == "refresh"]


# ------------------------------------------------------------------ D1
def test_d1_each_item_is_random_and_inside_the_range():
    rng = random.Random(7)
    samples = [repeat.draw(1, 3, rng) for _ in range(100)]
    assert all(1 <= v <= 3 for v in samples)
    # 每件独立抽：100 件里三个取值都得出现，且不是「全一样」
    assert set(samples) == {1, 2, 3}
    assert len(set(samples)) > 1


def test_d1_zero_zero_never_refreshes(monkeypatch):
    e, res = _prepare(monkeypatch, _cfg(0, 0))
    assert res is None
    assert _refresh_calls() == []                       # 0/0 = 一次都不刷
    st = e.status()["stats"]
    assert st["refresh_route"] == "countdown"
    assert st["refresh_counts"][-1] == {"item": "", "budget": 0, "used": 0}


def test_d1_budget_is_bounded_and_exits_when_used_up(monkeypatch):
    e, res = _prepare(monkeypatch, _cfg(2, 2))
    assert res is None
    assert len(_refresh_calls()) == 2                   # 抽到 2 就只刷 2 次
    assert e.status()["stats"]["refresh_counts"][-1] == {
        "item": "", "budget": 2, "used": 2}


def test_d1_refresh_failure_still_counts(monkeypatch):
    calls = {"n": 0}

    def boom(self):
        calls["n"] += 1
        raise RuntimeError("刷新按钮没出来")

    monkeypatch.setattr(FakeNav, "ons_refresh", boom)
    e, res = _prepare(monkeypatch, _cfg(2, 2))
    assert res is None
    assert calls["n"] == 2                              # 失败也计入次数，不白刷
    assert e.status()["stats"]["refresh_counts"][-1]["used"] == 2


# ------------------------------------------------------------------ D2
@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield "http://127.0.0.1:%d" % port
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "regions": {"clock": CLOCK, "list": {"x": 0.06, "y": 0.2,
                                             "w": 0.9, "h": 0.7}},
        "queue": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    return path


def _request(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_d2_control_to_config_to_real_refresh_count(server, cfg_file, monkeypatch):
    st, data = _request(server, "/api/switches")
    assert st == 200
    grp = next(g for g in data["groups"] if g["name"] == "抢购设置")
    items = {i["key"]: i for i in grp["items"]}
    # 控件由开关表生成：两个 int 字段都在 #swFire 那组里
    assert items["scan.repeat.min"]["kind"] == "int"
    assert items["scan.repeat.min"]["default"] == 1
    # 默认 max=1：升级后行为与现状一致，随机区间必须显式配置才生效
    assert items["scan.repeat.max"]["default"] == 1
    assert "0~10" in items["scan.repeat.min"]["hint"]

    st, data = _request(server, "/api/switches", {"values": {
        "scan.repeat.min": 2, "scan.repeat.max": 2}})
    assert (st, data.get("ok")) == (200, True)
    assert data["applied"] == {"scan.repeat.min": 2, "scan.repeat.max": 2}

    cfg = cfgmod.Config.load()
    assert repeat.limits(cfg) == (2, 2)                 # 配置真的变了
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["scan"]["repeat"] == {"min": 2, "max": 2}

    # 拿落盘的配置真跑一遍：每件只刷 2 次（不再是默认的 1~3 随机）
    e, res = _prepare(monkeypatch, cfg)
    assert res is None
    assert len(_refresh_calls()) == 2
    assert e.status()["stats"]["refresh_counts"][-1]["budget"] == 2


# ------------------------------------------------------------------ D3
@pytest.mark.parametrize("bad,why", [
    ("abc", "不是数字"),
    (None, "不是数字"),
    (float("nan"), "有限数字"),
    (float("inf"), "有限数字"),
    (1.5, "必须是整数"),
])
def test_d3_abnormal_values_rejected(bad, why):
    with pytest.raises(SwitchValueError) as ei:
        switches.apply_all({}, {"scan.repeat.min": bad})
    assert "scan.repeat.min" in str(ei.value)
    assert why in str(ei.value)


def test_d3_zero_legal_negative_and_eleven_rejected():
    # 契约把下限改成 0：0/0 是合法配置（完全不刷新）
    assert switches.apply_all({}, {"scan.repeat.min": 0,
                                  "scan.repeat.max": 0}) == {
        "scan.repeat.min": 0, "scan.repeat.max": 0}
    for v in (-1, 11):
        with pytest.raises(SwitchValueError):
            switches.apply_all({}, {"scan.repeat.min": v})


def test_d3_missing_section_falls_back_to_defaults():
    # 默认 1/1 = 等价现状（没货只刷一次），只在显式调大 max 后才随机
    assert repeat.limits({"regions": {}}) == (1, 1)
    assert repeat.limits({}) == (1, 1)


# ------------------------------------------------------------------ D4
def test_d4_inverted_rejected_with_reason_and_disk_untouched(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    with cfgmod.Config.transaction() as cfg:
        switches.apply_all(cfg.data, {"scan.repeat.min": 1,
                                      "scan.repeat.max": 3})
    before = path.read_bytes()

    with pytest.raises(SwitchValueError) as ei:
        with cfgmod.Config.transaction() as cfg:
            switches.apply_all(cfg.data, {"scan.repeat.min": 4,
                                          "scan.repeat.max": 3})
    assert str(ei.value) == "values：scan.repeat：最少 4 大于最多 3"
    assert path.read_bytes() == before                  # 一个字节都没动


def test_d4_single_field_save_cannot_create_inversion():
    cfg = {"scan": {"repeat": {"min": 1, "max": 3}}}
    with pytest.raises(SwitchValueError) as ei:
        switches.set(cfg, "scan.repeat.min", 4)
    assert "scan.repeat：最少 4 大于最多 3" in str(ei.value)
    assert switches.get(cfg, "scan.repeat.min") == 1


# ------------------------------------------------------------------ D5
def test_d5_interval_floor_coexists_with_scan_interval():
    # 间隔走既有的 list_scan_interval，硬下限兜底；两者互不改写
    assert repeat.gap_floor(0.05) == repeat.GAP_FLOOR_S
    assert repeat.gap_floor(5.0) == 5.0
    cfg = {"scan": {"repeat": {"min": 1, "max": 3}},
           "list_scan_interval": 5.0}
    assert repeat.limits(cfg) == (1, 3)
    assert switches.get(cfg, "scan.interval") == 5.0
    assert repeat.gap_floor(switches.get(cfg, "scan.interval")) == 5.0


def test_d5_list_route_marks_setting_inactive(monkeypatch):
    e = _engine()
    cfg = _cfg(1, 3)
    cfg["failsafe"] = False

    def stop(*a, **k):
        raise eng_mod.Aborted("测试到此为止")

    monkeypatch.setattr(e, "_ons_ready", stop)
    with pytest.raises(eng_mod.Aborted):
        e._execute_list(cfg, ListScreen(), FakeMouse(), None)
    st = e.status()["stats"]
    assert st["refresh_route"] == "list"                # 状态里注明不生效
    assert st["refresh_counts"] == []


def test_d5_record_carries_refresh_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PATH", str(tmp_path / "records.jsonl"))
    e = _engine()
    e._repeat_ctx = {"refresh_budget": 3, "refresh_used": 2}
    cfg = _cfg(1, 3)
    cfg["env.record"] = True
    cfg["active_task"] = {"name": "测试件"}
    e._record(cfg, ListScreen(), item="测试件", price=100, result="miss")
    rows = records.load()
    # records.add 会把 extra 平铺进记录行（契约里的 extra.refresh_budget）
    assert rows[-1]["refresh_budget"] == 3
    assert rows[-1]["refresh_used"] == 2


def test_d5_draw_value_is_the_hard_cap(monkeypatch):
    """随机取值只能决定「刷几次」，不能绕过预算：抽到 3 就最多刷 3 次。"""
    monkeypatch.setattr(eng_mod.repeat, "limits", lambda cfg: (0, 3))
    monkeypatch.setattr(eng_mod.repeat, "draw", lambda lo, hi: 3)
    e, res = _prepare(monkeypatch, _cfg(0, 3))
    assert res is None
    assert len(_refresh_calls()) == 3
    st = e.status()["stats"]["refresh_counts"][-1]
    assert st == {"item": "", "budget": 3, "used": 3}


def test_d5_stop_stops_refresh_immediately(monkeypatch):
    """买到即停/用户按停后：预算还剩也不再刷。"""
    e = _engine()
    calls = {"n": 0}

    def refresh_then_stop(self):
        calls["n"] += 1
        e._stop.set()                       # 模拟「买到即停」

    monkeypatch.setattr(FakeNav, "ons_refresh", refresh_then_stop)
    e, res = _prepare(monkeypatch, _cfg(1, 5), engine=e)
    assert res is None
    assert calls["n"] == 1                  # 只刷了触发停止的那一次
    assert e.status()["stats"]["refresh_counts"][-1]["used"] == 1
