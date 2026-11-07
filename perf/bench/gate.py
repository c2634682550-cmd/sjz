"""守门：拿基线（baseline.json）对候选补丁的结果做红线判定 + 全指标差异表。

退出码：
  0  通过（红线不退化，非红线指标允许小幅波动）
  1  阻断（任一红线退化 —— 不看收益多大）
  2  证据不全（红线指标缺失，按 fail-closed 处理）

用法：
  python perf/bench/gate.py --base perf/bench/baseline.json --cand out/xxx/perf.json
  python perf/bench/gate.py --converge out/r1/perf.json out/r2/perf.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from common import flatten, load_json


def _at(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _value(doc, key):
    """red-line 取值：accuracy.* 来自结果文件，redlines.* 来自旁证 JSON。"""
    if key.startswith("accuracy."):
        return _at(doc, key)
    return _at(doc, key)


def _pct_delta(base, cand):
    if base in (None, 0) or cand is None:
        return None
    return (cand - base) / base * 100.0


def check(base, cand, redlines, criteria):
    problems, missing, table = [], [], []
    base_rl = base.get("redlines") or {}
    cand_rl = dict(cand.get("redlines") or {})
    cand_rl.update(redlines or {})

    for rule in criteria.get("red_lines", []):
        rid = rule["id"]
        is_acc = rid.startswith("accuracy.")
        key = rid.split(".", 1)[1]
        if is_acc:
            braw = (base.get("accuracy") or {}).get(key)
            craw = (cand.get("accuracy") or {}).get(key)
        else:
            braw = base_rl.get(key)
            craw = cand_rl.get(key)
        if isinstance(braw, dict):
            braw = braw.get("value")
        if isinstance(craw, dict):
            craw = craw.get("value")

        if craw is None:
            missing.append(rid)
            table.append((rid, braw, None, None, "MISSING"))
            continue

        direction = rule.get("dir")
        verdict = "PASS"
        if direction == "max":
            floor = rule.get("floor")
            tol = float(rule.get("tolerance") or 0.0)
            if floor is not None and craw < floor:
                verdict = f"FAIL (<floor {floor})"
            elif braw is not None and craw < braw - tol:
                verdict = "FAIL (退化)"
        elif direction == "min":
            tol = float(rule.get("tolerance") or 0.0)
            if braw is not None and craw > braw + tol:
                verdict = "FAIL (退化)"
        elif direction == "bool":
            if not bool(craw):
                verdict = "FAIL"
        elif direction == "enum":
            allow = rule.get("allow") or []
            if craw not in allow:
                verdict = "FAIL (非法身份)"
            if rule.get("no_downgrade_from") and braw and craw != braw:
                verdict = "FAIL (通道降级)"

        if verdict != "PASS":
            problems.append((rid, braw, craw, verdict))
        delta = _pct_delta(braw if isinstance(braw, (int, float)) else None,
                           craw if isinstance(craw, (int, float)) else None)
        table.append((rid, braw, craw, delta, verdict))

    # 非红线指标：只报告，不阻断
    fb, fc = flatten(base), flatten(cand)
    for key in sorted(fc):
        if key.startswith("accuracy."):
            continue
        v = fc[key]
        if not isinstance(v, dict) or v.get("p50") is None:
            continue
        b = fb.get(key)
        if not isinstance(b, dict) or b.get("p50") is None:
            continue
        for stat in ("p50", "p95"):
            bv, cv = b.get(stat), v.get(stat)
            d = _pct_delta(bv, cv)
            if d is None:
                continue
            tag = "regress" if d > 3.0 else ("improve" if d < -3.0 else "flat")
            table.append((f"{key}.{stat}", bv, cv, d, tag))

    return problems, missing, table


def main(argv=None):
    p = argparse.ArgumentParser(description="性能红线与差异守门")
    p.add_argument("--base", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json"))
    p.add_argument("--cand", default="", help="候选补丁的 perf.json")
    p.add_argument("--redlines", default="", help="redlines.py 产出的旁证 JSON")
    p.add_argument("--criteria", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "criteria.json"))
    p.add_argument("--converge", nargs=2, metavar=("R1", "R2"), help="收敛判定：两轮结果对比")
    p.add_argument("--json-out", default="", help="把差异表写成 JSON")
    args = p.parse_args(argv)
    if not args.converge and not args.cand:
        p.error("需要 --cand（或 --converge）")

    criteria = load_json(args.criteria)
    if args.converge:
        a, b = load_json(args.converge[0]), load_json(args.converge[1])
        fa, fb = flatten(a), flatten(b)
        rows, converged = [], True
        for key in sorted(fb):
            va, vb = fa.get(key), fb.get(key)
            if not isinstance(va, dict) or not isinstance(vb, dict):
                continue
            if va.get("p50") is None or vb.get("p50") is None:
                continue
            d = _pct_delta(va["p50"], vb["p50"])
            if d is None:
                continue
            rows.append((key, va["p50"], vb["p50"], d))
            thr = float(criteria.get("convergence", {}).get("improve_pct", 3.0))
            if d < -thr:
                converged = False
        print(f"{'指标':44s} {'上一轮 p50':>12s} {'本轮 p50':>12s} {'变化%':>9s}")
        for key, va, vb, d in rows:
            print(f"{key:44s} {va:12.2f} {vb:12.2f} {d:8.2f}%")
        print("\n收敛：", "是（全指标改善均 < 3%）" if converged else "否（仍有指标改善 ≥ 3%）")
        return 0 if converged else 1

    base = load_json(args.base)
    cand = load_json(args.cand)
    redlines = load_json(args.redlines) if args.redlines and os.path.exists(args.redlines) else {}

    problems, missing, table = check(base, cand, redlines, criteria)
    print(f"{'指标':46s} {'基线':>10s} {'候选':>10s} {'变化%':>9s}  {'判定':s}")
    for key, b, c, d, verdict in table:
        bs = "-" if b is None else (f"{b:.4g}" if isinstance(b, (int, float)) else str(b))
        cs = "-" if c is None else (f"{c:.4g}" if isinstance(c, (int, float)) else str(c))
        ds = "-" if d is None else f"{d:.2f}"
        print(f"{key:46s} {bs:>10s} {cs:>10s} {ds:>9s}  {verdict}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"problems": problems, "missing": missing,
                       "table": [{"id": k, "base": b, "cand": c, "delta_pct": d,
                                  "verdict": v} for k, b, c, d, v in table]},
                      f, ensure_ascii=False, indent=2)

    print()
    if problems:
        for rid, b, c, v in problems:
            print(f"[阻断] {rid}: 基线 {b} → 候选 {c}（{v}）")
        return 1
    if missing:
        print("[证据不全] " + "，".join(missing) + "  按 fail-closed 处理：补证据后再放行")
        return 2
    print("[通过] 红线全部达标，可以应用该补丁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
