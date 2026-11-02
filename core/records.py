"""成交记录：每抢一次记一行，抢到什么、多少钱、用多久、截图在哪。

为什么要有：`data/shots/` 里那堆截图没有索引，日志滚一屏就没了。抢完一趟
回过头想知道「刚才那件多少钱、几次里成了一次」，只能翻日志找时间戳。

存 JSONL（一行一条）而不是 JSON 数组：抢购是边抢边写，追加不用重写整个文件，
进程被强杀也不会把之前的记录写坏 —— 最后一行可能截断，读的时候跳过即可。
"""
import json
import os
import time

from .config import DATA_DIR

# 数据写在 exe 同级目录（冻结时 __file__ 指向临时解包目录，写那儿退出即丢）。
PATH = os.path.join(DATA_DIR, "records.jsonl")

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
    row = {
        "ts": round(time.time(), 3),
        "t": time.strftime("%m-%d %H:%M:%S"),
        "name": str(name or ""),
        "price": None if price in (None, "", 0) else float(price),
        "result": result if result in RESULTS else "hit",
        "elapsed": None if elapsed is None else round(float(elapsed), 2),
        "shot": shot or "",
        "mode": mode or "",
        "quota": quota,
        # 这次「成交」是校验过结算弹窗的，还是只点了确认就算的。
        # 2026-11-02 之前没有这个字段，78 条「成交」里一条真的都没有。
        "verified": verified,
    }
    if extra:
        row.update(extra)
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return row


def load(limit=200):
    """读记录，新的在前。坏行（写到一半被杀的）直接跳过。"""
    if not os.path.isfile(PATH):
        return []
    rows = []
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    rows.reverse()
    return rows[:limit] if limit else rows


def stats(rows=None):
    """统计：抢到几件、花了多少、试了几次、命中率、中位耗时。

    命中率的分母是「记下来的每一次尝试」，不是成功次数 —— 只报成功次数
    会让人以为每次都成。没有尝试时返回 0，不返回 None，前端不用判空。
    """
    rows = load(limit=0) if rows is None else rows
    hits = [r for r in rows if r.get("result") == "hit"]
    spent = sum(float(r.get("price") or 0) for r in hits)
    elapsed = sorted(float(r["elapsed"]) for r in hits if r.get("elapsed") is not None)

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
    }


def clear():
    """清空记录（截图不删 —— 那是用户的证据，要删自己去 data/shots）。"""
    try:
        open(PATH, "w", encoding="utf-8").close()
    except OSError:
        return False
    return True
