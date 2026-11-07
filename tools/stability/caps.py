"""资源上限验证：台账轮转 / 截图修剪 / 日志轮转。

长跑只证明「72h 等效后文件多大」，这里证明「上限真的会触发」——
把阈值临时调小，灌进超过上限的数据，看当前文件、归档、总占用是否被压住。
验证完把阈值还原，不污染全局常量。

    python -m tools.stability.caps [--out DIR]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from tools.stability import fakes, workload  # noqa: E402
from core import records, retention  # noqa: E402


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def check_records(ws, line_bytes, target_bytes, max_bytes=16384, keep=3):
    """台账：写超过上限的数据，确认当前文件被轮转、归档数受 keep 约束。"""
    old = (retention.RECORDS_MAX_BYTES, retention.RECORDS_KEEP)
    retention.RECORDS_MAX_BYTES, retention.RECORDS_KEEP = max_bytes, keep
    records.clear()
    try:
        n = 0
        while _size(records.PATH) + sum(_size(p) for p in retention.archives(
                records.PATH, 99)) < target_bytes and n < 50000:
            records.add(name="cap-%d" % n, price=100, result="hit",
                        elapsed=0.5, extra={"pad": "x" * line_bytes})
            n += 1
        live = _size(records.PATH)
        arch = [p for p in retention.archives(records.PATH, 99)]
        total = live + sum(_size(p) for p in arch)
        kept = [p for p in retention.archives(records.PATH, keep)]
        res = {
            "writes": n, "max_bytes": max_bytes, "keep": keep,
            "live_bytes": live, "archives": len(arch),
            "archives_kept": len(kept), "total_bytes": total,
            "ok_live": live <= max_bytes + line_bytes * 4,
            "ok_keep": len(kept) <= keep,
            "ok_total": total <= max_bytes * (keep + 1) + line_bytes * 4,
            "rows_after": len(records.load(0)),
        }
        return res
    finally:
        retention.RECORDS_MAX_BYTES, retention.RECORDS_KEEP = old


def check_shots(ws, max_files=64, max_bytes=1 << 20):
    old = (retention.SHOTS_MAX_FILES, retention.SHOTS_MAX_BYTES)
    retention.SHOTS_MAX_FILES, retention.SHOTS_MAX_BYTES = max_files, max_bytes
    d = ws.shots
    try:
        for i in range(160):
            with open(os.path.join(d, "c%03d.jpg" % i), "wb") as f:
                f.write(b"x" * 4096)
            # 用真实调用路径（引擎里就是 prune_dir），不是直接删
            retention.prune_dir(d, retention.SHOTS_MAX_FILES, retention.SHOTS_MAX_BYTES)
        files = sorted(os.listdir(d))
        used = sum(_size(os.path.join(d, p)) for p in files)
        res = {"max_files": max_files, "max_bytes": max_bytes,
               "files_after": len(files), "bytes_after": used,
               "ok_files": len(files) <= max_files, "ok_bytes": used <= max_bytes}
        return res
    finally:
        retention.SHOTS_MAX_FILES, retention.SHOTS_MAX_BYTES = old
        for p in os.listdir(d):
            os.remove(os.path.join(d, p))


def check_log(ws, max_bytes=8192, total=40000):
    old = retention.LOG_MAX_BYTES
    retention.LOG_MAX_BYTES = max_bytes
    p = os.path.join(ws.data, "ocr_service.log")
    try:
        with open(p, "wb") as f:
            f.write(b"line\n" * (total // 5))
        before = _size(p)
        rotated = retention.rotate_log(p, max_bytes)
        after = _size(p)
        arch = retention.archives(p, 99)
        res = {"max_bytes": max_bytes, "before_bytes": before,
               "after_bytes": after, "rotated": bool(rotated),
               "archives": len(arch),
               "ok_rotated": bool(rotated), "ok_after": after <= max_bytes,
               "ok_archive": len(arch) <= 1}
        return res
    finally:
        retention.LOG_MAX_BYTES = old
        for q in [p] + retention.archives(p, 99):
            try:
                os.remove(q)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "_results", "caps_%s" % time.strftime("%Y%m%d_%H%M%S"))
    ws = fakes.TempWorkspace("caps")
    workload.apply_workspace(ws)
    res = {
        "records": check_records(ws, 200, 96 * 1024),
        "shots": check_shots(ws, 64, 1 << 20),
        "log": check_log(ws, 8192, 40000),
        "records_defaults": {"max_bytes": retention.RECORDS_MAX_BYTES,
                             "keep": retention.RECORDS_KEEP,
                             "shots_max_files": retention.SHOTS_MAX_FILES,
                             "shots_max_bytes": retention.SHOTS_MAX_BYTES,
                             "log_max_bytes": retention.LOG_MAX_BYTES},
    }
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "caps.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    for k, v in res.items():
        print("[上限] %-7s %s" % (k, json.dumps(v, ensure_ascii=False)))
    bad = [(k, kk) for k, v in res.items() if k != "records_defaults"
           for kk, vv in v.items() if kk.startswith("ok_") and not vv]
    print("[上限] 判定：%s 明细：%s" % ("全部通过" if not bad else "失败 %s" % bad, out))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
