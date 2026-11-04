"""启动入口：python run.py [--port 8777] [--no-browser]"""
import argparse
import sys
import threading
import webbrowser

from core.server import serve


def main():
    ap = argparse.ArgumentParser(description="SJZ 抢购控制台")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    # 提权重开时由 core.input.elevate_self 自动加上。它只表示「这次别再
    # 提权了」，对用户没有意义，所以不出现在 --help 里。
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    # 和桌面版共用同一把单实例闸：源码跑一个、exe 又开一个，等于两个
    # 调度器抢同一个鼠标（会重复下单）。
    from core.single_instance import acquire, activate_window, release
    if not acquire():
        activate_window("SJZ 抢购控制台")
        print("已经有一个控制台在跑（开发时要多开请设 SJZ_ALLOW_MULTI=1）")
        return 1

    # 游戏以管理员权限跑的时候，普通权限的我们**点不动它**（UIPI 会把鼠标
    # 键盘消息全拦掉且不报错）—— 这时候直接以管理员身份重开，省得用户对着
    # 一个「点了没反应」的控制台发呆。
    #
    # 闸只在**真要提权**的时候才放。以前这里无条件 release() 再 acquire()：
    #  · 中间那段空窗期谁都能抢到闸，两个控制台就一起跑起来了（实测
    #    8778 / 8779 两个进程同时在监听，两个调度器抢同一个鼠标）；
    #  · 重新 acquire() 的返回值没人看，抢不到也照跑。
    held = True
    try:
        from core.input import elevate_self, needs_elevation
        if needs_elevation():
            release()                   # 子进程要抢闸，先放掉
            held = False
            if elevate_self():
                print("游戏是管理员权限，已用管理员身份重新启动本程序")
                return 0
            held = acquire()            # 没提起来，把闸抢回来
            if not held:
                activate_window("SJZ 抢购控制台")
                print("已经有一个控制台在跑（开发时要多开请设 SJZ_ALLOW_MULTI=1）")
                return 1
    except Exception:
        # 判断/提权本身出错不该拦住正常启动，但闸必须还在手上
        if not held:
            acquire()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
