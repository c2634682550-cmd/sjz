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
import hmac
import json
import math
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
from . import regions as _regions
from . import switches
from . import click_burst
from . import license as _license
from .hotkey import HookHotkey, Hotkey
from .pagewatch import PageWatcher
from .scheduler import Scheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "web")

_scheduler = Scheduler()
# 盯当前页面：用户随时要知道「现在停在哪个界面」。画面不变就不做 OCR，
# 所以几乎不花钱（见 core/pagewatch.py）。
_watcher = PageWatcher()
# 手动「立即重校」与后台自动校时共用同一把锁：两路 NTP 同时采样本会互相
# 拉长 delay，算出更差的 offset。锁由 timesync 模块持有。
_sync_lock = timesync.sync_lock

# 最近一次 HTTP 请求的时刻。浏览器兜底模式（WebView2 起不来）没有窗口可关，
# 用户把标签一关，进程就没人管了 —— 它继续占着端口和单实例闸，下次双击
# 只会看到「已经有一个实例在跑」，而屏幕上什么都没有。desktop.py 那边靠
# 这个时间戳判断「页面还在不在」。
_last_hit = time.time()

_hotkey = None          # 停止热键
_hotkey_start = None    # 开抢热键
_hotkey_pre = None      # 休市截前热键

# 停止热键**实际生效**的键（配置里可能是 F8，被占后自动降级到 F7）。
_hotkey_effective = ""
# 降级/占用的一句人话，直接塞进状态和悬浮窗，别让用户去猜。
_hotkey_warn = ""
# 启动自检：同会话里同名进程的个数（含自己）。>1 时界面顶上要写出提示。
_instance_notice = ""

# ---------------- 控制面鉴权（launcher ⇄ 控制台） ----------------
# 启动器每次拉起 desktop.py 时随机生成一个 token，只通过命令行交给子进程；
# 进程内保存，不落盘、不进日志、不出现在任何响应体里。
# 只要 token 非空，本机回环也要验：拦掉浏览器里任意站点对 127.0.0.1 的
# 盲打（CSRF / DNS rebinding 的 Host 头也一起验）。
# token 为空 = 源码直跑 / 单元测试的旧行为，不启用鉴权（run.py 没被改造）。
_ctrl_token = None
_started_at = time.time()

# 不带 token 也要能访问的路径：页面本身（它负责把 token 种成 cookie）
_PUBLIC_PATHS = ("/", "/index.html")
# 只有 API 和成交截图需要令牌；其余路径都是 web/ 里的静态资源（页面、脚本、
# 图片），按前缀放行。悬浮窗以前卡在这里：/overlay.html 不在白名单 → 403，
# 只能降级到 file://，冻结包里 web/ 在 _internal 下，降级路径并不稳。
_GATED_PREFIXES = ("/api/", "/shots/")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def set_control_token(token):
    """启用/关闭控制面鉴权。传空串 = 关闭（仅供测试与源码直跑）。"""
    global _ctrl_token
    _ctrl_token = (token or "").strip() or None
    return _ctrl_token


def _host_is_loopback(host_header):
    """Host 头里的主机名必须是回环名（DNS rebinding 防御）。"""
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):                 # [::1]:8777
        name = host[1:host.find("]")] if "]" in host else host.strip("[]")
    elif ":" in host:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    return name in _LOOPBACK_HOSTS


def _shutdown_soon(reason="quit"):
    """统一收尾：先停调度器（别点一半）、放单实例闸，再退出进程。

    延后 0.3 秒是为了先把 HTTP 响应发出去，别让调用方看到连接重置。
    """
    def _bye():
        time.sleep(0.3)
        try:
            _scheduler.stop()
        except Exception:                # noqa: BLE001
            pass
        try:
            from .single_instance import release
            release()
        except Exception:                # noqa: BLE001
            pass
        os._exit(0)                      # 可能还有 daemon 线程卡在 IO 里

    threading.Thread(target=_bye, name=f"quit-{reason}", daemon=True).start()


# 停止热键去抖：连按 F8 时钩子会连发好几次，每次都起一个 stop 线程去
# join 引擎 —— 线程越堆越多，还会在日志里刷一屏「已停止」（审查 S26）。
_stop_hotkey_at = 0.0
_STOP_HOTKEY_GAP = 0.3


def touch():
    """记一次请求。每个 HTTP 请求都会调它。"""
    global _last_hit
    _last_hit = time.time()


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


def _overflow_reason(value):
    """数值类字段的溢出原因（N9/E7）：`10**400` / `1e400` 不许抛 500。

    返回 None 表示不是溢出问题（普通脏值仍按旧行为静默丢弃）；
    返回字符串 = 可读的 400 原因。
    """
    try:
        f = float(value)
    except OverflowError:
        return "数值超出可表示范围（太大）"
    except (TypeError, ValueError):
        return None                       # 非数字：保持原有静默丢弃语义
    if not math.isfinite(f):
        return "数值不是有限值（NaN/Inf）"
    return None


def _time_sync_block():
    """校时降级状态（只加不改）：给状态接口附带的 `time_sync` 对象。

    `timesync.info()` 已经给出 degraded/warning；这里补上界面需要的
    source_count 和以毫秒计的偏移，前端据此画顶部提示条（严格模式不显示）。
    """
    try:
        ts = timesync.info()
    except Exception:                     # noqa: BLE001
        ts = {}
    samples = [s for s in (ts.get("samples") or [])
               if isinstance(s, dict) and s.get("server") != "median"]
    srcs = {s.get("ip") or s.get("server") for s in samples
            if (s.get("ip") or s.get("server"))}
    try:
        off_ms = round(float(ts.get("offset") or 0.0) * 1000.0, 3)
    except (TypeError, ValueError, OverflowError):
        off_ms = 0.0
    if not math.isfinite(off_ms):
        off_ms = 0.0
    return {
        "degraded": bool(ts.get("degraded")),
        "source_count": len(srcs),
        "offset_ms": off_ms,
        "warning": str(ts.get("warning") or ""),
    }


def _as_int(value):
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return int(f)                        # 有限浮点转 int 不会抛异常


def _as_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _as_trigger_mode(value):
    return value if value in ("vanish", "appear") else None


def _as_workflow(value):
    wf = normalize_workflow(value, "")
    return wf or None


_INPUT_MODES = ("interception", "legacy", "message")
_INPUT_FALLBACKS = ("legacy", "message", "fail")


def _as_input_mode(value):
    """三个通道都收（review F5：以前漏了 message，前端存不进去）。"""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _INPUT_MODES:
            return v
    return None


def _as_input_fallback(value):
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _INPUT_FALLBACKS:
            return v
    return None


