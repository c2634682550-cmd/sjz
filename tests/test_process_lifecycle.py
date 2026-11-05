"""进程生命周期与并发一致性。

这几条以前**一条测试都没有** —— 54 个测试文件测的全是单模块行为，
没人测「启动/退出/多开/提权」这类进程级的事，所以下面四个问题
一直活到真机上才被发现：

  · 关窗不退出 → 弹浏览器、进程常驻、端口漂移、越开越多
  · 没有单实例锁 → 两个调度器抢同一个鼠标（会重复下单）
  · 提权判定扫所有可见窗口 → 任务管理器一开着就无限弹 UAC
  · 配置读写各持快照 → 后写的整份覆盖先写的（设置自己变回去）
"""
import os
import threading
import time

import pytest

from core import config, jobs, single_instance
from core import input as input_mod


# ---------------------------------------------------------------- 提权判定

def test_needs_elevation_ignores_unrelated_high_windows(monkeypatch):
    """机器上开着更高权限的窗口（任务管理器）不该触发提权。

    以前 needs_elevation 扫**所有可见窗口**取最高权限，任务管理器一开着
    就判「需要提权」；提权重开之后新进程又扫到同一个窗口、再判一次，
    变成无限弹 UAC（desktop.log 里 01:07–01:39 连弹 8 次）。
    """
    monkeypatch.setattr(input_mod, "process_integrity", lambda *a: 0x2000)
    monkeypatch.setattr(input_mod, "game_integrity", lambda: None)
    assert input_mod.needs_elevation() is False


def test_needs_elevation_when_game_is_higher(monkeypatch):
    """游戏真的比我们高，才该提权。"""
    monkeypatch.setattr(input_mod, "process_integrity", lambda *a: 0x2000)
    monkeypatch.setattr(input_mod, "game_integrity", lambda: 0x3000)
    assert input_mod.needs_elevation() is True


def test_elevate_self_never_relaunches_twice(monkeypatch):
    """已经提过一次就不再重开 —— 防无限弹 UAC。

    就算判定逻辑还是认为「权限不够」（游戏以 System 跑），也只能弹一次，
    之后把选择权交回用户。
    """
    monkeypatch.setenv(input_mod.ELEVATED_ENV, "1")
    monkeypatch.setattr(input_mod, "needs_elevation", lambda: True)
    assert input_mod.elevate_self() is False


def test_elevation_marker_is_inherited_by_the_child():
    """重开子进程靠环境变量传递「别再提权了」，名字必须稳定。"""
    assert input_mod.ELEVATED_ENV == "SJZ_ELEVATED"


# ---------------------------------------------------------------- 单实例

def test_single_instance_blocks_the_second_copy():
    """第二个实例必须被挡住，并且第一个放掉之后还能再抢。"""
    name = f"Local\\SJZ-Test-{os.getpid()}-{time.time_ns()}"
    single_instance.release()
    try:
        assert single_instance.acquire(name) is True
        assert single_instance.acquire(name) is False, "第二个实例没被拦住"
        single_instance.release()
        assert single_instance.acquire(name) is True, "放掉之后抢不回来了"
    finally:
        single_instance.release()


def test_single_instance_can_be_disabled_for_development(monkeypatch):
    """开发时同时开源码版和 exe 版是常态，要能放行。"""
    monkeypatch.setenv(single_instance.ALLOW_MULTI_ENV, "1")
    assert single_instance.acquire() is True


# ---------------------------------------------------------------- 配置事务

