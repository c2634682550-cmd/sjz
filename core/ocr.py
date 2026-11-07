"""游戏内文字读取 —— 主路径 RapidOCR，PP-OCRv6 服务与自建字模做降级。

2026-11-06 的第二轮选型（20 条主基准 + 21 条真实截图 ROI，两轮打乱，
CPU-only，见仓库根 `_ocr_engine_selection.md`）：

    引擎                        主基准  真实 ROI   单帧(CPU)  冷启动   体积
    RapidOCR v6 small (ONNX)    36/36   42/42     13~73ms   1.2s    30MB
    PP-OCRv6 tiny (paddle)      36/36   42/42     25~34ms   4.3s    6MB+4GB venv
    PP-OCRv6 small (paddle 服务) 34/36   38/42     71~90ms   4.6s    —
    自建字模                     32/36   24~28/42   4~6ms    0ms     0.08MB

所以这里的主路径换成进程内的 RapidOCR（`core/ocr_rapid.py`），PP-OCRv6
服务（`core/ocr_ppocr.py`）退成第二级，只在价格读不准时用 3× 放大复核；
字模（`core/digit_ocr.py`）是最后一级纯数字快路径。编排在
`core/ocr_chain.py`，本模块只保留配置、自检和对外接口。

服务没起会怎样
--------------
RapidOCR 是进程内的，服务不在照样能读（这是这次换主路径的主要收益：
以前 paddlex 服务没起来，OCR 整项检查就不过）。服务不在时：
`available()` 仍看 Rapid；价格的二级复核自动跳过；日志里会写明。

运行中 Rapid 报错：`read_words` 落到服务（若在跑），否则抛 OCRError，
调用方按「眼睛瞎了」处理，不会把空串当成「没字」去乱点。
"""
import json
import re
import threading

from . import ocr_chain, ocr_ppocr, ocr_rapid

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
    """OCR 主链（懒建；建失败一次就不再建，直到 reload/set_url）。

    主链的第一级是进程内 RapidOCR，模型在 core/assets/rapid/（随仓库走）；
    第二级才是原来的 PP-OCR 服务。三级的参数写死在 ocr_rapid / ocr_chain，
    这里不重复配置，避免两份参数各改一半。
    """
    global _backend, _broken, _last_err
    if _backend is not None:
        return _backend
    if _broken:
        return None
    try:
        _backend = ocr_chain.make(_cfg())
        _last_err = ""
    except Exception as e:                       # noqa: BLE001
        _last_err = f"{type(e).__name__}: {e}"
        _backend = None
        _broken = True
    return _backend


# ---------------- 详情面板 ROI 路由 ----------------
#
# 详情面板是一整块 590×860 的竖版窗口，真正要读的只有四块：名称 / 品阶+成色 /
# 价格 / 数量。整帧送 det 要覆盖 50 万像素、还要把武器大图与装饰一起跑一遍，
# 实测 222.8ms（p50）；只拼四块必要区，同一个面板 89.6ms（p50），读出的
# price / craft / wear 完全一致。
#
# 路由条件只认「竖版 + 够大」的几何特征，不改调用点：
#   · 关掉：config.json 的 ocr.detail_panel_roi 设 false（默认 true）
#   · 读不出四条（面板换版、或这一帧根本不是详情面板）自动退回整幅读，
#     宁可慢一点，也不让面板读数整块失灵。

def _panel_roi_on():
    return bool((_cfg() or {}).get("detail_panel_roi", True))


def _panel_like(img):
    """是不是详情面板那种竖长条。整屏(0.56)、列表(0.46) 都不在此区间。"""
    h, w = img.shape[:2]
    if w < 480 or h < 700:
        return False
    ratio = h / float(w)
    return 1.32 <= ratio <= 1.62


def _panel_read_ok(words):
    """拼条读全必需块、且价格能**只**从 price 块读出来，才算成功。

    门槛不按「词数 < 4/5」：一块可能吐出两个词（成色行），词数够不代表块
    齐。这里按块判：名称 / 成色 / 价格 / 数量缺任一就整幅重读。稀有度是
    可选补充块（卡片版式与成色同行、无做工面板没有品阶行），理由与实测
    见 `ocr_chain.PANEL_REQUIRED`。
    """
    have = {w.get("field") for w in (words or []) if w.get("field")}
    if not all(f in have for f in ocr_chain.PANEL_REQUIRED):
        return False
    value, _tok = ocr_chain.price_from_words(words)
    return value is not None


# ---------------- 自检 ----------------

def available():
    """OCR 服务能不能用。启动前检查看这一项。"""
    b = _client()
    return bool(b is not None and b.available())


