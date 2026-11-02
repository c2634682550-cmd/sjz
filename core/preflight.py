"""启动前检查：五件事全通过才允许点「启动」。

为什么要有这个：抢购失败最常见的原因不是引擎写错，而是**开工前的环境不对**
——游戏没窗口化、OCR 语言包没装、没校时、站在推荐页上、确认按钮还没框选。
这些错现在都要等到「点了启动、跑一轮、看日志」才暴露，而那时候已经错过一批货。

照竞品（Axon·skin）的做法：启动按钮由检查项卡住，而不是靠人记得检查。
每一项都要给出「怎么修」——只说「未通过」等于让人自己猜。

分两层：
  `evaluate(facts, mode)` 纯函数，只做判断 —— 测试打这里
  `collect(cfg, mode)`    真的去抓屏幕 / 问 OCR / 读校时 / 认页面
"""

# 坐标全是 1920×1080 实测值，尺寸不对就一切都不对
WANT_SIZE = (1920, 1080)
# 时钟偏差超过这个就当没过：倒计时抢购 200ms 已经够错过一批货
# 校时偏差的上限。为什么是 2 秒而不是 200ms：
#   `timesync.true_now()` = time.time() + offset，偏差是**被补偿掉**的
#   （引擎取开卖时刻、调度器排任务都用 true_now）。所以 400ms 的本机偏差
#   校准后只剩 ±10ms（NTP 往返误差），不影响掐点。
#   真正该拦的是「大得离谱」——偏差几秒通常意味着系统时间错得离谱，或者
#   NTP 那次测量本身有问题，这时候不该信它。
CLOCK_TOLERANCE_S = 2.0

# 完整性级别（RID）→ 中文。UIPI：低权限进程不能给高权限窗口发输入消息。
_INTEGRITY_CN = {0x1000: "低", 0x2000: "普通", 0x3000: "管理员", 0x4000: "系统"}


def _ck(cid, label, ok, detail, fix=None, warn=False):
    return {"id": cid, "label": label, "ok": bool(ok),
            "detail": detail, "fix": fix, "warn": bool(warn)}


