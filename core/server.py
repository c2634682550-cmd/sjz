"""本地 Web 服务。只用标准库，不额外引依赖。

端点：
  GET  /                      前端页面
  GET  /api/config            读配置
  POST /api/config            改配置（部分字段）
  POST /api/scheduler/start   {dry_run}
  POST /api/scheduler/stop
  POST /api/tasks/add         {name, keywords, min_price, ...} 空名 = 抢所有皮肤
  POST /api/tasks/clear       清掉已完成
  GET  /api/tasks             队列（控制台只维护一条「所有皮肤」任务）
  GET  /api/scheduler         调度器状态
  GET  /api/timesync
  POST /api/timesync          校时
  GET  /api/hotkey            热键状态
  POST /api/hotkey            {spec, enabled} 换热键 / 开关
  GET  /api/jobs              后台动作进度（?id=xxx 查具体一个）
  POST /api/skins/locate      定位某件砖（异步，返回 job id；?sync 退回同步）
  GET  /api/records           成交记录
  POST /api/records/clear     清空记录
  POST /api/preflight         启动前检查
  GET  /shots/<file>          成交截图
  GET  /assets/<file>         前端引用的图片
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import preflight, timesync
from .config import Config, DATA_DIR
from . import records
from . import switches
from .hotkey import Hotkey
from .pagewatch import PageWatcher
from .scheduler import Scheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web")

_scheduler = Scheduler()
# 盯当前页面：用户随时要知道「现在停在哪个界面」。画面不变就不做 OCR，
# 所以几乎不花钱（见 core/pagewatch.py）。
_watcher = PageWatcher()
_sync_lock = threading.Lock()
_hotkey = None          # 停止热键
_hotkey_start = None    # 开抢热键
_hotkey_pre = None      # 休市截前热键


def _as_list(value):
    """把「材质/颜色/分类」这种多选值统一成列表。

    界面传数组，配置文件里可能是逗号分隔的字符串 —— 两种都收。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.replace("，", ",").replace("、", ",")
        return [v.strip() for v in text.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


# 控制台「抢购条件」在 config.json 的 console 段里存着，按类型分开处理：
# 数字的直接转 float，字符串原样存，多选的存成列表。
_CONSOLE_NUM = ("min_price", "max_price", "quota", "budget", "max_wear")
_CONSOLE_TEXT = ("workflow", "craft", "wear", "sort")
_CONSOLE_LIST = ("materials", "colors", "categories")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---------- 基础 ----------

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # ---------- 路由 ----------

    def do_GET(self):
        u = urlparse(self.path)
        path, query = u.path, parse_qs(u.query)
        return self._guard(lambda: self._get(path, query), path)

    def _get(self, path, query):
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/api/config":
            return self._json(Config.load().public())
        if path == "/api/jobs":
            # 后台动作（定位那类慢活）的进度。不带 id 就返回「现在在跑什么」
            from . import jobs
            jid = (query.get("id", [""])[0] or "").strip()
            if not jid:
                return self._json({"ok": True, "busy": jobs.busy(),
                                   "current": jobs.current()})
            st = jobs.status(jid)
            if st is None:
                return self._json({"ok": False, "msg": "没有这个任务"}, 404)
            return self._json({"ok": True, "job": st})
        if path == "/api/tasks":
            return self._json({"tasks": _scheduler.tasks()})
        if path == "/api/scheduler":
            st = _scheduler.status()
            st["page"] = _watcher.snapshot()
            return self._json(st)
        if path == "/api/timesync":
            return self._json(timesync.info())
        if path == "/api/hotkey":
            return self._json(self._hotkey_status())
        if path == "/api/records":
            raw = (query.get("limit", ["200"])[0] or "200")
            try:
                limit = max(1, min(5000, int(raw)))
            except (TypeError, ValueError):
                return self._json({"ok": False,
                                   "msg": f"limit 要是整数，收到 {raw!r}"}, 400)
            rows = records.load(limit=limit)
            return self._json({"ok": True, "rows": rows, "stats": records.stats()})
        if path == "/api/skins":
            return self._skins()
        if path == "/api/switches":
            # 开关表：定义 + 当前值，按分组排好。界面直接照着画，
            # 新增开关只要改 core/switches.py，前端不用动。
            cfg = Config.load()
            return self._json({"ok": True, "groups": switches.snapshot(cfg)})
        if path == "/api/skins/plan":
            # 这件皮肤在筛选面板上该点哪儿：赛季下拉选哪项、品阶第几框、
            # 枪种哪个框。界面拿它显示，排查时也直接看这一条。
            from . import skinfilter
            name = (query.get("name", [""])[0] or "").strip()
            p = skinfilter.plan(name)
            if not p:
                return self._json({"ok": False, "msg": f"表里没有「{name}」"}, 404)
            return self._json({"ok": True, "plan": p,
                                "text": skinfilter.describe(name)})
        if path.startswith("/shots/"):
            return self._shot_file(path[len("/shots/"):])
        # web/ 下的普通文件（assets/ 里的图片等）。放在 API 之后，避免抢占路由。
        if path.startswith("/assets/"):
            return self._asset(path[len("/assets/"):])
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        routes = {
            "/api/config": lambda: self._save_config(data),
            "/api/tasks/add": lambda: self._task_add(data),
            "/api/tasks/clear": lambda: self._task_clear(),
            "/api/tasks/remove": lambda: self._task_remove(data),
            "/api/tasks/update": lambda: self._task_update(data),
            "/api/scheduler/start": lambda: self._sched_start(data),
            "/api/scheduler/stop": lambda: self._sched_stop(),
            "/api/timesync": lambda: self._do_timesync(data),
            "/api/hotkey": lambda: self._set_hotkey(data),
            "/api/preflight": lambda: self._preflight(data),
            "/api/records/clear": lambda: self._json(
                {"ok": records.clear(), "stats": records.stats()}),
            "/api/skins/locate": lambda: self._skin_locate(data),
            "/api/follow": lambda: self._follow(data),
            "/api/switches": lambda: self._set_switches(data),
            "/api/skins/target": lambda: self._skin_target(data),
        }
        fn = routes.get(path)
        if fn:
            return self._guard(fn, path)
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def _guard(self, fn, path):
        """跑一个接口处理函数，异常也要变成一条 JSON 响应。

        以前没有这层：处理函数里任何一个没接住的异常（比如 /api/timesync
        收到 samples="abc"，int() 直接 ValueError）都会让 BaseHTTPRequestHandler
        把连接掐掉 —— 前端看到的是「网络错误」，日志里只有一段 traceback，
        既不知道哪个接口、也不知道为什么。现在统一回 500 + 错误摘要。
        """
        try:
            return fn()
        except BrokenPipeError:
            return None                       # 浏览器提前关了，不算错
        except Exception as e:                # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                return self._json({"ok": False,
                                   "msg": f"{type(e).__name__}: {e}",
                                   "path": path}, 500)
            except Exception:                 # noqa: BLE001
                return None

    # ---------- 静态文件 / 配置 ----------

    def _static(self, name, ctype):
        p = os.path.join(STATIC, name)
        if not os.path.isfile(p):
            return self._send(404, "missing " + name, "text/plain; charset=utf-8")
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype)

    def _asset(self, rel):
        """web/assets/ 下的文件。只认白名单后缀，且必须仍在 assets 目录内。

        前端引用的背景图和 logo 走这里。名字来自 HTTP 路径，所以必须挡住
        `../` 穿越 —— 本服务绑在 127.0.0.1，但浏览器里任何一个页面都能发请求。
        """
        ext = os.path.splitext(rel)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon"}.get(ext)
        if not ctype:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        base = os.path.join(STATIC, "assets")
        p = os.path.normpath(os.path.join(base, rel))
        if not (p == base or p.startswith(base + os.sep)) or not os.path.isfile(p):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype)

    def _shot_file(self, rel):
        """data/shots/ 下的成交截图。名字来自 HTTP 路径，必须挡住 `../` 穿越。

        路径里的中文是 percent-encoded 的（商品名直接进了文件名），
        不 unquote 就永远 404 —— 而浏览器只会显示一张破图，看不出为什么。
        """
        rel = unquote(rel)
        if not rel.lower().endswith(".jpg"):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        base = os.path.join(DATA_DIR, "shots")
        p = os.path.normpath(os.path.join(base, rel))
        if not (p == base or p.startswith(base + os.sep)) or not os.path.isfile(p):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        with open(p, "rb") as f:
            self._send(200, f.read(), "image/jpeg")

    def _save_config(self, data):
        # 事务：读盘 → 改 → 落盘 在同一把锁里。以前是 load 出一份全量快照
        # 改完整份写回，调度器那边同时改任务状态时，两边后写的会把先写的
        # 整份盖掉（用户看到「设置改完几秒后自己变回去」）。
        with Config.transaction() as cfg:
            for key in ("threshold", "click_hz", "confirm_clicks", "confirm_interval_ms",
                        "fire_timeout_s", "failsafe", "trigger_mode", "workflow", "monitor",
                        "save_shots", "arm_grace_s", "list_max_tries", "input_mode"):
                if key in data:
                    cfg[key] = data[key]
            # 界面上的抢购条件也存 —— 关掉再打开要能接着干
            if isinstance(data.get("console"), dict):
                cur = dict(cfg.get("console") or {})
                for key in _CONSOLE_NUM:
                    if key in data["console"]:
                        try:
                            cur[key] = float(data["console"][key])
                        except (TypeError, ValueError):
                            pass
                for key in _CONSOLE_TEXT:
                    if key in data["console"]:
                        cur[key] = str(data["console"][key] or "")
                for key in _CONSOLE_LIST:
                    if key in data["console"]:
                        cur[key] = _as_list(data["console"][key])
                cfg["console"] = cur
            public = cfg.public()
        return self._json({"ok": True, "config": public})

    # ---------- 队列 ----------

    def _task_add(self, data):
        # name 传 null 和「不传」应当一样：都是「不按名字过滤」。
        # 以前 null 会被 scheduler.add 判成「商品名不能为空」，
        # 前端顺手写个 {name: null} 就加不进去，很莫名。
        name = data.get("name")
        task, err = _scheduler.add(
            "" if name is None else name,
            keywords=data.get("keywords") or [],
            fire_at=data.get("fire_at") or 0,
            note=data.get("note", ""),
            template_from=data.get("template_from"),
            mode=data.get("mode") or "detail",
            min_price=data.get("min_price") or 0,
            max_price=data.get("max_price") or 0,
            max_wear=data.get("max_wear") or 0,
            craft=data.get("craft") or "",
            wear=data.get("wear") or "",
            season=data.get("season") or "",
            sort=data.get("sort") or "price",
            quota=data.get("quota") or 1,
            budget=data.get("budget") or 0,
            workflow=data.get("workflow") or None,
            materials=_as_list(data.get("materials")),
            colors=_as_list(data.get("colors")),
            categories=_as_list(data.get("categories")),
        )
        if err:
            return self._json({"ok": False, "msg": err}, 400)
        return self._json({"ok": True, "task": task, "tasks": _scheduler.tasks()})

    def _skins(self):
        """典藏外观目录：每个赛季 × 四个品阶的全部皮肤。

        界面用它列出「有哪些砖、各自属于哪个赛季/品阶/枪种」。
        """
        from . import bricks
        out = []
        for s in bricks._load().get("seasons", []):
            tiers = {}
            for tier, items in (s.get("tiers") or {}).items():
                tiers[tier] = [{"name": i.get("name", ""),
                                "weapon": i.get("weapon", ""),
                                "gun": i.get("gun", ""),
                                "skin": i.get("skin", "")} for i in items]
            out.append({"season": s.get("season", ""),
                        "season_name": s.get("name", ""),
                        "series": s.get("series", ""),
                        "tiers": tiers})
        return self._json({"ok": True, "seasons": out})

    def _skin_locate(self, data):
        """在游戏里定位并选中某件砖 —— 提交到后台执行，立刻返回 job id。

        这个动作要抢前台、滚列表、跑 OCR，一次几秒到几十秒。以前它同步跑
        在 handler 里：浏览器一直转圈、没有进度、超时了也不知道它在干
        什么，而它正在动用户的鼠标。现在校验完参数就返回，前端轮询
        /api/jobs?id=...。

        传 {"sync": true} 可以退回同步执行（脚本/curl 调试用）。
        """
        from . import actions, bricks, jobs
        name = (data.get("name") or "").strip()
        if not name:
            return self._json({"ok": False, "msg": "没说要找哪一件"}, 400)
        if _scheduler.status().get("running"):
            return self._json({"ok": False, "msg": "正在抢购，先停掉再定位"}, 409)

        hit = bricks.find_skin(name, tier=data.get("tier") or None,
                               season=data.get("season") or None)
        if not hit:
            return self._json({"ok": False, "msg": f"目录里没有「{name}」"}, 404)

        tier, season = data.get("tier") or None, data.get("season") or None
        try:
            job_id = jobs.submit(
                lambda log: actions.locate_skin(name, tier=tier, season=season,
                                                log=log),
                name=f"定位「{hit['name']}」")
        except jobs.Busy as e:
            return self._json({"ok": False,
                               "msg": f"{e}还没做完，等它结束再试"}, 409)
        except Exception as e:                   # noqa: BLE001
            return self._json({"ok": False,
                               "msg": f"{type(e).__name__}: {e}"}, 500)

        if data.get("sync"):
            return self._json(self._wait_job(job_id))
        return self._json({"ok": True, "job": job_id, "name": hit["name"],
                           "msg": f"已开始定位「{hit['name']}」"})

    @staticmethod
    def _wait_job(job_id, timeout=120.0):
        """同步等待一个后台动作跑完（只给 sync 模式用）。"""
        from . import jobs
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = jobs.status(job_id)
            if st is None:
                return {"ok": False, "msg": "任务不见了"}
            if st["state"] == "done":
                return {"ok": True, "job": job_id, **st["result"]}
            if st["state"] == "failed":
                return {"ok": False, "job": job_id, "msg": st["error"],
                        "log": st["log"]}
            time.sleep(0.1)
        return {"ok": False, "job": job_id, "msg": f"等太久了（>{timeout:.0f}s）"}

    def _set_switches(self, data):
        """保存开关。传 {"values": {key: value}}，只改传进来的那几项。

        写进 config.json 之后立刻生效：引擎每次 start 都重新 Config.load()，
        导航/服务端也每次都读。不需要重启程序。
        """
        values = data.get("values")
        if not isinstance(values, dict):
            return self._json({"ok": False, "msg": "缺少 values"}, 400)
        try:
            with Config.transaction() as cfg:
                applied = switches.apply_all(cfg.data, values)
                groups = switches.snapshot(cfg)
            return self._json({"ok": True, "applied": applied, "groups": groups})
        except Exception as e:                   # noqa: BLE001
            return self._json({"ok": False,
                               "msg": f"{type(e).__name__}: {e}"}, 500)

    def _follow(self, data):
        """把交易行里当前选中的这件挂单关注 / 取消关注。

        设备编排在 core/actions.py（和定位共用一套「抢前台 + 建设备」），
        这里只管状态闸和回包。
        """
        from . import actions
        want = bool(data.get("want", True))
        if _scheduler.status().get("running"):
            return self._json({"ok": False, "msg": "正在抢购，先停掉再动"}, 409)
        try:
            return self._json(actions.set_follow(want))
        except Exception as e:                   # noqa: BLE001
            return self._json({"ok": False,
                               "msg": f"{type(e).__name__}: {e}"}, 500)

    def _skin_target(self, data):
        """把某件砖标记成当前目标（抢购时优先去这一件）。"""
        from . import bricks
        name = (data.get("name") or "").strip()
        if name:
            hit = bricks.find_skin(name)
            if not hit:
                return self._json({"ok": False, "msg": f"目录里没有「{name}」"}, 404)
            target = {"name": hit["name"], "season": hit.get("season", ""),
                      "series": hit.get("series", ""),
                      "tier": hit.get("tier", ""),
                      "weapon": hit.get("weapon", "")}
        else:
            target = {}
        with Config.transaction() as cfg:
            cur = dict(cfg.get("console") or {})
            cur["target"] = target
            cfg["console"] = cur
        return self._json({"ok": True, "target": target})

    def _task_remove(self, data):
        """删掉一条任务。条件改了必须删旧的 —— 留着会按老条件再抢一遍。"""
        return self._json({"ok": True, "removed": _scheduler.remove(data.get("id"))})

    def _task_update(self, data):
        """改一条任务的条件。

        界面改条件时**改同一条**而不是删了重建：任务上的「已抢 N 件 /
        已花多少」是累计的，重建就归零了（限量 2 件的任务会因此多买一件）。
        """
        tid = data.get("id")
        if not tid:
            return self._json({"ok": False, "msg": "缺任务 id"}, 400)
        fields = {}
        for key in ("min_price", "max_price", "quota", "budget", "name",
                    "mode", "workflow", "craft", "wear", "season",
                    "max_wear", "fire_at", "sort", "enabled"):
            if key in data:
                fields[key] = data[key]
        for key in ("materials", "colors", "categories"):
            if key in data:
                fields[key] = _as_list(data[key])
        if not fields:
            return self._json({"ok": False, "msg": "没有要改的字段"}, 400)
        ok = _scheduler.update(tid, **fields)
        if not ok:
            return self._json({"ok": False, "msg": "没有这条任务"}, 404)
        return self._json({"ok": True, "task": next(
            (t for t in _scheduler.tasks() if t["id"] == tid), None),
            "tasks": _scheduler.tasks()})

    def _task_clear(self):
        _scheduler.clear_finished()
        return self._json({"ok": True, "tasks": _scheduler.tasks()})

    def _preflight(self, data):
        """启动前检查。慢（约 1 秒，要抓屏认页面），只在进第 3 步和手动重测时调。"""
        from . import actions
        mode = "detail" if (data.get("mode") == "detail") else "list"
        try:
            checks = actions.preflight_now(mode)
        except Exception as e:
            return self._json({"ok": False, "msg": f"检查失败：{type(e).__name__}: {e}",
                               "checks": []}, 500)
        return self._json({"ok": preflight.all_ok(checks), "checks": checks})

    def _sched_start(self, data):
        # 权限不够就别开跑：UIPI 会把我们发的鼠标键盘消息全拦掉且不报错，
        # 跑起来是「点了没反应」，最后还可能被记成「没抢到」。
        try:
            from .input import needs_elevation
            # 演练模式不点鼠标，权限低也能跑 —— 拦它没有意义，反而让人
            # 以为「连试一下都不行」。只有真点的时候才必须提权。
            if needs_elevation() and not data.get("dry_run", True):
                return self._json({"ok": False,
                                   "msg": "控制台权限比游戏低，点不动它 —— "
                                          "请以管理员身份重新运行 start.bat"})
        except Exception:
            pass
        mode = "list"
        try:
            tasks = [t for t in Config.load()["queue"] if t.get("enabled", True)]
            if tasks:
                mode = tasks[0].get("mode") or "list"
        except Exception:                        # noqa: BLE001
            pass
        ok_gate, why = _preflight_gate(mode)
        if not ok_gate:
            return self._json({"ok": False, "msg": f"启动前检查没过：{why}"})
        ok, msg = _scheduler.start(dry_run=data.get("dry_run", True))
        return self._json({"ok": ok, "msg": msg})

    def _sched_stop(self):
        ok, msg = _scheduler.stop()
        return self._json({"ok": ok, "msg": msg})

    # ---------- 校时 / 热键 ----------

    def _do_timesync(self, data):
        if not _sync_lock.acquire(blocking=False):
            return self._json({"ok": False, "error": "上一次校时还没完成"}, 429)
        try:
            cfg = Config.load()["time_sync"]
            raw = data.get("samples")
            try:
                samples = int(raw) if raw not in (None, "") else int(cfg["samples"])
            except (TypeError, ValueError):
                return self._json({"ok": False,
                                   "error": f"samples 要是整数，收到 {raw!r}"}, 400)
            res = timesync.sync(
                servers=data.get("servers") or cfg["servers"],
                samples=max(1, min(20, samples)),
            )
            return self._json(res)
        finally:
            _sync_lock.release()

    def _hotkey_status(self):
        cfg = Config.load()["hotkey"]

        def one(h, key_spec, key_en, label):
            st = (h.status() if h is not None
                  else {"spec": cfg.get(key_spec), "active": False, "msg": "未启动"})
            st["enabled"] = bool(cfg.get(key_en, True))
            st["label"] = label
            return st

        stop = one(_hotkey, "spec", "enabled", "停止")
        start = one(_hotkey_start, "start_spec", "start_enabled", "开抢")
        return {"stop": stop, "start": start}

    def _set_hotkey(self, data):
        with Config.transaction() as cfg:
            old = cfg["hotkey"]
            spec = (data.get("spec") or old.get("spec") or "F8").strip()
            enabled = bool(data.get("enabled", old.get("enabled", True)))
            start_spec = (data.get("start_spec") or old.get("start_spec") or "F7").strip()
            start_enabled = bool(data.get("start_enabled", old.get("start_enabled", True)))
            cfg["hotkey"] = {"enabled": enabled, "spec": spec,
                             "start_enabled": start_enabled, "start_spec": start_spec}
            hk = dict(cfg["hotkey"])
        res = dict(_start_hotkeys(hk))
        bad = [f"{label}：{msg}" for label, (ok, msg) in res.items() if not ok]
        return self._json({"ok": not bad, "msg": "；".join(bad) or "热键已就绪",
                           "status": self._hotkey_status()})


