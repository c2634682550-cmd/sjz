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

import time

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

# 缺模板时的统一说法（审查 R09-05）。**不再要求用户去命令行**：模板自愈
# （`core/template_heal.py`）会在每次启动前检查时按当前画面自动重采；命令行
# `--calibrate` 保留为可选的手动入口，不是必经步骤。
_CALIBRATE_FIX = {
    "hint": "缺模板。先把游戏停在对应页面（购买按钮 / 确认弹窗），然后"
            "点「一键自动标定」或直接点「重新检查」—— 程序会按当前画面"
            "自动重新采集 templates/buy.png（购买）或 templates/confirm.png"
            "（确认），采集屏与 DPI 写进 templates/meta.json（旧文件有备份，"
            "可回滚）；框选区域（regions）有内置默认值、按客户区自动换算，"
            "一般不用改。想手工框一次也可以：运行 SJZ-Control.exe --calibrate"
            "（源码 `python tools/calibrate.py`），这是可选入口。",
}

# 引擎搜索时给区域外扩的比例。**必须和 engine 的 expand() 一致**：
# buy 0.15、confirm 0.25。检查项拿「外扩后的搜索窗」比，才是引擎真正在
# 找的那块地方；拿未外扩的框选区域比会系统性误报（真机 2026-11-08：
# buy 模板 315×67 比 298×58 的按钮本体大，于是 1080p/100% 下永远红）。
SEARCH_PAD = {"buy": 0.15, "confirm": 0.25}
DEFAULT_PAD = 0.15


def _required_templates(wf):
    """按路线取真正需要的模板，与 engine `_validate` 同口径。"""
    if wf == "list":
        return ("buy",)
    if wf == "countdown":
        return ("confirm",)
    return ("buy", "confirm")


def _size_ok(size, facts):
    """按实际尺寸 / 基准尺寸的比例判定，阈值来自配置。

    以前是 `tuple(size) != (1920,1080)` 写死：换台 2K 屏就直接红，
    哪怕窗口化后游戏客户区只是等比例放大。
    """
    base = tuple(facts.get("want_size") or WANT_SIZE)
    tol = float(facts.get("size_tolerance") or 0.0)
    try:
        if not base or len(base) != 2 or not size:
            return False
        return all(abs(float(size[i]) / float(base[i]) - 1.0) <= tol
                   for i in range(2))
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _ck(cid, label, ok, detail, fix=None, warn=False):
    return {"id": cid, "label": label, "ok": bool(ok),
            "detail": detail, "fix": fix, "warn": bool(warn)}


def _age_cn(seconds):
    """把「最近一次校准距今多久」压成面板上一眼能读的短串。"""
    try:
        sec = max(0.0, float(seconds))
    except (TypeError, ValueError, OverflowError):
        return "刚"
    if sec < 1.0:
        return "刚刚"
    if sec < 90.0:
        return f"{sec:.0f}s"
    if sec < 3600.0:
        return f"{sec / 60.0:.0f}min"
    return f"{sec / 3600.0:.1f}h"


def _search_px(name, region):
    """`region` 是 facts 里的 `(w, h)` → 引擎外扩后的搜索窗 `(w, h, pad)`。"""
    pad = SEARCH_PAD.get(name, DEFAULT_PAD)
    return (region[0] * (1 + 2 * pad), region[1] * (1 + 2 * pad), pad)


def _heal_note(heal):
    """模板自愈的结果 → 检查项里的一句人话（解释这次为什么是绿的）。"""
    heal = heal if isinstance(heal, dict) else {}
    st = heal.get("status")
    if st in (None, "", "skipped"):
        return ""
    env = heal.get("env") or {}
    pct = env.get("scale_percent") or 100
    if st == "ok":
        score = next((t.get("score") for t in (heal.get("templates") or {}).values()
                      if t.get("score") is not None), None)
        return f"模板与当前画面匹配 {score:.2f}" if score else "模板与当前画面一致"
    if st == "fit":
        return f"已按当前 {pct}% 缩放自动适配（几何一致）"
    if st == "rescaled":
        return f"已按当前 {pct}% 缩放自动补偿模板尺寸"
    if st == "recaptured":
        return f"已按当前 {pct}% 缩放自动重采模板"
    return ""


