"""pytest 公共夹具。

OCR 只剩一个后端了（PaddleOCR PP-OCRv6，走本机服务），而 tests/ 里有一批
用例是「拿录屏帧跑真 OCR，再断言解析结果」—— 它们必须有 OCR 才能跑。

所以这里做两件事：
  1. 整个会话开始前问一次 OCR 服务在不在；
  2. 不在的话，把「依赖真 OCR」的模块整体 skip，并说清原因。

为什么是 skip 而不是假装通过：那些用例断言的是真实识别结果（`2,001` 被
读坏时要返回 None 之类），没有真 OCR 就没有意义。**跳过是诚实的，通过是
骗人的。** 起服务的方法见 说明.txt 第九节。
"""
import os

import pytest


def _relax_timeout(ocr):
    """测试里把 OCR 超时放宽到 30 秒。

    真截图用例断言的是「认得出什么」，不是「多快」。游戏正占着显卡时，
    一次识别偶尔会超过生产用的 5 秒 —— 那会让用例随机拿到空结果，看起来
    像识别坏了。生产超时另有考虑（抢购等不起），别在这里跟着一起卡。
    """
    s = dict(ocr._settings or {})
    s["ppocr"] = {**s.get("ppocr", {}), "timeout_s": 30.0}
    ocr._settings = s
    ocr._backend = None


def _smoke(ocr):
    """真认一次字：能认出来才跑真截图用例。

    只探 TCP 不够 —— 服务活着但推理慢到超时的时候，用例会随机拿到空结果，
    看起来像「识别坏了」，其实是显卡被游戏占满了。这种情况整组跳过，
    并在原因里写清楚，比给一个假失败有用。
    """
    try:
        from core import vision
        img = vision.imread(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "fixtures", "market", "list_unfiltered.png"))
        if img is None:
            return True                      # 没有素材就不拦
        words = ocr.read_words(img)
        return bool(words)
    except Exception:                        # noqa: BLE001
        return False


# 这些模块不注入假 OCR，直接对录屏帧跑真识别
NEEDS_REAL_OCR = {
    "test_popup.py",
    "test_market_video.py",
    "test_engine_insufficient.py",
    # 典藏外观页 / 两套筛选面板的真截图回放（2026-11-02 补）
    "test_skin_ocr_real.py",
}


@pytest.fixture(autouse=True)
def _keep_user_data_clean(tmp_path_factory, monkeypatch):
    """测试**绝不**往用户真实的 data/ 里写东西。

    为什么必须放在这里：`engine.DATA_DIR`（成交截图）和 `records.PATH`
    （成交记录）都是 **import 时就算死**的常量 —— 只 monkeypatch
    `config.DATA_DIR` 对它们无效。于是任何走到成功分支的用例都会在
    `data/records.jsonl` 里留下一条「成交」、在 `data/shots/` 里留一张
    100×100 的黑图。实测真实记录里混着 `elapsed=200350`（那是开机秒数，
    因为用例传了 `t_start=0.0`）、名字为空的假成交 —— 和真成交混在一起，
    事后根本分不清哪条是真的。

    放在 conftest 里而不是每个用例各写一遍：漏一个就会重新开始污染。
    """
    from core import engine as engine_mod
    from core import records

    d = tmp_path_factory.mktemp("userdata")
    monkeypatch.setattr(engine_mod, "DATA_DIR", str(d))
    monkeypatch.setattr(records, "PATH", str(d / "records.jsonl"))
    yield


def pytest_collection_modifyitems(config, items):
    from core import ocr

    if ocr.available():
        _relax_timeout(ocr)
        if _smoke(ocr):
            return
    if ocr.available():
        why = "服务活着但这次没认出字（多半是显卡被游戏占满，识别超时）"
    else:
        why = ocr.backend_name()
    skip = pytest.mark.skip(reason=f"需要可用的 PP-OCR：{why}")
    for item in items:
        if item.path.name in NEEDS_REAL_OCR:
            item.add_marker(skip)
