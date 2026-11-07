"""遥测增量游标的换行口径回归测试。

背景：台账在 Windows 上曾以 CRLF 写出，而游标首次哈希保留 `\r`、回看哈希
剥掉 `\r`，导致每次追加都被判成「台账被替换」并从 0 重读。这里锁死：
- CRLF / LF 同一行内容哈希必须一致；
- 仅追加、或仅换行风格变化时不得重读；
- 真替换 / 轮转缩小时仍必须全量重读；
- 断网期间新增 3 行，缓冲必须恰好 3 条。
"""
import json
import os
import shutil

import pytest

from tools import telemetry_agent as agent
from tools import telemetry_server as server


def _row(i, name=None):
    return {"ts": 1000 + i, "t": "01-01 00:00:00",
            "name": name or f"行{i}", "price": 10.0, "result": "hit",
            "elapsed": 1.2, "shot": "", "mode": "", "quota": None,
            "verified": True}


def _write(path, rows, newline):
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _append(path, rows, newline):
    with open(path, "a", encoding="utf-8", newline=newline) as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read_all(path, cursor=None):
    return agent.read_new_lines(path, cursor or {})


def test_crlf_and_lf_lines_hash_same(tmp_path):
    rows = [_row(i) for i in range(3)]
    lf = tmp_path / "lf.jsonl"
    crlf = tmp_path / "crlf.jsonl"
    _write(str(lf), rows, "\n")
    _write(str(crlf), rows, "\r\n")

    lines_lf, cur_lf = _read_all(str(lf))
    lines_crlf, cur_crlf = _read_all(str(crlf))
    assert len(lines_lf) == len(lines_crlf) == 3
    assert cur_lf["last_line_sha256"] == cur_crlf["last_line_sha256"]
    assert agent._tail_line_sha256(
        str(lf), cur_lf["offset"]) == cur_lf["last_line_sha256"]
    assert agent._tail_line_sha256(
        str(crlf), cur_crlf["offset"]) == cur_crlf["last_line_sha256"]


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_append_reads_only_new_rows(tmp_path, newline):
    rows = [_row(i) for i in range(6)]
    p = str(tmp_path / "records.jsonl")
    _write(p, rows, newline)
    lines, cur = _read_all(p)
    assert len(lines) == 6

    _append(p, [_row(6), _row(7), _row(8)], newline)
    lines2, cur2 = _read_all(p, cur)
    assert len(lines2) == 3, "追加后只应读到新增 3 行"
    assert cur2["offset"] == os.path.getsize(p)
    assert cur2["last_line_sha256"] == agent._tail_line_sha256(
        p, cur2["offset"])


def test_newline_style_rewrite_does_not_reread(tmp_path):
    rows = [_row(i) for i in range(6)]
    p = str(tmp_path / "records.jsonl")
    _write(p, rows, "\n")
    lines, cur = _read_all(p)
    assert len(lines) == 6

    _write(p, rows, "\r\n")                       # 内容不变，只换行风格变化
    lines2, cur2 = _read_all(p, cur)
    assert lines2 == [], "仅换行风格变化不应重读"
    assert cur2["offset"] == os.path.getsize(p)
    assert cur2["last_action"] == "reanchor", "漂移只能记 reanchor，不能记 replaced"


def test_true_replacement_forces_full_reread(tmp_path):
    rows = [_row(i) for i in range(6)]
    p = str(tmp_path / "records.jsonl")
    _write(p, rows, "\n")
    _, cur = _read_all(p)

    replaced = [_row(i, name=f"替换{i}") for i in range(6)]
    _write(p, replaced, "\n")
    lines2, cur2 = _read_all(p, cur)
    assert len(lines2) == 6, "台账被替换必须全量重读"
    assert cur2["offset"] == os.path.getsize(p)


def test_rotation_shrink_forces_full_reread(tmp_path):
    rows = [_row(i) for i in range(6)]
    p = str(tmp_path / "records.jsonl")
    _write(p, rows, "\n")
    _, cur = _read_all(p)

    _write(p, [_row(9, name="轮转后")], "\n")
    lines2, _ = _read_all(p, cur)
    assert len(lines2) == 1