def _template_scale_issue(facts, wf=None):
    """模板像素尺寸和当前搜索窗/屏幕是否「根本不可能匹配」。

    判据必须是**引擎真正在找的那块地方**：`expand(区域, pad)` 之后的搜索窗，
    再乘上 vision 会用的缩放比例。以前的写法拿未外扩的框选区域比原始模板，
    结果是「随包模板 + 内置区域」在 1920×1080 / 100% 下必然红（buy 模板
    315×67 对按钮本体 298×58），把用户赶去重采 —— 而其实引擎在外扩到
    387×75 的窗里匹配得好好的（真机 2026-11-08）。
    """
    sizes = facts.get("template_sizes") or {}
    regions = facts.get("region_px") or {}
    scales = facts.get("template_scales") or {}
    wf = wf if wf in ("countdown", "list", "detail") else "list"
    for name in _required_templates(wf):
        size = sizes.get(name)
        reg = regions.get(name)
        if not size or not reg:
            continue
        tw, th = size
        rw, rh = reg
        sw, sh, pad = _search_px(name, reg)
        cands = scales.get(name)
        if cands:
            # 有元数据：只认 vision 会试的那几个比例（和 `_scale_candidates`
            # 同一套），避免自检用「引擎根本不会试」的比例自我开脱。
            if any(tw * s <= sw and th * s <= sh for s in cands):
                continue
            lo = min(cands)
            return (f"{name} 模板 {tw}×{th} 比框选区域 {rw}×{rh} 还大"
                    f"（外扩 {pad:.0%} 后搜索窗 {sw:.0f}×{sh:.0f}，"
                    f"按 {lo:.2f}× 缩也放不下）—— 当前比例无法匹配")
        # 没有元数据的老模板：走保守口径（外扩后仍要放得下才算过）
        if tw > sw or th > sh:
            return (f"{name} 模板 {tw}×{th} 比框选区域 {rw}×{rh} 还大 —— "
                    "模板是在更大分辨率/缩放下截的，当前比例无法匹配")
    cap = facts.get("template_capture_screen")
    cur = facts.get("size")
    if cap and cur:
        try:
            scale = float(cur[0]) / float(cap[0])
        except (TypeError, ValueError, ZeroDivisionError):
            scale = 1.0
        if scale > 2.2 or scale < 0.45:
            return (f"模板采集屏 {cap[0]}×{cap[1]}，当前 {cur[0]}×{cur[1]}"
                    f"（{scale:.2f}×），跨度太大，重采样后匹配不可靠")
    return None


