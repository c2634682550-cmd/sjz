"""桌面版入口：原生窗口内嵌控制台，不打开浏览器。

和 run.py 的分工：
  run.py      浏览器模式，开发时用（改完前端刷新即可，不用重启）
  desktop.py  桌面模式，打包成 exe 给最终用户

两者共用 core.server 的全部端点。
"""
import argparse
import os
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

import webview  # noqa: E402

from core.server import serve  # noqa: E402

# 无控制台的 windowed exe 里 print 什么也看不见，所以出问题只能靠日志文件。
# 窗口没起来是最难查的一类故障（进程在、端口通、就是没界面）。
LOG_PATH = os.path.join(APPDIR, "desktop.log")


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


class WinCtl:
    """暴露给页面 JS 的窗口控制。

    无边框窗口没有系统标题栏，最小化/关闭得由页面自己触发，
    而 pywebview 不内置这些方法 —— 必须用 js_api 显式暴露。
    """

    def __init__(self):
        self.win = None

    def minimize(self):
        if self.win is not None:
            self.win.minimize()

    def destroy(self):
        if self.win is not None:
            self.win.destroy()


def _doctor():
    """打印运行时路径和关键依赖状态。

    打包后路径和开发时完全不同（单文件 exe 会解包到临时目录），
    出问题时必须能一眼看出配置会写到哪里。
    """
    from core import config, server
    print("frozen        =", getattr(sys, "frozen", False))
    print("_MEIPASS      =", getattr(sys, "_MEIPASS", "n/a"))
    print("BASE          =", BASE)
    print("sys.executable=", sys.executable)
    print("cwd           =", os.getcwd())
    print("config.json   =", config.CONFIG_PATH)
    print("templates/    =", config.TEMPLATE_DIR)
    print("data/         =", config.DATA_DIR)
    print("web/ (STATIC) =", server.STATIC)
    print("web 存在      =", os.path.isdir(server.STATIC))
    print("配置目录可写  =", os.access(os.path.dirname(config.CONFIG_PATH), os.W_OK))
    try:
        from core import ocr
        print("OCR 后端      =", ocr.backend_name())
    except Exception as e:
        print("OCR 自检失败  =", type(e).__name__, e)


def main():
    ap = argparse.ArgumentParser(description="SJZ 抢购控制台（桌面版）")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--doctor", action="store_true",
                    help="打印路径与依赖自检后退出（排查打包问题用）")
    args = ap.parse_args()

    if args.doctor:
        _doctor()
        return

    _log(f"--- 启动 (frozen={getattr(sys, 'frozen', False)}) ---")
    _log(f"BASE={BASE}")
    _log(f"日志写到 {LOG_PATH}")

    port = _pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    _log(f"选中端口 {port}")

    # 服务必须在后台线程：pywebview 要求主线程归它，否则窗口起不来
    t = threading.Thread(target=serve, args=(args.host, port), daemon=True)
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

    ctl = WinCtl()
    try:
        # 无边框：去掉系统标题栏和外圈边。
        # easy_drag=False —— 全页可拖会和框选拖拽打架，改成只让顶栏可拖
        # （页面里给顶栏加 .pywebview-drag-region，见 web/index.html）。
        # shadow=True 保留 Windows 的圆角和投影，否则窗口会像一块贴纸。
        win = webview.create_window(
            "SJZ 抢购控制台", url,
            width=args.width, height=args.height, min_size=(900, 620),
            frameless=True, easy_drag=False, shadow=True,
            background_color="#0d1117",
            js_api=ctl,
        )
        ctl.win = win
        _log("create_window 返回")
        _log("调用 webview.start()")
        webview.start()
        # 冻结环境里如果 WebView2 初始化失败，start() 会立刻返回而不是
        # 阻塞到窗口关闭 —— 那样进程会静默退出，用户看到双击后什么都没发生。
        # 这里显式报出来，别让它变成「点了没反应」。
        _log("!! webview.start() 立刻返回了，窗口没能开起来")
        _log("   多半是 WebView2 运行时缺失或初始化失败")
    except Exception as e:
        _log(f"!! 开窗失败: {type(e).__name__}: {e}")
        _log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
