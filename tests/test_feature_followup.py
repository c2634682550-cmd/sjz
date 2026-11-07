"""假购买补刀（buy.followup.enabled）的回归测试。

契约：`_feature_contract.md`
    2.7  `buy.followup.enabled`（bool，默认 false）：只在「一次开火尝试结束
         且没等到确认弹窗」后补 1 次点击（同一购买点），补完再读一次确认
         弹窗；仍没有就按 miss 记。
         D1 默认关 / 开+append+miss → 恰好 1 次补刀点击 / 连点开启 → 0 次 +
            info 日志 / 无确认区域版式 → 0 次。
         D2 单测 + 引擎注入守卫；记录 extra.followup_used；stats.followup。
         D3 严格布尔：2、"maybe" 拒收；1/0/on/off/是/否 接受；缺键走默认。
    4.1  DEFAULTS 里 `buy.followup.enabled = false`。
    4.5  引擎只许在指定扩展点追加，本文件只测这些扩展点的行为。

为什么不用真 sleep：
    · 引擎用例把 `engine.time.sleep` 换成空操作；`fire.timeout` 置 0，
      开火循环一次都不点，后面的点击就只可能来自补刀，可以精确计数。
    · 配置/开关用例是纯函数，没有时间概念。
"""
import copy
import json
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core import engine as engine_mod
from core import switches
from core.config import DEFAULTS

KEY = "buy.followup.enabled"
FIRE_GROUP = "抢购设置"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_INDEX = os.path.join(ROOT, "web", "index.html")


# ---------------------------------------------------------------- 假环境

class _Screen:
    size = (1920, 1080)

    def grab(self, box=None):
        return np.zeros((40, 40, 3), np.uint8)

    def norm_to_px(self, box):
        return (0, 0, 40, 40)

    def close(self):
        pass


class _Mouse:
    def __init__(self):
        self.clicks = 0

    def click(self, x, y, **kw):
        self.clicks += 1
        return True

    def ensure_foreground(self, hwnd, screen=None):
        return True


class _Cfg(dict):
    def region(self, name):
        return self["regions"].get(name)


def _cfg(**over):
    cfg = _Cfg(copy.deepcopy(DEFAULTS))
    cfg["regions"] = {
        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
        "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1},
    }
    cfg.update(over)
    return cfg


def _quiet(eng, monkeypatch, match):
    """把引擎里会碰真环境的钩子换成假的（不会移动鼠标/写盘）。"""
    monkeypatch.setattr(engine_mod, "match", match)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda *a, **k: None)
    real_get = switches.get
    monkeypatch.setattr(
        engine_mod.switches, "get",
        lambda cfg, key: 0.0 if key == "fire.timeout" else real_get(cfg, key))
    eng._insufficient_showing = lambda s: False
    eng._guard_insufficient = lambda *a, **k: None
    eng._wait_dialog_unlock = lambda *a, **k: True
    eng._balance = lambda *a, **k: None
    eng._close_reward = lambda *a, **k: None
    eng._say = lambda *a, **k: None


def _spy_record(eng, monkeypatch):
    """让 `_record` 走真实逻辑，但把写盘换成收集。"""
    rows = []
    monkeypatch.setattr(engine_mod.records, "add", lambda **kw: rows.append(kw))
    monkeypatch.setattr(eng, "_save_shot", lambda *a, **k: "")
    return rows


def _run(eng, cfg, mouse, monkeypatch, click_spy):
    monkeypatch.setattr(eng, "_click", click_spy)
    return eng._fire_and_confirm(cfg, _Screen(), mouse,
                                 np.zeros((10, 10), np.uint8), (500, 500),
                                 time.perf_counter())


# ---------------------------------------------------------------- 配置与开关表

def test_default_is_off_in_both_defaults_and_switch_table():
    assert DEFAULTS["buy"]["followup"]["enabled"] is False
    s = switches.spec(KEY)
    assert s["kind"] == "bool" and s["default"] is False
    assert s["path"] == "buy.followup.enabled" and s.get("strict") is True


