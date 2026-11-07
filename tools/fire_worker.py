"""极简卡点进程（分支 B 结论落地）。

只做一件事：读一行 `target`（`time.perf_counter()` 秒），自旋到该时刻，
输出实际触发时刻。**不 import 任何项目模块**，不开线程池，不做日志
格式化 —— 目的就是让这个进程的 GIL 上没有任何别的活，避免主进程满载
时的调度抖动传染过来。
"""

import sys
import time


def main():
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            target = float(line)
        except ValueError:
            out.write("ERR\n")          # 主进程会解析失败 -> 走降级
            out.flush()
            continue
        while True:
            if time.perf_counter() >= target:
                break
        out.write(f"{time.perf_counter():.9f}\n")
        out.flush()


if __name__ == "__main__":
    main()
