"""模板匹配。只做一件事：在搜索区域里找模板，返回最高相关度和位置。

**模板必须和当前像素比例对齐**（审查 R09-02）。模板是在某块屏、某个
分辨率/缩放下截的；换机后模板的像素尺寸不等于画面里按钮的像素尺寸，
直接 `matchTemplate` 的分数会从 1.0 掉到 0.4x，低于阈值 0.85 →
购买键永远「没出现」，程序静默空转到结束。

这里的做法是**按尺度重采样模板**（不是盲目多尺度穷举）：

  · `templates/meta.json` 记录模板采集时的屏尺寸与 DPI；
  · `imread` / `to_gray` 把这份元数据绑到图像对象上；
  · `Screen` 每次构造把「当前屏尺寸 + 当前显示器 DPI」登记进来；
  · 匹配时按 `当前/采集` 的比例把模板缩放到当前像素比例，再匹配。

没有元数据的老模板退回多尺度搜索（同样能匹配上，只是每帧多几次
`matchTemplate`）。所有数值都在**物理像素**口径 —— DPI 感知由
`core/screen.py` 统一负责，这里不做任何坐标换算。
"""
import os
import weakref

import cv2
import numpy as np

# 纯色模板做归一化相关会除零，先挡掉
_MIN_STD = 1e-6

# 元数据缺失时的兜底尺度表。覆盖常见的 75% / 100% / 125% / 133% / 150%
# / 175% / 200% 渲染比例；先试 1.0，命中即停（正常机器的快路径）。
_MATCH_SCALES = (1.0, 0.75, 0.8, 0.9, 1.1, 1.25, 1.333333, 1.5, 1.75, 2.0)
# 单一尺度就达到这个分数时不再试其它尺度。0.93 远高于阈值 0.85，
# 不会把「勉强过线」的结果当成确定命中。
_FAST_ACCEPT = 0.93
# 元数据候选全部低于这个分数时，认为「几何均值/DPI 推算没命中真实比例」，
# 允许调用方做一次穷举兜底。3440×1440（21:9）就是典型：几何均值给出
# 1.5456，真实信箱比例是 1.3333，不兜底就永远匹配不上。
_DEEP_BELOW = 0.80
# 穷举兜底表：1.00–2.00 步进 0.05，外加 4:3 / 16:10 / 21:9 等真实比例
# （2.3333 不在网格里）。只在 `deep=True` 时使用，实时匹配不受影响。
DEEP_SCALES = tuple(
    sorted(set([round(1.0 + 0.05 * i, 4) for i in range(21)]
               + [0.5, 0.6667, 0.75, 0.9, 1.3333, 1.6, 1.7778, 2.3333, 2.4,
                  2.5, 3.0])))

# 当前采集目标的物理像素环境（由 core.screen.Screen 每次构造时登记）
_TARGET_SCREEN = [1920, 1080]
_TARGET_DPI = 96

# 图像对象 → 模板元数据。用 id + weakref 校验同一性，避免 numpy 数组
# 不可哈希的问题；数组被回收时自动清条目。
_BOUND = {}
# (id(模板), 尺度) → 缩放后的模板，同样是 weakref 缓存。
_SCALED = {}
# meta.json 目录 → (mtime, 解析结果)
_META_CACHE = {}


def imread(path, flags=cv2.IMREAD_COLOR):
    """读图，路径含中文也能用。

    OpenCV 的 imread 在 Windows 上按 ANSI 码页打开文件，路径里有中文
    （比如解压在「新建文件夹 (2)」下）会直接返回 None，而且不抛异常。
    走 numpy 的字节流再 imdecode 就绕开了编码问题。
    """
    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    img = cv2.imdecode(buf, flags)
    if img is not None:
        _bind_meta(img, _meta_for(path, _read_meta(path)))
    return img


def imwrite(path, img):
    """写图，路径含中文也能用。返回是否成功。

    同样的编码问题：imwrite 对中文路径静默返回 False，模板就永远存不上，
    界面上看起来「点了保存但没反应」。所以这里必须返回真实结果。
    """
    ok, buf = cv2.imencode(_ext(path), img)
    if not ok:
        return False
    try:
        with open(path, "wb") as f:
            f.write(buf.tobytes())
    except OSError:
        return False
    return True


