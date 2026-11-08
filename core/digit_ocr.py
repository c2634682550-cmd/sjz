"""纯模板数字 OCR 快路径（毫秒级，无外部模型依赖）。

流程: 灰度化 -> OTSU/自适应二值 -> 连通域切分 -> 宽高比/间距过滤 -> 归一化字模匹配。

字模有两套，``real`` 优先、渲染兜底:
- ``<asset_dir>/real/``：从真实游戏截图切出的字形（每字符多个变体，见 meta.json）；
- ``<asset_dir>/*.png``：用 cv2.putText 本机渲染的字模，仅作兜底（不下载任何外部图片）。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------- 常量

CANON_W = 20
CANON_H = 32

CHAR_SET = "0123456789,"
_FONT = cv2.FONT_HERSHEY_SIMPLEX
# 字模跟着 core/ 走（core/assets/digits），不写死任何绝对路径；
# sjz.spec 已经把整个 core/assets 打进 _MEIPASS，冻结后同样能按 __file__ 找到。
_ASSET_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "digits"
)

DEFAULT_MIN_CONF = 0.55
# 最优字符与次优字符的最小分差：小于此值视为"3/8 之类拿不准"，直接拒识
DEFAULT_MIN_MARGIN = 0.055
# 真字模的额外加成（同分时优先真字模）
REAL_BONUS = 0.02
# 匹配时允许的 ±1px 平移量，抵消亚像素抖动
_SHIFT_RANGE = 1


class DigitOcrError(Exception):
    """数字 OCR 内部异常（ocr.py 会转成 OcrError）。"""


# ---------------------------------------------------------------- 字模生成

def _render_char(ch: str, thickness: int) -> np.ndarray:
    """把单个字符渲染成白字黑底小图，返回裁剪到 bbox 后的图像。"""
    scale = 1.2
    (tw, th), base = cv2.getTextSize(ch, _FONT, scale, thickness)
    canvas = np.zeros((th + base + 12, tw + 12), np.uint8)
    cv2.putText(canvas, ch, (6, th + 6), _FONT, scale, 255, thickness, cv2.LINE_AA)
    ys, xs = np.nonzero(canvas)
    if len(xs) == 0:
        raise DigitOcrError("渲染字符失败: %r" % ch)
    return canvas[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _imwrite(path: str, img: np.ndarray) -> None:
    """cv2.imwrite 不支持非 ASCII 路径，用 imencode + 文件写入。"""
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise DigitOcrError("编码 PNG 失败: %s" % path)
    with open(path, "wb") as f:
        f.write(buf.tobytes())


def _imread_gray(path: str) -> Optional[np.ndarray]:
    if not os.path.isfile(path):
        return None
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)


def _canon(img: np.ndarray) -> np.ndarray:
    """裁剪 + 归一化到 CANON_W×CANON_H 的 0~1 浮点图。"""
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    ys, xs = np.nonzero(img > 40)
    if len(xs) == 0:
        return np.zeros((CANON_H, CANON_W), np.float32)
    patch = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    ph, pw = patch.shape[:2]
    upscale = (CANON_W / float(pw)) > 1.0 or (CANON_H / float(ph)) > 1.0
    patch = cv2.resize(
        patch, (CANON_W, CANON_H),
        interpolation=cv2.INTER_CUBIC if upscale else cv2.INTER_AREA,
    )
    return patch.astype(np.float32) / 255.0


def ensure_templates(asset_dir: str = _ASSET_DIR, force: bool = False) -> str:
    """生成（或复用）内置字模，返回资产目录。字模 = 两种笔宽 × 12 个字符。"""
    meta_path = os.path.join(asset_dir, "meta.json")
    chars = [c for c in CHAR_SET]
    need = force or not os.path.isdir(asset_dir)
    if not need and os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            need = not all(
                os.path.isfile(os.path.join(asset_dir, item["file"]))
                for item in meta.get("variants", [])
            )
        except Exception:
            need = True
    if not need:
        return asset_dir

    os.makedirs(asset_dir, exist_ok=True)
    variants: List[dict] = []
    for ch in chars:
        name = "comma" if ch == "," else ch
        for thickness in (1, 2):
            raw = _render_char(ch, thickness)
            h, w = raw.shape[:2]
            fname = "%s_t%d.png" % (name, thickness)
            _imwrite(os.path.join(asset_dir, fname), raw)
            variants.append(
                {
                    "char": ch,
                    "file": fname,
                    "aspect": float(w) / float(h),
                    "thickness": thickness,
                }
            )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"canon": [CANON_W, CANON_H], "font": _FONT, "variants": variants}, f, ensure_ascii=False, indent=2)
    return asset_dir


_TEMPLATE_LOCK = threading.Lock()
_TEMPLATES: Optional[List[Tuple[str, np.ndarray, float]]] = None
_REAL_DIRNAME = "real"


def _load_real_templates(asset_dir: str, out: List[Tuple[str, np.ndarray, float]]) -> Tuple[int, dict]:
    """把 ``<asset_dir>/real/meta.json`` 描述的真字模追加到 out，返回 (真字模个数, meta)。"""
    real_dir = os.path.join(asset_dir, _REAL_DIRNAME)
    meta_path = os.path.join(real_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return 0, {}
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    n = 0
    for item in meta.get("variants", []):
        img = _imread_gray(os.path.join(real_dir, item["file"]))
        if img is None:
            continue
        out.append((str(item["char"]), _canon(img), float(item.get("aspect", 1.0))))
        n += 1
    return n, meta


def load_templates(asset_dir: str = _ASSET_DIR) -> List[Tuple[str, np.ndarray, float]]:
    """加载字模为 [(char, canon_float_img, aspect)]，进程内缓存。

    真字模排在前面（匹配时享 REAL_BONUS），渲染字模兜底。
    """
    global _TEMPLATES
    with _TEMPLATE_LOCK:
        if _TEMPLATES is not None:
            return _TEMPLATES
        ensure_templates(asset_dir)
        with open(os.path.join(asset_dir, "meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        out: List[Tuple[str, np.ndarray, float]] = []
        real_n, real_meta = _load_real_templates(asset_dir, out)
        # 每字符自适应分差阈值：由构建脚本按“训练集 margin 分布”离线算好后写进 meta，
        # 这里只读，不在加载时用字模互相相似度拍脑袋估（那样会把正确读数拒掉一片）。
        _ADAPTIVE[id(out)] = {
            str(k): float(v) for k, v in (real_meta.get("margin_thresholds") or {}).items()
        }
        for item in meta["variants"]:
            img = _imread_gray(os.path.join(asset_dir, item["file"]))
            if img is None:
                raise DigitOcrError("字模文件缺失: %s" % item["file"])
            out.append((item["char"], _canon(img), float(item["aspect"])))
        _REAL_INDEX[id(out)] = real_n
        _INDEX_CACHE.pop(id(out), None)
        if not _ADAPTIVE[id(out)]:
            _ADAPTIVE.pop(id(out), None)
        _TEMPLATES = out
        return out


# ---------------------------------------------------------------- 预处理

def to_gray(img: np.ndarray) -> np.ndarray:
    if img is None:
        raise DigitOcrError("输入图像为 None")
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def binarize(gray: np.ndarray) -> np.ndarray:
    """OTSU 二值化，自动判定极性，输出"文字=255、背景=0"。

    先粗二值定位前景并裁紧，再把小字号放大到 ~48px 高后重新阈值化，
    避免 ROI 留白导致小字被吃掉。
    """
    if gray.size == 0:
        return np.zeros_like(gray)

    def _otsu(g: np.ndarray) -> np.ndarray:
        blur = cv2.GaussianBlur(g, (3, 3), 0)
        _, binv = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        # 文字是少数派；若"前景"超过一半说明极性反了
        return binv if float(np.count_nonzero(binv)) <= 0.5 * binv.size else cv2.bitwise_not(binv)

    binary = _otsu(gray)
    ys, xs = np.nonzero(binary)
    if len(xs) == 0:
        return binary
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    crop = gray[y0:y1, x0:x1]
    ch = crop.shape[0]
    if ch < 44:
        scale = 44.0 / max(ch, 1)
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), 44), interpolation=cv2.INTER_CUBIC)
        binary = _otsu(crop)
    else:
        binary = binary[y0:y1, x0:x1]
    # 补 2px 边框，避免字形贴边被切分阶段当成"占满整幅"而丢弃
    return cv2.copyMakeBorder(binary, 2, 2, 2, 2, cv2.BORDER_CONSTANT, value=0)


# ---------------------------------------------------------------- 切分

def _projection_split(mask: np.ndarray) -> List[Tuple[int, int]]:
    """按列投影谷底把粘连的多个字形拆开，返回 [(x0, x1)]（相对 mask 的 x）。"""
    if mask.ndim == 1:
        mask = mask[:, None]
    mw = mask.shape[1]
    if mw < 5:
        return [(0, mw)]
    cols = mask.sum(axis=0).astype(np.float32)
    mx = float(cols.max()) if cols.size else 0.0
    if mx <= 0:
        return [(0, mw)]
    lo, hi = max(1, int(0.2 * mw)), min(mw - 2, int(0.8 * mw))
    if hi <= lo:
        return [(0, mw)]
    j = int(np.argmin(cols[lo:hi + 1])) + lo
    if cols[j] <= 0.42 * mx and cols[j] <= 0.5 * float(mask.shape[0]):
        left = _projection_split(mask[:, :j])
        right = [(a + j + 1, b + j + 1) for a, b in _projection_split(mask[:, j + 1:])]
        return left + right
    return [(0, mw)]


def segment(binary: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """连通域切分，返回按 x 排序的 (x, y, w, h)；只做绝对噪声过滤。

    相对过滤（太小 = 分隔符/噪声）放在 read_number 里按最大字形高度做，
    避免 ROI 上方留白把真实数字整体滤掉。粘连的多字形按投影谷底拆分。
    """
    if binary.size == 0:
        return []
    h, w = binary.shape[:2]
    num, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    boxes: List[Tuple[int, int, int, int]] = []
    min_area = max(4.0, 0.0004 * h * w)
    for i in range(1, num):
        x, y, cw, ch, area = stats[i]
        if area < min_area:
            continue
        if ch < 3 or ch > 0.98 * h:
            continue
        if cw < 2:
            continue
        comp = (labels[y:y + ch, x:x + cw] == i)
        if cw > 1.05 * ch:
            for x0, x1 in _projection_split(comp):
                if x1 - x0 < 2:
                    continue
                sub = comp[:, x0:x1]
                ys = np.nonzero(sub.any(axis=1))[0]
                if ys.size == 0:
                    continue
                boxes.append((int(x + x0), int(y + ys.min()), int(x1 - x0), int(ys.max() - ys.min() + 1)))
        else:
            boxes.append((int(x), int(y), int(cw), int(ch)))
    boxes.sort(key=lambda b: b[0])
    return boxes


def _merge_close(boxes: Sequence[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int]]:
    """合并重叠/紧邻的碎片（如 '1' 的圆点、断裂笔画）。"""
    if not boxes:
        return []
    items = [list(b) for b in boxes]
    merged: List[List[int]] = []
    for x, y, w, h in items:
        placed = False
        for m in merged:
            mx, my, mw, mh = m
            gap_x = max(mx, x) - min(mx + mw, x + w)
            overlap_y = min(my + mh, y + h) - max(my, y)
            ref_h = max(mh, h)
            union_w = max(mx + mw, x + w) - min(mx, x)
            # 只合并同一字形的碎片（垂直堆叠、x 方向贴合），不合并相邻数字
            if gap_x <= 1 and union_w <= 0.85 * ref_h and overlap_y >= -1:
                nx, ny = min(mx, x), min(my, y)
                nw = max(mx + mw, x + w) - nx
                nh = max(my + mh, y + h) - ny
                m[:] = [nx, ny, nw, nh]
                placed = True
                break
        if not placed:
            merged.append([x, y, w, h])
    merged.sort(key=lambda b: b[0])
    return [tuple(b) for b in merged]  # type: ignore[misc]


# ---------------------------------------------------------------- 匹配

_REAL_INDEX: Dict[int, int] = {}
_INDEX_CACHE: Dict[int, dict] = {}
_ADAPTIVE: Dict[int, Dict[str, float]] = {}
_INDEX_LOCK = threading.Lock()

ADAPTIVE_MAX_MARGIN = 0.10


def margin_thresholds(margins: Dict[str, Sequence[float]],
                      pct: float = 5.0,
                      floor: float = DEFAULT_MIN_MARGIN,
                      ceil: float = ADAPTIVE_MAX_MARGIN) -> Dict[str, float]:
    """按字符自适应分差阈值 = 该字符在**训练集**识别时 margin 分布的 p5。

    只用训练折的实测 margin（不碰验证集），并夹在 [floor, ceil]：
    宁可略保守，也不许把正确读数拒掉一片。
    """
    out: Dict[str, float] = {}
    for ch, vals in margins.items():
        vals = [float(v) for v in vals if v is not None]
        if not vals:
            continue
        p5 = float(np.percentile(vals, pct))
        out[str(ch)] = min(ceil, max(floor, p5))
    return out


def _unit(v: np.ndarray) -> np.ndarray:
    """零均值单位向量（等价于归一化互相关 NCC 的查询向量）。"""
    v = v.astype(np.float32, copy=True)
    v -= float(v.mean())
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


def _shift(img: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """平移（越界补 0）。"""
    h, w = img.shape[:2]
    out = np.zeros_like(img)
    ys0, ys1 = max(0, dy), min(h, h + dy)
    xs0, xs1 = max(0, dx), min(w, w + dx)
    out[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx] = img[ys0:ys1, xs0:xs1]
    return out


def _template_index(templates: List[Tuple[str, np.ndarray, float]]) -> dict:
    """把字模集合矢量化为 (N×640) 单位向量矩阵，进程内按列表身份缓存。"""
    key = id(templates)
    idx = _INDEX_CACHE.get(key)
    if idx is not None:
        return idx
    with _INDEX_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is not None:
            return idx
        labels: List[str] = []
        aspects: List[float] = []
        rows: List[np.ndarray] = []
        for ch, img, asp in templates:
            labels.append(ch)
            aspects.append(float(asp))
            rows.append(_unit(np.asarray(img, np.float32).ravel()))
        mat = np.vstack(rows) if rows else np.zeros((0, CANON_W * CANON_H), np.float32)
        idx = {
            "labels": labels,
            "aspects": np.asarray(aspects, np.float32),
            "mat": mat,
            "real_n": int(_REAL_INDEX.get(key, 0)),
        }
        _INDEX_CACHE[key] = idx
        return idx


def match_char_detail(
    patch: np.ndarray,
    templates: List[Tuple[str, np.ndarray, float]],
) -> Tuple[str, float, float]:
    """最近邻匹配，返回 (字符, 置信度, 与次优字符的分差)。"""
    canon = _canon(patch)
    ph, pw = patch.shape[:2]
    aspect = float(pw) / float(ph) if ph else 0.0
    idx = _template_index(templates)
    if not idx["labels"]:
        return "", 0.0, 0.0

    # 多平移取最大 NCC，抵消亚像素抖动
    sims = None
    for dy in range(-_SHIFT_RANGE, _SHIFT_RANGE + 1):
        for dx in range(-_SHIFT_RANGE, _SHIFT_RANGE + 1):
            q = _unit(_shift(canon, dy, dx).ravel())
            if float(np.linalg.norm(q)) < 1e-6:
                continue
            s = idx["mat"] @ q
            sims = s if sims is None else np.maximum(sims, s)
    if sims is None:
        return "", 0.0, 0.0

    tpl_aspects = idx["aspects"]
    aspect_score = 1.0 - np.minimum(
        1.0, np.abs(np.log((aspect + 1e-3) / (tpl_aspects + 1e-3))) / 1.2
    )
    score = 0.85 * np.maximum(sims, 0.0) + 0.15 * aspect_score
    score[: idx["real_n"]] += REAL_BONUS

    order = np.argsort(-score)
    best_i = int(order[0])
    best_ch = idx["labels"][best_i]
    best_score = float(score[best_i])
    second = 0.0
    for i in order[1:]:
        if idx["labels"][int(i)] != best_ch:
            second = float(score[int(i)])
            break
    return best_ch, max(0.0, min(1.0, best_score)), max(0.0, best_score - second)


# ---------------------------------------------------------------- 数字解析

def _parse_token(token: str) -> Optional[int]:
    """把识别出的字符序列解析为整数，支持 , / . 千分位。"""
    if not token:
        return None
    body = token.strip()
    if not body:
        return None
    seps = [c for c in body if c in ",."]
    digits = [c for c in body if c.isdigit()]
    if not digits or len(digits) > 9:
        return None
    if seps:
        groups = body.replace(",", ".").split(".")
        legal = (1 <= len(groups[0]) <= 3 and groups[0].isdigit()
                 and all(g.isdigit() and len(g) == 3 for g in groups[1:]))
        if not legal:
            return None  # 非法的千分位分组，宁可判读不出也不猜
    return int("".join(digits))


def _drop_icons(boxes: Sequence[Tuple[int, int, int, int]],
                binary: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """丢掉圆形图标（金币）与贴边条带。

    实测真实截图：金币宽高比 0.95~1.00，数字字形 0.54~0.70，用宽高比即可分开；
    填充率反而重叠（金币 50~62%，"8" 64%），不能单独用来判定。
    """
    if not boxes:
        return []
    bh, bw = binary.shape[:2]
    ws = [b[2] for b in boxes]
    med_w = float(np.median(ws))
    out: List[Tuple[int, int, int, int]] = []
    for x, y, w, h in boxes:
        if w >= bw - 1 and h >= 0.8 * bh:           # 贴边的整条边框
            continue
        if w > 2.2 * med_w and w >= 0.85 * h:       # 明显更宽的图标
            continue
        if w >= 0.85 * h:                           # 接近 1:1 = 圆形图标
            continue
        out.append((x, y, w, h))
    return out


def _flatten(gray: np.ndarray) -> np.ndarray:
    """局部对比度归一化：除掉卡片高亮/遮挡造成的整体亮度差。"""
    bg = cv2.GaussianBlur(gray, (0, 0), 9.0)
    return cv2.divide(gray, bg, scale=128)


def _read_binary(binary: np.ndarray,
                 tpl: List[Tuple[str, np.ndarray, float]],
                 min_conf: float,
                 min_margin: float,
                 thr_map: Dict[str, float],
                 glyphs: Optional[List[Tuple[str, float, float]]] = None,
                 ) -> Tuple[Optional[int], str, float]:
    boxes = _drop_icons(_merge_close(segment(binary)), binary)
    if not boxes:
        return None, "", 0.0

    max_h = float(max(h for _, _, _, h in boxes))
    median_h = float(np.median([h for _, _, _, h in boxes]))
    # 明显偏矮的块（噪声碎片/标点残片）直接丢弃；逗号在 kept 里按字符匹配处理
    kept = sorted([b for b in boxes if b[3] >= 0.22 * max_h], key=lambda b: b[0])
    if not kept:
        return None, "", 0.0

    chars: List[str] = []
    scores: List[float] = []
    margins: List[float] = []
    thrs: List[float] = []
    big_gap_limit = max(8, int(1.25 * median_h))
    thr_default = float(np.median(list(thr_map.values()))) if thr_map else min_margin
    last_right = None
    for x, y, w, h in kept:
        if last_right is not None and x - last_right > big_gap_limit:
            return None, "", 0.0  # 间距过大 = 两段独立文本
        last_right = x + w
        patch = binary[y:y + h, x:x + w]
        ch, score, margin = match_char_detail(patch, tpl)
        if ch in ",.":
            ch, score, margin = ",", max(score, 0.6), max(margin, min_margin)
        elif h < 0.55 * max_h:
            # 偏矮又不像逗号：多半是噪声，跳过
            continue
        chars.append(ch)
        scores.append(score)
        margins.append(margin)
        thrs.append(max(min_margin, thr_map.get(ch, thr_default)))

    # 首尾的分隔符可能是 ROI 边缘噪点（如金币描边）被误判成逗号，去掉后再解析
    while chars and chars[0] in ",.":
        chars.pop(0)
        scores.pop(0)
        margins.pop(0)
        thrs.pop(0)
    while chars and chars[-1] in ",.":
        chars.pop()
        scores.pop()
        margins.pop()
        thrs.pop()
    if not chars:
        return None, "", 0.0
    token = "".join(chars)
    if not any(c.isdigit() for c in token):
        return None, "", 0.0
    score = float(sum(scores) / len(scores))
    if glyphs is not None:
        glyphs.extend(zip(chars, scores, margins))
    if score < min_conf:
        return (None, token, score)
    if any(m < t for m, t in zip(margins, thrs)):
        # 有字符"两个候选都很像"（如 3 vs 8）：整串拒识，宁可读不出也不猜
        return (None, "", score)
    return (_parse_token(token), token, score)


def read_detail(
    img: np.ndarray,
    box: Optional[Tuple[int, int, int, int]] = None,
    min_conf: float = DEFAULT_MIN_CONF,
    templates: Optional[List[Tuple[str, np.ndarray, float]]] = None,
    min_margin: float = DEFAULT_MIN_MARGIN,
    thr_map: Optional[Dict[str, float]] = None,
    norm: bool = True,
) -> Tuple[Optional[int], str, float]:
    """识别整数并返回 (value, raw_text, score)；读不到时 value=None。

    box 语义: (left, top, right, bottom) 绝对像素，与 contracts 的 px_box 一致。
    任一字符"最优与次优分差"小于阈值时整体拒识（返回空串），不猜。
    ``thr_map`` 为每字符自适应阈值（训练集 margin 的 p5，贴合本字模集合时自动带上），
    ``norm=True`` 时先试原始灰度、再试局部对比度归一化（高亮/遮挡 ROI 兜底）。
    """
    empty: Tuple[Optional[int], str, float] = (None, "", 0.0)
    if img is None or getattr(img, "size", 0) == 0:
        return empty
    roi = img
    if box is not None:
        l, t, r, b = [int(v) for v in box]
        h, w = img.shape[:2]
        l, r = max(0, min(l, w)), max(0, min(r, w))
        t, b = max(0, min(t, h)), max(0, min(b, h))
        if r <= l or b <= t:
            return empty
        roi = img[t:b, l:r]
    tpl = templates if templates is not None else load_templates()
    thr = thr_map if thr_map is not None else _ADAPTIVE.get(id(tpl), {})

    gray = to_gray(roi)
    fallback = empty
    variants = (gray, _flatten(gray)) if norm else (gray,)
    for g in variants:
        value, token, score = _read_binary(binarize(g), tpl, min_conf, min_margin, thr)
        if token and value is not None:
            return value, token, score
        if score > fallback[2]:
            fallback = (value, token, score)
    return fallback


def read_number(
    img: np.ndarray,
    box: Optional[Tuple[int, int, int, int]] = None,
    min_conf: float = DEFAULT_MIN_CONF,
    templates: Optional[List[Tuple[str, np.ndarray, float]]] = None,
) -> Optional[int]:
    """从图像（或指定 ROI）识别整数；识别不到或置信度低返回 None。"""
    return read_detail(img, box=box, min_conf=min_conf, templates=templates)[0]


class DigitReader:
    """惰性单例封装，首次调用才加载字模。"""

    _instance: Optional["DigitReader"] = None
    _lock = threading.Lock()

    def __init__(self, asset_dir: str = _ASSET_DIR, min_conf: float = DEFAULT_MIN_CONF):
        self.asset_dir = asset_dir
        self.min_conf = min_conf
        self._templates: Optional[List[Tuple[str, np.ndarray, float]]] = None

    @classmethod
    def instance(cls) -> "DigitReader":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def templates(self) -> List[Tuple[str, np.ndarray, float]]:
        if self._templates is None:
            self._templates = load_templates(self.asset_dir)
        return self._templates

    def read(self, img: np.ndarray, box: Optional[Tuple[int, int, int, int]] = None, min_conf: Optional[float] = None) -> Optional[int]:
        return self.detail(img, box=box, min_conf=min_conf)[0]

    def detail(
        self,
        img: np.ndarray,
        box: Optional[Tuple[int, int, int, int]] = None,
        min_conf: Optional[float] = None,
    ) -> Tuple[Optional[int], str, float]:
        return read_detail(
            img,
            box=box,
            min_conf=self.min_conf if min_conf is None else min_conf,
            templates=self.templates,
        )
