"""重负载用例分组表 —— 唯一真源，可枚举、可审计。

分组规则（准入条件，写死在代码里，不靠猜）
------------------------------------------
一条用例属于 heavy，当且仅当它满足下面至少一条：

* ``timing``      —— 断言里有墙钟阈值（毫秒/微秒级），结果取决于 CPU 是否被抢；
* ``spawn``       —— 起子进程 / 多解释器 / 命名互斥体，跨进程竞态；
* ``playwright``  —— 起真 Chromium（Playwright），吃内存与 CPU；
* ``ocr``         —— 跑真 OCR（RapidOCR 模型或 PP-OCR 服务），吃 CPU/GPU；
* ``realdriver``  —— 碰真实 Win32 输入驱动 / 真窗口消息，抢系统级资源；
* ``full``        —— 一次跑完整条链路（采样→决策→报告 / 恢复规划→蒙特卡洛），
                     单条用例就是一段持续负载。

其余用例一律视为轻量，不加锁、不串行。

为什么用「模块 → 组」的静态表而不是靠命名规则去猜
------------------------------------------------
命名规则会误判（比如 ``test_market_video`` 听起来像轻量，实际跑真 OCR 帧）。
表是显式的：新增用例要进 heavy 组，必须在这里加一行 —— 评审时看得见。

同一模块可以属于多个组；组锁会全部拿一遍（总闸 ``heavy`` 仍然是全局串行），
文件名用 ``GROUP_PRIORITY`` 里第一个命中的组，便于日志排查。

node 级覆盖用于「同一个模块里只有个别用例重」的情况，key 是
``模块文件名::用例名``。
"""
from __future__ import annotations

import os

#: 组锁命名与诊断时的优先级（第一个命中的组决定锁文件名）
GROUP_PRIORITY = ("timing", "playwright", "ocr", "spawn", "full", "realdriver")

GROUP_DESCRIPTIONS = {
    "timing": "时序敏感：毫秒/微秒级墙钟阈值断言，CPU 被抢就假失败",
    "playwright": "Playwright：起真 Chromium，吃内存/CPU/焦点",
    "ocr": "真 OCR：RapidOCR 模型或 PP-OCR 服务，吃 CPU/GPU",
    "spawn": "起子进程/多解释器/命名互斥体，跨进程竞态",
    "full": "完整链路：一次用例就是一段持续负载",
    "realdriver": "真实驱动：碰真 Win32 输入/窗口消息，抢系统资源",
}

#: 模块级分组（key 为 tests/ 下的文件名，不含路径）
MODULE_GROUPS: dict[str, tuple[str, ...]] = {
    # ---- full：完整链路 -------------------------------------------------
    "test_soak.py": ("full",),                    # soak 短集成跑：采样→报告整链路
    "test_recovery.py": ("full",),                # 恢复规划 + 蒙特卡洛统计
    "test_engine_countdown_flow.py": ("full",),   # 引擎外层完整流程（挑件→抢→换下一件）

    # ---- timing：墙钟阈值 ----------------------------------------------
    "test_precision.py": ("timing",),             # 真 QPC / 真 Sleep / 线程优先级
    "test_click_speed.py": ("timing",),           # 点击延迟预算（毫秒级）
    "test_feature_mouse_human.py": ("timing", "playwright"),
    # 段预热/尾补断言 lag < 2ms；同模块还有一条真 Chromium 的前端联通用例

    # ---- spawn：子进程 -------------------------------------------------
    "test_fire_proc.py": ("spawn",),
    "test_fire_process.py": ("spawn",),
    "test_hotkey_mutex.py": ("spawn",),
    "test_persist_hardening.py": ("spawn",),
    "test_process_lifecycle.py": ("spawn",),

    # ---- playwright：真 Chromium ---------------------------------------
    "test_feature_skip_late.py": ("playwright",),
    "test_frontend_switch_rollback.py": ("playwright",),

    # ---- ocr：真识别 ---------------------------------------------------
    "test_popup.py": ("ocr",),
    "test_market_video.py": ("ocr",),
    "test_engine_insufficient.py": ("ocr",),
    "test_skin_ocr_real.py": ("ocr",),
    "test_ocr_panel.py": ("ocr",),
    "test_ocr_price.py": ("ocr",),
    "test_ocr_grade.py": ("ocr",),

    # ---- realdriver：真实驱动/窗口 -------------------------------------
    "test_interception.py": ("realdriver",),
    "test_uipi_probe.py": ("realdriver",),
}

#: 用例级覆盖（key 为 ``模块文件名::用例名``）
NODE_GROUPS: dict[str, tuple[str, ...]] = {
    # preflight 会调 ocr.available()，冷启动时真的加载 RapidOCR 模型
    "test_console_api.py::test_preflight_reports_every_item_with_a_fix": ("ocr",),
}


def _split(nodeid: str) -> tuple[str, str]:
    """nodeid -> (模块文件名, 用例部分)。兼容 Windows 路径分隔符与 xdist 前缀。"""
    path, _, test = nodeid.replace("\\", "/").partition("::")
    return os.path.basename(path), test


def groups_for_nodeid(nodeid: str) -> tuple[str, ...]:
    """返回该用例命中的组（无命中 = 轻量用例）。"""
    module, test = _split(nodeid)
    override = NODE_GROUPS.get("%s::%s" % (module, test))
    if override:
        return override
    return MODULE_GROUPS.get(module, ())


def is_heavy(nodeid: str) -> bool:
    return bool(groups_for_nodeid(nodeid))


def primary_group(groups) -> str:
    """用于锁文件命名的组：按 GROUP_PRIORITY 取第一个。"""
    ordered = [g for g in GROUP_PRIORITY if g in groups]
    return ordered[0] if ordered else (groups[0] if groups else "unknown")


def inventory_text() -> str:
    """``--test-lock-groups`` 的输出：一眼能看全的分组清单。"""
    lines = ["重负载用例分组（tests/_isolation/groups.py）", ""]
    for name in GROUP_PRIORITY:
        modules = [m for m, gs in MODULE_GROUPS.items() if name in gs]
        nodes = [n for n, gs in NODE_GROUPS.items() if name in gs]
        lines.append("[%s] %s" % (name, GROUP_DESCRIPTIONS[name]))
        if modules:
            lines.append("  模块：")
            for m in sorted(modules):
                lines.append("    %s" % m)
        if nodes:
            lines.append("  用例：")
            for n in sorted(nodes):
                lines.append("    %s" % n)
        if not modules and not nodes:
            lines.append("  （空）")
        lines.append("")
    total = len(MODULE_GROUPS)
    lines.append("共 %d 个模块、%d 条用例级覆盖；其余用例为轻量组（可并发）。"
                 % (total, len(NODE_GROUPS)))
    lines.append('也支持在用例上直接写 @pytest.mark.heavy(group="timing") 临时入组。')
    return "\n".join(lines)
