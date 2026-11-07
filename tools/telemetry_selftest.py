"""遥测系统自检（不弹窗、不动鼠标、不写项目数据目录）。

覆盖：幂等 / 验签 / 限流 / schema / 鉴权 / 数据正确性 / 断网与重启 /
性能 / 路径穿越 / CSV 导出。所有临时数据都在系统 temp 目录里，跑完即删。

跑法：
    .venv\\Scripts\\python.exe tools\\telemetry_selftest.py
    .venv\\Scripts\\python.exe tools\\telemetry_selftest.py --keep   # 保留现场
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, ROOT)
sys.path.insert(0, TOOLS)

import telemetry_agent as agent                     # noqa: E402
import telemetry_server as server                  # noqa: E402
from core import license as lic                    # noqa: E402
from tools import license_admin as la              # noqa: E402

PY = sys.executable
ADMIN = "selftest-token-9f3a"
RESULTS = []
STATS = {}
NAMES = ["战术手电", "夜视仪", "防弹插板", "医疗包", "加密硬盘", "信号干扰器"]
TIERS = ["普通", "稀有", "史诗", "传说"]


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + ("   " + detail if detail else ""))


def build_user(idx, lic_id, days=365):
    fp = hashlib.sha256(f"fp-{lic_id}".encode()).hexdigest()
    vec = {"bios_uuid": hashlib.sha256(f"bios-{lic_id}".encode()).hexdigest()[:32],
           "machine_guid": hashlib.sha256(f"guid-{lic_id}".encode()).hexdigest()[:32],
           "baseboard": hashlib.sha256(f"bb-{lic_id}".encode()).hexdigest()[:32]}
    payload = la.make_payload(lic_id, fp, vec, tier="pro", days=days)
    blob = la.sign(payload, SEED)
    return {"idx": idx, "lic_id": lic_id, "fp": fp, "vec": vec,
            "payload": payload, "blob": blob,
            "lic_hash": hashlib.sha256(("lic:" + lic_id).encode()).hexdigest()[:32],
            "machine": hashlib.sha256(("fp:" + fp).encode()).hexdigest()[:16],
            "key": lic._ledger_key(payload),
            "code": "SJZ-%04d-%s-%s" % (idx, hashlib.sha256(lic_id.encode())
                                        .hexdigest()[:8].upper(),
                                        hashlib.sha256(lic_id.encode())
                                        .hexdigest()[-4:].upper())}


def gen_rows(seed, n):
    rnd = random.Random(seed)
    now = time.time()
    rows = []
    for i in range(n):
        ts = now - i * 7 - rnd.random() * 3
        tier = rnd.choice(TIERS)
        price = rnd.choice([620, 880, 1200, 1500, 1860, 2200, 2600, 3100])
        roll = rnd.random()
        if roll < 0.62:
            result = "hit"
        elif roll < 0.86:
            result = rnd.choice(["miss", "no_money", "aborted"])
        else:
            result = "skip"
        row = {"ts": round(ts, 3), "t": round(ts, 3),
               "name": f"{rnd.choice(NAMES)}·{i}", "price": price,
               "result": result, "elapsed": round(rnd.uniform(2, 15), 2),
               "craft": tier, "mode": rnd.choice(["countdown", "realtime"])}
        if result == "skip":
            row["skip_reason"] = rnd.choice(["来不及", "资金不足", "窗口关闭"])
        if result == "hit" and rnd.random() < 0.8:
            row["skew_ms"] = rnd.randint(-120, 220)
        rows.append(row)
    return rows


def expect(rows):
    hits, spent, tiers, skews = 0, 0.0, {}, []
    for r in rows:
        res = agent._result_of(r)
        tier = agent._tier_of(r, r.get("name") or "")
        tiers.setdefault(tier, 0)
        if res == "hit":
            hits += 1
            if r.get("price") is not None:
                spent += float(r["price"])
            if r.get("skew_ms") is not None:
                skews.append(int(r["skew_ms"]))
            tiers[tier] += 1
    return {"events": len(rows), "hits": hits,
            "spent": round(spent, 2), "tiers": tiers,
            "skew_avg": round(sum(skews) / len(skews), 1) if skews else None}


def write_records(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run_agent(base_url, user, state_dir, records, extra=()):
    cmd = [PY, os.path.join(TOOLS, "telemetry_agent.py"), "--once",
           "--server", base_url, "--records", records,
           "--license", os.path.join(TMP, "licenses", user["lic_id"] + ".json"),
           "--state-dir", state_dir, "--client-version", "0.1.0",
           "--batch-size", "100"] + list(extra)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def http(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def admin(method, path, body=None, token=ADMIN, headers=None):
    h = {"X-Admin-Token": token} if token else {}
    h.update(headers or {})
    return http(method, path, body, h)


def json_body(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def make_batch(user, events, version="0.1.0"):
    body = {"license_id": user["lic_hash"], "machine": user["machine"],
            "client_version": version, "sent_at": round(time.time(), 3),
            "events": events}
    raw = json_body(body)
    sig = hmac.new(user["key"], raw, hashlib.sha256).hexdigest()
    return raw, sig


def post_batch(raw, sig, lid):
    h = {"Content-Type": "application/json"}
    if sig is not None:
        h["X-Telemetry-Signature"] = sig
    if lid:
        h["X-Telemetry-License"] = lid
    return http("POST", "/api/telemetry/batch", raw, h)


def ev(idx, ts=None, tier="稀有", name="测试商品", price=1200,
       result="hit", skew=37):
    return {"event_id": hashlib.sha256(f"ev-{idx}".encode()).hexdigest()[:16],
            "occurred_at": round(ts if ts is not None else time.time(), 3),
            "tier": tier, "name": name, "price": price,
            "result": result, "skew_ms": skew, "skip_reason": ""}


def main():
    global TMP, BASE, SEED
    keep = "--keep" in sys.argv
    TMP = tempfile.mkdtemp(prefix="sjz-telemetry-selftest-")
    SEED = la.load_seed(la.DEFAULT_KEY)
    os.makedirs(os.path.join(TMP, "licenses"))
    os.makedirs(os.path.join(TMP, "states"))
    server._log = lambda _m: None

    # ------------------------------------------------------------ 夹具
    users = [build_user(i + 1, f"lic-selftest-{i + 1}") for i in range(3)]
    offline_user = build_user(4, "lic-selftest-offline")
    rl_user = build_user(5, "lic-selftest-rate")
    expired_user = build_user(6, "lic-selftest-exp", days=-2)
    for u in users + [offline_user, rl_user, expired_user]:
        with open(os.path.join(TMP, "licenses", u["lic_id"] + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump(u["blob"], f)

    lic_db = os.path.join(TMP, "license.db")
    conn = la.db_open(lic_db)
    for u in users + [offline_user, rl_user, expired_user]:
        conn.execute(
            "INSERT INTO licenses(lic_id,code,fp,fp_vec,tier,issued_at,exp,"
            "trial,revoked,revoked_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (u["lic_id"], u["code"], u["fp"], json.dumps(u["vec"]), "pro",
             u["payload"]["issued_at"], u["payload"]["exp"], 0, 0, None, None))
    conn.commit()
    conn.close()

    records = {}
    for u in users:
        records[u["idx"]] = os.path.join(TMP, f"records-u{u['idx']}.jsonl")
        write_records(records[u["idx"]], gen_rows(u["idx"] * 1000, 200))
    offline_records = os.path.join(TMP, "records-offline.jsonl")
    write_records(offline_records, gen_rows(77, 40))

    # ------------------------------------------------------------ 起服务（同进程，真 HTTP）
    server.DB = server.Store(os.path.join(TMP, "telemetry.db"))
    server.LICENSE = server._connect_license_db(lic_db)
    server.ADMIN_TOKEN = ADMIN
    server.LIMITER = server.RateLimiter()
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    BASE = "http://127.0.0.1:%d" % srv.server_address[1]
    print(f"临时服务已起：{BASE}（数据目录 {TMP}）")

    # ------------------------------------------------------------ 1. agent 跑通 3 台
    for u in users:
        rc, out = run_agent(BASE, u, os.path.join(TMP, "states", f"u{u['idx']}"),
                            records[u["idx"]])
        if rc != 0:
            check(f"agent 上报 u{u['idx']}", False, out.strip()[-300:])
        else:
            check(f"agent 上报 u{u['idx']}", True, "200 条已入缓冲并送达")

    st, raw, _ = admin("GET", "/api/admin/overview")
    ov = json.loads(raw)
    total = server.DB._q("SELECT COUNT(*) FROM events").fetchone()[0]
    check("入库条数", total == 600, f"库里 {total} 条（应为 600）")

    exp_all = {"hits": 0, "spent": 0.0, "tiers": {}, "today_hits": 0,
               "today_spent": 0.0, "skew_sum": 0, "skew_n": 0}
    today = server._day_of(time.time())
    for u in users:
        rows = [json.loads(x) for x in
                open(records[u["idx"]], encoding="utf-8") if x.strip()]
        e = expect(rows)
        exp_all["hits"] += e["hits"]
        exp_all["spent"] += e["spent"]
        for k, v in e["tiers"].items():
            exp_all["tiers"][k] = exp_all["tiers"].get(k, 0) + v
        for r in rows:
            if server._day_of(r["ts"]) == today and agent._result_of(r) == "hit":
                exp_all["today_hits"] += 1
                exp_all["today_spent"] += float(r["price"] or 0)
                if r.get("skew_ms") is not None:
                    exp_all["skew_sum"] += int(r["skew_ms"])
                    exp_all["skew_n"] += 1
    check("今日成交件数", ov["today_hits"] == exp_all["today_hits"],
          f"接口 {ov['today_hits']} / 源数据 {exp_all['today_hits']}")
    check("今日总花费", abs(ov["today_spent"] - round(exp_all["today_spent"], 2)) < 0.01,
          f"接口 {ov['today_spent']} / 源数据 {round(exp_all['today_spent'], 2)}")
    api_tiers = {t["tier"]: t["hits"] for t in ov["tiers"]}
    check("品阶分布", api_tiers == {k: v for k, v in exp_all["tiers"].items() if v},
          f"接口 {api_tiers} / 源数据 {exp_all['tiers']}")
    check("在线台数", ov["online"] == 3, f"接口 {ov['online']}（应为 3）")

    st, raw, _ = admin("GET", "/api/admin/users")
    uj = json.loads(raw)
    check("用户列表条数", uj["count"] == 3, f"接口 {uj['count']}")
    ok_users = True
    for u in users:
        rows = [json.loads(x) for x in
                open(records[u["idx"]], encoding="utf-8") if x.strip()]
        e = expect(rows)
        target = next(x for x in uj["users"] if x["id"] == u["lic_hash"])
        if not (target["hits"] == e["hits"]
                and abs(target["spent"] - e["spent"]) < 0.01
                and target["machine"] == u["machine"]
                and target["events"] == 200):
            ok_users = False
            print("  偏差：", target, e)
    check("每用户成交数/花费/机器码", ok_users, "3 台逐项一致")

    uid = users[0]["lic_hash"]
    st, raw, _ = admin("GET", f"/api/admin/user/{uid}")
    det = json.loads(raw)
    check("单用户明细条数", len(det["events"]) == 50,
          f"返回 {len(det['events'])} 条（上限 50）")
    check("单用户按天趋势", len(det["daily"]) >= 1,
          f"{len(det['daily'])} 个日期桶")

    st, raw, _ = admin("GET", "/api/admin/trend?bucket=hour")
    hour_pts = json.loads(raw)["points"]
    st, raw, _ = admin("GET", "/api/admin/trend?bucket=day")
    day_pts = json.loads(raw)["points"]
    check("小时趋势落在当前小时", len(hour_pts) == 24 and hour_pts[-1]["hits"] > 0,
          f"24 桶，末桶成交 {hour_pts[-1]['hits']}")
    check("按天趋势", len(day_pts) == 30 and day_pts[-1]["hits"] > 0,
          f"30 桶，今日成交 {day_pts[-1]['hits']}")

    # ------------------------------------------------------------ 2. 幂等
    before = server.DB._q("SELECT COUNT(*) FROM events").fetchone()[0]
    rows1 = [json.loads(x) for x in open(records[1], encoding="utf-8") if x.strip()]
    ev1 = [agent._event(r) for r in rows1]
    for _ in range(2):
        raw, sig = make_batch(users[0], ev1[:100])
        st, body, _ = post_batch(raw, sig, users[0]["lic_hash"])
    after = server.DB._q("SELECT COUNT(*) FROM events").fetchone()[0]
    resp = json.loads(body)
    check("幂等重发", st == 200 and after == before and resp.get("inserted") == 0,
          f"库 {before} → {after}，本次新增 {resp.get('inserted')}")

    # ------------------------------------------------------------ 3. 鉴权
    for label, tok in (("无令牌", None), ("错令牌", "wrong-token")):
        st, body, _ = admin("GET", "/api/admin/overview", token=tok)
        check(f"管理接口{label}", st == 401, f"HTTP {st}")
    st, body, _ = http("GET", "/api/admin/export?scope=users", None, {})
    check("导出接口无令牌", st == 401, f"HTTP {st}")

    # ------------------------------------------------------------ 4. 验签
    raw, sig = make_batch(users[0], [ev(1)])
    st, body, _ = post_batch(raw, "", users[0]["lic_hash"])
    check("空签名", st == 401, f"HTTP {st} {body[:60]!r}")

    tampered = json.loads(raw)
    tampered["events"][0]["price"] = 9999
    st, body, _ = post_batch(json_body(tampered), sig, users[0]["lic_hash"])
    check("篡改 payload", st == 403 and "签名" in body.decode("utf-8", "replace"),
          f"HTTP {st} {body[:80]!r}")

    st, body, _ = post_batch(raw, "0" * 64, users[0]["lic_hash"])
    check("篡改签名", st == 403, f"HTTP {st}")

    raw, sig = make_batch(expired_user, [ev(2)])
    st, body, _ = post_batch(raw, sig, expired_user["lic_hash"])
    check("过期 license", st == 403 and "到期" in body.decode("utf-8", "replace"),
          f"HTTP {st} {body[:80].decode('utf-8', 'replace')}")

    raw = json_body({"license_id": "f" * 32, "machine": "abcdef0123456789",
                     "client_version": "0.1.0", "sent_at": time.time(),
                     "events": [ev(3)]})
    fake_key = b"\x11" * 32
    sig = hmac.new(fake_key, raw, hashlib.sha256).hexdigest()
    st, body, _ = post_batch(raw, sig, "f" * 32)
    check("未登记 license", st == 403 and "未登记" in body.decode("utf-8", "replace"),
          f"HTTP {st}")

    # ------------------------------------------------------------ 5. schema
    def schema_case(label, events, expect_text):
        raw, sig = make_batch(users[1], events)
        st, body, _ = post_batch(raw, sig, users[1]["lic_hash"])
        txt = body.decode("utf-8", "replace")
        detail = f"HTTP {st} {txt[:90]}"
        check(f"schema·{label}", st == 400 and expect_text in txt, detail)

    bad_unknown = ev(4)
    bad_unknown["screenshot"] = "shot_001.png"
    schema_case("未知字段", [bad_unknown], "未知字段")
    missing = ev(5)
    missing.pop("result")
    schema_case("缺字段", [missing], "缺字段")
    typed = ev(6)
    typed["price"] = "abc"
    schema_case("类型错", [typed], "数字")
    toolong = ev(7)
    toolong["name"] = "甲" * 100
    schema_case("超大字符串", [toolong], "超长")
    schema_case("单批超上限", [ev(1000 + i) for i in range(201)], "单批最多")

    # ------------------------------------------------------------ 6. 限流
    codes = []
    for i in range(21):
        raw, sig = make_batch(rl_user, [ev(2000)])
        st, body, _ = post_batch(raw, sig, rl_user["lic_hash"])
        codes.append(st)
    st, body, _ = post_batch(*make_batch(rl_user, [ev(2000)]), rl_user["lic_hash"])
    txt = body.decode("utf-8", "replace")
    check("限流", codes.count(200) == 20 and codes[-1] == 429 and st == 429
          and "每分钟" in txt, f"21 次状态 {codes}，第 22 次 {st} {txt[:70]}")

    # ------------------------------------------------------------ 7. 断网 + 重启
    dead = "http://127.0.0.1:9"
    st_dir = os.path.join(TMP, "states", "offline")
    rc, out = run_agent(dead, offline_user, st_dir, offline_records)
    q = os.path.join(st_dir, agent.QUEUE_NAME)
    q_lines = sum(1 for x in open(q, encoding="utf-8") if x.strip()) if os.path.exists(q) else 0
    offline_hash = offline_user["lic_hash"]
    in_db = server.DB._q("SELECT COUNT(*) FROM events WHERE lic_hash=?",
                         offline_hash).fetchone()[0]
    check("断网缓冲", q_lines == 40 and in_db == 0,
          f"缓冲 {q_lines} 条 / 库里 {in_db} 条")

    rc, out = run_agent(BASE, offline_user, st_dir, offline_records)
    q_lines = sum(1 for x in open(q, encoding="utf-8") if x.strip()) if os.path.exists(q) else 0
    in_db = server.DB._q("SELECT COUNT(*) FROM events WHERE lic_hash=?",
                         offline_hash).fetchone()[0]
    check("恢复补传", q_lines == 0 and in_db == 40,
          f"缓冲 {q_lines} 条 / 库里 {in_db} 条")

    rc, out = run_agent(BASE, offline_user, st_dir, offline_records)
    in_db2 = server.DB._q("SELECT COUNT(*) FROM events WHERE lic_hash=?",
                          offline_hash).fetchone()[0]
    cur = json.load(open(os.path.join(st_dir, agent.CURSOR_NAME), encoding="utf-8"))
    size = os.path.getsize(offline_records)
    check("agent 重启游标", in_db2 == 40 and cur["offset"] == size,
          f"库里 {in_db2} 条 / 游标 {cur['offset']} = 文件 {size}")

    # ------------------------------------------------------------ 8. 性能
    perf_rows = []
    base_ts = time.time() - 20 * 86400
    for i in range(10000):
        perf_rows.append({
            "event_id": hashlib.sha256(f"perf-{i}".encode()).hexdigest()[:16],
            "occurred_at": round(base_ts + i * 172.0, 3),
            "tier": TIERS[i % 4], "name": f"压测商品·{i}", "price": 1000.0,
            "result": "hit" if i % 3 else "miss",
            "skew_ms": (i % 200) - 80, "skip_reason": ""})
    perf_hash = hashlib.sha256(b"perf-license").hexdigest()[:32]
    t0 = time.perf_counter()
    for i in range(0, 10000, 200):
        server.DB.ingest(perf_hash, perf_rows[i:i + 200], "perf0000", "0.1.0")
    import_ms = (time.perf_counter() - t0) * 1000
    STATS["import_10k_ms"] = round(import_ms, 0)

    t0 = time.perf_counter()
    for _ in range(20):
        server.DB.overview()
    ov_ms = (time.perf_counter() - t0) / 20 * 1000
    STATS["overview_ms"] = round(ov_ms, 1)
    n = server.DB._q("SELECT COUNT(*) FROM events").fetchone()[0]
    check("1 万条导入", import_ms < 15000,
          f"{import_ms:.0f} ms（{10000 / max(import_ms / 1000, 0.001):.0f} 条/秒，"
          f"库内共 {n} 条）")
    check("overview 查询", ov_ms < 200, f"20 次平均 {ov_ms:.1f} ms")

    # ------------------------------------------------------------ 9. 路径穿越
    traversal = True
    for rel in ("../evil.csv", "..\\evil.csv", "exports/../../evil.csv"):
        try:
            server._safe_path(server.DATA_ROOT, rel)
            traversal = False
        except ValueError:
            pass
    try:
        server._safe_path(server.DATA_ROOT, "C:\\Windows\\evil.csv")
        traversal = False
    except ValueError:
        pass
    inside = server._safe_path(server.DATA_ROOT, "exports/ok.csv")
    same_base = os.path.commonpath([inside, os.path.realpath(server.DATA_ROOT)]) == \
        os.path.realpath(server.DATA_ROOT)
    check("路径穿越（函数）", traversal and same_base,
          f"3 种 `..` + 绝对路径被拒，合法路径 {inside}")

    cli = [PY, os.path.join(TOOLS, "telemetry_server.py")]
    p1 = subprocess.run(cli + ["export", "--scope", "users", "--out", "../evil.csv",
                               "--db", os.path.join(TMP, "telemetry.db"),
                               "--license-db", lic_db],
                        capture_output=True, text=True, timeout=60)
    p2 = subprocess.run(cli + ["purge", "--days", "1",
                               "--archive", "../../evil.ndjson",
                               "--db", os.path.join(TMP, "telemetry.db"),
                               "--license-db", lic_db],
                        capture_output=True, text=True, timeout=60)
    check("路径穿越（CLI）", p1.returncode != 0 and "被拒" in (p1.stderr + p1.stdout),
          (p1.stderr or p1.stdout).strip().splitlines()[0]
          if (p1.stderr or p1.stdout) else "无输出")
    check("归档路径穿越（CLI）", p2.returncode != 0 and "被拒" in (p2.stderr + p2.stdout),
          (p2.stderr or p2.stdout).strip().splitlines()[0]
          if (p2.stderr or p2.stdout) else "无输出")

    old_ts = time.time() - 200 * 86400
    server.DB.ingest(perf_hash, [dict(perf_rows[0], event_id="0" * 16,
                                      occurred_at=old_ts)], "perf0000", "0.1.0")
    before_purge = server.DB._q("SELECT COUNT(*) FROM events").fetchone()[0]
    archive = os.path.join(TMP, "archive_old.ndjson")
    removed, hit = server.DB.purge(180, archive)
    after_purge = server.DB._q("SELECT COUNT(*) FROM events").fetchone()[0]
    check("180 天清理", removed >= 1 and after_purge == before_purge - removed
          and os.path.exists(archive),
          f"删除 {removed} 条（{before_purge} → {after_purge}），归档已写")

    # ------------------------------------------------------------ 10. CSV
    st, raw, _ = admin("GET", "/api/admin/export?scope=overview")
    txt = raw.decode("utf-8-sig")
    check("CSV 总览", st == 200 and "在线台数" in txt and "今日成交件数" in txt,
          f"HTTP {st}，{len(txt.splitlines())} 行")
    st, raw, _ = admin("GET", "/api/admin/export?scope=users")
    txt = raw.decode("utf-8-sig")
    check("CSV 用户列表", st == 200 and "卡密（掩码）" in txt and "总花费（元）" in txt,
          f"HTTP {st}，{len(txt.splitlines())} 行")
    st, raw, _ = admin("GET", f"/api/admin/export?scope=user&id={uid}")
    txt = raw.decode("utf-8-sig")
    check("CSV 单用户", st == 200 and "卡点误差（毫秒）" in txt and "跳过原因" in txt,
          f"HTTP {st}，{len(txt.splitlines())} 行")

    # ------------------------------------------------------------ 11. 看板可打开
    st, raw, hdr = http("GET", "/")
    html = raw.decode("utf-8", "replace")
    check("看板首页", st == 200 and "<title>抢购遥测看板</title>" in html
          and "https://" not in html and "http://" not in html.replace(
              "http://www.w3.org", ""),
          f"HTTP {st}，{len(raw)} 字节，无外链")

    # ------------------------------------------------------------ 汇总
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "-" * 66)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，"
          f"失败 {len(failed)} 项")
    print(f"性能：1 万条导入 {STATS['import_10k_ms']} ms；"
          f"overview 平均 {STATS['overview_ms']} ms")
    if failed:
        print("失败项：" + "；".join(r[0] for r in failed))

    srv.shutdown()
    if keep:
        print(f"现场保留在 {TMP}")
    else:
        shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
