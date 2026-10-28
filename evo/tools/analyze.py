"""Tool: analyze_runs - deterministic curve diagnostics and comparisons.

All numbers the agent reasons about come from here, never from the LLM.
Thresholds come from settings (documented in evo/settings.py).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from evo.memory.store import ProjectScope, config_hash

# --------------------------------------------------------------------------------
# single run


def summarize_curve(points: list[dict], *, k: int, target: float | None, a: dict) -> dict[str, Any]:
    vals = [(p["step"], p["mean"]) for p in points if p.get("mean") is not None]
    if not vals:
        return {"score": None, "n_evals": 0, "flags": ["no_eval"]}
    steps = np.array([s for s, _ in vals], dtype=float)
    ys = np.array([v for _, v in vals], dtype=float)
    flags: list[str] = []
    if len(ys) < k:
        flags.append("short_curve")
    score = float(ys[-k:].mean())
    first, final = float(ys[0]), float(ys[-1])
    i_peak = int(ys.argmax())
    peak = float(ys[i_peak])
    out: dict[str, Any] = {
        "score": round(score, 2), "first": round(first, 2), "final": round(final, 2), "peak": round(peak, 2),
        "peak_step": int(steps[i_peak]), "auc": round(float(ys.mean()), 2), "n_evals": int(len(ys)),
        "last_step": int(steps[-1]),
    }
    # collapse: on the smoothed curve (rolling mean, so one lucky eval is not a "peak"), the run
    # lost >= frac of the progress it made and stayed below that line for the last N evals
    w = int(a.get("smooth_window", 3))
    sm = np.convolve(ys, np.ones(w) / w, mode="valid") if len(ys) >= w else ys
    s_steps = steps[w - 1:] if len(ys) >= w else steps
    j_peak = int(sm.argmax())
    s_first, s_peak, s_final = float(sm[0]), float(sm[j_peak]), float(sm[-1])
    gain = s_peak - s_first
    line = s_peak - a["collapse_drop_frac"] * gain
    n = a["collapse_min_evals"]
    if gain >= a["collapse_min_gain"] and len(sm) >= n + 1 and j_peak < len(sm) - n and bool((sm[-n:] < line).all()):
        flags.append("collapse")
        out["collapse"] = {"smoothed_peak": round(s_peak, 2), "peak_step": int(s_steps[j_peak]),
                           "smoothed_final": round(s_final, 2), "lost_frac": round((s_peak - s_final) / gain, 2)}
    # plateau: flat tail while target unmet
    if target is not None and len(ys) >= 4:
        w = max(3, int(math.ceil(len(ys) * a["plateau_window_frac"])))
        xs_t, ys_t = steps[-w:], ys[-w:]
        slope = float(np.polyfit(xs_t, ys_t, 1)[0]) * 10_000  # return per 10k steps
        scale = max(abs(target - first), 1.0)
        rel = slope / scale
        out["tail_slope_per_10k"] = round(slope, 3)
        if abs(rel) < a["plateau_rel_slope"] and score < target and "collapse" not in flags:
            flags.append("plateau")
    if target is not None:
        out["target_met"] = bool(score >= target)
    out["flags"] = flags
    return out


def train_flags(train: dict, a: dict) -> list[str]:
    flags = []
    if train.get("nan_columns"):
        flags.append("divergence_nan")
    kl = train.get("max_approx_kl")
    if kl is not None and kl > a["kl_ceiling"]:
        flags.append("high_kl")
    return flags


# --------------------------------------------------------------------------------
# groups of seeds


def bootstrap_ci(x: list[float], *, n_boot: int, level: float, seed: int) -> tuple[float, float] | None:
    if len(x) < 2:
        return None
    rng = np.random.default_rng(seed)
    arr = np.array(x, dtype=float)
    means = rng.choice(arr, size=(n_boot, len(arr)), replace=True).mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def bootstrap_diff_ci(a_: list[float], b_: list[float], *, n_boot: int, level: float,
                      seed: int) -> tuple[float, float] | None:
    if len(a_) < 2 or len(b_) < 2:
        return None
    rng = np.random.default_rng(seed)
    A, B = np.array(a_, float), np.array(b_, float)
    da = rng.choice(A, size=(n_boot, len(A)), replace=True).mean(axis=1)
    db = rng.choice(B, size=(n_boot, len(B)), replace=True).mean(axis=1)
    lo, hi = np.quantile(da - db, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def config_diff(hp: dict, base: dict) -> dict[str, Any]:
    """Hyperparameters that differ from the baseline: {param: value}."""
    keys = sorted(set(hp) | set(base))
    return {k: hp.get(k) for k in keys if hp.get(k) != base.get(k)}


def label_for(diff: dict) -> str:
    if not diff:
        return "baseline"
    return ", ".join(f"{k}={v}" for k, v in diff.items())


def analyze_project(scope: ProjectScope, settings: dict, *, only_cycle: int | None = None,
                    persist_cycle: int | None = None, original_metrics: bool = False) -> dict[str, Any]:
    """Summarise every run, group seeds by config, compare each group with the baseline.

    `only_cycle` restricts the view to runs first seen in that cycle.
    `persist_cycle` writes new findings as diagnoses (idempotent).
    `original_metrics` uses each run's first recorded metric version, i.e. what a stateless
    agent re-reading the run folders would see (memory-off ablation: no approved corrections).
    """
    a = settings["analysis"]
    facts = scope.facts()
    k = int(facts.get("primary_metric", {}).get("k", 5))
    target = facts.get("target_score")
    min_seeds = int(facts.get("min_seeds", 3))
    base_hp = facts.get("baseline_hyperparams", {})
    base_cfg_hash = config_hash({"env_id": facts.get("env_id"), "algo": facts.get("algo"),
                                 "total_timesteps": facts.get("total_timesteps"), "n_envs": facts.get("n_envs", 1),
                                 "hyperparams": base_hp})

    runs = scope.runs(cycle_id=only_cycle) if only_cycle is not None else scope.runs()
    run_rows: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    for r in runs:
        cfg = r["config"]
        if original_metrics:
            hist = [x for x in scope.metric_rows(r["run_id"]) if x["status"] in ("current", "superseded")]
            m = hist[0] if hist else None
        else:
            m = scope.current_metrics(r["run_id"])
        summ = summarize_curve(m["eval"], k=k, target=target, a=a) if m else {"score": None, "flags": ["no_eval"]}
        tflags = train_flags(m["train"], a) if m else []
        flags = list(summ.get("flags", [])) + tflags
        if r["status"] in ("incomplete", "failed", "running", "queued"):
            flags.append(r["status"])
        diff = config_diff(cfg.get("hyperparams", {}), base_hp)
        row = {"run": r["name"], "status": r["status"], "seed": r["seed"], "group": r["config_hash"],
               "label": label_for(diff), "changes": diff, "score": summ.get("score"), "flags": flags,
               "metric_version": m["version"] if m else None, "summary": summ, "launched_by": r["launched_by"]}
        run_rows.append(row)
        if persist_cycle is not None and m:
            for fl in ("collapse", "plateau"):
                if fl in flags:
                    scope.add_diagnosis(run_id=r["run_id"], cycle_id=persist_cycle, kind=fl,
                                        severity="critical" if fl == "collapse" else "warning",
                                        detail={"metric_version": m["version"], **({fl: summ[fl]} if fl in summ else
                                                {"score": summ.get("score"), "slope": summ.get("tail_slope_per_10k")})})
            for fl in tflags:
                scope.add_diagnosis(run_id=r["run_id"], cycle_id=persist_cycle, kind="divergence", severity="critical",
                                    detail={"flag": fl, "nan_columns": m["train"].get("nan_columns"),
                                            "max_approx_kl": m["train"].get("max_approx_kl")})
        g = groups.setdefault(r["config_hash"], {"group": r["config_hash"], "label": label_for(diff), "changes": diff,
                                                 "is_baseline": r["config_hash"] == base_cfg_hash, "runs": [],
                                                 "scores": [], "seeds": [], "flags": {}, "pending": 0})
        g["runs"].append(r["name"])
        if r["status"] == "completed" and summ.get("score") is not None:
            g["scores"].append(summ["score"])
            g["seeds"].append(r["seed"])
        elif r["status"] in ("running", "queued"):
            g["pending"] += 1
        for fl in flags:
            g["flags"][fl] = g["flags"].get(fl, 0) + 1

    base = groups.get(base_cfg_hash)
    for g in groups.values():
        sc = g["scores"]
        g["n"] = len(sc)
        g["mean"] = round(float(np.mean(sc)), 2) if sc else None
        g["std"] = round(float(np.std(sc, ddof=1)), 2) if len(sc) > 1 else None
        ci = bootstrap_ci(sc, n_boot=a["bootstrap_samples"], level=a["ci_level"], seed=a["bootstrap_seed"])
        g["ci95"] = [round(ci[0], 2), round(ci[1], 2)] if ci else None
        if target is not None and g["mean"] is not None:
            g["target_met"] = g["mean"] >= target
        if base is None or g is base:
            g["vs_baseline"] = None
            continue
        diff_ci = bootstrap_diff_ci(sc, base["scores"], n_boot=a["bootstrap_samples"], level=a["ci_level"],
                                    seed=a["bootstrap_seed"])
        if len(sc) < min_seeds or len(base["scores"]) < min_seeds:
            verdict = "insufficient_seeds"
        elif diff_ci and diff_ci[0] > 0:
            verdict = "better"
        elif diff_ci and diff_ci[1] < 0:
            verdict = "worse"
        else:
            verdict = "uncertain"
        g["vs_baseline"] = {
            "diff_mean": round(g["mean"] - base["mean"], 2) if (g["mean"] is not None and base["mean"] is not None) else None,
            "diff_ci95": [round(diff_ci[0], 2), round(diff_ci[1], 2)] if diff_ci else None,
            "verdict": verdict, "n": len(sc), "n_baseline": len(base["scores"]), "min_seeds": min_seeds,
        }

    ranked = sorted((g for g in groups.values() if g["mean"] is not None), key=lambda g: g["mean"], reverse=True)
    return {
        "metric": {"name": "mean eval return over last k evals", "k": k, "target": target, "min_seeds": min_seeds},
        "baseline_group": base_cfg_hash if base else None,
        "runs": run_rows,
        "groups": list(groups.values()),
        "ranking": [{"group": g["group"], "label": g["label"], "mean": g["mean"], "n": g["n"]} for g in ranked],
        "budget": {"runs_used": sum(1 for r in runs if r["launched_by"] == "agent"),
                   "run_budget": facts.get("run_budget")},
    }


def inspect_run(scope: ProjectScope, name: str, settings: dict) -> dict[str, Any]:
    from evo.safety.untrusted import fence
    from evo.tools.ingest import read_log_tail
    from pathlib import Path

    r = scope.run_by_name(name)
    m = scope.current_metrics(r["run_id"])
    facts = scope.facts()
    a = settings["analysis"]
    k = int(facts.get("primary_metric", {}).get("k", 5))
    summ = summarize_curve(m["eval"], k=k, target=facts.get("target_score"), a=a) if m else None
    curve = [(p["step"], p["mean"]) for p in (m["eval"] if m else [])]
    # downsample to ~12 points for the prompt
    stride = max(1, len(curve) // 12)
    tail = read_log_tail(Path(r["folder"]) / "train.log", 15)
    return {
        "run": name, "status": r["status"], "seed": r["seed"], "metric_version": m["version"] if m else None,
        "summary": summ, "train": m["train"] if m else None,
        "curve": [{"step": s, "mean": None if v is None else round(v, 1)} for s, v in curve[::stride]],
        "diagnoses": [{"id": d["diag_id"], "kind": d["kind"]} for d in scope.diagnoses(run_id=r["run_id"])],
        "log_tail": fence(f"train.log of {name}", tail, 1500),
    }
