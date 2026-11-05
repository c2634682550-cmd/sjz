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
            "game_hwnd": True, "game_fore": True,
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


def test_window_size_is_judged_by_ratio_not_a_hardcoded_number():
    """分辨率判定按「比例 + 配置容差」，不再写死 1920×1080。

    默认 tolerance=0 时保持老行为（差一点都不行）；放宽容差后，
    等比例放大的窗口（比如 2560×1440）就该放行。
    """
    assert preflight._size_ok((1920, 1080), {"want_size": (1920, 1080),
                                             "size_tolerance": 0.0}) is True
    # 等比例放大 4/3，容差 0 时仍然红
    assert preflight._size_ok((2560, 1440), {"want_size": (1920, 1080),
                                             "size_tolerance": 0.0}) is False
    # 容差放到 35% 就放行
    assert preflight._size_ok((2560, 1440), {"want_size": (1920, 1080),
                                             "size_tolerance": 0.35}) is True
    # 基准改了以后，按新基准比对
    assert preflight._size_ok((2560, 1440), {"want_size": (2560, 1440),
                                             "size_tolerance": 0.0}) is True
    # 非等比（改了宽没改高）即使容差相同也不算过
    assert preflight._size_ok((2304, 1440), {"want_size": (1920, 1080),
                                             "size_tolerance": 0.05}) is False
    # 坏数据不崩
    assert preflight._size_ok(None, {"want_size": (1920, 1080)}) is False
    assert preflight._size_ok(("x", "y"), {"want_size": (1920, 1080)}) is False


def test_ocr_missing_blocks_the_start():
    checks = preflight.evaluate(_facts(ocr=False), "list")
    assert not _by_id(checks, "ocr")["ok"]


def test_ocr_booting_is_not_reported_as_broken():
    """OCR 服务冷启动（Paddle 加载模型十几秒）时不能说「不可用」。

    冷启动那一瞬间 ocr.available() 必然 False，但原因不是坏了 —— 报「不可用」
    会让人去手敲启动命令，其实再等十几秒它自己就绪。文案要能分开。
    """
    facts = _facts(ocr=False, ocr_name="不可用（http://127.0.0.1:8081 连不上）",
                   ocr_service={"enabled": True, "healthy": False,
                                "starting": True, "last_error": None})
    c = _by_id(preflight.evaluate(facts, "list"), "ocr")
    assert not c["ok"], "启动窗口里也不该放行"
    assert "启动" in c["detail"] and "不可用" not in c["detail"]


def test_ocr_failure_detail_is_not_double_wrapped():
    """backend_name() 自带「不可用（…）」，外面别再包一层。"""
    facts = _facts(ocr=False, ocr_name="不可用（http://127.0.0.1:8081 连不上）",
                   ocr_service={"enabled": True, "healthy": True,
                                "starting": False, "last_error": None})
    c = _by_id(preflight.evaluate(facts, "list"), "ocr")
    assert c["detail"].count("不可用") == 1, c["detail"]


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


def test_standing_on_another_market_page_warns_but_does_not_block():
    """停在推荐页/市场首页 → 提示，但**不拦**启动。

    为什么改了：引擎的 `_navigate` 会自己走 go_to_onsale()（F4 → 典藏外观
    → 选皮肤 → 购买 → 在售页），`recover()` 还专门处理「用户点开始时停在
    任意位置」。在预检里拦死，等于让人先手动切页再点开始，白点一次。

    真机踩过的教训（推荐页顶部也有「在售/推荐」）保留在别处：页面**识别**
    要准，认不出（unknown）照样拦 —— 见下面那条。
    """
    checks = preflight.evaluate(_facts(page="recommend"), "list")
    c = _by_id(checks, "page")
    assert c["ok"] and c.get("warn"), c
    assert "推荐页" in c["detail"]
    assert c["fix"]["hint"], "要告诉人可以先切到在售页"


