"""前端开关「提交非法值 → 界面回滚到磁盘值」回归。

背景（`_nonreal_fe.md` 第七节观察 1）
    成对输入倒挂（`scan.repeat.min` > `scan.repeat.max`）提交时，服务端按契约
    返回 400 拒收，界面弹了错误 toast，但输入框仍停在非法值：实测
    `ui=5 / disk=1 / reload=1`。刷新前界面和磁盘不一致，用户会误以为生效。

契约（本文件守着的东西）
    1. 后端 400 契约不动，前端不做静默纠正 —— 提交的还是用户输入的那个值。
    2. 被拒后控件必须回填成服务端实际落盘值（上一次成功保存的值）。
    3. toast 照旧提示错误原因。
    4. 所有有服务端范围校验的数字型开关都走同一条回滚路径，不是只修成对输入。

两类用例：
    · 静态：读 `web/index.html`，确认提交路径没有夹带前端纠正、catch 分支
      真的调用回滚、回滚以 `/api/switches` 快照为准。
    · 真界面：无头 Chromium + 进程内 `core.server`，config.json 用 tmp_path
      副本（仓库文件只读）。逐项提交非法值，断言控件值 == 磁盘值。
"""
import contextlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
WEB_INDEX = os.path.join(ROOT, "web", "index.html")

CHROME_CANDIDATES = [
    r"C:\Users\26346\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe",
    r"C:\Users\26346\AppData\Local\ms-playwright\chromium-1169\chrome-win\chrome.exe",
]

# 设置页分组 → 容器 id / 分栏
GROUP_BOX = {
    "运行模式": "swRunMode", "收藏设置": "swFollow", "环境与安全": "swEnv",
    "价格与筛选": "swPrice", "抢购设置": "swFire", "自动与停止": "swAuto",
    "截前": "swPre",
}
GROUP_PANE = {
    "运行模式": "general", "收藏设置": "general", "环境与安全": "general",
    "价格与筛选": "fire", "抢购设置": "fire", "自动与停止": "fire", "截前": "fire",
}


# ---------------------------------------------------------------- 静态契约

