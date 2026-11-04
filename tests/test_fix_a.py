"""配置 / 调度 / 服务 这一组的修复回归（A 组）。

覆盖审查里落在 config.py / scheduler.py / preflight.py / records.py /
jobs.py / timesync.py / pagewatch.py 的条目，全部用 tmp 目录 + 假对象，
不联网、不启动 GUI、不碰真鼠标。
"""
import builtins
import json
import os
import time

import pytest

from core import config, jobs, preflight, records, timesync
from core.scheduler import Scheduler, _fire_at


# ── 配置：读失败 / 坏文件 / 加载期归一 ───────────────────────────────

def test_read_failure_never_overwrites(tmp_path, monkeypatch):
    """读不了 ≠ 文件坏了：不能拿默认值回写（审查 S1）。

    触发条件：config.json 被别的进程占着（Windows 上很常见），open 抛
    PermissionError。以前 `_load_locked` 吞掉异常、返回默认值，transaction()
    退出时照样 save —— 用户框选 / 队列 / 阈值被整份默认值覆盖。
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"threshold": 0.71}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config.time, "sleep", lambda s: None)   # 别真睡 9 次

    real_open = builtins.open

    def fake_open(f, *a, **k):
        if os.path.abspath(str(f)) == os.path.abspath(str(path)):
            raise PermissionError("被占用")
        return real_open(f, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)

    cfg, ok = config.Config._load_locked()
    assert ok is False, "读失败被当成了「读到了默认配置」"
    assert cfg["threshold"] == config.DEFAULTS["threshold"]

    with pytest.raises(config.ConfigLoadError):
        with config.Config.transaction() as c:
            c["threshold"] = 0.9
    assert json.loads(path.read_text(encoding="utf-8"))["threshold"] == 0.71


def test_non_object_json_is_quarantined(tmp_path, monkeypatch):
    """顶层是字符串/数字/数组也要留档重置（审查 S1）。"""
    path = tmp_path / "config.json"
    path.write_text('"not-an-object"', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))

    cfg, ok = config.Config._load_locked()
    assert ok is True
    assert cfg["workflow"] in config.WORKFLOWS
    backups = [p for p in os.listdir(tmp_path) if ".broken-" in p]
    assert backups, "坏文件没留档，用户手写的内容就彻底没了"
    assert any(p.endswith(".bad") for p in backups)
    assert json.loads(path.read_text(encoding="utf-8"))["threshold"] \
        == config.DEFAULTS["threshold"]


def test_reload_failure_keeps_regions_and_active_task(tmp_path, monkeypatch):
    """运行中 reload 失败不许清空内存配置（审查 S5）。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))

    cfg = config.Config({
        "regions": {"buy": {"x": 0, "y": 0, "w": 1, "h": 1}},
        "active_task": {"name": "AKM"},
        "queue": [{"id": "abcd1234", "name": "AKM"}],
    })
    monkeypatch.setattr(config.time, "sleep", lambda s: None)
    real_open = builtins.open

    def fake_open(f, *a, **k):
        if os.path.abspath(str(f)) == os.path.abspath(str(path)):
            raise PermissionError("被占用")
        return real_open(f, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    cfg.reload()
    assert cfg["regions"]["buy"]["w"] == 1
    assert cfg["active_task"]["name"] == "AKM"


def test_illegal_trigger_mode_and_dual_track_fields_are_normalized():
    """`trigger_mode:"countdown"` 归一成 vanish；mode 跟着 workflow 走（审查 S6/S7）。"""
    cfg = config.Config({
        "trigger_mode": "countdown",
        "mode": "list",
        "workflow": "countdown",
        "queue": [{"id": "abcd1234", "mode": "list", "workflow": "countdown",
                   "fire_at": "1700000000"}],
    })
    assert cfg["trigger_mode"] == "vanish", "引擎会把它当 appear"
    assert cfg["workflow"] == "countdown"
    assert cfg["mode"] == "detail", "workflow=countdown 却派生出 list"
    task = cfg["queue"][0]
    assert task["mode"] == "detail"
    # S25：pending.sort 以前直接 t["fire_at"]，字符串比大小 / 缺失都会炸
    assert isinstance(task["fire_at"], float)


def test_queue_garbage_is_coerced():
    cfg = config.Config({"queue": {}, "regions": [1, 2], "trigger_mode": 123})
    assert cfg["queue"] == []
    assert isinstance(cfg["regions"], dict)
    assert cfg["trigger_mode"] == "vanish"


def test_fire_at_helper_tolerates_dirty_values():
    assert _fire_at({"fire_at": None}) == 0.0
    assert _fire_at({"fire_at": "abc"}) == 0.0
    assert _fire_at({}) == 0.0
    assert _fire_at({"fire_at": 123}) == 123.0


# ── 调度：active_task 同步 ─────────────────────────────────────────

def test_update_active_task_writes_active_task(tmp_path, monkeypatch):
    """改正在跑的任务必须同步写 active_task（审查 S4）。

    以前只改 queue 里那份，引擎 reload 读 active_task，永远拿旧条件。
    """
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    config.Config().save()

    s = Scheduler()
    task, msg = s.add(name="AKM", mode="list", workflow="list",
                      regions={"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                               "buy": {"x": 0, "y": 0, "w": 1, "h": 1}})
    assert task, msg
    s._active_id = task["id"]

    assert s.update(task["id"], max_price=1200)
    cfg = config.Config.load()
    assert cfg["active_task"]["max_price"] == 1200
    assert [t for t in cfg["queue"] if t["id"] == task["id"]][0]["max_price"] == 1200


# ── preflight：三路口径与引擎一致 ─────────────────────────────────

def _facts(**over):
    base = {
        "size": (1920, 1080), "ocr": True, "ocr_name": "stub",
        "clock": {"offset": 0.0, "at": 1700000000.0, "stale": False},
        "templates": {"buy": True, "confirm": True},
        "regions": {"buy": True, "confirm": True, "list": True, "clock": True},
        "page": "onsale",
    }
    base.update(over)
    return base


def test_list_mode_does_not_require_confirm():
    facts = _facts(templates={"buy": True, "confirm": False})
    checks = preflight.evaluate(facts, "list", "list")
    assert preflight.all_ok(checks), "列表模式被确认模板拦住了（P0-4）"
    tpl = [c for c in checks if c["id"] == "templates"][0]
    assert "余额" in tpl["detail"]


def test_detail_mode_requires_confirm():
    checks = preflight.evaluate(_facts(templates={"buy": True, "confirm": False}),
                                "detail", "detail")
    assert not preflight.all_ok(checks)


def test_countdown_mode_checks_clock_region():
    """倒计时任务没框 clock 区域时要标红（审查 S7）。"""
    checks = preflight.evaluate(_facts(regions={"buy": True, "confirm": True,
                                                "list": True}),
                                "list", "countdown")
    assert not preflight.all_ok(checks)
    tpl = [c for c in checks if c["id"] == "templates"][0]
    assert "倒计时" in tpl["detail"]


# ── records：读失败要响、脏价格不炸、verified 可分 ─────────────────

def test_bad_price_does_not_raise():
    row = records.add(name="X", price="¥1,230", result="hit")
    assert row is not None and row["price"] is None


def test_read_failure_raises_instead_of_zero(monkeypatch):
    real_open = builtins.open

    def fake_open(f, *a, **k):
        if os.path.abspath(str(f)) == os.path.abspath(records.PATH):
            raise PermissionError("被占用")
        return real_open(f, *a, **k)

    monkeypatch.setattr(records, "PATH", __file__)      # 一定存在，走到 open
    monkeypatch.setattr(builtins, "open", fake_open)
    with pytest.raises(records.RecordReadError):
        records.load()


def test_stats_can_split_verified(monkeypatch):
    monkeypatch.setattr(records, "PATH", records.PATH + ".stats")
    records.clear()
    records.add(name="真成交", price=100, verified=True)
    records.add(name="只点了确认", price=100, verified=False)
    all_rows = records.load()
    assert records.stats(all_rows)["hits"] == 2
    assert records.stats(all_rows)["verified_hits"] == 1
    assert records.stats(verified_only=True)["hits"] == 1
    os.remove(records.PATH)


# ── jobs：BaseException 不能把 busy 卡死 ──────────────────────────

def test_base_exception_marks_job_failed():
    job = {"id": "x", "name": "t", "state": "running", "result": None,
           "error": None, "started": time.time(), "finished": None,
           "log": []}

    class HardStop(BaseException):
        """模拟 KeyboardInterrupt / SystemExit 这类 BaseException。"""

    def boom(log):
        raise HardStop("假装被打断")

    # 真线程里异常只会带走那条线程；这里照样让它往外抛，模拟真实行为
    with pytest.raises(HardStop):
        jobs._run(job, boom)
    assert job["state"] == "failed", "线程带着 running 死了，busy() 永远为真"
    assert "BaseException" in (job["error"] or "")


# ── timesync：过期 offset 不再被信任 ───────────────────────────────

def test_stale_offset_is_ignored(monkeypatch):
    monkeypatch.setattr(timesync, "_state",
                        {"offset": 0.35, "delay": 0.01, "server": "s",
                         "at": time.time() - timesync.OFFSET_TTL_S - 10,
                         "samples": [{"server": "s", "offset": 0.35, "delay": 0.01}]})
    assert timesync.offset() == 0.0
    info = timesync.info()
    assert info["stale"] is True and info["offset"] == 0.0
    # samples 是拷贝，外部拿着迭代不会被下一轮 sync 换掉
    assert info["samples"] is not timesync._state["samples"]


def test_fresh_offset_is_used(monkeypatch):
    monkeypatch.setattr(timesync, "_state",
                        {"offset": 0.35, "delay": 0.01, "server": "s",
                         "at": time.time(), "samples": []})
    assert abs(timesync.offset() - 0.35) < 1e-9


# ── pagewatch：monitor 真的传下去了 ──────────────────────────────

def test_pagewatch_uses_configured_monitor(monkeypatch):
    import core.input as input_mod
    import core.nav as nav_mod
    import core.screen as screen_mod

    seen = {}

    class FakeScreen:
        def __init__(self, monitor=1):
            self.monitor = monitor

        def grab(self):
            raise AssertionError("这里不该真抓屏")

    class FakeNav:
        def __init__(self, screen, mouse, snap=None, dry_run=True):
            seen["monitor"] = screen.monitor

    monkeypatch.setattr(screen_mod, "Screen", FakeScreen)
    monkeypatch.setattr(input_mod, "Mouse", lambda screen: object())
    monkeypatch.setattr(nav_mod, "Navigator", FakeNav)

    from core.pagewatch import PageWatcher
    PageWatcher(monitor=2)._make_nav()
    assert seen["monitor"] == 2, "选了 2 号屏却还在认 1 号屏（S19）"
