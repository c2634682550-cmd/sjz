"""可移植性与适配缺陷的回归测试（审查 _bug_r09_portability.md）。

这些用例的重点是**可复现的实测证据**，不是「读代码觉得对」：

  · R09-02 缩放：用仓库里真实的 templates/buy.png，在 1080p 纹理背景上
    合成按钮，再按 100/125/150/200% 缩放整屏，逐档打印「旧实现 / 新实现」
    的匹配分数。缩放曲线是跑出来的，阈值断言也是真的比过。
  · R09-03 多屏：两块**同分辨率**的假显示器 + 假游戏窗口矩形，验证选屏
    下标；再用一份独立的交集面积循环复核，两法结果必须一致。
  · R09-04 DPI：在子进程里测真实生效值，并用两种 Win32 读法互相核对。
  · R09-05 模板：缺模板时的检查项必须给出「哪个文件、跑哪条命令」。
  · R09-06/08/10/13 等：启动脚本探测、退出前释放输入、窗口夹紧、字体分级。

不开可见窗口、不动真实鼠标、不跑长时间循环；所有截图/文件都在 tmp_path。
"""
import importlib.util
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import preflight, vision                     # noqa: E402
from core import screen as screen_mod                 # noqa: E402

TPL_BUY = os.path.join(ROOT, "templates", "buy.png")
TPL_CONFIRM = os.path.join(ROOT, "templates", "confirm.png")


def _textured_screen(w=1920, h=1080, seed=0):
    """有纹理的假游戏画面。

    纯色背景 + 模板会让 TM_CCOEFF_NORMED 失去统计意义（方差为 0），
    这里用随机噪声 + 高斯模糊，保证背景本身有结构、模板分数可比。
    """
    rng = np.random.default_rng(seed)
    base = rng.integers(30, 210, (h, w, 3), dtype=np.uint8)
    return cv2.GaussianBlur(base, (9, 9), 0)


def _paste(screen_img, tpl, x, y):
    h, w = tpl.shape[:2]
    screen_img[y:y + h, x:x + w] = tpl


def _legacy_score(haystack_gray, needle_gray):
    """旧实现的等价复算：不做任何尺度对齐，直接 matchTemplate。"""
    res = cv2.matchTemplate(haystack_gray, needle_gray, cv2.TM_CCOEFF_NORMED)
    _, best, _, loc = cv2.minMaxLoc(res)
    return float(best), loc


def _warped_template(gray, factor):
    """独立复核用的缩放：走 warpAffine（另一条 OpenCV 代码路径）。

    不用 `cv2.resize(dsize=...)`，因为它先把宽高取整、再各自算采样系数，
    `fx/fy` 与 `dsize` 两条路径在 303×70→379×88 这种非整倍数下会差 0.02
    以上。warpAffine 用显式变换矩阵，缩放系数就是 factor 本身，和
    `vision._resized` 的语义一致，但实现完全独立。
    """
    h, w = gray.shape[:2]
    mat = np.float32([[factor, 0, 0], [0, factor, 0]])
    return cv2.warpAffine(gray, mat,
                          (int(round(w * factor)), int(round(h * factor))),
                          flags=cv2.INTER_CUBIC)


# ---------------------------------------------------------------- R09-02


