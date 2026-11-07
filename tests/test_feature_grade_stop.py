"""按品阶分别计数停止（stop.by.grade）的回归测试。

契约：`_feature_contract.md`
  §2.5  legend=传说 / epic=紫(史诗) / rare=蓝(稀有)；0 = 不限；普通不计数；
        品阶读不出不计数也不拦；scope=all 整轮停 / scope=grade 只停该品阶；
        计数只在「校验成交」之后 +1。
  D1    单档达标 / 多档同时达标 / 全 0 不限 / 未知品阶不计数。
  D2    控件 → config.json → 运行时读取，三段都有断言。
  D3    负数、100、NaN、字符串、1.5、缺键、scope 传不存在的枚举。
  D4    越界拒收，盘上字节不变。
  D5.7  与 quota / budget 共存时先触发哪个以哪个为准（grade > quota；budget 独立）。
  §4.5  引擎只允许在指定扩展点追加，本文件只测这些扩展点的行为。
  §5    共享脚本区归合并负责人，本分支不写 index.html；前端只做无头渲染断言
        （snapshot → numHtml/selectHtml → data-sw 前缀归组）。

判阶用真实目录（core/bricks.json）里各品阶的皮肤名，不 mock 分类表。
"""
import copy
import json
import os
import re
import subprocess
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config
from core import engine as eng_mod
from core import stop_grade
from core import switches
from core.config import DEFAULTS

LEGEND_TXT = "M4A1突击步枪·棱镜攻势 极品 成色A 价格2050"
EPIC_TXT = "ASH-12战斗步枪·飓风行动 极品 成色A 价格2050"
RARE_TXT = "AK-12突击步枪·阿萨拉卫队 极品 成色A 价格2050"
NORMAL_TXT = "AK-12突击步枪·水墨迷彩 极品 成色A 价格2050"
UNKNOWN_TXT = "神秘卡面 没有点名的皮肤 价格2000"

KEYS = {
    "legend": "stop.by_grade.legend",
    "epic": "stop.by_grade.epic",
    "rare": "stop.by_grade.rare",
    "scope": "stop.by_grade.scope",
}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_INDEX = os.path.join(ROOT, "web", "index.html")


def _grade(**over):
    """本功能完整设置；测试默认把总开关打开，只测三档逻辑本身。"""
    g = {"enabled": True, "legend": 0, "epic": 0, "rare": 0, "scope": "all"}
    g.update(over)
    return g


# ---------------------------------------------------------------- 假环境

def _row(x0, y0, text, price=2050.0):
    return {"text": text, "price": float(price), "craft": None, "wear": None,
            "wear_value": None, "x0": float(x0), "y0": float(y0),
            "cy": float(y0) + 20.0}


class FakeCfg(dict):
    def __init__(self, **kw):
        super().__init__({
            "threshold": 0.8, "failsafe": False, "click_hz": 50,
            "fire_timeout_s": 0.2, "confirm_clicks": 1,
            "confirm_interval_ms": 0, "list_scan_interval": 0.0,
            "list_scan_timeout": 5.0, "list_detail_timeout": 0.2,
            "list_max_tries": 50,
            "regions": {"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                        "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1},
                        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.1}},
            "active_task": {"min_price": 1000, "max_price": 3000,
                            "sort": "order", "quota": 1},
            "stop": {"by_grade": _grade()},
        })
        self.update(kw)

    def region(self, name):
        return self["regions"].get(name)

    def has_template(self, name):
        return True


class FakeScreen:
    size = (1920, 1080)

    def norm_to_px(self, box):
        return (60, 210, 1340, 730)

    def grab(self, px=None):
        return np.zeros((8, 8, 3), np.uint8)

    def close(self):
        pass


class FakeNav:
    made = []
    page = ("onsale", 0.9)
    can_switch = True
    price_returns = None

    def __init__(self, *a, **k):
        FakeNav.made.append(self)
        self.calls = []

    def where(self):
        return FakeNav.page

    def to_onsale(self):
        self.calls.append(("to_onsale",))
        FakeNav.page = ("onsale", 0.9)
        return FakeNav.can_switch

    def pub_filter(self, want):
        self.calls.append(("pub_filter", want))

    def ons_refresh(self):
        self.calls.append(("refresh",))

    def ons_filter(self):
        self.calls.append(("filter",))

    def ons_price(self, lo=None, hi=None):
        self.calls.append(("price", lo, hi))
        return (lo, hi)

    def ons_checks(self, names, want=True):
        self.calls.append(("checks", tuple(names)))
        return list(names)

    def ons_filter_confirm(self):
        self.calls.append(("confirm",))


