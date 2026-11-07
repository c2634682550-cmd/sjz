"""页面特征判定：枪皮列表 vs 挂饰/近战等拒绝页。

负样本来自真机 050404 前的挂饰页（`neg_charm_page.json` / `list_3*.png`）：
正文含「类型 挂饰」「挂饰炫彩」「拥有该挂饰」，没有任何武器名。
正样本来自真机 050404 帧：正文是 `K437突击步枪-王者之剑` 这类武器名。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import page_check

CHARM_LINES = [
    ("CN UID:186668917734233179499", 28, 7),
    ("曼德尔砖", 157, 66), ("典藏外观", 286, 65), ("典藏挂饰", 416, 65),
    ("交易", 166, 121), ("单据", 299, 121),
    ("芬里尔", 504, 179), ("筛选", 222, 195),
    ("典藏-S12赛季-隐藏款", 465, 237),
    ("芬里尔", 91, 248),
    ("当前市场流通量:999+个", 466, 277),
    ("交易开放时间：08:00-次日05:00", 465, 309),
    ("典藏", 92, 326),
    ("神域忆章主题中的芬里尔挂饰", 466, 335),
    ("类型", 465, 380), ("挂饰", 700, 380),
    ("挂饰炫彩", 465, 420), ("图案模板", 465, 460),
    ("附加配件", 465, 500), ("隐藏特性", 465, 540),
    ("拥有该挂饰：0个", 1618, 820), ("购买", 1658, 871),
    ("剩余：3小时5分", 1600, 860), ("5,201", 1660, 930),
]

WEAPON_LINES = [
    ("曼德尔砖", 157, 66), ("典藏外观", 286, 65), ("典藏挂饰", 416, 65),
    ("交易", 166, 121), ("单据", 299, 121),
    ("K437突击步枪-王者之剑", 100, 150),
    ("MK4冲锋枪-疾光魅影", 100, 210),
    ("AKM突击步枪-群星", 100, 270),
    ("当前市场流通量:999+个", 500, 190),
    ("拥有该皮肤：0个", 1600, 780), ("购买", 1658, 830),
]

LISTING_LINES = WEAPON_LINES + [
    ("在售", 90, 66), ("磨损度", 700, 300), ("崭新出厂", 700, 330),
    ("18,200", 1300, 400), ("9,999", 600, 400),
]

# 成色版挂单（真机 050506 帧）：没有「磨损」二字，只有「极品|成色C」+ 单价。
GRADE_LISTING_LINES = WEAPON_LINES + [
    ("在售", 90, 66), ("极品", 380, 395), ("成色C", 560, 395),
    ("118,888", 1250, 395), ("9,999,999", 1250, 601),
    ("极品", 380, 601), ("成色A", 560, 601),
]

# 六类非目标页的负样本（正文特征，不含武器名）
REJECT_SAMPLES = {
    "charm": CHARM_LINES,
    "melee": [
        ("曼德尔砖", 157, 66), ("典藏外观", 286, 65), ("典藏挂饰", 416, 65),
        ("近战武器", 465, 300), ("战术匕首-赤焰", 100, 200),
        ("近战", 700, 380), ("在售", 90, 121), ("2,100", 1250, 400),
    ],
    "emote": [
        ("典藏外观", 286, 65), ("表情", 465, 300),
        ("动作展示", 465, 420), ("当前市场流通量:12个", 466, 277),
    ],
    "spray": [
        ("典藏外观", 286, 65), ("喷漆", 465, 300),
        ("图案模板", 465, 420), ("1,200", 1250, 400),
    ],
    "card": [
        ("典藏外观", 286, 65), ("名片", 465, 300),
        ("名片背景", 465, 420), ("980", 1250, 400),
    ],
    "action": [
        ("典藏外观", 286, 65), ("动作", 465, 300),
        ("专属动作", 465, 420), ("3,300", 1250, 400),
    ],
}


def _words(lines):
    return [{"text": t, "x": x, "y": y, "w": max(8, len(t) * 12), "h": 24}
            for t, x, y in lines]


def test_charm_page_is_not_weapon_list():
    r = page_check.weapon_list(words=_words(CHARM_LINES))
    assert r["ok"] is False
    assert any("挂饰" in w or "武器名" in w for w in r["why"])


def test_charm_page_is_not_listing_list():
    assert page_check.listing_list(words=_words(CHARM_LINES))["ok"] is False


def test_weapon_page_is_weapon_list():
    r = page_check.weapon_list(words=_words(WEAPON_LINES))
    assert r["ok"] is True, r["why"]
    assert any("K437突击步枪-王者之剑" == n for n in r["names"])


def test_weapon_page_without_wear_is_not_listing():
    assert page_check.weapon_list(words=_words(WEAPON_LINES))["ok"] is True
    assert page_check.listing_list(words=_words(WEAPON_LINES))["ok"] is False


def test_listing_with_wear_and_price_passes():
    r = page_check.listing_list(words=_words(LISTING_LINES))
    assert r["ok"] is True, r["why"]
    assert 18200 in r["prices"]


def test_tab_bar_text_does_not_trigger_reject():
    """标签栏里的「典藏挂饰」不能把枪皮页误杀（050404 实测踩过）。"""
    words = [{"text": "典藏挂饰", "x": 416, "y": 65, "w": 80, "h": 24},
             {"text": "K437突击步枪-王者之剑", "x": 100, "y": 300, "w": 200, "h": 24}]
    r = page_check.weapon_list(words=words)
    assert r["ok"] is True, r["why"]


def test_grade_listing_without_wear_word_passes():
    """挂单页只有「成色 C」，没有「磨损」二字，也必须判成挂单列表。"""
    r = page_check.listing_list(words=_words(GRADE_LISTING_LINES))
    assert r["ok"] is True, r["why"]
    assert any(w.startswith("成色 ") for w in r["wear"]), r["wear"]
    assert 118888 in r["prices"]


def test_wear_hits_normalizes_grade():
    assert page_check.wear_hits("极品 成色B 148,888") == ["成色 B"]
    assert "磨损" in page_check.wear_hits("磨损度 崭新出厂")


def test_reject_pages_each_classified():
    """挂饰/近战/表情/喷漆/名片/动作 六类都不算枪皮页，且能认出是哪一类。"""
    for kind, lines in REJECT_SAMPLES.items():
        ws = _words(lines)
        assert page_check.weapon_list(words=ws)["ok"] is False, kind
        assert page_check.listing_list(words=ws)["ok"] is False, kind
        assert page_check.page_kind(words=ws) == kind, kind


def test_page_kind_on_weapon_and_listing():
    assert page_check.page_kind(words=_words(WEAPON_LINES)) == "weapon_list"
    assert page_check.page_kind(words=_words(GRADE_LISTING_LINES)) == "listing"