def selfcheck_pp(probe_timeout=0.3):
    """启动自检二级（真推理一次空白小图）。返回 (ok, error)。

    为什么不用 TCP 探活：本机 Paddle 3.x 的 CPU/oneDNN 分支端口在听、每次
    推理都 500，probe 会把死掉的二级显示成可用。这里只做诊断，不关级。
    probe_timeout 由调用方给（启动时用 0.3s，且整段自检在后台线程跑）。
    """
    b = _client()
    if b is None:
        return False, f"客户端不可用：{_last_err or '未配置'}"
    fn = getattr(b, "selfcheck_pp", None)
    if not callable(fn):
        return False, "当前后端没有二级服务"
    try:
        return fn(probe_timeout=probe_timeout)
    except Exception as e:                       # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


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
    """把 PP-OCR 客户端切到另一个服务地址（蓝绿重启用，见 core/ocr_service.py）。

    只改地址、不动别的配置。**已经建好的主链只换第二级**：RapidOCR 的
    ONNX session 留着（重建要 1.2s 冷启动，蓝绿切换正好发生在抢购间隙，
    那 1.2s 白等一次很浪费）。
    """
    global _settings, _backend, _broken
    with _lock:
        s = dict(_settings or _load_settings())
        pp = dict(s.get("ppocr") or {})
        pp["url"] = url
        s["ppocr"] = pp
        _settings = s
        b = _backend
        if b is not None and hasattr(b, "pp"):
            try:
                b.pp = ocr_ppocr.make(pp)
                b.url = getattr(b.pp, "url", "") or url
                return
            except Exception:                   # noqa: BLE001
                pass
        _backend = None
        _broken = False


def current_url():
    """当前配置里的服务地址（不建客户端，纯读配置）。"""
    s = _cfg()
    return str(((s.get("ppocr") or {}).get("url") or ""))


def metrics():
    """给管家和自检看的一组数：地址、调用次数、最近中位耗时、二级死活。

    `tier2_*` 是后加的：二级（PP-OCRv6）在 CPU 上可能「端口通、每次推理
    500」，只看 ok/url 会把它显示成启用。tier2_fails 涨而 tier2_attempts
    同步涨，就是这层在崩、实际全靠字模兜底。
    """
    b = _client()
    if b is None:
        return {"ok": False, "url": "", "calls": 0, "fails": 0, "recent_ms": 0.0,
                "tier2_attempts": 0, "tier2_fails": 0, "tier2_error": ""}
    try:
        st = getattr(b, "stats", {}) or {}
        return {
            "ok": bool(b.available()),
            "url": getattr(b, "url", "") or "",
            "calls": st.get("calls", 0),
            "fails": st.get("fails", 0),
            "recent_ms": round(b.recent_ms(), 1) if hasattr(b, "recent_ms") else 0.0,
            "tier2_attempts": st.get("tier2", 0),
            "tier2_fails": st.get("tier2_fail", 0),
            "tier2_error": str(getattr(b, "last_error", "") or ""),
        }
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "url": "", "calls": 0, "fails": 0,
                "recent_ms": 0.0, "tier2_attempts": 0, "tier2_fails": 0,
                "tier2_error": f"{type(e).__name__}: {e}"}


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
        words = None
        if _panel_roi_on() and _panel_like(img):
            panel = getattr(b, "read_panel", None)
            if callable(panel):
                words = panel(img, timeout=timeout) or []
                if not _panel_read_ok(words):
                    # 缺块 / 价格读不出来：整幅再读一次，不走拼条
                    words = None
        if words is None:
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
    """右上角余额栏里的三角币数量（整数）；结构不清楚返回 None。

    这是 `engine._balance` 的唯一入口。余额栏是固定三组货币，不是自由
    文本：必须读到「带 `+` 的最右一组」才算数，规则见
    `ocr_chain.balance_from_text` / `money.scan_balance`。以前它复用
    `price_from_text` 的 `rest[-1]`（取最后一个数字），最右一组读丢时会
    静默退到中间那组青铜币（实测 `2,537K 2,075 + à` → 2075），所以现在
    走余额专用解析：读不出就是 None，让上层按「余额未知」处理。
    """
    value, _tok = ocr_chain.balance_from_text(text)
    return value


def read_price(img, ban=None, timeout=None):
    """读价格条上那个数。返回 int；读不到 / 不合格返回 None。

    这是「点购买」前唯一该用的价格入口（`engine._bar_price` 走这里）。
    三级降级 + 两道闸门都在 ocr_chain.Backend.read_price 里：
      RapidOCR(原图) → PP-OCRv6(3×，仅分数<0.5 或格式没过时) → 自建字模
    任何一级没过闸门就继续往下，全都没过返回 None —— 不猜。
    `ban`：出现这些词（详情按钮上的「解锁」「秒」）直接判为非价格。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return None
    b = _client()
    if b is None:
        return None
    try:
        value = b.read_price(img, ban=ban, timeout=timeout)
    except Exception:                            # noqa: BLE001
        _stats["empty"] += 1
        return None
    if value is None:
        _stats["empty"] += 1
    else:
        _stats["calls"] += 1
    return value


def rapid_model_dir():
    """RapidOCR 本地模型目录（打包后是 _MEIPASS/core/assets/rapid）。

    `--doctor` 用它确认离线模型确实进包了 —— 模型没进包时 RapidOCR
    会退回自己包内的副本，看起来一切正常，实际多背了 30MB 且版本可能不同。
    """
    return ocr_rapid.MODEL_DIR


def rapid_error():
    """RapidOCR 初始化/调用失败原因；正常时返回空串。

    打包版出问题（yaml 没进包、onnxruntime DLL 缺失）时，`--doctor`
    只打「OCR 可用 = False」是没法修的，必须把这行原样带出来。
    """
    b = _client()
    rp = getattr(b, "rapid", None) if b is not None else None
    if rp is None:
        return _last_err or ""
    return getattr(rp, "last_error", "") or _last_err or ""


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
