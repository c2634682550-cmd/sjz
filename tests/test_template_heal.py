"""模板自愈：真机那个「模板 315×67 比筛选区域 298×58 还大」的红叉不该出现。

真机现场（1920×1080 / 96DPI / 100%）三处叠加导致误报：
  · 自检拿**未外扩**的框选区域比原始模板尺寸，引擎其实在 387×75 的
    搜索窗里匹配 315×67，放得下；
  · 倒计时路线根本不用 buy 模板，却被一起判；
  · 内置 buy 区域框的是按钮本体 298×58，随包模板带了一圈边 315×67。

这个用例集钉住三件事：
  1. 几何口径必须是引擎的搜索窗（buy 0.15、confirm 0.25）；
  2. 倒计时路线不因为 buy 模板误报；
  3. 兜底重采只在「详情页 + 分数 0.55..阈值 + 位置居中」时发生，写盘前
     有快照、能回滚；测试环境默认不写盘（conftest 已设
     DAFORS_TEMPLATE_WRITE=0）。
"""
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
from core import preflight  # noqa: E402
from core import template_heal  # noqa: E402
from core import vision  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(BASE, "templates")

REGION_BUY = {"x": 1482.0 / 1920, "y": 903.0 / 1080,
              "w": 298.0 / 1920, "h": 58.0 / 1080}
REGION_CONFIRM = {"x": 1164.0 / 1920, "y": 690.0 / 1080,
                  "w": 303.0 / 1920, "h": 70.0 / 1080}


class FakeScreen:
    """只实现自愈要用的几个口子；不碰 mss，不开窗。"""

    def __init__(self, size=(1920, 1080), dpi=96, frame=None, game=True):
        self.size = size
        self.dpi = dpi
        self.scale = dpi / 96.0
        self.awareness = 2
        self.client_rect = (0, 0, size[0], size[1]) if game else None
        self.frame = frame
        vision.set_target_screen(size, dpi)

    def norm_to_px(self, box):
        w, h = self.size
        return (int(round(box["x"] * w)), int(round(box["y"] * h)),
                int(round(box["w"] * w)), int(round(box["h"] * h)))

    def grab(self):
        return self.frame

    def close(self):
        pass


class FakeCfg:
    def __init__(self, tdir, regions=None, workflow="detail", threshold=0.85):
        self.tdir = str(tdir)
        self.regions = regions if regions is not None else {
            "buy": REGION_BUY, "confirm": REGION_CONFIRM}
        self.workflow = workflow
        self.threshold = threshold

    def get(self, key, default=None):
        if key == "threshold":
            return self.threshold
        if key == "run":
            return {"workflow": self.workflow}
        if key == "workflow":
            return self.workflow
        return default

    def region(self, name):
        return self.regions.get(name)

    def template_path(self, name):
        return os.path.join(self.tdir, name + ".png")

    def has_template(self, name):
        return os.path.isfile(self.template_path(name))


@pytest.fixture()
def writable(monkeypatch):
    """conftest 默认关掉模板写盘；需要验证写盘的用例显式打开。"""
    monkeypatch.setenv("DAFORS_TEMPLATE_WRITE", "1")


@pytest.fixture()
def tdir(tmp_path, monkeypatch):
    """临时 templates 目录：拷真模板过去，仓库文件保持只读。"""
    d = tmp_path / "templates"
    d.mkdir()
    for name in ("buy.png", "confirm.png", "meta.json"):
        with open(os.path.join(TPL, name), "rb") as src:
            (d / name).write_bytes(src.read())
    monkeypatch.setattr(cfgmod, "TEMPLATE_DIR", str(d))
    monkeypatch.setattr(cfgmod, "user_dir", lambda: str(tmp_path / "user"))
    return d


def _flat_frame(w=1920, h=1080, value=32):
    return np.full((h, w, 3), value, dtype=np.uint8)


