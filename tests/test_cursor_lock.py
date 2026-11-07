"""光标裁剪锁：**只读记录，不改写**（release_cursor_lock 已停用写操作）。

游戏独占全屏时会把光标钉在一个点上（ClipCursor）。旧实现用
`ClipCursor(None)` 清掉它，确实能让 SetCursorPos 恢复，但那是**全局改写
用户桌面状态**，和「不碰本机鼠标」的要求直接冲突。

现在的契约：
- 只调用 `GetClipCursor`（读），**从不**调用 `ClipCursor`（写）；
- **从不**移动光标；
- 返回值恒为 `(True, False)`，表示「没有做任何清锁动作」；
- 检测到裁剪区时只往输入账本里记一条 info。

本文件的所有用例都替换 `core.input._user32`，不读、不写真实光标。
"""
import ctypes
import ctypes.wintypes  # noqa: F401  (RECT 类型断言要用)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import input as input_mod  # noqa: E402
from core.input import Mouse  # noqa: E402


class FakeUser32:
    """只实现 `release_cursor_lock` 允许触达的调用。

    任何写裁剪区 / 移动光标的调用都记下来 —— 本版本一次都不许出现。
    """

    def __init__(self, *, had_lock=True):
        self.had_lock = had_lock
        self.get_clip_calls = 0
        self.clip_writes = []
        self.cursor_moves = []

    def GetClipCursor(self, _ref):
        self.get_clip_calls += 1
        return 1 if self.had_lock else 0

    def ClipCursor(self, rect):
        self.clip_writes.append(rect)
        return 1

    def SetCursorPos(self, x, y):
        self.cursor_moves.append((x, y))
        return 1

    def GetCursorPos(self, _ref):
        return 0


def _use(monkeypatch, fake):
    monkeypatch.setattr(input_mod, "_user32", fake)
    return fake


def test_never_reports_a_cleanup_it_did_not_do(monkeypatch):
    """即使系统里有锁，也不许报「刚清掉了一个锁」—— 本版本什么都没做。"""
    fake = _use(monkeypatch, FakeUser32(had_lock=True))
    ok, had = Mouse.release_cursor_lock()

    assert (ok, had) == (True, False)
    assert fake.get_clip_calls == 1, "应该读一次系统状态"
    assert fake.clip_writes == [], "不许调用 ClipCursor 改写裁剪区"
    assert fake.cursor_moves == [], "不许移动光标"


def test_no_lock_still_reports_nothing_done(monkeypatch):
    fake = _use(monkeypatch, FakeUser32(had_lock=False))
    ok, had = Mouse.release_cursor_lock()

    assert (ok, had) == (True, False)
    assert fake.clip_writes == []
    assert fake.cursor_moves == []


def test_detected_clip_is_recorded_in_the_ledger(monkeypatch):
    """检测到裁剪区只记账（info），不改写系统状态。"""
    _use(monkeypatch, FakeUser32(had_lock=True))
    before = len(input_mod.channel_events())
    Mouse.release_cursor_lock()

    events = input_mod.channel_events()[before:]
    hits = [e for e in events if e.get("stage") == "cursor_lock"]
    assert len(hits) == 1, "检测到裁剪区必须落一条账"
    assert hits[0]["severity"] == "info"
    assert hits[0]["ok"] is True


def test_no_ledger_entry_when_there_is_no_clip(monkeypatch):
    _use(monkeypatch, FakeUser32(had_lock=False))
    before = len(input_mod.channel_events())
    Mouse.release_cursor_lock()

    events = input_mod.channel_events()[before:]
    assert [e for e in events if e.get("stage") == "cursor_lock"] == []


def test_returns_real_bools_not_the_raw_api_wints(monkeypatch):
    _use(monkeypatch, FakeUser32(had_lock=True))
    ok, had = Mouse.release_cursor_lock()
    assert isinstance(ok, bool) and isinstance(had, bool)


def test_no_cursor_is_locked_api():
    """`cursor_is_locked` 必须不存在 —— 它的实现会真实移动用户光标。"""
    assert not hasattr(Mouse, "cursor_is_locked")


def test_calls_into_ctypes_are_the_ones_we_think(monkeypatch):
    """确认 `_user32` 被真的用上了（不是绕过它直接摸 ctypes.windll）。"""
    fake = _use(monkeypatch, FakeUser32(had_lock=False))
    seen = []
    orig = fake.GetClipCursor

    def spy(ref):
        seen.append(ref)
        return orig(ref)

    fake.GetClipCursor = spy
    Mouse.release_cursor_lock()
    assert len(seen) == 1
    assert type(seen[0]).__name__ == "CArgObject", "传进去的应该是 RECT 的指针"
    assert isinstance(seen[0]._obj, ctypes.wintypes.RECT), "指针应该指向 RECT，不是别的结构"
