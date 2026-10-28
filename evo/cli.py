"""EVO command-line interface (Typer + Rich).

Typical session:
  evo onboard projects/lander/project.yaml
  evo stage lander cycle1          # demo helper: results of runs finished "overnight"
  evo cycle lander                 # review cycle; approve proposals when asked
  ... process exits; later ...
  evo status lander                # everything reloaded from evo.db
  evo cycle lander
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from evo import agent as agent_mod
from evo.memory.actions import ActionLog
from evo.memory.store import IsolationError, NotFound, Store
from evo.safety.faults import FaultInjector
from evo.settings import db_path, knowledge_dir, load_settings
from evo.tools import analyze as analyze_tool
from evo.tools import export as export_tool
from evo.tools import knowledge as kb_tool
from evo.tools import runner as runner_tool
from evo.util import jdumps

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="EVO - persistent game-AI experiment co-pilot")
con = Console()


def _store() -> Store:
    return Store(db_path())


def _scope(store: Store, project: str):
    try:
        return store.scope(project)
    except NotFound as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def _fmt(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return yaml.safe_dump(v, default_flow_style=True, sort_keys=False).strip()
    return str(v)


# ----------------------------------------------------------------------------------------
@app.command()
def init():
    """Create evo.db and index the knowledge notes."""
    s = _store()
    r = kb_tool.index_knowledge(s, knowledge_dir())
    con.print(f"evo.db ready at [bold]{s.db_path}[/bold]; knowledge docs: {r['total_docs']} "
              f"(re-indexed {len(r['indexed'])})")


@app.command()
def onboard(file: Path, yes: bool = typer.Option(False, "--yes", help="approve without prompting")):
    """Onboard a project from its project.yaml (once). Facts are shown for approval first."""
    s = _store()
    data = agent_mod.read_onboarding_file(file)
    if s.project_exists(str(data["project_id"])):
        con.print(f"[yellow]{data['project_id']} is already onboarded; facts live in evo.db. "
                  f"Use `evo correct` to change one.[/yellow]")
        raise typer.Exit(0)
    t = Table(title=f"Extracted facts for {data['project_id']} - confirm baseline")
    t.add_column("Fact")
    t.add_column("Value")
    for k in agent_mod.FACT_KEYS:
        if k in data:
            t.add_row(k, _fmt(data[k]))
    con.print(t)
    if not yes and not typer.confirm("Approve these facts as the project baseline?", default=False):
        con.print("Not onboarded. Nothing was saved.")
        raise typer.Exit(1)
    res = agent_mod.onboard(s, file, actor="user")
    kb_tool.index_knowledge(s, knowledge_dir())
    con.print(f"[green]Onboarded {res['project_id']}[/green] ({res.get('facts')} facts, version 1).")


@app.command()
def projects():
    """List onboarded projects."""
    s = _store()
    t = Table("Project", "Name", "Env", "Algo", "Folder")
    for p in s.list_projects():
        t.add_row(p["project_id"], p["name"], p["env_id"], p["algo"], p["root_dir"])
    con.print(t)


@app.command()
def status(project: str):
    """Current state: facts, runs by config, hypotheses, lessons, pending approvals, last recommendation."""
    s = _store()
    sc = _scope(s, project)
    settings = load_settings()
    facts = sc.facts()
    proj = sc.project()
    con.print(Panel(f"[bold]{proj['name']}[/bold] ({project}) - {proj['env_id']} / {proj['algo']}\n"
                    f"goal: {facts.get('goal')}\nmetric: mean eval return, last {facts.get('primary_metric', {}).get('k')} "
                    f"evals | target {facts.get('target_score')} | min seeds {facts.get('min_seeds')} | "
                    f"budget {facts.get('run_budget')} agent runs | deadline {facts.get('deadline')}",
                    title="EVO status"))
    an = analyze_tool.analyze_project(sc, settings)
    t = Table(title="Config groups (deterministic analysis)")
    for c in ("Config", "Seeds", "Mean", "CI95", "vs baseline", "Diff CI95", "Flags", "Pending"):
        t.add_column(c)
    for g in sorted(an["groups"], key=lambda g: (g["mean"] is None, -(g["mean"] or 0))):
        vs = g.get("vs_baseline") or {}
        t.add_row(g["label"] + (" [dim](baseline)[/dim]" if g["is_baseline"] else ""), str(g["n"]), str(g["mean"]),
                  str(g["ci95"] or ""), vs.get("verdict", ""), str(vs.get("diff_ci95") or ""),
                  ", ".join(f"{k}x{v}" for k, v in g["flags"].items()), str(g["pending"] or ""))
    con.print(t)
    hy = sc.hypotheses()
    if hy:
        t = Table(title="Hypotheses")
        for c in ("ID", "Status", "Change", "Statement", "Resolution"):
            t.add_column(c)
        for h in hy:
            t.add_row(h["label"], h["status"], _fmt(h["change"]), h["statement"],
                      (h.get("resolution") or {}).get("summary", ""))
        con.print(t)
    ls = sc.lessons()
    if ls:
        t = Table("ID", "Kind", "Lesson", "Approved by", title="Lessons")
        for l in ls:
            t.add_row(str(l["lesson_id"]), l["kind"], l["text"], str(l["approved_by"]))
        con.print(t)
    pend = sc.proposals("pending")
    if pend:
        t = Table("ID", "Kind", "Payload", "Rationale", "Evidence", title="[bold yellow]Waiting for your approval[/bold yellow]")
        for p in pend:
            t.add_row(str(p["proposal_id"]), p["kind"], _fmt(p["payload"]), p["rationale"], ", ".join(p["evidence"]))
        con.print(t)
        con.print("Approve with [bold]evo approve " + project + " <ID>[/bold] or reject with [bold]evo reject[/bold].")
    last = sc.latest_cycle()
    if last:
        rec = (last.get("summary") or {}).get("recommendation", "(cycle in progress)")
        con.print(Panel(f"cycle {last['number']} ({last['status']}, memory {last['memory_mode']}, clock {last['clock']})\n"
                        f"recommended next: {rec}", title="Last cycle"))


@app.command()
def cycle(project: str,
          memory: str = typer.Option("on", help="on | off (ablation: no history retrieval)"),
          provider: Optional[str] = typer.Option(None, help="openai_compat | offline (default from config)"),
          clock: Optional[str] = typer.Option(None, help="simulated review date YYYY-MM-DD"),
          no_prompt: bool = typer.Option(False, "--no-prompt", help="leave proposals pending instead of asking"),
          inject_fault: Optional[str] = typer.Option(None, help="e.g. run_experiment:after:1"),
          wait: bool = typer.Option(False, help="block until launched runs finish"),
          in_place: bool = typer.Option(False, "--in-place",
                                        help="memory off only: write the ablation into evo.db (eval harness copies)")):
    """Run one review cycle: ingest, analyse, decide, (ask to) act.

    With --memory off the cycle is an ablation: it runs on a scratch copy of evo.db, never asks for
    approvals or launches runs, and leaves the authoritative memory and reports untouched.
    """
    if memory not in ("on", "off"):
        raise typer.BadParameter("memory must be on or off")
    s = _store()
    _scope(s, project)
    settings = load_settings()
    faults = FaultInjector.parse(inject_fault) if inject_fault else FaultInjector.from_env()
    ablation = memory == "off"
    scratch = None
    if ablation and not in_place:
        import sqlite3
        import tempfile
        scratch = Path(tempfile.mkdtemp(prefix="evo-ablation-")) / "evo.db"
        dst = sqlite3.connect(scratch)
        s.conn.backup(dst)
        dst.close()
        s = Store(scratch)
        con.print(f"[dim]memory-off ablation on a scratch copy ({scratch}); evo.db is not changed[/dim]")
    log = ActionLog(s, faults=faults)
    approver = None if (no_prompt or ablation or not sys.stdin.isatty()) else _interactive_approver
    ag = agent_mod.Agent(s, project, settings, provider=provider, memory_mode=memory, approver=approver, log=log,
                         write_reports=not ablation)
    res = ag.run_cycle(clock=clock)
    for e in res["events"]:
        con.print(f"  {e}")
    if faults.fired:
        con.print(f"[magenta]injected faults fired: {faults.fired}[/magenta]")
    sm = res["summary"]
    con.print(Panel(f"{sm['recommendation']}\npending approvals: {sm['pending']}\nregister: {sm['register']}\n"
                    f"report: {sm['report']}", title=f"Cycle {res['cycle']} closed"))
    if wait:
        sc = s.scope(project)
        running = [r["name"] for r in sc.runs(status="running")]
        if running:
            con.print(f"waiting for {len(running)} run(s)...")
            runner_tool.wait_for_runs(sc, running)


def _interactive_approver(prop: dict):
    con.print(Panel(f"kind: {prop['kind']}\npayload: {_fmt(prop['payload'])}\nrationale: {prop['rationale']}\n"
                    f"evidence: {', '.join(prop['evidence'])}", title=f"Approval needed - proposal {prop['proposal_id']}"))
    ans = typer.prompt("Approve? [y]es / [n]o / [l]ater", default="l").strip().lower()
    if ans.startswith("y"):
        return ("approved", typer.prompt("Comment (optional)", default="", show_default=False) or None)
    if ans.startswith("n"):
        return ("rejected", typer.prompt("Why? (optional)", default="", show_default=False) or None)
    return None


@app.command()
def pending(project: str):
    """List proposals waiting for a decision."""
    s = _store()
    sc = _scope(s, project)
    for p in sc.proposals("pending"):
        con.print(f"[bold]{p['proposal_id']}[/bold] {p['kind']}: {_fmt(p['payload'])}\n   {p['rationale']}")


@app.command()
def approve(project: str, proposal_id: int, comment: Optional[str] = typer.Option(None),
            inject_fault: Optional[str] = typer.Option(None), wait: bool = typer.Option(False)):
    """Approve a proposal and execute it (idempotent: approving twice never duplicates runs)."""
    s = _store()
    sc = _scope(s, project)
    faults = FaultInjector.parse(inject_fault) if inject_fault else FaultInjector.from_env()
    try:
        res = agent_mod.decide_proposal(sc, ActionLog(s, faults=faults), load_settings(), proposal_id, "approved",
                                        comment=comment, actor="user", wait=wait)
    except (NotFound, IsolationError) as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    con.print_json(jdumps(res))


@app.command()
def reject(project: str, proposal_id: int, comment: Optional[str] = typer.Option(None)):
    """Reject a proposal (kept in history with your comment)."""
    s = _store()
    sc = _scope(s, project)
    res = agent_mod.decide_proposal(sc, ActionLog(s), load_settings(), proposal_id, "rejected", comment=comment,
                                    actor="user")
    con.print_json(jdumps(res))


@app.command()
def correct(project: str, key: str, value: str, reason: str = typer.Option(..., help="why the fact changes"),
            yes: bool = typer.Option(False, "--yes")):
    """Correct an approved project fact. The old version stays in history."""
    s = _store()
    sc = _scope(s, project)
    parsed = yaml.safe_load(value)
    old = sc.fact(key)
    pid = agent_mod.propose_fact_change(sc, key, parsed, reason)
    con.print(f"proposal {pid}: {key}: {_fmt(old)} -> {_fmt(parsed)}")
    if yes or (sys.stdin.isatty() and typer.confirm("Approve this correction now?", default=True)):
        con.print_json(jdumps(agent_mod.decide_proposal(sc, ActionLog(s), load_settings(), pid, "approved",
                                                       comment=reason, actor="user")))


@app.command("correct-metric")
def correct_metric(project: str, run: str, eval_file: Path, reason: str = typer.Option(...),
                   yes: bool = typer.Option(False, "--yes")):
    """Supply a corrected eval.csv for a run (e.g. re-scored with more episodes)."""
    s = _store()
    sc = _scope(s, project)
    pid = agent_mod.propose_metric_correction(sc, run, eval_file, reason)
    con.print(f"proposal {pid}: new metric version for {run} from {eval_file}")
    if yes or (sys.stdin.isatty() and typer.confirm("Approve this correction now?", default=True)):
        con.print_json(jdumps(agent_mod.decide_proposal(sc, ActionLog(s), load_settings(), pid, "approved",
                                                       comment=reason, actor="user")))


@app.command()
def history(project: str, fact: Optional[str] = typer.Option(None), run: Optional[str] = typer.Option(None)):
    """Show version history of a fact or of a run's metrics (current and superseded)."""
    s = _store()
    sc = _scope(s, project)
    if run:
        r = sc.run_by_name(run)
        t = Table("Version", "Status", "Source", "Reason", "Approved by", "Created", title=f"metrics of {run}")
        k = int(sc.fact("primary_metric", {}).get("k", 5))
        for m in sc.metric_rows(r["run_id"]):
            sm = analyze_tool.summarize_curve(m["eval"], k=k, target=sc.fact("target_score"),
                                              a=load_settings()["analysis"])
            t.add_row(str(m["version"]), m["status"], f"{Path(m['source_ref']).name} (score {sm.get('score')})",
                      str(m["reason"] or ""), str(m["approved_by"] or ""), m["created_at"])
        con.print(t)
        return
    t = Table("Fact", "Version", "Status", "Value", "Reason", "By", "Created", title="fact history")
    for f in sc.fact_rows(fact):
        t.add_row(f["key"], str(f["version"]), f["status"], _fmt(f["value"])[:80], str(f["reason"] or ""),
                  f"{f['proposed_by']}/{f['approved_by']}", f["created_at"])
    con.print(t)


