"""回测：用历史数据检验诊断器，不看标准答案。

两类样本：
1. soak 窗口级 —— events.jsonl 只用来切窗口和打分，诊断器读的是 soak.log / cycles /
   metrics / 台账（并且已剔除 fault_active 与 [fault] 行）；
2. RESULT 行级 —— _fi/out 里每一条真实故障行的独立诊断，考硬信号规则。

输出：每类命中率、误报率、混淆对、缺数据项。
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict

from . import engine, rules as R
from . import sources
from .model import Evidence

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "_soak_data")
FIXTURES = r"C:\Users\26346\Documents\Trae-CN\temp_20261107_50\_fi\out"


def _jsonl(path: str) -> list[dict]:
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


def run_soak(book: R.RuleBook, cases: dict) -> dict:
    out = []
    for d in sorted(os.listdir(DATA)):
        path = os.path.join(DATA, d)
        if not os.path.exists(os.path.join(path, "events.jsonl")):
            continue
        bundle = sources.load_bundle(
            logs=[os.path.join(path, "soak.log")],
            cycles=os.path.join(path, "cycles.jsonl"),
            metrics=os.path.join(path, "metrics.csv"),
            ledger=os.path.join(path, "data", "records.jsonl"),
            root=ROOT)
        ev = _jsonl(os.path.join(path, "events.jsonl"))
        faults = [e for e in ev if e["kind"] == "fault_start"]
        recovered = [e for e in ev if e["kind"] == "fault_recovered"]
        spans = [(f["ts"] - 3, f["ts"] + f.get("duration_s", 0) + 12) for f in faults]
        mapped = cases["soak"]
        for f in faults:
            expected = mapped.get(f["type"])
            start, end = f["ts"] - 3, f["ts"] + f.get("duration_s", 0) + 12
            diag = engine.diagnose(bundle, book, start, end, artifacts=path,
                                   label=f"{d}/{f['type']}")
            top = diag.top
            rec = {"dataset": d, "type": f["type"], "expected": expected or [],
                   "verdict": diag.verdict, "top": top.cause if top else None,
                   "top_conf": top.confidence if top else 0.0,
                   "top_label": top.label if top else None,
                   "top1": bool(expected and top and top.cause == expected[0]),
                   "topn": bool(expected and top and top.cause in expected),
                   "missing": diag.missing[:2]}
            out.append(rec)
        for r in recovered:
            if not faults:
                # 没有 fault_start 就切不出真实注入窗口，任何「安静」都可能落在别的故障里
                continue
            start, end = r["ts"] + 12, r["ts"] + 42
            # 只把真正安静的时段算误报：与任何注入窗口重叠的时段不算
            if any(s <= end and start <= e for s, e in spans):
                continue
            diag = engine.diagnose(bundle, book, start, end, artifacts=path,
                                   label=f"{d}/quiet")
            top = diag.top
            out.append({"dataset": d, "type": "_quiet", "expected": [],
                        "verdict": diag.verdict, "top": top.cause if top else None,
                        "top_conf": top.confidence if top else 0.0,
                        "top_label": top.label if top else None,
                        "false_positive": diag.verdict == "conclusive" and top is not None
                        and top.confidence >= engine.MIN_CONF,
                        "missing": diag.missing[:2]})
    return {"rows": out}


def run_lines(book: R.RuleBook, cases: dict, fixtures: str) -> dict:
    rows = []
    for c in cases["lines"]:
        path = os.path.join(fixtures, c["file"])
        if not os.path.exists(path):
            rows.append({"case": c["case"], "cause": c["cause"], "status": "NOFILE"})
            continue
        lines = sources.read_text(path)[0]
        idx = next((i for i, ln in enumerate(lines, 1) if c["case"] in ln), None)
        if idx is None:
            rows.append({"case": c["case"], "cause": c["cause"], "status": "NOCASE"})
            continue
        b = sources.SourceBundle(logs=[sources.LogLine(c["file"], idx, None, lines[idx - 1])],
                                 counts={"log": 1, "cycles": 0, "metrics": 0, "ledger": 0, "ledger_bad": 0})
        d = engine.diagnose(b, book, None, None)
        top = d.top
        rows.append({"case": c["case"], "cause": c["cause"],
                     "status": "PASS" if (top and top.cause == c["cause"]) else "MISS",
                     "predicted": top.cause if top else None,
                     "predicted_label": top.label if top else None,
                     "confidence": top.confidence if top else 0.0,
                     "line": f'{c["file"]}:{idx}',
                     "verdict": d.verdict})
    return {"rows": rows}


def _bucket(r: dict) -> str:
    """单条注入窗口的归类（互斥，避免 top1/abstain 重叠计数）。"""
    v = r["verdict"]
    if v == "conclusive":
        return "hit" if r["topn"] else "wrong"
    if v == "ambiguous":
        return "ambiguous"
    if v == "conflict":
        return "conflict"
    if v == "insufficient":
        return "insufficient"
    return "blind"          # normal：没命中任何规则，但也不能说健康


def summarize(w: dict, l: dict) -> dict:
    rows = w["rows"]
    per = defaultdict(Counter)
    for r in rows:
        if r["type"] == "_quiet":
            continue
        k = r["type"]
        b = _bucket(r)
        per[k]["n"] += 1
        per[k][b] += 1
        if b == "hit" and r["top1"]:
            per[k]["top1"] += 1
        if b == "wrong":
            per[k][f"->{r['top']}"] += 1
    quiet = [r for r in rows if r["type"] == "_quiet"]
    fp = [r for r in quiet if r.get("false_positive")]
    lines = l["rows"]
    ok = [r for r in lines if r["status"] == "PASS"]
    total = Counter()
    for k, v in per.items():
        for key in ("n", "hit", "wrong", "ambiguous", "conflict", "insufficient", "blind", "top1"):
            total[key] += v.get(key, 0)
    return {
        "soak": {k: dict(v) for k, v in sorted(per.items())},
        "soak_total": dict(total),
        "quiet": {"n": len(quiet), "fp": len(fp),
                  "fp_rate": round(len(fp) / len(quiet), 3) if quiet else 0.0,
                  "fp_causes": dict(Counter(r["top"] for r in fp))},
        "lines": {"n": len(lines), "pass": len(ok),
                  "rate": round(len(ok) / len(lines), 3) if lines else 0.0},
    }


def render(summary: dict) -> str:
    out = ["== soak 窗口级（每个注入一次，互斥归类） =="]
    out.append(f"{'注入类型':<18}{'次':>3}{'命中':>5}{'误判':>5}{'歧义':>5}{'无据':>5}{'盲区':>5}  误判去向")
    for k, v in summary["soak"].items():
        wrong = ", ".join(f"{x[2:]}×{n}" for x, n in v.items() if x.startswith("->"))
        out.append(f"{k:<18}{v.get('n',0):>3}{v.get('hit',0):>5}{v.get('wrong',0):>5}"
                   f"{v.get('ambiguous',0):>5}{v.get('insufficient',0):>5}{v.get('blind',0):>5}  {wrong}")
    t = summary["soak_total"]
    n = t.get("n", 0)
    out.append("")
    out.append(f"合计 {n} 个窗口：命中 {t.get('hit',0)}（{t.get('hit',0)/n:.0%}），"
               f"误判 {t.get('wrong',0)}（{t.get('wrong',0)/n:.0%}），"
               f"歧义 {t.get('ambiguous',0)}，无据 {t.get('insufficient',0)}，盲区 {t.get('blind',0)}")
    q = summary["quiet"]
    out.append("")
    out.append(f"== 安静窗口（恢复后 30s，已剔除与注入重叠的时段） ==")
    out.append(f"样本 {q['n']}，误报 {q['fp']}，误报率 {q['fp_rate']:.0%}  {q['fp_causes']}")
    l = summary["lines"]
    out.append("")
    out.append(f"== RESULT 行级 ==")
    out.append(f"样本 {l['n']}，命中 {l['pass']}（{l['rate']:.0%}）")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="诊断器回测")
    ap.add_argument("--fixtures", default=FIXTURES)
    ap.add_argument("--rules", default=os.path.join(ROOT, "evo", "diagnose", "rules"))
    ap.add_argument("--json", dest="json_path")
    args = ap.parse_args(argv)

    book = R.load_rules(args.rules, ROOT)
    if book.issues:
        print("规则自检未通过：" + "; ".join(book.issues))
        return 2
    cases = json.load(open(os.path.join(args.rules, "bench_cases.json"), encoding="utf-8"))
    w = run_soak(book, cases)
    l = run_lines(book, cases, args.fixtures)
    s = summarize(w, l)
    print(render(s))
    print()
    print("== 逐条 ==")
    for r in w["rows"]:
        if r["type"] != "_quiet":
            print(f"{r['dataset']:<9}{r['type']:<16}{r['verdict']:<13}"
                  f"{(r['top'] or '-'):<38}{r['top_conf']:.2f}"
                  f"  {'OK' if r['topn'] else 'X'}")
    print()
    for r in l["rows"]:
        print(f"{r['case']:<34}{r['status']:<6}{str(r['predicted']):<38}{r.get('confidence', 0):.2f}"
              f"  {r.get('line', '')}")
    if args.json_path:
        json.dump({"summary": s, "soak": w["rows"], "lines": l["rows"]},
                  open(args.json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
