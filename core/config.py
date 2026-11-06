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
import json
import re
import shutil
import threading
import time
import os
import sys

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

DEFAULTS = {
    "monitor": 1,
    # 框选出来的归一化区域
    "regions": {
        "buy": None,        # 购买按钮
        "confirm": None,    # 确认弹窗里的确认按钮
        "name": None,       # 商品名称文字
        "price": None,      # 价格文字（可选）
        "clock": None,      # 游戏内倒计时（可选，只用于显示）
        "bar": None,        # 详情弹窗上的价格条（公示期版式价签会飘时用它补读）
        "list": None,       # 交易行列表（列表模式用）
        "list_refresh": None,  # 列表刷新按钮（可选）
    },
    "trigger_mode": "vanish",
    # 抢购路线：countdown（公示期倒计时归零就买）/ list（在售列表）/ detail（详情页按钮）
    "workflow": "countdown",
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
    # 输入通道：interception（默认，内核驱动注入）或 legacy
    # （SetCursorPos+PostMessage 旧路子）。驱动不可用时引擎自动退到 legacy
    # 并在日志里说明 —— 宁可慢，不要停。
    "input_mode": "interception",
    # 成交后留一张截图：抢到了什么、花了多少，事后能回看（约 300KB / 张）
    "save_shots": True,
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
        },
    },
}


# 可重入：load/save 内部也拿它，transaction 里再调 save 不会自己把自己锁死
_save_lock = threading.RLock()

# 任务 id 的形态（scheduler 用 uuid4().hex[:8] 生成）
_TASK_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# 抢购路线的唯一枚举。`workflow` 是**唯一真源**，`mode`（list/detail）
# 只是它的派生视图 —— 以前两个字段各自为政，`workflow:"countdown"` +
# `mode:"list"` 的任务会让前置检查去看列表区域（审查 S7）。
WORKFLOWS = ("countdown", "list", "detail")
TRIGGER_MODES = ("vanish", "appear")


class ConfigLoadError(RuntimeError):
    """config.json 读不出来（被占用 / 权限 / 一直是坏文件）。

    抛这个而不是退回默认值：`transaction()` 一退就会 `_save_locked()`，
    拿默认值回写等于把用户的框选 / 队列 / 阈值整份抹掉，不可逆（审查 S1）。
    """


def normalize_workflow(value, fallback="countdown"):
    """把任意输入归一成合法路线；不认识就退回 fallback。"""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in WORKFLOWS:
            return v
    return fallback


def mode_for_workflow(workflow):
    """workflow → 引擎的 mode（list / detail）。"""
    return "list" if normalize_workflow(workflow) == "list" else "detail"


def task_workflow(task, fallback="countdown"):
    """任务走哪条路线。workflow 优先；老任务没有它时按 mode 迁移。"""
    task = task or {}
    wf = normalize_workflow(task.get("workflow"), "")
    if wf:
        return wf
    legacy = task.get("mode")
    if legacy == "list":
        return "list"
    if legacy == "detail":
        return "detail"
    return normalize_workflow(fallback)