def _as_strict_bool(value):
    """只认明确的真/假：`"maybe"` 不能再被 truthy 蒙成 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "on", "yes", "是"):
            return True
        if v in ("0", "false", "off", "no", "否", ""):
            return False
    return None


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
    "input_fallback": _as_input_fallback,
    "input_message_strict": _as_strict_bool,
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
    # 浏览器/UI 拿到响应后可能不关连接（切后台、崩溃、休眠）。没有读超时
    # 时这个 keep-alive 连接会永久占住 1 个线程 + 7 个句柄（R02-L02），
    # 8 小时挂机下随标签页重启线性累积。30s 无新请求即由基类关闭。
    timeout = 30

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
        for k, v in getattr(self, "_extra_headers", ()):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器/WebView 提前关连接是常态（刷新、关窗口、预加载取消）。
            # 不接住的话 socketserver 会打一段 "Exception occurred during
            # processing of request" 的 traceback，看着像程序崩了。
            pass

    def _json(self, obj, code=200):
        try:
            # allow_nan=False：Infinity/NaN 不是合法 JSON，前端 JSON.parse
            # 直接失败。序列化异常也在这里收口，绝不把请求打成 500。
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, OverflowError) as e:
            body = json.dumps({"ok": False,
                               "msg": f"返回值无法序列化：{type(e).__name__}: {e}"},
                              ensure_ascii=False)
            code = 400
        self._send(code, body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # ---------- 控制面鉴权 ----------

    def _req_token(self):
        """请求携带的 token：优先 X-SJZ-Token 头，其次 sjz_ctrl cookie。

        页面加载时由服务端种 cookie，前端不用改一行代码就能带上；
        启动器用请求头（它没有浏览器 cookie 罐）。
        """
        tok = (self.headers.get("X-SJZ-Token") or "").strip()
        if tok:
            return tok
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sjz_ctrl" and v:
                return v.strip()
        return ""

    def _auth_gate(self, path):
        """True = 放行。token 未启用时直接放行（源码直跑 / 老测试）。

        失败统一 403，不回显任何 token 片段，也不区分「没带」和「带错」——
        少给爆破方一点信息。
        """
        if _ctrl_token is None:
            return True
        if not _host_is_loopback(self.headers.get("Host")):
            self.close_connection = True          # 别让客户端半路断开时刷 traceback
            self._json({"ok": False, "msg": "拒绝：Host 不是本机回环地址"}, 403)
            return False
        got = self._req_token()
        if got and hmac.compare_digest(got, _ctrl_token):
            return True
        self.close_connection = True
        self._json({"ok": False, "msg": "拒绝：缺少或错误的控制令牌"}, 403)
        return False

    # ---------- 路由 ----------

    def do_GET(self):
        touch()
        self._extra_headers = []
        u = urlparse(self.path)
        path, query = u.path, parse_qs(u.query)
        return self._guard(lambda: self._get(path, query), path)

    def _get(self, path, query):
        if path in _PUBLIC_PATHS:
            if _ctrl_token is not None:
                # 页面本身不设防（否则没法把 token 种进浏览器），但它负责种 cookie；
                # 之后的 /api/* 和静态资源都靠这个 cookie 过闸。
                self._extra_headers = [(
                    "Set-Cookie",
                    f"sjz_ctrl={_ctrl_token}; Path=/; SameSite=Strict; HttpOnly")]
            return self._static("index.html", "text/html; charset=utf-8")
        # 静态资源按前缀放行（页面/脚本/图片本身没有秘密，不带令牌也能取）；
        # 只有 /api/*、/shots/* 走下面的令牌闸。
        if not path.startswith(_GATED_PREFIXES):
            if path.startswith("/assets/"):
                return self._asset(path[len("/assets/"):])
            return self._static_file(path.lstrip("/"))
        if not self._auth_gate(path):
            return None
        if path == "/api/ctrl/status":
            return self._json(self._ctrl_status())
        if path == "/api/config":
            return self._json(Config.load().public())
        if path == "/api/regions":
            # 区域框选：当前值 + 来源（默认/用户）+ 实际像素矩形
            return self._regions_get()
        if path == "/api/screen/capture":
            # 给框选页用的当前画面（PNG）。抓的是引擎同一块屏，
            # 看到的就是程序会看到的那一帧。
            return self._screen_capture()
        if path == "/api/license":
            return self._json(_license.summary())
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
            return self._json(_scheduler.tasks_status())
        if path == "/api/scheduler":
            st = _scheduler.status()
            st["page"] = _watcher.snapshot()
            # 只加字段：界面用它对降级校时给一句低调提示
            st["time_sync"] = _time_sync_block()
            return self._json(st)
        if path == "/api/guard":
            # 场景守卫状态：当前级别、一句人话、唯一主按钮、现在在做什么。
            # 前端按这个画「需要你处理」卡片；不在这里做任何自动恢复。
            snap = _scheduler.guard_status()
            return self._json({"ok": True, **snap})
        if path == "/api/timesync":
            return self._json(timesync.info())
        if path == "/api/hotkey":
            return self._json(self._hotkey_status())
        if path == "/api/instances":
            # 启动自检 + 设置页「结束其它实例」的数据源：当前同会话里还有
            # 谁在用同名 exe、每个热键被谁占着。
            return self._json({"ok": True, **instances_info()})
        if path == "/api/records":
            raw = (query.get("limit", ["200"])[0] or "200")
            try:
                limit = max(1, min(5000, int(raw)))
            except (TypeError, ValueError):
                return self._json({"ok": False,
                                   "msg": f"limit 要是整数，收到 {raw!r}"}, 400)
            snap = records.snapshot(limit=limit)
            return self._json({"ok": True, "rows": snap["rows"],
                               "stats": snap["stats"],
                               # 预热没完成时是占位（rows 空、stats 全 0）；
                               # 正常启动顺序下 `serve()` 已经同步预热过了。
                               "warming": snap["warming"]})
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
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        touch()
        self._extra_headers = []
        path = urlparse(self.path).path
        if not self._auth_gate(path):
            return None
        data = self._body()
        routes = {
            "/api/config": lambda: self._save_config(data),
            "/api/tasks/add": lambda: self._task_add(data),
            "/api/tasks/clear": lambda: self._task_clear(),
            "/api/tasks/remove": lambda: self._task_remove(data),
            "/api/tasks/update": lambda: self._task_update(data),
            "/api/scheduler/start": lambda: self._sched_start(data),
            "/api/scheduler/stop": lambda: self._sched_stop(),
            "/api/guard/resume": lambda: self._json(
                {"ok": True, "guard": _scheduler.guard_resume()}),
            "/api/guard/stop": lambda: self._json(
                {"ok": True, "guard": _scheduler.guard_stop()}),
            "/api/timesync": lambda: self._do_timesync(data),
            "/api/hotkey": lambda: self._set_hotkey(data),
            "/api/preflight": lambda: self._preflight(data),
            "/api/records/clear": lambda: self._json(
                {"ok": records.clear(), "stats": records.stats()}),
            "/api/skins/locate": lambda: self._skin_locate(data),
            "/api/follow": lambda: self._follow(data),
            "/api/switches": lambda: self._set_switches(data),
            "/api/skins/target": lambda: self._skin_target(data),
            "/api/calibrate": lambda: self._auto_calibrate(data),
            "/api/quit": lambda: self._quit(),
            # 设置页「结束其它实例」：只清同会话 + 同 exe + 无窗口的残留，
            # 正在用的那个不动。前端那条线接不接都行，日志里也会提示。
            "/api/instances/cleanup": lambda: self._json(
                {"ok": True, **cleanup_instances()}),
            # 控制面：启动器用它查状态 / 要求停机；不受许可门禁影响
            # （许可失效时也得能把它关掉，否则用户只能去任务管理器）。
            "/api/ctrl/stop": lambda: self._ctrl_stop(),
            # 许可：这三个是「激活/续期」入口本身，不能反过来被许可门禁拦掉
            "/api/license/activate": lambda: self._license_activate(data),
            "/api/license/refresh": lambda: self._license_refresh(data),
            "/api/license/revocations": lambda: self._license_pull(data),
        }
        fn = routes.get(path)
        if fn:
            if (not path.startswith("/api/license/")
                    and not path.startswith("/api/ctrl/")
                    and not _license.server_gate_ok()):
                return self._json({"ok": False,
                                   "msg": "未授权：本机许可无效，请先激活"}, 403)
            return self._guard(fn, path)
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_PUT(self):
        """区域框选保存。契约是 PUT /api/regions（写操作，语义上不是 POST）。"""
        touch()
        self._extra_headers = []
        path = urlparse(self.path).path
        if not self._auth_gate(path):
            return None
        data = self._body()
        if path == "/api/regions":
            if not _license.server_gate_ok():
                return self._json({"ok": False,
                                   "msg": "未授权：本机许可无效，请先激活"}, 403)
            return self._guard(lambda: self._regions_save(data), path)
        return self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- 控制面（launcher 专用） ----------

    def _ctrl_status(self):
        """给启动器看的运行快照：只放状态，不放任何许可材料。"""
        try:
            scr = _scheduler.status()
        except Exception:                 # noqa: BLE001
            scr = {}
        try:
            lic = _license.summary()
        except Exception:                 # noqa: BLE001
            lic = {}
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - _started_at, 1),
            "auth": _ctrl_token is not None,
            "license": {k: lic.get(k) for k in
                        ("ok", "status", "reason", "lic_id", "tier",
                         "remaining_s") if k in lic},
            "scheduler": {k: scr.get(k) for k in
                          ("running", "busy", "msg") if k in scr},
            "time_sync": _time_sync_block(),
            "time_sync_auto": timesync.auto_status(),
        }

    def _ctrl_stop(self):
        """启动器要求控制台停机：走和 /api/quit 一样的收尾路径。"""
        _shutdown_soon("launcher-stop")
        return self._json({"ok": True, "msg": "正在停止"})

    # ---------- 许可 ----------

    def _license_call(self, data, fields, missing_msg, fn):
        """三个许可入口共用：校验必填 → 调库 → 统一的 400 / 成功回包形状。"""
        vals = [str(data.get(k) or "").strip() for k in fields]
        if not all(vals):
            return self._json({"ok": False, "msg": missing_msg}, 400)
        try:
            d = fn(*vals)
        except _license.LicenseError as e:
            return self._json({"ok": False, "msg": str(e)}, 400)
        return self._json({"ok": d.ok, "license": d.as_dict()})

    def _license_activate(self, data):
        return self._license_call(data, ("server", "key"),
                                  "缺少 server 或 key", _license.activate)

    def _license_refresh(self, data):
        return self._license_call(data, ("server",), "缺少 server",
                                  _license.refresh)

    def _license_pull(self, data):
        return self._license_call(data, ("server",), "缺少 server",
                                  _license.pull_revocations)

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

    def _static_file(self, rel):
        """web/ 根目录下的静态文件（悬浮窗的 html/js、样式等）。

        后缀白名单 + 目录穿越防护：路径来自 HTTP，不能让它读到 web/ 之外。
        `/assets/` 有独立的图片白名单（`_asset`），这里不重复覆盖。
        """
        ext = os.path.splitext(rel)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".png": "image/png",
                 ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg",
                 ".webp": "image/webp",
                 ".ico": "image/x-icon"}.get(ext)
        if not ctype:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        base = os.path.normpath(STATIC)
        p = os.path.normpath(os.path.join(base, rel))
        if not p.startswith(base + os.sep) or not os.path.isfile(p):
            return self._send(404, "not found", "text/plain; charset=utf-8")
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
        # review F5：输入通道三件套非法时给出可读原因，和数值项一样整单拒绝。
        # 以前是 coerce 返回 None → 静默丢弃，前端以为存上了。
        bad = []
        if "input_mode" in data and _as_input_mode(data["input_mode"]) is None:
            bad.append(f"input_mode={data['input_mode']!r} 不合法，"
                       f"可选：{' / '.join(_INPUT_MODES)}")
        if "input_fallback" in data and \
                _as_input_fallback(data["input_fallback"]) is None:
            bad.append(f"input_fallback={data['input_fallback']!r} 不合法，"
                       f"可选：{' / '.join(_INPUT_FALLBACKS)}")
        if "input_message_strict" in data and \
                _as_strict_bool(data["input_message_strict"]) is None:
            bad.append("input_message_strict 只接受 true/false"
                       "（也认 1/0、on/off、yes/no、是/否）")
        # N9/E7：`10**400`、`1e400` 这类溢出值以前会让 _as_int/_as_float 抛
        # 未捕获 OverflowError → 500。这里前置成可读 400，不许 500。
        for key in ("threshold", "click_hz", "confirm_clicks",
                    "confirm_interval_ms", "fire_timeout_s", "monitor",
                    "arm_grace_s", "list_max_tries"):
            if key in data:
                why = _overflow_reason(data[key])
                if why:
                    shown = repr(data[key])
                    if len(shown) > 40:
                        shown = shown[:37] + "..."
                    bad.append(f"{key}={shown} 不合法：{why}")
        if bad:
            return self._json({"ok": False,
                               "msg": "配置值不合法：" + "；".join(bad)}, 400)
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

    # ---------- 区域框选 ----------

    def _region_size(self, cfg):
        """返回 `(size, meta, error)`；拿不到屏幕时退到配置里的基准尺寸并标注。"""
        try:
            sc = _regions.open_screen(cfg)
            try:
                return tuple(sc.size), _regions.screen_meta(sc), None
            finally:
                sc.close()
        except Exception as e:                        # 无显示/热键占用/权限...
            return (_regions.base_size(cfg), {"fallback": True},
                    f"{type(e).__name__}: {e}")

    def _regions_get(self):
        cfg = Config.load()
        size, meta, err = self._region_size(cfg)
        return self._json(_regions.describe(
            cfg, size, meta, err, calibration=cfg.get("calibration")))

    def _auto_calibrate(self, data):
        """一键自动标定：默认抓当前客户区；也接受 base64 截图（离线复验）。

        返回体同时带 `result`（自检细节）和完整的区域描述，前端不用再
        补一次 GET。失败（fallback）也是 200 —— 这是「跑完了但没通过」，
        不是请求错误；按钮要能显示具体哪几个锚点没对上。
        """
        from . import autocalib
        cfg = Config.load()
        raw = data.get("image") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("apply_proposal"):
            ans = autocalib.apply_proposal(cfg)
            cfg = Config.load()
            size, meta, err = self._region_size(cfg)
            out = _regions.describe(cfg, size, meta, err,
                                    calibration=cfg.get("calibration"))
            out.update(ok=ans["ok"], msg=ans["msg"], apply=ans)
            if ans.get("skipped"):
                names = "、".join(_regions.LABELS.get(n, n)
                                 for n in ans["skipped"])
                out["hint"] = f"这些区域你手动框过，保持原样：{names}"
            return self._json(out)
        if raw:
            import base64
            import cv2
            import numpy as np
            b64 = raw.split(",", 1)[1] if "," in str(raw) else str(raw)
            try:
                buf = np.frombuffer(base64.b64decode(b64), np.uint8)
                img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            except Exception as e:
                return self._json({"ok": False, "msg": f"截图解码失败：{e}"}, 400)
            if img is None:
                return self._json({"ok": False, "msg": "截图解码失败：不是有效图片"}, 400)
            res = autocalib.calibrate_image(
                img, size=(img.shape[1], img.shape[0]), cfg=cfg,
                workflow=(cfg.get("run") or {}).get("workflow")
                if isinstance(cfg.get("run"), dict) else None)
            # 默认跟随 apply_auto（默认 True）：坐标落盘，闸门校验的就是
            # 引擎会点的那套值；手框过的区域不会被覆盖（见 record）。
            autocalib.record(cfg, res, save=True,
                             apply=data.get("apply"))
        else:
            res = autocalib.run_for_engine(cfg)

        cfg = Config.load()
        size, meta, err = self._region_size(cfg)
        out = _regions.describe(cfg, size, meta, err,
                                calibration=cfg.get("calibration"))
        out["result"] = res
        # 标定完顺手把模板也对齐当前画面：坐标和素材必须同步更新，否则
        # 「坐标标好了、模板还是旧尺寸」会让启动前检查继续亮红，用户又得
        # 手动跑 `--calibrate`。自愈失败不影响标定结果，只是多一条说明。
        try:
            from . import template_heal
            heal = template_heal.heal(
                cfg, workflow=(cfg.get("run") or {}).get("workflow")
                if isinstance(cfg.get("run"), dict) else None,
                reason="auto-calibrate")
            out["template_heal"] = heal
            if heal.get("status") in ("rescaled", "recaptured", "ok", "fit"):
                out["template_msg"] = heal.get("message")
        except Exception as e:                   # noqa: BLE001
            out["template_heal"] = {"status": "failed",
                                    "message": f"{type(e).__name__}: {e}"}
        out["msg"] = res.get("message") or "自动标定完成"
        block = res.get("gate") or (cfg.get("calibration") or {}).get("block")
        if block:
            out["ok"] = False
            out["hint"] = ("本次不会启动。打开「高级设置 → 区域微调」拖一次框"
                           "（手框后即可启动），或把游戏窗口完整移回屏幕内、"
                           "别遮挡购买/确认按钮。")
        return self._json(out)

    def _buy_region_check(self, box):
        """手框 buy 保存前校验：非空 + 与背景有明显对比，并记录框内 OCR 文本。

        按钮上不一定有「购买」二字（实测挂单页按钮只显示价格），所以不能用
        「框内有没有购买文字」当门槛；但必须排除「框到同色空白/纯背景」。
        """
        try:
            cfg0 = Config.load()
            sc = _regions.open_screen(cfg0)
            try:
                img = sc.grab()
            finally:
                try:
                    sc.close()
                except Exception:            # noqa: BLE001
                    pass
        except Exception:                    # noqa: BLE001
            return None
        try:
            from . import autocalib as _autocalib
            rect = _regions.px_box(box, (img.shape[1], img.shape[0]))
            return _autocalib.check_buy_region(img, rect)
        except Exception:                    # noqa: BLE001
            return None

    def _regions_save(self, data):
        # 校验先行：任何一项不合法都整单拒绝，磁盘一个字节都不动。
        clean, errors = _regions.validate_update(data)
        if errors:
            return self._json({"ok": False,
                               "msg": "区域不合法：" + "；".join(errors),
                               "errors": errors}, 400)
        cfg0 = Config.load()
        size, meta, err = self._region_size(cfg0)
        buy_chk = None
        if clean.get("buy"):
            prev = None
            try:
                prev = cfg0.region_raw("buy")
            except Exception:                # noqa: BLE001
                prev = None
            if prev != clean["buy"]:
                buy_chk = self._buy_region_check(clean["buy"])
                if buy_chk and not buy_chk.get("ok"):
                    msg = ("购买按钮区域未保存：%s。请重新框住按钮本体"
                           "（有底色/描边的长条），不要框空白或列表卡片。"
                           % buy_chk.get("why"))
                    return self._json({"ok": False, "msg": msg,
                                       "errors": [msg], "check": buy_chk}, 400)
        with Config.transaction() as cfg:
            for name, box in clean.items():
                cfg.set_region(name, box)   # None = 删掉用户设定，回默认
            if buy_chk:
                cal = cfg.get("calibration")
                if not isinstance(cal, dict):
                    cal = {}
                    cfg.data["calibration"] = cal
                cal["buy_region_check"] = buy_chk
                cfg._dirty.add("calibration")
            out = _regions.describe(cfg, size, meta, err)
        out["ok"] = True
        out["saved"] = sorted(clean)
        out["msg"] = ("已保存 %d 个区域，引擎下次读配置即生效"
                      % len(clean))
        if buy_chk:
            out["buy_check"] = buy_chk
            out["msg"] += "；按钮区校验：" + str(buy_chk.get("why"))
        return self._json(out)

    def _screen_capture(self):
        cfg = Config.load()
        try:
            sc = _regions.open_screen(cfg)
            try:
                img = sc.grab()
            finally:
                sc.close()
        except Exception as e:
            return self._json({"ok": False,
                               "msg": f"抓不到画面：{type(e).__name__}: {e}"}, 503)
        try:
            import cv2
            ok, buf = cv2.imencode(".png", img)
            if not ok:
                raise RuntimeError("PNG 编码失败")
        except Exception as e:
            return self._json({"ok": False, "msg": f"画面编码失败：{e}"}, 500)
        self._send(200, buf.tobytes(), "image/png")

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

        数据源优先用 `skins_lib.json`（2026-11-08 的 44 池两遍枚举，
        232 条，品阶来自筛选上下文）；没有这份文件时退回 bricks.json
        的 243 条老表。老表**不删**，`bricks.catalog()` 定位流程照旧用它。
        """
        from . import bricks
        lib = bricks.load_skin_lib()
        if lib:
            out, by_season = [], {}
            for it in lib["items"]:
                if it["season"] not in by_season:
                    by_season[it["season"]] = {
                        "season": it["season"], "season_name": it["season_name"],
                        "series": it["season_name"], "tiers": {}}
                    out.append(by_season[it["season"]])
                by_season[it["season"]]["tiers"].setdefault(it["tier"], []).append({
                    "name": it["name"], "weapon": it["weapon"],
                    "gun": it["gun"], "skin": it["skin"],
                    "category": it.get("category", "枪皮"),
                    "source": it.get("source", "filter_context"),
                    "enumerated_at": it.get("enumerated_at", ""),
                })
            return self._json({"ok": True, "seasons": out, "total": lib["total"],
                               "updated": lib.get("updated", "")})
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

        tier, season = data.get("tier") or None, data.get("season") or None
        hit = bricks.find_skin(name, tier=tier, season=season)
        if not hit:
            return self._json({"ok": False, "msg": f"目录里没有「{name}」"}, 404)

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
                # 契约 5.1：互斥配置**可保存**，但运行时只按连点执行。
                # 这里把原因随保存回包一起给界面 —— 不能只留在运行日志里。
                warnings = []
                if (switches.get(cfg, "click.burst.mode") == "burst"
                        and switches.get(cfg, "buy.followup.enabled")):
                    warnings.append(click_burst.MUTEX_WARNING)
            return self._json({"ok": True, "applied": applied,
                               "groups": groups, "warnings": warnings})
        except switches.SwitchValueError as e:
            # 契约：越界/倒挂一律拒收，并把**可读原因**原样回给界面。
            # 整批拒收时磁盘一个字节都没动（Config.transaction 里抛了就回滚）。
            return self._json({"ok": False, "msg": str(e),
                               "key": getattr(e, "key", ""),
                               "reason": getattr(e, "reason", str(e))}, 400)
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
                      ("min_price", "max_price", "quota", "budget",
                       "max_wear", "craft")
                      if k in fields}
        if num_fields:
            clean, errs = check_conditions(num_fields)
            if errs:
                return self._json({"ok": False,
                                   "msg": "条件不合法：" + "；".join(errs)}, 400)
            # 只回写**真的传了值**的键：check_conditions 对缺省键会补默认值
            # （max_wear 的默认是 0.0 = 不限制磨损，显式传 null/空串也是这个
            # 语义），那不是「用户要改」。以前 max_wear 不在校验集合里、判据
            # 又是 `k in fields`，调用方显式传 0.4 时 clean 里的默认 0.0 照样
            # 被写回 —— 界面显示有上限、引擎实际不限磨损。
            for k, v in clean.items():
                if k in num_fields:
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
            checks = preflight.evaluate(preflight.collect(cfg, mode, wf), mode, wf)
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

        _shutdown_soon("api-quit")
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
            # 手动「立即重校」现在是可选补跑入口：结果回灌给后台调度，
            # 成功就清零自动重试的失败计数，不必等下一轮。
            timesync.auto_note_result(res)
            return self._json(res)
        finally:
            _sync_lock.release()

    def _hotkey_status(self):
        cfg = Config.load()["hotkey"]

        def one(h, spec, key_en, label):
            st = (h.status() if h is not None
                  else {"spec": spec, "active": False, "msg": "未启动"})
            st["enabled"] = bool(cfg.get(key_en, True))
            st["label"] = label
            return st

        configured = cfg.get("stop") or cfg.get("spec") or "F8"
        stop_spec = _hotkey_effective or (
            _hotkey.status().get("spec") if _hotkey is not None else configured)
        stop = one(_hotkey, stop_spec, "enabled", "停止")
        stop["configured"] = configured
        stop["effective"] = _hotkey_effective or ""
        stop["note"] = _hotkey_warn
        start = one(_hotkey_start, cfg.get("start_spec") or "F7",
                    "start_enabled", "开抢")
        return {"stop": stop, "start": start}

    def _set_hotkey(self, data):
        with Config.transaction() as cfg:
            old = dict(cfg["hotkey"])
            spec = (data.get("spec") or old.get("spec") or "F8").strip()
            enabled = bool(data.get("enabled", old.get("enabled", True)))
            start_spec = (data.get("start_spec") or old.get("start_spec") or "F7").strip()
            start_enabled = bool(data.get("start_enabled", old.get("start_enabled", True)))
            cfg["hotkey"] = {"enabled": enabled, "spec": spec, "stop": spec,
                             "start_enabled": start_enabled, "start_spec": start_spec}
            hk = dict(cfg["hotkey"])
        res = dict(_start_hotkeys(hk))
        bad = [f"{label}：{msg}" for label, (ok, msg) in res.items() if not ok]
        if bad:
            # **失败必须回滚。** `_start_hotkeys` 是先 stop 全部再按新配置注册，
            # 新键被别人占着时旧键已经停了 —— 用户点一次「保存」失败就把键盘
            # 弄没了。把旧配置写回去并重新注册，至少保住原来的热键。
            with Config.transaction() as cfg:
                cfg["hotkey"] = old
            back = dict(_start_hotkeys(old))
            back_bad = [f"{label}：{msg}" for label, (ok, msg) in back.items()
                        if not ok]
            msg = "；".join(bad)
            if back_bad:
                msg += "；恢复原热键也失败：" + "；".join(back_bad)
            else:
                msg += f"；已恢复原热键（{old.get('spec') or 'F8'}）"
            return self._json({"ok": False, "msg": msg,
                               "status": self._hotkey_status()})
        return self._json({"ok": True, "msg": "热键已就绪",
                           "status": self._hotkey_status()})


