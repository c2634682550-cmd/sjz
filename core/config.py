"""配置。坐标存 0-1 归一化比例，换分辨率不用重配。"""
import json
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
    "filter_defaults": {"craft": "", "max_wear": 0.0, "sort": "price"},
    # ---------------- 开关表（core/switches.py）读的那些 ----------------
    # 每一项都在 switches.SWITCHES 里有对应条目，界面上拨一下就是改这里。
    "run_mode": "auto",           # auto / semi / dry
    "dry_run": True,              # 演练：不点鼠标
    "preflight_required": True,   # 开抢前必须过检查
    "force_foreground": True,     # 开点前抢前台并等画面刷新
    "record_enabled": True,       # 写 data/records.jsonl
    "countdown_attempts": 3,      # 倒计时模式最多换几件
    "stop_after_hit": False,      # 抢到就停整条队列
    "stop_after_fails": 0,        # 连续失败几次停（0=不停）
    # 关注（交易行详情面板顶部的星标，见 说明.txt 第十一节）
    "follow_before_buy": True,
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

    # OCR（见 core/ocr.py）。2026-11-01 起只剩 PP-OCRv6 一个后端：
    # 真截图评测里它是唯一「准确率满分 + 延迟可接受」的，其它方案全删。
    "ocr": {
        "ppocr": {
            "mode": "auto",                   # auto / http / off
            "url": "http://127.0.0.1:8081",   # paddlex --serve --pipeline OCR
            "timeout_s": 5.0,
            "cache": 64,                      # 同图缓存条数
            "max_side": 2560,                 # 超过就先缩图再发
            "min_score": 0.35,                # 低于这个置信度的行丢掉
            "model": "PP-OCRv6 small",        # 只用于自检显示，不影响请求
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


_save_lock = threading.Lock()


class Config:
    def __init__(self, data=None):
        self.data = json.loads(json.dumps(DEFAULTS))
        if data:
            self._merge(data)
        for d in (TEMPLATE_DIR, DATA_DIR):
            os.makedirs(d, exist_ok=True)

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

    def region(self, name):
        return self.data["regions"].get(name)

    def set_region(self, name, box):
        self.data["regions"][name] = box

    def save(self):
        """原子写：先写临时文件再 os.replace。

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
        with _save_lock:
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
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    return cls(json.load(f))
            except (OSError, ValueError) as e:
                # 文件坏了（手改漏个逗号最常见）。**先留个备份再重置** ——
                # 以前这里直接 pass，接着 save() 用默认值把原文件覆盖掉，
                # 用户所有设置（价格区间、队列、框选区域）当场没了，
                # 连「我改坏了什么」都看不出来。
                cls._quarantine(e)
        # 第一次运行把默认值落盘，用户能看见、能备份、能手改
        cfg = cls()
        cfg.save()
        return cfg

    @staticmethod
    def _quarantine(err):
        """把坏掉的 config.json 改名留档，返回备份路径（失败返回 None）。"""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = f"{CONFIG_PATH}.broken-{stamp}"
        try:
            os.replace(CONFIG_PATH, backup)
            print(f"[config] config.json 读不出来（{err}），已备份为 "
                  f"{os.path.basename(backup)}，本次用默认值启动")
            return backup
        except OSError:
            return None

    @classmethod
    def load(cls):
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    return cls(json.load(f))
            except (OSError, ValueError) as e:
                # 文件坏了（手改漏个逗号最常见）。**先留个备份再重置** ——
                # 以前这里直接 pass，接着 save() 用默认值把原文件覆盖掉，
                # 用户所有设置（价格区间、队列、框选区域）当场没了，
                # 连「我改坏了什么」都看不出来。
                cls._quarantine(e)
        # 第一次运行把默认值落盘，用户能看见、能备份、能手改
        cfg = cls()
        cfg.save()
        return cfg

    @staticmethod
    def _quarantine(err):
        """把坏掉的 config.json 改名留档，返回备份路径（失败返回 None）。"""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = f"{CONFIG_PATH}.broken-{stamp}"
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
