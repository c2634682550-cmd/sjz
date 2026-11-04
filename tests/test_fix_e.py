"""验证轮 R1-R5 的离线回归。

R1  配置读失败被当成「队列跑完了」
R2  新建任务默认 mode 与全局默认分歧
R3  run_mode 与 env.dry_run 语义重叠
R4  驱动点击异常路径（down 失败仍补 UP / finally 覆盖原始异常）
R5  nav_ons 筛选项列位写死 1396/1641

全部离线：不启动 GUI、不抓屏、不注入输入、不联网。
"""
import builtins
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
from core import interception as X  # noqa: E402
from core import input as I  # noqa: E402
from core import scheduler as sch  # noqa: E402
from core.config import Config, resolve_dry_run  # noqa: E402
from core.nav_ons import NavOnsPanel  # noqa: E402


# ---------------------------------------------------------------- 工具


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把 CONFIG_PATH 指到临时文件；返回 (path, 写内容的函数)。"""
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(p))
    # 模板目录也挪走，避免 Scheduler.add 往仓库 data/ 里写文件
    monkeypatch.setattr(cfgmod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cfgmod, "TEMPLATE_DIR", str(tmp_path / "tpl"))
    (tmp_path / "tpl").mkdir()
    # add() 要求购买/确认模板存在（不然只返回「先框选按钮」）
    for name in ("buy", "confirm"):
        (tmp_path / "tpl" / f"{name}.png").write_bytes(b"\x89PNG\r\n")

    def write(data):
        p.write_text(json.dumps(data), encoding="utf-8")

    return p, write


def _break_config_reads(monkeypatch, path):
    """让读取 config 路径的 open 抛 OSError（模拟被占用/权限不足）。"""
    real_open = builtins.open
    target = os.path.abspath(str(path))

    def fake_open(file, *a, **kw):
        try:
            same = os.path.abspath(os.fspath(file)) == target
        except TypeError:
            same = False
        if same:
            raise OSError(13, "Permission denied")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)


# ---------------------------------------------------------------- R1


def test_config_reports_read_failure(cfg_file, monkeypatch):
    """Config.load 读不出来时必须能区分「读失败」和「配置就是空」。"""
    _, write = cfg_file
    write({"queue": [{"id": "a", "name": "A", "status": "pending"}]})
    _break_config_reads(monkeypatch, cfgmod.CONFIG_PATH)

    cfg = Config.load()
    assert cfg.load_ok is False
    assert cfg.last_error and "读不出来" in cfg.last_error
    # 手上一份是 DEFAULTS，不是用户的队列 —— 但状态位能看出来
    assert cfg["queue"] == []
    pub = cfg.public()
    assert pub["config_ok"] is False
    assert pub["config_error"]


def test_scheduler_keeps_queue_when_config_unreadable(cfg_file, monkeypatch):
    """读失败时保持上一次的队列快照，不推进状态机、不写盘。"""
    path, write = cfg_file
    write({"queue": [{"id": "a", "name": "A", "status": "pending",
                      "enabled": True}]})
    s = sch.Scheduler()
    assert [t["id"] for t in s.tasks()] == ["a"]        # 成功一次，缓存拿到队列

    before = path.read_text(encoding="utf-8")
    _break_config_reads(monkeypatch, path)

    assert [t["id"] for t in s.tasks()] == ["a"], "读失败不能把队列当空"
    st = s.tasks_status()
    assert st["config_ok"] is False and st["config_error"]
    assert [t["id"] for t in st["tasks"]] == ["a"]
    status = s.status()
    assert status["config_ok"] is False
    assert [t["id"] for t in status["tasks"]] == ["a"]

    s._stop.set()                    # 别在 _run_step 里真等 0.5s
    assert s._run_step(True) is True, "读失败要连任，不能判「跑完了」"
    assert any("配置读不出来" in e["msg"] for e in s._log)
    # 状态机没动
    assert path.read_text(encoding="utf-8") == before


def test_scheduler_start_refuses_when_config_unreadable(cfg_file, monkeypatch):
    _, write = cfg_file
    write({"queue": [{"id": "a", "name": "A", "status": "pending"}]})
    _break_config_reads(monkeypatch, cfgmod.CONFIG_PATH)
    s = sch.Scheduler()
    ok, msg = s.start()
    assert ok is False, "配置读不出来时不能拿默认配置开抢"
    assert "读不出来" in msg


# ---------------------------------------------------------------- R2


def test_add_defaults_to_global_workflow(cfg_file):
    """两者都没给：走全局默认，不再是写死的 detail。"""
    _, write = cfg_file
    write({"workflow": "countdown",
           "regions": {"buy": {"x": 0.9, "y": 0.9, "w": 0.05, "h": 0.05},
                       "list": {"x": 0.2, "y": 0.2, "w": 0.4, "h": 0.4}}})
    s = sch.Scheduler()
    t, err = s.add("A")
    assert err is None, err
    assert t["workflow"] == "countdown"
    # mode 是 workflow 的派生值（countdown 属于 detail 模式），不再是入参默认
    assert t["mode"] == "detail"

    t2, err = s.add("B", mode="detail")
    assert t2["workflow"] == "detail"

    t3, err = s.add("C", mode="detail", workflow="list")
    assert t3["workflow"] == "list", "显式 workflow 要压过老 mode"


def test_add_follows_global_default_when_it_is_detail(cfg_file):
    _, write = cfg_file
    write({"workflow": "detail",
           "regions": {"buy": {"x": 0.9, "y": 0.9, "w": 0.05, "h": 0.05}}})
    s = sch.Scheduler()
    t, err = s.add("A")
    assert err is None, err
    assert t["workflow"] == "detail"


def test_preflight_uses_same_route_as_add(cfg_file):
    """preflight 缺省也应回落到全局默认（以前写死 list）。"""
    from core.server import _preflight_workflow
    _, write = cfg_file
    write({"workflow": "countdown",
           "regions": {"buy": {"x": 0.9, "y": 0.9, "w": 0.05, "h": 0.05}}})
    cfg = {"workflow": "countdown"}
    assert _preflight_workflow({}, cfg) == "countdown"
    assert _preflight_workflow({"mode": "list"}, cfg) == "list"
    assert _preflight_workflow({"workflow": "detail"}, cfg) == "detail"
    # 和 Scheduler.add 推导同一结论
    s = sch.Scheduler()
    assert s.add("A")[0]["workflow"] == _preflight_workflow({}, cfg)


# ---------------------------------------------------------------- R3


@pytest.mark.parametrize("data,expect,srclike", [
    ({"run_mode": "auto", "dry_run": True}, False, "run_mode"),
    ({"run_mode": "semi", "dry_run": True}, False, "run_mode"),
    ({"run_mode": "dry", "dry_run": False}, True, "run_mode"),
    ({"dry_run": True}, True, "dry_run"),
    ({}, False, "缺省"),
])
def test_resolve_dry_run_priority(data, expect, srclike):
    dry, src = resolve_dry_run(data)
    assert dry is expect
    assert srclike in src


def test_request_beats_run_mode():
    dry, src = resolve_dry_run({"run_mode": "dry"}, requested=False)
    assert dry is False and src == "请求指定"


def test_http_and_config_share_one_decision(cfg_file):
    """/api/config 展示的演练结论 == HTTP 启动判定。"""
    from core.server import _dry_run_requested, _preflight_workflow  # noqa: F401
    _, write = cfg_file
    for data in ({"run_mode": "auto", "dry_run": True},
                 {"run_mode": "dry", "dry_run": False},
                 {"dry_run": True}):
        write(data)
        pub = Config.load().public()
        assert pub["dry_run"] is _dry_run_requested({})
        assert pub["dry_run_source"]


def test_scheduler_logs_final_decision():
    """调度器必须走同一个函数，并把结论+来源打进日志。"""
    src = inspect.getsource(sch.Scheduler.start)
    assert "resolve_dry_run(cfg, dry_run)" in src
    assert "判定来源" in src
    assert "switches.get" not in src, "调度器里不该再自己拼 run_mode"


# ---------------------------------------------------------------- R4


class _Drv:
    """假驱动：记录每次 send，可在第 n 次调用时抛 InterceptionError。"""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def send_mouse(self, state, x=0, y=0, flags=0):
        self.calls.append(state)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise X.InterceptionError("驱动被卸了")


def _mouse_with(drv, monkeypatch):
    monkeypatch.setattr(X, "shared", lambda: drv)
    m = I.Mouse(None, mode="legacy")
    m._mode = "interception"          # 跳过 driver_ready 的真实检测
    return m


def test_click_down_failure_does_not_send_up(monkeypatch):
    drv = _Drv(fail_at=2)             # 第 2 次 = DOWN
    m = _mouse_with(drv, monkeypatch)
    with pytest.raises(I.MouseStolen) as ei:
        m.click(100, 100)
    assert X.MOUSE_LEFT_UP not in drv.calls, \
        f"down 没按下去就不该补 UP，实际发了 {drv.calls}"
    assert drv.calls == [0, X.MOUSE_LEFT_DOWN]
    assert isinstance(ei.value.__cause__, X.InterceptionError), \
        "原始驱动异常链要保留（raise ... from）"


def test_click_up_failure_keeps_cause(monkeypatch):
    drv = _Drv(fail_at=3)             # 第 3 次 = UP
    m = _mouse_with(drv, monkeypatch)
    with pytest.raises(I.MouseStolen) as ei:
        m.click(100, 100)
    assert drv.calls == [0, X.MOUSE_LEFT_DOWN, X.MOUSE_LEFT_UP]
    assert isinstance(ei.value.__cause__, X.InterceptionError)


def test_click_success_is_move_down_up(monkeypatch):
    drv = _Drv()
    m = _mouse_with(drv, monkeypatch)
    m.click(100, 100)
    assert drv.calls == [0, X.MOUSE_LEFT_DOWN, X.MOUSE_LEFT_UP]


# ---------------------------------------------------------------- R5


def _nav():
    return NavOnsPanel()               # 纯 mixin，无 __init__


def _write_nav(cfg_file, nav):
    p, write = cfg_file
    write({"nav": nav})


def test_columns_derived_from_panel_geometry(cfg_file):
    _write_nav(cfg_file, {"ons_panel": [1360, 1900],
                          "ons_columns": [1396, 1641]})
    n = _nav()
    cols, src = n._ons_columns()
    assert src == "几何"
    assert round(cols[0]) == 1396
    assert round(cols[1]) == 1641


def test_columns_follow_panel_shift(cfg_file):
    """面板整体右移 40px：列心跟着走，不用改代码。"""
    _write_nav(cfg_file, {"ons_panel": [1400, 1940],
                          "ons_columns": [1396, 1641]})
    cols, src = _nav()._ons_columns()
    assert src == "几何"
    assert round(cols[0]) == 1436
    assert round(cols[1]) == 1681


def test_columns_fall_back_to_config_when_geometry_broken(cfg_file):
    _write_nav(cfg_file, {"ons_panel": [1900, 1360],   # 左右反了
                          "ons_columns": [1500, 1700]})
    cols, src = _nav()._ons_columns()
    assert src == "配置标定"
    assert (round(cols[0]), round(cols[1])) == (1500, 1700)


def test_columns_fall_back_to_code_default(cfg_file):
    _write_nav(cfg_file, {"ons_panel": None, "ons_columns": "坏了"})
    cols, src = _nav()._ons_columns()
    assert src == "兜底"
    assert (round(cols[0]), round(cols[1])) == (1396, 1641)


def test_columns_scale_with_resolution(cfg_file):
    _write_nav(cfg_file, {"ons_panel": [1360, 1900],
                          "ons_columns": [1396, 1641]})
    n = _nav()
    n.size = (1280, 720)
    cols, _src = n._ons_columns()
    assert abs(cols[0] - 1396 * 1280 / 1920) < 2
    assert abs(cols[1] - 1641 * 1280 / 1920) < 2


def test_column_mismatch_writes_observable_log():
    """列位偏移导致全部被过滤时，要留下实测/判定列位的日志。"""
    n = _nav()
    logs = []
    n._log = lambda msg, level="info": logs.append((msg, level))
    n._ons_warn_columns((1396.0, 1641.0), "几何", [2200, 2300, 2400], 6)
    assert len(logs) == 1
    msg, level = logs[0]
    assert level == "warn"
    assert "列位对不上" in msg and "2200" in msg and "1396" in msg
    # 节流：立刻再来一次不再刷屏
    n._ons_warn_columns((1396.0, 1641.0), "几何", [2200, 2300], 6)
    assert len(logs) == 1


def test_grid_column_split_uses_given_midpoint():
    boxes = [{"cx": 1396, "cy": 300, "x": 1380, "y": 285, "w": 29, "h": 29},
             {"cx": 1641, "cy": 300, "x": 1626, "y": 285, "w": 29, "h": 29}]
    grid = NavOnsPanel._ons_assign_grid(boxes, [], None, 10,
                                        col_split=(1396 + 1641) / 2.0)
    assert [g[2] for g in grid] == [0, 1]
