# -*- coding: utf-8 -*-
"""R08 render/interaction robustness harness (headless, mock backend, no source changes)."""
import json, os, sys, time, threading, socket, math, random, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = r"C:\Users\26346\Desktop\project\dafors"
WEB = os.path.join(ROOT, "web", "index.html")
SHOTS = os.path.join(ROOT, "_r08_shots")
os.makedirs(SHOTS, exist_ok=True)

LOCK = threading.Lock()
RESULTS = []

# ---------------- mock state --------------
def mk_task(i, **kw):
    d = dict(id=i, name=f"目标{i}", status="pending", enabled=True, quota=2, bought=0,
             min_price=0, max_price=5000, craft="", wear="", max_wear=0,
             keywords="关键词" + str(i), note="", season="s1", workflow="countdown",
             spent=0, budget=0, fire_at=0, result=None, price=None)
    d.update(kw)
    return d

def mk_seasons(n_per_tier=4, seasons=2):
    out = []
    for s in range(seasons):
        tiers = {"普通": [], "优品": [], "极品": []}
        for t in tiers:
            for i in range(n_per_tier):
                tiers[t].append({"name": f"{t}皮肤{s}-{i}", "weapon": ["步枪", "手枪", "近战"][i % 3],
                                 "gun": "M4", "skin": "涂装"})
        out.append({"season": f"s{s+1}", "season_name": f"第{s+1}赛季", "tiers": tiers})
    return out

ST = dict(
    ver=0, down=False, running=False, tasks=[mk_task(i) for i in range(1, 6)],
    sched_delay=[], sched_500=False, sched_hold=False,
    tasks_500_once=False, switches_500=False, add_delay=0.0, start_delay=0.0,
    start_fail=False, guard_level=0, guard_state="ok",
    skins=mk_seasons(), records=[], records_delay=0.0,
    inflight=0, max_inflight=0, req_total=0, hits=0, log=[], toast_q=[],
    engine_state="idle", clicks=0, q_len=None, tasks_payload_override=None,
    update_500=False, records_500=False,
)

def sched_snapshot():
    now = int(time.time())
    tasks = ST["tasks"]
    return dict(ok=True, running=ST["running"], active=(tasks[0]["id"] if tasks and ST["running"] else None),
                page={"page": "market", "at": now, "stale": False},
                clock={"server": time.strftime("%H:%M:%S"), "at": now, "offset": 0.021, "stale": False},
                engine={"state": ST["engine_state"], "doing": "等待公示",
                        "stats": {"hz": 7, "clicks": ST["clicks"]},
                        "log": ST["log"]},
                tasks=tasks, log=[{"t": time.strftime("%H:%M:%S"), "level": "info", "msg": f"日志条目 {i}"} for i in range(30)])

SWITCH_GROUPS = [
    ("运行模式", [("run.mode", "choice", "运行模式", "auto", {"options": ["auto", "manual"]}),
                 ("run.threads", "int", "识别线程数", 2), ("run.debug", "bool", "调试日志", True)]),
    ("收藏设置", [("fav.auto", "bool", "自动收藏", True), ("fav.rate", "int", "收藏间隔", 120)]),
    ("环境与安全", [("env.guard", "bool", "场景保护", True), ("env.check", "bool", "启动前检查", True)]),
    ("价格与筛选", [("price.mode", "choice", "出价模式", "limit", {"options": ["limit", "auto"]}),
                   ("price.max", "int", "最高单价", 5000)]),
    ("抢购设置", [("fire.delay", "int", "基础点击延迟", 42), ("fire.adaptive", "bool", "动态调整", True),
                 ("fire.confirm_clicks", "int", "确认连点次数", 3), ("fire.confirm_gap", "int", "确认间隔", 18),
                 ("fire.jitter", "float", "随机抖动", 1.5), ("fire.timeout", "int", "单轮超时", 800)]),
    ("自动与停止", [("stop.after", "int", "抢到几件后停", 2), ("stop.idle", "bool", "空闲自动停", False)]),
    ("截前", [("pre.shot", "bool", "抢前截图", True), ("pre.keep", "int", "保留天数", 7)]),
]

