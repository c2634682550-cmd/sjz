"""perf/bench 公共口径：样本统计、离群剔除、快照装载、环境指纹、结果输出。

所有基准都必须经由 Summary 出数，口径固定：
  1) 前 `warmup` 个样本单独作为「冷态」列出，不进热态统计；
  2) 热态样本用 MAD 剔除异常值，阈值 = max(6*1.4826*MAD, 20%*中位数, 0.05ms)；
  3) 只报 p50 / p95 / max / mean / n，不只报平均；
  4) 每个结果都带 meta：命令、脚本、快照路径、git rev、配置摘要、种子。
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time

MAD_K = 6.0
MAD_SCALE = 1.4826
FLOOR_MS = 0.05

CORE_MODULES = (
    "config", "vision", "ocr", "ocr_rapid", "ocr_ppocr", "ocr_chain",
    "digit_ocr", "market", "cards", "money", "bricks", "click_burst",
    "screen", "input", "interception",
)


def now() -> float:
    return time.perf_counter()


def percentile(values, q):
    """线性插值分位（与 numpy 默认一致），空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(n - 1, lo + 1)
    frac = pos - lo
    return float(s[lo] * (1 - frac) + s[hi] * frac)


def mad(values):
    if not values:
        return 0.0
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


class Summary:
    """一组原始样本 → 结构化统计。单位默认毫秒。"""

    def __init__(self, unit: str = "ms", warmup: int = 5):
        self.unit = unit
        self.warmup = int(warmup)
        self.raw: list[float] = []

    def add(self, value):
        if value is None:
            return
        self.raw.append(float(value))

    def extend(self, values):
        for v in values:
            self.add(v)

    def summary(self):
        n_raw = len(self.raw)
        if n_raw == 0:
            return {"n": 0, "n_raw": 0, "warmup": 0, "cold_p50": None,
                    "p50": None, "p95": None, "p99": None, "max": None, "mean": None,
                    "min": None, "dropped": 0, "unit": self.unit}
        cold = self.raw[:self.warmup]
        warm = self.raw[self.warmup:] or list(self.raw)
        med = statistics.median(warm)
        thr = max(MAD_K * MAD_SCALE * mad(warm), 0.2 * abs(med), FLOOR_MS)
        kept = [v for v in warm if abs(v - med) <= thr]
        if not kept:
            kept = list(warm)
        return {
            "n": len(kept),
            "n_raw": n_raw,
            "warmup": min(self.warmup, n_raw),
            "cold_p50": percentile(cold, 0.5),
            "p50": percentile(kept, 0.5),
            "p95": percentile(kept, 0.95),
            "p99": percentile(kept, 0.99),
            "max": max(kept),
            "mean": statistics.fmean(kept),
            "min": min(kept),
            "dropped": len(warm) - len(kept),
            "unit": self.unit,
            "drop_rule": f"|x-median|>{thr:.4g}",
        }


def deep_merge(dst, src):
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def sha256_json(obj):
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


SUITE_FILES = ("common.py", "ocr_group.py", "click_group.py", "flow_group.py",
               "run.py", "gate.py", "redlines.py", "startup.py", "flow_gate.py")


