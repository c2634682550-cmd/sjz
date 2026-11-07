"""成交记录：每抢一次记一行，抢到什么、多少钱、用多久、截图在哪。

为什么要有：`data/shots/` 里那堆截图没有索引，日志滚一屏就没了。抢完一趟
回过头想知道「刚才那件多少钱、几次里成了一次」，只能翻日志找时间戳。

存 JSONL（一行一条）而不是 JSON 数组：抢购是边抢边写，追加不用重写整个文件，
进程被强杀也不会把之前的记录写坏 —— 最后一行可能截断，读的时候跳过即可。

**长时间挂机**：文件会一直长（每抢一次一行、连抢好几天），而 `stats()` 要
每次都读全文 —— 越大越慢。所以两件事：
  · 写完顺手轮转（超过 `retention.RECORDS_MAX_BYTES` 存成 .1/.2）；
  · 读的时候流式累加，不再把整份文件物化成 list（原来 `load(limit=0)`
    会把几天的记录一次性读进内存）。
"""
import bisect
import contextlib
import hashlib
import json
import math
import os
import tempfile
import threading
import time

from . import retention, settle_verify
from .config import DATA_DIR, FileLockTimeout, file_lock

# 数据写在 exe 同级目录（冻结时 __file__ 指向临时解包目录，写那儿退出即丢）。
PATH = os.path.join(DATA_DIR, "records.jsonl")

# 跨进程等锁超时（审查 R13-03）：等不到就显式失败，绝不把「没写进去」
# 当成「写成功」。同机正常情况下一次追加只占锁几毫秒。
RECORDS_LOCK_TIMEOUT = 10.0

# 追加 / 读 / 清空都在同一把锁里。以前三个线程（引擎记成交、web 读列表、
# 用户点清空）各干各的：清空是 `open(PATH, "w")` 先截断，正好撞上 append
# 时那条记录就写进了被截断的洞里，读出来是半行 JSON（审查 S15）。
_lock = threading.RLock()

# 写失败的显式状态（审查 R13-07：以前只 read-only/满盘都静默返回 None，
# 统计照常显示旧数据，上层完全不知道有记录没落盘）。
_error_state = {"error": None, "at": None}

# 崩溃残行的修复记录（审查 R13-02：恢复过程必须有明确记录，不能悄悄做）。
_recovery_state = {"count": 0, "last": None}


class RecordReadError(OSError):
    """记录文件读不出来（被占用 / 权限）。

    以前 `except OSError: return []` 把「读失败」伪装成「一条记录都没有」，
    界面上看着一切正常，其实历史全丢了（审查 S22）。现在抛出来，
    接口层报 500，用户至少知道「读不到」而不是「0 条」。
    """


class RecordWriteError(OSError):
    """记录写不进去（满盘 / 只读 / 等锁超时）。

    `add(strict=True)` 抛它；默认的 `add()` 为了不打断抢购仍返回 None，
    但会把原因写进 `last_error()` 并体现在 `stats()["write_ok"]` 上 ——
    上层至少有办法知道「这一行没落盘」。
    """


def last_error():
    """最近一次写失败的原因（没失败时是 None）。"""
    return _error_state["error"]


def recovery_log():
    """崩溃残行修复记录：次数 + 最近一次的详情。"""
    return {"count": _recovery_state["count"],
            "last": dict(_recovery_state["last"] or {})}


def _note_ok():
    if _error_state["error"] is not None:
        _error_state["error"] = None
        _error_state["at"] = None


def _note_error(err):
    msg = f"{type(err).__name__}: {err}"
    if _error_state["error"] != msg:
        print(f"[records] 写台账失败（{msg}）—— 这一行没有落盘")
    _error_state["error"] = msg
    _error_state["at"] = time.time()


def _lock_path():
    """锁文件放在系统临时目录，按台账绝对路径散列命名。

    为什么不放在台账旁边：PATH 在测试里会被指到任意位置（甚至指向源码
    文件），旁边写 `.lock` 会在程序/仓库目录里留垃圾；放临时目录里
    同机同用户的所有进程仍然指向同一把锁，跨进程互斥照样成立。
    """
    key = hashlib.sha1(os.path.abspath(PATH).encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"dafors-records-{key}.lock")


