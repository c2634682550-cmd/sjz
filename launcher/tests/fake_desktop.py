"""测试替身：模拟 desktop.py 的控制面协议。

不是要替代真正的 desktop.py —— 真的桌面端会开 WebView 窗口、抢鼠标，
自动化里不能跑。这里只实现启动器需要的那两条接口：
  GET  /api/ctrl/status   （校验 X-SJZ-Token + Host 必须是回环）
  POST /api/ctrl/stop     （回 200 后自己走正常退出）
另外实现「启动器没了就退出」，用来验证父进程守护链路。
"""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = ""
START = time.time()


def _log(msg):
    path = os.environ.get("FAKE_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.time():.2f} {msg}\n")
    except OSError:
        pass


def _parent_alive(pid):
    import ctypes
    if pid <= 0:
        return True
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, int(pid))
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x00000102
    finally:
        k32.CloseHandle(h)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        got = self.headers.get("X-SJZ-Token") or ""
        if got != TOKEN:
            _log("403 token mismatch len=%d/%d" % (len(got), len(TOKEN)))
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        _log("GET %s" % self.path)
        if self.path != "/api/ctrl/status":
            return self._send(404, {"ok": False})
        if not self._authorized():
            return self._send(403, {"ok": False, "msg": "bad token"})
        self._send(200, {"ok": True, "pid": os.getpid(),
                         "uptime_s": round(time.time() - START, 1),
                         "fake": True})

    def do_POST(self):
        if self.path != "/api/ctrl/stop":
            return self._send(404, {"ok": False})
        if not self._authorized():
            return self._send(403, {"ok": False, "msg": "bad token"})
        self._send(200, {"ok": True, "msg": "stopping"})
        threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)),
                         daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ctrl-token", default="")
    ap.add_argument("--launcher-pid", type=int, default=0)
    ap.add_argument("--elevated", action="store_true")
    args, _unknown = ap.parse_known_args()

    global TOKEN
    TOKEN = args.ctrl_token

    if args.launcher_pid:
        def watch():
            while True:
                time.sleep(0.5)
                if not _parent_alive(args.launcher_pid):
                    _log("launcher gone, exit")
                    os._exit(0)
        threading.Thread(target=watch, daemon=True).start()

    _log("starting httpd %s:%s" % (args.host, args.port))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    _log("httpd ready pid=%d port=%d" % (os.getpid(), args.port))
    httpd.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        _log("CRASH\n" + traceback.format_exc())
        raise
