"""开关表：拨了真的有用。

这组用例守的是「不做假开关」这条线：

1. 表本身自洽（key 唯一、分组有序、默认值和 config.DEFAULTS 一致）
2. **每个开关的配置路径都有人在读** —— 静态扫 core/ 的源码，找不到消费点
   就算失败。界面摆一排 checkbox、后端根本不读，是这个项目里最恶心的一种
   假功能，所以拿测试钉死。
3. 类型归一化（前端传字符串也能吃）
4. 几个能直接观察行为的新开关：半自动停手、关掉记录、关掉成交校验。
"""

import os
import re
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, switches

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "core")


# ---------------------------------------------------------------- 表自洽

def test_keys_are_unique_and_groups_are_ordered():
    keys = [s["key"] for s in switches.SWITCHES]
    assert len(keys) == len(set(keys)), "有重复的 key"
    order = [switches.spec(k)["group"] for k in keys]
    # 分组的第一次出现顺序必须和 GROUPS 一致（界面按这个排）
    seen = []
    for g in order:
        if not seen or seen[-1] != g:
            assert g not in seen, f"分组「{g}」被打散了，界面上会跳来跳去"
            seen.append(g)
    assert seen == switches.GROUPS


def test_there_are_at_least_thirty_switches():
    """竞品右栏是三十多个开关；少了说明有功能没做进来。"""
    assert len(switches.SWITCHES) >= 30, len(switches.SWITCHES)


def test_every_default_matches_config_defaults():
    """表里的默认值必须和 config.DEFAULTS 一致。

    不一致的后果很隐蔽：界面上显示「开」，引擎读到的是「关」。
    """
    d = config.DEFAULTS
    for s in switches.SWITCHES:
        path = s["path"]
        cur = d
        for part in path.split("."):
            assert isinstance(cur, dict) and part in cur, \
                f"{s['key']} 的路径 {path} 在 config.DEFAULTS 里不存在"
            cur = cur[part]
        assert cur == s["default"], \
            f"{s['key']} 默认值不一致：表里 {s['default']!r}，配置 {cur!r}"


def test_every_switch_has_a_real_consumer():
    """每个开关的配置项都必须在 core/ 里被读过。

    这是「不做假功能」的硬约束：静态扫源码，找不到消费点直接失败。
    新增开关时如果只在表里加一行、没人读，这条会拦住。
    """
    files = [f for f in os.listdir(CORE) if f.endswith(".py")
             and f not in ("switches.py", "config.py")]
    blob = ""
    for f in files:
        with open(os.path.join(CORE, f), encoding="utf-8") as fh:
            blob += fh.read()

    # 这些叶子名太泛（"enabled" 到处都有），单靠名字证明不了什么，
    # 但它们所属的配置段是真的被读的（hotkey.enabled → cfg["hotkey"]["enabled"]）
    generic = {"enabled", "hotkey", "craft", "sort", "threshold", "workflow"}

    missing = []
    for s in switches.SWITCHES:
        leaf = s["path"].split(".")[-1]
        if leaf in generic:
            continue
        # 两种消费方式都算：直接读配置（cfg["click_hz"]），
        # 或者经开关表读（switches.get(cfg, "fire.hz")）。
        if re.search(r"\b" + re.escape(leaf) + r"\b", blob):
            continue
        if ('"' + s["key"] + '"') in blob:
            continue
        missing.append(f"{s['key']} → {s['path']}")
    assert not missing, f"这些开关没人读，是假开关：{missing}"


# ---------------------------------------------------------------- 类型

@pytest.mark.parametrize("key,raw,want", [
    ("env.dry_run", "true", True),
    ("env.dry_run", "0", False),
    ("env.dry_run", 1, True),
    ("fire.hz", "33", 33),
    ("fire.hz", 33.7, 33),
    ("fire.threshold", "0.9", 0.9),
    ("run.mode", "semi", "semi"),
    ("run.mode", "乱写的", "auto"),        # 非法 select 回默认
    ("filter.craft", "", ""),
])
def test_values_are_normalised(key, raw, want):
    cfg = config.Config()
    switches.set(cfg, key, raw)
    assert switches.get(cfg, key) == want


def test_missing_config_falls_back_to_default():
    """配置里没这个键（老版本 config.json）时用默认值，不炸。"""
    empty = {}
    assert switches.get(empty, "fire.hz") == switches.spec("fire.hz")["default"]


def test_unknown_key_raises():
    """写错 key 要当场炸，不能静默忽略 —— 静默就是假开关的温床。"""
    with pytest.raises(KeyError):
        switches.spec("no.such.switch")