@contextlib.contextmanager
def _file_guard(timeout=None):
    """台账文件的跨进程互斥。"""
    cm = file_lock(_lock_path(),
                   timeout=RECORDS_LOCK_TIMEOUT if timeout is None else timeout)
    cm.__enter__()
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def _repair_tail(path):
    """截掉崩溃留下的半行，保证下一次追加从干净的行边界开始。

    以前直接用 `open(path, "a")`：崩溃后文件尾没换行，新记录被拼在残行
    后面，**新记录自己也成了非法 JSON**，重启后连丢两条且全程静默
    （审查 R13-02）。返回截掉的字节数；不需要截返回 0；修不动（只读）
    返回 -1，调用方退化成「先补一个换行再写」。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    if size <= 0:
        return 0
    try:
        with open(path, "rb") as f:
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return 0
            cut = 0
            pos = size
            step = 8192
            while pos > 0:
                n = min(step, pos)
                pos -= n
                f.seek(pos)
                idx = f.read(n).rfind(b"\n")
                if idx >= 0:
                    cut = pos + idx + 1
                    break
        removed = size - cut
        with open(path, "r+b") as f:
            f.truncate(cut)
            f.flush()
            os.fsync(f.fileno())
        _recovery_state["count"] += 1
        _recovery_state["last"] = {"ts": round(time.time(), 3),
                                   "path": path, "removed": removed}
        print(f"[records] 上次崩溃留下 {removed} 字节残行，已截断：{path}")
        return removed
    except OSError as e:
        print(f"[records] 残行截断失败（{e}），改为先补换行再追加")
        return -1


def _needs_newline(path):
    """文件尾是不是缺一个换行（缺的话追加前要补）。"""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return False
        with open(path, "rb") as f:
            f.seek(size - 1)
            return f.read(1) != b"\n"
    except OSError:
        return False

# 结果码 → 中文。只收引擎真会产生的几种，多一个都算没想清楚。
RESULTS = {
    "hit": "抢到",
    "miss": "没等到弹窗",
    "no_money": "余额不足",
    "over_budget": "超预算",
    "aborted": "中止",
}


def _invalid_value(value, path="row"):
    """返回第一个非法值的可读原因；全部合法返回 None。

    非法 = 写不进 JSONL 的东西：
      · 孤代理字符（`\\ud800`）：json 原样输出后 utf-8 编码炸（审查 E7）；
      · NaN / Infinity：不是合法 JSON 字面量，前端 `JSON.parse` 直接失败，
        统计里的 `spent` 变成 nan、预算比较恒 False（审查 E7）；
      · 其它 json 序列化不了的类型（set、bytes…）。
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as e:
            return f"{path} 含非法 Unicode 字符（{e.reason}）"
        return None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return f"{path} 不是有限数值（{value}）"
        return None
    if isinstance(value, int):
        return None                      # json 能写任意大整数
    if isinstance(value, dict):
        for k, v in value.items():
            bad = _invalid_value(k, path)
            if bad:
                return bad
            bad = _invalid_value(v, f"{path}.{k}")
            if bad:
                return bad
        return None
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            bad = _invalid_value(v, f"{path}[{i}]")
            if bad:
                return bad
        return None
    return f"{path} 类型 {type(value).__name__} 不能写入 JSON"


