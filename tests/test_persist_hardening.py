"""持久化可靠性加固（审查 R12 / R13）。

这里的用例都对着「真实崩溃」而不是「模拟返回值」：
  · 强杀：真的起子进程、写到一半 TerminateProcess，然后检查文件还完不完整；
  · 多进程：真的起 6/8 个解释器同时写，不是拿线程假装进程；
  · 失败路径：满盘 / 只读 / 等锁超时，断言「显式失败」，而不是静默吞掉。

跑之前（旧代码）这些用例会红，跑之后必须全绿 —— 修复报告
`_persist_fixes.md` 里记了两次实测的对照结果。
"""
import builtins
import json
import math
import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest

from core import config as cfgmod
from core import records as recmod


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_WIN = 0x08000000 if os.name == "nt" else 0


def _popen(code, args):
    env = dict(os.environ)
    env["PERSIST_ARGS"] = json.dumps(args)
    env["PYTHONPATH"] = ROOT + os.sep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=NO_WIN)


def _run(code, args, timeout=60):
    p = _popen(code, args)
    out, err = p.communicate(timeout=timeout)
    return p.returncode, out, err


def _wait_file(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """CONFIG_PATH 指到临时文件，模板/数据目录也挪走。"""
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(p))
    monkeypatch.setattr(cfgmod, "TEMPLATE_DIR", str(tmp_path / "tpl"))
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path / "data"))
    return p


# ───────────────────────────────────────────── 1. .good 备份必须原子化

_SLOW_SAVE_CHILD = r'''
import builtins, json, os, sys, time
a = json.loads(os.environ["PERSIST_ARGS"])
sys.path.insert(0, a["root"])
import core.config as C
C.CONFIG_PATH = a["config"]
C.TEMPLATE_DIR = a["tpl"]
C.DATA_DIR = a["data"]

real_open = builtins.open

class Slow:
    """把写入按 8KB 切片并逐片睡 2ms：把「写到一半被强杀」的窗口放大到秒级。"""
    def __init__(self, f):
        self.f = f
    def write(self, data):
        for i in range(0, len(data), 8192):
            self.f.write(data[i:i + 8192])
            self.f.flush()
            time.sleep(0.002)
        return len(data)
    def __getattr__(self, name):
        return getattr(self.f, name)
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.f.close()

base = os.path.dirname(a["config"])
def slow_open(file, mode="r", *args, **kw):
    f = real_open(file, mode, *args, **kw)
    if str(file).startswith(base) and any(c in mode for c in ("w", "a", "+")):
        return Slow(f)
    return f

builtins.open = slow_open
cfg = C.Config.load()
cfg["big"] = "x" * (4 * 1024 * 1024)      # 4MB：写盘和备份各约 1 秒
real_open(a["ready"], "w").write("1")
cfg.save()
'''


def test_kill_during_save_never_leaves_a_broken_backup(tmp_path, monkeypatch, cfg_path):
    """强杀 3 次（都落在备份阶段），config.json 和 .good 都必须还能解析。"""
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))
    seeded = cfgmod.Config({"threshold": 0.71})
    seeded.save()
    good = str(cfg_path) + ".good"
    assert cfgmod.Config._read_backup() is not None

    for delay in (1.15, 1.45, 1.75):
        args = {"root": ROOT, "config": str(cfg_path),
                "tpl": str(tmp_path / "tpl"), "data": str(tmp_path / "data"),
                "ready": str(tmp_path / "ready")}
        p = _popen(_SLOW_SAVE_CHILD, args)
        try:
            assert _wait_file(args["ready"]), "子进程没走到 save"
            time.sleep(delay)
        finally:
            p.kill()
            p.communicate(timeout=10)
        # 1) 主配置：要么是旧的有效版本，要么是新的有效版本，绝不半截
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict) and data
        # 2) 备份：要么上一份完好，要么这份新的写完，绝不半截
        assert os.path.getsize(good) > 0
        bak = json.loads(open(good, encoding="utf-8").read())
        assert isinstance(bak, dict) and bak, ".good 被强杀写成了半份"

    # 3) 崩溃留下的临时文件可以被清理掉（mtime 够老时）
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers, "强杀居然没留下临时文件？这个实验没测到东西"
    for n in leftovers:
        os.utime(tmp_path / n, (time.time() - 120, time.time() - 120))
    cfgmod._cleanup_at.pop(str(cfg_path), None)
    cfgmod.Config.load()
    assert not [n for n in leftovers if (tmp_path / n).exists()], "孤儿临时文件没被清理"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))


def test_backup_replace_failure_keeps_previous_good(tmp_path, monkeypatch, cfg_path):
    """备份失败不能把上一份还能用的 .good 弄坏（也不能让本次保存失败）。"""
    cfg = cfgmod.Config({"threshold": 0.5})
    cfg.save()
    good = str(cfg_path) + ".good"
    old_good = open(good, "rb").read()

    real_replace = cfgmod._replace_with_retry

    def deny_backup(src, dst, attempts=7):
        if str(dst).endswith(".good"):
            raise PermissionError("备份目标被占用")
        return real_replace(src, dst, attempts)

    monkeypatch.setattr(cfgmod, "_replace_with_retry", deny_backup)
    cfg["threshold"] = 0.9
    cfg.save()
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["threshold"] == 0.9
    assert open(good, "rb").read() == old_good, "备份被半份覆盖"
    assert cfgmod.Config._read_backup() is not None


