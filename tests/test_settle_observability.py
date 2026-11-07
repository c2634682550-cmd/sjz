"""结算三态 + 两个观测时间戳 + 六段漏斗证据态的回归。

硬约束在这里逐条钉死：**没有证据必须是 unknown**，不许落成 lost，
也不许落成 won；缺观测的段标 ``no_evidence``，不许算 0。
"""
import json

import pytest

from core import records, settle_verify
from evo import funnel
from evo.config import load_config

BASE = {"ts": 1794000000.0, "day": "2026-11-07", "name": "A件",
        "price": 100.0, "result": "hit", "ts_ms": 1794000000000,
        "time_src": {}}


def _attempt(**kw):
    row = dict(BASE)
    row.update(kw)
    return row


def _bundle(attempts, measured=None, skips=None):
    return {"attempts": attempts, "skips": skips or [], "rollup": {"days": {}},
            "health": {}, "telemetry_events": [],
            "discover_measured": True,
            "stage_measured": measured or {}}


def test_no_evidence_is_unknown_never_lost():
    """完全无证据的行：unknown，且不能是 lost / won。"""
    row = _attempt()                      # result=hit，但没有任何结算证据
    verdict = settle_verify.from_row(row)
    assert verdict == settle_verify.UNKNOWN
    assert verdict != settle_verify.LOST
    assert verdict != settle_verify.WON
    detail = settle_verify.explain(row)
    assert detail["verified"] == "unknown"
    assert detail["reason"] == "no_evidence"
    assert detail["won_evidence"] == [] and detail["lost_evidence"] == []


def test_empty_and_false_like_inputs_are_unknown():
    for payload in ({}, {"verified": None}, {"verified": False},
                    {"verified": "false"}, {"verified": 0},
                    {"verified": "garbage"}):
        assert settle_verify.from_row(payload) == "unknown"
    assert settle_verify.decide({}) == "unknown"


def test_won_evidence():
    assert settle_verify.decide({"won_screen": True}) == "won"
    assert settle_verify.decide({"item_received": True}) == "won"
    assert settle_verify.decide({"reward_claimable": True}) == "won"
    assert settle_verify.decide({"toast_won": True,
                                 "toast_won_source": "text"}) == "won"


def test_lost_requires_explicit_evidence():
    assert settle_verify.decide({"lost_text": True}) == "lost"
    assert settle_verify.decide({"toast_won": False,
                                 "toast_won_source": "text"}) == "lost"
    # 公示结束 + 列表消失，但奖励状态未知 -> unknown（不猜「没奖」）
    assert settle_verify.decide({"publicity_over": True,
                                 "row_gone_from_list": True}) == "unknown"
    # 三条都拿到才算 lost
    assert settle_verify.decide({"publicity_over": True,
                                 "row_gone_from_list": True,
                                 "reward_absent": True}) == "lost"


def test_points_only_and_conflict_are_unknown():
    assert settle_verify.decide({"toast_won": False,
                                 "toast_won_source": "points"}) == "unknown"
    both = {"won_screen": True, "lost_text": True}
    assert settle_verify.decide(both) == "unknown"
    assert settle_verify.explain(both)["reason"] == "conflicting_evidence"


def test_legacy_bool_mapping():
    assert settle_verify.normalize(True) == "won"
    assert settle_verify.normalize(False) == "unknown"
    assert settle_verify.normalize(None) == "unknown"
    assert settle_verify.normalize("WON") == "won"
    assert settle_verify.normalize("lost") == "lost"


def test_time_marks_reject_zero_and_negative():
    assert settle_verify.norm_ms(0) is None
    assert settle_verify.norm_ms(-1) is None
    assert settle_verify.norm_ms(True) is None
    assert settle_verify.norm_ms("1000") is None
    assert settle_verify.norm_ms(1234) == 1234
    assert settle_verify.missing_marks({"first_seen_ms": 0,
                                        "listed_at_ms": -1}) == [
        "first_seen_ms", "listed_at_ms"]
    assert settle_verify.missing_marks({"first_seen_ms": 10,
                                        "listed_at_ms": 20}) == []


def test_mark_helpers_are_idempotent():
    row = {"first_seen_ms": 1000}
    settle_verify.mark_first_seen(row, clock=9.0)
    assert row["first_seen_ms"] == 1000       # 已有值不覆盖
    settle_verify.mark_listed(row, clock=9.5)
    assert row["listed_at_ms"] == 9500
    assert settle_verify.gap_ms(row["first_seen_ms"], row["listed_at_ms"]) == 8500
    assert settle_verify.gap_ms(None, 9500) is None
    assert settle_verify.gap_ms(1000, 999) is None


def test_gap_rejects_mixed_clock_bases():
    # first_seen 是单调时钟（开机毫秒），listed 是 epoch 毫秒 -> 假时长，判无证据
    assert settle_verify.gap_ms(1000, 1793000000000) is None
    # 两端同基准（都是 epoch）仍可算，差值在 12h 内
    assert settle_verify.gap_ms(1793000000000, 1793000005000) == 5000
    # 超过 12h 的作息跨度过长，不作为段间时长
    assert settle_verify.gap_ms(1000, 1000 + settle_verify.MAX_GAP_MS + 1) is None