def _engine_is_firing():
    """抢购开火/确认期间让后台校时让路，别和点击、OCR 抢资源。"""
    try:
        st = _scheduler.status() or {}
    except Exception:                            # noqa: BLE001
        return False
    eng = st.get("engine") or {}
    if not isinstance(eng, dict) or not eng.get("running"):
        return False
    return str(eng.get("state") or "").lower() in (
        "fire", "firing", "confirm", "click", "clicking")


def _game_window_probe():
    """游戏窗口是否存在（只做 EnumWindows，不建窗口、不抢焦点）。"""
    try:
        from .input import find_game_window
        return bool(find_game_window())
    except Exception:                            # noqa: BLE001
        return False


def _on_auto_sync_result(res, trigger):
    """把每轮自动校时写进调度器日志（不弹窗、不抢焦点）。"""
    try:
        where = {"startup": "启动", "window": "游戏窗口出现",
                 "manual": "立即重校", "interval": "定时重校",
                 "retry": "失败重试"}.get(trigger, trigger or "自动")
        if res.get("ok"):
            _scheduler.note(f"{where}校时完成：{res.get('server') or 'NTP'} "
                            f"{float(res.get('offset') or 0) * 1000:+.0f}ms", "info")
        else:
            _scheduler.note(f"{where}校时失败：{res.get('error') or '原因未知'}"
                            "（已退回系统时间，后台自动重试）", "warn")
    except Exception:                            # noqa: BLE001
        pass


