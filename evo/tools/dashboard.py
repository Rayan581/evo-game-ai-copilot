"""Read-only HTML dashboard of one project, generated from evo.db.

`evo dashboard <project>` writes a single self-contained HTML file (no server, no network,
no external fonts or scripts). It only reads memory; nothing in evo.db changes.

Sections: overview, how a cycle works, results (learning curves + config groups vs the
baseline), cycles and decisions (with the gate's verdict and the evidence cited),
memory (hypotheses, lessons, versioned facts, metric corrections), approvals, and
safety (diagnoses, untrusted text, tool-call log).
"""
from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Any

from evo.memory.store import ProjectScope
from evo.tools.analyze import analyze_project
from evo.util import now_iso

# Categorical slots in fixed order (validated: adjacent CVD dE >= 8 in both modes). The
# baseline is not a slot: it is the neutral reference line.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

VERDICT_TEXT = {"better": "better", "worse": "worse", "uncertain": "uncertain",
                "insufficient_seeds": "too few seeds", None: "-"}


def e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _fmt_change(ch: dict | None) -> str:
    if not ch:
        return "baseline"
    return ", ".join(f"{k}={v}" for k, v in ch.items())


def _fmt_num(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _score(points: list[dict], k: int) -> float | None:
    vals = [p["mean"] for p in points if p.get("mean") is not None]
    if not vals:
        return None
    tail = vals[-k:]
    return round(sum(tail) / len(tail), 2)


# ---------------------------------------------------------------------------------------
# data


def collect(scope: ProjectScope, settings: dict) -> dict[str, Any]:
    proj = scope.project()
    facts = scope.facts()
    k = int((facts.get("primary_metric") or {}).get("k", 5))
    an = analyze_project(scope, settings)            # read-only (no persist_cycle)
    runs = scope.runs()
    run_by_id = {r["run_id"]: r for r in runs}

    # stable colour per config group: order of the group's first run (colour follows the entity)
    first_seen: dict[str, int] = {}
    for r in sorted(runs, key=lambda r: r["run_id"]):
        first_seen.setdefault(r["config_hash"], r["run_id"])
    base = an.get("baseline_group")
    order = [g for g in sorted(first_seen, key=first_seen.get) if g != base]
    slot = {g: i for i, g in enumerate(order)}

    groups = []
    for g in an["groups"]:
        vs = g.get("vs_baseline") or {}
        groups.append({
            "group": g["group"], "label": g.get("label") or _fmt_change(g.get("changes")),
            "is_baseline": bool(g.get("is_baseline")), "n": g.get("n"), "mean": g.get("mean"),
            "ci95": g.get("ci95"), "pending": g.get("pending"), "runs": g.get("runs") or [],
            "flags": g.get("flags") or {}, "verdict": vs.get("verdict"), "diff_mean": vs.get("diff_mean"),
            "diff_ci95": vs.get("diff_ci95"), "slot": None if g.get("is_baseline") else slot.get(g["group"], 99),
        })

    curves = []
    corrections = []
    for r in runs:
        cur = scope.current_metrics(r["run_id"])
        pts = [{"x": p["step"], "y": p["mean"]} for p in (cur or {}).get("eval", []) if p.get("mean") is not None]
        curves.append({"run": r["name"], "group": r["config_hash"], "status": r["status"], "seed": r["seed"],
                       "by": r["launched_by"], "points": pts})
        versions = scope.metric_rows(r["run_id"])
        if len(versions) > 1:
            for old, new in zip(versions, versions[1:]):
                corrections.append({"run": r["name"], "from_v": old["version"], "to_v": new["version"],
                                    "from_score": _score(old.get("eval") or [], k),
                                    "to_score": _score(new.get("eval") or [], k),
                                    "status": new["status"], "reason": new.get("reason"),
                                    "approved_by": new.get("approved_by")})

    run_rows = {r["run"]: r for r in an.get("runs", [])}

    cycles = []
    all_dec = scope.decisions()
    for c in scope.cycles():
        ds = [d for d in all_dec if d["cycle_id"] == c["cycle_id"]]
        summ = c.get("summary") or {}
        cycles.append({
            "number": c["number"], "clock": c.get("clock"), "memory": c.get("memory_mode"), "status": c["status"],
            "planner": summ.get("planner") or {}, "ended_by": summ.get("ended_by"),
            "recommendation": summ.get("recommendation"), "pending": summ.get("pending") or [],
            "decisions": [{
                "step": d["step"], "action": d["action"], "arguments": d.get("arguments") or {},
                "rationale": d.get("rationale") or "", "evidence": d.get("evidence") or [],
                "allowed": bool((d.get("gate") or {}).get("allowed")),
                "reasons": (d.get("gate") or {}).get("reasons") or [],
                "provider": d.get("provider"), "model": d.get("model"), "latency_ms": d.get("latency_ms"),
                "brief": d.get("brief"),
            } for d in sorted(ds, key=lambda d: d["step"])],
        })

    approvals = scope.approvals()
    appr_for = {}
    for a in approvals:
        if a["subject_type"] == "proposal":
            appr_for.setdefault(str(a["subject_id"]), []).append(a)
    proposals = []
    agent_runs = sum(1 for r in runs if r["launched_by"] == "agent")
    reserved = 0
    for p in scope.proposals():
        pl = p.get("payload") or {}
        if p["kind"] == "runs":
            items = pl.get("runs") or []
            what = f"{_fmt_change(items[0]['change'] if items else {})} x {len(items)} seed(s)"
            if p["status"] in ("pending", "approved"):
                reserved += len(items)
        elif p["kind"] == "resolve_hypothesis":
            verdict = pl.get("verdict") or (pl.get("vs_baseline") or {}).get("verdict")
            what = f"close {pl.get('hypothesis')} as {pl.get('outcome')}" + (
                f" (analysis verdict: {VERDICT_TEXT.get(verdict, verdict)})" if verdict else "")
        elif p["kind"] == "metric_correction":
            what = f"replace the scores of {pl.get('run')} with {Path(str(pl.get('file', ''))).name}"
        elif p["kind"] == "fact_change":
            what = f"set {pl.get('key')} = {json.dumps(pl.get('value'))}"
        else:
            what = json.dumps(pl)[:120]
        proposals.append({"id": p["proposal_id"], "cycle": p.get("cycle_id"), "kind": p["kind"], "what": what,
                          "hypothesis": pl.get("hypothesis"), "status": p["status"],
                          "rationale": p.get("rationale"), "evidence": p.get("evidence") or [],
                          "approvals": appr_for.get(str(p["proposal_id"]), [])})

    fact_hist = []
    for key in sorted({f["key"] for f in scope.fact_rows()}):
        rows = scope.fact_rows(key)
        if len(rows) > 1:
            fact_hist.append({"key": key, "versions": [{"v": r["version"], "status": r["status"], "value": r["value"],
                                                        "reason": r.get("reason"), "by": r.get("approved_by"),
                                                        "at": r.get("approved_at")} for r in rows]})

    lessons = scope.lessons("current") + [l for l in _all_lessons(scope) if l["status"] != "current"]
    diags = scope.diagnoses(kinds=None)
    log = scope.action_log()
    status_counts = Counter(r["status"] for r in log)
    tool_counts = Counter(r["tool"] for r in log)
    hyps = scope.hypotheses()

    return {
        "generated_at": now_iso(),
        "project": {"id": proj["project_id"], "name": proj.get("name"), "env": proj.get("env_id"),
                    "algo": proj.get("algo"), "goal": facts.get("goal"), "owner": facts.get("owner"),
                    "deadline": facts.get("deadline"), "target": facts.get("target_score"),
                    "min_seeds": facts.get("min_seeds"), "budget": facts.get("run_budget"), "k": k,
                    "steps": facts.get("total_timesteps"), "search_space": facts.get("search_space") or {},
                    "baseline": facts.get("baseline_hyperparams") or {}},
        "stats": {"cycles": len(cycles), "runs": len(runs), "agent_runs": agent_runs, "reserved": reserved,
                  "open_hyps": sum(1 for h in hyps if h["status"] == "open"),
                  "waiting": sum(1 for p in proposals if p["status"] == "pending"),
                  "lessons": len([l for l in lessons if l["status"] == "current"]),
                  "corrections": len(corrections) + sum(len(f["versions"]) - 1 for f in fact_hist),
                  "decisions": len(all_dec), "refused": sum(1 for d in all_dec if not (d.get("gate") or {}).get("allowed")),
                  "tool_calls": len(log), "failed_attempts": status_counts.get("failed", 0)},
        "baseline_group": base,
        "groups": groups, "curves": curves, "run_rows": run_rows,
        "cycles": cycles, "hypotheses": hyps, "lessons": lessons, "fact_history": fact_hist,
        "corrections": corrections, "proposals": proposals, "approvals": approvals,
        "diagnoses": diags, "log": log, "status_counts": dict(status_counts), "tool_counts": dict(tool_counts),
        "run_by_id": {str(k_): v["name"] for k_, v in run_by_id.items()},
    }


def _diag_text(kind: str, det: dict) -> str:
    try:
        if kind == "incomplete":
            return (f"stopped at step {det.get('last_step')} of {det.get('target_steps')} ({det.get('evals')} evals), "
                    f"no DONE marker: excluded from scores")
        if kind == "collapse":
            c = det.get("collapse", det)
            return (f"smoothed score peaked at {c.get('smoothed_peak')} (step {c.get('peak_step')}) and fell to "
                    f"{c.get('smoothed_final')}: lost {round(100 * float(c.get('lost_frac', 0)))}% of the gain")
        if kind == "plateau":
            return f"score {det.get('score')} with a flat tail (slope {det.get('slope')} per 10k steps)"
    except (TypeError, ValueError):
        pass
    return json.dumps(det, default=str)[:220]


def _all_lessons(scope: ProjectScope) -> list[dict]:
    out = []
    for st in ("rejected", "superseded", "retired"):
        try:
            out += scope.lessons(st)
        except Exception:  # noqa: BLE001 - unknown status values simply return nothing
            pass
    return out


# ---------------------------------------------------------------------------------------
# html


def _ref_chip(ref: str, known: dict[str, set[str]]) -> str:
    kind, _, val = ref.partition(":")
    anchor = None
    if kind == "run" and val in known["runs"]:
        anchor = f"run-{val}"
    elif kind == "hyp" and val in known["hyps"]:
        anchor = f"hyp-{val}"
    elif kind == "group":
        g = next((x for x in known["groups"] if x.startswith(val) or val.startswith(x)), None)
        anchor = f"group-{g}" if g else None
    elif kind == "lesson":
        anchor = f"lesson-{val}"
    elif kind == "diag":
        anchor = f"diag-{val}"
    label = e(ref)
    if anchor:
        return f'<a class="chip" href="#{e(anchor)}">{label}</a>'
    return f'<span class="chip" title="knowledge note or reference">{label}</span>'


def _verdict_badge(v: str | None, is_baseline: bool = False) -> str:
    if is_baseline:
        return '<span class="badge b-ref">baseline</span>'
    cls = {"better": "b-good", "worse": "b-bad", "uncertain": "b-warn", "insufficient_seeds": "b-muted"}.get(v or "",
                                                                                                          "b-muted")
    return f'<span class="badge {cls}">{e(VERDICT_TEXT.get(v, v))}</span>'


def _flags(flags: dict) -> str:
    if not flags:
        return '<span class="muted">none</span>'
    return " ".join(f'<span class="flag">{e(k)}{" x" + e(v) if v and v != 1 else ""}</span>' for k, v in flags.items())


def _args(a: dict) -> str:
    if not a:
        return ""
    return e(", ".join(f"{k}={json.dumps(v) if isinstance(v, (dict, list)) else v}" for k, v in a.items()))


def render(data: dict[str, Any]) -> str:
    p = data["project"]
    s = data["stats"]
    known = {"runs": {c["run"] for c in data["curves"]}, "hyps": {h["label"] for h in data["hypotheses"]},
             "groups": {g["group"] for g in data["groups"]}}
    group_label = {g["group"]: g["label"] for g in data["groups"]}

    # -- overview ------------------------------------------------------------------------
    providers = sorted({f"{c['planner'].get('provider', '?')} / {c['planner'].get('model', '?')}"
                        for c in data["cycles"] if c["planner"]})
    budget_used = s["agent_runs"] + s["reserved"]
    overview = f"""
<section id="overview" class="hero">
  <p class="eyebrow">EVO project dashboard · {e(p['id'])}</p>
  <h1>{e(p['name'])}</h1>
  <p class="lede">{e(p['goal'])}</p>
  <dl class="facts">
    <div><dt>Environment</dt><dd>{e(p['env'])} · {e(p['algo'])}, {e(p['steps'])} steps</dd></div>
    <div><dt>Metric</dt><dd>mean eval return over the last {e(p['k'])} evals · target {e(p['target'])}</dd></div>
    <div><dt>Rules</dt><dd>at least {e(p['min_seeds'])} seeds before any verdict · budget {e(p['budget'])} agent runs</dd></div>
    <div><dt>Planner</dt><dd>{e('; '.join(providers) or 'no cycles yet')}</dd></div>
  </dl>
  <dl class="stats">
    <div><dt>Review cycles</dt><dd>{s['cycles']}</dd></div>
    <div><dt>Runs in memory</dt><dd>{s['runs']}<small>{s['agent_runs']} launched by EVO</small></dd></div>
    <div><dt>Run budget</dt><dd>{budget_used}<small>of {e(p['budget'])} used or reserved</small></dd></div>
    <div><dt>Open hypotheses</dt><dd>{s['open_hyps']}</dd></div>
    <div><dt>Waiting for you</dt><dd>{s['waiting']}<small>pending proposals</small></dd></div>
    <div><dt>Gate refusals</dt><dd>{s['refused']}<small>of {s['decisions']} decisions</small></dd></div>
  </dl>
</section>"""

    flow_steps = [
        ("Ingest", "Read new run folders: config, scores, logs. Validate and hash every file."),
        ("Analyse", "Plain code scores each run, groups seeds by config and compares with the baseline (bootstrap CI)."),
        ("Brief", "Build the planner's briefing from memory: facts, hypotheses, lessons, past decisions, rejections."),
        ("Decide", "The planner (LLM or offline rules) picks ONE action and cites evidence."),
        ("Gate", "Code checks the action: approved settings, budget, real evidence, no repeats. It can refuse."),
        ("Act", "Allowed actions run. New training runs wait for your approval."),
        ("Save", "Every step, tool call and memory read/write is written to evo.db. Up to 8 steps, then end."),
    ]
    flow = "<ol class='flow'>" + "".join(
        f"<li><span class='n'>{i}</span><b>{e(t)}</b><span>{e(d)}</span></li>" for i, (t, d) in enumerate(flow_steps, 1)
    ) + "</ol>"
    how = f"""
<section id="how">
  <h2>How a cycle works</h2>
  <p class="sub">The same loop runs every time you call <code>evo cycle</code>. The program then exits; the next cycle starts
  from evo.db alone, which is what "persistent memory" means here.</p>
  {flow}
</section>"""

    # -- results -------------------------------------------------------------------------
    rows = []
    for g in sorted(data["groups"], key=lambda g: (not g["is_baseline"], -(g["mean"] if g["mean"] is not None else -1e9))):
        ci = g["ci95"]
        ci_txt = f"[{_fmt_num(ci[0])}, {_fmt_num(ci[1])}]" if ci else "-"
        dci = g["diff_ci95"]
        diff = "" if g["is_baseline"] or g["diff_mean"] is None else (
            f"{_fmt_num(g['diff_mean'])}" + (f" <span class='muted'>[{_fmt_num(dci[0])}, {_fmt_num(dci[1])}]</span>" if dci else ""))
        sw = "" if g["is_baseline"] else f"<i class='sw' style='--c:var(--s{min(g['slot'], 8)})'></i>"
        sw = sw if not g["is_baseline"] else "<i class='sw ref'></i>"
        rows.append(f"""<tr id="group-{e(g['group'])}">
  <td>{sw}<b>{e(g['label'])}</b><br><span class="mono muted">group:{e(g['group'])}</span></td>
  <td class="num">{e(g['n'])}{f" <span class='muted'>+{e(g['pending'])} running</span>" if g['pending'] else ''}</td>
  <td class="num">{_fmt_num(g['mean'])}</td><td class="num">{ci_txt}</td>
  <td>{_verdict_badge(g['verdict'], g['is_baseline'])}</td><td class="num">{diff}</td>
  <td>{_flags(g['flags'])}</td></tr>""")
    group_table = f"""<div class="tablewrap"><table>
<thead><tr><th>Config</th><th class="num">Seeds</th><th class="num">Mean</th><th class="num">95% CI</th>
<th>vs baseline</th><th class="num">Difference [CI]</th><th>Flags</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""

    run_rows = []
    for c in sorted(data["curves"], key=lambda c: (c["group"] != data["baseline_group"], c["group"], c["seed"])):
        rr = data["run_rows"].get(c["run"], {})
        run_rows.append(f"""<tr id="run-{e(c['run'])}"><td class="mono">{e(c['run'])}</td>
<td>{e(group_label.get(c['group'], c['group']))}</td><td class="num">{e(c['seed'])}</td><td>{e(c['status'])}</td>
<td>{e(c['by'])}</td><td class="num">{_fmt_num(rr.get('score'))}</td><td>{_flags({f: 1 for f in (rr.get('flags') or [])})}</td></tr>""")
    runs_table = f"""<details class="more"><summary>All runs ({len(run_rows)})</summary><div class="tablewrap"><table>
<thead><tr><th>Run</th><th>Config</th><th class="num">Seed</th><th>Status</th><th>Launched by</th><th class="num">Score</th><th>Flags</th></tr></thead>
<tbody>{''.join(run_rows)}</tbody></table></div></details>"""

    results = f"""
<section id="results">
  <h2>Results</h2>
  <p class="sub">Every number here comes from the deterministic analysis, not from the planner. A config only counts as
  better or worse when the 95% confidence interval of its difference from the baseline excludes zero.</p>
  <figure class="chart">
    <figcaption><b>Learning curves</b><span>Eval return over training steps. "Config mean" averages the completed seeds of each
    config (faint lines are the individual seeds); "Each seed" draws every run, dashed if incomplete. Click a config to hide it.</span></figcaption>
    <div class="controls"><div class="seg" role="group" aria-label="View"><button type="button" data-mode="mean" aria-pressed="true">Config mean</button><button type="button" data-mode="seeds" aria-pressed="false">Each seed</button></div>
    <div class="legend" id="curve-legend" role="group" aria-label="Configs"></div></div>
    <div class="plot" id="curve-plot"></div>
  </figure>
  <figure class="chart">
    <figcaption><b>Config groups vs the baseline</b><span>Dot = mean score across seeds, bar = 95% CI.
    The grey line is the baseline mean; the dashed line is the target.</span></figcaption>
    <div class="plot" id="ci-plot"></div>
  </figure>
  {group_table}
  {runs_table}
</section>"""

    # -- cycles --------------------------------------------------------------------------
    cyc_html = []
    for c in data["cycles"]:
        items = []
        for d in c["decisions"]:
            gate = ('<span class="badge b-good">allowed</span>' if d["allowed"]
                    else '<span class="badge b-bad">refused by gate</span>')
            reasons = "".join(f"<li>{e(r)}</li>" for r in d["reasons"])
            ev = " ".join(_ref_chip(r, known) for r in d["evidence"]) or '<span class="muted">no evidence cited</span>'
            brief = d.get("brief")
            brief_html = ""
            if brief:
                mem = brief.get("memory") or {}
                counts = ", ".join(f"{k.replace('_', ' ')} {len(v)}" for k, v in mem.items()
                                   if isinstance(v, list) and v) or "nothing remembered yet"
                if (brief.get("cycle") or {}).get("memory") == "off":
                    counts = "memory off: history hidden"
                brief_html = (f"<details class='brief'><summary>What the planner saw ({e(counts)})</summary>"
                              f"<pre>{e(json.dumps(brief, indent=2, default=str))}</pre></details>")
            items.append(f"""<li class="dec {'refused' if not d['allowed'] else ''}">
  <div class="dec-head"><span class="step">{e(d['step'])}</span><code class="act">{e(d['action'])}</code>{gate}</div>
  {f'<div class="args">{_args(d["arguments"])}</div>' if d['arguments'] else ''}
  <p>{e(d['rationale'])}</p>
  {f'<ul class="reasons">{reasons}</ul>' if reasons else ''}
  <div class="evidence"><span class="lbl">Evidence</span>{ev}</div>
  {brief_html}
</li>""")
        pl = c["planner"]
        cyc_html.append(f"""<article class="cycle" id="cycle-{e(c['number'])}">
  <header><h3>Cycle {e(c['number'])}</h3>
  <p class="meta">review date {e(c['clock'])} · memory <b>{e(c['memory'])}</b> · planner {e(pl.get('provider', '?'))} / {e(pl.get('model', '?'))}
  · ended by {e({'budget': 'step limit'}.get(c['ended_by'], c['ended_by']) or c['status'])}</p></header>
  <ol class="decisions">{''.join(items) or '<li class="muted">no decisions recorded</li>'}</ol>
  <p class="rec"><span class="lbl">Outcome</span>{e(c['recommendation'] or '-')}</p>
</article>""")
    cycles = f"""
<section id="cycles">
  <h2>Cycles and decisions</h2>
  <p class="sub">Each step: the action the planner chose, its reasoning, the evidence it cited and what the policy gate
  decided. Refused steps change nothing. Open "What the planner saw" to read the exact briefing it got.</p>
  {''.join(cyc_html) or '<p class="muted">No cycles yet. Run <code>evo cycle ' + e(p['id']) + '</code>.</p>'}
</section>"""

    # -- memory --------------------------------------------------------------------------
    hyp_items = []
    for h in data["hypotheses"]:
        res = h.get("resolution") or {}
        ev = " ".join(_ref_chip(r, known) for r in (h.get("evidence") or []))
        st = {"open": "b-warn", "supported": "b-good", "refuted": "b-bad", "inconclusive": "b-muted"}.get(h["status"], "b-muted")
        hyp_items.append(f"""<li id="hyp-{e(h['label'])}"><div class="row"><b>{e(h['label'])}</b>
<span class="badge {st}">{e(h['status'])}</span><span class="muted">opened in cycle {e(h['opened_cycle'])}
{f"· closed in cycle {e(h['resolved_cycle'])}" if h.get('resolved_cycle') else ''}</span></div>
<p>{e(h['statement'])}</p><p class="mono muted">change: {e(_fmt_change(h.get('change')))}</p>
{f"<p class='muted'>resolution: {e(json.dumps(res, default=str))}</p>" if res else ''}
<div class="evidence"><span class="lbl">Evidence</span>{ev or '<span class="muted">none</span>'}</div></li>""")
    les_items = "".join(
        f"""<li id="lesson-{e(l['lesson_id'])}"><div class="row"><b>lesson:{e(l['lesson_id'])}</b>
<span class="badge {'b-bad' if l['kind'] == 'avoid' else 'b-good'}">{e(l['kind'])}</span>
<span class="badge b-muted">{e(l['status'])}</span></div><p>{e(l['text'])}</p>
<p class="mono muted">region: {e(json.dumps(l.get('region'), default=str))} · approved by {e(l.get('approved_by'))}</p></li>"""
        for l in data["lessons"])
    fact_rows = "".join(
        f"<tr><td class='mono'>{e(f['key'])}</td><td class='num'>v{e(v['v'])}</td><td>{e(v['status'])}</td>"
        f"<td class='mono'>{e(json.dumps(v['value'], default=str))}</td><td>{e(v['reason'] or 'original')}</td>"
        f"<td>{e(v['by'])}</td></tr>"
        for f in data["fact_history"] for v in f["versions"])
    corr_rows = "".join(
        f"<tr><td class='mono'><a href='#run-{e(c['run'])}'>{e(c['run'])}</a></td><td class='num'>v{e(c['from_v'])} → v{e(c['to_v'])}</td>"
        f"<td class='num'>{_fmt_num(c['from_score'])} → {_fmt_num(c['to_score'])}</td><td>{e(c['status'])}</td>"
        f"<td>{e(c['reason'])}</td></tr>" for c in data["corrections"])
    memory = f"""
<section id="memory">
  <h2>Memory</h2>
  <p class="sub">What EVO carries from one session to the next. Corrections never overwrite: the old version is kept and
  marked superseded.</p>
  <div class="cols">
    <div><h3>Hypotheses</h3><ul class="items">{''.join(hyp_items) or '<li class="muted">none yet</li>'}</ul></div>
    <div><h3>Lessons</h3><ul class="items">{les_items or '<li class="muted">none yet</li>'}</ul></div>
  </div>
  <h3>Corrected facts</h3>
  {('<div class="tablewrap"><table><thead><tr><th>Fact</th><th class="num">Version</th><th>Status</th><th>Value</th><th>Reason</th><th>Approved by</th></tr></thead><tbody>' + fact_rows + '</tbody></table></div>') if fact_rows else '<p class="muted">No fact has been corrected.</p>'}
  <h3>Corrected run scores</h3>
  {('<div class="tablewrap"><table><thead><tr><th>Run</th><th class="num">Version</th><th class="num">Score</th><th>Status</th><th>Reason</th></tr></thead><tbody>' + corr_rows + '</tbody></table></div>') if corr_rows else '<p class="muted">No run score has been corrected.</p>'}
</section>"""

    # -- approvals -----------------------------------------------------------------------
    prow = []
    for pr in data["proposals"]:
        ap = "; ".join(f"{a['decision']} by {a['actor']}" + (f" ({a['comment']})" if a.get("comment") else "")
                       for a in pr["approvals"]) or "-"
        st = {"pending": "b-warn", "approved": "b-good", "executed": "b-good", "rejected": "b-bad"}.get(pr["status"], "b-muted")
        prow.append(f"<tr><td class='num'>#{e(pr['id'])}</td><td class='num'>{e(pr['cycle'])}</td><td>{e(pr['kind'])}</td>"
                    f"<td>{e(pr['what'])}{' · ' + _ref_chip('hyp:' + pr['hypothesis'], known) if pr.get('hypothesis') else ''}</td>"
                    f"<td><span class='badge {st}'>{e(pr['status'])}</span></td><td>{e(ap)}</td></tr>")
    approvals = f"""
<section id="approvals">
  <h2>Proposals and approvals</h2>
  <p class="sub">EVO never launches training or closes a hypothesis on its own. It proposes; you approve or reject with
  <code>evo approve</code> / <code>evo reject</code>. A rejection is remembered and blocks the same proposal next time.</p>
  {('<div class="tablewrap"><table><thead><tr><th class="num">Id</th><th class="num">Cycle</th><th>Kind</th><th>What</th><th>Status</th><th>Your decision</th></tr></thead><tbody>' + ''.join(prow) + '</tbody></table></div>') if prow else '<p class="muted">No proposals yet.</p>'}
</section>"""

    # -- safety --------------------------------------------------------------------------
    drow = []
    for d in data["diagnoses"]:
        det = d.get("detail") or {}
        if d["kind"] == "untrusted_instruction":
            hits = det.get("hits") or []
            detail = "".join(f"<div class='untrusted'><span class='lbl'>untrusted text, line {e(h.get('line'))} · shown as data</span>"
                             f"<code>{e(h.get('excerpt'))}</code></div>" for h in hits)
        else:
            detail = e(_diag_text(d["kind"], det))
        sev = {"warning": "b-warn", "error": "b-bad", "critical": "b-bad", "info": "b-muted"}.get(d.get("severity"), "b-muted")
        drow.append(f"<tr id='diag-{e(d['diag_id'])}'><td class='mono'>diag:{e(d['diag_id'])}</td><td>{e(d['kind'])}</td>"
                    f"<td><span class='badge {sev}'>{e(d.get('severity'))}</span></td>"
                    f"<td class='mono'>{e(d.get('run_name') or '-')}</td><td>{detail}</td></tr>")
    sc = data["status_counts"]
    log_rows = "".join(
        f"<tr class='{'bad' if r['status'] == 'failed' else ''}'><td class='mono'>{e((r.get('started_at') or '')[11:19])}</td>"
        f"<td class='num'>{e(r.get('cycle_id') or '-')}.{e(r.get('step') if r.get('step') is not None else '-')}</td>"
        f"<td class='mono'>{e(r['tool'])}</td><td class='num'>{e(r['attempt'])}</td><td>{e(r['status'])}</td>"
        f"<td class='mono small'>{e(r.get('idempotency_key') or '')}</td><td>{e(r.get('error') or '')}</td></tr>"
        for r in data["log"][-60:])
    safety = f"""
<section id="safety">
  <h2>Safety and the tool-call log</h2>
  <p class="sub">Text from log files is scanned for instructions and only ever passed to the planner as fenced data.
  Every tool call is logged with its memory reads and writes; launches carry an idempotency key so a retry cannot start a
  run twice.</p>
  <h3>Diagnoses</h3>
  {('<div class="tablewrap"><table><thead><tr><th>Ref</th><th>Kind</th><th>Severity</th><th>Run</th><th>Detail</th></tr></thead><tbody>' + ''.join(drow) + '</tbody></table></div>') if drow else '<p class="muted">No diagnoses.</p>'}
  <h3>Tool calls</h3>
  <p class="muted">{s['tool_calls']} logged · {', '.join(f'{k} {v}' for k, v in sorted(sc.items()))}</p>
  <details class="more"><summary>Last {min(60, len(data['log']))} tool calls</summary><div class="tablewrap"><table>
  <thead><tr><th>Time (UTC)</th><th class="num">Cycle.step</th><th>Tool</th><th class="num">Attempt</th><th>Status</th><th>Idempotency key</th><th>Error</th></tr></thead>
  <tbody>{log_rows}</tbody></table></div></details>
</section>"""

    chart_data = {"groups": [{"group": g["group"], "label": g["label"], "is_baseline": g["is_baseline"], "slot": g["slot"],
                              "n": g["n"], "mean": g["mean"], "ci95": g["ci95"], "verdict": VERDICT_TEXT.get(g["verdict"], g["verdict"])}
                             for g in data["groups"]],
                  "curves": data["curves"], "target": p["target"], "baseline": data["baseline_group"]}
    payload = json.dumps(chart_data, default=str).replace("</", "<\\/")

    nav = "".join(f'<a href="#{i}">{t}</a>' for i, t in [("overview", "Overview"), ("how", "How it works"),
                                                         ("results", "Results"), ("cycles", "Cycles"),
                                                         ("memory", "Memory"), ("approvals", "Approvals"),
                                                         ("safety", "Safety & log")])
    return TEMPLATE.replace("{{TITLE}}", e(f"EVO · {p['name']}")).replace("{{NAV}}", nav).replace(
        "{{BODY}}", overview + how + results + cycles + memory + approvals + safety).replace(
        "{{FOOT}}", e(f"Generated {data['generated_at']} from evo.db, read-only. Regenerate with: evo dashboard {p['id']}")
    ).replace("{{DATA}}", payload).replace("{{SERIES_LIGHT}}", _series_css(SERIES_LIGHT)).replace(
        "{{SERIES_DARK}}", _series_css(SERIES_DARK))


def _series_css(cols: list[str]) -> str:
    return " ".join(f"--s{i}: {c};" for i, c in enumerate(cols)) + f" --s8: var(--muted);"


def write_dashboard(scope: ProjectScope, settings: dict, out: Path | None = None) -> Path:
    out = Path(out) if out else scope.root_dir / "reports" / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(collect(scope, settings)), encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root {
  color-scheme: light;
  --page: #f4f3ee; --surface: #eeede6; --ink: #191817; --ink-2: #4a4843; --muted: #75726b; --border: #d8d3c8;
  --accent: #c96442; --ref: #4a4843; --target: #191817;
  --good: #0f7a0f; --bad: #b83232; --warn: #9a6400;
  --good-bg: #e3eedb; --bad-bg: #f5dfdb; --warn-bg: #f4e8cc; --muted-bg: #e6e4dc;
  --serif: "Iowan Old Style", "Charter", "Tiempos Headline", Georgia, serif;
  --sans: "Styrene A", Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace;
  {{SERIES_LIGHT}}
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #1a1918; --surface: #232220; --ink: #edebe5; --ink-2: #c9c6bd; --muted: #9b988f; --border: #3a3834;
    --accent: #df8463; --ref: #c9c6bd; --target: #edebe5;
    --good: #5cc45c; --bad: #ec7b7b; --warn: #e6b04a;
    --good-bg: #1f3320; --bad-bg: #3a2120; --warn-bg: #3a2f17; --muted-bg: #2c2b28;
    {{SERIES_DARK}}
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #1a1918; --surface: #232220; --ink: #edebe5; --ink-2: #c9c6bd; --muted: #9b988f; --border: #3a3834;
  --accent: #df8463; --ref: #c9c6bd; --target: #edebe5;
  --good: #5cc45c; --bad: #ec7b7b; --warn: #e6b04a;
  --good-bg: #1f3320; --bad-bg: #3a2120; --warn-bg: #3a2f17; --muted-bg: #2c2b28;
  {{SERIES_DARK}}
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 64px; }
body { margin: 0; background: var(--page); color: var(--ink); font: 15px/1.55 var(--sans); }
a { color: inherit; text-decoration-color: var(--accent); text-underline-offset: 3px; }
code, .mono { font-family: var(--mono); font-size: 0.86em; }
.small { font-size: 0.78em; }
.muted { color: var(--muted); }
nav.top { position: sticky; top: 0; z-index: 5; background: var(--page); border-bottom: 1px solid var(--border); }
nav.top .in { max-width: 1180px; margin: 0 auto; padding: 0 24px; display: flex; gap: 22px; align-items: center;
  overflow-x: auto; white-space: nowrap; height: 52px; }
nav.top .brand { font-family: var(--serif); font-weight: 500; font-size: 18px; margin-right: 8px; }
nav.top a { text-decoration: none; color: var(--ink-2); font-size: 14px; }
nav.top a:hover { color: var(--ink); text-decoration: underline; text-decoration-color: var(--accent); }
main { max-width: 1180px; margin: 0 auto; padding: 0 24px 96px; }
section { padding-top: 72px; }
h1, h2, h3 { font-family: var(--serif); font-weight: 500; letter-spacing: -0.01em; margin: 0; }
h1 { font-size: 44px; line-height: 1.1; max-width: 24ch; }
h2 { font-size: 30px; margin-bottom: 6px; }
h3 { font-size: 20px; margin: 28px 0 10px; }
.sub { color: var(--ink-2); max-width: 72ch; margin: 0 0 20px; }
.eyebrow { text-transform: uppercase; letter-spacing: 0.08em; font-size: 12px; color: var(--accent); margin: 0 0 10px; }
.lede { font-size: 19px; color: var(--ink-2); max-width: 60ch; margin: 14px 0 28px; }
.hero { padding-top: 48px; }
dl { margin: 0; }
.facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 32px; max-width: 900px; margin-bottom: 32px; }
.facts dt { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.facts dd { margin: 2px 0 0; }
.stats { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); border-top: 1px solid var(--ink);
  border-bottom: 1px solid var(--border); }
.stats div { padding: 14px 16px 16px 0; }
.stats div + div { padding-left: 16px; border-left: 1px solid var(--border); }
.stats dt { font-size: 13px; color: var(--ink-2); }
.stats dd { margin: 4px 0 0; font-family: var(--serif); font-size: 34px; line-height: 1; }
.stats dd small { display: block; font-family: var(--sans); font-size: 12px; color: var(--muted); margin-top: 6px; }
.flow { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr));
  border-top: 1px solid var(--border); }
.flow li { padding: 14px 14px 4px 0; display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--ink-2); }
.flow li + li { padding-left: 14px; border-left: 1px solid var(--border); }
.flow .n { font-family: var(--serif); font-size: 24px; color: var(--accent); line-height: 1; }
.flow b { color: var(--ink); font-size: 15px; }
figure.chart { margin: 0 0 32px; background: var(--surface); border: 1px solid var(--border); padding: 18px 18px 8px; }
figcaption { display: flex; flex-direction: column; gap: 2px; margin-bottom: 10px; }
figcaption b { font-family: var(--serif); font-weight: 500; font-size: 18px; }
figcaption span { color: var(--ink-2); font-size: 13px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center; margin-bottom: 8px; }
.seg { display: inline-flex; border: 1px solid var(--border); }
.seg button { font: inherit; font-size: 13px; border: 0; background: transparent; color: var(--ink-2); padding: 4px 10px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--page); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 10px; }
.legend button { font: inherit; font-size: 13px; color: var(--ink); background: transparent; border: 1px solid var(--border);
  padding: 4px 10px 4px 8px; display: inline-flex; align-items: center; gap: 7px; cursor: pointer; border-radius: 2px; }
.legend button[aria-pressed="false"] { color: var(--muted); text-decoration: line-through; }
.legend button[aria-pressed="false"] .sw { opacity: 0.3; }
.sw { display: inline-block; width: 14px; height: 3px; background: var(--c, var(--ref)); border-radius: 2px; vertical-align: middle;
  margin-right: 7px; }
.sw.ref { background: repeating-linear-gradient(90deg, var(--ref) 0 4px, transparent 4px 6px); }
.plot { position: relative; width: 100%; }
.plot svg { display: block; width: 100%; height: auto; overflow: visible; }
.plot .grid line { stroke: var(--border); stroke-width: 1; }
.plot .axis text { fill: var(--muted); font: 11px var(--sans); }
.plot .lbl-t { fill: var(--ink-2); font: 11px var(--sans); paint-order: stroke; stroke: var(--surface); stroke-width: 4px; stroke-linejoin: round; }
.tip { position: absolute; pointer-events: none; background: var(--page); border: 1px solid var(--border); padding: 8px 10px;
  font-size: 12px; min-width: 160px; max-width: 280px; display: none; z-index: 3; box-shadow: 0 4px 16px rgba(0,0,0,0.08); }
.tip b { display: block; margin-bottom: 4px; font-size: 12px; }
.tip .r { display: flex; justify-content: space-between; gap: 12px; }
.tip .r span:first-child { display: inline-flex; align-items: center; color: var(--ink-2); }
.tablewrap { overflow-x: auto; margin: 0 0 16px; border-top: 1px solid var(--ink); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th { text-align: left; font-weight: 600; font-size: 12px; color: var(--ink-2); padding: 9px 12px 7px 0; border-bottom: 1px solid var(--border); }
td { padding: 9px 12px 9px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.bad td { color: var(--bad); }
:target { background: var(--warn-bg); }
.badge { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 2px; white-space: nowrap; font-weight: 500; }
.b-good { background: var(--good-bg); color: var(--good); }
.b-bad { background: var(--bad-bg); color: var(--bad); }
.b-warn { background: var(--warn-bg); color: var(--warn); }
.b-muted { background: var(--muted-bg); color: var(--ink-2); }
.b-ref { background: transparent; border: 1px solid var(--border); color: var(--ink-2); }
.flag { display: inline-block; font-family: var(--mono); font-size: 11px; color: var(--ink-2); border: 1px solid var(--border);
  padding: 0 6px; margin: 0 4px 2px 0; }
details.more summary, details.brief summary { cursor: pointer; color: var(--ink-2); font-size: 14px; padding: 6px 0; }
details.more summary:hover, details.brief summary:hover { color: var(--ink); }
details.brief pre { background: var(--surface); border: 1px solid var(--border); padding: 12px; max-height: 360px; overflow: auto;
  font: 12px/1.45 var(--mono); white-space: pre-wrap; word-break: break-word; }
article.cycle { border-top: 1px solid var(--ink); padding: 18px 0 8px; margin-bottom: 28px; }
article.cycle header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; }
article.cycle h3 { margin: 0; font-size: 24px; }
.meta { color: var(--ink-2); font-size: 14px; margin: 0; }
ol.decisions { list-style: none; padding: 0; margin: 14px 0 0; }
li.dec { padding: 12px 0 12px 16px; border-left: 2px solid var(--border); margin-bottom: 10px; }
li.dec.refused { border-left-color: var(--bad); }
li.dec p, li.dec ul.reasons, li.dec .evidence, li.dec details { margin-left: 2em; }
li.dec p { margin-top: 6px; margin-bottom: 6px; max-width: 90ch; color: var(--ink-2); }
.dec-head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
.step { font-family: var(--serif); font-size: 18px; color: var(--muted); min-width: 1.2em; }
code.act { font-size: 14px; font-weight: 600; color: var(--ink); }
.args { font-family: var(--mono); font-size: 12px; color: var(--ink-2); word-break: break-word; margin: 4px 0 0 2em; }
ul.reasons { margin: 4px 0 6px; padding-left: 18px; color: var(--bad); font-size: 14px; }
.evidence { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 6px; }
.lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-right: 6px; }
.chip { font-family: var(--mono); font-size: 11.5px; border: 1px solid var(--border); padding: 1px 7px; text-decoration: none;
  color: var(--ink-2); background: var(--page); word-break: break-all; }
a.chip:hover { border-color: var(--accent); color: var(--ink); }
.rec { background: var(--surface); border: 1px solid var(--border); padding: 12px 14px; margin: 6px 0 0; max-width: 100ch; }
.cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 40px; }
ul.items { list-style: none; padding: 0; margin: 0; }
ul.items li { border-top: 1px solid var(--border); padding: 12px 0; }
ul.items li p { margin: 6px 0; }
ul.items .row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.untrusted { border-left: 2px solid var(--warn); padding: 4px 0 4px 10px; margin: 2px 0; }
.untrusted code { display: block; word-break: break-word; white-space: pre-wrap; }
footer { max-width: 1180px; margin: 0 auto; padding: 24px; border-top: 1px solid var(--border); color: var(--muted); font-size: 13px; }
@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .stats div:nth-child(4) { border-left: 0; padding-left: 0; }
  .flow { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .flow li, .flow li + li { border-left: 0; padding-left: 0; border-top: 1px solid var(--border); padding-top: 12px; }
  .cols { grid-template-columns: 1fr; gap: 0; }
}
@media (max-width: 560px) {
  nav.top .in, main { padding-left: 16px; padding-right: 16px; }
  h1 { font-size: 32px; }
  .facts { grid-template-columns: 1fr; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stats div, .stats div + div { border-left: 0; padding-left: 0; }
  .flow { grid-template-columns: 1fr; }
  figure.chart { padding: 12px 10px 6px; }
  li.dec { padding-left: 10px; }
  li.dec p, li.dec ul.reasons, li.dec .evidence, li.dec details, .args { margin-left: 0; }
}
@media print { nav.top { position: static; } details { display: block; } }
</style>
</head>
<body>
<nav class="top" aria-label="Sections"><div class="in"><span class="brand">EVO</span>{{NAV}}</div></nav>
<main>
{{BODY}}
</main>
<footer>{{FOOT}}</footer>
<script id="evo-data" type="application/json">{{DATA}}</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("evo-data").textContent);
  var NS = "http://www.w3.org/2000/svg";
  var groups = {}; D.groups.forEach(function (g) { groups[g.group] = g; });
  function color(gid) {
    var g = groups[gid];
    if (!g || g.is_baseline) return "var(--ref)";
    return "var(--s" + Math.min(g.slot == null ? 8 : g.slot, 8) + ")";
  }
  function el(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function nice(lo, hi, n) {
    var span = hi - lo || 1, step = Math.pow(10, Math.floor(Math.log10(span / n))), err = span / n / step;
    step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
    var t = [], v = Math.ceil(lo / step) * step;
    for (; v <= hi + 1e-9; v += step) t.push(+v.toFixed(6));
    return t;
  }
  function fmtStep(v) { return v >= 1000 ? (v / 1000) + "k" : String(v); }
  function f2(v) { return v == null ? "-" : (+v).toFixed(2); }

  // ---------------- learning curves ----------------
  var hidden = {};
  var order = D.groups.slice().sort(function (a, b) { return (a.is_baseline ? -1 : (a.slot == null ? 99 : a.slot)) - (b.is_baseline ? -1 : (b.slot == null ? 99 : b.slot)); });
  var legend = document.getElementById("curve-legend");
  order.forEach(function (g) {
    if (!D.curves.some(function (c) { return c.group === g.group && c.points.length; })) return;
    var b = document.createElement("button");
    b.type = "button"; b.setAttribute("aria-pressed", "true");
    b.innerHTML = "<i class='sw" + (g.is_baseline ? " ref" : "") + "' style='--c:" + color(g.group) + "'></i>" + esc(g.label) +
      " <span class='muted'>(" + D.curves.filter(function (c) { return c.group === g.group; }).length + ")</span>";
    b.addEventListener("click", function () {
      hidden[g.group] = !hidden[g.group];
      b.setAttribute("aria-pressed", hidden[g.group] ? "false" : "true");
      drawCurves();
    });
    legend.appendChild(b);
  });

  var mode = "mean";
  Array.prototype.forEach.call(document.querySelectorAll(".seg button"), function (b) {
    b.addEventListener("click", function () {
      mode = b.getAttribute("data-mode");
      Array.prototype.forEach.call(document.querySelectorAll(".seg button"), function (o) {
        o.setAttribute("aria-pressed", o === b ? "true" : "false");
      });
      drawCurves();
    });
  });
  function groupMeans(vis) {
    var by = {};
    vis.forEach(function (c) {
      if (c.status !== "completed") return;
      (by[c.group] = by[c.group] || []).push(c);
    });
    return Object.keys(by).map(function (gid) {
      var acc = {};
      by[gid].forEach(function (c) { c.points.forEach(function (p) { (acc[p.x] = acc[p.x] || []).push(p.y); }); });
      var pts = Object.keys(acc).map(Number).sort(function (a, b) { return a - b; }).map(function (x) {
        var v = acc[x]; return {x: x, y: v.reduce(function (a, b) { return a + b; }, 0) / v.length, n: v.length};
      });
      return {run: (groups[gid] || {}).label || gid, group: gid, status: "completed", points: pts, isMean: true};
    });
  }
  function drawCurves() {
    var host = document.getElementById("curve-plot");
    host.innerHTML = "";
    var W = Math.max(320, host.clientWidth), H = W < 560 ? 260 : 340, m = {l: 48, r: 16, t: 12, b: 28};
    var vis = D.curves.filter(function (c) { return !hidden[c.group] && c.points.length; });
    var all = D.curves.filter(function (c) { return c.points.length; });
    if (!all.length) { host.innerHTML = "<p class='muted'>No evaluation points yet.</p>"; return; }
    var xs = [], ys = [];
    all.forEach(function (c) { c.points.forEach(function (p) { xs.push(p.x); ys.push(p.y); }); });
    if (D.target != null) ys.push(+D.target);
    var x0 = 0, x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    var pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
    var X = function (v) { return m.l + (v - x0) / (x1 - x0 || 1) * (W - m.l - m.r); };
    var Y = function (v) { return m.t + (1 - (v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b); };
    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, role: "img",
      "aria-label": "Learning curves: eval return against training steps for " + all.length + " runs"}, host);
    var grid = el("g", {"class": "grid"}, svg), axis = el("g", {"class": "axis"}, svg);
    nice(y0, y1, 5).forEach(function (t) {
      el("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t)}, grid);
      el("text", {x: m.l - 8, y: Y(t) + 4, "text-anchor": "end"}, axis).textContent = t;
    });
    nice(x0, x1, W < 560 ? 4 : 8).forEach(function (t) {
      el("text", {x: X(t), y: H - 8, "text-anchor": "middle"}, axis).textContent = fmtStep(t);
    });
    if (D.target != null) {
      el("line", {x1: m.l, x2: W - m.r, y1: Y(+D.target), y2: Y(+D.target), stroke: "var(--target)", "stroke-width": 1,
        "stroke-dasharray": "5 4", opacity: 0.7}, svg);
      el("text", {x: W - m.r, y: Y(+D.target) - 6, "text-anchor": "end", "class": "lbl-t"}, svg).textContent = "target " + D.target;
    }
    var lines = el("g", {}, svg);
    var baseFirst = function (a, b) { return (groups[a.group] && groups[a.group].is_baseline ? 0 : 1) - (groups[b.group] && groups[b.group].is_baseline ? 0 : 1); };
    vis.sort(baseFirst);
    function path(c, w, op) {
      var d = c.points.map(function (p, i) { return (i ? "L" : "M") + X(p.x).toFixed(1) + " " + Y(p.y).toFixed(1); }).join("");
      el("path", {d: d, fill: "none", stroke: color(c.group), "stroke-width": w, "stroke-linejoin": "round",
        "stroke-linecap": "round", "stroke-dasharray": c.status === "completed" ? "" : "5 4", opacity: op}, lines);
    }
    var series = mode === "mean" ? groupMeans(vis).sort(baseFirst) : vis;
    if (mode === "mean") vis.forEach(function (c) { path(c, 1, 0.2); });
    series.forEach(function (c) { path(c, mode === "mean" ? 2.5 : 2, mode === "mean" ? 1 : 0.9); });
    // hover layer: crosshair + tooltip
    var cross = el("line", {y1: m.t, y2: H - m.b, stroke: "var(--ink-2)", "stroke-width": 1, opacity: 0}, svg);
    var dots = el("g", {}, svg);
    var tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
    var steps = Array.from(new Set(xs)).sort(function (a, b) { return a - b; });
    var hit = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"}, svg);
    function move(ev) {
      var r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
      var v = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0), best = steps[0];
      steps.forEach(function (s) { if (Math.abs(s - v) < Math.abs(best - v)) best = s; });
      cross.setAttribute("x1", X(best)); cross.setAttribute("x2", X(best)); cross.setAttribute("opacity", 0.5);
      dots.innerHTML = "";
      var rows = [];
      series.forEach(function (c) {
        var p = c.points.find(function (q) { return q.x === best; });
        if (!p) return;
        el("circle", {cx: X(p.x), cy: Y(p.y), r: 4, fill: color(c.group), stroke: "var(--surface)", "stroke-width": 2}, dots);
        rows.push({run: c.isMean ? c.run + " (" + p.n + " seeds)" : c.run, g: c.group, y: p.y});
      });
      rows.sort(function (a, b) { return b.y - a.y; });
      var more = rows.length > 8 ? "<div class='muted'>+" + (rows.length - 8) + " more</div>" : "";
      tip.innerHTML = "<b>step " + fmtStep(best) + "</b>" + rows.slice(0, 8).map(function (q) {
        return "<div class='r'><span><i class='sw" + (groups[q.g] && groups[q.g].is_baseline ? " ref" : "") +
          "' style='--c:" + color(q.g) + "'></i>" + esc(q.run) + "</span><span>" + f2(q.y) + "</span></div>";
      }).join("") + more;
      tip.style.display = rows.length ? "block" : "none";
      var hostR = host.getBoundingClientRect(), tx = ev.clientX - hostR.left + 14;
      if (tx + 290 > hostR.width) tx = ev.clientX - hostR.left - 14 - Math.min(290, tip.offsetWidth || 220);
      tip.style.left = Math.max(0, tx) + "px"; tip.style.top = Math.max(0, ev.clientY - hostR.top - 20) + "px";
    }
    hit.addEventListener("mousemove", move);
    hit.addEventListener("mouseleave", function () { tip.style.display = "none"; cross.setAttribute("opacity", 0); dots.innerHTML = ""; });
  }

  // ---------------- group means with CI ----------------
  function drawCI() {
    var host = document.getElementById("ci-plot");
    host.innerHTML = "";
    var gs = D.groups.filter(function (g) { return g.mean != null; })
      .sort(function (a, b) { return a.is_baseline ? -1 : b.is_baseline ? 1 : b.mean - a.mean; });
    if (!gs.length) { host.innerHTML = "<p class='muted'>No scored groups yet.</p>"; return; }
    var W = Math.max(320, host.clientWidth), narrow = W < 560, rowH = 34, labW = narrow ? 120 : 220;
    var m = {l: labW, r: 20, t: 8, b: 28}, H = m.t + m.b + rowH * gs.length;
    var vals = []; gs.forEach(function (g) { vals.push(g.mean); if (g.ci95) { vals.push(g.ci95[0], g.ci95[1]); } });
    if (D.target != null) vals.push(+D.target);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    var X = function (v) { return m.l + (v - lo) / (hi - lo || 1) * (W - m.l - m.r); };
    var svg = el("svg", {viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Mean score and 95% CI per config"}, host);
    var grid = el("g", {"class": "grid"}, svg), axis = el("g", {"class": "axis"}, svg);
    nice(lo, hi, narrow ? 4 : 7).forEach(function (t) {
      el("line", {x1: X(t), x2: X(t), y1: m.t, y2: H - m.b}, grid);
      el("text", {x: X(t), y: H - 8, "text-anchor": "middle"}, axis).textContent = t;
    });
    var base = gs.find(function (g) { return g.is_baseline; });
    if (base) el("line", {x1: X(base.mean), x2: X(base.mean), y1: m.t, y2: H - m.b, stroke: "var(--ref)", "stroke-width": 1.5}, svg);
    if (D.target != null) el("line", {x1: X(+D.target), x2: X(+D.target), y1: m.t, y2: H - m.b, stroke: "var(--target)",
      "stroke-width": 1, "stroke-dasharray": "5 4", opacity: 0.7}, svg);
    var tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip);
    gs.forEach(function (g, i) {
      var cy = m.t + rowH * i + rowH / 2, c = color(g.group);
      var lab = g.label.length > (narrow ? 16 : 30) ? g.label.slice(0, narrow ? 15 : 29) + "…" : g.label;
      el("text", {x: 0, y: cy + 4, "class": "lbl-t"}, svg).textContent = lab + "  n=" + g.n;
      if (g.ci95) el("line", {x1: X(g.ci95[0]), x2: X(g.ci95[1]), y1: cy, y2: cy, stroke: c, "stroke-width": 3,
        "stroke-linecap": "round", opacity: 0.55}, svg);
      el("circle", {cx: X(g.mean), cy: cy, r: 6, fill: c, stroke: "var(--surface)", "stroke-width": 2}, svg);
      var hit = el("rect", {x: 0, y: cy - rowH / 2, width: W, height: rowH, fill: "transparent"}, svg);
      hit.addEventListener("mousemove", function (ev) {
        tip.innerHTML = "<b>" + esc(g.label) + "</b><div class='r'><span>mean</span><span>" + f2(g.mean) + "</span></div>" +
          "<div class='r'><span>95% CI</span><span>" + (g.ci95 ? f2(g.ci95[0]) + " to " + f2(g.ci95[1]) : "-") + "</span></div>" +
          "<div class='r'><span>seeds</span><span>" + g.n + "</span></div>" +
          "<div class='r'><span>vs baseline</span><span>" + esc(g.is_baseline ? "baseline" : g.verdict) + "</span></div>";
        tip.style.display = "block";
        var hr = host.getBoundingClientRect(), tx = ev.clientX - hr.left + 14;
        if (tx + 240 > hr.width) tx = ev.clientX - hr.left - 250;
        tip.style.left = Math.max(0, tx) + "px"; tip.style.top = (cy - 10) + "px";
      });
      hit.addEventListener("mouseleave", function () { tip.style.display = "none"; });
    });
  }

  function drawAll() { drawCurves(); drawCI(); }
  drawAll();
  var t; window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(drawAll, 120); });
})();
</script>
</body>
</html>
"""
