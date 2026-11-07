"""余额不足弹窗识别。

数据来源：录屏第 76 帧（1920x1080）裁出的 `insufficient.png`，
裁切偏移 (640, 330)。`docs/auto-buy-design.md` §4.6 记了真实按钮位置。

这个模块存在的唯一理由是**别点错按钮**：弹窗右边是「充值」，点下去花真钱。
所以除了识别本身，这里还要钉住「永远不给出充值按钮的坐标」。
"""
import os
import time

import pytest

from core import popup
from core.vision import imread

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "market")

# 录屏帧的裁切偏移：fixture 坐标 + 这个偏移 = 1920x1080 全屏坐标
CROP = (640, 330)


def _load(name):
    img = imread(os.path.join(FIXTURES, name))
    assert img is not None, f"{name} 读不出来"
    return img


@pytest.fixture(scope="module")
def result():
    return popup.scan(_load("insufficient.png"))


# ---------------- 识别 ----------------

def test_recognises_the_real_popup(result):
    assert result["found"] is True
    assert result["kind"] == "insufficient"


def test_reads_the_recommended_top_up_amount(result):
    """「推荐充值 ¥ 328 档位」里的 328 要读出来 —— 这行字大，OCR 稳。"""
    assert result["recommend"] == 328


def test_shortfall_is_none_when_ocr_mangles_the_number():
    """读不准的缺失金额必须是 None，不能猜。

    录屏里 `2,001` 被 OCR 读成 `2 ， 〔 〕 1 〔 〕`。凑出来的「21」比不报更糟：
    用户会以为只缺 21 币，于是反复重试而不去充值。

    2026-11-01 起 OCR 换成 PP-OCRv6，录屏那一帧已经能读对（shortfall=2001），
    所以这里不再指望「引擎恰好读错」—— 直接喂一份读坏的词表，钉的是解析层
    的判断：带括号/逗号残渣的一律当没读到。
    """
    words = [
        {"x": 640, "y": 470, "w": 300, "h": 20, "text": "您当前的三角币不足，是否前往充值？"},
        {"x": 640, "y": 500, "w": 60, "h": 20, "text": "（缺少"},
        {"x": 700, "y": 500, "w": 20, "h": 20, "text": "2"},
        {"x": 720, "y": 500, "w": 80, "h": 20, "text": "，〔〕1〔〕"},
        {"x": 800, "y": 500, "w": 60, "h": 20, "text": "三角币）"},
    ]
    res = popup.detect(words)
    assert res["found"] is True, "词表里明明有「三角币不足」，却没认出弹窗"
    assert res["shortfall"] is None


# ---------------- 按钮定位 ----------------

def test_cancel_point_matches_the_frame_it_came_from(result):
    """「取消」定位到 (160, 344)，换回全屏是 (800, 674)。

    录屏实测「取消」按钮 x 782-819 / y 665-683（§4.6），中心约 (800, 674)。
    """
    pt = popup.cancel_point(result)
    assert pt is not None
    x, y = pt[0] + CROP[0], pt[1] + CROP[1]
    assert 782 <= x <= 819, f"取消按钮 x={x} 不在实测范围 782-819"
    assert 665 <= y <= 683, f"取消按钮 y={y} 不在实测范围 665-683"


def test_cancel_is_left_of_recharge(result):
    """「取消」在左、「充值」在右。位置关系反了说明拆字或排序出了问题。"""
    btns = result["buttons"]
    assert btns["cancel"]["cx"] < btns["recharge"]["cx"]


def test_never_offers_the_top_up_button_as_a_click_target(result):
    """核心安全约束：对外只暴露「取消」，且它必须离「充值」足够远。

    `cancel_point()` 是调用方唯一能拿到的点击点。如果哪天有人把它改成
    「取 buttons 里第一个」或「按颜色找绿色按钮」，这条会立刻失败 ——
    因为「充值」是绿的（#33BA82），「取消」是灰的。
    """
    pt = popup.cancel_point(result)
    assert pt is not None
    recharge = result["buttons"]["recharge"]
    dist = abs(pt[0] - recharge["cx"])
    assert dist > 100, f"取消点离充值按钮只有 {dist}px，太近了"


# ---------------- 不误判 ----------------

