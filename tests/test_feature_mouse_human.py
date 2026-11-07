"""拟人鼠标轨迹（mouse.human.*）—— 五维验证。

契约：`_feature_contract.md` §2.1（配置键与范围）、§3.3（D1~D5）、§4.2（strict /
integral）、§4.5（引擎扩展点）、§5（引擎状态必须回读）。

全部用例都不构造真实 Mouse、不移动真实光标：轨迹是纯计算，通道语义用
`Mouse.__new__` 只探 message 分支，前端用无头 Chromium。
"""
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, humanmouse, input as inputmod, switches  # noqa: E402
from core import server as srv  # noqa: E402
from core.config import Config  # noqa: E402

TRAJ, MS, CURVE = ("mouse.human.traj", "mouse.human.move_ms", "mouse.human.curve")
BLOCK = "#blk-fire-human"


# ---------------------------------------------------------------- 工具
def _spec(traj="auto", move_ms=120, curve=0.35):
    return {"traj": traj, "move_ms": move_ms, "curve": curve, "problems": []}


def _cfg(traj="auto", move_ms=120, curve=0.35):
    return {"mouse": {"human": {"traj": traj, "move_ms": move_ms, "curve": curve}}}


def _speeds(points):
    out = []
    for a, b in zip(points, points[1:]):
        dt = (b[2] - a[2]) / 1000.0
        out.append(math.hypot(b[0] - a[0], b[1] - a[1]) / dt if dt > 0 else 0.0)
    return out


def _deviation(points, start, end):
    """离弦最大距离（px）—— 0 就是直线。"""
    x1, y1 = end[0] - start[0], end[1] - start[1]
    length = math.hypot(x1, y1)
    return max(abs(x1 * (p[1] - start[1]) - y1 * (p[0] - start[0])) / length
               for p in points)


# ---------------------------------------------------------------- D1 功能可用
@pytest.mark.parametrize("traj", ["windmouse", "bezier"])
def test_d1_real_call_is_curved_and_accelerating(traj):
    """真实调一次规划：点序列 > 2、不共线、速度不是一条水平线。"""
    p = humanmouse.plan((0, 0), (400, 300), _spec(traj=traj), rng=random.Random(11))
    pts = p["points"]
    assert p["type"] == traj
    assert len(pts) > 2
    assert pts[0][:2] == [0.0, 0.0]
    assert pts[-1][:2] == [400.0, 300.0], "落点必须精确"
    assert p["ms"] == 120.0

    dev = _deviation(pts, (0, 0), (400, 300))
    assert dev > 1.5, f"轨迹几乎共线（最大偏移 {dev:.2f}px）"

    sp = _speeds(pts)
    body = sp[1:-1]
    assert max(body) > min(body) * 2.0, "速度必须是变化的，不能匀速"
    peak = body.index(max(body))
    assert 0 < peak < len(body) - 1, "峰值要在中段：先加速、后减速"
    assert body[0] < max(body) * 0.8 and body[-1] < max(body) * 0.8


def test_d1_auto_picks_windmouse_for_long_and_bezier_for_short():
    long_ = humanmouse.plan((0, 0), (360, 240), _spec("auto"), rng=random.Random(1))
    short = humanmouse.plan((0, 0), (40, 30), _spec("auto"), rng=random.Random(1))
    assert long_["type"] == "windmouse"
    assert short["type"] == "bezier"


