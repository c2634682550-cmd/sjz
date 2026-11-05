"""前端源码级回归：这些点在单文件 index.html 里没有可测的纯函数，
但「一定要这样写」是硬要求，用源码断言守住，防止下次改回去。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "web", "index.html")


def _html():
    with open(HTML, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 演练已删除

def test_no_dry_run_left_in_the_ui():
    src = _html()
    for word in ("dry_run", "dry-run", "演练", "isDry", "run_mode"):
        assert word not in src, f"界面里还有 {word}"


def test_start_button_shows_the_only_mode():
    """状态栏显示的只能是「实战」——没有第二种模式可切。"""
    src = _html()
    assert "实战" in src
    assert "sbMode" in src


# ---------------------------------------------------------------- 注入

def test_esc_escapes_quotes_too():
    src = _html()
    assert '&#39;' in src, "esc 没转义单引号，属性值照样能被截断"
    assert '&#34;' not in src


def test_wear_tag_escapes_the_ocr_value():
    src = _html()
    start = src.index("function wearTag")
    end = src.index("function esc")
    body = src[start:end]
    assert "esc(w)" in body, "OCR 回填的 wear 又原样拼进 HTML 了"
    assert ".replace(/[^a-z0-9_-]/g, '')" in body, "class 属性要白名单过滤"


# ---------------------------------------------------------------- 启动互斥 / 轮询

def test_start_is_mutually_exclusive():
    src = _html()
    assert "if(STATE.starting) return;" in src, "启动按钮没有互斥锁"
    assert "STATE.starting = true;" in src
    assert "starting:false" in src


def test_poll_discards_stale_responses():
    src = _html()
    assert "STATE.pollSeq += 1" in src
    assert "STATE.pollGen" in src
    assert "过期响应" in src, "没有丢弃过期响应的逻辑"


def test_tier_options_come_from_the_backend_contract():
    src = _html()
    start = src.index("function fillTierOptions")
    body = src[start:start + 400]
    assert "switchSpec" in body and "filter.craft" in body, \
        "稀有度下拉不是从后端契约来的"


def test_keyboard_accessibility_is_wired():
    src = _html()
    assert 'role="switch"' in src and 'tabindex="0"' in src
    assert ":focus-visible" in src, "没有可见焦点环"
    assert "Enter" in src and "Space" in src


def test_task_name_is_escaped_in_the_queue_list():
    src = _html()
    start = src.index("function renderTasks")
    body = src[start:start + 900]
    assert "esc(" in body


def test_condition_errors_are_rendered_inline():
    src = _html()
    assert "condErr" in src, "条件非法时没有页面内提示"
    assert "showCondErr" in src
