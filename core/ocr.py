"""游戏内文字读取。

用 Windows 自带的 OCR（Windows.Media.Ocr），不额外装 Tesseract。
进程内直调 WinRT，不再为每次识别起 powershell 进程。实测本机
（1920x1080 全屏截图）单次 read_words：旧 PowerShell 路径约 1140ms，
新路径预热后 6~8ms。

OcrEngine 的 RecognizeAsync 同一时刻只允许一个在跑（并发会抛
"Another RecognizeAsync operation is already running!"），所以全局
只建一个引擎，所有调用过 _engine_lock 串行。

中文识别有个已知问题：Windows OCR 会把中文按字拆开加空格，而且对
小字号识别率一般。所以这里做了两件事：
  1. 去掉汉字之间的空格
  2. 用模糊匹配把识别结果和任务里登记的商品名对齐
"""
import re
import sys
import threading

_engine = None
_engine_lock = threading.Lock()      # 串行化：引擎不允许并发识别
_engine_failed = False               # 装不上/建不起来时不再重试

# ---------------- WinRT 后端 ----------------

def _load_winrt():
    """按需导入 WinRT 投影包。返回模块元组，不可用返回 None。"""
    try:
        import winrt.windows.media.ocr as wocr
        import winrt.windows.globalization as g11n
        import winrt.windows.graphics.imaging as imaging
        import winrt.windows.storage.streams as streams
        return wocr, g11n, imaging, streams
    except ImportError:
        return None


def _get_engine():
    """返回常驻的 OcrEngine，不可用返回 None。"""
    global _engine, _engine_failed
    if _engine is not None or _engine_failed:
        return _engine
    mods = _load_winrt()
    if mods is None:
        _engine_failed = True
        return None
    wocr, g11n, _, _ = mods
    try:
        eng = wocr.OcrEngine.try_create_from_language(g11n.Language("zh-Hans-CN"))
    except OSError:
        eng = None
    if eng is None:
        _engine_failed = True
    _engine = eng
    return _engine


def _recognize(img):
    """把 BGR ndarray 交给引擎识别，返回 OcrResult；失败返回 None。"""
    import cv2
    engine = _get_engine()
    if engine is None:
        return None
    mods = _load_winrt()
    if mods is None:
        return None
    _, _, imaging, streams = mods
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return None
    stream = streams.InMemoryRandomAccessStream()
    writer = streams.DataWriter(stream)
    writer.write_bytes(buf.tobytes())
    writer.store_async().get()      # 不 commit 的话解码器读不到内容
    writer.flush_async().get()
    stream.seek(0)                  # 不回头的话解码器从流末尾开始读
    decoder = imaging.BitmapDecoder.create_async(stream).get()
    bitmap = decoder.get_software_bitmap_async().get()
    with _engine_lock:              # 引擎不允许多个 RecognizeAsync 并发
        return engine.recognize_async(bitmap).get()


# 汉字之间被 OCR 插进去的空格
_HAN_GAP = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_SPACES = re.compile(r"\s+")


def available():
    return sys.platform == "win32"


def backend_name():
    """OCR 后端是否真的可用，以及用的哪个引擎。

    打包成 exe 后最容易出问题的就是这条链（winrt 是惰性导入，
    PyInstaller 静态分析扫不到，见 sjz.spec 的注释），所以要能自查。
    """
    if sys.platform != "win32":
        return f"不可用（平台 {sys.platform}）"
    mods = _load_winrt()
    if mods is None:
        return "不可用（winrt 没装上，或缺失 winrt._winrt_windows_*）"
    try:
        engine = _get_engine()
    except Exception as e:
        return f"不可用（建引擎失败：{type(e).__name__}: {e}）"
    if engine is None:
        return "不可用（没有可用的 OCR 语言包）"
    try:
        lang = engine.recognizer_language.language_tag
    except Exception:
        lang = "?"
    return f"WinRT Windows.Media.Ocr（语言 {lang}）"


