"""配置加载：默认值 + evo_config.json + 环境变量（用于自检夹具）。"""
import copy
import os

from .common import load_json, path

DEFAULTS = {
    "observation": {
        "ledger_path": "data/records.jsonl",
        "telemetry_db": "data/telemetry/telemetry.db",
        "max_name_bytes": 128,
        "max_batch_rows": 5000,
        "max_batch_events": 5000,
    },
    "funnel": {"window_days": 7, "tier_keywords": [],
               "min_stage_n": 10},
    "attribution": {"top_k": 6, "min_evidence_n": 10,
                    "skew_p95_baseline_ms": 120.0,
                    "queue_p95_baseline_s": 3.0, "skew_abs_limit_ms": 5000},
    "hypothesis": {"max_per_round": 5, "min_sample_floor": 30,
                   "failure_cooldown_days": 7, "offline_min_rows": 30,
                   "shadow_min_coverage": 0.9, "shadow_max_p95_ms": 350.0,
                   "shadow_max_sessions": 60},
    "guardrails": {"budget_cap": 0, "quota_cap": 1, "skew_p95_max_ms": 120.0,
                   "allow_foreground": False, "allow_real_purchase": False,
                   "allow_input_downgrade": False},
    "decision": {"batch_size": 2, "exploration": 1.0},
    "rollback": {"degrade_rel_threshold": 0.05, "min_obs": 10,
                 "canary_cycles": 2},
    "daemon": {"cycle_interval_s": 86400, "max_rounds_per_cycle": 1,
               "lock_timeout_s": 5.0, "replay_buffer_max": 2000,
               "sample_cap": 2000},
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(overrides=None):
    cfg = _merge(DEFAULTS, load_json(path("evo_config.json"), {}))
    if overrides:
        cfg = _merge(cfg, overrides)
    env_map = {
        "ledger_path": "EVO_LEDGER_PATH",
        "telemetry_db": "EVO_TELEMETRY_DB",
    }
    for key, env in env_map.items():
        val = os.environ.get(env)
        if val:
            cfg["observation"][key] = val
    if os.environ.get("EVO_STATE_DIR"):
        cfg["_state_dir"] = os.environ["EVO_STATE_DIR"]
    if os.environ.get("EVO_RUNS_DIR"):
        cfg["_runs_dir"] = os.environ["EVO_RUNS_DIR"]
    if os.environ.get("EVO_REGISTRY"):
        cfg["_registry"] = os.environ["EVO_REGISTRY"]
    return cfg


def state_dir(cfg):
    return cfg.get("_state_dir") or path("state")


def runs_dir(cfg):
    return cfg.get("_runs_dir") or path("runs")


def registry_path(cfg):
    return cfg.get("_registry") or path("registry.jsonl")