@app.command()
def log(project: str, tail: int = typer.Option(25)):
    """Action log: tool calls, status, memory reads/writes, errors."""
    s = _store()
    sc = _scope(s, project)
    t = Table("#", "Cycle", "Step", "Tool", "Status", "Try", "Reads", "Writes", "Error", "At")
    for r in sc.action_log(tail):
        t.add_row(str(r["log_id"]), str(r["cycle_id"] or ""), str(r["step"] if r["step"] is not None else ""),
                  r["tool"], r["status"], str(r["attempt"]), str(len(r["memory_reads"])),
                  ", ".join(r["memory_writes"])[:60], (r["error"] or "")[:60], r["started_at"][11:19])
    con.print(t)


@app.command()
def decisions(project: str, cycle_no: Optional[int] = typer.Option(None, "--cycle")):
    """Planner decisions with rationale, evidence and gate result."""
    s = _store()
    sc = _scope(s, project)
    cid = None
    if cycle_no:
        cid = next((c["cycle_id"] for c in sc.cycles() if c["number"] == cycle_no), None)
    for d in sc.decisions(cycle_id=cid):
        ok = "[green]allowed[/green]" if d["gate"]["allowed"] else "[red]refused[/red] " + "; ".join(d["gate"]["reasons"])
        con.print(f"[bold]c{d['cycle_number']}.{d['step']}[/bold] {d['action']} {jdumps(d['arguments'])[:140]} - {ok}\n"
                  f"     {d['rationale']}\n     evidence: {', '.join(d['evidence'])}  [dim]({d['provider']}/{d['model']}, memory {d['memory_mode']})[/dim]")


