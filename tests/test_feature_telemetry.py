"""台账补两列（offset_ms / tier）的回归测试。

需求：
  · offset_ms = 本次「第一下点击」时刻 − 反推的理论零点（归零=解锁），
    毫秒整数，可正可负；没有可用零点必须写 null，**不许写 0**（0 会被
    统计成「完美卡点」）。
  · tier = 项目既有判阶结果（bricks.find_skin 的 tier）；判不出来写
    「未标注」，不默认成某一档。
  · 只追加字段，不重排/改名/不动已有字段；遥测按这两个名字读。
  · 既有消费方（统计、前端、导出）遇到多出来的字段不能报错。
"""
import copy
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bricks
from core import engine as engine_mod
from core import records
from core import switches
from core.config import DEFAULTS


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


def _spy_record(eng, monkeypatch):
    rows = []
    monkeypatch.setattr(engine_mod.records, "add", lambda **kw: rows.append(kw))
    monkeypatch.setattr(eng, "_save_shot", lambda *a, **k: "")
    return rows


def _quiet(eng, monkeypatch, fire_timeout=0.0):
    monkeypatch.setattr(engine_mod, "match", lambda *a, **k: (0.0, (0, 0)))
    monkeypatch.setattr(engine_mod.time, "sleep", lambda *a, **k: None)
    real_get = switches.get
    monkeypatch.setattr(
        engine_mod.switches, "get",
        lambda cfg, key: (fire_timeout if key == "fire.timeout"
                          else real_get(cfg, key)))
    eng._insufficient_showing = lambda s: False
    eng._guard_insufficient = lambda *a, **k: None
    eng._wait_dialog_unlock = lambda *a, **k: True
    eng._balance = lambda *a, **k: None
    eng._close_reward = lambda *a, **k: None
    eng._say = lambda *a, **k: None


@pytest.fixture(autouse=True)
def _clean_records_cache(tmp_path, monkeypatch):
    """每个用例用独立台账文件，且清掉模块级统计缓存，避免互相污染。"""
    path = os.path.join(str(tmp_path), "records.jsonl")
    monkeypatch.setattr(records, "PATH", path)
    records._cache["acc"] = None
    records._cache["sig"] = None
    yield
    records._cache["acc"] = None
    records._cache["sig"] = None


def _engine():
    return engine_mod.Engine()


def _record_row(eng, cfg, item="", result="hit"):
    """跑真实 _record + 真实 records.add，返回落盘的那一行（dict）。"""
    real_add = records.add
    got = {}

    def spy(**kw):
        row = real_add(**kw)
        got["row"] = row
        return row

    records.add = spy
    try:
        eng._save_shot = lambda *a, **k: ""
        eng._record(cfg, _Screen(), item=item, price=100.0,
                    elapsed=0.5, result=result)
    finally:
        records.add = real_add
    assert got.get("row") is not None, "这一行没有写进台账"
    return got["row"]


# ---------------------------------------------------------------- offset_ms

@pytest.mark.parametrize("click,zero,expect",
                         [(100.250, 100.000, 250),
                          (99.500, 100.000, -500),
                          (100.000, 100.000, 0)])
def test_offset_ms_is_click_minus_zero_in_ms(click, zero, expect):
    """正常成交：offset_ms 精确等于（点击 − 零点）×1000 的整数。"""
    eng = _engine()
    eng._click_perf = click
    eng._zero_perf = zero
    eng._zero_primed = True
    row = _record_row(eng, _cfg(), item="")
    assert row["offset_ms"] == expect
    assert isinstance(row["offset_ms"], int)
    # 消费后就清掉，不能串到下一次购买
    assert eng._click_perf is None and eng._zero_perf is None


def test_offset_ms_null_when_zero_unavailable():
    """零点不可用（这一版没有倒计时反推结果）→ 必须 null，绝不是 0。"""
    eng = _engine()
    eng._click_perf = 100.250
    eng._zero_perf = None
    eng._zero_primed = False
    row = _record_row(eng, _cfg(), item="")
    assert row["offset_ms"] is None
    assert row["offset_ms"] != 0
    # JSON 里落的是 null，不是 0
    assert json.loads(json.dumps(row, ensure_ascii=False))["offset_ms"] is None


def test_offset_ms_null_when_no_click():
    """一下都没点（半自动/超时前中止）→ 没有点击时刻，写 null。"""
    eng = _engine()
    eng._click_perf = None
    eng._zero_perf = 100.0
    eng._zero_primed = True
    row = _record_row(eng, _cfg(), item="", result="miss")
    assert row["offset_ms"] is None


# ---------------------------------------------------------------- tier

def test_tier_uses_existing_catalog_grade():
    """tier 用项目既有判阶结果（bricks.find_skin 的 tier），不做二次猜测。"""
    entry = bricks.catalog()[0]
    eng = _engine()
    row = _record_row(eng, _cfg(), item=entry["name"])
    assert row["tier"] == entry["tier"]
    assert row["tier"] in ("传说", "史诗", "稀有", "普通")


@pytest.mark.parametrize("broken", ["none", "raise"])
def test_tier_falls_back_to_unannotated(monkeypatch, broken):
    """判不出来 → 「未标注」，不许默认成某一档。"""
    real = bricks.find_skin
    if broken == "none":
        monkeypatch.setattr(bricks, "find_skin", lambda *a, **k: None)
    else:
        def boom(*a, **k):
            raise RuntimeError("目录坏了")
        monkeypatch.setattr(bricks, "find_skin", boom)
    try:
        eng = _engine()
        row = _record_row(eng, _cfg(), item="完全不认识的商品名 XYZ")
    finally:
        monkeypatch.setattr(bricks, "find_skin", real)
    assert row["tier"] == "未标注"


