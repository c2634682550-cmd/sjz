"""只读测量：flow 台架 item_e2e / item_e2e_full 真值（不改 core、不改文档）。

做什么：
1. 指纹：先算 perf/merged 与主树的 core_digest（用 tools/tree_digest.py 同一实现）；
2. 跑 perf/bench/flow_bench.py（子进程隐藏窗口、硬超时、登记 PID），输出到 %TEMP%；
3. 从结果 JSON 取 entry / fire_all / list_prefix / item_e2e / item_e2e_full 的
   n、p50、p95、p99；
4. 拆差：p50 相加 vs 逐件相加取分位，各自与 CURRENT_BREAKDOWN 的 4354.33 比。

用法：
  .venv\\Scripts\\python.exe tools\\measure_item_e2e.py --run 60
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(PROJ, "perf", "bench", "flow_bench.py")


def _digest(root):
    """复用工具自身实现，避免另造一套哈希口径。"""
    sys.path.insert(0, os.path.join(PROJ, "tools"))
    try:
        import tree_digest as td
        fn = getattr(td, "core_digest", None) or getattr(td, "compute", None)
        if fn is not None:
            try:
                return fn(root)
            except TypeError:
                pass
    except Exception:
        pass
    r = subprocess.run(
        [sys.executable, os.path.join(PROJ, "tools", "tree_digest.py"),
         "--root", root, "--quiet"],
        capture_output=True, text=True, timeout=60,
        creationflags=0x08000000)
    out = (r.stdout or "").strip()
    return out.splitlines()[-1].strip() if out else "?"


def _run_bench(root, run_n, out_json, timeout_s):
    """隐藏窗口 + 硬超时；返回 (pid, returncode, elapsed_s, stdout_tail)。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    t0 = time.perf_counter()
    p = subprocess.Popen(
        [sys.executable, BENCH, "--root", root, "--run", str(run_n),
         "--out", out_json],
        cwd=PROJ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        startupinfo=si, creationflags=0x08000000)
    print("bench_pid=%d timeout=%ds" % (p.pid, timeout_s), flush=True)
    try:
        out, _ = p.communicate(timeout=timeout_s)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        rc = "TIMEOUT_KILLED"
    return p.pid, rc, round(time.perf_counter() - t0, 1), (out or "")[-600:]


def _row(stages, key):
    s = stages.get(key) or {}
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.join(PROJ, "perf", "merged"))
    ap.add_argument("--run", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    out_dir = os.path.join(tempfile.gettempdir(), "dafors_e2e")
    os.makedirs(out_dir, exist_ok=True)
    out_json = a.out or os.path.join(out_dir, "item_e2e_%s.json" %
                                    time.strftime("%H%M%S"))

    d_merged = _digest(a.root)
    d_main = _digest(PROJ)
    print("digest merged=%s main=%s" % (d_merged, d_main), flush=True)
    print("out=%s run=%d" % (out_json, a.run), flush=True)

    pid, rc, sec, tail = _run_bench(a.root, a.run, out_json, a.timeout)
    print("bench_pid=%d rc=%s elapsed=%ss" % (pid, rc, sec))
    if tail:
        print("---- bench tail ----\n%s" % tail)

    if not os.path.isfile(out_json):
        print("NO_OUTPUT")
        return 2
    j = json.load(open(out_json, encoding="utf-8"))
    hit = j["res"]["hit"]["stages"]
    cb = j.get("current_breakdown", {})

    print("\n== hit 阶段（unit=%s, digest=%s）==" % (j.get("unit"), j.get("core_digest")))
    print("| 阶段 | n | p50 | p95 | p99 | max |")
    print("|---|---|---|---|---|---|")
    for k in ("list_prefix", "entry", "fire_all", "item_e2e", "item_e2e_full"):
        s = _row(hit, k)
        print("| %s | %s | %s | %s | %s | %s |" % (
            k, s.get("n"), s.get("p50"), s.get("p95"), s.get("p99"), s.get("max")))

    # 常数自检：4354.33 = entry + fire_all（p50 相加）
    ref_entry = cb.get("reference_entry_ms")
    ref_fire = cb.get("reference_fire_all_ms")
    ref_list = cb.get("reference_list_prefix_ms")
    ref_e2e = cb.get("reference_end_to_end_ms")
    meas = {k: _row(hit, k) for k in
            ("list_prefix", "entry", "fire_all", "item_e2e", "item_e2e_full",
             "sub_guard_insufficient", "sub_wait_dialog_unlock",
             "sub_close_reward", "sub_bought_dialog", "sub_record")}

    s_entry = (meas["entry"] or {}).get("p50")
    s_fire = (meas["fire_all"] or {}).get("p50")
    s_list = (meas["list_prefix"] or {}).get("p50")
    q_e2e = (meas["item_e2e"] or {}).get("p50")
    q_full = (meas["item_e2e_full"] or {}).get("p50")

    print("\n== 4354.33 分解 ==")
    print("reference: entry %.2f + fire_all %.2f = %.2f（常量）; list_prefix %.2f 未计入"
          % (ref_entry, ref_fire, (ref_entry or 0) + (ref_fire or 0), ref_list))
    print("reference_end_to_end=%s kind=%s" % (ref_e2e, cb.get("end_to_end_kind")))
    if q_e2e is not None:
        print("measured  p50(entry) %.2f + p50(fire) %.2f = %.2f  (被台架报为 sum_of_p50)"
              % (s_entry, s_fire, s_entry + s_fire))
        print("measured  p50(item_e2e) = %s  (逐件相加取分位，n=%s)"
              % (q_e2e, (meas["item_e2e"] or {}).get("n")))
        print("Δ(quantile - sum_of_p50) = %+.2f ms  (%.2f%%)"
              % (q_e2e - (s_entry + s_fire),
                 100.0 * (q_e2e - (s_entry + s_fire)) / (s_entry + s_fire)))
    if q_full is not None:
        print("measured  p50(list_prefix) = %.2f; p50(item_e2e_full) = %s (n=%s)"
              % (s_list, q_full, (meas["item_e2e_full"] or {}).get("n")))
        print("Δ(full vs 4354.33) = %+.2f ms (%.2f%%)"
              % (q_full - ref_e2e, 100.0 * (q_full - ref_e2e) / ref_e2e))
        print("Δ(full vs item_e2e + list) = %+.2f ms"
              % (q_full - (q_e2e + s_list)))

    print("\n== fire 分项（hit p50）==")
    for k in ("sub_guard_insufficient", "sub_wait_dialog_unlock",
              "sub_close_reward", "sub_bought_dialog", "sub_record"):
        s = meas[k] or {}
        print("  %-26s n=%-4s p50=%s" % (k, s.get("n"), s.get("p50")))
    tot = sum((meas[k] or {}).get("p50") or 0 for k in
              ("sub_guard_insufficient", "sub_wait_dialog_unlock",
               "sub_close_reward", "sub_bought_dialog", "sub_record"))
    if s_fire:
        print("  分项合计 %.2f；fire_all %.2f；差（其它/点击循环）%+.2f"
              % (tot, s_fire, s_fire - tot))

    print("\nsamples_json=%s" % out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
