"""R1 回归：六段漏斗口径 + 入池证据判定 + inbox 挂接。

这些用例只跑 evo/ 的纯离线层，不加载引擎、不碰游戏。
"""
import json
import os

import pytest

from evo import attribute, funnel, pool_contract, pool_inbox
from evo.config import load_config


def _attempt(**kw):
    row = {"ts": 1794000000.0, "day": "2026-11-07", "name": "A件",
           "price": 100.0, "result": "hit", "ts_ms": 1794000000000,
           "time_src": {}}
    row.update(kw)
    return row


def _bundle(attempts, skips=None, measured=None):
    return {"attempts": attempts, "skips": skips or [], "rollup": {"days": {}},
            "health": {}, "telemetry_events": [],
            "stage_measured": measured or {}}


def test_stage_order_is_six():
    assert [k for k, _ in funnel.STAGES] == [
        "discover", "listed", "click", "pool", "hit", "deal"]


def test_legacy_miss_is_pool_unknown_not_pool():
    out = funnel.build(load_config(), _bundle([_attempt(result="miss")]))
    batch = out["batch"]
    assert batch["counts"]["click"] == 1
    assert batch["counts"]["pool"] == 0
    assert batch["pool_unknown"] == 1
    assert batch["metrics"]["pool_unknown_count"] == 1


def test_evidence_states():
    joined = pool_contract.classify(_attempt(join_result="joined"))
    assert joined["pool"] == "joined" and joined["evidence"] == "ledger:join_result"

    full = pool_contract.classify(_attempt(result="miss", join_result="queue_full"))
    assert full["pool"] == "not_joined" and full["unknown"] is False

    roll = pool_contract.classify(_attempt(result="miss", roll_self=99, roll_max=125))
    assert roll["pool"] == "joined" and roll["won"] is False
    assert roll["unknown"] is False

    deal = pool_contract.classify(_attempt(result="hit", verified=True))
    assert deal["hit"] is True and deal["deal"] is True

    conflict = pool_contract.classify(_attempt(result="hit", won=False))
    assert conflict["conflict"] == "ledger_hit_toast_lose"


def test_inbox_attach_turns_unknown_into_pool_evidence(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "20261107.jsonl").write_text(json.dumps({
        "ts": 1794000060.0, "text": "已加入抢购池", "name": "A件"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    cfg = load_config({"observation": {"pool_inbox_dir": str(inbox)}})
    ev = _attempt(result="miss")

    stats = pool_inbox.attach([ev], cfg)
    assert stats["attached"] == 1 and stats["unattached"] == 0
    assert ev["join_result"] == "joined"
    assert ev["time_src"]["join_result"] == "ocr_epoch"

    out = funnel.build(cfg, _bundle([ev]))
    assert out["batch"]["counts"]["pool"] == 1
    assert out["batch"]["pool_unknown"] == 0


def test_attribute_skips_unmeasured_stage_and_reports_gap():
    out = funnel.build(load_config(), _bundle([_attempt(result="hit")]))
    attr = attribute.attribute(load_config(), out)
    assert attr["bottleneck"]["from"] == "hit"
    assert attr["bottleneck"]["to"] == "deal"
    gaps = {(g["from"], g["to"]) for g in attr["data_gaps"]}
    assert ("discover", "listed") in gaps
    assert ("listed", "click") in gaps