def test_records_write_tristate_and_marks(monkeypatch):
    records.add(name="无证据", result="hit")
    records.add(name="时间齐全", result="hit",
                first_seen_ms=1000, listed_at_ms=3500)
    records.add(name="明确未中签", result="hit", verified="lost")
    rows = {r["name"]: r for r in records.load(limit=0)}

    assert rows["无证据"]["verified"] == "unknown"
    assert rows["无证据"]["first_seen_ms"] is None
    assert rows["无证据"]["listed_at_ms"] is None
    assert rows["无证据"]["no_evidence"] == ["first_seen_ms", "listed_at_ms"]

    assert rows["时间齐全"]["verified"] == "unknown"
    assert rows["时间齐全"]["first_seen_ms"] == 1000
    assert rows["时间齐全"]["listed_at_ms"] == 3500
    assert rows["时间齐全"]["no_evidence"] == []

    assert rows["明确未中签"]["verified"] == "lost"

    stats = records.stats()
    assert stats["hits"] == 3
    assert stats["unverified_hits"] == 3   # won=0，三单都不是成交
    assert stats["verified_hits"] == 0


def test_records_legacy_row_reads_without_error(monkeypatch):
    """旧行（verified=None、没有新字段）照读，并进漏斗不报错。"""
    legacy = {"ts": 1794000000.0, "t": "08:00:00", "day": "2026-11-07",
              "name": "旧行", "price": 100.0, "result": "hit",
              "elapsed": None, "shot": "", "mode": "normal", "quota": None,
              "verified": None}
    with open(records.PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(legacy, ensure_ascii=False) + "\n")
    rows = records.load(limit=0)
    assert len(rows) == 1 and rows[0]["verified"] is None
    assert settle_verify.from_row(rows[0]) == "unknown"

    attempts = [dict(legacy)]
    attempts[0]["verified"] = rows[0]["verified"]
    out = funnel.build(load_config(), _bundle(attempts, {"listed": False}))
    assert out["batch"]["counts"]["hit"] == 1
    assert out["batch"]["metrics"]["settle_rate"] is None


def test_funnel_six_stage_no_evidence_statuses():
    out = funnel.build(load_config(), _bundle([_attempt()],
                                              {"discover": False,
                                               "listed": False}))
    batch = out["batch"]
    stages = {s["stage"]: s for s in batch["stages"]}
    assert [s["stage"] for s in batch["stages"]] == list(
        ["discover", "listed", "click", "pool", "hit", "deal"])
    assert stages["discover"]["status"] == "no_evidence"
    assert stages["listed"]["status"] == "no_evidence"
    assert stages["deal"]["status"] == "no_evidence"
    assert stages["deal"]["unknown"] == 1
    assert stages["deal"]["step_rate"] is None
    assert stages["deal"]["no_evidence"] is True
    assert batch["no_evidence"] == ["discover", "listed", "deal"]
    assert batch["metrics"]["settle_state"] == "no_evidence"
    assert batch["metrics"]["settle_rate"] is None
    assert batch["metrics"]["end_to_end_rate"] is None
    assert batch["metrics"]["discover_to_listed"]["status"] == "no_evidence"
    assert batch["metrics"]["discover_to_listed"]["samples"] == 0
    assert batch["metrics"]["listed_to_click"]["samples"] == 0


def test_funnel_six_stage_with_full_evidence():
    common = {"first_seen_ms": 1000, "listed_at_ms": 3000,
              "click_at_ms": 3500}
    attempts = [
        _attempt(name="中签", join_result="joined", verified="won", **common),
        _attempt(name="未中签", join_result="joined", verified="lost", **common),
        _attempt(name="无证据", join_result="joined", verified="unknown", **common),
        _attempt(name="队列满", result="miss", join_result="queue_full", **common),
        _attempt(name="池内点败", result="miss",
                 roll_self=10, roll_max=100, **common),
    ]
    out = funnel.build(load_config(), _bundle(attempts))
    batch = out["batch"]
    stages = {s["stage"]: s for s in batch["stages"]}

    assert batch["counts"] == {"discover": 5, "listed": 5, "click": 5,
                               "pool": 4, "hit": 3, "deal": 1}
    for s in batch["stages"]:
        assert s["status"] in ("measured", "partial")
        assert s["step_rate"] is not None or s["stage"] == "deal"
    assert stages["deal"]["settle_breakdown"] == {"won": 1, "lost": 1,
                                                  "unknown": 1}
    assert batch["metrics"]["settle_rate"] == pytest.approx(1 / 3)
    assert batch["metrics"]["settle_state"] == "measured"
    assert batch["metrics"]["discover_to_listed"]["p50_s"] == 2.0
    assert batch["metrics"]["discover_to_listed"]["samples"] == 5
    assert batch["metrics"]["listed_to_click"]["p50_s"] == 0.5
    assert batch["metrics"]["listed_to_click"]["samples"] == 5
