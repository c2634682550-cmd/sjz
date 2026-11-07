"""成交截图 + 品牌水印（后台线程，不进主链路）。

主线程只做一件事：把「游戏客户区矩形 + 本次成交的真实数据」投递到队列。
抓屏、绘图、PNG 编码全部在独立工作线程里做，成交路径不等待、不抓屏、不做
任何磁盘 IO；任何失败只回一句日志，绝不抛回主流程。

设计约束（对应需求）：
  · 只截游戏窗口客户区（矩形由 engine 的 Screen.base_rect 给出），不含桌面。
  · 时间点取成交判定成功那一刻：任务在 `_record(result="hit")` 里立刻投递，
    紧接着才收奖励弹窗，工作线程拿到的是那一刻前后的客户区画面。
  · 中文字体只用系统已装的微软雅黑（msyh.ttc / msyhbd.ttc），不用位图字体。
  · 数据全部来自本次成交的真实值；缺项统一写「—」，不编造。
"""
import os
import queue
import re
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import DATA_DIR, ROOT

# ---------------------------------------------------------------- 资源与配置

FONT_REG = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
_FONT_FALLBACK = (r"C:\Windows\Fonts\msyh.ttf",
                  r"C:\Windows\Fonts\simhei.ttf",
                  r"C:\Windows\Fonts\simsun.ttc")

DASH = "—"
DEFAULT_BRAND = "三角洲 · 抢购控制台"

_BAR_BG = (9, 11, 17)
_BAR_LINE = (96, 106, 255)
_PILL_BG = (86, 108, 255)
_WHITE = (255, 255, 255)
_GREY = (176, 183, 196)
_LIGHT = (232, 237, 245)
_TOP_GREY = (196, 202, 212)


def _asset_path(name, configured=""):
    """资源插槽：配置优先；没配置就用 `<ROOT>/assets/shot/<name>`。"""
    if configured:
        p = os.path.expanduser(str(configured))
        return p if os.path.isabs(p) else os.path.join(ROOT, p)
    return os.path.join(ROOT, "assets", "shot", name)


def settings(cfg):
    """把配置收敛成渲染参数；`shot.enabled` 与旧的 `save_shots` 同时生效。"""
    s = cfg.get("shot") or {}
    enabled = bool(s.get("enabled", True)) and bool(cfg.get("save_shots", True))
    d = str(s.get("dir") or "")
    if d:
        d = os.path.expanduser(d)
        if not os.path.isabs(d):
            d = os.path.join(ROOT, d)
    else:
        d = os.path.join(DATA_DIR, "shots")
    return {
        "enabled": enabled,
        "dir": d,
        "watermark": bool(s.get("watermark", True)),
        "keep_raw": bool(s.get("keep_raw", True)),
        "brand": str(s.get("brand") or DEFAULT_BRAND),
        "qgroup": str(s.get("qgroup") or ""),
        "uid": str(s.get("uid") or ""),
        "avatar": _asset_path("avatar.png", s.get("avatar")),
        "logo": _asset_path("logo.png", s.get("logo")),
    }


# ---------------------------------------------------------------- 命名

def _clean(text, limit=40):
    v = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text or "")).strip("_")
    return (v[:limit] or "成交")


def _price_text(price):
    if price in (None, ""):
        return DASH
    if isinstance(price, (int, float)):
        return ("%g" % price)
    return _clean(price, 20)


def file_name(meta):
    ts = float(meta.get("ts") or time.time())
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    return f"{stamp}_{_clean(meta.get('item'))}_{_price_text(meta.get('price'))}.png"


def raw_name(name):
    stem, ext = os.path.splitext(name)
    return f"{stem}_raw{ext or '.png'}"


# ---------------------------------------------------------------- 字体

_FONT_CACHE = {}
_FONT_LOCK = threading.Lock()


def font(size, bold=False):
    size = max(8, int(round(size)))
    key = (size, bool(bold))
    with _FONT_LOCK:
        got = _FONT_CACHE.get(key)
        if got is not None:
            return got
    paths = []
    if bold:
        paths.append(FONT_BOLD)
    paths.append(FONT_REG)
    paths.extend(_FONT_FALLBACK)
    picked = None
    for p in paths:
        if os.path.isfile(p):
            picked = p
            break
    try:
        f = ImageFont.truetype(picked, size) if picked else ImageFont.load_default()
    except Exception:                       # noqa: BLE001 字体坏了不能炸截图
        f = ImageFont.load_default()
    with _FONT_LOCK:
        _FONT_CACHE[key] = f
    return f


