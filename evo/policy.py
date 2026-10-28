"""Policy gate: deterministic checks on every planner decision.

The LLM proposes; this module decides whether the proposal is allowed. It never
calls the LLM and never reads permissions from untrusted text - only from the
current approved project facts and the store.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from evo.memory.store import IsolationError, NotFound, ProjectScope, config_hash
from evo.tools.knowledge import kb_ref_exists

ACTIONS = {
    "inspect_run": "Look at one run's curve, diagnostics and (untrusted) log tail. args: {run}",
    "compare_runs": "Get the full deterministic comparison of all config groups vs the baseline. args: {}",
    "search_knowledge": "Search cited RL tuning notes. args: {query}",
    "open_hypothesis": "Record a testable hypothesis. args: {statement, change: {param: value}}",
    "propose_runs": "Propose new runs of ONE config (baseline + change) for user approval. "
                    "args: {change: {param: value}, seeds: int, hypothesis?: 'H1', new_reason?: str}",
    "request_seeds": "Propose more seeds of an existing config group. args: {group, seeds}",
    "resolve_hypothesis": "Propose closing a hypothesis (user approves). args: {hypothesis, outcome: supported|refuted|inconclusive}",
    "ask_user": "Stop and ask the user a question when evidence is missing or contradictory. args: {question}",
    "end_cycle": "Finish this review cycle. args: {summary}",
}
NEEDS_EVIDENCE = {"open_hypothesis", "propose_runs", "request_seeds", "resolve_hypothesis"}
MAX_SEEDS_PER_PROPOSAL = 5


class Decision(BaseModel):
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)


def normalize(d: Decision) -> Decision:
    """Tolerate harmless formatting differences in LLM output (prefixed refs, case, numeric strings)."""
    a = dict(d.arguments or {})
    for key, prefix in (("hypothesis", "hyp:"), ("run", "run:"), ("group", "group:")):
        if isinstance(a.get(key), str) and a[key].startswith(prefix):
            a[key] = a[key][len(prefix):]
    if isinstance(a.get("outcome"), str):
        a["outcome"] = a["outcome"].strip().lower()
    if "seeds" in a:
        try:
            a["seeds"] = int(a["seeds"])
        except (TypeError, ValueError):
            pass
    action = d.action.strip().lower()
    return Decision(action=action, arguments=a, rationale=d.rationale, evidence=[str(e).strip() for e in d.evidence])


def parse_decision(obj: Any) -> Decision:
    if not isinstance(obj, dict):
        raise ValidationError.from_exception_data("Decision", [])  # pragma: no cover
    return Decision.model_validate(obj)


@dataclass
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reasons": self.reasons, "normalized": self.normalized}


# --------------------------------------------------------------------------------------
# helpers shared with the runner


def check_value(param: str, value: Any, space: dict[str, Any]) -> str | None:
    spec = space.get(param)
    if spec is None:
        return f"'{param}' is not in the approved search space ({', '.join(sorted(space))})"
    if "choices" in spec:
        if value not in spec["choices"]:
            return f"{param}={value!r} not in allowed choices {spec['choices']}"
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return f"{param}={value!r} is not numeric"
    if spec.get("type") == "int" and float(x) != int(x):
        return f"{param} must be an integer"
    if "min" in spec and x < float(spec["min"]) - 1e-12:
        return f"{param}={value} below approved minimum {spec['min']}"
    if "max" in spec and x > float(spec["max"]) + 1e-12:
        return f"{param}={value} above approved maximum {spec['max']}"
    return None


def coerce_change(change: dict[str, Any], space: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in change.items():
        spec = space.get(k, {})
        if "choices" in spec:
            out[k] = v
        elif spec.get("type") == "int":
            out[k] = int(float(v))
        else:
            out[k] = float(v)
    return out


def build_config(facts: dict[str, Any], change: dict[str, Any], seed: int) -> dict[str, Any]:
    hp = dict(facts.get("baseline_hyperparams", {}))
    hp.update(change)
    return {
        "env_id": facts["env_id"], "algo": facts["algo"], "seed": int(seed),
        "total_timesteps": int(facts["total_timesteps"]), "n_envs": int(facts.get("n_envs", 1)),
        "device": facts.get("device", "auto"), "eval": facts.get("eval", {"freq": 10000, "episodes": 5}),
        "hyperparams": hp,
    }


def in_region(hp: dict[str, Any], region: dict[str, Any]) -> bool:
    for param, cond in region.items():
        v = hp.get(param)
        if v is None:
            return False
        for op, ref in cond.items():
            if op == "eq" and v != ref:
                return False
            if op in ("gte", "lte"):
                try:
                    fv, fr = float(v), float(ref)
                except (TypeError, ValueError):
                    return False
                if op == "gte" and fv < fr - 1e-12:
                    return False
                if op == "lte" and fv > fr + 1e-12:
                    return False
    return True


# --------------------------------------------------------------------------------------


class PolicyGate:
    def __init__(self, scope: ProjectScope, *, memory_mode: str, analysis: dict[str, Any]):
        self.scope = scope
        self.memory_mode = memory_mode
        self.analysis = analysis
        self.facts = scope.facts()

    # -- evidence --------------------------------------------------------------------
    def evidence_problems(self, refs: list[str]) -> list[str]:
        bad = []
        for ref in refs:
            ok = False
            try:
                if ref.startswith("run:"):
                    self.scope.run_by_name(ref[4:])
                    ok = True
                elif ref.startswith("diag:"):
                    self.scope.diagnosis(int(ref[5:]))
                    ok = True
                elif ref.startswith("hyp:"):
                    self.scope.hypothesis(ref[4:])
                    ok = True
                elif ref.startswith("lesson:"):
                    ok = any(int(ref[7:]) == l["lesson_id"] for l in self.scope.lessons())
                elif ref.startswith("fact:"):
                    ok = ref[5:] in self.facts
                elif ref.startswith("group:"):
                    ok = any(g["group"].startswith(ref[6:]) for g in self.analysis["groups"]) and len(ref) > 9
                elif ref.startswith("kb:"):
                    ok = kb_ref_exists(self.scope.store, ref)
            except (NotFound, IsolationError, ValueError):
                ok = False
            if not ok:
                bad.append(ref)
        return [f"unknown or out-of-project evidence reference '{b}'" for b in bad]

    # -- budget ------------------------------------------------------------------------
    def runs_committed(self) -> int:
        used = sum(1 for r in self.scope.runs() if r["launched_by"] == "agent")
        pending = sum(len(p["payload"].get("runs", [])) for p in self.scope.proposals(kind="runs")
                      if p["status"] in ("pending", "approved"))
        return used + pending

    def _seeds_for(self, change: dict[str, Any], n: int) -> list[int]:
        cfg0 = build_config(self.facts, change, 0)
        h = config_hash(cfg0)
        used = {r["seed"] for r in self.scope.runs(config_hash_=h)}
        for p in self.scope.proposals(kind="runs"):
            if p["status"] in ("pending", "approved", "executed"):
                for item in p["payload"].get("runs", []):
                    if item.get("config_hash") == h:
                        used.add(item["seed"])
        seeds, s = [], 0
        while len(seeds) < n:
            if s not in used:
                seeds.append(s)
            s += 1
        return seeds

    def _check_runs(self, change: dict[str, Any], n: int, new_reason: str | None,
                    reasons: list[str]) -> dict[str, Any] | None:
        space = self.facts.get("search_space", {})
        for k, v in change.items():
            msg = check_value(k, v, space)
            if msg:
                reasons.append(msg)
        if reasons:
            return None
        change = coerce_change(change, space)
        base_hp = self.facts.get("baseline_hyperparams", {})
        if all(base_hp.get(k) == v for k, v in change.items()) and change:
            reasons.append("change is identical to the baseline (a no-op); request baseline seeds instead")
            return None
        if self.facts.get("env_id") not in self.facts.get("env_allowlist", [self.facts.get("env_id")]):
            reasons.append(f"env {self.facts.get('env_id')} not on the allowlist")
        if not (1 <= n <= MAX_SEEDS_PER_PROPOSAL):
            reasons.append(f"seeds must be between 1 and {MAX_SEEDS_PER_PROPOSAL}")
        budget = self.facts.get("run_budget")
        committed = self.runs_committed()
        if budget is not None and committed + n > int(budget):
            reasons.append(f"run budget exceeded: {committed} committed + {n} new > {budget} approved")
        hp = dict(self.facts.get("baseline_hyperparams", {}))
        hp.update(change)
        if self.memory_mode == "on":
            for p in self.scope.proposals("rejected", kind="runs"):
                runs = p["payload"].get("runs") or []
                if runs and runs[0]["change"] == change and not new_reason:
                    reasons.append(f"the user rejected runs of this exact change in proposal {p['proposal_id']}; "
                                   "give new_reason to ask again")
            for lesson in self.scope.lessons():
                if lesson["kind"] == "avoid" and in_region(hp, lesson["region"]) and not new_reason:
                    reasons.append(f"config falls in an 'avoid' region from lesson:{lesson['lesson_id']} "
                                   f"({lesson['text']}); give new_reason to override")
        if reasons:
            return None
        seeds = self._seeds_for(change, n)
        cfg = build_config(self.facts, change, 0)
        return {"change": change, "config_hash": config_hash(cfg), "seeds": seeds}

    # -- main check ----------------------------------------------------------------------
    def check(self, d: Decision) -> GateResult:
        reasons: list[str] = []
        norm: dict[str, Any] = {}
        if d.action not in ACTIONS:
            return GateResult(False, [f"unknown action '{d.action}'; allowed: {', '.join(ACTIONS)}"])
        if d.action in NEEDS_EVIDENCE and not d.evidence:
            reasons.append(f"{d.action} must cite evidence references")
        reasons += self.evidence_problems(d.evidence)
        args = d.arguments or {}

        if d.action == "inspect_run":
            try:
                self.scope.run_by_name(str(args.get("run", "")))
            except (NotFound, IsolationError) as e:
                reasons.append(str(e))
        elif d.action == "search_knowledge":
            if not str(args.get("query", "")).strip():
                reasons.append("query is empty")
        elif d.action == "open_hypothesis":
            change = args.get("change") or {}
            if not isinstance(change, dict) or not change:
                reasons.append("change must be a non-empty {param: value} mapping")
            else:
                space = self.facts.get("search_space", {})
                for k, v in change.items():
                    msg = check_value(k, v, space)
                    if msg:
                        reasons.append(msg)
                if not reasons:
                    norm["change"] = coerce_change(change, space)
                    base_hp = self.facts.get("baseline_hyperparams", {})
                    if all(base_hp.get(k) == v for k, v in norm["change"].items()):
                        reasons.append("change is identical to the baseline (a no-op)")
            if not str(args.get("statement", "")).strip():
                reasons.append("statement is empty")
        elif d.action == "propose_runs":
            change = args.get("change") or {}
            if not isinstance(change, dict):
                reasons.append("change must be a mapping")
            else:
                hyp = args.get("hypothesis")
                if hyp:
                    try:
                        h = self.scope.hypothesis(str(hyp))
                        if h["status"] != "open":
                            reasons.append(f"hypothesis {hyp} is {h['status']}, not open")
                    except NotFound as e:
                        reasons.append(str(e))
                r = self._check_runs(change, int(args.get("seeds", self.facts.get("min_seeds", 3))),
                                     args.get("new_reason"), reasons)
                if r:
                    norm.update(r, hypothesis=hyp)
        elif d.action == "request_seeds":
            gid = str(args.get("group", ""))
            g = next((g for g in self.analysis["groups"] if gid and g["group"].startswith(gid)), None)
            if g is None:
                reasons.append(f"unknown config group '{gid}'")
            elif any(p["status"] in ("pending", "approved") and
                     any(item.get("config_hash") == g["group"] for item in p["payload"].get("runs", []))
                     for p in self.scope.proposals(kind="runs")):
                reasons.append(f"runs of group {gid} are already proposed and waiting for approval or launch; "
                               "do not ask again, use end_cycle and wait")
            elif self.memory_mode == "on" and (rej := next(
                    (p for p in self.scope.proposals("rejected", kind="runs")
                     if any(item.get("config_hash") == g["group"] for item in p["payload"].get("runs", []))), None)):
                reasons.append(f"the user rejected runs of group {gid} in proposal {rej['proposal_id']}; "
                               "more seeds of it are the same request. Propose a different change or end the cycle")
            else:
                r = self._check_runs(dict(g["changes"]), int(args.get("seeds", 2)), "more seeds of a tested config",
                                     reasons)
                if r:
                    hyp = next((h["label"] for h in self.scope.hypotheses("open")
                                if h["change"] == r["change"]), None)
                    norm.update(r, hypothesis=hyp)
        elif d.action == "resolve_hypothesis":
            label = str(args.get("hypothesis", ""))
            outcome = args.get("outcome")
            if outcome not in ("supported", "refuted", "inconclusive"):
                reasons.append("outcome must be supported, refuted or inconclusive")
            try:
                h = self.scope.hypothesis(label)
                if h["status"] != "open":
                    reasons.append(f"{label} is already {h['status']}")
                if any(p["status"] == "pending" and p["payload"].get("hypothesis") == label
                       for p in self.scope.proposals(kind="resolve_hypothesis")):
                    reasons.append(f"a resolution of {label} is already waiting for the user's approval; "
                                   "do not propose it again, use end_cycle")
                g = next((g for g in self.analysis["groups"] if g["changes"] == h["change"]), None)
                if g is None or g.get("vs_baseline") is None:
                    reasons.append(f"no completed runs of {label}'s change compared with the baseline yet")
                else:
                    verdict = g["vs_baseline"]["verdict"]
                    expected = {"better": "supported", "worse": "refuted"}.get(verdict)
                    if outcome in ("supported", "refuted") and outcome != expected:
                        reasons.append(f"outcome '{outcome}' contradicts the deterministic verdict '{verdict}' "
                                       f"(diff CI {g['vs_baseline']['diff_ci95']})")
                    if outcome == "inconclusive" and verdict == "insufficient_seeds":
                        reasons.append("not enough seeds to call it inconclusive; request more seeds first")
                    norm.update(group=g["group"], verdict=verdict, vs_baseline=g["vs_baseline"])
            except NotFound as e:
                reasons.append(str(e))
        elif d.action == "ask_user":
            if not str(args.get("question", "")).strip():
                reasons.append("question is empty")
        return GateResult(not reasons, reasons, norm)


EVIDENCE_RX = re.compile(r"^(run|diag|hyp|lesson|fact|group|kb):.+")
