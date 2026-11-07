"""配置。坐标存 0-1 归一化比例，换分辨率不用重配。

config.json 一份文件承担四件事：用户设置、任务队列、运行状态、模板索引。
拆分成本高（旧配置要迁移），所以这里用**事务**兜住一致性：

    with Config.transaction() as cfg:      # 读盘 + 改 + 落盘 全在一把锁里
        cfg["queue"].append(task)

以前每个调用方各自 Config.load() → 改一点 → save()，两边都持有一份
**全量快照**，后写的把先写的整份盖掉 —— 表现就是「设置改完几秒后自己
变回去」「开关拨了不生效」。
"""
import contextlib
import hashlib
import json
import math
import re
import shutil
import threading
import time
import os
import sys

from . import region_table as _region_table
from . import retention as _retention
from .version import APP_VERSION

if getattr(sys, "frozen", False):
    # 单文件 exe：__file__ 指向临时解包目录（sys._MEIPASS），退出即删。
    # 配置和模板写在那里等于每次启动都丢，所以放到 exe 同级目录 ——
    # 用户也要能直接改 config.json、看 templates/ 里的图。
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(ROOT, "config.json")
TEMPLATE_DIR = os.path.join(ROOT, "templates")
DATA_DIR = os.path.join(ROOT, "data")


def user_dir():
    """用户数据目录（**不在仓库/程序目录里**）。

    区域快照这类「写坏要能退回」的数据放这里，避免进 git、也不会随
    程序目录被清空。`DAFORS_USER_DIR` 可覆盖（便携版、测试）。
    """
    env = os.environ.get("DAFORS_USER_DIR")
    if env:
        return os.path.abspath(env)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "dafors")
    return os.path.join(os.path.expanduser("~"), ".dafors")