def test_offline_buffer_is_exactly_new_rows(tmp_path, monkeypatch):
    p = str(tmp_path / "records.jsonl")
    monkeypatch.setitem(agent._STATE, "records", p)
    monkeypatch.setitem(agent._STATE, "cursor", str(tmp_path / "cursor.json"))
    monkeypatch.setitem(agent._STATE, "queue", str(tmp_path / "queue.jsonl"))
    monkeypatch.setitem(agent._STATE, "rejected", str(tmp_path / "rej.jsonl"))

    _write(p, [_row(i) for i in range(6)], "\r\n")
    cur = agent.harvest("lic", "machine", "test", b"k" * 32, 50, False, {})
    assert agent.queue_size() == 6

    agent.queue_rewrite([])                       # 模拟已全部上报
    _append(p, [_row(6), _row(7), _row(8)], "\r\n")
    cur = agent.harvest("lic", "machine", "test", b"k" * 32, 50, False, cur)
    assert agent.queue_size() == 3, "断网期间新增 3 行，缓冲必须恰好 3 条"
    assert cur["offset"] == os.path.getsize(p)


def test_server_event_id_dedupe_still_holds(tmp_path):
    """event_id 唯一约束是客户端重复上报的最后兜底，改游标不得让它退化。"""
    db = server.Store(str(tmp_path / "telemetry.db"))
    events = [{"event_id": f"e{i}", "occurred_at": 1000 + i, "tier": "pro",
               "name": f"行{i}", "price": 10.0, "result": "hit",
               "skew_ms": None, "skip_reason": ""} for i in range(3)]

    assert db.ingest("lic_hash", events, "machine", "v") == 3
    assert db.ingest("lic_hash", events, "machine", "v") == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM events").fetchone()[0] == 3
    assert db.conn.execute(
        "SELECT SUM(hits) FROM daily_agg").fetchone()[0] == 3


# ------------------------------------------------- 复核第 2 轮：边界用例

@pytest.mark.parametrize("size", [8191, 8192, 1024 * 1024])
def test_long_tail_line_never_triggers_reread(tmp_path, size):
    """末行 >= 8KB 时不能因为「回看不清一行」把整份台账判成被替换。

    size 是「整行字节数」（不含换行）：8191 是旧实现能过的边界，8192 就失败。
    """
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(5)]
    base = len(json.dumps(_row(5, name="x"), ensure_ascii=False).encode())
    line = json.dumps(_row(5, name="x" * (size - base + 1)),
                      ensure_ascii=False).encode("utf-8")
    assert len(line) == size
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write(line.decode("utf-8") + "\n")
    lines, cur = _read_all(p)
    assert len(lines) == 6

    idle, cur = _read_all(p, cur)
    assert idle == [], "没有新增时不得返回任何行（旧实现会重读 6 行）"

    _append(p, [_row(6), _row(7)], "\n")
    more, cur = _read_all(p, cur)
    assert [json.loads(x)["name"] for x in more] == ["行6", "行7"]


@pytest.mark.parametrize("via_replace", [False, True])
def test_replacement_with_duplicate_tail_rereads(tmp_path, via_replace):
    """整份替换且新文件里含与旧尾行逐字节相同的行：必须全量重读，不得 0 行。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(6)]
    _write(p, rows, "\n")
    _, cur = _read_all(p)

    tail = rows[-1]
    new_rows = ([_row(100 + i, name=f"导入{i}") for i in range(2)]
                + [tail]
                + [_row(200 + i, name=f"补录{i}") for i in range(3)])
    if via_replace:
        tmp = p + ".new"
        _write(tmp, new_rows, "\n")
        os.replace(tmp, p)
    else:
        _write(p, new_rows, "\n")

    got, cur2 = _read_all(p, cur)
    assert len(got) == 6, "替换后的文件必须全量重读（旧实现静默返回 0 行）"
    assert cur2["offset"] == os.path.getsize(p)


def test_rotation_reads_unharvested_archive_tail(tmp_path):
    """追加后、下次采集前发生轮转：`.1` 里未采集的 5 条不能丢。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(6)], "\n")
    _, cur = _read_all(p)

    _append(p, [_row(i) for i in range(6, 11)], "\n")
    os.replace(p, p + ".1")
    _write(p, [_row(11)], "\n")

    got, cur2 = _read_all(p, cur)
    names = [json.loads(x)["name"] for x in got]
    assert len(got) == 6, "应为 .1 的 5 条 + 当前台账 1 条"
    assert names == [f"行{i}" for i in range(6, 12)]
    assert cur2["offset"] == os.path.getsize(p)