class FakeMouse:
    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True


def _run(monkeypatch, cfg, rows):
    """跑一次 _execute_list；成交走真实的 `_record`（计数就在这里 +1）。"""
    e = eng_mod.Engine()
    e._stop = threading.Event()
    e._target_hwnd = 1
    e._said = []
    e._say = lambda msg, level="info": e._said.append((level, msg))
    e._failsafe = lambda on: None

    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan", lambda frame, **kw: list(rows))
    monkeypatch.setattr(eng_mod, "match", lambda a, b: (1.0, None))
    monkeypatch.setattr(e, "_load", staticmethod(lambda c, name: np.zeros((4, 4))))
    monkeypatch.setattr(e, "_save_shot", lambda *a, **k: "")
    clicks = []
    monkeypatch.setattr(e, "_click", lambda m, x, y: clicks.append((x, y)))

    ledger = []
    monkeypatch.setattr(eng_mod.records, "add", lambda **kw: ledger.append(kw))

    def fire(screen, mouse, *a, **k):
        item = k.get("item") or ""
        # 校验成交 → 引擎真实入账路径（品阶计数只在这里发生）
        e._record(cfg, screen, item=item, result="hit")
        return True

    monkeypatch.setattr(e, "_fire_and_confirm", fire)

    def go():
        e._execute_list(cfg, FakeScreen(), FakeMouse(), None)

    return e, clicks, ledger, go


# ---------------------------------------------------------------- D1 功能

def test_single_grade_target_stops_the_whole_run(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 9},
                  stop={"by_grade": _grade(legend=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, LEGEND_TXT)])
    with pytest.raises(eng_mod.Aborted) as ei:
        go()
    assert "传说" in str(ei.value) and "整轮停止" in str(ei.value)
    assert len(clicks) == 1
    assert e.status()["stats"]["grade_counts"]["legend"] == 1
    assert e.status()["stats"]["stop_reason"] == "grade"
    assert ledger[-1]["extra"]["stop_reason"] == "grade"
    assert ledger[-1]["extra"]["grade"] == "legend"


def test_scope_grade_only_skips_that_grade_and_keeps_buying(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 2},
                  stop={"by_grade": _grade(legend=1, scope="grade")})
    rows = [_row(40, 10, LEGEND_TXT), _row(700, 10, EPIC_TXT),
            _row(40, 220, RARE_TXT)]
    e, clicks, ledger, go = _run(monkeypatch, cfg, rows)
    go()
    assert e._state == eng_mod.DONE
    assert len(clicks) == 2, "传说只买 1 件，紫继续买"
    assert clicks[1][0] > clicks[0][0], "第二次应买本来排后面的紫，不是再买传说"
    counts = e.status()["stats"]["grade_counts"]
    assert counts == {"legend": 1, "epic": 1, "rare": 0}
    assert e.status()["stats"]["stop_reason"] == "quota"


def test_all_zero_means_unlimited_and_counts_still_update(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 3},
                  stop={"by_grade": _grade()})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, LEGEND_TXT)])
    go()
    assert e._state == eng_mod.DONE
    assert len(clicks) == 3
    assert e.status()["stats"]["grade_counts"]["legend"] == 3
    assert e.status()["stats"]["stop_reason"] == "quota"


def test_multiple_grade_targets_reached_at_once():
    """计数到位的判断是「一次性列出全部达标档」，不依赖触发顺序。"""
    s = {**_grade(legend=1, epic=2, rare=3), "scope": "all"}
    c = stop_grade.Counter()
    for _ in range(1):
        c.record("legend")
    for _ in range(2):
        c.record("epic")
    for _ in range(3):
        c.record("rare")
    hits = stop_grade.reached(s, c.snapshot())
    assert hits == ["legend", "epic", "rare"]
    txt = stop_grade.detail(s, c.snapshot(), hits)
    assert "传说 1/1" in txt and "紫 2/2" in txt and "蓝 3/3" in txt


def test_unknown_grade_is_never_counted_and_logged(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 1},
                  stop={"by_grade": _grade(rare=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, UNKNOWN_TXT)])
    go()
    assert e.status()["stats"]["grade_counts"] == {"legend": 0, "epic": 0, "rare": 0}
    assert ledger[-1]["extra"]["grade"] == "unknown"
    assert any("读不出品阶" in m for _lv, m in e._said), "未知品阶要留一条日志"
    assert e._state == eng_mod.DONE, "读不出品阶不该被拦，也不该被猜成蓝"


