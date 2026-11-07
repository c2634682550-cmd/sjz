"""OCR 服务管家的行为契约：蓝绿重启、等待空闲、退让。

这个模块的价值全在「重启的时候调用方不能失败」上，所以测试也围绕这一条：
  · 新实例没验活 → 不切、不杀老的
  · 新实例验活 → 先切客户端、再杀老的（顺序反了就有空窗）
  · 备用端口被占 → 放弃这次重启，别去踩别人的进程
  · 正在抢购 → 等空闲；等太久才照做

真实实现会去起 paddlex.exe、连 GPU，测试里全部换成假的 —— 只验调度逻辑。
"""
import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ocr, ocr_service  # noqa: E402


class FakeProc:
    def __init__(self, port):
        self.port = port
        self.pid = 10000 + port
        self.killed = False

    def terminate(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0 if self.killed else None


@pytest.fixture
def env(monkeypatch):
    """造一套假环境：端口表、进程表、客户端地址可观测。"""
    state = {
        "up": {8081},          # 哪些端口「有服务」
        "spawned": [],
        "attempts": [],        # 拉起尝试（含失败）
        "killed": [],
        "probe_ok": True,
        "spawn_error": None,
        "busy": False,
    }
    monkeypatch.setattr(ocr_service, "_tcp_ok",
                        lambda host, port, timeout=0.4: port in state["up"])
    monkeypatch.setattr(ocr, "_settings",
                        {"ppocr": {"url": "http://127.0.0.1:8081"}}, raising=False)
    monkeypatch.setattr(ocr, "_backend", None, raising=False)

    def spawn(port):
        state["attempts"].append(port)
        if state["spawn_error"]:
            raise state["spawn_error"]
        state["spawned"].append(port)
        if state["probe_ok"]:
            state["up"].add(port)          # 起得来 → 端口开始有响应
        return FakeProc(port)

    def probe(url):
        return state["probe_ok"]

    def kill(proc):
        state["killed"].append(proc.port)
        state["up"].discard(proc.port)

    def kill_port(port):
        # **必须替掉**：真实现会 taskkill 掉监听这个端口的进程 ——
        # 测试里那个端口上跑的是真的 OCR 服务，一杀后面全组用例都失败。
        state["killed"].append(port)
        state["up"].discard(port)

    def make(**cfg):
        log = cfg.pop("log", None)
        return ocr_service.Supervisor(
            settings={"enabled": True, "healthy_timeout_s": 1.0,
                      "idle_wait_s": 0, "check_interval_s": 999, **cfg},
            is_busy=lambda: state["busy"],
            spawn=spawn, probe=probe, kill=kill, kill_port=kill_port,
            log=log)

    yield state, make

    # 这些用例会改 ocr 的全局配置（set_url / _backend），也会装一个假管家。
    # 收尾必须显式复位，否则后面跑真截图用例时会拿着一个指向假地址、
    # 或者带着冷却状态的客户端，看起来像「识别坏了」。
    ocr.reload()
    ocr_service.install(None)


def test_restart_switches_client_then_kills_the_old_one(env):
    """蓝绿的正序：新实例验活 → 切客户端 → 杀老的。

    顺序反了就会出现一段「没有服务」的窗口，抢购正好在那时候读倒计时
    就是直接失败。
    """
    state, make = env
    svc = make()
    svc._port = 8081
    svc._proc = FakeProc(8081)
    svc._started_at = 1.0

    assert svc.restart("测试") is True
    assert ocr.current_url() == "http://127.0.0.1:8082", "客户端没切过去"
    assert state["killed"] == [8081], "老实例没被收掉"
    assert svc.status()["port"] == 8082
    assert svc.status()["restarts"] == 1


def test_new_instance_that_never_gets_healthy_leaves_the_old_one_alone(env):
    """新实例起不来就原地不动 —— 老的还在服务，调用方什么都察觉不到。"""
    state, make = env
    state["probe_ok"] = False
    svc = make()
    svc._port = 8081
    svc._proc = FakeProc(8081)

    assert svc.restart("测试") is False
    assert ocr.current_url() == "http://127.0.0.1:8081", "不该切过去"
    # 起不来的新实例要被收掉，老实例必须留着继续服务
    assert state["killed"] == [8082], f"收拾错了：{state['killed']}"
    assert 8081 in state["up"]
    assert "没验活" in (svc.status()["last_error"] or "")


def test_port_answering_from_another_instance_is_not_success(env):
    """端口通了、但应声的不是我起的那个进程 —— 不许当成功。

    真机踩过：手动起的实例占着 8081，管家起的新实例绑不上端口、加载完就退，
    探针却探到了别人那个实例，于是判「验活成功」：客户端切过去，`_proc` 里
    记的是一个已经死掉的句柄，后面「杀老实例」只能靠端口去猜。
    """
    state, make = env
    svc = make()
    svc._port = 8081
    svc._proc = FakeProc(8081)

    dead = FakeProc(8082)
    dead.killed = True                  # 起来就退了（端口被别人抢走）

    def spawn(port):
        state["spawned"].append(port)
        state["up"].add(port)           # 端口有人应 —— 但不是它
        return dead

    svc._spawn = spawn
    assert svc.restart("测试") is False
    assert ocr.current_url() == "http://127.0.0.1:8081", \
        "不该切到一个不是自己起的实例上"
    assert 8081 in state["up"], "老实例必须留着继续服务"
    assert "没验活" in (svc.status()["last_error"] or "")


def test_cold_start_reclaims_an_orphan_on_the_spare_port(env):
    """冷启动时备用端口上留着的旧实例要收掉。

    场景：上次蓝绿切到 8082 之后进程被强杀，下次启动配置里的 8081 是空的、
    8082 上还蹲着一个。不去管它的话，新实例要跟它抢显存，加载慢一截，
    而且这一份永远不会再被切回来 —— 跑一天能堆出好几个。
    """
    state, make = env
    state["up"] = {8082}                 # 配置端口 8081 空着，8082 上有孤儿
    svc = make()

    assert svc.restart("冷启动") is True
    assert 8082 in state["killed"], "备用端口上的孤儿没被收掉"
    assert state["spawned"] == [8081], "冷启动要起在配置的端口上"
    assert ocr.current_url() == "http://127.0.0.1:8081"


def test_orphan_is_left_alone_when_kill_adopted_is_off(env):
    """kill_adopted 关掉时不许动别人的进程 —— 这是用户自己起的。"""
    state, make = env
    state["up"] = {8082}
    svc = make(kill_adopted=False)

    assert svc.restart("冷启动") is True
    assert 8082 not in state["killed"]
    assert 8082 in state["up"]


def test_kill_port_only_touches_our_own_processes():
    """按端口找 PID 再杀之前，先认映像名。

    实测撞到过：8081 上响应的根本不是我们的服务（被别的东西占着）。要是
    管家照旧 taskkill，就是把别人的进程杀了。宁可漏收一个孤儿，也不能误杀。
    """
    assert ocr_service._is_ocr_exe(r"C:\x\.venv-vl\Scripts\paddlex.exe")
    assert ocr_service._is_ocr_exe(r"C:\Python311\python.exe")
    assert not ocr_service._is_ocr_exe(r"C:\Python311\pythonw.exe"), "那是控制台自己"
    assert not ocr_service._is_ocr_exe(r"C:\Other\proxy.exe")
    assert not ocr_service._is_ocr_exe(None), "认不出来就别动手"


def test_refuses_when_the_spare_port_is_taken(env):
    """备用端口被别人占着就放弃 —— 别去踩一个不认识的进程。"""
    state, make = env
    state["up"].add(8082)
    svc = make()
    svc._port = 8081
    assert svc.restart("测试") is False
    assert state["spawned"] == []
    assert "已被占用" in (svc.status()["last_error"] or "")


def test_spawn_failure_is_reported_not_raised(env):
    """起进程失败要如实记下来，不能把管家线程带崩。"""
    state, make = env
    state["spawn_error"] = FileNotFoundError("没有 paddlex.exe")
    svc = make()
    svc._port = 8081
    assert svc.restart("测试") is False
    assert "起不来" in (svc.status()["last_error"] or "")


def test_tick_restarts_when_the_service_is_gone(env):
    """探活失败 = 服务没了：原地重起，不等空闲、也不走蓝绿。

    端口已经空了，没有「不能断流」的问题，直接在原端口起最快 ——
    换端口反而会让配置和实际对不上，下次启动还得靠兜底接管。
    """
    state, make = env
    state["up"].discard(8081)
    svc = make()
    svc._port = 8081
    svc._started_at = 1.0
    assert svc.tick() is True
    assert state["spawned"] == [8081], f"应该在原端口起：{state['spawned']}"
    assert svc.status()["port"] == 8081


def test_tick_restarts_when_recognition_gets_slow(env, monkeypatch):
    """最近中位耗时超阈值 → 重启。

    实测跑 22 小时后单次从 150ms 涨到 2.4s，整屏直接超时返回空。
    这条就是给那种退化准备的。
    """
    state, make = env
    monkeypatch.setattr(ocr, "metrics", lambda: {"ok": True, "recent_ms": 2400.0})
    svc = make(slow_ms=800.0)
    svc._port = 8081
    svc._started_at = 1.0
    assert svc.tick() is True
    assert svc.status()["port"] == 8082


def test_tick_does_not_restart_when_everything_is_fine(env, monkeypatch):
    state, make = env
    monkeypatch.setattr(ocr, "metrics", lambda: {"ok": True, "recent_ms": 120.0})
    svc = make(slow_ms=800.0, restart_hours=6.0)
    svc._port = 8081
    svc._started_at = time.monotonic()      # 刚起，没到定时重启的时间
    assert svc.tick() is None


def test_restart_waits_for_an_idle_engine(env):
    """正在抢购就先等，空闲了再重启。"""
    state, make = env
    svc = make(idle_wait_s=2.0)
    svc._port = 8081
    # 「已经跑了 7 小时」要按 monotonic 表示；写死 1.0 在开机不足 6 小时的
    # 机器上算不出 7 小时，这条测试会随开机时长偶发失败。
    svc._started_at = time.monotonic() - 7.0 * 3600
    state["busy"] = True

    import threading
    threading.Timer(0.3, lambda: state.update(busy=False)).start()
    assert svc.tick() is True
    assert svc.status()["port"] == 8082


def test_boot_adopts_a_service_that_is_already_running(env):
    """用户自己起了服务（端口已通）就接管监控，不再起第二个。"""
    state, make = env
    svc = make()
    assert svc._adopt_or_spawn() is True
    assert state["spawned"] == [], "不该重复起一个"
    assert svc.status()["managed"] is False


def test_boot_spawns_on_the_configured_port(env):
    """冷启动就在配置的端口上起，不要绕到备用端口。

    绕过去的话配置里写的 8081 永远空着，下次启动又得走一遍「接管备用端口」
    的兜底 —— 端口漂移会让「服务在哪」变成猜谜。
    """
    state, make = env
    state["up"].discard(8081)
    svc = make()
    assert svc._adopt_or_spawn() is True
    assert state["spawned"] == [8081]
    assert svc.status()["managed"] is True


def test_restart_reclaims_an_adopted_instance_by_port(env):
    """老实例是「接管」来的（没有进程句柄）也要收掉。

    不收拾的话每重启一次就多一个常驻实例：实测跑一天能堆出五六个，
    每个占几百 MB 显存，最后把游戏挤掉。所以按端口找 PID 再杀。
    """
    state, make = env
    svc = make()
    svc._port = 8081
    svc._proc = None                       # 接管来的，手里没有进程
    assert svc.restart("测试") is True
    assert state["killed"] == [8081], f"没回收老实例：{state['killed']}"


def test_boot_adopts_the_spare_port_when_the_configured_one_is_dead(env):
    """上次重启把服务切到了 8082，下次启动别又去起一个。

    配置里写的是 8081，但实例在 8082 —— 直接接管，省一次模型加载
    （几十秒），也避免两个实例抢显存。
    """
    state, make = env
    state["up"] = {8082}                   # 只有备用端口活着
    svc = make()
    assert svc._adopt_or_spawn() is True
    assert ocr.current_url() == "http://127.0.0.1:8082"
    assert state["spawned"] == [], "不该再起一个"


def test_status_does_not_block_while_restarting(env):
    """重启期间 status() 必须立刻返回。

    restart() 整段（起进程 → 等验活 → 预热）握着锁，冷启动十几秒、失败时
    最长 150 秒。status() 要是跟着一起等，/api/preflight 就跟着卡住 ——
    而前端 boot 是「先 preflight 再 poll」串行的，卡住的结果是状态栏一直
    停在「未连接 / OCR —」，看着像整个后端没连上。
    """
    state, make = env
    svc = make()
    svc._port = 8081
    svc._healthy = True
    held = svc._lock.acquire(timeout=1)
    assert held
    try:
        t0 = time.monotonic()
        st = svc.status()
        took = time.monotonic() - t0
    finally:
        svc._lock.release()
    assert took < 0.2, f"status() 被锁拖住了 {took:.1f}s"
    assert st["starting"] is True
    assert st["port"] == 8081, "拿不到锁也要给出上次的快照"


def test_cold_start_does_not_kill_the_instance_it_just_started(env):
    """冷启动时 old_port == new_port，「回收老实例」那一步会把刚起来的自己杀掉。

    症状：服务起得来、日志里能看到 uvicorn running，几秒后进程没了；下一轮
    tick 再起、再被杀 —— 界面上绝大多数时间都是「OCR 不可用」，偶尔闪一下
    「正常」。冷启动那一刻端口本来是空的，没有任何老实例需要回收。
    """
    state, make = env
    state["up"] = set()                    # 冷启动：8081 上没有服务
    svc = make()
    assert svc.restart("启动时拉起") is True
    assert state["killed"] == [], f"把自己杀了：{state['killed']}"
    assert 8081 in state["up"], "刚起的实例被收掉了"


def test_boot_does_not_adopt_a_foreign_service_on_the_configured_port(env):
    """8081 上有人监听、但认不出字 —— 不能当成我们的 OCR 服务接管。

    接管下来之后 tick() 同样只看 TCP，会永远判「健康」：我们自己的服务再也
    不会被拉起，界面一直「OCR 不可用」，重启多少次都一样。这种情况现实里
    见过（端口被代理/别的工具占着）。
    """
    state, make = env
    state["up"] = {8081}                    # 8081 上确实有人在听
    svc = make()
    svc._probe = lambda url: ":8082" in url  # 但只有我们起的那个能认字
    assert svc._adopt_or_spawn() is True
    assert state["spawned"] == [8082], "不该接管别人的端口"
    assert ocr.current_url() == "http://127.0.0.1:8082"


# ---------------- 二级服务只是可选加速：起不来要安静降级 ----------------

def test_optional_service_failure_degrades_with_one_log(env):
    """8081 起不来 → 一条说明 + 降级状态，不逐轮刷日志。

    现场症状：日记里每轮三条（没响应 / 重启 8081→8081 / 前端把
    FileNotFoundError 翻译成「缺少需要的文件」）。根因是把它当必需服务，
    每 60 秒无条件重起一次；内置 RapidOCR 本来就能跑，不该这样折腾。
    """
    state, make = env
    state["up"] = set()
    state["spawn_error"] = FileNotFoundError(
        r"没有 C:\x\.venv-vl\Scripts\paddlex.exe（OCR 服务环境没装）")
    logs = []
    svc = make(log=logs.append)

    assert svc._adopt_or_spawn() is False
    st = svc.status()
    assert st["degraded"] is True, "可选服务起不来要进降级"
    assert st["fails"] >= 1
    assert len(logs) == 1, f"降级只写一条日志，实际 {len(logs)} 条：{logs}"
    assert "降级为内置识别" in logs[0]
    assert "FileNotFoundError" not in logs[0], \
        "异常类名会被前端翻译成「缺少需要的文件」，是误报"
    assert state["attempts"] == [8081], "启动时只尝试一次"


def test_degraded_state_does_not_retry_or_log_every_tick(env):
    """降级之后挂机：不重启、不写日志，只累加失败次数。"""
    state, make = env
    state["up"] = set()
    state["busy"] = False
    logs = []
    svc = make(log=logs.append, spawn_error=None)
    svc._degraded = True
    svc._next_retry_at = time.monotonic() + 600.0
    n_logs = len(logs)
    n_attempts = len(state["attempts"])

    for _ in range(30):
        assert svc.tick() is None

    assert len(logs) == n_logs, f"降级期间又写日志了：{logs[n_logs:]}"
    assert len(state["attempts"]) == n_attempts, "降级期间不该反复拉进程"
    assert svc.status()["degraded"] is True


def test_degraded_retry_is_low_frequency_and_silent_on_failure(env):
    """到重试点才试一次；再失败只累加次数，不写重复日志。"""
    state, make = env
    state["up"] = set()
    state["spawn_error"] = FileNotFoundError("没有 paddlex.exe")
    logs = []
    svc = make(log=logs.append)
    svc._degraded = True
    svc._next_retry_at = 0.0
    n_logs = len(logs)

    assert svc.tick() is None
    assert len(state["attempts"]) == 1, "到点应静默重试一次"
    assert len(logs) == n_logs, "重试失败不写日志"
    assert svc.status()["fails"] >= 1
    # 重试后下一次窗口又是 600 秒，期间不再打进程
    for _ in range(10):
        svc.tick()
    assert len(state["attempts"]) == 1


def test_degraded_recovers_when_the_port_answers(env):
    """二级服务自己回来了：一条恢复日志，状态回到正常。"""
    state, make = env
    state["up"] = {8081}
    state["probe_ok"] = True
    logs = []
    svc = make(log=logs.append)
    svc._degraded = True
    svc._next_retry_at = 0.0

    assert svc.tick() is True
    st = svc.status()
    assert st["degraded"] is False and st["healthy"] is True
    assert "已恢复" in logs[-1]
    assert state["attempts"] == [], "端口自己能认字就不该再拉进程"


def test_flapping_service_is_degraded_instead_of_restarted_forever(env):
    """起来几秒就挂的实例：连续两次就认定拉不住，别再无限重启。"""
    state, make = env
    state["up"] = set()
    svc = make()
    svc._port = 8081
    svc._started_at = time.monotonic() - 10.0
    assert svc.tick() is True              # 第一次：起来过一次
    state["up"].discard(8081)
    svc._started_at = time.monotonic() - 5.0   # 刚起来 5 秒又挂
    svc.tick()
    assert svc.status()["degraded"] is True
    assert state["attempts"] == [8081], "第二次不该再拉进程"
