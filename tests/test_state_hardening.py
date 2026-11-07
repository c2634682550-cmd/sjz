"""状态机/调度接缝硬化回归（_bug_r04_state.md + _bug_r01_concurrency.md）。

覆盖：
  B01 看门狗在开火/确认阶段打断后**绝不**自动重跑（防重复下单）
  B02 守卫硬停持久化 + 调度器派发前必须看守卫
  B03 containment_pending 只能由「收尾真的做完」清零
  B04 WAITING_HUMAN 目标达成后自动续跑
  B05 RECOVERING 不再永久悬挂
  B14 抖动未确认帧不算正常；重入不重置总计时
  B08 任务切换不残留 _fails_in_row / _rows
  B09 模板借用必须归还
  R01 #1/#2 并发 start 只能起 1 个实例；#3 stop 不再假成功；#4 退出前释放输入
  R01 #5 PageWatcher 并发 start 幂等、异常不静默

全部离线：不抓屏、不注入输入、不联网、不起长跑任务。
"""
import contextlib
import copy
import json
import os
import sys
import threading
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as eng_mod           # noqa: E402
from core import config as cfg_mod          # noqa: E402
from core.config import Config              # noqa: E402
from core import pagewatch as pw_mod        # noqa: E402
from core import scheduler as sch           # noqa: E402
from core import scene_guard as sg          # noqa: E402
from core.engine import (Engine, ABORT, CONFIRM, DONE, FIRE,  # noqa: E402
                         SCAN, WATCH)
from core.pagewatch import PageWatcher      # noqa: E402
from core.scene_guard import (SceneGuard, GuardConfig, Signals,  # noqa: E402
                              SC_CLICK_NO_EFFECT, SC_INSUFFICIENT)
from core.scheduler import Scheduler        # noqa: E402


# ------------------------------------------------------------------ 工具

class _Cfg(dict):
    """最小的 Config 替身：内存队列 + 可指定的模板路径。"""

    load_ok = True
    last_error = None

    def __init__(self, queue=None, tpl_dir=None):
        super().__init__({
            "queue": queue or [],
            "regions": {"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                        "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1}},
            "active_task": {},
            "workflow": "countdown",
        })
        self._tpl_dir = tpl_dir

    def save(self):
        pass

    def template_path(self, name, tid=None):
        if self._tpl_dir:
            return os.path.join(self._tpl_dir, f"{name}.png")
        return os.path.join("no-such-dir", f"{name}.png")

    def set_region(self, name, box):
        self["regions"][name] = box


def _install_cfg(monkeypatch, cfg):
    import contextlib

    @contextlib.contextmanager
    def transaction():
        yield cfg
        cfg.save()

    monkeypatch.setattr(sch, "Config",
                        types.SimpleNamespace(load=lambda: cfg,
                                              transaction=transaction))


def _scheduler(monkeypatch, cfg=None):
    cfg = cfg or _Cfg()
    _install_cfg(monkeypatch, cfg)
    s = Scheduler()
    s._stop = threading.Event()
    return s


def _task(name="甲", mode="list"):
    return {"id": f"t{name}", "name": name, "mode": mode,
            "status": "pending", "enabled": True}


class _FakeEngine:
    """可定制状态/守卫的引擎替身（调度器测试用）。"""

    def __init__(self, state="done", running=False, guard=None,
                 stop_res=(True, "已停止"), release=(True, "")):
        self._state = state
        self._running = running
        self._guard = guard or {"state": "ok"}
        self._stop_res = stop_res
        self._release = release
        self.started = 0

    def start(self, **kw):
        self.started += 1
        return True, ""

    def status(self):
        return {"state": self._state, "running": self._running,
                "stage_before_stop": getattr(self, "_stage", None),
                "guard": self._guard,
                "stats": {"clicks": 0, "bought": 0, "spent": 0.0},
                "error": None}

    def guard_snapshot(self):
        return self._guard

    def stop(self, timeout=2.0):
        return self._stop_res

    def wait_stop(self, timeout=3.0):
        return True

    def force_release_input(self):
        return self._release

    def guard_resume(self):
        self._guard = {"state": "ok"}
        return dict(self._guard)

    def guard_stop(self):
        self._guard = {"state": "halted", "message": "已安全停止"}
        return dict(self._guard)


