"""8 项故障注入：每一项都要证明「会坏、但能自己回来、且不留脏状态」。

判定口径：
  · 预期行为 —— 设计上应该怎么退（中止/降级/重试，而不是死转或崩掉）
  · 实际行为 —— 真跑出来的状态、错误文案、用了多久
  · 恢复耗时 —— 虚拟秒（长跑里等于「真实几秒」的放大）
  · 脏状态 —— 记录有没有多写半条、文件能不能解析、线程/句柄有没有多出来
  · 自愈 —— 故障解除后再跑一轮，必须正常成交

用法：`python -m tools.stability.faults`
产出：`tools/stability/_results/<run>/faults.json` + `faults_table.md`
"""
import builtins
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import config, fire_proc, records, scheduler, timesync  # noqa: E402
from core import countdown, engine as engine_mod, pagewatch  # noqa: E402

from . import clock as clock_mod  # noqa: E402
from . import fakes, metrics, workload  # noqa: E402

RESULT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_results")


def _rows(path):
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except ValueError:
        return ["__bad_json__"]


def _clean(res):
    return (res["outcome"] == "hit" and not res["error"])


def _log_tail(eng, n=3):
    """引擎自述日志里的前几条 warn/error —— 用来证明「是被哪条熔断拦下的」。

    取**最早**的几条而不是最后几条：一个任务可能连试 3 件，最后一屏是
    第 3 件的兜底文案，而真正的熔断原因在第 1 件里。
    """
    try:
        rows = (eng.status() or {}).get("log") or []
    except Exception:                             # noqa: BLE001
        return ""
    picked = []
    for r in rows:
        if isinstance(r, dict) and r.get("level") in ("warn", "error"):
            picked.append(str(r.get("msg")))
        elif not isinstance(r, dict):
            picked.append(str(r))
        if len(picked) >= n:
            break
    return " / ".join(picked)


def occ_ocr_failure(ws, clock):
    """OCR 连续失败：应中止这一轮，而不是空转；恢复后下一轮正常。"""
    # 场景 A：一开机 OCR 就是坏的 —— 会被「6 秒读不出秒数」兜底拦下。
    before = len(_rows(records.PATH))
    ocr = fakes.FakeOcr(clock, clock.perf_counter() + 60, mode="raise")
    res, eng = workload.run_cycle(clock, ocr=ocr)
    after = len(_rows(records.PATH))
    err = (eng.status().get("error") or res["error"] or "")
    # 场景 B：跑了几秒之后 OCR 才挂（cd 里已经有读数）。这时「停更」不能
    # 抢答，必须由 OCR_ERROR_ABORT_S 熔断说明真实原因。
    ocr2 = fakes.FakeOcr(clock, clock.perf_counter() + 60,
                         fail_at=clock.perf_counter() + 3.0)
    res2, eng2 = workload.run_cycle(clock, ocr=ocr2)
    err2 = (eng2.status().get("error") or res2["error"] or "")
    recover, _ = workload.run_cycle(clock)
    return {
        "expected": "连续失败 6 秒后中止本轮（Aborted），不空转到天亮；下一轮自动恢复",
        "actual": f"开机即坏：outcome={res['outcome']}，{res['virt_s']}s 虚拟；"
                  f"错误：{err[:60]}；日志：{_log_tail(eng)} ｜ "
                  f"中途挂：outcome={res2['outcome']}，{res2['virt_s']}s 虚拟；"
                  f"错误：{err2[:60]}；日志：{_log_tail(eng2)}；"
                  f"恢复轮：{recover['outcome']}",
        "recovery_s": max(res["virt_s"], res2["virt_s"]),
        "dirty": _dirty(before, after, err + err2),
        "healed": _clean(recover) and res2["outcome"] != "hit",
    }


