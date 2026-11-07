"""GDI/USER 资源回归：重复抓屏 / 重复刷新 N 次后对象数不许上涨。

为什么单独锁这一条：长跑里 GDI 泄漏不会立刻爆，它只表现为「对象数单调
上涨」—— 8 小时才到 10000 上限，中途所有功能都正常，最难在真机上复现。
能复现的最小单元是「同一个路径重复 N 次」，所以这里就按这个口径断。

覆盖两条真实的分配路径：
  · core.regions.open_screen → mss Screen → grab → close（/api/screen/capture
    每刷新一次就走一遍；框选页 1Hz 预览、看板重绘都在用它）
  · core.pagewatch.PageWatcher._make_nav 构造失败（Screen 建好了但
    Navigator/Mouse 抛异常）—— 这条以前不 close，一次漏 2 个 GDI 对象
    （实测 60 次失败 +120，修复后 +0）

判据用 GetGuiResources 的进程总量，允许 ±2 的抖动（系统/字体缓存），
再大就是真泄漏。
"""
import ctypes
import os
import sys
from ctypes import wintypes

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="GDI 对象数只有 Windows 有")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1

# 注意：GetCurrentProcess() 是伪句柄 (-1)，GetGuiResources 对它一律返回 0，
# 用它量 GDI 会量到「永远是 0」的假平稳。必须 OpenProcess 拿真句柄。
_SELF = _kernel32.OpenProcess(0x1000 | 0x0400, False,
                              _kernel32.GetCurrentProcessId())


def gdi():
    return int(_user32.GetGuiResources(_SELF, GR_GDIOBJECTS))


def user():
    return int(_user32.GetGuiResources(_SELF, GR_USEROBJECTS))


@pytest.fixture(scope="module", autouse=True)
def _need_display():
    """没有真实屏幕（headless / 会话 0）时跳过：抓屏本身就没法做。"""
    from core import regions
    try:
        sc = regions.open_screen({"monitor": 1})
    except Exception as e:                       # noqa: BLE001
        pytest.skip(f"当前会话抓不到屏幕：{type(e).__name__}: {e}")
    sc.close()


def test_repeated_capture_does_not_grow_gdi():
    """重复 200 次「开屏→抓帧→关屏」：GDI/USER 必须回到起点。

    这就是看板每秒刷新 / 框选页 1Hz 预览走的那条路。每轮 mss 会建
    2 个 DC + 1 个 DIB 位图，close() 必须把它们全还回去；少还一个，
    这里就会看到 +200。
    """
    from core import regions

    # 预热：让字体/主题/DPI 缓存先建好，不计入基线
    for _ in range(5):
        sc = regions.open_screen({"monitor": 1})
        sc.grab()
        sc.close()

    g0, u0 = gdi(), user()
    checkpoints = []
    for i in range(200):
        sc = regions.open_screen({"monitor": 1})
        try:
            sc.grab()
        finally:
            sc.close()
        if (i + 1) % 50 == 0:
            checkpoints.append((i + 1, gdi(), user()))

    g1, u1 = gdi(), user()
    assert g1 - g0 <= 2, (
        f"重复抓屏 200 次后 GDI 从 {g0} 涨到 {g1}（+{g1 - g0}）；"
        f"中途检查点 {checkpoints}")
    assert u1 - u0 <= 2, (
        f"重复抓屏 200 次后 USER 从 {u0} 涨到 {u1}（+{u1 - u0}）")
    # 中途也不许单调爬升，避免「总量刚好被别的释放抵消」的假平稳
    assert all(g <= g0 + 2 for _, g, _ in checkpoints), checkpoints


def test_make_nav_failure_closes_screen(monkeypatch):
    """_make_nav 构造失败时，Screen 必须被 close（每次失败不许漏 2 个 GDI）。

    现实触发条件：显示器序号被改坏、mss 初始化偶发失败、Mouse 装载
    interception 驱动失败。8 小时挂机反复起停任务时，这条会攒成
    「GDI 单调上涨」。
    """
    from core import pagewatch
    import core.nav as nav_mod
    import core.input as input_mod

    class _BoomNav:
        def __init__(self, *a, **kw):
            raise RuntimeError("boom: 导航器构造失败（测试注入）")

    class _DummyMouse:
        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(nav_mod, "Navigator", _BoomNav)
    monkeypatch.setattr(input_mod, "Mouse", _DummyMouse)

    w = pagewatch.PageWatcher(monitor=1)
    g0, u0 = gdi(), user()
    for i in range(60):
        with pytest.raises(RuntimeError):
            w._make_nav()
    g1, u1 = gdi(), user()
    assert g1 - g0 <= 2, (
        f"构造失败 60 次后 GDI 从 {g0} 涨到 {g1}（+{g1 - g0}），"
        f"平均每次漏 {(g1 - g0) / 60:.1f} 个")
    assert u1 - u0 <= 2, f"构造失败 60 次后 USER 从 {u0} 涨到 {u1}"
