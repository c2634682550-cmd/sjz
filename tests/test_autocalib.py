"""自动标定：内置坐标表、缩放换算、离线锚点自检。

不依赖游戏、不依赖屏幕、不依赖 OCR 安装：锚点用例用 `templates/` 里
现成的两张按钮图贴到纯色画布上，纯离线可复现。
"""
import json
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import autocalib as A  # noqa: E402
from core import config as cfgmod  # noqa: E402
from core import region_table  # noqa: E402
from core import regions  # noqa: E402
from core import vision  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = lambda name: os.path.join(ROOT, "templates", name + ".png")  # noqa: E731


class _Cfg:
    def __init__(self):
        self.data = {"regions": {}, "calibration": {}}
        self._dirty = set()

    def template_path(self, name):
        return TPL(name)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set_region(self, name, box):
        self.data.setdefault("regions", {})[name] = dict(box)

    def save(self):
        return True


def _frame_with_templates(offset=(0, 0), size=(1920, 1080), skip=()):
    """在任意客户区尺寸的纯色画布上，把按钮模板按同一比例贴到期望位置。

    模板必须用 `vision.scaled()` 生成：与匹配链路的重采样完全一致，
    才能构造出「锚点就在内置坐标上」的基准帧。
    """
    w, h = size
    frame = np.full((h, w, 3), 245, np.uint8)
    # 真实游戏是 16:9 UI 等比居中（黑边），不是几何均值拉伸。
    # 21:9（3440×1440）真实比例 1.3333，几何均值会给 1.5456。
    scale = min(w / 1920.0, h / 1080.0)
    boxes, _ = A._aspect_fix(A.boxes_for(size), size)
    for name in ("buy", "confirm"):
        if name in skip:
            continue
        tpl = cv2.imread(TPL(name))
        assert tpl is not None, f"缺模板 {name}.png"
        used = vision.scaled(tpl, scale)
        x, y, bw, bh = boxes[name]
        tx = int(round(x + bw / 2.0 - used.shape[1] / 2.0 + offset[0]))
        ty = int(round(y + bh / 2.0 - used.shape[0] / 2.0 + offset[1]))
        frame[ty:ty + used.shape[0], tx:tx + used.shape[1]] = used
    return frame


def _center(rect):
    x, y, w, h = rect
    return x + w / 2.0, y + h / 2.0


# ---------------------------------------------------------------- 内置坐标表

def test_builtin_table_has_exactly_the_nine_required_regions():
    assert len(region_table.ORDER) == 9
    assert set(region_table.BUILTIN) == set(region_table.ORDER)
    for name, box in region_table.BUILTIN.items():
        assert set(box) == {"x", "y", "w", "h"}
        assert 0 < box["w"] <= 1 and 0 < box["h"] <= 1
        assert box["x"] + box["w"] <= 1 + 1e-9
        assert box["y"] + box["h"] <= 1 + 1e-9


def test_1080p_100_matches_hand_measured_pixels_exactly():
    """1080p/100% 下必须与手工实测像素逐位一致（不是"差不多"）。"""
    got = A.boxes_for((1920, 1080))
    for name, expect in region_table.BUILTIN_PX.items():
        assert got[name] == expect, (name, got[name], expect)


def test_config_defaults_are_the_same_table():
    """默认配置里写的就是这张表，不是另一份拷贝。"""
    regions_cfg = cfgmod.DEFAULTS["regions"]
    assert set(regions_cfg) == set(region_table.BUILTIN)
    for name, box in region_table.BUILTIN.items():
        assert regions_cfg[name] == box, name


# ---------------------------------------------------------------- 确认弹窗现场标定

