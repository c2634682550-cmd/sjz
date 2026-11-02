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
        return ocr_service.Supervisor(
            settings={"enabled": True, "healthy_timeout_s": 1.0,
                      "idle_wait_s": 0, "check_interval_s": 999, **cfg},
            is_busy=lambda: state["busy"],
            spawn=spawn, probe=probe, kill=kill, kill_port=kill_port)

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
    svc._started_at = 1.0
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
