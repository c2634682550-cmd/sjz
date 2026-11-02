"""三角洲行动 武器型号 → 枪种 的对照表（查资料整理）。

来源：百科「枪械武器」页（wiki.bittopup.com/zh-CN/deltaforce/guns），
收录 57 把枪械：冲锋枪 10、手枪 7、突击步枪 19、狙击步枪 4、精确射手步枪 9、
轻机枪 4、霰弹枪 5。

**为什么要有这张表**：游戏里皮肤名的结构是「型号 + 枪种 + · + 皮肤名」
（如「M4A1突击步枪·棱镜攻势」），而采集时 OCR 认不准型号
（AS Val→「ASVa」、SV-98→「SV」、勇士→「U乙」、SCAR-H→「SCARH」）。
拿这张表去对齐，型号和枪种就都准了 —— 枪种直接决定「哪个枪种」这一维。

枪种用**游戏里皮肤名的叫法**（百科写「步枪/精确射手步枪」，游戏皮肤名
写「突击步枪/战斗步枪/通用机枪/射手步枪/精确射手步枪」），两边要分清。
"""
import re

# 型号 → 游戏里的枪种
WEAPONS = {
    # 突击步枪
    "M4A1": "突击步枪",
    "AKM": "突击步枪",
    "QBZ95-1": "突击步枪",
    "AKS-74U": "突击步枪",
    "ASh-12": "战斗步枪",
    "K416": "突击步枪",
    "M16A4": "突击步枪",
    "AUG": "突击步枪",
    "M7": "战斗步枪",
    "SG552": "突击步枪",
    "AK-12": "突击步枪",
    "SCAR-H": "战斗步枪",
    "G3": "战斗步枪",
    "PTR-32": "突击步枪",
    "CAR-15": "突击步枪",
    "AS Val": "突击步枪",
    "191式": "突击步枪",
    "MDR": "突击步枪",
    # 冲锋枪
    "MP5": "冲锋枪",
    "P90": "冲锋枪",
    "Vector": "冲锋枪",
    "UZI": "冲锋枪",
    "野牛": "冲锋枪",
    "SMG-45": "冲锋枪",
    "SR-3M": "冲锋枪",
    "勇士": "冲锋枪",
    "MP7": "冲锋枪",
    # 霰弹枪
    "M1014": "霰弹枪",
    "S12K": "霰弹枪",
    "M870": "霰弹枪",
    # 轻机枪
    "PKM": "通用机枪",
    "M249": "轻机枪",
    "M250": "通用机枪",
    # 精确射手步枪
    "Mini-14": "射手步枪",
    "VSS": "精确射手步枪",
    "SVD": "精确射手步枪",
    "M14": "射手步枪",
    "SKS": "射手步枪",
    "SR-25": "射手步枪",
    "PSG-1": "射手步枪",
    # 狙击步枪
    "SV-98": "狙击步枪",
    "R93": "狙击步枪",
    "M700": "狙击步枪",
    "AWM": "狙击步枪",
    # 手枪
    "QSZ92G": "手枪",
    ".357左轮": "手枪",
    "沙漠之鹰": "手枪",
    "G18": "手枪",
    "93R": "手枪",
    "G17": "手枪",
    "M1911": "手枪",
    "腾龙": "突击步枪",
    "KC17": "突击步枪",
    "MK4": "冲锋枪",
    "QCQ-171": "冲锋枪",
    "UMP45": "冲锋枪",
    "SR-3M": "冲锋枪",
    # 下面几个是 2026-10-24 换源数据后补的（旧目录采的时候没认出来，
    # 型号名是 OCR 残骸，型号表里自然就没有）
    "QCQ171": "冲锋枪",     # QCQ-171 的另一种写法（游戏里两种都有）
    "SR9": "射手步枪",
    "ASH-12": "战斗步枪",   # ASh-12 的大小写两种写法
    "K437": "突击步枪",
    "QJB201": "轻机枪",
    "FS-12": "霰弹枪",
    "725": "霰弹枪",
}

# 游戏里皮肤名的枪种叫法（表里的值都是这几个之一）
GUN_KINDS = ("突击步枪", "战斗步枪", "射手步枪", "精确射手步枪", "狙击步枪",
             "冲锋枪", "霰弹枪", "轻机枪", "通用机枪", "手枪")

# 筛选面板里的**七档**枪种（页面上的叫法）。皮肤名的枪种更细
# （突击步枪/战斗步枪分得开），筛选面板只有「步枪」一档 —— 两边要能对上。
FILTER_GUNS = ("步枪", "冲锋枪", "霰弹枪", "轻机枪",
               "精准射手步枪", "狙击步枪", "手枪")

# 细枪种 → 筛选面板那一档
FILTER_GUN = {
    "突击步枪": "步枪",
    "战斗步枪": "步枪",
    "射手步枪": "精准射手步枪",
    "精确射手步枪": "精准射手步枪",
    "狙击步枪": "狙击步枪",
    "冲锋枪": "冲锋枪",
    "霰弹枪": "霰弹枪",
    "轻机枪": "轻机枪",
    "通用机枪": "轻机枪",
    "手枪": "手枪",
}


def filter_gun(fine):
    """细枪种（皮肤名的叫法）→ 筛选面板那一档。"""
    if not fine:
        return ""
    return FILTER_GUN.get(fine, fine)


def fix_weapon(token):
    """把 OCR 出来的型号对齐到真实型号，返回 (型号, 枪种)；对不上返回 (原样, "")。

    三步：完全相等 → 忽略大小写/连字符/空格 → 模糊（认错的字母、
    数字和形近字都能对上：「ASVa」→「AS Val」、「MISA4」→「M16A4」、
    「MI引4」→「M1014」）。
    """
    if not token:
        return "", ""
    t = str(token).strip()
    if t in WEAPONS:
        return t, WEAPONS[t]
    loose = {_key(k): (k, v) for k, v in WEAPONS.items()}
    key = _key(t)
    if key in loose:
        return loose[key]
    # 模糊：OCR 常把字母认错、把数字认成字母（16 → IS）。
    # 相似度之外再按长度差打个折 —— 不然「MI引4」会被更短的「M14」抢走。
    best, best_score = None, 0.0
    for k, v in WEAPONS.items():
        for a in _variants(t):
            for b in _variants(k):
                sim = _similar(a, b)
                score = sim * (1 - abs(len(a) - len(b)) / max(len(a), len(b), 1))
                if score > best_score:
                    best, best_score = (k, v), score
    if best and best_score >= 0.7:
        return best
    return t, ""


# 形近字：OCR 把数字认成字母（或反过来）
_CONFUSE = str.maketrans({
    "0": "o", "1": "l", "2": "z", "5": "s", "6": "g", "8": "b",
    "o": "0", "l": "1", "i": "1", "z": "2", "s": "5", "g": "6", "b": "8",
})


def _key(s):
    """比较用的键：去空格和连字符，转小写。"""
    return re.sub(r"[\s\-]+", "", str(s or "")).lower()


def _variants(s):
    """一个名字的两种写法：原样 + 形近字互换。"""
    base = _key(s)
    other = base.translate(_CONFUSE)
    return {base, other}


def _similar(a, b):
    """字符级相似度（和 ocr.similarity 同一口径）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b)) * 0.5 + 0.5
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / max(len(a), len(b))