def test_confirm_is_deferred_at_startup_not_required():
    """启动阶段 confirm 不参与：记 deferred，闸门不因为它拦。

    确认弹窗只有点了购买才出现，启动时画面里没有它，拿它当启动锚点
    实测最高 0.223 < 0.55，必然 fallback。
    """
    frame = _frame_with_templates(skip=("confirm",))
    res = A.calibrate_image(frame, cfg=_Cfg(), workflow="list")
    assert res["status"] in ("ok", "adjusted"), res["message"]
    conf = next(a for a in res["anchors"] if a["name"] == "confirm")
    assert conf["method"] == "deferred"
    assert A.gate_reason(res, _Cfg()) is None


def test_calibrate_confirm_hits_dialog_and_caches_roi():
    """弹窗真出现时现场标定：命中写归一化 ROI + 分数，不是像素模板。"""
    cfg = _Cfg()
    got = A.calibrate_confirm(cfg, _frame_with_templates())
    assert got["ok"], got["msg"]
    assert got["score"] >= A.ABSENT_SCORE
    want = A.boxes_for((1920, 1080))["confirm"]
    assert abs(got["norm"]["x"] - want[0] / 1920.0) < 0.01
    assert abs(got["norm"]["y"] - want[1] / 1080.0) < 0.01
    assert cfg.data["regions"]["confirm"] == got["norm"]
    assert cfg.data["calibration"]["confirm_score"] >= A.ABSENT_SCORE
    assert "confirm_roi" in cfg.data["calibration"]


def test_calibrate_confirm_stays_off_when_no_dialog_on_screen():
    """画面里没弹窗就不许乱写：不命中 -> 配置一个字节都不动。"""
    cfg = _Cfg()
    blank = np.full((1080, 1920, 3), 40, np.uint8)
    got = A.calibrate_confirm(cfg, blank)
    assert not got["ok"]
    assert "confirm" not in cfg.data.get("regions", {})
    assert "confirm_roi" not in cfg.data.get("calibration", {})


def test_twelve_presets_cover_three_resolutions_times_four_scales():
    ps = A.presets()
    assert len(ps) == 12
    assert {(p["logical"][0], p["logical"][1], p["scale"]) for p in ps} == {
        (1920, 1080, 100), (1920, 1080, 125), (1920, 1080, 150), (1920, 1080, 200),
        (2560, 1440, 100), (2560, 1440, 125), (2560, 1440, 150), (2560, 1440, 200),
        (3840, 2160, 100), (3840, 2160, 125), (3840, 2160, 150), (3840, 2160, 200),
    }

    for p in ps:
        size = (p["width"], p["height"])
        boxes = A.boxes_for(size)
        for name, (bx, by, bw, bh) in region_table.BUILTIN_PX.items():
            sx, sy = size[0] / 1920.0, size[1] / 1080.0
            expect = (bx * sx, by * sy, bw * sx, bh * sy)
            gx, gy = _center(boxes[name])
            ex, ey = _center(expect)
            assert abs(gx - ex) <= 1.0 and abs(gy - ey) <= 1.0, (
                p["name"], name, (gx - ex, gy - ey))


def test_21x9_client_falls_back_to_exhaustive_scale():
    """P0-A：3440×1440 几何均值 1.5456 不等于真实 1.3333，必须穷举兜底。"""
    res = A.calibrate_image(_frame_with_templates(size=(3440, 1440)),
                            size=(3440, 1440), cfg=_Cfg())
    assert res["status"] in ("ok", "adjusted"), res["message"]
    buy = [a for a in res["anchors"] if a["name"] == "buy"][0]
    assert buy["status"] == "found" and buy["score"] > 0.8


def test_countdown_workflow_does_not_require_buy_anchor():
    """P0-D：倒计时模式没有 buy 模板也不能拦死。"""
    res = A.calibrate_image(_frame_with_templates(skip=("buy",)),
                            cfg=_Cfg(), workflow="countdown")
    assert res["status"] in ("adjusted", "ok"), res["message"]
    assert A.gate_reason(res) is None
    # 同一条画面在自由购买路线仍要求 buy → fallback
    res2 = A.calibrate_image(_frame_with_templates(skip=("buy",)),
                             cfg=_Cfg(), workflow="detail")
    assert res2["status"] == "fallback"
    assert "购买按钮" in res2["message"]


