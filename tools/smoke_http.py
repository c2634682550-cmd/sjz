"""HTTP 冒烟：真起一个服务，把端点挨个打一遍（用临时配置，不碰真数据）。"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config  # noqa: E402  ← 必须先于 server 导入

# 先把路径指到临时目录，再导入 server：server 模块级就会构造 Scheduler，
# 晚一步改 CONFIG_PATH 就已经把真实 config.json 重写了一遍。
tmp = tempfile.mkdtemp(prefix="sjz-smoke-")
config.CONFIG_PATH = os.path.join(tmp, "config.json")
config.TEMPLATE_DIR = os.path.join(tmp, "templates")
config.DATA_DIR = os.path.join(tmp, "data")

from core import ocr_service, server  # noqa: E402


class _NoService:
    def start(self):
        return False

    def stop(self):
        pass


ocr_service.make = lambda **kw: _NoService()      # 别真去拉 Paddle
server._watcher.start = lambda: None              # 别真去抓屏认页面
server._start_hotkeys = lambda hk=None: {}        # 别抢全局热键

def _free_port():
    """找一个能真绑上的端口。

    不能用固定端口：Windows 有「排除端口段」，绑上去会 WinError 10013，
    而那个端口上可能已经坐着别的服务（冒烟脚本会打到别人身上）。
    """
    import socket
    for port in range(18777, 18877):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用端口")


PORT = _free_port()
threading.Thread(target=server.serve, args=("127.0.0.1", PORT), daemon=True).start()

BASE = f"http://127.0.0.1:{PORT}"
for _ in range(100):
    try:
        with urllib.request.urlopen(f"{BASE}/api/config", timeout=1) as r:
            if "regions" in json.loads(r.read()):
                break
    except Exception:
        time.sleep(0.05)
else:
    print("服务没起来")
    sys.exit(1)

fails = []


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw[:120]}


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        fails.append(name)


print("--- 读 ---")
code, cfg = call("/api/config")
check("GET /api/config", code == 200 and "regions" in cfg, f"{code} {cfg}")
code, st = call("/api/scheduler")
check("GET /api/scheduler", code == 200 and "engine" in st, f"{code} {str(st)[:120]}")
check("  状态里有 page", "page" in st, str(st.keys()))
code, jb = call("/api/jobs")
check("GET /api/jobs", code == 200 and "busy" in jb, f"{code} {jb}")
code, sk = call("/api/skins")
check("GET /api/skins", code == 200 and sk.get("ok"), f"{code} {str(sk)[:80]}")
code, rec = call("/api/records?limit=5")
check("GET /api/records", code == 200 and rec.get("ok"), f"{code}")
code, sw = call("/api/switches")
check("GET /api/switches", code == 200 and sw.get("groups"), f"{code}")

print("--- 写（事务） ---")
code, r = call("/api/config", {"threshold": 0.91, "console": {"max_price": 4321}})
check("POST /api/config", code == 200 and r.get("ok"), f"{code} {r}")
code, cfg2 = call("/api/config")
check("  阈值落盘", abs(float(cfg2["threshold"]) - 0.91) < 1e-9, cfg2["threshold"])
check("  条件落盘", float(cfg2["console"]["max_price"]) == 4321, cfg2["console"])

code, r = call("/api/tasks/add", {"name": "", "mode": "list"})
check("POST /api/tasks/add（缺框选时应给出人话）",
      code == 400 and "列表" in (r.get("msg") or ""), f"{code} {r}")
code, r = call("/api/tasks")
check("  队列没被写脏", isinstance(r.get("tasks"), list), str(r)[:80])

print("--- 错误处理 ---")
code, r = call("/api/skins/locate", {"name": "根本不存在的皮肤xyz"})
check("未知皮肤 → 404 且是 JSON", code == 404 and r.get("ok") is False, f"{code} {r}")
code, r = call("/api/records?limit=abc")
check("坏参数 → 400 且是 JSON", code == 400 and r.get("ok") is False, f"{code} {r}")
code, r = call("/api/jobs?id=nope")
check("未知 job → 404", code == 404, f"{code}")

print("--- 后台动作 ---")
code, r = call("/api/jobs")
check("空闲时 busy=False", r.get("busy") is False, str(r))

print("--- 收尾 ---")
code, r = call("/api/scheduler/stop", {})
check("POST /api/scheduler/stop", code == 200, f"{code} {r}")

print()
if fails:
    print(f"失败 {len(fails)} 项：{fails}")
    sys.exit(1)
print("全部通过")