@pytest.mark.skipif(not os.path.isfile(TPL_BUY), reason="仓库缺少 templates/buy.png")
def test_scale_curve_is_measured_and_fixed(capsys):
    """100/125/150/200% 缩放下：旧实现掉分，新实现仍过 0.85 且定位正确。"""
    tpl = vision.imread(TPL_BUY)                       # cwd: 项目根
    assert tpl is not None
    tpl_gray = vision.to_gray(tpl)
    # imread 绑定了 templates/meta.json 的采集环境（1920×1080 / 96 DPI）
    assert vision.capture_screen_of(tpl_gray) == (1920, 1080)

    base = _textured_screen()
    _paste(base, tpl, 800, 600)
    base_gray = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)

    rows = []
    for factor in (1.0, 1.25, 1.5, 2.0):
        scaled = cv2.resize(base_gray, None, fx=factor, fy=factor,
                            interpolation=cv2.INTER_CUBIC)
        vision.set_target_screen(
            (int(round(1920 * factor)), int(round(1080 * factor)),), 96)

        legacy, _ = _legacy_score(scaled, tpl_gray)
        new_score, loc = vision.match(scaled, tpl_gray)

        # 独立复核：用 warpAffine 缩放模板，再自己 matchTemplate 一遍
        manual_tpl = _warped_template(tpl_gray, factor)
        manual, manual_loc = _legacy_score(scaled, manual_tpl)

        rows.append((factor, legacy, new_score, manual, loc, manual_loc))
        print(f"{factor * 100:5.0f}%  legacy={legacy:.4f}  new={new_score:.4f}  "
              f"独立复核={manual:.4f}  loc={loc} / {manual_loc}")

        # 两条独立路径对边缘像素的插值策略不同（resize 与 warpAffine 的
        # 边界处理不一样），分数允许小幅差异；但**两个实现都必须过阈值**，
        # 且命中坐标必须一致 —— 这才是「修好了」的可证伪结论。
        assert manual >= 0.85, f"{factor:.0%} 独立实现也匹配不上"
        assert abs(manual - new_score) <= 0.06, "两种算法差异过大"
        assert new_score >= 0.85, f"{factor:.0%} 缩放仍匹配不上"
        # 两种插值在整数边缘差 1px 属正常（新模板 298×58 在 200% 正好压边界）；
        # 超过 1px 才是真错位。
        assert abs(loc[0] - manual_loc[0]) <= 1 and abs(loc[1] - manual_loc[1]) <= 1, (
            f"两种算法命中的位置不一致：{loc} vs {manual_loc}")
        expected = (int(round(800 * factor)), int(round(600 * factor)))
        assert loc[0] == pytest.approx(expected[0], abs=1), "命中位置不对"
        assert loc[1] == pytest.approx(expected[1], abs=1), "命中位置不对"

    # 这条是「修之前确实坏」的证据：非 100% 缩放下旧实现明显低于阈值
    assert rows[1][1] < 0.85, "125% 下旧实现居然过线了，合成场景不具代表性"
    assert rows[2][1] < 0.85
    assert rows[3][1] < 0.85


@pytest.mark.skipif(not os.path.isfile(TPL_CONFIRM), reason="仓库缺少 confirm.png")
def test_dpi_ratio_also_drives_scale_hint():
    """同分辨率、125% 系统缩放：分辨率比例是 1.0，得靠 DPI 比例选到 1.25。"""
    tpl = vision.imread(TPL_CONFIRM)
    gray = vision.to_gray(tpl)

    vision.set_target_screen((1920, 1080), 120)        # 120 DPI = 125%
    cands = vision._scale_candidates(gray)
    assert any(abs(c - 1.25) < 0.02 for c in cands), cands

    base = _textured_screen()
    _paste(base, tpl, 300, 400)
    scaled = cv2.resize(cv2.cvtColor(base, cv2.COLOR_BGR2GRAY),
                        None, fx=1.25, fy=1.25, interpolation=cv2.INTER_CUBIC)

    score, loc = vision.match(scaled, gray)
    manual_tpl = _warped_template(gray, 1.25)
    manual, manual_loc = _legacy_score(scaled, manual_tpl)
    print(f"DPI 125%: new={score:.4f} 独立复核={manual:.4f} loc={loc} / {manual_loc}")
    assert score >= 0.85
    assert manual >= 0.85
    assert abs(score - manual) <= 0.06
    assert loc[0] == pytest.approx(int(300 * 1.25), abs=1)


# ---------------------------------------------------------------- R09-03


class _FakeSct:
    def __init__(self, monitors):
        self.monitors = monitors

    def close(self):
        pass

    def grab(self, rect):       # pragma: no cover - 本组用例不抓屏
        return None


