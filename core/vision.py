"""模板匹配。只做一件事：在搜索区域里找模板，返回最高相关度和位置。"""
import os

import cv2
import numpy as np

# 纯色模板做归一化相关会除零，先挡掉
_MIN_STD = 1e-6


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
    return cv2.imdecode(buf, flags)


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



def to_gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def match(haystack_gray, needle_gray):
    """返回 (score, (x, y))；坐标相对 haystack 左上角。找不到返回 (0.0, None)。"""
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