def occ_grab_failure(ws, clock):
    """截图失败：偶发应容忍，持续应中止且不刷爆 CPU。"""
    # 偶发：每 3 次失败 1 次
    transient, _ = workload.run_cycle(clock, screen=fakes.FakeScreen(fail_every=3))
    before = len(_rows(records.PATH))
    # 持续：全部失败
    screen = fakes.FakeScreen(fail_every=1)
    res, eng = workload.run_cycle(clock, screen=screen)
    after = len(_rows(records.PATH))
    err = eng.status().get("error") or res["error"] or ""
    recover, _ = workload.run_cycle(clock)
    return {
        "expected": "偶发失败继续盯；持续 10 秒抓不到画面则中止；不刷屏、不空转",
        "actual": f"偶发轮 outcome={transient['outcome']}；持续轮 outcome={res['outcome']}，"
                  f"{res['virt_s']}s 虚拟、{res['grabs']} 次抓取；错误：{err[:70]}；"
                  f"日志：{_log_tail(eng)}；恢复轮：{recover['outcome']}",
        "recovery_s": res["virt_s"],
        "dirty": _dirty(before, after, err),
        "healed": _clean(transient) and _clean(recover),
    }


def injection_rejected(ws, clock):
    """注入被拒绝：抛异常必须止住这一轮，不能写半条记录。"""
    before = len(_rows(records.PATH))
    res, eng = workload.run_cycle(
        clock, fire_error=OSError("SendInput 被拒绝（注入失败）"))
    after = len(_rows(records.PATH))
    err = eng.status().get("error") or res["error"] or ""
    bad = "__bad_json__" in _rows(records.PATH)
    recover, _ = workload.run_cycle(clock)
    return {
        "expected": "异常上抛到引擎边界 → 本轮中止；台账不出现半条/坏 JSON",
        "actual": f"outcome={res['outcome']}，{res['virt_s']}s 虚拟；错误：{err[:70]}；"
                  f"台账 +{after - before} 行；坏行={bad}；恢复轮：{recover['outcome']}",
        "recovery_s": res["virt_s"],
        "dirty": _dirty(before, after, err) if not bad else "台账出现坏 JSON",
        "healed": _clean(recover) and not bad,
    }


def ipc_peer_gone(ws, clock):
    """IPC 对端进程消失：fire 返回 False，restart() 能就地重开并恢复工作。"""
    fp = fire_proc.FireProcess()
    ok0 = fp.alive()
    t0 = time.perf_counter()
    ok_fire = fp.fire(time.perf_counter() + 0.15)
    got = fp.wait(timeout=2.0)
    try:
        fp.p.kill()
        fp.p.wait(timeout=2)
    except Exception:                     # noqa: BLE001
        pass
    time.sleep(0.1)
    dead_alive = fp.alive()
    fire_dead = fp.fire(time.perf_counter() + 0.15)
    t_restart = time.perf_counter()
    restarted = fp.restart()
    t_after = time.perf_counter()
    fire_after = fp.fire(time.perf_counter() + 0.15) if restarted else False
    got_after = fp.wait(timeout=2.0) if fire_after else None
    fp.close()
    recovered = bool(restarted and fire_after and got_after)
    return {
        "expected": "对端消失后 fire=False、wait 超时；restart 就地重开，恢复后能再次触发",
        "actual": f"启动={ok0}，首发生效={got is not None}，杀进程后 alive={dead_alive}、"
                  f"fire={fire_dead}；restart={restarted}（{(t_after - t_restart) * 1000:.0f}ms），"
                  f"恢复后 fire={fire_after}、生效={got_after is not None}",
        "recovery_s": round(t_after - t_restart, 3),
        "dirty": "无残留子进程" if not fp.alive() else "子进程仍在",
        "healed": recovered and not fp.alive(),
    }


def network_timeout(ws, clock):
    """网络超时：NTP 全不通时应快速失败、offset 归零，而不是一直阻塞。"""
    t0 = time.perf_counter()
    r = timesync.sync(servers=["127.0.0.1"], samples=1, timeout=1.0)
    cost = round(time.perf_counter() - t0, 2)
    off = timesync.offset()
    return {
        "expected": "每个服务器最多阻塞 timeout，随后 ok=False、offset=0；不抛异常",
        "actual": f"ok={r['ok']}，耗时 {cost}s，offset={off}，error={r.get('error')}",
        "recovery_s": cost,
        "dirty": "状态已归零（offset=0）" if off == 0.0 else f"offset 未归零：{off}",
        "healed": (not r["ok"]) and off == 0.0,
    }


