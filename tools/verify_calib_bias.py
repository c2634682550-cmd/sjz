"""量化「模板中心用未缩放尺寸」造成的匹配几何误差（P0 回归基准）。

不抓屏、不依赖游戏：在纯色画布上把购买/确认模板按当前客户区比例贴到
**内置坐标正中心**，然后分别用旧逻辑（原模板 th/tw）与新逻辑（缩放后
尺寸）算锚点偏移。

    python tools/verify_calib_bias.py

旧逻辑的报出偏移 = tpl_w*(1-r)/2，会被当成真实偏差写进 9 个区域；
新逻辑应接近 0（只剩像素取整的 ±1px）。
"""
import os
import sys

import cv2
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import autocalib as A      # noqa: E402
from core import region_table as RT  # noqa: E402
from core import vision              # noqa: E402

SIZES = ((1280, 720), (1600, 900), (1920, 1080),
         (2560, 1440), (2880, 1620), (3840, 2160))


class _Cfg:
    def template_path(self, name):
        return os.path.join(BASE, "templates", name + ".png")


def _frame(size):
    w, h = size
    frame = np.full((h, w, 3), 245, np.uint8)
    r = ((w / 1920.0) * (h / 1080.0)) ** 0.5
    boxes, _ = A._aspect_fix(A.boxes_for(size), size)
    for name in ("buy", "confirm"):
        tpl = cv2.imread(_Cfg().template_path(name))
        used = vision.scaled(tpl, r)
        x, y, bw, bh = boxes[name]
        tx = int(round(x + bw / 2.0 - used.shape[1] / 2.0))
        ty = int(round(y + bh / 2.0 - used.shape[0] / 2.0))
        frame[ty:ty + used.shape[0], tx:tx + used.shape[1]] = used
    return frame


def _old_offsets(frame, size):
    """旧实现：match() 的 loc 配原模板尺寸算中心。"""
    gray = vision.to_gray(frame)
    vision.set_target_screen(size, 96)
    boxes, _ = A._aspect_fix(A.boxes_for(size), size)
    offs = []
    for name in ("buy", "confirm"):
        rect = boxes[name]
        search = A._search_rect(rect, size)
        crop, (ox, oy) = A._crop(gray, search)
        tpl = vision.imread(_Cfg().template_path(name), cv2.IMREAD_GRAYSCALE)
        score, loc = vision.match(crop, tpl)
        th, tw = tpl.shape[:2]
        cx, cy = ox + loc[0] + tw / 2.0, oy + loc[1] + th / 2.0
        offs.append((cx - (rect[0] + rect[2] / 2.0),
                     cy - (rect[1] + rect[3] / 2.0)))
    xs = sorted(o[0] for o in offs)
    ys = sorted(o[1] for o in offs)
    return xs[len(xs) // 2], ys[len(ys) // 2]


def _center(rect):
    return rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0


def main():
    print("| 客户区 | 旧报出偏移 (x/y) | 旧点击中心误差 | 新报出偏移 (x/y) "
          "| 新点击中心误差 | 状态 |")
    print("|---|---|---|---|---|---|")
    for size in SIZES:
        frame = _frame(size)
        old = _old_offsets(frame, size)
        res = A.calibrate_image(frame, size=size, cfg=_Cfg())
        want = A.boxes_for(size)["buy"]
        got = res["boxes"]["buy"]
        new_err = (_center(got)[0] - _center(want)[0],
                   _center(got)[1] - _center(want)[1])
        print(f"| {size[0]}×{size[1]} | {old[0]:+.1f} / {old[1]:+.1f} "
              f"| {old[0]:+.0f} / {old[1]:+.0f} px "
              f"| {res['offset'][0]:+.1f} / {res['offset'][1]:+.1f} "
              f"| {new_err[0]:+.0f} / {new_err[1]:+.0f} px "
              f"| {res['status']} |")


if __name__ == "__main__":
    main()
