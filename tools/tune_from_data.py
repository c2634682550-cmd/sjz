"""用抢购数据 + 后台数据出调优建议（只读，不改运行参数）。

数据源（有哪个读哪个，缺的跳过并在报告里写明）：

1. 本地台账 ``data/records.jsonl``（core/records.py 写的）
2. 后台看板导出 CSV/JSON（通用字段：时间 / 卡密哈希 / 商品名 / 品阶 /
   成交价 / 结果 / 卡点误差毫秒；字段名支持中英文别名，缺列容忍）
3. 真机验证报告 ``_realmachine_result.json``（可选）

产出：

* ``tuning_recommendation.json`` —— 机器读的指标 + 参考建议
* ``_tuning_report.md`` —— 人读的报告

用法（项目根目录）::

    .venv\\Scripts\\python.exe tools/tune_from_data.py
    .venv\\Scripts\\python.exe tools/tune_from_data.py \\
        --backend exports/board.csv --realmachine _realmachine_result.json
    .venv\\Scripts\\python.exe tools/tune_from_data.py --backtest

``--backtest`` 跑合成数据收敛 / 漂移回测，只打印 JSON，不写文件。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import autotune, switches  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RECORDS = os.path.join(ROOT, "data", "records.jsonl")
DEFAULT_REALMACHINE = os.path.join(ROOT, "_realmachine_result.json")


# ---------------------------------------------------------------- 读文件


def read_jsonl(path):
    """容错 JSONL：坏行计数不抛异常。"""
    rows, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    bad += 1
    except OSError:
        return None, 0, "文件不存在"
    return rows, bad, ""


def read_backend(path):
    """CSV 或 JSON。JSON 支持 list、{"rows"/"data"/"records"/"items": [...]}。"""
    if not os.path.exists(path):
        return None, 0, "文件不存在"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            return None, 0, f"JSON 解析失败：{exc}"
        if isinstance(data, list):
            return data, 0, ""
        if isinstance(data, dict):
            for key in ("rows", "data", "records", "items", "list"):
                value = data.get(key)
                if isinstance(value, list):
                    return value, 0, ""
            return [data], 0, ""
        return None, 0, "JSON 结构不认识（既不是数组也不是对象）"

    rows, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for obj in reader:
                rows.append(dict(obj))
    except OSError as exc:
        return None, 0, f"CSV 读取失败：{exc}"
    return rows, bad, ""


def read_realmachine(path):
    """真机报告：list / {runs|cases|results|records: [...]} / 单对象。"""
    if not os.path.exists(path):
        return None, 0, "文件不存在"
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, 0, f"JSON 解析失败：{exc}"
    if isinstance(data, list):
        return data, 0, ""
    if isinstance(data, dict):
        for key in ("runs", "cases", "results", "records", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return value, 0, ""
        return [data], 0, ""
    return None, 0, "结构不认识"


def _load_source(label, path, reader, source):
    """读一个源 -> (归一化行, 元信息, 顺序检查)。"""
    if not path:
        return [], {"label": label, "path": path, "present": False,
                    "note": "未指定路径，已跳过"}, None
    raw, bad, note = reader(path)
    if raw is None:
        return [], {"label": label, "path": path, "present": False,
                    "note": note or "未找到，已跳过"}, None
    rows = []
    dropped = 0
    for obj in raw:
        row = autotune.normalize_row(obj, source=source)
        if row is None:
            dropped += 1
            continue
        rows.append(row)
    order = autotune.time_order_report(rows)
    meta = {
        "label": label, "path": path, "present": True, "rows": len(rows),
        "bad_lines": bad, "dropped": dropped, "note": note,
        "time_order": order,
        "fields_seen": sorted({f for row in rows for f in row["_present"]}),
    }
    return rows, meta, order


def load_sources(args):
    records, m_rec, o_rec = _load_source(
        "本地台账", args.records, read_jsonl, "records")
    if args.backend:
        backend, m_back, o_back = _load_source(
            "后台看板", args.backend, read_backend, "backend")
    else:
        backend, o_back = [], None
        m_back = {"label": "后台看板", "path": None, "present": False,
                  "note": "未指定 --backend，已跳过"}
    realm, m_real, o_real = _load_source(
        "真机报告", args.realmachine, read_realmachine, "realmachine")

    orders = [o for o in (o_rec, o_back, o_real) if o]
    combined = {
        "direction": "unknown", "pairs": 0, "mixed_ratio": 0.0,
        "disordered": any(o["disordered"] for o in orders),
        "by_source": {m["label"]: o for m, o in
                      ((m_rec, o_rec), (m_back, o_back), (m_real, o_real))
                      if o},
    }
    if orders:
        combined["pairs"] = sum(o["pairs"] for o in orders)
        combined["mixed_ratio"] = round(
            max(o["mixed_ratio"] for o in orders), 4)
        dirs = {o["direction"] for o in orders}
        combined["direction"] = dirs.pop() if len(dirs) == 1 else "mixed"

    # 合并后按时间排序（无时间戳的放最后，保持稳定）；后台/真机在前，
    # 台账在后 —— 重复事件去重时优先保留字段更全的那条。
    merged = backend + realm + records
    merged.sort(key=lambda r: (r["ts"] is None,
                               r["ts"] if r["ts"] is not None else 0.0))
    meta = {
        "records": m_rec, "backend": m_back, "realmachine": m_real,
        "merged_rows": len(merged), "time_order": combined,
    }
    return merged, meta


# ---------------------------------------------------------------- 配置


def load_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 报告


def _fmt(value, digits=2):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _grade_rows(summary):
    rows = []
    for grade, stat in summary["by_grade"].items():
        lead = stat["lead"]
        rows.append([
            autotune.GRADE_LABEL.get(grade, grade),
            stat["attempts"], stat["hits"], stat["misses"], stat["skips"],
            _pct(stat["hit_rate"]),
            _pct(stat["hit_rate_ci95"][0]) + " ~ " + _pct(stat["hit_rate_ci95"][1])
            if stat["hit_rate_ci95"] else "—",
            _pct(stat["miss_rate"]), _pct(stat["skip_rate"]),
            _fmt(lead["p50"], 1), _fmt(lead["p95"], 1),
            _pct(lead["out_of_window_ratio"]),
        ])
    return rows


def _conclusions(summary, meta, args):
    out = []
    for grade, stat in summary["by_grade"].items():
        if stat["attempts"] < args.min_sample:
            out.append(
                f"品阶「{autotune.GRADE_LABEL.get(grade, grade)}」只有 "
                f"{stat['attempts']} 次尝试（< {args.min_sample}），"
                "成交率与误差分位数不下结论。")
    lead = summary["lead_error"]["all"]
    if lead["n"] == 0:
        out.append("没有任何卡点误差字段，R1（开点对齐）无法评估。")
    elif lead["n"] < args.min_sample:
        out.append(f"卡点误差只有 {lead['n']} 条（< {args.min_sample}），"
                   "分位数与超窗比例仅供排查，不作为调参依据。")
    skip = summary["skip_accuracy"]
    if skip["verdict"] == "unknown":
        out.append("被跳过的商品没有任何事后证据，跳过命中率标 unknown；"
                   "不把它当「跳对了」。")
    elif skip["verdict"] == "insufficient":
        out.append(f"跳过命中率只有 {skip['confirmed_n']} 条确认证据"
                   f"（< {args.min_sample}），不下结论。")
    refresh = summary["refresh_benefit"]
    if refresh["status"] != "ok":
        out.append("刷新收益：" + (refresh.get("reason") or "数据不足") + "。")
    burst = summary["burst_benefit"]
    if burst["status"] != "ok":
        out.append("连点收益：" + (burst.get("reason") or "数据不足") + "。")
    if not meta["backend"]["present"]:
        out.append("后台数据缺失：品阶、成交价、卡密哈希维度只能靠台账；"
                   "台账没有品阶时，分品阶结论为 unknown。")
    if not meta["realmachine"]["present"]:
        out.append("真机报告缺失：误差分布来自线上/台账记录，"
                   "没有真机独立复核。")
    thin_hours = [(h, s["attempts"]) for h, s in summary["by_hour"].items()
                  if s["attempts"] < args.min_sample]
    if thin_hours:
        joined = "、".join(f"{h}点({n})" for h, n in thin_hours)
        out.append(f"以下时段样本 < {args.min_sample}，不做时段对比：{joined}。")
    thin_clients = [(c, s["attempts"]) for c, s in summary["by_client"].items()
                    if s["attempts"] < args.min_sample]
    if thin_clients:
        joined = "、".join(f"{c}({n})" for c, n in thin_clients)
        out.append(f"以下客户端版本样本 < {args.min_sample}，"
                   f"不做版本对比：{joined}。")
    # 去重后仍有字段缺失
    missing = summary["missing_fields"]
    total = summary["n_rows"]
    all_missing = [field for field in ("grade", "lead_err_ms", "refresh_count",
                                       "burst_mode", "reentry_delay_ms")
                   if total and missing.get(field) == total]
    if all_missing:
        out.append(f"全部 {total} 行都缺字段：{'、'.join(all_missing)}；"
                   "对应指标不可用。")
    return out


def _notes(summary):
    notes = [
        "抢皮肤是抽签模型（拿号 -> 摇号 -> 比点数），价格不是竞争维度；"
        "成交率只反映中签，不反映「手速更快」。",
        "刷新收益是观测相关性：热门商品本身中签率低、且往往被刷得更多，"
        "所以边际收益的因果解释需要分品阶/分商品控制后才可信；"
        "报告里的 95%CI 只处理抽样误差，不处理这种混杂。",
        "解锁时刻由「挂单时刻 + 公示时长」反推；屏幕秒数截断，"
        "所以卡点误差是相对反推值的偏差，不是绝对时钟误差。",
    ]
    q = summary["quality"]
    if q["duplicates_removed"]:
        notes.append(f"多源合并去重移除 {q['duplicates_removed']} 条重复事件。")
    if q["result_unknown"]:
        notes.append(f"{q['result_unknown']} 行结果字段无法识别，"
                     "未计入成交率分母。")
    if q["ts_suspect"]:
        notes.append(f"{q['ts_suspect']} 行时间戳落在合理区间外，"
                     "只参与计数，不参与时段分段。")
    return notes


def build_markdown(report, args):
    summary = report["metrics"]
    meta = report["inputs"]
    recs = report["autotune"]["recommendations"]
    lines = [
        "# 抢购数据调优报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 可点窗口：±{summary['window_ms']:.0f}ms；最小样本：{args.min_sample}",
        f"- 合并后 {summary['n_rows']} 行，其中可识别尝试 {summary['n_attempts']} 次",
        "",
        "## 0. 数据源",
        "",
    ]
    src_rows = []
    for key in ("backend", "realmachine", "records"):
        m = meta[key]
        src_rows.append([
            m["label"], "有" if m["present"] else "缺",
            m.get("rows", "—"), m.get("bad_lines", "—"),
            m.get("note") or "",
        ])
    lines += [_table(["数据源", "状态", "行数", "坏行", "备注"], src_rows), ""]

    lines += ["## 1. 各品阶成交率 / 尝试次数 / 结果占比", ""]
    if summary["by_grade"]:
        lines += [_table(
            ["品阶", "尝试", "成交", "未中", "跳过", "成交率", "95%CI",
             "未中率", "跳过率", "p50误差ms", "p95误差ms", "超窗比例"],
            _grade_rows(summary)), ""]
    else:
        lines += ["没有品阶字段，分品阶统计不可用。", ""]

    lines += ["## 2. 卡点误差分布", ""]
    lead = summary["lead_error"]["all"]
    lines += [_table(
        ["样本", "均值ms", "p50", "p90", "p95", "超窗(>±窗)", "超窗比例",
         "偏晚", "偏早"],
        [[lead["n"], _fmt(lead["mean"], 1), _fmt(lead["p50"], 1),
          _fmt(lead["p90"], 1), _fmt(lead["p95"], 1), lead["out_of_window"],
          _pct(lead["out_of_window_ratio"]), lead["late_off"],
          lead["early_off"]]]), ""]
    if summary["lead_error"]["by_hour"]:
        hourly = {h: v for h, v in summary["lead_error"]["by_hour"].items()
                  if v["n"] > 0}
        if hourly:
            lines += ["按小时分段（本机时区）：", "",
                      _table(["小时", "样本", "p50", "p95", "超窗比例"],
                             [[h, v["n"], _fmt(v["p50"], 1), _fmt(v["p95"], 1),
                               _pct(v["out_of_window_ratio"])]
                              for h, v in hourly.items()]),
                      ""]
        else:
            lines += ["按小时分段：没有卡点误差字段，无法分段。", ""]

    lines += ["## 3. 跳过命中率", ""]
    skip = summary["skip_accuracy"]
    lines += [_table(
        ["被跳过", "有事后证据", "确认来不及", "确认本来来得及", "unknown",
         "跳对率", "结论"],
        [[skip["n_skipped"], skip["confirmed_n"], skip["confirmed_late"],
          skip["confirmed_wrong"], skip["unknown"],
          _pct(skip["hit_accuracy"]), skip["verdict"]]]), ""]
    lines += ["unknown 不会被算作「跳对了」；分母只含有事后证据的样本。", ""]

    lines += ["## 4. 刷新收益", ""]
    refresh = summary["refresh_benefit"]
    if refresh["buckets"]:
        lines += [_table(
            ["刷新次数", "尝试", "成交率", "95%CI"],
            [[b["refresh"], b["attempts"], _pct(b["hit_rate"]),
              _pct(b["hit_rate_ci95"][0]) + " ~ " + _pct(b["hit_rate_ci95"][1])
              if b["hit_rate_ci95"] else "—"]
             for b in refresh["buckets"]]), ""]
    if refresh["status"] == "ok":
        ci = refresh["slope_ci95"]
        lines += [f"- 每多刷一次成交率增量：{refresh['slope_per_refresh']:.2%}"
                  f"（95%CI [{ci[0]:.2%}, {ci[1]:.2%}]，最小桶 n="
                  f"{refresh['min_bucket_n']}）"]
    else:
        lines += [f"- 不下结论：{refresh.get('reason')}"]
    lines += [""]

    lines += ["## 5. 连点收益（再进池延迟）", ""]
    burst = summary["burst_benefit"]
    if burst["groups"]:
        lines += [_table(
            ["连点模式", "间隔ms", "持续ms", "样本", "再进池延迟中位数ms",
             "p90ms", "样本够"],
            [[g["burst_mode"], _fmt(g["gap_ms"]), _fmt(g["duration_ms"]),
              g["n"], _fmt(g["median_reentry_ms"], 1),
              _fmt(g["p90_reentry_ms"], 1), "否" if g["insufficient"] else "是"]
             for g in burst["groups"]]), ""]
    else:
        lines += [f"不下结论：{burst.get('reason')}", ""]

    lines += ["## 6. 分时段 / 分客户端对比", ""]
    lines += ["按小时：", ""]
    lines += [_table(
        ["小时", "尝试", "成交率", "p95误差ms"],
        [[h, s["attempts"], _pct(s["hit_rate"]), _fmt(s["lead"]["p95"], 1)]
         for h, s in summary["by_hour"].items()]), ""]
    lines += ["按客户端版本：", ""]
    lines += [_table(
        ["客户端", "尝试", "成交率", "p50误差ms", "p95误差ms"],
        [[c, s["attempts"], _pct(s["hit_rate"]), _fmt(s["lead"]["p50"], 1),
          _fmt(s["lead"]["p95"], 1)]
         for c, s in summary["by_client"].items()]), ""]

    lines += ["## 7. 现在还不能下的结论", ""]
    if report["conclusions_withheld"]:
        lines += [f"- {item}" for item in report["conclusions_withheld"]]
    else:
        lines += ["- 样本量均已达标；仍需真机复核后才能把建议升级为自动变更。"]
    lines += [""]

    lines += ["## 8. 自适应建议（shadow 模式，未改运行参数）", ""]
    if report["autotune"]["frozen"]:
        lines += ["- **kill switch 已触发，冻结自适应**："
                  + "；".join(summary["quality"]["freeze_reasons"]), ""]
    if recs:
        lines += [_table(
            ["开关", "旧值", "建议值", "触发规则", "样本", "结论/解释"],
            [[r["knob"], _fmt(r["old"], 4), _fmt(r["new"], 4), r["rule"],
              r["sample"], r["text"]] for r in recs]), ""]
    else:
        lines += ["- 没有达到触发条件的建议。", ""]

    lines += ["## 9. 备注", ""]
    lines += [f"- {note}" for note in report["notes"]]
    lines += [""]
    return "\n".join(lines)


def build_report(summary, meta, cfg, args, now):
    tmpdir = tempfile.mkdtemp(prefix="tune_rec_")
    try:
        tuner = autotune.AutoTuner(
            cfg, shadow=True,
            store=autotune.AutotuneStore(directory=tmpdir),
            window_ms=args.window_ms, min_sample=args.min_sample)
        proposals = tuner.propose(summary, cfg)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(now)),
        "window_ms": args.window_ms,
        "min_sample": args.min_sample,
        "inputs": meta,
        "metrics": summary,
        "autotune": {
            "shadow": True,
            "frozen": summary["quality"]["frozen"],
            "freeze_reasons": summary["quality"]["freeze_reasons"],
            "recommendations": proposals,
            "guards": list(autotune.GUARDS),
        },
        "conclusions_withheld": _conclusions(summary, meta, args),
        "notes": _notes(summary),
    }
    return report


# ---------------------------------------------------------------- 回测


def run_backtest(args):
    conv = autotune.run_convergence(
        true_bias_ms=-30.0, batches=20, batch_size=args.batch_size,
        seed=args.seed, drift_to=-60.0, drift_batches=12)
    extreme = _extreme_cases()
    payload = {
        "convergence": conv,
        "extreme_inputs": extreme,
        "guards": list(autotune.GUARDS),
        "thresholds": {
            "window_ms": autotune.WINDOW_MS,
            "min_sample": autotune.MIN_SAMPLE,
            "min_hits_between": autotune.MIN_HITS_BETWEEN,
            "align_band_ms": autotune.ALIGN_BAND_MS,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _extreme_cases():
    """极端输入：只要求不崩、不越界、不误判。"""
    import random
    rng = random.Random(7)
    cases = {}

    def rows(n, result="miss", bias=-200.0, delay=0.0, ts0=1_700_000_000.0,
             reversed_ts=False):
        out = autotune.synthetic_rows(
            rng, bias_ms=bias, delay_ms=delay, n=n, start_ts=ts0)
        for row in out:
            row["result"] = result
        if reversed_ts:
            out.reverse()
        return out

    def run(name, data):
        # 极端输入可能连字段都没有：先走生产同一条归一化路径，
        # 保证 summarize 永远只看到统一字段（缺字段留 None）。
        data = [autotune.normalize_row(obj) for obj in data]
        data = [row for row in data if row is not None]
        cfg = {}
        tuner = autotune.AutoTuner(cfg, shadow=False,
                                   store=autotune.AutotuneStore(
                                       directory=tempfile.mkdtemp(
                                           prefix="tune_ex_")))
        try:
            summary = autotune.summarize(data, now=1_700_100_000.0)
            out = tuner.step(summary, cfg, now=1_700_100_000.0)
            cases[name] = {
                "rows": len(data),
                "frozen": out["frozen"],
                "freeze_reasons": out["freeze_reasons"],
                "changes": len(out["applied"]),
                "values": tuner.snapshot(cfg),
            }
        finally:
            shutil.rmtree(tuner.store.directory, ignore_errors=True)

    run("three_rows", rows(3, result="hit"))
    run("all_fail", rows(40, result="miss"))
    run("all_success", rows(40, result="hit"))
    run("reversed_ts", rows(40, reversed_ts=True))
    dup = rows(6, result="hit")
    run("duplicates", dup + [dict(r) for r in dup])
    run("missing_fields", [{"foo": 1}, {"bar": None}, {}, {"name": "x"}])
    huge = rows(40, result="hit")
    for row in huge:
        row["lead_err_ms"] = 1e18
        row["price"] = 1e30
    run("huge_numbers", huge)
    return cases


# ---------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抢购数据 -> 调优建议（只读，shadow）")
    parser.add_argument("--records", default=DEFAULT_RECORDS,
                        help="本地台账 jsonl")
    parser.add_argument("--backend", default=None,
                        help="后台看板导出 CSV/JSON")
    parser.add_argument("--realmachine", default=DEFAULT_REALMACHINE,
                        help="真机验证报告 json")
    parser.add_argument("--config", default=os.path.join(ROOT, "config.json"),
                        help="只读当前配置（用于建议里的旧值）")
    parser.add_argument("--out-dir", default=ROOT, help="输出目录")
    parser.add_argument("--window-ms", type=float,
                        default=autotune.WINDOW_MS, help="可点窗口毫秒")
    parser.add_argument("--min-sample", type=int,
                        default=autotune.MIN_SAMPLE, help="最小样本")
    parser.add_argument("--backtest", action="store_true",
                        help="跑合成数据收敛/漂移回测，只打印 JSON")
    parser.add_argument("--batch-size", type=int, default=30,
                        help="回测每批样本数")
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--quiet", action="store_true", help="只写文件不打印")
    args = parser.parse_args(argv)

    if args.backtest:
        run_backtest(args)
        return 0

    now = time.time()
    rows, meta = load_sources(args)
    if not rows:
        print("没有任何可用数据：台账/后台/真机报告都缺失或为空。"
              "仍会写出空报告。")
    summary = autotune.summarize(
        rows, window_ms=args.window_ms, min_sample=args.min_sample,
        now=now, order_override=meta["time_order"])
    cfg = load_config(args.config)
    report = build_report(summary, meta, cfg, args, now)

    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "tuning_recommendation.json")
    md_path = os.path.join(args.out_dir, "_tuning_report.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, allow_nan=False)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(report, args))

    if not args.quiet:
        present = [m["label"] for m in (meta["records"], meta["backend"],
                                        meta["realmachine"]) if m["present"]]
        print(f"数据源：{('、'.join(present)) or '无'}；"
              f"合并 {summary['n_rows']} 行 / 尝试 {summary['n_attempts']} 次")
        for rec in report["autotune"]["recommendations"]:
            flag = "不改" if rec.get("blocked") else "建议"
            print(f"[{flag}] {rec['knob']}: {rec['old']} -> {rec['new']} "
                  f"({rec['rule']}, n={rec['sample']})")
        print(f"已写出：{json_path}")
        print(f"已写出：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