def add(name="", price=None, result="hit", elapsed=None, shot=None,
        mode="", quota=None, verified=None, extra=None, strict=False,
        first_seen_ms=None, listed_at_ms=None):
    """追加一条记录。写不进去不能影响抢购 —— 记不下就是少一行历史。

    `strict=True` 时写失败抛 `RecordWriteError`；默认返回 None，但失败原因
    记在 `last_error()` 里、并出现在 `stats()["write_ok"]`，不再静默
    （审查 R13-07）。

    **结算三态（2026-11-07）**：`verified` 落盘为 `won` / `lost` / `unknown`
    三个字符串之一。不传（None）就是 `unknown` —— 没有证据绝不允许写成
    `lost`/`False`（见 `core/settle_verify.py` 的硬约束）。历史调用方仍按
    旧口径传 bool：True（校验过结算弹窗）保留 True，False（只点了确认）
    保留 False，读侧统一折算成三态，旧台账照读不报错。

    `first_seen_ms` / `listed_at_ms` 是单调时钟毫秒：候选首次出现在列表扫描
    结果里、以及该单挂单完成进入公示的时刻。拿不到就留 None，并在
    `no_evidence` 里列字段名，不用 0 / -1 冒充。

    **写入前净化**（审查 E7）：孤代理字符、NaN/Infinity、10**400 这类
    写不进/读不回来的值一律拒收 —— 台账里出现 `NaN` 会让前端
    `JSON.parse` 失败、`spent=nan` 让预算比较恒 False。拒收不影响抢购，
    只是这一行不记。
    """
    # float(price) 以前在 try 外：调用方传进来一个脏值（OCR 抽出 "¥1,230"）
    # 就 ValueError，直接把引擎那条线程打死。脏值当"没记下价格"，别拦成交（审查 S22）。
    bad_reason = None
    try:
        price = None if price in (None, "", 0) else float(price)
    except (TypeError, ValueError):
        price = None
    except OverflowError:
        price = None
        bad_reason = "price 超出可表示范围"
    if isinstance(price, float) and not math.isfinite(price):
        bad_reason = f"price 不是有限数值（{price}）"
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
    except OverflowError:
        row["elapsed"] = None
        bad_reason = bad_reason or "elapsed 超出可表示范围"
    if isinstance(row["elapsed"], float) and not math.isfinite(row["elapsed"]):
        bad_reason = bad_reason or f"elapsed 不是有限数值（{row['elapsed']}）"
        row["elapsed"] = None
    if extra:
        row.update(extra)

    # 三态落盘：字符串三态原样保留；历史 bool 保留 bool（引擎还在用
    # True=校验开关 / False=只点了确认），None/脏值一律记成 unknown。
    if not isinstance(row.get("verified"), bool):
        row["verified"] = settle_verify.normalize(row.get("verified"))
    # 两个观测时间戳放在 extra 之后写，既有 22 列的键序一个字都不动。
    if first_seen_ms is not None:
        row["first_seen_ms"] = first_seen_ms
    if listed_at_ms is not None:
        row["listed_at_ms"] = listed_at_ms
    for k in settle_verify.TIME_FIELDS:
        row[k] = settle_verify.norm_ms(row.get(k))
    row["no_evidence"] = settle_verify.missing_marks(row)

    if bad_reason is None:
        bad_reason = _invalid_value(row)
    if bad_reason is not None:
        msg = f"这一行有非法值，没有写入：{bad_reason}"
        _note_error(RecordWriteError(msg))
        if strict:
            raise RecordWriteError(msg)
        return None

    try:
        line = json.dumps(row, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        msg = f"这一行写不成 JSON，没有写入：{e}"
        _note_error(RecordWriteError(msg))
        if strict:
            raise RecordWriteError(msg)
        return None
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with _lock:
            with _file_guard():
                # 1) 先修上次崩溃留下的残行（修不动就补换行，保证行边界）
                _repair_tail(PATH)
                prefix = "\n" if _needs_newline(PATH) else ""
                sig_before = _signature()
                # 2) 追加。整段在跨进程锁里：两个进程同时写也不会丢记录。
                # newline="\n"：Windows 默认会把 \n 翻成 \r\n，而增量游标按
                # 行内容做哈希；写入字节必须与读取/哈希口径一致，否则每次
                # 追加后都会被判成「台账被替换」而从 0 重读。
                with open(PATH, "a", encoding="utf-8", newline="\n") as f:
                    f.write(prefix + json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                # 轮转必须和 append 在同一把锁里：rename 撞上写就是半行丢进新文件。
                # 失败（被占用/权限）只意味着文件继续长，不影响记录本身。
                rotated = retention.rotate(PATH, retention.RECORDS_MAX_BYTES,
                                           retention.RECORDS_KEEP)
                if rotated:
                    # 轮转丢掉了最老的一份，缓存里的累计值已经不完整 —— 作废，
                    # 下次统计重扫。否则「抢到件数」会凭空多出旧账。
                    _cache["acc"] = None
                    _cache["sig"] = None
                    _rows_snap.update(sig=None, rows=None, limit=0)
                else:
                    _cache_add(row, sig_before)
                    _rows_snap_add(row)
        _note_ok()
    except (OSError, FileLockTimeout) as e:
        _note_error(e)
        if strict:
            raise RecordWriteError(f"成交记录写不进去：{e}") from e
        return None
    return row


def _iter_rows(path):
    """逐行产出 dict；坏行（写到一半被杀的）直接跳过。

    以前用文本模式 `open(path, encoding="utf-8")`：文件尾停在半个 UTF-8
    字符上时（强杀/断电留下的残行），解码错误是在**迭代过程**里抛的 ——
    已经读出来的有效行跟着一起炸，`stats()` / `load(0)` 变成 500，而且
    再读一次还是炸（读路径不自愈，审查 P5）。

    现在按字节逐行读：坏编码只会影响那一行（`UnicodeDecodeError` 是
    `ValueError` 子类，和坏 JSON 同一条路）；最后一行如果确实不完整，
    顺手截掉，下一次读就干净了。前面的有效数据一条都不少。
    """
    tail_bad = False
    try:
        with open(path, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    # 坏 JSON 或尾部半个 UTF-8 字符：跳过这一行，继续读别的
                    tail_bad = True
                    continue
                # 坏行可能不是对象（手改过 / 截断成 `123`），stats 里 r.get 会炸
                if isinstance(obj, dict):
                    tail_bad = False
                    yield obj
    except OSError as e:
        # 读不了 ≠ 没有记录。抛出去让界面报「读不到」，别伪装成 0 条。
        raise RecordReadError(f"读不到成交记录：{path}（{e}）") from e
    finally:
        # 最后一行解码/解析失败 = 尾部不完整：截到最后一个完整行边界。
        # 如果文件本来就以换行结尾，_repair_tail 不会动它（完整坏行只是跳过）。
        if tail_bad:
            _repair_tail(path)


def _paths():
    """当前文件 + 轮转出来的历史（新的在前）。"""
    out = [PATH] if os.path.isfile(PATH) else []
    out.extend(retention.archives(PATH, retention.RECORDS_KEEP))
    return out


def _signature():
    """「盘上台账现在是哪一份」的指纹：路径 + 字节数。

    只用来判断缓存能不能复用。改名轮转、清空、外部追加都会改变它。
    """
    sig = []
    for p in _paths():
        try:
            sig.append((p, os.path.getsize(p)))
        except OSError:
            sig.append((p, -1))
    return tuple(sig)


def _tail_rows(path, limit):
    """从文件尾往前读，最多 limit 条。返回**新的在前**。

    原来 `load(limit)` 是把整个文件读完再取最后 N 条 —— 挂机几天后
    每次界面刷新都要解析几万行，越刷新越慢。倒着读只需要碰最后几 KB。
    """
    limit = int(limit)
    if limit <= 0:
        return []
    rows = []
    chunk = 8192
    try:
        with open(path, "rb") as f:
            size = os.path.getsize(path)
            pos = size
            tail = b""
            while pos > 0 and len(rows) < limit:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + tail
                lines = data.split(b"\n")
                tail = lines[0]          # 可能被截断的半行，留给下一轮
                for line in reversed(lines[1:]):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
                    if len(rows) >= limit:
                        return rows
            if tail.strip() and len(rows) < limit:
                try:
                    obj = json.loads(tail)
                except ValueError:
                    return rows
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError as e:
        raise RecordReadError(f"读不到成交记录：{path}（{e}）") from e
    return rows


# 默认统计的增量缓存。台账只由本模块写，所以 add() 能安全地往上加；
# 轮转/清空/外部改动靠签名失配自动回退到全量扫描。
_cache = {"sig": None, "acc": None}

# 界面轮询快照（2026-11-07，185ms 尖刺）。
#   · `_rows_snap`：最近一次倒读出来的 rows，按文件签名命中；add() 成功后
#     直接把新行插到头部，所以轮询在两次写入之间完全不碰盘。
#   · `_stats_snap`：最近一次可用的统计值。缓存签名失配时先返回它，
#     全量重算交给后台线程 —— 请求线程上不再做 O(台账行数) 的活。
_rows_snap = {"sig": None, "limit": 0, "rows": None}
_stats_snap = {"value": None}
_stats_rebuild = {"running": False}
_rebuild_lock = threading.Lock()
# 「统计已经有真值了」的信号。预热（`warm()`）或后台重算第一次成功时置位。
# 界面轮询靠它决定是给占位还是读盘，测试靠它等预热，不靠 sleep 猜时间。
_ready = threading.Event()
# 后台重算的完成信号（开始时 clear、跑完 finally 里 set）。`wait_ready()`
# 除了等「有真值」，还要等正在跑的那一轮重算结束，时序才是确定的。
_rebuild_done = threading.Event()
_rebuild_done.set()


def reset_state():
    """清空模块级诊断/增量缓存（测试隔离用，生产不调用）。

    三块都是模块级可变状态，跨用例存活：
      * `_error_state` —— 上一次写失败的原因（`last_error()`/`stats()` 读）
      * `_recovery_state` —— 崩溃残行修复计数
      * `_cache` —— 按文件签名命中的统计增量缓存
    测试里前一个用例把 `records.PATH` 指向临时文件并写失败/触发修复，
    后一个用例仍会读到上一轮的 error 与缓存签名；随机顺序下表现成
    「按品阶停止」计数与台账断言偶发对不上。测试侧由
    `tests/order_isolation.py` 的 autouse 夹具在每个用例前调用。
    """
    with _lock:
        _error_state.update(error=None, at=None)
        _recovery_state.update(count=0, last=None)
        _cache.update(sig=None, acc=None)
        _rows_snap.update(sig=None, rows=None, limit=0)
        _stats_snap["value"] = None
        _stats_rebuild["running"] = False
        _ready.clear()
        _rebuild_done.set()


# 中位耗时只参考最近这么多次成交。72h 连续挂机约 4200 次成交，30 天约 43 万次：
# 不设上限时 `elapsed` 列表就是唯一会无限长的内部状态（每个 float ~32B，
# 30 天 ~14MB），`_finish` 还要每次排序（43 万条 ~20ms/次，1 秒刷一次界面
# 就是 2% 的常驻 CPU）。中位数本来就该看「最近的手感」，截断不损失信息。
ELAPSED_SAMPLE_MAX = 5000
_ELAPSED_TRIM_AT = ELAPSED_SAMPLE_MAX + 1000   # 攒够这么多再裁一次，摊平开销


def _empty_acc():
    return {"hits": 0, "tries": 0, "spent": 0.0, "miss": 0, "no_money": 0,
            "unverified": 0, "elapsed": []}    # elapsed 始终保持升序


def _cache_add(row, sig_before):
    """写成功一条后顺手累加，保证下一次 stats() 不用重扫全文。

    `sig_before` 是写盘**之前**的签名：只有它和缓存签名一致，说明中间
    没有别人动过台账，这一条才可以安全地加到缓存上。
    """
    if _cache["acc"] is None:
        return
    if _cache["sig"] != sig_before:
        _cache["acc"] = None
        _cache["sig"] = None
        return
    _accumulate(_cache["acc"], row)
    _cache["sig"] = _signature()


def _rows_snap_add(row):
    """写成功一条后把轮询行快照也往前推一格（调用方已持有 `_lock`）。

    不这么做的话，压缩模式下每 0.4s 写一行、界面每 1s 轮询一次，每次
    都得重新倒读台账 —— 宿主磁盘一忙，单次就是几十毫秒的 I/O 尖刺。
    """
    if _rows_snap["rows"] is None:
        return
    _rows_snap["rows"].insert(0, dict(row))
    del _rows_snap["rows"][max(1, int(_rows_snap["limit"])):]
    # `_cache_add` 刚算过新签名；它作废缓存时这里是 None，下次轮询重读。
    _rows_snap["sig"] = _cache["sig"]


def _set_stats(out):
    """记住一份可用统计，并置位「已就绪」。"""
    _stats_snap["value"] = dict(out)
    _ready.set()


def _stats_for_poll(sig, cold=False):
    """轮询用统计：缓存命中就 O(1)；失配先返回上一份，后台重算。

    调用方已持有 `_lock` + 跨进程锁。

    `cold=True` 只由 `warm()` 传 —— 那是进程启动路径，不是请求线程，
    允许在这里做一次 O(台账行数) 的全扫。
    """
    acc = _cache["acc"]
    if acc is not None and _cache["sig"] == sig:
        out = _with_health(_finish(acc))
        _set_stats(out)
        return out
    if cold:
        # 预热：这是唯一允许同步全扫的地方（启动时一次）。
        out = _with_health(_stats_impl(False))
        _set_stats(out)
        return out
    _schedule_stats_rebuild()
    last = _stats_snap["value"]
    if last is None:
        # 还没预热就先来了请求：给占位（全 0 + warming），绝不在这里全扫。
        # 正常启动顺序下走不到这条 —— `serve()` 在开始接请求之前已同步预热。
        out = _with_health(_finish(_empty_acc()))
        out["warming"] = True
        return out
    return dict(last)


def warm(limit=200):
    """同步预热界面轮询的两个快照（启动路径调一次，测试也用它）。

    这一次的倒读 + 全量统计是冷启动必须付的成本，放到**启动线程**上，
    请求线程就永远看不到 `warming` 占位、也不会替谁等这次扫描。
    返回拿到的快照；台账读不出来照旧抛 `RecordReadError`。
    """
    limit = int(limit)
    try:
        with _lock:
            with _file_guard():
                sig = _signature()
                rows = _load_impl(limit)
                _rows_snap.update(sig=_signature(),
                                  rows=[dict(r) for r in rows], limit=limit)
                return {"rows": rows, "stats": _stats_for_poll(sig, cold=True),
                        "warming": False}
    except FileLockTimeout as e:
        raise RecordReadError(f"台账被别的进程占用太久：{e}") from e


def wait_ready(timeout=None):
    """等「统计有真值 + 正在跑的后台重算结束」。返回是否在超时前就绪。

    测试用它替代 sleep 猜时间；进程里没有别的用途（请求路径永远不阻塞等待）。
    """
    end = None if timeout is None else time.monotonic() + timeout
    if not _ready.wait(timeout):
        return False
    if not _stats_rebuild["running"]:
        return True
    left = None if end is None else max(0.0, end - time.monotonic())
    return _rebuild_done.wait(left)


def _schedule_stats_rebuild():
    if _stats_rebuild["running"]:
        return
    with _rebuild_lock:
        if _stats_rebuild["running"]:
            return
        _stats_rebuild["running"] = True
        _rebuild_done.clear()
    threading.Thread(target=_rebuild_stats, name="records-stats-rebuild",
                     daemon=True).start()


def _rebuild_stats():
    """后台全量重算（轮转/清空/外部追加后才会走到这里）。"""
    try:
        with _lock:
            with _file_guard():
                out = _stats_impl(False)
        _set_stats(_with_health(dict(out)))
    except Exception as e:                   # noqa: BLE001
        # 后台线程不能把异常抛到无人接管的地方；下一次失配会再触发。
        print(f"[records] 统计后台重算失败：{type(e).__name__}: {e}")
    finally:
        _stats_rebuild["running"] = False
        _rebuild_done.set()


def snapshot(limit=200):
    """界面轮询一次拿 `rows` + `stats`（同一次加锁，避免两次锁往返）。

    和 `load()`/`stats()` 的区别：**请求线程上不做重活**。
      · rows：签名没变就用 `_rows_snap`；变了才倒读一次（几 KB）。
      · stats：缓存命中就用增量值；失配先给上一份可用值，后台重算；
        预热还没完成（`_ready` 未置位）就给 `warming` 占位 —— O(台账行数)
        的全扫只在 `warm()` / 后台线程上，绝不在请求线程上。
    """
    limit = int(limit)
    try:
        with _lock:
            with _file_guard():
                sig = _signature()
                if (_rows_snap["rows"] is not None
                        and _rows_snap["sig"] == sig
                        and _rows_snap["limit"] >= limit):
                    rows = [dict(r) for r in _rows_snap["rows"][:limit]]
                else:
                    rows = _load_impl(limit)
                    _rows_snap.update(sig=_signature(),
                                      rows=[dict(r) for r in rows], limit=limit)
                stats = _stats_for_poll(sig)
                return {"rows": rows, "stats": stats,
                        "warming": not _ready.is_set()}
    except FileLockTimeout as e:
        raise RecordReadError(f"台账被别的进程占用太久：{e}") from e


def load(limit=200):
    """读记录，新的在前。坏行直接跳过。读失败抛 RecordReadError。

    `limit>0` 只从文件尾倒着读（碰几 KB，不随台账变长变慢）；`limit=0`
    是全量，仍然逐行流式产出，不一次性塞进内存。

    整段在跨进程锁里：别人正在追加/轮转时，读到一半再被 rename 会把
    同一份文件读两遍或读丢一段。
    """
    try:
        with _lock:
            with _file_guard():
                return _load_impl(limit)
    except FileLockTimeout as e:
        raise RecordReadError(f"台账被别的进程占用太久：{e}") from e


def _load_impl(limit):
    paths = _paths()
    if not paths:
        return []
    if limit:
        rows = []
        left = int(limit)
        for p in paths:                 # 先当前文件（最新），不够再补历史
            got = _tail_rows(p, left)
            rows.extend(got)
            left -= len(got)
            if left <= 0:
                break
        return rows
    rows = []
    # `paths` 已经是「新的在前」。每个文件内部按行是旧→新，要单独反转再拼，
    # 不能全部读完再 reverse —— 那样最旧的文件会跑到最前面（审查 S7）。
    for p in paths:
        file_rows = list(_iter_rows(p))
        rows.extend(reversed(file_rows))
    return rows


def stats(rows=None, verified_only=False):
    """统计：抢到几件、花了多少、试了几次、命中率、中位耗时。

    命中率的分母是「记下来的每一次尝试」，不是成功次数 —— 只报成功次数
    会让人以为每次都成。没有尝试时返回 0，不返回 None，前端不用判空。

    `verified_only=True` 只统计**校验过结算弹窗**的成交：`verified is None`
    是历史记录（2026-11-02 前的字段缺失），`False` 是"只点了确认"，
    都不算真成交（审查 S22）。

    默认路径走增量缓存：`add()` 写成功后直接把这一条累加进缓存，
    查统计不再重扫全文。只有签名失配（轮转/清空/外部改动）才回退全量。
    这是长跑「越跑越慢」的主要来源之一 —— 原来每查一次都 O(N)。

    返回值额外带 `write_ok` / `write_error` / `recovered_lines`：台账写失败
    不再是静默的（审查 R13-07），界面/上层能从统计里看到。
    """
    if rows is not None:
        return _with_health(_stats_from(
            [r for r in rows if isinstance(r, dict)], verified_only))
    try:
        with _lock:
            with _file_guard():
                return _with_health(_stats_impl(verified_only))
    except FileLockTimeout as e:
        raise RecordReadError(f"台账被别的进程占用太久：{e}") from e


def _with_health(out):
    out["write_ok"] = _error_state["error"] is None
    out["write_error"] = _error_state["error"]
    out["recovered_lines"] = _recovery_state["count"]
    return out


def _stats_impl(verified_only):
    # 过滤口径要逐行判断，走全量（这个开关只有排查时用，不热）；全量结果
    # 不进增量缓存，否则下一次默认统计会拿到过滤后的数。
    if not verified_only and _cache["acc"] is not None \
            and _cache["sig"] == _signature():
        return _finish(_cache["acc"])
    acc = _empty_acc()
    for p in _paths():
        for r in _iter_rows(p):
            _accumulate(acc, r, verified_only)
    if not verified_only:
        _cache["acc"] = acc
        _cache["sig"] = _signature()
    return _finish(acc)


def _accumulate(acc, r, verified_only=False):
    # 三态口径：won 才算已结算成交；lost / unknown 都不算（旧 bool True -> won，
    # 旧 bool False / None -> unknown，行为与之前 `is True` 完全一致）。
    won = settle_verify.normalize(r.get("verified")) == settle_verify.WON
    if verified_only and not won:
        return
    acc["tries"] += 1
    if r.get("result") == "miss":
        acc["miss"] += 1
    if r.get("result") == "no_money":
        acc["no_money"] += 1
    if r.get("result") != "hit":
        return
    acc["hits"] += 1
    if not won:
        acc["unverified"] += 1
    try:
        acc["spent"] += float(r.get("price") or 0)
    except (TypeError, ValueError):
        pass
    try:
        if r.get("elapsed") is not None:
            v = float(r["elapsed"])
            if math.isfinite(v):
                # 插入即保持有序：`_finish` 取中位数变成 O(1)，不用每次
                # stats() 重排一遍（长跑时 stats() 每轮/每秒都会被调）。
                bisect.insort(acc["elapsed"], v)
                if len(acc["elapsed"]) > _ELAPSED_TRIM_AT:
                    del acc["elapsed"][:len(acc["elapsed"]) - ELAPSED_SAMPLE_MAX]
    except (TypeError, ValueError):
        pass


def _finish(acc):
    # `_accumulate` 用 bisect 插入，elapsed 天然有序；这里不再 sorted()。
    elapsed = acc["elapsed"]

    def median(xs):
        if not xs:
            return None
        mid = len(xs) // 2
        return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2

    hits = acc["hits"]
    tries = acc["tries"]
    return {
        "hits": hits,
        "tries": tries,
        "spent": round(acc["spent"], 2),
        "rate": round(hits / tries * 100, 1) if tries else 0.0,
        "median_s": median(elapsed),
        "miss": acc["miss"],
        "no_money": acc["no_money"],
        # 「抢到」里有多少没校验过结算弹窗 —— 以前这两种混在一个 hits 里
        "verified_hits": hits - acc["unverified"],
        "unverified_hits": acc["unverified"],
    }


def _stats_from(rows, verified_only):
    acc = _empty_acc()
    for r in rows:
        _accumulate(acc, r, verified_only)
    return _finish(acc)


def cache_stats():
    """统计缓存的当前体量（长跑用来判「缓存是不是无上限增长」）。

    `elapsed_samples` 是缓存里为了算中位耗时保留的每次成交耗时；
    这个列表只增不减，是 72h 之后仍在增长的唯一一处内部状态。
    """
    acc = _cache["acc"]
    if acc is None:
        return {"cached": False, "elapsed_samples": 0, "tries": 0, "hits": 0,
                "bytes_est": 0}
    n = len(acc["elapsed"])
    return {"cached": True, "elapsed_samples": n, "tries": acc["tries"],
            "hits": acc["hits"],
            # list 8B/指针 + float 24B/个（CPython 64 位），粗估用
            "bytes_est": n * 32 + 200}


def clear():
    """清空记录（截图不删 —— 那是用户的证据，要删自己去 data/shots）。

    先写临时文件再原子替换：`open(PATH, "w")` 是先截断再写的，撞上
    引擎 append 就会两边互相踩（审查 S15）。轮转出来的历史也一并清掉，
    否则「清空」之后统计里还留着旧账。

    整段在跨进程锁里（别的进程正追加时清空同样会踩）；任一步失败都返回
    False，不再「文件清空了、历史没删掉却报成功」。
    """
    tmp = f"{PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with _lock:
            with _file_guard():
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write("")
                    f.flush()
                    os.fsync(f.fileno())
                _replace_with_retry_soft(tmp, PATH)
                ok = True
                for p in retention.archives(PATH, retention.RECORDS_KEEP):
                    try:
                        os.remove(p)
                    except OSError:
                        ok = False
                _cache["acc"] = None
                _cache["sig"] = None
                _rows_snap.update(sig=None, rows=None, limit=0)
                _set_stats(_with_health(_finish(_empty_acc())))
                if ok:
                    _note_ok()
                return ok
    except (OSError, FileLockTimeout) as e:
        _remove_temp(tmp)
        _note_error(e)
        return False


def _replace_with_retry_soft(src, dst, attempts=7):
    """清空时的 replace：目标被读着（Windows 共享冲突）就重试几次。"""
    delay = 0.02
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i >= attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def _remove_temp(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
