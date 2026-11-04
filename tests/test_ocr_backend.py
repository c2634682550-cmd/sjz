"""OCR 客户端（PaddleOCR PP-OCRv6）的回归测试。

原则和这个仓库其它测试一致：**不依赖真模型**。用本地假服务端（http.server）
顶替 PaddleX，只验四件事：

  1. 请求发对了（路径、字段、base64、关掉可视化图）
  2. 返回解析对了（rec_boxes / rec_polys / 各种缺字段的形状）
  3. 服务挂了的行为是「返回空」，不是抛异常、更不是乱点
  4. 缓存与熔断：同一张图不重复请求，连续失败会停用

真模型的准确率和延迟对比在 tools/ocr_eval.py —— 那个要 GPU 和模型，
不适合放进 pytest。

跑法：.venv\\Scripts\\python.exe -m pytest tests/test_ocr_backend.py -v
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, ocr, ocr_ppocr  # noqa: E402

# /ocr 的真实返回形状（照 PaddleX OCR serving 抄的，只留用到的字段）
OCR_OK = {
    "errorCode": 0,
    "errorMsg": "Success",
    "result": {
        "ocrResults": [{
            "prunedResult": {
                "rec_texts": ["QBZ95-1突击步枪-王牌之剑", "极品|成色A", "25,000"],
                "rec_scores": [0.99, 0.97, 0.98],
                "rec_boxes": [[3, 0, 230, 23], [0, 176, 95, 197], [598, 176, 683, 197]],
                "rec_polys": [[[3, 0], [230, 2], [230, 23], [3, 21]],
                              [[0, 176], [95, 177], [95, 197], [0, 196]],
                              [[598, 176], [683, 177], [683, 197], [598, 196]]],
            },
            "ocrImage": None,
        }],
        "dataInfo": {"type": "image", "width": 1380, "height": 630},
    },
}


class _Fake:
    """假 PaddleX OCR 服务端。payload / 延迟 / 状态码都可调，并记请求。"""

    def __init__(self):
        self.payload = OCR_OK
        self.status = 200
        self.delay = 0.0
        self.hits = 0
        self.paths = []
        self.bodies = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):                      # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.hits += 1
                outer.paths.append(self.path)
                outer.bodies.append(body)
                if outer.delay:
                    time.sleep(outer.delay)
                raw = json.dumps(outer.payload).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):              # 别往测试输出里刷日志
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    f = _Fake()
    yield f
    f.close()


@pytest.fixture
def use_backend(monkeypatch):
    """把 ocr 的配置临时换掉，用例结束自动还原。"""
    def _apply(url="http://127.0.0.1:9", **pp):
        s = {"ppocr": {**config.DEFAULTS["ocr"]["ppocr"], "url": url, **pp}}
        monkeypatch.setattr(ocr, "_settings", s, raising=False)
        monkeypatch.setattr(ocr, "_backend", None, raising=False)
        monkeypatch.setattr(ocr, "_last_err", "", raising=False)
        monkeypatch.setattr(ocr, "_stats", {"calls": 0, "empty": 0}, raising=False)
        return s
    return _apply


def _img(w=1380, h=630):
    """纯色假图：假服务端不看内容，只验链路。"""
    return np.full((h, w, 3), 30, dtype=np.uint8)


# ---------------------------------------------------------------- 配置

def test_defaults_only_have_ppocr():
    """只剩一个后端 —— 配置里不该再有 backend / paddle_vl / hot_backend。

    service 是服务管家（定时重启）的配置，不是第二个后端。
    """
    d = config.DEFAULTS["ocr"]
    assert set(d) == {"ppocr", "service"}
    assert d["ppocr"]["model"].startswith("PP-OCRv6")
    assert d["ppocr"]["url"].startswith("http")


def test_config_merge_keeps_untouched_ppocr_keys():
    """用户只改 url 时，其它项要保住默认值（整体替换会把端口改回默认）。"""
    c = config.Config({"ocr": {"ppocr": {"url": "http://127.0.0.1:9999"}}})
    assert c["ocr"]["ppocr"]["url"] == "http://127.0.0.1:9999"
    assert c["ocr"]["ppocr"]["timeout_s"] == config.DEFAULTS["ocr"]["ppocr"]["timeout_s"]


# ---------------------------------------------------------------- 返回解析

def test_parse_maps_boxes_to_xywh():
    """rec_boxes 是 [x1,y1,x2,y2]，要换成 [x,y,w,h]。错一位就点到别处。"""
    lines = ocr_ppocr._parse(OCR_OK, (1380, 630), (1380, 630))
    assert [l["text"] for l in lines] == [
        "QBZ95-1突击步枪-王牌之剑", "极品|成色A", "25,000"]
    assert (lines[0]["x"], lines[0]["y"], lines[0]["w"], lines[0]["h"]) == (3, 0, 227, 23)


def test_parse_falls_back_to_polys():
    """服务端只给 rec_polys 时也要能算出包围盒。"""
    payload = {"result": {"ocrResults": [{"prunedResult": {
        "rec_texts": ["25,000"], "rec_scores": [0.9],
        "rec_polys": [[[598, 176], [683, 177], [683, 197], [598, 196]]]}}]}}
    lines = ocr_ppocr._parse(payload, (1380, 630), (1380, 630))
    assert (lines[0]["x"], lines[0]["y"], lines[0]["w"], lines[0]["h"]) == (598, 176, 85, 21)


def test_parse_tolerates_unknown_shape():
    """服务端版本一换字段名就可能变 —— 解析不出就返回空，不许抛。"""
    assert ocr_ppocr._parse({"foo": [{"bar": 1}]}, (10, 10), (10, 10)) == []
    assert ocr_ppocr._parse({}, (10, 10), (10, 10)) == []
    assert ocr_ppocr._parse({"result": {"ocrResults": []}}, (10, 10), (10, 10)) == []


def test_parse_rescales_when_image_was_downscaled():
    """发给服务端前缩过图（max_side），坐标要按比例放大回原图。

    这条错了的症状很隐蔽：字都读对，但点购买点到别处去。
    """
    payload = {"result": {"ocrResults": [{"prunedResult": {
        "rec_texts": ["a"], "rec_scores": [1.0],
        "rec_boxes": [[100, 50, 200, 70]]}}]}}
    lines = ocr_ppocr._parse(payload, (1000, 500), (500, 250))
    assert (lines[0]["x"], lines[0]["y"], lines[0]["w"], lines[0]["h"]) == (200, 100, 200, 40)


# ---------------------------------------------------------------- HTTP 链路

def test_read_words_over_http(fake, use_backend):
    use_backend(url=fake.url, timeout_s=3)
    words = ocr.read_words(_img())
    assert [w["text"] for w in words] == [
        "QBZ95-1突击步枪-王牌之剑", "极品|成色A", "25,000"]
    assert all({"x", "y", "w", "h"} <= set(w) for w in words)
    assert fake.paths == ["/ocr"]
    body = json.loads(fake.bodies[0])
    assert body["fileType"] == 1 and len(body["file"]) > 100
    # 可视化图必须关掉：不关的话每张图多传 600KB 无用数据
    assert body["visualize"] is False


def test_read_text_over_http(fake, use_backend):
    use_backend(url=fake.url, timeout_s=3)
    text = ocr.read_text(_img())
    assert "王牌之剑" in text and "25,000" in text


def test_cache_serves_repeated_frames_without_hitting_server(fake, use_backend):
    """同一张图第二次不该再请求 —— 页面识别每秒会看同一张图好几次。"""
    use_backend(url=fake.url, timeout_s=3)
    img = _img()
    ocr.read_words(img)
    ocr.read_words(img)
    assert fake.hits == 1


def test_min_score_filters_low_confidence_lines(fake, use_backend):
    """低置信度的碎块多半是图标/描边，留着只会污染名称匹配。"""
    fake.payload = {"result": {"ocrResults": [{"prunedResult": {
        "rec_texts": ["真文字", "噪声"], "rec_scores": [0.95, 0.05],
        "rec_boxes": [[0, 0, 50, 20], [0, 30, 10, 40]]}}]}}
    use_backend(url=fake.url, timeout_s=3, min_score=0.35)
    assert [w["text"] for w in ocr.read_words(_img())] == ["真文字"]


# ---------------------------------------------------------------- 失败行为

def test_server_error_returns_empty_not_raise(fake, use_backend):
    """服务挂了返回空 —— 让引擎走它已有的失败分支，别在这里抛。"""
    fake.status = 500
    use_backend(url=fake.url, timeout_s=1, cooldown_s=0.01)
    assert ocr.read_text(_img()) == ""
    assert ocr.read_words(_img()) == []
    assert ocr.stats()["empty"] >= 1


def test_timeout_returns_empty(fake, use_backend):
    fake.delay = 1.5
    use_backend(url=fake.url, timeout_s=0.3, cooldown_s=0.01)
    assert ocr.read_text(_img()) == ""


def test_unavailable_when_service_down(use_backend):
    """服务没起时自检必须说实话，启动前检查才能拦住。"""
    use_backend(url="http://127.0.0.1:9", timeout_s=0.3)
    assert ocr.available() is False
    assert "不可用" in ocr.backend_name()


def test_repeated_failures_disable_client_for_a_long_time(fake, use_backend):
    """连续失败说明不是偶发抖动，停用它，别让每次调用都白等一个 timeout。

    每次之间要等短冷却过去：冷却期里识别会直接短路返回空（见下面那条
    用例），不等的话三次里只有第一次是真尝试，永远到不了 fail_limit。
    """
    fake.status = 500
    use_backend(url=fake.url, timeout_s=1, cooldown_s=0.01,
                fail_limit=3, long_cooldown_s=600)
    for _ in range(3):
        ocr.read_text(_img(120, 32))
        time.sleep(0.02)
    b = ocr._client()
    assert b.available() is False
    assert "长时间停用" in (b.last_error or "")


def test_backend_name_reports_measurement_after_calls(fake, use_backend):
    """自检要带实测耗时 —— 一眼看出是不是慢到不该用。"""
    use_backend(url=fake.url, timeout_s=3)
    ocr.read_text(_img())
    assert "实测" in ocr.backend_name()


def test_cooldown_lets_go_once_the_service_recovers(fake, use_backend):
    """服务重启好了，客户端必须自己爬出冷却期。

    以前 available() 先看 _fail_until 再探活：连续 3 次失败把冷却推到 600 秒，
    服务明明已经恢复，界面还连续 10 分钟报「OCR 不可用」，用户只能重启控制台。
    """
    fake.status = 500
    use_backend(url=fake.url, timeout_s=1, cooldown_s=0.01,
                fail_limit=3, long_cooldown_s=600, cooldown_probe_s=0)
    for _ in range(3):
        ocr.read_text(_img(120, 32))
    b = ocr._client()
    assert b.available() is False, "服务还在 500，不能判成可用"

    fake.status = 200                       # 服务重启好了
    assert b.available() is True, "服务恢复了却还在冷却期里装死"
    assert b.available() is True


def test_cooldown_stops_sending_requests_entirely(fake, use_backend):
    """进了冷却期就别再发请求了 —— 服务卡死时每次都要白等一个 timeout。

    以前只在 available() 里拦，识别这条路照样一次次撞 5 秒超时：引擎一轮
    扫十几张图就是一分多钟，用户看到的是「点了没反应」。
    """
    fake.status = 500
    use_backend(url=fake.url, timeout_s=1, cooldown_s=0.01,
                fail_limit=3, long_cooldown_s=600)
    for _ in range(3):
        ocr.read_text(_img(120, 32))
    b = ocr._client()
    assert b.available() is False
    before = fake.hits
    assert ocr.read_text(_img(120, 32)) == ""
    assert ocr.read_words(_img(120, 32)) == []
    assert fake.hits == before, "冷却期还在往服务发请求"
