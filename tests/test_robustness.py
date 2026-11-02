"""一批「不该再犯」的健壮性回归。

这些用例对应的都是真实踩到的坑，不是假想的边界：

  1. 有个文件语法错误却没人发现（tools/ocr_eval.py 被一次脚本改坏，
     compileall 之前不跑，谁都没注意）
  2. 灰度图 / 四通道图喂进卡片检测直接抛 cv2 异常
  3. config.json 写坏一个字，整套设置被静默重置并覆盖原文件
  4. OCR 管家 stop 之后再 start，线程立刻退出（停止标志没复位）
  5. 接口里没接住的异常会把 HTTP 连接掐掉，前端只看到「网络错误」
  6. 点击坐标算错时越界，被系统夹到屏幕边缘 —— 而左上角正是急停位置
  7. 队列里一条任务抛异常，整条队列停摆，剩下的永远「等待」
"""

import io
import json
import os
import sys
import time

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import cards, config, market, ocr_service, scheduler, server, vision


# ---------------------------------------------------------------- 1. 语法

def test_every_source_file_compiles():
    """所有 .py 都要能编译 —— 一次脚本改坏转义，工具就整个不能用了。"""
    bad = []
    for base in ("core", "tools", "tests"):
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".py"):
                continue
            path = os.path.join(d, name)
            try:
                src = io.open(path, encoding="utf-8").read()
                compile(src, path, "exec")
            except SyntaxError as e:
                bad.append(f"{path}:{e.lineno} {e.msg}")
    for name in ("desktop.py", "run.py"):
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            try:
                compile(io.open(path, encoding="utf-8").read(), path, "exec")
            except SyntaxError as e:
                bad.append(f"{path}:{e.lineno} {e.msg}")
    assert not bad, f"这些文件编译不过：{bad}"


# ---------------------------------------------------------------- 2. 通道数

@pytest.mark.parametrize("shape,label", [
    ((300, 400), "灰度"),
    ((300, 400, 2), "2 通道"),
    ((300, 400, 4), "BGRA"),
])
def test_scan_accepts_any_channel_count(shape, label):
    """卡片检测不能因为图是单通道就抛 cv2 异常。

    `cv2.cvtColor(img, COLOR_BGR2GRAY)` 只吃 3/4 通道，喂灰度图会
    「Invalid number of channels」。截图工具、测试夹具、
    cv2.imread(path, 0) 都可能给出单通道。
    """
    img = np.zeros(shape, np.uint8)
    assert cards.locate_cards(img) == []
    assert market.scan(img) == []


def test_to_gray_handles_every_depth():
    assert vision.to_gray(np.zeros((5, 5), np.uint8)).shape == (5, 5)
    assert vision.to_gray(np.zeros((5, 5, 3), np.uint8)).shape == (5, 5)
    assert vision.to_gray(np.zeros((5, 5, 4), np.uint8)).shape == (5, 5)
    assert vision.to_gray(np.zeros((5, 5, 2), np.uint8)).shape == (5, 5)
    assert vision.to_gray(None) is None


# ---------------------------------------------------------------- 3. 配置损坏

def test_broken_config_is_quarantined_not_destroyed(tmp_path, monkeypatch):
    """config.json 写坏了要留档，不能直接覆盖成默认值。

    用户手改漏个逗号 → 以前所有设置（价格区间、队列、框选区域）当场清零，
    连「我改坏了什么」都看不到。
    """
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    io.open(path, "w", encoding="utf-8").write('{"console": {"min_price": 5000,}}')

    cfg = config.Config.load()

    backups = [p for p in os.listdir(str(tmp_path)) if ".broken-" in p]
    assert len(backups) == 1, f"没留备份：{os.listdir(str(tmp_path))}"
    kept = io.open(os.path.join(str(tmp_path), backups[0]), encoding="utf-8").read()
    assert "min_price" in kept, "备份里应该是用户原来的内容"
    assert cfg["console"]["min_price"] == 0, "加载的应该是默认值"
    # 新文件是合法 JSON
    json.loads(io.open(path, encoding="utf-8").read())