def log_write_denied(ws, clock):
    """日志/台账写盘失败：只丢一条记录，不能把抢购线程打死。"""
    orig = builtins.open
    target = records.PATH

    def deny(path, *a, **k):
        if str(path) == str(target):
            raise PermissionError(13, "Permission denied（注入）", str(path))
        return orig(path, *a, **k)

    before = len(_rows(target))
    builtins.open = deny
    try:
        got = records.add(name="注入测试", price=99, result="hit")
    finally:
        builtins.open = orig
    after = len(_rows(target))
    ok_after = records.add(name="恢复测试", price=99, result="hit")
    return {
        "expected": "add() 返回 None（不抛）；未写入半条；恢复后能正常追加",
        "actual": f"写失败返回={got}，台账 {before}→{after} 行；恢复写入={bool(ok_after)}",
        "recovery_s": 0.0,
        "dirty": "无" if after == before else f"多写 {after - before} 行",
        "healed": got is None and after == before and ok_after is not None,
    }


def bad_config(ws, clock):
    """配置读到坏文件：隔离坏文件并从最近一次正常备份恢复，队列不能凭空消失。"""
    tmp = tempfile.mkdtemp(prefix="dafors_cfg_")
    old_path = config.CONFIG_PATH
    old_good = config.CONFIG_PATH + ".good"
    config.CONFIG_PATH = os.path.join(tmp, "config.json")
    try:
        c = config.Config()
        cfg = c
        # 用**已知键**做探针：Config 会丢弃未知键，随便加一个 key_probe
        # 会被规范化吃掉，测出来的「没恢复」是假阳性。
        cfg["regions"]["clock"] = {"x": 777, "y": 1, "w": 2, "h": 3}
        cfg._save_locked()
        with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write('{"regions": {"clock": {"x": 1,,}}')   # 坏 JSON
        cfg2, ok2 = config.Config._load_locked()
        probe = (cfg2.get("regions") or {}).get("clock", {}).get("x")
    finally:
        config.CONFIG_PATH = old_path
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "expected": "坏文件进隔离区；有 .good 备份则恢复原配置（含队列/区域），ok=True",
        "actual": f"ok={ok2}，load_error={getattr(cfg2, '_load_error', '')[:60]}，"
                  f"探针值恢复={probe}",
        "recovery_s": 0.0,
        "dirty": "坏文件已隔离，备份完好" if probe == 777 else "备份未恢复",
        "healed": bool(ok2 and probe == 777),
    }


def thread_stuck(ws, clock):
    """关键线程卡住：心跳停 → 看门狗确认卡死；点过购买后不自动重跑（防重复下单）。"""
    eng = engine_mod.Engine()
    eng._hb.beat()
    stop = threading.Event()

    def _stuck():
        stop.wait()                       # 不再 beat，模拟卡在同步调用里

    th = threading.Thread(target=_stuck, daemon=True)
    th.start()
    time.sleep(0.35)
    wd = engine_mod.watchdog.Watchdog(eng._hb, limit_s=0.2, confirm_times=3,
                                     cooldown_s=0.5)
    fired = [wd.poll() for _ in range(4)]
    stop.set()
    th.join(timeout=1)

    # 调度器判定路径：拿一个最小替身把 `_drive_task` 真跑一遍
    class FakeEngine:
        def __init__(self, state):
            self.state = state
            self.stopped = False

        def start(self, **kw):
            return True, ""

        def status(self):
            return {"state": self.state, "running": True, "beat_age_s": 999,
                    "stats": {"bought": 0, "spent": 0}}

        def stop(self):
            self.stopped = True

    class FakeSched:
        _stop = threading.Event()
        _engine = None

        def _say(self, *a):
            pass

        def _label(self, task):
            return "测试任务"

        def _set_status(self, *a):
            pass

        def update(self, *a, **k):
            pass

        def _engine_reason(self, st):
            return ""

    sc1 = FakeSched()
    sc1._engine = FakeEngine("watch")
    # `_drive_task` 会读全局配置、同步模板、启动引擎 —— 这三样都指向真实
    # 项目文件/真实桌面，故障注入里必须替身掉，否则会改到用户配置。
    old_load = scheduler.Config.load
    old_sync = scheduler.sync_template
    scheduler.Config.load = staticmethod(lambda: fakes.FakeCfg())
    scheduler.sync_template = lambda cfg, name, tid: True
    try:
        out_watch = scheduler.Scheduler._drive_task(
            sc1, {"id": "t1", "name": "x"}, "countdown", "countdown")
        sc2 = FakeSched()
        sc2._engine = FakeEngine("fire")
        out_fire = scheduler.Scheduler._drive_task(
            sc2, {"id": "t2", "name": "x"}, "countdown", "countdown")
    finally:
        scheduler.Config.load = old_load
        scheduler.sync_template = old_sync

    # 恢复：重启后跑一轮，只应多出一条记录（不能重复买）
    before = len(_rows(records.PATH))
    res, eng2 = workload.run_cycle(clock)
    after = len(_rows(records.PATH))
    return {
        "expected": "心跳停 → 看门狗触发一次（冷却期内不重复）；watch 阶段可重开，"
                    "fire/confirm 阶段不自动重跑；恢复后只记一条",
        "actual": f"watchdog 触发={fired}，drive(watch)={out_watch}，"
                  f"drive(fire)={out_fire}，恢复轮={res['outcome']}，台账 +{after - before}",
        "recovery_s": 0.35,
        "dirty": "无重复记录" if after - before == 1 else f"记录 +{after - before}",
        "healed": out_watch == "stalled" and out_fire == "stalled_no_retry"
        and _clean(res) and (after - before) == 1,
    }


