"""筛选面板「取消勾选」的退路：区块不允许一项都不选时改选「所有…」。

真机实测（2026-10-21）：稀有度和成色这两组**不允许一项都不选**。勾上
「极品」后「所有稀有度」自动取消，再点「极品」没有任何反应 —— 点文字、
点字母本身、点方框中心都一样，不是点击位置的问题。材质和颜色组没有
这个限制（取消「速度线」后可以一项都不选）。

`ons_check(name, want=False)` 遇到前者时改选该区块的「所有…」，语义等价：
调用方要的是「不要再按极品过滤」。

假对象照抄真机这条规则 —— 不模拟的话退路永远走不到，测试等于没测。
"""
import numpy as np
import pytest

from core import glyphs
from core.nav import NavError, Navigator

# 真机实测坐标（1920x1080 窗口化，面板滚到顶部）
RARITY_ALL = ("所有稀有度", 1396, 340)
RARITY_JP = ("极品", 1641, 340)
RARITY_YP = ("优品", 1396, 396)
COND_ALL = ("所有成色", 1396, 515)
COND_A = ("A", 1396, 571)
COND_B = ("B", 1641, 571)
COND_C = ("C", 1396, 627)
MAT_ALL = ("不限", 1396, 745)
MAT_SPEED = ("速度线", 1641, 745)

# 这两组不允许「一项都不选」；材质/颜色可以
NEEDS_ONE = {"rarity", "condition"}


class GroupedPanel:
    """假面板：方框 + 右边一行字，并照抄「区块至少留一项」的真机规则。"""

    def __init__(self, rows, checked=()):
        self.rows = list(rows)
        self.checked = set(checked)
        self.clicked = []
        self.said = []
        self.dry_run = False
        self.mouse = self
        self.pt = lambda point: point
        self.say = lambda msg, level="info": self.said.append(msg)

    # -- 面板长什么样 ------------------------------------------------

    def _snap(self):
        img = np.zeros((1080, 1920, 3), np.uint8)
        for name, cx, cy in self.rows:
            x0, y0 = cx - 14, cy - 14
            img[y0:y0 + 29, x0:x0 + 29] = 100
            img[y0 + 2:y0 + 27, x0 + 2:x0 + 27] = 30
            if name not in self.checked:
                continue
            # 真机两种已勾选外观：实心白块（不限，约 196 白像素）
            # 和对勾（具体档位，约 65 白像素）
            if name == "不限":
                img[y0 + 7:y0 + 22, x0 + 7:x0 + 22] = 255
            else:
                import cv2
                cv2.line(img, (x0 + 8, y0 + 16), (x0 + 13, y0 + 21),
                         (255, 255, 255), 3)
                cv2.line(img, (x0 + 13, y0 + 21), (x0 + 23, y0 + 9),
                         (255, 255, 255), 3)
        return img

    def _ocr_words(self, crop):
        # crop 是 img[30:1080, 1360:1900]，换算回整屏坐标
        return [{"x": cx + 40 - 1360, "y": cy - 30 - 8, "w": 30, "h": 16,
                 "text": name}
                for name, cx, cy in self.rows]

    # -- 点击行为 ----------------------------------------------------

    def click(self, x, y, verify=True):
        self.clicked.append((x, y))
        row = None
        for name, cx, cy in self.rows:
            if abs(y - cy) <= 15 and abs(x - (cx + 40)) <= 40:
                row = (name, cx, cy)
                break
        if row is None:
            return True
        name, cx, cy = row
        block = self._block_of(cy)
        same = {n for n, _x, y2 in self.rows if self._block_of(y2) == block}
        if name in self.checked:
            # 真机规则：该区块只剩这一项时点不动
            if block in NEEDS_ONE and (same & self.checked) == {name}:
                return True
            self.checked.discard(name)
        else:
            self.checked.add(name)
            self.checked -= (same - {name})     # 选具体档位会顶掉「所有…」
        return True

    def _block_of(self, cy):
        """按竖直位置反推区块（与生产代码的切组规则一致：空隙 >= 100 分组）。"""
        tops = sorted({c[2] for c in self.rows})
        groups = [[tops[0]]]
        for t in tops[1:]:
            if t - groups[-1][-1] >= 100:
                groups.append([t])
            else:
                groups[-1].append(t)
        order = ["rarity", "condition", "material", "color", "more"]
        for i, g in enumerate(groups):
            if cy in g:
                return order[i]
        return None


def _nav(panel):
    n = Navigator.__new__(Navigator)
    n._snap = panel._snap
    n._ocr_words = panel._ocr_words
    n.mouse = panel.mouse
    n.pt = panel.pt
    n.say = panel.say
    n.dry_run = False
    # 只桩 _ons_wheel：真实现会移动真实鼠标、滚动游戏窗口
    n._ons_wheel = lambda notches, at=None: True
    return n


def _basic_panel(checked=()):
    # 行距必须照抄真机（56px，区块间 >= 100px）—— 少一行 A/B 会让
    # 515→627 的空隙变成 112px，被当成区块边界，成色组就被拆成两组。
    return GroupedPanel([RARITY_ALL, RARITY_JP, RARITY_YP,
                         COND_ALL, COND_A, COND_B, COND_C,
                         MAT_ALL, MAT_SPEED], checked=checked)


# ---------------------------------------------------------------- 测试

def test_last_rarity_option_falls_back_to_all():
    """稀有度只剩「极品」时取消不了，应改选「所有稀有度」。"""
    p = _basic_panel(checked={"极品"})
    n = _nav(p)
    assert n.ons_check("极品", want=False) is True
    assert "所有稀有度" in p.checked
    assert "极品" not in p.checked


def test_last_condition_option_falls_back_to_all():
    """成色组同样不允许一项都不选。"""
    p = _basic_panel(checked={"C"})
    n = _nav(p)
    assert n.ons_check("C", want=False) is True
    assert "所有成色" in p.checked
    assert "C" not in p.checked


def test_uncheck_direct_when_sibling_still_on():
    """同组还有别的项勾着时，直接取消就行，不该去点「所有…」。

    这一条防的是退路被滥用：点不动就无脑改选「所有…」会静默改掉
    调用方的筛选条件。
    """
    p = _basic_panel(checked={"极品", "优品"})
    n = _nav(p)
    assert n.ons_check("极品", want=False) is True
    assert "极品" not in p.checked
    assert "优品" in p.checked
    assert "所有稀有度" not in p.checked


def test_material_allows_empty_group():
    """材质组允许一项都不选，直接取消，不走退路。"""
    p = _basic_panel(checked={"速度线"})
    n = _nav(p)
    assert n.ons_check("速度线", want=False) is True
    assert "速度线" not in p.checked
    assert "不限" not in p.checked


def test_fallback_never_picks_a_sibling_option():
    """退路只能点该区块的「所有…」，不能顺手点成另一个具体档位。

    点错成「优品」就是买错货的级别。
    """
    p = _basic_panel(checked={"极品"})
    n = _nav(p)
    n.ons_check("极品", want=False)
    assert "优品" not in p.checked


def test_still_raises_when_fallback_impossible(monkeypatch):
    """连「所有…」都点不动时必须报错，不能假装成功。"""
    p = _basic_panel(checked={"极品"})
    n = _nav(p)
    # 让「所有稀有度」那一行从面板上消失
    p.rows = [r for r in p.rows if r[0] != "所有稀有度"]
    with pytest.raises(NavError):
        n.ons_check("极品", want=False)
