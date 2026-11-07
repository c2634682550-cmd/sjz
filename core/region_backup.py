"""自动标定写入前的 regions 快照、读回校验与回滚。

`calibration.apply_auto` 默认 True，标定结果会直接写进用户的 `regions`。
一旦某次标定算错，用户原来的坐标就被覆盖且无从找回 —— 所以每次写入前
先把整段 `regions`（以及 `calibration.applied`）快照到**用户数据目录**：

    Windows: %LOCALAPPDATA%\\dafors\\region_backups\\
    其它:    ~/.dafors/region_backups/
    `DAFORS_USER_DIR` 可覆盖（便携版/测试用）

仓库/程序目录里不放备份。文件名带毫秒时间戳，保留最近 `KEEP` 份。

写后必须**读回校验**：重新 `Config.load()` 确认写入值确实等于提案值；
不一致（被外部改动、写盘被截断）就整体回滚到快照，并把本次标定判为拦截。

一键回滚入口：`tools/rollback_regions.py`（见该脚本 `--help`）。
"""
import json
import os
import re
import time

from . import config as _cfgmod
from . import region_table

KEEP = 30
_NAME_RE = re.compile(r"^regions-\d{8}_\d{6}_\d{3}(?:-\d+)?\.json$")


def backup_dir():
    return os.path.join(_cfgmod.user_dir(), "region_backups")


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S") + "_%03d" % (
        int(time.time() * 1000) % 1000)


def _same(a, b, tol=1e-6):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    try:
        return all(abs(float(a[k]) - float(b[k])) <= tol
                   for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return False


def snapshot_data(cfg):
    """当前配置里要备份的两段：regions + calibration.applied。"""
    cal = cfg.get("calibration") if isinstance(cfg.get("calibration"), dict) else {}
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "regions": json.loads(json.dumps(cfg.get("regions") or {})),
        "applied": json.loads(json.dumps(cal.get("applied") or {})),
    }


def save_snapshot(cfg, reason="auto-calibration"):
    """写前快照。成功返回文件路径；失败返回空串 —— 调用方据此拒绝写入。"""
    snap = snapshot_data(cfg)
    snap["reason"] = reason
    d = backup_dir()
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "regions-%s.json" % _stamp())
        n = 0
        while os.path.exists(path) and n < 100:
            n += 1
            path = os.path.join(d, "regions-%s-%d.json" % (_stamp(), n))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            if "tmp" in locals():
                os.remove(tmp)
        except OSError:
            pass
        return ""
    prune()
    return path


def list_backups(limit=None):
    """按时间倒序列出快照路径。"""
    try:
        names = [n for n in os.listdir(backup_dir()) if _NAME_RE.match(n)]
    except OSError:
        return []
    names.sort(reverse=True)
    paths = [os.path.join(backup_dir(), n) for n in names]
    return paths[:limit] if limit else paths


def latest():
    got = list_backups(1)
    return got[0] if got else ""


def load(path):
    """读快照并校验基本结构；坏文件返回 None（不能拿半份快照去回滚）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("regions"), dict):
        return None
    if not data["regions"]:
        return None
    return data


def verify_applied(applied, load_cfg=None):
    """重新读盘，确认写入值确实等于提案值。

    返回第一个不一致的区域名；全部一致返回 None。读盘失败也算不一致
    （不能因为读不出来就假定写成功了）。
    """
    if not applied:
        return None
    load_cfg = load_cfg or _cfgmod.Config.load
    try:
        again = load_cfg()
    except Exception:                            # noqa: BLE001
        return next(iter(applied))
    for name, box in applied.items():
        if not _same(again.region_raw(name), box):
            return name
    return None


def restore(cfg, path):
    """把快照写回 `cfg` 并落盘；返回备份路径，无快照/写失败也返回路径。

    只恢复 `regions` 与 `calibration.applied`，不碰队列/设置 —— 回滚的
    语义是「把按钮坐标退回到上一次可用的那套」。
    """
    snap = load(path) if path else None
    if snap is None:
        return ""
    regions = {n: {k: float(v[k]) for k in ("x", "y", "w", "h")}
               for n, v in snap["regions"].items()
               if n in region_table.BUILTIN
               and isinstance(v, dict)
               and all(k in v for k in ("x", "y", "w", "h"))}
    if not regions:
        return ""
    cfg.data["regions"] = regions
    cal = cfg.get("calibration")
    if not isinstance(cal, dict):
        cal = {}
        cfg.data["calibration"] = cal
    cal["applied"] = json.loads(json.dumps(snap.get("applied") or {}))
    cfg._dirty.add("regions")
    cfg._dirty.add("calibration")
    try:
        cfg.save()
    except Exception:                            # noqa: BLE001
        pass
    return path


def prune(keep=KEEP):
    """只留最近 keep 份，避免用户目录无限增长。"""
    for p in list_backups()[keep:]:
        try:
            os.remove(p)
        except OSError:
            pass