def test_normal_tier_does_not_count(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 1},
                  stop={"by_grade": _grade(rare=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, NORMAL_TXT)])
    go()
    assert e.status()["stats"]["grade_counts"]["rare"] == 0
    assert ledger[-1]["extra"]["grade"] == "other"


def test_real_catalog_grade_mapping():
    """判阶沿用项目目录；普通不是蓝，读不出不是蓝。"""
    assert stop_grade.grade_key(LEGEND_TXT) == "legend"
    assert stop_grade.grade_key(EPIC_TXT) == "epic"
    assert stop_grade.grade_key(RARE_TXT) == "rare"
    assert stop_grade.grade_key(NORMAL_TXT) == "other"
    assert stop_grade.grade_key(UNKNOWN_TXT) == "unknown"
    assert stop_grade.grade_key("") == "unknown"


# ---------------------------------------------------------------- D2 三段联通

def test_switch_table_exposes_controls_inside_fire_group():
    groups = {g["name"]: g for g in switches.snapshot(FakeCfg())}
    items = {i["key"]: i for i in groups["抢购设置"]["items"]}
    enabled = items["stop.by_grade.enabled"]
    assert enabled["kind"] == "bool"
    assert enabled["default"] is False
    assert enabled.get("strict") is True
    assert "不计数" in enabled["desc"] and "保留" in enabled["desc"]
    for k in ("stop.by_grade.legend", "stop.by_grade.epic", "stop.by_grade.rare"):
        assert items[k]["kind"] == "int"
        assert items[k]["default"] == 0
        assert items[k]["integral"] is True
    scope = items["stop.by_grade.scope"]
    assert scope["kind"] == "select"
    assert scope["options"] == [["all", "整轮停止"], ["grade", "只停该品阶"]]
    assert "整轮停止" in scope["desc"] and "只停该品阶" in scope["desc"]


def test_defaults_match_switch_table_and_zero_unlimited():
    assert DEFAULTS["stop"]["by_grade"] == {
        "enabled": False, "legend": 0, "epic": 0, "rare": 0, "scope": "all"}
    # 配置里没写 enabled（老配置文件）时按默认「关」处理
    s = stop_grade.settings(FakeCfg(stop={"by_grade": {
        "legend": 0, "epic": 0, "rare": 0, "scope": "all"}}))
    assert s == {"enabled": False, "legend": 0, "epic": 0,
                 "rare": 0, "scope": "all"}


def test_value_flows_control_to_disk_to_runtime(tmp_path, monkeypatch):
    """控件值 → config.json → 重新加载后的运行时读取，三段各有断言。"""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.Config.load()                    # 首启：默认值落盘
    assert json.loads(open(path, encoding="utf-8").read())["stop"]["by_grade"] == {
        "enabled": False, "legend": 0, "epic": 0, "rare": 0, "scope": "all"}

    switches.set(cfg, "stop.by_grade.enabled", True)   # 控件打开总开关
    switches.set(cfg, KEYS["legend"], 2)          # 控件送 2
    switches.set(cfg, KEYS["scope"], "grade")
    cfg.save()
    on_disk = json.loads(open(path, encoding="utf-8").read())["stop"]["by_grade"]
    assert on_disk["enabled"] is True
    assert on_disk["legend"] == 2 and on_disk["scope"] == "grade"

    again = config.Config.load()
    s = stop_grade.settings(again)
    assert s["enabled"] is True
    assert s["legend"] == 2 and s["scope"] == "grade"


def test_engine_reads_runtime_config_object():
    cfg = config.Config()
    cfg.data.setdefault("stop", {}).setdefault("by_grade", {}).update(
        {"legend": 3, "scope": "grade"})
    s = stop_grade.settings(cfg)
    assert s["legend"] == 3 and s["scope"] == "grade"


# ---------------------------------------------------------------- D3/D4 异常

@pytest.mark.parametrize("bad", ["abc", "", None, [], {}, float("nan"),
                                float("inf")])