def bench_digest(bench_dir):
    """套件自身指纹：换了统计口径/基准脚本，也算换了判据。

    只认本套件文件。perf/bench/ 下还有别的分支的脚本，把它们算进来会让
    指纹随别人改文件乱跳，基线就失去「同口径」的锚。
    """
    h = hashlib.sha256()
    for name in SUITE_FILES:
        path = os.path.join(bench_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            h.update(name.encode())
            h.update(f.read())
    return h.hexdigest()[:16]


def env_packages():
    """参与耗时的包版本。环境被别人 pip 动过时，这一栏会第一时间露出来。"""
    import importlib.metadata as md
    out = {}
    for name in ("rapidocr", "onnxruntime", "opencv-python", "numpy", "mss",
                 "paddleocr", "paddlepaddle"):
        try:
            out[name] = md.version(name)
        except Exception:
            out[name] = None
    return out


def load_snapshot(snapshot):
    """把快照目录放到 sys.path 最前，并校验 core 确实来自该目录。"""
    root = os.path.abspath(snapshot)
    if not os.path.isdir(os.path.join(root, "core")):
        raise SystemExit(f"快照目录里没有 core/：{root}")
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def import_core(root):
    mods = {}
    for name in CORE_MODULES:
        mods[name] = importlib.import_module("core." + name)
    core_file = os.path.abspath(mods["config"].__file__)
    if os.path.dirname(os.path.dirname(core_file)) != root:
        raise SystemExit(f"core 不是从快照加载的：{core_file} != {root}")
    return mods


def pin_config(config_mod, ocr_mod, overrides=None):
    """把基准固定在 DEFAULTS + bench 覆盖上，忽略工作目录里的 config.json。

    不同机器上用户改过的 config.json 会让数字不可比；基准只认钉死的配置。
    """
    data = copy.deepcopy(config_mod.DEFAULTS)
    deep_merge(data, overrides or {})
    fd, path = tempfile.mkstemp(prefix="dafors_bench_cfg_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    config_mod.CONFIG_PATH = path
    ocr_mod.reload()
    return path, sha256_json(data)


def cpu_load(sample_ms=300):
    """整机 CPU 占用（含别的进程），用来标记这一轮基准是否被抢占。"""
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def val(ft):
        return (ft.hi << 32) | ft.lo

    k = ctypes.windll.kernel32
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    try:
        k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        i0, k0, u0 = val(idle), val(kern), val(user)
        time.sleep(max(0.05, sample_ms / 1000.0))
        k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        i1, k1, u1 = val(idle), val(kern), val(user)
        di, dk, du = i1 - i0, k1 - k0, u1 - u0
        total = dk + du
        if total <= 0:
            return None
        return round(100.0 * (total - di) / total, 1)
    except Exception:
        return None


def process_cpu_ms():
    """当前进程累计 CPU（用户+内核）毫秒，用来算「每商品 CPU 成本」。"""
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def val(ft):
        return (ft.hi << 32) | ft.lo

    k = ctypes.windll.kernel32
    c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k.GetProcessTimes(k.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e),
                             ctypes.byref(kt), ctypes.byref(ut)):
        return None
    return (val(kt) + val(ut)) / 10000.0


def core_digest(root):
    """core/ 全部 .py 的内容指纹：把基线和某一版代码绑死。"""
    h = hashlib.sha256()
    core_dir = os.path.join(root, "core")
    names = sorted(n for n in os.listdir(core_dir) if n.endswith(".py"))
    for name in names:
        path = os.path.join(core_dir, name)
        h.update(name.encode())
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _git_dirty(root):
    try:
        out = subprocess.run(["git", "-C", root, "status", "--porcelain", "--", "core"],
                             capture_output=True, text=True, timeout=5).stdout
        return len([x for x in out.splitlines() if x.strip()])
    except Exception:
        return None


def env_fingerprint(root, cfg_digest, bench_dir):
    import ctypes
    try:
        u = ctypes.windll.user32
        screen = [int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))]
        try:
            dpi = int(u.GetDpiForSystem())
        except Exception:
            dpi = None
    except Exception:
        screen, dpi = None, None
    try:
        git_head = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        git_head = None
    return {
        "snapshot": root,
        "git_head": git_head,
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "packages": env_packages(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "screen": screen,
        "dpi": dpi,
        "config_sha256": cfg_digest,
        "bench_sha256": bench_digest(bench_dir),
        "core_digest": core_digest(root),
        "git_dirty": _git_dirty(root),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def materials(root):
    fx = os.path.join(root, "tests", "fixtures")
    return {
        "price_roi": os.path.join(fx, "price_roi"),
        "grade_roi": os.path.join(fx, "grade_roi"),
        "market": os.path.join(fx, "market"),
        "templates": os.path.join(root, "templates"),
    }


def load_manifest(folder):
    path = os.path.join(folder, "manifest.json")
    if not os.path.exists(path):
        return []
    return load_json(path)


def flatten(result):
    """把结果压平成 {group.metric: summary}，给守门脚本用。"""
    out = {}
    for gname, group in (result.get("groups") or {}).items():
        for metric, value in (group or {}).items():
            if isinstance(value, dict) and "p50" in value:
                out[f"{gname}.{metric}"] = value
    for key, value in (result.get("accuracy") or {}).items():
        out[f"accuracy.{key}"] = value
    return out


def write_outputs(result, outdir, stem="perf"):
    os.makedirs(outdir, exist_ok=True)
    json_path = os.path.join(outdir, stem + ".json")
    md_path = os.path.join(outdir, stem + ".md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    lines = [f"# 基准结果 `{stem}`", ""]
    meta = result.get("meta") or {}
    lines += [
        f"- 快照：`{meta.get('snapshot')}`",
        f"- git：`{meta.get('git_head')}`　套件指纹：`{meta.get('bench_sha256')}`",
        f"- 配置指纹：`{meta.get('config_sha256')}`",
        f"- 机器：{meta.get('platform')} / {meta.get('cpu_count')} 核 / 屏 {meta.get('screen')} / DPI {meta.get('dpi')}",
        f"- 命令：`{meta.get('command')}`",
        f"- 种子：{meta.get('seed')}　时间：{meta.get('generated_at')}",
        "",
        "| 组 | 指标 | n | 冷态p50 | p50 | p95 | max | mean | 剔除 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for gname, group in (result.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        for metric, value in group.items():
            if not isinstance(value, dict) or "p50" not in value:
                continue
            def fmt(v):
                return "-" if v is None else f"{v:.2f}"
            lines.append(
                f"| {gname} | {metric} | {value.get('n')} | {fmt(value.get('cold_p50'))} "
                f"| {fmt(value.get('p50'))} | {fmt(value.get('p95'))} | {fmt(value.get('max'))} "
                f"| {fmt(value.get('mean'))} | {value.get('dropped')} |"
            )
    acc = result.get("accuracy") or {}
    if acc:
        lines += ["", "## 准确率", "", "| 指标 | 值 | n |", "| --- | --- | --- |"]
        for k, v in acc.items():
            if isinstance(v, dict):
                lines.append(f"| {k} | {v.get('value')} | {v.get('n')} |")
            else:
                lines.append(f"| {k} | {v} | - |")
    notes = result.get("notes") or {}
    if notes:
        lines += ["", "## 说明", ""]
        for k, v in notes.items():
            lines.append(f"- **{k}**：{v}")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return json_path, md_path