def _start_auto_timesync():
    """启动即自动校时；运行中按配置间隔持续重校，失败自动退避。

    用户不再需要点「时间校准」：首轮结果进 preflight，之后状态从
    `/api/scheduler` 的 `clock.auto` / `/api/timesync` 的 `auto` 持续可读。
    """
    try:
        cfg = Config.load()
        ts = cfg.get("time_sync") or {}
        # 需求里写作 `timesync.auto_interval_s`；老配置段名是 `time_sync`，
        # 两处都认，免得用户照着文档写了键却读不到。
        alias = cfg.get("timesync") or {}
        interval = ts.get("auto_interval_s", alias.get("auto_interval_s"))
        if not ts.get("enabled", True):
            _scheduler.note("自动校时已在配置里关闭（time_sync.enabled=false）",
                            "info")
            return
        try:
            samples = max(1, min(20, int(ts.get("samples") or 4)))
        except (TypeError, ValueError):
            samples = 4
        started = timesync.auto_start(
            servers=ts.get("servers"),
            samples=samples,
            interval_s=interval,
            notify=_on_auto_sync_result,
            busy=_engine_is_firing,
            window_probe=_game_window_probe,
        )
        st = timesync.auto_status()
        print(f"  自动校时：{'已启动' if started else '已在运行'}，"
              f"间隔 {float(st.get('interval_s') or 0):.0f}s，首轮进行中")
    except Exception as e:                       # noqa: BLE001
        _scheduler.note(f"自动校时启动失败：{type(e).__name__}: {e}"
                        "（将退回系统时间，不影响抢购）", "warn")


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
    checks = preflight.evaluate(preflight.collect(cfg, mode, workflow),
                               mode, workflow)
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
        # 走驱动注入（真实设备），不再 mouse_event：mouse_event 是系统级合成
        # 输入，会被目标当成注入标志、也不会到游戏里。驱动点击在鼠标当前
        # 位置按一下，亚毫秒内把光标回原位。
        from .input import Mouse
        m = Mouse(None)
        cx, cy = Mouse.cursor()
        m.click(cx, cy, expect_foreground=False, fire=False)
        _scheduler.note("截前热键：已在鼠标位置点了一下（驱动注入，进入排队）",
                        "hit")
    except Exception as e:                       # noqa: BLE001
        _scheduler.note(f"截前热键点不动：{type(e).__name__}: {e}", "warn")