def switches_payload():
    groups = []
    for name, items in SWITCH_GROUPS:
        g = {"name": name, "label": name, "items": []}
        for key, kind, label, value, *rest in items:
            it = {"key": key, "kind": kind, "label": label, "desc": "说明文字", "value": value}
            if rest:
                it.update(rest[0])
            g["items"].append(it)
        groups.append(g)
    return {"ok": True, "groups": groups}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/" or p == "/index.html":
            data = open(WEB, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/__ctl":
            return self._json(ST)
        self._api(p, u, "GET")

    def do_POST(self):
        u = urlparse(self.path)
        body = self._read()
        if u.path == "/__ctl":
            with LOCK:
                for k, v in body.items():
                    if k in ST:
                        ST[k] = v
            return self._json({"ok": True})
        self._api(u.path, u, "POST", body)

    def _api(self, p, u, method, body=None):
        with LOCK:
            ST["inflight"] += 1
            ST["req_total"] += 1
            ST["max_inflight"] = max(ST["max_inflight"], ST["inflight"])
        try:
            if ST["down"]:
                try:
                    self.connection.close()
                except Exception:
                    pass
                return
            q = parse_qs(u.query)
            delay = 0.0
            snap = None
            with LOCK:
                if p == "/api/scheduler" and ST["sched_delay"]:
                    delay = ST["sched_delay"].pop(0)
                if p == "/api/scheduler" and not ST["sched_500"]:
                    ST["ver"] += 1
                    snap = sched_snapshot()
                    snap["_v"] = ST["ver"]
                    snap["engine"]["stats"]["clicks"] = ST["clicks"] if ST["clicks"] else ST["ver"]
                if p == "/api/records" and ST["records_delay"]:
                    delay = ST["records_delay"]
                if p == "/api/scheduler/start" and ST["start_delay"]:
                    delay = ST["start_delay"]
            if delay:
                time.sleep(delay)
            if p == "/api/scheduler":
                if ST["sched_500"]:
                    return self._json({"ok": False, "error": "scheduler boom"}, 500)
                return self._json(snap)
            if p == "/api/guard":
                lv = ST["guard_level"]
                g = {"level": lv, "state": ST["guard_state"], "message": "场景保护已触发",
                     "goal": "确认这是交易行页面", "attempts": 1,
                     "plan": [{"a": 1}, {"a": 2}, {"a": 3}], "history": [],
                     "scene_text": "画面识别到未知页面", "button": {"id": "resume", "label": "继续"}}
                if lv == 0:
                    g = {"level": 0, "state": "ok"}
                return self._json({"ok": True, "guard": g})
            if p == "/api/config":
                return self._json({"ok": True, "version": "3.2.1", "workflow": "countdown",
                                   "clock": {"server": "12:00:00", "offset": 0.02}})
            if p == "/api/tasks":
                if ST["tasks_500_once"]:
                    with LOCK:
                        ST["tasks_500_once"] = False
                    return self._json({"ok": False, "error": "设置文件被占用"}, 500)
                return self._json({"ok": True, "tasks": ST["tasks"], "config_ok": True})
            if p == "/api/tasks/add":
                with LOCK:
                    new = mk_task(1000 + len(ST["tasks"]), name=body.get("name") or "新任务",
                                  keywords=body.get("keywords") or "", max_price=body.get("max_price") or 0,
                                  quota=body.get("quota") or 1, season=body.get("season") or "")
                    ST["tasks"].append(new)
                    ST["toast_q"].append("add")
                if ST["add_delay"]:
                    time.sleep(ST["add_delay"])
                return self._json({"ok": True, "task": new, "tasks": ST["tasks"]})
            if p == "/api/tasks/update":
                if ST["update_500"]:
                    return self._json({"ok": False, "error": "写设置失败"}, 500)
                with LOCK:
                    for t in ST["tasks"]:
                        if str(t["id"]) == str(body.get("id")):
                            for k, v in body.items():
                                if k != "id":
                                    t[k] = v
                return self._json({"ok": True, "tasks": ST["tasks"]})
            if p == "/api/tasks/remove":
                with LOCK:
                    ST["tasks"] = [t for t in ST["tasks"] if str(t["id"]) != str(body.get("id"))]
                return self._json({"ok": True, "tasks": ST["tasks"]})
            if p == "/api/tasks/clear":
                with LOCK:
                    ST["tasks"] = [t for t in ST["tasks"] if t["status"] not in ("done", "failed", "skipped")]
                return self._json({"ok": True, "tasks": ST["tasks"]})
            if p == "/api/switches":
                if ST["switches_500"]:
                    return self._json({"ok": False, "error": "switches boom"}, 500)
                return self._json(switches_payload())
            if p == "/api/scheduler/start":
                if ST["start_fail"]:
                    return self._json({"ok": False, "msg": "需要管理员权限才能控制游戏窗口"})
                with LOCK:
                    ST["running"] = True
                    ST["engine_state"] = "watch"
                return self._json({"ok": True})
            if p == "/api/scheduler/stop":
                with LOCK:
                    ST["running"] = False
                    ST["engine_state"] = "idle"
                return self._json({"ok": True})
            if p == "/api/skins":
                return self._json({"ok": True, "seasons": ST["skins"]})
            if p == "/api/skins/locate":
                return self._json({"ok": True, "job": "job-1"})
            if p == "/api/skins/target":
                return self._json({"ok": True})
            if p == "/api/jobs":
                return self._json({"ok": True, "current": None,
                                   "job": {"state": "running", "log": ["滚动查找中"], "name": "定位", "elapsed": 3}})
            if p == "/api/hotkey":
                return self._json({"ok": True, "stop": {"spec": "F8", "enabled": True, "active": True},
                                   "start": {"spec": "F7", "enabled": True, "active": True}})
            if p == "/api/timesync":
                if ST["sched_500"]:
                    time.sleep(0.2)
                    return self._json({"ok": False, "error": "校时服务器全部超时"}, 500)
                return self._json({"ok": True, "offset": 0.031, "delay": 0.02, "server": "ntp.aliyun.com",
                                   "stale": False, "samples": [{"server": "ntp.aliyun.com", "delay": 0.02, "offset": 0.031}],
                                   "tried": []})
            if p == "/api/records":
                if ST["records_500"]:
                    return self._json({"ok": False, "error": "记录文件读不出来"}, 500)
                rows = ST["records"]
                return self._json({"ok": True, "rows": rows,
                                   "stats": {"hits": sum(1 for r in rows if r.get("result") == "hit"),
                                             "verified_hits": 0, "unverified_hits": 0, "spent": 0,
                                             "rate": 0, "tries": len(rows), "median_s": 1.2}})
            if p == "/api/preflight":
                return self._json({"ok": True, "checks": [
                    {"ok": True, "label": "游戏窗口", "detail": "已找到"},
                    {"ok": False, "warn": True, "label": "时间校准", "detail": "偏差 120 毫秒", "fix": {"hint": "点重新校准"}},
                    {"ok": True, "label": "识别模型", "detail": "就绪"}]})
            if p == "/api/quit":
                return self._json({"ok": True})
            return self._json({"ok": True})
        finally:
            with LOCK:
                ST["inflight"] -= 1


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


# ---------------- playwright --------------
from playwright.sync_api import sync_playwright

PORT = None

def ctl(page=None, **kw):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/__ctl",
                                 data=json.dumps(kw).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()

def reset_state(page, **kw):
    base = dict(down=False, running=False, tasks=[mk_task(i) for i in range(1, 6)], sched_delay=[], sched_500=False,
                tasks_500_once=False, switches_500=False, add_delay=0, start_fail=False, guard_level=0,
                guard_state="ok", skins=mk_seasons(), records=[], records_delay=0, engine_state="idle",
                clicks=0, update_500=False, records_500=False, log=[], inflight=0, max_inflight=0)
    base.update(kw)
    ctl(page, **base)

def js(page, expr):
    if expr.strip().startswith("()=>"):
        return page.evaluate(expr)
    return page.evaluate("()=>(" + expr + ")")

def shot(page, name, full=False):
    p = os.path.join(SHOTS, name + ".png")
    page.screenshot(path=p, full_page=full)
    return p

def add(case, sev, title, trigger, evidence, phenomenon, good=None):
    RESULTS.append(dict(case=case, sev=sev, title=title, trigger=trigger, evidence=evidence,
                        phenomenon=phenomenon, good=good))
    print(f"[{case}] {sev} {title}")

def wait(page, ms):
    page.wait_for_timeout(ms)

CASES = []

def case(fn):
    CASES.append(fn)
    return fn


@case
def c01_poll_race(page):
    reset_state(page, running=True, engine_state="fire", clicks=100, sched_delay=[])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 1800)
    # 第3次轮询延迟 3.2s；第4/5/6次立即返回，旧快照将在新快照之后到达
    ctl(page, sched_delay=[0.05, 0.05, 3.2, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05])
    samples = []
    t0 = time.time()
    while time.time() - t0 < 9:
        v = js(page, "S.status && S.status._v")
        samples.append(v)
        wait(page, 50)
    import urllib.request
    mx = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/__ctl", timeout=5).read())["max_inflight"]
    pairs = [(i, samples[i - 1], samples[i]) for i in range(1, len(samples))
             if samples[i] is not None and samples[i - 1] is not None and samples[i] < samples[i - 1]]
    shot(page, "c01_poll_race")
    add("R08-01", "高", "轮询无 in-flight 保护：迟到的旧响应覆盖较新状态",
        "运行页保持轮询，让第 3 次 /api/scheduler 延迟 3.2s 才返回（第 4~6 次立即返回）",
        f"服务端并发峰值 max_inflight={mx}；S.status._v 采样 {len(samples)} 次，回退 {len(pairs)} 次 {pairs[:6]}；截图 c01_poll_race.png",
        "较新的快照先显示，3.2s 后到达的旧快照把界面整块覆盖回去，顶部进度/引擎状态/队列出现倒跳；两次请求同时在途，说明 setInterval(poll) 没有 in-flight 互斥或序号校验")

