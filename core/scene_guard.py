"""场景守卫：看清现在是什么情况、危险多大、该回到哪个目标状态。

这一层只做三件事，不做具体操作：
  · 把可观测信号翻译成「场景」（不靠猜，全部是运行时事实）；
  · 维护五级处置阶梯和优先级 / 合并规则，保证同一时刻只有一套恢复动作在跑；
  · 暴露前端要的状态：第几级、一句话、唯一主按钮、现在在做什么。

具体怎么恢复由 `core/recovery.py` 的能力库 + 规划器负责，本模块只管
「该不该恢复、恢复成什么样算成功、什么时候找人、什么时候硬停」。

面向用户的文案（guide_message / halt_message / button_label）里不出现任何
专业词；代码里的字段名才用技术词。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Mapping

# ---------------- 级别 -----------------------

LEVEL_NORMAL = 0
LEVEL_RETRY = 1
LEVEL_FIX = 2
LEVEL_RELOCATE = 3
LEVEL_GUIDE = 4
LEVEL_HALT = 5

LEVEL_TEXT = {
    LEVEL_NORMAL: "正常",
    LEVEL_RETRY: "再试一次",
    LEVEL_FIX: "换个方式",
    LEVEL_RELOCATE: "回到安全页面",
    LEVEL_GUIDE: "需要你",
    LEVEL_HALT: "已停止",
}

STATE_OK = "ok"                 # 正常盯盘
STATE_RECOVERING = "recovering" # 自动恢复中
STATE_WAITING_HUMAN = "waiting_human"  # 停准位置，等人工过一下（通过后自动续跑）
STATE_HALTED = "halted"         # 已硬停 / 已自动止损

# ---------------- 场景 id ----------------

SC_CLICK_NO_EFFECT = "click_no_effect"
SC_UNEXPECTED_PAGE = "unexpected_page"
SC_CONFIRM_UNEXPECTED = "confirm_unexpected"
SC_MISBUY = "misbuy"
SC_LIST_STALL = "list_stall"
SC_LAG = "lag"
SC_WINDOW_LOST = "window_lost"
SC_GAME_DOWN = "game_down"
SC_UI_CHANGED = "ui_changed"
SC_RESOLUTION_CHANGED = "resolution_changed"
SC_OCR_FAIL = "ocr_fail"
SC_CLOCK_DRIFT = "clock_drift"
SC_INSUFFICIENT = "insufficient"
SC_ANTICHEAT = "anticheat"
SC_USER_TAKEOVER = "user_takeover"
SC_SELF_FAULT = "self_fault"
SC_CONFIG_FAULT = "config_fault"


@dataclass
class GuardConfig:
    """每一级的次数、超时、间隔都可配；默认值写在这里，不写进用户配置。"""

    l1_fail_n: int = 3
    l2_fail_n: int = 2
    l3_fail_n: int = 2
    l1_budget_s: float = 8.0
    l2_budget_s: float = 15.0
    l3_budget_s: float = 30.0
    max_total_s: float = 60.0
    max_actions: int = 20
    max_plans: int = 8
    max_depth: int = 3
    min_interval_s: float = 0.25
    clear_ticks: int = 3
    clear_window_s: float = 2.0
    evidence_ttl_s: float = 3.0
    manual_timeout_s: float = 600.0
    hard_timeout_s: float = 1800.0
    user_idle_s: float = 2.0
    frame_lag_ms: float = 250.0
    clock_drift_ms: float = 150.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "GuardConfig":
        """从配置里可选的 guard 段读覆盖值；缺项一律用默认，不写盘。"""
        out = cls()
        if not isinstance(data, Mapping):
            return out
        for f in fields(cls):
            if f.name not in data:
                continue
            raw = data.get(f.name)
            try:
                if isinstance(getattr(out, f.name), int):
                    setattr(out, f.name, int(raw))
                else:
                    setattr(out, f.name, float(raw))
            except (TypeError, ValueError):
                continue
        return out


@dataclass
class Signals:
    """一次遥测快照。全部是运行时能拿到的事实；缺项用默认值，不猜。"""

    now: float = 0.0
    page: str = "unknown"
    expected_pages: tuple[str, ...] = ()
    anchor_hits: int = 9
    anchor_total: int = 9
    anchor_pages_sampled: int = 0

    # 点击 / 画面
    click_age_s: float | None = None
    click_effect_age_s: float | None = None
    frame_changed: bool | None = None
    frame_dt_ms: float = 0.0
    frame_dt_p95_ms: float = 0.0

    # 确认框 / 购买结果
    confirm_found: bool = False
    confirm_price: float | None = None
    confirm_item: str | None = None
    expected_item: str | None = None
    budget: float = 0.0
    balance_before: float | None = None
    balance_after: float | None = None
    expected_price: float | None = None
    bought_delta: int = 0

    # 列表
    refresh_rounds: int = 0
    hits: int = 0
    stall_s: float = 0.0
    list_fresh: bool = True

    # 时间 / 识别
    countdown_conf: float = 1.0
    countdown_jitter_ms: float = 0.0
    ocr_fail_streak: int = 0
    ocr_value_jump: float = 0.0
    clock_offset_ms: float = 0.0
    prev_clock_offset_ms: float = 0.0

    # 窗口 / 游戏
    hwnd: int | None = 1
    hwnd_alive: bool = True
    minimized: bool = False
    foreground: bool = True
    game_alive: bool = True
    login_visible: bool = False

    # 分辨率 / 标定
    screen_size: tuple[int, int] = (1920, 1080)
    baseline_size: tuple[int, int] = (1920, 1080)
    scale: float = 1.0
    baseline_scale: float = 1.0
    recalibrated: bool = False

    # 账户
    balance: float | None = None
    price: float | None = None
    quota_left: int = 1

    # 安全 / 外部
    anticheat: bool = False
    user_input_age_s: float | None = None
    own_input_age_s: float | None = None
    in_fire: bool = False

    # 程序自身
    driver_ok: bool = True
    worker_alive: bool = True
    config_ok: bool = True

    # 自动止损完成标志
    clicks_frozen: bool = False
    cursor_restored: bool = False
    hooks_unloaded: bool = False
    record_written: bool = False
    purchase_blocked: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "Signals":
        src = data or {}
        out = cls()
        for f in fields(cls):
            if f.name in src and src[f.name] is not None:
                setattr(out, f.name, src[f.name])
        return out


# ---------------- 场景定义 ----------------

@dataclass(frozen=True)
class SceneSpec:
    id: str
    name: str
    priority: int
    goal_text: str
    detect: Callable[[Signals], bool] | None
    min_confirm: int = 2
    # 0 = 走正常阶梯；4 = 直接进人工等待；5 = 直接自动止损并硬停
    hard_level: int = 0
    max_level: int = LEVEL_GUIDE
    total_s: float = 60.0
    guide_message: str = ""
    halt_message: str = ""
    button_id: str = "resume"
    button_label: str = "继续"
    non_recoverable: bool = False
    # 自动手段用完后：True = 直接安全硬停；False = 进第 4 级等人工
    halt_on_exhausted: bool = False


def _d_click(s: Signals) -> bool:
    return (s.click_effect_age_s is not None
            and s.click_effect_age_s >= 0.35
            and s.frame_changed is False)


def _d_unexpected_page(s: Signals) -> bool:
    if s.page == "unknown":
        return True
    if s.expected_pages and s.page not in s.expected_pages:
        # 大厅/市场永远算已知安全页
        return s.page not in ("hall", "market")
    return False


def _d_confirm(s: Signals) -> bool:
    if not s.confirm_found:
        return False
    if s.confirm_price is not None and s.budget and s.confirm_price > s.budget:
        return True
    if s.confirm_item and s.expected_item and s.confirm_item != s.expected_item:
        return True
    return False


def _d_misbuy(s: Signals) -> bool:
    if s.bought_delta > 1:
        return True
    if (s.balance_before is not None and s.balance_after is not None
            and s.expected_price):
        spent = s.balance_before - s.balance_after
        if spent > 0 and spent > s.expected_price * 1.2 + 0.01:
            return True
    return False


def _d_list_stall(s: Signals) -> bool:
    return s.hits == 0 and (s.refresh_rounds >= 8 or s.stall_s >= 600)


def _d_lag(s: Signals) -> bool:
    if s.frame_dt_p95_ms >= 250:
        return True
    return s.in_fire and s.countdown_conf < 0.6 and s.frame_dt_ms >= 120


def _d_window(s: Signals) -> bool:
    if not s.game_alive:
        return False  # 交给 game_down（优先级更高）
    return not s.hwnd or not s.hwnd_alive or s.minimized or not s.foreground


def _d_game_down(s: Signals) -> bool:
    return not s.game_alive or s.login_visible


def _d_ui_changed(s: Signals) -> bool:
    return s.anchor_hits == 0 and s.anchor_pages_sampled >= 2


def _d_resolution(s: Signals) -> bool:
    return (tuple(s.screen_size) != tuple(s.baseline_size)
            or abs(float(s.scale) - float(s.baseline_scale)) > 0.01)


def _d_ocr(s: Signals) -> bool:
    return s.ocr_fail_streak >= 5 or s.ocr_value_jump >= 0.3


def _d_clock(s: Signals) -> bool:
    return (abs(s.clock_offset_ms) > 150
            or abs(s.clock_offset_ms - s.prev_clock_offset_ms) > 300)


def _d_insufficient(s: Signals) -> bool:
    if s.quota_left <= 0:
        return True
    return (s.balance is not None and s.price is not None
            and s.balance < s.price)


def _d_anticheat(s: Signals) -> bool:
    return bool(s.anticheat)


def _d_takeover(s: Signals) -> bool:
    return s.user_input_age_s is not None and s.user_input_age_s <= 0.5


def _d_self_fault(s: Signals) -> bool:
    return (not s.driver_ok) or (not s.worker_alive)


def _d_config_fault(s: Signals) -> bool:
    return not s.config_ok


SCENES: dict[str, SceneSpec] = {
    SC_CLICK_NO_EFFECT: SceneSpec(
        SC_CLICK_NO_EFFECT, "点了没生效", 30,
        "画面有变化，并且还停在交易列表", _d_click, min_confirm=2,
        guide_message="点了几下，游戏画面没变化。程序已经停下，不会乱点。"
                      "看看画面正常后点「继续」。"),
    SC_UNEXPECTED_PAGE: SceneSpec(
        SC_UNEXPECTED_PAGE, "停在不认识的画面", 40,
        "回到交易行首页或允许的列表/详情页", _d_unexpected_page, min_confirm=2,
        guide_message="现在停在一个程序不认识的画面。已经停下不乱点了。"
                      "把游戏切回交易行，点「继续」。"),
    SC_CONFIRM_UNEXPECTED: SceneSpec(
        SC_CONFIRM_UNEXPECTED, "购买确认框不对", 45,
        "弹窗关掉、回到在售列表、没有扣钱", _d_confirm, min_confirm=1,
        guide_message="弹出了一个购买确认，程序已经点了取消，不会买这一件。"
                      "回到列表了，确认后点「继续」。"),
    SC_MISBUY: SceneSpec(
        SC_MISBUY, "已经误买", 95,
        "完全停手、光标还原、记录写完",
        _d_misbuy, min_confirm=1, hard_level=LEVEL_HALT,
        halt_message="已经买下了，但价格或数量和设置不一样。程序已停手、"
                     "记下这一笔，不需要你操作，只请你到游戏里核对。",
        button_id="quit", button_label="知道了，关闭程序"),
    SC_LIST_STALL: SceneSpec(
        SC_LIST_STALL, "列表一直买不到", 20,
        "回到在售列表且拿到新的一页", _d_list_stall, min_confirm=2,
        total_s=180.0,
        guide_message="交易行里一直没有出现你要的东西。程序还在等。"
                      "点「继续等」会再看 10 分钟。",
        button_id="wait_more", button_label="继续等"),
    SC_LAG: SceneSpec(
        SC_LAG, "电脑卡顿", 50,
        "画面流畅，倒计时可信", _d_lag, min_confirm=2,
        guide_message="电脑现在有点卡，程序先停一下，不会按不准的时间乱点。"
                      "画面顺畅后点「继续」。"),
    SC_WINDOW_LOST: SceneSpec(
        SC_WINDOW_LOST, "游戏窗口不见了", 70,
        "窗口可见、在前面、页面认得出来", _d_window, min_confirm=2,
        guide_message="游戏窗口不见了或被切走了，程序已经停下。"
                      "把游戏窗口切回来，点「继续」。"),
    SC_GAME_DOWN: SceneSpec(
        SC_GAME_DOWN, "游戏断开", 75,
        "游戏回到大厅/交易行；或登录框已打开",
        _d_game_down, min_confirm=1, total_s=180.0, non_recoverable=True,
        guide_message="游戏断开了，需要重新登录。登录框已经打开，你登录"
                      "回到大厅后，程序会自己接着跑。"),
    SC_UI_CHANGED: SceneSpec(
        SC_UI_CHANGED, "游戏界面变了", 65,
        "至少 2 个已知标志重新对上，否则停在安全页",
        _d_ui_changed, min_confirm=1, total_s=120.0, non_recoverable=True,
        guide_message="游戏界面变了，程序认不出来。已经停在安全画面，不会乱点。"
                      "请重新设置一次界面位置，点「继续」。"),
    SC_RESOLUTION_CHANGED: SceneSpec(
        SC_RESOLUTION_CHANGED, "画面大小变了", 60,
        "重新对上位置，或窗口回到原来大小",
        _d_resolution, min_confirm=2, total_s=90.0,
        guide_message="游戏画面大小变了，原来的位置对不上了。程序已经停下。"
                      "把窗口调回原来的大小，点「继续」。"),
    SC_OCR_FAIL: SceneSpec(
        SC_OCR_FAIL, "屏幕上的字看不清", 35,
        "连续读到有效内容，页面认得出来", _d_ocr, min_confirm=3,
        guide_message="屏幕上的字看不清，程序已经停下，不会乱按。"
                      "把游戏画面调清晰、别被挡住，点「继续」。"),
    SC_CLOCK_DRIFT: SceneSpec(
        SC_CLOCK_DRIFT, "电脑时间差太多", 55,
        "时间偏差回到正常范围", _d_clock, min_confirm=2,
        guide_message="电脑时间和实际差得太多，程序不敢按秒点。"
                      "已经重新对过时间，确认后点「继续」。"),
    SC_INSUFFICIENT: SceneSpec(
        SC_INSUFFICIENT, "金币不够", 80,
        "弹窗关掉、回到交易行、没有再买", _d_insufficient, min_confirm=1,
        hard_level=LEVEL_GUIDE,
        guide_message="金币不够买这一件，程序已经取消，不会继续买。"
                      "补足后点「继续」。"),
    SC_ANTICHEAT: SceneSpec(
        SC_ANTICHEAT, "游戏安全提示", 100,
        "完全停手、记录写完", _d_anticheat, min_confirm=1,
        hard_level=LEVEL_HALT,
        halt_message="游戏弹出了安全提示，程序已经全部停止，不会再做任何操作。"
                     "请手动处理游戏里的提示。",
        button_id="quit", button_label="知道了，关闭程序"),
    SC_USER_TAKEOVER: SceneSpec(
        SC_USER_TAKEOVER, "你正在操作", 85,
        "连续 2 秒没有用户输入，且页面安全", _d_takeover, min_confirm=1,
        hard_level=LEVEL_GUIDE,
        guide_message="你正在操作，程序让给你了。你停下来后它会自己接着跑。"),
    SC_SELF_FAULT: SceneSpec(
        SC_SELF_FAULT, "程序连接断了", 90,
        "连接恢复到可用", _d_self_fault, min_confirm=2,
        guide_message="程序自己的连接断了，正在自动重连。不行会停下，"
                      "点「重新连接」。",
        button_id="reconnect", button_label="重新连接"),
    SC_CONFIG_FAULT: SceneSpec(
        SC_CONFIG_FAULT, "设置文件读不出来", 90,
        "设置恢复可用；无法恢复则安全停止", _d_config_fault, min_confirm=1,
        halt_on_exhausted=True,
        halt_message="设置文件读不出来。为了不乱点，程序已经停止。"
                     "恢复设置文件后重新打开。",
        button_id="quit", button_label="关闭程序"),
}

# 对外公布的 16 个场景（config_fault 作为 self_fault 的子类一起报）
MATRIX_SCENES = (
    SC_CLICK_NO_EFFECT, SC_UNEXPECTED_PAGE, SC_CONFIRM_UNEXPECTED, SC_MISBUY,
    SC_LIST_STALL, SC_LAG, SC_WINDOW_LOST, SC_GAME_DOWN, SC_UI_CHANGED,
    SC_RESOLUTION_CHANGED, SC_OCR_FAIL, SC_CLOCK_DRIFT, SC_INSUFFICIENT,
    SC_ANTICHEAT, SC_USER_TAKEOVER, SC_SELF_FAULT,
)

# 因果抑制：高优先级场景生效时，这些低优先级场景先不启动动作。
_SUPPRESS = {
    SC_LAG: {SC_OCR_FAIL, SC_CLICK_NO_EFFECT},
    SC_UI_CHANGED: {SC_UNEXPECTED_PAGE},
    SC_WINDOW_LOST: {SC_CLICK_NO_EFFECT, SC_UNEXPECTED_PAGE},
    SC_GAME_DOWN: {SC_WINDOW_LOST, SC_CLICK_NO_EFFECT, SC_UNEXPECTED_PAGE},
    SC_USER_TAKEOVER: {SC_CLICK_NO_EFFECT, SC_UNEXPECTED_PAGE,
                       SC_OCR_FAIL, SC_LIST_STALL},
    SC_INSUFFICIENT: {SC_CONFIRM_UNEXPECTED},
    SC_ANTICHEAT: set(MATRIX_SCENES) | {SC_CONFIG_FAULT},
    SC_MISBUY: set(MATRIX_SCENES) | {SC_CONFIG_FAULT},
    SC_CONFIG_FAULT: set(MATRIX_SCENES),
}

_DOING = {
    LEVEL_NORMAL: "正在正常盯着画面",
    LEVEL_RETRY: "正在再试一次",
    LEVEL_FIX: "正在换个方式试",
    LEVEL_RELOCATE: "正在自己回到安全页面",
    LEVEL_GUIDE: "正在等你处理",
    LEVEL_HALT: "已经安全停止",
}


def goal_met(scene_id: str, s: Signals) -> bool:
    """每个场景的「恢复成功」判据。只认目标状态，不认动作做没做完。"""
    if scene_id == SC_CLICK_NO_EFFECT:
        return bool(s.frame_changed) and s.page != "unknown"
    if scene_id == SC_UNEXPECTED_PAGE:
        allowed = set(s.expected_pages) | {"hall", "market", "onsale", "detail"}
        return s.page in allowed and s.page != "unknown"
    if scene_id == SC_CONFIRM_UNEXPECTED:
        return (not s.confirm_found) and s.page in ("onsale", "market", "detail")
    if scene_id == SC_MISBUY:
        return (s.clicks_frozen and s.cursor_restored
                and s.hooks_unloaded and s.record_written)
    if scene_id == SC_LIST_STALL:
        return s.page in ("onsale", "market") and s.list_fresh
    if scene_id == SC_LAG:
        return s.frame_dt_p95_ms < 250 and s.countdown_conf >= 0.8
    if scene_id == SC_WINDOW_LOST:
        return (s.hwnd_alive and not s.minimized and s.foreground
                and s.page != "unknown")
    if scene_id == SC_GAME_DOWN:
        return s.game_alive and s.page in ("hall", "market", "onsale", "detail")
    if scene_id == SC_UI_CHANGED:
        return s.anchor_hits >= 2
    if scene_id == SC_RESOLUTION_CHANGED:
        same = (tuple(s.screen_size) == tuple(s.baseline_size)
                and abs(float(s.scale) - float(s.baseline_scale)) <= 0.01)
        return same or s.recalibrated
    if scene_id == SC_OCR_FAIL:
        return s.ocr_fail_streak == 0 and s.page != "unknown"
    if scene_id == SC_CLOCK_DRIFT:
        return abs(s.clock_offset_ms) <= 150
    if scene_id == SC_INSUFFICIENT:
        return (not s.confirm_found) and s.purchase_blocked
    if scene_id == SC_ANTICHEAT:
        return (s.clicks_frozen and s.hooks_unloaded and s.record_written)
    if scene_id == SC_USER_TAKEOVER:
        idle = s.user_input_age_s is None or s.user_input_age_s >= 2.0
        return idle and s.page in ("hall", "market", "onsale", "detail")
    if scene_id == SC_SELF_FAULT:
        return s.driver_ok and s.worker_alive
    if scene_id == SC_CONFIG_FAULT:
        return s.config_ok
    return False


def detect_all(s: Signals, cfg: GuardConfig | None = None) -> list[str]:
    """原始检测（未做优先级合并）。按优先级从高到低返回。"""
    cfg = cfg or GuardConfig()
    hits = []
    for sid, spec in SCENES.items():
        if spec.detect is None:
            continue
        try:
            if spec.detect(s):
                hits.append(sid)
        except Exception:                    # noqa: BLE001  检测器不能把守卫带崩
            continue
    hits.sort(key=lambda x: SCENES[x].priority, reverse=True)
    return hits


def resolve(detected: list[str], owner: str | None,
            cfg: GuardConfig | None = None) -> str | None:
    """优先级 + 因果抑制：同一时刻只留一个场景。"""
    if not detected:
        return None
    out = list(detected)
    priorities = {sid: SCENES[sid].priority for sid in out}
    # 高优先级压制低优先级
    for high, low_set in _SUPPRESS.items():
        if high in out:
            out = [x for x in out if x == high or x not in low_set]
    # 已误买 / 反作弊 / 配置坏：独占
    for hard in (SC_MISBUY, SC_ANTICHEAT, SC_CONFIG_FAULT):
        if hard in out:
            return hard
    if not out:
        return None
    out.sort(key=lambda x: priorities.get(x, 0), reverse=True)
    return out[0]


# ---------------- 守卫 ----------------

class SceneGuard:
    """五级状态机 + 优先级合并 + 人工等待/自动续跑。"""

    def __init__(self, cfg: GuardConfig | None = None, clock: Callable[[], float] | None = None):
        self.cfg = cfg or GuardConfig()
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._state = STATE_OK
        self._level = LEVEL_NORMAL
        self._owner: str | None = None
        self._since = self._clock()
        self._total_since = self._since
        self._level_since = self._since
        self._fails = 0
        self._attempts = 0
        self._ok_ticks = 0
        self._last_detect = 0.0
        self._streak: dict[str, int] = {}
        self._evidence: dict[str, Any] = {}
        self._plan: list[str] = []
        self._plan_tried: set[tuple] = set()
        self._history: deque = deque(maxlen=60)
        self._containment_pending = False
        self._pause_reason = ""
        self._entries = 0
        # 硬停门闩：hard_halt / 硬停场景置位，只有用户显式「继续」
        # （user_resume / auto_resume）才清除。上一轮启动、下一任务派发都
        # 不许自行清掉它 —— 否则队列会在秒级内接着下单（审查 B02）。
        self._halt_latch = False

    # ---------------- 对外 ----------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def level(self) -> int:
        with self._lock:
            return self._level

    @property
    def owner(self) -> str | None:
        with self._lock:
            return self._owner

    @property
    def entries(self) -> int:
        """进入过异常场景的次数（用于验收「确实检测到了」）。"""
        with self._lock:
            return self._entries

    @property
    def halt_latched(self) -> bool:
        """硬停是否仍然锁着（跨任务、跨轮持久，直到用户点「继续」）。"""
        with self._lock:
            return self._halt_latch

    def now(self) -> float:
        return float(self._clock())

    def feed(self, signals: Signals | Mapping[str, Any] | None,
             now: float | None = None) -> dict:
        """喂一次遥测。返回快照（前端可直接用）。"""
        s = signals if isinstance(signals, Signals) else Signals.from_mapping(signals)
        t = self.now() if now is None else float(now)
        with self._lock:
            s.now = t
            detected = detect_all(s, self.cfg)
            if not detected:
                for k in list(self._streak):
                    self._streak[k] = max(0, self._streak[k] - 1)
                self._ok_tick(t, s)
                return self._snapshot(s)

            for sid in detected:
                self._streak[sid] = self._streak.get(sid, 0) + 1
            confirmed = [sid for sid in detected
                         if self._streak.get(sid, 0) >= SCENES[sid].min_confirm
                           or SCENES[sid].hard_level]
            winner = resolve(confirmed, self._owner, self.cfg)

            if self._state == STATE_HALTED:
                # 已硬停：只记录证据，不启动新动作
                if winner:
                    self._history.append({"t": t, "scene": winner,
                                          "action": "observe", "ok": None,
                                          "level": self._level})
                return self._snapshot(s)

            if winner:
                self._last_detect = t
                if self._owner == winner:
                    self._evidence.update(self._evidence_from_signals(s, winner))
                    return self._snapshot(s)
                if self._owner is None or self._state == STATE_OK:
                    self._enter(winner, t, s)
                else:
                    cur = SCENES.get(self._owner)
                    new = SCENES[winner]
                    # 人工等待中，只有硬停/更高优先级能抢占
                    if self._state == STATE_WAITING_HUMAN:
                        if new.hard_level == LEVEL_HALT:
                            self._enter(winner, t, s)
                    elif cur and new.priority > cur.priority:
                        self._enter(winner, t, s, preempt=cur.id)
                    elif new.hard_level == LEVEL_HALT:
                        self._enter(winner, t, s, preempt=cur.id if cur else None)
                    else:
                        self._history.append({"t": t, "scene": winner,
                                              "action": "suppressed", "ok": None,
                                              "level": self._level,
                                              "by": self._owner})
                return self._snapshot(s)

            # 有异常信号但还没到 min_confirm（画面抖动/第一次看到）：这一拍
            # **不能**当成正常。以前这里继续走 _ok_tick，抖动帧被算进清场，
            # 两次「假正常」就把守卫放回 OK，异常被静默吞掉（审查 B14）。
            self._last_detect = t
            self._ok_ticks = 0
            return self._snapshot(s)

    def force(self, scene_id: str, evidence: Mapping[str, Any] | None = None,
              now: float | None = None) -> dict:
        """引擎/调度器直接上报一个确定场景（绕过错觉检测，用于异常路径）。"""
        t = self.now() if now is None else float(now)
        with self._lock:
            if scene_id not in SCENES:
                return self._snapshot(None)
            spec = SCENES[scene_id]
            self._evidence.update(dict(evidence or {}))
            if self._state == STATE_HALTED and spec.hard_level != LEVEL_HALT:
                return self._snapshot(None)
            if self._state == STATE_WAITING_HUMAN and spec.hard_level != LEVEL_HALT:
                if SCENES[scene_id].priority < SCENES[self._owner or scene_id].priority:
                    return self._snapshot(None)
            self._enter(scene_id, t, None)
            return self._snapshot(None)

    def plan_failed(self, reason: str = "", now: float | None = None) -> dict:
        """一整套组合都没到目标：升级一级；到顶就进人工/硬停。"""
        t = self.now() if now is None else float(now)
        with self._lock:
            if self._state not in (STATE_RECOVERING,):
                return self._snapshot(None)
            self._fails += 1
            self._attempts += 1
            self._history.append({"t": t, "scene": self._owner, "action": "plan",
                                  "ok": False, "level": self._level,
                                  "why": reason})
            n = {LEVEL_RETRY: self.cfg.l1_fail_n,
                 LEVEL_FIX: self.cfg.l2_fail_n,
                 LEVEL_RELOCATE: self.cfg.l3_fail_n}.get(self._level, 1)
            if self._fails >= max(1, n):
                self._escalate(t)
            return self._snapshot(None)

    def tick(self, now: float | None = None) -> dict:
        """按时间升级：某一级停留超过预算也算失败。"""
        t = self.now() if now is None else float(now)
        with self._lock:
            if self._state != STATE_RECOVERING:
                return self._snapshot(None)
            budget = {LEVEL_RETRY: self.cfg.l1_budget_s,
                      LEVEL_FIX: self.cfg.l2_budget_s,
                      LEVEL_RELOCATE: self.cfg.l3_budget_s}.get(self._level)
            if budget is not None and t - self._level_since >= budget:
                self._history.append({"t": t, "scene": self._owner,
                                      "action": "timeout", "ok": False,
                                      "level": self._level})
                self._escalate(t)
            if t - self._total_since >= self._total_budget():
                self._history.append({"t": t, "scene": self._owner,
                                      "action": "total_timeout", "ok": False,
                                      "level": self._level})
                self._to_guide_or_halt(t, "总时长用完")
            return self._snapshot(None)

    def succeed(self, now: float | None = None) -> dict:
        """恢复规划器确认到达目标状态。"""
        t = self.now() if now is None else float(now)
        with self._lock:
            self._history.append({"t": t, "scene": self._owner, "action": "goal",
                                  "ok": True, "level": self._level})
            self._reset()
            return self._snapshot(None)

    def user_resume(self, now: float | None = None) -> dict:
        """用户点了唯一的「继续」：清掉暂停，回到正常盯盘。"""
        t = self.now() if now is None else float(now)
        with self._lock:
            self._history.append({"t": t, "scene": self._owner,
                                  "action": "resume", "ok": True,
                                  "level": self._level})
            self._reset()
            return self._snapshot(None)

    def auto_resume(self, now: float | None = None) -> dict:
        """人工段已经通过（检测到目标状态）：自动续跑，不需要用户再点。"""
        return self.user_resume(now)

    def abandon_recovery(self, reason: str = "",
                         now: float | None = None) -> dict:
        """本轮结束、自动恢复却没有可执行动作：结算成人工/硬停。

        RECOVERING 以前没有任何出口：引擎退出后守卫永远停在「自动恢复中」，
        下一个任务启动时又把它当普通状态放过去（审查 B05）。这里把它升级到
        该场景的最终级别 —— 要人处理的就等人，该硬停的就硬停。
        """
        t = self.now() if now is None else float(now)
        with self._lock:
            if self._state != STATE_RECOVERING:
                return self._snapshot(None)
            self._history.append({"t": t, "scene": self._owner,
                                  "action": "abandon_recovery", "ok": False,
                                  "level": self._level, "why": str(reason or "")})
            self._to_guide_or_halt(t, reason or "自动恢复没有可执行动作")
            return self._snapshot(None)

    def hard_halt(self, reason: str = "", now: float | None = None) -> dict:
        t = self.now() if now is None else float(now)
        with self._lock:
            self._state = STATE_HALTED
            self._level = LEVEL_HALT
            self._pause_reason = str(reason or "")
            self._containment_pending = True
            # 硬停门闩：跨任务、跨轮持久，直到用户显式「继续」（B02）
            self._halt_latch = True
            self._since = t
            self._level_since = t
            self._plan = []
            self._plan_tried.clear()
            self._history.append({"t": t, "scene": self._owner,
                                  "action": "halt", "ok": True, "level": LEVEL_HALT})
            return self._snapshot(None)

    def containment_done(self, now: float | None = None) -> dict:
        with self._lock:
            self._containment_pending = False
            self._history.append({"t": self.now() if now is None else float(now),
                                  "scene": self._owner, "action": "contained",
                                  "ok": True, "level": LEVEL_HALT})
            return self._snapshot(None)

    def set_plan(self, plan: list[str]) -> None:
        with self._lock:
            self._plan = list(plan)

    def record_action(self, action: str, ok: bool,
                      now: float | None = None) -> None:
        t = self.now() if now is None else float(now)
        with self._lock:
            self._attempts += 1
            self._history.append({"t": t, "scene": self._owner,
                                  "action": str(action), "ok": bool(ok),
                                  "level": self._level})

    def add_evidence(self, **kw) -> None:
        with self._lock:
            self._evidence.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot(None)

    # ---------------- 内部 ----------------

    def _total_budget(self) -> float:
        spec = SCENES.get(self._owner or "")
        return float(spec.total_s if spec else self.cfg.max_total_s)

    def _evidence_from_signals(self, s: Signals, scene_id: str) -> dict:
        if scene_id == SC_CONFIRM_UNEXPECTED:
            return {"confirm_price": s.confirm_price, "budget": s.budget,
                    "confirm_item": s.confirm_item, "expected_item": s.expected_item}
        if scene_id == SC_MISBUY:
            return {"balance_before": s.balance_before,
                    "balance_after": s.balance_after,
                    "expected_price": s.expected_price,
                    "bought_delta": s.bought_delta}
        return {}

    def _enter(self, scene_id: str, t: float, s: Signals | None,
               preempt: str | None = None) -> None:
        spec = SCENES[scene_id]
        # 同一个场景被重复上报（force 连发、抖动重入）不能重置总计时：
        # 以前每次 _enter 都把 _total_since 拨到当下，实测总用时从 44.5s
        # 回退到 5.0s，total_timeout 永远到不了（审查 B14）。
        same = (self._owner == scene_id
                and self._state in (STATE_RECOVERING, STATE_WAITING_HUMAN))
        if s is not None:
            self._evidence.update(self._evidence_from_signals(s, scene_id))
        self._owner = scene_id
        self._entries += 1
        self._since = t
        if not same:
            self._total_since = t
            self._level_since = t
            self._fails = 0
            self._attempts = 0
        self._ok_ticks = 0
        self._plan = []
        self._plan_tried.clear()
        if preempt:
            self._history.append({"t": t, "scene": scene_id,
                                  "action": "preempt", "ok": True,
                                  "level": 0, "by": preempt})
        if spec.hard_level == LEVEL_HALT:
            self._state = STATE_HALTED
            self._level = LEVEL_HALT
            self._containment_pending = True
            self._halt_latch = True
        elif spec.hard_level == LEVEL_GUIDE:
            self._state = STATE_WAITING_HUMAN
            self._level = LEVEL_GUIDE
            self._containment_pending = False
        else:
            self._state = STATE_RECOVERING
            if not same:
                self._level = LEVEL_RETRY
            self._containment_pending = False

    def _escalate(self, t: float) -> None:
        self._fails = 0
        self._level_since = t
        self._plan = []
        self._plan_tried.clear()
        if self._level < LEVEL_RELOCATE:
            self._level += 1
            self._state = STATE_RECOVERING
            self._history.append({"t": t, "scene": self._owner,
                                  "action": "level_up", "ok": True,
                                  "level": self._level})
            return
        self._to_guide_or_halt(t, "自动手段用完")

    def _to_guide_or_halt(self, t: float, why: str) -> None:
        spec = SCENES.get(self._owner or "")
        if spec and spec.halt_on_exhausted:
            self.hard_halt(why, t)
            return
        if spec and spec.max_level >= LEVEL_GUIDE:
            self._state = STATE_WAITING_HUMAN
            self._level = LEVEL_GUIDE
            self._level_since = t
            self._plan = []
            self._history.append({"t": t, "scene": self._owner,
                                  "action": "guide", "ok": True,
                                  "level": LEVEL_GUIDE, "why": why})
            return
        self.hard_halt(why, t)

    def _ok_tick(self, t: float, s: Signals | None = None,
                 owner: str | None = None) -> None:
        """没有异常信号。满足滞回条件才清零，防止抖动反复进出。"""
        if self._state == STATE_WAITING_HUMAN:
            # 人工段的目标已经达成 → 自动续跑（B04）。以前 WAITING_HUMAN 只能
            # 等用户点按钮，可「登录页/开卖页/余额恢复」这些目标本身就看得到，
            # 白白停在那儿让整条队列不再开火。
            if self._owner and s is not None and goal_met(self._owner, s):
                self._ok_ticks += 1
                if (self._ok_ticks >= max(1, self.cfg.clear_ticks)
                        and t - self._last_detect >= self.cfg.clear_window_s):
                    self._history.append({"t": t, "scene": self._owner,
                                          "action": "auto_resume", "ok": True,
                                          "level": self._level})
                    self._reset()
                return
            self._ok_ticks = 0
            return
        if self._state != STATE_RECOVERING or not self._owner:
            return
        self._ok_ticks += 1
        # 两个条件都要满足：连续足够多拍正常，**且**最后一次见到异常已经过去
        # 足够久。只看拍数的话，慢间隔下两拍就能差几十秒，抖动一过就清场。
        if (self._ok_ticks >= max(1, self.cfg.clear_ticks)
                and t - self._last_detect >= self.cfg.clear_window_s):
            self._history.append({"t": t, "scene": self._owner,
                                  "action": "clear", "ok": True,
                                  "level": self._level})
            self._reset()

    def _reset(self) -> None:
        self._state = STATE_OK
        self._level = LEVEL_NORMAL
        self._owner = None
        self._fails = 0
        self._attempts = 0
        self._ok_ticks = 0
        self._streak.clear()
        self._evidence.clear()
        self._plan = []
        self._plan_tried.clear()
        self._containment_pending = False
        self._pause_reason = ""
        self._halt_latch = False
        self._since = self.now()

    def _snapshot(self, s: Signals | None) -> dict:
        spec = SCENES.get(self._owner or "")
        if self._state == STATE_HALTED:
            message = (spec.halt_message if spec else "") or "程序已经安全停止。"
            button = {"id": spec.button_id if spec else "quit",
                      "label": spec.button_label if spec else "关闭程序"}
        elif self._state == STATE_WAITING_HUMAN:
            message = (spec.guide_message if spec else "") or "需要你处理一下。"
            button = {"id": spec.button_id if spec else "resume",
                      "label": spec.button_label if spec else "继续"}
        else:
            message = _DOING.get(self._level, "")
            # 自动处理中也要有且只有一个主按钮：用户随时能安全停止。
            button = ({"id": "stop", "label": "停止"} if self._level > LEVEL_NORMAL
                      else None)
        return {
            "state": self._state,
            "level": self._level,
            "level_text": LEVEL_TEXT.get(self._level, ""),
            "scene": self._owner,
            "scene_text": spec.name if spec else "",
            "priority": spec.priority if spec else 0,
            "goal": spec.goal_text if spec else "",
            "message": message,
            "button": button,
            "doing": _DOING.get(self._level, ""),
            "since": self._since,
            "attempts": self._attempts,
            "elapsed_s": round(max(0.0, self.now() - self._since), 2),
            "plan": list(self._plan),
            "containment_pending": self._containment_pending,
            "ok_ticks": self._ok_ticks,
            "containment_ok": self._evidence.get("containment_ok"),
            "halt_latched": self._halt_latch,
            "evidence": dict(self._evidence),
            "history": list(self._history)[-30:],
        }

    def doing_text(self, engine_text: str = "") -> str:
        """给前端的一句「现在在做什么」。正常时用引擎的话，异常时用处置动作。"""
        with self._lock:
            if self._level >= LEVEL_GUIDE:
                return self._snapshot(None)["message"]
            if self._state == STATE_RECOVERING:
                return _DOING.get(self._level, "正在自动处理")
            return engine_text or "正在正常盯着画面"