def test_unknown_page_blocks_the_start():
    checks = preflight.evaluate(_facts(page=None, page_error="认不出页面：X"), "list")
    assert not _by_id(checks, "page")["ok"]
    # 游戏就在前台，却认不出页面（对局里、加载中）→ 不许启动
    checks2 = preflight.evaluate(_facts(page="unknown"), "detail")
    assert not _by_id(checks2, "page")["ok"], "详情模式把「认不出」当通过，等于没检查"


def test_resolution_ok_but_no_game_window_is_called_out():
    """分辨率对得上 ≠ 游戏开着 —— 桌面本来就是 1920×1080。

    以前这里报绿「游戏窗口 1920×1080」，用户以为环境没问题，点开始白跑一轮。
    不拦死（窗口类名万一对不上，拦了就是误拦），但必须把「没找到」说出来。
    """
    checks = preflight.evaluate(_facts(game_hwnd=False), "list")
    c = _by_id(checks, "window")
    assert c["ok"] and c.get("warn"), c
    assert "没找到游戏窗口" in c["detail"]
    assert preflight.all_ok(checks), "窗口找不到是警告，拦启动的活由 page 那项干"


def test_game_window_missing_blocks():
    checks = preflight.evaluate(_facts(page="unknown", game_hwnd=False,
                                       game_fore=False), "list")
    c = _by_id(checks, "page")
    assert not c["ok"] and not c.get("warn"), "连窗口都没有还放行，等于对着桌面盲点"
    assert "没找到游戏窗口" in c["detail"]


def test_game_behind_console_warns_instead_of_blocking():
    """游戏被控制台挡着 → 只是警告，不拦启动。

    抓屏抓的是桌面，控制台压在上面时页面永远认不出 —— 这是检查方式自带的
    偏差，不是环境错。拦死的后果：点「开始」→ 报认不出 → 切游戏看一眼 →
    切回来再点 → 又被拦。引擎开工第一步会 ensure_foreground + _navigate。
    """
    checks = preflight.evaluate(_facts(page="unknown", game_hwnd=True,
                                       game_fore=False), "list")
    c = _by_id(checks, "page")
    assert not c["ok"], "认不出就是认不出，不能报绿"
    assert c.get("warn"), "被挡着要按警告放行"
    assert preflight.all_ok(checks), "警告项不能拦启动"
    assert "挡" in c["detail"] and c["fix"]["hint"]


def test_all_ok_is_false_when_any_single_check_fails():
    """一条「真拦」的没过就不能启动 —— 这正是竞品「还有 1 项未通过」的做法。

    注意：**校时不再属于「真拦」的那一类**（偏差是被补偿掉的，倒计时路线
    也不看挂钟），所以这里用「认不出页面」来验，别拿 clock 当拦路虎 ——
    见 test_clock_drift_is_warn_only。
    """
    checks = preflight.evaluate(_facts(page="unknown", game_fore=True), "list")
    assert not _by_id(checks, "page")["ok"], "认不出页面必须拦"
    assert not preflight.all_ok(checks), "有一条没过就必须拦"
    # warn 项（驱动未就绪）单独验证，见 test_driver_not_ready_is_warn_only


def test_clock_drift_is_warn_only():
    """校时偏差只警告、不拦启动。

    偏差是**补偿**掉的（timesync.true_now 会加上 offset），而且倒计时那条
    路线盯的是按钮状态跃迁、根本不看挂钟。以前这里判 False 且不标 warn，
    等于「NTP 一次没通就永远开不了工」—— 用户看到的就是点了没反应。
    """
    checks = preflight.evaluate(_facts(clock={"offset": 5.0, "at": 1700000000.0}), "list")
    c = _by_id(checks, "clock")
    assert not c["ok"], "偏差大要亮出来，不能报绿"
    assert c.get("warn"), "只警告，不拦启动"
    assert preflight.all_ok(checks), "校时不过不能拦住开抢"

    never = preflight.evaluate(_facts(clock={"offset": 0.0, "at": None}), "list")
    c2 = _by_id(never, "clock")
    assert not c2["ok"] and c2.get("warn"), "从没校过时也要能开抢，但要提示"
    assert "不影响" in c2["detail"], c2["detail"]
