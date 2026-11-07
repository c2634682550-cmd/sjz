"""仓库根 conftest：把 `tests/order_isolation.py` 注册成全局插件。

为什么需要这一层 shim：`tests/` 下定义的夹具只对同目录树生效，而跨模块
共享状态的复位必须覆盖整个套件；同时 `tests/conftest.py` 正被另一条线改
（跨进程互斥锁），本任务不许碰。于是用一个最小根级 conftest：

1. 把仓库根与 `tests/` 放进 `sys.path`，让 `core` 与插件模块都能导入；
2. 用 `pytest_plugins` 注册隔离插件（autouse 夹具 + runtest_setup hook）。

除注册外不写任何逻辑，避免和根级其它配置抢职责。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.join(_HERE, "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytest_plugins = ["order_isolation"]