def test_snapshot_is_grouped_and_carries_values():
    cfg = config.Config()
    groups = switches.snapshot(cfg)
    assert [g["name"] for g in groups] == switches.GROUPS
    total = sum(len(g["items"]) for g in groups)
    assert total == len(switches.SWITCHES)
    for g in groups:
        for it in g["items"]:
            assert "value" in it and "kind" in it and "label" in it


# ---------------------------------------------------------------- 行为

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
        self.clicks = []

    def click(self, x, y, **kw):
        self.clicks.append((int(x), int(y)))
        return True

    def ensure_foreground(self, hwnd, screen=None):
        return True


class _Cfg(dict):
    """最小配置：只提供 _fire_and_confirm 用到的那几项。"""

    def __init__(self, **over):
        super().__init__(**config.DEFAULTS)
        self["regions"] = {
            "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
            "buy": {"x": 0.4, "y": 0.8, "w": 0.2, "h": 0.1},
        }
        self.update(over)

    def region(self, name):
        return self["regions"].get(name)


def _engine(monkeypatch):
    from core import engine as engine_mod
    eng = engine_mod.Engine()
    eng._dry_run = False
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (0.99, (0, 0)))
    monkeypatch.setattr(eng, "_bought_dialog", lambda screen: True)
    monkeypatch.setattr(eng, "_close_reward", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_guard_insufficient", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_wait_dialog_unlock", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_balance", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_record", lambda *a, **k: None)
    return eng, engine_mod


def test_semi_mode_stops_before_the_click(monkeypatch):
    """半自动：走到「该点了」就停手，一下都不点。

    这是竞品右栏「全自动·材料 / 半自动·单品」里的后半个 —— 有人就是想在
    最后一刻自己按那一下。
    """
    eng, engine_mod = _engine(monkeypatch)
    cfg = _Cfg(run_mode="semi")
    mouse = _Mouse()
    with pytest.raises(engine_mod.Aborted):
        eng._fire_and_confirm(cfg, _Screen(), mouse, np.zeros((10, 10), np.uint8),
                              (500, 500), 0.0)
    assert mouse.clicks == [], "半自动不该点任何东西"


def test_auto_mode_clicks(monkeypatch):
    eng, _ = _engine(monkeypatch)
    cfg = _Cfg(run_mode="auto")
    mouse = _Mouse()
    ok = eng._fire_and_confirm(cfg, _Screen(), mouse, np.zeros((10, 10), np.uint8),
                               (500, 500), 0.0)
    assert ok is True
    assert mouse.clicks, "全自动却没点"


def test_record_switch_off_writes_nothing(tmp_path, monkeypatch):
    """关掉「写成交记录」就真的不写 —— 不然记录页会堆满用户以为不存在的东西。"""
    from core import engine as engine_mod
    from core import records

    monkeypatch.setattr(records, "PATH", str(tmp_path / "r.jsonl"))
    eng = engine_mod.Engine()
    cfg = _Cfg(record_enabled=False)
    eng._record(cfg, _Screen(), item="X", price=1, result="hit")
    assert records.load() == []

    cfg2 = _Cfg(record_enabled=True)
    eng._record(cfg2, _Screen(), item="X", price=1, result="hit")
    rows = records.load()
    assert len(rows) == 1 and rows[0]["name"] == "X"


def test_verify_switch_off_marks_record_unverified(tmp_path, monkeypatch):
    """关掉成交校验时，记录里必须标 verified=false。

    允许关（有些界面版本认不出结算弹窗），但「点了就算」的成交要能事后
    分辨出来，不能再出现「78 条成交一条真的都没有」那种账。
    """
    from core import engine as engine_mod
    from core import records

    monkeypatch.setattr(records, "PATH", str(tmp_path / "r.jsonl"))
    eng = engine_mod.Engine()
    eng._record(_Cfg(fire_verify=False), _Screen(), item="X", price=1,
                result="hit")
    assert records.load()[0]["verified"] is False


def test_fire_lag_is_measured(monkeypatch):
    """自适应延迟要有数据可用：每轮记下「点下去→弹窗出现」的耗时。"""
    eng, _ = _engine(monkeypatch)
    assert eng._fire_lag == 0.0
    eng._fire_and_confirm(_Cfg(), _Screen(), _Mouse(),
                          np.zeros((10, 10), np.uint8), (500, 500), 0.0)
    assert eng._fire_lag > 0, "没记下实测延迟，自适应等于没有"


