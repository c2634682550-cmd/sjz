"""前端回归测试：守住两个「静默失败」。

这两个 bug 都不会报错、不会让页面崩，只是让某块区域一直空着 ——
玩家看得到、但查不出为什么。所以只能用结构性断言守住。

跑法：uv run pytest
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(ROOT, "web", "index.html")


@pytest.fixture(scope="module")
def html():
    if not os.path.isfile(HTML_PATH):
        pytest.skip("web/index.html 不存在")
    with open(HTML_PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def js(html):
    return html[html.index("<script>"):]


# ---------------------------------------------------------------- 预览流

def test_stream_is_started_through_the_retry_helper(html):
    """`#stream` 的 src 必须由 startStream() 设，不能直接写死一行。

    本地回归：WebView2 在页面刚加载完时设 MJPEG 的 src，连接有时根本不建立，
    而且**不触发 error 事件**（没有可捕获的失败信号）。naturalWidth 一直是 0，
    预览区空白。同一个页面里重设一次 src 就立刻好了 —— 所以只能主动重试。
    """
    assert "function startStream(" in html, "startStream 没了"
    assert "startStream()" in html, "没人调用 startStream"
    # 不该再出现裸设 src 的写法
    bare = re.findall(r"\$\('#stream'\)\.src\s*=", html)
    assert not bare, (
        f"发现 {len(bare)} 处直接给 #stream 设 src。必须走 startStream()，"
        "否则 WebView2 下连接可能不建立且不报错，预览区会一直空白。")


def test_stream_retries_until_it_gets_a_picture(js):
    """重试必须有终止条件（拿到画面就停），而不是无限重设。

    无限重设会把服务端拖死；不重试则永远空白。两头都要防。
    """
    body = js[js.index("function startStream("):]
    body = body[:body.index("\n}\n") + 3]
    assert "naturalWidth" in body, "重试判据必须看 naturalWidth（画面到了没）"
    assert "clearTimeout" in body, "重设前要清掉上一个定时器，否则会叠加"


def test_stream_restarts_when_returning_to_setup(js):
    """切回框选页时要检查流还在不在。

    `.pane { display: none }` 会让 Chromium **中止** 隐藏 <img> 的 MJPEG 请求，
    切回来不会自动恢复。不补这一步的话，玩家切去看一眼任务、再切回来，
    预览就是白的。
    """
    # 定位到 #tabs 的点击处理器
    i = js.index("$('#tabs').addEventListener('click'")
    tab = js[i:i + 1200]
    assert "dataset.tab" in tab, "标签页切换里没读 dataset.tab"
    assert "setup" in tab, "标签页切换里没有针对 setup 的处理分支"
    assert "startStream" in tab, (
        "切回框选页时没有重启流。display:none 会中止 MJPEG，切回来必须重连。")


# ---------------------------------------------------------------- 预览尺寸

def test_stream_has_explicit_size(html):
    """`#stream` 必须显式给宽高，不能只写 max-width/max-height。

    本地回归：MJPEG 的 <img> 永远不「加载完成」，在 flex 容器里按内在尺寸
    算，结果就是 0×0 —— 整块预览空白。
    """
    m = re.search(r"#stream\s*\{([^}]*)\}", html)
    assert m, "找不到 #stream 的样式"
    css = m.group(1)
    assert re.search(r"width:\s*100%", css), "#stream 缺 width: 100%"
    assert re.search(r"height:\s*100%", css), "#stream 缺 height: 100%"
    assert "object-fit" in css, (
        "#stream 缺 object-fit。给了 100% 宽高但不约束拉伸方式的话，"
        "画面会被拉变形。")


def test_overlay_uses_the_contained_image_rect(js):
    """框选坐标必须按「图片实际占的矩形」算，不能按 <img> 元素矩形。

    object-fit: contain 会留黑边；按元素矩形算的话框出来的比例是偏的，
    存进配置的区域坐标就错了 —— 而且能存能读，看不出错，只是抢的时候点偏。
    """
    assert "function streamRect(" in js, "streamRect 没了"
    body = js[js.index("function streamRect("):]
    body = body[:body.index("\n}\n") + 3]
    assert "naturalWidth" in body and "Math.min" in body, \
        "streamRect 必须按 naturalWidth/naturalHeight 和容器尺寸算 contain 后的矩形"

    # 其余地方不该再直接用元素矩形
    after = js[js.index("function streamRect("):]
    left = after.count("stream.getBoundingClientRect()")
    assert left <= 1, (
        f"streamRect 之外还有 {left} 处直接用 stream.getBoundingClientRect()。"
        "框选、画区域框、拖框都必须用 streamRect()，否则坐标会偏。")


# ---------------------------------------------------------------- 玩家看得懂

JARGON = ["整队", "布防", "触发方式", "匹配阈值", "相关度", "检测频率",
          "状态机", "模板", "Tesseract"]


def test_no_developer_jargon_in_visible_text(html):
    """玩家可见的文案里不该出现我们自己的技术词。

    保留的游戏内术语（砖皮、极品、成色 A/S/B）不算 —— 那是玩家本来就懂的。
    """
    body = html[html.index("<body>"):html.index("<script>")]
    visible = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    text = " ".join(re.findall(r">([^<>]{1,120})<", visible))
    found = [w for w in JARGON if w in text]
    assert not found, f"可见文案里还有技术词: {found}"


def test_no_jargon_in_user_facing_js_strings(js):
    """JS 里弹给用户看的字符串同样不能有技术词。

    只查字符串字面量，不查注释 —— 注释里写「模板」是在跟维护者解释
    实现细节，玩家看不到。
    """
    code = re.sub(r"//[^\n]*", "", js)          # 去行注释
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)   # 去块注释
    found = [w for w in ["整队", "布防", "模板"] if w in code]
    assert not found, f"JS 的用户可见文案里还有技术词: {found}"


def test_advanced_settings_start_collapsed(html):
    """高级参数默认折叠 —— 默认值已经能用，不该占主流程。"""
    m = re.search(r"<details class=\"adv\"[^>]*>", html)
    assert m, "找不到高级设置的 details"
    assert "open" not in m.group(0).split(">")[0].replace("class=\"adv\"", ""), \
        "高级设置不该默认展开"


def test_the_three_wizard_steps_exist(html):
    """框选页是三步向导，步骤号要在。"""
    steps = re.findall(r'<span class="stepno">(\d)</span>', html)
    assert steps == ["1", "2", "3"], f"步骤号不对: {steps}"