def test_two_rotations_read_all_archives_in_order(tmp_path):
    """连转两次：游标停在 `.2`，要按 旧尾 → .1 → 当前 的顺序补齐。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(6)], "\n")
    _, cur = _read_all(p)

    _append(p, [_row(6), _row(7)], "\n")           # A 里未采集 2 条
    os.replace(p, p + ".1")                        # A → .1
    _write(p, [_row(8), _row(9)], "\n")            # B
    _append(p, [_row(10), _row(11)], "\n")         # B 里未采集 2 条
    os.replace(p + ".1", p + ".2")                 # A → .2（模拟 keep=3 的移位）
    os.replace(p, p + ".1")                        # B → .1
    _write(p, [_row(12)], "\n")                    # C

    got, _ = _read_all(p, cur)
    names = [json.loads(x)["name"] for x in got]
    assert names == [f"行{i}" for i in range(6, 13)], names


def test_archive_cleaned_emits_gap_event(tmp_path):
    """归档已被清理：必须显式记一条 gap，不能静默跳过。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(6)], "\n")
    _, cur = _read_all(p)

    _append(p, [_row(6), _row(7)], "\n")
    os.replace(p, p + ".1")
    os.remove(p + ".1")                            # 归档被清理
    _write(p, [_row(8)], "\n")

    got, _ = _read_all(p, cur)
    rows = [json.loads(x) for x in got]
    gaps = [r for r in rows if r.get("skip_reason") == "archive_missing"]
    assert len(gaps) == 1, rows
    assert gaps[0]["result"] == "skip"
    assert [r["name"] for r in rows
            if r.get("skip_reason") != "archive_missing"] == ["行8"]


@pytest.mark.parametrize("src,dst", [("\n", "\r\n"), ("\r\n", "\n")])
def test_large_newline_drift_uses_adaptive_window(tmp_path, src, dst):
    """7 万行：漂移量超过旧的 65536 字节窗口，两个方向都必须重锚成功。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(70000)]
    _write(p, rows, src)
    _, cur = _read_all(p)
    _write(p, rows, dst)
    got, cur2 = _read_all(p, cur)
    assert got == [], "仅换行风格变化，不应重读 7 万行"
    assert cur2["offset"] == os.path.getsize(p)
    assert cur2["last_action"] == "reanchor"


def test_partial_line_waits_and_is_not_duplicated(tmp_path):
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(6)], "\n")
    _, cur = _read_all(p)

    partial = json.dumps(_row(6), ensure_ascii=False).encode("utf-8")
    with open(p, "ab") as f:
        f.write(partial[:len(partial) // 2])

    got, cur2 = _read_all(p, cur)
    assert got == [], "残行必须留到下次"
    assert cur2["offset"] == cur["offset"]

    with open(p, "ab") as f:
        f.write(partial[len(partial) // 2:] + b"\n")
    got, cur3 = _read_all(p, cur2)
    assert [json.loads(x)["name"] for x in got] == ["行6"]

    _append(p, [_row(7), _row(8)], "\n")
    got, _ = _read_all(p, cur3)
    assert [json.loads(x)["name"] for x in got] == ["行7", "行8"]


def test_concurrent_append_loses_and_duplicates_nothing(tmp_path):
    import threading
    import time

    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(3)], "\n")
    _, cur = _read_all(p)

    def writer():
        for i in range(3, 53):
            _append(p, [_row(i)], "\n")
            time.sleep(0.002)

    th = threading.Thread(target=writer)
    th.start()
    seen = []
    deadline = time.time() + 5
    while len(seen) < 50 and time.time() < deadline:
        got, cur = _read_all(p, cur)
        seen.extend(json.loads(x)["name"] for x in got)
    th.join(timeout=5)

    got, _ = _read_all(p, cur)
    seen.extend(json.loads(x)["name"] for x in got)
    assert len(seen) == len(set(seen)) == 50, (len(seen), len(set(seen)))
    assert set(seen) == {f"行{i}" for i in range(3, 53)}


def test_head_rewrite_with_same_tail_is_detected(tmp_path):
    """只改文件头、末 3 行逐字节保留、大小不变：四项尾部判据会全中，
    必须靠「已读区域采样」打出来，不能静默跳行。
    """
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(200)]
    _write(p, rows, "\n")
    got, cur = _read_all(p)
    assert len(got) == 200
    assert cur["samples"], "游标必须存已读区域采样块"
    assert os.path.getsize(p) > 2 * agent._SAMPLE_WINDOW

    rows2 = list(rows)
    for i in range(5):
        rows2[i] = _row(i, name=f"改{i}")          # 等长改名，文件大小不变
    _write(p, rows2, "\n")
    assert os.path.getsize(p) == cur["size"]

    got2, cur2 = _read_all(p, cur)
    names = [json.loads(x)["name"] for x in got2]
    assert len(got2) == 200, "改头必须被打出来（旧实现返回 0 行）"
    assert cur2["last_action"] == "replaced"
    assert names[:5] == [f"改{i}" for i in range(5)]


def test_archive_chain_gap_emits_event(tmp_path):
    """A → B → C 两次轮转后删掉中间代 .1：缺代必须发 gap，其余代照常补齐。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(3)], "\n")        # A：行0..2 已采
    _, cur = _read_all(p)
    _append(p, [_row(i) for i in range(3, 6)], "\n")   # A 的尾巴 3 条未采
    os.replace(p, p + ".1")                             # A → .1
    _write(p, [_row(i) for i in range(6, 9)], "\n")     # B：3 条未采
    os.replace(p + ".1", p + ".2")                      # A → .2
    os.replace(p, p + ".1")                             # B → .1
    os.remove(p + ".1")                                 # 中间代被清理
    _write(p, [_row(9), _row(10)], "\n")                # C：2 条

    got, _ = _read_all(p, cur)
    rows = [json.loads(x) for x in got]
    gaps = [r for r in rows if r.get("skip_reason") == "archive_missing"]
    assert len(gaps) == 1, rows
    assert ".1 代缺失" in gaps[0]["name"]
    assert gaps[0]["shot"].endswith("gen=1")
    kept = [r["name"] for r in rows if r.get("skip_reason") != "archive_missing"]
    assert kept == ["行3", "行4", "行5", "行9", "行10"]