def test_broken_backup_is_ignored(tmp_path, cfg_path):
    """校验不过的 .good 当没有备份：不许拿半份 JSON 当正常配置用。"""
    # 场景 A：备份是半截 JSON（旧代码就是直接覆盖，被杀就长这样）
    (tmp_path / "config.json").write_text('{"threshold": 0.71,', encoding="utf-8")
    (tmp_path / "config.json.good").write_text('{"sentinel": "OL', encoding="utf-8")
    assert cfgmod.Config._read_backup() is None

    cfg = cfgmod.Config.load()
    assert cfg["threshold"] == cfgmod.DEFAULTS["threshold"]
    assert "sentinel" not in cfg.data
    backups = [n for n in os.listdir(tmp_path) if ".broken-" in n]
    assert backups, "坏文件没留档"

    # 场景 B：备份完好 → 从备份恢复，而不是清成默认值
    good = json.dumps({"sentinel": "GOOD", "threshold": 0.88})
    (tmp_path / "config.json.good").write_text(good, encoding="utf-8")
    (tmp_path / "config.json").write_text("{oops", encoding="utf-8")
    cfg = cfgmod.Config.load()
    assert cfg["sentinel"] == "GOOD" and cfg["threshold"] == 0.88


# ───────────────────────────────────────────── 2. 台账残行不能吞掉下一条

_TORN_CHILD = r'''
import builtins, json, os, sys, time
a = json.loads(os.environ["PERSIST_ARGS"])
sys.path.insert(0, a["root"])
import core.records as R
R.PATH = a["records"]

for i in range(5):
    R.add(name="seed%d" % i, extra={"wid": i})

real_open = builtins.open

class Slow:
    def __init__(self, f):
        self.f = f
    def write(self, data):
        for i in range(0, len(data), 8192):
            self.f.write(data[i:i + 8192])
            self.f.flush()
            time.sleep(0.002)
        return len(data)
    def __getattr__(self, name):
        return getattr(self.f, name)
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.f.close()

def slow_open(file, mode="r", *args, **kw):
    f = real_open(file, mode, *args, **kw)
    if os.path.abspath(str(file)) == os.path.abspath(R.PATH) and "a" in mode:
        return Slow(f)
    return f

builtins.open = slow_open
real_open(a["ready"], "w").write("1")
R.add(name="torn", extra={"wid": "huge", "pad": "y" * (8 * 1024 * 1024)})
'''


def test_partial_line_is_truncated_so_next_record_survives(tmp_path, monkeypatch):
    """写到一半被强杀后，重启的第一条记录一定要落盘（旧代码连丢两条）。"""
    path = tmp_path / "records.jsonl"
    args = {"root": ROOT, "records": str(path), "ready": str(tmp_path / "ready")}
    p = _popen(_TORN_CHILD, args)
    try:
        assert _wait_file(args["ready"])
        time.sleep(0.5)                     # 8MB / 8KB × 2ms ≈ 2s：必落在写的中途
    finally:
        p.kill()
        p.communicate(timeout=10)

    raw = path.read_bytes()
    assert raw and not raw.endswith(b"\n"), "没制造出残行，这个实验没测到东西"

    monkeypatch.setattr(recmod, "PATH", str(path))
    # 乱序跑时，同一进程里更早的用例可能已经触发过记录恢复，_recovery_state
    # 是模块级累计值；本用例断言的是「这一次恢复恰好 1 条」，先清零再跑，
    # 否则会把别人的恢复次数算到自己头上（不是改断言，是隔离进程级状态）。
    monkeypatch.setattr(recmod, "_recovery_state", {"count": 0, "last": None})
    row = recmod.add(name="after-restart", extra={"wid": "new"})
    assert row is not None, "重启后的第一条记录没写进去"

    # 方式一：API 读出来
    rows = recmod.load(0)
    names = [r.get("name") for r in rows]
    assert names == ["after-restart", "seed4", "seed3", "seed2", "seed1", "seed0"]
    # 方式二：直接逐行解析文件（每条都必须是合法 JSON）
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    for ln in lines:
        assert isinstance(json.loads(ln), dict)
    # 恢复过程有明确记录
    rec = recmod.recovery_log()
    assert rec["count"] == 1 and rec["last"]["removed"] > 1000


# ───────────────────────────────────────────── 3. 跨进程互斥

_MP_ADD_CHILD = r'''
import json, os, sys
a = json.loads(os.environ["PERSIST_ARGS"])
sys.path.insert(0, a["root"])
import core.records as R
R.PATH = a["records"]
for i in range(a["n"]):
    row = R.add(name="w%s-%d" % (a["wid"], i),
                extra={"wid": a["wid"], "seq": i}, strict=True)
    if row is None:
        print("WRITE_FAILED", R.last_error(), flush=True)
        sys.exit(3)
'''


