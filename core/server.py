"""本地 Web 服务。只用标准库，不额外引依赖。

端点：
  GET  /                      前端页面
  GET  /api/config            读配置
  POST /api/config            改配置（部分字段）
  POST /api/scheduler/start
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
from .config import (Config, DATA_DIR, mode_for_workflow, normalize_workflow,
                     task_workflow)
from .validate import check_conditions
from . import records
from . import switches
from .hotkey import HookHotkey, Hotkey
from .pagewatch import PageWatcher
from .scheduler import Scheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web")

_scheduler = Scheduler()
# 盯当前页面：用户随时要知道「现在停在哪个界面」。画面不变就不做 OCR，
# 所以几乎不花钱（见 core/pagewatch.py）。
_watcher = PageWatcher()
_sync_lock = threading.Lock()

# 最近一次 HTTP 请求的时刻。浏览器兜底模式（WebView2 起不来）没有窗口可关，
# 用户把标签一关，进程就没人管了 —— 它继续占着端口和单实例闸，下次双击
# 只会看到「已经有一个实例在跑」，而屏幕上什么都没有。desktop.py 那边靠
# 这个时间戳判断「页面还在不在」。
_last_hit = time.time()

_hotkey = None          # 停止热键
_hotkey_start = None    # 开抢热键
_hotkey_pre = None      # 休市截前热键

# 停止热键去抖：连按 F8 时钩子会连发好几次，每次都起一个 stop 线程去
# join 引擎 —— 线程越堆越多，还会在日志里刷一屏「已停止」（审查 S26）。
_stop_hotkey_at = 0.0
_STOP_HOTKEY_GAP = 0.3


def touch():
    """记一次请求。每个 HTTP 请求都会调它。"""
    global _last_hit
    _last_hit = time.time()


def idle_seconds():
    """多久没有页面连过来了。"""
    return time.time() - _last_hit


def busy():
    """现在是不是正在跑一轮抢购（看门狗别在抢购中途把进程干掉）。"""
    try:
        return _scheduler.busy()
    except Exception:                        # noqa: BLE001
        return False


def _as_bool(value):
    """"false" / 0 / "0" / "off" 都是 False —— 别让字符串 truthy 蒙混过去。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "no", "否")
    return None


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_trigger_mode(value):
    return value if value in ("vanish", "appear") else None


def _as_workflow(value):
    wf = normalize_workflow(value, "")
    return wf or None


def _as_input_mode(value):
    return value if value in ("interception", "legacy") else None


