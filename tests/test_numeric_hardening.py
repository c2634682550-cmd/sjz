"""数值与时间基准加固的回归测试（审查 R03）。

每个用例都对应 `_bug_r03_numeric.md` 里一条「修之前会静默出错」的缺陷：
断言的是**错误不再发生**（丢弃/拒识），而不是"某条路径刚好能跑"。

跑法：uv run pytest tests/test_numeric_hardening.py
"""
import copy
import math
import os
import socket
import statistics
import struct
import sys
import threading
import time
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config  # noqa: E402
from core import engine as eng_mod  # noqa: E402
from core import jobs  # noqa: E402
from core import market  # noqa: E402
from core import nav_col  # noqa: E402
from core import ocr_chain  # noqa: E402
from core import screen  # noqa: E402
from core import switches  # noqa: E402
from core import timesync  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_module_state():
    """timesync/switches 有模块级状态，用例之间必须隔离。"""
    ts = dict(timesync._state)
    issues = dict(switches._issues)
    yield
    timesync._state.clear()
    timesync._state.update(ts)
    switches._issues.clear()
    switches._issues.update(issues)


# ----------------------------------------------------------------- timesync
def _clock_proxy(wall):
    """替换模块里的 `time`：壁钟由用例控制，单调时钟/睡眠用真的。"""
    return types.SimpleNamespace(time=wall, monotonic=time.monotonic,
                                 sleep=time.sleep)


def _fake_socket(factory):
    class _Sock:
        def __init__(self, *_a, **_kw):
            self.req = b"\x00" * 48

        def settimeout(self, _t):
            pass

        def sendto(self, data, _addr):
            self.req = data

        def recvfrom(self, _n):
            payload = factory(self.req)
            if isinstance(payload, Exception):
                raise payload
            return payload, ("127.0.0.1", 123)

        def close(self):
            pass

    return _Sock


def _patch_socket(monkeypatch, factory):
    stub = types.SimpleNamespace(
        socket=_fake_socket(factory), AF_INET=socket.AF_INET,
        SOCK_DGRAM=socket.SOCK_DGRAM, timeout=TimeoutError)
    monkeypatch.setattr(timesync, "socket", stub)


def _reply(req, recv_off=0.0005, send_gap=0.0005, stratum=2, mode=4,
           origin=None, size=48, base=None):
    """按请求里的发送时间戳构造一条应答。

    base 给定时，服务器时钟就用这个固定值（测试可控）；否则按请求里的
    origin 解码 —— 注意 origin 的低位掺了随机数，解码值最多差 1 秒。
    """
    sec, frac = struct.unpack("!II", req[40:48])
    t1 = base if base is not None else timesync._from_ntp(sec, frac)
    t2 = t1 + recv_off
    t3 = t2 + send_gap
    buf = bytearray(48)
    buf[0] = (0 << 6) | (4 << 3) | mode
    buf[1] = stratum
    struct.pack_into("!II", buf, 24, *(origin if origin is not None else (sec, frac)))
    struct.pack_into("!II", buf, 32, *timesync._to_ntp(t2))
    struct.pack_into("!II", buf, 40, *timesync._to_ntp(t3))
    return bytes(buf[:size])


def test_all_zero_reply_is_rejected(monkeypatch):
    _patch_socket(monkeypatch, lambda _req: b"\x00" * 48)
    with pytest.raises(ValueError) as ei:
        timesync._query("127.0.0.1", timeout=0.2)
    # 全零包里 stratum=0、mode=0、origin=0 —— 任何一条都该先炸
    assert "stratum" in str(ei.value) or "mode" in str(ei.value)


def test_short_reply_is_rejected(monkeypatch):
    _patch_socket(monkeypatch, lambda req: _reply(req, size=20))
    with pytest.raises(ValueError) as ei:
        timesync._query("127.0.0.1", timeout=0.2)
    assert "短包" in str(ei.value)


def test_out_of_range_offset_is_rejected(monkeypatch):
    # 服务器时间比本机快 11.5 天：不是"校时"，是错配主机/脏包
    _patch_socket(monkeypatch, lambda req: _reply(req, recv_off=1e6))
    with pytest.raises(ValueError) as ei:
        timesync._query("127.0.0.1", timeout=0.2)
    assert "偏移" in str(ei.value) and "超出" in str(ei.value)


def test_forged_origin_is_rejected(monkeypatch):
    _patch_socket(monkeypatch, lambda req: _reply(req, origin=(0, 0)))
    with pytest.raises(ValueError) as ei:
        timesync._query("127.0.0.1", timeout=0.2)
    assert "origin" in str(ei.value)