@pytest.mark.parametrize("val,want", [
    (True, True), (False, False),
    ("on", True), ("off", False), ("true", True), ("1", True),
    ("是", True), ("否", False), (1, True), (0, False),
])
def test_switch_accepts_bool_and_contract_spellings(val, want):
    cfg = _Cfg(copy.deepcopy(DEFAULTS))
    assert switches.set(cfg, KEY, val) is want
    assert switches.get(cfg, KEY) is want


@pytest.mark.parametrize("bad", [2, "maybe", "", None, [], {}, 1.5, float("nan")])
def test_switch_rejects_non_bool_with_readable_reason(bad):
    cfg = _Cfg(copy.deepcopy(DEFAULTS))
    before = switches.get(cfg, KEY)
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, KEY, bad)
    assert "不是布尔值" in str(ei.value)
    assert switches.get(cfg, KEY) is before            # 拒收不落配置


def test_missing_key_falls_back_to_default_off():
    cfg = _Cfg(copy.deepcopy(DEFAULTS))
    del cfg["buy"]
    assert switches.get(cfg, KEY) is False


def test_snapshot_exposes_control_inside_fire_group():
    groups = {g["name"]: g for g in switches.snapshot(_Cfg(copy.deepcopy(DEFAULTS)))}
    assert FIRE_GROUP in groups, "挂载点 #swFire 只渲染「抢购设置」组"
    item = [i for i in groups[FIRE_GROUP]["items"] if i["key"] == KEY]
    assert len(item) == 1
    it = item[0]
    assert it["kind"] == "bool" and it["value"] is False
    for phrase in ("默认关", "互斥", "不生效", "不补刀", "双买"):
        assert phrase in it["desc"], f"界面说明缺关键词：{phrase}"


def test_persist_roundtrip_control_value_to_disk(tmp_path, monkeypatch):
    """控件值 → config.json → 重新加载。三段都断言。"""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    cfg = config.Config.load()                     # 首启：默认值落盘
    assert json.loads(open(path, encoding="utf-8").read())[
        "buy"]["followup"]["enabled"] is False

    switches.set(cfg, KEY, "on")                   # 控件送字符串也要认
    cfg.save()
    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert on_disk["buy"]["followup"]["enabled"] is True

    again = config.Config.load()
    assert switches.get(again, KEY) is True        # 运行时读到的就是盘上的值


