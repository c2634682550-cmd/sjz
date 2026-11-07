"""自适应调优层的护栏 / 回滚 / kill switch / 极端输入测试（不 sleep、不弹窗）。"""

from __future__ import annotations

import copy
import random

import pytest

from core import autotune, switches

GRADE = "legend"
TS0 = 1_700_000_000.0


def make_rows(n, *, err=-40.0, hit_ratio=0.6, grade=GRADE, ts0=TS0,
              names=("A", "B", "C", "D", "E"), refresh=None, evidence=None,
              result=None):
    rows = []
    for i in range(n):
        if result is not None:
            outcome = result
        else:
            # 均匀穿插，避免「前 N 个成交、后面全失败」被 kill switch 误判
            bucket = max(0, min(10, int(round(hit_ratio * 10))))
            outcome = "hit" if (i % 10) < bucket else "miss"
        row = {
            "ts": ts0 + i,
            "card_hash": None,
            "name": names[i % len(names)],
            "grade": grade,
            "price": 1000.0,
            "result": outcome,
            "lead_err_ms": err,
            "refresh_count": refresh,
            "burst_mode": None,
            "burst_gap_ms": None,
            "burst_duration_ms": None,
            "reentry_delay_ms": None,
            "client": "pc-1.0",
            "skip_evidence": evidence,
            "source": "test",
            "_present": ["ts", "name", "grade", "result", "lead_err_ms"],
        }
        rows.append(row)
    return rows


def make_tuner(tmp_path, cfg=None, *, shadow=False, **kwargs):
    store = autotune.AutotuneStore(directory=str(tmp_path / "autotune"))
    tuner = autotune.AutoTuner(cfg if cfg is not None else {},
                               shadow=shadow, store=store, **kwargs)
    return tuner, store, tuner.cfg


def block(n=30, **kwargs):
    return autotune.summarize(make_rows(n, **kwargs))


# ---------------------------------------------------------------- 归一化


def test_normalize_aliases_and_units():
    row = autotune.normalize_row({
        "时间": "2025-01-01 20:00:00",
        "卡密哈希": "abc123",
        "商品名": "霜火之刃",
        "品阶": "传说",
        "成交价": "1200",
        "结果": "成交",
        "卡点误差毫秒": "-12.5",
    }, source="backend")
    assert row["ts"] is not None
    assert row["card_hash"] == "abc123"
    assert row["name"] == "霜火之刃"
    assert row["grade"] == "legend"
    assert row["price"] == 1200.0
    assert row["result"] == "hit"
    assert row["lead_err_ms"] == -12.5

    # 秒单位字段自动换算
    assert autotune.normalize_row({"卡点误差秒": "0.25"})["lead_err_ms"] == 250.0
    # 缺字段 / 脏值全部容忍
    assert autotune.normalize_row({"foo": "bar"})["result"] is None
    assert autotune.normalize_row({"卡点误差毫秒": "1e18"})["lead_err_ms"] is None
    assert autotune.normalize_row({"品阶": "???"})["grade"] == "unknown"


def test_summarize_rates_and_percentiles():
    rows = (make_rows(30, grade="legend", err=-10, hit_ratio=0.5)
            + make_rows(30, grade="epic", err=40, hit_ratio=0.2,
                        ts0=TS0 + 100))
    summary = autotune.summarize(rows, window_ms=33.0)
    legend = summary["by_grade"]["legend"]
    assert legend["attempts"] == 30
    assert legend["hits"] == 15
    assert legend["hit_rate"] == 0.5
    assert summary["lead_error"]["all"]["n"] == 60
    assert summary["lead_error"]["all"]["p50"] in (-10, 40) or \
        summary["lead_error"]["all"]["p50"] is not None
    assert summary["lead_error"]["all"]["out_of_window"] == 30  # epic 的 40ms
    assert summary["lead_error"]["by_grade"]["epic"]["p95"] == 40


def test_skip_unknown_not_counted_as_correct():
    rows = make_rows(10, result="skip", evidence=None)
    skip = autotune.summarize(rows)["skip_accuracy"]
    assert skip["confirmed_n"] == 0
    assert skip["unknown"] == 10
    assert skip["hit_accuracy"] is None
    assert skip["verdict"] == "unknown"