class _QuitOnJoinThread:
    """假引擎线程：join 之后才算退出（复现「停住了才会 abort」的真实顺序）。"""

    def __init__(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.alive = False


class _StuckInFire(Engine):
    """真实 Engine，但一启动就卡在开火阶段、心跳早已超时。"""

    def __init__(self):
        super().__init__()
        self._state = FIRE
        self._thread = _QuitOnJoinThread()
        self.start_calls = 0

    def status(self):
        return {"state": self._state,
                "running": self._thread.is_alive(),
                "stage_before_stop": self._stage_before_stop,
                "beat_age_s": 999.0,
                "guard": self._guard.snapshot(),
                "stats": {"clicks": 0, "bought": 0, "spent": 0.0},
                "error": None}

    def start(self, **kw):
        self.start_calls += 1
        return True, ""


def _patch_engine_start(monkeypatch):
    """让 Engine.start 在离线环境里能跑完（不找窗口/不抓屏）。"""
    monkeypatch.setattr(eng_mod, "find_game_window", lambda: 1)
    monkeypatch.setattr(eng_mod, "Mouse", types.SimpleNamespace(
        cursor=lambda: None,
        release_cursor_lock=lambda: (True, False),
        foreground=lambda: 1))
    monkeypatch.setattr(eng_mod.Engine, "_validate",
                        lambda self, cfg, mode, wf: "")
    monkeypatch.setattr(eng_mod.Config, "load",
                        staticmethod(lambda: {"trigger_mode": "countdown",
                                              "workflow": "detail"}))


# ------------------------------------------------------------------ B01

def test_stop_records_fire_stage_before_abort():
    """stop() 把状态改成 abort 之前，必须留下「真的在开火」这一事实。"""
    e = Engine()
    e._state = FIRE
    e._thread = _QuitOnJoinThread()
    ok, _msg = e.stop(timeout=0.01)
    assert ok is True
    st = e.status()
    assert st["state"] == ABORT
    assert st["stage_before_stop"] == FIRE, \
        "stop() 抹掉了开火阶段，调度器会误判成「还没下单、可以重跑」"


def test_watchdog_stall_in_fire_is_not_redispatched(monkeypatch):
    """看门狗在开火阶段打断：任务不得自动重跑（否则同一件买两次）。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    monkeypatch.setattr(sch, "sync_template", lambda cfg, name, tid: True)
    eng = _StuckInFire()
    s._engine = eng

    s._run_task(s.tasks()[0])

    t = s.tasks()[0]
    assert eng.start_calls == 1, f"开火阶段被打断后自动重跑了 {eng.start_calls} 次"
    assert t["status"] == "failed"
    assert t.get("needs_human") is True
    assert "开火" in (t.get("result") or "") or "fire" in (t.get("result") or "")
    assert s._halt_reason, "硬停没有锁住队列，下一个任务会接着点"


# ------------------------------------------------------------------ B02

def test_guard_halt_survives_engine_start(monkeypatch):
    """硬停之后下一轮启动不许自行 user_resume。"""
    _patch_engine_start(monkeypatch)
    e = Engine()
    e._guard.hard_halt("用户请求停止")
    ok, msg = e.start(mode="list", workflow="list")
    assert ok is False
    assert "停止" in msg
    assert e._guard.halt_latched is True
    assert e._guard.snapshot()["containment_pending"] is True


def test_scheduler_does_not_dispatch_when_guard_halted(monkeypatch):
    """派发下一个任务之前必须先看守卫（B02 / M-3）。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    s.add("乙", mode="list", max_price=3000)
    ran = []
    monkeypatch.setattr(s, "_run_task", lambda t: ran.append(t["id"]) or "ok")
    s._engine = _FakeEngine(state="abort", running=False,
                            guard={"state": "halted", "message": "已安全停止"})

    assert s._run_step() is True          # 队列被锁住，但不能收工/空转
    assert ran == [], f"守卫硬停后仍然派发了：{ran}"
    assert s._halt_reason
    assert s._halt_hard is True


# ------------------------------------------------------------------ B03

def test_guard_containment_cleared_only_after_cleanup():
    """安全收尾真的跑完才清 pending，前端按钮才可用。"""
    e = Engine()
    snap = e.guard_stop()
    assert snap["containment_pending"] is False, \
        "containment_done() 没有生产调用者，按钮永远灰着"
    assert e.status()["guard"]["containment_ok"] is True


def test_guard_stop_does_not_overwrite_done():
    """任务已完成时，「停止」不许把它改成 abort（M-4）。"""
    e = Engine()
    e._state = DONE
    e.guard_stop()
    assert e.status()["state"] == DONE


# ------------------------------------------------------------------ B04 / B05

def test_waiting_human_auto_resumes_when_goal_met():
    """人工段目标已达成 → 不用等用户点按钮，自己续跑。"""
    g = SceneGuard(GuardConfig(clear_ticks=2, clear_window_s=0.0))
    g.force(SC_INSUFFICIENT, now=0.0)
    assert g.state == sg.STATE_WAITING_HUMAN
    good = Signals(page="onsale", confirm_found=False, purchase_blocked=True,
                   quota_left=2, balance=10000.0, price=100.0)
    g.feed(good, now=1.0)
    assert g.state == sg.STATE_WAITING_HUMAN, "一拍就清场，滞回没了"
    g.feed(good, now=2.0)
    assert g.state == sg.STATE_OK, "目标达成后 WAITING_HUMAN 永不自动续跑"


def test_recovering_is_settled_when_round_ends():
    """RECOVERING 不能成为引擎退出后的永久悬挂态（B05）。"""
    g = SceneGuard(GuardConfig())
    g.force(SC_CLICK_NO_EFFECT, now=0.0)
    assert g.state == sg.STATE_RECOVERING
    g.abandon_recovery("本轮结束", now=1.0)
    assert g.state in (sg.STATE_WAITING_HUMAN, sg.STATE_HALTED)


# ------------------------------------------------------------------ B14

def test_unconfirmed_jitter_ticks_are_not_treated_as_normal():
    """抖动帧没到 min_confirm：不能算进清场（否则两次「假正常」就放行）。"""
    cfg = GuardConfig(clear_ticks=2, clear_window_s=0.0)
    g = SceneGuard(cfg)
    g.force(SC_CLICK_NO_EFFECT, now=0.0)
    assert g.state == sg.STATE_RECOVERING
    jitter = Signals(page="onsale", click_effect_age_s=0.5,
                     frame_changed=False, frame_dt_p95_ms=16.0,
                     countdown_conf=0.95, ocr_fail_streak=0)
    for t in (1.0, 2.0, 3.0, 4.0):
        g.feed(jitter, now=t)
        assert g.state == sg.STATE_RECOVERING, \
            f"第 {t}s 的未确认抖动帧被当成正常，异常被吞了"
    assert g.snapshot()["ok_ticks"] == 0


def test_reentry_does_not_reset_the_total_timer():
    """同一场景重复上报不能重置总计时（实测 44.5s 回退到 5.0s）。"""
    g = SceneGuard(GuardConfig())
    g.force(SC_CLICK_NO_EFFECT, now=0.0)
    g.tick(now=10.0)                      # 升一级，证明它确实在恢复
    g.force(SC_CLICK_NO_EFFECT, now=20.0)  # 抖动重入
    g.tick(now=65.0)                      # 总时长 65s ≥ 60s 预算
    assert g.state == sg.STATE_WAITING_HUMAN, \
        "重入把 _total_since 拨回 20s，total_timeout 永远到不了"


# ------------------------------------------------------------------ B08 / B09

def test_engine_start_resets_task_residue(monkeypatch):
    """切任务不许带着上一轮的连续失败计数和清单页残留。"""
    _patch_engine_start(monkeypatch)
    monkeypatch.setattr(eng_mod.Engine, "_run",
                        lambda self, cfg, mode, wf: None)
    e = Engine()
    e._fails_in_row = 3
    e._rows = [{"name": "上一件"}]
    ok, msg = e.start(mode="list", workflow="list")
    assert ok is True, msg
    assert e._fails_in_row == 0
    assert e._rows == []


def test_template_borrow_is_returned(monkeypatch, tmp_path):
    """任务模板只进不出会把下一条任务的 preflight 骗过去（B09）。"""
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "buy.png").write_bytes(b"GLOBAL")
    cfg = _Cfg(tpl_dir=str(tpl))
    _install_cfg(monkeypatch, cfg)
    s = Scheduler()
    s._stop = threading.Event()
    task = _task()
    saved = s._borrow_config(task)

    # 模拟 _drive_task 把任务模板拷到全局位置
    (tpl / "buy.png").write_bytes(b"TASK-OF-A")
    s._return_config(saved)

    assert (tpl / "buy.png").read_bytes() == b"GLOBAL", \
        "任务模板永久留在全局位置，下一条任务会用上一条的模板"


