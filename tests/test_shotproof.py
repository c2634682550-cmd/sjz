"""水印渲染与后台落盘：不出窗口、不抓真实屏幕。"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import shotproof


def _frame(w=960, h=540):
    # 上半亮、下半暗的合成画面，便于看清水印/通栏
    img = np.zeros((h, w, 3), np.uint8)
    img[: h // 2] = (90, 110, 140)
    img[h // 2:] = (30, 35, 45)
    return img


def _opts(tmp_path, **kw):
    o = {
        "enabled": True,
        "dir": str(tmp_path),
        "watermark": True,
        "keep_raw": True,
        "brand": "三角洲 · 抢购控制台",
        "qgroup": "295941658",
        "uid": "6351828245056138192",
        "avatar": str(tmp_path / "missing_avatar.png"),
        "logo": "",
    }
    o.update(kw)
    return o


def _meta():
    return {
        "item": "AUG突击步枪-天命",
        "tier": "史诗",
        "price": 230,
        "elapsed_ms": 827.300,
        "round": 95,
        "ts": 1793000000.0,
        "uid": "6351828245056138192",
        "badge": "进池",
        "brand": "三角洲 · 抢购控制台",
        "_name": "20261028_162228_AUG突击步枪-天命_230.png",
    }


def test_fonts_are_system_cjk():
    assert shotproof.fonts_available(), "必须能找到 msyh/msyhbd，不能用位图字体"
    f = shotproof.font(32, True)
    assert getattr(f, "path", "").lower().endswith((".ttc", ".ttf"))


def test_file_name_uses_real_values_and_dash():
    import time
    ts = 1793000000.0
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    name = shotproof.file_name({"item": "AUG突击步枪-天命", "price": 230, "ts": ts})
    assert name == f"{stamp}_AUG突击步枪-天命_230.png"
    # 缺项写 ——，不编造
    name2 = shotproof.file_name({"item": "", "price": None, "ts": ts})
    assert "_成交_—.png" in name2


def test_render_layout_and_watermark():
    img = _frame()
    out = shotproof.render(img, _meta(), {
        "watermark": True,
        "brand": "三角洲 · 抢购控制台",
        "qgroup": "295941658",
        "uid": "",
        "avatar": "",
    })
    assert out.shape == img.shape and out.dtype == np.uint8
    # 底部 9% 被压黑
    assert out[int(img.shape[0] * 0.96):, :, :].mean() < 30
    # 斜向水印让上半部分出现轻微亮度纹理（纯色块均值会被抬高一点点）
    top = out[: int(img.shape[0] * 0.45)]
    assert top.std() > 5


def test_worker_writes_png_and_raw(tmp_path):
    says = []
    w = shotproof.Worker(say=lambda m, lv="info": says.append((lv, m)))
    w._grab = lambda rect: _frame()[..., ::-1].copy()   # 假抓屏，给 BGR
    opts = _opts(tmp_path)
    meta = _meta()
    assert w.submit({"left": 0, "top": 0, "width": 960, "height": 540}, meta, opts)
    assert w.drain(3.0)
    name = meta["_name"]
    assert os.path.isfile(tmp_path / name)
    assert os.path.isfile(tmp_path / shotproof.raw_name(name))
    assert says and says[-1][0] == "hit" and "已保存截图" in says[-1][1]
    w.close()


def test_worker_watermark_off_single_file(tmp_path):
    says = []
    w = shotproof.Worker(say=lambda m, lv="info": says.append((lv, m)))
    w._grab = lambda rect: _frame()[..., ::-1].copy()
    assert w.submit({"left": 0, "top": 0, "width": 960, "height": 540},
                    _meta(), _opts(tmp_path, watermark=False))
    assert w.drain(3.0)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 1, "关水印就只存一份原图"
    w.close()


def test_worker_failure_only_reports(tmp_path):
    says = []
    w = shotproof.Worker(say=lambda m, lv="info": says.append((lv, m)))

    def boom(rect):
        raise RuntimeError("grab failed")

    w._grab = boom
    assert w.submit({"left": 0, "top": 0, "width": 1, "height": 1},
                    _meta(), _opts(tmp_path))
    assert w.drain(2.0)
    assert any(lv == "warn" and "截图失败" in m for lv, m in says)
    assert list(tmp_path.iterdir()) == []
    w.close()