def test_bad_numbers_rejected_with_readable_reason(bad):
    for k in ("legend", "epic", "rare"):
        cfg = FakeCfg()
        before = switches.get(cfg, KEYS[k])
        with pytest.raises(switches.SwitchValueError) as ei:
            switches.set(cfg, KEYS[k], bad)
        assert KEYS[k] in str(ei.value)
        assert switches.get(cfg, KEYS[k]) == before, "拒收不得落配置"


@pytest.mark.parametrize("bad", [-1, 100, 999, 1.5, -0.5])
def test_out_of_range_and_float_rejected_with_reason(bad):
    for k in ("legend", "epic", "rare"):
        cfg = FakeCfg()
        with pytest.raises(switches.SwitchValueError) as ei:
            switches.set(cfg, KEYS[k], bad)
        msg = str(ei.value)
        assert ("小于下限" in msg or "大于上限" in msg or "整数" in msg), msg
        assert switches.get(cfg, KEYS[k]) == 0


def test_bool_true_is_accepted_as_one():
    """布尔 true 当 1 送进来可以接受（契约 D3 只卡浮点/字符串/越界）。"""
    cfg = FakeCfg()
    assert switches.set(cfg, KEYS["legend"], True) == 1
    assert switches.get(cfg, KEYS["legend"]) == 1


# ------------------------------------------------------- 总开关 enabled

def _off_cfg(**kw):
    """不带 enabled 的配置：老配置文件/首启时的默认形态（总开关缺省 = 关）。"""
    return FakeCfg(stop={"by_grade": {"legend": 0, "epic": 0, "rare": 0,
                                      "scope": "all"}}, **kw)


def test_enabled_defaults_off_and_strict():
    cfg = _off_cfg()
    assert switches.get(cfg, "stop.by_grade.enabled") is False, "默认关闭"
    assert stop_grade.enabled(stop_grade.settings(cfg)) is False
    assert switches.set(cfg, "stop.by_grade.enabled", True) is True
    assert switches.set(cfg, "stop.by_grade.enabled", "off") is False


@pytest.mark.parametrize("bad", ["maybe", 2, "2", None, [], {}, float("nan")])
def test_enabled_rejects_non_bool_with_readable_reason(bad):
    cfg = _off_cfg()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, "stop.by_grade.enabled", bad)
    assert "stop.by_grade.enabled" in str(ei.value)
    assert "不是布尔值" in str(ei.value)
    assert switches.get(cfg, "stop.by_grade.enabled") is False


def test_enabled_off_ignores_nonzero_targets_completely(monkeypatch):
    """关掉时即使填了目标也不计数、不停止、台账不带品阶字段。"""
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 3},
                  stop={"by_grade": _grade(enabled=False, legend=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, LEGEND_TXT)])
    go()
    assert e._state == eng_mod.DONE, "关掉时不许按品阶停"
    assert len(clicks) == 3
    zero = {"legend": 0, "epic": 0, "rare": 0}
    assert e.status()["stats"].get("grade_counts") in (None, zero)
    assert e.status()["stats"]["stop_reason"] == "quota", "停止原因只由总量给出"
    assert all("grade" not in r["extra"] and "grade_counts" not in r["extra"]
               for r in ledger), "关掉时台账不写品阶字段"
    assert not any("品阶" in m for _lv, m in e._said), "关掉时连品阶日志都不该有"


def test_enabled_on_but_all_zero_counts_without_stopping(monkeypatch):
    """打开但三档全 0：计数照旧更新，只是永远不达标。"""
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 3},
                  stop={"by_grade": _grade(enabled=True)})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, LEGEND_TXT)])
    go()
    assert e._state == eng_mod.DONE
    assert len(clicks) == 3
    assert e.status()["stats"]["grade_counts"]["legend"] == 3
    assert e.status()["stats"]["stop_reason"] == "quota"
    assert e.status()["stats"]["grade_counts"] == {"legend": 3, "epic": 0, "rare": 0}


def test_enabled_on_and_target_reached_stops(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 9},
                  stop={"by_grade": _grade(enabled=True, epic=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(700, 10, EPIC_TXT)])
    with pytest.raises(eng_mod.Aborted) as ei:
        go()
    assert "紫" in str(ei.value) and "整轮停止" in str(ei.value)
    assert e.status()["stats"]["grade_counts"]["epic"] == 1