@app.command()
def export(project: str):
    """Write the Excel review register (preserves your User columns)."""
    s = _store()
    sc = _scope(s, project)
    res = export_tool.export_register(sc, analyze_tool.analyze_project(sc, load_settings()))
    con.print(f"register: {res['path']} (user cells preserved: {res['user_cells_preserved']})")


@app.command()
def dashboard(project: str,
              out: Optional[Path] = typer.Option(None, help="output file (default: <project>/reports/dashboard.html)"),
              open_browser: bool = typer.Option(False, "--open", help="open the page in your browser")):
    """Write a read-only HTML dashboard of the project's memory (one self-contained file)."""
    from evo.tools import dashboard as dash_tool
    s = _store()
    sc = _scope(s, project)
    path = dash_tool.write_dashboard(sc, load_settings(), out)
    con.print(f"dashboard: {path}")
    if open_browser:
        import webbrowser
        webbrowser.open(path.resolve().as_uri())


@app.command()
def serve(project: Optional[str] = typer.Argument(None, help="project to open first"),
          port: int = typer.Option(8765, help="local port"),
          no_browser: bool = typer.Option(False, "--no-browser", help="don't open a browser tab")):
    """Start the EVO web app on http://127.0.0.1:<port> (run cycles, approve, browse memory)."""
    from evo.tools import webapp
    webapp.serve(port=port, open_browser=not no_browser, project=project)