class Config:
    def __init__(self, data=None):
        self.data = json.loads(json.dumps(DEFAULTS))
        # 读盘失败时这里记一句原因；正常读出来的配置永远是 None。
        # `load_ok` / `last_error` 就是靠它区分「用户配置就是空队列」
        # 和「config.json 根本没读出来」——以前两者长得一模一样。
        self._load_error = None
        if data:
            self._merge(data)
        self._normalize()
        for d in (TEMPLATE_DIR, DATA_DIR):
            os.makedirs(d, exist_ok=True)

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

        wf = normalize_workflow(d.get("workflow"), "")
        if not wf:
            legacy = d.get("mode")
            wf = ("list" if legacy == "list"
                  else "detail" if legacy == "detail" else "countdown")
        d["workflow"] = wf
        d["mode"] = mode_for_workflow(wf)

        if not isinstance(d.get("queue"), list):
            d["queue"] = []
        d["queue"] = [t for t in d["queue"] if isinstance(t, dict)]
        if not isinstance(d.get("regions"), dict):
            d["regions"] = json.loads(json.dumps(DEFAULTS["regions"]))
        for name, box in list(d["regions"].items()):
            if box is not None and not isinstance(box, dict):
                d["regions"][name] = None
        if not isinstance(d.get("console"), dict):
            d["console"] = json.loads(json.dumps(DEFAULTS["console"]))
        if not isinstance(d.get("filter_defaults"), dict):
            d["filter_defaults"] = json.loads(json.dumps(DEFAULTS["filter_defaults"]))

        for t in d["queue"]:
            twf = task_workflow(t, wf)
            t["workflow"] = twf
            t["mode"] = mode_for_workflow(twf)
            try:
                t["fire_at"] = float(t.get("fire_at") or 0)
            except (TypeError, ValueError):
                t["fire_at"] = 0.0
            if not isinstance(t.get("regions"), dict):
                t["regions"] = {}
            if not isinstance(t.get("status"), str) or not t.get("status"):
                t["status"] = "pending"

    def _merge(self, data):
        for k, v in data.items():
            if k == "regions" and isinstance(v, dict):
                self.data["regions"].update({n: b for n, b in v.items() if n in self.data["regions"]})
            elif k in ("time_sync", "hotkey") and isinstance(v, dict):
                self.data[k].update(v)
            elif k == "ocr" and isinstance(v, dict):
                # ocr 是两层：backend / hot_backend + paddle_vl 子字典，
                # 整体替换会把用户只改了一半的 paddle_vl 打回默认
                for kk, vv in v.items():
                    if isinstance(vv, dict) and isinstance(self.data["ocr"].get(kk), dict):
                        self.data["ocr"][kk].update(vv)
                    else:
                        self.data["ocr"][kk] = vv
            elif k == "queue" and isinstance(v, list):
                self.data["queue"] = v
            else:
                self.data[k] = v

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def load_ok(self):
        """这份配置是不是**真的从盘上读出来的**。

        False = config.json 被占用/权限不足，手上这份是 DEFAULTS。
        调度器必须按「读失败」处理：保持队列、别推进状态机 ——
        否则空队列会被当成「跑完了」直接收工（review R1）。
        """
        return self._load_error is None

    @property
    def last_error(self):
        """读盘失败的原因（没失败时是 None）。"""
        return self._load_error

    def region(self, name):
        return self.data["regions"].get(name)

    def set_region(self, name, box):
        self.data["regions"][name] = box

    def save(self):
        """原子写：先写临时文件再 os.replace。"""
        with _save_lock:
            self._save_locked()

    def _save_locked(self):
        """真正的写盘。调用方必须已经持有 _save_lock。

        为什么不能直接 open(CONFIG_PATH, "w")：
          · 它先截断再写。写到一半进程被杀（抢购中强退、蓝屏）→ 文件是半截
            JSON，下次启动读不出来。
          · 服务端线程（保存开关）和调度器线程（每个任务状态变化都 save）
            会并发写同一个文件。两边同时截断/写，最后落盘的是两份内容交错
            的垃圾。

        两个 Windows 细节（都是压测压出来的）：
          · `os.replace` 在目标文件正被别的线程**打开着读**时会抛
            PermissionError（共享冲突）。所以重试几次，而不是一失败就退回直写。
          · 临时文件名要带线程号：共用同一个 `.tmp` 时，A 写一半、B 又把
            它截断，A 的 replace 就把 B 的半成品换上去 —— 读出来是空文件。
        """
        tmp = f"{CONFIG_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(6):
                try:
                    os.replace(tmp, CONFIG_PATH)
                    return
                except PermissionError:
                    # 别人正开着这个文件读（杀软扫描、另一个线程 json.load）
                    time.sleep(0.02 * (attempt + 1))
            os.replace(tmp, CONFIG_PATH)          # 最后一次，失败就抛
        except OSError:
            # 实在写不了（目录只读之类）才退回直写 —— 至少别把保存整个搞挂
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls):
        """读配置。整个过程持有 _save_lock，和 save 串行化。

        不串行的话：save 正在 os.replace，这边正好 open 同一个路径，
        Windows 会抛 PermissionError（共享冲突）。
        """
        with _save_lock:
            return cls._load_locked()[0]

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
        """
        with _save_lock:
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
            fresh, ok = self._load_locked()
            if not ok:
                print("[config] reload 失败，保留当前内存里的配置")
                return self
            self.data.clear()
            self.data.update(fresh.data)
        return self

    @classmethod
    def _load_locked(cls):
        """真正的读。调用方必须已经持有 _save_lock（所以不能调 save()）。

        返回 `(配置, 是否读到合法配置)`。第二项为 False 时调用方**禁止写盘**。
        """
        if not os.path.isfile(CONFIG_PATH):
            cfg = cls()
            cfg._save_locked()                     # 第一次运行：把默认值落盘
            return cfg, True

        for attempt in range(9):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    data = json.load(f)
                # 顶层不是对象（"abc" / 123 / [...]）同样当坏文件：以前这种
                # 内容进 _merge 会 AttributeError，或者被当成合法配置悄悄
                # 补全默认值，用户看到的是「配置自己重置了」（审查 S1）。
                if not isinstance(data, dict):
                    raise ValueError(f"顶层不是 JSON 对象（{type(data).__name__}）")
                return cls(data), True
            except ValueError as e:
                # **只有内容坏了才留档重置**（手改漏个逗号最常见）。
                # 以前这里连 OSError 一起吞，把「文件被占用」也当成「文件坏了」，
                # 会把好好的配置备份走再重置 —— 那是数据丢失。
                cls._quarantine(e)
                cfg = cls()
                cfg._save_locked()
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
    def _quarantine(err):
        """把坏掉的 config.json 改名留档，返回备份路径（失败返回 None）。"""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # 保留 `.broken-<ts>` 段（现有测试按这个前缀断言），末尾再带 `.bad`
        # 标记，一眼能看出这是坏掉的配置备份。
        backup = f"{CONFIG_PATH}.broken-{stamp}.bad"
        try:
            os.replace(CONFIG_PATH, backup)
            print(f"[config] config.json 读不出来（{err}），已备份为 "
                  f"{os.path.basename(backup)}，本次用默认值启动")
            return backup
        except OSError:
            return None

    def public(self):
        out = json.loads(json.dumps(self.data))
        out["templates_ready"] = {n: self.has_template(n) for n in ("buy", "confirm")}
        # 读盘状态：界面据此显示「配置读不出来」，而不是把默认空队列
        # 当成「队列是空的」（review R1）。
        out["config_ok"] = self.load_ok
        out["config_error"] = self._load_error
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