def test_manual_regions_lift_the_gate_for_skipped(tmp_path, monkeypatch):
    """P0-B/P1-F：手框一次就能启动；只框一半仍然拦。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"queue": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    cfg = cfgmod.Config.load()
    skipped = {"status": "skipped", "message": "按钮位置未经校对"}
    assert A.gate_reason(skipped, cfg)
    cfg.set_region("buy", {"x": 0.70, "y": 0.80, "w": 0.18, "h": 0.08})
    assert A.gate_reason(skipped, cfg)          # 只框了 buy 还不够
    cfg.set_region("confirm", {"x": 0.44, "y": 0.60, "w": 0.12, "h": 0.06})
    assert A.gate_reason(skipped, cfg) is None  # 两个必需区域都手框 → 放行


def test_letterbox_client_centers_the_16x9_frame():
    """4:3 客户区：坐标应等比缩小并居中，而不是横向拉满。"""
    boxes, meta = A._aspect_fix(A.boxes_for((1600, 1200)), (1600, 1200))
    assert meta["changed"] is True
    assert meta["scale"] == pytest.approx(1600 / 1920.0, rel=1e-3)
    x, y, w, h = boxes["buy"]
    # 4:3 下内容按 1600×900 居中：x 不压缩，y 压到 900/1080 再加 150 黑边
    assert x == pytest.approx(1482 * 1600 / 1920.0, abs=1)
    assert y == pytest.approx(903 * 900 / 1080.0 + 150, abs=1)


# ---------------------------------------------------------------- 离线锚点自检

def test_offline_anchors_pass_when_templates_are_where_expected():
    res = A.calibrate_image(_frame_with_templates(), cfg=_Cfg())
    assert res["status"] in ("ok", "skipped"), res["message"]
    assert res["status"] == "ok"          # 两个模板都找到了，不算 skipped
    found = {a["name"]: a for a in res["anchors"]}
    assert found["buy"]["status"] == "found" and abs(found["buy"]["score"]) > 0.9
    assert found["confirm"]["status"] == "found"
    assert abs(found["buy"]["offset"][0]) <= 1 and abs(found["buy"]["offset"][1]) <= 1
    assert res["boxes"]["buy"] == region_table.BUILTIN_PX["buy"]


def test_offline_anchors_nudge_then_pass():
    """整体偏 14px：应自动平移坐标并复验通过（status=adjusted）。"""
    res = A.calibrate_image(_frame_with_templates(offset=(14, 14)), cfg=_Cfg())
    assert res["status"] == "adjusted", res["message"]
    assert res["attempts"] >= 1
    x, y, w, h = res["boxes"]["buy"]
    assert x == pytest.approx(region_table.BUILTIN_PX["buy"][0] + 14, abs=1)
    assert y == pytest.approx(region_table.BUILTIN_PX["buy"][1] + 14, abs=1)


def test_offline_returns_skipped_when_no_anchor_visible():
    """游戏不在交易行页面：状态 skipped，但必须明说「未经校对」，闸门拦。"""
    res = A.calibrate_image(np.full((1080, 1920, 3), 245, np.uint8), cfg=_Cfg())
    assert res["status"] == "skipped"
    assert res["ok"] is False
    assert res["boxes"] == region_table.BUILTIN_PX
    assert "未经校对" in res["message"]
    assert A.gate_reason(res)                      # skipped 不是绿灯


def test_template_matching_has_no_scale_bias():
    """P0：模板中心必须用缩放后的尺寸，1280/2560/3840 都不能有系统性偏移。

    旧实现用原图 th/tw，偏差 = tpl_w*(1-r)/2；1280×720 下约 +53px，
    3840×2160 下约 −152px，并会被当成真实偏差写进坐标。
    """
    for size in ((1280, 720), (2560, 1440), (3840, 2160)):
        res = A.calibrate_image(_frame_with_templates(size=size), cfg=_Cfg())
        assert res["status"] == "ok", (size, res["status"], res["message"])
        assert abs(res["offset"][0]) <= 1 and abs(res["offset"][1]) <= 1, (size, res["offset"])
        boxes = res["boxes"]
        want = A.boxes_for(size)["buy"]
        assert abs(_center(boxes["buy"])[0] - _center(want)[0]) <= 1, size
        assert abs(_center(boxes["buy"])[1] - _center(want)[1]) <= 1, size


def test_missing_critical_anchor_forces_fallback_to_builtin():
    """P0：购买按钮被遮挡（只剩确认按钮）时不得判 ok，且回内置坐标。"""
    frame = _frame_with_templates(skip=("buy",))
    res = A.calibrate_image(frame, cfg=_Cfg())
    assert res["status"] == "fallback", res["message"]
    assert res["ok"] is False
    assert "购买按钮" in res["message"]
    assert res["boxes"] == region_table.BUILTIN_PX
    buy = {a["name"]: a for a in res["anchors"]}["buy"]
    assert buy["status"] == "absent"


def test_ocr_text_only_is_skipped_not_ok(monkeypatch):
    """P0：只有 OCR 文字、没有任何像素锚点时不得判 ok。"""
    class _Ocr:
        @staticmethod
        def available():
            return True

        @staticmethod
        def read_text(_crop):
            return "剩余 12 分 3456"

    monkeypatch.setattr(A._ocr, "available", _Ocr.available)
    monkeypatch.setattr(A._ocr, "read_text", _Ocr.read_text)
    res = A.calibrate_image(np.full((1080, 1920, 3), 245, np.uint8), cfg=_Cfg())
    assert res["status"] == "skipped", res["message"]
    assert res["ok"] is False                   # 没核对过点击位置，不能算通过
    assert "文字锚点" in res["message"]


def test_large_offset_is_not_trusted_and_returns_builtin():
    """P1：偏移上限按模板短边比例算，超了才回内置。

    旧口径写死 24px，30px 的整体平移会被误杀；现在上限是模板短边×1.5
    （购买模板 298×58 → 87px），30px 属于可信范围，必须放行。
    """
    small = A.calibrate_image(_frame_with_templates(offset=(30, 10)), cfg=_Cfg())
    assert small["status"] in ("ok", "adjusted"), small["message"]
    assert small["limit_px"] >= 80
    res = A.calibrate_image(_frame_with_templates(offset=(95, 0)), cfg=_Cfg())
    assert res["status"] == "fallback", res["message"]
    assert res["ok"] is False
    assert "超出可信范围" in res["message"]
    assert res["boxes"] == region_table.BUILTIN_PX


def test_gate_blocks_adjusted_beyond_threshold():
    """P1：闸门不能对 adjusted 一律放行，超阈值当次就拦。"""
    cap = A._max_adjust((1920, 1080))
    ok = A.gate_reason({"status": "adjusted", "client": [1920, 1080],
                        "offset": [cap - 1.0, 0.0]})
    assert ok is None
    bad = A.gate_reason({"status": "adjusted", "client": [1920, 1080],
                         "offset": [cap + 1.0, 0.0]})
    assert bad and "可信上限" in bad
    assert A.gate_reason({"status": "fallback", "message": "缺少关键锚点"})
    assert A.gate_reason({"status": "error", "message": "屏幕外"})
    # P0-B：skipped 不再是绿灯——没锚点、没手框就必须拦。
    assert A.gate_reason({"status": "skipped", "message": "未经校对"})
    assert A.gate_reason({"status": "ok"}) is None


def test_partial_offscreen_window_refuses_instead_of_guessing(tmp_path,
                                                              monkeypatch):
    """P2：窗口有一部分在屏幕外时拒绝标定，不拿显示器矩形冒充客户区。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"queue": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    cfg = cfgmod.Config.load()
    cfg.data["calibration"]["auto"] = True

    class _Sc:
        client_partial = True
        client_rect = {"left": -200, "top": 100, "width": 2120, "height": 1080}
        bounds = {"left": 0, "top": 0, "width": 1920, "height": 1080}

        def grab(self):
            raise AssertionError("越界窗口不该抓图")

        def close(self):
            pass

    monkeypatch.setattr(A.regions, "open_screen", lambda _cfg: _Sc())
    res = A.run_for_engine(cfg)
    assert res["status"] == "error"
    assert res["ok"] is False
    assert "屏幕外" in res["message"] and "2120" in res["message"]
    assert cfgmod.Config.load().get("calibration")["status"] == "error"


