"""成交记录：每抢一次记一行，抢到什么、多少钱、用多久、截图在哪。

为什么要有：`data/shots/` 里那堆截图没有索引，日志滚一屏就没了。抢完一趟
回过头想知道「刚才那件多少钱、几次里成了一次」，只能翻日志找时间戳。

存 JSONL（一行一条）而不是 JSON 数组：抢购是边抢边写，追加不用重写整个文件，
进程被强杀也不会把之前的记录写坏 —— 最后一行可能截断，读的时候跳过即可。
"""
import json
import os
import threading
import time

from .config import DATA_DIR

# 数据写在 exe 同级目录（冻结时 __file__ 指向临时解包目录，写那儿退出即丢）。
PATH = os.path.join(DATA_DIR, "records.jsonl")

# 追加 / 读 / 清空都在同一把锁里。以前三个线程（引擎记成交、web 读列表、
# 用户点清空）各干各的：清空是 `open(PATH, "w")` 先截断，正好撞上 append
# 时那条记录就写进了被截断的洞里，读出来是半行 JSON（审查 S15）。
_lock = threading.RLock()


class RecordReadError(OSError):
    """记录文件读不出来（被占用 / 权限）。

    以前 `except OSError: return []` 把「读失败」伪装成「一条记录都没有」，
    界面上看着一切正常，其实历史全丢了（审查 S22）。现在抛出来，
    接口层报 500，用户至少知道「读不到」而不是「0 条」。
    """

# 结果码 → 中文。只收引擎真会产生的几种，多一个都算没想清楚。
RESULTS = {
    "hit": "抢到",
    "miss": "没等到弹窗",
    "no_money": "余额不足",
    "over_budget": "超预算",
    "aborted": "中止",
}


def add(name="", price=None, result="hit", elapsed=None, shot=None,
        mode="", quota=None, verified=None, extra=None):
    """追加一条记录。写不进去不能影响抢购 —— 记不下就是少一行历史。"""
    # float(price) 以前在 try 外：调用方传进来一个脏值（OCR 抽出 "¥1,230"）
    # 就 ValueError，直接把引擎那条线程打死。脏值当"没记下价格"，别拦成交（审查 S22）。
    try:
        price = None if price in (None, "", 0) else float(price)
    except (TypeError, ValueError):
        price = None
    row = {
        "ts": round(time.time(), 3),
        "t": time.strftime("%m-%d %H:%M:%S"),
        "name": str(name or ""),
        "price": price,
        "result": result if result in RESULTS else "hit",
        "elapsed": None,
        "shot": shot or "",
        "mode": mode or "",
        "quota": quota,
        # 这次「成交」是校验过结算弹窗的，还是只点了确认就算的。
        # 2026-11-02 之前没有这个字段，78 条「成交」里一条真的都没有。
        "verified": verified,
    }
    try:
        if elapsed is not None:
            row["elapsed"] = round(float(elapsed), 2)
    except (TypeError, ValueError):
        row["elapsed"] = None
    if extra:
        row.update(extra)
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with _lock, open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return row


def load(limit=200):
    """读记录，新的在前。坏行（写到一半被杀的）直接跳过。

    读失败抛 RecordReadError：不能假装成「0 条」（审查 S22）。
    """
    if not os.path.isfile(PATH):
        return []
    rows = []
    try:
        with _lock, open(PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                # 坏行可能不是对象（手改过 / 截断成 `123`），stats 里 r.get 会炸
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError as e:
        raise RecordReadError(f"读不到成交记录：{PATH}（{e}）") from e
    rows.reverse()
    return rows[:limit] if limit else rows


def stats(rows=None, verified_only=False):
    """统计：抢到几件、花了多少、试了几次、命中率、中位耗时。

    命中率的分母是「记下来的每一次尝试」，不是成功次数 —— 只报成功次数
    会让人以为每次都成。没有尝试时返回 0，不返回 None，前端不用判空。

    `verified_only=True` 只统计**校验过结算弹窗**的成交：`verified is None`
    是历史记录（2026-11-02 前的字段缺失），`False` 是"只点了确认"，
    都不算真成交（审查 S22）。
    """
    rows = load(limit=0) if rows is None else rows
    rows = [r for r in rows if isinstance(r, dict)]
    if verified_only:
        rows = [r for r in rows if r.get("verified") is True]
    hits = [r for r in rows if r.get("result") == "hit"]
    unverified = sum(1 for r in hits if r.get("verified") is not True)
    spent = 0.0
    for r in hits:
        try:
            spent += float(r.get("price") or 0)
        except (TypeError, ValueError):
            pass
    elapsed = []
    for r in hits:
        try:
            if r.get("elapsed") is not None:
                elapsed.append(float(r["elapsed"]))
        except (TypeError, ValueError):
            pass
    elapsed.sort()

    def median(xs):
        if not xs:
            return None
        mid = len(xs) // 2
        return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2

    return {
        "hits": len(hits),
        "tries": len(rows),
        "spent": round(spent, 2),
        "rate": round(len(hits) / len(rows) * 100, 1) if rows else 0.0,
        "median_s": median(elapsed),
        "miss": sum(1 for r in rows if r.get("result") == "miss"),
        "no_money": sum(1 for r in rows if r.get("result") == "no_money"),
        # 「抢到」里有多少没校验过结算弹窗 —— 以前这两种混在一个 hits 里
        "verified_hits": len(hits) - unverified,
        "unverified_hits": unverified,
    }


def clear():
    """清空记录（截图不删 —— 那是用户的证据，要删自己去 data/shots）。

    先写临时文件再原子替换：`open(PATH, "w")` 是先截断再写的，撞上
    引擎 append 就会两边互相踩（审查 S15）。
    """
    tmp = PATH + ".tmp"
    try:
        with _lock:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("")
            os.replace(tmp, PATH)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
    return True