def evaluate(facts, mode="list"):
    """facts → 检查项列表。mode: "list" 在售列表 / "detail" 详情页按钮。"""
    out = []
    is_list = mode != "detail"

    # ① 游戏窗口：抓得到屏幕 + 分辨率对得上
    size = facts.get("size")
    if not size:
        out.append(_ck("window", "游戏窗口", False,
                       facts.get("screen_error") or "抓不到屏幕",
                       {"hint": "检查游戏是否在运行、显示器是否被占用"}))
    elif tuple(size) != WANT_SIZE:
        out.append(_ck("window", "游戏窗口", False,
                       f"{size[0]}×{size[1]}（坐标按 1920×1080 标的）",
                       {"hint": "把游戏改成窗口化 1920×1080"}))
    else:
        out.append(_ck("window", "游戏窗口", True, f"{size[0]}×{size[1]}"))

    # ② OCR：没有它读不出商品名和价格，等于瞎抢
    if facts.get("ocr"):
        detail = "就绪" + (f" · {facts['ocr_name']}" if facts.get("ocr_name") else "")
        svc = facts.get("ocr_service") or {}
        if svc.get("managed"):
            up = svc.get("uptime_s") or 0
            detail += f" · 服务已跑 {up / 3600:.1f}h"
            if svc.get("restarts"):
                detail += f"，重启 {svc['restarts']} 次"
        out.append(_ck("ocr", "OCR 识别", True, detail))
    else:
        name = facts.get("ocr_name") or ""
        out.append(_ck("ocr", "OCR 识别", False,
                       f"不可用{('（' + name + '）') if name else ''}",
                       {"hint": "先起 OCR 服务：.venv-vl\\Scripts\\paddlex.exe "
                                "--serve --pipeline tools\\OCR_game.yaml "
                                "--port 8081 --device gpu:0"}))

    # ③ 时间校准
    clock = facts.get("clock") or {}
    off = clock.get("offset")
    # 没校过时 offset 是 0.0（不是 None），只看 offset 会把「从没校准」当成
    # 「偏差 0ms」放行 —— 那是拿没校过的钟去掐点。
    if off is None or clock.get("at") is None:
        out.append(_ck("clock", "时间校准", False, "还没校过时",
                       {"tab": "run", "hint": "点「重新校时」"}))
    elif abs(off) > CLOCK_TOLERANCE_S:
        out.append(_ck("clock", "时间校准", False,
                       f"{off * 1000:+.0f} ms（超过 {CLOCK_TOLERANCE_S:.0f} 秒，"
                       "先把系统时间对一下再校时）",
                       {"tab": "run", "hint": "重新校时"}))
    else:
        out.append(_ck("clock", "时间校准", True,
                       f"{(clock.get('server') or 'NTP')} {off * 1000:+.0f} ms"
                       "（已补偿）"))

    # ④ 按钮模板：靠它判断「购买按钮出现了」「确认弹窗出来了」
    tpl = facts.get("templates") or {}
    reg = facts.get("regions") or {}
    if is_list:
        if not reg.get("list"):
            out.append(_ck("templates", "列表区域", False, "还没框选交易行列表",
                           {"hint": "列表区域在配置里标定；缺了要把游戏停在在售页重标一次"}))
        else:
            out.append(_ck("templates", "列表区域", True, "已框选"))
            # 「确认」模板不再拦启动：这一版界面点价格按钮直接成交，没有弹窗
            # 可截。没框它的时候引擎按余额变化判成交（`_buy_without_dialog`）。
            note = ("确认按钮已框选" if tpl.get("confirm")
                    else "没框确认按钮 —— 按余额变化判断成交")
            out[-1] = _ck("templates", "列表区域", True, "已框选 · " + note)
    else:
        if not tpl.get("buy"):
            out.append(_ck("templates", "购买按钮", False, "缺 购买",
                           {"hint": "购买按钮在配置里标定；缺了要把游戏停在商品页重标一次"}))
        else:
            out.append(_ck("templates", "购买按钮", True,
                           "购买 + 确认" if tpl.get("confirm") else "购买（无弹窗界面）"))

    # ⑤ 程序权限：比游戏低的话，鼠标键盘消息全被 UIPI 拦掉
    integ = facts.get("integrity") or {}
    ours, game = integ.get("ours"), integ.get("game")
    if ours is None:
        # 读不到就当没问题 —— 拦错了比不拦更烦
        out.append(_ck("integrity", "程序权限", True, "读不到权限信息"))
    elif game is not None and game > ours:
        out.append(_ck("integrity", "程序权限", False,
                       f"控制台是{_INTEGRITY_CN.get(ours, hex(ours))}权限，"
                       f"游戏是{_INTEGRITY_CN.get(game, hex(game))}权限",
                       {"hint": "关掉控制台，用管理员身份重新运行 start.bat"}))
    else:
        out.append(_ck("integrity", "程序权限", True,
                       f"{_INTEGRITY_CN.get(ours, hex(ours))}权限"))

    # ⑤·5 输入驱动：interception 没就绪也能跑（自动退 legacy），但开抢前
    # 要让人知道现在走的是哪条通道 —— 慢 100 倍的事不能开抢了才发现。
    # ok=False + warn=True：不算「没过」（不拦启动），但界面亮警示。
    drv = facts.get("driver") or {}
    if drv.get("ok"):
        out.append(_ck("driver", "输入驱动", True, "Interception 就绪（内核注入）"))
    else:
        out.append(_ck("driver", "输入驱动", False,
                       "未就绪，自动退回 PostMessage（点击慢 ~100 倍）",
                       {"hint": drv.get("why")
                        or "管理员运行 install-interception.exe install 后重启"},
                       warn=True))

    # ⑥ 游戏页面：站错页是最常见的空跑
    page = facts.get("page")
    PAGE_CN = {"onsale": "在售页", "detail": "详情页", "recommend": "推荐页",
               "market": "市场首页", "unknown": "认不出"}
    if page is None:
        out.append(_ck("page", "游戏页面", False,
                       facts.get("page_error") or "认不出当前页面",
                       {"hint": "确认游戏窗口在前面，没被别的程序挡住"}))
    elif page == "unknown":
        # 认不出就别放行：可能是别的窗口盖着游戏，也可能在某个不认识的页面
        out.append(_ck("page", "游戏页面", False, "认不出当前页面",
                       {"hint": "把游戏切到交易行，停在「在售」页"}))
    elif is_list and page != "onsale":
        out.append(_ck("page", "游戏页面", False,
                       f"{PAGE_CN.get(page, page)}（列表模式要在在售页）",
                       {"hint": "切到交易行的「在售」页"}))
    else:
        out.append(_ck("page", "游戏页面", True, PAGE_CN.get(page, page)))

    return out


