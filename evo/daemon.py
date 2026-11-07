"""长任务守护：24h 一轮、checkpoint 断点续跑、崩溃自恢复、每轮产物独立落盘。"""
import json
import os
import time

from . import attribute as attribute_mod
from . import decide as decide_mod
from . import experiment as experiment_mod
from . import funnel as funnel_mod
from . import hypothesize as hypothesize_mod
from . import observe as observe_mod
from . import registry
from . import rollback
from .common import (abs_path, atomic_write_json, ensure_dir, fmt_ts,
                     load_json, read_jsonl, stamp)
from .config import runs_dir, state_dir

PHASES = ["observe", "funnel", "attribute", "hypothesize", "decide",
          "experiment", "settle", "rollback", "report"]

ARTIFACT = {
    "observe": "00_observation.json",
    "funnel": "01_funnel.json",
    "attribute": "02_attribution.json",
    "hypothesize": "03_hypotheses.json",
    "decide": "04_decision.json",
    "experiment": "05_experiments.json",
    "settle": "06_registry.json",
    "rollback": "07_rollback.json",
    "report": "08_summary.json",
}


class InjectedFault(RuntimeError):
    pass


def _cp_path(cfg):
    return os.path.join(state_dir(cfg), "checkpoint.json")


def _daemon_path(cfg):
    return os.path.join(state_dir(cfg), "daemon.json")


def _lock(cfg):
    return _FileLock(os.path.join(state_dir(cfg), "evo.lock"),
                     timeout=float(cfg["daemon"]["lock_timeout_s"]))


class _FileLock:
    def __init__(self, p, timeout=5.0):
        self.p = p
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        ensure_dir(os.path.dirname(self.p))
        end = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.p) > 120:
                        os.remove(self.p)
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise TimeoutError("evo lock timeout（另一个轮次在跑）")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
        finally:
            try:
                os.remove(self.p)
            except OSError:
                pass


def _artifact(run_dir, phase):
    return os.path.join(run_dir, ARTIFACT[phase])


def _load(run_dir, phase, default=None):
    return load_json(_artifact(run_dir, phase), default)


def _write(run_dir, phase, obj):
    atomic_write_json(_artifact(run_dir, phase), obj)


def _bump_cycle(cfg, run_id, next_at):
    st = load_json(_daemon_path(cfg), {"cycle": 0})
    st = {"cycle": int(st.get("cycle", 0)) + 1, "last_run_id": run_id,
          "last_round_at": round(time.time(), 3),
          "next_round_at": round(next_at, 3)}
    atomic_write_json(_daemon_path(cfg), st)
    return st