DEFAULTS = {
    "monitor": 1,
    # 9 个区域的内置坐标（1080p 客户区基准，归一化 0..1）。
    # 买家不用再自己拖框：程序按客户区尺寸等比换算；高级设置里只做微调。
    "regions": _region_table.clone(),
    # 自动标定状态：开抢前跑一次锚点校验，结果写回这里供界面提示。
    # 默认开启；P0 修复的验证依据见 `_auto_calibration.md`（匹配几何误差
    # 三档实测 0px、缺关键锚点必 fallback、手框区域不被覆盖）。若要临时
    # 关掉，把 auto 设为 False 即可，界面与闸门都会按 skipped 处理。
    "calibration": {
        "auto": True,
        # 标定结果直接写进 regions（默认开）。留成「提议不落盘」会让闸门
        # 校验提案、引擎点配置，两套坐标并存；4K 下 40/47/48px 全部放行但
        # 真实偏移一模一样。手框区域仍永不覆盖。
        "apply_auto": True,
        "at": "",
        # never / ok / adjusted / skipped / error / fallback
        "status": "never",
        "attempts": 0,
        "message": "",
        # 当次闸门的拦截原因；空串表示放行。这样 preflight 不用 import
        # autocalib 也能按同一套阈值判断（阈值只在 autocalib 里定义一次）。
        "block": "",
        "client": [0, 0],
        "offset": None,
        "offsets": [],
        "anchors": [],
        # 标定建议值（归一化），需用户确认或 apply_auto 才写入。
        "proposed": {},
        # 上一次自动写入的值，用来区分「程序写的」和「用户手框的」。
        "applied": {},
        "apply_skipped_manual": [],
    },
    "trigger_mode": "vanish",
    # 抢购路线：countdown（公示期倒计时归零就买）/ list（在售列表）/ detail（详情页按钮）
    "workflow": "countdown",
    # 进入交易行（市场）的入口。真机确认：大厅底部快捷栏中间那个图标对应
    # F4，点它或按 F4 都能进交易行。
    #   market_hotkey    主路径热键（走 Keyboard → Interception 真实设备）
    #   market_icon      备选路径：图标归一化坐标（客户区 0..1）
    #   market_icon_enabled 是否启用图标备选路径（默认 False）
    #                    **未验证/实验性**：真机点 (506,1049) 一次仍在 hall，
    #                    路径不通；保持配置项但默认关闭，不再自动降级。
    #   market_timeout_s 每次尝试后轮询页面识别的超时
    #   market_rounds    尝试轮数：1 轮 = F4 一次 + 图标一次（最多 1 按 + 1 点）
    # 注意：不能叫 "nav" —— 那一节已经被在售面板几何（ons_panel/ons_columns）
    # 占了，重名会被后面的字面量覆盖掉（真机踩过：配置读到的还是面板几何）。
    "market_entry": {
        "market_hotkey": "F4",
        "market_icon": [0.264, 0.972],
        "market_icon_enabled": False,
        "market_timeout_s": 2.5,
        "market_rounds": 1,
    },
    "threshold": 0.85,
    "click_hz": 50,
    "confirm_clicks": 20,
    "confirm_interval_ms": 10,
    # 点确认的兜底时长：弹窗还开着就一直点，最多点这么久。
    # 替代原来「倒计时归零后固定 sleep(0.6)」的等待。
    "confirm_timeout_s": 2.0,
    "fire_timeout_s": 8.0,
    "arm_grace_s": 1.5,     # 到点还没见到占位按钮时，宽限多久后强行开点
    "failsafe": True,
    # 输入通道（正式三选一）：
    #   interception 内核驱动注入（默认，真实硬件事件、不动光标）
    #   legacy       SetCursorPos 借光标 + PostMessage（驱动不可用时的兜底）
    #   message      纯 PostMessage 投递（全程不动光标；目标读真实光标时无效，
    #                游戏就是这种 —— 见 branchA 实测：200/200 受理、游戏零响应）
    "input_mode": "interception",
    # 降级策略（2026-11-07 起默认 fail）：
    #   fail    不降级，驱动不通直接失败并在界面报错（默认）
    #   message 降到纯消息投递（不动光标，目标读真实光标时无效）
    #   legacy  降到「SetCursorPos 借光标 + PostMessage」—— 录屏可见、会占
    #           用户的鼠标，只能由用户显式填写，不再是默认兜底
    # 每次降级都会在 input.channel_events() 里留一条（时间/原因/从哪到哪）。
    "input_fallback": "fail",
    # legacy 会 SetCursorPos 借系统光标（录屏可见、占用用户的鼠标）。
    # 老配置里残留 input_fallback=legacy 时，只有把这个开关一起打开才生效；
    # 默认 false = 宁愿显式报错。
    "input_legacy_borrow": False,
    # 纯消息通道严格模式：目标窗口类是 UnrealWindow（游戏按真实光标判定）时，
    # True=按 input_fallback 降级/失败；False=只告警、照常投递（仍逐次校验受理）。
    "input_message_strict": True,
    # 驱动注入的目标设备（2026-11-07 起只允许真实设备）：
    #   0 = 自动：枚举 11..20 槽，按硬件 ID 剔除 VHF/ROOT 虚拟设备；
    #       多只真实鼠标时用 1.2s 被动 Raw Input 活动度挑「你正在用的那只」。
    #   非 0 = 显式钉住某个 device id（例如 14）。
    "input_mouse_device": 0,
    "input_keyboard_device": 0,
    # 虚拟设备（HID\HID_DEVICE_SYSTEM_VHF 之类）默认拒绝：注入到虚拟设备后
    # Raw Input 的 hDevice 归属是一台虚拟设备，正是反作弊判定「不是本机鼠标」
    # 的特征。只有调试/无真实鼠标的环境才应该打开。
    "input_allow_virtual": False,
    # 多只真实鼠标时被动活动度观测时长（毫秒）；0 = 不观测，按设备号兜底。
    "input_mouse_activity_ms": 1200,
    # 成交后留一张截图：抢到了什么、花了多少，事后能回看（约 300KB / 张）
    "save_shots": True,
    # 成交截图 + 品牌水印（core/shotproof.py）。全部在后台线程完成，
    # 成交路径只投递任务，不抓屏、不绘图、不写盘。
    #   enabled   总开关（与上面的 save_shots 同时生效）
    #   dir       落盘目录；留空 = data/shots（控制台的 /shots/ 能直接看）
    #   watermark 是否打水印；关掉就只存客户区原图
    #   keep_raw  打水印时是否同时留一份无水印原图（文件名带 _raw）
    #   brand / qgroup / uid  底栏左侧与左上角的展示值，缺省留空按「—」
    #   avatar / logo  资源插槽；留空 = <程序目录>/assets/shot/avatar.png
    "shot": {
        "enabled": True,
        "dir": "",
        "watermark": True,
        "keep_raw": True,
        "brand": "三角洲 · 抢购控制台",
        "qgroup": "",
        "uid": "",
        "avatar": "",
        "logo": "",
    },
    # 界面上填的抢购条件（价格区间 / 限量 / 预算）。存下来是为了
    # 「关掉再打开接着干」：条件不存，重启就得重填一遍。
    "console": {"min_price": 0, "max_price": 0, "quota": 1, "budget": 0},
    # 列表模式（盯交易行搜索结果，谁先符合条件就买谁）
    "list_scan_interval": 0.05,   # 列表没变时多久看一次
    "list_scan_timeout": 0,       # 扫多久放弃，0 = 一直扫
    "list_detail_timeout": 3.0,   # 点进详情页后等购买按钮多久
    "list_max_tries": 20,         # 最多试多少条挂单
    # 当前任务的条件（引擎读它做过滤）。**不要**往这里塞默认值：
    # 每跑一个任务，scheduler 会把整个 active_task 换成那个任务的字段
    # （_engine_task），塞进来的东西跑一次就没了。
    "active_task": {},
    # 三个「任务没写就用它」的全局默认值。界面上的「做工 / 最大磨损 /
    # 挑单顺序」三个开关写这里，任务自己的字段优先。
    "filter_defaults": {"craft": "", "max_wear": 0.0, "sort": "price",
                        # 在售页顶部那两个下拉（界面上可选，见 switches.py）
                        "pub": "只显示公示期",
                        "ons_sort": "默认排序"},
    # 公示期筛选闸门（engine._ons_public_gate）：
    #   public_period=only  严格「只显示公示期」，筛后 0 条 = 公示期无货；
    #   public_fallback     筛后 0 条时是否允许自动退回「不限公示期」，
    #                       默认 False —— 不许蒙混，放宽必须显式打开。
    "filters": {"public_period": "only", "public_fallback": False},
    # ---------------- 开关表（core/switches.py）读的那些 ----------------
    # 每一项都在 switches.SWITCHES 里有对应条目，界面上拨一下就是改这里。
    "semi_mode": False,           # 半自动：走到购买键前停手，不点购买/确认
    # 分辨率基准与容差：换机器/换 DPI 只改这里，判定代码不写死 1920×1080。
    "screen": {"base_size": [1920, 1080], "tolerance": 0.0},
    "preflight_required": True,   # 开抢前必须过检查
    "force_foreground": True,     # 开点前抢前台并等画面刷新
    "record_enabled": True,       # 写 data/records.jsonl
    "countdown_attempts": 3,      # 倒计时模式最多换几件
    # 解锁倒计时短于这个秒数的商品直接跳过：来不及弹确认框，等到了也多半
    # 被别人先买走。0 秒（已经解锁）不受限制，那种可以直接买。
    "countdown_min_lead_s": 6,
    "stop_after_hit": False,      # 抢到就停整条队列
    "stop_after_fails": 0,        # 连续失败几次停（0=不停）
    # 关注（交易行详情面板顶部的星标，见 说明.txt 第十一节）
    "follow_before_buy": False,
    "follow_after_hit": False,
    "follow_retry": 2,
    "follow_wait_ms": 450,
    "follow_skip_done": True,
    # 开点节奏
    "fire_delay_ms": 0,           # 解锁后再等多久开点
    "fire_jitter_ms": 0,          # 额外随机抖动上限
    "fire_adaptive": True,        # 按上一轮实测耗时自动调延迟
    "fire_verify": True,          # 必须看到结算弹窗才算成交
    # 休市截前（05:00-08:00 专用）
    "premarket": {"enabled": False, "hotkey": "F10"},
    # 全局热键停止。鼠标甩左上角在游戏全屏时不一定灵，键盘更可靠。
    # spec/enabled = 停止；start_spec/start_enabled = 开抢（热键跳过界面但不跳过检查）
    "hotkey": {"enabled": True, "spec": "F8",
               "start_enabled": True, "start_spec": "F7"},
    # 校时
    "time_sync": {
        "enabled": True,
        "servers": ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"],
        "samples": 4,
    },
    # 队列
    "queue": [],            # [{id, name, regions, templates_dir, fire_at, status, note}]
    # 在售页筛选面板的几何（1920×1080 基准坐标）。
    # 两列复选框的列心不再写死在 nav_ons 里：由面板左右边界 + 列宽推导；
    # 这里只放边界，以及推导不出时的兜底标定（review R5）。
    "nav": {
        "ons_panel": [1360, 1900],      # 面板左右边界
        "ons_columns": [1396, 1641],    # 兜底列心
    },

    # OCR（见 core/ocr.py）。2026-11-06 起主路径换成进程内 RapidOCR：
    # 两块基准集全对（36/36、42/42）、离线、冷启动 1.2s、模型 30MB 随仓库走；
    # PP-OCRv6 服务退成「价格二次确认」的第二级，自建字模是第三级。
    "ocr": {
        "rapid": {
            "enabled": True,                  # false = 退回旧的纯 PP-OCR 服务路径
        },
        "ppocr": {
            "mode": "auto",                   # auto / http / off
            "url": "http://127.0.0.1:8081",   # paddlex --serve --pipeline tools/OCR_tiny.yaml
            "timeout_s": 5.0,
            "cache": 64,                      # 同图缓存条数
            "max_side": 2560,                 # 超过就先缩图再发
            "min_score": 0.5,                 # 低于这个置信度的行丢掉（与 ocr_ppocr 默认一致）
            "model": "PP-OCRv6 tiny",         # 只用于自检显示，不影响请求
            "pipeline": "tools/OCR_tiny.yaml",  # 服务端流水线；换 OCR_game.yaml 用 small
            "cooldown_s": 30.0,
            "fail_limit": 3,                  # 连续失败几次就长时间停用
            "long_cooldown_s": 600.0,
        },
        # OCR 服务的进程管家（core/ocr_service.py）：定时/变慢/挂掉时自动重启，
        # 蓝绿双实例，切换时调用方一次都不会失败。
        "service": {
            "enabled": True,
            "restart_hours": 6.0,             # 0 = 不定时重启
            "slow_ms": 800.0,                 # 最近中位耗时超它就算变慢，0 = 不管
            "check_interval_s": 60.0,
            "idle_wait_s": 600.0,             # 等引擎空闲最多等多久
            "healthy_timeout_s": 150.0,       # 新实例验活超时（要加载模型）
            "start_on_boot": True,            # 启动时没有服务就自己拉一个
            "kill_adopted": True,             # 重启时把接管来的老实例也收掉
            # 二级服务（8081 PP-OCRv6）只是价格二次确认的加速通道，主路径是
            # 进程内 RapidOCR。optional=true 时它起不来就安静降级：一条日志，
            # 之后每 retry_s 秒最多静默重试一次，不会逐轮刷屏。
            "optional": True,
            "retry_s": 600.0,
            "flap_s": 600.0,                  # 起不到这么久又挂 = 反复退出
        },
    },
    # ==== FEATURE-CONFIG BEGIN ====
    # ---- feature:buy.followup BEGIN ----
    # 假购买补刀：开火超时没等到确认弹窗时，在同一购买点再补 1 次点击。
    # 默认关 —— 抽签模型下收益不确定，还可能双买。
    "buy": {
        "followup": {"enabled": False},
        # ---- feature:buy.skip_late BEGIN ----
        # 来不及跳过：首次读到这件只剩不到 threshold_s 秒就不进场，
        # 不点购买、不占一次尝试。0 = 关闭。
        "skip_late": {"threshold_s": 2},
        # ---- feature:buy.skip_late END ----
    },
    # ---- feature:buy.followup END ----
    # ---- feature:scan.repeat BEGIN ----
    # 每件随机刷新次数的区间（次数，不是秒）。间隔走既有 list_scan_interval。
    # 默认 1/1 = 等价现状（没货只刷一次）；显式调大 max 才启用随机区间。
    "scan": {"repeat": {"min": 1, "max": 1}},
    # ---- feature:scan.repeat END ----
    # ---- feature:mouse.human BEGIN ----
    # 拟人鼠标轨迹（规划层 core/humanmouse.py）。只在会移动真实光标的通道
    # （interception / legacy）生效；message 通道不动光标，开了也不生效，
    # 引擎会在 stats.trajectory.effective 里如实标 degraded。
    # 合并提示：mouse.idle 也挂在 "mouse" 下，拼接时要把两个子键并进同一个
    # dict，别写两份 "mouse" 字面量（后一份会把前一份吃掉）。
    "mouse": {
        "human": {
            "traj": "auto",       # auto / windmouse / bezier / pack
            "move_ms": 120,       # 一次移动到落点的总时长（毫秒）
            "curve": 0.35,        # 贝塞尔控制点偏移幅度（0~1）
        },
        # ---- feature:mouse.idle BEGIN ----
        # 空闲漫游：倒计时等待期在客户区内只移动 / 移动+滚轮；
        # 开点前 return_s 秒归位，全程不点击，移动走拟人轨迹。
        "idle": {
            "mode": "off",          # off / move / move_wheel
            "interval_ms": 1200,    # 两次漫游动作之间的间隔
            "return_s": 2,          # 开点前多少秒必须归位
            "min_remain_s": 8,      # 剩余大于该值才允许开始漫游
            "wheel_notches": 3,     # 移动+滚轮模式每步滚几格
        },
        # ---- feature:mouse.idle END ----
    },
    # ---- feature:mouse.human END ----
    # ---- feature:click.burst BEGIN ----
    # 连点参数：抽签模型下只缩短「没中之后再次进池」的等待，不提高中签概率。
    # append 保持原 click_hz 节奏；burst 开点瞬间按「按住 hold_ms、间隔
    # interval_ms」反复点，window 用尽或弹窗出现立刻回到 click_hz。
    "click": {"burst": {"mode": "append", "hold_ms": 100,
                        "interval_ms": 5, "window": "all"}},
    # ---- feature:click.burst END ----
    # ---- feature:stop.by.grade BEGIN ----
    # 按品阶分别计数停止：传说 / 紫(史诗) / 蓝(稀有)；0 = 不限；
    # 普通不参与计数，品阶读不出的件也不计数（契约 §2.5）。
    # 合并提示：stop 下目前只有 by_grade 一个子键，后续若有 stop.* 别的功能
    # 请并进同一个 "stop" dict，别写两份字面量。
    "stop": {"by_grade": {"enabled": False, "legend": 0, "epic": 0,
                          "rare": 0, "scope": "all"}},
    # ---- feature:stop.by.grade END ----
    # ==== FEATURE-CONFIG END ====
}