def test_d1_fire_phase_is_capped_and_still_a_short_curve():
    """开火连点：耗时不超 min(move_ms, 0.25×间隔, 30ms)，且仍是 3 点短曲线。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.4), mode="legacy")
    first = t.path_for(300, 200, start=(0, 0), now=0.0)          # 非开火
    second = t.path_for(300, 200, start=(0, 0), now=0.02)        # 20ms 后再点
    assert first and second
    assert first[1] == 120.0
    assert second[1] == pytest.approx(min(120.0, 0.25 * 20.0, 30.0))
    assert len(second[0]) <= 3, "开火期只给 2~3 个采样点"
    assert second[0][-1][:2] == [300.0, 200.0]


def test_d1_curve_zero_is_straight_and_curve_one_is_wide():
    straight = humanmouse.plan((0, 0), (400, 0), _spec("bezier", 120, 0.0),
                               rng=random.Random(5))
    wide = humanmouse.plan((0, 0), (400, 0), _spec("bezier", 120, 1.0),
                           rng=random.Random(5))
    assert _deviation(straight["points"], (0, 0), (400, 0)) < 0.6
    assert _deviation(wide["points"], (0, 0), (400, 0)) > 20


def test_d1_pack_used_when_present(tmp_path):
    d = tmp_path / "traj"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({
        "name": "sample",
        "points": [[0, 0, 0], [40, 10, 30], [90, 40, 60], [120, 70, 90],
                   [130, 90, 120], [120, 100, 150], [100, 100, 180],
                   [80, 95, 210], [40, 90, 240], [0, 90, 270]],
    }), encoding="utf-8")
    t = humanmouse.Trajectory(_cfg("pack"), mode="legacy", pack_dir=str(d))
    assert t.effective == "legacy" and t.packs[0]["name"] == "sample"
    pts, ms = t.path_for(400, 300, start=(0, 0))
    assert t.last["type"] == "pack"
    assert ms == 120.0
    assert pts[-1][:2] == [400.0, 300.0]


# ---------------------------------------------------------------- D2 前后端联通
def test_d2_snapshot_exposes_three_controls():
    groups = switches.snapshot({})
    items = {i["key"]: i for g in groups for i in g["items"]}
    assert items[TRAJ]["kind"] == "select"
    assert [o[0] for o in items[TRAJ]["options"]] == ["auto", "windmouse", "bezier", "pack"]
    assert items[MS]["kind"] == "int" and items[MS]["integral"] is True
    assert items[MS]["hint"] == "毫秒" and items[MS]["value"] == 120
    assert items[CURVE]["kind"] == "float" and items[CURVE]["value"] == 0.35
    assert switches.BOUNDS[MS] == (20, 800)
    assert switches.BOUNDS[CURVE] == (0.0, 1.0)
    fire = next(g for g in groups if g["name"] == "抢购设置")
    assert [i["key"] for i in fire["items"] if i["key"].startswith("mouse.human.")] == \
        [TRAJ, MS, CURVE], "三个控件必须落在同一组（#swFire）"


def test_d2_value_to_config_to_behavior(tmp_path, monkeypatch):
    """三段：改值 → config.json 真的变 → 轨迹时长真的跟着变。"""
    p = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    cfg = Config()
    switches.apply_all(cfg.data, {MS: 300, CURVE: 0.8})
    cfg.save()

    assert json.loads(p.read_text(encoding="utf-8"))["mouse"]["human"]["move_ms"] == 300
    reloaded = Config.load()
    assert switches.get(reloaded, MS) == 300

    t = humanmouse.Trajectory(reloaded, mode="legacy")
    pts, ms = t.path_for(400, 300, start=(0, 0))
    assert ms == 300.0, "move_ms 改到 300，规划时长必须跟着变"
    assert len(pts) == max(3, min(40, int(round(300 / 12.0))))

    # 再改一次，行为必须再变（不是只在第一次生效）
    cfg2 = Config.load()
    switches.apply_all(cfg2.data, {MS: 60})
    cfg2.save()
    pts2, ms2 = humanmouse.Trajectory(Config.load(), mode="legacy").path_for(
        400, 300, start=(0, 0))
    assert ms2 == 60.0 and len(pts2) < len(pts)


# ---------------------------------------------------------------- D3 异常输入
@pytest.mark.parametrize("key,bad,needle", [
    (MS, None, "不是数字"),
    (MS, 1.5, "必须是整数"),
    (MS, float("nan"), "有限数字"),
    (MS, float("inf"), "有限数字"),
    (MS, [1], "不是数字"),
    (CURVE, "abc", "不是数字"),
    (CURVE, None, "不是数字"),
    (CURVE, float("nan"), "有限数字"),
    (CURVE, float("inf"), "有限数字"),
    (TRAJ, "bogus", "不在可选值"),
    (TRAJ, None, "不在可选值"),
    (TRAJ, 123, "不在可选值"),
])
def test_d3_dirty_values_rejected_with_readable_reason(key, bad, needle, tmp_path,
                                                      monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    cfg = Config()
    before = json.dumps(cfg.data, sort_keys=True)
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.apply_all(cfg.data, {key: bad})
    assert key in str(ei.value) and needle in str(ei.value)
    assert json.dumps(cfg.data, sort_keys=True) == before, "整批拒收，内存也不能半截"


def test_d3_dirty_disk_value_is_reported_not_silently_defaulted():
    t = humanmouse.Trajectory({"mouse": {"human": {"traj": "bogus", "move_ms": 999,
                                                  "curve": 2.5}}}, mode="legacy")
    assert t.effective == "invalid_config"
    assert t.active is False
    assert "mouse.human.traj" in t.reason and "不在可选值" in t.reason
    assert "mouse.human.move_ms" in t.reason and "越界" in t.reason
    assert "mouse.human.curve" in t.reason
    st = t.status()
    assert st["problems"] and st["effective"] == "invalid_config"


def test_d3_missing_fields_fall_back_to_contract_defaults():
    t = humanmouse.Trajectory({}, mode="interception")
    assert t.spec == {"traj": "auto", "move_ms": 120, "curve": 0.35, "problems": []}
    assert t.effective == "interception"


# ---------------------------------------------------------------- D4 越界拒收
@pytest.mark.parametrize("key,bad,needle,bound", [
    (MS, 19, "小于下限", "20"),
    (MS, 801, "大于上限", "800"),
    (MS, 10 ** 6, "大于上限", "800"),
    (CURVE, -0.01, "小于下限", "0"),
    (CURVE, 1.01, "大于上限", "1"),
])
def test_d4_out_of_range_rejected(key, bad, needle, bound):
    with pytest.raises(switches.SwitchValueError) as ei:
        switches.apply_all({}, {key: bad})
    msg = str(ei.value)
    assert key in msg and needle in msg and bound in msg


def test_d4_boundary_values_are_accepted():
    got = switches.apply_all({}, {MS: 20, CURVE: 0.0})
    assert got[MS] == 20 and got[CURVE] == 0.0
    got = switches.apply_all({}, {MS: 800, CURVE: 1.0})
    assert got[MS] == 800 and got[CURVE] == 1.0


def test_d4_rejected_value_never_reaches_disk(live):
    url, path = live
    before = path.read_bytes()
    code, body = _post_json(url + "/api/switches", {"values": {MS: 801}})
    assert code == 400 and body["ok"] is False
    assert "上限" in body["msg"] and MS in body["msg"]
    assert path.read_bytes() == before, "越界拒收时磁盘一个字节都不动"


# ---------------------------------------------------------------- D5 共存 / 降级
def test_d5_message_channel_reports_degraded_and_moves_nothing():
    cap = inputmod.trajectory_capability("message")
    assert cap["effective"] == "degraded" and cap["active"] is False
    assert "纯消息通道无真实移动" in cap["reason"]

    m = object.__new__(inputmod.Mouse)          # 只探 message 分支，不碰硬件
    m._mode = "message"
    m._last_path = None
    assert m.move_path([[0, 0, 0], [100, 80, 40]]) is False
    assert m._last_path["used"] is False
    assert "不生效" in m._last_path["reason"]


def test_d5_pack_missing_degrades_to_bezier_with_reason():
    t = humanmouse.Trajectory(_cfg("pack"), mode="legacy")
    assert t.effective == "pack_fallback_auto"
    assert "轨迹包不可用" in t.reason
    pts, ms = t.path_for(400, 300, start=(0, 0))
    assert t.last["type"] == "bezier", "无包时必须真的走贝塞尔"
    assert ms == 120.0 and pts[-1][:2] == [400.0, 300.0]


def test_d5_bad_pack_is_ignored_as_a_whole(tmp_path):
    d = tmp_path / "traj"
    d.mkdir()
    (d / "bad.json").write_text(json.dumps(
        {"points": [[0, 0, 0], [10, 10, 20]]}), encoding="utf-8")   # 点数不够
    t = humanmouse.Trajectory(_cfg("pack"), mode="interception", pack_dir=str(d))
    assert t.packs == [] and t.effective == "pack_fallback_auto"
    assert any("点数不足" in w for w in t.pack_warns)
    pts, _ = t.path_for(400, 300, start=(0, 0))
    assert t.last["type"] == "bezier"


def test_d5_coexists_with_other_mouse_settings(tmp_path, monkeypatch):
    """和空闲漫游 + 连点 + 扫货去重同时开：各写各的，谁也不改谁。"""
    p = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(p))
    cfg = Config()
    values = {MS: 200, CURVE: 0.5,
              "mouse.idle.mode": "move",
              "mouse.idle.interval_ms": 2000,
              "click.burst.mode": "append",
              "scan.repeat.max": 2}
    applied = switches.apply_all(cfg.data, values)
    cfg.save()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["mouse"]["human"]["move_ms"] == 200
    assert raw["mouse"]["idle"]["mode"] == "move"
    assert raw["click"]["burst"]["mode"] == "append"
    assert set(applied) >= {MS, CURVE, "mouse.idle.mode"}

    t = humanmouse.Trajectory(Config.load(), mode="legacy")
    assert t.active is True and t.spec["move_ms"] == 200
    assert t.path_for(400, 300, start=(0, 0))[1] == 200.0

    # 反过来：改 idle 不能把 human 冲掉
    cfg2 = Config.load()
    switches.apply_all(cfg2.data, {"mouse.idle.mode": "off"})
    cfg2.save()
    raw2 = json.loads(p.read_text(encoding="utf-8"))
    assert raw2["mouse"]["human"] == raw["mouse"]["human"]


# ---------------------------------------------------------------- 前后端真联通
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
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
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


def test_http_changes_config_and_behavior(live):
    url, path = live
    code, data = _get_json(url + "/api/switches")
    assert code == 200
    items = {i["key"]: i for g in data["groups"] for i in g["items"]}
    assert items[MS]["value"] == 120

    code, good = _post_json(url + "/api/switches", {"values": {TRAJ: "windmouse", MS: 240}})
    assert code == 200 and good["ok"] is True
    disk = json.loads(path.read_text(encoding="utf-8"))["mouse"]["human"]
    assert disk["traj"] == "windmouse" and disk["move_ms"] == 240

    t = humanmouse.Trajectory(Config.load(), mode="legacy")
    assert t.spec["traj"] == "windmouse"
    assert t.path_for(400, 300, start=(0, 0))[1] == 240.0


_LAUNCH_OK = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p:\n"
    "    b = p.chromium.launch(headless=True)\n"
    "    b.close()\n"
)


def _playwright_python():
    for e in [sys.executable, shutil.which("python"), shutil.which("py")]:
        if not e:
            continue
        try:
            if subprocess.run([e, "-c", _LAUNCH_OK], capture_output=True,
                              timeout=60).returncode == 0:
                return e
        except Exception:                   # noqa: BLE001
            pass
    return None


_CHILD = r'''
import json, os
from playwright.sync_api import sync_playwright

url = os.environ["HM_URL"]
sel = '#blk-fire-human [data-sw="mouse.human.move_ms"]'
sel_traj = '#blk-fire-human [data-sw="mouse.human.traj"]'

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-first-run"])
    pg = b.new_page()
    # 共享首页有「上手引导」：首访 400ms 后自动弹，会挡住设置页。
    # 在页面脚本执行前写 localStorage，等价于「已经看过」，不碰别人的区块。
    pg.add_init_script("try { localStorage.setItem('dafors.guided', '1'); }"
                       " catch (e) {}")
    pg.goto(url, wait_until="domcontentloaded")
    pg.wait_for_selector(sel, state="attached", timeout=40000)
    # 共享 index.html 里可能有别的分支的引导弹窗；先关掉，别让它挡住设置页
    pg.evaluate("() => document.querySelectorAll('dialog[open]')"
                ".forEach(d => { try { d.close(); } catch (e) {} })")
    pg.click("#tab-set")
    pg.evaluate("() => document.querySelectorAll('dialog[open]')"
                ".forEach(d => { try { d.close(); } catch (e) {} })")
    pg.click('.setnav [data-setpane="fire"]')
    pg.wait_for_selector(sel, state="visible", timeout=30000)
    out = {"first": pg.input_value(sel),
           "in_block": pg.eval_on_selector_all(
               "#blk-fire-human [data-sw]", "els => els.length"),
           "traj_opts": pg.eval_on_selector(
               sel_traj, "e => Array.from(e.options).map(o => o.value)")}

    def setv(sel, v, ev="input"):
        pg.eval_on_selector(
            sel,
            "(e, a) => { e.value = a[0];"
            " e.dispatchEvent(new Event(a[1], {bubbles: true})); }",
            [v, ev])

    setv(sel, "300")
    setv(sel, "300", "change")
    pg.wait_for_function(
        "s => { const e = document.querySelector(s); return e && e.value === '300'; }",
        arg=sel, timeout=30000)
    out["saved"] = pg.input_value(sel)

    setv(sel, "801")
    setv(sel, "801", "change")
    pg.wait_for_function(
        "() => document.querySelector('#toastHost').innerText.includes('上限')",
        timeout=30000)  # 并发/高负载下 POST 往返可能超过 10s，断言不变只放宽等待
    out["err_toast"] = pg.inner_text("#toastHost")

    setv(sel_traj, "bezier", "change")
    pg.wait_for_function(
        "s => { const e = document.querySelector(s); return e && e.value === 'bezier'; }",
        arg=sel_traj, timeout=30000)
    out["traj"] = pg.input_value(sel_traj)
    b.close()

print(json.dumps(out, ensure_ascii=False))
'''


def test_frontend_control_reaches_backend(live):
    """无头 Chromium：区块挂载 → 控件改值 → /api/switches → 磁盘 + 行为。"""
    exe = _playwright_python()
    if not exe:
        pytest.skip("没有可用 Playwright 解释器")
    url, path = live
    r = subprocess.run([exe, "-c", _CHILD], capture_output=True, text=True,
                       timeout=150, env=dict(os.environ, HM_URL=url),
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert r.returncode == 0, r.stderr[-4000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["first"] == "120"
    assert out["in_block"] == 3, "三个控件必须都在 #blk-fire-human 里"
    assert out["traj_opts"] == ["auto", "windmouse", "bezier", "pack"]
    assert out["saved"] == "300"
    assert "上限" in out["err_toast"], "越界要在界面上给出可读原因"
    assert out["traj"] == "bezier"

    disk = json.loads(path.read_text(encoding="utf-8"))["mouse"]["human"]
    assert disk["move_ms"] == 300 and disk["traj"] == "bezier"
    t = humanmouse.Trajectory(Config.load(), mode="legacy")
    assert t.path_for(400, 300, start=(0, 0))[1] == 300.0


# ---------------------------------------------------------------- 状态回读
def test_status_reports_trajectory_from_engine():
    """契约 §5：生效/降级必须能从 engine.status()['stats'] 读到。"""
    from core.engine import Engine

    e = Engine()
    st = e.status()["stats"]["trajectory"]
    assert st["effective"] == "idle" and st["active"] is False
    assert "尚未开跑" in st["reason"]

    e._traj = humanmouse.Trajectory(_cfg("windmouse", 240, 0.5), mode="legacy")
    st = e.status()["stats"]["trajectory"]
    assert st["effective"] == "legacy" and st["active"] is True
    assert st["traj"] == "windmouse" and st["move_ms"] == 240

    e._traj = humanmouse.Trajectory(_cfg(), mode="message")
    st = e.status()["stats"]["trajectory"]
    assert st["effective"] == "degraded"
    assert "纯消息通道无真实移动" in st["reason"]


# ---------------------------------------------------------------- 开火精度
# 轨迹不能吃掉卡点：开火路径必须「预热完成 → 只做 down/up」。
# 下面全部用 __new__ + 假 user32 / 假驱动，不碰真实光标。
class _Stub32:
    def __init__(self):
        self.pos = (0, 0)

    def SetCursorPos(self, x, y):
        self.pos = (int(x), int(y))
        return 1


class _Drv:
    def __init__(self, stub):
        self.stub = stub
        self.moves = 0
        self.buttons = []
        self.track = []        # 每个绝对移动事件的落点（屏幕相对坐标）

    def mouse_device(self):
        return {"device": 9, "vid": 0x1234, "pid": 0x5678}

    def send_mouse(self, state, x=0, y=0, rolling=0, flags=0, device=None):
        if state == 0 and (flags & 0x0003):   # 绝对移动事件
            self.moves += 1
            self.stub.pos = (int(round(x * 1919 / 65535.0)),
                             int(round(y * 1079 / 65535.0)))
            self.track.append(self.stub.pos)
        else:
            self.buttons.append(state)


def _fire_mouse(monkeypatch, traj, mode="interception"):
    import core.interception as X
    stub = _Stub32()
    drv = _Drv(stub)
    m = object.__new__(inputmod.Mouse)
    m._mode = mode
    m._origin_left = 0
    m._origin_top = 0
    m._vx = 0
    m._vy = 0
    m._vw = 1919
    m._vh = 1079
    m._occ = []
    m._last_click = None
    m._last_path = None
    m._prepared = None
    m._traj = traj
    monkeypatch.setattr(inputmod, "_user32", stub)
    monkeypatch.setattr(inputmod, "_cursor_pos", lambda: stub.pos)
    monkeypatch.setattr(X, "shared", lambda: drv)
    monkeypatch.setattr(X, "delivery_watch", lambda: None)
    return m, drv, stub


def test_prewarm_uses_full_move_ms_not_fire_cap():
    """预热走完整 move_ms，且不写 _last_at（不把下一次点击误判成开火）。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.4), mode="legacy")
    pts, ms = t.prewarm_path(300, 200, start=(0, 0))
    assert len(pts) > 2 and pts[-1][:2] == [300.0, 200.0]
    assert ms == 120.0, "预热不该被 30ms 开火上限截断"
    assert t.last["preheat"] is True and t.last["fire"] is False
    assert t._last_at is None
    # 对照：同一目标在 20ms 内连点才算开火，才吃 5ms 上限
    t.path_for(300, 200, start=(0, 0), now=0.0)
    assert t.path_for(300, 200, start=(0, 0), now=0.02)[1] \
        == pytest.approx(min(120.0, 0.25 * 20.0, 30.0))


