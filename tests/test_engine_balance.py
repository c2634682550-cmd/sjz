"""余额读取：只认「最右一组带 `+` 的货币」，其余一律不猜。

真机铁证（2026-11-08 15:14）：点购买弹出「您当前的三角币不足…(缺少2三角币…)」，
那一件单价 10 → 账户三角币 = 8。同一帧货币条 OCR 是
`2,049 / 0+ / 8+`：2,049 是左侧纸币，8 才是最右的三角币。

所以规则不是「带千分位优先」（那会读成 2,049 纸币，多出一千多倍），
而是「最右一组带 `+` 的数字」。ROI 里没有带 `+` 的组时返回 None，
上层按「余额未识别」处理 —— 绝不许返回一个错误的数字。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import engine as engine_mod
from core import ocr


def _w(text, x=0, y=40, w=50, h=17):
    return {"x": x, "y": y, "w": w, "h": h, "text": text, "score": 0.9}


# ---------------------------------------------------------------- 纯解析


def test_reads_rightmost_marked_currency():
    """同帧 2,049 / 0+ / 8+ → 三角币 8（游戏弹窗「缺少2」证实）。"""
    words = [_w("2,049", x=83), _w("0+", x=176), _w("8+", x=256)]
    assert ocr.balance_from_words(words) == 8


def test_paper_currency_never_returned():
    """ROI 收窄到只剩纸币 2,049 → None（余额未识别），绝不返回 2049。"""
    assert ocr.balance_from_words([_w("2,049", x=82)]) is None


def test_marker_split_into_own_token():
    """OCR 把 `+` 拆成独立 token（`26 +`）也要认最右那组。"""
    words = [_w("2,049", x=83), _w("0+", x=176), _w("26", x=250), _w("+", x=268)]
    assert ocr.balance_from_words(words) == 26


def test_zero_marked_currency():
    assert ocr.balance_from_words([_w("2,049"), _w("0+", x=176), _w("0+", x=256)]) == 0


def test_no_marker_returns_none():
    """一组 `+` 都没有 → 结构不明，不猜。"""
    assert ocr.balance_from_words([_w("2,049"), _w("8")]) is None


def test_garbage_marked_token_returns_none():
    assert ocr.balance_from_words([_w("2,049"), _w("à+", x=200)]) is None
    assert ocr.balance_from_words([]) is None


def test_thousands_marked_currency():
    """带千分位的三角币（>999）也要能读。"""
    assert ocr.balance_from_words([_w("0+"), _w("1,234+", x=200)]) == 1234


# ---------------------------------------------------------------- 引擎接线


class FakeScreen:
    def __init__(self, frame=None):
        self._frame = frame if frame is not None else np.zeros((60, 400, 3), np.uint8)

    def grab(self, box=None):
        return self._frame

    def norm_to_px(self, box):
        return (1500, 0, 400, 60)

    def size(self):
        return 1920, 1080

    base_rect = {"left": 0, "top": 0, "width": 1920, "height": 1080}


def test_engine_balance_uses_words_not_last_number(monkeypatch):
    """引擎走的必须是词框解析：同帧 2,049 / 0+ / 8+ → 8。"""
    monkeypatch.setattr(engine_mod.ocr, "read_words",
                        lambda *a, **k: [_w("2,049", x=83), _w("0+"), _w("8+")])
    assert engine_mod.Engine._balance(None, FakeScreen()) == 8


def test_engine_balance_unreadable_returns_none(monkeypatch):
    """反证：ROI 变宽/变窄只剩纸币 → None，绝不给错误数字。"""
    monkeypatch.setattr(engine_mod.ocr, "read_words",
                        lambda *a, **k: [_w("2,049", x=83)])
    assert engine_mod.Engine._balance(None, FakeScreen()) is None


def test_engine_balance_grab_failure_returns_none(monkeypatch):
    monkeypatch.setattr(engine_mod.ocr, "read_words", lambda *a, **k: [])

    class BadScreen(FakeScreen):
        def grab(self, box=None):
            return None

    assert engine_mod.Engine._balance(None, BadScreen()) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