# POST /api/config 收哪些字段、各是什么类型。以前是 `cfg[key] = data[key]`
# 裸赋值：`"failsafe": "false"` 存成字符串（真值 → 紧急停用反而不生效）、
# `"monitor": "2"` 存成字符串（抓屏直接报参数错误）。现在统一过一遍类型闸，
# 脏值丢掉、保留原值（审查：`/api/config` 无类型校验）。
_CONFIG_COERCERS = {
    "threshold": _as_float,
    "click_hz": _as_float,
    "confirm_clicks": _as_int,
    "confirm_interval_ms": _as_int,
    "fire_timeout_s": _as_float,
    "failsafe": _as_bool,
    "trigger_mode": _as_trigger_mode,
    "workflow": _as_workflow,
    "monitor": _as_int,
    "save_shots": _as_bool,
    "arm_grace_s": _as_float,
    "list_max_tries": _as_int,
    "input_mode": _as_input_mode,
}


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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器/WebView 提前关连接是常态（刷新、关窗口、预加载取消）。
            # 不接住的话 socketserver 会打一段 "Exception occurred during
            # processing of request" 的 traceback，看着像程序崩了。
            pass

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
        touch()
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
            # tasks_status 额外带 config_ok / config_error：配置读不出来时
            # 界面要显示「读失败」，不能把快照当成「队列空了」（review R1）。
            status = getattr(_scheduler, "tasks_status", None)
            return self._json(status() if status else {"tasks": _scheduler.tasks()})
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
            try:
                p = skinfilter.plan(name)
            except FileNotFoundError:
                return self._json({"ok": False,
                                   "msg": "筛选表 core/skins_raw.csv 缺失，"
                                          "请从仓库恢复后重启"}, 503)
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
        touch()
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
            "/api/quit": lambda: self._quit(),
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
        except (BrokenPipeError, ConnectionResetError):
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
        # 数值先整单校验：错一项就整单拒绝，不能「错一项、其余照存」。
        # 前端也会拦，这里是独立兜底（不信任前端）。
        src = data.get("console") if isinstance(data.get("console"), dict) else None
        cond = None
        if src:
            cond, errs = check_conditions(src)
            if errs:
                return self._json({"ok": False,
                                   "msg": "条件不合法：" + "；".join(errs)}, 400)
        # 事务：读盘 → 改 → 落盘 在同一把锁里。以前是 load 出一份全量快照
        # 改完整份写回，调度器那边同时改任务状态时，两边后写的会把先写的
        # 整份盖掉（用户看到「设置改完几秒后自己变回去」）。
        with Config.transaction() as cfg:
            for key, coerce in _CONFIG_COERCERS.items():
                if key in data:
                    value = coerce(data[key])
                    # None = 这值不合法：丢掉，保留盘上原值，别把配置写脏
                    if value is not None:
                        cfg[key] = value
            if "workflow" in data:
                # mode 是 workflow 的派生视图，这里必须跟着走，
                # 否则存进去的就是「workflow=list、mode=detail」这种自相矛盾的组合
                cfg["mode"] = mode_for_workflow(cfg["workflow"])
            # 界面上的抢购条件也存 —— 关掉再打开要能接着干
            if isinstance(data.get("console"), dict):
                cur = dict(cfg.get("console") or {})
                for key in _CONSOLE_NUM:
                    if key in data["console"]:
                        # cond 已在上面校验过，这里只接受 clean 值
                        cur[key] = cond[key]
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
            # 不补默认值：路线由 scheduler.add 里的 task_workflow 推导，
            # 两者都没给时回落到全局 workflow（review R2）。
            mode=data.get("mode"),
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
        # 「重新排队」。界面上的「开始抢购」是改**同一条**任务再启动，
        # 而调度器只挑 pending/armed 的任务跑 —— 上一次跑完（done/failed）
        # 的状态不重置的话，start 会立刻打出「队列跑完了」，用户看到的就是
        # 「点了开始没反应」，只能删任务或重启程序才能再抢一次。
        # 只接受 pending：界面不能借这个口子把任务直接标成 done。
        if str(data.get("status") or "") == "pending":
            fields["status"] = "pending"
        if not fields:
            return self._json({"ok": False, "msg": "没有要改的字段"}, 400)
        num_fields = {k: fields[k] for k in
                      ("min_price", "max_price", "quota", "budget", "craft")
                      if k in fields}
        if num_fields:
            clean, errs = check_conditions(num_fields)
            if errs:
                return self._json({"ok": False,
                                   "msg": "条件不合法：" + "；".join(errs)}, 400)
            for k, v in clean.items():
                if k in fields:
                    fields[k] = v
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
        try:
            cfg = Config.load()
        except Exception as e:                   # noqa: BLE001
            return self._json({"ok": False, "msg": f"检查失败：{type(e).__name__}: {e}",
                               "checks": []}, 500)
        # 路线口径与 scheduler.add / 调度器完全一致（review R2）：
        # workflow 显式 > 老 mode 参数 > 全局默认 workflow。
        # 以前缺省写死 "list"，全局默认是 countdown，同一份配置两处结论不同。
        wf = _preflight_workflow(data, cfg)
        mode = mode_for_workflow(wf)
        try:
            # 直接调 preflight 是为了把 workflow 带进去：倒计时路线要看 clock
            # 区域，只看 mode 会把倒计时任务判成「一切正常」（审查 S7）。
            checks = preflight.evaluate(preflight.collect(cfg, mode), mode, wf)
        except Exception as e:
            return self._json({"ok": False, "msg": f"检查失败：{type(e).__name__}: {e}",
                               "checks": []}, 500)
        return self._json({"ok": preflight.all_ok(checks), "checks": checks})

    def _sched_start(self, data):
        # 只有实战一条路径：启动即实战，没有可绕过的旁路。

        # 权限不够就别开跑：UIPI 会把我们发的鼠标键盘消息全拦掉且不报错，
        # 跑起来是「点了没反应」，最后还可能被记成「没抢到」。
        try:
            from .input import needs_elevation
            if needs_elevation():
                return self._json({"ok": False,
                                   "msg": "控制台权限比游戏低，点不动它 —— "
                                          "请以管理员身份重新运行 start.bat"})
        except Exception:
            pass
        # mode/workflow 从**第一条要跑的任务**推导，和 dispatch 时一致。
        # 以前这里默认 "list"、scheduler.start 默认 "detail" —— 两个默认值
        # 相反，同一份配置在两处能得出不同结论（审查 S7）。
        mode, workflow = _next_task_route()
        ok_gate, why = _preflight_gate(mode, workflow)
        if not ok_gate:
            return self._json({"ok": False, "msg": f"启动前检查没过：{why}"})
        ok, msg = _scheduler.start()
        return self._json({"ok": ok, "msg": msg})

    def _sched_stop(self):
        ok, msg = _scheduler.stop()
        return self._json({"ok": ok, "msg": msg})

    def _quit(self):
        """让程序自己退出。

        无边框窗口没有系统标题栏。页面万一没加载出来（前端脚本挂了、WebView2
        抽风），用户就**没有任何办法**关掉它，只能去任务管理器 —— 而任务管理器
        杀管理员进程还得先以管理员身份打开。这个接口是最后的兜底：

            curl -X POST http://127.0.0.1:8777/api/quit

        只收本机请求：控制台默认绑 127.0.0.1，但万一有人改成 0.0.0.0 对外，
        局域网里谁都能把正在抢购的程序关掉。
        """
        host = (self.client_address[0] or "").split("%")[0]
        if host not in ("127.0.0.1", "::1", "localhost"):
            return self._json({"ok": False, "msg": "只接受本机请求"}, 403)

        def _bye():
            time.sleep(0.3)              # 先把响应发出去，别让 curl 报连接中断
            try:
                _scheduler.stop()        # 正在抢的话先停下来，别点一半
            except Exception:            # noqa: BLE001
                pass
            try:
                from .single_instance import release
                release()                # 放掉闸，下一个实例才能起来
            except Exception:            # noqa: BLE001
                pass
            os._exit(0)                  # 硬退：可能还有 daemon 线程卡在 IO 里

        threading.Thread(target=_bye, name="quit", daemon=True).start()
        return self._json({"ok": True, "msg": "正在退出"})

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