def _dirty(before, after, err):
    if after != before:
        return f"台账多写 {after - before} 行"
    if "__bad_json__" in _rows(records.PATH):
        return "台账有坏 JSON"
    return "无"


SCENARIOS = [
    ("OCR 连续失败", occ_ocr_failure),
    ("截图/屏幕捕获失败", occ_grab_failure),
    ("注入失败或被拒绝", injection_rejected),
    ("IPC 对端进程消失", ipc_peer_gone),
    ("网络请求超时", network_timeout),
    ("日志写盘失败（权限/磁盘满）", log_write_denied),
    ("配置读到坏文件", bad_config),
    ("关键线程被卡住", thread_stuck),
]


def main():
    stamp = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(RESULT_ROOT, f"faults_{stamp}")
    os.makedirs(outdir, exist_ok=True)
    ws = fakes.TempWorkspace("faults")
    workload.apply_workspace(ws)
    clock = clock_mod.VirtualClock(sleep_step=0.05)
    metrics.track_files()
    base_threads = threading.active_count()
    base_files = metrics.file_handles()

    results = []
    with clock_mod.install([engine_mod, countdown, records, pagewatch,
                            scheduler], clock):
        for name, fn in SCENARIOS:
            t0 = time.perf_counter()
            try:
                r = fn(ws, clock)
            except Exception as e:               # noqa: BLE001
                r = {"expected": "-", "actual": f"注入脚本自身异常：{e}",
                     "recovery_s": None, "dirty": "未知", "healed": False}
            r["name"] = name
            r["wall_s"] = round(time.perf_counter() - t0, 2)
            results.append(r)
            print(f"[故障注入] {name}: {'PASS' if r['healed'] else 'FAIL'} "
                  f"({r['wall_s']}s)")

    tail = {
        "threads_before": base_threads,
        "threads_after": threading.active_count(),
        "open_files_before": base_files,
        "open_files_after": metrics.file_handles(),
        "dirty_files": [],
    }
    for name in os.listdir(ws.data):
        p = os.path.join(ws.data, name)
        if os.path.isfile(p):
            tail["dirty_files"].append(name)
    # 临时日志/records 目录里不该有 .tmp 残留
    tail["tmp_left"] = [n for n in tail["dirty_files"] if n.endswith(".tmp")]

    with open(os.path.join(outdir, "faults.json"), "w", encoding="utf-8") as f:
        json.dump({"scenarios": results, "tail": tail,
                   "workspace": ws.root}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(outdir, "faults_table.md"), "w", encoding="utf-8") as f:
        f.write("| 故障 | 预期 | 实际 | 恢复 | 脏状态 | 自愈 |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['name']} | {r['expected']} | {r['actual']} | "
                    f"{r['recovery_s']} | {r['dirty']} | "
                    f"{'PASS' if r['healed'] else 'FAIL'} |\n")
    ok = all(r["healed"] for r in results) and not tail["tmp_left"]
    print(f"\n故障注入总判定：{'全部自愈' if ok else '有未通过项'}")
    print(f"明细：{outdir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
