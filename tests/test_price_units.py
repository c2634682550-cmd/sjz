# -*- coding: utf-8 -*-
"""N11：价格解析的数量级 / 跨行回归（2026-11-07）。

故障注入复核判出的 N11：没有货币符号时"取最后一段数字"、解析窗口跨行，
会把 15252 读成 252、把 1234567 读成 567、把 `¥12 345` 换行 `678` 读成
12345678。价格错一个数量级，预算闸门就是误放行/误拦截。

判据只有一条：**要么读到正确值，要么明确拒识（None / ""）**。
不允许出现"读到了一个错值但看起来合理"。

三个入口都走 `core/money.py` 一份语法：
  · `ocr_chain.price_from_text`（OCR 后处理）
  · `market.parse_row`（交易行价格判定）
  · `cards._price_token_in`（卡片价格带）

既有行为（余额行 `2,049 0+ 0 40+` → 40、价格带 `05`、`9.000`）由
`test_numeric_hardening.py` / `test_market.py` / `test_fix_c.py` 守着，
这里只加 N11 的用例和自己的边界。
"""
import pytest

from core import cards, market, money, ocr_chain


# ---------------------------------------------------------------- 三条原案

@pytest.mark.parametrize("text, want", [
    ("15 252", 15252),            # 原：252
    ("1 234 567", 1234567),       # 原：567
    ("¥12 345\n678", 12345),      # 原：12345678（吞了下一行）
])
def test_n11_three_regressions(text, want):
    assert ocr_chain.price_from_text(text)[0] == want
    assert market.parse_row(text)["price"] == want
    assert money.digits_first_line(cards._price_token_in(text)) == str(want)


def test_n11_cross_line_words_stay_separate():
    """词级拼接也不许跨行：同一行用空格，不同行用换行。"""
    words = [
        {"text": "¥12 345", "score": 0.99, "x": 0, "y": 0, "w": 80, "h": 20},
        {"text": "678", "score": 0.99, "x": 0, "y": 40, "w": 30, "h": 20},
    ]
    joined = ocr_chain._join(words)
    assert "¥12 345\n678" == joined
    assert ocr_chain.price_from_text(joined)[0] == 12345


def test_n11_same_line_words_still_join():
    words = [
        {"text": "¥12", "score": 0.99, "x": 0, "y": 4, "w": 40, "h": 20},
        {"text": "345", "score": 0.99, "x": 50, "y": 4, "w": 40, "h": 20},
    ]
    assert ocr_chain._join(words) == "¥12 345"


# ---------------------------------------------------------------- 逐类规则

@pytest.mark.parametrize("text, want", [
    ("¥1,234.5", 1234.5),         # 两位小数是金额
    ("¥12 345", 12345),           # 空格千分位
    ("１２３", 123),               # 全角数字归一
    ("¥ 250,000", 250000),
    ("168,000", 168000),
    ("15 000", 15000),
])
def test_n11_accepts(text, want):
    assert ocr_chain.price_from_text(text)[0] == want


@pytest.mark.parametrize("text", [
    "−¥250",                      # 负号：价格语义反了，拒识
    "-5",
    "1,234 567",                  # 逗号 + 空格混用，拒识
    "12 34",                      # 不是三位分组，拒识
    "1 234.567",                  # 三位小数：磨损/没读全，拒识
    "9,9",
    "1,00,000",
    "1,000,",
    "0.583 172",                  # 磨损 + 数量残片，不猜价格
    "1234567890",                 # 超过 9 位
])
def test_n11_refusals(text):
    assert ocr_chain.price_from_text(text)[0] is None
    assert market.parse_row(text)["price"] is None
    assert cards._price_token_in(text) == ""


def test_n11_leading_zero_is_policy_not_grammar():
    """`025,000`：语法上拒识；价格带/交易行显式 strip 前导 0（金币图标）。
    这条是既有策略（`_price_from_block("025,000") == 25000`），保留。"""
    assert ocr_chain.price_from_text("025,000")[0] is None
    assert market.parse_row("025,000")["price"] == 25000
    assert money.digits_first_line(cards._price_token_in("025,000")) == "25000"


def test_n11_card_price_band_rejects_decimal_money():
    """价格带里出现 `¥1,234.5` 这种小数金额：拒识，不能让下游把点当噪声拼成 12345。"""
    assert cards._price_token_in("¥1,234.5") == ""
    assert money.digits_first_line(cards._price_token_in("¥1,234.5")) == ""


def test_n11_balance_line_keeps_last_field():
    """余额行的空格是字段分隔（有 `+` 标记），取最后一段的行为不变。"""
    assert ocr_chain.price_from_text("2,049 0+ 0 40+")[0] == 40
