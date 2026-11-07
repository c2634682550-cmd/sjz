"""桌面版入口：原生窗口内嵌控制台，不打开浏览器。

和 run.py 的分工：
  run.py      浏览器模式，开发时用（改完前端刷新即可，不用重启）
  desktop.py  桌面模式，打包成 exe 给最终用户

两者共用 core.server 的全部端点。
"""
import argparse
import ctypes
from ctypes import wintypes
import os
import re
import socket
import sys
import threading
import time
import traceback

# 冻结后（PyInstaller）路径基准是解包目录，源码运行时是仓库根
if getattr(sys, "frozen", False):
    BASE = sys._MEIPASS
    APPDIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    APPDIR = BASE
sys.path.insert(0, BASE)


# ---------------------------------------------------------------- 线程预算
# 启动瞬间整机卡死的根因之一：numpy / onnxruntime / OpenCV 默认按逻辑核数
# 开线程池，16 核机器一 import 就吃满全核。这里必须在任何重库 import 之前
# 把预算压到 min(4, 逻辑核//2)；SJZ_THREADS 或 config.json 的
# perf.threads 可覆盖（显式 0 = 不限制）。
def _thread_budget():
    raw = os.environ.get("SJZ_THREADS", "")
    if not raw:
        try:
            import json
            cfg_path = os.path.join(APPDIR, "config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    perf = (json.load(f) or {}).get("perf") or {}
                raw = str(perf.get("threads", ""))
        except Exception:
            raw = ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        n = max(1, min(4, (os.cpu_count() or 4) // 2))
    return n


THREADS = _thread_budget()
for _k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
           "ORT_NUM_THREADS", "OMP_THREAD_LIMIT"):
    os.environ[_k] = str(THREADS)

WINDOW_TITLE = "SJZ 抢购控制台"

# 无控制台的 windowed exe 里 print 什么也看不见，所以出问题只能靠日志文件。
# 窗口没起来是最难查的一类故障（进程在、端口通、就是没界面）。
LOG_PATH = os.path.join(APPDIR, "desktop.log")

# WebView2 的 cookie 存储目录。pywebview 默认 private_mode=True：每新建一个
# WebView2 控件（悬浮窗是第二个窗口）都会调 CookieManager.DeleteAllCookies()
# 把整份 cookie 清空 —— 主窗首屏刚种下的 sjz_ctrl 就是这样被清掉的，之后
# 所有 /api/* 立刻 403。关掉 private_mode 并给一个专属 profile 目录，两个
# 窗口共享一份 cookie，服务端种的令牌才能一直带在请求上。
WEBVIEW_PROFILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or APPDIR, "SJZ-Control", "webview-profile")


def _log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)     # 有控制台时（源码运行）也打一份
    except Exception:
        pass


# ---------------------------------------------------------------- DPI 感知
# **必须在 import webview / core.server 之前**（审查 R09-04）。
# 以前是 `pywebview.start()` 之前才调 SetProcessDPIAware()，进程被锁成
# system-aware：笔记本 150% + 外接 100% 的混合 DPI 下，Win32 窗口坐标、
# GetSystemMetrics、mss 截图三者口径不一致，坐标整体偏移。
# 现在是 per-monitor V2，坐标系口径统一为「物理像素」：
#   归一化区域 → 物理像素：只由 core.screen.Screen.norm_to_px() 负责；
#   物理像素 → 鼠标 0..65535：只由 core.input.Mouse 负责；
#   其它模块只处理物理像素，不再有第二次缩放。
try:
    from core.screen import enable_dpi_awareness, awareness_name
    _DPI_AWARENESS = enable_dpi_awareness()
except Exception:                    # mss/ctypes 不可用也不能挡住启动
    _DPI_AWARENESS = None

try:
    import webview                      # noqa: E402
except Exception:                       # 没装 pywebview / WebView2 缺失
    webview = None

from core.server import serve  # noqa: E402

# core.server 会连带 import cv2；OpenCV 自己的线程池不吃 OMP 环境变量，
# 必须显式设一次，否则默认还是按逻辑核数开。
try:
    import cv2 as _cv2
    _cv2.setNumThreads(THREADS)
except Exception as _e:
    _log("线程预算 %d（cv2 限制失败：%s）" % (THREADS, _e))
else:
    _log("线程预算 %d（OMP/ORT/cv2 均已限制）" % THREADS)


_MB_YESNO = 0x04
_MB_ICONERROR = 0x10
_MB_ICONWARNING = 0x30
_MB_TOPMOST = 0x4000
_IDYES = 6


def _ask(msg, flags):
    """原生消息框；无交互桌面（自动化）时返回 0，绝不阻塞验收。"""
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, msg, WINDOW_TITLE, flags))
    except Exception:
        return 0


def _interactive():
    """能不能弹框：SJZ_NO_WINDOW / --no-window 的验收场景下不能。"""
    return not _no_window_requested()


def _single_instance():
    """同一时间只允许一个实例在跑。返回 True = 可以继续。

    抢不到闸时**不允许静默退出**：用户看到的就是「双击没反应」，而且
    旧进程握着互斥量、零窗口，永远恢复不了。顺序是：
      按 PID 探测旧窗口 → 有窗口就置前；没窗口就发恢复信号让它自己找回来；
      还不成，弹框给出可执行的处理办法（结束残留并继续启动）。
    """
    from core import single_instance as si

    if si.acquire():
        si.write_owner()
        _start_restore_listener()
        return True

    info = si.read_owner() or {}
    pid = int(info.get("pid") or 0)
    exe = info.get("exe") or "?"
    probe = si.windows_for_pid(pid) if pid else {"total": 0, "visible": 0,
                                                 "hwnds": []}
    _log(f"单实例：旧实例 pid={pid or '未知'} exe={exe} "
         f"存活={si.pid_alive(pid) if pid else '未知'} "
         f"窗口探测 total={probe['total']} visible={probe['visible']} "
         f"hwnd={probe['hwnds'][:3]}")

    if pid and probe["visible"] and si.activate_pid(pid):
        _log("已把旧实例窗口提到前面")
        return False

    # 旧实例可能只是窗口丢了：命名事件让它自己 ShowWindow + 置前
    if pid and si.pid_alive(pid):
        if si.signal_restore():
            for _ in range(8):
                time.sleep(0.25)
                probe = si.windows_for_pid(pid)
                if probe["visible"] and si.activate_pid(pid):
                    _log("旧实例收到恢复信号后已把窗口提回前台")
                    return False
        _log("旧实例没有响应恢复信号（窗口确实不存在）")

    if not pid:
        _log("!! 单实例闸被占，但读不到旧实例 PID（instance.json 缺失）")
        if _interactive():
            _ask("SJZ 控制台已经在运行，但读不到它的进程号，无法自动恢复窗口。\n\n"
                 "处理办法（任选一种）：\n"
                 "1) 任务管理器结束所有 SJZ-Control.exe 后重新双击；\n"
                 "2) 命令行执行：SJZ-Control.exe --stop",
                 _MB_ICONWARNING | _MB_TOPMOST)
        return False

    # 有 PID、零窗口：这就是「双击没反应」的现场
    if not _interactive():
        _log("!! 旧实例零窗口（僵尸），自动化模式不弹框，按已运行退出")
        return False

    code = _ask(
        "SJZ 控制台已经在后台运行，但窗口不见了（上次关闭后没收尾）。\n\n"
        f"旧实例 PID：{pid}\n路径：{exe}\n"
        f"窗口探测：共 {probe['total']} 个，可见 0 个\n\n"
        "点「是」＝结束这个残留进程并继续启动。\n"
        "点「否」＝保持不变（之后可用 SJZ-Control.exe --stop 清理）。",
        _MB_YESNO | _MB_ICONWARNING | _MB_TOPMOST)
    if code != _IDYES:
        _log("用户选择不结束残留实例，本进程退出")
        return False

    _log(f"结束残留实例 pid={pid}")
    si.terminate_pid(pid)
    deadline = time.time() + 5
    while time.time() < deadline and si.pid_alive(pid):
        time.sleep(0.1)
    if si.pid_alive(pid) or not si.mutex_free():
        _log(f"!! 结束残留失败（存活={si.pid_alive(pid)} "
             f"闸空闲={si.mutex_free()}）")
        if _interactive():
            _ask("结束残留实例失败（它可能是以管理员身份启动的）。\n\n"
                 "请用任务管理器结束 SJZ-Control.exe，或在管理员命令行执行：\n"
                 f"taskkill /F /PID {pid}",
                 _MB_ICONERROR | _MB_TOPMOST)
        return False

    if not si.acquire():
        _log("!! 清掉残留后仍抢不到单实例闸")
        return False
    si.write_owner()
    _start_restore_listener()
    _log("残留实例已结束，继续启动")
    return True