def test_save_is_atomic_under_concurrency(tmp_path, monkeypatch):
    """并发保存不能写出半个文件。

    服务端线程存开关、调度器线程存任务状态，两边都在写同一个 config.json。
    """
    import threading

    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    errors = []

    def worker(n):
        for i in range(20):
            try:
                c = config.Config.load()
                c["console"] = {"min_price": n, "max_price": i}
                c.save()
                json.loads(io.open(path, encoding="utf-8").read())
            except Exception as e:               # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors[:3]
    assert not os.path.exists(path + ".tmp"), "临时文件没清掉"


# ---------------------------------------------------------------- 4. 管家重启

def test_supervisor_can_restart_after_stop():
    """stop() 之后再 start() 必须真的跑起来。

    以前 start 不清停止标志，新线程一进 _loop 就被判死 ——
    「点了重启没反应」，日志里一个字都没有。
    """
    svc = ocr_service.Supervisor(
        settings={"enabled": True, "start_on_boot": False,
                  "check_interval_s": 999},
        log=lambda m: None)
    assert svc.start() is True
    time.sleep(0.15)
    svc.stop()
    assert not svc._thread.is_alive()

    assert svc.start() is True, "重启没起来"
    time.sleep(0.2)
    assert svc._thread.is_alive(), "重启后线程立刻退出了（停止标志没复位）"
    svc.stop()


# ---------------------------------------------------------------- 5. 接口兜底

def test_handler_exception_becomes_a_json_500():
    """处理函数抛异常 → 500 + JSON，而不是把连接掐掉。

    以前 /api/timesync 收到 samples="abc"，int() 直接 ValueError，
    BaseHTTPRequestHandler 把连接关了，前端只看到「网络错误」。
    """

    class Stub:
        _guard = server.Handler._guard

        def _json(self, obj, code=200):
            return code, obj

    def boom():
        raise ValueError("模拟一个没接住的异常")

    code, body = Stub()._guard(boom, "/api/x")
    assert code == 500
    assert body["ok"] is False
    assert "ValueError" in body["msg"]
    assert body["path"] == "/api/x"


# ---------------------------------------------------------------- 6. 点击越界

def test_click_rejects_points_outside_the_screen():
    """坐标算错时不许硬点。

    越界坐标会被系统夹到屏幕边缘，而这个程序的急停判据正是
    「鼠标甩到左上角」—— 一个越界点击看起来像用户按了急停。
    """
    from core.input import Mouse

    m = Mouse.__new__(Mouse)          # 不初始化（会去碰 Windows API）
    m._origin_left = m._origin_top = 0
    m._vx = m._vy = 0
    m._vw, m._vh = 1919, 1079

    assert m._abs(0, 0) == (0, 0)
    assert m._abs(1919, 1079) == (65535, 65535)
    assert m._abs(1920, 1080) == (65535, 65535)     # 边界容 2px
    for pt in ((-3, 10), (10, -3), (5000, 10), (10, 5000)):
        with pytest.raises(ValueError):
            m._abs(*pt)


# ---------------------------------------------------------------- 7. 队列韧性

def test_one_failing_task_does_not_kill_the_queue(tmp_path, monkeypatch):
    """一条任务炸了，剩下的还要继续跑。

    以前异常一路冒到 _run 的 except，线程结束，后面的任务永远「等待」。
    """
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    s = scheduler.Scheduler()
    cfg = config.Config()
    cfg["queue"] = [
        {"id": "a", "name": "甲", "status": scheduler.PENDING, "enabled": True,
         "fire_at": 0, "mode": "list", "regions": {}, "keywords": []},
        {"id": "b", "name": "乙", "status": scheduler.PENDING, "enabled": True,
         "fire_at": 0, "mode": "list", "regions": {}, "keywords": []},
    ]
    s._cfg = cfg

    ran = []

    def fake_run_task(task, dry_run):
        ran.append(task["id"])
        raise RuntimeError("模拟引擎炸了")

    monkeypatch.setattr(s, "_run_task", fake_run_task)
    s._run(dry_run=True)

    assert ran == ["a", "b"], f"第二条没跑到：{ran}"
    status = {t["id"]: t["status"] for t in cfg["queue"]}
    assert status == {"a": scheduler.FAILED, "b": scheduler.FAILED}, status
    assert any("跳过它继续" in e["msg"] for e in s._log), "日志里没说清楚"