def _src():
    with open(WEB_INDEX, encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    """取函数的 `{...}` 主体（跳过字符串/模板字面量里的花括号）。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"找不到函数 {name}()"
    # 跳过参数表（可能带默认值 `opts = {}`），再定位函数体左花括号
    k, pdepth = m.end(), 0
    while k < len(src):
        c = src[k]
        if c == "(":
            pdepth += 1
        elif c == ")":
            if pdepth == 0:
                k += 1
                break
            pdepth -= 1
        k += 1
    i = src.index("{", k)
    depth, quote, j = 0, None, i
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


def test_rejected_value_is_not_corrected_before_submit():
    """提交的还是用户输入的那个值：前端不夹带 clamp / 取整 / 改写成合法值。"""
    body = _function_body(_src(), "applySwitch")
    assert "{ [key]: value }" in body, "提交体不再原样带用户输入的值"
    for bad in ("clamp(", "Math.min", "Math.max"):
        assert bad not in body, f"applySwitch 里出现前端纠正 {bad}，违反 400 契约"


def test_catch_branch_rolls_back_and_keeps_error_toast():
    """400 之后：错误 toast 保留并说明本次未生效，同时走回滚。"""
    body = _function_body(_src(), "applySwitch")
    assert "toast(" in body and "'err'" in body, "错误 toast 被删了"
    assert "本次未生效" in body and "已恢复上次保存的值" in body, \
        "toast 没有说明「被拒 + 已恢复」，用户会以为输入丢了"
    assert re.search(r"rollbackSwitch\(\s*key\s*,\s*seq\s*\)", body), \
        "catch 分支没有调用 rollbackSwitch，非法值会留在界面上"


def test_rollback_reads_the_server_snapshot_without_wholesale_overwrite():
    """回滚值必须来自服务端快照，且只能校正被拒那一项。"""
    body = _function_body(_src(), "rollbackSwitch")
    assert "api('/api/switches')" in body, "回滚没有重新拉服务端快照"
    assert "fresh.value" in body, "回滚没有用服务端返回的 value"
    assert "restoreSwitchControls" in body, "回滚没有落到控件上"
    assert "mergeSwitchValue" in body, "回滚没有校正本地快照"
    assert "S.switches =" not in body, "回滚整体覆盖了 S.switches（会污染快照）"


def test_rollback_is_gated_by_seq_and_version():
    """旧回包不能写回填、不能更新快照：seq 与 swVer 双重门禁。"""
    body = _function_body(_src(), "rollbackSwitch")
    assert "S.swLastOkSeq" in body, "回填没有判断是否晚于最新一次成功保存"
    assert "S.swVer" in body, "回填没有判断服务端状态版本"
    assert "seq !== S.swSeq" in body, "回填没有判断是否还有更新的提交"
    ok = _function_body(_src(), "applySwitch")
    assert "S.swLastOkSeq" in ok, "成功回包没有登记最新序号（竞态时会被旧回包覆盖）"


def test_api_has_timeout_and_save_reports_it():
    """api() 有超时上限；超时按「本次未生效」回滚并显示「保存超时」。"""
    api = _function_body(_src(), "api")
    assert "AbortController" in api and "ctrl.abort()" in api, "api() 没有超时中断"
    assert "timeout: true" in api, "超时没有独立错误标记"
    ok = _function_body(_src(), "applySwitch")
    assert "timeout: 2000" in ok, "开关保存没有超时上限"
    assert "setSaveNotice('busy'" in ok, "「保存中」没有登记为保持状态，会被周期刷新盖掉"
    assert "保存超时" in ok and "e.timeout" in ok, "超时没有被当成未生效处理"
    band = _function_body(_src(), "paintStatusBand")
    assert "saveNotice" in band, "周期刷新会把保存状态盖掉"


def test_restore_helper_covers_number_and_bool_controls():
    """数字框按插入点回填；布尔开关同步 on/aria-checked（镜像控件也会被扫到）。"""
    body = _function_body(_src(), "restoreSwitchControls")
    assert "assignKeepCaret" in body, "数字/下拉控件没有走保插入点的回填"
    assert "aria-checked" in body and "classList.toggle('on'" in body, \
        "布尔开关的显示状态没有跟着回滚"
    assert "querySelectorAll('[data-sw]')" in body, \
        "回滚只改了单个元素，运行页的镜像控件会残留非法值"


def test_caret_restore_never_forces_cursor_to_end():
    """读得到选区就还原；读不到就不碰光标，不强行 setSelectionRange 到末尾。"""
    body = _function_body(_src(), "assignKeepCaret")
    assert "setSelectionRange" in body, "没有还原插入点"
    assert "sel[0] <= next.length" in body, "没有做越界保护就直接设选区"
    assert "memo <= next.length" in body, "number 输入的插入点没有做越界保护"
    assert "el.value.length, el.value.length" not in body, \
        "回填把光标强行拉到了末尾"


# ---------------------------------------------------------------- 真界面

def _chrome():
    return next((p for p in CHROME_CANDIDATES if os.path.isfile(p)), None)


def _wait_booted(page, timeout=40000):
    """等「整个启动流程走完」的确定性信号，而不是等连接状态。

    `#sbConnTxt === '已连接'` 是第一次 api() 成功就置上的（api() → noteOk()
    → setConn(true)），此时 boot() 还在跑 6 个 loader + renderAll()，最后
    才 go('run')。等它会把夹具放进「启动未完成」的窗口里：全量负载下 boot
    收尾晚到，把已经切到「设置」的页面抢回「运行」，`#page-set` 重新
    hidden，分组按钮点不到 —— 旧账就是 `Page.click: Timeout 30000ms
    exceeded`（元素全程 is not visible）。boot() 收尾打的 data-booted 才是
    真正的完成态（产品侧同时修了点页后被抢回运行页的竞态）。
    """
    page.wait_for_function(
        "() => document.body.dataset.booted === '1'", timeout=timeout)


@pytest.fixture(scope="module")
def live(tmp_path_factory, request):
    """进程内服务端 + 无头浏览器；config.json 用临时副本。"""
    chrome = _chrome()
    if not chrome:
        pytest.skip("没有可用的 headless Chromium（ms-playwright）")

    from core import config

    tmp = tmp_path_factory.mktemp("fe_rollback")
    cfg_path = str(tmp / "config.json")
    shutil.copy(os.path.join(ROOT, "config.json"), cfg_path)
    # 以前这里是 `config.CONFIG_PATH = cfg_path` 直接赋值、模块 fixture
    # 结束时没人还原：后面所有用例（包括随机顺序下先跑完本文件再跑的
    # 任何模块）都对着这个临时副本读写，读到的是本文件留下的开关值。
    # 走 MonkeyPatch + finalizer，模块结束时精确还原。
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CONFIG_PATH", cfg_path)
    request.addfinalizer(mp.undo)

    from core import server as srv

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    url = f"http://127.0.0.1:{port}"

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, executable_path=chrome)
    ctx = browser.new_context(viewport={"width": 1600, "height": 900})
    page = ctx.new_page()
    page.add_init_script("try{localStorage.setItem('dafors.guided','1')}catch(e){}")
    page.goto(url + "/", wait_until="domcontentloaded")
    _wait_booted(page)
    page.click("#tab-set")
    page.wait_for_function(
        "() => { const p = document.querySelector('#page-set'); return !!p && !p.hidden; }",
        timeout=10000)

    with urllib.request.urlopen(url + "/api/switches", timeout=10) as resp:
        snap = json.loads(resp.read().decode("utf-8"))

    yield {"url": url, "cfg": cfg_path, "page": page, "snapshot": snap,
           "browser": browser, "chrome": chrome}

    browser.close()
    pw.stop()
    httpd.shutdown()


def _items(snapshot):
    out = []
    for g in snapshot["groups"]:
        for it in g["items"]:
            it = dict(it)
            it["_group"] = g["name"]
            out.append(it)
    return out


def _dig(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _ensure_pane(page, group):
    """切到设置页的第 group 组，并等该面板真的显示出来（不睡固定时间）。"""
    pane = GROUP_PANE[group]
    page.click("#tab-set")
    page.wait_for_function(
        "() => { const p = document.querySelector('#page-set'); return !!p && !p.hidden; }",
        timeout=10000)
    page.click('.setnav [data-setpane="%s"]' % pane)
    page.wait_for_function(
        "(id) => { const p = document.querySelector('#pane-' + id);"
        " return !!p && !p.hidden; }", arg=pane, timeout=10000)


def _locator(page, group, key):
    return page.locator('#%s [data-sw="%s"]' % (GROUP_BOX[group], key))


def _editable(page, group, key):
    """取可编辑控件；按品阶停止的三个件数框在总开关关掉时是置灰的，先打开。"""
    loc = _locator(page, group, key)
    if loc.is_disabled() and key.startswith("stop.by_grade."):
        sw = _locator(page, group, "stop.by_grade.enabled")
        if sw.get_attribute("aria-checked") != "true":
            pred = lambda r: "/api/switches" in r.url and r.request.method == "POST"
            with page.expect_response(pred, timeout=15000) as ri:
                sw.click()
            assert ri.value.status == 200, "打开按品阶停止总开关失败"
            page.wait_for_function(
                "() => { const e=document.querySelector('[data-sw=\"stop.by_grade.legend\"]');"
                " return e && !e.disabled; }", timeout=8000)
    loc = _locator(page, group, key)
    loc.wait_for(state="visible", timeout=10000)
    return loc


def _submit(page, loc, raw):
    """填值 + change，等 POST /api/switches 回包。"""
    pred = lambda r: "/api/switches" in r.url and r.request.method == "POST"
    with page.expect_response(pred, timeout=15000) as ri:
        loc.fill(str(raw))
        loc.dispatch_event("change")
    return ri.value


def _wait_value(page, group, key, want, timeout=8000):
    """等控件值变成 `want`（数字按数值比较，字符串按原样比较）。

    磁盘上的 float 可能是 `1.0`，number 输入渲染成 `1`；按字符串比会误判
    成「没回滚」。数值等价比较不放松语义：1.0 与 1 本来就是同一个值。
    """
    sel = '#%s [data-sw="%s"]' % (GROUP_BOX[group], key)
    page.wait_for_function(
        """([sel, want]) => {
             const e = document.querySelector(sel);
             if (!e) return false;
             const a = String(e.value).trim(), b = String(want).trim();
             if (a === b) return true;
             if (a === '' || b === '') return false;
             const fa = Number(a), fb = Number(b);
             return Number.isFinite(fa) && Number.isFinite(fb) && fa === fb;
           }""",
        arg=[sel, str(want)], timeout=timeout)


def _reload_page(live):
    """整页重载并等 boot 完成，返回模块级页面。

    模块级 `live["page"]` 从 fixture setup 起只在少数用例里手动 reload；
    同模块别的用例可能通过独立 context 改了磁盘（例如把 click_hz 存成
    60），页面却停在旧值。数字型用例开跑前必须重载，让「页面值 ==
    磁盘值」成立，否则会把「回滚到磁盘 60」误判成回滚失败。
    """
    page = live["page"]
    page.reload(wait_until="domcontentloaded")
    _wait_booted(page)
    page.wait_for_function(
        "() => document.querySelectorAll('[data-sw]').length > 0", timeout=20000)
    return page


def _sync_locator(live, page, it):
    """切到目标分组、取可编辑控件，并断言页面值与磁盘值一致（前置）。"""
    _ensure_pane(page, it["_group"])
    loc = _editable(page, it["_group"], it["key"])
    base = loc.input_value()
    disk = _disk(live, it["path"])
    assert abs(float(base) - float(disk)) < 1e-9, (
        f"{it['key']}: 前置不同步 —— 页面 {base}，磁盘 {disk}")
    return loc, disk


def test_pair_inversion_rolls_back_to_disk(live):
    """成对输入倒挂：服务端 400，界面回滚到磁盘值（两个方向都测）。"""
    page, cfg = live["page"], live["cfg"]
    items = {it["key"]: it for it in _items(live["snapshot"])}
    lo_it, hi_it = items["scan.repeat.min"], items["scan.repeat.max"]

    # 基线：盘上 min=max=1，界面与之一致
    body = {"values": {"scan.repeat.min": 1, "scan.repeat.max": 1}}
    req = urllib.request.Request(
        live["url"] + "/api/switches", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert json.loads(resp.read().decode())["ok"] is True
    page.reload(wait_until="domcontentloaded")
    _wait_booted(page)
    page.wait_for_function(
        "() => document.querySelectorAll('[data-sw=\"scan.repeat.min\"]').length > 0",
        timeout=20000)

    _ensure_pane(page, lo_it["_group"])
    for key, it, illegal in (("scan.repeat.min", lo_it, 5),
                             ("scan.repeat.max", hi_it, 0)):
        loc = _locator(page, it["_group"], key)
        loc.wait_for(state="visible", timeout=10000)
        base = loc.input_value()
        r = _submit(page, loc, illegal)
        assert r.status == 400, f"{key}: 倒挂提交应被 400 拒收，实际 {r.status}"
        payload = r.json()
        assert payload.get("ok") is False
        assert "最少" in payload.get("msg", ""), payload
        _wait_value(page, it["_group"], key, base)
        with open(cfg, encoding="utf-8") as f:
            disk = _dig(json.load(f), it["path"])
        assert abs(float(disk) - float(base)) < 1e-9, \
            f"{key}: 被拒后磁盘变成了 {disk}（基线 {base}）"


def test_single_out_of_bounds_rolls_back_to_disk(live):
    """单值越界：`fire.hz` 高于上限 400 拒收，界面回滚到磁盘值。

    开跑前重载页面并对齐「页面 == 磁盘」；期望值一律取磁盘值，不拿页面
    上的旧值当基准（旧值可能已经被同模块别的用例改掉）。
    """
    it = next(x for x in _items(live["snapshot"]) if x["key"] == "fire.hz")
    page = _reload_page(live)
    loc, want = _sync_locator(live, page, it)
    r = _submit(page, loc, 201)
    assert r.status == 400, f"越界提交应被 400 拒收，实际 {r.status}"
    assert "上限" in r.json().get("msg", ""), r.json()
    _wait_value(page, it["_group"], "fire.hz", want)
    disk = _disk(live, it["path"])
    assert abs(float(disk) - float(want)) < 1e-9, \
        f"fire.hz: 被拒后磁盘变成 {disk}（基线 {want}）"


def test_every_bounded_numeric_switch_rolls_back(live):
    """全量数字型开关逐个验：凡是服务端有范围校验的，拒收后都回到磁盘值。

    覆盖 `stop.by_grade.*`、`click.burst.hold_ms/interval_ms`、
    `buy.skip_late.threshold_s`、`mouse.idle.*`、`mouse.human.*` 等全部 int/float。

    每条都先断言「页面值 == 磁盘值」再提交；被拒后界面和磁盘都必须停在
    这个磁盘值上。期望值不取页面旧值，避免把别的用例写下的磁盘值误判成
    回滚失败。
    """
    from core import switches

    page = _reload_page(live)
    rows = []
    for it in _items(live["snapshot"]):
        if it["kind"] not in ("int", "float") or it["key"] not in switches.BOUNDS:
            continue
        lo, hi = switches.BOUNDS[it["key"]]
        illegal = (int(hi) + 1) if it["kind"] == "int" else (float(hi) + 1.0)
        loc, want = _sync_locator(live, page, it)
        r = _submit(page, loc, illegal)
        assert r.status == 400, f"{it['key']}={illegal} 应被 400 拒收，实际 {r.status}"
        _wait_value(page, it["_group"], it["key"], want)
        disk = _disk(live, it["path"])
        assert abs(float(disk) - float(want)) < 1e-9, \
            f"{it['key']}: 界面回到 {want}，但磁盘是 {disk}"
        rows.append((it["key"], illegal, want))

    assert len(rows) >= 25, f"只测到 {len(rows)} 个数字开关，覆盖面不对"


# ------------------------------------------------- 竞态 / 超时 / 插入点（第二轮）

def _post(live, values):
    body = {"values": values}
    req = urllib.request.Request(
        live["url"] + "/api/switches", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _disk(live, path):
    with open(live["cfg"], encoding="utf-8") as f:
        return _dig(json.load(f), path)


@contextlib.contextmanager
def _new_page(live, baseline):
    """独立 context + 独立页面；先把磁盘写成已知基线再加载页面。

    退出时把 `baseline` 涉及的键还原成进入前的磁盘值：用例中途保存成功
    （例如 fire.hz 存成 60）会留在共享配置文件里，而模块级 `live["page"]`
    不会自己刷新，下一条数字用例读到的就是「页面旧值 vs 磁盘新值」。
    以前这里只 `ctx.close()`，60 就这样漏给了后一条用例。
    """
    items = {it["key"]: it for it in _items(live["snapshot"])}
    restore = {k: _disk(live, items[k]["path"])
               for k in baseline if k in items}
    st, _ = _post(live, baseline)
    assert st == 200, f"基线写入失败：HTTP {st}"
    ctx = live["browser"].new_context(viewport={"width": 1600, "height": 900})
    page = ctx.new_page()
    page.add_init_script("try{localStorage.setItem('dafors.guided','1')}catch(e){}")
    page.goto(live["url"] + "/", wait_until="domcontentloaded")
    _wait_booted(page)
    page.click("#tab-set")
    page.wait_for_function(
        "() => { const p = document.querySelector('#page-set'); return !!p && !p.hidden; }",
        timeout=10000)
    try:
        yield page
    finally:
        ctx.close()
        st, _ = _post(live, restore)
        assert st == 200, f"还原磁盘失败：HTTP {st}"


def _fire(page):
    _ensure_pane(page, "抢购设置")
    loc = _locator(page, "抢购设置", "fire.hz")
    loc.wait_for(state="visible", timeout=10000)
    return loc


_POST_PRED = lambda r: "/api/switches" in r.url and r.request.method == "POST"  # noqa: E731


def _toasts(page):
    return page.locator("#toastHost .toast").all_text_contents()


_STUB_DELAY = """() => {
  const orig = window.fetch;
  window.fetch = (url, init) => {
    const isPost = init && init.method === 'POST' && String(url).includes('/api/switches');
    let wait = 0;
    if (isPost) {
      try {
        const v = (JSON.parse(init.body || '{}').values || {})['fire.hz'];
        wait = (v === 201) ? 1600 : 40;
      } catch (e) { wait = 40; }
    }
    return new Promise(res => setTimeout(res, wait)).then(() => orig(url, init));
  };
}"""


def test_delayed_400_after_later_success(live):
    """旧 400 晚于下一次成功保存才回来：整包作废，界面必须停在磁盘值 60。"""
    with _new_page(live, {"fire.hz": 50}) as page:
        loc = _fire(page)
        page.evaluate(_STUB_DELAY)
        loc.fill("201")
        loc.dispatch_event("change")
        page.wait_for_timeout(120)
        with page.expect_response(_POST_PRED, timeout=15000) as ri:
            loc.fill("60")
            loc.dispatch_event("change")
        assert ri.value.status == 200, f"合法值 60 应保存成功，实际 {ri.value.status}"
        page.wait_for_timeout(1800)          # 等旧 400 的回滚回包到达并被丢弃
        assert loc.input_value() == "60", "旧回包把界面改回了 50（竞态没挡住）"
        assert abs(float(_disk(live, "click_hz")) - 60) < 1e-9
        assert not any("已恢复" in t for t in _toasts(page)), _toasts(page)


_STUB_HANG = """() => {
  const orig = window.fetch;
  window.fetch = (url, init) => {
    const isPost = init && init.method === 'POST' && String(url).includes('/api/switches');
    if (isPost) {
      let v = null;
      try { v = (JSON.parse(init.body || '{}').values || {})['fire.hz']; } catch (e) {}
      if (v === 201) {
        return new Promise((_, rej) => {
          const sig = init.signal;
          if (!sig) return;                       // 没有超时信号就永远挂着
          const fire = () => rej(new DOMException('aborted', 'AbortError'));
          if (sig.aborted) return fire();
          sig.addEventListener('abort', fire);
        });
      }
    }
    return orig(url, init);
  };
}"""


def test_post_timeout_rolls_back(live):
    """请求永不返回：超时按「本次未生效」回滚，状态条显示「保存超时」。"""
    with _new_page(live, {"fire.hz": 50}) as page:
        loc = _fire(page)
        page.evaluate(_STUB_HANG)
        loc.fill("201")
        loc.dispatch_event("change")
        page.wait_for_timeout(2500)          # 复核原话：挂起 2.5s 后必须已经回到落盘值
        assert loc.input_value() == "50", "超时后界面仍停在非法值"
        assert (page.locator("#gText").text_content() or "") == "保存超时", \
            "状态条没有显示「保存超时」"
        page.wait_for_timeout(1200)          # 跨过两次周期刷新，不能被盖掉
        assert loc.input_value() == "50"
        assert abs(float(_disk(live, "click_hz")) - 50) < 1e-9
        txt = " / ".join(_toasts(page))
        assert "保存超时" in txt and "已恢复上次保存的值" in txt, txt
        assert (page.locator("#gText").text_content() or "") == "保存超时", \
            page.locator("#gText").text_content()
        assert (page.locator("#gState").text_content() or "") == "未生效"
        assert page.evaluate(
            "() => window.__apiLog.filter(x => x.timeout).length") >= 1
        page.wait_for_timeout(1200)          # 跨过一次周期刷新，不能被盖成「待命」
        assert (page.locator("#gText").text_content() or "") == "保存超时", \
            "周期刷新把「保存超时」盖掉了"


_STUB_STALE_GET = """() => {
  const orig = window.fetch;
  window.__uiLog = [];
  window.__delayGet = true;
  window.fetch = async (url, init) => {
    const u = String(url);
    const isPost = init && init.method === 'POST';
    if (u.includes('/api/switches') && !isPost) {
      const r = await orig(url, init);                       // 立刻取磁盘（可能是旧值）
      if (window.__delayGet) {
        window.__delayGet = false;
        await new Promise(x => setTimeout(x, 1600));          // 回包故意晚到
      }
      return r;
    }
    return orig(url, init);
  };
  setInterval(() => {
    const e = document.querySelector('#swFire [data-sw="fire.hz"]');
    if (e) window.__uiLog.push([Math.round(performance.now()), String(e.value)]);
  }, 10);
}"""


def test_stale_rollback_get_does_not_pollute_snapshot(live):
    """复核复现：201 被拒 → 120ms 后保存 60 → 1.7s 旧 GET(50) 到达必须丢弃。"""
    with _new_page(live, {"fire.hz": 50}) as page:
        loc = _fire(page)
        page.evaluate(_STUB_STALE_GET)
        with page.expect_response(_POST_PRED, timeout=15000) as ri:
            loc.fill("201")
            loc.dispatch_event("change")
        assert ri.value.status == 400
        page.wait_for_timeout(120)
        with page.expect_response(_POST_PRED, timeout=15000) as r2:
            loc.fill("60")
            loc.dispatch_event("change")
        assert r2.value.status == 200
        page.wait_for_timeout(1800)          # 旧 GET 在 1.6s 才回来
        assert loc.input_value() == "60", "竞态下界面回落到 50，和磁盘 60 不一致"
        assert abs(float(_disk(live, "click_hz")) - 60) < 1e-9

        log = page.evaluate("() => window.__uiLog")
        first60 = next((i for i, (_, v) in enumerate(log) if v == "60"), None)
        assert first60 is not None, "采样里没看到保存成功后的 60"
        tail = [v for _, v in log[first60:] if v != "60"]
        assert not any(v == "50" for v in tail), \
            f"保存 60 之后出现回落 50 的帧：{tail[:12]}"

        # 快照没被污染：再拒一次，仍应回滚到磁盘 60 而不是旧值 50
        with page.expect_response(_POST_PRED, timeout=15000) as r3:
            loc.fill("201")
            loc.dispatch_event("change")
        assert r3.value.status == 400
        page.wait_for_timeout(600)
        assert loc.input_value() == "60", "第二次拒收回滚到了脏快照 50"
        assert abs(float(_disk(live, "click_hz")) - 60) < 1e-9


def test_inflight_input_wiped_with_explanation(live):
    """回滚抹掉在途输入是现行为，但 toast 必须说明「被拒 + 已恢复」。"""
    with _new_page(live, {"fire.hz": 50}) as page:
        loc = _fire(page)
        page.evaluate(_STUB_DELAY)
        loc.fill("201")
        loc.dispatch_event("change")
        page.wait_for_timeout(250)
        page.keyboard.type("8")              # 400 还没回来，用户继续输入（2018）
        assert loc.input_value() != "50"
        page.wait_for_timeout(2000)          # 1.6s 的 400 + 回滚落定
        assert loc.input_value() == "50", "回滚没有恢复上一次保存值"
        txt = " / ".join(_toasts(page))
        assert "本次未生效" in txt and "已恢复上次保存的值" in txt, txt


def test_caret_restore_keeps_insert_point(live):
    """非法值被拒回滚后，插入点回到提交前的位置：中间插入得到 590 而不是 509。"""
    with _new_page(live, {"fire.hz": 50}) as page:
        loc = _fire(page)
        loc.click()
        loc.fill("201")
        loc.press("ArrowLeft")
        loc.press("ArrowLeft")               # 插入点落在 2 和 0 之间（索引 1）
        with page.expect_response(_POST_PRED, timeout=15000) as ri:
            loc.dispatch_event("change")
        assert ri.value.status == 400
        page.wait_for_timeout(500)
        assert loc.input_value() == "50", "回滚后控件没有回到 50"
        assert page.evaluate(
            "() => (document.activeElement && document.activeElement.dataset.sw) || ''"
        ) == "fire.hz", "回滚后焦点丢了"
        page.keyboard.type("9")              # 在中间插入，期望 590
        assert loc.input_value() == "590", \
            f"插入点被重置到末尾：{loc.input_value()}（509 即旧缺陷）"
