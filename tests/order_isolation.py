"""跨模块共享可变状态的统一复位（随机顺序隔离网）。

背景：`grade_stop`（按品阶停止）/ `click_burst`（连点参数）/ `time_sync`
（时间同步）三条用例只在 `pytest-random-order` 乱序时失败，顺序跑全绿。
根因不是抖动，而是 core 里的**模块级可变状态**跨用例存活 —— 前一个用例
把它写脏，后一个用例读到脏值。

为什么不直接塞进 `tests/conftest.py`：那份文件正被另一条线改（跨进程
互斥锁），本任务明确不许碰。所以做成独立插件 `tests/order_isolation.py`，
由仓库根的 `conftest.py` 通过 `pytest_plugins` 注册，autouse 夹具对整个
测试套件生效。

复位只覆盖**模块级**状态，不 monkeypatch 路径、不改任何用例的运行条件；
每个 `reset_*` 都定义在对应模块里，生产代码与测试不各写一份。
"""
import pytest

from core import bricks
from core import input as input_mod
from core import interception, records, switches, timesync


#: 模块级可变状态 -> 复位函数。新增载体时只在这里加一行。
RESETTERS = (
    ("timesync._state", timesync.reset),
    ("bricks._cache", bricks.reset_cache),
    ("switches._issues", switches.reset_issues),
    ("records._error_state/_recovery_state/_cache", records.reset_state),
)


def reset_all():
    """把所有模块级可变状态清回初始值。幂等，可重复调用。"""
    for _, reset in RESETTERS:
        reset()
    # core/input.py 与 core/interception.py 正被别的会话改，不能在里面加
    # reset()；两者暴露的模块级容器本身是线程安全的，直接从测试侧清。
    input_mod.LEDGER.clear()
    with interception._DEV_LOCK:          # noqa: SLF001 模块级锁，测试侧复用
        interception._DEV_CACHE.clear()   # noqa: SLF001 同上


def pytest_runtest_setup(item):
    """用例进入任何 fixture 之前先复位一次。

    为什么用 hook 而不只靠夹具：module 级夹具比 function 级 autouse
    夹具先 setup，若它一开始就读 `timesync.info()` / 砖皮表，脏值已经
    进去了。hook 在全部夹具之前跑，能保证「每个用例的起点都是干净的」。
    """
    reset_all()


@pytest.fixture(autouse=True)
def reset_shared_module_state():
    """autouse 复位夹具（本任务指定的入口），与上面的 hook 幂等叠加。

    收尾也复位一次：让「用例结束时的全局状态 == 用例开始时的全局状态」，
    这样任何泄漏探针（比较前后快照）都不会把跨用例的脏值算在下一条头上。
    """
    reset_all()
    yield
    reset_all()
