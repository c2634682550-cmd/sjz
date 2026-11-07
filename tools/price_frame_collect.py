# -*- coding: utf-8 -*-
"""真机价格帧采集器（**默认关闭**，只有显式 `--enable` 才落帧）。

目的
----
`price_exact` 在真实帧上的 95% 置信下界卡在 0.8754（23 帧 / 27 样本、零错），
瓶颈是**分母**不是准确率。要把它推到 0.98，只能靠真机进交易行跑一段、
逐帧落盘、再离线评。本工具负责「落盘」这一段，只写 tools/，core/ 只读引用。

产出目录（默认项目内 `_price_frames/`，可清理）
--------------------------------------------
    _price_frames/
        frames/         整帧 PNG（像素 sha256 去重，同图只留一份）
        rois/           ROI 裁片 PNG（每帧每个价格样本一张）
        frames.jsonl    一行一帧：时间戳 / 窗口尺寸 / DPI / 页面判定 /
                        OCR 原文 / 解析值 / 每个样本的 roi 与 box
        dups.jsonl      重复帧记录（不进分母，只做可审计的丢弃凭据）
        prune.log       超容量后从最旧删除的记录
        collect.log     每 5s 一行「已采集 N 帧 / 已丢弃 M 帧」
        status.json     与 collect.log 同口径的机器可读状态（界面可直接轮询）

三条硬约束
----------
1. **不阻塞、不抢焦点**：采集线程只做 grab + 投递；编码、OCR、写盘全在
   后台 worker 线程。队列有界（默认 6），满了**丢帧并计数**，producer 永不等待。
2. **频率有上限**：`--interval` 最小 0.5s（≈2 fps 上限），默认 0.8s。
3. **磁盘有上限**：总量超 `--max-bytes`（默认 2GB）时从最旧的帧整组删除。

用法
----
    # 真机采集（默认关闭，必须显式打开）
    python tools/price_frame_collect.py --enable --live --interval 0.8 --max-frames 200
    # 只跑一遍链路（用已有真实截图回放，不碰屏幕）
    python tools/price_frame_collect.py --enable --replay <目录或文件...>
    # 不带 --enable 时什么都不写，只打印一行提示后退出（退出码 2）

后续两步在另外两个脚本里：
    python tools/price_label_export.py --dir _price_frames
    python tools/price_label_eval.py --labels _price_frames/labels.csv

口径（与 tools/ocr_independent_real_eval.py 一致）
------------------------------------------------
* 列表页：`market.scan(frame)` 一次整帧扫描，每行一条价格样本；
* 面板/详情/结算：`ocr.read_words(crop)` → `ocr_chain.price_from_words`；
  tier1 取同一批 words 里 `field=="price"` 的块拼串再 `price_from_text`。
  两者都**只喂一次**，不重复推理、不拿别的字段顶替。
"""
import argparse
import collections
import hashlib
import json
import os
import queue
import signal
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cv2                       # noqa: E402
import numpy as np               # noqa: E402

from core import market, nav, ocr, ocr_chain   # noqa: E402
from core.config import Config                   # noqa: E402

DEFAULT_OUT = os.path.join(ROOT, "_price_frames")
MIN_INTERVAL = 0.5               # 采集频率上限 ≈ 2 fps，再快就真会拖主链路
DEFAULT_INTERVAL = 0.8
DEFAULT_MAX_BYTES = 2 * 1024 ** 3      # 2GB
DEFAULT_QUEUE = 6


def pixel_sha(img):
    """像素级 sha256：同一画面不同压缩/不同文件头也算同一帧。"""
    return hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest()