def _make_hotkey(spec, callback):
    """建一个热键：**优先低级键盘钩子**，装不上才退回 RegisterHotKey。

    为什么换：RegisterHotKey 在游戏全屏独占键盘时经常收不到 —— 注册是
    成功的（日志「F7 已就绪」），按下去毫无反应。用户反复踩到这个坑。
    低级钩子在输入栈更底层，游戏吃键盘也照样能看见，而且只监听不拦截。

    **跨实例占用时不回退。** 钩子被另一个实例占着的时候，回退到
    RegisterHotKey 反而更糟：系统热键和对方的钩子会同时响应，一台机器上
    两个进程都去执行抢购。这种情况直接把失败原因交回上层。
    """
    h = HookHotkey(spec, callback, log=lambda m: _scheduler.note(m, "info"))
    ok, msg = h.start()
    if ok:
        return h, ok, msg
    if getattr(h, "_conflict", False):
        return h, False, msg
    h2 = Hotkey(spec, callback)
    ok2, msg2 = h2.start()
    return h2, ok2, f"{msg}；改用 RegisterHotKey：{msg2}"


def _stop_candidates(spec, hk):
    """停止热键的候选序列：配置的键优先，其次 `hotkey.stop_alternatives`。

    被占时**自动降级**而不是直接报错 —— F8 被别的程序/残留实例占着时，
    用户最需要的是「按一下就停」，不是先去设置页折腾。
    """
    raw = hk.get("stop_alternatives")
    if raw is None or raw == "":
        raw = "F8, F7, F9, Ctrl+F8"
    if isinstance(raw, (list, tuple)):
        alts = [str(x).strip() for x in raw]
    else:
        alts = [x.strip() for x in str(raw).replace("，", ",").split(",")]
    seq = [str(spec)] + [a for a in alts if a]
    out = []
    for s in seq:
        if s and s.upper() not in [x.upper() for x in out]:
            out.append(s)
    return out


