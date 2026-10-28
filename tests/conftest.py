"""Shared fixtures: a throwaway EVO workspace with synthetic run folders.

Synthetic curves are used ONLY in unit tests (fast, exact). The demo and the
evaluation harness use real training runs from projects/*/fixtures.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path

# Tests must never read the user's config.yaml / .env choices (a real provider would call the hosted API,
# burn the rate limit and make the suite slow). Point at a missing config so the offline defaults apply.
os.environ["EVO_CONFIG"] = str(Path(__file__).parent / "_no_such_config.yaml")
os.environ.pop("EVO_LLM_PROVIDER", None)
os.environ.pop("EVO_LLM_MODEL", None)
os.environ.pop("EVO_LLM_BASE_URL", None)
os.environ.pop("EVO_RUNNER_MODE", None)
os.environ.pop("EVO_MAX_STEPS", None)

import pytest
import yaml

from evo.memory.store import Store
from evo.policy import build_config
from evo.settings import ROOT, load_settings

PROJECT = {
    "project_id": "toy", "name": "Toy PPO", "owner": "test", "goal": "test project", "env_id": "LunarLander-v3",
    "algo": "PPO", "total_timesteps": 100000, "n_envs": 1, "device": "cpu", "eval": {"freq": 10000, "episodes": 3},
    "primary_metric": {"name": "mean_eval_return_last_k", "k": 3}, "target_score": 100, "min_seeds": 3,
    "run_budget": 9, "deadline": "2026-11-10", "env_allowlist": ["LunarLander-v3"],
    "baseline_hyperparams": {"learning_rate": 0.0003, "lr_schedule": "constant", "ent_coef": 0.01},
    "search_space": {"learning_rate": {"min": 0.0001, "max": 0.003}, "lr_schedule": {"choices": ["constant", "linear"]},
                     "ent_coef": {"min": 0.0, "max": 0.05}},
}


def write_run(folder: Path, cfg: dict, evals: list[float], *, done: bool = True, log_extra: str = "",
              nan_metrics: bool = False) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with open(folder / "eval.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "mean", "std", "n"])
        for i, v in enumerate(evals, start=1):
            w.writerow([i * 10000, v, 5.0, 3])
    with open(folder / "metrics.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "ep_rew_mean", "loss", "approx_kl"])
        w.writerow([10000, evals[0] if evals else 0, "nan" if nan_metrics else 1.0, 0.01])
    (folder / "train.log").write_text("INFO start\nINFO eval ok\n" + log_extra, encoding="utf-8")
    if done:
        (folder / "DONE").write_text(json.dumps({"wall_time_s": 1.0, "device": "cpu"}), encoding="utf-8")
    return folder


@pytest.fixture()
def settings():
    return load_settings(refresh=True)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("EVO_HOME", str(tmp_path / "data"))
    proj_dir = tmp_path / "projects" / "toy"
    proj_dir.mkdir(parents=True)
    (proj_dir / "project.yaml").write_text(yaml.safe_dump(PROJECT), encoding="utf-8")
    store = Store(tmp_path / "data" / "evo.db")
    yield {"root": tmp_path, "proj_dir": proj_dir, "store": store}
    store.close()


def cfg_for(change: dict, seed: int) -> dict:
    return build_config(PROJECT, change, seed)


RISING = [-100, -50, 0, 40, 60, 80]          # healthy
COLLAPSE = [-100, 0, 90, 120, 110, 0, -30, -50, -60, -70]  # peaks then loses everything