def test_tier_unannotated_for_empty_name():
    eng = _engine()
    row = _record_row(eng, _cfg(), item="")
    assert row["tier"] == "未标注"


# ---------------------------------------------------------------- 台账字段兼容

def test_new_fields_are_appended_at_row_end():
    """新列只追加在行尾：基础字段一个不少、名字不变、顺序不动。"""
    eng = _engine()
    eng._click_perf = 100.250
    eng._zero_perf = 100.000
    eng._zero_primed = True
    row = _record_row(eng, _cfg(), item="", result="miss")
    keys = list(row.keys())
    assert keys[:10] == ["ts", "t", "name", "price", "result", "elapsed",
                         "shot", "mode", "quota", "verified"]
    # 行尾契约一条锁死：offset_ms、tier 保持原有相对位置（都在 10 列之后），
    # 本特性的 10 列必须紧接着排在 tier 之后、顺序固定；再往后的列属于别的
    # 特性（records.add 的 settle 字段），不归这条断言管。
    tail = keys[keys.index("offset_ms"):]
    assert tail[:2] == ["offset_ms", "tier"]
    assert tail[2:12] == [
        "pool_joined_at", "join_result", "roll_self", "roll_max", "won",
        "listed_at", "publicity_s", "listed_at_err_s", "join_conflict",
        "won_source"]


def test_legacy_rows_without_new_fields_load_and_stats(tmp_path):
    """老台账（没有这两列）读进来不炸，统计不崩。"""
    legacy = [
        {"ts": 1700000000.0, "t": "11-14 22:13:20", "name": "老记录A",
         "price": 100.0, "result": "hit", "elapsed": 1.2, "shot": "",
         "mode": "detail", "quota": 1, "verified": True},
        {"ts": 1700000001.0, "t": "11-14 22:13:21", "name": "老记录B",
         "price": None, "result": "miss", "elapsed": 8.0, "shot": "",
         "mode": "detail", "quota": 1, "verified": True},
    ]
    with open(records.PATH, "w", encoding="utf-8") as f:
        for r in legacy:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    rows = records.load(limit=0)
    assert len(rows) == len(legacy)
    st = records.stats()
    assert st["tries"] == 2 and st["hits"] == 1 and st["miss"] == 1
    assert st["verified_hits"] == 1
    # 新列不在老行里也不能被凭空补成 0
    assert "offset_ms" not in rows[0] and "tier" not in rows[0]


def test_consumers_tolerate_extra_fields(tmp_path):
    """统计 / 导出（JSON）/ 前端字段访问遇到多出来的列都不报错。"""
    eng = _engine()
    eng._click_perf = 100.250
    eng._zero_perf = 100.000
    eng._zero_primed = True
    entry = bricks.catalog()[0]
    row = _record_row(eng, _cfg(), item=entry["name"])

    rows = records.load(limit=0)
    assert len(rows) == 1
    assert rows[0]["offset_ms"] == 250 and rows[0]["tier"] == entry["tier"]

    # 导出：整个 /api/records 回包要能 JSON 序列化
    payload = {"ok": True, "rows": rows, "stats": records.stats()}
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    again = json.loads(text)
    assert again["ok"] is True

    # 统计只认既有字段，多出来的列不影响数字
    st = records.stats(rows=rows)
    assert st["tries"] == 1 and st["hits"] == 1 and st["spent"] == 100.0

    # 前端记录页只读这些既有字段；新增列在场时照常取到值
    for r in again["rows"]:
        assert r.get("name") == entry["name"]
        assert r.get("result") == "hit"
        assert r.get("verified") is True
        assert r.get("ts") is not None


# ---------------------------------------------------------------- 引擎接线

def test_fire_confirm_records_click_offset_end_to_end(monkeypatch):
    """倒计时路线（有零点）跑一次开火到 miss：台账带上真实的 offset_ms。"""
    eng = _engine()
    rows = _spy_record(eng, monkeypatch)
    _quiet(eng, monkeypatch, fire_timeout=0.02)
    eng._zero_perf = 0.0
    eng._zero_primed = True

    clock = {"t": 0.0}

    def fake_pc():
        clock["t"] += 0.001
        return clock["t"]

    monkeypatch.setattr(engine_mod.time, "perf_counter", fake_pc)
    monkeypatch.setattr(eng, "_click", lambda m, x, y, **kw: True)

    got = eng._fire_and_confirm(_cfg(), _Screen(), _Mouse(),
                                np.zeros((10, 10), np.uint8), (500, 500),
                                0.0)
    assert got is False
    assert len(rows) == 1 and rows[0]["result"] == "miss"
    off = rows[0]["extra"]["offset_ms"]
    assert isinstance(off, int) and off > 0, "有零点、点过一下却没算出偏差"
    assert eng._click_perf is None and eng._zero_perf is None


def test_fire_confirm_drops_stale_zero_on_other_routes(monkeypatch):
    """详情/列表路线没有零点：上一轮残留下来的零点必须清掉 → null。"""
    eng = _engine()
    rows = _spy_record(eng, monkeypatch)
    _quiet(eng, monkeypatch, fire_timeout=0.02)
    eng._zero_perf = 5.0          # 上一轮留下的（没被 primed 标记）
    eng._zero_primed = False

    clock = {"t": 100.0}

    def fake_pc():
        clock["t"] += 0.001
        return clock["t"]

    monkeypatch.setattr(engine_mod.time, "perf_counter", fake_pc)
    monkeypatch.setattr(eng, "_click", lambda m, x, y, **kw: True)

    got = eng._fire_and_confirm(_cfg(), _Screen(), _Mouse(),
                                np.zeros((10, 10), np.uint8), (500, 500),
                                100.0)
    assert got is False
    assert rows[0]["extra"]["offset_ms"] is None