@app.command("kb-search")
def kb_search(query: str):
    """Search the knowledge notes directly."""
    s = _store()
    kb_tool.index_knowledge(s, knowledge_dir())
    for r in kb_tool.search(s, query):
        con.print(f"[bold]{r['ref']}[/bold] {r['title']} - {r['section']}\n  {r['snippet']}\n  [dim]{r['source_url']}[/dim]")


@app.command("lesson-reject")
def lesson_reject(project: str, lesson_id: int):
    """Reject an automatic lesson so it no longer steers proposals."""
    s = _store()
    sc = _scope(s, project)
    sc.set_lesson_status(lesson_id, "rejected")
    sc.record_approval(subject_type="lesson", subject_id=str(lesson_id), decision="rejected", comment=None, actor="user")
    con.print(f"lesson {lesson_id} rejected")


@app.command()
def stage(project: str, fixture_set: str):
    """Demo helper: copy prepared real runs (fixtures/<set>) into the project's runs/ folder."""
    s = _store()
    sc = _scope(s, project)
    src = sc.root_dir / "fixtures" / fixture_set
    if not src.exists():
        con.print(f"[red]no fixture set {src}[/red]")
        raise typer.Exit(1)
    n = 0
    for d in sorted(src.iterdir()):
        if d.is_dir():
            dst = sc.root_dir / "runs" / d.name
            if not dst.exists():
                shutil.copytree(d, dst, ignore=shutil.ignore_patterns("ckpt"))
                n += 1
    con.print(f"staged {n} run folder(s) from {fixture_set}")


@app.command()
def wait(project: str, timeout: int = typer.Option(3600)):
    """Wait for this project's background runs to finish."""
    s = _store()
    sc = _scope(s, project)
    running = [r["name"] for r in sc.runs(status="running")]
    con.print(f"waiting for {running}")
    runner_tool.wait_for_runs(sc, running, timeout_s=timeout)
    con.print("done")


@app.command("eval")
def eval_cmd(provider: str = typer.Option("offline"), out: Path = typer.Option(Path("eval/results"))):
    """Run the 10-scenario evaluation + memory on/off comparison in a scratch workspace."""
    from eval.harness import main as harness_main
    harness_main(provider=provider, out_dir=out)


if __name__ == "__main__":
    app()