def _sync_on_boot():
    """启动时自动校时。

    配置里 `time_sync.enabled` 默认是 True，但以前**没有任何代码读它** ——
    于是每次开程序都是「未校准」，启动前检查拦下「时间校准」，用户必须
    手动点一次「时间校准」才能开始。更糟的是关掉再开又回到未校准。
    现在按配置在后台校一次；失败只记日志，不挡启动（用户还能手点）。
    """
    try:
        cfg = Config.load()
        ts = cfg.get("time_sync") or {}
        if not ts.get("enabled", True):
            return
        if not _sync_lock.acquire(blocking=False):
            return
        try:
            res = timesync.sync(servers=ts.get("servers"),
                                samples=int(ts.get("samples") or 4))
            if res.get("ok"):
                _scheduler.note(f"启动校时完成：{res.get('server')} "
                                f"{res.get('offset', 0) * 1000:+.0f}ms", "info")
            else:
                _scheduler.note(f"启动校时失败：{res.get('error') or '原因未知'}"
                                "（可以在设置页手动点「时间校准」）", "warn")
        finally:
            _sync_lock.release()
    except Exception as e:                       # noqa: BLE001
        _scheduler.note(f"启动校时出错：{type(e).__name__}: {e}", "warn")


def _preflight_gate(mode="list"):
    """开抢前的检查闸门。返回 (ok, 说明)。

    开关 env.preflight 关掉时直接放行 —— 有人就是不想被拦（比如界面改版
    导致某项检查误报）。但默认是开的：宁可开不了工，也别带着瞎的眼睛去抢。
    """
    cfg = Config.load()
    if not switches.get(cfg, "env.preflight"):
        return True, "启动前检查已关闭"
    checks = preflight.evaluate(preflight.collect(cfg, mode), mode)
    bad = [c["label"] for c in checks if not c["ok"] and not c.get("warn")]
    if bad:
        return False, f"{'、'.join(bad)} 没过"
    return True, ""


