"""按字形认选项名 —— OCR 读不出的小字，用像素认。

为什么需要这个模块
------------------
在售页筛选面板里，稀有度「极品/优品」、成色 S/A/B/C、材质「其他」这几处
文字只有 14px 高（实测包围盒高 13-14px，灰度峰值 192、背景 18）。WinRT OCR
放大 1/2/3/4/6 倍、反相、归一化全部返回空。原来的兜底是「方框在第几行第几
列」查 ONS_GRID 反推名字 —— 那个方案依赖滚动位置推断，滚动一错名字就错，
而且错了没有任何信号。

字形模板匹配把名字还给像素：同一个选项在不同滚动位置渲染完全一致（实测
匹配分数 1.000），勾选与否也不改文字外观（勾选后 0.978，变的是方框填充）。
所以只要模板和选项一一对应，名字就和滚动位置、勾选状态都无关。

模板从哪来
----------
core/assets/glyphs/<选项名>.png，从真机截图切出来的固定窗口（80x18），
跟着代码走、随 exe 打包。注意和 templates/ 目录的区别：templates/ 是用户
自己框选的购买按钮模板，运行时生成、gitignore 掉；这里的是游戏字体渲染的
只读参考数据，必须提交。

窗口几何（实测）
----------------
文字左边缘 = 方框中心 x + 25，垂直居中于方框中心 y，字高 14px。
窗口取 x=cx+24 起、y=cy-9 起，80x18 —— 比最长选项「镭射贵金属」(71px)
略宽，且不会碰到方框（方框右边缘在 cx+14.5）或右列方框（左边缘 cx+230）。

阈值怎么定的
------------
同一选项跨滚动位置：1.000；勾选态：0.978；无模板的格子（颜色区那些）
最高只到 0.63。所以 MIN_SCORE=0.88 能干净分开。另外要求最佳与次优至少
差 MIN_GAP，避免「镭射」和「镭射贵金属」这种前缀关系在低分时互相抢。
"""
import os

import cv2

from . import vision

# 文字相对方框中心的偏移与窗口尺寸（见模块注释的实测数据）
X_OFF = 24
Y_OFF = -9
WIN_W = 80
WIN_H = 18
PAD = 4             # 搜索余量：允许 ±4px 位移，仍不会碰到方框

MIN_SCORE = 0.88
MIN_GAP = 0.10

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "glyphs")
_cache = None


def dir_path():
    """模板目录。打包后 __file__ 在 _MEIPASS 里，路径自然跟着走。"""
    return _DIR


def search_rect(cx, cy):
    """方框中心 → 模板搜索窗口 (x, y, w, h)，含 PAD 余量。

    测试要往合成图上贴模板时用这个函数，不要自己重算偏移 —— 偏移改了
    只改这里，测试跟着动。
    """
    return (cx + X_OFF - PAD, cy + Y_OFF - PAD,
            WIN_W + 2 * PAD, WIN_H + 2 * PAD)


def load(force=False):
    """读模板，返回 {选项名: 灰度图}。读不到就是空字典（不抛异常）。

    空字典是合法状态：用户可能删了 assets、或者这是裁剪过的分发。
    调用方据此降级回网格推断，而不是崩掉。
    """
    global _cache
    if _cache is not None and not force:
        return _cache
    out = {}
    d = dir_path()
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".png"):
                continue
            img = vision.imread(os.path.join(d, fn))
            if img is None or img.shape[0] != WIN_H or img.shape[1] != WIN_W:
                continue        # 尺寸不对的模板直接跳过，别让它污染匹配
            out[fn[:-4]] = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _cache = out
    return out


def identify(img, cx, cy):
    """认方框右边的字。返回 (名字, 分数, 与次优的差)；认不出返回 (None, 0, 0)。

    img 是 BGR 全屏图，cx/cy 是方框中心。窗口越界、没有模板、分数不够，
    一律返回 None —— 宁可交给网格推断，也不猜一个名字出去点错选项。
    """
    if img is None or not getattr(img, "size", 0):
        return None, 0.0, 0.0
    tpls = load()
    if not tpls:
        return None, 0.0, 0.0
    x, y, w, h = search_rect(cx, cy)
    if x < 0 or y < 0 or y + h > img.shape[0] or x + w > img.shape[1]:
        return None, 0.0, 0.0
    win = cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    scored = []
    for name, t in tpls.items():
        if t.shape[0] > win.shape[0] or t.shape[1] > win.shape[1]:
            continue
        scored.append((name, float(cv2.matchTemplate(
            win, t, cv2.TM_CCOEFF_NORMED).max())))
    if not scored:
        return None, 0.0, 0.0
    scored.sort(key=lambda kv: -kv[1])
    best, score = scored[0]
    gap = score - (scored[1][1] if len(scored) > 1 else 0.0)
    if score < MIN_SCORE or gap < MIN_GAP:
        return None, score, gap
    return best, score, gap