def test_stale_archive_copy_is_not_reported_twice(tmp_path):
    """`.1` 保存的是已采内容、当前台账又是它的副本：不得从 0 全读重报。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(6)]
    _write(p, rows, "\n")
    got, cur = _read_all(p)
    assert len(got) == 6

    os.replace(p, p + ".1")                             # 已采文件变归档
    _write(p, rows, "\n")                               # 当前 = 同一内容的副本
    got2, cur2 = _read_all(p, cur)
    assert got2 == [], "已采内容的副本不得重复上报"
    assert cur2["last_action"] == "rotation"
    assert cur2["offset"] == os.path.getsize(p)



def _mixed_write(path, rows, styles):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r, s in zip(rows, styles):
            f.write(json.dumps(r, ensure_ascii=False) + s)


def _gap_rows(got):
    out = []
    for x in got:
        r = json.loads(x)
        if r.get("skip_reason") == "archive_missing":
            out.append(r)
    return out


def test_two_missing_generations_emit_one_gap_each(tmp_path):
    """R3b：连续两代一起缺失时，每一代都要发 gap（不能静默丢行）。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(3)], "\n")
    _, cur = _read_all(p)
    _append(p, [_row(i) for i in range(3, 6)], "\n")     # A 尾 3 条未采

    os.replace(p, p + ".1")                              # A=.1
    _write(p, [_row(6)], "\n")                           # B
    os.replace(p + ".1", p + ".2")
    os.replace(p, p + ".1")                              # B=.1 A=.2
    _write(p, [_row(7)], "\n")                           # C
    os.replace(p + ".2", p + ".3")
    os.replace(p + ".1", p + ".2")
    os.replace(p, p + ".1")                              # C=.1 B=.2 A=.3
    _write(p, [_row(8)], "\n")                           # D

    os.remove(p + ".1")
    os.remove(p + ".2")                                  # 连续两代缺失，只剩 .3

    got, cur2 = _read_all(p, cur)
    gaps = _gap_rows(got)
    assert len(gaps) == 2, gaps
    assert {g["shot"].split("gen=")[-1] for g in gaps} == {"1", "2"}
    for g in gaps:
        assert "代缺失" in g["name"]
        assert g["line_no"] and g["line_no_end"] and g["est_lines"]
    by_gen = {g["shot"].split("gen=")[-1]: g for g in gaps}
    # A3：行号必须按「全量历史行号」算 —— 游标已读 3 行，A(.3) 还有 3 行未采，
    # 所以 .2 从第 7 行起（不是第 4 行），.1 接在 .2 的估算区间之后，互不重叠。
    assert by_gen["2"]["line_no"] == 7, "缺失代从更旧代的行数前缀和之后起算"
    assert by_gen["1"]["line_no"] == 7 + by_gen["2"]["est_lines"], "两代区间不能重叠"
    assert by_gen["2"]["line_no_end"] == by_gen["1"]["line_no"] - 1
    names = [json.loads(x).get("name") for x in got
             if json.loads(x).get("skip_reason") != "archive_missing"]
    assert names == ["行3", "行4", "行5", "行8"], "A 尾巴要补读，当前文件也要读"


