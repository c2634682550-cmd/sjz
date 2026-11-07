"""前端静态契约：对着**服务端真正发出去的那一份** web/index.html 断言。

历史问题（`_bug_r21_tests_static.md`）
    上一版这套用例断言的符号 —— `实战`、`sbMode`、`function wearTag`、
    `STATE.pollSeq`、`condErr`、`dry_run` —— 只存在于 `_fe_old_backup/index.html`。
    界面重写后当前 `web/index.html` 一个都没有，9 条断言全部 ValueError，
    唯一"活着"的那条只查 `&#39;` 子串，等于空转。

这一版只认当前结构，并且每一项都对应一个**会真出事**的回归：

1. 页面被换回旧备份 → 主容器 / 主函数消失，立刻失败
2. 脚本里 `$('#x')` 拿到 null → 运行时 TypeError，按钮点了没反应
3. 某个必需接口调用被删 → 对应按钮变成死键（开始/停止/清记录/定位…）
4. 前端调了一个服务端没有的路由 → 点了必然 404
5. `esc` 少了某个实体 或 渲染函数不再转义 → 用户数据当 HTML 执行（XSS）
6. `esc` / `num` 的真实行为（node 里跑真代码，不是看字符串）

不测的东西：无。上一版明确跳过的 poll 过期响应守卫、withBusy 重入锁、
STATE.starting，在 r22 审计里属于「测试过期」——当前 web/index.html
确实有 `S.pollSeq` / `S.starting`，已在第 6 节按现状补回断言。
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WEB_INDEX = os.path.join(ROOT, "web", "index.html")


# ---------------------------------------------------------------- 工具

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def html():
    assert os.path.isfile(WEB_INDEX), f"前端文件不存在：{WEB_INDEX}"
    src = _read(WEB_INDEX)
    assert len(src) > 50_000, f"index.html 只有 {len(src)} 字节，像是被截断了"
    return src


def _function_body(src, name):
    """取一个函数的 `{...}` 主体（跳过字符串/模板字面量里的花括号）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"找不到函数 {name}()"
    i = src.index("{", m.end())
    depth = 0
    quote = None
    j = i
    while j < len(src):
        c = src[j]
        if quote:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError(f"{name}() 的花括号没闭合")


def _selector_ids(src):
    """`$('#x')` / `getElementById('x')` 里引用的元素 id（去掉后代选择器）。"""
    raw = re.findall(r"\$\('#([^']+)'\)", src)
    raw += re.findall(r'\$\("#([^"]+)"\)', src)
    raw += re.findall(r"getElementById\(['\"]([^'\"]+)", src)
    out = set()
    for sel in raw:
        if "${" in sel:
            continue                      # 动态拼出来的 id，静态查不了
        out.add(re.split(r"[ .\[]", sel, maxsplit=1)[0])
    return out


def _server_api_paths():
    """core/server.py 里真的注册了的路由（GET 判断 + POST 分发表）。"""
    src = _read(os.path.join(ROOT, "core", "server.py"))
    return set(re.findall(r"['\"](/api/[a-z0-9/_\-]+)['\"]", src))


# ---------------------------------------------------------------- 1. 文件本身

def test_server_ships_this_file():
    """测的必须是服务端发的那一份，不是 `_fe_old_backup` 里的旧版。"""
    from core import server

    served = os.path.join(server.STATIC, "index.html")
    assert os.path.isfile(served), served
    assert os.path.samefile(served, WEB_INDEX), \
        f"服务端发的是 {served}，测试读的是 {WEB_INDEX} —— 两个文件不是同一份"


def test_main_structure_is_the_rewritten_ui(html):
    """主骨架的锚点都在。整份文件被换回旧备份时，这里第一条就会挂。"""
    for token in ('id="page-run"', 'id="tab-run"', 'id="libGrid"',
                  'id="taskRows"', 'id="excCard"', 'id="toastHost"',
                  "function paintLive", "function renderLive",
                  "function withBusy", "function guardPath"):
        assert token in html, f"主结构里缺 {token!r}"


def test_tabs_and_pages_pair_up(html):
    """每个 tab 都有对应的 page 容器 —— 多一个 tab 就是点不开的死页。"""
    tabs = set(re.findall(r'id="tab-([^"]+)"', html))
    pages = set(re.findall(r'id="page-([^"]+)"', html))
    assert tabs, "一个 tab 都没找到"
    assert tabs == pages, f"tab 和 page 对不上：只在 tab 有 {tabs - pages}，只在 page 有 {pages - tabs}"


# ---------------------------------------------------------------- 2. 选择器

def test_every_selector_resolves_to_an_element(html):
    """`$('#x')` 的 id 必须在 DOM 里存在，否则拿到 null，点了就 TypeError。"""
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    missing = sorted(_selector_ids(html) - ids)
    assert missing == [], f"这些选择器指向不存在的元素：{missing[:8]}"


