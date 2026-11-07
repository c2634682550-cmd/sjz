"""连点参数（`click.burst.*`）。

语义前提（视频分析已确认）：抢皮肤是**抽签模型** —— 点进去拿号、摇号、点数
比大小。所以连点**不提高**单次中签概率，它只缩短「没中之后再次进池」的
等待：在命中之前，把下一次进场尽早发出去。任何界面文案、日志都不许写成
「提高中签率 / 提高成功率」。

四个字段（与 `_feature_contract.md` 一致，不再并存别的名字）：

    mode         append / burst
                 append = 完全保持现有 `click_hz` 节奏，本组参数不改变开火频率；
                 burst  = 开点瞬间按下面的形态反复点击，`window` 用尽或弹窗
                          出现，立刻回到 `click_hz` 节奏。
    hold_ms      20~1000，每一次按下**按住**多久（毫秒，默认 100）
    interval_ms  1~50，前一次抬起之后到下一次按下之间隔多久（毫秒，默认 5）
    window       all / 1s / 2s，连点阶段最晚持续到开点后多久（默认 all）

三个时间量的关系是物理自洽的：`window`（或 `fire_timeout`）决定阶段总时长，
`hold_ms + interval_ms` 决定每一次点击的形态。阶段内**能排下几次就点几次**；
剩余时间不够一次完整的 `hold_ms` 就不再开始新点击，绝不为了凑次数压缩按住
时长。

越界、NaN、负数、小数、非数字、非法枚举：一律拒收并给出可读原因；被拒的值
不会静默变成默认值，原因通过 `begin()` 的 `say` 打进日志。缺键才算没写，
缺键走 schema 默认（`config.DEFAULTS` 已深合并）。

点击走引擎已有的 `Mouse.click`（三通道 / 零位移通道），这里只负责节奏和
按住时长，不直接调 Win32、不另起注入。
"""

from __future__ import annotations

import time

# ---------------------------------------------------------------- 常量

MODE_APPEND = "append"
MODE_BURST = "burst"
MODES = (MODE_APPEND, MODE_BURST)

WINDOW_ALL = "all"
WINDOWS = ("all", "1s", "2s")
WINDOW_SECONDS = {WINDOW_ALL: None, "1s": 1.0, "2s": 2.0}

DEFAULTS = {
    "mode": MODE_APPEND,
    "hold_ms": 100,
    "interval_ms": 5,
    "window": WINDOW_ALL,
}
RANGES = {
    "hold_ms": (20, 1000),
    "interval_ms": (1, 50),
}
LABELS = {
    "mode": "连点模式",
    "hold_ms": "按住时长",
    "interval_ms": "点击间隔",
    "window": "触发窗口",
}

# 连点阶段空档采样节拍：和开火循环里的 4ms 保持一致 —— 弹窗在这几毫秒里
# 出现也要能立刻看见，不能睡死。
SAMPLE_S = 0.004

BURST_PREFIX = "click.burst."

# 互斥提示：服务端保存回包（warnings）与运行日志共用同一句，避免两套说法。
MUTEX_WARNING = "连点与补刀互斥，当前按连点执行"
MUTEX_REASON = (MUTEX_WARNING
                + "：同一次进场只由连点出手，假购买补刀本次不生效")


class BurstConfigError(ValueError):
    """单个字段被拒收：带键名和可读原因。"""

    def __init__(self, key, reason):
        self.key = key
        self.reason = reason
        super().__init__(f"{key}：{reason}")


# ---------------------------------------------------------------- 读配置


def _data(cfg):
    data = getattr(cfg, "data", None)
    if isinstance(data, dict):
        return data
    return cfg if isinstance(cfg, dict) else {}