def _hotkey_toggle():
    """开抢热键：没在跑就开抢，在跑就停。

    热键跳过的是界面，不是检查 —— 启动前检查不过就不开，并把不过的那几项
    写进日志（界面上那五项，热键这条路一样要过）。
    """
    try:
        if _scheduler.status().get("running"):
            _scheduler.stop()
            return
        cfg = Config.load()
        tasks = [t for t in cfg["queue"] if t.get("enabled", True)]
        if not tasks:
            _scheduler.note("热键开抢：队列是空的（或全部停用）", "warn")
            return
        mode = tasks[0].get("mode") or "list"
        # collect 回来的是「事实」，判断要交给 evaluate —— 界面那条路
        # （_preflight）也是这么走的。之前这里把 facts 当检查列表迭代，
        # 取 c["label"] 直接抛 TypeError，F7 开抢从来没成功过。
        ok_gate, why = _preflight_gate(mode)
        if not ok_gate:
            _scheduler.note(f"热键开抢被拦下：{why}", "warn")
            return
        ok, msg = _scheduler.start(dry_run=False)
        _scheduler.note(f"热键开抢：{msg}", "info" if ok else "warn")
    except Exception as e:
        _scheduler.note(f"热键开抢出错：{type(e).__name__}: {e}", "error")