def _start_restore_listener():
    """监听恢复信号：第二个实例双击时，把本进程的窗口找回来置前。"""
    def loop():
        try:
            from core import single_instance as si
            h = si.create_restore_event()
            if not h:
                _log("!! 恢复信号事件创建失败")
                return
            while True:
                if not si.wait_restore(h):
                    continue
                _log("收到恢复信号：把窗口提回前台")
                for _ in range(6):
                    if si.activate_pid(os.getpid()):
                        break
                    time.sleep(0.5)
        except Exception as e:                # noqa: BLE001
            _log(f"!! 恢复信号监听失败：{type(e).__name__}: {e}")

    threading.Thread(target=loop, name="restore-signal", daemon=True).start()


def _release_mutex():
    """放掉单实例闸（提权重开前必须放，见 single_instance.release）。"""
    from core.single_instance import release
    release()


def _instance_teardown():
    """任何退出路径都要注销热键 + 清登记 + 放闸，别留下握闸的僵尸。"""
    try:
        from core import server
        server.stop_hotkeys()
    except Exception:                          # noqa: BLE001
        pass
    try:
        from core import single_instance as si
        si.clear_owner(os.getpid())
        si.release()
    except Exception:                          # noqa: BLE001
        pass


def _hard_exit(code=0):
    """关窗即真退出：放输入 → 放单实例闸 → 硬退。

    以前窗口销毁后 pywebview 主循环不返回，进程攥着互斥量活着，但零窗口：
    第二次双击只会看到「已经有一个实例在跑」然后静默退出。
    """
    try:
        _release_input_before_exit(timeout=0.3)
    except Exception:                          # noqa: BLE001
        pass
    _instance_teardown()
    _log("进程退出（已释放单实例闸与实例登记）")
    # os._exit 走的是 CRT 退出路径，在个别环境（被 job/调试器托管、WebView2
    # 还有非 daemon 线程）里会被拖住（实测进程还要多活 2 秒）。TerminateProcess
    # 不经过任何 Python/C 退出钩子，直接在内核里把进程收掉。
    # 必须显式声明原型：默认 restype=c_int 会把 -1 的伪句柄截断成 int，
    # TerminateProcess 直接抛异常（之前那层兜底其实一直没生效）。
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.TerminateProcess.restype = ctypes.c_int
        if not k32.TerminateProcess(k32.GetCurrentProcess(), int(code)):
            _log(f"!! TerminateProcess 失败 err={ctypes.get_last_error()}")
    except Exception as e:                     # noqa: BLE001
        _log(f"!! TerminateProcess 不可用：{type(e).__name__}: {e}")
    os._exit(int(code))


def _stop_existing():
    """`--stop`：同权限自我收尾——结束残留实例、清 lock、放闸。

    提权实例杀不掉时该命令同样会失败（同权限原则），但日常实例
    （启动器已改为 asInvoker）一定能收掉。
    """
    from core import single_instance as si

    info = si.read_owner() or {}
    pid = int(info.get("pid") or 0)
    exe = info.get("exe") or "?"
    stopped = False
    if pid and si.pid_alive(pid):
        _log(f"--stop：结束实例 pid={pid} exe={exe}")
        si.terminate_pid(pid)
        deadline = time.time() + 5
        while time.time() < deadline and si.pid_alive(pid):
            time.sleep(0.1)
        stopped = not si.pid_alive(pid)
        _log(f"--stop：结束结果={stopped}")
    elif pid:
        _log(f"--stop：登记里的 pid={pid} 已经不在了（清理过期登记）")

    for p in (os.path.join(APPDIR, "config.json.lock"),
              os.path.join(APPDIR, "data", "config.json.lock")):
        try:
            os.remove(p)
            _log(f"--stop：已清 {p}")
        except OSError:
            pass

    si.clear_owner()
    free = si.mutex_free()
    _log(f"--stop 完成：pid={pid or '无'} 结束={stopped} 单实例闸空闲={free}")
    if _interactive():
        if stopped or free:
            _ask("SJZ 控制台已收尾。", 0x40)          # MB_ICONINFORMATION
        else:
            _ask("没能结束残留实例（它可能是管理员启动的）。\n\n"
                 "请在管理员命令行执行：\n"
                 f"taskkill /F /PID {pid}",
                 _MB_ICONWARNING | _MB_TOPMOST)
    return 0 if (stopped or free) else 1


def _pick_port(host, want):
    """want 被占用就往后找一个空的。

    桌面版默认端口被别的实例占了时，报错让人去杀进程是最差的做法 ——
    用户双击 exe 就该看到窗口。
    """
    for p in range(want, want + 20):
        with socket.socket() as s:
            if s.connect_ex((host, p)) != 0:      # 连不上 = 没人在用
                return p
    raise RuntimeError(f"{want}~{want + 20} 都被占用")


# ---------------------------------------------------------------- 父进程守护


_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_WAIT_OBJECT_0 = 0x00000000


def _parent_alive(pid):
    """父进程还活着吗？OpenProcess + WaitForSingleObject(0)。

    查不到（权限不足等）按「活着」处理：宁可多活一秒，也不能因为一次
    查询失败把自己误杀。
    """
    if pid <= 0:
        return True
    try:
        k32 = ctypes.windll.kernel32
        # 64 位句柄：不声明签名会被 c_int 截断，活着的父进程会被判成已退出
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        k32.WaitForSingleObject.restype = ctypes.c_uint
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(_SYNCHRONIZE, False, int(pid))
        if not h:
            return False                       # 进程不存在 = 已经没了
        try:
            return k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    except Exception:                          # noqa: BLE001
        return True


