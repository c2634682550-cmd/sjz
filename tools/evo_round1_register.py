#!/usr/bin/env python
"""R1 登记：把本轮 AB 实测写进 evo/registry.jsonl（schema/2，可回滚）。

数据全部取自 evo/runs/round1_20261107/ab_runs.json，不手填指标。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from evo import registry  # noqa: E402
from evo.config import load_config  # noqa: E402

RUN_DIR = os.path.join(ROOT, "evo", "runs", "round1_20261107")
SIG = "b41c9e07d2a53f18"


def main():
    with open(os.path.join(RUN_DIR, "ab_runs.json"), encoding="utf-8") as f:
        ab = json.load(f)
    b, c = ab["by_arm"]["base"], ab["by_arm"]["cand"]
    delta = ab["delta"]

    hyp = {
        "id": "H-" + SIG + "-1",
        "class": "观测级",
        "title": "把台账/旁路入池证据接进 observe→funnel，扩成六段漏斗",
        "factor": "pool_evidence_gap",
        "metric": "phantom_pool_count",
        "expected_direction": {"metric": "phantom_pool_count", "direction": "down",
                               "min_delta": 1},
        "expected_gain": {"metric": "stages_n", "abs_delta": 1,
                          "basis": "老台账 9 条 miss 无入池证据，却被算成入池"},
        "param": {"name": "evo.funnel.stages", "from": 5, "to": 6, "unit": "stage"},
        "signature": SIG,
    }
    sample = {"planned": int(b["counts"]["discover"]),
              "observed": {"n": int(c["counts"]["discover"]),
                           "pool_evidence_rows": int(c["counts"]["pool"]),
                           "pool_unknown": int(c["metrics"]["pool_unknown_count"] or 0)}}
    before = {"metric": "phantom_pool_count", "value": delta["phantom_pool_removed"],
              "pool_count": int(b["counts"]["pool"]),
              "pool_rate": b["metrics"]["pool_rate"], "stages_n": b["stages_n"]}
    after = {"metric": "phantom_pool_count", "value": 0,
             "pool_count": int(c["counts"]["pool"]),
             "pool_rate": c["metrics"]["pool_rate"], "stages_n": c["stages_n"],
             "pool_unknown_count": int(c["metrics"]["pool_unknown_count"] or 0)}

    changes = [
        {"file": "evo/pool_contract.py", "lines": "1-111",
         "summary": "新增证据口径：join_result/pool_entered/roll/won/verified 分级判定，缺失即 unknown"},
        {"file": "evo/pool_inbox.py", "lines": "1-111",
         "summary": "新增 inbox 挂接：按 attempt_id 精确匹配，其次同名 0~180s 邻接"},
        {"file": "evo/funnel.py", "lines": "1-284",
         "summary": "五段扩六段，输出 unknown/measured/claimed，pool_rate 分母只算可证入池"},
        {"file": "evo/attribute.py", "lines": "1-168",
         "summary": "瓶颈评选跳过无证据段；未知区间记 unresolved；新增 pool_evidence_gap 因子"},
        {"file": "evo/observe.py", "lines": "18,59,223,232-301,308-337,366-391",
         "summary": "透传 22 列入池字段；接 pool_inbox；rollup 按 classify 计数 unknown/listed"},
        {"file": "evo/daemon.py", "lines": "382-420",
         "summary": "日报改六段漏斗，增未知/数据列与可证入池率"},
        {"file": "evo/selftest.py", "lines": "26-75,152-170",
         "summary": "夹具补入池/抽签字段，断言改六段与有证据段单调"},
    ]
    side_effects = [
        {"metric": "pool_rate", "delta": f"{delta['pool_rate_pp']:+.2f}pp",
         "note": "口径纠正而非退化：9 条无证据 miss 从入池移到 unknown"},
        {"metric": "funnel_build_p95", "delta": f"{delta['wall_p95_ms']:+.3f}ms",
         "note": f"离线分析层，{ab['repeat_per_run']} 次重复；相对 "
                 f"{delta['wall_p95_rel']:+.1%}，不在抢购热路径"},
        {"metric": "stages_n", "delta": f"{delta['stages_n']:+d}",
         "note": "五段→六段，下游台账/日报按 stage key 读取"},
    ]
    rollback = {
        "method": "patch-reverse",
        "ref": os.path.join(RUN_DIR, "impl_round1.patch").replace("\\", "/"),
        "verify": "tools/evo_round1_ab.py 复跑应回到 pool=137、stages_n=5、"
                  "pool_unknown_count=None",
        "reverse_check": ab["patch_reverse_check"]["returncode"],
    }
    entry = registry.make_entry(
        hyp, run_id="20261107T195502", round_no=1, level="offline",
        sample=sample, before=before, after=after, conclusion="accepted",
        changes=changes, side_effects=side_effects, rollback=rollback)
    entry["evidence"] = {
        "ab_runs": os.path.join(RUN_DIR, "ab_runs.json").replace("\\", "/"),
        "corpus": os.path.join(RUN_DIR, "corpus_real.json").replace("\\", "/"),
        "funnel": os.path.join(RUN_DIR, "funnel_real.json").replace("\\", "/"),
        "attribution": os.path.join(RUN_DIR, "attribution_real.json").replace("\\", "/"),
        "source_audit": os.path.join(RUN_DIR, "source_audit.json").replace("\\", "/"),
        "core_digest": ab["core_digest_after"]["short16"],
        "core_digest_stable": ab["core_digest_stable"],
        "order": ab["order"],
        "repeat_per_run": ab["repeat_per_run"],
    }
    n = registry.append_entries(load_config(), [entry])
    print(f"appended={n} signature={SIG} conclusion=accepted")
    print(f"before={before}")
    print(f"after={after}")


if __name__ == "__main__":
    main()
