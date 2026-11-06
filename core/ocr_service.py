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
    "alt_port": 0,             # 蓝绿用的备用端口，0 = 沿用 8082/8081 的老规矩
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


def _is_ocr_exe(path):
    """这个映像像不像我们的 OCR 服务。

    `paddlex.exe` 是启动器，它 re-exec 出来的真正服务是 `python.exe` ——
    两个都认。`pythonw.exe` 不算：那是控制台自己（窗口版解释器），
    真在 8081 上看到它说明端口配错了，不该动手。

    为什么要这道判断：`_kill_port` 是「按端口找 PID → taskkill」，端口被
    无关程序占着时会把人家杀掉。今天实测就撞到一次 —— 8081 上响应的根本
    不是我们的服务。
    """
    name = (path or "").replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if name.startswith("pythonw"):
        return False
    return name.startswith(("paddlex", "python"))


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
        self._log_fh = None     # 服务进程的 stdout 句柄，换实例时先关掉旧的

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
        self._close_log()

    def status(self):
        """给自检/日志看的快照。**必须不阻塞。**

        为什么：restart() 整段（起进程 → 等验活 → 预热）都握着这把锁，
        冷启动要十几秒，失败时最长 healthy_timeout_s（150 秒）。以前这里
        直接 `with self._lock`，于是 /api/preflight 跟着一起卡住 —— 而前端
        boot 是 `await loadPreflight(); await poll();` 串行的，卡住的结果就是
        状态栏一直停在 HTML 里的初始值「未连接 / OCR — / 校准 —」，
        看着像整个后端没连上，其实是 OCR 服务在加载模型。
        拿不到锁 = 正在重启，返回上一次的快照 + starting 标记。
        """
        snap = {
            "enabled": bool(self.cfg.get("enabled", True)),
            "port": self._port,
            "managed": self._proc is not None,
            "healthy": self._healthy,
            "uptime_s": round(time.monotonic() - self._started_at, 1)
            if self._started_at else 0,
            "restarts": self._restarts,
            "last_error": self._last_error,
            "starting": False,
        }
        if not self._lock.acquire(blocking=False):
            snap["starting"] = True
            return snap
        try:
            snap["port"] = self._port
            snap["managed"] = self._proc is not None
            snap["healthy"] = self._healthy
            snap["last_error"] = self._last_error
            snap["restarts"] = self._restarts
            return snap
        finally:
            self._lock.release()

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
        # 只看 TCP 会把「端口上蹲着别的程序／模型还没加载完」判成健康，
        # 于是我们的服务再也不会被拉起（review V09）。这里跑一次真推理。
        alive = _tcp_ok(host, port) and self._probe(f"http://{host}:{port}")
        self._healthy = alive
        if not alive:
            self._log(f"OCR 服务没响应（:{port}），重启")
            return self.restart("服务无响应")

        reason = self._degrade_reason()
        if not reason:
            return None
        # 变慢/到点：等空闲再动手，别在抢购那一轮里折腾
        if not self._wait_idle(float(self.cfg.get("idle_wait_s") or 0)):
            if self._stop.is_set():
                # 管家正在停止：这时候再起进程就是在关机路上凭空造一个
                self._last_error = "管家已停止，取消本次重启"
                self._log(self._last_error)
                return None
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
        """等引擎空闲。返回 False 表示「没等到」（超时或管家已停止）。"""
        if limit <= 0:
            return True
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            if not self._busy():
                return True
            if self._stop.wait(1.0):
                # 停止不是「等到了空闲」，以前在这里 return True 会让
                # tick() 继续往下走、在关机过程中重启服务（review V11）
                return False
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
                # 冷启动还有个坑：另一个端口上可能蹲着上次蓝绿切完没回收干净
                # 的实例（进程被强杀、或者切换中途断电）。它不影响这次启动，
                # 但会一直占着显存 —— 每留一个，新实例加载就慢一截。这里
                # 顺手收掉，和「杀接管来的老实例」是同一个理由。
                other = self._alt_port(new_port)
                if _tcp_ok(host, other) and self.cfg.get("kill_adopted", True):
                    self._log(f"备用端口 :{other} 上还留着上次的实例，先收掉")
                    self._kill_port(other)
            else:
                new_port = self._alt_port(old_port)
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

            if not self._wait_healthy(host, new_port, proc):
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
            elif self.cfg.get("kill_adopted", True) and old_port != new_port:
                # 老实例是「接管」来的（用户自己起的），我们手里没有进程句柄，
                # 但它还占着显存 —— 按端口找出来收掉，否则每重启一次就多
                # 一个常驻实例，跑一天能堆出五六个。
                #
                # **old_port == new_port 时绝对不能走这里**：那是冷启动，
                # 端口上现在监听的就是我们刚起的这个（spawn → 验活 → 预热
                # 一路下来它已经绑上了 8081）。按端口收「老实例」= 把自己
                # taskkill 掉，于是「起 → 被杀 → 60 秒后再起 → 再被杀」无限
                # 循环，界面上绝大多数时间都是「OCR 不可用」，偶尔闪一下正常。
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

    def _wait_healthy(self, host, port, proc=None, timeout_s=None):
        """等新实例真的能认字。

        只探 TCP 不够：端口通了但模型还没加载完，请求会 500 或者卡住。
        所以最后要跑一次真推理（一张纯色小图，几十毫秒）。

        **还要盯着自己起的那个进程**：端口探测只能证明「有人在听」，
        证明不了「听的是我起的这个」。实测踩过这一条 —— 手动起的实例占着
        8081，管家起的新实例绑不上端口、加载完就退了，而探针探到了别人那个
        实例，于是判「验活成功」：客户端被切过去，`_proc` 里记的却是一个
        已经死掉的句柄，后面「杀老实例」全靠端口猜。
        """
        deadline = time.monotonic() + float(
            timeout_s if timeout_s is not None
            else (self.cfg.get("healthy_timeout_s") or 150))
        while time.monotonic() < deadline and not self._stop.is_set():
            if proc is not None and proc.poll() is not None:
                return False        # 自己那个已经退了，别人应声不算数
            if _tcp_ok(host, port) and self._probe(f"http://{host}:{port}"):
                return True
            time.sleep(0.5)
        return False

    def _adopt_or_spawn(self):
        """启动时：端口上已经有人就接管监控，否则自己拉一个。

        三种情况：
          1. 配置的端口活着**且真能认字** → 接管（用户自己起的，或上次留下的）
          2. 配置的端口死了，但**备用端口**活着 → 那是上次重启后留下的实例，
             直接把客户端指过去，别再起一个新的（否则会顶掉端口、白等一次
             模型加载）
          3. 都死 → 起一个新的

        为什么「活着」还要加「真能认字」：8081 上监听的可能根本不是我们的
        服务（代理、别的工具、上次没退干净的半死实例）。只看 TCP 就接管，
        tick() 同样只看 TCP，于是它永远判「健康」—— 我们自己的 OCR 服务
        再也不会被拉起，界面一直「OCR 不可用」，重启多少次都一样。
        """
        host, port = self._endpoint()
        alt = self._alt_port(port)
        if _tcp_ok(host, port):
            url = f"http://{host}:{port}"
            # 先直接探一次；不成就给一点时间（上次留下的实例可能还在加载
            # 模型，十几秒），还不成就认定「不是我们的服务」，改用备用端口。
            budget = min(float(self.cfg.get("healthy_timeout_s") or 150), 20.0)
            if self._probe(url) or self._wait_healthy(host, port, None, budget):
                self._started_at = time.monotonic()
                self._healthy = True
                self._log(f"接管已有服务 :{port}")
                return True
            self._log(f":{port} 上有人监听但认不出字（不是 OCR 服务？），"
                      f"改用备用端口 :{alt}")
            return self.restart("端口上的不是 OCR 服务")
        if _tcp_ok(host, alt) and self._probe(f"http://{host}:{alt}"):
            ocr.set_url(f"http://{host}:{alt}")
            self._port = alt
            self._started_at = time.monotonic()
            self._healthy = True
            self._log(f"接管备用端口上的实例 :{alt}")
            return True
        return self.restart("启动时拉起")

    # ---------------- 默认实现 ----------------

    def _alt_port(self, port):
        """蓝绿要用的另一个端口。

        以前写死成「不是 8082 就是 8081」，用户把服务配在别的端口（或 8082
        本身被占）时就会踩到别人的端口。现在 `ocr.service.alt_port` 可以指定，
        没配才退回老规矩（review V22）。
        """
        try:
            want = int(self.cfg.get("alt_port") or 0)
        except (TypeError, ValueError):
            want = 0
        if want and want != port:
            return want
        return 8082 if port != 8082 else 8081

    def _close_log(self):
        fh = self._log_fh
        self._log_fh = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

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
        # 价格二次确认只认 tiny（见 tools/OCR_tiny.yaml 的说明）；想回整屏
        # 更准的 small，把 ocr.ppocr.pipeline 配成 tools/OCR_game.yaml 即可。
        yaml = os.path.join(root, self.cfg.get("pipeline") or "tools/OCR_tiny.yaml")
        if not os.path.isfile(yaml):
            yaml = os.path.join(root, "tools", "OCR_game.yaml")
        args = [exe, "--serve",
                "--pipeline", yaml if os.path.isfile(yaml) else "OCR",
                "--port", str(port), "--device", self.cfg.get("device") or "gpu:0"]
        env = dict(os.environ)
        env.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
        log_path = os.path.join(root, f"ocr_service_{port}.log")
        # 每次重启都开一个句柄且从不关闭：跑一天几十次切端口就是几十个
        # 泄漏的文件句柄，日志文件还一直被锁着（review V10）
        self._close_log()
        f = open(log_path, "ab")
        self._log_fh = f
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
            exe = self._pid_exe(pid)
            if not _is_ocr_exe(exe):
                # 端口被无关程序占着（代理、别的服务）—— 按端口盲杀太粗暴，
                # 今天就被这种东西绕过一次：8081 上根本不是我们的服务。
                self._log(f":{port} 上监听的是 {exe or '未知程序'}，不是 OCR 服务，不动它")
                continue
            try:
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                               capture_output=True, timeout=10)
            except Exception:                    # noqa: BLE001
                pass

    @staticmethod
    def _pid_exe(pid):
        """进程的映像路径。拿不到（权限不够 / 进程已退）返回 None。"""
        import ctypes
        from ctypes import wintypes
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return None
                return buf.value
            finally:
                k32.CloseHandle(h)
        except Exception:                        # noqa: BLE001
            return None

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