def test_old_identity_gone_with_deeper_archive_emits_gaps(tmp_path):
    """R3b：identity 在归档链里找不到 + 当前文件更大，仍要按代发 gap。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(6)], "\n")
    _, cur = _read_all(p)
    os.remove(p)                                         # 旧文件彻底消失
    _write(p + ".3", [_row(90)], "\n")                   # 只剩更旧的一代
    _write(p, [_row(i) for i in range(6, 40)], "\n")     # 当前文件更大

    got, cur2 = _read_all(p, cur)
    gaps = _gap_rows(got)
    assert len(gaps) == 2, gaps
    assert {g["shot"].split("gen=")[-1] for g in gaps} == {"1", "2"}
    assert cur2["last_action"] == "rotation"


def test_archive_generations_not_stopped_by_double_hole(tmp_path):
    """`_archive_generations` 不能在缺 i 且缺 i+1 时 break，漏掉更旧的归档。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(0)], "\n")
    _write(p + ".4", [_row(1)], "\n")
    gens = agent._archive_generations(p)
    assert [i for i, _ in gens] == [4]


def test_stale_archive_head_only_match_does_not_hide_tail(tmp_path):
    """R4b：`.1` 只有头部 4KB 相同、总大小 <= 游标时，不得判「整份已采」。"""
    p = str(tmp_path / "records.jsonl")
    big = [_row(i) for i in range(140)]
    _write(p, big, "\n")
    _, cur = _read_all(p)
    offset = cur["offset"]
    assert offset > 12 * 1024

    raw = open(p, "rb").read(4096)                       # 头部 4KB 原样保留
    stale = tmp_path / "stale.jsonl"
    with open(stale, "wb") as f:
        f.write(raw + b"\n")
        for i in range(5):
            f.write((json.dumps(_row(900 + i, name=f"补{i}"),
                                ensure_ascii=False) + "\n").encode())
    assert os.path.getsize(stale) <= offset

    os.replace(p, p + ".1")
    os.replace(str(stale), p + ".1")                     # 陈旧 .1，身份不同
    _write(p, [_row(50)], "\n")                          # 当前文件

    got, _ = _read_all(p, cur)
    names = []
    for x in got:
        try:
            names.append(json.loads(x).get("name"))
        except ValueError:
            continue
    for i in range(5):
        assert f"补{i}" in names, f"补{i} 被静默跳过：{names[:3]}..."


def test_middle_newline_flip_is_not_replaced(tmp_path):
    """R1d2：已读区域中间 CRLF<->LF 等量翻转，净大小不变，不得判 replaced。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(200)]
    styles = (["\r\n"] * 50 + ["\n"] * 50 + ["\r\n"] * 50 + ["\n"] * 50)
    _mixed_write(p, rows, styles)
    _, cur = _read_all(p)
    size0 = os.path.getsize(p)
    assert cur["offset"] == size0

    flip = (["\r\n"] * 50 + ["\r\n"] * 50 + ["\n"] * 50 + ["\n"] * 50)
    _mixed_write(p, rows, flip)
    assert os.path.getsize(p) == size0, "翻转必须净大小不变"

    got, cur2 = _read_all(p, cur)
    assert got == []
    assert cur2["offset"] == cur["offset"]
    # reanchor = 只在原 offset 上刷新采样/窗口指纹；不得出现 replaced
    assert cur2["last_action"] in ("idle", "append", "reanchor"), cur2["last_action"]
    # 刷新后同一场景再来一轮必须不再触发前缀复核（采样已更新）
    got3, cur3 = _read_all(p, cur2)
    assert got3 == [] and cur3["offset"] == cur2["offset"]
    assert cur3["line_chain"] == cur2["line_chain"]


def test_acl_identity_jitter_does_not_reread(tmp_path):
    """ACL 重写只改 st_ino、不改字节：不得触发全量重读。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(140)]
    _write(p, rows, "\r\n")
    _, cur = _read_all(p)
    old_id = cur["file_id"]

    side = str(tmp_path / "acl_copy.jsonl")
    shutil.copyfile(p, side)                             # 新文件 = 新身份
    os.replace(side, p)                                  # 原子替换，内容一致
    new_id, _ = agent._file_identity(p)
    assert new_id != old_id

    got, cur2 = _read_all(p, cur)
    assert got == []
    assert cur2["offset"] == cur["offset"]
    assert cur2["last_action"] in ("idle", "append")
    assert not _gap_rows(got)