def _two_same_screens():
    return [
        {"left": 0, "top": 0, "width": 3840, "height": 1080},   # 0 = 虚拟屏
        {"left": 0, "top": 0, "width": 1920, "height": 1080},   # 副屏（左边）
        {"left": 1920, "top": 0, "width": 1920, "height": 1080},  # 同分辨率（右边）
    ]


def test_same_resolution_monitors_are_distinguished_by_game_window(monkeypatch):
    """两块 1920×1080：游戏窗口在右屏，就必须抓右屏（旧实现永远 1 号屏）。"""
    fake = _FakeSct(_two_same_screens())
    monkeypatch.setattr(screen_mod.mss, "mss", lambda: fake)
    game_rect = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
    monkeypatch.setattr(screen_mod, "find_game_window_rect", lambda cls="UnrealWindow": game_rect)
    monkeypatch.setattr(screen_mod, "_dpi_for_monitor", lambda bounds: 96)

    s = screen_mod.Screen(1)
    assert s.index == 2, f"选错了显示器：{s.index}（{s.pick_reason}）"
    assert "游戏窗口" in s.pick_reason

    # 独立复核：手写一遍「交集面积最大」的循环，结果必须一致
    expected = None
    best_area = -1
    for i, m in enumerate(_two_same_screens()):
        if i == 0:
            continue
        area = screen_mod._intersection_area(game_rect, m)
        if area > best_area:
            best_area, expected = area, i
    assert expected == s.index


def test_primary_display_fallback_uses_position_not_just_size(monkeypatch):
    """没找到游戏窗口时按主屏「位置+尺寸」匹配；同尺寸的幽灵屏不能顶替。"""
    fake = _FakeSct(_two_same_screens())
    monkeypatch.setattr(screen_mod.mss, "mss", lambda: fake)
    monkeypatch.setattr(screen_mod, "find_game_window_rect", lambda cls="UnrealWindow": None)
    monkeypatch.setattr(screen_mod, "_primary_monitor_rect",
                        lambda: {"left": 1920, "top": 0, "width": 1920, "height": 1080})
    monkeypatch.setattr(screen_mod, "_dpi_for_monitor", lambda bounds: 96)

    s = screen_mod.Screen(1)
    # 旧实现只比尺寸，两块都是 1920×1080，会命中第一块（1 号）；新实现按
    # 左边界匹配到右屏（2 号）。
    assert s.index == 2, f"{s.index} / {s.pick_reason}"


# ---------------------------------------------------------------- R09-04


