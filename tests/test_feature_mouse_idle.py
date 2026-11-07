"""feature:mouse.idle —— 空闲漫游的五维验证。

D1 功能可用：60 秒倒计时 → 有漫游移动；move_wheel → 有滚轮；off → 零事件。
D2 前后端联通：GET /api/switches 带键 → POST → Config.load 读回 → 引擎分支行为变。
D3 异常输入：缺字段 / 负阈值 / 非法模式都被拒收并给原因。
D4 越界拒收：范围外、类型错都挡住；盘上字节不变。
D5 共存与互斥：共用拟人轨迹入口；开点窗口前归位且之后零事件；list 不失效。

全部用 FakeMouse / FakeClock，不构造真实 Mouse，不移动真实光标。
"""

import hashlib
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as config_mod  # noqa: E402
from core import idle_roam, switches  # noqa: E402
from core.config import Config  # noqa: E402
from core.server import Handler, ThreadingHTTPServer  # noqa: E402

HWND = 4242
RECT = (0.0, 0.0, 900.0, 600.0)


class FakeClock:
    def __init__(self, zero_at):
        self._zero = float(zero_at)

    def zero_at(self):
        return self._zero

    def remaining(self, now):
        return max(0.0, self._zero - now)


class FakeMouse:
    """只实现 idle 需要的接口；click 被调用即视为违规。"""

    def __init__(self, hwnd=HWND):
        self.hwnd = hwnd
        self.paths = []
        self.wheels = []
        self.clicks = 0

    def foreground(self):
        return self.hwnd

    def move_path(self, points, total_ms):
        self.paths.append((list(points), total_ms))
        return True

    def click(self, *args, **kwargs):
        self.clicks += 1


class FakeTraj:
    active = True

    def __init__(self):
        self.calls = []

    def path_for(self, x, y, start=None):
        self.calls.append((x, y, start))
        return [(start[0], start[1]), (x, y)], 100.0


def _cfg(mode="move", **extra):
    cfg = Config()
    cfg.data.setdefault("mouse", {}).setdefault("idle", {}).update({
        "mode": mode, "interval_ms": 1200, "return_s": 2,
        "min_remain_s": 8, "wheel_notches": 3,
    })
    cfg.data["mouse"]["idle"].update(extra)
    return cfg


def _session(cfg, mouse=None, workflow="countdown", input_mode="interception",
             traj=None, wheel_fn=None):
    mouse = mouse or FakeMouse()
    wheels = wheel_fn or (lambda x, y, n: mouse.wheels.append((x, y, n)))
    sess = idle_roam.RoamSession(
        cfg, mouse=mouse, hwnd=HWND, workflow=workflow,
        input_mode=input_mode, traj=traj, rect=RECT,
        cursor_pos_fn=(450.0, 300.0), wheel_fn=wheels,
        rng=random.Random(7),
    )
    sess.begin_attempt()
    return sess, mouse


def _drive(sess, clock, until_s, step=0.02):
    now = 0.0
    while now <= until_s:
        sess.tick(now, clock)
        now += step


# ---------------------------------------------------------------- D1 功能可用

def test_d1_countdown_60s_roams_then_homes_before_fire_window():
    cfg = _cfg("move")
    sess, mouse = _session(cfg)
    clock = FakeClock(60.0)
    _drive(sess, clock, 66.0)
    snap = sess.snapshot()

    assert snap["moves"] >= 1, "一次漫游必须产生至少 1 次移动"
    assert snap["wheel"] == 0
    assert snap["clicks"] == 0
    assert snap["settled"] is True
    assert snap["settled_left_s"] is not None
    # return_s=2、FIRE_LEAD_S=5 → 最迟在剩余 7s 归位（开点窗口前 2s）。
    assert snap["settled_left_s"] >= idle_roam.FIRE_LEAD_S + 2.0 - 0.05
    assert all(p["inside"] for p in snap["trace"]), "轨迹点不得跑出客户区"
    assert len(mouse.paths) >= 1


