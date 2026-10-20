"""server.py 的回归测试：MJPEG 流必须真的发数据。

本地回归的是一条**很难发现的原有缺陷**：`_stream()` 是无限循环、响应永不
结束，而 `BaseHTTPRequestHandler.wfile` 带缓冲 —— 不 flush 的话第一帧永远
卡在缓冲区里，浏览器一个字节都收不到，直接把 `<img>` 判成 broken，
表现为预览区全黑。

源码模式下这个 bug 也在，只是当时没人去点"预览到底有没有画面"。
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


def _read_stream(base, seconds=2.0, timeout=8.0):
    """读一段时间流，返回 (字节数, 帧数)。"""
    total = frames = 0
    end = time.time() + seconds
    req = urllib.request.Request(base + "/api/stream")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        assert r.headers.get("Content-Type", "").startswith("multipart/x-mixed-replace"), \
            r.headers.get("Content-Type")
        while time.time() < end:
            chunk = r.read(65536)
            if not chunk:
                break
            total += len(chunk)
            frames += chunk.count(b"\xff\xd8")
    return total, frames


def test_stream_flushes_every_frame(server):
    """每帧之后必须 flush。

    为什么这条测试要单独存在：源码模式下单帧 93KB 会撞满 8KB 缓冲自动刷出，
    所以"流有没有数据"在源码模式下**测不出** flush 缺失。只有在 PyInstaller
    打包后的 exe 里，wfile 不自动刷，去掉 flush 就是 0 字节（实测：有 flush
    327,680 字节 / 无 flush 超时）。

    直接对着源码断言，比依赖运行时行为可靠 —— 这是**结构性检查**：
    _stream 里出现 wfile.write 就必须有 wfile.flush。
    """
    import inspect
    from core import server as srv
    src = inspect.getsource(srv.Handler._stream)
    assert "self.wfile.write" in src, "取不到 _stream 源码"
    assert "self.wfile.flush()" in src, (
        "_stream 漏了 self.wfile.flush()。打包后的 exe 里这会让预览区全黑 —— "
        "客户端收不到任何字节，浏览器把 <img> 判成 broken。")


def test_dropping_flush_would_break_the_stream(server):
    """反证：把 flush 从源码里拿掉，上面的断言必须失败。

    防止 test_stream_flushes_every_frame 变成永真的空断言。
    """
    import inspect
    from core import server as srv
    src = inspect.getsource(srv.Handler._stream)
    mutated = src.replace("self.wfile.flush()", "pass")
    assert mutated != src, "没找到 flush，测试本身失效了"
    assert "self.wfile.flush()" not in mutated, "替换没生效"


def test_stream_actually_sends_frames(server):
    """流的字节数必须真的到客户端（基本连通性）。"""
    total, frames = _read_stream(server, seconds=2.0)
    assert total > 0, "一个字节都没收到"
    assert frames >= 3, f"2 秒才 {frames} 帧，太少了（应约 10 帧）"


def test_stream_frame_is_valid_jpeg(server):
    """帧头是 FFD8、帧尾是 FFD9 —— 不然浏览器解不出图。"""
    req = urllib.request.Request(server + "/api/stream")
    with urllib.request.urlopen(req, timeout=8) as r:
        buf = b""
        end = time.time() + 3
        while len(buf) < 200_000 and time.time() < end:
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
    assert b"\xff\xd8" in buf, "没有 JPEG 帧头"
    assert b"\xff\xd9" in buf, "没有 JPEG 帧尾，帧可能被截断"
    assert b"Content-Length:" in buf, "缺 Content-Length，浏览器无法切分帧"


def test_config_endpoint_serves_json(server):
    """流不影响别的端点 —— 拒绝在预览开着时也能读配置。"""
    with urllib.request.urlopen(server + "/api/config", timeout=10) as r:
        assert r.status == 200
        cfg = json.loads(r.read().decode("utf-8"))
    assert "regions" in cfg and "threshold" in cfg


def test_stream_does_not_block_other_endpoints(server):
    """一边挂着流，一边请求配置必须还能通。

    ThreadingHTTPServer 保证这点；如果用单线程 Server 会直接卡死。
    """
    stop = threading.Event()
    got = {}

    def reader():
        try:
            req = urllib.request.Request(server + "/api/stream")
            with urllib.request.urlopen(req, timeout=8) as r:
                while not stop.is_set():
                    if not r.read(65536):
                        break
        except Exception:                    # 关闭时的正常中断
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.0)
    try:
        with urllib.request.urlopen(server + "/api/screen", timeout=10) as r:
            got["status"] = r.status
    finally:
        stop.set()
        t.join(timeout=3)
    assert got.get("status") == 200


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
