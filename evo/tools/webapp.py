"""EVO web app: `evo serve` runs a local web server (127.0.0.1 only) with a browser UI.

Everything the CLI does is available from the browser: onboard a project, stage demo
runs, run a review cycle with the LLM planner or the offline rules (progress streams
live), approve or reject proposals, correct facts and scores, reject lessons, and run a
memory on/off comparison as a dry run on scratch copies of evo.db.

Design notes
- Standard library only (http.server); the page is one self-contained HTML file.
- Every request opens its own Store; SQLite WAL lets reads run while a cycle writes.
- One background job at a time (a cycle or a comparison). Actions on a project are
  refused while a real cycle for it is running, so the gate never sees half a decision.
- POST requests need the per-server token embedded in the page and a localhost Host
  header, so another website cannot drive the app through your browser.
- Same rules as the CLI: proposals only ever execute after you approve them.
"""
from __future__ import annotations

import copy
import json
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from evo import agent as agent_mod
from evo.memory.actions import ActionLog
from evo.memory.store import IsolationError, NotFound, Store
from evo.settings import ROOT, db_path, knowledge_dir, load_settings
from evo.tools import dashboard as dash_tool
from evo.tools import knowledge as kb_tool
from evo.util import now_iso

HTML_FILE = Path(__file__).with_name("webapp.html")
FIXTURE_SKIP = {"bank", "corrections"}


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ---------------------------------------------------------------------------------------
# settings with in-app overrides (planner model, runner mode); config.yaml is never edited


class App:
    def __init__(self, db: Path | None = None):
        self.db = Path(db) if db else db_path()
        self.token = secrets.token_urlsafe(24)
        self.overrides: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.jobs: dict[str, Job] = {}

    def settings(self) -> dict[str, Any]:
        s = copy.deepcopy(load_settings(refresh=True))
        if self.overrides.get("model"):
            s["llm"]["model"] = self.overrides["model"]
        if self.overrides.get("runner_mode"):
            s["runner"]["mode"] = self.overrides["runner_mode"]
        return s

    def store(self) -> Store:
        return Store(self.db)

    def settings_view(self) -> dict[str, Any]:
        import os
        s = self.settings()
        llm = s["llm"]
        host = urlparse(llm.get("base_url") or "").netloc
        return {"provider": llm.get("provider"), "model": llm.get("model"), "host": host,
                "key_present": bool(os.environ.get(llm.get("api_key_env", "EVO_LLM_API_KEY"))) or
                ("localhost" in host or "127.0.0.1" in host),
                "runner_mode": s["runner"]["mode"], "max_steps": s["agent"]["max_steps_per_cycle"],
                "db": str(self.db), "overrides": self.overrides}

    def busy_with(self, project: str) -> bool:
        j = self.job
        return bool(j and j.status == "running" and j.project == project and not j.dry)


# ---------------------------------------------------------------------------------------
# jobs: a review cycle (real or dry) or a memory on/off comparison, in a background thread


@dataclass
class Leg:
    name: str                 # "real" | "on" | "off"
    memory: str
    db: Path
    dry: bool
    start_number: int = 0
    events: list[str] = field(default_factory=list)
    status: str = "waiting"   # waiting | running | done | error
    result: dict | None = None
    error: str | None = None
    agent: Any = None


@dataclass
class Job:
    id: str
    project: str
    kind: str                 # cycle | compare
    provider: str
    model: str | None
    dry: bool
    legs: list[Leg]
    status: str = "running"
    started: float = field(default_factory=time.time)
    ended: float | None = None
    scratch: Path | None = None
    task: dict | None = None


def _copy_db(src: Path, dst: Path) -> None:
    a = sqlite3.connect(src)
    b = sqlite3.connect(dst)
    a.backup(b)
    b.close()
    a.close()


