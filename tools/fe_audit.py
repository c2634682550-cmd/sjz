#!/usr/bin/env python3
"""前端连通性审计（无头）

逐项触发页面上的每个可交互元素，断言：
  1. 带 data-api 的控件确实发出了对应的网络请求；
  2. 触发后界面状态发生了预期变化；
  3. 带 data-gap 的控件一律禁用，且有可读的理由（title / aria-label）；
  4. 没有未处理的 data-act（不会出现"点了没反应"的假按钮）；
  5. 控制台无报错、五种界面状态都能出现、1280~2560 不横向溢出。

用法：
    python tools/fe_audit.py            # 需要 pip install playwright
    python tools/fe_audit.py --shots    # 同时把关键界面截图存到 _fe_shots/
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少 playwright：pip install playwright && playwright install chromium")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

# ---------------------------------------------------------------- mock backend


def T(i, name, status="pending", **kw):
    d = {"id": i, "name": name, "keywords": [name.split(" ")[-1]], "status": status,
         "enabled": True, "quota": 2, "bought": 0, "spent": 0, "budget": 1200,
         "min_price": 100, "max_price": 500, "craft": "", "wear": "", "max_wear": 0.1,
         "season": "S1", "workflow": "countdown", "fire_at": 0}
    d.update(kw)
    return d


class Mock:
    def __init__(self):
        self.running = False
        self.guard = {"state": "ok", "level": 0}
        self.tasks = [T("t1", "AK-12 · 赤霄", bought=1, spent=320, quota=3),
                      T("t2", "M4A1 · 溯源", status="done", bought=1, spent=900, budget=900)]
        self.records = [{"ts": time.time(), "name": "AK-12 · 赤霄", "result": "hit",
                         "price": 320, "elapsed": 0.82, "verified": True,
                         "shot": "", "mode": "detail"},
                        {"ts": time.time() - 60, "name": "M4A1 · 溯源", "result": "miss",
                         "price": 0, "elapsed": 0.0, "verified": False,
                         "shot": "", "mode": "detail"}]
        self.sync = {"offset": 0.012, "delay": 0.003, "server": "ntp.aliyun.com",
                     "at": time.time(), "stale": False,
                     "samples": [{"server": "ntp.aliyun.com", "delay": 0.003, "offset": 0.012},
                                 {"server": "ntp.tencent.com", "delay": 0.006, "offset": 0.020}]}
        self.seq = 0
        self.fail_start = False
        self.calls = []

    def log(self, m, p):
        self.calls.append((m, p))

    def get(self, path):
        b = path.split("?")[0]
        if b == "/api/scheduler":
            return self.scheduler()
        if b == "/api/guard":
            return {"ok": True, "guard": self.guard, "doing": self.guard.get("doing", ""),
                    "engine_state": "watch" if self.running else "idle"}
        if b == "/api/tasks":
            return {"ok": True, "tasks": self.tasks, "config_ok": True, "config_error": None}
        if b == "/api/switches":
            items = [{"key": "fire.delay", "label": "购买延迟", "kind": "int", "value": 120,
                      "desc": "点下购买后等多久再确认，单位毫秒"},
                     {"key": "fire.adaptive", "label": "延迟自适应", "kind": "bool", "value": True,
                      "desc": "按实测延迟自动微调"},
                     {"key": "fire.confirm_clicks", "label": "确认连点", "kind": "int",
                      "value": 20, "desc": "弹窗出现后连续点几下"},
                     {"key": "fire.confirm_gap", "label": "确认间隔", "kind": "int",
                      "value": 10, "desc": "两次点击之间隔多久"},
                     {"key": "fire.jitter", "label": "随机抖动", "kind": "int",
                      "value": 3, "desc": "让点击时间不固定"},
                     {"key": "fire.timeout", "label": "连点超时", "kind": "int",
                      "value": 8000, "desc": "多久还没买到就放弃"}]
            return {"ok": True, "groups": [
                {"name": "运行模式", "items": [items[0]]},
                {"name": "收藏设置", "items": [items[1]]},
                {"name": "环境与安全", "items": [items[2]]},
                {"name": "价格与筛选", "items": [items[3]]},
                {"name": "抢购设置", "items": [items[0], items[4]]},
                {"name": "自动与停止", "items": [items[5]]},
                {"name": "截前", "items": [items[4]]}]}
        if b == "/api/skins":
            return {"ok": True, "seasons": [
                {"season": "S1", "season_name": "第一赛季", "tiers": {
                    "极品": [{"name": "AK-12 · 赤霄", "weapon": "步枪", "gun": "AK-12", "skin": "赤霄"}],
                    "优品": [{"name": "M4A1 · 溯源", "weapon": "步枪", "gun": "M4A1", "skin": "溯源"}]}},
                {"season": "S2", "season_name": "第二赛季", "tiers": {
                    "珍品": [{"name": "SCAR · 裂空", "weapon": "步枪", "gun": "SCAR", "skin": "裂空"}]}}]}
        if b == "/api/hotkey":
            return {"ok": True, "stop": {"spec": "F8", "enabled": True, "active": True},
                    "start": {"spec": "F7", "enabled": True, "active": True}}
        if b == "/api/records":
            return {"ok": True, "rows": self.records,
                    "stats": {"hits": 1, "tries": len(self.records), "spent": 320,
                              "rate": 50.0, "median_s": 0.82, "miss": 1, "no_money": 0,
                              "verified_hits": 1, "unverified_hits": 0}}
        if b == "/api/jobs":
            return {"ok": True, "busy": False, "current": None}
        if b == "/api/config":
            return {"version": "audit", "workflow": "countdown", "ocr": {"threads": 2}}
        return {"ok": True}

    def post(self, path, body):
        b = path.split("?")[0]
        if b == "/api/scheduler/start":
            if self.fail_start:
                return {"ok": False, "msg": "没有权限操作游戏窗口，请用管理员身份重新打开"}
            self.running = True
            return {"ok": True}
        if b == "/api/scheduler/stop":
            self.running = False
            return {"ok": True}
        if b == "/api/tasks/add":
            self.seq += 1
            t = T("n%d" % self.seq, body.get("name") or body.get("keywords", ["新目标"])[0],
                  quota=body.get("quota") or 1, max_price=body.get("max_price") or 500)
            self.tasks.append(t)
            return {"ok": True, "task": t, "tasks": self.tasks}
        if b == "/api/tasks/update":
            ids = body.get("ids")
            if not ids and body.get("id"):
                ids = [body["id"]]
            fields = body.get("values") if isinstance(body.get("values"), dict) else \
                {k: v for k, v in body.items() if k not in ("id", "ids", "values")}
            if ids:
                for t in self.tasks:
                    if t["id"] in ids:
                        t.update(fields)
            elif body.get("enabled") is not None:
                for t in self.tasks:
                    t["enabled"] = body["enabled"]
            return {"ok": True, "tasks": self.tasks}
        if b == "/api/tasks/remove":
            ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
            self.tasks = [t for t in self.tasks if t["id"] not in ids]
            return {"ok": True, "tasks": self.tasks}
        if b == "/api/tasks/clear":
            self.tasks = [t for t in self.tasks if t["status"] not in ("done", "failed", "skipped")]
            return {"ok": True, "tasks": self.tasks}
        if b == "/api/records/clear":
            self.records = []
            return {"ok": True}
        if b == "/api/preflight":
            return {"ok": True, "checks": [
                {"id": "window", "label": "游戏窗口", "ok": True, "detail": "已找到"},
                {"id": "ocr", "label": "画面识别", "ok": True, "detail": "看懂了"},
                {"id": "clock", "label": "时间校准", "ok": False, "detail": "还没校准",
                 "fix": {"tab": "vision", "hint": "去校准一下时间"}},
                {"id": "templates", "label": "模板文件", "ok": True, "detail": "完整"},
                {"id": "integrity", "label": "文件完整性", "ok": True, "detail": "完整"},
                {"id": "driver", "label": "操作驱动", "ok": True, "detail": "可用"},
                {"id": "page", "label": "当前页面", "ok": True, "detail": "交易行"}]}
        if b == "/api/timesync":
            self.sync["offset"] = 0.014
            return {"ok": True, "offset": 0.014, "delay": 0.004, "server": "ntp.aliyun.com",
                    "stratum": 3, "at": time.time(), "samples": self.sync["samples"], "tried": []}
        if b == "/api/guard/resume":
            self.guard = {"state": "ok", "level": 0}
            return {"ok": True}
        if b == "/api/guard/stop":
            self.guard = {"state": "halted", "level": 5, "containment_ok": True,
                          "message": "已经安全停止", "button": {"id": "quit", "label": "知道了，关闭程序"}}
            return {"ok": True}
        return {"ok": True}

    def scheduler(self):
        eng = {"state": "watch" if self.running else "idle", "running": self.running,
               "doing": "正在读取第 3 个目标的价格", "stats": {"clicks": 8, "bought": 1, "spent": 320, "hz": 44},
               "log": [{"t": "12:00:01", "ts": 1, "level": "info", "msg": "开始盯盘"}],
               "guard": self.guard}
        return {"ok": True, "running": self.running, "active": "t1" if self.running else None,
                "tasks": self.tasks, "log": eng["log"], "engine": eng,
                "clock": self.sync, "page": {"page": "market", "at": time.time(), "stale": False}}


class H(SimpleHTTPRequestHandler):
    mock = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, *a):
        pass

    def _send(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self.mock.log("GET", self.path)
            self._send(self.mock.get(self.path))
            return
        super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        self.mock.log("POST", self.path)
        self._send(self.mock.post(self.path, body))


# ------------------------------------------------------------------- the audit

FAILS = []
CHECKS = 0


def ok(cond, msg):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(msg)
    return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", action="store_true")
    args = ap.parse_args()

    mock = Mock()
    H.mock = mock
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % port
    shots_dir = os.path.join(ROOT, "_fe_shots")
    if args.shots:
        os.makedirs(shots_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 960})
        errors = []
        page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
        page.on("console", lambda m: errors.append("console: %s" % m.text)
                if m.type == "error" and "favicon" not in m.text
                and "net::ERR" not in m.text
                and "Failed to load resource" not in m.text
                and "the server responded with a status of" not in m.text else None)
        page.goto(base + "index.html", wait_until="networkidle")
        page.wait_for_timeout(500)

        def log_len():
            return page.evaluate("window.__apiLog.length")

        def hit_since(n, path=None):
            rows = page.evaluate("window.__apiLog.slice(%d)" % n)
            if path:
                return [r for r in rows if r["path"].split("?")[0] == path]
            return rows

        def click(sel):
            page.locator(sel).first.click()
            page.wait_for_timeout(320)

        def confirm():
            if page.locator("#dlg").is_visible():
                page.locator("#dlgOk").click()
                page.wait_for_timeout(320)

        def tab(name):
            page.locator('.tabs [data-page="%s"]' % name).click()
            page.wait_for_timeout(260)

        def shot(n):
            if args.shots:
                page.screenshot(path=os.path.join(shots_dir, n + ".png"))

        # 基础渲染
        ok(page.locator("#statCards .scard").count() == 4, "四张状态卡没有渲染")
        ok(page.locator("#taskRows tr").count() == 2, "任务表没有渲染 2 行")
        ok(page.locator("#gText").inner_text().strip() != "", "全局状态带没有文案")
        ok(page.locator("#boot").is_hidden(), "启动遮罩没有消失")
        shot("audit-01-idle")

        # ---- 启动 / 停止
        n = log_len()
        click("#btnStartTop")
        ok(len(hit_since(n, "/api/scheduler/start")) == 1, "启动按钮没有请求 /api/scheduler/start")
        page.wait_for_timeout(1200)
        ok(mock.running, "启动后后端没进入运行态")
        ok(page.locator("#startTopTxt").inner_text() == "停止抢购", "启动后按钮文案没变")
        ok(page.locator("#runLive").is_visible(), "启动后运行态没出现")
        shot("audit-02-live")
        n = log_len()
        click("#btnStartTop")
        confirm()
        page.wait_for_timeout(1200)
        ok(len(hit_since(n, "/api/scheduler/stop")) == 1, "停止按钮没有请求 /api/scheduler/stop")
        ok(page.locator("#startTopTxt").inner_text() == "启动抢购", "停止后按钮文案没恢复")

        # ---- 任务表：选中 / 保存 / 删除
        page.locator("#taskRows tr").first.click()
        page.wait_for_timeout(200)
        ok(page.locator("#setForm").is_visible(), "选中任务后没有显示设置面板")
        page.locator("#setMaxPrice").fill("640")
        page.wait_for_timeout(120)
        n = log_len()
        click("#btnTaskSave")
        ok(len(hit_since(n, "/api/tasks/update")) == 1, "保存修改没有请求 /api/tasks/update")
        page.wait_for_timeout(600)
        ok(mock.tasks[0]["max_price"] == 640, "保存修改没有传到后端")
        n = log_len()
        click("#btnTaskDel")
        page.wait_for_timeout(200)
        ok(page.locator("#dlg").is_visible(), "删除没有二次确认")
        confirm()
        page.wait_for_timeout(600)
        ok(len(hit_since(n, "/api/tasks/remove")) == 1, "删除任务没有请求 /api/tasks/remove")
        ok(len(mock.tasks) == 1, "删除后端没生效")

        # ---- 批量与清空
        n = log_len()
        click('[data-act="bulk-on"]')
        ok(len(hit_since(n, "/api/tasks/update")) >= 1, "批量启用没有请求")
        n = log_len()
        click('[data-act="bulk-off"]')
        confirm()
        page.wait_for_timeout(700)
        ok(len(hit_since(n, "/api/tasks/update")) >= 1, "全部停用没有请求")
        n = log_len()
        click('[data-act="clear-done"]')
        confirm()
        page.wait_for_timeout(700)
        ok(len(hit_since(n, "/api/tasks/clear")) == 1, "清空已完成没有请求")

        # ---- 添加任务
        click('[data-act="task-add-dlg"]')
        ok(page.locator("#addDlg").is_visible(), "添加任务没有弹出对话框")
        page.locator("#addName").fill("审计用目标")
        n = log_len()
        click("#addOk")
        page.wait_for_timeout(600)
        ok(len(hit_since(n, "/api/tasks/add")) == 1, "添加任务没有请求 /api/tasks/add")
        ok(any(t["name"] == "审计用目标" for t in mock.tasks), "添加任务后端没生效")

        # ---- 目标库
        tab("lib")
        page.wait_for_timeout(500)
        ok(page.locator("#libGrid .gcard").count() >= 3, "目标库没有卡片")
        n = log_len()
        page.locator(".gcard [data-act=lib-locate]").first.click()
        page.wait_for_timeout(500)
        ok(len(hit_since(n, "/api/skins/locate")) == 1, "定位没有请求")
        n = log_len()
        page.locator(".gcard [data-act=lib-target]").first.click()
        page.wait_for_timeout(500)
        ok(len(hit_since(n, "/api/skins/target")) == 1, "设为目标没有请求")
        before = len(mock.tasks)
        n = log_len()
        page.locator(".gcard [data-act=lib-add]").first.click()
        page.wait_for_timeout(600)
        ok(len(hit_since(n, "/api/tasks/add")) == 1, "加入方案没有请求")
        ok(len(mock.tasks) == before + 1, "加入方案后端没生效")
        page.locator('#libTypes .chip[data-val="步枪"]').click()
        page.wait_for_timeout(260)
        ok(page.locator('#libTypes .chip[data-val="步枪"]').get_attribute("class").find("on") >= 0,
           "类型筛选没有选中")
        ok("/ 3 件" in page.locator("#libCount").inner_text() or
           page.locator("#libCount").inner_text().startswith("3"), "类型筛选没有生效")
        page.locator('[data-act="lib-reset"]').first.click()
        page.wait_for_timeout(200)
        ok(page.locator("#libCount").inner_text().startswith("3"), "清空筛选没有生效")
        page.locator('.lib-toolbar [data-view="list"]').click()
        page.wait_for_timeout(200)
        ok("lib-list" in (page.locator("#libGrid").get_attribute("class") or ""), "列表视图没切换")
        shot("audit-03-library")

        # ---- 记录
        tab("rec")
        page.wait_for_timeout(500)
        ok(page.locator("#recRows tr").count() >= 2, "记录表没有行")
        n = log_len()
        click('[data-act="rec-refresh"]')
        ok(len(hit_since(n, "/api/records")) == 1, "刷新记录没有请求")
        n = log_len()
        click('[data-act="rec-clear"]')
        page.wait_for_timeout(200)
        ok(page.locator("#dlg").is_visible(), "清空记录没有二次确认")
        confirm()
        page.wait_for_timeout(600)
        ok(len(hit_since(n, "/api/records/clear")) == 1, "清空记录没有请求")
        ok(len(mock.records) == 0, "清空记录后端没生效")
        ok("还没有成交记录" in page.locator("#recState").inner_text(), "清空后没有空状态")
        shot("audit-04-records")

        # ---- 日记
        tab("journal")
        page.wait_for_timeout(400)
        n = log_len()
        click('[data-act="j-refresh"]')
        ok(len(hit_since(n, "/api/scheduler")) >= 1, "刷新日记没有请求")
        page.locator("#jSearch").fill("开始")
        page.wait_for_timeout(200)
        ok(page.locator("#jCount").inner_text().strip() != "", "日记筛选没有计数")

        # ---- 设置：热键 / 检查 / 校时
        tab("set")
        page.locator('.setnav [data-setpane="hotkey"]').click()
        page.wait_for_timeout(200)
        page.locator("#hkStartSpec").fill("F7")
        page.locator("#hkStopSpec").fill("F8")
        n = log_len()
        click('[data-act="hk-save"]')
        hits = hit_since(n, "/api/hotkey")
        ok(len([h for h in hits if h["method"] == "POST"]) == 1, "保存热键没有请求")
        page.wait_for_timeout(400)
        ok("已保存" in page.locator("#hkMsg").inner_text(), "保存热键没有反馈")

        page.locator('.setnav [data-setpane="vision"]').click()
        page.wait_for_timeout(200)
        n = log_len()
        click('[data-act="preflight"]')
        ok(len(hit_since(n, "/api/preflight")) == 1, "启动前检查没有请求")
        page.wait_for_timeout(500)
        ok(page.locator("#pfChecks .check").count() == 7, "检查结果没有渲染 7 项")
        n = log_len()
        click('[data-act="timesync"]')
        ok(len(hit_since(n, "/api/timesync")) == 1, "校时没有请求")
        page.wait_for_timeout(500)
        ok(page.locator("#tsOffset").inner_text().strip() not in ("", "—"), "校时后偏差没有显示")
        ok(page.locator("#ntpRows tr").count() == 2, "NTP 表没有渲染")
        shot("audit-05-settings")

        # ---- 设置：实验工具 / 控制台
        page.locator('.setnav [data-setpane="lab"]').click()
        page.wait_for_timeout(200)
        page.locator("#locateName").fill("AK-12 · 赤霄")
        n = log_len()
        click('[data-act="locate"]')
        ok(len(hit_since(n, "/api/skins/locate")) == 1, "开始定位没有请求")
        page.wait_for_timeout(400)
        ok(page.locator("#locateMsg").inner_text().strip() != "", "定位没有反馈")
        n = log_len()
        page.locator('[data-act="follow"][data-follow="1"]').click()
        page.wait_for_timeout(400)
        ok(len(hit_since(n, "/api/follow")) == 1, "关注没有请求")
        n = log_len()
        page.locator('[data-act="follow"][data-follow="0"]').click()
        page.wait_for_timeout(400)
        ok(len(hit_since(n, "/api/follow")) >= 1, "取消关注没有请求")
        n = log_len()
        click('[data-act="target"]')
        ok(len(hit_since(n, "/api/skins/target")) >= 1, "标记目标没有请求")
        page.locator('.setnav [data-setpane="console"]').click()
        page.wait_for_timeout(200)
        n = log_len()
        click('[data-act="cfg-refresh"]')
        ok(len(hit_since(n, "/api/config")) == 1, "读取配置没有请求")
        n = log_len()
        click('[data-act="job-check"]')
        ok(len(hit_since(n, "/api/jobs")) == 1, "查询任务没有请求")

        # ---- 节拍开关
        page.locator('.setnav [data-setpane="fire"]').click()
        page.wait_for_timeout(300)
        sw = page.locator("#swFire [data-sw]").first
        if ok(sw.count() > 0, "节拍面板没有可保存的开关"):
            n = log_len()
            if sw.evaluate("e => e.tagName") == "BUTTON":
                sw.click()
            else:
                sw.fill("150")
                sw.dispatch_event("change")
            page.wait_for_timeout(700)
            ok(len(hit_since(n, "/api/switches")) >= 1, "节拍开关没有保存 /api/switches")

        # ---- 输入边界
        page.locator('.setnav [data-setpane="general"]').click()
        page.wait_for_timeout(150)
        tab("run")
        page.wait_for_timeout(1300)
        page.locator("#taskRows tr").first.click()
        page.wait_for_timeout(200)
        page.locator("#setMinPrice").fill("900")
        page.wait_for_timeout(250)
        ok("最低价" in page.locator("#msgPrice").inner_text(), "越界输入没有即时提示")
        ok(page.locator("#btnTaskSave").is_disabled(), "越界输入时保存按钮没禁用")
        page.locator("#setMinPrice").fill("100")
        page.wait_for_timeout(250)
        ok(page.locator("#btnTaskSave").is_enabled(), "改回合法值后保存按钮没恢复")

        # ---- 密度切换
        dens = page.evaluate("document.body.classList.contains('compact')")
        page.locator("#btnDensity").click()
        page.wait_for_timeout(200)
        ok(page.evaluate("document.body.classList.contains('compact')") != dens, "紧凑切换没有生效")
        page.locator("#btnDensity").click()
        page.wait_for_timeout(200)

        # ---- 键盘：Esc 关闭 / Enter 提交
        page.locator('[data-act="task-add-dlg"]').click()
        page.wait_for_timeout(250)
        ok(page.locator("#addDlg").is_visible(), "添加对话框没打开")
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        ok(page.locator("#addDlg").is_hidden(), "Esc 没有关掉对话框")
        page.locator('[data-act="task-add-dlg"]').click()
        page.wait_for_timeout(250)
        page.locator("#addName").fill("回车提交的目标")
        n = log_len()
        page.locator("#addName").press("Enter")
        page.wait_for_timeout(700)
        ok(len(hit_since(n, "/api/tasks/add")) == 1, "Enter 没有提交添加任务")
        ok(page.locator("#addDlg").is_hidden(), "Enter 提交后对话框没关")
        mock.tasks = [t for t in mock.tasks if t["name"] != "回车提交的目标"]

        # ---- 记录读取失败
        page.route("**/api/records*", lambda r: r.fulfill(status=500, body="{}"))
        tab("rec")
        page.wait_for_timeout(300)
        click('[data-act="rec-refresh"]')
        page.wait_for_timeout(600)
        ok("记录读不出来" in page.locator("#recState").inner_text(), "记录出错时没有错误界面")
        page.unroute("**/api/records*")
        n = log_len()
        click('[data-act="load-records"]')
        page.wait_for_timeout(700)
        ok(len(hit_since(n, "/api/records")) >= 1, "重新读取没有请求")
        ok("记录读不出来" not in page.locator("#recState").inner_text(), "重试后错误界面没消失")

        # ---- 任务读取失败
        page.route("**/api/tasks", lambda r: r.fulfill(status=500, body="{}"))
        tab("run")
        page.wait_for_timeout(400)
        page.evaluate("loadTasks(true)")
        page.wait_for_timeout(800)
        ok("任务读不出来" in page.locator("#runIdle").inner_text(), "任务出错时没有错误界面")
        shot("audit-06-taskfail")
        page.unroute("**/api/tasks")
        n = log_len()
        page.evaluate("document.querySelector('[data-act=\"load-tasks\"]').click()")
        page.wait_for_timeout(900)
        ok(len(hit_since(n, "/api/tasks")) >= 1, "任务重新读取没有请求")
        ok("任务读不出来" not in page.locator("#runIdle").inner_text(), "重试后任务错误界面没消失")

        # ---- 启动失败（权限）
        mock.fail_start = True
        tab("run")
        page.wait_for_timeout(1000)
        click("#btnStartTop")
        page.wait_for_timeout(900)
        ok(page.locator("#exception").is_visible(), "启动失败没有全屏提示")
        ok("权限" in page.locator("#excDesc").inner_text(), "启动失败没有说明原因")
        shot("audit-06-permission")
        mock.fail_start = False
        click("#excBtn")
        page.wait_for_timeout(1200)
        ok(page.locator("#exception").is_hidden(), "重新检查后异常层没收起")

        # ---- 待接入控件
        gaps = page.locator("[data-gap]")
        for i in range(gaps.count()):
            el = gaps.nth(i)
            gid = el.get_attribute("data-gap")
            tag = el.evaluate("e => e.tagName")
            disabled = el.is_disabled() if tag in ("BUTTON", "INPUT", "SELECT") else \
                el.get_attribute("aria-disabled") == "true" or el.get_attribute("disabled") is not None
            ok(disabled, "待接入控件没有禁用：%s" % gid)
            ok(bool((el.get_attribute("title") or "").strip()), "待接入控件没有理由：%s" % gid)
        ok(gaps.count() >= 8, "待接入标记数量异常")

        # ---- 异常页（4 级）
        mock.guard = {"state": "waiting_human", "level": 4, "level_text": "需要你",
                      "scene_text": "页面偏离",
                      "message": "游戏画面跑到别的地方了，抢购已经暂停",
                      "goal": "需要你把游戏切回交易行，程序才敢继续点",
                      "attempts": 2, "plan": ["a", "b", "c"],
                      "history": [{"t": time.time(), "scene": "x", "action": "plan", "ok": False},
                                  {"t": time.time(), "scene": "x", "action": "guide", "ok": True}],
                      "containment_pending": False, "containment_ok": True,
                      "button": {"id": "resume", "label": "我已手动回去，继续"}}
        tab("run")
        page.wait_for_timeout(1600)
        ok(page.locator("#exception").is_visible(), "4 级异常没有占满工作区")
        text = page.locator("#exception").inner_text()
        for w in ("OCR", "锚点", "DPI", "注入", "UIPI", "Traceback"):
            ok(w not in text, "异常页出现了技术词：%s" % w)
        ok(page.locator("#excBtn").is_visible(), "异常页没有主按钮")
        ok(page.locator("#excBtn").inner_text() == "我已手动回去，继续", "异常页按钮文案不是后端给的")
        ok(page.locator("#excStep").inner_text().find("3") >= 0, "异常页没有步骤进度")
        ok(page.locator("#excBtn").is_enabled(), "可继续时按钮不该禁用")
        shot("audit-06-exception")
        n = log_len()
        click("#excBtn")
        page.wait_for_timeout(900)
        ok(len(hit_since(n, "/api/guard/resume")) == 1, "继续没有请求 /api/guard/resume")
        ok(page.locator("#exception").is_hidden(), "继续后异常页没收起")

        # ---- 4 级但后端建议结束（/api/guard/stop）
        mock.guard = {"state": "waiting_human", "level": 4, "level_text": "需要你",
                      "scene_text": "连续失败",
                      "message": "同一个目标连续几次都没点到，程序停下来了。",
                      "goal": "继续尝试可能触发限制，先结束这一轮更稳妥。",
                      "attempts": 3, "plan": ["a", "b"], "history": [],
                      "containment_pending": False, "containment_ok": True,
                      "button": {"id": "stop", "label": "放弃这一轮"}}
        page.wait_for_timeout(1500)
        ok(page.locator("#excBtn").inner_text() == "放弃这一轮", "结束按钮文案不是后端给的")
        n = log_len()
        click("#excBtn")
        page.wait_for_timeout(200)
        ok(page.locator("#dlg").is_visible(), "结束这一轮没有二次确认")
        confirm()
        page.wait_for_timeout(800)
        ok(len(hit_since(n, "/api/guard/stop")) == 1, "结束没有请求 /api/guard/stop")
        mock.guard = {"state": "ok", "level": 0}
        page.wait_for_timeout(1300)

        # ---- 5 级 + 收尾中
        mock.guard = {"state": "halted", "level": 5, "level_text": "已停止",
                      "scene_text": "疑似误买",
                      "message": "程序发现可能买错了东西，已经立刻停下并开始还原。",
                      "goal": "先把多买的东西退回去，再让你确认。",
                      "attempts": 4, "plan": ["a", "b", "c", "d"],
                      "containment_pending": True, "containment_ok": None,
                      "history": [{"t": time.time(), "scene": "x", "action": "halt", "ok": True}],
                      "button": {"id": "quit", "label": "知道了，关闭程序"}}
        page.wait_for_timeout(1600)
        ok(page.locator("#exception").is_visible(), "5 级没有切到终止页")
        ok("正在收尾" in page.locator("#exception").inner_text(), "收尾中没有显示「正在收尾」")
        ok("已停止" not in page.locator("#excTitle").inner_text(), "收尾中错误显示已停止")
        ok(page.locator("#excBtn").is_disabled(), "收尾中关闭按钮没禁用")
        ok(page.locator("#excHist").count() == 1, "没有最近发生列表")
        shot("audit-07-halt")
        mock.guard["containment_pending"] = False
        mock.guard["containment_ok"] = True
        page.wait_for_timeout(1400)
        ok(page.locator("#excBtn").is_enabled(), "收尾完成后按钮没恢复")
        n = log_len()
        click("#excBtn")
        page.wait_for_timeout(200)
        ok(page.locator("#dlg").is_visible(), "关闭程序没有二次确认")
        confirm()
        page.wait_for_timeout(800)
        ok(len(hit_since(n, "/api/quit")) >= 1, "关闭程序没有请求 /api/quit")
        mock.guard = {"state": "ok", "level": 0}

        # ---- 空状态
        mock.tasks = []
        tab("run")
        page.wait_for_timeout(1300)
        ok("还没有" in page.locator("#runIdle").inner_text() or
           "添加" in page.locator("#runIdle").inner_text(), "任务为空时没有引导")
        shot("audit-08-empty")

        # ---- 离线状态
        mock.tasks = [T("t1", "AK-12 · 赤霄")]
        page.route("**/api/**", lambda r: r.abort())
        page.wait_for_timeout(2600)
        ok(page.locator("#connOverlay").is_visible(), "后端断开后没有离线界面")
        ok(page.locator("#connTitle").inner_text().strip() != "", "离线界面没有说明")
        page.unroute("**/api/**")
        page.wait_for_timeout(2200)
        ok(page.locator("#connOverlay").is_hidden(), "后端恢复后离线界面没有消失")

        # ---- 布局
        for w, h in [(1280, 720), (1440, 900), (1920, 1080), (2560, 1440)]:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(260)
            over = page.evaluate("document.documentElement.scrollWidth > "
                                 "document.documentElement.clientWidth + 1")
            ok(not over, "%dx%d 出现横向溢出" % (w, h))
        shot("audit-09-2560")

        # ---- 全量 data-api 覆盖：控件声明的接口必须真的被触发过
        declared = page.evaluate("""() => {
            const set = new Set();
            document.querySelectorAll('[data-api]').forEach(e => {
                const p = (e.dataset.api || '').split('?')[0]; if (p) set.add(p);
            });
            return Array.from(set);
        }""")
        logged = page.evaluate("() => Array.from(new Set(window.__apiLog.map(r => r.path.split('?')[0])))")
        ok(len(declared) >= 15, "data-api 声明数量异常：%d" % len(declared))
        for p in declared:
            ok(p in logged, "控件声明的接口从未触发：%s" % p)

        # ---- 未处理的 data-act
        known = {
            "start", "stop", "gaps", "task-add-dlg", "task-edit", "task-del", "task-save",
            "bulk-on", "bulk-off", "clear-done", "task-add-ok", "task-filter-reset",
            "lib-filter", "lib-view", "lib-reset", "lib-locate", "lib-target", "lib-add",
            "rec-refresh", "rec-clear", "load-records", "j-refresh", "j-filter-reset",
            "go-run", "hk-toggle", "hk-save", "preflight", "timesync", "locate", "follow",
            "target", "quit", "cfg-refresh", "job-check", "reconnect", "exc-fix",
            "exc-custom", "load-tasks", "ask", "density", "sel-task",
        }
        acts = page.evaluate("""() => {
            const out = {};
            document.querySelectorAll('[data-act]').forEach(e => {
                const a = e.dataset.act; if (!a) return;
                out[a] = (out[a] || 0) + 1;
            });
            return out;
        }""")
        for a in acts:
            if a in known:
                continue
            known_missing = True
            ok(False, "未处理的 data-act：%s" % a)

        browser.close()
    srv.shutdown()

    for e in errors[:10]:
        FAILS.append(e)
    print("检查 %d 项，失败 %d 项" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  FAIL  %s" % f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
