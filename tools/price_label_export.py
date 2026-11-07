# -*- coding: utf-8 -*-
"""把采集到的帧整理成「待标注清单」（CSV + Markdown）。

一行一条**价格样本**（不是一帧）：列表页一帧里可能同时有 6 个价格，
如果按帧一行，人只能填一个真值，其余 5 个就永远进不了分母。
帧编号仍在 `frame` 列里，同一个帧会出现多行 —— 这是有意的，不是错误。

列
----
    sample_id   帧序号#样本序号，唯一
    frame       整帧 PNG（相对采集目录）
    roi         ROI 裁片 PNG
    page        list / detail / settle / other
    cls         价格形态（list_thousands / low_nosep / detail_price / ...）
    ocr_text    OCR 原文（价格块内的拼串）
    parsed      生产解析值（None 表示拒识）
    tier1       同一批词里 field=="price" 的 tier1 解析值
    candidate   预填候选（= parsed，供人核对时少敲键）
    truth       **留空给人填**：真值（纯数字，不带逗号）
    review      自动备注（parsed 为空时提示「疑似拒识，重点核对」）

用法
----
    python tools/price_label_export.py --dir _price_frames
    python tools/price_label_export.py --dir _price_frames --out-csv my_labels.csv
"""
import argparse
import collections
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLS = ["sample_id", "frame", "roi", "page", "cls", "ocr_text",
        "parsed", "tier1", "candidate", "truth", "review"]


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def main():
    ap = argparse.ArgumentParser(description="生成待标注清单")
    ap.add_argument("--dir", default=os.path.join(ROOT, "_price_frames"))
    ap.add_argument("--out-csv", default="")
    ap.add_argument("--out-md", default="")
    args = ap.parse_args()

    d = os.path.abspath(args.dir)
    frames = read_jsonl(os.path.join(d, "frames.jsonl"))
    dups = read_jsonl(os.path.join(d, "dups.jsonl"))
    if not frames:
        print("!! %s 里没有 frames（先跑 price_frame_collect.py --enable）" % d)
        return 2

    rows = []
    for fr in sorted(frames, key=lambda x: x.get("seq") or 0):
        samples = fr.get("samples") or []
        if not samples:
            rows.append({"sample_id": "%06d#-" % (fr.get("seq") or 0),
                         "frame": fr.get("frame", ""), "roi": "",
                         "page": fr.get("page") or "",
                         "cls": "unreadable",
                         "ocr_text": (fr.get("ocr_text") or "")[:120],
                         "parsed": "", "tier1": "", "candidate": "", "truth": "",
                         "review": "整帧没抓到价格样本，重点核对（可能是空列表/遮挡）"})
            continue
        for i, s in enumerate(samples):
            parsed = s.get("value")
            t1 = s.get("tier1")
            note = "拒识，重点核对" if parsed is None else ""
            rows.append({
                "sample_id": "%06d#%d" % (fr.get("seq") or 0, i),
                "frame": fr.get("frame", ""),
                "roi": s.get("roi") or "",
                "page": fr.get("page") or "",
                "cls": s.get("cls") or "",
                "ocr_text": (s.get("text") or "").replace("\n", " ")[:120],
                "parsed": "" if parsed is None else parsed,
                "tier1": "" if t1 is None else t1,
                "candidate": "" if parsed is None else parsed,
                "truth": "",
                "review": note,
            })

    csv_path = os.path.abspath(args.out_csv or os.path.join(d, "labels.csv"))
    md_path = os.path.abspath(args.out_md or os.path.join(d, "labels.md"))
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)

    n_frame = len(frames)
    n_sample = len(rows)
    n_none = sum(1 for r in rows if r["parsed"] == "")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# 价格帧待标注清单\n\n")
        f.write("- 采集目录：`%s`\n- 帧：%d（像素重复丢弃 %d）\n"
                "- 价格样本行：%d（其中拒识 %d）\n\n" % (d, n_frame, len(dups), n_sample, n_none))
        f.write("填法：只改 `truth` 列（纯数字，不带逗号），其余列别动；"
                "整行都是误检的直接删掉该行。填完跑\n\n```\n"
                "python tools/price_label_eval.py --labels \"%s\"\n```\n\n" % csv_path)
        f.write("| sample_id | frame | page | cls | ocr_text | parsed | truth |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write("| %s | %s | %s | %s | %s | %s | ______ |\n" % (
                r["sample_id"], r["frame"], r["page"], r["cls"],
                (r["ocr_text"] or "").replace("|", "\\|")[:60], r["parsed"]))

    print("帧 %d（重复丢弃 %d）｜样本行 %d（拒识 %d）" % (n_frame, len(dups), n_sample, n_none))
    print("CSV:", csv_path)
    print("MD :", md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
