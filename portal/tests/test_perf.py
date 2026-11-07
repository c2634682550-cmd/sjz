"""性能基线：10 万卡插入/查询、卡密校验接口 p95。数字直接打印到 stdout。

单独跑： .venv/Scripts/python -m pytest portal/tests/test_perf.py -q -s
"""
from __future__ import annotations

import random
import statistics
import time

import pytest

from portal import cards_service, db as dbmod

from .conftest import create_admin, login, submit, upload_version

N_CARDS = 100_000
N_QUERY = 2000
N_REQUEST = 120


def _p95(values):
    ordered = sorted(values)
    idx = max(0, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


@pytest.mark.slow
def test_bulk_insert_and_query_timing(app, db, capsys):
    cfg = app.extensions["portal_config"]
    signer = app.extensions["portal_signer"]
    conn = dbmod.connect(cfg.db_path)

    t0 = time.perf_counter()
    result = cards_service.create_batch(
        conn, cfg, signer, count=N_CARDS, days=365, tier="pro",
        note="perf", max_per_day=5)
    insert_seconds = time.perf_counter() - t0

    total = conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    assert total == N_CARDS

    hashes = [r["code_hash"] for r in conn.execute(
        "SELECT code_hash FROM cards ORDER BY id LIMIT 5000").fetchall()]
    probes = random.sample(hashes, min(N_QUERY, len(hashes)))

    query_times = []
    for h in probes:
        start = time.perf_counter()
        conn.execute("SELECT * FROM cards WHERE code_hash=?", (h,)).fetchone()
        query_times.append((time.perf_counter() - start) * 1000)

    conn.close()
    print(f"\n[perf] 插入 {N_CARDS} 张卡：{insert_seconds:.2f}s "
          f"（{N_CARDS / insert_seconds:,.0f} 张/秒）")
    print(f"[perf] 单卡哈希查询 {len(query_times)} 次："
          f"avg={statistics.mean(query_times):.3f}ms "
          f"p95={_p95(query_times):.3f}ms "
          f"max={max(query_times):.3f}ms")
    assert insert_seconds < 60


@pytest.mark.slow
def test_download_check_endpoint_p95(app, capsys):
    create_admin(app)
    client = app.test_client()
    login(client)
    upload_version(client)
    result = issue_for_perf(app, max_per_day=1000)
    code = result["codes"][0]

    latencies = []
    for _ in range(N_REQUEST):
        start = time.perf_counter()
        resp = submit(client, code, ip="203.0.113.9")
        latencies.append((time.perf_counter() - start) * 1000)
        assert resp.status_code == 200

    print(f"\n[perf] POST / 卡密校验 {N_REQUEST} 次："
          f"avg={statistics.mean(latencies):.2f}ms "
          f"p95={_p95(latencies):.2f}ms "
          f"max={max(latencies):.2f}ms")
    assert _p95(latencies) < 100


def issue_for_perf(app, **kw):
    cfg = app.extensions["portal_config"]
    signer = app.extensions["portal_signer"]
    conn = dbmod.connect(cfg.db_path)
    try:
        return cards_service.create_batch(conn, cfg, signer, count=1,
                                          days=30, tier="pro", **kw)
    finally:
        conn.close()
