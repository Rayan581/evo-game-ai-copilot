"""Tool: run_experiment - the controlled action.

Launches APPROVED runs only. Each (project, config, seed) gets a deterministic
folder name and an idempotency key, so a retry, a crash-and-restart, or a second
`evo approve` never starts the same training twice.

Modes (settings.runner.mode):
  live     - spawn train/evo_train.py as a detached background process
  fixture  - copy a matching real run from projects/<id>/fixtures/bank (same
             config hash and seed) if one exists; otherwise train live. Used by
             the eval harness so results are reproducible; every replay is
             labelled in the run's notes and in a REPLAY.txt file.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from evo.memory.actions import ActionLog, ToolRefused
from evo.memory.store import ProjectScope, config_hash
from evo.policy import build_config, check_value
from evo.settings import ROOT
from evo.tools.procs import pid_alive, spawn_detached
from evo.util import now_iso


def run_name(project_id: str, cfg: dict, seed: int) -> str:
    return f"{project_id}-{config_hash(cfg)[:6]}-s{seed}"


def idem_key(project_id: str, proposal_id: int, chash: str, seed: int) -> str:
    return f"run_experiment:{project_id}:p{proposal_id}:{chash}:s{seed}"


def _find_fixture(root_dir: Path, chash: str, seed: int) -> Path | None:
    bank = root_dir / "fixtures" / "bank"
    if not bank.exists():
        return None
    for d in bank.iterdir():
        cfg_p = d / "config.yaml"
        if cfg_p.exists() and (d / "DONE").exists():
            cfg = yaml.safe_load(cfg_p.read_text(encoding="utf-8"))
            if config_hash(cfg) == chash and int(cfg.get("seed", -1)) == seed:
                return d
    return None


def _launch_one(scope: ProjectScope, *, cfg: dict, name: str, key: str, cycle_id: int | None, mode: str,
                wait: bool, timeout_s: int) -> dict[str, Any]:
    folder = scope.root_dir / "runs" / name
    # Idempotency at the effect level: an existing finished or live run is never relaunched.
    if (folder / "DONE").exists():
        return {"run": name, "status": "completed", "reused": True}
    try:
        existing = scope.run_by_name(name)
    except LookupError:
        existing = None
    if existing and existing.get("pid") and pid_alive(existing["pid"]):
        return {"run": name, "status": "running", "reused": True, "pid": existing["pid"]}

    folder.mkdir(parents=True, exist_ok=True)
    cfg = dict(cfg, run_name=name)
    chash = config_hash(cfg)
    fixture = _find_fixture(scope.root_dir, chash, int(cfg["seed"])) if mode == "fixture" else None
    if fixture is not None:
        for f in fixture.iterdir():
            if f.is_file() and f.name != "config.yaml":
                shutil.copy2(f, folder / f.name)
        (folder / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        (folder / "REPLAY.txt").write_text(f"fixture replay of real run {fixture.name} at {now_iso()}\n",
                                           encoding="utf-8")
        scope.upsert_run(name=name, cfg=cfg, status="running", folder=str(folder), file_hashes={},
                         launched_by="agent", cycle_id=cycle_id, idempotency_key=key,
                         notes={"fixture_replay": fixture.name})
        return {"run": name, "status": "launched", "mode": "fixture", "source": fixture.name}

    (folder / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    args = [str(ROOT / "train" / "evo_train.py"), "--run-dir", str(folder)]
    if wait:
        rc = subprocess.run([sys.executable, *args], cwd=ROOT, timeout=timeout_s,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        scope.upsert_run(name=name, cfg=cfg, status="running", folder=str(folder), file_hashes={},
                         launched_by="agent", cycle_id=cycle_id, idempotency_key=key, notes={"mode": "live-wait"})
        return {"run": name, "status": "finished" if rc == 0 else "failed", "mode": "live", "rc": rc}
    proc = spawn_detached(args, ROOT, folder / "stderr.txt")
    scope.upsert_run(name=name, cfg=cfg, status="running", folder=str(folder), file_hashes={},
                     launched_by="agent", cycle_id=cycle_id, idempotency_key=key, pid=proc.pid,
                     notes={"mode": "live", "launched_at": now_iso()})
    return {"run": name, "status": "launched", "mode": "live", "pid": proc.pid}


def execute_run_proposal(scope: ProjectScope, log: ActionLog, proposal: dict, settings: dict,
                         *, cycle_id: int | None, wait: bool = False) -> list[dict[str, Any]]:
    if proposal["status"] not in ("approved", "failed"):
        raise ToolRefused(f"proposal {proposal['proposal_id']} is {proposal['status']}; it must be approved first")
    approvals = [a for a in scope.approvals() if a["subject_type"] == "proposal"
                 and a["subject_id"] == str(proposal["proposal_id"]) and a["decision"] == "approved"]
    if not approvals:
        raise ToolRefused(f"no approval recorded for proposal {proposal['proposal_id']}")
    facts = scope.facts()
    space = facts.get("search_space", {})
    mode = settings["runner"]["mode"]
    results = []
    for item in proposal["payload"]["runs"]:
        # Re-validate at execution time: facts may have been corrected since the proposal.
        for k, v in item["change"].items():
            msg = check_value(k, v, space)
            if msg:
                raise ToolRefused(f"proposal no longer valid under current facts: {msg}")
        cfg = build_config(facts, item["change"], item["seed"])
        name = run_name(scope.pid, cfg, item["seed"])
        key = idem_key(scope.pid, proposal["proposal_id"], config_hash(cfg), item["seed"])
        res = log.call("run_experiment", {"run": name, "change": item["change"], "seed": item["seed"],
                                          "proposal": proposal["proposal_id"]},
                       lambda cfg=cfg, name=name, key=key: _launch_one(
                           scope, cfg=cfg, name=name, key=key, cycle_id=cycle_id, mode=mode, wait=wait,
                           timeout_s=settings["runner"]["timeout_s"]),
                       project_id=scope.pid, cycle_id=cycle_id, idempotency_key=key)
        results.append(res)
    return results


def reconcile(scope: ProjectScope, log: ActionLog) -> list[dict[str, Any]]:
    """After a crash/restart: settle side effects left 'running' by a dead process."""
    out = []
    rows = scope.conn.execute(
        "SELECT * FROM side_effects WHERE project_id=? AND status='running' AND tool='run_experiment'", (scope.pid,)
    ).fetchall()
    for r in rows:
        key = r["idempotency_key"]
        name_part = key.split(":")
        try:
            run = next(x for x in scope.runs() if x.get("idempotency_key") == key)
        except StopIteration:
            run = None
        if run is not None:
            log._settle(key, "succeeded", {"run": run["name"], "status": "reconciled"})
            out.append({"key": key, "settled": "succeeded", "run": run["name"]})
        else:
            log._settle(key, "failed", {"reconciled": "no run recorded"})
            out.append({"key": key, "settled": "failed", "parts": name_part[-2:]})
    if out:
        log.note("reconcile", {"count": len(out)}, project_id=scope.pid, result=out)
    return out


def wait_for_runs(scope: ProjectScope, names: list[str], timeout_s: int = 3600, poll_s: float = 5.0) -> None:
    """Block until the named runs have a DONE marker or their process has exited."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        pending = []
        for n in names:
            r = scope.run_by_name(n)
            folder = Path(r["folder"])
            if (folder / "DONE").exists():
                continue
            if r.get("pid") and pid_alive(r["pid"]):
                pending.append(n)
        if not pending:
            return
        time.sleep(poll_s)
