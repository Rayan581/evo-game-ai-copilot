"""Runtime settings.

Defaults live here so every threshold is documented in one place. A
`config.yaml` in the repo root (copy `config.example.yaml`) overrides them.
Secrets (API keys) are only ever read from the environment / `.env`.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "paths": {
        # Where evo.db lives. Override with EVO_HOME for isolated test runs.
        "data_dir": "data",
        "knowledge_dir": "knowledge",
    },
    "analysis": {
        # A run "collapsed" if, at the end of training, it has lost at least this
        # fraction of the progress it made from its first eval to its peak ...
        "collapse_drop_frac": 0.5,
        # ... and the last N evals are all below that line.
        "collapse_min_evals": 3,
        # Minimum progress (in return units) before collapse is even considered.
        "collapse_min_gain": 20.0,
        # Curves are smoothed with a rolling mean over this many evals before looking for a
        # collapse, so a single noisy eval point cannot count as a peak.
        "smooth_window": 3,
        # Plateau: slope over the last fraction of evals is flat while the
        # target is unmet. Slope is measured per 10k steps relative to the
        # distance between the first eval and the target.
        "plateau_window_frac": 0.25,
        "plateau_rel_slope": 0.01,
        # PPO approx-KL above this is reported as a divergence warning.
        "kl_ceiling": 0.2,
        "bootstrap_samples": 2000,
        "ci_level": 0.95,
        "bootstrap_seed": 1234,
    },
    "agent": {
        "max_steps_per_cycle": 8,
        "similar_configs_in_brief": 5,
        "decisions_in_brief": 6,
        "max_llm_retries": 2,
    },
    "llm": {
        # openai_compat: any OpenAI-compatible chat endpoint (Groq, Ollama, ...)
        # offline: deterministic rule-based planner (tests, replays, no key)
        "provider": "offline",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_key_env": "EVO_LLM_API_KEY",
        "temperature": 0.0,
        "timeout_s": 60,
        "use_cache": True,
    },
    "runner": {
        "timeout_s": 1800,
        "max_parallel": 3,
        # live: train for real. fixture: copy a matching run from the project's
        # fixture bank when one exists (used by the eval harness); falls back to live.
        "mode": "live",
    },
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_CACHE: dict[str, Any] | None = None


def load_settings(refresh: bool = False) -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env", override=False)
    cfg = copy.deepcopy(DEFAULTS)
    cfg_path = Path(os.environ.get("EVO_CONFIG", ROOT / "config.yaml"))
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
    # environment overrides (handy for the eval harness and demos)
    if os.environ.get("EVO_LLM_PROVIDER"):
        cfg["llm"]["provider"] = os.environ["EVO_LLM_PROVIDER"]
    if os.environ.get("EVO_LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["EVO_LLM_MODEL"]
    if os.environ.get("EVO_LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["EVO_LLM_BASE_URL"]
    if os.environ.get("EVO_RUNNER_MODE"):
        cfg["runner"]["mode"] = os.environ["EVO_RUNNER_MODE"]
    if os.environ.get("EVO_MAX_STEPS"):
        cfg["agent"]["max_steps_per_cycle"] = int(os.environ["EVO_MAX_STEPS"])
    _CACHE = cfg
    return cfg


def data_dir() -> Path:
    home = os.environ.get("EVO_HOME")
    if home:
        p = Path(home)
    else:
        p = ROOT / load_settings()["paths"]["data_dir"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "evo.db"


def knowledge_dir() -> Path:
    return ROOT / load_settings()["paths"]["knowledge_dir"]