@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把配置/模板/数据目录全指到临时目录，别碰真实 data/。"""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path / "templates"))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    config.Config().save()
    return tmp_path


def test_transaction_keeps_other_writers_changes(sandbox):
    """两个线程各改各的字段，谁也不能把对方盖掉。

    这就是「设置改完几秒后自己变回去」的根因：调度器存任务状态时拿
    自己启动时的快照整份回写，把用户刚存的设置盖掉。
    """
    def set_console():
        for i in range(30):
            with config.Config.transaction() as cfg:
                cur = dict(cfg.get("console") or {})
                cur["max_price"] = 100 + i
                cfg["console"] = cur

    def set_threshold():
        for i in range(30):
            with config.Config.transaction() as cfg:
                cfg["threshold"] = round(0.5 + i / 100, 4)

    ts = [threading.Thread(target=set_console), threading.Thread(target=set_threshold)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)

    cfg = config.Config.load()
    assert cfg["console"]["max_price"] == 129, "设置被别的写入盖掉了"
    assert abs(float(cfg["threshold"]) - 0.79) < 1e-6, "阈值被别的写入盖掉了"


def test_reload_swaps_in_disk_contents(sandbox):
    """引擎手上的 cfg 对象要能被就地刷新（运行中改参数即时生效）。"""
    cfg = config.Config.load()
    cfg["active_task"] = {"max_price": 100}
    with config.Config.transaction() as other:
        other["active_task"] = {"max_price": 5000}

    assert cfg.get("active_task")["max_price"] == 100   # 手上还是旧的
    cfg.reload()
    assert cfg.get("active_task")["max_price"] == 5000  # 刷新后是新的


# ---------------------------------------------------------------- data 目录

def test_gc_removes_only_orphan_task_dirs(sandbox):
    """孤儿任务目录要清掉，队列里还在的、shots/ 这些一律不许动。"""
    data = sandbox / "data"
    (data / "001ec86e").mkdir(parents=True)
    (data / "deadbeef").mkdir()
    (data / "shots").mkdir()
    (data / "records.jsonl").write_text("{}", encoding="utf-8")

    dropped = config.Config().gc_task_dirs(keep=["001ec86e"])

    assert dropped == 1
    assert (data / "001ec86e").is_dir(), "队列里的任务目录被误删"
    assert not (data / "deadbeef").exists(), "孤儿目录没删掉"
    assert (data / "shots").is_dir(), "shots/ 被当成任务目录删了"
    assert (data / "records.jsonl").is_file(), "记录文件被删了"


def test_removing_a_task_deletes_its_template_dir(sandbox):
    """删任务要连它的模板目录一起删，否则 data/ 只涨不落。"""
    from core import scheduler as scheduler_mod

    tid = "deadbeef"
    (sandbox / "data" / tid).mkdir(parents=True)
    (sandbox / "data" / tid / "buy.png").write_bytes(b"x")
    with config.Config.transaction() as cfg:
        cfg["queue"] = [{"id": tid, "name": "甲", "status": "pending",
                         "enabled": True, "fire_at": 0, "mode": "list",
                         "regions": {}, "keywords": []}]

    s = scheduler_mod.Scheduler()
    assert s.remove(tid) == 1
    assert not (sandbox / "data" / tid).exists(), "任务目录没跟着删"


def test_task_without_own_template_falls_back_instead_of_crashing(sandbox):
    """任务目录里没有模板时回退到全局那份，不能抛 SameFileError。

    这台机器上就是这么死的：任务启动即失败（队列里那条的 result 就是
    SameFileError），而 /api/scheduler/start 已经回了「已启动」——
    界面上看起来就是「点了没反应」。
    """
    from core import scheduler as scheduler_mod

    tpl = sandbox / "templates"
    tpl.mkdir(parents=True, exist_ok=True)
    (tpl / "buy.png").write_bytes(b"x")
    (tpl / "confirm.png").write_bytes(b"x")
    (sandbox / "data" / "deadbeef").mkdir(parents=True, exist_ok=True)

    cfg = config.Config.load()
    assert scheduler_mod.sync_template(cfg, "confirm", "deadbeef") is True
    assert scheduler_mod.sync_template(cfg, "buy", "deadbeef") is True
    # 真缺的模板要判 False（而不是抛异常），调用方才能给出人话
    assert scheduler_mod.sync_template(cfg, "不存在的模板", "deadbeef") is False


def test_start_refuses_before_spawning_when_template_is_missing(sandbox):
    """缺模板时要在**启动接口里**就失败，别让任务在后台线程里秒死。

    后台线程里死掉的话，接口已经回了 ok，用户看不到任何东西。
    """
    from core import scheduler as scheduler_mod

    with config.Config.transaction() as cfg:
        cfg["queue"] = [{"id": "deadbeef", "name": "", "status": "pending",
                         "enabled": True, "fire_at": 0, "mode": "list",
                         "regions": {}, "keywords": []}]

    s = scheduler_mod.Scheduler()
    ok, msg = s.start()
    assert ok is False, "缺模板也让它跑起来了"
    assert "模板" in msg, msg


def test_task_regions_are_borrowed_then_returned(sandbox):
    """跑任务时临时借用全局框选，跑完必须还回去。

    以前借了不还：跑完一条任务，用户手动框选的区域就被那条任务永久改写，
    下一个任务和「定位」都按错的区域去点。
    """
    from core import scheduler as sch

    mine = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    theirs = {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}
    with config.Config.transaction() as cfg:
        cfg["regions"]["list"] = mine
        cfg["regions"]["buy"] = mine
        cfg["queue"] = [{"id": "aaaaaaaa", "name": "", "status": "pending",
                         "enabled": True, "fire_at": 0, "mode": "list",
                         "regions": {"list": theirs}, "keywords": [],
                         "min_price": 0, "max_price": 0, "quota": 1, "budget": 0}]

    s = sch.Scheduler()
    task = s.tasks()[0]
    saved = s._borrow_config(task)
    assert config.Config.load()["regions"]["list"] == theirs, "任务区域没生效"

    s._return_config(saved)
    back = config.Config.load()["regions"]
    assert back["list"] == mine, "跑完没把框选还回去"
    assert back["buy"] == mine, "没借的键也被还原操作动了"


def test_start_does_not_revive_a_done_task(sandbox, monkeypatch):
    """已完成的任务不会被 start 自动复活；显式改回 pending 后才重跑，只跑一遍。

    以前 start 会把 failed/done 全部重置成 pending：换目标后旧任务还留在
    队列里，点一次开始就把上一个目标重新抢一遍。现在必须由用户在队列页
    显式确认（update status=pending），「只跑一遍」同样要守住。
    """
    from core import scheduler as sch

    tpl = sandbox / "templates"
    tpl.mkdir(parents=True, exist_ok=True)
    (tpl / "buy.png").write_bytes(b"x")
    (tpl / "confirm.png").write_bytes(b"x")
    with config.Config.transaction() as cfg:
        cfg["queue"] = [{"id": "deadbeef", "name": "", "status": sch.DONE,
                         "enabled": True, "fire_at": 0, "mode": "list",
                         "regions": {}, "keywords": [], "quota": 1,
                         "budget": 0, "bought": 0, "spent": 0.0,
                         "result": "SameFileError: ..."}]

    started = []

    class FakeEngine:
        def __init__(self):
            self.on_hit = None

        def start(self, **kw):
            started.append(kw)
            return True, "已启动"

        def status(self):
            return {"state": "done", "running": False, "stats": {"clicks": 0},
                    "log": [], "error": None, "rows": []}

        def stop(self):
            return True, ""

    monkeypatch.setattr(sch, "Engine", FakeEngine)
    s = sch.Scheduler()
    ok, msg = s.start()
    assert ok is False, "已完成的任务不该被 start 自动复活"
    assert "重跑" in msg, f"要告诉用户怎么重跑：{msg}"
    assert started == [], "没确认就启动了引擎"
    assert s.update("deadbeef", status=sch.PENDING) is True
    ok, msg = s.start()
    assert ok is True, msg

    deadline = time.time() + 5
    while time.time() < deadline:
        if any("队列跑完了" in e["msg"] for e in list(s._log)):
            break
        time.sleep(0.02)
    s.stop()

    msgs = [e["msg"] for e in list(s._log)]
    assert any("开始执行" in m for m in msgs), f"失败的任务没被重跑：{msgs}"
    assert len(started) == 1, f"引擎被启动了 {len(started)} 次（会重复下单）"


# ---------------------------------------------------------------- 后台动作

@pytest.fixture()
def fresh_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "_current", None)
    monkeypatch.setattr(jobs, "_jobs", {})
    yield jobs
    # 别把还在跑的线程留给下一个测试
    deadline = time.time() + 3
    while jobs.busy() and time.time() < deadline:
        time.sleep(0.02)


def _wait(job_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = jobs.status(job_id)
        if st and st["state"] != "running":
            return st
        time.sleep(0.02)
    raise AssertionError(f"任务 {job_id} 没在 {timeout}s 内结束")


def test_job_runs_in_background_and_carries_result(fresh_jobs):
    """定位这类慢活要能扔到后台，进度和结果都能查。"""
    def work(log):
        log("第一步")
        log("第二步")
        return {"name": "某件砖", "y": 300}

    jid = jobs.submit(work, name="定位")
    st = _wait(jid)
    assert st["state"] == "done"
    assert st["result"] == {"name": "某件砖", "y": 300}
    assert st["log"] == ["第一步", "第二步"]
    assert st["elapsed"] >= 0


def test_job_failure_is_reported_not_swallowed(fresh_jobs):
    def boom(log):
        raise RuntimeError("抢不到游戏前台")

    st = _wait(jobs.submit(boom))
    assert st["state"] == "failed"
    assert "抢不到游戏前台" in st["error"]


def test_second_job_is_refused_while_one_is_running(fresh_jobs):
    """鼠标只有一个：忙的时候直接拒绝，不排队。

    排队意味着用户点了之后过很久才突然开始动鼠标，比报错更吓人。
    """
    started = threading.Event()
    release = threading.Event()

    def slow(log):
        started.set()
        release.wait(2.0)

    jid = jobs.submit(slow, name="定位")
    assert started.wait(2.0), "第一个任务没跑起来"
    with pytest.raises(jobs.Busy):
        jobs.submit(lambda log: None, name="关注")
    release.set()
    _wait(jid)


def test_unknown_job_id_returns_none(fresh_jobs):
    assert jobs.status("nope") is None


# ---------------------------------------------------------------- 独立窗口

def test_open_app_window_is_a_real_app_window(monkeypatch):
    """没有 pywebview 时用 Edge/Chrome 的 --app 起独立窗口，不是浏览器标签。

    以前这里 `webbrowser.open()`：开出来带地址栏和标签页，直接违反
    「必须独立窗口」，而且失败还被吞掉，进程静默挂住占端口。
    """
    import subprocess

    import desktop

    calls = {}

    class _Proc:
        def poll(self):
            return None

    def fake_popen(args, **kw):
        calls["args"] = list(args)
        return _Proc()

    monkeypatch.setattr(desktop, "_find_browser",
                        lambda: r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    proc, exe = desktop._open_app_window("http://127.0.0.1:8777", 1180, 800)
    assert proc is not None, exe
    args = calls["args"]
    assert args[0].endswith("msedge.exe")
    assert "--app=http://127.0.0.1:8777" in args, args
    assert "--window-size=1180,800" in args, args


def test_open_app_window_fails_loudly_without_browser(monkeypatch):
    """连 Edge/Chrome 都没有时把原因交回去，由调用方报错退出。"""
    import desktop

    monkeypatch.setattr(desktop, "_find_browser", lambda: None)
    proc, why = desktop._open_app_window("http://127.0.0.1:8777", 1180, 800)
    assert proc is None
    assert "Edge/Chrome" in why, why


def test_fatal_no_window_returns_error_and_writes_log(monkeypatch, tmp_path):
    """打不开窗口要留下日志 + 错误码，绝不静默留在后台占端口。"""
    import types

    import desktop

    monkeypatch.setattr(desktop, "LOG_PATH", str(tmp_path / "desktop.log"))
    fake = types.SimpleNamespace(
        user32=types.SimpleNamespace(MessageBoxW=lambda *a: 0))
    monkeypatch.setattr(desktop.ctypes, "windll", fake)

    assert desktop._fatal_no_window("http://127.0.0.1:8777",
                                    "本机找不到 Edge/Chrome") == 2
    text = (tmp_path / "desktop.log").read_text(encoding="utf-8")
    assert "打不开独立窗口" in text and "Edge/Chrome" in text, text


def test_wait_app_returns_once_the_window_closes():
    """窗口关了就要返回，不能像旧看门狗那样永久阻塞。"""
    import desktop

    class _Proc:
        def poll(self):
            return 0

    class _Thread:
        def is_alive(self):
            return True

    t0 = time.time()
    desktop._wait_app(_Proc(), _Thread(), tick_s=0.01)
    assert time.time() - t0 < 1


def test_start_bat_never_opens_a_browser_tab():
    """start.bat 不许再 `start http://...` 开浏览器标签。"""
    import pathlib

    bat = (pathlib.Path(__file__).resolve().parent.parent / "start.bat").read_text(
        encoding="utf-8", errors="ignore").lower()
    assert "start http" not in bat, "start.bat 还在开浏览器标签"