def test_plausible_reply_is_accepted(monkeypatch):
    frozen = 1_700_000_000.0
    monkeypatch.setattr(timesync, "time", _clock_proxy(lambda: frozen))
    _patch_socket(monkeypatch, lambda req: _reply(req, base=frozen))
    got = timesync._query("127.0.0.1", timeout=0.2)
    assert got["offset"] == pytest.approx(0.00075, abs=0.001)
    assert 0 <= got["delay"] <= timesync.MAX_DELAY_S


def _fake_samples(samples):
    """monkeypatch 用的 _query 替身：按次序吐出预设样本。"""
    it = iter(samples)

    def _q(host, timeout=2.0):
        s = next(it)
        return {"offset": s[0], "delay": s[1], "stratum": 2, "server": host}
    return _q


def test_zero_delay_outlier_does_not_win(monkeypatch):
    """R03-N01：delay=0 的离群包以前会"因为延迟最小"被采纳。"""
    monkeypatch.setattr(timesync, "_query", _fake_samples([
        (0.40, 0.050), (0.45, 0.040), (0.42, 0.030),
        (1234.0, 0.000),          # 极短延迟 + 离谱偏移
    ]))
    res = timesync.sync(servers=["a", "b", "c", "d"], samples=1)
    assert res["ok"] is True
    # 独立复核：结果必须等于三条正常样本的中位数
    expect = statistics.median([0.40, 0.45, 0.42])
    assert res["offset"] == pytest.approx(expect, abs=1e-9)
    assert res["server"] == "c"
    assert res["offset"] < 1.0                 # 1234 没被选上
    assert timesync._state["at_mono"] is not None


def test_too_few_valid_samples_keep_old_state(monkeypatch):
    """R03-N01：异常路径不得污染已有时钟状态。"""
    timesync._state.update(offset=0.7, delay=0.01, server="old",
                           at=time.time(), at_mono=time.monotonic())
    monkeypatch.setattr(timesync, "_query",
                        lambda host, timeout=2.0: (_ for _ in ()).throw(
                            ValueError("bad packet")))
    res = timesync.sync(servers=["a", "b", "c"], samples=1)
    assert res["ok"] is False
    assert timesync._state["offset"] == pytest.approx(0.7)
    assert timesync._state["server"] == "old"
    assert timesync.offset() == pytest.approx(0.7)


def test_forward_clock_jump_keeps_offset(monkeypatch):
    """R03-N05：壁钟前跳应该由单调时钟兜住，不能立刻失效。"""
    now = time.time()
    timesync._state.update(offset=0.7, delay=0.01, server="x",
                           at=now, at_mono=time.monotonic())
    monkeypatch.setattr(timesync, "time",
                        _clock_proxy(lambda: now + 3 * 86400))
    assert timesync.offset() == pytest.approx(0.7)
    assert timesync.info()["stale"] is False


def test_backward_clock_jump_invalidates_offset(monkeypatch):
    """R03-N05：壁钟回拨 60 秒 = 有人在改时间，旧偏移不再可信。"""
    now = time.time()
    timesync._state.update(offset=0.7, delay=0.01, server="x",
                           at=now, at_mono=time.monotonic())
    monkeypatch.setattr(timesync, "time", _clock_proxy(lambda: now - 60))
    assert timesync.offset() == 0.0
    info = timesync.info()
    assert info["stale"] is True and "回拨" in info["stale_reason"]


def test_ttl_still_expires(monkeypatch):
    now = time.time()
    timesync._state.update(offset=0.7, delay=0.01, server="x",
                           at=now, at_mono=time.monotonic() - timesync.OFFSET_TTL_S - 10)
    monkeypatch.setattr(timesync, "time", _clock_proxy(lambda: now))
    assert timesync.offset() == 0.0
    assert "过期" in timesync.info()["stale_reason"]


def test_legacy_wall_anchor_still_works():
    """旧状态（只有 at 没有 at_mono）仍按壁钟判 TTL —— 既有测试靠这条。"""
    timesync._state.update(offset=0.7, at=time.time() - timesync.OFFSET_TTL_S - 10,
                           at_mono=None)
    assert timesync.offset() == 0.0


# ----------------------------------------------------------------- switches
def _cfg():
    return copy.deepcopy(config.DEFAULTS)


def test_numeric_bounds_cover_every_numeric_switch():
    for s in switches.SWITCHES:
        if s["kind"] in ("int", "float"):
            assert s["key"] in switches.BOUNDS, s["key"]


