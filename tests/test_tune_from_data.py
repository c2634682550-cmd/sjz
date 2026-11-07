"""分析层 tools/tune_from_data.py 的 IO 容错 / 报告 / 回测测试。

只碰临时目录：不读用户真实 data/，不写项目根目录，不 sleep，不弹窗。
"""

from __future__ import annotations

import argparse
import json

import pytest

from core import autotune, switches
from tools import tune_from_data as tool

TS0 = 1_700_000_000.0


def _raw_rows(n=40, *, err=-40.0, hit_ratio=0.6, grade="传说",
              ts0=TS0, name_cycle=("A", "B", "C", "D", "E")):
    rows = []
    bucket = max(0, min(10, int(round(hit_ratio * 10))))
    for i in range(n):
        rows.append({
            "时间": ts0 + i,
            "商品名": name_cycle[i % len(name_cycle)],
            "品阶": grade,
            "成交价": 1200,
            "结果": "成交" if (i % 10) < bucket else "未中",
            "卡点误差毫秒": err,
        })
    return rows


def _write_jsonl(path, rows, extra_lines=()):
    lines = [json.dumps(r, ensure_ascii=False) for r in rows] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_csv(path, rows, headers=None):
    headers = headers or ["时间", "卡密哈希", "商品名", "品阶", "成交价",
                          "结果", "卡点误差毫秒"]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(str(row.get(h, "")) for h in headers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _args(**kwargs):
    base = {"records": None, "backend": None, "realmachine": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_reader_contract_always_three_tuple(tmp_path):
    """三个 reader 的返回形状在所有分支一致：(rows|None, bad:int, note:str)。"""
    good_jsonl = _write_jsonl(tmp_path / "r.jsonl", _raw_rows(2))
    good_csv = _write_csv(tmp_path / "b.csv", _raw_rows(2))
    good_json = tmp_path / "m.json"
    good_json.write_text(json.dumps({"runs": _raw_rows(2)}, ensure_ascii=False),
                         encoding="utf-8")
    bad_json = tmp_path / "broken.json"
    bad_json.write_text("{oops", encoding="utf-8")

    for reader, paths in (
        (tool.read_jsonl, [str(good_jsonl), str(tmp_path / "none.jsonl")]),
        (tool.read_backend, [str(good_csv), str(bad_json),
                             str(tmp_path / "none.csv")]),
        (tool.read_realmachine, [str(good_json), str(bad_json),
                                 str(tmp_path / "none.json")]),
    ):
        for path in paths:
            result = reader(path)
            assert isinstance(result, tuple) and len(result) == 3, (reader, path)
            rows, bad, note = result
            assert rows is None or isinstance(rows, list), (reader, path)
            assert isinstance(bad, int) and isinstance(note, str)


# ---------------------------------------------------------------- 读文件


def test_read_jsonl_tolerates_bad_lines_and_missing_file(tmp_path):
    path = tmp_path / "bad.jsonl"
    rows, bad, note = tool.read_jsonl(str(path))
    assert rows is None and bad == 0 and note == "文件不存在"

    _write_jsonl(path, [{"a": 1}, {"b": 2}],
                 extra_lines=["这不是 json", "[1,2]", "   "])
    rows, bad, note = tool.read_jsonl(str(path))
    assert note == ""
    assert rows == [{"a": 1}, {"b": 2}]
    assert bad == 2  # 坏 JSON 一行，非对象数组一行


def test_read_backend_csv_and_json_shapes(tmp_path):
    csv_path = _write_csv(tmp_path / "board.csv", _raw_rows(3))
    rows, bad, note = tool.read_backend(str(csv_path))
    assert note == "" and len(rows) == 3
    assert rows[0]["商品名"] == "A"

    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([{"时间": 1}], ensure_ascii=False),
                         encoding="utf-8")
    rows, _, note = tool.read_backend(str(list_path))
    assert note == "" and rows == [{"时间": 1}]

    for key in ("rows", "data", "records", "items", "list"):
        wrapped = tmp_path / f"{key}.json"
        wrapped.write_text(json.dumps({key: [{"时间": 2}]},
                                      ensure_ascii=False), encoding="utf-8")
        rows, _, note = tool.read_backend(str(wrapped))
        assert note == "" and rows == [{"时间": 2}], key

    broken = tmp_path / "broken.json"
    broken.write_text("{oops", encoding="utf-8")
    rows, _, note = tool.read_backend(str(broken))
    assert rows is None and "JSON 解析失败" in note

    scalar = tmp_path / "scalar.json"
    scalar.write_text("42", encoding="utf-8")
    rows, _, note = tool.read_backend(str(scalar))
    assert rows is None and "结构不认识" in note


def test_read_realmachine_shapes(tmp_path):
    path = tmp_path / "real.json"
    path.write_text(json.dumps({"runs": [{"结果": "成交"}]},
                               ensure_ascii=False), encoding="utf-8")
    rows, _, note = tool.read_realmachine(str(path))
    assert note == "" and rows == [{"结果": "成交"}]

    single = tmp_path / "single.json"
    single.write_text(json.dumps({"结果": "成交"}, ensure_ascii=False),
                      encoding="utf-8")
    rows, _, _ = tool.read_realmachine(str(single))
    assert rows == [{"结果": "成交"}]

    rows, _, note = tool.read_realmachine(str(tmp_path / "nope.json"))
    assert rows is None and note == "文件不存在"


def test_load_sources_merges_and_checks_order_per_source(tmp_path):
    records = _write_jsonl(tmp_path / "records.jsonl",
                           _raw_rows(5, ts0=TS0 + 20))
    backend = _write_csv(tmp_path / "board.csv", _raw_rows(5, ts0=TS0))
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"runs": []}), encoding="utf-8")

    merged, meta = tool.load_sources(_args(
        records=str(records), backend=str(backend), realmachine=str(real)))
    assert meta["records"]["present"] is True
    assert meta["backend"]["present"] is True
    assert meta["realmachine"]["present"] is True
    assert meta["merged_rows"] == 10
    # 每个源自身都是升序：跨源交错不该被判成乱序
    assert meta["time_order"]["disordered"] is False
    assert [r["ts"] for r in merged] == sorted(r["ts"] for r in merged)
    assert merged[0]["source"] == "backend"

    # 没给 --backend 时明确标「已跳过」，不假装有数据
    _, meta2 = tool.load_sources(_args(records=str(records), realmachine=None))
    assert meta2["backend"]["present"] is False
    assert "未指定" in meta2["backend"]["note"]
    assert meta2["realmachine"]["present"] is False