def test_list_page_is_not_mistaken_for_the_popup():
    """交易行列表页没有「三角币不足」，不能被认成弹窗。

    误判的后果是程序在正常列表页上点一个不存在的「取消」，
    或者更糟——停在那里不动，用户以为在扫货其实什么都没做。
    """
    r = popup.scan(_load("list_unfiltered.png"))
    assert r["found"] is False
    assert popup.cancel_point(r) is None


def test_empty_input_is_not_a_popup():
    assert popup.detect(None)["found"] is False
    assert popup.detect([])["found"] is False
    assert popup.cancel_point(None) is None


def test_text_without_cancel_button_still_reports_the_popup():
    """只读到正文、没读到按钮行时：仍要说「是弹窗」，但点击点是 None。

    调用方据此停下来喊人，而不是退回某个默认坐标去点 —— 默认坐标可能
    正好落在「充值」上。
    """
    words = [
        {"x": 100, "y": 100, "w": 20, "h": 20, "text": "三"},
        {"x": 120, "y": 100, "w": 20, "h": 20, "text": "角"},
        {"x": 140, "y": 100, "w": 20, "h": 20, "text": "币"},
        {"x": 160, "y": 100, "w": 20, "h": 20, "text": "不"},
        {"x": 180, "y": 100, "w": 20, "h": 20, "text": "足"},
    ]
    r = popup.detect(words)
    assert r["found"] is True
    assert popup.cancel_point(r) is None


# ---------------- 拆字 ----------------

def test_glued_button_labels_are_split_into_separate_buttons():
    """「取消」和「充值」被 OCR 粘成一个词时，仍要能分出两个按钮。

    Windows OCR 一般一字一词，但实测里确实出现过粘连。按字数均分宽度是
    估算，位置会有偏差，所以这里只断言「分开了、左消右充」，不断言精确像素。
    """
    words = [{"x": 100, "y": 300, "w": 200, "h": 20, "text": "取消充值"}]
    r = popup.detect([
        {"x": 100, "y": 100, "w": 20, "h": 20, "text": "三"},
        {"x": 120, "y": 100, "w": 20, "h": 20, "text": "角"},
        {"x": 140, "y": 100, "w": 20, "h": 20, "text": "币"},
        {"x": 160, "y": 100, "w": 20, "h": 20, "text": "不"},
        {"x": 180, "y": 100, "w": 20, "h": 20, "text": "足"},
    ] + words)
    assert r["found"] is True
    btns = r["buttons"]
    assert btns is not None, "粘连的「取消充值」没被拆开"
    assert btns["cancel"]["cx"] < btns["recharge"]["cx"]


def test_cancel_inside_a_longer_word_is_still_found():
    """「取消充值」这种粘连词里，「取消」是前两个字，不能因为整词不等就漏掉。"""
    words = [
        {"x": 50, "y": 100, "w": 20, "h": 20, "text": "三"},
        {"x": 70, "y": 100, "w": 20, "h": 20, "text": "角"},
        {"x": 90, "y": 100, "w": 20, "h": 20, "text": "币"},
        {"x": 110, "y": 100, "w": 20, "h": 20, "text": "不"},
        {"x": 130, "y": 100, "w": 20, "h": 20, "text": "足"},
        {"x": 50, "y": 400, "w": 160, "h": 20, "text": "取消充值"},
    ]
    assert popup.detect(words)["buttons"] is not None


# ---------------- 性能 ----------------

def test_detection_is_fast_enough_for_the_buy_loop(perf_machine):
    """买完一次要立刻查弹窗。识别超过 300ms 就会明显拖慢重试节奏。

    实测本机约 50ms（弹窗区域 690x400）。

    取 7 次的中位数，阈值仍是 300ms：单次墙钟会被操作系统调度尖刺
    （本机常有别的 pytest 在跑）放大到几百毫秒，中位数才是这段逻辑
    的稳定成本。不用 process_time：OCR 内部是多线程，CPU 时间会把
    所有工作线程累加，反而恒大于墙钟、量不出真实响应。
    """
    img = _load("insufficient.png")
    popup.scan(img)          # 预热：首次调用要建 OCR 引擎
    samples = []
    for _ in range(7):
        t = time.perf_counter()
        popup.scan(img)
        samples.append(time.perf_counter() - t)
    samples.sort()
    med = samples[len(samples) // 2]
    assert med < 0.3, (f"识别中位耗时 {med * 1000:.0f}ms，太慢"
                       f"（min {samples[0] * 1000:.0f} / max {samples[-1] * 1000:.0f}ms）")
