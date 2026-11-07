"""假输入：假屏幕、假鼠标、假 OCR、假配置。

硬约束（用户明确要求）：
  · 不移动真实光标 —— `FakeMouse` 全部是空实现；
  · 不在桌面开可见窗口 —— 没有任何 GUI/截图库调用；
  · 不写真实 data/ 目录 —— 长跑前把 `records.PATH`、`engine.DATA_DIR`
    指向本次运行的临时目录。
"""
import os
import shutil
import tempfile

import numpy as np


class FakeBox:
    """`screen.norm_to_px()` 的返回值替身。只需要宽高。"""

    def __init__(self, w, h):
        self.w = int(w)
        self.h = int(h)
        self.size = self.w * self.h

    def __iter__(self):
        return iter((0, 0, self.w, self.h))


class FakeScreen:
    """假截图。可以按次数/概率失败，用来注入「截图驱动挂了」。"""

    def __init__(self, crop_w=240, crop_h=65, full_w=480, full_h=270,
                 fail_every=0, fail_after=None):
        self.crop_w = crop_w
        self.crop_h = crop_h
        self.full_w = full_w
        self.full_h = full_h
        self.fail_every = int(fail_every or 0)
        self.fail_after = fail_after
        self.calls = 0
        self.full_calls = 0

    def _fail(self, n):
        if self.fail_after is not None and n >= self.fail_after:
            return True
        return bool(self.fail_every) and n % self.fail_every == 0

    def norm_to_px(self, box):
        return FakeBox(self.crop_w, self.crop_h)

    def grab(self, px=None):
        n = self.calls
        self.calls += 1
        if px is None:
            self.full_calls += 1
            if self._fail(self.full_calls):
                raise OSError("假截图：全屏捕获失败")
            return np.zeros((self.full_h, self.full_w, 3), np.uint8)
        if self._fail(n):
            raise OSError("假截图：捕获失败（注入）")
        return np.full((self.crop_h, self.crop_w, 3), 24, np.uint8)


class FakeMouse:
    """点一下只记数。绝不调用 SendInput / SetCursorPos。"""

    clicks = 0

    @staticmethod
    def foreground():
        return 1

    @staticmethod
    def ensure_foreground(hwnd, screen=None):
        return True

    @staticmethod
    def position():
        return (0, 0)

    @staticmethod
    def click(x, y, button="left", clicks=1, interval=0.0):
        FakeMouse.clicks += 1
        return True

    @staticmethod
    def down(x, y, button="left"):
        FakeMouse.clicks += 1
        return True

    @staticmethod
    def up(x, y, button="left"):
        return True

    @staticmethod
    def move(x, y):
        return True


class FakeOcr:
    """按虚拟时钟算出剩余秒数，返回真 `parse_words` 能认的词序列。

    `mode`:
      · "ok"      —— 正常输出「X分Y秒后解锁购买」
      · "empty"   —— 一直返回空（OCR 认不出字）
      · "raise"   —— 抛异常（OCR 服务不可用）
      · "remain"  —— 输出「剩余：X分Y秒」（框错了区域，部署陷阱）
    """

    def __init__(self, clock, target_pc, mode="ok", fail_after=None,
                 fail_at=None):
        self.clock = clock
        self.target_pc = float(target_pc)
        self.mode = mode
        self.fail_after = fail_after
        self.fail_at = fail_at
        self.calls = 0
        self.window = 0

    def read_words(self, img):
        n = self.calls
        self.calls += 1
        if self.fail_at is not None and self.clock.perf_counter() >= self.fail_at:
            self.mode = "raise"
        if self.fail_after is not None and n >= self.fail_after:
            if self.mode == "ok":
                self.mode = "raise"
        if self.mode == "raise":
            raise RuntimeError("假 OCR：服务不可用（注入）")
        if self.mode == "empty":
            return []
        rem = max(0, int(round(self.target_pc - self.clock.perf_counter())))
        if self.mode == "remain":
            m, s = divmod(rem, 60)
            return [{"text": f"剩余：{m}分{s:02d}秒", "x": 10}, {"text": "", "x": 20}]
        # 解锁瞬间文字会变成「剩余：…」，但 `due()` 已经成立，引擎不会再读
        m, s = divmod(rem, 60)
        return [{"text": "公示期", "x": 0},
                {"text": f"{m}分{s:02d}秒后解锁购买", "x": 30}]


class FakeCfg(dict):
    """引擎只认配置。真 Config 会把用户全局配置卷进来，这里给一份最小自洽的。"""

    def __init__(self, data=None):
        base = {
            "failsafe": False,
            "click_hz": 60,
            "fire_timeout_s": 2.0,
            "confirm_clicks": 3,
            "confirm_interval_ms": 20,
            "fire_delay_ms": 0,
            "fire_jitter_ms": 0,
            "fire_adaptive": False,
            "save_shots": True,
            "countdown_attempts": 3,
            "active_task": {"name": "稳定性测试·AK-12", "quota": None},
            "fire": {"verify": True},
            "env": {"record": True},
            "regions": {
                "clock": {"x": 100, "y": 100, "w": 240, "h": 65},
                "buy": {"x": 200, "y": 300, "w": 120, "h": 40},
                "confirm": {"x": 260, "y": 420, "w": 120, "h": 40},
            },
        }
        if data:
            base.update(data)
        super().__init__(base)

    def region(self, name):
        return self["regions"].get(name)


class TempWorkspace:
    """本次运行的临时目录。结束后默认保留，方便翻 CSV / 截图。"""

    def __init__(self, tag="stability"):
        self.root = tempfile.mkdtemp(prefix=f"dafors_{tag}_")
        self.data = os.path.join(self.root, "data")
        self.shots = os.path.join(self.data, "shots")
        os.makedirs(self.shots, exist_ok=True)

    def wipe(self):
        try:
            shutil.rmtree(self.root, ignore_errors=True)
        except OSError:
            pass
