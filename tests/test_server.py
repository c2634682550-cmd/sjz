"""server.py 的回归测试：前端用到的那些端点必须真的能用。

界面是三页版（抢购 / 记录 / 设置），它只依赖这些 JSON 端点 ——
预览流（/api/stream）在这一版里没有页面用它，已经删掉了。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server import STATIC, Handler, ThreadingHTTPServer  # noqa: E402


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


def test_config_endpoint_serves_json(server):
    """配置端点要能直接读出配置本体 —— 设置页靠它回显开关状态。"""
    with urllib.request.urlopen(server + "/api/config", timeout=10) as r:
        assert r.status == 200
        cfg = json.loads(r.read().decode("utf-8"))
    assert "regions" in cfg and "threshold" in cfg


# ---------------------------------------------------------------- assets

def _get(base, path, timeout=10):
    """发一个 GET，返回 (状态码, body bytes)。"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get_raw(base, raw_target, timeout=10):
    """用原始 socket 发请求，**不做 URL 规范化**。

    urllib 会在发出去之前把 `assets/../index.html` 折成 `/index.html`，
    于是请求根本到不了 _asset，穿越守卫永远测不到。这里直接写请求行。
    """
    host, port = base.replace("http://", "").rstrip("/").rsplit(":", 1)
    req = (f"GET {raw_target} HTTP/1.0\r\nHost: {host}\r\n"
           f"Connection: close\r\n\r\n").encode()
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.sendall(req)
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body


def test_asset_serves_the_logo_and_background(server):
    """前端引用的两张官方素材必须能取到。

    回归：do_GET 原来只硬编码了 `/` 和 `/index.html`，没有通用静态分支，
    `/assets/*` 一律 404 —— 界面上 logo 破图、背景没了，而且不报任何错。
    """
    base = server + "/"
    for name, sig in (("assets/df-logo.png", b"\x89PNG"),
                      ("assets/df-bg.jpg", b"\xff\xd8\xff")):
        code, body = _get(base, name)
        assert code == 200, f"{name} 返回 {code}，素材没被服务出来"
        assert body.startswith(sig), f"{name} 不是预期的图片格式"
        assert len(body) > 1000, f"{name} 只有 {len(body)} 字节，像是被截断"


def test_asset_rejects_path_traversal(server):
    """`/assets/` 后面的东西来自 HTTP 路径，必须挡住 `../`。

    服务绑在 127.0.0.1，但浏览器里任何一个页面都能往它发请求 —— 穿越成功
    就等于把仓库里的任意图片读出去（`config.json` 不在此列，后缀白名单另挡）。

    **用例必须是「后缀合法 + 目标真实存在」**，否则会同时被后缀白名单和
    「文件不存在」拦住，测试通过但守卫根本没被触发 —— 这一点踩过坑：
    最初用 `../index.html` / `../../core/server.py`，两种失效条件下都返回
    404，断言完全是永真的。
    这里用仓库根的真实 PNG（`_shots/` 下的截图），只差「是否在 assets 内」
    这一个判据。
    """
    outside = os.path.join(STATIC, "..", "_shots")   # STATIC=web/，往上一级是仓库根
    real = None
    if os.path.isdir(outside):
        for f in sorted(os.listdir(outside)):
            if os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg", ".webp"):
                real = f
                break
    if not real:
        pytest.skip("仓库根下没有可用作穿越目标的图片")

    for evil in (f"/assets/../../_shots/{real}",
                 f"/assets/..%2f..%2f_shots%2f{real}"):
        code, body = _get_raw(server, evil)
        assert code == 404, (
            f"{evil} 返回 {code} —— 穿越没挡住，读到了 assets 之外的文件")
        assert len(body) < 5000, f"{evil} 回吐了 {len(body)} 字节，像是把图发出去了"


def test_asset_serves_files_only_from_its_own_directory(server):
    """对照：同一张图放在 assets 里就该能取到。

    没有这条，上面那条穿越测试无法区分「守卫挡住了」和「文件压根不存在」。
    """
    import shutil
    src_dir = os.path.join(STATIC, "..", "_shots")
    if not os.path.isdir(src_dir):
        pytest.skip("没有可用的对照图片")
    cand = [f for f in sorted(os.listdir(src_dir))
            if os.path.splitext(f)[1].lower() == ".png"][:1]
    if not cand:
        pytest.skip("_shots 下没有 PNG")

    assets = os.path.join(STATIC, "assets")
    os.makedirs(assets, exist_ok=True)
    probe = "_probe_copy.png"
    dst = os.path.join(assets, probe)
    shutil.copy2(os.path.join(src_dir, cand[0]), dst)
    try:
        code, body = _get_raw(server, "/assets/" + probe)
        assert code == 200, f"assets 内的图取不到（{code}），对照无效"
        assert body.startswith(b"\x89PNG"), "取回的不是 PNG"
    finally:
        os.remove(dst)