def run_round(cfg, resume=True, fault=None, run_id=None, cycle_index=None):
    """跑一轮；崩溃时写 crashed checkpoint，下次自动从这里续。"""
    with _lock(cfg):
        cp = load_json(_cp_path(cfg), {})
        resumable = (resume and cp and cp.get("status") in ("running", "crashed")
                     and cp.get("run_dir") and os.path.isdir(cp["run_dir"]))
        if resumable:
            run_dir = cp["run_dir"]
            run_id = cp["run_id"]
            phases_done = list(cp.get("phases_done") or [])
            crash_count = int(cp.get("crash_count", 0))
            cycle_index = cp.get("cycle_index") or cycle_index or 1
            resumed = True
        else:
            run_id = run_id or stamp()
            run_dir = ensure_dir(os.path.join(runs_dir(cfg), run_id))
            phases_done = []
            crash_count = 0
            cycle_index = cycle_index or 1
            resumed = False
            cp = {"run_id": run_id, "run_dir": run_dir,
                  "cycle_index": cycle_index, "phases_done": [],
                  "status": "running", "crash_count": 0, "fault": fault}
            atomic_write_json(_cp_path(cfg), cp)

        cp["status"] = "running"
        cp["updated_at"] = round(time.time(), 3)
        cp["fault"] = fault
        atomic_write_json(_cp_path(cfg), cp)

        bundle = _load(run_dir, "observe") if "observe" in phases_done else None
        fun = _load(run_dir, "funnel") if "funnel" in phases_done else None

        try:
            if "observe" not in phases_done:
                _maybe_fault("observe", fault)
                bundle = observe_mod.observe(cfg)
                _write(run_dir, "observe", {
                    "attempts": _slim_rows(bundle["attempts"]),
                    "skips": _slim_rows(bundle["skips"]),
                    "telemetry_events": _slim_rows(
                        (bundle["telemetry_events"] or [])[:500]),
                    "health": bundle["health"], "meta": bundle["meta"],
                    "rollup": bundle["rollup"],
                    "observed_at": bundle["observed_at"],
                })
                _done(cfg, cp, "observe")
                bundle = load_json(_artifact(run_dir, "observe"))

            if "funnel" not in phases_done:
                _maybe_fault("funnel", fault)
                fun = funnel_mod.build(cfg, bundle)
                fun["today"] = time.strftime("%Y-%m-%d")
                _write(run_dir, "funnel", fun)
                _done(cfg, cp, "funnel")
                fun = load_json(_artifact(run_dir, "funnel"))

            if "attribute" not in phases_done:
                _maybe_fault("attribute", fault)
                attr = attribute_mod.attribute(cfg, fun, bundle.get("health"))
                _write(run_dir, "attribute", attr)
                _done(cfg, cp, "attribute")
            attr = load_json(_artifact(run_dir, "attribute"))

            if "hypothesize" not in phases_done:
                _maybe_fault("hypothesize", fault)
                idx = registry.index(cfg)
                cur_n = _replay_count(cfg)
                hyp = hypothesize_mod.generate(
                    cfg, attr, fun, round_no=cycle_index,
                    blocked_signature=lambda s: registry.blocked(cfg, idx, s, cur_n),
                    registry_index=idx)
                hyp["generated_at"] = round(time.time(), 3)
                _write(run_dir, "hypothesize", hyp)
                _done(cfg, cp, "hypothesize")
            hyp = load_json(_artifact(run_dir, "hypothesize"))

            if "decide" not in phases_done:
                _maybe_fault("decide", fault)
                veto_log = []
                dec = decide_mod.select(cfg, hyp["hypotheses"], registry.index(cfg),
                                        fun, run_id, veto_log)
                _write(run_dir, "decide", dec)
                with open(os.path.join(run_dir, "04_vetoes.jsonl"), "w",
                          encoding="utf-8") as f:
                    for v in veto_log:
                        f.write(json.dumps(v, ensure_ascii=False) + "\n")
                _done(cfg, cp, "decide")
            dec = load_json(_artifact(run_dir, "decide"))

            if "experiment" not in phases_done:
                _maybe_fault("experiment", fault)
                replay_rows = read_jsonl(os.path.join(state_dir(cfg),
                                                       "replay.jsonl"))
                exp = {}
                for h in dec.get("selected") or []:
                    exp[h["id"]] = experiment_mod.run(
                        cfg, h, fun, replay_rows, {"vetoed": False})
                _write(run_dir, "experiment", exp)
                _done(cfg, cp, "experiment")
            exp = load_json(_artifact(run_dir, "experiment"), {})

            if "settle" not in phases_done:
                _maybe_fault("settle", fault)
                entries = []
                for h in dec.get("selected") or []:
                    r = exp.get(h["id"]) or {}
                    final = r.get("final") or {}
                    status = final.get("status")
                    conclusion = "inconclusive"
                    reason = final.get("failure_reason") or ""
                    if status == "canary_candidate":
                        reason = "real_stage_disabled:awaiting_operator"
                    elif status == "rejected":
                        conclusion = "rejected"
                    entries.append(registry.make_entry(
                        h, run_id, cycle_index, final.get("level") or "offline",
                        {"planned": h.get("min_sample"),
                         "observed": (r.get("offline") or {}).get("sample")},
                        {"metric": h.get("metric"),
                         "value": (r.get("offline") or {}).get(
                             "observed_before",
                             (r.get("offline") or {}).get("before"))},
                        {"metric": h.get("metric"),
                         "value": (r.get("offline") or {}).get("after")},
                        conclusion, reason))
                written = registry.append_entries(cfg, entries)
                _write(run_dir, "settle", {"entries": entries,
                                           "written": written,
                                           "summary": registry.summary(cfg)})
                _done(cfg, cp, "settle")
            settle = _load(run_dir, "settle", {})

            if "rollback" not in phases_done:
                _maybe_fault("rollback", fault)
                rec = rollback.observe(cfg, fun)
                if rec:
                    registry.append_entries(cfg, [{
                        "schema": "evo-registry/2",
                        "ts": rec["ts"], "run_id": run_id,
                        "round": cycle_index, "experiment_id":
                            f"RB-{rec.get('signature')}-{run_id[-8:]}",
                        "hypothesis": {"id": rec.get("signature"),
                                       "title": "自动回滚"},
                        "params": {"name": rec.get("param"),
                                   "to": (rollback.load(cfg).get("current", {})
                                          .get(rec.get("param"), {})
                                          .get("value"))},
                        "sample_size": {"observed": rec.get("n")},
                        "metric_before": {"metric": rec.get("metric"),
                                          "value": rec.get("baseline")},
                        "metric_after": {"metric": rec.get("metric"),
                                         "value": rec.get("current")},
                        "conclusion": "rolled_back",
                        "failure_reason": f"degrade>{rec.get('threshold')}",
                        "changes": [],
                        "side_effects": [{"metric": rec.get("metric"),
                                          "delta": f"-{rec.get('degrade_rel')}",
                                          "note": "灰度期劣化触发自动回滚"}],
                        "rollback": {"method": "param-overlay",
                                     "ref": "evo/state/params_overlay.json",
                                     "verify": "同台架复测回到基线"},
                        "level": "canary",
                        "signature": rec.get("signature")}])
                _write(run_dir, "rollback", {"rollback": rec,
                                             "overlay": rollback.load(cfg)})
                _done(cfg, cp, "rollback")
            rb = _load(run_dir, "rollback", {})

            if "report" not in phases_done:
                _maybe_fault("report", fault)
                summ = _summary(cfg, run_id, cycle_index, fun, attr, hyp,
                                dec, exp, settle, rb, resumed, crash_count)
                _write(run_dir, "report", summ)
                with open(os.path.join(run_dir, "summary.md"), "w",
                          encoding="utf-8") as f:
                    f.write(_summary_md(summ))
                _done(cfg, cp, "report")

            cp["status"] = "done"
            cp["updated_at"] = round(time.time(), 3)
            cp["finished_at"] = round(time.time(), 3)
            atomic_write_json(_cp_path(cfg), cp)
            interval = float(cfg["daemon"]["cycle_interval_s"])
            daemon = _bump_cycle(cfg, run_id, time.time() + interval)
            return {"ok": True, "run_id": run_id, "run_dir": run_dir,
                    "resumed": resumed, "phases_done": phases_done,
                    "next_round_at": daemon["next_round_at"],
                    "summary_file": os.path.join(run_dir, "summary.md")}

        except Exception as e:  # noqa: BLE001 - 任何崩溃都要留痕并可从 checkpoint 续
            crash_count += 1
            cp["status"] = "crashed"
            cp["crash_count"] = crash_count
            cp["crash_error"] = f"{type(e).__name__}: {e}"
            cp["updated_at"] = round(time.time(), 3)
            atomic_write_json(_cp_path(cfg), cp)
            atomic_write_json(os.path.join(run_dir, "crash.json"), {
                "error": cp["crash_error"], "at": cp["updated_at"],
                "phases_done": cp.get("phases_done"), "run_id": run_id})
            return {"ok": False, "crashed": True, "run_id": run_id,
                    "run_dir": run_dir, "error": cp["crash_error"],
                    "phases_done": cp.get("phases_done")}