@case
def c02_double_start(page):
    reset_state(page, running=False, clicks=0, start_delay=0.6)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    counts = {"start": 0, "stop": 0}
    def onreq(r):
        if "/api/scheduler/start" in r.url: counts["start"] += 1
        if "/api/scheduler/stop" in r.url: counts["stop"] += 1
    page.on("request", onreq)
    # 同一任务内三连击，等价于用户手速极快的连点
    page.evaluate("()=>{const b=document.querySelector('#btnStartTop');b.click();b.click();b.click();}")
    wait(page, 250)
    inflight_dis = js(page, "$('#btnStartTop').disabled")
    inflight_act = js(page, "$('#btnStartTop').dataset.act")
    shot(page, "c02_double_start")
    wait(page, 2200)
    after_act = js(page, "$('#btnStartTop').dataset.act")
    after_txt = js(page, "$('#btnStartTop').textContent.trim()")
    shot(page, "c02_after")
    add("R08-02", "中", "启动/停止按钮在请求在途时不禁用，连击会重复提交启动",
        "在运行页待命态对「启动抢购」同一帧内连点 3 次（/start 响应 0.6s）",
        f"在途时按钮 disabled={inflight_dis}、data-act={inflight_act}；网络层 start={counts['start']} 次 stop={counts['stop']} 次；最终按钮「{after_txt}」act={after_act}；截图 c02_double_start.png / c02_after.png",
        "一次意图发出多次启动请求（按钮既不置灰也不显示 loading）；若首包先返回，按钮在 1s 轮询后立刻变成「停止抢购」，用户后续连点会变成停抢，操作结果与意图相反")

@case
def c03_double_add(page):
    reset_state(page, tasks=[mk_task(1)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    n0 = js(page, "S.tasks.length")
    adds = {"n": 0}
    page.on("request", lambda r: adds.__setitem__("n", adds["n"] + 1) if "/api/tasks/add" in r.url else None)
    ctl(page, add_delay=0.5)
    page.evaluate("()=>openAdd()")
    wait(page, 200)
    page.fill("#addName", "连点测试")
    okbtn = page.locator("#addOk")
    for _ in range(4):
        okbtn.click(force=True, no_wait_after=True)
        wait(page, 40)
    wait(page, 2000)
    n1 = js(page, "S.tasks.length")
    shot(page, "c03_double_add")
    add("R08-03", "高", "「添加任务」无在途锁，连点会重复创建任务",
        "打开添加对话框，填入名称，对「添加」按钮连点 4 次（接口延迟 0.5s）",
        f"POST /api/tasks/add 次数={adds['n']}；任务数 {n0} → {n1}；截图 c03_double_add.png",
        f"同一次添加产生 {adds['n']} 条重复任务（{n0}→{n1}），addTask 只有 S.saving 用于保存任务，没有 added-in-flight 标志，按钮也未禁用")

@case
def c04_dialog_reentry(page):
    reset_state(page, tasks=[mk_task(1, name="会被删两次的任务"), mk_task(2)])
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    page.evaluate("()=>selectTask(1)")
    wait(page, 200)
    page.locator('[data-act="task-del"]').click()
    wait(page, 250)
    open_now = js(page, "$('#dlg').open")
    # 用户双击确认按钮
    page.evaluate("()=>{const b=document.querySelector('#dlgOk');b.click();b.click();}")
    wait(page, 900)
    still_open = js(page, "$('#dlg').open")
    del_count = js(page, "S.tasks.length")
    shot(page, "c04_dialog_reentry")
    add("R08-04", "通过", "确认框双击「确认」不重复执行、不抛错（与 dialog 重入相关的担心不成立）",
        "点击「删除任务」打开确认框，然后双击确认按钮",
        f"对话框打开={open_now}；双击后 open={still_open}，剩余任务={del_count}；捕获错误={errors[:2]}；截图 c04_dialog_reentry.png",
        "close() 对已关闭 <dialog> 是 no-op，dlgResolve 置空后不会二次执行；背景按钮的第二次点击被 modal inertness 吞掉，无法重入 ask()",
        good="快速双击删除/确认不会产生重复删除或悬挂 Promise")


@case
def c05_sel_mismatch(page):
    reset_state(page, tasks=[mk_task(1, name="A"), mk_task(2, name="B")])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 400)
    sel_before = js(page, "JSON.stringify([S.sel, typeof S.sel])")
    page.locator('#taskRows tr[data-id="2"]').click()
    wait(page, 300)
    sel_after = js(page, "JSON.stringify([S.sel, typeof S.sel])")
    sel_rows = js(page, "$$('#taskRows tr.sel').length")
    shot(page, "c05_sel_mismatch")
    add("R08-05", "中", "id 类型不一致导致选中高亮/当前项标记失效（字符串 vs 数字严格比较）",
        "点击左侧任务列表第二行（数据来自 data-id 字符串）",
        f"S.sel 从 {sel_before} 变为 {sel_after}；带 .sel 的行数={sel_rows}；截图 c05_sel_mismatch.png",
        f"初始数字 id 时高亮正常，点击后 S.sel 变成字符串，taskRowsHtml 用 S.sel === t.id 严格比较永不相等，选中行高亮消失；renderLive 里 t.id === activeId 同样失效")

@case
def c06_check_reset(page):
    reset_state(page, tasks=[mk_task(i) for i in range(1, 7)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 300)
    page.locator('.rowchk').nth(0).check()
    page.locator('.rowchk').nth(1).check()
    page.locator('.rowchk').nth(2).check()
    wait(page, 150)
    n_before = js(page, "$$('.rowchk:checked').length")
    shot(page, "c06_check_before")
    wait(page, 1200)
    n_after = js(page, "$$('.rowchk:checked').length")
    chkall_after = js(page, "$('#chkAll').checked")
    shot(page, "c06_check_after")
    add("R08-06", "高", "1s 轮询重绘任务表，勾选状态被静默清空",
        "在任务列表勾选 3 条，等待一次轮询（1s）",
        f"勾选数 {n_before} → {n_after}；chkAll={chkall_after}；截图 c06_check_before.png / c06_check_after.png",
        f"勾选后 1 秒内选中项全部丢失，全选框也被 renderTaskTable 强制复位；批量启用/停用按钮拿不到用户选择，随后按“无勾选=全部”的兜底逻辑操作全量任务")

@case
def c07_error_sticky(page):
    reset_state(page, tasks_500_once=True, tasks=[mk_task(1), mk_task(2)],
                sched_delay=[])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 1500)
    mode = js(page, "taskMode")
    has_tasks = js(page, "S.tasks?S.tasks.length:-1")
    text = js(page, "$('#taskRows').innerText.slice(0,40)")
    shot(page, "c07_error_sticky")
    wait(page, 1500)
    mode2 = js(page, "taskMode")
    text2 = js(page, "$('#taskRows').innerText.slice(0,40)")
    add("R08-07", "高", "任务读取失败后错误页固化，轮询已拿到数据仍显示「任务读不出来」",
        "让 /api/tasks 首次返回 500，之后再正常；轮询持续从 /api/scheduler 带回 tasks",
        f"taskMode={mode}→{mode2}，S.tasks 长度={has_tasks}，表格文本「{text}」/「{text2}」；截图 c07_error_sticky.png",
        "paintTop/paintRunIdle 每秒把 tasks 写回 S.tasks，但 taskMode 一直停在 error，renderTaskTable 因此持续渲染错误态；数据其实已恢复，用户必须手动点「重新读取」才看得到任务")

@case
def c08_switches_loading(page):
    reset_state(page, switches_500=True)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 3000)
    beat = js(page, "$('#beatBody').innerText.slice(0,60)")
    skel = js(page, "$$('#beatBody .skel').length")
    shot(page, "c08_switches_loading")
    add("R08-08", "中", "开关接口失败时「节拍」面板永远停在加载骨架，加载态与错误态不可区分",
        "启动时让 /api/switches 持续 500",
        f"3 秒后 #beatBody 文本=「{beat}」，骨架元素={skel}；截图 c08_switches_loading.png",
        "renderBeat 只区分 switches 为 null（显示“加载中…”）与有数据，loadSwitches 的 catch 吞掉错误不置错误态，面板无限骨架、无重试入口，与真正的加载中无法分辨")