@pytest.mark.parametrize("key,value,frag", [
    ("fire.threshold", "nan", "有限"),      # 比较恒为假 -> 永不识别
    ("scan.interval", -0.05, "小于下限"),   # sleep 负数 -> 线程崩
    ("fire.timeout", "inf", "有限"),        # while 条件恒真 -> 无限连点
    ("follow.retry", "inf", "有限"),        # int(inf) -> OverflowError
    ("fire.hz", 0, "小于下限"),
    ("price.quota", 0, "小于下限"),
    ("fire.threshold", 1.5, "大于上限"),
    ("filter.wear_max", 11, "大于上限"),
])
def test_switch_values_are_rejected(key, value, frag):
    cfg = _cfg()
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.set(cfg, key, value)
    msg = str(ei.value)
    assert key in msg and frag in msg


def test_follow_retry_inf_raises_switch_error_not_overflow():
    cfg = _cfg()
    with pytest.raises(switches.SwitchValueError):
        switches.set(cfg, "follow.retry", float("inf"))


def test_dirty_config_value_falls_back_to_default():
    """手改 config.json 写成 NaN：引擎读到的必须是安全默认值。"""
    cfg = _cfg()
    tree = switches.spec("fire.threshold")["path"]
    switches._plant(cfg, tree, float("nan"))
    assert switches.get(cfg, "fire.threshold") == switches.spec("fire.threshold")["default"]
    assert "fire.threshold" in switches.issues()


def test_apply_all_is_all_or_nothing():
    cfg = _cfg()
    before = switches.get(cfg, "fire.hz")
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.apply_all(cfg, {"fire.hz": 50, "scan.interval": -0.05})
    assert "scan.interval" in str(ei.value)
    assert switches.get(cfg, "fire.hz") == before      # 一个字段都没落盘


def test_valid_values_still_pass():
    cfg = _cfg()
    assert switches.set(cfg, "fire.threshold", "0.9") == 0.9
    assert switches.set(cfg, "scan.interval", 0.2) == 0.2
    assert switches.set(cfg, "follow.retry", "3") == 3


# ---------------------------------------------------------------- ocr_chain
@pytest.mark.parametrize("text,value", [
    ("¥1,234.5", 1234.5),
    ("¥12 345", 12345),
    ("¥15,000", 15000),
    ("¥ 250,000", 250000),
    ("￥9,999", 9999),
])
def test_currency_amount_parses_as_a_whole(text, value):
    assert ocr_chain.price_from_text(text)[0] == pytest.approx(value)


@pytest.mark.parametrize("text", [
    "¥1,234.567",     # 三位小数 = 没读全/句点当逗号
    "¥12 34",         # 空格组不是三位
    "¥-5",            # 负价
    "-5",             # 负号被丢会变 +5
    "1,234.567",      # 无货币符号，尾段 567 不是独立价格
])
def test_bad_amounts_are_refused(text):
    assert ocr_chain.price_from_text(text)[0] is None


def test_existing_ocr_cases_unchanged():
    """既有行为的锚点：这些断言在 test_ocr_price.py 里也有，不许回退。

    `"2,049 0+ 0 40+" → 40` 这条由 `tests/test_ocr_price.py:94` 继续守护；
    本文件不再重复（该解析当前由并行分支的 `core/money.py` 接管）。
    """
    assert ocr_chain.price_from_text("168,000")[0] == 168000
    assert ocr_chain.price_from_text("0")[0] == 0
    assert ocr_chain.price_from_text("9,9")[0] is None
    assert ocr_chain.price_from_text("1,000,")[0] is None


# ------------------------------------------------------------------- market
@pytest.mark.parametrize("text,price", [
    ("¥15,000 崭新 0.01", 15000),
    ("¥5 崭新 0.01", None),          # 低于下限
    ("¥0 崭新 0.01", None),
    ("¥15.5 崭新 0.01", None),       # 价格不许小数
    ("-15000 崭新", None),           # 负号语义不能丢
    ("崭新 0.583", None),
])
def test_parse_row_currency_semantics(text, price):
    row = market.parse_row(text)
    if price is None:
        assert row["price"] is None or row["price"] <= 0
    else:
        assert row["price"] == price


def test_pick_never_takes_zero_price_row():
    rows = [
        {"text": "QBZ95 0", "price": 0.0, "craft": None, "wear": None,
         "wear_value": None, "numbers": [0.0]},
        {"text": "QBZ95 15,000", "price": 15000.0, "craft": None, "wear": None,
         "wear_value": None, "numbers": [15000.0]},
    ]
    row, why, diag = market.pick(rows, {})
    assert row is not None and row["price"] == 15000.0
    assert any("不可信" in d["why"] for d in diag)