def test_eight_processes_keep_all_records(tmp_path):
    """8 个真实进程各写 100 条：800 条一条都不能丢（旧代码丢 126~154 条）。"""
    path = tmp_path / "records.jsonl"
    procs = []
    for wid in range(8):
        procs.append((wid, _popen(_MP_ADD_CHILD, {
            "root": ROOT, "records": str(path), "wid": wid, "n": 100})))
    for wid, p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"进程 {wid} 写入失败：{out} {err}"

    old = recmod.PATH
    recmod.PATH = str(path)
    try:
        rows = recmod.load(0)
        assert len(rows) == 800, f"只活下来 {len(rows)} 条"
        keys = {(r.get("wid"), r.get("seq")) for r in rows if "wid" in r}
        assert len(keys) == 800, "有重复/丢失的写入"
        assert recmod.stats()["hits"] == 800
    finally:
        recmod.PATH = old
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 800 and all(json.loads(ln) for ln in lines)


_MP_TX_CHILD = r'''
import json, os, sys, time
a = json.loads(os.environ["PERSIST_ARGS"])
sys.path.insert(0, a["root"])
import core.config as C
C.CONFIG_PATH = a["config"]
C.TEMPLATE_DIR = a["tpl"]
C.DATA_DIR = a["data"]
for i in range(a["n"]):
    with C.Config.transaction() as cfg:
        cfg["queue"].append({"id": "w%s-%d" % (a["wid"], i),
                             "name": "w%s-%d" % (a["wid"], i)})
        time.sleep(0.01)
'''


def test_six_processes_keep_all_config_writes(tmp_path, cfg_path):
    """6 个真实进程各 5 次事务：30 条队列一条都不能丢（旧代码只活下来 7 次）。"""
    cfg_path.write_text(json.dumps({"queue": [], "workflow": "list"}),
                        encoding="utf-8")
    tpl = str(tmp_path / "tpl")
    data = str(tmp_path / "data")
    procs = []
    for wid in range(6):
        procs.append((wid, _popen(_MP_TX_CHILD, {
            "root": ROOT, "config": str(cfg_path), "tpl": tpl,
            "data": data, "wid": wid, "n": 5})))
    for wid, p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"进程 {wid} 事务失败：{out} {err}"

    ids = [t["id"] for t in cfgmod.Config.load()["queue"]]
    assert len(ids) == 30, f"只活下来 {len(ids)} 次写入：{sorted(ids)}"
    assert len(set(ids)) == 30
    # 方式二：直接读盘上的 JSON，确认不是内存缓存骗人
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert len(disk["queue"]) == 30


_LOCK_HOLDER_CHILD = r'''
import json, os, sys, time
a = json.loads(os.environ["PERSIST_ARGS"])
sys.path.insert(0, a["root"])
from core.config import file_lock
with file_lock(a["lock"], timeout=5):
    open(a["ready"], "w").write("1")
    time.sleep(3)
'''


def test_lock_timeout_is_an_explicit_failure(tmp_path, monkeypatch):
    """等不到跨进程锁：必须报失败，不能假装写成功。"""
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    lock = recmod._lock_path()
    ready = tmp_path / "ready"
    p = _popen(_LOCK_HOLDER_CHILD, {"root": ROOT, "lock": lock,
                                    "ready": str(ready)})
    try:
        assert _wait_file(ready)
        monkeypatch.setattr(recmod, "RECORDS_LOCK_TIMEOUT", 0.3)
        assert recmod.add(name="blocked") is None
        assert recmod.last_error() and "等锁超时" in recmod.last_error()
        with pytest.raises(recmod.RecordWriteError):
            recmod.add(name="blocked", strict=True)
    finally:
        p.kill()
        p.communicate(timeout=10)
    time.sleep(0.1)
    assert recmod.stats()["write_ok"] is False


# ───────────────────────────────────────────── 4. mode → workflow 迁移

def test_legacy_mode_list_is_not_silently_switched(tmp_path, cfg_path):
    """老配置只有 mode="list"：升级后必须还是 list，不能被默认 countdown 顶掉。"""
    cfg_path.write_text(json.dumps({
        "mode": "list",
        "queue": [{"id": "aaaa0001", "name": "A", "mode": "list"}],
        "console": {"min_price": 100},
        "run_mode": "live", "dry_run": False,
    }, ensure_ascii=False), encoding="utf-8")

    cfg = cfgmod.Config.load()
    assert cfg["workflow"] == "list", "老用户的 list 模式被换成了 countdown"
    assert cfg["mode"] == "list"
    task = cfg["queue"][0]
    assert task["workflow"] == "list" and task["mode"] == "list"

    # 幂等：落盘一次再读，内容不变；第二次落盘字节完全一致
    cfg.save()
    first = cfg_path.read_bytes()
    again = cfgmod.Config.load()
    assert again["workflow"] == "list" and again["mode"] == "list"
    again.save()
    assert cfg_path.read_bytes() == first, "迁移不幂等，每次加载都在改文件"

    # 方式二：直接看盘上的 JSON
    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["workflow"] == "list" and disk["mode"] == "list"


def test_workflow_written_explicitly_still_wins(tmp_path, cfg_path):
    """用户显式写了 workflow 就认它（迁移只补缺，不改用户的显式选择）。"""
    cfg_path.write_text(json.dumps({"mode": "list", "workflow": "countdown"}),
                        encoding="utf-8")
    cfg = cfgmod.Config.load()
    assert cfg["workflow"] == "countdown" and cfg["mode"] == "detail"


# ───────────────────────────────────────────── 5. 部分字典按字段合并

