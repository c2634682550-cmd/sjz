"""沉淀层：evo/registry.jsonl 实验登记册。

字段：schema / 时间 / 轮次 / 假设 / 参数 / 样本量 / 指标前后值 / 改动清单
(changes) / 副作用 (side_effects) / 结论 / 失败原因 / 回滚方式 (rollback) /
实验级别 / 签名。schema/2 起带代码轮次三字段，旧条目按空值读。
追加写 + 文件锁；签名索引用于「同类失败不重试」。
"""
import json
import os
import time

from .common import append_jsonl, ensure_dir, read_jsonl
from .config import registry_path, state_dir


def _lock_path(cfg):
    return os.path.join(state_dir(cfg), "registry.lock")


class _Lock:
    def __init__(self, p, timeout=8.0):
        self.p = p
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        ensure_dir(os.path.dirname(self.p))
        end = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.p) > 30:
                        os.remove(self.p)
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise TimeoutError("registry lock timeout")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
        finally:
            try:
                os.remove(self.p)
            except OSError:
                pass


def append_entries(cfg, entries):
    if not entries:
        return 0
    p = registry_path(cfg)
    with _Lock(_lock_path(cfg)):
        append_jsonl(p, entries)
    return len(entries)


def load(cfg):
    return read_jsonl(registry_path(cfg))


def index(cfg=None, entries=None):
    """签名 -> {attempts, failures, last_ts, last_conclusion, last_sample_n}。"""
    rows = entries if entries is not None else load(cfg)
    idx = {}
    for r in rows:
        sig = r.get("signature")
        if not sig:
            continue
        slot = idx.setdefault(sig, {"attempts": 0, "failures": 0,
                                    "successes": 0, "last_ts": 0,
                                    "last_conclusion": "",
                                    "last_sample_n": 0})
        slot["attempts"] += 1
        concl = r.get("conclusion") or ""
        slot["last_conclusion"] = concl
        slot["last_ts"] = max(slot["last_ts"], float(r.get("ts") or 0))
        obs = (r.get("sample_size") or {}).get("observed")
        if isinstance(obs, dict):
            slot["last_sample_n"] = max(slot["last_sample_n"], int(obs.get("n") or 0))
        if concl in ("rejected", "rolled_back"):
            slot["failures"] += 1
        elif concl in ("accepted", "canary"):
            slot["successes"] += 1
    return idx


def blocked(cfg, idx, sig, current_sample=None):
    slot = idx.get(sig)
    if not slot:
        return False
    if slot["last_conclusion"] in ("accepted", "canary"):
        return True
    cooldown = float(cfg["hypothesis"]["failure_cooldown_days"]) * 86400
    if slot["failures"] > 0 and time.time() - slot["last_ts"] < cooldown:
        return True
    # 结论不确定且证据量没有明显增长：不重复消耗轮次
    if slot["last_conclusion"] == "inconclusive" and current_sample is not None:
        prev = int(slot.get("last_sample_n") or 0)
        if prev and current_sample < prev * 1.5:
            return True
    return False


def make_entry(hyp, run_id, round_no, level, sample, before, after,
               conclusion, failure_reason="", changes=None, side_effects=None,
               rollback=None):
    """写一条登记。changes/side_effects/rollback 为代码轮次字段（schema/2）。

    changes: [{"file": "core/market.py", "lines": "412-430", "summary": "..."}]
    side_effects: [{"metric": "item_total_ms", "delta": "+1.8%", "note": "..."}]
    rollback: {"method": "patch-reverse|git-restore|param-overlay",
               "ref": "perf/out/.../r3.patch", "verify": "同台架复测回到基线"}
    """
    return {
        "schema": "evo-registry/2",
        "ts": round(time.time(), 3),
        "run_id": run_id,
        "round": round_no,
        "experiment_id": f"EXP-{hyp['signature']}-{run_id[-8:]}",
        "hypothesis": {
            "id": hyp.get("id"), "class": hyp.get("class"),
            "title": hyp.get("title"), "factor": hyp.get("factor"),
            "metric": hyp.get("metric"),
            "expected_direction": hyp.get("expected_direction"),
            "expected_gain": hyp.get("expected_gain"),
        },
        "params": hyp.get("param"),
        "sample_size": sample,
        "metric_before": before,
        "metric_after": after,
        "conclusion": conclusion,
        "failure_reason": failure_reason,
        "changes": list(changes or []),
        "side_effects": list(side_effects or []),
        "rollback": rollback or {},
        "level": level,
        "signature": hyp["signature"],
    }


def summary(cfg=None, entries=None):
    rows = entries if entries is not None else load(cfg)
    out = {"total": len(rows), "by_conclusion": {}, "by_level": {},
           "signatures": 0}
    sigs = set()
    for r in rows:
        c = r.get("conclusion") or "unknown"
        out["by_conclusion"][c] = out["by_conclusion"].get(c, 0) + 1
        l = r.get("level") or "unknown"
        out["by_level"][l] = out["by_level"].get(l, 0) + 1
        if r.get("signature"):
            sigs.add(r["signature"])
    out["signatures"] = len(sigs)
    return out