# ---------------------------------------------------------------- 3. 必需接口

# 每个都对应界面上一处真实功能；删掉调用等于那个按钮变成死键。
REQUIRED_CALLS = {
    "/api/scheduler/start": "「开始」按钮",
    "/api/scheduler/stop": "「停止」按钮",
    "/api/tasks/add": "新建任务",
    "/api/tasks/update": "保存任务修改",
    "/api/tasks/remove": "删除任务",
    "/api/tasks/clear": "清空任务清单",
    "/api/records/clear": "清空成交记录",
    "/api/skins": "典藏外观库列表",
    "/api/skins/locate": "「定位到游戏界面」",
    "/api/skins/target": "标记目标皮肤",
    "/api/follow": "关注/取消关注",
    "/api/hotkey": "热键保存",
    "/api/preflight": "启动前检查",
    "/api/timesync": "校时",
    "/api/switches": "开关表",
    "/api/config": "配置读写",
    "/api/guard/resume": "异常页「继续」",
    "/api/guard/stop": "异常页「安全停止」",
    "/api/quit": "退出程序",
    "/api/jobs": "任务/作业状态轮询",
}


def test_required_api_calls_are_present(html):
    missing = [f"{p}（{why}）" for p, why in REQUIRED_CALLS.items()
               if not re.search(r"['\"]" + re.escape(p) + r"['\"]", html)]
    assert missing == [], f"这些功能的前端调用被删了：{missing}"


def test_start_and_stop_are_real_buttons(html):
    """开始/停止必须是 `data-api` 绑定的按钮，不是只有文字。"""
    for p in ("/api/scheduler/start", "/api/scheduler/stop"):
        assert f'data-api="{p}"' in html, f"{p} 没有绑到任何按钮上"


def test_every_frontend_route_exists_on_the_server(html):
    """前端调的每个 /api/* 都必须在 core/server.py 里有对应处理。"""
    used = {p for p in re.findall(r"['\"](/api/[a-z0-9/_\-]+)['\"]", html)
            if not p.endswith("/")}
    known = _server_api_paths()
    unknown = sorted(used - known)
    assert unknown == [], f"前端在调服务端没有的路由（点了必然 404）：{unknown}"


# ---------------------------------------------------------------- 4. 转义契约

def _js_line(html, prefix):
    """取一行 JS 定义（esc/num 里带 `;` 的实体串，不能按 `;` 截断）。"""
    m = re.search(r"^" + re.escape(prefix) + r".*$", html, re.M)
    assert m, f"找不到 {prefix.strip()} 的定义"
    return m.group(0)


def test_esc_covers_all_five_html_entities(html):
    """`esc` 少一个实体，那个字符就能逃出属性值/文本节点。"""
    body = _js_line(html, "const esc = ")
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert entity in body, f"esc 没有转义 {entity}"
    assert "BIDI" in body, "esc 应该先剥掉双向控制字符（它们会伪装成正常文字）"


def test_user_data_renderers_escape_their_input(html):
    """把用户/OCR 读来的文字拼进 innerHTML 的渲染函数必须走 esc()。

    这些函数的内容来源是任务名、皮肤名、日志、异常描述 —— 都可能带
    `<` `"` 之类的字符（OCR 认错一个字符就可能带进来）。
    """
    for name in ("taskRowsHtml", "renderLibrary", "renderRecords",
                 "renderJournal", "paintException", "histHtml"):
        body = _function_body(html, name)
        assert "esc(" in body, f"{name}() 拼 HTML 却没用 esc() 转义"


# ---------------------------------------------------------------- 5. 真行为（node）

_NODE = shutil.which("node")


