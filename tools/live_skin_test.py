"""实机 OCR 冒烟测试：读典藏外观列表、定位目标、滚动一屏再看。

为什么单独一个脚本
------------------
`pytest` 跑的是截图回放，证明不了「真机 + 真服务 + 真窗口」这条链。
这个脚本就是那条链的最小闭环，而且**只读 + 只滚轮，绝不点击** ——
不会买、不会卖、不会切页。

前置：游戏开在「典藏外观」页、OCR 服务起着（说明.txt 第九节）。
权限：游戏一般是管理员权限，本脚本也必须提权，否则滚轮会被 UIPI 静默
吞掉（列表纹丝不动，日志里什么也没有）。

用法
----
    .venv\\Scripts\\python.exe tools\\live_skin_test.py
    .venv\\Scripts\\python.exe tools\\live_skin_test.py --scroll 2 --json out.json

提权跑（游戏是管理员权限时必须这样）：
    Start-Process -Verb RunAs .venv\\Scripts\\pythonw.exe `
        -ArgumentList "tools\\live_skin_test.py","--json","out.json"
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.input import Keyboard, Mouse, find_game_window, integrity_name, \
    needs_elevation, process_integrity  # noqa: E402
from core.nav import Navigator  # noqa: E402
from core.screen import Screen  # noqa: E402


def run(scroll=0, target=None):
    out = {"ok": False, "steps": []}

    def step(name, **kw):
        kw["step"] = name
        out["steps"].append(kw)
        return kw

    s = Screen(1)
    hwnd = find_game_window()
    if not hwnd:
        out["error"] = "找不到游戏窗口（UnrealWindow）"
        return out
    out.update(screen=list(s.size), hwnd=hwnd,
               game_integrity=integrity_name(process_integrity(hwnd)),
               our_integrity=integrity_name(process_integrity()),
               elevated=not needs_elevation())

    m = Mouse(s, hwnd)
    kb = Keyboard(s, hwnd)
    if not m.ensure_foreground(hwnd):
        out["error"] = "抢不到游戏前台（可能被别的窗口压着）"
        return out
    time.sleep(0.6)

    nav = Navigator(s, m, kb, dry_run=True)

    t0 = time.perf_counter()
    items = nav.skin_items()
    step("skin_items", ms=round((time.perf_counter() - t0) * 1000),
         count=len(items), items=[{"text": t, "y": int(y)} for t, y in items])

    t0 = time.perf_counter()
    title = nav.skin_title()
    step("skin_title", ms=round((time.perf_counter() - t0) * 1000), text=title)

    if target:
        hit = nav.locate_skin_card(target)
        step("locate", target=target,
             found=bool(hit),
             text=hit[0] if hit else None,
             y=int(hit[1]) if hit else None,
             score=round(hit[2], 3) if hit else None)

    if scroll:
        before = [t for t, _ in items]
        t0 = time.perf_counter()
        nav.skin_scroll(scroll)
        time.sleep(0.9)
        after_items = nav.skin_items()
        after = [t for t, _ in after_items]
        step("scroll", notches=scroll,
             ms=round((time.perf_counter() - t0) * 1000),
             changed=before != after,
             before=before[:4], after=after[:4],
             after_items=[{"text": t, "y": int(y)} for t, y in after_items])
        # 滚完再定位一次：这才是「滚动找皮肤」的真实场景 ——
        # 目标往往一开始不在屏上，滚过去之后才出现
        if target:
            hit = nav.locate_skin_card(target)
            step("locate_after_scroll", target=target, found=bool(hit),
                 text=hit[0] if hit else None,
                 y=int(hit[1]) if hit else None,
                 score=round(hit[2], 3) if hit else None)
        nav.skin_scroll_top()
        time.sleep(0.6)
        step("scroll_back", count=len(nav.skin_items()))

    out["ok"] = True
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scroll", type=int, default=0, help="滚动几格（正数向下）")
    ap.add_argument("--target", default=None, help="要定位的皮肤名")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    res = run(scroll=args.scroll, target=args.target)
    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
