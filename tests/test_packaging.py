"""打包配置要收齐运行时要读的数据文件。

`core/bricks.py` 用 `__file__` 找同目录的 `bricks.json`；冻结成单文件 exe 后
`__file__` 指向 `sys._MEIPASS`，那里只有 `sjz.spec` 的 `datas` 明确收进去的东西。
漏收的表现是**静默空库**：`_load()` 读不到文件就返回空 seasons，`identify()`
永远回「对不上任何赛季砖皮」，而界面上仍然写着「11 个赛季」—— 没人会想到是
打包漏了文件。

这里不读源码文本，而是把 `core/` 下真实存在的非 .py 文件列出来，逐个检查
`datas` 有没有覆盖。新增数据文件忘了加进 spec，这条就会失败。
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "sjz.spec")


def _load_spec():
    """把 sjz.spec 当 Python 代码执行，取它的 datas。

    spec 不是可导入的模块（扩展名不是 .py，importlib 认不出来），但它到
    `datas = [...]` 之间只用了 os / sys / collect_submodules，直接 exec 进一个
    空命名空间就够了。执行到 `Analysis(...)` 之前截断 —— 那之后是 PyInstaller
    的构建调用，不该在测试里跑。

    spec 顶部 `from PyInstaller.utils.hooks import collect_submodules` 在没装
    PyInstaller 的环境里会失败。这里塞一个假的 PyInstaller 进去：我们只要
    `datas`，`collect_submodules` 返回空表不影响它。
    """
    import types

    fake_hooks = types.ModuleType("PyInstaller.utils.hooks")
    fake_hooks.collect_submodules = lambda name: []
    fake_utils = types.ModuleType("PyInstaller.utils")
    fake_utils.hooks = fake_hooks
    fake_pi = types.ModuleType("PyInstaller")
    fake_pi.utils = fake_utils
    stubs = {
        "PyInstaller": fake_pi,
        "PyInstaller.utils": fake_utils,
        "PyInstaller.utils.hooks": fake_hooks,
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        src = open(SPEC, encoding="utf-8").read()
        cut = src.index("a = Analysis(")
        ns = {"__file__": SPEC, "__name__": "_sjz_spec"}
        exec(compile(src[:cut], SPEC, "exec"), ns)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert "datas" in ns, "sjz.spec 里没定义 datas"
    return ns["datas"]


@pytest.fixture(scope="module")
def datas():
    assert os.path.isfile(SPEC), "找不到 sjz.spec"
    return _load_spec()


def _bundled(datas):
    """datas 里每一项的 (源路径, 目标目录)，统一成小写正斜杠比较。"""
    out = []
    for src, dest in datas:
        out.append((os.path.normcase(os.path.abspath(src)).replace("\\", "/"),
                    dest.replace("\\", "/")))
    return out


def _data_files():
    """core/ 下所有运行时要读的非 .py 文件。"""
    core = os.path.join(ROOT, "core")
    out = []
    for name in sorted(os.listdir(core)):
        p = os.path.join(core, name)
        if not os.path.isfile(p):
            continue
        if name.endswith(".py") or name.startswith("."):
            continue
        out.append(p)
    return out


def test_core_has_a_bricks_database_to_bundle():
    """前提：知识库文件确实存在。不存在的话下面的检查会空转通过。"""
    assert os.path.isfile(os.path.join(ROOT, "core", "bricks.json"))


@pytest.mark.parametrize("path", _data_files(), ids=lambda p: os.path.basename(p))
def test_every_core_data_file_is_bundled(datas, path):
    """core/ 下的数据文件必须出现在 spec 的 datas 里。

    漏收 = 打包后的 exe 里知识库是空的，但界面上看不出来。
    """
    want = os.path.normcase(os.path.abspath(path)).replace("\\", "/")
    got = [src for src, _ in _bundled(datas)]
    assert want in got, (
        f"{os.path.basename(path)} 没被收进 sjz.spec 的 datas —— "
        f"打出的 exe 里读不到它"
    )


def test_bricks_database_lands_next_to_the_module(datas):
    """目标目录必须是 `core`，因为 `bricks.py` 只在**自己所在目录**里找。

    收进 `data/` 或根目录都不行 —— `_load()` 用的是
    `os.path.dirname(__file__)`，不是 `sys._MEIPASS`。
    """
    want = os.path.normcase(os.path.abspath(
        os.path.join(ROOT, "core", "bricks.json"))).replace("\\", "/")
    dests = [dest for src, dest in _bundled(datas) if src == want]
    assert dests, "bricks.json 不在 datas 里"
    assert dests[0] == "core", f"收进了 {dests[0]}/，应该在 core/"


def test_web_assets_are_bundled(datas):
    """前端页面也得收 —— 这条是原本就有的，留着防止改 spec 时误删。"""
    want = os.path.normcase(os.path.abspath(
        os.path.join(ROOT, "web"))).replace("\\", "/")
    got = [src for src, _ in _bundled(datas)]
    assert want in got
