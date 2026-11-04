"""游戏内文字读取 —— 唯一后端：PaddleOCR PP-OCRv6（本机服务）。

为什么只剩一个
--------------
2026-11-01 用真截图做了横向评测（tools/ocr_eval.py，判分见 说明.txt 第九节）：

    引擎                        字段准确   整屏 1380×630   1920×1080
    Windows OCR                  16/42       66ms          118ms / 价格 0/6
    PaddleOCR-VL-1.6              漏块     161s(CPU)         —
    RapidOCR v6 small            31/42      264ms            —
    RapidOCR v5 server           40/42     2159ms            —
    PP-OCRv5 server              42/42      192ms            —
    PP-OCRv5 mobile              42/42       95ms            —
    PP-OCRv6 small               42/42       63ms          98ms / 价格 6/6

PP-OCRv6 small 是唯一「准确率满分 + 延迟可接受」的，所以其它后端全部删掉
（ocr_winrt.py / ocr_vl.py），只留这一条路。少一层选择，就少一处「后端
切换导致落点偏移」的隐患。

为什么走 HTTP 而不是进程内
--------------------------
paddle 装进来 exe 会从几十 MB 涨到 1.5GB，PyInstaller 对它的静态分析也
基本不可用。所以本机另起一个 PaddleX OCR 服务，这里只做客户端：

    .venv-vl\\Scripts\\paddlex.exe --serve --pipeline tools\\OCR_game.yaml ^
        --port 8081 --device gpu:0

服务没起会怎样
--------------
`available()` 为假 → 启动前检查的「OCR 识别」那项不过 → 不许开抢，
并直接告诉你哪条命令没执行。**宁可开不了工，也不要带着瞎的眼睛去抢。**

运行中服务挂了：每次识别抛错 → 返回空 → 引擎按「读不出倒计时/读不出
价格」走它已有的失败分支（Aborted 或跳过该商品），不会乱点。连续 3 次
失败后客户端会长时间停用，避免每 5 秒白等一次超时。
"""
import json
import re
import threading

from . import ocr_ppocr

_lock = threading.Lock()
_settings = None
_backend = None
_broken = False          # 客户端建不起来后短路，不再每次调用都重试构造
_last_err = ""
_stats = {"calls": 0, "empty": 0}


class OCRError(RuntimeError):
    """OCR 调用本身失败（服务不在、超时、HTTP 500、冷却期）。

    与「图上真的没字」严格区分：调用方拿到 OCRError 应该重试/中止，
    拿到空列表才可以说「没有这一项」（review V08）。
    """


# ---------------- 配置 ----------------

def _load_settings():
    """读 config.json 的 ocr 段。缺项用 config.DEFAULTS 补齐。"""
    from .config import CONFIG_PATH, DEFAULTS
    base = dict(DEFAULTS.get("ocr") or {})
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = (json.load(f) or {}).get("ocr") or {}
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def _cfg():
    global _settings
    if _settings is None:
        with _lock:
            if _settings is None:
                _settings = _load_settings()
    return _settings


def reload():
    """配置改完（或服务刚起来）后重建客户端。"""
    global _settings, _backend, _broken, _last_err
    with _lock:
        _settings = _load_settings()
        _backend = None
        _broken = False
        _last_err = ""


def _client():
    """PP-OCR 客户端（懒建；建失败一次就不再建，直到 reload/set_url）。"""
    global _backend, _broken, _last_err
    if _backend is not None:
        return _backend
    if _broken:
        return None
    try:
        _backend = ocr_ppocr.make(_cfg().get("ppocr") or {})
        _last_err = ""
    except Exception as e:                       # noqa: BLE001
        _last_err = f"{type(e).__name__}: {e}"
        _backend = None
        _broken = True
    return _backend


# ---------------- 自检 ----------------

def available():
    """OCR 服务能不能用。启动前检查看这一项。"""
    b = _client()
    return bool(b is not None and b.available())


def backend_name():
    """当前后端是谁 —— 自检、日志、打包排查都看这一行。

    带上实测平均耗时：延迟跟显卡/是否和游戏抢显存强相关，写在自检里比
    写在文档里有用（一眼能看出是不是慢到不该用）。
    """
    b = _client()
    if b is None:
        return f"不可用（{_last_err or '客户端建不起来'}）"
    try:
        name = b.name()
    except Exception as e:                       # noqa: BLE001
        return f"不可用（{type(e).__name__}: {e}）"
    if b.stats["calls"]:
        name += f" · 实测 {b.stats['ms'] / b.stats['calls']:.0f}ms/次"
    return name