def _start_parent_watchdog(pid):
    """启动器没了就安全停机：先释放输入，再走 server 的正常收尾路径。

    不是硬杀：`_shutdown_soon` 会先 `scheduler.stop()`（把正在点的
    鼠标/按键抬起来），再放单实例闸，最后才 os._exit。
    """
    def loop():
        while True:
            time.sleep(1.0)
            if _parent_alive(pid):
                continue
            _log(f"!! 启动器进程 {pid} 已退出，控制台安全停机")
            try:
                _release_input_before_exit()
            except Exception:                  # noqa: BLE001
                pass
            try:
                from core import server as _srv
                _srv._shutdown_soon("launcher-gone")   # noqa: SLF001
            except Exception as e:              # noqa: BLE001
                _log(f"!! 收尾失败：{type(e).__name__}: {e}")
                os._exit(0)
            return

    t = threading.Thread(target=loop, name="parent-watch", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 打包资源


def _seed_missing_assets():
    """把随包的 templates/tools 释放到 exe 同级目录（只补缺，不覆盖）。

    PyInstaller onedir 会把 datas 放进 `_internal`，而 `core/config.py`
    的 TEMPLATE_DIR 是 exe 同级目录 —— 不释放的话，用户装完看到的就是
    一个空的 `templates/`，程序因为没有模板而静默空转（审查 R09-05）。
    """
    if os.path.normcase(os.path.abspath(BASE)) == os.path.normcase(os.path.abspath(APPDIR)):
        return 0                       # 源码运行：BASE 就是 APPDIR
    copied = 0
    for sub in ("templates", "tools"):
        src_root = os.path.join(BASE, sub)
        if not os.path.isdir(src_root):
            continue
        for root, _dirs, files in os.walk(src_root):
            rel = os.path.relpath(root, src_root)
            dst_dir = os.path.join(APPDIR, sub) if rel == "." else os.path.join(
                APPDIR, sub, rel)
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except OSError:
                continue
            for name in files:
                src = os.path.join(root, name)
                dst = os.path.join(dst_dir, name)
                if os.path.exists(dst):
                    continue
                try:
                    import shutil
                    shutil.copy2(src, dst)
                    copied += 1
                except OSError:
                    pass
    if copied:
        _log(f"已释放随包资源 {copied} 个文件到 {APPDIR}")
    return copied


def _writable_dir(path):
    """真写一个临时文件判断目录可写；`os.access(W_OK)` 在 Windows 上会骗人。"""
    import tempfile
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return False
    try:
        with tempfile.TemporaryFile(dir=path, prefix=".write_probe"):
            pass
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 窗口尺寸


def _primary_work_area():
    """主显示器工作区（物理像素，不含任务栏）；拿不到返回 None。"""
    try:
        r = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(r), 0)      # SPI_GETWORKAREA
        if ok and r.right > r.left and r.bottom > r.top:
            return r.right - r.left, r.bottom - r.top
    except (AttributeError, OSError):
        return None
    return None


def _fit_window(width, height, margin=16):
    """把窗口夹到工作区内，返回 (宽, 高, min_size)。

    审查 R09-10：窗口尺寸写死 1180×800，1366×768 或 150% 缩放下底部
    控件被推到屏幕外，用户看不到「开始/停止」。这里按实际工作区夹紧，
    最小尺寸也同步下压，保证标题栏以下的操作区永远可见。
    """
    aw, ah = _primary_work_area() or (1920, 1080)
    width = max(720, min(int(width), aw - margin))
    height = max(560, min(int(height), ah - margin))
    min_size = (min(900, width), min(620, height))
    return width, height, min_size


# ---------------------------------------------------------------- 退出收尾


def _release_input_before_exit(timeout=0.3):
    """退出前确认「输入已释放」，再放行进程退出（审查 R09-08）。

    `server._bye` / 调度器停止后必须确认鼠标按键与热键真的抬起来了：
    引擎可能正卡在一次点击/拖拽里，此时进程直接退出会把左键留在按下
    状态，游戏里一直拖视野。

    以前这里不管有没有活都在循环里睡满 timeout（冻结包上光这一段就要
    1.5 秒）。现在先看「有没有未完成的注入」：没有就用到 stop() 的同步
    结果直接返回；有就等引擎线程真正结束（join 是事件驱动，不空转），
    最多等 timeout（默认 300ms），到点也放行退出。
    """
    t0 = time.monotonic()
    try:
        from core import server
    except Exception:                    # noqa: BLE001
        return True
    sched = getattr(server, "_scheduler", None)
    if sched is None:
        return True                      # 服务还没起来，没有输入可释放

    eng = getattr(sched, "_engine", None)
    thread = getattr(sched, "_thread", None)
    busy = False
    try:
        busy = bool(thread and thread.is_alive())
        if not busy and eng is not None:
            busy = bool(eng.status().get("running"))
    except Exception:                    # noqa: BLE001
        busy = True                      # 状态查不出来就按「可能有活」处理

    ok, msg = True, "调度器未启动"
    try:
        res = sched.stop()
        if isinstance(res, tuple) and len(res) >= 2:
            ok, msg = bool(res[0]), str(res[1])
        else:
            ok = bool(res)
    except Exception as e:               # noqa: BLE001
        ok, msg = False, f"stop() 报错：{type(e).__name__}: {e}"

    # 没有未完成的注入：stop() 已经同步放完输入，不用陪等超时。
    if not busy:
        _log(f"退出前输入已释放（{msg}，{int((time.monotonic() - t0) * 1000)}ms）")
        return True

    deadline = time.monotonic() + max(0.0, float(timeout))
    while not ok and time.monotonic() < deadline:
        if eng is None:
            break
        try:
            eng.force_release_input()
        except Exception:                # noqa: BLE001
            pass
        # 等线程自己结束（内核事件），比固定 sleep 轮询更快也更省。
        remain = deadline - time.monotonic()
        if thread is not None and thread.is_alive() and remain > 0:
            thread.join(min(remain, 0.05))
        else:
            time.sleep(min(max(remain, 0.0), 0.02))
        try:
            alive = bool(eng.status().get("running"))
        except Exception:                # noqa: BLE001
            alive = False
        if not alive:
            ok, msg = True, "引擎已退出，输入已释放"
            break
        try:
            res = sched.stop()
            if isinstance(res, tuple) and len(res) >= 2:
                ok, msg = bool(res[0]), str(res[1])
            else:
                ok = bool(res)
        except Exception:                # noqa: BLE001
            ok = False

    if ok:
        _log(f"退出前输入已释放（{msg}，{int((time.monotonic() - t0) * 1000)}ms）")
    else:
        _log(f"!! 退出时输入可能没释放干净：{msg}（继续退出，未按下的键请手动确认）")
    return ok


def _registry_path(exe_name):
    """从注册表 App Paths 读浏览器真实路径（Edge/Chrome 都写这里）。"""
    try:
        import winreg
    except Exception:
        return None
    keys = [
        rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
        rf"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
    ]
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                p = winreg.QueryValue(k, None)
            if p and os.path.isfile(p):
                return p
        except OSError:
            continue
    return None


def _find_browser():
    """定位本机 Edge/Chrome：`where` → 注册表 → 常见安装路径。"""
    import shutil
    for exe in ("msedge.exe", "chrome.exe"):
        p = shutil.which(exe)
        if p:
            return p
        p = _registry_path(exe)
        if p:
            return p
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for p in (
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
    ):
        if os.path.isfile(p):
            return p
    return None


def _open_app_window(url, width, height):
    """用 Edge/Chrome 的 `--app` 起一个独立窗口：无地址栏、无标签页。

    以前这里退回 `webbrowser.open()`，开出来的是浏览器**标签**，直接
    违反「必须独立窗口」的硬约束，而且失败还被吞掉，进程静默挂住占端口。
    """
    exe = _find_browser()
    if not exe:
        return None, "本机找不到 Edge/Chrome"
    import subprocess
    try:
        proc = subprocess.Popen([
            exe, f"--app={url}", f"--window-size={width},{height}",
            "--no-first-run", "--disable-features=Translate",
        ])
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    return proc, exe


def _license_activate_cli(args):
    """`--activate KEY`：兑换卡密后退出（不需要开窗口）。"""
    from core import license as licensing
    if not args.license_server:
        _log("!! 缺少 --license-server，无法兑换卡密")
        return 4
    try:
        d = licensing.activate(args.license_server, args.activate)
    except licensing.LicenseError as e:
        _log(f"!! 兑换失败：{e}")
        return 4
    _log(f"兑换结果：{d.status}（{d.reason}）lic={d.lic_id}")
    return 0 if d.ok else 4


