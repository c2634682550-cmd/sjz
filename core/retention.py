"""磁盘资源上限：台账轮转、截图缓存修剪、日志轮转。

为什么要有：原来三处都只写不清 ——
  · `data/records.jsonl` 每抢一次追加一行，`records.stats()` 每次都全文读；
  · `data/shots/` 每成交一张 JPEG，永不删除；
  · `ocr_service_<port>.log` 以 append 打开，服务切换一次就长一点。
连挂几天之后：台账越读越慢、磁盘被截图吃满、日志把盘写爆（写日志失败
本身又是另一条故障源）。这里给每个可增长的资产一个**上限 + 轮转/修剪**，
调用方在写完之后顺手调一次即可。

全部是「尽力而为」：清不动（被占用/权限）就放弃并返回 0，绝不因为
清理失败影响抢购本身。
"""
import os
import stat
import time

# 台账：超过这个大小就轮转，只留最近 KEEP 份（含当前）。
RECORDS_MAX_BYTES = 8 * 1024 * 1024
RECORDS_KEEP = 3

# 截图：最多留这么多张 / 这么多字节，按修改时间从旧到新删。
SHOTS_MAX_FILES = 400
SHOTS_MAX_BYTES = 256 * 1024 * 1024

# 截图修剪的扫描间隔：每存 `every` 张才扫一次目录。
# 为什么：引擎每成交一张就调一次 prune_dir，而每次都要把整目录 stat 一遍。
# 目录攒到 400 张时实测 21~54ms/轮（还在波动），占单轮 CPU 的 15%~45%，
# 比存截图本身（0.7ms）贵两个数量级；长跑曲线也因此被抬出一个「假台阶」。
# 按 40 张扫一次可以平摊到 ~0.5~1.5ms/轮，代价是目录最多超上限 40 张。
SHOTS_PRUNE_EVERY = 40
_prune_tick = {}

# 服务日志：单文件上限，超了就轮转一份 .1（再超就覆盖 .1）。
LOG_MAX_BYTES = 8 * 1024 * 1024

# OCR 服务日志的文件名前缀（`ocr_service_<port>.log`）。
LOG_PREFIX = "ocr_service_"
# 兜底扫描的最小间隔：`Config.load()` 每次都会顺带问一次，10 分钟内只扫一次。
LOG_SWEEP_INTERVAL = 10 * 60

# 轮转失败不能静默：记下来，界面上/日志里能看见（审查 R5）。
_rotate_state = {"error": None, "path": None, "ts": 0.0}
_sweep_at = {}


def last_rotate_error():
    """最近一次日志轮转失败的原因；没失败过是 None。"""
    return _rotate_state["error"]


def _note_rotate_error(path, exc):
    """记录轮转失败。同一个原因只打印一次，避免每秒刷屏。"""
    text = f"{type(exc).__name__}: {exc}"
    first = _rotate_state["error"] != text or _rotate_state["path"] != path
    _rotate_state.update(error=text, path=path, ts=time.time())
    if first:
        print(f"[retention] 日志轮转失败，下次再试：{os.path.basename(path)}（{text}）")


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def rotate(path, max_bytes=RECORDS_MAX_BYTES, keep=RECORDS_KEEP):
    """把超限的 path 轮转成 path.1 / path.2 …

    返回 True 表示发生了轮转。**调用方要持有该文件的写锁**（records 是
    `_lock`），否则会在 rename 的瞬间和 append 撞上。

    以前是「先把最老那份 `.2` 删掉，再把 `.1` 往 `.2` 挪」：只要 `.1` 被
    别的进程打开着（杀毒/索引器/另一个进程在读），`os.replace` 就失败，
    结果是**轮转没发生、最老归档却已经被删了**（审查 S1）；`.2` 自己被
    占用时又永远转不动、上限彻底失效且没有任何日志（审查 S2）。

    现在改成「先改名到临时名 → 全部到位后才删退役份」：
      · 任何一步改名失败都原样放回，归档一份不少（S1）；
      · 腾位置不依赖删除最老份，`.2` 只是被原子覆盖/改名，失败就告警（S2）；
      · 退役那份留到最后才删，删不掉只是多留一份，不覆盖任何还在用的归档。
    """
    if max_bytes <= 0 or _size(path) < max_bytes:
        return False
    keep = max(1, keep)

    # 1) 现有归档先改到临时名。纯改名不动内容，任何一步失败都能原样放回。
    token = f"{os.getpid()}-{time.time_ns()}"
    moved = []
    for i in range(1, keep):
        src = f"{path}.{i}"
        if not os.path.isfile(src):
            continue
        tmp = f"{path}.rot-{token}-{i}"
        try:
            os.replace(src, tmp)
        except OSError as e:
            _rollback_archives(path, moved, keep)
            _note_rotate_error(src, e)
            return False
        moved.append((i, tmp))

    # 2) 归档整体后移一位；最老的一份（下标 keep-1）退役，先留在临时名里。
    retired = None
    for i, tmp in reversed(moved):
        if i + 1 >= keep:
            retired = tmp
            continue
        try:
            os.replace(tmp, f"{path}.{i + 1}")
        except OSError as e:
            _rollback_archives(path, moved, keep)
            _note_rotate_error(tmp, e)
            return False

    # 3) 当前文件 → .1
    try:
        os.replace(path, f"{path}.1")
    except OSError as e:
        _rollback_archives(path, moved, keep)
        _note_rotate_error(path, e)
        return False

    # 4) 退役那份最后删；删不掉告警但不算失败（数据完好，只是多一份）
    if retired is not None:
        try:
            os.remove(retired)
        except OSError as e:
            _note_rotate_error(retired, e)
    _rotate_state.update(error=None, path=path, ts=time.time())
    return True


