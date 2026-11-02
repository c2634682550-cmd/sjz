r"""OCR 引擎横向评测：同一批真实裁切图，比准确率和延迟。

为什么要单独一个脚本
--------------------
选 OCR 不能看论文指标 —— 我们读的是**游戏界面**：深色底、12px 小字、
金色描边标题、带千位逗号的价格、成色字母。这类图和文档 OCR 的评测集
完全不是一回事（上一轮 PaddleOCR-VL 就是这么翻车的：文档模型认整屏只
认出 7 块，价格整块漏掉）。

所以用真截图、真字段、真判分：
  价格  数字必须一模一样（差一位就是买错东西）
  商品名 归一化后相似度 ≥0.8
  成色  「成色A」这种标签必须原样出现

引擎在不同 venv 里（paddle 装不进打包用的 .venv），所以用子进程跑，
各自吐 JSON，再合并比：

    # PP-OCR 侧（.venv-vl 里跑，有 paddleocr）
    .venv-vl\Scripts\python.exe tools/ocr_eval.py --engine ppocr_v6_small --json %TEMP%\p.json
    # Windows OCR 基线（.venv 里跑）
    .venv\Scripts\python.exe tools/ocr_eval.py --engine winrt --json %TEMP%\w.json
    # 合并出报告
    .venv\Scripts\python.exe tools/ocr_eval.py --compare %TEMP%\w.json %TEMP%\p.json
"""
import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import ocr, vision  # noqa: E402

SHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "shots", "20261031_000707_成交.jpg")

NAME = "QBZ95-1突击步枪-王牌之剑"

# 6 张卡片（2 列 × 3 行）的字段框，坐标是 1380×630 实测值。
# 相对每个卡片的左上角，左右两列只差 x0（0 / 690）。
# 真值是我逐字看原图核过的，不是 OCR 输出回填的。
CARDS = [
    # (x0, y0, 成色, 价格)
    (0,   0,   "A", 25000),
    (690, 0,   "B", 17000),
    (0,   210, "B", 28888),
    (690, 210, "S", 16000),
    (0,   420, "C", 9000),
    (690, 420, "B", 9500),
]

FIELD_BOX = {
    # 框都留了余量：右列的内容比左列偏左十几像素，框紧一点就会把
    # 开头一两个字切掉（第一版就踩了，读成「95-1」）。
    "name":  (6, 2, 285, 42),        # 商品名（金色小字）
    "grade": (2, 160, 155, 38),      # 极品 | 成色X
    "price": (545, 156, 145, 42),    # 右下角价格（带金币图标）
}


def cases():
    """展开成 [{id, field, box, expect, scale}]。

    每个字段都测原尺寸和 2 倍：真机里 engine 就是这么干的（小字必须先放大），
    所以「1 倍不行 2 倍行」也算可用 —— 但不能只测放大，那会掩盖模型本身
    对小字的敏感度。
    """
    out = []
    for i, (x0, y0, grade, price) in enumerate(CARDS):
        for field, (dx, dy, w, h) in FIELD_BOX.items():
            expect = {"name": NAME, "grade": f"成色{grade}", "price": str(price)}[field]
            for scale in (1, 2):
                out.append({"id": f"c{i}.{field}@{scale}x", "field": field,
                            "box": (x0 + dx, y0 + dy, w, h),
                            "expect": expect, "scale": scale})
    # 价格条再测 3 倍（engine._bar_price 的实参）
    for i, (x0, y0, _g, price) in enumerate(CARDS):
        dx, dy, w, h = FIELD_BOX["price"]
        out.append({"id": f"c{i}.price@3x", "field": "price",
                    "box": (x0 + dx, y0 + dy, w, h),
                    "expect": str(price), "scale": 3})
    return out


# ---------------------------------------------------------------- 引擎

def _winrt(img):
    """Windows OCR 基线（对照用）。

    core/ocr_winrt.py 已经删掉了（生产只留 PP-OCR），这里内联一份最小实现，
    只为「换了新引擎到底好多少」这个对比还能复现。
    """
    import cv2
    import winrt.windows.globalization as g11n
    import winrt.windows.graphics.imaging as imaging
    import winrt.windows.media.ocr as wocr
    import winrt.windows.storage.streams as streams

    eng = wocr.OcrEngine.try_create_from_language(g11n.Language("zh-Hans-CN"))
    if eng is None:
        raise SystemExit("本机没有中文 OCR 语言包")
    ok, buf = cv2.imencode(".png", img)
    stream = streams.InMemoryRandomAccessStream()
    writer = streams.DataWriter(stream)
    writer.write_bytes(buf.tobytes())
    writer.store_async().get()
    writer.flush_async().get()
    stream.seek(0)
    decoder = imaging.BitmapDecoder.create_async(stream).get()
    bitmap = decoder.get_software_bitmap_async().get()
    res = eng.recognize_async(bitmap).get()
    text = "\n".join(line.text for line in res.lines)
    return text, [line.text for line in res.lines]


def _ppocr_engine(mobile=False):
    from paddleocr import PaddleOCR
    kw = {}
    if mobile:
        kw = dict(text_detection_model_name="PP-OCRv5_mobile_det",
                  text_recognition_model_name="PP-OCRv5_mobile_rec")
    return PaddleOCR(ocr_version="PP-OCRv5", lang="ch",
                     use_doc_orientation_classify=False,
                     use_doc_unwarping=False,
                     use_textline_orientation=False, **kw)


