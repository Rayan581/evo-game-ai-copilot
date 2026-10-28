"""Tool: export_register - Excel review register + Markdown cycle report.

evo.db is authoritative. The workbook is a generated review view. Columns whose
header starts with "User" belong to the user: on every export they are read
back from the existing file (keyed by the row's stable ID) and written again
unchanged, so an export never overwrites the user's notes. To make a decision
binding, the user records it with `evo approve` / `evo reject`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from evo.memory.store import ProjectScope
from evo.util import now_iso

USER_COLS = ["User decision", "User comment"]
HEADER_FILL = PatternFill("solid", fgColor="E8E4DA")
USER_FILL = PatternFill("solid", fgColor="FFF4D6")


def _existing_user_values(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """{sheet: {row_key: {user_col: value}}} from a previous export."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not path.exists():
        return out
    wb = load_workbook(path)
    for ws in wb.worksheets:
        headers = [c.value for c in ws[1]]
        if not headers or headers[0] != "Key":
            continue
        idx = {h: i for i, h in enumerate(headers) if isinstance(h, str) and h.startswith("User")}
        sheet: dict[str, dict[str, Any]] = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] is None:
                continue
            vals = {h: row[i] for h, i in idx.items() if row[i] not in (None, "")}
            if vals:
                sheet[str(row[0])] = vals
        out[ws.title] = sheet
    return out


def _write_sheet(wb: Workbook, title: str, cols: list[str], rows: list[dict[str, Any]],
                 user_prev: dict[str, dict[str, Any]]) -> None:
    ws = wb.create_sheet(title)
    headers = ["Key"] + cols + USER_COLS
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = USER_FILL if str(c.value).startswith("User") else HEADER_FILL
    for r in rows:
        key = str(r["Key"])
        prev = user_prev.get(key, {})
        ws.append([key] + [r.get(c) for c in cols] + [prev.get(u) for u in USER_COLS])
    for i, h in enumerate(headers, start=1):
        width = max(len(str(h)), *(len(str(ws.cell(row=j, column=i).value or "")) for j in range(2, ws.max_row + 1))) \
            if ws.max_row > 1 else len(str(h))
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 60)
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "B2"