# ------------------------------------------------------------------ R01 #3/#4

def test_scheduler_stop_keeps_task_when_engine_will_not_quit(monkeypatch):
    """引擎没真停：不许报「已停止」，也不许把任务放回 PENDING 再开一轮。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    s.update(s.tasks()[0]["id"], status="running")
    s._engine = _FakeEngine(state="fire", running=True,
                            stop_res=(False, "引擎线程还在收尾"))

    ok, msg = s.stop()
    assert ok is False
    assert "停止" in msg and "引擎" in msg
    assert s.tasks()[0]["status"] == "running", \
        "假成功把任务放回 PENDING，下一轮会和旧引擎并行点同一个鼠标"
    assert s._halt_reason


def test_scheduler_stop_reports_input_release_failure(monkeypatch):
    """退出前必须确认输入已释放；释放不了要显式失败（R01 #4）。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    s._engine = _FakeEngine(state="done", running=False,
                            release=(False, "键盘状态没确认释放"))
    ok, msg = s.stop()
    assert ok is False
    assert "键盘" in msg
    assert s._halt_reason


def test_scheduler_resume_restarts_the_queue(monkeypatch):
    """用户点「继续」后，队列要真的接着跑（B04/M-12）。"""
    s = _scheduler(monkeypatch)
    s.add("甲", mode="list", max_price=3000)
    s._engine = _FakeEngine(state="abort", running=False,
                            guard={"state": "halted", "message": "已安全停止"})
    called = []
    monkeypatch.setattr(s, "start", lambda: (called.append(1), (True, ""))[1])
    out = s.guard_resume()
    assert called == [1], "点了继续，调度器没被重新拉起来"
    assert out["queue_running"] is True
    assert s._halt_reason == ""