def describe():
    """给日志看的一行摘要。"""
    b = _client()
    main = b.name() if b is not None else "不可用"
    out = f"后端={main} 调用={_stats['calls']} 空结果={_stats['empty']}"
    if b is not None:
        out += " | " + b.stats_line()
    return out


def stats():
    return dict(_stats)


def set_url(url):
    """把客户端切到另一个 OCR 服务地址（蓝绿重启用，见 core/ocr_service.py）。

    只改地址、不动别的配置；下一次调用时按新地址重建客户端。
    调用方不用感知 —— 老实例在新实例验活之前一直还在服务。
    """
    global _settings, _backend, _broken
    with _lock:
        s = dict(_settings or _load_settings())
        pp = dict(s.get("ppocr") or {})
        pp["url"] = url
        s["ppocr"] = pp
        _settings = s
        _backend = None
        _broken = False


def current_url():
    """当前配置里的服务地址（不建客户端，纯读配置）。"""
    s = _cfg()
    return str(((s.get("ppocr") or {}).get("url") or ""))


def metrics():
    """给管家和自检看的一组数：地址、调用次数、最近中位耗时。"""
    b = _client()
    if b is None:
        return {"ok": False, "url": "", "calls": 0, "fails": 0, "recent_ms": 0.0}
    try:
        return {
            "ok": bool(b.available()),
            "url": getattr(b, "url", "") or "",
            "calls": b.stats.get("calls", 0),
            "fails": b.stats.get("fails", 0),
            "recent_ms": round(b.recent_ms(), 1) if hasattr(b, "recent_ms") else 0.0,
        }
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "url": "", "calls": 0, "fails": 0,
                "recent_ms": 0.0, "error": f"{type(e).__name__}: {e}"}


# ---------------- 对外 ----------------

def read_text(img, timeout=None, raise_on_error=False):
    """img: BGR ndarray。返回 OCR 出来的文本（已清理空格）。

    raise_on_error=True 时，OCR 服务不可用/调用失败抛 OCRError，而不是
    返回 "" —— 否则调用方分不清「这一块没字」和「眼睛瞎了」（review V08）。
    timeout=None 表示用配置里的 ocr.ppocr.timeout_s；给了显式值就用它，
    以前形参被忽略、调用方以为能延长等待（V16）。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return ""
    b = _client()
    if b is None:
        if raise_on_error:
            raise OCRError(f"OCR 客户端不可用：{_last_err or '未配置'}")
        return ""
    try:
        raw = b.read_text(img, timeout=timeout) or ""
    except Exception as e:                       # noqa: BLE001
        _stats["empty"] += 1
        if raise_on_error:
            raise OCRError(f"{type(e).__name__}: {e}") from e
        return ""
    if not raw.strip():
        _stats["empty"] += 1
        return ""
    _stats["calls"] += 1
    return clean(raw)


def read_words(img, timeout=None, raise_on_error=False):
    """OCR 出每一行文字和它的包围盒。

    返回 [{"x","y","w","h","text","score"}]，坐标是相对传入图片左上角的
    真实像素。列表模式必须知道每行在哪，才能把行聚起来、算出点哪儿。

    raise_on_error=True 时，服务不可用/调用失败抛 OCRError；空列表只表示
    「图上确实没读出字」（review V08）。timeout=None 用配置值，显式值透传（V16）。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return []
    b = _client()
    if b is None:
        if raise_on_error:
            raise OCRError(f"OCR 客户端不可用：{_last_err or '未配置'}")
        return []
    try:
        words = b.read_words(img, timeout=timeout) or []
    except Exception as e:                       # noqa: BLE001
        _stats["empty"] += 1
        if raise_on_error:
            raise OCRError(f"{type(e).__name__}: {e}") from e
        return []
    if not words:
        _stats["empty"] += 1
    else:
        _stats["calls"] += 1
    return words


# ---------------- 文本清理 / 比对 ----------------

