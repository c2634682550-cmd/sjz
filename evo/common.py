"""共享工具：路径、原子写、统计量、时间格式化。只依赖标准库。"""
import hashlib
import json
import math
import os
import tempfile
import time

EVO_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(EVO_DIR)


def path(*parts):
    return os.path.join(EVO_DIR, *parts)


def abs_path(p):
    """相对路径按仓库根解析；绝对路径原样返回。"""
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p))


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def load_json(p, default=None):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write_json(p, obj):
    """先写临时文件再替换，崩在写一半也不会污染上一版。"""
    d = os.path.dirname(p)
    ensure_dir(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False,
                      allow_nan=False)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def append_jsonl(p, rows):
    ensure_dir(os.path.dirname(p))
    with open(p, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n")


def read_jsonl(p, limit=0):
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def sig16(*parts):
    raw = "|".join(str(x) for x in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def day_str(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def stamp(ts=None):
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(time.time() if ts is None else ts))


def fmt_ts(ts=None):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() if ts is None else ts))


def wilson_lower(k, n, z=1.96):
    """二项比例的 Wilson 下界；样本量为 0 时返回 0。"""
    if n <= 0:
        return 0.0
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2.0 * n)
    r = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z * z / (4.0 * n * n)))
    return max(0.0, (c - r) / d)


def percentile(vals, q):
    """线性插值分位；空列表返回 None。"""
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * q
    f = int(math.floor(k))
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def mean(vals):
    return (sum(vals) / len(vals)) if vals else None


def safe_ratio(a, b):
    return (a / b) if b else 0.0


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_name(s):
    return str(s or "").strip()