def fonts_available():
    return os.path.isfile(FONT_REG) and os.path.isfile(FONT_BOLD)


# ---------------------------------------------------------------- 渲染

def _text(draw, xy, text, f, fill, anchor="la", stroke=0, stroke_fill=(0, 0, 0)):
    if stroke:
        draw.text(xy, text, font=f, fill=fill, anchor=anchor,
                  stroke_width=int(stroke), stroke_fill=stroke_fill)
    else:
        draw.text(xy, text, font=f, fill=fill, anchor=anchor)


def _load_rgba(path, size=None):
    try:
        im = Image.open(path).convert("RGBA")
    except Exception:                       # noqa: BLE001 资源缺失/损坏按缺省
        return None
    if size:
        im = im.resize((int(size), int(size)), Image.LANCZOS)
    return im


def _circle_avatar(base, path, box, ring_w):
    im = _load_rgba(path, box[2])
    if im is None:
        return False
    d = int(box[2])
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, d - 1, d - 1), fill=255)
    base.paste(im, (int(box[0]), int(box[1])), mask)
    draw = ImageDraw.Draw(base)
    draw.ellipse((box[0] - ring_w, box[1] - ring_w,
                  box[0] + d + ring_w - 1, box[1] + d + ring_w - 1),
                 outline=(118, 140, 255, 255), width=int(ring_w))
    return True


_WM_LOCK = threading.Lock()
_WM_CACHE = {}