def test_check_ignores_nan_limits():
    row = {"text": "任何", "price": 15000.0, "craft": None, "wear": None,
           "wear_value": None, "numbers": []}
    ok, _ = market.check(row, {"min_price": float("nan"),
                               "max_price": float("nan")})
    assert ok is True                      # NaN 不能让上下限"恒为假"
    ok, why = market.check(row, {"min_price": -100})
    assert ok is True                      # 负下限按未设置处理
    ok, why = market.check(row, {"min_price": 20000})
    assert ok is False and "下限" in why


# ------------------------------------------------ jobs / screen / nav_col
def test_jobs_elapsed_survives_backward_wall_clock(monkeypatch):
    wall = iter([1000.0, 400.0])           # 提交时一刻，结束时"回拨"600 秒

    def fake_time():
        return next(wall, 400.0)

    monkeypatch.setattr(jobs, "time", _clock_proxy(fake_time))
    jid = jobs.submit(lambda log: None, name="clock")
    for _ in range(200):
        st = jobs.status(jid)
        if st and st["state"] != "running":
            break
        time.sleep(0.01)
    st = jobs.status(jid)
    assert st["elapsed"] >= 0 and st["elapsed"] < 5


def test_wait_frame_change_uses_monotonic(monkeypatch):
    calls = {"wall": 0}

    def wall():
        calls["wall"] += 1
        return 1000.0 - calls["wall"] * 100    # 每次都往回跳

    proxy = _clock_proxy(wall)
    monkeypatch.setattr(screen, "time", proxy)
    frames = iter([None, object()])

    class _Frame:
        shape = (2, 2, 3)
        size = 12

    got = screen.wait_frame_change(None, lambda: _Frame(), timeout=0.5, interval=0.01)
    assert got is not None and got >= 0
    assert calls["wall"] == 0              # 壁钟一次都不该被用来计时


class _FakeNav:
    def __init__(self, titles=None, items=None):
        self.titles = list(titles or [])
        self.items = items
        self.n = 0

    def skin_title(self):
        self.n += 1
        return self.titles[min(self.n - 1, len(self.titles) - 1)] if self.titles else ""

    def skin_items(self):
        return self.items


def test_wait_title_change_uses_monotonic(monkeypatch):
    calls = {"wall": 0}

    def wall():
        calls["wall"] += 1
        return 500.0 - calls["wall"] * 10

    monkeypatch.setattr(nav_col, "time", _clock_proxy(wall))
    fake = _FakeNav(titles=["旧", "目标"])
    got = nav_col.NavColPanel._wait_title_change(fake, "旧", timeout=0.5, gap=0.01)
    assert got == "目标"
    assert calls["wall"] == 0


def test_wait_skin_list_uses_monotonic(monkeypatch):
    calls = {"wall": 0}

    def wall():
        calls["wall"] += 1
        return 500.0 - calls["wall"] * 10

    monkeypatch.setattr(nav_col, "time", _clock_proxy(wall))
    fake = _FakeNav(items=[("目标", 100)])
    assert nav_col.NavColPanel._wait_skin_list(fake, timeout=0.5) is True
    assert calls["wall"] == 0


def test_norm_to_px_rejects_non_finite():
    fake = types.SimpleNamespace(size=(1000, 600))
    with pytest.raises(ValueError) as ei:
        screen.Screen.norm_to_px(fake, {"x": float("nan"), "y": 0,
                                        "w": 1, "h": 1})
    assert "有限" in str(ei.value)
    out = screen.Screen.norm_to_px(fake, {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4})
    assert out == (100, 120, 300, 240)


# ------------------------------------------- F6 引擎直读配置的带校验入口
def test_switches_read_falls_back_and_records_issue():
    """手改 config.json 的越界值 → 读回安全默认值，并留下排查记录。"""
    cfg = _cfg()
    switches._plant(cfg, "monitor", -1)
    assert switches.read(cfg, "monitor", 1, "int", 0, 16, key="monitor") == 1
    assert "monitor" in switches.issues()

    switches._plant(cfg, "arm_grace_s", float("inf"))
    assert switches.read(cfg, "arm_grace_s", 1.5, "float", 0.0, 30.0,
                         key="arm_grace_s") == 1.5

    switches._plant(cfg, "confirm_timeout_s", "abc")
    assert switches.read(cfg, "confirm_timeout_s", 2.0, "float", 0.05, 600.0,
                         key="confirm_timeout_s") == 2.0


def test_switches_read_keeps_valid_and_missing_values():
    cfg = _cfg()
    assert switches.read(cfg, "不存在的键", 7, "int", 0, 10) == 7
    switches._plant(cfg, "monitor", 2)
    assert switches.read(cfg, "monitor", 1, "int", 0, 16, key="monitor") == 2


