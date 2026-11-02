"""PP-OCRv5 后端（PaddleX OCR 服务）。

为什么是它
----------
2026-11-01 用 `data/shots` 的真截图做了横向评测（tools/ocr_eval.py），
同一批 6 张卡片的 42 个字段（商品名 / 成色 / 价格 × 1x2x3x 放大）：

    引擎                          字段准确  整屏 1380×630  小裁切
    Windows OCR（原来的）          16/42      66ms         3ms
    PaddleOCR-VL-1.6（上一版接的）   漏块      161s(CPU)    27s(CPU)
    RapidOCR v6 small             31/42      264ms        264ms
    RapidOCR v5 server            40/42     2159ms          —
    PaddleOCR PP-OCRv5 server     42/42      192ms         24ms
    PaddleOCR PP-OCRv5 mobile     42/42       95ms         13ms   ← 最优

结论：PP-OCRv5 是唯一「准确率满分且延迟可接受」的。VL 那个 0.9B 模型
在游戏界面上会整块漏检，RapidOCR 的 onnx 管线在同精度下慢一个数量级。

为什么走 HTTP 而不是进程内
--------------------------
paddle 装进来 exe 会从几十 MB 涨到 1.5GB，PyInstaller 对它的静态分析也
基本不可用。所以和 VL 一样：本机另起一个 PaddleX OCR 服务，这里只做客户端。

    paddlex --serve --pipeline OCR --port 8081 --device gpu:0

协议（实测）
  POST /ocr   {"file": <base64 图片>, "fileType": 1}
  → {"result": {"ocrResults": [{"prunedResult": {
        "rec_texts": [...], "rec_scores": [...],
        "rec_boxes": [[x1,y1,x2,y2], ...], "rec_polys": [...]}}]}}

坐标是**输入图**的像素，和 winrt 的 bounding_rect 同一口径。
"""
import base64
import hashlib
import json
import socket
import statistics
import threading
import time
import urllib.request
from collections import OrderedDict, deque
from urllib.parse import urlparse

NAME = "ppocr"

DEFAULTS = {
    "mode": "auto",                       # auto / http / off
    "url": "http://127.0.0.1:8081",       # paddlex --serve --pipeline OCR 的地址
    "timeout_s": 5.0,
    "probe_ttl_s": 10.0,
    "cooldown_s": 30.0,
    "fail_limit": 3,
    "long_cooldown_s": 600.0,
    "cache": 64,
    "max_side": 2560,
    "min_score": 0.35,                    # 低于这个置信度的识别结果丢掉
    "model": "PP-OCRv6 small",            # 只用于自检显示，不影响请求
}


def _digest(raw):
    return hashlib.md5(raw).hexdigest()


class _Cache:
    """按图片字节的 LRU —— 页面识别会反复看同一张图。"""

    def __init__(self, size):
        self.size = max(0, int(size or 0))
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        if not self.size:
            return None
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, val):
        if not self.size:
            return
        with self._lock:
            self._d[key] = val
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)