def test_preheat_budget_too_small_refuses_to_move(monkeypatch):
    """提前量装不下 move_ms：不硬走，标记瞬时落点（≤1ms 上界）。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.35), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    assert m.preheat(300, 200, budget_s=0.01) is False
    assert drv.moves == 0, "提前量不足时一次轨迹事件都不发"
    assert "提前量不足" in m._last_path["reason"]
    assert m._last_path["tail_jump"] is True
    assert m._last_path["tail_budget_ms"] == 1.0


def test_fire_after_preheat_is_zero_move_and_records_metrics(monkeypatch):
    """预热后开火：press 阶段不再走 move_path，press_ms 亚毫秒级。"""
    t = humanmouse.Trajectory(_cfg("bezier", 40, 0.35), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    assert m.preheat(300, 200, budget_s=0.2) is True
    assert m._prepared is not None
    assert stub.pos == (300, 200), "预热结束时光标必须已经停在按钮上"

    moved = []
    orig = m.move_path

    def counted(*a, **kw):
        moved.append(1)
        return orig(*a, **kw)

    m.move_path = counted
    for _ in range(12):
        time.sleep(0.004)                 # 模拟开火前的等待
        m.click(300, 200, verify=False, fire=True)

    assert len(moved) == 0, "开火点击不能再产生任何曲线位移/等待"
    st = t.status()["fire"]
    assert st["n"] == 12
    assert st["press_ms"]["p50"] < 2.0 and st["press_ms"]["p95"] < 2.0
    assert st["gap_ms"]["n"] == 12 and st["gap_ms"]["p50"] >= 4.0
    assert st["last"]["preheated"] is True
    # 开火只发了 target 上的 move + down/up，没有中间的轨迹点
    assert drv.moves >= 12


def test_fire_without_preheat_is_instant_tail_jump(monkeypatch):
    """目标晚到：不做曲线，只发一次瞬时落点，时间上界 1ms。"""
    t = humanmouse.Trajectory(_cfg("windmouse", 120, 0.4), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    m.click(300, 200, verify=False, fire=True)
    assert m._last_path["tail_jump"] is True
    assert m._last_path["tail_budget_ms"] == 1.0
    assert drv.moves == 1, "只有点击自带的那一次瞬时落点"
    assert t.status()["fire"]["last"]["jumped"] is True


def test_non_fire_click_still_uses_trajectory(monkeypatch):
    """非开火动作（导航/确认/列表点击）照常保留轨迹。"""
    t = humanmouse.Trajectory(_cfg("bezier", 40, 0.5), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    m.click(400, 300, verify=False)
    assert m._last_path["used"] is True and m._last_path["points"] > 2
    assert drv.moves > 2, "非开火点击必须真的走曲线"
    assert t.status()["fire"]["n"] == 0


def test_fire_stats_percentiles_are_nearest_rank():
    t = humanmouse.Trajectory(_cfg(), mode="legacy")
    for i in range(1, 21):
        t.record_fire(press_ms=float(i), gap_ms=float(i * 10),
                      preheated=(i % 2 == 0), jumped=(i % 2 == 1))
    st = t.fire_stats()
    assert st["n"] == 20
    assert st["press_ms"]["p50"] == 10.0
    assert st["press_ms"]["p95"] == 19.0
    assert st["press_ms"]["max"] == 20.0
    assert st["gap_ms"]["p50"] == 100.0
    assert st["tail_jump_budget_ms"] == 1.0


def test_segment_preheat_stops_short_then_tail_closes_gap(monkeypatch):
    """预算不足但位移小：分段预热停在按钮前，开火前用短移收尾，不是瞬移。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.35), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    stub.pos = (240, 200)                 # 离按钮 60px
    t0 = time.perf_counter()
    assert m.preheat(300, 200, budget_s=0.05) is True
    assert m._prepared["seg"] is True
    assert stub.pos[0] < 300, "预热点必须停在按钮前（安全区）"
    assert m.tail_reserve_s() > 0.0
    # 开火点击：用剩余 8~20ms 短移收尾，按下时刻仍落在计划时刻
    m.click(300, 200, verify=False, fire=True)
    st = t.status()["fire"]["last"]
    assert st["mode"] == "segment" and st["jumped"] is False
    assert st["lag_ms"] is not None and abs(st["lag_ms"]) < 2.0
    # 短移必须逐点走到按钮上（不是 SetCursorPos 一把跳过去）；点击收尾会
    # 把光标还原到开火前位置，所以看移动事件的落点轨迹，不看最终 stub.pos。
    assert max(p[0] for p in drv.track) >= 299, "短移最后一点要落在按钮上"
    assert time.perf_counter() - t0 < 0.12, "短移不能把开火拖成整条曲线"
    assert drv.moves >= 6, "预热 3 点 + 收尾短移 3 点"