def _ext(path):
    _, ext = os.path.splitext(path)
    return (ext or ".png").lower()



def _read_meta(path):
    """读模板旁的 `meta.json`（同目录一份，覆盖该目录所有模板）。

    结构（见 templates/meta.json）::

        {"schema": 1,
         "capture_screen": [1920, 1080], "dpi": 96,
         "templates": {"buy.png": {"capture_screen": [1920, 1080]},
                       "confirm.png": {...}}}

    读不出来就返回 None —— 老模板没有元数据，走多尺度兜底。
    """
    if not path:
        return None
    meta_path = os.path.join(os.path.dirname(os.path.abspath(path)), "meta.json")
    try:
        mtime = os.path.getmtime(meta_path)
    except OSError:
        return None
    hit = _META_CACHE.get(meta_path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        import json
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = None
    except (OSError, ValueError):
        data = None
    _META_CACHE[meta_path] = (mtime, data)
    return data


def forget_meta(meta_path=None):
    """让 meta 缓存失效。自愈写盘后必调：同一秒内写两次时，mtime 可能
    还没变，缓存会继续返回旧内容，于是「刚重采的模板还是按旧环境算比例」。"""
    if meta_path:
        _META_CACHE.pop(os.path.abspath(meta_path), None)
    else:
        _META_CACHE.clear()


def _meta_for(path, meta):
    """把「整目录一份 meta.json」收窄成「这张图自己的采集环境」。

    背景：`meta.json` 原本只有顶层 `capture_screen` / `dpi`，被同目录所有
    模板共享。模板自愈（`core/template_heal.py`）重采 **其中一张** 时会写
    `templates["buy.png"].capture_screen`；顶层不能跟着改 —— 一改，另一张
    没重采的模板也会被算成「在今天的屏上截的」，尺度对齐直接失效。
    所以这里按文件名合并：有单图条目就以单图为准，没有才回落到顶层
    （老 meta 没有 `templates` 条目，行为与之前完全一致）。
    """
    if not isinstance(meta, dict):
        return meta
    name = os.path.basename(path)
    entry = (meta.get("templates") or {}).get(name)
    if not isinstance(entry, dict):
        return meta
    merged = dict(meta)
    for key in ("capture_screen", "dpi"):
        if entry.get(key):
            merged[key] = entry[key]
    return merged


def _bind_meta(img, meta):
    """把元数据绑到图像对象；数组被回收时自动清理，不会越用越占内存。"""
    if img is None or meta is None:
        return
    key = id(img)
    try:
        ref = weakref.ref(img, lambda _r, k=key: _BOUND.pop(k, None))
    except TypeError:            # 不是可弱引用的对象，放弃绑定
        return
    _BOUND[key] = (ref, meta)


def _meta_of(img):
    if img is None:
        return None
    hit = _BOUND.get(id(img))
    if hit and hit[0]() is img:
        return hit[1]
    return None


def capture_screen_of(img):
    """模板采集时的物理屏尺寸 (w, h)，没有元数据返回 None。"""
    meta = _meta_of(img)
    if not isinstance(meta, dict):
        return None
    cs = meta.get("capture_screen")
    if isinstance(cs, (list, tuple)) and len(cs) == 2 and cs[0] and cs[1]:
        return int(cs[0]), int(cs[1])
    return None


def set_target_screen(size, dpi=96):
    """登记当前采集目标的物理像素环境（`Screen.__init__` 调用）。"""
    global _TARGET_DPI
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, IndexError, ValueError):
        return
    if w > 0 and h > 0:
        _TARGET_SCREEN[0], _TARGET_SCREEN[1] = w, h
    try:
        dpi = int(round(float(dpi)))
    except (TypeError, ValueError):
        dpi = 96
    _TARGET_DPI = dpi if dpi > 0 else 96