def evaluate(facts, mode="list", workflow=None):
    """facts → 检查项列表。

    mode: "list" 在售列表 / "detail" 详情页按钮（兼容旧调用）。
    workflow: countdown / list / detail —— **有它就按它判**，和引擎
    `_validate` 一模一样；没有才退回 mode 推导（审查 S7/P0-4）。

    三路口径（与 `Engine._validate` 对齐）：
      list      → 列表区域 + 购买区域 + 购买模板；确认可有可无（无弹窗版式）
      countdown → 倒计时区域 + 购买区域 + 确认区域 + 确认模板；不看购买模板
      detail    → 购买/确认 两个区域 + 两个模板
    """
    out = []
    wf = workflow if workflow in ("countdown", "list", "detail") else \
        ("list" if mode != "detail" else "detail")
    is_list = wf == "list"

    # ① 游戏窗口：抓得到屏幕 + 分辨率对得上
    size = facts.get("size")
    if not size:
        out.append(_ck("window", "游戏窗口", False,
                       facts.get("screen_error") or "抓不到屏幕",
                       {"hint": "检查游戏是否在运行、显示器是否被占用"}))
    elif not _size_ok(size, facts):
        base = tuple(facts.get("want_size") or WANT_SIZE)
        tol = float(facts.get("size_tolerance") or 0.0)
        out.append(_ck("window", "游戏窗口", False,
                       f"{size[0]}×{size[1]}（基准 {base[0]}×{base[1]}，"
                       f"允许偏差 {tol:.0%}）",
                       {"hint": f"当前显示器是 {size[0]}×{size[1]}。二选一："
                                f"① 游戏设为「窗口化无边框」并铺满这块显示器，"
                                f"再把 config.json 的 screen.base_size 改成 "
                                f"{size[0]}×{size[1]}（模板会自动按比例重采样，"
                                "不用重截）；② 把 screen.tolerance 调到能覆盖的比例。"
                                "别把 2K 屏硬调成 1920×1080 —— 游戏和桌面都会变形"}))
    elif facts.get("game_hwnd") is False:
        # 分辨率对得上不代表游戏开着 —— 桌面本来就是 1920×1080。以前这里
        # 报绿「游戏窗口 1920×1080」，用户以为环境没问题，点开始白跑一轮。
        # 不拦死（窗口类名万一对不上，拦了就是误拦），但必须说清楚。
        out.append(_ck("window", "游戏窗口", True,
                       f"{size[0]}×{size[1]} · 没找到游戏窗口（UnrealWindow）",
                       {"hint": "先启动游戏；在启动器/登录界面时也会找不到"},
                       warn=True))
    else:
        out.append(_ck("window", "游戏窗口", True, f"{size[0]}×{size[1]}"))

    # ② OCR：没有它读不出商品名和价格，等于瞎抢
    svc = facts.get("ocr_service") or {}
    if facts.get("ocr"):
        detail = "就绪" + (f" · {facts['ocr_name']}" if facts.get("ocr_name") else "")
        if svc.get("managed"):
            up = svc.get("uptime_s") or 0
            detail += f" · 服务已跑 {up / 3600:.1f}h"
            if svc.get("restarts"):
                detail += f"，重启 {svc['restarts']} 次"
        if svc.get("degraded"):
            # 二级服务缺席是稳定状态，不是故障：内置识别照常，价格复核走字模。
            detail += " · 二级加速已降级（内置识别照常）"
        out.append(_ck("ocr", "OCR 识别", True, detail))
    else:
        name = facts.get("ocr_name") or ""
        # 服务正在拉起（Paddle 加载模型 10~20 秒）时不能说「不可用」：
        # 冷启动那一瞬间检查必然不过，用户看到「不可用」就去手敲命令了，
        # 其实再等十几秒它自己就绪。这里单独给一句「正在启动」，前端见到
        # 会改成 3 秒重查一次，恢复后立刻变绿。
        starting = bool(svc.get("starting")) or bool(
            svc.get("enabled") and not svc.get("healthy")
            and not svc.get("last_error"))
        if starting:
            detail = "正在启动（加载模型约 10~20 秒，会自动重试）"
            fix = {"hint": "不用管；超过 1 分钟还这样就看 ocr_service_8081.log"}
        else:
            # backend_name() 本身就带「不可用（…）」前缀，别再包一层
            detail = name or "不可用"
            fix = {"hint": "缺 OCR 运行环境：仓外 .venv-vl 未安装或服务没起来。"
                            "先按 README 装（首次还要联网下载模型），再起："
                            ".venv-vl\\Scripts\\paddlex.exe "
                            "--serve --pipeline tools\\OCR_game.yaml "
                            "--port 8081 --device gpu:0"}
        out.append(_ck("ocr", "OCR 识别", False, detail, fix))

    # ③ 时间校准（自动）
    #
    # 宿主侧现在启动即自动校时、运行中按 time_sync.auto_interval_s 持续重校，
    # 失败自动退避重试，不拦开工。本项只呈现「已校准 / 校准中 / 仅系统时间」。
    # 后台失校时仅退回系统时间；不建窗口、不抢焦点、不拦开工。
    #
    #
    clock = facts.get("clock") or {}
    auto = clock.get("auto") if isinstance(clock.get("auto"), dict) else {}
    off = clock.get("offset")
    at = clock.get("at") if clock.get("at") is not None \
        else auto.get("last_ok_at")
    calibrated = bool(clock.get("calibrated", at is not None))

    if auto.get("running") and not calibrated:
        fails = int(auto.get("consecutive_failures") or 0)
        if fails <= 0:
            out.append(_ck("clock", "时间校准", True,
                           "自动校准中（启动时自动完成，完成后自动刷新，无需操作）",
                           None, warn=True))
        else:
            err = (auto.get("last_error") or "校准源不可用").strip()
            if len(err) > 60:
                err = err[:60] + "…"
            retry = auto.get("retry_interval_s")
            nxt = f"{float(retry):.0f}s 后自动重试" \
                if isinstance(retry, (int, float)) else "后台自动重试"
            out.append(_ck("clock", "时间校准", False,
                           f"仅系统时间（{err}；{nxt}，无需操作）",
                           None, warn=True))
    elif off is None or at is None:
        out.append(_ck("clock", "时间校准", False,
                       "还没校过时（程序会自动校准，不影响倒计时抢购）",
                       None, warn=True))
    elif clock.get("stale"):
        reason = clock.get("stale_reason") or "校时结果已过期"
        out.append(_ck("clock", "时间校准", False,
                       f"仅系统时间（{reason}，后台自动重校中）",
                       None, warn=True))
    elif abs(off) > CLOCK_TOLERANCE_S:
        out.append(_ck("clock", "时间校准", False,
                       f"已自动校准但偏差 {off * 1000:+.0f} ms（已补偿；"
                       "建议核对系统时间）",
                       None, warn=True))
    else:
        age = _age_cn(time.time() - float(at))
        out.append(_ck("clock", "时间校准", True,
                       f"已自动校准 · 偏差 {off * 1000:+.0f}ms（{age} 前，已补偿）"
                       f" · {int(auto.get('samples') or 0)} 样本"))

    # ④ 按钮模板 / 区域：靠它判断「购买按钮出现了」「确认弹窗出来了」
    # 口径必须和引擎 `_validate`、`scheduler.start`、`_drive_task` 一致，
    # 否则会出现「检查全绿 → 任务加上 → 点开始秒失败说缺模板」（审查 P0-4）。
    tpl = facts.get("templates") or {}
    reg = facts.get("regions") or {}
    if is_list:
        if not reg.get("list"):
            out.append(_ck("templates", "列表区域", False, "还没框选交易行列表",
                           {"hint": "列表区域在配置里标定；缺了要把游戏停在在售页重标一次"}))
        elif not reg.get("buy"):
            # 列表模式点进详情页后仍要用购买按钮定位，区域是硬要求
            out.append(_ck("templates", "列表区域", False, "还没框选购买按钮",
                           {"hint": "把游戏停在在售页，标一次购买按钮区域"}))
        elif not tpl.get("buy"):
            out.append(_ck("templates", "列表区域", False,
                           "缺 购买 模板（点进详情页靠它判断按钮可点）",
                           _CALIBRATE_FIX))
        else:
            # 「确认」模板不拦启动：这一版界面点价格按钮直接成交，没有弹窗
            # 可截。没框它的时候引擎按余额变化判成交（`_buy_without_dialog`）。
            note = ("确认按钮已框选" if (tpl.get("confirm") and reg.get("confirm"))
                    else "没框确认按钮 —— 按余额变化判断成交")
            out.append(_ck("templates", "列表区域", True, "已框选 · " + note))
    elif wf == "countdown":
        # 倒计时路线归零那刻直接点购买区域中心，不看按钮长相；但**必须**
        # 有倒计时区域 —— 以前这里从不看 `regions.clock`，倒计时任务没框
        # 倒计时也能全绿放行（审查 S7）。
        if not reg.get("clock"):
            out.append(_ck("templates", "倒计时区域", False, "还没框选倒计时",
                           {"hint": "把游戏停在商品页，标一次倒计时区域"}))
        elif not reg.get("buy"):
            out.append(_ck("templates", "倒计时区域", False, "还没框选购买按钮",
                           {"hint": "标一次购买按钮区域（归零那刻点它的中心）"}))
        elif not reg.get("confirm"):
            out.append(_ck("templates", "倒计时区域", False, "还没框选确认按钮区域",
                           {"hint": "标一次确认弹窗里的确认按钮区域"}))
        elif not tpl.get("confirm"):
            out.append(_ck("templates", "倒计时区域", False,
                           "缺 确认 模板（点完要靠它判断弹窗有没有出来）",
                           _CALIBRATE_FIX))
        else:
            out.append(_ck("templates", "倒计时区域", True, "倒计时 + 购买 + 确认 已就绪"))
    else:
        if not tpl.get("buy"):
            out.append(_ck("templates", "购买按钮", False, "缺 购买",
                           _CALIBRATE_FIX))
        elif not reg.get("buy"):
            out.append(_ck("templates", "购买按钮", False, "还没框选购买按钮区域",
                           {"hint": "把游戏停在商品页，标一次购买按钮区域"}))
        elif not tpl.get("confirm"):
            out.append(_ck("templates", "购买按钮", False, "缺 确认",
                           _CALIBRATE_FIX))
        elif not reg.get("confirm"):
            out.append(_ck("templates", "购买按钮", False, "还没框选确认按钮区域",
                           {"hint": "标一次确认弹窗里的确认按钮区域"}))
        else:
            out.append(_ck("templates", "购买按钮", True, "购买 + 确认"))

    # ④b 自动标定：买家不拖框，坐标是内置的；开抢前会自检一次。
    # 降级（fallback）必须显式告警，不能带着保守坐标静默开抢。
    cal = facts.get("calibration") or {}
    st = cal.get("status") or "never"
    block = cal.get("block") or ""
    if block or st in ("fallback", "error", "skipped"):
        # error（没跑起来/窗口不完整）、fallback（坐标已回落内置但没验证过）、
        # skipped（画面里没锚点）都拦：没核对过按钮位置就用内置坐标点，
        # 1280×800 偏 29px、3440×1440 偏 307px。超阈值的 adjusted 同样拦。
        # 唯一放行：必需区域都被用户手框过（manual_ready）。
        if block or not cal.get("manual_ready"):
            out.append(_ck("calibration", "自动标定", False,
                           block or cal.get("message") or "自动标定未通过",
                           {"hint": "打开「高级设置 → 区域微调」拖一次框"
                                    "（手框后即可启动），或确认游戏窗口完整"
                                    "在屏幕内、没有被遮挡"}))
        else:
            out.append(_ck("calibration", "自动标定", True,
                           "自动校验跳过，用的是你手框的坐标", warn=True))
    elif st == "adjusted":
        out.append(_ck("calibration", "自动标定", True,
                       cal.get("message") or "已按当前画面比例微调", warn=True))
    elif st == "ok":
        out.append(_ck("calibration", "自动标定", True,
                       cal.get("message") or "自动标定通过"))
    else:
        out.append(_ck("calibration", "自动标定", True,
                       "还没跑过；点开始抢购时会自动校验一次", warn=True))

    # ⑤ 程序权限：比游戏低的话，鼠标键盘消息全被 UIPI 拦掉
    integ = facts.get("integrity") or {}
    ours, game = integ.get("ours"), integ.get("game")
    if ours is None:
        # 读不到就当没问题 —— 拦错了比不拦更烦
        out.append(_ck("integrity", "程序权限", True, "读不到权限信息"))
    elif game is None:
        # 没找到游戏窗口：这一项无从判断，交给「游戏窗口」那一项去拦，
        # 这里不误报成「权限不够」。
        out.append(_ck("integrity", "程序权限", True,
                       f"{_INTEGRITY_CN.get(ours, hex(ours))}权限（没找到游戏窗口）",
                       warn=True))
    elif game > ours:
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
    mode = facts.get("input_mode") or drv.get("mode")
    if mode == "legacy" or drv.get("mode") == "legacy":
        # 用户显式选了 legacy：这是配置意图，不是故障。亮绿灯、说明慢在哪。
        out.append(_ck("driver", "输入驱动", True,
                       "已配置 PostMessage(legacy)（不用驱动，点击慢 ~100 倍）"))
    elif mode == "message" or drv.get("mode") == "message":
        # 纯消息通道：不动光标，但目标读真实光标时不进游戏逻辑 —— 如实提示。
        cap = drv.get("capability") or {}
        if drv.get("ok"):
            out.append(_ck("driver", "输入通道", True,
                           "纯消息投递(message)：窗口受理消息，全程不动光标"))
        else:
            out.append(_ck("driver", "输入通道", False,
                           f"纯消息通道不可用：{cap.get('detail') or drv.get('why') or '目标窗口不接收消息'}",
                           {"hint": "按 input_fallback 降级或直接失败（见输入通道账本）",
                            "reason_code": cap.get("reason_code")},
                           warn=True))
    elif drv.get("ok"):
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
               "market": "市场首页", "modeselect": "模式选择页", "unknown": "认不出"}
    if page is None:
        out.append(_ck("page", "游戏页面", False,
                       facts.get("page_error") or "认不出当前页面",
                       {"hint": "确认游戏窗口在前面，没被别的程序挡住"}))
    elif page == "unknown" and facts.get("game_hwnd") is False:
        # 连游戏窗口都没找到：这时候放行就是对着桌面盲点。
        out.append(_ck("page", "游戏页面", False, "没找到游戏窗口",
                       {"hint": "先启动游戏，进到交易行"}))
    elif page == "unknown" and facts.get("game_fore") is False:
        # 游戏在跑，但前台是别的窗口（绝大多数时候是**控制台自己**）。
        # 抓屏抓的是桌面，看到的是压在上面的控制台，认不出页面是必然的 ——
        # 这不是环境错，是「检查方式」的固有偏差。拦下来的后果很荒谬：
        # 用户点「开始」→ 控制台在前台 → 报认不出 → 切到游戏看一眼 →
        # 切回来再点 → 又被拦。
        # 引擎开工第一步就是 `ensure_foreground(游戏窗口)` 再 `_navigate`
        # （见 engine.py 里每次点击前的 ensure_foreground），真站错页它会
        # 自己导航，导航不了就记一条带原因的 miss —— 比在门口猜准得多。
        out.append(_ck("page", "游戏页面", False,
                       "看不出是哪个页面（游戏被别的窗口挡着）",
                       {"hint": "开抢时会自动把游戏提到前面；想现在就确认，切到游戏看一眼"},
                       warn=True))
    elif page == "unknown":
        # 游戏就在前台却认不出：可能在某个不认识的页面（对局里、加载中）
        out.append(_ck("page", "游戏页面", False, "认不出当前页面",
                       {"hint": "把游戏切到交易行，停在「在售」页"}))
    elif page == "modeselect":
        # 模式选择页（从桌面进游戏的第一屏）：引擎 recover() 会先点最左那张卡
        # 的「前往游玩」进大厅，再走 F4 进市场，所以不拦死，只提示。
        # 拦死的后果是用户每次开工前都得先手动点一次。
        out.append(_ck("page", "游戏页面", True,
                       "模式选择页 · 开抢时会自动点「前往游玩」进入烽火地带大厅",
                       {"hint": "想省掉这一步就先点「前往游玩」进大厅"},
                       warn=True))
    elif is_list and page != "onsale":
        # **不再拦死。** 引擎的 _navigate 会自己走
        # 「F4 → 典藏外观 → 选皮肤 → 购买 → 在售页」（go_to_onsale），
        # 停在市场首页时它能自己过去 —— 在这里拦下等于让用户白点一次。
        # 只有「认不出页面」才是真的不能开工（可能被别的窗口盖着、或者在
        # 对局里），那个分支上面已经 FAIL 了。
        out.append(_ck("page", "游戏页面", True,
                       f"{PAGE_CN.get(page, page)} · 开抢时会自动导航到在售页",
                       {"hint": "想省掉这一步就先手动切到「在售」页"},
                       warn=True))
    else:
        out.append(_ck("page", "游戏页面", True, PAGE_CN.get(page, page)))

    # ⑦ 模板像素尺寸 vs 当前搜索窗：尺度对齐救不了的组合提前拦下
    #
    # 模板自愈（`core/template_heal.py`）已经在 collect() 里先跑过一遍：
    # 几何能对齐就直接放行（附一句「已按当前 N% 缩放自动适配」），对齐不了
    # 才在这里标红，并把「已经试过什么」写进文案 —— 不再让用户去命令行。
    heal = facts.get("template_heal") or {}
    issue = _template_scale_issue(facts, wf)
    if issue:
        why = issue
        if heal.get("status") == "failed" and heal.get("message"):
            why = f"{issue}；自动处理失败：{heal['message']}"
        tried = heal.get("actions") or []
        fix = {"hint": "把游戏停在对应页面（购买按钮 / 确认弹窗）后点"
                        "「重新检查」或「一键自动标定」，程序会按当前画面"
                        "自动重新采集模板；仍不行再手工框一次（可选入口："
                        "SJZ-Control.exe --calibrate）。"}
        if tried:
            fix["tried"] = tried[:4]
        for c in out:
            if c["id"] == "templates" and c["ok"]:
                c.update(ok=False, detail=why, fix=fix)
                break
    else:
        note = _heal_note(heal)
        if note:
            for c in out:
                if c["id"] == "templates" and c["ok"]:
                    c["detail"] = f"{c['detail']} · {note}"
                    break

    return out