def _watermark_layer(w, h, brand, k):
    """整图斜向平铺水印：−30°、约 4~6% 透明度。

    平铺层只跟分辨率/产品名有关，第一次算完就缓存：一次 1080p 旋转要
    100ms 级，缓存放后台线程也省 CPU，游戏里不抢那一下的算力。
    """
    key = (int(w), int(h), brand, round(float(k), 3))
    with _WM_LOCK:
        got = _WM_CACHE.get(key)
    if got is not None:
        return got
    d = int(np.hypot(w, h)) + 1
    layer = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    f = font(46 * k)
    step_x = max(300, int(760 * k))
    step_y = max(140, int(250 * k))
    alpha = 13                              # 13/255 ≈ 5%，只做来源标识不压画面
    fill = (255, 255, 255, alpha)
    row = 0
    y = -step_y
    while y < d + step_y:
        x = -step_x + (step_x // 2 if row % 2 else 0)
        while x < d + step_x:
            draw.text((x, y), brand, font=f, fill=fill)
            x += step_x
        y += step_y
        row += 1
    layer = layer.rotate(-30, resample=Image.BICUBIC, center=(d / 2, d / 2))
    left = (d - w) // 2
    top = (d - h) // 2
    layer = layer.crop((left, top, left + w, top + h))
    with _WM_LOCK:
        if len(_WM_CACHE) >= 4:
            _WM_CACHE.clear()
        _WM_CACHE[key] = layer
    return layer


def render(img_rgb, meta, opts):
    """给一帧 RGB 打上底部通栏 + 斜向水印 + 左上角小字，返回 RGB。"""
    h, w = img_rgb.shape[:2]
    k = h / 900.0
    base = Image.fromarray(img_rgb).convert("RGBA")

    brand = str(meta.get("brand") or opts.get("brand") or DEFAULT_BRAND)
    qgroup = str(opts.get("qgroup") or "").strip()
    uid = str(meta.get("uid") or opts.get("uid") or DASH)
    ts = float(meta.get("ts") or time.time())

    if opts.get("watermark", True):
        base = Image.alpha_composite(
            base, _watermark_layer(w, h, brand, k))

    bar_h = max(48, int(round(h * 0.09)))
    top = h - bar_h
    draw = ImageDraw.Draw(base)
    # 底部通栏：近黑底 + 顶部一条蓝紫细线
    draw.rectangle((0, top, w, h), fill=_BAR_BG + (247,))
    draw.rectangle((0, top - max(2, int(2 * k)), w, top),
                   fill=_BAR_LINE + (230,))

    pad = int(18 * k)
    # ---- 左：圆形头像（缺资源就纯文字）→ 产品名 / Q群
    avatar_d = int(60 * k)
    avatar_cy = top + bar_h // 2
    has_avatar = False
    if opts.get("avatar"):
        has_avatar = _circle_avatar(
            base, opts["avatar"],
            (pad, avatar_cy - avatar_d // 2, avatar_d),
            max(2, int(3 * k)))
    x_text = pad + avatar_d + int(16 * k) if has_avatar else pad + int(4 * k)
    _text(draw, (x_text, top + bar_h * 0.30), brand, font(21 * k, True),
          _WHITE + (255,), anchor="lm", stroke=max(1, int(1.2 * k)))
    _text(draw, (x_text, top + bar_h * 0.68),
          f"Q群 {qgroup or DASH}", font(13 * k), _LIGHT + (235,),
          anchor="lm", stroke=max(1, int(1 * k)))

    # ---- 中：蓝色状态块 + 物品名大字 + 副行
    badge = str(meta.get("badge") or "成交")
    f_pill = font(20 * k, True)
    pill_h = int(30 * k)
    pill_w = int(draw.textlength(badge, font=f_pill)) + int(22 * k)
    f_item = font(31 * k, True)
    item = str(meta.get("item") or DASH)
    item_w = draw.textlength(item, font=f_item)
    total = pill_w + int(10 * k) + item_w
    x0 = w / 2 - total / 2
    cy_item = top + bar_h * 0.30
    draw.rounded_rectangle((x0, cy_item - pill_h / 2,
                            x0 + pill_w, cy_item + pill_h / 2),
                           radius=pill_h / 2, fill=_PILL_BG + (255,))
    _text(draw, (x0 + pill_w / 2, cy_item), badge, f_pill,
          _WHITE + (255,), anchor="mm")
    _text(draw, (x0 + pill_w + int(10 * k), cy_item), item, f_item,
          _WHITE + (255,), anchor="lm", stroke=max(1, int(1.2 * k)))

    tier = str(meta.get("tier") or DASH)
    price = _price_text(meta.get("price"))
    ms = meta.get("elapsed_ms")
    ms_txt = f"{float(ms):.3f} ms" if isinstance(ms, (int, float)) else DASH
    rnd = meta.get("round")
    rnd_txt = str(int(rnd)) if isinstance(rnd, (int, float)) else DASH
    sub = f"{tier} · ¥{price} · 延迟 {ms_txt} · 第 {rnd_txt} 轮"
    _text(draw, (w / 2, top + bar_h * 0.67), sub, font(15 * k),
          _LIGHT + (238,), anchor="mm", stroke=max(1, int(1 * k)))

    # ---- 右：本地时间 / 授权卡尾号
    x_right = w - pad - int(6 * k)
    _text(draw, (x_right, top + bar_h * 0.30),
          time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
          font(18 * k), _WHITE + (255,), anchor="rm",
          stroke=max(1, int(1.1 * k)))
    _text(draw, (x_right, top + bar_h * 0.68),
          str(meta.get("card") or DASH), font(14 * k), _LIGHT + (235,),
          anchor="rm", stroke=max(1, int(1 * k)))

    # ---- 左上角：UID + 紧凑时间戳
    _text(draw, (int(14 * k), int(8 * k)),
          f"CN UID:{uid}_{time.strftime('%Y%m%d%H%M', time.localtime(ts))}",
          font(12 * k), _TOP_GREY + (235,), anchor="la",
          stroke=max(1, int(0.8 * k)))

    return np.array(base.convert("RGB"))


# ---------------------------------------------------------------- 后台工作线程

_LIC_LOCK = threading.Lock()
_LIC_CACHE = {"at": 0.0, "info": {}}


def _license_fields():
    """授权卡尾号（离线读本地许可状态，60s 缓存；读不到就「—」）。"""
    now = time.time()
    with _LIC_LOCK:
        if now - _LIC_CACHE["at"] < 60.0 and _LIC_CACHE["info"]:
            return dict(_LIC_CACHE["info"])
    info = {}
    try:
        from . import license as _license
        s = _license.summary()
        lid = re.sub(r"[^0-9A-Za-z]", "", str(s.get("lic_id") or ""))
        tail = lid[-4:].upper()
        if tail:
            label = "试用卡" if s.get("trial") else "授权卡"
            info = {"card": f"{label} ****{tail}"}
        else:
            info = {"card": DASH}
    except Exception:                       # noqa: BLE001 许可读不到不影响截图
        info = {"card": DASH}
    with _LIC_LOCK:
        _LIC_CACHE["at"] = now
        _LIC_CACHE["info"] = dict(info)
    return info


class Worker:
    """单线程截图队列：drop-oldest，绝不阻塞投递方。"""

    def __init__(self, say=None):
        self._say = say or (lambda msg, level="info": None)
        self._q = queue.Queue(maxsize=8)
        self._closed = threading.Event()
        self._sct = None
        self._thread = threading.Thread(target=self._loop, name="shot-proof",
                                        daemon=True)
        self._thread.start()

    def submit(self, rect, meta, opts):
        task = (dict(rect), dict(meta), dict(opts))
        try:
            self._q.put_nowait(task)
            return True
        except queue.Full:
            pass
        try:                                # 队列满：丢最旧的一张，保住最新成交
            self._q.get_nowait()
        except queue.Empty:
            return False
        try:
            self._q.put_nowait(task)
            return True
        except queue.Full:
            return False

    def close(self, timeout=1.5):
        self._closed.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._close_sct()

    def drain(self, timeout=3.0):
        """等队列里的图都写完（测试/退出前用；主链路不调用）。"""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    # ---- 线程内部 ----
    def _loop(self):
        try:
            import mss
            factory = getattr(mss, "MSS", None) or mss.mss
            self._sct = factory()
        except Exception:                   # noqa: BLE001 抓屏不可用只影响截图
            self._sct = None
        while not self._closed.is_set():
            try:
                task = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if task is None:
                    break
                rect, meta, opts = task
                self._handle(rect, meta, opts)
            finally:
                self._q.task_done()
        self._close_sct()

    def _close_sct(self):
        sct, self._sct = self._sct, None
        if sct is not None:
            try:
                sct.close()
            except Exception:               # noqa: BLE001
                pass

    def _grab(self, rect):
        if self._sct is None:
            raise RuntimeError("截屏组件不可用")
        shot = self._sct.grab({
            "left": int(rect["left"]), "top": int(rect["top"]),
            "width": max(1, int(rect["width"])),
            "height": max(1, int(rect["height"])),
        })
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
            shot.height, shot.width, 4)
        return arr[:, :, :3].copy()         # BGR

    def _handle(self, rect, meta, opts):
        try:
            frame_bgr = self._grab(rect)
            rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
            meta.update(_license_fields())
            meta["brand"] = opts.get("brand") or DEFAULT_BRAND
            name = meta.get("_name") or file_name(meta)
            meta["_name"] = name
            d = opts["dir"]
            os.makedirs(d, exist_ok=True)
            out = render(rgb, meta, opts) if opts.get("watermark", True) else rgb
            Image.fromarray(out).save(os.path.join(d, name), "PNG",
                                      compress_level=3)
            if opts.get("watermark", True) and opts.get("keep_raw", True):
                Image.fromarray(rgb).save(
                    os.path.join(d, raw_name(name)), "PNG", compress_level=3)
            try:
                from . import retention
                retention.prune_dir(d, retention.SHOTS_MAX_FILES,
                                    retention.SHOTS_MAX_BYTES,
                                    retention.SHOTS_PRUNE_EVERY)
            except Exception:               # noqa: BLE001 清理失败不影响结果
                pass
            self._say(f"已保存截图：{name}", "hit")
        except Exception as e:              # noqa: BLE001 截图失败只记日志
            self._say(f"截图失败：{type(e).__name__}", "warn")


def preview(image_path, out_path, meta=None, opts=None):
    """把一张已有图片按同一套样式打水印（生成样例图 / 自检用）。"""
    import cv2
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError(f"读不到图片：{image_path}")
    cfg = opts or {}
    s = {
        "watermark": bool(cfg.get("watermark", True)),
        "brand": cfg.get("brand") or DEFAULT_BRAND,
        "qgroup": cfg.get("qgroup") or "",
        "uid": cfg.get("uid") or DASH,
        "avatar": _asset_path("avatar.png", cfg.get("avatar")),
    }
    m = dict({
        "item": "AUG突击步枪-天命",
        "tier": "史诗",
        "price": 230,
        "elapsed_ms": 827.300,
        "round": 95,
        "ts": time.time(),
        "uid": s["uid"],
        "badge": "进池",
        "brand": s["brand"],
    })
    m.update(meta or {})
    out = render(np.ascontiguousarray(img[:, :, ::-1]), m, s)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    Image.fromarray(out).save(out_path, "PNG", compress_level=3)
    return out_path