def _noise_frame(seed, w=1920, h=1080):
    return np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)


# ---------------------------------------------------------- 几何口径 / 误报


def test_geometry_uses_engine_search_window():
    """315×67 塞得进 298×58 外扩 15% 的 387×75 —— 不该判红。"""
    facts = {"size": (1920, 1080),
             "template_sizes": {"buy": (315, 67)},
             "template_scales": {"buy": [1.0]},
             "template_capture_screen": (1920, 1080),
             "region_px": {"buy": (298, 58)}}
    assert preflight._template_scale_issue(facts, "list") is None


def test_legacy_template_still_blocks():
    """没有元数据的老模板走保守口径：外扩后还放不下就得拦。"""
    facts = {"size": (1920, 1080),
             "template_sizes": {"buy": (400, 200)},
             "region_px": {"buy": (300, 120)}}
    issue = preflight._template_scale_issue(facts, "list")
    assert issue and "比框选区域" in issue


def test_countdown_does_not_judge_buy():
    """倒计时只看 confirm：buy 再大也不该把倒计时路线拦下来。"""
    facts = {"size": (1920, 1080),
             "template_sizes": {"buy": (400, 200), "confirm": (303, 70)},
             "template_scales": {"buy": [1.0], "confirm": [1.0]},
             "template_capture_screen": (1920, 1080),
             "region_px": {"buy": (298, 58), "confirm": (303, 70)}}
    assert preflight._template_scale_issue(facts, "countdown") is None
    assert preflight._template_scale_issue(facts, "list")


def test_heal_reports_fit_without_touching_files(tdir):
    """几何能对齐时只给结论，一个字节都不写。"""
    before = {p.name: p.read_bytes() for p in tdir.iterdir()}
    heal = template_heal.heal(FakeCfg(tdir), workflow="list",
                              screen=FakeScreen(frame=_flat_frame()))
    assert heal["status"] == "fit", heal
    assert heal["passed"] == ["buy"]
    assert not heal.get("backup")
    assert {p.name: p.read_bytes() for p in tdir.iterdir()} == before


