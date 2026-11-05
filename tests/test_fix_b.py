"""视觉 / 坐标 / OCR 这一组文件的三轮审查修复回归测试。

全部离线：只用 `_shots` 里已有的截图做纯解析/坐标换算，OCR 一律 mock
或注入，不启动游戏、不抓屏、不联网、不起 OCR 服务进程。

覆盖的问题 ID 见每个用例的 docstring。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import nav, nav_ons, ocr, ocr_service, screen as screen_mod  # noqa: E402
from core.nav_coords import NavError  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHOTS = os.path.join(_ROOT, "_shots")


def _shot(name):
    import cv2
    path = os.path.join(_SHOTS, name)
    img = cv2.imread(path)
    assert img is not None, path
    return img


class _Mouse:
    """记录点击的真实像素坐标，不做任何系统调用。"""

    def __init__(self):
        self.calls = []
        self._game_hwnd = None

    def click(self, x, y, verify=True, expect_foreground=None):
        self.calls.append((int(x), int(y)))
        return True


def _nav(size=(1920, 1080), img=None, words=None):
    """造一个离线的 Navigator：只保留坐标换算/点击护栏这些纯逻辑。"""
    n = nav.Navigator.__new__(nav.Navigator)
    n.size = size
    n.mouse = _Mouse()
    n._log = lambda msg, level="info": None
    shot = img if img is not None else np.zeros((size[1], size[0], 3), np.uint8)
    n._snap = lambda: shot
    n._ocr_read = (lambda im: list(words or [])) if words is not None else None
    n._ocr_injected = words is not None
    n._ons_wheel = lambda notches, at=None: True
    return n


# ---------------- V02 坐标口径 ------------------------

def test_px_box_scales_roi_with_real_resolution():
    """V02：ROI 常量是 1080p 基准坐标，必须按实际分辨率等比换算。"""
    n = _nav((1920, 1080))
    assert n.px_box((470, 915, 540, 60)) == (470, 915, 540, 60)

    n2 = _nav((2560, 1440))
    x, y, w, h = n2.px_box((470, 915, 540, 60))
    assert (x, y) == (627, 1220), (x, y)
    assert h == 80 and x + w == 1347, (w, h)


def test_crop_box_on_a_real_screenshot():
    """V02：裁剪框跟着分辨率走，不能拿 1080p 的数字去切 720p 的图。"""
    img = _shot("drv_onsale.png")
    assert img.shape[:2] == (1080, 1920)

    n = _nav((1920, 1080), img)
    crop = n.crop_box(img, (470, 915, 540, 60))
    assert crop.shape == (60, 540, 3), crop.shape

    # 同一块 ROI 在 1280x720 下要按 2/3 缩，而不是越界切出空图
    small = img[:720, :1280]
    n2 = _nav((1280, 720), small)
    crop2 = n2.crop_box(small, (470, 915, 540, 60))
    assert crop2.shape == (40, 360, 3), crop2.shape


def test_click_px_does_not_rescale_real_pixels():
    """V02：OCR/模板返回的已是真实像素，click_px 不能再乘一次缩放。"""
    n = _nav((2560, 1440))
    n.click_px((800, 400), "x", expect_change=False)
    assert n.mouse.calls[-1] == (800, 400), n.mouse.calls

    # 基准坐标才做换算（2560/1920 = 1440/1080 = 1.333）
    n.click((600, 300), "y", expect_change=False)
    assert n.mouse.calls[-1] == (800, 400), n.mouse.calls


def test_diff_never_raises_on_broken_frames():
    """V19：抓帧失败（None）或尺寸变化都当成「变了」，不能抛异常。"""
    assert nav.NavBase._diff(None, np.zeros((10, 10, 3), np.uint8)) == 1.0
    assert nav.NavBase._diff(np.zeros((10, 10, 3), np.uint8), None) == 1.0
    assert nav.NavBase._diff(np.zeros((10, 10, 3), np.uint8),
                             np.zeros((20, 20, 3), np.uint8)) == 1.0
    a = np.zeros((10, 10, 3), np.uint8)
    assert nav.NavBase._diff(a, a.copy()) == 0.0


# ---------------- V04 像素判据 ----------------

def test_tab_from_pixels_scans_a_vertical_band():
    """V04：判据只有 1~2px 厚，必须在 y±band 内逐行扫，不能死盯 y=53。"""
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[59:62, 250:390] = (133, 177, 86)      # 真机选中框的 BGR
    assert nav.tab_from_pixels(img, y=53, band=8) == "collection"
    assert nav.tab_from_pixels(img, y=53, band=2) is None


def test_tab_from_pixels_on_real_screenshot():
    """V04：真机截图不崩，返回值只能是三个标签之一或 None。"""
    img = _shot("drv_onsale.png")
    got = nav.tab_from_pixels(img)
    assert got in (None, "mandel", "collection", "charm")


# ---------------- V07 / V08 置信度与显式失败 ----------------

def test_read_page_no_drops_low_confidence_words(monkeypatch):
    """V07：页码是「在售/详情」的唯一判据，低分碎块不能参与拼串。"""
    monkeypatch.setattr(ocr, "read_words", lambda img, **kw: [
        {"text": "第3", "score": 0.92},
        {"text": "页", "score": 0.61},
        {"text": "xFF", "score": 0.21},        # 噪声，必须丢
    ])
    n = _nav()
    n._ocr_injected = False
    assert n._read_page_no(np.zeros((1080, 1920, 3), np.uint8)) == "第3页"


def test_read_page_no_raises_when_ocr_is_down(monkeypatch):
    """V08：OCR 挂了要抛 NavError，不能返回空串冒充「没有页码」。"""
    def boom(img, **kw):
        raise RuntimeError("服务不可用")

    monkeypatch.setattr(ocr, "read_words", boom)
    n = _nav()
    n._ocr_injected = False
    with pytest.raises(NavError):
        n._read_page_no(np.zeros((1080, 1920, 3), np.uint8))


def test_page_anchor_requires_an_actual_number():
    """V06：锚点「第N页」必须带数字，否则任意文本都能把页面判成在售页。"""
    assert nav.classify("第1页") == "onsale"
    assert nav.classify("第 12 页") == "onsale"
    assert nav.classify("第页") != "onsale"
    assert nav.classify("") == "unknown"


# ---------------- V14 / V15 文本拼装 ----------------

def test_similarity_does_not_give_half_credit_for_a_short_substring():
    """V14：读到半个名字不能装成 0.75 分（0.72 阈值会被蒙过去）。"""
    assert ocr.similarity("攻势", "棱镜攻势") <= 0.5
    assert ocr.similarity("棱镜攻势", "棱镜攻势") == 1.0
    assert ocr.similarity("电玩高手", "电玩高手 传说") > 0.5


def test_phrases_joins_by_the_rightmost_word():
    """V15：间隙要比「组内最右词」，不能比「最后 append 的词」。"""
    words = [
        {"x": 0, "y": 100, "w": 40, "h": 16, "text": "电玩", "score": 0.9},
        {"x": 44, "y": 102, "w": 40, "h": 16, "text": "高手", "score": 0.9},
        {"x": 300, "y": 101, "w": 60, "h": 16, "text": "传说", "score": 0.9},
    ]
    got = ocr.phrases(words, gap=12)
    texts = [p["text"] for p in got]
    assert "电玩高手" in texts, texts
    assert "电玩高手传说" not in texts, texts


# ---------------- V01 / V03 选项区块 ----------------

class _ClearHost:
    """只要有 mouse.click / ons_options / say 的轻量宿主。"""

    def __init__(self, after, later):
        self._after, self._later = after, later
        self.clicks = []
        self.said = []
        self.mouse = self

    def click(self, x, y, verify=False):
        self.clicks.append((int(x), int(y)))
        return True

    def ons_options(self, scroll_top=True):
        return self._later if self.clicks else self._after

    def say(self, msg, level="info"):
        self.said.append(msg)


def _item(name, on, y=340, block=None):
    return {"name": name, "raw": name, "on": on, "block": block, "row": 0,
            "col": 0, "text_pt": (1396, y), "box_pt": (1382, y - 14),
            "src": "ocr"}


def test_clear_via_all_finds_the_block_by_name_when_anchors_are_missing():
    """V01/V03：区块名认不出时按选项名反查区块，别直接放弃退路。"""
    after = [_item("所有稀有度", False), _item("优品", False, 396),
             _item("极品", True)]
    host = _ClearHost(after, [_item("所有稀有度", True)])
    o = _item("极品", True, block=None)
    assert nav_ons.NavOnsPanel._ons_clear_via_all(host, o, after) is True
    assert host.clicks == [(1396, 340)], host.clicks


def test_clear_via_all_refuses_when_a_sibling_is_still_on():
    """V01：还有别的项勾着就不是「最后一项」问题，不许静默改筛选条件。"""
    after = [_item("所有稀有度", False), _item("优品", True, 396),
             _item("极品", True)]
    host = _ClearHost(after, [_item("所有稀有度", True)])
    o = _item("极品", True, block=None)
    assert nav_ons.NavOnsPanel._ons_clear_via_all(host, o, after) is False
    assert host.clicks == []


def test_clear_via_all_refuses_an_ambiguous_name():
    """V01：名字同时出现在多个区块（「不限」）时不猜，明确放弃。"""
    after = [_item("不限", True), _item("速度线", False, 745)]
    host = _ClearHost(after, [])
    o = _item("不限", True, block=None)
    assert nav_ons.NavOnsPanel._ons_clear_via_all(host, o, after) is False
    assert host.clicks == []


class _Panel:
    """一张合成面板：一个复选框 + 一个名字，用来验证分数闸门。"""

    def __init__(self, name, score, cy=340):
        self.name, self.score, self.cy = name, score, cy
        self.mouse = self
        self.said = []

    def click(self, x, y, verify=True):
        return True

    def say(self, msg, level="info"):
        self.said.append(msg)

    def _snap(self):
        img = np.zeros((1080, 1920, 3), np.uint8)
        img[self.cy - 14:self.cy + 15, 1382:1382 + 29] = 100
        img[self.cy - 12:self.cy + 13, 1384:1384 + 25] = 30
        return img

    def _ocr_words(self, crop):
        return [{"x": 1396 + 40 - 1360, "y": self.cy - 30 - 8, "w": 30, "h": 16,
                 "text": self.name, "score": self.score}]


def _panel_nav(panel):
    n = _nav()
    n._snap = panel._snap
    n._ocr_words = panel._ocr_words
    n.mouse = panel.mouse
    n.say = panel.say
    n._ocr_injected = True
    return n


def test_ons_options_treats_low_confidence_text_as_absent():
    """V07：0.2 分的猜测不能当选项名用 —— 名字像就点下去是买错货。"""
    low = _panel_nav(_Panel("极品", 0.2))
    names_low = [o["name"] for o in low.ons_options(scroll_top=False)]
    assert "极品" not in names_low, names_low

    high = _panel_nav(_Panel("极品", 0.95))
    names_high = [o["name"] for o in high.ons_options(scroll_top=False)]
    assert "极品" in names_high, names_high


# ---------------- V05 显示器选择 ----------------

def test_pick_monitor_respects_the_requested_index():
    """V05：配置写 2 号屏就必须抓 2 号屏；做不到要明确报错，不能回落。"""
    mon = [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},   # 0 虚拟屏
        {"left": 0, "top": 0, "width": 1345, "height": 960},     # 1 幽灵屏
        {"left": 0, "top": 0, "width": 2560, "height": 1440},    # 2 真屏
    ]
    s = screen_mod.Screen.__new__(screen_mod.Screen)
    s.monitors = mon
    assert s._pick_monitor(0) == 0
    assert s._pick_monitor(2) == 2

    with pytest.raises(ValueError):
        s._pick_monitor(3)

    broken = list(mon)
    broken[2] = {"left": 0, "top": 0, "width": 0, "height": 0}
    s.monitors = broken
    with pytest.raises(ValueError):
        s._pick_monitor(2)


# ---------------- V09 / V10 / V11 / V22 OCR 管家 ----------------

class _Proc:
    def __init__(self, port):
        self.port = port
        self.pid = 10000 + port

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None


def _supervisor(**kw):
    cfg = {"port": 8081, "alt_port": 0, "healthy_timeout_s": 0.2,
           "warmup_s": 0, "idle_wait_s": 0, "interval_s": 60}
    cfg.update(kw.pop("settings", {}))
    return ocr_service.Supervisor(settings=cfg, log=lambda m: None, **kw)


def test_tick_probes_real_recognition_not_just_tcp(monkeypatch):
    """V09：端口通了但推理不通（别的程序占端口/模型没加载完）不算健康。"""
    monkeypatch.setattr(ocr_service, "_tcp_ok",
                        lambda host, port, timeout=1.5: True)
    monkeypatch.setattr(ocr, "_settings",
                        {"ppocr": {"url": "http://127.0.0.1:8081"}},
                        raising=False)
    svc = _supervisor(probe=lambda url: False, spawn=lambda port: _Proc(port),
                      kill=lambda p: None, kill_port=lambda port: None)
    assert svc.tick() is False, "推理不通必须报不健康并触发重启"
    assert svc.status()["healthy"] is False


def test_wait_idle_reports_failure_when_the_supervisor_is_stopping():
    """V11：停止不算「等到了空闲」，否则关机路上还会拉起新进程。"""
    svc = _supervisor(is_busy=lambda: True, settings={"idle_wait_s": 30})
    svc._stop.set()
    assert svc._wait_idle(0.5) is False


def test_alt_port_comes_from_config():
    """V22：备用端口可配；没配才退回 8082/8081 的老规矩。"""
    assert _supervisor()._alt_port(8081) == 8082
    assert _supervisor()._alt_port(8082) == 8081
    svc = _supervisor(settings={"alt_port": 9000})
    assert svc._alt_port(8081) == 9000
    # 配成和当前端口一样就等于没配，不能自己跟自己蓝绿
    same = _supervisor(settings={"alt_port": 8081})
    assert same._alt_port(8081) == 8082