def test_asset_rejects_non_image_extensions(server):
    """只放行图片后缀 —— 用**真实存在**的非图片文件测。

    踩过的坑：最初拿 `assets/x.txt` / `assets/config.json` 测，这些文件在
    assets 里压根不存在，「文件不存在」就返回 404，后缀白名单有没有都一样，
    断言是永真的。必须真的放一个非图片文件进去。
    """
    assets = os.path.join(STATIC, "assets")
    os.makedirs(assets, exist_ok=True)
    probe = "_probe_not_image.txt"
    dst = os.path.join(assets, probe)
    with open(dst, "w", encoding="utf-8") as f:
        f.write("this file exists but must never be served\n")
    try:
        code, body = _get_raw(server, "/assets/" + probe)
        assert code == 404, (
            f"{probe} 真实存在却被服务出来（{code}）—— 后缀白名单没起作用")
        assert b"never be served" not in body, "文件内容泄漏了"

        # 顺手确认白名单不是把一切都挡了：同目录的 PNG 必须能取
        code2, _ = _get_raw(server, "/assets/df-logo.png")
        assert code2 == 200, "对照失败：assets 里的合法图片也取不到"
    finally:
        os.remove(dst)


# ---------------------------------------------------------------- 记录

@pytest.fixture
def tmp_records(tmp_path):
    """把记录文件指到临时目录 —— 测试绝不能清掉用户真实的成交记录。"""
    from core import records
    old = records.PATH
    records.PATH = str(tmp_path / "records.jsonl")
    # 这个 fixture 自己起了 httpd，绕过了 serve()；生产里预热是启动的一部分
    # （`serve()` 在接请求之前同步 warm），这里补上同样的前置条件。
    records.warm(50)
    try:
        yield records
    finally:
        records.PATH = old


def test_records_endpoint_returns_rows_and_stats(server, tmp_records):
    tmp_records.add(name="MK4 疾光魅影", price=2050, result="hit", elapsed=1.28)
    tmp_records.add(name="AKM 群星", price=1888, result="miss", elapsed=8.0)
    code, body = _get(server + "/", "/api/records")
    assert code == 200
    data = json.loads(body)
    assert len(data["rows"]) == 2
    assert data["stats"]["hits"] == 1 and data["stats"]["tries"] == 2
    assert data["stats"]["rate"] == 50.0
    assert data["stats"]["spent"] == 2050.0


def test_records_clear_endpoint_empties_the_list(server, tmp_records):
    tmp_records.add(name="X", price=1, result="hit")
    code, body = _get(server + "/", "/api/records")
    assert len(json.loads(body)["rows"]) == 1

    req = urllib.request.Request(server + "/api/records/clear",
                                 data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    assert data["ok"] is True
    assert data["stats"]["tries"] == 0
    assert tmp_records.load() == []


def test_shot_route_rejects_traversal_and_other_files(server):
    """截图路由只发 data/shots 里的 .jpg。路径来自 HTTP，必须挡 `../`。"""
    # 第三个必须指向一个**真实存在**的 .jpg：只测不存在的路径，
    # 就算守卫被删掉也照样 404，测了个寂寞。
    for raw in ("/shots/../config.json", "/shots/..%2fconfig.json",
                "/shots/../../web/assets/df-bg.jpg",
                "/shots/x.txt", "/shots/nope.jpg"):
        code, _ = _get_raw(server, raw)
        assert code == 404, f"{raw} 返回 {code}，穿越或非图片文件被发出去了"


def test_shot_route_serves_a_chinese_filename(server, tmp_path, monkeypatch):
    """商品名带中文时，URL 里是 percent-encoded 的 —— 不 unquote 就永远 404。

    真机症状：记录页里「看图」点开是一张破图，服务端日志什么都没说。
    """
    import urllib.parse
    from core import server as srv_mod

    shots = tmp_path / "data" / "shots"
    shots.mkdir(parents=True)
    name = "20261022_022921_看图测试.jpg"
    (shots / name).write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 200)

    monkeypatch.setattr(srv_mod, "DATA_DIR", str(tmp_path / "data"))
    code, body = _get(server + "/", "/shots/" + urllib.parse.quote(name))
    assert code == 200, f"中文文件名取不到（{code}）"
    assert body.startswith(b"\xff\xd8\xff"), "发出来的不是 JPEG"