def test_meta_for_prefers_per_template_capture(tdir):
    """单图条目优先：静默切换全目录环境会把没重采的那张算错。"""
    import json
    meta = json.loads((tdir / "meta.json").read_text(encoding="utf-8"))
    meta["templates"]["buy.png"]["capture_screen"] = [1280, 720]
    (tdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    img = vision.imread(str(tdir / "buy.png"), cv2.IMREAD_GRAYSCALE)
    assert vision.capture_screen_of(img) == (1280, 720)
    other = vision.imread(str(tdir / "confirm.png"), cv2.IMREAD_GRAYSCALE)
    assert vision.capture_screen_of(other) == (1920, 1080)


# ------------------------------------------------------------------ 等比补偿


def _put_template(tdir, name, w, h, seed=7):
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    tmp = str(tdir / (name + ".png"))
    vision.imwrite(tmp, img)
    return img


def test_rescale_writes_with_backup(tdir, writable):
    """候选比例都塞不下时按需要比例补偿，并留快照。"""
    _put_template(tdir, "buy", 400, 200)
    cfg = FakeCfg(tdir, regions={"buy": {"x": 0.2, "y": 0.2,
                                         "w": 300.0 / 1920, "h": 120.0 / 1080}})
    heal = template_heal.heal(cfg, workflow="list", screen=FakeScreen())
    assert heal["status"] == "rescaled", heal
    assert heal["templates"]["buy"]["state"] == "rescaled"
    assert heal["backup"] and os.path.isdir(heal["backup"])
    img = vision.imread(str(tdir / "buy.png"), cv2.IMREAD_GRAYSCALE)
    assert img.shape[1] <= 390 and img.shape[0] <= 156
    meta_text = (tdir / "meta.json").read_text(encoding="utf-8")
    assert "rescaled" in meta_text


def test_write_guard_blocks_rescale(tdir, monkeypatch):
    """测试环境（DAFORS_TEMPLATE_WRITE=0）不允许写盘：只报告，不改文件。"""
    _put_template(tdir, "buy", 400, 200)
    before = (tdir / "buy.png").read_bytes()
    monkeypatch.setenv("DAFORS_TEMPLATE_WRITE", "0")
    cfg = FakeCfg(tdir, regions={"buy": {"x": 0.2, "y": 0.2,
                                         "w": 300.0 / 1920, "h": 120.0 / 1080}})
    heal = template_heal.heal(cfg, workflow="list", screen=FakeScreen())
    assert heal["status"] == "failed"
    assert (tdir / "buy.png").read_bytes() == before


# -------------------------------------------------------------------- 重采


def _fake_match(expected, first=0.70, then=0.995):
    calls = {"n": 0}

    def _f(hay, needle, scales=None, deep=False):
        calls["n"] += 1
        return (first, expected, 1.0) if calls["n"] == 1 else (then, expected, 1.0)

    return _f


def _confirm_expected():
    rw, rh = 303, 70
    pad = 0.25
    return (int(rw * pad), int(rh * pad))


def test_recapture_requires_detail_page(tdir, monkeypatch):
    """分数落在 0.55..阈值之间，但不在详情页 —— 不重采。"""
    monkeypatch.setattr(template_heal.vision, "match_scaled",
                        _fake_match(_confirm_expected()))
    heal = template_heal.heal(FakeCfg(tdir), workflow="countdown", page="onsale",
                              screen=FakeScreen(frame=_noise_frame(1)))
    assert heal["status"] == "fit", heal
    assert "不是详情页" in (heal["templates"]["confirm"].get("actions") or [""])[0]


def test_recapture_on_detail_page_writes_and_backs_up(tdir, monkeypatch, writable):
    before = (tdir / "confirm.png").read_bytes()
    monkeypatch.setattr(template_heal.vision, "match_scaled",
                        _fake_match(_confirm_expected()))
    heal = template_heal.heal(FakeCfg(tdir), workflow="countdown", page="detail",
                              screen=FakeScreen(frame=_noise_frame(3)))
    assert heal["status"] == "recaptured", heal
    assert heal["backup"] and os.path.isdir(heal["backup"])
    assert (tdir / "confirm.png").read_bytes() != before
    meta_text = (tdir / "meta.json").read_text(encoding="utf-8")
    assert "recaptured" in meta_text
    # 回滚：最近快照把原图放回去
    assert "confirm.png" in template_heal.restore("confirm")
    assert (tdir / "confirm.png").read_bytes() == before


def test_recapture_rejected_when_location_off_center(tdir, monkeypatch):
    """分数够但位置飘到窗口边上：像的不是它，宁可不采。"""
    monkeypatch.setattr(template_heal.vision, "match_scaled",
                        _fake_match((0, 0)))
    before = (tdir / "confirm.png").read_bytes()
    heal = template_heal.heal(FakeCfg(tdir), workflow="countdown",
                              page="detail", screen=FakeScreen(frame=_noise_frame(2)))
    assert heal["status"] == "fit", heal
    assert "偏离" in heal["templates"]["confirm"].get("message", "")
    assert (tdir / "confirm.png").read_bytes() == before


def test_no_recapture_when_nothing_looks_like_it(tdir, monkeypatch):
    """分数低于 0.55：按钮不在画面里，硬裁只会把背景存成模板。"""
    monkeypatch.setattr(template_heal.vision, "match_scaled",
                        _fake_match(_confirm_expected(), first=0.30))
    before = (tdir / "confirm.png").read_bytes()
    heal = template_heal.heal(FakeCfg(tdir), workflow="countdown", page="detail",
                              screen=FakeScreen(frame=_noise_frame(4)))
    assert heal["status"] == "fit", heal
    assert (tdir / "confirm.png").read_bytes() == before