def _scale_candidates(needle):
    """模板应该缩放到哪些比例。有元数据时最多 3 个确定候选，没有就穷举。"""
    meta = _meta_of(needle)
    if not isinstance(meta, dict):
        return list(_MATCH_SCALES)
    cs = capture_screen_of(needle)
    if not cs:
        return list(_MATCH_SCALES)
    base_w, base_h = cs
    cur_w, cur_h = _TARGET_SCREEN
    # 分辨率比例取宽高的几何平均，等比缩放时正好等于缩放倍数
    ratio = ((cur_w / float(base_w)) * (cur_h / float(base_h))) ** 0.5
    try:
        base_dpi = float(meta.get("dpi") or 96)
    except (TypeError, ValueError):
        base_dpi = 96.0
    dpi_ratio = _TARGET_DPI / base_dpi if base_dpi > 0 else 1.0

    out = []
    # 信箱/黑边比例优先：ui 是 16:9，客户区不是 16:9 时真实缩放就是
    # min(宽比, 高比)，几何均值在这里会系统性偏大（3440×1440 → 1.5456）。
    fit = min(cur_w / float(base_w), cur_h / float(base_h))
    for s in (fit, ratio, dpi_ratio, ratio * dpi_ratio):
        s = round(s, 4)
        if 0.2 <= s <= 4.5 and all(abs(s - o) > 0.015 for o in out):
            out.append(s)
    return out or list(_MATCH_SCALES)


def scale_candidates(needle):
    """公开版：返回这张模板**会**被按哪些比例试。

    自检（`core/preflight.py`）和模板自愈（`core/template_heal.py`）必须
    用同一套候选比例，否则会出现「自检说放不下、引擎其实匹配得上」的
    假红。有元数据时是最多 4 个确定比例，没有则穷举老比例表。
    """
    return _scale_candidates(needle)


def target_screen():
    """当前登记的采集目标环境 `((w, h), dpi)`。"""
    return (int(_TARGET_SCREEN[0]), int(_TARGET_SCREEN[1])), int(_TARGET_DPI)


def _resized(needle, factor):
    """把模板重采样到目标比例；同一模板同一比例只算一次。

    用 `fx/fy` 形式而不是显式 dsize：dsize 会把宽高四舍五入后各自再算一遍
    缩放系数（303×70 → 379×88 时纵向实际是 1.257 而不是 1.25），亚像素
    采样位置偏移，细纹理的匹配分数会莫名低几个百分点（实测 0.929 vs
    0.972）。fx/fy 语义保证采样比例就是传入的 factor。
    """
    if abs(factor - 1.0) < 1e-4:
        return needle
    key = (id(needle), round(factor, 4))
    hit = _SCALED.get(key)
    if hit and hit[0]() is needle:
        return hit[1]
    interp = cv2.INTER_AREA if factor < 1.0 else cv2.INTER_CUBIC
    img = cv2.resize(needle, None, fx=factor, fy=factor, interpolation=interp)
    if img is None or img.size == 0:
        return needle
    try:
        ref = weakref.ref(needle, lambda _r, k=key: _SCALED.pop(k, None))
        _SCALED[key] = (ref, img)
    except TypeError:
        pass
    return img


def _match_once(haystack_gray, needle_gray):
    hh, hw = haystack_gray.shape[:2]
    nh, nw = needle_gray.shape[:2]
    if hh < nh or hw < nw:
        return 0.0, None
    if float(needle_gray.std()) < _MIN_STD:
        return 0.0, None
    result = cv2.matchTemplate(haystack_gray, needle_gray, cv2.TM_CCOEFF_NORMED)
    _, best, _, loc = cv2.minMaxLoc(result)
    if best != best:  # NaN
        return 0.0, None
    return float(best), loc


def match_scaled(haystack_gray, needle_gray, scales=None, deep=False):
    """尺度对齐的模板匹配，返回 (score, (x, y), 使用比例)。

    先按元数据算出的比例试（最多 4 个），达到 `_FAST_ACCEPT` 就停止；
    没有元数据的老模板走 `_MATCH_SCALES`。返回坐标仍然是**当前**
    haystack 的物理像素坐标。

    `deep=True` 时，候选比例全部低于 `_DEEP_BELOW` 会再穷举一遍
    `DEEP_SCALES`（1.00–2.00 步进 0.05 + 常见非 16:9 比例）。抓屏一次性
    自检（自动标定）该用；每帧实时匹配不要用，免得拖慢循环。
    """
    if needle_gray is None:
        return 0.0, None, 1.0
    best_score, best_loc, best_scale = 0.0, None, 1.0
    tried = list(scales if scales else _scale_candidates(needle_gray))
    for scale in tried:
        scaled = _resized(needle_gray, scale)
        score, loc = _match_once(haystack_gray, scaled)
        if score > best_score:
            best_score, best_loc, best_scale = score, loc, scale
        if score >= _FAST_ACCEPT:
            return best_score, best_loc, best_scale
    if deep and best_score < _DEEP_BELOW:
        for scale in DEEP_SCALES:
            if any(abs(scale - s) <= 0.015 for s in tried):
                continue
            scaled = _resized(needle_gray, scale)
            score, loc = _match_once(haystack_gray, scaled)
            if score > best_score:
                best_score, best_loc, best_scale = score, loc, scale
            if score >= _FAST_ACCEPT:
                break
    return best_score, best_loc, best_scale


