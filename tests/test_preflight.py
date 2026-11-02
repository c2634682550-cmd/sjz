"""启动前检查的判断规则：错一条就不许点「启动」。

这些规则都是「错了就会白跑一轮」的环境问题：
  - 分辨率不对 → 所有坐标全偏（全是 1920×1080 实测的）
  - 没 OCR      → 读不出商品名和价格，等于瞎点
  - 没校时      → 倒计时抢购差 200ms 就错过一批货
  - 缺模板      → 判断不了「购买按钮出现了」「确认弹窗出来了」
  - 站错页      → 在推荐页上点筛选、把榜单当商品行扫（真机踩过）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import preflight  # noqa: E402


def _facts(**kw):
    base = {"size": (1920, 1080), "ocr": True, "ocr_name": "WinRT zh-Hans",
            "clock": {"offset": 0.012, "at": 1700000000.0, "server": "ntp.aliyun.com"},
            "templates": {"buy": True, "confirm": True},
            "regions": {"buy": True, "confirm": True, "list": True, "clock": True},
            "page": "onsale",
            "driver": {"ok": True, "why": None}}
    base.update(kw)
    return base


def _by_id(checks, cid):
    return next(c for c in checks if c["id"] == cid)


def test_everything_green_passes():
    checks = preflight.evaluate(_facts(), "list")
    assert preflight.all_ok(checks), [(c["label"], c["detail"]) for c in checks if not c["ok"]]
    assert [c["id"] for c in checks] == ["window", "ocr", "clock", "templates",
                                        "integrity", "driver", "page"]


def test_driver_not_ready_is_warn_only():
    """驱动没装：亮警示但**不拦启动** —— 自动退 legacy 还能跑。

    warn 项不算「没过」：all_ok 必须仍是 True，否则六项检查里多出的
    这一项会把本来能开抢的场拦死。
    """
    checks = preflight.evaluate(_facts(driver={"ok": False, "why": "没装"}), "list")
    c = _by_id(checks, "driver")
    assert not c["ok"]
    assert c["warn"], "驱动未就绪必须是警示，不能拦启动"
    assert preflight.all_ok(checks), "warn 项不参与拦截"
    assert "PostMessage" in c["detail"]


def test_wrong_resolution_blocks_the_start():
    """坐标是 1920×1080 实测的，别的分辨率一律不许开工。"""
    checks = preflight.evaluate(_facts(size=(2560, 1440)), "list")
    c = _by_id(checks, "window")
    assert not c["ok"]
    assert "2560×1440" in c["detail"]
    assert c["fix"] and c["fix"]["hint"], "光说没过不够，得告诉人怎么改"
    assert not preflight.all_ok(checks)


def test_missing_screen_does_not_crash():
    checks = preflight.evaluate(_facts(size=None, screen_error="抓不到屏幕：X"), "list")
    c = _by_id(checks, "window")
    assert not c["ok"] and "抓不到屏幕" in c["detail"]


def test_ocr_missing_blocks_the_start():
    checks = preflight.evaluate(_facts(ocr=False), "list")
    assert not _by_id(checks, "ocr")["ok"]


def test_clock_never_synced_blocks_the_start():
    checks = preflight.evaluate(_facts(clock={"offset": None}), "list")
    c = _by_id(checks, "clock")
    assert not c["ok"] and "没校" in c["detail"]


def test_clock_not_synced_yet_blocks_the_start_even_at_zero_offset():
    """没校过时 offset 是 0.0，不是 None。

    只看 offset 会把「一次都没校过」当成「偏差 0ms」放行 ——
    那是拿没校准的钟去掐点，一秒能差半秒。
    """
    checks = preflight.evaluate(_facts(clock={"offset": 0.0, "at": None}), "list")
    c = _by_id(checks, "clock")
    assert not c["ok"] and "没校" in c["detail"]


def test_clock_drift_is_compensated_not_blocked():
    """本机偏差是被**补偿**掉的，不该拦。

    引擎取开卖时刻、调度器排任务都用 `timesync.true_now()`
    （= time.time() + offset）。所以本机钟差 350ms 校准后只剩 ±10ms 的
    NTP 往返误差，不影响掐点。

    以前这条按「偏差 > 200ms 就拦」写，结果这台机器钟差 396ms，
    启动前检查永远红 —— 用户看到的是「明明校过时了还说不行」。
    """
    checks = preflight.evaluate(_facts(clock={"offset": 0.35, "at": 1700000000.0}), "list")
    c = _by_id(checks, "clock")
    assert c["ok"], c["detail"]
    assert "已补偿" in c["detail"], "要说清楚偏差是被补偿掉的，不是被忽略"


def test_absurd_clock_drift_blocks_the_start():
    """偏差大到几秒就别信这次测量了 —— 多半系统时间本身错得离谱。"""
    checks = preflight.evaluate(_facts(clock={"offset": 3.5, "at": 1700000000.0}), "list")
    assert not _by_id(checks, "clock")["ok"]


def test_missing_confirm_template_does_not_block_the_start():
    """这一版界面点价格按钮直接成交，没有确认弹窗可截。

    缺「确认」模板时不能再拦启动 —— 引擎改按余额变化判断成没成交，
    否则任务永远加不上、启动永远被卡。
    """
    checks = preflight.evaluate(_facts(templates={"buy": True, "confirm": False}), "list")
    c = _by_id(checks, "templates")
    assert c["ok"], "缺确认模板不该拦启动"
    assert "余额" in c["detail"], "要说明改成靠什么判断成交"


def test_list_mode_needs_the_list_region():
    checks = preflight.evaluate(_facts(regions={"list": False, "confirm": True}), "list")
    c = _by_id(checks, "templates")
    assert not c["ok"] and "列表" in c["detail"]


def test_detail_mode_needs_both_buttons():
    checks = preflight.evaluate(
        _facts(templates={"buy": False, "confirm": True}), "detail")
    c = _by_id(checks, "templates")
    assert not c["ok"] and "购买" in c["detail"]


def test_standing_on_the_recommend_page_blocks_list_mode():
    """真机踩过：推荐页顶部也有「在售/推荐」，判据松了就把它当在售页。"""
    checks = preflight.evaluate(_facts(page="recommend"), "list")
    c = _by_id(checks, "page")
    assert not c["ok"] and "推荐页" in c["detail"]
    assert c["fix"]["hint"], "要告诉人切到在售页"


def test_unknown_page_blocks_the_start():
    checks = preflight.evaluate(_facts(page=None, page_error="认不出页面：X"), "list")
    assert not _by_id(checks, "page")["ok"]
    # 详情模式下也认不出页面 → 同样不许启动（可能是别的窗口盖着游戏）
    checks2 = preflight.evaluate(_facts(page="unknown"), "detail")
    assert not _by_id(checks2, "page")["ok"], "详情模式把「认不出」当通过，等于没检查"


def test_all_ok_is_false_when_any_single_check_fails():
    """一条没过就不能启动 —— 这正是竞品「还有 1 项未通过」的做法。"""
    checks = preflight.evaluate(_facts(clock={"offset": 5.0, "at": 1700000000.0}), "list")
    assert not preflight.all_ok(checks), "clock 没过必须拦"
    # warn 项（驱动未就绪）单独验证，见 test_driver_not_ready_is_warn_only
