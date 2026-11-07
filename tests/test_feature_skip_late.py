"""来不及跳过（buy.skip_late.threshold_s）—— 功能 / 联通 / 异常 / 互斥。

契约：`_feature_contract.md` §2.4、§3、§4、§5、§7。

真机背景：抽签模型下点进去只是拿号，公示期解锁那一瞬间是一堆人同时进池。
倒计时只剩一两秒才被读到的那件，等确认框弹出来常常已经结束 —— 硬进去只是
白占一次进场机会，所以首次读到就判，来不及就跳过。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, engine as eng_mod, skip_late, switches  # noqa: E402
from core import server as srv  # noqa: E402
from core.config import Config  # noqa: E402
from core.countdown import Clock  # noqa: E402
from core.engine import Engine  # noqa: E402

KEY = "buy.skip_late.threshold_s"


# ---------------- 纯判定 ----------------

def _clock_with(observations):
    """observations: [(显示秒, 采样时刻)]，等价于引擎连续几帧读同一个钟。"""
    c = Clock()
    for sec, t in observations:
        c.observe(sec, t)
    return c


def test_inferred_remaining_beats_truncated_display():
    """显示值只是下界：显示 2 秒、反推剩 1.5 秒时必须按 1.5 判。"""
    c = _clock_with([(3, 100.0), (2, 101.0), (1, 102.0)])
    # zero_at = 104；102.5 时真实剩余 1.5 秒，而显示是 1 秒
    v = skip_late.judge(2, c, 102.5, display_sec=1, kind="unlock")
    assert v["skip"] is True
    assert v["source"] == "inferred"
    assert v["remaining_s"] == pytest.approx(1.5)
    assert "不足" in v["reason"]


def test_enough_time_is_not_skipped():
    c = _clock_with([(5, 100.0), (4, 101.0)])
    # zero_at = 106；103 时剩 3 秒
    v = skip_late.judge(2, c, 103.0, display_sec=3, kind="unlock")
    assert v["skip"] is False
    assert v["source"] == "inferred"
    assert v["remaining_s"] == pytest.approx(3.0)


def test_display_fallback_only_for_unlock_kind():
    """没有反推就退回显示值，但「剩余：X分Y秒」是下架倒计时，不能拿来判。"""
    v = skip_late.judge(5, None, None, display_sec=1, kind="unlock")
    assert v["skip"] is True
    assert v["source"] == "display"

    v = skip_late.judge(5, None, None, display_sec=1, kind="remain")
    assert v["skip"] is False
    assert v["remaining_s"] is None
    assert "没有可用" in v["reason"]


def test_zero_display_and_unlocked_are_never_skipped():
    """显示 0 秒 = 已解锁，这种不跳过（常常 4 秒就成交）。"""
    assert skip_late.judge(10, None, None, display_sec=0, kind="unlock")["skip"] is False

    c = _clock_with([(2, 100.0)])
    # zero_at = 103；104 时早过了解锁时刻
    v = skip_late.judge(10, c, 104.0, display_sec=0, kind="unlock")
    assert v["skip"] is False
    assert "已解锁" in v["reason"]


def test_threshold_zero_disables():
    c = _clock_with([(1, 100.0)])
    v = skip_late.judge(0, c, 100.0, display_sec=1, kind="unlock")
    assert v["skip"] is False
    assert "关闭" in v["reason"]


# ---------------- 严格取数 / 异常输入 ----------------

def test_threshold_default_and_ok_values():
    assert skip_late.threshold({}) == 2
    assert skip_late.threshold({"buy": {}}) == 2
    assert skip_late.threshold({"buy": {"skip_late": {}}}) == 2
    for sec in (0, 1, 2, 10, 3600):
        got = skip_late.threshold({"buy": {"skip_late": {"threshold_s": sec}}})
        assert got == sec
    assert skip_late.threshold(
        {"buy": {"skip_late": {"threshold_s": 2.0}}}) == 2, "整数值的浮点可以收"


@pytest.mark.parametrize("bad, needle", [
    (float("nan"), "有限数字"),
    (float("inf"), "有限数字"),
    (float("-inf"), "有限数字"),
    ("3", "不是数字"),
    (None, "不是数字"),
    (True, "不是数字"),
    (1.5, "必须是整数"),
    (-1, "小于下限"),
    (-0.5, "必须是整数"),
    (3601, "大于上限"),
    (10 ** 9, "大于上限"),
])
def test_threshold_rejects_dirty_values_with_readable_reason(bad, needle):
    """手改 config.json 写成脏值：报字段 + 原因，不静默套默认值。"""
    with pytest.raises(skip_late.SkipLateConfigError) as ei:
        skip_late.threshold({"buy": {"skip_late": {"threshold_s": bad}}})
    msg = str(ei.value)
    assert KEY in msg
    assert needle in msg


def test_threshold_rejects_wrong_container_types():
    with pytest.raises(skip_late.SkipLateConfigError) as ei:
        skip_late.threshold({"buy": {"skip_late": 3}})
    assert "buy.skip_late 必须是对象" in str(ei.value)
    with pytest.raises(skip_late.SkipLateConfigError):
        skip_late.threshold({"buy": []})


# ---------------- 保存入口拒收（API 同一条路） ----------------

@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), -1, 3601, "abc", None, 1.5,
])
def test_switch_save_rejects_before_writing(bad, tmp_path, monkeypatch):
    """走 switches.apply_all，和 /api/switches 完全同一条校验。"""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg = Config()
    before = json.dumps(cfg.data, sort_keys=True)
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.apply_all(cfg.data, {KEY: bad})
    assert KEY in str(ei.value)
    # 整批拒收：内存里也不能半截生效
    assert json.dumps(cfg.data, sort_keys=True) == before


def test_switch_meta_shape():
    it = next(s for s in switches.SWITCHES if s["key"] == KEY)
    assert it["group"] == "抢购设置"
    assert it["kind"] == "int" and it["integral"] is True
    assert it["default"] == 2
    assert it["hint"] == "秒，0=关闭"
    assert it["desc"].startswith("首次读到这件")
    assert switches.BOUNDS[KEY] == (0, 3600)


# ---------------- 真实购买回路 ----------------

class FakeCfg(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setdefault("regions", {
            "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1},
        })
        self.setdefault("buy", {})

    def region(self, name):
        return (self.get("regions") or {}).get(name)

    def get(self, k, d=None):
        return super().get(k, d)


class FakeScreen:
    def norm_to_px(self, box):
        return (100, 200, 50, 30)

    def grab(self, px=None):
        return np.zeros((4, 4, 3), np.uint8)


class FakeNav:
    made = []

    def __init__(self, *a, **k):
        FakeNav.made.append(self)
        self.calls = []
        self.page = 1

    def where(self):
        return "onsale", 0.99

    def pub_filter(self, want):
        self.calls.append(("pub_filter", want))

    def ons_refresh(self):
        self.calls.append(("refresh",))

    def ons_page(self):
        return self.page

    def ons_next_page(self):
        self.calls.append(("next_page",))
        self.page += 1
        return self.page

    def ons_prev_page(self):
        self.calls.append(("prev_page",))
        self.page = max(1, self.page - 1)
        return self.page


class FakeMouse:
    def __init__(self):
        self.clicked = []

    def ensure_foreground(self, hwnd, tries=3, gap=0.25, screen=None):
        return True

    def click(self, x, y, **kw):
        self.clicked.append((x, y))


def _cd_words(sec, kind="unlock"):
    if kind == "unlock":
        ws = ["0", "分", str(sec), "秒", "后", "解", "锁", "购", "买"]
    else:
        ws = ["剩余", "：", "0", "分", str(sec), "秒"]
    return [{"text": t, "x": i * 20, "y": 0, "w": 18, "h": 16}
            for i, t in enumerate(ws)]


def _row(price):
    return {"text": "KC17 突击步枪造物纪元", "price": float(price), "craft": None,
            "wear": None, "wear_value": None, "x0": 40.0, "cy": 30.0}


def _engine():
    e = Engine()
    e._stop = threading.Event()
    e._target_hwnd = 1
    e._said = []
    e._say = lambda msg, level="info": e._said.append((level, msg))
    return e


def _prepare(e, cfg, monkeypatch, clocks, serials=("P1B",), min_lead=None):
    if min_lead is not None:
        real_get = switches.get

        def _get(c, k, *a, **kw):
            if k == "countdown.min_lead":
                return min_lead
            return real_get(c, k, *a, **kw)
        monkeypatch.setattr(eng_mod.switches, "get", _get)
    FakeNav.made = []
    monkeypatch.setattr(eng_mod, "Navigator", FakeNav)
    monkeypatch.setattr(eng_mod, "Keyboard", lambda *a, **k: None)
    rows = [_row(2050), _row(2888)]
    monkeypatch.setattr(eng_mod.market, "scan", lambda img: rows)
    it = iter(clocks)
    monkeypatch.setattr(e, "_ocr_clock", lambda scr, px: next(it))
    sit = iter(serials)
    monkeypatch.setattr(e, "_read_serial", lambda scr: next(sit))
    clicks = []
    monkeypatch.setattr(e, "_click", lambda m, x, y: clicks.append((x, y)))
    cfg["active_task"] = {"min_price": 1000, "max_price": 3000}
    cfg["regions"]["clock"] = {"x": 0.8, "y": 0.79, "w": 0.1, "h": 0.5}
    target = e._countdown_prepare(cfg, FakeScreen(), FakeMouse(), [])
    return target, clicks


def test_prepare_skips_late_candidate_and_does_not_enter(monkeypatch):
    """首读只剩 1 秒（反推 2 秒）< 3 秒：跳过，不拿号，不点购买。"""
    e = _engine()
    cfg = FakeCfg()
    cfg["buy"] = {"skip_late": {"threshold_s": 3}}
    cfg["countdown"] = {"min_lead": 0}               # 只测本功能，不让旧闸抢答
    target, clicks = _prepare(e, cfg, monkeypatch,
                              [_cd_words(1), _cd_words(9)], min_lead=0)

    assert target is not None and target["sec"] == 9, "该选 9 秒那件"
    # 两件各点一次读详情 + 选中第二件再点一次 = 3。跳过只省掉「进场拿号」，
    # 不会为跳过额外补一次点击。
    assert len(clicks) == 3
    late = e._stats["late_skips"]
    assert len(late) == 1
    assert late[0]["source"] == "inferred"
    assert late[0]["remaining_s"] == pytest.approx(2.0, abs=0.2)
    assert "不足 3 秒" in late[0]["reason"]
    assert e.status()["stats"]["late_skips"] == late, \
        "契约 §3.3：跳过记录必须能从 engine.status()['stats'] 读到"
    assert any("来不及" in m and "跳过不进场" in m for _, m in e._said), \
        "跳过必须有一条独立日志，不能和购买失败混在一起"


def test_prepare_off_buys_the_fast_one(monkeypatch):
    e = _engine()
    cfg = FakeCfg()
    cfg["buy"] = {"skip_late": {"threshold_s": 0}}   # 关闭
    cfg["countdown"] = {"min_lead": 0}               # 排除旧的短倒计时闸，单测本功能
    target, clicks = _prepare(e, cfg, monkeypatch,
                              [_cd_words(1), _cd_words(9)], min_lead=0)
    assert target is not None and target["sec"] == 1
    assert e._stats["late_skips"] == []
    assert len(clicks) == 3, "两件各一次详情点击 + 选中件再点一次"


# ---------------- 与已有开关共存 / 互斥 ----------------

def test_min_lead_and_skip_late_both_apply_stricter(monkeypatch):
    """min_lead 用显示秒拦「显示太短」，skip_late 用反推闸；两条都生效。"""
    e = _engine()
    cfg = FakeCfg()
    cfg["buy"] = {"skip_late": {"threshold_s": 2}}
    cfg["countdown"] = {"min_lead": 6}      # 显示 5 秒 → min_lead 拦
    target, clicks = _prepare(e, cfg, monkeypatch,
                              [_cd_words(5), _cd_words(30)], min_lead=6)
    assert target is not None and target["sec"] == 30
    # 5 秒那件被 min_lead 拦下（反推 6 秒不小于 skip_late 的 2 秒），
    # 两条阈值各管各的，统计里不把它算成「来不及」。
    assert e._stats["late_skips"] == []
    assert any("太短" in m or "来不及" in m for _, m in e._said)


def test_skip_late_does_not_touch_other_buy_settings(monkeypatch):
    """和「买到即停 / 提前量 / 刷新档位」同时开：只跳过，不改别人。"""
    e = _engine()
    cfg = FakeCfg()
    cfg["buy"] = {"skip_late": {"threshold_s": 3}, "followup": {"enabled": False}}
    cfg["countdown"] = {"min_lead": 0}
    cfg["auto"] = {"stop_after_hit": True}
    watch = ("buy", "countdown", "auto", "scan", "click", "stop", "mouse")
    before = {k: json.loads(json.dumps(cfg[k])) for k in watch if k in cfg}
    target, _ = _prepare(e, cfg, monkeypatch,
                         [_cd_words(1), _cd_words(9)], min_lead=0)
    assert target["sec"] == 9
    assert len(e._stats["late_skips"]) == 1
    after = {k: cfg[k] for k in watch if k in cfg}
    assert after == before, "跳过不能改配置（买到即停/提前量/刷新档位都不许动）"
    # 「提前量」是引擎常量，跳过路径不许动它
    assert eng_mod.FIRE_LEAD_S == 5.0


def test_config_file_roundtrip(tmp_path, monkeypatch):
    """控件值 → config.json → Config.load()，三段都真的落到盘上。"""
    p = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    cfg = Config()
    switches.apply_all(cfg.data, {KEY: 3})
    cfg.save()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["buy"]["skip_late"]["threshold_s"] == 3

    reloaded = Config.load()
    assert switches.get(reloaded, KEY) == 3
    assert skip_late.threshold(reloaded) == 3


# ---------------- 前后端联通（真 HTTP + 真无头浏览器） --------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    Config().save()
    port = _free_port()
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)
    yield f"http://127.0.0.1:{port}", p
    httpd.shutdown()
    httpd.server_close()


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_backend_snapshot_and_reject_without_writing(live):
    url, path = live
    code, data = _get_json(url + "/api/switches")
    assert code == 200
    item = next(it for g in data["groups"] for it in g["items"] if it["key"] == KEY)
    assert item["kind"] == "int" and item["value"] == 2
    assert item["hint"] == "秒，0=关闭"
    assert "跳过" in item["desc"]

    before = path.read_bytes()
    code, bad = _post_json(url + "/api/switches", {"values": {KEY: 3601}})
    assert code == 400 and bad["ok"] is False
    assert KEY in bad["msg"] and "上限" in bad["msg"]
    assert path.read_bytes() == before, "越界拒收时磁盘一个字节都不动"

    code, good = _post_json(url + "/api/switches", {"values": {KEY: 3}})
    assert code == 200 and good["ok"] is True
    assert good["applied"][KEY] == 3
    assert json.loads(path.read_text(encoding="utf-8"))["buy"]["skip_late"]["threshold_s"] == 3


_LAUNCH_OK = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p:\n"
    "    b = p.chromium.launch(headless=True)\n"
    "    b.close()\n"
)


def _playwright_python():
    """挑一个不仅能 import、而且真能拉起无头 Chromium 的解释器。

    项目 venv 里可能装了 playwright 但没下浏览器（报 chromium_headless_shell
    不存在），这时退回系统 Python 里已装好的那一份。
    """
    exe = [sys.executable, shutil.which("python"), shutil.which("py")]
    for e in exe:
        if not e:
            continue
        try:
            r = subprocess.run([e, "-c", _LAUNCH_OK],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                return e
        except Exception:                   # noqa: BLE001
            pass
    return None


_CHILD = r'''
import json, os
from playwright.sync_api import sync_playwright

url = os.environ["SL_URL"]
key = "buy.skip_late.threshold_s"
sel = '#blk-fire-skip-late [data-sw="%s"]' % key

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-first-run"])
    pg = b.new_page()
    pg.goto(url, wait_until="domcontentloaded")
    pg.wait_for_selector(sel, state="attached", timeout=40000)
    pg.click("#tab-set")
    pg.click('.setnav [data-setpane="fire"]')
    pg.wait_for_selector(sel, state="visible", timeout=30000)
    out = {"first": pg.input_value(sel),
           "in_block": pg.eval_on_selector_all(
               "#blk-fire-skip-late [data-sw]", "els => els.length")}

    # 设置页每秒会重绘一次开关列表（paintStats），Playwright 的 fill 可能
    # 正好落在被替换掉的旧节点上；改成在页面里设值 + 派发 input/change，
    # 走的就是用户改控件时同一条委托监听。
    def setv(v):
        pg.eval_on_selector(
            sel,
            "(e, v) => { e.value = v;"
            " e.dispatchEvent(new Event('input', {bubbles: true}));"
            " e.dispatchEvent(new Event('change', {bubbles: true})); }",
            v)

    setv("3")
    pg.wait_for_function(
        "s => { const e = document.querySelector(s); return e && e.value === '3'; }",
        arg=sel, timeout=30000)
    out["saved"] = pg.input_value(sel)

    setv("3601")
    pg.wait_for_function(
        "() => document.querySelector('#toastHost').innerText.includes('上限')",
        timeout=30000)  # 并发/高负载下 POST 往返可能超过 10s，断言不变只放宽等待
    out["err_toast"] = pg.inner_text("#toastHost")

    setv("0")
    pg.wait_for_function(
        "s => { const e = document.querySelector(s); return e && e.value === '0'; }",
        arg=sel, timeout=30000)
    out["off"] = pg.input_value(sel)
    b.close()

print(json.dumps(out, ensure_ascii=False))
'''


def test_frontend_headless_control_to_config(live):
    """无头 Chromium：区块挂载、控件 → /api/switches → config.json。"""
    exe = _playwright_python()
    if not exe:
        pytest.skip("没有可用 Playwright 解释器")
    url, path = live
    env = dict(os.environ, SL_URL=url)
    r = subprocess.run([exe, "-c", _CHILD], capture_output=True,
                       text=True, timeout=120, env=env, cwd=os.path.dirname(
                           os.path.dirname(os.path.abspath(__file__))))
    assert r.returncode == 0, r.stderr[-4000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["first"] == "2"
    assert out["in_block"] == 1, "控件必须且只能出现在 #blk-fire-skip-late 里"
    assert out["saved"] == "3"
    assert "上限" in out["err_toast"], "越界时界面要拿到可读原因"
    assert out["off"] == "0"

    # 浏览器点出来的值真的落盘，并且后端再读也是它
    disk = json.loads(path.read_text(encoding="utf-8"))["buy"]["skip_late"]["threshold_s"]
    assert disk == 0
    code, data = _get_json(url + "/api/switches")
    item = next(it for g in data["groups"] for it in g["items"] if it["key"] == KEY)
    assert item["value"] == 0
    assert skip_late.threshold(Config.load()) == 0