def test_late_cursor_bump_uses_tail_not_teleport(monkeypatch):
    """预热命中后等待期光标被挪走 40px：用短移接回，不走瞬时落点。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.35), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    stub.pos = (300, 200)
    assert m.preheat(300, 200, budget_s=0.15) is True
    stub.pos = (260, 200)                  # 等待期被挪走 40px
    m.click(300, 200, verify=False, fire=True)
    st = t.status()["fire"]["last"]
    assert st["mode"] == "segment", "能短移补回就不该退回瞬移"
    assert st["lag_ms"] is not None and abs(st["lag_ms"]) < 2.0
    assert drv.moves >= 3, "短移要真的发出去，而不是 SetCursorPos 一把跳过去"
    assert max(p[0] for p in drv.track) >= 299, "短移最后一点要回到按钮上"


def test_fire_stats_count_degrade_first_and_retry():
    """stats 里必须能读出降级计数，且卡点第一击与连点分开。"""
    t = humanmouse.Trajectory(_cfg(), mode="legacy")
    t.record_fire(0.3, mode="zero", first=True, preheated=True)
    t.record_fire(0.3, mode="segment", first=True)
    t.record_fire(0.3, mode="straight", first=False, jumped=True)
    t.record_fire(0.3, mode="straight", first=False, jumped=True)
    d = t.fire_stats()["degrade"]
    assert d["all"]["n"] == 4
    assert d["all"]["zero"] == 1 and d["all"]["segment"] == 1
    assert d["all"]["straight"] == 2
    assert d["all"]["straight_rate"] == 0.5
    assert d["first"]["n"] == 2 and d["first"]["straight"] == 0
    assert d["retry"]["n"] == 2 and d["retry"]["straight"] == 2
    assert d["tail_window_ms"] == [8.0, 20.0]


def test_straight_fallback_is_logged_as_teleport_feature(monkeypatch):
    """真的塞不下短移时：文案必须写「瞬移特征」，并进 stats。"""
    t = humanmouse.Trajectory(_cfg("windmouse", 120, 0.4), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    stub.pos = (0, 0)
    assert m.preheat(900, 700, budget_s=0.02) is False
    assert "瞬移特征" in m._last_path["reason"]
    m.click(900, 700, verify=False, fire=True)
    last = t.status()["fire"]["last"]
    assert last["mode"] == "straight" and last["jumped"] is True
    assert t.status()["fire"]["degrade"]["all"]["straight"] == 1


def test_countdown_close_tail_moves_before_due_then_fire_is_zero(monkeypatch):
    """倒计时路线（预热无预算）：移位在 close_tail 里补回，开火仍零位移。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.35), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    stub.pos = (0, 0)
    assert m.preheat(300, 200) is True
    assert m.tail_reserve_s() > 0.0, "无预算也要留收尾窗口"
    stub.pos = (260, 200)                  # 列表刷新，按钮/光标错位 40px
    assert m.close_tail(300, 200) is True
    assert max(p[0] for p in drv.track) >= 299, "收尾短移要真的走到按钮"
    assert m._tail_s == 0.0, "收尾只做一次"
    m.click(300, 200, verify=False, fire=True)
    last = t.status()["fire"]["last"]
    assert last["mode"] == "zero", "收尾已经补回，开火不能再动"


def test_countdown_without_close_tail_never_moves_at_fire(monkeypatch):
    """没赶上 close_tail：开火瞬间绝不补短移（会拖后按下），按瞬移记账。"""
    t = humanmouse.Trajectory(_cfg("bezier", 120, 0.35), mode="interception")
    m, drv, stub = _fire_mouse(monkeypatch, t)
    stub.pos = (0, 0)
    assert m.preheat(300, 200) is True
    stub.pos = (260, 200)
    before = drv.moves
    m.click(300, 200, verify=False, fire=True)
    last = t.status()["fire"]["last"]
    assert last["mode"] == "straight" and last["jumped"] is True
    # 退化必须是「单点瞬移」：只有一次落点事件，不能出现 3 点的短移曲线
    # （短移塞在开火前会把按下时刻拖后 8~20ms）。
    added = drv.track[before:]
    assert len(added) == 1 and added[0][0] >= 299, "开火阶段不能补短移"
    assert t.status()["fire"]["degrade"]["all"]["straight"] == 1