class Backend:
    """PaddleX OCR 服务客户端。对外是 available / name / read_words / read_text。"""

    supports_words = True

    def __init__(self, settings=None):
        self.cfg = dict(DEFAULTS)
        if settings:
            self.cfg.update({k: v for k, v in settings.items() if v is not None})
        self.mode = str(self.cfg.get("mode") or "auto").lower()
        self.url = str(self.cfg.get("url") or "").rstrip("/")
        self.timeout = float(self.cfg.get("timeout_s") or 5.0)
        self._cache = _Cache(self.cfg.get("cache"))
        self._lock = threading.Lock()
        self._probe_at = 0.0
        self._probe_ok = False
        self._fail_until = 0.0
        self._fails_in_row = 0
        self.last_error = None
        self.stats = {"calls": 0, "hits": 0, "ms": 0.0, "fails": 0, "lines": 0}
        # 最近 20 次真推理的耗时（毫秒）。只看累计平均发现不了「最近变慢」——
        # 跑了几万次之后，平均被历史数据摊平，退化要很久才浮出来。
        self.recent = deque(maxlen=20)

    # ---------- 探活 ----------

    def _host_port(self):
        u = urlparse(self.url if "://" in self.url else "http://" + self.url)
        return (u.hostname or "127.0.0.1"), (u.port or 80)

    def probe(self, force=False):
        now = time.monotonic()
        if not force and now - self._probe_at < float(self.cfg.get("probe_ttl_s") or 10):
            return self._probe_ok
        self._probe_at = now
        host, port = self._host_port()
        try:
            # 0.4s 太紧：显卡被游戏占满时，本机连一下都可能超 400ms，
            # 于是「服务明明活着却判成不可用」，整条 OCR 链路白给。
            with socket.create_connection((host, port), timeout=1.5):
                self._probe_ok = True
        except OSError:
            self._probe_ok = False
        return self._probe_ok

    def available(self):
        if self.mode in ("off", "none"):
            return False
        if time.monotonic() < self._fail_until:
            return False
        return self.probe()

    def name(self):
        if self.mode in ("off", "none"):
            return "关闭（ocr.ppocr.mode=off）"
        if not self.available():
            return (f"不可用（{self.url} 连不上）。控制台启动时会自动拉起；"
                    "一直起不来看 ocr_service_*.log")
        return f"PaddleOCR {self.cfg.get('model') or 'PP-OCR'}（HTTP {self.url}/ocr）"

    # ---------- 识别 ----------

    def _post(self, img):
        import cv2
        h, w = img.shape[:2]
        limit = int(self.cfg.get("max_side") or 0)
        sent = (w, h)
        if limit and max(h, w) > limit:
            f = limit / float(max(h, w))
            img = cv2.resize(img, (int(w * f), int(h * f)),
                             interpolation=cv2.INTER_AREA)
            sent = img.shape[1::-1]
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            raise RuntimeError("PNG 编码失败")
        payload = {"file": base64.b64encode(buf.tobytes()).decode("ascii"),
                   "fileType": 1,
                   # 不加这个，服务端会把原图 + 预处理图 + 可视化图三张 base64
                   # 塞进响应（实测 633KB，其中只有 3KB 有用），整屏一次多花
                   # 100ms 在传输和 JSON 序列化上。
                   "visualize": False}
        req = urllib.request.Request(
            f"{self.url}/ocr", data=json.dumps(payload).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        code = resp.get("errorCode", 0)
        if code not in (0, None):
            raise RuntimeError(f"服务端报错 {code}: {resp.get('errorMsg') or resp}")
        return _parse(resp, (w, h), sent)

    def _lines(self, img):
        if img is None or getattr(img, "size", 0) == 0:
            return []
        import cv2
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return []
        key = _digest(buf.tobytes())
        hit = self._cache.get(key)
        if hit is not None:
            self.stats["hits"] += 1
            return hit
        t0 = time.perf_counter()
        with self._lock:
            try:
                lines = self._post(img)
                floor = float(self.cfg.get("min_score") or 0)
                if floor:
                    # 低置信度的碎块多半是图标/描边，留着只会污染名称匹配
                    lines = [l for l in lines if l.get("score", 1.0) >= floor]
            except Exception as e:          # noqa: BLE001 - 失败必须能降级
                self.stats["fails"] += 1
                self._fails_in_row += 1
                self.last_error = f"{type(e).__name__}: {e}"
                limit = int(self.cfg.get("fail_limit") or 0)
                if limit and self._fails_in_row >= limit:
                    self._fail_until = time.monotonic() + float(
                        self.cfg.get("long_cooldown_s") or 600)
                    self.last_error += (f"（连续 {self._fails_in_row} 次失败，"
                                        "本会话长时间停用 PP-OCR）")
                else:
                    self._fail_until = time.monotonic() + float(
                        self.cfg.get("cooldown_s") or 30)
                raise
        self._fails_in_row = 0
        self.recent.append((time.perf_counter() - t0) * 1000.0)
        self.stats["calls"] += 1
        self.stats["ms"] += (time.perf_counter() - t0) * 1000.0
        self.stats["lines"] += len(lines)
        self._cache.put(key, lines)
        return lines

    def read_words(self, img):
        """[{"x","y","w","h","text"}]，坐标是输入图像素。"""
        return [dict(x=l["x"], y=l["y"], w=l["w"], h=l["h"], text=l["text"])
                for l in self._lines(img)]

    def read_text(self, img):
        return "\n".join(l["text"] for l in self._lines(img))

    def recent_ms(self):
        """最近 20 次识别的中位耗时（毫秒）。没有样本返回 0。"""
        if not self.recent:
            return 0.0
        return float(statistics.median(self.recent))

    def stats_line(self):
        s = self.stats
        avg = (s["ms"] / s["calls"]) if s["calls"] else 0.0
        return (f"ppocr calls={s['calls']} hits={s['hits']} fails={s['fails']} "
                f"avg={avg:.0f}ms lines={s['lines']}")


def _parse(resp, orig_size, sent_size):
    """把 ocrResults[].prunedResult 摊平成 [{x,y,w,h,text,score}]。"""
    result = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(result, dict):
        return []
    items = result.get("ocrResults") or []
    out = []
    for item in items:
        pr = (item or {}).get("prunedResult") or item or {}
        texts = pr.get("rec_texts") or []
        boxes = pr.get("rec_boxes") or []
        polys = pr.get("rec_polys") or []
        scores = pr.get("rec_scores") or []
        for i, t in enumerate(texts):
            t = (t or "").strip()
            if not t:
                continue
            box = boxes[i] if i < len(boxes) else None
            if box is None and i < len(polys):
                poly = polys[i]
                xs = [p[0] for p in poly]
                ys = [p[1] for p in poly]
                box = [min(xs), min(ys), max(xs), max(ys)]
            if not box or len(box) < 4:
                continue
            x1, y1, x2, y2 = (float(v) for v in box[:4])
            if x2 < x1 or y2 < y1:
                x1, y1, x2, y2 = x1, y1, x1 + x2, y1 + y2
            score = float(scores[i]) if i < len(scores) else 1.0
            out.append({"x": int(round(x1)), "y": int(round(y1)),
                        "w": max(1, int(round(x2 - x1))),
                        "h": max(1, int(round(y2 - y1))),
                        "text": t, "score": score})
    return _fit(out, sent_size, orig_size)


def _fit(lines, sent_size, src_size):
    """坐标拉回原图（发送前可能按 max_side 缩过）。"""
    if not lines:
        return []
    sw, sh = sent_size
    ow, oh = src_size
    if (sw, sh) == (ow, oh):
        return lines
    fx = ow / float(sw) if sw else 1.0
    fy = oh / float(sh) if sh else 1.0
    for l in lines:
        l["x"] = int(round(l["x"] * fx))
        l["y"] = int(round(l["y"] * fy))
        l["w"] = max(1, int(round(l["w"] * fx)))
        l["h"] = max(1, int(round(l["h"] * fy)))
    return lines


def make(settings=None):
    return Backend(settings)
