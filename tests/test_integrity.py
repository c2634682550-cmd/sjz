"""程序权限（UIPI）这一项的判断。

为什么值得单独守：控制台比游戏权限低时，鼠标键盘消息会被系统**静默拦掉**
—— 点击、F4 全都没反应，看起来像代码坏了。真机上就是这么查了两个小时
才发现的（实测 2026-10-23）。这个检查要保证「低权限」时一定报出来。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import preflight  # noqa: E402


def _facts(**kw):
    base = {"size": (1920, 1080), "ocr": True, "ocr_name": "WinRT zh-Hans",
            "clock": {"offset": 0.012, "at": 1700000000.0},
            "templates": {"buy": True, "confirm": True},
            "regions": {"buy": True, "confirm": True, "list": True, "clock": True},
            "page": "onsale",
            "integrity": {"ours": 0x3000, "game": 0x3000}}
    base.update(kw)
    return base


def _by_id(checks, cid):
    return next(c for c in checks if c["id"] == cid)


def test_same_privileges_pass():
    checks = preflight.evaluate(_facts(), "list")
    assert preflight.all_ok(checks), [(c["label"], c["detail"]) for c in checks if not c["ok"]]
    c = _by_id(checks, "integrity")
    assert c["ok"] and "管理员" in c["detail"]


def test_lower_privileges_block_the_start():
    """游戏是管理员、我们是普通权限 → 必须拦下来并说清怎么修。"""
    checks = preflight.evaluate(_facts(integrity={"ours": 0x2000, "game": 0x3000}), "list")
    c = _by_id(checks, "integrity")
    assert not c["ok"]
    assert "普通" in c["detail"] and "管理员" in c["detail"]
    assert c["fix"] and "管理员" in c["fix"]["hint"], "得告诉人怎么修"
    assert not preflight.all_ok(checks), "权限不够还放行 = 白跑一轮"


def test_unknown_privileges_do_not_block():
    """读不到完整性级别时（比如别的系统上）不该拦 —— 拦错了比不拦更烦。"""
    checks = preflight.evaluate(_facts(integrity={}), "list")
    assert _by_id(checks, "integrity")["ok"]
    assert preflight.all_ok(checks)
