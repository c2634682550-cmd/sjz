"""RapidOCR 主后端：进程内 ONNXRuntime 跑 PP-OCRv6 small，不依赖 paddle。

为什么把它放主路径
------------------
2026-11-06 用 20 条主基准 + 21 条真实截图 ROI 做了横向评测
（见仓库根 `_ocr_engine_selection.md` / `_ocr_bench_user_shots.md`）：

    RapidOCR（v6 small ONNX）  主基准 36/36，真实 ROI 42/42，0 误识 0 拒识
    PP-OCRv6 tiny（paddle）    36/36 / 42/42，但冷启动 4.3s、依赖 4.3GB
    自建字模                   32/36 / 24~28/42，噪声块会编数字

它一次 pip 装完（rapidocr + onnxruntime），模型 30MB 随仓库走，冷启动 1.2s，
ROI 12~70ms —— 这是「Windows + 无 GPU + 必须离线」约束下唯一同时满足
准确率、体积、冷启动三项的。

三个参数不能删（删了就会退回「慢且漏检」）
------------------------------------------
下面 `_PARAMS` 里这三项是实测定出来的，不是抄默认值：

  1. `Global.use_preprocess_img = False`
     RapidOCR 3.x 默认 True：会先把小 ROI 拉到 max_side 再检测。实测在
     12~30px 高的价格行上整块漏检（20 条基准只出 18 条），单帧从 70ms
     涨到 1.2s。关掉后与 PaddleOCR 产线参数一致。
  2. `Det.limit_side_len = 64` + `Det.limit_type = "min"`
     默认 736/`max`：小图会被放大 10 倍以上再送检测，耗时爆炸且细笔画
     被插值糊掉；64/min 只在短边不足 64 时才补，价格行原分辨率进网络。
  3. `Det.box_thresh = 0.5`
     默认 0.6：对单个数字（货币栏的 `0`、`8`）框置信度常在 0.5~0.6，
     用 0.6 会把这些单字整行丢掉。0.5 与 det_thresh(0.3) 配合是实测最稳的。
"""
import os
import statistics
import threading
import time
from collections import deque

import cv2

_HERE = os.path.dirname(os.path.abspath(__file__))
# 随仓库分发的离线模型（不含绝对路径；换机器/换目录都能找到）
MODEL_DIR = os.path.join(_HERE, "assets", "rapid")
DET_NAME = "PP-OCRv6_det_small.onnx"
REC_NAME = "PP-OCRv6_rec_small.onnx"

NAME = "rapidocr"

_PARAMS = {
    # 见模块 docstring：这三组参数是「小 ROI 上的价格行」实测出来的唯一可用组合
    "Global.use_preprocess_img": False,
    "Global.use_cls": False,          # 价格行都是水平单行，方向分类纯浪费 3~5ms
    "Global.log_level": "ERROR",      # 启动那三行 INFO（用了哪个模型）留给 debug
    "Det.limit_side_len": 64,
    "Det.limit_type": "min",
    "Det.thresh": 0.3,
    "Det.box_thresh": 0.5,
    "Det.unclip_ratio": 1.6,
}


def _model_paths():
    """本地模型路径；缺哪个就返回 None（交给 RapidOCR 自带的包内模型/下载）。"""
    det = os.path.join(MODEL_DIR, DET_NAME)
    rec = os.path.join(MODEL_DIR, REC_NAME)
    return (det if os.path.isfile(det) else None,
            rec if os.path.isfile(rec) else None)