def clean_task(sc, raw: Any) -> dict | None:
    """Validate a task typed in the app: optional note, optional {param: value} change, optional seed count."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ApiError(400, "bad task")
    out: dict[str, Any] = {}
    text = str(raw.get("text") or "").strip()
    if text:
        out["text"] = text[:600]
    ch = raw.get("change")
    if ch:
        if not isinstance(ch, dict) or len(ch) != 1:
            raise ApiError(400, "Change one setting at a time.")
        space = sc.fact("search_space") or {}
        base = sc.fact("baseline_hyperparams") or {}
        (k, v), = ch.items()
        if k not in base and k not in space:
            raise ApiError(400, f"'{k}' is not a setting of this project.")
        if isinstance(v, str):
            try:
                v = yaml.safe_load(v)
            except yaml.YAMLError:
                pass
        if v == "" or v is None:
            raise ApiError(400, "Give the new value.")
        b = base.get(k)
        if isinstance(b, (int, float)) and not isinstance(b, bool) and (
                not isinstance(v, (int, float)) or isinstance(v, bool)):
            raise ApiError(400, f"'{k}' takes a number, not '{v}'.")
        out["change"] = {k: v}
        if raw.get("seeds") not in (None, ""):
            try:
                n = int(raw["seeds"])
            except (TypeError, ValueError):
                raise ApiError(400, "seeds must be a number")
            if not 1 <= n <= 10:
                raise ApiError(400, "seeds must be between 1 and 10")
            out["seeds"] = n
    return out or None


def start_job(app: App, project: str, *, kind: str, provider: str, memory: str = "on", clock: str | None = None,
              task: Any = None) -> Job:
    with app.lock:
        if app.job and app.job.status == "running":
            raise ApiError(409, "EVO is already working on a cycle. Wait for it to finish.")
        s = app.store()
        try:
            user_request = clean_task(s.scope(project), task)
            start_no = max((c["number"] for c in s.scope(project).cycles()), default=0)
            last = s.scope(project).latest_cycle()
        finally:
            s.close()
        settings = app.settings()
        if provider not in ("offline", "openai_compat"):
            raise ApiError(400, "provider must be offline or openai_compat")
        legs: list[Leg] = []
        scratch = None
        if kind == "cycle" and memory == "on":
            legs.append(Leg("real", "on", app.db, dry=False, start_number=start_no))
        else:
            # memory off (or the comparison) never touches the real evo.db: scratch copies only
            scratch = Path(tempfile.mkdtemp(prefix="evo-web-"))
            modes = ["on", "off"] if kind == "compare" else [memory]
            for m in modes:
                d = scratch / m
                d.mkdir()
                _copy_db(app.db, d / "evo.db")
                legs.append(Leg(m, m, d / "evo.db", dry=True, start_number=start_no))
        if last and last["status"] == "open" and not legs[0].dry:
            legs[0].start_number = start_no - 1  # resuming an interrupted cycle
        job = Job(id=secrets.token_hex(6), project=project, kind=kind, provider=provider, task=user_request,
                  model=settings["llm"]["model"] if provider == "openai_compat" else None,
                  dry=all(l.dry for l in legs), legs=legs, scratch=scratch)
        app.job = job
        app.jobs[job.id] = job

    def work():
        for leg in job.legs:
            leg.status = "running"
            st = Store(leg.db)
            try:
                ag = agent_mod.Agent(st, project, settings, provider=provider, memory_mode=leg.memory, approver=None,
                                     log=ActionLog(st), write_reports=not leg.dry)
                ag.events = leg.events          # share the list: the UI sees events as they happen
                leg.agent = ag
                res = ag.run_cycle(clock=clock, user_request=user_request)
                leg.result = {"cycle": res["cycle"], "summary": res["summary"]}
                leg.status = "done"
            except Exception as e:  # noqa: BLE001 - reported to the UI, never swallowed silently
                leg.status = "error"
                leg.error = f"{type(e).__name__}: {e}"
                leg.events.append("error: " + leg.error)
                traceback.print_exc()
            finally:
                st.close()
        job.status = "error" if any(l.status == "error" for l in job.legs) else "done"
        job.ended = time.time()

    threading.Thread(target=work, daemon=True, name=f"evo-job-{job.id}").start()
    return job


def job_view(app: App, job: Job) -> dict[str, Any]:
    legs = []
    for leg in job.legs:
        decisions: list[dict] = []
        cycle_no = None
        if leg.status != "waiting":
            try:
                st = Store(leg.db)
                try:
                    sc = st.scope(job.project)
                    cyc = [c for c in sc.cycles() if c["number"] > leg.start_number]
                    if cyc:
                        cycle_no = cyc[0]["number"]
                        cid = cyc[0]["cycle_id"]
                        decisions = [_decision_view(d) for d in sc.decisions(cycle_id=cid)]
                finally:
                    st.close()
            except Exception:  # noqa: BLE001 - db busy for a moment; next poll catches up
                pass
        legs.append({"name": leg.name, "memory": leg.memory, "dry": leg.dry, "status": leg.status,
                     "events": list(leg.events), "result": leg.result, "error": leg.error, "cycle": cycle_no,
                     "decisions": decisions})
    return {"id": job.id, "project": job.project, "kind": job.kind, "provider": job.provider, "model": job.model,
            "task": job.task,
            "dry": job.dry, "status": job.status, "elapsed": round((job.ended or time.time()) - job.started, 1),
            "legs": legs}


def _decision_view(d: dict) -> dict[str, Any]:
    g = d.get("gate") or {}
    return {"step": d["step"], "action": d["action"], "arguments": d.get("arguments") or {},
            "rationale": d.get("rationale") or "", "evidence": d.get("evidence") or [],
            "allowed": bool(g.get("allowed")), "reasons": g.get("reasons") or [],
            "provider": d.get("provider"), "model": d.get("model"), "latency_ms": d.get("latency_ms")}


# ---------------------------------------------------------------------------------------
# project state for the UI


def _runs_dir(sc) -> Path:
    return Path(sc.project()["root_dir"]) / "runs"


def disk_view(sc) -> dict[str, Any]:
    root = Path(sc.project()["root_dir"])
    runs_dir = root / "runs"
    on_disk = sorted(p.name for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.exists() else []
    known = {r["name"] for r in sc.runs()}
    fx = root / "fixtures"
    sets = []
    if fx.exists():
        for d in sorted(p for p in fx.iterdir() if p.is_dir() and p.name not in FIXTURE_SKIP):
            names = sorted(p.name for p in d.iterdir() if p.is_dir())
            sets.append({"name": d.name, "runs": names, "staged": bool(names) and all((runs_dir / n).exists() for n in names)})
    corr = []
    cdir = fx / "corrections"
    proposed = {}
    for p in sc.proposals(kind="metric_correction"):
        proposed[(p["payload"].get("run"), Path(str(p["payload"].get("file", ""))).name)] = p["status"]
    if cdir.exists():
        for f in sorted(cdir.glob("*.csv")):
            run = f.name.split(".")[0]
            corr.append({"file": f.name, "run": run, "run_known": run in known,
                         "status": proposed.get((run, f.name))})
    return {"new_folders": [n for n in on_disk if n not in known], "fixture_sets": sets, "corrections": corr}


def next_steps(sc, data: dict, disk: dict, busy: bool) -> list[dict[str, Any]]:
    """Plain-language suggestions for what to do next, most important first."""
    out: list[dict[str, Any]] = []
    if busy:
        return [{"id": "busy", "title": "EVO is running a review cycle",
                 "text": "Watch it work in the agent panel. Each step appears as soon as the planner decides it.",
                 "action": "open-console"}]
    pend = [p for p in data["proposals"] if p["status"] == "pending"]
    if pend:
        out.append({"id": "review", "title": f"{len(pend)} proposal{'s' if len(pend) > 1 else ''} waiting for your decision",
                    "text": "EVO never starts training or closes a hypothesis on its own. Approve or reject in the Inbox.",
                    "action": "goto:inbox"})
    if disk["new_folders"]:
        n = len(disk["new_folders"])
        out.append({"id": "cycle-new", "title": f"{n} new run folder{'s' if n > 1 else ''} to read",
                    "text": "Run a review cycle: EVO reads the new results, analyses them and decides what to do next.",
                    "action": "cycle"})
    running = [r for r in data["curves"] if r["status"] == "running"]
    if running:
        out.append({"id": "running", "title": f"{len(running)} run{'s' if len(running) > 1 else ''} training in the background",
                    "text": "They were launched after your approval. The next cycle reads whatever has finished.",
                    "action": "cycle" if not out else None})
    if not data["curves"] and not disk["new_folders"]:
        nxt = next((s for s in disk["fixture_sets"] if not s["staged"]), None)
        out.append({"id": "add-runs", "title": "Add some training results",
                    "text": ("Copy the prepared real runs into the project (demo set "
                             f"'{nxt['name']}'), or put your own run folders in {_runs_dir(sc)}.") if nxt else
                            f"Put run folders in {_runs_dir(sc)}.",
                    "action": f"stage:{nxt['name']}" if nxt else None})
    if not out:
        nxt = next((s for s in disk["fixture_sets"] if not s["staged"]), None)
        if nxt:
            out.append({"id": "stage", "title": f"Continue the demo: add the '{nxt['name']}' runs",
                        "text": "These are new results 'finished overnight'. Then run a cycle so EVO reads them.",
                        "action": f"stage:{nxt['name']}"})
        out.append({"id": "cycle", "title": "Run another review cycle",
                    "text": "Nothing is waiting. A cycle re-checks the results and follows up open hypotheses.",
                    "action": "cycle"})
    return out


def demo_steps(sc, data: dict, disk: dict, compared: bool = False) -> list[dict[str, Any]]:
    sets = {s["name"]: s for s in disk["fixture_sets"]}
    ncyc = len(data["cycles"])
    props = data["proposals"]
    decided = lambda c: any(p["cycle"] and p["status"] != "pending" for p in props if _cyc_no(data, p["cycle"]) == c)  # noqa: E731
    corr_done = [c for c in disk["corrections"] if c["status"]]
    budget_versions = next((len(f["versions"]) for f in data["fact_history"] if f["key"] == "run_budget"), 1)
    steps = [
        ("onboard", "Onboard the project", "Facts from project.yaml are approved once and stored in evo.db.", True, None),
        ("stage1", "Add the first results", "5 real runs: 3 baseline seeds, one lucky learning_rate=0.003 seed, one ent_coef=0 seed.",
         sets.get("cycle1", {}).get("staged", False), "stage:cycle1"),
        ("cycle1", "Run cycle 1", "EVO analyses the runs. It should distrust the single lucky seed and ask for more seeds.",
         ncyc >= 1, "cycle"),
        ("decide1", "Decide on EVO's proposal", "Approve (or reject) the runs it asked for. Nothing launches without you.",
         decided(1), "goto:inbox"),
        ("stage2", "Add the second batch", "A manual run whose log contains a planted instruction, and an interrupted run.",
         sets.get("cycle2", {}).get("staged", False), "stage:cycle2"),
        ("cycle2", "Run cycle 2", "EVO follows up H1 from memory, treats the planted text as data, and excludes the incomplete run.",
         ncyc >= 2, "cycle"),
        ("correct", "Correct the baseline scores", "The baseline was scored with 3 eval episodes; re-scored files use 20. Old versions are kept.",
         len(corr_done) >= len(disk["corrections"]) > 0, "goto:setup#corrections"),
        ("budget", "Cut the run budget to 12", "A fact correction: the old value stays in history and the gate uses the new one.",
         budget_versions > 1, "goto:setup#facts"),
        ("cycle3", "Run cycle 3", "EVO ranks with the corrected scores and the new budget.", ncyc >= 3, "cycle"),
        ("compare", "Compare memory on vs off", "A dry run on scratch copies: same inputs, one planner with memory, one without.",
         compared, "compare"),
    ]
    return [{"id": i, "title": t, "text": x, "done": bool(d), "action": a} for i, t, x, d, a in steps]


def _cyc_no(data: dict, cycle_id: int | None) -> int | None:
    for c in data["cycles"]:
        if c.get("cycle_id") == cycle_id:
            return c["number"]
    return None


def project_state(app: App, project: str) -> dict[str, Any]:
    st = app.store()
    try:
        sc = st.scope(project)
        settings = app.settings()
        data = dash_tool.collect(sc, settings)
        for c, raw in zip(data["cycles"], sc.cycles()):
            c["cycle_id"] = raw["cycle_id"]
            c["started_at"] = raw.get("started_at")
            c["ended_at"] = raw.get("ended_at")
            c["task"] = (raw.get("summary") or {}).get("task")
            ing = next((r.get("result") or {} for r in data["log"] if r.get("tool") == "ingest_runs"
                        and r.get("cycle_id") == raw["cycle_id"] and r.get("status") == "succeeded"
                        and isinstance(r.get("result"), dict)), {})
            c["read"] = {"new": [x.get("run") for x in ing.get("new") or [] if isinstance(x, dict)],
                         "updated": [x.get("run") for x in ing.get("updated") or [] if isinstance(x, dict)],
                         "findings": [{"kind": d["kind"], "run": d.get("run_name")}
                                      for d in sc.diagnoses(cycle_id=raw["cycle_id"], include_superseded=True)]}
        disk = disk_view(sc)
        busy = app.busy_with(project)
        data["disk"] = disk
        data["next"] = next_steps(sc, data, disk, busy)
        compared = any(j.kind == "compare" and j.status == "done" and j.project == project for j in app.jobs.values())
        data["demo"] = demo_steps(sc, data, disk, compared)
        data["fact_keys"] = list(agent_mod.FACT_KEYS)
        data["facts_current"] = {k: v for k, v in sc.facts().items()}
        data["settings"] = app.settings_view()
        data["job"] = job_view(app, app.job) if app.job and app.job.project == project else None
        data["busy"] = busy
        data["version"] = version_token(st, project, sc)
        return data
    finally:
        st.close()


def version_token(st: Store, project: str, sc=None) -> str:
    """Cheap fingerprint of everything the UI shows; the page polls it and reloads on change."""
    parts = []
    for t in ("runs", "run_metrics", "decisions", "proposals", "approvals", "hypotheses", "lessons", "project_facts",
              "cycles", "action_log", "diagnoses"):
        try:
            r = st.conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {t} WHERE project_id=?", (project,)).fetchone()
            parts.append(f"{r[0]}.{r[1]}")
        except sqlite3.OperationalError:
            parts.append("-")
    r = st.conn.execute("SELECT GROUP_CONCAT(status) FROM proposals WHERE project_id=?", (project,)).fetchone()
    parts.append(str(r[0]))
    r = st.conn.execute("SELECT GROUP_CONCAT(status) FROM runs WHERE project_id=?", (project,)).fetchone()
    parts.append(str(r[0]))
    if sc is not None:
        rd = _runs_dir(sc)
        parts.append(str(len([p for p in rd.iterdir() if p.is_dir()]) if rd.exists() else 0))
    return "|".join(parts)


def onboarding_files(app: App) -> list[dict[str, Any]]:
    st = app.store()
    try:
        out = []
        for f in sorted((ROOT / "projects").glob("*/project.yaml")):
            try:
                data = agent_mod.read_onboarding_file(f)
            except Exception as e:  # noqa: BLE001
                out.append({"file": str(f.relative_to(ROOT)), "error": str(e)})
                continue
            pid = str(data.get("project_id"))
            out.append({"file": str(f.relative_to(ROOT)).replace("\\", "/"), "project_id": pid, "name": data.get("name"),
                        "env": data.get("env_id"), "algo": data.get("algo"), "goal": data.get("goal"),
                        "onboarded": st.project_exists(pid),
                        "facts": {k: data[k] for k in agent_mod.FACT_KEYS if k in data}})
        return out
    finally:
        st.close()


# ---------------------------------------------------------------------------------------
# actions


def act(app: App, project: str | None, action: str, body: dict[str, Any]) -> dict[str, Any]:
    if action == "onboard":
        files = {o["file"]: o for o in onboarding_files(app)}
        f = body.get("file")
        if f not in files:
            raise ApiError(400, "unknown onboarding file")
        if files[f].get("onboarded"):
            raise ApiError(409, f"{files[f]['project_id']} is already onboarded")
        st = app.store()
        try:
            res = agent_mod.onboard(st, ROOT / f, actor="user")
            kb_tool.index_knowledge(st, knowledge_dir())
            return {"ok": True, "project": res["project_id"], "message": f"Onboarded {res['project_id']}."}
        finally:
            st.close()
    if action == "settings":
        m = (body.get("model") or "").strip()
        rm = body.get("runner_mode")
        if rm not in (None, "", "live", "fixture"):
            raise ApiError(400, "runner_mode must be live or fixture")
        if m:
            app.overrides["model"] = m
        elif "model" in body:
            app.overrides.pop("model", None)
        if rm:
            app.overrides["runner_mode"] = rm
        return {"ok": True, "settings": app.settings_view()}

    assert project is not None
    if action in ("cycle", "compare"):
        job = start_job(app, project, kind=action, provider=body.get("provider") or app.settings()["llm"]["provider"],
                        memory=body.get("memory", "on"), clock=body.get("clock") or None, task=body.get("task"))
        return {"ok": True, "job": job.id}
    if app.busy_with(project):
        raise ApiError(409, "A review cycle is running for this project. Try again when it finishes.")
    st = app.store()
    try:
        sc = st.scope(project)
        settings = app.settings()
        if action in ("approve", "reject"):
            pid = int(body["proposal"])
            comment = (body.get("comment") or "").strip() or None
            res = agent_mod.decide_proposal(sc, ActionLog(st), settings, pid,
                                            "approved" if action == "approve" else "rejected", comment=comment,
                                            actor="user")
            msg = {"approve": f"Approved proposal #{pid}.", "reject": f"Rejected proposal #{pid}. EVO will remember."}[action]
            if res.get("error"):
                msg = f"Approved #{pid}, but running it failed: {res['error']}"
            return {"ok": not res.get("error"), "message": msg, "result": res}
        if action == "stage":
            name = body.get("set")
            sets = {s["name"] for s in disk_view(sc)["fixture_sets"]}
            if name not in sets:
                raise ApiError(400, "unknown demo set")
            src = Path(sc.project()["root_dir"]) / "fixtures" / name
            n = 0
            for d in sorted(src.iterdir()):
                dst = _runs_dir(sc) / d.name
                if d.is_dir() and not dst.exists():
                    shutil.copytree(d, dst, ignore=shutil.ignore_patterns("ckpt"))
                    n += 1
            return {"ok": True, "message": f"Added {n} run folder(s) from '{name}'. Run a cycle so EVO reads them."}
        if action == "correct-fact":
            key = body.get("key")
            reason = (body.get("reason") or "").strip()
            if not reason:
                raise ApiError(400, "Give a reason: it is stored with the new version.")
            try:
                value = yaml.safe_load(str(body.get("value")))
                pid = agent_mod.propose_fact_change(sc, key, value, reason)
            except (ValueError, yaml.YAMLError) as e:
                raise ApiError(400, str(e))
            res = agent_mod.decide_proposal(sc, ActionLog(st), settings, pid, "approved", comment=reason, actor="user")
            return {"ok": True, "message": f"{key} is now {json.dumps(value)} (version history kept).", "result": res}
        if action == "correct-metric":
            fname = body.get("file")
            reason = (body.get("reason") or "").strip() or "re-scored evaluation"
            corr = {c["file"]: c for c in disk_view(sc)["corrections"]}
            if fname not in corr:
                raise ApiError(400, "unknown correction file")
            if not corr[fname]["run_known"]:
                raise ApiError(409, f"run {corr[fname]['run']} has not been read yet: run a cycle first")
            path = Path(sc.project()["root_dir"]) / "fixtures" / "corrections" / fname
            pid = agent_mod.propose_metric_correction(sc, corr[fname]["run"], path, reason)
            res = agent_mod.decide_proposal(sc, ActionLog(st), settings, pid, "approved", comment=reason, actor="user")
            return {"ok": True, "message": f"New score version for {corr[fname]['run']} is current; the old one is kept.",
                    "result": res}
        if action == "lesson-reject":
            lid = int(body["lesson"])
            sc.set_lesson_status(lid, "rejected")
            sc.record_approval(subject_type="lesson", subject_id=str(lid), decision="rejected",
                               comment=body.get("comment"), actor="user")
            return {"ok": True, "message": f"Lesson {lid} rejected: it no longer steers proposals."}
        if action == "kb-search":
            kb_tool.index_knowledge(st, knowledge_dir())
            return {"ok": True, "results": kb_tool.search(st, str(body.get("query") or ""))}
        if action == "run":
            name = body.get("run")
            from evo.tools.analyze import inspect_run
            return {"ok": True, "run": inspect_run(sc, name, settings)}
        raise ApiError(404, f"unknown action {action}")
    except (NotFound, IsolationError) as e:
        raise ApiError(404, str(e))
    except KeyError as e:
        raise ApiError(400, f"missing field {e}")
    finally:
        st.close()


# ---------------------------------------------------------------------------------------
# HTTP


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EVO"

        def log_message(self, fmt, *args):  # quiet: the terminal shows only errors
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            try:
                if not parts:
                    page = HTML_FILE.read_text(encoding="utf-8").replace("__EVO_TOKEN__", app.token)
                    return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                if parts == ["api", "projects"]:
                    st = app.store()
                    try:
                        projs = st.list_projects()
                    finally:
                        st.close()
                    return self._json(200, {"projects": projs, "onboarding": onboarding_files(app),
                                            "settings": app.settings_view()})
                if len(parts) == 3 and parts[:2] == ["api", "job"]:
                    job = app.jobs.get(parts[2])
                    if not job:
                        return self._json(404, {"error": "no such job"})
                    return self._json(200, job_view(app, job))
                if len(parts) >= 3 and parts[:2] == ["api", "p"]:
                    pid = parts[2]
                    if len(parts) == 3:
                        return self._json(200, project_state(app, pid))
                    if parts[3] == "version":
                        st = app.store()
                        try:
                            sc = st.scope(pid)
                            j = app.job
                            jt = f"{j.id}:{j.status}" if j else "-"
                            return self._json(200, {"version": version_token(st, pid, sc) + "|" + jt})
                        finally:
                            st.close()
                    if parts[3] == "run" and len(parts) == 5:
                        return self._json(200, act(app, pid, "run", {"run": parts[4]}))
                    if parts[3] == "kb":
                        q = parse_qs(u.query).get("q", [""])[0]
                        return self._json(200, act(app, pid, "kb-search", {"query": q}))
                return self._json(404, {"error": "not found"})
            except NotFound as e:
                return self._json(404, {"error": str(e)})
            except ApiError as e:
                return self._json(e.status, {"error": e.msg})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-EVO-Token") != app.token:
                return self._json(403, {"error": "forbidden: reload the page"})
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if parts[:1] != ["api"]:
                    return self._json(404, {"error": "not found"})
                if len(parts) == 2:
                    return self._json(200, act(app, None, parts[1], body))
                if len(parts) == 4 and parts[1] == "p":
                    return self._json(200, act(app, parts[2], parts[3], body))
                return self._json(404, {"error": "not found"})
            except ApiError as e:
                return self._json(e.status, {"error": e.msg})
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad JSON"})
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def make_server(app: App, port: int = 8765) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    return srv


def serve(port: int = 8765, open_browser: bool = True, project: str | None = None) -> None:
    app = App()
    srv = make_server(app, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/" + (f"#/{project}" if project else "")
    print(f"EVO web app on {url}  (database: {app.db})")
    print("Keep this window open while you use the app. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