def read_text(img, timeout=6.0):
    """img: BGR ndarray。返回 OCR 出来的原始文本（已清理空格），失败返回 ""。

    timeout 保留是为了兼容旧调用点，进程内识别本身很快。
    """
    if not available() or img is None or getattr(img, "size", 0) == 0:
        return ""
    try:
        result = _recognize(img)
    except (OSError, RuntimeError):
        return ""
    if result is None:
        return ""
    raw = "\n".join(line.text for line in result.lines)
    return clean(raw)


def clean(text):
    """去掉汉字间空格、合并多余空白。"""
    if not text:
        return ""
    t = text.replace("\r", "\n")
    t = _HAN_GAP.sub("", t)
    t = _SPACES.sub(" ", t)
    return t.strip()


def normalize(s):
    """只留字母数字和汉字，用于模糊比对。"""
    if not s:
        return ""
    return "".join(ch for ch in s if ch.isalnum() or "\u4e00" <= ch <= "\u9fff").lower()


def similarity(a, b):
    """0-1 相似度。字符级 LCS 比例，避免引入额外依赖。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return min(len(na), len(nb)) / max(len(na), len(nb)) * 0.5 + 0.5
    # LCS 长度
    prev = [0] * (len(nb) + 1)
    for ca in na:
        cur = [0]
        for j, cb in enumerate(nb, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / max(len(na), len(nb))


def best_match(text, candidates, floor=0.55):
    """在候选商品名里找最像的。返回 (name, score) 或 (None, 0)。"""
    best, best_score = None, 0.0
    for c in candidates:
        s = similarity(text, c)
        if s > best_score:
            best, best_score = c, s
    if best_score < floor:
        return None, best_score
    return best, best_score


_PRICE = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
_NUM = re.compile(r"([0-9][0-9,]*(?:\.[0-9]+)?)")


def parse_price(text):
    """从 OCR 文本里抠价格。找不到返回 None。"""
    if not text:
        return None
    m = _PRICE.search(text)
    if not m:
        m = _NUM.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# ---------------- 带坐标的 OCR（列表模式用） ----------------

def read_words(img, timeout=10.0):
    """OCR 出每个词和它的包围盒。

    返回 [{"x","y","w","h","text"}]，坐标是相对传入图片左上角的像素。
    列表模式必须知道每个词在哪一行，才能把行聚起来、算出点哪儿。

    timeout 保留是为了兼容旧调用点，进程内识别本身很快。
    """
    if not available() or img is None or getattr(img, "size", 0) == 0:
        return []
    try:
        result = _recognize(img)
    except (OSError, RuntimeError):
        return []
    if result is None:
        return []
    out = []
    for line in result.lines:
        for w in line.words:
            t = (w.text or "").strip()
            if not t:
                continue
            r = w.bounding_rect
            out.append({"x": int(r.x), "y": int(r.y),
                        "w": int(r.width), "h": int(r.height), "text": t})
    return out


def rows(words, tol_ratio=0.7):
    """把词按纵向位置聚成行。

    容差取「词高中位数 × tol_ratio」：同一行的字高差不多，跨行的间距更大，
    这样不同字号混排（商品名大、价格小）也不会把两行粘成一行。
    """
    if not words:
        return []
    heights = sorted(w["h"] for w in words)
    median_h = heights[len(heights) // 2] or 1
    tol = max(2.0, median_h * tol_ratio)

    lines = []
    for w in sorted(words, key=lambda w: w["y"] + w["h"] / 2.0):
        cy = w["y"] + w["h"] / 2.0
        for line in lines:
            if abs(line["cy"] - cy) <= tol:
                line["words"].append(w)
                line["cy"] = sum(x["y"] + x["h"] / 2.0 for x in line["words"]) / len(line["words"])
                break
        else:
            lines.append({"cy": cy, "words": [w]})

    out = []
    for line in lines:
        ws = sorted(line["words"], key=lambda w: w["x"])
        x0 = min(w["x"] for w in ws)
        y0 = min(w["y"] for w in ws)
        x1 = max(w["x"] + w["w"] for w in ws)
        y1 = max(w["y"] + w["h"] for w in ws)
        out.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "cy": round((y0 + y1) / 2.0, 1),
            "text": clean(" ".join(w["text"] for w in ws)),
            "words": ws,
        })
    out.sort(key=lambda r: r["y0"])
    return out
