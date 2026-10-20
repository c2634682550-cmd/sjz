"""配置。坐标存 0-1 归一化比例，换分辨率不用重配。"""
import json
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
        "list": None,       # 交易行列表（列表模式用）
        "list_refresh": None,  # 列表刷新按钮（可选）
    },
    "trigger_mode": "vanish",
    "threshold": 0.85,
    "click_hz": 50,
    "confirm_clicks": 20,
    "confirm_interval_ms": 10,
    "fire_timeout_s": 8.0,
    "arm_grace_s": 1.5,     # 到点还没见到占位按钮时，宽限多久后强行开点
    "failsafe": True,
    # 列表模式（盯交易行搜索结果，谁先符合条件就买谁）
    "list_scan_interval": 0.05,   # 列表没变时多久看一次
    "list_scan_timeout": 0,       # 扫多久放弃，0 = 一直扫
    "list_detail_timeout": 3.0,   # 点进详情页后等购买按钮多久
    "list_max_tries": 20,         # 最多试多少条挂单
    "active_task": {},            # 当前任务的条件（引擎读它做过滤）
    # 全局热键停止。鼠标甩左上角在游戏全屏时不一定灵，键盘更可靠。
    "hotkey": {"enabled": True, "spec": "F8"},
    # 校时
    "time_sync": {
        "enabled": True,
        "servers": ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"],
        "samples": 4,
    },
    # 测速
    "latency": {
        "targets": [
            {"host": "df.qq.com", "port": 443, "label": "三角洲官网"},
            {"host": "game.qq.com", "port": 443, "label": "腾讯游戏"},
        ],
        "rounds": 4,
    },
    # 队列
    "queue": [],            # [{id, name, regions, templates_dir, fire_at, status, note}]
}


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
            elif k in ("time_sync", "latency", "hotkey") and isinstance(v, dict):
                self.data[k].update(v)
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
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls):
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    return cls(json.load(f))
            except (OSError, ValueError):
                pass
        # 第一次运行把默认值落盘，用户能看见、能备份、能手改
        cfg = cls()
        cfg.save()
        return cfg

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
