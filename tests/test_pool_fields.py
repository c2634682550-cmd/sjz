# -*- coding: utf-8 -*-
"""入池/抽签证据字段（core/pooltrace.py + engine 接线）回归。

覆盖：
  1. 复核打回的解析串位（拆词数字 / 全角逗号粘连 / 最大点数换行 / 五位数 /
     ZWNJ / 最高点数）；
  2. 判定不许硬猜（等值平局 / 否定句 / 成功率名词 / 单边点数 / 矛盾标记）；
  2b. 第三轮 D1-D5（self 与 max 字段级失效 / max 上界 / 「本轮最大值」变体 /
     数字紧贴「本轮」的字段分界 / 参与人数后缀吞掉整串拆词）；
  3. 台账 10 个新字段（8 事实 + 误差 + 矛盾标记 + 结果来源）严格追加在 tier 之后；
  4. 残影二次读数交叉校验（倒计时与列表直达两条路线）；
  5. 探针开销：读到即停、耗时不计入 elapsed；
  6. 老台账（10 列）与新台账（22 列）混排、轮转后 stats 一致。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from core import engine, pooltrace, records, retention


# ---------------------------------------------------------------- 假屏幕/配置

class FakeScreen:
    """只实现 _probe_toast 用到的两个方法，绝不碰真实屏幕。"""

    size = (1920, 1080)

    def norm_to_px(self, box):
        w, h = self.size
        return (int(box["x"] * w), int(box["y"] * h),
                int(box["w"] * w), int(box["h"] * h))

    def grab(self, box=None):
        if box is None:
            x, y, w, h = 0, 0, *self.size
        else:
            x, y, w, h = box
        return np.zeros((max(1, h), max(1, w), 3), np.uint8)


class FakeCfg:
    def __init__(self, task=None):
        self._d = {"active_task": task or {"name": "测试商品", "quota": 1},
                   "save_shots": False}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def region(self, name):
        return None                      # 没配 toast 区域 → 走 N_TOAST 兜底


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    d = tmp_path / "inbox"
    monkeypatch.setattr(pooltrace, "INBOX_DIR", str(d))
    return d


def _probe(text, monkeypatch, item="曼德尔砖-测试"):
    """跑一次引擎的 toast 探针（合成帧 + 假 OCR），返回证据。"""
    monkeypatch.setattr(engine.ocr, "read_text", lambda img: text)
    eng = SimpleNamespace(_pooltrace=pooltrace.PoolTrace())
    engine.Engine._probe_toast(eng, FakeCfg(), FakeScreen(), item=item)
    return eng._pooltrace.evidence()


# ---------------------------------------------------------------- 六类 toast

SIX_CASES = [
    # (toast 文案, 期望 join_result, 期望 won, 期望 (roll_self, roll_max))
    ("您已加入抢购池，将在抢购玩家名单收集完成后进行随机抽取",
     pooltrace.JOINED, None, (None, None)),
    ("排队已满", pooltrace.QUEUE_FULL, None, (None, None)),
    ("未进入抢购池，请稍后再试", pooltrace.UNKNOWN, None, (None, None)),
    ("未抢购成功，随机点数 ９９ / 本轮最大 １２５", pooltrace.UNKNOWN, False, (99, 125)),
    ("恭喜您抢购成功！随机点数 125 / 本轮最大点数 125", pooltrace.UNKNOWN, True, (125, 125)),
    ("系统提示：请稍后再试", pooltrace.UNKNOWN, None, (None, None)),
]


@pytest.mark.parametrize("text,join,won,rolls", SIX_CASES)
def test_six_toast_categories(text, join, won, rolls, monkeypatch, inbox):
    ev = _probe(text, monkeypatch)
    assert ev["join_result"] == join
    assert ev["won"] is won
    assert (ev["roll_self"], ev["roll_max"]) == rolls
    if join == pooltrace.JOINED:
        assert isinstance(ev["pool_joined_at"], int)
    else:
        assert ev["pool_joined_at"] is None
    # 识别不到的点数必须是 null，绝不能用 0 冒充。
    assert ev["roll_self"] != 0 and ev["roll_max"] != 0


def test_unrecognized_toast_does_not_write_inbox(monkeypatch, inbox):
    ev = _probe("系统提示：请稍后再试", monkeypatch)
    assert ev["join_result"] == pooltrace.UNKNOWN
    assert not inbox.exists() or not list(inbox.iterdir())


def test_recognized_toast_goes_to_evo_inbox(monkeypatch, inbox):
    _probe("排队已满", monkeypatch)
    files = list(inbox.glob("*.jsonl"))
    assert files, "识别到的 toast 应写入 evo 现成的 inbox 旁路"
    row = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert row["text"] == "排队已满"
    assert row["source"] == "toast_ocr"


def test_fullwidth_comma_is_a_separator_not_thousands():
    """全角逗号一律按语义分隔（复核第二轮第 1 条），不能把 99 和 125 粘成 99125。"""
    p = pooltrace.parse_toast("随机点数 99，125 / 本轮最大点数 125")
    assert p["roll_self"] == 99 and p["roll_max"] == 125
    assert p["won"] is False and p["won_source"] == "points"

    p2 = pooltrace.parse_toast("未抢购成功，随机点数 99，125。")
    assert p2["roll_self"] == 99
    assert p2["won"] is False and p2["won_source"] == "text"


def test_ascii_thousands_are_restored_in_chinese_context():
    """只有原始 ASCII 逗号才是千分位：`1,250 人` / `1,250，共 12 人` 都要是 1250。"""
    assert pooltrace.parse_toast("随机点数 1,250 人")["roll_self"] == 1250
    p = pooltrace.parse_toast("随机点数 1,250，共 12 人参与")
    assert p["roll_self"] == 1250
    assert p["join_conflict"] is True       # 单边点数


def test_impossible_roll_combo_keeps_both_values_without_conclusion():
    """self>max 是两条事实互相矛盾，不是把 self 抹掉的理由（复核第三轮 D1）。"""
    p = pooltrace.parse_toast("随机点数 99125 / 本轮最大点数 125")
    assert p["roll_self"] == 99125 and p["roll_max"] == 125
    assert p["won"] is None and p["won_source"] is None
    assert p["join_conflict"] is True


def test_max_phrase_tolerates_reach_word():
    """「您本次最高点数达到 125」不能因为多了「达到」就丢 max（复核第 3 条）。"""
    p = pooltrace.parse_toast("您本次最高点数达到 125")
    assert p["roll_max"] == 125
    assert p["roll_self"] is None


def test_negated_zhongqian_is_a_loss():
    """「没能中签」没带「成功」，也要进否定表（复核第 4 条）。"""
    p = pooltrace.parse_toast("没能中签，随机点数 99")
    assert p["won"] is False and p["won_source"] == "text"


@pytest.mark.parametrize("text,expected", [
    ("随机点数 1 2 3", None),   # 三段单字无锚点 → 歧义，不猜
    ("随机点数 12 5", 125),      # 反向漏拼要补齐
    ("随机点数 1 25", 125),
])
def test_three_segment_ambiguity_and_partial_joins(text, expected):
    assert pooltrace.parse_toast(text)["roll_self"] == expected


def test_rolls_do_not_leak_into_self_from_max_only_toast():
    p = pooltrace.parse_toast("本轮最大点数 125")
    assert p["roll_self"] is None
    assert p["roll_max"] == 125
    assert p["won"] is None
    assert p["join_conflict"] is True       # 只有单边点数 → 矛盾/不确定


# ------------------------------------------------- 复核 1-5：解析串位

def test_ocr_split_single_digits_are_joined():
    """「随机点数 9 9 / 本轮最大点数 1 2 5」→ 99 / 125，判负而不是判赢。"""
    p = pooltrace.parse_toast("随机点数 9 9，本轮最大点数 1 2 5")
    assert (p["roll_self"], p["roll_max"]) == (99, 125)
    assert p["won"] is False


def test_fullwidth_comma_not_swallowed_as_thousands():
    """「随机点数 99，125 人参与本轮抽签」→ self=99，不能粘成 99125。"""
    p = pooltrace.parse_toast("随机点数 99，125 人参与本轮抽签")
    assert p["roll_self"] == 99
    assert p["roll_max"] is None            # 125 是参与人数，不是本轮最大点数
    assert p["won"] is None


def test_max_points_with_newline_is_not_read_as_self():
    """「本轮最大\\n点数 125」→ self=None，max=125（空格换行挡不住旧断言）。"""
    p = pooltrace.parse_toast("本轮最大\n点数 125")
    assert p["roll_self"] is None
    assert p["roll_max"] == 125
    assert p["won"] is None


def test_five_digit_roll_is_not_truncated():
    """「随机点数 12,345」→ 12345（不设四位上限）。"""
    p = pooltrace.parse_toast("随机点数 12,345")
    assert p["roll_self"] == 12345
    assert p["roll_max"] is None
    assert p["join_conflict"] is True


def test_zero_width_joiners_are_stripped():
    """ZWNJ(U+200C)/ZWJ/词连接符夹在数字里也要拼回来。"""
    p = pooltrace.parse_toast(
        "随机点数 9\u200c9，本轮最大点数 1\u200d2\u20605")
    assert p["roll_self"] == 99
    assert p["roll_max"] == 125


def test_highest_points_is_equivalent_to_max_points():
    p = pooltrace.parse_toast("最高点数 125，随机点数 99")
    assert p["roll_self"] == 99
    assert p["roll_max"] == 125


def test_absurd_number_writes_null():
    p = pooltrace.parse_toast("随机点数 99,999,999")
    assert p["roll_self"] is None


# ------------------------------------------------- 复核 6-8：判定不硬猜

def test_equal_rolls_do_not_infer_winner():
    p = pooltrace.parse_toast("随机点数 100 / 本轮最大点数 100")
    assert p["roll_self"] == 100 and p["roll_max"] == 100
    assert p["won"] is None                 # 等值平局不推结论
    assert p["join_conflict"] is False


@pytest.mark.parametrize("text", [
    "未抢购成功，随机点数 99 / 本轮最大点数 125",
    "没能抢购成功",
    "没有抢购成功",
    "并未抢购成功",
])
def test_negated_success_is_never_a_win(text):
    p = pooltrace.parse_toast(text)
    assert p["won"] is False


def test_success_rate_noun_is_not_a_win():
    p = pooltrace.parse_toast("本次抢购成功率 12%")
    assert p["won"] is None
    assert p["matched"] is False


def test_single_sided_points_are_unknown_and_marked():
    p = pooltrace.parse_toast("随机点数 99")
    assert p["roll_self"] == 99
    assert p["roll_max"] is None
    assert p["won"] is None
    assert p["join_conflict"] is True
    assert p["matched"] is False            # 只有单边点数，不发旁路


def test_text_win_with_lower_roll_marks_conflict():
    p = pooltrace.parse_toast(
        "恭喜您抢购成功！随机点数 99 / 本轮最大点数 125")
    assert p["won"] is True                 # 保留「文本说的」
    assert p["join_conflict"] is True       # 同时标出与点数矛盾


def test_text_lose_with_higher_roll_keeps_values_but_no_conclusion():
    p = pooltrace.parse_toast(
        "未抢购成功，随机点数 130 / 本轮最大点数 125")
    assert p["roll_self"] == 130 and p["roll_max"] == 125
    assert p["won"] is None and p["won_source"] is None
    assert p["join_conflict"] is True


# ------------------------------------------------- 复核第三轮 D1-D5

@pytest.mark.parametrize("text,self_,maximum", [
    ("随机点数 999，本轮最大点数 1，250", 999, 1),          # 全角千分位
    ("未抢购成功，随机点数 99，本轮最大点数 12", 99, 12),      # max 漏读一位
    ("随机点数 99，本轮最大点数 1,25", 99, 1),              # 逗号后不是三位
    ("随机点数 99，本轮最大点数 1.25", 99, 1),             # 小数点被当边界
])
def test_self_survives_conflicting_roll_pair(text, self_, maximum):
    """D1：max 被读小也不许带走 self；两值都留，只是不落结论。"""
    p = pooltrace.parse_toast(text)
    assert p["roll_self"] == self_
    assert p["roll_max"] == maximum
    assert p["won"] is None and p["won_source"] is None
    assert p["join_conflict"] is True


@pytest.mark.parametrize("text", [
    "随机点数 99，本轮最大点数 无",           # max 缺位
    "随机点数 99，本轮最大点数 1 2 5人参与",   # max 粘连中文且是人数
    "随机点数 99，本轮最大点数 0",            # max 为 0 → 按缺失
    "随机点数 99，本轮最大点数 ",             # max 为空
    "随机点数 99，本轮最大点数 12,345,678",    # 8 位 → OCR 噪声
])
def test_self_is_never_dropped_when_max_is_unreadable(text):
    """D1 的另一半：max 读不到时，self 必须照样落账。"""
    p = pooltrace.parse_toast(text)
    assert p["roll_self"] == 99
    assert p["roll_max"] is None
    assert p["won"] is None
    assert p["join_conflict"] is True


def test_max_above_ceiling_keeps_both_values():
    """D2：max>1M 与 self>max 走同一套字段级失效，不再「只清 max」。"""
    p = pooltrace.parse_toast("随机点数 99，本轮最大点数 1,200,000")
    assert p["roll_self"] == 99 and p["roll_max"] == 1200000
    assert p["won"] is None and p["won_source"] is None
    assert p["join_conflict"] is True


def test_max_before_self_preserves_self():
    p = pooltrace.parse_toast("本轮最大点数 125，随机点数 99")
    assert p["roll_self"] == 99 and p["roll_max"] == 125
    assert p["won"] is False and p["won_source"] == "points"


def test_max_value_variant_is_recognized_but_not_over_generalized():
    """D3：「本轮最大值 N」落 max；没轮次前缀的「最大值 N」不当点数。"""
    p = pooltrace.parse_toast("未抢购成功，随机点数 99，本轮最大值 125")
    assert p["roll_self"] == 99 and p["roll_max"] == 125
    assert p["won"] is False and p["won_source"] == "text"
    assert pooltrace.parse_toast("本轮最大值暂时无法获取")["roll_max"] is None
    assert pooltrace.parse_toast("最大值 125 仅供参考")["roll_max"] is None


@pytest.mark.parametrize("text", [
    "随机点数 99本轮最大点数 125",
    "随机点数99本轮最大点数125",
    "随机点数 99本轮最大点数 1 2 5",
])
def test_glued_digits_are_split_at_round_prefix(text):
    """D4：数字后紧贴「本轮」就是字段边界，不许拼成 99125。"""
    p = pooltrace.parse_toast(text)
    assert p["roll_self"] == 99 and p["roll_max"] == 125


@pytest.mark.parametrize("text", [
    "随机点数 1 2 3 人参与",
    "随机点数 1 2 5 人参与",
])
def test_trailing_headcount_swallows_whole_split_run(text):
    """D5：整串拆词后面跟参与人数后缀 → 全是人数，一个都不并入点数。"""
    p = pooltrace.parse_toast(text)
    assert p["roll_self"] is None
    assert p["roll_max"] is None


# ------------------------------------------------- 复核 10：单边也落账

def test_single_sided_points_land_in_evidence_even_without_inbox(monkeypatch, inbox):
    ev = _probe("随机点数 99", monkeypatch)
    assert ev["roll_self"] == 99            # 证据照写
    assert ev["won"] is None
    assert ev["join_conflict"] is True
    assert not inbox.exists() or not list(inbox.iterdir())   # 但不灌旁路


# ---------------------------------------------------------------- listed_at

def test_listed_at_uses_first_seen_minus_remaining_with_error():
    seen = 1762500000000
    tr = pooltrace.PoolTrace()
    tr.observe_clock(seen, 40.0, 0.012)
    ev = tr.evidence()
    assert ev["listed_at"] == seen - 40_000
    assert ev["listed_at_err_s"] == pytest.approx(1.012)
    # 公示总时长不可见，没有稳定常量就不硬猜。
    assert ev["publicity_s"] is None


@pytest.mark.parametrize("remaining", [None, 0, -5])
def test_listed_at_is_null_without_usable_clock(remaining):
    tr = pooltrace.PoolTrace()
    tr.observe_clock(1762500000000, remaining, 0.01)
    ev = tr.evidence()
    assert ev["listed_at"] is None
    assert ev["listed_at_err_s"] is None


def test_ghost_frame_second_read_writes_null():
    """第二次读数没跟着掉（残影）→ 宁可 null，不按正常值记账。"""
    seen = 1762500000000
    tr = pooltrace.PoolTrace()
    tr.observe_clock(seen, 40.0, 0.01, second=(seen + 2000, 40.0, 0.01))
    ev = tr.evidence()
    assert ev["listed_at"] is None
    assert ev["listed_at_err_s"] is None


def test_consistent_second_read_keeps_value_and_error():
    seen = 1762500000000
    tr = pooltrace.PoolTrace()
    tr.observe_clock(seen, 40.0, 0.01, second=(seen + 2000, 38.0, 0.01))
    ev = tr.evidence()
    assert ev["listed_at"] == seen - 40_000
    assert ev["listed_at_err_s"] == pytest.approx(1.01, abs=0.02)


def test_detail_route_panel_delay_inflates_error():
    """详情页 0.55s 面板等待要算进误差（scan_lag 由引擎传进来）。"""
    tr = pooltrace.PoolTrace()
    tr.observe_clock(1762500000000, 40.0, 0.55 + 0.01)
    assert tr.evidence()["listed_at_err_s"] == pytest.approx(1.56)


# ---------------------------------------------------------------- 端到端记账

def _fake_engine(trace):
    return SimpleNamespace(
        _pooltrace=trace,
        _save_shot=lambda *a, **k: None,
        _grade_on_hit=lambda *a, **k: {},
        _stats={},
        _repeat_ctx={},
        _followup_used=False,
        _mode="countdown",
        _verified_by="",
        _click_perf=None,
        _zero_perf=None,
        _say=lambda *a, **k: None,
    )


def test_record_appends_ten_fields_after_tier(monkeypatch, inbox):
    seen = 1762500000000
    tr = pooltrace.PoolTrace()
    tr.observe_clock(seen, 40.0, 0.010, second=(seen + 2000, 38.0, 0.010))
    tr.observe_toast("您已加入抢购池，将在抢购玩家名单收集完成后进行随机抽取",
                     at_ms=seen + 5000, name="测试商品")
    tr.observe_toast("未抢购成功，随机点数 99 / 本轮最大 125",
                     at_ms=seen + 20000, name="测试商品")

    monkeypatch.setattr(engine.switches, "get",
                        lambda cfg, key, *a, **k:
                        {"env.record": True, "fire.verify": True}.get(key, False))
    monkeypatch.setattr("core.bricks.find_skin", lambda name: None)

    eng = _fake_engine(tr)
    engine.Engine._record(eng, FakeCfg(), FakeScreen(),
                          item="测试商品", price=230, elapsed=1.2, result="hit")

    row = records.load(1)[0]
    # 既有 10 列的名称与顺序一个字都不能动。
    assert list(row.keys())[:10] == ["ts", "t", "name", "price", "result",
                                     "elapsed", "shot", "mode", "quota", "verified"]
    keys = list(row.keys())
    assert keys[keys.index("tier") + 1:keys.index("tier") + 11] == [
        "pool_joined_at", "join_result", "roll_self", "roll_max", "won",
        "listed_at", "publicity_s", "listed_at_err_s", "join_conflict",
        "won_source"]

    assert isinstance(row["pool_joined_at"], int)
    assert row["join_result"] == "joined"
    assert isinstance(row["roll_self"], int) and row["roll_self"] == 99
    assert isinstance(row["roll_max"], int) and row["roll_max"] == 125
    assert row["won"] is False
    assert isinstance(row["listed_at"], int) and row["listed_at"] == seen - 40_000
    assert row["publicity_s"] is None
    assert isinstance(row["listed_at_err_s"], float)
    assert row["join_conflict"] is False


# ---------------------------------------------------------------- 探针开销

def test_probe_is_read_once_and_short_circuits(monkeypatch, inbox):
    """读到有效 toast 后，后续探针立刻短路，不再花 100ms+ 的 OCR。"""
    cost = 0.14
    monkeypatch.setattr(engine.ocr, "read_text",
                        lambda img: (time.sleep(cost), "已加入抢购池")[1])
    eng = SimpleNamespace(_pooltrace=pooltrace.PoolTrace())
    t0 = time.perf_counter()
    engine.Engine._probe_toast(eng, FakeCfg(), FakeScreen())
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    engine.Engine._probe_toast(eng, FakeCfg(), FakeScreen())
    second = time.perf_counter() - t0
    assert first >= cost
    assert second < 0.005                   # 已读到 → 直接不进 OCR
    assert eng._pooltrace.has_toast() is True


def test_probe_seconds_are_deducted_from_elapsed(monkeypatch, inbox):
    """探针耗时不能抬高 elapsed：扣掉后回到真实抢购耗时。"""
    eng = SimpleNamespace(_probe_s=0.14)
    t_start = time.perf_counter() - 0.50
    got = engine.Engine._elapsed_since(eng, t_start)
    assert got == pytest.approx(0.36, abs=0.03)
    eng._probe_s = 0.0
    got = engine.Engine._elapsed_since(eng, t_start)
    assert got >= 0.49


# ---------------------------------------------------------------- 记账开销

def _book(extra, n):
    t0 = time.perf_counter()
    for _ in range(n):
        records.add(name="测试商品", price=230, result="hit", elapsed=1.2,
                    mode="countdown", quota=1, verified=True, extra=extra)
    return (time.perf_counter() - t0) / n * 1000.0


def test_accounting_overhead_under_5ms(inbox, tmp_path):
    n = 60
    old = {"offset_ms": 12, "tier": "未标注"}
    tr = pooltrace.PoolTrace()
    tr.observe_clock(1762500000000, 40.0, 0.01, second=(1762500002000, 38.0, 0.01))
    tr.observe_toast("未抢购成功，随机点数 99 / 本轮最大 125")
    new = dict(old, **tr.evidence())

    t0 = time.perf_counter()
    for _ in range(1000):
        p = pooltrace.parse_toast("未抢购成功，随机点数 99 / 本轮最大 125")
        pooltrace.derive_listed(1762500000000, 40.0, 0.01,
                                second=(1762500002000, 38.0, 0.01))
        assert p["roll_self"] == 99
    parse_ms = (time.perf_counter() - t0) * 1000.0 / 1000.0

    base_ms = _book(old, n)
    with_ms = _book(new, n)
    delta = with_ms - base_ms
    print(f"\n[pool] 纯解析 {parse_ms:.3f}ms/件；台账单件 {base_ms:.3f} → "
          f"{with_ms:.3f}ms（+{delta:.3f}ms）")
    assert parse_ms < 5.0
    assert delta < 5.0


# ---------------------------------------------------------------- 老台账

def test_old_ledger_without_new_columns_still_loads_stats_and_rotates(
        monkeypatch, tmp_path):
    old_rows = [
        {"ts": 1762400000.0, "t": "11-06 10:00:00", "name": "老记录A",
         "price": 100.0, "result": "hit", "elapsed": 2.0, "shot": "",
         "mode": "countdown", "quota": 1, "verified": True},
        {"ts": 1762400100.0, "t": "11-06 10:01:40", "name": "老记录B",
         "price": 50.0, "result": "miss", "elapsed": 1.5, "shot": "",
         "mode": "list", "quota": 1, "verified": False},
    ]
    p = tmp_path / "records.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in old_rows) + "\n",
                 encoding="utf-8")
    monkeypatch.setattr(records, "PATH", str(p))

    rows = records.load(0)
    assert len(rows) == 2
    st = records.stats(rows)
    assert st["tries"] == 2 and st["hits"] == 1

    # 老行 + 新行（带全套 9 列）混排：老行不被改写，新行完整可读。
    extra = {"offset_ms": 12, "tier": "优品", "pool_joined_at": 1762500000000,
             "join_result": "joined", "roll_self": 99, "roll_max": 125,
             "won": False, "listed_at": 1762499960000, "publicity_s": None,
             "listed_at_err_s": 1.01, "join_conflict": False}
    records.add(name="新记录C", price=30, result="hit", elapsed=1.2, extra=extra)
    rows = records.load(0)
    assert len(rows) == 3
    assert rows[0]["name"] == "新记录C"        # load 新的在前
    assert rows[0]["roll_self"] == 99 and rows[0]["join_conflict"] is False
    assert "roll_self" not in rows[-1] and "roll_self" not in rows[-2]
    assert records.stats(rows)["tries"] == 3 and records.stats(rows)["hits"] == 2

    # 轮转：把阈值压到 1 字节，追加必须把它转成 .1 且旧内容仍可读。
    monkeypatch.setattr(retention, "RECORDS_MAX_BYTES", 1)
    records.add(name="新记录D", price=31, result="miss", elapsed=1.0,
                extra={"pool_joined_at": None, "join_result": "queue_full"})
    archives = retention.archives(str(p), retention.RECORDS_KEEP)
    assert archives, "超过阈值的台账必须轮转"
    rows = records.load(0)
    stats = records.stats(rows)
    assert stats["tries"] == len(rows)          # 轮转后统计口径不丢行
    assert stats["tries"] == 4 and stats["hits"] == 2
    assert any(r.get("name") in ("老记录A", "老记录B") for r in rows)
    assert any(r.get("name") == "新记录C" and r.get("roll_self") == 99
               for r in rows)


# ---------------------------------------------------------------- 消费方兼容

def test_telemetry_whitelist_drops_unknown_fields_without_rejecting():
    from tools import telemetry_agent as ta
    row = {"ts": 1762500000.0, "name": "测试商品", "price": 230, "result": "hit",
           "elapsed": 1.2, "mode": "countdown", "quota": 1, "verified": True,
           "offset_ms": 12, "tier": "未标注", "join_result": "joined",
           "pool_joined_at": 1762500000000, "roll_self": 99, "roll_max": 125,
           "won": False, "listed_at": 1762499960000, "publicity_s": None,
           "listed_at_err_s": 1.01, "join_conflict": False}
    ev = ta._event(row)
    assert ev is not None
    assert set(ev.keys()) == set(ta.EVENT_KEYS)   # 白名单逐字一致，不会被服务端整批拒收


def test_evo_ledger_parser_ignores_extra_fields_and_inbox_toast_is_readable(
        monkeypatch, inbox):
    from evo.collect import parse as ep
    row = {"ts": 1762500000.0, "t": "11-07 08:00:00", "name": "测试商品",
           "price": 230, "result": "hit", "elapsed": 1.2, "shot": "",
           "mode": "countdown", "quota": 1, "verified": True,
           "offset_ms": 12, "tier": "未标注", "join_result": "joined",
           "pool_joined_at": 1762500000000, "roll_self": 99, "roll_max": 125,
           "won": False, "listed_at": 1762499960000, "publicity_s": None,
           "listed_at_err_s": 1.01, "join_conflict": False}
    ev = ep.parse_ledger_line(json.dumps(row, ensure_ascii=False),
                              "test:records", 1)
    assert ev is not None and ev["stage_reached"] >= 2   # 不认识的新列不会让解析崩

    _probe("未抢购成功，随机点数 99 / 本轮最大 125", monkeypatch)
    line = next(inbox.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()[0]
    st = ep.parse_toast_line(line, "inbox/test.jsonl", 1)
    assert st is not None
    assert st.get("roll_self") == 99
    # 「本轮最大 125」被旁路规范成 evo 认得的「本轮最大点数 125」，
    # 所以它不改代码也能算出 roll_max / won。
    assert st.get("roll_max") == 125
    assert st.get("won") is False
