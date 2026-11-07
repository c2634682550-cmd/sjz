"""tools/soak.py 的自检：指标数学、故障注入、报告判定、一次短集成跑。

这里不碰真实桌面：截图/OCR/注入器全是假的，集成用例也不开窗口。
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import soak   # noqa: E402


# ---------------------------------------------------------------- 纯函数


def test_p95_and_linreg():
    assert soak._p95([]) is None
    assert soak._p95([1, 2, 3, 4, 5]) == 5
    # 100 个点，p95 = 第 95 个（1..100）
    assert soak._p95(list(range(1, 101))) == 95
    xs = [0, 1, 2, 3]
    ys = [10, 12, 14, 16]
    assert soak._linreg(xs, ys) == pytest.approx(2.0)
    assert soak._linreg([0, 0], [1, 2]) == 0.0


def test_size_and_dir_usage(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    assert soak._size(str(p)) == 5
    n, total = soak._dir_usage(str(tmp_path))
    assert n == 1 and total == 5
    assert soak._size(str(tmp_path / "missing")) == 0


# ---------------------------------------------------------------- 故障注入


def test_fault_config_corrupt_restores_exact_bytes(tmp_path):
    cfg = tmp_path / "config.json"
    original = b'{"monitor": 1, "active_task": {"name": "x"}}'
    cfg.write_bytes(original)
    f = soak.Faults(interval=60, seed=1, workdir=str(tmp_path),
                    config_path=str(cfg))
    f._apply("config_corrupt")
    with pytest.raises(json.JSONDecodeError):
        json.loads(cfg.read_text(encoding="utf-8"))
    f._undo("config_corrupt")
    assert cfg.read_bytes() == original


def test_fault_disk_write_fail_swaps_and_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(soak.records, "PATH", str(tmp_path / "data" / "r.jsonl"))
    f = soak.Faults(interval=60, seed=2, workdir=str(tmp_path))
    f._apply("disk_write_fail")
    assert str(tmp_path / "_disk_blocked") in soak.records.PATH
    assert os.path.isfile(str(tmp_path / "_disk_blocked"))
    f._undo("disk_write_fail")
    assert soak.records.PATH == str(tmp_path / "data" / "r.jsonl")


def test_fault_schedule_one_at_a_time_and_durations():
    f = soak.Faults(interval=10.0, duration_range=(3.0, 3.0), seed=3)
    assert f.maybe_start(0.0) is None          # 第一次只登记下一次
    assert f.maybe_start(10.0) is not None
    assert f.active is not None
    assert f.maybe_start(12.0) is None         # 已有故障，不叠加
    ev = f.tick(14.0)
    assert ev and ev[0] == "clear"
    assert f.active is None


# ---------------------------------------------------------------- 故障统计


def _write_events(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_fault_table_uses_events_not_rotated_log(tmp_path):
    events = [
        {"kind": "fault_start", "type": "ocr_garbage", "ts": 100.0},
        {"kind": "fault_end", "type": "ocr_garbage", "ts": 104.0},
        {"kind": "fault_recovered", "type": "ocr_garbage", "recovery_s": 3.0},
        {"kind": "fault_start", "type": "focus_lost", "ts": 200.0},
        {"kind": "fault_end", "type": "focus_lost", "ts": 205.0},
    ]
    _write_events(tmp_path / "events.jsonl", events)
    cycles = [
        {"ts": 101.0, "state": "abort"}, {"ts": 103.0, "state": "abort"},
        {"ts": 150.0, "state": "done"}, {"ts": 201.0, "state": "abort"},
        {"ts": 260.0, "state": "done"},
    ]
    table = {x["type"]: x for x in soak._fault_table(cycles, str(tmp_path))}
    assert table["ocr_garbage"]["occurrences"] == 1
    assert table["ocr_garbage"]["recovered"] == 1
    assert table["ocr_garbage"]["aborted_cycles"] == 2
    assert table["focus_lost"]["occurrences"] == 1
    assert table["focus_lost"]["recovered"] == 0
    assert table["clock_jump"]["occurrences"] == 0


# ---------------------------------------------------------------- 报告


COLS = soak.Sampler.COLUMNS


def _mk_run(dirpath, hours, planned_s, *, leak=False, drift_last=10.0,
            handles_grow=0, faults=True):
    os.makedirs(dirpath, exist_ok=True)
    n = int(hours * 3600 / 30) + 1
    rows = []
    for i in range(n):
        t = i * 30.0
        rss = 50.0 + (400.0 * t / (hours * 3600) if leak else 2.0 * t / (hours * 3600))
        rows.append({
            "ts": str(1700000000 + t), "elapsed_s": str(t),
            "rss_mb": f"{rss:.2f}", "peak_rss_mb": f"{rss + 5:.2f}",
            "private_mb": f"{rss:.2f}", "handles": str(240 + int(handles_grow * t / (hours * 3600))),
            "gdi": "0", "user": "2", "threads": "20",
            "cpu_pct": "45.0", "log_bytes": str(500000), "ledger_bytes": str(900000),
            "ledger_archives": "1", "shots_files": "100", "shots_bytes": "30000000",
            "disk_free_mb": "120000", "queue_pending": "0", "queue_running": "0",
            "cycles_ok": str(i), "cycles_fail": "0", "cycles_stuck": "0",
            "faults_active": "0", "faults_total": "20", "faults_recovered": "20",
            "drift_p95_ms": "10.0", "config_ok": "1", "py_traced_mb": "0.5",
        })
    with open(os.path.join(dirpath, "metrics.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    cycles = []
    # 每 6 秒一轮，稳定 done；故障结束后不连续失败
    t = 0.0
    idx = 0
    while t < hours * 3600:
        state = "done" if idx % 10 != 3 else "abort"
        cycles.append({"ts": 1700000000 + t, "elapsed_s": t, "state": state,
                       "drift_ms": drift_last if t > hours * 3600 - 3600 else 10.0,
                       "clean": True, "duration_s": 6.0, "config_ok": True})
        t += 6.0
        idx += 1
    with open(os.path.join(dirpath, "cycles.jsonl"), "w", encoding="utf-8") as f:
        for c in cycles:
            f.write(json.dumps(c) + "\n")

    events = []
    if faults:
        # 一条完整的故障链：发生 → 结束 → 下一轮恢复
        events = [
            {"kind": "fault_start", "type": "ocr_garbage", "ts": 1700000000 + 600},
            {"kind": "fault_end", "type": "ocr_garbage", "ts": 1700000000 + 606},
            {"kind": "fault_recovered", "type": "ocr_garbage", "recovery_s": 2.0},
        ]
    _write_events(os.path.join(dirpath, "events.jsonl"), events)

    meta = {"tag": os.path.basename(dirpath), "planned_s": planned_s,
            "sample_interval_s": 30, "fault_interval_s": 120,
            "countdown_s": 10, "seed": 1, "start_wall": 1700000000,
            "thresholds": soak.THRESHOLDS, "notes": "synthetic"}
    with open(os.path.join(dirpath, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def test_report_pass(tmp_path):
    base = tmp_path / "baseline"
    long = tmp_path / "long"
    _mk_run(str(base), 1.0, 3600.0)
    _mk_run(str(long), 8.5, 30600.0)
    out = tmp_path / "report.md"
    verdict = soak.build_report(str(base), str(long), str(out))
    text = out.read_text(encoding="utf-8")
    assert verdict == "PASS"
    for sec in ("判据与结果", "小时级时序", "定时精度", "故障注入", "队列与清理",
                "需要在真机确认的项", "复现"):
        assert sec in text
    assert "| RSS 斜率 |" in text


def test_report_fail_on_leak(tmp_path):
    base = tmp_path / "baseline"
    long = tmp_path / "long"
    _mk_run(str(base), 1.0, 3600.0)
    _mk_run(str(long), 8.5, 30600.0, leak=True, handles_grow=200)
    out = tmp_path / "report.md"
    verdict = soak.build_report(str(base), str(long), str(out))
    text = out.read_text(encoding="utf-8")
    assert verdict == "FAIL"
    assert "RSS 上升" in text


def test_report_incomplete(tmp_path):
    base = tmp_path / "baseline"
    long = tmp_path / "long"
    _mk_run(str(base), 1.0, 3600.0)
    _mk_run(str(long), 2.0, 30600.0)       # 只跑了 2h，计划 8.5h
    verdict = soak.build_report(str(base), str(long), str(tmp_path / "r.md"))
    assert verdict == "INCOMPLETE"


# ---------------------------------------------------------------- 集成


def test_short_run_end_to_end(tmp_path):
    """真跑 30 秒：验证采集、轮次完成、故障注入、落盘、中断后汇总都能工作。

    第 1 轮不允许注故障（--fault-warmup-cycles 1）：首轮必须在干净条件下
    真的跑完，这条用例才在验证「端到端能完成轮次」，而不是在赌故障注入的
    时点。故障从第 2 轮起才注入，事件仍要真写出来。
    """
    tag = "it"
    cmd = [sys.executable, os.path.join(ROOT, "tools", "soak.py"),
           "--tag", tag, "--seconds", "30", "--sample-interval", "5",
           "--fault-interval", "3", "--countdown-s", "3",
           "--fault-warmup-cycles", "1",
           "--no-tracemalloc", "--outroot", str(tmp_path)]
    env = dict(os.environ, PYTHONPATH=ROOT)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=90, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    d = tmp_path / tag

    rows = list(csv.DictReader(open(d / "metrics.csv", encoding="utf-8")))
    assert len(rows) >= 3
    assert float(rows[-1]["rss_mb"]) > 10
    assert int(rows[-1]["handles"]) > 50

    cycles = [json.loads(x) for x in (d / "cycles.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(cycles) >= 2
    done = [c for c in cycles if c["state"] == "done"]
    # 干净窗口里必须真跑完一轮，而且真的点了、成交了 —— 不是「0 轮也算过」
    assert len(done) >= 1, f"干净窗口内一轮都没跑完：{cycles}"
    for c in done:
        assert c["bought"] is True and c["clicks"] >= 1, c
        assert c["clean"] is True, c
    # 每一行都必须是「引擎线程已经退出」后写的：停机中途 break 再写行会
    # 报出 thread_alive=true 的假泄漏（injector_down 故障下 pressed 残留
    # 是假注入器有意留的，不应据此判整个 run 不干净）。
    assert all("clean" in c for c in cycles)
    assert all(c["thread_alive"] is False for c in cycles), cycles

    # 故障注入确实发生了（预热轮次之后），且事件落在 events.jsonl
    events = soak._load_jsonl(str(d / "events.jsonl"))
    assert any(e.get("kind") == "fault_start" for e in events), events
    assert any(e.get("kind") == "fault_end" for e in events), events

    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["planned_s"] == 30
    summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert summary["duration_s"] >= 20
    assert summary["cycles_ok"] >= 1

    # 同一份数据既当基线又当长跑，但把 meta 里声称的计划时长改成 2h：
    # 实际只跑了 30s，完整度必须判失败（INCOMPLETE），报告本身仍要能生成。
    meta["planned_s"] = 7200.0
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    out = tmp_path / "it_report.md"
    v = soak.build_report(str(d), str(d), str(out))
    assert v == "INCOMPLETE", f"30s 的跑动对 2h 的计划仍判 {v}，完整度检查没生效"
    text = out.read_text(encoding="utf-8")
    assert "运行完整度" in text and "0.01" in text
    assert out.stat().st_size > 1000