def scaled(needle_gray, factor):
    """把模板重采样到 factor，返回实际图像。

    调用方算命中框中心时必须用这张图的 `shape`，不能用原模板尺寸：
    `match_scaled` 返回的 loc 对应的是缩放后的模板，尺寸差一半就是几十
    像素的系统性偏移（自动标定会把这种偏移当成真实偏差写进坐标）。
    """
    if needle_gray is None:
        return None
    return _resized(needle_gray, float(factor))


def to_gray(img):
    """任意通道数 → 灰度。

    以前只判 ndim==3，碰到 4 通道（BGRA，mss 在某些桌面合成模式下会吐）
    会走 COLOR_BGR2GRAY 然后抛「Invalid number of channels」。截图工具、
    测试夹具、cv2.imread(path, 0) 都可能给出非 3 通道的图。
    """
    if img is None:
        return None
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        out = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    elif img.shape[2] == 3:
        out = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        out = img[:, :, 0]                   # 2 通道这种畸形输入，取第一通道
    # 模板元数据跟着灰度图走（engine 是 imread → to_gray 两步，绑定要在
    # 转换后仍然有效，否则尺度对齐拿不到采集尺寸）
    _bind_meta(out, _meta_of(img))
    return out


def match(haystack_gray, needle_gray, scales=None):
    """返回 (score, (x, y))；坐标相对 haystack 左上角。找不到返回 (0.0, None)。

    尺度对齐由 `match_scaled` 处理；这里保持原来的二元返回，调用方
    （engine 的扫描循环、preflight 自检）无需改动。
    """
    score, loc, _ = match_scaled(haystack_gray, needle_gray, scales=scales)
    return score, loc


def expand(box, pad=0.25):
    """搜索区域比模板外扩一圈，否则模板在区域里只有一个位置，匹配度恒为 1。"""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    return {
        "x": max(0.0, x - w * pad),
        "y": max(0.0, y - h * pad),
        "w": min(1.0 - max(0.0, x - w * pad), w * (1 + 2 * pad)),
        "h": min(1.0 - max(0.0, y - h * pad), h * (1 + 2 * pad)),
    }


def upscale(img, factor):
    """放大图像，给小字 OCR 用。

    浮层选项是 15px 高的灰字，WinRT 原尺寸下会整行跳过笔画少的短项 ——
    实测「群星」被完全漏掉。放大 2x（INTER_CUBIC）后能正常读出。

    factor 为 1 时原样返回，不做多余拷贝（测试里的假图也走这条路）。
    """
    if factor <= 1 or img is None or getattr(img, "size", 0) == 0:
        return img
    return cv2.resize(img, None, fx=factor, fy=factor,
                      interpolation=cv2.INTER_CUBIC)


def box_brightness(img, x, y, half=13, thr=85):
    """复选框 (x,y) 周围 ±half 像素内的亮像素个数。

    用来判断勾选状态：空框只有边框（亮像素个位数），勾选后框内出现对勾
    （亮像素 300+）。实测空框 0、勾选后 304，差别极大，不会误判。
    """
    if img is None or getattr(img, "size", 0) == 0:
        return 0
    h, w = img.shape[:2]
    y0, y1 = max(0, y - half), min(h, y + half)
    x0, x1 = max(0, x - half), min(w, x + half)
    if y1 <= y0 or x1 <= x0:
        return 0
    g = img[y0:y1, x0:x1].astype("int16").mean(axis=2)
    return int((g > thr).sum())