def _premarket_fire():
    """休市截前：把鼠标移到游戏购买键上按热键，替用户点这一下。

    为什么需要它：05:00-08:00 休市，市场里没有挂单可抢，只能靠「提前站到
    购买键上」—— 人守着点，手一抖就错过开市第一秒。热键替你点，位置由你
    自己摆（鼠标在哪就点哪），我们只负责按下。

    只点一下，不做任何连点 —— 这一下是「进入排队」，不是「抢」。
    """
    import ctypes
    import datetime

    cfg = Config.load()
    if not switches.get(cfg, "premarket.enabled"):
        _scheduler.note("截前热键：功能没开（设置里打开「休市截前」）", "warn")
        return
    now = datetime.datetime.now().time()
    if not (datetime.time(5, 0) <= now <= datetime.time(8, 0)):
        _scheduler.note(f"截前热键：只在 05:00-08:00 休市时段可用（现在 "
                        f"{now.strftime('%H:%M')}）", "warn")
        return
    try:
        u = ctypes.windll.user32
        u.mouse_event(0x0002, 0, 0, 0, 0)        # LEFTDOWN
        u.mouse_event(0x0004, 0, 0, 0, 0)        # LEFTUP
        _scheduler.note("截前热键：已在鼠标位置点了一下（进入排队）", "hit")
    except Exception as e:                       # noqa: BLE001
        _scheduler.note(f"截前热键点不动：{type(e).__name__}: {e}", "warn")