def _run_js(js, timeout=15):
    proc = subprocess.run([_NODE, "-e", js], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"node 执行失败：{proc.stderr[-800:]}"
    return proc.stdout.strip()


@pytest.mark.skipif(not _NODE, reason="没装 node，跑不了 JS 真行为")
def test_esc_strips_bidi_and_escapes_tags(html):
    js = (_js_line(html, "const BIDI = ") + "\n"
          + _js_line(html, "const esc = ") + "\n" + """
const cases = [
  [esc('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;'],
  [esc('a"b'), 'a&quot;b'],
  [esc("a'b"), 'a&#39;b'],
  [esc('a&b'), 'a&amp;b'],
  [esc(null), ''],
  [esc(undefined), ''],
  [esc('\\u202eabc'), 'abc'],
];
const bad = cases.filter(([got, want]) => got !== want);
console.log(JSON.stringify(bad));
""")
    bad = _run_js(js)
    assert bad == "[]", f"esc 的行为不对：{bad}"


# ---------------------------------------------------------------- 6. 交互契约回归
# 这 8 条对应 `_bug_r22_tests_dynamic.md` 里报过红的 8 个前端静态契约。
# 旧用例断言的符号（wearTag / condErr / sbMode / STATE.* / dry_run）在
# 重写后确实不存在，属于「测试过期」；这里按当前实现重写为行为断言，
# 不是删掉——每条仍然守着同一个会真出事的回归。

def test_condition_errors_are_rendered_inline(html):
    """参数不合法的原因必须显示在对应输入框下面，并禁用保存。"""
    body = _function_body(html, "validateTaskForm")
    for mid, why in (("msgWearMax", "磨损上限"), ("msgPrice", "价格区间"),
                     ("msgQuota", "本轮限量")):
        assert mid in body, f"{why} 的校验信息没在 validateTaskForm 里"
        assert f'id="{mid}"' in html, f"页面没有 {why} 的行内错误位"
    assert "textContent" in body, "行内错误必须用 textContent 写，不能拼 HTML"
    assert "btn.disabled" in body, "校验不通过必须禁用保存按钮"


def test_keyboard_accessibility_is_wired(html):
    """只能鼠标点等于键盘用户用不了：行要可聚焦，回车要能选中。"""
    assert 'role="tab"' in html and "aria-selected" in html, "页签缺少 ARIA 角色/状态"
    body = _function_body(html, "taskRowsHtml")
    assert 'tabindex="0"' in body, "任务行不能键盘聚焦"
    assert "aria-selected" in body, "任务行缺少选中状态"
    assert re.search(r"\$\('#taskRows'\).addEventListener\('keydown'.*?Enter", html, re.S), \
        "任务行没有键盘（Enter）处理"


def test_poll_discards_stale_responses(html):
    """两次轮询交叠时，旧响应必须丢弃，不能覆盖新状态。"""
    body = _function_body(html, "poll")
    assert "++S.pollSeq" in body, "轮询没有递增序号"
    assert body.count("seq !== S.pollSeq") >= 2, \
        "过期响应守卫少于两处（请求前/落地前都要查）"
    assert "pollBusy" in body, "没有重入保护，快速轮询会叠起来"


def test_start_button_shows_the_only_mode(html):
    """现在只有实战一种模式：旧版「演练/dry_run」开关不能再冒出来。"""
    assert "dry_run" not in html and "演练" not in html, "旧的双模式开关回来了"
    assert html.count('data-api="/api/scheduler/start"') >= 1, "开始按钮没绑启动接口"


def test_start_is_mutually_exclusive(html):
    """连点「开始」只能产生一次启动请求。"""
    body = _function_body(html, "handleStart")
    assert "if (S.starting) return;" in body, "没有重入短路"
    assert "S.starting = true;" in body and "S.starting = false;" in body, \
        "重入标志没有正确置位/复位（复位丢了会永远点不动）"


def test_task_name_is_escaped_in_the_queue_list(html):
    """任务名来自用户输入，拼进 innerHTML 前必须转义。"""
    body = _function_body(html, "taskRowsHtml")
    assert "esc(t.name)" in body, "任务名没转义"
    assert "esc(t.id)" in body, "行 data-id 没转义"
    assert "esc(wearTxt)" in body, "磨损/品阶标签没转义"


def test_tier_options_come_from_the_backend_contract(html):
    """品阶选项必须和后端 bricks.json 的 name 完全一致，不能各写一套。"""
    import json
    with open(os.path.join(ROOT, "core", "bricks.json"), encoding="utf-8") as f:
        names = {b["name"] for b in json.load(f)["craft"]}
    dom = set(re.findall(r'data-craft="([^"]+)"', html)) - {""}   # 去掉「不限」
    assert dom == names, f"前端品阶 {dom} 与后端契约 {names} 不一致"


def test_wear_tag_escapes_the_ocr_value(html):
    """磨损值来自 OCR，认错字符也可能带进 `<`，标签必须走 esc。"""
    body = _function_body(html, "taskRowsHtml")
    assert "t.wear" in body, "磨损值没进标签"
    assert "esc(wearTxt)" in body, "磨损标签没整体转义"


@pytest.mark.skipif(not _NODE, reason="没装 node，跑不了 JS 真行为")
def test_num_shows_dash_instead_of_nan(html):
    """`num` 把空值/非数字显示成「—」，而不是 `NaN` / `Infinity`。"""
    js = _js_line(html, "const num = ") + "\n" + """
const cases = [
  [num(null), '—'],
  [num(undefined), '—'],
  [num(''), '—'],
  [num('abc'), '—'],
  [num(Infinity), '—'],
  [num(12.345, 1), '12.3'],
  [num('7'), '7'],
];
const bad = cases.filter(([got, want]) => got !== want);
console.log(JSON.stringify(bad));
"""
    bad = _run_js(js)
    assert bad == "[]", f"num 的行为不对：{bad}"
