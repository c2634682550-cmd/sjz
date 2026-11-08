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

契约：对外可见的只能是**白名单里的方法**；窗口引用、悬浮窗引用、状态字段
（`_win` / `_overlay` / `_maxed`）一律下划线，不许出现在 JS 可见面。

`get_functions` 是嵌套函数，导不出来，所以这里断言的是它的**输入**：
`dir(obj)` 里那些不以 `_` 开头的名字。pywebview 就是照这个集合往下走的。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop  # noqa: E402

# 页面（web/index.html + overlay.js）实际会调用的方法白名单。
# 新增对外方法必须同时改这里 —— 多一个名字，pywebview 就多展开一条对象图。
ALLOWED_PUBLIC = {
    "minimize", "destroy",
    "win_minimize", "win_maximize", "win_close",
    "win_mini_enter", "win_mini_exit",
    "purchase_start", "purchase_stop",
    "overlay_toggle", "get_overlay_state", "overlay_size",
    "restart_elevated",
}


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

    def maximize(self):
        pass

    def restore(self):
        pass

    def destroy(self):
        pass


@pytest.fixture
def ctl():
    """一个已经把窗口塞进去的 WinCtl，和 desktop.main() 里的状态一致。"""
    c = desktop.WinCtl()
    c._win = FakeWindow()
    return c


@pytest.fixture
def no_hard_exit(monkeypatch):
    """把 `_hard_exit` 换成记账桩。

    `win_close/destroy` 是「关窗即真退出」的实现：它会起后台线程调
    `_hard_exit`（内部 TerminateProcess 杀掉**当前进程**）。测试进程里必须
    拦掉，否则跑完这几条用例 pytest 自己就被收掉了。
    """
    calls = []
    monkeypatch.setattr(desktop, "_hard_exit", lambda code=0: calls.append(code))
    return calls


def _public_names(obj):
    """pywebview 会往下递归的那些名字。"""
    return {n for n in dir(obj) if not n.startswith("_")}


def test_only_the_intended_methods_are_visible_to_js(ctl):
    """对外可见的只能是白名单方法。

    多出任何一个名字，pywebview 都会去展开它 —— 如果它指向 Window，
    就是上面说的白屏 + HTTP 全挂。
    """
    extra = _public_names(ctl) - ALLOWED_PUBLIC
    assert extra == set(), (
        f"WinCtl 对外暴露了 {sorted(extra)}；这些名字会被 pywebview 递归展开"
    )


def test_window_reference_is_not_visible(ctl):
    """窗口引用本身不能出现在可见名字里。

    旧代码用 `self.win`，于是 `win` 变成 JS 可调用的名字，而且会一路
    递归进 Window 的对象图。
    """
    names = _public_names(ctl)
    for bad in ("win", "events", "native", "overlay", "maxed"):
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


def _wait(cond, timeout=1.2):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_minimize_reaches_the_window_and_close_triggers_exit(no_hard_exit):
    """minimize 要真的调到窗口上；destroy 是「真退出」而不是 window.destroy。"""
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
    assert calls == ["minimize"], "minimize 必须落到窗口上；destroy 不再调 window.destroy"
    assert _wait(lambda: no_hard_exit), "destroy 必须排在退出收尾"


def test_win_close_requests_a_real_exit(no_hard_exit):
    """win_close 是页面上的关闭按钮：必须排一次硬退出，不能只是隐藏窗口。"""
    c = desktop.WinCtl()
    assert c.win_close() is True
    assert _wait(lambda: no_hard_exit), "win_close 没有触发退出收尾"
    assert no_hard_exit == [0]


def test_methods_are_safe_before_the_window_exists(no_hard_exit):
    """窗口还没建好时调用不能抛 —— 页面可能先于 create_window 完成加载。"""
    c = desktop.WinCtl()
    assert c.minimize() is True        # 没有窗口也返回 True：最小化是幂等空操作
    assert c.destroy() is True