def _start_hotkeys(hk=None):
    """按配置（重）注册两个热键，返回 {标签: (ok, msg)}。

    两个热键是两条独立的 RegisterHotKey 消息循环，互不影响 —— 一个被别的
    程序占了，另一个照样能用。
    """
    global _hotkey, _hotkey_start, _hotkey_pre, _hotkey_effective, _hotkey_warn
    if hk is None:
        hk = Config.load()["hotkey"]
    for h in (_hotkey, _hotkey_start, _hotkey_pre):
        if h is not None:
            try:
                h.stop()
            except Exception:                 # noqa: BLE001
                pass
    _hotkey = _hotkey_start = _hotkey_pre = None
    _hotkey_effective = ""
    _hotkey_warn = ""

    out = {}
    if hk.get("enabled", True):
        want = str(hk.get("stop") or hk.get("spec") or "F8")
        tried = []
        got = False
        last = ""
        start_cfg = str(hk.get("start_spec") or "F7").upper()
        cands = _stop_candidates(want, hk)
        if hk.get("start_enabled", True) and want.upper() != start_cfg:
            # 开抢热键占着的那个键不能拿来降级：同进程内再注册一次必然 183，
            # 症状就是「F8 被占，降级到 F7，结果 F7 也被占」。
            cands = [s for s in cands if s.upper() != start_cfg]
        for spec in cands:
            h, ok, msg = _make_hotkey(spec, _hotkey_stop)
            if ok:
                _hotkey = h
                _hotkey_effective = spec
                got = True
                last = msg
                if spec.upper() != want.upper():
                    _hotkey_warn = f"当前生效：{spec}（{want} 被占用）"
                    last = f"{msg}；{_hotkey_warn}"
                break
            try:
                h.stop()                      # 失败的对象也要放掉占位
            except Exception:                 # noqa: BLE001
                pass
            tried.append(f"{spec}：{msg}")
            last = msg
        if got:
            out["停止"] = (True, last)
        else:
            _hotkey_warn = "停止热键全部不可用（" + "；".join(tried) + "）"
            out["停止"] = (False, _hotkey_warn)
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


