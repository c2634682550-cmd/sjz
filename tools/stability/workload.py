"""用**真实引擎代码**跑一圈完整的倒计时抢购周期（假输入）。

保真边界（写清楚，免得报告被当成真机数据）：
  · 真实：`_execute_countdown` / `_watch_countdown` 主循环、`countdown.Clock`
    的观测与归零估计、`_record`（真写 records.jsonl）、`_save_shot`
    （真写 JPEG + 目录修剪）、`_say` 日志环形缓冲、状态机。
  · 替身：`Screen.grab`（返回全零图）、`Mouse`（只记数）、`ocr.read_words`
    （按虚拟时钟算剩余秒数）、`_countdown_prepare`（直接返回一件目标）、
    `_fire_and_confirm`（跳过真实连点，改为调用真实 `_record`）。
  · 不替代：像素匹配、真实点击注入、真实 OCR 推理 —— 这些是「真机验证」
    阶段的事，代码层长跑只关心资源与逻辑稳定性。
"""
import os
import time
import types

import numpy as np

from core import engine as engine_mod
from core import records

from . import fakes


def apply_workspace(ws):
    """把数据落点全部搬到临时目录，绝不碰项目里的 data/。"""
    records.PATH = os.path.join(ws.data, "records.jsonl")
    engine_mod.DATA_DIR = ws.data
    # 前台校验默认走真实 Win32（`Mouse.foreground()` 会读当前前台窗口），
    # 假屏环境下永远不会等于 `_target_hwnd`，会直接判「抢不到前台」。
    # 换成假鼠标后所有输入相关调用都是空实现 —— 不动真实光标。
    engine_mod.Mouse = fakes.FakeMouse
    os.makedirs(ws.shots, exist_ok=True)


def _prepare_stub(target_sec=60, serial="S-0001"):
    def _prepare(self, cfg, screen, mouse, tried=(), refresh=False):
        return {"serial": serial, "text": f"0分{int(target_sec):02d}秒后解锁购买",
                "sec": int(target_sec), "x": 0, "y": 0,
                "kind": "unlock", "name": "AK-12突击步枪·水墨迷彩"}
    return _prepare


def _fire_stub(price=1234.0):
    """跳过真实连点，但**走真实记账 + 截图**路径。"""

    def _fire(self, cfg, screen, mouse, confirm_tpl, buy_click, t_start,
              *a, **k):
        self._set_state(getattr(engine_mod, "CONFIRM", "confirm"))
        # 模拟「点完到弹窗出现」的几百毫秒
        self.time_hint(0.35)
        self._record(cfg, screen, item="AK-12突击步枪·水墨迷彩",
                     price=price, elapsed=60.0, result="hit")
        return True
    return _fire


def run_cycle(clock, cfg=None, screen=None, ocr=None, price=1234.0,
              prepare_sec=60, fire=True, fire_error=None):
    """跑一圈。返回 (结果 dict, engine)。"""
    cfg = cfg or fakes.FakeCfg()
    screen = screen or fakes.FakeScreen()
    eng = engine_mod.Engine()
    eng._target_hwnd = 1
    eng._shot_px = None
    eng._close_reward = lambda *a, **k: None
    # 真实实现里 `_center` 走 screen.norm_to_px；替身屏幕没有坐标系，直接给点。
    eng._center = lambda scr, region: (200, 300)
    eng._countdown_prepare = types.MethodType(_prepare_stub(prepare_sec), eng)
    eng.time_hint = clock.advance
    if fire_error is not None:
        def _boom(*a, **k):
            raise fire_error
        eng._fire_and_confirm = _boom
    elif fire:
        eng._fire_and_confirm = types.MethodType(_fire_stub(price), eng)
    else:
        eng._fire_and_confirm = lambda *a, **k: False

    target_pc = clock.perf_counter() + prepare_sec
    ocr = ocr or fakes.FakeOcr(clock, target_pc)
    # `_watch_countdown` 调的是 `engine` 模块里的 `ocr`，不是参数 —— 这里替换。
    engine_mod.ocr = ocr

    t_real = time.perf_counter()
    t_virt = clock.perf_counter()
    result = {"outcome": "hit", "error": ""}
    try:
        eng._execute_countdown(cfg, screen, fakes.FakeMouse, None)
    except Exception as e:                       # noqa: BLE001
        result = {"outcome": "aborted",
                  "error": f"{type(e).__name__}: {e}"}
    result["real_ms"] = round((time.perf_counter() - t_real) * 1000, 1)
    result["virt_s"] = round(clock.perf_counter() - t_virt, 2)
    result["grabs"] = screen.calls
    result["ocr_calls"] = ocr.calls
    result["state"] = eng.status().get("state")
    result["beat_age_s"] = eng.status().get("beat_age_s")
    # 解绑替身：`eng.x = MethodType(fn, eng)` 会形成 Engine -> MethodType ->
    # Engine 的环，Engine 就只能等 gc 才回收，它持有的锁句柄也跟着拖住。
    # 长跑每轮新建一个 Engine，测出来的句柄曲线就会被这个环污染。
    eng._countdown_prepare = None
    eng._fire_and_confirm = None
    eng._close_reward = None
    eng._center = None
    return result, eng


def idle_tick(clock, seconds=5.0, records_limit=200, do_stats=True):
    """模拟界面轮询：读记录 + 统计。长跑里「越跑越慢」最可能出在这里。"""
    t0 = time.perf_counter()
    n = records.load(records_limit) if os.path.isfile(records.PATH) else []
    st = records.stats() if do_stats else None
    clock.advance(seconds)
    return {"real_ms": round((time.perf_counter() - t0) * 1000, 2),
            "rows": len(n), "hits": (st or {}).get("hits", 0),
            "records_bytes": os.path.getsize(records.PATH)
            if os.path.isfile(records.PATH) else 0}
