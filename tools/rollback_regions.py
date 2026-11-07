"""一键回滚自动标定写入的区域坐标。

自动标定写入前会把整段 `regions` 快照到用户数据目录
（`core/region_backup.py`）。本脚本列出/恢复这些快照。

    python tools/rollback_regions.py --list
    python tools/rollback_regions.py                  # 回滚最近一次
    python tools/rollback_regions.py --at 20261107_213045_123

回滚只动 `regions` 与 `calibration.applied`，不动队列/设置；写回后会
重新读盘核对，确认恢复值等于快照值。
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import config as cfgmod          # noqa: E402
from core import region_backup as RB       # noqa: E402
from core import region_table as RT        # noqa: E402


def _fmt(box):
    if not isinstance(box, dict):
        return "—"
    return ("x=%.6f y=%.6f w=%.6f h=%.6f"
            % (box.get("x", 0), box.get("y", 0),
               box.get("w", 0), box.get("h", 0)))


def _changed(cur, snap):
    out = []
    for name in RT.BUILTIN:
        a, b = cur.get(name) or {}, snap.get(name) or {}
        if any(abs(float(a.get(k, 0)) - float(b.get(k, 0))) > 1e-9
               for k in ("x", "y", "w", "h")):
            out.append(name)
    return out


def _pick(stamp):
    if not stamp:
        return RB.latest()
    for path in RB.list_backups():
        if stamp in os.path.basename(path):
            return path
    return ""


def _verify(snap):
    again = cfgmod.Config.load()
    for name, want in snap["regions"].items():
        if name not in RT.BUILTIN:
            continue
        got = again.region_raw(name) or {}
        if any(abs(float(got.get(k, 0)) - float(want.get(k, 0))) > 1e-9
               for k in ("x", "y", "w", "h")):
            return name
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="回滚自动标定写入的区域坐标")
    ap.add_argument("--list", action="store_true", help="只列快照，不回滚")
    ap.add_argument("--at", default="",
                    help="快照文件名里的时间戳片段，如 20261107_213045_123")
    ap.add_argument("--config", default="",
                    help="要恢复的 config.json（默认程序目录那份）")
    args = ap.parse_args(argv)

    if args.config:
        cfgmod.CONFIG_PATH = os.path.abspath(args.config)

    paths = RB.list_backups()
    if args.list:
        if not paths:
            print("没有快照：" + RB.backup_dir())
            return 1
        print("快照目录：" + RB.backup_dir())
        for p in paths:
            snap = RB.load(p) or {}
            print("  %-34s %s  %s" % (os.path.basename(p),
                                      snap.get("at", "?"),
                                      snap.get("reason", "")))
        return 0

    path = _pick(args.at)
    if not path:
        print("找不到快照（%s）" % (args.at or "最近一次") + "：" + RB.backup_dir())
        return 1
    snap = RB.load(path)
    if snap is None:
        print("快照文件损坏，已拒绝回滚：" + path)
        return 1

    cfg = cfgmod.Config.load()
    cur = cfg.get("regions") or {}
    changed = _changed(cur, snap["regions"])
    print("回滚到：" + path)
    print("快照时间：%s  原因：%s" % (snap.get("at", "?"), snap.get("reason", "")))
    if not changed:
        print("当前区域坐标与快照一致，无需改动。")
        return 0
    for name in changed:
        print("  %-8s 当前 %s" % (RT.LABELS.get(name, name), _fmt(cur.get(name))))
        print("  %-8s 恢复 %s" % ("", _fmt(snap["regions"].get(name))))

    RB.restore(cfg, path)
    bad = _verify(snap)
    if bad:
        print("回滚后读回不一致：%s（请检查 config.json 是否被其它进程占用）"
              % RT.LABELS.get(bad, bad))
        return 1
    print("回滚完成：%d 个区域已恢复，读回核对一致。" % len(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
