"""跨进程用例的子进程：注册一个真 HookHotkey，然后停在那里等指令。

协议（stdout 一行 JSON，ASCII 转义，避免控制台编码差异）：
  启动后 {"ok": bool, "msg": str, "pid": int, "mutex": str}
  收到 stdin 的 "stop\n" 或 EOF 后调用 stop()，再打 {"stopped": true} 退出。

父进程要模拟「被强杀」时直接 terminate()，不要发 stop。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import hotkey  # noqa: E402


def main():
    spec = sys.argv[1]
    mods, vk = hotkey.parse(spec)
    hk = hotkey.HookHotkey(spec)
    ok, msg = hk.start()
    print(json.dumps({"ok": ok, "msg": msg, "pid": os.getpid(),
                      "mutex": hotkey._mutex_name(mods, vk)}), flush=True)

    line = sys.stdin.readline()          # 阻塞直到父进程发指令或关 stdin
    hk.stop()
    print(json.dumps({"stopped": True, "cmd": line.strip()}), flush=True)


if __name__ == "__main__":
    main()