def export_register(scope: ProjectScope, analysis: dict[str, Any]) -> dict[str, Any]:
    reports = scope.root_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / "register.xlsx"
    prev = _existing_user_values(path)
    preserved = sum(len(v) for v in prev.values())

    wb = Workbook()
    wb.remove(wb.active)
    groups = {g["group"]: g for g in analysis["groups"]}
    run_rows = []
    for r in analysis["runs"]:
        g = groups.get(r["group"], {})
        vs = g.get("vs_baseline") or {}
        run_rows.append({
            "Key": f"run:{r['run']}", "Run": r["run"], "Config": r["label"], "Seed": r["seed"], "Status": r["status"],
            "Score (last k evals)": r["score"], "Metric version": r["metric_version"],
            "Flags": ", ".join(r["flags"]), "Launched by": r["launched_by"],
            "Group mean": g.get("mean"), "Group seeds": g.get("n"),
            "Agent verdict vs baseline": vs.get("verdict", "baseline" if g.get("is_baseline") else ""),
            "Diff CI95": str(vs.get("diff_ci95") or ""),
        })
    _write_sheet(wb, "Runs", [c for c in run_rows[0].keys() if c != "Key"] if run_rows else ["Run"], run_rows,
                 prev.get("Runs", {}))

    hyp_rows = []
    for h in scope.hypotheses():
        res = h.get("resolution") or {}
        hyp_rows.append({"Key": f"hyp:{h['label']}", "Hypothesis": h["label"], "Statement": h["statement"],
                         "Change": str(h["change"]), "Status": h["status"], "Opened cycle": h["opened_cycle"],
                         "Resolved cycle": h["resolved_cycle"], "Evidence": ", ".join(h["evidence"]),
                         "Resolution": res.get("summary", "")})
    _write_sheet(wb, "Hypotheses", ["Hypothesis", "Statement", "Change", "Status", "Opened cycle", "Resolved cycle",
                                    "Evidence", "Resolution"], hyp_rows, prev.get("Hypotheses", {}))

    dec_rows = []
    for d in scope.decisions():
        dec_rows.append({"Key": f"decision:{d['cycle_number']}.{d['step']}", "Cycle": d["cycle_number"],
                         "Step": d["step"], "Action": d["action"], "Allowed": d["gate"]["allowed"],
                         "Gate reasons": "; ".join(d["gate"]["reasons"]), "Rationale": d["rationale"],
                         "Evidence": ", ".join(d["evidence"]), "Memory": d["memory_mode"],
                         "Planner": f"{d['provider']}/{d['model']}", "At": d["created_at"]})
    _write_sheet(wb, "Decisions", ["Cycle", "Step", "Action", "Allowed", "Gate reasons", "Rationale", "Evidence",
                                   "Memory", "Planner", "At"], dec_rows, prev.get("Decisions", {}))

    prop_rows = []
    for p in scope.proposals():
        prop_rows.append({"Key": f"proposal:{p['proposal_id']}", "Kind": p["kind"], "Status": p["status"],
                          "Payload": str(p["payload"]), "Rationale": p["rationale"],
                          "Evidence": ", ".join(p["evidence"]), "Created by": p["created_by"],
                          "Decided at": p["decided_at"]})
    _write_sheet(wb, "Approvals", ["Kind", "Status", "Payload", "Rationale", "Evidence", "Created by", "Decided at"],
                 prop_rows, prev.get("Approvals", {}))

    info = wb.create_sheet("About", 0)
    info.append(["EVO review register", scope.pid])
    info.append(["Generated", now_iso()])
    info.append(["Authoritative store", str(scope.store.db_path)])
    info.append(["Note", "Generated view. Columns starting with 'User' are yours and are preserved on every export. "
                         "Record binding decisions with `evo approve` / `evo reject`."])
    info.column_dimensions["A"].width = 22
    info.column_dimensions["B"].width = 100
    wb.save(path)
    scope.t.write(f"file:{path.name}")
    return {"path": str(path), "user_cells_preserved": preserved, "rows": {"runs": len(run_rows),
            "hypotheses": len(hyp_rows), "decisions": len(dec_rows), "proposals": len(prop_rows)}}


def write_cycle_report(scope: ProjectScope, cycle: dict, analysis: dict, summary: dict) -> str:
    reports = scope.root_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"cycle-{cycle['number']:02d}.md"
    lines = [f"# {scope.pid} - review cycle {cycle['number']}", "",
             f"- Clock: {cycle['clock']}  |  memory: {cycle['memory_mode']}  |  generated {now_iso()}",
             f"- Metric: {analysis['metric']['name']} (k={analysis['metric']['k']}), target {analysis['metric']['target']}",
             "", "## Config groups", "", "| Config | Seeds | Mean | CI95 | vs baseline | Flags |", "|---|---|---|---|---|---|"]
    for g in sorted(analysis["groups"], key=lambda g: (g["mean"] is None, -(g["mean"] or 0))):
        vs = g.get("vs_baseline") or {}
        flags = ", ".join(f"{k}x{v}" for k, v in g["flags"].items())
        lines.append(f"| {g['label']} | {g['n']} | {g['mean']} | {g['ci95'] or ''} | "
                     f"{vs.get('verdict', 'baseline' if g['is_baseline'] else '')} {vs.get('diff_ci95') or ''} | {flags} |")
    lines += ["", "## Decisions", ""]
    for d in scope.decisions(cycle_id=cycle["cycle_id"]):
        ok = "allowed" if d["gate"]["allowed"] else "REFUSED: " + "; ".join(d["gate"]["reasons"])
        lines.append(f"{d['step']}. **{d['action']}** {d['arguments']} - {ok}")
        lines.append(f"   - rationale: {d['rationale']}")
        if d["evidence"]:
            lines.append(f"   - evidence: {', '.join(d['evidence'])}")
    lines += ["", "## Outcome", "", f"- Recommendation: {summary.get('recommendation', '')}",
              f"- Pending approvals: {summary.get('pending', [])}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
