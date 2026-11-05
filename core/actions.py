"""动作层：真正去操作游戏的那几件事。

从 server.py 抽出来的原因：
  · handler 只该做「校验参数 → 调一个动作 → 回 JSON」，不该知道
    Screen/Mouse/Navigator 怎么建、前台怎么抢；
  · 这几个动作要建设备对象、抢前台、滚列表，属于**设备编排**。以前
    写在 handler 里，想从 CLI 或热键复用只能把那 20 行初始化复制一遍；
  · 它们慢（秒级到几十秒），必须能扔到后台跑（见 core/jobs.py）。

约定：这里不碰 HTTP，不返回 (code, body)，只返回 dict 或抛异常。
错误信息写成人能看懂的中文，上层直接展示。
"""
import time

from . import bricks
from .config import Config


class ActionError(Exception):
    """动作失败，消息直接给用户看。"""


def _log_fn(log):
    """把外部传进来的 logger 归一成「一个字符串参数」的函数。"""
    if log is None:
        return lambda _m: None
    return log


def _open_devices(cfg, log=None):
    """建 Screen / Mouse / Navigator，并把游戏抢到前台。

    返回 (screen, mouse, nav)。调用方负责 screen.close()。

    **必须带游戏窗口句柄**：不带的话 Mouse.click 把消息发给「当前前台
    窗口」，用户点了别的东西就点错地方（实测终端压着游戏时，点全落在
    终端上，界面上一点反应没有）。
    """
    from .input import Keyboard, Mouse, find_game_window
    from .nav import NavError, Navigator
    from .screen import Screen

    say = _log_fn(log)
    screen = Screen(cfg.get("monitor") or 1)
    try:
        # **找不到目标窗口就失败，不许拿前台窗口顶替。** 前台可能是控制台
        # 自己、浏览器、IDE —— 拿它当游戏，点击/POST 全都落在错的窗口上，
        # 表现是「程序一直在点，游戏毫无反应」，而且日志看不出原因。
        hwnd = find_game_window()
        if not hwnd:
            raise NavError("找不到游戏窗口（UnrealWindow）—— 先把游戏开起来")
        mode = cfg.get("input_mode") or "interception"
        mouse = Mouse(screen, game_hwnd=hwnd, mode=mode)
        # **必须先把游戏提到前面**：控制台窗口压在游戏上时，游戏按真实
        # 光标位置判断点到了哪，而光标在控制台窗口里 —— 点「筛选」「确认」
        # 全落空。
        if not mouse.ensure_foreground(hwnd, screen=screen):
            raise NavError("抢不到游戏前台 —— 先把游戏切到前台")
        nav = Navigator(screen, mouse, Keyboard(hwnd, mode=mouse.mode),
                        snap=screen.grab, log=say)
    except Exception:
        screen.close()
        raise
    return screen, mouse, nav


def locate_skin(name, tier=None, season=None, log=None):
    """在游戏里定位并选中某件砖（设筛选 → 滚列表 → 点中）。

    name 是皮肤名；tier/season 可选，用来缩小搜索范围。
    返回 dict：{name, y, title, score, plan, screens, tried, elapsed_s,
                season, series, tier, weapon}
    """
    name = (name or "").strip()
    if not name:
        raise ActionError("没说要找哪一件")
    hit = bricks.find_skin(name, tier=tier or None, season=season or None)
    if not hit:
        raise ActionError(f"目录里没有「{name}」")

    from .skinfind import SkinFinder

    cfg = Config.load()
    say = _log_fn(log)
    t0 = time.time()
    screen, _mouse, nav = _open_devices(cfg, log=say)
    try:
        finder = SkinFinder(nav, say=lambda m, lv="info": say(m))
        # 游戏「赛季下拉」里列的是**砖名**（棱镜攻势/美杜莎…），不是子系列
        # （水墨迷彩/青纹…）—— 定位要按砖名筛。用找皮肤的完整逻辑：
        # 筛不到就逐级放宽，最后全库扫。
        res = finder.find(hit["name"],
                          season=hit.get("brick") or hit.get("series") or None,
                          tier=hit.get("tier") or None,
                          gun=hit.get("gun") or None)
        if not res.get("ok"):
            raise ActionError(res.get("msg") or "没找到")
        return {"name": hit["name"], "y": res.get("y"), "title": res.get("title"),
                "score": res.get("score"), "plan": res.get("plan"),
                "screens": res.get("screens"), "tried": res.get("tried"),
                "elapsed_s": res.get("elapsed_s") or round(time.time() - t0, 2),
                "season": hit.get("season", ""), "series": hit.get("series", ""),
                "tier": hit.get("tier", ""), "weapon": hit.get("weapon", "")}
    finally:
        screen.close()


def set_follow(want=True, log=None):
    """把交易行里当前选中的这件挂单关注 / 取消关注。

    星标在右侧详情面板顶部（标题右边、放大镜左边）。判据是颜色：
    金色 = 已关注，灰白 = 未关注 —— 14px 的小星星 OCR 认不出，颜色
    一眼能分（实测金色 R-B ≈ 135，灰白 ≈ 0）。
    """
    cfg = Config.load()
    say = _log_fn(log)
    screen, _mouse, nav = _open_devices(cfg, log=say)
    try:
        before = nav.follow_state()
        after = nav.set_follow(bool(want))
        return {"ok": after == bool(want), "before": before, "after": after}
    finally:
        screen.close()
