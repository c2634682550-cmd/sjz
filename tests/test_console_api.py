"""控制台（三页版：抢购 / 记录 / 设置）跟后端之间的约定。

界面是 web/index.html 里那段脚本，它只认这些端点。这里守的是**端点真的
返回界面要用的东西**：

  · 抢购条件填的数（价格区间 / 限量 / 预算）要一路到引擎，不能停在界面上
  · 改条件时先删旧任务，删了就要真的没了 —— 留着会按老条件再抢一遍
  · 设置页的开关（成交截图）要落进 config.json，不能点了没反应
  · 启动前检查要给出界面能渲染的字段（哪一项没过、怎么修）
  · 记录页要拿到每一次尝试和统计，清空只清列表、截图还在

测的是行为（真的发请求、真的看返回），不是「源码里有没有这串字」。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config as cfgmod  # noqa: E402
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
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """把配置写到临时文件 —— 测试不许碰用户真的 config.json。

    里面要带一块「列表区域」：列表模式的任务没有它就加不进来
    （引擎得知道盯屏幕上哪一块）。
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "regions": {"list": {"x": 0.03, "y": 0.19, "w": 0.70, "h": 0.68},
                    "buy": {"x": 0.77, "y": 0.83, "w": 0.16, "h": 0.06}},
        "queue": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))
    return path