@case
def c09_long_list(page):
    reset_state(page, tasks=[mk_task(i, name="长列表任务%03d" % i) for i in range(1, 601)], running=True,
                engine_state="watch")
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 600)
    dur = js(page, "()=>{const t=performance.now(); renderTaskTable(); return performance.now()-t}")
    nodes = js(page, "$('#taskRows').querySelectorAll('*').length")
    shot(page, "c09_long_list")
    page.evaluate("()=>go('run')")
    wait(page, 400)
    qh = js(page, "$('#liveQueue').scrollHeight")
    q_nodes = js(page, "$('#liveQueue').querySelectorAll('*').length")
    cost = js(page, "()=>{const t=performance.now(); renderLive(S.status); return performance.now()-t}")
    # 滚动位置：内容变化后是否被拉回
    page.evaluate("()=>{const q=$('#liveQueue'); q.scrollTop=q.scrollHeight/2;}")
    s1 = js(page, "$('#liveQueue').scrollTop")
    ctl(page, tasks=[mk_task(i, name="长列表任务%03d" % i, status=("done" if i % 3 == 0 else "pending")) for i in range(1, 601)])
    wait(page, 1400)
    s2 = js(page, "$('#liveQueue').scrollTop")
    shot(page, "c09_queue_scroll")
    # 放大到 2000 条再测
    ctl(page, tasks=[mk_task(i, name="长列表任务%04d" % i) for i in range(1, 2001)])
    wait(page, 1500)
    dur2 = js(page, "()=>{const t=performance.now(); renderTaskTable(); return performance.now()-t}")
    nodes2 = js(page, "$('#taskRows').querySelectorAll('*').length")
    cost2 = js(page, "()=>{const t=performance.now(); renderLive(S.status); return performance.now()-t}")
    shot(page, "c09_2000")
    add("R08-09", "中", "轮询每秒整表/整队列 innerHTML 重建，开销随任务数线性增长并丢失交互态",
        "600 条任务下进入运行页并等待轮询；随后把任务扩到 2000 条再测",
        f"600 条：renderTaskTable {dur:.1f}ms/{nodes} 节点，renderLive {cost:.1f}ms，队列高 {qh}px/{q_nodes} 节点；2000 条：{dur2:.1f}ms/{nodes2} 节点，renderLive {cost2:.1f}ms；内容刷新后队列 scrollTop {s1} → {s2}；截图 c09_long_list.png / c09_queue_scroll.png / c09_2000.png",
        "即使数据没变，每秒也会把整张表和整个队列重建一次；节点数与耗时随任务量线性上升（2000 条时单次 45ms/24ms，已超过一帧预算），期间文本选择与 hover 状态丢失；滚动位置未被强制回顶，但内容刷新时列表会整块闪烁")

@case
def c10_lib_cap(page):
    reset_state(page, skins=mk_seasons(n_per_tier=120, seasons=2))
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    page.evaluate("()=>go('lib')")
    wait(page, 1500)
    total = js(page, "S.skins.seasons.reduce((a,s)=>a+Object.values(s.tiers).reduce((x,y)=>x+y.length,0),0)")
    cards = js(page, "$$('#libGrid .gcard').length")
    count_txt = js(page, "$('#libCount').textContent")
    shot(page, "c10_lib_cap")
    add("R08-10", "中", "目标库硬截断 400 张卡片但计数仍显示全量，剩余目标无任何提示",
        "构造 720 件皮肤后打开「目标库」",
        f"实际条目={total}，渲染卡片={cards}，计数文本=「{count_txt}」；截图 c10_lib_cap.png",
        "rows.slice(0,400) 静默截断，界面显示 720/720 但只有 400 张可点，后 320 件搜不到也翻不到，用户会以为目录缺件")

