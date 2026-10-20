"""本地 Web 服务。只用标准库，不额外引依赖。

端点：
  GET  /                      前端页面
  GET  /api/config            读配置
  POST /api/config            改配置（部分字段）
  GET  /api/screen            屏幕尺寸 + 显示器列表
  GET  /api/stream            实时预览（MJPEG）
  POST /api/region            {name, box} 保存框选区域（归一化）
  POST /api/template          {name, task_id?} 把该区域当前画面存成模板
  GET  /api/template/<name>   模板图片
  GET  /api/probe             当前各模板的相关度
  POST /api/scheduler/start   {dry_run}
  POST /api/scheduler/stop
  POST /api/tasks/add         {name, keywords, fire_at, note}
  POST /api/tasks/remove      {id}
  POST /api/tasks/update      {id, ...}
  POST /api/tasks/clear       清掉已完成
  POST /api/tasks/reset       全部回到 pending
  POST /api/tasks/add_many    {names, fire_at, interval_s} 批量加
  POST /api/tasks/capture     {id} 用当前画面给这条任务重截模板
  GET  /api/scheduler         调度器状态
  POST /api/read              读游戏内商品名/价格
  GET  /api/timesync
  POST /api/timesync          校时
  POST /api/latency           测速
  GET  /api/bricks            砖皮知识库（赛季 / 砖皮 / 稀有度 / 成色）
  POST /api/bricks/identify   {text} 把一段文字对回赛季砖皮
  POST /api/market/scan       {task_id?} 扫一次交易行列表，返回解析出的每一行
  GET  /api/hotkey            停止热键状态
  POST /api/hotkey            {spec, enabled} 换热键 / 开关
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2

from . import bricks, latency, market, timesync
from .config import Config
from .hotkey import Hotkey
from .scheduler import Scheduler
from .screen import Screen
from .vision import expand, match, to_gray

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web")

_scheduler = Scheduler()
_sync_lock = threading.Lock()
_latency_cache = {"at": 0, "data": None}
_hotkey = None


def _jpeg(img, quality=72, width=0):
    if width and img.shape[1] > width:
        scale = width / img.shape[1]
        img = cv2.resize(img, (width, int(img.shape[0] * scale)),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


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
        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path == "/api/config":
            return self._json(Config.load().public())
        if path == "/api/screen":
            return self._screen_info()
        if path == "/api/stream":
            return self._stream()
        if path == "/api/probe":
            return self._probe()
        if path == "/api/tasks":
            return self._json({"tasks": _scheduler.tasks()})
        if path == "/api/scheduler":
            return self._json(_scheduler.status())
        if path == "/api/timesync":
            return self._json(timesync.info())
        if path == "/api/latency":
            return self._json(_latency_cache["data"] or {"results": [], "best": None})
        if path == "/api/bricks":
            return self._json(bricks.knowledge())
        if path == "/api/hotkey":
            return self._json(self._hotkey_status())
        m = re.fullmatch(r"/api/template/(buy|confirm)", path)
        if m:
            return self._template_image(m.group(1), query.get("task_id", [None])[0])
        # web/ 下的普通文件（assets/ 里的图片等）。放在 API 之后，避免抢占路由。
        if path.startswith("/assets/"):
            return self._asset(path[len("/assets/"):])
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        routes = {
            "/api/config": lambda: self._save_config(data),
            "/api/region": lambda: self._save_region(data),
            "/api/template": lambda: self._capture_template(data),
            "/api/tasks/add": lambda: self._task_add(data),
            "/api/tasks/add_many": lambda: self._task_add_many(data),
            "/api/tasks/capture": lambda: self._task_capture(data),
            "/api/tasks/remove": lambda: self._task_remove(data),
            "/api/tasks/update": lambda: self._task_update(data),
            "/api/tasks/clear": lambda: self._task_clear(),
            "/api/tasks/reset": lambda: self._task_reset(),
            "/api/scheduler/start": lambda: self._sched_start(data),
            "/api/scheduler/stop": lambda: self._sched_stop(),
            "/api/read": lambda: self._read_product(),
            "/api/timesync": lambda: self._do_timesync(data),
            "/api/latency": lambda: self._do_latency(data),
            "/api/bricks/identify": lambda: self._identify(data),
            "/api/market/scan": lambda: self._market_scan(data),
            "/api/hotkey": lambda: self._set_hotkey(data),
        }
        fn = routes.get(path)
        if fn:
            return fn()
        return self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- 屏幕 / 预览 ----------

    def _screen_info(self):
        s = Screen(Config.load()["monitor"])
        try:
            return self._json({
                "size": list(s.size), "monitor": s.index,
                "monitors": [{"i": i, "w": m["width"], "h": m["height"]}
                             for i, m in enumerate(s.monitors) if i > 0],
            })
        finally:
            s.close()

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        s = Screen(Config.load()["monitor"])
        try:
            while True:
                buf = _jpeg(s.grab(), quality=70, width=1280)
                if not buf:
                    # 编码失败时不发空帧：浏览器收到 0 字节的 part 会把 img 判成 broken
                    time.sleep(0.1)
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(buf))
                self.wfile.write(buf)
                self.wfile.write(b"\r\n")
                # flush 必须留：MJPEG 是 while True、响应永不结束，而 wfile 带缓冲。
                # 实测（同一个 exe，只改这一行）：
                #   有 flush → 4 帧 / 327,680 字节
                #   无 flush → 0 字节，客户端直接超时
                # 客户端收不到字节时浏览器把 <img> 判成 broken，表现为预览区全黑。
                # 源码模式下单帧 93KB 会撞满 8KB 缓冲自动刷出，所以这个 bug 只在
                # 打包后的 exe 里暴露。
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            s.close()

    # ---------- 配置 / 框选 ----------

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

    def _save_config(self, data):
        cfg = Config.load()
        for key in ("threshold", "click_hz", "confirm_clicks", "confirm_interval_ms",
                    "fire_timeout_s", "failsafe", "trigger_mode", "monitor"):
            if key in data:
                cfg[key] = data[key]
        cfg.save()
        return self._json({"ok": True, "config": cfg.public()})

    def _save_region(self, data):
        name = data.get("name")
        box = data.get("box") or {}
        if name not in ("buy", "confirm", "name", "price", "clock", "list", "list_refresh"):
            return self._json({"ok": False, "msg": "未知区域"}, 400)
        try:
            x, y, w, h = (float(box["x"]), float(box["y"]),
                          float(box["w"]), float(box["h"]))
        except (KeyError, TypeError, ValueError):
            return self._json({"ok": False, "msg": "框选数据不完整"}, 400)
        if w < 0.003 or h < 0.003:
            return self._json({"ok": False, "msg": "框太小了，重画一个"}, 400)
        cfg = Config.load()
        cfg.set_region(name, {"x": round(x, 5), "y": round(y, 5),
                              "w": round(w, 5), "h": round(h, 5)})
        cfg.save()
        return self._json({"ok": True, "config": cfg.public()})

    def _capture_template(self, data):
        name = data.get("name")
        task_id = data.get("task_id")
        if name not in ("buy", "confirm"):
            return self._json({"ok": False, "msg": "模板只能是 buy 或 confirm"}, 400)
        cfg = Config.load()
        box = cfg.region(name)
        if not box:
            return self._json({"ok": False, "msg": "先框选区域再截模板"}, 400)
        s = Screen(cfg["monitor"])
        try:
            img = s.grab_norm(box)
        finally:
            s.close()
        if img.size == 0:
            return self._json({"ok": False, "msg": "截图失败"}, 500)
        cv2.imwrite(cfg.template_path(name, task_id), img)
        return self._json({"ok": True, "size": [img.shape[1], img.shape[0]],
                           "config": cfg.public()})

    def _template_image(self, name, task_id=None):
        cfg = Config.load()
        p = cfg.template_path(name, task_id) if task_id else cfg.template_path(name)
        if not os.path.isfile(p):
            return self._send(404, "no template", "text/plain; charset=utf-8")
        with open(p, "rb") as f:
            self._send(200, f.read(), "image/png")

    def _probe(self):
        cfg = Config.load()
        s = Screen(cfg["monitor"])
        out = {}
        try:
            for name in ("buy", "confirm"):
                box = cfg.region(name)
                if not box or not cfg.has_template(name):
                    out[name] = None
                    continue
                tpl = cv2.imread(cfg.template_path(name), cv2.IMREAD_COLOR)
                if tpl is None:
                    out[name] = None
                    continue
                roi = s.grab_norm(expand(box, 0.25))
                score, _ = match(to_gray(roi), to_gray(tpl))
                out[name] = round(score, 3)
        finally:
            s.close()
        return self._json(out)

    # ---------- 队列 ----------

    def _task_add(self, data):
        task, err = _scheduler.add(
            data.get("name", ""),
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
        )
        if err:
            return self._json({"ok": False, "msg": err}, 400)
        return self._json({"ok": True, "task": task, "tasks": _scheduler.tasks()})

    def _task_capture(self, data):
        """给指定任务重截模板（用当前屏幕上的画面）。

        用途：读商品名 → 绑定任务 → 现在界面上就是那个商品，此刻截模板
        最准。批量添加时也可以只截一次再复制。
        """
        tid = data.get("id") or ""
        tasks = _scheduler.tasks()
        if not any(t["id"] == tid for t in tasks):
            return self._json({"ok": False, "msg": "没有这条任务"}, 404)
        cfg = Config.load()
        s = Screen(cfg["monitor"])
        sizes = {}
        try:
            for name in ("buy", "confirm"):
                box = cfg.region(name)
                if not box:
                    return self._json({"ok": False, "msg": f"还没框选 {name} 区域"}, 400)
                img = s.grab_norm(box)
                if img.size == 0:
                    return self._json({"ok": False, "msg": "截图失败"}, 500)
                cv2.imwrite(cfg.template_path(name, tid), img)
                sizes[name] = [img.shape[1], img.shape[0]]
        finally:
            s.close()
        return self._json({"ok": True, "sizes": sizes, "tasks": _scheduler.tasks()})

    def _task_add_many(self, data):
        raw = data.get("names") or ""
        names = ([n for n in raw.splitlines()] if isinstance(raw, str) else list(raw))
        names = [n.strip() for n in names if n and n.strip()]
        if not names:
            return self._json({"ok": False, "msg": "一行一个商品名，先填几个"}, 400)
        made, errors = _scheduler.add_many(
            names,
            keywords=data.get("keywords") or [],
            fire_at=data.get("fire_at") or 0,
            interval_s=data.get("interval_s") or 0,
            template_from=data.get("template_from"),
            mode=data.get("mode") or "detail",
            min_price=data.get("min_price") or 0,
            max_price=data.get("max_price") or 0,
            max_wear=data.get("max_wear") or 0,
            craft=data.get("craft") or "",
            wear=data.get("wear") or "",
            season=data.get("season") or "",
            sort=data.get("sort") or "price",
        )
        return self._json({"ok": bool(made), "added": len(made), "errors": errors,
                           "msg": f"加了 {len(made)} 条" if made else "一条都没加成",
                           "tasks": _scheduler.tasks()})

    def _task_remove(self, data):
        ok, msg = _scheduler.remove(data.get("id", ""))
        return self._json({"ok": ok, "msg": msg, "tasks": _scheduler.tasks()})

    def _task_update(self, data):
        tid = data.pop("id", "")
        ok = _scheduler.update(tid, **data)
        return self._json({"ok": ok, "tasks": _scheduler.tasks()})

    def _task_clear(self):
        _scheduler.clear_finished()
        return self._json({"ok": True, "tasks": _scheduler.tasks()})

    def _task_reset(self):
        _scheduler.reset()
        return self._json({"ok": True, "tasks": _scheduler.tasks()})

    def _sched_start(self, data):
        ok, msg = _scheduler.start(dry_run=data.get("dry_run", True))
        return self._json({"ok": ok, "msg": msg})

    def _sched_stop(self):
        ok, msg = _scheduler.stop()
        return self._json({"ok": ok, "msg": msg})

    # ---------- 读取 / 校时 / 测速 ----------

    def _read_product(self):
        try:
            return self._json(_scheduler.read_product())
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _do_timesync(self, data):
        if not _sync_lock.acquire(blocking=False):
            return self._json({"ok": False, "error": "上一次校时还没完成"}, 429)
        try:
            cfg = Config.load()["time_sync"]
            res = timesync.sync(
                servers=data.get("servers") or cfg["servers"],
                samples=int(data.get("samples") or cfg["samples"]),
            )
            return self._json(res)
        finally:
            _sync_lock.release()

    def _do_latency(self, data):
        cfg = Config.load()["latency"]
        res = latency.run(
            targets=data.get("targets") or cfg["targets"],
            rounds=int(data.get("rounds") or cfg["rounds"]),
        )
        _latency_cache.update(at=time.time(), data=res)
        return self._json(res)

    def _identify(self, data):
        text = (data.get("text") or "").strip()
        if not text:
            return self._json({"ok": False, "reason": "没有文字"}, 400)
        return self._json(bricks.identify(text))

    # ---------- 列表扫描 / 热键 ----------

    def _market_scan(self, data):
        """扫一次列表区域，把每行解析结果返回给前端预览。

        这个接口是给「先看看 OCR 认不认得出来」用的：用户框完列表点一下，
        立刻能看到读出了几行、每行的价格/做工/成色认成什么。认错了就去
        调框选，而不是等到开抢才发现买错。
        """
        cfg = Config.load()
        box = cfg.region("list")
        if not box:
            return self._json({"ok": False, "msg": "还没框选交易行列表区域"}, 400)
        s = Screen(cfg["monitor"])
        try:
            img = s.grab_norm(box)
        finally:
            s.close()
        if img.size == 0:
            return self._json({"ok": False, "msg": "截图失败"}, 500)
        rows = market.scan(img)
        task = {}
        tid = data.get("task_id")
        if tid:
            task = next((t for t in _scheduler.tasks() if t["id"] == tid), {})
        if not task:
            task = dict(cfg.get("active_task") or {})
        out = []
        for r in rows:
            ok, why = market.check(r, task)
            out.append({"text": r["text"], "price": r["price"], "craft": r["craft"],
                        "wear": r["wear"], "wear_value": r["wear_value"],
                        "cy": r["cy"], "ok": ok, "why": why})
        return self._json({"ok": True, "rows": out, "size": [img.shape[1], img.shape[0]],
                           "hit": sum(1 for r in out if r["ok"])})

    def _hotkey_status(self):
        cfg = Config.load()["hotkey"]
        if _hotkey is None:
            return {"enabled": cfg["enabled"], "spec": cfg["spec"],
                    "active": False, "msg": "未启动"}
        st = _hotkey.status()
        st["enabled"] = cfg["enabled"]
        return st

    def _set_hotkey(self, data):
        global _hotkey
        cfg = Config.load()
        spec = (data.get("spec") or cfg["hotkey"]["spec"] or "F8").strip()
        enabled = bool(data.get("enabled", cfg["hotkey"]["enabled"]))
        cfg["hotkey"] = {"enabled": enabled, "spec": spec}
        cfg.save()
        ok, msg = _start_hotkey(spec, enabled)
        return self._json({"ok": ok, "msg": msg, "status": self._hotkey_status()})


def _start_hotkey(spec, enabled):
    """（重）注册停止热键。按 F8 等于点「停止」——抢购时手在键盘上，
    比鼠标甩左上角靠谱。"""
    global _hotkey
    if _hotkey:
        _hotkey.stop()
        _hotkey = None
    if not enabled:
        return True, "热键已关闭"
    _hotkey = Hotkey(spec, callback=lambda: _scheduler.stop())
    return _hotkey.start()


def serve(host="127.0.0.1", port=8777):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"  SJZ 控制台已启动 -> http://{host}:{port}")
    print("  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        _scheduler.stop()
        httpd.server_close()
