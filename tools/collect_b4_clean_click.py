# -*- coding: utf-8 -*-
"""B4 干净机点击占用复测：先检测本机有没有 pytest / click_main / bench 负载，
有负载直接拒绝出结论（exit 2），避免把「机器在忙」误读成「点击变慢」。

本脚本**不动鼠标**、不开窗口；它只做环境门禁 + 可选调用既有台架。

用法：
  python tools/collect_b4_clean_click.py                  # 只体检
  python tools/collect_b4_clean_click.py --selftest       # 合成进程表自检
  python tools/collect_b4_clean_click.py --exec "python perf/bench/click_perf.py --run 100"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _perf_collect_common import PROJ, default_out_dir, write_report  # noqa: E402

# 任何命中都视为「机器不干净」
LOAD_PATTERNS = (
    "pytest", "click_main", "click_perf", "flow_bench", "bench_",
    "soak", "py-spy", "perf\\bench", "perf/bench", "collect_b4",
)


def list_processes():
    """返回 [{pid,name,cmdline}]，按可用性依次尝试 psutil → CIM → tasklist。"""
    # 1) psutil
    try:
        import psutil
        out = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            info = p.info
            out.append({
                "pid": info.get("pid"),
                "ppid": p.ppid(),
                "name": info.get("name") or "",
                "cmdline": " ".join(info.get("cmdline") or []),
            })
        return out, "psutil"
    except Exception:
        pass

    # 2) PowerShell CIM（Windows 本机，能拿到命令行）
    try:
        cmd = ("Get-CimInstance Win32_Process | "
               "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
               "ConvertTo-Json -Compress")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=25,
                           encoding="utf-8", errors="ignore")
        raw = (r.stdout or "").strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            return [{"pid": d.get("ProcessId"),
                     "ppid": d.get("ParentProcessId"),
                     "name": d.get("Name") or "",
                     "cmdline": d.get("CommandLine") or ""} for d in data], "cim"
    except Exception:
        pass

    # 3) tasklist（只有进程名，没有命令行）
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=25,
                           encoding="utf-8", errors="ignore")
        out = []
        for line in (r.stdout or "").splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) < 2:
                continue
            out.append({"pid": parts[1], "ppid": None, "name": parts[0].strip('"'),
                        "cmdline": ""})
        return out, "tasklist"
    except Exception:
        return [], "none"


def ancestors(procs, self_pid, max_depth=12):
    """沿 ParentProcessId 上溯，返回 {self_pid, 祖先进程 pid}。

    目的：排除「启动本脚本的终端/外层 shell」——它们的命令行里会带
    `collect_b4` 字样，不排除就会把每次运行都误判成 DIRTY。
    """
    parent = {}
    for p in procs:
        try:
            parent[int(p.get("pid"))] = int(p.get("ppid")) if p.get("ppid") else None
        except (TypeError, ValueError):
            continue
    out = set()
    cur = int(self_pid)
    for _ in range(max_depth):
        out.add(cur)
        nxt = parent.get(cur)
        if not nxt or nxt in out:
            break
        cur = nxt
    return out


def is_python_like(p):
    name = (p.get("name") or "").lower()
    cmd = (p.get("cmdline") or "").lower()
    return ("python" in name or "pytest" in name or "pypy" in name
            or "python" in cmd.split(" ")[0] or "pytest" in cmd.split(" ")[0])


def classify(procs, self_pid=None, exclude_pids=None):
    skip = set(exclude_pids or ())
    if self_pid is not None:
        skip.add(int(self_pid))
    hits = []
    for p in procs:
        try:
            if int(p.get("pid")) in skip:
                continue
        except (TypeError, ValueError):
            pass
        blob = ("%s %s" % (p.get("name") or "", p.get("cmdline") or "")).lower()
        matched = [pat for pat in LOAD_PATTERNS if pat.lower() in blob]
        if matched and not is_python_like(p):
            # 外层 shell/终端只是命令行文本里带了路径，不是负载本身；
            # 真正跑 pytest/bench 的是它下面的 python 进程，会单独命中。
            matched = []
        if matched:
            hits.append({
                "pid": p.get("pid"),
                "name": p.get("name"),
                "cmdline": (p.get("cmdline") or "")[:160],
                "matched": matched,
            })
    status = "DIRTY" if hits else "CLEAN"
    return status, hits


def cpu_snapshot():
    """可选 CPU 占用；没有 psutil 就返回 None。"""
    try:
        import psutil
        return round(psutil.cpu_percent(interval=1.0), 1)
    except Exception:
        return None


def build_report(status, hits, source, cpu, args, exec_info=None, procs=None):
    procs = procs or []
    py = [p for p in procs if "python" in (p.get("name") or "").lower()]
    return {
        "script": "B4_clean_click",
        "objective": "干净机点击占用复测的环境门禁",
        "process_source": source,
        # 口径要求：报占用时必须同时记录当时的并发进程数
        "process_count": len(procs),
        "python_count": len(py),
        "load_count": len(hits),
        "cpu_percent_1s": cpu,
        "status": status,
        "load_hits": hits,
        "conclusion": (
            "拒绝出结论：检测到 %d 个负载进程，先在干净机复测" % len(hits)
            if status == "DIRTY" else
            "环境干净，可以复测点击占用"),
        "exec": exec_info,
        "out_dir": args.out_dir,
    }


def render(rep):
    lines = ["B4 干净机点击占用复测（环境门禁）",
             "进程来源: %s" % rep["process_source"],
             "并发进程数: %d（python %d）" % (rep["process_count"],
                                            rep["python_count"]),
             "命中负载进程数: %d" % rep["load_count"],
             "CPU 1s: %s" % ("n/a" if rep["cpu_percent_1s"] is None
                             else "%s%%" % rep["cpu_percent_1s"]),
             "状态: %s" % rep["status"]]
    for h in rep["load_hits"]:
        lines.append("  负载 pid=%s name=%s cmd=%s" %
                     (h["pid"], h["name"], h["cmdline"]))
    lines.append("结论: %s" % rep["conclusion"])
    if rep.get("exec"):
        e = rep["exec"]
        lines.append("复测命令: %s" % e["cmd"])
        lines.append("耗时: %.2f s  exit=%s" % (e["elapsed_s"], e["exit"]))
        if e.get("tail"):
            lines.append("输出尾部:")
            lines.extend("  " + x for x in e["tail"])
    return lines


def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))

    clean = [
        {"pid": 100, "name": "explorer.exe", "cmdline": ""},
        {"pid": 101, "name": "python.exe", "cmdline": "python tools/collect_b1_gate_p95.py"},
        {"pid": 102, "name": "chrome.exe", "cmdline": "--type=renderer"},
    ]
    dirty = clean + [
        {"pid": 201, "name": "python.exe",
         "cmdline": "python -m pytest tests -q"},
        {"pid": 202, "name": "python.exe",
         "cmdline": "python perf\\bench\\click_perf.py --run 100"},
    ]
    s1, h1 = classify(clean)
    s2, h2 = classify(dirty)
    check("干净进程表 -> CLEAN", s1 == "CLEAN" and not h1, str(h1))
    check("pytest 被识别 -> DIRTY", s2 == "DIRTY" and len(h2) >= 1, str(h2))
    check("click_perf 被识别", any("click_perf" in h["matched"] for h in h2))
    check("自身进程被排除", all(x["pid"] != 999 for x in
                                classify([{"pid": 999, "name": "python.exe",
                                           "cmdline": "collect_b4 --selftest"}],
                                          self_pid=999)[1]))
    # 祖先链排除：外层终端命令行里含 collect_b4，不应算负载
    chain = [
        {"pid": 999, "ppid": 998, "name": "python.exe",
         "cmdline": "python tools/collect_b4_clean_click.py"},
        {"pid": 998, "ppid": 997, "name": "powershell.exe",
         "cmdline": "powershell -Command python tools/collect_b4_clean_click.py"},
        {"pid": 997, "ppid": None, "name": "explorer.exe", "cmdline": ""},
        {"pid": 301, "ppid": 997, "name": "python.exe",
         "cmdline": "python -m pytest tests -q"},
    ]
    skip = ancestors(chain, 999)
    s_chain, h_chain = classify(chain, exclude_pids=skip)
    check("祖先链被排除且保留真实负载",
          skip == {999, 998, 997} and s_chain == "DIRTY"
          and [h["pid"] for h in h_chain] == [301],
          "skip=%s hits=%s" % (sorted(skip), [h["pid"] for h in h_chain]))
    check("DIRTY 拒绝结论文案", "拒绝出结论" in
          build_report("DIRTY", h2, "stub", 3.0, argparse.Namespace(out_dir=None),
                       procs=dirty)["conclusion"])
    r_clean = build_report("CLEAN", h1, "stub", 1.0,
                           argparse.Namespace(out_dir=None), procs=clean)
    r_dirty = build_report("DIRTY", h2, "stub", 3.0,
                           argparse.Namespace(out_dir=None), procs=dirty)
    check("并发进程数记录", r_clean["process_count"] == 3
          and r_dirty["process_count"] == 5,
          "clean=%d dirty=%d" % (r_clean["process_count"],
                                 r_dirty["process_count"]))
    check("负载进程数与命中数一致", r_clean["load_count"] == 0
          and r_dirty["load_count"] == len(h2),
          "clean=%d dirty=%d" % (r_clean["load_count"], r_dirty["load_count"]))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="B4 干净机点击占用复测门禁")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--exec", dest="exec_cmd", default=None,
                    help="环境干净时才执行的复测命令")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    procs, source = list_processes()
    skip = ancestors(procs, os.getpid())
    status, hits = classify(procs, exclude_pids=skip)
    cpu = cpu_snapshot()
    args.out_dir = args.out_dir or default_out_dir("b4_clean_click")

    exec_info = None
    if status == "CLEAN" and args.exec_cmd:
        t0 = time.perf_counter()
        r = subprocess.run(args.exec_cmd, shell=True, cwd=PROJ,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="ignore")
        tail = [x for x in (r.stdout or "").strip().splitlines()[-8:]]
        exec_info = {"cmd": args.exec_cmd,
                     "elapsed_s": round(time.perf_counter() - t0, 2),
                     "exit": r.returncode, "tail": tail}

    rep = build_report(status, hits, source, cpu, args, exec_info, procs=procs)
    lines = render(rep)
    print("\n".join(lines))
    jp, tp = write_report(args.out_dir, "b4_clean_click", rep, lines)
    print("输出: %s\n      %s" % (jp, tp))
    return 2 if status == "DIRTY" else 0


if __name__ == "__main__":
    sys.exit(main())