def _rollback_archives(path, moved, keep):
    """轮转中途失败：把已经改成临时名的归档放回原位（数据一份都不少）。

    只处理本次 `moved` 里的文件：临时名还在就直接改回去；已经被挪到
    `.{i+1}` 的，从那里改回 `.{i}`。原来的 `.1..keep-1` 此刻都空着，
    不会互相覆盖。放不回去时只记录，不抛（不能让一次清理失败影响写台账）。
    """
    for i, tmp in moved:
        dst = f"{path}.{i}"
        try:
            if os.path.isfile(tmp):
                os.replace(tmp, dst)
            elif i + 1 < keep and os.path.isfile(f"{path}.{i + 1}"):
                os.replace(f"{path}.{i + 1}", dst)
        except OSError as e:
            _note_rotate_error(tmp, e)


def archives(path, keep=RECORDS_KEEP):
    """轮转出来的历史文件，从新到旧。"""
    out = []
    for i in range(1, max(1, keep)):
        p = f"{path}.{i}"
        if os.path.isfile(p):
            out.append(p)
    return out


def _remove_cache_file(path):
    """删一个缓存文件；只读属性先清掉再删。

    截图是程序自己生成的缓存，被备份/同步工具设成只读并不代表用户要
    永久保留。以前直接 `os.remove` 失败就放弃，上限永远收敛不了（S3）。
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass
    try:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)
        return True
    except OSError:
        return False


def prune_dir(directory, max_files=SHOTS_MAX_FILES, max_bytes=SHOTS_MAX_BYTES,
              every=0):
    """按修改时间删最旧的文件，直到数量/总字节都达标。返回删了几个。

    `every>0` 表示「每调用这么多次才真正扫一次目录」：调用方是每存一张
    截图调一次的，节流后扫描成本被摊平（见 SHOTS_PRUNE_EVERY 的说明）。
    直接调用（测试 / 上限校验）传 every=0，行为与以前一致：立刻扫、立刻删。
    """
    if every > 0:
        key = os.path.abspath(directory)
        n = _prune_tick.get(key, 0) + 1
        if n < every:
            _prune_tick[key] = n
            return 0
        _prune_tick[key] = 0
    files = []
    total = 0
    try:
        # scandir + entry.stat()：每个文件只 stat 一次（原来 os.stat 之后
        # 又 os.path.isfile 再 stat 一次，等于双倍开销）。
        with os.scandir(directory) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                files.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
    except OSError:
        return 0
    files.sort()
    removed = 0
    i = 0
    while i < len(files) and (len(files) > max_files or total > max_bytes):
        _, size, p = files[i]
        if _remove_cache_file(p):
            removed += 1
            total -= size
            del files[i]
        else:
            # 删不动：**不扣减**、留在计数里（免得上限假装达标，S3），
            # 继续尝试下一个，避免卡在这一条上死循环。
            i += 1
    return removed


def rotate_log(path, max_bytes=LOG_MAX_BYTES):
    """服务日志轮转：超限就把当前文件改名成 .1（覆盖旧的 .1）。

    失败**不再静默**（审查 R5）：Windows 上子进程还握着日志文件的句柄时
    `os.replace` 会 PermissionError —— 以前这里吞掉返回 False，文件上限
    就永远不生效，用户只看到盘越来越满。现在记下原因并打印一次，
    下一次扫描（`sweep_logs` / 下次 spawn）还会再试。
    """
    if max_bytes <= 0 or _size(path) < max_bytes:
        return False
    bak = path + ".1"
    try:
        if os.path.isfile(bak):
            os.remove(bak)
        os.replace(path, bak)
    except OSError as e:
        _note_rotate_error(path, e)
        return False
    _rotate_state.update(error=None, path=path, ts=time.time())
    return True


def sweep_logs(directory, max_bytes=LOG_MAX_BYTES, prefix=LOG_PREFIX,
               interval=LOG_SWEEP_INTERVAL, now=None):
    """兜底触发：扫一遍目录里的 OCR 服务日志，超限就轮转。返回轮转了几个。

    OCR 服务的轮转原本只在 `spawn` 时跑一次：服务开上几天不重启，日志就
    一直涨，上限永远不生效（审查 R5）。这里给一个**和 spawn 无关**的入口，
    由 `Config.load()` 顺便调（每个进程都有 10 分钟节流，只扫一次目录）。
    扫不动/转不动都不抛，失败原因进 `last_rotate_error()`。
    """
    now = time.time() if now is None else now
    key = os.path.abspath(directory)
    if now - _sweep_at.get(key, 0.0) < interval:
        return 0
    _sweep_at[key] = now
    try:
        names = os.listdir(directory)
    except OSError as e:
        _note_rotate_error(directory, e)
        return 0
    rotated = 0
    for name in names:
        if not name.startswith(prefix) or not name.endswith(".log"):
            continue
        path = os.path.join(directory, name)
        if rotate_log(path, max_bytes):
            rotated += 1
    return rotated


def dir_usage(directory):
    """目录文件数与总字节，给报告/界面用。"""
    try:
        names = os.listdir(directory)
    except OSError:
        return 0, 0
    n = 0
    total = 0
    for name in names:
        p = os.path.join(directory, name)
        try:
            if os.path.isfile(p):
                n += 1
                total += os.path.getsize(p)
        except OSError:
            pass
    return n, total
