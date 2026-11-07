"""自检：用确定性夹具验证漏斗数学、假设校验、护栏、登记册、断点续跑、回滚。

夹具仅用于校验机器，不代表真实数据；真实数据由 dryrun 单独跑。
"""
import json
import os
import random
import sqlite3
import time

from . import decide, funnel, guardrails, hypothesize, registry, rollback
from .common import ensure_dir, fmt_ts, load_json, path, stamp
from .config import load_config
from .daemon import run_round

FIX_DIR = path("fixtures", "synthetic")
RESULTS = []


def _check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def build_fixture():
    ensure_dir(FIX_DIR)
    rng = random.Random(20261107)
    ledger = os.path.join(FIX_DIR, "records.jsonl")
    db = os.path.join(FIX_DIR, "telemetry.db")
    rows, events = [], []
    base = time.mktime(time.strptime("2026-11-05 09:00:00", "%Y-%m-%d %H:%M:%S"))
    plan = [(600, 150, 430), (600, 60, 520), (600, 90, 490)]
    ev_seq = 1
    for day, (n, hits, misses) in enumerate(plan):
        for i in range(n):
            ts = base + day * 86400 + i * 90 + rng.randint(-20, 20)
            blocked = 4 if i % 17 == 0 else 0
            if blocked:
                result = "no_money" if i % 2 else "over_budget"
                verified = None
                joined = None
                roll_self = roll_max = won = None
            elif i < hits:
                result = "hit"
                verified = True
                joined = True
                roll_self = rng.randint(80, 140)
                roll_max = rng.randint(40, roll_self)
                won = True
            else:
                result = "miss"
                verified = None
                joined = True
                roll_self = rng.randint(20, 90)
                roll_max = rng.randint(roll_self + 1, 160)
                won = False
            row = {
                "ts": round(ts, 3),
                "t": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                "name": f"{'传说' if i % 5 == 0 else '优品'}-{day}-{i}",
                "price": round(rng.uniform(80, 400), 2) if result == "hit" else None,
                "result": result, "elapsed": round(rng.uniform(4, 9), 2),
                "shot": "s.png" if result == "hit" else "",
                "mode": "detail", "quota": 1,
                "verified": verified,
                "verified_by": "dialog" if verified else "",
                "join_result": "joined" if joined else None,
                "pool_entered": joined,
                "roll_self": roll_self,
                "roll_max": roll_max,
                "won": won,
                "attempt_id": f"a{ev_seq:08x}",
            }
            rows.append(row)
            events.append({
                "event_id": f"{ev_seq:016x}", "occurred_at": ts,
                "tier": row["name"].split("-")[0], "name": row["name"],
                "price": row["price"], "result": result,
                "skew_ms": int(rng.gauss(110, 35)),
                "skip_reason": None,
            })
            ev_seq += 1
        for k in range(300):
            ts = base + day * 86400 + 200 + k * 2
            events.append({
                "event_id": f"{ev_seq:016x}", "occurred_at": ts,
                "tier": "优品", "name": f"skip-{day}-{k}", "price": None,
                "result": "skip",
                "skew_ms": None,
                "skip_reason": "来不及" if k % 3 else "未解锁",
            })
            ev_seq += 1
    rows.sort(key=lambda r: r["ts"])
    with open(ledger, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE events(
      seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
      lic_hash TEXT NOT NULL, machine TEXT, occurred_at REAL NOT NULL,
      day TEXT NOT NULL, tier TEXT, name TEXT, price REAL, result TEXT NOT NULL,
      skew_ms INTEGER, skip_reason TEXT, client_version TEXT,
      received_at REAL NOT NULL)""")
    for e in events:
        conn.execute(
            "INSERT INTO events(event_id,lic_hash,machine,occurred_at,day,tier,"
            "name,price,result,skew_ms,skip_reason,client_version,received_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["event_id"], "lic" + e["event_id"][:8], "m", e["occurred_at"],
             time.strftime("%Y-%m-%d", time.localtime(e["occurred_at"]),
                           ), e["tier"], e["name"], e["price"], e["result"],
             e["skew_ms"], e["skip_reason"], "selftest",
             time.time()))
    conn.commit()
    conn.close()
    return ledger, db, rows, events


def _cfg(ledger, db, tag):
    cfg = load_config()
    cfg["observation"]["ledger_path"] = ledger
    cfg["observation"]["telemetry_db"] = db
    stamp_ = stamp()
    cfg["_state_dir"] = path("fixtures", f"state-{tag}-{stamp_}")
    cfg["_runs_dir"] = path("fixtures", f"runs-{tag}-{stamp_}")
    cfg["_registry"] = path("fixtures", f"registry-{tag}-{stamp_}.jsonl")
    cfg["_shadow_dir"] = "data"
    return cfg


def _clean_old():
    import glob
    import shutil
    for pat in ("state-*", "runs-*"):
        for p in glob.glob(os.path.join(FIX_DIR, pat)):
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
    for p in glob.glob(os.path.join(FIX_DIR, "registry-*.jsonl")):
        try:
            os.remove(p)
        except OSError:
            pass


def run_selftest():
    print("EVO 自检开始", fmt_ts())
    _clean_old()
    ledger, db, rows, events = build_fixture()

    # ---- 1. 漏斗数学 ----
    cfg = _cfg(ledger, db, "main")
    res = run_round(cfg, resume=False, cycle_index=1)
    _check("一轮跑通", res.get("ok"), str(res.get("error") or res.get("run_id")))
    if not res.get("ok"):
        return _finish()
    summ = load_json(os.path.join(res["run_dir"], "08_summary.json"))
    stages = summ["funnel_window"]["stages"]
    counts = [s["count"] for s in stages]
    _check("六段漏斗存在", len(stages) == 6, str([s["label"] for s in stages]))
    measured_pairs = [(a, b) for a, b in zip(stages[:-1], stages[1:])
                      if a.get("measured") and b.get("measured")]
    _check("有证据段单调不增",
           all(a["effective"] >= b["count"] for a, b in measured_pairs),
           str(counts))
    _check("候选样本量 > 300", counts[0] > 300, f"discover={counts[0]}")
    _check("有证据段都有流失率",
           all(a["loss_rate"] is not None for a, _ in measured_pairs),
           f"pairs={len(measured_pairs)}")
    _check("瓶颈已定位", bool(summ.get("bottleneck")),
           str((summ.get("bottleneck") or {}).get("from")))

    # ---- 2. 假设校验 ----
    hs = summ.get("hypotheses") or []
    _check("假设 <= 5 条", len(hs) <= 5, f"n={len(hs)}")
    req = ("expected_direction", "min_sample", "stop_condition", "expected_gain")
    _check("每条四要素齐全", all(all(h.get(k) for k in req) for h in hs))
    _check("类别均在三级内",
           all(h["class"] in ("参数级", "策略级", "时序级") for h in hs),
           str(sorted({h["class"] for h in hs})))

    # ---- 3. 三级实验闸门 ----
    exps = summ.get("experiments") or {}
    off = [(r.get("offline") or {}).get("status") for r in exps.values()]
    sh = [(r.get("shadow") or {}).get("status") for r in exps.values()]
    _check("离线回放已执行", bool(off), str(off))
    _check("离线通过后可进影子", ("passed" in off) == ("passed" in sh), str(sh))
    _check("真机级未执行（禁用）",
           all((r.get("real") or {}).get("status") in ("blocked", "skipped")
               for r in exps.values()))

    # ---- 4. 登记册 ----
    entries = registry.load(cfg)
    _check("登记册已写入", len(entries) > 0, f"{len(entries)} 条")
    _check("登记册字段齐全",
           all(k in entries[0] for k in ("hypothesis", "params", "sample_size",
                                         "metric_before", "metric_after",
                                         "conclusion", "failure_reason", "ts"))
           if entries else False)
    idx = registry.index(cfg)
    if hs:
        fake = {"conclusion": "rejected", "signature": hs[0]["signature"],
                "ts": time.time()}
        registry.append_entries(cfg, [fake])
        _check("同类失败被冷静期拦截",
               registry.blocked(cfg, registry.index(cfg), hs[0]["signature"]))

    # ---- 5. 护栏 ----
    risky = {"id": "H-risk", "title": "真实下单", "signature": "risk",
             "actions": ["submit_order"], "param": {"name": "console.quota",
                                                     "to": 3},
             "risk_flags": ["foreground"]}
    g = guardrails.evaluate(cfg, risky, {"baseline_skew_p95_ms": 100,
                                         "projected_skew_p95_ms": 100})
    codes = [v["code"] for v in g["vetoes"]]
    _check("G1 不误买否决", "G1_NO_PURCHASE" in codes, str(codes))
    _check("G2 额度否决", "G2_BUDGET_QUOTA" in codes, str(codes))
    _check("G5 前台否决", "G5_FOREGROUND" in codes, str(codes))

    # ---- 6. 断点续跑 ----
    cfg2 = _cfg(ledger, db, "crash")
    r1 = run_round(cfg2, resume=False, cycle_index=1, fault="attribute")
    _check("注入崩溃已留痕", (not r1.get("ok")) and r1.get("crashed"),
           str(r1.get("error")))
    cp = load_json(os.path.join(cfg2["_state_dir"], "checkpoint.json"))
    _check("checkpoint 记录已完成阶段",
           cp.get("phases_done") == ["observe", "funnel"],
           str(cp.get("phases_done")))
    r2 = run_round(cfg2, resume=True)
    _check("断点续跑成功", r2.get("ok") and r2.get("resumed"),
           str(r2.get("error") or ""))
    _check("续跑保留原 run 目录", r2.get("run_id") == r1.get("run_id"))

    # ---- 7. 回滚 ----
    cfg3 = _cfg(ledger, db, "rollback")
    fun = {"window": {"counts": {"discover": 100},
                      "metrics": {"hit_rate": 0.50,
                                  "end_to_end_rate": 0.30}}}
    hyp = {"param": {"name": "fire_delay_ms", "from": 0, "to": 30},
           "metric": "hit_rate",
           "expected_direction": {"direction": "up"},
           "signature": "rb-test"}
    rollback.promote(cfg3, hyp, fun)
    degraded = {
        "window": {"counts": {"discover": 100},
                   "metrics": {"hit_rate": 0.10}},
        "batch": {}}
    rec = None
    for _ in range(int(cfg3["rollback"]["canary_cycles"])):
        rec = rollback.observe(cfg3, degraded)
    _check("劣化超阈值自动回滚", rec is not None
           and rec.get("event") == "rollback", str(rec))
    ov = rollback.load(cfg3)
    _check("回滚恢复上一版参数",
           (ov.get("current") or {}).get("fire_delay_ms", {}).get("value") == 0,
           str(ov.get("current")))
    return _finish()


def _finish():
    bad = [r for r in RESULTS if not r[1]]
    print("-" * 60)
    print(f"自检结束：{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    return 0 if not bad else 1
