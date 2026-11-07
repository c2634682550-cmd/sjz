"""dry-run 回放工装的静态契约（不碰真实驱动、不碰屏幕）。

覆盖四件事：
  1. 帧回放路径绝不引用注入栈（源码级断言）
  2. `enter_market` 幂等：已在交易行（含在售/详情子页）→ 零输入
  3. 入口两条路径各只发一次（F4 一次按键 / 图标一次点击），都失败就抛错
  4. dry-run 落点判定：客户区 / 按钮 bbox 越界要能数出来
"""
import importlib.util
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAV_FRAMES = os.path.join(ROOT, "tests", "fixtures", "nav_frames",
                          "hall_to_market.jsonl")
PRICE_LIST = os.path.join(ROOT, "tests", "fixtures", "market", "list_filtered.png")


# ---------------------------------------------------------------- 桩对象

class _StubScreen:
    size = (1920, 1080)
    base_rect = {"left": 0, "top": 0, "width": 1920, "height": 1080}

    def grab(self, box=None):
        return None


class _StubKeyboard:
    def __init__(self):
        self.taps = []

    def tap(self, key):
        self.taps.append(key)
        return True


class _SeqNav:
    """where() 按给定序列返回，跑完重复最后一个值。"""

    def make(self, pages, keyboard=None, opts=None):
        from core import dry_run
        from core.nav import Navigator

        outer = self

        class _Nav(Navigator):
            def __init__(self):
                super().__init__(_StubScreen(),
                                 dry_run.DryRunMouse(_StubScreen()),
                                 keyboard=keyboard,
                                 log=lambda msg, level="info": None,
                                 opts=opts)
                self.pages = list(pages)
                self.seen = []

            def where(self):
                page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
                self.seen.append(page)
                return page, ""

        return _Nav()