# ------------------------------------------------------------------ R01 #1/#2

def test_scheduler_start_is_atomic_under_barrier(monkeypatch, tmp_path):
    """6 线程同时 start()：只能有 1 条调度线程、1 个 Engine。"""
    made = []

    class _DummyEngine:
        def __init__(self):
            made.append(self)
            self.on_hit = None

        def status(self):
            return {"state": "idle", "running": False,
                    "guard": {"state": "ok"}}

        def guard_snapshot(self):
            return {"state": "ok"}

        def stop(self, timeout=2.0):
            return True, "已停止"

        def force_release_input(self):
            return True, ""

    tpl = tmp_path / "tpl"
    tpl.mkdir()
    (tpl / "buy.png").write_bytes(b"x")
    (tpl / "confirm.png").write_bytes(b"x")
    cfg = _Cfg(queue=[_task()], tpl_dir=str(tpl))
    _install_cfg(monkeypatch, cfg)
    monkeypatch.setattr(sch, "Engine", _DummyEngine)
    s = Scheduler()
    s._stop = threading.Event()
    monkeypatch.setattr(s, "_run", lambda: s._stop.wait(1.0))

    n = 6
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        r = s.start()
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    ok_count = sum(1 for ok, _ in results if ok)
    assert ok_count == 1, f"{n} 线程同时 start() 起了 {ok_count} 条调度：{results}"
    assert len(made) == 2, f"构造了 {len(made)} 个 Engine（1 基线 + 1 启动才是对的）"
    assert s._thread is not None and s._thread.is_alive()
    s.stop()
    assert not s._thread.is_alive()