class _EngCfg(dict):
    def __init__(self, **kw):
        super().__init__({
            "failsafe": False,
            "regions": {"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                        "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1},
                        "confirm": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.1}},
            "active_task": {"min_price": 0, "max_price": 0,
                            "sort": "order", "quota": 1},
        })
        self.update(kw)

    def region(self, name):
        return self["regions"].get(name)


class _EngScreen:
    size = (1920, 1080)

    def norm_to_px(self, box):
        return (60, 210, 1340, 730)

    def grab(self, px=None):
        return np.zeros((8, 8, 3), np.uint8)

    def close(self):
        pass


class _EngMouse:
    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True


class _EngNav:
    made = []

    def __init__(self, *a, **k):
        _EngNav.made.append(self)
        self.calls = []

    def where(self):
        return ("onsale", 0.9)

    def to_onsale(self):
        self.calls.append(("to_onsale",))
        return True

    def pub_filter(self, want):
        self.calls.append(("pub_filter", want))

    def ons_refresh(self):
        self.calls.append(("refresh",))

    def ons_filter(self):
        self.calls.append(("filter",))

    def ons_price(self, lo=None, hi=None):
        self.calls.append(("price", lo, hi))
        return (lo, hi)

    def ons_checks(self, names, want=True):
        self.calls.append(("checks", tuple(names)))
        return list(names)

    def ons_filter_confirm(self):
        self.calls.append(("confirm",))


def _make_engine():
    e = eng_mod.Engine()
    e._stop = threading.Event()
    e._target_hwnd = 1
    e._said = []
    e._say = lambda msg, level="info": e._said.append((level, msg))
    e._failsafe = lambda on: None
    return e


def _card(x0, y0, price=2050.0):
    return {"text": "KC17 突击步枪造物纪元 极品 成色 A", "price": float(price),
            "craft": None, "wear": None, "wear_value": None,
            "x0": float(x0), "y0": float(y0), "cy": float(y0) + 20.0}


def test_engine_list_mode_survives_hand_edited_out_of_range_config(monkeypatch):
    """F6 的实际证据：把 config 手改成 NaN/Inf/负数，列表模式照样跑完。"""
    e = _make_engine()
    _EngNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", _EngNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    monkeypatch.setattr(eng_mod.market, "scan",
                        lambda frame, **kw: [_card(40.0, 10.0)])
    monkeypatch.setattr(eng_mod, "match", lambda a, b: (1.0, None))
    monkeypatch.setattr(e, "_load", staticmethod(lambda cfg, name: None))
    clicks = []
    monkeypatch.setattr(e, "_click", lambda m, x, y: clicks.append((x, y)))
    monkeypatch.setattr(e, "_fire_and_confirm", lambda *a, **k: True)

    cfg = _EngCfg(
        threshold=float("nan"),          # 比较恒为假 → 永不识别
        click_hz=float("inf"),           # int(inf) → OverflowError
        fire_timeout_s=-5,               # 循环条件直接不成立
        confirm_clicks="not-a-number",   # int() → ValueError
        confirm_interval_ms=10 ** 9,     # 越界
        list_scan_interval=-3,           # sleep 负数 → 线程崩
        list_scan_timeout=float("nan"),
        list_detail_timeout=float("inf"),
        list_max_tries=-7,
    )
    e._execute_list(cfg, _EngScreen(), _EngMouse(), None)

    assert e._state == eng_mod.DONE
    assert len(clicks) == 1
    issues = switches.issues()
    for key in ("fire.threshold", "scan.interval", "scan.timeout",
                "scan.detail_timeout", "scan.max_tries"):
        assert key in issues, f"{key} 的脏值没有被记录"