class Backend:
    """对外与 ocr_ppocr.Backend 同形：available / name / read_words / read_text。"""

    supports_words = True

    def __init__(self, settings=None):
        self.cfg = dict(settings or {})
        # 只留一个总开关：排障时想退回「纯 PP-OCR 服务」路径，把 ocr.rapid.enabled
        # 设成 false 即可，不用改代码。
        self.enabled = bool(self.cfg.get("enabled", True))
        self.stats = {"calls": 0, "hits": 0, "ms": 0.0, "fails": 0, "lines": 0}
        self.recent = deque(maxlen=20)
        self.last_error = None
        self._lock = threading.Lock()
        self._eng = None
        self._broken = False

    # ---------- 构建 / 自检 ----------

    def _engine(self):
        if self._eng is not None:
            return self._eng
        if self._broken:
            return None
        with self._lock:
            if self._eng is not None:
                return self._eng
            if self._broken:
                return None
            try:
                from rapidocr import RapidOCR
                params = dict(_PARAMS)
                det, rec = _model_paths()
                if det:
                    params["Det.model_path"] = det
                if rec:
                    params["Rec.model_path"] = rec
                self._eng = RapidOCR(params=params)
            except Exception as e:            # noqa: BLE001
                self._broken = True
                self.last_error = f"{type(e).__name__}: {e}"
                return None
        return self._eng

    def available(self):
        return self.enabled and self._engine() is not None

    def name(self):
        det, rec = _model_paths()
        where = "本地模型" if (det and rec) else "包内模型（缺 core/assets/rapid 时自动回退）"
        return f"RapidOCR PP-OCRv6 small（进程内 ONNX，{where}）"

    # ---------- 识别 ----------

    def read_words(self, img, timeout=None):
        """[{"x","y","w","h","text","score"}]，坐标是输入图真实像素。"""
        if img is None or getattr(img, "size", 0) == 0:
            return []
        if not self.enabled:
            raise RuntimeError("RapidOCR 已被 ocr.rapid.enabled=false 关闭")
        eng = self._engine()
        if eng is None:
            raise RuntimeError(f"RapidOCR 不可用：{self.last_error or '模型未加载'}")
        scale = self._det_scale(img)
        t0 = time.perf_counter()
        with self._lock:
            try:
                out = None
                if scale < 1.0:
                    out = _read_scaled(eng, img, scale)
                    if out is None or (not out and not _is_strip(img)):
                        # 快速路径出错、或整屏一个词都没出来 → 整幅重读一次，
                        # 绝不把「漏检」当成「这一帧没字」（review V08 口径）。
                        # 窄条（页码框）例外：0.4 倍后字仍有 28px，det 报空
                        # 就是真的没字；此时重读只会多付一次 450ms。
                        self.stats["fast_fallback"] = \
                            self.stats.get("fast_fallback", 0) + 1
                        out = None
                if out is None:
                    out = _words(eng(img))
            except Exception as e:            # noqa: BLE001
                self.stats["fails"] += 1
                self.last_error = f"{type(e).__name__}: {e}"
                raise
        ms = (time.perf_counter() - t0) * 1000.0
        self.recent.append(ms)
        self.stats["calls"] += 1
        self.stats["ms"] += ms
        self.stats["lines"] += len(out)
        if scale < 1.0:
            self.stats["fast"] = self.stats.get("fast", 0) + 1
        return out

    def _det_scale(self, img):
        """整幅 det 的降采样倍率。

        为什么可以降：det 的耗时随像素面积线性涨，而 rec 会先把每条框归一化
        到固定高度 —— 决定「认不认得出」的是 rec，不是 det 的原始分辨率。
        两种几何各自实测过（见 `perf/bench/roi_loss_probe.py`）：

          · 被放大 6 倍的页码条（3240×360）：原始字已经有 70px 高，压到
            长边 1300 后仍是 28px，rec 照样认得出；6 倍只是为了让 det 看见，
            没必要按 3240 宽跑。实测 452ms → 167ms，文字逐字一致，默认开。
          · 整屏 1920×1080：0.75 倍在 5 张真机 ROI 语料上逐字一致，但在
            文字密集的桌面整屏上会丢 12~13px 小字（1330 → 1272 字）。页面
            锚点都是大字，可「公示期」正好压在临界线上 —— 按识别率优先，
            整屏默认不降（1.0），留配置项等真机验证后再开。

        小 ROI（价格行、按钮、列表条）一律 1.0 —— 价格识别不能冒任何风险。
        """
        if not bool(self.cfg.get("fast_det", True)):
            return 1.0
        h, w = img.shape[:2]
        if w >= 2400 and h <= 600:
            # 6× 放大的页码条（3240×360）：原始字已有 70px 高，压到长边 1300
            # 后仍是 28px，rec 照样认得出，实测 452ms → 167ms，文字逐字一致。
            return max(0.35, 1300.0 / float(w))
        if 1600 <= w <= 2600 and 900 <= h <= 1500:
            # 整屏。**默认关闭**（1.0）：实测 1920×1080 在 0.75/0.8 倍会丢
            # 一批 12~13px 小字（桌面文字密集的帧 1330 → 1272 字），虽然
            # 页面判定的锚点都是大字、但「公示期」这类 14px 标签正好在
            # 临界线上，识别率优先，所以默认不动；真机验证通过后再开。
            # 上限 2600 是为了避开 `_scan_grid` 的 2× 整屏（3840×2160）——
            # 那条是价格兜底路径，不能在这里降采样。
            return float(self.cfg.get("fast_det_screen", 1.0))
        if 600 <= w <= 1100 and h <= 1700:
            # 中等 ROI（皮肤列表 750×1560、2× 放大的面板）。默认 1.0：
            # 单次约 300ms，降 0.75 只省 ~60ms/链（<1% e2e），而这批 ROI
            # 里是皮肤名，读错=点错卡片。默认关，只留旋钮。
            return float(self.cfg.get("fast_det_roi", 1.0))
        return 1.0

    def read_text(self, img, timeout=None):
        return "\n".join(w["text"] for w in self.read_words(img, timeout=timeout))

    def recent_ms(self):
        if not self.recent:
            return 0.0
        return float(statistics.median(self.recent))

    def stats_line(self):
        s = self.stats
        avg = (s["ms"] / s["calls"]) if s["calls"] else 0.0
        return (f"rapid calls={s['calls']} fails={s['fails']} "
                f"avg={avg:.0f}ms lines={s['lines']}")