def test_engine_start_is_atomic_under_barrier(monkeypatch):
    """8 线程同时 Engine.start()：只有 1 条引擎线程，其余明确拒绝。"""
    _patch_engine_start(monkeypatch)
    e = Engine()
    monkeypatch.setattr(eng_mod.Engine, "_run",
                        lambda self, cfg, mode, wf: self._stop.wait(0.5))

    n = 8
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        r = e.start(mode="list", workflow="list")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    ok_count = sum(1 for ok, _ in results if ok)
    refused = [msg for ok, msg in results if not ok]
    assert ok_count == 1, f"{n} 线程同时 start() 全部返回「已启动」：{results}"
    assert all("启动" in m or "运行" in m for m in refused), refused
    assert e._thread.is_alive()
    e.stop(timeout=1.0)


def test_engine_stop_is_honest_when_thread_will_not_quit():
    """停不下来必须显式失败，不许返回假成功（R01 #3）。"""
    e = Engine()
    e._state = WATCH

    class _Immortal:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    e._thread = _Immortal()
    ok, msg = e.stop(timeout=0.01)
    assert ok is False
    assert "收尾" in msg
    assert e.status()["state"] == WATCH, "线程还在跑就把状态改成 abort，等于假装停了"
    assert e.status()["running"] is True


# ------------------------------------------------------------------ R01 #5

def test_pagewatch_start_is_idempotent_under_barrier(monkeypatch):
    """6 线程同时打开 watcher：只能起 1 条轮询。"""
    w = PageWatcher(interval=0.05)
    entered = []

    def loop():
        entered.append(1)
        w._stop.wait(0.5)

    monkeypatch.setattr(w, "_loop", loop)
    n = 6
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        w.start()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert len(entered) == 1, f"{n} 次并发 start() 起了 {len(entered)} 条轮询"
    w.stop()


def test_pagewatch_records_loop_errors(monkeypatch):
    """轮询异常不能完全静默（以前 pass 掉，stale 永远 false）。"""
    w = PageWatcher(interval=0.01)

    def boom():
        raise RuntimeError("抓屏失败")

    monkeypatch.setattr(w, "_refresh_once", boom)
    w.start()
    try:
        deadline = time.time() + 1.0
        while time.time() < deadline and w.snapshot().get("errors", 0) < 1:
            time.sleep(0.02)
        snap = w.snapshot()
        assert snap["errors"] >= 1, "轮询一直失败却一点记录都没有"
        assert "抓屏失败" in (snap["error"] or "")
    finally:
        w.stop()


# ------------------------------------------------------------------ F4：借调配置失败必须收场

class _FlakyCfg(_Cfg):
    """可注入落盘失败的配置替身：第 N 次 save 起 / 指定某一次抛 OSError。"""

    def __init__(self, queue=None, tpl_dir=None):
        super().__init__(queue, tpl_dir)
        self.saves = 0
        self.fail_at = set()
        self.fail_from = None

    def save(self):
        self.saves += 1
        if self.saves in self.fail_at or (
                self.fail_from is not None and self.saves >= self.fail_from):
            raise OSError("模拟磁盘只读（WinError 5）")