def test_persist_rejects_bad_value_and_keeps_disk_intact(tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.Config.load()
    cfg.save()
    before = open(path, encoding="utf-8").read()

    with pytest.raises(switches.SwitchValueError) as ei:
        with config.Config.transaction() as tx:
            switches.set(tx, KEY, 2)
    assert "不是布尔值" in str(ei.value)
    assert open(path, encoding="utf-8").read() == before


def test_post_api_applies_and_persists(tmp_path, monkeypatch):
    """POST /api/switches 的处理函数：applied 回包 + 落盘。"""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    config.Config.load()

    from core import server
    replies = []

    class _H:
        def _json(self, payload, code=200):
            replies.append((code, payload))
            return code, payload

    code, resp = server.Handler._set_switches(_H(), {"values": {KEY: "on"}})
    assert code == 200 and resp["ok"] is True
    assert resp["applied"][KEY] is True
    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert on_disk["buy"]["followup"]["enabled"] is True

    code, resp = server.Handler._set_switches(_H(), {"values": {KEY: 2}})
    assert code == 400 and "不是布尔值" in resp["reason"]
    assert json.loads(open(path, encoding="utf-8").read())[
        "buy"]["followup"]["enabled"] is True   # 拒收后盘上值不变


def test_runtime_read_from_config_object():
    """引擎读的是 Config 对象（不是 dict），开关必须打通到 .data。"""
    cfg = config.Config()
    cfg.data.setdefault("buy", {}).setdefault("followup", {})["enabled"] = True
    assert switches.get(cfg, KEY) is True


# ---------------------------------------------------------------- 引擎行为 D1

def test_default_off_never_follows_up(monkeypatch):
    eng = engine_mod.Engine()
    mouse = _Mouse()
    rows = _spy_record(eng, monkeypatch)
    _quiet(eng, monkeypatch, lambda *a, **k: (0.1, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda s: False)

    clicks = []
    got = _run(eng, _cfg(), mouse, monkeypatch,
               lambda m, x, y, **kw: clicks.append((x, y)))

    assert got is False
    assert clicks == [], "默认关却补了刀"
    assert len(rows) == 1 and rows[0]["result"] == "miss"
    assert rows[0]["extra"]["followup_used"] is False
    assert eng._stats["followup"] == {"enabled": False, "effective": False,
                                      "used": False}
    assert eng._stats["clicks"] == 0


def test_enabled_and_miss_does_exactly_one_extra_click(monkeypatch):
    eng = engine_mod.Engine()
    mouse = _Mouse()
    rows = _spy_record(eng, monkeypatch)
    _quiet(eng, monkeypatch, lambda *a, **k: (0.1, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda s: False)

    cfg = _cfg()
    cfg["buy"]["followup"]["enabled"] = True
    clicks = []
    got = _run(eng, cfg, mouse, monkeypatch,
               lambda m, x, y, **kw: clicks.append((x, y)))

    assert got is False, "没等到确认弹窗不能当成交"
    assert clicks == [(500, 500)], f"补刀应恰好 1 次点击，实际 {clicks}"
    assert len(rows) == 1 and rows[0]["result"] == "miss"   # 台账只记一次
    assert rows[0]["extra"]["followup_used"] is True
    assert eng._stats["followup"] == {"enabled": True, "effective": True,
                                      "used": True}


def test_followup_click_can_recover_the_confirm_dialog(monkeypatch):
    """补刀那一下把弹窗点出来了 → 继续走确认，最后按成交记一次。"""
    eng = engine_mod.Engine()
    mouse = _Mouse()
    rows = _spy_record(eng, monkeypatch)

    state = {"open": False, "bought": False}

    def fake_match(img, tpl, *a, **k):
        return (0.99 if state["open"] else 0.1), (0, 0)

    def fake_click(m, x, y, **kw):
        mouse.clicks += 1
        if (x, y) == (500, 500):
            state["open"] = True          # 补刀/购买点击 → 弹窗出现
        else:
            state["open"] = False         # 确认点击 → 弹窗收掉
            state["bought"] = True

    _quiet(eng, monkeypatch, fake_match)
    monkeypatch.setattr(eng, "_bought_dialog", lambda s: state["bought"])

    cfg = _cfg()
    cfg["buy"]["followup"]["enabled"] = True
    clicks = []
    got = _run(eng, cfg, mouse, monkeypatch,
               lambda m, x, y, **kw: (clicks.append((x, y)),
                                      fake_click(m, x, y, **kw)))

    assert got is True
    assert clicks[0] == (500, 500), "第一下必须是补刀点击（同一购买点）"
    assert len(rows) == 1 and rows[0]["result"] == "hit"
    assert rows[0]["extra"]["followup_used"] is True
    assert eng._stats["followup"]["used"] is True


def test_burst_mode_explicitly_skips_and_says_so(monkeypatch):
    """与连点互斥：开关照样保存，但本次补刀显式不生效并写一条 info。"""
    eng = engine_mod.Engine()
    mouse = _Mouse()
    rows = _spy_record(eng, monkeypatch)
    _quiet(eng, monkeypatch, lambda *a, **k: (0.1, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda s: False)
    # 只测互斥判定本身：让连点分支「有计划」但一次都不点。
    monkeypatch.setattr(engine_mod.click_burst, "begin",
                        lambda cfg, say=None: {"mode": "burst"})
    monkeypatch.setattr(engine_mod.click_burst, "run",
                        lambda *a, **k: {"clicks": 0})

    logs = []
    eng._say = lambda msg, lvl="info": logs.append((msg, lvl))
    cfg = _cfg()
    cfg["buy"]["followup"]["enabled"] = True
    cfg["click"] = {"burst": {"mode": "burst"}}

    clicks = []
    got = _run(eng, cfg, mouse, monkeypatch,
               lambda m, x, y, **kw: clicks.append((x, y)))

    assert got is False and clicks == []
    assert any("不生效" in m for m, _ in logs), "连点互斥必须显式说明，不能静默"
    assert eng._stats["followup"] == {"enabled": True, "effective": False,
                                      "used": False}
    assert len(rows) == 1 and rows[0]["extra"]["followup_used"] is False


def test_no_confirm_region_layout_never_follows_up(monkeypatch):
    """无确认弹窗的版式走 _buy_without_dialog，补刀完全不进入。"""
    eng = engine_mod.Engine()
    mouse = _Mouse()
    monkeypatch.setattr(engine_mod.time, "sleep", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(eng, "_buy_without_dialog",
                        lambda *a, **k: called.append(1) or False)
    clicks = []
    monkeypatch.setattr(eng, "_click",
                        lambda m, x, y, **kw: clicks.append((x, y)))

    cfg = _cfg()
    cfg["buy"]["followup"]["enabled"] = True
    cfg["regions"].pop("confirm")

    assert eng._fire_and_confirm(cfg, _Screen(), mouse,
                                 np.zeros((10, 10), np.uint8), (500, 500),
                                 time.perf_counter()) is False
    assert called == [1]
    assert clicks == []
    assert "followup" not in eng._stats


def test_semi_auto_aborts_before_any_click(monkeypatch):
    from core.engine import Aborted
    eng = engine_mod.Engine()
    mouse = _Mouse()
    clicks = []
    monkeypatch.setattr(eng, "_click",
                        lambda m, x, y, **kw: clicks.append((x, y)))

    cfg = _cfg(semi_mode=True)
    cfg["buy"]["followup"]["enabled"] = True
    with pytest.raises(Aborted):
        eng._fire_and_confirm(cfg, _Screen(), mouse,
                              np.zeros((10, 10), np.uint8), (500, 500),
                              time.perf_counter())
    assert clicks == []


def test_status_has_stable_followup_shape():
    eng = engine_mod.Engine()
    assert eng.status()["stats"]["followup"] == {
        "enabled": False, "effective": False, "used": False}


def test_engine_uses_value_loaded_from_disk(tmp_path, monkeypatch):
    """D2 第三段：盘上的开关值 → 引擎真的补刀（不是只改了个变量）。"""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.Config.load()
    cfg.data["regions"] = {
        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
        "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1},
    }
    switches.set(cfg, KEY, "on")
    cfg.save()
    cfg2 = config.Config.load()
    assert switches.get(cfg2, KEY) is True

    eng = engine_mod.Engine()
    mouse = _Mouse()
    rows = _spy_record(eng, monkeypatch)
    _quiet(eng, monkeypatch, lambda *a, **k: (0.1, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda s: False)

    clicks = []
    got = _run(eng, cfg2, mouse, monkeypatch,
               lambda m, x, y, **kw: clicks.append((x, y)))
    assert got is False and clicks == [(500, 500)]
    assert rows[0]["extra"]["followup_used"] is True


# ---------------------------------------------------------------- 前端（无头静态）

def test_frontend_mount_and_binding_are_headless_checked():
    src = open(WEB_INDEX, encoding="utf-8").read()
    assert 'id="swFire"' in src, "抢购设置挂载点不见了"
    assert "data-sw" in src and "data-kind" in src, "开关控件不是数据驱动绑定的"
    assert FIRE_GROUP in src, "挂载映射里的分组名变了"


def test_frontend_switch_row_would_render_our_key():
    """无头链路：snapshot → 前端按 group 取出 → 生成 data-sw=key 的控件。"""
    groups = switches.snapshot(_Cfg(copy.deepcopy(DEFAULTS)))
    rows = [i["key"] for g in groups if g["name"] == FIRE_GROUP
            for i in g["items"]]
    assert KEY in rows
    s = switches.spec(KEY)
    assert s["kind"] in ("bool", "int", "float", "select")
    assert s["label"] and s["desc"]