def test_advanced_settings_survive_a_round_trip(server, tmp_path, monkeypatch):
    """设置页「抢购高级」那四个数字必须真的落到 config.json。

    白名单漏一个键，前端就变成「点保存、提示已保存、其实什么都没存」——
    这种失败没有任何报错，只有下次开抢才发现还是老参数。
    """
    import core.config as cfgmod

    path = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(path))

    body = json.dumps({"arm_grace_s": 2.5, "list_max_tries": 7,
                       "threshold": 0.91, "fire_timeout_s": 12}).encode()
    req = urllib.request.Request(server + "/api/config", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert json.loads(r.read().decode("utf-8"))["ok"]

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["arm_grace_s"] == 2.5
    assert saved["list_max_tries"] == 7
    assert saved["threshold"] == 0.91
    assert saved["fire_timeout_s"] == 12


# ---------------------------------------------------------------- 任务清单

def _post(base, path, payload, timeout=10):
    import urllib.request
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_task_can_be_disabled_and_reenabled(server):
    """清单页的勾选框要能真正关掉一条任务。

    引擎跑之前会过滤 `enabled`，所以这个开关不是纯装饰 —— 关掉的必须
    真的被跳过。以前 /api/tasks/update 的白名单里没有 enabled，
    界面点了没反应（接口静默忽略）。
    """
    # 直接进队列（显式给框选区域）：不依赖本机 config.json 里有没有框过，
    # 这条测试只关心 /api/tasks/update 的白名单。
    from core import server as srv
    task, err = srv._scheduler.add(
        "测试皮肤-勾选开关", mode="list", min_price=100, max_price=200,
        regions={"list": {"x": 0, "y": 0, "w": 1, "h": 1},
                 "buy": {"x": 0.7, "y": 0.8, "w": 0.2, "h": 0.1}})
    assert task and not err, err
    tid = task["id"]

    _post(server, "/api/tasks/update", {"id": tid, "enabled": False})
    tasks = json.loads(urllib.request.urlopen(server + "/api/tasks", timeout=10).read())
    got = [t for t in tasks["tasks"] if t["id"] == tid][0]
    assert got["enabled"] is False, "关不掉"

    _post(server, "/api/tasks/update", {"id": tid, "enabled": True})
    tasks = json.loads(urllib.request.urlopen(server + "/api/tasks", timeout=10).read())
    got = [t for t in tasks["tasks"] if t["id"] == tid][0]
    assert got["enabled"] is True, "开不回来"

    _post(server, "/api/tasks/remove", {"id": tid})


# ---------------------------------------------------------------- 场景守卫

def test_guard_endpoint_exposes_state_and_doing(server):
    """前端要靠这个端点画「现在在做什么 / 唯一主按钮」，不能是空壳。"""
    code, body = _get(server, "/api/guard")
    assert code == 200
    data = json.loads(body)
    assert data["ok"] is True
    g = data["guard"]
    assert g["state"] in ("ok", "recovering", "waiting_human", "halted")
    assert g["level"] in (0, 1, 2, 3, 4, 5)
    assert g["doing"]
    assert "message" in g and "button" in g
    assert g["button"] is None or set(g["button"]) == {"id", "label"}


def test_guard_resume_endpoint_clears_pause(server):
    code, data = _post(server, "/api/guard/resume", {})
    assert code == 200 and data["ok"] is True
    assert data["guard"]["state"] == "ok"


def test_keepalive_connections_have_a_read_timeout():
    """浏览器切后台/崩溃后 keep-alive 连接不会永久占线程和句柄。

    HTTP/1.1 + 无超时 = 每条半开连接常驻 1 线程 + 7 句柄（R02-L02）；
    实测 20 条常驻 45s 不退。基类在 setup() 里用 Handler.timeout 设置
    socket 超时，所以这里锁住「必须有值且不能太长」。
    """
    assert 0 < Handler.timeout <= 60