def _raw_section(cfg):
    """取 `click.burst` 这一层。缺层 / 类型不对都算「没写」。"""
    cur = _data(cfg)
    for part in ("click", "burst"):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _int_field(key, name, value):
    lo, hi = RANGES[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise BurstConfigError(key, f"不是数字：{value!r}") from None
        else:
            raise BurstConfigError(key, f"不是数字：{value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise BurstConfigError(key, f"不是数字：{value!r}") from None
    if num != num or num in (float("inf"), float("-inf")):
        raise BurstConfigError(
            key, f"必须是有限数字（NaN/Infinity 不接受）：{value!r}")
    if num != int(num):
        raise BurstConfigError(key, f"必须是整数：{value!r}")
    val = int(num)
    if val < lo:
        raise BurstConfigError(key, f"{val} 小于下限 {lo}")
    if val > hi:
        raise BurstConfigError(key, f"{val} 大于上限 {hi}")
    return val


def _enum_field(key, value, allowed):
    val = "" if value is None else str(value)
    if val not in allowed:
        raise BurstConfigError(
            key, f"不在可选值 [{', '.join(allowed)}]：{value!r}")
    return val


def field(key, value):
    """把单个字段按类型收死。非法抛 `BurstConfigError`。"""
    name = key[len(BURST_PREFIX):] if key.startswith(BURST_PREFIX) else key
    if name == "mode":
        return _enum_field(key, value, MODES)
    if name == "window":
        return _enum_field(key, value, WINDOWS)
    if name in RANGES:
        return _int_field(key, name, value)
    raise KeyError(key)


def read(cfg):
    """读出可执行计划。

    返回 `(plan, errors)`：
      · `plan` 永远是一个自洽的 dict（被拒的字段不留在里面）；
      · `errors` 是被拒字段的可读原因列表，调用方必须打出来（不许静默）。
    缺键不算错 —— 那是「用户没写」，走默认。
    """
    plan = dict(DEFAULTS)
    errors = []
    raw = _raw_section(cfg)
    if not isinstance(raw, dict):
        return plan, errors
    for name in DEFAULTS:
        if name not in raw:
            continue
        key = BURST_PREFIX + name
        try:
            plan[name] = field(key, raw[name])
        except BurstConfigError as e:
            errors.append(str(e))
    return plan, errors


def begin(cfg, say=None):
    """开火前调用：返回 burst 计划，或 None（append 模式 / 没开连点）。

    `say(text, level)` 用来把被拒原因和互斥说明打进日志。没有它也能工作，
    但那样就是静默 —— 引擎务必传。
    """
    plan, errors = read(cfg)
    if say:
        for msg in errors:
            say(f"连点参数被拒收（{msg}），该字段按默认值执行", "warn")
    if plan["mode"] != MODE_BURST:
        return None
    if say:
        blocked, reason = followup_gate(cfg)
        if blocked:
            say("连点开启：" + reason, "info")
    return plan


def followup_gate(cfg):
    """连点 / 补刀互斥闸门。返回 `(allowed, reason)`。

    硬约束：`mode=burst` 时补刀不得再覆盖同一次进场 —— 两个功能同时开，
    同一次进场只许一处出手，否则会双买。保存阶段不改用户的值（契约 5.1），
    但运行阶段必须显式拒绝并说明原因。
    """
    plan, _ = read(cfg)
    if plan["mode"] == MODE_BURST:
        return False, MUTEX_REASON
    return True, ""


# ---------------------------------------------------------------- 执行


def run(plan, *, t_fire, fire_timeout, click, dialog,
        stop=None, guard=None, beat=None, now=None, sleep=None):
    """在连点窗口内按 `hold_ms` / `interval_ms` 反复点击，返回统计。

    `now` / `sleep` 可注入（测试用假时钟）。`click(hold_s)` 必须让按住
    时长真的发生（引擎里是 `Mouse.click(..., hold=hold_s)`），不能写死。

    调用方必须保证：`click` 走的是项目已有的鼠标通道；`dialog()` 返回
    「确认弹窗已出现」；`stop()` 返回用户是否按了停止；`guard()` 做故障
    兜底（可能抛 Aborted）。

    停止条件按优先级：用户停止 → 弹窗出现 → 窗口/开火超时用尽。
    弹窗出现后一下都不多点（先查 `dialog()` 再决定要不要点下一发）。
    剩余时间不够一次完整 `hold_ms` 时不再开始新点击 —— 不压缩按住时长。
    """
    now = now or time.perf_counter
    sleep = sleep or time.sleep
    hold = plan["hold_ms"] / 1000.0
    interval = plan["interval_ms"] / 1000.0
    win = WINDOW_SECONDS.get(plan["window"])
    stage = float(fire_timeout) if win is None else min(win, float(fire_timeout))
    stage = max(0.0, stage)
    end = t_fire + stage

    clicks = 0
    holds_ms = []
    stopped_by = "window"
    t0 = now()
    while True:
        if stop is not None and stop():
            stopped_by = "stop"
            break
        # 物理自洽：排不下一次完整按住就不再开新的一发，绝不压 hold。
        if end - now() < hold:
            stopped_by = "window"
            break
        if guard is not None:
            guard()
        if beat is not None:
            beat()
        t_press = now()
        click(hold)
        clicks += 1
        holds_ms.append(max(0.0, (now() - t_press) * 1000.0))
        if dialog():
            stopped_by = "dialog"
            break
        # 前一次抬起后按 interval_ms 等，但按 4ms 采样 —— 弹窗/停止都要立刻看见。
        nxt = min(now() + interval, end)
        while now() < nxt:
            sleep(min(SAMPLE_S, max(0.0, nxt - now())))
            if stop is not None and stop():
                stopped_by = "stop"
                break
            if dialog():
                stopped_by = "dialog"
                break
        if stopped_by in ("stop", "dialog"):
            break

    return {
        "mode": plan["mode"],
        "clicks": clicks,
        "stopped_by": stopped_by,
        "hold_ms": plan["hold_ms"],
        "interval_ms": plan["interval_ms"],
        "holds_ms": holds_ms,
        "window": plan["window"],
        "stage_ms": stage * 1000.0,
        "elapsed_ms": max(0.0, (now() - t0) * 1000.0),
    }