def _maybe_fault(phase, fault):
    if fault and fault == phase:
        raise InjectedFault(f"injected_fault_at_{phase}")


def _replay_count(cfg):
    p = os.path.join(state_dir(cfg), "replay.jsonl")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _slim_rows(rows, cap=200):
    out = []
    for r in rows or []:
        b = dict(r)
        if b.get("name"):
            b["name"] = str(b["name"])[:cap]
        out.append(b)
    return out


def _done(cfg, cp, phase):
    cp.setdefault("phases_done", []).append(phase)
    cp["phase"] = phase
    cp["updated_at"] = round(time.time(), 3)
    atomic_write_json(_cp_path(cfg), cp)


def _summary(cfg, run_id, cycle_index, fun, attr, hyp, dec, exp, settle,
             rb, resumed, crash_count):
    return {
        "run_id": run_id,
        "cycle": cycle_index,
        "generated_at": round(time.time(), 3),
        "generated_at_human": fmt_ts(),
        "resumed": resumed,
        "crash_count": crash_count,
        "window_days": fun.get("window_days"),
        "window_end": fun.get("window_end"),
        "funnel_window": {
            "counts": fun["window"]["counts"],
            "stages": fun["window"]["stages"],
            "metrics": fun["window"]["metrics"],
            "settle": fun["window"].get("settle"),
            "time_marks": fun["window"].get("time_marks"),
            "no_evidence": fun["window"].get("no_evidence"),
        },
        "funnel_batch": {
            "counts": fun["batch"]["counts"],
            "metrics": fun["batch"]["metrics"],
            "stages": fun["batch"]["stages"],
            "settle": fun["batch"].get("settle"),
            "time_marks": fun["batch"].get("time_marks"),
            "no_evidence": fun["batch"].get("no_evidence"),
        },
        "health": fun.get("health"),
        "bottleneck": attr.get("bottleneck"),
        "candidates": attr.get("candidates"),
        "evidence_sufficient": attr.get("evidence_sufficient"),
        "hypotheses": hyp.get("hypotheses"),
        "dropped": hyp.get("dropped"),
        "duplicates": hyp.get("duplicates"),
        "decision": {"selected": [h["id"] for h in dec.get("selected") or []],
                     "batch_detail": dec.get("batch_detail"),
                     "vetoed": dec.get("vetoed"),
                     "method": dec.get("method")},
        "experiments": exp,
        "registry": {"written": settle.get("written"),
                     "summary": settle.get("summary")},
        "rollback": rb.get("rollback"),
    }