# 可重入：load/save 内部也拿它，transaction 里再调 save 不会自己把自己锁死
_save_lock = threading.RLock()

# ---------------- 跨进程互斥（审查 R13-03） ----------------
# 线程锁只管同进程。两个进程各自 load→改→save 时，后写的照样整份盖掉
# 先写的 —— 实测 6 进程 30 次写只活下来 7 次，而且全部返回成功。
# 这里叠一层操作系统级文件锁：Windows 用 msvcrt.locking，POSIX 用 flock。
# 等不到锁就抛 FileLockTimeout（OSError 子类）—— 显式失败，绝不假装写成功。
PROC_LOCK_TIMEOUT = 15.0          # 秒

try:
    import msvcrt                 # Windows
except ImportError:               # pragma: no cover - 非 Windows
    msvcrt = None
try:
    import fcntl                   # POSIX
except ImportError:               # pragma: no cover - Windows
    fcntl = None


class FileLockTimeout(OSError):
    """等别的进程释放文件锁超时。调用方应当按「本次失败」上报。"""


@contextlib.contextmanager
def file_lock(path, timeout=None, poll=0.002):
    """在 `path` 上做跨进程互斥。

    `path` 是专用的锁文件（不能拿被保护的数据文件本身当锁：Windows 上
    msvcrt.locking 锁的是字节区间，和读写同一文件会互相干扰）。锁文件
    随进程退出由操作系统释放，崩溃不会留下死锁。

    重试间隔从 2ms 起指数退避到 20ms：多个进程高频抢锁时，固定 50ms
    会让 800 次写入拖成几十秒。
    """
    if timeout is None:
        timeout = PROC_LOCK_TIMEOUT
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    f = open(path, "a+b")
    try:
        deadline = time.monotonic() + float(timeout)
        delay = poll
        if msvcrt is not None:
            # _locking 锁的是「从当前文件位置起的 n 字节」，先保证文件有 1 字节
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"\0")
                f.flush()
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise FileLockTimeout(
                            f"等锁超时（{timeout}s）：{path}")
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.02)
        elif fcntl is not None:   # pragma: no cover - 非 Windows
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise FileLockTimeout(
                            f"等锁超时（{timeout}s）：{path}")
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.02)
        # else: 两套机制都没有 —— 退化成无锁（只可能是非常规解释器），
        # 不抛异常把整个保存搞挂。
        try:
            yield
        finally:
            try:
                if msvcrt is not None:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:   # pragma: no cover
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        f.close()


# 同一进程内嵌套调用只加一次文件锁（save 里调 _save_locked 是常见路径）。
# 只在持有 _save_lock 时读写，所以不需要额外的线程同步。
_proc_state = {"depth": 0}


@contextlib.contextmanager
def _proc_lock(timeout=None):
    """跨进程互斥的可重入壳。前提：调用方已持有 `_save_lock`。"""
    if _proc_state["depth"]:
        _proc_state["depth"] += 1
        try:
            yield
        finally:
            _proc_state["depth"] -= 1
        return
    cm = file_lock(CONFIG_PATH + ".lock", timeout=timeout)
    cm.__enter__()
    _proc_state["depth"] = 1
    try:
        yield
    finally:
        _proc_state["depth"] = 0
        cm.__exit__(None, None, None)