def _is_strip(img):
    """被过度放大的窄条（页码框）。见 `Backend._det_scale` 的几何约定。"""
    h, w = img.shape[:2]
    return w >= 2400 and h <= 600


def _read_scaled(eng, img, scale):
    """降采样 det + 原图 rec。

    步骤：小图跑 det → 框按 scale 还原到原图 → 按框裁**原像素** → 一次批量 rec。
    框裁成轴对齐矩形（原实现用四点透视裁）：这两类输入都是水平排版的
    界面文字，倾斜最多 1~2°，轴对齐裁 + 2px 外扩对 rec 没有差别，
    还省掉了透视变换。
    """
    ach = img
    small = cv2.resize(ach, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA)
    # `text_det` 是懒加载属性，第一次 `eng(img)` 之前是 None；直接走
    # `_load_det_model()`（RapidOCR 自己的加载器，带锁）更稳。
    try:
        loader = getattr(eng, "_load_det_model", None)
        det_model = loader() if callable(loader) else getattr(eng, "text_det", None)
        if det_model is None:
            return None
        det = det_model(small)
    except Exception:                            # noqa: BLE001
        return None
    boxes = getattr(det, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []
    h, w = ach.shape[:2]
    crops, rects = [], []
    for pts in boxes:
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        x0 = max(0, int(min(xs) / scale) - 2)
        y0 = max(0, int(min(ys) / scale) - 2)
        x1 = min(w, int(max(xs) / scale) + 3)
        y1 = min(h, int(max(ys) / scale) + 3)
        if x1 <= x0 or y1 <= y0:
            continue
        crops.append(ach[y0:y1, x0:x1])
        rects.append((x0, y0, x1, y1))
    if not crops:
        return []
    try:
        rec = eng.recognize_txt(crops)
    except Exception:                            # noqa: BLE001
        return None                              # 交给上层整幅重读
    txts = getattr(rec, "txts", None) or ()
    scores = getattr(rec, "scores", None) or ()
    floor = float(getattr(eng, "text_score", 0.0) or 0.0)   # 与走原路径时的过滤口径一致
    out = []
    for i, t in enumerate(txts):
        t = (t or "").strip()
        if not t or i >= len(rects):
            continue
        sc = float(scores[i]) if i < len(scores) else 1.0
        if sc < floor:
            continue
        x0, y0, x1, y1 = rects[i]
        out.append({"x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0),
                    "text": t, "score": sc})
    return out


def _words(res):
    """把 RapidOCR 的 Output 摊成 [{x,y,w,h,text,score}]。"""
    if res is None:
        return []
    txts = getattr(res, "txts", None)
    boxes = getattr(res, "boxes", None)
    scores = getattr(res, "scores", None)
    if txts is None:
        txts = ()
    if scores is None:
        scores = ()
    out = []
    for i, t in enumerate(txts):
        t = (t or "").strip()
        if not t or boxes is None or len(boxes) <= i:
            continue
        pts = boxes[i]
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        x0, y0 = min(xs), min(ys)
        out.append({
            "x": int(round(x0)), "y": int(round(y0)),
            "w": max(1, int(round(max(xs) - x0))),
            "h": max(1, int(round(max(ys) - y0))),
            "text": t,
            "score": float(scores[i]) if i < len(scores) else 1.0,
        })
    return out


def make(settings=None):
    return Backend(settings)