def _post(base, path, payload=None):
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(base + path, data=body,
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


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 抢购条件

def test_conditions_reach_the_queue(server, cfg_file):
    """界面填的抢购条件要真的进队列 —— 少一个就是按错的价去抢。"""
    code, data = _post(server, "/api/tasks/add", {
        "name": "", "mode": "list",
        "min_price": 100, "max_price": 3000, "quota": 2, "budget": 5000,
    })
    assert code == 200 and data.get("ok"), data
    task = data["task"]
    assert task["min_price"] == 100 and task["max_price"] == 3000
    assert task["quota"] == 2 and task["budget"] == 5000

    _, listing = _get(server, "/api/tasks")
    assert [t["id"] for t in listing["tasks"]] == [task["id"]]


def test_conditions_reach_the_engine(server, cfg_file):
    """队列里的条件要交给引擎 —— 引擎只认配置里的 active_task。

    这里直接查引擎那条路：条件停在队列里、没进 active_task，就是
    「界面填了价，引擎按不限价抢」。
    """
    from core import scheduler as sch

    code, data = _post(server, "/api/tasks/add", {
        "name": "", "mode": "list", "workflow": "countdown",
        "min_price": 10, "max_price": 99, "quota": 3, "budget": 777,
        "materials": ["金属"], "colors": ["黑色"], "categories": ["扑克 J"],
    })
    assert code == 200 and data.get("ok"), data

    task = data["task"]
    assert task["workflow"] == "countdown", "任务必须自己说走哪条路线"
    got = sch.Scheduler._engine_task(task)
    assert got["min_price"] == 10 and got["max_price"] == 99
    assert got["quota"] == 3 and got["budget"] == 777
    assert got["materials"] == ["金属"]
    assert got["colors"] == ["黑色"]
    assert got["categories"] == ["扑克 J"]


def test_filter_lists_accept_a_comma_separated_string(server, cfg_file):
    """配置文件里多选值常写成逗号分隔的字符串 —— 两种都要收成列表。"""
    code, data = _post(server, "/api/tasks/add", {
        "name": "", "mode": "list", "workflow": "countdown", "max_price": 100,
        "materials": "金属, 塑料", "colors": "黑色、白色",
    })
    assert code == 200 and data.get("ok"), data
    assert data["task"]["materials"] == ["金属", "塑料"]
    assert data["task"]["colors"] == ["黑色", "白色"]


def test_an_unknown_workflow_is_refused(server, cfg_file):
    """写错抢购方式要当场退回来，不能默默跑成别的路线。"""
    code, data = _post(server, "/api/tasks/add", {
        "name": "", "mode": "list", "workflow": "乱写", "max_price": 100,
    })
    assert code == 400, data
    assert not data.get("ok") and data.get("msg")


def test_removing_a_task_actually_removes_it(server, cfg_file):
    """改条件时界面会先删旧任务 —— 删不掉的话旧条件会按老价再抢一遍。"""
    _, first = _post(server, "/api/tasks/add", {"name": "", "mode": "list",
                                                "max_price": 100})
    assert first.get("ok"), first
    tid = first["task"]["id"]

    code, data = _post(server, "/api/tasks/remove", {"id": tid})
    assert code == 200 and data.get("ok"), data
    assert data["removed"] == 1

    _, listing = _get(server, "/api/tasks")
    assert [t["id"] for t in listing["tasks"]] == []


def test_removing_an_unknown_task_is_not_an_error(server, cfg_file):
    """界面会先删掉所有旧任务，其中一条可能已经不在了 —— 不能报错。"""
    code, data = _post(server, "/api/tasks/remove", {"id": "不存在"})
    assert code == 200 and data.get("ok")
    assert data["removed"] == 0


def test_a_task_without_the_list_region_is_refused(server, tmp_path, monkeypatch):
    """列表模式要知道盯哪块屏幕，没框选就不能加任务 —— 否则白跑一轮。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"regions": {}, "queue": []}), encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))

    code, data = _post(server, "/api/tasks/add", {"name": "", "mode": "list",
                                                  "max_price": 100})
    assert code == 400, data
    assert not data.get("ok") and data.get("msg")


# ---------------------------------------------------------------- 启动前检查

def test_preflight_reports_every_item_with_a_fix(server, cfg_file):
    """检查项要带「哪一项没过、怎么修」—— 界面靠 fix.tab 跳到对应的页。"""
    code, data = _post(server, "/api/preflight", {"mode": "list"})
    assert code == 200, data
    assert "checks" in data
    for c in data["checks"]:
        assert c["id"] and c["label"] and isinstance(c["ok"], bool)
        if not c["ok"]:
            assert c["detail"], f"{c['id']} 没过却没说怎么判的"
    # 没校过时时钟这一项必须没过 —— 拿没校准的钟去掐点是抢不准的
    clock = next(c for c in data["checks"] if c["id"] == "clock")
    assert not clock["ok"]
    assert clock["fix"]["tab"] == "run", "要能一键跳到校时那一页"


# ---------------------------------------------------------------- 设置页

def test_the_screenshot_switch_reaches_the_config_file(server, cfg_file):
    """成交截图开关要真的落进 config.json。

    白名单漏一个键的表现是「点了提示已保存、其实什么都没存」——
    只有下次开抢才发现截图没存。
    """
    code, data = _post(server, "/api/config", {"save_shots": False})
    assert code == 200 and data.get("ok"), data
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["save_shots"] is False

    _post(server, "/api/config", {"save_shots": True})
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["save_shots"] is True


def test_both_hotkeys_are_reported(server, cfg_file):
    """设置页要同时显示开抢和停止两把键 —— 只显示一把，用户不知道按什么开始。"""
    code, data = _get(server, "/api/hotkey")
    assert code == 200
    for key in ("stop", "start"):
        assert key in data, f"少了 {key} 那把键"
        assert data[key]["spec"], f"{key} 没有键名"
        assert "active" in data[key] and "msg" in data[key]


# ---------------------------------------------------------------- 状态与日志

def test_scheduler_status_carries_what_the_console_renders(server, cfg_file):
    """抢购页轮询的就是这个端点，字段少一个界面就空一块。"""
    code, data = _get(server, "/api/scheduler")
    assert code == 200
    assert "running" in data and "tasks" in data and "log" in data
    eng = data["engine"]
    assert "state" in eng and "stats" in eng
    for k in ("clicks", "elapsed"):
        assert k in eng["stats"], f"引擎状态里少了 {k}"


def test_log_lines_carry_a_sortable_timestamp(server, cfg_file):
    """日志行要带浮点时间戳 —— 界面把调度器和引擎两路日志并起来排，
    只按 HH:MM:SS 排会把同一秒里的先后打乱。"""
    from core import scheduler as sch

    s = sch.Scheduler()
    s._say("第一条")
    s._say("第二条")
    for line in s._log:
        assert set(("t", "ts", "level", "msg")) <= set(line), line
        assert isinstance(line["ts"], float)


# ---------------------------------------------------------------- 记录页

def test_records_endpoint_returns_rows_and_stats(server, cfg_file, tmp_path, monkeypatch):
    """记录页要拿到每一次尝试（不只是成功的）和统计。"""
    from core import records as rec

    monkeypatch.setattr(rec, "PATH", str(tmp_path / "records.jsonl"))
    rec.add(name="AKM-群星", price=2050, result="hit", elapsed=1.2)
    rec.add(name="M4A1-电玩高手", price=None, result="miss", elapsed=0.8)

    code, data = _get(server, "/api/records")
    assert code == 200
    assert len(data["rows"]) == 2, "只记成功的那几次 = 永远不知道自己试了几次"
    st = data["stats"]
    assert st["tries"] == 2 and st["hits"] == 1
    assert st["spent"] == 2050 and st["rate"] == 50.0


def test_clearing_records_keeps_the_screenshots(server, cfg_file, tmp_path, monkeypatch):
    """清空只清列表 —— 截图是证据，不能跟着没了。"""
    from core import records as rec

    monkeypatch.setattr(rec, "PATH", str(tmp_path / "records.jsonl"))
    rec.add(name="AKM-群星", price=2050, result="hit", elapsed=1.2)

    shots = tmp_path / "data" / "shots"
    shots.mkdir(parents=True)
    shot = shots / "20261023_051206_成交.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xe0")

    code, data = _post(server, "/api/records/clear")
    assert code == 200 and data.get("ok")

    _, listing = _get(server, "/api/records")
    assert listing["rows"] == []
    assert shot.exists(), "清空记录把截图也删了"


# ------------------------------------------------- 新增的筛选条件（成色/磨损/材质/颜色/排序）

def test_new_conditions_round_trip_through_config(server, cfg_file):
    """控制台新加的条件要存得下来、类型还对。

    数字要存成数字、字符串原样存、多选存成列表 —— 存错了下次打开
    界面就是空的，或者把「S」存成 0。
    """
    code, data = _post(server, "/api/config", {"console": {
        "min_price": 100, "max_price": 3000, "quota": 2, "budget": 5000,
        "workflow": "list", "craft": "极品", "wear": "A", "max_wear": 1.25,
        "sort": "wear", "materials": ["镭射"], "colors": ["黑色", "炫彩"],
        "categories": ["彩绘款"],
    }})
    assert code == 200 and data.get("ok"), data

    _, cfg = _get(server, "/api/config")
    c = cfg["console"]
    assert c["min_price"] == 100 and c["max_price"] == 3000
    assert c["quota"] == 2 and c["budget"] == 5000
    assert c["workflow"] == "list" and c["sort"] == "wear"
    assert c["craft"] == "极品" and c["wear"] == "A"
    assert c["max_wear"] == 1.25
    assert c["materials"] == ["镭射"]
    assert c["colors"] == ["黑色", "炫彩"]
    assert c["categories"] == ["彩绘款"]


def test_craft_wear_and_sort_reach_the_engine(server, cfg_file):
    """成色/磨损上限/排序也要进引擎 —— 少一个就是那项条件没生效。"""
    from core import scheduler as sch

    code, data = _post(server, "/api/tasks/add", {
        "name": "", "mode": "list", "workflow": "list", "max_price": 3000,
        "craft": "优品", "wear": "S", "max_wear": 0.4, "sort": "wear",
    })
    assert code == 200 and data.get("ok"), data

    got = sch.Scheduler._engine_task(data["task"])
    assert got["craft"] == "优品"
    assert got["wear"] == "S"
    assert got["max_wear"] == 0.4
    assert got["sort"] == "wear"


def test_updating_a_task_can_change_the_sort(server, cfg_file):
    """界面改条件是改同一条任务，排序也得改得动。"""
    _, first = _post(server, "/api/tasks/add", {"name": "", "mode": "list",
                                                "max_price": 100})
    assert first.get("ok"), first
    tid = first["task"]["id"]

    code, data = _post(server, "/api/tasks/update", {"id": tid, "sort": "wear"})
    assert code == 200 and data.get("ok"), data
    assert data["task"]["sort"] == "wear"