def _next_task_route():
    """下一条要跑的任务走哪条路线，返回 (mode, workflow)。

    和 scheduler 完全同一套推导（`config.task_workflow`），避免
    HTTP 这一层和调度层自己算自己的一套（审查 S7）。
    """
    try:
        from .scheduler import ARMED, PENDING
        cfg = Config.load()
        tasks = [t for t in cfg["queue"] if t.get("enabled", True)]
        # 只认「还会跑」的那条：`queue[0]` 可能是一条已经跑完的旧任务，
        # 拿它的路线判检查项，会和 scheduler.start 自己看到的第一条对不上
        # （审查 S7 补漏）。已经在跑的 ARMED/RUNNING 也算，scheduler 里
        # 只把 PENDING/ARMED 当「待跑」，这里保持一致。
        runnable = [t for t in tasks
                    if t.get("status") in (PENDING, ARMED)]
        pick = (runnable or tasks or [None])[0]
        default = cfg.get("workflow") or "countdown"
        workflow = (task_workflow(pick, default) if pick
                    else normalize_workflow(default))
    except Exception:                        # noqa: BLE001
        workflow = "countdown"
    return mode_for_workflow(workflow), workflow


def _preflight_workflow(data, cfg):
    """preflight 用哪条路线 —— 和 `Scheduler.add` 同一口径（review R2）。

    workflow 显式 > 老 mode 参数 > 全局默认 workflow。
    以前这里缺省写死 "list"，而全局默认是 countdown：同一份配置，
    界面加任务和启动前检查能得出两种结论。
    """
    data = data or {}
    return (normalize_workflow(data.get("workflow"), "")
            or task_workflow({"mode": data.get("mode")},
                             (cfg or {}).get("workflow") or "countdown"))


