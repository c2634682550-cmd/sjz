"""OCR 服务的进程管家：拉起、探活、无感重启（蓝绿双实例）。

为什么需要它
------------
PaddleX 服务连跑二十几个小时后会明显退化：实测单次识别从 150ms 涨到
2.4s，整屏直接超过客户端 5 秒超时、返回空。重启就恢复 —— 所以要有定时重启。

为什么不能「到点就杀」
----------------------
杀掉之后要重新加载模型（几十秒），这期间所有 OCR 调用都会失败。抢购正跑着
的时候等于把眼睛挖了。所以用**蓝绿**：

    1. 在另一个端口起一个新实例
    2. 等它真的能认字（TCP 通 + 跑一次真推理）
    3. 把客户端切到新端口（ocr.set_url）
    4. 再杀老实例

切换发生在两行代码之间，调用方一次都不会失败。旧实例在新实例验活之前
一直留着，新实例起不来就原地不动、下一轮再试。

什么时候重启
------------
  · 定时：默认 6 小时（ocr.service.restart_hours）
  · 变慢：最近 20 次识别的中位耗时超过阈值（ocr.service.slow_ms）
  · 挂了：探活失败（这时服务本来就用不了，不等空闲，直接重启）

前两种会**等引擎空闲**再动手 —— 正在抢购的一轮里重启没有意义，蓝绿虽然
不断流，但会占显存和带宽。等不到空闲就按 idle_wait_s 超时后照做。
"""
import os
import socket
import subprocess
import sys
import threading
import time

from . import ocr

DEFAULTS = {
    "enabled": True,
    "restart_hours": 6.0,      # 0 = 不定时重启
    "slow_ms": 800.0,          # 最近识别的中位耗时超过它就算「变慢」，0 = 不管
    "check_interval_s": 60.0,  # 多久检查一次
    "idle_wait_s": 600.0,      # 等引擎空闲最多等多久，超时也照做
    "healthy_timeout_s": 150.0,  # 新实例起不来的判定时间（要加载模型）
    "start_on_boot": True,     # 启动时没有服务就自己拉一个
    "kill_adopted": True,      # 重启时把「接管」来的老实例也收掉（否则会越堆越多）
    "device": "gpu:0",
}


