"""Tool: ingest_runs - read local run folders into memory (evidence retrieval).

Deterministic. File contents are untrusted: configs are parsed with
yaml.safe_load and validated, logs are only scanned and quoted, never obeyed.
Re-ingesting unchanged folders is a no-op (file hashes), so the tool is safe
to retry.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import yaml

from evo.memory.store import ProjectScope
from evo.safety.untrusted import scan
from evo.tools.procs import pid_alive
from evo.util import sha256_file, sha256_text

CONTRACT = ["config.yaml", "metrics.csv", "eval.csv", "train.log", "DONE"]
REQUIRED_CFG = ["env_id", "algo", "seed", "total_timesteps", "hyperparams"]


def parse_eval_csv(path: Path) -> list[dict[str, Any]]:
    pts: list[dict[str, Any]] = []
    if not path.exists():
        return pts
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            try:
                step = int(float(row["step"]))
                mean = float(row["mean"])
                std = float(row.get("std") or "nan")
                n = int(float(row.get("n") or 0))
            except (KeyError, ValueError, TypeError):
                continue  # malformed line: skipped, not guessed
            pts.append({"step": step, "mean": mean if math.isfinite(mean) else None,
                        "std": std if math.isfinite(std) else None, "n": n})
    pts.sort(key=lambda p: p["step"])
    return pts


def summarize_metrics_csv(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"rows": 0, "nan_columns": [], "max_approx_kl": None, "last_ep_rew_mean": None,
                           "last_step": None}
    if not path.exists():
        return out
    nan_cols: set[str] = set()
    max_kl = None
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            out["rows"] += 1
            for k, v in row.items():
                if v in ("", None):
                    continue
                try:
                    x = float(v)
                except ValueError:
                    continue
                if not math.isfinite(x):
                    nan_cols.add(k)
                    continue
                if k == "approx_kl":
                    max_kl = x if max_kl is None else max(max_kl, x)
                if k == "ep_rew_mean":
                    out["last_ep_rew_mean"] = x
                if k == "step":
                    out["last_step"] = int(x)
    out["nan_columns"] = sorted(nan_cols)
    out["max_approx_kl"] = max_kl
    return out


def read_log_tail(path: Path, n_lines: int = 40) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n_lines:])


def validate_config(cfg: Any, project: dict) -> list[str]:
    problems = []
    if not isinstance(cfg, dict):
        return ["config.yaml is not a mapping"]
    for k in REQUIRED_CFG:
        if k not in cfg:
            problems.append(f"missing '{k}'")
    if cfg.get("env_id") and cfg["env_id"] != project["env_id"]:
        problems.append(f"env_id {cfg['env_id']} != project env {project['env_id']}")
    if cfg.get("algo") and str(cfg["algo"]).upper() != project["algo"].upper():
        problems.append(f"algo {cfg['algo']} != project algo {project['algo']}")
    if "hyperparams" in cfg and not isinstance(cfg["hyperparams"], dict):
        problems.append("hyperparams is not a mapping")
    return problems


def _status(folder: Path, eval_pts: list, prev: dict | None) -> str:
    log_text = (folder / "train.log").read_text(encoding="utf-8", errors="replace") if (folder / "train.log").exists() else ""
    if (folder / "DONE").exists() and eval_pts:
        return "completed"
    if "run failed" in log_text or "Traceback" in log_text:
        return "failed"
    if prev and prev.get("pid") and pid_alive(prev["pid"]):
        return "running"
    return "incomplete"


def ingest_runs(scope: ProjectScope, cycle_id: int | None) -> dict[str, Any]:
    project = scope.project()
    runs_dir = Path(project["root_dir"]) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    known = {r["name"]: r for r in scope.runs()}
    report: dict[str, Any] = {"new": [], "updated": [], "unchanged": 0, "invalid": [], "corrections_proposed": [],
                              "injection_flags": [], "still_running": []}

    for folder in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        if runs_dir.resolve() not in folder.resolve().parents:
            continue  # symlink escaping the project: ignored
        name = folder.name
        hashes = {f: sha256_file(folder / f) for f in CONTRACT if (folder / f).exists()}
        prev = known.get(name)
        if prev and prev["file_hashes"] == hashes and prev["status"] not in ("running",):
            report["unchanged"] += 1
            continue
        if "config.yaml" not in hashes:
            report["invalid"].append({"run": name, "problems": ["no config.yaml"]})
            continue
        try:
            cfg = yaml.safe_load((folder / "config.yaml").read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            report["invalid"].append({"run": name, "problems": [f"unparseable config: {e}"]})
            continue
        problems = validate_config(cfg, project)
        if problems:
            report["invalid"].append({"run": name, "problems": problems})
            run_id = scope.upsert_run(name=name, cfg=cfg if isinstance(cfg, dict) else {}, status="failed",
                                      folder=str(folder), file_hashes=hashes, launched_by="user", cycle_id=cycle_id,
                                      notes={"invalid": problems})
            scope.add_diagnosis(run_id=run_id, cycle_id=cycle_id, kind="invalid_config", severity="warning",
                                detail={"problems": problems})
            continue

        eval_pts = parse_eval_csv(folder / "eval.csv")
        train = summarize_metrics_csv(folder / "metrics.csv")
        status = _status(folder, eval_pts, prev)
        log_tail = read_log_tail(folder / "train.log")
        train["log_tail_hash"] = sha256_text(log_tail)[:12]
        if status == "running":
            report["still_running"].append(name)

        run_id = scope.upsert_run(name=name, cfg=cfg, status=status, folder=str(folder), file_hashes=hashes,
                                  launched_by=(prev or {}).get("launched_by", "user"), cycle_id=cycle_id,
                                  pid=(prev or {}).get("pid"))
        (report["updated"] if prev else report["new"]).append({"run": name, "status": status})

        eval_hash = hashes.get("eval.csv", "none")
        current = scope.current_metrics(run_id)
        if eval_pts:
            if current is None:
                scope.add_metric_version(run_id, eval_points=eval_pts, train=train,
                                         source_ref=str(folder / "eval.csv"), source_hash=eval_hash)
            elif current["source_hash"] != eval_hash:
                was_final = prev is not None and prev["status"] == "completed"
                if was_final:
                    # A finished run's results changed on disk: a correction. Keep the old
                    # version current until the user approves the new one.
                    mid = scope.add_metric_version(run_id, eval_points=eval_pts, train=train,
                                                   source_ref=str(folder / "eval.csv"), source_hash=eval_hash,
                                                   status="proposed", reason="eval.csv changed after completion")
                    pid_, _ = scope.create_proposal(
                        kind="metric_correction", payload={"run": name, "metric_id": mid},
                        rationale=f"eval.csv of completed run {name} changed on disk", evidence=[f"run:{name}"],
                        cycle_id=cycle_id, created_by="ingest")
                    scope.add_diagnosis(run_id=run_id, cycle_id=cycle_id, kind="source_changed", severity="warning",
                                        detail={"file": "eval.csv", "proposal": pid_})
                    report["corrections_proposed"].append({"run": name, "proposal": pid_})
                else:
                    scope.add_metric_version(run_id, eval_points=eval_pts, train=train,
                                             source_ref=str(folder / "eval.csv"), source_hash=eval_hash,
                                             reason="run progressed")

        if status == "incomplete":
            scope.add_diagnosis(run_id=run_id, cycle_id=cycle_id, kind="incomplete", severity="warning",
                                detail={"evals": len(eval_pts), "done_marker": False,
                                        "last_step": eval_pts[-1]["step"] if eval_pts else None,
                                        "target_steps": cfg.get("total_timesteps")})
        if status == "failed":
            scope.add_diagnosis(run_id=run_id, cycle_id=cycle_id, kind="failed", severity="critical",
                                detail={"log_tail_hash": train["log_tail_hash"]})
        hits = scan(log_tail) + scan((folder / "config.yaml").read_text(encoding="utf-8", errors="replace"))
        if hits:
            did, created = scope.add_diagnosis(run_id=run_id, cycle_id=cycle_id, kind="untrusted_instruction",
                                               severity="warning", detail={"hits": hits[:5]})
            report["injection_flags"].append({"run": name, "diag": did, "hits": [h["excerpt"] for h in hits[:3]]})

    # runs whose folders vanished are kept (history) but noted
    on_disk = {p.name for p in runs_dir.iterdir() if p.is_dir()}
    report["missing_folders"] = sorted(set(known) - on_disk)
    return report