def test_record_applies_by_default_and_never_overwrites_manual_regions(
        tmp_path, monkeypatch):
    """P0-C/P0-3：默认落盘（闸门校验的就是会点的坐标）；手框区域永不覆盖。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"queue": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    # 快照写到临时用户目录，别落到真实 %LOCALAPPDATA% 里。
    monkeypatch.setattr(cfgmod, "user_dir", lambda: str(tmp_path / "user"))
    cfg = cfgmod.Config.load()
    manual = {"x": 0.10, "y": 0.30, "w": 0.60, "h": 0.50}
    cfg.set_region("list", manual)

    res = A.calibrate_image(_frame_with_templates(offset=(14, 14)), cfg=cfg)
    assert res["status"] == "adjusted"
    A.record(cfg, res, save=True)              # 默认跟随 apply_auto=True

    again = cfgmod.Config.load()
    # 坐标真的写进去了：引擎点哪儿，闸门校验的就是哪儿。
    assert again.region("buy")["x"] > region_table.BUILTIN["buy"]["x"]
    assert again.get("calibration")["applied_now"]
    assert again.region("list") == manual      # 手框的永不覆盖
    assert "list" in again.get("calibration")["apply_skipped_manual"]

    # apply=False 时只留提议，不碰坐标（给「提议模式」留的口子）。
    cfg2 = cfgmod.Config.load()
    A.record(cfg2, res, save=True, apply=False)
    third = cfgmod.Config.load()
    assert third.region("buy") == again.region("buy")
    assert third.get("calibration")["proposed"]["buy"]["x"] > \
        region_table.BUILTIN["buy"]["x"]


def test_real_screenshot_rois_land_inside_the_builtin_regions():
    """仓库里 5 张真实截图（1920×1080）的人工标注 ROI 必须落在内置区域里。

    这些 ROI 是 `tests/fixtures/price_roi/manifest.json` 里人工标的真值，
    不是我们算出来的；用它检查内置表没有把真实元素框漏。
    """
    manifest = os.path.join(ROOT, "tests", "fixtures", "price_roi", "manifest.json")
    data = json.load(open(manifest, encoding="utf-8"))
    assert "5 张真实截图" in data["source"]
    boxes = A.boxes_for((1920, 1080))
    for item in data["items"]:
        x, y, w, h = item["box"]
        mx, my = x + w / 2.0, y + h / 2.0
        if item["id"] == "u3_detail_price":
            rx, ry, rw, rh = boxes["bar"]
            assert rx <= x and ry <= y and x + w <= rx + rw and y + h <= ry + rh
        elif item["cls"] == "price":
            if y < 210:
                # 顶部筛选价格区间（u4_range_lo/hi），不属于 9 个内置区域，
                # 只要求在 1080p 画面内，不参与内置表校验。
                assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080
            elif x < 1000 and y < 500:
                # 左列第一行：内置 price 框就是按这一排量的（逐位一致）
                rx, ry, rw, rh = boxes["price"]
                assert abs(mx - (rx + rw / 2.0)) <= 3
                assert abs(my - (ry + rh / 2.0)) <= 3
            else:
                # 其余左列行 / 右列价格：落在在售列表大框里
                lx, ly, lw, lh = boxes["list"]
                assert lx <= mx <= lx + lw and ly <= my <= ly + lh
        # cls == currency 是右上角余额，不属于 9 个区域，跳过
