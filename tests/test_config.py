"""config.json 的读写：什么能存、什么存不进去。

为什么值得单独守：config._merge 只保留**白名单里的键**。白名单漏一个，
用户手改的配置就会在下次启动时被悄悄丢掉 —— 而且不报错（配置照样读得
出来，只是那个值没了）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402


def _write(tmp_path, monkeypatch, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    return path


def test_the_price_bar_region_survives_a_round_trip(tmp_path, monkeypatch):
    """引擎读 cfg.region("bar")（公示期版式价签会飘时补读价格）。

    以前 "bar" 根本不在白名单里 —— 那一行永远读到 None，配置里写了也
    存不进去，程序只能一直用硬编码坐标。
    """
    box = {"x": 0.77, "y": 0.83, "w": 0.16, "h": 0.06}
    _write(tmp_path, monkeypatch, {"regions": {"bar": box}})

    cfg = cfgmod.Config.load()
    assert cfg.region("bar") == box


def test_unknown_region_keys_are_dropped(tmp_path, monkeypatch):
    """白名单外的键不认 —— 免得拼错字以为存进去了。"""
    _write(tmp_path, monkeypatch, {"regions": {"瞎写的": {"x": 0, "y": 0, "w": 1, "h": 1}}})

    cfg = cfgmod.Config.load()
    assert cfg.region("瞎写的") is None


def test_the_workflow_is_stored_and_readable(tmp_path, monkeypatch):
    """抢购路线是任务级选择，但全局也有个默认值。"""
    path = _write(tmp_path, monkeypatch, {"workflow": "list"})

    cfg = cfgmod.Config.load()
    assert cfg["workflow"] == "list"

    cfg["workflow"] = "countdown"
    cfg.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["workflow"] == "countdown"


def test_a_fresh_config_gets_the_documented_defaults(tmp_path, monkeypatch):
    """第一次启动要落一份能看懂的默认配置。"""
    path = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))

    cfg = cfgmod.Config.load()
    assert cfg["workflow"] == "countdown"
    assert cfg["threshold"] == 0.85
    assert path.exists()
