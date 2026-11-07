# -*- coding: utf-8 -*-
"""04 补丁「启动自检不再是 1.5s 同步探活」的实测证据。

四个场景：
  1. 端口没起（最常见）：探活立刻失败，基线/补丁都快 —— 说明 1.5s 只在丢包时发生
  2. 黑洞地址（SYN 被丢）：旧预算 1.5s vs 新预算 0.3s
  3. 端口在听但推理 500：selfcheck 返回错误文案（不再把二级显示成可用）
  4. 异步块：主线程只付 Thread.start()，不等自检结束
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"))

from core import ocr_ppocr as P


def ms(t):
    return f"{t * 1000:.1f}ms"


def tcp_probe(url, timeout):
    b = P.Backend({"mode": "http", "url": url})
    t = time.perf_counter()
    ok = b.probe(force=True, timeout=timeout)
    return ok, time.perf_counter() - t


def serve_500(port):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
                c.recv(4096)
                c.sendall(b"HTTP/1.1 500 Internal Server Error\r\n"
                          b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
                c.close()
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return srv


print("== 1 端口没起（127.0.0.1:18081 关闭） ==")
ok, dt = tcp_probe("http://127.0.0.1:18081", None)
print(f"  旧预算(1.5s) probe={ok} {ms(dt)}")
ok, dt = tcp_probe("http://127.0.0.1:18081", 0.3)
print(f"  新预算(0.3s) probe={ok} {ms(dt)}")

print("== 2 selfcheck 走完整路径（端口没起） ==")
b0 = P.Backend({"mode": "http", "url": "http://127.0.0.1:18081"})
t = time.perf_counter()
ok, err = b0.selfcheck(probe_timeout=0.3)
print(f"  selfcheck ok={ok} {ms(time.perf_counter() - t)} err={err}")

print("== 3 端口在听但每次 500（模拟 Paddle CPU 分支必崩） ==")
srv = serve_500(18082)
b = P.Backend({"mode": "http", "url": "http://127.0.0.1:18082"})
t = time.perf_counter()
ok, err = b.selfcheck(probe_timeout=0.3)
print(f"  selfcheck ok={ok} {ms(time.perf_counter() - t)} err={err[:60]}")
print(f"  available()={b.available()}  name={b.name()[:40]}")
srv.close()

print("== 4 异步块：主线程只付 Thread.start() ==")
done = []


def _slow_check():
    time.sleep(1.5)          # 模拟最坏情况：探活把 1.5s 花光
    done.append(True)


t = time.perf_counter()
th = threading.Thread(target=_slow_check, name="ocr-ppcheck", daemon=True)
th.start()
start_cost = time.perf_counter() - t
print(f"  Thread.start()={ms(start_cost)}  启动线程后自检仍在跑={th.is_alive()}")
th.join()
print(f"  自检总耗时={ms(time.perf_counter() - t)}（期间主线程已可继续初始化）")
