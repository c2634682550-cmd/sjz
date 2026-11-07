"""命令行入口。

python -m evo.cli dryrun            # 真实台账 dry 跑一轮
python -m evo.cli cycle --once      # 守护跑一轮（可 --fault 注入崩溃验证续跑）
python -m evo.cli cycle --loop      # 24h 循环
python -m evo.cli status            # 游标/checkpoint/登记册
python -m evo.cli selftest          # 夹具自检（漏斗数学/护栏/回滚/续跑）
"""
import argparse
import json
import os
import sys
import time

from . import daemon, funnel as funnel_mod, registry
from .config import load_config
from .daemon import PHASES


def _p(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_dryrun(cfg, args):
    res = daemon.run_round(cfg, resume=not args.fresh, cycle_index=1)
    if not res.get("ok"):
        print("dry run 崩溃：", res.get("error"))
        print("可执行 `python -m evo.cli cycle --resume` 续跑。")
        return 2
    summ = json.load(open(os.path.join(res["run_dir"], "08_summary.json"),
                          encoding="utf-8"))
    _print_summary(summ)
    print("\n产物目录：", res["run_dir"])
    reg = registry.summary(cfg)
    print(f"登记册：{reg['total']} 条，签名 {reg['signatures']} 个，"
          f"结论分布 {reg['by_conclusion']}")
    return 0


def cmd_cycle(cfg, args):
    if args.loop:
        cycles = 0
        while True:
            cycles += 1
            res = daemon.run_round(cfg, resume=not args.fresh)
            print(f"[cycle {cycles}] ok={res.get('ok')} run={res.get('run_id')}"
                  f" resumed={res.get('resumed')}")
            if not res.get("ok") and args.auto_heal:
                print("检测到崩溃，自动从 checkpoint 续跑 ...")
                res = daemon.run_round(cfg, resume=True)
                print(f"[heal] ok={res.get('ok')} phases={res.get('phases_done')}")
            if args.max_cycles and cycles >= args.max_cycles:
                break
            nxt = res.get("next_round_at") or (time.time() + 86400)
            wait = max(1.0, nxt - time.time())
            print(f"下一轮 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(nxt))}"
                  f"，等待 {wait:.0f}s")
            while wait > 0:
                time.sleep(min(60.0, wait))
                wait = nxt - time.time()
        return 0

    res = daemon.run_round(cfg, resume=not args.fresh, fault=args.fault)
    if not res.get("ok"):
        print("崩溃：", res.get("error"))
        print("已写 checkpoint，phases_done=", res.get("phases_done"))
        if args.auto_heal:
            heal = daemon.run_round(cfg, resume=True)
            print("自恢复：", heal.get("ok"), heal.get("error") or "")
            if heal.get("ok"):
                print("续跑完成：", heal.get("summary_file"))
                return 0
        return 2
    print("完成：", res.get("run_id"), "resumed=", res.get("resumed"))
    print("产物：", res.get("run_dir"))
    return 0


def cmd_status(cfg, args):
    _p(daemon.status(cfg))
    return 0


def _pct_or_dash(v):
    return "-" if v is None else f"{v:.2%}"


def _print_summary(s):
    fw = s["funnel_window"]
    print("=" * 72)
    print(f"EVO 轮次 {s['run_id']} | cycle {s['cycle']} | "
          f"窗口 {s['window_days']} 天（截止 {s['window_end']}）")
    print("=" * 72)
    print(f"{'阶段':<6}{'样本量':>8}{'占发现':>10}{'段间转化':>10}"
          f"{'流失数':>8}{'流失率':>10}  数据")
    for st in fw["stages"]:
        def pct(v):
            return "-" if v is None else f"{v * 100:.2f}%"
        tag = {"measured": "有数据", "partial": "部分未知",
               "no_evidence": "无证据"}.get(st.get("status"), "-")
        print(f"{st['label']:<6}{st['count']:>8}{pct(st['rate_of_discover']):>12}"
              f"{pct(st['step_rate']):>12}"
              f"{('-' if st['loss_count'] is None else st['loss_count']):>8}"
              f"{pct(st['loss_rate']):>12}  {tag}")
    m = fw["metrics"]
    print(f"\nKPI 点击 {_pct_or_dash(m['click_rate'])} | "
          f"进池 {_pct_or_dash(m['pool_rate'])} | "
          f"中签 {_pct_or_dash(m['hit_rate'])} | "
          f"结算 {_pct_or_dash(m['settle_rate'])} | "
          f"端到端 {_pct_or_dash(m['end_to_end_rate'])}")
    print(f"    拦截 {_pct_or_dash(m['block_rate'])} | 刷新跳过 "
          f"{_pct_or_dash(m['refresh_skip_rate'])} | 未校验中签 "
          f"{_pct_or_dash(m['unverified_hit_rate'])} | "
          f"结算证据 {m.get('settle_evidence_n', 0)} 条 / 未知 "
          f"{m.get('settle_unknown_n', 0)} 条")
    bn = s.get("bottleneck")
    if bn:
        print(f"\n瓶颈：{bn['from']} -> {bn['to']} 流失 {bn['lost']} "
              f"({bn['loss_rate']:.2%}, n={bn['n']})")
    print("\n归因候选：")
    for c in s.get("candidates") or []:
        print(f"  - {c['label']} [{c['stage']}] effect={c['effect']:.4f} "
              f"n={c['evidence_n']} ({c['confidence']}) {c['support']}")
    print("\n假设：")
    for h in s.get("hypotheses") or []:
        print(f"  - [{h['class']}] {h['title']} | {h['change']} | "
              f"预期 {h['expected_direction']['direction']} "
              f"{h['expected_direction']['min_delta']} | "
              f"n>={h['min_sample']} | {h['signature']}")
    if not s.get("hypotheses"):
        print("  （无）")
    print("\n实验结论：")
    for hid, r in (s.get("experiments") or {}).items():
        off = r.get("offline") or {}
        fin = r.get("final") or {}
        print(f"  - {hid}: 离线={off.get('status')} "
              f"n={((off.get('sample') or {}).get('n'))} "
              f"Δ={off.get('delta')} 影子="
              f"{(r.get('shadow') or {}).get('status')} 结论={fin.get('status')}")
        if fin.get("failure_reason"):
            print(f"      原因: {fin['failure_reason']}")
    v = (s.get("decision") or {}).get("vetoed") or []
    print(f"\n护栏否决 {len(v)} 条；回滚 "
          f"{'触发' if s.get('rollback') else '无'}")
    reg = (s.get("registry") or {}).get("summary") or {}
    print(f"登记册累计 {reg.get('total')} 条 / 签名 {reg.get('signatures')} | "
          f"{reg.get('by_conclusion')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="evo")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("dryrun", help="用现有台账跑一轮")
    d.add_argument("--fresh", action="store_true", help="忽略旧 checkpoint")

    c = sub.add_parser("cycle", help="守护轮次")
    c.add_argument("--once", action="store_true")
    c.add_argument("--loop", action="store_true")
    c.add_argument("--fresh", action="store_true")
    c.add_argument("--fault", choices=PHASES, help="注入崩溃，验证断点续跑")
    c.add_argument("--auto-heal", action="store_true", help="崩溃后自动续跑")
    c.add_argument("--max-cycles", type=int, default=0)

    sub.add_parser("status", help="查看游标/checkpoint/登记册")
    sub.add_parser("selftest", help="夹具自检")

    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd == "dryrun":
        return cmd_dryrun(cfg, args)
    if args.cmd == "cycle":
        if not args.once and not args.loop:
            args.loop = False
        return cmd_cycle(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "selftest":
        from .selftest import run_selftest
        return run_selftest()
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