def test_dpi_awareness_is_per_monitor_v2_by_two_readings():
    """子进程里设一次，两种读法互相核对；必须是 2（per-monitor）。"""
    code = (
        "import ctypes, sys;"
        f"sys.path.insert(0, {ROOT!r});"
        "from core.screen import enable_dpi_awareness;"
        "a = enable_dpi_awareness();"
        "b = ctypes.c_int();"
        "ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(b));"
        "print('%s %s' % (a, b.value))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.split()
    assert first == "2" and second == "2", out.stdout

    # 静态复核：启用 DPI 感知的调用必须出现在 import webview 之前
    src = open(os.path.join(ROOT, "desktop.py"), encoding="utf-8").read()
    pos_dpi = src.index("enable_dpi_awareness()")
    pos_webview = src.index("\n    import webview")
    assert pos_dpi < pos_webview, "DPI 感知仍然在 pywebview 之后设置"


# ---------------------------------------------------------------- R09-05


def _facts(**kw):
    base = {
        "size": (1920, 1080), "want_size": (1920, 1080), "size_tolerance": 0.0,
        "ocr": True, "page": "detail", "page_error": None,
        "templates": {"buy": True, "confirm": True},
        "regions": {"buy": {"x": .7, "y": .7, "w": .1, "h": .1},
                    "confirm": {"x": .4, "y": .5, "w": .1, "h": .1},
                    "list": None, "clock": None},
        "clock": {"offset": 0.004, "at": 1, "server": "NTP"},
    }
    base.update(kw)
    return base


def test_missing_template_points_at_actual_files_and_command():
    facts = _facts(templates={"buy": False, "confirm": True})
    out = {c["id"]: c for c in preflight.evaluate(facts, workflow="detail")}
    item = out["templates"]
    assert not item["ok"]
    hint = item["fix"]["hint"]
    assert "calibrate" in hint and "templates/buy.png" in hint
    assert "regions" in hint


def test_template_bigger_than_region_is_blocked():
    """模板 400×200 塞进 300×120 的搜索区域：matchTemplate 必返回 0。"""
    facts = _facts(template_sizes={"buy": (400, 200)},
                   region_px={"buy": (300, 120)},
                   template_capture_screen=(1920, 1080))
    out = {c["id"]: c for c in preflight.evaluate(facts, workflow="detail")}
    item = out["templates"]
    assert not item["ok"]
    assert "比框选区域" in item["detail"]
    assert "重新采集" in item["fix"]["hint"]


def test_template_scale_ok_when_region_is_larger():
    facts = _facts(template_sizes={"buy": (315, 67)},
                   region_px={"buy": (360, 110)},
                   template_capture_screen=(1920, 1080))
    out = {c["id"]: c for c in preflight.evaluate(facts, workflow="detail")}
    assert out["templates"]["ok"]


def test_calibrate_dry_run_lists_required_items():
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "calibrate.py"),
         "--workflow", "countdown", "--dry-run"],
        capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    for name in ("clock", "buy", "confirm"):
        assert name in out.stdout


def test_specs_bundle_default_templates_and_calibrator():
    for spec_name in ("sjz.spec", "sjz-onefile.spec"):
        src = open(os.path.join(ROOT, spec_name), encoding="utf-8").read()
        assert '"templates"' in src, f"{spec_name} 没把 templates/ 打进包"
        assert "calibrate.py" in src, f"{spec_name} 没带首次标定工具"


# ---------------------------------------------------------------- R09-06


def test_start_bat_probes_before_trusting_venv():
    src = open(os.path.join(ROOT, "start.bat"), encoding="utf-8").read()
    # 必须先真的运行 .venv 解释器（导入核心依赖），而不是只看文件存在
    assert re.search(r'if exist "%VENV_PY%"[^\n]*', src)
    assert "-c \"import sys,cv2,mss,numpy; assert sys.prefix\"" in src
    assert "uv sync" in src                       # 回落 1
    assert "python -c \"import sys; assert sys.version_info" in src   # 回落 2
    assert "pause" in src                         # 全失败时别一闪而过


# ---------------------------------------------------------------- R09-08


def test_exit_skips_input_release_when_nothing_is_running(monkeypatch):
    """关闭时引擎没在跑 → 不该等 stop()、也不该强制释放输入。

    退出耗时 2.1s → 33ms 的两条快路径之一：没有在跑的任务就直接收工。
    旧实现无条件 stop() + force_release_input()，每次关窗白等一轮超时。
    """
    import core.server as server_mod

    class FakeEngine:
        def __init__(self):
            self.released = 0

        def force_release_input(self):
            self.released += 1

        def status(self):
            return {"running": False}

    class FakeSched:
        def __init__(self):
            self._engine = FakeEngine()
            self.calls = 0

        def stop(self):
            self.calls += 1
            return (False, "正在停止（上一轮还在收尾）")

    sched = FakeSched()
    monkeypatch.setattr(server_mod, "_scheduler", sched, raising=False)

    import desktop
    ok = desktop._release_input_before_exit(timeout=1.0)
    assert ok is True
    assert sched.calls == 1, "stop() 是同步的，该调一次"
    assert sched._engine.released == 0, "没在跑，不该再走强制释放的等待循环"


