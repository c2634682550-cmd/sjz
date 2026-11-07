"""抢购台账遥测上报器（客户端）。

只做一件事：把 `core/records.py` 写出来的 `data/records.jsonl` **增量**读出来，
按白名单裁成事件，批量上报给作者自建的收集服务。不改引擎、不改配置加载、
不碰 web/index.html —— 这里全程只读项目文件，写入只发生在 `data/telemetry/`。

为什么用字节游标而不是每次全量：
  · 台账是「挂机好几天、每抢一次一行」的追加文件，全量读会越跑越慢；
  · 游标记「上次读到的字节偏移 + 最后一行 sha256」，重启后从断点续读；
  · 文件被 `retention.rotate()` 换掉（新文件变小）时自动从 0 重读，
    重复的 event_id 由服务端唯一索引吃掉，所以「重读」不等于「重复入库」。

隐私边界（写死在 `_build_event()` 里，不发这些）：
  · 游戏账号 / 密码 / 截图文件名 / 进程列表 / 本机路径 / 许可见明文。
  只发：event_id、发生时间、品阶、商品名（可脱敏）、成交价、结果、
  卡点误差毫秒、客户端版本、license 哈希、机器指纹哈希。

用法（在仓库根，venv 里跑）：
    .venv\\Scripts\\python.exe tools\\telemetry_agent.py --interval 30
  单次跑一遍（排查/测试用）：
    .venv\\Scripts\\python.exe tools\\telemetry_agent.py --once

以后由启动器拉起的那一行命令见 `_telemetry.md` 的「部署命令」。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import license as lic                       # noqa: E402  只读：指纹/许可派生

# ---------------------------------------------------------------- 默认值

DEFAULT_STATE_DIR = os.path.join(ROOT, "data", "telemetry")
DEFAULT_RECORDS = os.path.join(ROOT, "data", "records.jsonl")
DEFAULT_SERVER = "http://127.0.0.1:8091"
DEFAULT_INTERVAL = 30
DEFAULT_BATCH = 100
DEFAULT_VERSION = "0.1.0"

QUEUE_NAME = "telemetry_queue.jsonl"
CURSOR_NAME = "telemetry_cursor.json"
REJECTED_NAME = "telemetry_rejected.jsonl"

# 服务端白名单必须与这里逐字一致（多一个字段就会被整批拒收）。
EVENT_KEYS = ("event_id", "occurred_at", "tier", "name", "price", "result",
              "skew_ms", "skip_reason")

# 台账 result → 上报用的三态。引擎只会产生前五种；后三种给「跳过」留口子。
RESULT_MAP = {
    "hit": "hit", "ok": "hit", "success": "hit",
    "miss": "miss", "no_money": "miss", "over_budget": "miss",
    "aborted": "miss", "fail": "miss", "failed": "miss",
    "skip": "skip", "skipped": "skip", "late_skip": "skip",
}

# 「极品 / 优品」是台账里唯一能稳定拿到的品阶信息（core/bricks.py 的同一套正则）。
# 不 import core.bricks：它会连带把 OCR/cv2 拉进来，上报器要能轻量独立跑。
_CRAFT_RE = re.compile("(极品|优品)")
_TIER_MAX = 16
_NAME_MAX = 64
_SKIP_MAX = 24

_STATE = {"dir": DEFAULT_STATE_DIR, "records": DEFAULT_RECORDS,
          "server": DEFAULT_SERVER, "queue": "", "cursor": "", "rejected": ""}


def _log(msg):
    try:
        print(f"[telemetry] {msg}", flush=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 配置

def _project_config():
    """只读 config.json 的 `telemetry` 段；文件坏/没有就返回空字典。

    读取失败不允许影响上报 —— 默认值已经能跑（台账默认就在 data/records.jsonl）。
    """
    path = os.path.join(ROOT, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    t = cfg.get("telemetry")
    return t if isinstance(t, dict) else {}


def _setup_paths(args):
    cfg = _project_config()
    state_dir = (args.state_dir or os.environ.get("TELEMETRY_STATE_DIR")
                 or cfg.get("state_dir") or DEFAULT_STATE_DIR)
    state_dir = os.path.abspath(state_dir)
    os.makedirs(state_dir, exist_ok=True)
    _STATE["dir"] = state_dir
    _STATE["queue"] = os.path.join(state_dir, QUEUE_NAME)
    _STATE["cursor"] = os.path.join(state_dir, CURSOR_NAME)
    _STATE["rejected"] = os.path.join(state_dir, REJECTED_NAME)
    _STATE["records"] = os.path.abspath(
        args.records or cfg.get("records_path") or DEFAULT_RECORDS)
    _STATE["server"] = (args.server or os.environ.get("TELEMETRY_URL")
                        or cfg.get("server_url") or DEFAULT_SERVER).rstrip("/")


# ---------------------------------------------------------------- 许可 / 密钥

def load_license(path=None):
    """读本地许可文件，返回 (payload, key)。

    key = `core/license._ledger_key(payload)`：嵌入密钥 + 许可里的稳定组件子集，
    服务端能用许可库里的 fp_vec 重算出同一把 key，所以签名只能由这台机器 +
    这份许可产生。验签失败只警告，不阻断（P-256 公钥在另一路迁移中，
    发布前可能还是占位符；真正判定仍以服务端许可库为准）。
    """
    p = path or lic.paths()["license"]
    if not os.path.isfile(p):
        raise SystemExit(f"找不到许可文件：{p}\n"
                         f"先在客户端激活，或给 --license 指一个 license.json")
    try:
        with open(p, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"许可文件读不出来（{type(e).__name__}）：{p}")
    payload = blob.get("payload") if isinstance(blob, dict) else None
    if not isinstance(payload, dict):
        # 兜底：有的旧文件直接把 payload 摊平存
        payload = blob if isinstance(blob, dict) else None
    if not isinstance(payload, dict) or not payload.get("lic_id"):
        raise SystemExit(f"许可文件格式不对（没有 payload.lic_id）：{p}")
    try:
        lic.verify_blob(blob)
    except Exception as e:                            # noqa: BLE001
        _log(f"本地许可验签未通过（{e}）—— 继续上报，服务端会按许可库判定")
    key = lic._ledger_key(payload)
    if not key:
        raise SystemExit("许可里没有可用的指纹组件，无法派生上报签名密钥")
    return payload, key


def ids_from_payload(payload):
    """license_id / 机器码都只发哈希，永不发明文。"""
    lid = str(payload.get("lic_id") or "")
    fp = str(payload.get("fp") or "")
    return (hashlib.sha256(("lic:" + lid).encode("utf-8")).hexdigest()[:32],
            hashlib.sha256(("fp:" + fp).encode("utf-8")).hexdigest()[:16])


# ---------------------------------------------------------------- 游标

# 游标位置的内容指纹窗口：一次 4KB 读。追加不会改动 offset 之前的字节，
# 所以窗口一致 = 同一份文件在继续写。注意这不是「整轮只读 4KB」：
# 一轮巡检还要读采样块，见下面的 `_OPEN_PER_ROUND` 口径。
_WINDOW_BYTES = 4096
# 已读区域采样：除游标前 4KB 外，再看文件头与 1/3、2/3 处的 4KB 块。
# 只比尾部会被「改头不改尾」骗过；采样成本是每轮 3~4 次 4KB 读，与台账规模无关。
# 采样哈希按 canonical（剥裸 \r）计算，换行风格翻转不算内容变化。
_SAMPLE_WINDOW = 4096
_SAMPLE_TAG = "c:"        # canonical 哈希前缀；无前缀 = 旧版游标的裸字节哈希
# 重锚上下文：锚点前必须连续一致的非空行数。只比末行哈希会被「重复内容」骗过。
_CONTEXT_LINES = 3
_GAP_NAME = "台账轮转缺口"
# A2：已发送行内容哈希（每行 16 hex）。归档被换成「已投递内容的截短副本」时，
# 按哈希并集去重，而不是按 offset 信任。只保留最前/最后各 512 条，
# 游标因此最多多出约 19KB（1024×16 hex），换来「截短副本不重发」。
_SENT_HEAD = 512
_SENT_TAIL = 512
# 单轮巡检的真实 I/O（实测，10 万行台账）：
#   · 空闲轮：3 次 open / 16KB（游标前 4KB 窗口 + 3 个 4KB 采样块）；
#   · 追加 1 行：约 6 次 open / 49KB（多出来的是重建 samples 12KB + head_canon 16KB）。
# 两者都与台账行数无关，但不能再写成「每轮只读 4KB」。`os.stat` 每轮 1 次。


def _atomic_write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _to_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _sent_field(cur, key, keep, tail):
    raw = cur.get(key)
    out = ([str(x)[:16] for x in raw if isinstance(x, str) and x]
           if isinstance(raw, list) else [])
    return out[-keep:] if tail else out[:keep]


def _sent_state(cursor):
    """已发送行哈希集合：最前 `_SENT_HEAD` 条 + 最后 `_SENT_TAIL` 条。

    只做「已投递内容的截短副本」去重（A2），集合必须有界，
    否则 10 万行台账的游标会膨胀到 MB 级。
    """
    head = _sent_field(cursor, "sent_head", _SENT_HEAD, tail=False)
    tail = _sent_field(cursor, "sent_tail", _SENT_TAIL, tail=True)
    return head, tail, set(head) | set(tail)


def _sent_key(line):
    return _line_sha256(line)[:16]


def _sent_add(head, tail, union, key):
    """记一条已投递行；返回 False = 该内容此前已经投递过。"""
    if key in union:
        return False
    union.add(key)
    if len(head) < _SENT_HEAD:
        head.append(key)
    tail.append(key)
    if len(tail) > _SENT_TAIL:
        del tail[0]
    return True


def _count_lines(path, chunk=1 << 20):
    """完整行的物理行数（只数换行符），用于按代累计「全量历史行号」。"""
    total = 0
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                total += block.count(b"\n")
    except OSError:
        return 0
    return total


def load_cursor():
    try:
        with open(_STATE["cursor"], encoding="utf-8") as f:
            cur = json.load(f)
        if not isinstance(cur, dict):
            return {}
        ctx = cur.get("tail_context")
        if not isinstance(ctx, list):
            ctx = []
        raw_samples = cur.get("samples")
        samples = []
        if isinstance(raw_samples, list):
            for item in raw_samples:
                if (isinstance(item, list) and len(item) == 2
                        and isinstance(item[1], str)):
                    samples.append([_to_int(item[0]), item[1]])
        return {"path": str(cur.get("path") or ""),
                "offset": _to_int(cur.get("offset")),
                "last_line_sha256": str(cur.get("last_line_sha256") or ""),
                "size": _to_int(cur.get("size")),
                "mtime": _to_int(cur.get("mtime")),
                "file_id": str(cur.get("file_id") or ""),
                "canon_offset": _to_int(cur.get("canon_offset")),
                "line_no": _to_int(cur.get("line_no")),
                "tail_context": [str(x) for x in ctx if isinstance(x, str)],
                "window_sha256": str(cur.get("window_sha256") or ""),
                "head_canon": str(cur.get("head_canon") or ""),
                "samples": samples,
                "sent_head": _sent_field(cur, "sent_head", _SENT_HEAD, False),
                "sent_tail": _sent_field(cur, "sent_tail", _SENT_TAIL, True),
                "line_chain": str(cur.get("line_chain") or ""),
                "last_scan_ms": float(cur.get("last_scan_ms") or 0),
                "last_action": str(cur.get("last_action") or ""),
                "updated_at": float(cur.get("updated_at") or 0)}
    except (OSError, ValueError, TypeError):
        return {}


def save_cursor(path, cur):
    """落盘游标。除偏移外还记文件身份、行号、上下文与 4KB 窗口指纹，
    下一轮才能区分「同一份文件继续追加」「换行风格漂移」「整份被替换」。
    """
    doc = {"path": path,
           "offset": _to_int(cur.get("offset")),
           "size": _to_int(cur.get("size")),
           "mtime": _to_int(cur.get("mtime")),
           "last_line_sha256": str(cur.get("last_line_sha256") or ""),
           "file_id": str(cur.get("file_id") or ""),
           "canon_offset": _to_int(cur.get("canon_offset")),
           "line_no": _to_int(cur.get("line_no")),
           "tail_context": list(cur.get("tail_context") or [])[-_CONTEXT_LINES:],
           "window_sha256": str(cur.get("window_sha256") or ""),
           "head_canon": str(cur.get("head_canon") or ""),
           "samples": list(cur.get("samples") or [])[:4],
           "sent_head": _sent_field(cur, "sent_head", _SENT_HEAD, False),
           "sent_tail": _sent_field(cur, "sent_tail", _SENT_TAIL, True),
           "line_chain": str(cur.get("line_chain") or ""),
           "last_scan_ms": round(float(cur.get("last_scan_ms") or 0), 3),
           "last_action": str(cur.get("last_action") or ""),
           "updated_at": round(time.time(), 3)}
    _atomic_write(_STATE["cursor"],
                  json.dumps(doc, ensure_ascii=False).encode("utf-8"))


def _line_sha256(line):
    """单行内容哈希：行尾 `\\r\\n` / `\\n` / `\\r` 一律剥掉再算。

    台账在 Windows 上可能是 CRLF，也可能被改成 LF；换行风格不属于「内容」，
    写入、回看、哈希三处必须用同一口径，否则同一行会算出两个哈希。
    """
    return hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()


def _canon_len(data):
    """canonical 字节数：去掉行尾 `\\r` 后的长度。

    JSONL 里的 `\\r` 只会是行尾（JSON 会把控制字符转义），所以直接数裸 `\\r`
    就等价于「换行风格无关的内容长度」。
    """
    return len(data) - data.count(b"\r")


def _file_stat(path):
    """一次 stat 取齐 (文件身份, 大小, mtime_ns)。

    A1 的判据要用 mtime：净长度不变的「前插一行 + 删一行」只看 size 看不出来，
    但 mtime 一定变。多取一项不增加 stat 次数（仍然是每轮 1 次 os.stat）。
    """
    st = os.stat(path)
    if os.name == "nt":
        fid = f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}"
    else:
        fid = f"{st.st_dev}:{st.st_ino}"
    return fid, st.st_size, int(getattr(st, "st_mtime_ns", st.st_mtime * 1e9))


def _file_identity(path):
    """返回 (文件身份, 大小)。身份变化 = 同一路径上换了一份文件。

    Windows 的 st_ctime 是创建时间（追加/改名不变，替换成新文件才变）；
    POSIX 上 st_ctime 会随内容变化，只用 (dev, ino)。
    """
    fid, size, _ = _file_stat(path)
    return fid, size


def _window_sha256(path, offset, window=_WINDOW_BYTES):
    """游标前 window 字节的哈希（追加不会改这段，所以它应恒定）。

    返回空串表示读不全（文件变小/读失败），调用方按异常处理。
    """
    if offset <= 0:
        return ""
    start = max(0, offset - window)
    want = offset - start
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(want)
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest() if len(data) == want else ""


def _canon_prefix_sha(path, want=_SAMPLE_WINDOW):
    """文件开头 want 个 canonical 字节的哈希（去掉裸 `\\r`）。

    换行风格变化不改 canonical 内容，所以这个指纹跨 CRLF/LF 稳定；
    只改文件头（末行、行号、上下文都不变）则它能打出来。
    """
    raw = b""
    try:
        with open(path, "rb") as f:
            while len(raw.replace(b"\r", b"")) < want:
                block = f.read(16 * 1024)
                if not block:
                    break
                raw += block
    except OSError:
        return ""
    canon = raw.replace(b"\r", b"")[:want]
    return hashlib.sha256(canon).hexdigest() if canon else ""


def _sample_digest(data):
    """采样块的 canonical 哈希（剥掉裸 `\r`）。

    CRLF↔LF 等量翻转会让裸字节哈希全变，但内容没变；采样若按裸字节存，
    就会把「中间换行翻转」误判成整份替换（R1d2）。统一用 canonical。
    """
    return _SAMPLE_TAG + hashlib.sha256(data.replace(b"\r", b"")).hexdigest()


def _sample_matches(data, want):
    """`want` 带 `c:` 前缀按 canonical 比；旧游标没有前缀，按裸字节比（兼容）。"""
    if not want:
        return False
    if want.startswith(_SAMPLE_TAG):
        return _sample_digest(data) == want
    return hashlib.sha256(data).hexdigest() == want


def _chain_lines(chain_hex, lines):
    """行序列链：`acc = sha256(acc || sha256(canonical 行))`，逐行折叠。

    canonical 行哈希剥掉行尾 `\\r\\n`，所以 **换行风格翻转不改变这条链**；
    而行的增删、改写、移位一定改变它。它按「行」而不是按「原始字节位置」定义，
    因此不受 `\\r` 增删造成的整体位移影响 —— 这正是固定位置采样块的盲区（R1d2）。
    """
    try:
        acc = bytes.fromhex(chain_hex) if chain_hex else b""
    except (TypeError, ValueError):
        acc = b""                                   # 链脏了就当未知，重扫重建
    for line in lines:
        acc = hashlib.sha256(acc + _line_sha256(line).encode()).digest()
    return acc.hex() if acc else ""


def _prefix_line_chain(path, upto):
    """重算 0..upto 的行序列链；`upto` 必须是行尾（游标总落在换行之后）。

    只在两处调用，都是低频路径：
      · 采样块对不上时做一次 canonical 复核（代替「直接全量重读」）；
      · 旧版游标第一次落盘时建一次基线（之后靠 `_chain_lines` 增量维护）。
    成本 = 读 upto 字节 + 每行一次 sha256：暖缓存 10 万行约 0.2s 量级，
    冷缓存首扫约 1.5s（与重锚同属一次性成本，不是巡检成本）。
    返回 None = 读不全/游标不在行尾，判据不足。
    """
    upto = max(0, int(upto))
    if upto <= 0:
        return ""
    acc = b""
    got = 0
    line = bytearray()
    try:
        with open(path, "rb") as f:
            while got < upto:
                block = f.read(min(1 << 20, upto - got))
                if not block:
                    break
                got += len(block)
                parts = block.split(b"\n")
                for seg in parts[:-1]:
                    line += seg
                    if line.strip():
                        acc = hashlib.sha256(
                            acc + _line_sha256(bytes(line)).encode()).digest()
                    line = bytearray()
                line += parts[-1]
    except OSError:
        return None
    if got < upto or line.strip():
        return None                                  # 截断 / 游标不在行尾
    return acc.hex() if acc else ""


def _sample_blocks(path, offset, window=_SAMPLE_WINDOW):
    """已读区域的采样块：文件头 + 游标 1/3、2/3 处的 4KB 原始字节。

    用于「身份不变、末行不变、但中间/头部被就地改写」的判定。
    只取整块落在游标之前的采样，块数固定，成本与台账规模无关。
    """
    if offset < 2 * window:
        return []
    positions = [0, offset // 3, (2 * offset) // 3]
    out = []
    used = []
    try:
        with open(path, "rb") as f:
            for pos in positions:
                if pos + window > offset:
                    continue
                if any(abs(pos - u) < window for u in used):
                    continue
                f.seek(pos)
                data = f.read(window)
                if len(data) != window:
                    continue
                used.append(pos)
                out.append([pos, _sample_digest(data)])
    except OSError:
        return out
    return out


def _samples_ok(path, samples, window=_SAMPLE_WINDOW):
    """采样块逐个复核。返回 False = 游标之前的已读区域被动过，必须重读。"""
    if not samples:
        return True
    try:
        with open(path, "rb") as f:
            for pos, want in samples:
                f.seek(pos)
                got = f.read(window)
                if len(got) != window:
                    return False
                if not _sample_matches(got, want):
                    return False
    except OSError:
        return False
    return True


def _resume_offset(path, cursor):
    """归档/当前文件若前缀就是游标记住的那份，就跳过已读的 prefix。"""
    offset = _to_int(cursor.get("offset"))
    window = str(cursor.get("window_sha256") or "")
    if not offset or not window:
        return 0
    try:
        if os.path.getsize(path) < offset:
            return 0
    except OSError:
        return 0
    return offset if _window_sha256(path, offset) == window else 0


def _archive_harvested(path, cursor):
    """归档整份都是已采内容（内容指纹一致）→ 不必从 0 重读。

    判据必须是「游标里存的每一块采样都可比且都对得上」：
      · 归档比采样点短（只有头部 4KB 可比）时不能判已采 —— 头部相同不代表
        整份相同，R4b 就是这样静默丢掉尾部的未采行；
      · 任何一块对不上就不跳（宁可多读，不静默丢行）。
    """
    samples = cursor.get("samples") or []
    if not samples:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    comparable = [[p, s] for p, s in samples if p + _SAMPLE_WINDOW <= size]
    if len(comparable) != len(samples):
        return False           # 采不全 = 判据不足，按「未采完」处理
    try:
        with open(path, "rb") as f:
            for pos, want in comparable:
                f.seek(pos)
                got = f.read(_SAMPLE_WINDOW)
                if len(got) != _SAMPLE_WINDOW:
                    return False
                if not _sample_matches(got, want):
                    return False
    except OSError:
        return False
    return True


def _same_content(path, cursor):
    """身份变了但内容可能是同一份（ACL/备份改写只改 st_ino，不改字节）。

    只有三条指纹全中才认「同一文件、身份抖动」：
      1. 游标前 4KB 窗口逐字节一致；
      2. 文件头 canonical 4096 字节一致；
      3. 每一块已读区域采样都一致。
    任何一条缺失（旧游标）或对不上，一律回落到轮转/替换判定，不做内容豁免。
    """
    offset = _to_int(cursor.get("offset"))
    window = str(cursor.get("window_sha256") or "")
    head = str(cursor.get("head_canon") or "")
    samples = cursor.get("samples") or []
    if not offset or not window or not head or not samples:
        return False
    if _window_sha256(path, offset) != window:
        return False
    if _canon_prefix_sha(path) != head:
        return False
    return _samples_ok(path, samples)


def _old_in_archives(path, old_id):
    """旧文件身份是否还在 `.1..32` 里（是 = 真轮转，不是身份抖动）。"""
    if not old_id:
        return False
    for _, p in _archive_generations(path):
        try:
            sid, _ = _file_identity(p)
        except OSError:
            continue
        if sid == old_id:
            return True
    return False


def _tail_line_sha256(path, offset):
    """offset 之前最后一行的 canonical 哈希（诊断/兼容用）。

    从 offset 往前按块回看，直到真的碰到换行符，不再假设「一行 <= 8KB」。
    正常一行一次 8KB 读取就够；超长行才会多读几块，属于异常路径。
    """
    if offset <= 0:
        return ""
    chunk = 8192
    start = offset
    data = b""
    try:
        with open(path, "rb") as f:
            while start > 0:
                n = min(chunk, start)
                f.seek(start - n)
                data = f.read(n) + data
                start -= n
                body = data.rstrip(b"\r\n")
                idx = body.rfind(b"\n")
                if idx >= 0:
                    return _line_sha256(body[idx + 1:])
                if start == 0:
                    return _line_sha256(body)
                chunk = min(chunk * 2, 64 * 1024 * 1024)
    except OSError:
        return ""
    return ""


def _find_reanchor(path, offset, last_hash, canon_offset, line_no, context):
    """换行风格漂移时重新对齐游标；判据严格到「内容重复」骗不过。

    四个条件必须同时成立：
      1. 该行 canonical 哈希 == 旧末行哈希；
      2. canonical 字节偏移 == 旧游标（换行风格不改内容长度）；
      3. 物理行号 == 旧行号（插入/删除行会让行号错位）；
      4. 锚点前连续 _CONTEXT_LINES 行哈希与旧上下文逐行一致。
    任何一条不满足就返回 None，调用方按「替换」全量重读，绝不静默跳行。
    """
    if not last_hash or canon_offset <= 0:
        return None
    want_ctx = list(context or [])
    canon = 0
    line_no_now = 0
    recent = []
    line = bytearray()
    raw_done = 0
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                pos = 0
                while True:
                    nl = block.find(b"\n", pos)
                    if nl < 0:
                        line += block[pos:]
                        break
                    line += block[pos:nl]
                    line_end = raw_done + nl + 1
                    line_no_now += 1
                    canon += _canon_len(bytes(line)) + 1
                    digest = _line_sha256(bytes(line))
                    if line.strip():
                        recent.append(digest)
                        if len(recent) > _CONTEXT_LINES:
                            recent.pop(0)
                    if canon > canon_offset:
                        return None
                    if (canon == canon_offset and line_no_now == line_no
                            and digest == last_hash and recent == want_ctx):
                        return line_end
                    line = bytearray()
                    pos = nl + 1
                raw_done += len(block)
    except OSError:
        return None
    return None


def _archive_generations(path):
    """轮转归档，按代数从新到旧：`path.1` 是最近一次轮转出来的那份。

    1..32 全量探测，不再「缺 i 且缺 i+1 就 break」——连续两代一起缺失时
    那样会把更旧的 `.4..` 漏掉（R3b）。只在轮转/身份变化分支调用，
    每轮最多 32 次 stat，空闲轮不受影响。
    """
    out = []
    for i in range(1, 33):
        p = f"{path}.{i}"
        if os.path.isfile(p):
            out.append((i, p))
    return out


def _read_complete_lines(path, from_off=0):
    """从 from_off 读到最后一个完整换行。

    返回 (行列表, 行尾偏移, canonical 字节数, 换行符个数)。残行不返回，
    留给下一次（写侧是整行 flush，残行只会出现在崩溃现场）。
    """
    from_off = max(0, from_off)
    try:
        with open(path, "rb") as f:
            f.seek(from_off)
            buf = f.read()
    except OSError:
        return [], from_off, 0, 0
    cut = buf.rfind(b"\n")
    if cut < 0:
        return [], from_off, 0, 0
    chunk = buf[:cut + 1]
    raw = [ln for ln in chunk.split(b"\n") if ln.strip()]
    return raw, from_off + cut + 1, _canon_len(chunk), chunk.count(b"\n")


def _avg_line_bytes(cursor):
    """游标已读区域的平均行长（canonical 字节/行），用于缺口事件的丢失量估算。"""
    line_no = _to_int(cursor.get("line_no"))
    canon = _to_int(cursor.get("canon_offset")) or _to_int(cursor.get("offset"))
    if line_no <= 0 or canon <= 0:
        return 0
    return canon / float(line_no)


def _est_lines(est_bytes, avg_bytes):
    """按平均行长把估算字节数折成行数（向上取整）；没有平均行长就返回 0。"""
    est_bytes = max(0, int(est_bytes or 0))
    if not est_bytes or avg_bytes <= 0:
        return 0
    return int((est_bytes + int(avg_bytes) - 1) // int(avg_bytes))


def _gap_line(offset, old_size, old_id, ts, gen=None, line_from=0,
              est_bytes=0, avg_bytes=0):
    """归档缺失时显式缺口事件。

    `gen` 给出缺失的归档代数（`.N`）；`gen=None` 表示游标所属的旧文件本身被清理。
    `line_from` / `line_no_end` 是行号区间：起点是游标已读行数的下一行，
    终点按平均行长估算（未知时只留起点并在 name 里写明「行数不可知」）。
    `elapsed` 记估算丢失字节数（估算值，不是精确值）。
    走正常事件管线（result=skip），服务端按 event_id 去重；不静默跳过。
    """
    est_bytes = max(0, int(est_bytes or 0))
    est_lines = _est_lines(est_bytes, avg_bytes)
    line_from = max(0, int(line_from))
    line_end = (line_from + est_lines - 1) if (line_from and est_lines) else 0
    where = f".{gen} 代缺失" if gen else "上一份文件已清理"
    if line_from and line_end:
        span = f"行{line_from}–{line_end}，估算约{est_lines}行"
    elif line_from:
        span = f"自第{line_from}行起，行数不可知"
    else:
        span = "行号不可知"
    row = {"ts": round(float(ts or time.time()), 3), "t": "",
           "name": f"{_GAP_NAME}（{where}，{span}）",
           "price": None, "result": "skip",
           "skip_reason": "archive_missing",
           "elapsed": est_bytes, "line_no": line_from,
           "line_no_end": line_end, "est_lines": est_lines,
           "shot": f"{old_id}@{offset}" + (f"|gen={gen}" if gen else ""),
           "mode": "gap", "tier": "系统", "quota": None, "verified": False}
    return json.dumps(row, ensure_ascii=False).encode("utf-8")


def _make_cursor(path, offset, last_hash, canon, line_no, context, fid, size,
                 action, window=None, samples=None, head=None, scan_ms=0,
                 chain="", mtime=0, sent_head=None, sent_tail=None):
    if window is None:
        window = _window_sha256(path, offset)
    if samples is None:
        samples = _sample_blocks(path, offset)
    if head is None:
        head = _canon_prefix_sha(path)
    return {"path": path, "offset": int(offset),
            # size 不能小于 offset：追加与 stat 之间可能有竞态，否则下一轮会
            # 误判「文件变小」，把正常追加当成替换（A1 的 ±1 字节微调同理）。
            "size": max(int(size), int(offset)),
            "mtime": int(mtime),
            "last_line_sha256": last_hash, "file_id": fid,
            "canon_offset": int(canon), "line_no": int(line_no),
            "tail_context": list(context)[-_CONTEXT_LINES:],
            "window_sha256": window, "head_canon": head, "samples": samples,
            "sent_head": list(sent_head or [])[: _SENT_HEAD],
            "sent_tail": list(sent_tail or [])[-_SENT_TAIL:],
            "line_chain": str(chain or ""),
            "last_scan_ms": round(float(scan_ms or 0), 3),
            "last_action": action}


def _read_after_identity_change(path, cursor, fid, size):
    """当前路径已不是游标记住的那份文件：轮转 → 先补归档尾巴；替换 → 全量重读。

    轮转判据：旧文件的身份在 `.1..N` 里能找到（rename 保留 inode/创建时间）。
    归档链中间断代 → 每一代缺失都发一条 gap（写明代数）。
    行号口径（A3）：gap 的 `line_no` 是「该代在全量历史行号中的起点」
    = 游标已读行数 + 1 + 更旧各代行数（缺失代按平均体量估算）前缀和，
    不是当前文件里的行号，否则缺一代就会让后面所有区间一起前移。
    旧文件被清理（身份不在归档链）→ 它未采集的尾巴单发一条 gap。
    归档内容与游标记录对不上 → 按已发送行内容哈希并集去重（A2），不按 offset 信任。
    """
    offset = _to_int(cursor.get("offset"))
    old_id = str(cursor.get("file_id") or "")
    old_size = _to_int(cursor.get("size"))
    ts = float(cursor.get("updated_at") or 0) or time.time()
    lines = []
    gens = _archive_generations(path)
    present = {i for i, _ in gens}
    matched = None
    for i, p in gens:
        try:
            sid, _, _ = _file_stat(p)
        except OSError:
            continue
        if old_id and sid == old_id:
            matched = i
            break

    avg = _avg_line_bytes(cursor)
    line_from = _to_int(cursor.get("line_no")) + 1
    # 缺失代没有文件可量：用现存归档的平均体量估算，明确标成「估算」
    gen_bytes = 0
    if gens:
        try:
            gen_bytes = int(sum(os.path.getsize(p) for _, p in gens) / len(gens))
        except OSError:
            gen_bytes = 0
    action = "replaced"
    sent_head, sent_tail, sent = _sent_state(cursor)

    def gap(gen, est_bytes, start):
        lines.append(_gap_line(offset, old_size, old_id, ts, gen=gen,
                               line_from=start, est_bytes=est_bytes,
                               avg_bytes=avg))

    def take(part, dedupe=True):
        """归档行按内容哈希去重；当前台账（替换后的新内容）不去重。"""
        out = []
        for ln in part:
            key = _sent_key(ln)
            if dedupe and key in sent:
                continue
            _sent_add(sent_head, sent_tail, sent, key)
            out.append(ln)
        return out

    # 全量历史行号的游标：游标已读行数的下一行，是「旧文件尾巴 / 最旧代」的起点。
    cur_line = line_from

    if matched is not None:
        # 旧文件成了 .matched；更小代号的归档比它新，按时间顺序补读。
        try:
            unread = max(0, _count_lines(f"{path}.{matched}")
                        - _to_int(cursor.get("line_no")))
        except OSError:
            unread = 0
        for i in range(matched, 0, -1):
            p = f"{path}.{i}"
            if i == matched:
                part, _, _, _ = _read_complete_lines(p, offset)
                lines.extend(take(part, dedupe=False))   # offset 之后必然是未采行
                cur_line += unread
                continue
            if i not in present:
                gap(i, gen_bytes, cur_line)
                cur_line += _est_lines(gen_bytes, avg)
                _log(f"轮转归档 .{i} 缺失，记缺口事件（该代起点行 {cur_line} 之前）")
                continue
            total = _count_lines(p)
            start = _resume_offset(p, cursor)
            if (_archive_harvested(p, cursor) and not start
                    and os.path.getsize(p) <= offset):
                _log(f"归档 .{i} 内容指纹与已采段一致且不超出游标，跳过不重读")
            else:
                part, _, _, _ = _read_complete_lines(p, start)
                lines.extend(take(part))
            cur_line += total
        action = "rotation"
        _log(f"检测到轮转：先补 .{matched} 未采集的尾巴，再切到当前台账")
    else:
        top = max(present) if present else 0
        if old_id and offset:
            # 旧文件已不在归档链：它未采集的尾巴无法恢复，单独一条 gap。
            # 已知未读字节（old_size-offset）时按字节估行；当前文件比游标还小
            # 说明旧文件在两次巡检之间被换掉，尾巴长度不可知，也要留痕。
            est_tail = max(0, old_size - offset)
            if est_tail > 0 or size < offset:
                gap(None, est_tail, cur_line)
                cur_line += _est_lines(est_tail, avg)
                _log("旧文件未采集尾巴无法从归档恢复，单独记一条缺口事件")
        for i in range(top, 0, -1):
            p = f"{path}.{i}"
            if i not in present:
                gap(i, gen_bytes, cur_line)
                cur_line += _est_lines(gen_bytes, avg)
                _log(f"轮转归档 .{i} 缺失，记缺口事件（缺代数已写入 shot/name）")
                continue
            total = _count_lines(p)
            start = _resume_offset(p, cursor)
            if (_archive_harvested(p, cursor) and not start
                    and os.path.getsize(p) <= offset):
                _log(f"归档 .{i} 内容指纹与已采段一致且不超出游标，跳过不重读")
            else:
                part, _, _, _ = _read_complete_lines(p, start)
                lines.extend(take(part))
            cur_line += total
        if gens:
            action = "rotation"
            _log("旧文件身份不在归档链里：按代记缺口，并补读现存归档")
        else:
            if old_id and size < offset:
                action = "rotation"
            _log("台账文件身份变化且不在归档里（整份替换），从 0 重读")

    # 当前台账若只是「旧内容的副本」就跳过已读前缀，避免把已采行再报一遍
    cur_start = _resume_offset(path, cursor) if action == "rotation" else 0
    cur_lines, cur_off, cur_canon, cur_nl = _read_complete_lines(path, cur_start)
    if cur_start:
        cur_canon += _to_int(cursor.get("canon_offset"))
        cur_nl += _to_int(cursor.get("line_no"))
    # 当前文件按「新文件」处理：替换后同样内容也允许重报（服务端 event_id 兜底），
    # 只有归档才按已发送哈希去重。
    lines.extend(take(cur_lines, dedupe=False))
    try:
        cur_fid, cur_size, cur_mtime = _file_stat(path)
    except OSError:
        cur_fid, cur_size, cur_mtime = fid, size, 0
    if cur_lines:
        cur_hash = _line_sha256(cur_lines[-1])
        cur_ctx = [_line_sha256(ln) for ln in cur_lines[-_CONTEXT_LINES:]]
    else:
        cur_hash = str(cursor.get("last_line_sha256") or "")
        cur_ctx = list(cursor.get("tail_context") or [])
    # 行链路：从 0 读全 → 重建；从旧游标处续读 → 接着旧链往后折；
    # 其它情况（无法确认前缀）一律留空 = 未知，不做内容豁免。
    if cur_start == 0:
        cur_chain = _chain_lines("", cur_lines)
    elif cur_start == offset:
        cur_chain = _chain_lines(str(cursor.get("line_chain") or ""), cur_lines)
    else:
        cur_chain = ""
    return lines, _make_cursor(path, cur_off, cur_hash, cur_canon, cur_nl,
                               cur_ctx, cur_fid, cur_size, action,
                               chain=cur_chain, mtime=cur_mtime,
                               sent_head=sent_head, sent_tail=sent_tail)


def read_new_lines(path, cursor):
    """返回 (新行列表, 新游标 dict)。只吃完整行，残行留到下次。

    判定顺序固定，三类事件互斥：
      1. 文件身份变了 → 旧文件还在 `.1..N` 里就按轮转补读；不在归档里但
         内容指纹 + 四项锚点全中 → 判身份抖动（ACL/备份改写），继续追加；
         内容也对不上 → 替换全量重读；
      2. 身份不变 → 比游标处 4KB 内容指纹；一致就是继续追加；
      3. 指纹不一致才可能是换行漂移，必须末行哈希 / canonical 偏移 /
         行号 / 前 3 行上下文四项全中，否则按替换全量重读；
         采样块对不上时还有最后一道：0..offset 的行序列链（按行定义、
         不受 `\\r` 位移影响）一致 = 换行风格翻转，不重读。
    """
    try:
        fid, size, mtime = _file_stat(path)
    except OSError:
        return [], cursor
    offset = _to_int(cursor.get("offset"))
    last_hash = str(cursor.get("last_line_sha256") or "")
    canon = _to_int(cursor.get("canon_offset"))
    line_no = _to_int(cursor.get("line_no"))
    context = list(cursor.get("tail_context") or [])
    window = str(cursor.get("window_sha256") or "")
    head = str(cursor.get("head_canon") or "")
    samples = list(cursor.get("samples") or [])
    chain = str(cursor.get("line_chain") or "")
    old_id = str(cursor.get("file_id") or "")
    old_size = _to_int(cursor.get("size"))
    old_mtime = _to_int(cursor.get("mtime"))
    action = "append"
    scan_ms = 0.0
    start_offset = offset
    jitter = False
    sent_head, sent_tail, sent = _sent_state(cursor)

    if cursor.get("path") and cursor.get("path") != path:
        offset, last_hash, canon, line_no, context = 0, "", 0, 0, []
        window, head, samples, old_id, chain = "", "", [], "", ""
        old_size, old_mtime = 0, 0
        sent_head, sent_tail, sent = [], [], set()

    def _scan():
        """重锚是一次性成本，与台账行数线性相关（不是巡检成本）。

        同一份台账的实测差异很大：暖缓存（刚写完再读）10 万行约 0.15–0.22s，
        冷缓存（换进程/掉页后首次扫描）10 万行约 1.5s、50 万行约 4.3s。
        日志里的数字只对本次运行有效，排障时按「暖/冷」两档理解。
        """
        t0 = time.perf_counter()
        got = _find_reanchor(path, offset, last_hash, canon, line_no, context)
        return got, (time.perf_counter() - t0) * 1000

    if offset and old_id and fid and old_id != fid:
        # 先看旧文件是否还在归档链里：在 = 真轮转，按轮转语义补读。
        if _old_in_archives(path, old_id):
            return _read_after_identity_change(path, cursor, fid, size)
        # 不在归档里才可能是「同一文件、身份抖动」（ACL/备份软件改写只改 st_ino）。
        # 判据不能只看采样：再要求四项重锚恰好落在 offset（末行哈希 / canonical
        # 偏移 / 行号 / 前 3 行上下文全中），与漂移判定同等严格。
        # A4：四项锚点 + 窗口 + 头 4KB + 三个采样块之间仍有 4096..12288 的缝，
        # 只改这一段会让所有指纹全中；行链按行覆盖 0..offset，是最终判据。
        if size >= offset and _same_content(path, cursor):
            if chain:
                t0 = time.perf_counter()
                prefix_chain = _prefix_line_chain(path, offset)
                scan_ms = (time.perf_counter() - t0) * 1000
                jitter = prefix_chain == chain
            else:
                anchor, scan_ms = _scan()
                jitter = anchor == offset
        if jitter:
            _log("文件身份变了但内容与四项锚点全中（ACL/备份改写导致身份抖动），"
                 "按同一文件继续，不重读")
        else:
            return _read_after_identity_change(path, cursor, fid, size)

    if offset:
        anchor = None
        scan_ms = 0.0
        if size < offset:
            # 变小：可能是 LF→CRLF 反向漂移，也可能是截断/轮转（A1）。
            # 绝不把 offset 往 ±1 字节微调：只有「当前文件完整行链 == 旧行链」
            # 才认作换行漂移并重锚到文件尾；否则一律按替换全量重读。
            # 官方修正（size<offset 分支）：走 replaced 或完整行链复核后重锚。
            head_now = _canon_prefix_sha(path) if head else ""
            if head and head_now and head_now != head:
                _log("台账头部内容变了（不是换行漂移），从 0 重读")
                offset, last_hash, canon = 0, "", 0
                line_no, context, action = 0, [], "replaced"
            else:
                anchor = None
                t0 = time.perf_counter()
                full_chain = _prefix_line_chain(path, size) if chain else None
                scan_ms = (time.perf_counter() - t0) * 1000
                if chain and full_chain == chain:
                    anchor = size
                if anchor is not None:
                    _log(f"换行漂移：重锚 {offset}→{anchor}（行链完全一致，不重读；"
                         f"一次性扫描 {size} 字节 {scan_ms:.1f}ms，这是暖缓存值，"
                         f"冷缓存首扫约为其 5–7 倍）")
                    offset, action = anchor, "reanchor"
                elif action != "replaced":
                    _log("台账被替换（同路径但长度/内容对不上），从 0 重读")
                    offset, last_hash, canon = 0, "", 0
                    line_no, context, action = 0, [], "replaced"
        else:
            probe = _window_sha256(path, offset) if window else ""
            if window and probe and probe != window:
                head_now = _canon_prefix_sha(path) if head else ""
                if head and head_now and head_now != head:
                    _log("台账头部内容变了（不是换行漂移），从 0 重读")
                    offset, last_hash, canon = 0, "", 0
                    line_no, context, action = 0, [], "replaced"
                else:
                    anchor, scan_ms = _scan()
                    if anchor is not None and anchor != offset:
                        _log(f"换行漂移：重锚 {offset}→{anchor}"
                             f"（行号/上下文校验通过，不重读；"
                             f"一次性扫描 {size} 字节 {scan_ms:.1f}ms，"
                             f"暖缓存值，冷缓存首扫约为其 5–7 倍）")
                        offset, action = anchor, "reanchor"
                    elif anchor is None:
                        _log("台账被替换（游标前内容变了），从 0 重读")
                        offset, last_hash, canon = 0, "", 0
                        line_no, context, action = 0, [], "replaced"
                    else:
                        # 四项锚点恰好都还在 offset，但尾部窗口变了：A1 的核心，
                        # 「锚点仍在 offset」不能当成无需复核。前插一行 + 删一行
                        # 净长度不变时正是这种形态，必须用 0..offset 行链定论。
                        t0 = time.perf_counter()
                        prefix_chain = _prefix_line_chain(path, offset) if chain else None
                        scan_ms += (time.perf_counter() - t0) * 1000
                        if chain and prefix_chain == chain:
                            _log(f"尾部窗口变了但 0..{offset} 行链一致"
                                 f"（换行风格翻转），不重读；一次性复核 {offset} 字节")
                            action = "reanchor"
                        else:
                            _log("已读区域内容变了（锚点全中但行链不一致），从 0 重读")
                            offset, last_hash, canon = 0, "", 0
                            line_no, context, action = 0, [], "replaced"
            elif window and probe and not samples and offset >= 2 * _SAMPLE_WINDOW:
                # 旧版游标只看尾部，判据不足：保守重读一次，宁可多读不静默跳行
                _log("游标缺少已读区域指纹，判据不足，保守全量重读一次")
                offset, last_hash, canon = 0, "", 0
                line_no, context, action = 0, [], "replaced"
            elif window and probe and not _samples_ok(path, samples):
                # 采样块按「固定原始位置」存放，`\r` 增删会让整段位移，
                # 固定窗口切出的 canonical 内容随之变化 —— 采样失配不等于内容变化。
                # 这里用「按行定义、与原始偏移无关」的行链做一次 canonical 复核：
                # 链一致 = 只是换行风格翻转（R1d2），不重读；链不一致才判替换。
                prefix_chain = _prefix_line_chain(path, offset) if chain else None
                if chain and prefix_chain == chain:
                    _log(f"已读区域采样不一致，但 0..{offset} 的行链完全一致"
                         f"（换行风格翻转），不重读；一次性前缀复核 {offset} 字节")
                    action = "reanchor"      # 只是刷新采样/窗口指纹，offset 不动
                else:
                    _log("已读区域内容变了（改头/中间就地改写），从 0 重读")
                    offset, last_hash, canon = 0, "", 0
                    line_no, context, action = 0, [], "replaced"
            elif not window and last_hash:
                # 旧版游标没有窗口指纹：回退到末行哈希校验，对不上就全量重读
                got = _tail_line_sha256(path, offset)
                if got and got != last_hash:
                    _log("台账被替换（旧游标末行对不上），从 0 重读")
                    offset, last_hash, canon = 0, "", 0
                    line_no, context, action = 0, [], "replaced"

        # A1：净长度不变（游标前插一行 + 删一行）时，尾部窗口、三个采样块、
        # 四项锚点会全部命中，仅凭它们会静默丢行。「长度不变」不是免复核的理由：
        # 只要 mtime/size 与上次记录不同且 size == offset，就做一次 0..offset
        # 的行链复核；链一致只刷新指纹，不一致按替换全量重读。
        # 无 mtime 的旧游标（old_mtime=0）不在此列，留给一次性的基线构建。
        if (action == "append" and not jitter and size == offset and old_mtime
                and (mtime != old_mtime or size != old_size)):
            t0 = time.perf_counter()
            prefix_chain = _prefix_line_chain(path, offset) if chain else None
            scan_ms += (time.perf_counter() - t0) * 1000
            if chain and prefix_chain == chain:
                _log(f"长度未变但 mtime/size 变了：0..{offset} 行链一致，只刷新指纹；"
                     f"一次性复核 {offset} 字节 {scan_ms:.1f}ms")
                action = "reanchor"
            else:
                _log("已读区域内容变了（长度不变就地改写/前插删行），从 0 重读")
                offset, last_hash, canon = 0, "", 0
                line_no, context, action = 0, [], "replaced"

    try:
        with open(path, "rb") as f:
            f.seek(offset)
            buf = f.read(max(0, size - offset))
    except OSError as e:
        _log(f"台账读不出来（{e}），本轮跳过")
        return [], cursor

    cut = buf.rfind(b"\n")
    if cut < 0:
        if action == "append" and offset == start_offset:
            if not chain and offset > 0 and samples:
                # 旧版游标第一次遇到空闲轮：借这一轮把行链基线补上（一次性成本），
                # 之后每轮都走下面的快速返回。
                base = _prefix_line_chain(path, offset) or ""
                if base:
                    return [], _make_cursor(
                        path, offset, last_hash, canon, line_no, context,
                        fid, size, "idle", window=window, samples=samples,
                        head=head, chain=base, mtime=mtime,
                        sent_head=sent_head, sent_tail=sent_tail)
            if jitter:
                # 身份抖动且本轮没有新行：把新身份/新 mtime 落进游标，
                # 否则下一轮又要重做一次 0..offset 行链复核。
                return [], _make_cursor(
                    path, offset, last_hash, canon, line_no, context,
                    fid, size, "idle", window=window, samples=samples,
                    head=head, chain=chain, mtime=mtime,
                    sent_head=sent_head, sent_tail=sent_tail)
            # 逐字节没变（最多多了个残行）：原游标直接复用，不再多读一次窗口
            return [], cursor
        # 重锚/替换后的新游标要落盘，下次不必重算
        return [], _make_cursor(path, offset, last_hash, canon, line_no, context,
                                fid, size, action if action != "append" else "idle",
                                scan_ms=scan_ms,
                                chain="" if offset == 0 else chain,
                                mtime=mtime,
                                sent_head=sent_head, sent_tail=sent_tail)
    chunk = buf[:cut + 1]
    raw_lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
    new_offset = offset + cut + 1
    canon_new = canon + _canon_len(chunk)
    line_no_new = line_no + chunk.count(b"\n")
    new_hash = _line_sha256(raw_lines[-1]) if raw_lines else last_hash
    ctx = list(context)
    ctx.extend(_line_sha256(ln) for ln in raw_lines)
    # 行链：offset 归零（替换/首次）→ 从 0 重建；否则接在旧链后面继续折。
    if offset == 0:
        chain_new = _chain_lines("", raw_lines)
    elif chain:
        chain_new = _chain_lines(chain, raw_lines)
    else:
        chain_new = ""
    if not chain_new and new_offset > 0 and samples:
        # 旧版游标（有采样、没有行链）：第一次落盘时建一次基线。
        # 只在这一刻建：采样刚验过一致，前缀是「已知良好」的，不会把被改写的内容
        # 固化成基线；之后每轮只做增量折叠，不再整段重扫。
        chain_new = _prefix_line_chain(path, new_offset) or ""
    # chunk 覆盖到新游标；够长就直接取尾部 4KB 当新窗口指纹，省一次开文件
    win = (hashlib.sha256(chunk[-_WINDOW_BYTES:]).hexdigest()
           if len(chunk) >= _WINDOW_BYTES else None)
    final = (action if action in ("replaced", "rotation", "reanchor")
             else ("append" if raw_lines else "idle"))
    # 本轮交付的当前台账行都记进「已发送内容哈希」，下一轮轮转时归档若只是
    # 这些行的截短副本，就按哈希去重，不再重发（A2）。
    for ln in raw_lines:
        _sent_add(sent_head, sent_tail, sent, _sent_key(ln))
    return raw_lines, _make_cursor(path, new_offset, new_hash, canon_new,
                                   line_no_new, ctx, fid, size, final,
                                   window=win, scan_ms=scan_ms, chain=chain_new,
                                   mtime=mtime, sent_head=sent_head,
                                   sent_tail=sent_tail)


# ---------------------------------------------------------------- 事件

def _result_of(row):
    r = str(row.get("result") or "").strip().lower()
    return RESULT_MAP.get(r, "miss")


def _tier_of(row, name):
    for key in ("tier", "craft", "grade", "rarity"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:_TIER_MAX]
    m = _CRAFT_RE.search(name or "")
    return m.group(1) if m else "未标注"


def _price_band(price):
    if price is None:
        return "价格未知"
    if price < 500:
        return "¥0-500"
    if price < 1000:
        return "¥500-1000"
    if price < 2000:
        return "¥1000-2000"
    if price < 5000:
        return "¥2000-5000"
    return "¥5000以上"


def _skew_ms(row):
    """卡点误差毫秒：台账里有这些键就用，没有就 None（不编造）。

    当前 `core/records.py` 还不写这项（要改引擎才能拿到 precision 的
    offset_s），所以真实台账这一列多半是空 —— 看板会显示「—」。
    字段名留了四种常见写法，引擎那一侧补键后这边不用改。
    """
    for key in ("skew_ms", "offset_ms", "delta_ms", "error_ms"):
        v = row.get(key)
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not (-5000 <= f <= 5000):
            continue
        return int(round(f))
    return None


def _event(row, desensitize=False):
    """白名单裁剪。返回 None 表示这行不能用（脏行/坏 ts）。"""
    if not isinstance(row, dict):
        return None
    try:
        ts = float(row.get("ts"))
    except (TypeError, ValueError):
        return None
    if not (0 < ts < 1e11):
        return None
    name = str(row.get("name") or "")[:_NAME_MAX]
    tier = _tier_of(row, name)
    try:
        price = None if row.get("price") in (None, "", 0) else float(row.get("price"))
    except (TypeError, ValueError):
        price = None
    if price is not None and not (0 <= price <= 1e7):
        price = None
    result = _result_of(row)

    reason = row.get("skip_reason") or row.get("reason")
    reason = str(reason)[:_SKIP_MAX] if isinstance(reason, str) else ""
    if result != "skip":
        reason = ""

    if desensitize:
        name = f"{tier}｜{_price_band(price)}"

    ident = (f"{row.get('ts')}|{row.get('name')}|{row.get('price')}|"
             f"{row.get('result')}|{row.get('elapsed')}|{row.get('shot')}|"
             f"{row.get('mode')}")
    eid = "e" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:24]

    ev = {"event_id": eid,
          "occurred_at": round(ts, 3),
          "tier": tier,
          "name": name,
          "price": price,
          "result": result,
          "skew_ms": _skew_ms(row),
          "skip_reason": reason}
    return {k: ev[k] for k in EVENT_KEYS}


# ---------------------------------------------------------------- 队列

def queue_append(events):
    if not events:
        return
    data = b"".join(json.dumps(e, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8") + b"\n"
                    for e in events)
    with open(_STATE["queue"], "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def queue_read(limit):
    out = []
    if not os.path.isfile(_STATE["queue"]):
        return out
    try:
        with open(_STATE["queue"], "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("event_id"):
                    out.append(obj)
                if len(out) >= limit:
                    break
    except OSError as e:
        _log(f"缓冲队列读不出来（{e}）")
    return out


def queue_rewrite(keep):
    """把「已确认送达」的那批从缓冲里去掉（原子替换，断电不留半个文件）。"""
    if not keep:
        _atomic_write(_STATE["queue"], b"")
        return
    data = b"".join(json.dumps(e, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8") + b"\n"
                    for e in keep)
    _atomic_write(_STATE["queue"], data)


def queue_size():
    try:
        with open(_STATE["queue"], "rb") as f:
            return sum(1 for ln in f if ln.strip())
    except OSError:
        return 0


def queue_reject(events):
    """服务端明确拒收（schema/许可）的批次：单独留档，不再重试。"""
    data = b"".join(json.dumps(e, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8") + b"\n"
                    for e in events)
    with open(_STATE["rejected"], "ab") as f:
        f.write(data)
        f.flush()


# ---------------------------------------------------------------- 发送

class Retryable(Exception):
    """网络/5xx/429 —— 留着下次再发。"""


class Rejected(Exception):
    """4xx（除 429）—— 重发也没用，落盘留档。"""


def build_batch(events, license_id, machine, version, key):
    body = json.dumps({"license_id": license_id, "machine": machine,
                       "client_version": version, "sent_at": round(time.time(), 3),
                       "events": events},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, body, hashlib.sha256).hexdigest()
    return body, sig


def send_batch(body, sig, license_id, timeout=10.0):
    req = urllib.request.Request(
        _STATE["server"] + "/api/telemetry/batch", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-Telemetry-Signature": sig,
                 "X-Telemetry-License": license_id,
                 "User-Agent": "SJZ-Telemetry/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(4096).decode("utf-8", "replace")
        except OSError:
            pass
        if e.code == 429 or e.code >= 500:
            raise Retryable(f"HTTP {e.code}：{detail[:200]}")
        raise Rejected(f"HTTP {e.code}：{detail[:200]}")
    except (urllib.error.URLError, OSError) as e:
        raise Retryable(f"连不上收集服务：{e}")
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return {}


def drain(license_id, machine, version, key, batch_size):
    """尽量把缓冲发空。返回 (发送批数, 失败原因或 None)。

    整份队列先读进内存：每批成功后就原子重写剩下的，失败就原样留着。
    4xx（schema/许可）不算「暂时失败」——重发只会刷限流，转存 rejected 留档。
    """
    pending = queue_read(10 ** 9)
    sent, i, err = 0, 0, None
    while i < len(pending):
        batch = pending[i:i + batch_size]
        body, sig = build_batch(batch, license_id, machine, version, key)
        try:
            send_batch(body, sig, license_id)
        except Retryable as e:
            err = str(e)
            break
        except Rejected as e:
            _log(f"服务端拒收这一批（{e}），已转存 {REJECTED_NAME}")
            queue_reject(batch)
        i += len(batch)
        sent += 1
    if i:
        queue_rewrite(pending[i:])
    return sent, err


# ---------------------------------------------------------------- 主循环

def harvest(license_id, machine, version, key, batch_size, desensitize, cursor):
    """读增量 → 入缓冲。返回新游标。"""
    path = _STATE["records"]
    if not os.path.isfile(path):
        _log(f"台账不存在：{path}（等引擎写出第一条后自动开始）")
        return cursor
    raw_lines, new_cursor = read_new_lines(path, cursor)
    if not raw_lines:
        # 即使没有新行，也可能发生了重锚/替换/轮转：游标要落盘，否则每轮重算
        if new_cursor is not cursor:
            save_cursor(path, new_cursor)
        return new_cursor
    events, bad = [], 0
    for raw in raw_lines:
        try:
            row = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            bad += 1
            continue
        ev = _event(row, desensitize)
        if ev is None:
            bad += 1
            continue
        events.append(ev)
    if events:
        queue_append(events)
    if bad:
        _log(f"跳过 {bad} 行坏数据（残行/字段不全）")
    # 先落缓冲再推进游标：崩在中间只会重复读，不会丢
    save_cursor(path, new_cursor)
    if events:
        _log(f"新增 {len(events)} 条待上报（缓冲共 {queue_size()} 条）")
    return new_cursor


def run(args):
    _setup_paths(args)
    payload, key = load_license(args.license)
    license_id, machine = ids_from_payload(payload)
    cfg = _project_config()
    version = (args.client_version or cfg.get("client_version")
               or DEFAULT_VERSION)
    desensitize = bool(args.desensitize or cfg.get("desensitize_names"))
    batch_size = max(1, min(int(args.batch_size or cfg.get("batch_size")
                             or DEFAULT_BATCH), 100))
    interval = max(1, int(args.interval or cfg.get("interval_s")
                          or DEFAULT_INTERVAL))

    _log(f"许可 {license_id} / 机器 {machine} / 服务 {_STATE['server']}")
    _log(f"台账 {_STATE['records']} / 状态目录 {_STATE['dir']}")
    _log(f"间隔 {interval}s / 单批 {batch_size} / 脱敏 {'开' if desensitize else '关'}")

    cursor = load_cursor()
    fail_streak = 0
    while True:
        cursor = harvest(license_id, machine, version, key, batch_size,
                         desensitize, cursor)
        sent, err = drain(license_id, machine, version, key, batch_size)
        if err:
            fail_streak += 1
            wait = min(1800, 15 * (2 ** min(fail_streak - 1, 7)))
            _log(f"上报失败（第 {fail_streak} 次）：{err}，{wait}s 后重试；"
                 f"缓冲 {queue_size()} 条不会丢")
        else:
            if fail_streak:
                _log("上报已恢复")
            fail_streak = 0
            wait = interval
            if sent:
                _log(f"已上报 {sent} 批")
        if args.once:
            break
        time.sleep(wait if fail_streak else interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description="抢购台账遥测上报器")
    ap.add_argument("--interval", type=int, default=None,
                    help="轮询间隔秒，默认 30（配置里 telemetry.interval_s 可覆盖）")
    ap.add_argument("--once", action="store_true", help="只跑一轮（排查/测试）")
    ap.add_argument("--server", default=None, help="收集服务地址，默认 http://127.0.0.1:8091")
    ap.add_argument("--records", default=None, help="台账 jsonl 路径")
    ap.add_argument("--license", default=None, help="license.json 路径")
    ap.add_argument("--client-version", default=None)
    ap.add_argument("--batch-size", type=int, default=None)
    ap.add_argument("--desensitize", action="store_true",
                    help="商品名脱敏成「品阶+价格区间」")
    ap.add_argument("--state-dir", default=None,
                    help="游标/缓冲目录，默认 data/telemetry")
    args = ap.parse_args(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