def _preflight_gate(mode="list", workflow=None):
    """开抢前的检查闸门。返回 (ok, 说明)。

    开关 env.preflight 关掉时直接放行 —— 有人就是不想被拦（比如界面改版
    导致某项检查误报）。但默认是开的：宁可开不了工，也别带着瞎的眼睛去抢。

    workflow 必须和调度器/引擎同一口径（`preflight.evaluate` 的参数），
    否则会出现「preflight 全绿 → 任务加上 → 一点开始就报缺模板」（P0-4）。
    """
    cfg = Config.load()
    if not switches.get(cfg, "env.preflight"):
        return True, "启动前检查已关闭"
    checks = preflight.evaluate(preflight.collect(cfg, mode), mode, workflow)
    bad = [c["label"] for c in checks if not c["ok"] and not c.get("warn")]
    if bad:
        return False, f"{'、'.join(bad)} 没过"
    return True, ""


def _hotkey_stop():
    """停止热键的回调。

    **必须立刻返回。** 以前直接 `_scheduler.stop()`：它要 join 引擎 2 秒、
    再 join 调度器 3 秒 —— 这 5 秒全卡在热键的消息循环里，用户连按几下
    都没反应，看起来就是「按了不停」。改成丢给后台线程，热键线程马上回去
    收消息；同时记一条「正在停止」，让界面上有个交代。

    连按去抖（300ms 内只受理一次）：钩子一次按键可能连发好几条，
    每条都起一个 stop 线程会在日志里刷屏、也白等 join（审查 S26）。
    """
    global _stop_hotkey_at
    try:
        now = time.time()
        if now - _stop_hotkey_at < _STOP_HOTKEY_GAP:
            return
        _stop_hotkey_at = now
        # 先留一条「收到了」，不然钩子没触发时完全看不出是没收到还是停不掉
        _scheduler.note("收到停止热键", "info")
        threading.Thread(target=_scheduler.stop, name="hotkey-stop",
                         daemon=True).start()
    except Exception as e:                    # noqa: BLE001
        _scheduler.note(f"热键停止失败：{type(e).__name__}: {e}", "error")


def _hotkey_toggle():
    """开抢热键：没在跑就开抢，在跑就停。

    热键跳过的是界面，不是检查 —— 启动前检查不过就不开，并把不过的那几项
    写进日志（界面上那五项，热键这条路一样要过）。
    """
    try:
        _scheduler.note("收到 F7（开抢/停止）", "info")
        st = _scheduler.status()
        # 线程活着 **或者** 引擎还在跑，都算「在跑」。只看线程会漏掉
        # 「任务已结束、引擎线程还在收尾」那一小段，那时候按热键会又开一次。
        if st.get("running") or (st.get("engine") or {}).get("running"):
            _hotkey_stop()
            return
        cfg = Config.load()
        tasks = [t for t in cfg["queue"] if t.get("enabled", True)]
        if not tasks:
            _scheduler.note("热键开抢：队列是空的（或全部停用）", "warn")
            return
        mode, workflow = _next_task_route()
        ok_gate, why = _preflight_gate(mode, workflow)
        if not ok_gate:
            _scheduler.note(f"热键开抢被拦下：{why}", "warn")
            return
        ok, msg = _scheduler.start()
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