def _remove_quietly(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _replace_with_retry(src, dst, attempts=7):
    """`os.replace` 在目标正被别的线程打开着读时会抛 PermissionError
    （Windows 共享冲突）。重试几次；其它 OSError（满盘等）立即抛。"""
    delay = 0.02
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i >= attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _stat_sig():
    """只 stat，不读内容：界面轮询用的廉价指纹。

    `_disk_sig` 每次都要把整份 config.json 读一遍算 sha1。轮询 1s 一次、
    还可能在保存刚结束时调用，读内容本身就可能撞上 os.replace 后的
    磁盘/杀软扫描。轮询只用来判断「要不要重读」，mtime_ns + 字节数足够；
    同尺寸同 mtime 的极端情况由 `load_cached(max_age=...)` 的过期兜底。
    """
    try:
        st = os.stat(CONFIG_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# 界面轮询的配置快照。只由 `load_cached()` 与保存成功后的钩子更新。
_POLL_CACHE = {"sig": None, "cfg": None, "at": 0.0}


def _disk_sig():
    """盘上 config.json 的指纹（mtime_ns + 字节数 + 内容哈希）。

    只用于「这份配置被外部改过没有」。以前只有 mtime+size：备份还原、
    `copy /b`、某些同步盘把外部改动写回来时 mtime/size 可能一模一样，
    指纹相同 → 合并被跳过 → 用户手改的值被内存里的旧快照盖掉（审查 P11）。
    配置只有几 KB，每次多读一遍算 sha1 是微秒级，不值得再维护缓存表。
    """
    try:
        st = os.stat(CONFIG_PATH)
        with open(CONFIG_PATH, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, hashlib.sha1(raw).hexdigest())


def _decode_config(raw):
    """识别 config.json 的真实编码，返回 `(文本, 编码名)`。

    以前固定 `encoding="utf-8"`：用户用记事本/某些编辑器存成「UTF-8 带
    BOM」或 UTF-16 时，解析直接 ValueError → 被当成坏文件隔离、再拿旧
    `.good` 覆盖 —— 用户刚改的配置就这么没了（审查 E1/E1b）。

    顺序：BOM（UTF-8 / UTF-16）→ 无 BOM 的 UTF-16 启发式（JSON 里 ASCII
    为主，UTF-16 编码后几乎每隔一个字节就是 0x00）→ UTF-8。全都解不出来
    就抛 `UnicodeDecodeError`（ValueError 子类），交给上层按坏文件处理。
    """
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    head = raw[:64]
    if head and head.count(0) >= max(4, len(head) // 4):
        odd = sum(1 for i in range(1, len(head), 2) if head[i] == 0)
        even = sum(1 for i in range(0, len(head), 2) if head[i] == 0)
        codec = "utf-16-le" if odd >= even else "utf-16-be"
        return raw.decode(codec), "utf-16"
    return raw.decode("utf-8"), "utf-8"


def _sanitize_tree(node, ref=None):
    """把配置树里的数值/字符串净化到能安全 `float()`、能安全写 JSON 的范围。

    手改的 config.json 里写进 `10**400` 时，`switches.get/snapshot` 的
    `float(value)` 会抛未捕获 `OverflowError`（那个模块不在本次改动范围），
    所以在这里 —— **加载入口** —— 就把无法表示的数退回同一路径的默认值
    （审查 N9/N12）。顺带把 NaN/Infinity 和孤代理字符也清掉，否则保存时
    会炸在 json/utf-8 编码上。
    """
    if isinstance(node, dict):
        for k in list(node):
            r = ref.get(k) if isinstance(ref, dict) else None
            node[k] = _sanitize_tree(node[k], r)
        return node
    if isinstance(node, list):
        for i, v in enumerate(node):
            r = ref[i] if isinstance(ref, list) and i < len(ref) else None
            node[i] = _sanitize_tree(v, r)
        return node
    if isinstance(node, float):
        if not math.isfinite(node):
            return ref if isinstance(ref, (int, float)) and not isinstance(ref, bool) else 0.0
        return node
    if isinstance(node, int) and not isinstance(node, bool):
        try:
            float(node)
        except OverflowError:
            return ref if isinstance(ref, (int, float)) and not isinstance(ref, bool) else 0
        return node
    if isinstance(node, str):
        try:
            node.encode("utf-8")
        except UnicodeEncodeError:
            return node.encode("utf-8", "replace").decode("utf-8")
    return node


# 孤儿临时文件：崩溃/强杀会留下 config.json.<pid>.<tid>.tmp 之类。
# 不清理的话它们会一直在程序目录里堆着（审查 R13-08b）。
_TMP_RE = re.compile(r"^config\.json(?:\.good)?\.(\d+)\.(\d+)\.tmp$")
_cleanup_at = {}


def _cleanup_orphan_tmp():
    """删掉旧的临时文件。只删「明显不是活跃写」的：mtime 超过 60s 的才动 ——
    正在写的那个 mtime 是刚刚，别的进程写大配置也可能花几秒，不能误删
    （误删会让对方的 os.replace 直接失败）。"""
    now = time.time()
    last = _cleanup_at.get(CONFIG_PATH)
    if last is not None and now - last < 10:
        return 0
    _cleanup_at[CONFIG_PATH] = now
    d = os.path.dirname(CONFIG_PATH) or "."
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    removed = 0
    for name in names:
        m = _TMP_RE.match(name)
        if not m:
            continue
        p = os.path.join(d, name)
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if age < 60:
            continue
        if _remove_quietly(p):
            removed += 1
    if removed:
        print(f"[config] 清理了 {removed} 个遗留临时文件")
    return removed


def _deep_merge(base, incoming):
    """按字段合并字典：用户只写了一半的节（console/nav/screen…）不能把
    默认子键整节抹掉，否则回退老版本读 `console["quota"]` 直接 KeyError
    （审查 R13-06）。"""
    for k, v in incoming.items():
        cur = base.get(k)
        if isinstance(v, dict) and isinstance(cur, dict):
            _deep_merge(cur, v)
        else:
            base[k] = v


def _task_key(task):
    """队列里按任务 id 认身份；没有 id 的脏数据退回整条 JSON 比对。"""
    tid = task.get("id") if isinstance(task, dict) else None
    if tid:
        return ("id", str(tid))
    return ("raw", json.dumps(task, sort_keys=True, ensure_ascii=False))


def _same_task(a, b):
    try:
        return (json.dumps(a, sort_keys=True, ensure_ascii=False)
                == json.dumps(b, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError):
        return a == b


def _merge_queue(ours, base, theirs):
    """按**任务**粒度做三方合并，而不是把整段 queue 当成一个值。

    以前 queue 整份取本地：另一个进程（或用户手改 config.json）新加的
    任务会在 save() 时被静默丢掉（审查 F3）。规则：
      · 本地改过 / 本地新加的          → 留本地
      · 本地没动、盘上改过            → 用盘上的
      · 本地没动、盘上删了            → 跟着删
      · 盘上新加的                    → 追加到末尾
    """
    ours = ours if isinstance(ours, list) else []
    base = base if isinstance(base, list) else []
    theirs = theirs if isinstance(theirs, list) else []
    base_by = {_task_key(t): t for t in base if isinstance(t, dict)}
    theirs_by = {_task_key(t): t for t in theirs if isinstance(t, dict)}
    out, seen = [], set()
    for t in ours:
        if not isinstance(t, dict):
            continue
        k = _task_key(t)
        seen.add(k)
        b, ext = base_by.get(k), theirs_by.get(k)
        if ext is None:
            if b is not None and _same_task(t, b):
                continue                    # 外部删了这条、本地没动 → 跟着删
        elif b is not None and _same_task(t, b):
            t = ext                         # 本地没动 → 用盘上的新版本
        out.append(t)
    for t in theirs:
        if not isinstance(t, dict):
            continue
        k = _task_key(t)
        if k in seen:
            continue
        seen.add(k)
        if base_by.get(k) is None:          # 盘上新加的
            out.append(t)
    return out


# 任务 id 的形态（scheduler 用 uuid4().hex[:8] 生成）
_TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# 抢购路线的唯一枚举。`workflow` 是**唯一真源**，`mode`（list/detail）
# 只是它的派生视图 —— 以前两个字段各自为政，`workflow:"countdown"` +
# `mode:"list"` 的任务会让前置检查去看列表区域（审查 S7）。
WORKFLOWS = ("countdown", "list", "detail")
TRIGGER_MODES = ("vanish", "appear")
# 输入通道与降级策略（core/input.py 的正式三选一 + 兜底目标）
INPUT_MODES = ("interception", "legacy", "message")
INPUT_FALLBACKS = ("legacy", "message", "fail")


def _normalize_choice(value, allowed, fallback):
    """枚举归一：大小写/空白不敏感，不在 `allowed` 里就退回 fallback。"""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in allowed:
            return v
    return fallback


def normalize_input_mode(value, fallback="interception"):
    """输入通道归一：interception / legacy / message，非法值退回 fallback。"""
    return _normalize_choice(value, INPUT_MODES, fallback)


def normalize_input_fallback(value, fallback="fail"):
    """降级策略归一：fail / message / legacy。

    默认 fail：宁可显式失败，也不悄悄换到会借系统光标的 legacy。
    """
    return _normalize_choice(value, INPUT_FALLBACKS, fallback)


class ConfigLoadError(RuntimeError):
    """config.json 读不出来（被占用 / 权限 / 一直是坏文件）。

    抛这个而不是退回默认值：`transaction()` 一退就会 `_save_locked()`，
    拿默认值回写等于把用户的框选 / 队列 / 阈值整份抹掉，不可逆（审查 S1）。
    """


def normalize_workflow(value, fallback="countdown"):
    """把任意输入归一成合法路线；不认识就退回 fallback。"""
    return _normalize_choice(value, WORKFLOWS, fallback)


def mode_for_workflow(workflow):
    """workflow → 引擎的 mode（list / detail）。"""
    return "list" if normalize_workflow(workflow) == "list" else "detail"


def _legacy_workflow(mode):
    """老任务的 `mode` 字段迁移成路线；不是 list/detail 就返回空串。

    历史遗留：`mode` 只是 workflow 的派生视图，这里只做单向迁移，不做
    strip/lower —— 老数据里写的值都是精确的小写字面量。
    """
    return mode if mode in ("list", "detail") else ""


def task_workflow(task, fallback="countdown"):
    """任务走哪条路线。workflow 优先；老任务没有它时按 mode 迁移。"""
    task = task or {}
    wf = normalize_workflow(task.get("workflow"), "")
    if wf:
        return wf
    return _legacy_workflow(task.get("mode")) or normalize_workflow(fallback)


def _nonneg_int(value, default=0):
    """非负整数归一（设备号 / 毫秒这类手改参数）：非法值退回 default。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self, data=None):
        self.data = json.loads(json.dumps(DEFAULTS))
        # 读盘失败时这里记一句原因；正常读出来的配置永远是 None。
        # `load_ok` / `last_error` 就是靠它区分「用户配置就是空队列」
        # 和「config.json 根本没读出来」——以前两者长得一模一样。
        self._load_error = None
        # 数据救回来了、但发生过损坏：只是一句提示，**不影响可用性**。
        # 以前把「已从备份恢复」也写进 _load_error，结果 load_ok=False ——
        # 调度器拒启动、孤儿任务不复位、任务目录不清理（审查 F1）。
        self._notice = None
        # 合并前的原始输入。迁移 workflow 必须看「用户到底写没写 workflow」，
        # 不能看合并后的默认值 —— 否则 mode="list" 的老配置会被默认的
        # countdown 顶掉，升级后行为静默改变（审查 R13-05）。
        self._raw = data if isinstance(data, dict) else None
        # 本对象改过哪些顶层键。save() 撞上外部改动时做三方合并用（R13-09）。
        self._dirty = set()
        # 读盘时的指纹与基线快照：判断盘上内容是否被外部改过。
        self._disk_sig = None
        self._loaded_data = None
        if data:
            self._merge(data)
        self._normalize()
        self._loaded_data = json.loads(json.dumps(self.data))
        self._ensure_dirs()

    def _ensure_dirs(self):
        """建 templates/ 和 data/。

        以前直接 `os.makedirs(..., exist_ok=True)`：路径上已经有个同名
        **文件**、或 ACL 拒绝读取时，Windows 抛 `[WinError 183] 文件已存在`
        —— 文案完全指不到真正原因（审查 R13-10d），排查全靠猜。
        """
        for d in (TEMPLATE_DIR, DATA_DIR):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                raise OSError(
                    f"无法使用目录 {d}：同名路径已存在但不是目录，"
                    f"或没有读写权限（原始错误：{e}）") from e

    def _normalize(self):
        """加载期归一：非法值不留在内存里，交给下游一个自洽的配置。

        三件事（审查 S6/S7/S25）：
          · `trigger_mode` 只允许 vanish/appear，仓库里那个 "countdown"
            会被引擎当成 appear（>vanish）又按 vanish 记日志 —— 统一成 vanish；
          · `workflow` 是唯一真源，`mode` 由它派生；老配置只有 `mode` 时
            迁移成 workflow；
          · 队列里每条任务同样归一，并把 `fire_at` 变成数字（`pending.sort`
            以前直接 `t["fire_at"]`，缺字段就 KeyError 打死调度线程）。
        """
        d = self.data
        # 老配置里的演练开关已废弃：静默丢弃，不报错也不写回。
        d.pop("run_mode", None)
        d.pop("dry_run", None)
        trig = d.get("trigger_mode")
        d["trigger_mode"] = trig if trig in TRIGGER_MODES else "vanish"
        d["input_mode"] = normalize_input_mode(d.get("input_mode"))
        d["input_fallback"] = normalize_input_fallback(d.get("input_fallback"))
        if not isinstance(d.get("input_message_strict"), bool):
            d["input_message_strict"] = True
        for key in ("input_mouse_device", "input_keyboard_device"):
            d[key] = _nonneg_int(d.get(key), 0)
        d["input_mouse_activity_ms"] = _nonneg_int(
            d.get("input_mouse_activity_ms", 1200), 1200)
        if not isinstance(d.get("input_allow_virtual"), bool):
            d["input_allow_virtual"] = False

        # workflow 是唯一真源。**只有原始输入真的写了 workflow 才认它**：
        # `_merge` 之后 d["workflow"] 永远是默认的 countdown，拿它判断会把
        # 只写了 mode="list" 的老配置静默改成 countdown（审查 R13-05）。
        raw = self._raw or {}
        # 「用户显式选过 workflow」有两个来源：盘上那份写了，或本对象在内存里
        # 改过（_dirty）。只看盘上那份会在外部合并之后把内存里的选择顶掉。
        had_wf = ("workflow" in self._dirty
                  or normalize_workflow(raw.get("workflow"), "") != "")
        wf = normalize_workflow(d.get("workflow"), "") if had_wf else ""
        if not wf:
            legacy = raw.get("mode") if self._raw is not None else None
            # 推导不出来（老配置连 mode 都没有）就保留默认行为，
            # 不能拿「默认值」当「用户选择」。
            wf = _legacy_workflow(legacy) or normalize_workflow(d.get("workflow"))
        d["workflow"] = wf
        d["mode"] = mode_for_workflow(wf)

        if not isinstance(d.get("queue"), list):
            d["queue"] = []
        d["queue"] = [t for t in d["queue"] if isinstance(t, dict)]
        if not isinstance(d.get("regions"), dict):
            d["regions"] = _region_table.clone()
        # 老配置（regions 为 null / 缺键 / 手改坏）一律回落到内置表。
        # 只保留 9 个正式区域，历史遗留的 list_refresh 等键不再写入。
        old = d["regions"]
        d["regions"] = {}
        for name, default in _region_table.BUILTIN.items():
            box = old.get(name)
            ok = isinstance(box, dict) and all(
                isinstance(box.get(k), (int, float))
                and not isinstance(box.get(k), bool)
                and math.isfinite(float(box.get(k)))
                for k in ("x", "y", "w", "h"))
            if ok and box["w"] > 0 and box["h"] > 0:
                d["regions"][name] = {k: float(box[k]) for k in ("x", "y", "w", "h")}
            else:
                d["regions"][name] = dict(default)
        if not isinstance(d.get("calibration"), dict):
            d["calibration"] = json.loads(json.dumps(DEFAULTS["calibration"]))
        else:
            # 老配置没有 proposed/applied/anchors 等键：补齐，避免界面 KeyError。
            for k, v in DEFAULTS["calibration"].items():
                if k not in d["calibration"]:
                    d["calibration"][k] = json.loads(json.dumps(v))
        if not isinstance(d.get("console"), dict):
            d["console"] = json.loads(json.dumps(DEFAULTS["console"]))
        if not isinstance(d.get("filter_defaults"), dict):
            d["filter_defaults"] = json.loads(json.dumps(DEFAULTS["filter_defaults"]))
        # 结构就位之后统一净化数值：超大整数 / NaN / 孤代理字符不进内存
        d = _sanitize_tree(d, DEFAULTS)

        for t in d["queue"]:
            # 路线来源（审查 R3）：以前这里无条件把每条任务的 workflow 固化成
            # 当时的全局值，用户切换全局路线后，已排队任务永远走旧路线，且
            # 界面上看不出任何差别。现在区分三种来源：
            #   task   任务自己选过路线 —— 不跟着全局变，但标 route_stale；
            #   global 任务从没选过   —— 每次加载按**当前全局**重新派生，跟随；
            #   frozen 已经开跑/结束   —— 保持当时那条，半路不换图，标 stale。
            src = t.get("workflow_source")
            explicit = t.get("workflow_explicit")
            if explicit is None:
                # 老任务没有标记：盘上写了 workflow 就算用户选过（保守，不静默改）
                explicit = (src != "global"
                            and bool(normalize_workflow(t.get("workflow"), "")))
                t["workflow_explicit"] = explicit
            status = str(t.get("status") or "").lower()
            unstarted = status in ("", "pending", "armed")
            if explicit:
                twf = normalize_workflow(t.get("workflow"), "") or wf
                source = "task"
            elif unstarted:
                twf = wf
                source = "global"
            else:
                twf = (normalize_workflow(t.get("workflow"), "")
                       or normalize_workflow(t.get("workflow_effective"), "") or wf)
                source = "frozen"
            t["workflow"] = twf
            t["mode"] = mode_for_workflow(twf)
            t["workflow_source"] = source
            t["workflow_effective"] = twf
            # 与当前全局不一致 = 用了旧路线；界面上要显式标出来（不静默）
            t["route_stale"] = bool(twf != wf)
            try:
                t["fire_at"] = float(t.get("fire_at") or 0)
            except (TypeError, ValueError, OverflowError):
                t["fire_at"] = 0.0
            if not isinstance(t.get("regions"), dict):
                t["regions"] = {}
            if not isinstance(t.get("status"), str) or not t.get("status"):
                t["status"] = "pending"

    def _merge(self, data):
        # 和 __init__ 里 `self._raw = data if isinstance(data, dict) else None`
        # 对称：非 dict 输入（顶层是数组/字符串/数字的坏 JSON 被直接塞进
        # 构造器）一律忽略，退回 DEFAULTS，而不是 data.items() 抛
        # AttributeError 把构造打穿。读盘主路径 _load_locked 另有
        # isinstance 校验（坏文件会被隔离），这里兜的是绕过它的调用方
        # （portal / soak 把读来的 JSON 原样传进来）。
        if not isinstance(data, dict):
            return
        for k, v in data.items():
            cur = self.data.get(k)
            if k == "regions" and isinstance(v, dict) and isinstance(cur, dict):
                # 区域名白名单不变（瞎写的区域名下游不认），但已有的区域按值合并
                self.data["regions"].update(
                    {n: b for n, b in v.items() if n in cur})
            elif k == "queue" and isinstance(v, list):
                self.data["queue"] = v
            elif isinstance(v, dict) and isinstance(cur, dict):
                # 按字段合并，不做整节替换：console / nav / screen /
                # filter_defaults / premarket / hotkey / ocr 这些节里，用户
                # 只写了一部分字段时，剩下的默认子键必须留着 —— 否则
                # 老版本读 console["quota"]、screen["tolerance"] 直接
                # KeyError（审查 R13-06）。
                _deep_merge(cur, v)
            else:
                self.data[k] = v

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self._dirty.add(key)

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def load_ok(self):
        """这份配置是不是**可用**（真的从盘上读出来，或从备份救回来）。

        False = config.json 被占用/权限不足，手上这份是 DEFAULTS。
        调度器必须按「读失败」处理：保持队列、别推进状态机 ——
        否则空队列会被当成「跑完了」直接收工（review R1）。

        注意：从 `.good` 备份恢复成功也算 True —— 数据是用户的、能跑；
        损坏这件事放在 `notice` 里提示，不影响调度（审查 F1）。
        """
        return self._load_error is None

    @property
    def notice(self):
        """救回数据时的提示（没发生任何事时是 None）。"""
        return self._notice

    @property
    def last_error(self):
        """读盘失败的原因（没失败时是 None）。"""
        return self._load_error

    def region(self, name):
        """当前生效的归一化区域。

        自带内置默认（`region_table.BUILTIN`），所以老配置没有该键时
        返回的是能直接用的坐标，而不是 None —— 引擎/自检不再因为
        「还没框选」拦下买家。
        """
        box = self.data["regions"].get(name)
        if isinstance(box, dict):
            return box
        default = _region_table.BUILTIN.get(name)
        return dict(default) if default else None

    def region_raw(self, name):
        """只返回盘上/内存里存的值，不做内置兜底（界面判断来源用）。"""
        return self.data["regions"].get(name)

    def set_region(self, name, box):
        if name in _region_table.BUILTIN and isinstance(box, dict):
            box = {k: float(box[k]) for k in ("x", "y", "w", "h")}
        self.data["regions"][name] = box
        self._dirty.add("regions")

    def save(self):
        """原子写：先写临时文件再 os.replace。"""
        with _save_lock:
            with _proc_lock():
                self._maybe_merge_external()
                self._save_locked()

    def _maybe_merge_external(self):
        """盘上配置被外部改过时，做三方合并再写。

        以前 `cfg.load()` 之后长时间握着对象、回来直接 `save()`，中间用户
        手改了 config.json（或另一个旧版本进程改过）→ 整份快照把它盖掉，
        无声无息（审查 R13-09）。现在：盘上指纹变了就先重读，按顶层键
        做 ours/base/theirs 合并 —— 本对象没碰过的字段一律采用盘上的新值。
        """
        if self._disk_sig is None or self._loaded_data is None:
            return
        cur = _disk_sig()
        if cur is None or cur == self._disk_sig:
            return
        fresh, ok = self._load_locked()
        if not ok:
            return
        base = self._loaded_data
        ours = self.data
        merged = {}
        for k in set(base) | set(ours) | set(fresh.data):
            if k == "queue":
                # 队列按任务合并：整段取本地会把别的进程新加的任务丢掉（F3）
                merged[k] = _merge_queue(ours.get(k), base.get(k), fresh.data.get(k))
                continue
            if (k in ours
                    and (k in self._dirty
                         or k not in base
                         or ours.get(k) != base.get(k))):
                merged[k] = json.loads(json.dumps(ours[k]))
            elif k in fresh.data:
                merged[k] = json.loads(json.dumps(fresh.data[k]))
        self.data.clear()
        self.data.update(merged)
        # 合并后的「原始输入」以盘上那份为准：否则下一次 _normalize 会拿
        # 旧文件的 mode 去推导 workflow，把盘上显式写的 workflow 覆盖掉。
        self._raw = fresh._raw
        self._normalize()
        print("[config] 盘上配置被外部改过，已按字段合并后保存")

    def _save_locked(self):
        """真正的写盘。调用方必须已经持有 _save_lock。

        为什么不能直接 open(CONFIG_PATH, "w")：
          · 它先截断再写。写到一半进程被杀（抢购中强退、蓝屏）→ 文件是半截
            JSON，下次启动读不出来。
          · 服务端线程（保存开关）和调度器线程（每个任务状态变化都 save）
            会并发写同一个文件。两边同时截断/写，最后落盘的是两份内容交错
            的垃圾。

        三个硬性约束（审查 R13-01/03/07）：
          · 写失败（满盘 / 只读）**不动原文件**，直接抛 OSError —— 以前
            兜底直写会把 config.json 写成 0 字节。
          · 整段在跨进程文件锁里，两个进程并发写不会互相覆盖。
          · `.good` 备份也是「临时文件 + 原子替换 + 读回校验」，绝不留半份。
        """
        with _proc_lock():
            payload = json.dumps(self.data, ensure_ascii=False,
                                 indent=2).encode("utf-8")
            self._write_payload(payload)
            self._write_backup(payload)
            self._disk_sig = _disk_sig()
            self._loaded_data = json.loads(json.dumps(self.data))
            self._dirty.clear()
            # 保存成功后立刻刷新轮询快照：否则同一个进程里刚改完配置，
            # 界面下一次轮询还会拿到 2s 内的旧快照（队列/开关看起来没生效）。
            _POLL_CACHE.update(sig=_stat_sig(), cfg=self, at=time.monotonic())

    @staticmethod
    def _write_payload(payload):
        """把 payload 原子地写到 CONFIG_PATH。失败抛 OSError，原文件保持不变。"""
        tmp = f"{CONFIG_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, CONFIG_PATH)
        except OSError:
            _remove_quietly(tmp)
            raise

    @staticmethod
    def _write_backup(payload):
        """`.good` 备份：写临时文件 → 读回逐字节校验 → 原子替换。

        以前是 `open(.good, "wb")` 直接覆盖，写到一半被强杀就是半份 JSON；
        下一次配置一坏，恢复逻辑把半份备份当正常配置用，4.19MB 变 2.6KB，
        区域与队列全丢（审查 R13-01）。备份失败**不影响**本次保存，但也不能
        把上一份还能用的 `.good` 弄坏。
        """
        good = CONFIG_PATH + ".good"
        tmp = f"{good}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # 完整性校验：长度 + 内容（读回来必须与刚写的字节完全一致，
            # 且能解析成非空 JSON 对象）
            with open(tmp, "rb") as f:
                got = f.read()
            if len(got) != len(payload) or got != payload:
                raise OSError("备份完整性校验失败（长度或内容不一致）")
            if not isinstance(json.loads(got.decode("utf-8")), dict):
                raise OSError("备份内容不是 JSON 对象")
            _replace_with_retry(tmp, good)
        except (OSError, ValueError) as e:
            _remove_quietly(tmp)
            print(f"[config] 备份 {os.path.basename(good)} 未更新（{e}），"
                  f"保留上一份可用备份")

    @classmethod
    def load(cls):
        """读配置。整个过程持有 _save_lock + 跨进程锁，和 save 串行化。

        不串行的话：save 正在 os.replace，这边正好 open 同一个路径，
        Windows 会抛 PermissionError（共享冲突）。

        跨进程锁（审查 R1）：读路径也会**写盘**（隔离坏文件、从 .good 恢复、
        自愈回写）。不加锁的话，A 刚把坏 config.json 隔离走、B 正好进来
        看到文件不存在 → 走「首启」分支写默认值，把 A 即将恢复的配置盖掉。
        """
        with _save_lock:
            with _proc_lock():
                _cleanup_orphan_tmp()
                cfg = cls._load_locked()[0]
        # OCR 日志的兜底轮转（审查 R5）：服务只在 spawn 时转一次，几天不重启
        # 上限就永远不生效。load() 每个进程都有调用（界面轮询/调度），
        # 10 分钟节流扫一次；失败不影响加载。
        try:
            _retention.sweep_logs(os.path.dirname(os.path.abspath(CONFIG_PATH)))
        except Exception:                    # noqa: BLE001
            pass
        return cfg

    @classmethod
    def load_cached(cls, max_age=2.0, lock_timeout=0.01):
        """界面轮询专用读配置：有内存快照就直接用，绝不长时间等 `_save_lock`。

        调用方只读不要改（快照对象会被多个请求线程共用）。

        为什么不能直接用 `load()`：`load()` 和 `transaction()` 抢同一把
        `_save_lock`，而 `transaction()` 落盘时要做两次 fsync。磁盘/杀软
        忙的时候落盘 130ms+，界面 1s 一次的轮询就被压在锁上 ——
        长时挂机里看到的就是这种单次 185ms 的顿点。

        规则：
          · stat 指纹没变、快照不超过 max_age → 直接返回快照（μs 级）；
          · 盘上改过了 → 真读一次；`_save_lock` / 跨进程锁只等 lock_timeout
            （默认 10ms），等不到就继续用上一份可用快照 —— 这是保存正在
            进行而不是配置坏了，`load_ok` 保持上一次的值，界面不会误报；
          · 从来没有快照（进程刚起）→ 退回阻塞的 `load()`，宁可慢一次也
            不能拿默认值糊弄界面。
        """
        cached = _POLL_CACHE["cfg"]
        sig = _stat_sig()
        if (cached is not None and sig is not None
                and _POLL_CACHE["sig"] == sig
                and time.monotonic() - _POLL_CACHE["at"] < max_age):
            return cached
        if not _save_lock.acquire(timeout=lock_timeout):
            return cached if cached is not None else cls.load()
        try:
            with _proc_lock(timeout=lock_timeout):
                _cleanup_orphan_tmp()
                fresh = cls._load_locked()[0]
                _POLL_CACHE.update(sig=_stat_sig(), cfg=fresh,
                                   at=time.monotonic())
        except FileLockTimeout:
            # 另一个进程正握着跨进程锁写盘：同样是「保存中」，用快照。
            return cached if cached is not None else cls.load()
        finally:
            _save_lock.release()
        try:
            _retention.sweep_logs(os.path.dirname(os.path.abspath(CONFIG_PATH)))
        except Exception:                    # noqa: BLE001
            pass
        return fresh

    @classmethod
    @contextlib.contextmanager
    def transaction(cls):
        """读-改-写 的原子段。退出时落盘（块内抛异常则不落盘）。

            with Config.transaction() as cfg:
                cfg["queue"].append(task)

        为什么需要：调用方各自 load() → 改 → save()，各自持一份全量
        快照，后写的会把先写的整份覆盖 —— 这就是「设置改完自己变回去」
        的根因。整段持锁之后，读到的永远是最新盘上内容。

        **读失败不打盘**（审查 S1）：以前读失败会拿到一份默认值，退出时
        照样 `_save_locked()` —— 被占用/权限问题就变成整份配置被默认值
        覆盖。这里改为抛 ConfigLoadError：调用方报错或跳过本次写入。

        整段还在跨进程文件锁里（审查 R13-03）：两个进程各自事务时，
        读-改-写必须串行，否则后写的整份盖掉先写的。
        """
        with _save_lock:
            with _proc_lock():
                cfg, ok = cls._load_locked()
                if not ok:
                    raise ConfigLoadError(
                        "config.json 读不出来（被占用或权限不足），"
                        f"本次不写盘以免覆盖原文件：{CONFIG_PATH}")
                yield cfg
                cfg._save_locked()

    def reload(self):
        """就地把盘上的内容合并进来（保留对象身份）。

        给长时间运行的循环用：引擎在跑一轮抢购时手上握着 cfg 对象，
        想让用户中途改的筛选条件生效，就得让这个对象的 data 换成最新的。

        **读失败原样保留**（审查 S5）：以前失败会拿到默认值再 update 进来，
        引擎手上的 regions / active_task 被清空，下一轮直接按「没框选」
        报错。失败就什么都不改。
        """
        with _save_lock:
            with _proc_lock():
                fresh, ok = self._load_locked()
            if not ok:
                print("[config] reload 失败，保留当前内存里的配置")
                return self
            self.data.clear()
            self.data.update(fresh.data)
            # 换成了盘上的内容：基线也跟着换，否则下一次 save() 会误判成
            # 「没被外部改过」而把旧快照写回去。
            self._disk_sig = fresh._disk_sig
            self._loaded_data = json.loads(json.dumps(self.data))
            self._dirty.clear()
        return self

    @classmethod
    def _load_locked(cls):
        """真正的读。调用方必须已经持有 _save_lock（所以不能调 save()）。

        返回 `(配置, 是否读到合法配置)`。第二项为 False 时调用方**禁止写盘**。
        """
        if not os.path.isfile(CONFIG_PATH):
            # 真的首启 vs「另一个进程刚把坏文件隔离走」：后者的 .good 还在。
            # 有可用备份就先恢复，绝不拿默认值把刚恢复/备份的配置盖掉
            # （审查 R1：隔离瞬间的「首启」分支会写默认值）。
            if cls._restore_good():
                cfg = cls._restore_good_cfg
                cfg._notice = "config.json 不见了，已从最近一次正常配置备份恢复"
                print(f"[config] {cfg._notice}")
                cls._try_write_back(cfg)
                return cfg, True
            cfg = cls()
            cfg._save_locked()                     # 第一次运行：把默认值落盘
            return cfg, True

        for attempt in range(9):
            try:
                with open(CONFIG_PATH, "rb") as f:
                    raw = f.read()
                text, encoding = _decode_config(raw)
                data = json.loads(text)
                # 顶层不是对象（"abc" / 123 / [...]）同样当坏文件：以前这种
                # 内容进 _merge 会 AttributeError，或者被当成合法配置悄悄
                # 补全默认值，用户看到的是「配置自己重置了」（审查 S1）。
                if not isinstance(data, dict):
                    raise ValueError(f"顶层不是 JSON 对象（{type(data).__name__}）")
                cfg = cls(data)
                # 记录「这份配置是从盘上哪个版本读来的」，save() 靠它发现外部改动
                cfg._disk_sig = _disk_sig()
                cfg._loaded_data = json.loads(json.dumps(cfg.data))
                if encoding != "utf-8":
                    # 解析成功就按统一编码回写：不然用户下次用编辑器再存一次
                    # 又会踩同一个坑（审查 E1/E1b）
                    cfg._notice = f"config.json 是 {encoding} 编码，已转成 UTF-8 保存"
                    print(f"[config] {cfg._notice}")
                    cls._try_write_back(cfg)
                return cfg, True
            except ValueError as e:
                # **只有内容坏了才留档重置**（手改漏个逗号最常见）。
                # 以前这里连 OSError 一起吞，把「文件被占用」也当成「文件坏了」，
                # 会把好好的配置备份走再重置 —— 那是数据丢失。
                backup = cls._quarantine(e)
                # 隔离之后优先用最近一次正常备份恢复。没有备份（第一次运行
                # 就写坏）才退回默认值。
                if cls._restore_good():
                    cfg = cls._restore_good_cfg
                    # 恢复成功 = **加载成功**。数据救回来了就不能算「读不出来」，
                    # 否则调度器拒启动、孤儿任务不复位、任务目录不清理（审查 F1）。
                    cfg._notice = "config.json 损坏，已从最近一次正常配置备份恢复"
                    print(f"[config] {cfg._notice}")
                    cls._try_write_back(cfg)
                    return cfg, True
                cfg = cls()
                cfg._load_error = (f"config.json 损坏（{e}），"
                                   "已隔离并用默认值启动"
                                   + (f"，备份：{os.path.basename(backup)}" if backup else ""))
                cls._try_write_back(cfg)
                return cfg, True
            except OSError:
                # 读不了（被占用/权限）≠ 文件坏了：重试，最后兜底用默认值
                # 但**不落盘**，下次启动再试。
                if attempt < 8:
                    time.sleep(0.02)
                    continue
                print(f"[config] config.json 暂时读不了（被占用？），"
                      f"本次用默认值，不覆盖原文件：{CONFIG_PATH}")
                cfg = cls()
                cfg._load_error = ("config.json 读不出来（被占用或权限不足），"
                                   "本次只能用默认值")
                return cfg, False

    @staticmethod
    def _try_write_back(cfg):
        """自愈后的回写：写不进去只告警，不能因此让整次加载失败。"""
        try:
            cfg._save_locked()
        except OSError as e:
            print(f"[config] 自愈回写失败（{e}），本次仅在内存里使用")

    @staticmethod
    def _read_backup(path=None):
        """读 `.good`，并做完整性校验。校验不过就返回 None = 当没有备份。

        校验至少两项：长度（非 0、读回字节数与 stat 一致）与内容
        （能解析成非空 JSON 对象）。半份备份绝不能被当成正常配置用
        （审查 R13-01）。
        """
        good = path or (CONFIG_PATH + ".good")
        try:
            size = os.path.getsize(good)
            if size <= 0:
                return None
            with open(good, "rb") as f:
                raw = f.read()
            if len(raw) != size:
                return None
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data:
            return None
        return data

    @staticmethod
    def _restore_good():
        """从 `config.json.good` 恢复；成功时把解析结果暂存到类属性。"""
        data = Config._read_backup()
        if data is None:
            return False
        Config._restore_good_cfg = Config(data)
        return True

    @staticmethod
    def _quarantine(err):
        """把坏掉的 config.json 改名留档，返回备份路径（失败返回 None）。

        文件名带纳秒级后缀：以前只精确到秒，同秒内连坏两次，第二份会把
        第一份覆盖掉 —— 第一份证据就这么没了（审查 R13-08c）。
        """
        stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 10 ** 9:09d}"
        # 保留 `.broken-<ts>` 段（现有测试按这个前缀断言），末尾再带 `.bad`
        # 标记，一眼能看出这是坏掉的配置备份。
        backup = f"{CONFIG_PATH}.broken-{stamp}.bad"
        n = 0
        while os.path.exists(backup) and n < 100:
            n += 1
            backup = f"{CONFIG_PATH}.broken-{stamp}-{n}.bad"
        try:
            os.replace(CONFIG_PATH, backup)
            print(f"[config] config.json 读不出来（{err}），已备份为 "
                  f"{os.path.basename(backup)}，本次用默认值启动")
            return backup
        except OSError:
            return None

    def public(self):
        out = json.loads(json.dumps(self.data))
        # 版本号只从 core/version.py 来（exe 版本资源、更新比对同一来源）
        out["version"] = APP_VERSION
        out["templates_ready"] = {n: self.has_template(n) for n in ("buy", "confirm")}
        # 读盘状态：界面据此显示「配置读不出来」，而不是把默认空队列
        # 当成「队列是空的」（review R1）。
        out["config_ok"] = self.load_ok
        out["config_error"] = self._load_error
        # 数据救回来了（从 .good 恢复）时的提示，别和 config_error 混在一起：
        # 前者照常工作，后者拒绝启动（审查 F1）。
        out["config_notice"] = self._notice
        return out

    # ---------- 模板 ----------

    def task_dir(self, task_id):
        d = os.path.join(DATA_DIR, task_id)
        os.makedirs(d, exist_ok=True)
        return d

    def template_path(self, name, task_id=None):
        base = self.task_dir(task_id) if task_id else TEMPLATE_DIR
        return os.path.join(base, name + ".png")

    def has_template(self, name, task_id=None):
        return os.path.isfile(self.template_path(name, task_id))

    def gc_task_dirs(self, keep=()):
        """删掉队列里已经不存在的任务目录，返回删了几个。

        task_dir() 每次加任务建一个 data/<id>/，但删任务、清已完成时没人
        删它 —— 实测 data/ 里堆了 799 个目录、53MB，而队列通常只有 1-2 条。
        只认「8 位十六进制」这种任务 id 命名的目录，shots/ 之类一律不碰。
        """
        if not self.load_ok:
            # 队列读不出来时调用方传进来的 keep 必然为空 —— 照旧执行就等于
            # 把 data/ 下所有任务目录当孤儿全删（审查 R12-03）。宁可不清理。
            print("[config] 配置读不出来，跳过任务目录清理")
            return 0
        keep = {str(k) for k in keep if k}
        removed = 0
        try:
            names = os.listdir(DATA_DIR)
        except OSError:
            return 0
        for name in names:
            if name in keep or not _TASK_ID_RE.match(name):
                continue
            if drop_task_dir(name):
                removed += 1
        return removed


def drop_task_dir(task_id):
    """删掉一个任务的模板目录。删了返回 True。

    放在模块级而不是 Config 方法里：调用方（scheduler）只需要一个 id，
    不该为了删目录去读一遍配置。
    """
    if not task_id:
        return False
    path = os.path.join(DATA_DIR, str(task_id))
    try:
        if not os.path.isdir(path):
            return False
        shutil.rmtree(path)
        return True
    except OSError:
        return False