def all_ok(checks):
    # warn 项不拦启动：它们是「能跑但降级」（如驱动没装退 legacy），
    # 拦下来的话本来能抢的场会被一项慢通道警告卡死。ok=False+warn=True
    # 只在界面上亮警示。
    return all(c["ok"] or c.get("warn") for c in checks)


def collect(cfg, mode="list", workflow=None):
    """真的去查一遍。慢的只有「认页面」那一步（抓屏 + OCR，约 1 秒）。"""
    from . import ocr, timesync
    from .input import Mouse, process_integrity
    from .nav import Navigator
    from .screen import Screen

    facts: dict = {"templates": {n: bool(cfg.has_template(n)) for n in ("buy", "confirm")},
                   "regions": {n: bool(cfg.region(n)) for n in ("buy", "confirm", "list", "clock")},
                   "calibration": dict(cfg.get("calibration") or {}),
                   # 模板像素尺寸 / 采集屏 / 引擎会试的缩放比例（尺度对齐
                   # 是否够用，见 `_template_scale_issue`）
                   "template_sizes": {}, "template_capture_screen": None,
                   "template_scales": {},
                   "region_px": {},
                   # 分辨率基准与容差都来自配置，不在代码里写死 1920×1080：
                   # 换机器/换 DPI 只需要改配置。
                   "want_size": tuple(cfg.get("screen.base_size") or WANT_SIZE),
                   "size_tolerance": float(cfg.get("screen.tolerance") or 0.0)}
    # 先问服务管家、再问客户端：status() 现在不阻塞，但顺序反过来的话，
    # 「服务刚验活成功」和「客户端探活」之间会差一次往返，冷启动那一刻
    # 容易采到「服务已就绪、客户端还没重连」的假阴性。
    try:
        from . import ocr_service
        svc = ocr_service.current()
        facts["ocr_service"] = svc.status() if svc is not None else None
    except Exception:
        facts["ocr_service"] = None
    try:
        facts["ocr"] = bool(ocr.available())
        facts["ocr_name"] = ocr.backend_name()
    except Exception:
        facts["ocr"], facts["ocr_name"] = False, ""
    try:
        # 启动后首轮自动校时通常 1~3 秒；这里最多等 3 秒，让检查面板
        # 一打开就能看到「已自动校准」，而不是把待办塞回给用户。
        # 网络不通时首轮会慢，panel 先显示「自动校准中」，后台完成后
        # 状态接口（/api/scheduler 的 clock）会带出新结果。
        timesync.auto_wait_first(3.0)
        facts["clock"] = dict(timesync.info())
    except Exception:
        facts["clock"] = {}

    # 权限：游戏跑得比我们高（UIPI）的话，鼠标键盘消息全被系统拦掉，
    # 而且**一声不吭** —— 症状是「点了没反应」，看起来像代码坏了。
    # 查的是**游戏窗口**的权限，不是「所有可见窗口里最高的那个」：
    # 后者会让任务管理器/安装器一开着就误报（提权重开还判不过，见
    # core/input.py 里 needs_elevation 的说明）。游戏被别的窗口盖住时
    # 按窗口类名照样找得到它，不需要靠前台。
    try:
        from .input import game_integrity
        facts["integrity"] = {"ours": process_integrity(),
                              "game": game_integrity()}
    except Exception:
        facts["integrity"] = {}

    # 输入驱动（interception）：没就绪不拦启动（自动退 legacy 能跑），
    # 但检查项里要亮出来 —— 别等开抢了才发现点击慢百倍。
    # 局部变量必须叫 input_mode：形参 mode 是入口签名（server.py 按位置
    # 传 list/detail/countdown），以前同名覆盖让「路线」和「输入通道」在
    # 同一个函数里共用一个名字，读代码的人分不清这两条线（签名不改）。
    input_mode = cfg.get("input_mode") or "interception"
    facts["input_mode"] = input_mode
    if input_mode == "interception":
        try:
            from .interception import driver_ready
            ok, why = driver_ready()
            facts["driver"] = {"ok": bool(ok), "why": why or None,
                               "mode": "interception"}
        except Exception as e:
            facts["driver"] = {"ok": False, "why": f"{type(e).__name__}: {e}",
                               "mode": "interception"}
    elif input_mode == "message":
        # 纯消息通道：先做结构性预检（不投鼠标消息、不动光标）。
        try:
            from .input import find_game_window, message_capability
            cap = message_capability(find_game_window())
            facts["driver"] = {
                "ok": bool(cap.get("usable")
                           and cap.get("functional") != "unsupported"),
                "why": cap.get("detail") or None,
                "mode": "message",
                "capability": cap,
            }
        except Exception as e:
            facts["driver"] = {"ok": False, "why": f"{type(e).__name__}: {e}",
                               "mode": "message"}
    else:
        # 显式配了 legacy：这不是「驱动没就绪」，是用户自己选的通道，
        # 不能在检查项里亮红灯说「未就绪，自动退回」（审查 I23）。
        facts["driver"] = {"ok": False, "why": None, "mode": "legacy"}

    s = None
    try:
        s = Screen(cfg["monitor"])
        facts["size"] = tuple(s.size)
    except Exception as e:
        facts["size"] = None
        facts["screen_error"] = f"抓不到屏幕：{type(e).__name__}: {e}"

    if s is not None and facts.get("ocr"):
        # 先记下「游戏窗口在不在、在不在前台」：认页面靠抓屏，抓屏抓的是
        # 桌面 —— 控制台压在游戏上时，看到的永远是控制台，页面必然认不出。
        # 有了这两条，「被挡着」和「真的站在不认识的页面上」才分得开。
        try:
            from .input import find_game_window
            hwnd = find_game_window()
            facts["game_hwnd"] = bool(hwnd)
            facts["game_fore"] = bool(hwnd) and Mouse.foreground() == hwnd
        except Exception:                        # noqa: BLE001
            facts["game_hwnd"], facts["game_fore"] = None, None
        try:
            # **认页面不造 Mouse。** Mouse(s) 会在构造时判定输入模式
            # （driver_ready() 会去建一个 Interception context），一次检查
            # 就顺手把驱动挂上、还要在收尾时销毁 —— 检查阶段只是只读抓屏，
            # 不该碰输入通道（审查 I24）。Navigator.where() 只用 _snap()，
            # mouse=None 足够：where() 只读截图，不发任何输入。
            facts["page"] = Navigator(s, None).where()[0]
        except Exception as e:
            facts["page"] = None
            facts["page_error"] = f"认不出页面：{type(e).__name__}: {e}"
    # 模板自愈：比原计划先跑 —— 这里是唯一还握着 Screen、且刚认过页面的
    # 位置，自愈要按「当前画面」判几何/复验/重采。它只抓屏，不碰鼠标键盘；
    # 写盘前必快照，任何异常都收敛成一条 failed 事实，绝不掀掉整个检查。
    if s is not None:
        try:
            from . import template_heal
            facts["template_heal"] = template_heal.heal(
                cfg, page=facts.get("page"), workflow=workflow, screen=s,
                reason="preflight")
        except Exception as e:                   # noqa: BLE001
            facts["template_heal"] = {
                "status": "failed", "ok": False, "actions": [],
                "templates": {}, "passed": [],
                "message": f"模板自愈没有跑起来：{type(e).__name__}: {e}"}

    if s is not None:
        # 模板的真实像素尺寸 + 采集环境 + 引擎会试的缩放比例：读在图已
        # 落地之后（自愈可能刚改过某张模板），否则自检看到的是旧尺寸。
        # 任何一项读不到就不填，别让自检因为读图失败而误报。
        try:
            from . import vision
            import cv2
            for name in ("buy", "confirm"):
                if not cfg.has_template(name):
                    continue
                img = vision.imread(cfg.template_path(name), cv2.IMREAD_GRAYSCALE)
                if img is not None:
                    facts["template_sizes"][name] = (int(img.shape[1]),
                                                     int(img.shape[0]))
                    facts["template_scales"][name] = [
                        float(s) for s in vision.scale_candidates(img)]
                    if facts["template_capture_screen"] is None:
                        facts["template_capture_screen"] = (
                            vision.capture_screen_of(img))
        except Exception:
            pass
        try:
            for name in ("buy", "confirm"):
                box = cfg.region(name)
                if box:
                    _x, _y, w, h = s.norm_to_px(box)
                    facts["region_px"][name] = (w, h)
        except Exception:
            pass
    try:
        if s is not None:
            s.close()
    except Exception:
        pass
    return facts