# ---------------------------------------------------------------- 护栏


def test_min_sample_guard_keeps_value(tmp_path):
    tuner, store, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    summary = block(20)  # < 30
    proposals = tuner.propose(summary, cfg)
    lead = [p for p in proposals if p["knob"] == "fire.delay"][0]
    assert lead["blocked"].startswith("sample")

    out = tuner.step(summary, cfg)
    assert out["applied"] == []
    assert switches.get(cfg, "fire.delay") == 100
    assert any(e["action"] == "withheld" for e in out["audit"])
    # 审计落盘了，但没有任何 change
    entries = store.audit_entries()
    assert entries and not any(e["action"] == "change" for e in entries)


def test_step_cap_is_20_percent(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    out = tuner.step(block(30, err=-40), cfg)
    assert len(out["applied"]) == 1
    assert switches.get(cfg, "fire.delay") == 120  # 100 + 20%
    assert out["applied"][0]["knob"] == "fire.delay"
    assert "p50 卡点误差 -40" in out["audit"][0]["text"]


def test_min_hits_between_blocks_second_change(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    first = tuner.step(block(30, err=-40), cfg)
    assert first["applied"]
    assert switches.get(cfg, "fire.delay") == 120

    # 新一批有 40 次尝试但只有 8 次成交（< 10）：护栏 G4 拦住
    thin = make_rows(40, err=-60, hit_ratio=0.2, ts0=TS0 + 100)
    out = tuner.step(autotune.summarize(thin), cfg)
    assert out["applied"] == []
    assert switches.get(cfg, "fire.delay") == 120
    notes = [e["note"] for e in out["audit"] if e["action"] == "withheld"]
    assert any("不足 10" in n for n in notes)


def test_hard_bound_stops_and_records(tmp_path):
    tuner, store, cfg = make_tuner(tmp_path, {"fire_delay_ms": 1190})
    out = tuner.step(block(30, err=-40), cfg)
    assert switches.get(cfg, "fire.delay") == 1200
    assert out["applied"][0]["new"] == 1200

    nxt = tuner.step(block(30, err=-40, ts0=TS0 + 100), cfg)
    assert nxt["applied"] == []
    assert switches.get(cfg, "fire.delay") == 1200
    assert any(e.get("note", "").startswith("stopped")
               or "撞界" in e.get("note", "")
               for e in nxt["audit"])
    state = store.state["knobs"]["fire.delay"]
    assert state["stop_side"] == "hi"


def test_shadow_changes_nothing_but_audits(tmp_path):
    tuner, store, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100},
                                    shadow=True)
    before = copy.deepcopy(cfg)
    for i in range(3):
        out = tuner.step(block(30, err=-40, ts0=TS0 + i * 100), cfg)
        assert out["shadow"] is True
        assert out["applied"] == []
    assert cfg == before
    assert switches.get(cfg, "fire.delay") == 100
    entries = store.audit_entries()
    assert entries and all(e["mode"] == "shadow" for e in entries)
    assert any(e["action"] == "shadow" for e in entries)
    assert all(e["knob"] and "text" in e for e in entries)


def test_rollback_three_changes(tmp_path):
    tuner, store, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    values = [100]
    for i in range(3):
        out = tuner.step(block(30, err=-40, ts0=TS0 + i * 100), cfg)
        assert out["applied"], out["audit"]
        values.append(switches.get(cfg, "fire.delay"))
    assert values == [100, 120, 144, 173]
    assert len(store.state["history"]) == 3

    result = tuner.rollback(cfg, steps=3)
    assert result["rolled_back"] is True
    assert switches.get(cfg, "fire.delay") == 100
    assert result["version"] == 0
    assert store.state["history"] == []
    assert store.audit_entries()[-1]["action"] == "rollback"


# ---------------------------------------------------------------- kill switch


def test_kill_switch_all_fail(tmp_path):
    tuner, store, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    out = tuner.step(block(40, result="miss", err=-200), cfg)
    assert out["frozen"] is True
    assert any("全部失败" in r for r in out["freeze_reasons"])
    assert out["applied"] == []
    assert switches.get(cfg, "fire.delay") == 100
    assert any(e["action"] == "freeze" for e in store.audit_entries())

    assert tuner.unfreeze("人工复核：确实是配置太差，不是数据异常") is True
    assert tuner.state["frozen"] is False


