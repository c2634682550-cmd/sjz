"""界面轮询路径的回归网：不在请求线程上做重活，也不等落盘的锁。

锁住三件事（长时挂机 185ms 单次尖刺的修复）：
  1. `records.snapshot()` 命中缓存后不重扫台账、不重倒读 —— 请求线程只做内存拷贝；
  2. 预热（`records.warm()`）没完成就来请求时，`snapshot()` 只给占位、一次盘都不读；
     冷扫只发生在启动路径（`serve()` 里的同步预热）或后台重算线程上；
  3. `Config.load_cached()` 在别的线程正持有 `_save_lock` 落盘（fsync）时，
     最多等 `lock_timeout` 就返回上一份可用快照，绝不跟着卡 100ms+。

计时前一律显式预热并等 `records.wait_ready()`，不用 sleep 赌时间：
全量回归里机器满负载时，赌时间就会赌出「抢在预热完成之前」的假失败。
"""
import json
import os
import threading
import time

import pytest

from core import config as config_mod
from core import records


def _seed(path, n):
    now = time.time()
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({
                "ts": round(now - (n - i) * 60, 3),
                "t": "11-07 10:00:00",
                "name": f"seed-{i}",
                "price": 1000 + i % 500,
                "result": "hit" if i % 3 == 0 else "miss",
                "elapsed": 0.5,
                "verified": True,
            }, ensure_ascii=False) + "\n")


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "records.jsonl"
    _seed(path, 20000)
    return path


def _p99(vals):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(0.99 * (len(vals) - 1))))]


def _warm_and_wait(limit=200):
    """显式预热 + 等完成信号（预热里那次全扫不计入任何样本）。"""
    records.warm(limit)
    assert records.wait_ready(10.0), "预热没在 10s 内完成"


def test_snapshot_never_scans_or_rereads(ledger, monkeypatch):
    """轮询命中缓存时：0 次全量扫、0 次倒读，p99 远低于 50ms。"""
    monkeypatch.setattr(records, "PATH", str(ledger))
    records.reset_state()

    _warm_and_wait()
    records.add(name="warm", price=100, result="hit", elapsed=0.5)

    calls = {"scan": 0, "tail": 0}
    orig_iter, orig_tail = records._iter_rows, records._tail_rows

    def scan_spy(path):
        calls["scan"] += 1
        return orig_iter(path)

    def tail_spy(path, limit):
        calls["tail"] += 1
        return orig_tail(path, limit)

    monkeypatch.setattr(records, "_iter_rows", scan_spy)
    monkeypatch.setattr(records, "_tail_rows", tail_spy)

    times = []
    for _ in range(200):
        t = time.perf_counter()
        snap = records.snapshot(200)
        times.append(time.perf_counter() - t)
        assert len(snap["rows"]) == 200
        assert snap["stats"]["hits"] >= 1
        assert snap["warming"] is False

    assert calls["scan"] == 0, "轮询路径不应全量扫台账"
    assert calls["tail"] == 0, "add() 之后行快照应已前推，不应再倒读"
    assert _p99(times) < 0.05, f"p99={_p99(times) * 1000:.1f}ms"


def test_stats_invalidation_returns_immediately(ledger, monkeypatch):
    """统计缓存失效（轮转/外部改动）时先给上一份值，全量重算转后台。"""
    monkeypatch.setattr(records, "PATH", str(ledger))
    records.reset_state()

    _warm_and_wait()                   # 先让统计有真值，再谈「失配时返回旧值」
    assert records._stats_snap["value"]["tries"] == 20000

    with records._lock:                # 模拟轮转/外部追加把缓存打失效
        records._cache.update(sig=None, acc=None)

    t = time.perf_counter()
    snap = records.snapshot(200)
    dt = time.perf_counter() - t

    assert dt < 0.05, f"失效后轮询被重算拖住：{dt * 1000:.1f}ms"
    assert snap["stats"]["tries"] == 20000, "应先返回上一份可用统计"
    assert snap["warming"] is False, "已有真值，不该退回预热占位"

    assert records.wait_ready(10.0), "后台重算没在 10s 内完成"
    assert records._cache["acc"] is not None
    assert records._stats_snap["value"]["tries"] == 20000


def test_cold_snapshot_does_not_full_scan_on_request_thread(ledger, monkeypatch):
    """预热没完成就来请求：统计给占位，绝不在请求线程上全扫台账。

    契约（二选一里选「占位」这条）：请求不阻塞等预热、也不自己全扫；
    rows 仍是真值（只倒读几 KB），stats 带 `warming` 占位，O(台账行数)
    的全扫只发生在后台重算线程上。`serve()` 在开始接请求之前已同步预热，
    所以正常启动顺序下连这个占位窗口都不存在。
    """
    monkeypatch.setattr(records, "PATH", str(ledger))
    records.reset_state()

    main_tid = threading.get_ident()
    scan_threads = []
    orig_iter = records._iter_rows

    def scan_spy(path):
        scan_threads.append(threading.get_ident())
        return orig_iter(path)

    monkeypatch.setattr(records, "_iter_rows", scan_spy)

    snap = records.snapshot(200)
    assert snap["warming"] is True
    assert snap["stats"]["warming"] is True
    assert snap["stats"]["tries"] == 0, "占位不当真值"
    assert len(snap["rows"]) == 200, "行是倒读出来的真值，不是空占位"
    assert main_tid not in scan_threads, "请求线程上不该出现全量扫"

    assert records.wait_ready(10.0), "后台重算没在 10s 内补出真值"
    assert scan_threads, "后台重算应该扫过台账"
    assert main_tid not in scan_threads, "全扫不能落在请求线程上"

    snap = records.snapshot(200)
    assert snap["warming"] is False
    assert len(snap["rows"]) == 200
    assert snap["stats"]["tries"] == 20000


def test_load_cached_does_not_wait_for_config_save(tmp_path, monkeypatch):
    """别人正在落盘（持 _save_lock + fsync）时，轮询读配置不能等。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"queue": [{"id": "t1"}]}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(config_mod, "_POLL_CACHE",
                        {"sig": None, "cfg": None, "at": 0.0})

    first = config_mod.Config.load_cached()       # 首读：真实读盘
    assert first.load_ok

    held = threading.Event()

    def hold_save_lock():
        with config_mod._save_lock:               # 保存路径一直持到 fsync 完成
            held.set()
            time.sleep(0.15)

    t = threading.Thread(target=hold_save_lock, daemon=True)
    t.start()
    assert held.wait(2.0)

    monkeypatch.setitem(config_mod._POLL_CACHE, "sig", None)   # 强制走「真读」分支
    t0 = time.perf_counter()
    cfg = config_mod.Config.load_cached(lock_timeout=0.01)
    dt = time.perf_counter() - t0
    t.join()

    assert dt < 0.05, f"轮询等落盘锁 {dt * 1000:.1f}ms"
    assert [x.get("id") for x in cfg["queue"]] == ["t1"], "应返回上一份可用快照"
    assert cfg.load_ok, "保存进行中不等于配置读失败，不能误报"