def _license_gate(args):
    """启动门禁：没有有效许可（含试用）就不进主功能。

    返回 None = 放行；否则是退出码。提示必须是「可操作」的，不是崩溃。
    """
    from core import license as licensing
    try:
        d = licensing.ensure(server=(args.license_server or None))
    except Exception as e:                    # noqa: BLE001
        # 判定层任何意外都按「没有许可」处理，绝不闪退
        _log(f"!! 许可判定异常：{type(e).__name__}: {e}")
        d = licensing.Decision("none", False, f"许可判定异常：{type(e).__name__}")
    if d.ok:
        _log(f"许可门禁放行：{d.status} lic={d.lic_id} "
             f"tier={d.tier} 剩余={d.remaining_s}")
        return None

    port_hint = args.port
    msg = (
        "本机没有可用的许可，无法启动。\n\n"
        f"原因：{d.reason}\n\n"
        "激活方式（任选一种）：\n"
        "1) 命令行兑换：\n"
        "   SJZ-Control.exe --license-server http://<许可服务器> --activate <卡密>\n"
        "2) 把服务端签发的 license.json 放到：\n"
        f"   {licensing.app_dir()}\n"
        "3) 启动后在本机控制台调用 /api/license/activate\n"
        f"   http://127.0.0.1:{port_hint}/api/license\n"
    )
    _log("!! " + msg.replace("\n", " | "))
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, WINDOW_TITLE, 0x10)
    except Exception:
        pass
    return 3


def _fatal_no_window(url, why):
    msg = (f"打不开独立窗口：{why}\n\n"
           f"装了 pywebview 仍失败时，请确认本机有 Edge 或 Chrome。\n"
           f"控制台地址：{url}")
    _log("!! " + msg)
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, WINDOW_TITLE, 0x10)
    except Exception:
        pass
    return 2


def _wait_app(proc, thread, tick_s=0.5):
    """等独立窗口关闭（或服务线程结束），随后正常退出释放端口。"""
    while thread.is_alive() and proc.poll() is None:
        time.sleep(tick_s)


class WinCtl:
    """暴露给页面 JS 的窗口控制。

    无边框窗口没有系统标题栏，最小化/关闭得由页面自己触发，
    而 pywebview 不内置这些方法 —— 必须用 js_api 显式暴露。

    属性名**必须以下划线开头**：pywebview 的 `get_functions()` 会把 js_api
    对象的所有公开属性递归展开成 JS 可调用的东西（`webview/util.py`）。
    `self.win` 指向 pywebview 的 `Window`，它的对象图里有大量互相引用，
    递归展开会长时间占住 GIL —— 表现为**窗口能开、页面白屏、HTTP 全挂**
    （后台线程连 `Thread.start()` 都等不到 GIL）。改名 `_win` 即跳过展开。
    """

    def __init__(self):
        self._win = None
        self._overlay = None
        self._maxed = False

    # ---- 窗口三键（bridge 契约：win_minimize / win_maximize / win_close）
    def win_minimize(self):
        try:
            if self._win is not None:
                self._win.minimize()          # 最小化到任务栏，不是 hide
            return True
        except Exception as e:                # noqa: BLE001
            _log(f"!! win_minimize 失败：{type(e).__name__}: {e}")
            return False

    def win_maximize(self):
        """切换最大化/还原。pywebview 没有「是否最大化」的读接口，自己记状态。"""
        try:
            if self._win is None:
                return False
            if self._maxed:
                self._win.restore()
            else:
                self._win.maximize()
            self._maxed = not self._maxed
            _log(f"win_maximize -> {'还原' if not self._maxed else '最大化'}")
            return True
        except Exception as e:                # noqa: BLE001
            _log(f"!! win_maximize 失败：{type(e).__name__}: {e}")
            return False

    def win_close(self):
        """真退出：和 P0 的收尾同一份实现（放输入 → 放闸 → 硬退）。"""
        _log("win_close：用户点了关闭，收尾退出")
        threading.Thread(target=_hard_exit, args=(0,), daemon=True).start()
        return True

    # ---- 抢购 / 悬浮窗（契约见 inbound：purchase_stop 等）
    def purchase_stop(self):
        from core import server
        return server.purchase_stop()

    def purchase_start(self):
        from core import server
        return server.purchase_start()

    def overlay_toggle(self):
        try:
            ov = self._overlay
            if ov is None:
                return False
            if getattr(ov, "_shown_flag", True):
                ov.hide()
                ov._shown_flag = False
                return False
            # 走 SW_SHOWNOACTIVATE：pywebview 的 show() 会 Activate()，展开瞬间抢前台
            prev_fg = _get_foreground()
            _place_overlay(ov)
            _show_overlay_noactivate(ov)
            _restore_foreground(prev_fg, _find_hwnd(OVERLAY_TITLE, tries=20, delay=0.05))
            ov._shown_flag = True
            return True
        except Exception as e:                # noqa: BLE001
            _log(f"!! overlay_toggle 失败：{type(e).__name__}: {e}")
            return False

    # 按需提权：日常不提权，只有用户明确点了才弹 UAC。
    def restart_elevated(self):
        try:
            from core.input import elevate_self
        except Exception as e:                # noqa: BLE001
            _log(f"!! 提权入口不可用：{type(e).__name__}: {e}")
            return False
        # 提权重开前先放掉闸和热键：新进程一起来就要抢它们。
        _instance_teardown()
        ok = bool(elevate_self())
        if not ok:
            _single_instance()                # 没提起来就继续跑，把闸重新关上
            return False
        threading.Thread(target=_hard_exit, args=(0,), daemon=True).start()
        return True

    def get_overlay_state(self):
        from core import server
        return server.overlay_state()

    def overlay_size(self, expanded=False):
        """悬浮窗展开/收起时由页面调用：宿主负责改窗口高度。"""
        return _resize_overlay(bool(expanded))

    # 兼容旧页面
    def minimize(self):
        return self.win_minimize()

    def destroy(self):
        return self.win_close()


OVERLAY_TITLE = "SJZ 悬浮窗"
OVERLAY_W = 304
OVERLAY_H = 137
OVERLAY_H_EXP = 260

_OVERLAY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
html,body{margin:0;padding:0;background:rgba(13,17,23,0.80);color:#e6edf3;
 font:12px/1.5 "Microsoft YaHei",sans-serif;border-radius:8px;overflow:hidden}
#hd{display:flex;justify-content:space-between;align-items:center;
 padding:6px 10px;background:rgba(255,255,255,0.06);cursor:move;
 -webkit-user-select:none;user-select:none}