def test_partial_sections_keep_default_keys(cfg_path):
    """只写一半的节不能整节替换，否则回退老版本直接 KeyError。"""
    cfg = cfgmod.Config({
        "console": {"min_price": 100},
        "nav": {"ons_panel": [[1, 2, 3, 4]]},
        "screen": {"base_size": [960, 540]},
        "filter_defaults": {"craft": "步枪"},
        "premarket": {"enabled": True},
        "hotkey": {"enabled": False},
        "ocr": {"ppocr": {"url": "http://127.0.0.1:9"}},
    })
    assert cfg["console"]["min_price"] == 100
    for k in ("quota", "budget", "max_price"):
        assert k in cfg["console"], f"console.{k} 被整节抹掉了"
    assert "ons_columns" in cfg["nav"], "nav.ons_columns 被整节抹掉了"
    assert "tolerance" in cfg["screen"], "screen.tolerance 被整节抹掉了"
    for k in ("sort", "pub", "ons_sort"):
        assert k in cfg["filter_defaults"], f"filter_defaults.{k} 被整节抹掉了"
    assert "spec" in cfg["hotkey"] and "start_spec" in cfg["hotkey"]
    assert set(cfgmod.DEFAULTS["premarket"]) <= set(cfg["premarket"])
    assert cfg["premarket"]["enabled"] is True
    for k in cfgmod.DEFAULTS["ocr"]["ppocr"]:
        assert k in cfg["ocr"]["ppocr"], "ocr.ppocr 被整节替换了"


# ───────────────────────────────────────────── 6. 满盘不许截断原文件

def test_save_failure_keeps_original_config(tmp_path, monkeypatch, cfg_path):
    """tmp 与直写都失败时，原文件必须一个字节都不动（旧代码会写成 0 字节）。"""
    cfg_path.write_text(json.dumps({"threshold": 0.71, "queue": [], "note": "keep"}),
                        encoding="utf-8")
    cfg = cfgmod.Config.load()
    cfg["threshold"] = 0.9

    real_open = builtins.open
    target = os.path.abspath(str(cfg_path))

    def full_open(file, mode="r", *a, **k):
        p = os.path.abspath(os.fspath(file))
        if p.startswith(target) and any(c in mode for c in "wa+"):
            if os.path.exists(p):
                os.truncate(p, 0)          # 模拟旧代码「先截断再写、写到一半满盘」
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", full_open)
    with pytest.raises(OSError):
        cfg.save()

    monkeypatch.setattr(builtins, "open", real_open)
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["threshold"] == 0.71 and data["note"] == "keep"
    assert os.path.getsize(cfg_path) > 0


# ───────────────────────────────────────────── 7. 台账写失败要显式