def _load_tool():
    path = os.path.join(ROOT, "tools", "replay_dryrun.py")
    spec = importlib.util.spec_from_file_location("replay_dryrun_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 1. 零注入（源码级）

def test_replay_tool_never_references_injection():
    import ast

    src = open(os.path.join(ROOT, "tools", "replay_dryrun.py"),
               "r", encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    bad = [m for m in mods
           if m.split(".")[-1] in ("input", "interception", "winput")
           or "interception" in m]
    assert bad == [], f"回放工具引入了注入栈: {bad}"
    calls = [n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("Mouse", "Keyboard")]
    assert calls == [], f"回放工具实例化了注入对象: {calls}"
    tool = _load_tool()
    audit = tool.injection_audit()
    assert audit["injections"] == 0
    assert audit["ok"] is True


def test_dry_mouse_records_but_never_injects():
    from core import dry_run
    m = dry_run.DryRunMouse(_StubScreen())
    assert m.click(100, 200) is True
    assert m.wheel(300, 400, 2) is True
    assert m.injections == 0
    assert len(m.events) == 2
    assert m.events[0]["in_client"] is True


def test_point_report_bounds_and_bbox():
    from core import dry_run
    ok = dry_run.point_report(_StubScreen(), 100, 200,
                              bbox=(0, 0, 300, 300))
    assert ok["in_client"] is True and ok["in_bbox"] is True
    out_box = dry_run.point_report(_StubScreen(), 500, 900,
                                   bbox=(0, 0, 300, 300))
    assert out_box["in_client"] is True and out_box["in_bbox"] is False
    off_screen = dry_run.point_report(_StubScreen(), -10, 1100)
    assert off_screen["in_client"] is False


def test_engine_click_in_dry_mode_bypasses_real_mouse(monkeypatch):
    from core import engine
    from core import input as input_mod
    from core import dry_run

    def boom(*_a, **_k):
        raise AssertionError("dry-run 里不该调用真实 Mouse.click")

    monkeypatch.setattr(input_mod.Mouse, "click", boom, raising=False)
    eng = engine.Engine(dry_run=True)
    assert eng._dry is True
    m = dry_run.DryRunMouse(None)
    eng._click(m, 100, 200)                     # 不抛就是没注入
    assert m.injections == 0 and len(m.events) == 1


# ---------------------------------------------------------------- 2/3. 入口幂等与次数

@pytest.mark.parametrize("page", ["market", "onsale", "detail",
                                  "filter_onsale", "filter_collection"])
def test_enter_market_idempotent_no_input(page):
    nav = _SeqNav().make([page], keyboard=_StubKeyboard())
    got = nav.enter_market(timeout=0.01)        # 不传 rounds，用默认值
    assert got == page
    assert nav.keyboard.taps == []              # 一次 F4 都没按
    assert [e for e in nav.mouse.events if e["kind"] == "click"] == []


def test_market_rounds_default_is_one():
    from core.config import DEFAULTS
    from core.nav import NAV_DEFAULTS
    assert NAV_DEFAULTS["market_rounds"] == 1
    assert DEFAULTS["market_entry"]["market_rounds"] == 1


def test_enter_market_f4_path_one_tap():
    kb = _StubKeyboard()
    nav = _SeqNav().make(["hall", "market"], keyboard=kb)
    got = nav.enter_market(timeout=0.01)
    assert got == "market"
    assert kb.taps == ["F4"]
    assert [e for e in nav.mouse.events if e["kind"] == "click"] == []


def test_enter_market_icon_path_one_click():
    """图标路径是实验性的：显式打开时才走（默认关闭，见下一个用例）。"""
    nav = _SeqNav().make(["hall", "market"], keyboard=None,
                         opts={"market_icon_enabled": True})
    got = nav.enter_market(timeout=0.01)
    assert got == "market"
    clicks = [e for e in nav.mouse.events if e["kind"] == "click"]
    assert len(clicks) == 1
    # 归一化坐标走 NAV_DEFAULTS，落点必须与配置换算一致（不再写死像素）
    from core.nav import NAV_DEFAULTS
    nx, ny = NAV_DEFAULTS["market_icon"]
    want = tuple(int(v) for v in nav.pt((nx * 1920, ny * 1080)))
    assert (clicks[0]["x"], clicks[0]["y"]) == want
    assert clicks[0]["in_client"] is True


def test_enter_market_both_fail_raises_without_extra_click():
    """默认 rounds=1：F4 一次 + 图标一次都失败就抛错，总共最多 2 次输入。"""
    kb = _StubKeyboard()
    nav = _SeqNav().make(["hall"], keyboard=kb,
                         opts={"market_icon_enabled": True})
    with pytest.raises(Exception) as ei:
        nav.enter_market(timeout=0.01)
    msg = str(ei.value)
    assert "进不了交易行" in msg
    assert kb.taps == ["F4"]                    # 只按一次
    clicks = [e for e in nav.mouse.events if e["kind"] == "click"]
    assert len(clicks) == 1                     # 只点一次，没有继续点
    assert len(kb.taps) + len(clicks) == 2      # 输入总次数上限 = 1 按 + 1 点


def test_enter_market_icon_disabled_by_default():
    """图标路径「未验证/不可用」：默认一个点击都不发，错误里写清原因。"""
    nav = _SeqNav().make(["hall"], keyboard=None)
    with pytest.raises(Exception) as ei:
        nav.enter_market(timeout=0.01)
    assert "图标路径已关闭" in str(ei.value)
    assert [e for e in nav.mouse.events if e["kind"] == "click"] == []


def test_popup_frame_classified_as_popup():
    """余额不足弹窗盖在在售页上：必须判 popup，不能落在 MARKET_PAGES。"""
    from core.nav import classify, MARKET_PAGES
    text = ("推荐不限公示期默认排序MK4冲锋枪疾光魅影您当前的三角币不足，"
            "是否前往充值？（缺少2,001三角币，推荐充值328档位）取消充值")
    assert classify(text) == "popup"
    assert "popup" not in MARKET_PAGES
    assert classify("您当前的三角币不足是否前往充值取消充值") == "popup"


def test_enter_market_popup_sends_no_input():
    """入口遇到弹窗：一个输入都不发，直接抛明确错误。"""
    from core.nav import NavError
    kb = _StubKeyboard()
    nav = _SeqNav().make(["popup"], keyboard=kb)
    with pytest.raises(NavError) as ei:
        nav.enter_market(timeout=0.01)
    assert "弹窗" in str(ei.value)
    assert kb.taps == []
    assert [e for e in nav.mouse.events if e["kind"] == "click"] == []


def _frame_text(frame_id):
    with open(NAV_FRAMES, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            it = json.loads(line)
            if it["id"] == frame_id:
                return it["text"]
    raise AssertionError(f"回放集里没有 {frame_id}")


# 反向风险：交易行页面本身会带这些词（按钮/页脚/活动横幅）。
# 弹窗判据一旦单词命中，这些页面会被判成 popup，入口永远进不去。
_BUTTON_WORDS = ["确定", "取消", "推荐充值", "余额", "提示",
                 "取消确定提示", "确定取消推荐充值余额提示"]


@pytest.mark.parametrize("extra", _BUTTON_WORDS)
@pytest.mark.parametrize("frame_id,want", [
    ("09_market_full", "market"),
    ("11_onsale_after_entry", "onsale"),
])
def test_market_frame_with_button_words_not_popup(frame_id, want, extra):
    from core.nav import classify

    text = _frame_text(frame_id) + extra
    got = classify(text)
    assert got != "popup", f"{frame_id}+「{extra}」被误判成 popup"
    assert got == want, f"{frame_id}+「{extra}」判成 {got}"


def test_popup_requires_core_phrase_or_combination():
    """弹窗判据是组合，不是单词：单词命中一律不成立。"""
    from core.nav import classify

    # 单个充值引导语 / 余额 / 按钮词都不算弹窗
    for word in ("推荐充值", "前往充值", "余额", "取消", "确定", "缺少", "档位"):
        assert classify(word) != "popup", f"「{word}」单独命中就判了弹窗"
    # 引导语 + 缺额行（组合）才算
    assert classify("推荐充值缺少三角币") == "popup"
    # 唯一整句单独命中就算
    assert classify("您当前的三角币不足") == "popup"


# ---------------------------------------------------------------- 4. 过渡/加载帧判定

def test_transition_and_loading_frames_never_market():
    from core.nav import classify, MARKET_PAGES
    with open(NAV_FRAMES, "r", encoding="utf-8") as f:
        items = [json.loads(x) for x in f if x.strip()]
    assert items
    for it in items:
        page = classify(it["text"])
        want = it["expect"]
        if want in ("transition", "loading"):
            assert page not in MARKET_PAGES, f"{it['id']} 误判成 {page}"
            assert page in ("hall", "unknown", "modeselect"), \
                f"{it['id']} 过渡帧出现意外页面 {page}"
        elif want == "hall":
            assert page == "hall", f"{it['id']} → {page}"
        elif want == "market":
            assert page in MARKET_PAGES, f"{it['id']} → {page}"


def test_transition_frame_corpus_has_all_three_phases():
    with open(NAV_FRAMES, "r", encoding="utf-8") as f:
        phases = {json.loads(x)["phase"] for x in f if x.strip()}
    assert {"transition", "loading", "market"} <= phases


# ---------------------------------------------------------------- 5. 列表决策字段（要 OCR）

def _ocr_ready():
    try:
        from core import ocr
        from core.vision import imread
        img = imread(PRICE_LIST)
        return img is not None and len(ocr.read_words(img[:200, :400])) > 0
    except Exception:
        return False


@pytest.mark.skipif(not _ocr_ready(), reason="OCR 服务不可用")
def test_list_frame_decision_fields_and_bbox():
    from core.config import Config
    tool = _load_tool()
    from core.vision import imread

    cfg = Config.load()
    img = imread(PRICE_LIST)
    r = tool.decide_list(img, cfg, tool.DEFAULT_TASKS, None, "onsale/list")

    assert r["rows"], "列表帧没扫出卡片"
    assert isinstance(r["price_value"], list) and r["price_value"]
    assert r["ms"] > 0
    for t in r["tasks"]:
        assert set(t) >= {"task", "matched", "reason", "point"}
        if t["matched"]:
            p = t["point"]
            assert p["space"] == "client"
            assert p["bbox"][0] <= p["x"] <= p["bbox"][2]
            assert p["bbox"][1] <= p["y"] <= p["bbox"][3]
    # 严格条件（优品 ≤3000）在这张图上必须命中
    assert any(t["matched"] for t in r["tasks"] if t["task"] == "t_strict")
