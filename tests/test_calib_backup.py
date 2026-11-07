"""apply_auto 写配置的备份 / 读回校验 / 回滚。

覆盖两条失败路径：
  1. 写盘失败（save 抛异常）→ 内存与磁盘都必须回到写入前；
  2. 写成功但读回被外部改动 → 按快照整体回滚，并拦本次启动。
"""
import json
import os
import sys

import cv2
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import autocalib as A          # noqa: E402
from core import config as cfgmod       # noqa: E402
from core import region_backup as RB    # noqa: E402
from core import truth_model as TM      # noqa: E402
from core import vision                 # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = lambda name: os.path.join(ROOT, "templates", name + ".png")  # noqa: E731


class _Cfg:
    def template_path(self, name):
        return TPL(name)


def _frame(offset=(14, 14), size=(1920, 1080)):
    """贴 buy/confirm 模板的基准帧；offset 让它落到 adjusted 分支。"""
    w, h = size
    frame = np.full((h, w, 3), 245, np.uint8)
    scale = min(w / 1920.0, h / 1080.0)
    boxes, _ = A._aspect_fix(A.boxes_for(size), size)
    for name in ("buy", "confirm"):
        tpl = cv2.imread(TPL(name))
        used = vision.scaled(tpl, scale)
        x, y, bw, bh = boxes[name]
        tx = int(round(x + bw / 2.0 - used.shape[1] / 2.0 + offset[0]))
        ty = int(round(y + bh / 2.0 - used.shape[0] / 2.0 + offset[1]))
        frame[ty:ty + used.shape[0], tx:tx + used.shape[1]] = used
    return frame


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"queue": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    monkeypatch.setattr(cfgmod, "user_dir", lambda: str(tmp_path / "user"))
    cfg = cfgmod.Config.load()
    cfg.save()                                   # 让磁盘上先有完整 regions
    pre = json.loads(json.dumps(cfg.get("regions")))
    return cfg, path, pre, tmp_path


def _disk_regions(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["regions"]


def test_backup_is_written_before_apply_and_lives_outside_repo(env):
    cfg, path, pre, tmp_path = env
    res = A.calibrate_image(_frame(), cfg=cfg)
    assert res["status"] == "adjusted"
    A.record(cfg, res, save=True)

    paths = RB.list_backups()
    assert len(paths) == 1
    snap = RB.load(paths[0])
    assert snap["reason"] == "auto-calibration"
    assert snap["regions"] == pre                       # 写入前的整段快照
    assert not os.path.abspath(paths[0]).startswith(os.path.abspath(ROOT))
    assert "region_backups" in paths[0]

    # 写完的坐标确实变了，且与提案一致（读回校验已通过）。
    assert _disk_regions(path)["buy"] != pre["buy"]
    assert cfg.get("calibration")["backup"] == paths[0]
    assert cfg.get("calibration")["rolled_back"] == ""


def test_write_failure_restores_snapshot(env, monkeypatch):
    cfg, path, pre, _ = env
    res = A.calibrate_image(_frame(), cfg=cfg)

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(cfg, "save", boom)
    out = A.record(cfg, res, save=True)

    assert out["gate"] and "回滚" in out["gate"]
    assert cfg.get("regions") == pre                    # 内存已恢复
    assert _disk_regions(path) == pre                   # 磁盘从未被写坏
    assert RB.list_backups(), "失败前必须先留下快照"


def test_external_tamper_after_write_rolls_back_snapshot(env, monkeypatch):
    """写盘被外部改动：读回值与提案不一致 → 整体回滚到快照。"""
    cfg, path, pre, _ = env
    res = A.calibrate_image(_frame(), cfg=cfg)
    real_save = cfg.save
    state = {"n": 0}

    def tampering_save():
        state["n"] += 1
        if state["n"] == 1:
            data = json.loads(json.dumps(cfg.get("regions")))
            data["buy"]["x"] = float(data["buy"]["x"]) + 0.01   # 模拟外部改动
            cfg.data["regions"] = data
        real_save()

    monkeypatch.setattr(cfg, "save", tampering_save)
    out = A.record(cfg, res, save=True)

    assert out["gate"] and "回滚" in out["gate"]
    assert out["rolled_back"]
    assert _disk_regions(path) == pre                   # 整段回到写入前
    again = cfgmod.Config.load()
    assert again.get("regions") == pre
    assert again.get("calibration")["applied_now"] == []
    assert again.get("calibration")["rolled_back"] == out["rolled_back"]
    # 快照本身完整，可用于一键回滚。
    assert RB.load(out["rolled_back"])["regions"] == pre


def test_restore_reverts_regions_and_applied(env):
    cfg, path, pre, _ = env
    res = A.calibrate_image(_frame(), cfg=cfg)
    A.record(cfg, res, save=True)
    snap_path = cfg.get("calibration")["backup"]
    changed = cfgmod.Config.load()
    assert changed.region("buy")["x"] > pre["buy"]["x"]

    RB.restore(changed, snap_path)
    again = cfgmod.Config.load()
    assert again.get("regions") == pre
    assert again.get("calibration")["applied"] == {}


# ------------------------------------------------------------------ 真实位置模型

def test_truth_model_marks_default_as_model_not_measured():
    boxes, meta = TM.truth_boxes((3440, 1440))
    assert meta["scale"] == pytest.approx(1440 / 1080.0)
    assert meta["source"] == "model"
    assert meta["measured"] is False
    assert boxes["buy"][0] == pytest.approx(1482 * 1440 / 1080.0
                                            + (3440 - 1920 * 1440 / 1080.0) / 2)


def test_truth_model_accepts_measured_anchor_table():
    anchors = {(3440, 1440): {"client": (3440, 1440), "scale": 1.0,
                              "dx": 0.0, "dy": 0.0, "source": "真机标注"}}
    assert TM.truth_scale(3440, 1440, anchors) == 1.0
    boxes, meta = TM.truth_boxes((3440, 1440), None, anchors)
    assert meta["source"] == "真机标注" and meta["measured"] is True
    assert boxes["buy"][0] == pytest.approx(1482.0)


def test_load_anchors_reports_bad_rows(tmp_path):
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps({"anchors": [
        {"client": [3440, 1440], "scale": 1.3333, "source": "人工"},
        {"client": "bad"},
    ]}), encoding="utf-8")
    anchors, bad = TM.load_anchors(p)
    assert anchors[(3440, 1440)]["scale"] == pytest.approx(1.3333)
    assert bad and "第 2 条" in bad[0]
