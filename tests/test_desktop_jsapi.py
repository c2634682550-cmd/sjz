"""桌面版 js_api 对象的属性名约束。

pywebview 的 `get_functions()`（`webview/util.py` 里 `inject_pywebview` 的
嵌套函数）会把 `js_api` 对象的**所有不以 `_` 开头的属性**递归展开，想把它们
都变成 JS 可调用的东西。`WinCtl` 必须持有一个指向 pywebview `Window` 的引用
（才能 minimize/destroy），而 `Window` 的对象图里有大量互相引用 —— 一旦这个
属性是公开的，递归展开会长时间占住 GIL。

实测表现（Python 3.11 和 3.14 都复现，源码运行和打包 exe 都一样）：

    窗口能开出来，但页面白屏，所有 HTTP 请求超时
    （后台线程连 Thread.start() 都等不到 GIL）

修法是给那个引用加下划线前缀。这个改动很容易被「顺手改成 win 更清楚」而
改回去，所以钉住 `WinCtl` 对外的可见属性。

`get_functions` 是嵌套函数，导不出来，所以这里断言的是它的**输入**：
`dir(obj)` 里那些不以 `_` 开头的名字。pywebview 就是照这个集合往下走的。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop  # noqa: E402


class FakeWindow:
    """足够像 pywebview 的 Window：有公开属性、有指向自己的引用。

    真实的 Window 对象图更大，但「公开属性 + 自引用」这两点就足以让
    递归展开一直走下去 —— 这正是卡死的原因。
    """

    def __init__(self):
        self.title = "fake"
        self.width = 1180
        self.events = self          # 自引用
        self.native = self

    def minimize(self):
        pass

    def destroy(self):
        pass


@pytest.fixture
def ctl():
    """一个已经把窗口塞进去的 WinCtl，和 desktop.main() 里的状态一致。"""
    c = desktop.WinCtl()
    c._win = FakeWindow()
    return c


def _public_names(obj):
    """pywebview 会往下递归的那些名字。"""
    return {n for n in dir(obj) if not n.startswith("_")}


def test_only_the_intended_methods_are_visible_to_js(ctl):
    """对外可见的只有 minimize / destroy。

    多出任何一个名字，pywebview 都会去展开它 —— 如果它指向 Window，
    就是上面说的白屏 + HTTP 全挂。
    """
    extra = _public_names(ctl) - {"minimize", "destroy"}
    assert extra == set(), (
        f"WinCtl 对外暴露了 {sorted(extra)}；这些名字会被 pywebview 递归展开"
    )


def test_window_reference_is_not_visible(ctl):
    """窗口引用本身不能出现在可见名字里。

    旧代码用 `self.win`，于是 `win` 变成 JS 可调用的名字，而且会一路
    递归进 Window 的对象图。
    """
    names = _public_names(ctl)
    for bad in ("win", "events", "native"):
        assert bad not in names, f"WinCtl.{bad} 对外可见"


def test_visible_attribute_values_are_all_plain_functions(ctl):
    """可见属性只能是普通方法，不能是对象引用。

    这条比逐个名字更结实：以后往 WinCtl 上挂任何新字段，
    只要它是对象（而不是方法），这里就会失败。
    """
    for name in _public_names(ctl):
        attr = getattr(ctl, name)
        assert inspect.ismethod(attr) or inspect.isfunction(attr), (
            f"WinCtl.{name} 是 {type(attr).__name__}，不是方法；"
            f"pywebview 会递归展开它"
        )


def test_minimize_and_destroy_reach_the_window():
    """两个方法必须真的调到窗口上，不能因为改名而变成空操作。"""
    calls = []

    class W:
        def minimize(self):
            calls.append("minimize")

        def destroy(self):
            calls.append("destroy")

    c = desktop.WinCtl()
    c._win = W()
    c.minimize()
    c.destroy()
    assert calls == ["minimize", "destroy"]


def test_methods_are_safe_before_the_window_exists():
    """窗口还没建好时调用不能抛 —— 页面可能先于 create_window 完成加载。"""
    c = desktop.WinCtl()
    c.minimize()
    c.destroy()