def test_toggle_off_then_on_keeps_filled_targets(tmp_path, monkeypatch):
    """关掉再打开：三个目标件数原样保留（不清空、不回 0）。"""
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.Config.load()
    switches.set(cfg, "stop.by_grade.enabled", True)
    for k, v in (("legend", 2), ("epic", 3), ("rare", 5)):
        switches.set(cfg, KEYS[k], v)
    cfg.save()

    switches.set(cfg, "stop.by_grade.enabled", False)
    cfg.save()
    off = json.loads(open(path, encoding="utf-8").read())["stop"]["by_grade"]
    assert off["enabled"] is False
    assert (off["legend"], off["epic"], off["rare"]) == (2, 3, 5)

    back = config.Config.load()
    switches.set(back, "stop.by_grade.enabled", True)
    back.save()
    on = config.Config.load()
    s = stop_grade.settings(on)
    assert s["enabled"] is True
    assert (s["legend"], s["epic"], s["rare"]) == (2, 3, 5), "重新打开还是上次填的值"


def test_unknown_scope_value_rejected_and_key_missing_uses_default():
    cfg = FakeCfg()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, KEYS["scope"], "stop_all")      # 旧枚举，不存在
    assert "不在可选值" in str(ei.value)
    assert switches.get(cfg, KEYS["scope"]) == "all"

    del cfg["stop"]
    s = stop_grade.settings(cfg)
    assert s["enabled"] is False, "缺节按默认关，不能默认打开"
    assert s["legend"] == 0 and s["scope"] == "all"


def test_batch_is_atomic_when_one_value_is_bad():
    cfg = FakeCfg()
    with pytest.raises(switches.SwitchValueError):
        switches.apply_all(cfg, {KEYS["legend"]: 2, KEYS["rare"]: -1})
    assert switches.get(cfg, KEYS["legend"]) == 0, "整批拒收，合法项也不许落"


def test_disk_stays_byte_identical_after_rejection(tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    config.Config.load()
    config.Config.load().save()
    before = open(path, encoding="utf-8").read()
    with pytest.raises(switches.SwitchValueError):
        with config.Config.transaction() as tx:
            switches.set(tx, KEYS["legend"], 100)
    assert open(path, encoding="utf-8").read() == before


# ---------------------------------------------------------------- D5.7 共存

def test_grade_stop_wins_over_quota_when_both_hit(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 1},
                  stop={"by_grade": _grade(legend=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, LEGEND_TXT)])
    with pytest.raises(eng_mod.Aborted):
        go()
    assert e.status()["stats"]["stop_reason"] == "grade", "品阶优先，原因可查"
    assert ledger[-1]["extra"]["stop_reason"] == "grade"


def test_quota_still_stops_after_grade_scope_keeps_other_grades(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 3},
                  stop={"by_grade": _grade(legend=1, epic=1, scope="grade")})
    rows = [_row(40, 10, LEGEND_TXT), _row(700, 10, EPIC_TXT),
            _row(40, 220, RARE_TXT)]
    e, clicks, ledger, go = _run(monkeypatch, cfg, rows)
    go()
    assert len(clicks) == 3
    assert e.status()["stats"]["grade_counts"] == {"legend": 1, "epic": 1, "rare": 1}
    assert e.status()["stats"]["stop_reason"] == "quota"
    assert any(r["extra"].get("stop_reason") == "quota" for r in ledger)


def test_budget_stop_is_labelled_and_ledgered(monkeypatch):
    cfg = FakeCfg(active_task={"min_price": 1000, "max_price": 3000,
                               "sort": "order", "quota": 9, "budget": 100},
                  stop={"by_grade": _grade(legend=1, scope="all")})
    e, clicks, ledger, go = _run(monkeypatch, cfg, [_row(40, 10, LEGEND_TXT)])
    go()
    assert clicks == [], "预算不够，一件都不点"
    assert e.status()["stats"]["stop_reason"] == "budget"
    assert any(r["result"] == "over_budget"
               and r["extra"]["stop_reason"] == "budget" for r in ledger)


# ---------------------------------------------------------------- 前端无头

def _js_func(src, name):
    i = src.index("function " + name)
    depth, j = 0, src.index("{", i)
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1