def _start_hotkeys(hk=None):
    """按配置（重）注册两个热键，返回 {标签: (ok, msg)}。

    两个热键是两条独立的 RegisterHotKey 消息循环，互不影响 —— 一个被别的
    程序占了，另一个照样能用。
    """
    global _hotkey, _hotkey_start, _hotkey_pre
    if hk is None:
        hk = Config.load()["hotkey"]
    for h in (_hotkey, _hotkey_start, _hotkey_pre):
        if h is not None:
            h.stop()
    _hotkey = _hotkey_start = _hotkey_pre = None

    out = {}
    if hk.get("enabled", True):
        _hotkey = Hotkey(hk.get("spec") or "F8", callback=lambda: _scheduler.stop())
        out["停止"] = _hotkey.start()
    else:
        out["停止"] = (True, "热键已关闭")

    if hk.get("start_enabled", True):
        _hotkey_start = Hotkey(hk.get("start_spec") or "F7", callback=_hotkey_toggle)
        out["开抢"] = _hotkey_start.start()
    else:
        out["开抢"] = (True, "开抢热键已关闭")

    pre = Config.load().get("premarket") or {}
    if pre.get("enabled"):
        _hotkey_pre = Hotkey(pre.get("hotkey") or "F10", callback=_premarket_fire)
        out["截前"] = _hotkey_pre.start()
    else:
        out["截前"] = (True, "截前热键已关闭")
    return out