def _ppocr(eng, img):
    txts = []
    for res in eng.predict(img):
        data = res.json if hasattr(res, "json") else {}
        data = data.get("res", data) if isinstance(data, dict) else {}
        txts.extend(data.get("rec_texts") or [])
    return "\n".join(txts), txts


def _http_engine():
    """走 HTTP 服务，和程序里实际用的那条路一致。"""
    from core import ocr_ppocr
    b = ocr_ppocr.make({"mode": "http", "timeout_s": 30,
                        "url": os.environ.get("PPOCR_URL", "http://127.0.0.1:8081")})
    if not b.available():
        raise SystemExit(f"OCR 服务没起来：{b.url}")
    return b


def _http_call(b, img):
    b._cache = type(b._cache)(0)          # 计时别看缓存
    return b.read_text(img), [l["text"] for l in b.read_words(img)]


def _ppocr_named(base):
    """按模型名建引擎：PP-OCRv6_tiny / PP-OCRv6_small / PP-OCRv6_medium …"""
    def make():
        from paddleocr import PaddleOCR
        return PaddleOCR(text_detection_model_name=f"{base}_det",
                         text_recognition_model_name=f"{base}_rec",
                         use_doc_orientation_classify=False,
                         use_doc_unwarping=False,
                         use_textline_orientation=False)
    return make


ENGINES = {
    # 名字 → (建实例的工厂, 调用它拿文本的函数)
    # Windows OCR 基线（对照）
    "winrt": (lambda: None, _winrt),
    "ppocr": (_ppocr_engine, _ppocr),
    # 同一套 pipeline 换 mobile 权重：快 3~5 倍，看小字掉多少
    "ppocr_mobile": (lambda: _ppocr_engine(mobile=True), _ppocr),
    # 走本机服务（和程序里实际用的那条路完全一致），用来验产线配置
    "ppocr_http": (_http_engine, _http_call),
    # PP-OCRv6 三档：v6 是更新的版本，之前只测了 v5，这里补齐
    "ppocr_v6_tiny": (_ppocr_named("PP-OCRv6_tiny"), _ppocr),
    "ppocr_v6_small": (_ppocr_named("PP-OCRv6_small"), _ppocr),
    "ppocr_v6_medium": (_ppocr_named("PP-OCRv6_medium"), _ppocr),
}


def run(engine_name, repeat=3):
    make, call = ENGINES[engine_name]     # 取工厂函数，不是取实例
    inst = make() if make else None
    img_all = vision.imread(SHOT)
    if img_all is None:
        raise SystemExit(f"读不到测试截图：{SHOT}")

    rows = []
    for c in cases():
        x, y, w, h = c["box"]
        crop = img_all[y:y + h, x:x + w]
        if c["scale"] > 1:
            crop = vision.upscale(crop, c["scale"])
        times = []
        text = ""
        for _ in range(repeat):
            t0 = time.perf_counter()
            text, _txts = call(inst, crop) if inst is not None else call(crop)
            times.append((time.perf_counter() - t0) * 1000.0)
        rows.append({"id": c["id"], "field": c["field"], "expect": c["expect"],
                     "got": ocr.clean(text), "ms": statistics.median(times)})
    return {"engine": engine_name, "rows": rows}


# ---------------------------------------------------------------- 判分

def score(row):
    got, want = row["got"], row["expect"]
    if row["field"] == "price":
        return ocr.last_number(got) == int(want)
    if row["field"] == "grade":
        return ocr.normalize(want) in ocr.normalize(got)
    return ocr.similarity(got, want) >= 0.8


def compare(paths, verbose=True):
    data = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data.append(json.load(f))

    cols = [("name", 1), ("name", 2), ("grade", 1), ("grade", 2),
            ("price", 1), ("price", 2), ("price", 3)]
    head = "".join(f"{f[:4]}@{s}x".rjust(9) for f, s in cols)
    print(f"{'引擎':<13}{'总准确':>9}{head}{'中位延迟':>11}")
    print("-" * (13 + 9 + 9 * len(cols) + 11))
    for d in data:
        rows = d["rows"]
        ok = sum(1 for r in rows if score(r))
        cells = ""
        for f, s in cols:
            sel = [r for r in rows if r["field"] == f and r["id"].endswith(f"@{s}x")]
            cells += f"{sum(1 for r in sel if score(r))}/{len(sel)}".rjust(9)
        ms = statistics.median([r["ms"] for r in rows])
        print(f"{d['engine']:<13}{f'{ok}/{len(rows)}':>9}{cells}{ms:>9.0f}ms")

    if not verbose:
        return
    # 错在哪：逐条列出没对上的，比总分有用
    print("\n没对上的（只列 2x 及 3x，1x 全错很正常）：")
    for d in data:
        bad = [r for r in d["rows"] if not score(r) and not r["id"].endswith("@1x")]
        if not bad:
            continue
        print(f"  [{d['engine']}]")
        for r in bad:
            print(f"    {r['id']:<16} 期望 {r['expect']!r:<12} 得到 {r['got']!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=sorted(ENGINES))
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json", default=None)
    ap.add_argument("--compare", nargs="+", default=None)
    args = ap.parse_args()

    if args.compare:
        compare(args.compare)
        return 0
    if not args.engine:
        ap.error("要么 --engine，要么 --compare")

    out = run(args.engine, args.repeat)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"{args.engine}: {len(out['rows'])} 条 → {args.json}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