def _make_hotkey(spec, callback):
    """建一个热键：**优先低级键盘钩子**，装不上才退回 RegisterHotKey。

    为什么换：RegisterHotKey 在游戏全屏独占键盘时经常收不到 —— 注册是
    成功的（日志「F7 已就绪」），按下去毫无反应。用户反复踩到这个坑。
    低级钩子在输入栈更底层，游戏吃键盘也照样能看见，而且只监听不拦截。
    """
    h = HookHotkey(spec, callback, log=lambda m: _scheduler.note(m, "info"))
    ok, msg = h.start()
    if ok:
        return h, ok, msg
    h2 = Hotkey(spec, callback)
    ok2, msg2 = h2.start()
    return h2, ok2, f"{msg}；改用 RegisterHotKey：{msg2}"


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
        _hotkey, ok, msg = _make_hotkey(hk.get("spec") or "F8", _hotkey_stop)
        out["停止"] = (ok, msg)
    else:
        out["停止"] = (True, "热键已关闭")

    if hk.get("start_enabled", True):
        _hotkey_start, ok, msg = _make_hotkey(hk.get("start_spec") or "F7",
                                              _hotkey_toggle)
        out["开抢"] = (ok, msg)
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
    """队列空着就先放一条任务。

    顾客可能一次界面都没点过就直接按 F7 开抢 —— 热键那条路读的是队列，
    空队列就只能记一句「队列是空的」。条件取配置里的 active_task（价格
    区间），界面上的抢购条件卡之后会按顾客填的值重同步这条任务。

    **名字要用界面上选的那件皮肤**（console.target.name），不能一律给空：
    空名 = 「抢所有挂单」，配上 max_price=0（不限价）就是「看到就买」。
    顾客明明选了 ASH-12，按一下 F7 却买了列表里第一件 —— 这是真金白银的事。
    只有他压根没选皮肤时，才是「抢所有」。
    """
    cfg = Config.load()
    if cfg["queue"]:
        return
    task = cfg.get("active_task") or {}
    target = ((cfg.get("console") or {}).get("target") or {}).get("name") or ""
    _scheduler.add(
        target,
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


class _Server(ThreadingHTTPServer):
    """控制台的 HTTP 服务。

    **必须关掉 allow_reuse_address。** Windows 上 SO_REUSEADDR 的语义是
    「可以绑一个别人正在监听的端口」（跟 Unix 的「复用 TIME_WAIT」完全不同），
    而 http.server 默认开着它 —— 于是第二个控制台能"成功"绑上同一个端口：
    两个实例都在 LISTENING，连接随机落到其中一个。表现出来就是「一个在抢，
    另一个界面上什么都不动」，而且 `_seed_default_task()` / `gc_task_dirs()`
    还会被两个实例各跑一遍（实测队列里凭空多出 4 条一模一样的任务）。
    关掉之后第二个实例当场报「地址被占用」，一眼就能看出是重复启动。
    """

    allow_reuse_address = False
    daemon_threads = True


def _startup_checks():
    """启动期资源自检：缺东西要现在就说，不能等到点开始才 500 / 静默空库。"""
    problems = []
    try:
        from . import bricks
        prob = bricks.load_problem()
        if prob:
            problems.append(prob + "（界面目录会是空的，不是没有皮肤）")
    except Exception as e:                       # noqa: BLE001
        problems.append(f"皮肤砖块表检查失败：{type(e).__name__}: {e}")

    raw = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skins_raw.csv")
    if not os.path.isfile(raw):
        problems.append(f"筛选表缺失：{raw}（/api/skins/plan 会 503）")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tpl = os.path.join(root, "templates")
    if not os.path.isdir(tpl):
        problems.append("templates/ 没入库也没采集过：购买/确认模板缺失，"
                        "请在设置页重新框选并截图（说明.txt「模板」一节）")

    venv_vl = os.path.join(root, ".venv-vl")
    if not os.path.isdir(venv_vl):
        problems.append("OCR 运行环境 .venv-vl 不在仓内：需要单独安装，"
                        "首次还要联网下载模型（见 README）")

    for p in problems:
        print(f"  !! {p}")
        _scheduler.note(p, "warn")
    if problems:
        print("  !! 以上缺失项不会阻止控制台启动，但启动闸门会逐项拦下并说明")
    return problems


def serve(host="127.0.0.1", port=8777):
    try:
        httpd = _Server((host, port), Handler)
    except OSError as e:
        print(f"  !! 端口 {port} 起不来（多半是另一个控制台还在跑）：{e}")
        raise
    httpd.daemon_threads = True
    print(f"  SJZ 控制台已启动 -> http://{host}:{port}")
    _startup_checks()
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
        # 也写进调度器日志：pythonw 没有控制台，print 出去就没了 ——
        # 用户按 F7 没反应时，界面上至少能看到「注册失败：被别的程序占了」
        _scheduler.note(f"热键 {label}：{msg}", "info" if ok else "warn")
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