def test_acl_identity_jitter_with_new_lines_keeps_reading(tmp_path):
    """身份抖动后仍要能继续读到游标之后的新行，且不重读已有行。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(140)]
    _write(p, rows, "\n")
    _, cur = _read_all(p)

    side = str(tmp_path / "acl_copy.jsonl")
    shutil.copyfile(p, side)
    _append(side, [_row(140), _row(141)], "\n")          # 抖动同时追加 2 行
    os.replace(side, p)

    got, cur2 = _read_all(p, cur)
    assert [json.loads(x)["name"] for x in got] == ["行140", "行141"]
    assert cur2["last_action"] == "append"


# --------------------------------------------- 复核第 3 轮：行链与身份抖动

def test_middle_equal_size_edit_still_replaced(tmp_path):
    """行链只豁免「换行风格翻转」，不豁免「就地等长改写」。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(200)]
    _write(p, rows, "\n")
    _, cur = _read_all(p)
    assert cur["line_chain"], "游标必须存行序列链"

    rows2 = list(rows)
    rows2[0] = _row(0, name="改0")                       # 等长（4 字节）
    _write(p, rows2, "\n")
    assert os.path.getsize(p) == cur["size"]

    got, cur2 = _read_all(p, cur)
    assert len(got) == 200, "中间等长改写必须整份重读，不能被行链豁免"
    assert cur2["last_action"] == "replaced"
    assert cur2["line_chain"] != cur["line_chain"]


def test_identity_change_with_content_change_still_rereads(tmp_path):
    """身份变化 + 内容也变：必须仍走全量重读，不能判身份抖动。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(140)]
    _write(p, rows, "\n")
    _, cur = _read_all(p)

    side = str(tmp_path / "acl_copy.jsonl")
    shutil.copyfile(p, side)
    edited = list(rows)
    edited[0] = _row(0, name="改0")                      # 内容变了，且大小不变
    _write(side, edited, "\n")
    os.replace(side, p)

    got, cur2 = _read_all(p, cur)
    assert len(got) == 140, "身份+内容都变必须全量重读"
    assert cur2["last_action"] == "replaced"
    assert not _gap_rows(got)


def test_line_chain_matches_prefix_and_extends_on_append(tmp_path):
    """行链必须等于 0..offset 的独立重算值，追加后同步延长。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(200)], "\n")
    _, cur = _read_all(p)
    assert agent._prefix_line_chain(p, cur["offset"]) == cur["line_chain"]

    _append(p, [_row(200), _row(201)], "\n")
    got, cur2 = _read_all(p, cur)
    assert len(got) == 2
    assert agent._prefix_line_chain(p, cur2["offset"]) == cur2["line_chain"]