#st{font-weight:700}
#bd{padding:8px 10px}
.row{display:flex;justify-content:space-between;margin:1px 0}
.k{color:#8b949e}.v{color:#e6edf3}
#ev{color:#9da7b3;margin-top:4px;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis}
#err{color:#ffb3b3;margin-top:3px}
#ft{display:flex;gap:6px;padding:6px 10px}
button{flex:1;background:#21262d;color:#e6edf3;border:1px solid #30363d;
 border-radius:5px;padding:4px 0;cursor:pointer}
button:hover{background:#30363d}
</style></head><body>
<div id="hd"><span>抢购状态</span><span id="hk">F8</span></div>
<div id="bd">
 <div class="row"><span class="k">状态</span><span class="v" id="st">空闲</span></div>
 <div class="row"><span class="k">目标</span><span class="v" id="item">-</span></div>
 <div class="row"><span class="k">价格</span><span class="v" id="price">-</span></div>
 <div class="row"><span class="k">扫描/匹配/成交</span><span class="v" id="cnt">0 / 0 / 0</span></div>
 <div class="row"><span class="k">授权剩余</span><span class="v" id="lic">-</span></div>
 <div id="ev">-</div><div id="err"></div>
</div>
<div id="ft"><button onclick="window.pywebview.api.purchase_stop()">一键停止</button>
<button onclick="window.pywebview.api.overlay_toggle()">收起</button></div>
<script>
var M={idle:"空闲",scanning:"扫描中",firing:"抢购中",paused:"已暂停",error:"异常"};
var tick=function(){ if(!window.pywebview||!window.pywebview.api) return;
 var a=window.pywebview.api;
 a.get_overlay_state().then(function(s){
  document.getElementById("st").textContent=M[s.state]||s.state;
  document.getElementById("item").textContent=s.item||"-";
  document.getElementById("price").textContent=s.price||"-";
  document.getElementById("cnt").textContent=s.scanned+" / "+s.matched+" / "+s.fired;
  document.getElementById("lic").textContent=(s.license_remaining_s>0?
    Math.floor(s.license_remaining_s/86400)+" 天":"-");
  document.getElementById("hk").textContent=s.hotkey||"F8";
  document.getElementById("ev").textContent=s.last_event||"-";
  document.getElementById("err").textContent=(s.errors||[]).join("；");
 }).catch(function(){});};
setInterval(tick,1500);
window.addEventListener("pywebviewready",tick);
</script></body></html>"""


def _work_area():
    """工作区（不含任务栏），悬浮窗贴右上角时用。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        r = _RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                   ctypes.byref(r), 0)
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:                         # noqa: BLE001
        return 0, 0, 1920, 1080


def _place_overlay(ov=None):
    """贴右上角，留 16px 边距，不越界。"""
    try:
        x, y, aw, ah = _work_area()
        x = x + aw - OVERLAY_W - 16
        y = y + 16
        ov = ov or _OVERLAY_HANDLE.get("win")
        if ov is not None:
            ov.move(max(x, 0), max(y, 0))
            _log(f"悬浮窗已定位 ({max(x, 0)}, {max(y, 0)}) 工作区 {aw}x{ah}")
            return True
    except Exception as e:                    # noqa: BLE001
        _log(f"!! 悬浮窗定位失败：{type(e).__name__}: {e}")
    return False


_OVERLAY_HANDLE = {"win": None}


def _resize_overlay(expanded):
    """紧凑 ↔ 展开：只改高度，右边缘位置不变（JS 改不了 WebView 窗口尺寸）。"""
    ov = _OVERLAY_HANDLE.get("win")
    if ov is None:
        return False
    h = OVERLAY_H_EXP if expanded else OVERLAY_H
    try:
        ov.resize(OVERLAY_W, h)
    except Exception as e:                    # noqa: BLE001
        _log(f"!! 悬浮窗改尺寸失败：{type(e).__name__}: {e}")
        return False
    _place_overlay(ov)
    return True


SW_SHOWNOACTIVATE = 4
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_U32 = {"lib": None}


def _u32():
    """独立一份 user32 句柄：绝不能在 ctypes.windll.user32 上设 argtypes，
    那是全局共享对象，改完 pywebview 自己的 SetWindowPos/MoveWindow 就会
    参数类型不匹配（实测 `ov.move()` 直接抛 ArgumentError，窗口卡在默认位置）。"""
    if _U32["lib"] is None:
        lib = ctypes.WinDLL("user32")
        lib.FindWindowW.restype = ctypes.c_void_p
        lib.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        lib.GetWindowLongW.restype = ctypes.c_long
        lib.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.SetWindowLongW.restype = ctypes.c_long
        lib.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        lib.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_uint]
        lib.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GetForegroundWindow.restype = ctypes.c_void_p
        lib.GetForegroundWindow.argtypes = []
        lib.SetForegroundWindow.restype = ctypes.c_int
        lib.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        _U32["lib"] = lib
    return _U32["lib"]


def _get_foreground():
    try:
        return int(_u32().GetForegroundWindow() or 0)
    except Exception:                         # noqa: BLE001
        return 0


def _restore_foreground(prev, hwnd, seconds=4.0):
    """WebView2 初始化/首次导航会异步抢前台（真机实测：显示后 0.2s 内就变它）。

    这里只做一件事：短时间内盯住前台，一旦变成悬浮窗就还回去。
    不激活悬浮窗、不改变它的样式，失败也不影响其他逻辑。
    """
    if not prev or not hwnd or prev == hwnd:
        return

    def worker():
        end = time.time() + seconds
        while time.time() < end:
            try:
                if _get_foreground() == hwnd:
                    _u32().SetForegroundWindow(ctypes.c_void_p(prev))
            except Exception:                 # noqa: BLE001
                return
            time.sleep(0.1)

    threading.Thread(target=worker, daemon=True).start()


def _find_hwnd(title, tries=100, delay=0.02):
    """按标题找 hwnd（64 位句柄必须声明 restype，否则被截断成负数）。"""
    try:
        u = _u32()
        for _ in range(tries):
            hwnd = u.FindWindowW(None, title)
            if hwnd:
                return int(hwnd)
            time.sleep(delay)
    except Exception as e:                    # noqa: BLE001
        _log(f"!! 找悬浮窗句柄失败：{type(e).__name__}: {e}")
    return None


