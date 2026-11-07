"""区域框选：GET/PUT /api/regions 的行为契约。

守的是一条链路：
    界面拖出来的归一化框 → PUT /api/regions → config.json（原子替换+备份）
    → 引擎下次 Config.load() 读到的就是新值 → Screen.norm_to_px 算出的
    像素矩形与接口返回的 px 完全一致。

另外守两类边界：
  · 越界 / 倒置 / 过小 / 未知键 / NaN / Inf / 字符串 —— 全部 400 且磁盘
    一个字节都不动（不许「校验一半先写一半」）；
  · 老配置没有 regions 字段 —— 按默认值工作，不报错；
  · 125% / 150% / 200% 缩放 —— 物理像素口径与引擎一致（沿用 project 唯一
    的 Screen.norm_to_px，不另起一套换算）。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
from core import regions as regions_mod  # noqa: E402
from core.region_table import BUILTIN  # noqa: E402
from core.screen import Screen  # noqa: E402
from core.server import Handler, ThreadingHTTPServer  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.5)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


class FakeScreen:
    """假屏幕：够 regions 用的字段 + 一张「深底 + 亮按钮条」的假画面。

    画面不能全黑：PUT buy 时会做「按钮区非空白」校验
    （`autocalib.check_buy_region`），全黑会被正确拒收。这里画一块
    比背景亮的横条，模拟真实挂单页的购买按钮。
    """

    def __init__(self, size=(1920, 1080), dpi=96):
        self.size = size
        self.dpi = dpi
        self.scale = round(dpi / 96.0, 3)
        self.awareness = 2
        self.index = 1
        self.bounds = {"left": 0, "top": 0, "width": size[0], "height": size[1]}
        self.game_rect = {"left": 0, "top": 0, "right": size[0], "bottom": size[1]}
        self.pick_reason = "测试假屏幕"

    def grab(self, box=None):
        img = np.full((self.size[1], self.size[0], 3), 32, dtype=np.uint8)
        img[300:600, 300:1000] = 200
        return img

    def close(self):
        pass


@pytest.fixture
def fake_screen(monkeypatch):
    sc = FakeScreen()
    monkeypatch.setattr(regions_mod, "open_screen", lambda cfg: sc)
    return sc


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "regions": {
            "buy": {"x": 0.7, "y": 0.8, "w": 0.1, "h": 0.06},
        },
        "queue": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    return path


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _put(base, path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


def _by_name(data, name):
    return next(r for r in data["regions"] if r["name"] == name)


# ---------------------------------------------------------------- GET

def test_get_reports_every_region_with_source_and_pixels(server, cfg_file, fake_screen):
    code, data = _get(server, "/api/regions")
    assert code == 200 and data["ok"], data
    names = [r["name"] for r in data["regions"]]
    for k in BUILTIN:
        assert k in names, f"GET 少了 {k}"
    assert len(names) == 9, f"应该正好 9 个区域，实际 {names}"

    buy = _by_name(data, "buy")
    assert buy["source"] == "user" and buy["editable"] is True
    assert buy["norm"] == {"x": 0.7, "y": 0.8, "w": 0.1, "h": 0.06}
    # 1920×1080：0.7→1344、0.8→864、0.1→192、0.06→65（round）
    assert buy["px"] == {"x": 1344, "y": 864, "w": 192, "h": 65}

    clock = _by_name(data, "clock")
    assert clock["source"] == "builtin"
    assert clock["norm"] == dict(BUILTIN["clock"])

    # 内置表里 9 个区域都有坐标：不会再出现「没设定 / px 为 None」
    for row in data["regions"]:
        assert row["px"] and row["px"]["w"] >= 1 and row["px"]["h"] >= 1, row
        assert row["source"] in ("builtin", "user", "invalid")
        assert row["editable"] is True, row

    conf = _by_name(data, "confirm")
    assert conf["px"]["w"] > 0 and conf["px"]["h"] > 0


def test_get_survives_missing_screen(server, cfg_file, monkeypatch):
    def boom(cfg):
        raise RuntimeError("no display")
    monkeypatch.setattr(regions_mod, "open_screen", boom)
    code, data = _get(server, "/api/regions")
    assert code == 200 and data["screen"]["available"] is False
    assert "no display" in data["screen"]["error"]


# ---------------------------------------------------------------- PUT 正常路径

def test_put_persists_and_engine_reads_the_new_box(server, cfg_file, fake_screen):
    before = cfg_file.read_bytes()
    payload = {"regions": {"buy": {"x": 0.2, "y": 0.3, "w": 0.25, "h": 0.2}}}
    code, data = _put(server, "/api/regions", payload)
    assert code == 200 and data["ok"], data
    assert data["saved"] == ["buy"]
    # 买框落了盘，就一定要附上「按钮区非空白」的校验结论
    assert data["buy_check"]["ok"] is True, data["buy_check"]
    assert "按钮区校验" in data["msg"]

    # 1) 响应里带「生效后的实际像素矩形」
    buy = _by_name(data, "buy")
    assert buy["source"] == "user"
    assert buy["px"] == {"x": 384, "y": 324, "w": 480, "h": 216}

    # 2) 磁盘真的变了，且新值是合法 JSON
    after = cfg_file.read_bytes()
    assert after != before
    disk = json.loads(after.decode("utf-8"))
    assert disk["regions"]["buy"] == {"x": 0.2, "y": 0.3, "w": 0.25, "h": 0.2}

    # 3) 引擎走的那条路：Config.load() → region() → norm_to_px()
    cfg = cfgmod.Config.load()
    box = cfg.region("buy")
    assert box == {"x": 0.2, "y": 0.3, "w": 0.25, "h": 0.2}
    px = Screen.norm_to_px(fake_screen, box)
    assert {"x": px[0], "y": px[1], "w": px[2], "h": px[3]} == buy["px"]


def test_put_accepts_x1y1x2y2_shape(server, cfg_file, fake_screen):
    code, data = _put(server, "/api/regions",
                      {"regions": {"clock": {"x1": 0.4, "y1": 0.05,
                                             "x2": 0.55, "y2": 0.1}}})
    assert code == 200, data
    row = _by_name(data, "clock")
    assert row["norm"] == {"x": 0.4, "y": 0.05, "w": 0.15, "h": 0.05}


def test_put_null_restores_the_builtin_default(server, cfg_file, fake_screen):
    code, data = _put(server, "/api/regions", {"regions": {"list": None}})
    assert code == 200, data
    row = _by_name(data, "list")
    assert row["source"] == "builtin"
    assert row["norm"] == dict(BUILTIN["list"])
    # 盘上写的是内置坐标本身，不是 null —— 下次启动就是确定的值
    assert cfgmod.Config.load().region("list") == dict(BUILTIN["list"])


# ---------------------------------------------------------------- PUT 拒收

@pytest.mark.parametrize("payload, why", [
    ({"regions": {"buy": {"x": -0.01, "y": 0.2, "w": 0.2, "h": 0.2}}}, "左上角越界"),
    ({"regions": {"buy": {"x": 0.9, "y": 0.2, "w": 0.2, "h": 0.2}}}, "右边越界"),
    ({"regions": {"buy": {"x": 0.1, "y": 0.95, "w": 0.2, "h": 0.2}}}, "下边越界"),
    ({"regions": {"buy": {"x1": 0.6, "y1": 0.2, "x2": 0.4, "y2": 0.4}}}, "左右颠倒"),
    ({"regions": {"buy": {"x1": 0.2, "y1": 0.6, "x2": 0.4, "y2": 0.4}}}, "上下颠倒"),
    ({"regions": {"buy": {"x": 0.2, "y": 0.2, "w": 0.001, "h": 0.2}}}, "太窄"),
    ({"regions": {"buy": {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.001}}}, "太矮"),
    ({"regions": {"nope": {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}}}, "未知区域"),
    ({"regions": {"list_refresh": {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2}}}, "旧键已废弃"),
    ({"regions": {"buy": {"x": "0.2", "y": 0.2, "w": 0.2, "h": 0.2}}}, "有限数字"),
    ({"regions": {"buy": {"x": True, "y": 0.2, "w": 0.2, "h": 0.2}}}, "有限数字"),
    ({"regions": {"buy": {"x": 0.2, "y": 0.2, "w": 0.2}}}, "有限数字"),
    ({"regions": {}}, "没有 regions"),
    ({}, "没有 regions"),
])
def test_put_rejects_bad_boxes_without_touching_disk(server, cfg_file,
                                                     fake_screen, payload, why):
    before = cfg_file.read_bytes()
    bak = str(cfg_file) + ".good"
    had_bak = os.path.exists(bak)
    code, data = _put(server, "/api/regions", payload)
    assert code == 400, f"{why} 应该拒收，却返回 {code}: {data}"
    assert data["ok"] is False and data["msg"], data
    assert cfg_file.read_bytes() == before, f"{why}：非法请求改了磁盘"
    assert os.path.exists(bak) == had_bak, f"{why}：非法请求产生了备份文件"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_put_rejects_non_finite_numbers(server, cfg_file, fake_screen, bad):
    before = cfg_file.read_bytes()
    # json.dumps 会写出 NaN/Infinity，服务端 json.loads 也认；必须在校验层挡住
    code, data = _put(server, "/api/regions",
                      {"regions": {"buy": {"x": bad, "y": 0.2, "w": 0.2, "h": 0.2}}})
    assert code == 400, data
    assert cfg_file.read_bytes() == before


def test_put_is_all_or_nothing(server, cfg_file, fake_screen):
    """一项合法一项非法 → 整单拒绝，合法那项也不许落盘。"""
    before = cfg_file.read_bytes()
    code, data = _put(server, "/api/regions", {"regions": {
        "buy": {"x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2},
        "clock": {"x": 0.9, "y": 0.9, "w": 0.2, "h": 0.2},
    }})
    assert code == 400, data
    assert cfg_file.read_bytes() == before


# ---------------------------------------------------------------- 兼容性

def test_old_config_without_regions_uses_defaults(tmp_path, monkeypatch, fake_screen):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"queue": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    cfg = cfgmod.Config.load()
    assert cfg.region("list") == dict(BUILTIN["list"])   # 老配置自动补内置值
    out = regions_mod.describe(cfg, (1920, 1080))
    assert _by_name(out, "list")["source"] == "builtin"
    assert _by_name(out, "buy")["source"] == "builtin"
    assert _by_name(out, "toast")["px"]["w"] > 0


def test_old_config_with_null_regions_is_migrated(tmp_path, monkeypatch, fake_screen):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"regions": {"buy": None, "list": None}},
                               ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    cfg = cfgmod.Config.load()
    assert cfg.region("buy") == dict(BUILTIN["buy"])
    assert set(cfg.data["regions"]) == set(BUILTIN)


def test_dpi_scales_map_to_the_same_physical_pixels():
    """125% / 150% / 200%：归一化不变，像素随物理分辨率线性走。

    per-monitor aware 下接口返回的 size 就是物理像素，与引擎截屏口径一致；
    这里用同一套 Screen.norm_to_px 验证三档，防止有人另写一套换算。
    """
    box = {"x": 0.25, "y": 0.5, "w": 0.125, "h": 0.25}
    cases = [((1920, 1080), 125), ((2304, 1296), 150), ((3072, 1728), 200)]
    for size, pct in cases:
        sc = FakeScreen(size=size, dpi=int(96 * pct / 100))
        expected = Screen.norm_to_px(sc, box)
        got = regions_mod.pixel_rect(box, size)
        assert (got["x"], got["y"], got["w"], got["h"]) == expected, \
            f"{pct}% ({size[0]}×{size[1]}) 换算与 Screen.norm_to_px 不一致"
        assert sc.scale == pytest.approx(pct / 100)


# ---------------------------------------------------------------- 抓帧

def test_capture_returns_png_from_the_engine_screen(server, cfg_file, fake_screen):
    with urllib.request.urlopen(server + "/api/screen/capture", timeout=15) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type", "").startswith("image/png")
        blob = r.read()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(blob) > 50


def test_capture_reports_readable_error_without_screen(server, cfg_file, monkeypatch):
    def boom(cfg):
        raise RuntimeError("no display")
    monkeypatch.setattr(regions_mod, "open_screen", boom)
    try:
        urllib.request.urlopen(server + "/api/screen/capture", timeout=15)
        pytest.fail("没有屏幕时抓帧不该成功")
    except urllib.error.HTTPError as e:
        assert e.code == 503
        body = json.loads(e.read().decode("utf-8"))
        assert body["ok"] is False and "no display" in body["msg"]