def test_kill_switch_all_success(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    out = tuner.step(block(40, result="hit", err=-40), cfg)
    assert out["frozen"] is True
    assert any("全部成交" in r for r in out["freeze_reasons"])
    assert switches.get(cfg, "fire.delay") == 100


def test_kill_switch_time_disorder(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    rows = make_rows(40, err=-40)
    rng = random.Random(3)
    rng.shuffle(rows)  # 时间戳字段不变，顺序打乱 -> 升/降混杂
    out = tuner.step(autotune.summarize(rows), cfg)
    assert out["frozen"] is True
    assert any("乱序" in r for r in out["freeze_reasons"])


def test_kill_switch_single_item_spam(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    out = tuner.step(block(40, err=-40, names=("只有一个",)), cfg)
    assert out["frozen"] is True
    assert any("刷屏" in r for r in out["freeze_reasons"])


def test_freeze_blocks_even_with_clear_signal(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    tuner.step(block(40, result="miss", err=-200), cfg)
    out = tuner.step(block(60, err=-40, ts0=TS0 + 100), cfg)
    assert out["frozen"] is True
    assert switches.get(cfg, "fire.delay") == 100


# ---------------------------------------------------------------- 规则


def test_skip_threshold_rule(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {})
    rows = make_rows(40, result="skip", evidence="本来来得及", err=None)
    out = tuner.step(autotune.summarize(rows), cfg)
    assert len(out["applied"]) == 1
    assert out["applied"][0]["knob"] == "buy.skip_late.threshold_s"
    assert switches.get(cfg, "buy.skip_late.threshold_s") == 1
    entry = [e for e in out["audit"]
             if e["knob"] == "buy.skip_late.threshold_s"][0]
    assert "本来来得及" in entry["text"]


def test_refresh_rule_positive(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {})
    rows = (make_rows(40, refresh=1, hit_ratio=0.40, err=None, ts0=TS0)
            + make_rows(40, refresh=3, hit_ratio=0.85, err=None,
                        ts0=TS0 + 100))
    summary = autotune.summarize(rows)
    assert summary["refresh_benefit"]["status"] == "ok"
    assert summary["refresh_benefit"]["slope_ci95"][0] > 0
    out = tuner.step(summary, cfg)
    assert out["applied"][0]["knob"] == "scan.interval"
    assert switches.get(cfg, "scan.interval") < 0.05
    entry = [e for e in out["audit"] if e["knob"] == "scan.interval"][0]
    assert "每多刷一次" in entry["text"]


def test_refresh_no_signal_keeps_value(tmp_path):
    tuner, _, cfg = make_tuner(tmp_path, {})
    rows = (make_rows(40, refresh=1, hit_ratio=0.5, err=None, ts0=TS0)
            + make_rows(40, refresh=3, hit_ratio=0.5, err=None,
                        ts0=TS0 + 100))
    out = tuner.step(autotune.summarize(rows), cfg)
    assert out["applied"] == []
    assert switches.get(cfg, "scan.interval") == 0.05


# ---------------------------------------------------------------- 极端输入


@pytest.mark.parametrize("rows", [
    [],
    make_rows(3, err=-40),
    [{"lead_err_ms": 1e18, "price": 1e30}],
    [{"result": "hit"}, {"result": "hit"}, {"result": "hit"}],
])
def test_extreme_inputs_never_crash_or_break_bounds(tmp_path, rows):
    tuner, _, cfg = make_tuner(tmp_path, {"fire_delay_ms": 100})
    normalized = [autotune.normalize_row(r) for r in rows]
    normalized = [r for r in normalized if r is not None]
    summary = autotune.summarize(normalized)
    out = tuner.step(summary, cfg)
    for knob, spec in autotune.KNOBS.items():
        lo, hi = spec["soft"]
        value = switches.get(cfg, knob)
        assert lo <= value <= hi, knob
    assert isinstance(out["applied"], list)


def test_duplicates_are_removed_not_counted(tmp_path):
    rows = make_rows(5, err=-10, hit_ratio=0.5)
    summary = autotune.summarize(rows + [dict(r) for r in rows])
    assert summary["quality"]["duplicates_removed"] == 5
    assert summary["n_rows"] == 5


# ---------------------------------------------------------------- 返回契约


_TOP_KEYS = {
    "n_rows", "n_attempts", "n_hits", "hit_ts", "window_ms", "min_sample",
    "quality", "by_grade", "lead_error", "result_breakdown", "skip_accuracy",
    "refresh_benefit", "burst_benefit", "by_hour", "by_client",
    "missing_fields",
}
_QUALITY_KEYS = {
    "n_rows", "n_attempts", "duplicates_removed", "result_unknown",
    "ts_min", "ts_max", "time_order", "ts_suspect", "ts_suspect_ratio",
    "recent_window", "recent_hits", "all_fail", "all_success",
    "single_item_share", "single_item_spam", "freeze_reasons", "frozen",
}
_GROUP_KEYS = {
    "attempts", "hits", "misses", "skips", "others", "hit_rate", "miss_rate",
    "skip_rate", "hit_rate_ci95", "lead", "insufficient",
}
_DIST_KEYS = {
    "n", "mean", "p50", "p90", "p95", "out_of_window",
    "out_of_window_ratio", "late_off", "early_off",
}


def test_dedupe_always_returns_two_tuple():
    """_dedupe 的返回形状在所有输入分支上一致： (kept_list, duplicates_int)。"""
    empty = autotune._dedupe([])
    assert isinstance(empty, tuple) and len(empty) == 2
    assert isinstance(empty[0], list) and isinstance(empty[1], int)

    rows = make_rows(4)
    kept, dup = autotune._dedupe(rows)
    assert len(kept) == 4 and dup == 0

    kept, dup = autotune._dedupe(rows + [dict(r) for r in rows])
    assert len(kept) == 4 and dup == 4

    # 无时间戳的行不参与去重，同样保持二元返回
    without_ts = [{**r, "ts": None} for r in make_rows(2)]
    kept, dup = autotune._dedupe(without_ts)
    assert len(kept) == 2 and dup == 0


@pytest.mark.parametrize("branch", ["empty", "insufficient", "frozen", "normal"])
def test_summary_shape_is_identical_across_branches(branch, tmp_path):
    if branch == "empty":
        rows = []
    elif branch == "insufficient":
        rows = make_rows(3, err=-40)
    elif branch == "frozen":
        rows = make_rows(40, result="miss", err=-200)
    else:
        rows = make_rows(40, err=-40, hit_ratio=0.6)
    summary = autotune.summarize(rows)

    assert set(summary) == _TOP_KEYS
    assert set(summary["quality"]) == _QUALITY_KEYS
    assert isinstance(summary["by_grade"], dict)
    for stat in summary["by_grade"].values():
        assert set(stat) == _GROUP_KEYS
        assert set(stat["lead"]) == _DIST_KEYS
    assert set(summary["lead_error"]) == {"all", "by_grade", "by_hour",
                                          "by_client"}
    assert set(summary["lead_error"]["all"]) == _DIST_KEYS
    assert set(summary["skip_accuracy"]) == {
        "n_skipped", "confirmed_n", "confirmed_late", "confirmed_wrong",
        "unknown", "hit_accuracy", "hit_accuracy_ci95", "wrong_rate",
        "verdict",
    }
    assert set(summary["refresh_benefit"]) == {
        "status", "reason", "buckets", "total_n", "slope_per_refresh",
        "slope_ci95", "min_bucket_n",
    }
    assert set(summary["burst_benefit"]) == {"status", "reason", "groups"}
    assert set(summary["result_breakdown"]) == {"total", "counts", "rates"}
    assert isinstance(summary["hit_ts"], list)
    for key in ("by_hour", "by_client"):
        for stat in summary[key].values():
            assert set(stat) == _GROUP_KEYS
    # 所有分支都必须能直接进 step，且不崩
    tuner = autotune.AutoTuner(
        {}, shadow=True,
        store=autotune.AutotuneStore(directory=str(tmp_path / "autotune")))
    out = tuner.step(summary, tuner.cfg)
    assert set(out) == {"shadow", "frozen", "freeze_reasons", "decisions",
                        "applied", "withheld", "audit", "version"}
    assert out["frozen"] == summary["quality"]["frozen"]