def _apply_noactivate(hwnd, topmost=True):
    """WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW：点它不抢焦点、不进任务栏。

    pywebview 没有这个开关，只能拿到 hwnd 后自己改扩展样式。
    **必须在第一次 Show 之前调用**：样式加上之后再显示，Show 才不会激活窗口。
    """
    try:
        u = _u32()
        h = ctypes.c_void_p(int(hwnd))
        style = u.GetWindowLongW(h, _GWL_EXSTYLE) or 0
        u.SetWindowLongW(h, _GWL_EXSTYLE,
                         style | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
        if topmost:
            # 只改 Z 序（TOPMOST）+ 不激活，不动位置尺寸
            u.SetWindowPos(h, ctypes.c_void_p(_HWND_TOPMOST & 0xFFFFFFFF), 0, 0, 0, 0,
                           _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
        return True
    except Exception as e:                    # noqa: BLE001
        _log(f"!! 悬浮窗样式设置失败：{type(e).__name__}: {e}")
    return False


def _show_overlay_noactivate(ov=None):
    """显示悬浮窗但不激活：直接 ShowWindow(SW_SHOWNOACTIVATE)。

    不能用 pywebview 的 `Window.show()` —— 它先 `Show()` 再 `Activate()`，
    展开瞬间会把前台抢走（真机实测：收起时前台是启动器，展开后变成悬浮窗）。
    """
    hwnd = _find_hwnd(OVERLAY_TITLE, tries=40, delay=0.02)
    if not hwnd:
        try:
            (ov or _OVERLAY_HANDLE.get("win")).show()
        except Exception as e:                # noqa: BLE001
            _log(f"!! 悬浮窗显示失败：{type(e).__name__}: {e}")
            return False
        return True
    _apply_noactivate(hwnd)
    try:
        _u32().ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
        _log("悬浮窗已显示（SW_SHOWNOACTIVATE，未激活）")
        return True
    except Exception as e:                    # noqa: BLE001
        _log(f"!! 悬浮窗显示失败：{type(e).__name__}: {e}")
        return False


def _overlay_page_path():
    """web/overlay.html 的实际路径：优先服务端静态目录（http 能取到的那一份）。"""
    dirs = []
    try:
        from core import server as _srv
        dirs.append(_srv.STATIC)
    except Exception:                         # noqa: BLE001
        pass
    dirs.extend([os.path.join(APPDIR, "web"), os.path.join(BASE, "web")])
    for d in dirs:
        p = os.path.join(d, "overlay.html")
        if os.path.isfile(p):
            return p
    return None


def _inline_overlay_js(html, page_path):
    """把 `<script src="overlay.js">` 换成就地内联。

    about:blank / data: 那种塞 HTML 的方式里，相对 src 解析不出来，
    外部脚本一个都不会执行（真机实测：conn 永远「未连接宿主」、热键「—」）。
    """
    js_path = os.path.join(os.path.dirname(page_path), "overlay.js")
    if not os.path.isfile(js_path):
        return html
    try:
        with open(js_path, "r", encoding="utf-8") as f:
            js = f.read()
    except OSError:
        return html
    js = js.replace("</script", "<\\/script")
    marker = re.search(r'<script[^>]*src="overlay\.js"[^>]*>\s*</script>', html)
    if not marker:
        return html.replace("</body>", f"<script>\n{js}\n</script>\n</body>")
    return html[:marker.start()] + f"<script>\n{js}\n</script>" + html[marker.end():]


def _http_page_ok(url, timeout=1.5):
    """宿主本地服务能不能取到这个页面。

    静态页面/脚本按前缀放行（不走令牌闸），正常情况这里是 200；探测只是
    兜底：服务没起/端口被占/资源缺失时才降级到 file:// 或内联。
    """
    try:
        from urllib.request import urlopen
        with urlopen(url, timeout=timeout) as r:
            return int(getattr(r, "status", 0) or 0) == 200
    except Exception:                         # noqa: BLE001
        return False


def _overlay_source(base_url):
    """返回 (mode, value)：'url'=真 http 地址，'file'=本地文件，'html'=内联页。

    优先级：http 真地址 → file:// → 内联（脚本事先生成到 HTML 里）。
    """
    page = _overlay_page_path()
    if page and base_url:
        url = base_url.rstrip("/") + "/overlay.html"
        if _http_page_ok(url):
            return ("url", url)
        _log("overlay.html 经 http 取不到（403/404），改走本地文件")
    if page:
        try:
            from urllib.request import pathname2url
            from urllib.parse import urljoin
            return ("file", urljoin("file:", pathname2url(page)))
        except Exception:                     # noqa: BLE001
            pass
    if page:
        try:
            with open(page, "r", encoding="utf-8") as f:
                html = f.read()
            _log(f"悬浮窗页面：内联 {page}")
            return ("html", _inline_overlay_js(html, page))
        except OSError:
            pass
    _log("悬浮窗页面：内置（web/overlay.html 缺失）")
    return ("html", _OVERLAY_HTML)


def _overlay_alive(ov, expect, tries=8, delay=0.4):
    """页面真的跑起来了没：脚本数 >0 且地址就是目标 URL。

    403/404 返回的是 JSON 体，location 对、scripts.length==0 —— 这时立即判定失败；
    页面还没加载好（about:blank / evaluate_js 抛错）就继续等，别误判。
    """
    for _ in range(tries):
        try:
            got = ov.evaluate_js("document.scripts.length + '|' + location.href")
        except Exception:                      # noqa: BLE001
            got = None
        if got:
            n, _, href = str(got).partition("|")
            try:
                n = int(n)
            except ValueError:
                n = 0
            if href.startswith(expect):
                return n > 0
        time.sleep(delay)
    return False


def _create_overlay(ctl, base_url=""):
    """建悬浮窗：无边框、置顶、半透明、可拖动、默认右上角，且**全程不激活**。"""
    prev_fg = _get_foreground()
    mode, src = _overlay_source(base_url)
    # file:// 也是 url，必须一起传；只认 'url' 会让窗口停在 about:blank
    src_url = src if mode in ("url", "file") else None
    src_html = src if mode == "html" else None
    kw = dict(width=OVERLAY_W, height=OVERLAY_H, frameless=True,
              easy_drag=True, on_top=True, background_color="#0d1117",
              js_api=ctl, hidden=True)
    ov = None
    try:
        ov = webview.create_window(
            OVERLAY_TITLE, url=src_url, html=src_html,
            transparent=True, focus=False, **kw)
    except TypeError:                          # 老版本没有 transparent/focus/hidden
        kw.pop("hidden", None)
        ov = webview.create_window(
            OVERLAY_TITLE, url=src_url, html=src_html, transparent=True, **kw)
    if ov is None:
        return None
    _OVERLAY_HANDLE["win"] = ov
    ctl._overlay = ov
    try:
        ov._shown_flag = True
    except Exception:                          # noqa: BLE001
        pass

    hwnd = _find_hwnd(OVERLAY_TITLE, tries=100, delay=0.02)
    if hwnd:
        _apply_noactivate(hwnd)
        _log(f"悬浮窗样式已就绪（NOACTIVATE+TOOLWINDOW）hwnd={hwnd}")
    # 真机实测：transparent 窗口初建时会被 MinimumSize 之类压成 288x100，
    # 建好后显式 resize 一次才是请求的 304x137
    try:
        ov.resize(OVERLAY_W, OVERLAY_H)
    except Exception as e:                     # noqa: BLE001
        _log(f"!! 悬浮窗初始尺寸设置失败：{type(e).__name__}: {e}")
    _place_overlay(ov)

    # http 被鉴权拦下（403）时页面里 0 个脚本：退回 file:// / 内联
    if mode == "url":
        if not _overlay_alive(ov, src):
            _log("!! overlay.html 经 http 没取到（可能 403），改用本地文件")
            mode, src = _overlay_source("")
            try:
                if mode == "file":
                    ov.load_url(src)
                else:
                    ov.load_html(src)
                time.sleep(0.6)
            except Exception as e:            # noqa: BLE001
                _log(f"!! 悬浮窗切换加载方式失败：{type(e).__name__}: {e}")

    _log(f"悬浮窗加载方式={mode}（{src[:80]}…）" if mode != "html"
         else "悬浮窗加载方式=html（脚本已内联）")
    _show_overlay_noactivate(ov)
    _restore_foreground(prev_fg, _find_hwnd(OVERLAY_TITLE, tries=20, delay=0.05))
    return ov


def _post_start_ui(ctl, base_url=""):
    """webview.start() 的回调：GUI 已在跑，这时候再建悬浮窗才安全。"""
    def run():
        time.sleep(1.0)
        try:
            _create_overlay(ctl, base_url)
        except Exception as e:                # noqa: BLE001
            _log(f"!! 悬浮窗创建失败（不影响主窗）：{type(e).__name__}: {e}")

    threading.Thread(target=run, name="overlay-boot", daemon=True).start()


def _start_window_watchdog():
    """兜底：主窗被系统关掉（Alt+F4 / WM_CLOSE）时，事件可能不触发，
    进程会攥着互斥量变成僵尸。窗口从「见过」到「连续 2 秒没有」就硬退。"""
    def loop():
        try:
            from core import single_instance as si
            seen = False
            for _ in range(60):
                time.sleep(0.5)
                if si.windows_for_pid(os.getpid())["visible"]:
                    seen = True
                    break
            if not seen:
                return
            miss = 0
            while True:
                time.sleep(1.0)
                try:
                    vis = si.windows_for_pid(os.getpid())["visible"]
                except Exception:              # noqa: BLE001
                    vis = 1
                if vis == 0:
                    miss += 1
                    if miss >= 2:
                        _log("主窗已消失（系统关闭/Alt+F4），强制收尾")
                        _hard_exit(0)
                else:
                    miss = 0
        except Exception as e:                 # noqa: BLE001
            _log(f"!! 窗口看门狗异常：{type(e).__name__}: {e}")

    threading.Thread(target=loop, name="window-watchdog", daemon=True).start()


def _doctor_templates(p, tdir):
    """自检里把「模板有没有、缺哪个、怎么补」说清楚（审查 R09-05）。"""
    try:
        names = sorted(n for n in os.listdir(tdir) if n.lower().endswith(".png"))
    except OSError:
        names = []
    p(f"模板文件      = {len(names)} 个：{', '.join(names) if names else '（空）'}")
    p(f"模板元数据    = {'有' if os.path.isfile(os.path.join(tdir, 'meta.json')) else '缺（匹配会退化为多尺度搜索）'}")
    if len(names) < 2:
        p("!! 缺模板：装上不能直接用。首次标定（任选一种）：")
        p("   1) SJZ-Control.exe --calibrate")
        p("   2) python tools/calibrate.py --workflow countdown")
        p("   依次截图并输入像素框，生成 templates/buy.png、confirm.png 与配置里的 regions；")
        p("   完整步骤见 _portability_fixes.md 第 4 节。")


def _run_calibrate(workflow=None):
    """`--calibrate`：调起首次标定向导（tools/calibrate.py），不需要开窗口。"""
    import importlib.util
    candidates = [os.path.join(APPDIR, "tools", "calibrate.py"),
                  os.path.join(BASE, "tools", "calibrate.py")]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        _log("!! 找不到 tools/calibrate.py，无法进入标定")
        return 5
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location("sjz_calibrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    argv = ["calibrate"]
    if workflow:
        argv += ["--workflow", str(workflow)]
    return int(mod.main(argv) or 0)


def _doctor():
    """打印运行时路径和关键依赖状态。

    打包后路径和开发时完全不同（单文件 exe 会解包到临时目录），
    出问题时必须能一眼看出配置会写到哪里。

    windowed exe 没有控制台，print 谁也看不见 —— 每条同时写进
    desktop.log，`SJZ-Control.exe --doctor` 才能在打包版上留证据。
    """
    from core import config, server

    def p(line):
        print(line)
        _log(line)

    p(f"frozen        = {getattr(sys, 'frozen', False)}")
    p(f"_MEIPASS      = {getattr(sys, '_MEIPASS', 'n/a')}")
    p(f"BASE          = {BASE}")
    p(f"sys.executable= {sys.executable}")
    p(f"cwd           = {os.getcwd()}")
    p(f"DPI awareness = {_DPI_AWARENESS} ({awareness_name(_DPI_AWARENESS) if _DPI_AWARENESS is not None else 'n/a'})")
    if _DPI_AWARENESS in (0, 1):
        p("!! DPI 感知不是 per-monitor：混合 DPI 下坐标会偏，检查是否被别的启动器先设过")
    p(f"config.json   = {config.CONFIG_PATH}")
    p(f"templates/    = {config.TEMPLATE_DIR}")
    p(f"data/         = {config.DATA_DIR}")
    p(f"web/ (STATIC) = {server.STATIC}")
    p(f"web 存在      = {os.path.isdir(server.STATIC)}")
    p(f"配置目录可写  = {_writable_dir(os.path.dirname(config.CONFIG_PATH))}"
      f"（实测写入临时文件）")
    _doctor_templates(p, config.TEMPLATE_DIR)
    try:
        from core import ocr
        p(f"OCR 后端      = {ocr.backend_name()}")
        p(f"OCR 通道      = {ocr.describe()}")
        p(f"OCR 模型目录  = {ocr.rapid_model_dir()}")
        p(f"OCR Rapid 错误= {ocr.rapid_error() or '（无）'}")
        p(f"OCR 可用      = {ocr.available()}")
    except Exception as e:
        p(f"OCR 自检失败  = {type(e).__name__} {e}")

    p("")
    try:
        from core import selfcheck
        for line in selfcheck.doctor_lines():
            p(line)
    except Exception as e:
        p(f"扩展自检失败  = {type(e).__name__} {e}")


def _elevate_if_needed():
    """游戏权限更高时**只提示，不自动提权**。

    以前这里会把自己以管理员身份重开一遍，这正是用户侧「双击没反应」
    的根源：UAC 被拒时旧进程已经放掉单实例闸并退出，新进程没起来，
    再双击又走同一套；提权成功时进程在高完整性下运行，普通权限的任务
    管理器 / taskkill 一律 Access denied，用户连自救都做不到。

    现在日常保持用户权限，只有用户显式点「以管理员重启」（或 --elevate）
    才提权。这里只把风险写进日志和界面，返回 False 表示「继续用当前权限跑」。
    """
    global _ELEVATE_NOTICE
    try:
        from core.input import needs_elevation, process_integrity
    except Exception:                         # noqa: BLE001
        return False
    # needs_elevation 只看**游戏窗口**的权限（core/input.py）。不能用
    # foreground_integrity：启动那一刻前台通常是终端或资源管理器，权限
    # 和我们一样，会判成「不用提权」，等切到游戏时已经晚了 —— UIPI 会把
    # 点击全吞掉，症状是「点了没反应」。
    try:
        if not needs_elevation():
            return False
    except Exception:                         # noqa: BLE001
        return False

    try:
        ours = process_integrity()
    except Exception:                         # noqa: BLE001
        ours = None
    _ELEVATE_NOTICE = (
        f"游戏窗口权限更高（本程序 {hex(ours) if ours is not None else '?'}）——"
        f"UIPI 会静默丢弃我们发的鼠标键盘消息；日常不提权，"
        f"需要点游戏时用界面「以管理员重启」或 --elevate 手动提权")
    os.environ["SJZ_ELEVATE_NOTICE"] = _ELEVATE_NOTICE
    _log("!! " + _ELEVATE_NOTICE)
    return False


def _no_window_requested():
    """自动化用：SJZ_NO_WINDOW=1 时只跑服务、不开窗口。

    打包验收和 CI 机器上没有可交互桌面，WebView2 建窗会把主线程卡在
    建窗里（GIL 被占，HTTP 线程跟着没响应）。产品默认永不设这个变量，
    这里只是给验收/排障留一个能真跑链路的入口。
    """
    return (os.environ.get("SJZ_NO_WINDOW") or "").strip().lower() in (
        "1", "true", "yes")


def _run_app(args):
    """许可通过后的主流程：起服务、算窗口尺寸、开窗、等关窗。"""
    _seed_missing_assets()
    _log(f"--- 启动 (frozen={getattr(sys, 'frozen', False)}) ---")
    _log(f"BASE={BASE}")
    _log(f"日志写到 {LOG_PATH}")
    _log(f"DPI awareness = {_DPI_AWARENESS} "
         f"({awareness_name(_DPI_AWARENESS) if _DPI_AWARENESS is not None else 'n/a'})")

    port = _pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    _log(f"选中端口 {port}")

    win_w, win_h, min_size = _fit_window(args.width, args.height)
    area = _primary_work_area()
    _log(f"窗口尺寸 {win_w}×{win_h}，min_size={min_size}，工作区={area}")

    # 服务必须在后台线程：pywebview 要求主线程归它，否则窗口起不来
    t = threading.Thread(target=serve,
                         args=(args.host, port, args.ctrl_token or None),
                         daemon=True)
    t.start()

    # 等端口真的可连再开窗口，否则 WebView 会先加载出一个「无法访问」
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex((args.host, port)) == 0:
                break
        time.sleep(0.05)
    else:
        _log(f"!! 服务没起来（{url}）")
        raise RuntimeError(f"服务没起来（{url}）")
    _log("服务已就绪，开始建窗口")

    if _no_window_requested():
        _log("SJZ_NO_WINDOW=1：不建窗口，只跑服务（自动化验收用；/api/ctrl/stop 可停）")
        while True:
            time.sleep(1)

    if webview is None:
        _log("没装 pywebview，改用 Edge/Chrome 独立应用窗口（--app）")
        proc, why = _open_app_window(url, win_w, win_h)
        if proc is None:
            return _fatal_no_window(url, why)
        _log(f"已用 {why} 打开独立窗口")
        _wait_app(proc, t)
        _log("窗口已关闭，退出")
        return 0

    ctl = WinCtl()
    shown = {"value": False}
    closed = {"value": False}

    def _on_shown(*_a):
        shown["value"] = True
        _log("窗口已显示")

    def _on_closing(*_a):
        # 关窗到 pywebview 的 closed 事件之间，WinForms 还要拆 WebView2
        # （实测 1.5–2 秒），这段时间进程还在、窗口已经没了。closing 事件
        # 在拆之前就发，这里直接走硬退，用户感知就是「点叉即退」。
        _log("窗口正在关闭，立即收尾")
        threading.Thread(target=_hard_exit, args=(0,), daemon=True).start()

    def _on_closed(*_a):
        closed["value"] = True
        _log("窗口已关闭，开始收尾")
        # 关窗就必须真退出：pywebview 的 closed 事件后主循环不保证返回，
        # 留在进程里就是「零窗口 + 握着互斥量」的僵尸。
        threading.Thread(target=_hard_exit, args=(0,), daemon=True).start()

    try:
        # 无边框：去掉系统标题栏和外圈边。
        # easy_drag=False —— 全页可拖会和框选拖拽打架，改成只让顶栏可拖
        # （页面里给顶栏加 .pywebview-drag-region，见 web/index.html）。
        # shadow=True 保留 Windows 的圆角和投影，否则窗口会像一块贴纸。
        win = webview.create_window(
            WINDOW_TITLE, url,
            width=win_w, height=win_h, min_size=min_size,
            frameless=True, easy_drag=False, shadow=True,
            background_color="#0d1117",
            js_api=ctl,
        )
        ctl._win = win
        try:
            win.events.shown += _on_shown
            win.events.closing += _on_closing
            win.events.closed += _on_closed
        except Exception:                 # 老版本 pywebview 没有 events，退回时间判断
            pass
        _start_window_watchdog()
        _log("create_window 返回")
        _log("调用 webview.start()")
        # 第二个窗口必须在 GUI 起来之后再建：start 之前连续 create_window
        # 会卡在 GUI 队列里等循环（实测主窗都不出来）。回调里交给后台线程。
        # 第一个参数传真实服务地址：悬浮窗走 /overlay.html，同源加载 overlay.js
        # private_mode=False 必须显式传：默认 True 时 pywebview 会在每个新
        # WebView2 控件初始化时 DeleteAllCookies()，悬浮窗一建好就把主窗的
        # 鉴权 cookie 清掉（首屏之后全 403，见文件头 WEBVIEW_PROFILE 注释）。
        _log(f"WebView2 profile={WEBVIEW_PROFILE}")
        try:
            webview.start(_post_start_ui, (ctl, url),
                          private_mode=False, storage_path=WEBVIEW_PROFILE)
        except TypeError:                 # 老版本 pywebview 没这两个参数
            _log("!! 当前 pywebview 不支持 private_mode/storage_path，保持默认")
            webview.start(_post_start_ui, (ctl, url))

        events = getattr(win, "events", None)
        closed_evt = getattr(events, "closed", None)
        if closed["value"] or (closed_evt is not None
                               and closed_evt.wait(0.5)):
            _log("窗口已关闭，退出")
            _hard_exit(0)

        if not shown["value"]:
            _log("!! webview.start() 在窗口显示前就返回了，窗口没能开起来")
            why = "WebView2 运行时缺失或初始化失败"
            proc, exe = _open_app_window(url, win_w, win_h)
            if proc is None:
                return _fatal_no_window(url, f"{why}；{exe}")
            _log(f"   改用 {exe} 独立应用窗口")
            _wait_app(proc, t)
            return 0

        _log("窗口已结束，退出")
        _hard_exit(0)
    except Exception as e:
        _log(f"!! 开窗失败: {type(e).__name__}: {e}")
        _log(traceback.format_exc())
        proc, exe = _open_app_window(url, win_w, win_h)
        if proc is None:
            return _fatal_no_window(url, f"pywebview 失败；{exe}")
        _log(f"   改用 {exe} 独立应用窗口")
        _wait_app(proc, t)
        return 0


def main():
    ap = argparse.ArgumentParser(description="SJZ 抢购控制台（桌面版）")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--doctor", action="store_true",
                    help="打印路径与依赖自检后退出（排查打包问题用）")
    # 提权重开时由 core.input.elevate_self 自动加上：只表示「这次别再提权」，
    # 对用户没有意义，所以不出现在 --help 里。有它才不会无限弹 UAC。
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--activate", metavar="KEY",
                    help="兑换卡密后退出（配合 --license-server）")
    ap.add_argument("--license-server",
                    default=os.environ.get("SJZ_LICENSE_SERVER", ""),
                    help="许可服务器地址（也可用 SJZ_LICENSE_SERVER）")
    ap.add_argument("--calibrate", nargs="?", const="", metavar="WORKFLOW",
                    help="进入首次标定向导后退出（可用值见 tools/calibrate.py）")
    # 以下两个由 SjzLauncher.exe 传入，用户不用手写：
    #   --launcher-pid  父进程守护（启动器一没，控制台安全停机）
    #   --ctrl-token    控制面 token（只走命令行，不落盘、不进日志）
    ap.add_argument("--launcher-pid", type=int, default=0,
                    help=argparse.SUPPRESS)
    ap.add_argument("--ctrl-token", default="", help=argparse.SUPPRESS)
    ap.add_argument("--no-window", action="store_true", help=argparse.SUPPRESS)
    # 同权限自我收尾：结束残留实例 + 清 config.json.lock + 放单实例闸。
    # 提权实例杀不掉时，SJZ-Control.exe --stop 也会失败（同权限原则）。
    ap.add_argument("--elevate", action="store_true",
                    help="以管理员身份重启（需要点高权限游戏时手动用）")
    ap.add_argument("--stop", action="store_true",
                    help="结束正在运行的实例并清理锁文件后退出")
    args = ap.parse_args()

    if args.no_window:
        os.environ["SJZ_NO_WINDOW"] = "1"

    # --stop 先于一切：不需要窗口、不需要许可、也不再抢闸
    if args.stop:
        return _stop_existing()

    # 先补随包资源：doctor/标定/正式启动看到的模板目录必须一致
    _seed_missing_assets()

    if args.calibrate is not None and args.calibrate != "":
        return _run_calibrate(args.calibrate)
    if args.calibrate == "":
        return _run_calibrate(None)

    if args.doctor:
        _doctor()
        return 0

    # 兑换不需要窗口/单实例闸，先处理掉
    if args.activate:
        return _license_activate_cli(args)

    # 父进程守护要在许可门禁之前起：门禁可能弹阻塞式消息框，
    # 启动器一旦退出，控制台不能挂在那儿等用户点确定。
    if args.launcher_pid:
        _log(f"父进程守护：launcher pid={args.launcher_pid}")
        _start_parent_watchdog(args.launcher_pid)
        if args.ctrl_token:
            _log("控制面 token 已接收（不写日志、不落盘）")
        else:
            _log("!! 启动器没传 --ctrl-token，控制面将不启用鉴权")

    # 单实例闸先于提权：已经有一个在跑就别再提权、别再起第二个。
    # （提权那条路里会临时放掉它，见 _elevate_if_needed）
    if not _single_instance():
        return 0

    # 日常运行不提权。游戏权限更高时只在日志/界面提示（见 _elevate_if_needed），
    # 需要点高权限游戏时由用户手动加 --elevate 或点界面「以管理员重启」。
    if args.elevate:
        try:
            from core.input import elevate_self
        except Exception as e:                # noqa: BLE001
            _log(f"!! 提权入口不可用：{type(e).__name__}: {e}")
        else:
            if elevate_self():
                _log("已请求管理员权限重启，本进程退出")
                return 0
            _log("!! 提权请求失败（UAC 被拒？）——继续以当前权限运行")
    elif _elevate_if_needed():
        return 0

    # 许可门禁必须在「起服务 / 装热键 / 调度器」之前：没有有效许可
    # 就不进入主功能（以前这一步缺失，复制 dist 即可直接使用，R10-L01）。
    blocked = _license_gate(args)
    if blocked is not None:
        return blocked

    try:
        return _run_app(args)
    finally:
        # 任何退出路径（关窗、开窗失败、异常）都要先确认输入释放，
        # 不能把左键/热键留在按下状态（审查 R09-08）。
        _release_input_before_exit()
        # 再放单实例闸 + 清登记：进程退出也不能留下占闸的僵尸。
        _instance_teardown()


if __name__ == "__main__":
    # pythonw 启动时没有控制台，出错只能靠日志 + 消息框 —— 否则就是「双击没反应」
    try:
        sys.exit(main() or 0)
    except Exception as e:
        _log(f"!! 启动失败: {type(e).__name__}: {e}")
        _log(traceback.format_exc())
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"启动失败：{type(e).__name__}: {e}\n\n详情见 {LOG_PATH}",
                WINDOW_TITLE, 0x10)
        except Exception:
            pass
        raise