def test_d1_move_wheel_emits_wheel_notches():
    cfg = _cfg("move_wheel")
    sess, mouse = _session(cfg)
    _drive(sess, FakeClock(60.0), 66.0)
    snap = sess.snapshot()
    assert snap["moves"] >= 1
    assert snap["wheel"] >= 1
    assert mouse.wheels and all(w[2] == 3 for w in mouse.wheels)
    assert snap["clicks"] == 0


def test_d1_off_emits_nothing():
    cfg = _cfg("off")
    sess, _ = _session(cfg)
    _drive(sess, FakeClock(60.0), 66.0)
    snap = sess.snapshot()
    assert snap["moves"] == 0 and snap["wheel"] == 0
    assert snap["events"] == [] and snap["phase"] == "off"


# ---------------------------------------------------------------- D2 前后端联通

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(tmp_path / "config.json"))
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    yield "http://127.0.0.1:%d" % port
    httpd.shutdown()
    httpd.server_close()


def _get_json(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(base, path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_d2_http_switch_roundtrip_and_engine_behavior(http_server, tmp_path, monkeypatch):
    # 1) 前端读到的开关表里有这一组控件（data-sw 由 key 提供，data-kind 由 kind）
    data = _get_json(http_server, "/api/switches")
    flat = {it["key"]: it for g in data["groups"] for it in g["items"]}
    for key in ("mouse.idle.mode", "mouse.idle.interval_ms",
                "mouse.idle.return_s", "mouse.idle.min_remain_s",
                "mouse.idle.wheel_notches"):
        assert key in flat, "开关表缺 %s，前端不会渲染控件" % key
    mode = flat["mouse.idle.mode"]
    assert mode["kind"] == "select" and mode["value"] == "off"
    assert [o[0] for o in mode["options"]] == ["off", "move", "move_wheel"]

    # 2) POST 存盘 → Config.load 读回，类型/键名都对
    status, body = _post_json(http_server, "/api/switches", {"values": {
        "mouse.idle.mode": "move_wheel",
        "mouse.idle.interval_ms": 1800,
        "mouse.idle.return_s": 3,
        "mouse.idle.min_remain_s": 10,
        "mouse.idle.wheel_notches": 2,
    }})
    assert status == 200 and body["ok"] is True
    disk = Config.load()
    assert disk.data["mouse"]["idle"] == {
        "mode": "move_wheel", "interval_ms": 1800, "return_s": 3,
        "min_remain_s": 10, "wheel_notches": 2,
    }

    # 3) 同一份落盘值喂给引擎侧会话 → 行为分型正确
    sess, mouse = _session(disk)
    _drive(sess, FakeClock(60.0), 66.0)
    snap = sess.snapshot()
    assert snap["moves"] >= 1 and snap["wheel"] >= 1
    assert all(w[2] == 2 for w in mouse.wheels)

    off = _cfg("off")
    off_sess, _ = _session(off)
    _drive(off_sess, FakeClock(60.0), 66.0)
    assert off_sess.snapshot()["moves"] == 0


def test_d2_frontend_grouping_maps_mouse_idle_to_block():
    """契约 §3.2：控件由共享脚本按 data-sw 前缀归入 #blk-fire-idle。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "web", "index.html"), encoding="utf-8").read()
    assert 'id="swFire"' in html and "data-sw=" in html
    assert "'mouse.idle.', 'blk-fire-idle'" in html


# ---------------------------------------------------------------- D3 异常输入

def test_d3_bad_mode_is_rejected_with_reason():
    cfg = Config()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, "mouse.idle.mode", "sideways")
    msg = str(ei.value)
    assert "mouse.idle.mode" in msg and "可选值" in msg


def test_d3_negative_or_missing_number_is_rejected_with_reason():
    cfg = Config()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, "mouse.idle.return_s", -1)
    assert "mouse.idle.return_s" in str(ei.value) and "下限" in str(ei.value)

    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, "mouse.idle.min_remain_s", None)
    assert "mouse.idle.min_remain_s" in str(ei.value)


def test_d3_missing_values_field_is_400(http_server):
    status, body = _post_json(http_server, "/api/switches", {})
    assert status == 400 and body["ok"] is False
    assert "缺少 values" in body["msg"]


# ---------------------------------------------------------------- D4 越界拒收

@pytest.mark.parametrize("key,value", [
    ("mouse.idle.interval_ms", 299), ("mouse.idle.interval_ms", 10001),
    ("mouse.idle.return_s", 0), ("mouse.idle.return_s", 31),
    ("mouse.idle.min_remain_s", 2), ("mouse.idle.min_remain_s", 3601),
    ("mouse.idle.wheel_notches", 0), ("mouse.idle.wheel_notches", 11),
])
def test_d4_out_of_range_is_rejected(key, value):
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(Config(), key, value)
    assert key in str(ei.value)


def test_d4_type_errors_are_rejected():
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(Config(), "mouse.idle.interval_ms", "abc")
    assert "不是数字" in str(ei.value)
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(Config(), "mouse.idle.interval_ms", 1.5)
    assert "整数" in str(ei.value)


def test_d4_http_out_of_range_is_400_with_field(http_server):
    status, body = _post_json(http_server, "/api/switches", {"values": {
        "mouse.idle.interval_ms": 299,
    }})
    assert status == 400 and body["ok"] is False
    assert "mouse.idle.interval_ms" in body["msg"]
    assert "下限" in body["msg"]


def test_d4_rejected_batch_does_not_touch_disk(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(path))
    cfg = Config()
    with cfg.transaction():
        switches.apply_all(cfg, {"mouse.idle.mode": "move",
                                 "mouse.idle.interval_ms": 1800})
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(switches.SwitchValueError):
        with cfg.transaction():
            switches.apply_all(cfg, {"mouse.idle.return_s": -1})
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after
    assert Config.load().data["mouse"]["idle"]["return_s"] == 2


# ---------------------------------------------------------------- D5 共存与互斥

def test_d5_idle_shares_human_trajectory():
    cfg = _cfg("move")
    traj = FakeTraj()
    sess, mouse = _session(cfg, traj=traj)
    sess.tick(0.0, FakeClock(60.0))
    snap = sess.snapshot()
    assert traj.calls, "漫游移动必须走拟人轨迹决策器"
    assert mouse.paths and snap["moves"] >= 1 and snap["clicks"] == 0


def test_d5_message_channel_reports_wheel_invalid():
    cfg = _cfg("move_wheel")
    sess, mouse = _session(cfg, input_mode="message")
    assert "滚轮" in sess.startup_notice()
    sess.tick(0.0, FakeClock(60.0))
    snap = sess.snapshot()
    assert snap["effective"] == "message_no_wheel"
    assert snap["wheel"] == 0 and mouse.wheels == []
    assert "滚轮" in snap["reason"] or "无真实光标" in snap["reason"]


def test_d5_fire_window_preempts_and_then_stays_quiet():
    cfg = _cfg("move")
    sess, mouse = _session(cfg)
    clock = FakeClock(100.0)
    sess.tick(88.0, clock)                  # 剩余 12s，允许漫游
    assert sess.snapshot()["moves"] >= 1

    sess.tick(93.5, clock)                  # 剩余 6.5s = 5 + 2 - 0.5
    snap = sess.snapshot()
    assert snap["settled"] is True and snap["phase"] == "settled"
    assert snap["events"][-1]["type"] == "settle"
    mv, ev = snap["moves"], len(snap["events"])

    sess.tick(94.0, clock)                  # 开点窗口内：不再移动、不再记事件
    after = sess.snapshot()
    assert after["moves"] == mv and len(after["events"]) == ev
    assert mouse.clicks == 0


def test_d5_list_route_has_zero_events_and_one_notice():
    cfg = _cfg("move")
    sess, _ = _session(cfg, workflow="list")
    assert "没有可反推的解锁时刻" in sess.startup_notice()
    sess.tick(0.0, FakeClock(60.0))
    snap = sess.snapshot()
    assert snap["moves"] == 0 and snap["events"] == []
    assert snap["phase"] == "inactive_route"
