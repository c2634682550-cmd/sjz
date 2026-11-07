"""形状与自愈度量：给迭代台账提供可复现的「流程步骤/分支/自愈覆盖率」口径。

只读，不写仓库；不截图、不发输入、不开窗口。

用法：
  python tools/loop_metrics.py                # 人读
  python tools/loop_metrics.py --json         # 机读（台账直接贴）
  python tools/loop_metrics.py --sim-n 50     # 缩小仿真轮数，快速冒烟
  python tools/loop_metrics.py --engine <snap>\\core\\engine.py   # 对冻结快照计分支

口径：
  flow.stages      生产抢购单件的固定阶段，取自 perf/bench/flow_group.py 的计时键
  flow.branches    core/engine.py 主链 6 个函数的 ast.If 计数（决策点数）
  recovery.*       场景数 / 原子动作数 / 故障注入仿真的自动恢复率
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STAGES = ["capture_list", "scan_list", "pick", "enter_click",
          "detail_ocr", "fire_frame", "confirm_click"]
LIST_CHAIN = ["_execute_list", "_fire_and_confirm",
              "_bought_dialog", "_wait_dialog_unlock"]
COUNTDOWN_CHAIN = ["_countdown_prepare", "_watch_countdown"]


def flow_shape(engine_path=None):
    src = open(engine_path or os.path.join(ROOT, "core", "engine.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in LIST_CHAIN + COUNTDOWN_CHAIN:
                funcs[node.name] = {
                    "lines": node.end_lineno - node.lineno + 1,
                    "if": sum(1 for n in ast.walk(node) if isinstance(n, ast.If)),
                    "loop": sum(1 for n in ast.walk(node)
                                if isinstance(n, (ast.For, ast.While))),
                    "try": sum(1 for n in ast.walk(node) if isinstance(n, ast.Try)),
                }
    return {
        "stages": STAGES,
        "stages_n": len(STAGES),
        "grab_per_item": 3,
        "click_per_item": 2,
        "branches": funcs,
        "list_chain_if": sum(funcs.get(n, {}).get("if", 0) for n in LIST_CHAIN),
        "countdown_chain_if": sum(funcs.get(n, {}).get("if", 0)
                                  for n in COUNTDOWN_CHAIN),
        "branches_normalized_if_total": sum(v["if"] for v in funcs.values()),
    }


def recovery_shape(sim_n):
    from core import recovery, scene_guard as sg
    from tests import scene_sim as sim
    from tests.test_recovery import independent_goal

    stats = sim.run_all(sim_n, oracle=independent_goal)
    totals = {"auto": 0, "manual_continue": 0, "fail": 0}
    per_scene = {}
    false_success = 0
    for sid, row in stats.items():
        c = row["counts"]
        for k in totals:
            totals[k] += c.get(k, 0)
        n = sum(c.values()) or 1
        per_scene[sid] = {"auto_rate": round(c.get("auto", 0) / n, 3)}
        false_success += int(row.get("false_success") or 0)
    trials = sum(totals.values()) or 1
    return {
        "scenes": len(sg.MATRIX_SCENES),
        "sim_scenes": len(stats),
        "actions": len(recovery.ACTIONS),
        "actions_enabled": sum(1 for a in recovery.ACTIONS.values() if a.enabled),
        "sim_trials": trials,
        "sim_auto": totals["auto"],
        "sim_manual_continue": totals["manual_continue"],
        "sim_fail": totals["fail"],
        "sim_auto_rate": round(totals["auto"] / trials, 4),
        "sim_false_success": false_success,
        "per_scene_auto_rate": per_scene,
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--sim-n", type=int, default=200)
    p.add_argument("--engine", default=None,
                   help="指定 core/engine.py（对冻结快照计分支，避免主树漂移）")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    out = {"flow": flow_shape(args.engine), "recovery": recovery_shape(args.sim_n)}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    f, r = out["flow"], out["recovery"]
    print("== 流程形状 ==")
    print(f"阶段 {f['stages_n']}：" + " -> ".join(f["stages"]))
    print(f"每件动作：抓屏 {f['grab_per_item']} / 点击 {f['click_per_item']} / OCR 1")
    for name, v in f["branches"].items():
        print(f"  {name:22s} {v['lines']:4d} 行  if={v['if']:3d} loop={v['loop']}")
    print(f"分支：列表链 {f['list_chain_if']} + 倒计时链 "
          f"{f['countdown_chain_if']} = {f['branches_normalized_if_total']}")
    print("\n== 自愈覆盖 ==")
    print(f"场景 {r['scenes']}（仿真含 config_fault：{r['sim_scenes']}）"
          f" / 原子动作 {r['actions']}（启用 {r['actions_enabled']}）")
    print(f"注入 {r['sim_trials']} 次：自动 {r['sim_auto']} / 转人工 "
          f"{r['sim_manual_continue']} / 失败 {r['sim_fail']}"
          f" → 自动率 {r['sim_auto_rate']:.1%}")
    print(f"误报成功 {r['sim_false_success']} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