# 汉字之间被 OCR 插进去的空格
_HAN_GAP = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_SPACES = re.compile(r"\s+")


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
    """0-1 相似度。字符级 LCS 比例，避免引入额外依赖。

    「包含」关系不再给 0.5 的加成底：标题区域小，OCR 常只读出一两个字，
    `similarity("攻势","棱镜攻势")` 原来有 0.750，直接过了 0.72 的判定
    阈值 —— 读到半个名字就敢点开并买（review V14）。短串（<4 字）只给
    长度覆盖比，要求几乎完全包含才算像。
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        small, big = (na, nb) if len(na) <= len(nb) else (nb, na)
        ratio = len(small) / max(len(big), 1)
        if len(small) < 4:
            return ratio
        return ratio * 0.5 + 0.5
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


def last_number(text):
    """文本里最后一个数字（整数）。

    千位逗号先去掉（OCR 会把 `15,000` 认成 `15` + `囤` + `00`），
    取**最后一个**数字串：价格条上印的是一个数，右上角货币栏里三角币
    在最右边，两处都是「最后一个」。
    """
    nums = digit_runs(text)
    return int(nums[-1]) if nums else None


# ---------------- 数字 ----------------

def digit_runs(text):
    """文本里的数字串，按出现顺序返回。

    千位逗号先去掉：OCR 会把 `15,000` 认成 `15` + `囤` + `00`，
    逗号认成汉字的情况也见过，所以 `,` 和 `，` 都去掉再切。
    """
    return re.findall(r"\d+", (text or "").replace(",", "").replace("，", ""))


def digits_only(text, strip_zeros=True):
    """文本里所有的数字位拼成一个串（其它字符全丢）。

    strip_zeros=True 会去掉前导零：价格块里金币图标常被认成 0，
    `025,000` 不去掉就是 25000 变 25。
    """
    d = re.sub(r"[^\d]", "", text or "")
    return d.lstrip("0") if strip_zeros else d


# ---------------- 行 / 词组 ----------------

def _group_by_y(words, tol_of, join_ok=None):
    """按纵向位置把词聚成组。

    tol_of(word, group) → 容差像素；join_ok(word, group) → 能不能并进这一组
    （默认都能并）。这是「聚行」和「切词组」共用的底子。
    """
    groups = []
    for w in words:
        cy = w["y"] + w["h"] / 2.0
        for g in groups:
            if abs(g["cy"] - cy) <= tol_of(w, g) and (join_ok is None or join_ok(w, g)):
                g["words"].append(w)
                g["cy"] = (sum(x["y"] + x["h"] / 2.0 for x in g["words"])
                           / len(g["words"]))
                break
        else:
            groups.append({"cy": cy, "words": [w]})
    return groups


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

    lines = _group_by_y(sorted(words, key=lambda w: w["y"] + w["h"] / 2.0),
                        lambda w, g: tol)

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


def phrases(words, gap=12):
    """把 OCR 词按行拼成词组，返回 [{text, x0, y0, x1, y1, cx, cy}]。

    为什么需要：OCR 按字符块返回，「电玩高手」会拆成「电玩」「高手」两块，
    直接拿单词比对永远匹配不上。按 y 归行、按 x 间隙拼接才能还原成词。
    """
    if not words:
        return []
    ws = sorted(words, key=lambda w: (w["y"], w["x"]))

    def join_ok(w, g):
        # 间隙要比「这一组最右边的词」，不是「最后 append 进来的词」：
        # 同行内两个词的 y 差 1~3px 时排序会变，拿最后 append 的词去比，
        # 间隙算成负数 → 隔着 90px 的两个词也被拼成一句（review V15）。
        last = max(g["words"], key=lambda x: x["x"])
        if w["x"] < last["x"]:
            return False
        return w["x"] - (last["x"] + last["w"]) <= gap

    groups = _group_by_y(ws, lambda w, g: max(6.0, w["h"] * 0.6), join_ok)
    out = []
    for g in groups:
        ws2 = sorted(g["words"], key=lambda w: w["x"])
        x0 = min(w["x"] for w in ws2)
        y0 = min(w["y"] for w in ws2)
        x1 = max(w["x"] + w["w"] for w in ws2)
        y1 = max(w["y"] + w["h"] for w in ws2)
        out.append({"text": "".join(w["text"] for w in ws2),
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "cx": (x0 + x1) // 2, "cy": (y0 + y1) // 2})
    out.sort(key=lambda p: (p["y0"], p["x0"]))
    return out