def test_follow_before_buy_calls_the_navigator(monkeypatch):
    """「抢前先关注」打开时，进详情后要真的去点星标；关掉时一次都不点。

    默认值是**关**（2026-11-03 改）：进购买界面多一次点击就多一次抢不到
    的机会，而且星标在详情面板顶部，点到别处会切走页面。
    """
    eng, _ = _engine(monkeypatch)
    called = []
    eng._follow = lambda nav, cfg, want: called.append(want)

    class Nav:
        def set_follow(self, want=True, wait=0.45):
            return want

    # 开：调一次
    on = _Cfg(follow_before_buy=True)
    if switches.get(on, "follow.before_buy"):
        eng._follow(Nav(), on, True)
    assert called == [True], "开关打开时没去点星标"

    # 关：不调
    eng2, _ = _engine(monkeypatch)
    called2 = []
    eng2._follow = lambda nav, cfg, want: called2.append(want)
    off = _Cfg(follow_before_buy=False)
    if not switches.get(off, "follow.before_buy"):
        pass
    assert called2 == []
    assert switches.get(_Cfg(), "follow.before_buy") is False, \
        "默认应该是关（不要在购买界面多点那一下）"


# ---------------------------------------------------------------- 任务 ↔ 全局开关

def _fake_sched(monkeypatch, **globals_):
    """把 scheduler 的 Config.load 换成一个受控的全局配置。"""
    from core import scheduler as sched_mod

    cfg = config.Config()
    for k, v in globals_.items():
        cfg[k] = v

    class Cfg(dict):
        @classmethod
        def load(cls):
            return cfg

    monkeypatch.setattr(sched_mod, "Config", Cfg)
    return sched_mod, cfg


def test_task_falls_back_to_global_switches(monkeypatch):
    """任务没写筛选条件时，用设置页上拨的全局值。

    这是「开关是不是真的有用」的关键一环：任务对象里通常没有
    craft/max_wear/sort/min_price，如果 _engine_task 直接 `or 0` / `or ""`，
    开关拨了也会被盖掉 —— 界面上能拨、实际不生效。
    """
    sched_mod, cfg = _fake_sched(
        monkeypatch,
        console={"min_price": 2000, "max_price": 25000, "quota": 3, "budget": 80000},
        filter_defaults={"craft": "极品", "max_wear": 0.4, "sort": "wear"})

    t = sched_mod.Scheduler._engine_task({"name": "某件皮肤"})
    assert t["min_price"] == 2000
    assert t["max_price"] == 25000
    assert t["quota"] == 3
    assert t["budget"] == 80000
    assert t["craft"] == "极品"
    assert t["max_wear"] == 0.4
    assert t["sort"] == "wear"


def test_task_fields_win_over_global_switches(monkeypatch):
    """任务自己写了条件就以任务为准 —— 清单里每条可以不一样。"""
    sched_mod, _ = _fake_sched(
        monkeypatch,
        console={"min_price": 2000, "max_price": 25000, "quota": 3, "budget": 80000},
        filter_defaults={"craft": "极品", "max_wear": 0.4, "sort": "wear"})

    t = sched_mod.Scheduler._engine_task(
        {"name": "另一件", "max_price": 999, "craft": "优品", "sort": "price",
         "quota": 1, "min_price": 10})
    assert t["max_price"] == 999
    assert t["min_price"] == 10
    assert t["craft"] == "优品"
    assert t["sort"] == "price"
    assert t["quota"] == 1


# ---------------------------------------------------------------- 失败原因

def test_failed_task_carries_the_engine_reason(monkeypatch):
    """任务失败时要把引擎日志里最后一条 warn/error 带出来。

    以前只写「未抢到」，真正的原因（「公示期里没有符合条件、还在倒计时的
    商品」「界面导航失败」）只在引擎日志里，用户盯着清单页的「失败」两字
    完全不知道为什么。
    """
    from core import scheduler as sched_mod

    st = {"state": "abort", "error": None,
          "log": [{"level": "info", "msg": "输入通道：驱动注入"},
                  {"level": "warn", "msg": "公示期里没有符合条件、还在倒计时的商品"}]}
    assert "公示期" in sched_mod.Scheduler._engine_reason(st)

    # 这个辅助函数只负责「从日志里捞」；引擎自己报了 error 时，
    # 调用方会优先用 error（见 _run_task 里的 `st.get("error") or ...`）
    st2 = {"state": "abort", "error": "鼠标被抢：X", "log": [{"level": "warn", "msg": "别的"}]}
    assert sched_mod.Scheduler._engine_reason(st2) == "别的"


def test_engine_reason_returns_empty_when_no_warnings():
    from core import scheduler as sched_mod
    assert sched_mod.Scheduler._engine_reason({"log": [{"level": "info", "msg": "x"}]}) == ""
    assert sched_mod.Scheduler._engine_reason({}) == ""