def _install_flaky(monkeypatch, cfg):
    """和 `Config.transaction()` 一样：写盘失败就回滚内存里的这次改动。"""

    @contextlib.contextmanager
    def tx():
        snap = copy.deepcopy(dict(cfg))
        try:
            yield cfg
            cfg.save()
        except Exception:
            cfg.clear()
            cfg.update(snap)
            raise

    monkeypatch.setattr(sch.Config, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(sch.Config, "transaction", staticmethod(tx))
    # 界面轮询走的是新增的 `Config.load_cached`（盘上 stat 没变就复用快照，
    # 避开落盘锁）；只打桩 `load` 的话 `Scheduler.tasks()` 读的是真盘，不是
    # 上面的 `_FlakyCfg`，拿到的任务 id 和 `cfg["queue"]` 里的对不上。
    monkeypatch.setattr(sch.Config, "load_cached",
                        classmethod(lambda cls, **kw: cfg))
    return cfg


def test_borrow_config_failure_settles_task_not_running(monkeypatch):
    """借调配置落盘失败：任务必须收场（FAILED + 原因），绝不停在 RUNNING。"""
    cfg = _FlakyCfg()
    _install_flaky(monkeypatch, cfg)
    s = Scheduler()
    s._stop = threading.Event()
    s.add("甲", mode="list", max_price=3000)
    tid = s.tasks()[0]["id"]
    # add() 已经写过 1 次盘：本次 RUNNING 是第 2 次（成功），
    # 借调配置退出事务是第 3 次 —— 让它失败。
    cfg.fail_at = {cfg.saves + 2}

    s._run_task(s.tasks()[0])

    t = next(x for x in cfg["queue"] if x["id"] == tid)
    assert t["status"] == "failed", \
        f"借调配置失败后任务停在 {t['status']}，没有任何人收场"
    assert "借用配置失败" in (t.get("result") or "")
    assert s._active_id is None
    print(f"[F4-a] status={t['status']} result={t.get('result')} "
          f"active={s._active_id} halt={s._halt_reason!r}")


def test_borrow_config_write_failure_halts_and_rolls_back_on_restart(monkeypatch):
    """盘彻底写不进去：当前只能锁队列；重启时孤儿回收要回滚成可重试。"""
    cfg = _FlakyCfg()
    _install_flaky(monkeypatch, cfg)
    s = Scheduler()
    s._stop = threading.Event()
    s._engine = _FakeEngine(state="idle", running=False)
    s.add("甲", mode="list", max_price=3000)
    tid = s.tasks()[0]["id"]
    # RUNNING 那一次（第 2 次）成功，从第 3 次（借调）起全部失败
    cfg.fail_from = cfg.saves + 2

    s._run_task(s.tasks()[0])

    t = next(x for x in cfg["queue"] if x["id"] == tid)
    assert t["status"] == "running"          # 盘写不进去，RUNNING 只能停在内存里
    during = t["status"]
    assert s._halt_reason and "借用配置失败" in s._halt_reason
    assert s._halt_hard is True

    # 模拟重启：盘恢复可写，`_recover_orphans` 把它回滚为可重试的 pending
    cfg.fail_from = None
    s._thread = None
    fixed = s._recover_orphans()
    t = next(x for x in cfg["queue"] if x["id"] == tid)
    assert fixed == 1
    assert t["status"] == "pending", "重启后仍然留在 RUNNING"
    print(f"[F4-b] 写盘失败当场 status={during} halt={s._halt_reason!r} "
          f"hard={s._halt_hard} → 重启回收后 status={t['status']}")


# ------------------------------------------------------------------ R3：全局路线切换的语义

def _disk_cfg(tmp_path, monkeypatch, data):
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(tmp_path / "config.json"))
    c = Config(data)
    c.save()
    return c


def test_global_route_switch_rederives_inherited_tasks(tmp_path, monkeypatch):
    """没显式选过路线的排队任务要跟随全局；钉住的不跟随但必须标出来。"""
    base = {"workflow": "detail", "queue": [
        {"id": "a", "name": "跟随", "status": "pending", "enabled": True},
        {"id": "b", "name": "钉住", "status": "pending", "enabled": True,
         "workflow": "list"},
    ]}
    _disk_cfg(tmp_path, monkeypatch, base)

    first = Config.load()
    a, b = first["queue"][0], first["queue"][1]
    assert a["workflow_source"] == "global"
    assert a["workflow_effective"] == "detail"
    assert b["workflow_source"] == "task"
    assert b["workflow_effective"] == "list"
    assert b["route_stale"] is True

    cur = Config.load()
    cur["workflow"] = "countdown"
    cur._dirty.add("workflow")
    cur._normalize()
    cur.save()

    second = Config.load()
    a2, b2 = second["queue"][0], second["queue"][1]
    assert a2["workflow_effective"] == "countdown", \
        "未开始、没选过路线的任务没有跟随全局切换"
    assert a2["workflow_source"] == "global" and a2["route_stale"] is False
    assert b2["workflow_effective"] == "list", "显式路线被全局切换静默覆盖"
    assert b2["route_stale"] is True
    hint = Scheduler._with_route_hint(dict(b2))
    assert hint.get("route_hint", "").startswith("该任务使用旧路线"), \
        "钉住的路由和全局不一致却没有界面提示"
    print(f"[R3-a] 切全局 detail→countdown：a={a2['workflow_source']}/"
          f"{a2['workflow_effective']}/stale={a2['route_stale']}；"
          f"b={b2['workflow_source']}/{b2['workflow_effective']}/"
          f"stale={b2['route_stale']} hint={hint.get('route_hint')!r}")