# ---------------------------------------------------------------- CLI 报告


def test_cli_writes_json_and_markdown(tmp_path):
    records = _write_jsonl(tmp_path / "records.jsonl", _raw_rows(40))
    backend = _write_csv(tmp_path / "board.csv", _raw_rows(10, ts0=TS0 + 100))
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"runs": _raw_rows(5, ts0=TS0 + 200)},
                               ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"

    rc = tool.main([
        "--records", str(records), "--backend", str(backend),
        "--realmachine", str(real),
        "--config", str(tmp_path / "missing_config.json"),
        "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0

    payload = json.loads((out / "tuning_recommendation.json").read_text(
        encoding="utf-8"))
    assert payload["metrics"]["n_attempts"] == 55
    assert payload["window_ms"] == autotune.WINDOW_MS
    assert payload["autotune"]["shadow"] is True
    assert payload["autotune"]["frozen"] is False

    lead = [p for p in payload["autotune"]["recommendations"]
            if p["knob"] == "fire.delay"][0]
    assert lead["blocked"] is None
    assert lead["old"] == 0 and lead["new"] == 10
    assert "p50 卡点误差" in lead["text"]
    assert "因为" in lead["text"] and "所以" in lead["text"]

    md = (out / "_tuning_report.md").read_text(encoding="utf-8")
    for section in ("## 0. 数据源", "## 1. 各品阶成交率", "## 2. 卡点误差分布",
                    "## 3. 跳过命中率", "## 4. 刷新收益",
                    "## 5. 连点收益", "## 6. 分时段 / 分客户端对比",
                    "## 7. 现在还不能下的结论",
                    "## 8. 自适应建议", "## 9. 备注"):
        assert section in md, section
    assert "unknown 不会被算作「跳对了」" in md


def test_cli_small_sample_withholds_conclusions(tmp_path):
    records = _write_jsonl(tmp_path / "records.jsonl", _raw_rows(3))
    out = tmp_path / "out"
    rc = tool.main([
        "--records", str(records), "--backend", str(tmp_path / "none.csv"),
        "--realmachine", str(tmp_path / "none.json"),
        "--config", str(tmp_path / "none.json"),
        "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0

    payload = json.loads((out / "tuning_recommendation.json").read_text(
        encoding="utf-8"))
    reasons = " ".join(payload["conclusions_withheld"])
    assert "只有 3 次尝试" in reasons
    assert "后台数据缺失" in reasons
    assert "真机报告缺失" in reasons
    assert "unknown" in reasons

    # 样本不足：所有建议都必须 blocked，new == old
    for rec in payload["autotune"]["recommendations"]:
        assert rec["blocked"], rec
        assert rec["new"] == rec["old"], rec


def test_cli_empty_data_still_writes_valid_report(tmp_path):
    out = tmp_path / "out"
    rc = tool.main([
        "--records", str(tmp_path / "none.jsonl"),
        "--backend", str(tmp_path / "none.csv"),
        "--realmachine", str(tmp_path / "none.json"),
        "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    payload = json.loads((out / "tuning_recommendation.json").read_text(
        encoding="utf-8"))
    assert payload["metrics"]["n_rows"] == 0
    assert payload["autotune"]["frozen"] is True  # 无尝试 -> kill switch
    assert "没有任何可识别的尝试" in " ".join(
        payload["autotune"]["freeze_reasons"])


# ---------------------------------------------------------------- 回测


def test_extreme_cases_never_crash_or_break_bounds(tmp_path):
    cases = tool._extreme_cases()
    assert set(cases) == {
        "three_rows", "all_fail", "all_success", "reversed_ts",
        "duplicates", "missing_fields", "huge_numbers",
    }
    for name, case in cases.items():
        for knob in autotune.KNOBS:
            lo, hi = autotune._effective_bounds(knob)
            value = case["values"][knob]
            assert lo <= value <= hi, (name, knob, value, lo, hi)
        assert isinstance(case["changes"], int)
        assert case["rows"] >= 0

    assert cases["all_fail"]["frozen"] is True
    assert cases["all_success"]["frozen"] is True
    assert cases["missing_fields"]["changes"] == 0


def test_backtest_cli_prints_convergence(capsys):
    rc = tool.main(["--backtest", "--seed", "20261107"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    conv = payload["convergence"]
    assert conv["target_ms"] == 30
    assert len(conv["curve"]) == 20
    assert conv["final_error_ms"] <= autotune.ALIGN_BAND_MS
    assert conv["drift"]["new_target_ms"] == 60
    assert conv["drift"]["lag_batches"] is not None
    assert len(conv["drift"]["trail"]) == 12
    assert payload["thresholds"]["min_sample"] == 30
    assert payload["thresholds"]["min_hits_between"] == 10
