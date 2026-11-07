"""点击通道不变量：源码里不存在「会借系统鼠标」的点击兜底。

锁死三件事（全静态，不需要游戏 / 驱动 / 桌面）：

1. `SendInput` / `mouse_event` / `SetCursorPos` / `SetSystemCursor` /
   `ShowCursor` / `ClipCursor` / `pyautogui` / `pydirectinput` 只能出现在
   **显式 allowlist 的函数**里，且那些函数必须带 `allowlist:` 标记与理由。
   新增任何一条系统鼠标兜底都会让本测试变红。
2. 点击路径（`click` / `_click_driver` / `_click_message` / `wheel` /
   `_ons_wheel` / `_premarket_fire`）里一条都不能出现 —— 一条都没有。
   `_click_messages` 是 legacy 通道本体，必须被 `== "legacy"` 分支隔离。
3. 降级策略默认 `fail`：驱动不通就显式失败，不静默降级到借光标通道。

跑法：python -m pytest tests/test_click_channel.py -q
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CORE = os.path.join(ROOT, "core")

# 会动系统鼠标 / 发系统合成输入的 API
BANNED = {
    "SendInput",
    "mouse_event",
    "SetCursorPos",
    "SetSystemCursor",
    "ShowCursor",
    "ClipCursor",
    "keybd_event",
}

# 允许出现，但必须带 allowlist 标记（函数名 -> 标记里必须出现的理由关键字）
ALLOWLIST = {
    ("input.py", "_send"): "TEST-FIXTURE-ONLY",
    ("input.py", "_emergency_cursor_restore"): "CURSOR-RESTORE-ONLY",
    ("input.py", "_legacy_place_cursor"): "LEGACY-BORROW",
    ("nocursor.py", "_borrow_click"): "LEGACY-BORROW",
    ("nocursor.py", "_driver_click"): "CURSOR-RESTORE-ONLY",
    ("nocursor.py", "wheel"): "CURSOR-RESTORE-ONLY",
    ("nav_ons.py", "_key_vk"): "KEYBOARD-ONLY",
    # engine.py 本轮不可改（用户边界）。这里是「回用户原位置」的还原调用，
    # 不参与点击定位 —— 只读登记，标记 external。
    ("engine.py", "_restore_cursor"): "EXTERNAL-RESTORE-ONLY",
}

# 不在本轮可改范围内的文件：只校验不落在点击路径上，不要求补 docstring
EXTERNAL_FILES = {"engine.py"}

# 点击路径：这些函数体里不允许出现任何 BANNED
CLICK_PATHS = {
    ("input.py", "click"),
    ("input.py", "_click_driver"),
    ("input.py", "_click_message"),
    ("input.py", "wheel"),
    ("input.py", "_wheel_message"),
    ("nav_ons.py", "_ons_wheel"),
    ("server.py", "_premarket_fire"),
}


def _parse(filename):
    with open(os.path.join(CORE, filename), encoding="utf-8") as f:
        return ast.parse(f.read(), filename=filename)


def _docstring(node):
    return ast.get_docstring(node) or ""


def _toplevel_funcs(tree):
    """{函数名: node}（只看模块级；点击路径都在模块级或方法级）。"""
    out = {}

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            out.setdefault(node.name, node)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    V().visit(tree)
    return out


def _banned_in(node):
    """node 子树里所有调用 BANNED 的位置：(api, lineno)。"""
    hits = []

    class V(ast.NodeVisitor):
        def visit_Call(self, call):
            f = call.func
            name = None
            if isinstance(f, ast.Attribute):
                name = f.attr
            elif isinstance(f, ast.Name):
                name = f.id
            if name in BANNED:
                hits.append((name, call.lineno))
            self.generic_visit(call)

    V().visit(node)
    return hits


def _imported_modules(tree):
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                mods.add(n.module.split(".")[0])
    return mods


def _all_hits():
    """[(file, func, api, lineno)]"""
    out = []
    for fn in sorted(os.listdir(CORE)):
        if not fn.endswith(".py"):
            continue
        tree = _parse(fn)
        for gui in ("pyautogui", "pydirectinput"):
            if gui in _imported_modules(tree):
                out.append((fn, "<import>", gui, 0))
        for name, node in _toplevel_funcs(tree).items():
            for api, lineno in _banned_in(node):
                out.append((fn, name, api, lineno))
    return out


def test_banned_apis_only_in_allowlist():
    """所有系统鼠标 API 只允许出现在 allowlist 函数里，并且都带理由标记。"""
    hits = _all_hits()
    assert hits, "扫描不到任何系统鼠标调用 —— 扫描逻辑坏了，别当成通过"

    offenders = []
    for fn, func, api, lineno in hits:
        if (fn, func) not in ALLOWLIST:
            offenders.append(f"{fn}:{lineno} {func}() -> {api}")
    assert not offenders, (
        "发现未被 allowlist 的系统鼠标调用（这就是兜底路径的来源）：\n  "
        + "\n  ".join(offenders))

    # allowlist 里的每个函数都必须真的带 allowlist 标记，防止「静默加上」
    for (fn, func), reason in ALLOWLIST.items():
        if fn in EXTERNAL_FILES:
            continue
        tree = _parse(fn)
        funcs = _toplevel_funcs(tree)
        assert func in funcs, f"{fn}:{func} 不存在了，清理 ALLOWLIST"
        doc = _docstring(funcs[func])
        assert "allowlist" in doc, f"{fn}:{func} 缺少 allowlist 说明"
        assert reason in doc, f"{fn}:{func} 的 allowlist 标记应包含 {reason}"


def test_click_paths_use_no_system_mouse_api():
    """点击路径函数体内没有任何 BANNED（不打印，纯断言）。"""
    offenders = []
    for fn, func in sorted(CLICK_PATHS):
        tree = _parse(fn)
        funcs = _toplevel_funcs(tree)
        assert func in funcs, f"{fn}:{func} 不存在（点击路径清单过期）"
        for api, lineno in _banned_in(funcs[func]):
            offenders.append(f"{fn}:{lineno} {func}() -> {api}")
    assert not offenders, (
        "点击路径上出现系统鼠标 API：\n  " + "\n  ".join(offenders))


def test_legacy_click_messages_is_guarded():
    """`_click_messages`（借光标通道本体）只能被 `== "legacy"` 分支调用。"""
    tree = _parse("input.py")
    click = _toplevel_funcs(tree)["click"]

    found = []
    unguarded = []

    def rec(node, guarded):
        if isinstance(node, ast.If):
            src = ast.unparse(node.test)
            g = guarded or ("legacy" in src)
            for child in node.body:
                rec(child, g)
            for child in node.orelse:
                rec(child, g)
            rec(node.test, guarded)
            return
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) \
                else getattr(f, "id", None)
            if name in ("_click_messages", "_legacy_place_cursor"):
                found.append(node.lineno)
                if not guarded:
                    unguarded.append(f"input.py:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            rec(child, guarded)

    rec(click, False)
    assert found, "click() 里找不到 legacy 分支调用，测试前提变了"
    assert not unguarded, ("legacy 借光标点击没有被 '== legacy' 隔离：\n  "
                           + "\n  ".join(unguarded))


def test_default_fallback_is_fail():
    """降级策略默认 fail：驱动不通就报错，不静默借系统鼠标。"""
    from core import input as core_input
    from core.config import DEFAULTS, normalize_input_fallback

    assert DEFAULTS["input_fallback"] == "fail"
    assert DEFAULTS["input_legacy_borrow"] is False
    assert normalize_input_fallback("garbage") == "fail"

    old = core_input._config_value
    try:
        # 老配置里残留 legacy + 没开借光标开关 -> 必须落回 fail
        core_input._config_value = lambda key, default=None: (
            "legacy" if key == "input_fallback" else default)
        assert core_input._resolve_fallback(None) == "fail"
        assert core_input._resolve_fallback("legacy") == "fail"

        # 显式打开开关后才允许 legacy
        core_input._config_value = lambda key, default=None: (
            True if key == "input_legacy_borrow" else default)
        assert core_input._resolve_fallback("legacy") == "legacy"

        # 未知值一律落回 fail
        core_input._config_value = lambda key, default=None: default
        assert core_input._resolve_fallback("banana") == "fail"
        assert core_input._resolve_fallback(None) == "fail"
    finally:
        core_input._config_value = old


def test_switches_default_fallback_is_fail():
    """设置界面里「通道降级」的默认值也是 fail（用户看得到）。"""
    from core import switches

    spec = [s for s in switches.SWITCHES if s.get("path") == "input_fallback"]
    assert spec, "switches 里找不到 input_fallback"
    assert spec[0]["default"] == "fail"
    first = spec[0]["options"][0]
    assert first[0] == "fail", "下拉第一项应是默认的 fail"


def test_nocursor_borrow_is_gated():
    """nocursor 的借光标点击也被 input_legacy_borrow 门禁挡住，默认抛错。"""
    from core import input as core_input
    from core import nocursor

    c = nocursor.Clicker(0, "borrow", verify=False)
    old = core_input._legacy_allowed
    try:
        core_input._legacy_allowed = lambda: False
        try:
            c._borrow_click(100, 100, 0)
        except nocursor.InputError as e:
            assert e.reason_code == nocursor.R_FALLBACK_DISABLED, e.reason_code
        else:
            raise AssertionError("borrow 门禁失效：默认配置下仍然借系统光标")
    finally:
        core_input._legacy_allowed = old


def test_release_cursor_lock_is_readonly():
    """release_cursor_lock 不再写裁剪区：只读检测 + 恒返回 (True, False)。"""
    from core.input import Mouse

    tree = _parse("input.py")
    funcs = _toplevel_funcs(tree)
    node = funcs["release_cursor_lock"]
    assert not _banned_in(node), (
        "release_cursor_lock 里还有系统鼠标写操作：" + repr(_banned_in(node)))

    ok, had = Mouse.release_cursor_lock()
    assert (ok, had) == (True, False)


def test_verify_tool_compiles():
    """tools/verify_click_channel.py 必须能编译（交付物不许是半成品）。"""
    path = os.path.join(ROOT, "tools", "verify_click_channel.py")
    assert os.path.exists(path), "缺少 tools/verify_click_channel.py"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    compile(src, path, "exec")
    assert "GetCursorPos" in src and "WH_MOUSE_LL" in src