def test_readonly_records_file_reports_failure(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(recmod, "PATH", str(path))
    os.chmod(path, stat.S_IREAD)
    try:
        assert recmod.add(name="x") is None
        assert recmod.last_error(), "只读文件写失败仍然是静默的"
        assert recmod.stats()["write_ok"] is False
        with pytest.raises(recmod.RecordWriteError):
            recmod.add(name="x", strict=True)
    finally:
        os.chmod(path, stat.S_IWRITE)


def test_disk_full_records_write_reports_failure(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    real_open = builtins.open
    target = os.path.abspath(str(path))

    def full_open(file, mode="r", *a, **k):
        if os.path.abspath(os.fspath(file)) == target and any(c in mode for c in "wa+"):
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", full_open)
    assert recmod.add(name="x") is None
    assert recmod.last_error() and recmod.last_error() != "None"
    assert recmod.stats()["write_ok"] is False


# ───────────────────────────────────────────── 8. 其余四条

def test_save_merges_external_edits_instead_of_clobbering(tmp_path, cfg_path):
    """外部改过 config.json，未 reload 的 save 不能整份盖回去。"""
    cfg_path.write_text(json.dumps({"sentinel": "S1", "note": ""}), encoding="utf-8")
    cfg = cfgmod.Config.load()
    time.sleep(0.02)
    cfg_path.write_text(json.dumps({"sentinel": "EXT", "note": ""}), encoding="utf-8")
    time.sleep(0.02)
    cfg["note"] = "hello"
    cfg.save()

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["sentinel"] == "EXT", "外部改动被静默覆盖了"
    assert data["note"] == "hello", "本进程的改动没保存"


def test_orphan_tmp_files_are_cleaned(tmp_path, cfg_path):
    cfg = cfgmod.Config({"threshold": 0.5})
    cfg.save()
    old_a = tmp_path / "config.json.777.1.tmp"
    old_b = tmp_path / "config.json.good.777.1.tmp"
    fresh = tmp_path / "config.json.777.2.tmp"
    for p in (old_a, old_b, fresh):
        p.write_bytes(b"{}")
    for p in (old_a, old_b):
        os.utime(p, (time.time() - 120, time.time() - 120))

    cfgmod._cleanup_at.pop(str(cfg_path), None)
    cfgmod.Config.load()
    assert not old_a.exists() and not old_b.exists(), "孤儿临时文件没被清理"
    assert fresh.exists(), "正在写的临时文件被误删"


def test_corruption_backups_do_not_overwrite_each_other(tmp_path, cfg_path):
    """同一秒内坏两次：两份证据都要留下来。"""
    cfg_path.write_text("{bad1", encoding="utf-8")
    cfgmod.Config.load()
    cfg_path.write_text("{bad2", encoding="utf-8")
    cfgmod.Config.load()
    backups = sorted(n for n in os.listdir(tmp_path) if ".broken-" in n)
    assert len(backups) == 2, f"同秒备份互相覆盖了：{backups}"


def test_dir_error_message_points_at_the_real_cause(tmp_path, monkeypatch):
    """构造期目录不可用时报「目录」而不是 WinError 183。"""
    bad = tmp_path / "not_a_dir"
    bad.write_text("x", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "TEMPLATE_DIR", str(bad))
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(OSError) as ei:
        cfgmod.Config()
    msg = str(ei.value)
    assert "目录" in msg and str(bad) in msg
    assert "183" not in msg or "目录" in msg


def test_gc_task_dirs_is_skipped_when_config_unreadable(tmp_path, cfg_path):
    """配置读不出来时不能把 data/ 下的任务目录当孤儿全删（R12-03）。"""
    data_dir = tmp_path / "data"
    tasks = [data_dir / ("%08x" % i) for i in range(3)]
    for t in tasks:
        t.mkdir(parents=True)
    (data_dir / "shots").mkdir()

    cfg_path.write_text("{broken", encoding="utf-8")
    cfg = cfgmod.Config.load()          # 读不出来：这份 cfg 是默认值、load_ok=False
    assert not cfg.load_ok

    dropped = cfg.gc_task_dirs(keep=[])  # 调用方拿不到队列，只能传空
    assert dropped == 0
    assert all(t.is_dir() for t in tasks), "配置读不出来时任务目录被删了"


# ───────────────────────────── F1：恢复成功必须「真的能用」，不只是返回值

def _seed_recoverable(tmp_path, cfg_path, queue=None, threshold=0.62):
    """造一份「config.json 损坏 + .good 完好」的现场。"""
    data = {"threshold": threshold, "workflow": "countdown",
            "queue": queue or []}
    (tmp_path / "config.json.good").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg_path.write_text("{broken json", encoding="utf-8")
    return data


def test_recovered_config_is_reported_as_ok(tmp_path, cfg_path):
    """从 .good 恢复成功 = 加载成功：load_ok 必须 True（审查 F1）。"""
    queue = [{"id": "aaaa1111", "name": "救回来的任务",
              "status": "pending", "enabled": True, "fire_at": 0}]
    _seed_recoverable(tmp_path, cfg_path, queue)

    cfg = cfgmod.Config.load()
    assert cfg.load_ok is True, "数据救回来了却仍被判成「配置读不出来」"
    assert cfg._load_error is None
    assert cfg.notice and "恢复" in cfg.notice
    assert cfg["queue"][0]["id"] == "aaaa1111"
    assert cfg["threshold"] == 0.62
    pub = cfg.public()
    assert pub["config_ok"] is True and pub["config_error"] is None
    assert pub["config_notice"]


def test_recovered_config_starts_and_runs_a_task(tmp_path, monkeypatch, cfg_path):
    """完整路径：恢复成功 → 调度器能启动 → 孤儿复位 → 任务真的跑到 done。"""
    from core import scheduler as sched

    tid = "aaaa1111"
    queue = [{"id": tid, "name": "被强杀的任务", "status": "running",
              "enabled": True, "fire_at": 0, "mode": "countdown"}]
    _seed_recoverable(tmp_path, cfg_path, queue)

    # countdown 版式只需要 confirm 模板；任务目录里放一份就够
    td = tmp_path / "data" / tid
    td.mkdir(parents=True)
    (td / "confirm.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    class FakeEngine:
        """不碰真机的引擎替身：start 即 done，跑任务这条路径是真的走通的。"""
        def __init__(self):
            self.on_hit = None

        def start(self, deadline=None, mode=None, workflow=None):
            return True, "ok"

        def status(self):
            return {"state": "done", "running": False, "clicks": 3,
                    "stats": {"clicks": 3, "bought": 1}}

        def stop(self):
            return True, "ok"

        def wait_stop(self, timeout=3.0):
            return True

        def guard_snapshot(self):
            return {"state": "ok"}

    monkeypatch.setattr(sched, "Engine", FakeEngine)

    s = sched.Scheduler()               # 构造时 _recover_stale_tasks
    try:
        cfg = cfgmod.Config.load()
        assert cfg.load_ok is True, "恢复后 load_ok 还是 False"
        statuses = [t.get("status") for t in cfg["queue"] if t.get("id") == tid]
        assert statuses == ["pending"], f"孤儿任务没从 running 复位：{statuses}"

        ok, msg = s.start()
        assert ok is True, f"恢复成功后调度器仍拒绝启动：{msg}"

        deadline = time.time() + 10
        while time.time() < deadline:
            todo = [t for t in s.tasks() if t.get("id") == tid]
            if todo and todo[0].get("status") == "done":
                break
            time.sleep(0.05)
        else:
            pytest.fail("恢复后任务没跑起来：" +
                        json.dumps(s.tasks(), ensure_ascii=False)[:300])
    finally:
        s.stop()


# ───────────────────────────── F3：queue 必须按任务合并，不能整段取本地

def test_external_queue_addition_is_not_dropped(tmp_path, cfg_path):
    """别的进程往盘上加的任务，本地 save() 不能把它整段抹掉（审查 F3）。"""
    cfg = cfgmod.Config({"queue": [
        {"id": "t1", "name": "A", "status": "pending",
         "enabled": True, "fire_at": 0}]})
    cfg.save()

    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    disk["queue"].append({"id": "t2", "name": "B（外部加的）", "status": "pending",
                          "enabled": True, "fire_at": 0})
    cfg_path.write_text(json.dumps(disk, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    cfg.data["queue"][0]["max_price"] = 12.0     # 本地改了 t1
    cfg["threshold"] = 0.93
    cfg.save()

    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    ids = [t["id"] for t in on_disk["queue"]]
    assert ids == ["t1", "t2"], f"外部新增的任务被静默丢弃：{ids}"
    assert on_disk["queue"][0]["max_price"] == 12.0, "本地对 t1 的修改没生效"


def test_external_queue_deletion_wins_when_local_untouched(tmp_path, cfg_path):
    """本地没动过的任务、盘上被删了 → 跟着删（不是「只增不减」）。"""
    cfg = cfgmod.Config({"queue": [
        {"id": "t1", "name": "A", "status": "pending", "enabled": True, "fire_at": 0},
        {"id": "t2", "name": "B", "status": "pending", "enabled": True, "fire_at": 0}]})
    cfg.save()

    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    disk["queue"] = [t for t in disk["queue"] if t["id"] != "t2"]
    cfg_path.write_text(json.dumps(disk, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    cfg["threshold"] = 0.94
    cfg.save()
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [t["id"] for t in on_disk["queue"]] == ["t1"]


# ───────────────────────────── R1：读路径也必须跨进程串行

def test_read_path_waits_for_the_cross_process_lock(tmp_path, cfg_path):
    """别人持锁时 Config.load() 必须等，不能抢在隔离/恢复窗口里读/写（R1）。"""
    cfg = cfgmod.Config({"threshold": 0.66})
    cfg.save()
    lock = str(cfg_path) + ".lock"
    ready = tmp_path / "ready"
    p = _popen(_LOCK_HOLDER_CHILD, {"root": ROOT, "lock": lock,
                                    "ready": str(ready)})
    try:
        assert _wait_file(ready)
        t0 = time.time()
        got = cfgmod.Config.load()
        waited = time.time() - t0
    finally:
        p.kill()
        p.communicate(timeout=10)

    assert waited >= 1.0, f"读路径没等锁，{waited:.2f}s 就返回了"
    assert got["threshold"] == 0.66 and got.load_ok is True


def test_missing_config_restores_backup_instead_of_defaults(tmp_path, cfg_path):
    """config.json 不见了但有可用 .good：先恢复，别走「首启」写默认值（R1）。"""
    good = {"threshold": 0.77,
            "queue": [{"id": "bbbb2222", "status": "pending", "enabled": True}]}
    (tmp_path / "config.json.good").write_text(
        json.dumps(good, ensure_ascii=False), encoding="utf-8")
    assert not cfg_path.exists()

    cfg = cfgmod.Config.load()
    assert cfg.load_ok is True
    assert cfg["threshold"] == 0.77, "「首启」分支拿默认值盖掉了备份"
    assert cfg["queue"][0]["id"] == "bbbb2222"


# ───────────────────────────── R5：OCR 日志轮转失败要记录 + 兜底触发

def test_log_rotation_failure_is_recorded(tmp_path, monkeypatch):
    """转不动（文件被服务握着）要记下来，不能吞掉（审查 R5）。"""
    log = tmp_path / "ocr_service_8000.log"
    log.write_bytes(b"x" * 200)

    def deny(src, dst):
        raise PermissionError("日志文件被服务占用")

    monkeypatch.setattr(recmod.retention.os, "replace", deny)
    assert recmod.retention.rotate_log(str(log), max_bytes=64) is False
    err = recmod.retention.last_rotate_error()
    assert err and "PermissionError" in err, f"轮转失败没被记录：{err}"


def test_log_sweep_rotates_oversized_ocr_logs(tmp_path):
    """兜底扫描：不依赖 spawn，超限的 OCR 日志会被转掉（审查 R5）。"""
    big = tmp_path / "ocr_service_8001.log"
    big.write_bytes(b"x" * 200)
    other = tmp_path / "unrelated.log"
    other.write_bytes(b"x" * 200)

    rotated = recmod.retention.sweep_logs(str(tmp_path), max_bytes=64, interval=0)
    assert rotated == 1
    assert not big.exists() and (tmp_path / "ocr_service_8001.log.1").exists()
    assert other.exists(), "非 OCR 日志不该被轮转"


def test_config_load_triggers_the_log_sweep(tmp_path, monkeypatch, cfg_path):
    """Config.load() 是兜底触发点：OCR 不 spawn 也能让上限生效（审查 R5）。"""
    calls = []
    real = recmod.retention.sweep_logs

    def spy(directory, *a, **kw):
        calls.append(directory)
        return real(directory, *a, **kw)

    monkeypatch.setattr(cfgmod._retention, "sweep_logs", spy)
    cfgmod._retention._sweep_at.clear()
    cfgmod.Config.load()
    expected = os.path.abspath(str(tmp_path))
    assert calls and os.path.abspath(calls[0]) == expected, \
        "Config.load() 没有触发日志兜底扫描"


# ───────────────────────────── P11：同尺寸 + 同 mtime 的外部改动不能被盖掉

def test_external_same_size_same_mtime_edit_survives(tmp_path, cfg_path):
    """外部把 quota 改成 5，长度和 mtime 都不变 → 保存时必须按内容发现改动。

    这是备份还原 / `copy /b` / 同步盘的典型形态：旧实现只看
    (mtime_ns, size)，指纹一模一样，`_maybe_merge_external` 直接跳过，
    内存里的旧快照（quota=1）把用户的 5 盖回去（审查 P11）。
    """
    cfg = cfgmod.Config.load()
    cfg["console"]["quota"] = 1
    cfg.save()
    st = os.stat(str(cfg_path))
    raw = cfg_path.read_bytes()
    old, new = b'"quota": 1', b'"quota": 5'
    assert raw.count(old) == 1, "测试前提不成立：找不到唯一的 quota 字段"
    edited = raw.replace(old, new)
    assert len(edited) == len(raw), "测试前提不成立：改动必须等长"

    cfg_path.write_bytes(edited)
    os.utime(str(cfg_path), ns=(st.st_atime_ns, st.st_mtime_ns))
    st2 = os.stat(str(cfg_path))
    assert st2.st_mtime_ns == st.st_mtime_ns, "测试前提不成立：mtime 没还原"
    assert st2.st_size == st.st_size, "测试前提不成立：size 变了"

    # 模拟「旧进程还握着快照」：改一个无关字段再保存，合并必须看见 quota 变了
    cfg["monitor"] = cfg["monitor"] + 1
    cfg.save()

    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk["console"]["quota"] == 5, "外部改的 quota 被旧快照盖掉了"
    assert on_disk["monitor"] == cfg["monitor"], "本地改的 monitor 没写进去"
    assert cfgmod.Config.load()["console"]["quota"] == 5


# ───────────────────────────── P5：台账尾部半个 UTF-8 字符

def test_half_utf8_tail_is_tolerated_and_repaired(tmp_path, monkeypatch):
    """尾部停在半个汉字上：前后有效记录必须读得出来，并且自动截掉尾巴。

    旧实现用文本模式迭代，解码错误在迭代中抛出 → `stats()` / `load(0)`
    直接 500，重读还是 500（审查 P5）。
    """
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    row1 = {"ts": 1, "price": 10, "result": "hit", "name": "甲"}
    row2 = {"ts": 2, "price": 20, "result": "hit", "name": "乙"}
    good = (json.dumps(row1, ensure_ascii=False) + "\n"
            + json.dumps(row2, ensure_ascii=False) + "\n").encode("utf-8")
    half = "一".encode("utf-8")[:2]          # 半个汉字，且文件不以换行结尾
    path.write_bytes(good + half)

    s = recmod.stats()
    assert s["hits"] == 2 and s["spent"] == 30.0, f"有效记录被一起吞了：{s}"
    rows = recmod.load(0)
    assert [r["name"] for r in reversed(rows)] == ["甲", "乙"]

    # 读路径自愈：尾部截到最后一个完整行
    assert path.read_bytes() == good, "尾部半个字符没有被自动修掉"
    assert recmod.stats()["hits"] == 2, "修完之后再读仍然出错"


# ───────────────────────────── E1/E1b：BOM / UTF-16 配置不能被当成坏文件

@pytest.mark.parametrize("encoding,bom", [
    ("utf-8-sig", b"\xef\xbb\xbf"),
    ("utf-16", b"\xff\xfe"),
    ("utf-16-le", b""),          # 无 BOM 的 UTF-16LE（启发式识别）
])
def test_non_utf8_config_is_loaded_not_quarantined(tmp_path, cfg_path,
                                                   encoding, bom):
    """用户用记事本存成 BOM/UTF-16：配置必须照常加载，且转回 UTF-8。"""
    payload = json.dumps({"threshold": 0.88, "console": {"quota": 3}},
                         ensure_ascii=False, indent=2)
    body = payload.encode(encoding)
    if encoding == "utf-8-sig":
        body = bom + payload.encode("utf-8")
    elif encoding == "utf-16":
        body = payload.encode("utf-16")          # codec 自己带 BOM
    cfg_path.write_bytes(body)

    cfg = cfgmod.Config.load()
    assert cfg.load_ok is True, f"{encoding} 配置被当成坏文件了"
    assert cfg["threshold"] == 0.88 and cfg["console"]["quota"] == 3, \
        f"{encoding} 配置被旧 .good 或默认值覆盖了"
    assert not list(tmp_path.glob("*.broken-*")), "不该产生坏文件隔离留档"
    # 统一编码回写
    text = cfg_path.read_bytes().decode("utf-8")
    assert json.loads(text)["threshold"] == 0.88
    assert cfg_path.read_bytes()[:3] != b"\xef\xbb\xbf", "回写时又带上了 BOM"


# ───────────────────────────── S1/S2：轮转失败不能先毁最老归档

def _seed_three(path):
    path.write_text("current\n" * 1000, encoding="utf-8")
    for i in (1, 2):
        (path.parent / f"{path.name}.{i}").write_text(
            f"archive{i}\n" * 500, encoding="utf-8")


def test_rotate_failure_keeps_the_oldest_archive(tmp_path, monkeypatch):
    """`.1` 被读句柄占着：轮转失败，但 `.2` 不能被先删掉（审查 S1）。"""
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    _seed_three(path)

    fh = open(str(path) + ".1", "rb")
    try:
        lost = []
        for _ in range(2):
            ok = recmod.retention.rotate(str(path), max_bytes=1000, keep=3)
            lost.append(not (tmp_path / "records.jsonl.2").exists())
            (tmp_path / "records.jsonl.2").write_text("again\n", encoding="utf-8")
    finally:
        fh.close()

    assert lost == [False, False], f"轮转没成功却把最老归档删了：{lost}"
    assert recmod.retention.last_rotate_error(), "失败没有被记录"


def test_rotate_when_oldest_archive_is_locked_tells_the_truth(tmp_path, monkeypatch):
    """`.2` 被占：转不动就告警，`.1`/`.2` 都保持原样（审查 S2）。"""
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    _seed_three(path)

    fh = open(str(path) + ".2", "rb")
    try:
        ok = recmod.retention.rotate(str(path), max_bytes=1000, keep=3)
    finally:
        fh.close()

    assert ok is False
    assert (tmp_path / "records.jsonl.1").read_text(encoding="utf-8").startswith("archive1")
    assert (tmp_path / "records.jsonl.2").read_text(encoding="utf-8").startswith("archive2")
    assert recmod.retention.last_rotate_error(), "`.2` 被占用时没有任何告警"


def test_prune_converges_even_with_readonly_old_files(tmp_path):
    """500 张、最旧 450 张只读：上限 400 必须真的收敛（审查 S3）。"""
    d = tmp_path / "shots"
    d.mkdir()
    for i in range(500):
        (d / f"s{i:04d}.jpg").write_bytes(b"x" * 64)
    for i in range(450):
        os.chmod(str(d / f"s{i:04d}.jpg"), 0o444)
    try:
        removed = recmod.retention.prune_dir(str(d), max_files=400,
                                             max_bytes=10 ** 12)
        left = len(os.listdir(str(d)))
    finally:
        for n in os.listdir(str(d)):
            try:
                os.chmod(str(d / n), 0o666)
            except OSError:
                pass
    assert left <= 400, f"只读文件删不掉却仍被扣减：删了 {removed}，还剩 {left}"


# ───────────────────────────── S7：load(0) 必须和 load(N) 一样「新的在前」

def test_full_load_is_newest_first_across_archives(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    (tmp_path / "records.jsonl.2").write_text(
        '{"name":"oldest1"}\n{"name":"oldest2"}\n', encoding="utf-8")
    (tmp_path / "records.jsonl.1").write_text(
        '{"name":"older1"}\n{"name":"older2"}\n', encoding="utf-8")
    path.write_text('{"name":"new1"}\n{"name":"new2"}\n', encoding="utf-8")

    names = [r["name"] for r in recmod.load(0)]
    assert names == ["new2", "new1", "older2", "older1", "oldest2", "oldest1"], \
        f"load(0) 顺序错了（应新的在前）：{names}"


# ───────────────────────────── 写入污染：孤代理 / NaN / 10**400 不许落盘

def test_poisoned_values_never_reach_the_ledger(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    monkeypatch.setattr(recmod, "PATH", str(path))
    recmod.clear()

    assert recmod.add(name="\ud800bad", price=100) is None
    assert recmod.add(name="ok", price=float("nan")) is None
    assert recmod.add(name="ok", price=float("inf")) is None
    assert recmod.add(name="ok", price=10 ** 400) is None
    assert recmod.add(name="ok", elapsed=10 ** 400) is None
    assert recmod.add(name="ok", extra={"k": float("nan")}) is None

    assert path.read_bytes() == b"", f"非法值被写进了台账：{path.read_bytes()!r}"
    st = recmod.stats()
    assert st["write_ok"] is False and "非法" in (st["write_error"] or ""), \
        f"拒收没有留下可读原因：{st}"
    json.dumps(st, allow_nan=False)                 # 统计本身必须可序列化

    assert recmod.add(name="ok", price=199, result="hit")
    st = recmod.stats()
    assert st["spent"] == 199.0 and st["write_ok"] is True


# ───────────────────────────── N9/N12：10**400 不许把 get/snapshot 打炸

def test_huge_numbers_in_config_are_sanitized_at_load(cfg_path):
    huge = int("9" * 400)
    cfg_path.write_text(json.dumps({
        "click_hz": huge,
        "threshold": huge,
        "console": {"quota": huge},
        "queue": [{"fire_at": huge}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg = cfgmod.Config.load()
    assert cfg.load_ok is True
    for key in ("click_hz", "threshold"):
        v = float(cfg.get(key))                  # 以前这里抛 OverflowError
        assert math.isfinite(v), f"{key} 还是不可表示的数：{v}"
    assert cfg["console"]["quota"] == 1, "超大 quota 没有退回默认值"
    assert cfg["queue"][0]["fire_at"] == 0.0

    from core import switches
    assert switches.get(cfg, "fire.hz") is not None
    json.dumps(switches.snapshot(cfg), allow_nan=False)   # 界面接口不再 500
    cfg.save()                                            # 保存也不能炸