def test_running_task_does_not_follow_global_switch(tmp_path, monkeypatch):
    """在跑的任务半路不换图；但同样要标 route_stale。"""
    base = {"workflow": "detail", "queue": [
        {"id": "c", "name": "在跑", "status": "running",
         "workflow": "detail", "workflow_explicit": False},
    ]}
    _disk_cfg(tmp_path, monkeypatch, base)

    cur = Config.load()
    cur["workflow"] = "list"
    cur._dirty.add("workflow")
    cur._normalize()
    cur.save()

    t = Config.load()["queue"][0]
    assert t["workflow_source"] == "frozen"
    assert t["workflow_effective"] == "detail", "在跑的任务被换成了新路线"
    assert t["route_stale"] is True
    print(f"[R3-b] 在跑任务切全局 detail→list：{t['workflow_source']}/"
          f"{t['workflow_effective']}/stale={t['route_stale']} "
          f"hint={Scheduler._with_route_hint(dict(t)).get('route_hint')!r}")


# ------------------------------------------------------------------ S9：看门狗真的接进调度

class _StallingEngine:
    """第一轮「某一步永久卡住」，第二轮正常完成。

    卡住的判定走**调度级看门狗**（心跳 1s，远小于 `_drive_task` 里那条
    60s 的旧阈值），这样才能证明 `scheduler.py` 真的用上了
    `core/watchdog.py`，而不是靠旧路径碰巧恢复。
    """

    def __init__(self, stage="watch", stall_age=1.0):
        self.stage = stage
        self.stall_age = stall_age
        self.started = 0
        self.stop_calls = 0
        self.guard_stop_calls = 0
        self.round = 0
        self.running = False
        self.done = False

    def start(self, **kw):
        self.started += 1
        self.round += 1
        self.running = True
        return True, ""

    def status(self):
        stuck = self.round == 1 and self.running
        state = "done" if (self.done or self.round > 1) else "watch"
        return {"state": state,
                "running": self.running,
                "stage_before_stop": self.stage if stuck else None,
                "beat_age_s": self.stall_age if stuck else 0.01,
                "guard": {"state": "ok"},
                "stats": {"clicks": 0, "bought": 0, "spent": 0.0},
                "error": None}

    def stop(self, timeout=2.0):
        self.stop_calls += 1
        if self.round == 1:
            self.running = False
        return True, "已停止"

    def wait_stop(self, timeout=3.0):
        return True

    def guard_snapshot(self):
        return {"state": "ok"}

    def guard_stop(self):
        self.guard_stop_calls += 1
        return {"state": "halted", "message": "已安全停止"}

    def force_release_input(self):
        return True, ""


def _arm_watch(s, step_s=0.2, round_s=5.0, confirm=2, poll_s=0.02):
    s._arm_watchdog({"watchdog": {"step_s": step_s, "round_s": round_s,
                                  "confirm": confirm, "poll_s": poll_s}})