def all_ok(checks):
    # warn 项不拦启动：它们是「能跑但降级」（如驱动没装退 legacy），
    # 拦下来的话本来能抢的场会被一项慢通道警告卡死。ok=False+warn=True
    # 只在界面上亮警示。
    return all(c["ok"] or c.get("warn") for c in checks)


def collect(cfg, mode="list"):
    """真的去查一遍。慢的只有「认页面」那一步（抓屏 + OCR，约 1 秒）。"""
    from . import ocr, timesync
    from .input import Mouse, process_integrity, visible_max_integrity
    from .nav import Navigator
    from .screen import Screen

    facts: dict = {"templates": {n: bool(cfg.has_template(n)) for n in ("buy", "confirm")},
                   "regions": {n: bool(cfg.region(n)) for n in ("buy", "confirm", "list", "clock")}}
    try:
        facts["ocr"] = bool(ocr.available())
        facts["ocr_name"] = ocr.backend_name()
    except Exception:
        facts["ocr"], facts["ocr_name"] = False, ""
    try:
        from . import ocr_service
        svc = ocr_service.current()
        facts["ocr_service"] = svc.status() if svc is not None else None
    except Exception:
        facts["ocr_service"] = None
    try:
        facts["clock"] = dict(timesync.info())
    except Exception:
        facts["clock"] = {}

    # 权限：游戏跑得比我们高（UIPI）的话，鼠标键盘消息全被系统拦掉，
    # 而且**一声不吭** —— 症状是「点了没反应」，看起来像代码坏了。
    # 查的是**所有可见窗口**的最高权限，不是只看前台 —— 游戏被别的
    # 窗口盖住时，前台查不到它的权限，这一项就漏了。
    try:
        facts["integrity"] = {"ours": process_integrity(),
                              "game": visible_max_integrity()}
    except Exception:
        facts["integrity"] = {}

    # 输入驱动（interception）：没就绪不拦启动（自动退 legacy 能跑），
    # 但检查项里要亮出来 —— 别等开抢了才发现点击慢百倍。
    if cfg.get("input_mode") == "interception":
        try:
            from .interception import driver_ready
            ok, why = driver_ready()
            facts["driver"] = {"ok": bool(ok), "why": why or None}
        except Exception as e:
            facts["driver"] = {"ok": False, "why": f"{type(e).__name__}: {e}"}
    else:
        facts["driver"] = {"ok": False, "why": None}

    s = None
    try:
        s = Screen(cfg["monitor"])
        facts["size"] = tuple(s.size)
    except Exception as e:
        facts["size"] = None
        facts["screen_error"] = f"抓不到屏幕：{type(e).__name__}: {e}"

    if s is not None and facts.get("ocr"):
        try:
            facts["page"] = Navigator(s, Mouse(s)).where()[0]
        except Exception as e:
            facts["page"] = None
            facts["page_error"] = f"认不出页面：{type(e).__name__}: {e}"
    try:
        if s is not None:
            s.close()
    except Exception:
        pass
    return facts