def test_legacy_cursor_without_chain_builds_it_once(tmp_path):
    """旧版游标（有采样、无行链）第一次落盘时建基线，且不重读。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(200)], "\n")
    _, cur = _read_all(p)

    legacy = dict(cur)
    legacy.pop("line_chain", None)                       # 模拟上一版游标
    got, cur2 = _read_all(p, legacy)
    assert got == []
    assert cur2["line_chain"], "应一次性建立行链基线"
    assert agent._prefix_line_chain(p, cur2["offset"]) == cur2["line_chain"]


def _fixed_row(i):
    """定宽行：插入/删除同长度行时文件总字节不变（A1 的触发条件）。"""
    return _row(i, name=f"行{i:04d}")


def test_equal_size_insert_delete_does_not_lose_line(tmp_path):
    """A1：游标前插一行 + 删一行、净长度不变，不得静默丢行。

    这种形态下尾部窗口、三个采样块、四项锚点会全部命中，只有 0..offset
    的行链能发现；修复前会返回 0 行且新行永不投递。
    """
    p = str(tmp_path / "records.jsonl")
    rows = [_fixed_row(i) for i in range(150)]
    _write(p, rows, "\n")
    got, cur = _read_all(p)
    assert len(got) == 150

    new = rows[:60] + [_fixed_row(2060)] + rows[60:90] + rows[91:]
    assert len(new) == 150
    _write(p, new, "\n")
    assert os.path.getsize(p) == cur["size"] == cur["offset"], "必须净长度不变"

    got2, cur2 = _read_all(p, cur)
    names = [json.loads(x)["name"] for x in got2]
    assert "行2060" in names, "前插的新行必须补投，不能静默丢行"
    assert cur2["last_action"] == "replaced"


def test_truncated_archive_copy_is_not_resent(tmp_path):
    """A2：`.1` 被换成「已投递内容的截短副本」，不得单轮重发。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(240)]
    _write(p, rows, "\n")
    got, cur = _read_all(p)
    assert len(got) == 240

    os.replace(p, p + ".1")                              # 归档 = 已投递 240 行
    side = str(tmp_path / "arch_new.jsonl")
    _write(side, rows[:120], "\n")                       # 截短副本（新身份）
    os.replace(side, p + ".1")
    _write(p, [_row(i) for i in range(240, 245)], "\n")  # 当前新增 5 行

    got2, cur2 = _read_all(p, cur)
    kept = [json.loads(x)["name"] for x in got2
            if json.loads(x).get("skip_reason") != "archive_missing"]
    assert kept == [f"行{i}" for i in range(240, 245)], "截短副本不得重发"
    assert cur2["last_action"] == "rotation"


def test_removed_old_file_with_unread_tail_emits_gap(tmp_path):
    """A3：旧文件被清理且归档里没有它的身份时，未读尾巴要单独一条 gap。"""
    p = str(tmp_path / "records.jsonl")
    _write(p, [_row(i) for i in range(6)], "\n")
    _, cur = _read_all(p)
    _append(p, [_row(i) for i in range(6, 8)], "\n")     # 2 行未采
    os.remove(p)                                         # 旧文件彻底消失
    _write(p + ".1", [_row(i) for i in range(8, 10)], "\n")
    _write(p, [_row(10)], "\n")                          # 当前文件比游标小

    got, cur2 = _read_all(p, cur)
    gaps = _gap_rows(got)
    assert len(gaps) == 1, "旧文件未读尾巴必须单独发一条 gap"
    assert gaps[0]["line_no"] == 7, "从游标已读 6 行的下一行起算"
    assert "上一份文件已清理" in gaps[0]["name"]
    assert [json.loads(x)["name"] for x in got
            if json.loads(x).get("skip_reason") != "archive_missing"] == \
        ["行8", "行9", "行10"]


def test_sample_gap_between_fingerprints_is_detected(tmp_path):
    """A4：只改 4096..12288、避开头部/窗口/三个采样块，也必须检出（同身份）。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(600)]                 # ≈46KB，采样点 0/15400/30800
    _write(p, rows, "\n")
    got, cur = _read_all(p)
    assert len(got) == 600 and len(cur["samples"]) == 3

    edited = list(rows)
    edited[60] = _row(60, name="改60")                   # 落在 4096..12288 的缝里
    edited[100] = _row(100, name="改100")
    _write(p, edited, "\n")
    assert os.path.getsize(p) == cur["offset"]

    got2, cur2 = _read_all(p, cur)
    assert len(got2) == 600, "指纹之间的采样缝改动必须检出并重读"
    assert cur2["last_action"] == "replaced"
    assert any(json.loads(x)["name"] == "改60" for x in got2)


def test_sample_gap_with_new_identity_is_not_jitter(tmp_path):
    """A4：改采样缝 + 换身份（新 inode）时，不能被判成身份抖动。"""
    p = str(tmp_path / "records.jsonl")
    rows = [_row(i) for i in range(600)]
    _write(p, rows, "\n")
    _, cur = _read_all(p)

    side = str(tmp_path / "same_content.jsonl")
    edited = list(rows)
    edited[60] = _row(60, name="改60")
    edited[100] = _row(100, name="改100")
    _write(side, edited, "\n")
    os.replace(side, p)                                  # 新身份，长度不变

    got, cur2 = _read_all(p, cur)
    assert len(got) == 600, "身份变 + 采样缝改动必须是 replaced，不是抖动"
    assert cur2["last_action"] == "replaced"
    assert any(json.loads(x)["name"] == "改60" for x in got)