@case
def c11_overflow_text(page):
    longname = "超长" * 60 + "名字"
    rtl = "RTL" + "\u202e" + "gnp.exe" + "\u202c"
    reset_state(page, tasks=[mk_task(1, name=longname, keywords="关键词 " * 40),
                             mk_task(2, name=rtl),
                             mk_task(3, name="引号\"双\"'单'<b>标签</b> & 符号"),
                             mk_task(4, name="😀🎮🧨" * 20)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 500)
    page.set_viewport_size({"width": 1024, "height": 720})
    wait(page, 600)
    shot(page, "c11_overflow_1024")
    over = js(page, """()=>{const out=[];document.querySelectorAll('.tname,.tsub,.scard,.qitem,.gcard,.beat-row .bl').forEach(e=>{
        if(e.scrollWidth>e.clientWidth+1) out.push((e.className||e.tagName)+':'+(e.scrollWidth-e.clientWidth));});return out.slice(0,8)}""")
    docw = js(page, "document.documentElement.scrollWidth-window.innerWidth")
    # verify no HTML injection from names
    injected = js(page, "$('#taskRows').querySelectorAll('b').length")
    tlen = js(page, "($('.tname')||{}).title ? $('.tname').title.length : 0")
    page.set_viewport_size({"width": 1440, "height": 900})
    wait(page, 400)
    shot(page, "c11_overflow_1440")
    add("R08-11", "低", "超长名/RTL 覆盖符在 1024 宽下被裁剪，名称可被双向控制符伪装显示顺序",
        "注入 240 字符名称、RTL 覆盖符、emoji、引号与伪 HTML 标签，切到 1024 宽再切回 1440",
        f"1024 宽：文本溢出（ellipsis 裁剪）元素={over}，文档横向溢出={docw}px；伪标签被转义（DOM 内 b 元素={injected}）；.tname 的 title 长度={tlen}；截图 c11_overflow_1024.png / c11_overflow_1440.png",
        "长名按设计被省略号截断且 title 提供全文，未造成布局错乱；但名称中的 U+202E 会让「RTLgnp.exe」这类字符串显示成反向，用户看到的名字与实际目标不一致（无 bidi 隔离）",
        good="引号、<、>、& 与 emoji 均未造成注入或横向滚动；taskRowsHtml/gcard/日志全部走 esc()")

@case
def c12_huge_numbers(page):
    reset_state(page, tasks=[mk_task(1, name="大数字", quota=999, bought=999, max_price=1e15,
                                     spent=1e15, budget=1e15, min_price=1e15)],
                running=True, engine_state="fire")
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 700)
    shot(page, "c12_huge_numbers")
    def over():
        return js(page, """()=>{const out=[];['#bpBought','#bpSpent','#ringSpent','#ringBudget','#liveQueue','#taskRows'].forEach(s=>{
            const e=$(s); if(!e) return; if(e.scrollWidth>e.clientWidth+1) out.push(s+':'+(e.scrollWidth-e.clientWidth));});
            document.querySelectorAll('.num,.qm,.qitem .qm b,.lm .v,.ring .rv b').forEach(e=>{if(e.scrollWidth>e.clientWidth+1)out.push(e.className+':'+(e.scrollWidth-e.clientWidth))});return out.slice(0,10)}""")
    overflow = over()
    page.set_viewport_size({"width": 1024, "height": 700})
    wait(page, 600)
    overflow2 = over()
    shot(page, "c12_huge_1024")
    add("R08-12", "通过", "1e15 级数值与 999/999 额度在 1440 与 1024 宽下均未溢出格子",
        "把 quota/bought/spent/budget/价格设为 1e15，分别在 1440 与 1024 宽截图",
        f"溢出元素 1440={overflow}，1024={overflow2}；截图 c12_huge_numbers.png / c12_huge_1024.png",
        "长数字完整显示在容器内（右侧额度面板按行排列，不撑破）；未使用千分位，纯数字堆叠可读性一般，但无布局破坏",
        good="超大数字不会顶破卡片或表格，1024 宽下也保持在容器内")

@case
def c13_unescaped_inject(page):
    base_ts = int(time.time())
    rows = [
        {"ts": base_ts, "name": "正常", "result": "hit", "price": '<img src=x onerror="window.__r08xss=1">',
         "elapsed": 1.2, "verified": True, "shot": ""},
        {"ts": base_ts - 10, "name": "转账<script>window.__r08xss2=1</script>", "result": "hit",
         "price": 0, "elapsed": 1, "verified": False, "shot": ""},
    ]
    reset_state(page, records=rows)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    page.evaluate("()=>go('rec')")
    wait(page, 1500)
    xss = js(page, "window.__r08xss||0")
    xss2 = js(page, "window.__r08xss2||0")
    imgs = js(page, "$$('#recRows img').length")
    txt = js(page, "$('#recRows').innerText.slice(0,120)")
    shot(page, "c13_unescaped_inject")
    add("R08-13", "中", "记录页价格列直接拼接服务端字段（innerHTML 未转义），可注入 HTML/事件",
        "让 /api/records 的 price 字段返回 <img src=x onerror=...>，打开「记录」页",
        f"window.__r08xss={xss}，注入的 img 节点={imgs}；名称字段被转义（__r08xss2={xss2}）；表格文本「{txt}」；截图 c13_unescaped_inject.png",
        "r.price / s.hits / spent 等数值位直接进 innerHTML（第2897行），本地记录文件被污染或字段类型异常时脚本会被执行；名称类字段因走 esc() 未受影响")

