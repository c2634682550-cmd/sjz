"""GPU_TDR 识别与自动恢复预算的单元测试（全部离线，不拉真实进程）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import gpu_guard


def _runner(system_items=None, app_items=None):
    def run(script):
        if "nvlddmkm" in script:
            return {"error": "", "items": system_items or []}
        return {"error": "", "items": app_items or []}
    return run


def test_classify_detects_gpu_tdr():
    run = _runner(
        system_items=[{"TimeCreated": "/Date(1)/", "Id": 153,
                       "ProviderName": "nvlddmkm",
                       "Message": "显示器驱动程序 nvlddmkm 已停止响应"}],
        app_items=[{"TimeCreated": "/Date(2)/", "Id": 1001,
                    "ProviderName": "Windows Error Reporting",
                    "Message": "LiveKernelEvent 代码: 141"}])
    r = gpu_guard.classify_gone(runner=run)
    assert r["cause"] == "GPU_TDR"
    assert r["tdr_153"] == 1 and r["livekernel_141"] == 1


def test_classify_unknown_without_events():
    r = gpu_guard.classify_gone(runner=_runner())
    assert r["cause"] == "UNKNOWN"
    assert r["tdr_153"] == 0 and r["livekernel_141"] == 0


def test_ledger_allows_two_then_gives_up(tmp_path):
    led = gpu_guard.RecoveryLedger(path=str(tmp_path / "l.json"))
    assert led.allow() is True
    led.spend("GPU_TDR")
    assert led.allow() is True
    led.spend("GPU_TDR")
    assert led.allow() is False
    assert "显卡不稳定" in led.give_up_message()
    assert led.used == 2


def test_handle_gone_recovers_once(tmp_path):
    led = gpu_guard.RecoveryLedger(path=str(tmp_path / "l.json"))
    said = []
    r = gpu_guard.handle_window_gone(
        progress={"stage": "listing", "task_id": "t1"},
        say=lambda m, l="info": said.append((l, m)),
        runner=_runner(system_items=[{"Id": 153, "ProviderName": "nvlddmkm",
                                      "Message": "TDR"}]),
        ledger=led,
        launcher=lambda exe=None: {"ok": True, "pid": 1234, "exe": "x",
                                   "why": ""},
        finder=lambda: 777,
        wait_s=0.1)
    assert r["action"] == "resume" and r["hwnd"] == 777
    assert led.used == 1
    assert any("GPU_TDR" in m for _l, m in said)


def test_handle_gone_gives_up_after_two(tmp_path):
    led = gpu_guard.RecoveryLedger(path=str(tmp_path / "l.json"))
    led.spend("GPU_TDR")
    led.spend("GPU_TDR")
    called = {"launch": 0}

    def launcher(exe=None):
        called["launch"] += 1
        return {"ok": True, "pid": 1, "exe": "x", "why": ""}

    r = gpu_guard.handle_window_gone(
        say=lambda m, l="info": None, runner=_runner(), ledger=led,
        launcher=launcher, finder=lambda: 1, wait_s=0.1)
    assert r["action"] == "gave_up"
    assert "显卡不稳定" in r["message"]
    assert called["launch"] == 0


def test_launch_failure_does_not_spend_budget(tmp_path):
    led = gpu_guard.RecoveryLedger(path=str(tmp_path / "l.json"))
    r = gpu_guard.handle_window_gone(
        say=lambda m, l="info": None, runner=_runner(), ledger=led,
        launcher=lambda exe=None: {"ok": False, "why": "找不到游戏 exe"},
        finder=lambda: 1, wait_s=0.1)
    assert r["action"] == "no_exe"
    assert led.used == 0


def test_progress_roundtrip(tmp_path):
    p = str(tmp_path / "p.json")
    gpu_guard.record_progress({"stage": "detail", "price": 5201}, path=p)
    assert gpu_guard.load_progress(p)["price"] == 5201
    with open(p, encoding="utf-8") as f:
        assert json.load(f)["stage"] == "detail"