def test_fire_and_confirm_survives_hand_edited_out_of_range_config(monkeypatch):
    """F6：连点/确认/延迟全部被手改坏，点购买到确认仍能走完。"""
    e = _make_engine()
    scores = iter([1.0, 0.0])          # 第 1 次匹配=弹窗出现；第 2 次=弹窗已消失

    def fake_match(a, b):
        return next(scores, 0.0), None

    monkeypatch.setattr(eng_mod, "match", fake_match)
    clicks = []
    monkeypatch.setattr(e, "_click", lambda m, x, y: clicks.append((x, y)))
    monkeypatch.setattr(e, "_wait_dialog_unlock", lambda *a, **k: "ok")
    monkeypatch.setattr(e, "_balance", lambda s: None)
    monkeypatch.setattr(e, "_bought_dialog", lambda s: True)
    monkeypatch.setattr(e, "_record", lambda *a, **k: None)
    monkeypatch.setattr(e, "_guard_insufficient", lambda *a, **k: None)
    monkeypatch.setattr(e, "_close_reward", lambda *a, **k: None)
    monkeypatch.setattr(e, "_insufficient_showing", lambda s: False)

    cfg = _EngCfg(
        threshold=float("nan"), click_hz=float("inf"), fire_timeout_s=-5,
        confirm_clicks=float("nan"), confirm_interval_ms=10 ** 9,
        fire_delay_ms=-100, fire_jitter_ms=float("inf"),
        confirm_timeout_s=float("nan"),
    )
    ok = e._fire_and_confirm(cfg, _EngScreen(), _EngMouse(), None,
                             (300, 500), time.perf_counter(),
                             item="x", price=2050)
    assert ok is True
    assert len(clicks) == 2, f"应当 1 次购买 + 1 次确认，实际 {clicks}"


# ------------------------------------------ R2 丢包重试 / 退避 / 上限
def _sleep_proxy(slept):
    """time 替身：壁钟/单调时钟用真的，sleep 只记录不真睡。"""
    return types.SimpleNamespace(time=time.time, monotonic=time.monotonic,
                                 sleep=lambda s: slept.append(s))


def test_single_packet_loss_is_retried_not_abandoned(monkeypatch):
    """R2：一次丢包只算一次失败 —— 不许整台放弃，也不许吃掉样本名额。"""
    slept = []
    monkeypatch.setattr(timesync, "time", _sleep_proxy(slept))
    lost = {"a": False}

    def q(host, timeout=2.0):
        if host == "a" and not lost["a"]:
            lost["a"] = True
            raise TimeoutError("timed out")
        return {"offset": 0.5, "delay": 0.01, "stratum": 2, "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["a", "b", "c"], samples=2)

    assert res["ok"] is True
    assert res["attempts"] == 7              # 3 台×2 + 1 次重试
    assert [s["server"] for s in res["samples"]].count("a") == 2
    assert slept == [pytest.approx(timesync.BACKOFF_BASE_S)]


def test_consecutive_failures_abandon_host_and_rotate(monkeypatch):
    """R2：连续失败到上限才放弃该主机，其余主机照常出样本。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))

    def q(host, timeout=2.0):
        if host == "dead":
            raise TimeoutError("timed out")
        return {"offset": 0.4, "delay": 0.02, "stratum": 2, "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["dead", "b", "c", "d"], samples=2)

    assert res["ok"] is True
    assert res["attempts"] == timesync.MAX_CONSEC_FAIL + 6
    assert any("放弃该主机" in e for e in res["tried"])


def test_total_attempts_are_capped(monkeypatch):
    """R2：全都不通时请求数有硬上限，不会无限重试拖死启动。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))

    def q(host, timeout=2.0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["a", "b", "c", "d", "e", "f"], samples=4)

    assert res["ok"] is False
    assert res["attempts"] == timesync.MAX_TOTAL_ATTEMPTS
    assert timesync._state["last_error"]


def test_backoff_grows_and_is_capped(monkeypatch):
    """R2：退避按连续失败次数递增，且不超过上限。"""
    slept = []
    monkeypatch.setattr(timesync, "time", _sleep_proxy(slept))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: None)
    n = {"a": 0}

    def q(host, timeout=2.0):
        if host == "a":
            n["a"] += 1
            if n["a"] <= 2:
                raise TimeoutError("timed out")
        return {"offset": 0.3, "delay": 0.01, "stratum": 2, "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["a", "b", "c"], samples=3)

    assert res["ok"] is True and res["attempts"] == 11
    assert slept == [pytest.approx(timesync.BACKOFF_BASE_S),
                     pytest.approx(timesync.BACKOFF_BASE_S * 2)]
    assert all(v <= timesync.BACKOFF_MAX_S for v in slept)


def test_isolated_losses_do_not_starve_the_update(monkeypatch):
    """R2 的核心症状：零星丢包不再让校时长期不更新（三台不同源）。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: None)
    hits = {"n": 0}

    def q(host, timeout=2.0):
        hits["n"] += 1
        if hits["n"] in (1, 2):
            raise TimeoutError("timed out")
        return {"offset": 0.2, "delay": 0.01, "stratum": 2, "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["a", "b", "c"], samples=4)

    assert res["ok"] is True
    assert res["independent"] == 3
    assert [s["server"] for s in res["samples"]].count("a") == 4
    assert timesync._state["offset"] == pytest.approx(0.2)
    assert timesync.offset() == pytest.approx(0.2)


# ------------------------------------- N5/N6/N7 单源带偏防护
def _query_by_host(offsets, ips=None):
    def q(host, timeout=2.0):
        s = {"offset": offsets[host], "delay": 0.01, "stratum": 2,
             "server": host}
        if ips:
            s["ip"] = ips[host]
        return s
    return q


def test_eleven_hours_offset_from_one_source_is_rejected(monkeypatch):
    """N6：单源（显式只用这一台）偏 +40000s → 连降级都不许，直接拒收。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: "203.0.113.9")
    monkeypatch.setattr(timesync, "_query", _query_by_host(
        {i: 40000.0 for i in ("a", "b", "c", "d")}))

    res = timesync.sync(servers=["a"], samples=4, only_configured=True)

    assert res["ok"] is False
    assert res["degraded"] is False
    assert len(res["samples"]) == 4
    assert "超出可信范围" in res["error"]
    assert timesync._state["offset"] == 0.0        # 状态没被污染