def test_frontend_rows_render_via_shared_templates_headless(tmp_path):
    src = open(WEB_INDEX, encoding="utf-8").read()
    assert 'id="swFire"' in src, "抢购设置挂载点不见了"
    assert "swFire: '抢购设置'" in src, "挂载映射里的分组名变了"
    assert "'stop.by_grade.', 'blk-fire-grade'" in src, "共享分组脚本没认这一档"
    assert "data-sw" in src and "data-kind" in src, "控件不是数据驱动绑定"

    items = [i for g in switches.snapshot(_off_cfg())
             if g["name"] == "抢购设置"
             for i in g["items"] if i["key"].startswith("stop.by_grade.")]
    assert len(items) == 5, "一个总开关 + 三个数字框 + 一个下拉"

    num_html = _js_func(src, "numHtml")
    sel_html = _js_func(src, "selectHtml")
    sw_html = _js_func(src, "switchHtml")
    script = "\n".join([
        "const esc = (v) => String(v == null ? '' : v)"
        ".replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"
        "'\"':'&quot;',\"'\":'&#39;'}[c]));",
        num_html, sel_html, sw_html,
        "const items = JSON.parse(process.argv[2]);",
        "const html = items.map(it => it.kind === 'bool' ? switchHtml(it)"
        " : (it.kind === 'select' ? selectHtml(it) : numHtml(it)));",
        "console.log(JSON.stringify(html));",
    ])
    js = tmp_path / "render.js"
    js.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(js), json.dumps(items)],
                         capture_output=True, text=True, check=True)
    html = json.loads(out.stdout)

    nums = [h for h, it in zip(html, items) if it["kind"] == "int"]
    sel = [h for h, it in zip(html, items) if it["kind"] == "select"][0]
    boolean = [h for h, it in zip(html, items) if it["kind"] == "bool"][0]
    assert 'data-sw="stop.by_grade.enabled"' in boolean
    assert 'data-kind="bool"' in boolean and 'role="switch"' in boolean
    assert 'aria-checked="false"' in boolean, "默认关闭"
    assert 'type="number"' not in boolean, "总开关不是数字框"
    for h, it in zip(nums, [i for i in items if i["kind"] == "int"]):
        assert 'data-sw="%s"' % it["key"] in h and 'data-kind="int"' in h
        assert 'type="number"' in h and 'value="0"' in h
    assert 'data-sw="stop.by_grade.scope"' in sel
    assert ">整轮停止</option>" in sel and ">只停该品阶</option>" in sel
    assert ">all<" not in sel and ">grade<" not in sel, "下拉不许显示英文枚举"

    # 契约 §3.2：共享脚本按 data-sw 前缀归组 → section#blk-fire-grade
    matched = [h for h in html if 'data-sw="stop.by_grade.' in h]
    assert len(matched) == 5
    section = ('<section class="setsec" id="blk-fire-grade" '
               'data-feature="stop.by_grade">%s</section>' % "".join(matched))
    assert re.search(r'id="blk-fire-grade"', section)
    assert section.count("data-sw=") == 5


def test_frontend_off_switch_disables_targets_and_keeps_values(tmp_path):
    """总开关关掉：三个数字框置灰，但 value 不写、不清空；打开即恢复。"""
    src = open(WEB_INDEX, encoding="utf-8").read()
    assert "FEATURE-GRADE-UI BEGIN" in src and "FEATURE-GRADE-UI END" in src
    # 静态锚点必须在 #swFire 内，并带起止标记（渲染器重建后由共享脚本按 MAP 复用/创建）
    box = src[src.index('<div id="swFire">'):]
    box = box[:box.index("</div>")]
    assert '<!-- blk-fire-grade-start' in box
    assert '<!-- blk-fire-grade-end' in box
    assert 'id="blk-fire-grade"' in box
    assert 'data-feature="stop.by_grade"' in box
    ui = src[src.index("FEATURE-GRADE-UI BEGIN"):src.index("FEATURE-GRADE-UI END")]
    assert "stop.by_grade.enabled" in ui and "el.disabled = it.disabled" in ui
    assert "getElementById('blk-fire-grade')" in ui, "置灰脚本只操作本容器"
    assert "el.value" not in ui and "value =" not in ui, "置灰不许动输入框的值"

    fn = _js_func(src, "gradeUiState")
    script = fn + "\nconsole.log(JSON.stringify([gradeUiState(false), gradeUiState(true)]));"
    js = tmp_path / "grade_ui.js"
    js.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(js)], capture_output=True, text=True, check=True)
    off, on = json.loads(out.stdout)
    assert [x["key"] for x in off] == ["stop.by_grade.legend", "stop.by_grade.epic",
                                       "stop.by_grade.rare"]
    assert all(x["disabled"] for x in off), "关掉时三个数字框全部置灰"
    assert not any(x["disabled"] for x in on), "打开时恢复可编辑"