def _tcp_ok(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _url_port(url):
    from urllib.parse import urlparse
    u = urlparse(url if "://" in url else "http://" + url)
    return (u.hostname or "127.0.0.1"), (u.port or 80)


class Supervisor:
    """管一个 PaddleX OCR 服务进程。不启动也不影响程序 —— 只是没人管它。"""

    def __init__(self, settings=None, is_busy=None, spawn=None, probe=None,
                 kill=None, kill_port=None, log=None):
        self.cfg = dict(DEFAULTS)
        if settings:
            self.cfg.update({k: v for k, v in settings.items() if v is not None})
        self._busy = is_busy or (lambda: False)
        self._spawn = spawn or self._spawn_real
        self._probe = probe or self._probe_real
        self._kill = kill or self._kill_real
        self._kill_port = kill_port or self._kill_port_real
        self._log = log or (lambda msg: None)

        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._proc = None
        self._port = None
        self._started_at = 0.0
        self._last_error = None
        self._restarts = 0
        self._healthy = False

    # ---------------- 生命周期 ----------------

    def start(self):
        """拉起管家线程。已有服务就只是接管监控，不会重复起一个。"""
        if not self.cfg.get("enabled", True):
            return False
        if self._thread and self._thread.is_alive():
            return False
        # **必须清掉停止标志**：stop() 里 set 过它，不清的话新线程一进
        # _loop 就被 `while not self._stop.wait(...)` 直接判死 —— 表现为
        # 「点了重启没反应」，日志里一个字都没有。
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ocr-svc",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)

    def status(self):
        with self._lock:
            return {
                "enabled": bool(self.cfg.get("enabled", True)),
                "port": self._port,
                "managed": self._proc is not None,
                "healthy": self._healthy,
                "uptime_s": round(time.monotonic() - self._started_at, 1)
                if self._started_at else 0,
                "restarts": self._restarts,
                "last_error": self._last_error,
            }

    # ---------------- 主循环 ----------------

    def _loop(self):
        if self.cfg.get("start_on_boot") and not self._adopt_or_spawn():
            self._log("启动时没能拉起 OCR 服务，稍后重试")
        while not self._stop.wait(float(self.cfg.get("check_interval_s") or 60)):
            try:
                self.tick()
            except Exception as e:                  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"

    def tick(self):
        """一次检查。返回做了什么（测试用）。"""
        host, port = self._endpoint()
        alive = _tcp_ok(host, port)
        self._healthy = alive
        if not alive:
            self._log(f"OCR 服务没响应（:{port}），重启")
            return self.restart("服务无响应")

        reason = self._degrade_reason()
        if not reason:
            return None
        # 变慢/到点：等空闲再动手，别在抢购那一轮里折腾
        if not self._wait_idle(float(self.cfg.get("idle_wait_s") or 0)):
            self._log(f"{reason}：等空闲超时，照做")
        return self.restart(reason)

    def _degrade_reason(self):
        hours = float(self.cfg.get("restart_hours") or 0)
        if hours > 0 and self._started_at:
            up = time.monotonic() - self._started_at
            if up >= hours * 3600:
                return f"定时重启（已跑 {up / 3600:.1f} 小时）"
        slow = float(self.cfg.get("slow_ms") or 0)
        if slow > 0:
            ms = self._recent_ms()
            if ms and ms >= slow:
                return f"识别变慢（最近中位 {ms:.0f}ms ≥ {slow:.0f}ms）"
        return None

    def _recent_ms(self):
        try:
            return ocr.metrics().get("recent_ms") or 0.0
        except Exception:                            # noqa: BLE001
            return 0.0

    def _wait_idle(self, limit):
        """等引擎空闲。返回 False 表示等到超时。"""
        if limit <= 0:
            return True
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            if not self._busy():
                return True
            if self._stop.wait(1.0):
                return True
        return False

    # ---------------- 蓝绿重启 ----------------

    def restart(self, reason="手动"):
        """起新实例 → 验活 → 切客户端 → 杀老实例。返回是否成功。"""
        with self._lock:
            host, old_port = self._endpoint()
            # 冷启动（老端口本来就没服务）就直接在配置的端口上起，
            # 别绕到备用端口 —— 否则配置里写的 8081 永远空着，下次启动
            # 又要走一遍「备用端口接管」的兜底。
            if not _tcp_ok(host, old_port):
                new_port = old_port
            else:
                new_port = 8082 if old_port != 8082 else 8081
            if _tcp_ok(host, new_port):
                # 备用端口被别的进程占着，别去踩它
                self._last_error = f"端口 {new_port} 已被占用"
                self._log(self._last_error)
                return False
            self._log(f"重启（{reason}）：{old_port} → {new_port}")
            try:
                proc = self._spawn(new_port)
            except Exception as e:                   # noqa: BLE001
                self._last_error = f"起不来：{type(e).__name__}: {e}"
                self._log(self._last_error)
                return False

            if not self._wait_healthy(host, new_port):
                self._last_error = "新实例没验活成功，保持原实例"
                self._log(self._last_error)
                self._kill(proc)
                return False

            # 切之前先把它焐热：冷实例的第一次识别要 2~5 秒（PaddleX 按
            # 输入尺寸分配显存、编译 kernel），显卡被游戏占着时更久 —— 正好
            # 撞上客户端 5 秒超时，调用方看到的是「没认出字」。
            self._warmup(f"http://{host}:{new_port}")

            # 关键的两行：先切客户端，再杀老的。顺序反了就会出现一段
            # 「没有服务」的窗口。
            ocr.set_url(f"http://{host}:{new_port}")
            old = self._proc
            self._proc = proc
            self._port = new_port
            self._started_at = time.monotonic()
            self._restarts += 1
            self._healthy = True
            if old is not None:
                self._kill(old)
            elif self.cfg.get("kill_adopted", True):
                # 老实例是「接管」来的（用户自己起的），我们手里没有进程句柄，
                # 但它还占着显存 —— 按端口找出来收掉，否则每重启一次就多
                # 一个常驻实例，跑一天能堆出五六个。
                self._kill_port(old_port)
            self._log(f"已切到 :{new_port}")
            return True

    @staticmethod
    def _warmup(url):
        """用真机尺寸的空图各跑一遍，把显存和 kernel 预热好。

        尺寸取两种最常用的：整屏 1920×1080、左侧列表 750×1560。
        认不出字没关系，这里只要它把这次推理跑完。
        """
        try:
            import numpy as np

            from . import ocr_ppocr
            b = ocr_ppocr.make({"mode": "http", "url": url,
                                "timeout_s": 60, "cache": 0})
            for shape in ((1080, 1920), (1560, 750)):
                try:
                    b.read_text(np.full(shape + (3,), 32, dtype=np.uint8))
                except Exception:                # noqa: BLE001
                    pass
        except Exception:                        # noqa: BLE001
            pass

    def _wait_healthy(self, host, port):
        """等新实例真的能认字。

        只探 TCP 不够：端口通了但模型还没加载完，请求会 500 或者卡住。
        所以最后要跑一次真推理（一张纯色小图，几十毫秒）。
        """
        deadline = time.monotonic() + float(self.cfg.get("healthy_timeout_s") or 150)
        while time.monotonic() < deadline and not self._stop.is_set():
            if _tcp_ok(host, port) and self._probe(f"http://{host}:{port}"):
                return True
            time.sleep(0.5)
        return False

    def _adopt_or_spawn(self):
        """启动时：端口上已经有人就接管监控，否则自己拉一个。

        三种情况：
          1. 配置的端口活着 → 接管（用户自己起的，或上次留下的）
          2. 配置的端口死了，但**备用端口**活着 → 那是上次重启后留下的实例，
             直接把客户端指过去，别再起一个新的（否则会顶掉端口、白等一次
             模型加载）
          3. 都死 → 起一个新的
        """
        host, port = self._endpoint()
        alt = 8082 if port != 8082 else 8081
        if _tcp_ok(host, port):
            self._started_at = time.monotonic()
            self._healthy = True
            self._log(f"接管已有服务 :{port}")
            return True
        if _tcp_ok(host, alt) and self._probe(f"http://{host}:{alt}"):
            ocr.set_url(f"http://{host}:{alt}")
            self._port = alt
            self._started_at = time.monotonic()
            self._healthy = True
            self._log(f"接管备用端口上的实例 :{alt}")
            return True
        return self.restart("启动时拉起")

    # ---------------- 默认实现 ----------------

    def _endpoint(self):
        """当前客户端指向哪里 —— 以 ocr 的配置为准，别自己另记一份。"""
        try:
            url = (ocr.current_url() or "").strip()
        except Exception:                            # noqa: BLE001
            url = ""
        if not url:
            from .config import DEFAULTS as CD
            url = ((CD.get("ocr") or {}).get("ppocr") or {}).get("url") or ""
        return _url_port(url)

    def _spawn_real(self, port):
        from . import config as cfgmod
        root = cfgmod.ROOT
        exe = os.path.join(root, ".venv-vl", "Scripts", "paddlex.exe")
        if not os.path.isfile(exe):
            raise FileNotFoundError(
                f"没有 {exe}（OCR 服务环境没装，见 说明.txt 第九节）")
        yaml = os.path.join(root, "tools", "OCR_game.yaml")
        args = [exe, "--serve",
                "--pipeline", yaml if os.path.isfile(yaml) else "OCR",
                "--port", str(port), "--device", self.cfg.get("device") or "gpu:0"]
        env = dict(os.environ)
        env.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
        log_path = os.path.join(root, f"ocr_service_{port}.log")
        f = open(log_path, "ab")
        kw = {}
        if sys.platform == "win32":
            # 别弹黑框；进程归我们管，退出时一起收掉
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT,
                                cwd=root, env=env, **kw)

    def _probe_real(self, url):
        """跑一次真推理：纯色小图，认不出字没关系，能正常返回就说明模型在。"""
        try:
            import numpy as np

            from . import ocr_ppocr
            b = ocr_ppocr.make({"mode": "http", "url": url, "timeout_s": 8,
                                "cache": 0})
            img = np.full((40, 160, 3), 32, dtype=np.uint8)
            b.read_text(img)
            return True
        except Exception:                            # noqa: BLE001
            return False

    @staticmethod
    def _pids_on_port(port):
        """谁在监听这个端口。Windows 上用 netstat -ano。"""
        try:
            # 中文 Windows 的 netstat 是 GBK，按 utf-8 读会炸
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True,
                                 encoding="mbcs", errors="replace",
                                 timeout=10).stdout or ""
        except Exception:                        # noqa: BLE001
            return []
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}")                     and parts[3].upper() == "LISTENING":
                if parts[4].isdigit() and parts[4] not in pids:
                    pids.append(parts[4])
        return pids

    def _kill_port_real(self, port):
        for pid in self._pids_on_port(port):
            try:
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                               capture_output=True, timeout=10)
            except Exception:                    # noqa: BLE001
                pass

    def _kill_real(self, proc):
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:                            # noqa: BLE001
            pass
        try:
            if proc.poll() is None:
                # paddlex 会派生子进程（uvicorn），整棵树一起收
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
        except Exception:                            # noqa: BLE001
            try:
                proc.kill()
            except Exception:                        # noqa: BLE001
                pass


_current = None


def install(svc):
    """登记当前管家，供启动前检查 / 界面读状态。"""
    global _current
    _current = svc
    return svc


def current():
    return _current


def make(settings=None, is_busy=None, log=None):
    return install(Supervisor(settings=settings, is_busy=is_busy, log=log))