def test_three_hostnames_on_one_ip_are_one_source(monkeypatch):
    """N5：3 个域名经 DNS 投毒指向同一台伪造源 → 按 1 个来源计，且拒收。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: "198.51.100.7")
    monkeypatch.setattr(timesync, "_query", _query_by_host(
        {i: 40000.0 for i in ("a", "b", "c")}))

    res = timesync.sync(servers=["a", "b", "c"], samples=2)

    assert res["ok"] is False
    assert res["degraded"] is False
    assert len(res["samples"]) == 6
    assert "独立来源只有 1" in res["error"]


def test_independent_multi_source_syncs_normally(monkeypatch):
    """多源严格路径：三个真正独立、偏差合理的来源正常校时。"""
    ips = {"a": "192.0.2.10", "b": "203.0.113.20", "c": "198.51.100.30"}
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: ips.get(h))
    monkeypatch.setattr(timesync, "_query",
                        _query_by_host({"a": 0.32, "b": 0.28, "c": 0.35}, ips))

    res = timesync.sync(servers=list(ips), samples=1)

    assert res["ok"] is True
    assert res["degraded"] is False
    assert res["independent"] == 3
    assert len(res["samples"]) == 4              # 3 个样本 + median 汇总
    assert res["offset"] == pytest.approx(0.32)
    assert timesync.offset() == pytest.approx(0.32)


def test_single_source_normal_sync_degrades_but_succeeds(monkeypatch):
    """黑盒复核：只配一台 NTP 的正常用户走降级路径，不再永久 ok=False。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: "192.0.2.10")
    offsets = iter([0.200, 0.202, 0.198, 0.201])

    def q(host, timeout=2.0):
        return {"offset": next(offsets), "delay": 0.01, "stratum": 2,
                "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["a"], samples=4, only_configured=True)

    assert res["ok"] is True
    assert res["degraded"] is True
    assert len(res["samples"]) == 5              # 4 个样本 + median 汇总
    assert res["offset"] == pytest.approx(0.2005)
    assert "单源降级校时" in res["warning"]
    assert timesync.info()["degraded"] is True
    assert timesync.info()["warning"].startswith("单源降级校时")


def test_single_source_one_sample_is_not_enough_for_degraded(monkeypatch):
    """降级也要求连续两个样本，单包不校时。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: "192.0.2.10")
    monkeypatch.setattr(timesync, "_query", _query_by_host({"a": 0.2}))

    res = timesync.sync(servers=["a"], samples=1, only_configured=True)

    assert res["ok"] is False
    assert len(res["samples"]) == 1
    assert "至少 2 个连续样本" in res["error"]


def test_single_source_unstable_samples_are_rejected(monkeypatch):
    """降级路径：两个连续样本互相矛盾 → 判不可信。"""
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda host: "192.0.2.10")
    offsets = iter([0.2, 5.0, 0.2, 5.0])

    def q(host, timeout=2.0):
        return {"offset": next(offsets), "delay": 0.01, "stratum": 2,
                "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    res = timesync.sync(servers=["a"], samples=4, only_configured=True)

    assert res["ok"] is False
    assert res["degraded"] is False
    assert "偏差稳定" in res["error"]


def test_unset_servers_fall_back_to_builtin_list(monkeypatch):
    """没配服务器时自动用内置清单（≥4 个不同来源）。"""
    hits = []

    def q(host, timeout=2.0):
        hits.append(host)
        return {"offset": 0.2, "delay": 0.01, "stratum": 2, "server": host}

    nets = {h: f"10.{i}.0.1" for i, h in enumerate(timesync.DEFAULT_SERVERS)}
    monkeypatch.setattr(timesync, "_query", q)
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: nets.get(h))

    res = timesync.sync(samples=1)

    assert len(timesync.DEFAULT_SERVERS) >= 4
    assert set(hits) == set(timesync.DEFAULT_SERVERS)
    assert res["ok"] is True and res["degraded"] is False


def test_one_configured_server_is_auto_filled_to_multi_source(monkeypatch):
    """只配一台且没声明「只用这些」→ 自动补齐内置源，走严格路径。"""
    hits = []
    nets = {h: f"10.{i}.0.1" for i, h in enumerate(timesync.DEFAULT_SERVERS)}
    nets["a"] = "192.0.2.10"

    def q(host, timeout=2.0):
        hits.append(host)
        return {"offset": 0.2, "delay": 0.01, "stratum": 2, "server": host}

    monkeypatch.setattr(timesync, "_query", q)
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: nets.get(h))

    res = timesync.sync(servers=["a"], samples=1)

    assert set(hits) == {"a"} | set(timesync.DEFAULT_SERVERS)
    assert res["ok"] is True and res["degraded"] is False


def test_consistent_but_absurd_offset_across_sources_is_rejected(monkeypatch):
    """N6：多源一致但绝对值离谱 = 同源伪造特征，不许因“一致”而采纳。"""
    ips = {"a": "192.0.2.10", "b": "203.0.113.20", "c": "198.51.100.30"}
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: ips.get(h))
    monkeypatch.setattr(timesync, "_query", _query_by_host(
        {"a": 40000.0, "b": 40000.2, "c": 39999.8}, ips))

    res = timesync.sync(servers=list(ips), samples=1)

    assert res["ok"] is False
    assert len(res["samples"]) == 3
    assert "超出可信范围" in res["error"]
    assert "高度一致" in res["error"]
    assert timesync._state["offset"] == 0.0


def test_sources_within_one_net16_are_rejected(monkeypatch):
    """N5：同一 /16 内换 IP 也是同一来源。"""
    ips = {"a": "10.1.1.1", "b": "10.1.2.2", "c": "10.1.3.3"}
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: ips.get(h))
    monkeypatch.setattr(timesync, "_query",
                        _query_by_host({i: 0.3 for i in ips}, ips))

    res = timesync.sync(servers=list(ips), samples=1)

    assert res["ok"] is False
    assert len(res["samples"]) == 3
    assert "/16" in res["error"]


def test_clock_backstep_is_rejected_by_monotonic_cross_check(monkeypatch):
    """N7：壁钟被回拨 + 外部源给出矛盾偏移 → 拒收，保留旧值。"""
    mono_now = time.monotonic()
    wall_now = time.time() - 60.0            # 本机壁钟被回拨 60s
    timesync._state.update(offset=0.10, delay=0.01, server="old",
                           at=wall_now + 50.0, at_mono=mono_now - 10.0)
    proxy = types.SimpleNamespace(time=lambda: wall_now,
                                  monotonic=lambda: mono_now,
                                  sleep=lambda s: None)
    monkeypatch.setattr(timesync, "time", proxy)
    ips = {"a": "192.0.2.10", "b": "203.0.113.20", "c": "198.51.100.30"}
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: ips.get(h))
    monkeypatch.setattr(timesync, "_query",
                        _query_by_host({i: 0.10 for i in ips}, ips))

    res = timesync.sync(servers=list(ips), samples=1)

    assert res["ok"] is False
    assert len(res["samples"]) == 3
    assert "单调时钟漂移矛盾" in res["error"]
    assert timesync._state["offset"] == 0.10


def test_large_offset_needs_explicit_confirmation(monkeypatch):
    """N6：分钟级阈值可由调用方/环境变量显式放宽。"""
    ips = {"a": "192.0.2.10", "b": "203.0.113.20", "c": "198.51.100.30"}
    monkeypatch.setattr(timesync, "time", _sleep_proxy([]))
    monkeypatch.setattr(timesync, "_resolve_ip", lambda h: ips.get(h))
    monkeypatch.setattr(timesync, "_query", _query_by_host(
        {i: 40000.0 for i in ips}, ips))

    res = timesync.sync(servers=list(ips), samples=1, allow_large_offset=True)
    assert res["ok"] is True
    assert res["offset"] == pytest.approx(40000.0)

    timesync._state.update(offset=0.0, at=None, at_mono=None, server=None,
                           delay=None, last_error="")
    monkeypatch.setenv(timesync.ALLOW_LARGE_OFFSET_ENV, "1")
    res2 = timesync.sync(servers=list(ips), samples=1)
    assert res2["ok"] is True
