"""回滚层：晋级 -> 灰度 -> 指标劣化超阈值自动回到上一版参数。

只维护框架自有参数覆盖层（state/params_overlay.json），不写 core/config.json。
"""
import time

from .common import load_json, safe_ratio
from .config import state_dir
import os


def _p(cfg):
    return os.path.join(state_dir(cfg), "params_overlay.json")


def load(cfg):
    return load_json(_p(cfg), {"version": 1, "current": {}, "baseline": {},
                               "canary": None, "history": []})


def promote(cfg, hyp, funnel):
    """实验通过后进入灰度：记录参数、基线指标与观察轮数。"""
    ov = load(cfg)
    name = hyp["param"]["name"]
    prev = ov["current"].get(name)
    m = funnel["window"]["metrics"]
    value = m.get(hyp["metric"])
    if value is None:
        value = m.get("end_to_end_rate")
    ov["current"][name] = {
        "value": hyp["param"]["to"],
        "prev": prev["value"] if prev else hyp["param"]["from"],
        "promoted_at": round(time.time(), 3),
        "signature": hyp["signature"],
    }
    ov["baseline"] = {
        "metric": hyp["metric"], "value": value,
        "direction": hyp["expected_direction"]["direction"],
        "n": funnel["window"]["counts"].get("discover", 0),
        "ts": round(time.time(), 3),
    }
    ov["canary"] = {"signature": hyp["signature"], "param": name,
                    "cycles_left": int(cfg["rollback"]["canary_cycles"]),
                    "started_at": round(time.time(), 3)}
    ov["history"].append({"event": "promote", "ts": round(time.time(), 3),
                          "param": name, "to": hyp["param"]["to"],
                          "signature": hyp["signature"]})
    _save(cfg, ov)
    return ov


def observe(cfg, funnel):
    """灰度观察：轮数递减；劣化超阈值则回滚。返回回滚记录或 None。"""
    ov = load(cfg)
    canary = ov.get("canary")
    if not canary:
        return None
    canary["cycles_left"] = int(canary["cycles_left"]) - 1
    if canary["cycles_left"] > 0:
        _save(cfg, ov)
        return None

    base = ov.get("baseline") or {}
    metric = base.get("metric")
    cur = (funnel["window"]["metrics"] or {}).get(metric)
    n = int(funnel["window"]["counts"].get("discover", 0) or 0)
    threshold = float(cfg["rollback"]["degrade_rel_threshold"])
    min_obs = int(cfg["rollback"]["min_obs"])
    degrade = None
    if cur is not None and base.get("value") and n >= min_obs:
        direction = base.get("direction", "up")
        if direction == "up":
            degrade = 1.0 - safe_ratio(cur, base["value"])
        else:
            degrade = safe_ratio(cur, base["value"]) - 1.0
    if degrade is not None and degrade > threshold:
        name = canary["param"]
        slot = ov["current"].get(name) or {}
        if slot.get("prev") is not None:
            ov["current"][name] = {"value": slot["prev"],
                                   "prev": None,
                                   "promoted_at": round(time.time(), 3),
                                   "signature": slot.get("signature")}
        rec = {"event": "rollback", "ts": round(time.time(), 3),
               "param": name, "metric": metric,
               "baseline": base.get("value"), "current": cur,
               "degrade_rel": round(degrade, 4),
               "threshold": threshold, "n": n,
               "signature": canary["signature"]}
        ov["history"].append(rec)
        ov["canary"] = None
        _save(cfg, ov)
        return rec

    # 灰度通过：保留当前参数，清空灰度标记
    ov["history"].append({"event": "canary_ok", "ts": round(time.time(), 3),
                          "param": canary["param"],
                          "signature": canary["signature"]})
    ov["canary"] = None
    _save(cfg, ov)
    return None


def _save(cfg, ov):
    from .common import atomic_write_json
    atomic_write_json(_p(cfg), ov)
