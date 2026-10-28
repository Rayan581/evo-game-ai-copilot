"""Orchestrator: onboarding, the review-cycle loop, approvals and corrections.

One cycle:
  0. reconcile anything a crash left half-done; open or resume the cycle
  1. ingest + analyse (deterministic, always first)
  2. loop: brief -> planner decision -> policy gate -> tool -> observation -> checkpoint
  3. close: summary, register export, cycle report
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

import yaml

from evo.memory.actions import ActionLog, ToolFailed, ToolRefused
from evo.memory.store import ProjectScope, Store
from evo.planner import Planner, build_brief
from evo.policy import Decision, PolicyGate, normalize
from evo.settings import knowledge_dir
from evo.tools import analyze as analyze_tool
from evo.tools import export as export_tool
from evo.tools import ingest as ingest_tool
from evo.tools import knowledge as kb_tool
from evo.tools import runner as runner_tool
from evo.util import jdumps, sha256_file

FACT_KEYS = ["goal", "owner", "env_id", "algo", "total_timesteps", "n_envs", "device", "eval", "primary_metric",
             "target_score", "min_seeds", "run_budget", "deadline", "env_allowlist", "baseline_hyperparams",
             "search_space"]

# approver(proposal) -> ("approved" | "rejected", comment) or None to leave it pending
Approver = Callable[[dict], "tuple[str, str | None] | None"]


# ----------------------------------------------------------------------------------------
# onboarding


def read_onboarding_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("project file must be a YAML mapping")
    missing = [k for k in ("project_id", "name", "env_id", "algo", "baseline_hyperparams", "search_space",
                           "total_timesteps") if k not in data]
    if missing:
        raise ValueError(f"project file missing: {', '.join(missing)}")
    if not str(data["project_id"]).replace("-", "").replace("_", "").isalnum():
        raise ValueError("project_id must be a simple slug")
    return data


def onboard(store: Store, path: Path, *, actor: str, log: ActionLog | None = None) -> dict[str, Any]:
    """Create the project and its v1 facts (call only after the user approved the extracted facts)."""
    log = log or ActionLog(store)
    data = read_onboarding_file(path)
    pid = str(data["project_id"])
    if store.project_exists(pid):
        return {"project_id": pid, "created": False, "note": "already onboarded; use `evo correct` to change facts"}

    def _do():
        store.create_project(project_id=pid, name=data["name"], env_id=data["env_id"], algo=data["algo"],
                             root_dir=str(path.parent.resolve()), source_ref=str(path), source_hash=sha256_file(path))
        scope = store.scope(pid)
        for k in FACT_KEYS:
            if k in data:
                scope.add_fact_version(k, data[k], source_ref=f"{path.name}#{k}", proposed_by="onboarding",
                                       approved_by=actor)
        scope.record_approval(subject_type="onboarding", subject_id=pid, decision="approved",
                              comment=f"baseline facts from {path.name}", actor=actor)
        return {"project_id": pid, "created": True, "facts": len([k for k in FACT_KEYS if k in data])}

    return log.call("onboard", {"file": str(path)}, _do, project_id=pid, idempotency_key=f"onboard:{pid}")


# ----------------------------------------------------------------------------------------


class Agent:
    def __init__(self, store: Store, project_id: str, settings: dict, *, provider: str | None = None,
                 memory_mode: str = "on", approver: Approver | None = None, log: ActionLog | None = None,
                 actor: str = "user", write_reports: bool = True):
        self.store = store
        self.scope: ProjectScope = store.scope(project_id)
        self.settings = settings
        self.memory_mode = memory_mode
        self.planner = Planner(store, settings, provider)
        self.log = log or ActionLog(store)
        self.approver = approver
        self.actor = actor
        self.write_reports = write_reports
        self.events: list[str] = []  # human-readable trace for the CLI

    def say(self, msg: str) -> None:
        self.events.append(msg)

    # ------------------------------------------------------------------------------------
    def run_cycle(self, clock: str | None = None, user_request: dict | None = None) -> dict[str, Any]:
        scope, log = self.scope, self.log
        settings = self.settings
        kb_tool.index_knowledge(self.store, knowledge_dir())
        rec = runner_tool.reconcile(scope, log)
        if rec:
            self.say(f"reconciled {len(rec)} interrupted side effect(s) from a previous process")

        last = scope.latest_cycle()
        if last and last["status"] == "open":
            cycle = last
            cp = cycle["checkpoint"] or {}
            self.memory_mode = cycle["memory_mode"]
            self.say(f"resuming cycle {cycle['number']} from checkpoint at step {cp.get('step', 0)}")
        else:
            cycle = scope.open_cycle(clock=clock or date.today().isoformat(), memory_mode=self.memory_mode)
            cp = {}
            self.say(f"opened cycle {cycle['number']} (memory {self.memory_mode}, clock {cycle['clock']})")
        cid = cycle["cycle_id"]

        # step 0: ingest + analyse ----------------------------------------------------------
        if "ingest" not in cp:
            before = {d["diag_id"] for d in scope.diagnoses()}
            report = log.call("ingest_runs", {"project": scope.pid},
                              lambda: ingest_tool.ingest_runs(scope, cid), project_id=scope.pid, cycle_id=cid, step=0)
            touched = sorted({x["run"] for x in report["new"] + report["updated"]})
            analysis = self._analyze(cid, touched, persist=True)
            self._auto_lessons(analysis, cid)
            new_diags = sorted({d["diag_id"] for d in scope.diagnoses()} - before)
            cp = {"step": 0, "ingest": report, "touched": touched, "new_diags": new_diags, "observations": [],
                  "user_request": user_request or None}
            scope.save_checkpoint(cid, cp)
            self.say(f"ingested: {len(report['new'])} new, {len(report['updated'])} updated, "
                     f"{report['unchanged']} unchanged; {len(new_diags)} new finding(s)")
            for f in report["injection_flags"]:
                self.say(f"flagged instruction-like text in {f['run']} (diag:{f['diag']}) - treated as data")
        report, touched = cp["ingest"], cp["touched"]

        # decision loop --------------------------------------------------------------------
        max_steps = settings["agent"]["max_steps_per_cycle"]
        summary: dict[str, Any] = {}
        step = cp.get("step", 0)
        while step < max_steps:
            step += 1
            analysis = self._analyze(cid, touched, persist=False)
            brief = build_brief(scope, cycle=cycle, step=step, max_steps=max_steps, analysis=analysis,
                                ingest_report=report, new_diag_ids=cp["new_diags"], observations=cp["observations"],
                                memory_mode=self.memory_mode, settings=settings,
                                user_request=cp.get("user_request"))
            self.store.tracker.drain()
            decision, meta = self.planner.decide(brief)
            decision = normalize(decision)
            gate = PolicyGate(scope, memory_mode=self.memory_mode, analysis=analysis).check(decision)
            scope.add_decision(cycle_id=cid, step=step, action=decision.action, arguments=decision.arguments,
                               rationale=decision.rationale, evidence=decision.evidence, gate=gate.as_dict(),
                               memory_mode=self.memory_mode, provider=meta["provider"], model=meta["model"],
                               prompt_hash=meta["prompt_hash"], latency_ms=meta.get("latency_ms"), brief=brief)
            obs: dict[str, Any] = {"step": step, "action": decision.action, "arguments": decision.arguments,
                                   "evidence": decision.evidence, "allowed": gate.allowed}
            if not gate.allowed:
                obs["reasons"] = gate.reasons
                log.note("policy_gate", {"action": decision.action, "arguments": decision.arguments},
                         project_id=scope.pid, cycle_id=cid, step=step, status="refused", error="; ".join(gate.reasons))
                self.say(f"step {step}: {decision.action} REFUSED by policy gate - {'; '.join(gate.reasons)}")
            else:
                self.say(f"step {step}: {decision.action} {jdumps(decision.arguments)[:160]}")
                try:
                    obs["result"] = self._execute(decision, gate.normalized, cid, step, analysis)
                except (ToolFailed, ToolRefused) as e:
                    obs["error"] = str(e)
                    self.say(f"  tool error: {e} (state kept; safe to rerun `evo cycle`)")
            cp["observations"].append(_compact(obs))
            cp["step"] = step
            scope.save_checkpoint(cid, cp)
            if gate.allowed and decision.action in ("end_cycle", "ask_user"):
                summary = {"recommendation": decision.arguments.get("summary") or decision.arguments.get("question"),
                           "ended_by": decision.action}
                break
        else:
            summary = {"recommendation": "step budget reached", "ended_by": "budget"}

        # close ---------------------------------------------------------------------------------
        final = self._analyze(cid, touched, persist=False)
        summary["pending"] = [p["proposal_id"] for p in scope.proposals("pending")]
        if cp.get("user_request"):
            summary["task"] = cp["user_request"]
        summary["planner"] = {"provider": self.planner.provider, "model": self.planner.model,
                              "llm_errors": self.planner.errors}
        if self.write_reports:
            exp = log.call("export_register", {"project": scope.pid},
                           lambda: export_tool.export_register(scope, final), project_id=scope.pid, cycle_id=cid,
                           step=step + 1)
            rpt = export_tool.write_cycle_report(scope, scope.cycle(cid), final, summary)
            summary["register"], summary["report"] = exp["path"], rpt
        else:
            summary["register"], summary["report"] = None, None
        scope.close_cycle(cid, summary)
        self.say(f"closed cycle {cycle['number']}: {summary['recommendation']}")
        return {"cycle": cycle["number"], "cycle_id": cid, "summary": summary, "events": self.events}

    # ------------------------------------------------------------------------------------
    def _analyze(self, cid: int, touched: list[str], *, persist: bool) -> dict[str, Any]:
        scope = self.scope
        # memory off = a stateless agent: it re-reads every run folder, but has no memory of
        # approved corrections, hypotheses, lessons, decisions or the user's feedback.
        return analyze_tool.analyze_project(scope, self.settings, persist_cycle=cid if persist else None,
                                            original_metrics=self.memory_mode == "off")

    def _auto_lessons(self, analysis: dict, cid: int) -> None:
        """Every seed of a config collapsed or diverged -> an 'avoid' lesson (user can reject it)."""
        base = self.scope.fact("baseline_hyperparams", {})
        for g in analysis["groups"]:
            bad = g["flags"].get("collapse", 0) + g["flags"].get("divergence_nan", 0)
            if not g["n"] or bad < g["n"] or len(g["changes"]) != 1:
                continue
            (param, val), = g["changes"].items()
            if not isinstance(val, (int, float)) or param not in base:
                continue
            op = "gte" if float(val) > float(base[param]) else "lte"
            text = (f"{param} {'>=' if op == 'gte' else '<='} {val:g} failed ({'collapse' if g['flags'].get('collapse') else 'divergence'} "
                    f"in {bad}/{g['n']} seed(s)) - avoid")
            self.scope.add_lesson(text=text, kind="avoid", region={param: {op: val}},
                                  source={"group": g["group"], "runs": g["runs"], "cycle": cid},
                                  approved_by="auto:evidence")

    # ------------------------------------------------------------------------------------
    def _execute(self, d: Decision, norm: dict, cid: int, step: int, analysis: dict) -> Any:
        scope, log = self.scope, self.log
        a = d.arguments
        call = lambda tool, fn, **kw: log.call(tool, {"action": d.action, **a}, fn, project_id=scope.pid,  # noqa: E731
                                                cycle_id=cid, step=step, **kw)
        if d.action == "inspect_run":
            return call("inspect_run", lambda: analyze_tool.inspect_run(scope, a["run"], self.settings))
        if d.action == "compare_runs":
            return call("analyze_runs", lambda: {"groups": [{k: g[k] for k in ("group", "label", "n", "mean", "ci95",
                                                                                "vs_baseline", "flags")}
                                                             for g in analysis["groups"]]})
        if d.action == "search_knowledge":
            return call("search_knowledge", lambda: kb_tool.search(self.store, a["query"]))
        if d.action == "open_hypothesis":
            label = call("memory.write", lambda: scope.open_hypothesis(
                statement=a["statement"], change=norm["change"], evidence=d.evidence, cycle_id=cid,
                created_by=f"agent:{self.planner.provider}"))
            return {"label": label, "change": norm["change"]}
        if d.action in ("propose_runs", "request_seeds"):
            payload = {"runs": [{"change": norm["change"], "seed": s, "config_hash": norm["config_hash"]}
                                for s in norm["seeds"]],
                       "hypothesis": norm.get("hypothesis"), "new_reason": a.get("new_reason")}
            pid, created = call("memory.write", lambda: scope.create_proposal(
                kind="runs", payload=payload, rationale=d.rationale, evidence=d.evidence, cycle_id=cid,
                created_by="agent"))
            res = {"proposal": pid, "runs": len(payload["runs"]), "seeds": norm["seeds"], "change": norm["change"]}
            res.update(self._maybe_approve(pid, cid))
            return res
        if d.action == "resolve_hypothesis":
            payload = {"hypothesis": a["hypothesis"], "outcome": a["outcome"], "group": norm.get("group"),
                       "vs_baseline": norm.get("vs_baseline")}
            pid, _ = call("memory.write", lambda: scope.create_proposal(
                kind="resolve_hypothesis", payload=payload, rationale=d.rationale, evidence=d.evidence,
                cycle_id=cid, created_by="agent"))
            res = {"proposal": pid}
            res.update(self._maybe_approve(pid, cid))
            return res
        if d.action in ("ask_user", "end_cycle"):
            return {"noted": True}
        raise ToolRefused(f"no executor for {d.action}")

    def _maybe_approve(self, proposal_id: int, cid: int) -> dict[str, Any]:
        if self.approver is None:
            return {"approval": "pending"}
        prop = self.scope.proposal(proposal_id)
        verdict = self.approver(prop)
        if verdict is None:
            return {"approval": "pending"}
        decision, comment = verdict
        return decide_proposal(self.scope, self.log, self.settings, proposal_id, decision, comment=comment,
                               actor=self.actor, cycle_id=cid)


# ----------------------------------------------------------------------------------------
# approvals and corrections (also used directly by the CLI)


def decide_proposal(scope: ProjectScope, log: ActionLog, settings: dict, proposal_id: int, decision: str, *,
                    comment: str | None, actor: str, cycle_id: int | None = None, wait: bool = False) -> dict[str, Any]:
    prop = scope.proposal(proposal_id)
    if prop["status"] in ("executed", "rejected"):
        return {"approval": prop["status"], "note": "already decided"}
    if prop["status"] == "pending":
        scope.record_approval(subject_type="proposal", subject_id=str(proposal_id), decision=decision,
                              comment=comment, actor=actor)
        scope.set_proposal_status(proposal_id, "approved" if decision == "approved" else "rejected")
    if decision != "approved":
        return {"approval": "rejected"}
    prop = scope.proposal(proposal_id)
    try:
        result = execute_proposal(scope, log, settings, prop, cycle_id=cycle_id, wait=wait)
    except (ToolFailed, ToolRefused) as e:
        scope.set_proposal_status(proposal_id, "failed")
        return {"approval": "approved", "executed": False, "error": str(e)}
    scope.set_proposal_status(proposal_id, "executed")
    return {"approval": "approved", "executed": True, "result": result}


def execute_proposal(scope: ProjectScope, log: ActionLog, settings: dict, prop: dict, *, cycle_id: int | None,
                     wait: bool = False) -> Any:
    kind, p = prop["kind"], prop["payload"]
    if kind == "runs":
        return runner_tool.execute_run_proposal(scope, log, prop, settings, cycle_id=cycle_id, wait=wait)
    if kind == "resolve_hypothesis":
        def _do():
            h = scope.hypothesis(p["hypothesis"])
            vs = p.get("vs_baseline") or {}
            summary = (f"{p['outcome']}: diff {vs.get('diff_mean')} (CI {vs.get('diff_ci95')}) over "
                       f"{vs.get('n')} vs {vs.get('n_baseline')} seeds")
            scope.resolve_hypothesis(p["hypothesis"], status=p["outcome"],
                                     resolution={"summary": summary, "vs_baseline": vs, "proposal": prop["proposal_id"]},
                                     cycle_id=cycle_id or 0)
            if p["outcome"] in ("supported", "refuted"):
                region = {k: {"eq": v} for k, v in h["change"].items()}
                scope.add_lesson(text=f"{h['label']} {p['outcome']}: {', '.join(f'{k}={v}' for k, v in h['change'].items())} "
                                      f"is {'better' if p['outcome'] == 'supported' else 'worse'} than the baseline",
                                 kind="prefer" if p["outcome"] == "supported" else "avoid", region=region,
                                 source={"hypothesis": h["label"], "proposal": prop["proposal_id"]},
                                 approved_by="user")
            return {"hypothesis": p["hypothesis"], "status": p["outcome"]}
        return log.call("memory.write", {"resolve": p["hypothesis"]}, _do, project_id=scope.pid, cycle_id=cycle_id,
                        idempotency_key=f"resolve:{scope.pid}:{prop['proposal_id']}")
    if kind == "fact_change":
        return log.call("memory.write", {"fact": p["key"]}, lambda: scope.add_fact_version(
            p["key"], p["value"], source_ref=p.get("source_ref", "user-correction"), proposed_by="user",
            approved_by="user", reason=p.get("reason")), project_id=scope.pid, cycle_id=cycle_id,
            idempotency_key=f"fact:{scope.pid}:{prop['proposal_id']}")
    if kind == "metric_correction":
        def _do():
            run = scope.run_by_name(p["run"])
            scope.promote_metric(p["metric_id"], approved_by="user")
            scope.supersede_diagnoses(run["run_id"], ["collapse", "plateau", "divergence"])
            return {"run": p["run"], "metric_id": p["metric_id"]}
        return log.call("memory.write", {"metric_correction": p["run"]}, _do, project_id=scope.pid,
                        cycle_id=cycle_id, idempotency_key=f"metric:{scope.pid}:{prop['proposal_id']}")
    raise ToolRefused(f"unknown proposal kind {kind}")


def propose_fact_change(scope: ProjectScope, key: str, value: Any, reason: str) -> int:
    from evo.agent import FACT_KEYS as _K
    if key not in _K:
        raise ValueError(f"unknown fact '{key}' (known: {', '.join(_K)})")
    pid, _ = scope.create_proposal(kind="fact_change", payload={"key": key, "value": value, "reason": reason},
                                   rationale=reason, evidence=[f"fact:{key}"], cycle_id=None, created_by="user")
    return pid


def propose_metric_correction(scope: ProjectScope, run: str, eval_file: Path, reason: str) -> int:
    r = scope.run_by_name(run)
    pts = ingest_tool.parse_eval_csv(eval_file)
    if not pts:
        raise ValueError(f"{eval_file} has no valid eval rows")
    cur = scope.current_metrics(r["run_id"])
    mid = scope.add_metric_version(r["run_id"], eval_points=pts, train=(cur or {}).get("train", {}),
                                   source_ref=str(eval_file), source_hash=sha256_file(eval_file), status="proposed",
                                   reason=reason)
    pid, _ = scope.create_proposal(kind="metric_correction", payload={"run": run, "metric_id": mid,
                                                                      "file": str(eval_file)},
                                   rationale=reason, evidence=[f"run:{run}"], cycle_id=None, created_by="user")
    return pid


# ----------------------------------------------------------------------------------------


def _compact(obs: dict) -> dict:
    """Bound the size of an observation kept in the checkpoint / brief."""
    res = obs.get("result")
    if isinstance(res, list):
        obs["result"] = res[:4]
    elif isinstance(res, dict) and "groups" in res:
        obs["result"] = {"groups": res["groups"][:8]}
    return obs