def _summary_md(s):
    lines = []
    lines.append(f"# EVO 轮次 {s['run_id']}  （cycle {s['cycle']}）")
    lines.append("")
    lines.append(f"- 生成时间：{s['generated_at_human']}")
    lines.append(f"- 断点续跑：{'是' if s['resumed'] else '否'}；崩溃次数：{s['crash_count']}")
    lines.append(f"- 窗口：最近 {s['window_days']} 天（截止 {s['window_end']}）")
    lines.append("")
    lines.append("## 六段漏斗（滚动窗口）")
    lines.append("")
    lines.append("| 段 | 样本量 | 未知 | 占候选比 | 段间转化 | 流失数 | 流失率 | 数据 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for st in s["funnel_window"]["stages"]:
        def pct(v):
            return "-" if v is None else f"{v * 100:.2f}%"
        data_tag = {"measured": "有数据", "partial": "部分未知",
                    "no_evidence": "无证据"}.get(st.get("status"),
                                                  "有数据" if st.get("measured")
                                                  else "无数据")
        lines.append(f"| {st['label']} | {st['count']} | {st.get('unknown', 0)} | "
                     f"{pct(st['rate_of_discover'])} | {pct(st['step_rate'])} | "
                     f"{'-' if st['loss_count'] is None else st['loss_count']} | "
                     f"{pct(st['loss_rate'])} | {data_tag} |")
    lines.append("")
    m = s["funnel_window"]["metrics"]
    dl = m.get("discover_to_listed") or {}
    lc = m.get("listed_to_click") or {}
    dl_p50 = "-" if dl.get("p50_s") is None else f"{dl['p50_s']:.2f}s"
    lc_p50 = "-" if lc.get("p50_s") is None else f"{lc['p50_s']:.2f}s"
    lines.append(
        f"- 候选→挂单 P50={dl_p50} 样本 {dl.get('samples', 0)}"
        f"（{dl.get('status', 'no_evidence')}）"
        f" / 挂单→点进 P50={lc_p50} 样本 {lc.get('samples', 0)}"
        f"（{lc.get('status', 'no_evidence')}）")
    lines.append("")

    def pctm(key):
        v = m.get(key)
        return "-" if v is None else f"{v:.2%}"

    lines.append("## KPI")
    lines.append("")
    lines.append(f"- 列表率 {pctm('list_rate')} / 点进率 {pctm('detail_rate')} / "
                 f"点击率 {pctm('click_rate')} / 可证入池率 {pctm('pool_rate')} / "
                 f"中签率 {pctm('hit_rate')} / 结算率 {pctm('settle_rate')} / "
                 f"端到端 {pctm('end_to_end_rate')}")
    lines.append(f"- 入池未知率 {pctm('pool_unknown_rate')}"
                 f"（{m.get('pool_unknown_count', 0)} 单）/ 额度拦截率 "
                 f"{pctm('block_rate')} / 刷新跳过率 {pctm('refresh_skip_rate')} / "
                 f"未校验中签率 {pctm('unverified_hit_rate')} / "
                 f"中签冲突 {m.get('won_conflict_count', 0)}")
    lines.append("")
    bn = s.get("bottleneck")
    lines.append("## 瓶颈")
    lines.append("")
    if bn:
        flag = "（证据缺失：不计为已确认流失）" if bn.get("no_evidence") else ""
        lines.append(f"- 最大流失段：{bn['from']} → {bn['to']}，流失 {bn['lost']}"
                     f"（流失率 {bn['loss_rate']:.2%}，样本 {bn['n']}）{flag}")
    else:
        lines.append("- 本窗口无流失记录")
    lines.append("")
    lines.append("| 候选因素 | 段 | 效应 | 证据样本 | 置信 | 支撑数据 |")
    lines.append("|---|---|---|---|---|---|")
    for c in s.get("candidates") or []:
        lines.append(f"| {c['label']} | {c['stage']} | {c['effect']:.4f} | "
                     f"{c['evidence_n']} | {c['confidence']} | {c['support']} |")
    lines.append("")
    lines.append("## 假设（本轮）")
    lines.append("")
    hs = s.get("hypotheses") or []
    if not hs:
        lines.append("- 无通过校验的假设")
    for h in hs:
        lines.append(f"- [{h['class']}] {h['title']}｜参数 {h['change']}｜"
                     f"预期 {h['expected_direction']['metric']} "
                     f"{h['expected_direction']['direction']} "
                     f">= {h['expected_direction']['min_delta']}｜"
                     f"最小样本 {h['min_sample']}｜签名 {h['signature']}")
    lines.append("")
    lines.append("## 实验")
    lines.append("")
    for hid, r in (s.get("experiments") or {}).items():
        off = r.get("offline") or {}
        fin = r.get("final") or {}
        lines.append(f"- {hid}: 离线 {off.get('status')} "
                     f"(n={off.get('sample', {}).get('n')}, Δ={off.get('delta')}); "
                     f"影子 {(r.get('shadow') or {}).get('status')}; "
                     f"结论 {fin.get('status')}")
        if fin.get("failure_reason"):
            lines.append(f"  - 原因：{fin['failure_reason']}")
    lines.append("")
    v = (s.get("decision") or {}).get("vetoed") or []
    lines.append(f"## 护栏否决：{len(v)} 条")
    for x in v:
        lines.append(f"- {x.get('title')}: "
                     f"{[c['code'] for c in x.get('vetoes') or []]}")
    lines.append("")
    rb = s.get("rollback")
    lines.append(f"## 回滚：{'触发 ' + rb['param'] if rb else '无'}")
    lines.append("")
    reg = (s.get("registry") or {}).get("summary") or {}
    lines.append(f"## 登记册累计：{reg.get('total', 0)} 条；"
                 f"签名 {reg.get('signatures', 0)} 个；"
                 f"结论分布 {reg.get('by_conclusion') or {}}")
    lines.append("")
    return "\n".join(lines)


def status(cfg):
    cp = load_json(_cp_path(cfg), {})
    daemon = load_json(_daemon_path(cfg), {})
    return {
        "checkpoint": cp,
        "daemon": daemon,
        "cursors": load_json(os.path.join(state_dir(cfg), "cursors.json"), {}),
        "registry": registry.summary(cfg),
        "overlay": rollback.load(cfg),
        "next_round_at": daemon.get("next_round_at"),
    }