def _seed_default_task():
    """队列空着就先放一条「所有皮肤」任务。

    顾客可能一次界面都没点过就直接按 F7 开抢 —— 热键那条路读的是队列，
    空队列就只能记一句「队列是空的」。条件取配置里的 active_task（价格
    区间），界面上的抢购条件卡之后会按顾客填的值重同步这条任务。
    """
    cfg = Config.load()
    if cfg["queue"]:
        return
    task = cfg.get("active_task") or {}
    _scheduler.add(
        "",
        mode="list",
        min_price=task.get("min_price") or 0,
        max_price=task.get("max_price") or 0,
        quota=task.get("quota") or 1,
        budget=task.get("budget") or 0,
    )


def _start_ocr_service():
    """拉起 OCR 服务管家（定时/变慢/挂掉自动重启，蓝绿切换）。

    失败不影响主流程：没有 .venv-vl、或者用户自己在外面临时起了服务，
    程序照常用 —— 只是没人帮它重启。
    """
    from . import ocr_service
    cfg = Config.load()
    s = (cfg.get("ocr") or {}).get("service") or {}
    svc = ocr_service.make(
        settings=s,
        is_busy=_scheduler.busy,
        log=lambda msg: _scheduler.note(f"OCR 服务：{msg}", "info"))
    if svc.start():
        # 管家在后台线程里起服务，这里不阻塞等待 —— 启动前检查会告诉你
        # 到底有没有就绪（那行会显示「服务已跑 X.Xh」）
        print("  OCR 服务管家：已启动（后台拉起/接管服务，失败会写进日志）")
    return svc


def serve(host="127.0.0.1", port=8777):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"  SJZ 控制台已启动 -> http://{host}:{port}")
    _seed_default_task()
    # 清掉已删任务留下的模板目录（data/<id>/ 只建不删，实测堆了 799 个）
    _scheduler.gc_task_dirs()
    _watcher.start()
    # OCR 服务的管家：定时/变慢/挂掉时自动蓝绿重启。没有 .venv-vl 就只是
    # 接管监控（用户自己起的服务照样管）。
    svc = _start_ocr_service()
    # 校时放后台：NTP 要采几个样本，几百毫秒到几秒，别挡着窗口出来
    threading.Thread(target=_sync_on_boot, name="boot-timesync",
                     daemon=True).start()
    # 启动时就把热键注册上：以前只在用户改设置时才注册，重启后热键是死的
    for label, (ok, msg) in _start_hotkeys().items():
        print(f"  热键 {label}：{msg}")
    print("  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        _watcher.stop()
        _scheduler.stop()
        svc.stop()
        httpd.server_close()