def imwrite(path, img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        return False
    buf.tofile(path)
    return True


def imread(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _box_px(box):
    try:
        return tuple(int(round(float(box[k]))) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None


def _crop(img, box):
    """box=(x,y,w,h) 像素；越界自动夹紧。返回 (crop, (x0,y0,x1,y1)) 或 (None,None)。"""
    if not box:
        return None, None
    x, y, w, h = box
    H, W = img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None, None
    return img[y0:y1, x0:x1], (x0, y0, x1, y1)


class Stats:
    def __init__(self):
        self.captured = 0        # 已落盘帧数（去重后）
        self.dropped = 0         # 队列满丢弃帧数
        self.dups = 0            # 像素重复帧数（不进分母）
        self.samples = 0         # 价格样本数
        self.pruned = 0          # 因超量删除的帧组数
        self.used_bytes = 0
        self.failed = 0

    def as_dict(self, extra=None):
        d = {"captured": self.captured, "dropped": self.dropped,
             "dups": self.dups, "samples": self.samples, "pruned": self.pruned,
             "used_bytes": self.used_bytes, "used_mb": round(self.used_bytes / 1048576.0, 1),
             "failed": self.failed, "updated": _now()}
        if extra:
            d.update(extra)
        return d


class Collector:
    """有界队列 + 后台 worker：producer 只 grab + put_nowait。"""

    def __init__(self, out_dir, max_bytes=DEFAULT_MAX_BYTES,
                 queue_size=DEFAULT_QUEUE, page_ocr=False):
        self.out = os.path.abspath(out_dir)
        self.frames_dir = os.path.join(self.out, "frames")
        self.rois_dir = os.path.join(self.out, "rois")
        for d in (self.out, self.frames_dir, self.rois_dir):
            os.makedirs(d, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.page_ocr = bool(page_ocr)
        # 产物目录自带 .gitignore：采集数据是临时素材，不该出现在 git status 里
        gi = os.path.join(self.out, ".gitignore")
        if not os.path.exists(gi):
            try:
                with open(gi, "w", encoding="utf-8") as f:
                    f.write("# 采集产物，不进版本库\n*\n")
            except OSError:
                pass
        self.cfg = Config.load()
        self.stats = Stats()
        self.q = queue.Queue(maxsize=max(1, int(queue_size)))
        self._seen = {}                       # pixel_sha -> seq
        self._cache = collections.OrderedDict()   # seq -> {"paths": [...], "bytes": n}
        self._stop = threading.Event()
        self._thread = None
        self._seq = 0
        self._lock = threading.Lock()
        self._fjson = open(os.path.join(self.out, "frames.jsonl"), "a", encoding="utf-8")
        self._djson = open(os.path.join(self.out, "dups.jsonl"), "a", encoding="utf-8")
        self._plog = open(os.path.join(self.out, "prune.log"), "a", encoding="utf-8")
        self._clog = open(os.path.join(self.out, "collect.log"), "a", encoding="utf-8")
        # 断点续采：老 frames.jsonl 里已有帧的像素 sha 先装进去重表
        for line in self._read_jsonl(os.path.join(self.out, "frames.jsonl")):
            try:
                sha = line.get("frame_sha")
                if sha and line.get("frame"):
                    self._seen[sha] = line.get("seq")
                    self.stats.captured += 1
                    self.stats.samples += len(line.get("samples") or [])
            except Exception:            # noqa: BLE001
                continue
        self._rescan_usage()

    @staticmethod
    def _read_jsonl(path):
        out = []
        if not os.path.exists(path):
            return out
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def _rescan_usage(self):
        total = 0
        for d in (self.frames_dir, self.rois_dir):
            try:
                for name in os.listdir(d):
                    p = os.path.join(d, name)
                    try:
                        total += os.path.getsize(p)
                    except OSError:
                        pass
            except OSError:
                pass
        self.stats.used_bytes = total

    # ------------------------------------------------------------ 生命周期
    def start(self):
        self._thread = threading.Thread(target=self._worker, name="price-frames-writer",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=20.0):
        self._stop.set()
        # 用哨兵让 worker 把队列里剩下的写完（最多等 timeout）
        try:
            self.q.put(None, timeout=0.2)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)
        with self._lock:
            for f in (self._fjson, self._djson, self._plog, self._clog):
                try:
                    f.flush()
                    f.close()
                except Exception:        # noqa: BLE001
                    pass
        self._write_status(final=True)

    # ------------------------------------------------------------ producer
    def submit(self, frame, meta):
        """投递一帧。队列满 → 丢帧计数，绝不让 producer 等。"""
        try:
            self.q.put_nowait((frame, meta))
            return True
        except queue.Full:
            self.stats.dropped += 1
            return False

    def status_line(self):
        s = self.stats
        return ("正在采集 %d 帧 / 已丢弃 %d 帧 / 重复 %d / 样本 %d / 磁盘 %.0f MB"
                % (s.captured, s.dropped, s.dups, s.samples,
                   s.used_bytes / 1048576.0))

    def _write_status(self, final=False):
        d = self.stats.as_dict({"final": bool(final), "queue": self.q.qsize()})
        p = os.path.join(self.out, "status.json")
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except OSError:
            pass

    # ------------------------------------------------------------ worker
    def _worker(self):
        last_status = 0.0
        while True:
            try:
                item = self.q.get(timeout=0.3)
            except queue.Empty:
                item = None
            if item is None:
                if self._stop.is_set():
                    break
            else:
                try:
                    self._process(*item)
                except Exception as e:              # noqa: BLE001
                    self.stats.failed += 1
                    self._clog.write("%s worker 失败：%s: %s\n" % (_now(), type(e).__name__, e))
            if time.time() - last_status > 5.0:
                last_status = time.time()
                self._clog.write("%s %s\n" % (_now(), self.status_line()))
                self._clog.flush()
                self._write_status()
        self._write_status(final=True)

    def _process(self, frame, meta):
        if frame is None or getattr(frame, "size", 0) == 0:
            self.stats.failed += 1
            return
        sha = pixel_sha(frame)
        if sha in self._seen:
            self.stats.dups += 1
            self._djson.write(json.dumps({"ts": meta.get("ts"), "frame_sha": sha,
                                          "dup_of": self._seen[sha],
                                          "w": frame.shape[1], "h": frame.shape[0]},
                                         ensure_ascii=False) + "\n")
            self._djson.flush()
            return

        with self._lock:
            self._seq += 1
            seq = self._seq
        base = "f%06d" % seq
        fpath = os.path.join(self.frames_dir, base + ".png")
        if not imwrite(fpath, frame):
            self.stats.failed += 1
            return

        samples, analysis = self._analyze(frame, seq)
        paths = [fpath]
        bytes_used = os.path.getsize(fpath)
        for i, s in enumerate(samples):
            rp = os.path.join(self.rois_dir, "%s_r%d.png" % (base, i))
            if s.get("crop") is not None and imwrite(rp, s["crop"]):
                s["roi"] = os.path.relpath(rp, self.out).replace("\\", "/")
                paths.append(rp)
                bytes_used += os.path.getsize(rp)
            s.pop("crop", None)

        rec = {
            "seq": seq,
            "ts": meta.get("ts") or _now(),
            "route": meta.get("route", "live"),
            "frame": os.path.relpath(fpath, self.out).replace("\\", "/"),
            "frame_sha": sha,
            "w": int(frame.shape[1]), "h": int(frame.shape[0]),
            "base_w": meta.get("base_w"), "base_h": meta.get("base_h"),
            "dpi": meta.get("dpi"), "awareness": meta.get("awareness"),
            "scale": meta.get("scale"),
            "page": analysis.get("page"),
            "page_raw": analysis.get("page_raw"),
            "ocr_text": analysis.get("ocr_text"),
            "samples": samples,
            "interval_s": meta.get("interval_s"),
        }
        if meta.get("src"):
            rec["src"] = meta["src"]

        self._seen[sha] = seq
        self._fjson.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fjson.flush()
        self.stats.captured += 1
        self.stats.samples += len(samples)
        self.stats.used_bytes += bytes_used
        self._cache[seq] = {"paths": paths, "bytes": bytes_used}
        self._enforce_cap()

    # ------------------------------------------------------------ 分析
    def _analyze(self, frame, seq):
        """返回 (samples, analysis)：样本里带 crop，analysis 回填页面判定。

        整屏/横屏截图（宽高比 ≥1.2 且宽 ≥800）才可能是列表页 —— 用宽高比
        而不是绝对分辨率判断，是因为真实素材里有 1380×630 的列表截图，
        按「必须接近 1920×1080」会把它误判成面板、一帧只读出一个价格。
        """
        h, w = frame.shape[:2]
        if w >= 800 and h > 0 and (w / float(h)) >= 1.2:
            return self._analyze_full(frame)
        return self._analyze_panel(frame)

    def _analyze_full(self, frame):
        """整屏帧：先列表扫描，再详情 bar，再结算 confirm。"""
        samples = []
        analysis = {"page": "other", "ocr_text": "", "page_raw": None}
        try:
            rows = market.scan(frame)
        except Exception as e:                # noqa: BLE001
            rows = []
            analysis["scan_error"] = "%s: %s" % (type(e).__name__, e)
        if rows:
            for r in rows:
                val = r.get("price")
                txt = (r.get("text") or "").strip()
                try:
                    box = (int(r["x0"]) - 4, int(r["y0"]) - 2,
                           int(r["x1"]) - int(r["x0"]) + 8,
                           int(r["y1"]) - int(r["y0"]) + 4)
                except (KeyError, TypeError, ValueError):
                    box = None
                crop, pxbox = _crop(frame, box)
                samples.append({"text": txt, "value": _as_int(val), "tier1": None,
                                "cls": _cls_of(_as_int(val), txt), "box": pxbox,
                                "crop": crop, "route": "scanner"})
            analysis["page"] = "list"
            analysis["ocr_text"] = "\n".join(s["text"] for s in samples)[:400]
            if self.page_ocr:
                analysis["page_raw"] = nav.classify(self._full_text(frame))
            return samples, analysis

        for page, rname in (("detail", "bar"), ("settle", "confirm"), ("settle", "buy")):
            box = _box_px(self.cfg.region(rname))
            if not box:
                continue
            crop, pxbox = _crop(frame, box)
            if crop is None:
                continue
            words = ocr.read_words(crop)
            val, tok = ocr_chain.price_from_words(words)
            t1w = [x for x in words if x.get("field") == "price"]
            t1, _ = ocr_chain.price_from_text("\n".join(x["text"] for x in t1w))
            if val is None:
                val, tok = ocr_chain.price_from_text("\n".join(x["text"] for x in words))
            text = tok or "\n".join(x["text"] for x in words)
            samples.append({"text": text.strip(), "value": val, "tier1": t1,
                            "cls": "detail_price" if page == "detail" else "settle_price",
                            "box": pxbox, "crop": crop, "route": "panel"})
            analysis["page"] = page
            analysis["ocr_text"] = text.strip()[:300]
            break
        if self.page_ocr:
            analysis["page_raw"] = nav.classify(self._full_text(frame))
        return samples, analysis

    def _analyze_panel(self, frame):
        """面板尺寸帧（回放已裁好的真实面板）：整幅一次 read_words。"""
        words = ocr.read_words(frame)
        val, tok = ocr_chain.price_from_words(words)
        t1w = [x for x in words if x.get("field") == "price"]
        t1, _ = ocr_chain.price_from_text("\n".join(x["text"] for x in t1w))
        if val is None:                       # 面板拼条缺块时退回整幅文本
            val, tok = ocr_chain.price_from_text("\n".join(x["text"] for x in words))
        text = (tok or "\n".join(x["text"] for x in words)).strip()
        sample = {"text": text, "value": val, "tier1": t1, "cls": "detail_price",
                  "box": [0, 0, frame.shape[1], frame.shape[0]],
                  "crop": frame, "route": "panel"}
        return [sample], {"page": "detail", "ocr_text": text[:300], "page_raw": None}

    def _full_text(self, frame):
        try:
            return ocr.read_text(frame) or ""
        except Exception:                     # noqa: BLE001
            return ""

    # ------------------------------------------------------------ 容量
    def _enforce_cap(self):
        if self.max_bytes <= 0:
            return
        while self.stats.used_bytes > self.max_bytes and self._cache:
            seq, ent = self._cache.popitem(last=False)
            for p in ent["paths"]:
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.stats.used_bytes -= ent["bytes"]
            if self.stats.used_bytes < 0:
                self.stats.used_bytes = 0
            self.stats.pruned += 1
            self._plog.write("%s 超容量删除 seq=%s %d 字节\n"
                             % (_now(), seq, ent["bytes"]))
            self._plog.flush()


def _as_int(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _cls_of(val, text):
    if val is None:
        return "unreadable"
    if val < 1000 and "," not in (text or ""):
        return "low_nosep"
    return "list_thousands"


def _window_meta(sc=None):
    if sc is None:
        return {}
    try:
        w, h = sc.size
    except Exception:                     # noqa: BLE001
        return {}
    return {"base_w": w, "base_h": h, "dpi": getattr(sc, "dpi", None),
            "awareness": getattr(sc, "awareness", None),
            "scale": round(w / 1920.0, 4)}


def run_live(args, col):
    from core.screen import Screen
    sc = Screen(monitor=args.monitor)
    meta0 = _window_meta(sc)
    interval = max(MIN_INTERVAL, float(args.interval))
    t_end = time.time() + args.duration if args.duration else None
    t_next = time.perf_counter()
    sent = 0
    while not args._stop.is_set():
        t_next += interval
        frame = sc.grab()
        sent += 1
        col.submit(frame, dict(meta0, ts=_now(), route="live",
                               interval_s=interval, analysis=None))
        if args.max_frames and sent >= args.max_frames:
            break
        if t_end and time.time() >= t_end:
            break
        sleep = t_next - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)
        else:
            t_next = time.perf_counter()      # 落后不追帧，避免补偿式爆发
    print("采集结束：投递 %d 帧（%s）" % (sent, col.status_line()))


def run_replay(args, col):
    paths = []
    for p in args.replay:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                    paths.append(os.path.join(p, name))
        elif os.path.isfile(p):
            paths.append(p)
        else:
            print("!! 跳过不存在的路径：%s" % p)
    print("回放 %d 个文件" % len(paths))
    sent = 0
    for p in paths:
        if args._stop.is_set():
            break
        img = imread(p)
        if img is None:
            print("!! 读不了：%s" % p)
            continue
        h, w = img.shape[:2]
        col.submit(img, {"ts": _now(), "route": "replay", "src": p,
                         "scale": round(w / 1920.0, 4),
                         "base_w": w, "base_h": h,
                         "interval_s": args.interval, "analysis": None})
        sent += 1
        if args.max_frames and sent >= args.max_frames:
            break
        # 回放要按节奏投递：worker 每帧要做 OCR，喂太快只会全被丢（这正是
        # 有界队列想要的保护，但回放的目的就是让每一帧都被算一遍）
        if args.replay_interval > 0:
            time.sleep(args.replay_interval)
    print("回放结束：投递 %d 帧（%s）" % (sent, col.status_line()))


def main():
    ap = argparse.ArgumentParser(description="价格帧采集器（默认关闭）")
    ap.add_argument("--enable", action="store_true",
                    help="显式开关：不加就什么都不落盘（防日常抢购无声吃磁盘）")
    ap.add_argument("--live", action="store_true", help="真机抓屏采集")
    ap.add_argument("--replay", nargs="+", metavar="PATH",
                    help="回放已有截图/目录（不碰屏幕，用于跑通链路）")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="采集间隔秒；下限 %.1fs（≈2fps 上限）" % MIN_INTERVAL)
    ap.add_argument("--max-frames", type=int, default=0, help="最多投递几帧，0=不限")
    ap.add_argument("--duration", type=float, default=0, help="采集时长秒，0=不限")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                    help="目录总容量上限，超出从最旧删（默认 2GB）")
    ap.add_argument("--queue", type=int, default=DEFAULT_QUEUE, help="有界队列长度")
    ap.add_argument("--monitor", type=int, default=1)
    ap.add_argument("--replay-interval", type=float, default=1.0,
                    help="回放模式下每帧间隔秒（让 worker 有机会算完，0=全速）")
    ap.add_argument("--page-ocr", action="store_true",
                    help="额外做一次整帧 OCR 用 nav.classify 记录页面原文（更慢）")
    args = ap.parse_args()

    if not args.enable:
        print("价格帧采集器默认关闭：加 --enable 才会落帧（--out=%s）"
              % os.path.abspath(args.out))
        return 2
    if args.live and args.replay:
        print("!! --live 与 --replay 只能选一个")
        return 2
    if not args.live and not args.replay:
        print("!! 需要 --live（真机）或 --replay PATH（回放）")
        return 2

    args._stop = threading.Event()

    def _sig(_a, _b):
        args._stop.set()
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except (ValueError, AttributeError):
        pass

    col = Collector(args.out, max_bytes=args.max_bytes, queue_size=args.queue,
                    page_ocr=args.page_ocr)
    print("输出目录：%s" % col.out)
    print("频率上限：%.2f fps（间隔 %.2fs）｜队列 %d｜容量上限 %.0f MB"
          % (1.0 / max(MIN_INTERVAL, args.interval),
             max(MIN_INTERVAL, args.interval), args.queue,
             args.max_bytes / 1048576.0))
    col.start()
    try:
        if args.live:
            run_live(args, col)
        else:
            run_replay(args, col)
    finally:
        col.stop()
        print("汇总：%s" % col.status_line())
        print("frames.jsonl：%s" % os.path.join(col.out, "frames.jsonl"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