def _wait_until(pred, timeout=2.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_watchdog_step_stall_fires_within_threshold_and_recovers(monkeypatch):
    """S9-1/4：某一步永久卡住 → 阈值内触发；开火前 → 自动重开并跑完。"""
    cfg = _Cfg()
    _install_cfg(monkeypatch, cfg)
    s = Scheduler()
    s._stop = threading.Event()
    s.add("甲", mode="list", max_price=3000)
    monkeypatch.setattr(sch, "sync_template", lambda c, name, tid: True)
    eng = _StallingEngine(stage="watch", stall_age=1.0)
    s._engine = eng
    _arm_watch(s)

    t0 = time.time()
    s._start_watchdog()
    try:
        s._run_task(s.tasks()[0])
    finally:
        s._stop_watchdog()
    total = time.time() - t0

    steps = [r for r in s._stall_records if r["kind"] == "step"]
    assert steps, "某一步永久卡住，看门狗一次都没触发"
    latency = steps[0]["at"] - t0
    assert latency <= 0.6, f"触发时延 {latency:.3f}s，阈值 0.2s×2 确认太慢"
    assert steps[0]["action"] == "retry"

    t = s.tasks()[0]
    assert eng.started == 2, f"卡死后没有自动重开（start={eng.started}）"
    assert t["status"] == "done", f"重开一轮后没跑完：{t['status']} {t.get('result')}"
    assert s._halt_reason == "", "开火前卡死不该锁队列"
    print(f"[S9] 单步卡死触发时延 {latency:.3f}s，整轮恢复耗时 {total:.2f}s")
    print("[S9] 卡死后自动恢复记录：")
    for row in s._log:
        if "看门狗" in row["msg"] or "重开" in row["msg"]:
            print(f"  {row['t']} [{row['level']}] {row['msg']}")


def test_watchdog_slow_step_is_not_killed(monkeypatch):
    """S9-2：只是慢、心跳还在跳 → 不能误杀。"""
    s = Scheduler()
    s._stop = threading.Event()
    eng = _StallingEngine(stall_age=0.01)   # 心跳一直新鲜 = 还在推进
    eng.running = True
    eng.round = 0
    s._engine = eng
    _arm_watch(s, step_s=0.1, round_s=5.0, confirm=2, poll_s=0.02)

    s._start_watchdog()
    try:
        time.sleep(0.6)                    # 超过 6 个 step 阈值
    finally:
        s._stop_watchdog()

    assert len(s._stall_records) == 0, \
        f"慢但没卡死被误判：{list(s._stall_records)}"
    assert eng.stop_calls == 0, "误杀：看门狗停了一个还在推进的引擎"
    assert s._halt_reason == ""


def test_watchdog_round_stall_safe_stops_with_readable_reason(monkeypatch):
    """S9-3：整轮卡住 → 走安全停机，原因可读，时延可测。"""
    s = Scheduler()
    s._stop = threading.Event()
    eng = _StallingEngine(stall_age=0.01)
    eng.running = True
    s._engine = eng
    _arm_watch(s, step_s=0.2, round_s=0.1, confirm=2, poll_s=0.02)

    s._hb.beat("tick")                     # 跳过第一次，之后整轮心跳停住
    t0 = time.time()
    s._start_watchdog()
    try:
        assert _wait_until(lambda: bool(s._halt_reason), timeout=1.5), \
            "整轮卡住没人发现"
        latency = time.time() - t0
    finally:
        s._stop_watchdog()

    assert latency <= 0.5, f"整轮卡住 {latency:.3f}s 才被发现"
    assert "整轮卡住" in s._halt_reason
    assert "安全停机" in s._halt_reason
    assert s._halt_hard is True, "整轮卡住没锁队列"
    assert eng.guard_stop_calls >= 1, "整轮卡住没有安全停机（引擎还在跑）"
    recs = [r for r in s._stall_records if r["kind"] == "round"]
    assert recs and recs[0]["action"] == "halt"
    snap = s.status()["watchdog"]
    assert snap["alive"] is False and snap["stalls"]
    print(f"[S9] 整轮卡死触发时延 {latency:.3f}s，原因：{s._halt_reason}")


def test_watchdog_step_stall_in_fire_never_retries(monkeypatch):
    """S9-5：接入看门狗也不许破坏「fire/confirm 不自动重跑」。"""
    cfg = _Cfg()
    _install_cfg(monkeypatch, cfg)
    s = Scheduler()
    s._stop = threading.Event()
    s.add("甲", mode="list", max_price=3000)
    monkeypatch.setattr(sch, "sync_template", lambda c, name, tid: True)
    eng = _StallingEngine(stage="fire", stall_age=1.0)
    s._engine = eng
    _arm_watch(s)

    s._start_watchdog()
    try:
        s._run_task(s.tasks()[0])
    finally:
        s._stop_watchdog()

    t = s.tasks()[0]
    assert eng.started == 1, f"开火阶段卡死后自动重跑了 {eng.started} 次"
    assert t["status"] == "failed"
    assert t.get("needs_human") is True
    assert "不自动重跑" in (t.get("result") or "")
    assert s._halt_reason and s._halt_hard is True