def test_exit_forces_input_release_when_scheduler_wont_stop(monkeypatch):
    """引擎还在跑、stop() 又不成功 → 超时后必须强制释放输入（并如实报 False）。"""
    import core.server as server_mod
    import time

    class FakeEngine:
        def __init__(self):
            self.released = 0

        def force_release_input(self):
            self.released += 1

        def status(self):
            return {"running": True}

    class FakeSched:
        def __init__(self):
            self._engine = FakeEngine()

        def stop(self):
            return (False, "正在停止（上一轮还在收尾）")

    sched = FakeSched()
    monkeypatch.setattr(server_mod, "_scheduler", sched, raising=False)

    import desktop
    t0 = time.monotonic()
    ok = desktop._release_input_before_exit(timeout=0.2)
    assert ok is False, "没释放干净却报 True，正是要钉住的假阳性"
    assert sched._engine.released >= 1, "超时期间没强制释放输入"
    assert time.monotonic() - t0 < 2.0, "退出又被拖成秒级"


def test_exit_without_scheduler_is_noop():
    import core.server as server_mod
    import desktop
    old = getattr(server_mod, "_scheduler", None)
    try:
        server_mod._scheduler = None
        assert desktop._release_input_before_exit() is True
    finally:
        server_mod._scheduler = old


# ---------------------------------------------------------------- R09-10


def test_window_is_clamped_into_work_area(monkeypatch):
    import desktop
    monkeypatch.setattr(desktop, "_primary_work_area", lambda: (1366, 728))
    w, h, min_size = desktop._fit_window(1180, 800)
    assert w <= 1366 and h <= 728, (w, h)
    assert h >= 700, f"1366×768 上窗口被压得太扁：{h}"
    assert min_size[1] <= h

    monkeypatch.setattr(desktop, "_primary_work_area", lambda: (2560, 1440))
    w, h, _ = desktop._fit_window(2560, 1440)
    assert w <= 2560 and h <= 1440


# ---------------------------------------------------------------- R09-13


def test_mdl2_is_not_required_font():
    import core.selfcheck as selfcheck
    assert selfcheck.FONT_REQUIRED == ("msyh.ttc",)
    assert "segmdl2.ttf" in selfcheck.FONT_ICON_OPTIONAL
    assert "segmdl2.ttf" not in selfcheck.FONT_REQUIRED
    text = "\n".join(selfcheck.doctor_lines(deep=False))
    assert "mdl2" in text.lower() and "可选" in text


# ---------------------------------------------------------------- R09-12


def test_price_parser_rejects_dot_grouped_numbers():
    """`1.234` 不能被当成 234 —— 点分是小数/千分位的歧义写法，只能拒识。

    `price_from_text` 返回 (value, token)；value=None 表示拒识，token 仅供日志。
    这条用例复核审查 R09-12：当前实现已经拒识（`(None, '234')`），
    这里把它锁住，防止以后有人把「取最后一个数字串」的逻辑改回去。
    """
    from core import ocr_chain
    assert ocr_chain.price_from_text("1.234")[0] is None
    assert ocr_chain.price_from_text("12.345")[0] is None
    assert ocr_chain.price_from_text("1,234")[0] == 1234
    assert ocr_chain.price_from_text("12,345")[0] == 12345


# ---------------------------------------------------------------- R09-05 打包释放


def test_seed_assets_copies_default_templates(tmp_path, monkeypatch):
    import desktop
    bundled = tmp_path / "bundle"
    (bundled / "templates").mkdir(parents=True)
    (bundled / "templates" / "buy.png").write_bytes(b"\x89PNG fake")
    (bundled / "templates" / "meta.json").write_text("{}", encoding="utf-8")
    appdir = tmp_path / "app"
    appdir.mkdir()

    monkeypatch.setattr(desktop, "BASE", str(bundled))
    monkeypatch.setattr(desktop, "APPDIR", str(appdir))
    copied = desktop._seed_missing_assets()
    assert copied >= 2
    assert (appdir / "templates" / "buy.png").is_file()

    # 用户自己的模板不覆盖
    (appdir / "templates" / "buy.png").write_bytes(b"user")
    desktop._seed_missing_assets()
    assert (appdir / "templates" / "buy.png").read_bytes() == b"user"