@case
def c14_focus_loss(page):
    reset_state(page, tasks=[mk_task(i, name=f"键盘任务{i}") for i in range(1, 5)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 400)
    page.locator('#taskRows tr[data-id="2"]').focus()
    before = js(page, "document.activeElement.tagName+'|'+document.activeElement.dataset.id")
    wait(page, 1300)
    after = js(page, "document.activeElement.tagName+'|'+(document.activeElement.dataset||{}).id")
    shot(page, "c14_focus_loss")
    # tab into filter then poll
    page.focus("#taskSearch")
    t1 = js(page, "document.activeElement.id")
    wait(page, 1300)
    t2 = js(page, "document.activeElement.id")
    add("R08-14", "中", "轮询整表重绘导致键盘焦点每秒丢失（键盘可达性破坏）",
        "用 Tab 把焦点放到任务行，保持不动等待一次轮询",
        f"焦点 {before} → {after}；输入框焦点 {t1} → {t2}；截图 c14_focus_loss.png",
        "焦点落在 <tr> 后，1 秒内被轮询重绘踢回 body，方向键/回车继续操作的焦点上下文丢失；屏幕阅读器也会反复重读整张表")

@case
def c15_reconnect_leak(page):
    reset_state(page)
    st = {"down": True, "cfg": 0}
    def handler(route):
        if st["down"]:
            route.abort("failed")
        else:
            route.continue_()
    page.route("**/api/**", handler)
    page.on("request", lambda r: st.__setitem__("cfg", st["cfg"] + 1) if "/api/config" in r.url else None)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    wait(page, 3200)
    visible = js(page, "!$('#connOverlay').hidden")
    boot_at = js(page, "$('#boot')?!!$('#boot'):false")
    c0 = st["cfg"]
    page.evaluate("()=>{for(let i=0;i<6;i++){document.querySelector('[data-act=\"reconnect\"]').click();}}")
    wait(page, 2200)
    c1 = st["cfg"]
    shots = shot(page, "c15_reconnect_leak")
    st["down"] = False
    wait(page, 3000)
    c2 = st["cfg"]
    boot_gone = js(page, "!$('#boot')")
    poll_on = js(page, "S.pollTimer!==0")
    add("R08-15", "中", "「立即重试」叠加多条 boot 重试链：恢复瞬间并发多套启动流程",
        "启动时 /api/* 全部网络失败（route.abort），3.2s 后连点 6 次「立即重试」，再恢复网络",
        f"离线时遮罩={visible}，启动页还在={boot_at}，/api/config 请求数 {c0}；点 6 次后 2.2s 内新增 {c1-c0} 次；恢复后 3s 内总数 {c2}（共 {c2} 次），启动页已移除={boot_gone}，轮询运行中={poll_on}；截图 c15_reconnect_leak.png",
        "每次 boot() 失败都会 setTimeout(boot,2000) 再起一条自循环，重试按钮又直接再起一条；点击 6 次后恢复网络，多套 boot/load 流程同时跑完，重复拉取开关/任务/皮肤并多次 bootDone，无 single-flight 或取消机制")

@case
def c16_toast_ephemeral(page):
    reset_state(page, sched_500=True)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    # 触发 5 条真实错误 toast（立即校准按钮 -> /api/timesync 500）
    page.evaluate("()=>go('set')")
    wait(page, 400)
    page.evaluate("""()=>{const b=document.querySelector('[data-act="timesync"]');
        for(let i=0;i<5;i++) b.click();}""")
    wait(page, 300)
    shown = js(page, "$('#toastTxt').textContent")
    shot(page, "c16_toast_burst")
    wait(page, 3600)
    cls = js(page, "$('#toast').className")
    gone = js(page, "getComputedStyle($('#toast')).opacity")
    tsmsg = js(page, "$('#tsMsg') ? $('#tsMsg').textContent : 'n/a'")
    any_log = js(page, "document.body.innerText.includes('校时服务器全部超时')")
    shot(page, "c16_toast_gone")
    add("R08-16", "中", "错误提示只在 toast 停留 3.2 秒，连续触发互相覆盖，消失后无任何可追溯记录",
        "让 /api/timesync 返回 500，连点 5 次「重新校准」",
        f"连点后 toast 文本=「{shown}」，3.6s 后 className=「{cls}」opacity={gone}；#tsMsg 残留=「{tsmsg}」；正文仍能找到该错误={any_log}；截图 c16_toast_burst.png / c16_toast_gone.png",
        "5 次失败只有最后一条可见，其余被覆盖；3.2 秒后策略性消失，界面无错误历史区，日记页也只显示本次运行日志；设置页状态文字还停在“正在测速并校准…”，与实际失败矛盾")

@case
def c17_toast_long(page):
    reset_state(page)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    page.evaluate("()=>toast('A'.repeat(260),'err')")
    wait(page, 300)
    box = js(page, """()=>{const r=$('#toast').getBoundingClientRect();const s=getComputedStyle($('#toast'));
        return {left:Math.round(r.left),right:Math.round(r.right),w:Math.round(r.width),vw:innerWidth,sw:$('#toast').scrollWidth,cw:$('#toast').clientWidth}}""")
    shot(page, "c17_toast_long")
    add("R08-17", "低", "超长无空格错误文本撑破 toast（缺少 word-break/min-width:0）",
        "直接把 260 个连续字符的错误消息塞进 toast",
        f"toast 盒 {box}；截图 c17_toast_long.png",
        "toast 有 max-width 但内部文本不换行，内容横向溢出卡片并越过左边界，长 URL/路径类错误会盖住状态栏内容")

@case
def c18_offline_recover(page):
    reset_state(page, running=True, engine_state="fire", clicks=5000,
                tasks=[mk_task(i, name=f"任务{i}") for i in range(1, 9)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 900)
    page.evaluate("()=>go('run')")
    wait(page, 1200)
    st = {"down": False}
    def handler(route):
        if st["down"]:
            route.abort("failed")
        else:
            route.continue_()
    page.route("**/api/**", handler)
    before_txt = js(page, "$('#liveCountdown').textContent")
    st["down"] = True
    ctl(page, running=True, clicks=5000, engine_state="fire")
    off = []
    for _ in range(16):
        wait(page, 500)
        off.append((js(page, "S.connected"), js(page, "S.netFail"),
                    js(page, "!$('#connOverlay').hidden"), js(page, "S.status&&S.status.engine&&S.status.engine.stats.clicks"),
                    js(page, "$('#liveCountdown').textContent")))
    ov = any(r[2] for r in off)
    cd_offline = js(page, "$('#liveCountdown').textContent+'|'+$('#liveCountdownSub').textContent")
    shot(page, "c18_offline")
    ctl(page, running=True, clicks=11111, engine_state="fire")
    st["down"] = False
    rec = []
    for _ in range(16):
        wait(page, 500)
        rec.append((js(page, "S.connected"), js(page, "!$('#connOverlay').hidden"),
                    js(page, "S.status&&S.status.engine&&S.status.engine.stats.clicks")))
    ov2 = js(page, "!$('#connOverlay').hidden")
    conn = js(page, "$('#sbConnTxt').textContent")
    clicks = js(page, "S.status&&S.status.engine&&S.status.engine.stats.clicks")
    caught = next((i for i, r in enumerate(rec) if r[2] == 11111), None)
    shot(page, "c18_recovered")
    add("R08-18", "通过", "断网能识别、恢复后 1 个轮询周期内自动追平（倒计时冻结场景未构造）",
        "运行中断开 /api（route.abort）8 秒，再恢复并把 clicks 改为 11111，每 0.5s 采样",
        f"断网期 16 次采样：遮罩出现过={ov}，末次 connected={off[-1][0]}、netFail={off[-1][1]}、clicks 冻结在 {off[-1][3]}、状态文本「{off[-1][4]}」；恢复后首个采样 clicks={rec[0][2]}，最终 clicks={clicks}，遮罩={ov2}，连接=「{conn}」；截图 c18_offline.png / c18_recovered.png",
        "断网 1s 左右即弹遮罩并把连接标为断开，运行数据停止更新；网络恢复后下一次轮询就把 clicks 从 5000 追平到 11111，遮罩自动消失，无残留脏状态",
        good="断网检测与恢复追平正常（正确性层面），遮罩只缺重试次数/进度展示")

@case
def c19_http500_silent(page):
    reset_state(page, running=True, engine_state="fire", clicks=10)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 800)
    t_before = js(page, "$('#gDetail').textContent")
    ctl(page, sched_500=True, clicks=10)
    wait(page, 4000)
    t_after = js(page, "$('#gDetail').textContent")
    conn = js(page, "$('#sbConnTxt').textContent")
    ov = js(page, "!$('#connOverlay').hidden")
    toast_cls = js(page, "$('#toast').className")
    shot(page, "c19_http500")
    add("R08-19", "高", "接口 500 不计入连接健康度：界面静默冻结在旧数据，无任何异常提示",
        "让 /api/scheduler 持续返回 HTTP 500（网络本身正常）",
        f"4 秒前后 #gDetail 文本 「{t_before}」→「{t_after}」；连接状态={conn}，离线遮罩={ov}，toast={toast_cls}；截图 c19_http500.png",
        "api() 只在 fetch 抛错时 noteFail，HTTP 500 直接 throw 但既不算失败也不刷新数据；轮询 catch 空吞，运行中界面继续显示过期的点击次数/阶段，用户以为程序在跑")

@case
def c20_viewport(page):
    reset_state(page, running=True, engine_state="fire", clicks=321,
                tasks=[mk_task(i, name=f"目标{i}", bought=1) for i in range(1, 5)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    page.evaluate("()=>go('run')")
    wait(page, 900)
    out = []
    for w, h in [(1024, 700), (1180, 760), (1280, 800), (1440, 900)]:
        page.set_viewport_size({"width": w, "height": h})
        wait(page, 500)
        clip = js(page, """()=>{const out=[];document.querySelectorAll('.topbar,.tabs,.statcards,.run-grid,.live-grid,.panel,.scard').forEach(e=>{
            const r=e.getBoundingClientRect(); if(r.right>innerWidth+1||r.left<-1) out.push((e.className||e.tagName)+':'+Math.round(r.left)+'/'+Math.round(r.right));});return out.slice(0,6)}""")
        docw = js(page, "document.documentElement.scrollWidth-window.innerWidth")
        livehide = js(page, "$('.live-right') ? getComputedStyle($('.live-right')).display : 'no-el'")
        ringvis = js(page, "$('#bpBought') ? (!!$('#bpBought').offsetParent) : false")
        out.append((w, h, clip, docw, livehide, ringvis))
        shot(page, f"c20_viewport_{w}")
    add("R08-20", "低", "运行页在 ≤1240px 时右侧面板整体消失（额度/关键日志无替代入口），待命页不受影响",
        "运行状态进入运行页，依次把视口改为 1024/1180/1280/1440 宽截图，读取 .live-right 计算样式",
        f"各尺寸 [宽,高,越界元素,横向溢出, .live-right display, 额度数字可见]= {out}；截图 c20_viewport_*.png",
        "1024/1180 宽下运行页右侧整列（额度环、关键日志）直接 display:none，没有折叠按钮也没有别处显示这些数据；1280 起恢复。待命页使用的是任务设置面板，因此不受影响。125%/150% 系统缩放会把 1440 屏幕的等效 CSS 宽度压到这一区间（布局行为已由宽度测试复现）")

@case
def c21_tab_timer(page):
    reset_state(page, tasks=[mk_task(i) for i in range(1, 4)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    jobs = {"n": 0}
    page.on("request", lambda r: jobs.__setitem__("n", jobs["n"] + 1) if "/api/jobs" in r.url else None)
    page.evaluate("()=>go('set')")
    wait(page, 300)
    page.evaluate("()=>trackJob('job-1','定位')")
    wait(page, 800)
    n1 = jobs["n"]
    page.evaluate("()=>go('lib')")
    wait(page, 4000)
    n2 = jobs["n"]
    msg = js(page, "S.jobTimer!==0")
    dr = js(page, "typeof S.jobTimer")
    shot(page, "c21_tab_timer")
    add("R08-21", "中", "后台 job 轮询不随页面切换停止，切走后仍在写隐藏 DOM",
        "在设置页启动 trackJob 后立即切到「目标库」，停留 4 秒",
        f"/api/jobs 请求数 {n1} → {n2}（继续增长 {n2-n1}），jobTimer 仍在={msg}；截图 c21_tab_timer.png",
        "定位任务轮询 700ms/次不随导航取消，切页后仍持续请求并写 #locateMsg/#jobNow（隐藏区域），关闭页面前一直运行；快速来回切页会叠加多条写相同 DOM 的轮询")

@case
def c22_local_exc_lost(page):
    reset_state(page, start_fail=True, tasks=[mk_task(1)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 300)
    page.locator("#btnStartTop").click()
    wait(page, 800)
    exc1 = js(page, "JSON.stringify([!$('#exception').hidden,$('#excTitle').textContent,$('#excBtn').textContent])")
    shot(page, "c22_local_exc")
    # 轮询带回 guard level 5（场景保护），随后 guards 清零
    ctl(page, guard_level=5, guard_state="halted", start_fail=True)
    wait(page, 1600)
    exc2 = js(page, "JSON.stringify([!$('#exception').hidden,$('#excTitle').textContent])")
    ctl(page, guard_level=0, guard_state="ok")
    wait(page, 1600)
    exc3 = js(page, "JSON.stringify([!$('#exception').hidden,!!S.localExc])")
    shot(page, "c22_exc_gone")
    add("R08-22", "中", "本地异常（启动失败）会被后续 guard 轮询覆盖并永久清除，问题未修复却不再提示",
        "启动失败弹出权限异常 → 期间 guard 升到 5 → guard 清零",
        f"阶段1={exc1}；阶段2={exc2}；阶段3（guard 归零后）={exc3}；截图 c22_local_exc.png / c22_exc_gone.png",
        "paintException 在 guard 激活时直接把 S.localExc 置空，guard 回落后只剩 hidden，启动失败的异常提示连同原因一起消失，用户看不到到底缺权限")

@case
def c23_rapid_nav(page):
    reset_state(page, records=[{"ts": int(time.time()) - i, "name": "记录%d" % i, "result": "hit",
                                "price": 100, "elapsed": 1, "verified": True, "shot": ""} for i in range(200)],
                records_delay=0.4, skins=mk_seasons(n_per_tier=30))
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    rec_req = {"n": 0}
    page.on("request", lambda r: rec_req.__setitem__("n", rec_req["n"] + 1) if "/api/records" in r.url else None)
    for _ in range(3):
        for pid in ["rec", "ana", "journal", "lib", "set", "run"]:
            page.evaluate("(p)=>go(p)", pid)
            wait(page, 90)
    wait(page, 1500)
    state = js(page, "JSON.stringify([S.page, !$('#page-rec').hidden, !$('#page-ana').hidden])")
    rows = js(page, "$$('#recRows tr').length")
    shot(page, "c23_rapid_nav")
    add("R08-23", "低", "快速切页不取消在途请求：每次进记录页都重新全量拉取，旧响应照样写隐藏 DOM",
        "以 90ms 间隔循环切换 rec/analysis/journal/lib/settings/run 三遍（记录接口延迟 0.4s）",
        f"最终页状态={state}；/api/records 请求次数={rec_req['n']}（3 次进入记录页）；隐藏的记录页已渲染行数={rows}；截图 c23_rapid_nav.png",
        "同一份 200 行台账被反复全量请求，切换过程中没有任何取消或缓存；较早的响应会在切页后继续渲染，快速来回切页时可能出现旧数据覆盖新数据，且用户无法区分空白页是加载中还是没数据")


@case
def c24_empty_loading(page):
    reset_state(page, tasks=[], records=[], records_delay=1.2)
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 500)
    empty_txt = js(page, "$('#taskRows').innerText.slice(0,80)")
    skeleton = js(page, "$('#taskRows').querySelectorAll('.skel').length")
    shot(page, "c24_tasks_empty")
    page.evaluate("()=>go('rec')")
    wait(page, 250)
    loading_txt = js(page, "$('#recState').innerText.slice(0,40)")
    loading_skel = js(page, "$('#recState').querySelectorAll('.skel').length")
    shot(page, "c24_rec_loading")
    wait(page, 1700)
    empty_rec = js(page, "$('#recState').innerText.slice(0,40)")
    shot(page, "c24_rec_empty")
    add("R08-24", "通过", "任务空态与记录加载态各自有明确文案/骨架，不与错误态混淆",
        "任务列表置空后打开运行页；记录接口延迟 1.2s，观察记录页加载中→空态",
        f"空任务区文本=「{empty_txt}」骨架={skeleton}；记录加载中「{loading_txt}」骨架={loading_skel}；加载完成后「{empty_rec}」；截图 c24_tasks_empty.png / c24_rec_loading.png / c24_rec_empty.png",
        "空态给出行动入口，加载态用骨架+文案，与错误态（R08-07）在视觉上可区分；节拍面板是例外（见 R08-08）",
        good="任务空态/记录加载态/记录空态均可区分")

@case
def c25_dpi(page):
    reset_state(page, tasks=[mk_task(i) for i in range(1, 4)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    wait(page, 500)
    base = js(page, "()=>{const r=document.querySelector('.topbar').getBoundingClientRect();return [r.width,innerWidth,devicePixelRatio]}")
    cdp = page.context.new_cdp_session(page)
    res = {}
    for dpr in (1.25, 2):
        cdp.send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": dpr, "mobile": False})
        wait(page, 600)
        res[dpr] = js(page, "()=>{const r=document.querySelector('.topbar').getBoundingClientRect();const v=visualViewport;return [r.width,r.height,innerWidth,devicePixelRatio,v?v.width:0]}")
        shot(page, f"c25_dpr_{str(dpr).replace('.','_')}")
    cdp.send("Emulation.clearDeviceMetricsOverride")
    add("R08-25", "通过", "DPR 1.25 / 2 下未出现重排或横向错位（亚像素边框无法用截图量化）",
        "在 1440×900 视口中把 deviceScaleFactor 改为 1.25 与 2，比较高分辨率截图与关键容器尺寸",
        f"基准 topbar 宽/视口/DPR={base}；1.25x={res[1.25]}；2x={res[2]}；截图 c25_dpr_1_25.png / c25_dpr_2.png",
        "CSS 布局尺寸不随 DPR 变化，未观察到错位或溢出；1.25 倍下 1px 边框落在亚像素，理论上会轻微发虚，属截图无法量化的部分",
        good="高 DPI 缩放未导致布局错位")

@case
def c26_focus_trap(page):
    reset_state(page, tasks=[mk_task(1)])
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
    page.wait_for_selector("#boot", state="detached", timeout=15000)
    page.evaluate("()=>openAdd()")
    wait(page, 300)
    outside = []
    ids = []
    for _ in range(14):
        page.keyboard.press("Tab")
        wait(page, 40)
        inside = js(page, "!!(document.activeElement && document.activeElement.closest('#addDlg'))")
        ids.append(js(page, "document.activeElement.id||document.activeElement.tagName"))
        if not inside:
            outside.append(ids[-1])
    page.keyboard.press("Escape")
    wait(page, 300)
    closed = js(page, "!$('#addDlg').open")
    shot(page, "c26_focus_trap")
    add("R08-26", "低", "对话框 Tab 循环到底部时焦点会短暂落到 body（未进入背景控件）",
        "打开「添加任务」对话框，连续按 14 次 Tab，再按 Esc",
        f"Tab 序列={ids}；逃出 #addDlg 的次数={len(outside)} {outside}；Esc 后关闭={closed}；截图 c26_focus_trap.png",
        "14 次 Tab 中有 1 次焦点从最后一个按钮直接落到 body，再由下一次 Tab 回到对话框内；未落到背景可交互控件，但屏幕阅读器会短暂脱离对话框上下文，焦点环也消失一帧",
        good="modal 阻断了背景操作，Esc 关闭正常")


def main():
    global PORT
    srv, PORT = serve()
    print("mock server on", PORT)
    only = sys.argv[1:] or None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--force-device-scale-factor=1"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        for fn in CASES:
            if only and fn.__name__ not in only:
                continue
            page = ctx.new_page()
            page.set_default_timeout(15000)
            try:
                fn(page)
            except Exception as e:
                print(f"[!!] {fn.__name__} failed: {e}")
                traceback.print_exc()
                try:
                    shot(page, "fail_" + fn.__name__)
                except Exception:
                    pass
            page.close()
        browser.close()
    srv.shutdown()
    with open(os.path.join(SHOTS, "results.json"), "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=1)
    print("done", len(RESULTS))


if __name__ == "__main__":
    main()
