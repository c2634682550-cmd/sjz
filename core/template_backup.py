"""模板快照：自愈写盘前先把 `templates/` 里的 PNG + meta.json 存一份。

为什么单独一个文件而不是塞进 `region_backup.py`：那是「配置（regions/
calibration）」的快照，这是「素材（templates/）」的快照，两者的回滚入口
和保留策略都不一样（素材是二进制，快照要成目录存）。混在一起的后果是
`restore()` 分不清自己在恢复坐标还是恢复图。

放用户数据目录（`config.user_dir()/template_backups/<时间戳>/`），
不进 git、不随程序目录被清空；失败宁可不写（调用方据此拒绝覆盖模板）。
"""
import json
import os
import re
import shutil
import time

from . import config as _cfgmod

KEEP = 10
_NAME_RE = re.compile(r"^\d{8}_\d{6}_\d{3}(?:-\d+)?$")


def backup_dir():
    return os.path.join(_cfgmod.user_dir(), "template_backups")


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S") + "_%03d" % (
        int(time.time() * 1000) % 1000)


def save(paths, reason=""):
    """把若干文件复制进一个快照目录。成功返回目录路径，失败返回空串。

    `paths` 里的文件不存在就跳过；一个都没复制成就当失败返回空串。
    """
    files = [p for p in (paths or []) if p and os.path.isfile(p)]
    if not files:
        return ""
    root = backup_dir()
    try:
        os.makedirs(root, exist_ok=True)
        target = os.path.join(root, _stamp())
        n = 0
        while os.path.isdir(target) and n < 100:
            n += 1
            target = os.path.join(root, _stamp() + "-%d" % n)
        os.makedirs(target)
        copied = []
        for p in files:
            dst = os.path.join(target, os.path.basename(p))
            shutil.copy2(p, dst)
            copied.append(os.path.basename(p))
        with open(os.path.join(target, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "reason": reason, "files": copied}, fh,
                      ensure_ascii=False, indent=2)
    except OSError:
        return ""
    prune()
    return target


def list_backups(limit=None):
    """按时间倒序列出快照目录。"""
    root = backup_dir()
    try:
        names = [n for n in os.listdir(root)
                 if _NAME_RE.match(n) and os.path.isdir(os.path.join(root, n))]
    except OSError:
        return []
    names.sort(reverse=True)
    return [os.path.join(root, n) for n in names[:limit]] if limit else \
        [os.path.join(root, n) for n in names]


def latest():
    hits = list_backups(1)
    return hits[0] if hits else ""


def restore(snapshot, dest_dir, names=None):
    """把快照里的文件复制回 `dest_dir`。返回实际恢复的文件名列表。

    `names` 只认列表里的文件（例如只回滚 buy.png 而不动 meta.json）；
    不传就整份恢复。缺文件直接跳过，不报错。
    """
    out = []
    if not snapshot or not os.path.isdir(snapshot):
        return out
    for name in sorted(os.listdir(snapshot)):
        if name == "manifest.json":
            continue
        if names and name not in names:
            continue
        src = os.path.join(snapshot, name)
        if not os.path.isfile(src):
            continue
        try:
            shutil.copy2(src, os.path.join(dest_dir, name))
            out.append(name)
        except OSError:
            continue
    return out


def prune(keep=KEEP):
    """只留最近 keep 份，其余删掉。快照是二进制，攒多了占地方。"""
    for path in list_backups()[keep:]:
        try:
            shutil.rmtree(path)
        except OSError:
            pass
