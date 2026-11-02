"""启动入口：python run.py [--port 8777] [--no-browser]"""
import argparse
import threading
import webbrowser

from core.server import serve


def main():
    ap = argparse.ArgumentParser(description="SJZ 抢购控制台")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    # 游戏以管理员权限跑的时候，普通权限的我们**点不动它**（UIPI 会把鼠标
    # 键盘消息全拦掉且不报错）—— 这时候直接以管理员身份重开，省得用户对着
    # 一个「点了没反应」的控制台发呆。
    try:
        from core.input import elevate_self
        if elevate_self():
            print("游戏是管理员权限，已用管理员身份重新启动本程序")
            return
    except Exception:
        pass
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