def _hint_once(key):
    """同一条启动提示只在第一次启动时说一次，之后静默。

    启动日志刷屏会把真正的问题淹掉（尤其 pythonw 形态日志会落到文件里）。
    标记写在用户数据目录（%LOCALAPPDATA%\\SJZ-Control）而不是安装目录：
    装到 Program Files 时安装目录是只读的，写不进去就会变成每次启动都刷。
    """
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "SJZ-Control")
        os.makedirs(d, exist_ok=True)
        flag = os.path.join(d, f".hint-{key}")
        if os.path.exists(flag):
            return False
        with open(flag, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        return True
    except OSError:
        return True


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

    # frozen 包内 __file__ 在 _internal\core\ 下，.venv-vl 实际放在主程序旁边
    app_root = root
    if os.path.basename(root).lower() == "_internal":
        app_root = os.path.dirname(root)
    venv_vl = os.path.join(app_root, ".venv-vl")
    if not os.path.isdir(venv_vl) and _hint_once("ocr-env"):
        problems.append("OCR 热环境（可选）没装：不影响开箱，内置 OCR 照常工作；"
                        "想要更快的识别，按 说明.txt 第九节装一次即可（此提示只出现一次）")

    for p in problems:
        print(f"  !! {p}")
        _scheduler.note(p, "warn")
    if problems:
        print("  !! 以上缺失项不会阻止控制台启动，但启动闸门会逐项拦下并说明")
    return problems


def _warm_poll_caches():
    """同步预热界面轮询的两个快照（台账统计、配置）。

    不预热的话，界面第一次打开 /api/records 会在请求线程上全扫一遍台账
    （2 万行 ≈ 70–100ms，忙盘更久）。以前是丢给后台线程，但「请求比预热
    先到」时那一次冷扫还是落在请求线程上（长时报告那条 185ms 就是这个
    形状）。现在改成同步、且放在 `serve_forever()` 之前 —— 开始接请求时
    快照已经就绪，请求线程永远不会替这次扫描买单。启动多花几十毫秒。
    """
    try:
        records.warm(200)
        Config.load_cached()
    except Exception as e:                   # noqa: BLE001
        print(f"  !! 轮询缓存预热失败：{type(e).__name__}: {e}")


def serve(host="127.0.0.1", port=8777, token=None):
    """起 HTTP 服务。token 非空时启用控制面鉴权（launcher 传入）。

    注意：token 只存进程内存，不进日志；打印时也只会打掩码。
    """
    global _started_at
    _started_at = time.time()
    set_control_token(token)
    try:
        httpd = _Server((host, port), Handler)
    except OSError as e:
        print(f"  !! 端口 {port} 起不来（多半是另一个控制台还在跑）：{e}")
        raise
    print(f"  SJZ 控制台已启动 -> http://{host}:{port}")
    if _ctrl_token is not None:
        print(f"  控制面鉴权：已启用（token ****{_ctrl_token[-4:]}）")
    _startup_checks()
    _seed_default_task()
    # 清掉已删任务留下的模板目录（data/<id>/ 只建不删，实测堆了 799 个）
    _scheduler.gc_task_dirs()
    _watcher.start()
    # OCR 服务的管家：定时/变慢/挂掉时自动蓝绿重启。没有 .venv-vl 就只是
    # 接管监控（用户自己起的服务照样管）。
    svc = _start_ocr_service()
    # 校时全自动：启动即校一次，之后后台实时重校（失败退避、游戏窗口出现
    # 立刻补校）。NTP 样本要走几百毫秒到几秒，放后台别挡着窗口出来。
    _start_auto_timesync()
    # 启动时就把热键注册上：以前只在用户改设置时才注册，重启后热键是死的
    _instance_selfcheck()
    for label, (ok, msg) in _start_hotkeys().items():
        print(f"  热键 {label}：{msg}")
        # 也写进调度器日志：pythonw 没有控制台，print 出去就没了 ——
        # 用户按 F7 没反应时，界面上至少能看到「注册失败：被别的程序占了」
        _scheduler.note(f"热键 {label}：{msg}", "info" if ok else "warn")
    # 同步预热放在最后：紧接着就开始接请求，请求线程不会再撞上冷扫。
    _warm_poll_caches()
    print("  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        timesync.auto_stop()
        _watcher.stop()
        _scheduler.stop()
        svc.stop()
        httpd.server_close()


# ---------------------------------------------------------------- 桌面 bridge
# pywebview 的 js_api 与悬浮窗共用这几个入口。悬浮窗取数只读**进程内存里的
# 状态快照**：不触发 OCR、不扫台账、不遍历目录，1 秒内复用同一份快照。

_OVERLAY_TTL = 1.0
_overlay_lock = threading.Lock()
_overlay_cache = {"at": 0.0, "snap": None}
# 每秒扫描次数环形缓冲：只由 scans 计数的差分推出来（不额外触发扫描）。
_overlay_rate = {"last": None, "buckets": []}
_OVERLAY_RATE_N = 30

_STATE_MAP = {"scan": "scanning", "watch": "scanning", "fire": "firing",
              "confirm": "firing", "done": "idle", "abort": "paused",
              "idle": "idle"}


def _instance_selfcheck():
    """启动自检：同会话里还有没有别的同名实例 / 热键占位者。

    「检测到 N 个实例」必须写进界面能读到的地方，不能只 print —— pythonw
    下 print 出去就没了，用户只会看到热键莫名其妙不可用。
    """
    global _instance_notice
    try:
        from . import hotkey as hk
        me = os.getpid()
        others = [x for x in hk.instances() if x.get("pid") != me]
        if not others:
            _instance_notice = ""
            return 0
        pids = ", ".join(str(x.get("pid")) for x in others)
        _instance_notice = f"检测到 {len(others) + 1} 个实例（另有 PID {pids}）"
        _scheduler.note(_instance_notice, "warn")
    except Exception as e:                    # noqa: BLE001
        _instance_notice = f"实例自检失败：{type(e).__name__}: {e}"
    return 0


def instances_info():
    """当前实例 / 其它同 exe 实例 / 各热键占位者。设置页直接照着显示。"""
    from . import hotkey as hk
    me = os.getpid()
    others = []
    try:
        others = [x for x in hk.instances() if x.get("pid") != me]
    except Exception:                         # noqa: BLE001
        pass
    holders = []
    for label, spec in (("停止", _hotkey_effective or "F8"),
                        ("开抢", (Config.load().get("hotkey") or {})
                         .get("start_spec") or "F7")):
        try:
            info = hk.holder_of(spec)
        except Exception:                     # noqa: BLE001
            info = None
        if info and info.get("pid") != me:
            holders.append({"label": label, "spec": spec, "pid": info.get("pid"),
                            "exe": info.get("exe"),
                            "started": info.get("started")})
    notice = ""
    if others:
        pids = ", ".join(str(x.get("pid")) for x in others)
        notice = f"检测到 {len(others) + 1} 个实例（另有 PID {pids}）"
    return {"self": me, "count": len(others) + 1, "others": others,
            "holders": holders, "notice": notice or _instance_notice,
            "hotkey_effective": _hotkey_effective, "hotkey_warn": _hotkey_warn}


def cleanup_instances():
    """结束「同会话 + 同 exe + 无可见窗口」的残留实例。

    只清僵尸：正在用的那个有窗口，不会被误杀；自己跳过；有窗口的一律不动。
    复用 desktop 里同样的 terminate/windows 探测，不另写一套。
    """
    from . import hotkey as hk
    from . import single_instance as si
    me = os.getpid()
    killed, skipped = [], []
    for it in hk.instances():
        pid = it.get("pid")
        if not pid or pid == me:
            continue
        try:
            win = si.windows_for_pid(pid)
        except Exception as e:                # noqa: BLE001
            skipped.append({"pid": pid, "why": f"窗口探测失败：{e}"})
            continue
        if win.get("visible"):
            skipped.append({"pid": pid, "why": "有可见窗口（正在使用，不动）"})
            continue
        try:
            ok = si.terminate_pid(pid)
        except Exception as e:                # noqa: BLE001
            ok = False
            skipped.append({"pid": pid, "why": f"结束失败：{e}"})
            continue
        if ok:
            killed.append({"pid": pid, "exe": it.get("exe") or ""})
        else:
            skipped.append({"pid": pid,
                            "why": "结束失败（可能是提权进程，需管理员）"})
    return {"killed": killed, "skipped": skipped}


def purchase_stop():
    """停止抢购：与界面停止按钮同一个实现（bridge / 悬浮窗 / 热键共用）。"""
    try:
        ok, msg = _scheduler.stop()
    except Exception as e:                    # noqa: BLE001
        return {"ok": False, "state": "error", "msg": f"{type(e).__name__}: {e}"}
    return {"ok": bool(ok), "state": overlay_state()["state"], "msg": msg}


def purchase_start():
    """开始抢购：沿用界面启动按钮的同一套前置检查。"""
    try:
        from .input import needs_elevation
        if needs_elevation():
            return {"ok": False, "state": "paused",
                    "msg": "权限不足：UIPI 会拦截键鼠消息，需要更高权限"}
        mode, workflow = _next_task_route()
        ok_gate, why = _preflight_gate(mode, workflow)
        if not ok_gate:
            return {"ok": False, "state": "idle",
                    "msg": f"启动前检查没过：{why}"}
        ok, msg = _scheduler.start()
    except Exception as e:                    # noqa: BLE001
        return {"ok": False, "state": "error", "msg": f"{type(e).__name__}: {e}"}
    return {"ok": bool(ok), "state": overlay_state()["state"], "msg": msg}


def stop_hotkeys():
    """退出前注销热键（钩子线程 + 命名互斥占位一起放掉）。"""
    global _hotkey, _hotkey_start, _hotkey_pre
    for h in (_hotkey, _hotkey_start, _hotkey_pre):
        try:
            if h is not None:
                h.stop()
        except Exception:                     # noqa: BLE001
            pass
    _hotkey = _hotkey_start = _hotkey_pre = None


def overlay_state():
    """悬浮窗数据：一次调用最多刷新一次（≥1s），全部读内存。"""
    now = time.time()
    with _overlay_lock:
        if _overlay_cache["snap"] is not None \
                and now - _overlay_cache["at"] < _OVERLAY_TTL:
            return dict(_overlay_cache["snap"])

    snap = {"running": False, "state": "idle", "item": "", "price": "",
            "scanned": 0, "matched": 0, "fired": 0,
            "last_event": "", "last_event_ts": 0.0,
            # 悬浮窗展开态追加：事件、速率、任务序号、耗时、倒计时
            "events": [], "scan_rate": [0] * _OVERLAY_RATE_N,
            "task_index": 0, "task_total": 0, "avg_match_ms": None,
            "next_retry_s": None, "next_window_s": None,
            "license_remaining_s": 0.0, "license_days": 0,
            "hotkey": "F8", "errors": [],
            # 迷你控制条：进度 + 可即时修改的必要设置
            "bought": 0, "attempts": 0, "target": 1,
            "settings": {"max_price": 0, "quota": 1, "semi": False}}

    try:
        cfg = Config.load()
        hk = cfg.get("hotkey") or {}
        snap["hotkey"] = str(_hotkey_effective or hk.get("stop")
                             or hk.get("spec") or "F8")
        if _hotkey_warn:
            snap["errors"].append(_hotkey_warn)
        if _instance_notice:
            snap["errors"].append(_instance_notice)
        _el = os.environ.get("SJZ_ELEVATE_NOTICE")
        if _el:
            snap["errors"].append(_el)
        tgt = cfg.get("target") or {}
        snap["item"] = str(tgt.get("name") or tgt.get("item") or "")
        price = tgt.get("price", tgt.get("max_price"))
        snap["price"] = "" if price in (None, "") else str(price)
        # 迷你控制条直接改三个必要项：价格上限 / 每轮数量 / 自动-半自动
        cn = cfg.get("console") or {}
        snap["settings"] = {
            "max_price": int(cn.get("max_price") or 0),
            "quota": int(cn.get("quota") or 1),
            "semi": bool(cfg.get("semi_mode")),
        }
    except Exception as e:                    # noqa: BLE001
        snap["errors"].append(f"配置：{type(e).__name__}: {e}")

    try:
        st = _scheduler.status() or {}
        eng = st.get("engine") or {}
        stats = eng.get("stats") or {}
        snap["running"] = bool(eng.get("running"))
        snap["state"] = _STATE_MAP.get(str(eng.get("state") or "idle").lower(),
                                       "idle")
        if eng.get("error"):
            snap["state"] = "error"
            snap["errors"].append(f"引擎：{eng['error']}")
        snap["scanned"] = int(stats.get("scans") or 0)
        snap["matched"] = int(stats.get("rows") or 0)
        snap["fired"] = int(stats.get("clicks") or 0)
        log = st.get("log") or []
        if log:
            last = log[-1]
            if isinstance(last, dict):
                snap["last_event"] = str(last.get("text") or last.get("msg") or "")
                snap["last_event_ts"] = float(last.get("ts") or 0.0)
            else:
                snap["last_event"] = str(last)
        # 最近 3 条事件（倒序）：只看调度器内存里的环形日志，不读盘
        evs = []
        for it in reversed(log[-3:]):
            if isinstance(it, dict):
                evs.append({"ts": float(it.get("ts") or 0.0),
                            "level": str(it.get("level") or "info"),
                            "text": str(it.get("text") or it.get("msg") or "")})
            else:
                evs.append({"ts": 0.0, "level": "info", "text": str(it)})
        snap["events"] = evs

        # 任务序号 / 总数：active 与队列里的 id 对齐，1 基
        tasks = st.get("tasks") or []
        enabled = [t for t in tasks if t.get("enabled", True)]
        snap["task_total"] = len(enabled)
        act = st.get("active")
        for i, t in enumerate(enabled):
            if act and t.get("id") == act:
                snap["task_index"] = i + 1
                break

        # 迷你控制条：已抢 / 目标 / 尝试次数（只读内存计数，不动上传语义）
        try:
            snap["bought"] = int(stats.get("bought") or 0)
        except (TypeError, ValueError):
            snap["bought"] = 0
        snap["attempts"] = int(snap["matched"])
        cur_task = None
        for t in enabled:
            if act and t.get("id") == act:
                cur_task = t
                break
        if cur_task is None and enabled:
            cur_task = enabled[0]
        if isinstance(cur_task, dict):
            try:
                snap["target"] = int(cur_task.get("quota") or 0) or 1
            except (TypeError, ValueError):
                snap["target"] = 1

        # 下次开卖窗口：队列里最近一个未来的 fire_at（没有就 null）
        soon = None
        for t in enabled:
            try:
                fa = float(t.get("fire_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if fa > now and (soon is None or fa < soon):
                soon = fa
        snap["next_window_s"] = round(soon - now, 1) if soon else None

        # 平均匹配耗时：引擎每秒扫描帧数（hz）在内存里，一帧一次模板匹配
        try:
            hz = float(stats.get("hz") or 0.0)
            if hz > 0:
                snap["avg_match_ms"] = int(round(1000.0 / hz))
            elif snap["scanned"] and float(stats.get("elapsed") or 0) > 0:
                snap["avg_match_ms"] = int(round(
                    float(stats["elapsed"]) * 1000.0 / snap["scanned"]))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    except Exception as e:                    # noqa: BLE001
        snap["errors"].append(f"状态：{type(e).__name__}: {e}")

    try:
        hk = (Config.load().get("hotkey") or {})
        if hk.get("enabled", True):
            if _hotkey is None:
                snap["errors"].append("停止热键未注册")
            else:
                hs = _hotkey.status() or {}
                if not hs.get("active"):
                    snap["errors"].append(
                        str(hs.get("msg") or "停止热键未生效"))
    except Exception:                         # noqa: BLE001
        pass

    try:
        snap["license_remaining_s"] = float(
            (_license.summary().get("remaining_s") or 0.0))
        snap["license_days"] = int(snap["license_remaining_s"] // 86400) \
            if snap["license_remaining_s"] > 0 else 0
    except Exception:                         # noqa: BLE001
        pass

    # 每秒扫描次数（近 30 秒）：用两次快照之间的 scans 差值累加，只读计数
    try:
        sec = int(time.time())
        with _overlay_lock:
            cur = int(snap["scanned"])
            prev = _overlay_rate["last"]
            delta = 0 if prev is None or cur < prev else cur - prev
            _overlay_rate["last"] = cur
            buckets = _overlay_rate["buckets"]
            if buckets and buckets[-1][0] == sec:
                buckets[-1][1] += delta
            else:
                buckets.append([sec, delta])
            while buckets and sec - buckets[0][0] > _OVERLAY_RATE_N - 1:
                buckets.pop(0)
            got = dict((s, c) for s, c in buckets)
        snap["scan_rate"] = [int(got.get(sec - (_OVERLAY_RATE_N - 1) + i, 0))
                             for i in range(_OVERLAY_RATE_N)]
    except Exception:                         # noqa: BLE001
        pass

    with _overlay_lock:
        _overlay_cache["at"] = time.time()
        _overlay_cache["snap"] = dict(snap)
    return snap
